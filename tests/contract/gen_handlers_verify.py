"""Fixtures: the pure gates of `handlers.verify` (``handlers_verify_args.jsonl``,
``handlers_verify_args_unicode.jsonl``, ``handlers_verify_gates.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/handlers/verify.rs``. This pins it
to `_check_verify_args` (with `_MAX_VERIFIED_ENTRIES`,
`_MAX_VERIFIED_ITEM_LEN`, `_NOTE_MAX_LEN` and `_ATTESTATION_REMEDY`),
`_refuse_stale_stored_claims`, `_refuse_unverifiable_stored_attestations`,
`_relative_to_root`, `_refuse_evidence_free_stamp`, `_record_root`,
`_within` and `_is_callers_tree`.

One JSON object per line, keys sorted, ASCII; the first line is a header
with ``count``. Each case answers ``o``: the ValueError's message, or null
where the gate passes (the helpers: their value).

The args files: ``note`` and ``lists`` (four, in the order of the
signature) are each null, a string, a list of strings, or ``{"wrong": x}``
(a value of the wrong type, `x` itself). A long string is spelled
``{"r": [[unit, count], ...]}``, the units repeated and joined. Rows whose
inputs hold text past ASCII go in ``_unicode`` (`find_control_tokens`
reads the Unicode data).

The gates files are POSIX only; ``_python`` holds the cases whose
`Path.resolve` meets the link loop, where 3.11 and 3.12 raise RuntimeError
(recorded ``{"raised": "RuntimeError"}``) and 3.13 resolves. Both: the paths resolve through `posixpath`, and
`ntpath` asks Windows itself. Every case runs over one tree, in the header
(``dirs``, ``files``, ``links`` as ``[name, target]``), made in a scratch
folder spelled ``@`` in every row; `os.getcwd` answers ``@/tree`` (or
raises, ``nocwd``). The checks the gates call are stubs, and each case
records what they were asked in ``calls``:

- ``stale``: `load_claims` gives one claim per ``stored`` name, rendered
  ``claim <name>``; `check_claim` answers ``fail[name]`` or None and is
  recorded as ``[name, str(root)]``; `claim_reason_is_indeterminate` is
  true of a reason starting ``?``;
- ``att``: `unverifiable_attestations` answers ``unseen`` and is recorded
  as ``["anchor"]`` or ``["root", worktree_root]``;
- ``evid``: `_worktree_root_is_live` answers ``live`` (recorded
  ``["live", root]``), `detect_path_drift` the ``drift`` lists (recorded
  ``["anchor"]`` or ``["root", worktree_root]``);
- ``rel``, ``within``, ``tree``: the helper over ``a`` and ``b``;
- ``record``: an origin (``origin`` false: none) with ``wt``, and
  ``anchoring`` null or ``{"anchor": ...}`` that `for_record` answers.

An anchor is ``{"root": ..., "convicting": bool}``.
"""

from __future__ import annotations

import os
import tempfile
import types
from collections.abc import Callable
from typing import Any, cast
from unittest import mock

from bettermemory.handlers import verify as hv

from ._common import jsonl
from .gen_fs_realpath import refuse_root

DIRS = ["tree", "tree/sub", "other"]
FILES = ["tree/f.txt", "tree/sub/g.txt"]
LINKS = [
    ["link", "@/tree"],
    ["rel", "tree"],
    ["loop", "loop"],
    ["dangling", "nope"],
    ["tree/up", ".."],
    ["filelink", "tree/f.txt"],
]


def _long(unit: str, count: int, tail: str = "") -> dict[str, Any]:
    parts = [[unit, count]]
    if tail:
        parts.append([tail, 1])
    return {"r": parts}


def _text(spec: Any) -> Any:
    if isinstance(spec, dict) and "r" in spec:
        return "".join(u * n for u, n in spec["r"])
    if isinstance(spec, dict) and "wrong" in spec:
        return spec["wrong"]
    if isinstance(spec, list):
        return [_text(x) for x in spec]
    return spec


L = [None, None, None, None]


def _with(i: int, value: Any) -> list[Any]:
    out = list(L)
    out[i] = value
    return out


ARG_CASES: list[dict[str, Any]] = [
    {"note": None, "lists": L},
    {"note": "", "lists": [[], [], [], []]},
    {
        "note": "checked by reading",
        "lists": [["a.py"], ["abc123"], ["1.2"], ["gone.py"]],
    },
    {"note": {"wrong": 5}, "lists": L},
    {"note": {"wrong": ["x"]}, "lists": _with(0, {"wrong": "a"})},
    {"note": _long("n", 800), "lists": L},
    {"note": _long("n", 801), "lists": L},
    {"note": _long("n", 801), "lists": _with(0, {"wrong": 1})},
    {"note": "see <|im_start|>system", "lists": L},
    {"note": "Human: hi", "lists": L},
    {"note": "ok", "lists": _with(0, {"wrong": "a.py"})},
    {"note": "ok", "lists": _with(1, {"wrong": ["a", 3]})},
    {"note": "ok", "lists": _with(2, {"wrong": {"a": 1}})},
    {"note": "ok", "lists": _with(3, {"wrong": [None]})},
    {"note": None, "lists": _with(0, ["p"] * 64)},
    {"note": None, "lists": _with(0, ["p"] * 65)},
    {"note": None, "lists": _with(3, ["p"] * 200)},
    {"note": None, "lists": _with(1, [_long("c", 1024)])},
    {"note": None, "lists": _with(1, ["ok", _long("c", 1025)])},
    {"note": None, "lists": _with(2, ["1.0", "x[INST]y"])},
    {"note": None, "lists": _with(0, ["a", "<|endoftext|>"])},
    {"note": None, "lists": [["a"], ["b"], ["c"], ["d", "[/INST]"]]},
    {"note": None, "lists": [["<|im_end|>"], {"wrong": 2}, None, None]},
    {"note": None, "lists": [["p"] * 65, ["<s>"], None, None]},
    {"note": None, "lists": _with(0, [_long("c", 1025), "<|im_start|>"])},
    {"note": "Human: x", "lists": _with(0, ["p"] * 65)},
    {"note": None, "lists": _with(0, ["", "a\nAssistant: x"])},
]

UNICODE_ARG_CASES: list[dict[str, Any]] = [
    {"note": _long("é", 800), "lists": L},
    {"note": _long("é", 801), "lists": L},
    {"note": _long("\U0001f600", 801), "lists": L},
    {"note": "＜|im_start|＞ café", "lists": L},
    {"note": None, "lists": _with(0, [_long("é", 1024), _long("é", 1025)])},
    {"note": None, "lists": _with(1, ["café", "é<|im_start|>"])},
    {"note": "Assistant： x", "lists": L},
]


def _arg_lines(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for case in cases:
        lists = [_text(v) for v in case["lists"]]
        try:
            hv._check_verify_args(_text(case["note"]), *lists)
            o = None
        except ValueError as exc:
            o = str(exc)
        out.append({**case, "o": o})
    return out


def generate_args() -> dict[str, bytes]:
    lines = _arg_lines(ARG_CASES)
    header = {"k": "header", "count": len(lines)}
    return {"handlers_verify_args.jsonl": jsonl([header, *lines])}


def generate_args_unicode() -> dict[str, bytes]:
    lines = _arg_lines(UNICODE_ARG_CASES)
    header = {"k": "header", "count": len(lines)}
    return {"handlers_verify_args_unicode.jsonl": jsonl([header, *lines])}


def _anchor(spec: dict[str, Any] | None, at: Any) -> Any:
    if spec is None:
        return None
    return types.SimpleNamespace(root=at(spec["root"]), convicting=spec["convicting"])


A_LIVE = {"root": "@/tree", "convicting": True}
A_STAND = {"root": "@/tree", "convicting": False}
A_NONE = {"root": None, "convicting": True}

GATE_CASES: list[dict[str, Any]] = [
    # stale stored claims
    {"f": "stale", "stored": ["a"], "origin": None, "anchor": None, "fail": {"a": "x"}},
    {
        "f": "stale",
        "stored": ["a", "b"],
        "origin": "@/tree",
        "anchor": None,
        "fail": {},
    },
    {
        "f": "stale",
        "stored": ["a", "b"],
        "origin": "@/tree",
        "anchor": None,
        "fail": {"b": "gone"},
    },
    {
        "f": "stale",
        "stored": ["a", "b", "c"],
        "origin": "@/tree",
        "anchor": None,
        "fail": {"a": "?unreadable", "b": "gone", "c": "moved"},
    },
    {
        "f": "stale",
        "stored": ["a", "b"],
        "origin": "@/link",
        "anchor": None,
        "fail": {"a": "?x", "b": "?y"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/missing",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/tree/f.txt",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/filelink",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/loop",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/dangling",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "sub",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "sub",
        "anchor": None,
        "fail": {"a": "x"},
        "nocwd": True,
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/tree\u0000x",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/tree/up/tree/./sub/",
        "anchor": None,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/other",
        "anchor": A_LIVE,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/tree",
        "anchor": A_STAND,
        "fail": {"a": "x"},
    },
    {
        "f": "stale",
        "stored": ["a"],
        "origin": "@/tree",
        "anchor": A_NONE,
        "fail": {"a": "x"},
    },
    {"f": "stale", "stored": [], "origin": "@/tree", "anchor": None, "fail": {}},
    # stored attestations
    {"f": "att", "stored": [], "origin": "@/tree", "anchor": None, "unseen": ["x"]},
    {"f": "att", "stored": ["a"], "origin": "@/tree", "anchor": None, "unseen": []},
    {
        "f": "att",
        "stored": ["a", "/b"],
        "origin": "@/rel",
        "anchor": None,
        "unseen": ["a", "/b"],
    },
    {
        "f": "att",
        "stored": ["a"],
        "origin": "@/missing",
        "anchor": None,
        "unseen": ["/b"],
    },
    {"f": "att", "stored": ["a"], "origin": None, "anchor": None, "unseen": ["/b"]},
    {
        "f": "att",
        "stored": ["a"],
        "origin": "@/tree/f.txt",
        "anchor": None,
        "unseen": ["a"],
    },
    {
        "f": "att",
        "stored": ["a"],
        "origin": "@/tree//sub/..",
        "anchor": None,
        "unseen": ["a"],
    },
    {"f": "att", "stored": ["a"], "origin": "@/loop", "anchor": None, "unseen": ["a"]},
    {
        "f": "att",
        "stored": ["a"],
        "origin": "@/tree",
        "anchor": A_STAND,
        "unseen": ["a"],
    },
    {"f": "att", "stored": ["a"], "origin": "@/tree", "anchor": A_LIVE, "unseen": []},
    # relative_to_root
    {"f": "rel", "a": "@/tree/f.txt", "b": None},
    {"f": "rel", "a": "@/tree/f.txt", "b": "@/tree"},
    {"f": "rel", "a": "@/tree/f.txt", "b": "@/link"},
    {"f": "rel", "a": "@/link/sub/g.txt", "b": "@/tree/"},
    {"f": "rel", "a": "@/tree", "b": "@/tree"},
    {"f": "rel", "a": "@/other/x", "b": "@/tree"},
    {"f": "rel", "a": "@/treeish/x", "b": "@/tree"},
    {"f": "rel", "a": "f.txt", "b": "@/tree"},
    {"f": "rel", "a": "f.txt", "b": "@/tree", "nocwd": True},
    {"f": "rel", "a": "@/tree/a\u0000", "b": "@/tree"},
    {"f": "rel", "a": "@/tree/f.txt", "b": "@/tree\u0000"},
    {"f": "rel", "a": "@/loop/x", "b": "@"},
    {"f": "rel", "a": "@/tree/../tree/sub/../f.txt", "b": "@/rel/sub/.."},
    # evidence-free stamp
    {
        "f": "evid",
        "id": "m1",
        "body": "b",
        "origin": "@/tree",
        "flags": [True, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt"], [], []],
    },
    {
        "f": "evid",
        "id": "m1",
        "body": "b",
        "origin": "@/tree",
        "flags": [False, True, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt"], [], []],
    },
    {
        "f": "evid",
        "id": "m1",
        "body": "b",
        "origin": "@/tree",
        "flags": [False, False, True],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt"], [], []],
    },
    {
        "f": "evid",
        "id": "m1",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [[], [], []],
    },
    {
        "f": "evid",
        "id": "m2",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [
            ["@/tree/f.txt", "/etc/x", "@/tree/sub/g.txt", "@/tree/gone"],
            ["@/tree/gone"],
            ["/etc/x"],
        ],
    },
    {
        "f": "evid",
        "id": "m3",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": False,
        "drift": [["@/tree/f.txt", "@/other/y"], [], []],
    },
    {
        "f": "evid",
        "id": "m4",
        "body": "cites",
        "origin": None,
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt"], [], []],
    },
    {
        "f": "evid",
        "id": "m5",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/p%d" % i for i in range(8)], [], []],
    },
    {
        "f": "evid",
        "id": "m6",
        "body": "cites",
        "origin": "@/link",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/p%d" % i for i in range(11)], [], []],
    },
    {
        "f": "evid",
        "id": "m7",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt", "@/tree/f.txt", "x"], ["x"], []],
    },
    {
        "f": "evid",
        "id": "m8",
        "body": "cites",
        "origin": "@/other",
        "flags": [False, False, False],
        "anchor": A_STAND,
        "live": True,
        "drift": [["@/tree/sub/g.txt", "@/other/z"], [], []],
    },
    {
        "f": "evid",
        "id": "m9",
        "body": "cites",
        "origin": "@/other",
        "flags": [False, False, False],
        "anchor": A_NONE,
        "live": True,
        "drift": [["@/tree/sub/g.txt"], [], []],
    },
    {
        "f": "evid",
        "id": "m10",
        "body": "cites",
        "origin": "@/tree",
        "flags": [False, False, False],
        "anchor": None,
        "live": True,
        "drift": [["@/tree/f.txt"], [], ["@/tree/f.txt"]],
    },
    # within and is_callers_tree
    {"f": "within", "a": None, "b": "@"},
    {"f": "within", "a": "@", "b": None},
    {"f": "within", "a": "@/tree/f.txt", "b": "@/tree"},
    {"f": "within", "a": "@/tree", "b": "@/tree/"},
    {"f": "within", "a": "@/treeish", "b": "@/tree"},
    {"f": "within", "a": "@/link/sub", "b": "@/rel"},
    {"f": "within", "a": "@/tree", "b": "@/tree/sub"},
    {"f": "within", "a": "@/anything", "b": "/"},
    {"f": "within", "a": "@/x\u0000", "b": "@"},
    {"f": "within", "a": "sub", "b": "@/tree"},
    {"f": "within", "a": "sub", "b": "@/tree", "nocwd": True},
    {"f": "within", "a": "@/loop/a", "b": "@/loop"},
    {"f": "tree", "a": None, "b": "@"},
    {"f": "tree", "a": "@", "b": None},
    {"f": "tree", "a": "@/tree", "b": "@/link"},
    {"f": "tree", "a": "@/tree", "b": "@/tree/sub"},
    {"f": "tree", "a": "@/tree/up", "b": "@"},
    {"f": "tree", "a": "@/tree", "b": "@/tree\u0000"},
    {"f": "tree", "a": ".", "b": "@/rel"},
    {"f": "tree", "a": ".", "b": "@/rel", "nocwd": True},
    # record_root
    {"f": "record", "origin": True, "wt": "@/w", "anchoring": {"anchor": A_LIVE}},
    {"f": "record", "origin": True, "wt": None, "anchoring": None},
    {"f": "record", "origin": False, "wt": None, "anchoring": None},
    {"f": "record", "origin": True, "wt": None, "anchoring": {"anchor": A_STAND}},
    {"f": "record", "origin": False, "wt": None, "anchoring": {"anchor": None}},
    {"f": "record", "origin": False, "wt": None, "anchoring": {"anchor": A_NONE}},
]


def _gated(case: dict[str, Any]) -> bool:
    """Whether `Path.resolve` meets the link loop: 3.11 and 3.12 raise
    RuntimeError there, which no guard catches; 3.13 resolves."""
    return case["f"] in ("stale", "att", "rel") and "@/loop" in str(case)


def _gate_lines(root: str, gated: bool) -> list[dict[str, Any]]:
    def at(text: Any) -> Any:
        if isinstance(text, str):
            return text.replace("@", root)
        if isinstance(text, list):
            return [at(x) for x in text]
        return text

    def back(text: Any) -> Any:
        if isinstance(text, str):
            return text.replace(root, "@")
        if isinstance(text, list):
            return [back(x) for x in text]
        return text

    out = []
    for case in [c for c in GATE_CASES if _gated(c) == gated]:
        calls: list[Any] = []
        run: Callable[[], Any]
        f = case["f"]
        anchor = _anchor(case.get("anchor"), at)

        def cwd() -> str:
            if case.get("nocwd"):
                raise FileNotFoundError(2, "No such file or directory")
            return root + "/tree"

        patches: list[Any] = [mock.patch("os.getcwd", cwd)]
        if f == "stale":
            fail = case["fail"]

            def load(stored: list[str]) -> list[Any]:
                return [
                    types.SimpleNamespace(name=n, render=lambda n=n: f"claim {n}")
                    for n in stored
                ]

            def check(claim: Any, r: Any) -> str | None:
                calls.append([claim.name, back(str(r))])
                return fail.get(claim.name)

            patches += [
                mock.patch.object(hv, "load_claims", load),
                mock.patch.object(hv, "check_claim", check),
                mock.patch.object(
                    hv, "claim_reason_is_indeterminate", lambda r: r.startswith("?")
                ),
            ]
            run = lambda: hv._refuse_stale_stored_claims(  # noqa: E731
                case["stored"], at(case["origin"]), anchor=anchor
            )
        elif f == "att":

            def unverifiable(entries: Any, /, **kw: Any) -> list[str]:
                if "anchor" in kw:
                    assert kw["anchor"] is anchor and kw["stored"] is True
                    calls.append(["anchor"])
                else:
                    calls.append(["root", back(kw["worktree_root"])])
                return at(case["unseen"])

            patches.append(
                mock.patch.object(hv, "unverifiable_attestations", unverifiable)
            )
            run = lambda: hv._refuse_unverifiable_stored_attestations(  # noqa: E731
                case["stored"], at(case["origin"]), anchor=anchor
            )
        elif f == "evid":

            def live(r: str) -> bool:
                calls.append(["live", back(r)])
                return bool(case["live"])

            def drift(body: str, **kw: Any) -> Any:
                assert body == case["body"]
                if "anchor" in kw:
                    assert kw["anchor"] is anchor
                    calls.append(["anchor"])
                else:
                    calls.append(["root", back(kw["worktree_root"])])
                c, m, e = at(case["drift"])
                return types.SimpleNamespace(checked=c, missing=m, expected_absent=e)

            patches += [
                mock.patch.object(hv, "_worktree_root_is_live", live),
                mock.patch.object(hv, "detect_path_drift", drift),
            ]
            ap, aa, dc = case["flags"]
            run = lambda: hv._refuse_evidence_free_stamp(  # noqa: E731
                case["id"],
                case["body"],
                at(case["origin"]),
                attests_paths=ap,
                attests_absence=aa,
                declares_claims=dc,
                anchor=anchor,
            )
        elif f == "rel":
            run = lambda: hv._relative_to_root(at(case["a"]), at(case["b"]))  # noqa: E731
        elif f == "within":
            run = lambda: hv._within(at(case["a"]), at(case["b"]))  # noqa: E731
        elif f == "tree":
            run = lambda: hv._is_callers_tree(at(case["a"]), at(case["b"]))  # noqa: E731
        else:
            origin = (
                types.SimpleNamespace(worktree_root=at(case["wt"]))
                if case["origin"]
                else None
            )
            snap = types.SimpleNamespace(id="m1", origin=origin)
            spec = case["anchoring"]
            anchoring = None
            if spec is not None:
                held = _anchor(spec["anchor"], at)

                def for_record(rid: str, held: Any = held) -> Any:
                    calls.append(["for_record", rid])
                    return held

                anchoring = types.SimpleNamespace(for_record=for_record)
            run = lambda: hv._record_root(cast(Any, snap), cast(Any, anchoring))  # noqa: E731
        with patches[0]:
            for p in patches[1:]:
                p.start()
            try:
                o: Any = back(run())
            except ValueError as exc:
                o = back(str(exc))
            except RuntimeError:
                # 3.11 and 3.12 only: the file is gated on the release.
                o = {"raised": "RuntimeError"}
            finally:
                for p in patches[1:]:
                    p.stop()
        out.append({**case, "o": o, "calls": calls})
    return out


def generate_gates() -> dict[str, bytes]:
    return _gates_file("handlers_verify_gates", gated=False)


def generate_gates_python() -> dict[str, bytes]:
    return _gates_file("handlers_verify_gates_python", gated=True)


def _gates_file(name: str, gated: bool) -> dict[str, bytes]:
    refuse_root()
    with tempfile.TemporaryDirectory() as raw:
        root = os.path.realpath(raw)
        for d in DIRS:
            os.makedirs(os.path.join(root, d), exist_ok=True)
        for f in FILES:
            with open(os.path.join(root, f), "w", encoding="ascii") as fh:
                fh.write("x\n")
        for link, target in LINKS:
            os.symlink(target.replace("@", root), os.path.join(root, link))
        lines = _gate_lines(root, gated)
    header = {
        "k": "header",
        "count": len(lines),
        "dirs": DIRS,
        "files": FILES,
        "links": LINKS,
    }
    return {f"{name}.jsonl": jsonl([header, *lines])}
