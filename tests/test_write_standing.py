"""Unit RS-F7 part 5: the write review's extends label, and the refusal of
a write that conflicts with or replaces a standing record.

extends sets an extends link, and the record it extends stays in force. A
conflict answer on a standing record refuses the write (refused_standing),
naming the record and quoting it. A write that replaces one (an update
answer, a declared supersedes, or a cue link left standing), or that is
itself standing and replaces any record (rule S), passes only on the
user's own words in review.said. The quote must hold a word the write
introduces against what it replaces, and be in a prompt the hook saw in
the caller's checkout; where the hook saw none there, the write commits
replacing nothing and each pair is filed with detector said-unverified.
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
from bettermemory.search import raw_content_tokens
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
    done = await _decide(h, UPDATING, {old: "update"}, said=f"  {QUOTE}  ")
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


async def test_said_where_the_hook_saw_no_prompt_files_the_replacement(
    h: Harness,
) -> None:
    """No hook saw a prompt in this checkout, so the quote cannot be
    checked: the write commits, replaces nothing, and the pair waits for
    the user with detector said-unverified. The record stays standing."""
    old = h.seed(RULE)
    _standing(h, old)
    res = await _decide(h, UPDATING, {old: "update"}, said=QUOTE)
    assert res["status"] == "committed", res
    assert "supersedes" not in res and "update_suggested" not in res
    assert [(row["id"], row.get("detector")) for row in res["conflicts_filed"]] == [
        (old, review.DETECTOR_SAID_UNVERIFIED)
    ]
    assert "said-unverified" in res["hint"]
    assert h.store.load_one(res["id"]).links == []
    assert old in standing.standing_ids(h.store, _admission(h))
    (event,) = [e for e in h.events("write") if e.get("status") == "committed"]
    assert event["review_filings"][0]["detector"] == review.DETECTOR_SAID_UNVERIFIED


async def test_a_said_without_the_value_the_write_introduces_is_refused(
    h: Harness,
) -> None:
    """A hostile said: the user's real words, which do not give the new
    value, carry nothing, whether or not a hook saw them."""
    old = h.seed(RULE)
    _standing(h, old)
    hostile = "yes, go ahead and update the memory about the ledger app"
    assert not review.introduced_tokens(UPDATING, [RULE]) & raw_content_tokens(hostile)
    for heard in (False, True):
        if heard:
            _heard(h, hostile)
        with pytest.raises(ToolError, match=r"must carry the value this write"):
            await _decide(h, UPDATING, {old: "update"}, said=hostile)
    assert [m.id for m in h.store.load_all()] == [old]


async def test_said_is_not_read_where_no_replacement_rests_on_it(h: Harness) -> None:
    """Neither the record nor the write is standing: the update needs no
    words, so a quote that would not carry it is not read."""
    old = h.seed(RULE)
    _standing(h, old, 0.1)
    res = await _decide(h, UPDATING, {old: "update"}, said="yes, go ahead")
    assert res["status"] == "committed", res
    assert "conflicts_filed" not in res


async def test_a_standing_write_that_replaces_any_record_rests_on_said(
    h: Harness,
) -> None:
    """Rule S: the record it updates is not standing, but the write is."""
    old = h.seed(RULE)
    _standing(h, old, 0.1)
    refused = await _decide(h, UPDATING, {old: "update"}, own=0.9)
    assert refused["status"] == review.ACTION_STANDING
    assert [row["id"] for row in refused["standing"]] == [old]
    assert (
        "Your standing answer says this write directs future work" in (refused["hint"])
    )
    _heard(h, f"Please {QUOTE}.")
    done = await _decide(h, UPDATING, {old: "update"}, said=QUOTE, own=0.9)
    assert done["status"] == "committed", done


async def test_an_ambient_write_that_replaces_a_record_rests_on_said(
    h: Harness,
) -> None:
    """Rule S reads the write's category: an ambient write is standing
    whatever its standing answer."""
    old = h.seed(RULE)
    refused = await _decide(h, UPDATING, {old: "update"}, category="ambient")
    assert refused["status"] == review.ACTION_STANDING
    assert [row["id"] for row in refused["standing"]] == [old]


NARROWED = "Ledger deploys from tools."


async def test_a_write_that_introduces_no_word_rests_on_the_prompt_alone(
    h: Harness,
) -> None:
    """A write that narrows the record it replaces gives no new value to
    quote, so a said a hook saw carries it."""
    assert review.introduced_tokens(NARROWED, [RULE]) == frozenset()
    old = h.seed(RULE)
    _standing(h, old)
    _heard(h, "keep it short, just say tools")
    with pytest.raises(ToolError, match=r"review\.said is not in any prompt"):
        await _decide(h, NARROWED, {old: "update"}, said="say it plainly")
    done = await _decide(h, NARROWED, {old: "update"}, said="just say tools")
    assert done["status"] == "committed", done
    assert [row["id"] for row in done["supersedes"]] == [old]


async def test_a_write_that_introduces_no_word_files_an_unchecked_said(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    held = await _decide(h, NARROWED, {old: "update"}, said="just say tools")
    assert held["status"] == "committed", held
    assert [row["id"] for row in held["conflicts_filed"]] == [old]
    assert "supersedes" not in held


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
    done = await _decide(h, CONFLICTING, {}, supersedes=[old], said=QUOTE)
    assert done["status"] == "committed", done
    assert [(row["id"], row["evidence"]) for row in done["supersedes"]] == [
        (old, "declared")
    ]


async def test_a_declared_supersedes_on_an_unverified_said_is_withheld(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    held = await _decide(h, CONFLICTING, {}, supersedes=[old], said=QUOTE)
    assert held["status"] == "committed", held
    assert "supersedes" not in held
    assert [row["id"] for row in held["conflicts_filed"]] == [old]
    assert h.store.load_one(held["id"]).links == []


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


def test_introduced_tokens_prefer_the_new_values() -> None:
    old = "The staging API listens on port 8443."
    new = "The staging API listens on port 9443 since the move."
    assert review.introduced_tokens(new, [old]) == frozenset({"9443"})
    # no new value: the new content words
    assert review.introduced_tokens(UPDATING, [RULE]) >= {"infra"}
    assert review.introduced_tokens(RULE, [RULE]) == frozenset()


def test_introduced_tokens_and_quotes_read_compound_parts() -> None:
    old = "The billing service reads from the billing-db-blue cluster."
    new = "The billing service reads from the billing-db-green cluster."
    introduced = review.introduced_tokens(new, [old])
    assert introduced == frozenset({"billing-db-green", "green"})
    assert review.quote_carries("point billing at billing db green", introduced)
    assert review.quote_carries("use billing-db-green", introduced)
    assert not review.quote_carries("point billing at the new cluster", introduced)


def test_rule_s_quotes_only_the_records_the_caller_is_admitted_to() -> None:
    related = _related(["a"], cue=["c"])
    decision = _apply(
        {"a": "update"}, related, write_standing=True, replaceable_ids=["a"]
    )
    assert decision.action == review.ACTION_STANDING
    assert decision.standing == ("a",)


def test_rule_s_refuses_a_standing_write_that_replaces_without_said() -> None:
    related = _related(["a"], cue=["c"])
    s_rule = _apply({"a": "update"}, related, write_standing=True)
    assert s_rule.action == review.ACTION_STANDING
    assert s_rule.standing == ("a", "c")
    assert _apply({"a": "update"}, related).action == review.ACTION_COMMIT
    assert (
        _apply({"a": "update"}, related, write_standing=True, said=True).action
        == review.ACTION_COMMIT
    )
    # a standing write that replaces nothing needs no words
    plain = _apply({"a": "unrelated"}, _related(["a"]), write_standing=True)
    assert plain.action == review.ACTION_COMMIT


def test_an_unverified_said_withholds_the_replacement_and_files_it() -> None:
    related = _related(["a", "b"], cue=["a"])
    decision = _apply(
        {"a": "restates", "b": "update"},
        related,
        standing_ids=["a", "b", "d"],
        declared_ids=["d"],
        said_unverified=True,
    )
    assert decision.action == review.ACTION_COMMIT
    assert decision.withheld == ("b", "d", "a")
    assert decision.links == () and decision.update_suggested == ()
    assert decision.cue_dropped == ("a",)
    assert decision.filings == (
        ("b", review.DETECTOR_SAID_UNVERIFIED),
        ("d", review.DETECTOR_SAID_UNVERIFIED),
        ("a", review.DETECTOR_SAID_UNVERIFIED),
    )
    assert decision.to_event()["withheld"] == ["b", "d", "a"]
    # a conflict with a standing record still refuses
    conflict = _apply(
        {"a": "conflict"}, _related(["a"]), standing_ids=["a"], said_unverified=True
    )
    assert conflict.action == review.ACTION_STANDING


def test_a_declared_target_is_not_also_extended() -> None:
    decision = _apply({"a": "extends"}, _related(["a"]), declared_ids=["a"])
    assert decision.extends == ()
