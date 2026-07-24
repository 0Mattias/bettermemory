"""Corpus-level contradiction candidates: detection, queue lifecycle,
and the memory_conflicts arbitration surface.

Covers the numeric-divergence guard's dedup-side effect too — before
3.28.0, "port 5432" vs "port 5433" was a DEDUP CANDIDATE and an
applying consolidate pass would have tombstoned one side on recency
rather than truth.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.conflicts import (
    ConflictQueue,
    conflicts_pending_count,
    find_conflict_candidates,
    scan_conflicts,
)
from bettermemory.consolidate import (
    _find_dedup_with_skips,
    _numeric_divergence,
    _numeric_token_set,
)
from bettermemory.events import Recorder
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _memory(body: str) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_T,
        updated=_T,
        scopes=["infrastructure"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


# ---------------------------------------------------------------------------
# Numeric divergence detector
# ---------------------------------------------------------------------------


def test_numeric_token_set_shapes() -> None:
    toks = _numeric_token_set(
        "postgres 16 runs on port 5432, release v3.27.0 cut 2026-07-20; "
        "see 01J0ABCDEFGHJKMNPQRSTVWXYZ for details"
    )
    assert "5432" in toks and "v3.27.0" in toks and "2026-07-20" in toks
    assert "16" in toks
    # 26-char ULID exceeds the length cap — identifiers are references,
    # not claims.
    assert not any(len(t) > 16 for t in toks)


def test_numeric_divergence_requires_mutual_difference() -> None:
    a = _numeric_token_set("postgres runs on port 5432")
    b = _numeric_token_set("postgres runs on port 5433")
    c = _numeric_token_set("postgres runs on port 5432 since v3")
    assert _numeric_divergence(a, b) is True
    # One-sided extra number = added detail, merges fine.
    assert _numeric_divergence(a, c) is False
    assert _numeric_divergence(a, _numeric_token_set("postgres runs fine")) is False


def test_numeric_divergent_pair_skips_dedup_and_surfaces() -> None:
    """The mis-curation regression: near-identical bodies disagreeing on
    a value must NOT be a dedup candidate (an applying pass would
    tombstone one side) — they surface as a conflict-shaped skip."""
    a = _memory(
        "the homelab postgres instance backing grafana metrics listens on tcp port 5432"
    )
    b = _memory(
        "the homelab postgres instance backing grafana metrics listens on tcp port 5433"
    )
    candidates, skipped, method = _find_dedup_with_skips([a, b])
    assert method == "jaccard"
    assert candidates == []
    assert len(skipped) == 1
    assert skipped[0].detector == "numeric"
    assert {skipped[0].memory_id_a, skipped[0].memory_id_b} == {a.id, b.id}


def test_polarity_pair_still_skips_with_polarity_detector() -> None:
    a = _memory("use sudo for the deploy script on the homelab host")
    b = _memory("do not use sudo for the deploy script on the homelab host")
    _, skipped, _ = _find_dedup_with_skips([a, b])
    assert len(skipped) == 1
    assert skipped[0].detector == "polarity"


def test_find_conflict_candidates_lifts_skips() -> None:
    a = _memory("grafana admin web dashboard port is 3000 on the homelab host machine")
    b = _memory("grafana admin web dashboard port is 3001 on the homelab host machine")
    cands = find_conflict_candidates([a, b])
    assert len(cands) == 1
    assert cands[0].detector == "numeric"
    assert cands[0].status == "pending"
    # Stable, order-independent id.
    assert cands[0].id == find_conflict_candidates([b, a])[0].id


# ---------------------------------------------------------------------------
# Queue lifecycle
# ---------------------------------------------------------------------------


def test_queue_upsert_resolve_and_resurrect(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    root.mkdir()
    a = _memory(
        "the alpha api service binds listen port 8080 on the shared docker host"
    )
    b = _memory(
        "the alpha api service binds listen port 8081 on the shared docker host"
    )
    by_id = {a.id: a, b.id: b}

    first = scan_conflicts(root, [a, b])
    assert first["added"] == 1 and first["pending_total"] == 1

    # Idempotent re-scan: refreshed, not duplicated.
    second = scan_conflicts(root, [a, b])
    assert second["added"] == 0 and second["refreshed"] == 1
    assert second["pending_total"] == 1

    queue = ConflictQueue(root)
    cand = queue.pending()[0]
    assert (
        queue.resolve(cand.id, status="dismissed", note="different services")
        is not None
    )
    assert conflicts_pending_count(root) == 0

    # Dismissal is sticky across scans while content is unchanged...
    third = scan_conflicts(root, [a, b])
    assert third["resurrected"] == 0 and third["pending_total"] == 0

    # ...but edited content resurrects the pair: the judged bodies are gone.
    a2 = a.model_copy(update={"updated": datetime.now(timezone.utc)})
    by_id[a.id] = a2
    fourth = ConflictQueue(root).upsert_scan(find_conflict_candidates([a2, b]), by_id)
    assert fourth["resurrected"] == 1
    assert conflicts_pending_count(root) == 1


def test_queue_gc_drops_rows_with_dead_members(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    root.mkdir()
    a = _memory("the beta metrics exporter claims scrape port 9090 on the shared host")
    b = _memory("the beta metrics exporter claims scrape port 9091 on the shared host")
    scan_conflicts(root, [a, b])
    assert conflicts_pending_count(root) == 1
    # b vanishes (tombstoned/merged): the next full scan drops the row.
    result = ConflictQueue(root).upsert_scan([], {a.id: a})
    assert result["dropped"] == 1
    assert conflicts_pending_count(root) == 0


# ---------------------------------------------------------------------------
# End-to-end: the memory_conflicts tool
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _seed_conflicting_pair(server: Any) -> tuple[str, str]:
    first = await _call(
        server,
        "memory_write",
        content=(
            "the homelab postgres instance backing grafana metrics "
            "listens on tcp port 5432"
        ),
        scopes=["infrastructure"],
    )
    # force=True: the write-time dedup gate would otherwise reject the
    # conflicting claim as a duplicate — which is exactly why corpus
    # scan exists: conflicting pairs mostly enter via force or via
    # drift between sessions.
    second = await _call(
        server,
        "memory_write",
        content=(
            "the homelab postgres instance backing grafana metrics "
            "listens on tcp port 5433"
        ),
        scopes=["infrastructure"],
        force=True,
    )
    return first["id"], second["id"]


async def test_e2e_scan_list_and_confirm_contradiction(memory_dir: Path) -> None:
    server = _build(memory_dir)
    a_id, b_id = await _seed_conflicting_pair(server)

    res = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert res["scan"]["added"] == 1
    assert res["pending_total"] == 1
    row = res["pending"][0]
    assert {row["a"]["id"], row["b"]["id"]} == {a_id, b_id}
    assert "port 543" in row["a"]["body"]
    assert row["detector"] == "numeric"

    verdictres = _unwrap(
        await _call(
            server,
            "memory_conflicts",
            resolve=row["id"],
            verdict="contradiction",
            note="one of these ports is stale",
        )
    )
    assert verdictres["resolved"]["status"] == "confirmed"
    assert verdictres["resolved"]["link_written"] is True
    assert verdictres["pending_total"] == 0

    # The contradicts link is now live on the retrieval surface.
    shown = _unwrap(await _call(server, "memory_show", id=row["a"]["id"]))
    links = shown.get("links") or []
    assert any(
        link.get("type") == "contradicts" and link.get("target_id") == row["b"]["id"]
        for link in links
    )

    # Confirmed is terminal: a re-scan does not resurrect the pair even
    # though the link-write bumped `updated`.
    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["pending_total"] == 0


async def test_e2e_compatible_dismissal_and_errors(memory_dir: Path) -> None:
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)
    res = _unwrap(await _call(server, "memory_conflicts", scan=True))
    cid = res["pending"][0]["id"]

    out = _unwrap(
        await _call(server, "memory_conflicts", resolve=cid, verdict="compatible")
    )
    assert out["resolved"]["status"] == "dismissed"
    assert out["pending_total"] == 0

    with pytest.raises(Exception):
        await _call(server, "memory_conflicts", resolve=cid, verdict="nonsense")
    with pytest.raises(Exception):
        await _call(
            server, "memory_conflicts", resolve="cf-missing", verdict="compatible"
        )


async def test_e2e_contradiction_verdict_refuses_when_target_is_dead(
    memory_dir: Path,
) -> None:
    """A contradiction verdict must check BOTH members, not just the
    link's source. A link whose target was tombstoned resolves to
    nothing at annotation time and the next scan GCs the queue row — the
    arbitration's only durable artifact would be invisible from the
    moment it was made."""
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)
    res = _unwrap(await _call(server, "memory_conflicts", scan=True))
    row = res["pending"][0]

    await _call(
        server,
        "memory_remove",
        id=row["b"]["id"],
        reason="the second port claim turned out to be a typo",
    )

    with pytest.raises(Exception, match="no longer active"):
        await _call(
            server,
            "memory_conflicts",
            resolve=row["id"],
            verdict="contradiction",
            note="one of these ports is stale",
        )

    # Refused, not half-applied: no dangling link on the surviving side,
    # and the row stays pending until the named remedy runs.
    shown = _unwrap(await _call(server, "memory_show", id=row["a"]["id"]))
    assert not (shown.get("links") or [])
    assert conflicts_pending_count(memory_dir) == 1
    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["pending_total"] == 0
    assert conflicts_pending_count(memory_dir) == 0


async def test_e2e_compatible_clears_standing_contradicts_links(
    memory_dir: Path,
) -> None:
    """The queue and the link layer are two authorities on one question.
    A `compatible` verdict that left a standing `contradicts` edge in
    place would leave them permanently disagreeing: the queue calls the
    pair settled while every retrieval keeps flagging it."""
    server = _build(memory_dir)
    a_id, b_id = await _seed_conflicting_pair(server)
    # Both directions — the confirm path only ever writes a→b, but the
    # relation is symmetric and retrieval annotates from either side.
    await _call(
        server,
        "memory_update",
        id=a_id,
        links=[{"type": "contradicts", "target_id": b_id}],
    )
    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "contradicts", "target_id": a_id}],
    )

    res = _unwrap(await _call(server, "memory_conflicts", scan=True))
    cid = res["pending"][0]["id"]
    out = _unwrap(
        await _call(
            server,
            "memory_conflicts",
            resolve=cid,
            verdict="compatible",
            note="two different services, two different ports",
        )
    )
    assert out["resolved"]["status"] == "dismissed"
    assert {c["source"] for c in out["resolved"]["links_cleared"]} == {a_id, b_id}

    for mid in (a_id, b_id):
        shown = _unwrap(await _call(server, "memory_show", id=mid))
        assert not any(
            link.get("type") == "contradicts" for link in (shown.get("links") or [])
        )

    # The clear lands BEFORE the verdict stamp, so its `updated` bump
    # cannot resurrect the very row it just settled.
    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["pending_total"] == 0
    assert conflicts_pending_count(memory_dir) == 0


async def test_e2e_applying_pass_gcs_dead_rows_without_fresh_skips(
    memory_dir: Path,
) -> None:
    """`upsert_scan` is the queue's only garbage collector, so the
    applying pass has to call it even when the scan found nothing fresh.
    Gated on fresh skips, a row whose member died stayed pending forever
    and `curation_pending.conflicts` kept advertising work that a
    curation pass would find nothing to do about."""
    server = _build(memory_dir)
    _a_id, b_id = await _seed_conflicting_pair(server)
    await _call(server, "memory_curate", dry_run=False)
    assert conflicts_pending_count(memory_dir) == 1

    await _call(
        server, "memory_remove", id=b_id, reason="the 5433 claim was plain wrong"
    )
    # One member left: this pass detects ZERO conflict-shaped skips.
    await _call(server, "memory_curate", dry_run=False)
    assert conflicts_pending_count(memory_dir) == 0
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert overview["curation_pending"]["conflicts"] == 0


async def test_e2e_applying_curate_feeds_queue(memory_dir: Path) -> None:
    """The Stop-hook / memory_curate apply path persists conflict-shaped
    skips automatically; dry-run stays side-effect free."""
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)

    await _call(server, "memory_curate", dry_run=True)
    assert conflicts_pending_count(memory_dir) == 0, "dry-run must not write"

    await _call(server, "memory_curate", dry_run=False)
    assert conflicts_pending_count(memory_dir) == 1

    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert overview["curation_pending"]["conflicts"] == 1
