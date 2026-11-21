"""Fixtures: the read side of the membership model, unit mqueries
(`membership_queries.py` 69-277 and `membership.py` 1700-1729): `home`,
`homes` and `_home_from`, `admit_recall`, `restated`, `_written_afresh`,
`_restatement` and `filed_elsewhere`.

The reference model runs over the recording store of
`gen_membership_core` (`_Stub`), given the store's `declared_scopes`
over a table ``scoped`` of ``{id, scopes}`` rows and logged as
``["scopes", ids]``. The root project file of a view is scripted: the
module's `Path` and `project_file.parse` are replaced, while the file is
written, by a table from a view's path to what the file holds (``missing``,
``stat_oserror`` where `exists` raises, ``oserror`` and ``valueerror`` where
`parse` raises, ``error`` where it answers no ProjectFile, or the id of a
well-formed one). No file is read.

One file, ``membership_queries.jsonl``, not gated: a sequence per line
``{"seq", "tables", "files", "fail", "steps"}``, each step ``{"op", "a",
"o", "log", "rows"}`` as `gen_membership_core` spells them. Every name and
path is ASCII, so no row follows the Unicode tables.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bettermemory import membership as mm
from bettermemory import membership_queries as mq
from bettermemory import membership_rows as rows
from bettermemory import project_file

from ._common import jsonl
from .gen_membership_core import _raised, _spelled, _Stub
from .gen_membership_rows import _CHECKOUT, _PROJECT, _PROPOSAL, _VIEW, dec, enc


class _Scoped(_Stub):
    def declared_scopes(self, record_ids: Any) -> dict[str, list[str]]:
        self._note("scopes", list(record_ids))
        out: dict[str, list[str]] = {}
        for r in self.tables.get("scoped", []):
            if r["id"] in record_ids and isinstance(r["scopes"], list):
                out[str(r["id"])] = [str(s) for s in r["scopes"]]
        return out


_FILES: dict[str, Any] = {}


class _FakePath:
    def __init__(self, path: str, view: str | None = None) -> None:
        self.path = path
        self.view = view

    def __truediv__(self, name: str) -> _FakePath:
        return _FakePath(f"{self.path}/{name}", self.path)

    def exists(self) -> bool:
        held = _FILES.get(str(self.view), "missing")
        if held == "stat_oserror":
            raise OSError("stat")
        return held != "missing"


def _parse(path: _FakePath) -> Any:
    held = _FILES.get(str(path.view), "missing")
    if held == "oserror":
        raise OSError("read")
    if held == "valueerror":
        raise ValueError("malformed")
    if held == "error":
        return "not a project file"
    return project_file.ProjectFile(
        id=held["id"], name="n", boundary=False, path=Path("/scripted")
    )


@contextlib.contextmanager
def _scripted(files: dict[str, Any]) -> Iterator[None]:
    saved = (getattr(mq, "Path"), project_file.parse)
    _FILES.clear()
    _FILES.update(files)
    setattr(mq, "Path", _FakePath)
    setattr(project_file, "parse", _parse)
    try:
        yield
    finally:
        setattr(mq, "Path", saved[0])
        setattr(project_file, "parse", saved[1])
        _FILES.clear()


def _home(h: mm.Home | None) -> Any:
    if h is None:
        return None
    return {
        "record_id": h.record_id,
        "kind": h.kind,
        "project": h.project,
        "checkout": h.checkout,
        "rule": h.rule,
        "decided": h.decided,
        "recorded_project": h.recorded_project,
        "chain": list(h.chain),
        "tenant": h.tenant,
        "unresolved": h.unresolved,
        "named": sorted(h.named),
    }


def _pr(a: Any) -> mm.Proposal:
    return rows._proposal_from({**_PROPOSAL, **dec(a)})


def _answer(m: mm.Membership, op: str, a: Any) -> Any:
    if op == "home":
        return _home(m.home(a))
    if op == "homes":
        return [[k, _home(h)] for k, h in m.homes(a).items()]
    if op == "admit":
        caller = mm.Resolution(**{**a["caller"], "chain": tuple(a["caller"]["chain"])})
        got = m.admit_recall(caller, a["hits"], admission=a["admission"])
        return {"delivered": list(got.delivered), "suppressed": list(got.suppressed)}
    if op == "restated":
        return rows._proposal_row(m.restated(_pr(a)))
    if op == "afresh":
        return mq._written_afresh(m, _pr(a))
    if op == "restatement":
        return mq._restatement(m, _pr(a["pr"]), a["at"])
    if op == "filed":
        return mq.filed_elsewhere(m, _pr(a))
    raise ValueError(op)


def _sequence(
    name: str,
    tables: dict[str, list[dict[str, Any]]],
    steps: list[dict[str, Any]],
    *,
    files: dict[str, Any] | None = None,
    fail: Any = None,
) -> dict[str, Any]:
    stub = _Scoped(tables, fail, False)
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        now=lambda: datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc),
    )
    done = []
    with _scripted(files or {}):
        for s in steps:
            op, a = s["op"], s.get("a")
            start = len(stub.log)
            out: dict[str, Any] = {"op": op, "a": a}
            try:
                out["o"] = enc(_answer(m, op, a))
            except Exception as e:  # noqa: BLE001
                out["o"] = _raised(e)
            out["log"] = enc(stub.log[start:])
            out["rows"] = list(m._rows)
            done.append(out)
    return {
        "seq": name,
        "tables": "base" if tables is _TABLES else _spelled(tables),
        "files": files or {},
        "fail": fail,
        "steps": done,
    }


def _p(pid: str, name: str, **kw: Any) -> dict[str, Any]:
    return {**_PROJECT, "id": pid, "name": name, **kw}


def _k(kid: str, project: str, *views: tuple[str, int, str]) -> list[dict[str, Any]]:
    return [{**_CHECKOUT, "id": kid, "project": project}] + [
        {**_VIEW, "checkout": kid, "path": p, "seen_order": o, "machine": mach}
        for p, o, mach in views
    ]


def _h(rid: str, project: Any, **kw: Any) -> dict[str, Any]:
    return {
        "record_id": rid,
        "kind": "memory",
        "project": project,
        "checkout": "K1",
        "rule": "file",
        "decided": "2026-01-01T00:00:00Z",
        **kw,
    }


_PROJECTS = [
    _p("P001", "alpha"),
    _p("P002", "beta", merged_into="P001"),
    _p("P003", "gamma", parent="P001", tenant="T1"),
    _p("P004", "limbo", provisional="indeterminate"),
    _p("P005", "later", provisional="deferred"),
    _p("P006", "other", tenant="T2"),
    _p("P007", "plain", provisional="settled"),
]

_KS = [
    _k("K1", "P002", ("/w/one", 1, "M1")),
    _k("K2", "P001", ("/w/two", 1, "M1")),
    _k("K3", "P001", ("/w/three", 1, "M1")),
    _k("K4", "P001", ("/w/four", 1, "M1")),
    _k("K5", "P001", ("/w/five", 1, "M1")),
    _k("K6", "P001", ("/w/six", 1, "M1")),
    _k("K7", "P001", ("/w/seven", 1, "M1")),
    _k("K8", "P001", ("/w/eight", 1, "M2")),
    _k("K9", "P003", ("/w/new", 2, "M1"), ("/w/old", 1, "M1"), ("/w/far", 3, "M2")),
    _k("KG", "PX", ("/w/gone", 1, "M1")),
]

_FILE_MAP: dict[str, Any] = {
    "/w/one": {"id": "F1"},
    "/w/two": {"id": "F2"},
    "/w/four": "oserror",
    "/w/five": "valueerror",
    "/w/six": "error",
    "/w/seven": "stat_oserror",
    "/w/eight": {"id": "F1"},
    "/w/old": {"id": "F1"},
    "/w/new": {"id": "F3"},
    "/w/gone": {"id": "F1"},
}

_TABLES: dict[str, list[dict[str, Any]]] = {
    "projects": _PROJECTS,
    "checkouts": [r for k in _KS for r in k[:1]],
    "checkout_views": [r for k in _KS for r in k[1:]],
    "project_files": [
        {"file_id": "F1", "project": "P003", "evidence": "init"},
        {"file_id": "F2", "project": "P002", "evidence": "init"},
    ],
    "homes": [
        _h("R1", "P001"),
        _h("R2", "P002", kind="episode", checkout=None),
        _h("R3", "P003", rule="witness"),
        _h("R4", "global"),
        _h("R5", "global"),
        _h("R6", "P004"),
        _h("R7", "PX"),
        _h("R8", "P006"),
        _h("R9", "global"),
        _h("R10", "P005"),
        _h("R11", "P007"),
    ],
    "scoped": [
        {"id": "R4", "scopes": ["projects:alpha"]},
        {"id": "R9", "scopes": ["projects:beta", "x", "projects:GAMMA"]},
        {"id": "R1", "scopes": ["projects:gamma"]},
        {"id": "R5", "scopes": "projects:alpha"},
    ],
}

_ALL = ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"]


def _caller(project: Any, chain: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "outcome": "silent",
        "project": project,
        "checkout": "K1",
        "rule": "file",
        "chain": chain,
        **kw,
    }


_CALLERS = [
    _caller("P001", ["P001"]),
    _caller("P003", ["P003", "P001"], tenant="T1"),
    _caller("global", [], checkout=None),
    _caller(None, [], outcome="indeterminate"),
    _caller("P004", ["P004"]),
    _caller("P006", ["P006"], tenant="T2"),
    _caller("P001", []),
]


def _homes() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = [{"op": "home", "a": r} for r in [*_ALL, "RZ", ""]]
    s += [
        {"op": "homes", "a": a}
        for a in [_ALL, [], ["R9", "R4", "R9"], ["RZ"], ["R1", "R4", "R5"]]
    ]
    return s


def _admits() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = []
    for c in _CALLERS:
        for adm in ["prompt_recall", "search_unscoped"]:
            s.append(
                {"op": "admit", "a": {"caller": c, "hits": _ALL, "admission": adm}}
            )
    c = _CALLERS[0]
    for hits, adm in [
        (["R1", "R2"], "prompt_recall"),
        (["R7", "R8", "RZ"], "prompt_recall"),
        (["R6", "R10", "R11"], "prompt_recall"),
        (["RZ", "RY"], "prompt_recall"),
        ([], "prompt_recall"),
        (["RZ"], "bogus"),
        (["R1"], "bogus"),
        (["R7"], "links"),
        (["R7"], "session_brief"),
        (["R1", "R1", "R7", "R7"], "prompt_recall"),
    ]:
        s.append({"op": "admit", "a": {"caller": c, "hits": hits, "admission": adm}})
    return s


_OLD_SEP = "separate: bettermemory project init --new (a fresh project file)"
_Q = 'bettermemory: "/w/one" is new'


def _q(**kw: Any) -> Any:
    return enc(kw)


def _imp(**ev: Any) -> Any:
    base = {"imported_project": "P003", "source": "/x/y.json"}
    return _q(kind="import", candidates_json=["P001"], evidence_json={**base, **ev})


def _unres(**ev: Any) -> Any:
    base = {"path": "/w/gone", "memories": 3, "episodes": 4}
    return _q(
        kind="legacy",
        reason="unresolved_location",
        candidates_json=["P002"],
        evidence_json={**base, **ev},
    )


def _leg(**ev: Any) -> Any:
    base = {"path": "/w/old/repo", "memories": 3, "episodes": 4}
    return _q(
        kind="legacy",
        reason="legacy_no_live_checkout",
        candidates_json=["P002"],
        evidence_json={**base, **ev},
    )


_INF = float("inf")


def _questions() -> list[Any]:
    return [
        _q(state="answered", line="kept"),
        _q(line=_Q),
        _q(line=_Q + "; " + _OLD_SEP),
        _q(line="bettermemory: /w/one is new; " + _OLD_SEP),
        _q(line="bettermemory: '/w/one'"),
        _q(line='bettermemory:  "x'),
        _q(kind="file_contradiction", line=_Q),
        _q(kind="subproject_contradiction", line=_Q),
        _q(kind="subproject_contradiction", evidence_json={"dir": "/w/sub"}),
        _q(kind="subproject_contradiction", evidence_json={"dir": ""}),
        _q(kind="subproject_contradiction", evidence_json={"dir": 5}),
        _q(kind="subproject_contradiction", evidence_json={}),
        _q(kind="remote_diverged", line="bettermemory: x", reason="diverged"),
        _q(kind="remote_diverged", line=_Q),
        _q(kind="duplicate_checkout", candidates_json=[]),
        _q(candidates_json=["P002", "PX", "P003", "", "global"]),
        _imp(codes={"P001": ["b", "a", "c"]}),
        _imp(),
        _imp(codes={"P001": "ba"}),
        _imp(codes={"P001": {"z": 1, "y": 2}}),
        _imp(codes={"P001": [1]}),
        _imp(codes={"P001": []}),
        _imp(codes={"P001": None}),
        _imp(codes={"P002": ["a"]}),
        _imp(codes=["P001"]),
        _imp(codes=None),
        _imp(imported_project="PX"),
        _imp(imported_project=7),
        _imp(source=None),
        _q(kind="import", candidates_json=[], evidence_json={}),
        _q(
            kind="import",
            candidates_json=["PX"],
            evidence_json={"imported_project": "P001", "source": "s"},
        ),
        _q(kind="import", line=_Q, evidence_json={}),
        _unres(),
        _unres(memories="12"),
        _unres(memories=" -7 "),
        _unres(memories="1_000"),
        _unres(memories="x"),
        _unres(memories=2.9),
        _unres(memories=-2.9),
        _unres(memories=True),
        _unres(memories=None),
        _unres(memories=[1]),
        _unres(memories=float("nan")),
        _unres(memories=_INF),
        _unres(episodes=_INF, memories="x"),
        _unres(path=None),
        _q(
            kind="legacy",
            reason="unresolved_location",
            candidates_json=["PX"],
            evidence_json={"path": "/p", "memories": 1, "episodes": 1},
        ),
        _leg(),
        _leg(held=0, recorded=0),
        _leg(held=2, recorded=5),
        _leg(held="", recorded=""),
        _leg(held="3", recorded=[]),
        _leg(held=[2], recorded=None),
        _leg(held=0.0, recorded=False),
        _leg(held="z"),
        _leg(held={}, recorded=[]),
        _leg(held=2, recorded={}),
        _leg(recorded=_INF),
        _leg(path="/"),
        _leg(path="C:\\old\\repo"),
        _leg(path=7),
        _q(
            kind="legacy",
            reason="legacy_no_live_checkout",
            candidates_json=["PX"],
            evidence_json={"path": "/p", "memories": 1, "episodes": 1},
        ),
        _q(
            kind="legacy",
            reason="legacy_no_live_checkout",
            candidates_json=[],
            evidence_json={"path": "/p", "memories": 1, "episodes": 1},
        ),
        _q(kind="legacy", reason="other", evidence_json={}),
        _q(
            kind="legacy",
            reason="unresolved_location",
            state="answered",
            evidence_json={},
        ),
    ]


def _first_sights() -> list[Any]:
    out = [_q(checkout=None)]
    out += [
        _q(checkout=k)
        for k in ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "KG", "KX"]
    ]
    return out


def _restates() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = [{"op": "restated", "a": q} for q in _questions()]
    s += [{"op": "afresh", "a": q} for q in _questions()[16:]]
    fs = _first_sights()
    s += [{"op": "restated", "a": q} for q in fs]
    s += [{"op": "filed", "a": q} for q in fs]
    s += [{"op": "restatement", "a": {"pr": q, "at": "/else"}} for q in fs[:3]]
    s += [
        {"op": "restatement", "a": {"pr": q, "at": at}}
        for q in _questions()[8:12]
        for at in [None, "/else", ""]
    ]
    s += [{"op": "filed", "a": _q(kind="import", checkout="K1")}]
    return s


def _odd_rows() -> dict[str, Any]:
    tables = {
        "projects": _PROJECTS,
        "homes": [
            _h("D1", "P001"),
            _h("D1", "P003", rule="later"),
            _h("I5", "P001", kind=6, rule=None, decided=1.5),
            {
                "record_id": "N1",
                "project": "P001",
                "kind": "memory",
                "rule": "r",
                "decided": "d",
            },
            _h("G1", "global"),
        ],
        "scoped": [{"id": "G1", "scopes": ["projects:alpha", 3]}],
    }
    steps: list[dict[str, Any]] = [
        {"op": "homes", "a": ["D1"]},
        {"op": "home", "a": "D1"},
        {"op": "homes", "a": ["I5"]},
        {"op": "home", "a": "N1"},
        {"op": "homes", "a": ["G1"]},
    ]
    return _sequence("odd_rows", tables, steps)


def _missing_project_column() -> dict[str, Any]:
    tables = {
        "projects": _PROJECTS,
        "homes": [{"record_id": "M1", "kind": "memory"}],
    }
    return _sequence("no_project", tables, [{"op": "home", "a": "M1"}])


def _empty_merge() -> dict[str, Any]:
    tables = {"projects": [*_PROJECTS, _p("P008", "hollow", merged_into="")]}
    steps: list[dict[str, Any]] = [
        {"op": "restated", "a": _q(candidates_json=["P008"])},
        {"op": "restated", "a": _imp(imported_project="P008")},
    ]
    return _sequence("empty_merge", tables, steps)


def _failures() -> list[dict[str, Any]]:
    adm = {"caller": _CALLERS[0], "hits": ["R7"], "admission": "prompt_recall"}
    ok = {"caller": _CALLERS[0], "hits": ["R1"], "admission": "prompt_recall"}
    out = []
    for fail in [["record", 1], ["scopes", 1], ["rows", 1], ["rows", 2]]:
        out.append(
            _sequence(
                f"fail_{fail[0]}_{fail[1]}",
                _TABLES,
                [{"op": "admit", "a": adm}, {"op": "admit", "a": ok}],
                fail=fail,
            )
        )
    for fail in [["rows", 1], ["rows", 2], ["rows", 3], ["rows", 4]]:
        out.append(
            _sequence(
                f"fail_restate_{fail[1]}",
                _TABLES,
                [{"op": "restated", "a": _q(line=_OLD_SEP, checkout="K1")}],
                files=_FILE_MAP,
                fail=fail,
            )
        )
    return out


def generate() -> dict[str, bytes]:
    header = {"seq": "header", "base": _spelled(_TABLES), "proposal": enc(_PROPOSAL)}
    seqs = [
        header,
        _sequence("homes", _TABLES, _homes()),
        _sequence("admit", _TABLES, _admits()),
        _sequence("restate", _TABLES, _restates(), files=_FILE_MAP),
        _odd_rows(),
        _missing_project_column(),
        _empty_merge(),
        *_failures(),
    ]
    return {"membership_queries.jsonl": jsonl(seqs)}
