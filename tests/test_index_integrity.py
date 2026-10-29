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
