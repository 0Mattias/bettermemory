"""Failed optional body reads leave the main search hit usable."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory.store import Store

from .test_dependency_load_budget import _depends, _hit
from .test_search_links import _call, _search, _hit as search_hit, server

__all__ = ["server"]


@pytest.mark.parametrize(
    "failure", [sqlite3.OperationalError, sqlite3.ProgrammingError, OSError, None]
)
def test_a_target_read_failure_keeps_other_dependency_annotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception] | None,
) -> None:
    with Store(tmp_path / "store") as store:
        bad = store.write(content="unavailable context", scopes=["tools"])
        good = store.write(content="healthy context", scopes=["tools"])
        source = _depends("main result", [bad, good])
        real = Store.load_one

        def load_one(self: Store, record_id: str) -> Any:
            if record_id == bad.id and failure is not None:
                raise failure("synthetic target read failure")
            return real(self, record_id)

        monkeypatch.setattr(Store, "load_one", load_one)
        out: list[dict[str, Any]] = [{"id": source.id}]
        ResponseBuilder(stale_after_days=30).attach_depends_on_resolved(
            out, [_hit(source)], [source], store=store
        )
    expected = [bad.id, good.id] if failure is None else [good.id]
    assert [row["id"] for row in out[0]["depends_on_resolved"]] == expected


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, OSError, None])
async def test_search_keeps_its_hit_when_an_optional_target_cannot_be_read(
    server: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception] | None,
) -> None:
    # The target does not match the query, so the pool does not hold it
    # and its targeted load is exercised.
    target = await _call(
        server,
        "memory_write",
        content="unrelated quartz backing detail",
        scopes=["tools"],
    )
    source = await _call(
        server,
        "memory_write",
        content="saffron optional dependency parsing guidance",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_update",
        id=source["id"],
        links=[{"type": "depends_on", "target_id": target["id"]}],
    )
    real = Store.load_one
    loaded: list[str] = []

    def load_one(self: Store, record_id: str) -> Any:
        loaded.append(record_id)
        if record_id == target["id"] and failure is not None:
            raise failure("synthetic target read failure")
        return real(self, record_id)

    monkeypatch.setattr(Store, "load_one", load_one)
    hits = await _search(server, "saffron dependency parsing")
    assert target["id"] in loaded, "The target never left the preloaded candidate pool"
    row = search_hit(hits, source["id"])
    if failure is None:
        assert [entry["id"] for entry in row["depends_on_resolved"]] == [target["id"]]
    else:
        assert "depends_on_resolved" not in row


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, OSError, None])
async def test_search_keeps_its_hit_when_optional_expansion_cannot_be_read(
    server: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception] | None,
) -> None:
    content = "alpha beta gamma the quick brown fox jumps over"
    source = await _call(server, "memory_write", content=content, scopes=["tools"])
    real = Store.load_one
    loaded: list[str] = []

    def load_one(self: Store, record_id: str) -> Any:
        loaded.append(record_id)
        if record_id == source["id"] and failure is not None:
            raise failure("synthetic optional expansion read failure")
        return real(self, record_id)

    monkeypatch.setattr(Store, "load_one", load_one)
    result = await _call(
        server,
        "memory_search",
        query="alpha beta gamma quick brown fox jumps over",
        auto_scope=False,
        expand_top=True,
    )
    hits = result.get("result", result) if isinstance(result, dict) else result
    assert source["id"] in loaded, "The expansion read was never exercised"
    row = search_hit(hits, source["id"])
    assert row["relevance"] == "high"
    if failure is None:
        assert row["body"].strip() == content
    else:
        assert "body" not in row
