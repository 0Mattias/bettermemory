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

import gzip
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ._fsutil import flock_excl, fsync_dir, fsync_file

log = logging.getLogger("bettermemory.events")


EVENT_LOG_FILENAME = ".events.jsonl"
ARCHIVE_PREFIX = ".events-"
ARCHIVE_SUFFIX = ".jsonl.gz"
DEFAULT_MAX_BYTES = 10_000_000  # 10 MB before rotation.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# `_locked` is re-exported here as the local symbol for the event log's
# append path; the canonical definition lives in `_fsutil.flock_excl`
# (single source — 2.6.3 audit-pass-of-audit-pass found the matching
# `_locked` in store.py and events.py had drifted in comments alone, and
# the unlink-on-finally regression risk doubles with each duplicate).
# Top-level assignment (not `import flock_excl as _locked`) so mypy strict's
# no_implicit_reexport rule accepts external imports of `_locked` here.
_locked = flock_excl


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
                # fsync the file after each event so the audit log survives
                # a crash. One event per tool call, so the fsync cost is
                # negligible compared to the value of not losing audit
                # records in a power-loss scenario.
                first_write = not self.path.exists()
                with self.path.open("ab") as f:
                    f.write(line.encode("utf-8"))
                    f.flush()
                    fsync_file(f.fileno())
                # Tighten permissions on first write — without this, the
                # log inherits the user umask (typically 0o644) and ends
                # up world-readable. Event records carry session ids and
                # the raw user/model queries that triggered them; that's
                # private user data on a shared-user box. No-op on
                # Windows. Done outside the open() block so the chmod
                # doesn't race the buffered append.
                #
                # Pre-2.6.4 a chmod failure was silently suppressed —
                # the log would land world-readable and nothing flagged
                # the gap. Log WARNING so the operator at least sees it
                # in the logs and can investigate (typical causes:
                # noexec/nosuid mounts in containers, root-owned dirs
                # on shared boxes, restricted filesystems).
                if first_write:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError as chmod_exc:
                        log.warning(
                            "event log %s: chmod 0o600 failed (%s); "
                            "log may be world-readable",
                            self.path,
                            chmod_exc,
                        )
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
            # fsync the archive AFTER `gzip.open(...) as dst` exits, so the
            # gzip trailer (CRC32 + ISIZE) — written by `GzipFile.close()`
            # at `with` exit — is part of what gets pushed to disk. An
            # earlier version fsynced `dst.fileno()` from inside the `with`
            # block, which flushed the body but raced the trailer; a crash
            # at that point could leave a body-only archive that gzip.open
            # would reject on read with a CRC error. Re-open the file to
            # get a clean fd for the fsync. Best-effort; pseudo-filesystems
            # may not support fsync, and a failure here doesn't change the
            # durability of the source `.jsonl` — that one isn't unlinked
            # until below.
            try:
                with archive.open("rb") as fsynced:
                    fsync_file(fsynced.fileno())
            except OSError:
                pass
            self.path.unlink()
            # fsync the directory so the unlink + archive creation are
            # both durable. Without this, a crash here could leave us
            # with the original log still present AND the archive, or
            # with neither (depending on what was flushed first).
            fsync_dir(self.root)
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


_TRAILING_COUNTER_RE = re.compile(r"-(\d+)$")


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

    Session ids carry arbitrary internal dashes — Claude Code stamps
    full UUIDs (e.g. `0c69b1b2-cb4e-4cea-…`) — so a naive
    `inner.split("-")[-1]` trips on a UUID's trailing hex segment.
    We strip the timestamp prefix (no dashes by construction) and
    detect the optional `-N` counter via a regex anchored to end-of-
    string; the remaining body is treated as the session id wholesale.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    inner = path.name[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]
    ts_split = inner.split("-", 1)
    if len(ts_split) == 1:
        # Bare `.events-{ts}.jsonl.gz` — first-write-of-second.
        return (mtime, 0)
    remainder = ts_split[1]
    match = _TRAILING_COUNTER_RE.search(remainder)
    if match is not None:
        # `.events-{ts}-{session}-N.jsonl.gz` — third or later. The
        # regex is end-anchored, so it only matches a `-\d+` suffix
        # rather than any digit substring inside the session id.
        return (mtime, 1 + int(match.group(1)))
    # `.events-{ts}-{session}.jsonl.gz` — second-write-of-second.
    return (mtime, 1)


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
