"""Fixtures: the membership model, unit membership-3 (`membership.py`
972-1028 and 1079-1315): the views and their liveness (`view_state`,
`live_view`, `_live_state`, `liveness_held`), witness O (`_continuity`,
`_confirms`), the checkout of an object store (`_by_common`,
`_by_pointer`) and the history readings (`_holds`, `_held`,
`_rewritten`, `_most_specific`, `_specific`, `_independent`); and
`location.continuity` (1040-1094).

The reference model runs over `gen_membership_core._Stub`, with a stub
liveness (a state per path, every read logged), a stub history reader
(every call logged) and a stub `_shared_remint`.

Two files, one JSON object per line, keys sorted, ASCII, not gated:

- ``membership_views.jsonl``: a case ``{"case", "tables", "live",
  "steps"}``; each step ``{"op", "a", "o", "log", "lv", "hc"}`` with the
  answer (or ``{"raised": [class, text]}``), the store calls, the paths
  the liveness read and the history calls.
- ``location_continuity.jsonl``: rows ``{"a", "o"}`` of `continuity`
  with the readers it asked, then a digest of an exhaustive sweep over a
  small domain (the rule is in `_sweep`).
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import datetime, timezone
from typing import Any

from bettermemory import location
from bettermemory import membership as mm
from bettermemory.membership_records import (
    InitStamp,
    NeedIndependent,
    ObjectKey,
    ProjectFileRef,
)

from ._common import jsonl
from .gen_membership_core import _Stub, _raised, _spelled
from .gen_membership_rows import _CHECKOUT, _PROJECT, _VIEW, enc

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _History:
    def __init__(self, spec: dict[str, Any], calls: list[Any]) -> None:
        self.spec = spec
        self.calls = calls

    def holds(self, commits: Any) -> set[str]:
        self.calls.append(["holds", list(commits)])
        return set(commits) & set(self.spec.get("holds", []))

    def holds_objects(self, ids: Any) -> set[str]:
        raise AssertionError

    def fingerprint(self, commit: str) -> str | None:
        self.calls.append(["fingerprint", commit])
        return self.spec.get("fp", {}).get(commit)

    def parents(self, commit: str) -> list[str]:
        self.calls.append(["parents", commit])
        return list(self.spec.get("parents", {}).get(commit, []))

    def reachable_among(self, commits: Any) -> Any:
        self.calls.append(["reachable_among", list(commits)])
        reach = self.spec.get("reach", {})
        out = {c.lower(): frozenset(reach.get(c.lower(), [])) for c in commits}
        if "bounded" not in self.spec:
            return out

        class _Reach(dict[str, frozenset[str]]):
            bounded = self.spec["bounded"]

        return _Reach(out)

    def blobs(self, commit: str) -> list[str]:
        raise AssertionError


class _GitHistory(_History):
    def independent(self, commits: Any) -> frozenset[str] | None:
        raise AssertionError


def _history(spec: Any, calls: list[Any]) -> Any:
    if spec is None:
        return None
    return (_GitHistory if spec.get("git") else _History)(spec, calls)


_LOC = {
    "kind": "primary",
    "root": "/w/r",
    "git_dir": "/w/r/.git",
    "head": None,
    "ref_tips": [],
    "token": None,
    "token_written": None,
    "token_status": "present",
    "object_key": ["1", "100", None],
    "common_object_key": None,
    "stamps": [],
    "files": [],
}


def _key(v: Any) -> ObjectKey | None:
    return None if v is None else ObjectKey(v[0], v[1], v[2])


def _loc(change: dict[str, Any], history: Any) -> Any:
    s = _LOC | change
    return mm.Location(
        machine="M1",
        cwd=s["root"] or "/w",
        state="repo",
        root=s["root"],
        kind=s["kind"],
        git_dir=s["git_dir"],
        object_key=_key(s["object_key"]),
        common_object_key=_key(s["common_object_key"]),
        head=s["head"],
        ref_tips=tuple(s["ref_tips"]),
        token=s["token"],
        token_written=s["token_written"],
        token_status=s["token_status"],
        stamps=tuple(InitStamp(*x) for x in s["stamps"]),
        files=tuple(
            ProjectFileRef(f[0], "n", f[1], f[1] + "/bettermemory.toml")
            for f in s["files"]
        ),
        history=history,
    )


def _k(kid: str, project: str, **kw: Any) -> dict[str, Any]:
    return {**_CHECKOUT, "id": kid, "project": project, **kw}


def _v(kid: str, path: str, order: int, machine: str = "M1") -> dict[str, Any]:
    return {
        **_VIEW,
        "checkout": kid,
        "path": path,
        "seen_order": order,
        "machine": machine,
    }


def _p(pid: str, **kw: Any) -> dict[str, Any]:
    return {**_PROJECT, "id": pid, "name": pid.lower(), **kw}


def _w(project: str, commit: Any, arrival: int, fp: Any = None) -> dict[str, Any]:
    return {
        "project": project,
        "commit_id": commit,
        "arrival": arrival,
        "fingerprint": fp,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, tables: dict[str, Any], live: dict[str, str]) -> None:
        self.stub = _Stub(tables, None, False)
        self.lv: list[str] = []
        self.hc: list[Any] = []
        self.remints = 0

        def liveness(k: Any, v: Any) -> str:
            self.lv.append(v.path)
            return live.get(v.path, "gone")

        self.m = mm.Membership(
            self.stub,  # type: ignore[arg-type]
            "M1",
            liveness=liveness,  # type: ignore[arg-type]
            now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def remint(self, answer: bool) -> None:
        def shared(k: Any, loc: Any, here: Any) -> bool:
            self.remints += 1
            return answer

        self.m._shared_remint = shared  # type: ignore[method-assign,assignment]

    def answer(self, op: str, a: Any) -> Any:  # noqa: C901, PLR0911
        m = self.m
        hist = _history(a.get("hist"), self.hc)
        loc: Any = _loc(a["loc"], hist) if "loc" in a else None
        k: Any = m.checkout(a["k"]) if "k" in a else None
        if op == "view_state":
            view = None if a.get("view") is None else k.views[a["view"]]
            return m.view_state(k, view)
        if op == "live_view":
            return m.live_view(k)
        if op == "held_span":
            with m.liveness_held():
                return [self.answer(s["op"], s["a"]) for s in a["ops"]]
        if op == "continuity":
            self.remint(a.get("remint", False))
            ok, how = m._continuity(k, loc)
            return [ok, how, self.remints]
        if op == "confirms":
            return m._confirms(k, loc)
        if op == "by_common":
            self.remint(a.get("remint", False))
            got, rule = m._by_common(loc)
            return [None if got is None else got.id, rule, self.remints]
        if op == "by_pointer":
            return m._by_pointer(k, loc)
        if op == "holds":
            return sorted(m._holds(_loc({}, hist), a["commits"]))
        if op == "held":
            held_by = m._held(loc, a.get("exclude", ()))
            return [[p, sorted(s)] for p, s in sorted(held_by.items())]
        if op == "rewritten":
            return sorted(m._rewritten(loc, a.get("exclude", ())).items())
        if op in ("specific", "most_specific", "independent"):
            anc = a.get("ancestry")
            m._ancestry = (
                None
                if anc is None
                else {tuple(s): (None if r is None else frozenset(r)) for s, r in anc}
            )
            try:
                if op == "independent":
                    named = m._independent(hist, tuple(a["shas"]))
                    return None if named is None else sorted(named)
                held = {p: set(s) for p, s in a["held"].items()}
                if op == "most_specific":
                    return m._most_specific(_loc({}, hist), held)
                spec = m._specific(_loc({}, hist), held)
                return [spec.best, sorted(spec.top), spec.held, spec.bounded]
            except NeedIndependent as e:
                return {"need": list(e.commits)}
            finally:
                m._ancestry = None
        raise ValueError(op)


def _case(
    name: str,
    tables: dict[str, Any],
    steps: list[tuple[str, dict[str, Any]]],
    live: dict[str, str] | None = None,
) -> dict[str, Any]:
    live = live or {}
    run = _Run(tables, live)
    out = []
    for op, a in steps:
        start = len(run.stub.log)
        run.lv, run.hc, run.remints = [], [], 0
        try:
            o = enc(run.answer(op, a))
        except Exception as e:  # noqa: BLE001
            o = _raised(e)
        out.append(
            {
                "op": op,
                "a": a,
                "o": o,
                "log": enc(run.stub.log[start:]),
                "lv": run.lv,
                "hc": run.hc,
            }
        )
    return {"case": name, "tables": _spelled(tables), "live": live, "steps": out}


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

_S1 = ["description", 1, 2, 3, 4]
_S2 = ["info/exclude", 5, 6, 7, 8]


def _views_cases() -> list[dict[str, Any]]:
    tables = {
        "checkouts": [
            _k("K1", "P1"),
            _k("K2", "P1", kind="unresolved"),
            _k("K3", "P1", machine="M2"),
            _k("K4", "P1"),
        ],
        "checkout_views": [
            _v("K1", "/a/old", 1),
            _v("K1", "/a/new", 3),
            _v("K1", "/a/mid", 2),
            _v("K1", "/a/far", 9, "M2"),
            _v("K2", "/a/u", 1),
            _v("K3", "/a/m2", 1, "M2"),
            _v("K4", "/a/t1", 4),
            _v("K4", "/a/t2", 4),
        ],
    }
    live = {
        "/a/old": "live",
        "/a/mid": "live_legacy",
        "/a/new": "gone",
        "/a/t2": "live",
    }
    k = lambda kid, **kw: {"k": kid, **kw}  # noqa: E731
    steps = [
        ("view_state", k("K1")),
        ("view_state", k("K1", view=0)),
        ("view_state", k("K1", view=3)),
        ("view_state", k("K2")),
        ("view_state", k("K3")),
        ("view_state", k("K4")),
        ("live_view", k("K1")),
        ("live_view", k("K2")),
        ("live_view", k("K3")),
        ("live_view", k("K4")),
        ("held_span", {"ops": [{"op": "live_view", "a": k("K1")}] * 2}),
        (
            "held_span",
            {
                "ops": [
                    {"op": "view_state", "a": k("K1")},
                    {
                        "op": "held_span",
                        "a": {"ops": [{"op": "live_view", "a": k("K1")}]},
                    },
                    {"op": "view_state", "a": k("K1", view=1)},
                ]
            },
        ),
        ("live_view", k("K1")),
    ]
    return [_case("views", tables, steps, live)]


def _continuity_cases() -> list[dict[str, Any]]:
    tables = {
        "projects": [_p("P1"), _p("P2", merged_into="P1"), _p("P3")],
        "checkouts": [
            _k("KT", "P1", token="tokA", token_status="written"),
            _k("KS", "P1", token="tokA", token_status="superseded", last_head=None),
            _k("KH", "P1", token=None, last_head="aaa"),
            _k("KI", "P1", token=None, last_head=None),
            _k("KN", "P2", token=None, last_head=None, stamps_json=[]),
            _k(
                "KF",
                "P2",
                kind="folder",
                token=None,
                last_head=None,
                stamps_json=[],
                git_dir=None,
            ),
            _k(
                "KC",
                "P3",
                token=None,
                last_head=None,
                stamps_json=[],
                common_device="7",
                common_inode="70",
            ),
        ],
        "project_witnesses": [
            _w("P1", "C1" + "0" * 38, 1),
            _w("P2", "c2" + "0" * 38, 2),
            _w("P3", "c3" + "0" * 38, 3),
        ],
        "project_files": [
            {"file_id": "F1", "project": "P2", "evidence": "init"},
            {"file_id": "F2", "project": "P3", "evidence": "init"},
        ],
    }
    c1 = "c1" + "0" * 38
    hist: dict[str, Any] = {"holds": ["aaa", c1]}
    steps: list[tuple[str, dict[str, Any]]] = []
    h: Any
    for kid, loc, h, remint in [
        ("KT", {"token": "tokA"}, None, False),
        ("KT", {"token": "tokB"}, None, False),
        ("KT", {"token": "tokB"}, None, True),
        ("KT", {"token": None, "token_written": "tokA"}, None, False),
        ("KT", {"token": "", "token_written": "tokB"}, None, True),
        ("KT", {"token": None}, None, True),
        ("KS", {"token": None, "token_written": "tokB"}, None, True),
        ("KS", {"token": "tokB"}, None, False),
        ("KS", {"token": "tokB", "head": "zzz"}, hist, False),
        ("KS", {"token": "tokA"}, None, False),
        ("KH", {"head": "aaa"}, None, False),
        ("KH", {"head": "bbb"}, None, False),
        ("KH", {"head": "bbb"}, hist, False),
        ("KH", {"head": "bbb"}, {"holds": []}, False),
        ("KI", {"stamps": [_S1]}, None, False),
        ("KI", {"stamps": [_S2]}, None, False),
        ("KI", {"stamps": []}, hist, False),
        ("KI", {"stamps": []}, {"holds": []}, False),
        ("KN", {"stamps": [_S1]}, hist, False),
        ("KN", {}, None, False),
        ("KF", {"kind": "folder", "files": [["F1", "/w/r"]]}, None, False),
        ("KF", {"kind": "folder", "files": [["F2", "/w/r"]]}, None, False),
        ("KF", {"kind": "folder", "files": []}, None, False),
        ("KC", {"common_object_key": ["7", "70", None]}, None, False),
        ("KC", {"common_object_key": ["1", "100", None]}, None, False),
        ("KC", {"common_object_key": ["7", "71", None]}, None, False),
    ]:
        steps.append(
            ("continuity", {"k": kid, "loc": loc, "hist": h, "remint": remint})
        )
    for kid, loc, h in [
        ("KF", {"kind": "folder", "files": [["F1", "/w/r"], ["F2", "/w/r"]]}, None),
        ("KF", {"kind": "primary", "files": [["F2", "/w/x"], ["F1", "/w/r/"]]}, None),
        ("KF", {"kind": "primary", "root": None, "files": [["F1", "/w/r"]]}, None),
        ("KT", {"common_object_key": ["7", "70", None]}, None),
        ("KC", {"common_object_key": ["7", "70", "5"]}, None),
        ("KC", {"common_object_key": None}, {"holds": ["c3" + "0" * 38]}),
        ("KT", {}, hist),
        ("KT", {}, {"holds": []}),
        ("KN", {}, {"holds": ["c2" + "0" * 38]}),
    ]:
        steps.append(("confirms", {"k": kid, "loc": loc, "hist": h}))
    return [_case("continuity", tables, steps)]


def _common_cases() -> list[dict[str, Any]]:
    tables = {
        "projects": [_p("P1")],
        "checkouts": [
            _k(
                "KA",
                "P1",
                token=None,
                last_head=None,
                stamps_json=[],
                object_device="7",
                object_inode="70",
                object_birth="9",
            ),
            _k(
                "KB",
                "P1",
                token=None,
                last_head=None,
                stamps_json=[],
                object_device="5",
                object_inode="50",
                object_birth=None,
                common_device="7",
                common_inode="71",
                common_birth=None,
            ),
            _k(
                "KL",
                "P1",
                kind="linked",
                token="tokL",
                last_head=None,
                stamps_json=[],
                git_dir="/w/m/.git/worktrees/l",
                object_device="5",
                object_inode="51",
                object_birth=None,
                common_device="8",
                common_inode="80",
                common_birth=None,
            ),
            _k(
                "KP",
                "P1",
                token="tokP",
                last_head=None,
                stamps_json=[],
                git_dir="/w/p/.git",
                object_device="8",
                object_inode="81",
                object_birth=None,
            ),
            _k(
                "KR",
                "P1",
                kind="bare",
                token="tokR",
                last_head=None,
                stamps_json=[],
                git_dir="/w/bare",
                object_device="6",
                object_inode="60",
                object_birth=None,
                machine="M1",
            ),
            _k(
                "KX",
                "P1",
                kind="bare",
                token="tokX",
                last_head=None,
                stamps_json=[],
                git_dir="/w/bx",
                object_device="6",
                object_inode="60",
                object_birth="1",
                machine="M2",
            ),
        ],
        "checkout_views": [
            _v("KL", "/w/l", 1),
            _v("KP", "/w/p", 1),
            _v("KR", "/w/bare", 1),
        ],
    }
    live = {"/w/l": "live", "/w/p": "live", "/w/bare": "live_legacy"}
    linked = {"kind": "linked", "token": "tokZ"}
    steps: list[tuple[str, dict[str, Any]]] = []
    for loc, remint in [
        ({"common_object_key": None}, False),
        ({"kind": "folder", "common_object_key": ["7", "70", "9"]}, False),
        ({"common_object_key": ["7", "70", "9"]}, False),
        ({"common_object_key": ["7", "70", "8"]}, False),
        ({"common_object_key": ["7", "70", None], "stamps": [_S1]}, False),
        ({"common_object_key": ["7", "71", None]}, False),
        (
            {
                **linked,
                "git_dir": "/w/m/.git/worktrees/n",
                "common_object_key": ["8", "80", None],
            },
            False,
        ),
        (
            {
                **linked,
                "git_dir": "/w/m/.git/worktrees/n",
                "common_object_key": ["8", "80", None],
            },
            True,
        ),
        (
            {
                **linked,
                "git_dir": "/w/p/.git/worktrees/n",
                "common_object_key": ["8", "81", None],
            },
            False,
        ),
        (
            {
                **linked,
                "git_dir": "/w/q/.git/worktrees/n",
                "common_object_key": ["8", "81", None],
            },
            False,
        ),
        (
            {
                **linked,
                "git_dir": "/w/bare/worktrees/n",
                "common_object_key": ["6", "60", None],
            },
            False,
        ),
        (
            {
                **linked,
                "git_dir": "/w/bare/wt/n",
                "common_object_key": ["6", "60", None],
            },
            False,
        ),
        (
            {
                "token": "tokZ",
                "git_dir": "/w/p/.git",
                "common_object_key": ["8", "81", None],
            },
            False,
        ),
    ]:
        steps.append(("by_common", {"loc": loc, "remint": remint}))
    for kid, loc in [
        (
            "KL",
            {
                "kind": "primary",
                "git_dir": "/w/m/.git",
                "common_object_key": ["8", "80", None],
            },
        ),
        (
            "KL",
            {
                "kind": "primary",
                "git_dir": "/w/m/.git",
                "common_object_key": ["8", "99", None],
            },
        ),
        (
            "KP",
            {
                "kind": "primary",
                "git_dir": "/w/p/.git",
                "common_object_key": ["8", "81", None],
            },
        ),
        (
            "KP",
            {
                "kind": "linked",
                "git_dir": "/w/p/.git/worktrees/x/",
                "common_object_key": ["8", "81", None],
            },
        ),
        (
            "KP",
            {"kind": "linked", "git_dir": "", "common_object_key": ["8", "81", None]},
        ),
        (
            "KP",
            {
                "kind": "linked",
                "git_dir": "/w/p/.git/worktrees/x",
                "common_object_key": None,
            },
        ),
        (
            "KR",
            {
                "kind": "linked",
                "git_dir": "/w/bare/worktrees/x",
                "common_object_key": ["6", "60", None],
            },
        ),
        (
            "KX",
            {
                "kind": "linked",
                "git_dir": "/w/bx/worktrees/x",
                "common_object_key": ["6", "60", None],
            },
        ),
    ]:
        steps.append(("by_pointer", {"k": kid, "loc": loc}))
    return [_case("common", tables, steps, live)]


def _history_cases() -> list[dict[str, Any]]:
    a, b, c, d = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    tables = {
        "projects": [_p("P1"), _p("P2"), _p("P3", merged_into="P2")],
        "project_witnesses": [
            _w("P1", a.upper(), 1, "fa"),
            _w("P2", b, 2, ""),
            _w("P3", c, 3, "fc"),
            _w("P1", c, 4, "fa"),
            _w("P2", d, 5, None),
        ],
    }
    hist = {
        "holds": [a, c, "e" * 40],
        "fp": {"h1": "fa", "h2": "fc", "h3": "", "h4": "fa", "t1": "fc"},
        "parents": {"h1": ["h2", "h3"], "h2": ["h4", "h1"], "h3": ["h4"]},
    }
    loop = {
        "fp": {f"n{i}": "fa" for i in range(600)},
        "parents": {f"n{i}": [f"n{i + 1}"] for i in range(599)},
    }
    born = {"head": "h1"}
    steps: list[tuple[str, dict[str, Any]]] = [
        ("holds", {"hist": hist, "commits": []}),
        ("holds", {"hist": None, "commits": [a]}),
        ("holds", {"hist": hist, "commits": [c, a, c, "x"]}),
        ("held", {"loc": born, "hist": None}),
        ("held", {"loc": {}, "hist": hist}),
        ("held", {"loc": born, "hist": hist}),
        ("held", {"loc": {"ref_tips": ["t1"]}, "hist": hist, "exclude": ["P2"]}),
        ("held", {"loc": born, "hist": hist, "exclude": ["P1"]}),
        ("held", {"loc": born, "hist": {"holds": []}}),
        ("rewritten", {"loc": born, "hist": None}),
        ("rewritten", {"loc": {}, "hist": hist}),
        ("rewritten", {"loc": born, "hist": hist}),
        ("rewritten", {"loc": {"head": "", "ref_tips": ["h3", "t1"]}, "hist": hist}),
        ("rewritten", {"loc": born, "hist": hist, "exclude": ["P2"]}),
        ("rewritten", {"loc": {"head": "n0"}, "hist": loop}),
    ]
    hr: dict[str, Any] = {"reach": {a: [c], b: []}}
    anc: Any
    for held, h, anc in [
        ({"P1": [a, c], "P2": [c]}, None, None),
        ({}, hr, None),
        ({"P1": [a], "P2": [a]}, {**hr, "git": True}, {}),
        ({"P1": [a], "P2": [c]}, hr, None),
        ({"P1": [a], "P2": [c]}, {**hr, "bounded": False}, None),
        ({"P1": [a], "P2": [c]}, {**hr, "bounded": True}, None),
        ({"P1": [a], "P2": [c], "P3": [b]}, {**hr, "git": True}, None),
        ({"P1": [a.upper()], "P2": [c]}, {**hr, "git": True}, {}),
        ({"P1": [a.upper()], "P2": [c]}, {**hr, "git": True}, [[[a, c], [a]]]),
        ({"P1": [a], "P2": [c]}, {**hr, "git": True}, [[[a, c], None]]),
        ({"P1": [a], "P2": [c]}, {**hr, "git": True}, [[[a, b], [a]]]),
        ({"P1": [a], "P2": [c]}, hr, [[[a, c], [c]]]),
    ]:
        for op in ("specific", "most_specific"):
            steps.append((op, {"held": held, "hist": h, "ancestry": anc}))
    for shas, git, anc in [
        ([], False, None),
        ([a], True, {}),
        ([a, b], False, {}),
        ([a, b], True, None),
        ([a, b], True, {}),
        ([a, b], True, [[[a, b], [b]]]),
        ([a, b], True, [[[a, c], [c]]]),
    ]:
        steps.append(
            ("independent", {"hist": {"git": git}, "shas": shas, "ancestry": anc})
        )
    return [_case("history", tables, steps)]


def generate() -> dict[str, bytes]:
    rows = _views_cases() + _continuity_cases() + _common_cases() + _history_cases()
    return {"membership_views.jsonl": jsonl(rows)}


# ---------------------------------------------------------------------------
# location.continuity
# ---------------------------------------------------------------------------

_TOKENS = [None, "", "A", "B"]
_HEADS = [None, "h1", "h2"]
_STAMPS = [[], [["s", 1]], [["s", 1], ["t", 2]], [["t", 2]]]
_READER = [None, False, True]


def _domain() -> list[list[Any]]:
    """The sweep's axes, in the order the cases count through them (the
    last axis fastest): token, last_head, stamps, token_here, head_here,
    stamps_here, holds, confirms, superseded, reminted."""
    return [
        _TOKENS,
        _HEADS[:2],
        _STAMPS[:3],
        _TOKENS,
        _HEADS,
        [_STAMPS[0], _STAMPS[1], _STAMPS[3]],
        [False, True],
        _READER,
        [False, True],
        _READER,
    ]


def _one(args: list[Any]) -> list[Any]:
    tok, last, st, here, head, sh, holds, confirms, sup, reminted = args
    asked: list[str] = []

    def reader(name: str, value: bool) -> Any:
        def f(*_: Any) -> Any:
            asked.append(name)
            return (
                {"x"}
                if (name == "holds" and value)
                else (set() if name == "holds" else value)
            )

        return f

    ok, how = location.continuity(
        tok,
        last,
        [tuple(s) for s in st],
        token_here=here,
        head_here=head,
        stamps_here=[tuple(s) for s in sh],
        holds=reader("holds", holds),
        confirms=None if confirms is None else reader("confirms", confirms),
        superseded=sup,
        reminted=None if reminted is None else reader("reminted", reminted),
    )
    return [ok, how, asked]


def _sweep() -> dict[str, Any]:
    h = hashlib.sha256()
    count = 0
    for args in itertools.product(*_domain()):
        ok, how, asked = _one(list(args))
        h.update(f"{int(ok)}|{how}|{','.join(asked)}\n".encode())
        count += 1
    return {"sweep": "product", "count": count, "sha256": h.hexdigest()}


def generate_continuity() -> dict[str, bytes]:
    rows = []
    for i, args in enumerate(itertools.product(*_domain())):
        if i % 499 == 0:
            rows.append({"a": list(args), "o": _one(list(args))})
    return {"location_continuity.jsonl": jsonl([*rows, _sweep()])}
