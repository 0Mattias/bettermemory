"""The agent's declarations (design 8.8; the owner's ruling of
2026-10-21: nothing in the product waits on a human at a terminal).

Three ``memory_admin`` actions. ``answer_question`` answers an open
membership question, the one named by ``id`` or the caller's checkout's
first: ``separate`` on the agent's own judgement (it keeps every record
where it is), ``join`` only on the user's words, quoted in ``said``.
``join_project`` merges a project the user names into the caller's
checkout's project (a legacy or an imported project whose question
closed as separate, offered again by the user); ``unjoin_project``
undoes a merge. Every declaration is one logged row naming the agent as
its principal (``agent:<harness session>``), and a join carries the
words it rests on.

The words are checked against what the prompt hook saw in the
checkout (`recall.WORDS`): a quote the hook never saw is refused. Where
the hook saw no prompt at all for the checkout (a client without hooks),
the quote is taken as given and the row and the reply say so
(``tethered`` false), since nothing there can check it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .. import identity, location
from ..declare import declaring_model
from ..membership import (
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    GLOBAL,
    OPEN,
    SUBPROJECT_CONTRADICTION,
    Declaration,
    Membership,
    Proposal,
)
from ..recall import WORDS
from ._shared import Context, _advance_turn, request_cwd

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


ANSWERS = ("separate", "join")


def _principal() -> str:
    session = identity.current_harness_session()
    return f"agent:{session}" if session else "agent:mcp"


def _named(model: Membership, project_id: str | None) -> str:
    if project_id is None:
        return "no project"
    if project_id == GLOBAL:
        return GLOBAL
    canon = model.canonical(project_id) or project_id
    try:
        return f'"{model.project(canon).name}" ({canon})'
    except KeyError:
        return canon


def _caller(deps: ToolHandlers) -> tuple[Membership, str | None]:
    """The declaring model and the caller's checkout, resolved where the
    request stands (design 3.1)."""
    session = identity.current_harness_session()
    caller = location.resolve_caller(
        deps.store, deps.config, request_cwd(), session=session
    )
    model = declaring_model(deps.store, deps.config, session=session)
    return model, caller.resolution.checkout


def _question(model: Membership, qid: str | None, checkout: str | None) -> Proposal:
    if qid is not None:
        try:
            pr = model.proposal(qid)
        except KeyError:
            raise ValueError(f"no question {qid}") from None
        if pr.state != OPEN:
            raise ValueError(f"question {qid} is already answered ({pr.answer})")
        return pr
    if checkout is None:
        raise ValueError(
            "no question named and the caller stands in no checkout; pass `id`"
        )
    opened = [
        pr
        for pr in model.pending()
        if pr.checkout == checkout and pr.kind != SUBPROJECT_CONTRADICTION
    ]
    if not opened:
        raise ValueError(f"no open question in checkout {checkout}; pass `id`")
    return opened[0]


def _tether(
    deps: ToolHandlers, checkouts: list[str], said: str | None, what: str
) -> tuple[str, bool]:
    """The user's words a join rests on, and whether the hook saw them."""
    if said is None or not said.strip():
        raise ValueError(
            f"{what} merges two projects and rests on the user's words: pass "
            "`said` quoting the prompt in which the user said so, or answer "
            "`separate`"
        )
    quote = " ".join(said.split())
    seen = [k for k in checkouts if WORDS.seen(deps.store.path, k)]
    if not seen:
        return quote, False
    if any(WORDS.holds(deps.store.path, k, quote) for k in seen):
        return quote, True
    raise ValueError(
        "`said` is not in any prompt the hook saw in this checkout (the last "
        "twenty, whitespace and case folded, eight characters or more): quote "
        "the user's words as they typed them, or answer `separate`"
    )


def _reversal(done_rows: tuple[int, ...], deps: ToolHandlers) -> str | None:
    """How to undo what a join wrote: the project it merged, when it merged
    one (a moved checkout is undone by `bettermemory project new`)."""
    if not done_rows:
        return None
    for row in deps.store.membership_log(
        ["project_merged"], since_seq=min(done_rows) - 1
    ):
        if row.seq in done_rows:
            payload = json.loads(row.payload)
            if isinstance(payload, dict) and payload.get("project"):
                return (
                    f'memory_admin(action="unjoin_project", id="{payload["project"]}")'
                )
    return None


async def answer_question(
    deps: ToolHandlers,
    *,
    id: str | None,
    answer: str | None,
    target: str | None,
    said: str | None,
    ctx: Context | None,
) -> dict[str, Any]:
    _advance_turn(deps.sessions.for_request(ctx), deps.recorder)
    if answer not in ANSWERS:
        raise ValueError('action="answer_question" takes `answer` "separate" or "join"')
    model, mine = _caller(deps)
    pr = _question(model, id, mine)
    if pr.checkout is None:
        raise ValueError(f"question {pr.id} is on no checkout")
    principal = _principal()
    if answer == "separate":
        if pr.kind == SUBPROJECT_CONTRADICTION:
            raise ValueError(
                f"question {pr.id} is about a folder's project file; a part of "
                f"its own writes a file: bettermemory project init --new in "
                f"{pr.evidence.get('dir')}"
            )
        if pr.kind == FILE_CONTRADICTION:
            raise ValueError(
                f"question {pr.id} is about the checkout's project file; keep "
                "the checkout's project with a fresh file: bettermemory project "
                "init --rebind; a project of its own: bettermemory project init "
                "--new"
            )
        if pr.kind == FIRST_SIGHT and _filed(model, pr):
            raise ValueError(
                f"question {pr.id}: the checkout's project file is bound to "
                "another project; a project of its own writes a fresh file: "
                "bettermemory project init --new"
            )
        done = model.declare(
            Declaration("new", principal, checkout=pr.checkout, proposal=pr.id)
        )
        location.forget()
        return {
            "question": pr.id,
            "answer": "separate",
            "project": done.project,
            "line": (
                f"question {pr.id} answered: separate; checkout {pr.checkout} is "
                f"project {_named(model, done.project)}; nothing moved"
            ),
            "tethered": None,
        }
    named = {model.canonical(c) or c for c in pr.candidates}
    imported = pr.evidence.get("imported_project")
    if isinstance(imported, str):
        named.add(model.canonical(imported) or imported)
    chosen = model.canonical(target) if target else None
    if chosen is None:
        if len(named) != 1:
            raise ValueError(
                f"question {pr.id} names {len(named)} projects; pass `target`"
            )
        chosen = next(iter(named))
    elif chosen not in named:
        raise ValueError(
            f"question {pr.id} does not offer {_named(model, chosen)}; it names "
            + ", ".join(_named(model, c) for c in sorted(named))
        )
    quote, tethered = _tether(
        deps, [k for k in (pr.checkout, mine) if k], said, f"question {pr.id}'s join"
    )
    done = model.declare(
        Declaration(
            "join",
            principal,
            checkout=pr.checkout,
            proposal=pr.id,
            target=chosen,
            said=quote,
        )
    )
    location.forget()
    return {
        "question": pr.id,
        "answer": f"join:{chosen}",
        "project": done.project,
        "line": (
            f"question {pr.id} answered: join {_named(model, chosen)}; checkout "
            f"{pr.checkout} is project {_named(model, done.project)}"
        ),
        "said": quote,
        "tethered": tethered,
        "reversal": _reversal(done.rows, deps),
    }


def _filed(model: Membership, pr: Proposal) -> bool:
    """Whether the checkout's root project file is bound to a project other
    than the checkout's own (E22 e, as `cli/project_cmd._filed_root` reads
    it): then a separate project needs a fresh file, which only
    ``bettermemory project init --new`` writes."""
    from pathlib import Path

    from .. import project_file

    if pr.checkout is None:
        return False
    k = model.checkout(pr.checkout)
    views = k.views_on(model.machine)
    if not views:
        return False
    path = Path(views[-1].path) / project_file.PROJECT_FILE_NAME
    try:
        found = project_file.parse(path) if path.exists() else None
    except OSError:
        return False
    if not isinstance(found, project_file.ProjectFile):
        return False
    bound = model.file_binding(found.id)
    return bound is not None and bound != (model.canonical(k.project) or k.project)


async def join_project(
    deps: ToolHandlers,
    *,
    id: str | None,
    said: str | None,
    ctx: Context | None,
) -> dict[str, Any]:
    _advance_turn(deps.sessions.for_request(ctx), deps.recorder)
    if id is None or not id.strip():
        raise ValueError('action="join_project" needs `id`, the project to merge')
    model, mine = _caller(deps)
    if mine is None:
        raise ValueError("the caller stands in no checkout; nothing to merge into")
    src = model.canonical(id)
    if src is None:
        raise ValueError(f"no project {id}")
    k = model.checkout(mine)
    into = model.canonical(k.project) or k.project
    if src == into:
        return {
            "project": into,
            "line": f"{_named(model, src)} is already this checkout's project",
            "tethered": None,
        }
    quote, tethered = _tether(deps, [mine], said, f"joining {_named(model, src)}")
    model.declare(
        Declaration("absorb", _principal(), checkout=mine, target=src, said=quote)
    )
    location.forget()
    return {
        "project": into,
        "merged": src,
        "line": (
            f"{_named(model, src)} is merged into {_named(model, into)}; its "
            "records are delivered here from now on"
        ),
        "said": quote,
        "tethered": tethered,
        "reversal": f'memory_admin(action="unjoin_project", id="{src}")',
    }


async def unjoin_project(
    deps: ToolHandlers, *, id: str | None, ctx: Context | None
) -> dict[str, Any]:
    _advance_turn(deps.sessions.for_request(ctx), deps.recorder)
    if id is None or not id.strip():
        raise ValueError('action="unjoin_project" needs `id`, the merged project')
    model, _mine = _caller(deps)
    try:
        p = model.project(id)
    except KeyError:
        raise ValueError(f"no project {id}") from None
    was = p.merged_into
    if was is None:
        raise ValueError(f"{_named(model, p.id)} is not merged into another project")
    model.declare(Declaration("unjoin", _principal(), target=p.id))
    location.forget()
    return {
        "project": p.id,
        "was_into": was,
        "line": (
            f'"{p.name}" ({p.id}) is a project of its own again, no longer merged '
            f"into {_named(model, was)}"
        ),
    }


__all__ = ["ANSWERS", "answer_question", "join_project", "unjoin_project"]
