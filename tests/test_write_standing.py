"""Unit RS-F7 part 5: the write review's extends label, and the refusal of
a write that conflicts with or replaces a standing record.

extends sets an extends link, and the record it extends stays in force. A
conflict answer on a standing record refuses the write (refused_standing),
naming the record and quoting it. A write that replaces one (an update
answer, a declared supersedes, or a cue link left standing) passes only on
the user's own words in review.said, checked as a join's are: the quote is
in a prompt the hook saw in the caller's checkout; where the hook saw none
there, the words are taken as given and the reply says so.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, standing
from bettermemory.admission import Admission
from bettermemory.location import resolve_caller
from bettermemory.membership import REVIEW_STRATA
from bettermemory.models import Category, LinkType
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer
from bettermemory.supersession import SupersessionMatch, SupersessionReport

from ._review_rows import FACTS, ORIGIN, Harness, fix_origin_and_identity, set_tau

RULE = FACTS[0]
CONFLICTING = "The ledger app deploys from the infra folder."
UPDATING = "The ledger app deploys from the infra folder since the tools folder went."
QUOTE = "deploy the ledger app from the infra folder from now on"


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
    checkout = resolve_caller(h.store, h.config, os.getcwd()).resolution.checkout
    assert checkout is not None
    WORDS.remember(h.store.path, checkout, words)


async def _decide(
    h: Harness,
    content: str,
    labels: dict[str, str],
    *,
    said: str | None = None,
    supersedes: list[str] | None = None,
    own: float = 0.0,
    category: str | None = None,
) -> dict[str, Any]:
    """`content` decided in one call: every related id answered (by
    `labels`, else unrelated), scores 0 and the write's own standing
    answer `own`. The related set leaves out the declared targets, as the
    daemon's does."""
    ids = review.related_for(
        h.store,
        content,
        behavior=h.config.behavior,
        admission=_admission(h),
        exclude_ids=supersedes or [],
    ).ids
    answers: dict[str, Any] = {
        "relation": {i: labels.get(i, "unrelated") for i in ids},
        "unsafe": 0.0,
        "secret": 0.0,
        "standing": own,
    }
    if said is not None:
        answers["said"] = said
    args: dict[str, Any] = {"review": answers}
    if supersedes is not None:
        args["supersedes"] = supersedes
    if category is not None:
        args["category"] = category
    return await h.write(content, **args)


def _admission(h: Harness) -> Admission:
    return Admission.of(REVIEW_STRATA, resolve_caller(h.store, h.config, os.getcwd()))


# ---------------------------------------------------------------------------
# extends
# ---------------------------------------------------------------------------


def test_extends_is_the_fifth_label_in_the_drafts_words() -> None:
    assert review.LABELS == ("update", "extends", "conflict", "restates", "unrelated")
    criteria = review.questions_payload()["relation"]["criteria"]
    assert criteria["extends"] == (
        "It adds to that stored statement, which stays in force."
    )
    assert '"extends"' in review.ANSWER_FORMAT


async def test_extends_sets_an_extends_link_and_the_record_stays_in_force(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    content = "The ledger app deploy from the tools folder also tags the release."
    assert old in h.related_ids(content)
    res = await _decide(h, content, {old: "extends"})
    assert res["status"] == "committed", res
    assert res["extends"] == [{"id": old, "evidence": "review"}]
    memory = h.store.load_one(res["id"])
    assert [(link.type, link.target_id, link.note) for link in memory.links] == [
        (LinkType.EXTENDS, old, review.REVIEW_LINK_NOTE)
    ]
    assert old in standing.standing_ids(h.store, _admission(h))


# ---------------------------------------------------------------------------
# A conflict with a standing record
# ---------------------------------------------------------------------------


async def test_a_conflict_on_a_standing_record_refuses_and_quotes_it(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    assert old in h.related_ids(CONFLICTING)
    before = {m.id for m in h.store.load_all()}
    res = await _decide(h, CONFLICTING, {old: "conflict"})
    assert res["status"] == review.ACTION_STANDING
    assert [(row["id"], row["text"]) for row in res["standing"]] == [(old, RULE)]
    assert "review.said" in res["hint"]
    assert {m.id for m in h.store.load_all()} == before
    (event,) = [
        e for e in h.events("write") if e.get("status") == review.ACTION_STANDING
    ]
    assert event["standing"] == [old]
    assert event["review_action"] == review.ACTION_STANDING


async def test_the_users_words_do_not_carry_a_conflict(h: Harness) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    _heard(h, QUOTE)
    res = await _decide(h, CONFLICTING, {old: "conflict"}, said=QUOTE)
    assert res["status"] == review.ACTION_STANDING


async def test_a_conflict_is_refused_whatever_said_holds(h: Harness) -> None:
    """A write that conflicts with one standing record and replaces
    another is refused for the conflict before said is read, so a quote
    that would not carry the replacement is no teaching error."""
    old = h.seed(RULE)
    other = h.seed(FACTS[1])
    _standing(h, old)
    _standing(h, other)
    res = await _decide(
        h, CONFLICTING, {old: "conflict"}, supersedes=[other], said="yes, go ahead"
    )
    assert res["status"] == review.ACTION_STANDING
    assert [row["id"] for row in res["standing"]] == [old]


async def test_an_ambient_record_is_standing(h: Harness) -> None:
    old = h.store.write(
        content=RULE,
        scopes=["tools"],
        origin=ORIGIN,
        home=h.home,
        category=Category.AMBIENT,
    ).id
    res = await _decide(h, CONFLICTING, {old: "conflict"})
    assert res["status"] == review.ACTION_STANDING


async def test_a_conflict_on_a_record_below_the_threshold_is_filed(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old, 0.1)
    res = await _decide(h, CONFLICTING, {old: "conflict"})
    assert res["status"] == "committed", res
    assert old in [row["id"] for row in res["conflicts_filed"]]


# ---------------------------------------------------------------------------
# Replacing a standing record
# ---------------------------------------------------------------------------


async def test_an_update_of_a_standing_record_rests_on_the_users_words(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    refused = await _decide(h, UPDATING, {old: "update"})
    assert refused["status"] == review.ACTION_STANDING
    _heard(h, f"Please {QUOTE}.")
    done = await _decide(h, UPDATING, {old: "update"}, said=f"  {QUOTE}  ", own=0.9)
    assert done["status"] == "committed", done
    (event,) = [e for e in h.events("write") if e.get("status") == "committed"]
    assert event["review_answers"]["said"] == QUOTE


async def test_said_the_hook_did_not_see_is_a_teaching_error(h: Harness) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    _heard(h, "Something else entirely was asked in this checkout.")
    with pytest.raises(ToolError, match=r"review\.said is not in any prompt"):
        await _decide(h, UPDATING, {old: "update"}, said=QUOTE)
    assert [m.id for m in h.store.load_all()] == [old]


async def test_said_where_the_hook_saw_no_prompt_is_taken_as_given(
    h: Harness,
) -> None:
    """No hook saw a prompt in this checkout, so the quote cannot be
    checked: the write commits on the words as given, replaces the
    record, and the reply and the row say the words were not checked."""
    old = h.seed(RULE)
    _standing(h, old)
    res = await _decide(h, UPDATING, {old: "update"}, said=QUOTE)
    assert res["status"] == "committed", res
    assert [row["id"] for row in res["supersedes"]] == [old]
    assert res["said_checked"] is False
    assert "conflicts_filed" not in res
    assert old not in standing.standing_ids(h.store, _admission(h))
    (row,) = h.events("standing_guard")
    assert (row["door"], row["standing"]) == ("memory_write", [old])
    assert (row["said"], row["said_checked"]) == (QUOTE, False)


async def test_the_words_are_checked_for_presence_only(h: Harness) -> None:
    """The guard checks that the user typed the quote, not what it meant:
    words the hook saw carry the replacement whatever they say, and the
    quote is on the log for the user to read."""
    old = h.seed(RULE)
    _standing(h, old)
    words = "yes, go ahead and update the memory about the ledger app"
    _heard(h, "Something else entirely was asked in this checkout.")
    with pytest.raises(ToolError, match=r"review\.said is not in any prompt"):
        await _decide(h, UPDATING, {old: "update"}, said=words)
    _heard(h, words)
    done = await _decide(h, UPDATING, {old: "update"}, said=words)
    assert done["status"] == "committed", done
    assert "said_checked" not in done
    (row,) = h.events("standing_guard")
    assert (row["said"], row["said_checked"]) == (words, True)


async def test_said_is_not_read_where_no_replacement_rests_on_it(h: Harness) -> None:
    """Neither the record nor the write is standing: the update needs no
    words, so a quote that would not carry it is not read."""
    old = h.seed(RULE)
    _standing(h, old, 0.1)
    res = await _decide(h, UPDATING, {old: "update"}, said="yes, go ahead")
    assert res["status"] == "committed", res
    assert "conflicts_filed" not in res


async def test_a_standing_write_that_replaces_a_plain_record_needs_no_words(
    h: Harness,
) -> None:
    """The record it updates is not standing: the write's own standing
    answer, or an ambient category, asks nothing of the user's words."""
    old = h.seed(RULE)
    _standing(h, old, 0.1)
    done = await _decide(h, UPDATING, {old: "update"}, own=0.9)
    assert done["status"] == "committed", done
    other = h.seed(FACTS[1])
    changed = "Grafana dashboards for the queue moved to the platform repo."
    assert other in h.related_ids(changed)
    ambient = await _decide(h, changed, {other: "update"}, category="ambient")
    assert ambient["status"] == "committed", ambient
    assert h.events("standing_guard") == []


NARROWED = "Ledger deploys from tools."
NARROWING = "shorten the ledger deploy note, the app folder part is noise"


async def test_a_write_that_narrows_a_record_rests_on_the_prompt_alone(
    h: Harness,
) -> None:
    """A write that narrows the record it replaces brings no new value:
    the quote carries it as any other does, by having been typed."""
    old = h.seed(RULE)
    _standing(h, old)
    _heard(h, NARROWING)
    with pytest.raises(ToolError, match=r"review\.said is not in any prompt"):
        await _decide(h, NARROWED, {old: "update"}, said=NARROWING + " please")
    done = await _decide(h, NARROWED, {old: "update"}, said=NARROWING)
    assert done["status"] == "committed", done
    assert [row["id"] for row in done["supersedes"]] == [old]


async def test_an_empty_said_is_a_teaching_error(h: Harness) -> None:
    old = h.seed(RULE)
    with pytest.raises(ToolError, match=r"review\.said quotes the user's words"):
        await _decide(h, UPDATING, {old: "update"}, said="   ")


async def test_a_declared_supersedes_of_a_standing_record_rests_on_said(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    refused = await _decide(h, CONFLICTING, {}, supersedes=[old])
    assert refused["status"] == review.ACTION_STANDING
    assert [row["id"] for row in refused["standing"]] == [old]
    assert refused["standing"][0]["text"] == RULE
    _heard(h, QUOTE)
    done = await _decide(h, CONFLICTING, {}, supersedes=[old], said=QUOTE, own=0.9)
    assert done["status"] == "committed", done
    assert [(row["id"], row["evidence"]) for row in done["supersedes"]] == [
        (old, "declared")
    ]


async def test_a_declared_supersedes_on_unchecked_words_is_set(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    done = await _decide(h, CONFLICTING, {}, supersedes=[old], said=QUOTE)
    assert done["status"] == "committed", done
    assert [(row["id"], row["evidence"]) for row in done["supersedes"]] == [
        (old, "declared")
    ]
    assert done["said_checked"] is False
    assert [x.target_id for x in h.store.load_one(done["id"]).links] == [old]


async def test_an_update_of_a_record_that_is_not_standing_needs_no_words(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    res = await _decide(h, UPDATING, {old: "update"})
    assert res["status"] == "committed", res


# ---------------------------------------------------------------------------
# The decision, pure
# ---------------------------------------------------------------------------

_TAU = review.Tau(unsafe=0.5, secret=0.5)


def _related(ids: list[str], cue: list[str] = ()) -> review.RelatedSet:  # type: ignore[assignment]
    return review.RelatedSet(
        entries=tuple(
            review.RelatedEntry(id=i, scopes=(), updated="", claim_sized=True, text="")
            for i in ids
        ),
        report=SupersessionReport(
            eligible=True,
            supersedes=[
                SupersessionMatch(i, "supersedes", "", None, "", "", 0.0, "")
                for i in cue
            ],
            conflicts=[],
        ),
    )


def _apply(
    relation: dict[str, str], related: review.RelatedSet, **kw: Any
) -> review.ReviewDecision:
    answers = review.ReviewAnswers(
        relation=relation,
        unsafe=kw.pop("unsafe", 0.0),
        secret=kw.pop("secret", 0.0),
    )
    return review.apply_review(
        answers,
        related,
        tau=_TAU,
        write_claim_sized=True,
        write_supersession=True,
        **kw,
    )


def test_the_scores_refuse_before_a_standing_record_does() -> None:
    decision = _apply(
        {"a": "conflict"}, _related(["a"]), secret=0.9, standing_ids=["a"]
    )
    assert decision.action == review.ACTION_SECRET


def test_a_standing_conflict_refuses_before_restates_keeps_a_write_out() -> None:
    decision = _apply(
        {"a": "conflict", "b": "restates"}, _related(["a", "b"]), standing_ids=["a"]
    )
    assert decision.action == review.ACTION_STANDING
    assert decision.standing == ("a",)
    assert decision.to_event()["standing"] == ["a"]


def test_a_restating_write_replaces_nothing_and_stays_a_duplicate() -> None:
    decision = _apply({"a": "restates"}, _related(["a"], cue=["a"]), standing_ids=["a"])
    assert decision.action == review.ACTION_RESTATES


def test_a_cue_link_left_standing_on_a_standing_record_rests_on_said() -> None:
    related = _related(["a", "b"], cue=["a"])
    relation = {"a": "restates", "b": "update"}
    refused = _apply(relation, related, standing_ids=["a"])
    assert refused.action == review.ACTION_STANDING
    assert refused.standing == ("a",)
    assert _apply(relation, related, standing_ids=["a"], said=True).action == (
        review.ACTION_COMMIT
    )
    # Disputed, the cue link is dropped, so nothing replaces the record.
    disputed = _apply({"a": "unrelated", "b": "update"}, related, standing_ids=["a"])
    assert disputed.action == review.ACTION_COMMIT
    assert disputed.cue_dropped == ("a",)


def test_extends_on_a_cue_linked_target_drops_the_link_and_extends_it() -> None:
    decision = _apply({"a": "extends"}, _related(["a"], cue=["a"]))
    assert decision.action == review.ACTION_COMMIT
    assert decision.cue_dropped == ("a",)
    assert decision.extends == ("a",)
    assert decision.filings == (("a", review.DETECTOR_REVIEW_DISAGREES),)


def test_extends_commits_a_write_that_also_restates() -> None:
    decision = _apply({"a": "restates", "b": "extends"}, _related(["a", "b"]))
    assert decision.action == review.ACTION_COMMIT
    assert decision.restated == ("a",)
    assert decision.extends == ("b",)


def test_replaced_ids_reads_updates_declared_and_undisputed_cue_links() -> None:
    related = _related(["a", "b", "c"], cue=["c", "d", "e"])
    answers = review.ReviewAnswers(
        relation={"a": "update", "b": "conflict", "c": "unrelated"},
        unsafe=0.0,
        secret=0.0,
    )
    assert review.replaced_ids(answers, related, ["x", "a"]) == ["a", "x", "d", "e"]


def test_a_declared_supersedes_of_a_standing_record_rests_on_said_alone() -> None:
    related = _related(["a", "b"], cue=["a"])
    decision = _apply(
        {"a": "restates", "b": "update"},
        related,
        standing_ids=["a", "b", "d"],
        declared_ids=["d"],
    )
    assert decision.action == review.ACTION_STANDING
    assert decision.standing == ("b", "d", "a")
    carried = _apply(
        {"a": "restates", "b": "update"},
        related,
        standing_ids=["a", "b", "d"],
        declared_ids=["d"],
        said=True,
    )
    assert carried.action == review.ACTION_COMMIT
    assert carried.links == ("b",)
    # a conflict with a standing record refuses whatever said holds
    conflict = _apply({"a": "conflict"}, _related(["a"]), standing_ids=["a"], said=True)
    assert conflict.action == review.ACTION_STANDING


def test_a_declared_target_is_not_also_extended() -> None:
    decision = _apply({"a": "extends"}, _related(["a"]), declared_ids=["a"])
    assert decision.extends == ()
