"""Fixtures: the legacy remote files and the stamps
(``gitconfig_legacy.jsonl``, and ``gitconfig_legacy_python.jsonl`` for the
trees that hold a link loop).

The native build reads them in
``native/crates/bettermemory-core/src/gitconfig/legacy.rs``; this pins it to
`gitconfig._legacy_names`, `_legacy_remote` (with `_legacy_file` and
`_add_url`), `_default_branch`, `legacy_remote_stamps`, `file_stamp` and
`unchanged`.

Each case is a tree as data under a fresh directory of the scratch world
(`_sandbox.world`), which is the common directory: ``"f"`` files (bytes
spelled as in ``gitconfig_file.jsonl``, or a recipe of pieces and counts),
``"d"`` folders, ``"l"`` links, ``"p"`` FIFOs and ``"m"`` modes; every
other file and FIFO the case makes, and every folder (a file's parents
too), is set to 644 and 755 (none is left to the umask). ``"k"``: ``rs``
whether this git empties a remote's URLs at an empty one (null: not
known), ``db`` the ``init.defaultBranch`` values the config reads give,
``tn`` GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME. ``"r"``: ``nm`` the names
`_legacy_names` gives (or ``{"d": why}``), ``rm`` each name read in turn
as the caller reads them, stopping at the first that declines
(``[name, urls]``; ``null``: git dies; ``{"d": why}``; ``"unknown"``:
`resets` asked and not known), ``db`` `_default_branch`, ``st``
`legacy_remote_stamps` of ``origin``, and ``fl`` the files read with
their stamps. A stamp is recorded without host facts: null for nothing
there, ``"u"`` for one that cannot be stated, else ``[mode, size]`` (the
type and permission bits in octal, and the size; the times and the inode
are the host's).

A case with ``"ch"`` is a stamp case: the tree is stamped
(`unchanged`'s input is the file list of the read above), one change
``ch`` is made to ``x``, and ``r.u`` is `unchanged` afterwards.

POSIX only (`generate.POSIX_ONLY`): links, FIFOs and modes. No name is
not UTF-8 (APFS refuses one) and no folder holds two names that differ
by case alone (a case-insensitive file system). Not run as root
(`gen_fs_realpath.refuse_root`).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import gitconfig

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b, _content, _expand

_Content = bytes | list[tuple[bytes, int]]


class _Unknown(Exception):
    pass


class Tree:
    def __init__(
        self,
        files: dict[str, _Content],
        *,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        resets: bool | None = True,
        defaults: list[bytes | None] | None = None,
        test_name: str | None = None,
        change: str | None = None,
    ) -> None:
        self.files = files
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.ask: dict[str, Any] = {"rs": resets}
        if defaults is not None:
            self.ask["db"] = [None if v is None else _b(v) for v in defaults]
        self.defaults = defaults
        if test_name is not None:
            self.ask["tn"] = test_name
        self.change = change
        # Every path the case makes, with its mode.
        made: dict[str, int] = {}

        def parents(rel: str) -> None:
            parts = rel.split("/")[:-1]
            for i in range(len(parts)):
                made.setdefault("/".join(parts[: i + 1]), 0o755)

        for rel in files:
            parents(rel)
            made[rel] = 0o644
        for rel in dirs:
            parents(rel)
            made[rel] = 0o755
        for rel in self.links:
            parents(rel)
        for rel in fifos:
            parents(rel)
            made[rel] = 0o644
        self.explicit = dict(modes or {})
        made.update(self.explicit)
        self.modes = made

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": {k: _content(v) for k, v in self.files.items()}}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.links:
            out["l"] = dict(self.links)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.explicit:
            out["m"] = {k: f"{v:03o}" for k, v in self.explicit.items()}
        out["k"] = self.ask
        if self.change is not None:
            out["ch"] = self.change
        return out


def _url(data: bytes) -> Any:
    if len(data) > 120:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _b(data)


def _stamp(stamp: gitconfig.Stamp | None) -> Any:
    if stamp is None:
        return None
    if stamp == gitconfig._UNSTATED:
        return "u"
    return [f"{stamp[4]:o}", stamp[2]]


def _change(root: Path, change: str) -> None:
    x = root / "x"
    if change == "size":
        with open(x, "ab") as f:
            f.write(b"y")
    elif change == "mtime":
        os.utime(x, ns=(1_000_000_000, 1_000_000_000))
    elif change == "replace":
        (root / "x.new").write_bytes(x.read_bytes())
        os.chmod(root / "x.new", os.stat(x).st_mode & 0o7777)
        os.utime(root / "x.new", ns=(os.stat(x).st_atime_ns, os.stat(x).st_mtime_ns))
        os.replace(root / "x.new", x)
    elif change == "mode":
        os.chmod(x, 0o600)
    elif change == "remove":
        if x.is_dir() and not x.is_symlink():
            shutil.rmtree(x)
        else:
            x.unlink()
    elif change == "create":
        x.write_bytes(b"URL: u\n")
        os.chmod(x, 0o644)
    elif change == "entry":
        (x / "new").write_bytes(b"")
    elif change == "hardlink":
        os.link(x, root / "x.link")
    elif change == "write":
        with open(x, "ab") as f:
            f.write(b"y")
    elif change == "retarget":
        x.unlink()
        os.symlink("b", x)
    elif change != "none":
        raise ValueError(change)


def _answer(case: Tree, root: Path) -> dict[str, Any]:
    root = Path(os.path.realpath(root))
    for rel in case.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    for rel in case.fifos:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(root / rel)  # type: ignore[attr-defined,unused-ignore]
    by_depth = sorted(case.modes, key=lambda rel: rel.count("/"))
    for rel in reversed(by_depth):
        os.chmod(root / rel, case.modes[rel])
    prefix = str(root)

    def label(text: str) -> Any:
        return _b(os.fsencode(text.replace(prefix, "<scratch>")))

    def resets() -> bool:
        if case.ask["rs"] is None:
            raise _Unknown
        return bool(case.ask["rs"])

    reads: list[gitconfig.ConfigValues] = []
    if case.defaults is not None:
        read = gitconfig.ConfigValues()
        read[("init", None, "defaultbranch")] = list(case.defaults)
        reads.append(read)
    env_name = "GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME"
    out: dict[str, Any] = {}
    try:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_name, None)
            if "tn" in case.ask:
                os.environ[env_name] = case.ask["tn"]
            default = gitconfig._default_branch(reads)
            files: list[gitconfig.ConfigFile] = []
            names = gitconfig._legacy_names(prefix, files)
            against = gitconfig._Legacy(
                resets, lambda: gitconfig._default_branch(reads)
            )
            if isinstance(names, str):
                out["nm"] = {"d": label(names)}
            else:
                out["nm"] = [_b(os.fsencode(n)) for n in names]
                read_names: list[Any] = []
                for name in names:
                    try:
                        got = gitconfig._legacy_remote(prefix, name, files, against)
                    except _Unknown:
                        read_names.append([_b(os.fsencode(name)), "unknown"])
                        break
                    if isinstance(got, str):
                        read_names.append([_b(os.fsencode(name)), {"d": label(got)}])
                        break
                    shown = None if got is None else [_url(u) for u in got]
                    read_names.append([_b(os.fsencode(name)), shown])
                out["rm"] = read_names
            out["db"] = None if default is None else _b(default)
            out["st"] = [
                _stamp(s) for s in gitconfig.legacy_remote_stamps(prefix, "origin")
            ]
            out["fl"] = [[label(p), _stamp(s)] for p, s in files]
            if case.change is not None:
                stamped = files + [(prefix + "/x", gitconfig.file_stamp(prefix + "/x"))]
                _change(root, case.change)
                out["u"] = gitconfig.unchanged(stamped)
    finally:
        for rel in by_depth:
            if (root / rel).exists() or (root / rel).is_symlink():
                try:
                    os.chmod(root / rel, 0o700)
                except OSError:
                    pass
    return out


U = b"https://example.invalid/a"
V = b"https://example.invalid/b"


def _trees() -> Iterator[tuple[Tree, bool]]:
    """Each tree, and whether it holds a link loop (Python-gated)."""

    def t(files: dict[str, _Content], **kw: Any) -> tuple[Tree, bool]:
        return Tree(files, **kw), False

    # Nothing; empty folders; each folder alone; a folder that is a file.
    yield t({})
    yield t({}, dirs=("remotes", "branches"))
    yield t({"remotes/origin": b"URL: " + U + b"\n"})
    yield t({"branches/origin": U + b"\n"})
    yield t({"remotes": b"URL: x\n", "branches/o": U + b"\n"})
    # remotes/<name>: URL, Pull and Push lines in every order, repeated,
    # CRLF, no final newline, spaces and tabs around, a NUL, empty.
    for text in (
        b"URL: " + U + b"\nPull: refs/heads/*:refs/remotes/o/*\nPush: HEAD\n",
        b"Pull: refs/heads/m:refs/remotes/o/m\nURL: " + U + b"\nURL: " + V + b"\n",
        b"Push: refs/heads/a\nPush: refs/heads/b\nURL:" + U,
        b"URL: " + U + b"\r\nURL: " + V + b"\r\n",
        b"  URL: " + U + b"\n\tURL: " + V + b"\n",
        b"URL: \t " + U + b" \t\r\n",
        b"URL: " + U + b"\x00tail\nURL: " + V + b"\n",
        b"URL:\n",
        b"URL: \nURL: " + U + b"\n",
        b"URL: " + U + b"\nURL:\n",
        b"URL: " + U + b"\nURL:\nURL: " + V + b"\n",
        b"",
        b"\n\n",
        b"url: " + U + b"\n",
        b"URL " + U + b"\n",
        b"Pull: refs/heads/a b:x\nURL: " + U + b"\n",
        b"Push: :\nURL: " + U + b"\n",
        b"Pull: ^" + b"a" * 40 + b"\nURL: " + U + b"\n",
        b"Pull: refs/heads/*:refs/remotes/*/o\nURL: " + U + b"\n",
        b"URL: " + b"\xff\xfe" + b"\n",
        b"Push: refs/heads/*\nURL: " + U + b"\n",
        b"Pull: x:\nURL: " + U + b"\n",
    ):
        for resets in (True, False, None):
            yield t({"remotes/origin": text}, resets=resets)
    # Over the bound, at it, a long URL.
    yield t({"remotes/origin": [(b"#", (1 << 16) + 1)]})
    yield t({"remotes/origin": [(b"URL: ", 1), (b"a", (1 << 16) - 6), (b"\n", 1)]})
    yield t({"remotes/origin": [(b"URL: ", 1), (b"a", 500), (b"\n", 1)]})
    # branches/<name>: a URL, with #branch, empty, several lines, spaces.
    for text in (
        U + b"#dev\n",
        U + b"#\n",
        b"#dev\n",
        U + b"#a#b\n",
        U + b"#bad..name\n",
        U + b"#-x\n",
        U + b"#:a\n",
        b"  " + U + b"  \nsecond\n",
        b"\n" + U + b"\n",
        U + b"\x00#dev\n",
        U,
        b"",
    ):
        for resets in (True, False, None):
            yield t({"branches/origin": text}, resets=resets)
    # The default branch: the config's last, a valueless one, a refused
    # one, the test name, an empty test name.
    for defaults, name in (
        ([b"main"], None),
        ([b"main", b"trunk"], None),
        ([None], None),
        ([b"main", None], None),
        ([b"a..b"], None),
        ([b"main"], "next"),
        ([b"main"], ""),
        (None, "bad..x"),
    ):
        yield t({"branches/origin": U + b"\n"}, defaults=defaults, test_name=name)
    # Both folders: remotes wins where it gives a URL, branches is read
    # where it gives none (or resets empty it).
    yield t({"remotes/origin": b"URL: " + U + b"\n", "branches/origin": V + b"\n"})
    yield t({"remotes/origin": b"Pull: x:y\n", "branches/origin": V + b"\n"})
    yield t(
        {"remotes/origin": b"URL: " + U + b"\nURL:\n", "branches/origin": V + b"\n"}
    )
    yield t(
        {"remotes/origin": b"URL: " + U + b"\nURL:\n", "branches/origin": V + b"\n"},
        resets=False,
    )
    yield t({"remotes/origin": b"Pull: a b\n", "branches/origin": V + b"\n"})
    # Names: several, sorted, in both folders, dots, a leading dash, upper
    # case, a nested folder, past _LEGACY_NAMES.
    yield t(
        {
            "remotes/b": b"URL: " + U + b"\n",
            "remotes/a": b"URL: " + V + b"\n",
            "remotes/.dot": b"URL: d\n",
            "remotes/-dash": b"URL: e\n",
            "remotes/Up": b"URL: f\n",
            "branches/a": b"g\n",
            "branches/c": b"h\n",
            "branches/lower": b"i\n",
        }
    )
    yield t({"remotes/a/b": b"URL: " + U + b"\n", "branches/a": V + b"\n"})
    yield t({"branches/.x": U + b"#main\n"})
    yield t({f"remotes/r{i:03}": b"URL: u\n" for i in range(257)})
    # 256 names, at the bound: read, the first declining at once.
    yield t({f"remotes/r{i:03}": b"" for i in range(1, 256)}, fifos=("remotes/r000",))
    yield t(
        {
            **{f"remotes/r{i:03}": b"" for i in range(200)},
            **{f"branches/r{i:03}": b"" for i in range(150, 257)},
        }
    )
    # What stands where a file should: a folder, a FIFO, a link, mode 000.
    yield t({"branches/origin": U + b"\n"}, dirs=("remotes/origin",))
    yield t({"branches/origin": U + b"\n"}, fifos=("remotes/origin",))
    yield t({"remotes/origin": b"", "branches/o2": b""}, fifos=("branches/origin",))
    yield t(
        {"remotes/real": b"URL: " + U + b"\n"},
        links={"remotes/origin": "real", "branches/b": "../remotes/real"},
    )
    yield t({"remotes/x": b""}, links={"remotes/origin": "missing"})
    yield t(
        {"remotes/origin": b"URL: " + U + b"\n", "branches/origin": V + b"\n"},
        modes={"remotes/origin": 0o000},
    )
    yield t({"remotes/origin": b"URL: " + U + b"\n"}, modes={"remotes": 0o000})
    yield t({"remotes/origin": b"URL: " + U + b"\n"}, modes={"remotes": 0o300})
    yield t({"real/origin": b"URL: " + U + b"\n"}, links={"remotes": "real"})
    yield t({}, links={"branches": "missing"})
    # Link loops: the folder, and a remote file.
    yield Tree({}, links={"remotes": "remotes"}), True
    yield Tree({"branches/origin": U + b"\n"}, links={"remotes/origin": "origin"}), True
    # Stamps: a change to x of each kind.
    for change in ("none", "size", "mtime", "replace", "mode", "remove", "hardlink"):
        yield t({"x": b"URL: u\n"}, change=change)
    yield t({}, change="create")
    yield t({}, change="none")
    for change in ("none", "entry", "mode", "remove"):
        yield t({}, dirs=("x",), change=change)
    for change in ("none", "retarget", "remove"):
        yield t({"a": b"1\n", "b": b"22\n"}, links={"x": "a"}, change=change)
    yield t({"b": b"1\n"}, links={"x": "a"}, change="none")
    for change in ("none", "mode", "remove"):
        yield t({}, fifos=("x",), change=change)
    # The null device: written to, its times move; its stamp does not.
    yield t({}, links={"x": "/dev/null"}, change="write")


def _file(looped: bool) -> bytes:
    refuse_root()
    cases = [case for case, loop in _trees() if loop == looped]
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitconfig-legacy"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            os.chmod(root, 0o755)
            answer = _answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    name = "gitconfig_legacy_python" if looped else "gitconfig_legacy"
    header = {"fixture": name, "cases": len(cases)}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"gitconfig_legacy.jsonl": _file(False)}


def generate_python() -> dict[str, bytes]:
    return {"gitconfig_legacy_python.jsonl": _file(True)}
