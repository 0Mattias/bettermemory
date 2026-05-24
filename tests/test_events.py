"""Unit tests for events.py — the append-only JSONL event recorder."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bettermemory.events import (
    EVENT_LOG_FILENAME,
    Recorder,
    iter_all_events,
    iter_events,
)


# ---------------------------------------------------------------------------
# Basic record / read round-trips
# ---------------------------------------------------------------------------


def test_record_appends_one_jsonl_line(tmp_path: Path) -> None:
    rec = Recorder(root=tmp_path, session_id="sess_test")
    rec.record("write", id="01HXYZ", scopes=["tools"])

    log_path = tmp_path / EVENT_LOG_FILENAME
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
    assert (tmp_path / EVENT_LOG_FILENAME).stat().st_size > 0
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
    # the bytes that lived past the preview are gone.
    line = (tmp_path / EVENT_LOG_FILENAME).read_text(encoding="utf-8")
    assert "sk-very-secret-tail-bytes-here" not in line


def test_record_keeps_query_verbatim_when_opted_in(tmp_path: Path) -> None:
    rec = Recorder(
        root=tmp_path, session_id="sess_test", log_queries_verbatim=True
    )
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
