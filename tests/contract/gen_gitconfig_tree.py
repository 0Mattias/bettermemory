"""Fixtures: a git config file and its plain includes
(``gitconfig_tree.jsonl``) and the path rules an include follows
(``gitconfig_tree_rules.jsonl``).

The native build reads a config file with its ``include.path`` lines
(``native/crates/bettermemory-core/src/gitconfig/tree.rs``); this pins it
to `gitconfig.config_tree` without conditions: `_read_config`,
`_parse_config`, `_include`, `_include_target`, `_interpolate`,
`_git_absolute`, `_native`, `_home`, `_user_home`, the depth of 10, the
64 files, cycles by identity and the remote rules where a context asks
for them, and the ``gitdir:``, ``gitdir/i:`` and ``onbranch:``
conditions. A ``hasconfig:remote.*.url:`` one is the next unit's: a case
holding one records the reference's answer, and the native read declines
it.

``gitconfig_tree.jsonl``: each case is a tree as data, written as real
files in a fresh directory of the scratch world (`_sandbox.world`), and
read by the reference with HOME, the ``~user`` accounts (``pwd`` stood in
for, by name) and the context's ``%(prefix)`` given. A line holds
``"f"``, ``"d"``, ``"l"``, ``"m"`` as in ``gitconfig_file.jsonl``;
``"t"``, the file read; ``"k"``, the read's keywords (``s``:
``stamp_self``, ``g``: ``gentle``, ``c``: whether a context is given,
``p``: its prefix, relative to the case's directory, ``"%(prefix)"``, or
null; ``gd``: the git directory, relative; ``b``: the branch, null for HEAD
elsewhere, left out for not known); ``"hm"``: HOME relative to the case's directory, or null for unset;
``"u"``: accounts, name to home relative to the case's directory; and
``"r"``: the answer: ``v`` the values as ``[section, subsection, key,
[value, ...]]`` (bytes spelled as in ``gitconfig_file.jsonl``), ``c``
complete, ``ud`` the undecided values and ``md`` ``maybe_dies`` where
any, ``d`` why git dies, ``fl`` the files read in order, ``np`` the
``includeIf`` subsection a native read stops at (computed here from the
values, then the undecided ones: the first ``hasconfig:remote.*.url:``
one at ``path``, or at any key under a context). Each case's directory is
taken as ``realpath`` gives it, so the only links a path passes through
are the case's own. Texts name the case's directory
``<scratch>``. POSIX only (`generate.POSIX_ONLY`): links, FIFOs and
modes, and the reference's ``os.path`` and ``pwd`` are the host's. No
case holds 30 links or more, and nothing depends on case folding,
normalization or the Python release.

``gitconfig_tree_rules.jsonl``: `_git_absolute`, `_native` and
`_include_target` (with `_interpolate`, ``os.path.dirname`` and
``os.path.join``) of text alone, for both path flavours: the Windows
flavour is the reference's own code run with its ``os`` module standing
in as Windows' (``ntpath``, ``os.name`` ``nt``, ``sys.platform``
``win32``), HOME and the profile given. Each line: ``w`` the flavour,
``x`` the value, ``i`` the including file, ``h`` HOME (null: unset),
``pf`` the profile, ``u`` the accounts, ``p`` the prefix, and ``r``:
``a`` absolute, ``n`` native, ``t`` the target or ``{"d": why}`` or
``"over"``, texts spelled as values are (``=`` and printable ASCII
without a backslash, else hex, so no host-like ``\\`` appears). The rules read no file and no host fact (``os.fsdecode`` and
``pwd`` stand in as POSIX has them, on any host), so the file is
ungated: the same bytes under Python 3.11 and 3.13 (compared), on macOS,
Linux and Windows. Windows values are UTF-8, as Python decodes them there.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import gitconfig

from . import _sandbox
from ._common import jsonl
from .gen_gitconfig_file import _b, _content, _expand

_Content = bytes | list[tuple[bytes, int]]


def _text(data: bytes) -> Any:
    try:
        return {"s": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"x": data.hex()}


class Tree:
    def __init__(
        self,
        target: str,
        files: dict[str, _Content],
        *,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        modes: dict[str, int] | None = None,
        fifos: tuple[str, ...] = (),
        home: str | None = "h",
        users: dict[str, str] | None = None,
        stamp_self: bool = True,
        gentle: bool = False,
        context: bool = False,
        prefix: str | None = None,
        git_dir: str | None = None,
        branch: str | None = "\0",
    ) -> None:
        self.target = target
        self.files = files
        self.dirs = dirs
        self.links = links or {}
        assert len(self.links) < 30, target
        self.modes = modes or {}
        self.fifos = fifos
        self.home = home
        self.users = users or {}
        self.ask: dict[str, Any] = {
            "s": stamp_self,
            "g": gentle,
            "c": context,
            "p": prefix,
        }
        if git_dir is not None:
            self.ask["gd"] = git_dir
        if branch != "\0":
            # A branch name, null for HEAD elsewhere; left out: unsettled.
            self.ask["b"] = branch

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": {k: _content(v) for k, v in self.files.items()}}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.links:
            out["l"] = dict(self.links)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.modes:
            out["m"] = {k: f"{v:03o}" for k, v in self.modes.items()}
        out["t"] = self.target
        out["k"] = self.ask
        out["hm"] = self.home
        if self.users:
            out["u"] = self.users
        return out


_KNOWN = ("gitdir:", "gitdir/i:", "onbranch:", "hasconfig:remote.*.url:")


def _not_ported(read: gitconfig.ConfigValues, context: bool) -> str | None:
    """The first ``hasconfig:remote.*.url:`` ``includeIf`` the reference
    acts on, which a native read declines (the collecting and forbidding
    pass is not ported): at ``path``, or at another key where a context is
    given (`_parse_config` 436-444). Values first, then undecided ones."""
    for names in (read, read.undecided):
        for section, sub, key in names:
            if section != "includeif" or sub is None:
                continue
            if sub.startswith(_KNOWN[3]) and (key == "path" or context):
                return _b(gitconfig.subsection_bytes(sub))
    return None


def _values(names: dict[Any, Any]) -> list[Any]:
    return [
        [
            section,
            None if sub is None else _b(gitconfig.subsection_bytes(sub)),
            key,
            [None if v is None else _b(v) for v in found_values],
        ]
        for (section, sub, key), found_values in names.items()
    ]


def _tree_answer(case: Tree, root: Path) -> dict[str, Any]:
    # The directory as `realpath` gives it, so a link the case makes is the
    # only one a path passes through (macOS's /var is a link).
    root = Path(os.path.realpath(root))
    for d in case.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    for rel in case.fifos:
        os.mkfifo(root / rel)  # type: ignore[attr-defined,unused-ignore]
    by_depth = sorted(case.modes, key=lambda rel: rel.count("/"))
    for rel in reversed(by_depth):
        os.chmod(root / rel, case.modes[rel])
    prefix = str(root)

    def label(text: str) -> str:
        return text.replace(prefix, "<scratch>")

    def getpwnam(name: str) -> Any:
        if name in case.users:
            return SimpleNamespace(pw_dir=prefix + "/" + case.users[name])
        raise KeyError(name)

    env = {} if case.home is None else {"HOME": prefix + "/" + case.home}
    context = None
    if case.ask["c"]:
        given: str | None = case.ask["p"]
        found = (
            None
            if given is None
            else given
            if given == "%(prefix)"
            else prefix + "/" + given
        )
        branch: bytes | None = (
            gitconfig.UNSETTLED
            if "b" not in case.ask
            else None
            if case.ask["b"] is None
            else os.fsencode(case.ask["b"])
        )
        context = gitconfig.IncludeContext(lambda: branch, prefix=lambda: found)
    pwd = __import__("importlib").import_module("pwd")
    try:
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(pwd, "getpwnam", getpwnam),
        ):
            if case.home is None:
                os.environ.pop("HOME", None)
            read = gitconfig.config_tree(
                prefix + "/" + case.target,
                stamped=True,
                stamp_self=bool(case.ask["s"]),
                gentle=bool(case.ask["g"]),
                context=context,
                git_dir=None if "gd" not in case.ask else prefix + "/" + case.ask["gd"],
            )
    finally:
        for rel in by_depth:
            os.chmod(root / rel, 0o700)
    out: dict[str, Any] = {"v": _values(read)}
    if read.undecided:
        out["ud"] = _values(read.undecided)
    if read.maybe_dies is not None:
        out["md"] = _text(os.fsencode(label(read.maybe_dies)))
    return out | {
        "c": read.complete,
        "d": None if read.dies is None else _text(os.fsencode(label(read.dies))),
        "fl": [_text(os.fsencode(label(p))) for p, _ in read.files or ()],
        "np": _not_ported(read, bool(case.ask["c"])),
    }


P = b"[core]\n\tbare = false\n"


def _cond(condition: str, path: bytes = b"x") -> bytes:
    return b'[includeIf "' + condition.encode() + b'"]\n\tpath = ' + path + b"\n"


def _inc(*paths: bytes) -> bytes:
    return b"".join(b"[include]\n\tpath = " + p + b"\n" for p in paths)


def _trees() -> Iterator[Tree]:
    # One file, missing, a refused line, values with NULs and no value.
    yield Tree("c", {"c": P + b"[a]\n\tb\n\tb = x\x00y\n[a]\n\tB = 2\n"})
    yield Tree("missing", {})
    yield Tree("c", {"c": b"[a\n"})
    yield Tree("c", {"c": P}, stamp_self=False)
    yield Tree("c", {"c": P}, modes={"c": 0}, gentle=True)
    yield Tree("c", {"c": P}, modes={"c": 0})
    # include.path: relative, absolute (relabelled in), nested directories,
    # a missing file, a directory, valueless, empty, `.`.
    yield Tree("c", {"c": _inc(b"x") + b"[a]\n\tafter = 1\n", "x": P})
    yield Tree("c", {"c": _inc(b"s/x"), "s/x": _inc(b"y"), "s/y": P})
    yield Tree("c", {"c": _inc(b"s/../x"), "x": P}, dirs=("s",))
    yield Tree("c", {"c": _inc(b"missing") + P})
    yield Tree("c", {"c": _inc(b"d") + P}, dirs=("d",))
    yield Tree("c", {"c": b"[include]\n\tpath\n" + P})
    yield Tree("c", {"c": _inc(b"") + P})
    yield Tree("c", {"c": _inc(b".") + P})
    yield Tree("c", {"c": _inc(b'"x"') + P, "x": P})
    yield Tree("c", {"c": _inc(b"x/") + P, "x": P})
    yield Tree(
        "c", {"c": b"[include]\n\tPATH = x\n[Include]\n\tpath = y\n", "x": P, "y": P}
    )
    yield Tree("c", {"c": b'[include "sub"]\n\tpath = x\n', "x": P})
    # A tab or a CR in a value; a NUL cuts it first.
    yield Tree("c", {"c": b'[include]\n\tpath = "x\\ty"\n', "x\ty": P})
    yield Tree("c", {"c": b"[include]\n\tpath = x\ry\n" + P})
    yield Tree("c", {"c": b"[include]\n\tpath = x\x00\ty\n", "x": P})
    # `~/`, `~` alone, `~user/`, an unknown user, HOME unset, HOME empty.
    yield Tree("c", {"c": _inc(b"~/x"), "h/x": P})
    yield Tree("c", {"c": _inc(b"~"), "h": P})
    yield Tree("c", {"c": _inc(b"~u/x"), "uh/x": P}, users={"u": "uh"})
    yield Tree("c", {"c": _inc(b"~nobody/x") + P})
    yield Tree("c", {"c": _inc(b"~/x") + P}, home=None)
    yield Tree("c", {"c": _inc(b"~\xff/x") + P})
    yield Tree("c", {"c": _inc(b"x\xff~") + P})
    yield Tree("c", {"c": _inc(b"~u") + P, "uh": P}, users={"u": "uh"})
    # `%(prefix)/`: no context, a context not knowing it, a directory,
    # as written, an absolute rest.
    for context, prefix in (
        (False, None),
        (True, None),
        (True, "pf"),
        (True, "%(prefix)"),
    ):
        yield Tree(
            "c",
            {"c": _inc(b"%(prefix)/x") + P, "pf/x": P, "%(prefix)/x": P},
            context=context,
            prefix=prefix,
        )
    yield Tree("c", {"c": _inc(b"%(prefix)") + P})
    # Links: the including file through a link (its directory is the
    # link's, as written), a link included, a dangling one.
    yield Tree(
        "l/c",
        {"d/c": _inc(b"x"), "l/x": P, "d/x": b"[d]\n\tx = 1\n"},
        links={"l/c": "../d/c"},
    )
    yield Tree("c", {"c": _inc(b"l"), "x": P}, links={"l": "x"})
    yield Tree("c", {"c": _inc(b"l") + P}, links={"l": "missing"})
    yield Tree("c", {"c": _inc(b"p") + P}, fifos=("p",))
    yield Tree("c", {"c": _inc(b"x") + P, "x": P}, modes={"x": 0})
    # Cycles: one file naming itself, two naming each other, through a
    # link, and the same file twice (no cycle).
    yield Tree("c", {"c": _inc(b"c") + P})
    yield Tree("c", {"c": _inc(b"x") + P, "x": _inc(b"c")})
    yield Tree("c", {"c": _inc(b"l") + P}, links={"l": "c"})
    yield Tree("c", {"c": _inc(b"x", b"x"), "x": P})
    # Depth: a chain of 9, 10 and 11 includes; one ending in a missing file.
    for n in (9, 10, 11):
        files: dict[str, _Content] = {
            f"f{i}": _inc(f"f{i + 1}".encode()) for i in range(n)
        }
        files[f"f{n}"] = P
        yield Tree("f0", files)
    files = {f"f{i}": _inc(f"f{i + 1}".encode()) for i in range(12)}
    yield Tree("f0", files)
    # Files: 63, 64 and 65 in all, missing ones counted.
    for n in (62, 63, 64):
        yield Tree("c", {"c": _inc(*[b"x"] * n), "x": P})
    yield Tree("c", {"c": _inc(*[b"missing"] * 63) + _inc(b"x"), "x": P})
    # Size: an include past the bound.
    yield Tree("c", {"c": _inc(b"x") + P, "x": [(b"#", (1 << 21) + 1)]})
    # Remote rules with a context and without.
    for context in (False, True):
        yield Tree(
            "c", {"c": _inc(b"x"), "x": b'[remote "o"]\n\turl\n'}, context=context
        )
        yield Tree(
            "c",
            {"c": b'[remote "\xff"]\n\tfetch = +refs/heads/*:refs/x/*\n\tpushurl\n'},
            context=context,
        )
    # gitdir: and gitdir/i: against a git directory `r/g` (and through a
    # link), the including file `c` at the top; the include names `x`.
    x = {"x": b"[x]\n\ty = 1\n"}
    for kind in ("gitdir", "gitdir/i"):
        for pattern in (
            "g",
            "g/",
            "r/g",
            "r/g/",
            "**/g",
            "**/g/",
            "*/g",
            "r/*",
            "r/",
            "r",
            "R/G/",
            "R/g",
            "/",
            "**",
            "g/**",
            "r/?",
            "r/[gh]/",
            "r/[!g]/",
            "rg",
            "~/g",
            "~/g/",
            "~",
            "~u/g/",
            "~nobody/g/",
            "./r/g/",
            "./r/",
            "./g/",
            ".\\r/g/",
            "%(prefix)/g",
            "r\\g",
            "r//g/",
        ):
            yield Tree(
                "c",
                {"c": _cond(kind + ":" + pattern), **x},
                dirs=("r/g",),
                git_dir="r/g",
                home="r",
                users={"u": "r"},
            )
    for pattern in ("r/g/", "lg/", "l/g/", "**/lg", "./l/g/"):
        yield Tree(
            "c",
            {"c": _cond("gitdir:" + pattern), **x},
            dirs=("r/g",),
            links={"lg": "r/g", "l": "r"},
            git_dir="lg",
        )
        yield Tree(
            "c",
            {"c": _cond("gitdir:" + pattern), **x},
            dirs=("r/g",),
            links={"l": "r"},
            git_dir="l/g",
        )
    # `./` from an including file reached through a link, and from an
    # included file in another directory.
    yield Tree(
        "lc",
        {"r/c": _cond("gitdir:./g/"), "r/x": P},
        dirs=("r/g",),
        links={"lc": "r/c"},
        git_dir="r/g",
    )
    yield Tree(
        "c",
        {"c": _inc(b"r/i"), "r/i": _cond("gitdir:./g/"), "r/x": P},
        dirs=("r/g",),
        git_dir="r/g",
    )
    # A held condition whose include is missing, a directory, valueless,
    # a cycle, or itself conditional; an unsettled one that dies or
    # overflows (maybe_dies) and nests another.
    yield Tree(
        "c", {"c": _cond("gitdir:**/g/", b"missing") + P}, dirs=("r/g",), git_dir="r/g"
    )
    yield Tree(
        "c", {"c": _cond("gitdir:**/g/", b"r") + P}, dirs=("r/g",), git_dir="r/g"
    )
    yield Tree(
        "c",
        {"c": b'[includeIf "gitdir:**/g/"]\n\tpath\n' + P},
        dirs=("r/g",),
        git_dir="r/g",
    )
    yield Tree(
        "c", {"c": _cond("gitdir:**/g/", b"c") + P}, dirs=("r/g",), git_dir="r/g"
    )
    yield Tree(
        "c",
        {"c": _cond("gitdir:**/g/"), "x": _cond("gitdir:**/g/", b"y") + P, "y": P},
        dirs=("r/g",),
        git_dir="r/g",
    )
    for target in (b"c", b"r", b"p", b"%(prefix)/x"):
        yield Tree(
            "c",
            {"c": _cond("onbranch:m", target) + P},
            dirs=("r",),
            fifos=("p",),
            context=True,
        )
    # Two unsettled includes that die differently, in both orders: the
    # first refusal is the one kept in maybe_dies.
    bare = b'[includeIf "onbranch:m"]\n\tpath\n'
    for first, second in ((b"a", b""), (b"", b"a"), (b"a", b"p"), (b"p", b"a")):
        text = b"".join(_cond("onbranch:m", t) if t else bare for t in (first, second))
        yield Tree(
            "c",
            {"c": text + P, "a": _cond("onbranch:m", b"a")},
            fifos=("p",),
            context=True,
        )
    yield Tree(
        "c",
        {
            "c": _cond("onbranch:m"),
            "x": _cond("onbranch:n", b"y") + P,
            "y": _inc(b"z"),
            "z": P,
        },
        context=True,
    )
    yield Tree(
        "c", {"c": _cond("gitdir:**/g/", b"x\ty") + P}, dirs=("r/g",), git_dir="r/g"
    )
    # The wildmatch steps spent: the condition is not settled.
    deep = "/".join(["a"] * 30)
    yield Tree(
        "c",
        {"c": _cond("gitdir:" + "*a" * 16 + "b"), **x},
        dirs=("a" * 48,),
        git_dir="a" * 48,
    )
    yield Tree(
        "c", {"c": _cond("gitdir:" + "**/" * 24 + "x"), **x}, dirs=(deep,), git_dir=deep
    )
    # After the steps are spent, a later pattern that would match is not
    # settled either: one budget a read.
    yield Tree(
        "c",
        {
            "c": _cond("gitdir:" + "**/" * 24 + "x") + _cond("gitdir:**/a/", b"y"),
            **x,
            "y": P,
        },
        dirs=(deep,),
        git_dir=deep,
    )
    # onbranch: with a branch, HEAD elsewhere, unsettled, no context.
    for branch in ("main", "feature/x", "a*b", "feature/x/y"):
        for pattern in (
            "main",
            "feature/",
            "feature/*",
            "feature/**",
            "*",
            "f*",
            "main/",
            "a\\*b",
            "Main",
            "feature",
        ):
            yield Tree(
                "c",
                {"c": _cond("onbranch:" + pattern), **x},
                context=True,
                branch=branch,
            )
    for other in (None, "\0"):
        yield Tree("c", {"c": _cond("onbranch:main"), **x}, context=True, branch=other)
    yield Tree("c", {"c": _cond("onbranch:main"), **x}, branch="main")
    # includeIf: the reference's answer kept; native declines.
    yield Tree("c", {"c": b'[includeIf "gitdir:x"]\n\tpath = x\n', "x": P})
    yield Tree("c", {"c": b'[includeIf "onbranch:x"]\n\tother = 1\n' + P})
    # Conditions git does not know read nothing, a missing value too; each
    # known prefix beside a near miss; no subsection; an empty one.
    for sub in (
        b"foo:bar",
        b"",
        b"Gitdir:x",
        b"gitdir",
        b"gitdir :x",
        b"GITDIR/I:x",
        b"gitdir/i",
        b"onbranch",
        b"OnBranch:x",
        b"onbranch :x",
        b"hasconfig:other:x",
        b"hasconfig:remote.*.url",
        b"hasconfig:remote.*.URL:x",
        b"gitdir:x",
        b"gitdir/i:x",
        b"onbranch:x",
        b"hasconfig:remote.*.url:x",
    ):
        head = b'[includeIf "' + sub + b'"]\n'
        yield Tree("c", {"c": head + b"\tpath = x\n" + P, "x": b"[x]\n\ty = 1\n"})
        yield Tree("c", {"c": head + b"\tpath\n" + P})
    yield Tree("c", {"c": b"[includeIf]\n\tpath = x\n" + P, "x": P})
    for context in (False, True):
        yield Tree(
            "c",
            {"c": b'[includeIf "hasconfig:remote.*.url:x"]\n\tother = 1\n' + P},
            context=context,
        )


def _tree_file() -> bytes:
    cases = list(_trees())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitconfig-tree"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            answer = _tree_answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "gitconfig_tree", "cases": len(cases)}
    return jsonl([header, *lines])


# ---------------------------------------------------------------------------
# The text rules
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    """`os.fsdecode` as POSIX has it, on any host."""
    return data.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    """`os.fsencode` as POSIX has it, on any host."""
    return text.encode("utf-8", "surrogateescape")


def _flavoured(windows: bool, home: str | None, profile: str | None) -> Any:
    """The `os` and `sys` the reference's path rules read, as one platform
    has them."""
    path = ntpath if windows else posixpath

    def expanduser(text: str) -> str:
        if text == "~" and profile is not None:
            return profile
        return text

    fake_path = SimpleNamespace(
        join=path.join,
        dirname=path.dirname,
        sep=path.sep,
        altsep=path.altsep,
        expanduser=expanduser,
    )
    fake_os = SimpleNamespace(
        name="nt" if windows else "posix",
        path=fake_path,
        environ={} if home is None else {"HOME": home},
        fsdecode=_decode,
        sep=path.sep,
    )
    fake_sys = SimpleNamespace(platform="win32" if windows else "linux")
    return fake_os, fake_sys


def _rule(
    windows: bool,
    value: bytes,
    including: str,
    home: str | None,
    profile: str | None,
    users: dict[str, str],
    prefix: str | None,
    has_prefix: bool,
) -> dict[str, Any]:
    fake_os, fake_sys = _flavoured(windows, home, profile)

    def getpwnam(name: str) -> Any:
        if name in users:
            return SimpleNamespace(pw_dir=users[name])
        raise KeyError(name)

    text = _decode(value)
    with (
        mock.patch.object(gitconfig, "os", fake_os),
        mock.patch.object(gitconfig, "sys", fake_sys),
        mock.patch.dict(sys.modules, {"pwd": SimpleNamespace(getpwnam=getpwnam)}),
    ):
        absolute = gitconfig._git_absolute(text)
        native = gitconfig._native(text, fake_os.path)
        try:
            target: Any = _b(
                _encode(
                    gitconfig._include_target(
                        value, including, (lambda: prefix) if has_prefix else None
                    )
                )
            )
        except gitconfig._Dies as exc:
            target = {"d": _b(_encode(exc.why))}
        except gitconfig._Overflow:
            target = "over"
    record: dict[str, Any] = {
        "w": int(windows),
        "x": _b(value),
        "i": including,
        "h": home,
        "pf": profile,
        "p": prefix if has_prefix else 0,
    }
    if users:
        record["u"] = users
    record["r"] = {"a": absolute, "n": _b(_encode(native)), "t": target}
    return record


_POSIX_VALUES = [
    b"x",
    b"/x",
    b"./x",
    b"../x",
    b"a/b",
    b"a\\b",
    b"",
    b".",
    b"/",
    b"//x",
    b"~",
    b"~/x",
    b"~/",
    b"~u/x",
    b"~u",
    b"~v/x",
    b"~\xff/x",
    b"x~",
    b"\xff/x",
    b"%(prefix)/x",
    b"%(prefix)//x",
    b"%(prefix)",
    b"%(prefix)x",
    b"C:/x",
    b"x'y",
    b"x'y\"z",
    b"\x01\x7f",
    b"\xc3\xa9",
]
_WINDOWS_VALUES = [
    b"x",
    b"/x",
    b"\\x",
    b"C:/x",
    b"C:\\x",
    b"c:x",
    b"C:",
    b"1:x",
    b"\xc3\xa9:x",
    b"\xce\xb1:\\x",
    b"\xe2\x85\xa0:x",
    b"::x",
    b"a/b\\c",
    b"..\\x",
    b"",
    b"~",
    b"~/x",
    b"~\\x",
    b"~u/x",
    b"%(prefix)/x",
    b"%(prefix)/C:/x",
    b"%(prefix)/\\x",
]
_POSIX_INCLUDING = ["/r/c", "r/c", "c", "/c", "//c", "/r//c", "/r/d/"]
_WINDOWS_INCLUDING = [
    "C:\\r\\c",
    "C:/r/c",
    "C:c",
    "c",
    "C:\\",
    "r\\d\\",
]


def _rules() -> Iterator[dict[str, Any]]:
    users = {"u": "/hm/u"}
    for value in _POSIX_VALUES:
        for including in _POSIX_INCLUDING[:2]:
            yield _rule(False, value, including, "/hm/me", None, users, "/pf", True)
    for including in _POSIX_INCLUDING:
        yield _rule(False, b"x", including, "/hm/me", None, users, None, False)
    for home in (None, "", "/", "/hm/me/", "rel"):
        for value in (b"~/x", b"~", b"~u/x"):
            yield _rule(False, value, "/r/c", home, None, users, None, False)
    for prefix, has in (
        (None, False),
        (None, True),
        ("%(prefix)", True),
        ("/pf/", True),
        ("", True),
    ):
        for value in (b"%(prefix)/x", b"%(prefix)//x", b"%(prefix)/"):
            yield _rule(False, value, "/r/c", "/hm/me", None, users, prefix, has)
    for value in _WINDOWS_VALUES:
        for including in _WINDOWS_INCLUDING[:2]:
            yield _rule(True, value, including, "C:\\U\\me", None, users, "C:/pf", True)
    for including in _WINDOWS_INCLUDING:
        for value in (b"x", b"a/b", b"1:x"):
            yield _rule(True, value, including, None, "C:\\U\\me", users, None, False)
    for home, profile in (
        (None, None),
        ("", "D:\\p"),
        ("H:\\h", "D:\\p"),
        (None, "D:/p\\q"),
    ):
        for value in (b"~/x", b"~", b"~u/x"):
            yield _rule(True, value, "C:\\r\\c", home, profile, users, None, False)


def _rules_file() -> bytes:
    lines = list(_rules())
    header = {"fixture": "gitconfig_tree_rules", "cases": len(lines)}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"gitconfig_tree.jsonl": _tree_file()}


def generate_rules() -> dict[str, bytes]:
    return {"gitconfig_tree_rules.jsonl": _rules_file()}
