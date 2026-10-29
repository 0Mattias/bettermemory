"""The tokenizer a store's search index is spelled by (ENG-100).

Each memory row keeps its index text as the tokenizer of the build that
wrote it spelled it (`body_fts`, `scopes_fts`), and meta's
`tokenizer_fingerprint` names that tokenizer. A store written under an
older tokenizer is made here by spelling every token with a prefix and
naming another fingerprint while it is written (`_old_tokenizer`), which
is what a token stream change does to a store's index: the live
tokenizer's queries no longer match its rows. Search is read through
`Store.query_candidates`, the FTS5 prefilter every search over a store
above the prefilter threshold goes through.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory import log as _log, store as store_module, store_index, store_rows
from bettermemory.audit_export import CLASS_CONTROL, export_lines
from bettermemory.search import fts_index_text, tokenizer_fingerprint
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_schema import MIN_WRITER_KEY

#: The fingerprint the older tokenizer's stores carry in meta.
OLD_FINGERPRINT = "0f" * 32

#: The log kind a rebuild appends (`log.INDEX_REBUILT`), spelled here so
#: the module imports where the kind does not exist.
INDEX_REBUILT = "index_rebuilt"

#: One memory per word: the word is in its body and nowhere else.
BODIES = {
    "kubernetes": "the kubernetes ingress controller runs on port 8443",
    "postgres": "the postgres vacuum runs nightly at two",
    "terraform": "the terraform state lives in a versioned bucket",
}


def _old_spelling(text: str) -> str:
    """The older tokenizer's index text: every token the live one gives,
    prefixed, so no live query token matches it."""
    return " ".join(f"old{token}" for token in fts_index_text(text).split())


@contextlib.contextmanager
def _old_tokenizer() -> Iterator[None]:
    """The older build: its fingerprint at create and at open, its
    spelling for every row it writes and for a rebuild."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "tokenizer_fingerprint", lambda: OLD_FINGERPRINT)
        patch.setattr(
            store_index,
            "tokenizer_fingerprint",
            lambda: OLD_FINGERPRINT,
            raising=False,
        )
        patch.setattr(store_rows, "fts_index_text", _old_spelling)
        patch.setattr(store_index, "fts_index_text", _old_spelling, raising=False)
        yield


def _old_store(tmp_path: Path, keys_dir: Path) -> tuple[Path, dict[str, str]]:
    """A store the older build created and wrote `BODIES` to, closed, and
    the memory id each word names."""
    path = tmp_path / "store" / STORE_FILENAME
    ids: dict[str, str] = {}
    with _old_tokenizer():
        with Store.create(path, keys_dir=keys_dir) as store:
            for word, body in BODIES.items():
                ids[word] = store.write(content=body, scopes=["global"]).id
        # The older build's own open finds nothing to do.
        with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
            assert store.meta()["tokenizer_fingerprint"] == OLD_FINGERPRINT
    return path, ids


def _raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _stored(path: Path) -> tuple[str, int]:
    """meta's fingerprint and the count of `index_rebuilt` rows, read
    beside the store."""
    conn = _raw(path)
    try:
        fingerprint = conn.execute(
            "SELECT value FROM meta WHERE key = 'tokenizer_fingerprint'"
        ).fetchone()[0]
        rebuilt = conn.execute(
            "SELECT count(*) FROM log WHERE kind = ?", (INDEX_REBUILT,)
        ).fetchone()[0]
    finally:
        conn.close()
    return str(fingerprint), int(rebuilt)


def _found(store: Store) -> dict[str, list[str]]:
    return {
        word: [memory_id for memory_id, _ in store.query_candidates(word)]
        for word in BODIES
    }


# ---------------------------------------------------------------------------
# The owner's writing open re-spells the index
# ---------------------------------------------------------------------------


def test_index_rebuilt_is_a_control_kind() -> None:
    """Neither a mutation the fold applies nor telemetry."""
    assert _log.INDEX_REBUILT == INDEX_REBUILT
    assert INDEX_REBUILT in _log.CONTROL_KINDS


def test_an_owner_open_respells_an_index_another_tokenizer_spelled(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The tokenizer changed between two builds: the owner's open finds
    meta naming the old one, re-spells every row and the FTS5 table in one
    transaction with one `index_rebuilt` row, and every memory is found by
    the live tokenizer's spelling. `log verify` reads ok."""
    path, ids = _old_store(tmp_path, keys_dir)
    live = tokenizer_fingerprint()
    assert live != OLD_FINGERPRINT
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store) == {word: [ids[word]] for word in BODIES}
        assert store.meta()["tokenizer_fingerprint"] == live
        for word, memory_id in ids.items():
            row = store.conn.execute(
                "SELECT body_fts, scopes_fts FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            assert row["body_fts"] == fts_index_text(BODIES[word])
            assert row["scopes_fts"] == fts_index_text("global")
        rows = [row for row in store.log_rows() if row.kind == INDEX_REBUILT]
        assert len(rows) == 1
        assert rows[0].session is None
        assert json.loads(rows[0].payload) == {
            "reason": "tokenizer",
            "old_fingerprint": OLD_FINGERPRINT,
            "new_fingerprint": live,
            "memories": len(BODIES),
            "respelled": len(BODIES),
        }
        # The last row: nothing else was written with it.
        assert store.log_rows()[-1].seq == rows[0].seq
        report = store.log_verify()
        assert report["status"] == "ok", report["problems"]
        assert report["unaccounted_ids"] == []
        # `log export` carries the row as a control row that verifies.
        exported = [line for line in export_lines(store) if line["seq"] == rows[0].seq]
        assert len(exported) == 1
        assert exported[0]["kind"] == INDEX_REBUILT
        assert exported[0]["class"] == CLASS_CONTROL
        assert exported[0]["verified"] is True
    # The next owner's open has nothing to do.
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store) == {word: [ids[word]] for word in BODIES}
    assert _stored(path) == (live, 1)


def test_the_store_opened_as_every_runtime_entry_point_opens_it_is_respelled(
    tmp_path: Path, keys_dir: Path
) -> None:
    """`Store(directory)` and `open_or_create`, which the daemon, the hooks
    and the CLI open through, are the owner's open."""
    path, ids = _old_store(tmp_path, keys_dir)
    with Store(path.parent, keys_dir=keys_dir) as store:
        assert _found(store) == {word: [ids[word]] for word in BODIES}
    assert _stored(path) == (tokenizer_fingerprint(), 1)


def test_a_rebuild_moves_the_document_frequency_memos_key(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The memo of the FTS hits is keyed on the log's last memory, home
    or `index_rebuilt` row, so hits read before a re-spelling (in this
    process or the daemon's) are read again after it."""
    path, _ = _old_store(tmp_path, keys_dir)
    with Store.open(path, keys_dir=keys_dir) as reader:
        before = reader._memory_head()
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        after = store._memory_head()
        rebuilt = [row for row in store.log_rows() if row.kind == INDEX_REBUILT]
    assert after != before
    assert after == (rebuilt[0].seq, rebuilt[0].mac)


def test_a_store_another_opener_respelled_meanwhile_is_left_alone(
    tmp_path: Path, keys_dir: Path
) -> None:
    """meta is read again under the write lock: two owners' opens that
    both found the old spelling re-spell it once."""
    path, _ = _old_store(tmp_path, keys_dir)
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        again = store_index.rebuild_index(
            store, reason=store_index.REBUILT_FOR_TOKENIZER
        )
        assert again is None
    assert _stored(path) == (tokenizer_fingerprint(), 1)


def test_a_rebuild_that_fails_leaves_the_open_and_the_store_as_they_were(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store the rebuild cannot write (busy past the timeout, full)
    opens anyway and says so; the next writing open tries again."""
    path, ids = _old_store(tmp_path, keys_dir)

    def busy(store: Store, *, reason: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_index, "rebuild_index", busy)
    caplog.set_level(logging.WARNING, logger="bettermemory.store")
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store) == {word: [] for word in BODIES}
        written = store.write(content="written while the index waits", scopes=["g"])
        assert written.id
    assert "re-spelling it failed (database is locked)" in caplog.text
    assert _stored(path) == (OLD_FINGERPRINT, 0)
    monkeypatch.undo()
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store) == {word: [ids[word]] for word in BODIES}
    assert _stored(path) == (tokenizer_fingerprint(), 1)


# ---------------------------------------------------------------------------
# Every other open reports it and searches as it can
# ---------------------------------------------------------------------------


def _read_only_by_versions(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, '999') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MIN_WRITER_KEY,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("how", "why"),
    [
        ("read_only", "it was opened read-only"),
        ("versions", "this build may only read the store"),
        ("not_owner", "not the owner's writing open"),
    ],
)
def test_an_open_that_may_not_write_reports_the_drift_and_rebuilds_nothing(
    tmp_path: Path,
    keys_dir: Path,
    caplog: pytest.LogCaptureFixture,
    how: str,
    why: str,
) -> None:
    """A read-only open (`log verify`, `doctor`, `stats`), a store this
    build may only read (``PRAGMA query_only``, `check_versions`) and an
    open that is not the owner's (``allow_rekey=False``: a daemon's
    worker, `backup verify`'s copy) re-spell nothing. Each says once, in
    a warning naming both tokenizers, and searches the index as it
    stands."""
    path, _ = _old_store(tmp_path, keys_dir)
    if how == "versions":
        _read_only_by_versions(path)
    kwargs = {
        "read_only": {"read_only": True},
        "versions": {"keys_dir": keys_dir, "allow_rekey": True},
        "not_owner": {"keys_dir": keys_dir, "allow_rekey": False},
    }[how]
    caplog.set_level(logging.WARNING, logger="bettermemory.store")
    for _ in range(2):
        with Store.open(path, **kwargs) as store:  # type: ignore[arg-type]
            assert _found(store) == {word: [] for word in BODIES}
    told = [
        r.getMessage() for r in caplog.records if "spelled by tokenizer" in r.message
    ]
    assert len(told) == 1, told
    assert OLD_FINGERPRINT[:16] in told[0]
    assert tokenizer_fingerprint()[:16] in told[0]
    assert why in told[0]
    assert _stored(path) == (OLD_FINGERPRINT, 0)


def test_a_store_whose_key_is_elsewhere_is_not_respelled(
    tmp_path: Path, keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reader's open of a store whose key this machine does not hold:
    no key to sign the row with, so nothing is written."""
    path, _ = _old_store(tmp_path, keys_dir)
    caplog.set_level(logging.WARNING, logger="bettermemory.store")
    with Store.open(path, keys_dir=tmp_path / "other-keys") as store:
        assert not store.has_key
        assert _found(store) == {word: [] for word in BODIES}
    assert _stored(path) == (OLD_FINGERPRINT, 0)
    assert "it was opened without the store's key" in caplog.text


# ---------------------------------------------------------------------------
# The fold reads a tokenizer change as no tampering
# ---------------------------------------------------------------------------


def _edit(path: Path, sql: str, *params: object) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_a_store_another_tokenizer_spelled_verifies_before_and_after_its_rebuild(
    tmp_path: Path, keys_dir: Path
) -> None:
    """A reader's `log verify` (read-only, so no rebuild) of a store an
    older tokenizer spelled reads ok, every memory accounted for: the
    replay spells the index text with this build's tokenizer, and while
    meta names another the fold leaves those columns out. After the
    owner's open re-spells it, `log verify` and a fold read ok again,
    now comparing them."""
    path, ids = _old_store(tmp_path, keys_dir)
    with Store.open(path, read_only=True) as reader:
        report = reader.log_verify()
        assert report["status"] == "ok", report["fold"]["tables"]["memories"]
        assert report["unaccounted_ids"] == []
        assert reader.refold()["status"] == "ok"
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store) == {word: [ids[word]] for word in BODIES}
        assert store.log_verify()["status"] == "ok"
        fold = store.refold()
        assert fold["status"] == "ok"
        assert fold["tables"]["memories"]["rows"] == len(BODIES)
    with Store.open(path, read_only=True) as reader:
        assert reader.log_verify()["status"] == "ok"


def test_an_edit_of_a_rows_index_text_under_this_builds_tokenizer_reads_tampered(
    tmp_path: Path, keys_dir: Path
) -> None:
    """While meta names this build's tokenizer the fold compares the
    index text, so text that no longer spells the row's body (a memory
    hidden from search, or put in front of queries it does not answer)
    names that row unaccounted."""
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as store:
        memory = store.write(content=BODIES["postgres"], scopes=["global"])
    _edit(path, "UPDATE memories SET body_fts = 'nothing here' WHERE id = ?", memory.id)
    with Store.open(path, read_only=True) as reader:
        report = reader.log_verify()
    assert report["status"] == "tampered"
    assert report["unaccounted_ids"] == [memory.id]


def test_under_another_tokenizer_the_fold_leaves_the_index_text_out(
    tmp_path: Path, keys_dir: Path
) -> None:
    """meta naming another tokenizer is the only state in which the fold
    leaves the text out; an edit of the record itself still reads
    tampered, and the owner's next open re-spells the text, so an edit
    of it does not outlast that open."""
    path, ids = _old_store(tmp_path, keys_dir)
    _edit(
        path,
        "UPDATE memories SET body_fts = 'nothing here' WHERE id = ?",
        ids["postgres"],
    )
    with Store.open(path, read_only=True) as reader:
        assert reader.log_verify()["status"] == "ok"
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert _found(store)["postgres"] == [ids["postgres"]]
        assert store.log_verify()["status"] == "ok"
    _edit(
        path,
        "UPDATE memories SET body = 'a body the log never held' WHERE id = ?",
        ids["terraform"],
    )
    with Store.open(path, read_only=True) as reader:
        report = reader.log_verify()
    assert report["status"] == "tampered"
    assert report["unaccounted_ids"] == [ids["terraform"]]
