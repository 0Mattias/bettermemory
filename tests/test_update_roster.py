"""ENG-112, the other doors: an update that makes a stored memory standing
is shown the standing roster before anything changes.

A standing write is shown every standing ruling in force on its review
(tests/test_write_roster.py). Three other doors made a stored memory
standing with no roster: memory_update with a `standing` answer, with
`category="ambient"`, and memory_admin(action="answer_standing"). Here a
memory_update that leaves its memory standing and in force with a
statement no roster has read gets the write's roster page, answered with
`review` = {review_id, named, relation} and decided as a write's answers
are; answer_standing sends such an answer to memory_update.

The case is the write's: a ruling whose subject sits past character 400
of its body, and a plan that breaks it without naming it, stored first as
a plain record and then made standing through each door. Before this
change every door made the plan standing on its first call, the ruling
never shown.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, review_counters, standing
from bettermemory.builder import build_server
from bettermemory.events import Recorder
from bettermemory.handlers.standing_review import UPDATE_DOOR
from bettermemory.models import LinkType, generate_ulid
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer

from ._mcp import call_tool
from ._review_rows import (
    FACTS,
    Harness,
    fix_origin_and_identity,
    is_roster,
    set_tau,
)
from .test_write_roster import NEAR, OTHER_RULING, PLAN, RULING

QUOTE = "the user changed the ruling here"


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


def _seed_case(h: Harness) -> tuple[str, str, str]:
    """The ruling and the other ruling, standing and in force; the near
    records; and the plan, stored as a plain record with no answer."""
    ruling = h.seed(RULING)
    _standing(h, ruling)
    other = h.seed(OTHER_RULING)
    _standing(h, other)
    for text in NEAR:
        h.seed(text)
    plan = h.seed(PLAN)
    return ruling, other, plan


async def _update(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_update", args)


async def _answer_standing(h: Harness, answers: dict[str, float]) -> dict[str, Any]:
    return await call_tool(
        h.server, "memory_admin", {"action": "answer_standing", "standing": answers}
    )


def _roster_ids(page: dict[str, Any]) -> list[str]:
    return [line.split(" ", 1)[0] for line in page["review"]["roster"]]


def _answer_of(h: Harness, record_id: str) -> float | None:
    return standing.answers(h.store, [record_id]).get(record_id)


async def _name(
    h: Harness,
    page: dict[str, Any],
    named: list[str],
    relation: dict[str, str] | None = None,
    **args: Any,
) -> dict[str, Any]:
    """Re-send the update `args` under `page`'s review_id."""
    answers: dict[str, Any] = {
        "review_id": page["review"]["review_id"],
        "named": named,
    }
    if relation is not None:
        answers["relation"] = relation
    return await _update(h, review=answers, **args)


async def _to_full(
    h: Harness, ruling: str, plan: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan answered standing, the ruling named on its roster: return
    the roster page and the page that shows the ruling in full."""
    page = await _update(h, id=plan, standing=0.9)
    assert is_roster(page), page
    full = await _name(h, page, [ruling], id=plan, standing=0.9)
    assert full["status"] == "review" and "related" in full["review"], full
    return page, full


# ---------------------------------------------------------------------------
# The case, through each door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("door", ["answer", "edit", "ambient", "answer_standing"])
async def test_the_case_reaches_its_ruling_through_every_door(
    h: Harness, door: str
) -> None:
    """The plan made standing by an answer, by an edit that answers its new
    body standing, by the ambient category, or by answer_standing (sent on
    to memory_update) is shown the ruling on the roster, and nothing is
    changed until the roster is answered."""
    ruling, other, plan = _seed_case(h)
    if door == "edit":
        plan = h.seed("Handoff notes for the next session, to be filled in.")
        page = await _update(h, id=plan, content=PLAN, standing=0.9)
    elif door == "ambient":
        page = await _update(h, id=plan, category="ambient")
    elif door == "answer_standing":
        sent = await _answer_standing(h, {plan: 0.9})
        assert sent["standing_review"] == [plan], sent
        assert "memory_update(id, standing=" in sent["hint"]
        assert _answer_of(h, plan) is None
        page = await _update(h, id=plan, standing=0.9)
    else:
        page = await _update(h, id=plan, standing=0.9)
    assert is_roster(page), page
    assert page["written"] is False
    assert page["review"]["question"] == review.ROSTER_STATEMENT
    assert set(_roster_ids(page)) == {ruling, other}
    stored = h.store.load_one(plan)
    assert _answer_of(h, plan) is None
    assert stored.category is None or stored.category.value != "ambient"
    if door == "edit":
        assert stored.body.strip() != PLAN


async def test_the_line_shows_where_the_change_meets_the_ruling(h: Harness) -> None:
    ruling, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    (line,) = [x for x in page["review"]["roster"] if x.startswith(ruling + " ")]
    text = line.split(") ", 1)[1]
    assert "billing database" in text
    assert len(text) <= review.ROSTER_TEXT_CHARS + 3


async def test_the_roster_row_is_a_chain_of_the_update_door(h: Harness) -> None:
    ruling, other, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    (row,) = h.events(review.REVIEW_ISSUED)
    assert row["review_id"] == page["review"]["review_id"]
    assert row["parent_review_id"] is None
    assert (row["door"], row["record"]) == (UPDATE_DOOR, plan)
    assert row["related"] == [] and set(row["roster"]) == {ruling, other}
    assert "billing" not in str(row)
    assert page["review"]["review_id"] in page["hint"]
    assert '"named"' in page["hint"]
    chain = review.find_chain(h.store, page["review"]["review_id"])
    assert chain.open and chain.door == UPDATE_DOOR and chain.record == plan


# ---------------------------------------------------------------------------
# Who is not shown the roster
# ---------------------------------------------------------------------------


async def test_an_answer_below_the_threshold_is_shown_no_roster(h: Harness) -> None:
    _, _, plan = _seed_case(h)
    res = await _update(h, id=plan, standing=0.1)
    assert res["status"] == "committed", res
    assert _answer_of(h, plan) == 0.1
    assert h.events(review.REVIEW_ISSUED) == []


async def test_a_standing_memory_answered_again_is_shown_no_roster(
    h: Harness,
) -> None:
    ruling, _, _ = _seed_case(h)
    res = await _update(h, id=ruling, standing=0.95)
    assert res["status"] == "committed", res
    assert _answer_of(h, ruling) == 0.95


async def test_a_memory_out_of_force_is_shown_no_roster(h: Harness) -> None:
    _, _, plan = _seed_case(h)
    h.store.write(
        content=FACTS[3],
        scopes=["tools"],
        origin=h.store.load_one(plan).origin,
        home=h.home,
        links=[{"type": "supersedes", "target_id": plan}],
    )
    res = await _update(h, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert _answer_of(h, plan) == 0.9


async def test_with_no_other_standing_memory_the_update_commits(h: Harness) -> None:
    plan = h.seed(PLAN)
    res = await _update(h, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert _answer_of(h, plan) == 0.9


async def test_with_the_review_off_nothing_is_shown(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review, "TAU_UNSAFE", None)
    _, _, plan = _seed_case(h)
    res = await _update(h, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    sent = await _answer_standing(h, {h.seed(FACTS[4]): 0.9})
    assert "standing_review" not in sent


async def test_answer_standing_records_the_answers_that_make_nothing_standing(
    h: Harness,
) -> None:
    ruling, _, plan = _seed_case(h)
    plain = h.seed(FACTS[5])
    sent = await _answer_standing(h, {plan: 0.9, plain: 0.1, ruling: 0.95})
    assert sent["status"] == "answered", sent
    assert sent["answered"] == 2
    assert sent["standing_review"] == [plan]
    assert (_answer_of(h, plain), _answer_of(h, ruling)) == (0.1, 0.95)
    assert _answer_of(h, plan) is None
    (row,) = h.events("answer_standing")
    assert row["standing_review"] == [plan]
    # The plan still waits and is counted, but the reply that sends it to
    # memory_update does not offer it again.
    assert plan not in [record["id"] for record in sent.get("records", [])]
    later = await _answer_standing(h, {})
    assert plan in [record["id"] for record in later["records"]]
    assert later["waiting"] == sent["waiting"]


# ---------------------------------------------------------------------------
# Naming and deciding
# ---------------------------------------------------------------------------


async def test_naming_nothing_records_the_answer_and_closes_the_chain(
    h: Harness,
) -> None:
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    res = await _name(h, page, [], id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert _answer_of(h, plan) == 0.9
    row = h.events("update")[-1]
    assert row["review_id"] == page["review"]["review_id"]
    assert row["review_answers"] == {"relation": {}, "named": []}
    assert row["review_action"] == review.ACTION_COMMIT
    assert review.find_chain(h.store, page["review"]["review_id"]).decided


async def test_a_named_ruling_is_shown_in_full_and_a_conflict_refuses(
    h: Harness,
) -> None:
    ruling, _, plan = _seed_case(h)
    page, full = await _to_full(h, ruling, plan)
    (entry,) = full["review"]["related"]
    assert entry["id"] == ruling and "dry run first" in entry["text"]
    assert set(full["review"]["questions"]) == {"relation"}
    rows = h.events(review.REVIEW_ISSUED)
    assert rows[-1]["related"] == [ruling]
    assert rows[-1]["parent_review_id"] == page["review"]["review_id"]
    res = await _name(h, full, [ruling], {ruling: "conflict"}, id=plan, standing=0.9)
    assert res["status"] == review.ACTION_STANDING, res
    assert [row["id"] for row in res["standing"]] == [ruling]
    assert _answer_of(h, plan) is None
    row = h.events("update")[-1]
    assert row["status"] == review.ACTION_STANDING
    assert row["review_answers"]["relation"] == {ruling: "conflict"}
    # A refusal changes nothing and leaves the chain open, so the call
    # re-sent with other answers is decided on the pages already shown.
    assert "review_id" not in row
    again = await _name(h, full, [ruling], {ruling: "unrelated"}, id=plan, standing=0.9)
    assert again["status"] == "committed", again


async def test_a_named_ruling_answered_extends_is_linked(h: Harness) -> None:
    ruling, _, plan = _seed_case(h)
    _, full = await _to_full(h, ruling, plan)
    res = await _name(h, full, [ruling], {ruling: "extends"}, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert res["extends"] == [{"id": ruling, "evidence": "review"}]
    links = h.store.load_one(plan).links
    assert [(link.type, link.target_id) for link in links] == [
        (LinkType.EXTENDS, ruling)
    ]
    assert links[0].note == review.REVIEW_LINK_NOTE
    assert _answer_of(h, plan) == 0.9


async def test_a_named_ruling_answered_restates_refuses_nothing(h: Harness) -> None:
    """The memory is in the store whether or not it is standing, so a
    restatement is recorded and not refused as a duplicate."""
    ruling, _, plan = _seed_case(h)
    _, full = await _to_full(h, ruling, plan)
    res = await _name(h, full, [ruling], {ruling: "restates"}, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert _answer_of(h, plan) == 0.9
    assert h.events("update")[-1]["review_restated"] == [ruling]


async def test_replacing_a_ruling_rests_on_the_users_words(h: Harness) -> None:
    ruling, _, plan = _seed_case(h)
    _, full = await _to_full(h, ruling, plan)
    args: dict[str, Any] = {"id": plan, "standing": 0.9}
    res = await _name(h, full, [ruling], {ruling: "update"}, **args)
    assert res["status"] == review.ACTION_STANDING, res
    assert "said=" in res["hint"]
    done = await _name(h, full, [ruling], {ruling: "update"}, said=QUOTE, **args)
    assert done["status"] == "committed", done
    # No hook saw the checkout: the words are taken as given, and said so.
    assert done["said_checked"] is False
    # Both statements are claim-sized: the update answer links.
    assert done["supersedes"] == [{"id": ruling, "evidence": "review"}]
    links = h.store.load_one(plan).links
    assert [(link.type, link.target_id) for link in links] == [
        (LinkType.SUPERSEDES, ruling)
    ]
    assert standing.guarded_ids(h.store, [ruling]) == []
    guard = h.events("standing_guard")[-1]
    assert (guard["door"], guard["standing"]) == (UPDATE_DOOR, [ruling])


async def test_an_update_answer_between_long_statements_is_named_not_linked(
    h: Harness,
) -> None:
    _, other, _ = _seed_case(h)
    note = h.seed("Release notes style is still being decided.")
    args: dict[str, Any] = {
        "id": note,
        "content": (
            "Release notes are written in the present tense. They open with "
            "the change. They name the ticket. They credit the author. They "
            "link the pull request. They close with the upgrade steps."
        ),
        "standing": 0.9,
    }
    page = await _update(h, **args)
    assert other in _roster_ids(page)
    full = await _name(h, page, [other], **args)
    done = await _name(h, full, [other], {other: "update"}, said=QUOTE, **args)
    assert done["status"] == "committed", done
    assert done["update_suggested"] == [other]
    assert h.store.load_one(note).links == []


async def test_an_edit_of_a_standing_memory_is_shown_the_roster(h: Harness) -> None:
    ruling, other, _ = _seed_case(h)
    args: dict[str, Any] = {
        "id": other,
        "content": PLAN,
        "standing": 0.9,
        "said": QUOTE,
    }
    page = await _update(h, **args)
    assert is_roster(page), page
    assert _roster_ids(page) == [ruling]
    res = await _name(h, page, [], **args)
    assert res["status"] == "committed", res
    assert h.store.load_one(other).body.strip() == PLAN


# ---------------------------------------------------------------------------
# The chain's rules
# ---------------------------------------------------------------------------


async def test_named_is_required_after_a_roster(h: Harness) -> None:
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    with pytest.raises(ToolError, match="review.named is missing"):
        await _update(
            h, id=plan, standing=0.9, review={"review_id": page["review"]["review_id"]}
        )


async def test_named_holds_only_roster_ids(h: Harness) -> None:
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    near = [m.id for m in h.store.load_all() if m.body.strip() in NEAR]
    with pytest.raises(ToolError, match="the roster did not list"):
        await _name(h, page, [near[0]], id=plan, standing=0.9)


async def test_a_roster_record_is_answered_only_after_it_is_named(
    h: Harness,
) -> None:
    ruling, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    with pytest.raises(ToolError, match="roster ids not named"):
        await _name(h, page, [], {ruling: "unrelated"}, id=plan, standing=0.9)


async def test_the_update_review_takes_only_its_own_keys(h: Harness) -> None:
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    with pytest.raises(ToolError, match="keys an update does not read"):
        await _update(
            h,
            id=plan,
            standing=0.9,
            review={"review_id": page["review"]["review_id"], "named": [], "unsafe": 0},
        )


async def test_a_record_shown_in_full_stays_decided_whatever_named_says(
    h: Harness,
) -> None:
    ruling, _, plan = _seed_case(h)
    _, full = await _to_full(h, ruling, plan)
    res = await _name(h, full, [], {ruling: "conflict"}, id=plan, standing=0.9)
    assert res["status"] == review.ACTION_STANDING, res


async def test_a_record_in_force_after_the_roster_gets_a_page_of_its_own(
    h: Harness,
) -> None:
    ruling, other, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    late = h.seed(FACTS[10])
    _standing(h, late)
    again = await _name(h, page, [], id=plan, standing=0.9)
    assert is_roster(again), again
    assert _roster_ids(again) == [late]
    assert "came into force since" in again["hint"]
    res = await _name(h, again, [], id=plan, standing=0.9)
    assert res["status"] == "committed", res
    chain = review.find_chain(h.store, page["review"]["review_id"])
    assert {ruling, other, late} <= chain.roster


async def test_a_chain_answers_only_an_update_of_its_own_memory(h: Harness) -> None:
    """Another memory's roster, or a write's review, does not answer this
    update: it is shown a roster of its own."""
    _, _, plan = _seed_case(h)
    second = h.seed(FACTS[6])
    page = await _update(h, id=plan, standing=0.9)
    other = await _name(h, page, [], id=second, standing=0.9)
    assert is_roster(other), other
    assert other["review"]["review_id"] != page["review"]["review_id"]
    assert h.events(review.REVIEW_ISSUED)[-1]["record"] == second
    first = await h.write(FACTS[7])
    written = await _update(
        h,
        id=plan,
        standing=0.9,
        review={"review_id": first["review"]["review_id"], "named": []},
    )
    assert is_roster(written), written
    assert _answer_of(h, plan) is None


async def test_a_write_does_not_take_an_update_chain(h: Harness) -> None:
    """A write that carries an update roster's review_id is handled as one
    that carries none it can use: pre-answered, the sent id kept."""
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    res = await h.write(
        FACTS[8],
        review={
            "review_id": page["review"]["review_id"],
            "relation": {i: "unrelated" for i in h.related_ids(FACTS[8])},
            "unsafe": 0.0,
            "secret": 0.0,
            "standing": 0.0,
        },
    )
    assert res["status"] == "committed", res
    row = h.events(review.REVIEW_ISSUED)[-1]
    assert row["pre_answered"] is True
    assert row["sent_review_id"] == page["review"]["review_id"]
    assert review.find_chain(h.store, page["review"]["review_id"]).open


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


async def test_the_write_reviews_counters_leave_the_update_rows_out(
    h: Harness,
) -> None:
    _, _, plan = _seed_case(h)
    page = await _update(h, id=plan, standing=0.9)
    await _name(h, page, [], id=plan, standing=0.9)
    await h.reviewed_commit(FACTS[9])
    chains = review_counters.build_chains(
        h.store.iter_events(), now=datetime.now(timezone.utc)
    )
    assert len(chains) == 1
    assert page["review"]["review_id"] not in chains[0].review_ids
    report = review_counters.compute_review_report(
        list(h.store.iter_events()),
        start=None,
        end=None,
        now=datetime.now(timezone.utc),
    )
    assert report.to_dict()["response_chars_n"] == 1


async def test_the_update_chain_holds_with_telemetry_off(h: Harness) -> None:
    ruling, _, plan = _seed_case(h)
    h.server = build_server(
        config=h.config,
        store=h.store,
        state=h.state,
        recorder=Recorder(store=h.store, session_id=h.state.session_id, enabled=False),
    )
    _, full = await _to_full(h, ruling, plan)
    res = await _name(h, full, [ruling], {ruling: "unrelated"}, id=plan, standing=0.9)
    assert res["status"] == "committed", res
    rows = h.events(review.REVIEW_ISSUED)
    assert all(row["door"] == UPDATE_DOOR and row["record"] == plan for row in rows)
    assert h.events("update")[-1]["review_id"] == full["review"]["review_id"]
    assert review.find_chain(h.store, full["review"]["review_id"]).decided


# ---------------------------------------------------------------------------
# The guard's row, the reply and the update row
# ---------------------------------------------------------------------------


async def test_the_guard_row_is_written_once_the_edit_commits(h: Harness) -> None:
    """An edit of a standing memory rests on the user's words; the row is
    written for the change that commits, not for the question, the roster
    pages or the refusal before it."""
    ruling, other, _ = _seed_case(h)
    args: dict[str, Any] = {"id": other, "content": PLAN, "said": QUOTE}
    asked = await _update(h, **args)
    assert set(asked["questions"]) == {"standing"}, asked
    args["standing"] = 0.9
    page = await _update(h, **args)
    assert is_roster(page), page
    full = await _name(h, page, [ruling], **args)
    refused = await _name(h, full, [ruling], {ruling: "conflict"}, **args)
    assert refused["status"] == review.ACTION_STANDING, refused
    with pytest.raises(ToolError):
        await _update(h, review={"unsafe": 0}, **args)
    assert h.events("standing_guard") == []
    assert h.store.load_one(other).body.strip() == OTHER_RULING
    done = await _name(h, full, [ruling], {ruling: "unrelated"}, **args)
    assert done["status"] == "committed", done
    assert done["said_checked"] is False
    (guard,) = h.events("standing_guard")
    assert (guard["door"], guard["standing"]) == (UPDATE_DOOR, [other])


async def test_an_answer_leaving_a_ruling_not_standing_says_unchecked_words(
    h: Harness,
) -> None:
    ruling, _, _ = _seed_case(h)
    res = await _update(h, id=ruling, standing=0.1, said=QUOTE)
    assert res["status"] == "committed", res
    assert res["said_checked"] is False
    (guard,) = h.events("standing_guard")
    assert (guard["door"], guard["standing"]) == (UPDATE_DOOR, [ruling])
    plain = await _update(h, id=h.seed(FACTS[6]), standing=0.1)
    assert "said_checked" not in plain


async def test_the_update_row_names_the_links_the_roster_set(h: Harness) -> None:
    ruling, _, plan = _seed_case(h)
    args: dict[str, Any] = {"id": plan, "category": "ambient"}
    page = await _update(h, **args)
    full = await _name(h, page, [ruling], **args)
    done = await _name(h, full, [ruling], {ruling: "extends"}, **args)
    assert done["status"] == "committed", done
    assert [link.target_id for link in h.store.load_one(plan).links] == [ruling]
    assert h.events("update")[-1]["fields"] == ["category", "links"]


async def test_roster_links_past_the_cap_write_nothing(h: Harness) -> None:
    """A record over its link cap is written and then fails every read, so
    roster answers that would take the memory past it raise first."""
    ruling, _, plan = _seed_case(h)
    held = [{"type": "depends_on", "target_id": generate_ulid()} for _ in range(64)]
    assert (await _update(h, id=plan, links=held))["status"] == "committed"
    _, full = await _to_full(h, ruling, plan)
    with pytest.raises(ToolError, match="capped at 64"):
        await _name(h, full, [ruling], {ruling: "extends"}, id=plan, standing=0.9)
    assert len(h.store.load_one(plan).links) == 64
    assert _answer_of(h, plan) is None
    assert review.find_chain(h.store, full["review"]["review_id"]).open


async def test_the_stop_counts_an_update_chain_left_open(h: Harness) -> None:
    from bettermemory.recall_settle import review_unanswered
    from bettermemory.recall_state import RecallState

    _, _, plan = _seed_case(h)
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    page = await _update(h, id=plan, standing=0.9)
    row = h.events(review.REVIEW_ISSUED)[-1]
    session = str(row.get("session") or row.get("session_id"))
    roots = review_unanswered(
        store=h.store,
        recorder=Recorder(store=h.store, session_id="stop"),
        state=RecallState(),
        session_id="stop",
        sessions={session},
        since=since,
    )
    assert roots == [page["review"]["review_id"]]
