"""A restore writes itself onto the log: the ``store_restored`` row
(ENG-103).

What a restore did was kept only in ``<store_id>.backup_restore.json``
beside the head, on the host that ran it: `log export` shipped nothing of
it, `memory history` showed a record's history jump back with no word of
why, and nothing signed said a restore happened. The restore now appends
a ``store_restored`` control row carrying its record's entry, signed under
the key it made current; `log verify` checks it, `log export` ships it as
a ``control`` row, and `memory history` shows it, as role ``store``, in
the history of every record older than it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import log
from bettermemory.backup import create_backup, restore_bundle, restore_record_path
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

# The kind as the log and every export carry it.
STORE_RESTORED = "store_restored"


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


@pytest.fixture
def restored(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object], str, int, int]:
    """A store restored to a bundle taken after one memory was written,
    over two later rows: the store, the report, the memory's id, the
    bundle's head seq and the seq the replaced log reached."""
    store_file = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    older = _memory("written before the bundle")
    with Store.create(store_file) as created:
        created.put_memory(older)
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        rewound_to = opened.log_rows()[-1].seq
        for n in range(2):
            opened.put_memory(_memory(f"rewound away {n}"))
        reached = opened.log_rows()[-1].seq
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    return store_file, report, older.id, rewound_to, reached


def test_the_restore_appends_a_signed_row_after_the_bundles_head(
    restored: tuple[Path, dict[str, object], str, int, int],
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_file, report, _, rewound_to, reached = restored
    assert getattr(log, "STORE_RESTORED", None) == STORE_RESTORED
    assert STORE_RESTORED in log.CONTROL_KINDS
    with Store.open(store_file, read_only=True) as opened:
        last = opened.log_rows()[-1]
        assert last.seq == rewound_to + 1
        assert last.kind == STORE_RESTORED
        assert report["row"] == {"seq": last.seq, "mac": last.mac}
        payload = json.loads(last.payload)
        assert payload["head_after"]["seq"] == rewound_to
        assert payload["head_before"]["seq"] == reached
        assert payload["bundle"] == report["bundle"]
        assert payload["store_id"] == opened.store_id
        assert "engine_version" in payload
        record = json.loads(Path(str(report["record"])).read_text())["restores"][-1]
        # One entry shape for the rows that replace a history: the record
        # names the row's kind and, once appended, the row; the row carries
        # the rest of the entry.
        assert record["kind"] == payload["kind"] == STORE_RESTORED
        assert record["row"] == report["row"]
        assert {k: v for k, v in payload.items() if k != "engine_version"} == {
            k: v for k, v in record.items() if k != "row"
        }
        # The head checkpoint moved on to it, as after any write; the
        # record's head_after stays the bundle's head.
        head = opened.keyring.read_head()
        assert head is not None and head.seq == last.seq

    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["status"] == "ok"


def test_a_restored_store_this_build_reads_only_gets_no_row(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store waiting on a schema step this build runs opens for reading
    only (``PRAGMA query_only``), as here, and writes nothing: the restore
    finishes without a row, the record keeps the entry, and the text says
    so. (`tests/test_store_steps.py` restores such a store for real.)"""
    store_file = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    with Store.create(store_file) as created:
        created.put_memory(_memory("written before the bundle"))
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        opened.put_memory(_memory("rewound away"))
    real_open = Store.open

    def reading_only(*args: Any, **kwargs: Any) -> Store:
        opened = real_open(*args, **kwargs)
        opened.conn.execute("PRAGMA query_only = ON")
        return opened

    monkeypatch.setattr(Store, "open", reading_only)
    argv = ["backup", "restore", str(bundle), "--yes"]
    assert _run_cli(argv, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "backup restore: restored" in out
    assert "no store_restored row" in out
    monkeypatch.undo()
    with Store.open(store_file, read_only=True) as opened:
        assert opened.log_rows()[-1].kind != STORE_RESTORED
        record = restore_record_path(keys_dir, opened.store_id)
    (entry,) = json.loads(record.read_text())["restores"]
    assert entry["kind"] == STORE_RESTORED and entry["row"] is None


def test_a_bundle_whose_store_needs_a_later_writer_is_refused(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bundle whose store asks for a writer at a later schema revision
    than this build's (ENG-107's ``min_writer_revision``) is refused before
    anything moves, with the upgrade line: restored, it would open for
    reading only here, and the restore could not write its row."""
    import hashlib
    import sqlite3

    from bettermemory.store_schema import SCHEMA_REVISION

    store_file = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    with Store.create(store_file) as created:
        created.put_memory(_memory("written by a later build"))
    later = SCHEMA_REVISION + 1
    with sqlite3.connect(store_file) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [("min_writer_revision", str(later)), ("engine_version", "99.0.0")],
        )
    conn.close()
    bundle = Path(create_backup(store_file)["bundle"])
    before = hashlib.sha256(store_file.read_bytes()).hexdigest()
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "refused"
    detail = str(report["detail"])
    assert f"needs a writer at schema revision {later} or later" in detail
    assert "(its newest writer was bettermemory 99.0.0)" in detail
    assert f"writes revision {SCHEMA_REVISION}: upgrade bettermemory" in detail
    assert report["verify"]["min_writer_revision"] == later
    assert report["moved_aside"] == [] and report["record"] is None
    assert hashlib.sha256(store_file.read_bytes()).hexdigest() == before
    with Store.open(store_file, read_only=True) as opened:
        assert not restore_record_path(keys_dir, opened.store_id).exists()
    argv = ["backup", "restore", str(bundle), "--yes"]
    assert _run_cli(argv, monkeypatch) == 1
    captured = capsys.readouterr()
    assert "upgrade bettermemory to restore it" in captured.out + captured.err


def test_log_export_ships_the_row_as_a_verified_control_row(
    restored: tuple[Path, dict[str, object], str, int, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, report, _, rewound_to, _ = restored
    argv = ["log", "export", "--since-seq", str(rewound_to), "--kinds", STORE_RESTORED]
    assert _run_cli(argv, monkeypatch) == 0
    [line] = capsys.readouterr().out.splitlines()
    exported = json.loads(line)
    assert exported["kind"] == STORE_RESTORED
    assert exported["class"] == "control"
    assert exported["verified"] is True
    assert exported["record_ids"] == []
    assert exported["seq"] == rewound_to + 1
    assert exported["payload"]["head_after"]["seq"] == rewound_to
    assert exported["payload"]["bundle"] == report["bundle"]


def test_memory_history_shows_the_restore_to_an_older_record_only(
    restored: tuple[Path, dict[str, object], str, int, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_file, _, older, rewound_to, reached = restored
    newer = _memory("written after the restore")
    with Store.open(store_file) as opened:
        opened.put_memory(newer)

    assert _run_cli(["memory", "history", older, "--json"], monkeypatch) == 0
    history = json.loads(capsys.readouterr().out)
    [row] = [row for row in history["rows"] if row["role"] == "store"]
    assert row["kind"] == STORE_RESTORED
    assert row["action"] == "rewound"
    assert row["verified"] is True
    assert row["seq"] == rewound_to + 1
    assert row["detail"]["to_seq"] == rewound_to
    assert row["detail"]["from_seq"] == reached
    assert "bundle_created_at" in row["detail"]
    assert history["rows"][-1] == row

    assert _run_cli(["memory", "history", older], monkeypatch) == 0
    text = capsys.readouterr().out
    assert f"seq {rewound_to + 1} " in text
    assert f"{STORE_RESTORED} rewound [mac ok]" in text
    assert f"to_seq={rewound_to} from_seq={reached}" in text

    assert _run_cli(["memory", "history", newer.id, "--json"], monkeypatch) == 0
    later = json.loads(capsys.readouterr().out)
    assert [row["role"] for row in later["rows"] if row["role"] == "store"] == []
