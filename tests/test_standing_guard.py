"""Unit RS-F7 part 5, the guard on a standing memory.

A standing memory is the user's rule: through the tools it is replaced,
edited, removed, demoted or taken out of force only with ``said``, the
user's words for the change, whatever the caller is admitted to. The
daemon checks the words as it checks a join's: the quote is in a prompt
the prompt hook saw in the caller's checkout; where no hook saw the
checkout the words are taken as given and the row says so. Nothing about
the words' meaning is read. Every change the words carried is one
``standing_guard`` row naming the door, the memories and the quote.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, standing
from bettermemory.location import resolve_caller
from bettermemory.models import Category, LinkType, MemoryLink
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer

from ._mcp import call_tool
from ._review_rows import FACTS, ORIGIN, Harness, fix_origin_and_identity, set_tau
from .test_membership_security import checkout
from .test_write_standing import CONFLICTING, QUOTE, RULE, UPDATING, _decide

DROP = "forget the ledger app deploy rule, it is gone"


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


def _heard(h: Harness, words: str) -> None:
    """The prompt hook saw `words` in the caller's checkout."""
    checkout_id = resolve_caller(h.store, h.config, os.getcwd()).resolution.checkout
    assert checkout_id is not None
    WORDS.remember(h.store.path, checkout_id, words)


def _ruling(h: Harness, content: str = RULE) -> str:
    record_id = h.seed(content)
    _standing(h, record_id)
    return record_id


async def _update(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_update", args)


async def _remove(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_remove", args)


async def _admin(h: Harness, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, "memory_admin", args)


def _guarded(h: Harness, record_id: str) -> bool:
    return standing.guarded_ids(h.store, [record_id]) == [record_id]


def _refused(res: dict[str, Any], record_id: str) -> None:
    assert res["status"] == review.ACTION_STANDING, res
    assert [row["id"] for row in res["standing"]] == [record_id]
    assert "said=" in res["hint"]


def _rows(h: Harness) -> list[dict[str, Any]]:
    return list(h.events("standing_guard"))


# ---------------------------------------------------------------------------
# memory_update
# ---------------------------------------------------------------------------


async def test_an_edit_of_a_standing_memory_rests_on_the_users_words(
    h: Harness,
) -> None:
    old = _ruling(h)
    _refused(await _update(h, id=old, content=UPDATING, standing=0.9), old)
    assert h.store.load_one(old).body.strip() == RULE
    _heard(h, "Something else entirely was asked in this checkout.")
    with pytest.raises(ToolError, match=r"said is not in any prompt the hook saw"):
        await _update(h, id=old, content=UPDATING, standing=0.9, said=QUOTE)
    _heard(h, f"Please {QUOTE}.")
    done = await _update(h, id=old, content=UPDATING, standing=0.9, said=QUOTE)
    assert done["status"] == "committed", done
    assert h.store.load_one(old).body.strip() == UPDATING
    (row,) = _rows(h)
    assert (row["door"], row["standing"]) == ("memory_update", [old])
    assert (row["said"], row["said_checked"]) == (QUOTE, True)


async def test_the_meaning_of_the_words_is_not_read(h: Harness) -> None:
    """The guard checks that the user typed the quote, not what it meant:
    the agent chose the quote, and the quote is on the log."""
    old = _ruling(h)
    naming = "the ledger app deploy rule"
    _heard(h, f"what does {naming} say?")
    done = await _update(h, id=old, content=UPDATING, standing=0.9, said=naming)
    assert done["status"] == "committed", done
    (row,) = _rows(h)
    assert row["said"] == naming


async def test_a_lower_answer_rests_on_the_users_words(h: Harness) -> None:
    old = _ruling(h)
    _refused(await _update(h, id=old, standing=0.0), old)
    assert _guarded(h, old)
    _heard(h, DROP)
    done = await _update(h, id=old, standing=0.0, said=DROP)
    assert done["status"] == "committed", done
    assert not _guarded(h, old)


async def test_a_higher_answer_needs_no_words(h: Harness) -> None:
    old = _ruling(h)
    done = await _update(h, id=old, standing=0.95)
    assert done["status"] == "committed", done
    assert _rows(h) == []


async def test_an_ambient_memory_keeps_its_category_without_words(
    h: Harness,
) -> None:
    old = h.store.write(
        content=RULE,
        scopes=["tools"],
        origin=ORIGIN,
        home=h.home,
        category=Category.AMBIENT,
    ).id
    _refused(await _update(h, id=old, category="fact"), old)
    assert h.store.load_one(old).category == Category.AMBIENT


async def test_dropping_a_scope_rests_on_words_and_adding_one_does_not(
    h: Harness,
) -> None:
    old = _ruling(h)
    added = await _update(h, id=old, scopes=["tools", "ops"])
    assert added["status"] == "committed", added
    _refused(await _update(h, id=old, scopes=["ops"]), old)
    assert sorted(h.store.load_one(old).scopes) == ["ops", "tools"]


async def test_a_supersedes_link_to_a_standing_memory_rests_on_words(
    h: Harness,
) -> None:
    old = _ruling(h)
    other = h.seed(CONFLICTING)
    link = [{"type": "supersedes", "target_id": old}]
    _refused(await _update(h, id=other, links=link), old)
    assert h.store.load_one(other).links == []
    _heard(h, QUOTE)
    done = await _update(h, id=other, links=link, said=QUOTE)
    assert done["status"] == "committed", done
    assert [(x.type, x.target_id) for x in h.store.load_one(other).links] == [
        (LinkType.SUPERSEDES, old)
    ]
    assert not _guarded(h, old)


async def test_dropping_a_rulings_own_link_needs_no_words(h: Harness) -> None:
    """A ruling that replaced a memory holds it out of force by its link.
    Dropping the link puts the older memory back beside the ruling, which
    stays as it is: nothing of the user's rule changes."""
    old = h.seed(RULE)
    ruling = h.seed(UPDATING)
    h.store.put_memory(
        h.store.load_one(ruling).model_copy(
            update={"links": [MemoryLink(type=LinkType.SUPERSEDES, target_id=old)]}
        )
    )
    _standing(h, ruling)
    done = await _update(h, id=ruling, links=[])
    assert done["status"] == "committed", done
    assert h.store.load_one(ruling).links == []
    assert _guarded(h, ruling)


async def test_an_update_of_a_memory_that_is_not_standing_needs_no_words(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    done = await _update(h, id=old, content=UPDATING, standing=0.1)
    assert done["status"] == "committed", done
    assert _rows(h) == []


async def test_words_no_hook_could_check_are_taken_as_given(h: Harness) -> None:
    """No hook saw a prompt in this checkout, so the quote cannot be
    checked: the change goes ahead on the words as given, and the row
    says they were not checked."""
    old = _ruling(h)
    done = await _update(h, id=old, standing=0.0, said=DROP)
    assert done["status"] == "committed", done
    assert not _guarded(h, old)
    (row,) = _rows(h)
    assert (row["said"], row["said_checked"]) == (DROP, False)


async def test_with_the_review_off_the_doors_are_open(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is part 5 of the review and follows its switch, as a
    replacing write does."""
    old = _ruling(h)
    monkeypatch.setattr(review, "TAU_UNSAFE", None)
    monkeypatch.setattr(review, "TAU_SECRET", None)
    other = h.seed(CONFLICTING)
    link = [{"type": "supersedes", "target_id": old}]
    linked = await _update(h, id=other, links=link)
    assert linked["status"] == "committed", linked
    done = await _update(h, id=old, standing=0.0)
    assert done["status"] == "committed", done
    assert _rows(h) == []


# ---------------------------------------------------------------------------
# memory_remove and answer_standing
# ---------------------------------------------------------------------------


async def test_removing_a_standing_memory_rests_on_the_users_words(
    h: Harness,
) -> None:
    old = _ruling(h)
    _refused(await _remove(h, id=old, reason="stale"), old)
    assert h.store.has_memory(old)
    _heard(h, "Something else entirely was asked in this checkout.")
    with pytest.raises(ToolError, match=r"said is not in any prompt the hook saw"):
        await _remove(h, id=old, reason="stale", said=DROP)
    assert h.store.has_memory(old)
    _heard(h, DROP)
    assert await _remove(h, id=old, reason="stale", said=DROP) == {"removed": old}
    (row,) = _rows(h)
    assert (row["door"], row["standing"], row["said"]) == ("memory_remove", [old], DROP)


async def test_removing_a_memory_that_is_not_standing_needs_no_words(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    assert await _remove(h, id=old, reason="stale") == {"removed": old}


async def test_an_empty_said_is_a_teaching_error(h: Harness) -> None:
    old = _ruling(h)
    with pytest.raises(ToolError, match=r"said quotes the user's words"):
        await _remove(h, id=old, reason="stale", said="   ")
    assert h.store.has_memory(old)


async def test_answer_standing_lowering_a_standing_memory_rests_on_words(
    h: Harness,
) -> None:
    old = _ruling(h)
    plain = h.seed(FACTS[1])
    refused = await _admin(h, action="answer_standing", standing={old: 0.0, plain: 0.2})
    _refused(refused, old)
    # nothing of the call was written
    assert standing.answers(h.store, [old, plain]) == {old: 0.9, plain: None}
    _heard(h, DROP)
    done = await _admin(
        h, action="answer_standing", standing={old: 0.0, plain: 0.2}, said=DROP
    )
    assert done["answered"] == 2
    assert standing.answers(h.store, [old, plain]) == {old: 0.0, plain: 0.2}
    (row,) = _rows(h)
    assert (row["door"], row["standing"]) == ("answer_standing", [old])


# ---------------------------------------------------------------------------
# restore, and the doors that need no words
# ---------------------------------------------------------------------------


async def test_restoring_a_memory_that_replaced_a_ruling_rests_on_words(
    h: Harness,
) -> None:
    """A removed memory's ``supersedes`` links come back with it and take
    the ruling out of force again."""
    old = _ruling(h)
    other = h.seed(UPDATING)
    h.store.put_memory(
        h.store.load_one(other).model_copy(
            update={"links": [MemoryLink(type=LinkType.SUPERSEDES, target_id=old)]}
        )
    )
    h.store.tombstone(other, reason="test")
    assert _guarded(h, old)
    _refused(await _admin(h, action="restore", id=other), old)
    assert not h.store.has_memory(other)
    _heard(h, QUOTE)
    await _admin(h, action="restore", id=other, said=QUOTE)
    assert h.store.has_memory(other)
    assert not _guarded(h, old)
    (row,) = _rows(h)
    assert (row["door"], row["standing"]) == ("restore", [old])


async def test_renaming_a_scope_a_ruling_carries_needs_no_words(h: Harness) -> None:
    """A rename moves the scope one to one and changes nothing the rule
    says; it is logged as every rename is."""
    old = _ruling(h)
    await _admin(h, action="rename_scope", old_scope="tools", new_scope="ops")
    assert h.store.load_one(old).scopes == ["ops"]
    assert _guarded(h, old)
    assert _rows(h) == []


# ---------------------------------------------------------------------------
# Outside the caller's admission
# ---------------------------------------------------------------------------


async def test_a_standing_memory_in_another_project_is_guarded(
    h: Harness, tmp_path: Path
) -> None:
    """A declared supersedes names a standing memory in a project the
    caller is not admitted to: it rests on the user's words all the same,
    and the refusal names it without quoting its text."""
    elsewhere = checkout(tmp_path / "others" / "bar")
    home = resolve_caller(h.store, h.config, str(elsewhere)).home()
    old = h.store.write(content=RULE, scopes=["tools"], origin=ORIGIN, home=home).id
    _standing(h, old)
    refused = await _decide(h, CONFLICTING, {}, supersedes=[old])
    assert refused["status"] == review.ACTION_STANDING
    (row,) = refused["standing"]
    assert row["id"] == old and row["text"] is None and row["admitted"] is False
    _heard(h, QUOTE)
    done = await _decide(h, CONFLICTING, {}, supersedes=[old], said=QUOTE)
    assert done["status"] == "committed", done
    assert [row["id"] for row in done["supersedes"]] == [old]
    (logged,) = _rows(h)
    assert (logged["door"], logged["standing"]) == ("memory_write", [old])


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


def test_a_quote_matches_a_prompt_on_word_bounds(h: Harness) -> None:
    """The quote is the user's words, not the tail of one of them: "get
    the ledger app deploy rule" is not in "forget the ledger app deploy
    rule"."""
    WORDS.remember(h.store.path, "k1", "please forget the ledger app deploy rule")
    assert WORDS.holds(h.store.path, "k1", "forget the ledger app deploy rule")
    assert not WORDS.holds(h.store.path, "k1", "get the ledger app deploy rule")
