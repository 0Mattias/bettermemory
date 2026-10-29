"""An older writer refuses a record a later build wrote with fields it does
not define (ENG-107).

`Memory` reads with pydantic's default ``extra='ignore'``, so a field a
later 9.x added to the record is dropped on the way in, and the old
write path then put the record back without it: a `memory_update` from an
older build lost the field silently, and with it whatever the later
build tied to the fields the update changed. Round-tripping the unknown
field would keep its bytes and lose those rules, so this build refuses
every mutation of such a record instead, naming the fields. A later
build's put is simulated here as that build appends it: a signed
``memory_put`` (or ``tombstone_put``) whose record carries a field this
build's model does not define.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import STORE_FILENAME, Store

from ._mcp import call_tool, crash_text

#: A field no build of this one defines, as a later build would add it.
LATER = {"review_due": "2027-01-01T00:00:00+00:00"}


def later_put(store: Store, memory_id: str) -> None:
    """A later build's put of the active record `memory_id`, carrying
    `LATER` beside every field this build defines."""
    memory = store.get_memory(memory_id)
    record = {**memory.model_dump(mode="json"), **LATER}
    payload = {
        "memory": record,
        "provenance": "local",
        "filename": store.filename_for(memory_id),
    }
    with store._transaction() as tx:
        store._mutate(tx, "memory_put", payload, session=None)


def later_tombstone_put(store: Store, memory_id: str) -> None:
    """A later build's put of the tombstone `memory_id`, carrying `LATER`."""
    dead = store.get_tombstone(memory_id)
    payload: dict[str, Any] = {
        "tombstone": {**dead.model_dump(mode="json"), **LATER},
        "provenance": "local",
        "filename": None,
        "links": [],
        "corroborations": 0,
        "last_corroborated": None,
    }
    with store._transaction() as tx:
        store._mutate(tx, "tombstone_put", payload, session=None)


def last_record(store: Store, memory_id: str) -> dict[str, Any]:
    """The record the last put of `memory_id` carries, active or removed."""
    import json

    for kind, payload in store.conn.execute(
        "SELECT kind, payload FROM log WHERE kind IN ('memory_put', 'tombstone_put') "
        "ORDER BY seq DESC"
    ):
        body = json.loads(payload)
        record = body.get("memory") or body.get("tombstone")
        if record.get("id") == memory_id:
            return dict(record)
    raise AssertionError(f"no put of {memory_id}")


def log_rows(store: Store) -> int:
    """The record mutations in the log: a refused call may still record
    its telemetry row, never a change to a record."""
    return int(
        store.conn.execute(
            "SELECT COUNT(*) FROM log WHERE kind IN ("
            "'memory_put', 'memory_delete', 'tombstone_put', 'tombstone_delete')"
        ).fetchone()[0]
    )


async def test_memory_update_refuses_a_record_carrying_a_field_this_build_lacks(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )
    written = await call_tool(
        server,
        "memory_write",
        {"content": "A record a later build extends.", "scopes": ["tools"]},
    )
    later_put(store, written["id"])
    rows = log_rows(store)
    with pytest.raises(Exception) as caught:
        await call_tool(
            server,
            "memory_update",
            {
                "id": written["id"],
                "content": "An older build's edit of the record, longer than the first.",
            },
        )
    text = crash_text(caught.value)
    assert "defines fields this one does not ('review_due')" in text
    assert "upgrade bettermemory" in text
    # Nothing was written: the later build's put is still the record's
    # last, field and body as it left them.
    assert log_rows(store) == rows
    record = last_record(store, written["id"])
    assert record["review_due"] == LATER["review_due"]
    assert record["body"] == "A record a later build extends.\n"
    assert store.get_memory(written["id"]).body == record["body"]
    # The fold still reproduces every row: the field is ignored on replay.
    assert store.log_verify()["status"] == "ok"
    store.close()


def test_a_record_without_such_a_field_is_updated_as_before(memory_dir: Path) -> None:
    with Store(memory_dir) as store:
        memory = store.write(content="a record of this build", scopes=["tools"])
        updated = store.update(memory.model_copy(update={"body": "edited\n"}))
        assert store.get_memory(memory.id).body == updated.body == "edited\n"


@pytest.mark.parametrize(
    ("refused", "change"),
    [
        (
            "the change",
            lambda s, i: s.update(s.get_memory(i).model_copy(update={"body": "x\n"})),
        ),
        ("the change", lambda s, i: s.mark_verified(i)),
        ("the change", lambda s, i: s.record_corroboration(i)),
        (
            "the removal",
            lambda s, i: s.tombstone(i, reason="an older build's removal"),
        ),
    ],
    ids=["update", "verify", "corroborate", "remove"],
)
def test_every_change_to_such_an_active_record_is_refused(
    memory_dir: Path, refused: str, change: Callable[[Store, str], object]
) -> None:
    with Store(memory_dir) as store:
        memory = store.write(content="a record a later build extends", scopes=["tools"])
        later_put(store, memory.id)
        rows = log_rows(store)
        with pytest.raises(ValueError) as caught:
            change(store, memory.id)
        assert type(caught.value).__name__ == "UnknownFieldsError"
        assert f"refuses {refused}: upgrade bettermemory" in str(caught.value)
        assert log_rows(store) == rows
        assert store.has_memory(memory.id)
        assert last_record(store, memory.id)["review_due"] == LATER["review_due"]


@pytest.mark.parametrize(
    "change",
    [lambda s, i: s.restore(i), lambda s, i: s.delete_tombstone(i)],
    ids=["restore", "purge"],
)
def test_every_change_to_such_a_removed_record_is_refused(
    memory_dir: Path, change: Callable[[Store, str], object]
) -> None:
    with Store(memory_dir) as store:
        memory = store.write(content="a record a later build removed", scopes=["tools"])
        store.tombstone(memory.id, reason="removed by this build")
        later_tombstone_put(store, memory.id)
        rows = log_rows(store)
        with pytest.raises(ValueError) as caught:
            change(store, memory.id)
        assert type(caught.value).__name__ == "UnknownFieldsError"
        assert "refuses the restore, purge or prune" in str(caught.value)
        assert log_rows(store) == rows
        assert store.get_tombstone(memory.id).id == memory.id
        assert last_record(store, memory.id)["review_due"] == LATER["review_due"]
        assert store.log_verify()["status"] == "ok"


def test_a_prune_leaves_a_later_builds_removed_record_and_prunes_the_rest(
    memory_dir: Path,
) -> None:
    """The prune goes oldest removal first; a later build's tombstone, the
    oldest here, is left for that build, and the prune does not stop at it:
    the others go, and a rerun prunes nothing more and raises nothing."""
    with Store(memory_dir) as store:
        ids = []
        for n in range(3):
            memory = store.write(content=f"removed record {n}", scopes=["tools"])
            store.tombstone(memory.id, reason=f"removed {n}")
            ids.append(memory.id)
        later_tombstone_put(store, ids[0])
        pruned = store.prune_tombstones(timedelta(0))
        assert sorted(pruned) == sorted(ids[1:])
        assert [dead.id for dead in store.iter_tombstones()] == [ids[0]]
        assert last_record(store, ids[0])["review_due"] == LATER["review_due"]
        assert store.prune_tombstones(timedelta(0)) == []
        assert store.log_verify()["status"] == "ok"


def test_a_scope_rename_lists_a_later_builds_removed_record_as_failed(
    memory_dir: Path,
) -> None:
    with Store(memory_dir) as store:
        kept = store.write(content="a record renamed with the rest", scopes=["old"])
        theirs = store.write(content="a record a later build removed", scopes=["old"])
        store.tombstone(theirs.id, reason="removed by this build")
        later_tombstone_put(store, theirs.id)
        result = store.rename_scope("old", "new")
        assert result["active"] == [kept.id]
        assert result["tombstoned"] == []
        [failed] = result["failed"]
        assert failed["id"] == theirs.id
        assert "defines fields this one does not ('review_due')" in failed["reason"]
        assert store.get_memory(kept.id).scopes == ["new"]
        assert store.get_tombstone(theirs.id).scopes == ["old"]
        assert store.log_verify()["status"] == "ok"


def test_the_guard_reads_the_records_last_put_through_the_mac_index(
    tmp_path: Path,
) -> None:
    """The guard finds the payload by the row's `log_mac` (`log_by_mac`),
    not by a scan of the log: every record mutation asks it."""
    from bettermemory import store_rows

    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path) as store:
        memory = store.write(content="a record", scopes=["tools"])
        later_put(store, memory.id)
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        try:
            found = store_rows._unknown_record_fields(store.conn, memory.id)
        finally:
            store.conn.set_trace_callback(None)
        assert found == ["review_due"]
        [query] = [text for text in statements if "FROM log" in text]
        plan = " ".join(
            str(row[3]) for row in store.conn.execute("EXPLAIN QUERY PLAN " + query)
        )
    assert "log_by_mac" in plan, plan
