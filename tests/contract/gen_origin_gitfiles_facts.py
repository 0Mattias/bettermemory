"""Fixtures: what git dies on in a repository's or the machine's config,
what a config text says, and the directories a check holds
(``origin_gitfiles_facts.jsonl``, ``origin_gitfiles_facts_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles/facts.rs``.

``origin_gitfiles_facts.jsonl`` (no gate: nothing here reads the machine
or turns on the release). The header names the value list ``values``,
each spelled as ``_spell`` spells bytes (None, ``=`` and the text where
printable ASCII without a backslash, else hex). Rows:

- ``["rpd", name, at, bits]``: `_rev_parse_dies(name, value)` for each of
  ``values`` in order, ``bits`` a ``0``/``1`` per value, with
  `gitsystem.is_at_least` patched to answer `at` (true, false or null)
  for every release, or, where `at` is ``[major, minor]``, to compare
  that release with the one asked.
- ``["mvd", name, at, bits]``: `_machine_value_dies` the same way.
- ``["fo", text, at, answer]``: `_facts_of(text)` and
  `_config_variable_names(text)`, the answer as `_facts_line` spells it.
- ``["fod", seed, count, sha256]``: `_facts_line` of `_facts_of` over
  ``count`` texts `_random_text` draws from ``seed``, one line each with
  a LF, hashed in order.
- ``["mcd", reads, at, answer]``: `_machine_config_dies` with a `reads`
  that answers the `ConfigValues` ``reads`` describes, or raises the
  class it names.

``origin_gitfiles_facts_posix.jsonl`` (POSIX only, `generate.POSIX_ONLY`:
`_global_attributes_path` joins with the host's `os.path`, which is
`ntpath` on Windows, and the held directories need links and modes):

- ``["gap", xdg, home, root, answer]``: `_global_attributes_path(Path(root))`
  under XDG_CONFIG_HOME and HOME as given (null: unset), ``root`` spelled
  as ``os.fspath(Path(root))`` spells it (the native argument).
- ``["hold", steps, answers]``: a scenario in a fresh scratch folder
  (``<root>``): each step's answer in order (`_run_step`).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import gitconfig, gitsystem
from bettermemory.origin_gitfiles import (
    _config_variable_names,
    _facts_of,
    _global_attributes_path,
    _hold_directory,
    _machine_config_dies,
    _machine_value_dies,
    _rev_parse_dies,
    directories_held,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root

#: The pause before each change to a scenario's tree: a kernel that
#: stamps a change with a coarse clock (Linux, a tick) could otherwise
#: give a change the stamp already held.
PAUSE = 0.05

VALUES: list[bytes | None] = [
    None, b"", b"\0", b"\0x", b"true", b"True", b"YES", b"on", b"no", b"Off",
    b"false", b"FALSE", b"0", b"1", b"2", b"-1", b"+1", b"-0", b"0x10", b"010",
    b"08", b"1k", b"1K", b"1m", b"1g", b"2g", b"3G", b"2147483647",
    b"2147483648", b"9223372036854775808", b"1-", b" 1", b"1 ", b"1\0-",
    b"input", b"INPUT", b"input\0x", b"warn", b"Warn", b"always", b"ALWAYS",
    b"Always", b"always\0x", b"inherit", b"Inherit", b"simple", b"SIMPLE",
    b"never", b"local", b"remote", b"nothing", b"matching", b"tracking",
    b"upstream", b"current", b"skipping", b"NOOP", b"consecutive",
    b"default", b"x", b"true\0junk",
]  # fmt: skip

TABLES: list[bytes] = sorted(
    {
        b"core.filemode", b"core.ignorecase", b"core.precomposeunicode",
        b"core.symlinks", b"core.sparsecheckout", b"core.sparsecheckoutcone",
        b"core.trustctime", b"core.quotepath", b"core.ignorestat",
        b"core.protecthfs", b"core.protectntfs", b"core.warnambiguousrefs",
        b"core.multipackindex", b"core.commitgraph", b"user.useconfigonly",
        b"color.pager", b"pager.color", b"sparse.expectfilesoutsideofpatterns",
        b"extensions.preciousobjects", b"extensions.worktreeconfig",
        b"feature.manyfiles", b"pack.usesparse", b"pack.usepathwalk",
        b"pack.readreverseindex", b"pack.usebitmapboundarytraversal",
        b"commitgraph.readchangedpaths", b"gc.writecommitgraph",
        b"fetch.writecommitgraph", b"core.fscache", b"core.longpaths",
        b"core.preloadindex", b"core.excludesfile", b"core.attributesfile",
        b"core.editor", b"core.askpass", b"core.whitespace", b"core.fsync",
        b"core.fsyncmethod", b"core.checkroundtripencoding",
        b"core.untrackedcache", b"user.name", b"user.email", b"author.name",
        b"author.email", b"committer.name", b"committer.email",
        b"i18n.commitencoding", b"i18n.logoutputencoding", b"attr.tree",
        b"extensions.partialclone", b"core.notesref", b"core.hookspath",
        b"mailmap.file", b"core.bigfilethreshold", b"core.autocrlf",
        b"core.safecrlf", b"core.logallrefupdates", b"push.default",
        b"branch.autosetuprebase", b"branch.autosetupmerge", b"core.bare",
        b"core.worktree", b"core.pager", b"core.sshcommand", b"core.eol",
        b"core.fsmonitor", b"core.splitindex", b"core.alternaterefscommand",
        b"core.alternaterefsprefixes", b"extensions.noop",
        b"pack.packsizelimit", b"pack.compression",
        b"core.repositoryformatversion", b"fetch.negotiationalgorithm",
        b"commitgraph.generationversion", b"commitgraph.changedpathsversion",
    }
)  # fmt: skip

OTHER_NAMES: list[bytes] = [
    b"advice.statushints", b"advice.", b"advice.x.y", b"advicex.y",
    b"include.path", b"include.x", b"includeif.gitdir:x.path",
    b"includeif.path", b"includeif..path", b"includeif.x.y",
    b"core.unknown", b"core.", b"extensions.objectformat", b"feature.x",
    b"index.version", b"color.advice", b"color.advice.hint", b"color.advicex",
    b"color.ui", b"pack.windowmemory", b"foo.bar", b"remote.origin.url",
    b"user.name.x", b"", b"core", b"Core.filemode",
]  # fmt: skip

# A constant answer (true, false or null), then real releases on each side
# of the 2.35 and 2.37 thresholds, which the reference compares itself.
ATS: list[bool | list[int] | None] = [
    None,
    True,
    False,
    [2, 34],
    [2, 35],
    [2, 36],
    [2, 37],
]


def _spell(data: bytes | None) -> str | None:
    if data is None:
        return None
    if all(0x20 <= c < 0x7F and c != 0x5C for c in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _unspell(text: str | None) -> bytes | None:
    if text is None:
        return None
    if text.startswith("="):
        return text[1:].encode("ascii")
    return bytes.fromhex(text)


@contextmanager
def _at(answer: bool | list[int] | None) -> Iterator[None]:
    def is_at_least(release: tuple[int, int]) -> bool | None:
        if isinstance(answer, list):
            return (answer[0], answer[1]) >= release
        return answer

    with mock.patch.object(gitsystem, "is_at_least", is_at_least):
        yield


def _bits(fn: Callable[[bytes, bytes | None], bool], name: bytes) -> str:
    return "".join("1" if fn(name, v) else "0" for v in VALUES)


def _facts_line(data: bytes) -> str:
    facts = _facts_of(data)
    names = _config_variable_names(data)
    if (names is None) != (facts.names is None) or (
        names is not None and frozenset(names) != facts.names
    ):
        raise AssertionError("_config_variable_names and _facts_of disagree")
    spelled = (
        "-" if facts.names is None else ",".join(n.hex() for n in sorted(facts.names))
    )
    flags = (
        facts.attributes_elsewhere,
        facts.bare_moves,
        facts.git_dies,
        facts.includes,
    )
    return spelled + "|" + "".join("1" if f else "0" for f in flags)


FACT_TEXTS: list[bytes] = [
    b"", b"\n", b"[core]\n", b"[core]\n\tbare = false\n", b"[core]\n\tbare = true\n",
    b"[core]\nbare=false", b"[core]\n  bare\t=\tno \t\n", b"[core]\n\tbare = off\r\n",
    b"[core]\n\tbare = 0\r\r\n", b"[core]\n\tBARE = FALSE\n", b"[core]\n\tbare = false\r",
    b"[core]\n\tbare = false ; comment\n", b"[core]\n\tbare\n", b"[core]\n\tbare =\n",
    b"[core]\n\tbare = 00\n", b"[core]\n\tbare = nope\n", b"[core]\n\tbarely = 1\n",
    b"[core]\n\tbare = \"false\"\n", b"# bare\n", b"[core]\n\tbare = false\n\tbare = no\n",
    b"[core]\n\t bare= false\n", b"[core]\n\tbare = false\x0b\n", b"\tbare = false\n",
    b"[core]\n\tattributesFile = x\n", b"[ATTR]\n\ttree = x\n", b"[attr]\n", b"; [attr\n",
    b"[core]\n\tfilemode = maybe\n", b"[core]\n\tfilemode = true\n",
    b"[core]\n\tfilemode\n", b"[core]\n\trepositoryformatversion = 2\n",
    b"[core]\n\trepositoryformatversion = 1\n", b"[include]\n\tpath = x\n",
    b"[includeIf \"gitdir:/x\"]\n\tpath = y\n", b"[includeIf \"gitdir:/x\"]\n\tother = y\n",
    b"[include]\n\tx = 1\n", b"[branch]\n\tautosetupmerge = inherit\n",
    b"[branch]\n\tautosetupmerge = ALWAYS\n", b"[user]\n\tname\n", b"[user]\n\tname = x\n",
    b"[advice]\n\tstatusHints = maybe\n", b"[core]\n\tbigFileThreshold = -1\n",
    b"[core]\n\tbare = false\n\0", b"[core]\n\tfilemode = x\0y\n", b"[core\n",
    b"[core]\n\t1bad = x\n", b"\xef\xbb\xbf[core]\n\tbare = false\n", b"\xef[core]\n",
    b"[core]\n\tx = \"unterminated\n", b"[log \"follow\"]\n\tx = 1\n",
    b"[core]\r\n\tbare = false\r\n", b"[core] bare = false\n",
    b"[core]\n\tbare = false\n[core]\n\teditor\n", b"[foo]\n\tbar\n",
    b"[core]\n\tpager = less\n", b"[core]\n\tunknownthing = 1\n",
]  # fmt: skip

TOKENS: list[bytes] = [
    b"[core]", b"[attr", b"[include]", b"[includeIf \"gitdir:x\"]", b"[branch]",
    b"[advice]", b"[user]", b"bare", b"BARE", b"filemode", b"path", b"name",
    b"attributesFile", b"autosetupmerge", b"repositoryformatversion", b"x",
    b"=", b" ", b"\t", b"\r", b"\n", b"\n", b"\n", b"false", b"no", b"off",
    b"0", b"1", b"2", b"true", b"maybe", b"inherit", b"\"", b"#", b";", b"\\",
    b"\0", b".", b"-",
]  # fmt: skip

MULT = 6364136223846793005
INCR = 1442695040888963407


def _random_texts(seed: int, count: int) -> Iterator[bytes]:
    """Texts of 0 to 39 tokens of `TOKENS`: the state steps
    ``s = s * MULT + INCR mod 2**64`` and each draw is ``(s >> 33) % n``,
    the length first."""
    state = seed

    def draw(n: int) -> int:
        nonlocal state
        state = (state * MULT + INCR) % (1 << 64)
        return (state >> 33) % n

    for _ in range(count):
        length = draw(40)
        yield b"".join(TOKENS[draw(len(TOKENS))] for _ in range(length))


SEED = 20261121
COUNT = 5000


def _values_of(
    spec: list[Any],
) -> dict[tuple[str, str | None, str], list[bytes | None]]:
    out: dict[tuple[str, str | None, str], list[bytes | None]] = {}
    for section, sub, key, values in spec:
        name = (
            section,
            None
            if sub is None
            else (_unspell(sub) or b"").decode("utf-8", "surrogateescape"),
            key,
        )
        out[name] = [_unspell(v) for v in values]
    return out


def _config_values(spec: dict[str, Any]) -> gitconfig.ConfigValues:
    read = gitconfig.ConfigValues()
    read.update(_values_of(spec.get("v", [])))
    read.undecided = _values_of(spec.get("u", []))
    read.complete = spec.get("c", True)
    read.dies = spec.get("d")
    read.maybe_dies = spec.get("m")
    read.asks_remote_urls = spec.get("ar", False)
    read.forbidden = spec.get("fb", False)
    return read


def _v(section: str, sub: str | None, key: str, *values: str | None) -> list[Any]:
    return [section, sub, key, list(values)]


MCD: list[Any] = [
    [],
    [{}],
    [{}, {}],
    {"raise": "OSError"},
    {"raise": "ValueError"},
    [{"d": "dies"}],
    [{"m": "maybe"}],
    [{"c": False}],
    [{}, {"c": False}],
    [{"ar": True}],
    [{"fb": True}],
    [{"ar": True, "fb": True, "v": [_v("core", None, "filemode", "=true")]}],
    [{"v": [_v("core", None, "filemode", "=true")]}],
    [{"v": [_v("core", None, "filemode", "=maybe")]}],
    [{"v": [_v("core", None, "filemode", "=true", "=maybe")]}],
    [{"v": [_v("core", None, "filemode", None)]}],
    [{"v": [_v("core", None, "bare", "=maybe")]}],
    [{"v": [_v("core", None, "bare", "=yes")]}],
    [{"v": [_v("core", None, "bare", None)]}],
    [{"v": [_v("core", None, "bare", "=")]}],
    [{"v": [_v("core", None, "editor", None)]}],
    [{"v": [_v("core", None, "editor", "=vi")]}],
    [{"v": [_v("include", None, "path", None)]}],
    [{"v": [_v("include", None, "path", "=x")]}],
    [{"v": [_v("includeif", "=gitdir:/x", "path", "=y")]}],
    [{"v": [_v("includeIf", "=gitdir:/x", "path", "=y")]}],
    [{"v": [_v("core", None, "unknown", "=1")]}],
    [{"v": [_v("core", None, "pager", "=less")]}],
    [{"v": [_v("remote", "=origin", "url", "=x")]}],
    [{"v": [_v("color", "=advice", "hint", "=red")]}],
    [{"v": [_v("color", "=x", "advice", "=red")]}],
    [{"v": [_v("advice", "=", "x", "=maybe")]}],
    [{"v": [_v("branch", None, "autosetupmerge", "=inherit")]}],
    [{"v": [_v("branch", None, "autosetupmerge", "=ALWAYS")]}],
    [{"v": [_v("branch", None, "autosetupmerge", "=simple")]}],
    [{"v": [_v("x", "ff80", "y", "=1")]}],
    [{"u": [_v("core", None, "filemode", "=maybe")]}],
    [{"u": [_v("core", None, "filemode", "=true")]}],
    [{"u": [_v("include", None, "path", None)]}],
    [{}, {"v": [_v("user", None, "name", None)]}],
    [{"v": [_v("user", None, "name", "=me")]}, {"u": [_v("core", None, "repositoryformatversion", "=2")]}],
    [{"d": "dies", "v": [_v("core", None, "filemode", "=true")]}],
]  # fmt: skip


def _mcd(spec: Any) -> bool:
    if isinstance(spec, dict):
        kind = {"OSError": OSError, "ValueError": ValueError}[spec["raise"]]

        def raising(git_dir: str) -> tuple[gitconfig.ConfigValues, ...]:
            raise kind(git_dir)

        return _machine_config_dies(raising, Path("/g"))
    reads = tuple(_config_values(s) for s in spec)
    return _machine_config_dies(lambda _git_dir: reads, Path("/g"))


def generate() -> dict[str, bytes]:
    rows: list[Any] = []
    for name in TABLES + OTHER_NAMES:
        ats = ATS if name == b"branch.autosetupmerge" else [None]
        for at in ats:
            with _at(at):
                rows.append(["rpd", _spell(name), at, _bits(_rev_parse_dies, name)])
    for name in [
        b"core.bare",
        b"core.filemode",
        b"core.editor",
        b"core.unknown",
        b"x.y",
    ]:
        rows.append(["mvd", _spell(name), None, _bits(_machine_value_dies, name)])
    with _at(True):
        rows.append(["mvd", "=branch.autosetupmerge", True,
                     _bits(_machine_value_dies, b"branch.autosetupmerge")])  # fmt: skip
    for text in FACT_TEXTS:
        for at in ATS if b"autosetupmerge" in text else [None]:
            with _at(at):
                rows.append(["fo", _spell(text), at, _facts_line(text)])
    with _at(None):
        digest = hashlib.sha256()
        for text in _random_texts(SEED, COUNT):
            digest.update(_facts_line(text).encode("ascii") + b"\n")
        rows.append(["fod", SEED, COUNT, digest.hexdigest()])
    for spec in MCD:
        for at in ATS if "autosetupmerge" in repr(spec) else [None]:
            with _at(at):
                rows.append(["mcd", spec, at, _mcd(spec)])
    kinds = {
        k: sum(1 for r in rows if r[0] == k) for k in ("rpd", "mvd", "fo", "fod", "mcd")
    }
    header = {
        "fixture": "origin_gitfiles_facts",
        "values": [_spell(v) for v in VALUES],
        "tokens": [_spell(t) for t in TOKENS],
        **kinds,
    }
    return {"origin_gitfiles_facts.jsonl": jsonl([header, *rows])}


GAP: list[tuple[str | None, str | None, str]] = [
    (None, None, "/r"),
    (None, "/h/u", "/r"),
    (None, "", "/r"),
    (None, "rel", "/r"),
    (None, "rel", "r/s"),
    ("", None, "/r"),
    ("", "/h/u", "/r"),
    ("/xdg", None, "/r"),
    ("/xdg", "/h/u", "/r"),
    ("/xdg/", "/h/u", "/r"),
    ("xdg", "/h/u", "/r/"),
    ("xdg", None, "."),
    ("xdg", None, ""),
    ("xdg", None, "r//s/"),
    ("//x", None, "/r"),
    (None, "/", "/r"),
    (None, "~", "/r"),
]


def _gap(xdg: str | None, home: str | None, root: str) -> str | None:
    for name, value in (("XDG_CONFIG_HOME", xdg), ("HOME", home)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    return _global_attributes_path(Path(root))


Step = list[Any]

SCENARIOS: list[list[Step]] = [
    # A folder held, then an entry created in it and removed.
    [["mkdir", "d"], ["hold", "<root>/d"], ["held"], ["write", "d/f", "x"], ["held"]],
    [["mkdir", "d"], ["write", "d/f", "x"], ["hold", "<root>/d"], ["rm", "d/f"], ["held"]],
    # A file's own write moves it; its folder's stamp does not move.
    [["mkdir", "d"], ["write", "d/f", "x"], ["hold", "<root>/d"], ["write", "d/f", "yy"],
     ["held"], ["hold", "<root>/d/f"], ["held"], ["write", "d/f", "zzz"], ["held"]],
    # A missing path holds its nearest existing ancestor, then creating it.
    [["mkdir", "d"], ["hold", "<root>/d/a/b/c"], ["held"], ["mkdir", "d/a"], ["held"]],
    [["hold", "<root>/d/a"], ["hold", "<root>/e"], ["held"]],
    # Held already: not read again, the first stamp kept.
    [["mkdir", "d"], ["hold", "<root>/d"], ["write", "d/f", "x"], ["hold", "<root>/d"],
     ["held"]],
    [["mkdir", "d"], ["hold", "<root>/d"], ["hold", "<root>/d/missing"], ["held"]],
    # Links followed.
    [["mkdir", "d"], ["link", "ld", "d"], ["hold", "<root>/ld"], ["held"],
     ["write", "d/f", "x"], ["held"]],
    [["mkdir", "d"], ["link", "ld", "d"], ["hold", "<root>/ld/missing/x"], ["held"]],
    [["link", "dang", "nothing"], ["hold", "<root>/dang/x"], ["held"],
     ["mkdir", "nothing"], ["held"]],
    [["link", "dang", "nothing"], ["hold", "<root>/dang"]],
    [["link", "loop", "loop"], ["hold", "<root>/loop/x"], ["hold", "<root>/loop"]],
    # Through a file: NotADirectoryError, read as missing.
    [["write", "f", "x"], ["hold", "<root>/f/x/y"], ["held"]],
    [["write", "f", "x"], ["link", "lf", "f"], ["hold", "<root>/lf/x"], ["held"]],
    # A stat that fails for another reason, and a NUL.
    [["mkdir", "locked"], ["mkdir", "locked/in"], ["chmod", "locked", 0],
     ["hold", "<root>/locked/in/x"], ["hold", "<root>/locked"], ["chmod", "locked", 0o755]],
    [["hold", "<root>/a\x00b"], ["hold", "<root>/missing/a\x00b"]],
    [["hold", ""]],
    # Removed after holding, and a key that cannot be read.
    [["mkdir", "d"], ["hold", "<root>/d"], ["rmdir", "d"], ["held"]],
    [["mkdir", "d"], ["hold", "<root>/d"], ["fake", "<root>/a\x00b"], ["held"]],
    [["fake", "<root>/nothing"], ["held"]],
    [["held"]],
    # A chmod moves the change time alone; the pair is compared whole.
    [["mkdir", "d"], ["hold", "<root>/d"], ["chmod", "d", 0o700], ["held"]],
    [["write", "f", "x"], ["hold", "<root>/f"], ["chmod", "f", 0o600], ["held"]],
    # A rename over a held file moves its pair.
    [["write", "f", "x"], ["write", "g", "x"], ["hold", "<root>/f"], ["rename", "g", "f"],
     ["held"]],
]  # fmt: skip


def _run_step(step: Step, root: str, held: dict[str, tuple[int, int]]) -> Any:
    kind = step[0]

    def at(rel: str) -> str:
        return os.path.join(root, rel)

    def spelled(path: str) -> str:
        return path.replace(root, "<root>")

    if kind in ("mkdir", "write", "rm", "rmdir", "link", "rename", "chmod"):
        time.sleep(PAUSE)
    if kind == "mkdir":
        os.mkdir(at(step[1]))
    elif kind == "write":
        Path(at(step[1])).write_bytes(step[2].encode("ascii"))
    elif kind == "rm":
        os.unlink(at(step[1]))
    elif kind == "rmdir":
        os.rmdir(at(step[1]))
    elif kind == "link":
        os.symlink(step[2], at(step[1]))
    elif kind == "rename":
        os.replace(at(step[1]), at(step[2]))
    elif kind == "chmod":
        os.chmod(at(step[1]), step[2])
    elif kind == "fake":
        held[step[1].replace("<root>", root)] = (0, 0)
    elif kind == "hold":
        try:
            _hold_directory(step[1].replace("<root>", root), held)
        except (OSError, ValueError) as exc:
            return {"e": type(exc).__name__}
        return sorted(spelled(k) for k in held)
    elif kind == "held":
        return directories_held(held)
    else:
        raise ValueError(kind)
    return None


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    rows: list[Any] = []
    with _sandbox.world() as world:
        for xdg, home, root in GAP:
            spelled = os.fspath(Path(root))
            rows.append(["gap", xdg, home, spelled, _gap(xdg, home, root)])
        base = world.root / "ogf-facts"
        for i, steps in enumerate(SCENARIOS):
            scratch = base / str(i)
            scratch.mkdir(parents=True)
            root = os.path.realpath(scratch)
            held: dict[str, tuple[int, int]] = {}
            answers = [_run_step(step, root, held) for step in steps]
            shutil.rmtree(scratch)
            rows.append(["hold", steps, answers])
    header = {
        "fixture": "origin_gitfiles_facts_posix",
        "gap": sum(1 for r in rows if r[0] == "gap"),
        "hold": sum(1 for r in rows if r[0] == "hold"),
    }
    return {"origin_gitfiles_facts_posix.jsonl": jsonl([header, *rows])}
