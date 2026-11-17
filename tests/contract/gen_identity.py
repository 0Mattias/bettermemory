"""Fixtures: the identity helpers of `gitfile_rule` (``gitfile_identity.jsonl``,
``gitfile_identity_python.jsonl``).

Unit 2's second part (``native/.../gitfile_rule/identity.rs``) ports
`gitfile_rule._names_here`, `_names_folder`, `_same_folder`, `_is_link`,
`_working_tree_of` and `_tree_of`; this pins each to the reference's own
function on real trees.

Each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), then each query answered.
Lines: a header, then one per case: the tree as `gen_gitdir` records it
(``"f"``, ``"d"``, ``"l"``, ``"m"``; ``"h"``: hard links, a new name to an
existing file), with ``"N"``: folder to a count of empty files ``n0`` ...
written in it (thousands of names, recorded by count). ``"q"``: one
answer a query ``op:args`` (arguments split at ``|``; a path relative to
the case's directory, or under ``<root>``): ``h`` `_names_here(raw, base,
here)`; ``F`` `_names_folder(raw, base, directory)`; ``s``
`_same_folder(a, b)`; ``k`` `_is_link(os.lstat(path))`; ``t``
`_tree_of(path)` and `_working_tree_of(path)` (``"w"``); ``G``
`_gitfile_target(path)`; ``T`` `_small_text(path)` (the text spelled as
a path is). A path answered is spelled with the case's directory as
``<scratch>``; an exception is ``{"e": class}`` with ``"n"`` the errno's
name for an OSError, the class alone otherwise. The identity helpers
catch what they ask; a ValueError (a NUL) rises out of ``G`` and ``T``.

Which file a query goes in is decided by its input: every query of a
tree holding a loop goes in the Python-gated file. Gates: POSIX only
(links, hard links, modes). Names in NFC against NFD are not here: the
platforms answer them differently (tested natively). Nothing depends on
root (`refuse_root`), the umask, the host's users, case folding,
``listdir``'s order or the scratch path; the shared generator checks
apply.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bettermemory.gitfile_rule import (
    _gitfile_target,
    _small_text,
    _is_link,
    _names_folder,
    _names_here,
    _same_folder,
    _tree_of,
    _working_tree_of,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import Tree, _b, check_length, check_parent_of_file, refuse_root
from .gen_fs_stat import _error


class Case(Tree):
    def __init__(
        self, queries: list[str], *, many: dict[str, int] | None = None, **kw: Any
    ) -> None:
        super().__init__(queries, **kw)
        self.many = many or {}

    def record(self) -> dict[str, Any]:
        out = super().record()
        if self.many:
            out["N"] = dict(self.many)
        return out


def _trees() -> Iterator[Case]:
    # The same folder: by a link, by `..`, by a trailing slash, by a hard
    # link inside; and not: equal names in other parents, a link to a
    # sibling, a dangling link, a file, nothing, a NUL, a mode-000 parent.
    yield Case(
        [
            "s:<root>/a|<root>/a",
            "s:<root>/la|<root>/a",
            "s:<root>/a/sub/..|<root>/a",
            "s:<root>/a/|<root>/a",
            "s:a|<root>/a",
            "s:<root>/a/f|<root>/b/hf",
            "s:<root>/a/x|<root>/b/x",
            "s:<root>/lb|<root>/a",
            "s:<root>/dangle|<root>/a",
            "s:<root>/a/f|<root>/a",
            "s:<root>/missing|<root>/a",
            "s:<root>/a|<root>/missing",
            "s:<root>/a/f/x|<root>/a",
            "s:<root>/a\x00b|<root>/a",
            "s:<root>/a|<root>/a\x00b",
            "s:<root>/locked/x|<root>/locked/x",
            "s:<root>/a|<root>/locked/x",
            "s:|<root>/a",
            "s:.|<root>",
            "h:x|<root>/a|<root>/a/x",
            "h:<root>/a/x|<root>|<root>/b/x",
            "h:../a/x|<root>/b|<root>/a/x",
            "h:f|<root>/a|<root>/b/hf",
            "h:nothing|<root>/a|<root>/a",
            "h:x|<root>/a|<root>/missing",
            "h:x|<root>/locked|<root>/a",
            "h:f/x|<root>/a|<root>/a",
            "h:a\x00|<root>|<root>/a",
            "h:|<root>/a|<root>/a",
            "h:<root>/dangle||<root>/a",
            "k:<root>/la",
            "k:<root>/dangle",
            "k:<root>/a",
            "k:<root>/a/f",
            "k:<root>/missing",
            "k:<root>/locked/x",
        ],
        files={"a/f": "f", "a/x": "1", "b/x": "1", "locked/x/y": ""},
        dirs=("a/sub", "b"),
        links={"la": "a", "lb": "b", "dangle": "nothing"},
        hard={"b/hf": "a/f"},
        modes={"locked": 0o000},
    )
    # Worktree `gitdir` back-pointers: agree, disagree, missing, through
    # links, by another path; and the folder whose `.git` an entry is.
    yield Case(
        [
            "F:<root>/wt/.git|<root>/main/.git/worktrees/wt|<root>/wt",
            "F:<root>/wt/.git|<root>/main/.git/worktrees/wt|<root>/other",
            "F:<root>/gone/.git|<root>/main/.git/worktrees/wt|<root>/gone",
            "F:<root>/lwt/.git|<root>/main/.git/worktrees/wt|<root>/wt",
            "F:<root>/wt/.git|<root>/main/.git/worktrees/wt|<root>/lwt",
            "F:<root>/lother/../wt/.git|<root>/main|<root>/wt",
            "F:../../../../wt/.git|<root>/main/.git/worktrees/wt|<root>/wt",
            "F:wt/.git||<root>/wt",
            "F:wt||<root>",
            "F:<root>/wt/./.git/|<root>|<root>/wt",
            "F:<root>/wt/dangle|<root>|<root>/wt",
            "F:<root>/locked/.git|<root>|<root>/locked",
            "F:<root>/wt/.git\x00|<root>|<root>/wt",
            "F:|<root>|<root>",
            "t:<root>/main/.git",
            "t:main/.git",
            "t:<root>/wt/.git",
            "t:<root>/lwt/.git",
            "t:<root>/ldot",
            "t:<root>/alone/.git",
            "t:<root>/lalone",
            "t:<root>/crowd/.git",
            "t:<root>/main",
            "t:<root>/main/.git/worktrees",
            "t:<root>/locked/.git",
            "t:<root>/unlisted/.git",
            "t:<root>/odd/.git",
            "t:<root>/gone/.git",
            "t:<root>/wt/dangle",
            "t:<root>/a\x00b",
            "t:",
        ],
        files={
            "main/.git/HEAD": "ref: refs/heads/main\n",
            "main/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
            "wt/.git": "gitdir: <root>/main/.git/worktrees/wt\n",
            "wt/f": "",
            "unlisted/.git/HEAD": "",
            "odd/.git/HEAD": "",
            "odd/.gi": "",
            "odd/.git.": "",
            "alone/.git/HEAD": "",
        },
        dirs=("other", "locked/.git", "crowd/.git"),
        links={
            "lwt": "wt",
            "lother": "other",
            "ldot": "main/.git",
            "lalone": "alone/.git",
            "wt/dangle": "nothing",
        },
        many={"crowd": 3000},
        modes={"locked": 0o000, "unlisted": 0o300},
    )
    # A NUL at the start, middle and end of a path, relative and
    # absolute: `open` raises ValueError, which `_gitfile_target` and
    # `_small_text` do not catch; the identity helpers catch it.
    nul = ["\x00w/.git", "w/\x00.git", "w/.git\x00"]
    nul += [f"<root>/{p}" for p in nul]
    yield Case(
        [f"{op}:{p}" for p in nul for op in "GTt"]
        + [f"s:{p}|<root>/w" for p in nul]
        + [f"s:<root>/w|{p}" for p in nul]
        + [f"h:{p}|<root>|<root>/w/.git" for p in nul]
        + [f"F:{p}|<root>|<root>/w" for p in nul]
        + ["G:w/.git", "T:w/.git", "G:<root>/w/.git", "T:<root>/w/note"],
        files={"w/.git": "gitdir: <root>/g\n", "w/note": " text \n"},
    )
    # Both stats answer, naming other files: a git directory whose
    # folder's `.git` is another folder; a `.git` link to a sibling's
    # `.git`, and to a sibling's other folder.
    yield Case(
        [
            "t:<root>/p/repo",
            "t:<root>/q/.git",
            "t:<root>/r/.git",
            "t:<root>/sib/x",
        ],
        dirs=("p/.git", "p/repo", "sib/.git", "sib/x", "q", "r"),
        files={"sib/f": ""},
        links={"q/.git": "../sib/.git", "r/.git": "../sib/x"},
    )
    # A loop: `_tree_of`'s `realpath` (gated by input).
    yield Case(
        ["t:<root>/s", "t:<root>/s/.git", "s:<root>/s|<root>"],
        links={"s": "s"},
        loop=True,
    )


def _spell(answer: str, real: str) -> str:
    return _b(answer.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))


def _call(fn: Any, real: str) -> Any:
    try:
        out = fn()
    except (OSError, ValueError) as exc:
        return _error(exc)
    if isinstance(out, str):
        return _spell(out, real)
    return out


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    for a in args:
        check_length(a, links, real)
        check_parent_of_file(a)
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    if op == "h":
        out["v"] = _call(lambda: _names_here(*args), real)
    elif op == "F":
        out["v"] = _call(lambda: _names_folder(*args), real)
    elif op == "s":
        out["v"] = _call(lambda: _same_folder(*args), real)
    elif op == "k":
        out["v"] = _call(lambda: _is_link(os.lstat(args[0])), real)
    elif op == "G":
        out["v"] = _call(lambda: _gitfile_target(args[0]), real)
    elif op == "T":
        out["v"] = _call(lambda: _small_text(args[0]), real)
    elif op == "t":
        out["v"] = _call(lambda: _tree_of(args[0]), real)
        out["w"] = _call(lambda: _working_tree_of(args[0]), real)
    else:
        raise ValueError(op)
    return out


def _answer(tree: Case, root: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        base = Path(real)
        for d in tree.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
            os.chmod(base / d, 0o755)
        for rel, value in tree.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o755)
            path.write_bytes(
                value.replace("<root>", real).encode("utf-8", "surrogateescape")
            )
            os.chmod(path, 0o644)
        for folder, n in tree.many.items():
            (base / folder).mkdir(parents=True, exist_ok=True)
            for i in range(n):
                (base / folder / f"n{i}").write_bytes(b"")
        for rel, target in tree.links.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        for rel, target in tree.hard.items():
            os.link(base / target, base / rel)
        by_depth = sorted(tree.modes, key=lambda rel: rel.count("/"))
        for rel in reversed(by_depth):
            os.chmod(base / rel, tree.modes[rel])
        try:
            return [_query(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in by_depth:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


def gated(query: str, tree: Tree) -> bool:
    """Whether a query goes in the Python-gated file, by its input."""
    return tree.loop


def _lines(want_gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitfile-identity"
        for i, tree in enumerate(_trees()):
            queries = [q for q in tree.queries if gated(q, tree) == want_gated]
            if not queries:
                continue
            asked = Case(
                queries,
                files=tree.files,
                dirs=tree.dirs,
                links=tree.links,
                modes=tree.modes,
                hard=tree.hard,
                loop=tree.loop,
                many=tree.many,
            )
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(asked, root)
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(False)
    header = {"fixture": "gitfile_identity", "cases": len(lines)}
    return {"gitfile_identity.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(True)
    header = {"fixture": "gitfile_identity_python", "cases": len(lines)}
    return {"gitfile_identity_python.jsonl": jsonl([header, *lines])}
