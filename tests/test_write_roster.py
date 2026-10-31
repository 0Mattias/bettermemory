"""ENG-112: the standing roster. Every in-force standing ruling reaches the
review of a standing write.

A ranked leg does not reach a ruling a write breaks without using its
words. So a standing write (its own standing answer at or above
`standing.TAU_STANDING`, or category ambient) is shown, once its relations
are answered, every admitted in-force standing record outside its related
set, one line each, windowed on where the write meets the record. The
writer names the records it touches in `review.named`; each named record
comes back in full and is answered and decided as a related one is.

The case the unit opened on is mirrored here: a ruling whose subject sits
past character 400 of its body, behind its provenance, and a long write
that breaks it without naming it, whose related set is held by records
nearer its other sentences. Before the roster that write committed on its
second call and the ruling was never shown.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, review_counters
from bettermemory.builder import build_server
from bettermemory.events import Recorder
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer

from ._mcp import call_tool
from ._review_rows import (
    FACTS,
    ORIGIN,
    Harness,
    fix_origin_and_identity,
    is_roster,
    set_tau,
)

# The ruling: provenance first, its subject past character 400.
RULING = (
    "RULING given in the planning call on the second day of the quarter, "
    "written down at the request of the lead after a long discussion of "
    "earlier incidents and rollbacks. It holds for every repository until "
    "it is replaced, and it was read back and confirmed word for word before "
    "it was stored here exactly as written, with nothing added and nothing "
    "left out, and with the two incidents named in the ticket tracker under "
    "their own numbers for anyone who needs the history behind it. The rule: "
    "no migration runs against the billing database without a dry run first."
)
RULING_HEAD = "RULING given in the planning call"

# The write: a plan whose one billing sentence breaks the ruling without
# naming it; the rest of it is about the reporting service.
PLAN = (
    "Handoff for the next session on the reporting service. "
    "The dashboard tiles load from the reporting cache. "
    "The reporting cache warms every hour from the warehouse. "
    "Apply the billing database migration straight away, then rebuild the "
    "reporting cache. "
    "The reporting service owner reviews the dashboard tiles after the rebuild."
)

# The records nearer the plan's other sentences, which hold its related set.
NEAR = (
    "The reporting cache warms every hour from the warehouse tables.",
    "Dashboard tiles for the reporting service load from the reporting cache.",
    "The reporting service owner reviews dashboard tile changes after a rebuild.",
)

# A second ruling in force, which the plan does not touch.
OTHER_RULING = "Release notes are written in the past tense."


@pytest.fixture(autouse=True)
def _setup(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)
    WORDS.clear()
    yield
    WORDS.clear()


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _standing(h: Harness, record_id: str, probability: float = 0.9) -> None:
    h.store.put_standing(record_id, StandingAnswer(probability, "asked", "test/test"))


def _seed_case(h: Harness) -> tuple[str, str, list[str]]:
    """The ruling, the other ruling and the near records, all in force;
    the rulings standing."""
    ruling = h.seed(RULING)
    _standing(h, ruling)
    other = h.seed(OTHER_RULING)
    _standing(h, other)
    near = [h.seed(text) for text in NEAR]
    return ruling, other, near


async def _to_roster(
    h: Harness, content: str = PLAN, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The first two calls of a standing write: its review, answered
    unrelated with the write's standing at 0.9; return the answers sent
    and the response."""
    first = await h.write(content)
    sent = h.answers(first, standing=0.9, **kwargs)
    return sent, await h.write(content, review=sent)


def _roster_ids(page: dict[str, Any]) -> list[str]:
    return [line.split(" ", 1)[0] for line in page["review"]["roster"]]


def _line_of(page: dict[str, Any], record_id: str) -> str:
    (line,) = [x for x in page["review"]["roster"] if x.startswith(record_id + " ")]
    return line


# ---------------------------------------------------------------------------
# Who is shown the roster
# ---------------------------------------------------------------------------


async def test_the_case_reaches_its_ruling_only_through_the_roster(
    h: Harness,
) -> None:
    """The unit's case: the related set does not hold the ruling, and the
    standing write is shown it on the roster instead of committing."""
    ruling, other, near = _seed_case(h)
    related = h.related_ids(PLAN)
    assert ruling not in related
    assert set(related) <= set(near)

    _, page = await _to_roster(h)
    assert is_roster(page), page
    assert page["written"] is False
    assert page["review"]["question"] == review.ROSTER_STATEMENT
    listed = _roster_ids(page)
    assert set(listed) == {ruling, other}
    assert not set(listed) & set(related)
    assert h.store.load_all() and all(m.body != PLAN for m in h.store.load_all())


async def test_a_write_that_is_not_standing_is_shown_no_roster(h: Harness) -> None:
    _seed_case(h)
    first = await h.write(PLAN)
    res = await h.answer(first, PLAN, standing=0.1)
    assert res["status"] == "committed", res
    assert all("roster" not in row for row in h.events(review.REVIEW_ISSUED))


async def test_an_ambient_write_is_shown_the_roster(h: Harness) -> None:
    ruling, _, _ = _seed_case(h)
    first = await h.write(PLAN, category="ambient")
    res = await h.write(PLAN, category="ambient", review=h.answers(first, standing=0.0))
    assert is_roster(res), res
    assert ruling in _roster_ids(res)


async def test_with_no_other_standing_record_a_standing_write_commits(
    h: Harness,
) -> None:
    for text in NEAR:
        h.seed(text)
    _, res = await _to_roster(h)
    assert res["status"] == "committed", res


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


async def test_a_line_shows_where_the_write_meets_the_record(h: Harness) -> None:
    """The ruling's line is windowed on the plan's billing sentence, where
    the plan breaks it, not on its provenance head."""
    ruling, other, _ = _seed_case(h)
    _, page = await _to_roster(h)
    line = _line_of(page, ruling)
    updated = h.store.load_one(ruling).updated.date().isoformat()
    assert line.startswith(f"{ruling} ({updated}) ")
    text = line.split(") ", 1)[1]
    assert "billing database" in text
    assert RULING_HEAD not in text
    assert len(text) <= review.ROSTER_TEXT_CHARS + 3
    # A short record shows whole.
    assert _line_of(page, other).endswith(OTHER_RULING)


async def test_the_lines_are_newest_first(h: Harness) -> None:
    ruling, other, _ = _seed_case(h)
    later = h.seed(FACTS[5])
    _standing(h, later)
    _, page = await _to_roster(h)
    assert _roster_ids(page) == [later, other, ruling]


async def test_the_roster_leaves_out_what_the_review_shows_otherwise(
    h: Harness,
) -> None:
    """A declared supersedes target, a record the related set holds, a
    record that is not standing, one superseded, and one in a scope the
    session disabled are not listed."""
    ruling, other, near = _seed_case(h)
    _standing(h, near[0])
    plain = h.seed(FACTS[6])
    old = h.seed(FACTS[7])
    _standing(h, old)
    h.store.write(
        content=FACTS[8],
        scopes=["tools"],
        origin=ORIGIN,
        home=h.home,
        links=[{"type": "supersedes", "target_id": old}],
    )
    off = h.store.write(content=FACTS[9], scopes=["ops"], origin=ORIGIN, home=h.home).id
    _standing(h, off)
    await call_tool(
        h.server, "memory_admin", {"action": "disable_scope", "scope": "ops"}
    )
    first = await h.write(PLAN, supersedes=[other])
    sent = h.answers(first, standing=0.9)
    page = await h.write(PLAN, supersedes=[other], review=sent)
    assert is_roster(page), page
    assert _roster_ids(page) == [ruling]
    assert near[0] in h.related_ids(PLAN)
    assert plain not in _roster_ids(page)


async def test_the_roster_row_is_a_review_of_the_chain(h: Harness) -> None:
    ruling, other, _ = _seed_case(h)
    _, page = await _to_roster(h)
    root, follow = h.events(review.REVIEW_ISSUED)
    assert follow["review_id"] == page["review"]["review_id"]
    assert follow["parent_review_id"] == root["review_id"]
    assert follow["related"] == []
    assert set(follow["roster"]) == {ruling, other}
    assert follow["pre_answered"] is False
    assert follow["response_chars"] > 0
    assert page["review"]["review_id"] in page["hint"]
    assert '"named"' in page["hint"]
    # Ids only on the row: no line text.
    assert "billing" not in str(follow)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


async def test_a_named_ruling_is_shown_in_full_and_a_conflict_refuses(
    h: Harness,
) -> None:
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    assert full["status"] == "review", full
    (entry,) = full["review"]["related"]
    assert entry["id"] == ruling
    assert "dry run first" in entry["text"]
    assert "roster records this review now holds" in full["hint"]
    rows = h.events(review.REVIEW_ISSUED)
    assert rows[-1]["related"] == [ruling]
    assert rows[-1]["parent_review_id"] == rows[0]["review_id"]

    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "conflict"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == review.ACTION_STANDING, res
    assert [row["id"] for row in res["standing"]] == [ruling]
    (row,) = h.events("write")
    assert row["review_answers"]["named"] == [ruling]


async def test_a_named_ruling_answered_unrelated_commits(h: Harness) -> None:
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "unrelated"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == "committed", res
    links = h.store.load_one(res["id"]).links
    assert not any(link.target_id == ruling for link in links)


async def test_a_named_ruling_answered_extends_is_linked(h: Harness) -> None:
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "extends"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == "committed", res
    assert res["extends"] == [{"id": ruling, "evidence": "review"}]


async def test_naming_nothing_commits_and_says_so_on_the_row(h: Harness) -> None:
    _seed_case(h)
    sent, page = await _to_roster(h)
    res = await h.name(page, PLAN, sent, [])
    assert res["status"] == "committed", res
    (row,) = h.events("write")
    assert row["review_answers"]["named"] == []
    assert row["review_id"] == page["review"]["review_id"]


async def test_named_is_required_after_a_roster(h: Harness) -> None:
    _seed_case(h)
    sent, page = await _to_roster(h)
    resend = {**sent, "review_id": page["review"]["review_id"]}
    with pytest.raises(ToolError, match="review.named is missing"):
        await h.write(PLAN, review=resend)


async def test_named_holds_only_roster_ids(h: Harness) -> None:
    _, _, near = _seed_case(h)
    sent, page = await _to_roster(h)
    with pytest.raises(ToolError, match="the roster did not list"):
        await h.name(page, PLAN, sent, [near[0]])


async def test_named_is_a_list_of_ids(h: Harness) -> None:
    _seed_case(h)
    sent, page = await _to_roster(h)
    bad = {**sent, "review_id": page["review"]["review_id"], "named": "all"}
    with pytest.raises(ToolError, match="review.named is a list"):
        await h.write(PLAN, review=bad)


async def test_a_roster_record_is_answered_only_after_it_is_named(
    h: Harness,
) -> None:
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    answers = {
        **sent,
        "review_id": page["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "unrelated"},
        "named": [],
    }
    with pytest.raises(ToolError, match="roster ids not named"):
        await h.write(PLAN, review=answers)


async def test_a_record_answered_with_its_name_is_still_shown_in_full(
    h: Harness,
) -> None:
    """Named and answered on the same call: the answer waits for the full
    text, so the call is shown the record before anything is decided."""
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    answers = {
        **sent,
        "review_id": page["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "unrelated"},
        "named": [ruling],
    }
    full = await h.write(PLAN, review=answers)
    assert full["status"] == "review", full
    assert [e["id"] for e in full["review"]["related"]] == [ruling]
    res = await h.write(
        PLAN, review={**answers, "review_id": full["review"]["review_id"]}
    )
    assert res["status"] == "committed", res


async def test_a_refused_write_needs_no_roster_answer(h: Harness) -> None:
    _seed_case(h)
    sent, page = await _to_roster(h)
    res = await h.write(
        PLAN,
        review={**sent, "review_id": page["review"]["review_id"], "secret": 0.9},
    )
    assert res["status"] == "credential_warning", res


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


async def test_a_record_in_force_after_the_roster_gets_a_page_of_its_own(
    h: Harness,
) -> None:
    """A ruling written by another session between the roster and the
    answer is listed on a second roster page, alone."""
    ruling, other, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    late = h.seed(FACTS[10])
    _standing(h, late)
    again = await h.name(page, PLAN, sent, [])
    assert is_roster(again), again
    assert _roster_ids(again) == [late]
    rows = h.events(review.REVIEW_ISSUED)
    assert rows[-1]["parent_review_id"] == rows[0]["review_id"]
    res = await h.name(again, PLAN, sent, [])
    assert res["status"] == "committed", res
    assert {ruling, other} <= set(
        review.find_chain(h.store, rows[0]["review_id"]).roster
    )


async def test_a_pre_answered_standing_write_is_shown_the_roster(
    h: Harness,
) -> None:
    """Answers sent without a review_id decide nothing for a standing
    write until the roster is shown. The call is recorded as pre-answered,
    as any such call is, and the roster page follows it in its chain."""
    ruling, _, _ = _seed_case(h)
    res = await h.pre_answer(PLAN, standing=0.9)
    assert is_roster(res), res
    root, page = h.events(review.REVIEW_ISSUED)
    assert root["pre_answered"] is True and root["parent_review_id"] is None
    assert page["parent_review_id"] == root["review_id"]
    assert page["pre_answered"] is False
    assert ruling in page["roster"]
    done = await h.write(
        PLAN,
        review={
            "review_id": res["review"]["review_id"],
            "relation": {i: "unrelated" for i in h.related_ids(PLAN)},
            "unsafe": 0.0,
            "secret": 0.0,
            "standing": 0.9,
            "named": [],
        },
    )
    assert done["status"] == "committed", done
    (chain,) = review_counters.build_chains(
        h.store.iter_events(), now=datetime.now(timezone.utc)
    )
    assert chain.pre_answered and chain.state == "answered"
    assert chain.calls == 2


async def test_a_record_shown_in_full_stays_decided_whatever_named_says(
    h: Harness,
) -> None:
    """Named, shown in full and answered conflict: leaving it out of a
    later call's `named` keeps the conflict, so the write is refused; a
    second roster page, for a ruling that came into force meanwhile, asks
    only for that one."""
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    conflict = {
        **sent,
        "relation": {**sent["relation"], ruling: "conflict"},
        "named": [ruling],
    }
    late = h.seed(FACTS[10])
    _standing(h, late)
    again = await h.write(
        PLAN, review={**conflict, "review_id": full["review"]["review_id"]}
    )
    assert is_roster(again), again
    assert _roster_ids(again) == [late]
    assert "Keep every answer, review.named included" in again["hint"]
    res = await h.write(
        PLAN,
        review={**conflict, "review_id": again["review"]["review_id"], "named": []},
    )
    assert res["status"] == review.ACTION_STANDING, res
    assert [row["id"] for row in res["standing"]] == [ruling]


async def test_an_answer_left_out_for_a_record_shown_in_full_is_asked_again(
    h: Harness,
) -> None:
    ruling, _, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    res = await h.write(
        PLAN, review={**sent, "review_id": full["review"]["review_id"], "named": []}
    )
    assert res["status"] == "review", res
    assert [e["id"] for e in res["review"]["related"]] == [ruling]


async def test_named_is_not_required_once_the_write_is_not_standing(
    h: Harness,
) -> None:
    _seed_case(h)
    sent, page = await _to_roster(h)
    res = await h.write(
        PLAN,
        review={**sent, "review_id": page["review"]["review_id"], "standing": 0.0},
    )
    assert res["status"] == "committed", res


async def test_a_roster_record_come_into_the_related_set_is_shown_in_full(
    h: Harness,
) -> None:
    """A listed ruling that the related set holds by the next call (a
    nearer record removed meanwhile) is answered only after its full text,
    though the answers never named it."""
    ruling, _, near = _seed_case(h)
    sent, page = await _to_roster(h)
    for record_id in near:
        h.store.tombstone(record_id, "test: the related set changes")
    assert ruling in h.related_ids(PLAN)
    answers = {
        **sent,
        "review_id": page["review"]["review_id"],
        "relation": {ruling: "unrelated"},
        "named": [],
    }
    full = await h.write(PLAN, review=answers)
    assert full["status"] == "review", full
    assert ruling in [e["id"] for e in full["review"]["related"]]
    relation = {e["id"]: "unrelated" for e in full["review"]["related"]}
    res = await h.write(
        PLAN,
        review={
            **answers,
            "review_id": full["review"]["review_id"],
            "relation": {**answers["relation"], **relation},
        },
    )
    assert res["status"] == "committed", res


async def test_an_unanswered_roster_is_not_closed_by_another_write(
    h: Harness,
) -> None:
    """The counters' closing rule reads a call without a review_id as the
    answer to the open chain when it names that chain's related ids. A
    roster page has none, and no such call answers it: an abandoned roster
    stays open, and the other write is a chain of its own. Its size is not
    in the review pages' p50."""
    _seed_case(h)
    _, page = await _to_roster(h)
    other = await h.pre_answer(FACTS[11])
    assert other["status"] == "committed", other
    chains = review_counters.build_chains(
        h.store.iter_events(), now=datetime.now(timezone.utc)
    )
    roster_chain = next(
        c for c in chains if page["review"]["review_id"] in c.review_ids
    )
    assert roster_chain.decided is None and roster_chain.state == "pending"
    assert not any(c.absorbed for c in chains)
    report = review_counters.compute_review_report(
        list(h.store.iter_events()),
        start=None,
        end=None,
        now=datetime.now(timezone.utc),
    )
    sizes = [
        row["response_chars"]
        for row in h.events(review.REVIEW_ISSUED)
        if not row["pre_answered"] and "roster" not in row
    ]
    assert report.to_dict()["response_chars_n"] == len(sizes)


async def test_the_roster_chain_holds_with_telemetry_off(h: Harness) -> None:
    ruling, _, _ = _seed_case(h)
    h.server = build_server(
        config=h.config,
        store=h.store,
        state=h.state,
        recorder=Recorder(store=h.store, session_id=h.state.session_id, enabled=False),
    )
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    assert full["status"] == "review", full
    rows = h.events(review.REVIEW_ISSUED)
    assert any(ruling in (row.get("roster") or []) for row in rows)
    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "unrelated"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == "committed", res


def test_the_roster_stays_outside_the_sealed_review() -> None:
    """The roster's question and constant are not among the sealed ones:
    the integrity bench reads the related set and the four questions, and
    those are unchanged."""
    assert dict(review.RELATED_CONSTANTS) == {"top": 3, "cap": 5, "text_chars": 600}
    assert set(review.QUESTIONS) == {"relation", "unsafe", "secret", "standing"}
    assert review.ROSTER_STATEMENT not in str(review.questions_payload())
