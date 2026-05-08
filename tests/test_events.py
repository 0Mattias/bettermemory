"""Unit tests for events.py — the append-only JSONL event recorder."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import patch


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
