"""Dependency display limits must guide the bounded targeted-load fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory.models import LinkType, Memory, MemoryHit, MemoryLink
from bettermemory.store import Store

from .test_store import _memory


class CountingStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.loaded: list[str] = []

    def load_one(self, record_id: str) -> Memory:
        self.loaded.append(record_id)
        return self.store.load_one(record_id)


def _hit(memory: Memory) -> MemoryHit:
    return MemoryHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        snippet=memory.body,
        score=1.0,
        created=memory.created,
        updated=memory.updated,
    )


def _depends(body: str, targets: list[Memory]) -> Memory:
    return _memory(
        body,
        links=[MemoryLink(type=LinkType.DEPENDS_ON, target_id=m.id) for m in targets],
    )


@pytest.mark.parametrize("first_targets", ["active", "tombstoned", "excluded"])
def test_an_oversized_first_dependency_list_does_not_starve_a_later_hit(
    tmp_path: Path, first_targets: str
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        targets = [
            store.put_memory(_memory(f"target {i}", "private")) for i in range(10)
        ]
        later = store.put_memory(_memory("later context"))
        if first_targets == "tombstoned":
            for target in targets:
                store.tombstone(target.id, reason="synthetic")
        first = _depends("first result", targets)
        second = _depends("second result", [later])
        counted = CountingStore(store)
        out: list[dict[str, Any]] = [{"id": first.id}, {"id": second.id}]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out,
            [_hit(first), _hit(second)],
            [first, second],
            store=counted,
            excluded_scopes={"private"} if first_targets == "excluded" else set(),
        )
        assert [r["id"] for r in out[1]["depends_on_resolved"]] == [later.id]
        assert len(counted.loaded) <= 10
        assert len(out[0].get("depends_on_resolved", [])) <= 3
        if first_targets != "active":
            assert "depends_on_resolved" not in out[0]


@pytest.mark.parametrize("max_per_hit,max_total", [(0, 10), (3, 0)])
def test_a_disabled_dependency_surface_loads_no_targets(
    tmp_path: Path, max_per_hit: int, max_total: int
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        target = store.put_memory(_memory("target"))
        source = _depends("result", [target])
        counted = CountingStore(store)
        out: list[dict[str, Any]] = [{"id": source.id}]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out,
            [_hit(source)],
            [source],
            store=counted,
            max_per_hit=max_per_hit,
            max_total=max_total,
        )
        assert "depends_on_resolved" not in out[0]
        assert counted.loaded == []


def test_failed_targets_are_backfilled_after_later_hits_get_a_turn(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        targets = [store.put_memory(_memory(f"context {i}")) for i in range(6)]
        for target in targets[:3]:
            store.tombstone(target.id, reason="synthetic")
        later = store.put_memory(_memory("later context"))
        sources = [_depends("first", targets), _depends("second", [later])]
        counted = CountingStore(store)
        out: list[dict[str, Any]] = [{"id": m.id} for m in sources]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out, [_hit(m) for m in sources], sources, store=counted
        )
        assert [r["id"] for r in out[0]["depends_on_resolved"]] == [
            m.id for m in targets[3:]
        ]
        assert [r["id"] for r in out[1]["depends_on_resolved"]] == [later.id]
        assert len(counted.loaded) == 7


def test_preloaded_display_slots_leave_the_load_budget_for_later_hits(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        targets = [store.put_memory(_memory(f"context {i}")) for i in range(14)]
        sources = [
            _depends("first", targets[:10]),
            _depends("second", targets[10:13]),
            _depends("third", targets[13:]),
        ]
        counted = CountingStore(store)
        out: list[dict[str, Any]] = [{"id": m.id} for m in sources]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out,
            [_hit(m) for m in sources],
            [*sources, *targets[:3]],
            store=counted,
        )
        assert [len(row["depends_on_resolved"]) for row in out] == [3, 3, 1]
        assert set(counted.loaded) == {m.id for m in targets[10:]}


def test_load_and_display_caps_preserve_hit_order(tmp_path: Path) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        targets = [store.put_memory(_memory(f"context {i}")) for i in range(8)]
        sources = [_depends("first", targets[:4]), _depends("second", targets[4:])]
        counted = CountingStore(store)
        out: list[dict[str, Any]] = [{"id": m.id} for m in sources]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out, [_hit(m) for m in sources], sources, store=counted, max_total=4
        )
        assert [len(row["depends_on_resolved"]) for row in out] == [3, 1]
        assert len(counted.loaded) == 4
