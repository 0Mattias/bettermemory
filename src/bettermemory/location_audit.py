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
from .gitfile_rule import _recorded_reading
from .location import kept_apart
from .location_git import _discover

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
    so until then the first folder seen naming it becomes its tree;
    ``ok`` otherwise; ``skip`` with no working directory. A recorded tree
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
    except (OSError, ValueError):
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
    if refused:
        first, _target, why = refused[0]
        return {
            "id": GITFILE_FINDING,
            "status": "warn",
            "summary": f"{first} names a git directory membership does not follow: {why}",
            "path": first,
            "fix": _gitfile_fix(first),
            "details": {
                "cwd": start,
                "refused": [
                    {"dotgit": d, "target": t, "reason": w} for d, t, w in refused
                ],
            },
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


def _gitfile_fix(refused: str) -> str:
    """The warning's fix for the refused `.git` file or folder `refused`
    (review M D4): a check by hand of where it points and the removal of a
    plant. It suggests no command that rewrites a worktree's registration:
    one run in a copy of a live worktree moves the live worktree's
    registration to the copy, and membership then follows the copy."""
    plant = (
        "its .git file"
        if os.path.basename(refused) == ".git"
        else "the HEAD and commondir files in it"
    )
    return (
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
