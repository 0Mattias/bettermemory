"""Declarations of `membership.py` (design 8.8) and the B6 writes.

`declare` applies a statement that outranks evidence: a join or a
separation with a question open or none, an ``init`` at or below a
checkout's root, a move, an absorb or an unjoin. `unresolved_home`,
`session_resolution` and `_settle_unresolved` home and later settle the
writes made where nothing could be read (decision B6).

Each function takes the model as `self: Membership` and reaches its state
(`_store`, `_rows`, `_remint`) and core methods through it; the class
binds every one by name. This module lives apart from `membership.py` to
keep each module under the package's 2,000-line bound (ENG-96). It
imports the standard library, `location`, `store` and the records and
lines modules, and `membership` only under TYPE_CHECKING.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from .location import NO_MACHINE
from .membership_lines import line_unresolved
from .membership_records import (
    ASK,
    DEFERRED,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    FOLDER,
    GLOBAL,
    HB,
    HF,
    IMPORT,
    INDETERMINATE,
    LEGACY,
    OPEN,
    REMOTE_DIVERGED,
    SILENT,
    SUBPROJECT_CONTRADICTION,
    UNREADABLE,
    UNRESOLVED,
    UNRESOLVED_LOCATION,
    Checkout,
    Declaration,
    Declared,
    F,
    H,
    Location,
    Project,
    Proposal,
    R,
    Resolution,
    View,
    _COMMANDS,
    _INITS,
    _basename,
    _dir_key,
    _inside,
    _norm,
)
from .store import RecordHome

if TYPE_CHECKING:
    from .membership import Membership


# -- declarations (design 8.8) ---------------------------------------------------------


def declare(self: Membership, decl: Declaration) -> Declared:
    """Apply a declaration (design 8.8). ``join`` and ``new`` answer
    the open proposal named (or the checkout's first open one) or,
    with none, join or separate the checkout itself; ``init``,
    ``init_new`` and ``init_rebind`` bind the file id the command
    wrote, at the checkout's root to the checkout's project and below
    it to a part (decision B17); ``move`` re-homes one record;
    ``absorb`` merges the project named into the checkout's; ``unjoin``
    undoes the merge of the project named. Each is logged with the
    principal, and a join with the user's words it rests on."""
    with self._call():
        before = {pr.id for pr in self._open_proposals()}
        project = self._declare(decl)
        if not before - {pr.id for pr in self._open_proposals()}:
            self._declared(decl, project)
        rows = tuple(self._rows)
        remint = self._remint
    return Declared(
        action=decl.action,
        proposal=decl.proposal,
        project=project,
        rows=rows,
        remint_checkout=remint,
    )


def _declared(self: Membership, decl: Declaration, project: str | None) -> None:
    """The row of a join or a separation that answered no question,
    naming the principal (decision D6; design 3.2 rung 1, a
    declaration decides the checkout's project). An answer names the
    principal in its ``proposal_answered`` row, and the bindings and
    moves of the other declarations in their own rows' evidence; an
    ``init_new`` below the root leaves the checkout where it is, and
    its part's rows (minted, bound) name the declaration."""
    if decl.action == "init_new" and decl.checkout is not None:
        if self._declared_below(decl, self.checkout(decl.checkout)):
            return
    if decl.action == "join":
        payload: dict[str, Any] = {
            "checkout": decl.checkout,
            "project": project,
            "witnesses": ["declaration"],
            "principal": decl.principal,
            "proposal": decl.proposal,
        }
        if decl.said is not None:
            payload["said"] = decl.said
        self._event("resolution_joined", **payload)
    elif decl.action in ("new", "init_new"):
        self._event(
            "resolution_new",
            checkout=decl.checkout,
            project=project,
            principal=decl.principal,
            proposal=decl.proposal,
            reused_path=False,
        )


def _question_of(
    self: Membership, decl: Declaration, k: Checkout, below: bool
) -> Proposal | None:
    """The open question a declaration naming none answers. A part's
    question (L7) is answered in that folder: `project init --new`
    below the root answers only the one about its own folder, and
    neither `project new` nor `project init --new` at the root answers
    one. With no directory, `init --new` answers the checkout's first."""
    opened = self._open_proposals(k.id)
    if decl.action == "new" or (
        decl.action == "init_new" and decl.directory is not None and not below
    ):
        opened = [x for x in opened if x.kind != SUBPROJECT_CONTRADICTION]
    elif decl.action == "init_new" and below:
        assert decl.directory is not None
        folder = _dir_key(decl.directory)
        opened = [
            x
            for x in opened
            if x.kind == SUBPROJECT_CONTRADICTION
            and _dir_key(str(x.evidence.get("dir") or "")) == folder
        ]
    return opened[0] if opened else None


def _declare(self: Membership, decl: Declaration) -> str | None:
    by = f"declaration:{decl.principal}"
    if decl.action == "move":
        if decl.record is None or decl.target is None:
            raise ValueError("memory move names a record and a project")
        return self._move_record(decl.record, decl.target, by)
    if decl.action == "unjoin":
        if decl.target is None:
            raise ValueError("unjoin names a project")
        return self._unmerge(decl.target, by)
    if decl.checkout is None:
        raise ValueError(f"{decl.action} is made in a checkout")
    k = self.checkout(decl.checkout)
    if decl.action == "absorb":
        if decl.target is None:
            raise ValueError("absorb names a project")
        return self._absorb(k, decl.target, by, said=decl.said)
    below = self._declared_below(decl, k)
    pr: Proposal | None = None
    if decl.proposal is not None:
        pr = self.proposal(decl.proposal)
        if pr.state != OPEN:
            raise ValueError(f"proposal {pr.id} is already answered")
    elif decl.action in ("join", "new", "init_new"):
        pr = self._question_of(decl, k, below)
    if decl.action == "join":
        if pr is not None:
            return self._answer_join(pr, k, decl, by)
        if decl.target is None:
            raise ValueError("project join names a project")
        return self._join_checkout(k, decl.target, by)
    if decl.action == "new":
        if pr is not None:
            return self._answer_new(pr, k, decl, by)
        return self._split(k, decl.name or self._checkout_name(k), by)
    if decl.action in _INITS:
        if decl.file_id is None:
            raise ValueError(f"{decl.action} names the file id it wrote")
        if decl.action == "init_new" and pr is not None:
            # The question's "separate" answer, the file the command
            # wrote bound to the checkout's project (E22 e), or below
            # the root to a part of its own (L7, E35 g).
            return self._answer_new(pr, k, decl, by)
        if below:
            return self._declare_below_root(decl, k, by)
        pid = self._canon(k.project)
        if decl.action == "init_new":
            # No question open: the checkout and the new file become a
            # project of their own.
            pid = self._split(k, decl.name or self._checkout_name(k), by)
        self._bind_file(
            decl.file_id,
            pid,
            evidence=f"{by}: bettermemory project {decl.action}",
            boundary=decl.boundary,
        )
        if decl.action == "init_rebind":
            if not decl.boundary:
                # The fresh file sets the project's boundary (B12).
                self._lift_boundary(pid, decl.outer_tenant)
            for x in self._open_proposals(k.id):
                if x.kind == FILE_CONTRADICTION:
                    self._close(x, "rebind", by)
        return pid
    raise ValueError(f"unknown declaration {decl.action!r}")


def _declare_below_root(
    self: Membership, decl: Declaration, k: Checkout, by: str
) -> str:
    """`bettermemory project init [--new | --rebind]` in a folder below
    the checkout's root (decision B17; design 2.1, a file below the
    root names a part, D13): the file names a part of the project
    enclosing that folder, never the checkout, which keeps its project.
    ``init_rebind`` binds the fresh file to the part the replaced file
    named (its boundary set from the fresh file, B12), or makes a new
    part when that file named none; ``init`` and ``init_new`` with no
    question of that folder open make a new part."""
    assert decl.file_id is not None
    part = (
        self._file_binding(decl.replaced)
        if decl.action == "init_rebind" and decl.replaced
        else None
    )
    if part is None:
        return self._declare_subproject(decl, k, by)
    self._bind_file(
        decl.file_id,
        part,
        evidence=f"{by}: bettermemory project init --rebind in {decl.directory}",
        boundary=decl.boundary,
        checkout=k.id,
        directory=decl.directory,
    )
    if not decl.boundary:
        self._lift_boundary(part, decl.outer_tenant)
    return part


def _declare_subproject(
    self: Membership, decl: Declaration, k: Checkout, by: str
) -> str:
    """A new part for a folder below the checkout's root (decision D13;
    design 2.1, a project file below the root names a sub-project): a
    sub-project of ``decl.target``, the project enclosing that folder
    as the caller's resolution there found it, else of the checkout's
    project, in its tenant, with the file the command wrote bound to
    it, as the first sight of a nested file binds one (X5). Below a
    folder project's root the part is a folder project inside it
    (I31), registered at its folder's first sight
    (`_declared_folder`). The checkout keeps its project."""
    parent = self.canonical(decl.target) if decl.target is not None else None
    if parent is None or parent == GLOBAL or parent not in self._all_projects():
        parent = self._canon(k.project)
    assert decl.file_id is not None
    sp = self._mint_project(
        decl.name or _basename(decl.directory or "") or self._checkout_name(k),
        reason="declared_subproject",
        parent=parent,
        tenant=self.project(parent).tenant,
        folder=k.kind == FOLDER,
    )
    self._bind_file(
        decl.file_id,
        sp.id,
        evidence=f"{by}: bettermemory project {_COMMANDS[decl.action]} in "
        f"{decl.directory}",
        boundary=decl.boundary,
        checkout=k.id,
        directory=decl.directory,
    )
    return sp.id


def _answer_join(
    self: Membership, pr: Proposal, k: Checkout, decl: Declaration, by: str
) -> str | None:
    if pr.kind == LEGACY and pr.evidence.get("unresolved_project"):
        return self._answer_unresolved(pr, k, decl, by, join=True)
    if pr.kind == IMPORT:
        return self._answer_import(pr, k, decl, by, join=True)
    mine = self._canon(k.project)
    target = self._canon(decl.target or pr.candidates[0])
    if pr.kind == REMOTE_DIVERGED and target == mine:
        self._add_remotes(mine, pr.evidence.get("new", ()))
    elif pr.kind == SUBPROJECT_CONTRADICTION:
        pass  # that folder is the other project: read by `_nested_answer`
    elif pr.kind == FILE_CONTRADICTION:
        self._move_checkout(k, target, by)
    else:
        evidence = set(pr.evidence.get(target) or ())
        shallow = (
            bool(pr.signature) and bool(pr.signature[-1]) if pr.signature else False
        )
        if (
            pr.kind == FIRST_SIGHT
            and shallow
            and R in evidence
            and not ({H, HF, HB, F} & evidence)
        ):
            # A question that rested on the remote alone in a shallow
            # clone: the answer declares that remote the project's for
            # shallow clones (witness D, D18).
            for key in k.remotes:
                self._put(
                    "declared_remote",
                    {
                        "remote_key": key,
                        "project": target,
                        "principal": decl.principal,
                    },
                )
                self._event(
                    "remote_declared",
                    remote=key,
                    project=target,
                    principal=decl.principal,
                )
        self._merge(mine, target, evidence=by)
    self._close(pr, f"join:{target}", by, said=decl.said)
    return target


def _answer_new(
    self: Membership, pr: Proposal, k: Checkout, decl: Declaration, by: str
) -> str | None:
    if pr.kind == LEGACY and pr.evidence.get("unresolved_project"):
        return self._answer_unresolved(pr, k, decl, by, join=False)
    if pr.kind == IMPORT:
        return self._answer_import(pr, k, decl, by, join=False)
    mine = self._canon(k.project)
    if pr.kind == SUBPROJECT_CONTRADICTION:
        # `bettermemory project init --new` in that folder wrote a file
        # with an id of its own: a new sub-project of the enclosing one.
        if decl.file_id is None:
            raise ValueError("project init --new names the file id it wrote")
        parent = self._canon(str(pr.evidence.get("parent") or mine))
        name = (
            decl.name
            or str(pr.evidence.get("name") or "")
            or _basename(str(pr.evidence.get("dir") or ""))
        )
        sp = self._mint_project(
            name,
            reason="declared_subproject",
            parent=parent,
            tenant=self.project(parent).tenant,
        )
        self._bind_file(
            decl.file_id,
            sp.id,
            evidence=f"{by}: bettermemory project init --new in "
            f"{pr.evidence.get('dir')}",
            boundary=decl.boundary,
            checkout=k.id,
            directory=str(pr.evidence.get("dir") or "") or None,
        )
        self._close(pr, "new", by)
        return sp.id
    if pr.kind == REMOTE_DIVERGED:
        self._close(pr, "new", by)
        mine = self._split(k, decl.name or self._checkout_name(k), by)
        if decl.file_id is not None:
            self._bind_file(
                decl.file_id,
                mine,
                evidence=f"{by}: bettermemory project init --new",
                boundary=decl.boundary,
            )
        return mine
    p = self.project(mine)
    if decl.file_id is not None:
        self._bind_file(
            decl.file_id,
            mine,
            evidence=f"{by}: bettermemory project init --new",
            boundary=decl.boundary,
        )
    self._close(pr, "new", by)
    if p.provisional is not None:
        # The checkout's provisional project becomes its own, with a
        # token of its own, so it is not asked again (E22 a).
        self._put_project(dataclasses.replace(p, provisional=None))
        self._give_token(k)
    return mine


def _rehome_checkout(
    self: Membership, k: Checkout, target: str, by: str, *, parts: Collection[str] = ()
) -> None:
    """A declared move of a checkout: the checkout row and its records'
    homes name the target, each a logged row naming the declaration.
    A record homed in one of `parts`, the parts the checkout takes
    along (B10), stays in its part."""
    self._put_checkout(dataclasses.replace(k, project=target))
    for row in self._store.membership_rows("homes", where={"checkout": k.id}):
        if self._canon(str(row["project"])) in parts:
            continue
        self._put("home", {**row, "project": target, "rule": f"{row['rule']}|{by}"})


def _move_checkout(self: Membership, k: Checkout, target: str, by: str) -> None:
    """A declared move of a checkout to another project, its parts with
    it (B10)."""
    parts = self.parts_in(k.id)
    dst = self._canon(target)
    self._rehome_checkout(k, dst, by, parts={part.id for part, _f, _d in parts})
    self._reparent_parts(k, parts, dst, by, "project join")


def _join_checkout(self: Membership, k: Checkout, target: str, by: str) -> str:
    """``project join`` with no question open: the checkout's project
    merged into the target when the checkout is its only one, else the
    checkout moved there."""
    mine, dst = self._canon(k.project), self._canon(target)
    if mine == dst:
        return dst
    if all(x.id == k.id for x in self._checkouts_of(mine)):
        self._merge(mine, dst, evidence=by)
    else:
        self._move_checkout(k, dst, by)
    return dst


def _absorb(
    self: Membership, k: Checkout, target: str, by: str, *, said: str | None = None
) -> str:
    """The agent's ``join_project``: project `target` (a legacy or an
    imported project with no live checkout, whose question was closed
    as separate, or any project the user names) merged into the
    checkout's project, so its records are delivered here from now on;
    one project row and one ``project_absorbed`` row naming the
    declaration and the user's words. Reversed by ``unjoin``."""
    mine, src = self._canon(k.project), self._canon(target)
    if src == mine:
        return mine
    self._merge(src, mine, evidence=by)
    self._event(
        "project_absorbed",
        project=src,
        into=mine,
        checkout=k.id,
        evidence=by,
        said=said,
    )
    return mine


def _unmerge(self: Membership, target: str, by: str) -> str:
    """The agent's ``unjoin_project``: the merge of project `target`
    undone, its records its own again; one project row and one
    ``project_unmerged`` row. A project not merged into another
    raises ValueError."""
    p = self.project(target)
    if p.merged_into is None:
        raise ValueError(f"project {target} is not merged into another")
    was = p.merged_into
    self._put_project(dataclasses.replace(p, merged_into=None))
    self._event("project_unmerged", project=p.id, was_into=was, evidence=by)
    return p.id


def _split(self: Membership, k: Checkout, name: str, by: str) -> str:
    """``project new``: the checkout and what was written there become
    a project of their own, and the checkout gets a token of its own."""
    old = self._canon(k.project)
    parts = self.parts_in(k.id)
    p = self._mint_project(
        name, reason="declared_split", tenant=self.project(old).tenant
    )
    self._rehome_checkout(k, p.id, by, parts={part.id for part, _f, _d in parts})
    self._reparent_parts(k, parts, p.id, by, "project new")
    if self.project(old).provisional and not self._checkouts_of(old):
        self._merge(old, p.id, evidence=f"the placeholder of checkout {k.id}, {by}")
    self._give_token(self.checkout(k.id))
    return p.id


def parts_in(
    self: Membership, checkout_id: str
) -> list[tuple[Project, str, str | None]]:
    """The sub-projects of the checkout's project whose files were bound
    inside that checkout (a first sight there, or `project init`
    there), each with its file id and folder: what `project new` and a
    `project join` that moves the checkout take along (B10). A part
    first bound in another checkout stays with the project that holds
    it (one file id binds one project); the next session in that part
    here asks once, L7, whose two commands settle it."""
    k = self.checkout(checkout_id)
    mine = self._canon(k.project)
    out: list[tuple[Project, str, str | None]] = []
    seen: set[str] = set()
    for row in self._store.membership_log(["project_file_bound"]):
        payload = json.loads(row.payload)
        if not isinstance(payload, dict) or payload.get("checkout") != k.id:
            continue
        file_id = str(payload.get("file_id") or "")
        bound = self._file_binding(file_id) if file_id else None
        if bound is None or bound in seen:
            continue
        seen.add(bound)
        part = self.project(bound)
        if self.canonical(part.parent) == mine:
            folder = payload.get("dir")
            out.append((part, file_id, str(folder) if folder else None))
    return out


def _reparent_parts(
    self: Membership,
    k: Checkout,
    parts: Sequence[tuple[Project, str, str | None]],
    new: str,
    by: str,
    command: str,
) -> None:
    """B10: the checkout's parts (`parts_in`, read before the checkout
    left its project) become parts of `new`, so a later session in a
    part resolves to it silently. Each is a logged project row and a
    ``project_reparented`` row naming the declaration (B13)."""
    for part, file_id, folder in parts:
        old = self.canonical(part.parent)
        self._put_project(dataclasses.replace(self.project(part.id), parent=new))
        self._event(
            "project_reparented",
            project=part.id,
            parent=new,
            previous=old,
            file_id=file_id,
            dir=folder,
            in_checkout=k.id,
            evidence=f"{by}: bettermemory {command} in checkout {k.id}",
        )


def _move_record(self: Membership, record_id: str, target: str, by: str) -> str:
    rows = self._store.membership_rows("homes", where={"record_id": record_id})
    if not rows:
        raise KeyError(record_id)
    dst = target if target == GLOBAL else self._canon(target)
    if dst != GLOBAL:
        self.project(dst)
    row = rows[0]
    self._put("home", {**row, "project": dst, "rule": f"{row['rule']}|{by}:move"})
    return dst


# -- writes where nothing could be read (decision B6) --------------------------------


def unresolved_home(
    self: Membership, loc: Location, *, session: str | None
) -> RecordHome:
    """The home of a write made where the resolution was INDETERMINATE
    (nothing readable) or DEFERRED (an evicted file at first sight).
    The write is neither refused nor made global: it lands in a
    provisional project of its own (reason ``unresolved_location``,
    provisional ``indeterminate`` or ``deferred``), on a checkout of
    kind ``unresolved`` keyed on the machine and the cwd as given,
    which every later write there finds again until a resolution at
    that path settles it (`resolve`). `session` is the writing
    session (``loc.session`` when None)."""
    if loc.state == UNREADABLE:
        state = INDETERMINATE
    elif loc.evicted:
        state = DEFERRED
    else:
        raise ValueError(
            f"{loc.cwd} was read: a write there takes its resolution's home"
        )
    who = session if session is not None else loc.session
    with self._call():
        k = self._unresolved_at(loc.machine, loc.cwd)
        if k is None:
            p = self._mint_project(
                _basename(loc.cwd), reason=UNRESOLVED_LOCATION, provisional=state
            )
            k = self._register_unresolved(loc, p.id, state, who)
        elif who is not None and who not in k.sessions:
            k = self._put_checkout(dataclasses.replace(k, sessions=(*k.sessions, who)))
    return RecordHome(k.project, k.id, f"unresolved:{state}")


def _register_unresolved(
    self: Membership, loc: Location, project_id: str, state: str, session: str | None
) -> Checkout:
    kid = self._store.next_membership_id("checkouts", "K")
    stamp = self._stamp()
    view = View(
        checkout=kid,
        machine=loc.machine,
        path=_norm(loc.cwd),
        first_seen=stamp,
        last_seen=stamp,
        order=self._store.next_membership_number("checkout_views", "seen_order"),
    )
    k = Checkout(
        id=kid,
        project=project_id,
        machine=loc.machine,
        kind=UNRESOLVED,
        object_key=None,
        common_object_key=None,
        git_dir=None,
        token=None,
        token_status="n/a",
        stamps=(),
        last_head=None,
        branch=None,
        remotes=(),
        manifest=None,
        legacy=False,
        retired=False,
        registered=stamp,
        views=(view,),
        sessions=(session,) if session is not None else (),
    )
    self._put_checkout(k)
    self._put_view(view)
    self._event(
        "checkout_registered",
        checkout=kid,
        project=project_id,
        path=view.path,
        machine=loc.machine,
        checkout_kind=UNRESOLVED,
        rule=f"unresolved:{state}",
        token="n/a",
        session=session,
        detail=loc.detail,
    )
    return k


def _unresolved_at(self: Membership, machine: str, path: str) -> Checkout | None:
    """The unsettled ``unresolved`` checkout at `path` on `machine`."""
    found = [
        k
        for k in (
            self.checkout(str(v["checkout"]))
            for v in self._store.membership_rows(
                "checkout_views", where={"machine": machine, "path": _norm(path)}
            )
        )
        if k.kind == UNRESOLVED and k.replaced_by is None
    ]
    return max(found, key=lambda k: k.registered) if found else None


def _unresolved_of(self: Membership, session: str, path: str) -> list[Checkout]:
    """The unsettled ``unresolved`` checkouts at `path` on any machine
    that `session` wrote at (decision B24)."""
    return [
        k
        for k in (
            self.checkout(str(v["checkout"]))
            for v in self._store.membership_rows(
                "checkout_views", where={"path": _norm(path)}
            )
        )
        if k.kind == UNRESOLVED and k.replaced_by is None and session in k.sessions
    ]


def session_resolution(
    self: Membership, loc: Location, prior: Resolution, at: str
) -> Resolution | None:
    """A write where nothing could be read, by a harness session whose
    latest hook resolution (`prior`, made at `at`) has a checkout with a
    view at that path or above it (decision B24): the resolution of that
    checkout, rule ``session_resolved``, SILENT, with one
    ``resolution_session`` row, so the write takes its home there and no
    provisional project is minted. At or below `at` the project is the
    one the hook resolved (a part where a nested file named one);
    elsewhere in the checkout, the checkout's project. None when the
    session's checkout has no view holding the path."""
    if (
        loc.state != UNREADABLE
        or loc.session is None
        or prior.checkout is None
        or prior.project in (None, GLOBAL)
    ):
        return None
    with self._call():
        try:
            k = self.checkout(prior.checkout)
        except KeyError:
            return None
        held = any(_inside(loc.cwd, v.path) for v in k.views_on(k.machine))
        if k.kind == UNRESOLVED or not held:
            return None
        hooked = [self._canon(c) for c in prior.chain]
        if _inside(loc.cwd, at) and prior.project is not None:
            project = self._canon(prior.project)
            chain = list(dict.fromkeys([project, *hooked]))
        else:
            project = self._canon(k.project)
            own = self.chain_of(project)
            around = hooked[len(self.chain_of(prior.project)) :]
            chain = list(dict.fromkeys([*own, *around]))
        tenant = next((c for c in chain if self.project(c).boundary), prior.tenant)
        self._event(
            "resolution_session",
            checkout=k.id,
            project=project,
            session=loc.session,
            path=loc.cwd,
        )
        rows = tuple(self._rows)
    return Resolution(
        outcome=SILENT,
        project=project,
        checkout=k.id,
        rule="session_resolved",
        chain=tuple(chain),
        tenant=tenant,
        rows=rows,
    )


def _settle_unresolved(self: Membership, loc: Location, res: Resolution) -> Resolution:
    """A resolution ending with a checkout K at a path written at while
    nothing could be read there (its root or its cwd): the session that
    wrote there merges those writes into K's project and the unresolved
    checkout is replaced; any other session, or no session, meets a
    ``legacy`` question in K while the writes stay apart (decision B6).
    A write by a process with no machine identity registered its path
    under `location.NO_MACHINE`; a resolution on any machine finds it
    there too (decision B16). One the resolving session wrote is found
    under any machine, the session proving the namespace (a container
    client's path, written at through the daemon's machine; decision
    B24)."""
    if res.checkout is None:
        return res
    paths = {_norm(loc.cwd)}
    if loc.root:
        paths.add(_norm(loc.root))
    found: dict[str, Checkout] = {}
    for path in sorted(paths):
        for machine in dict.fromkeys((loc.machine, NO_MACHINE)):
            u = self._unresolved_at(machine, path)
            if u is not None:
                found[u.id] = u
        if loc.session is not None:
            for u in self._unresolved_of(loc.session, path):
                found.setdefault(u.id, u)
    unsettled = list(found.values())
    if not unsettled:
        return res
    k = self.checkout(res.checkout)
    target = self._canon(k.project)
    lines = list(res.lines)
    outcome, proposal = res.outcome, res.proposal
    for u in unsettled:
        mine = self._canon(u.project)
        if mine == target or (loc.session is not None and loc.session in u.sessions):
            self._merge(
                mine,
                target,
                evidence="written while the path could not be read; the same "
                "session resolved it at the next prompt",
            )
            self._put_checkout(dataclasses.replace(u, replaced_by=k.id))
            continue
        memories, episodes = self._record_counts(mine)
        if memories + episodes == 0:
            continue
        opened = {x.id for x in self._open_proposals(k.id)}
        path = u.newest_view.path if u.newest_view is not None else loc.cwd
        pr = self._open_proposal(
            checkout=k.id,
            kind=LEGACY,
            candidates=[target],
            evidence={
                "unresolved_project": mine,
                "unresolved_checkout": u.id,
                "path": path,
                "memories": memories,
                "episodes": episodes,
                "sessions": list(u.sessions),
            },
            reason=UNRESOLVED_LOCATION,
            line=self._unresolved_line(path, memories, episodes, target),
            signature=None,
            distinct="unresolved_project",
        )
        if pr.id not in opened and pr.line not in lines:
            # Named as every open question of the checkout is (`_caller`).
            lines.append(pr.line)
            outcome = ASK if outcome == SILENT else outcome
            proposal = proposal or pr.id
    return dataclasses.replace(
        res, lines=tuple(lines), outcome=outcome, proposal=proposal
    )


def _unresolved_line(
    self: Membership, path: str, memories: int, episodes: int, target: str
) -> Callable[[str], str]:
    """The B6 question's line, built from the question's id."""

    def line(q: str) -> str:
        return line_unresolved(
            path, memories, episodes, self.project(target).name, target, question=q
        )

    return line


def _answer_unresolved(
    self: Membership,
    pr: Proposal,
    k: Checkout,
    decl: Declaration,
    by: str,
    *,
    join: bool,
) -> str:
    """The answers to a B6 ``legacy`` question: ``join`` merges the
    writes' project into the project named (the checkout's by
    default); ``new`` makes it a project of its own with no live
    checkout. Either way the unresolved checkout is settled."""
    unresolved = self._canon(str(pr.evidence["unresolved_project"]))
    if join:
        target = self._canon(decl.target or k.project)
        self._merge(unresolved, target, evidence=by)
        answer, result = f"join:{target}", target
    else:
        p = self.project(unresolved)
        self._put_project(
            dataclasses.replace(p, provisional=None, name=decl.name or p.name)
        )
        answer, result = "new", unresolved
    u = self.checkout(str(pr.evidence["unresolved_checkout"]))
    if u.replaced_by is None:
        self._put_checkout(dataclasses.replace(u, replaced_by=k.id))
    self._close(pr, answer, by, said=decl.said)
    return result


def _answer_import(
    self: Membership,
    pr: Proposal,
    k: Checkout,
    decl: Declaration,
    by: str,
    *,
    join: bool,
) -> str:
    """The answers to an ``import`` question (design 8.7, line L10):
    ``join`` merges the imported project into the project named (the
    checkout's when none is, or when the imported project itself is
    named); ``new`` keeps it a project of its own. The other questions
    about the same imported project close with the same answer."""
    imported_id = str(pr.evidence.get("imported_project") or "")
    imported = self._canon(imported_id)
    mine = self._canon(k.project)
    if join:
        target = self._canon(decl.target) if decl.target else mine
        if target == imported:
            target = mine
        self._merge(imported, target, evidence=f"{by}: import question {pr.id}")
        answer, result = f"join:{target}", target
    else:
        p = self.project(imported)
        if p.provisional is not None:
            self._put_project(dataclasses.replace(p, provisional=None))
        answer, result = "new", mine
    self._close(pr, answer, by)
    for other in self._open_proposals():
        if other.kind == IMPORT and other.evidence.get("imported_project") == (
            imported_id
        ):
            self._close(other, answer, f"{by} (question {pr.id})")
    return result
