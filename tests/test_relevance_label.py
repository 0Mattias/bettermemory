"""What the `relevance` label measures, and what `matched_leg` adds.

`relevance` is LEXICAL COVERAGE — the fraction of distinct query tokens
that literally appear in the hit. `_score_semantic` deliberately reports
only the tokens that literally hit, so a pure-paraphrase result carries
`match_terms=[]`, coverage 0.0, and the label "low". That is honest about
coverage and misleading about the hit: it is the field the tool
description told callers to treat as noise, and the field `expand_top`
refused to expand on, so the embeddings extra's whole capability arrived
wearing the mark of junk.

The recut ships EVIDENCE rather than a replacement verdict:

- `matched_leg` says WHICH RANKER surfaced the hit, so "low" on a
  `semantic` leg reads as "matched by meaning, shares no words" instead
  of "probably noise".
- `expand_top` gates on the coverage label OR a decisive score lead, so a
  hit no lexical rule can call "high" can still reach the affordance.
- The label rule itself is UNCHANGED. The cosine-band recut was measured
  and closed negative — `search._relevance_label` carries the numbers and
  the reason.

The tests here pin all three, plus the boundary arithmetic of the margin,
because each one is a thing that silently regresses to the old behaviour
if someone "simplifies" it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _RRF_K_DEFAULT,
    EXPAND_TOP_SCORE_MARGIN,
    search,
    top_hit_leads_runner_up,
)
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


def _memory(body: str, scopes: list[str] | None = None) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


class _StubModel:
    """SentenceTransformer-shaped stub returning canned unit vectors.

    Keyed on the exact stripped text so a test can make one body a
    paraphrase of the query (cosine 1.0) while sharing no tokens with it,
    which is the only way to produce a genuinely semantic-only hit
    without the embeddings extra installed.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
        return self.vectors.get(text.strip(), [0.0, 0.0, 1.0])


# ---------------------------------------------------------------------------
# matched_leg
# ---------------------------------------------------------------------------


def test_a_lexical_only_hit_reports_the_lexical_leg() -> None:
    legs: dict[str, str] = {}
    m = _memory("rollback the migration script lives in db/migrate")
    hits = search([m], "rollback migration", matched_leg_out=legs)
    assert [h.id for h in hits] == [m.id]
    assert legs == {m.id: "lexical"}


def test_a_pure_semantic_hit_is_labelled_low_and_says_why() -> None:
    """The defect, made legible rather than papered over.

    The paraphrase body shares NO tokens with the query, so
    `match_terms` is empty and the coverage label is "low" — that stays
    true, because inventing matched terms is what
    `_score_semantic`'s contract exists to prevent. What changes is that
    the hit now also says it arrived on the semantic leg, which is the
    difference between "this is noise" and "this matched by meaning".

    Without `matched_leg` the two are indistinguishable on the wire: a
    stopword-grade lexical miss and a cosine-1.0 paraphrase both come
    back `relevance="low", match_terms=[]`.
    """
    query = "rollback the migration"
    paraphrase = _memory("undoing a schema change in production")
    legs: dict[str, str] = {}
    hits = search(
        [paraphrase],
        query,
        mode="hybrid",
        semantic_model=_StubModel(
            {query: [1.0, 0.0, 0.0], paraphrase.body: [1.0, 0.0, 0.0]}
        ),
        matched_leg_out=legs,
    )
    assert [h.id for h in hits] == [paraphrase.id]
    assert hits[0].match_terms == []
    assert hits[0].relevance == "low"
    assert legs == {paraphrase.id: "semantic"}


def test_a_hit_both_rankers_found_reports_both() -> None:
    query = "rollback the migration"
    shared = _memory("rollback the migration by hand")
    legs: dict[str, str] = {}
    search(
        [shared],
        query,
        mode="hybrid",
        semantic_model=_StubModel(
            {query: [1.0, 0.0, 0.0], shared.body: [1.0, 0.0, 0.0]}
        ),
        matched_leg_out=legs,
    )
    assert legs == {shared.id: "both"}


def test_a_lexical_hit_below_the_cosine_threshold_is_not_called_both() -> None:
    """The leg is what SCORED the hit, not what was configured.

    `_score_semantic` drops anything under cosine 0.3, so an orthogonal
    body never entered the semantic ranking even though a model was
    supplied. Reporting "both" here would make the field a restatement
    of `semantic_model is not None`.
    """
    query = "rollback the migration"
    lexical = _memory("rollback the migration by hand")
    legs: dict[str, str] = {}
    search(
        [lexical],
        query,
        mode="hybrid",
        semantic_model=_StubModel(
            {query: [1.0, 0.0, 0.0], lexical.body: [0.0, 0.0, 1.0]}
        ),
        matched_leg_out=legs,
    )
    assert legs == {lexical.id: "lexical"}


def test_a_degraded_semantic_search_reports_the_leg_that_actually_ran() -> None:
    """`mode="semantic"` is a request; the leg is a report.

    Both semantic branches degrade to a lexical ranking when a loaded
    model raises at encode time. Deriving the leg from `mode` would
    attribute a keyword ordering to an embedding ranker that never
    produced a number — precisely the misattribution this field exists
    to prevent.
    """

    class _Exploding:
        def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
            raise RuntimeError("device fault")

    m = _memory("rollback the migration script")
    legs: dict[str, str] = {}
    hits = search(
        [m],
        "rollback migration",
        mode="semantic",
        semantic_model=_Exploding(),
        matched_leg_out=legs,
    )
    assert [h.id for h in hits] == [m.id]
    assert legs == {m.id: "lexical"}


def test_hybrid_degrading_to_lexical_fusion_reports_no_semantic_leg() -> None:
    class _Exploding:
        def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
            raise RuntimeError("device fault")

    m = _memory("rollback the migration script")
    legs: dict[str, str] = {}
    search(
        [m],
        "rollback migration",
        mode="hybrid",
        semantic_model=_Exploding(),
        matched_leg_out=legs,
    )
    assert legs == {m.id: "lexical"}


@pytest.mark.parametrize("mode", ["keyword", "bm25"])
def test_single_lexical_modes_report_lexical(mode: str) -> None:
    m = _memory("rollback the migration script")
    legs: dict[str, str] = {}
    search([m], "rollback migration", mode=mode, matched_leg_out=legs)  # type: ignore[arg-type]
    assert legs == {m.id: "lexical"}


def test_browse_mode_hits_carry_no_leg() -> None:
    """Nothing ranked them, so there is nothing to report.

    Browse mode (`allow_empty_query` with a stopword-only query) filters
    and date-sorts. Emitting "lexical" here would be a guess dressed as
    evidence, and the handler omits the key entirely rather than ship
    one.
    """
    m = _memory("rollback the migration script")
    legs: dict[str, str] = {}
    hits = search([m], "", allow_empty_query=True, matched_leg_out=legs)
    assert [h.id for h in hits] == [m.id]
    assert legs == {}


def test_the_leg_sink_is_opt_in() -> None:
    """The default caller pays nothing and sees no behaviour change."""
    m = _memory("rollback the migration script")
    with_sink: dict[str, str] = {}
    a = search([m], "rollback migration")
    b = search([m], "rollback migration", matched_leg_out=with_sink)
    assert [h.model_dump() for h in a] == [h.model_dump() for h in b]
    assert with_sink == {m.id: "lexical"}


# ---------------------------------------------------------------------------
# expand_top: the gate that suppressed the semantic leg
# ---------------------------------------------------------------------------


def test_the_expand_margin_is_derived_from_the_fusion_constant() -> None:
    """Pinned as arithmetic, not as a literal.

    A fused score sums `1/(k + rank)`, so a one-rank-slot lead is
    `(k+2)/(k+1)` — the smallest possible non-tie, and no evidence of
    anything. The gate asks for two slots. Writing the number down
    instead would silently stop meaning that the day `rrf_k` moves.
    """
    assert EXPAND_TOP_SCORE_MARGIN == pytest.approx(
        (_RRF_K_DEFAULT + 3) / (_RRF_K_DEFAULT + 1) - 1.0
    )
    one_slot = (_RRF_K_DEFAULT + 2) / (_RRF_K_DEFAULT + 1)
    assert not top_hit_leads_runner_up(one_slot, 1.0)
    two_slots = (_RRF_K_DEFAULT + 3) / (_RRF_K_DEFAULT + 1)
    assert top_hit_leads_runner_up(two_slots, 1.0)


def test_an_all_zero_result_set_leads_nothing() -> None:
    """Browse-mode hits all score 0.0 — a tie, not a win."""
    assert not top_hit_leads_runner_up(0.0, 0.0)
    assert top_hit_leads_runner_up(0.5, 0.0)


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return (
        build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec),
        memory_dir,
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_expand_top_fires_on_a_decisive_lead_without_a_high_label(
    server_with_events: tuple[Any, Path],
) -> None:
    """The suppression this item exists to remove.

    A long query keeps coverage under 0.75, so the top hit cannot be
    labelled "high" no matter how decisively it won — which is exactly
    the shape a pure-semantic hit is permanently stuck in, since its
    coverage is 0 by construction. Before the recut this response came
    back with no `body` at all.
    """
    server, _ = server_with_events
    top = await _call(
        server,
        "memory_write",
        content="postgres replication lag alert runbook for the primary",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_write",
        content="terraform module layout notes",
        scopes=["tools"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="postgres replication lag terraform kubernetes ansible",
            mode="bm25",
            expand_top=True,
        )
    )
    assert hits[0]["id"] == top["id"]
    assert hits[0]["relevance"] != "high"
    assert top_hit_leads_runner_up(hits[0]["score"], hits[1]["score"])
    assert "body" in hits[0]
    assert "runbook" in hits[0]["body"]
    # Still only the top hit — widening the gate must not widen the slice.
    assert "body" not in hits[1]


async def test_expand_top_holds_when_the_runner_up_is_within_a_rank_slot(
    server_with_events: tuple[Any, Path],
) -> None:
    """The gate widened; it did not open.

    Two near-tied hybrid hits differ by exactly one RRF rank slot, which
    is the smallest possible non-tie and no evidence at all. Neither is
    "high". Nothing expands, so the cost story the gate protects
    survives the recut.
    """
    server, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="postgres replication lag alert runbook",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_write",
        content="postgres replication lag dashboard panel",
        scopes=["tools"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="postgres replication kubernetes terraform ansible vault",
            expand_top=True,
        )
    )
    assert len(hits) == 2
    assert all(h["relevance"] != "high" for h in hits)
    assert not top_hit_leads_runner_up(hits[0]["score"], hits[1]["score"])
    assert "body" not in hits[0]


async def test_a_sole_low_hit_still_does_not_expand(
    server_with_events: tuple[Any, Path],
) -> None:
    """ "Won a field of one" is an absence of evidence.

    With no runner-up there is nothing to dominate, so the margin arm
    must not fire. This is also the pin that keeps
    `test_search_expand_top_no_body_when_only_low_relevance` honest from
    the other side.
    """
    server, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="python list comprehension notes",
        scopes=["tools"],
    )
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="python kubernetes networking docker terraform",
            expand_top=True,
        )
    )
    assert len(hits) == 1
    assert hits[0]["relevance"] == "low"
    assert "body" not in hits[0]


# ---------------------------------------------------------------------------
# The wire, and the instrument the negative result asked for
# ---------------------------------------------------------------------------


# Both wire tests below drive a REAL server on its configured default
# mode, so the legs that run are whatever the install offers. On a
# lexical-only install that is keyword+BM25 and the leg is `lexical`;
# with an embeddings extra present the hybrid fuse adds the semantic
# ranker and the same hit reports `both`. The exact string is the point
# of these two — a `matched_leg in {...}` assertion would pass against a
# field that reported the requested mode rather than the legs that ran —
# so they are scoped to the no-extras install with the marker this repo
# already registers for that, rather than loosened. The leg-vs-mode
# distinction under a live semantic ranker is pinned by the unit tests
# above, which construct their model explicitly.
@pytest.mark.no_extras
async def test_matched_leg_rides_the_search_response(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="postgres replication lag runbook",
        scopes=["tools"],
    )
    hits = _unwrap(
        await _call(server, "memory_search", query="postgres replication lag")
    )
    assert hits[0]["matched_leg"] == "lexical"


@pytest.mark.no_extras
async def test_search_events_record_the_matched_leg(
    server_with_events: tuple[Any, Path],
) -> None:
    """The instrument whose absence closed the label recut negative.

    Nothing in the event history recorded whether a hit had a semantic
    leg, so the shadow replay could not separate the population a
    cosine-band rule would have changed from the population it would
    not. Dropping this field puts the next attempt back where this one
    started — blind.
    """
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="postgres replication lag runbook",
        scopes=["tools"],
    )
    await _call(server, "memory_search", query="postgres replication lag")
    searches = [e for e in iter_events(memory_dir) if e.get("kind") == "search"]
    assert searches[-1]["matched_leg"] == ["lexical"]


# ---------------------------------------------------------------------------
# What the description promises about the label
# ---------------------------------------------------------------------------


def _desc_line(fragment: str) -> str:
    from bettermemory.handlers.search import DESC_MEMORY_SEARCH

    return next(seg for seg in DESC_MEMORY_SEARCH.split("\n") if fragment in seg)


def test_the_description_no_longer_calls_a_low_label_noise() -> None:
    """The absolutism was the harm.

    "treat low as probable noise" is a correct reading of a lexical
    result and a wrong one for every paraphrase hit, and the model has
    no way to tell them apart from the label alone. The description has
    to name the pairing instead — scoped to the `relevance` line so an
    unrelated future bullet can still use the word.
    """
    line = _desc_line("`relevance`")
    assert "noise" not in line
    assert "matched_leg" in line


def test_the_description_does_not_promise_expand_top_gates_on_high() -> None:
    """It stopped being true; a resident surface may not lag the code."""
    line = _desc_line("`expand_top=True`")
    assert 'relevance is "high"' not in line
    assert "score lead" in line
