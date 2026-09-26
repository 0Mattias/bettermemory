"""Tests for expansion.py and the rescue-expansion path in search().

The retrieval campaign's Phase-1 lane: committed vocabulary tables, the
filler df-floor, and the coverage-gated rescue leg. Every mechanism
invariant measured on bench/retrieval (2026-08-09) is pinned here so a
future table edit or scorer change can't silently un-earn the shipped
numbers.
"""

from __future__ import annotations


import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.config import BehaviorConfig, load_config
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
    _EVIDENCE_FULL_AT,
    _EXPANSION_TABLES,
    _RESCUE_LEG_EVIDENCE_SCALING,
    _RESCUE_LEG_MIN_EVIDENCE,
    _RESCUE_LEG_WEIGHT,
    _STOPWORDS,
    _kebab_parts,
    _leg_evidence_weight,
    _leg_top_evidence,
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


# The -ing/-ed surface forms a casual question actually opens with. The
# three tables are curated by hand, so checking their values covers
# them; `morph_variants` is a RULE and regenerates filler from filler
# ("wondering" -> 'wonder'/'wondered'), so the invariant has to be
# driven through the real emit site instead.
_FILLER_INFLECTIONS: tuple[str, ...] = (
    "wondering",
    "wondered",
    "thinking",
    "wanted",
    "wanting",
    "remembered",
    "remembering",
    "guessing",
    "guessed",
    "forgetting",
    "knowing",
    "supposing",
    "supposed",
)


@pytest.mark.parametrize(
    "word", sorted(set(QUERY_FILLER_WORDS) | set(_FILLER_INFLECTIONS))
)
def test_morph_variants_never_leak_filler_into_the_rescue_leg(word: str) -> None:
    """The df-floor (`search._filler_floor_stats`) is applied to the
    CALLER's tokens only, so a filler stem synthesized by the rule
    reaches the rescue leg at full corpus-rare IDF — restoring exactly
    the weight the floor removed, on the one leg that has no floor.
    Measured: 'wonder'/'wondering' priced 7.1x their floored sibling
    and above the genuine expansion vocabulary the lane exists to add,
    which is enough to float a body of pure discourse filler over the
    memory the rescue was engaged to reach."""
    tok = _stem_token(word)
    leaked = set(expansion_terms([tok], TABLES, _stem_token)) & TABLES.filler_stems
    assert not leaked, f"{word!r} leaked filler into the rescue leg: {sorted(leaked)}"


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


def test_no_irregular_target_stems_to_a_live_url_fragment() -> None:
    """'came' -> 'come' is out for the reason BEHIND the floor, not the
    floor itself: the stemmer's final-e normalisation carries 'come' to
    'com', which clears `_MIN_EXPANSION_LEN` by one character and is a
    real body token in every memory citing a `.com` host. A 3-char stem
    that a dotted hostname splits into is the promiscuous matcher the
    floor exists to block, one character above it."""
    assert "came" not in IRREGULAR_PAST
    assert "com" not in {v for vs in TABLES.irregular.values() for v in vs}
    # The mechanism, not just the entry: this is what made it reachable.
    assert "com" in _kebab_parts(_stem_token("status.example.com"))


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


def test_morph_variants_undo_doubling_on_the_ed_side_too() -> None:
    """The -ed branch carries the same doubling rule as the -ing branch
    and had no test of its own: 'we stopped the nightly job' has to meet
    a body that says 'stop'. Pinned separately because the two branches
    are separate code — the -ing rule being right says nothing about
    this one."""
    for surface, base in (
        ("stopped", "stop"),
        ("shipped", "ship"),
        ("dropped", "drop"),
        ("planned", "plan"),
    ):
        variants = morph_variants(surface, _stem_token)
        assert base in variants, f"{surface} -> {base} lost"
        # And the re-inflection the rule exists for, so a body keeping
        # its surface -ing spelling still matches.
        assert base + "ing" in variants


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
        rescue_expansion=True,
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
    pure-paraphrase shape.

    The body has to be reachable by TWO synthesized terms: since round
    5 a leg whose rank-1 rests on one matched term is withheld as a
    coincidence (`_RESCUE_LEG_MIN_EVIDENCE`). A single-synonym rescue
    on a tiny store no longer fires, which is a real narrowing of the
    lane and is pinned separately below."""
    now = datetime.now(timezone.utc)
    memories = [
        _memory("Credential and secret injection for containers uses files."),
        _memory("The reconciliation job runs at 0300 UTC.", created=now),
    ]
    legs: dict[str, str] = {}
    hits = search(
        memories, "creds", max_results=2, matched_leg_out=legs, rescue_expansion=True
    )
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
    on = search(
        memories,
        "feature flag removal owner deadline",
        max_results=3,
        rescue_expansion=True,
    )
    off = search(
        memories,
        "feature flag removal owner deadline",
        max_results=3,
        rescue_expansion=False,
    )
    assert [h.id for h in on] == [h.id for h in off]
    assert [h.score for h in on] == [h.score for h in off]


def test_default_is_off_and_byte_stable() -> None:
    """The product default is rescue_expansion=False — the lane's own
    preregistered LongMemEval check killed default-on (kill line
    macro@5 0.8900; the run read 0.8770). A bare search() call must be
    byte-identical to an explicit False and must never report an
    expansion leg. Flipping the default back is earned by a fresh
    preregistration on both bench instruments, not by editing this
    test."""
    memories = _corpus()
    legs_default: dict[str, str] = {}
    legs_off: dict[str, str] = {}
    query = "do we ever rip the old toggles back out"
    default = search(memories, query, max_results=3, matched_leg_out=legs_default)
    off = search(
        memories,
        query,
        max_results=3,
        matched_leg_out=legs_off,
        rescue_expansion=False,
    )
    assert [h.id for h in default] == [h.id for h in off]
    assert [h.score for h in default] == [h.score for h in off]
    assert "expansion" not in legs_default.values()


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


def test_the_rescue_lane_beats_a_filler_heavy_distractor() -> None:
    """The measured failure class: conversational filler is corpus-rare
    in technical prose, so BM25 prices it like a discriminating term and
    a distractor matching "remember" + "supposed" outranks the right
    memory matching "paged" + "ack".

    **What actually fixes this fixture is the LEG, not the floor**, and
    that is pinned below rather than assumed. Until round 5 this test
    toggled `rescue_expansion` — which moves BOTH mechanisms — and
    credited the floor. Driving them separately shows the floor alone
    leaves the distractor on top; the expansion leg is what surfaces
    the gold. The floor's own contribution is pinned directly in
    `test_the_filler_floor_deflates_listed_words`.
    """
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

    # Lane off: the measured wrong order.
    off = search(memories, query, max_results=2, rescue_expansion=False)
    assert off[0].id == distractor.id, (
        "expected the unfloored ranking to prefer the filler-heavy "
        "distractor; if this starts failing the fixture no longer "
        "reproduces the measured failure class and needs rebuilding"
    )

    # Floor on, leg withheld: still wrong. The floor is not what fixes
    # this — a fact the previous version of this test hid.
    import bettermemory.search as engine

    saved_floor = engine._RESCUE_LEG_MIN_EVIDENCE
    saved_reader = engine._leg_top_evidence
    try:
        engine._RESCUE_LEG_MIN_EVIDENCE = 99
        floor_only = search(memories, query, max_results=2, rescue_expansion=True)
        assert floor_only[0].id == distractor.id

        # Leg voting: the gold surfaces. Forced by the evidence READER
        # rather than by lowering the floor — since round 6 the weight
        # curve is also zero at one matched term, so the floor alone no
        # longer lets a one-term leg vote, and this fixture's leg has
        # exactly one.
        engine._RESCUE_LEG_MIN_EVIDENCE = saved_floor
        engine._leg_top_evidence = lambda scored: 9
        with_leg = search(memories, query, max_results=2, rescue_expansion=True)
        assert with_leg[0].id == gold.id
    finally:
        engine._RESCUE_LEG_MIN_EVIDENCE = saved_floor
        engine._leg_top_evidence = saved_reader


def test_the_filler_floor_deflates_listed_words() -> None:
    """The floor's own contribution, pinned where it cannot be confused
    with the leg's: a listed filler word is priced at a document
    frequency of at least half the collection, and an unlisted word is
    left alone."""
    from bettermemory.search import _filler_floor_stats

    floored = _filler_floor_stats(None, ["remember", "escalation"], 200)
    assert floored is not None
    assert floored.body_df["remember"] >= 100
    assert "escalation" not in floored.body_df
    assert _filler_floor_stats(None, ["escalation"], 200) is None


def test_bm25_and_keyword_modes_are_untouched_by_the_flag() -> None:
    memories = _corpus()
    for mode in ("bm25", "keyword"):
        on = search(
            memories, "creds toggles", max_results=3, mode=mode, rescue_expansion=True
        )
        off = search(
            memories,
            "creds toggles",
            max_results=3,
            mode=mode,
            rescue_expansion=False,
        )
        assert [h.id for h in on] == [h.id for h in off]
        assert [h.score for h in on] == [h.score for h in off]


# ---------------------------------------------------------------------------
# No config key
#
# Everything above drives `search()` directly, and that is the whole
# surface: 9.0.0 removed `[behavior] rescue_expansion`. The product ranks
# with the leg off, and bench/retrieval's requery arm is the one caller
# that turns the parameter on.
# ---------------------------------------------------------------------------


def test_the_lane_has_no_config_key_and_the_parameter_defaults_off(
    tmp_path: Path,
) -> None:
    """A config still carrying the 8.x key loads (the loader drops it
    with a notice), `BehaviorConfig` has no such field, and `search()`'s
    parameter stays default-False."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[storage]\ndirectory = '{tmp_path / 'memories'}'\n"
        "[behavior]\nrescue_expansion = true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert not hasattr(cfg.behavior, "rescue_expansion")
    assert not hasattr(BehaviorConfig(), "rescue_expansion")
    assert inspect.signature(search).parameters["rescue_expansion"].default is False


def test_expansion_stats_fetch_covers_kebab_parts_like_the_base_fetch() -> None:
    """A synthesized term can itself be joined — `morph_variants`
    rewrites the tail of a kebab token, so "split-testing" emits
    "split-test" — and `_score_bm25`'s conjunctive fallback prices a
    joined term with no direct hit off its component IDFs. The base
    fetch adds `_kebab_parts` for exactly that reason; a whole-terms
    fetch on the rescue leg would leave those parts at the
    pool-collapsed IDF the provider exists to correct."""
    assert "split-test" in expansion_terms(
        [_stem_token("split-testing")], TABLES, _stem_token
    )

    asked: list[list[str]] = []

    def provider(terms: list[str]) -> None:
        asked.append(list(terms))
        return None

    search(
        [
            _memory("The split test harness reports weekly."),
            _memory("Unrelated inventory shelving notes."),
        ],
        "who owns split-testing here",
        max_results=2,
        rescue_expansion=True,
        corpus_stats_provider=provider,
    )
    assert len(asked) >= 2, "the rescue leg never fetched its own statistics"
    exp_fetch = asked[-1]
    assert "split-test" in exp_fetch
    assert "split" in exp_fetch and "test" in exp_fetch, (
        f"the expansion fetch dropped the components of a joined term: {exp_fetch}"
    )


# ---------------------------------------------------------------------------
# The leg-margin cap (round 3, the LongMemEval preregistration addendum 5)
#
# RRF fuses by RANK, so an unseparated leg votes exactly as hard as a
# confident one. The cap is the only place the fusion can tell them
# apart, which makes both its arithmetic and its withholding behaviour
# load-bearing.
# ---------------------------------------------------------------------------


def _scored(*scores: float) -> list[tuple[Memory, float, list[str]]]:
    """A leg-shaped `[(memory, score, matched)]` list, best first."""
    now = datetime.now(timezone.utc)
    return [
        (_memory(f"body {i}", created=now - timedelta(days=i)), s, ["term"])
        for i, s in enumerate(scores)
    ]


def _scored_matched(
    *pairs: tuple[float, int],
) -> list[tuple[Memory, float, list[str]]]:
    """A leg-shaped list of `(score, matched-term-count)` pairs."""
    now = datetime.now(timezone.utc)
    return [
        (
            _memory(f"body {i}", created=now - timedelta(days=i)),
            score,
            [f"t{j}" for j in range(n)],
        )
        for i, (score, n) in enumerate(pairs)
    ]


def test_leg_top_evidence_counts_the_rank_one_candidates_matches() -> None:
    assert _leg_top_evidence(_scored_matched((10.0, 3), (5.0, 1))) == 3
    assert _leg_top_evidence(_scored_matched((10.0, 1), (5.0, 4))) == 1
    assert _leg_top_evidence([]) == 0


def test_leg_top_evidence_reads_the_fusion_ordering_not_list_order() -> None:
    """`_id_order` sorts by `(score, created, id)` before fusion, so the
    candidate judged must be the one that would have voted — not
    whatever order `_score_bm25` happened to return."""
    assert _leg_top_evidence(_scored_matched((3.0, 1), (12.0, 3), (7.0, 2))) == 3


def test_the_evidence_bar_is_the_preregistered_one() -> None:
    """Addendum 7 fixed this before the code existed, argued as the
    minimum non-trivial count and confirmed against true labels: on 39
    engaged dev legs it withholds 3 of 3 harmful and 0 of 21 helpful."""
    assert _RESCUE_LEG_MIN_EVIDENCE == 2


def test_the_shipped_lane_form_is_the_flat_weight() -> None:
    """The lane ships FLAT above the evidence floor, and the scaled form
    is a committed non-default variant.

    The decision follows the audience. Both forms were preregistered and
    measured; scaling wins on conversational stores and loses on
    technical ones, and every scaled figure on the conversational corpus
    is still below that corpus's lane-off baseline — so a conversational
    store is better off leaving the lane off entirely than running any
    form of it. The audience that turns it on holds technical prose, and
    for them flat measured 0.90 as-asked recall@5 against 0.80 under
    both scaled forms.
    """
    assert _RESCUE_LEG_EVIDENCE_SCALING is False
    assert _leg_evidence_weight(2) == pytest.approx(_RESCUE_LEG_WEIGHT)
    assert _leg_evidence_weight(9) == pytest.approx(_RESCUE_LEG_WEIGHT)
    # The floor is untouched by the form.
    assert _leg_evidence_weight(1) == 0.0


def test_the_scaled_variant_is_still_reachable_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kept rather than deleted: it is the campaign's best held-out
    configuration and its record is published. It must stay bounded and
    monotone so the safety argument survives the demotion."""
    monkeypatch.setattr("bettermemory.search._RESCUE_LEG_EVIDENCE_SCALING", True)
    weights = [_leg_evidence_weight(m) for m in range(0, 12)]
    assert all(0.0 <= w <= _RESCUE_LEG_WEIGHT for w in weights)
    assert weights == sorted(weights)
    assert _leg_evidence_weight(2) == pytest.approx(_RESCUE_LEG_WEIGHT * 2 / 3)
    assert _leg_evidence_weight(3) == pytest.approx(_RESCUE_LEG_WEIGHT)


def test_the_lane_form_is_invisible_with_the_lane_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither form may be observable to a default install."""
    memories = _corpus()
    query = "do we ever rip the old toggles back out"
    baseline = search(memories, query, max_results=3)
    for scaling in (False, True):
        monkeypatch.setattr("bettermemory.search._RESCUE_LEG_EVIDENCE_SCALING", scaling)
        got = search(memories, query, max_results=3)
        assert [h.id for h in got] == [h.id for h in baseline], scaling
        assert [h.score for h in got] == [h.score for h in baseline], scaling


def test_the_leg_weight_scales_with_its_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variant curve: withheld below the floor, two thirds of full weight
    at the floor, full weight at `_EVIDENCE_FULL_AT`, capped above.

    The floor value is `2/3` because the weight is the fraction of the
    full-evidence bar the leg reaches — round 7 replaced round 6's
    offset form, which put it at 1/2 and cost the dev set."""
    monkeypatch.setattr("bettermemory.search._RESCUE_LEG_EVIDENCE_SCALING", True)
    assert _leg_evidence_weight(0) == 0.0
    assert _leg_evidence_weight(1) == 0.0
    assert _leg_evidence_weight(2) == pytest.approx(_RESCUE_LEG_WEIGHT * 2 / 3)
    assert _leg_evidence_weight(3) == pytest.approx(_RESCUE_LEG_WEIGHT)
    assert _leg_evidence_weight(9) == pytest.approx(_RESCUE_LEG_WEIGHT)


def test_the_leg_weight_is_bounded_and_monotone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded in [0, _RESCUE_LEG_WEIGHT] by construction, so the change
    can only ever REDUCE the leg's influence relative to the flat
    weight, never amplify it — the safety argument addendum 9 makes."""
    monkeypatch.setattr("bettermemory.search._RESCUE_LEG_EVIDENCE_SCALING", True)
    weights = [_leg_evidence_weight(m) for m in range(0, 12)]
    assert all(0.0 <= w <= _RESCUE_LEG_WEIGHT for w in weights)
    assert weights == sorted(weights)
    assert max(weights) == pytest.approx(_RESCUE_LEG_WEIGHT)


def test_the_full_weight_anchor_is_the_preregistered_one() -> None:
    """Read off the dev labels by a stated rule — the count where they
    first reach 100% helpful. A later edit re-opens the experiment."""
    assert _EVIDENCE_FULL_AT == 3


def test_one_matched_term_is_a_coincidence_and_two_is_evidence() -> None:
    """The rule in one line, at the boundary."""
    assert _leg_top_evidence(_scored_matched((9.0, 1))) < _RESCUE_LEG_MIN_EVIDENCE
    assert _leg_top_evidence(_scored_matched((9.0, 2))) >= _RESCUE_LEG_MIN_EVIDENCE


def test_a_leg_with_two_agreeing_terms_still_rescues() -> None:
    """The pure-paraphrase shape survives the rule when the body is
    reachable by two synthesized terms — 'creds' emits 'credential' and
    'secret', and a body carrying both is evidence rather than
    coincidence."""
    memories = [
        _memory("Credential and secret injection for containers uses files."),
        _memory("The reconciliation job runs at 0300 UTC."),
    ]
    legs: dict[str, str] = {}
    hits = search(
        memories, "creds", max_results=2, matched_leg_out=legs, rescue_expansion=True
    )
    assert hits and hits[0].id == memories[0].id
    assert legs[hits[0].id] == "expansion"


def test_an_unseparated_leg_is_withheld_from_the_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The withholding wiring, isolated from fixture luck.

    The count is pinned by its own unit tests above; what this pins is
    that `search()` acts on its verdict. Driving `_leg_top_evidence`
    directly rather than hunting a corpus whose leg happens to rest on
    one term keeps the test about the wiring.

    What is asserted is the RANKING, not score equality with
    `rescue_expansion=False`: the filler df-floor is keyed on
    `rescue_expansion` rather than on the leg, so it still prices the
    base legs when the leg is withheld. Addenda 5 and 6 both overstated
    that as "byte-identical", corrected in the preregistration.
    """
    now = datetime.now(timezone.utc)
    memories = _corpus() + [
        _memory(f"note {i} about flags toggles and rollout staging", created=now)
        for i in range(6)
    ]
    query = "do we ever rip the old toggles back out"

    legs_voting: dict[str, str] = {}
    monkeypatch.setattr("bettermemory.search._leg_top_evidence", lambda scored: 99)
    voting = search(
        memories,
        query,
        max_results=3,
        matched_leg_out=legs_voting,
        rescue_expansion=True,
    )

    legs_withheld: dict[str, str] = {}
    monkeypatch.setattr("bettermemory.search._leg_top_evidence", lambda scored: 0)
    withheld = search(
        memories,
        query,
        max_results=3,
        matched_leg_out=legs_withheld,
        rescue_expansion=True,
    )
    off = search(memories, query, max_results=3, rescue_expansion=False)

    assert "expansion" not in legs_withheld.values()
    assert [h.id for h in withheld] == [h.id for h in off]
    # The withheld run is the base fusion; the voting run is not.
    assert [h.score for h in withheld] != [h.score for h in voting]
    # Scores match flag-off here only because the filler floor happens
    # to be inert on this fixture. That is a property of the corpus,
    # not of the mechanism, so it is stated rather than asserted — the
    # floor is keyed on `rescue_expansion`, and on a store where it
    # bites, a withheld leg and a flag-off query differ by exactly it.


def test_the_cap_never_touches_a_query_the_gate_already_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confident query never engages the leg, so no value of θ may
    change its result. The cap sits strictly inside the coverage gate."""
    memories = _corpus()
    query = "feature flag removal owner deadline"
    baseline = search(memories, query, max_results=3, rescue_expansion=True)
    for bar in (0, 2, 99):
        monkeypatch.setattr("bettermemory.search._RESCUE_LEG_MIN_EVIDENCE", bar)
        got = search(memories, query, max_results=3, rescue_expansion=True)
        assert [h.id for h in got] == [h.id for h in baseline], bar
        assert [h.score for h in got] == [h.score for h in baseline], bar


def test_the_cap_is_inert_with_the_lane_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default install can observe this constant at any value."""
    memories = _corpus()
    query = "do we ever rip the old toggles back out"
    baseline = search(memories, query, max_results=3)
    for bar in (0, 2, 99):
        monkeypatch.setattr("bettermemory.search._RESCUE_LEG_MIN_EVIDENCE", bar)
        got = search(memories, query, max_results=3)
        assert [h.id for h in got] == [h.id for h in baseline], bar
        assert [h.score for h in got] == [h.score for h in baseline], bar
