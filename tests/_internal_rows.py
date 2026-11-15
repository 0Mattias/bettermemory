"""Read a search's working rows instead of its wire shape.

`memory_search` assembles one row per hit (`ResponseBuilder.hit_to_dict`
and the attach passes: the path and commit drift, the verification block,
the links) and renders each into the compact wire hit
(`ResponseBuilder.render_hit`). A test of the drift computation asserts on
the rows: `raw_hits(monkeypatch)` makes the render return its row, so the
search answers with them.
"""

from __future__ import annotations

from typing import Any

import pytest

from bettermemory._response import ResponseBuilder

_RENDER_HIT = ResponseBuilder.render_hit


def raw_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every `memory_search` in the test return its working rows."""

    def render(self: ResponseBuilder, row: dict[str, Any]) -> dict[str, Any]:
        return row

    monkeypatch.setattr(ResponseBuilder, "render_hit", render)


def wire_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `memory_search` return its wire hits again in a module whose
    other tests read the working rows."""
    monkeypatch.setattr(ResponseBuilder, "render_hit", _RENDER_HIT)


@pytest.fixture
def search_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`raw_hits` as a fixture."""
    raw_hits(monkeypatch)
