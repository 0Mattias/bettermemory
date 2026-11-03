"""A restore's ``store_restored`` row that the restore could not write is
appended by the writing open that comes after it (ENG-208).

9.0.0rc14 listed two ways a restore left its record entry with no row for
good. A restore that failed between its record and its row reported
failed; running it again wrote a second entry and its row, and nothing
wrote the first one's. A restore onto a store waiting on a schema step
opened the restored store for reading only and appended nothing, and the
owner's open that later ran the step added nothing either. The entry's
``row`` field (null until written, rc14's) now tells the owner's writing
open, and a restore run again, which rows are missing, and each appends
them (`restore_rows`): the same payload, under the store's key, once.

A late row stands at the log's end, so it goes on only while nothing but
control rows stands past the bundle's head: after a record written since
the restore (by a build before 9.0.0rc18, which appends no late row), it
would read in `memory history` as a rewind of that record, and it is
withheld instead (``row_withheld``, naming that record's row), until a
later restore takes the record off the log. Nor does it go on the
history the restore moved aside, put back by hand: the head the restore
replaced tells that one by MAC, and only where the record names none
past the bundle's (``head_before`` null, or not past ``head_after``)
does the first row past the bundle's head, written before the restore,
tell it instead, so a clock stepped back after the restore does not
cost the row where the MAC answers. Every entry kept off the log is
settled and said once, in words that name what the time cannot tell
apart.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import backup, restore_lock, restore_rows
from bettermemory.backup import create_backup, restore_bundle, restore_record_path
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

from .test_store_steps import adds_a_table, bundles, later_build

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


def _log_verifies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bettermemory log verify --json` reads the store ok, exit 0."""
    capsys.readouterr()
    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def _entries(keys_dir: Path, store_id: str) -> list[dict[str, Any]]:
    document = json.loads(restore_record_path(keys_dir, store_id).read_text())
    return list(document["restores"])


def _restored_rows(store_file: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """The store's ``store_restored`` rows: seq, MAC and payload."""
    with Store.open(store_file, read_only=True) as opened:
        return [
            (row.seq, row.mac, json.loads(row.payload))
            for row in opened.log_rows()
            if row.kind == STORE_RESTORED
        ]


def _carries(payload: dict[str, Any], entry: dict[str, Any]) -> bool:
    """The row's payload is the entry's, less the row it names and the
    version of the build that appended it."""
    carried = {k: v for k, v in payload.items() if k != "engine_version"}
    return carried == {k: v for k, v in entry.items() if k != "row"}


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store holding one memory, named by BETTERMEMORY_DIR."""
    path = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(path.parent))
    with Store.create(path) as created:
        created.put_memory(_memory("written before the bundle"))
    return path


@contextlib.contextmanager
def _restored_open_fails(store_file: Path) -> Iterator[None]:
    """For the block, an open of `store_file` itself fails as a disk that
    stopped answering would (the bundle's verification opens a copy
    elsewhere and is left alone): the restore's own open of the store it
    has just restored, after its record and before its row."""
    real = Store.open
    target = store_file.resolve()

    def failing(path: Path | str, *args: Any, **kwargs: Any) -> Store:
        if Path(path).resolve() in (target, target.parent):
            raise sqlite3.OperationalError("disk I/O error")
        return real(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Store, "open", failing)
        yield


def _fail_between_record_and_row(store_file: Path, bundle: Path) -> dict[str, Any]:
    """Restore `bundle` over `store_file`, failing between the record and
    the row; returns the report."""
    with _restored_open_fails(store_file):
        report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed", report
    assert "disk I/O error" in report["detail"]
    assert report["record"] is not None and report["row"] is None
    assert report["lock"]["kept"] is True
    return report


def test_a_restore_that_failed_between_record_and_row_gets_its_row_at_the_next_writing_open(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The restore swapped the bundle in, rewound the head and wrote its
    record, then its open of the restored store failed: no row, and the
    lock keeps its record. Once `backup unlock --yes` lets writers in, the
    owner's next writing open appends the row the restore would have
    appended, the record names it, `log verify` reads ok, and an open after
    that appends nothing more."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        rewound_to = opened.log_rows()[-1].seq
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, bundle)
    (entry,) = _entries(keys_dir, store_id)
    assert entry["kind"] == STORE_RESTORED and entry["row"] is None
    assert _restored_rows(store_file) == []

    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    with Store.open(store_file, allow_rekey=True) as owner:
        last = owner.log_rows()[-1]
    assert last.kind == STORE_RESTORED
    assert last.seq == rewound_to + 1
    [(seq, mac, payload)] = _restored_rows(store_file)
    assert (seq, mac) == (last.seq, last.mac)
    assert _carries(payload, entry)
    assert payload["head_after"]["seq"] == rewound_to
    (named,) = _entries(keys_dir, store_id)
    assert named["row"] == {"seq": seq, "mac": mac}
    _log_verifies(monkeypatch, capsys)

    with Store.open(store_file, allow_rekey=True):
        pass
    assert len(_restored_rows(store_file)) == 1


def test_a_restore_run_again_writes_the_failed_ones_row_before_its_own(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The way out the refusal names: run the same restore again. It takes
    the record over and finishes, and the log now tells both restores, in
    the order they ran: the failed one's row first (it rewound the history
    that was there), then its own. Each entry names its row."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        rewound_to = opened.log_rows()[-1].seq
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, bundle)

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    first, second = _entries(keys_dir, store_id)
    rows = _restored_rows(store_file)
    assert [seq for seq, _, _ in rows] == [rewound_to + 1, rewound_to + 2]
    assert _carries(rows[0][2], first) and _carries(rows[1][2], second)
    assert first["row"] == {"seq": rows[0][0], "mac": rows[0][1]}
    assert second["row"] == {"seq": rows[1][0], "mac": rows[1][1]}
    assert report["row"] == second["row"]
    # The second restore found the first one's history in place, rewound
    # by it: that is what its row says it replaced.
    assert second["head_before"]["seq"] == rewound_to
    _log_verifies(monkeypatch, capsys)


def test_a_restore_onto_a_store_waiting_on_a_step_gets_its_row_from_the_open_that_runs_it(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later build ran a step that is not additive, after a backup of the
    store as it stood; restoring that bundle under the same build puts back
    a store waiting on the step, which the restore opens for reading only:
    it finishes with no row, and its record keeps the entry. The owner's
    open that runs the step again appends the row, the record names it,
    and the log verifies."""
    path = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(path.parent))
    with Store.create(path) as created:
        created.put_memory(_memory("A memory the store held before any later step."))
        store_id = created.store_id
    calls: list[str] = []
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True) as newer:
            newer.put_memory(_memory("A memory written after the step."))
        (bundle,) = bundles(path)
        report = backup.restore_bundle(bundle, path, apply=True)
        assert report["status"] == backup.RESTORED, report["detail"]
        assert report["row"] is None
        assert _restored_rows(path) == []
        (entry,) = _entries(keys_dir, store_id)
        assert entry["row"] is None

        with Store.open(path, allow_rekey=True) as owner:
            assert owner.conn.execute("PRAGMA query_only").fetchone()[0] == 0
            last = owner.log_rows()[-1]
            assert owner.log_verify()["status"] == "ok"
        assert last.kind == STORE_RESTORED
        [(seq, mac, payload)] = _restored_rows(path)
        assert (seq, mac) == (last.seq, last.mac)
        assert _carries(payload, entry)
        (named,) = _entries(keys_dir, store_id)
        assert named["row"] == {"seq": seq, "mac": mac}
    assert len(calls) == 2


def test_a_row_on_the_log_that_its_entry_does_not_name_is_named_not_appended_again(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restore that appended its row and died before its record named it
    (here, the record's write of the row is lost) leaves a row on the log
    and an entry with ``row`` null. The next writing open finds the row
    whose payload is the entry's, names it in the record and appends no
    second one."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(restore_rows, "record_row", lambda *args, **kwargs: None)
        report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    [(seq, mac, _)] = _restored_rows(store_file)
    assert report["row"] == {"seq": seq, "mac": mac}
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None

    with Store.open(store_file, allow_rekey=True):
        pass
    assert [(s, m) for s, m, _ in _restored_rows(store_file)] == [(seq, mac)]
    (named,) = _entries(keys_dir, store_id)
    assert named["row"] == {"seq": seq, "mac": mac}
    _log_verifies(monkeypatch, capsys)


def test_the_history_a_restore_moved_aside_never_gets_its_row(
    store_file: Path, keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """After a restore failed between its record and its row, the operator
    unlocks and puts the file the restore moved aside back by hand. That
    file holds the bundle's head too (a bundle is taken from the store's
    own history) but also the head the restore replaced, so no restore put
    its history in place: the owner's open appends no row to it, and the
    entry keeps waiting for the history it names. The replaced head's MAC
    decided it, so the warning says the file is the replaced history, and
    names no clock."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("kept in the file put back"))
    report = _fail_between_record_and_row(store_file, bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    for move in report["moved_aside"]:
        os.replace(move["to"], move["from"])

    caplog.set_level("WARNING", logger="bettermemory.restore_rows")
    with Store.open(store_file, allow_rekey=True) as owner:
        kinds = [row.kind for row in owner.log_rows()]
    assert STORE_RESTORED not in kinds
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None
    said = [r.getMessage() for r in caplog.records if "row_withheld" in r.getMessage()]
    assert len(said) == 1, said
    assert "this store file holds the history the restore" in said[0]
    assert "clock" not in said[0]


def test_the_record_is_read_with_the_bundle_manifests_cap() -> None:
    """The record's read cap is the manifest's, as its constant says."""
    assert restore_rows.RECORD_CAP_BYTES == backup._MANIFEST_CAP_BYTES


def _withheld(row: Any, because: str) -> dict[str, Any]:
    """The ``row_withheld`` a log row decides, for `because`."""
    return {"seq": row.seq, "kind": row.kind, "mac": row.mac, "because": because}


@contextlib.contextmanager
def _a_build_that_appends_no_late_row() -> Iterator[None]:
    """For the block, the owner's open appends no late row, as 9.0.0rc14 to
    9.0.0rc17 open the store: they read the record's ``row`` and append
    none."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(restore_rows, "settle", lambda store: None)
        yield


def _store_rows_of(store_file: Path, record_id: str) -> list[dict[str, Any]]:
    """The rows `memory history` shows for `record_id` in the ``store``
    role: the restores and accepted histories it reads as having replaced
    the record's history."""
    from bettermemory.history import record_history

    with Store.open(store_file, read_only=True) as opened:
        history = record_history(opened, record_id)
    return [row.to_dict(diff=False) for row in history.rows if row.role == "store"]


def test_a_late_row_is_withheld_once_a_record_was_written_after_the_restore(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restore failed between its record and its row, the operator ran
    `backup unlock --yes`, and a build before 9.0.0rc18 wrote a memory
    (here, an owner's open that appends no late row). This build's first
    owner open finds the record row past the bundle's head: a late row
    there would read in `memory history` as a rewind of that memory, from
    a seq the restore never reached. So no row is appended; the entry
    keeps ``row`` null, gains ``row_withheld`` naming the memory's row,
    and the open says so once. The next open says nothing, appends
    nothing, and the log verifies."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    later = _memory("written under a build that appends no late row")
    with _a_build_that_appends_no_late_row():
        with Store.open(store_file, allow_rekey=True) as older:
            older.put_memory(later)
            written = older.log_rows()[-1]

    caplog.set_level("WARNING", logger="bettermemory.restore_rows")
    with Store.open(store_file, allow_rekey=True):
        pass
    said = [r.getMessage() for r in caplog.records if "row_withheld" in r.getMessage()]
    assert len(said) == 1, said
    assert f"at seq {written.seq}" in said[0]
    assert _restored_rows(store_file) == []
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None
    assert entry[restore_rows.WITHHELD_KEY] == _withheld(written, "record")
    assert _store_rows_of(store_file, later.id) == []

    caplog.clear()
    with Store.open(store_file, allow_rekey=True):
        pass
    assert not [r for r in caplog.records if "store_restored" in r.getMessage()]
    assert _restored_rows(store_file) == []
    _log_verifies(monkeypatch, capsys)


def test_a_later_restore_withholds_the_row_of_an_earlier_one_a_record_came_after(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """As above, but the next thing to write is this build's restore of a
    bundle taken after that memory: the restored log holds the earlier
    restore's head, then the memory. The restore appends its own row and
    not the earlier one's, which would stand after the memory; the earlier
    entry is withheld, the later one names its row."""
    first_bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, first_bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    later = _memory("written under a build that appends no late row")
    with _a_build_that_appends_no_late_row():
        with Store.open(store_file, allow_rekey=True) as older:
            older.put_memory(later)
            written = older.log_rows()[-1]
        second_bundle = Path(create_backup(store_file)["bundle"])

    report = restore_bundle(second_bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    first, second = _entries(keys_dir, store_id)
    assert first["row"] is None
    assert first[restore_rows.WITHHELD_KEY] == _withheld(written, "record")
    [(seq, mac, payload)] = _restored_rows(store_file)
    assert second["row"] == {"seq": seq, "mac": mac} == report["row"]
    assert _carries(payload, second)
    _log_verifies(monkeypatch, capsys)


@pytest.mark.parametrize("head", ["missing", "behind"])
def test_the_history_moved_aside_gets_no_row_when_the_head_checkpoint_did_not_name_it(
    store_file: Path, keys_dir: Path, head: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The head checkpoint was missing, or behind the log (put back from an
    older copy), when the restore ran, so the entry's ``head_before`` does
    not name the history the restore moved aside. The restore failed
    between its record and its row; the operator unlocked and put the
    moved-aside file back by hand. Its first row past the bundle's head
    was written before the restore ran, so it is the replaced history:
    the owner's open appends no row to it, and the entry is withheld as
    the replaced history's, by that row. The warning names what the time
    cannot tell apart, and why the time decided: no head checkpoint at
    the restore, or one not past the bundle's head, not a missing one."""
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        head_path = opened.keyring.head_path()
    older = head_path.read_bytes()
    with Store.open(store_file) as opened:
        opened.put_memory(_memory("before the bundle"))
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        opened.put_memory(_memory("kept in the file put back"))
    if head == "missing":
        head_path.unlink()
    else:
        head_path.write_bytes(older)
    report = _fail_between_record_and_row(store_file, bundle)
    (entry,) = _entries(keys_dir, store_id)
    assert entry["log_end_before"] is not None
    before = entry["head_before"]
    assert before is None or before["seq"] < entry["head_after"]["seq"]
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    for move in report["moved_aside"]:
        os.replace(move["to"], move["from"])

    caplog.set_level("WARNING", logger="bettermemory.restore_rows")
    with Store.open(store_file, allow_rekey=True) as owner:
        kinds = [row.kind for row in owner.log_rows()]
    assert STORE_RESTORED not in kinds
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None
    assert entry[restore_rows.WITHHELD_KEY]["because"] == "replaced"
    said = [r.getMessage() for r in caplog.records if "row_withheld" in r.getMessage()]
    assert len(said) == 1, said
    assert "stamped before the restore: the history it replaced, or a clock" in said[0]
    if before is None:
        why = "with no head checkpoint at the restore (head_before null) to compare"
    else:
        why = (
            f"with no replaced head past the bundle's (head_before at seq "
            f"{before['seq']}, the bundle's head at seq {entry['head_after']['seq']})"
        )
    assert why in said[0], said[0]


@pytest.mark.parametrize(
    ("head_before", "log_end_before", "what", "why"),
    [
        (
            None,
            {"seq": 3, "path": "memory.sqlite"},
            "the history it replaced, or a clock set back",
            "no head checkpoint at the restore (head_before null)",
        ),
        (
            {"seq": 1, "mac": "m1"},
            {"seq": 3, "path": "memory.sqlite"},
            "the history it replaced, or a clock set back",
            "no replaced head past the bundle's (head_before at seq 1, the "
            "bundle's head at seq 2)",
        ),
        (
            {"seq": 2, "mac": "m2"},
            None,
            "a history written before the restore (the one it replaced, or "
            "another copy of the store), or a clock set back",
            "no replaced head past the bundle's (head_before at seq 2, the "
            "bundle's head at seq 2)",
        ),
        (
            None,
            None,
            "another copy of the store, written before the restore (this host "
            "held none when it ran: head_before and log_end_before null), or a "
            "clock set back",
            "no head checkpoint at the restore (head_before null)",
        ),
    ],
    ids=["no-checkpoint", "checkpoint-not-past", "no-log-read", "no-copy-here"],
)
def test_a_withhold_the_clock_decided_says_what_the_file_may_be(
    head_before: dict[str, Any] | None,
    log_end_before: dict[str, Any] | None,
    what: str,
    why: str,
) -> None:
    """Where the time decided a ``replaced`` withhold, the warning says
    what a file whose first row past the bundle's head was written before
    the restore may be, by what the entry holds: the history the restore
    replaced where it read that log here (``log_end_before``); the one
    its head checkpoint named, or another copy, where it read only that;
    and, where it read neither, not a replaced history at all, since this
    host held no copy of the store: another copy, or a clock set back. And
    why the time decided: no head checkpoint at the restore, or one not
    past the bundle's head, which is there but says nothing."""
    entry = {
        "restored_at": "2026-11-03T00:00:00Z",
        "bundle": "the bundle",
        "head_after": {"seq": 2, "mac": "m2"},
        "head_before": head_before,
        "log_end_before": log_end_before,
    }
    withheld = {"seq": 3, "kind": "rekey", "mac": "m3", "because": "replaced"}
    said, args = restore_rows._said("the store", entry, withheld)
    text = said % args
    assert (
        f"(a rekey row at seq 3) is stamped before the restore: {what}; with "
        f"{why} to compare, nothing tells which"
    ) in text
    if log_end_before is None and head_before is None:
        assert "the history it replaced" not in text


def test_the_history_moved_aside_gets_no_row_when_only_a_rekey_follows_the_bundle(
    store_file: Path, keys_dir: Path
) -> None:
    """As above with no record past the bundle's head on the history moved
    aside, only the ``rekey`` row of a `key rotate`, and the head
    checkpoint missing: neither its ``head_before`` nor a record row tells
    the moved-aside file, but its rekey row was written before the
    restore ran. No row is appended to it."""
    from bettermemory import key_lifecycle

    with Store.open(store_file) as opened:
        store_id = opened.store_id
        head_path = opened.keyring.head_path()
    bundle = Path(create_backup(store_file)["bundle"])
    rotated = key_lifecycle.rotate_key(store_file, apply=True, keys_dir=keys_dir)
    assert rotated["status"] == "done", rotated
    head_path.unlink()
    report = _fail_between_record_and_row(store_file, bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    for move in report["moved_aside"]:
        os.replace(move["to"], move["from"])

    with Store.open(store_file, allow_rekey=True) as owner:
        kinds = [row.kind for row in owner.log_rows()]
    assert STORE_RESTORED not in kinds
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None
    assert entry[restore_rows.WITHHELD_KEY]["because"] == "replaced"


def _skew(keys_dir: Path, store_id: str, seconds: float) -> None:
    """Move the last entry's ``restored_at`` `seconds` later, as a host
    whose clock ran ahead at the restore and was stepped back after it
    would have written it."""
    from datetime import timedelta

    from bettermemory.time_utils import isoformat_utc, parse_event_ts

    path = restore_record_path(keys_dir, store_id)
    document = json.loads(path.read_text())
    entry = document["restores"][-1]
    ran = parse_event_ts(entry["restored_at"])
    assert ran is not None
    entry["restored_at"] = isoformat_utc(ran + timedelta(seconds=seconds))
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _rotate(store_file: Path, keys_dir: Path) -> None:
    from bettermemory import key_lifecycle

    rotated = key_lifecycle.rotate_key(store_file, apply=True, keys_dir=keys_dir)
    assert rotated["status"] == "done", rotated


def test_a_clock_stepped_back_after_the_restore_does_not_cost_its_row(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The restore's host clock ran an hour ahead and was stepped back: the
    `key rotate` after the unlock writes a rekey row stamped before the
    entry's ``restored_at``. The head the restore replaced is past the
    bundle's, and the restored log does not hold it, so that is what
    tells the restored history from the one moved aside; the clock is not
    asked, and the owner's open appends the row after the rekey row."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, bundle)
    _skew(keys_dir, store_id, 3600)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    _rotate(store_file, keys_dir)

    with Store.open(store_file, allow_rekey=True) as owner:
        last = owner.log_rows()[-1]
    assert last.kind == STORE_RESTORED
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] == {"seq": last.seq, "mac": last.mac}
    assert restore_rows.WITHHELD_KEY not in entry
    _log_verifies(monkeypatch, capsys)


def test_an_entry_the_clock_places_on_the_replaced_history_is_settled_and_said_once(
    store_file: Path, keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Where the head checkpoint was missing at the restore, only the clock
    tells the two histories apart, and a clock stepped back after the
    restore misleads it: the rekey row written after the unlock reads as
    written before the restore, so the restored log reads as the replaced
    one. The entry does not stay unsettled: it is withheld by that rekey
    row (``replaced``), said once, and the next open reads one row and
    says nothing. The warning does not say the file is the replaced
    history, which it is not: it says the row past the bundle's head is
    stamped before the restore, which the replaced history or a clock set
    back explains, and that the file is not placed again while the row
    stands, since the row never leaves it."""
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        head_path = opened.keyring.head_path()
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        opened.put_memory(_memory("rewound away"))
    head_path.unlink()
    _fail_between_record_and_row(store_file, bundle)
    _skew(keys_dir, store_id, 3600)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    _rotate(store_file, keys_dir)

    caplog.set_level("WARNING", logger="bettermemory.restore_rows")
    with Store.open(store_file, allow_rekey=True) as owner:
        rekey = [row for row in owner.log_rows() if row.kind == "rekey"][0]
    said = [r.getMessage() for r in caplog.records if "row_withheld" in r.getMessage()]
    assert len(said) == 1, said
    stamped = (
        f"first row past the bundle's head (a rekey row at seq {rekey.seq}) is "
        "stamped before the restore: the history it replaced, or a clock set back"
    )
    assert stamped in said[0]
    assert "not placed again while that row stands" in said[0]
    assert "holds the history" not in said[0]
    (entry,) = _entries(keys_dir, store_id)
    assert entry["row"] is None
    assert entry[restore_rows.WITHHELD_KEY] == _withheld(rekey, "replaced")
    caplog.clear()
    with Store.open(store_file, allow_rekey=True):
        pass
    assert not [r for r in caplog.records if "store_restored" in r.getMessage()]


def test_a_withheld_row_is_appended_once_a_later_restore_takes_the_record_away(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first restore failed between its record and its row; after the
    unlock a bundle was taken, then an open that appends no late row wrote
    a memory, so the owner's open withheld the first restore's row. A
    restore of that later bundle rewinds the memory away: nothing but the
    first restore's history stands past its head now, so its row is
    appended before the second restore's own, ``row_withheld`` goes, and
    both entries name their rows."""
    first_bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, first_bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    second_bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file, allow_rekey=False) as other:
        other.put_memory(_memory("written by an open that appends no late row"))
        written = other.log_rows()[-1]
    with Store.open(store_file, allow_rekey=True):
        pass
    (entry,) = _entries(keys_dir, store_id)
    assert entry[restore_rows.WITHHELD_KEY] == _withheld(written, "record")

    report = restore_bundle(second_bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    first, second = _entries(keys_dir, store_id)
    rows = _restored_rows(store_file)
    assert [seq for seq, _, _ in rows] == [written.seq, written.seq + 1]
    assert first["row"] == {"seq": rows[0][0], "mac": rows[0][1]}
    assert second["row"] == {"seq": rows[1][0], "mac": rows[1][1]} == report["row"]
    assert restore_rows.WITHHELD_KEY not in first
    assert _carries(rows[0][2], {k: v for k, v in first.items()})
    assert "row_withheld" not in rows[0][2]
    _log_verifies(monkeypatch, capsys)


def test_two_opens_that_withhold_the_same_row_say_it_once(
    store_file: Path, keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two owner opens decide the same withholding at once; the one that
    finds the record already holding it says nothing (it said the record
    could not be written, which was not so)."""
    bundle = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file) as opened:
        store_id = opened.store_id
        opened.put_memory(_memory("rewound away"))
    _fail_between_record_and_row(store_file, bundle)
    assert restore_lock.unlock(store_file, apply=True)["status"] == "cleared"
    with Store.open(store_file, allow_rekey=False) as other:
        other.put_memory(_memory("written by an open that appends no late row"))

    caplog.set_level("WARNING", logger="bettermemory.restore_rows")
    with Store.open(store_file, read_only=False, allow_rekey=False) as first:
        (entry,) = restore_rows.pending(first)
        verdict, withheld = restore_rows._settle_one(first, entry)
        assert verdict == "withheld" and withheld is not None
        path = restore_record_path(keys_dir, store_id)
        restore_rows._report_withheld(first, path, entry, withheld)
        restore_rows._report_withheld(first, path, entry, withheld)
    said = [
        r.getMessage() for r in caplog.records if "store_restored" in r.getMessage()
    ]
    assert len(said) == 1, said
    assert "could not be written" not in said[0]
