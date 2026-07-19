"""Unit tests for events.py — the append-only JSONL event recorder."""

from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from bettermemory.events import (
    EVENT_LOG_FILENAME,
    Recorder,
    _REDACTED_TEXT_FIELDS,
    _SECRET_PATTERNS,
    iter_all_events,
    iter_events,
    iter_events_window,
)


# ---------------------------------------------------------------------------
# Basic record / read round-trips
# ---------------------------------------------------------------------------


def test_record_appends_one_jsonl_line(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])

    # The active log is sharded; this session's events land in its own
    # shard file (`rec.path`), not the legacy `.events.jsonl`.
    log_path = rec.path
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["kind"] == "write"
    assert event["session"] == "sess_test"
    assert event["id"] == "01HXYZ"
    assert event["scopes"] == ["tools"]
    # Timestamp is ISO-8601 with `Z` for UTC.
    assert event["ts"].endswith("Z")


def test_record_multiple_events_one_line_each(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])
    rec.record("show", id="01HXYZ")
    rec.record("search", query="foo", returned=["01HXYZ"])

    events = list(iter_events(tmp_path))
    assert len(events) == 3
    assert [e["kind"] for e in events] == ["write", "show", "search"]


def test_iter_events_skips_malformed_lines(tmp_path: Path) -> None:
    """An external editor or a partial write shouldn't crash the reader."""
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])
    # Append a malformed line by hand.
    log_path = tmp_path / EVENT_LOG_FILENAME
    with log_path.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
    rec.record("show", id="01HXYZ")

    events = list(iter_events(tmp_path))
    assert len(events) == 2
    assert [e["kind"] for e in events] == ["write", "show"]


def test_iter_events_skips_invalid_utf8_in_active_log(tmp_path: Path) -> None:
    """A stray non-UTF-8 byte (external edit, partial write) must not crash
    the reader. Regression: the previous text-mode `for line in f` raised
    UnicodeDecodeError from the line iterator — BEFORE json.loads — so the
    JSONDecodeError guard never fired, and the exception (a ValueError, not
    OSError) propagated up and took down memory_health / scope_overview /
    eval / doctor, which all read through here.
    """
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])
    with (tmp_path / EVENT_LOG_FILENAME).open("ab") as f:
        f.write(b"\xff\xfe not valid utf-8 or json\n")
    rec.record("show", id="01HXYZ")

    events = list(iter_events(tmp_path))  # must not raise
    assert [e["kind"] for e in events] == ["write", "show"]


# ---------------------------------------------------------------------------
# Active-log sharding (swarm-convergence): the global append lock is
# gone — writers stripe across per-shard files — and readers merge them.
# ---------------------------------------------------------------------------


def test_same_session_maps_to_a_stable_shard(tmp_path: Path) -> None:
    """A session id resolves to one shard file deterministically, so
    its events stay in a single ts-ordered stream (what the merge
    relies on) and two recorders for the same session share a file."""
    r1 = Recorder(root=tmp_path, session_id="stable-session")
    r2 = Recorder(root=tmp_path, session_id="stable-session")
    assert r1.path == r2.path
    assert r1.path != (tmp_path / EVENT_LOG_FILENAME)  # not the legacy name


def test_sessions_stripe_across_multiple_shard_files(tmp_path: Path) -> None:
    """Many sessions distribute across shard files rather than
    serialising on one — the whole point of the shard — and every
    event is still readable through the merged reader."""
    for i in range(60):
        Recorder(root=tmp_path, session_id=f"session-{i}").record(
            "write", id=f"m{i:03d}"
        )

    shard_files = list(tmp_path.glob(".events.*.jsonl"))
    assert len(shard_files) > 1, "sessions must stripe across shards, not one file"
    # No global legacy file was created by the sharded writers.
    assert not (tmp_path / EVENT_LOG_FILENAME).exists()

    events = list(iter_events(tmp_path))
    assert len(events) == 60
    assert {e["id"] for e in events} == {f"m{i:03d}" for i in range(60)}


def test_iter_events_merges_shards_preserving_per_session_order(tmp_path: Path) -> None:
    """Interleaved writes from two sessions (likely different shards)
    come back with every event present and each session's own order
    intact — cross-session order under a ts tie is not asserted (it's
    genuinely ambiguous), per-session order is the contract."""
    a = Recorder(root=tmp_path, session_id="sess-a")
    b = Recorder(root=tmp_path, session_id="sess-b")
    for i in range(5):
        a.record("write", id=f"a{i}")
        b.record("write", id=f"b{i}")

    ids = [e["id"] for e in iter_events(tmp_path)]
    assert sorted(ids) == sorted(
        [f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)]
    )
    assert [x for x in ids if x[0] == "a"] == [f"a{i}" for i in range(5)]
    assert [x for x in ids if x[0] == "b"] == [f"b{i}" for i in range(5)]


def test_legacy_events_jsonl_merges_in_after_sharding(tmp_path: Path) -> None:
    """A pre-upgrade store's single `.events.jsonl` is read as one more
    source and merged chronologically with the new shard writes — no
    migration, no lost history."""
    legacy = tmp_path / EVENT_LOG_FILENAME
    legacy.write_text(
        json.dumps(
            {
                "ts": "2020-01-01T00:00:00Z",
                "session": "old",
                "kind": "write",
                "id": "old1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    Recorder(root=tmp_path, session_id="new-session").record("write", id="new1")

    ids = [e["id"] for e in iter_events(tmp_path)]
    assert "old1" in ids and "new1" in ids
    # The 2020 legacy event sorts before the just-now sharded write.
    assert ids.index("old1") < ids.index("new1")


def test_iter_events_skips_valid_json_non_object_lines(tmp_path: Path) -> None:
    """A line that parses as VALID JSON but isn't an object (`[1,2,3]`,
    `"a string"`, `42`, `null` — a hand-edit or partial overwrite of this
    plain-text, git-syncable log) must be skipped like any other corrupt
    line. Regression: json.loads succeeded, so the JSONDecodeError guard
    never fired and the non-dict leaked through the Iterator[dict]
    contract — the eval rollups' isinstance guards tolerated it, but
    compute_health's first `ev.get(...)` raised AttributeError, taking
    memory_health / scope_overview / report_for_directory down with it.
    """
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])
    log_path = tmp_path / EVENT_LOG_FILENAME
    with log_path.open("a", encoding="utf-8") as f:
        f.write("[1, 2, 3]\n")
        f.write('"a string"\n')
    rec.record("show", id="01HXYZ")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("42\n")
        f.write("null\n")
        f.write("not json at all\n")
    rec.record("search", query="anything")

    events = list(iter_events(tmp_path))  # must not yield a non-dict
    assert all(isinstance(e, dict) for e in events)
    # ONLY the dict events survive, in write order.
    assert [e["kind"] for e in events] == ["write", "show", "search"]


def test_iter_all_events_survives_corrupt_archives_and_bytes(tmp_path: Path) -> None:
    """A truncated/CRC-corrupt gzip archive or an invalid-UTF-8 byte must not
    crash iter_all_events. It is the sole reader for memory_health,
    scope_overview, eval, and doctor, so one bad file used to blank the whole
    telemetry surface. Regression: EOFError (truncated gz), zlib.error
    (CRC-corrupt gz), and UnicodeDecodeError (bad byte) are NONE of them
    OSError, so the old `except OSError` / `except JSONDecodeError` guards
    missed all three. A valid sibling archive + the active log must still read.
    """
    # A valid archive that MUST survive its corrupt siblings.
    (tmp_path / ".events-20260102.jsonl.gz").write_bytes(
        gzip.compress(b'{"kind": "search", "session": "s", "ts": "t"}\n')
    )
    # Truncated gzip -> EOFError on read.
    full = gzip.compress(b'{"kind": "x"}\n{"kind": "y"}\n')
    (tmp_path / ".events-20260101.jsonl.gz").write_bytes(full[: len(full) // 2])
    # CRC-corrupt gzip -> zlib.error: flip a byte well past the 10-byte header.
    corrupt = bytearray(gzip.compress(b'{"kind": "z"}\n' * 6))
    corrupt[len(corrupt) // 2] ^= 0xFF
    (tmp_path / ".events-20260103.jsonl.gz").write_bytes(bytes(corrupt))
    # Invalid-UTF-8 byte in the active log, between two valid events.
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])
    with (tmp_path / EVENT_LOG_FILENAME).open("ab") as f:
        f.write(b"\xff\xfe not valid json\n")
    rec.record("show", id="01HXYZ")

    events = list(iter_all_events(tmp_path))  # must not raise
    kinds = [e.get("kind") for e in events]
    # Valid archive + both active-log events come through despite the corrupt
    # siblings; the reader degrades per-source instead of aborting.
    assert "search" in kinds
    assert "write" in kinds and "show" in kinds


def test_iter_events_empty_when_no_log(tmp_path: Path) -> None:
    assert list(iter_events(tmp_path)) == []


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


def test_disabled_recorder_writes_nothing(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test", enabled=False)
    rec.record("write", id="01HXYZ", scopes=["tools"])
    rec.record("search", query="anything")

    log_path = tmp_path / EVENT_LOG_FILENAME
    assert not log_path.exists()
    assert list(iter_events(tmp_path)) == []


def test_disabled_recorder_does_not_create_directory(tmp_path: Path) -> None:
    """If telemetry is off, we shouldn't even materialize the dir."""
    target = tmp_path / "nonexistent"
    Recorder(root=target, session_id="sess_test", enabled=False)
    assert not target.exists()


# ---------------------------------------------------------------------------
# Robustness — telemetry failures must not propagate
# ---------------------------------------------------------------------------


def test_record_swallows_filesystem_errors(tmp_path: Path) -> None:
    """If the disk write itself fails, record() must not raise — telemetry is
    a side channel, never a tool blocker."""
    rec = Recorder(root=tmp_path, session_id="sess_test")

    # Force an OSError on open() inside record().
    real_open = Path.open

    def boom(self, *args, **kwargs):
        if self.name == EVENT_LOG_FILENAME:
            raise OSError("simulated disk failure")
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", boom):
        # Should not raise.
        rec.record("write", id="01HXYZ", scopes=["tools"])


def test_record_handles_unserializable_field_via_default(tmp_path: Path) -> None:
    """`json.dumps(..., default=str)` means odd objects don't crash record()."""
    rec = Recorder(root=tmp_path, session_id="sess_test")

    class Weird:
        def __str__(self) -> str:
            return "<weird>"

    rec.record("write", odd=Weird())
    events = list(iter_events(tmp_path))
    assert events[0]["odd"] == "<weird>"


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_archives_when_max_bytes_exceeded(tmp_path: Path) -> None:
    # Tiny max_bytes so a single write trips it after one prior write.
    rec = Recorder(root=tmp_path, session_id="sess_test", max_bytes=200)
    # First write fills well under 200 bytes. Active log present, no archive.
    rec.record("write", id="01HXYZ", scopes=["tools"])
    assert rec.path.stat().st_size > 0  # this session's active shard
    archives_before = list(tmp_path.glob(".events-*.jsonl.gz"))
    assert archives_before == []

    # Pad a few writes so the file crosses 200 bytes — the *next* record()
    # should rotate it.
    for i in range(20):
        rec.record(
            "write",
            id=f"01HXYZ{i:03d}",
            scopes=["tools"],
            note="filler text to push the log past the threshold",
        )

    archives_after = list(tmp_path.glob(".events-*.jsonl.gz"))
    assert len(archives_after) >= 1, "rotation should have produced an archive"

    # The archive contents are valid JSONL, gzipped.
    with gzip.open(archives_after[0], "rt", encoding="utf-8") as f:
        archived_lines = [json.loads(line) for line in f if line.strip()]
    assert all(e["kind"] == "write" for e in archived_lines)


def test_non_positive_max_bytes_never_rotates(tmp_path: Path) -> None:
    # A non-positive max_bytes must mean "never rotate". Without the guard in
    # _rotate_if_needed, `size < max_bytes` is always false for max_bytes <= 0,
    # so every append would gzip-rotate the active log (a rotation storm). The
    # loader clamps a *configured* value, but an explicitly-constructed Recorder
    # can still pass <= 0 (e.g. a programmatic embedder), so the guard lives in
    # _rotate_if_needed as well. Import the archive-name constants locally —
    # they are not part of this file's module-level import block.
    from bettermemory.events import (
        ARCHIVE_PREFIX,
        ARCHIVE_SUFFIX,
        ROTATING_SUFFIX,
    )

    for bad_cap in (0, -1, -10_000):
        root = tmp_path / f"cap_{bad_cap}"
        rec = Recorder(root=root, session_id="sess_test", max_bytes=bad_cap)
        for i in range(30):
            rec.record("write", n=i, note="filler text " * 20)
        # No gzip archives and no in-flight `.rotating` holding files.
        assert list(root.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")) == []
        assert list(root.glob(f"*{ROTATING_SUFFIX}")) == []
        # Every event stayed in the single active log — none lost to rotation.
        events = list(iter_events(root))
        assert len(events) == 30
        assert sorted(e["n"] for e in events) == list(range(30))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl.F_GETFL / O_ACCMODE are POSIX-only; the fsync-ordering "
    "invariant is structurally moot on Windows because the underlying "
    "fcntl-based fd-mode introspection isn't available",
)
def test_rotation_fsyncs_archive_after_gzip_trailer_is_flushed(
    tmp_path: Path,
) -> None:
    """Pin the fsync ordering: the archive fsync must run AFTER the
    `gzip.open(...) as dst` block exits, because the gzip trailer
    (CRC32 + ISIZE) is written by `GzipFile.close()` at `with` exit. If
    the fsync runs from inside the `with` block, only the body bytes
    get pushed to disk; a crash after the unlink could leave a
    body-only archive that `gzip.open` rejects on read with a CRC
    error.

    The structural assertion: at fsync time, the fd's open mode is
    `O_RDONLY` (the file was re-opened to fsync the trailer-inclusive
    bytes), not the write fd of an active GzipFile. Before the fix,
    the fsync ran on the write fd from inside the `with` block; after,
    it runs on a fresh read fd opened against the closed-and-flushed
    archive."""
    # POSIX-only imports. The `sys.platform` narrowing is what tells
    # mypy not to type-check this branch on Windows — without it,
    # strict mode flags `fcntl.fcntl`, `fcntl.F_GETFL`, and
    # `os.O_ACCMODE` as missing attributes on the win32 stubs. The
    # outer `skipif` already guarantees the test is skipped on
    # Windows; the narrowing is purely for the type checker.
    if sys.platform == "win32":  # pragma: no cover - skipped on Windows
        return
    import fcntl
    import os

    from bettermemory import events as events_mod

    fsync_modes: list[int] = []
    # events_mod re-exports fsync_file from _fsutil but doesn't list it
    # in `__all__` (it's an implementation detail). getattr keeps mypy
    # off the attr-defined complaint without polluting the public surface.
    real_fsync_file = getattr(events_mod, "fsync_file")

    def hooked_fsync_file(fd: int) -> None:
        # F_GETFL returns the access-mode flags. O_RDONLY = 0,
        # O_WRONLY = 1, O_RDWR = 2 — a write fd from gzip.open(...,
        # "wb") would be O_WRONLY or O_RDWR; a read fd from
        # archive.open("rb") is O_RDONLY. The masking against O_ACCMODE
        # isolates the access-mode bits from the rest of the flag set.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fsync_modes.append(flags & os.O_ACCMODE)
        real_fsync_file(fd)

    with patch.object(events_mod, "fsync_file", hooked_fsync_file):
        rec = Recorder(root=tmp_path, session_id="sess_trailer", max_bytes=200)
        rec.record("write", id="01HXYZ", scopes=["tools"])
        # Pad past the 200-byte threshold to trigger rotation. Mirror
        # the existing rotation-archives test's loop so the trigger
        # shape is identical.
        for i in range(20):
            rec.record(
                "write",
                id=f"01HXYZ{i:03d}",
                scopes=["tools"],
                note="filler text to push the log past the threshold",
            )

    archives = sorted(tmp_path.glob(".events-*.jsonl.gz"))
    assert archives, "rotation should have produced at least one archive"

    # `fsync_modes` should contain at least one O_RDONLY entry — that's
    # the rotation's archive fsync, structurally proving it happened
    # against a fresh read fd opened after gzip.close() wrote the
    # trailer. The per-append fsyncs (line 135) are also captured here;
    # those go through the same hook on an O_RDWR fd. The presence of
    # the O_RDONLY entry is the load-bearing signal.
    assert os.O_RDONLY in fsync_modes, (
        "rotation fsync should run on a read-only fd opened against the "
        "fully-closed archive — fsync_modes only contains write modes, "
        "which means the fsync still races the gzip trailer write: "
        f"{fsync_modes}"
    )

    # And the archive round-trips as a valid gzip stream — proves the
    # trailer was written and decodable. (Doesn't prove the fix on its
    # own; gzip in-memory close always writes a trailer. This is the
    # belt to the structural suspenders above.)
    with gzip.open(archives[0], "rt", encoding="utf-8") as f:
        decoded = [json.loads(line) for line in f if line.strip()]
    assert decoded, "archive should decode to at least one event"


def test_record_fsyncs_dir_on_first_write(tmp_path: Path) -> None:
    """First write of a fresh event log must fsync the parent directory
    so the new dirent survives power loss.

    POSIX does not guarantee a freshly-created dirent is durable without
    an explicit `fsync` on the parent directory fd — the file's own
    bytes can land while the directory entry listing the file stays in
    page-cache. The audit-loop tick-10 fix adds a `fsync_dir(self.root)`
    call right after the chmod inside the `first_write` branch; this
    pin proves the call lands on first write and only on first write.

    Mirrors the rotation-fsync test's mock pattern: patch `fsync_dir`
    in the `events` module namespace and observe call counts.
    """
    from bettermemory import events as events_mod

    dir_fsync_calls: list[Path] = []
    real_fsync_dir = getattr(events_mod, "fsync_dir")

    def hooked_fsync_dir(path: Path) -> None:
        dir_fsync_calls.append(Path(path))
        real_fsync_dir(path)

    with patch.object(events_mod, "fsync_dir", hooked_fsync_dir):
        rec = Recorder(root=tmp_path, session_id="sess_first_write")
        rec.record("write", id="01HXYZ", scopes=["tools"])

    # First-write fsync_dir must have run, with self.root as the target.
    # Other call sites (rotation paths) don't fire on a single write that
    # doesn't trip max_bytes, so this whole list belongs to the first-
    # write branch under test.
    assert dir_fsync_calls == [tmp_path], (
        f"expected exactly one fsync_dir(self.root) on first write, "
        f"got: {dir_fsync_calls}"
    )


def test_record_does_not_fsync_dir_on_subsequent_writes(tmp_path: Path) -> None:
    """Regression pin: subsequent writes append to an existing dirent
    and don't need re-syncing. fsync_dir is a measurable cost (one
    extra syscall per event); doing it on every append would double
    the fsync overhead for no durability gain past the first write.

    Pre-pin, a tempting "always fsync dir, just to be safe" refactor
    would silently re-introduce that overhead.
    """
    from bettermemory import events as events_mod

    dir_fsync_calls: list[Path] = []
    real_fsync_dir = getattr(events_mod, "fsync_dir")

    def hooked_fsync_dir(path: Path) -> None:
        dir_fsync_calls.append(Path(path))
        real_fsync_dir(path)

    with patch.object(events_mod, "fsync_dir", hooked_fsync_dir):
        rec = Recorder(root=tmp_path, session_id="sess_followup")
        rec.record("write", id="01HXYZ0", scopes=["tools"])
        first_count = len(dir_fsync_calls)
        # Several follow-up writes within the same log, well under
        # max_bytes so no rotation fires.
        for i in range(1, 5):
            rec.record("write", id=f"01HXYZ{i}", scopes=["tools"])

    # Only the first write fsync'd the dir; the four follow-ups did not.
    assert first_count == 1, f"expected 1 first-write fsync, got {first_count}"
    assert len(dir_fsync_calls) == first_count, (
        f"subsequent writes must not fsync the dir; "
        f"first_count={first_count}, total={len(dir_fsync_calls)}, "
        f"calls={dir_fsync_calls}"
    )


def test_rotation_collision_uses_session_suffix(tmp_path: Path) -> None:
    """Many rotations in the same UTC second mustn't clobber each other —
    the collision counter should ensure every archive name is unique."""
    rec = Recorder(root=tmp_path, session_id="sess_collision", max_bytes=80)
    # Tiny max_bytes means almost every write triggers a rotation.
    for i in range(40):
        rec.record("write", id=f"01HXYZ{i:03d}", scopes=["tools"])

    # iter_all_events covers archives + active in chronological order.
    # Every event must be present somewhere — no overwrites allowed.
    all_events = list(iter_all_events(tmp_path))
    assert len(all_events) == 40
    ids = [e["id"] for e in all_events]
    assert ids == [f"01HXYZ{i:03d}" for i in range(40)]

    # And every archive is itself non-empty.
    archives = sorted(tmp_path.glob(".events-*.jsonl.gz"))
    assert archives, "expected at least one rotation"
    for archive in archives:
        with gzip.open(archive, "rt", encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        assert lines


# ---------------------------------------------------------------------------
# iter_all_events: archives + active in chronological order
# ---------------------------------------------------------------------------


def test_iter_all_events_reads_archives_then_active(tmp_path: Path) -> None:
    # First batch: small max_bytes triggers rotation.
    rec = Recorder(root=tmp_path, session_id="sess_test", max_bytes=120)
    for i in range(15):
        rec.record("write", id=f"01HXYZ{i:03d}", scopes=["tools"])

    # The active log holds the tail; the archive(s) hold older events.
    all_events = list(iter_all_events(tmp_path))
    assert len(all_events) == 15
    # Order: archived events come first (older), active events last.
    ids = [e["id"] for e in all_events]
    assert ids == [f"01HXYZ{i:03d}" for i in range(15)]


def test_iter_all_events_handles_no_logs(tmp_path: Path) -> None:
    assert list(iter_all_events(tmp_path)) == []


def test_iter_all_events_handles_missing_root(tmp_path: Path) -> None:
    """Root dir doesn't exist — defensive return."""
    target = tmp_path / "ghost"
    assert list(iter_all_events(target)) == []


# ---------------------------------------------------------------------------
# Concurrency-ish: two recorders pointed at the same dir don't interleave
# corrupt lines (file locking does its job).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Privacy: query / probe_query redaction. Default since 2.6.8 is to
# replace the verbatim text with `{"hash", "preview", "len"}` so a secret
# pasted into a search doesn't land on disk. Verbatim mode opts back in
# via `Recorder(log_queries_verbatim=True)`.
# ---------------------------------------------------------------------------


# Closed-protocol pin for the field-name whitelist consumed by
# `_redact_event_fields` at `events.py:145`. `_REDACTED_TEXT_FIELDS`
# (`events.py:51`, frozenset of `{"query", "probe_query"}`) names every
# event-field whose value is treated as model/user-typed free text and
# replaced with a `{"hash", "preview", "len"}` redaction before the
# event hits the JSONL log. A silent addition of a new recorder field
# name (e.g. a hypothetical `prompt` field on some new event kind)
# without a matching entry in this frozenset would silently leak
# verbatim values to disk — the very thing the 2.6.8 default-redact
# switch exists to prevent. A silent deletion would un-redact the
# affected field, also silently. The existing
# `test_record_redacts_query_by_default` / `test_record_redacts_
# probe_query_field` tests cover the two current members per-name
# (they'd fail if either name dropped out) but neither imports the
# frozenset, so an addition couldn't be caught.
#
# The hardcoded tuple is alphabetised and NOT derived from the source
# set — derivation would silently shrink the expected list when the
# source shrinks, defeating the deletion guard. Mirrors the
# `_EXPECTED_USE_OUTCOMES` shape (db81630) on the privacy surface.
#
# Negative-control: adding `"bogus"` to `_REDACTED_TEXT_FIELDS` fails
# `test_redacted_text_fields_match_frozenset` (set inequality). Revert
# restores green.
_EXPECTED_REDACTED_TEXT_FIELDS: tuple[str, ...] = ("probe_query", "query")


def test_redacted_text_fields_match_frozenset() -> None:
    """Guard so additions to ``_REDACTED_TEXT_FIELDS`` (the closed-protocol
    field-name whitelist consumed by ``_redact_event_fields``) are
    mirrored in the hardcoded ``_EXPECTED_REDACTED_TEXT_FIELDS`` tuple
    — otherwise a new free-text recorder field could ship without
    redaction coverage, silently leaking verbatim values (possibly
    carrying secrets) into the JSONL event log. Mirrors
    ``test_use_outcomes_match_frozenset`` in
    ``tests/test_server_record_use_provenance.py`` — same closed-protocol
    addition-guard pattern on the privacy surface."""
    assert set(_EXPECTED_REDACTED_TEXT_FIELDS) == set(_REDACTED_TEXT_FIELDS)


# Ordered-tuple pin for the secret-shape patterns consumed by
# `_strip_known_secrets` at `events.py:87`. `_SECRET_PATTERNS`
# (`events.py:69`, `tuple[tuple[re.Pattern[str], str], ...]`) carries
# ordered `(regex, marker)` pairs that the stripper applies in tuple
# order via `pattern.sub(replacement, text)`. ORDER IS LOAD-BEARING:
# the comment at `events.py:66-68` documents that the more-specific
# Anthropic pattern `\bsk-ant-…\b` MUST run before the generic
# OpenAI `\bsk-…\b` pattern, otherwise the latter swallows `sk-ant-…`
# tokens and labels them as `[REDACTED:openai-key]` (a downstream
# log-correlation bug — the marker label is the contract with any
# triage tooling that bucketises redactions by provider). A silent
# insertion of an over-broad pattern between two specific siblings
# would also let entropy leak into the 32-char preview window.
#
# Contrast with the basic-shape membership guards landed in bde7602
# (`_REDACTED_TEXT_FIELDS`, `_PLACEHOLDER_PREFIXES`, `_INDEX_FILENAMES`):
# those use `set(...) == set(...)` because order isn't load-bearing
# for `in`-membership. This guard uses *tuple* equality because the
# precedence between members IS load-bearing — a silent reorder would
# pass a set-equality assertion while corrupting the precedence the
# `events.py:66-68` comment documents. Tuple equality catches
# additions, deletions, AND reorders in one assertion. The pattern
# regexes themselves intentionally aren't pinned — they're easier to
# read and review in the source than reflected in a test mirror, and
# the existing `test_strip_known_secrets_*` cases below exercise
# each pattern's substitution surface. The labels are what downstream
# log-correlation tooling keys on, so they're the load-bearing
# half of each tuple.
#
# A future contributor reordering for performance must update both
# the source tuple AND this expected tuple. Treat any drift between
# them as a deliberate decision requiring a CHANGELOG note plus a
# scan of any log-aggregation pipeline that buckets by marker label.
#
# Negative-control: swapping the anthropic-key and openai-key labels
# in `_SECRET_PATTERNS` (mimicking a "performance" reorder that puts
# the more common provider first) fails
# `test_secret_pattern_labels_match_expected_in_order` (tuple
# inequality). Revert restores green.
_EXPECTED_SECRET_PATTERN_LABELS: tuple[str, ...] = (
    "[REDACTED:anthropic-key]",
    "[REDACTED:openai-key]",
    "[REDACTED:github-token]",
    "[REDACTED:github-pat]",
    "[REDACTED:aws-access-key]",
)


def test_secret_pattern_labels_match_expected_in_order() -> None:
    """Guard so additions, deletions, AND reorders of ``_SECRET_PATTERNS``
    (the ordered-tuple secret-shape strip-list consumed by
    ``_strip_known_secrets``) are caught — uses *tuple* equality
    rather than set equality because order is load-bearing.

    The comment at ``events.py:66-68`` documents that the more
    specific Anthropic ``sk-ant-…`` pattern MUST run before the
    generic OpenAI ``sk-…`` pattern; a silent reorder would label
    Anthropic keys as ``[REDACTED:openai-key]`` in the JSONL event
    log, corrupting any downstream log-aggregation tooling that
    buckets redactions by provider. The labels (the second tuple
    member of each pair) are the load-bearing identifiers; the
    regexes are reviewed at the source. A future contributor
    reordering this tuple for performance must update both the
    source AND this expected tuple in the same commit."""
    actual = tuple(label for _, label in _SECRET_PATTERNS)
    assert actual == _EXPECTED_SECRET_PATTERN_LABELS


def test_record_redacts_query_by_default(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test")  # default: redact
    # Query longer than the 32-char preview so the secret tail lands
    # entirely outside the preserved prefix.
    query = "look up how I configure key=sk-very-secret-tail-bytes-here"
    rec.record("search", query=query)

    events = list(iter_events(tmp_path))
    assert len(events) == 1
    q = events[0]["query"]
    assert isinstance(q, dict)
    # First 32 chars survive for triage; the rest does not.
    assert q["preview"] == query[:32]
    assert q["len"] == len(query)
    assert len(q["hash"]) == 16
    # The full query is not recoverable from the on-disk event log —
    # the bytes that lived past the preview are gone. Read the session's
    # active shard file (`rec.path`), where the event actually landed.
    line = rec.path.read_text(encoding="utf-8")
    assert "sk-very-secret-tail-bytes-here" not in line


def test_record_keeps_query_verbatim_when_opted_in(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test", log_queries_verbatim=True)
    rec.record("search", query="kubernetes networking")
    events = list(iter_events(tmp_path))
    assert events[0]["query"] == "kubernetes networking"


def test_record_redacts_probe_query_field(tmp_path: Path) -> None:
    """`probe_query` lives on `search_miss` events. Same redaction
    treatment as `query` — the field name is in `_REDACTED_TEXT_FIELDS`."""
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("search_miss", probe_query="how do I X")
    events = list(iter_events(tmp_path))
    assert isinstance(events[0]["probe_query"], dict)
    assert events[0]["probe_query"]["preview"] == "how do I X"


def test_record_redaction_correlates_repeated_queries(tmp_path: Path) -> None:
    """A repeated query lands with the same hash even though the text
    is gone — gives downstream analytics a correlation handle."""
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("search", query="X")
    rec.record("search", query="Y")
    rec.record("search", query="X")
    events = list(iter_events(tmp_path))
    assert events[0]["query"]["hash"] == events[2]["query"]["hash"]
    assert events[0]["query"]["hash"] != events[1]["query"]["hash"]


def test_redact_query_strips_known_token_shapes(tmp_path: Path) -> None:
    """The 32-char preview can capture entire short secrets (a GitHub
    PAT, AWS access key, OpenAI / Anthropic key prefix). `redact_query`
    pattern-strips known secret shapes BEFORE truncating so the
    preview never carries a partial high-entropy token.

    Defense-in-depth: the event log is local `0o600`, but logs
    occasionally leave that perimeter (an attached `bettermemory eval`
    export, a shared transcript, a bug report).
    """
    from bettermemory.events import redact_query

    # Realistic-shape secret samples (NOT real credentials). Each
    # entry: (raw query, regex marker that must appear, original
    # secret substring that must NOT appear in the preview).
    cases = [
        # Anthropic — `sk-ant-` prefix, must be caught BEFORE the
        # generic `sk-` pattern so the marker is the specific one.
        (
            "key sk-ant-abc1234567890defghijklmnopqrstuv tail",
            "[REDACTED:anthropic-key]",
            "sk-ant-abc1234567890defghijklmnopqrstuv",
        ),
        # OpenAI — generic `sk-…` (no `ant-` segment). 48-char body.
        (
            "key sk-abc123def456ghi789jkl012mno345pqr678stu901vwx234 tail",
            "[REDACTED:openai-key]",
            "sk-abc123def456ghi789jkl012mno345pqr678stu901vwx234",
        ),
        # GitHub classic PAT.
        (
            "header ghp_abcdefghijklmnopqrstuvwxyz0123456789 done",
            "[REDACTED:github-token]",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        ),
        # GitHub fine-grained PAT.
        (
            "header github_pat_abcdefghijklmnopqrstuv done",
            "[REDACTED:github-pat]",
            "github_pat_abcdefghijklmnopqrstuv",
        ),
        # AWS access key — exactly 20 chars total (`AKIA` + 16).
        (
            "creds AKIAIOSFODNN7EXAMPLE tail",
            "[REDACTED:aws-access-key]",
            "AKIAIOSFODNN7EXAMPLE",
        ),
    ]

    for raw, marker, secret in cases:
        out = redact_query(raw)
        # The marker must appear in the preview AND the secret must
        # be gone — pattern-strip runs before the 32-char truncation
        # so both invariants hold on the same value.
        assert marker in out["preview"], (
            f"missing marker {marker!r} in preview {out['preview']!r}"
        )
        assert secret not in out["preview"], (
            f"secret {secret!r} leaked into preview {out['preview']!r}"
        )
        # The pre-strip length is retained — downstream triage can
        # still see "this query was 87 chars" without seeing what
        # those chars were.
        assert out["len"] == len(raw)

    # The change is additive, not breaking: a non-secret query still
    # produces the original 32-char preview behavior.
    benign = "look up how I configure kubernetes networking on raspberry pi"
    out = redact_query(benign)
    assert out["preview"] == benign[:32]
    assert out["len"] == len(benign)
    assert len(out["hash"]) == 16


# ---------------------------------------------------------------------------
# Rotation crash recovery: the .rotating/.gz.tmp two-phase rename should
# leave the reader with each event counted exactly once regardless of
# where a crash lands. The fix replaces the pre-2.6.8 "compress in place
# then unlink source" sequence, which could leave both files present
# after a crash between the gzip-close and the unlink — readers would
# see the events twice in `iter_all_events`.
# ---------------------------------------------------------------------------


def _seed_with_pending_rotation(tmp_path: Path) -> Path:
    """Build a `.rotating` orphan with no matching archive.

    Simulates a crash mid-rotation: the active log was renamed to its
    `.rotating` holding name (step 1 of the new rotation sequence) but
    the gzip step (step 3) never finished, so no `.gz` exists.
    """
    rotating = tmp_path / ".events-20300101T000000Z.jsonl.rotating"
    rotating.write_text(
        '{"ts":"2030-01-01T00:00:00Z","session":"sess_x","kind":"write","id":"RECOVER1"}\n'
        '{"ts":"2030-01-01T00:00:01Z","session":"sess_x","kind":"write","id":"RECOVER2"}\n',
        encoding="utf-8",
    )
    return rotating


def test_iter_all_events_yields_orphan_rotating_when_no_archive(
    tmp_path: Path,
) -> None:
    """A `.rotating` file with no matching archive carries the only
    copy of those events — the reader must include it."""
    _seed_with_pending_rotation(tmp_path)
    events = list(iter_all_events(tmp_path))
    ids = [e["id"] for e in events]
    assert ids == ["RECOVER1", "RECOVER2"]


def test_iter_all_events_skips_orphan_rotating_when_archive_exists(
    tmp_path: Path,
) -> None:
    """If a `.rotating` file and its matching archive both exist, the
    archive is canonical — yielding both would double-count events.
    Pre-2.6.8 the rotate path could leave both present after a crash;
    the reader must defend against double-counting at read time."""
    rotating = _seed_with_pending_rotation(tmp_path)
    archive = rotating.with_name(rotating.name.replace(".rotating", ".gz"))
    # Build a gzipped archive with the same contents.
    with gzip.open(archive, "wb") as gz:
        gz.write(rotating.read_bytes())

    events = list(iter_all_events(tmp_path))
    ids = [e["id"] for e in events]
    assert ids == ["RECOVER1", "RECOVER2"]  # once, not twice


def test_rotation_recovers_orphan_rotating_into_archive(tmp_path: Path) -> None:
    """The next rotation cycle picks up an orphan `.rotating` and finishes
    compressing it. After recovery the orphan is gone and the archive
    contains its events."""
    _seed_with_pending_rotation(tmp_path)
    rec = Recorder(root=tmp_path, session_id="sess_recover", max_bytes=80)
    # A single small write would normally not trigger rotation; pad past
    # the threshold so `_rotate_if_needed` runs. The recovery sweep
    # happens unconditionally at the top of that call.
    for i in range(8):
        rec.record("write", id=f"NEW{i}", note="filler to push past threshold")

    orphans = list(tmp_path.glob(".events-*.jsonl.rotating"))
    assert orphans == [], "orphan .rotating should be recovered"

    # The originally-orphaned events plus the new events all appear once
    # via iter_all_events.
    events = list(iter_all_events(tmp_path))
    ids = [e["id"] for e in events]
    assert ids.count("RECOVER1") == 1
    assert ids.count("RECOVER2") == 1
    assert "NEW0" in ids


# ---------------------------------------------------------------------------
# iter_events_window: window-aware read that survives one mid-window rotation.
# Rotation archives the ENTIRE active log at a size boundary independent of
# turn boundaries; a windowed consumer (the silent-miss probe's retrieval
# shield) reading the active log alone silently loses every event that
# rotated out mid-window. The window reader prepends the newest rotated
# segment when the active log doesn't cover the window — and ONLY then.
# ---------------------------------------------------------------------------


def _window_ts(now: datetime, *, seconds_ago: int) -> str:
    return (now - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _window_now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write_window_active(tmp_path: Path, rows: list[dict[str, object]]) -> None:
    (tmp_path / EVENT_LOG_FILENAME).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _write_window_archive(
    tmp_path: Path, name: str, rows: list[dict[str, object]]
) -> Path:
    archive = tmp_path / name
    archive.write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))
    )
    return archive


def test_iter_events_window_includes_archive_when_active_is_young(
    tmp_path: Path,
) -> None:
    """Mid-window rotation: the active log's oldest event is younger than
    the window start, so events from just before the rotation live only
    in the archive — the window reader must surface them, archive first
    (chronological, matching iter_all_events)."""
    now = _window_now()
    _write_window_archive(
        tmp_path,
        ".events-20260601T115500Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=300), "kind": "search", "id": "ARCH"}],
    )
    _write_window_active(
        tmp_path,
        [{"ts": _window_ts(now, seconds_ago=5), "kind": "write", "id": "ACT"}],
    )
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["ARCH", "ACT"]


def test_iter_events_window_skips_archive_when_active_covers_window(
    tmp_path: Path,
) -> None:
    """No double-read: when the active log's oldest event predates the
    window start, the active log alone covers the window — the archive
    must NOT be opened (its events would be stale duplicates from the
    consumer's perspective, and the read would pay gzip cost on every
    call forever after the first rotation)."""
    now = _window_now()
    _write_window_archive(
        tmp_path,
        ".events-20260601T110000Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=3000), "kind": "search", "id": "ARCH"}],
    )
    _write_window_active(
        tmp_path,
        [
            {"ts": _window_ts(now, seconds_ago=700), "kind": "write", "id": "OLD"},
            {"ts": _window_ts(now, seconds_ago=5), "kind": "write", "id": "NEW"},
        ],
    )
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["OLD", "NEW"]


def test_iter_events_window_missing_or_empty_active_includes_archive(
    tmp_path: Path,
) -> None:
    """A just-rotated (missing or empty) active log can't cover any
    window — the newest archive is the only source for the window's
    events."""
    now = _window_now()
    _write_window_archive(
        tmp_path,
        ".events-20260601T115900Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=30), "kind": "search", "id": "ARCH"}],
    )
    # Missing active log.
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["ARCH"]
    # Empty active log (rotation renamed it away; no append yet).
    (tmp_path / EVENT_LOG_FILENAME).write_text("", encoding="utf-8")
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["ARCH"]


def test_iter_events_window_reads_only_the_newest_archive(tmp_path: Path) -> None:
    """One segment is the documented bound: only the NEWEST archive is
    prepended. Older archives are beyond a single rotation's reach and
    reading them would turn the windowed read back into the full-history
    `iter_all_events` cost."""
    import os

    now = _window_now()
    older = _write_window_archive(
        tmp_path,
        ".events-20260601T110000Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=3000), "kind": "search", "id": "OLDARCH"}],
    )
    newer = _write_window_archive(
        tmp_path,
        ".events-20260601T115500Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=300), "kind": "search", "id": "NEWARCH"}],
    )
    # Pin mtimes so `_archive_sort_key`'s primary key is deterministic
    # regardless of how fast the two writes landed.
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    _write_window_active(
        tmp_path,
        [{"ts": _window_ts(now, seconds_ago=5), "kind": "write", "id": "ACT"}],
    )
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["NEWARCH", "ACT"]


def test_iter_events_window_reads_orphan_rotating_segment(tmp_path: Path) -> None:
    """A rotation that crashed before compression leaves the events' only
    copy in the `.rotating` holding file — the window reader must treat
    it as the newest segment. When a matching archive DOES exist the
    archive is canonical and the holding file is skipped (no
    double-count), mirroring iter_all_events."""
    now = _window_now()
    rotating = tmp_path / ".events-20260601T115500Z.jsonl.rotating"
    rotating.write_text(
        json.dumps(
            {"ts": _window_ts(now, seconds_ago=300), "kind": "search", "id": "ROT"}
        )
        + "\n",
        encoding="utf-8",
    )
    _write_window_active(
        tmp_path,
        [{"ts": _window_ts(now, seconds_ago=5), "kind": "write", "id": "ACT"}],
    )
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["ROT", "ACT"]

    # Matching archive lands (recovery completed): the archive is
    # canonical; the same events must not be yielded twice.
    _write_window_archive(
        tmp_path,
        ".events-20260601T115500Z.jsonl.gz",
        [{"ts": _window_ts(now, seconds_ago=300), "kind": "search", "id": "ROT"}],
    )
    ids = [e["id"] for e in iter_events_window(tmp_path, 600, now=now)]
    assert ids == ["ROT", "ACT"]


def test_iter_events_window_sees_events_across_a_real_rotation(
    tmp_path: Path,
) -> None:
    """End-to-end through the real rotation machinery: record a search,
    trip a rotation (max_bytes=1 rotates the non-empty active log before
    the next append), and confirm the windowed read still returns both
    events exactly once — the active-log-only read loses the first."""
    Recorder(root=tmp_path, session_id="sess_rot").record("search", id="BEFORE")
    Recorder(root=tmp_path, session_id="sess_rot", max_bytes=1).record(
        "write", id="AFTER"
    )
    assert list(tmp_path.glob(".events-*.jsonl.gz")), "rotation did not fire"
    # The plain active-log read demonstrates the gap the window read closes.
    assert [e["id"] for e in iter_events(tmp_path)] == ["AFTER"]
    ids = [e["id"] for e in iter_events_window(tmp_path, 600)]
    assert ids == ["BEFORE", "AFTER"]


def test_two_recorders_one_dir_no_corruption(tmp_path: Path) -> None:
    """A second Recorder pointed at the same dir appends cleanly."""
    a = Recorder(root=tmp_path, session_id="sess_a")
    b = Recorder(root=tmp_path, session_id="sess_b")

    a.record("write", id="A1", scopes=["tools"])
    b.record("write", id="B1", scopes=["tools"])
    a.record("write", id="A2", scopes=["tools"])

    events = list(iter_events(tmp_path))
    assert len(events) == 3
    sessions = {e["session"] for e in events}
    assert sessions == {"sess_a", "sess_b"}
    ids = {e["id"] for e in events}
    assert ids == {"A1", "B1", "A2"}


# ---------------------------------------------------------------------------
# Construction-site class check — the telemetry opt-out must thread
# through EVERY Recorder built in production code
# ---------------------------------------------------------------------------


# Deliberate exceptions to the enabled=-from-config rule, as paths
# relative to src/ (e.g. "bettermemory/foo.py"). EMPTY BY DESIGN: the
# Stop hook (2.6.x) and `bettermemory ingest` (3.22.x) both shipped
# this exact omission — a new construction site defaulted
# `enabled=True` and kept writing events for users who had set
# `[telemetry] enabled = false`. An entry here is a REVIEWED decision
# that a site may ignore the user's telemetry opt-out; today there is
# no such site.
_ENABLED_KWARG_ALLOWLIST: frozenset[str] = frozenset()


def test_every_recorder_site_threads_config_enabled() -> None:
    """Every ``Recorder(...)`` construction under src/ must pass an
    ``enabled=`` keyword sourced from telemetry config — a
    ``<...>.telemetry.enabled`` attribute chain — so `[telemetry]
    enabled = false` turns the event log off EVERYWHERE (the doctor
    module docstring's contract), not just in the MCP server.

    Implementation: AST-walk the source tree and inspect every call
    whose callee is named ``Recorder`` (bare name or attribute access
    like ``events.Recorder``). For each non-allowlisted site, require
    the ``enabled=`` keyword and require its value to be a dotted
    chain ending ``.telemetry.enabled`` (matching every live site:
    ``config.telemetry.enabled``, ``cfg.telemetry.enabled``,
    ``ctx.config.telemetry.enabled``, and doctor's ``telemetry.enabled``
    where ``telemetry`` is the resolved TelemetryConfig).

    Deliberately conservative: a site passing ``enabled`` positionally,
    via ``**kwargs``, or computed through a helper fails this check —
    convert it to the explicit keyword chain (or, after review, add
    the file to the allowlist) rather than broadening the extractor. A
    site constructed through an ALIAS of the class (``R = Recorder``)
    is the one shape the sweep cannot see; none exists today."""
    import ast

    src_root = Path(__file__).resolve().parents[1] / "src" / "bettermemory"

    def _dotted_segments(expr: ast.expr) -> list[str]:
        """Leaf-first attribute chain: `ctx.config.telemetry.enabled`
        -> ["enabled", "telemetry", "config", "ctx"]. Empty when the
        expression is not a plain Name/Attribute chain."""
        segs: list[str] = []
        while isinstance(expr, ast.Attribute):
            segs.append(expr.attr)
            expr = expr.value
        if isinstance(expr, ast.Name):
            segs.append(expr.id)
            return segs
        return []

    sites: list[tuple[str, int, ast.Call]] = []
    for py_file in sorted(src_root.rglob("*.py")):
        rel = py_file.relative_to(src_root.parent).as_posix()
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if callee == "Recorder":
                sites.append((rel, node.lineno, node))

    # The sweep finding nothing means the extractor broke (module move,
    # class rename), not that the invariant holds — fail loudly.
    assert sites, "AST sweep found no Recorder(...) construction site under src/"

    violations: list[str] = []
    for rel, lineno, call in sites:
        if rel in _ENABLED_KWARG_ALLOWLIST:
            continue
        enabled_kw = next((kw for kw in call.keywords if kw.arg == "enabled"), None)
        if enabled_kw is None:
            violations.append(
                f"{rel}:{lineno} — Recorder(...) without an enabled= keyword: "
                f"the site defaults enabled=True and ignores "
                f"[telemetry] enabled = false"
            )
            continue
        segs = _dotted_segments(enabled_kw.value)
        if len(segs) < 2 or segs[0] != "enabled" or segs[1] != "telemetry":
            violations.append(
                f"{rel}:{lineno} — enabled= is not sourced from telemetry "
                f"config (need a `<...>.telemetry.enabled` chain, got "
                f"{ast.unparse(enabled_kw.value)!r})"
            )
    assert not violations, (
        "Recorder construction site(s) that do not thread the user's "
        "telemetry opt-out:\n  "
        + "\n  ".join(violations)
        + "\nWire enabled=<config>.telemetry.enabled (see builder.py), or — "
        "only as a reviewed decision — add the file to "
        "_ENABLED_KWARG_ALLOWLIST in this test."
    )

    stale = set(_ENABLED_KWARG_ALLOWLIST) - {rel for rel, _, _ in sites}
    assert not stale, (
        f"_ENABLED_KWARG_ALLOWLIST entries {sorted(stale)} match no "
        f"Recorder construction site under src/ — remove the stale entries."
    )
