"""The delivery rows under rule rs-v5 (design 8.11): what the `session_brief`
and `prompt_recall` rows carry, and how `bettermemory eval` reads them.

From rs-v5 each row carries the caller's ``checkout`` (None where its
resolution names none), ``membership_rule`` (the resolution's rule, or the
outcome that admitted the caller nothing) and ``rc3_admitted``: the ids
rc3's rule, read from the origin columns, would have delivered. The rows
are written by the production surfaces against real repositories under
``tmp_path``; the eval reads them back and reports the split on the rule
with the membership delta.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location
from bettermemory.admission import NO_RESOLUTION
from bettermemory.eval import EARLIER_RULES, compute_eval
from bettermemory.membership import INDETERMINATE, SEARCH_UNSCOPED
from bettermemory.origin import Origin
from bettermemory.recall import RULE, RecallState, session_brief

from .test_eval import _run_eval_cli
from .test_membership_security import _git
from .test_recall_brief import _World
from .test_recall_prompt import OTHER_REPO, REPO, PromptWorld, config

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _rows(world: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in world.store.iter_events() if e["kind"] == kind]


def _linked_worktree(primary: Path, path: Path) -> Path:
    """A linked worktree of `primary` at `path`: another checkout of the
    same project (design 2.1), at another worktree root."""
    _git(primary, "worktree", "add", "-q", "-b", "side", str(path))
    return path.resolve()


def test_the_rule_is_rs_v5() -> None:
    assert RULE == "rs-v6"


# ---------------------------------------------------------------------------
# The brief's row
# ---------------------------------------------------------------------------


def test_a_brief_row_carries_the_checkout_the_rule_and_rc3_admitted(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")

    result = world.brief()

    assert result.ids == (second.id, first.id)
    (row,) = _rows(world, "session_brief")
    resolution = world.resolve(world.repo).resolution
    assert row["rule"] == "rs-v6"
    assert row["checkout"] == world.home.checkout == resolution.checkout
    assert row["membership_rule"] == resolution.rule
    # Written in the caller's own checkout: rc3 delivered them too.
    assert row["rc3_admitted"] == [second.id, first.id]


def test_a_brief_row_of_a_caller_nothing_was_read_for(tmp_path: Path) -> None:
    """A caller nothing could be read for is admitted nothing: its row
    names no checkout, the outcome stands for the rule, and no candidate
    was asked about."""
    world = _World(tmp_path)
    world.fresh("prefer short release notes in this project")

    unknown = world.resolve(None).resolution
    assert unknown.outcome == INDETERMINATE
    world.brief(resolution=unknown)

    (row,) = _rows(world, "session_brief")
    assert row["ids"] == []
    assert row["checkout"] is None
    assert row["membership_rule"] == INDETERMINATE
    assert row["rc3_admitted"] == []


def test_a_brief_row_of_a_resolution_that_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    world.fresh("prefer short release notes in this project")

    def raises(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the ladder failed")

    monkeypatch.setattr(location, "resolve_hook", raises)
    session_brief(
        session_id="raised",
        config=world.config,
        store=world.store,
        state=RecallState(),
        cwd=world.repo,
    )
    (row,) = _rows(world, "session_brief")
    assert row["checkout"] is None
    assert row["membership_rule"] == NO_RESOLUTION
    assert row["rc3_admitted"] == []


# ---------------------------------------------------------------------------
# The prompt recall's row
# ---------------------------------------------------------------------------


def test_a_recall_row_carries_the_checkout_the_rule_and_rc3_admitted(
    tmp_path: Path,
) -> None:
    world = PromptWorld(tmp_path)
    memory = world.write()

    result = world.recall()

    assert result.ids == (memory.id,)
    (row,) = _rows(world, "prompt_recall")
    resolution = world.resolve(world.worktree).resolution
    assert row["rule"] == "rs-v6"
    assert row["checkout"] == world.home.checkout == resolution.checkout
    assert row["membership_rule"] == resolution.rule
    assert row["rc3_admitted"] == [memory.id]


def test_a_cross_worktree_record_is_delivered_by_membership_not_by_rc3(
    tmp_path: Path,
) -> None:
    """A record written in a linked worktree of the caller's repository is
    the caller's project's (design 2.1), so the brief and the recall
    deliver it; rc3's worktree test kept a sibling worktree's notes apart,
    so its id is not in the row's rc3_admitted, and the eval counts it in
    the membership delta."""
    world = PromptWorld(tmp_path)
    linked = _linked_worktree(world.worktree, tmp_path / "foo-side")
    there = Origin(cwd=str(linked), repo=REPO, worktree_root=str(linked))
    side_home = world.resolve(linked).home()
    assert side_home.project == world.home.project
    assert side_home.checkout != world.home.checkout
    memory = world.write(origin=there, home=side_home)

    result = world.recall()

    assert result.ids == (memory.id,)
    (row,) = _rows(world, "prompt_recall")
    assert row["shown_ids"] == [memory.id]
    assert row["rc3_admitted"] == []

    report = compute_eval(world.store.load_all(), list(world.store.iter_events()))
    assert report.surfaces is not None
    assert report.surfaces["membership_delta"] == {
        "rows": 1,
        "rows_differing": 1,
        "delivered_not_rc3": 1,
        "rc3_not_delivered": 0,
    }
    split = report.surfaces["by_rule"]
    assert split["rs-v6"]["recall"]["deliveries"] == 1
    assert split[EARLIER_RULES]["recall"]["deliveries"] == 0


def test_under_everywhere_rc3_admitted_reads_rc3s_auto_scope(tmp_path: Path) -> None:
    """Under ``prompt_recall = "everywhere"`` rc3 delivered what its
    search's auto-scope admitted: a record of another repository's remote
    was not, while the unscoped admission delivers it now."""
    world = PromptWorld(tmp_path)
    everywhere = config(world.store_dir, mode="everywhere")
    elsewhere = Origin(
        cwd=str(tmp_path / "bar"), repo=OTHER_REPO, worktree_root=str(tmp_path / "bar")
    )
    other = world.write(origin=elsewhere)

    result = world.recall(config=everywhere)

    assert result.ids == (other.id,)
    (row,) = _rows(world, "prompt_recall")
    assert row["admission"] == SEARCH_UNSCOPED
    assert row["rc3_admitted"] == []
    assert row["checkout"] == world.home.checkout


# ---------------------------------------------------------------------------
# `bettermemory eval` over the rows
# ---------------------------------------------------------------------------


def test_bettermemory_eval_prints_the_split_and_the_delta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = PromptWorld(tmp_path)
    linked = _linked_worktree(world.worktree, tmp_path / "foo-side")
    there = Origin(cwd=str(linked), repo=REPO, worktree_root=str(linked))
    memory = world.write(origin=there, home=world.resolve(linked).home())
    assert world.recall().ids == (memory.id,)

    _run_eval_cli(world.store_dir, "--since", "all")
    text = capsys.readouterr().out
    assert "Recall surfaces (rule rs-v6):" in text
    assert "  By rule:" in text
    assert (
        "    rs-v6 recall: deliveries=1 shown=1 opened=0 used=0 ignored=0 helped=0"
        in text
    )
    assert (
        "    earlier (none) recall: deliveries=0 shown=0 opened=0 used=0 "
        "ignored=0 helped=0" in text
    )
    assert (
        "  membership_delta rows=1 differing=1 delivered_not_rc3=1 "
        "rc3_not_delivered=0" in text
    )

    _run_eval_cli(world.store_dir, "--json", "--since", "all")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["surfaces"]["membership_delta"]["delivered_not_rc3"] == 1
    assert parsed["surfaces"]["by_rule"]["rs-v6"]["rules"] == ["rs-v6"]
