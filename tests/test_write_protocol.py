"""The calls a reviewed write takes.

A chain owes answers for the ids it showed, plus any cue target and any
standing record the recomputation newly finds; rank drift opens no page.
A gate's acknowledgeable refusal carries the related statements and the
questions without opening a review, so the flag and the answers go in one
pre-answered retry. A write that declares itself standing on its
first call is shown its related records and the standing roster on one
page. A write refused for a standing record keeps its chain open, so the
retry with the user's words is one call.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import review, review_counters
from bettermemory.location import resolve_caller
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer

from .test_write_roster import NEAR, PLAN
from ._review_rows import (
    BACKUP_NEW,
    BACKUP_OLD,
    STATUS_OLD,
    FACTS,
    Harness,
    fix_origin_and_identity,
    is_roster,
    set_tau,
)

RULE = FACTS[0]
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


def _standing(h: Harness, record_id: str) -> None:
    h.store.put_standing(record_id, StandingAnswer(0.9, "asked", "test/test"))


def _ids(page: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in page["review"]["related"]]


def _roster_ids(page: dict[str, Any]) -> list[str]:
    return [line.split(" ", 1)[0] for line in page["review"]["roster"]]


def _seed_near(h: Harness) -> list[str]:
    """More records near the write than the related set holds, so the
    ruling sits outside it and on the roster."""
    return [h.seed(text) for text in NEAR]


def _scores(standing: float = 0.0) -> dict[str, Any]:
    return {"unsafe": 0.0, "secret": 0.0, "standing": standing}


async def test_a_standing_write_declared_up_front_gets_one_page(h: Harness) -> None:
    near = _seed_near(h)
    ruling = h.seed(FACTS[5])
    _standing(h, ruling)
    content = PLAN
    first = await h.write(content, review={"relation": {}, **_scores(0.9)})
    assert is_roster(first), first
    assert set(_ids(first)) <= set(near)
    assert ruling in _roster_ids(first)
    assert first["review"]["questions"] == review.questions_payload()
    assert first["review"]["question"] == review.ROSTER_STATEMENT
    assert '"named"' in first["hint"]
    done = await h.write(
        content,
        review={
            "review_id": first["review"]["review_id"],
            "relation": {i: "unrelated" for i in _ids(first)},
            "named": [],
            **_scores(0.9),
        },
    )
    assert done["status"] == "committed", done
    rows = h.events(review.REVIEW_ISSUED)
    assert [bool(row.get("roster")) for row in rows if not row["pre_answered"]] == [
        True
    ]


async def test_a_named_record_comes_back_once_then_the_write_commits(
    h: Harness,
) -> None:
    _seed_near(h)
    ruling = h.seed(FACTS[5])
    _standing(h, ruling)
    content = PLAN
    first = await h.write(content, review={"relation": {}, **_scores(0.9)})
    assert ruling in _roster_ids(first)
    sent = {
        "review_id": first["review"]["review_id"],
        "relation": {i: "unrelated" for i in _ids(first)},
        "named": [ruling],
        **_scores(0.9),
    }
    full = await h.write(content, review=sent)
    assert _ids(full) == [ruling]
    assert "roster" not in full["review"]
    done = await h.write(
        content,
        review={
            **sent,
            "review_id": full["review"]["review_id"],
            "relation": {**sent["relation"], ruling: "unrelated"},
        },
    )
    assert done["status"] == "committed", done


async def test_an_ambient_first_call_lists_the_roster(h: Harness) -> None:
    _seed_near(h)
    ruling = h.seed(FACTS[5])
    _standing(h, ruling)
    first = await h.write("The ledger deploy runs nightly.", category="ambient")
    assert is_roster(first)
    assert ruling in _roster_ids(first)


async def test_a_write_not_declared_standing_keeps_its_related_page(
    h: Harness,
) -> None:
    ruling = h.seed(FACTS[5])
    _standing(h, ruling)
    first = await h.write("The ledger deploy runs nightly.")
    assert "roster" not in first["review"]


async def test_a_refused_standing_write_retries_with_said_in_one_call(
    h: Harness,
) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    first = await h.write(UPDATING)
    assert old in _ids(first)
    sent = {
        "review_id": first["review"]["review_id"],
        "relation": {i: ("update" if i == old else "unrelated") for i in _ids(first)},
        **_scores(),
    }
    refused = await h.write(UPDATING, review=sent)
    assert refused["status"] == review.ACTION_STANDING
    assert [row["id"] for row in refused["standing"]] == [old]
    assert first["review"]["review_id"] in refused["hint"]
    assert "review.said" in refused["hint"]
    # A record that drifts in by rank meanwhile asks for nothing.
    h.seed("The ledger app deploy folder moved once before.")
    checkout = resolve_caller(h.store, h.config, os.getcwd()).resolution.checkout
    assert checkout is not None
    WORDS.remember(h.store.path, checkout, f"Please {QUOTE}.")
    chain = review.find_chain(h.store, first["review"]["review_id"])
    assert chain.open and chain.standing_refused
    done = await h.write(UPDATING, review={**sent, "said": QUOTE})
    assert done["status"] == "committed", done
    chain = review.find_chain(h.store, first["review"]["review_id"])
    assert chain.decided and not chain.standing_refused
    # The counters read the chain's last decision: the commit.
    (counted,) = review_counters.build_chains(
        h.store.iter_events(), now=datetime.now(timezone.utc)
    )
    assert counted.outcome == review.ACTION_COMMIT
    assert counted.calls == 3


async def test_scope_mismatch_refusal_then_one_retry_commits(h: Harness) -> None:
    h.seed("A foo project fact.")
    content = "When working on foo, the build script lives at scripts/build.sh."
    h.store.write(
        content="A foo project fact about its build.",
        scopes=["projects:foo"],
        origin=None,
        home=h.home,
    )
    res = await h.write(content)
    assert res["status"] == "scope_mismatch"
    assert "review" not in res
    done = await h.write(
        content,
        acknowledge_scope_mismatch=True,
        review={
            "relation": {e["id"]: "unrelated" for e in res["related"]},
            **_scores(),
        },
    )
    assert done["status"] == "committed", done


async def test_a_terminal_refusal_carries_no_review(h: Harness) -> None:
    h.seed(FACTS[2])
    res = await h.write(FACTS[2])
    assert res["status"] == "duplicate"
    assert "related" not in res


async def test_an_answer_error_names_the_format(h: Harness) -> None:
    first = await h.write("The ledger deploy runs nightly.")
    with pytest.raises(ToolError, match=r"review\.relation\['x'\].*Format: review="):
        await h.write(
            "The ledger deploy runs nightly.",
            review={
                "review_id": first["review"]["review_id"],
                "relation": {"x": "maybe"},
                **_scores(),
            },
        )


# ---------------------------------------------------------------------------
# Answers hold only for what they were given for
# ---------------------------------------------------------------------------

SWAPPED = "The public status page moved off Statuspage to Instatus."


async def test_answers_for_one_content_do_not_cover_another(h: Harness) -> None:
    old = h.seed(BACKUP_OLD)
    status = h.seed(STATUS_OLD)
    first = await h.write(BACKUP_NEW)
    assert _ids(first) == [old]
    assert status in h.related_ids(SWAPPED)
    res = await h.write(SWAPPED, review=h.answers(first))
    assert res["status"] == "review", res
    assert status in _ids(res)
    row = h.events(review.REVIEW_ISSUED)[-1]
    assert row["parent_review_id"] is None
    assert row["sent_review_id"] == first["review"]["review_id"]


async def test_a_chain_with_no_recorded_subject_covers_no_content(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review page a log recorded without a subject binds no call: its
    answers are read against the full set of the content as sent."""
    from bettermemory.handlers import write as write_handler

    record = write_handler._record_review_issued

    def without_subject(*args: Any, **kwargs: Any) -> None:
        record(*args, **{**kwargs, "subject": None})

    old = h.seed(BACKUP_OLD)
    status = h.seed(STATUS_OLD)
    with monkeypatch.context() as patched:
        patched.setattr(write_handler, "_record_review_issued", without_subject)
        first = await h.write(BACKUP_NEW)
    assert _ids(first) == [old]
    assert review.find_chain(h.store, first["review"]["review_id"]).subject is None
    res = await h.write(SWAPPED, review=h.answers(first))
    assert res["status"] == "review", res
    assert status in _ids(res)


@pytest.mark.parametrize(
    "change", [{"scopes": ["tools", "ops"]}, {"category": "user-inference"}]
)
async def test_a_changed_scope_or_category_leaves_the_chain(
    h: Harness, change: dict[str, Any]
) -> None:
    h.seed(BACKUP_OLD)
    first = await h.write(BACKUP_NEW)
    res = await h.write(BACKUP_NEW, review=h.answers(first), **change)
    assert res["status"] in ("committed", "review"), res
    row = h.events(review.REVIEW_ISSUED)[-1]
    assert row["sent_review_id"] == first["review"]["review_id"]
    assert row["review_id"] != first["review"]["review_id"]


async def test_a_refused_standing_chain_takes_no_other_content(h: Harness) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    status = h.seed(STATUS_OLD)
    first = await h.write(UPDATING)
    sent = {
        "review_id": first["review"]["review_id"],
        "relation": {i: ("update" if i == old else "unrelated") for i in _ids(first)},
        **_scores(),
    }
    refused = await h.write(UPDATING, review=sent)
    assert refused["status"] == review.ACTION_STANDING
    assert status in h.related_ids(SWAPPED)
    res = await h.write(SWAPPED, review={**sent, "relation": {}})
    assert res["status"] == "review", res
    assert status in _ids(res)


async def test_a_dropped_refused_standing_chain_counts_as_refused(h: Harness) -> None:
    old = h.seed(RULE)
    _standing(h, old)
    first = await h.write(UPDATING)
    sent = {
        "review_id": first["review"]["review_id"],
        "relation": {i: ("update" if i == old else "unrelated") for i in _ids(first)},
        **_scores(),
    }
    assert (await h.write(UPDATING, review=sent))["status"] == review.ACTION_STANDING
    (counted,) = review_counters.build_chains(
        h.store.iter_events(), now=datetime.now(timezone.utc)
    )
    assert counted.outcome == review.ACTION_STANDING
    assert review.find_chain(h.store, first["review"]["review_id"]).standing_refused
