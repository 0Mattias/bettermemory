"""Fixtures: the legacy questions, the shown proposals and the record
counts of the membership model, unit mlegacy: `pending`,
`settle_legacy_questions`, `below_root`, `_declared_below`,
`_record_counts` and `_records_by_project` (`membership.py`), and
`first_output` (`membership_queries.py`).

The reference model runs over the recording store of
`gen_membership_queries` (`_Scoped`, built on `gen_membership_core`'s
`_Stub`), with the root project files scripted as that generator scripts
them. `location.view_path`, which reads the file system, is replaced by
a table from a directory to the view it is read as (a directory the
table does not name is read as itself). `_DIR_PATHS` is set per step to
``posixpath`` or ``ntpath``, so no row follows the host; every path is
ASCII, so `ntpath.normcase` answers alike whether Python or the system
folds it.

`_records_by_project` walks a set of str, whose order follows the hash
seed, so a step that asks it spells its answer as items sorted by
project (the stub already logs a ``column_in`` list sorted).

One file, ``membership_legacy.jsonl``, not gated: a sequence per line
``{"seq", "tables", "files", "fail", "steps"}``, each step ``{"op", "a",
"o", "log", "rows"}`` as `gen_membership_core` spells them.
"""

from __future__ import annotations

import ntpath
import posixpath
import types
from datetime import datetime, timezone
from typing import Any

from bettermemory import membership as mm
from bettermemory import membership_queries as mq
from bettermemory import membership_records as mr
from bettermemory import membership_rows as rows

from ._common import jsonl
from .gen_membership_core import _raised, _spelled
from .gen_membership_queries import _scripted, _Scoped
from .gen_membership_rows import _CHECKOUT, _PROJECT, _PROPOSAL, _VIEW, enc

_VIEWS = {"/w/link": "/w/one/sub", "/w/up": "/w", "C:/link": "C:\\W\\One"}


class _Stub(_Scoped):
    """`_Scoped` with the ``proposal`` kind the stub of
    `gen_membership_proposals` adds, keyed by id."""

    def put_membership(self, kind: str, row: Any, *, session: Any = None) -> Any:
        if kind != "proposal":
            return super().put_membership(kind, row, session=session)
        self._note("put", kind, dict(row), session)
        rows_ = self.tables.setdefault("proposals", [])
        for i, r in enumerate(rows_):
            if r.get("id") == row["id"]:
                rows_[i] = dict(row)
                break
        else:
            rows_.append(dict(row))
        return self._written()


def _view_path(path: Any) -> str:
    return _VIEWS.get(str(path), str(path))


def _answer(m: mm.Membership, op: str, a: Any) -> Any:
    if op == "settle":
        return [rows._proposal_row(p) for p in m.settle_legacy_questions()]
    if op == "pending":
        return [rows._proposal_row(p) for p in m.pending()]
    if op == "first":
        res = mm.Resolution(
            outcome="silent",
            project=None,
            checkout=a["checkout"],
            rule="r",
            lines=tuple(a["lines"]),
        )
        return mq.first_output(m, res)
    if op == "counts":
        return list(m._record_counts(a))
    if op == "by_project":
        got = m._records_by_project(a)
        return [[k, list(v)] for k, v in sorted(got.items())]
    saved = (mr._DIR_PATHS, getattr(mm, "view_path"))
    mr._DIR_PATHS = ntpath if a.get("nt") else posixpath
    setattr(mm, "view_path", _view_path)
    try:
        k = m.checkout(a["k"])
        if op == "below":
            return m.below_root(k, a["dir"], canonical=a["canonical"])
        if op == "declared":
            decl = types.SimpleNamespace(action=a["action"], directory=a["dir"])
            return m._declared_below(decl, k)  # type: ignore[arg-type]
    finally:
        mr._DIR_PATHS = saved[0]
        setattr(mm, "view_path", saved[1])
    raise ValueError(op)


#: The tables the sequences start from, by name, spelled once in the
#: header line.
_NAMED: dict[str, dict[str, list[dict[str, Any]]]] = {}


def _sequence(
    name: str,
    tables: str,
    steps: list[dict[str, Any]],
    *,
    files: dict[str, Any] | None = None,
    fail: Any = None,
) -> dict[str, Any]:
    stub = _Stub(_NAMED[tables], fail, False)
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
        "tables": tables,
        "files": files or {},
        "fail": fail,
        "steps": done,
    }


def _p(pid: str, name: str, **kw: Any) -> dict[str, Any]:
    return {**_PROJECT, "id": pid, "name": name, **kw}


def _v(kid: str, path: str, order: int, machine: str = "M1") -> dict[str, Any]:
    return {
        **_VIEW,
        "checkout": kid,
        "path": path,
        "seen_order": order,
        "machine": machine,
    }


def _q(qid: str, **kw: Any) -> dict[str, Any]:
    return {**_PROPOSAL, "id": qid, "signature_json": None, "opened": "t", **kw}


def _k(kid: str, project: str, **kw: Any) -> dict[str, Any]:
    return {**_CHECKOUT, "id": kid, "project": project, "stamps_json": None, **kw}


def _leg(qid: str, cand: list[Any], **ev: Any) -> dict[str, Any]:
    base = {"path": "/o", "memories": 3, "episodes": 4}
    return _q(
        qid,
        checkout=None,
        kind="legacy",
        reason="legacy_no_live_checkout",
        candidates_json=cand,
        evidence_json={**base, **ev},
        line="legacy " + qid,
    )


_PROJECTS = [
    _p("L1", "old"),
    _p("C1", "old"),
    _p("C2", "new"),
    _p("C3", "old"),
    _p("C4", "old"),
    _p("M9", "merged", merged_into="C2"),
    _p("L2", "old"),
    _p("L3", "old"),
]

#: C5's only remote is the one L3's non-legacy checkout holds, so Q27
#: closes only when that checkout's remotes are left out. Q28 pairs a
#: legacy project that has remotes with a candidate that has none.
_SETTLE_TABLES: dict[str, list[dict[str, Any]]] = {
    "projects": [*_PROJECTS, _p("C5", "old")],
    "project_remotes": [
        {"project": "C3", "remote_key": "r:x"},
        {"project": "C4", "remote_key": "r:y"},
        {"project": "L2", "remote_key": "r:x"},
        {"project": "C5", "remote_key": "r:z"},
    ],
    "checkouts": [
        _k("KL", "L3", legacy=1, remotes_json=["r:y"]),
        _k("KN", "L3", legacy=0, remotes_json=["r:z"]),
        _k("KM", "L3", legacy=1, remotes_json=[]),
    ],
    "proposals": [
        _leg("Q01", ["C2"], unresolved_project="L1", rule="none"),
        _leg("Q02", ["C1"], unresolved_project="L1", rule="ambiguous"),
        _leg("Q03", ["C1"], unresolved_project="L1", rule="none"),
        _leg("Q04", ["C4"], unresolved_project="L2", rule="none"),
        _leg("Q05", ["C3"], unresolved_project="L2", rule="none"),
        _leg("Q06", ["C4"], unresolved_project="L3", rule="none"),
        _leg("Q07", ["C3"], unresolved_project="L3", rule="none"),
        _leg("Q08", ["C1"], rule="none"),
        _leg("Q09", ["C1"], unresolved_project=7, rule="none"),
        _leg("Q10", [], unresolved_project="L1", rule="none"),
        _leg("Q11", ["C1"], unresolved_project="PX", rule="none"),
        _leg("Q12", ["CX"], unresolved_project="L1", rule="none"),
        _leg("Q13", ["C1"], unresolved_project="L1"),
        _leg("Q14", ["C1"], unresolved_project="L1", rule=0),
        _leg("Q15", ["C1"], unresolved_project="L1", rule=True),
        _leg("Q16", ["M9"], unresolved_project="L1", rule="ambiguous"),
        _leg("Q17", ["M9"], unresolved_project="L1", rule="none"),
        _leg("Q18", ["C1"], unresolved_project="L1", rule="None"),
        _leg("Q19", ["C1"], unresolved_project="L1", rule=["none"]),
        _leg("Q20", ["C1", "C2"], unresolved_project="L1", rule="none"),
        _leg("Q21", ["C1"], unresolved_project="", rule="none"),
        _leg("Q22", ["C1"], unresolved_project="M9", rule="none"),
        {**_leg("Q23", ["C2"], unresolved_project="L1"), "state": "answered"},
        {**_leg("Q24", ["C2"], unresolved_project="L1"), "kind": "import"},
        {**_leg("Q25", ["C2"], unresolved_project="L1"), "reason": "other"},
        {
            **_leg("Q26", ["C2"], unresolved_project="L1"),
            "reason": "unresolved_location",
        },
        _leg("Q27", ["C5"], unresolved_project="L3", rule="none"),
        _leg("Q28", ["C1"], unresolved_project="L2", rule="none"),
    ],
}


def _settles() -> list[dict[str, Any]]:
    out = [
        _sequence(
            "settle",
            "settle",
            [{"op": "settle"}, {"op": "settle"}],
        )
    ]
    odd = {
        "projects": _PROJECTS,
        "proposals": [
            _leg("Q01", ["C2"], unresolved_project="L1"),
            {**_leg("Q02", ["C2"]), "evidence_json": ["unresolved_project"]},
            _leg("Q03", ["C2"], unresolved_project="L1"),
        ],
    }
    _NAMED["list_evidence"] = odd
    out.append(_sequence("settle_list_evidence", "list_evidence", [{"op": "settle"}]))
    rule_list = {
        "projects": _PROJECTS,
        "proposals": [
            _leg("Q01", ["C2"], unresolved_project="L1", rule={"a": 1}),
            _leg("Q02", ["C2"], unresolved_project="L1", rule=1.5),
        ],
    }
    _NAMED["rule_values"] = rule_list
    small = {
        "projects": _PROJECTS,
        "proposals": [
            _leg("Q01", ["C2"], unresolved_project="L1"),
            _leg("Q02", ["C2"], unresolved_project="L1"),
        ],
    }
    _NAMED["small"] = small
    for fail in [
        ["rows", 1],
        ["rows", 2],
        ["rows", 3],
        ["put", 1],
        ["record", 1],
    ]:
        out.append(
            _sequence(
                f"settle_fail_{fail[0]}_{fail[1]}",
                "small",
                [{"op": "settle"}, {"op": "settle"}],
                fail=fail,
            )
        )
    out.append(
        _sequence("settle_empty", "empty", [{"op": "settle"}, {"op": "pending"}])
    )
    return out


_Q = 'bettermemory: "/w/one" is new'

_SHOWN_TABLES: dict[str, list[dict[str, Any]]] = {
    "projects": [_p("P1", "alpha"), _p("P2", "beta")],
    "checkouts": [_k("K1", "P1"), _k("K2", "P2")],
    "checkout_views": [_v("K1", "/w/one", 1), _v("K2", "/w/two", 1)],
    "project_files": [{"file_id": "F1", "project": "P1", "evidence": "init"}],
    "proposals": [
        _q("Q1", checkout="K1", line=_Q),
        _q("Q2", checkout="K1", line=_Q),
        _q("Q3", checkout="K1", line='bettermemory: "/w/one" other'),
        _q("Q4", checkout="K1", line=_Q, state="answered"),
        _q("Q5", checkout="K2", line="plain", candidates_json=["P1", "P2"]),
        _q("Q6", checkout="K1", line="unquoted", candidates_json=["P1", "P2"]),
        _q("Q7", checkout="K1", line="unquoted", candidates_json=["P1", "P2"]),
        _q("Q8", checkout=None, line="none", candidates_json=["P1"]),
        _q("Q9", checkout="", line=_Q),
        {
            **_leg("QA", ["P2"], unresolved_project="P1"),
            "checkout": "K2",
            "evidence_json": {"path": "/w/old", "memories": 1, "episodes": 2},
        },
        {
            **_leg("QB", ["P2"]),
            "checkout": "K2",
            "evidence_json": {"path": "/w/old", "memories": "x", "episodes": 2},
        },
    ],
}

_SHOWN_FILES = {"/w/one": {"id": "F1"}, "/w/two": "missing"}


def _shown() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [{"op": "pending"}]
    for checkout, lines in [
        ("K1", []),
        ("K1", [_Q]),
        ("K1", ["x", "x"]),
        ("K2", ["plain"]),
        ("K2", []),
        ("", []),
        (None, ["kept", "kept"]),
        ("KZ", ["z"]),
    ]:
        steps.append({"op": "first", "a": {"checkout": checkout, "lines": lines}})
    out = [_sequence("shown", "shown", steps, files=_SHOWN_FILES)]
    for fail in [["rows", 1], ["rows", 3]]:
        out.append(
            _sequence(
                f"shown_fail_{fail[1]}",
                "shown",
                [
                    {"op": "first", "a": {"checkout": "K1", "lines": []}},
                    {"op": "pending"},
                ],
                files=_SHOWN_FILES,
                fail=fail,
            )
        )
    return out


_ROOT_TABLES: dict[str, list[dict[str, Any]]] = {
    "projects": [_p("P1", "alpha")],
    "checkouts": [
        _k("K1", "P1"),
        _k("K2", "P1"),
        _k("K3", "P1"),
        _k("K4", "P1"),
        _k("K5", "P1"),
        _k("K6", "P1"),
        _k("K7", "P1"),
    ],
    "checkout_views": [
        _v("K1", "/w/one", 1),
        _v("K2", "/w/new", 2),
        _v("K2", "/w/old", 1),
        _v("K2", "/w/far", 3, "M2"),
        _v("K3", "/w/elsewhere", 1, "M2"),
        _v("K4", "/", 1),
        _v("K5", "C:\\W\\One", 1),
        _v("K6", "/w/a", 5),
        _v("K6", "/w/b", 5),
        _v("K7", "/w/one/", 1),
    ],
}


def _roots() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    dirs = [
        "/w/one",
        "/w/one/",
        "/w/one/sub",
        "/w/one/sub/../..",
        "/w/onex",
        "/w",
        "/W/ONE",
        "",
        "/w/link",
        "/w/new/x",
        "/w/old/x",
        "/x",
        "//w/one",
    ]
    for kid in ["K1", "K2", "K4", "K7"]:
        for d in dirs:
            steps.append({"op": "below", "a": {"k": kid, "dir": d, "canonical": True}})
    for kid, d in [("K3", "/w"), ("K6", "/w/b/c"), ("K6", "/w/a")]:
        steps.append({"op": "below", "a": {"k": kid, "dir": d, "canonical": True}})
    for kid in ["K1"]:
        for d in ["/w/link", "/w/up", "/w/one"]:
            a = {"k": kid, "dir": d, "canonical": False}
            steps.append({"op": "below", "a": a})
    nt_dirs = [
        "C:\\W\\One",
        "c:/w/one",
        "C:\\w\\one\\sub",
        "C:/link",
        "C:\\W",
        "D:\\w\\one",
    ]
    for d in nt_dirs:
        for canonical in [True, False]:
            a = {"k": "K5", "dir": d, "canonical": canonical, "nt": True}
            steps.append({"op": "below", "a": a})
    steps.append({"op": "below", "a": {"k": "KZ", "dir": "/w", "canonical": True}})
    for action in ["init", "init_new", "init_rebind", "join", "INIT", ""]:
        for kid, dd in [
            ("K1", "/w/one/sub"),
            ("K1", "/w/one"),
            ("K1", None),
            ("K1", "/x"),
        ]:
            a = {"k": kid, "dir": dd, "action": action}
            steps.append({"op": "declared", "a": a})
    for kid, d in [("K3", "/w"), ("K2", "/w/link"), ("K6", "/w/a"), ("K2", "/w/new/q")]:
        steps.append({"op": "declared", "a": {"k": kid, "dir": d, "action": "init"}})
    steps.append(
        {
            "op": "declared",
            "a": {"k": "K5", "dir": "c:\\w\\one\\x", "action": "init", "nt": True},
        }
    )
    return [_sequence("roots", "roots", steps)]


def _h(rid: str, project: Any, kind: Any = "memory") -> dict[str, Any]:
    return {
        "record_id": rid,
        "kind": kind,
        "project": project,
        "checkout": "K1",
        "rule": "file",
        "decided": "2026-01-01T00:00:00Z",
    }


_COUNT_TABLES: dict[str, list[dict[str, Any]]] = {
    "projects": [
        _p("P1", "alpha"),
        _p("P2", "beta", merged_into="P1"),
        _p("P3", "gamma"),
        _p("P4", "delta", merged_into="P2"),
        _p("P5", "empty"),
    ],
    "homes": [
        _h("R1", "P1"),
        _h("R2", "P2"),
        _h("R3", "P2", "episode"),
        _h("R4", "P4", "Memory"),
        _h("R5", "P3", 7),
        _h("R6", "P3"),
        _h("R7", "PX"),
        _h("R8", "global"),
        _h("R9", "P1", None),
    ],
}


def _counts() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {"op": "counts", "a": p}
        for p in ["P1", "P2", "P4", "P3", "P5", "PX", "", "global"]
    ]
    steps += [
        {"op": "by_project", "a": a}
        for a in [
            [],
            ["P1"],
            ["P2", "P4"],
            ["P3", "P1"],
            ["P5"],
            ["PX", "PY"],
            ["P1", "P3", "P5", "PX"],
            ["P4", "P3", "P2", "P1"],
        ]
    ]
    out = [_sequence("counts", "counts", steps)]
    odd = {
        "projects": [_p("P1", "alpha")],
        "homes": [{"record_id": "R1", "project": "P1"}],
    }
    _NAMED["no_kind"] = odd
    out.append(_sequence("counts_no_kind", "no_kind", [{"op": "counts", "a": "P1"}]))
    odd2 = {
        "projects": [_p("P1", "alpha")],
        "homes": [{"record_id": "R1", "kind": "memory"}, _h("R2", "P1")],
    }
    _NAMED["no_project"] = odd2
    out.append(
        _sequence("counts_no_project", "no_project", [{"op": "counts", "a": "P1"}])
    )
    for fail in [["rows", 1], ["rows", 2]]:
        out.append(
            _sequence(
                f"counts_fail_{fail[1]}",
                "counts",
                [{"op": "counts", "a": "P1"}, {"op": "by_project", "a": ["P3"]}],
                fail=fail,
            )
        )
    return out


_NAMED.update(
    settle=_SETTLE_TABLES,
    empty={},
    shown=_SHOWN_TABLES,
    roots=_ROOT_TABLES,
    counts=_COUNT_TABLES,
)


def generate() -> dict[str, bytes]:
    seqs = [*_settles(), *_shown(), *_roots(), *_counts()]
    header = {
        "seq": "header",
        "tables": {k: _spelled(v) for k, v in sorted(_NAMED.items())},
    }
    seqs.insert(0, header)
    return {"membership_legacy.jsonl": jsonl(seqs)}
