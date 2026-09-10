"""An index that cannot be read is not a clean bill on the trust rule.

`verified_locally_at` is the one fact separating a `last_verified_at` this
host made from one that arrived inside a pulled file, and it lives only in
the index. `index.trust_for` returned `{}` both for "these rows are not
classified yet" and for "the index could not be opened", and the read
surfaces treated `{}` as the first: the trust rule stood down per row, and
the file's own stamp — of unknown origin — was published as `fresh`. A
truncated index, a deleted one (the product's own advice on a version
skew) and a newer-schema one all produced identical false-fresh output on
`memory_search`, `memory_show` and `memory_list`, with no warning anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bettermemory import index
from bettermemory._response import TRUST_UNAVAILABLE_RECOMMENDATION
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=recorder
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _seed(memory_dir: Path) -> str:
    """A verified memory whose file carries a stamp, in a classified index."""
    store = Store(memory_dir)
    memory = store.write(
        content="the auth service listens on port 8443 behind the gateway",
        scopes=["tools"],
    )
    store.mark_verified(memory.id)
    index.rebuild(memory_dir, store.iter_active())
    return memory.id


def _truncate_index(memory_dir: Path) -> None:
    path = index.index_path(memory_dir)
    path.write_bytes(path.read_bytes()[:100])


def test_trust_for_is_none_when_the_index_cannot_speak(memory_dir: Path) -> None:
    mid = _seed(memory_dir)
    rows = index.trust_for(memory_dir, [mid])
    assert rows is not None and rows[mid].provenance == "local"
    assert rows[mid].verified_locally_at is not None
    assert index.trust_for(memory_dir, []) == {}, "nothing asked, nothing unknown"

    _truncate_index(memory_dir)
    assert index.trust_for(memory_dir, [mid]) is None, "torn: could not look"
    index.index_path(memory_dir).unlink()
    assert index.trust_for(memory_dir, [mid]) is None, "absent: could not look"


def _row_for(rows: list[dict[str, Any]], mid: str) -> dict[str, Any]:
    return next(r for r in rows if r["id"] == mid)


async def test_the_three_read_surfaces_say_when_trust_could_not_be_read(
    memory_dir: Path,
) -> None:
    mid = _seed(memory_dir)
    server = _build(memory_dir)

    hit = _row_for(await _call(server, "memory_search", query="auth service port"), mid)
    assert "trust_unavailable" not in hit
    assert hit["provenance"] == "local"
    assert hit["staleness_verdict"] == "fresh"
    shown = await _call(server, "memory_show", id=mid)
    assert "trust_unavailable" not in shown
    assert shown["provenance"] == "local"

    _truncate_index(memory_dir)

    hit = _row_for(await _call(server, "memory_search", query="auth service port"), mid)
    assert hit["trust_unavailable"] is True
    assert "provenance" not in hit, "no label can be derived; none is invented"
    assert hit["verification"]["status"] == "fresh", "the file did say it"
    assert hit["verification"]["recommendation"] == TRUST_UNAVAILABLE_RECOMMENDATION
    assert hit["staleness_verdict"] == "spot_check_required"

    shown = await _call(server, "memory_show", id=mid)
    assert shown["trust_unavailable"] is True
    assert shown["provenance"] is None
    assert shown["verification"]["recommendation"] == TRUST_UNAVAILABLE_RECOMMENDATION
    assert shown["staleness_verdict"] == "spot_check_required"

    listed = await _call(server, "memory_list")
    rows = listed["memories"] if isinstance(listed, dict) else listed
    row = _row_for(rows, mid)
    assert row["trust_unavailable"] is True
    assert row["staleness_verdict"] == "spot_check_required"


async def test_the_expanded_top_hit_keeps_the_demotion(memory_dir: Path) -> None:
    """`expand_top=True` re-derives the top hit's verdict from body-level
    signals and used to guard only against the `remote` status; the
    unavailable-trust demotion has to survive the same re-derivation."""
    mid = _seed(memory_dir)
    server = _build(memory_dir)
    _truncate_index(memory_dir)
    hits = await _call(
        server, "memory_search", query="auth service port", expand_top=True
    )
    hit = _row_for(hits, mid)
    assert "body" in hit, "premise: the top hit was expanded"
    assert hit["trust_unavailable"] is True
    assert hit["staleness_verdict"] == "spot_check_required"


async def test_an_unstamped_row_only_gains_the_flag(memory_dir: Path) -> None:
    """Nothing to demote: a never-verified row already reads
    `spot_check_required`; it still says the index could not be read."""
    store = Store(memory_dir)
    memory = store.write(content="the cache service listens on port 6379", scopes=["t"])
    index.rebuild(memory_dir, store.iter_active())
    server = _build(memory_dir)
    _truncate_index(memory_dir)
    hit = _row_for(
        await _call(server, "memory_search", query="cache service port"), memory.id
    )
    assert hit["trust_unavailable"] is True
    assert hit["verification"]["status"] == "never"
    assert hit["verification"]["recommendation"] != TRUST_UNAVAILABLE_RECOMMENDATION
