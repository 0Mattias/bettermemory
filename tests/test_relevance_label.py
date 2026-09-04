"""What the `relevance` label measures.

`relevance` is LEXICAL COVERAGE — the fraction of distinct query tokens
that literally appear in the hit. Pre-4.0 the semantic leg made that
honest-but-misleading: a pure-paraphrase hit carried `match_terms=[]`,
coverage 0.0, and the label "low", while the then-current tool
description told callers to treat "low" as probable noise — so the
embeddings extra's whole capability arrived wearing the mark of junk.
The recut shipped EVIDENCE rather than a replacement verdict, and the
pieces that outlived the 4.0.0 strip still carry it:

- `matched_leg` says WHICH RANKER surfaced the hit. Every ranker is
  lexical since 4.0.0, so its live value is `lexical`; the field
  survives for parsers keyed on it, and for a future CODE ranker to
  earn a leg of its own.
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
from ._mcp import call_tool as _mcp_call

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


# ---------------------------------------------------------------------------
# matched_leg
# ---------------------------------------------------------------------------


def test_a_lexical_only_hit_reports_the_lexical_leg() -> None:
    legs: dict[str, str] = {}
    m = _memory("rollback the migration script lives in db/migrate")
    hits = search([m], "rollback migration", matched_leg_out=legs)
    assert [h.id for h in hits] == [m.id]
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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


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
# mode. On a default install the legs that run are keyword+BM25 and the
# leg is exactly `lexical` — the exact string is the point of these two:
# a `matched_leg in {...}` assertion would pass against a field that
# reported the requested mode rather than the legs that ran. Their
# pre-4.0 ancestors were scoped to the no-extras CI jobs, because an
# installed embeddings extra flipped the same hit to `both`; the lane
# left for good in 6.0.0, so the `lexical` string is now unconditional
# and the `no_extras` marker went with the registration the strip
# removed.
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

    "treat low as probable noise" over-read the label pre-4.0 (a
    paraphrase hit wore it while being exactly what was asked for) and
    over-reads it now in the other direction: low coverage is a fact
    about wording overlap, and the actionable response is a re-query
    with different nouns, not a dismissal. The description must state
    what the label measures and stop short of a quality verdict —
    scoped to the `relevance` line so an unrelated future bullet can
    still use the word.
    """
    line = _desc_line("`relevance`")
    assert "noise" not in line
    assert "not how good it is" in line


def test_the_description_does_not_promise_expand_top_gates_on_high() -> None:
    """It stopped being true; a resident surface may not lag the code."""
    line = _desc_line("`expand_top=True`")
    assert 'relevance is "high"' not in line
    assert "score lead" in line
