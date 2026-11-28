"""Fixtures: a record's provenance, unit membership-4 of `membership.py`:
`Membership.why` (1743-1814) and `_decides` (1846-1889).

The reference model runs over the recording store of
`gen_membership_queries` (`_Scoped`, built on `gen_membership_core`'s
`_Stub`), given a scripted membership log: each row ``[seq, kind,
payload]`` is answered as ``LogRow(seq, "t", None, kind, payload, "",
"")``, the rows of the kinds asked (every row where None) in the order
given, and the call is logged as ``["log", kinds, checkout]``. A payload
is the text the log holds, so invalid JSON, a list and a str are
written as they are.

One file, ``membership_why.jsonl``, not gated (ASCII names and ids). The
header names the tables (by table, then by variant) and the logs the
sequences start from; a log's entry that is an int is the base log's row
of that sequence number. Then a sequence per line ``{"seq", "tables",
"log", "fail", "steps"}`` (``tables`` names a variant per table), each
step ``{"op", "a", "o", "log"}``: ``why`` of a record id answers
``{"record_id", "project", "checkout", "rule", "rows", "text"}`` with the
rows as their sequence numbers in the order answered (or ``{"raised":
[class, text]}``, the text kept for KeyError and the store's error
only), and ``log`` holds the store calls it made; ``set`` replaces a
table's rows between two steps and calls nothing. The last line is the
digest of 1,000 seeded stores (`_store`, its pools written in the row):
four records asked of each, each answer and its store calls hashed in
turn.

A payload nested past the decoder's limit (RecursionError) is left to
the Rust robustness test: where `json.loads` stops recursing follows the
release and the system.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from bettermemory import membership as mm
from bettermemory.log import LogRow

from ._common import jsonl
from .gen_membership_core import _raised, _spelled
from .gen_membership_queries import _Scoped
from .gen_membership_rows import _PROJECT, _Lcg, dec, enc


class _WhyStub(_Scoped):
    """`_Scoped` with a scripted membership log."""

    def __init__(self, tables: Any, log: list[list[Any]], fail: Any) -> None:
        super().__init__(tables, fail, False)
        self.log_rows = log

    def membership_log(
        self, kinds: Any = None, *, since_seq: int = 0, checkout: Any = None
    ) -> list[LogRow]:
        self._note("log", None if kinds is None else list(kinds), checkout)
        out = []
        for seq, kind, payload in self.log_rows:
            if kinds is not None and kind not in kinds:
                continue
            if checkout is not None and _named_checkout(payload) != checkout:
                continue
            out.append(LogRow(seq, "t", None, kind, payload, "", ""))
        return out


def _named_checkout(payload: str) -> Any:
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value.get("checkout") if isinstance(value, dict) else None


def _why(w: mm.Why) -> dict[str, Any]:
    return {
        "record_id": w.record_id,
        "project": w.project,
        "checkout": w.checkout,
        "rule": w.rule,
        "rows": [r.seq for r in w.rows],
        "text": w.text,
    }


def _model(stub: _WhyStub) -> mm.Membership:
    return mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        now=lambda: datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc),
    )


#: The tables by table and variant, and the logs by name, that the
#: sequences start from, spelled once in the header line.
_TABLES: dict[str, dict[str, list[dict[str, Any]]]] = {}
_LOGS: dict[str, list[Any]] = {}


def _log_of(name: str) -> list[list[Any]]:
    by_seq = {r[0]: r for r in _BASE_LOG}
    return [by_seq[r] if isinstance(r, int) else r for r in _LOGS[name]]


def _sequence(
    name: str,
    tables: dict[str, str],
    log: str,
    steps: list[dict[str, Any]],
    *,
    fail: Any = None,
) -> dict[str, Any]:
    rows = {t: _TABLES[t][v] for t, v in tables.items()}
    stub = _WhyStub(rows, _log_of(log), fail)
    m = _model(stub)
    done = []
    for s in steps:
        op: str = s["op"]
        a: Any = s.get("a")
        start = len(stub.log)
        out: dict[str, Any] = {"op": op, "a": a}
        if op == "set":
            stub.tables[a[0]] = dec(a[1])
            out["o"] = None
        else:
            try:
                out["o"] = enc(_why(m.why(a)))
            except Exception as e:  # noqa: BLE001
                out["o"] = _raised(e)
        out["log"] = enc(stub.log[start:])
        done.append(out)
    return {"seq": name, "tables": tables, "log": log, "fail": fail, "steps": done}


def _p(pid: str, name: str, **kw: Any) -> dict[str, Any]:
    return {**_PROJECT, "id": pid, "name": name, **kw}


def _h(rid: str, project: Any, checkout: Any, rule: str = "file") -> dict[str, Any]:
    return {
        "record_id": rid,
        "kind": "memory",
        "project": project,
        "checkout": checkout,
        "rule": rule,
        "decided": "2026-01-01T00:00:00Z",
    }


def _q(qid: Any, checkout: Any, kind: str = "first_sight", **kw: Any) -> dict[str, Any]:
    return {
        "id": qid,
        "checkout": checkout,
        "kind": kind,
        "candidates_json": [],
        "evidence_json": {},
        "reason": "r",
        "line": "l",
        "state": "open",
        "answer": None,
        "answered_by": None,
        "signature_json": None,
        "opened": "2026-01-01T00:00:00",
        **kw,
    }


def _imp(qid: Any, evidence: Any) -> dict[str, Any]:
    return _q(qid, None, mm.IMPORT, evidence_json=evidence)


def _log(rows: list[tuple[str, Any]]) -> list[list[Any]]:
    """Rows numbered from 1; a payload given as a str is the log's text,
    any other value is written by `json.dumps`."""
    return [
        [i + 1, kind, p if isinstance(p, str) else json.dumps(p)]
        for i, (kind, p) in enumerate(rows)
    ]


_PROJECTS = [
    _p("P001", "alpha"),
    _p("P002", "beta", merged_into="P001"),
    _p("P003", "gamma", parent="P001"),
    _p("P004", "delta", merged_into="P002"),
    _p("P005", "epsilon", parent="P003"),
    _p("P006", "zeta"),
    _p("P007", "eta", merged_into="P006"),
    _p("P008", "toglobal", merged_into="global"),
    _p("P009", 'qu"ote'),
    _p("P010", "theta", merged_into="P005"),
    _p("P011", "loop1", merged_into="P012"),
    _p("P012", "loop2", merged_into="P011"),
]

_HOMES = [
    _h("R1", "P002", "K1"),
    _h("R2", "global", None, "outside"),
    _h("R3", "P005", "K5", "repo"),
    _h("R4", "P001", None),
    _h("R5", "PZ", "K1"),
    _h("R6", "P008", "K1"),
    _h("R7", "P009", "K7"),
    _h("R8", "P011", "K8"),
    _h("R9", "P006", ""),
    _h("R10", "P003", "K3"),
    _h("R11", "P001", "K1"),
    _h("R12", "P001", "K1"),
    _h("R13", "P006", "K13"),
    _h("R14", "P006", "K14"),
    _h("R15", "P006", "K15"),
    _h("R16", "P006", "K16"),
    _h("R17", "P006", "K17"),
    _h("R20", "global", "K1"),
]

_PROPOSALS = [
    _q("Q1", "K1"),
    _q("Q2", "K0"),
    _imp("Q3", {"imported_project": "P004"}),
    _imp("Q4", {"imported_project": "P006"}),
    _imp("Q5", {}),
    _imp("Q6", "P001"),
    _q("Q7", "KX"),
    _imp("Q8", {"imported_project": "P005"}),
    _imp("Q9", {"imported_project": "global"}),
    _q("Q10", "K5a", mm.IMPORT, evidence_json={"imported_project": "P001"}),
]

_BASE = {"projects": "base", "homes": "base", "proposals": "base", "scoped": "base"}

# Every branch of `_decides` for R1 (project P001 through P002, checkout
# K1, K0 and K00 retired into it), and the rows the other records read.
_BASE_LOG = _log(
    [
        ("project_minted", {"project": "P001", "name": "alpha", "reason": "new"}),
        ("project_minted", {"project": "P006"}),
        ("checkout_registered", {"checkout": "K0", "rule": "token"}),
        ("home_put", {"home": {"record_id": "R1", "checkout": "K0"}}),
        ("checkout_retired", {"checkout": "K0", "into": "K1"}),
        ("checkout_retired", {"checkout": "K00", "into": "K0"}),
        ("checkout_registered", {"checkout": "K1", "rule": "file"}),
        ("checkout_registered", {"checkout": "K1", "rule": "later"}),
        ("project_merged", {"project": "P002", "into": "P001"}),
        ("project_merged", {"project": "P004", "into": "P002"}),
        ("project_absorbed", {"project": "P007", "into": "P006"}),
        ("project_unmerged", {"project": "P006", "into": "P007", "was_into": "P001"}),
        ("project_minted", {"project": "P006", "was_into": "P001"}),
        ("project_file_bound", {"project": "P002", "file": "F1", "checkout": "K9"}),
        ("project_reparented", {"project": "P003", "into": "P001"}),
        ("project_minted", {"project": "P006", "checkout": "K1"}),
        ("checkout_put", {"checkout": {"id": "K1", "project": "P001"}}),
        ("checkout_put", {"checkout": {"id": "K2"}}),
        ("checkout_put", {"checkout": {"id": "Q1"}}),
        ("proposal_put", {"proposal": {"id": "Q3"}}),
        ("proposal_put", {"proposal": {"id": "Q4"}}),
        ("proposal_put", {"proposal": {"checkout": "K1", "id": "Q7"}}),
        ("proposal_put", {"proposal": {"id": "K00"}}),
        ("checkout_view_put", {"checkout_view": {"checkout": "K00", "path": "/w"}}),
        ("checkout_view_put", {"checkout_view": {"checkout": "K2"}}),
        ("checkout_view_put", {"checkout_view": {"checkout": ["K1"]}}),
        ("checkout_view_put", {"checkout_view": {"id": "K1"}, "checkout": "K1"}),
        ("project_put", {"project": {"id": "P004"}}),
        ("project_put", {"project": {"id": "P003"}}),
        ("project_put", {"project": {}, "checkout": "K1"}),
        ("project_file_put", {"project_file": {"project": "P001", "file_id": "F1"}}),
        ("project_file_put", {"project_file": {"project": "P006"}}),
        ("machine_put", {"machine": {"id": "M1"}, "checkout": "K1"}),
        ("checkout_put", {"checkout": "K1"}),
        ("project_put", {"project": "P001"}),
        ("home_put", {"home": {"record_id": "R2", "checkout": "K9"}}),
        ("home_put", {"home": {"checkout": "K1"}}),
        ("home_put", {"home": {"record_id": "R1", "checkout": "K9"}}),
        ("recall_suppressed", {"checkout": "K1", "surface": "s"}),
        ("witness_put", {"witness": {"project": "P001"}, "checkout": "K1"}),
        ("witness_put", {"witness": "w", "checkout": "K1"}),
        ("witness_released", {"project": "P001", "checkout": "K1"}),
        ("import_homed", {"record": "R1", "project": "P006"}),
        ("import_homed", {"record": "R2", "checkout": "K1"}),
        ("proposal_opened", {"proposal": "Q1", "checkout": "KX"}),
        ("proposal_answered", {"proposal": "Q3"}),
        ("proposal_answered", {"proposal": "Q4"}),
        ("proposal_opened", {"proposal": "Q7"}),
        ("checkout_moved", {"checkout": "K00"}),
        ("token_reminted", {"checkout": "KX"}),
        ("checkout_registered", "not json"),
        ("checkout_registered", '["K1"]'),
        ("checkout_registered", '"K1"'),
        ("checkout_moved", "5"),
        ("checkout_moved", "NaN"),
        ("checkout_moved", '{"checkout": "K1", "checkout": "KX"}'),
        ("checkout_moved", '{"checkout": "KX", "checkout": "K1"}'),
        ("_put", {"": {"id": "K1"}, "checkout": "K1"}),
        ("_put", {"checkout": "K1"}),
        ("checkout_registered", {"checkout": 5, "rule": "five"}),
        ("checkout_put", {"checkout": {"id": "K1x"}}),
        ("project_minted", {"project": "PZ9", "into": "P004"}),
        ("checkout_registered", {"rule": "nocheckout"}),
        ("checkout_registered", {"proposal": "Q3", "rule": "viaq"}),
        ("Checkout_Registered", {"checkout": "K1", "rule": "case"}),
        ("project_merged", {"project": "P008", "into": "global"}),
        # R3: P005, its chain and members; K5a retired into K5.
        ("checkout_retired", {"checkout": "K5a", "into": "K5"}),
        ("checkout_retired", {"checkout": "K5", "into": ""}),
        ("home_put", {"home": {"record_id": "R3", "checkout": "K6"}}),
        ("project_minted", {"project": "P010"}),
        ("checkout_registered", {"checkout": "K5", "rule": "file"}),
        # R4: no checkout, so a row naming none is its registration.
        ("home_put", {"home": {"record_id": "R4", "checkout": None}}),
        # R8: a cycle of merges.
        ("project_merged", {"project": "P012", "into": "P011"}),
        # R9: the checkout "".
        ("home_put", {"home": {"record_id": "R9", "checkout": ""}}),
        ("checkout_registered", {"checkout": "", "rule": "empty"}),
        # R10: K30 retired into K31, and K31 into K3.
        ("checkout_retired", {"checkout": "K31", "into": "K3"}),
        ("checkout_retired", {"checkout": "K30", "into": "K31"}),
        ("home_put", {"home": {"record_id": "R10", "checkout": "K30"}}),
        # R11 and R12: a first home that is not a str, true and false.
        ("home_put", {"home": {"record_id": "R11", "checkout": 5}}),
        ("home_put", {"home": {"record_id": "R12", "checkout": 0}}),
        ("home_put", {"home": {"record_id": "R12", "checkout": "K9"}}),
        # R13 to R17: the registration's rule of other types.
        ("checkout_registered", {"checkout": "K13", "rule": ["a", 1]}),
        ("checkout_registered", {"checkout": "K14", "rule": 0}),
        ("checkout_registered", {"checkout": "K14", "rule": "second"}),
        ("checkout_registered", {"checkout": "K15", "rule": {"a": 1.5}}),
        ("checkout_registered", {"checkout": "K16", "rule": True}),
        ("checkout_registered", {"checkout": "K17", "rule": 2.5e20}),
        ("checkout_registered", {"checkout": "K17", "rule": None}),
    ]
)

_ASKED = [
    "R1",
    "R2",
    "R3",
    "R4",
    "R5",
    "R6",
    "R7",
    "R8",
    "R9",
    "R10",
    "R11",
    "R12",
    "R13",
    "R14",
    "R15",
    "R16",
    "R17",
    "R20",
    "R0",
]


def _why_steps(ids: list[str]) -> list[dict[str, Any]]:
    return [{"op": "why", "a": rid} for rid in ids]


def _odd(rows: list[tuple[str, Any]], *, drop: tuple[int, ...] = ()) -> str:
    """A log of the base log's first ten rows, which decide R1, but those
    of `drop`, and `rows` after them, named by its index."""
    name = f"odd{len(_LOGS)}"
    _LOGS[name] = [i for i in range(1, 11) if i not in drop] + [
        [100 + i, kind, p if isinstance(p, str) else json.dumps(p)]
        for i, (kind, p) in enumerate(rows)
    ]
    return name


def _sequences() -> list[dict[str, Any]]:  # noqa: PLR0915
    _TABLES.clear()
    _LOGS.clear()
    _TABLES.update(
        {
            "projects": {"base": _PROJECTS},
            "homes": {"base": _HOMES},
            "proposals": {"base": _PROPOSALS},
            "scoped": {"base": [{"id": "R2", "scopes": ["projects:alpha"]}]},
        }
    )
    _LOGS["base"] = _BASE_LOG
    _LOGS["none"] = []
    # An unhashable value where `_decides` asks a set, and a list where
    # it asks the checkouts' list (which compares and never raises).
    raising = [
        ("checkout_put", {"checkout": {"id": ["K1"]}}),
        ("proposal_put", {"proposal": {"id": {"a": 1}}}),
        ("project_put", {"project": {"id": ["P001"]}}),
        ("project_file_put", {"project_file": {"project": {"p": 1}}}),
        ("project_minted", {"project": ["P001"]}),
        ("project_merged", {"project": "PZZ", "into": ["P001"]}),
        ("project_unmerged", {"project": "PZZ", "into": "PZZ", "was_into": ["x"]}),
        ("checkout_moved", {"checkout": "KQ", "proposal": ["Q1"]}),
    ]
    sparing = [
        ("checkout_put", {"checkout": {"id": "K1"}, "x": ["K1"]}),
        ("checkout_view_put", {"checkout_view": {"checkout": {"a": 1}}}),
        ("project_minted", {"project": "P001", "into": ["x"]}),
        ("project_minted", {"project": "PZZ", "into": "PZZ", "was_into": ["x"]}),
        ("checkout_moved", {"checkout": "K1", "proposal": ["Q1"]}),
        ("checkout_moved", {"checkout": ["K1"]}),
        ("home_put", {"home": {"record_id": ["R1"]}}),
        ("import_homed", {"record": ["R1"]}),
        ("recall_suppressed", {"proposal": ["Q1"]}),
        ("project_put", {"project": ["P001"], "checkout": "K1"}),
    ]
    out = [
        _sequence("base", _BASE, "base", _why_steps(_ASKED)),
        _sequence("empty", {}, "none", _why_steps(["R1"])),
        _sequence("no_log", _BASE, "none", _why_steps(["R1", "R2", "R4"])),
        _sequence("sparing", _BASE, _odd(sparing), _why_steps(["R1", "R4"])),
    ]
    for i, row in enumerate(raising):
        out.append(
            _sequence(f"unhashable{i}", _BASE, _odd([row]), _why_steps(["R1", "R2"]))
        )
    # A home_put kept by the checkout or the proposal it names whose
    # ``home`` is missing or not a mapping: `p["home"].get` raises, after
    # every home_put is read; a global record never reads them.
    home_puts: list[list[tuple[str, Any]]] = [
        [("home_put", {"checkout": "K1"})],
        [("home_put", {"home": "R1", "checkout": "K1"})],
        [("home_put", {"home": None, "proposal": "Q1"})],
        [("home_put", {"home": ["R1"], "checkout": "K0"})],
        [
            ("home_put", {"home": {"record_id": "R1", "checkout": "K0"}}),
            ("home_put", {"home": 5, "checkout": "K1"}),
        ],
    ]
    for i, rows in enumerate(home_puts):
        out.append(
            _sequence(f"home_put{i}", _BASE, _odd(rows), _why_steps(["R1", "R20"]))
        )
    # The first home is unhashable where it is looked up in the folded
    # checkouts; an empty mapping is false and reads as the checkout.
    for i, home in enumerate([["K9"], {"a": 1}, {}, [], "K00", "K1"]):
        rows = [("home_put", {"home": {"record_id": "R1", "checkout": home}})]
        log = _odd(rows, drop=(4,))
        out.append(_sequence(f"at{i}", _BASE, log, _why_steps(["R1"])))
    # A folded row without its checkout, and the import questions' rows.
    out.append(
        _sequence(
            "folded_keyerror",
            _BASE,
            _odd([("checkout_retired", {"into": "K1"})]),
            _why_steps(["R1", "R4", "R2"]),
        )
    )
    _TABLES["proposals"].update(
        {
            "bad": [_imp("Q1", {"imported_project": ["P001"]})],
            "noev": [{"id": "Q1", "kind": mm.IMPORT, "checkout": None}],
            "noid": [
                {"kind": mm.IMPORT, "checkout": None, "evidence_json": {"x": 1}},
                {"kind": mm.IMPORT, "evidence_json": {"imported_project": "P001"}},
            ],
            "noid_k": [{"kind": "first_sight", "checkout": "K1"}],
            "ids": [
                _q(5, "K1"),
                _imp(None, {"imported_project": "P002"}),
                _imp("Q9", {"imported_project": 7}),
            ],
        }
    )
    for v in ["bad", "noev", "noid", "noid_k", "ids"]:
        tables = {**_BASE, "proposals": v}
        out.append(
            _sequence(f"proposals_{v}", tables, "base", _why_steps(["R1", "R6"]))
        )
    # The store fails at each of its reads in turn.
    for fail in [
        ["rows", 1],
        ["scopes", 1],
        ["rows", 2],
        ["log", 1],
        ["rows", 3],
        ["rows", 4],
        ["log", 2],
    ]:
        out.append(
            _sequence(
                f"fail_{fail[0]}{fail[1]}", _BASE, "base", _why_steps(["R1"]), fail=fail
            )
        )
    out.append(
        _sequence("fail_global", _BASE, "base", _why_steps(["R20"]), fail=["rows", 2])
    )
    # Each call reads the projects and the homes anew.
    moved = [
        *_PROJECTS[:3],
        _p("P004", "delta"),
        *_PROJECTS[4:],
        _p("P013", "iota", merged_into="P003"),
    ]
    rehomed = [_h("R1", "P013", "K0"), *_HOMES[1:]]
    out.append(
        _sequence(
            "fresh",
            _BASE,
            "base",
            [
                {"op": "why", "a": "R1"},
                {"op": "set", "a": ["projects", enc(moved)]},
                {"op": "why", "a": "R1"},
                {"op": "set", "a": ["homes", enc(rehomed)]},
                {"op": "why", "a": "R1"},
            ],
        )
    )
    # A home without a checkout (R4): the folded checkouts are the empty
    # set, and `at in folded` still hashes the first home, so a list or a
    # dict raises TypeError there; a str is homed now at None.
    for i, home in enumerate([["K9"], {"a": 1}, "K9"]):
        rows = [("home_put", {"home": {"record_id": "R4", "checkout": home}})]
        out.append(_sequence(f"at_none{i}", _BASE, _odd(rows), _why_steps(["R4"])))
    # A log that answers its rows out of sequence order: the rows, the
    # first home and the registration follow the order answered.
    _LOGS["unordered"] = [
        [seq, kind, json.dumps(p)]
        for seq, kind, p in [
            (9, "checkout_moved", {"checkout": "K1"}),
            (3, "checkout_registered", {"checkout": "K1", "rule": "b"}),
            (2, "checkout_registered", {"checkout": "K1", "rule": "a"}),
            (5, "project_minted", {"project": "P001"}),
            (8, "checkout_retired", {"checkout": "K0", "into": "K1"}),
            (7, "home_put", {"home": {"record_id": "R1", "checkout": "K0"}}),
            (4, "home_put", {"home": {"record_id": "R1", "checkout": "K2"}}),
            (1, "checkout_moved", {"checkout": "K0"}),
            (6, "home_put", {"home": {"record_id": "R4", "checkout": "K2"}}),
        ]
    ]
    out.append(_sequence("unordered", _BASE, "unordered", _why_steps(["R1", "R4"])))
    return out


# ---------------------------------------------------------------------------
# The digest over seeded stores
# ---------------------------------------------------------------------------

_SEED = 20261127
_STORES = 1000
_SWEEP_ASKED = ["R1", "R2", "R3", "R4"]
_NAMES = ["alpha", "beta", "gamma", 'q"t']
_CIDS = ["K1", "K2", None]
_IDS = ['"K1"', '"K2"', '"K3"', '"K4"', '"Q1"', '"Q2"', '"P001"', '"P002"']
_PROJECT_IDS = ['"P001"', '"P002"', '"P003"', '"global"', '"PX"', "null"]
#: The values a payload's key takes, by key; an unhashable one in 40.
_POOLS = {
    "checkout": ['"K1"', '"K2"', '"K3"', '"K3"', "null", '""'],
    "proposal": ['"Q1"', '"Q2"', '"Q3"', "null", "5"],
    "project": _PROJECT_IDS,
    "into": [*_PROJECT_IDS, '"K1"', '"K2"', '"K3"', '"K1"', '"K2"', '""'],
    "was_into": _PROJECT_IDS,
    "record": ['"R1"', '"R2"', '"R3"'],
    "rule": ['"file"', '""', "0", "1.5", '["a"]', "null", "true"],
    "record_id": ['"R1"', '"R2"', '"R3"'],
    "id": _IDS,
}
_ODD_VALUES = ['["K1"]', '{"id": "K1"}', "{}", "[]"]
#: The kinds a row takes; the three `why`'s text reads come thrice.
_KINDS = [
    "home_put",
    "home_put",
    "home_put",
    "checkout_registered",
    "checkout_registered",
    "checkout_retired",
    "checkout_retired",
    "checkout_put",
    "proposal_put",
    "checkout_view_put",
    "project_put",
    "project_file_put",
    "machine_put",
    "_put",
    "recall_suppressed",
    "witness_put",
    "witness_released",
    "import_homed",
    "project_minted",
    "project_merged",
    "project_absorbed",
    "project_unmerged",
    "project_file_bound",
    "project_reparented",
    "checkout_registered",
    "checkout_retired",
    "proposal_opened",
    "checkout_moved",
]
_KEYS = ["checkout", "proposal", "project", "into", "was_into", "record", "rule"]
_BODY_KEYS = ["record_id", "id", "checkout", "project"]
_EVIDENCE = [
    '{"imported_project": "P001"}',
    '{"imported_project": "P002"}',
    '{"imported_project": "P003"}',
    '{"imported_project": "PX"}',
    '{"imported_project": "global"}',
    '{"imported_project": null}',
    "{}",
    '"P001"',
    "null",
    "[]",
    '{"other": "P001"}',
    '{"imported_project": 5}',
    '{"imported_project": "P001", "n": 1}',
    '{"imported_project": "P002", "n": 2}',
    '{"imported_project": "P003", "n": 3}',
    '{"imported_project": ["P001"]}',
]


def _value(rng: _Lcg, key: str) -> str:
    if rng.next(40) == 0:
        return _ODD_VALUES[rng.next(len(_ODD_VALUES))]
    pool = _POOLS[key]
    return pool[rng.next(len(pool))]


def _body(rng: _Lcg) -> str:
    if rng.next(6) == 0:
        return _value(rng, "id")
    parts = []
    for key in _BODY_KEYS:
        if rng.next(2) == 0:
            parts.append(f'"{key}": {_value(rng, key)}')
    return "{" + ", ".join(parts) + "}"


def _payload(rng: _Lcg, kind: str) -> str:
    shape = rng.next(24)
    if shape == 0:
        return "{"
    if shape == 1:
        return '["K1"]'
    if shape == 2:
        return '"K1"'
    parts = []
    if kind.endswith("_put") and rng.next(5) != 0:
        parts.append(f'"{kind[:-4]}": {_body(rng)}')
    retired = kind == "checkout_retired"
    for key in _KEYS:
        if (retired and key in ("checkout", "into")) or rng.next(2) == 0:
            pool = "checkout" if retired and key == "into" else key
            parts.append(f'"{key}": {_value(rng, pool)}')
    return "{" + ", ".join(parts) + "}"


def _store(rng: _Lcg) -> tuple[dict[str, list[dict[str, Any]]], list[list[Any]]]:
    n = 1 + rng.next(5)
    ids = [f"P{i:03d}" for i in range(1, n + 1)]
    links = [None, None, None, None, None, "global", *ids]
    parents = [*links, "PX"]
    projects = [
        _p(
            pid,
            _NAMES[rng.next(len(_NAMES))],
            merged_into=links[rng.next(len(links))],
            parent=parents[rng.next(len(parents))],
        )
        for pid in ids
    ]
    homed = [*ids, *ids, *ids, "global", "PX"]
    homes = []
    for rid in _SWEEP_ASKED[:3]:
        if rng.next(4) == 0:
            continue
        project = homed[rng.next(len(homed))]
        homes.append(_h(rid, project, _CIDS[rng.next(len(_CIDS))], f"r{rng.next(3)}"))
    proposals = []
    for i in range(rng.next(5)):
        kind = ["first_sight", mm.IMPORT][rng.next(2)]
        checkout = _CIDS[rng.next(len(_CIDS))]
        evidence = json.loads(_EVIDENCE[rng.next(len(_EVIDENCE))])
        proposals.append(_q(f"Q{i + 1}", checkout, kind, evidence_json=evidence))
    log = []
    for i in range(rng.next(20)):
        kind = _KINDS[rng.next(len(_KINDS))]
        log.append([i + 1, kind, _payload(rng, kind)])
    tables = {"projects": projects, "homes": homes, "proposals": proposals}
    return tables, log


def _t(v: Any) -> str:
    return "~" if v is None else str(v)


def _calls(log: list[Any]) -> str:
    return json.dumps(enc(log), sort_keys=True, separators=(",", ":"))


def _digest() -> dict[str, Any]:
    rng = _Lcg(_SEED)
    h = hashlib.sha256()
    count = 0
    raised: dict[str, int] = {}
    for _ in range(_STORES):
        tables, log = _store(rng)
        stub = _WhyStub(tables, log, None)
        m = _model(stub)
        for rid in _SWEEP_ASKED:
            start = len(stub.log)
            try:
                w = m.why(rid)
                seqs = ",".join(str(r.seq) for r in w.rows)
                line = (
                    f"{rid}|{_t(w.project)}|{_t(w.checkout)}|{_t(w.rule)}|{seqs}|"
                    f"{w.text}"
                )
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                raised[name] = raised.get(name, 0) + 1
                line = f"{rid}|raised|{name}"
            h.update(f"{line}|{_calls(stub.log[start:])}\n".encode())
            count += 1
    return {
        "seq": "digest",
        "seed": _SEED,
        "stores": _STORES,
        "asked": _SWEEP_ASKED,
        "names": _NAMES,
        "cids": _CIDS,
        "pools": _POOLS,
        "odd": _ODD_VALUES,
        "kinds": _KINDS,
        "keys": _KEYS,
        "body_keys": _BODY_KEYS,
        "evidence": _EVIDENCE,
        "raised": dict(sorted(raised.items())),
        "count": count,
        "sha256": h.hexdigest(),
    }


def generate() -> dict[str, bytes]:
    sequences = _sequences()
    header = {
        "seq": "header",
        "tables": {t: _spelled(v) for t, v in sorted(_TABLES.items())},
        "logs": dict(sorted(_LOGS.items())),
    }
    return {"membership_why.jsonl": jsonl([header, *sequences, _digest()])}
