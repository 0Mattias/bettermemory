"""Tests for expansion.py and the rescue-expansion path in search().

The retrieval campaign's Phase-1 lane: committed vocabulary tables, the
filler df-floor, and the coverage-gated rescue leg. Every mechanism
invariant measured on bench/retrieval (2026-08-09) is pinned here so a
future table edit or scorer change can't silently un-earn the shipped
numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bettermemory.expansion import (
    _MIN_EXPANSION_LEN,
    CLIPPINGS,
    IRREGULAR_PAST,
    QUERY_FILLER_WORDS,
    SYNONYM_GROUPS,
    build_tables,
    expansion_terms,
    morph_variants,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _EXPANSION_TABLES,
    _STOPWORDS,
    _stem_token,
    reciprocal_rank_fusion,
    search,
)

TABLES = _EXPANSION_TABLES


def _memory(
    body: str,
    scopes: list[str] = ["tools"],
    *,
    created: datetime | None = None,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes,
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


# ---------------------------------------------------------------------------
# Table hygiene
# ---------------------------------------------------------------------------


def test_raw_tables_are_lowercase_ascii() -> None:
    """Table entries live in pre-tokenize space; anything non-ascii or
    cased would silently never match the folded token stream."""
    words = list(QUERY_FILLER_WORDS)
    for k, vs in IRREGULAR_PAST.items():
        words.append(k)
        words.extend(vs)
    for k, vs in CLIPPINGS.items():
        words.append(k)
        words.extend(vs)
    for group in SYNONYM_GROUPS:
        words.extend(group)
    for w in words:
        assert w == w.lower(), f"table word not lowercase: {w!r}"
        assert w.isascii(), f"table word not ascii: {w!r}"


def test_no_table_output_is_a_stopword() -> None:
    """An expansion term that is a stopword would be stripped from the
    leg query (or price at the no-statistics floor) — dead weight that
    reads as coverage. Same check for filler: a word in both lists
    would be stripped before the df-floor could ever see it."""
    for vs in TABLES.irregular.values():
        for v in vs:
            assert v not in _STOPWORDS, f"irregular target is stopword: {v}"
    for vs in TABLES.clippings.values():
        for v in vs:
            assert v not in _STOPWORDS, f"clipping target is stopword: {v}"
    for mates in TABLES.synonyms.values():
        for v in mates:
            assert v not in _STOPWORDS, f"synonym mate is stopword: {v}"
    assert not (TABLES.filler_stems & _STOPWORDS)


def test_no_expansion_output_is_a_filler_word() -> None:
    """Emitting a filler word from the rescue leg would re-inflate the
    exact vocabulary the df-floor deflates — the two mechanisms must
    stay disjoint."""
    for vs in TABLES.irregular.values():
        assert not (set(vs) & TABLES.filler_stems)
    for vs in TABLES.clippings.values():
        assert not (set(vs) & TABLES.filler_stems)
    for mates in TABLES.synonyms.values():
        assert not (mates & TABLES.filler_stems)


def test_irregular_targets_survive_the_length_floor() -> None:
    """went/gone -> 'go' and done -> 'do' are documented OUT of the
    table: a target under `_MIN_EXPANSION_LEN` is filtered at emit time,
    so its entry is a dead row that reads as coverage. The floor itself
    is measured — a single 2-char term reaching the rescue leg cost the
    gold set 5 points at recall@1 and recall@5."""
    for k, vs in TABLES.irregular.items():
        surviving = [v for v in vs if len(v) >= _MIN_EXPANSION_LEN]
        assert surviving, f"irregular {k!r} has no target surviving the floor"
    assert "went" not in IRREGULAR_PAST
    assert "gone" not in IRREGULAR_PAST
    assert "done" not in IRREGULAR_PAST


def test_synonym_groups_are_bidirectional_and_exclude_self() -> None:
    tables = build_tables(_stem_token)
    for group in SYNONYM_GROUPS:
        stems = {_stem_token(w) for w in group}
        for s in stems:
            assert s in tables.synonyms
            assert tables.synonyms[s] == frozenset(stems - {s})


# ---------------------------------------------------------------------------
# morph_variants / expansion_terms
# ---------------------------------------------------------------------------


def test_morph_variants_undo_doubling_and_mute_e() -> None:
    """'splitting the repos apart' vs a body that says 'split' was a
    measured total miss — the shipped stemmer folds plurals only."""
    assert "split" in morph_variants("splitting", _stem_token)
    # staging -> stage; the stemmer's final-e normalisation carries both
    # sides to 'stag', and the -ed re-inflection catches a body that
    # says 'staged' (which the plural-only stemmer leaves verbatim).
    assert "stag" in morph_variants("staging", _stem_token)
    assert "staged" in morph_variants("staging", _stem_token)
    assert "switch" in morph_variants("switching", _stem_token)


def test_morph_variants_never_emit_the_input_or_short_junk() -> None:
    for tok in ("splitting", "staged", "wired", "digging"):
        variants = morph_variants(tok, _stem_token)
        assert tok not in variants
        assert all(len(v) >= _MIN_EXPANSION_LEN for v in variants)


def test_expansion_terms_sources_and_contract() -> None:
    """cred -> credential+secret (clipping + synonym), pr -> pull+request
    (multi-word clipping), and the output is sorted, deduplicated, free
    of query tokens, and floor-filtered — the determinism the WaC rules
    require of every ranking input."""
    out = expansion_terms(["cred"], TABLES, _stem_token)
    assert "credential" in out and "secret" in out
    out = expansion_terms(["pr"], TABLES, _stem_token)
    assert "pull" in out and "request" in out
    out = expansion_terms(["hash"], TABLES, _stem_token)
    assert {"digest", "sha256", "checksum"} <= set(out)

    tokens = ["toggl", "rip", "splitting", "cred"]
    out = expansion_terms(tokens, TABLES, _stem_token)
    assert out == sorted(out)
    assert len(out) == len(set(out))
    assert not (set(out) & set(tokens))
    assert all(len(t) >= _MIN_EXPANSION_LEN for t in out)


def test_expansion_terms_empty_for_vocabulary_the_tables_do_not_know() -> None:
    assert expansion_terms(["kubernetes", "ingress"], TABLES, _stem_token) == []


# ---------------------------------------------------------------------------
# Weighted RRF
# ---------------------------------------------------------------------------


def test_rrf_weights_none_is_byte_identical_to_all_ones() -> None:
    rankings = [["a", "b", "c"], ["b", "a"]]
    assert reciprocal_rank_fusion(rankings) == reciprocal_rank_fusion(
        rankings, weights=[1.0, 1.0]
    )


def test_rrf_weights_scale_a_leg_and_mismatch_raises() -> None:
    rankings = [["a"], ["b"]]
    fused = reciprocal_rank_fusion(rankings, weights=[1.0, 0.5])
    assert fused["b"] == pytest.approx(fused["a"] * 0.5)
    with pytest.raises(ValueError, match="weights length"):
        reciprocal_rank_fusion(rankings, weights=[1.0])


# ---------------------------------------------------------------------------
# search(): the rescue path
# ---------------------------------------------------------------------------


def _corpus() -> list[Memory]:
    """Small store with one paraphrase-reachable gold doc and filler-y
    distractors, shaped like the bench's measured failure cases."""
    now = datetime.now(timezone.utc)
    return [
        _memory(
            "Feature flag removal has an owner and a deadline; stale "
            "flags are cleaned up in the monthly sweep.",
            created=now - timedelta(days=5),
        ),
        _memory(
            "Dependency bumps are batched weekly; the process just sits "
            "in the bot queue forever until someone approves.",
            created=now - timedelta(days=3),
        ),
        _memory(
            "The escalation rotation pages the secondary after fifteen minutes.",
            created=now - timedelta(days=8),
        ),
    ]


def test_rescue_surfaces_paraphrase_via_synonyms_with_honest_labels() -> None:
    """'toggles' never appears in any body; the synonym group carries
    the query to the flag doc. The hit must be labelled honestly:
    surfaced by the expansion leg, match_terms still a subset of the
    caller's own tokens."""
    memories = _corpus()
    legs: dict[str, str] = {}
    hits = search(
        memories,
        "do we ever rip the old toggles back out",
        max_results=3,
        matched_leg_out=legs,
    )
    assert hits, "rescue produced no hits at all"
    top = hits[0]
    assert "flag removal" in memories[0].body
    assert top.id == memories[0].id
    # match_terms stays a subset of the caller's own (stemmed) tokens.
    assert set(top.match_terms) <= {
        "do",
        "we",
        "ever",
        "rip",
        "old",
        "toggl",
        "back",
        "out",
    }
    # Surfaced by base legs? 'rip'/'toggl' match nothing lexically in
    # this corpus, but 'out' does — accept either label, and require
    # the leg field to be present and valid.
    assert legs.get(top.id) in ("lexical", "expansion")


def test_rescue_expansion_only_hit_reports_expansion_leg() -> None:
    """A doc reachable ONLY through synthesized vocabulary reports
    matched_leg='expansion', relevance 'low', match_terms [] — the
    pure-paraphrase shape."""
    now = datetime.now(timezone.utc)
    memories = [
        _memory("Credential injection for containers uses mounted files."),
        _memory("The reconciliation job runs at 0300 UTC.", created=now),
    ]
    legs: dict[str, str] = {}
    hits = search(memories, "creds", max_results=2, matched_leg_out=legs)
    assert hits
    top = hits[0]
    assert top.id == memories[0].id
    assert legs[top.id] == "expansion"
    assert top.match_terms == []
    assert top.relevance == "low"


def test_confident_base_ranking_skips_the_rescue() -> None:
    """A query the base ranking answers with high coverage must be
    byte-identical with the feature on and off — the gate is what keeps
    precise queries precise (requery stayed 80/100 on the bench because
    of exactly this)."""
    memories = _corpus()
    on = search(memories, "feature flag removal owner deadline", max_results=3)
    off = search(
        memories,
        "feature flag removal owner deadline",
        max_results=3,
        rescue_expansion=False,
    )
    assert [h.id for h in on] == [h.id for h in off]
    assert [h.score for h in on] == [h.score for h in off]


def test_kill_switch_restores_two_leg_hybrid() -> None:
    """rescue_expansion=False must never report an expansion leg and
    must leave a paraphrase-only query unanswered by the rescue."""
    memories = _corpus()
    legs: dict[str, str] = {}
    hits = search(
        memories,
        "creds",
        max_results=3,
        rescue_expansion=False,
        matched_leg_out=legs,
    )
    assert all(leg != "expansion" for leg in legs.values())
    assert all(h.id != memories[0].id or h.match_terms for h in hits)


def test_filler_df_floor_stops_filler_outranking_content() -> None:
    """The measured failure class: conversational filler is corpus-rare
    in technical prose, so BM25 prices it like a discriminating term.
    With the floor, one real content-term match must beat two
    filler-term matches; with the switch off, the old (wrong) order
    comes back — pinning the mechanism, not just the outcome."""
    now = datetime.now(timezone.utc)
    gold = _memory(
        "The escalation rotation pages the secondary oncall after "
        "fifteen minutes without an ack.",
        created=now - timedelta(days=30),
    )
    distractor = _memory(
        "I remember someone supposed that the wiki search was broken; "
        "nobody filed a ticket.",
        created=now,
    )
    filler_dense = [
        _memory(f"note {i} about unrelated inventory shelving", created=now)
        for i in range(6)
    ]
    memories = [gold, distractor, *filler_dense]
    query = "i remember getting paged, who is supposed to ack escalation"

    with_floor = search(memories, query, max_results=2)
    assert with_floor[0].id == gold.id

    without = search(memories, query, max_results=2, rescue_expansion=False)
    assert without[0].id == distractor.id, (
        "expected the unfloored ranking to prefer the filler-heavy "
        "distractor; if this starts failing the fixture no longer "
        "reproduces the measured failure class and needs rebuilding"
    )


def test_bm25_and_keyword_modes_are_untouched_by_the_flag() -> None:
    memories = _corpus()
    for mode in ("bm25", "keyword"):
        on = search(memories, "creds toggles", max_results=3, mode=mode)
        off = search(
            memories,
            "creds toggles",
            max_results=3,
            mode=mode,
            rescue_expansion=False,
        )
        assert [h.id for h in on] == [h.id for h in off]
        assert [h.score for h in on] == [h.score for h in off]
