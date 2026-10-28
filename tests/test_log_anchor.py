"""External chain anchors: `bettermemory log anchor` and
`bettermemory log verify --anchor FILE`.

The head checkpoint sits beside the key, so a process that holds the key
can rewrite a past row, recompute every MAC after it, rewrite the head,
and plain `log verify` reads ok. An anchor kept off the host pins the log
up to its seq: these tests simulate that key holder and pin that the
anchor catches the rewrite where plain verify cannot, that a deleted tail
with a rewound head is caught too, that the digest is the one a shell can
recompute, and that `log verify` without --anchor is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import log as chain
from bettermemory.anchor import (
    ANCHOR_FORMAT,
    ANCHOR_MISMATCH,
    Anchor,
    AnchorFileError,
    chain_digests,
    check_anchors,
    load_anchors,
    merge_into_report,
    parse_anchor,
    take,
)
from bettermemory.cli.log_cmd import render_report
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import Store


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
def cli_store(memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The test's store, resolved by the CLI through BETTERMEMORY_DIR,
    with a memory and a run of telemetry rows after it."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    store.put_memory(_memory("alpha"))
    for n in range(5):
        store.record_event("show", session="sess_a", id=f"m{n}", actor={"client": "a"})
    return store


def _expected_digest(store: Store, seq: int) -> str:
    """The digest as the module docstring defines it, computed here from
    the log's rows without the module's code."""
    macs = [row.mac for row in store.log_rows() if row.seq <= seq]
    return hashlib.sha256("".join(f"{mac}\n" for mac in macs).encode()).hexdigest()


def _rewrite_as_key_holder(store: Store, seq: int, payload: str) -> None:
    """What a process holding the key can do: change the payload at
    ``seq``, recompute the MAC of that row and every row after it with
    the real key, and move the head checkpoint to the new last MAC. Plain
    `log verify` cannot tell. The rows rewritten here are telemetry, so
    the fold has nothing to notice either."""
    key = store.keyring.load_current()
    assert key is not None
    conn = sqlite3.connect(str(store.path))
    try:
        prev = str(
            conn.execute("SELECT mac FROM log WHERE seq = ?", (seq - 1,)).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT seq, ts, session, kind, payload FROM log WHERE seq >= ? "
            "ORDER BY seq",
            (seq,),
        ).fetchall()
        for row_seq, ts, session, kind, text in rows:
            if row_seq == seq:
                text = payload
            mac = chain.compute_mac(
                key,
                seq=row_seq,
                ts=ts,
                session=session,
                kind=kind,
                payload=text,
                prev_mac=prev,
            )
            conn.execute(
                "UPDATE log SET payload = ?, prev_mac = ?, mac = ? WHERE seq = ?",
                (text, prev, mac, row_seq),
            )
            prev = mac
        conn.commit()
        last = int(conn.execute("SELECT MAX(seq) FROM log").fetchone()[0])
    finally:
        conn.close()
    _rewind_head(store, last, prev)


def _rewind_head(store: Store, seq: int, mac: str) -> None:
    """Write the head checkpoint directly: `KeyRing.write_head` only moves
    forward, and the key holder is not bound by it."""
    store.keyring.head_path().write_text(
        json.dumps({"seq": seq, "mac": mac, "ts": "2026-10-28T00:00:00Z"}),
        encoding="utf-8",
    )


def _anchor_at(store: Store, seq: int) -> Anchor:
    """An anchor as `take` would have written it when ``seq`` was the
    log's last row."""
    row = next(r for r in store.log_rows() if r.seq == seq)
    return Anchor(
        store_id=store.store_id,
        seq=seq,
        mac=row.mac,
        chain_sha256=_expected_digest(store, seq),
        created_at="2026-10-28T00:00:00Z",
    )


def _anchor_file(tmp_path: Path, anchor: Anchor, name: str = "anchor.json") -> Path:
    path = tmp_path / name
    path.write_text(anchor.to_line(), encoding="utf-8")
    return path


def _verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *anchors: Path,
) -> tuple[int, dict[str, Any]]:
    argv = ["log", "verify", "--json"]
    for path in anchors:
        argv += ["--anchor", str(path)]
    code = _run_cli(argv, monkeypatch)
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Taking an anchor
# ---------------------------------------------------------------------------


def test_an_anchor_pins_the_last_row_and_the_chain_below_it(cli_store: Store) -> None:
    anchor = take(cli_store)
    last = cli_store.log_rows()[-1]
    assert anchor.store_id == cli_store.store_id
    assert (anchor.seq, anchor.mac) == (last.seq, last.mac)
    assert anchor.chain_sha256 == _expected_digest(cli_store, last.seq)
    assert anchor.created_at.endswith("Z")
    as_dict = anchor.to_dict()
    assert as_dict["format"] == ANCHOR_FORMAT
    assert set(as_dict) == {
        "format",
        "store_id",
        "seq",
        "mac",
        "chain_sha256",
        "created_at",
    }
    assert parse_anchor(json.loads(anchor.to_line()), where="x") == anchor


def test_chain_digests_read_every_prefix_in_one_pass(cli_store: Store) -> None:
    last = cli_store.log_rows()[-1].seq
    seqs = [1, 3, last, last + 4]
    digests = chain_digests(cli_store.conn, seqs)
    for seq in seqs:
        assert digests[seq] == _expected_digest(cli_store, seq)
    assert chain_digests(cli_store.conn, []) == {}


def test_cli_anchor_prints_one_json_line(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["log", "anchor"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    anchor = parse_anchor(json.loads(out), where="stdout")
    assert anchor.seq == cli_store.log_rows()[-1].seq
    # The command reads; it appends nothing to the log it anchors.
    assert cli_store.log_rows()[-1].seq == anchor.seq


def test_cli_anchor_out_is_owner_only(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "anchors" / "store.anchor.json"
    out.parent.mkdir()
    assert _run_cli(["log", "anchor", "--out", str(out)], monkeypatch) == 0
    assert "keep it off this host" in capsys.readouterr().out
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    (anchor,) = load_anchors(out)
    assert anchor.seq == cli_store.log_rows()[-1].seq

    assert _run_cli(["log", "anchor", "--out", str(out), "--json"], monkeypatch) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["out"] == str(out)
    assert printed["anchor"]["seq"] == anchor.seq


def test_cli_anchor_refuses_an_empty_log_and_a_missing_directory(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["log", "anchor", "--out", str(tmp_path / "nowhere" / "a.json")]
    assert _run_cli(argv, monkeypatch) == 2
    assert "no directory" in capsys.readouterr().err
    outside = sqlite3.connect(str(cli_store.path))
    outside.execute("DELETE FROM log")
    outside.commit()
    outside.close()
    assert _run_cli(["log", "anchor"], monkeypatch) == 2
    assert "holds no rows" in capsys.readouterr().err


def test_cli_anchor_refuses_a_directory_without_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["log", "anchor"], monkeypatch) == 2
    assert "no bettermemory 9 store" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Checking anchors
# ---------------------------------------------------------------------------


def test_a_fresh_anchor_verifies(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    anchor = take(cli_store)
    # Rows appended after the anchor are not its business.
    cli_store.record_event("show", session="sess_a", id="later")
    path = _anchor_file(tmp_path, anchor)
    code, report = _verify(monkeypatch, capsys, path)
    assert code == 0
    assert report["status"] == "ok"
    assert report["anchors"] == [
        {
            "file": str(path),
            "store_id": cli_store.store_id,
            "seq": anchor.seq,
            "created_at": anchor.created_at,
            "status": "ok",
        }
    ]
    assert _run_cli(["log", "verify", "--anchor", str(path)], monkeypatch) == 0
    text = capsys.readouterr().out
    assert "  anchors 1" in text
    assert f"    seq {anchor.seq}: ok ({path})" in text


def test_a_key_holders_rewrite_passes_plain_verify_and_fails_the_anchor(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = cli_store.log_rows()
    target = rows[-3]
    assert target.kind == "show"
    below = _anchor_file(tmp_path, _anchor_at(cli_store, target.seq - 1), "below.json")
    at_head = _anchor_file(tmp_path, take(cli_store), "head.json")

    # Rewrite who read the record: the actor of an earlier `show` row.
    forged = json.loads(target.payload)
    forged["actor"] = {"client": "someone-else"}
    _rewrite_as_key_holder(cli_store, target.seq, chain.canonical_payload(forged))

    code, report = _verify(monkeypatch, capsys)
    assert code == 0, report["problems"]
    assert report["status"] == "ok"
    assert "anchors" not in report

    code, report = _verify(monkeypatch, capsys, below, at_head)
    assert code == 1
    assert report["status"] == "tampered"
    statuses = [(a["seq"], a["status"], a.get("reason")) for a in report["anchors"]]
    assert statuses == [
        (target.seq - 1, "ok", None),
        (rows[-1].seq, "mismatch", "mac"),
    ]
    assert {
        "seq": rows[-1].seq,
        "kind": "anchor",
        "problem": ANCHOR_MISMATCH,
        "reason": "mac",
        "anchor": str(at_head),
    } in report["problems"]

    assert _run_cli(["log", "verify", "--anchor", str(at_head)], monkeypatch) == 1
    text = capsys.readouterr().out
    assert "bettermemory log verify: tampered" in text
    assert f"seq {rows[-1].seq}: mismatch (mac)" in text
    assert f"seq {rows[-1].seq} (anchor): {ANCHOR_MISMATCH}" in text


def test_a_rewrite_above_the_newest_anchor_is_not_pinned(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented limit: rows after the newest anchor are pinned by
    the key alone."""
    rows = cli_store.log_rows()
    anchor = _anchor_file(tmp_path, _anchor_at(cli_store, rows[-3].seq))
    forged = json.loads(rows[-1].payload)
    forged["id"] = "forged"
    _rewrite_as_key_holder(cli_store, rows[-1].seq, chain.canonical_payload(forged))
    code, report = _verify(monkeypatch, capsys, anchor)
    assert (code, report["status"]) == (0, "ok")


def test_a_deleted_tail_with_a_rewound_head_reads_missing(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    anchor = _anchor_file(tmp_path, take(cli_store))
    rows = cli_store.log_rows()
    keep = rows[-3]
    outside = sqlite3.connect(str(cli_store.path))
    outside.execute("DELETE FROM log WHERE seq > ?", (keep.seq,))
    outside.commit()
    outside.close()
    _rewind_head(cli_store, keep.seq, keep.mac)

    code, report = _verify(monkeypatch, capsys)
    assert (code, report["status"]) == (0, "ok")
    code, report = _verify(monkeypatch, capsys, anchor)
    assert code == 1
    assert report["status"] == "tampered"
    assert report["anchors"][0]["status"] == "mismatch"
    assert report["anchors"][0]["reason"] == "missing"


def test_a_lower_mac_changed_under_a_kept_anchor_row_reads_digest(
    cli_store: Store, tmp_path: Path
) -> None:
    anchor = take(cli_store)
    outside = sqlite3.connect(str(cli_store.path))
    outside.execute("UPDATE log SET mac = ? WHERE seq = 2", ("ab" * 32,))
    outside.commit()
    outside.close()
    (result,) = check_anchors(
        cli_store.conn, cli_store.store_id, [("anchor.json", anchor)]
    )
    assert (result["status"], result["reason"]) == ("mismatch", "digest")


def test_a_non_ascii_mac_at_the_anchored_row_reads_mac(cli_store: Store) -> None:
    """A hand-edited MAC column with a character `hmac.compare_digest`
    refuses as text is a mismatch, not an error."""
    anchor = take(cli_store)
    outside = sqlite3.connect(str(cli_store.path))
    outside.execute("UPDATE log SET mac = ? WHERE seq = ?", ("é" * 64, anchor.seq))
    outside.commit()
    outside.close()
    (result,) = check_anchors(
        cli_store.conn, cli_store.store_id, [("anchor.json", anchor)]
    )
    assert (result["status"], result["reason"]) == ("mismatch", "mac")


def test_another_stores_anchor_is_listed_and_changes_nothing(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mine = take(cli_store)
    theirs = Anchor(
        store_id="f" * 32,
        seq=10_000,
        mac="cd" * 32,
        chain_sha256="ef" * 32,
        created_at="2026-10-28T00:00:00Z",
    )
    path = tmp_path / "anchors.jsonl"
    path.write_text(theirs.to_line() + "\n" + mine.to_line(), encoding="utf-8")
    assert [a.store_id for a in load_anchors(path)] == [theirs.store_id, mine.store_id]
    code, report = _verify(monkeypatch, capsys, path)
    assert code == 0
    assert [a["status"] for a in report["anchors"]] == ["other_store", "ok"]
    assert report["problems"] == []


def test_anchors_need_no_key(
    cli_store: Store,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A copy on a machine without the key can still be held to its
    anchors: the report is unverifiable for the key, and the anchor ok."""
    anchor = _anchor_file(tmp_path, take(cli_store))
    (keys_dir / f"{cli_store.store_id}.key").unlink()
    code, report = _verify(monkeypatch, capsys, anchor)
    assert code == 1
    assert report["status"] == "unverifiable"
    assert report["anchors"][0]["status"] == "ok"
    assert not (keys_dir / f"{cli_store.store_id}.key").exists()


def test_merge_keeps_problems_sorted_and_status_on_a_clean_check() -> None:
    report: dict[str, Any] = {
        "status": "unverifiable",
        "problems": [{"seq": 9, "kind": "show", "problem": "mac_mismatch"}],
    }
    merge_into_report(
        report,
        [
            {"file": "a", "store_id": "s", "seq": 3, "created_at": "", "status": "ok"},
        ],
    )
    assert report["status"] == "unverifiable"
    assert [p["seq"] for p in report["problems"]] == [9]
    merge_into_report(
        report,
        [
            {
                "file": "a",
                "store_id": "s",
                "seq": 3,
                "created_at": "",
                "status": "mismatch",
                "reason": "mac",
            }
        ],
    )
    assert report["status"] == "tampered"
    assert [p["seq"] for p in report["problems"]] == [3, 9]


# ---------------------------------------------------------------------------
# Anchor files
# ---------------------------------------------------------------------------


def _good() -> dict[str, Any]:
    return {
        "format": ANCHOR_FORMAT,
        "store_id": "s" * 32,
        "seq": 3,
        "mac": "ab" * 32,
        "chain_sha256": "cd" * 32,
        "created_at": "2026-10-28T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "bettermemory.anchor/2"}, "not a bettermemory.anchor/1 anchor"),
        ({"store_id": ""}, "store_id"),
        ({"seq": 0}, "seq"),
        ({"seq": True}, "seq"),
        ({"seq": "3"}, "seq"),
        ({"seq": 2**63}, "seq"),
        ({"mac": "AB" * 32}, "mac"),
        ({"mac": "ab"}, "mac"),
        ({"chain_sha256": None}, "chain_sha256"),
        ({"created_at": 5}, "created_at"),
    ],
)
def test_a_malformed_anchor_names_the_field(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(AnchorFileError, match=message):
        parse_anchor({**_good(), **change}, where="f.json")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "holds no anchor"),
        ("\n\n", "holds no anchor"),
        ("not json", "f.json:1: not JSON"),
        ("[]", "an anchor is a JSON object"),
        (json.dumps(_good()) + "\n{broken", "f.json:2: not JSON"),
    ],
)
def test_an_unreadable_anchor_file_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "f.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(AnchorFileError, match=message):
        load_anchors(path)


def test_cli_verify_refuses_a_bad_anchor_file_before_reading_the_store(
    cli_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.json"
    assert _run_cli(["log", "verify", "--anchor", str(missing)], monkeypatch) == 2
    assert "cannot read an anchor file" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**_good(), "seq": -1}), encoding="utf-8")
    assert _run_cli(["log", "verify", "--anchor", str(bad)], monkeypatch) == 2
    assert "seq must be" in capsys.readouterr().err


def test_a_pretty_printed_anchor_file_is_one_anchor(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(json.dumps(_good(), indent=2), encoding="utf-8")
    (anchor,) = load_anchors(path)
    assert anchor.seq == 3


def test_an_anchor_file_saved_with_a_byte_order_mark_reads(tmp_path: Path) -> None:
    """What a Windows editor or PowerShell's UTF-8 writer saves: the
    anchors behind a byte-order mark, one or several."""
    one = tmp_path / "one.json"
    one.write_text(json.dumps(_good()), encoding="utf-8-sig")
    assert [a.seq for a in load_anchors(one)] == [3]
    many = tmp_path / "many.jsonl"
    many.write_text(
        json.dumps(_good()) + "\r\n" + json.dumps({**_good(), "seq": 4}) + "\r\n",
        encoding="utf-8-sig",
    )
    assert [a.seq for a in load_anchors(many)] == [3, 4]


# ---------------------------------------------------------------------------
# Without --anchor, verify is unchanged
# ---------------------------------------------------------------------------


def test_verify_without_anchors_is_unchanged(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = cli_store.log_verify()
    code, report = _verify(monkeypatch, capsys)
    assert code == 0
    assert report == json.loads(json.dumps(expected))
    assert "anchors" not in report
    assert _run_cli(["log", "verify"], monkeypatch) == 0
    text = capsys.readouterr().out
    assert text == render_report(expected)
    assert "\n  anchors " not in text
