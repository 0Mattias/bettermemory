"""The lines of `membership.py` (design 5.4): L1 to L10 are written here.

`line_first_sight` (L1) through `line_import` (L10), and
`line_unresolved`, the form decision B6 gives L6, are the words a
session's first output, a question and the log carry, and the lines
without a number beside them (a folder project's first sight, a
boundary flag, a match across a boundary, a crowd of content matches)
are written here too. `_named` spells a project and `_answer` the agent's
``answer_question`` call a line offers.

Every value a line quotes (a path, a directory or project name, a remote
key, an OS error's detail, an evidence list) is written through
`history.printable`, the one escaping function the text reports and
`bettermemory security audit` print through (ENG-154): a directory whose
name holds a newline, U+2028 or another control character renders inside
its own line, so no line of a session's first output starts with text a
folder's name carries. Project-file names are refused with those
characters already (`project_file`); the rest of what a line quotes is
read off the filesystem, a client's token or an imported document.

`membership.py` imports them back: its `__all__` lists the `line_*`
functions, which other modules import from there, and the model calls
them. This module lives apart from `membership.py` to keep each module
under the package's 2,000-line bound (ENG-96); it imports the standard
library, `membership_records` (for `WORDS`) and `history` (for
`printable`), and `membership.py` imports it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .history import printable
from .membership_records import WORDS


# ---------------------------------------------------------------------------
# The lines (design 5.4)
# ---------------------------------------------------------------------------


def _named(name: str, project_id: str) -> str:
    return f'"{printable(name)}" ({printable(project_id)})'


def _answer(
    question: str | None, answer: str, target: str | None = None, *, said: bool = False
) -> str:
    """The agent's call that answers a question (design 8.8): the
    ``answer_question`` action of ``memory_admin``, naming the question
    when its id is known, the target project of a join, and for a join
    the user's words it rests on."""
    args = ['action="answer_question"']
    if question:
        args.append(f'id="{printable(question)}"')
    args.append(f'answer="{printable(answer)}"')
    if target:
        args.append(f'target="{printable(target)}"')
    if said:
        args.append('said="<the user\'s words>"')
    return "memory_admin(" + ", ".join(args) + ")"


def line_first_sight(
    root: str,
    candidates: Sequence[tuple[str, str]],
    evidence: str,
    *,
    unwritten: str | None = None,
    filed: bool = False,
    question: str | None = None,
) -> str:
    """L1. `candidates` are (name, id) pairs; `evidence` the witness
    words; `unwritten` the token file and status that could not be
    written; `filed` when the root's project file is bound, which makes
    the separate answer ``project init --new`` (E22 e); `question` the
    id of the question the line opens."""
    names = " or ".join(_named(n, p) for n, p in candidates)
    join = " | ".join(_answer(question, "join", p, said=True) for _n, p in candidates)
    separate = (
        "bettermemory project init --new (a fresh project file)"
        if filed
        else _answer(question, "separate")
    )
    could_not = f" (could not write {printable(unwritten)})" if unwritten else ""
    return (
        f"bettermemory: {printable(root)} may be project {names}: "
        f"{printable(evidence)}{could_not}. "
        "Until answered, nothing from other projects is shown here and what "
        "is written here stays with this folder. "
        f"Same project, when the user says so: {join}; separate: {separate}"
    )


def line_new(
    root: str,
    name: str,
    project_id: str,
    *,
    previous: Sequence[tuple[str, str, str]] = (),
    unwritten: str | None = None,
) -> str:
    """L2. `previous` are (checkout, project name, project id) of the
    checkouts seen at this path before."""
    line = (
        f"bettermemory: {printable(root)} is a new project {_named(name, project_id)}"
    )
    if previous:
        line += (
            "; "
            + ", ".join(
                f"checkout {printable(k)} of {_named(n, p)}" for k, n, p in previous
            )
            + " was here before and this repository is not it (no token, commit or "
            "file of it); if it is: "
            + " | ".join(
                f"bettermemory project join {printable(p)}" for _k, _n, p in previous
            )
        )
    if unwritten:
        line += f"; could not write its token ({printable(unwritten)})"
    return line


def line_file_contradiction(
    root: str,
    name: str,
    project_id: str,
    file_name: str,
    file_project: str,
    *,
    question: str | None = None,
) -> str:
    """L3."""
    return (
        f"bettermemory: {printable(root)} is project {_named(name, project_id)} "
        f"but its project file names {_named(file_name, file_project)}. Keep: bettermemory "
        f"project init --rebind; move this checkout, when the user says so: "
        f"{_answer(question, 'join', file_project, said=True)}"
    )


def line_joined(root: str, name: str, project_id: str, codes: Iterable[str]) -> str:
    """L4."""
    words = ", ".join(WORDS[c] for c in sorted(codes) if c in WORDS)
    return (
        f"bettermemory: {printable(root)} is project {_named(name, project_id)} "
        f"({words}); "
        "what was written here joined it"
    )


def line_remote_diverged(
    root: str,
    new_key: str,
    name: str,
    project_id: str,
    old_key: str,
    *,
    question: str | None = None,
) -> str:
    """L5."""
    return (
        f"bettermemory: {printable(root)} now names {printable(new_key)}, while "
        f"project {_named(name, project_id)} elsewhere still names "
        f"{printable(old_key)}. Still the "
        f"same project, when the user says so: "
        f"{_answer(question, 'join', project_id, said=True)}; a project of its "
        f"own: {_answer(question, 'separate')}"
    )


def line_legacy(
    name: str,
    memories: int,
    episodes: int,
    path: str,
    held: int,
    candidate_name: str,
    candidate_id: str,
    *,
    recorded: int | None = None,
    question: str | None = None,
) -> str:
    """L6, the migration's question in the candidate checkout: `held` of
    the `recorded` commits the records carry are in this repository
    (`recorded` defaults to `memories`)."""
    of = memories if recorded is None else recorded
    return (
        f"bettermemory: the records of {printable(name)} ({memories} memories, "
        f"{episodes} episodes; its folder {printable(path)} is gone) may belong to "
        f'project "{printable(candidate_name)}" here: this repository holds {held} '
        "of their "
        f"{of} recorded commits. Same project, when the user says so: "
        f"{_answer(question, 'join', candidate_id, said=True)}; separate: "
        f"{_answer(question, 'separate')}"
    )


def line_subproject(
    file_path: str,
    name: str,
    project_id: str,
    repository_name: str,
    parent_name: str,
    *,
    question: str | None = None,
) -> str:
    """L7."""
    return (
        f"bettermemory: {printable(file_path)} names {_named(name, project_id)}, "
        f'which is not part of "{printable(repository_name)}"; treated as '
        f'"{printable(parent_name)}" until '
        f"answered. That folder is {_named(name, project_id)}, when the user says "
        f"so: {_answer(question, 'join', project_id, said=True)}; a part of "
        f'"{printable(parent_name)}" with an id of its own: bettermemory project '
        "init --new "
        "(in that folder)"
    )


def line_unreadable(cwd: str, detail: str) -> str:
    """L8."""
    return (
        f"bettermemory: cannot tell what {printable(cwd)} is ({printable(detail)}); "
        "nothing from any "
        "project is shown here"
    )


def line_deferred(root: str, file_name: str) -> str:
    """L9."""
    return (
        f"bettermemory: {printable(root)}: {printable(file_name)} is not on this "
        "disk yet (evicted by "
        "the file provider); it was asked for; this prompt shows nothing from any "
        "project and the next one decides"
    )


def line_import(
    name: str,
    source: str,
    candidates: Sequence[tuple[str, str, Sequence[str]]],
    *,
    question: str | None = None,
) -> str:
    """L10. `candidates` are (name, id, witness codes)."""
    named = ", ".join(
        f'"{printable(n)}" ({printable(p)}: {printable("+".join(sorted(codes)))})'
        for n, p, codes in candidates
    )
    first = candidates[0][1] if candidates else None
    return (
        f'bettermemory: imported project "{printable(name)}" from '
        f"{printable(source)} may be {named}. "
        f"Same project, when the user says so: "
        f"{_answer(question, 'join', first, said=True)} (in that checkout); keep "
        f"apart: {_answer(question, 'separate')}"
    )


def line_unresolved(
    path: str,
    memories: int,
    episodes: int,
    name: str,
    project_id: str,
    *,
    question: str | None = None,
) -> str:
    """L6 in the form decision B6 gives it: writes made at `path` while
    it could not be read, offered to the project of the checkout found
    there by another session."""
    return (
        f"bettermemory: the records written at {printable(path)} while it could "
        "not be "
        f"read ({memories} memories, {episodes} episodes) may belong to "
        f"project {_named(name, project_id)} here. Same project, when the user "
        f"says so: {_answer(question, 'join', project_id, said=True)}; a project "
        f"of their own: {_answer(question, 'separate')}"
    )


# ---------------------------------------------------------------------------
# The lines without a number
# ---------------------------------------------------------------------------


def line_folder_project(folder: str, name: str, *, boundary: bool) -> str:
    """A workspace's project file seen for the first time: the folder
    above the repository is a project of its own."""
    return (
        f'bettermemory: {printable(folder)} is a project of its own ("'
        f'{printable(name)}", its file)'
        + ("; a boundary: nothing crosses it" if boundary else "")
    )


def line_new_folder(root: str, name: str, project_id: str) -> str:
    """A plain folder whose project file was seen for the first time."""
    return (
        f"bettermemory: {printable(root)} is a new project "
        f"{_named(name, project_id)}, its file"
    )


def line_boundary_added(path: str, name: str, project_id: str) -> str:
    """B12: a bound file now carries ``boundary = true``."""
    return (
        f"bettermemory: {printable(path)} now declares a boundary: "
        f"{_named(name, project_id)} is a tenant from now on; nothing crosses it"
    )


def line_boundary_dropped(path: str, name: str, project_id: str, folder: str) -> str:
    """B12: a bound file no longer carries the flag its project has."""
    return (
        f"bettermemory: {printable(path)} no longer declares a boundary; "
        f"{_named(name, project_id)} stays one until it is lifted: bettermemory "
        f"project init --rebind (in {printable(folder)}) binds a fresh file "
        "without it"
    )


def line_across_boundary(root: str, named: Sequence[str]) -> str:
    """Projects the evidence names on the other side of a boundary;
    `named` are their spellings (`_named`)."""
    return (
        f"bettermemory: {printable(root)} matches "
        + ", ".join(printable(n) for n in named)
        + " across a boundary; kept apart"
    )


def line_content_crowded(root: str, count: int, limit: int) -> str:
    """More commit-less projects match the content than a question names."""
    return (
        f"bettermemory: {printable(root)} has the folder name and files of "
        f"{count} commit-less projects with no live checkout, more "
        f"than the {limit} a question names, so none is offered; "
        "`bettermemory project list` shows them, `bettermemory project "
        "join <id>` joins one"
    )
