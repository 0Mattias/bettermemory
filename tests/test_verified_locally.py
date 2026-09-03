"""`verified_locally_at` (schema v8): when this host last stamped a memory
through its own verify path, carried in the index because the file's
own `last_verified_at` cannot say who wrote it.

The column is set by the Store's verify upsert, cleared by `sync pull`
for the files it lands, carried across rebuilds and across the drop a
schema or tokenizer bump performs, and derived at rebuild from `verify`
and `sync_pull` events. Each rule has a case here that fails when the
rule is removed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bettermemory import index, provenance
from bettermemory.events import Recorder
from bettermemory.store import Store
from bettermemory.time_utils import parse_event_ts


def _filename(store: Store, memory_id: str) -> str:
    return next(p.name for p in store.root.glob("*.md") if memory_id.lower() in p.name)


def _rebuild(store: Store) -> int:
    return index.rebuild(store.root, store.iter_active())


def _drop_events(memory_dir: Path) -> None:
    for path in memory_dir.glob(".events*"):
        path.unlink()


STAMP = "2026-09-01T12:00:00+00:00"
LATER = "2026-09-02T12:00:00+00:00"


# ---------------------------------------------------------------------------
# The column and the upsert
# ---------------------------------------------------------------------------


def test_upsert_stamps_the_local_verification_and_none_preserves_it(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(content="a memory to verify locally", scopes=["tools"])
    name = _filename(store, memory.id)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at is None
    )

    index.upsert(memory_dir, memory, filename=name, verified_locally_at=STAMP)
    row = index.trust_for(memory_dir, [memory.id])[memory.id]
    assert row == index.TrustRow("local", STAMP)

    # None is "no claim": the row keeps its stamp.
    index.upsert(memory_dir, memory, filename=name)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at == STAMP
    )
    # A value replaces it.
    index.upsert(memory_dir, memory, filename=name, verified_locally_at=LATER)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at == LATER
    )


def test_trust_for_omits_unclassified_rows_and_degrades_to_empty(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    memory = store.write(content="a classified memory", scopes=["tools"])
    assert set(
        index.trust_for(memory_dir, [memory.id, "01ZZZZZZZZZZZZZZZZZZZZZZZZ"])
    ) == {memory.id}
    assert index.trust_for(memory_dir, []) == {}
    assert index.trust_for(tmp_path / "no-store", [memory.id]) == {}


def test_clear_local_verification_nulls_only_the_named_rows(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    first = store.write(content="first memory about kubernetes", scopes=["tools"])
    second = store.write(content="second memory about terraform", scopes=["tools"])
    first_name, second_name = _filename(store, first.id), _filename(store, second.id)
    index.upsert(memory_dir, first, filename=first_name, verified_locally_at=STAMP)
    index.upsert(memory_dir, second, filename=second_name, verified_locally_at=STAMP)

    assert index.clear_local_verification(memory_dir, [first_name, "not-there.md"]) == 1
    rows = index.trust_for(memory_dir, [first.id, second.id])
    assert rows[first.id].verified_locally_at is None
    assert rows[second.id].verified_locally_at == STAMP
    # Idempotent, and quiet where there is nothing to clear.
    assert index.clear_local_verification(memory_dir, [first_name]) == 0
    assert index.clear_local_verification(memory_dir, []) == 0
    assert index.clear_local_verification(tmp_path / "no-store", [first_name]) == 0


def test_links_for_with_status_returns_the_local_stamp(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(content="a memory with a local stamp", scopes=["tools"])
    index.upsert(
        memory_dir,
        memory,
        filename=_filename(store, memory.id),
        verified_locally_at=STAMP,
    )
    *_, label, stamp = index.links_for_with_status(memory_dir, memory.id)
    assert (label, stamp) == ("local", STAMP)
    *_, label, stamp = index.links_for_with_status(
        memory_dir, "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    )
    assert (label, stamp) == (None, None)


# ---------------------------------------------------------------------------
# The rebuild: carry, the pull that clears, the verify that re-establishes
# ---------------------------------------------------------------------------


def test_rebuild_carries_the_stamp_when_no_event_speaks(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(content="a stamped memory", scopes=["tools"])
    index.upsert(
        memory_dir,
        memory,
        filename=_filename(store, memory.id),
        verified_locally_at=STAMP,
    )
    _drop_events(memory_dir)
    _rebuild(store)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at == STAMP
    )


def test_a_pull_event_at_or_after_the_stamp_clears_it_at_rebuild(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(content="a memory another host will rewrite", scopes=["tools"])
    name = _filename(store, memory.id)
    index.upsert(memory_dir, memory, filename=name, verified_locally_at=STAMP)
    Recorder(root=memory_dir, session_id="cli-pull").record(
        "sync_pull", remote="origin", files=[name], count=1
    )
    _rebuild(store)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at is None
    )


def test_a_verify_event_after_the_pull_re_establishes_the_stamp(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(
        content="a memory verified here after a pull", scopes=["tools"]
    )
    name = _filename(store, memory.id)
    Recorder(root=memory_dir, session_id="cli-pull").record(
        "sync_pull", remote="origin", files=[name], count=1
    )
    Recorder(root=memory_dir, session_id="sess-verify").record(
        "verify", id=memory.id, last_verified_at=LATER
    )
    _rebuild(store)
    stamp = index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at
    assert stamp is not None
    assert parse_event_ts(stamp) is not None
    assert datetime.now(timezone.utc) - parse_event_ts(stamp) < timedelta(minutes=5)


def test_a_stale_verify_event_establishes_nothing(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(
        content="a memory whose verify lost the race", scopes=["tools"]
    )
    Recorder(root=memory_dir, session_id="sess-verify").record(
        "verify", status="stale", id=memory.id, current_updated=LATER
    )
    assert (
        provenance.local_verify_id({"kind": "verify", "status": "stale", "id": "x"})
        is None
    )
    assert provenance.local_verify_id({"kind": "verify", "id": "x"}) == "x"
    assert provenance.local_verify_id({"kind": "update", "id": "x"}) is None
    _rebuild(store)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at is None
    )


def test_deleting_the_index_derives_the_stamp_from_the_verify_event(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(
        content="a memory verified before the index was lost", scopes=["tools"]
    )
    Recorder(root=memory_dir, session_id="sess-verify").record(
        "verify", id=memory.id, last_verified_at=STAMP
    )
    db_path = index.index_path(memory_dir)
    for sibling in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        if sibling.exists():
            sibling.unlink()
    _rebuild(store)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at
        is not None
    )


def test_the_stamp_survives_a_tokenizer_drop_through_the_stash(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(
        content="a stamped memory that outlives a drop", scopes=["tools"]
    )
    index.upsert(
        memory_dir,
        memory,
        filename=_filename(store, memory.id),
        verified_locally_at=STAMP,
    )
    _drop_events(memory_dir)
    db_path = index.index_path(memory_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meta SET value = 'stale' WHERE key = 'tokenizer_fingerprint'"
        )
    assert index.status(memory_dir)["needs_rebuild"] is True
    with sqlite3.connect(db_path) as conn:
        stash = conn.execute(
            "SELECT value FROM meta WHERE key = 'trust_carry'"
        ).fetchone()
    assert stash is not None and memory.id in stash[0] and STAMP in stash[0]
    _rebuild(store)
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at == STAMP
    )
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT value FROM meta WHERE key = 'trust_carry'").fetchone()
            is None
        )


def test_classify_trust_takes_the_later_of_prior_and_event() -> None:
    class _Stub:
        id = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"

    stub = _Stub()
    evidence = provenance.Evidence(
        has_events=True,
        oldest_event_at=None,
        local_ids=frozenset(),
        pulled_files=frozenset(),
        tracked_files=None,
        verified_at={stub.id: parse_event_ts(LATER)},
        pulled_at={},
    )
    assert (
        provenance.classify_trust(stub, "a.md", evidence, STAMP)
        == parse_event_ts(LATER).isoformat()
    )  # type: ignore[arg-type]
    pulled_between = provenance.Evidence(
        has_events=True,
        oldest_event_at=None,
        local_ids=frozenset(),
        pulled_files=frozenset({"a.md"}),
        tracked_files=None,
        verified_at={},
        pulled_at={"a.md": parse_event_ts(LATER)},
    )
    assert provenance.classify_trust(stub, "a.md", pulled_between, STAMP) is None  # type: ignore[arg-type]
    assert provenance.classify_trust(stub, "a.md", pulled_between, None) is None  # type: ignore[arg-type]
    assert provenance.classify_trust(stub, "a.md", pulled_between, "garbage") is None  # type: ignore[arg-type]
