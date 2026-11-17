"""Fixtures: `sources` over a held cache (``gitsystem_sources.jsonl``,
``gitsystem_sources_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitsystem/sources.rs``. This pins
it to `gitsystem`'s `sources`, `reads_compat` and `is_at_least`, with the
held answers (`_HELD`), and through them `_key`, `_developer`, `_current`,
`_kept`, `_recorded`, `_record` and `_ask`.

No git runs: git, the clocks, ``os.environ`` and ``os.confstr`` are the
case's, as in `gen_gitsystem_ask`. ``_MACOS`` is the case's and
`XCODE_SELECT_LINK` is ``R/xcode``. Both files are POSIX only (the ask
reads modes on disk).

A case is a SEQUENCE of calls over one held cache, in a scratch folder
``R`` (state directory ``R/state``, working directory ``R``):

- ``t``: the tree (default `gen_gitsystem_ask`'s: ``bin/git``, 0755);
- ``mac``: ``_MACOS`` (default false);
- ``s``: the steps, each:
  - ``do``: ``"s"`` (`sources()`), ``"s0"`` (`sources(stamp=False)`),
    ``"rc"`` (`reads_compat()`) or ``[major, minor]`` (`is_at_least`);
  - ``now``: the wall clock (`repr`), which every read of it gives;
  - ``e``: the environment from this step on (``@`` for ``R``; default
    PATH ``@/bin``);
  - ``g``: git's responses (default the header's ``git``);
  - ``b``: an active budget (``"new"`` or its deadline), this step only;
  - ``pre``: done first: ``["clear"]`` (`_caches`' clear),
    ``["w", rel, bytes]`` (write a file), ``["rm", rel]``,
    ``["l", rel, target]`` (a link, replacing what is there),
    ``["m", rel, mode]``, ``["cp", rel, to]``, ``["edit", field, json]``
    (the record's first entry's field, the record written again as
    ``json.dumps`` and a newline);
  - a response's ``cp`` (``[from, to]``) or ``rm`` (``rel``) is done as
    git is asked, before it answers (another process meanwhile).

Each step answers ``q`` (the commands, as `gen_gitsystem_ask`'s, by
index in the header's ``commands``), ``r`` (the answer, or the class
raised), ``n`` (each clock's reads), ``h`` (the held answers afterwards,
oldest first, each ``[key, answer]``) and, where the record changed,
``rec`` (null where there is none; where it has `_record`'s layout,
its entries, by index in the header's ``entries``: each its
``json.dumps`` text with the stamp as whether it is its git's stamp now;
else its text). Held answers are by index in the header's ``answers``.
``@`` stands for ``R``
and a PATH's key for ``K<i>``, its order of first use in the case.

A case goes in ``_python`` where a version part runs past `int`'s 4,300
digits (`gitversion.parse` raises).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _daemon_client, _git, git_read_budget, gitconfig, gitsystem

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitsystem_ask import (
    CLOCK,
    DEFAULT_TREE,
    GIT,
    _answer,
    _lab,
    _Reads,
    _Script,
    _spell_response,
)
from .gen_gitsystem_probe import _make, _open_up
from .gen_gitsystem_record import _sp, _t

RECORD = "state/" + gitsystem.RECORD
REASK = gitsystem.REASK_SECONDS
RETRY = gitsystem.RETRY_SECONDS
NO_VERSION = {"x": 0, "o": b"version 2\n"}
UNSAID_PROBE = {"x": 128, "o": b""}


def _git_with(i: int, response: Any) -> list[Any]:
    script = list(GIT)
    script[i] = response
    return script


def _version(text: bytes) -> list[Any]:
    return _git_with(2, {"x": 0, "o": b"git version " + text + b"\n"})


FAIL = ["n"]


def _record_text(entries: list[dict[str, Any]], fmt: int = 5) -> bytes:
    return json.dumps({"format": fmt, "entries": entries}).encode()


def _entry(path: str, **kw: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path,
        "git": "/usr/bin/git",
        "stamp": None,
        "asked": 1000.0,
        "ok": False,
        "own": [],
        "system": "/etc/gitconfig",
    }
    entry.update(kw)
    return entry


def S(now: float, do: Any = "s", **kw: Any) -> dict[str, Any]:
    return {"do": do, "now": now, **kw}


def _cases() -> Iterator[dict[str, Any]]:
    t0 = 1000.0
    # A second call inside and outside each window: the whole answer
    # (REASK), and the clock gone back.
    for dt in (0.0, 1.0, REASK - 1, REASK, REASK + 1, -1.0):
        yield {"s": [S(t0), S(t0 + dt)]}
    # A partial answer (the probe unsaid; no version): RETRY.
    for script in (_git_with(3, UNSAID_PROBE), _git_with(2, NO_VERSION)):
        for dt in (RETRY - 1, RETRY, RETRY + 1):
            yield {"s": [S(t0, g=script), S(t0 + dt, g=script)]}
    # The fallback (no git ran): asked again after RETRY; never replacing
    # an answer git gave.
    for dt in (RETRY - 1, RETRY):
        yield {"s": [S(t0, g=FAIL), S(t0 + dt, g=FAIL)]}
    yield {"s": [S(t0, g=FAIL), S(t0 + RETRY)]}
    # Kept: a good answer, then a failed ask past its life: kept from now
    # for RETRY, then asked again.
    for dt in (RETRY - 1, RETRY, RETRY + 1):
        yield {
            "s": [
                S(t0),
                S(t0 + REASK, g=FAIL),
                S(t0 + REASK + dt, g=FAIL),
            ]
        }
    # The same with the cache cleared: kept from the record alone.
    yield {
        "s": [
            S(t0),
            S(t0 + REASK, g=FAIL, pre=[["clear"]]),
            S(t0 + REASK + 1, pre=[["clear"]]),
        ]
    }
    # A partial answer, then a failed ask: the partial is kept.
    yield {
        "s": [
            S(t0, g=_git_with(3, UNSAID_PROBE)),
            S(t0 + RETRY, g=FAIL),
            S(t0 + RETRY + 1, g=FAIL),
        ]
    }
    # stamp=False: the held answer while young enough without stating git.
    yield {
        "s": [
            S(t0),
            S(t0 + 1, "s0", pre=[["w", "bin/git", b"#!changed"]]),
            S(t0 + 2, "s", pre=[]),
            S(t0 + 3, "s0", pre=[["clear"]]),
        ]
    }
    yield {"s": [S(t0), S(t0 + REASK, "s0")]}
    # A recorded answer is stamped whatever `stamp` says: recorded, the
    # cache cleared, git changed, then stamp=False.
    yield {
        "s": [
            S(t0),
            S(t0 + 1, "s0", pre=[["clear"], ["w", "bin/git", b"#!changed"]]),
        ]
    }
    # reads_compat stamps the held answer: git changed while it is held.
    yield {"s": [S(t0, "rc"), S(t0 + 1, "rc", pre=[["w", "bin/git", b"#!changed"]])]}
    # The record's time edited to doubles a lossy parse gets wrong.
    for asked in ("1000.0000000000001", "0.30000000000000004"):
        yield {"s": [S(t0), S(t0 + 1, pre=[["clear"], ["edit", "asked", asked]])]}
    # The git binary's stamp changing: replaced, removed, a link moved.
    yield {"s": [S(t0), S(t0 + 1, pre=[["w", "bin/git", b"#!changed"]])]}
    yield {"s": [S(t0), S(t0 + 1, pre=[["m", "bin/git", 0o700]])]}
    yield {"s": [S(t0), S(t0 + 1, pre=[["rm", "bin/git"]], g=FAIL)]}
    yield {
        "t": [
            *DEFAULT_TREE,
            ["d", "b2"],
            ["f", "b2/real", b"#!"],
            ["m", "b2/real", 0o755],
            ["l", "b2/git", "@/b2/real"],
        ],
        "s": [
            S(t0, e=[["PATH", "@/b2"]]),
            S(t0 + 1, pre=[["l", "b2/git", "@/bin/git"]]),
        ],
    }
    # The record written by one cache and read by a fresh one; the
    # record's answer standing for the held one's PATH.
    yield {"s": [S(t0), S(t0 + 1, pre=[["clear"]])]}
    yield {
        "s": [
            S(t0),
            S(t0 + 1, pre=[["clear"], ["w", "bin/git", b"#!changed"]]),
        ]
    }
    # PATH changing between calls; back again; unset; empty.
    two = [
        *DEFAULT_TREE,
        ["d", "b2"],
        ["f", "b2/git", b"#!2"],
        ["m", "b2/git", 0o755],
    ]
    yield {
        "t": two,
        "s": [
            S(t0),
            S(t0 + 1, e=[["PATH", "@/b2"]]),
            S(t0 + 2, e=[["PATH", "@/bin"]]),
            S(t0 + 3, e=[["PATH", "@/b2:@/bin"]]),
            S(t0 + 4, e=[]),
            S(t0 + 5, e=[["PATH", ""]]),
            S(t0 + 6, e=[["PATH", "@/b2"]], pre=[["clear"]]),
        ],
    }
    # DEVELOPER_DIR and the xcode link changing, on macOS and off it.
    for mac in (True, False):
        yield {
            "mac": mac,
            "s": [
                S(t0),
                S(t0 + 1, e=[["PATH", "@/bin"], ["DEVELOPER_DIR", "/d1"]]),
                S(t0 + 2, e=[["PATH", "@/bin"], ["DEVELOPER_DIR", "/d2"]]),
                S(t0 + 3, e=[["PATH", "@/bin"]], pre=[["l", "xcode", "/x1"]]),
                S(t0 + 4, pre=[["l", "xcode", "/x2"]]),
                S(t0 + 5, pre=[["rm", "xcode"]]),
                S(t0 + 6, e=[["PATH", "@/bin"], ["DEVELOPER_DIR", "/d1"]]),
            ],
        }
    # The eight-PATH bound: nine PATHs, the first leaving; a held one
    # used again is not moved; one asked again moves to the end.
    many = [*DEFAULT_TREE] + [["d", f"p{i}"] for i in range(10)]
    steps = [S(t0 + i, e=[["PATH", f"@/p{i}:@/bin"]]) for i in range(9)]
    steps.append(S(t0 + 9, e=[["PATH", "@/p0:@/bin"]], pre=[["rm", RECORD]]))
    yield {"t": many, "s": steps}
    steps = [S(t0 + i, e=[["PATH", f"@/p{i}:@/bin"]]) for i in range(8)]
    steps.append(S(t0 + 8, e=[["PATH", "@/p0:@/bin"]]))
    steps.append(S(t0 + 9, e=[["PATH", "@/p8:@/bin"]]))
    steps.append(S(t0 + 10, e=[["PATH", "@/p1:@/bin"]]))
    steps.append(S(t0 + 11, e=[["PATH", "@/p0:@/bin"]]))
    yield {"t": many, "s": steps}
    steps = [S(t0 + i, e=[["PATH", f"@/p{i}:@/bin"]]) for i in range(8)]
    steps.append(S(t0 + REASK + 1, e=[["PATH", "@/p0:@/bin"]]))
    steps.append(S(t0 + REASK + 2, e=[["PATH", "@/p8:@/bin"]]))
    yield {"t": many, "s": steps}
    # The record: damaged, another format, another key, too big, an
    # entry that is not one, one recorded by a racing process.
    other = "0" * 32
    for data in (
        b"{",
        b"[]",
        _record_text([_entry(other, ok=True)], fmt=4),
        _record_text([_entry(other)]),
        b" " * (1 << 16) + b"{}",
        json.dumps({"format": 5, "entries": [1, None, "x"]}).encode(),
    ):
        yield {"s": [S(t0, pre=[["w", RECORD, data]]), S(t0 + 1)]}
    yield {"s": [S(t0), S(t0 + 1, pre=[["clear"], ["w", RECORD, b"{"]])]}
    # Nine entries of other PATHs: ours first, the eighth leaves.
    entries = [_entry(f"{i:032x}") for i in range(9)]
    yield {"s": [S(t0, pre=[["w", RECORD, _record_text(entries)]])]}
    # A racing process's good answer recorded while this one's ask fails.
    # (git's first answer puts back the record saved after the first
    # step, as another process would, and git then fails).
    yield {
        "s": [
            S(t0, pre=[]),
            S(t0 + 1, pre=[["cp", RECORD, "saved"], ["clear"], ["rm", RECORD]]),
            S(
                t0 + 2,
                g=[{"x": 0, "o": b"/etc/gitconfig\n", "cp": ["saved", RECORD]}, "n"],
                pre=[["clear"], ["rm", RECORD]],
            ),
        ]
    }
    # The record rewritten by another process with another answer of the
    # same time, then removed while git is asked, which fails: of the held
    # answer and the one read before the ask, the held one is kept.
    yield {
        "s": [
            S(t0),
            S(
                t0 + REASK,
                g=[{"x": 0, "o": b"/etc/gitconfig\n", "rm": RECORD}, "n"],
                pre=[["edit", "own", '["/other"]']],
            ),
        ]
    }
    # The ask raising at each step: nothing held or recorded changes.
    for at in range(3):
        steps = [S(t0 + i * (REASK + 1)) for i in range(3)]
        steps[at] = {**steps[at], "b": repr(1.0)}
        yield {"s": steps}
    yield {"s": [S(t0), S(t0 + REASK, b="new", g=["t"])]}
    for at in range(2):
        steps = [S(t0 + i * (REASK + 1)) for i in range(2)]
        steps[at] = {**steps[at], "g": _version(b"9" * 5000 + b".1.2")}
        yield {"s": steps}
    # reads_compat over every answer of compat.
    for script in (GIT, _git_with(5, {"x": 128, "o": b""}), _git_with(5, "t"), FAIL):
        yield {"s": [S(t0, "rc", g=script), S(t0 + 1, "rc")]}
    # is_at_least over every version shape and release.
    for text in (
        b"2.54.0",
        b"2.45",
        b"65535.65535.65535",
        b"65536.0.0",
        b"2.65536",
        b"99999999999999999999.0",
        b"007.08.09",
    ):
        g = _version(text)
        yield {
            "s": [
                S(t0, [2, 54], g=g),
                S(t0 + 1, [2, 53]),
                S(t0 + 2, [2, 55]),
                S(t0 + 3, [1, 99]),
                S(t0 + 4, [3, 0]),
                S(t0 + 5, [0, 0]),
                S(t0 + 6, [65535, 65535]),
                S(t0 + 7, [65536, 0]),
                S(t0 + 8, [2, 54], pre=[["clear"]]),
            ]
        }
    for g in (_git_with(2, NO_VERSION), FAIL):
        yield {"s": [S(t0, [2, 0], g=g), S(t0 + 1, [0, 0])]}


def _gated(case: dict[str, Any]) -> str:
    for step in case["s"]:
        for r in step.get("g", []):
            if isinstance(r, dict) and r["o"].startswith(b"git version "):
                if any(len(p) > 4300 for p in r["o"][12:].split(b".")):
                    return "python"
    return ""


def _spell_step(step: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"do": step["do"], "now": repr(float(step["now"]))}
    if "e" in step:
        out["e"] = [[_t(k), _t(v)] for k, v in step["e"]]
    if "g" in step:
        out["g"] = [
            {
                **_spell_response(r, False),
                **{k: r[k] for k in ("cp", "rm") if k in r},
            }
            if isinstance(r, dict) and ("cp" in r or "rm" in r)
            else _spell_response(r)
            for r in step["g"]
        ]
    if "b" in step:
        out["b"] = step["b"]
    if step.get("pre"):
        out["pre"] = [
            [a[0], a[1], _sp(a[2])] if a[0] == "w" else a for a in step["pre"]
        ]
    return out


def _pre(root: Path, action: list[Any]) -> None:
    kind = action[0]
    if kind == "clear":
        gitsystem._clear()
        return
    path = root / action[1]
    if kind == "edit":
        path = root / RECORD
    if kind == "w":
        if not path.parent.exists():
            path.parent.mkdir()
            os.chmod(path.parent, 0o700)
        path.write_bytes(action[2])
    elif kind == "rm":
        path.unlink()
    elif kind == "l":
        if path.is_symlink() or path.exists():
            path.unlink()
        target = action[2]
        os.symlink(str(root) + target[1:] if target.startswith("@/") else target, path)
    elif kind == "m":
        os.chmod(path, action[2])
    elif kind == "cp":
        shutil.copyfile(path, root / action[2])
    elif kind == "edit":
        raw = json.loads((root / RECORD).read_bytes())
        raw["entries"][0][action[1]] = json.loads(action[2])
        (root / RECORD).write_bytes((json.dumps(raw) + "\n").encode())


class _Racing(_Script):
    """`gen_gitsystem_ask`'s script, a response's ``cp`` copying a file
    (``[from, to]``) before it answers."""

    def __call__(self, command: list[str], **kw: Any) -> Any:
        r = self.responses[0] if self.responses else None
        if isinstance(r, dict) and "cp" in r:
            shutil.copyfile(self.root / r["cp"][0], self.root / r["cp"][1])
        if isinstance(r, dict) and "rm" in r:
            (self.root / r["rm"]).unlink()
        if isinstance(r, dict) and ("cp" in r or "rm" in r):
            self.responses[0] = {k: v for k, v in r.items() if k not in ("cp", "rm")}
        return super().__call__(command, **kw)


class _Labels:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def add(self, key: str) -> None:
        self.keys.setdefault(key, f"K{len(self.keys)}")

    def text(self, root: Path, text: str) -> str:
        text = _lab(root, text)
        for key, label in self.keys.items():
            text = text.replace(key, label)
        return text


def _run(root: Path, case: dict[str, Any]) -> list[dict[str, Any]]:
    _make(root, case.get("t", DEFAULT_TREE))
    gitsystem._clear()
    labels = _Labels()
    env_spec = [["PATH", "@/bin"]]
    record = root / RECORD
    before: bytes | None = None
    out: list[dict[str, Any]] = []
    saved = os.getcwd()
    try:
        os.chdir(root)
        for step in case["s"]:
            env_spec = step.get("e", env_spec)
            env = {k: v.replace("@", str(root)) for k, v in env_spec}
            for action in step.get("pre", []):
                _pre(root, action)
            script = _Racing(root, step.get("g", GIT))
            start, every, n = CLOCK
            mono = _Reads([start + i * every for i in range(n)])
            wall = _Reads([float(step["now"])] * 8)
            clock = types.SimpleNamespace(monotonic=mono, time=wall)
            token = None
            if "b" in step:
                b = step["b"]
                deadline = None if b == "new" else float(b)
                token = git_read_budget._ACTIVE.set(
                    git_read_budget.ReadBudget(deadline=deadline)
                )
            try:
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch.object(os, "confstr", lambda name: None),
                    mock.patch.object(
                        _daemon_client, "resolve_state_dir", lambda: root / "state"
                    ),
                    mock.patch.object(gitsystem, "time", clock),
                    mock.patch.object(git_read_budget, "_clock", mono),
                    mock.patch.object(subprocess, "run", script),
                    mock.patch.object(_git.log, "disabled", True),
                    mock.patch.object(gitsystem, "_MACOS", case.get("mac", False)),
                    mock.patch.object(
                        gitsystem, "XCODE_SELECT_LINK", str(root / "xcode")
                    ),
                ):
                    labels.add(gitsystem._key(os.environ.get("PATH")))
                    do = step["do"]
                    answer: Any
                    try:
                        if do in ("s", "s0"):
                            got = gitsystem.sources(stamp=do == "s")
                            answer = _sources(got)
                        elif do == "rc":
                            answer = gitsystem.reads_compat()
                        else:
                            answer = gitsystem.is_at_least((do[0], do[1]))
                    except (OSError, ValueError, git_read_budget.ReadDeadline) as e:
                        answer = type(e).__name__
                    # The held answers' git stamps, while the case's
                    # environment stands.
                    held = [
                        [labels.text(root, k), _held(root, a)]
                        for k, a in gitsystem._HELD.items()
                    ]
            finally:
                if token is not None:
                    git_read_budget._ACTIVE.reset(token)
            line: dict[str, Any] = {
                **_spell_step(step),
                "q": script.asked,
                "r": answer,
                "n": [mono.n, wall.n],
                "h": held,
            }
            now = record.read_bytes() if record.exists() else None
            if now != before:
                line["rec"] = None if now is None else _rec(root, labels, now)
                before = now
            out.append(line)
    finally:
        os.chdir(saved)
        gitsystem._clear()
    return out


def _rec(root: Path, labels: _Labels, data: bytes) -> Any:
    """The record as written: where it is `_record`'s layout, each
    entry's text (``json.dumps``) with its stamp as whether it is the
    entry's git's stamp now (the stamp's numbers are the host's); else
    its text."""
    text = data.decode("ascii")
    try:
        raw = json.loads(text)
        entries = raw["entries"]
        assert text == json.dumps({"format": raw["format"], "entries": entries}) + "\n"
        assert raw["format"] == gitsystem.RECORD_FORMAT
    except (ValueError, KeyError, TypeError, AssertionError):
        return labels.text(root, text)
    out = []
    for entry in entries:
        if isinstance(entry.get("stamp"), list):
            fresh = gitconfig.file_stamp(entry["git"])
            entry["stamp"] = fresh is not None and list(fresh) == entry["stamp"]
        out.append(labels.text(root, json.dumps(entry)))
    return out


def _sources(s: gitsystem.Sources) -> dict[str, Any]:
    return {
        "own": [_t(o) for o in s.own],
        "sy": None if s.system is None else _t(s.system),
        "lg": s.legacy,
        "rs": s.resets,
        "px": None if s.prefix is None else _t(s.prefix),
        "cd": s.compat_differ,
        "hc": s.hasconfig,
        "v": None if s.version is None else [str(p) for p in s.version],
    }


def _held(root: Path, a: gitsystem._Answer) -> dict[str, Any]:
    got = _answer(root, a)
    got["t"] = None if a.tried is None else repr(a.tried)
    return got


def _file(which: str) -> bytes:
    refuse_root()
    cases = [c for c in _cases() if _gated(c) == which]
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = Path(os.path.realpath(world.root / "gitsystem-sources"))
        base.mkdir()
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir()
            os.chmod(root, 0o755)
            steps = _run(root, case)
            _open_up(root)
            shutil.rmtree(root)
            line: dict[str, Any] = {"s": steps}
            if "t" in case:
                line["t"] = [
                    [*e[:2], _sp(e[2]), *e[3:]]
                    if e[0] == "f" and isinstance(e[2], bytes)
                    else e
                    for e in case["t"]
                ]
            if case.get("mac"):
                line["mac"] = True
            lines.append(line)
    table: list[Any] = []
    index: dict[str, int] = {}
    for line in lines:
        for step in line["s"]:
            named = []
            for argv, cwd, env, timeout in step["q"]:
                key = json.dumps([argv, cwd, env])
                if key not in index:
                    index[key] = len(table)
                    table.append([argv, cwd, env])
                named.append([index[key], timeout])
            step["q"] = named
    # Each distinct held answer and record entry once, in the header.
    answers: list[Any] = []
    entries: list[str] = []
    seen_a: dict[str, int] = {}
    seen_e: dict[str, int] = {}

    def at(table: list[Any], seen: dict[str, int], item: Any) -> int:
        k = json.dumps(item)
        if k not in seen:
            seen[k] = len(table)
            table.append(item)
        return seen[k]

    for line in lines:
        for step in line["s"]:
            step["h"] = [[k, at(answers, seen_a, a)] for k, a in step["h"]]
            if isinstance(step.get("rec"), list):
                step["rec"] = [at(entries, seen_e, e) for e in step["rec"]]
    name = "gitsystem_sources" + (f"_{which}" if which else "")
    header = {
        "fixture": name,
        "cases": len(lines),
        "git": [_spell_response(r, False) for r in GIT],
        "clock": [repr(CLOCK[0]), repr(CLOCK[1]), CLOCK[2]],
        "tree": [
            DEFAULT_TREE[0],
            [*DEFAULT_TREE[1][:2], _sp(DEFAULT_TREE[1][2])],
            DEFAULT_TREE[2],
        ],
        "commands": table,
        "answers": answers,
        "entries": entries,
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"gitsystem_sources.jsonl": _file("")}


def generate_python() -> dict[str, bytes]:
    return {"gitsystem_sources_python.jsonl": _file("python")}
