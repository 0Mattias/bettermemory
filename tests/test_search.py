"""Tests for search.py — keyword scoring and recency boost."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    SearchMode,
    find_similar,
    score_memory,
    search,
    tokenize,
)


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


def test_tokenize_preserves_unicode_letters() -> None:
    """Non-ASCII letters must survive tokenization — as whole tokens,
    now accent-folded. The pre-audit `[a-z0-9]`-anchored regex silently
    dropped accented characters because `.lower()` produced them but the
    character class didn't accept them — `Niño` came out as ['ni', 'o'].
    `\\w` keeps the word whole; diacritic folding then maps it to the
    same form the FTS5 unicode61 index already matches ('niño' and
    'nino' are one token), so the prefilter and the Python rankers
    agree. Pin the regression so a future "tighten the token regex"
    change can't quietly re-break it."""
    assert tokenize("Niño café") == ["nino", "cafe"]
    assert tokenize("Mañana 2026 Zürich") == ["manana", "2026", "zurich"]
    # Mixed ASCII + non-ASCII inside one kebab token also stays whole.
    assert tokenize("café-bar") == ["cafe-bar"]


def test_unicode_query_finds_unicode_body() -> None:
    """End-to-end version of the tokenize check: a user storing a
    memory in their native language can still surface it by query."""
    a = _memory("café del puerto opens at six")
    b = _memory("airport monorail timetable changes")
    hits = search([a, b], "café")
    assert hits and hits[0].id == a.id


def test_accent_insensitive_search_bidirectional() -> None:
    """The FTS5 unicode61 index matches accent-insensitively; the Python
    rankers must share that notion of equality or indexed candidates get
    silently dropped (prefilter says 'match', ranker scores 0). Users type
    the ASCII spelling ('zurich') against correctly-accented bodies — and
    occasionally the reverse — so pin both directions."""
    a = _memory("Conference travel: staying in Zürich near Hauptbahnhof")
    hits = search([a], "zurich")
    assert hits and hits[0].id == a.id
    b = _memory("zurich trip notes for the conference")
    hits = search([b], "Zürich")
    assert hits and hits[0].id == b.id


def test_tokenize_handles_nfd_combining_marks() -> None:
    """NFD-normalized text (what macOS apps and PDF copies produce) must
    tokenize the same as the precomposed form. Python's `\\w` excludes
    combining marks (category Mn), so before the fold NFD 'Tjörn' SPLIT
    at the mark into ['tjo', 'rn'] — same failure class attribution.py's
    `_normalize` already fixed with NFC-first normalization. Construct
    the decomposed form explicitly, mirroring test_attribution."""
    nfd = unicodedata.normalize("NFD", "Tjörn")
    assert tokenize(nfd) == ["tjorn"]
    assert tokenize(nfd) == tokenize("Tjörn")


def test_nfc_query_finds_nfd_body() -> None:
    """A query typed normally (NFC) must hit a body saved in decomposed
    form — previously a single-word query for the place name returned
    zero hits in every Python ranker."""
    body = "Sommarstugan ligger på Tjörn; nyckeln hänger hos grannen Görel."
    a = _memory(unicodedata.normalize("NFD", body))
    hits = search([a], "Tjörn")
    assert hits and hits[0].id == a.id


def test_tokenize_strips_contraction_fragments() -> None:
    """Possessive/contraction suffixes leave orphan fragments ('s', 't')
    that survive stopword stripping, deflate the coverage denominator,
    and show up in match_terms. The suffix strip fires for both the
    straight and the curly (macOS) apostrophe — pin the parity."""
    assert tokenize("What's the SSH port?") == ["what", "the", "ssh", "port"]
    assert tokenize("What’s the SSH port?") == ["what", "the", "ssh", "port"]
    assert tokenize("don't") == ["don"]
    assert tokenize("I'm") == ["i"]
    # The strip is anchored to the apostrophe: standalone tokens that
    # happen to spell a contraction suffix are untouched.
    assert tokenize("the re module") == ["the", "re", "module"]


def test_contraction_query_relevance_high_for_full_answer() -> None:
    """Both bodies fully answer the real query terms (ssh, port), so both
    must label 'high' with only real terms in match_terms — previously
    the apostrophe-free body landed at 'medium' (the phantom 's' counted
    in the denominator) while the possessive-bearing body flipped back
    to 'high' with 's' reported as a matched term."""
    plain = _memory("SSH port is 2222 on the NAS")
    possessive = _memory("The NAS's SSH port is 2222")
    for m in (plain, possessive):
        hits = search([m], "What's the SSH port?")
        assert hits and hits[0].relevance == "high"
        assert sorted(hits[0].match_terms) == ["port", "ssh"]


def test_joined_query_token_matches_spaced_body() -> None:
    """A hyphenated query token should match the prose spelling of the
    same identifier: 'claude-code' against a body saying 'Claude Code'.
    The conjunctive fallback requires ALL components to hit, so the
    documented precision guard (test_kebab_query_does_not_match_
    unrelated_component_body) still holds."""
    a = _memory("Claude Code hooks run before every tool call")
    hits = search([a], "claude-code")
    assert hits and hits[0].id == a.id
    assert "claude-code" in hits[0].match_terms


def test_separator_variant_query_matches_body() -> None:
    """'docker_compose' and 'docker-compose' are the same identifier;
    tokenize canonicalizes '_' to '-' on both sides so neither spelling
    misses the other."""
    a = _memory("Use docker-compose v2 syntax everywhere")
    hits = search([a], "docker_compose")
    assert hits and hits[0].id == a.id
    b = _memory("Use docker_compose v2 syntax everywhere")
    hits = search([b], "docker-compose")
    assert hits and hits[0].id == b.id


def test_tokenize_keeps_dotted_version_whole() -> None:
    """Dotted numeric literals are first-class memory content ('Postgres
    16.3') — splitting them on '.' turned the version into free-floating
    digit tokens that matched any enumeration digit."""
    assert tokenize("postgres 16.3 upgrade") == ["postgres", "16.3", "upgrade"]
    assert tokenize("python 3.12.1") == ["python", "3.12.1"]


def test_version_query_does_not_match_bare_digit_body() -> None:
    """The query token '16.3' must not be satisfied by a stray
    enumeration digit — previously 'Backups run 3 times daily' surfaced
    on match_terms=['3'] for a version-pinned query."""
    unrel = _memory("Backups run 3 times daily on the NAS via restic.")
    assert search([unrel], "postgres 16.3 upgrade") == []


def test_version_component_query_still_matches_dotted_body() -> None:
    """Index-side expansion emits the '.'-split components (mirroring the
    kebab asymmetry), so a coarser 'postgres 16' query still hits a body
    that pins '16.3'."""
    right = _memory("Upgraded homelab Postgres to 16.3; pgvector needs a rebuild.")
    hits = search([right], "postgres 16")
    assert hits and hits[0].id == right.id
    assert hits[0].relevance == "high"


def test_dotted_version_ranking_repro() -> None:
    """Three-memory repro from the audit: the enumeration-digit memory no
    longer earns 'high' on the strength of a bare '3', and the unrelated
    digit-bearing memory does not surface at all. The genuinely version-
    pinned memory matches via the whole '16.3' token."""
    right = _memory("Upgraded homelab Postgres to 16.3; pgvector needs a rebuild.")
    wrong = _memory("Step 3: upgrade postgres extensions before flashing the OS.")
    unrel = _memory("Backups run 3 times daily on the NAS via restic.")
    hits = search([right, wrong, unrel], "postgres 16.3 upgrade")
    by_id = {h.id: h for h in hits}
    assert unrel.id not in by_id
    assert by_id[wrong.id].relevance != "high"
    assert "3" not in by_id[wrong.id].match_terms
    assert "16.3" in by_id[right.id].match_terms


def test_tokenize_symbol_identifier_aliases() -> None:
    """Symbol-bearing tech names must not collapse to a bare letter:
    'C++' -> 'c' made C/C++/C#/Objective-C mutually indistinguishable and
    matched list-enumeration bodies ('a. ..., b. ..., c. ...'). The fixed
    alias allowlist normalizes them symmetrically on both sides."""
    assert tokenize("C++ style guide") == ["cpp", "style", "guide"]
    assert tokenize("C++20 modules") == ["cpp", "20", "modules"]
    assert tokenize("C# and F# on .NET") == ["csharp", "and", "fsharp", "on", "dotnet"]
    # Word-ish boundaries: 'asp.net' is not the standalone '.NET' name.
    assert tokenize("asp.net") == ["asp", "net"]


def test_cpp_query_does_not_match_list_enumeration_body() -> None:
    """A C++ query must not hit a body via the list label 'c.' — the
    degraded bare-letter token was reported in match_terms as a real
    matched term."""
    listc = _memory("Release plan: a. tag, b. build, c. style-check the log.")
    assert search([listc], "C++") == []
    hits = search([listc], "C++ style guide")
    for h in hits:
        assert "c" not in h.match_terms


def test_cpp_query_matches_cpp_body() -> None:
    """The aliased token still matches real C++ content, including the
    versioned spelling ('C++20' tokenizes as ['cpp', '20'])."""
    cpp = _memory("Prefers C++20 modules over header-only libs in the renderer.")
    hits = search([cpp], "C++ style guide")
    assert hits and hits[0].id == cpp.id
    assert "cpp" in hits[0].match_terms


def test_tokenize_strips_suspended_hyphen() -> None:
    """Suspended hyphenation ('pre- and post-deploy') used to leave the
    query token 'pre-', which could never match anything (queries are not
    kebab-expanded) yet still counted in the coverage denominator."""
    assert tokenize("pre- and post-deploy") == ["pre", "and", "post-deploy"]


def test_suspended_hyphenation_query_full_coverage() -> None:
    """End-to-end: the perfect-match body covers all three meaningful
    query concepts (pre, post-deploy, hooks) -> 'high', not the 'medium'
    the dead 'pre-' token used to force."""
    a = _memory("Run DB migrations in the pre-deploy hooks; then post-deploy hooks.")
    hits = search([a], "pre- and post-deploy hooks")
    assert hits and hits[0].relevance == "high"


def test_search_tiebreaker_is_deterministic_on_equal_score_and_created() -> None:
    """Two memories with identical score AND identical `created`
    timestamp must still sort deterministically. Without `id` in the
    tiebreaker the order silently depended on `load_all` iteration —
    fine in practice today, fragile under clock-mocked tests and
    microsecond-tied writes. ULID-shaped ids sort lexically by time,
    so the final discriminator also gives "newer wins" semantics.
    Sort the same input twice in independent search() calls to lock
    the output ordering in."""
    same_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    a = _memory("alpha beta gamma", created=same_time)
    b = _memory("alpha beta gamma", created=same_time)
    # Force `id` ordering: lower id sorts first under the desc sort,
    # so the higher id should top the result list every call.
    higher, lower = sorted([a, b], key=lambda m: m.id, reverse=True)
    hits1 = search([a, b], "alpha beta gamma")
    hits2 = search([b, a], "alpha beta gamma")
    assert hits1[0].id == hits2[0].id == higher.id
    assert hits1[1].id == hits2[1].id == lower.id


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


def test_hit_includes_category() -> None:
    """Regression for commit 88120ab: `MemoryHit.category` is
    propagated from the source memory. Negative-results suppression
    and the response builder both branch on category, so a hit
    without it falls through to the default and misclassifies the
    memory."""
    from bettermemory.models import Category

    a = _memory("durable infrastructure note about postgres")
    # Construct with an explicit non-default category so the test
    # catches both the "category dropped" and "category defaulted"
    # failure modes.
    a = a.model_copy(update={"category": Category.AMBIENT})
    hits = search([a], "postgres")
    assert hits, "expected at least one hit for the matching token"
    assert hits[0].category == Category.AMBIENT, (
        f"MemoryHit.category dropped or defaulted; got {hits[0].category!r}"
    )


def test_hit_includes_default_fact_category() -> None:
    """When the source memory has `category=None` (legacy memories
    written before the field existed), the hit's category should
    surface as None — NOT silently default to FACT. The
    response/scoring code can apply a default; the search layer
    should preserve the input shape."""
    a = _memory("legacy note")
    assert a.category is None
    hits = search([a], "legacy")
    assert hits, "expected at least one hit"
    assert hits[0].category is None, (
        f"hit.category should preserve None for legacy memories; "
        f"got {hits[0].category!r}"
    )


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


def test_retrieval_meta_query_not_labeled_low() -> None:
    """The canonical 'do you remember anything stored about X' retrieval
    phrasing must not deflate an exactly-on-topic memory to 'low' (the
    documented 'probable noise' bucket). 'about'/'anything' are pure
    grammatical filler within word classes the stoplist already covers
    (prepositions, indefinite pronouns); with them stripped the repro
    lands at 2/5 coverage = 'medium'."""
    a = _memory("Prefers a rebase-heavy git workflow with small atomic commits")
    hits = search(
        [a], "do you remember anything stored about the preferred git workflow"
    )
    assert hits and hits[0].relevance in {"medium", "high"}


# ---------------------------------------------------------------------------
# Keyword scorer invariants: query-token dedup and per-term TF saturation
# ---------------------------------------------------------------------------


def test_repeated_query_token_counts_once_in_keyword_scorer() -> None:
    """Stopword stripping turns reduplicated phrases ('end to end') into a
    repeated token; the scoring loop must use set semantics like the
    coverage bookkeeping already does, not double the repeated word's
    body contribution."""
    now = datetime.now(timezone.utc)
    b = _memory("The front-end build uses Vite")
    dup_score, dup_matched = score_memory(b, ["end", "end", "testing"], now=now)
    dedup_score, dedup_matched = score_memory(b, ["end", "testing"], now=now)
    assert dup_score == dedup_score
    assert dup_matched == dedup_matched


def test_reduplicated_phrase_query_no_longer_doubles_offtopic_memory() -> None:
    """End-to-end repro: 'end to end testing' scored the front-end build
    memory at exactly 2x the on-topic testing memory because the
    duplicated 'end' re-added its contribution. With dedup both match one
    term apiece and tie — no more 2:1 inversion."""
    same_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    a = _memory(
        "Prefers pytest for integration testing of the handlers", created=same_time
    )
    b = _memory("The front-end build uses Vite", created=same_time)
    hits = search([a, b], "end to end testing", mode="keyword", now=same_time)
    assert len(hits) == 2
    assert hits[0].score == hits[1].score


def test_keyword_full_coverage_beats_single_term_spam() -> None:
    """The property the coverage-multiplier comment claims: 'foo bar'
    ranks above 'foo foo foo' for query ['foo', 'bar']. Unbounded body TF
    used to overrun the 2x-capped multiplier (2.475 vs 2.2); the per-term
    TF cap makes the documented property actually hold."""
    now = datetime.now(timezone.utc)
    spam = _memory("foo foo foo")
    full = _memory("foo bar")
    spam_score, _ = score_memory(spam, ["foo", "bar"], now=now)
    full_score, _ = score_memory(full, ["foo", "bar"], now=now)
    assert full_score > spam_score


def test_keyword_tf_cap_ranks_full_coverage_first() -> None:
    """Audit repro: a fresher docker cheat-sheet (one query term, repeated
    7x) outranked the full-coverage 'high' match in keyword mode, and the
    structural keyword-vs-BM25 RRF tie handed hybrid rank 1 to the newer
    wrong memory. With saturated TF both modes agree on the genuine hit."""
    now = datetime.now(timezone.utc)
    cheat = _memory(
        "Docker cheatsheet: docker compose v2 is invoked as docker compose, "
        "not docker-compose. Install docker via colima; the docker daemon "
        "socket lives at ~/.colima/docker.sock.",
        created=now - timedelta(days=1),
    )
    ontopic = _memory(
        "Homelab containers: docker stack uses restart policy "
        "unless-stopped on every service.",
        created=now - timedelta(days=30),
    )
    modes: tuple[SearchMode, ...] = ("keyword", "hybrid")
    for mode in modes:
        hits = search([cheat, ontopic], "docker restart policy", mode=mode, now=now)
        assert hits and hits[0].id == ontopic.id, f"inversion in mode={mode}"


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
    a = _memory("vendored python-frontmatter to drop the deprecated codecs.open call")
    candidate = "vendored python-frontmatter so we can drop the deprecated codecs.open"
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
    spaced_candidate = "python frontmatter library is unmaintained, vendored locally"
    hits = find_similar(spaced_candidate, [kebab])
    assert hits and hits[0].relevance in {"high", "medium"}


def test_find_similar_flags_nfc_duplicate_of_nfd_body() -> None:
    """An NFC re-statement of a body originally saved in NFD form is an
    exact-text duplicate and must trip the high tier. Before the
    combining-mark fold, the NFD original tokenized to fragments
    ('tjo', 'rn') and the byte-identical-looking duplicate scored 0.31
    Jaccard — sailing through the write-dedup gate."""
    body = "Sommarstugan ligger på Tjörn; nyckeln hänger hos grannen Görel."
    nfd_original = _memory(unicodedata.normalize("NFD", body))
    hits = find_similar(body, [nfd_original])
    assert hits and hits[0].relevance == "high"
    assert hits[0].similarity >= 0.99


def test_find_similar_ignores_recency() -> None:
    """A year-old memory should still flag as a duplicate if the content
    overlaps. Dedup is a content question, not a recency one.
    """
    old = _memory(
        "vendored python-frontmatter to drop the deprecated codecs.open call",
        created=datetime.now(timezone.utc) - timedelta(days=365),
    )
    candidate = "vendored python-frontmatter to drop the deprecated codecs.open call"
    hits = find_similar(candidate, [old])
    assert hits and hits[0].relevance == "high"
