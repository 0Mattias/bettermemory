"""The security audit's finding on the ``.git`` entries at the working
directory and above (ENG-158, ENG-206): `gitfile_posture`, which
`posture.audit` carries as ``membership.gitfile``.

It reads what discovery reads (`location_git._discover`, with each
``.git`` entry it passes over and why) and the working tree recorded for
a git directory kept apart (`gitdir_pin`), within the bound discovery
reads it within (`gitfile_rule._recorded_reading`), and writes nothing.
It lives apart from `location_git` to keep that module under the
package's 2,000-line bound (ENG-96), and imports it and `location`,
never the reverse.
"""

from __future__ import annotations

import os
from typing import Any

from . import gitdir_pin
from .gitfile_rule import UNREAD_TREE, _recorded_reading, _small_text, names_git_dir
from .location import kept_apart
from .location_git import GitDies, _discover

#: The security audit's finding id for the ``.git`` files above the cwd.
GITFILE_FINDING = "membership.gitfile"


def gitfile_posture(cwd: str | None) -> dict[str, Any]:
    """The security audit's finding on the ``.git`` files at `cwd` (the
    working directory when None) and above (ENG-158), as the keyword
    arguments of `posture.Finding`: ``warn`` naming each ``.git`` file
    git's discovery walk passes there that membership does not follow
    (`gitfile_refusal`; git itself still follows it, so a git command
    there acts on the repository it names), and each folder holding
    ``HEAD`` and a ``commondir`` naming a repository it is not registered
    under, with a check by hand as its fix (`_gitfile_fix`); ``info``
    where the repository found is a ``.git`` file naming a git directory
    kept apart (``--separate-git-dir``) with no working tree recorded for
    it yet (`gitdir_pin`, ENG-206), which the next capture here records,
    so until then the first folder seen naming it becomes its tree, and
    where every refusal is of a layout that is no plant (`_layout`: a bare
    repository cloned into ``.git`` with its linked worktrees beside it,
    the inside of a submodule's git directory), which says where sessions
    belong and no fix touches its config; ``ok``
    otherwise; ``skip`` with no working directory. A recorded tree
    that stopped answering since discovery read it warns as discovery's
    refusal would. Reads only."""
    try:
        start = os.path.realpath(cwd if cwd is not None else os.getcwd())
    except (OSError, ValueError):
        return {
            "id": GITFILE_FINDING,
            "status": "skip",
            "summary": "no working directory to walk up from",
        }
    refused: list[tuple[str, str, str]] = []
    try:
        found = _discover(start, refused)
    except (OSError, ValueError, GitDies):
        # A git directory git dies on is in `refused` already.
        found = None
    record: tuple[gitdir_pin.Pin | None, bool] | None = None
    if (
        not refused
        and found is not None
        and found.dotgit is not None
        and found.root is not None
        and kept_apart(found)
    ):
        pin = gitdir_pin.recorded(found.git_dir)
        why, here = _recorded_reading(pin, found.git_dir, found.root)
        if why is None:
            record = (pin, here)
        else:
            refused.append((found.dotgit, found.git_dir, why))
    # A layout that is no plant, where sessions belong elsewhere (`_layout`),
    # is told, never warned of, and no fix touches its config.
    layouts = [(r[0], _layout(*r)) for r in refused]
    told = next(((path, said) for path, said in layouts if said), None)
    refused = [r for r, (_, said) in zip(refused, layouts, strict=True) if not said]
    if refused:
        first, target, why = refused[0]
        return {
            "id": GITFILE_FINDING,
            "status": "warn",
            "summary": f"{first} names a git directory membership does not follow: {why}",
            "path": first,
            "fix": _gitfile_fix(first, why, target),
            "details": {
                "cwd": start,
                "refused": [
                    {"dotgit": d, "target": t, "reason": w} for d, t, w in refused
                ],
            },
        }
    if told is not None:
        return {
            "id": GITFILE_FINDING,
            "status": "info",
            "summary": told[1],
            "path": told[0],
            "details": {"cwd": start},
        }
    dotgit = None if found is None else found.dotgit
    details: dict[str, Any] = {"cwd": start, "dotgit": dotgit}
    if found is not None and record is not None:
        pin, here = record
        kept = {**details, "target": found.git_dir}
        if pin is not None and here:
            return {
                "id": GITFILE_FINDING,
                "status": "ok",
                "summary": (
                    f"{dotgit} names {found.git_dir}, a git directory kept apart "
                    "(--separate-git-dir) recorded with this folder as its working "
                    "tree; a .git elsewhere naming it is not followed"
                ),
                "details": {**kept, "recorded": pin.tree},
            }
        recorded = None if pin is None else pin.tree
        if not gitdir_pin.writable():
            return {
                "id": GITFILE_FINDING,
                "status": "warn",
                "summary": (
                    f"{dotgit} names {found.git_dir}, a git directory kept apart "
                    "(--separate-git-dir), and no working tree can be recorded "
                    f"for it: {gitdir_pin.pins_dir()} cannot be written, so a .git "
                    "elsewhere naming it is followed too"
                ),
                "path": dotgit,
                "fix": (
                    "make the state directory writable by this account (or point "
                    "BETTERMEMORY_STATE_DIR at one that is), then start a session "
                    "here"
                ),
                "details": {**kept, "recorded": recorded},
            }
        return {
            "id": GITFILE_FINDING,
            "status": "info",
            "summary": (
                f"{dotgit} names {found.git_dir}, a git directory kept apart "
                "(--separate-git-dir) with no working tree recorded yet; the "
                "next session here records this folder, unless this machine's "
                "store saw the git directory first at another folder that still "
                "names it, and no .git elsewhere naming it is followed after that"
            ),
            "path": dotgit,
            "fix": (
                "if this checkout was not made with --separate-git-dir, remove "
                "the .git file before a session runs here: until a working tree "
                "is recorded, the first folder seen naming the git directory "
                "becomes it"
            ),
            "details": {**kept, "recorded": recorded},
        }
    return {
        "id": GITFILE_FINDING,
        "status": "ok",
        "summary": (
            "no .git file at or above the working directory names a git directory "
            "membership does not follow"
        ),
        "details": details,
    }


def _layout(refused: str, target: str, why: str) -> str | None:
    """The finding's summary where the refusal `why` of `refused` (which
    names the git directory `target`) is of a layout that is no plant and
    where a session belongs elsewhere, None otherwise (ENG-211): a bare
    repository cloned into ``.git`` with its linked worktrees beside it
    (``git clone --bare <url> .git``, then ``git worktree add``,
    `_linked_trees`), whose folder git reads no working tree in, and the
    inside of a git directory other than a ``.git`` whose config names its
    working tree (a submodule's under ``.git/modules``), which git roots in
    that tree and membership joins nothing from. Neither is followed, a
    session belongs in a working tree, and no config there wants a
    change."""
    if refused != target:
        return None
    trees = _linked_trees(target, why)
    if trees:
        return (
            f"{target} is a bare repository whose working trees are its linked "
            f"worktrees ({', '.join(trees)}): git reads no working tree in "
            f"{os.path.dirname(target)}, and sessions belong inside one of the "
            "worktrees"
        )
    if os.path.basename(target) != ".git" and why.endswith("(core.worktree)"):
        return (
            f"{target} is the inside of a git directory, where membership "
            f"joins no project ({why}): sessions belong in the working tree "
            "it names"
        )
    return None


def _linked_trees(target: str, why: str) -> list[str]:
    """The working trees of the linked worktrees registered under the git
    directory `target`, where it is a folder's ``.git`` refused for its
    ``core.bare`` (`gitfile_rule.own_refusal`): a bare repository cloned
    into ``.git`` with ``git worktree add`` run beside it. Each is the
    folder of the ``.git`` that a ``<target>/worktrees/<name>/gitdir`` file
    names and that names that directory back
    (`gitfile_rule.names_git_dir`). Empty for any other refusal."""
    if os.path.basename(target) != ".git" or not why.endswith("(core.bare)"):
        return []
    trees: list[str] = []
    try:
        with os.scandir(os.path.join(target, "worktrees")) as entries:
            for entry in entries:
                back = _small_text(os.path.join(entry.path, "gitdir"))
                if not back:
                    continue
                dotgit = os.path.normpath(os.path.join(entry.path, back))
                if names_git_dir(dotgit, entry.path):
                    trees.append(os.path.dirname(dotgit))
    except OSError:
        return []
    return sorted(trees)


def _gitfile_fix(refused: str, why: str, target: str) -> str:
    """The warning's fix for the refused `.git` file or folder `refused`,
    which names the git directory `target`, not followed for the reason
    `why` (review M D4): a check by hand of where it points and the removal
    of a plant. It suggests no command that rewrites a worktree's
    registration: one run in a copy of a live worktree moves the live
    worktree's registration to the copy, and membership then follows the
    copy. Where the working tree recorded for the git directory could not
    be read or did not answer (`gitfile_rule.UNREAD_TREE`, ENG-217), it
    says first how that record is freed: the recorded folder's .git read
    again, or its share unmounted. Where `refused` is a git directory
    itself refused for what its own config names (`gitfile_rule.own_refusal`
    and `inside_refusal`, ENG-211), it says how to make that config name
    this folder."""
    if refused == target and why.startswith(f"{target}'s config "):
        return (
            "git reads it as a git directory whose working tree is not the "
            "folder it sits in: its config names another folder or an empty "
            "one (core.worktree) or none (core.bare), or it is a config git "
            "dies on or larger than this check reads. A git command here acts "
            "on the tree it names, or fails, and neither membership nor the "
            "origin of a record written here follows it. If this folder is its "
            "working tree, remove core.worktree and a core.bare of true from "
            "that config (git config --unset), or mend what git reports on it; "
            "the next session here follows it. If it came from an archive, a "
            "copy or someone else, remove it."
        )
    plant = (
        "its .git file"
        if os.path.basename(refused) == ".git"
        else "the HEAD and commondir files in it"
    )
    unread = ""
    if why.endswith(UNREAD_TREE):
        unread = (
            "The working tree recorded for this git directory could not be read "
            "or did not answer, and while that lasts no other folder naming the "
            "git directory is followed. If this folder is that checkout, moved "
            "here from a share that hangs or answers with errors, fix that share "
            "or unmount it (an unmounted share reads as gone); if the recorded "
            ".git is a file provider's placeholder, or this account cannot read "
            "it, bring it back to this disk or make it readable. Once that .git "
            "reads, the record stays with that folder while it names the git "
            "directory; once it is gone, the next session here records this "
            "folder. "
        )
    return unread + (
        "git still follows it, so a git command here acts on the repository it "
        "names. Check by hand where it points: a worktree's .git file names "
        "<common>/worktrees/<name>, whose gitdir file names that .git file back, "
        "a submodule's names a git directory whose core.worktree names this "
        "folder, and a --separate-git-dir directory is followed only from the "
        "working tree recorded for it, the first seen; any other is not this "
        "folder's. If this folder came from an archive, a copy or someone else, "
        f"remove {plant}. If it is the --separate-git-dir checkout's tree and "
        "the recorded one is a copy you no longer use, remove that copy's .git "
        "file; the next session here records this folder."
    )
