"""The store's search index: its repair (ENG-100).

An index drift is made here the way SQLite leaves one when the FTS5
table and the memories table it indexes stop agreeing (a write that
bypassed the triggers, a torn page in the shadow tables): a memory's
entries taken out of the index (FTS5's ``'delete'`` command, which the
content table never sees), or an entry put in for text no row carries.
Search is read through `Store.query_candidates`, the FTS5 prefilter.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bettermemory import doctor
from bettermemory.backup import create_backup, manifest_mac, verify_bundle
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_schema import MIN_WRITER_KEY

BODIES = {
    "kubernetes": "the kubernetes ingress controller runs on port 8443",
    "postgres": "the postgres vacuum runs nightly at two",
    "terraform": "the terraform state lives in a versioned bucket",
}


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store of `BODIES` that the CLI resolves, its key under the test's
    keys directory."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    with Store.create(directory / STORE_FILENAME, keys_dir=keys_dir) as store:
        for body in BODIES.values():
            store.write(content=body, scopes=["global"])
    return directory / STORE_FILENAME


def _ids(path: Path, keys_dir: Path) -> dict[str, str]:
    """The memory id each word of `BODIES` names."""
    with Store.open(path, keys_dir=keys_dir) as store:
        rows = store.conn.execute("SELECT id, body FROM memories").fetchall()
    return {
        word: next(str(row["id"]) for row in rows if word in str(row["body"]))
        for word in BODIES
    }


def _found(path: Path, keys_dir: Path, word: str) -> list[str]:
    with Store.open(path, keys_dir=keys_dir) as store:
        return [memory_id for memory_id, _ in store.query_candidates(word)]


def _drop_from_index(path: Path, memory_id: str) -> None:
    """Take a memory's entries out of the FTS5 index and leave its row."""
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT rowid, body_fts, scopes_fts FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts) "
            "VALUES ('delete', ?, ?, ?)",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def _stale_entry(path: Path, memory_id: str, text: str) -> None:
    """Index `text` under a memory whose row does not carry it."""
    conn = sqlite3.connect(path)
    try:
        rowid = conn.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, ?, '')",
            (rowid, text),
        )
        conn.commit()
    finally:
        conn.close()


def _run_cli(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    out, err = capsys.readouterr()
    return int(exc.value.code or 0), out, err


def _rebuilt_rows(path: Path, keys_dir: Path) -> list[dict[str, object]]:
    with Store.open(path, keys_dir=keys_dir) as store:
        return [
            json.loads(row.payload)
            for row in store.log_rows()
            if row.kind == "index_rebuilt"
        ]


# ---------------------------------------------------------------------------
# bettermemory index rebuild
# ---------------------------------------------------------------------------


def test_index_rebuild_finds_a_memory_the_index_lost_again(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory whose entries left the index is in the store and in no
    search; `bettermemory index rebuild` rebuilds the index from the rows,
    the memory is found again, one `index_rebuilt` row says so, and
    `log verify` reads ok."""
    ids = _ids(store_file, keys_dir)
    _drop_from_index(store_file, ids["postgres"])
    assert _found(store_file, keys_dir, "postgres") == []
    code, out, err = _run_cli(["index", "rebuild"], monkeypatch, capsys)
    assert code == 0, err
    assert out.startswith("bettermemory index rebuild: rebuilt\n")
    assert f"  store {store_file}" in out
    assert "  3 memories, 0 of them re-spelled from their bodies" in out
    for word, memory_id in ids.items():
        assert _found(store_file, keys_dir, word) == [memory_id]
    rows = _rebuilt_rows(store_file, keys_dir)
    assert len(rows) == 1
    assert rows[0]["reason"] == "repair"
    assert rows[0]["memories"] == 3
    assert rows[0]["respelled"] == 0
    with Store.open(store_file, read_only=True) as reader:
        assert reader.log_verify()["status"] == "ok"


def test_index_rebuild_drops_an_entry_for_text_no_row_carries(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale entry puts a memory before a query its body does not
    answer; the rebuild indexes only what the rows carry."""
    ids = _ids(store_file, keys_dir)
    _stale_entry(store_file, ids["terraform"], "zeppelin")
    assert _found(store_file, keys_dir, "zeppelin") == [ids["terraform"]]
    code, _, err = _run_cli(["index", "rebuild"], monkeypatch, capsys)
    assert code == 0, err
    assert _found(store_file, keys_dir, "zeppelin") == []
    assert _found(store_file, keys_dir, "terraform") == [ids["terraform"]]


def test_index_rebuild_json_names_what_it_did(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run_cli(["index", "rebuild", "--json"], monkeypatch, capsys)
    assert code == 0
    report = json.loads(out)
    assert set(report) == {
        "status",
        "path",
        "reason",
        "memories",
        "respelled",
        "old_fingerprint",
        "new_fingerprint",
        "seq",
        "seconds",
    }
    assert report["status"] == "rebuilt"
    assert report["reason"] == "repair"
    assert report["memories"] == 3
    assert report["old_fingerprint"] == report["new_fingerprint"]


def test_index_rebuild_refuses_a_store_it_may_only_read(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store a later build's writer minimum keeps this build from
    writing: exit 1, the reason on stderr, nothing written."""
    conn = sqlite3.connect(store_file)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, '999') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (MIN_WRITER_KEY,),
    )
    conn.commit()
    conn.close()
    code, out, err = _run_cli(["index", "rebuild"], monkeypatch, capsys)
    assert code == 1
    assert out == ""
    assert "cannot be rebuilt here: this build may only read the store" in err
    assert _rebuilt_rows(store_file, keys_dir) == []


def test_index_without_a_subcommand_prints_its_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run_cli(["index"], monkeypatch, capsys)
    assert code == 2
    assert "rebuild" in out


# ---------------------------------------------------------------------------
# doctor's index check and backup verify
# ---------------------------------------------------------------------------


def _damage_a_page(path: Path, index: str) -> None:
    """Overwrite the header of `index`'s root page: a torn page in an
    index the reads at hand never use, which only a check of every page
    finds. The store is closed, so the page is in the file."""
    conn = sqlite3.connect(path)
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        root = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = ?", (index,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    with path.open("r+b") as raw:
        raw.seek((root - 1) * page_size)
        raw.write(b"\xff" * 16)


def _check(path: Path) -> doctor.Check:
    return doctor.check_index(doctor._Context(cwd=None, directory=path.parent))


def test_doctor_reads_a_whole_store_ok(store_file: Path) -> None:
    check = _check(store_file)
    assert check.id == "index"
    assert check.status == "ok", check
    assert check.remedy is None
    assert check.details["quick_check"] == "ok"
    assert check.details["fts_integrity"] == "ok"
    assert check.details["memories"] == len(BODIES)
    assert (
        check.details["stored_tokenizer_fingerprint"]
        == (check.details["tokenizer_fingerprint"])
    )


def test_doctor_names_the_rebuild_for_an_index_drift(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory the index lost is a warning naming `index rebuild`, which
    clears it; doctor itself writes nothing."""
    _drop_from_index(store_file, _ids(store_file, keys_dir)["kubernetes"])
    before = store_file.read_bytes()
    check = _check(store_file)
    assert store_file.read_bytes() == before
    assert check.status == "warn"
    assert "search index does not match its memories" in check.summary
    assert "FTS5 integrity-check" in check.summary
    assert check.remedy is not None
    assert "bettermemory index rebuild" in check.remedy
    assert check.details["quick_check"] == "ok"
    assert check.details["fts_integrity"] != "ok"
    code, _, _ = _run_cli(["index", "rebuild"], monkeypatch, capsys)
    assert code == 0
    assert _check(store_file).status == "ok"


def test_doctor_fails_a_damaged_page_and_names_the_restore(store_file: Path) -> None:
    _damage_a_page(store_file, "memories_by_updated")
    check = _check(store_file)
    assert check.status == "fail"
    assert "has a damaged page (SQLite's quick_check: " in check.summary
    assert check.remedy is not None
    assert "bettermemory backup restore" in check.remedy
    assert isinstance(check.details["quick_check"], list)
    assert any("memories_by_updated" in m for m in check.details["quick_check"])


def test_doctor_warns_an_index_another_tokenizer_spelled(
    tmp_path: Path, keys_dir: Path
) -> None:
    from .test_store_tokenizer import OLD_FINGERPRINT, _old_store

    path, _ = _old_store(tmp_path, keys_dir)
    check = _check(path)
    assert check.status == "warn"
    assert f"spelled by tokenizer {OLD_FINGERPRINT[:16]}" in check.summary
    assert check.remedy is not None
    assert "bettermemory index rebuild" in check.remedy
    assert check.details["stored_tokenizer_fingerprint"] == OLD_FINGERPRINT


def test_backup_verify_reports_an_index_drift_and_its_repair(
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bundle taken of a store whose index drifted verifies (its records
    are whole) and says so, with the repair for the restored store."""
    _drop_from_index(store_file, _ids(store_file, keys_dir)["postgres"])
    bundle = Path(create_backup(store_file)["bundle"])
    report = verify_bundle(bundle)
    assert report["status"] == "ok"
    drifts = [p for p in report["problems"] if p["problem"] == "index_drift"]
    assert len(drifts) == 1
    assert "bettermemory index rebuild" in str(drifts[0]["repair"])
    code, out, _ = _run_cli(["backup", "verify", str(bundle)], monkeypatch, capsys)
    assert code == 0
    assert "index_drift: detail " in out


def test_backup_verify_reads_a_damaged_page_as_an_unreadable_snapshot(
    store_file: Path, keys_dir: Path
) -> None:
    """A snapshot with a page the verification's own reads never reach
    (`log_by_ts`, the index `stats` reads its window through), in a
    bundle whose manifest names it as it is: quick_check finds it, and the
    bundle reads `mismatched` as one whose replay met such a page does, so
    `backup restore` refuses it. Before the check this bundle read ok."""
    from .test_backup import _key, _manifest, _rehash, _write_manifest

    bundle = Path(create_backup(store_file)["bundle"])
    assert verify_bundle(bundle)["status"] == "ok"
    _damage_a_page(bundle / STORE_FILENAME, "log_by_ts")
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    manifest["mac"] = manifest_mac(_key(keys_dir, manifest["store_id"]), manifest)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["manifest_mac"] == "ok", report["problems"]
    assert report["log"]["status"] == "ok"
    assert report["status"] == "mismatched"
    unreadable = [
        p for p in report["problems"] if p["problem"] == "snapshot_unreadable"
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["check"] == "quick_check"
    assert "log_by_ts" in str(unreadable[0]["detail"])
