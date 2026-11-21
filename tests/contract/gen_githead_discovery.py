"""Fixtures: git's discovery as `githead` reads it from the files
(``githead_discovery.jsonl``).

Unit githead-1 (``native/.../githead/discovery.rs``) ports `find_gitdir`,
`gitdir_at`, `locate`, `probed`, `across_filesystems`, `discovery_moved`,
`_entry_above`, `_located` and `stamp`; this pins each to the reference's
own function on real trees.

Each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), with the record store an
empty folder of the scratch world (`gitdir_pin.pins_dir` patched), the git
on PATH as data (release 2.54, `_reads_compat` True) and
``GIT_COMMON_DIR`` unset. What `githead` asks of the machine goes through
a stand-in for its ``os`` module (`_Os`), so no other module sees it:

- ``"env"``: the environment `githead` reads, as a dict (``<root>`` in a
  value is the case's directory); nothing else is set.
- ``"euid"``: ``same`` (the generating user), ``other`` (that user's uid
  plus one) or ``root`` (0); ``"o"``: paths, as `_check_owner` passes
  them, whose owner is that user's uid plus one.
- ``"mnt"``: folders mounted as a device of their own (``{"rel": n}``):
  ``st_dev`` is ``n`` for every path whose resolution lies at or under
  the folder (the longest wins) and 0 for every other, above the case's
  directory included, as the native model's mounts read.

Lines: a header (``"cases"`` and ``"queries"``), then one per case: the
tree as `gen_follow` spells it (``"f"``, ``"d"``, ``"l"``, ``"p"``,
``"m"``) and ``"q"``, one answer a query ``op:args`` (split at ``|``):

- ``f:start`` `find_gitdir`, ``a:root`` `gitdir_at`, ``L:start|flags``
  `locate` (``N`` for ``walk=False``, ``G`` for ``as_git=True``):
  null or ``{"g": gitdir, "c": commondir, "w": worktree_root}``;
- ``X:start`` `_located`: the same, or ``{"e": class}``;
- ``E:start`` `_entry_above`: a bool or ``{"e": class}``;
- ``P:here|d`` `probed` (``d``: the device of ``here``; ``0``: device 0; ``n``: None): the
  folders probed at or under the case's directory, ``"past"`` whether the
  walk went on above it, and ``"e"`` where a stat raised;
- ``S:path|flags`` `stamp` (``n``: ``follow=False``): null, an error, or
  ``{"t": st_mode >> 12, "s": size (regular files only), "eq": i}``
  where ``i`` is the first query of the case whose stamp equals this one,
  so stamps are compared by equality alone (no time or inode in a row);
- ``M:`` `discovery_moved`, ``A:`` `across_filesystems`.

A path answered is spelled with the case's directory as ``<scratch>``. A
walk that leaves its case goes on to the file system's root, where the
generator checks no ``.git`` or ``HEAD`` entry stands (`_clear_above`).
Gates: POSIX only (links, modes, FIFOs, owners). Nothing depends on the
umask, the host's users, home, the process environment or the scratch
path.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin, githead
from bettermemory import gitfile_rule as g

from . import _sandbox
from ._common import jsonl
from .gen_discovery import _clear_above
from .gen_follow import HEAD, V0, _repo, _spell, _text
from .gen_fs_realpath import _b, refuse_root
from .gen_fs_stat import _error


class Case:
    def __init__(
        self,
        queries: list[str],
        *,
        files: dict[str, Any],
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        env: dict[str, str] | None = None,
        euid: str = "same",
        owned: tuple[str, ...] = (),
        mounts: dict[str, int] | None = None,
    ) -> None:
        self.queries = queries
        self.files = files
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.modes = modes or {}
        self.env = env or {}
        self.euid = euid
        self.owned = owned
        self.mounts = mounts or {}

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": dict(self.files)}
        for key, value in (
            ("d", list(self.dirs)),
            ("l", dict(self.links)),
            ("p", list(self.fifos)),
            ("m", {k: f"{v:03o}" for k, v in self.modes.items()}),
            ("env", dict(self.env)),
            ("o", list(self.owned)),
            ("mnt", dict(self.mounts)),
        ):
            if value:
                out[key] = value
        if self.euid != "same":
            out["euid"] = self.euid
        return out


class _Os:
    """`githead`'s view of ``os``: the case's environment, user, owners
    and devices; everything else is the real module."""

    def __init__(self, case: Case, real: str, me: int) -> None:
        self.environ = {k: v.replace("<root>", real) for k, v in case.env.items()}
        self._euid = {"same": me, "other": me + 1, "root": 0}[case.euid]
        self._me = me
        self._owned = {f"{real}/{rel}" for rel in case.owned}
        self._mounts = {f"{real}/{rel}": n for rel, n in case.mounts.items()}

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def geteuid(self) -> int:
        return self._euid

    def _dev(self, path: Any) -> int:
        where = os.path.realpath(path)
        best, dev = -1, 0
        for at, n in self._mounts.items():
            if (where == at or where.startswith(at + "/")) and len(at) > best:
                best, dev = len(at), n
        return dev

    @staticmethod
    def _with(st: os.stat_result, index: int, value: int) -> os.stat_result:
        reduced: Any = st.__reduce__()
        seq, extra = reduced[1]
        fields = list(seq)
        fields[index] = value
        return os.stat_result(fields, extra)

    def stat(self, path: Any, *, follow_symlinks: bool = True) -> os.stat_result:
        st = os.stat(path, follow_symlinks=follow_symlinks)
        return self._with(st, 2, self._dev(path))

    def lstat(self, path: Any) -> os.stat_result:
        st = os.lstat(path)
        uid = self._me + 1 if os.fspath(path) in self._owned else self._me
        return self._with(st, 4, uid)


def _gd(found: githead.GitDir | None, real: str) -> Any:
    if found is None:
        return None
    return {
        "g": _spell(str(found.gitdir), real),
        "c": _spell(str(found.commondir), real),
        "w": _spell(str(found.worktree_root), real),
    }


def _caught(fn: Any) -> Any:
    try:
        return fn()
    except g._Unsettled:
        return {"e": "_Unsettled"}
    except (OSError, ValueError) as exc:
        return _error(exc)


def _probed(here: str, device: int | None, real: str) -> Any:
    seen: list[Any] = []
    past = False
    out: dict[str, Any] = {}
    try:
        for d in githead.probed(here, device):
            if d == real or d.startswith(real + "/"):
                seen.append(_spell(d, real))
            else:
                past = True
    except (OSError, ValueError) as exc:
        out["e"] = _error(exc)
    out["y"] = seen
    out["past"] = past
    return out


def _query(query: str, real: str, fake: _Os, stamps: list[Any]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    flags = args[1] if len(args) > 1 else ""
    out: dict[str, Any] = {"p": _b(query.encode())}
    if op == "f":
        out["v"] = _gd(githead.find_gitdir(Path(args[0])), real)
    elif op == "a":
        out["v"] = _gd(githead.gitdir_at(Path(args[0])), real)
    elif op == "L":
        found = githead.locate(
            Path(args[0]), walk="N" not in flags, as_git="G" in flags
        )
        out["v"] = _gd(found, real)
    elif op == "X":
        out["v"] = _caught(lambda: _gd(githead._located(Path(args[0])), real))
    elif op == "E":
        out["v"] = _caught(lambda: githead._entry_above(Path(args[0])))
    elif op == "P":
        device = (
            fake.stat(args[0]).st_dev if flags == "d" else 0 if flags == "0" else None
        )
        out["v"] = _probed(args[0], device, real)
    elif op == "S":
        got = _caught(lambda: githead.stamp(args[0], follow="n" not in flags))
        if isinstance(got, tuple):
            stamps.append(got)
            mode = got[4]
            shown: dict[str, Any] = {"t": mode >> 12, "eq": stamps.index(got)}
            if stat.S_ISREG(mode):
                shown["s"] = got[2]
            got = shown
        out["v"] = got
    elif op == "M":
        out["v"] = githead.discovery_moved()
    elif op == "A":
        out["v"] = githead.across_filesystems()
    else:
        raise ValueError(op)
    return out


def _answer(case: Case, root: Path, pins: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        _clear_above(real)
        base = Path(real)
        for d in case.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
        for rel, value in case.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_text(value).replace(b"<root>", os.fsencode(real)))
            os.chmod(path, 0o644)
        for rel, target in case.links.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        for rel in case.fifos:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(base / rel)  # type: ignore[attr-defined,unused-ignore]
            os.chmod(base / rel, 0o644)
        pins.mkdir(parents=True)
        for rel in sorted(case.modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, case.modes[rel])
        me = os.geteuid()  # type: ignore[attr-defined,unused-ignore]
        fake = _Os(case, real, me)
        stamps: list[Any] = []
        try:
            with (
                patch.dict(os.environ),
                patch.object(githead, "os", fake),
                patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want),
                patch.object(g, "_reads_compat", lambda: True),
                patch.object(g, "_RECORDED_BUDGET", 60.0),
                patch.object(gitdir_pin, "pins_dir", lambda: pins),
            ):
                os.environ.pop("GIT_COMMON_DIR", None)
                return [_query(q, real, fake, stamps) for q in case.queries]
        finally:
            for rel in case.modes:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


# --- the trees ---------------------------------------------------------------


#: Every walk-and-read query of a start.
def _walks(start: str) -> list[str]:
    return [
        f"f:{start}",
        f"a:{start}",
        f"L:{start}",
        f"L:{start}|N",
        f"L:{start}|G",
        f"X:{start}",
        f"E:{start}",
    ]


def _world() -> dict[str, Any]:
    files: dict[str, Any] = _repo("r/.git", V0)
    files |= {"r/f": "x", "r/sub/deep/f": "", "r/skip/.git/.keep": ""}
    files |= _repo("bare", V0)
    files |= _repo("sep", V0)
    # A linked worktree of `r`.
    files |= {
        "r/.git/worktrees/wt/HEAD": HEAD,
        "r/.git/worktrees/wt/commondir": "../..\n",
        "r/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "wt/.git": "gitdir: <root>/r/.git/worktrees/wt\n",
        "wt/in/f": "",
    }
    # `.git` files of each shape.
    for name, text in {
        "ok": "gitdir: <root>/sep\n",
        "crlf": "gitdir: <root>/sep\r\n",
        "bare": "gitdir: <root>/sep",
        "rel": "gitdir: ../../sep\n",
        "noprefix": "gitdir:<root>/sep\n",
        "empty": "gitdir: \n",
        "nothing": "",
        "notgit": "gitdir: <root>/plain\n",
        "missing": "gitdir: <root>/nothere\n",
        "other": "gitdir: <root>/r/.git\n",
        "nul": {"hex": "6769746469723a202f00610a"},
    }.items():
        files[f"gf/{name}/.git"] = text
        files[f"gf/{name}/in/f"] = ""
    files["gf/big/.git"] = {"p": "gitdir: <root>/sep\n", "x": "\n", "n": 1 << 20}
    files["gf/edge/.git"] = {"p": "gitdir: /", "x": "a", "n": (1 << 20) - 9}
    files["plain/f"] = ""
    return files


def _cases() -> Iterator[Case]:
    files = _world()
    links = {"lstart": "<root>/r/sub", "ln/.git": "<root>/r/.git", "lf": "<root>/r/f"}
    starts = [
        "<root>/r",
        "<root>/r/sub/deep",
        "<root>/r/f",
        "<root>/nothere",
        "<root>/r/.git",
        "<root>/r/.git/refs",
        "<root>/bare",
        "<root>/bare/refs",
        "<root>/r/skip",
        "<root>/r/skip/x",
        "<root>/wt",
        "<root>/wt/in",
        "<root>/plain",
        "<root>/lstart",
        "<root>/ln",
        "<root>/lf",
        "<root>/fifo",
        "<root>/r/noexec",
        "<root>/r//sub/./deep/",
        "r/sub",
        ".",
        "",
        "<root>/sep",
        "<root>/r/.git/worktrees/wt",
    ]
    starts += [
        f"<root>/gf/{n}"
        for n in (
            "ok",
            "crlf",
            "bare",
            "rel",
            "noprefix",
            "empty",
            "nothing",
            "notgit",
            "missing",
            "other",
            "nul",
            "big",
            "edge",
        )
    ]
    starts += ["<root>/gf/ok/in", "<root>/gf/other/in"]
    queries = [q for s in starts for q in _walks(s)]
    queries += [
        "P:<root>/r/sub/deep|d",
        "P:<root>/r/sub/deep|n",
        "P:<root>/r|d",
        "P:<root>/nothere|n",
        "P:<root>/nothere/x|0",
        "M:",
        "A:",
        "S:<root>/r/.git/HEAD",
        "S:<root>/r/.git/HEAD|n",
        "S:<root>/r",
        "S:<root>/r/f",
        "S:<root>/lf",
        "S:<root>/lf|n",
        "S:<root>/nothere",
        "S:<root>/r/f/x",
        "S:<root>/fifo",
        "S:<root>/fifo|n",
        "S:r/f",
        "S:<root>/lstart",
        "S:<root>/r/sub",
        "S:<root>/r/f",
    ]
    yield Case(
        queries,
        files=files,
        dirs=("r/noexec",),
        links=links,
        fifos=("fifo", "ff/.git"),
        modes={"r/noexec": 0o600},
    )
    # The fifo `.git`.
    yield Case(_walks("<root>/ff"), files={"x": ""}, fifos=("ff/.git",))

    # Ceilings.
    deep = [
        "f:<root>/r/sub/deep",
        "L:<root>/r/sub/deep",
        "X:<root>/r/sub/deep",
        "P:<root>/r/sub/deep|d",
        "f:<root>/r",
        "L:<root>/r",
        "P:<root>/r|n",
        "f:<root>/r/sub",
        "L:<root>/r/sub",
        "f:<root>/gf/ok/in",
        "L:<root>/gf/ok/in",
    ]
    small = {
        **_repo("r/.git", V0),
        "r/sub/deep/f": "",
        **_repo("sep", V0),
        "gf/ok/.git": "gitdir: <root>/sep\n",
        "gf/ok/in/f": "",
    }
    for ceiling in (
        "<root>/r",
        "<root>/r/",
        "<root>/r/sub/deep",
        "<root>/lstart",
        "r",
        "<root>/nothere",
        "<root>/nothere:<root>/r",
        "<root>/r:<root>/nothere",
        ":<root>/r",
        ":<root>/r/",
        "://<root>/r",
        ":<root>/./r",
        ":<root>//r",
        ":<root>/lstart",
        ":<root>/r/sub/",
        "<root>/lstart::<root>/lstart",
        "/",
        "<root>",
        "",
        ":",
        "<root>/x:<root>/r/sub",
        "<root>/gf",
        "<root>/r/f",
        "<root>/r/sub/..",
        "//",
        "<root>/./r",
        "<root>//r",
        "<root>/lstart/..",
        ":<root>/r/sub/..",
        ":<root>/gf/ok",
    ):
        yield Case(
            deep + ["M:"],
            files=small,
            links={"lstart": "<root>/r/sub"},
            env={"GIT_CEILING_DIRECTORIES": ceiling},
        )

    # Each variable that moves discovery, and the boolean.
    for name in githead._DISCOVERY_ENV:
        for value in ("", "0", "1", "<root>/r/.git"):
            yield Case(
                [
                    "f:<root>/r",
                    "a:<root>/r",
                    "L:<root>/r",
                    "X:<root>/r",
                    "X:<root>/plain",
                    "M:",
                    "A:",
                ],
                files={**_repo("r/.git", V0), "plain/f": ""},
                env={name: value},
            )
    for value in (
        "",
        "0",
        "1",
        "2",
        "-1",
        "true",
        "TRUE",
        "yes",
        "on",
        "off",
        "no",
        "false",
        "bogus",
        "1k",
        "0x10",
        " 1",
        "1 ",
        "99999999999",
        "+1",
        "0.5",
        "fAlSe",
        "k",
        "1g",
    ):
        yield Case(
            ["A:", "M:"],
            files={"x": ""},
            env={"GIT_DISCOVERY_ACROSS_FILESYSTEM": value},
        )
    yield Case(
        ["A:", "M:", "f:<root>/r"],
        files=_repo("r/.git", V0),
        env={
            "GIT_CEILING_DIRECTORIES": "/",
            "SUDO_UID": "0",
            "GIT_OBJECT_DIRECTORY": "<root>",
        },
    )

    # Owners.
    own = {
        **_repo("r/.git", V0),
        "r/in/f": "",
        **_repo("sep", V0),
        "gf/ok/.git": "gitdir: <root>/sep\n",
        "gf/ok/in/f": "",
    }
    owner_q = [
        "f:<root>/r/in",
        "a:<root>/r",
        "L:<root>/r/in",
        "X:<root>/r",
        "f:<root>/gf/ok/in",
        "a:<root>/gf/ok",
        "L:<root>/gf/ok",
    ]
    for euid, owned, env in (
        ("other", (), {}),
        ("same", ("r/.git",), {}),
        ("same", ("r",), {}),
        ("same", ("r/in",), {}),
        ("same", ("gf/ok/.git",), {}),
        ("same", ("gf/ok",), {}),
        ("same", ("sep",), {}),
        ("other", ("r", "r/.git", "gf/ok", "gf/ok/.git", "sep"), {}),
        ("other", ("r", "r/.git", "gf/ok", "gf/ok/.git"), {}),
        ("root", (), {}),
        ("root", (), {"SUDO_UID": "501"}),
        ("same", (), {"SUDO_UID": "501"}),
    ):
        yield Case(owner_q, files=own, euid=euid, owned=owned, env=env)

    # Devices.
    dev_q = [
        "f:<root>/r/sub/deep",
        "L:<root>/r/sub/deep",
        "X:<root>/r/sub/deep",
        "P:<root>/r/sub/deep|d",
        "P:<root>/r/sub/deep|n",
        "f:<root>/r/sub",
        "a:<root>/r",
        "f:<root>/r",
    ]
    dev_files = {**_repo("r/.git", V0), "r/sub/deep/f": ""}
    for mounts, env in (
        ({"r/sub": 1}, {}),
        ({"r/sub/deep": 1}, {}),
        ({"r/sub": 1, "r/sub/deep": 2}, {}),
        ({"r": 1}, {}),
        ({"r/sub": 1}, {"GIT_CEILING_DIRECTORIES": "<root>/r"}),
        ({"r/sub": 1}, {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"}),
    ):
        yield Case(dev_q, files=dev_files, mounts=mounts, env=env)


def _lines() -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "githead-discovery"
        for i, case in enumerate(_cases()):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(case, root, world.root / f"pins-{i}")
            for rel in case.modes:
                os.chmod(root / rel, 0o700)
            shutil.rmtree(root)
            lines.append({**case.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines()
    header = {
        "fixture": "githead_discovery",
        "cases": len(lines),
        "queries": sum(len(line["q"]) for line in lines),
    }
    return {"githead_discovery.jsonl": jsonl([header, *lines])}
