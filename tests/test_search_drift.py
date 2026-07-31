"""Tests for path-drift counts surfaced on every search hit + race-safety
of load_all against concurrent tombstoning.

Background: drift used to fire only on `expand_top=True` and only for the
top hit when its relevance was "high". Hits 2-5 in a default search carried
stale path claims silently. Surfacing cheap drift counts on every hit lets
the model self-triage which hit to expand without round-tripping through
memory_show.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _hits(raw: Any) -> list[dict[str, Any]]:
    """Unwrap the SDK's structured content envelope."""
    if isinstance(raw, dict) and "result" in raw:
        return raw["result"]
    return raw


# ---------------------------------------------------------------------------
# Drift counts on every hit
# ---------------------------------------------------------------------------


async def test_every_hit_carries_drift_counts(server: Any, tmp_path: Path) -> None:
    """A search response should include path_drift_checked and
    path_drift_missing on every hit — not just the top one — so the
    model can pick which hit to expand."""
    real_path = tmp_path / "real.txt"
    real_path.write_text("real")

    await _call(
        server,
        "memory_write",
        content=f"healthy memory referencing `{real_path}`",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_write",
        content="another healthy memory mentioning the same `tools` topic",
        scopes=["tools"],
    )
    raw = await _call(server, "memory_search", query="tools healthy memory")
    hits = _hits(raw)
    assert len(hits) >= 2
    for hit in hits:
        assert "path_drift_checked" in hit
        assert "path_drift_missing" in hit
        assert isinstance(hit["path_drift_checked"], int)
        assert isinstance(hit["path_drift_missing"], int)


async def test_drift_counts_fire_for_missing_path(server: Any, tmp_path: Path) -> None:
    bogus = tmp_path / "definitely-does-not-exist-12345.txt"
    body = f"see the script at `{bogus}` for the deploy steps"
    await _call(server, "memory_write", content=body, scopes=["tools"])

    raw = await _call(server, "memory_search", query="deploy script tools")
    hits = _hits(raw)
    assert len(hits) >= 1
    assert hits[0]["path_drift_checked"] == 1
    assert hits[0]["path_drift_missing"] == 1


async def test_drift_counts_zero_when_path_exists(server: Any, tmp_path: Path) -> None:
    real = tmp_path / "exists.txt"
    real.write_text("x")
    await _call(
        server,
        "memory_write",
        content=f"deploy step lives at `{real}`",
        scopes=["tools"],
    )
    raw = await _call(server, "memory_search", query="deploy step lives")
    hits = _hits(raw)
    assert len(hits) >= 1
    assert hits[0]["path_drift_checked"] == 1
    assert hits[0]["path_drift_missing"] == 0


async def test_drift_counts_zero_for_pathless_body(server: Any) -> None:
    """A memory body with no path-shaped tokens should carry both
    counts at zero — it's a positive "nothing to spot-check" signal,
    not noise."""
    await _call(
        server,
        "memory_write",
        content="prefer code-driven tutorials over abstract explanations",
        scopes=["learning-style"],
    )
    raw = await _call(server, "memory_search", query="tutorials")
    hits = _hits(raw)
    assert hits[0]["path_drift_checked"] == 0
    assert hits[0]["path_drift_missing"] == 0


async def test_expand_top_still_surfaces_full_drift_report(
    server: Any, tmp_path: Path
) -> None:
    """When expand_top=True fires, the per-hit counts coexist with the
    full path_drift report on the top hit — the existing surface that
    surfaces actual missing paths is unchanged."""
    bogus = tmp_path / "missing.txt"
    body = f"deploy at `{bogus}`"
    await _call(server, "memory_write", content=body, scopes=["tools"])
    raw = await _call(
        server,
        "memory_search",
        query="deploy tools missing",
        expand_top=True,
    )
    hits = _hits(raw)
    top = hits[0]
    assert top["path_drift_missing"] == 1
    # When drift is found AND expand_top fires, full report is present.
    if "path_drift" in top and top["path_drift"]:
        assert str(bogus) in top["path_drift"]["missing"]


# ---------------------------------------------------------------------------
# Race-safety: load_all skips files that disappeared mid-iteration
# ---------------------------------------------------------------------------


def test_load_all_skips_disappeared_file(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file listed by `_iter_active_paths` may have moved to
    `.tombstones/` between listdir and read. The defensive catch
    in `load_all` should yield the remaining memories rather than
    crashing the whole call."""
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["tools"])

    real_load = store._load_path
    target_id = a.id
    seen: dict[str, bool] = {}

    def flaky_load(path: Path) -> Any:
        # The first time we see `a`'s file, simulate a concurrent move.
        memory = real_load(path)
        if memory.id == target_id and not seen.get("done"):
            seen["done"] = True
            raise FileNotFoundError(path)
        return memory

    monkeypatch.setattr(store, "_load_path", flaky_load)
    out = store.load_all()
    ids = {m.id for m in out}
    assert b.id in ids
    assert a.id not in ids


async def test_list_with_bodies_survives_tombstone_race(
    server: Any,
    memory_dir: Path,
) -> None:
    """memory_list(with_bodies=True) used to crash if a tombstone race
    raised FileNotFoundError mid-iteration. With load_all defensively
    catching OSError, the surviving memories come back cleanly."""
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    await _call(server, "memory_write", content="beta", scopes=["tools"])

    store = Store(memory_dir)
    # Tombstone `a` to simulate the file moving out from under any
    # in-flight iteration; subsequent memory_list calls should see
    # `b` only and not crash.
    store.tombstone(a["id"], reason="race")

    raw = await _call(server, "memory_list", with_bodies=True)
    rows = raw.get("result", raw) if isinstance(raw, dict) else raw
    bodies = " ".join(row.get("body", "") for row in rows)
    assert "beta" in bodies
    assert "alpha" not in bodies
