"""`bettermemory project`: the projects membership knows, and the
declarations that settle them (RS-F6; design 2.1, 3.4, 8.8 and 8.10;
decisions D6, D14 and D18).

`list`, `show`, `why` and `pending` read. `join`, `new` and `init` are
declarations (decision D6): each refuses unless stdin is a terminal,
prints what it is about to do, asks for a name to be typed exactly (the
target project's for `join`, the folder's display name for `new` and
`init`) and only then calls `Membership.declare` with the OS user as the
principal. A piped stdin or a wrong name exits 2 with one line and
changes nothing. The installer also denies these commands to Claude Code
(`init.patch_claude_code_permissions`), so an agent's shell cannot run
them. `init` is the one caller of `project_file.write`, the only file
bettermemory writes into a working tree (design 1.1). Where it writes
decides what the file names (design 2.1, decision D13): at a repository's
root, the repository's project; below the root, a new sub-project of the
project enclosing that folder; in a plain folder, a folder project. A file
with `boundary = true` makes the project it binds a tenant wherever it lies
(B9); `--rebind` sets the project's boundary from the fresh file (B12).

A checkout that leaves its project (`new`, or a `join` that moves it)
takes along the parts whose files were bound inside it (B10). A part whose
file was first bound in another checkout stays with the project that holds
it, since one file id binds one project: the next session in that part
here asks once (L7), naming `project join <id>` and `project init --new`
in that folder. `new` refuses on a checkout whose root file is bound,
which `project init --new` answers with a fresh file (B11).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from ..membership import (
        Checkout,
        Declared,
        Membership,
        Project,
        Proposal,
        Resolution,
    )
    from ._common import CliContext


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``project`` and its commands."""
    help_text = (
        "The projects membership knows (list, show, why, pending) and the "
        "declarations that settle a checkout's project (join, new, init). "
        "Declarations need a terminal and the project's name typed to confirm."
    )
    parser = sub.add_parser("project", help=help_text, description=help_text)
    project_sub = parser.add_subparsers(dest="project_cmd")

    text = (
        "Every project with its id and name, its checkouts and their live "
        "views; provisional and unresolved projects marked."
    )
    project_sub.add_parser("list", help=text, description=text)

    text = (
        "One project: its witness commits, remotes, file ids, checkouts and "
        "open questions."
    )
    show = project_sub.add_parser("show", help=text, description=text)
    show.add_argument("id", metavar="ID", help="The project id (P001).")

    text = (
        "Why a record is where it is: its project, the checkout it was written "
        "in, the rule that decided it and every log row behind it."
    )
    why = project_sub.add_parser("why", help=text, description=text)
    why.add_argument("record", metavar="RECORD_ID", help="A memory or episode id.")

    text = (
        "Every open question with its line, which names the two commands that "
        "answer it, and the checkout to run them in."
    )
    project_sub.add_parser("pending", help=text, description=text)

    text = (
        "Declare this checkout part of project ID: answers the checkout's open "
        "question, or with none moves the checkout there. Needs a terminal and "
        "the project's name typed to confirm."
    )
    join = project_sub.add_parser("join", help=text, description=text)
    join.add_argument("id", metavar="ID", help="The project id (P001).")

    text = (
        "Declare this checkout a project of its own: answers the checkout's open "
        "question with 'separate', or with none takes the checkout and what was "
        "written there out of its project, and re-mints its checkout token. "
        "Needs a terminal and the folder's name typed to confirm."
    )
    project_sub.add_parser("new", help=text, description=text)

    text = (
        "Write bettermemory.toml in this directory (the only file bettermemory "
        "writes into a working tree; commit it). At a repository's root it is "
        "bound to the repository's project; below the root it names a new "
        "sub-project of the project around this folder (the checkout keeps its "
        "own); in a plain folder it makes a folder project. --new gives the file "
        "a project of its own (at the root the checkout's too; below it a "
        "sub-project with an id of its own); --rebind replaces an existing file "
        "with a new id bound to the project it named. Needs a terminal and the "
        "folder's name typed."
    )
    init = project_sub.add_parser("init", help=text, description=text)
    mode = init.add_mutually_exclusive_group()
    mode.add_argument(
        "--new",
        action="store_true",
        help="A project of its own with a file id of its own: for this checkout "
        "at its root, for this folder alone below it.",
    )
    mode.add_argument(
        "--rebind",
        action="store_true",
        help="Replace the existing file with a new id bound to the project it "
        "named here: this checkout's at its root (the 'keep' answer when the file "
        "names another project), the folder's part below it.",
    )
    init.add_argument(
        "--name",
        type=str,
        default=None,
        metavar="N",
        help="The display name the file carries. Default: the folder's name.",
    )
    init.add_argument(
        "--boundary",
        action="store_true",
        help="Make the project the file names a tenant boundary that nothing "
        "crosses, at a repository's root, below it or in a plain folder; a "
        "replaced file keeps its boundary.",
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory project ...``."""
    command = args.project_cmd
    if command is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if command == "list":
        _list()
    elif command == "show":
        _show(args.id)
    elif command == "why":
        _why(args.record)
    elif command == "pending":
        _pending()
    elif command == "join":
        _join(args.id)
    elif command == "new":
        _new()
    elif command == "init":
        _init(new=args.new, rebind=args.rebind, name=args.name, boundary=args.boundary)
    else:
        sub_parser.error(f"unknown project command: {command!r}")


# ---------------------------------------------------------------------------
# Shared by the declarations (also `bettermemory memory move`)
# ---------------------------------------------------------------------------


def refuse(line: str) -> NoReturn:
    """One line on stderr and exit 2; nothing was declared."""
    sys.stderr.write(f"bettermemory: {line}\n")
    raise SystemExit(2)


def require_terminal(command: str) -> None:
    """Decision D6: a declaration is the user's, typed in a terminal. A
    piped or redirected stdin (a script, an agent's shell) is refused
    before the store is opened."""
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        refuse(
            f"{command} is a declaration and runs only from a terminal (decision "
            "D6); nothing changed"
        )


def principal() -> str:
    """The OS user a declaration is logged with."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError, ImportError):
        user = "unknown"
    return f"os:{user}"


def confirm(expected: str, what: str) -> None:
    """Ask for `expected` to be typed exactly; anything else, or no input,
    exits 2 with nothing changed."""
    try:
        typed = input(f'Type {what} to confirm ("{expected}"): ')
    except EOFError:
        refuse("no name typed; nothing changed")
    if typed != expected:
        refuse(f'"{typed}" is not "{expected}"; nothing changed')


def context() -> CliContext:
    from ._common import cli_context

    return cli_context()


def open_model(ctx: CliContext) -> Membership:
    """The membership model of this process's machine over the CLI's store,
    its machine row written first, with a token writer: a declaration that
    separates a checkout re-mints its checkout token (design 1.1)."""
    from .. import checkout_token, location
    from ..membership import BARE, Membership

    identity = location.process_machine()
    policy = ctx.config.membership.write_token

    def write_token(k: Checkout) -> tuple[str | None, str]:
        if k.git_dir is None or k.machine != identity.id:
            return None, "skipped:other_machine"
        wrote = checkout_token.remint(k.git_dir, policy=policy, bare=k.kind == BARE)
        return wrote.token, wrote.status

    return Membership.open(ctx.store, identity, token_writer=write_token)


def read_model(ctx: CliContext) -> Membership:
    """The model for the reading commands: this machine's id, nothing
    written."""
    from .. import location
    from ..membership import Membership

    return Membership(ctx.store, location.process_machine().id)


def caller(ctx: CliContext) -> tuple[str, Resolution]:
    """The working directory and its resolution (`location.resolve_here`)."""
    from .. import location

    try:
        cwd = os.getcwd()
    except OSError as exc:
        refuse(
            f"the working directory cannot be read ({exc.strerror}); nothing changed"
        )
    return cwd, location.resolve_here(ctx.store, ctx.config, cwd, session=None)


def named(model: Membership, project_id: str | None) -> str:
    """``"name" (P001)``, or ``global``."""
    from ..membership import GLOBAL

    if project_id is None or project_id == GLOBAL:
        return "global"
    pid = model.canonical(project_id) or project_id
    try:
        return f'"{model.project(pid).name}" ({pid})'
    except KeyError:
        return pid


def project_or_refuse(model: Membership, project_id: str) -> str:
    """The project `project_id` stands for now (merges followed)."""
    from ..membership import GLOBAL

    if project_id != GLOBAL:
        try:
            model.project(project_id)
        except KeyError:
            pass
        else:
            return model.canonical(project_id) or project_id
    refuse(f"no project {project_id}; `bettermemory project list` shows them")


def _where(k: Checkout) -> str:
    newest = k.newest_view
    return newest.path if newest is not None else k.id


def _said(res: Resolution) -> str:
    """The resolution's lines without their prefix, for a refusal."""
    prefix = "bettermemory: "
    return "; ".join(
        line[len(prefix) :] if line.startswith(prefix) else line for line in res.lines
    )


def _checkout_of(model: Membership, res: Resolution, cwd: str) -> Checkout:
    if res.checkout is None:
        detail = _said(res) or "outside any repository and project file"
        refuse(
            f"{cwd} is no checkout ({detail}); `bettermemory project init` makes a "
            "folder a project; nothing changed"
        )
    return model.checkout(res.checkout)


def _open_on(model: Membership, checkout_id: str) -> list[Proposal]:
    return [pr for pr in model.pending() if pr.checkout == checkout_id]


def _rows_of(ctx: CliContext, done: Declared, kind: str) -> list[dict[str, object]]:
    """The payloads of the rows of `kind` a declaration wrote."""
    if not done.rows:
        return []
    out: list[dict[str, object]] = []
    for row in ctx.store.membership_log([kind], since_seq=min(done.rows) - 1):
        if row.seq in done.rows:
            payload = json.loads(row.payload)
            if isinstance(payload, dict):
                out.append(payload)
    return out


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _markers(model: Membership, p: Project) -> str:
    from ..membership import UNRESOLVED_STATES

    marks: list[str] = []
    if p.provisional in UNRESOLVED_STATES:
        marks.append(f"unresolved: {p.provisional}")
    elif p.provisional:
        marks.append(f"provisional: {p.provisional}")
    if p.folder:
        marks.append("folder")
    if p.boundary:
        marks.append("boundary")
    if p.parent:
        marks.append(f"part of {named(model, p.parent)}")
    return f" [{'; '.join(marks)}]" if marks else ""


def checkout_line(model: Membership, k: Checkout) -> str:
    """One checkout: its id, kind and where it is now."""
    from ..membership import UNRESOLVED

    where = _where(k)
    if k.kind == UNRESOLVED:
        state = f"written at {where} while it could not be read"
    elif k.machine != model.machine:
        state = f"on machine {k.machine}, last seen at {where}"
    else:
        live = model.live_view(k)
        state = f"live at {live}" if live else f"no live view, last seen at {where}"
    retired = ", retired" if k.retired else ""
    return f"{k.id} {k.kind}{retired}, {state}"


def _checkouts_by_project(model: Membership) -> dict[str, list[Checkout]]:
    out: dict[str, list[Checkout]] = {}
    for k in model.checkouts():
        pid = model.canonical(k.project) or k.project
        out.setdefault(pid, []).append(k)
    return out


def _list() -> None:
    ctx = context()
    model = read_model(ctx)
    projects = model.projects()
    if not projects:
        print("bettermemory: no projects yet")
        return
    by_project = _checkouts_by_project(model)
    for p in projects:
        print(f'{p.id} "{p.name}"{_markers(model, p)}')
        checkouts = sorted(by_project.get(p.id, []), key=lambda k: k.id)
        if not checkouts:
            print("  no checkout")
        for k in checkouts:
            print(f"  {checkout_line(model, k)}")


def _show(project_id: str) -> None:
    ctx = context()
    model = read_model(ctx)
    try:
        model.project(project_id)
    except KeyError:
        refuse(f"no project {project_id}; `bettermemory project list` shows them")
    pid = model.canonical(project_id) or project_id
    if pid != project_id:
        print(f"{project_id} is merged into {pid}")
    p = model.project(pid)
    members = sorted(
        str(r["id"])
        for r in ctx.store.membership_rows("projects")
        if model.canonical(str(r["id"])) == pid
    )
    witnesses = ctx.store.membership_rows(
        "project_witnesses", column_in=("project", members)
    )
    remotes = sorted(
        {
            str(r["remote_key"])
            for r in ctx.store.membership_rows(
                "project_remotes", column_in=("project", members)
            )
        }
    )
    files = sorted(
        {
            str(r["file_id"])
            for r in ctx.store.membership_rows(
                "project_files", column_in=("project", members)
            )
        }
    )
    homes = ctx.store.membership_rows("homes", column_in=("project", members))
    memories = sum(1 for r in homes if r["kind"] == "memory")
    print(f'{pid} "{p.name}"{_markers(model, p)}')
    print(f"  witnesses: {len(witnesses)} commits")
    print(f"  remotes: {', '.join(remotes) if remotes else '(none)'}")
    print(f"  file ids: {', '.join(files) if files else '(none)'}")
    others = [m for m in members if m != pid]
    if others:
        print(f"  merged into it: {', '.join(others)}")
    print(f"  records: {memories} memories, {len(homes) - memories} episodes")
    checkouts = sorted(_checkouts_by_project(model).get(pid, []), key=lambda k: k.id)
    print(f"  checkouts ({len(checkouts)}):")
    for k in checkouts:
        print(f"    {checkout_line(model, k)}")
    ids = {k.id for k in checkouts}
    asked = [
        pr
        for pr in model.pending()
        if pr.checkout in ids or pid in {model.canonical(c) for c in pr.candidates}
    ]
    print(f"  open questions ({len(asked)}):")
    for pr in asked:
        print(f"    {pr.id} in checkout {pr.checkout}: {pr.line}")


def _why(record_id: str) -> None:
    ctx = context()
    model = read_model(ctx)
    why = model.why(record_id)
    print(why.text)
    for row in why.rows:
        print(f"  seq {row.seq} {row.ts} {row.kind} {row.payload}")


def _pending() -> None:
    ctx = context()
    model = read_model(ctx)
    asked = model.pending()
    if not asked:
        print("bettermemory: no open questions")
        return
    for pr in asked:
        if pr.checkout is not None:
            where = (
                f"in checkout {pr.checkout} at {_where(model.checkout(pr.checkout))}"
            )
        else:
            where = "in no checkout"
        print(f"{pr.id} {where} ({pr.kind}: {pr.reason}), opened {pr.opened}")
        print(f"  {pr.line}")


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _question_for(
    model: Membership, asked: list[Proposal], target: str
) -> Proposal | None:
    """The open question `project join <target>` answers: one naming the
    target (as a candidate, or as the imported project an ``import``
    question is about), else the checkout's first."""
    for pr in asked:
        named_ids = {model.canonical(c) for c in pr.candidates}
        imported = pr.evidence.get("imported_project")
        if isinstance(imported, str):
            named_ids.add(model.canonical(imported))
        if target in named_ids:
            return pr
    return asked[0] if asked else None


def _join(project_id: str) -> None:
    from ..membership import FILE_CONTRADICTION, Declaration

    require_terminal("bettermemory project join")
    ctx = context()
    # The resolution first, so the model reads the projects it minted.
    cwd, res = caller(ctx)
    model = open_model(ctx)
    k = _checkout_of(model, res, cwd)
    target = project_or_refuse(model, project_id)
    mine = model.canonical(k.project) or k.project
    pr = _question_for(model, _open_on(model, k.id), target)
    if pr is None and target == mine:
        print(
            f"bettermemory: checkout {k.id} is already project {named(model, target)}"
        )
        return
    if pr is not None:
        print(
            f"bettermemory project join answers question {pr.id} in checkout {k.id} "
            f"at {_where(k)}:"
        )
        print(f"  {pr.line}")
        print(f"  with: the same project as {named(model, target)}")
    else:
        print(
            f"bettermemory project join: checkout {k.id} at {_where(k)} leaves "
            f"{named(model, mine)} and joins {named(model, target)}; what was "
            "written there goes with it"
        )
    if pr is None or pr.kind == FILE_CONTRADICTION:
        parts = _parts_said(model, k)
        if parts:
            print(parts)
    confirm(model.project(target).name, "the project's name")
    done = model.declare(
        Declaration(
            "join",
            principal(),
            checkout=k.id,
            proposal=pr.id if pr is not None else None,
            target=target,
        )
    )
    answered = f"; question {pr.id} answered" if pr is not None else ""
    print(
        f"bettermemory: checkout {k.id} is project {named(model, done.project)}{answered}"
    )
    for row in _rows_of(ctx, done, "remote_declared"):
        print(
            f"bettermemory: {row.get('remote')} is declared {named(model, target)}'s "
            "remote for shallow clones (D18)"
        )


def _folder_name(k: Checkout, cwd: str) -> str:
    newest = k.newest_view
    path = newest.path if newest is not None else cwd
    return os.path.basename(path.rstrip("/\\")) or path


def _token_said(ctx: CliContext, done: Declared) -> str:
    rows = _rows_of(ctx, done, "token_reminted")
    if not rows:
        return ""
    status = rows[-1].get("status")
    if rows[-1].get("written"):
        return "; its checkout token was re-minted"
    return f"; its checkout token was not re-minted ({status})"


def _root_of(model: Membership, k: Checkout) -> str:
    """The checkout's root on this machine: its newest view here."""
    views = k.views_on(model.machine)
    return views[-1].path if views else _where(k)


def _filed_root(model: Membership, k: Checkout) -> tuple[Path, str] | None:
    """The checkout's root project file and the project its id is bound
    to, when it is bound."""
    from .. import project_file

    path = Path(_root_of(model, k)) / project_file.PROJECT_FILE_NAME
    try:
        found = project_file.parse(path) if path.exists() else None
    except OSError:
        return None
    if not isinstance(found, project_file.ProjectFile):
        return None
    bound = model.file_binding(found.id)
    return (path, bound) if bound is not None else None


def _parts_said(model: Membership, k: Checkout) -> str:
    """The plan's line for the parts a split takes along (B10)."""
    parts = model.parts_in(k.id)
    if not parts:
        return ""
    listed = ", ".join(
        f"{named(model, part.id)}" + (f" at {folder}" if folder else "")
        for part, _file, folder in parts
    )
    return f"  and takes its parts along: {listed}"


def _new() -> None:
    from .. import location
    from ..membership import REMOTE_DIVERGED, SUBPROJECT_CONTRADICTION, Declaration

    require_terminal("bettermemory project new")
    ctx = context()
    # The resolution first, so the model reads the projects it minted.
    cwd, res = caller(ctx)
    model = open_model(ctx)
    k = _checkout_of(model, res, cwd)
    # A part's question (L7) is answered `init --new` in that folder.
    asked = [x for x in _open_on(model, k.id) if x.kind != SUBPROJECT_CONTRADICTION]
    pr = asked[0] if asked else None
    name = _folder_name(k, cwd)
    mine = model.canonical(k.project) or k.project
    filed = _filed_root(model, k)
    if filed is not None and (
        pr is None or pr.kind == REMOTE_DIVERGED or filed[1] != mine
    ):
        # The committed file would keep naming the project the checkout
        # leaves (B11): a fresh file makes it a project of its own.
        refuse(
            f"{filed[0]} names {named(model, filed[1])}; a project of its own "
            "for this checkout: bettermemory project init --new (a fresh file); "
            "nothing changed"
        )
    if pr is not None:
        print(
            f"bettermemory project new answers question {pr.id} in checkout {k.id} "
            f"at {_where(k)}:"
        )
        print(f"  {pr.line}")
        print("  with: separate, a project of its own")
    else:
        print(
            f"bettermemory project new: checkout {k.id} at {_where(k)} leaves "
            f'{named(model, mine)} and becomes a project of its own, "{name}"; what '
            "was written there goes with it, and its checkout token is re-minted"
        )
    if pr is None or pr.kind == REMOTE_DIVERGED:
        parts = _parts_said(model, k)
        if parts:
            print(parts)
    confirm(name, "the folder's name")
    done = model.declare(
        Declaration(
            "new",
            principal(),
            checkout=k.id,
            proposal=pr.id if pr is not None else None,
            name=name,
        )
    )
    location.forget(cwd)
    answered = f"; question {pr.id} answered" if pr is not None else ""
    print(
        f"bettermemory: checkout {k.id} is project {named(model, done.project)}"
        f"{answered}{_token_said(ctx, done)}"
    )


def _init(*, new: bool, rebind: bool, name: str | None, boundary: bool) -> None:
    from .. import location, project_file
    from ..membership import (
        FOLDER,
        GLOBAL,
        GLOBAL_OUTCOME,
        SUBPROJECT_CONTRADICTION,
        Declaration,
    )

    require_terminal("bettermemory project init")
    ctx = context()
    # The resolution first, so the model reads the projects it minted.
    cwd, res = caller(ctx)
    model = open_model(ctx)
    directory = Path(cwd)
    path = directory / project_file.PROJECT_FILE_NAME
    display = name or directory.name or cwd
    existing = project_file.parse(path) if path.exists() else None
    if isinstance(existing, project_file.ProjectFile):
        names = f' (it names "{existing.name}", {existing.id})'
    elif existing is not None:
        names = f" ({existing.detail})"
    else:
        names = ""
    if rebind and existing is None:
        refuse(f"no {path} to rebind; `bettermemory project init` writes one")
    if existing is not None and not (new or rebind):
        refuse(
            f"{path} exists{names}; keep this checkout's project: bettermemory "
            "project init --rebind; a project of its own: bettermemory project init "
            "--new"
        )
    # A replaced file keeps its boundary; `--boundary` sets one anywhere (B9).
    boundary = boundary or (
        isinstance(existing, project_file.ProjectFile) and existing.boundary
    )
    fence = ", a boundary: nothing crosses it" if boundary else ""
    if res.checkout is None and res.outcome != GLOBAL_OUTCOME:
        refuse(f"{_said(res)}; nothing changed")
    k = model.checkout(res.checkout) if res.checkout is not None else None
    # Where the file lies decides what it names (design 2.1, decisions D13
    # and B17), and the model decides it from the folder: at the
    # checkout's root the checkout's project; below it a part of the
    # project enclosing the folder (the caller's project there), the
    # checkout keeping its own; in a plain folder a folder project.
    below = False
    if k is not None:
        where = model.below_root(k, cwd)
        if where is None:
            refuse(
                f"cannot tell where {cwd} stands in checkout {k.id} (seen at "
                f"{_where(k)}); nothing changed"
            )
        below = where
    if below and (res.project is None or res.project == GLOBAL):
        refuse(f"{_said(res)}; nothing changed")
    bound = (
        model.file_binding(existing.id)
        if isinstance(existing, project_file.ProjectFile)
        else None
    )
    if below and rebind and bound is not None and bound != res.project:
        # The replaced file names a project off the caller's chain: L7's
        # question, answered in this folder by `join` or `init --new`.
        refuse(
            f"{path} names {named(model, bound)}, not a part of "
            f"{named(model, res.project)}; that folder is it: bettermemory "
            f"project join {bound}; a part with an id of its own: bettermemory "
            "project init --new; nothing changed"
        )
    pr: Proposal | None = None
    if k is not None and new:
        asked = _open_on(model, k.id)
        if below:
            # Below the root `--new` answers the question about this folder
            # only (L7, E35 g).
            here = [
                x
                for x in asked
                if x.kind == SUBPROJECT_CONTRADICTION
                and os.path.normpath(str(x.evidence.get("dir", "")))
                == os.path.normpath(cwd)
            ]
            pr = here[0] if here else None
        else:
            others = [x for x in asked if x.kind != SUBPROJECT_CONTRADICTION]
            pr = others[0] if others else None
    kept = bound if below and rebind else None
    part = below and pr is None and kept is None
    enclosing = res.project if part else None
    verb = "replaces" if existing is not None else "writes"
    shape = f'name "{display}"' + (", a boundary" if boundary else "")
    print(f"bettermemory project init {verb} {path}{names} with a new id ({shape})")
    if k is None:
        print(f"  and makes {cwd} a folder project")
    elif enclosing is not None and k.kind == FOLDER:
        print(
            f"  and makes {cwd} a folder project inside {named(model, enclosing)}, "
            f"whose folder {_where(k)} keeps its project"
        )
    elif enclosing is not None:
        print(
            f'  and makes {cwd} a sub-project "{display}" of '
            f"{named(model, enclosing)}, in checkout {k.id} at {_where(k)}"
        )
        if new:
            print(
                f"  with an id of its own; checkout {k.id} keeps project "
                f"{named(model, k.project)}"
            )
    elif pr is not None:
        print(f"  and answers question {pr.id} in checkout {k.id}:")
        print(f"  {pr.line}")
        print("  with: a project of its own, with this file's id")
    elif new:
        print(
            f"  and makes checkout {k.id} at {_where(k)} a project of its own; what "
            "was written there goes with it"
        )
        parts = _parts_said(model, k)
        if parts:
            print(parts)
    else:
        print(
            f"  bound to project {named(model, kept or k.project)} of checkout "
            f"{k.id} at {_where(k)}"
        )
    keeps = None if part else model.canonical(kept or (k.project if k else None))
    lifted = (
        rebind and not boundary and keeps is not None and model.project(keeps).boundary
    )
    if lifted:
        print(
            f"  and lifts the boundary of {named(model, keeps)}: the fresh file "
            "declares none"
        )
    confirm(display, "the folder's name")
    try:
        written = project_file.write(
            directory, name=display, boundary=boundary, rebind=existing is not None
        )
    except (OSError, ValueError) as exc:
        refuse(f"could not write {path}: {exc}; nothing changed")
    location.forget(cwd)
    if k is None:
        _, res = caller(ctx)
        for line in res.lines:
            print(line)
        if res.checkout is None:
            print(
                f"bettermemory: wrote {path} ({written.id}); the next session decides"
            )
            return
        # The folder's first sight made it a folder project from this file:
        # the declaration binds the file there, whatever the flags.
        k = model.checkout(res.checkout)
        action = "init"
    else:
        action = "init_new" if new else "init_rebind" if rebind else "init"
    outer: str | None = None
    if rebind and keeps is not None:
        own = model.chain_of(keeps)
        outer = next(
            (c for c in res.chain if c not in own and model.project(c).boundary),
            None,
        )
    done = model.declare(
        Declaration(
            action,
            principal(),
            checkout=k.id,
            proposal=pr.id if pr is not None else None,
            target=res.project if below else None,
            file_id=written.id,
            name=display,
            directory=cwd,
            boundary=boundary,
            outer_tenant=outer,
            replaced=(
                existing.id if isinstance(existing, project_file.ProjectFile) else None
            ),
        )
    )
    location.forget(cwd)
    if enclosing is not None:
        kind = "a folder project" if k.kind == FOLDER else "a sub-project"
        print(
            f"bettermemory: wrote {path} ({written.id}), {kind} "
            f"{named(model, done.project)} of {named(model, enclosing)}"
            + (
                fence
                if boundary
                else ": a session here gets its own and its parents' memories, "
                "never a sibling's"
            )
            + (
                f"; checkout {k.id} keeps project {named(model, k.project)}"
                if new
                else ""
            )
            + "; commit it so every clone and CI runner finds it"
        )
        return
    answered = f"; question {pr.id} answered" if pr is not None else ""
    if lifted:
        fence = ", no longer a boundary"
    print(
        f"bettermemory: wrote {path} ({written.id}), bound to project "
        f"{named(model, done.project)}{fence}{answered}{_token_said(ctx, done)}; "
        "commit it so every clone and CI runner finds the project"
    )


__all__ = [
    "add_subparser",
    "caller",
    "checkout_line",
    "confirm",
    "context",
    "named",
    "open_model",
    "principal",
    "project_or_refuse",
    "read_model",
    "refuse",
    "require_terminal",
    "run",
]
