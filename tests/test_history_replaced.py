"""The two rows that say a store's history was replaced read as one
contract (rc14, the cold review's fold of ENG-103).

``store_restored`` is what `backup restore` appends when a verified
bundle's snapshot replaces the store file; ``history_accepted`` is what
`bettermemory log accept-history --yes` (ENG-106) appends when the file's
history is accepted as it stands. Both are appended by one helper
(`log.append_history_replaced`), both keep an entry in the record beside
the head that names the row's kind and the row (`log.history_replaced_kind`
and `log.history_replaced_row`, which also read an entry written the way
`log accept-history` writes it: ``accepted_history`` and ``seq``), and
`log export`, `log verify` and `memory history` read both, each in its own
words. Before the contract, `log export` said "a restore" for an accepted
history, its ``--json`` summary carried no rewind at all, and `memory
history` and `log verify` did not show an accepted history.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import log
from bettermemory.backup import (
    _append_record,
    create_backup,
    restore_bundle,
    restore_record_path,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

STORE_RESTORED = "store_restored"
HISTORY_ACCEPTED = "history_accepted"
ACCEPTED_AT = "2026-10-29T12:00:00.000000Z"

DOCS = Path(__file__).resolve().parents[1] / "docs"


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


@dataclass
class Replaced:
    """A store whose history was replaced twice: a restore to a bundle
    (`rewound_to`, over a log that had reached `reached`; its row at
    `restore_seq`), then an accepted history (its row at `accept_seq`)
    that took the log as it stood at `end`, behind a head checkpoint that
    had said `head_said`. `older` is a memory written before both."""

    store_file: Path
    keys_dir: Path
    older: str
    rewound_to: int
    reached: int
    restore_seq: int
    end: int
    head_said: int
    accept_seq: int


@pytest.fixture
def replaced(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Replaced:
    store_file = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    older = _memory("written before the history was replaced")
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
    # The acceptance as `log accept-history` (ENG-106) writes it: its row,
    # then a record entry marked `accepted_history` that names the row by
    # its `seq` and no `kind`.
    with Store.open(store_file) as opened:
        end = opened.log_rows()[-1].seq
        entry = {
            "restored_at": ACCEPTED_AT,
            "accepted_history": True,
            "store": str(store_file),
            "store_id": opened.store_id,
            "replaced_store_id": opened.store_id,
            "head_before": {"seq": end + 3, "mac": "0" * 64},
            "head_after": {"seq": end},
            "log_end_before": {"seq": end},
        }
        row = log.append_history_replaced(opened, HISTORY_ACCEPTED, entry)
        assert row is not None
        record = restore_record_path(keys_dir, opened.store_id)
        _append_record(record, {**entry, "seq": row["seq"]}, "20261029T120000Z")
    return Replaced(
        store_file=store_file,
        keys_dir=keys_dir,
        older=older.id,
        rewound_to=rewound_to,
        reached=reached,
        restore_seq=int(report["row"]["seq"]),
        end=end,
        head_said=end + 3,
        accept_seq=int(row["seq"]),
    )


def test_a_record_entry_names_its_kind_and_row_one_way() -> None:
    """One entry shape: the kind and the row an entry names, read off an
    entry that says them, and off one written before entries did."""
    assert set(log.HISTORY_REPLACED) == {STORE_RESTORED, HISTORY_ACCEPTED}
    assert set(log.HISTORY_REPLACED) <= log.CONTROL_KINDS
    assert log.HISTORY_REPLACED[STORE_RESTORED][1] == "rewound"
    assert log.HISTORY_REPLACED[HISTORY_ACCEPTED][1] == "accepted"
    kind = log.history_replaced_kind
    assert kind({"kind": HISTORY_ACCEPTED}) == HISTORY_ACCEPTED
    assert kind({"accepted_history": True}) == HISTORY_ACCEPTED
    assert kind({}) == STORE_RESTORED
    assert kind({"kind": "rekey"}) == STORE_RESTORED
    row = log.history_replaced_row
    assert row({"row": {"seq": 4, "mac": "m"}}) == {"seq": 4, "mac": "m"}
    assert row({"seq": 5}) == {"seq": 5}
    assert row({"row": None}) is None
    assert row({"seq": True}) is None


def test_log_verify_names_no_replaced_history_where_there_is_none(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store whose history was never replaced verifies to the report it
    did before the contract: no ``history_replaced`` key, no lines."""
    store_file = tmp_path / "store" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    with Store.create(store_file) as created:
        created.put_memory(_memory("a history nobody replaced"))
    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 0
    assert "history_replaced" not in json.loads(capsys.readouterr().out)
    assert _run_cli(["log", "verify"], monkeypatch) == 0
    assert "history replaced" not in capsys.readouterr().out


def test_the_helper_appends_only_a_kind_that_replaces_a_history(
    tmp_path: Path, keys_dir: Path
) -> None:
    store_file = tmp_path / "store" / STORE_FILENAME
    with Store.create(store_file) as store:
        before = store.log_rows()[-1].seq
        with pytest.raises(ValueError, match="replaces a store's history"):
            log.append_history_replaced(store, log.REKEY, {})
        store.conn.execute("PRAGMA query_only = ON")
        assert log.append_history_replaced(store, HISTORY_ACCEPTED, {}) is None
        store.conn.execute("PRAGMA query_only = OFF")
        assert store.log_rows()[-1].seq == before


def test_the_restores_record_entry_names_its_kind_and_row(
    replaced: Replaced,
) -> None:
    with Store.open(replaced.store_file, read_only=True) as opened:
        rows = {row.seq: row for row in opened.log_rows()}
        record = restore_record_path(replaced.keys_dir, opened.store_id)
    restore, accept = json.loads(record.read_text())["restores"]
    assert restore["kind"] == STORE_RESTORED
    restore_row = rows[replaced.restore_seq]
    assert restore["row"] == {"seq": restore_row.seq, "mac": restore_row.mac}
    assert log.history_replaced_kind(accept) == HISTORY_ACCEPTED
    assert log.history_replaced_row(accept) == {"seq": replaced.accept_seq}
    assert rows[replaced.accept_seq].kind == HISTORY_ACCEPTED


def _export(
    replaced: Replaced,
    since: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, dict[str, object]]:
    out = replaced.store_file.parent.parent / f"export-{since}.jsonl"
    argv = ["log", "export", "--since-seq", str(since), "--out", str(out), "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    captured = capsys.readouterr()
    return captured.err, json.loads(captured.out)


def test_log_export_words_each_kind_and_names_it_in_the_summary(
    replaced: Replaced,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cursor inside the range the restore rewound is warned in a
    restore's words, one inside the range the acceptance left behind its
    head in an acceptance's, and the ``--json`` summary names each
    (cold review L8: the warning went to stderr only)."""
    err, summary = _export(replaced, replaced.restore_seq, monkeypatch, capsys)
    assert "a restore at " in err
    assert f"rewound the log from seq {replaced.reached} to seq" in err
    assert "accepted history" not in err
    rewinds = summary["rewinds"]
    assert isinstance(rewinds, list)
    assert [(r["kind"], r["seq"]) for r in rewinds] == [
        (STORE_RESTORED, replaced.restore_seq)
    ]
    err, summary = _export(replaced, replaced.head_said, monkeypatch, capsys)
    assert (
        f"an accepted history at {ACCEPTED_AT} accepted the log as it stood at "
        f"seq {replaced.end}, where the head checkpoint had said seq "
        f"{replaced.head_said}, and --since-seq {replaced.head_said} lies "
        "between: a cursor taken before that acceptance skips"
    ) in " ".join(err.split())
    assert "a restore at" not in err
    assert summary["rewinds"] == [
        {
            "kind": HISTORY_ACCEPTED,
            "restored_at": ACCEPTED_AT,
            "from_seq": replaced.head_said,
            "to_seq": replaced.end,
            "seq": replaced.accept_seq,
        }
    ]


def test_log_verify_lists_both_kinds_in_their_words(
    replaced: Replaced,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    listed = [
        (entry["seq"], entry["kind"], entry["action"], entry.get("to_seq"))
        for entry in report["history_replaced"]
    ]
    assert listed == [
        (replaced.restore_seq, STORE_RESTORED, "rewound", replaced.rewound_to),
        (replaced.accept_seq, HISTORY_ACCEPTED, "accepted", replaced.end),
    ]
    assert report["history_replaced"][1]["from_seq"] == replaced.head_said
    assert _run_cli(["log", "verify"], monkeypatch) == 0
    text = capsys.readouterr().out
    assert "  history replaced 2 time(s)\n" in text
    assert (f"    seq {replaced.restore_seq} store_restored: a restore at ") in text
    assert (
        f" put the log back to seq {replaced.rewound_to} from seq {replaced.reached}\n"
    ) in text
    assert (
        f"    seq {replaced.accept_seq} history_accepted: an accepted history at "
    ) in text
    assert (
        f" accepted the log as it stood at seq {replaced.end}, where the head "
        f"checkpoint had said seq {replaced.head_said}\n"
    ) in text


def test_memory_history_shows_both_kinds_with_their_actions(
    replaced: Replaced,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["memory", "history", replaced.older, "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    store_rows = [
        (row["seq"], row["kind"], row["action"], row["verified"], row["detail"])
        for row in rows
        if row["role"] == "store"
    ]
    assert [row[:4] for row in store_rows] == [
        (replaced.restore_seq, STORE_RESTORED, "rewound", True),
        (replaced.accept_seq, HISTORY_ACCEPTED, "accepted", True),
    ]
    assert store_rows[1][4] == {"to_seq": replaced.end, "from_seq": replaced.head_said}
    assert _run_cli(["memory", "history", replaced.older], monkeypatch) == 0
    text = capsys.readouterr().out
    assert f"seq {replaced.accept_seq} " in text
    assert f" {HISTORY_ACCEPTED} accepted [mac ok]" in text


def test_an_accepted_row_without_head_after_is_read_at_its_log_end() -> None:
    """`log accept-history` writes its row's payload without a
    ``head_after``: the history it accepted ends at ``log_end_before``."""
    from bettermemory.history import replaced_detail

    payload = {"head_before": {"seq": 9}, "log_end_before": {"seq": 6, "mac": "m"}}
    assert replaced_detail(HISTORY_ACCEPTED, payload) == {"to_seq": 6, "from_seq": 9}
    assert replaced_detail(STORE_RESTORED, payload) == {"from_seq": 9}


def test_operations_carries_the_decision_table() -> None:
    """One table says which command follows which event (the review's
    "history replaced" contract, point 6)."""
    text = " ".join((DOCS / "operations.md").read_text(encoding="utf-8").split())
    assert "### When a store's history was replaced" in text
    for row in (
        "| A `backup restore` finished | Nothing more:",
        "| `key import` put keys back in the ring | `log verify` only:",
        "| The store comes from a backup taken on another machine | "
        "`backup restore BUNDLE --yes`",
        "| A store file was put back or copied by hand, with no bundle | "
        "`log accept-history --yes`",
    ):
        assert row in text, row
