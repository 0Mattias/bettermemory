"""Known candidate links survive a bulk-query failure without bypassing filters."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory._response_links import candidate_link_rows
from bettermemory.store import Store

from .test_response_annotation_bounds import _hit
from .test_search_links import _call, _search, _hit as search_hit, server

__all__ = ["server"]


def _unavailable(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("synthetic bulk link read failure")


@pytest.mark.parametrize("excluded", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
def test_loaded_inbound_links_survive_a_bulk_read_failure(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    excluded: bool,
    streamed: bool,
) -> None:
    with Store(memory_dir) as store:
        target = store.write(content="linked anchor", scopes=["tools"])
        superseder = store.write(
            content="replacement cedar",
            scopes=["private"],
            links=[{"type": "supersedes", "target_id": target.id}],
        )
        contrary = store.write(
            content="contrary maple",
            scopes=["tools"],
            links=[{"type": "contradicts", "target_id": target.id}],
        )
        memories = store.load_all()
        monkeypatch.setattr(Store, "links_for_many", _unavailable)
        out: list[dict[str, Any]] = [{"id": target.id}]
        ResponseBuilder(stale_after_days=30).attach_link_annotations(
            out,
            [_hit(target.id)],
            iter(memories) if streamed else memories,
            store=store,
            excluded_scopes={"private"} if excluded else None,
        )
    assert [row["id"] for row in out[0].get("contradicts", [])] == [contrary.id]
    assert [row["id"] for row in out[0].get("superseded_by", [])] == (
        [] if excluded else [superseder.id]
    )


@pytest.mark.parametrize("denied", [False, True])
def test_recovered_outbound_links_hydrate_and_reapply_admission(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied: bool,
) -> None:
    class Admission:
        primed: set[str]

        def __init__(self) -> None:
            self.primed = set()

        def prime(self, ids: Iterable[str]) -> None:
            self.primed.update(ids)

        def admits_id(self, mid: str) -> bool:
            assert mid in self.primed, "fallback targets were not primed"
            return not denied

    with Store(memory_dir) as store:
        target = store.write(content="outside candidate target", scopes=["tools"])
        source = store.write(
            content="only loaded candidate",
            scopes=["tools"],
            links=[{"type": "contradicts", "target_id": target.id, "note": "clash"}],
        )
        gate = Admission()
        monkeypatch.setattr(Store, "links_for_many", _unavailable)
        out: list[dict[str, Any]] = [{"id": source.id}]
        ResponseBuilder(stale_after_days=30).attach_link_annotations(
            out,
            [_hit(source.id)],
            [source],
            store=store,
            admission=gate,  # type: ignore[arg-type]
        )
    assert target.id in gate.primed
    assert [row["id"] for row in out[0].get("contradicts", [])] == (
        [] if denied else [target.id]
    )
    if not denied:
        assert out[0]["contradicts"][0]["link_note"] == "clash"


def test_an_authoritative_empty_reply_overrides_older_candidate_links(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Store(memory_dir) as store:
        target = store.write(content="current target", scopes=["tools"])
        store.write(
            content="older linked candidate",
            scopes=["tools"],
            links=[{"type": "supersedes", "target_id": target.id}],
        )
        memories = store.load_all()

        def empty(self: Any, ids: Iterable[str]) -> Any:
            return {mid: ([], []) for mid in ids}

        monkeypatch.setattr(Store, "links_for_many", empty)
        out: list[dict[str, Any]] = [{"id": target.id}]
        ResponseBuilder(stale_after_days=30).attach_link_annotations(
            out,
            [_hit(target.id)],
            memories,
            store=store,
        )
    assert "superseded_by" not in out[0] and "contradicts" not in out[0]


async def test_search_recovers_known_annotations_when_bulk_read_fails(
    server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = await _call(
        server,
        "memory_write",
        content="cedar fallback validates session tokens",
        scopes=["tools"],
    )
    replacement = await _call(
        server,
        "memory_write",
        content="replacement spruce note",
        scopes=["tools"],
    )
    contrary = await _call(
        server,
        "memory_write",
        content="contrary walnut note",
        scopes=["tools"],
    )
    for record, kind in [(replacement, "supersedes"), (contrary, "contradicts")]:
        await _call(
            server,
            "memory_update",
            id=record["id"],
            links=[{"type": kind, "target_id": target["id"]}],
        )
    monkeypatch.setattr(Store, "links_for_many", _unavailable)
    row = search_hit(
        await _search(server, "cedar fallback session tokens"), target["id"]
    )
    assert [entry["id"] for entry in row.get("superseded_by", [])] == [
        replacement["id"]
    ]
    assert [entry["id"] for entry in row.get("contradicts", [])] == [contrary["id"]]


def test_candidate_rows_match_the_stores_exact_duplicate_policy(
    memory_dir: Path,
) -> None:
    with Store(memory_dir) as store:
        first = store.write(content="first projection target", scopes=["tools"])
        second = store.write(content="second projection target", scopes=["tools"])
        source = store.write(
            content="projection source",
            scopes=["tools"],
            links=[
                {"type": "supersedes", "target_id": first.id, "note": "z"},
                {"type": "supersedes", "target_id": first.id, "note": "z"},
                {"type": "supersedes", "target_id": first.id, "note": "a"},
                {"type": "contradicts", "target_id": second.id},
                {"type": "contradicts", "target_id": second.id},
                {"type": "extends", "target_id": first.id, "note": "detail"},
            ],
        )
        ids = [first.id, second.id, source.id]
        canonical = store.links_for_many(ids)
        recovered = candidate_link_rows(store.load_all(), ids)
    assert recovered.keys() == canonical.keys()
    for mid in ids:
        for actual, expected in zip(recovered[mid], canonical[mid]):
            assert set(actual) == set(expected)
            assert [(kind, oid) for kind, oid, _note in actual] == [
                (kind, oid) for kind, oid, _note in expected
            ]
    assert len(recovered[source.id][0]) == 4


@pytest.mark.parametrize("unavailable", ["tombstoned", "closed"])
def test_unavailable_hydration_preserves_other_known_annotations(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    with Store(memory_dir) as store:
        outside = store.write(content="unavailable target", scopes=["tools"])
        source = store.write(
            content="candidate with unavailable edge",
            scopes=["tools"],
            links=[{"type": "contradicts", "target_id": outside.id}],
        )
        replacement = store.write(
            content="loaded replacement",
            scopes=["tools"],
            links=[{"type": "supersedes", "target_id": source.id}],
        )
        if unavailable == "tombstoned":
            store.tombstone(outside.id, reason="removed target")
        else:
            store.close()
        monkeypatch.setattr(Store, "links_for_many", _unavailable)
        out: list[dict[str, Any]] = [{"id": source.id}]
        ResponseBuilder(stale_after_days=30).attach_link_annotations(
            out,
            [_hit(source.id)],
            [source, replacement],
            store=store,
        )
    assert "contradicts" not in out[0]
    assert [row["id"] for row in out[0].get("superseded_by", [])] == [replacement.id]
