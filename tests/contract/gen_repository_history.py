"""Fixture: `location_git.RepositoryHistory` and its git fallback `_git`
(``repository_history.jsonl``, ungated).

The native build reads it in
``native/crates/bettermemory-core/src/repository_history.rs``.

No git runs and no repository is read: `gitobjects.ObjectReader` is
replaced by a scripted reader and `_git.run_git` and
`_git.log_nonzero_exit` by a script that records each call. The common
directory is the text ``C``.

A case is:

- ``r``: the reader: ``c`` its `complete` (default true), ``n`` its
  `name_length` (default 40, may be null), ``o`` its objects
  (``{sha: [type, body as hex]}``: `read`, `has_many`, and `resolve`,
  which answers the one key starting with the lowered prefix of at least
  four characters), ``ct`` a commit's tree (`commit`), ``tr`` the trees
  (``{sha: [[mode, name, sha], ...]}``), ``sz`` the headers' sizes
  (`header`, type ``blob``), ``x`` the calls that raise ``ValueError``
  (``"method:arg"``, ``*`` for any argument) and ``mk`` how many times
  making the reader raises ``OSError`` first;
- ``g``: git's answers, one per run in order: null (git could not run)
  or ``{"rc": code, "o": stdout text, "ox": stdout hex, "e": stderr}``;
- ``s``: the steps, each ``[method, argument]``.

Each step answers ``a`` (the answer, or ``{"raised": class}``) and ``q``
(the git calls it made: ``["run", args, stdin, timeout, env, text]``
with ``<devnull>`` for `os.devnull`, ``["log", args, code, where]``).
Sets are sorted lists, `expand` a list of pairs in dict order, `_expand`
of triples, `reachable_among` ``[relations, bounded, handled]``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory import _git, gitobjects, location_git

from ._common import jsonl

_C = "C"
_DEVNULL = "<devnull>"


def _h(name: str, n: int = 40) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:n]


def _body(tree: str, parents: list[str], t: int | str = 1000, msg: str = "m") -> str:
    lines = [f"tree {tree}", *(f"parent {p}" for p in parents)]
    lines += [f"author A <a> {t} +0000", f"committer C <c> {t} +0000", "", msg, ""]
    return "\n".join(lines).encode().hex()


class _Reader:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec

    def _raise(self, method: str, arg: str) -> None:
        x = self.spec.get("x", [])
        if f"{method}:{arg}" in x or f"{method}:*" in x:
            raise ValueError(method)

    @property
    def complete(self) -> bool:
        self._raise("complete", "")
        return bool(self.spec.get("c", True))

    @property
    def name_length(self) -> int | None:
        return self.spec.get("n", 40)

    def has_many(self, shas: Any) -> set[str]:
        shas = list(shas)
        self._raise("has_many", "")
        return {s for s in shas if s in self.spec.get("o", {})}

    def resolve(self, prefix: str) -> str | None:
        self._raise("resolve", prefix)
        if len(prefix) < 4:
            return None
        hits = [k for k in self.spec.get("o", {}) if k.startswith(prefix.lower())]
        return hits[0] if len(hits) == 1 else None

    def read(self, sha: str) -> tuple[str, bytes] | None:
        self._raise("read", sha)
        got = self.spec.get("o", {}).get(sha)
        return None if got is None else (got[0], bytes.fromhex(got[1]))

    def commit(self, sha: str) -> gitobjects.Commit | None:
        self._raise("commit", sha)
        tree = self.spec.get("ct", {}).get(sha)
        return None if tree is None else gitobjects.Commit(tree, (), b"", b"", b"")

    def tree(self, sha: str) -> list[gitobjects.TreeEntry] | None:
        self._raise("tree", sha)
        got = self.spec.get("tr", {}).get(sha)
        if got is None:
            return None
        return [gitobjects.TreeEntry(m, n.encode(), s) for m, n, s in got]

    def header(self, sha: str) -> tuple[str, int] | None:
        self._raise("header", sha)
        size = self.spec.get("sz", {}).get(sha)
        return None if size is None else ("blob", size)


def _spell_out(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("latin-1")
    return str(data)


def _run_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    spec = case.get("r", {})
    answers = list(case.get("g", []))
    events: list[Any] = []
    fails = [spec.get("mk", 0)]

    def make(common: str) -> _Reader:
        assert common == _C
        if fails[0]:
            fails[0] -= 1
            raise OSError("cannot open")
        return _Reader(spec)

    def run_git(args: Any, **kw: Any) -> Any:
        env = kw.get("env")
        if env is not None:
            env = {k: (_DEVNULL if v == os.devnull else v) for k, v in env.items()}
        stdin = kw.get("stdin")
        events.append(
            [
                "run",
                list(args),
                None if stdin is None else stdin.decode(),
                kw.get("timeout"),
                env,
                kw.get("text", True),
                kw.get("git_dir"),
            ]
        )
        assert answers, "the case runs git more often than it scripts"
        got = answers.pop(0)
        if got is None:
            return None
        out = bytes.fromhex(got["ox"]) if "ox" in got else got.get("o", "").encode()
        err = got.get("e", "").encode()
        if kw.get("text", True):
            return mock.Mock(
                returncode=got.get("rc", 0),
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace"),
            )
        return mock.Mock(returncode=got.get("rc", 0), stdout=out, stderr=err)

    def log_nonzero_exit(args: Any, done: Any, where: Any) -> None:
        events.append(["log", list(args), done.returncode, where])

    out: list[dict[str, Any]] = []
    with (
        mock.patch.object(gitobjects, "ObjectReader", make),
        mock.patch.object(_git, "run_git", run_git),
        mock.patch.object(_git, "log_nonzero_exit", log_nonzero_exit),
    ):
        history = location_git.RepositoryHistory(_C)
        for method, arg in case["s"]:
            events.clear()
            try:
                answer = _answer(history, method, arg)
            except (ValueError, OSError) as exc:
                answer = {"raised": type(exc).__name__}
            out.append({"a": answer, "q": [list(e) for e in events]})
    assert not answers, "the case scripts git answers it never reads"
    return out


def _answer(history: Any, method: str, arg: Any) -> Any:
    if method == "expand":
        return [[g, f] for g, f in history.expand(arg).items()]
    if method == "_expand":
        return [[g, f, k] for g, (f, k) in history._expand(arg).items()]
    if method in ("holds", "holds_objects"):
        return sorted(getattr(history, method)(arg))
    if method in ("fingerprint", "parents", "committed"):
        got = getattr(history, method)(arg)
        return list(got) if method == "parents" else got
    if method == "reach":
        reach = history.reachable_among(arg)
        return [
            [[k, sorted(v)] for k, v in reach.items()],
            reach.bounded,
            reach.handled,
        ]
    if method == "independent":
        got = history.independent(arg)
        return None if got is None else sorted(got)
    if method == "blobs":
        return list(history.blobs(arg))
    raise AssertionError(method)


def _cases() -> Iterator[tuple[str, dict[str, Any]]]:
    a, b, c, d, e = (_h(x) for x in "abcde")
    t = _h("tree")
    objs = {a: ["commit", _body(t, [])], b: ["blob", "00"], c: ["tree", ""]}
    ab = a[:6]
    # Two ids sharing a 5-digit prefix: `_h` of these names.
    twins: dict[str, list[str]] = {}
    for i in range(5000):
        k = _h(f"t{i}")
        twins.setdefault(k[:4], []).append(k)
    pair = next(v for v in twins.values() if len(v) >= 2)[:2]
    amb = {pair[0]: ["blob", "00"], pair[1]: ["blob", "00"]}
    p4 = pair[0][:4]

    # expand, complete reader
    yield (
        "complete reader",
        {
            "r": {"o": {**objs, **amb}},
            "s": [
                [
                    "expand",
                    [a, a.upper(), d, ab, ab.upper(), p4, a[:3], "zz" + a[2:], a + "0"],
                ],
                ["_expand", [a, b, c, ab]],
                ["holds", [a, b, c, ab.upper(), d]],
                ["holds_objects", [a, b, c, ab, d]],
                ["expand", []],
            ],
        },
    )
    yield (
        "name length none",
        {
            "r": {"o": objs, "n": None},
            "s": [["expand", [a, ab]], ["holds", [a, d]]],
        },
    )
    yield (
        "name length 64",
        {
            "r": {"o": {_h("w", 64): ["commit", _body(t, [])], **objs}, "n": 64},
            "s": [["expand", [a, _h("w", 64), _h("w", 64).upper(), _h("w", 10)]]],
        },
    )
    # incomplete reader, git declines
    yield (
        "incomplete git none",
        {
            "r": {"o": objs, "c": False},
            "g": [None],
            "s": [["_expand", [a, d, ab, "xyz1", e[:8]]]],
        },
    )
    yield (
        "incomplete git nonzero",
        {
            "r": {"o": objs, "c": False},
            "g": [{"rc": 128, "e": "fatal: bad\nmore\n"}, {"rc": -9}],
            "s": [["_expand", [d, ab]], ["holds", [d]]],
        },
    )
    yield (
        "incomplete nothing askable",
        {
            "r": {"o": objs, "c": False},
            "s": [
                ["_expand", [a, "xyz", "g" * 40, "abc", "a" * 65, a]],
                ["holds", [a]],
            ],
        },
    )
    # incomplete reader, git answers in pairs
    yield (
        "git paired",
        {
            "r": {"o": objs, "c": False},
            "g": [
                {"o": f"{d} commit 10\n{e.upper()} tree 5\n{a} commit 3\nzz missing\n"},
                {"o": f"{ab} ambiguous\n{e} blob 7\n"},
                {"o": f"{d}\n{e} missing\n"},
            ],
            "s": [
                ["_expand", [d, e[:7], ab, "abcd"]],
                ["_expand", [ab, e]],
                ["_expand", [d, e]],
            ],
        },
    )
    yield (
        "git paired duplicates",
        {
            "r": {"o": objs, "c": False},
            "g": [{"o": f"{d} commit 1\n{e} tree 2\n"}],
            "s": [["_expand", [d, d, d.upper(), e[:5], e[:5]]]],
        },
    )
    yield (
        "git types",
        {
            "r": {"o": {}, "c": False},
            "g": [
                {"o": f"{a} commit 1\n{b} blob 2\n{c} tree 3\n{d} tag 4\n"},
                {"o": f"{a} commit 1\n{b} blob 2\n{c} tree 3\n{d} tag 4\n"},
            ],
            "s": [["holds", [a, b, c, d]], ["holds_objects", [a, b, c, d]]],
        },
    )
    # mispaired lines
    yield (
        "git mispaired fewer",
        {
            "r": {"o": {}, "c": False},
            "g": [
                {"o": f"{d} commit 1\n"},
                {"o": f"{d.upper()} commit 1\n{e} blob 1 x\n"},
            ],
            "s": [
                ["_expand", [a, d.upper(), e[:6]]],
                ["_expand", [a, d, e, b]],
            ],
        },
    )
    yield (
        "git mispaired more",
        {
            "r": {"o": {}, "c": False},
            "g": [
                {"o": f"{d} blob 1\n{d} commit 2\n{e} tree 3\nnoise\n\n{a}\n"},
                {"o": f"{d} commit 2\n{d} missing\n{d} ambiguous\n"},
            ],
            "s": [["_expand", [d, e, a]], ["_expand", [d, e]]],
        },
    )
    yield (
        "git odd separators",
        {
            "r": {"o": {}, "c": False},
            "g": [
                {"o": f"{a}\tcommit\r\n{b} blob\r{c}\x1ftree\x0b{d}\x1c commit\x0c"},
                {"o": f"{a} commit\x1d{b} blob\x1e{c} tree\u0085{d} blob {e} tag "},
                {"ox": (f"{a} \xff\xfe\n{b} blob\xc3\n").encode("latin-1").hex()},
                {"o": f"{a} commit　x\n{b}​blob\n"},
                {"o": ""},
                {"o": "\n\n"},
            ],
            "s": [
                ["_expand", [a, b, c, d, e]],
                ["_expand", [a, b, c, d, e]],
                ["_expand", [a, b]],
                ["_expand", [a, b]],
                ["_expand", [a]],
                ["_expand", [a, b]],
            ],
        },
    )
    yield (
        "git answers abbreviation reader also found",
        {
            "r": {"o": {**objs, **amb}, "c": False},
            "g": [{"o": f"{ab} missing\n{d} commit 9\n{p4} ambiguous\n"}, None],
            "s": [["_expand", [ab, d[:9], p4]], ["_expand", [ab, p4]]],
        },
    )
    # fingerprint, parents, committed
    root = _h("root")
    mid = _h("mid")
    top = _h("top")
    hist = {
        root: ["commit", _body(t, [], 5)],
        mid: ["commit", _body(t, [root], 18446744073709551615 * 2)],
        top: ["commit", _body(t, [mid, a], 7)],
        a: ["blob", _body(t, [root], 9)],
        b: ["commit", "6e6f7468696e67"],
    }
    yield (
        "parents complete",
        {
            "r": {"o": hist},
            "s": [
                ["parents", top],
                ["parents", top.upper()],
                ["committed", mid],
                ["committed", top.upper()],
                ["committed", root],
                ["parents", a],
                ["committed", a],
                ["parents", b],
                ["committed", b],
                ["parents", d],
                ["committed", e],
                ["fingerprint", top],
                ["fingerprint", top.upper()],
                ["fingerprint", a],
                ["fingerprint", b],
                ["fingerprint", d],
            ],
        },
    )
    w64 = _h("w64", 64)
    p64 = _h("p64", 64)
    yield (
        "parents sha256",
        {
            "r": {
                "o": {
                    w64: ["commit", _body(_h("t", 64), [p64], 3)],
                    p64: ["commit", _body(_h("t", 64), [p64, a], 3)],
                },
                "n": 64,
            },
            "s": [
                ["parents", w64],
                ["committed", w64],
                ["parents", p64],
                ["committed", p64],
                ["parents", w64[:40]],
            ],
        },
    )
    yield (
        "parents through git",
        {
            "r": {"o": {}, "c": False},
            "g": [
                {"ox": bytes.fromhex(hist[top][1]).hex()},
                {"rc": 1, "e": "fatal: not a commit"},
                None,
                {"ox": bytes.fromhex(hist[mid][1]).hex()},
                {"o": "garbage"},
            ],
            "s": [
                ["parents", top],
                ["parents", top],
                ["committed", d],
                ["parents", e],
                ["committed", mid],
                ["fingerprint", root],
                ["committed", e],
            ],
        },
    )
    yield (
        "reader raises",
        {
            "r": {
                "o": hist,
                "x": [f"read:{mid}", "has_many:", f"resolve:{ab}"],
                "mk": 1,
            },
            "g": [None],
            "s": [
                ["parents", top],
                ["parents", top],
                ["parents", mid],
                ["committed", mid],
                ["fingerprint", mid],
                ["expand", [a]],
                ["expand", [ab]],
                ["expand", [d[:5]]],
                ["independent", [a, b]],
                ["reach", [top, root]],
            ],
        },
    )
    yield (
        "complete raises",
        {
            "r": {"o": hist, "x": ["complete:"]},
            "s": [["expand", [a]], ["parents", d], ["parents", top]],
        },
    )
    yield (
        "make raises twice",
        {
            "r": {"o": hist, "mk": 2},
            "s": [["blobs", top], ["expand", [a]], ["parents", top]],
        },
    )
    # reachable_among
    yield (
        "reach complete",
        {
            "r": {"o": hist},
            "s": [
                ["reach", [top, root, mid, d, "nothex"]],
                ["reach", [top.upper(), root]],
                ["reach", []],
                ["reach", [a, root]],
            ],
        },
    )
    yield (
        "reach through git",
        {
            "r": {"o": {top: hist[top]}, "c": False},
            "g": [{"ox": bytes.fromhex(hist[mid][1]).hex()}, None, None],
            "s": [["reach", [top, root]]],
        },
    )
    yield (
        "reach raises midway",
        {
            "r": {"o": hist, "x": [f"read:{root}"]},
            "s": [["reach", [top, root, mid]], ["reach", [top, mid]], ["parents", mid]],
        },
    )
    # independent
    many = sorted(_h(f"i{i}") for i in range(257))
    w = [_h(f"w{i}") for i in range(4)]
    ws = sorted(w)
    yield (
        "independent bounds",
        {
            "s": [
                ["independent", []],
                ["independent", [a]],
                ["independent", [a, a.upper(), a[:39], "g" * 40, a + "0"]],
                ["independent", many],
                ["independent", [_h("x", 64)]],
            ],
        },
    )
    yield (
        "independent at the bound",
        {
            "g": [{"o": "\n".join(many[:256][:3]) + "\n"}],
            "s": [["independent", many[:256] + [m.upper() for m in many[:256]]]],
        },
    )
    yield (
        "independent answers",
        {
            "g": [
                {"o": f"{ws[0]}\n{ws[2]}\n"},
                None,
                {"rc": 128, "e": "fatal: bad object\n"},
                {"o": f"{ws[0]}\n{a}\n"},
                {"o": ""},
                {"o": "\n  \n\r\n"},
                {"o": f"  {ws[1].upper()}\t\r\n\n{ws[0]} \x1c {ws[3]}\u0085"},
                {"ox": (ws[0] + "\xff\n").encode("latin-1").hex()},
                {"o": f"{ws[0]}\n{ws[0]}\n"},
                {"o": f"{ws[0]} {ws[1]}\n"},
                {"rc": 1},
            ],
            "s": [["independent", [w[3], w[0], w[2].upper(), w[1]]]] * 10
            + [["independent", [w[0], w[1], d]]],
        },
    )
    yield (
        "independent sha256",
        {
            "g": [{"o": _h("s1", 64) + "\n"}],
            "s": [["independent", [_h("s1", 64), _h("s2", 64), a]]],
        },
    )
    # blobs
    files = [[0o100644, f"f{i:02d}", _h(f"f{i}")] for i in range(12)]
    sizes = {s: 64 + i for i, (_, _, s) in enumerate(files)}
    sub = _h("sub")
    subsub = _h("subsub")
    big = [[0o100644, f"g{i:02d}", _h(f"g{i}")] for i in range(70)]
    yield (
        "blobs",
        {
            "r": {
                "ct": {top: t, mid: sub, root: _h("nil"), a: _h("big")},
                "tr": {
                    t: [
                        [0o040000, "d", sub],
                        [0o040000, "gone", _h("gone")],
                        [0o100644, "bettermemory.toml", _h("pf")],
                        [0o120000, "link", _h("l")],
                        [0o160000, "mod", _h("m")],
                        [0o100755, "x", _h("x")],
                        [0o100644, "small", _h("small")],
                        [0o100644, "edge", _h("edge")],
                        [0o100644, "nohead", _h("nohead")],
                    ],
                    sub: [
                        [0o040000, "deeper", subsub],
                        [0o100644, "Bettermemory.toml", _h("pf2")],
                        *files,
                    ],
                    subsub: [[0o100644, "deep", _h("deep")]],
                    _h("big"): big,
                },
                "sz": {
                    _h("pf"): 100,
                    _h("l"): 100,
                    _h("m"): 100,
                    _h("x"): 64,
                    _h("small"): 63,
                    _h("edge"): 2**70,
                    _h("deep"): 100,
                    _h("pf2"): 100,
                    **sizes,
                    **{s: 100 for _, _, s in big},
                },
            },
            "s": [
                ["blobs", top],
                ["blobs", top.upper()],
                ["blobs", mid],
                ["blobs", root],
                ["blobs", d],
                ["blobs", a],
            ],
        },
    )
    yield (
        "blobs look bound across trees",
        {
            "r": {
                "ct": {top: t},
                "tr": {
                    t: [
                        [0o040000, "s", sub],
                        *big[:60],
                        [0o100644, "late", _h("late")],
                    ],
                    sub: files,
                },
                "sz": {**{s: 100 for _, _, s in big}, **sizes, _h("late"): 1},
            },
            "s": [["blobs", top]],
        },
    )
    yield (
        "blobs raise",
        {
            "r": {
                "ct": {top: t},
                "tr": {t: [[0o100644, "a", _h("ha")], [0o100644, "b", _h("hb")]]},
                "sz": {_h("ha"): 100, _h("hb"): 100},
                "x": [f"header:{_h('hb')}", f"commit:{mid}", f"tree:{sub}"],
            },
            "s": [["blobs", top], ["blobs", mid]],
        },
    )


def _file() -> bytes:
    rows: list[Any] = []
    for name, case in _cases():
        rows.append({"name": name, **case, "out": _run_case(case)})
    return jsonl([{"cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"repository_history.jsonl": _file()}
