"""Fixtures: `location`'s capture signature and capture cache
(``location_cache.jsonl`` and ``location_cache_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_cache.rs``.

``location_cache.jsonl`` (ungated) is scripted: `signature`, both clocks
(``location.time``), `_capture_uncached`, `config_unchanged`,
`record_working_tree` and `_clear_above` are the case's, so no row reads
a file. A case is a sequence of steps over one cache, cleared first. A
step:

- ``do``: ``cap`` (`capture`), ``cached`` (`_capture_cached`), ``tok``
  (`capture_token`), ``forget`` (`forget(cwd)`), ``fill`` (``n`` calls of
  `_capture_cached` over the cwds ``/f<i>``, each signature ``F``, clock
  ``[0, 0]``, an uncached capture tagged ``f<i>``) or ``ev`` (`_evicted`
  of a stat whose ``st_flags`` and ``st_file_attributes`` are ``fl``);
- ``cwd``, ``wt`` (write_token), ``mf`` (manifest), ``uc`` (use_cache),
  ``rt`` (record_tree), ``b`` (budget; absent, `FETCH_BUDGET`);
- ``sig``: what each call of `signature` answers, in turn: null, or a
  label ``X`` or ``X~n`` standing for ``("repo", ("/c", 1, 1), "X",
  ("token", n))`` (n absent: None);
- ``clk``: each clocks' read ``[monotonic, wall]``; ``cu``: each answer of
  `config_unchanged`; ``rec``: `record_working_tree`'s answer;
- ``un``: each uncached capture: ``s`` state, ``tag`` (its ``detail``),
  ``ev`` evicted, ``tw`` token_written, ``tok`` token, ``ka`` kept_apart,
  ``gd`` git_dir, ``root``.

Each step answers ``a`` (the capture: ``[tag, token, token_written,
token_status, tree_recorded]``; ``capture_token``'s ``json.dumps`` text;
`_evicted`'s bool), ``n`` (calls of signature, clocks, config_unchanged,
record_working_tree, _clear_above), ``u`` (each uncached call's ``[cwd,
write_token, budget, manifest]``) and ``c`` (the cache, oldest first, each
``[cwd, write_token, manifest, tag, token, token_written, token_status,
signature label, monotonic, wall]``; past six entries ``{"n", "head",
"tail"}`` with two of each).

``location_cache_posix.jsonl`` (POSIX only: modes, and the stamps of
real files) holds `signature` over trees built in scratch: ``t`` the
items (``["d", rel]``, ``["f", rel, text]`` with ``@`` for the case's
folder and each character a byte, ``["m", rel, mode]``), ``cwd``, ``mv``
(a discovery variable set), ``d`` each call of `_discover` (``[read_head,
repository]``, the repository ``null``, ``"os"`` or ``{dg, gd, cd, root,
k}``), and ``a`` the signature: null, ``{"o": cwd, "p": slot}`` or
``{"r": {...}}``. A slot is null, ``-1`` (`_stamp`'s ``(-1,) * 5``) or
``{"s": rel}``: the stamp of that path, which the Rust test stamps
itself; HEAD's and config's four numbers are ``{"s": rel}`` too. Paths are
under ``@``. Not run as root.
"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from bettermemory import githead, location

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root

# ---------------------------------------------------------------------------
# Scripted
# ---------------------------------------------------------------------------


def _sig(label: str | None) -> tuple[Any, ...] | None:
    if label is None:
        return None
    base, _, tok = label.partition("~")
    return ("repo", ("/c", 1, 1), base, ("token", int(tok) if tok else None))


def _label(sig: tuple[Any, ...]) -> str:
    tok = sig[3][1]
    return sig[2] + ("" if tok is None else f"~{tok}")


def _capture(cwd: str, spec: dict[str, Any]) -> location.Capture:
    return location.Capture(
        cwd=cwd,
        state=spec.get("s", "repo"),
        detail=spec["tag"],
        evicted=spec.get("ev", False),
        token_written=spec.get("tw"),
        token=spec.get("tok"),
        kept_apart=spec.get("ka", False),
        git_dir=spec.get("gd"),
        root=spec.get("root"),
    )


def _shown(cap: location.Capture) -> list[Any]:
    return [
        cap.detail,
        cap.token,
        cap.token_written,
        cap.token_status,
        cap.tree_recorded,
    ]


def _cache() -> Any:
    rows = [
        [
            k[0],
            k[1],
            k[2],
            *_shown(e.capture)[:4],
            _label(e.signature),
            e.monotonic,
            e.wall,
        ]
        for k, e in location._CACHE.items()
    ]
    if len(rows) > 6:
        return {"n": len(rows), "head": rows[:2], "tail": rows[-2:]}
    return rows


class _Stubs:
    def __init__(self) -> None:
        self.step: dict[str, Any] = {}
        self.n = [0, 0, 0, 0, 0]
        self.u: list[list[Any]] = []
        self.wall = 0.0

    def signature(self, cwd: str, *, read_head: bool = False) -> Any:
        i = self.n[0]
        self.n[0] += 1
        return _sig(self.step["sig"][i])

    def monotonic(self) -> float:
        mono, self.wall = self.step["clk"][self.n[1]]
        self.n[1] += 1
        return float(mono)

    def time(self) -> float:
        return float(self.wall)

    def config_unchanged(self, cap: location.Capture) -> bool:
        i = self.n[2]
        self.n[2] += 1
        return bool(self.step["cu"][i])

    def record(self, git_dir: str, tree: str) -> str:
        self.n[3] += 1
        return str(self.step["rec"])

    def clear_above(self) -> None:
        self.n[4] += 1

    def uncached(
        self, cwd: str, *, write_token: bool, budget: float, manifest: bool
    ) -> Any:
        i = len(self.u)
        self.u.append([cwd, write_token, budget, manifest])
        return _capture(cwd, self.step["un"][i])


@contextmanager
def _stubbed(stubs: _Stubs) -> Iterator[None]:
    clock = types.SimpleNamespace(monotonic=stubs.monotonic, time=stubs.time)
    stand_ins: dict[str, Any] = {
        "signature": stubs.signature,
        "time": clock,
        "_capture_uncached": stubs.uncached,
        "config_unchanged": stubs.config_unchanged,
        "record_working_tree": stubs.record,
        "_clear_above": stubs.clear_above,
    }
    saved = {n: getattr(location, n) for n in stand_ins}
    for n, v in stand_ins.items():
        setattr(location, n, v)
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(location, n, v)


def _run(stubs: _Stubs, step: dict[str, Any]) -> dict[str, Any]:
    stubs.step = step
    stubs.n = [0, 0, 0, 0, 0]
    stubs.u = []
    do = step["do"]
    cwd = step.get("cwd", "/w")
    kw: dict[str, Any] = {"write_token": step.get("wt", False)}
    if "b" in step:
        kw["budget"] = step["b"]
    out: dict[str, Any] = {}
    if do == "ev":
        flags, attrs = step["fl"]
        st = types.SimpleNamespace(st_flags=flags, st_file_attributes=attrs)
        return {"a": location._evicted(st)}  # type: ignore[arg-type]
    if do == "fill":
        for i in range(step["n"]):
            stubs.step = {
                "sig": ["F", "F"],
                "clk": [[0, 0]],
                "cu": [False],
                "un": [{"tag": f"f{i}"}],
            }
            stubs.n = [0, 0, 0, 0, 0]
            stubs.u = []
            location._capture_cached(
                f"/f{i}", write_token=False, budget=0.3, manifest=True, use_cache=True
            )
        stubs.u = []
        stubs.n = [0, 0, 0, 0, 0]
        return {"c": _cache()}
    if do == "forget":
        location.forget(step.get("cwd"))
    elif do == "tok":
        tok = location.capture_token(cwd, manifest=step.get("mf", True), **kw)
        out["a"] = json.dumps(tok)
    elif do == "cached":
        got = location._capture_cached(
            cwd,
            budget=step.get("b", location.FETCH_BUDGET),
            manifest=step.get("mf", True),
            use_cache=step.get("uc", True),
            write_token=kw["write_token"],
        )
        out["a"] = _shown(got)
    else:
        got = location.capture(
            cwd,
            manifest=step.get("mf", True),
            use_cache=step.get("uc", True),
            record_tree=step.get("rt", True),
            **kw,
        )
        out["a"] = _shown(got)
    out["n"] = stubs.n
    out["u"] = stubs.u
    out["c"] = _cache()
    return out


def _c(**kw: Any) -> dict[str, Any]:
    return {"do": "cap", **kw}


_KEPT = {"tag": "k", "tok": "T0", "gd": "/g", "root": "/w"}

_CASES: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "a miss keeps, a hit answers from the cache",
        [
            _c(sig=["A", "A"], clk=[[10, 100]], un=[{"tag": "one"}]),
            _c(sig=["A"], clk=[[20, 200]], cu=[True]),
        ],
    ),
    (
        "a changed signature reads again",
        [
            _c(sig=["A", "A"], clk=[[10, 100]], un=[{"tag": "one"}]),
            _c(sig=["B", "B"], clk=[[11, 101]], un=[{"tag": "two"}]),
        ],
    ),
    (
        "no signature: read, never kept",
        [_c(sig=[None], clk=[[1, 1]], un=[{"tag": "x"}])],
    ),
    (
        "the signature moved during the capture: not kept",
        [_c(sig=["A", "B"], clk=[[1, 1]], un=[{"tag": "x"}])],
    ),
    (
        "after reads None: not kept",
        [_c(sig=["A", None], clk=[[1, 1]], un=[{"tag": "x"}])],
    ),
    (
        "indeterminate and evicted captures are not kept",
        [
            _c(sig=["A"], clk=[[1, 1]], un=[{"tag": "i", "s": "indeterminate"}]),
            _c(sig=["A"], clk=[[1, 1]], un=[{"tag": "e", "ev": True}]),
            _c(sig=["A"], clk=[[1, 1]], un=[{"tag": "o", "s": "outside", "ev": True}]),
        ],
    ),
    (
        "use_cache off reads no signature and keeps nothing",
        [
            _c(sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "one"}]),
            _c(uc=False, sig=[], clk=[[2, 2]], un=[{"tag": "two"}]),
            _c(sig=["A"], clk=[[3, 3]], cu=[True]),
        ],
    ),
    (
        "freshness on both clocks, its bounds",
        [
            _c(sig=["A", "A"], clk=[[0, 1000]], un=[{"tag": "one"}]),
            _c(sig=["A"], clk=[[599.999, 1599.999]], cu=[True]),
            _c(sig=["A", "A"], clk=[[600, 1000]], un=[{"tag": "two"}]),
            _c(sig=["A", "A"], clk=[[600, 1600]], un=[{"tag": "three"}]),
            _c(sig=["A", "A"], clk=[[599, 1000]], un=[{"tag": "back"}]),
            _c(sig=["A", "A"], clk=[[700, 999]], un=[{"tag": "wall-back"}]),
            _c(sig=["A"], clk=[[700, 1000]], cu=[True]),
        ],
    ),
    (
        "a config that changed reads again",
        [
            _c(sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "one"}]),
            _c(sig=["A", "A"], clk=[[2, 2]], cu=[False], un=[{"tag": "two"}]),
            _c(sig=["A"], clk=[[3, 3]], cu=[True]),
        ],
    ),
    (
        "a written token: compared masked, kept as present",
        [
            _c(wt=True, sig=["A", "A~5"], clk=[[1, 1]], un=[{"tag": "w", "tw": "TOK"}]),
            _c(wt=True, sig=["A~5"], clk=[[2, 2]], cu=[True]),
            _c(wt=False, sig=["A~5", "A~6"], clk=[[3, 3]], un=[{"tag": "nw"}]),
            _c(
                wt=True,
                sig=["A~1", "B~1"],
                clk=[[4, 4]],
                un=[{"tag": "w2", "tw": "T2"}],
            ),
        ],
    ),
    (
        "keys: write_token and manifest apart",
        [
            _c(sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "plain"}]),
            _c(wt=True, sig=["A", "A"], clk=[[2, 2]], un=[{"tag": "wt"}]),
            _c(mf=False, sig=["A", "A"], clk=[[3, 3]], un=[{"tag": "nomf"}]),
            _c(cwd="/v", sig=["A", "A"], clk=[[4, 4]], un=[{"tag": "v"}]),
            _c(sig=["A"], clk=[[5, 5]], cu=[True]),
            _c(sig=["B", "B"], clk=[[6, 6]], un=[{"tag": "plain2"}]),
        ],
    ),
    (
        "forget one cwd, then all",
        [
            _c(sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "w"}]),
            _c(cwd="/v", sig=["A", "A"], clk=[[2, 2]], un=[{"tag": "v"}]),
            _c(cwd="/v", wt=True, sig=["A", "A"], clk=[[3, 3]], un=[{"tag": "v2"}]),
            {"do": "forget", "cwd": "/v"},
            {"do": "forget", "cwd": "/none"},
            {"do": "forget"},
        ],
    ),
    (
        "the tree recorded first",
        [_c(sig=["A", "A"], clk=[[1, 1]], rec="first", un=[{**_KEPT, "ka": True}])],
    ),
    (
        "recorded elsewhere: forgotten and read again uncached",
        [
            _c(cwd="/w", wt=True, sig=["A", "A"], clk=[[0, 0]], un=[{"tag": "wt"}]),
            _c(
                sig=["A", "A"],
                clk=[[1, 1], [2, 2]],
                rec="elsewhere",
                un=[
                    {**_KEPT, "ka": True},
                    {"tag": "again", "ka": True, "gd": "/g", "root": "/w"},
                ],
            ),
        ],
    ),
    (
        "recorded already, or not recorded",
        [
            _c(sig=["A", "A"], clk=[[1, 1]], rec="same", un=[{**_KEPT, "ka": True}]),
            _c(sig=["A"], clk=[[2, 2]], cu=[True], rec="first"),
            _c(rt=False, sig=["B", "B"], clk=[[3, 3]], un=[{**_KEPT, "ka": True}]),
            _c(sig=["C", "C"], clk=[[4, 4]], un=[{**_KEPT, "ka": True, "gd": ""}]),
            _c(sig=["D", "D"], clk=[[5, 5]], un=[{**_KEPT, "ka": True, "root": None}]),
            _c(sig=["E", "E"], clk=[[6, 6]], un=[{**_KEPT}]),
        ],
    ),
    (
        "budget passed through",
        [
            _c(b=1.5, sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "b"}]),
            {
                "do": "cached",
                "b": 0.0,
                "uc": False,
                "sig": [],
                "clk": [[2, 2]],
                "un": [{"tag": "c"}],
            },
        ],
    ),
    (
        "capture_token",
        [
            {
                "do": "tok",
                "wt": True,
                "sig": ["A", "A"],
                "clk": [[1, 1]],
                "un": [{"tag": "t", "tok": "T", "gd": "/g", "root": "/w"}],
            },
            {
                "do": "tok",
                "sig": [None],
                "clk": [[2, 2]],
                "un": [{"tag": "o", "s": "outside"}],
            },
        ],
    ),
    (
        "eviction past CACHE_CAP, oldest first, a re-put moves to the end",
        [
            {"do": "fill", "n": 256},
            _c(cwd="/f0", sig=["G", "G"], clk=[[1, 1]], un=[{"tag": "f0b"}]),
            {"do": "fill", "n": 2},
            _c(cwd="/new", sig=["A", "A"], clk=[[1, 1]], un=[{"tag": "new"}]),
        ],
    ),
    (
        "_evicted",
        [
            {"do": "ev", "fl": [0, 0]},
            {"do": "ev", "fl": [0x40000000, 0]},
            {"do": "ev", "fl": [0x7FFFFFFF ^ 0x40000000, 0]},
            {"do": "ev", "fl": [0, 0x00040000]},
            {"do": "ev", "fl": [0, 0x00400000]},
            {"do": "ev", "fl": [0, 0xFFFFFFFF ^ 0x00440000]},
        ],
    ),
]


def _scripted_file() -> bytes:
    rows: list[dict[str, Any]] = []
    stubs = _Stubs()
    with _stubbed(stubs):
        for note, steps in _CASES:
            location._clear_cache()
            rows.append({"note": note, "s": [{**s, **_run(stubs, s)} for s in steps]})
        location._clear_cache()
    return jsonl([{"fixture": "location_cache", "cases": len(rows)}, *rows])


# ---------------------------------------------------------------------------
# Over real trees
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdef" * 2 + "01234567"
_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"


def _repo(at: str, head: str = "ref: refs/heads/main\n") -> list[list[Any]]:
    g = f"{at}/.git"
    return [
        ["d", f"{g}/objects"],
        ["d", f"{g}/refs/heads"],
        ["f", f"{g}/HEAD", head],
        ["f", f"{g}/config", _CONFIG],
        ["f", f"{g}/refs/heads/main", _HEX + "\n"],
    ]


_TOML = "bettermemory.toml"
_REPO = _repo("r")

_TREES: list[tuple[str, list[list[Any]], str, bool]] = [
    ("outside, no project file", [["d", "o"]], "o", False),
    ("outside with a project file", [["f", f"o/{_TOML}", "x"]], "o", False),
    ("a cwd that is not there", [], "gone", False),
    ("a discovery variable set", _REPO, "r", True),
    ("symbolic HEAD, loose branch", _REPO, "r", False),
    ("cwd spelled with a trailing slash", _REPO, "r/", False),
    (
        "nested project files below the root",
        [
            *_REPO,
            ["f", f"r/{_TOML}", "x"],
            ["f", f"r/a/b/{_TOML}", "y"],
            ["d", "r/a/b/c"],
        ],
        "r/a/b/c",
        False,
    ),
    ("detached HEAD", _repo("r", _HEX + "\n"), "r", False),
    (
        "a per-worktree ref",
        [*_repo("r", "ref: refs/worktree/x\n"), ["f", "r/.git/refs/worktree/x", _HEX]],
        "r",
        False,
    ),
    (
        "a missing bisect ref: refs stamped",
        _repo("r", "ref: refs/bisect/bad\n"),
        "r",
        False,
    ),
    (
        "unborn with refs/remotes",
        [*_repo("r", "ref: refs/heads/none\n"), ["d", "r/.git/refs/remotes/o"]],
        "r",
        False,
    ),
    (
        "packed branch",
        [
            *_repo("r", "ref: refs/heads/p\n"),
            ["f", "r/.git/packed-refs", f"{_HEX} refs/heads/p\n"],
        ],
        "r",
        False,
    ),
    ("shallow", [*_REPO, ["f", "r/.git/shallow", _HEX + "\n"]], "r", False),
    (
        "a token file",
        [*_REPO, ["f", "r/.git/info/bettermemory", "t\n"]],
        "r",
        False,
    ),
    ("no config", [x for x in _REPO if x[1] != "r/.git/config"], "r", False),
    ("HEAD that git does not read", _repo("r", "garbage\n"), "r", False),
    ("HEAD spaced, CRLF", _repo("r", "ref:  refs/heads/main \r\n"), "r", False),
    (
        "HEAD naming a byte that is not UTF-8",
        _repo("r", "ref: refs/heads/\xff\n"),
        "r",
        False,
    ),
    ("HEAD naming refs//main", _repo("r", "ref: refs//heads//main\n"), "r", False),
    (
        "HEAD unreadable",
        [*_REPO, ["m", "r/.git/HEAD", 0]],
        "r",
        False,
    ),
    (
        "bare, cwd inside the git directory",
        [
            ["d", "b.git/objects"],
            ["d", "b.git/refs/heads"],
            ["f", "b.git/HEAD", "ref: refs/heads/main\n"],
            ["f", "b.git/config", "[core]\n\tbare = true\n"],
        ],
        "b.git/objects",
        False,
    ),
    (
        "a linked worktree",
        [
            *_REPO,
            ["f", "r/.git/worktrees/w/HEAD", "ref: refs/heads/side\n"],
            ["f", "r/.git/worktrees/w/commondir", "../..\n"],
            ["f", "r/.git/worktrees/w/gitdir", "@/w/.git\n"],
            ["f", "r/.git/refs/heads/side", _HEX + "\n"],
            ["f", "w/.git", "gitdir: @/r/.git/worktrees/w\n"],
            ["f", f"w/{_TOML}", "z"],
        ],
        "w",
        False,
    ),
]


def _build(base: str, items: list[list[Any]]) -> None:
    os.makedirs(base, exist_ok=True)
    for item in items:
        path = os.path.join(base, item[1])
        if item[0] == "d":
            os.makedirs(path, exist_ok=True)
        elif item[0] == "f":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(item[2].replace("@", base).encode("latin-1"))
        else:
            os.chmod(path, item[2])


def _open_up(base: str) -> None:
    for d, dirs, files in os.walk(base):
        for name in dirs + files:
            os.chmod(os.path.join(d, name), 0o700)


def _rel(base: str, path: str) -> str:
    if path == base:
        return "@"
    assert path.startswith(base + "/"), path
    return "@" + path[len(base) :]


def _index(base: str) -> dict[int, str]:
    found = {os.stat(base).st_ino: "@"}
    for d, dirs, files in os.walk(base):
        for name in dirs + files:
            p = os.path.join(d, name)
            found[os.stat(p).st_ino] = _rel(base, p)
    return found


def _slot(base: str, inos: dict[int, str], value: Any) -> Any:
    if value is None:
        return None
    if tuple(value) == (-1,) * 5:
        return -1
    rel = inos[value[3]]
    path = base + rel[1:]
    if len(value) == 5:
        assert githead.stamp(path) == value, (rel, value)
    else:
        st = os.stat(path)
        assert (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino) == value
    return {"s": rel}


def _git(base: str, g: Any) -> Any:
    if g is None:
        return None
    r = lambda p: None if p is None else _rel(base, p)  # noqa: E731
    return {
        "dg": r(g.dotgit),
        "gd": r(g.git_dir),
        "cd": r(g.common_dir),
        "root": r(g.root),
        "k": g.kind,
    }


def _shown_sig(base: str, sig: Any) -> Any:
    if sig is None:
        return None
    inos = _index(base)
    s = lambda v: _slot(base, inos, v)  # noqa: E731
    cwd, dev, ino = sig[1]
    st = os.stat(cwd)
    assert (dev, ino) == (st.st_dev, st.st_ino)
    if sig[0] == location.OUTSIDE:
        return {
            "o": _rel(base, cwd.rstrip("/")) + ("/" if cwd.endswith("/") else ""),
            "p": s(sig[2]),
        }
    (
        _,
        _,
        dotgit,
        dg_stamp,
        git_dir,
        common,
        head,
        hst,
        ref_part,
        packed,
        cst,
        shallow,
        tok,
        inner,
        refs,
    ) = sig
    return {
        "r": {
            "cwd": _rel(base, cwd.rstrip("/")) + ("/" if cwd.endswith("/") else ""),
            "dg": None if dotgit is None else _rel(base, dotgit),
            "dgs": s(dg_stamp),
            "gd": _rel(base, git_dir),
            "cd": _rel(base, common),
            "h": head.hex(),
            "hs": s(hst),
            "ref": s(ref_part),
            "pk": s(packed),
            "cf": s(cst),
            "sh": s(shallow),
            "tk": s(tok[1]),
            "in": [s(v) for v in inner],
            "rf": None if refs is None else [s(refs[0]), s(refs[1])],
        }
    }


def _posix_file() -> bytes:
    refuse_root()
    rows: list[dict[str, Any]] = []
    real_discover = getattr(location, "_discover")  # noqa: B009
    env = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    )
    with _sandbox.world() as world:
        top = os.path.realpath(os.fspath(world.root)) + "/cache"
        saved = {v: os.environ.pop(v, None) for v in (*env, "GIT_CEILING_DIRECTORIES")}
        try:
            for n, (note, items, cwd, moved) in enumerate(_TREES):
                base = f"{top}/{n}"
                _build(base, items)
                calls: list[list[Any]] = []

                def discover(
                    start: str, refused: Any = None, *, read_head: bool = True
                ) -> Any:
                    try:
                        found = real_discover(start, refused, read_head=read_head)
                    except OSError:
                        calls.append([read_head, "os"])  # noqa: B023
                        raise
                    calls.append([read_head, _git(base, found)])  # noqa: B023
                    return found

                os.environ["GIT_CEILING_DIRECTORIES"] = base
                if moved:
                    os.environ["GIT_DIR"] = "x"
                setattr(location, "_discover", discover)  # noqa: B010
                try:
                    sig = location.signature(f"{base}/{cwd}")
                finally:
                    setattr(location, "_discover", real_discover)  # noqa: B010
                    os.environ.pop("GIT_DIR", None)
                row: dict[str, Any] = {"note": note, "t": items, "cwd": cwd}
                if moved:
                    row["mv"] = 1
                rows.append({**row, "d": calls, "a": _shown_sig(base, sig)})
                _open_up(base)
        finally:
            for v, value in saved.items():
                os.environ.pop(v, None)
                if value is not None:
                    os.environ[v] = value
    return jsonl([{"fixture": "location_cache_posix", "cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"location_cache.jsonl": _scripted_file()}


def generate_posix() -> dict[str, bytes]:
    return {"location_cache_posix.jsonl": _posix_file()}
