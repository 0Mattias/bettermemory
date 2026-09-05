"""Content evidence: every store write records the file's hash beside
the index row, a rebuild carries it forward for a file that changed
without a recorded rewrite, and `doctor` names the mismatches.

The 2026-09-01 integrity recon's seventh weak point: no hash, MAC or
chain on memory files, so a file-write attacker changed any body, scope
or trust field unnoticed and `doctor`'s index reconcile was consistency
evidence a reindex cleared. This is the detect-only, single-machine
half: a writer who also rewrites the index defeats it, and SECURITY.md
says so.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bettermemory import index
from bettermemory.doctor import _check_memory_content_evidence
from bettermemory.migrate import _write_repaired, MigrationReport
from bettermemory.store import Store, _atomic_write_post
from bettermemory import _frontmatter as frontmatter


def _row(memory_dir: Path, memory_id: str) -> tuple[str | None, str | None]:
    hashes = index.content_hashes(memory_dir)
    assert hashes is not None
    return hashes[memory_id]


def _file_of(store: Store, memory_id: str) -> Path:
    return next(path for path, memory in store.iter_active() if memory.id == memory_id)


def _hand_edit(path: Path) -> None:
    path.write_text(
        path.read_text(encoding="utf-8").replace("port 8443", "port 9999"),
        encoding="utf-8",
    )


def test_every_store_write_path_anchors_the_file(
    memory_dir: Path, tmp_path: Path
) -> None:
    store = Store(memory_dir)
    memory = store.write(content="the auth service listens on port 8443", scopes=["t"])
    path = _file_of(store, memory.id)
    assert _row(memory_dir, memory.id) == (index.file_sha256(path), "local")

    updated = store.update(
        memory.model_copy(
            update={"body": "the auth service listens on port 8443 now\n"}
        )
    )
    assert _row(memory_dir, updated.id)[0] == index.file_sha256(
        _file_of(store, memory.id)
    )

    attested = tmp_path / "present.toml"
    attested.write_text("x\n", encoding="utf-8")
    store.mark_verified(memory.id, verified_paths=[str(attested)])
    assert _row(memory_dir, memory.id)[0] == index.file_sha256(
        _file_of(store, memory.id)
    )

    store.record_corroboration(memory.id)
    assert _row(memory_dir, memory.id)[0] == index.file_sha256(
        _file_of(store, memory.id)
    )

    store.rename_scope("t", "tools")
    assert _row(memory_dir, memory.id)[0] == index.file_sha256(
        _file_of(store, memory.id)
    )

    store.tombstone(memory.id, "round trip")
    restored = store.restore(memory.id)
    assert _row(memory_dir, restored.id) == (
        index.file_sha256(_file_of(store, memory.id)),
        "local",
    )


def test_a_hand_edit_is_named_and_a_store_write_re_anchors_it(memory_dir: Path) -> None:
    store = Store(memory_dir)
    memory = store.write(content="the auth service listens on port 8443", scopes=["t"])
    store.write(content="an untouched neighbour about deploys", scopes=["t"])
    clean = _check_memory_content_evidence(memory_dir)
    assert clean.status == "ok"
    assert clean.details["changed"] == [] and clean.details["checked"] == 2

    _hand_edit(_file_of(store, memory.id))
    diag = _check_memory_content_evidence(memory_dir)
    assert diag.status == "warn"
    assert memory.id in diag.message
    assert [row["id"] for row in diag.details["changed"]] == [memory.id]
    assert diag.fix_hint is not None and "memory_verify" in diag.fix_hint
    assert "reindex` does not clear" in diag.fix_hint

    # The owner recognises the change: a verify rewrites the file through
    # the store and re-anchors it.
    store.mark_verified(memory.id)
    assert _check_memory_content_evidence(memory_dir).status == "ok"


def test_the_evidence_survives_a_rebuild(memory_dir: Path) -> None:
    store = Store(memory_dir)
    memory = store.write(content="the auth service listens on port 8443", scopes=["t"])
    recorded = _row(memory_dir, memory.id)[0]
    path = _file_of(store, memory.id)
    _hand_edit(path)
    changed = index.file_sha256(path)
    assert changed != recorded

    index.rebuild(memory_dir, store.iter_active())
    assert _row(memory_dir, memory.id)[0] == recorded, (
        "the rebuild kept the recorded hash"
    )
    assert _check_memory_content_evidence(memory_dir).status == "warn"

    # ... and through a tokenizer drop, via the stash.
    db_path = index.index_path(memory_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meta SET value = 'stale' WHERE key = 'tokenizer_fingerprint'"
        )
    assert index.status(memory_dir)["needs_rebuild"] is True
    with sqlite3.connect(db_path) as conn:
        stash = conn.execute(
            "SELECT value FROM meta WHERE key = 'content_sha_carry'"
        ).fetchone()
    assert stash is not None and recorded in stash[0]
    index.rebuild(memory_dir, store.iter_active())
    assert _row(memory_dir, memory.id)[0] == recorded
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = 'content_sha_carry'"
            ).fetchone()
            is None
        )


def test_carried_hash_rule() -> None:
    assert index.carried_content_sha256("local", "aa", "bb") == "aa"
    assert index.carried_content_sha256("unaccounted", "aa", "bb") == "aa"
    assert index.carried_content_sha256("local", "aa", "aa") == "aa"
    assert index.carried_content_sha256("local", None, "bb") == "bb"
    # A pull is the recorded path by which a synced file's bytes changed.
    assert index.carried_content_sha256("synced", "aa", "bb") == "bb"


def test_a_migration_rewrite_is_the_stores_own_write(memory_dir: Path) -> None:
    store = Store(memory_dir)
    memory = store.write(content="the auth service listens on port 8443", scopes=["t"])
    path = _file_of(store, memory.id)
    post = frontmatter.load(path)
    post.metadata["scopes"] = ["tools"]
    report = MigrationReport(memory_dir=memory_dir, inferred=None, dry_run=False)
    assert _write_repaired(path, post, report) is True
    assert _row(memory_dir, memory.id)[0] == index.file_sha256(path)
    assert _check_memory_content_evidence(memory_dir).status == "ok"


def test_unanchored_rows_are_counted_not_judged(memory_dir: Path) -> None:
    store = Store(memory_dir)
    memory = store.write(content="the auth service listens on port 8443", scopes=["t"])
    with sqlite3.connect(index.index_path(memory_dir)) as conn:
        conn.execute(
            "UPDATE memories SET content_sha256 = NULL WHERE id = ?", (memory.id,)
        )
    diag = _check_memory_content_evidence(memory_dir)
    assert diag.status == "ok"
    assert diag.details["unanchored"] == 1 and diag.details["changed"] == []
    assert diag.fix_hint is not None and "reindex" in diag.fix_hint
    index.rebuild(memory_dir, store.iter_active())
    assert _check_memory_content_evidence(memory_dir).details["unanchored"] == 0


def test_no_index_reads_ok_with_null_evidence(tmp_path: Path) -> None:
    empty = tmp_path / "never-indexed"
    empty.mkdir()
    diag = _check_memory_content_evidence(empty)
    assert diag.status == "ok" and diag.details["changed"] is None


def test_atomic_write_post_returns_the_hash_of_what_it_wrote(tmp_path: Path) -> None:
    post = frontmatter.Post("a body\n")
    post.metadata = {"id": "01J0000000000000000000000X"}
    target = tmp_path / "x.md"
    sha = _atomic_write_post(target, post)
    assert sha == index.file_sha256(target)
