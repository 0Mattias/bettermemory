"""The lines of `membership.py` (design 5.4): L1 to L10 are written here.

`line_first_sight` (L1) through `line_import` (L10), and
`line_unresolved`, the form decision B6 gives L6, are the words a
session's first output, a question and the log carry, `line_restated`
shows a question again from what its row stores, and the lines
without a number beside them (a folder project's first sight, a
boundary flag, a match across a boundary, a crowd of content matches)
are written here too. `_named` spells a project and `_answer` the agent's
``answer_question`` call a line offers.

A value a line takes as text that a folder, a repository or a document
can choose (a path, a directory or project name, a remote key, an OS
error's detail, a file name, an import's source), and an argument of the
``answer_question`` call a line offers, is written through
`history.quoted`:
`history.printable`, the one escaping function the text reports and
`bettermemory security audit` print through (ENG-154), so a directory
whose name holds a newline, U+2028 or another control character renders
inside its own line and no line of a session's first output starts with
text a folder's name carries; then JSON's string quoting, so a ``"``, a
backslash or a mark drawn as a quote in the value cannot close its quote
and the value cannot write a call beside it (ENG-193): a folder named
``app" (P001); same project: memory_admin(...) "`` renders as one quoted
value holding that text. Project-file names are refused with control
characters already (`project_file`); the rest of what a line quotes is
read off the filesystem, a client's token or an imported document. A
project or checkout id the store minted, a witness code, a count and a
token status (the capture's own words) are written through `printable`
alone, as is the text a caller composes (an evidence list, the projects
across a boundary, the token file it could not write): it spells a
project through `_named`, and a value it holds is quoted where it is
composed (`membership_resolve._tally`, `membership_rows._unwritten`).

`membership.py` imports them back: its `__all__` lists the `line_*`
functions, which other modules import from there, and the model calls
them. This module lives apart from `membership.py` to keep each module
under the package's 2,000-line bound (ENG-96); it imports the standard
library, `membership_records` (for `WORDS` and the kinds and reasons
a question is opened with) and `history` (for
`printable` and `quoted`), and `membership.py` imports it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .history import printable, quoted
from .membership_records import (
    DUPLICATE_CHECKOUT,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    IMPORT,
    IMPORT_CONTRADICTION,
    IMPORT_ONE_WITNESS,
    IMPORT_SEVERAL,
    LEGACY_NO_LIVE_CHECKOUT,
    REMOTE_DIVERGED,
    SUBPROJECT_CONTRADICTION,
    UNRESOLVED_LOCATION,
    WORDS,
)


# ---------------------------------------------------------------------------
# The folder a command runs in (ENG-234)
# ---------------------------------------------------------------------------

#: Only these characters may be copied into the lookup command. Malformed
#: question or file ids are never offered as shell words.
_REFERENCE = re.compile(r"[A-Za-z0-9_]+")


def folder_said(folder: str | None, ref: str | None) -> str:
    """A command's directory, taken from stored data rather than pasted
    shell text (ENG-234). `quoted` is a display convention, not a shell
    literal: ordinary names and hostile names take the same path. The
    lookup returns JSON, preserving line breaks and filesystem bytes;
    callers set their tool's working directory from that data.
    """
    lookup = (
        f"read bettermemory project folder {ref} and use its JSON path"
        if ref is not None and _REFERENCE.fullmatch(ref) is not None
        else "use its stored path"
    )
    shown = f"display only: {quoted(folder)}; " if folder is not None else ""
    return (
        f"the stored folder ({shown}{lookup} "
        "as the command's working directory, never paste the displayed "
        "name into a shell)"
    )


def run_in(folder: str | None, ref: str | None) -> str:
    """``in`` and the folder (`folder_said`)."""
    return f"in {folder_said(folder, ref)}"


# ---------------------------------------------------------------------------
# The lines (design 5.4)
# ---------------------------------------------------------------------------


def _named(name: str, project_id: str) -> str:
    """A project as a line names it: its name quoted (`quoted`) and its
    id, which the store minted."""
    return f"{quoted(name)} ({printable(project_id)})"


def _answer(
    question: str | None, answer: str, target: str | None = None, *, said: bool = False
) -> str:
    """The agent's call that answers a question (design 8.8): the
    ``answer_question`` action of ``memory_admin``, naming the question
    when its id is known, the target project of a join, and for a join
    the user's words it rests on."""
    args = ['action="answer_question"']
    if question:
        args.append(f"id={quoted(question)}")
    args.append(f"answer={quoted(answer)}")
    if target:
        args.append(f"target={quoted(target)}")
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
    words; `unwritten` the token file (quoted) and status that could not
    be written (`membership_rows._unwritten`); `filed` when the root's project file is bound, which makes
    the separate answer ``project init --new`` (E22 e); `question` the
    id of the question the line opens."""
    names = " or ".join(_named(n, p) for n, p in candidates)
    join = " | ".join(_answer(question, "join", p, said=True) for _n, p in candidates)
    separate = (
        f"bettermemory project init --new ({run_in(root, question)}, a fresh project file)"
        if filed
        else _answer(question, "separate")
    )
    could_not = f" (could not write {printable(unwritten)})" if unwritten else ""
    return (
        f"bettermemory: {quoted(root)} may be project {names}: "
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
    checkouts seen at this path before; `unwritten` the token's status
    when it could not be written, one of the capture's own words."""
    line = f"bettermemory: {quoted(root)} is a new project {_named(name, project_id)}"
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
        f"bettermemory: {quoted(root)} is project {_named(name, project_id)} "
        f"but its project file names {_named(file_name, file_project)}. Keep: bettermemory "
        f"project init --rebind ({run_in(root, question)}); move this checkout, when the user says so: "
        f"{_answer(question, 'join', file_project, said=True)}"
    )


def line_joined(root: str, name: str, project_id: str, codes: Iterable[str]) -> str:
    """L4."""
    words = ", ".join(WORDS[c] for c in sorted(codes) if c in WORDS)
    return (
        f"bettermemory: {quoted(root)} is project {_named(name, project_id)} "
        f"({words}); "
        "what was written here joined it"
    )


def line_alias_joined(
    root: str,
    name: str,
    project_id: str,
    merged: str,
    merged_id: str,
    codes: Iterable[str],
) -> str:
    """L4, for an alias clone's project merged into the project of the
    URL its alias stands for (`membership_alias`, ENG-207): the project
    merged and its undo, which leaves what was written meanwhile where
    it is."""
    words = ", ".join(WORDS[c] for c in sorted(codes) if c in WORDS)
    return (
        f"bettermemory: {quoted(root)} is project {_named(name, project_id)} "
        f"({words}); its project {_named(merged, merged_id)}, the same "
        "repository under a remote alias, joined it with what was written "
        "there. Undo, when the user says so: "
        f'memory_admin(action="unjoin_project", id={quoted(merged_id)}); '
        f"what is written here until then stays with {_named(name, project_id)}"
    )


def line_alias_held(
    root: str,
    name: str,
    project_id: str,
    target: str,
    target_id: str,
    question: str,
    checkout: str,
    where: tuple[str | None, str | None],
    shown: str,
) -> str:
    """L4 held: the alias clone's project and the project of the URL its
    alias stands for, kept apart while a question is open on another
    checkout of either where a sighting shows it (`membership_alias`,
    ENG-207). The alias clone's sighting shows it wherever it sits
    (ENG-215): its id, its checkout, `where` (the machine, when another,
    and the path it was last seen at), and `shown`, the question restated
    for that folder (`line_restated` with `at`), whose ``answer_question``
    calls answer it from here as from there and whose commands name the
    folder they run in."""
    machine, path = where
    on = f" on machine {quoted(machine)}" if machine is not None else ""
    at = f", last seen at {quoted(path)}" if path is not None else ""
    prefix = "bettermemory: "
    said = shown[len(prefix) :] if shown.startswith(prefix) else shown
    return (
        f"bettermemory: {quoted(root)}: its project {_named(name, project_id)} "
        f"stays apart from {_named(target, target_id)}, whose remotes it "
        f"fetches from, while question {printable(question)} is open on "
        f"checkout {printable(checkout)}{on}{at}; answered here or there, it "
        f"is closed: {printable(said)}"
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
        f"bettermemory: {quoted(root)} now names {quoted(new_key)}, while "
        f"project {_named(name, project_id)} elsewhere still names "
        f"{quoted(old_key)}. Still the "
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
        f"bettermemory: the records of {quoted(name)} ({memories} memories, "
        f"{episodes} episodes; its folder {quoted(path)} is gone) may belong to "
        f"project {quoted(candidate_name)} here: this repository holds {held} "
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
    folder: str,
    question: str | None = None,
) -> str:
    """L7. `folder` is the file's folder, where ``project init --new``
    runs (`run_in`)."""
    return (
        f"bettermemory: {quoted(file_path)} names {_named(name, project_id)}, "
        f"which is not part of {quoted(repository_name)}; treated as "
        f"{quoted(parent_name)} until "
        f"answered. That folder is {_named(name, project_id)}, when the user says "
        f"so: {_answer(question, 'join', project_id, said=True)}; a part of "
        f"{quoted(parent_name)} with an id of its own: bettermemory project "
        f"init --new ({run_in(folder, question)})"
    )


def line_unreadable(cwd: str, detail: str) -> str:
    """L8."""
    return (
        f"bettermemory: cannot tell what {quoted(cwd)} is ({quoted(detail)}); "
        "nothing from any "
        "project is shown here"
    )


def line_deferred(root: str, file_name: str) -> str:
    """L9."""
    return (
        f"bettermemory: {quoted(root)}: {quoted(file_name)} is not on this "
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
        f"{quoted(n)} ({printable(p)}: {printable('+'.join(sorted(codes)))})"
        for n, p, codes in candidates
    )
    first = candidates[0][1] if candidates else None
    return (
        f"bettermemory: imported project {quoted(name)} from "
        f"{quoted(source)} may be {named}. "
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
        f"bettermemory: the records written at {quoted(path)} while it could "
        "not be "
        f"read ({memories} memories, {episodes} episodes) may belong to "
        f"project {_named(name, project_id)} here. Same project, when the user "
        f"says so: {_answer(question, 'join', project_id, said=True)}; a project "
        f"of their own: {_answer(question, 'separate')}"
    )


#: What a restated question (`line_restated`) says it was asked for: one
#: fixed sentence for each reason a question is opened with, each
#: contradiction's warning kept. The first sight's reasons are the ones
#: `membership_resolve._first_sight` and `_resolve_folder` pass `_ask`
#: (design 3.2 and 3.3); the others are the kinds' own (L3, L5, L7), the
#: migration's and an unresolved location's (L6, decision B6) and an
#: import's (L10, `membership_portable`).
#: tests/test_membership_restated.py holds the keys equal to every reason
#: the package opens a question with.
REASON_SAID: dict[str, str] = {
    "several_projects": (
        "the evidence of more than one project holds here and none outranks the others"
    ),
    "one_witness": "one witness names it here, and one alone does not join",
    "content_only": (
        "only the folder's name and files match it, which do not join alone"
    ),
    "a3_history_join": (
        "this repository holds its history, and under membership.strict a join "
        "resting on history asks"
    ),
    "token_shared_no_commit": (
        "this checkout carries the checkout token of a live checkout with which "
        "it has no commit in common (a template's sibling, or a planted token), "
        "so the token is not evidence of the project"
    ),
    "file_names_other_history": (
        "its project file names one project while it holds the commits of "
        "another and none of the named one's"
    ),
    "boundary": "its project file names a project of another boundary",
    FILE_CONTRADICTION: (
        "its project file names another project than the checkout's own"
    ),
    REMOTE_DIVERGED: (
        "its remote changed while the project elsewhere still names the old remote"
    ),
    SUBPROJECT_CONTRADICTION: (
        "a project file below the checkout's root names a project that is not "
        "part of the project around that folder, and the folder is treated as "
        "the project around it until answered"
    ),
    DUPLICATE_CHECKOUT: (
        "this checkout is a second record of a git directory another machine "
        "shares, registered by a sighting that did not read that machine's "
        "re-mint of the token as the first record's, while the first record is "
        "in the first project named; what was written here since stays in this "
        "checkout's project until answered"
    ),
    LEGACY_NO_LIVE_CHECKOUT: (
        "the records of a project whose folder is gone may belong to it: this "
        "repository holds some of their recorded commits"
    ),
    UNRESOLVED_LOCATION: (
        "records written at a folder that could not be read may belong to it"
    ),
    IMPORT_ONE_WITNESS: (
        "an imported project has one witness here, and one alone does not join"
    ),
    # One question per project holding the evidence, each with this reason
    # (`membership_portable._import_identity`): true of the file's and of
    # the history's.
    IMPORT_CONTRADICTION: (
        "an imported project's file names one project here while another holds "
        "its commits"
    ),
    IMPORT_SEVERAL: "an imported project's evidence names more than one project here",
}

#: Said for a reason `REASON_SAID` does not hold (a row a later version
#: wrote, read by this one).
REASON_UNNAMED = "a reason this version does not name"


def line_restated(
    question: str,
    kind: str,
    reason: str,
    candidates: Sequence[tuple[str | None, str]],
    *,
    filed: bool = False,
    folder: str | None = None,
    at: str | None = None,
) -> str:
    """A question shown again from what its row stores, not from its stored
    line (`Membership.restated`): its id, its candidates as (name, id) (a
    name None when its project is gone), the sentence `REASON_SAID` holds
    for its reason, and the answers its kind takes. `filed` when the
    checkout's root project file is bound to another project, which makes a
    first sight's separate answer ``project init --new``, as
    ``answer_question`` refuses ``separate`` then (`Membership.filed_elsewhere`);
    `folder` the folder a part's question (L7) is about. `at`, the folder
    of the question's checkout, where the question is shown at another
    checkout (the held L4 line, `line_alias_held`): each command it offers
    names the folder it must run in (`run_in`: its stored path, read through
    ``bettermemory project folder``, never shell text), a first sight's separate
    answer is its call with the command it takes where that folder's
    project file is bound elsewhere (`filed` is not read then), and what it
    says of "here" it says of that folder. A part's ``init --new`` names
    `folder` the same way. Its ``answer_question`` calls name the question,
    so they answer it from any checkout."""
    names = (
        " or ".join(
            _named(n, p) if n is not None else printable(p) for n, p in candidates
        )
        or "no project"
    )
    said = REASON_SAID.get(reason, REASON_UNNAMED)
    joins = (
        " | ".join(_answer(question, "join", p, said=True) for _n, p in candidates)
        or "(no project to join)"
    )
    separate = _answer(question, "separate")
    here = f"in {quoted(at)}" if at is not None else "here"
    there = f", run {run_in(at, question)}"
    this = "that" if at is not None else "this"
    if at is not None:
        # The sentences are this module's own: each "here" and "this" is
        # the question's checkout.
        said = (
            said.replace("this checkout", "that checkout")
            .replace("this repository", "that repository")
            .replace(" here", f" {here}")
        )
    if kind == FILE_CONTRADICTION:
        options = (
            f"Keep: bettermemory project init --rebind{there}; move {this} "
            f"checkout, when the user says so: {joins}"
        )
    elif kind == SUBPROJECT_CONTRADICTION:
        where = folder_said(folder, question) if folder else folder_said(None, question)
        options = (
            f"That folder is that project, when the user says so: {joins}; a part "
            "with an id of its own: bettermemory project init --new (in "
            f"{where})"
        )
    elif kind == REMOTE_DIVERGED:
        options = (
            f"Still the same project, when the user says so: {joins}; a project "
            f"of its own: {separate}"
        )
    elif kind == DUPLICATE_CHECKOUT:
        # The candidates are the first record's project and this
        # checkout's own (`membership_shared._ask_which`).
        ids = [p for _n, p in candidates]
        first, own = (
            _answer(question, "join", ids[i], said=True)
            if len(ids) > i
            else "(no project to join)"
            for i in (0, 1)
        )
        options = (
            f"The first record's project, {this} checkout and what was written "
            f"{here} moving there, when the user says so: {first}; {this} "
            f"checkout's own, where it is, when the user says so: {own}; a new "
            f"project of its own, with what was written {here}: {separate}"
        )
    elif kind == IMPORT:
        options = (
            f"Same project, when the user says so: {joins} (in that checkout); "
            f"keep apart: {separate}"
        )
    else:
        if kind == FIRST_SIGHT and at is not None:
            separate = (
                f"{separate} (refused where the project file {here} is bound "
                "to another project: then bettermemory project init --new"
                f"{there}, a fresh project file)"
            )
        elif kind == FIRST_SIGHT and filed:
            separate = (
                f"bettermemory project init --new ({run_in(None, question)}, "
                "a fresh project file)"
            )
        options = f"Same project, when the user says so: {joins}; separate: {separate}"
    held = (
        f" Until answered, nothing from other projects is shown {here} and what "
        f"is written {here} stays with that folder."
        if kind == FIRST_SIGHT and at is not None
        else " Until answered, nothing from other projects is shown here and what is "
        "written here stays with this folder."
        if kind == FIRST_SIGHT
        else ""
    )
    return (
        f"bettermemory: question {printable(question)}, restated from what it "
        f"stores, asks about project {names}: {said}.{held} {options}"
    )


# ---------------------------------------------------------------------------
# The lines without a number
# ---------------------------------------------------------------------------


def line_folder_project(folder: str, name: str, *, boundary: bool) -> str:
    """A workspace's project file seen for the first time: the folder
    above the repository is a project of its own."""
    return (
        f"bettermemory: {quoted(folder)} is a project of its own ("
        f"{quoted(name)}, its file)"
        + ("; a boundary: nothing crosses it" if boundary else "")
    )


def line_new_folder(root: str, name: str, project_id: str) -> str:
    """A plain folder whose project file was seen for the first time."""
    return (
        f"bettermemory: {quoted(root)} is a new project "
        f"{_named(name, project_id)}, its file"
    )


def line_boundary_added(path: str, name: str, project_id: str) -> str:
    """B12: a bound file now carries ``boundary = true``."""
    return (
        f"bettermemory: {quoted(path)} now declares a boundary: "
        f"{_named(name, project_id)} is a tenant from now on; nothing crosses it"
    )


def line_boundary_dropped(
    path: str, name: str, project_id: str, folder: str, file_id: str
) -> str:
    """B12: a bound file no longer carries the flag its project has. The
    command that lifts it runs in the file's `folder` (`run_in`), whose path
    ``bettermemory project folder`` finds by the file's id, `file_id`."""
    return (
        f"bettermemory: {quoted(path)} no longer declares a boundary; "
        f"{_named(name, project_id)} stays one until it is lifted: bettermemory "
        f"project init --rebind ({run_in(folder, file_id)}) binds a fresh file "
        "without it"
    )


def line_across_boundary(root: str, named: Sequence[str]) -> str:
    """Projects the evidence names on the other side of a boundary;
    `named` are their spellings (`_named`)."""
    return (
        f"bettermemory: {quoted(root)} matches "
        + ", ".join(printable(n) for n in named)
        + " across a boundary; kept apart"
    )


def line_content_crowded(root: str, count: int, limit: int) -> str:
    """More commit-less projects match the content than a question names."""
    return (
        f"bettermemory: {quoted(root)} has the folder name and files of "
        f"{count} commit-less projects with no live checkout, more "
        f"than the {limit} a question names, so none is offered; "
        "`bettermemory project list` shows them, `bettermemory project "
        "join <id>` joins one"
    )
