"""An expanded search row reflects the later filesystem read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory.handlers import search as handler
from bettermemory.verdict import memory_verdict

from .test_search_links import _call, server

__all__ = ["server"]


@pytest.mark.parametrize("attested", [False, True])
@pytest.mark.parametrize("field", ["counts", "report"])
async def test_expansion_replaces_the_earlier_missing_path_read(
    server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attested: bool,
    field: str,
) -> None:
    """The expanded top hit's path read replaces the earlier one. The counts
    and the report are internal computation, read from the row the search
    assembled; the wire hit reads memory_show's verdict and reason."""
    path = tmp_path / "restored-guide.py"
    path.write_text("pass\n")
    written = await _call(
        server,
        "memory_write",
        content=f"quartz saffron restore expansion guidance in `{path}`.",
        scopes=["tools"],
    )
    if attested:
        await _call(
            server, "memory_verify", id=written["id"], verified_paths=[str(path)]
        )
    path.unlink()
    before = await _call(
        server,
        "memory_search",
        query="quartz saffron restore expansion guidance",
        auto_scope=False,
    )
    before_hits = before.get("result", before) if isinstance(before, dict) else before
    assert before_hits[0]["staleness_verdict"] != "fresh"
    assert str(path) in before_hits[0]["reason"]
    real = memory_verdict
    later = []

    def restored(*args: Any, **kwargs: Any) -> Any:
        path.write_text("pass\n")
        shown = real(*args, **kwargs)
        later.append(shown)
        return shown

    monkeypatch.setattr(handler, "memory_verdict", restored)
    real_render = ResponseBuilder.render_hit
    assembled: list[dict[str, Any]] = []

    def render(self: ResponseBuilder, row: dict[str, Any]) -> dict[str, Any]:
        if row["id"] == written["id"]:
            assembled.append(dict(row))
        return real_render(self, row)

    monkeypatch.setattr(ResponseBuilder, "render_hit", render)
    result = await _call(
        server,
        "memory_search",
        query="quartz saffron restore expansion guidance",
        auto_scope=False,
        expand_top=True,
    )
    hits = result.get("result", result) if isinstance(result, dict) else result
    hit = hits[0]
    assert hit["id"] == written["id"] and hit["relevance"] == "high"
    assert "body" in hit and len(later) == 1
    assert hit["staleness_verdict"] == "fresh" and "reason" not in hit
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["staleness_verdict"] == "fresh" and "reason" not in shown
    assert len(assembled) == 1
    top = assembled[0]
    drift = later[0].path_drift
    assert drift.missing == () and drift.checked == (str(path),)
    if field == "counts":
        assert top["path_drift_checked"] == len(drift.checked)
        assert top["path_drift_missing"] == len(drift.missing)
    elif drift.should_report:
        assert top["path_drift"] == drift.to_dict()
    else:
        assert "path_drift" not in top
