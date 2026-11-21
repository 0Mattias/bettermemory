"""Fixtures: `origin_history`'s repository-locating half
(``origin_history_located.jsonl`` and ``origin_history_located_modes.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_history/located.rs``.

The first line is a header with the counts of cases and queries. Then one
case a line: a tree under the case's directory (``f``: files, a string with
``<root>`` for the case's directory, or ``{"p", "x", "n"}``: ``p`` then
``x`` repeated ``n`` times; ``d``: folders; ``m``: modes in octal, applied
deepest first), and ``q``, its queries in order, each ``{"p", "v"}``:

- ``K:<path>``: `located_key`; ``[root, head]`` or null;
- ``F:<path>|<flags>``: `shallow_file` (``W``: walk); the path or null;
- ``C:<path>|<flags>``: `cuts`; the commits sorted, or null;
- ``I:<path>``: `cuts_in`; the commits sorted, or null;
- ``G:<path>``: `history_guard`; null, or ``{"eq": i}``, the index of the
  first guard of the case equal to it (the stamps' values, which follow
  the file system, are never written);
- ``w:<rel>|<text>``: writes the file (a step, answer null);
- ``e:<name>|<value>``: sets the variable (``e:<name>`` unsets it).

A path answered is spelled with the case's directory as ``<scratch>``. No
query runs under a variable the case does not set: every variable of
`githead`'s discovery and of ``_HISTORY_ENV`` is cleared first.
Gates: POSIX only. The reference's discovery reads POSIX paths and
owners; the port's declines under the Windows flavour (the crate's stated
gap). The modes file reads mode-000 files and folders.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin
from bettermemory import gitfile_rule as g
from bettermemory import origin_history as oh

from . import _sandbox
from ._common import jsonl
from .gen_discovery import _clear_above
from .gen_follow import _spell
from .gen_fs_realpath import refuse_root

V0 = "[core]\n\trepositoryformatversion = 0\n"
V256 = "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = sha256\n"
V2 = "[core]\n\trepositoryformatversion = 2\n"
A = "a" * 40
B = "0123456789abcdef0123456789abcdef01234567"
C = "c" * 64
_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_TEST_ASSUME_DIFFERENT_OWNER",
    "GIT_CEILING_DIRECTORIES",
    *oh._HISTORY_ENV,
)


def _repo(at: str, config: str = V0, head: str | None = A) -> dict[str, Any]:
    files: dict[str, Any] = {
        f"{at}/HEAD": "ref: refs/heads/main\n",
        f"{at}/objects/.keep": "",
        f"{at}/config": config,
    }
    if head is None:
        files[f"{at}/refs/.keep"] = ""
    else:
        files[f"{at}/refs/heads/main"] = head + "\n"
    return files


def _text(value: Any) -> bytes:
    if isinstance(value, dict):
        return (value.get("p", "") + value["x"] * value["n"]).encode()
    return str(value).encode("utf-8", "surrogateescape")


def _cut(found: frozenset[str] | None) -> Any:
    return None if found is None else sorted(found)


def _query(query: str, real: str, guards: list[Any]) -> Any:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    flags = args[1] if len(args) > 1 else ""
    if op == "K":
        key = oh.located_key(Path(args[0]))
        return None if key is None else [_spell(key[0], real), key[1]]
    if op == "F":
        path = oh.shallow_file(Path(args[0]), walk="W" in flags)
        return None if path is None else _spell(str(path), real)
    if op == "C":
        return _cut(oh.cuts(Path(args[0]), walk="W" in flags))
    if op == "I":
        return _cut(oh.cuts_in(Path(args[0])))
    if op == "G":
        got = oh.history_guard(Path(args[0]))
        if got is None:
            return None
        guards.append(got)
        return {"eq": guards.index(got)}
    if op == "w":
        Path(real, raw.split("|")[0]).write_bytes(_text(args[1]))
        return None
    if op == "e":
        if len(args) > 1:
            os.environ[args[0]] = args[1]
        else:
            os.environ.pop(args[0], None)
        return None
    raise ValueError(op)


def _answer(case: dict[str, Any], root: Path, pins: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        _clear_above(real)
        base = Path(real)
        for d in case.get("d", ()):
            (base / d).mkdir(parents=True, exist_ok=True)
        for rel, value in case["f"].items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_text(value).replace(b"<root>", os.fsencode(real)))
            os.chmod(path, 0o644)
        pins.mkdir(parents=True)
        modes = case.get("m", {})
        for rel in sorted(modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, int(modes[rel], 8))
        guards: list[Any] = []
        try:
            with (
                patch.dict(os.environ),
                patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want),
                patch.object(g, "_reads_compat", lambda: True),
                patch.object(g, "_RECORDED_BUDGET", 60.0),
                patch.object(gitdir_pin, "pins_dir", lambda: pins),
            ):
                for name in _VARS:
                    os.environ.pop(name, None)
                return [{"p": q, "v": _query(q, real, guards)} for q in case["q"]]
        finally:
            for rel in modes:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


# --- the trees ---------------------------------------------------------------


def _shallow_files() -> Iterator[tuple[str, str | None, Any]]:
    """(name, config, shallow file) for `cuts_in`."""
    yield "one", V0, A + "\n"
    yield "two", V0, f"{A}\n{B}\n"
    yield "dup", V0, f"{A}\n{A}\n"
    yield "nolf", V0, A
    yield "crlf", V0, f"{A}\r\n{B}\r\n"
    yield "upper", V0, B.upper() + "\n"
    yield "mixed", V0, "AbCdEf" + A[6:] + "\n"
    yield "suffix", V0, A + "ffff tail\n"
    yield "suffixhex", V0, A + B + "\n"
    yield "short", V0, A[:39] + "\n"
    yield "shortlast", V0, f"{A}\n{B[:20]}"
    yield "nonhex", V0, "g" + A[1:] + "\n"
    yield "space", V0, " " + A[1:] + "\n"
    yield "blank", V0, f"{A}\n\n{B}\n"
    yield "twolf", V0, f"{A}\n\n"
    yield "lf", V0, "\n"
    yield "empty", V0, ""
    yield "empty_badcfg", V2, ""
    yield "nul", V0, "\x00" * 40 + "\n"
    yield "high", V0, "é" * 20 + "\n"
    yield "s256", V256, C + "\n"
    yield "s256upper", V256, C.upper() + "\n" + "ab" * 32
    yield "s256short", V256, A + "\n"
    yield "s256sfx", V256, C + "x\n"
    yield "v2", V2, A + "\n"
    yield "badcfg", "[core\n", A + "\n"
    yield "nocfg", None, A + "\n"
    yield (
        "unknownfmt",
        "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = md5\n",
        A + "\n",
    )
    # The bound: _CUTS_MAX bytes read, one more declined.
    rest = oh._CUTS_MAX - 41 * (oh._CUTS_MAX // 41)
    yield (
        "atmax",
        V0,
        {"p": "f" * (41 + rest - 1) + "\n", "x": A + "\n", "n": oh._CUTS_MAX // 41 - 1},
    )
    yield (
        "overmax",
        V0,
        {"p": "f" * (42 + rest - 1) + "\n", "x": A + "\n", "n": oh._CUTS_MAX // 41 - 1},
    )


def _cases() -> Iterator[dict[str, Any]]:
    # A plain repository, the guard's files moved one by one.
    files: dict[str, Any] = {**_repo("r/.git"), "r/sub/f": ""}
    yield {
        "f": files,
        "q": [
            "K:<root>/r",
            "K:<root>/r/sub",
            "K:<root>/nothere",
            "K:<root>/r/.git",
            "F:<root>/r|",
            "F:<root>/r/sub|",
            "F:<root>/r/sub|W",
            "C:<root>/r|",
            "C:<root>/r/sub|",
            "C:<root>/r/sub|W",
            "I:<root>/r/.git/shallow",
            "G:<root>/r",
            "G:<root>/r",
            "G:<root>/r/sub",
            "w:r/.git/refs/heads/main|" + B + "\n",
            "K:<root>/r",
            "G:<root>/r",
            "w:r/.git/shallow|" + A + "\n",
            "C:<root>/r|",
            "G:<root>/r",
            "G:<root>/r",
            "w:r/.git/packed-refs|# pack-refs with: peeled\n",
            "G:<root>/r",
            "w:r/.git/config|" + V0 + "\tbare = false\n",
            "G:<root>/r",
            "e:GIT_SHALLOW_FILE|x",
            "G:<root>/r",
            "e:GIT_SHALLOW_FILE|",
            "G:<root>/r",
            "e:GIT_SHALLOW_FILE",
            "G:<root>/r",
            "e:GIT_NO_REPLACE_OBJECTS|1",
            "G:<root>/r",
            "e:GIT_NO_REPLACE_OBJECTS",
            "e:GIT_REPLACE_REF_BASE|refs/r/",
            "G:<root>/r",
            "e:GIT_REPLACE_REF_BASE",
            "e:GIT_GRAFT_FILE|/dev/null",
            "G:<root>/r",
            "e:GIT_GRAFT_FILE",
            "G:<root>/r",
            "w:r/.git/info/grafts|" + A + "\n",
            "G:<root>/r",
            "w:r/.git/refs/replace/" + A + "|" + B + "\n",
            "G:<root>/r",
            "e:GIT_DIR|<root>/r/.git",
            "K:<root>/r",
            "G:<root>/r",
            "C:<root>/r|",
            "e:GIT_DIR",
            "e:GIT_WORK_TREE|<root>/r",
            "K:<root>/r",
            "e:GIT_WORK_TREE",
            "e:GIT_CEILING_DIRECTORIES|<root>/r",
            "K:<root>/r/sub",
            "C:<root>/r/sub|W",
            "e:GIT_CEILING_DIRECTORIES",
            "e:GIT_COMMON_DIR|<root>/r/.git",
            "K:<root>/r",
            "e:GIT_COMMON_DIR",
            "e:GIT_DISCOVERY_ACROSS_FILESYSTEM|1",
            "K:<root>/r",
        ],
        "d": ["r/.git/info", "r/.git/refs/replace"],
    }
    # A linked worktree: its HEAD, the main repository's shallow file.
    wt = {
        **_repo("r/.git"),
        "r/.git/shallow": B + "\n",
        "r/.git/worktrees/wt/HEAD": B + "\n",
        "r/.git/worktrees/wt/commondir": "../..\n",
        "r/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "wt/.git": "gitdir: <root>/r/.git/worktrees/wt\n",
        "wt/in/f": "",
    }
    yield {
        "f": wt,
        "q": [
            "K:<root>/wt",
            "K:<root>/wt/in",
            "F:<root>/wt|",
            "C:<root>/wt|",
            "C:<root>/wt/in|",
            "C:<root>/wt/in|W",
            "G:<root>/wt",
            "G:<root>/r",
            "w:r/.git/worktrees/wt/HEAD|" + A + "\n",
            "K:<root>/wt",
            "G:<root>/wt",
            "w:r/.git/shallow|" + A + "\n" + B + "\n",
            "G:<root>/wt",
            "C:<root>/wt|",
        ],
    }
    # A bare repository, an unborn branch, a detached head, a separate
    # git directory.
    yield {
        "f": {
            **_repo("b.git", V0 + "\tbare = true\n"),
            "b.git/shallow": A + "\n",
            **_repo("u/.git", head=None),
            **_repo("dt/.git"),
            "dt/.git/HEAD": B.upper() + "\n",
            "dh/.git/HEAD": B + "\n",
            "dh/.git/objects/.keep": "",
            "dh/.git/refs/.keep": "",
            **_repo("sep"),
            "gf/.git": "gitdir: <root>/sep\n",
            "plain/f": "",
            "file": "",
        },
        "q": [
            "K:<root>/b.git",
            "F:<root>/b.git|",
            "C:<root>/b.git|",
            "C:<root>/b.git|W",
            "G:<root>/b.git",
            "K:<root>/u",
            "C:<root>/u|",
            "G:<root>/u",
            "K:<root>/dt",
            "K:<root>/dh",
            "K:<root>/gf",
            "F:<root>/gf|",
            "G:<root>/gf",
            "K:<root>/plain",
            "F:<root>/plain|W",
            "C:<root>/plain|W",
            "G:<root>/plain",
            "K:<root>/file",
            "C:<root>/file|",
            "G:<root>/file",
            "G:<root>/nothere",
            "K:plain",
            "K:gf",
            "C:gf|",
            "I:sep/shallow",
        ],
    }
    # `cuts_in` over shallow files.
    files = {}
    queries: list[str] = []
    for name, config, text in _shallow_files():
        if config is not None:
            files[f"x/{name}/config"] = config
        files[f"x/{name}/shallow"] = text
        queries.append(f"I:<root>/x/{name}/shallow")
    files["x/dir/config"] = V0
    files["x/dir/shallow/f"] = ""
    files["x/file/config"] = V0
    files["x/file/is"] = ""
    queries += [
        "I:<root>/x/dir/shallow",
        "I:<root>/x/missing/shallow",
        "I:<root>/x/file/is/shallow",
        "I:<root>/x/one/shallow/",
        "I:<root>/x/one//shallow",
        "I:x/one/shallow",
        "I:<root>/x/one/./shallow",
    ]
    yield {"f": files, "q": queries}
    # A shallow repository found by cuts.
    yield {
        "f": {
            **_repo("s/.git", V256, head=C),
            "s/.git/shallow": C + "\n" + C.upper()[:64] + "\n",
            **_repo("t/.git"),
            "t/.git/shallow": A + "\n" + B + "\n",
        },
        "q": ["K:<root>/s", "C:<root>/s|", "C:<root>/t|", "G:<root>/s", "G:<root>/t"],
    }


def _modes() -> Iterator[dict[str, Any]]:
    yield {
        "f": {
            **_repo("r/.git"),
            "r/.git/shallow": A + "\n",
            **_repo("q/.git"),
            **_repo("p/.git"),
            "p/.git/shallow": A + "\n",
            **_repo("o/.git"),
            "o/.git/shallow": A + "\n",
        },
        "d": ["q/.git/refs/replace", "o/.git/info"],
        "m": {
            "r/.git/shallow": "000",
            "q/.git/refs": "000",
            "p/.git/config": "000",
            "o/.git/info": "000",
        },
        "q": [
            "C:<root>/r|",
            "I:<root>/r/.git/shallow",
            "G:<root>/r",
            "K:<root>/r",
            "G:<root>/q",
            "K:<root>/q",
            "C:<root>/q|",
            "C:<root>/p|",
            "G:<root>/p",
            "G:<root>/o",
            "C:<root>/o|",
        ],
    }


def _lines(cases: Iterator[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / tag
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(case, root, world.root / f"pins-{tag}-{i}")
            shutil.rmtree(root)
            lines.append({**{k: v for k, v in case.items() if k != "q"}, "q": answers})
    return lines


def _file(name: str, lines: list[dict[str, Any]]) -> bytes:
    header = {
        "fixture": name,
        "cases": len(lines),
        "queries": sum(len(line["q"]) for line in lines),
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    if os.name != "posix":
        return {}
    return {
        "origin_history_located.jsonl": _file(
            "origin_history_located", _lines(_cases(), "located")
        ),
        "origin_history_located_modes.jsonl": _file(
            "origin_history_located_modes", _lines(_modes(), "modes")
        ),
    }
