"""Tests for the Lane L conversational repairs (`search(conversational=...)`).

The mechanism pair, declared in bench/l/L1_DECLARATION.md §3: a
temporal-SCAFFOLD df-floor through the filler-floor stats seam (L1-S),
and date-anchor selection inside the fused ranking's near-tie band
(L1-T), both engaged only when the flag is on AND the query parses as
temporal. These tests pin the properties the declaration leans on —
and, since the 6.1.0 ship, the default-on contract: the engine default
matches the product default (ON, `[behavior] conversational` opting
out), `conversational=False` reproduces the pre-lane ranking, a
non-temporal query is inert either way, the parser reads the declared
shapes against the caller's clock, anchors resolve
header-then-body-then-created, and the repairs move an adversarial
ranking the way the L1 miss anatomy says the real corpus needs —
without touching keyword/bm25 modes or the stopword fallback.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    CorpusStats,
    _CONV_SCAFFOLD_STEMS,
    _conv_scaffold_terms,
    _memory_anchor_day,
    _scaffold_floor_stats,
    _temporal_reading,
    search,
)

_NOW = datetime(2023, 3, 25, 2, 46, tzinfo=timezone.utc)


def _memory(body: str, *, offset_seconds: int = 0) -> Memory:
    # Distinct, monotone `created` stamps keep every tiebreak
    # deterministic without mocking the clock. Created deliberately
    # POSTDATES the 2023 anchors, the harness's ingest shape.
    created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=offset_seconds
    )
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["longmemeval"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _ids(hits) -> list[str]:
    return [h.id for h in hits]


def _pairs(hits) -> list[tuple[str, float]]:
    return [(h.id, h.score) for h in hits]


# ---------------------------------------------------------------------------
# The temporal parser
# ---------------------------------------------------------------------------


def test_elapsed_ask_selects_earliest() -> None:
    r = _temporal_reading("How many days ago did I buy a smoker?", _NOW)
    assert r.window is None
    assert r.selector == "earliest"
    assert r.is_temporal


def test_adverbial_last_selects_latest() -> None:
    r = _temporal_reading(
        "How many months have passed since I last visited a museum?", _NOW
    )
    assert r.selector == "latest"


def test_order_ask_selects_earliest_despite_latest_wording() -> None:
    r = _temporal_reading(
        "What is the order of the three trips I took, from earliest to latest?",
        _NOW,
    )
    assert r.selector == "earliest"


def test_month_name_builds_window_with_year_rollback() -> None:
    # Query month after `now`'s month resolves to the PREVIOUS year.
    r = _temporal_reading("What did I plan in May?", _NOW)
    assert r.window == (date(2022, 5, 1), date(2022, 5, 31))
    # Query month at or before `now`'s month stays in `now`'s year.
    r2 = _temporal_reading("What did I do in March?", _NOW)
    assert r2.window == (date(2023, 3, 1), date(2023, 3, 31))


def test_last_month_is_a_calendar_window_not_a_selector() -> None:
    r = _temporal_reading("How many plants did I acquire in the last month?", _NOW)
    assert r.window == (date(2023, 2, 1), date(2023, 2, 28))
    # 'last month' must not read as the adverbial latest-selector.
    assert r.selector != "latest"


def test_month_range_merges_to_envelope() -> None:
    r = _temporal_reading("What happened between January and March?", _NOW)
    assert r.window == (date(2023, 1, 1), date(2023, 3, 31))


def test_plain_question_is_not_temporal() -> None:
    r = _temporal_reading("What degree did I graduate with?", _NOW)
    assert not r.is_temporal


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_anchor_prefers_leading_bracket_header() -> None:
    m = _memory("[2023/03/15 (Wed) 06:05]\nuser: bought a smoker on 2023-03-16.")
    assert _memory_anchor_day(m) == date(2023, 3, 15)


def test_anchor_falls_back_to_early_body_date() -> None:
    m = _memory("The deploy on 2024-02-01 rolled back cleanly.")
    assert _memory_anchor_day(m) == date(2024, 2, 1)


def test_anchor_falls_back_to_created_day() -> None:
    m = _memory("No dates anywhere in this body.")
    assert _memory_anchor_day(m) == m.created.date()


def test_anchor_ignores_invalid_calendar_dates() -> None:
    m = _memory("[2023/13/45 (Xxx) 99:99]\nuser: malformed header.")
    assert _memory_anchor_day(m) == m.created.date()


# ---------------------------------------------------------------------------
# The scaffold class and floor
# ---------------------------------------------------------------------------


def test_scaffold_class_is_bounded_and_stemmed() -> None:
    # Declaration §3 caps the class at 40 stems.
    assert len(_CONV_SCAFFOLD_STEMS) <= 40
    assert "day" in _CONV_SCAFFOLD_STEMS
    assert "mani" in _CONV_SCAFFOLD_STEMS  # 'many', through the live stemmer
    assert "smoker" not in _CONV_SCAFFOLD_STEMS


def test_scaffold_terms_take_small_numerals_not_years() -> None:
    terms = _conv_scaffold_terms(["day", "ago", "3", "2023", "smoker", "16.3"])
    assert terms == ["day", "ago", "3"]


def test_scaffold_floor_reprices_only_scaffold_terms() -> None:
    from bettermemory.search import _CONV_SCAFFOLD_FLOOR_RATIO

    stats = _scaffold_floor_stats(None, ["day", "smoker"], 200)
    assert stats is not None
    assert stats.size == 200
    # The floor tracks the tuned ratio, whatever the frontier set it to.
    assert stats.body_df["day"] == max(1, int(200 * _CONV_SCAFFOLD_FLOOR_RATIO))
    assert "smoker" not in stats.body_df


def test_scaffold_floor_keeps_honest_direction() -> None:
    from bettermemory.search import _CONV_SCAFFOLD_FLOOR_RATIO

    base = CorpusStats(size=200, body_df={"day": 150}, scope_df={"day": 150})
    floored = _scaffold_floor_stats(base, ["day"], 200)
    assert floored is not None
    # max(real df, floor): the floor can only make a scaffold term look
    # MORE common, never rarer than the collection says it is.
    assert floored.body_df["day"] == max(
        150, max(1, int(200 * _CONV_SCAFFOLD_FLOOR_RATIO))
    )


def test_scaffold_floor_without_scaffold_terms_returns_base_object() -> None:
    base = CorpusStats(size=10, body_df={}, scope_df={})
    assert _scaffold_floor_stats(base, ["smoker", "brisket"], 10) is base


# ---------------------------------------------------------------------------
# Search-level behaviour
# ---------------------------------------------------------------------------


def _adversarial_store() -> list[Memory]:
    """The L1 anatomy's shape in miniature: the gold narration matches
    the query's CONTENT once; the distractors match its temporal
    scaffold repeatedly. Distinct anchor days, gold earliest."""
    return [
        _memory(
            "[2023/03/15 (Wed) 06:05]\nuser: I bought a smoker today and "
            "seasoned it before the first cook.",
            offset_seconds=0,
        ),
        _memory(
            "[2023/03/20 (Mon) 10:00]\nuser: How many days should I marinate "
            "brisket? A day or two ago I read many opinions about how long.",
            offset_seconds=1,
        ),
        _memory(
            "[2023/03/22 (Wed) 09:00]\nuser: So many days of rain lately; "
            "weeks ago we had plans, how long until it clears?",
            offset_seconds=2,
        ),
    ]


def test_default_is_on_and_false_reproduces_the_pre_lane_ranking() -> None:
    import inspect

    # The engine default is the product default: ON since the 6.1.0
    # ship (bench/l/L1_RECORD.md's owner door, taken 2026-08-16).
    assert inspect.signature(search).parameters["conversational"].default is True

    mems = _adversarial_store()
    q = "How many days ago did I buy a smoker?"
    default = search(mems, q, mode="hybrid", max_results=3, now=_NOW)
    lane_on = search(
        mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True
    )
    lane_off = search(
        mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=False
    )
    # A bare call ranks lane-on…
    assert _pairs(default) == _pairs(lane_on)
    # …and the opt-out reproduces the pre-lane ranking: the anatomy's
    # shape, scaffold matcher on top, the narration displaced.
    assert _ids(lane_off)[0] != mems[0].id
    assert _pairs(lane_off) != _pairs(lane_on)


def test_ranking_surfaces_carry_the_conversational_flag() -> None:
    """The same parity contract `rescue_expansion` carries: a flag the
    silent-miss probe cannot see makes the probe rank differently from
    production, and the miss verdict reads only rank 1."""
    import inspect

    from bettermemory.audit import probe_for_miss
    from bettermemory.config import BehaviorConfig
    from bettermemory.handlers.search import RankingInputs

    assert "conversational" in RankingInputs._fields
    assert "conversational" in inspect.signature(probe_for_miss).parameters
    assert BehaviorConfig().conversational is True
    assert BehaviorConfig(conversational=False).conversational is False


def test_non_temporal_query_is_inert_under_the_flag() -> None:
    mems = _adversarial_store()
    q = "wood pellets for the smoker"
    off = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=False)
    on = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    assert _pairs(off) == _pairs(on)


def test_lane_lifts_content_match_over_scaffold_matchers(monkeypatch) -> None:
    mems = _adversarial_store()
    q = "How many days ago did I buy a smoker?"
    off = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=False)
    on = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    # Off: scaffold matchers outrank the narration (the anatomy's shape,
    # gold last in this deliberately pessimistic one-content-token store).
    assert _ids(off)[0] != mems[0].id
    # The floor alone strictly improves the narration's rank even here —
    # the keyword leg still credits scaffold, so #1 needs richer content
    # overlap (the real corpus) or the anchor bonus (below).
    assert _ids(on).index(mems[0].id) < _ids(off).index(mems[0].id)
    # With L1-T's earliest bonus pinned at its mechanism magnitude, the
    # composed lane puts the narration first.
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_SELECTOR_BOOST", 0.25)
    composed = search(
        mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True
    )
    assert _ids(composed)[0] == mems[0].id


def test_selector_orders_by_anchor_and_flips_with_adverbial_last(
    monkeypatch,
) -> None:
    # L1-T's MECHANISM is pinned under explicit nonzero magnitudes so
    # these tests keep guarding it while the tuning frontier moves the
    # shipped defaults (read-2 zeroed them to isolate the floor).
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_SELECTOR_BOOST", 0.25)
    twin_a = _memory(
        "[2023/03/01 (Wed) 08:00]\nuser: visited the museum exhibit downtown.",
        offset_seconds=0,
    )
    twin_b = _memory(
        "[2023/03/18 (Sat) 08:00]\nuser: visited the museum exhibit downtown.",
        offset_seconds=1,
    )
    mems = [twin_a, twin_b]
    earliest = search(
        mems,
        "How many weeks ago did I visit the museum?",
        mode="hybrid",
        max_results=2,
        now=_NOW,
        conversational=True,
    )
    assert _ids(earliest)[0] == twin_a.id
    latest = search(
        mems,
        "How many weeks have passed since I last visited the museum?",
        mode="hybrid",
        max_results=2,
        now=_NOW,
        conversational=True,
    )
    assert _ids(latest)[0] == twin_b.id


def test_window_boosts_in_window_anchor(monkeypatch) -> None:
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_WINDOW_BOOST", 0.30)
    monkeypatch.setattr(engine, "_CONV_WINDOW_DEMOTE", 0.15)
    feb = _memory(
        "[2023/02/10 (Fri) 08:00]\nuser: repotted the snake plant carefully.",
        offset_seconds=0,
    )
    mar = _memory(
        "[2023/03/12 (Sun) 08:00]\nuser: repotted the snake plant carefully.",
        offset_seconds=1,
    )
    mems = [feb, mar]
    hits = search(
        mems,
        "Which snake plant did I repot in the last month?",
        mode="hybrid",
        max_results=2,
        now=_NOW,
        conversational=True,
    )
    # `now` is 2023-03-25, so "last month" is February.
    assert _ids(hits)[0] == feb.id


def test_keyword_and_bm25_modes_are_untouched() -> None:
    mems = _adversarial_store()
    q = "How many days ago did I buy a smoker?"
    for mode in ("keyword", "bm25"):
        off = search(mems, q, mode=mode, max_results=3, now=_NOW, conversational=False)
        on = search(mems, q, mode=mode, max_results=3, now=_NOW, conversational=True)
        assert _pairs(off) == _pairs(on)


def test_stopword_fallback_skips_the_lane() -> None:
    # A query whose every token is a stopword rides the fallback TF
    # stream; the lane must not engage there (declaration §3 scope).
    mems = [_memory("what is the where is the", offset_seconds=0)]
    q = "what is the"
    off = search(mems, q, mode="hybrid", max_results=1, now=_NOW, conversational=False)
    on = search(mems, q, mode="hybrid", max_results=1, now=_NOW, conversational=True)
    assert _pairs(off) == _pairs(on)


def test_lane_on_is_deterministic() -> None:
    mems = _adversarial_store()
    q = "How many days ago did I buy a smoker?"
    a = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    b = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    assert _pairs(a) == _pairs(b)


# ---------------------------------------------------------------------------
# L2 — the pricing gate's widening and the keyword-leg repricing
# (bench/l/L2_DECLARATION.md §3; dark by default, arms via config commits)
# ---------------------------------------------------------------------------


def _count_ask_store() -> list[Memory]:
    """The L2 anatomy's untreated cluster in miniature: a count ask
    with no window and no selector, a gold matching its content, and a
    distractor matching its scaffold repeatedly."""
    return [
        _memory(
            "[2023/03/12 (Sun) 11:00]\nuser: I went to visit the doctor "
            "about my knee; Dr Patel says the knee is healing.",
            offset_seconds=0,
        ),
        _memory(
            "[2023/03/18 (Sat) 09:30]\nuser: So many appointments in the "
            "past month, and many many things besides — the past weeks ran "
            "long, month after month.",
            offset_seconds=1,
        ),
        _memory(
            "[2023/03/21 (Tue) 15:00]\nuser: How many days of rain in the "
            "past month? Many, and the month is not done.",
            offset_seconds=2,
        ),
    ]


_COUNT_ASK = "How many doctors did I visit in the past month?"


def test_l2_dark_state_leaves_count_asks_inert(monkeypatch) -> None:
    # The dark contract, value-agnostic: with BOTH constants None — the
    # implementation-commit state, and the reversion point if the owner
    # declines the ship — a scaffold-shaped, non-temporal query never
    # enters the lane: on equals off, byte for byte. The constants'
    # committed values move with the declared tuning reads and are
    # pinned by the artifacts, not by this suite.
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", None)
    monkeypatch.setattr(engine, "_CONV_KEYWORD_SCAFFOLD_WEIGHT", None)
    mems = _count_ask_store()
    off = search(
        mems, _COUNT_ASK, mode="hybrid", max_results=3, now=_NOW, conversational=False
    )
    on = search(
        mems, _COUNT_ASK, mode="hybrid", max_results=3, now=_NOW, conversational=True
    )
    assert _pairs(off) == _pairs(on)


def test_scaffold_shaped_predicate(monkeypatch) -> None:
    import bettermemory.search as engine

    from bettermemory.search import _conv_scaffold_shaped, _strip_stopwords, tokenize

    def toks(q: str) -> list[str]:
        return _strip_stopwords(tokenize(q))

    # None: constant-False, whatever the query.
    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", None)
    assert not _conv_scaffold_shaped(toks(_COUNT_ASK))
    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", 2)
    # A count ask carries the class in co-occurrence plus content.
    assert _conv_scaffold_shaped(toks(_COUNT_ASK))
    # One scaffold stem is not a shape.
    assert not _conv_scaffold_shaped(toks("how many doctors treated my knee"))
    # All scaffold, no content: not priced (nothing left to rank on).
    assert not _conv_scaffold_shaped(toks("how many days in the past month"))
    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", 3)
    assert _conv_scaffold_shaped(toks("how many times in the past month did I bake"))
    # Two stems clear 2 but not 3 — the threshold is live.
    assert not _conv_scaffold_shaped(toks("how many doctors did I visit this week"))


def test_widened_gate_reprices_count_asks(monkeypatch) -> None:
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", 2)
    monkeypatch.setattr(engine, "_CONV_KEYWORD_SCAFFOLD_WEIGHT", 0.0)
    mems = _count_ask_store()
    off = search(
        mems, _COUNT_ASK, mode="hybrid", max_results=3, now=_NOW, conversational=False
    )
    on = search(
        mems, _COUNT_ASK, mode="hybrid", max_results=3, now=_NOW, conversational=True
    )
    # Off: the scaffold matcher outprices the narration in both legs.
    assert _ids(off)[0] != mems[0].id
    # On: both legs price content alone, and the narration leads.
    assert _ids(on)[0] == mems[0].id


def test_keyword_repricing_weights_and_content_coverage() -> None:
    from bettermemory.search import score_memory

    q = ["doctor", "visit", "mani", "past", "month"]
    scaffold = frozenset({"mani", "past", "month"})
    gold = _memory("user: I went to visit the doctor; the doctor is Dr Patel.")
    lookalike = _memory("user: many many past months, past month after month, so many.")
    stock_gold, _ = score_memory(gold, q, now=_NOW)
    priced_gold, gold_matched = score_memory(
        gold, q, now=_NOW, scaffold_terms=scaffold, scaffold_weight=0.0
    )
    # Content coverage: the gold matches every content term, so its
    # priced coverage multiplier is full — above its stock one, where
    # scaffold dilutes the denominator.
    assert priced_gold > stock_gold
    assert set(gold_matched) == {"doctor", "visit"}
    # A candidate whose every match is scaffold leaves the leg whole.
    priced_look, look_matched = score_memory(
        lookalike, q, now=_NOW, scaffold_terms=scaffold, scaffold_weight=0.0
    )
    assert (priced_look, look_matched) == (0.0, [])
    # At an interior weight the scaffold hits still append to matched —
    # display and evidence read as before — while the contribution is
    # priced down.
    part, part_matched = score_memory(
        lookalike, q, now=_NOW, scaffold_terms=scaffold, scaffold_weight=0.25
    )
    assert 0.0 < part < score_memory(lookalike, q, now=_NOW)[0]
    assert set(part_matched) == {"mani", "past", "month"}


def test_widening_leaves_explicit_modes_pure(monkeypatch) -> None:
    import bettermemory.search as engine

    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", 2)
    monkeypatch.setattr(engine, "_CONV_KEYWORD_SCAFFOLD_WEIGHT", 0.0)
    mems = _count_ask_store()
    for mode in ("keyword", "bm25"):
        off = search(
            mems, _COUNT_ASK, mode=mode, max_results=3, now=_NOW, conversational=False
        )
        on = search(
            mems, _COUNT_ASK, mode=mode, max_results=3, now=_NOW, conversational=True
        )
        assert _pairs(off) == _pairs(on)


def test_all_scaffold_temporal_query_leaves_keyword_leg_stock(monkeypatch) -> None:
    # A temporal query with no content term keeps the shipped shape:
    # the floor applies, the keyword leg stays stock, and the weight
    # constant has nothing to change.
    import bettermemory.search as engine

    mems = _adversarial_store()
    q = "how many days ago?"
    monkeypatch.setattr(engine, "_CONV_SCAFFOLD_MIN_STEMS", 2)
    dark = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    monkeypatch.setattr(engine, "_CONV_KEYWORD_SCAFFOLD_WEIGHT", 0.0)
    lit = search(mems, q, mode="hybrid", max_results=3, now=_NOW, conversational=True)
    assert _pairs(dark) == _pairs(lit)
