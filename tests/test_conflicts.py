"""Corpus-level contradiction candidates: detection, queue lifecycle,
and the memory_conflicts arbitration surface.

Covers the numeric-divergence guard's dedup-side effect too — before
3.28.0, "port 5432" vs "port 5433" was a DEDUP CANDIDATE and an
applying consolidate pass would have tombstoned one side on recency
rather than truth.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.conflicts import (
    ConflictCandidate,
    ConflictQueue,
    conflicts_pending_count,
    find_conflict_candidates,
    scan_conflicts,
    split_judgeable,
)
from bettermemory.consolidate import (
    _find_dedup_with_skips,
    _numeric_divergence,
    _numeric_token_set,
)
from bettermemory.events import Recorder, iter_events
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

# The verbatim adversarial bodies live with the fence that refuses them
# (`consolidate._pick_keeper`); one copy, because the whole point is that
# the text is exact.
from .test_consolidate import _ADVERSARIAL_PAIRS

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


@pytest.mark.parametrize(("body_a", "body_b", "detector"), _ADVERSARIAL_PAIRS)
def test_adversarial_pair_routes_to_the_queue_only(
    tmp_path: Path, body_a: str, body_b: str, detector: str
) -> None:
    """Every adversarial pair lands in the arbitration queue and in
    NEITHER dedup candidate list — the queue is the only exit.

    Driven at `threshold=0.6` so one test covers all three: two clear
    the shipped 0.75 Jaccard gate on their own, the numeric pair
    measures 0.667. Below a gate a pair is never compared, which also
    means it is never merged, so the lower threshold only makes the
    routing observable — it cannot manufacture a safety property.
    """
    root = tmp_path / "memories"
    root.mkdir()
    a, b = _memory(body_a), _memory(body_b)

    candidates, skipped, _method = _find_dedup_with_skips([a, b], threshold=0.6)
    assert candidates == []
    assert [p.detector for p in skipped] == [detector]

    counters = scan_conflicts(root, [a, b], threshold=0.6)
    assert counters["added"] == 1
    rows = ConflictQueue(root).pending()
    assert [r.detector for r in rows] == [detector]
    assert {rows[0].a_id, rows[0].b_id} == {a.id, b.id}
    assert rows[0].status == "pending"


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

    first = scan_conflicts(root, [a, b])
    assert first["added"] == 1 and first["pending_rows_on_disk"] == 1

    # Idempotent re-scan: refreshed, not duplicated.
    second = scan_conflicts(root, [a, b])
    assert second["added"] == 0 and second["refreshed"] == 1
    assert second["pending_rows_on_disk"] == 1

    queue = ConflictQueue(root)
    cand = queue.pending()[0]
    assert (
        queue.resolve(
            cand.id,
            status="dismissed",
            note="different services",
            member_bodies={a.id: a.body, b.id: b.body},
        )
        is not None
    )
    assert conflicts_pending_count(root) == 0

    # Dismissal is sticky across scans while content is unchanged — even
    # though `updated` moved on both members in the meantime, which is
    # what a link edit from arbitrating a neighbouring pair looks like.
    later = datetime.now(timezone.utc)
    touched = [m.model_copy(update={"updated": later}) for m in (a, b)]
    third = ConflictQueue(root).upsert_scan(
        find_conflict_candidates(touched), {m.id: m for m in touched}
    )
    assert third["resurrected"] == 0 and third["pending_rows_on_disk"] == 0

    # ...but edited BODY resurrects the pair: the judged content is gone.
    a2 = a.model_copy(
        update={
            "body": "the alpha api service binds listen port 8082 on the shared docker host",
            "updated": later,
        }
    )
    fourth = ConflictQueue(root).upsert_scan(
        find_conflict_candidates([a2, b]), {a2.id: a2, b.id: b}
    )
    assert fourth["resurrected"] == 1
    assert conflicts_pending_count(root) == 1
    # The stale fingerprints go with the verdict they belonged to.
    revived = ConflictQueue(root).pending()[0]
    assert revived.verdict_ts is None
    assert revived.verdict_hash_a is None and revived.verdict_hash_b is None


def test_dismissal_without_verdict_hashes_falls_back_to_updated(
    tmp_path: Path,
) -> None:
    """Rows dismissed before verdict fingerprints existed keep the old
    rule, so an upgrade cannot strand them as permanently sticky.

    They converge: this resurrection re-queues the pair, and the next
    dismissal records hashes.
    """
    root = tmp_path / "memories"
    root.mkdir()
    a = _memory("the gamma worker pool runs 4 processes on the shared docker host")
    b = _memory("the gamma worker pool runs 5 processes on the shared docker host")
    scan_conflicts(root, [a, b])
    cand = ConflictQueue(root).pending()[0]
    # No `member_bodies` — exactly the shape a pre-upgrade row has.
    resolved = ConflictQueue(root).resolve(
        cand.id, status="dismissed", note="two different pools"
    )
    assert resolved is not None
    assert resolved.verdict_hash_a is None and resolved.verdict_hash_b is None

    a2 = a.model_copy(update={"updated": datetime.now(timezone.utc)})
    out = ConflictQueue(root).upsert_scan(
        find_conflict_candidates([a2, b]), {a2.id: a2, b.id: b}
    )
    assert out["resurrected"] == 1


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
    assert result["gc_deferred"] == 0
    assert conflicts_pending_count(root) == 0


def test_split_judgeable_short_circuits_and_keeps_order() -> None:
    """The shared judgeable-row filter, used by both counting surfaces.

    Input order is preserved (callers sort by similarity before
    windowing) and `is_active` short-circuits, so an authority that pays
    real I/O per member never prices the second side of an already-dead
    pair.
    """

    def _cand(cid: str, a_id: str, b_id: str) -> ConflictCandidate:
        return ConflictCandidate(
            id=cid,
            a_id=a_id,
            b_id=b_id,
            summary_a="a",
            summary_b="b",
            similarity=0.9,
            method="jaccard",
            detector="numeric",
            created=_T.isoformat(),
        )

    queued = [
        _cand("cf-1", "live-1", "live-2"),
        _cand("cf-dead", "gone", "live-2"),
        _cand("cf-2", "live-2", "live-3"),
    ]
    asked: list[str] = []

    def is_active(memory_id: str) -> bool:
        asked.append(memory_id)
        return memory_id != "gone"

    judgeable, omitted = split_judgeable(queued, is_active)
    assert [c.id for c in judgeable] == ["cf-1", "cf-2"]
    assert omitted == 1
    # `cf-dead`'s live second member is never asked about — the dead
    # first side already settled the pair.
    assert asked == ["live-1", "live-2", "gone", "live-2", "live-3"]


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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _seed_pair(server: Any, body_a: str, body_b: str) -> tuple[str, str]:
    first = await _call(
        server, "memory_write", content=body_a, scopes=["infrastructure"]
    )
    # force=True: the write-time dedup gate would otherwise reject the
    # conflicting claim as a duplicate — which is exactly why corpus
    # scan exists: conflicting pairs mostly enter via force or via
    # drift between sessions.
    second = await _call(
        server, "memory_write", content=body_b, scopes=["infrastructure"], force=True
    )
    return first["id"], second["id"]


async def _seed_conflicting_pair(server: Any) -> tuple[str, str]:
    return await _seed_pair(
        server,
        (
            "the homelab postgres instance backing grafana metrics "
            "listens on tcp port 5432"
        ),
        (
            "the homelab postgres instance backing grafana metrics "
            "listens on tcp port 5433"
        ),
    )


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


async def test_e2e_dead_member_row_is_not_counted_as_pending(
    memory_dir: Path,
) -> None:
    """A row the listing path cannot render must not be counted either.

    Counted from the raw queue, one dead-member row made a single
    payload contradict itself three ways: `pending: []`, `pending_total:
    1`, and a hint saying there was nothing pending. `pending_total` is
    the number the model reads to decide whether arbitration work
    exists, so it has to agree with the list beside it.
    """
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    b_id = scanned["pending"][0]["b"]["id"]
    await _call(
        server, "memory_remove", id=b_id, reason="the 5433 claim was plain wrong"
    )

    listed = _unwrap(await _call(server, "memory_conflicts"))
    assert listed["pending"] == []
    assert listed["pending_total"] == len(listed["pending"]) == 0
    assert "no longer active" in listed["hint"]
    assert "scan=True" in listed["hint"]
    # The old text claimed nothing was queued while the total said 1.
    assert "No pending conflict candidates" not in listed["hint"]

    # Deliberate: listing is a read path and does not GC. The row lives
    # on disk until a scan — which is why the hint names the remedy —
    # and no count of arbitration work advertises it. (The raw row count
    # a scan reports, `pending_rows_on_disk`, does include it; this
    # listing carries no such counter, and the scan that does carries the
    # gap in its own `hint` — see the deferred-GC test below.)
    assert conflicts_pending_count(memory_dir) == 1
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert overview["curation_pending"]["conflicts"] == 0

    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["pending_total"] == 0
    assert conflicts_pending_count(memory_dir) == 0
    settled = _unwrap(await _call(server, "memory_conflicts"))
    assert settled["pending_total"] == 0
    assert settled["hint"].startswith("No pending conflict candidates")


async def test_e2e_scope_overview_conflicts_agrees_with_memory_conflicts(
    memory_dir: Path,
) -> None:
    """The session-start cue and the tool it points at must describe the
    same store.

    `curation_pending.conflicts` is what tells the model arbitration
    work exists and sends it to memory_conflicts. Counting rows that
    tool can neither list nor rule on made the two surfaces disagree
    outright — conflicts=1 beside a memory_conflicts response listing
    nothing — which teaches the model to stop following the cue.
    """
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)

    # Positive control first: with both members alive the cue is
    # non-zero and equal to the tool's own total, so the agreement
    # asserted below is not a filter that always answers zero.
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert scanned["pending_total"] == 1
    assert overview["curation_pending"]["conflicts"] == 1

    await _call(
        server,
        "memory_remove",
        id=scanned["pending"][0]["b"]["id"],
        reason="the 5433 claim was plain wrong",
    )

    listed = _unwrap(await _call(server, "memory_conflicts"))
    overview = _unwrap(await _call(server, "memory_scope_overview"))
    assert listed["pending"] == []
    assert overview["curation_pending"]["conflicts"] == listed["pending_total"] == 0
    # Agreement by a shared filter, not by a GC either surface ran: the
    # row is still pending on disk, waiting for a scan.
    assert conflicts_pending_count(memory_dir) == 1


async def test_scope_overview_conflicts_delta_excludes_dead_member_rows(
    memory_dir: Path,
) -> None:
    """The delta arm counts the same judgeable rows as the absolute one.

    A candidate detected after the prior-session boundary is new work; a
    candidate nobody can rule on is not work at all. The delta view is
    what the model branches on when deciding whether to *prompt* about
    curation, so a phantom there costs a whole prompted pass that finds
    nothing.
    """
    session_a = _build(memory_dir)
    await _seed_conflicting_pair(session_a)

    # Second session: detection runs now, so the candidate's `created`
    # postdates every session-A event and the delta arm sees it as new.
    session_b = _build(memory_dir)
    scanned = _unwrap(await _call(session_b, "memory_conflicts", scan=True))
    dead_id = scanned["pending"][0]["b"]["id"]
    overview = _unwrap(await _call(session_b, "memory_scope_overview"))
    delta = overview["curation_pending_new_since_last_session"]
    assert delta is not None, "no prior-session boundary — delta arm untested"
    assert overview["curation_pending"]["conflicts"] == delta["conflicts"] == 1

    await _call(
        session_b,
        "memory_remove",
        id=dead_id,
        reason="the 5433 claim was plain wrong",
    )

    overview = _unwrap(await _call(session_b, "memory_scope_overview"))
    delta = overview["curation_pending_new_since_last_session"]
    assert delta is not None
    assert overview["curation_pending"]["conflicts"] == 0
    assert delta["conflicts"] == 0
    assert conflicts_pending_count(memory_dir) == 1


async def test_e2e_pending_total_counts_past_the_max_results_window(
    memory_dir: Path,
) -> None:
    """Excluding unrenderable rows must not collapse the total into
    `len(pending)`: a caller whose list was truncated by `max_results`
    would then have no way to learn more is queued."""
    server = _build(memory_dir)
    await _seed_conflicting_pair(server)
    await _seed_pair(
        server,
        "the alpha api service binds its listen port 8080 on the shared docker host",
        "the alpha api service binds its listen port 8081 on the shared docker host",
    )
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert scanned["pending_total"] == 2

    windowed = _unwrap(await _call(server, "memory_conflicts", max_results=1))
    assert len(windowed["pending"]) == 1
    assert windowed["pending_total"] == 2
    assert "hint" not in windowed


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
    Gated on fresh skips, a row whose member died stayed `pending` on
    disk forever with nothing able to collect it — every later read
    re-paying a liveness check to keep excluding it, and the queue file
    growing rows no verdict can ever retire.

    The on-disk count is the load-bearing assertion here: the reporting
    surfaces filter dead rows out on their own now, so they would read
    zero either way. Only the raw count can tell GC from filtering."""
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


def _member_file(memory_dir: Path, memory_id: str) -> Path:
    return next(
        p for p in memory_dir.glob("*.md") if memory_id in p.read_text(encoding="utf-8")
    )


async def test_e2e_unreadable_member_file_does_not_destroy_a_settled_verdict(
    memory_dir: Path,
) -> None:
    """GC may not read "absent from the snapshot" as "the memory died".

    `Store.load_all` skips a file on `PARSE_SKIP_EXCEPTIONS`, which is
    `(Exception,)` — a truncated write, a bad `chmod`, a mid-tombstone
    race all present as a missing member. Collecting on that evidence is
    irreversible and destroys the row's status, `verdict_ts`, `note` and
    body fingerprints, and re-detection can only ever re-file the pair as
    `pending`: the arbitration is simply gone. So a snapshot holding
    fewer memories than the root holds files collects nothing.
    """
    server = _build(memory_dir)
    _a_id, b_id = await _seed_conflicting_pair(server)
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    cid = scanned["pending"][0]["id"]
    await _call(
        server,
        "memory_conflicts",
        resolve=cid,
        verdict="compatible",
        note="two different services, two different ports",
    )

    path = _member_file(memory_dir, b_id)
    original = path.read_text(encoding="utf-8")
    path.write_text("---\nid: [unterminated\n---\nbroken\n", encoding="utf-8")

    degraded = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert degraded["scan"]["gc_deferred"] == 1
    assert degraded["scan"]["dropped"] == 0
    settled = ConflictQueue(memory_dir).load()
    assert [(c.id, c.status, c.note) for c in settled] == [
        (cid, "dismissed", "two different services, two different ports")
    ]

    # The file was only transiently unreadable. Once it parses again the
    # verdict is still there, still sticky, and GC resumes.
    path.write_text(original, encoding="utf-8")
    healthy = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert healthy["scan"]["gc_deferred"] == 0
    assert healthy["scan"]["resurrected"] == 0
    assert healthy["pending_total"] == 0
    assert [c.status for c in ConflictQueue(memory_dir).load()] == ["dismissed"]


async def test_e2e_deferred_gc_scan_payload_is_self_consistent(
    memory_dir: Path,
) -> None:
    """A scan that DEFERS GC is the payload that reports a raw
    queue-file count and a judgeable count while the two disagree —
    leaving rows the tool cannot offer sitting in the file is exactly
    what deferring means. Those two numbers must not share a name.

    `upsert_scan`'s counter is the RAW on-disk row count; the top-level
    `pending_total` counts judgeable rows. Both shipped as
    `pending_total`, so a deferred scan answered "how many pending?"
    twice, 1 and 0, in one response and explained neither. The previous
    round made `pending_total` and `curation_pending.conflicts` agree for
    exactly this reason: a count that disagrees with what the tool can
    act on erodes the count.
    """
    server = _build(memory_dir)
    _a_id, b_id = await _seed_conflicting_pair(server)
    # A third memory to break below: deferral needs the snapshot to
    # under-count the root's `.md` files, and the pair's own two files
    # have to keep parsing for the row to stay judgeable-shaped.
    third = _unwrap(
        await _call(
            server,
            "memory_write",
            content="the nightly restic snapshot job uploads to the offsite bucket",
            scopes=["infrastructure"],
        )
    )
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    # Healthy pass: the two counts answer the same way, which is why the
    # collision needed the deferred pass below to surface at all.
    assert scanned["scan"]["pending_rows_on_disk"] == scanned["pending_total"] == 1
    assert "hint" not in scanned

    # One member dies, so its row is on disk and unjudgeable...
    await _call(
        server, "memory_remove", id=b_id, reason="the 5433 claim was plain wrong"
    )
    # ...and an unreadable third file makes the next scan defer
    # collection, so the row survives the pass that normally collects it.
    path = _member_file(memory_dir, third["id"])
    original = path.read_text(encoding="utf-8")
    path.write_text("---\nid: [unterminated\n---\nbroken\n", encoding="utf-8")

    degraded = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert degraded["scan"]["gc_deferred"] == 1
    assert conflicts_pending_count(memory_dir) == 1

    # Both numbers are reported, both are true, and each name says which
    # question it answers.
    assert degraded["scan"]["pending_rows_on_disk"] == 1
    assert degraded["pending_total"] == len(degraded["pending"]) == 0
    assert "pending_total" not in degraded["scan"]
    # Structural: no key name occurs twice in one payload, so no future
    # counter can reintroduce the collision under a different pair of
    # meanings.
    assert not set(degraded["scan"]) & set(degraded), degraded

    # And the gap is named, with its cause, in the payload that has one.
    assert "pending_rows_on_disk" in degraded["hint"]
    assert "gc_deferred=1" in degraded["hint"]
    assert "no longer active" in degraded["hint"]

    # Control: once the file parses again the pass collects, the counts
    # re-converge, and there is no gap left to explain.
    path.write_text(original, encoding="utf-8")
    healthy = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert healthy["scan"]["gc_deferred"] == 0
    assert healthy["scan"]["dropped"] == 1
    assert healthy["scan"]["pending_rows_on_disk"] == healthy["pending_total"] == 0
    assert "hint" not in healthy
    assert conflicts_pending_count(memory_dir) == 0


async def _seed_triangle(server: Any) -> list[str]:
    """Three near-identical bodies → three overlapping candidate pairs.

    The realistic shape: near-identical bodies cluster, so one memory
    routinely has several conflict partners and any rewrite of it touches
    every pair it sits in.
    """
    ids: list[str] = []
    for index, port in enumerate((8080, 8081, 8082)):
        written = _unwrap(
            await _call(
                server,
                "memory_write",
                content=(
                    "the alpha api service binds its listen "
                    f"port {port} on the shared docker host"
                ),
                scopes=["infrastructure"],
                **({"force": True} if index else {}),
            )
        )
        ids.append(written["id"])
    return ids


def _neighbour_of(rows: list[ConflictCandidate], row: ConflictCandidate) -> Any:
    """The other queued pair that shares `row`'s a-side member."""
    return next(r for r in rows if r.id != row.id and row.a_id in (r.a_id, r.b_id))


async def test_e2e_confirming_one_pair_does_not_resurrect_a_neighbour(
    memory_dir: Path,
) -> None:
    """A dismissal must only reopen for a change to ITS OWN pair.

    The confirm path writes a `contradicts` link, and `store.update`
    bumps `updated` on the memory it rewrites. Keyed on `updated`, that
    bump re-queued every dismissed pair the rewritten memory also sits
    in — arbitration of one pair spontaneously undoing the arbitration of
    another. A cue that reappears for reasons the model cannot connect to
    its own decision is the signal erosion that teaches it to ignore the
    cue.
    """
    server = _build(memory_dir)
    await _seed_triangle(server)
    await _call(server, "memory_conflicts", scan=True)
    rows = ConflictQueue(memory_dir).load()
    assert len(rows) == 3
    victim, neighbour = rows[0], _neighbour_of(rows, rows[0])

    dismissed = _unwrap(
        await _call(
            server,
            "memory_conflicts",
            resolve=neighbour.id,
            verdict="compatible",
            note="three different services",
        )
    )
    assert dismissed["resolved"]["status"] == "dismissed"

    confirmed = _unwrap(
        await _call(
            server,
            "memory_conflicts",
            resolve=victim.id,
            verdict="contradiction",
            note="one of these ports is stale",
        )
    )
    assert confirmed["resolved"]["link_written"] is True

    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["scan"]["resurrected"] == 0
    statuses = {c.id: c.status for c in ConflictQueue(memory_dir).load()}
    assert statuses[neighbour.id] == "dismissed"
    assert statuses[victim.id] == "confirmed"


async def test_e2e_dismissing_one_pair_does_not_resurrect_a_neighbour(
    memory_dir: Path,
) -> None:
    """The dismiss path rewrites memories too — `_clear_contradicts_links`
    strips the standing edge — so it carried the same `updated`-keyed
    hazard as the confirm path, one pair over."""
    server = _build(memory_dir)
    ids = await _seed_triangle(server)
    await _call(server, "memory_conflicts", scan=True)
    rows = ConflictQueue(memory_dir).load()
    victim, neighbour = rows[0], _neighbour_of(rows, rows[0])

    # Give the victim pair a standing edge, so dismissing it actually
    # rewrites both its members.
    await _call(
        server,
        "memory_update",
        id=victim.a_id,
        links=[{"type": "contradicts", "target_id": victim.b_id}],
    )
    assert victim.a_id in ids

    await _call(
        server,
        "memory_conflicts",
        resolve=neighbour.id,
        verdict="compatible",
        note="three different services",
    )
    cleared = _unwrap(
        await _call(
            server,
            "memory_conflicts",
            resolve=victim.id,
            verdict="compatible",
            note="also three different services",
        )
    )
    assert cleared["resolved"]["links_cleared"] == [
        {"source": victim.a_id, "target": victim.b_id}
    ]

    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["scan"]["resurrected"] == 0
    statuses = {c.id: c.status for c in ConflictQueue(memory_dir).load()}
    assert statuses[neighbour.id] == "dismissed"
    assert statuses[victim.id] == "dismissed"


async def test_e2e_dismissal_still_resurrects_on_a_real_body_edit(
    memory_dir: Path,
) -> None:
    """The positive control for the two tests above: making dismissals
    immune to unrelated bumps must not make them immune to the edit they
    exist to catch. Rewrite a judged body and the pair comes back."""
    server = _build(memory_dir)
    a_id, _b_id = await _seed_conflicting_pair(server)
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    cid = scanned["pending"][0]["id"]
    await _call(
        server, "memory_conflicts", resolve=cid, verdict="compatible", note="two hosts"
    )
    assert _unwrap(await _call(server, "memory_conflicts"))["pending_total"] == 0

    await _call(
        server,
        "memory_update",
        id=a_id,
        content=(
            "the homelab postgres instance backing grafana metrics "
            "listens on tcp port 5434"
        ),
    )
    rescan = _unwrap(await _call(server, "memory_conflicts", scan=True))
    assert rescan["scan"]["resurrected"] == 1
    assert rescan["pending_total"] == 1
    assert [c.id for c in ConflictQueue(memory_dir).pending()] == [cid]


async def test_e2e_compatible_verdict_is_recorded_in_the_event_log(
    memory_dir: Path,
) -> None:
    """A `compatible` verdict rewrites memories (it strips the standing
    `contradicts` edge) and retires a queue row. Only the contradiction
    branch reached `recorder.record`, so the one mutating operation with
    no audit-trail entry was the one that silently un-links memories."""
    server = _build(memory_dir)
    a_id, b_id = await _seed_conflicting_pair(server)
    await _call(
        server,
        "memory_update",
        id=a_id,
        links=[{"type": "contradicts", "target_id": b_id}],
    )
    scanned = _unwrap(await _call(server, "memory_conflicts", scan=True))
    cid = scanned["pending"][0]["id"]
    await _call(
        server,
        "memory_conflicts",
        resolve=cid,
        verdict="compatible",
        note="two different services",
    )

    verdicts = [
        e for e in iter_events(memory_dir) if e.get("kind") == "conflict_verdict"
    ]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "compatible"
    assert verdicts[0]["candidate"] == cid
    assert {verdicts[0]["a"], verdicts[0]["b"]} == {a_id, b_id}
    assert verdicts[0]["memories_rewritten"] == 1


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
