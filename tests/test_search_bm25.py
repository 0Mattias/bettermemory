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
from bettermemory.search import compute_idf, score_memory_bm25, search


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
        ["obscure"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    common_score, _ = score_memory_bm25(
        common1,
        ["code"],
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
        ["projects"],
        body_idf_map=body_idf,
        scope_idf_map=scope_idf,
        avgdl=avgdl,
        now=now,
    )
    assert score > 0
    assert "projects" in matched


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
    assert "projects" in scope_idf
    # ...but ubiquitous => far less discriminating than a one-doc term.
    assert scope_idf["projects"] < scope_idf["restic"]


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
    assert "projects" in scope_idf
    assert "projects" not in body_idf
    # Body-rare + scope-ubiquitous term: high body IDF, deflated scope IDF.
    assert body_idf["bettermemory"] > 1.0
    assert scope_idf["bettermemory"] < 0.2


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
