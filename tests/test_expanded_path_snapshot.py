"""An expanded search row reflects the later filesystem read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
    assert before_hits[0]["path_drift_missing"] == 1
    assert before_hits[0]["path_drift"]["missing"] == [str(path)]
    real = memory_verdict
    later = []

    def restored(*args: Any, **kwargs: Any) -> Any:
        path.write_text("pass\n")
        shown = real(*args, **kwargs)
        later.append(shown)
        return shown

    monkeypatch.setattr(handler, "memory_verdict", restored)
    result = await _call(
        server,
        "memory_search",
        query="quartz saffron restore expansion guidance",
        auto_scope=False,
        expand_top=True,
    )
    hits = result.get("result", result) if isinstance(result, dict) else result
    top = hits[0]
    assert top["id"] == written["id"] and top["relevance"] == "high"
    assert "body" in top and len(later) == 1
    drift = later[0].path_drift
    assert drift.missing == () and drift.checked == (str(path),)
    if field == "counts":
        assert top["path_drift_checked"] == len(drift.checked)
        assert top["path_drift_missing"] == len(drift.missing)
    elif drift.should_report:
        assert top["path_drift"] == drift.to_dict()
    else:
        assert "path_drift" not in top
