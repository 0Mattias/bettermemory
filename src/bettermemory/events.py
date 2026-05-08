"""Append-only JSONL event log for instrumentation.

The `Recorder` writes one JSON object per line to `<root>/.events.jsonl`.
Rotates to `.events-<timestamp>.jsonl.gz` when the active file crosses
`max_bytes`. The log lives next to the memories so it shares the same trust
boundary — no separate permissions story, no separate gitignore decisions.

Events are append-only by design. They're the substrate that downstream
tooling reads from:

- the `memory_health` view aggregates dead-weight and heavily-used memories,
- the `memory_record_use` signal feeds back into ranking,
- the durability marker list gets tuned against real write traffic.

Don't truncate or modify the file in place; treat it as an audit log. If you
need to reset the store, rotate or delete the whole file rather than editing.

Privacy note: search queries are recorded verbatim. The log lives in the same
directory as the memories themselves, which already contain user data — we're
not crossing a new trust boundary. Disable via `[telemetry] enabled = false`
in `config.toml` if this is unwanted.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("bettermemory.events")


EVENT_LOG_FILENAME = ".events.jsonl"
ARCHIVE_PREFIX = ".events-"
ARCHIVE_SUFFIX = ".jsonl.gz"
DEFAULT_MAX_BYTES = 10_000_000  # 10 MB before rotation.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Same lock pattern as store.py — duplicated rather than shared because
# the two modules have different invariants (the store locks per-memory-file;
# events locks the single append log) and a shared helper would obscure that.
#
# Windows doesn't have fcntl; we no-op the lock there. The MVP single-process
# assumption (see store.py) means concurrent appends shouldn't happen anyway;
# the lock is belt-and-suspenders against a future async/multi-process world.
# The sys.platform guard is the form mypy understands as platform narrowing.
@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    if sys.platform == "win32":  # pragma: no cover - non-unix
        yield
        return

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()


@dataclass
class Recorder:
    """Append-only JSONL event recorder.

    Construct once per process, thread into tool handlers, call `record()`
    once per tool invocation. `enabled=False` makes every call a no-op so
    handlers can call unconditionally without an `if recorder` guard each
    time.

    Failure during a record is intentionally swallowed: a logging hiccup
    must never break a tool call. Errors are logged at WARNING and dropped.
    """

    root: Path
    session_id: str
    enabled: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / EVENT_LOG_FILENAME

    def record(self, kind: str, **fields: Any) -> None:
        """Append one event of the given `kind`. Extra `fields` are merged
        into the event dict. Best-effort — failures are logged, not raised.
        """
        if not self.enabled:
            return
        try:
            event = {
                "ts": _utcnow_iso(),
                "session": self.session_id,
                "kind": kind,
                **fields,
            }
            line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
            with _locked(self.path):
                self._rotate_if_needed()
                # Append-binary so we control line endings explicitly across
                # platforms and don't fight Python's text-mode translation.
                with self.path.open("ab") as f:
                    f.write(line.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — never break the caller.
            log.warning("event log write failed (kind=%s): %s", kind, exc)

    # ---- internals --------------------------------------------------------

    def _rotate_if_needed(self) -> None:
        """Gzip-rotate the active log if it has crossed `max_bytes`."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        except (
            OSError
        ) as exc:  # pragma: no cover — disk issues shouldn't kill the recorder.
            log.warning("event log stat failed: %s", exc)
            return

        if size < self.max_bytes:
            return

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = self.root / f"{ARCHIVE_PREFIX}{ts}{ARCHIVE_SUFFIX}"
        # Multiple rotations in the same UTC second collide on the timestamp
        # (real-world pathological — tests with tiny `max_bytes` hit it
        # immediately). First fall back to a session-tagged name; then a
        # numeric counter until we find an unused path. Bounded by the
        # number of bytes we've actually written, so the loop terminates.
        if archive.exists():
            archive = self.root / (
                f"{ARCHIVE_PREFIX}{ts}-{self.session_id}{ARCHIVE_SUFFIX}"
            )
        counter = 1
        while archive.exists():
            archive = self.root / (
                f"{ARCHIVE_PREFIX}{ts}-{self.session_id}-{counter}{ARCHIVE_SUFFIX}"
            )
            counter += 1
        try:
            with self.path.open("rb") as src, gzip.open(archive, "wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            self.path.unlink()
        except OSError as exc:
            log.warning("event log rotation failed: %s", exc)


# ---------------------------------------------------------------------------
# Read side — used by tests, future memory_health, debugging
# ---------------------------------------------------------------------------


def iter_events(root: Path) -> Iterator[dict[str, Any]]:
    """Yield all events from the *active* log only.

    Skips malformed lines defensively (single-process writers make corruption
    unlikely, but the read side stays robust against external editing). Does
    not read rotated archives — call `iter_all_events` for that.
    """
    path = root / EVENT_LOG_FILENAME
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _archive_sort_key(path: Path) -> tuple[int, int]:
    """Sort key for `iter_all_events` archive ordering.

    Primary: mtime_ns. Secondary: write-order index parsed from the
    filename. The secondary tiebreak only matters when the filesystem
    timestamp resolution is too coarse to separate rapid rotations
    within a single UTC second — Windows in particular records mtime
    at ~10ms granularity, so a test that calls `record()` 15 times in
    a row with `max_bytes=120` can produce multiple archives sharing
    one `mtime_ns`.

    The index is derived from the filename suffix structure; see the
    `_rotate_if_needed` collision-handling for the producer side.
    Bare `{ts}` -> 0, `{ts}-{session}` -> 1, `{ts}-{session}-N` -> 1+N.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    inner = path.name[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]
    parts = inner.split("-")
    # parts[0] is the timestamp; subsequent parts are session/counter.
    if len(parts) <= 1:
        return (mtime, 0)
    if len(parts) == 2:
        # `.events-{ts}-{session}.jsonl.gz` — second-write-of-second.
        return (mtime, 1)
    # `.events-{ts}-{session}-N.jsonl.gz` — third or later.
    try:
        return (mtime, 1 + int(parts[-1]))
    except ValueError:
        # Malformed counter — fall back to "after the bare/single forms".
        return (mtime, 2)


def iter_all_events(root: Path) -> Iterator[dict[str, Any]]:
    """Yield events from rotated archives + active log, in chronological order.

    Archive filenames embed a UTC timestamp, so lexicographic sort is
    chronological. The active log is yielded last. Used by `memory_health`
    in Phase 5 and by anything that wants the full history.
    """
    if not root.exists():
        return
    try:
        entries = list(root.iterdir())
    except OSError:  # pragma: no cover
        return

    archives = [
        p
        for p in entries
        if p.is_file()
        and p.name.startswith(ARCHIVE_PREFIX)
        and p.name.endswith(ARCHIVE_SUFFIX)
    ]
    # Sort by (mtime, in-second-counter). Naive filename sort is wrong
    # because collision-handling produces names like
    # `.events-{ts}-N.jsonl.gz` that lex-sort *before* the bare
    # `.events-{ts}.jsonl.gz` (since `-` < `.`). And mtime alone is
    # unreliable on Windows, where the filesystem timestamp resolution is
    # coarse enough that several rapid rotations land on the same
    # mtime_ns and the secondary sort is undefined. Parsing the suffix
    # counter out of the filename gives the right write-order tiebreak
    # within a single UTC second:
    #   .events-{ts}.jsonl.gz                  -> 0
    #   .events-{ts}-{session}.jsonl.gz        -> 1
    #   .events-{ts}-{session}-1.jsonl.gz      -> 2
    #   .events-{ts}-{session}-N.jsonl.gz      -> 1+N
    archives.sort(key=_archive_sort_key)
    for archive in archives:
        try:
            with gzip.open(archive, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:  # pragma: no cover
            continue

    yield from iter_events(root)


__all__ = [
    "Recorder",
    "iter_events",
    "iter_all_events",
    "EVENT_LOG_FILENAME",
    "DEFAULT_MAX_BYTES",
]
