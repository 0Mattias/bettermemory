"""Tests for BM25 scoring in search.py.

Separate file from test_search.py because the BM25 path lives next to the
keyword scorer and we want a clean signal when it regresses. The keyword
scorer's tests cover ordering invariants that BM25 should also satisfy;
these tests cover BM25-specific properties (IDF weighting, TF saturation,
length normalisation) that the keyword scorer never had.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import compute_idf, score_memory_bm25, search, tokenize


def _memory(
    body: str,
    scopes: list[str] | None = None,
    *,
    created: datetime | None = None,
    confidence: Confidence = Confidence.MEDIUM,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=confidence,
        source=Source.EXPLICIT,
        body=body,
    )


def test_compute_idf_empty_corpus() -> None:
    """No memories => empty IDF maps and zero avgdl. Callers can
    short-circuit on `avgdl == 0` rather than dividing by zero
    downstream."""
    body_idf, scope_idf, avgdl = compute_idf([])
    assert body_idf == {}
    assert scope_idf == {}
    assert avgdl == 0.0


def test_compute_idf_avgdl_is_average() -> None:
    """avgdl is the mean kebab-expanded stopword-stripped doc length. Two
    docs of length 3 and 5 (post-strip) average to 4."""
    short = _memory("python list comp")  # 3 tokens after stopword strip
    long = _memory("python list comprehension generator expressions")  # 5
    _, _, avgdl = compute_idf([short, long])
    assert avgdl == 4.0


def test_bm25_idf_rare_terms_outscore_common_terms() -> None:
    """A query for a rare term should score higher than a query for a common
    one, holding TF constant. This is the core BM25 property the keyword
    scorer doesn't have — TF alone treats "python" (everywhere) the same
    as "obscure" (one doc).
    """
    now = datetime.now(timezone.utc)
    rare_doc = _memory("python obscure")
    common1 = _memory("python code")
    common2 = _memory("python code")
    common3 = _memory("python code")
    corpus = [rare_doc, common1, common2, common3]

    body_idf, scope_idf, avgdl = compute_idf(corpus)

    rare_score, _ = score_memory_bm25(
        rare_doc,
        ["obscur"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    common_score, _ = score_memory_bm25(
        common1,
        ["cod"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert rare_score > common_score


def test_bm25_tf_saturation_diminishing_returns() -> None:
    """5x term frequency must not yield 5x score — that's the whole point
    of BM25's TF saturation curve. Anywhere between 1x and 5x is the
    acceptable band; in practice with k1=1.2 the ratio lands around 2.x."""
    now = datetime.now(timezone.utc)
    once = _memory("python")
    many = _memory("python python python python python")

    body_idf, scope_idf, avgdl = compute_idf([once, many])

    score_once, _ = score_memory_bm25(
        once,
        ["python"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    score_many, _ = score_memory_bm25(
        many,
        ["python"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )

    ratio = score_many / score_once
    assert 1.0 < ratio < 5.0


def test_bm25_length_normalisation_prefers_focused_docs() -> None:
    """Two docs that mention the query term once each: the shorter, more
    focused doc should score slightly higher. Length normalisation is the
    knob that does this; without `b > 0` BM25 would treat them equally."""
    now = datetime.now(timezone.utc)
    short = _memory("python notes")
    long = _memory(
        "python " + "filler words about other topics and unrelated content " * 20
    )

    body_idf, scope_idf, avgdl = compute_idf([short, long])

    s_short, _ = score_memory_bm25(
        short,
        ["python"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    s_long, _ = score_memory_bm25(
        long,
        ["python"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert s_short > s_long


def test_bm25_scope_match_contributes() -> None:
    """A query term that matches a memory's scope (but not its body) should
    still produce a positive score — the keyword scorer weights scopes 2x,
    BM25 mirrors that with `2 * idf` so RRF fusion doesn't accidentally
    drop scope signal."""
    now = datetime.now(timezone.utc)
    scoped = _memory("body without the keyword", scopes=["projects:alpha"])
    other = _memory("plain body text", scopes=["tools"])

    body_idf, scope_idf, avgdl = compute_idf([scoped, other])

    score, matched = score_memory_bm25(
        scoped,
        ["project"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert score > 0
    assert "project" in matched


def test_bm25_empty_query_returns_zero() -> None:
    """Defensive: empty query tokens => (0.0, []). Callers shouldn't
    have to special-case this before the scorer."""
    now = datetime.now(timezone.utc)
    m = _memory("any body")
    body_idf, scope_idf, avgdl = compute_idf([m])
    score, matched = score_memory_bm25(
        m, [], body_idf_map=body_idf, scope_idf_map=scope_idf, avgdl=avgdl, now=now
    )
    assert score == 0.0
    assert matched == []


def test_bm25_empty_corpus_returns_zero() -> None:
    """avgdl == 0 means the corpus is empty (or all bodies were empty
    after stopword strip). Scoring against an empty corpus should
    return zero rather than divide by zero."""
    now = datetime.now(timezone.utc)
    m = _memory("python notes")
    score, matched = score_memory_bm25(
        m, ["python"], body_idf_map={}, scope_idf_map={}, avgdl=0.0, now=now
    )
    assert score == 0.0
    assert matched == []


def test_bm25_recency_boost_applies() -> None:
    """A recently-updated memory should outscore an old one with identical
    body. Mirrors the keyword scorer's recency invariant so RRF doesn't
    lose freshness signal in hybrid mode."""
    now = datetime.now(timezone.utc)
    old = _memory("identical body words here", created=now - timedelta(days=180))
    new = _memory("identical body words here", created=now - timedelta(days=1))

    body_idf, scope_idf, avgdl = compute_idf([old, new])

    s_old, _ = score_memory_bm25(
        old,
        ["identical", "body"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    s_new, _ = score_memory_bm25(
        new,
        ["identical", "body"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert s_new > s_old


def test_bm25_matched_terms_deduplicated() -> None:
    """Repeated query tokens shouldn't appear twice in `matched_terms` —
    same contract as the keyword scorer. Consumer code on the MCP wire
    uses match_terms to render relevance, so duplication would be
    misleading."""
    now = datetime.now(timezone.utc)
    m = _memory("python python python")
    body_idf, scope_idf, avgdl = compute_idf([m])

    score, matched = score_memory_bm25(
        m,
        ["python", "python", "python"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert score > 0
    assert matched == ["python"]


def test_bm25_repeated_query_token_counts_once() -> None:
    """Stopword stripping turns reduplicated phrases ('end to end') into a
    repeated query token; the scoring loop must not re-add the saturated
    TF contribution per duplicate — that defeats TF saturation from the
    query side. Score must be identical with and without the duplicate."""
    now = datetime.now(timezone.utc)
    m = _memory("The front-end build uses Vite")
    body_idf, scope_idf, avgdl = compute_idf([m])

    dup, dup_matched = score_memory_bm25(
        m,
        ["end", "end", "testing"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    dedup, dedup_matched = score_memory_bm25(
        m,
        ["end", "testing"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert dup == dedup
    assert dup_matched == dedup_matched


def test_compute_idf_counts_scope_tokens_into_scope_df() -> None:
    """Scope tokens enter the per-doc document-frequency set of the
    SCOPE map, so a namespace token carried by every project-scoped
    memory ('projects') gets df≈N and a near-zero IDF — the `2.0 * idf`
    scope bonus then self-deflates instead of being priced off body
    rarity (where the token never appears and IDF would default high).
    avgdl stays body-only."""
    bodies = [
        "oat milk lattes from the corner shop",
        "restic snapshots run nightly",
        "kuma monitors ping every minute",
        "diun watches container image tags",
        "tailscale subnet router on the NAS",
    ]
    memories = [_memory(b, scopes=["projects:homelab"]) for b in bodies]
    _, scope_idf, _ = compute_idf(memories)
    # Present at all (previously absent: 'projects' is in no body)...
    assert "project" in scope_idf
    # ...but ubiquitous => far less discriminating than a one-doc term.
    assert scope_idf["project"] < scope_idf["restic"]


def test_compute_idf_body_map_excludes_scope_tokens() -> None:
    """The two-map split (round 88 audit): scope tokens deflate the SCOPE
    map only. The earlier single shared map fed BODY scoring too, which
    crushed the body-match weight of any term riding every candidate's
    scope — under auto-scoping that is the project's own name, the
    most-queried term of all. A term in one body and on every scope must
    keep rare-term pricing on the body side while the scope bonus still
    self-deflates."""
    bodies = [
        "restic snapshots run nightly",
        "kuma monitors ping every minute",
        "diun watches container image tags",
        "bettermemory crash traced to a stale index file",
    ]
    memories = [_memory(b, scopes=["projects:bettermemory"]) for b in bodies]
    body_idf, scope_idf, _ = compute_idf(memories)
    # Scope-only token: priced (deflated) in the scope map, absent from
    # the body map entirely.
    assert "project" in scope_idf
    assert "projects" not in body_idf
    # Body-rare + scope-ubiquitous term: high body IDF, deflated scope
    # IDF. Map keys are tokenize()'s index keys ('bettermemory' spells
    # 'bettermemori' under the final-y normalisation), so derive the key
    # rather than pinning the stem shape here.
    (key,) = tokenize("bettermemory")
    assert body_idf[key] > 1.0
    assert scope_idf[key] < 0.2


def test_bm25_body_match_not_crushed_by_pool_ubiquitous_scope_token() -> None:
    """Ranking-inversion regression (round 88 audit): with every
    candidate scoped projects:bettermemory — the standard auto-scoped
    pool — the shared df map priced a BODY mention of 'bettermemory'
    near zero (idf 0.0117 vs 3.356 body-only on the 42-doc repro), so
    'bettermemory crash' ranked a fresher memory that never mentions the
    project above the 30-day-old memory literally answering the query.
    With body IDF read from body-only df the genuine hit wins outright;
    the identical-on-both scope bonus stays deflated (see
    test_compute_idf_counts_scope_tokens_into_scope_df)."""
    now = datetime.now(timezone.utc)
    fillers = [
        "Uses ruff and mypy in CI for linting",
        "Restic snapshots run nightly at three",
        "Kuma monitors ping every minute",
        "Diun watches container image tags",
        "Tailscale subnet router runs on the NAS",
        "Prefers oat milk lattes from the corner shop",
        "Vendored frontmatter handling lives in the store module",
        "Episode handoffs summarize long loops",
        "Scope overview returns curation counts",
        "Tombstones are restorable for thirty days",
    ]
    corpus = [
        _memory(b, scopes=["projects:bettermemory"], created=now - timedelta(days=2))
        for b in fillers
    ]
    focal = _memory(
        "bettermemory crash on startup traced to a stale index file",
        scopes=["projects:bettermemory"],
        created=now - timedelta(days=30),
    )
    decoy = _memory(
        "MCP server crash loop: the crash repeats until systemd gives up",
        scopes=["projects:bettermemory"],
        created=now - timedelta(days=1),
    )
    hits = search(corpus + [focal, decoy], "bettermemory crash", mode="bm25", now=now)
    assert hits and hits[0].id == focal.id


def test_bm25_scope_namespace_token_does_not_outrank_body_match() -> None:
    """Ranking-inversion regression from the audit: for 'side projects',
    three unrelated project-scoped memories outscored (via the
    'projects' scope-prefix bonus, priced at body-derived IDF) the one
    memory matching BOTH query terms in its body — which ranked dead
    last. With scope tokens in the scope-side df map the genuine hit
    wins."""
    now = datetime.now(timezone.utc)
    noise_rows = [
        ("Prefers oat milk lattes from the corner shop", "projects:homelab"),
        ("Restic snapshots run nightly at three", "projects:homelab"),
        ("Uses ruff and mypy in CI", "projects:bettermemory"),
        ("Kuma monitors ping every minute", "projects:homelab"),
        ("Diun watches container image tags", "projects:homelab"),
        ("Tailscale subnet router runs on the NAS", "projects:homelab"),
    ]
    noise = [
        _memory(body, scopes=[scope], created=now - timedelta(days=2))
        for body, scope in noise_rows
    ]
    genuine = _memory(
        "Tracks side projects in a Notion board with a weekly review",
        scopes=["personal-context"],
        created=now - timedelta(days=2),
    )
    hits = search(noise + [genuine], "side projects", mode="bm25", now=now)
    assert hits and hits[0].id == genuine.id


def test_bm25_unknown_term_in_query_zero_contribution_but_others_still_score() -> None:
    """If a query token appears in no doc (no IDF entry), it should
    contribute zero from the body without poisoning the score for the
    matching terms in the same query. Realistic case: user types a typo
    next to a real term — the real term should still rank the doc."""
    now = datetime.now(timezone.utc)
    m = _memory("python notes")
    body_idf, scope_idf, avgdl = compute_idf([m])

    score, matched = score_memory_bm25(
        m,
        ["python", "qzzzzx"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert score > 0
    assert "python" in matched
    assert "qzzzzx" not in matched


def test_bm25_hyphenated_stopword_component_query_matches_spaced_body() -> None:
    """F9 regression: a hyphenated query whose components include a stopword
    ('end-to-end' -> ['end', 'to', 'end']) must still match a body that spells
    the phrase spaced ('end to end') in mode='bm25'. The conjunctive kebab
    fallback counts components off the stopword-STRIPPED body stream, where
    'to' has count 0 -> min(component_hits) was 0 -> silent zero recall in
    bm25 only (keyword/hybrid keep stopwords and were fine). Ranging the
    conjunction over the NON-stopword parts fixes it while preserving the
    'python-frontmatter' must-not-match-plain-'python' precision guard.
    """
    now = datetime.now(timezone.utc)
    # Body spells the phrase SPACED, so the query compound 'end-to-end' has
    # no direct hit and must go through the conjunctive fallback.
    focal = _memory("The end to end pipeline runs nightly and gates deploys")
    other = _memory("unrelated content here about widgets and gadgets")
    corpus = [focal, other]

    for mode in ("bm25", "keyword", "hybrid"):
        hits = search(corpus, "end-to-end", mode=mode, now=now)
        assert any(h.id == focal.id for h in hits), (
            f"mode={mode} failed to match spaced body for hyphenated query"
        )

    # Precision guard intact: 'python-frontmatter' (no stopword component)
    # must still NOT match a body of plain 'python'.
    guard_focal = _memory("python code and notes about scripting")
    guard_other = _memory("totally different subject matter entirely")
    guard_hits = search(
        [guard_focal, guard_other], "python-frontmatter", mode="bm25", now=now
    )
    assert not any(h.id == guard_focal.id for h in guard_hits)


def test_bm25_matches_keyword_for_x_to_x_compound_family() -> None:
    """F9 regression: the whole X-to-X / X-by-X compound family must match a
    spaced body in mode='bm25' exactly as it does in mode='keyword'.

    The conjunctive kebab fallback used to filter stopword components out of
    the parts and count the survivors against the stopword-STRIPPED content
    stream. That collapsed the conjunction: 'end-to-end' reduced to "any
    'end'", and 'to-do' (both parts stopwords) emptied the parts list and
    skipped the fallback entirely — and 'to-do' survives stopword stripping,
    so it never reached the stopword fallback either: silent zero recall in
    bm25 only. The 'to-do' iteration is the mutation-sound one — reverting the
    fix leaves keyword matching but bm25 not, breaking parity.
    """
    now = datetime.now(timezone.utc)
    other = _memory("totally unrelated widgets and gadgets galore")
    cases = [
        ("end-to-end", "The end to end pipeline runs nightly and gates deploys"),
        ("back-to-back", "We shipped two releases back to back last week"),
        ("to-do", "Add the migration to do list for the sprint planning"),
    ]
    for compound, body in cases:
        focal = _memory(body)
        corpus = [focal, other]
        bm = any(
            h.id == focal.id for h in search(corpus, compound, mode="bm25", now=now)
        )
        kw = any(
            h.id == focal.id for h in search(corpus, compound, mode="keyword", now=now)
        )
        assert kw, f"keyword baseline failed to match {compound!r}"
        assert bm == kw, f"bm25 diverged from keyword for {compound!r}"


def test_bm25_conjunction_requires_every_part_including_stopwords() -> None:
    """F9 precision: the fixed conjunction is a REAL conjunction over the
    UNFILTERED parts counted on the unstripped body — so 'end-to-end' matches
    a body only when 'end', 'to', AND 'end' all occur. A body that mentions
    'end' twice but never 'to' must NOT match.

    Mutation-sound: the pre-fix code filtered 'to' out and counted the
    duplicated ['end','end'] against the stripped stream, so this body DID
    match. Reverting the fix flips this assertion.
    """
    now = datetime.now(timezone.utc)
    other = _memory("totally unrelated widgets and gadgets galore")
    # 'end' twice, no 'to' anywhere.
    no_to = _memory("The end result matched the end goal exactly")
    hits = search([no_to, other], "end-to-end", mode="bm25", now=now)
    assert not any(h.id == no_to.id for h in hits)


def test_find_similar_flags_short_restatement_contained_in_long_body() -> None:
    """F14 regression: a short (~10-token) near-verbatim restatement of the
    first sentence of a long (~48-token) memory scores ~0.18 Jaccard, below
    the 0.40 'related' floor (the long body dominates the union), and used to
    commit SILENTLY. The containment score |intersection|/min, gated on
    large size asymmetry, now surfaces it as at-least-'related'.
    """
    from bettermemory.search import find_similar

    first = (
        "The deploy pipeline builds the Docker image tags it with the commit "
        "SHA and pushes it to the registry before the staging rollout begins."
    )
    filler = " ".join(
        [
            "Afterwards the health checks poll the readiness endpoint until it "
            "returns success or the timeout elapses and the rollout is aborted "
            "with a notification to the on-call channel.",
            "Rollback restores the previous image reference and re-runs the "
            "smoke suite while the engineer inspects the dashboards for "
            "elevated error rates across the fleet.",
        ]
        * 6
    )
    long_mem = _memory(first + " " + filler)
    short_restatement = (
        "The deploy pipeline builds the Docker image tagging it with the "
        "commit SHA then pushes it to the registry."
    )

    hits = find_similar(short_restatement, [long_mem])
    assert hits, "short restatement contained in a long body was not flagged"
    assert hits[0].relevance in ("high", "medium")

    # Guard against over-triggering: a distinct short memory that shares
    # no content tokens with a long one must NOT be flagged.
    distinct_short = "Prefers oat milk lattes from the corner shop near the office."
    distinct_hits = find_similar(distinct_short, [long_mem])
    assert not distinct_hits


def test_find_similar_short_common_vocab_not_silently_dropped() -> None:
    """F14 over-trigger guard: containment must never SILENTLY drop a write.

    A short, topically-distinct fact whose few words all happen to appear in
    a long unrelated memory used to reach containment ~1.0 -> 'high' -> the
    ingest dedup gate's `skip_duplicate` (it blocks only on a 'high' active
    hit). Two guards now prevent that: an absolute floor on the smaller token
    set (so a 2-3 token fact never triggers containment at all) and a ceiling
    that pins any containment-derived score into the 'related' band (so even a
    fully-contained longer short memory surfaces as 'related', never 'high').
    """
    from bettermemory.search import find_similar

    long_mem = _memory(
        "Development environment notes for newcomers: python 3.12 is the "
        "pinned interpreter, ruff handles both linting and formatting, mypy "
        "runs in strict mode, and the pre-commit hook chains all three before "
        "every commit lands."
    )

    # Below the smaller-set floor: a 2-3 content-token fact whose words all
    # appear in the long body. Must NOT be flagged 'high' (would silently drop
    # a distinct fact); in practice its raw Jaccard is below the 'related'
    # floor too, so it is not flagged at all.
    tiny_distinct = "Ruff handles linting."
    tiny_hits = find_similar(tiny_distinct, [long_mem])
    assert not [h for h in tiny_hits if h.relevance == "high"]

    # Above the floor and fully contained: containment ~1.0 fires, but the
    # ceiling keeps it 'related' (medium), never 'high'/block.
    contained = "python ruff mypy linting formatting strict interpreter commit hook"
    contained_hits = find_similar(contained, [long_mem])
    assert contained_hits, "a fully-contained multi-token memory should flag"
    assert all(h.relevance != "high" for h in contained_hits)
    assert any(h.relevance == "medium" for h in contained_hits)


# Token pools for the containment-gate tests below. Each word is a distinct,
# stopword-free, non-stemming stem (verified via `_raw_content_token_set`
# inside the tests), so the token-set sizes — and thus the Jaccard/containment
# ratios the assertions pin — are exact and won't drift silently under an
# unrelated tokenizer change.
_POOL_EIGHT = [
    "carbon",
    "oxygen",
    "radon",
    "argon",
    "cobalt",
    "nickel",
    "helium",
    "wolfram",
]
_POOL_EXTRA = [
    "granite",
    "basalt",
    "quartz",
    "marble",
    "gypsum",
    "copper",
    "silver",
    "bismuth",
    "uranium",
    "plutonium",
    "krypton",
    "xenon",
    "neon",
    "fluorine",
    "iodine",
    "bromine",
]


def test_find_similar_full_containment_in_dead_band_is_flagged() -> None:
    """F14 dead-band regression: full containment at a size ratio in the
    (2.5, 3.0) window was silently ignored. Pure Jaccard reaches the MEDIUM
    'related' floor (0.40) only up to ratio 2.5 (1/r >= 0.40), while the old
    `larger >= 3 * smaller` gate engaged the containment score only at ratio
    3.0 — so an 8-token fact fully contained in a 22-token body (ratio 2.75)
    scored Jaccard 8/22 = 0.36, below MEDIUM AND below the gate, and the
    near-duplicate committed silently.

    Mutation-sound for the ratio-gate removal: restoring `len(larger) >= 3 *
    len(smaller)` drops back to the 0.36 Jaccard (< MEDIUM), so no hit is
    returned and the assertion fails.
    """
    from bettermemory.search import (
        MEDIUM_SIMILARITY,
        _raw_content_token_set,
        find_similar,
    )

    short_body = " ".join(_POOL_EIGHT)
    long_body = " ".join(_POOL_EIGHT + _POOL_EXTRA[:14])
    # Pin the geometry the regression depends on: 8 fully contained in 22,
    # ratio 2.75 (inside the dead band), raw Jaccard below the MEDIUM floor.
    small_set = _raw_content_token_set(short_body)
    long_set = _raw_content_token_set(long_body)
    assert len(small_set) == 8
    assert len(long_set) == 22
    assert small_set <= long_set
    assert len(small_set) / len(long_set) < MEDIUM_SIMILARITY

    hits = find_similar(short_body, [_memory(long_body)])
    assert hits, "full containment in the (2.5, 3.0) dead band was not flagged"
    assert hits[0].relevance == "medium"


def test_find_similar_subfloor_full_containment_not_flagged() -> None:
    """F14 floor guard: a below-floor short fact (< _CONTAINMENT_MIN_TOKENS)
    FULLY CONTAINED in a long body must be flagged NEITHER 'high' NOR
    'medium'. A 4-token fact whose every word appears in a 20-token body has
    containment 1.0 but raw Jaccard 4/20 = 0.20; the token floor keeps
    containment from firing, so it stays below MEDIUM and is ignored.

    Mutation-sound for the floor: with `_CONTAINMENT_MIN_TOKENS = 0` the
    sub-floor fact clears the (now-absent) floor, containment 1.0 caps to
    _CONTAINMENT_CEILING (0.575) and surfaces it as 'medium' — flipping this
    assertion.
    """
    from bettermemory.search import (
        MEDIUM_SIMILARITY,
        _raw_content_token_set,
        find_similar,
    )

    short_body = " ".join(_POOL_EIGHT[:4])
    long_body = " ".join(_POOL_EIGHT[:4] + _POOL_EXTRA)
    small_set = _raw_content_token_set(short_body)
    long_set = _raw_content_token_set(long_body)
    assert len(small_set) == 4
    assert len(long_set) == 20
    assert small_set <= long_set
    assert len(small_set) / len(long_set) < MEDIUM_SIMILARITY

    hits = find_similar(short_body, [_memory(long_body)])
    assert not hits, "a sub-floor fully-contained fact must not be flagged"


def test_find_similar_comparable_length_distinct_not_over_rejected() -> None:
    """Ratio-gate removal guard: dropping the old `larger >= 3 * smaller`
    asymmetry gate widened the containment score to COMPARABLE-length pairs.
    The worst failure class for a memory product is a distinct write being
    SILENTLY REJECTED — the ingest dedup gate skips only on a 'high' active
    hit (ingest.py: `high_active = [h for h in active_hits if
    h.relevance == "high"]`). This test pins that containment can never push
    a comparable-length pair to that 'high'/block bar, while the intended
    short-in-long restatement still surfaces.

    Three arms, all over pure single-token pool words so the token-set sizes
    (and thus the exact jaccard/containment ratios) are pinned and can't
    drift under an unrelated tokenizer change:

    1. Comparable length (10 vs 10), heavy overlap (8 shared): raw Jaccard
       8/12 = 0.667 but containment 8/10 = 0.80. The `_CONTAINMENT_CEILING`
       (0.575) caps the containment contribution below HIGH_SIMILARITY, so
       `max(jaccard, min(containment, ceiling))` = 0.667 => 'medium', never
       'high'. MUTATION-SOUND against the ceiling: drop the `min(...,
       _CONTAINMENT_CEILING)` cap and the score becomes 0.80 => 'high',
       silently rejecting a distinct write — this assertion then fails.
    2. Comparable length (12 vs 12), modest overlap (4 shared): containment
       4/12 = 0.333 is below the MEDIUM floor, jaccard 4/20 = 0.20, so a
       legitimately-distinct comparable pair is not flagged at all.
    3. Short-in-long (8 fully contained in 22, ratio 2.75): the intended
       containment case still fires. MUTATION-SOUND against re-adding the
       ratio gate: restoring `len(larger) >= 3 * len(smaller)` drops this
       (ratio 2.75 < 3) back to jaccard 8/22 = 0.364 < MEDIUM => no hit.
    """
    from bettermemory.search import (
        HIGH_SIMILARITY,
        _raw_content_token_set,
        find_similar,
    )

    # Arm 1: comparable length, heavy overlap, must stay 'medium' (never
    # silently rejected as a 'high' duplicate).
    a1 = " ".join(_POOL_EIGHT + _POOL_EXTRA[0:2])  # 8 shared + 2 unique = 10
    b1 = " ".join(_POOL_EIGHT + _POOL_EXTRA[2:4])  # 8 shared + 2 unique = 10
    sa1, sb1 = _raw_content_token_set(a1), _raw_content_token_set(b1)
    assert len(sa1) == 10 and len(sb1) == 10
    assert len(sa1 & sb1) == 8
    assert len(sa1 | sb1) == 12  # jaccard 8/12 = 0.667 < HIGH_SIMILARITY
    assert 8 / 12 < HIGH_SIMILARITY
    arm1 = find_similar(a1, [_memory(b1)])
    assert arm1, "a heavy-overlap comparable pair should surface as related"
    assert arm1[0].relevance == "medium"
    assert all(h.relevance != "high" for h in arm1), (
        "containment must never silently reject a comparable-length write"
    )

    # Arm 2: comparable length, modest overlap => legitimately distinct,
    # flagged neither 'high' nor 'medium'.
    a2 = " ".join(_POOL_EIGHT[0:4] + _POOL_EXTRA[0:8])  # 12, 4 shared
    b2 = " ".join(_POOL_EIGHT[0:4] + _POOL_EXTRA[8:16])  # 12, 4 shared
    sa2, sb2 = _raw_content_token_set(a2), _raw_content_token_set(b2)
    assert len(sa2) == 12 and len(sb2) == 12 and len(sa2 & sb2) == 4
    assert not find_similar(a2, [_memory(b2)])

    # Arm 3: the intended short-in-long containment case still fires.
    short_in_long_short = " ".join(_POOL_EIGHT)  # 8
    short_in_long_long = " ".join(_POOL_EIGHT + _POOL_EXTRA[:14])  # 22
    arm3 = find_similar(short_in_long_short, [_memory(short_in_long_long)])
    assert arm3, "short-in-long restatement must still be flagged"
    assert arm3[0].relevance == "medium"
