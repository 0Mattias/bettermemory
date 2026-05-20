"""Tests for the `bettermemory consolidate` module (T2.1 of the 1.6 plan).

Covers the four passes (dedup, demotion, cold-scope, scope-typo), the
keeper selection in dedup, dry-run vs apply semantics, and the
text/JSON rendering surfaces.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.consolidate import (
    ColdScopeSuggestion,
    ConsolidateReport,
    DedupCandidate,
    DemotionCandidate,
    ScopeTypoPair,
    _pick_keeper,
    consolidate,
    find_cold_scopes,
    find_dedup_candidates,
    find_demotion_candidates,
    find_scope_typo_pairs,
    render_json,
    render_text,
)
from bettermemory.events import Recorder
from bettermemory.models import Category, Confidence, Memory, Source, generate_ulid
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
# exists on $PATH after `pip install -e .` (or a published install). CI
# runs `uv sync --extra dev` before pytest so the binary is present;
# a fresh local clone without the editable install would otherwise see
# these fail with FileNotFoundError. Mirrors the `shutil.which("git")`
# gate that protects test_sync.py from the same issue.
import shutil as _shutil  # noqa: E402

_BETTERMEMORY_ON_PATH = _shutil.which("bettermemory") is not None

_skip_without_cli = pytest.mark.skipif(
    not _BETTERMEMORY_ON_PATH,
    reason="`bettermemory` CLI not on $PATH; run `pip install -e .` locally",
)


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
