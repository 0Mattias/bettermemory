"""Tests for search.py — keyword scoring and recency boost."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import find_similar, search, tokenize


def _memory(
    body: str,
    scopes: list[str] = ["tools"],
    *,
    created: datetime | None = None,
    confidence: Confidence = Confidence.MEDIUM,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes,
        confidence=confidence,
        source=Source.EXPLICIT,
        body=body,
    )


def test_tokenize_basic() -> None:
    assert tokenize("Hello, World! 123") == ["hello", "world", "123"]
    assert tokenize("python-frontmatter") == ["python-frontmatter"]


def test_exact_match_outranks_partial() -> None:
    a = _memory("python python python list comprehension")
    b = _memory("kubernetes networking notes")

    hits = search([a, b], "python list")
    assert hits[0].id == a.id


def test_scope_filter_excludes_non_matching() -> None:
    a = _memory("home lab routing", scopes=["infrastructure"])
    b = _memory("python tutorial style", scopes=["learning-style"])

    hits = search([a, b], "tutorial", scopes=["infrastructure"])
    # 'tutorial' is in b, but b doesn't have the 'infrastructure' scope.
    assert hits == []


def test_disabled_scope_excluded() -> None:
    a = _memory("python comprehension", scopes=["tools"])
    b = _memory("python comprehension", scopes=["projects:foo"])

    hits = search(
        [a, b],
        "python",
        excluded_scopes={"projects:foo"},
    )
    ids = [h.id for h in hits]
    assert a.id in ids
    assert b.id not in ids


def test_recency_boost_breaks_ties() -> None:
    now = datetime.now(timezone.utc)
    old = _memory("identical body words here", created=now - timedelta(days=180))
    new = _memory("identical body words here", created=now - timedelta(days=1))

    hits = search([old, new], "identical body", now=now)
    assert hits[0].id == new.id
    assert hits[0].score >= hits[1].score


def test_recency_boost_uses_updated_when_newer() -> None:
    """A memory edited recently outranks an older memory with the same body,
    even if its `created` is the older one. Without this, memory_update would
    leave a refined fact buried under recency boost it should be earning.
    """
    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(days=365)

    edited = Memory(
        id=generate_ulid(),
        created=long_ago,
        updated=now - timedelta(days=1),  # edited yesterday
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="identical body words here",
    )
    stale = Memory(
        id=generate_ulid(),
        created=now - timedelta(days=30),  # newer creation
        updated=now - timedelta(days=30),  # never edited
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="identical body words here",
    )

    hits = search([edited, stale], "identical body", now=now)
    assert hits[0].id == edited.id
    assert hits[0].score > hits[1].score


def test_hit_includes_updated_timestamp() -> None:
    a = _memory("python list comprehension")
    hits = search([a], "python")
    assert hits[0].updated == a.updated


def test_empty_query_returns_empty_list() -> None:
    a = _memory("anything")
    assert search([a], "") == []
    assert search([a], "   ") == []


def test_no_hits_is_empty_not_error() -> None:
    a = _memory("kubernetes networking")
    assert search([a], "totally unrelated") == []


def test_max_results_caps_output() -> None:
    memories = [_memory(f"python notes {i}") for i in range(10)]
    hits = search(memories, "python", max_results=3)
    assert len(hits) == 3


def test_scope_match_contributes_to_score() -> None:
    # 'projects' as a scope should match 'projects' as a query token.
    a = _memory("body without the keyword", scopes=["projects:alpha"])
    b = _memory("body without the keyword", scopes=["tools"])

    hits = search([a, b], "projects")
    assert any(h.id == a.id for h in hits)
    # b shouldn't surface — its scope tokens don't match.
    assert not any(h.id == b.id for h in hits)


def test_snippet_truncated_to_200_chars() -> None:
    long = "python " * 200
    a = _memory(long.strip())
    hits = search([a], "python")
    assert len(hits[0].snippet) <= 203  # 200 + "..."


def test_snippet_does_not_cut_mid_word() -> None:
    """Truncation should land on a word boundary so the trailing token isn't
    sliced — previously a snippet could end with `...config --global user`
    when the full body said `user.name`.
    """
    from bettermemory.models import snippet_for

    text = "x" * 50 + " word " + "y" * 200
    snippet = snippet_for(text, max_chars=80)
    assert snippet.endswith("...")
    # The body of the snippet (sans ellipsis) should not end in a half word —
    # i.e. it should end at a space-separated token, not slice through `yyy...`.
    head = snippet[:-3].rstrip()
    assert head.endswith("word") or head.endswith("x" * 50)


# ---------------------------------------------------------------------------
# Stopwords
# ---------------------------------------------------------------------------


def test_stopwords_alone_in_query_returns_empty() -> None:
    """A query that is *only* stopwords has no signal — should return []."""
    a = _memory("the kubernetes networking notes")
    assert search([a], "what is the") == []
    assert search([a], "how do i") == []


def test_stopwords_dont_create_phantom_matches() -> None:
    """An off-topic query with one shared stopword shouldn't surface a hit."""
    a = _memory("python list comprehension tips")
    # 'how' / 'to' / 'at' are stopwords, 'bake', 'sourdough', 'bread', 'home'
    # don't appear in `a` — should be no hit.
    assert search([a], "how to bake sourdough bread at home") == []


def test_stopwords_stripped_but_real_terms_still_match() -> None:
    """The content words drive the match; stopwords are silently dropped."""
    a = _memory("python list comprehension tips")
    hits = search([a], "the python tutorial")
    assert len(hits) == 1
    assert hits[0].id == a.id


# ---------------------------------------------------------------------------
# Match terms / relevance label
# ---------------------------------------------------------------------------


def test_hit_includes_match_terms() -> None:
    a = _memory("python list comprehension performance notes")
    hits = search([a], "python performance")
    assert hits[0].match_terms == ["python", "performance"] or hits[0].match_terms == [
        "performance",
        "python",
    ]


def test_relevance_high_for_full_coverage() -> None:
    a = _memory("python list comprehension")
    hits = search([a], "python comprehension")
    assert hits[0].relevance == "high"


def test_relevance_medium_for_partial_coverage() -> None:
    # 5 content tokens, only 2 match → coverage 0.4 → "medium".
    a = _memory("python list comprehension")
    hits = search([a], "python comprehension kubernetes networking docker")
    assert hits[0].relevance == "medium"


def test_relevance_low_for_sparse_coverage() -> None:
    # 5 content tokens, only 1 matches → coverage 0.2 → "low".
    a = _memory("python notes")
    hits = search([a], "python kubernetes networking docker terraform")
    assert hits[0].relevance == "low"


# ---------------------------------------------------------------------------
# Kebab/snake expansion on indexed text
#
# `tokenize` keeps `python-frontmatter` as one token by design (so an exact
# query for the joined form ranks tightest). For the relaxed direction —
# query a single component, hit a body that contains the joined form — we
# expand on the indexed side only. Asymmetric: bodies/scopes widen, query
# stays specific.
# ---------------------------------------------------------------------------


def test_kebab_body_findable_by_component() -> None:
    """A body containing `python-frontmatter` should hit a query for `python`
    or `frontmatter` alone — without that, kebab-named libraries, ULIDs,
    package slugs, etc. become unsearchable by their parts.
    """
    a = _memory("we vendored python-frontmatter to drop the deprecated dep")
    assert any(h.id == a.id for h in search([a], "python"))
    assert any(h.id == a.id for h in search([a], "frontmatter"))


def test_kebab_body_still_matches_joined_query() -> None:
    """Index-side expansion must not regress the exact-form match."""
    a = _memory("we vendored python-frontmatter to drop the deprecated dep")
    hits = search([a], "python-frontmatter")
    assert hits and hits[0].id == a.id
    assert "python-frontmatter" in hits[0].match_terms


def test_kebab_query_does_not_match_unrelated_component_body() -> None:
    """Asymmetry guard: querying `python-frontmatter` should NOT pull in a
    body that only mentions plain `python`. The query is specific intent;
    we don't want every Python memory surfaced.
    """
    a = _memory("python is a great general-purpose language")
    assert search([a], "python-frontmatter") == []


def test_kebab_scope_findable_by_component() -> None:
    """A memory tagged `projects:foo-bar` should hit a query for `bar`.
    Without component-level scope expansion, nested project slugs become
    invisible to natural one-word queries.
    """
    a = _memory("body without the keyword", scopes=["projects:foo-bar"])
    hits = search([a], "bar")
    assert any(h.id == a.id for h in hits)


def test_kebab_underscore_treated_like_hyphen() -> None:
    """`zephyr_quartz_9417` should split the same way as `zephyr-quartz-9417`."""
    a = _memory("identifier zephyr_quartz_9417 in the logs")
    assert any(h.id == a.id for h in search([a], "zephyr"))
    assert any(h.id == a.id for h in search([a], "quartz"))


# ---------------------------------------------------------------------------
# find_similar — content dedup at write time
#
# Symmetric Jaccard on stopword-stripped, kebab-expanded token sets. >= 0.75
# is "high" (block the write); >= 0.40 is "medium" (surface as related);
# below is ignored. Recency is irrelevant — two memories aren't more or less
# duplicate based on age.
# ---------------------------------------------------------------------------


def test_find_similar_identical_body_is_high() -> None:
    """Byte-identical bodies should always trip the high-similarity tier."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    a = _memory(body)
    hits = find_similar(body, [a])
    assert len(hits) == 1
    assert hits[0].id == a.id
    assert hits[0].relevance == "high"
    assert hits[0].similarity >= 0.99


def test_find_similar_near_duplicate_is_high() -> None:
    """Bodies that share most content tokens should still be flagged high."""
    a = _memory(
        "vendored python-frontmatter to drop the deprecated codecs.open call"
    )
    candidate = (
        "vendored python-frontmatter so we can drop the deprecated codecs.open"
    )
    hits = find_similar(candidate, [a])
    assert hits and hits[0].relevance == "high"


def test_find_similar_partial_overlap_is_medium() -> None:
    """~60% overlap is the "related but not duplicate" zone (>=0.40, <0.75)."""
    # tokens(a) = {kubernetes, ingress, nginx, tls}
    # tokens(candidate) = {kubernetes, ingress, nginx, logging}
    # intersection = 3, union = 5, jaccard = 0.6 → medium.
    a = _memory("kubernetes ingress nginx tls")
    candidate = "kubernetes ingress nginx logging"
    hits = find_similar(candidate, [a])
    assert hits and hits[0].relevance == "medium"


def test_find_similar_unrelated_returns_empty() -> None:
    a = _memory("kubernetes ingress nginx tls termination notes")
    candidate = "user prefers tabs over spaces in editor config"
    assert find_similar(candidate, [a]) == []


def test_find_similar_empty_body_returns_empty() -> None:
    """No content tokens to compare → nothing to dedup against."""
    a = _memory("python frontmatter library notes")
    assert find_similar("", [a]) == []
    # All stopwords → also empty after stripping.
    assert find_similar("the and or but is", [a]) == []


def test_find_similar_skips_all_stopword_existing() -> None:
    """An existing memory whose body is pure filler shouldn't be a "match"
    for any new write — its stripped token set is empty."""
    a = _memory("the and or but is")
    hits = find_similar("kubernetes ingress nginx", [a])
    assert hits == []


def test_find_similar_orders_by_similarity_desc() -> None:
    a = _memory("kubernetes ingress nginx tls termination notes for cluster")
    b = _memory("kubernetes ingress nginx tls termination configuration")
    c = _memory("kubernetes ingress nginx general notes")

    candidate = "kubernetes ingress nginx tls termination"
    hits = find_similar(candidate, [a, b, c])
    similarities = [h.similarity for h in hits]
    assert similarities == sorted(similarities, reverse=True)


def test_find_similar_kebab_expansion_is_symmetric() -> None:
    """A body using kebab notation should match a body that spells it out
    (and vice versa). The asymmetry in `score_memory` is for the search
    direction; for dedup we want both sides expanded so equivalent phrasings
    collapse together.
    """
    kebab = _memory("python-frontmatter library is unmaintained, vendored locally")
    spaced_candidate = (
        "python frontmatter library is unmaintained, vendored locally"
    )
    hits = find_similar(spaced_candidate, [kebab])
    assert hits and hits[0].relevance in {"high", "medium"}


def test_find_similar_ignores_recency() -> None:
    """A year-old memory should still flag as a duplicate if the content
    overlaps. Dedup is a content question, not a recency one.
    """
    old = _memory(
        "vendored python-frontmatter to drop the deprecated codecs.open call",
        created=datetime.now(timezone.utc) - timedelta(days=365),
    )
    candidate = (
        "vendored python-frontmatter to drop the deprecated codecs.open call"
    )
    hits = find_similar(candidate, [old])
    assert hits and hits[0].relevance == "high"
