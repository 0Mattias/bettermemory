"""A record's search hit and its memory_show read the same verdict and the
same reason, in every state that makes either ask for a check or stop
asking. Both sides are the wire shapes the caller receives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.models import MemoryLink
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .test_membership_security import Lab, lab
from .test_server import _plant

__all__ = ["lab"]


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def server(memory_dir: Path, store: Store) -> Any:
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=config, store=store, state=SessionState())


async def _write(server: Any, body: str) -> str:
    written = await call_tool(
        server, "memory_write", {"content": body, "scopes": ["tools"]}
    )
    return written["id"]


async def _link(server: Any, source: str, links: list[dict[str, Any]]) -> None:
    await call_tool(server, "memory_update", {"id": source, "links": links})


async def _read(server: Any, rid: str, query: str) -> tuple[dict[str, Any], dict]:
    """The record's hit in an unscoped search for `query`, and its show."""
    found = await call_tool(
        server, "memory_search", {"query": query, "auto_scope": False}
    )
    hits = found.get("result", found) if isinstance(found, dict) else found
    (hit,) = [h for h in hits if h["id"] == rid]
    shown = await call_tool(server, "memory_show", {"id": rid})
    return hit, shown


def _agree(hit: dict[str, Any], shown: dict[str, Any]) -> None:
    assert hit["staleness_verdict"] == shown["staleness_verdict"]
    assert hit.get("reason") == shown.get("reason")


@pytest.mark.parametrize("direction", ["from", "to"])
async def test_a_contradiction_asks_on_both_surfaces(
    server: Any, direction: str
) -> None:
    """A contradicts link from the record, or to it, raises the hit and the
    show alike, and both reasons name the other record."""
    rid = await _write(server, "the heron gateway retries three times")
    other = await _write(server, "a different note on queue depth")
    source, target = (rid, other) if direction == "from" else (other, rid)
    await _link(server, source, [{"type": "contradicts", "target_id": target}])
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert other in hit["reason"]
    assert hit["contradicts"] == [other]


@pytest.mark.parametrize("direction", ["from", "to"])
async def test_a_removed_contradicting_record_objects_nowhere(
    server: Any, direction: str
) -> None:
    """Once the contradicting record is removed, neither surface objects and
    neither names its id."""
    rid = await _write(server, "the heron gateway retries three times")
    other = await _write(server, "a different note on queue depth")
    source, target = (rid, other) if direction == "from" else (other, rid)
    await _link(server, source, [{"type": "contradicts", "target_id": target}])
    await call_tool(server, "memory_remove", {"id": other, "reason": "wrong all along"})
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "fresh"
    assert "reason" not in hit
    assert "contradicts" not in hit


@pytest.mark.parametrize("direction", ["from", "to"])
async def test_a_contradiction_the_caller_cannot_see_objects_nowhere(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    """A contradicting record in a tenant the caller is not admitted to:
    neither surface objects, and neither reason names it."""
    rid = lab.write("root", "the lanternfish deploy window is tuesday")
    if direction == "to":
        hidden = lab.write(
            "client",
            "a tenant's private objection",
            links=[MemoryLink(type="contradicts", target_id=rid)],
        )
    else:
        hidden = lab.write("client", "a tenant's private objection")
        record = lab.store.load_one(rid)
        lab.store.update(
            record.model_copy(
                update={"links": [MemoryLink(type="contradicts", target_id=hidden)]}
            )
        )
        lab.store.mark_verified(rid)
    monkeypatch.chdir(lab.places["root"])
    server = build_server(config=lab.config, store=lab.store, state=SessionState())
    loaded: list[str] = []
    real_load = Store.load_one

    def spy(self: Store, record_id: str) -> Any:
        loaded.append(record_id)
        return real_load(self, record_id)

    monkeypatch.setattr(Store, "load_one", spy)
    hit, shown = await _read(server, rid, "lanternfish deploy window")
    assert hidden not in loaded
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "fresh"
    assert hidden not in hit.get("reason", "")
    assert hidden not in shown.get("reason", "")
    assert "contradicts" not in hit


async def test_supersessions_do_not_hide_a_contradiction(server: Any) -> None:
    """A hit superseded three times that also contradicts a record still
    asks for a check, as its show does."""
    rid = await _write(server, "the heron gateway retries three times")
    newer = [
        await _write(server, f"replacement number {n} for queue depth")
        for n in range(3)
    ]
    for source in newer:
        await _link(server, source, [{"type": "supersedes", "target_id": rid}])
    rival = await _write(server, "a different note on payload size")
    await _link(server, rid, [{"type": "contradicts", "target_id": rival}])
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert sorted(hit["superseded_by"]) == sorted(newer)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert rival in hit["reason"]
    assert hit["contradicts"] == [rival]


async def test_a_recent_rejection_asks_until_a_later_use_applies_it(
    server: Any,
) -> None:
    rid = await _write(server, "the heron gateway retries three times")
    await call_tool(
        server, "memory_record_use", {"memory_ids": [rid], "outcome": "ignored"}
    )
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert "ignored" in hit["reason"]

    await call_tool(
        server, "memory_record_use", {"memory_ids": [rid], "outcome": "applied"}
    )
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "fresh"
    assert "reason" not in hit


async def test_a_store_without_its_key_never_reads_an_unstamped_row_fresh(
    memory_dir: Path, server: Any, store: Store, tmp_path: Path
) -> None:
    """Opened without its key, the store cannot say where a row came from:
    an unstamped row is not fresh on either surface, and the hit says the
    trust rule could not run. One keyed read first records this machine,
    which a store open for reading only cannot."""
    rid = await _write(server, "the heron gateway retries three times")
    await _read(server, rid, "heron gateway retries")
    path = store.path
    store.close()
    blind = Store.open(path, keys_dir=tmp_path / "elsewhere", allow_rekey=False)
    assert blind.has_key is False
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=config, store=blind, state=SessionState())
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] != "fresh"
    assert hit.get("reason")
    assert hit.get("trust_unavailable") is True
    assert shown.get("trust_unavailable") is True
    blind.close()


async def test_an_unaccounted_row_never_verified_is_not_fresh(
    server: Any, store: Store
) -> None:
    rid = _plant(store, "the heron gateway retries three times", ["tools"])
    hit, shown = await _read(server, rid, "heron gateway retries")
    assert shown["provenance"] == "unaccounted"
    _agree(hit, shown)
    assert hit["staleness_verdict"] != "fresh"
    assert hit.get("reason")


async def test_a_synced_row_never_verified_here_is_not_fresh(
    server: Any, store: Store
) -> None:
    rid = await _write(server, "the heron gateway retries three times")
    store.put_memory(store.load_one(rid), provenance="synced")
    hit, shown = await _read(server, rid, "heron gateway retries")
    assert shown["provenance"] == "synced"
    _agree(hit, shown)
    assert hit["staleness_verdict"] != "fresh"
    assert hit.get("reason")


async def test_a_stamped_row_on_a_store_without_its_key_is_required(
    memory_dir: Path, server: Any, store: Store, tmp_path: Path
) -> None:
    """A stamp no key can vouch for requires a check on both surfaces."""
    rid = await _write(server, "the heron gateway retries three times")
    await call_tool(server, "memory_verify", {"id": rid})
    await _read(server, rid, "heron gateway retries")
    path = store.path
    store.close()
    blind = Store.open(path, keys_dir=tmp_path / "elsewhere", allow_rekey=False)
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=config, store=blind, state=SessionState())
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_required"
    assert "Nothing in these tools clears it" in hit["reason"]
    blind.close()


async def test_a_stamped_unaccounted_row_is_required(server: Any, store: Store) -> None:
    """A stamp on a row the log does not account for may be planted: it
    requires a check, and the reason says memory_verify does not clear it."""
    rid = await _write(server, "the heron gateway retries three times")
    await call_tool(server, "memory_verify", {"id": rid})
    # The row's pointer into the log is gone, as on a row a hand planted.
    store.conn.execute("UPDATE memories SET log_mac = NULL WHERE id = ?", (rid,))
    hit, shown = await _read(server, rid, "heron gateway retries")
    assert shown["provenance"] == "unaccounted"
    assert "last_verified_at" in shown
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_required"
    assert "memory_verify does not clear" in hit["reason"]


async def test_a_row_with_no_trust_row_reads_provenance_null(
    server: Any, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No trust row: both surfaces say `provenance: null`, apart from
    local, and that alone asks for nothing."""
    rid = await _write(server, "the heron gateway retries three times")
    real = Store.trust_rows

    def without(self: Store, ids: Any) -> Any:
        rows = real(self, ids)
        return None if rows is None else {k: v for k, v in rows.items() if k != rid}

    monkeypatch.setattr(Store, "trust_rows", without)
    hit, shown = await _read(server, rid, "heron gateway retries")
    _agree(hit, shown)
    assert "provenance" in hit and hit["provenance"] is None
    assert "provenance" in shown and shown["provenance"] is None
    assert hit["staleness_verdict"] == "fresh"


async def test_a_path_attested_absent_that_is_present_asks_on_both(
    server: Any, tmp_path: Path
) -> None:
    gone = tmp_path / "retired.conf"
    rid = await _write(server, f"the heron gateway config `{gone}` is retired")
    await call_tool(
        server, "memory_verify", {"id": rid, "verified_absent_paths": [str(gone)]}
    )
    gone.write_text("back\n")
    hit, shown = await _read(server, rid, "heron gateway config retired")
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert str(gone) in hit["reason"]
    assert shown["paths"][str(gone)] == "present, attested absent"


async def test_exclude_scopes_does_not_hide_an_objection(server: Any) -> None:
    """`exclude_scopes` narrows what a search lists, not what objects to a
    listed record: the hit and the show agree, and the hit lists the id its
    reason names."""
    rid = await _write(server, "the heron gateway retries three times")
    rival = await call_tool(
        server,
        "memory_write",
        {"content": "a different note on queue depth", "scopes": ["infrastructure"]},
    )
    await _link(server, rid, [{"type": "contradicts", "target_id": rival["id"]}])
    found = await call_tool(
        server,
        "memory_search",
        {
            "query": "heron gateway retries",
            "auto_scope": False,
            "exclude_scopes": ["infrastructure"],
        },
    )
    hits = found.get("result", found) if isinstance(found, dict) else found
    (hit,) = [h for h in hits if h["id"] == rid]
    shown = await call_tool(server, "memory_show", {"id": rid})
    _agree(hit, shown)
    assert hit["staleness_verdict"] == "spot_check_recommended"
    assert rival["id"] in hit["reason"]
    assert hit["contradicts"] == [rival["id"]]


async def _search_cost(
    server: Any, store: Store, monkeypatch: pytest.MonkeyPatch, links: int
) -> tuple[int, int]:
    """Five hits that each contradict `links` of their forty rivals (the
    store's size held fixed): the SQL statements and record loads one
    search makes."""
    for n in range(5):
        rid = await _write(server, f"the osprey relay number {n} retries")
        rivals = [await _write(server, f"unrelated rival {n}-{k}") for k in range(40)]
        await _link(
            server,
            rid,
            [{"type": "contradicts", "target_id": r} for r in rivals[:links]],
        )
    loads: list[int] = []
    real_one, real_many = Store.load_one, Store.load_many

    def load_one(self: Store, record_id: str) -> Any:
        loads.append(1)
        return real_one(self, record_id)

    def load_many(self: Store, ids: Any) -> Any:
        ids = list(ids)
        loads.append(len(ids))
        return real_many(self, ids)

    monkeypatch.setattr(Store, "load_one", load_one)
    monkeypatch.setattr(Store, "load_many", load_many)
    statements: list[str] = []
    store.conn.set_trace_callback(statements.append)
    try:
        found = await call_tool(
            server,
            "memory_search",
            {"query": "osprey relay retries", "auto_scope": False},
        )
    finally:
        store.conn.set_trace_callback(None)
        monkeypatch.setattr(Store, "load_one", real_one)
        monkeypatch.setattr(Store, "load_many", real_many)
    hits = found.get("result", found) if isinstance(found, dict) else found
    osprey = [h for h in hits if "osprey" in h["snippet"]]
    assert len(osprey) == 5
    assert all(len(h["contradicts"]) == links for h in osprey)
    return len(statements), sum(loads)


async def test_the_objection_read_does_not_grow_with_the_link_count(
    memory_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five hits with five contradictions each and with forty each cost a
    search the same statements and the same record loads."""
    costs = []
    for links, place in ((5, "few"), (40, "many")):
        store = Store(tmp_path / place)
        config = Config(storage=StorageConfig(directory=str(tmp_path / place)))
        server = build_server(config=config, store=store, state=SessionState())
        costs.append(await _search_cost(server, store, monkeypatch, links))
        store.close()
    assert costs[0] == costs[1]
