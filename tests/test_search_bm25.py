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
from bettermemory.search import compute_idf, score_memory_bm25


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
    """No memories => empty IDF map and zero avgdl. Callers can short-circuit
    on `avgdl == 0` rather than dividing by zero downstream."""
    idf, avgdl = compute_idf([])
    assert idf == {}
    assert avgdl == 0.0


def test_compute_idf_avgdl_is_average() -> None:
    """avgdl is the mean kebab-expanded stopword-stripped doc length. Two
    docs of length 3 and 5 (post-strip) average to 4."""
    short = _memory("python list comp")  # 3 tokens after stopword strip
    long = _memory("python list comprehension generator expressions")  # 5
    _, avgdl = compute_idf([short, long])
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

    idf_map, avgdl = compute_idf(corpus)

    rare_score, _ = score_memory_bm25(
        rare_doc, ["obscure"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    common_score, _ = score_memory_bm25(
        common1, ["code"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    assert rare_score > common_score


def test_bm25_tf_saturation_diminishing_returns() -> None:
    """5x term frequency must not yield 5x score — that's the whole point
    of BM25's TF saturation curve. Anywhere between 1x and 5x is the
    acceptable band; in practice with k1=1.2 the ratio lands around 2.x."""
    now = datetime.now(timezone.utc)
    once = _memory("python")
    many = _memory("python python python python python")

    idf_map, avgdl = compute_idf([once, many])

    score_once, _ = score_memory_bm25(
        once, ["python"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    score_many, _ = score_memory_bm25(
        many, ["python"], idf_map=idf_map, avgdl=avgdl, now=now
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

    idf_map, avgdl = compute_idf([short, long])

    s_short, _ = score_memory_bm25(
        short, ["python"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    s_long, _ = score_memory_bm25(
        long, ["python"], idf_map=idf_map, avgdl=avgdl, now=now
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

    idf_map, avgdl = compute_idf([scoped, other])

    score, matched = score_memory_bm25(
        scoped, ["projects"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    assert score > 0
    assert "projects" in matched


def test_bm25_empty_query_returns_zero() -> None:
    """Defensive: empty query tokens => (0.0, []). Callers shouldn't
    have to special-case this before the scorer."""
    now = datetime.now(timezone.utc)
    m = _memory("any body")
    idf_map, avgdl = compute_idf([m])
    score, matched = score_memory_bm25(m, [], idf_map=idf_map, avgdl=avgdl, now=now)
    assert score == 0.0
    assert matched == []


def test_bm25_empty_corpus_returns_zero() -> None:
    """avgdl == 0 means the corpus is empty (or all bodies were empty
    after stopword strip). Scoring against an empty corpus should
    return zero rather than divide by zero."""
    now = datetime.now(timezone.utc)
    m = _memory("python notes")
    score, matched = score_memory_bm25(m, ["python"], idf_map={}, avgdl=0.0, now=now)
    assert score == 0.0
    assert matched == []


def test_bm25_recency_boost_applies() -> None:
    """A recently-updated memory should outscore an old one with identical
    body. Mirrors the keyword scorer's recency invariant so RRF doesn't
    lose freshness signal in hybrid mode."""
    now = datetime.now(timezone.utc)
    old = _memory("identical body words here", created=now - timedelta(days=180))
    new = _memory("identical body words here", created=now - timedelta(days=1))

    idf_map, avgdl = compute_idf([old, new])

    s_old, _ = score_memory_bm25(
        old, ["identical", "body"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    s_new, _ = score_memory_bm25(
        new, ["identical", "body"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    assert s_new > s_old


def test_bm25_matched_terms_deduplicated() -> None:
    """Repeated query tokens shouldn't appear twice in `matched_terms` —
    same contract as the keyword scorer. Consumer code on the MCP wire
    uses match_terms to render relevance, so duplication would be
    misleading."""
    now = datetime.now(timezone.utc)
    m = _memory("python python python")
    idf_map, avgdl = compute_idf([m])

    score, matched = score_memory_bm25(
        m, ["python", "python", "python"], idf_map=idf_map, avgdl=avgdl, now=now
    )
    assert score > 0
    assert matched == ["python"]


def test_bm25_unknown_term_in_query_zero_contribution_but_others_still_score() -> None:
    """If a query token appears in no doc (no IDF entry), it should
    contribute zero from the body without poisoning the score for the
    matching terms in the same query. Realistic case: user types a typo
    next to a real term — the real term should still rank the doc."""
    now = datetime.now(timezone.utc)
    m = _memory("python notes")
    idf_map, avgdl = compute_idf([m])

    score, matched = score_memory_bm25(
        m,
        ["python", "qzzzzx"],
        idf_map=idf_map,
        avgdl=avgdl,
        now=now,
    )
    assert score > 0
    assert "python" in matched
    assert "qzzzzx" not in matched
