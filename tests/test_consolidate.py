"""Tests for the `bettermemory consolidate` module (T2.1 of the 1.6 plan).

Covers the four passes (dedup, demotion, cold-scope, scope-typo), the
keeper selection in dedup, dry-run vs apply semantics, and the
text/JSON rendering surfaces.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config
from bettermemory.consolidate import (
    AUTO_CONSOLIDATE_EVENT,
    ColdScopeSuggestion,
    ConsolidateReport,
    DedupCandidate,
    DemotionCandidate,
    ScopeTypoPair,
    _pick_keeper,
    _write_last_run,
    consolidate,
    find_cold_scopes,
    find_dedup_candidates,
    find_demotion_candidates,
    find_scope_typo_pairs,
    render_json,
    render_text,
    run_auto_consolidate,
)
from bettermemory.events import Recorder, iter_all_events, iter_events
from bettermemory.models import Category, Confidence, Memory, Source, generate_ulid
from bettermemory.server import (
    _cli_consolidate_acknowledge_debt,
    _cli_consolidate_acknowledge_misses,
)
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


def _memory(
    body: str,
    scopes: list[str] | None = None,
    *,
    created: datetime | None = None,
    updated: datetime | None = None,
    category: Category | None = None,
    verified_paths: list[str] | None = None,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=updated or now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
        category=category,
        verified_paths=verified_paths or [],
    )


# ---------------------------------------------------------------------------
# Keeper selection
# ---------------------------------------------------------------------------


def test_pick_keeper_more_recently_updated_wins() -> None:
    """Tier 1 of the keeper tiebreaker. Refining a memory implies that's
    the canonical version."""
    now = datetime.now(timezone.utc)
    older = _memory("body", updated=now - timedelta(days=10))
    newer = _memory("body", updated=now)
    keeper, dup = _pick_keeper(older, newer)
    assert keeper.id == newer.id
    assert dup.id == older.id


def test_pick_keeper_more_verified_paths_breaks_updated_tie() -> None:
    """Tier 2: same `updated`, the one with more attestation wins.
    Attestation is authority."""
    now = datetime.now(timezone.utc)
    a = _memory("body", updated=now, verified_paths=["/path/a"])
    b = _memory("body", updated=now, verified_paths=["/path/a", "/path/b"])
    keeper, _ = _pick_keeper(a, b)
    assert keeper.id == b.id


def test_pick_keeper_ulid_breaks_all_ties() -> None:
    """Tier 3: same `updated`, same verified_paths count — higher ULID
    wins (newer creation under microsecond-tied writes)."""
    now = datetime.now(timezone.utc)
    a = _memory("body", updated=now)
    b = _memory("body", updated=now)
    higher = a if a.id > b.id else b
    keeper, _ = _pick_keeper(a, b)
    assert keeper.id == higher.id


# ---------------------------------------------------------------------------
# Dedup pass
# ---------------------------------------------------------------------------


def test_dedup_no_pairs_for_single_memory() -> None:
    """Dedup requires at least two memories. One-memory corpus returns
    an empty candidate list with the chosen method label."""
    candidates, method = find_dedup_candidates([_memory("body")])
    assert candidates == []
    assert method == "jaccard"


def test_dedup_jaccard_finds_near_duplicates() -> None:
    """Two bodies with >75% token overlap should surface as a candidate.
    The newer one wins the keeper slot."""
    now = datetime.now(timezone.utc)
    a = _memory(
        "The user prefers terse code-driven explanations over prose.",
        updated=now - timedelta(days=2),
    )
    b = _memory(
        "The user prefers terse code-driven explanations over prose paragraphs.",
        updated=now,
    )
    distinct = _memory("Kubernetes networking notes for the homelab.")
    candidates, method = find_dedup_candidates([a, b, distinct])
    assert method == "jaccard"
    assert len(candidates) == 1
    assert candidates[0].keeper_id == b.id
    assert candidates[0].duplicate_id == a.id
    assert candidates[0].similarity > 0.75


def test_dedup_ignores_pairs_below_threshold() -> None:
    """A pair with token overlap below the threshold should not appear
    in the candidate list."""
    a = _memory("python list comprehension")
    b = _memory("kubernetes networking notes")
    candidates, _ = find_dedup_candidates([a, b])
    assert candidates == []


def test_dedup_pairs_sorted_by_similarity_desc() -> None:
    """Stronger matches come first so the caller can act on the most
    confident dedup proposals before drilling into ambiguous ones."""
    now = datetime.now(timezone.utc)
    # Two strong-similarity pairs of different strengths.
    a1 = _memory("apple banana cherry date", updated=now - timedelta(days=2))
    a2 = _memory(
        "apple banana cherry date elderberry", updated=now
    )  # very similar to a1
    b1 = _memory("kubernetes networking pods services", updated=now - timedelta(days=2))
    b2 = _memory(
        "kubernetes networking pods services ingress controllers", updated=now
    )  # similar but a longer non-overlap
    candidates, _ = find_dedup_candidates(
        [a1, a2, b1, b2],
        threshold=0.5,  # lowered to surface both pairs
    )
    assert len(candidates) >= 2
    # The first candidate's similarity must be >= the second's.
    assert candidates[0].similarity >= candidates[1].similarity


# ---------------------------------------------------------------------------
# Demotion pass
# ---------------------------------------------------------------------------


def test_demotion_identifies_retrieved_but_never_applied() -> None:
    """The core dead_weight rule: created before the window, retrieved
    at least once, applied zero times."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory("body", created=old, updated=old)
    events = [
        {"kind": "search", "hit_ids": [m.id]},
        {"kind": "search", "hit_ids": [m.id]},
        # No "applied" use event.
    ]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert len(candidates) == 1
    assert candidates[0].memory_id == m.id
    assert candidates[0].retrieved_count == 2


def test_demotion_skips_applied_memories() -> None:
    """Demotion is for retrieved-but-never-applied. An applied event
    means the memory is contributing, leave it alone."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory("body", created=old, updated=old)
    events = [
        {"kind": "search", "hit_ids": [m.id]},
        {"kind": "use", "ids": [m.id], "outcome": "applied"},
    ]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_skips_ambient_memories() -> None:
    """Ambient memories are structurally exempt from dead-weight — the
    use signal is implicit. Skip them in the demotion pass too."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory("body", created=old, updated=old, category=Category.AMBIENT)
    events = [{"kind": "search", "hit_ids": [m.id]}]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_skips_fresh_memories() -> None:
    """A memory created inside the window hasn't had a fair chance to
    accumulate use events yet. Skip until it ages."""
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(days=5)
    m = _memory("body", created=fresh, updated=fresh)
    events = [{"kind": "search", "hit_ids": [m.id]}]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_skips_never_retrieved() -> None:
    """A memory that's never been retrieved isn't dead weight — it's
    cold. Different bucket; different action; different curation
    question."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory("body", created=old, updated=old)
    candidates = find_demotion_candidates([m], [], window_days=30, now=now)
    assert candidates == []


def test_demotion_reads_returned_field_on_real_recorder_events(tmp_path: Path) -> None:
    """Regression: production `memory_search` events carry the result
    id list under the `returned` field (the canonical recorder shape),
    not `hit_ids` (the legacy synthetic-fixture name used elsewhere in
    this file). When the demotion scanner only consulted `hit_ids` it
    silently produced zero candidates against any real event log — the
    `bettermemory consolidate` demotion pass was dead in production
    while every test passed. This round-trips one event through the
    actual `Recorder` so a future refactor that drops the
    `returned`-aware code path is caught at suite time."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory("body", created=old, updated=old)

    recorder = Recorder(root=tmp_path, session_id="test-demotion")
    recorder.record("search", query="anything", returned=[m.id])

    from bettermemory.events import iter_events

    events = list(iter_events(tmp_path))
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert len(candidates) == 1
    assert candidates[0].memory_id == m.id
    assert candidates[0].retrieved_count == 1


# ---------------------------------------------------------------------------
# Cold-scope pass
# ---------------------------------------------------------------------------


def test_cold_scope_surfaces_when_newest_is_old_and_no_applies() -> None:
    """The simplest case: one scope, all its memories are old, no use
    events ever applied them."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    m = _memory("body", scopes=["projects:archived"], created=old, updated=old)
    suggestions = find_cold_scopes([m], [], cold_scope_days=180, now=now)
    assert len(suggestions) == 1
    assert suggestions[0].scope == "projects:archived"
    assert suggestions[0].memory_count == 1


def test_cold_scope_skipped_when_any_memory_applied() -> None:
    """A single applied event anywhere in the scope means the scope
    is firing value — don't suggest archiving."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    m = _memory("body", scopes=["projects:active"], created=old, updated=old)
    events = [{"kind": "use", "ids": [m.id], "outcome": "applied"}]
    suggestions = find_cold_scopes([m], events, cold_scope_days=180, now=now)
    assert suggestions == []


def test_cold_scope_skipped_when_recent_memory_in_scope() -> None:
    """The scope's newest memory anchors the cold-or-not decision. A
    fresh memory in the scope means the scope is still in use even
    if older memories never applied."""
    now = datetime.now(timezone.utc)
    old = _memory(
        "old body",
        scopes=["projects:foo"],
        created=now - timedelta(days=200),
        updated=now - timedelta(days=200),
    )
    fresh = _memory(
        "fresh body",
        scopes=["projects:foo"],
        created=now - timedelta(days=5),
        updated=now - timedelta(days=5),
    )
    suggestions = find_cold_scopes([old, fresh], [], cold_scope_days=180, now=now)
    assert suggestions == []


# ---------------------------------------------------------------------------
# Scope-typo pass
# ---------------------------------------------------------------------------


def test_scope_typo_pair_found_for_close_neighbors() -> None:
    """Two scopes within Levenshtein 2 of each other show up as a pair.
    The one with more memories is the keeper; the lesser is the typo."""
    a = _memory("body a", scopes=["projets:foo"])  # one typo
    b1 = _memory("body b1", scopes=["projects:foo"])
    b2 = _memory("body b2", scopes=["projects:foo"])
    pairs = find_scope_typo_pairs([a, b1, b2])
    assert len(pairs) == 1
    assert pairs[0].keeper == "projects:foo"
    assert pairs[0].typo == "projets:foo"
    assert pairs[0].keeper_count == 2
    assert pairs[0].typo_count == 1


def test_scope_typo_pair_not_found_for_distant_scopes() -> None:
    """Scopes that aren't typos of each other shouldn't pair up. Use a
    safe distance threshold to avoid noise."""
    a = _memory("body a", scopes=["projects:alpha"])
    b = _memory("body b", scopes=["infrastructure"])
    pairs = find_scope_typo_pairs([a, b], max_distance=2)
    assert pairs == []


# ---------------------------------------------------------------------------
# Orchestrator — end-to-end
# ---------------------------------------------------------------------------


def _write_two_near_duplicates(store: Store) -> tuple[Memory, Memory]:
    """Helper: write a pair of near-duplicate memories. The second one
    is then updated so its `updated` is strictly later than the
    first's — that drives keeper selection. Returns `(older, newer)`."""
    older = store.write(
        content="the user prefers terse code-driven explanations over prose",
        scopes=["tools"],
    )
    newer = store.write(
        content="the user prefers terse code-driven explanations over long prose",
        scopes=["tools"],
    )
    # Bump newer's `updated` so the keeper picker has a clean signal —
    # without the explicit update the two `updated` timestamps may
    # land within a microsecond of each other and the test relies on
    # the ULID tiebreaker, which is fragile under reordering.
    newer = store.update(newer)
    return older, newer


def test_consolidate_dry_run_does_not_modify_store(store: Store) -> None:
    """Dry-run must leave the store untouched. The whole point of the
    flag is "show me what would happen"."""
    older, newer = _write_two_near_duplicates(store)

    report = consolidate(store, apply=False)
    assert not report.applied
    assert report.dedup_candidates
    # Both memories still present on disk.
    remaining = store.load_all()
    assert {m.id for m in remaining} == {older.id, newer.id}


def test_consolidate_apply_tombstones_duplicates(store: Store) -> None:
    """With apply=True, the duplicate (older `updated`) is tombstoned
    and the keeper survives."""
    older, newer = _write_two_near_duplicates(store)

    report = consolidate(store, apply=True)
    assert report.applied
    assert any(a.kind == "tombstoned" for a in report.actions_taken)
    surviving_ids = {m.id for m in store.load_all()}
    assert newer.id in surviving_ids
    assert older.id not in surviving_ids


def test_consolidate_apply_demotes_dead_weight(store: Store, memory_dir: Path) -> None:
    """A dead-weight memory gets retagged to category=ambient. The
    body stays available; the category change suppresses future
    dead-weight flagging."""
    m = store.write(content="durable body content here", scopes=["tools"])

    # Seed the audit log with a retrieval (no applied event).
    rec = Recorder(root=memory_dir, session_id="test-session", enabled=True)
    rec.record("search", hit_ids=[m.id])

    # Shift `now` forward so the memory looks aged-out of the window —
    # creates the same observable state as backdating `created` would,
    # without bypassing the Store API.
    future_now = datetime.now(timezone.utc) + timedelta(days=60)
    report = consolidate(store, apply=True, window_days=30, now=future_now)
    assert any(a.kind == "demoted_to_ambient" for a in report.actions_taken)
    after = store.load_one(m.id)
    assert after.category == Category.AMBIENT


def test_consolidate_dedup_duplicate_seen_once_in_multi_pair(
    store: Store,
) -> None:
    """When a memory is similar to several others (e.g. duplicate triple),
    the tombstone action fires at most once per id. The report still
    surfaces all pairs so the caller sees the full set of similarities."""
    store.write(content="alpha beta gamma delta", scopes=["tools"])
    store.write(content="alpha beta gamma delta", scopes=["tools"])
    c = store.write(content="alpha beta gamma delta", scopes=["tools"])
    # Force c to be the strict winner by bumping its `updated`.
    c = store.update(c)

    report = consolidate(store, apply=True)
    tombstoned = [
        act.memory_id for act in report.actions_taken if act.kind == "tombstoned"
    ]
    # No id should appear twice.
    assert len(tombstoned) == len(set(tombstoned))
    # c is the keeper (newest); a and b are duplicates.
    assert c.id not in tombstoned


def test_consolidate_preserves_earlier_crowned_keeper_in_3way_cluster(
    store: Store,
) -> None:
    """Regression for commit 2a9c087.

    Scenario: three memories X / Y / Z where the pair (X, Y) has
    similarity 1.0 (identical bodies) and the pair (Z, X) has lower
    similarity (Z's body shares most tokens with X but adds one).
    Iteration order — created descending from `load_all` — and the
    sort-by-similarity-desc give the candidate list this shape:

        1. (keeper=X, duplicate=Y, sim=1.0)
        2. (keeper=Z, duplicate=X, sim≈0.78)

    Before the fix, applying the second pair would tombstone X — but
    X was already crowned keeper of pair 1, so Y's tombstone-reason
    citation would dangle to a removed memory. The `keepers_so_far`
    guard preserves X.

    The pre-existing 3-identical-bodies test exercises the
    duplicate-already-tombstoned guard (line 598), not this one
    (line 600); identical bodies and monotonic timestamps mean
    `load_all`'s newest-first ordering puts the newest as keeper of
    every pair, and the earlier-keeper-as-later-duplicate condition
    never fires. The bodies below are tuned so the two guards
    exercise different branches."""
    import time

    # Order matters: Y oldest, X middle, Z newest.
    # X and Y identical bodies → sim 1.0
    # Z differs from X by one token → sim ≈ 0.78 (above the 0.75 default)
    y = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf hotel",
        scopes=["tools"],
    )
    time.sleep(0.01)
    x = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf hotel",
        scopes=["tools"],
    )
    time.sleep(0.01)
    z = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf india",
        scopes=["tools"],
    )

    report = consolidate(store, apply=True)
    tombstoned = {
        act.memory_id for act in report.actions_taken if act.kind == "tombstoned"
    }
    assert y.id in tombstoned, "Y (oldest, identical to X) should be tombstoned"
    assert x.id not in tombstoned, (
        "regression: X was crowned keeper of (X, Y); the keepers_so_far "
        "guard must prevent (Z, X) from tombstoning X. Without the fix, "
        "X is tombstoned and Y's tombstone reason cites a dead memory."
    )
    assert z.id not in tombstoned

    # Y's tombstone reason should cite X — the canonical winner of the
    # (X, Y) pair — and the cited memory must still be alive.
    tombstones = store.load_tombstones()
    y_tomb = next(t for t in tombstones if t.id == y.id)
    assert x.id in (y_tomb.removed_reason or ""), (
        f"Y's tombstone-reason should cite X (the canonical winner of "
        f"(X, Y)); got: {y_tomb.removed_reason!r}"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_text_is_human_readable() -> None:
    """A consolidate report should produce a multi-section text report
    even when every bucket is empty — pinned so a future refactor
    can't silently drop a section header."""
    report = ConsolidateReport()
    text = render_text(report)
    assert "Consolidate report" in text
    assert "Dedup candidates" in text
    assert "Demotion candidates" in text
    assert "Cold-scope suggestions" in text
    assert "Scope-typo pairs" in text


def test_render_text_marks_dry_run_vs_applied() -> None:
    """The title line must make clear whether the report represents
    proposals or a record of actions actually taken — the difference
    is operationally critical."""
    dry = ConsolidateReport(applied=False)
    assert "dry-run" in render_text(dry).lower()
    applied = ConsolidateReport(applied=True)
    assert "applied" in render_text(applied).lower()


def test_failures_aggregated_and_rendered(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the L5 audit finding. The dedup apply loop used
    to log per-failure warnings but never aggregated them on the
    report. Ten disk-full errors would scroll past the user's
    terminal with no rollup. Now every failed `store.tombstone` is
    captured in `report.failures` and surfaces in both the JSON
    payload and the text rendering."""
    import time

    a = store.write(content="alpha beta gamma delta", scopes=["tools"])
    time.sleep(0.01)
    store.write(content="alpha beta gamma delta", scopes=["tools"])

    original_tombstone = Store.tombstone

    def failing_tombstone(self: Store, memory_id: str, **kwargs: Any) -> Any:
        raise OSError("simulated disk-full")

    monkeypatch.setattr(Store, "tombstone", failing_tombstone)

    report = consolidate(store, apply=True)
    # Restore so the test cleanup doesn't choke.
    monkeypatch.setattr(Store, "tombstone", original_tombstone)

    assert report.failures, "expected at least one captured failure"
    failure = report.failures[0]
    assert failure.kind == "tombstone"
    assert "disk-full" in failure.reason

    # The aggregated rollup must appear in the human-readable render.
    text = render_text(report)
    assert "Failures (" in text, f"failures section missing from render:\n{text}"
    assert "disk-full" in text

    # And in the JSON payload.
    import json as _json
    from bettermemory.consolidate import render_json

    payload = _json.loads(render_json(report))
    assert payload["failures"], "failures missing from JSON payload"
    assert payload["failures"][0]["kind"] == "tombstone"
    # The action that failed isn't recorded in actions_taken.
    assert a.id not in {act["memory_id"] for act in payload["actions_taken"]}


def test_render_json_is_valid_json_round_trippable() -> None:
    """The JSON surface is for CI/scripts. Must parse and contain every
    bucket the text surface shows."""
    report = ConsolidateReport(
        dedup_candidates=[
            DedupCandidate(
                keeper_id="kkk",
                keeper_summary="keeper text",
                duplicate_id="ddd",
                duplicate_summary="dup text",
                similarity=0.91,
                method="jaccard",
            )
        ],
        demotion_candidates=[
            DemotionCandidate(
                memory_id="mmm",
                summary="some body",
                age_days=42,
                retrieved_count=5,
                current_category="fact",
            )
        ],
        cold_scope_suggestions=[
            ColdScopeSuggestion(
                scope="projects:old",
                memory_count=3,
                most_recent_created_days_ago=400,
                suggestion="archive me",
            )
        ],
        scope_typo_pairs=[
            ScopeTypoPair(
                keeper="projects:foo",
                typo="projets:foo",
                distance=1,
                keeper_count=4,
                typo_count=1,
                suggestion="rename me",
            )
        ],
    )
    parsed = json.loads(render_json(report))
    assert parsed["dedup_candidates"][0]["keeper_id"] == "kkk"
    assert parsed["demotion_candidates"][0]["memory_id"] == "mmm"
    assert parsed["cold_scope_suggestions"][0]["scope"] == "projects:old"
    assert parsed["scope_typo_pairs"][0]["typo"] == "projets:foo"


def test_consolidate_empty_store_returns_empty_report(store: Store) -> None:
    """An empty store should produce an empty report, not crash. Common
    case for first-time `bettermemory consolidate` runs."""
    report = consolidate(store)
    assert report.dedup_candidates == []
    assert report.demotion_candidates == []
    assert report.cold_scope_suggestions == []
    assert report.scope_typo_pairs == []
    assert not report.applied


def test_consolidate_returns_method_label_when_no_semantic_model(
    store: Store,
) -> None:
    """Without a semantic model, the dedup method label must be
    `"jaccard"` so the consumer knows which threshold was used and
    what the false-positive profile looks like."""
    store.write(content="anything", scopes=["tools"])
    report = consolidate(store)
    assert report.dedup_method == "jaccard"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

# These tests invoke the `bettermemory` CLI script directly, which only
# works after `pip install -e .` (or a published install). CI runs
# `uv sync --extra dev` before pytest so the binary is present and the
# package is importable; a fresh local clone where the shim exists on
# $PATH but the editable install is broken (stale `.pth`, iCloud-sync
# UF_HIDDEN flag, etc.) would otherwise see these fail with
# `ModuleNotFoundError: bettermemory`. Probe with `--help` so the gate
# catches both "shim missing" and "shim broken" — the failure modes
# look identical from a developer's perspective. Mirrors the
# `shutil.which("git")` gate that protects test_sync.py.
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _cli_is_functional() -> bool:
    if _shutil.which("bettermemory") is None:
        return False
    try:
        result = _subprocess.run(
            ["bettermemory", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, _subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


_BETTERMEMORY_CLI_WORKS = _cli_is_functional()

_skip_without_cli = pytest.mark.skipif(
    not _BETTERMEMORY_CLI_WORKS,
    reason="`bettermemory` CLI not functional; run `pip install -e .` locally",
)


# ---------------------------------------------------------------------------
# acknowledge-debt CLI helper — direct in-process tests
# ---------------------------------------------------------------------------
#
# `_cli_consolidate_acknowledge_debt` clears the curation signal for
# memories the ranker keeps surfacing but the model never explicitly
# endorsed. The filter matches `compute_health`'s cold_endorsement_memories
# predicate exactly; these tests pin each predicate (high retrieval,
# zero explicit-applied, non-ambient) and confirm the event written
# is what `health._silent_miss_from_event` and the `applied_counts`
# walk on _handlers expect (kind=use, outcome=applied, auto=False).


def _seed_search_events(recorder: Recorder, memory_id: str, count: int) -> None:
    for _ in range(count):
        recorder.record("search", query="q", returned=[memory_id])


def test_acknowledge_debt_writes_explicit_use_event_for_unendorsed_memory(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory with retrieval_count >= floor and zero explicit applied
    events is written one `use(applied, auto=False)` event. The event
    shape is what `compute_health` reads on the next pass to clear the
    debt — same field set the model's deliberate `memory_record_use`
    would emit, with `attribution="cli_acknowledge_debt"` so the source
    of the endorsement is recoverable in the log."""
    m = store.write(content="durable note about indexer behavior", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=6)

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=False,
    )

    use_events = [e for e in iter_all_events(store.root) if e.get("kind") == "use"]
    assert len(use_events) == 1
    ev = use_events[0]
    assert ev["outcome"] == "applied"
    assert ev["auto"] is False
    assert ev["ids"] == [m.id]
    assert ev["attribution"] == "cli_acknowledge_debt"

    out = capsys.readouterr().out
    assert "1 explicit" in out
    assert m.id in out


def test_acknowledge_debt_skips_already_endorsed_memory(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory with even one explicit applied event in the log is
    already endorsed — no debt to clear, no event written. The
    discriminator is `auto=True`; an explicit applied with no `auto`
    flag (or `auto=False`) counts."""
    m = store.write(content="endorsed body", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=6)
    # One explicit applied — the model already endorsed this memory.
    recorder.record("use", ids=[m.id], outcome="applied", auto=False)

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=False,
    )

    use_events_after = [
        e
        for e in iter_all_events(store.root)
        if e.get("kind") == "use" and e.get("attribution") == "cli_acknowledge_debt"
    ]
    assert use_events_after == []
    assert "no cold-endorsement memories" in capsys.readouterr().out


def test_acknowledge_debt_does_not_count_auto_applied_as_endorsement(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The auto-fallback `_advance_turn` writes `use(applied, auto=True)`
    on every retrieval whose token aged out. The cold_endorsement_memories
    rollup excludes these — the whole point is to surface memories
    where every applied came from auto. Pin that an auto event does
    NOT block the acknowledgement."""
    m = store.write(content="auto-applied body", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=6)
    # Plenty of auto-applies — still debt, because none are explicit.
    for _ in range(3):
        recorder.record(
            "use", ids=[m.id], outcome="applied", auto=True, attribution="auto"
        )

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=False,
    )

    explicit = [
        e
        for e in iter_all_events(store.root)
        if e.get("kind") == "use" and e.get("auto") is False
    ]
    assert len(explicit) == 1
    assert explicit[0]["ids"] == [m.id]
    assert "1 explicit" in capsys.readouterr().out


def test_acknowledge_debt_skips_ambient_memory(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ambient memories are excluded by construction — their value is
    implicit (they shape responses without being cited) so an explicit
    use event for them is structurally rare and not a signal of
    weak endorsement. Mirrors the filter in `compute_health`."""
    m = store.write(
        content="ambient drift", scopes=["tools"], category=Category.AMBIENT
    )
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=10)

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=False,
    )

    cli_acks = [
        e
        for e in iter_all_events(store.root)
        if e.get("kind") == "use" and e.get("attribution") == "cli_acknowledge_debt"
    ]
    assert cli_acks == []
    assert "no cold-endorsement memories" in capsys.readouterr().out


def test_acknowledge_debt_skips_memory_below_retrieval_floor(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory retrieved fewer than `_ENDORSEMENT_DEBT_MIN_RETRIEVALS`
    (5) times isn't surfaced as debt — the rollup's floor exists so a
    one-off retrieval doesn't enter the bucket. Acknowledging it would
    create a false explicit signal where the audit had no opinion."""
    m = store.write(content="rare hit", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=4)

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=False,
    )

    cli_acks = [
        e
        for e in iter_all_events(store.root)
        if e.get("kind") == "use" and e.get("attribution") == "cli_acknowledge_debt"
    ]
    assert cli_acks == []
    assert "no cold-endorsement memories" in capsys.readouterr().out


def test_acknowledge_debt_json_output_carries_acknowledged_ids(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--json` path emits a parseable object with `acknowledged`,
    `floor`, and the full id list. Pins the JSON contract so a downstream
    consumer (CI script, dashboard) doesn't break on a stdout-format
    change."""
    m1 = store.write(content="alpha", scopes=["tools"])
    m2 = store.write(content="beta", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m1.id, count=6)
    _seed_search_events(recorder, m2.id, count=6)

    _cli_consolidate_acknowledge_debt(
        store=store,
        config=Config(),
        session_id="ack-cli",
        json_out=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["acknowledged"] == 2
    assert payload["floor"] == 5
    assert set(payload["ids"]) == {m1.id, m2.id}


# ---------------------------------------------------------------------------
# acknowledge-misses-before CLI helper — direct in-process tests
# ---------------------------------------------------------------------------
#
# `_cli_consolidate_acknowledge_misses` writes one additive
# `silent_miss_cutoff` event. The next `memory_health` /
# `memory_scope_overview` pass drops pre-cutoff `turn_audited` and
# `search_miss` events from the rollup. Tests pin the event shape, the
# input validation, the output channels, and the round-trip through
# `compute_health`.


def test_acknowledge_misses_writes_silent_miss_cutoff_event(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler writes exactly one `silent_miss_cutoff` event whose
    `cutoff_ts` round-trips through `_parse_ts`, with the same
    attribution scheme as `--acknowledge-debt`."""
    _cli_consolidate_acknowledge_misses(
        store=store,
        config=Config(),
        session_id="ack-cli",
        cutoff_ts="2026-05-25T05:25:35Z",
        json_out=False,
    )

    cutoff_events = [
        e for e in iter_all_events(store.root) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert len(cutoff_events) == 1
    ev = cutoff_events[0]
    assert ev["cutoff_ts"] == "2026-05-25T05:25:35Z"
    assert ev["attribution"] == "cli_acknowledge_misses"
    assert "2026-05-25T05:25:35Z" in capsys.readouterr().out


def test_acknowledge_misses_normalizes_offset_to_utc_z(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ISO timestamp with an explicit offset is stored canonicalised
    to UTC with a `Z` suffix — so every cutoff event in the log uses
    one representation regardless of which offset the caller passed."""
    _cli_consolidate_acknowledge_misses(
        store=store,
        config=Config(),
        session_id="ack-cli",
        cutoff_ts="2026-05-25T01:25:35-04:00",
        json_out=False,
    )

    capsys.readouterr()  # drain
    cutoff_events = [
        e for e in iter_all_events(store.root) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert cutoff_events[0]["cutoff_ts"] == "2026-05-25T05:25:35Z"


def test_acknowledge_misses_rejects_malformed_timestamp(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo'd timestamp surfaces as exit 1 + a clear stderr message
    rather than silently writing an event the rollup will then ignore."""
    with pytest.raises(SystemExit) as exc:
        _cli_consolidate_acknowledge_misses(
            store=store,
            config=Config(),
            session_id="ack-cli",
            cutoff_ts="not-a-timestamp",
            json_out=False,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "invalid ISO timestamp" in err
    # No event must have been written when validation failed.
    cutoff_events = [
        e for e in iter_all_events(store.root) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert cutoff_events == []


def test_acknowledge_misses_json_output_carries_canonical_cutoff(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--json` path emits a parseable object with the canonical
    cutoff_ts. Pins the JSON contract for downstream consumers."""
    _cli_consolidate_acknowledge_misses(
        store=store,
        config=Config(),
        session_id="ack-cli",
        cutoff_ts="2026-05-25T05:25:35Z",
        json_out=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"silent_miss_cutoff": "2026-05-25T05:25:35Z"}


def test_acknowledge_misses_rejects_naive_timestamp(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A naive ISO timestamp (no offset, no `Z`) is rejected up front.
    Silently stamping naive input as UTC was the previous behavior and
    produced off-by-zone cutoffs for non-UTC users with no warning."""
    with pytest.raises(SystemExit) as exc:
        _cli_consolidate_acknowledge_misses(
            store=store,
            config=Config(),
            session_id="ack-cli",
            cutoff_ts="2026-05-25T05:25:35",  # No offset, no Z.
            json_out=False,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "missing a UTC offset" in err
    # No event landed.
    cutoff_events = [
        e for e in iter_all_events(store.root) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert cutoff_events == []


def test_acknowledge_misses_refuses_when_telemetry_disabled(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI is itself a telemetry write; a disabled Recorder would
    silently no-op. Exit 1 with a clear message instead of writing
    nothing and returning success."""
    from bettermemory.config import TelemetryConfig

    config_no_telem = Config(telemetry=TelemetryConfig(enabled=False))
    with pytest.raises(SystemExit) as exc:
        _cli_consolidate_acknowledge_misses(
            store=store,
            config=config_no_telem,
            session_id="ack-cli",
            cutoff_ts="2026-05-25T05:25:35Z",
            json_out=False,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "telemetry is disabled" in err
    # No event landed (the Recorder wouldn't have written anyway, but
    # confirm the fail-fast prevented even that attempt).
    cutoff_events = [
        e for e in iter_all_events(store.root) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert cutoff_events == []


def test_acknowledge_misses_json_error_path_writes_nothing_to_stdout(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--json` on the invalid-timestamp path must not emit a partial
    JSON line or an empty `{}` to stdout — downstream consumers parse
    stdout as JSON and a stray byte breaks them. The error rides on
    stderr; stdout stays empty."""
    with pytest.raises(SystemExit) as exc:
        _cli_consolidate_acknowledge_misses(
            store=store,
            config=Config(),
            session_id="ack-cli",
            cutoff_ts="not-a-timestamp",
            json_out=True,
        )
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid ISO timestamp" in captured.err


def test_acknowledge_misses_event_clears_prior_miss_telemetry(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: pre-existing `search_miss` / `turn_audited` events
    are dropped from a subsequent `compute_health` rollup once the
    cutoff event is written."""
    from bettermemory.health import compute_health

    recorder = Recorder(root=store.root, session_id="seed")
    # Two pre-fix misses + two pre-fix audits.
    for _ in range(2):
        recorder.record("turn_audited")
        recorder.record("search_miss", probe_query="q", top_hit_ids=[])

    before = compute_health(
        store.load_all(),
        list(iter_all_events(store.root)),
        now=datetime.now(timezone.utc),
    )
    assert before.silent_misses.miss_total == 2
    assert before.silent_misses.audited_total == 2

    # Cutoff in the future — drops everything older.
    _cli_consolidate_acknowledge_misses(
        store=store,
        config=Config(),
        session_id="ack-cli",
        cutoff_ts=(datetime.now(timezone.utc) + timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        json_out=False,
    )
    capsys.readouterr()  # drain

    after = compute_health(
        store.load_all(),
        list(iter_all_events(store.root)),
        now=datetime.now(timezone.utc),
    )
    assert after.silent_misses.miss_total == 0
    assert after.silent_misses.audited_total == 0


@_skip_without_cli
async def test_cli_consolidate_via_subprocess(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end check: the `bettermemory consolidate` command runs
    against an empty store and prints a coherent dry-run report."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    import subprocess

    result = subprocess.run(
        ["bettermemory", "consolidate"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "BETTERMEMORY_DIR": str(memory_dir)},
        timeout=15,
    )
    assert result.returncode == 0
    assert "Consolidate report" in result.stdout
    assert "dry-run" in result.stdout


@_skip_without_cli
async def test_cli_consolidate_json_via_subprocess(
    memory_dir: Path,
) -> None:
    """The `--json` flag must produce parseable JSON with the expected
    top-level keys."""
    import subprocess

    result = subprocess.run(
        ["bettermemory", "consolidate", "--json"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "BETTERMEMORY_DIR": str(memory_dir)},
        timeout=15,
    )
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    for key in (
        "applied",
        "dedup_method",
        "dedup_candidates",
        "demotion_candidates",
        "cold_scope_suggestions",
        "scope_typo_pairs",
        "actions_taken",
    ):
        assert key in parsed


@_skip_without_cli
async def test_cli_consolidate_acknowledge_misses_via_subprocess(
    memory_dir: Path,
) -> None:
    """End-to-end argparse-wiring check: the
    `bettermemory consolidate --acknowledge-misses-before <ISO_TS>`
    invocation parses the flag, runs the in-process handler, and
    writes one `silent_miss_cutoff` event visible in the events log.
    The direct in-process tests above cover the handler logic; this
    test catches breakage in the argparse plumbing (typo'd flag
    name, missing `action=`, wrong dest, etc.) that the in-process
    tests would miss."""
    import subprocess

    cutoff = "2026-05-25T05:25:35Z"
    result = subprocess.run(
        [
            "bettermemory",
            "consolidate",
            "--acknowledge-misses-before",
            cutoff,
        ],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "BETTERMEMORY_DIR": str(memory_dir)},
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert cutoff in result.stdout
    # Confirm the event actually landed via the same code path the
    # rollup will use to read it back.
    cutoff_events = [
        e for e in iter_all_events(memory_dir) if e.get("kind") == "silent_miss_cutoff"
    ]
    assert len(cutoff_events) == 1
    assert cutoff_events[0]["cutoff_ts"] == cutoff


# ---------------------------------------------------------------------------
# run_auto_consolidate — the opt-in self-improving loop
# (debounced, bounded, conservative, reversible, recorded)
# ---------------------------------------------------------------------------


def test_auto_consolidate_applies_safe_subset_and_records_event(
    store: Store, memory_dir: Path
) -> None:
    """First run (no prior event) applies the structurally-safe subset —
    here, dedup of identical bodies — and records exactly one reviewable
    `auto_consolidate` event (the audit-transparency contract)."""
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])

    rec = Recorder(root=memory_dir, session_id="sess_auto")
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
    )
    assert result is not None
    assert result["status"] == "ran"
    assert result["tombstoned"] >= 1
    # One keeper remains active; the duplicate was tombstoned.
    assert len({m.id for m in store.load_all()}) == 1
    auto_events = [
        e for e in iter_events(memory_dir) if e.get("kind") == AUTO_CONSOLIDATE_EVENT
    ]
    assert len(auto_events) == 1
    assert auto_events[0]["status"] == "ran"
    assert auto_events[0]["tombstoned"] >= 1


def test_auto_consolidate_debounces_within_interval(
    store: Store, memory_dir: Path
) -> None:
    """A prior decision inside the window suppresses the run entirely —
    returns None, mutates nothing, records nothing."""
    store.write(content="alpha beta gamma delta", scopes=["tools"])
    store.write(content="alpha beta gamma delta", scopes=["tools"])

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _write_last_run(store.root, now - timedelta(hours=1))  # decided 1h ago
    rec = Recorder(root=memory_dir, session_id="sess_auto")
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
        now=now,
    )
    assert result is None
    assert len(store.load_all()) == 2  # untouched
    assert not (memory_dir / ".events.jsonl").exists()  # nothing recorded


def test_auto_consolidate_runs_after_interval_elapsed(
    store: Store, memory_dir: Path
) -> None:
    """A prior decision OLDER than the interval lets the pass run again."""
    store.write(content="alpha beta gamma delta epsilon", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon", scopes=["tools"])

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _write_last_run(store.root, now - timedelta(hours=48))  # decided 48h ago
    rec = Recorder(root=memory_dir, session_id="sess_auto")
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
        now=now,
    )
    assert result is not None
    assert result["status"] == "ran"
    assert len(store.load_all()) == 1


def test_auto_consolidate_skips_oversized_store(store: Store, memory_dir: Path) -> None:
    """Above max_memories the pass defers to manual consolidate: it records
    a skip event and mutates nothing (keeps the turn-end hook responsive)."""
    store.write(content="alpha beta gamma", scopes=["tools"])
    store.write(content="alpha beta gamma", scopes=["tools"])

    rec = Recorder(root=memory_dir, session_id="sess_auto")
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=1,  # below the 2-memory store
    )
    assert result is not None
    assert result["status"] == "skipped_store_too_large"
    assert len(store.load_all()) == 2  # nothing tombstoned
    auto_events = [
        e for e in iter_events(memory_dir) if e.get("kind") == AUTO_CONSOLIDATE_EVENT
    ]
    assert len(auto_events) == 1
    assert auto_events[0]["status"] == "skipped_store_too_large"


def test_auto_consolidate_tombstone_is_reversible(
    store: Store, memory_dir: Path
) -> None:
    """The reversal contract that makes unattended apply safe: an
    auto-applied dedup tombstone is restorable via the normal path."""
    store.write(content="alpha beta gamma delta epsilon zeta eta", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon zeta eta", scopes=["tools"])

    rec = Recorder(root=memory_dir, session_id="sess_auto")
    run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
    )
    tombstones = store.load_tombstones()
    assert len(tombstones) == 1
    restored = store.restore(tombstones[0].id)
    assert restored.id == tombstones[0].id
    assert len(store.load_all()) == 2  # both active again


def test_auto_consolidate_debounce_survives_event_log_rotation(
    store: Store, memory_dir: Path
) -> None:
    """Regression: the debounce clock lives in a sidecar file, not the
    rotating `.events.jsonl`. A rotation that archives the last
    `auto_consolidate` event must NOT make the next turn re-run an
    unscheduled pass while still inside the interval."""
    store.write(content="alpha beta gamma delta epsilon", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon", scopes=["tools"])

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rec = Recorder(root=memory_dir, session_id="sess_auto")
    first = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
        now=now,
    )
    assert first is not None and first["status"] == "ran"
    assert (store.root / ".auto_consolidate_last").exists()

    # Model a rotation: empty the active log (the prior auto_consolidate event
    # would now live in a .gz archive). Pre-fix this read as "never ran" and
    # fired again; the sidecar clock makes the next turn debounce instead.
    (memory_dir / ".events.jsonl").unlink(missing_ok=True)
    second = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
        now=now + timedelta(hours=1),
    )
    assert second is None


def test_auto_consolidate_demote_preserves_identity(
    store: Store, memory_dir: Path
) -> None:
    """The second auto-applied mutation (fact->ambient demote) must preserve
    id/created/body — only the category changes. Exercises run_auto_consolidate's
    demote path end-to-end, not just the manual consolidate() one."""
    store.write(content="a solitary stale fact worth keeping around", scopes=["tools"])
    before = store.load_all()[0]
    # Retrieved once, never applied → demotion candidate once it ages out.
    rec = Recorder(root=memory_dir, session_id="sess_auto")
    rec.record("search", query="anything", returned=[before.id])
    # Run far enough ahead that `before` is older than the 30d demote window.
    future = before.created + timedelta(days=60)
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
        now=future,
    )
    assert result is not None and result["status"] == "ran"
    assert result["demoted"] == 1
    after = store.load_all()
    assert len(after) == 1
    demoted = after[0]
    assert demoted.category == Category.AMBIENT
    assert demoted.id == before.id  # identity preserved through the retag
    assert demoted.created == before.created
    assert demoted.body == before.body


def test_auto_consolidate_dedup_keeper_retains_identity(
    store: Store, memory_dir: Path
) -> None:
    """When auto-dedup tombstones a near-duplicate, the survivor must be one
    of the originals with id/created/body intact — never a freshly minted
    record."""
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])
    originals = {m.id: m for m in store.load_all()}
    assert len(originals) == 2

    rec = Recorder(root=memory_dir, session_id="sess_auto")
    result = run_auto_consolidate(
        store,
        recorder=rec,
        session_id="sess_auto",
        interval_hours=24.0,
        max_memories=500,
    )
    assert result is not None and result["tombstoned"] == 1
    survivors = store.load_all()
    assert len(survivors) == 1
    keeper = survivors[0]
    assert keeper.id in originals  # an original, not a new id
    original = originals[keeper.id]
    assert keeper.created == original.created
    assert keeper.body == original.body
