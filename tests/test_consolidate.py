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
    last_verified_at: datetime | None = None,
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
        last_verified_at=last_verified_at,
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


def test_pick_keeper_attestation_beats_recency() -> None:
    """Tier 0: an attested member (non-empty verified_paths or
    last_verified_at set) wins outright, even against a more recently
    updated unattested one. Regression: metadata-only retags (including
    consolidate's own demotion pass) bump `updated`, so without this
    tier an unattested ambient husk retagged yesterday would beat — and
    tombstone — the verified fact. Content edits deliberately reset
    verification, so the attested body is by construction the
    spot-checked one."""
    now = datetime.now(timezone.utc)
    fact = _memory(
        "caddy reverse-proxies grafana on the nas",
        created=now - timedelta(days=60),
        updated=now - timedelta(days=60),
        category=Category.FACT,
        verified_paths=["/opt/stacks/proxy/Caddyfile"],
    )
    husk = _memory(
        "caddy reverse-proxies grafana on the nas",
        created=now - timedelta(days=60),
        updated=now - timedelta(days=1),
        category=Category.AMBIENT,
    )
    keeper, dup = _pick_keeper(fact, husk)
    assert keeper.id == fact.id
    assert dup.id == husk.id
    # Symmetric: argument order must not matter.
    keeper, dup = _pick_keeper(husk, fact)
    assert keeper.id == fact.id

    # last_verified_at alone (no verified_paths) also counts as
    # attestation — memory_verify sets it without bumping `updated`.
    verified = _memory(
        "caddy reverse-proxies grafana on the nas",
        created=now - timedelta(days=60),
        updated=now - timedelta(days=60),
        last_verified_at=now - timedelta(days=1),
    )
    keeper, _ = _pick_keeper(husk, verified)
    assert keeper.id == verified.id


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


def test_dedup_skips_opposite_polarity_pair() -> None:
    """Regression: 'do', 'no', and 'not' are stopwords, so 'Do not use
    sudo ...' and 'Use sudo ...' reduce to IDENTICAL token sets and
    score Jaccard 1.0 — above even the unattended 0.90 threshold, with
    zero headroom. That pair is a semantic contradiction the
    contradiction flow must arbitrate, not a duplicate to merge; the
    polarity guard must keep it out of the candidate list at both the
    manual (0.75) and the unattended (0.90) thresholds."""
    a = _memory("Do not use sudo for npm installs on this machine.")
    b = _memory("Use sudo for npm installs on this machine.")
    for threshold in (None, 0.90):
        candidates, _ = find_dedup_candidates([a, b], threshold=threshold)
        assert candidates == [], (
            f"opposite-polarity pair surfaced as a dedup candidate "
            f"at threshold={threshold}"
        )

    # Same-polarity negated bodies still dedup — the guard compares
    # polarity, it doesn't exempt negated bodies wholesale.
    c = _memory("Do not use sudo for npm installs on this machine.")
    candidates, _ = find_dedup_candidates([a, c])
    assert len(candidates) == 1


class _FixedVectorModel:
    """Stub embedding model: every body encodes to the same normalized
    vector, so every pair scores cosine 1.0 — above any threshold. The
    polarity guard is then the only thing between an opposite-polarity
    pair and a tombstone proposal."""

    def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
        return [1.0, 0.0]


def test_semantic_dedup_skips_opposite_polarity_pair() -> None:
    """Regression: round 84 added the polarity guard to the Jaccard loop
    only — `_find_dedup_semantic` went straight from the cosine threshold
    to `_pick_keeper`, and `consolidate --apply` tombstones every
    candidate without review, so an embedding model scoring 'Use X' /
    'Do not use X' above 0.85 (routine for sentence embeddings) got one
    side auto-tombstoned. The guard is method-independent: a negated
    pair is a contradiction to arbitrate, whichever scorer surfaced it."""
    a = _memory("Do not use sudo for npm installs on this machine.")
    b = _memory("Use sudo for npm installs on this machine.")
    candidates, method = find_dedup_candidates(
        [a, b], semantic_model=_FixedVectorModel()
    )
    assert method == "semantic"
    assert candidates == [], (
        "opposite-polarity pair surfaced as a semantic dedup candidate "
        "despite the polarity guard"
    )

    # Same-polarity pair still dedups at cosine 1.0 — the guard compares
    # polarity, it doesn't exempt negated bodies wholesale.
    c = _memory("Do not use sudo for npm installs on this machine.")
    candidates, method = find_dedup_candidates(
        [a, c], semantic_model=_FixedVectorModel()
    )
    assert method == "semantic"
    assert len(candidates) == 1


def test_polarity_guard_surfaces_skipped_pair_on_report(store: Store) -> None:
    """Regression (round-88): the polarity guard dropped above-threshold
    pairs with a bare `continue` — no log, counter, or report field —
    so a genuine duplicate pair caught by an incidental negator ("X
    instead of Y" vs "X, not Y", Jaccard ~0.83, identical meaning)
    accumulated invisibly forever; the dry-run report (the human-review
    surface) never showed it. The pair must land in `polarity_skipped`,
    stay out of `dedup_candidates`, and render suggest-only."""
    a = store.write(
        content="Use ripgrep instead of grep for repo-wide searches.",
        scopes=["tools"],
    )
    b = store.write(
        content="Use ripgrep, not grep, for repo-wide searches.",
        scopes=["tools"],
    )

    report = consolidate(store, apply=False)
    assert report.dedup_candidates == []
    assert len(report.polarity_skipped) == 1
    pair = report.polarity_skipped[0]
    assert {pair.memory_id_a, pair.memory_id_b} == {a.id, b.id}
    assert pair.similarity >= 0.75
    assert pair.method == "jaccard"

    # Suggest-only on the text surface...
    text = render_text(report)
    assert "Polarity-skipped pairs (1)" in text
    assert "review manually" in text
    # ...carried by the JSON surface...
    payload = json.loads(render_json(report))
    assert len(payload["polarity_skipped"]) == 1
    assert payload["polarity_skipped"][0]["method"] == "jaccard"
    # ...and absent entirely when nothing was skipped (exception
    # bucket, same convention as Failures).
    assert "Polarity-skipped" not in render_text(ConsolidateReport())


def test_polarity_skipped_pair_is_never_applied(store: Store) -> None:
    """The new observability must not change what gets tombstoned: the
    apply path iterates `dedup_candidates` only, so a polarity-skipped
    pair survives `apply=True` untouched."""
    a = store.write(
        content="Use ripgrep instead of grep for repo-wide searches.",
        scopes=["tools"],
    )
    b = store.write(
        content="Use ripgrep, not grep, for repo-wide searches.",
        scopes=["tools"],
    )

    report = consolidate(store, apply=True)
    assert len(report.polarity_skipped) == 1
    assert report.actions_taken == []
    assert {m.id for m in store.load_all()} == {a.id, b.id}


def test_semantic_polarity_skip_lands_on_report(store: Store) -> None:
    """Same observability on the semantic path: the guard fires after
    the threshold check, so the surfaced pair carries the similarity
    that would otherwise have made it a candidate."""
    a = store.write(
        content="Do not use sudo for npm installs on this machine.",
        scopes=["tools"],
    )
    b = store.write(
        content="Use sudo for npm installs on this machine.",
        scopes=["tools"],
    )

    report = consolidate(store, semantic_model=_FixedVectorModel())
    assert report.dedup_method == "semantic"
    assert report.dedup_candidates == []
    assert len(report.polarity_skipped) == 1
    pair = report.polarity_skipped[0]
    assert {pair.memory_id_a, pair.memory_id_b} == {a.id, b.id}
    assert pair.method == "semantic"
    assert pair.similarity == pytest.approx(1.0)


def test_dedup_shared_compound_does_not_inflate_jaccard() -> None:
    """Regression: symmetric kebab expansion in the shared token set
    multiplied a compound BOTH bodies share — expanding `docker-compose`
    on both sides added the same two part-tokens to intersection and
    union, lifting raw Jaccard 0.714 to 0.778 and proposing one of two
    DISTINCT per-environment facts for tombstoning at the 0.75
    manual-apply default. Pairwise-aware expansion leaves the shared
    compound as one token; the pair stays below threshold."""
    a = _memory("docker-compose stack restarts grafana automatically prod")
    b = _memory("docker-compose stack restarts grafana automatically dev")
    candidates, method = find_dedup_candidates([a, b])
    assert method == "jaccard"
    assert candidates == [], (
        "distinct per-environment facts crossed the dedup threshold "
        "purely from shared-compound kebab expansion"
    )

    # Cross-notation matching is preserved: a kebab body and its
    # spaced-out restatement still dedup (the compound is expanded
    # when the OTHER side lacks it).
    kebab = _memory("python-frontmatter library is unmaintained, vendored locally")
    spaced = _memory("python frontmatter library is unmaintained, vendored locally")
    candidates, _ = find_dedup_candidates([kebab, spaced])
    assert len(candidates) == 1


# Mirrors the provenance stamp `_apply_llm_proposal` appends to every
# `--llm --from-transcript` propose_new body: two distinct facts
# distilled from the same transcript turn share it VERBATIM, so its
# tokens dominate both Jaccard sets unless the dedup pass strips it.
_PROVENANCE_STAMP = (
    "\n\n_(consolidate --llm --from-transcript: My dotfiles live in "
    "~/dotfiles and I manage them with GNU stow; my shell is zsh with "
    "the starship prompt.)_"
)


def test_dedup_strips_shared_provenance_stamp_before_similarity() -> None:
    """Regression (round-88 RED): the provenance-stamp dedup exemption
    existed only at write time. Two DISTINCT facts distilled from the
    same transcript turn share the stamp verbatim; its tokens dominate
    the Jaccard sets (~0.93 stamped vs ~0.11 unstamped), crossing both
    the manual 0.75 default AND the unattended 0.90 threshold — so
    `consolidate --apply` (and the Stop hook's auto pass) tombstoned
    one genuine fact. Similarity must judge the claim, not the stamp —
    the same scoping the write gate applies."""
    a = _memory(
        "User manages dotfiles in ~/dotfiles with GNU stow." + _PROVENANCE_STAMP
    )
    b = _memory("User's shell is zsh with the starship prompt." + _PROVENANCE_STAMP)
    for threshold in (None, 0.90):
        candidates, method = find_dedup_candidates([a, b], threshold=threshold)
        assert method == "jaccard"
        assert candidates == [], (
            f"two distinct stamped facts surfaced as dedup candidates at "
            f"threshold={threshold} — stamp tokens leaked into similarity"
        )


def test_dedup_still_flags_genuine_duplicates_sharing_stamp() -> None:
    """Counterpart: stripping the stamp must not weaken true-duplicate
    detection. A genuinely duplicated claim pair (Jaccard ~0.857 once
    the stamp is stripped) sharing the same stamp still flags at the
    manual 0.75 default."""
    now = datetime.now(timezone.utc)
    a = _memory(
        "Prefers ripgrep over grep for repo-wide searches." + _PROVENANCE_STAMP,
        updated=now - timedelta(days=2),
    )
    b = _memory(
        "Prefers ripgrep over plain grep for repo-wide searches." + _PROVENANCE_STAMP,
        updated=now,
    )
    candidates, _ = find_dedup_candidates([a, b])
    assert len(candidates) == 1
    assert candidates[0].keeper_id == b.id
    assert candidates[0].duplicate_id == a.id
    assert candidates[0].similarity >= 0.75


class _StampAwareModel:
    """Stub embedding model that records every text it encodes and
    returns vectors keyed on content: anything still carrying the
    provenance stamp (or the dotfiles claim) encodes to [1, 0]; the
    zsh claim encodes to the orthogonal [0, 1]. If the dedup pass
    embeds raw stamped bodies — or reuses a stale full-body cache
    entry — the two DISTINCT facts collapse to cosine 1.0."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
        self.seen.append(text)
        if "consolidate --llm" in text or "stow" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_semantic_dedup_embeds_provenance_stripped_bodies() -> None:
    """The stamp strip applies to BOTH dedup paths: the semantic pass
    must embed the claim, not the stamped body."""
    model = _StampAwareModel()
    a = _memory(
        "User manages dotfiles in ~/dotfiles with GNU stow." + _PROVENANCE_STAMP
    )
    b = _memory("User's shell is zsh with the starship prompt." + _PROVENANCE_STAMP)
    candidates, method = find_dedup_candidates([a, b], semantic_model=model)
    assert method == "semantic"
    assert model.seen, "stub model was never asked to encode"
    assert all("consolidate --llm" not in text for text in model.seen), (
        "the semantic dedup pass embedded a stamped body"
    )
    assert candidates == []


def test_semantic_dedup_skips_stale_stamped_cache_entries() -> None:
    """`cached_embed` keys on (memory_id, updated_key) and never hashes
    the text, and the write path's `find_similar` embeds FULL stamped
    bodies under the unsalted key. Without the `#unstamped` key salt
    the dedup pass would hit those stale stamped vectors and the two
    distinct facts would collapse to cosine 1.0 again."""
    from bettermemory.semantic import cached_embed

    model = _StampAwareModel()
    a = _memory(
        "User manages dotfiles in ~/dotfiles with GNU stow." + _PROVENANCE_STAMP
    )
    b = _memory("User's shell is zsh with the starship prompt." + _PROVENANCE_STAMP)
    # Simulate the write path having already cached the stamped bodies.
    for m in (a, b):
        cached_embed(model, m.id, m.updated.isoformat(), m.body)
    candidates, _ = find_dedup_candidates([a, b], semantic_model=model)
    assert candidates == [], (
        "dedup reused a stale stamped-body embedding from the cache"
    )


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


def test_demotion_skips_minutes_old_first_retrieval() -> None:
    """Regression: the auto-applied endorsement structurally lags every
    retrieval by >= 2 memory-tool turns, so applied == 0 minutes after
    the first-ever retrieval carries no signal — the retrieval that
    proves the ranker works must not count as evidence against the
    memory at the very Stop hook that fired it. The earliest
    timestamped retrieval has to age past the endorsement grace before
    the memory is demotion-eligible."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=31)
    m = _memory(
        "Grafana admin UI runs on port 3001 on the NAS.", created=old, updated=old
    )
    events = [
        {
            "kind": "search",
            "returned": [m.id],
            "ts": (now - timedelta(minutes=2)).isoformat(),
        }
    ]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_skips_user_inference_memories() -> None:
    """Regression: the module docstring enumerates `fact` and (legacy)
    None as the demotion-eligible categories, but the pass used to skip
    only AMBIENT — user-inference sailed through to the unattended
    fact->ambient retag. The retag is one-way (memory_update's
    _PROPOSABLE_CATEGORIES gate cannot restore 'user-inference'), so
    the confirmation-protected tier must be whitelisted out."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=90)
    m = _memory(
        "User prefers code-driven tutorials over video walkthroughs.",
        scopes=["learning-style"],
        created=old,
        updated=old,
        category=Category.USER_INFERENCE,
    )
    events = [{"kind": "search", "hit_ids": [m.id]}]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_skips_recently_updated_or_verified_memories() -> None:
    """Regression: the grace window used to key only on `created`, so a
    90-day-old fact rewritten via memory_update or attested via
    memory_verify yesterday was still demoted unattended. The window
    keys on the latest maintenance touch — active maintenance is not
    rot."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=90)
    rewritten = _memory(
        "fact body rewritten yesterday",
        created=old,
        updated=now - timedelta(days=1),
    )
    verified = _memory(
        "fact body attested yesterday",
        created=old,
        updated=old,
        last_verified_at=now - timedelta(days=1),
    )
    events = [
        {"kind": "search", "hit_ids": [rewritten.id]},
        {"kind": "search", "hit_ids": [verified.id]},
    ]
    candidates = find_demotion_candidates(
        [rewritten, verified], events, window_days=30, now=now
    )
    assert candidates == []


def test_demotion_skips_unresolved_contradiction() -> None:
    """Regression: a contradicted memory is by construction retrieved-
    but-not-applied, so it satisfied the dead-weight triple — and the
    retag's `updated` bump then laundered health's unresolved-
    contradiction flag with zero resolution. A memory whose newest
    contradicted event postdates updated/last_verified_at is parked for
    explicit resolution, not dead weight."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    m = _memory(
        "Staging API base URL is https://staging.api.acme.dev", created=old, updated=old
    )
    events = [
        {
            "kind": "search",
            "returned": [m.id],
            "ts": (now - timedelta(days=10)).isoformat(),
        },
        {
            "kind": "use",
            "ids": [m.id],
            "outcome": "contradicted",
            "ts": (now - timedelta(days=5)).isoformat(),
        },
    ]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert candidates == []


def test_demotion_resolved_contradiction_is_still_a_candidate() -> None:
    """Mirror of the unresolved-contradiction skip: a contradiction
    OLDER than a later memory_update/memory_verify touch counts as
    resolved (same rule as health.MemoryStats.has_unresolved_
    contradiction), so the memory stays demotion-eligible."""
    now = datetime.now(timezone.utc)
    m = _memory(
        "Staging API base URL is https://staging2.api.acme.dev",
        created=now - timedelta(days=90),
        updated=now - timedelta(days=50),  # resolution touch
    )
    events = [
        {
            "kind": "search",
            "returned": [m.id],
            "ts": (now - timedelta(days=50)).isoformat(),
        },
        {
            "kind": "use",
            "ids": [m.id],
            "outcome": "contradicted",
            "ts": (now - timedelta(days=60)).isoformat(),  # pre-resolution
        },
    ]
    candidates = find_demotion_candidates([m], events, window_days=30, now=now)
    assert len(candidates) == 1
    assert candidates[0].memory_id == m.id


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
    # The Recorder stamps the event with the real current ts; evaluate
    # from 3 days later so the retrieval has aged past the endorsement
    # grace (a minutes-old first retrieval is now skipped — see
    # test_demotion_skips_minutes_old_first_retrieval).
    eval_now = now + timedelta(days=3)
    candidates = find_demotion_candidates([m], events, window_days=30, now=eval_now)
    assert len(candidates) == 1
    assert candidates[0].memory_id == m.id
    assert candidates[0].retrieved_count == 1


def test_consolidate_does_not_demote_fact_endorsed_in_rotated_archive(
    store: Store, memory_dir: Path
) -> None:
    """Regression: `consolidate()` must read the FULL event history
    (active log + rotated `.gz` archives) for the demotion pass, exactly
    as `memory_health` computes `dead_weight` from `iter_all_events`.

    The bug: `consolidate()` read `iter_events` (active log only). After a
    routine log rotation (telemetry.max_bytes), the `use(applied)`
    endorsement of a load-bearing FACT lives in a `.gz` archive and is
    invisible to the active-log-only read. The demotion pass then sees
    applied_count==0 and — for a memory retrieved since rotation and older
    than the window — demotes it fact->ambient on the UNATTENDED
    `run_auto_consolidate(apply=True)` Stop-hook path, with no human review.

    Setup mirrors the real split: the sole `applied` event sits in a
    rotated archive; a post-rotation `search` event sits in the active
    log. The FACT is older than the demote window. After the fix the
    memory must NOT be a demotion candidate — and `compute_health` (the
    canonical rule this pass mirrors) keeps it out of `dead_weight` for
    the identical inputs."""
    import gzip
    import json as _json

    from bettermemory.events import EVENT_LOG_FILENAME, iter_all_events
    from bettermemory.health import compute_health

    m = store.write(content="a load-bearing fact worth keeping", scopes=["tools"])
    assert (store.load_one(m.id).category or Category.FACT) != Category.AMBIENT

    # The sole `applied` endorsement lives ONLY in a rotated .gz archive —
    # exactly where a real rotation (telemetry.max_bytes) would have parked
    # it. Hand-craft the archive the same way test_events.py does so the
    # split is deterministic (a Recorder-driven rotation can't guarantee
    # which side of the boundary a given event lands on).
    applied_event = {
        "ts": "2026-01-01T00:00:00Z",
        "session": "pre-rotation",
        "kind": "use",
        "ids": [m.id],
        "outcome": "applied",
    }
    archive = memory_dir / ".events-20260101T000000Z.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write((_json.dumps(applied_event) + "\n").encode("utf-8"))

    # A post-rotation retrieval in the ACTIVE log: this is what makes the
    # memory look retrieved-since-rotation, the precondition that triggered
    # the spurious demotion. Round-trip through a real Recorder so the
    # active-log shape is production-faithful (`returned` field).
    rec = Recorder(root=memory_dir, session_id="post-rotation", enabled=True)
    rec.record("search", query="anything", returned=[m.id])
    # Sanity: the active log holds only the search; the applied lives in the
    # archive. iter_all_events stitches both back together chronologically.
    assert (memory_dir / EVENT_LOG_FILENAME).exists()
    all_events = list(iter_all_events(memory_dir))
    kinds = sorted(str(e.get("kind")) for e in all_events)
    assert kinds == ["search", "use"], kinds

    # Age the memory past the 30d demote window (shift `now`, same trick as
    # test_consolidate_apply_demotes_dead_weight — no created backdating).
    future_now = m.created + timedelta(days=60)

    # The fix: consolidate() reads iter_all_events, so the archived applied
    # event is counted and the memory is NOT a demotion candidate.
    report = consolidate(store, apply=False, window_days=30, now=future_now)
    demotion_ids = {d.memory_id for d in report.demotion_candidates}
    assert m.id not in demotion_ids, (
        "consolidate() demoted a FACT whose sole applied endorsement lives "
        "in a rotated .gz archive — it must read iter_all_events (full "
        "history) like health.py's dead_weight rule, not iter_events "
        "(active log only)."
    )

    # Cross-check the canonical rule: compute_health, fed the same full
    # history, keeps the memory out of dead_weight for the same reason.
    health = compute_health(
        store.load_all(), all_events, window_days=30, now=future_now
    )
    assert m.id not in {s.id for s in health.dead_weight}


# ---------------------------------------------------------------------------
# Cold-scope pass
# ---------------------------------------------------------------------------


def test_cold_scope_surfaces_when_newest_is_old_and_no_applies() -> None:
    """The simplest case: one scope, all its memories are old, no use
    events ever applied them. An applied event for an UNRELATED memory
    proves the applied signal exists (telemetry is on) — without any
    applied event anywhere the pass stays silent, see
    test_cold_scope_silent_when_no_applied_events_anywhere."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    m = _memory("body", scopes=["projects:archived"], created=old, updated=old)
    events = [
        {
            "kind": "use",
            "ids": ["unrelated-memory-id"],
            "outcome": "applied",
            "ts": now.isoformat(),
        }
    ]
    suggestions = find_cold_scopes([m], events, cold_scope_days=180, now=now)
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
    # Seed an unrelated applied event so the global telemetry gate
    # passes and the freshness exemption is what's actually pinned.
    events = [
        {
            "kind": "use",
            "ids": ["unrelated-memory-id"],
            "outcome": "applied",
            "ts": now.isoformat(),
        }
    ]
    suggestions = find_cold_scopes([old, fresh], events, cold_scope_days=180, now=now)
    assert suggestions == []


def test_cold_scope_silent_when_no_applied_events_anywhere() -> None:
    """Regression: with telemetry disabled (or a log predating
    telemetry) the event log carries zero applied events, making 'no
    applied events' vacuously true for EVERY stable scope older than
    the window. Pure absence of telemetry cannot distinguish dead
    scopes from healthy ones — the conservative default is silence."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    m1 = _memory(
        "Prefers code-driven tutorials over prose.",
        scopes=["learning-style"],
        created=old,
        updated=old,
    )
    m2 = _memory(
        "Time zone Europe/Stockholm.",
        scopes=["personal-context"],
        created=old,
        updated=old,
    )
    # A search event but no applied event anywhere — the applied signal
    # is structurally unavailable.
    events = [{"kind": "search", "returned": [m1.id], "ts": now.isoformat()}]
    suggestions = find_cold_scopes([m1, m2], events, cold_scope_days=180, now=now)
    assert suggestions == []


def test_cold_scope_skipped_when_memory_recently_updated() -> None:
    """Regression: scope freshness used to key on `created` alone, so a
    scope whose memory was rewritten 2 days ago (memory_update bumps
    `updated`, not `created`) was still flagged at 200 days stale.
    Freshness is the max across created/updated/last_verified_at —
    a rewrite this week is direct evidence the scope is maintained."""
    now = datetime.now(timezone.utc)
    m = _memory(
        "Homelab NAS runs Debian 13 (upgraded from 12 in June).",
        scopes=["infrastructure"],
        created=now - timedelta(days=200),
        updated=now - timedelta(days=2),
    )
    events = [
        {
            "kind": "use",
            "ids": ["unrelated-memory-id"],
            "outcome": "applied",
            "ts": now.isoformat(),
        }
    ]
    suggestions = find_cold_scopes([m], events, cold_scope_days=180, now=now)
    assert suggestions == []


def test_cold_scope_skips_all_ambient_scope() -> None:
    """Regression: ambient memories are exempt from applied-signal
    expectations everywhere else (models.py design note, health.py's
    dead_weight/cold exclusion, the demotion pass in this module), but
    the cold-scope pass used to flag all-ambient scopes for lacking the
    signal that is structurally absent for ambient by design."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    m1 = _memory(
        "User name and timezone identity note.",
        scopes=["personal-context"],
        created=old,
        updated=old,
        category=Category.AMBIENT,
    )
    m2 = _memory(
        "macOS + zsh + Tailscale environment.",
        scopes=["personal-context"],
        created=old,
        updated=old,
        category=Category.AMBIENT,
    )
    events = [
        {
            "kind": "use",
            "ids": ["unrelated-memory-id"],
            "outcome": "applied",
            "ts": now.isoformat(),
        }
    ]
    suggestions = find_cold_scopes([m1, m2], events, cold_scope_days=180, now=now)
    assert suggestions == []


def test_cold_scope_mixed_scope_with_never_applied_fact_still_flagged() -> None:
    """Mirror of the all-ambient exemption: a mixed scope keeps current
    behavior — its non-ambient member legitimately carries the applied
    expectation, so the scope is still suggested."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    ambient = _memory(
        "ambient identity note",
        scopes=["projects:old-mixed"],
        created=old,
        updated=old,
        category=Category.AMBIENT,
    )
    fact = _memory(
        "never-applied fact body",
        scopes=["projects:old-mixed"],
        created=old,
        updated=old,
        category=Category.FACT,
    )
    events = [
        {
            "kind": "use",
            "ids": ["unrelated-memory-id"],
            "outcome": "applied",
            "ts": now.isoformat(),
        }
    ]
    suggestions = find_cold_scopes(
        [ambient, fact], events, cold_scope_days=180, now=now
    )
    assert len(suggestions) == 1
    assert suggestions[0].scope == "projects:old-mixed"


def test_cold_scope_applied_event_older_than_window_does_not_exempt() -> None:
    """Regression: a single applied event EVER used to grant permanent
    immunity, structurally blinding the pass to its canonical target —
    a finished project scope that WAS useful while alive. A timestamped
    applied event older than cold_scope_days no longer proves current
    value, so the scope is flagged."""
    now = datetime.now(timezone.utc)
    m = _memory(
        "thesis pipeline runbook",
        scopes=["projects:thesis-pipeline"],
        created=now - timedelta(days=900),
        updated=now - timedelta(days=900),
    )
    events = [
        {
            "kind": "use",
            "ids": [m.id],
            "outcome": "applied",
            "ts": (now - timedelta(days=600)).isoformat(),
        }
    ]
    suggestions = find_cold_scopes([m], events, cold_scope_days=180, now=now)
    assert len(suggestions) == 1
    assert suggestions[0].scope == "projects:thesis-pipeline"


def test_cold_scope_applied_event_inside_window_exempts() -> None:
    """Counterpart to the aged-out test: an applied event INSIDE the
    cold window is live value — no suggestion. (An untimestamped
    applied event also keeps the exemption — the conservative default
    pinned by test_cold_scope_skipped_when_any_memory_applied.)"""
    now = datetime.now(timezone.utc)
    m = _memory(
        "thesis pipeline runbook",
        scopes=["projects:thesis-pipeline"],
        created=now - timedelta(days=900),
        updated=now - timedelta(days=900),
    )
    events = [
        {
            "kind": "use",
            "ids": [m.id],
            "outcome": "applied",
            "ts": (now - timedelta(days=10)).isoformat(),
        }
    ]
    suggestions = find_cold_scopes([m], events, cold_scope_days=180, now=now)
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


def test_scope_typo_no_pair_for_well_populated_neighbors() -> None:
    """Regression: two established multi-memory scopes at edit distance
    1-2 (projects:app / projects:api — both real projects) used to be
    flagged as a typo pair with an executable mass-rename suggestion.
    The singleton gate (typo side must hold exactly one memory) mirrors
    health.py's rare-scopes rule: a typo by definition accumulates ~one
    memory before being noticed."""
    memories = [
        _memory(f"app body {i}", scopes=["projects:app"]) for i in range(22)
    ] + [_memory(f"api body {i}", scopes=["projects:api"]) for i in range(15)]
    pairs = find_scope_typo_pairs(memories)
    assert pairs == []


def test_scope_typo_pair_not_found_for_distant_scopes() -> None:
    """Scopes that aren't typos of each other shouldn't pair up. Use a
    safe distance threshold to avoid noise."""
    a = _memory("body a", scopes=["projects:alpha"])
    b = _memory("body b", scopes=["infrastructure"])
    pairs = find_scope_typo_pairs([a, b], max_distance=2)
    assert pairs == []


def test_scope_typo_sibling_and_short_tail_singletons_not_flagged() -> None:
    """Regression (round-88): consolidate's raw whole-string
    Levenshtein-≤2 rule flagged aoc2024 → aoc2023 (deliberate successor
    scopes) and projects:api → projects:app (short distinct sibling
    tags) with copy-paste-ready `memory_rename_scope` commands — both
    singleton-sided, so the singleton gate couldn't save them — while
    health's rare-scopes detector correctly rejected both. The shared
    `_scope_typo_neighbor` rule (sibling-suffix exemption, length-scaled
    threshold) now backs both surfaces, so they can't diverge."""
    aoc = [
        _memory("solved day 25", scopes=["aoc2023"]),
        _memory("starting this year's puzzles", scopes=["aoc2024"]),
    ]
    assert find_scope_typo_pairs(aoc) == []

    short_tags = [
        _memory("api project body", scopes=["projects:api"]),
        _memory("app project body", scopes=["projects:app"]),
    ]
    assert find_scope_typo_pairs(short_tags) == []


def test_scope_typo_genuine_singleton_typo_still_flagged() -> None:
    """Counterpart: parity must not weaken genuine-typo detection. A
    transposition typo of an established scope (singleton side, long
    tail, distance 2) still flags with the rename suggestion — health's
    neighbor rule and consolidate's singleton gate agree on it."""
    memories = [
        _memory(f"body {i}", scopes=["projects:bettermemory"]) for i in range(3)
    ] + [_memory("typo'd body", scopes=["projects:bettermemoyr"])]
    pairs = find_scope_typo_pairs(memories)
    assert len(pairs) == 1
    assert pairs[0].keeper == "projects:bettermemory"
    assert pairs[0].typo == "projects:bettermemoyr"
    assert pairs[0].typo_count == 1
    assert "memory_rename_scope" in pairs[0].suggestion


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


def test_consolidate_apply_merges_duplicate_scopes_into_keeper(store: Store) -> None:
    """Regression: similarity is scope-blind, so identical boilerplate
    bodies in DIFFERENT project scopes dedup at 1.0 — and the apply
    loop used to tombstone the duplicate without merging its scopes
    into the keeper, silently removing the fact from one project's
    auto-scoped retrieval. The keeper must inherit the sorted union of
    both scope lists before the duplicate is tombstoned."""
    older = store.write(
        content="Run pnpm install then pnpm dev; node 20 required.",
        scopes=["projects:alpha"],
    )
    newer = store.write(
        content="Run pnpm install then pnpm dev; node 20 required.",
        scopes=["projects:beta"],
    )
    # Bump newer's `updated` so the keeper signal is unambiguous (same
    # trick as _write_two_near_duplicates).
    newer = store.update(newer)

    report = consolidate(store, apply=True)
    tombstoned = {
        act.memory_id for act in report.actions_taken if act.kind == "tombstoned"
    }
    assert older.id in tombstoned
    remaining = store.load_all()
    assert len(remaining) == 1
    keeper = remaining[0]
    assert keeper.id == newer.id
    assert keeper.scopes == ["projects:alpha", "projects:beta"], (
        "the duplicate's scope must be merged into the keeper before "
        "tombstoning, or the fact vanishes from projects:alpha's "
        f"auto-scoped retrieval; got scopes={keeper.scopes!r}"
    )


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


def test_consolidate_scope_merge_preserves_concurrent_verification(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (round-88): the dedup scope-merge is a scopes-only
    metadata edit, but called `store.update` without
    preserve_verification=True. `mark_verified` bumps `last_verified_at`
    WITHOUT bumping `updated` (verification is the orthogonal axis —
    store.py), so the W2 CAS structurally cannot catch a verify landing
    between consolidate's `load_all` snapshot and the merge write: the
    stale snapshot's empty verification fields silently clobbered the
    fresh attestation — which also feeds `_pick_keeper`'s Tier-0
    attested-beats-unattested rule and dead-weight classification on
    later passes."""
    older = store.write(
        content="Run pnpm install then pnpm dev; node 20 required.",
        scopes=["projects:alpha"],
    )
    newer = store.write(
        content="Run pnpm install then pnpm dev; node 20 required.",
        scopes=["projects:beta"],
    )
    newer = store.update(newer)  # unambiguous keeper signal

    real_load_all = Store.load_all
    fired = {"done": False}

    def racing_load_all(self: Store) -> list[Memory]:
        memories = real_load_all(self)
        if not fired["done"]:
            fired["done"] = True
            # The attestation lands AFTER consolidate snapshots the
            # store but BEFORE the scope-merge update — the interleave
            # the `updated` CAS cannot see.
            self.mark_verified(newer.id, verified_paths=["package.json"])
        return memories

    monkeypatch.setattr(Store, "load_all", racing_load_all)
    report = consolidate(store, apply=True)

    tombstoned = {
        act.memory_id for act in report.actions_taken if act.kind == "tombstoned"
    }
    assert older.id in tombstoned
    keeper = store.load_one(newer.id)
    assert keeper.scopes == ["projects:alpha", "projects:beta"]
    assert keeper.verified_paths == ["package.json"], (
        "scope merge clobbered the attestation that landed after the "
        "consolidate snapshot"
    )
    assert keeper.last_verified_at is not None


def test_consolidate_demotion_retag_preserves_concurrent_verification(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same metadata-only convention at the demotion retag: a verify
    landing between the apply pass's fresh `load_all` re-snapshot and
    the category-only update must survive the retag (the `updated` CAS
    can't see it — verify doesn't bump `updated`)."""
    m = store.write(content="durable body content here", scopes=["tools"])
    rec = Recorder(root=memory_dir, session_id="test-session", enabled=True)
    rec.record("search", hit_ids=[m.id])

    real_load_all = Store.load_all
    calls = {"count": 0}

    def racing_load_all(self: Store) -> list[Memory]:
        memories = real_load_all(self)
        calls["count"] += 1
        if calls["count"] == 2:
            # Call #1 is the candidate-finding snapshot; call #2 is the
            # apply pass's re-snapshot right before the retag loop. The
            # verify lands between that re-snapshot and the update.
            self.mark_verified(m.id, verified_paths=["src/build.py"])
        return memories

    monkeypatch.setattr(Store, "load_all", racing_load_all)
    future_now = datetime.now(timezone.utc) + timedelta(days=60)
    report = consolidate(store, apply=True, window_days=30, now=future_now)

    assert any(a.kind == "demoted_to_ambient" for a in report.actions_taken)
    after = store.load_one(m.id)
    assert after.category == Category.AMBIENT
    assert after.verified_paths == ["src/build.py"], (
        "demotion retag clobbered the attestation that landed after the "
        "apply pass's re-snapshot"
    )
    assert after.last_verified_at is not None


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


def test_consolidate_does_not_tombstone_against_already_tombstoned_keeper(
    store: Store,
) -> None:
    """Mirror twin of the keepers_so_far regression above.

    Bridge cluster Z–X–Y where X is the DUPLICATE of the higher-similarity
    pair and the KEEPER of the lower-similarity pair. With recency order
    Z > X > Y the candidates sort:

        1. (keeper=Z, duplicate=X, sim≈0.91)
        2. (keeper=X, duplicate=Y, sim≈0.83)
        3. (keeper=Z, duplicate=Y, sim=0.75)

    Pair 1 tombstones X. Before the fix, pair 2 then crowns the
    already-tombstoned X as Y's keeper and tombstones Y "near-duplicate
    of X" — so Y's tombstone reason cites a DEAD memory and Y's content is
    collapsed into a keeper that no longer exists in the active set. The
    `keeper_id in tombstoned_ids` guard skips pair 2; pair 3 then tombstones
    Y citing the still-live root Z. The invariant: no surviving tombstone's
    reason may cite a tombstoned id (a dangling 'where did this go?' hop).

    This is the symmetric case the original `keepers_so_far` guard did NOT
    cover (it caught earlier-keeper-becomes-later-duplicate; this is
    earlier-duplicate-whose-keeper-is-later-tombstoned). It fires unattended
    in `run_auto_consolidate` (the Stop hook), so the loss is silent.
    """
    import time

    # Order matters: Y oldest, X middle, Z newest (newest = pair keeper).
    y = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf hotel india kilo lima",
        scopes=["tools"],
    )
    time.sleep(0.01)
    x = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf hotel india juliet kilo",
        scopes=["tools"],
    )
    time.sleep(0.01)
    z = store.write(
        content="alpha beta gamma delta epsilon foxtrot golf hotel india juliet",
        scopes=["tools"],
    )

    report = consolidate(store, apply=True)
    tombstoned = {
        act.memory_id for act in report.actions_taken if act.kind == "tombstoned"
    }
    # X (the bridge duplicate of the highest-similarity pair) is removed,
    # and Z (the root keeper) survives.
    assert x.id in tombstoned, "X should be tombstoned as the near-duplicate of Z"
    assert z.id not in tombstoned, "Z (the root keeper) must survive"

    tombstones = store.load_tombstones()
    tombstone_ids = {t.id for t in tombstones}
    # The core invariant: NO surviving tombstone may cite a memory that is
    # itself tombstoned. Before the fix, Y's reason cited the dead X.
    for t in tombstones:
        reason = t.removed_reason or ""
        dangling = [tid for tid in tombstone_ids if tid != t.id and tid in reason]
        assert not dangling, (
            f"tombstone {t.id} cites tombstoned id(s) {dangling} — the "
            f"keeper-already-tombstoned guard regressed. reason: {reason!r}"
        )
    # Y, if tombstoned, must cite the live root Z — never the dead bridge X.
    if y.id in tombstoned:
        y_tomb = next(t for t in tombstones if t.id == y.id)
        assert z.id in (y_tomb.removed_reason or ""), (
            f"Y's tombstone should cite the live root Z, got: {y_tomb.removed_reason!r}"
        )
        assert x.id not in (y_tomb.removed_reason or "")


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


def test_acknowledge_debt_skips_pure_dead_weight_memory(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A memory retrieved >= floor with ZERO applied events of ANY kind
    (no auto, no explicit) is pure DEAD WEIGHT — not cold-endorsement.
    The canonical `health._is_weakly_endorsed` gates on
    `applied_count > 0` (returns False at applied_count == 0), so
    dead-weight rows route to the removal/demotion bucket, NOT here.

    Acknowledge-debt must NOT fabricate a `use(applied)` endorsement for
    them: doing so bumps applied_count to 1 and permanently shields a
    never-applied memory from dead-weight removal (`dead_weight` requires
    applied_count == 0). This is the CLI-path mirror of test_health.py's
    `test_zero_apply_memory_is_dead_weight_not_cold_endorsement`; the
    inline CLI filter previously omitted the `applied_count > 0` gate
    and wrongly endorsed these (pre-3.6.1 fail-open regression caught by
    the post-3.6.0 whole-tree sweep)."""
    m = store.write(content="durable note about indexer behavior", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=6)
    # No applied events at all -> applied_count == 0 -> dead weight, not debt.

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


def test_acknowledge_debt_refuses_when_telemetry_disabled(
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With telemetry disabled the Recorder is a hard no-op, so writing
    the explicit use(applied) endorsements would silently vanish while
    the CLI reported "wrote N events" and exited 0. Mirror the sibling
    `--acknowledge-misses-before` guard: refuse with exit 1 up front.
    Pins the pre-3.6.1 silent-no-op regression."""
    from bettermemory.config import TelemetryConfig

    m = store.write(content="cold endorsement body", scopes=["tools"])
    recorder = Recorder(root=store.root, session_id="seed")
    _seed_search_events(recorder, m.id, count=6)
    # A genuine cold-endorsement memory (one auto-apply, zero explicit)
    # so the ONLY reason to bail is the telemetry guard, not an empty set.
    recorder.record("use", ids=[m.id], outcome="applied", auto=True, attribution="auto")

    config_no_telem = Config(telemetry=TelemetryConfig(enabled=False))
    with pytest.raises(SystemExit) as exc:
        _cli_consolidate_acknowledge_debt(
            store=store,
            config=config_no_telem,
            session_id="ack-cli",
            json_out=False,
        )
    assert exc.value.code == 1
    assert "telemetry is disabled" in capsys.readouterr().err
    cli_acks = [
        e
        for e in iter_all_events(store.root)
        if e.get("kind") == "use" and e.get("attribution") == "cli_acknowledge_debt"
    ]
    assert cli_acks == []


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
    # Genuine cold-endorsement: each has >=1 auto-apply (applied_count > 0)
    # but zero explicit applies — the auto-fallback is doing all the work.
    # Pure-dead-weight (zero applies) is a DIFFERENT bucket (removal) and is
    # skipped here; see test_acknowledge_debt_skips_pure_dead_weight_memory.
    recorder.record(
        "use", ids=[m1.id], outcome="applied", auto=True, attribution="auto"
    )
    recorder.record(
        "use", ids=[m2.id], outcome="applied", auto=True, attribution="auto"
    )

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
