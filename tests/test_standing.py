"""Unit RS-F7 part 1: standing records and their answers.

A record is standing when it is ambient or its answer to the standing
question is at or above `standing.TAU_STANDING`, and it is in force. The
answer is asked of the agent: by the write review's fourth question, by
memory_update on an edit of the body, and for a record with no answer for
its current body in memory_show's response and through
memory_admin(action="answer_standing"), which the session's first output
names while any such record is admitted for the caller. An answer is a
``standing`` row naming the body it was given for, folded from the log
like a home.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, standing
from bettermemory.admission import Admission
from bettermemory.location import resolve_caller
from bettermemory.membership import REVIEW_STRATA, SEARCH_AUTO_SCOPE
from bettermemory.models import Category, generate_ulid
from bettermemory.recall import RecallState, session_brief
from bettermemory.store import MUTATION_KINDS, StandingAnswer, body_sha256

from ._mcp import call_tool
from ._review_rows import FACTS, ORIGIN, Harness, fix_origin_and_identity, set_tau


@pytest.fixture(autouse=True)
def _setup(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _rows(h: Harness) -> dict[str, dict[str, Any]]:
    return {r["record_id"]: r for r in h.store.membership_rows("standing")}


def _admission(h: Harness, surface: str) -> Admission:
    return Admission.of(surface, resolve_caller(h.store, h.config, os.getcwd()))


async def _update(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_update", args)


async def _answer_standing(h: Harness, answers: dict[str, Any] | None = None) -> Any:
    args: dict[str, Any] = {"action": "answer_standing"}
    if answers is not None:
        args["standing"] = answers
    return await call_tool(h.server, "memory_admin", args)


def _answer(h: Harness, record_id: str, probability: float) -> None:
    h.store.put_standing(record_id, StandingAnswer(probability, "asked", "test/test"))


# ---------------------------------------------------------------------------
# The write review's fourth question
# ---------------------------------------------------------------------------


async def test_the_review_asks_standing_as_its_fourth_question(h: Harness) -> None:
    first = await h.write(FACTS[0])
    assert first["status"] == "review"
    assert first["review"]["questions"]["standing"] == {
        "type": "probability",
        "instructions": review.STANDING_STATEMENT,
    }
    assert '"standing": <0 to 1>' in first["hint"]


async def test_a_reviewed_write_stores_its_answer_with_the_record(h: Harness) -> None:
    h.as_host("claude-code", "claude-opus-5-5")
    res = await h.reviewed_commit(FACTS[0], standing=0.8)
    row = _rows(h)[res["id"]]
    assert row["probability"] == 0.8
    assert row["source"] == standing.SOURCE_REVIEW
    assert row["answered_by"] == "claude-code/claude-opus-5-5"
    assert row["body_sha256"] == body_sha256(h.store.get_memory(res["id"]).body)
    (event,) = [e for e in h.events("write") if e.get("status") == "committed"]
    assert event["review_answers"]["standing"] == 0.8
    # The answer is written in the record's own transaction: the
    # standing_put row follows the memory_put row with nothing between.
    kinds = [r.kind for r in h.store.log_rows()]
    put = max(i for i, k in enumerate(kinds) if k == "memory_put")
    assert "standing_put" in kinds[put + 1 : put + 3]


async def test_answers_without_standing_are_a_teaching_error(h: Harness) -> None:
    first = await h.write(FACTS[0])
    with pytest.raises(ToolError, match="review.standing is missing"):
        await h.write(
            FACTS[0],
            review={
                "review_id": first["review"]["review_id"],
                "relation": {},
                "unsafe": 0,
                "secret": 0,
            },
        )
    assert h.store.load_all() == []
    assert _rows(h) == {}


@pytest.mark.parametrize("value", [-0.1, 1.5, "high", True, None])
async def test_a_standing_answer_outside_0_to_1_is_a_teaching_error(
    h: Harness, value: Any
) -> None:
    first = await h.write(FACTS[0])
    with pytest.raises(ToolError, match="review.standing"):
        await h.write(
            FACTS[0],
            review={
                "review_id": first["review"]["review_id"],
                "relation": {},
                "unsafe": 0,
                "secret": 0,
                "standing": value,
            },
        )
    assert h.store.load_all() == []


async def test_a_refused_write_stores_no_answer(h: Harness) -> None:
    first = await h.write(FACTS[0])
    refused = await h.answer(first, FACTS[0], secret=0.9, standing=0.9)
    assert refused["status"] == "credential_warning"
    assert _rows(h) == {}


# ---------------------------------------------------------------------------
# memory_update
# ---------------------------------------------------------------------------


async def test_an_edit_of_the_body_is_asked_before_it_applies(h: Harness) -> None:
    record = h.seed(FACTS[0])
    asked = await _update(h, id=record, content=FACTS[1])
    assert asked["status"] == "review" and asked["written"] is False
    assert asked["questions"]["standing"]["instructions"] == review.STANDING_STATEMENT
    assert "standing=<0 to 1>" in asked["hint"]
    assert h.store.get_memory(record).body.strip() == FACTS[0]

    done = await _update(h, id=record, content=FACTS[1], standing=0.6)
    assert done["status"] == "committed"
    row = _rows(h)[record]
    assert (row["probability"], row["source"]) == (0.6, standing.SOURCE_UPDATE)
    assert row["body_sha256"] == body_sha256(h.store.get_memory(record).body)
    (event,) = [e for e in h.events("update") if e.get("fields")]
    assert event["standing"] == 0.6 and "standing" in event["fields"]


async def test_an_answer_alone_is_for_the_current_body_and_edits_nothing(
    h: Harness,
) -> None:
    record = h.seed(FACTS[0])
    before = h.store.get_memory(record)
    res = await _update(h, id=record, standing=0.3)
    assert res["status"] == "committed"
    assert h.store.get_memory(record) == before
    assert standing.answers(h.store, [record]) == {record: 0.3}


async def test_a_metadata_edit_keeps_the_answer(h: Harness) -> None:
    res = await h.reviewed_commit(FACTS[0], standing=0.7)
    edited = await _update(h, id=res["id"], scopes=["tools", "ops"])
    assert edited["status"] == "committed"
    assert standing.answers(h.store, [res["id"]]) == {res["id"]: 0.7}


async def test_an_edit_that_does_not_ask_leaves_the_record_unanswered(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit made with the review off, or by a build that does not ask,
    leaves the old row naming the old body: it is no answer, and the
    record waits again."""
    first = await h.reviewed_commit(FACTS[0], standing=0.7)
    second = await h.reviewed_commit(FACTS[1], standing=0.7)
    monkeypatch.setattr(review, "TAU_UNSAFE", None)
    monkeypatch.setattr(review, "TAU_SECRET", None)
    res = await _update(h, id=first["id"], content=FACTS[2])
    assert res["status"] == "committed"
    memory = h.store.get_memory(second["id"])
    h.store.update(memory.model_copy(update={"body": FACTS[3] + "\n"}))
    assert standing.answers(h.store, [first["id"], second["id"]]) == {
        first["id"]: None,
        second["id"]: None,
    }
    waiting = standing.backlog(h.store, _admission(h, SEARCH_AUTO_SCOPE))
    assert set(waiting.ids) == {first["id"], second["id"]}


# ---------------------------------------------------------------------------
# The legacy path: memory_show and answer_standing
# ---------------------------------------------------------------------------


async def test_memory_show_asks_a_record_with_no_answer(h: Harness) -> None:
    record = h.seed(FACTS[0])
    shown = await call_tool(h.server, "memory_show", {"id": record})
    question = shown["standing_question"]
    assert question["instructions"] == review.STANDING_STATEMENT
    assert f'standing={{"{record}": <0 to 1>}}' in question["hint"]
    assert 'action="answer_standing"' in question["hint"]

    await _answer_standing(h, {record: 0.2})
    again = await call_tool(h.server, "memory_show", {"id": record})
    assert "standing_question" not in again


async def test_answer_standing_shows_the_newest_waiting_records_in_batches(
    h: Harness,
) -> None:
    ids = [h.seed(fact) for fact in FACTS[:7]]
    first = await _answer_standing(h)
    assert first["status"] == "waiting"
    assert (first["answered"], first["waiting"]) == (0, 7)
    assert first["question"]["instructions"] == review.STANDING_STATEMENT
    shown = [r["id"] for r in first["records"]]
    assert shown == list(reversed(ids))[: standing.BATCH]
    assert first["records"][0]["text"] == FACTS[6]
    assert "standing={<id>: <0 to 1>, ...}" in first["hint"]

    second = await _answer_standing(h, {i: 0.1 for i in shown})
    assert second["status"] == "answered"
    assert (second["answered"], second["waiting"]) == (standing.BATCH, 2)
    assert [r["id"] for r in second["records"]] == list(reversed(ids[:2]))
    assert all(_rows(h)[i]["source"] == standing.SOURCE_ASKED for i in shown)

    last = await _answer_standing(h, {i: 0.9 for i in ids[:2]})
    assert last["waiting"] == 0
    assert "records" not in last and "question" not in last
    assert last["hint"] == standing.batch_hint(0)


def test_a_long_record_is_shown_by_its_head_as_a_review_shows_one() -> None:
    body = "First sentence names it. " + "More words follow here. " * 60
    text = standing.record_text(body)
    assert len(text) <= review.RELATED_TEXT_CHARS
    assert text.startswith("First sentence names it.")


@pytest.mark.parametrize(
    "bad",
    [
        {"__unknown__": 0.5},
        {"__first__": 1.5},
        {"__first__": True},
        {"__first__": "0.5"},
    ],
)
async def test_answer_standing_checks_every_answer_before_writing_any(
    h: Harness, bad: dict[str, Any]
) -> None:
    first, second = h.seed(FACTS[0]), h.seed(FACTS[1])
    names = {"__unknown__": generate_ulid(), "__first__": first}
    answers = {second: 0.5, **{names[k]: v for k, v in bad.items()}}
    with pytest.raises(ToolError, match="standing"):
        await _answer_standing(h, answers)
    assert _rows(h) == {}


async def test_answer_standing_counts_what_the_callers_search_reads(
    h: Harness,
) -> None:
    """A record with no home is admitted nowhere, so no session here is
    asked about it; memory_show still asks, since it opens any record."""
    homed = h.seed(FACTS[0])
    unhomed = h.store.write(content=FACTS[1], scopes=["tools"], origin=ORIGIN).id
    listed = await _answer_standing(h)
    assert [r["id"] for r in listed["records"]] == [homed]
    shown = await call_tool(h.server, "memory_show", {"id": unhomed})
    assert "standing_question" in shown
    # Any active record takes an answer.
    await _answer_standing(h, {unhomed: 0.4})
    assert standing.answers(h.store, [unhomed]) == {unhomed: 0.4}


# ---------------------------------------------------------------------------
# The session's first output
# ---------------------------------------------------------------------------


def _first_output(h: Harness, session_id: str) -> str:
    result = session_brief(
        session_id=session_id,
        cwd=Path.cwd(),
        config=h.config,
        store=h.store,
        state=RecallState(),
    )
    return result.stdout


def test_the_first_output_names_the_waiting_records(h: Harness) -> None:
    ids = [h.seed(fact) for fact in FACTS[:3]]
    line = standing.backlog_line(3)
    assert line.startswith("bettermemory: 3 memories have no standing answer.")
    assert 'memory_admin(action="answer_standing")' in line
    assert line in _first_output(h, "s-1")
    for record in ids:
        _answer(h, record, 0.5)
    assert "standing answer" not in _first_output(h, "s-2")
    assert standing.backlog_line(1).startswith("bettermemory: 1 memory has")
    assert standing.backlog_line(0) == ""


async def test_nothing_is_asked_while_the_review_is_off(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question goes with the write review's switch: with both
    thresholds unset, no response and no first output asks it, and an
    answer given is still taken."""
    record = h.seed(FACTS[0])
    monkeypatch.setattr(review, "TAU_UNSAFE", None)
    monkeypatch.setattr(review, "TAU_SECRET", None)
    assert not standing.asking()
    shown = await call_tool(h.server, "memory_show", {"id": record})
    assert "standing_question" not in shown
    assert "standing answer" not in _first_output(h, "s-off")
    edited = await _update(h, id=record, content=FACTS[1])
    assert edited["status"] == "committed"
    await _answer_standing(h, {record: 0.3})
    assert standing.answers(h.store, [record]) == {record: 0.3}


# ---------------------------------------------------------------------------
# What is standing
# ---------------------------------------------------------------------------


def test_standing_is_ambient_or_at_tau_and_in_force(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = h.store.write(
        content=FACTS[0],
        scopes=["tools"],
        category=Category.AMBIENT,
        origin=ORIGIN,
        home=h.home,
    ).id
    ruled, plain, edge = h.seed(FACTS[1]), h.seed(FACTS[2]), h.seed(FACTS[3])
    _answer(h, ruled, 0.9)
    _answer(h, plain, 0.2)
    _answer(h, edge, 0.5)
    admission = _admission(h, REVIEW_STRATA)

    # With no threshold, nothing but ambient.
    monkeypatch.setattr(standing, "TAU_STANDING", None)
    assert standing.standing_ids(h.store, admission) == [ambient]

    monkeypatch.setattr(standing, "TAU_STANDING", 0.5)
    assert set(standing.standing_ids(h.store, admission)) == {ambient, ruled, edge}

    # A superseded ruling is history; its head stands only on its own answer.
    head = h.store.write(
        content=FACTS[4],
        scopes=["tools"],
        origin=ORIGIN,
        home=h.home,
        links=[{"type": "supersedes", "target_id": ruled}],
    ).id
    assert set(standing.standing_ids(h.store, admission)) == {ambient, edge}
    _answer(h, head, 0.95)
    assert set(standing.standing_ids(h.store, admission)) == {ambient, edge, head}


def test_is_standing_reads_the_threshold_inclusively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert standing.TAU_STANDING == 0.3
    assert standing.is_standing("fact", 0.3)
    assert not standing.is_standing("fact", 0.29)
    monkeypatch.setattr(standing, "TAU_STANDING", None)
    assert standing.is_standing("ambient", None)
    assert not standing.is_standing("fact", 1.0)
    monkeypatch.setattr(standing, "TAU_STANDING", 0.4)
    assert standing.is_standing("fact", 0.4)
    assert not standing.is_standing("fact", 0.39)
    assert not standing.is_standing("fact", None)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


async def test_the_standing_table_is_folded_from_the_log(h: Harness) -> None:
    assert {"standing_put", "standing_delete"} <= MUTATION_KINDS
    res = await h.reviewed_commit(FACTS[0], standing=0.8)
    _answer(h, h.seed(FACTS[1]), 0.1)
    _answer(h, res["id"], 0.2)
    fold = h.store.refold()
    assert fold["status"] == "ok", fold
    assert fold["membership"]["standing"] == {
        "unaccounted": [],
        "missing": [],
        "rows": 2,
    }
    assert h.store.log_verify()["status"] == "ok"


def test_a_row_naming_another_body_is_no_answer(h: Harness) -> None:
    record = h.seed(FACTS[0])
    h.store.put_membership(
        "standing",
        {
            "record_id": record,
            "probability": 0.9,
            "body_sha256": body_sha256("another body\n"),
            "source": standing.SOURCE_ASKED,
            "answered_by": "test/test",
            "answered": "2026-10-23T00:00:00Z",
        },
    )
    assert standing.answers(h.store, [record]) == {record: None}


def test_a_store_without_the_table_reads_no_answer(h: Harness) -> None:
    """A store file that could not take the membership tables (a
    read-only copy an earlier build made) reads as having no answer."""
    record = h.seed(FACTS[0])
    _answer(h, record, 0.9)
    h.store._conn.execute("DROP TABLE standing")
    assert standing.answers(h.store, [record]) == {record: None}


def test_a_put_refuses_an_answer_that_is_not_a_probability(h: Harness) -> None:
    record = h.seed(FACTS[0])
    for value in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValueError, match="probability"):
            _answer(h, record, value)
    with pytest.raises(ValueError, match="source"):
        h.store.put_standing(record, StandingAnswer(0.5, "", "test/test"))
    assert _rows(h) == {}
