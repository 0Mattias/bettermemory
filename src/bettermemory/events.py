"""Append-only JSONL event log for instrumentation.

The `Recorder` writes one JSON object per line to a per-shard active
file `<root>/.events.NN.jsonl` (NN = a stable hash of the session id,
mod `SHARD_COUNT`). Sharding the active log means writers from
different sessions append to different files instead of serialising on
one global append lock — the Phase-0 fleet benchmark measured that
lock at ~7-17% of throughput. Fixed striping (not one file per
session) bounds both the file count and a reader's open-fd count.
Readers merge the shards plus any pre-sharding legacy `.events.jsonl`
by event `ts`; a shard rotates to `.events-<timestamp>-s<NN>.jsonl.gz`
when it crosses `max_bytes`. Rotation is partitioned by the SAME key
the append lock is: the shard index is part of the archive stem, so
two shards crossing `max_bytes` in the same UTC second cannot derive
the same holding/archive name, and crash recovery only ever sweeps its
own shard's orphans (plus untagged pre-sharding ones, which have no
live producer). Uniform crc32 striping makes shards fill IN PHASE, so
same-second cross-shard rotation is the correlated case under the
swarm workload sharding was built for — not a rare interleaving. The
log lives next to the memories so it shares the same trust boundary —
no separate permissions story, no separate gitignore decisions.

Events are append-only by design. They're the substrate that downstream
tooling reads from:

- the `memory_health` view aggregates dead-weight and heavily-used memories,
- the `memory_record_use` signal feeds back into ranking,
- the durability marker list gets tuned against real write traffic.

Don't truncate or modify the file in place; treat it as an audit log. If you
need to reset the store, rotate or delete the whole file rather than editing.

Privacy note: search queries are NOT recorded verbatim by default (since
2.6.8). `query` / `probe_query` are redacted to `{hash, preview, len}` with a
defense-in-depth strip of known secret shapes (Anthropic / OpenAI / GitHub /
AWS tokens) before the line is written. The log is created 0o600 and lives in
the same per-user directory as the memories themselves, which already contain
user data — so it crosses no new trust boundary. Set `[telemetry]
log_queries_verbatim = true` in `config.toml` to restore the legacy verbatim
shape, or `[telemetry] enabled = false` to disable the log entirely.
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import json
import logging
import os
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ._fsutil import flock_excl, fsync_dir, fsync_file, replace_atomic
from .time_utils import parse_event_ts

log = logging.getLogger("bettermemory.events")


EVENT_LOG_FILENAME = ".events.jsonl"
ARCHIVE_PREFIX = ".events-"
ARCHIVE_SUFFIX = ".jsonl.gz"

# Active-log sharding (swarm-convergence). The active log is split into
# a fixed set of per-shard files at the store root:
# `.events.00.jsonl` … `.events.{SHARD_COUNT-1:02d}.jsonl`. A Recorder
# picks its shard by a stable crc32 of its session id, so different
# sessions append to different files and no longer contend on one
# global flock. Fixed striping (rather than one file per session)
# keeps the file count and a reader's simultaneously-open fds bounded
# no matter how many sessions a store accumulates. 16 gives a ~1/16
# residual collision probability between any two concurrent sessions —
# enough to erase the measured contention while staying a small,
# always-scannable set. Legacy stores keep their single `.events.jsonl`;
# it becomes one more read-only source the readers merge in.
SHARD_COUNT = 16
_SEGMENT_TEMPLATE = ".events.{:02d}.jsonl"
# Fields whose values are model/user-typed free text and may carry
# secrets. Redacted in `Recorder.record` when
# `telemetry.log_queries_verbatim = false` (the default since 2.6.8).
# Each value is replaced with `{"hash": "<sha256-prefix>", "preview":
# "<32 chars>", "len": N}` — cross-event correlation by hash works,
# the first 32 characters survive for triage, no raw body lands.
_REDACTED_TEXT_FIELDS = frozenset({"query", "probe_query"})
_QUERY_PREVIEW_CHARS = 32
_QUERY_HASH_PREFIX = 16

# Defense-in-depth pattern strip for known secret shapes. The 32-char
# preview alone can capture entire short tokens — a GitHub PAT
# (`ghp_<36chars>`) or an AWS access key (`AKIA<16chars>`) easily fits
# inside the preview window, and an OpenAI / Anthropic secret can have
# enough of its high-entropy tail land in the preview to be usable.
# The query log is local 0o600, so the primary defense is filesystem
# permissions; pattern-strip closes the gap when logs leave that
# perimeter (a `bettermemory eval` export, an attached transcript,
# a shared bug report). Patterns are applied BEFORE the 32-char
# truncation so the truncation never captures a partial secret.
#
# Order matters: the more-specific Anthropic key pattern runs before
# the generic `sk-` pattern so `sk-ant-…` is labelled correctly rather
# than caught as a generic OpenAI key.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED:openai-key]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-pat]"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[REDACTED:aws-access-key]"),
)


def _strip_known_secrets(text: str) -> str:
    """Replace known secret token shapes with redaction markers.

    Applied before the 32-char preview is taken so the preview never
    captures a partial token. The hash is also computed on the
    secret-stripped text so a repeated query with the same secret
    still correlates, but the secret bytes don't feed into the hash
    input either.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# Plain-JSONL holding name used during rotation. The active log is
# `rename`d to a sibling with this suffix before compression starts —
# the rename is atomic, so a crash never leaves the active log
# half-truncated. Recovery on the next rotation either completes the
# compression (no matching archive) or unlinks the holding file
# (archive already exists). Reader paths include orphan holding files
# only when no matching archive exists, so post-recovery a crashed
# rotation never double-counts events.
ROTATING_SUFFIX = ".jsonl.rotating"
ROTATING_GZ_TMP_SUFFIX = ".jsonl.gz.tmp"
DEFAULT_MAX_BYTES = 10_000_000  # 10 MB before rotation.

# Store-wide rotation-recovery lock. Held ONLY around the orphan sweep
# (`_recover_orphan_rotations`), never around name selection or the
# step-1 rename — those are already mutually exclusive by construction
# (names are shard-partitioned; same-shard rotations serialise on the
# shard's own append lock), and taking a global lock across them would
# re-introduce exactly the store-wide rotation serialisation that
# sharding removed. What the sweep genuinely needs it for: an UNTAGGED
# `.rotating` orphan (written by a pre-3.25 rotation) carries no shard
# and so is recoverable by any shard; without this lock two shards
# rotating at once could both gzip the same orphan into the same
# `.jsonl.gz.tmp` and interleave their output. The lockfile name is
# deliberately `.events-`-prefixed so `sync.py`'s `.events-*` ignore
# rule already covers it, and it ends in neither ARCHIVE_SUFFIX nor
# ROTATING_SUFFIX so no scanner mistakes it for a segment.
ROTATE_LOCK_STEM = ARCHIVE_PREFIX + "rotate"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Trailing `-N` collision counter on a rotated-segment stem.
_TRAILING_COUNTER_RE = re.compile(r"-(\d+)$")
# Shard tag on a rotated-segment stem: `s{NN}` immediately after the
# timestamp. Absent on pre-3.25 archives, which is why the parse
# returns `None` rather than a default shard — an untagged archive's
# shard is genuinely unknown, and pretending it is shard 0 would let
# `iter_events_window` prepend some other shard's history.
_SHARD_TAG_RE = re.compile(r"^s(\d{2})(?:-(.*))?$")


def _rotated_stem(name: str) -> str:
    """The `{ts}[-s{NN}][-{session}[-{counter}]]` body of a rotated
    segment's filename, with the `.events-` prefix and whichever of
    `.jsonl.gz` / `.jsonl.rotating` it carries stripped."""
    suffix = ROTATING_SUFFIX if name.endswith(ROTATING_SUFFIX) else ARCHIVE_SUFFIX
    return name[len(ARCHIVE_PREFIX) : -len(suffix)]


def _parse_rotated_name(name: str) -> tuple[str, int | None, int]:
    """`(ts, shard, in-second write order)` for a rotated segment name.

    Shapes produced since 3.25 (shard-partitioned rotation namespace):

        .events-{ts}-s{NN}.jsonl.gz                  -> (ts, NN, 0)
        .events-{ts}-s{NN}-{session}.jsonl.gz        -> (ts, NN, 1)
        .events-{ts}-s{NN}-{session}-{k}.jsonl.gz    -> (ts, NN, 1+k)

    Legacy shapes (pre-sharding, and the untagged 3.24.x archives) keep
    parsing with `shard = None`:

        .events-{ts}.jsonl.gz                        -> (ts, None, 0)
        .events-{ts}-{session}.jsonl.gz              -> (ts, None, 1)
        .events-{ts}-{session}-{k}.jsonl.gz          -> (ts, None, 1+k)

    Session ids carry arbitrary internal dashes — Claude Code stamps
    full UUIDs — so the counter is detected with an end-anchored regex
    rather than `split("-")[-1]`, and the shard tag is matched as an
    exact `s\\d\\d` token so a session id that merely starts with an
    `s` is not mistaken for one.
    """
    ts, _, remainder = _rotated_stem(name).partition("-")
    shard: int | None = None
    if remainder:
        tag = _SHARD_TAG_RE.match(remainder)
        if tag is not None:
            shard = int(tag.group(1))
            remainder = tag.group(2) or ""
    if not remainder:
        return (ts, shard, 0)
    counter = _TRAILING_COUNTER_RE.search(remainder)
    if counter is not None:
        return (ts, shard, 1 + int(counter.group(1)))
    return (ts, shard, 1)


def _rotated_segment_shard(path: Path) -> int | None:
    """Shard a rotated segment came from, or None when it predates the
    shard-tagged naming (pre-3.25 archives / holding files)."""
    return _parse_rotated_name(path.name)[1]


def redact_query(text: str) -> dict[str, Any]:
    """Replace a free-text field with a structured redaction.

    Shape: ``{"hash": "<16-hex-prefix>", "preview": "<first 32 chars>",
    "len": <total length>}``. The hash lets a consumer correlate
    repeated queries without seeing them; the preview is enough to
    triage what kind of query it was (e.g. "kubernetes networking"
    survives, "my-api-key=sk-…" survives only its first 32 chars
    rather than the full secret). The full text is not recoverable
    from the event log.

    Known token shapes (Anthropic / OpenAI / GitHub / AWS) are
    stripped to opaque markers BEFORE the 32-char preview is taken,
    so the preview never carries a partial high-entropy secret. The
    pre-strip text length is retained as ``len`` so downstream
    triage can still see "this query was 87 chars" without seeing
    what those chars were.
    """
    original_len = len(text)
    stripped = _strip_known_secrets(text)
    digest = hashlib.sha256(stripped.encode("utf-8", errors="replace")).hexdigest()
    return {
        "hash": digest[:_QUERY_HASH_PREFIX],
        "preview": stripped[:_QUERY_PREVIEW_CHARS],
        "len": original_len,
    }


def _redact_event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `fields` with redacted-text fields replaced.

    Non-string values pass through untouched — the field set isn't
    guaranteed across producers and we don't want to silently drop a
    legitimate non-string payload.
    """
    out = dict(fields)
    for key in _REDACTED_TEXT_FIELDS:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = redact_query(value)
    return out


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
    # Process-level worktree root (`git rev-parse --show-toplevel` at
    # construction), stamped on every event when set. A process's
    # working directory is stable for its lifetime, so capturing once
    # at construction is both correct and cheap — no git subprocess per
    # event. Lets cross-session consumers (`episode_handoff`) worktree-
    # match a prior session even when it wrote NO episodes, only events
    # (queue #28). None when the process isn't inside a git checkout or
    # the construction site didn't supply it (web UI, legacy callers);
    # downstream treats an absent field as "unknown worktree" and stays
    # conservative.
    worktree_root: str | None = None
    # When False (default since 2.6.8), fields in `_REDACTED_TEXT_FIELDS`
    # are replaced with `{"hash", "preview", "len"}` before the event is
    # serialised. Set True to keep the legacy verbatim shape — useful for
    # debugging your own ranker, less so for shared boxes. Wired from
    # `TelemetryConfig.log_queries_verbatim` at server construction.
    log_queries_verbatim: bool = False
    # Shard index this recorder appends to, derived from `session_id` in
    # `__post_init__`. Not a constructor argument.
    _shard: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        # crc32 is deterministic across processes and interpreter runs
        # (unlike the salted builtin hash()), so a session always maps
        # to the same shard file and its events stay ts-ordered within
        # it — the property `iter_events`' heapq.merge relies on.
        self._shard = zlib.crc32(self.session_id.encode("utf-8")) % SHARD_COUNT
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """This recorder's active shard file. Per-session by construction,
        so concurrent writers from different sessions don't serialise on
        one append lock. Readers merge every shard plus any legacy
        `.events.jsonl`."""
        return self.root / _SEGMENT_TEMPLATE.format(self._shard)

    def record(self, kind: str, **fields: Any) -> None:
        """Append one event of the given `kind`. Extra `fields` are merged
        into the event dict. Best-effort — failures are logged, not raised.
        """
        if not self.enabled:
            return
        try:
            if not self.log_queries_verbatim:
                fields = _redact_event_fields(fields)
            event = {
                "ts": _utcnow_iso(),
                "session": self.session_id,
                "kind": kind,
                **fields,
            }
            # Stamp the process worktree (queue #28) so a prior session
            # that wrote only events (e.g. a search-only loop tick that
            # crashed before episode_write) is still worktree-matchable
            # by episode_handoff. Only when known; a handler field of the
            # same name (none exist today) is left untouched.
            if self.worktree_root is not None:
                event.setdefault("worktree_root", self.worktree_root)
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
                    # Durability gate: the file's bytes were fsync'd above,
                    # but the parent directory's *entry* for the newly-
                    # created file lives in the directory's own page-cache
                    # until a dir-fsync hits disk. POSIX does not guarantee
                    # a fresh dirent survives power loss without an
                    # explicit `fsync` on the directory fd, so on a brand-
                    # new event log a crash between the first append and
                    # the next natural dir-fsync (a rotation, several
                    # writes later) could leave the file's data committed
                    # but the directory listing it from the kernel's
                    # perspective stale — readers see an empty/absent log.
                    # Only on first-write: subsequent appends modify an
                    # existing dirent and don't need re-syncing. Mirrors
                    # the `fsync_dir` ceremony in `episodes._write_post`
                    # (tick-3 fix 7017b2c) and `store._atomic_write_post`.
                    # `fsync_dir` no-ops on Windows; see `_fsutil.fsync_dir`.
                    fsync_dir(self.root)
        except Exception as exc:  # noqa: BLE001 — never break the caller.
            log.warning("event log write failed (kind=%s): %s", kind, exc)

    # ---- internals --------------------------------------------------------

    def _rotate_if_needed(self) -> None:
        """Gzip-rotate the active log if it has crossed `max_bytes`.

        Crash-safe sequence:

        1. Atomically rename `.events.jsonl` -> `.events-{ts}.jsonl.rotating`.
           After this point the active log is empty (the next append
           creates a fresh file). The data lives entirely in the
           `.rotating` holding file; readers know to include it when no
           matching `.gz` exists yet.
        2. fsync the directory so the rename is durable.
        3. Gzip the `.rotating` file into a `.jsonl.gz.tmp` sibling and
           fsync. A crash here leaves both files; recovery on the next
           rotation either completes step 4 (if the `.tmp` is intact) or
           re-runs from step 3.
        4. Atomically rename `.jsonl.gz.tmp` -> `.jsonl.gz` and fsync
           the directory. The archive is now canonical.
        5. Unlink the `.rotating` holding file and fsync the directory.

        Before any of this, sweep for orphan `.rotating` files from prior
        crashed rotations and either complete them (no matching `.gz`)
        or unlink them (matching `.gz` exists — the gz is canonical).

        Every name derived here carries this recorder's SHARD index, so
        the rotation namespace is partitioned by the same key the append
        lock is. That is what makes step 1 safe: two shards crossing
        `max_bytes` in the same UTC second derive different `.rotating`
        paths and cannot `replace_atomic` over each other.
        """
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        except (
            OSError
        ) as exc:  # pragma: no cover — disk issues shouldn't kill the recorder.
            log.warning("event log stat failed: %s", exc)
            return

        # A non-positive `max_bytes` means "never rotate": without this guard
        # `size < max_bytes` is always false for max_bytes <= 0, so every
        # append would gzip-rotate the active log (a rotation storm). The
        # loader clamps a 0/negative *configured* value to the default, but an
        # explicitly-constructed Recorder can still pass <= 0, so guard here.
        if self.max_bytes <= 0 or size < self.max_bytes:
            return

        # Recovery runs ONLY now, when a rotation is actually due — not at the
        # top of every append. `_recover_orphan_rotations()` does a full
        # `iterdir()` of the recorder root, which is the SHARED store
        # directory (memory `.md` files + episodes + archives), so running it
        # per-append turned every event write into an O(directory-size) scan
        # on a large store. Orphan `.rotating` files only arise from a crashed
        # rotation, are still read correctly until reclaimed, and the only
        # producer of a new orphan is a rotation — so sweeping here, right
        # before we start one, recovers any prior crash without taxing the
        # common no-rotation append path.
        #
        # Held under the store-wide rotate lock: an untagged (pre-3.25)
        # orphan belongs to no shard, so two shards rotating at once
        # could otherwise both try to compress it into the same
        # `.jsonl.gz.tmp`. Name selection and the step-1 rename below
        # deliberately stay OUTSIDE that lock — they are already
        # mutually exclusive (shard-partitioned names; same-shard
        # rotations serialise on the shard append lock we're inside) and
        # globalising them would undo the point of sharding.
        try:
            with _locked(self.root / ROTATE_LOCK_STEM):
                self._recover_orphan_rotations()
        except OSError as exc:  # pragma: no cover — lockfile unavailable.
            log.warning("event log rotation recovery lock failed: %s", exc)

        archive, rotating = self._next_rotation_paths()
        try:
            # Step 1: atomic rename. After this the active log is gone.
            # `replace_atomic` so a Windows reader holding the active
            # segment open cannot turn rotation into a hard failure —
            # sharding multiplied the number of concurrent segment
            # readers, which widened exactly this race.
            replace_atomic(self.path, rotating)
            fsync_dir(self.root)
        except OSError as exc:
            log.warning("event log rotation rename failed: %s", exc)
            return

        try:
            self._compress_rotating(rotating, archive)
        except OSError as exc:
            # Compression failed mid-flight. The `.rotating` file still
            # holds all the data; the next rotation will pick it up via
            # the recovery sweep. Don't unlink it — that would lose data.
            log.warning("event log rotation compress failed: %s", exc)

    def _next_rotation_paths(self) -> tuple[Path, Path]:
        """Pick an unused `(archive, rotating)` pair for this rotation.

        The stem is `{ARCHIVE_PREFIX}{ts}-s{shard:02d}`. The shard tag is
        load-bearing, not cosmetic: pre-3.25 the stem was
        `{ARCHIVE_PREFIX}{ts}` with no shard component, so two shards
        crossing `max_bytes` in the same UTC second derived the IDENTICAL
        `.rotating` path and the second `replace_atomic` silently
        unlinked the first shard's entire renamed segment (up to
        `max_bytes` of events, surfacing only as a WARNING from the
        follow-on compress). Tagging by shard makes the collision
        structurally impossible across shards; within one shard,
        rotations are serialised by that shard's append lock, which the
        caller already holds.

        Within a shard, several rotations can still land in the same UTC
        second (tests with a tiny `max_bytes` hit it immediately), so we
        fall back to a session-tagged stem and then a numeric counter.
        BOTH the archive and the `.rotating` holding path are probed —
        the old code checked only the archive, which was safe merely
        because the (now removed) global append lock made rotations
        mutually exclusive; the derived holding path was never
        existence-checked at all.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{ARCHIVE_PREFIX}{ts}-s{self._shard:02d}"

        def _paths(stem: str) -> tuple[Path, Path]:
            return (
                self.root / f"{stem}{ARCHIVE_SUFFIX}",
                self.root / f"{stem}{ROTATING_SUFFIX}",
            )

        def _taken(stem: str) -> bool:
            archive, rotating = _paths(stem)
            return archive.exists() or rotating.exists()

        if not _taken(base):
            return _paths(base)
        tagged = f"{base}-{self.session_id}"
        if not _taken(tagged):
            return _paths(tagged)
        # Bounded by the number of bytes we've actually written, so the
        # loop terminates.
        counter = 1
        while _taken(f"{tagged}-{counter}"):
            counter += 1
        return _paths(f"{tagged}-{counter}")

    def _compress_rotating(self, rotating: Path, archive: Path) -> None:
        """Steps 3-5: compress a `.rotating` holding file into its archive.

        Idempotent against partial completion — if `archive` already
        exists we skip recompression. Used both for the inline path
        (called from `_rotate_if_needed` immediately after the rename)
        and for recovery (called from `_recover_orphan_rotations` after
        a crash).
        """
        if archive.exists():
            # Compression already completed before a prior crash. The
            # archive is canonical; the .rotating file is a duplicate
            # that we can safely unlink.
            try:
                rotating.unlink()
                fsync_dir(self.root)
            except OSError as exc:  # pragma: no cover
                log.warning("orphan .rotating unlink failed: %s", exc)
            return

        tmp = archive.with_name(
            archive.name[: -len(ARCHIVE_SUFFIX)] + ROTATING_GZ_TMP_SUFFIX
        )
        # A leftover .tmp from a prior crashed compression is junk —
        # we have no way to know it's complete, so retry from scratch.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass

        with rotating.open("rb") as src, gzip.open(tmp, "wb") as dst:
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        # fsync the temp AFTER `gzip.open(...) as dst` exits so the gzip
        # trailer (CRC32 + ISIZE, written by `GzipFile.close()` at `with`
        # exit) is part of what gets pushed to disk. Best-effort — pseudo
        # filesystems may not support fsync, and the canonical-archive
        # rename below is the durability boundary.
        try:
            with tmp.open("rb") as fsynced:
                fsync_file(fsynced.fileno())
        except OSError:
            pass
        # Match the 0o600 the active log gets on first write — the
        # archive carries the same session ids and (when verbatim is
        # enabled) the same raw query text, so it deserves the same
        # permissions. No-op on Windows. Done before the canonical
        # rename so the file is never visible at the canonical name
        # with broader-than-target permissions.
        try:
            os.chmod(tmp, 0o600)
        except OSError as chmod_exc:  # pragma: no cover
            log.warning(
                "rotation archive %s: chmod 0o600 failed (%s); "
                "archive may be world-readable",
                tmp,
                chmod_exc,
            )
        # Step 4: atomic rename to canonical archive name.
        replace_atomic(tmp, archive)
        fsync_dir(self.root)
        # Step 5: now that the archive is canonical, unlink the holding
        # file. A crash between this and the next fsync_dir leaves a
        # duplicate-data .rotating file; the next rotation's recovery
        # sweep notices the matching archive and unlinks it.
        try:
            rotating.unlink()
            fsync_dir(self.root)
        except OSError as exc:  # pragma: no cover
            log.warning(".rotating unlink failed: %s", exc)

    def _recover_orphan_rotations(self) -> None:
        """Bring any prior crashed rotation OF THIS SHARD to a clean state.

        Called at the top of `_rotate_if_needed` under the store-wide
        rotate lock. A `.rotating` file represents a rotation that
        started but didn't finish. If a matching `.gz` exists, the
        compression completed before the crash — unlink the
        `.rotating`. Otherwise the data only lives in the `.rotating`
        file — re-run compression to produce the archive.

        Scoping is the correctness bit. Pre-3.25 this swept EVERY
        `.rotating` file in the store root, and once the active log was
        sharded it could no longer tell a crash orphan from another
        shard's LIVE in-flight rotation — the docstring premise ("each
        `.rotating` file represents a crashed rotation") was simply
        false. We now skip any orphan tagged with a different shard. A
        shard's own live rotation is never visible here because the
        caller holds that shard's append lock for the whole rotation.

        Untagged orphans (`.events-{ts}.jsonl.rotating`, written by a
        pre-3.25 rotation) belong to no shard, so any shard may reclaim
        them — no live producer creates that shape anymore. Concurrent
        reclaim of the same untagged orphan by two shards is what the
        store-wide rotate lock the caller holds exists to prevent.
        """
        try:
            entries = list(self.root.iterdir())
        except OSError:  # pragma: no cover
            return
        for path in entries:
            if not (
                path.is_file()
                and path.name.startswith(ARCHIVE_PREFIX)
                and path.name.endswith(ROTATING_SUFFIX)
            ):
                continue
            shard = _rotated_segment_shard(path)
            if shard is not None and shard != self._shard:
                # Another shard's rotation — either live in-flight (its
                # own append lock is held) or its own crash to recover.
                # Not ours to touch.
                continue
            archive_name = path.name[: -len(ROTATING_SUFFIX)] + ARCHIVE_SUFFIX
            archive = path.with_name(archive_name)
            try:
                self._compress_rotating(path, archive)
            except OSError as exc:  # pragma: no cover
                log.warning("orphan .rotating recovery failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Read side — used by tests, future memory_health, debugging
# ---------------------------------------------------------------------------


def _event_id_items(value: Any) -> list[tuple[int, str]]:
    """Normalize an event's id-list field (`returned` / `memory_ids` /
    `hit_ids` / `ids`) to `(original_index, id)` pairs before iteration.

    The event log is plaintext, git-synced, and hand-editable, so every
    consumer must survive a malformed field: a numeric scalar raises
    `TypeError` under `for mid in <scalar>`, a bare string iterates by
    CHARACTER (mis-attributing per-char counts), and a well-formed list
    whose ELEMENTS are lists/dicts (`ids=[[id]]`) passes a container
    check but blows up at the first hash/lookup of the element. One bad
    event in the active log would otherwise take down every consumer of
    the walk — memory_health blanked this way in 3.14.x, and
    memory_search / memory_audit_turn did in 3.15.0 via their own raw
    reads. This is the single choke point: consumers never iterate the
    raw field.

    The ORIGINAL index is preserved so parallel arrays recorded alongside
    the ids (`claim_excerpts` on `use` events) still attribute to the
    right slot after malformed elements are dropped — compacting the list
    would silently shift every claim after a dropped element onto the
    wrong memory. A lone non-empty string is treated as a single id at
    slot 0; every other non-list shape coerces to empty.
    """
    if isinstance(value, list):
        return [(i, v) for i, v in enumerate(value) if isinstance(v, str)]
    if isinstance(value, str) and value:
        return [(0, value)]
    return []


def _event_id_list(value: Any) -> list[str]:
    """`_event_id_items` without the indices — for consumers that only
    tally per-id and carry no parallel arrays. Same normalization, same
    guarantees; see `_event_id_items` for the rationale."""
    return [v for _, v in _event_id_items(value)]


def _iter_json_lines(f: Any) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects from a BINARY line stream, degrading
    per-record instead of aborting the whole stream on corruption.

    Three real corruption modes the previous text-mode `for line in f` +
    `except json.JSONDecodeError` did NOT survive — they all crashed the
    reader, taking down memory_health / scope_overview / eval / doctor,
    which read through here:

    - an invalid-UTF-8 byte: in text mode the decode happens in the line
      iterator, BEFORE json.loads, so JSONDecodeError never fired —
      UnicodeDecodeError (a ValueError, not OSError) escaped. We read bytes
      and decode each line with `errors="replace"`, so a bad byte becomes
      U+FFFD and the line simply fails json.loads and is skipped.
    - a truncated gzip archive: `readline` raises EOFError (not an OSError).
    - a CRC-corrupt gzip archive: `readline` raises zlib.error (not OSError).

    A fourth mode crashes not the reader but its consumers: a line that
    parses as VALID JSON yet isn't an object (`[1, 2, 3]`, `"a string"`,
    `42`, `null` — a hand-edit or partial overwrite of this plain-text,
    git-syncable log). `json.loads` succeeds, so the JSONDecodeError
    guard never fires, and the non-dict used to flow straight through
    the declared Iterator[dict] contract: the eval rollups' isinstance
    guards tolerated it, but `compute_health`'s first `ev.get(...)`
    raised AttributeError, taking memory_health / scope_overview /
    report_for_directory down with it. Such lines are now skipped here
    — at the single parse site every reader shares — exactly like any
    other corrupt line.

    Reading line-by-line under a try lets a truncated/corrupt archive still
    yield its readable prefix rather than contributing nothing.
    """
    while True:
        try:
            raw = f.readline()
        except (OSError, EOFError, zlib.error):
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Fourth corruption mode (docstring): valid JSON, wrong shape.
        # The parse succeeded but the value isn't an object — yielding
        # it would violate the Iterator[dict] contract every consumer
        # types against. Skip it like any other corrupt line.
        if not isinstance(parsed, dict):
            continue
        yield parsed


_TS_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _event_ts_key(event: dict[str, Any]) -> datetime:
    """Merge key for the active shards: the event's parsed `ts`, or a
    UTC-min sentinel for a missing/unparseable one so a corrupt
    timestamp sorts deterministically first rather than raising inside
    the merge."""
    ts = parse_event_ts(event.get("ts"))
    return ts if ts is not None else _TS_MIN


def _active_segments(root: Path) -> list[tuple[Path, int | None]]:
    """Every active event segment on disk, paired with its shard index:
    each per-shard `.events.NN.jsonl` that exists, plus a legacy
    pre-sharding `.events.jsonl` (shard `None`) if present. Post-upgrade
    writes only ever go to shards, so the legacy file is read-only from
    here on — it merges in as one more ts-ordered source, no migration
    required.

    The shard index is what lets `iter_events_window` decide window
    coverage PER SEGMENT and pair a segment with rotations from its own
    shard."""
    segments: list[tuple[Path, int | None]] = []
    legacy = root / EVENT_LOG_FILENAME
    if legacy.exists():
        segments.append((legacy, None))
    for shard in range(SHARD_COUNT):
        seg = root / _SEGMENT_TEMPLATE.format(shard)
        if seg.exists():
            segments.append((seg, shard))
    return segments


def _active_segment_paths(root: Path) -> list[Path]:
    """`_active_segments` without the shard indices."""
    return [path for path, _ in _active_segments(root)]


def iter_events(root: Path) -> Iterator[dict[str, Any]]:
    """Yield events from the *active* segments — the per-shard
    `.events.NN.jsonl` files plus any legacy `.events.jsonl` — merged
    into chronological order by event `ts`.

    Each segment is appended by a single stream of writers under that
    shard's lock, so it is already ts-ordered; `heapq.merge` across the
    segments yields the global chronological order without buffering,
    with open fds bounded by the shard count. Skips malformed lines
    defensively — single-process-per-shard writers make corruption
    unlikely, but the read side stays robust against external editing
    (a stray non-UTF-8 byte or hand-edit must not crash the reader).
    Does not read rotated archives — call `iter_all_events` for that.
    """
    paths = _active_segment_paths(root)
    if not paths:
        return
    handles: list[Any] = []
    try:
        streams: list[Iterator[dict[str, Any]]] = []
        for path in paths:
            try:
                f = path.open("rb")
            except OSError:
                continue
            handles.append(f)
            streams.append(_iter_json_lines(f))
        yield from heapq.merge(*streams, key=_event_ts_key)
    finally:
        for handle in handles:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass


def _archive_sort_key(path: Path) -> tuple[int, int]:
    """Sort key for rotated-segment ordering.

    Primary: mtime_ns. Secondary: write-order index parsed from the
    filename (`_parse_rotated_name`). The secondary tiebreak only
    matters when the filesystem timestamp resolution is too coarse to
    separate rapid rotations within a single UTC second — Windows in
    particular records mtime at ~10ms granularity, so a test that calls
    `record()` 15 times in a row with `max_bytes=120` can produce
    multiple archives sharing one `mtime_ns`.

    Tolerates `.rotating` holding files alongside `.gz` archives: both
    share the same stem structure, differing only in suffix.
    `iter_events_window` ranks an orphan holding file (a rotation that
    crashed before compression) against the archives to find the newest
    rotated segment, so the key must accept whichever suffix the
    candidate carries.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (mtime, _parse_rotated_name(path.name)[2])


def _rotated_segments(root: Path) -> list[Path]:
    """Every rotated segment worth reading, unordered.

    The `.jsonl.gz` archives plus orphan `.rotating` holding files (a
    rotation that crashed after the active-log rename but before
    compression — the events' only copy). A `.rotating` file WITH a
    matching archive is a stale duplicate that the next rotation will
    unlink; including it would double-count those events.
    """
    try:
        entries = list(root.iterdir())
    except OSError:  # pragma: no cover — unreadable root, nothing to read.
        return []
    archives = [
        p
        for p in entries
        if p.is_file()
        and p.name.startswith(ARCHIVE_PREFIX)
        and p.name.endswith(ARCHIVE_SUFFIX)
    ]
    archive_stems = {p.name[: -len(ARCHIVE_SUFFIX)] for p in archives}
    return archives + [
        p
        for p in entries
        if p.is_file()
        and p.name.startswith(ARCHIVE_PREFIX)
        and p.name.endswith(ROTATING_SUFFIX)
        and p.name[: -len(ROTATING_SUFFIX)] not in archive_stems
    ]


def _iter_segment_chain(paths: list[Path]) -> Iterator[dict[str, Any]]:
    """Yield events from an ordered run of rotated segments, opening one
    at a time so the merge's open-fd count stays bounded by the number
    of chains rather than the number of archives in the store."""
    for path in paths:
        yield from _iter_segment(path)


def iter_all_events(root: Path) -> Iterator[dict[str, Any]]:
    """Yield events from rotated archives + active log, in chronological order.

    Chronological is a real guarantee, produced by a `heapq.merge` on
    `_event_ts_key` — the same merge `iter_events` performs across the
    active shards. It has to be: since 3.24.0 sharded the active log,
    "all archives, then all active segments" is NOT chronological. A
    quiet shard's active segment routinely holds events far older than
    a busy shard's freshly-cut archive, and the reverse-walking
    consumers (`compute_health`'s `last_*` timestamps, consolidate's
    fact demotion, eval, doctor) all read order as meaning.

    Streams merged, in tie-break priority order: one chain per shard
    over that shard's rotated archives (chronological within a shard by
    construction — a shard rotates its own segment wholesale), one
    chain for untagged pre-3.25 archives, one stream per orphan
    `.rotating` holding file, and finally `iter_events(root)` for the
    active segments. Equal timestamps resolve toward the earlier
    stream, so a rotated segment still precedes the active tail it was
    cut from.

    The untagged-archive chain is the one approximate stream: those
    names predate the shard tag, so archives cut by DIFFERENT shards
    land in one chain ordered by mtime. Ordering within that chain is
    therefore best-effort rather than exact — no events are lost, and
    it degrades to exact as pre-3.25 archives age out of a store.

    Orphan `.rotating` holding files (produced when a rotation crashed
    after the active-log rename but before compression finished) are
    yielded *only* when no matching archive exists for the same stem.
    When a matching archive exists, the archive is canonical and the
    `.rotating` file is a stale duplicate that the next rotation will
    unlink — including it would double-count those events.
    """
    if not root.exists():
        return

    by_shard: dict[int, list[Path]] = {}
    untagged: list[Path] = []
    orphans: list[Path] = []
    for path in _rotated_segments(root):
        if path.name.endswith(ROTATING_SUFFIX):
            orphans.append(path)
            continue
        shard = _rotated_segment_shard(path)
        if shard is None:
            untagged.append(path)
        else:
            by_shard.setdefault(shard, []).append(path)

    streams: list[Iterator[dict[str, Any]]] = []
    for shard in sorted(by_shard):
        streams.append(
            _iter_segment_chain(sorted(by_shard[shard], key=_archive_sort_key))
        )
    if untagged:
        streams.append(_iter_segment_chain(sorted(untagged, key=_archive_sort_key)))
    for orphan in sorted(orphans, key=_archive_sort_key):
        streams.append(_iter_segment(orphan))
    streams.append(iter_events(root))
    yield from heapq.merge(*streams, key=_event_ts_key)


def _newest_rotated_segment(root: Path) -> Path | None:
    """Most recent rotated segment across every shard, or None when
    nothing has rotated. Ranking uses `_archive_sort_key` — (mtime,
    in-second write counter)."""
    candidates = _rotated_segments(root)
    if not candidates:
        return None
    return max(candidates, key=_archive_sort_key)


def _newest_rotated_segment_for_shard(
    candidates: list[Path], shard: int | None
) -> Path | None:
    """Newest rotated segment attributable to `shard`, or None.

    `shard=None` selects the untagged pre-3.25 segments — that is the
    right answer for a legacy `.events.jsonl` active log, whose
    rotations were untagged too. A sharded segment deliberately does
    NOT fall back to untagged candidates: an untagged archive's shard is
    unknown, so prepending it could splice a different shard's history
    into the window.
    """
    matching = [p for p in candidates if _rotated_segment_shard(p) == shard]
    if not matching:
        return None
    return max(matching, key=_archive_sort_key)


def _iter_segment(path: Path) -> Iterator[dict[str, Any]]:
    """Yield events from one rotated segment (`.jsonl.gz` or `.rotating`).

    Same per-source degradation contract as `iter_all_events`: a
    truncated / CRC-corrupt / unreadable segment contributes nothing
    rather than crashing the reader.
    """
    if path.name.endswith(ARCHIVE_SUFFIX):
        try:
            with gzip.open(path, "rb") as gz:
                yield from _iter_json_lines(gz)
        except (OSError, EOFError, zlib.error):
            log.warning("events: skipping unreadable archive %s", path.name)
        return
    try:
        rf = path.open("rb")
    except OSError:  # pragma: no cover — segment vanished mid-read.
        return
    with rf:
        yield from _iter_json_lines(rf)


def _first_event_ts(path: Path) -> datetime | None:
    """Parsed `ts` of the OLDEST event in one active segment, or None
    when the segment is missing, empty, or holds nothing with a
    readable timestamp.

    Reads only as far as the first parseable record — a segment is
    append-only and ts-ordered, so its head IS its oldest event. This
    is the per-segment coverage probe `iter_events_window` replaced its
    materialise-the-whole-active-log scan with.
    """
    try:
        f = path.open("rb")
    except OSError:  # pragma: no cover — segment vanished mid-read.
        return None
    with f:
        for event in _iter_json_lines(f):
            ts = parse_event_ts(event.get("ts"))
            if ts is not None:
                return ts
    return None


def iter_events_window(
    root: Path,
    window_seconds: int,
    *,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield recent events: the active log, rotation-proofed for a window.

    Rotation (`_rotate_if_needed`) archives the ENTIRE active log the
    moment it crosses `max_bytes` — a boundary independent of turn
    boundaries — so a consumer that reads `iter_events` for a "last N
    seconds" window silently loses every event that rotated out
    mid-window (the silent-miss probe's retrieval shield was the
    motivating victim: a turn straddling a rotation lost its own
    `search` event and re-fired as a false miss).

    This reader closes that gap without paying `iter_all_events`'s
    full-history cost: when the active log's oldest event is younger
    than ``now - window_seconds`` (or the log is empty/missing), the
    newest rotated segment — latest archive by `_archive_sort_key`, or
    an orphan `.rotating` holding file with no matching archive — is
    prepended. When the active log already covers the whole window, no
    archive is touched, so the common no-recent-rotation path costs
    exactly one extra timestamp parse over `iter_events`.

    Coverage is decided PER ACTIVE SEGMENT, not globally. Since 3.24.0
    sharded the active log there is no single "oldest active event" to
    test: taking the globally-oldest one (across all 16 shards) made
    this shield DEAD on any store older than a session, because one
    cold shard holding a single stale event pins that timestamp below
    `cutoff` and the prepend never fires — even when a *different*
    shard just rotated the window's events out. Shard files are never
    deleted, so after a handful of sessions nearly every shard holds
    ancient events. The concrete victim was the retrieval shield in
    `handlers/audit_turn` / `hook`: a session's own `search` event went
    invisible, the turn re-fired as a false `search_miss`, and the
    published silent_miss_rate inflated.

    So: for each active segment, compare THAT segment's own oldest `ts`
    against `cutoff`, and when it does not cover the window prepend the
    newest rotated segment attributable to the same shard. At most one
    rotated segment per shard — deeper history per shard would need
    `window_seconds`' worth of events past `max_bytes` twice over in one
    shard, a rotation cadence pathological enough that the right fix is
    the rotation config, not a deeper read. Note that N shards CAN each
    rotate inside one window, so up to N segments may be prepended; the
    old "at most two files" bound was a pre-sharding statement.

    Everything is merged on event `ts` (`heapq.merge`, as
    `iter_all_events` does), so the yield is chronological rather than
    merely oldest-segment-first. The no-recent-rotation path costs one
    extra head-read per active segment over `iter_events` and opens no
    archive.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=window_seconds)
    segments = _active_segments(root)
    candidates = _rotated_segments(root)

    prepend: list[Path] = []
    seen: set[Path] = set()

    def _consider(segment: Path | None) -> None:
        if segment is not None and segment not in seen:
            seen.add(segment)
            prepend.append(segment)

    if not segments:
        # Nothing active at all (a just-rotated store, or telemetry that
        # has never written here). No shard to attribute, so fall back
        # to the newest rotated segment store-wide.
        _consider(_newest_rotated_segment(root))
    else:
        for path, shard in segments:
            oldest_ts = _first_event_ts(path)
            if oldest_ts is None or oldest_ts > cutoff:
                _consider(_newest_rotated_segment_for_shard(candidates, shard))

    streams: list[Iterator[dict[str, Any]]] = [
        _iter_segment(path) for path in sorted(prepend, key=_archive_sort_key)
    ]
    streams.append(iter_events(root))
    yield from heapq.merge(*streams, key=_event_ts_key)


__all__ = [
    "Recorder",
    "iter_events",
    "iter_all_events",
    "iter_events_window",
    "EVENT_LOG_FILENAME",
    "DEFAULT_MAX_BYTES",
]
