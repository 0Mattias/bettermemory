"""The update protocol: every reply that stops an update carries all the
change still owes, so the call that pays it commits.

A body edit that sends `standing` and owes nothing else commits in one
call. One that does not send it is asked for it, and the same reply
carries the roster when the record is standing. A gate refusal and the
guard's refusal carry the standing question and the roster too, so the
retry with the flag, the words and the answers commits. Rule S holds: a
standing record is edited only with `said`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, standing
from bettermemory.handlers.update import DESC_MEMORY_UPDATE
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer

from ._mcp import call_tool
from ._review_rows import Harness, fix_origin_and_identity, set_tau
from .test_write_roster import OTHER_RULING, RULING

QUOTE = "the user changed the ruling here"
EDITED_RULING = RULING + " It also holds for the staging databases."
TRANSIENT = "Release notes are currently written in the past tense."


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


def _stand(h: Harness, record_id: str) -> None:
    h.store.put_standing(record_id, StandingAnswer(0.9, "asked", "test/test"))


def _rulings(h: Harness) -> tuple[str, str]:
    """Two standing rulings in force."""
    ruling = h.seed(RULING)
    _stand(h, ruling)
    other = h.seed(OTHER_RULING)
    _stand(h, other)
    return ruling, other


async def _update(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_update", args)


def _roster_ids(page: dict[str, Any]) -> set[str]:
    return {line.split(" ", 1)[0] for line in page["review"]["roster"]}


def _named(reply: dict[str, Any], named: list[str]) -> dict[str, Any]:
    return {"review_id": reply["review"]["review_id"], "named": named}


def test_the_description_pairs_standing_with_content() -> None:
    assert "`content` needs `standing`" in DESC_MEMORY_UPDATE
    assert "`links` replaces all edges" in DESC_MEMORY_UPDATE
    for edge in ("supersedes", "contradicts", "extends", "depends_on"):
        assert edge in DESC_MEMORY_UPDATE


async def test_an_edit_with_its_answer_commits_in_one_call(h: Harness) -> None:
    record = h.seed("The build cache lives under the target directory.")
    res = await _update(
        h,
        id=record,
        content="The build cache lives under the out directory.",
        standing=0.1,
    )
    assert res["status"] == "committed", res


async def test_an_edit_without_its_answer_is_asked_once(h: Harness) -> None:
    record = h.seed("The build cache lives under the target directory.")
    args = {"id": record, "content": "The build cache lives under out."}
    asked = await _update(h, **args)
    assert asked["status"] == "review" and asked["written"] is False
    assert asked["questions"]["standing"]["instructions"] == (review.STANDING_STATEMENT)
    assert "review" not in asked
    res = await _update(h, **args, standing=0.1)
    assert res["status"] == "committed", res


async def test_an_edit_of_a_standing_record_asks_everything_at_once(
    h: Harness,
) -> None:
    """The guard's refusal names the record and asks for `said`; the same
    reply asks the standing question and shows the roster, so the retry
    commits."""
    ruling, other = _rulings(h)
    args = {"id": ruling, "content": EDITED_RULING}
    first = await _update(h, **args)
    assert first["status"] == "refused_standing", first
    assert [row["id"] for row in first["standing"]] == [ruling]
    assert "said=" in first["hint"]
    assert first["questions"]["standing"]["instructions"] == (review.STANDING_STATEMENT)
    assert "roster" in first["review"]
    assert _roster_ids(first) == {other}
    assert first["review"]["question"] == review.ROSTER_STATEMENT
    assert first["review"]["review_id"] in first["hint"]
    assert h.store.load_one(ruling).body.strip() == RULING
    res = await _update(h, **args, said=QUOTE, standing=0.9, review=_named(first, []))
    assert res["status"] == "committed", res
    assert h.store.load_one(ruling).body.strip() == EDITED_RULING
    guard_rows = h.events("standing_guard")
    assert [row["said"] for row in guard_rows] == [QUOTE]


async def test_an_edit_answered_off_standing_ignores_the_roster(
    h: Harness,
) -> None:
    """The roster was read on the record's current answer; an answer that
    leaves it not standing needs no review, and the words still carry it."""
    ruling, _ = _rulings(h)
    args = {"id": ruling, "content": EDITED_RULING}
    first = await _update(h, **args)
    res = await _update(h, **args, said=QUOTE, standing=0.1, review=_named(first, []))
    assert res["status"] == "committed", res
    assert standing.answers(h.store, [ruling])[ruling] == 0.1


async def test_the_refusal_without_words_is_refused_again(h: Harness) -> None:
    ruling, _ = _rulings(h)
    args = {"id": ruling, "content": EDITED_RULING}
    first = await _update(h, **args)
    again = await _update(h, **args, standing=0.9, review=_named(first, []))
    assert again["status"] == "refused_standing", again
    assert h.store.load_one(ruling).body.strip() == RULING


async def test_a_gate_refusal_carries_what_the_retry_needs(h: Harness) -> None:
    ruling, other = _rulings(h)
    args = {"id": other, "content": TRANSIENT}
    first = await _update(h, **args)
    assert first["status"] == "transient_warning", first
    assert "acknowledge_transient=True" in first["hint"]
    assert [row["id"] for row in first["standing"]] == [other]
    assert "standing" in first["questions"]
    assert _roster_ids(first) == {ruling}
    res = await _update(
        h,
        **args,
        acknowledge_transient=True,
        said=QUOTE,
        standing=0.9,
        review=_named(first, []),
    )
    assert res["status"] == "committed", res


async def test_a_gate_refusal_on_a_plain_record_asks_the_answer_too(
    h: Harness,
) -> None:
    record = h.seed("Release notes are written by the release lead.")
    args = {"id": record, "content": TRANSIENT}
    first = await _update(h, **args)
    assert first["status"] == "transient_warning", first
    assert "standing" in first["questions"]
    assert "standing=<0 to 1>" in first["hint"]
    assert "review" not in first
    res = await _update(h, **args, acknowledge_transient=True, standing=0.1)
    assert res["status"] == "committed", res


async def test_a_gate_refusal_with_the_answer_asks_nothing_more(
    h: Harness,
) -> None:
    record = h.seed("Release notes are written by the release lead.")
    first = await _update(h, id=record, content=TRANSIENT, standing=0.1)
    assert first["status"] == "transient_warning", first
    assert "questions" not in first
    assert "standing=" not in first["hint"]


@pytest.mark.parametrize(
    ("link", "key"),
    [
        ({"type": "supersedes", "target": "x"}, "'target'"),
        ({"type": "supersedes"}, "'target_id'"),
        ({"type": "replaces", "target_id": "x"}, "'type'"),
    ],
)
async def test_a_link_error_names_the_format_and_the_key(
    h: Harness, link: dict[str, Any], key: str
) -> None:
    record = h.seed("The build cache lives under the target directory.")
    with pytest.raises(ToolError) as raised:
        await _update(h, id=record, links=[link])
    message = str(raised.value)
    assert key in message
    assert '"target_id": "<memory id>"' in message
    assert "depends_on" in message


async def test_a_reply_leaves_one_row_naming_all_it_owed(h: Harness) -> None:
    ruling, _ = _rulings(h)
    await _update(h, id=ruling, content=EDITED_RULING)
    (row,) = h.events("update")
    assert row["status"] == "refused_standing"
    assert row["owed"] == ["refused_standing", "standing_question", "standing_roster"]


async def test_a_reply_owing_one_thing_leaves_one_plain_row(h: Harness) -> None:
    record = h.seed("The build cache lives under the target directory.")
    await _update(h, id=record, content="The build cache lives under out.")
    (row,) = h.events("update")
    assert row["status"] == "standing_question"
    assert "owed" not in row


@pytest.mark.parametrize("change", ["content", "scopes", "links"])
async def test_a_retry_that_changes_the_edit_is_answered_afresh(
    h: Harness, change: str
) -> None:
    """The roster's answers hold only for the change they were given for."""
    ruling, other = _rulings(h)
    args: dict[str, Any] = {"id": ruling, "content": EDITED_RULING}
    first = await _update(h, **args)
    if change == "content":
        args["content"] = EDITED_RULING + " And for the archive databases."
    elif change == "scopes":
        args["scopes"] = ["tools", "billing"]
    else:
        args["links"] = [{"type": "extends", "target_id": other}]
    again = await _update(h, **args, said=QUOTE, standing=0.9, review=_named(first, []))
    assert again["status"] == "review", again
    assert "roster" in again["review"]
    assert again["review"]["review_id"] != first["review"]["review_id"]
    assert h.store.load_one(ruling).body.strip() == RULING
    res = await _update(h, **args, said=QUOTE, standing=0.9, review=_named(again, []))
    assert res["status"] == "committed", res


async def test_tool_usage_counts_one_call_per_reply(h: Harness) -> None:
    """Readers that count calls from `update` rows see one per reply."""
    from bettermemory.eval import compute_tool_usage

    ruling, _ = _rulings(h)
    first = await _update(h, id=ruling, content=EDITED_RULING)
    await _update(
        h,
        id=ruling,
        content=EDITED_RULING,
        said=QUOTE,
        standing=0.9,
        review=_named(first, []),
    )
    usage = compute_tool_usage(list(h.store.iter_events())).to_dict()
    (row,) = [r for r in usage["rows"] if r["tool"] == "memory_update"]
    assert row["count"] == 2
