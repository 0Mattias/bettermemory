"""The agent's declarations (design 8.8; the owner's ruling of
2026-10-21): `memory_admin(action="answer_question" | "join_project" |
"unjoin_project")`, served over the MCP surface on real repositories.

Each test stands the process in a checkout under `tmp_path` (`neutral_git`
and real git), opens a question the way the ladder does (a clone under
another remote holding the project's commits only: one witness, asked),
and calls the tool. The user's words a join rests on are what the prompt
hook keeps per checkout (`recall.WORDS`), filled here by hand or through
`prompt_recall` itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location, recall
from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig
from bettermemory.location import resolve_caller
from bettermemory.membership import ANSWERED, ASK, LEGACY, OPEN, SILENT, Membership
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call
from .test_membership_resolution import clone, commit, git, make_repo
from .test_membership_security import neutral_git


@pytest.fixture(autouse=True)
def _fresh_words() -> Any:
    recall.WORDS.clear()
    yield
    recall.WORDS.clear()


class Bench:
    """A store, a project `app` with one memory, and a fork of it under
    another remote whose first sight asks (one witness)."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        neutral_git(tmp_path, monkeypatch)
        self.tmp = tmp_path.resolve()
        self.store_dir = self.tmp / "store"
        self.store = Store(self.store_dir)
        self.config = Config(storage=StorageConfig(directory=str(self.store_dir)))
        self.monkeypatch = monkeypatch
        self.app = make_repo(
            self.tmp / "app", commits=2, remote="example.invalid/a/app"
        )
        monkeypatch.chdir(self.app)
        here = resolve_caller(self.store, self.config, str(self.app))
        self.project = str(here.resolution.project)
        self.checkout = str(here.resolution.checkout)
        with self.store.batch():
            m = self.store.write(
                content="memory in app", scopes=["tools"], home=here.home()
            )
            here.add_head_witness("write")
        self.memory = m.id
        location.forget()
        self.server = build_server(
            config=self.config, store=self.store, state=SessionState()
        )

    def fork(self, name: str = "fork") -> tuple[Path, str, str]:
        """A clone under another remote, stood in: asked, with its question
        and checkout."""
        path = clone(self.app, self.tmp / name, url=f"example.invalid/me/{name}")
        self.monkeypatch.chdir(path)
        location.forget()
        res = resolve_caller(self.store, self.config, str(path)).resolution
        assert res.outcome == ASK and res.proposal is not None
        location.forget()
        return path, str(res.proposal), str(res.checkout)

    def model(self) -> Membership:
        return Membership(self.store, location.process_machine().id)

    async def admin(self, **kw: Any) -> Any:
        return await _mcp_call(self.server, "memory_admin", kw)

    def rows(self, *kinds: str) -> list[dict[str, Any]]:
        return [json.loads(r.payload) for r in self.store.membership_log(list(kinds))]

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    b = Bench(tmp_path, monkeypatch)
    yield b
    b.close()


# ---------------------------------------------------------------------------
# answer_question
# ---------------------------------------------------------------------------


async def test_separate_is_the_agents_own_answer_and_moves_nothing(
    bench: Bench,
) -> None:
    """`separate` needs no words: the question closes, the fork is a
    project of its own, the memory stays in app, the row names the agent."""
    fork, qid, kid = bench.fork()
    out = await bench.admin(action="answer_question", id=qid, answer="separate")
    assert out["question"] == qid and out["answer"] == "separate"
    assert out["tethered"] is None
    assert f"question {qid} answered: separate" in out["line"]
    pr = bench.model().proposal(qid)
    assert (pr.state, pr.answer, pr.answered_by) == (
        ANSWERED,
        "new",
        "declaration:agent:mcp",
    )
    assert out["project"] != bench.project
    home = bench.model().home(bench.memory)
    assert home is not None and home.project == bench.project
    res = resolve_caller(bench.store, bench.config, str(fork)).resolution
    assert (res.outcome, res.project) == (SILENT, out["project"])


async def test_the_callers_checkouts_first_question_needs_no_id(bench: Bench) -> None:
    _fork, qid, _kid = bench.fork()
    out = await bench.admin(action="answer_question", answer="separate")
    assert out["question"] == qid
    with pytest.raises(Exception, match="no open question"):
        await bench.admin(action="answer_question", answer="separate")


async def test_join_needs_the_users_words_the_hook_saw(bench: Bench) -> None:
    """`join` without `said` is refused; with words the hook never saw in
    the checkout it is refused; with the user's words it merges the fork's
    project into app's, the row carries the words, and the reply names
    the reversal."""
    fork, qid, kid = bench.fork()
    with pytest.raises(Exception, match="rests on the user's words"):
        await bench.admin(action="answer_question", id=qid, answer="join")
    recall.WORDS.remember(
        bench.store.path, kid, "Yes, the fork is the same project as app, join them."
    )
    with pytest.raises(Exception, match="not in any prompt the hook saw"):
        await bench.admin(
            action="answer_question", id=qid, answer="join", said="merge everything"
        )
    assert bench.model().proposal(qid).state == OPEN
    out = await bench.admin(
        action="answer_question",
        id=qid,
        answer="join",
        said="the fork is the same project as app",
    )
    assert out["answer"] == f"join:{bench.project}"
    assert out["tethered"] is True
    assert out["project"] == bench.project
    assert out["reversal"] is not None and "unjoin_project" in out["reversal"]
    (answered,) = [r for r in bench.rows("proposal_answered") if r["proposal"] == qid]
    assert answered["said"] == "the fork is the same project as app"
    assert answered["by"] == "declaration:agent:mcp"
    res = resolve_caller(bench.store, bench.config, str(fork)).resolution
    assert (res.outcome, res.project) == (SILENT, bench.project)


async def test_a_short_or_folded_quote_is_matched_as_the_user_typed_it(
    bench: Bench,
) -> None:
    _fork, qid, kid = bench.fork()
    recall.WORDS.remember(bench.store.path, kid, "sure,   SAME project\nplease")
    with pytest.raises(Exception, match="not in any prompt"):
        await bench.admin(action="answer_question", id=qid, answer="join", said="sure")
    out = await bench.admin(
        action="answer_question", id=qid, answer="join", said="Same Project please"
    )
    assert out["tethered"] is True


async def test_join_where_no_hook_saw_the_checkout_is_logged_untethered(
    bench: Bench,
) -> None:
    """A client without hooks: the daemon saw no prompt for the checkout,
    so the quote is taken as given and the reply says so."""
    _fork, qid, _kid = bench.fork()
    out = await bench.admin(
        action="answer_question", id=qid, answer="join", said="join them please"
    )
    assert out["tethered"] is False
    assert out["said"] == "join them please"
    (answered,) = [r for r in bench.rows("proposal_answered") if r["proposal"] == qid]
    assert answered["said"] == "join them please"


async def test_join_refuses_a_project_the_question_does_not_name(bench: Bench) -> None:
    _fork, qid, kid = bench.fork()
    other = make_repo(bench.tmp / "other", commits=1)
    location.forget()
    theirs = str(
        resolve_caller(bench.store, bench.config, str(other)).resolution.project
    )
    location.forget()
    recall.WORDS.remember(bench.store.path, kid, "join it to other")
    with pytest.raises(Exception, match="does not offer"):
        await bench.admin(
            action="answer_question",
            id=qid,
            answer="join",
            target=theirs,
            said="join it to other",
        )
    with pytest.raises(Exception, match='"separate" or "join"'):
        await bench.admin(action="answer_question", id=qid, answer="maybe")
    with pytest.raises(Exception, match="no question Q999"):
        await bench.admin(action="answer_question", id="Q999", answer="separate")


async def test_an_answered_question_is_refused(bench: Bench) -> None:
    _fork, qid, _kid = bench.fork()
    await bench.admin(action="answer_question", id=qid, answer="separate")
    with pytest.raises(Exception, match="already answered"):
        await bench.admin(action="answer_question", id=qid, answer="separate")


# ---------------------------------------------------------------------------
# join_project and unjoin_project
# ---------------------------------------------------------------------------


async def test_join_project_merges_a_separate_project_on_the_users_words_and_unjoin_undoes_it(
    bench: Bench,
) -> None:
    """A fork answered `separate` is later named by the user: `join_project`
    merges it into the caller's project (standing in app), its records
    are app's; `unjoin_project` gives them back."""
    fork, qid, kid = bench.fork()
    theirs = (await bench.admin(action="answer_question", id=qid, answer="separate"))[
        "project"
    ]
    with bench.store.batch():
        who = resolve_caller(bench.store, bench.config, str(fork))
        written = bench.store.write(
            content="memory in fork", scopes=["tools"], home=who.home()
        )
    location.forget()
    bench.monkeypatch.chdir(bench.app)
    with pytest.raises(Exception, match="rests on the user's words"):
        await bench.admin(action="join_project", id=theirs)
    recall.WORDS.remember(
        bench.store.path, bench.checkout, "the fork belongs to app, merge it"
    )
    out = await bench.admin(action="join_project", id=theirs, said="merge it")
    assert (out["project"], out["merged"], out["tethered"]) == (
        bench.project,
        theirs,
        True,
    )
    assert out["reversal"] == f'memory_admin(action="unjoin_project", id="{theirs}")'
    home = bench.model().home(written.id)
    assert home is not None and home.project == bench.project
    (absorbed,) = bench.rows("project_absorbed")
    assert (absorbed["project"], absorbed["into"], absorbed["said"]) == (
        theirs,
        bench.project,
        "merge it",
    )
    assert absorbed["evidence"] == "declaration:agent:mcp"
    again = await bench.admin(action="join_project", id=theirs, said="merge it")
    assert "already this checkout's project" in again["line"]
    back = await bench.admin(action="unjoin_project", id=theirs)
    assert (back["project"], back["was_into"]) == (theirs, bench.project)
    home = bench.model().home(written.id)
    assert home is not None and home.project == theirs
    (unmerged,) = bench.rows("project_unmerged")
    assert (unmerged["project"], unmerged["was_into"]) == (theirs, bench.project)
    with pytest.raises(Exception, match="not merged"):
        await bench.admin(action="unjoin_project", id=theirs)
    with pytest.raises(Exception, match="no project P999"):
        await bench.admin(action="unjoin_project", id="P999")


async def test_join_project_needs_an_id_and_a_checkout(
    bench: Bench, tmp_path: Path
) -> None:
    with pytest.raises(Exception, match="needs `id`"):
        await bench.admin(action="join_project")
    plain = tmp_path / "plain"
    plain.mkdir()
    bench.monkeypatch.chdir(plain)
    with pytest.raises(Exception, match="stands in no checkout"):
        await bench.admin(action="join_project", id=bench.project, said="x")


# ---------------------------------------------------------------------------
# The words the hook keeps
# ---------------------------------------------------------------------------


def test_the_prompt_hook_keeps_the_users_words_per_checkout(bench: Bench) -> None:
    """`prompt_recall` keeps the owner's words, whole, for the checkout it
    resolved; a prompt the harness composed keeps nothing; twenty are
    kept and the oldest leaves."""
    res = recall.prompt_recall(
        prompt="  <system-reminder>x</system-reminder>\nplease join the fork to app, same project  ",
        session_id="s-words",
        cwd=bench.app,
        config=bench.config,
        store=bench.store,
    )
    assert res.surface == "prompt_recall"
    assert recall.WORDS.seen(bench.store.path, bench.checkout)
    assert recall.WORDS.holds(bench.store.path, bench.checkout, "join the fork to app")
    assert not recall.WORDS.holds(bench.store.path, bench.checkout, "system-reminder")
    recall.prompt_recall(
        prompt="<task-notification>done</task-notification>",
        session_id="s-words",
        cwd=bench.app,
        config=bench.config,
        store=bench.store,
    )
    assert not recall.WORDS.holds(bench.store.path, bench.checkout, "task-notification")
    for i in range(recall.WORDS_PER_CHECKOUT):
        recall.WORDS.remember(
            bench.store.path, bench.checkout, f"prompt number {i} here"
        )
    assert not recall.WORDS.holds(bench.store.path, bench.checkout, "join the fork")
    assert recall.WORDS.holds(bench.store.path, bench.checkout, "prompt number 19 here")


def test_a_legacy_question_line_names_the_call_with_its_id(bench: Bench) -> None:
    """Every question line carries the id the agent answers by (L6 here,
    the unresolved form), built from the id the store minted."""
    model = Membership.open(bench.store, location.process_machine())
    pr = model.open_proposal(
        checkout=bench.checkout,
        kind=LEGACY,
        candidates=[bench.project],
        evidence={"unresolved_project": "P900", "unresolved_checkout": "K900"},
        reason="legacy_no_live_checkout",
        line=lambda q: f"question {q} asks",
        distinct="unresolved_project",
    )
    assert pr.line == f"question {pr.id} asks"
    assert bench.model().proposal(pr.id).line == pr.line
    commit(bench.app, "unused")
    git(bench.app, "status")
