"""Fixtures: the membership model, unit membership-1 (`membership.py`):
the state and plumbing of `Membership` (`__init__`, `reads_here`,
`_call`, `_stamp`, `_put`, `_delete`, `_event`), its projects
(`_all_projects`, `project`, `projects`, `canonical`, `_canon`, `named`,
`chain_of`, `_members`, `_members_index`, `_name`, `_put_project`,
`_mint_project`), the witness and file-binding reads (`_witness_query`,
`_witness_rows`, `witness_commits`, `_has_witness`, `_has_witnesses`,
`_witnessed`, `file_binding`, `_file_binding`) and the checkout reads
(`_checkouts`, `checkout`, `checkouts`, `_checkouts_of`,
`_checkouts_by_project`, `_put_checkout`).

The reference model runs over `_Stub`, a recording store: canned table
rows, every call logged, a batch rolled back on an exception, and a
failure injected at a chosen call. The Rust test drives its own fake by
the same rules.

Two files, one JSON object per line, keys sorted, ASCII, values spelled
as `gen_membership_rows.enc` spells them:

- ``membership_core.jsonl``, not gated. A sequence line ``{"seq", "init",
  "tables", "fail", "outer", "steps"}``: each step ``{"op", "a", "o",
  "log", "rows"}`` with what the step answered (or ``{"raised": [class,
  text]}``, the text kept for KeyError and the store's error only), the
  store calls it made and the model's collected row numbers after it; a
  ``call`` step holds its inner steps under ``inner``. The last line is a
  digest of `canonical`, `chain_of`, `named` and `_members` over seeded
  merge graphs.
- ``membership_core_unicode.jsonl``, gated on the Unicode version:
  `named` over names past ASCII, which `str.casefold` maps.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import membership as mm
from bettermemory import membership_rows as rows

from ._common import jsonl
from .gen_membership_rows import _CHECKOUT, _PROJECT, _VIEW, _Lcg, dec, enc


class StubError(Exception):
    pass


class _W:
    def __init__(self, seq: int) -> None:
        self.seq = seq


#: kind -> (table, key columns)
_KINDS = {
    "project": ("projects", ("id",)),
    "checkout": ("checkouts", ("id",)),
    "project_file": ("project_files", ("file_id",)),
    "project_witness": ("project_witnesses", ("project", "commit_id")),
    "project_blob": ("project_blobs", ("project", "blob")),
    "project_remote": ("project_remotes", ("project", "remote_key")),
    "checkout_view": ("checkout_views", ("checkout", "machine", "path")),
}


def _cmp_key(v: Any) -> Any:
    return (isinstance(v, str), v if not isinstance(v, str) else v)


class _Stub:
    def __init__(self, tables: dict[str, list[dict[str, Any]]], fail: Any, outer: bool):
        self.tables = copy.deepcopy(tables)
        self.log: list[Any] = []
        self.seq = 0
        self.depth = 0
        self.outer = outer
        self.fail = fail
        self.calls: dict[str, int] = {}

    def _note(self, method: str, *args: Any) -> None:
        self.log.append([method, *args])
        self.calls[method] = self.calls.get(method, 0) + 1
        if self.fail is not None and self.fail == [method, self.calls[method]]:
            raise StubError("injected")

    @property
    def in_batch(self) -> bool:
        return self.outer or self.depth > 0

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        self.log.append(["batch"])
        snapshot = (copy.deepcopy(self.tables), self.seq)
        self.depth += 1
        try:
            yield
        except BaseException:
            self.tables, self.seq = snapshot
            self.log.append(["rollback"])
            raise
        else:
            self.log.append(["commit"])
        finally:
            self.depth -= 1

    def _written(self) -> _W:
        self.seq += 1
        return _W(self.seq)

    def put_membership(self, kind: str, row: Any, *, session: Any = None) -> _W:
        self._note("put", kind, dict(row), session)
        table, key = _KINDS[kind]
        rows_ = self.tables.setdefault(table, [])
        for i, r in enumerate(rows_):
            if all(r.get(k) == row[k] for k in key):
                rows_[i] = dict(row)
                break
        else:
            rows_.append(dict(row))
        return self._written()

    def delete_membership(self, kind: str, key: Any, *, session: Any = None) -> _W:
        self._note("delete", kind, dict(key), session)
        table, cols = _KINDS[kind]
        self.tables[table] = [
            r
            for r in self.tables.get(table, [])
            if not all(r.get(c) == key[c] for c in cols)
        ]
        return self._written()

    def record_membership(self, kind: str, payload: Any, *, session: Any = None) -> _W:
        self._note("record", kind, payload, session)
        self.tables.setdefault("events", []).append({"kind": kind, "payload": payload})
        return self._written()

    def membership_rows(
        self,
        table: str,
        *,
        where: Any = None,
        column_in: Any = None,
        order_by: Any = (),
    ) -> list[dict[str, Any]]:
        spelled_in = None
        if column_in is not None:
            spelled_in = [column_in[0], sorted(column_in[1], key=_cmp_key)]
        self._note("rows", table, where, spelled_in, list(order_by))
        out = [
            dict(r)
            for r in self.tables.get(table, [])
            if (where is None or all(r.get(k) == v for k, v in where.items()))
            and (column_in is None or r.get(column_in[0]) in list(column_in[1]))
        ]
        for col in reversed(list(order_by)):
            out.sort(key=lambda r: r[col])
        return out

    def next_membership_id(self, table: str, prefix: str) -> str:
        self._note("next_id", table, prefix)
        high = 0
        for r in self.tables.get(table, []):
            rest = str(r.get("id"))[len(prefix) :]
            if (
                str(r.get("id")).startswith(prefix)
                and rest.isascii()
                and rest.isdigit()
            ):
                high = max(high, int(rest))
        return f"{prefix}{high + 1:03d}"

    def next_membership_number(self, table: str, column: str) -> int:
        self._note("next_number", table, column)
        return 1 + max((int(r[column]) for r in self.tables.get(table, [])), default=0)


def _raised(e: BaseException) -> dict[str, Any]:
    kept = isinstance(e, (KeyError, StubError))
    return {"raised": [type(e).__name__, str(e) if kept else None]}


def _project(p: mm.Project) -> Any:
    return rows._project_row(p)


def _view(v: mm.View) -> Any:
    return {
        "checkout": v.checkout,
        "machine": v.machine,
        "path": v.path,
        "first_seen": v.first_seen,
        "last_seen": v.last_seen,
        "order": v.order,
    }


def _checkout(k: mm.Checkout) -> Any:
    return [rows._checkout_row(k), [_view(v) for v in k.views]]


class _Boom(RuntimeError):
    pass


def _answer(m: mm.Membership, op: str, a: Any) -> Any:  # noqa: C901, PLR0911, PLR0912
    if op == "reads_here":
        return m.reads_here
    if op == "stamp":
        return m._stamp()
    if op == "project":
        return _project(m.project(a))
    if op == "projects":
        return [_project(p) for p in m.projects()]
    if op == "canonical":
        return m.canonical(a)
    if op == "canon":
        return m._canon(a)
    if op == "named":
        return sorted(m.named(a))
    if op == "chain_of":
        return m.chain_of(a)
    if op == "members":
        return m._members(a)
    if op == "members_index":
        return [[k, list(v)] for k, v in sorted(m._members_index().items())]
    if op == "witnessed":
        return sorted(m._witnessed())
    if op == "witness_commits":
        return m.witness_commits(a)
    if op == "has_witness":
        return m._has_witness(a[0], a[1])
    if op == "has_witnesses":
        return m._has_witnesses(a)
    if op == "file_binding":
        return m.file_binding(a)
    if op == "file_binding_read":
        return m._file_binding(a)
    if op == "checkout":
        return _checkout(m.checkout(a))
    if op == "checkouts":
        return [_checkout(k) for k in m.checkouts(token=a)]
    if op == "checkouts_of":
        return [_checkout(k) for k in m._checkouts_of(a)]
    if op == "checkouts_by_project":
        got = m._checkouts_by_project(a[0], machine=a[1])
        return [[k, [_checkout(x) for x in v]] for k, v in sorted(got.items())]
    if op == "name":
        return m._name(a)
    if op == "put_project":
        m._put_project(rows._project_from(dec(a)))
        return None
    if op == "mint_project":
        return _project(
            m._mint_project(a["name"], **{k: v for k, v in a.items() if k != "name"})
        )
    if op == "put_checkout":
        return _checkout(m._put_checkout(rows._checkout_from(dec(a), [])))
    if op == "put":
        m._put(a[0], dec(a[1]))
        return None
    if op == "delete":
        m._delete(a[0], dec(a[1]))
        return None
    if op == "event":
        m._event(a[0], **dec(a[1]))
        return None
    raise ValueError(op)


def _step(m: mm.Membership, stub: _Stub, step: dict[str, Any]) -> dict[str, Any]:
    op: str = step["op"]
    a: Any = step.get("a")
    start = len(stub.log)
    out: dict[str, Any] = {"op": op, "a": a}
    if op == "call":
        inner: list[dict[str, Any]] = []
        out["inner"] = inner
        try:
            with m._call():
                for s in a["ops"]:
                    got = _step(m, stub, s)
                    inner.append(got)
                    if isinstance(got["o"], dict) and "raised" in got["o"]:
                        raise got.pop("_exc")
                if a.get("raise"):
                    raise _Boom("boom")
            out["o"] = None
        except Exception as e:  # noqa: BLE001
            out["o"] = _raised(e)
        for s in inner:
            s.pop("_exc", None)
    else:
        try:
            out["o"] = enc(_answer(m, op, a))
        except Exception as e:  # noqa: BLE001
            out["o"] = _raised(e)
            out["_exc"] = e
    out["log"] = enc(stub.log[start:])
    out["rows"] = list(m._rows)
    return out


def _now(spec: Any) -> datetime:
    text, minutes = spec
    return datetime.fromisoformat(text).replace(
        tzinfo=timezone(timedelta(minutes=minutes))
    )


def _given(k: Any, v: Any) -> str:
    return "live"


def _sequence(
    name: str,
    tables: dict[str, list[dict[str, Any]]],
    steps: list[dict[str, Any]],
    *,
    fail: Any = None,
    outer: bool = False,
    init: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init = {
        "now": ["2026-03-04T05:06:07.123456", 60],
        "session": None,
        "given": False,
    } | (init or {})
    stub = _Stub(tables, fail, outer)
    given: Any = _given if init["given"] else None
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        liveness=given,
        now=lambda: _now(init["now"]),
        session=init["session"],
    )
    done = []
    for s in steps:
        got = _step(m, stub, s)
        got.pop("_exc", None)
        done.append(got)
    return {
        "seq": name,
        "init": init,
        "tables": "base" if tables is _TABLES else _spelled(tables),
        "fail": fail,
        "outer": outer,
        "steps": done,
        "after": _sha(_spelled(stub.tables)),
    }


def _spelled(tables: dict[str, list[dict[str, Any]]]) -> Any:
    return {t: enc(rs) for t, rs in sorted(tables.items())}


def _sha(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _p(pid: str, name: str, **kw: Any) -> dict[str, Any]:
    return {**_PROJECT, "id": pid, "name": name, **kw}


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


_HEX40 = "0123456789abcdef" * 2 + "01234567"
_HEX64 = "0123456789abcdef" * 4


def _w(project: str, commit: Any, arrival: int) -> dict[str, Any]:
    return {"project": project, "commit_id": commit, "arrival": arrival}


_TABLES: dict[str, list[Any]] = {
    "projects": [
        _p("P001", "alpha"),
        _p("P002", "beta", merged_into="P001"),
        _p("P003", "gamma", parent="P001"),
        _p("P004", "delta", merged_into="P005"),
        _p("P005", "epsilon", merged_into="P004"),
        _p("P006", "Alpha"),
        _p("P007", "self", parent="P007"),
        _p("P008", "toglobal", merged_into="global"),
        _p("P009", "orphan", merged_into="PX", parent="P003"),
        _p("P010", "child", parent="P002"),
    ],
    "checkouts": [
        _k("K1", "P001"),
        _k("K2", "P002", machine="M2", kind="linked", token="tok2"),
        _k("K3", "P001", kind="unresolved"),
        _k("K4", "P003", token="tok2"),
        _k("K5", "PZ"),
        _k("K6", "P004"),
    ],
    "checkout_views": [
        _v("K1", "/work/a", 2),
        _v("K2", "/work/b", 1, "M2"),
        _v("K1", "/work/a2", 1),
        _v("K9", "/work/z", 1),
    ],
    "project_witnesses": [
        _w("P001", _HEX40, 2),
        _w("P002", _HEX40.upper()[:-1] + "9", 1),
        _w("P001", "abc123", 3),
        _w("P003", _HEX64, 4),
        _w("P003", _HEX40 + "0", 5),
        _w("P006", 10**39, 6),
        _w("P005", _HEX40[:-1], 7),
        _w("P004", _HEX64[:-1] + "g", 8),
    ],
    "project_files": [
        {"file_id": "F1", "project": "P002", "evidence": "init"},
        {"file_id": "F2", "project": "PZ", "evidence": "init"},
    ],
}

_PIDS = [
    "P001",
    "P002",
    "P003",
    "P004",
    "P005",
    "P007",
    "P008",
    "P009",
    "P010",
    "PX",
    "global",
    "",
]


def _reads() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = [{"op": "reads_here"}, {"op": "stamp"}]
    s += [{"op": "project", "a": a} for a in ["P001", "P002", "PX", ""]]
    s += [{"op": "projects"}]
    s += [{"op": "canonical", "a": a} for a in [*_PIDS, None]]
    s += [{"op": "canon", "a": a} for a in _PIDS]
    s += [{"op": "chain_of", "a": a} for a in [*_PIDS, None]]
    s += [{"op": "members", "a": a} for a in _PIDS]
    s += [{"op": "members_index"}]
    scopes = [
        [],
        ["projects:"],
        ["projects:alpha"],
        ["projects:ALPHA", "x"],
        ["project:alpha"],
        ["projects:beta"],
        ["projects:delta", "projects:self"],
        ["Projects:alpha"],
        ["projects:nope"],
        ["projects:toglobal", "projects:orphan"],
    ]
    s += [{"op": "named", "a": a} for a in scopes]
    s += [{"op": "witnessed"}]
    s += [
        {"op": "witness_commits", "a": a}
        for a in [None, "P001", "P002", "P003", "P004", "PX"]
    ]
    s += [
        {"op": "has_witness", "a": a}
        for a in [
            ["P001", _HEX40.upper()],
            ["P002", _HEX40[:-1] + "9"],
            ["P001", "abc123"],
            ["P003", _HEX64.upper()],
            ["PX", _HEX40],
        ]
    ]
    s += [{"op": "has_witnesses", "a": a} for a in ["P001", "P005", "P006", "P007"]]
    s += [{"op": "file_binding", "a": a} for a in ["F1", "F2", "F3"]]
    s += [{"op": "checkout", "a": a} for a in ["K1", "K3", "KX"]]
    s += [{"op": "checkouts", "a": a} for a in [None, "tok2", "none"]]
    s += [{"op": "checkouts_of", "a": a} for a in ["P001", "P002", "P004", "PX"]]
    s += [
        {"op": "checkouts_by_project", "a": a}
        for a in [
            [["P002", "P003", "PX"], None],
            [["P001"], "M1"],
            [["P001"], "M2"],
            [["P001"], ""],
            [[], None],
            [["PX"], None],
            [["P004", "P005"], None],
        ]
    ]
    s += [{"op": "name", "a": a} for a in ["P002", "P001", "PX", "P008"]]
    return s


def _cache() -> list[dict[str, Any]]:
    new = enc(rows._project_row(rows._project_from(_p("P020", "late"))))
    return [
        {"op": "project", "a": "P001"},
        {"op": "members", "a": "P001"},
        {"op": "put", "a": ["project", new]},
        {"op": "project", "a": "P020"},
        {"op": "file_binding_read", "a": "F1"},
        {"op": "project", "a": "P020"},
        {"op": "file_binding", "a": "F1"},
        {"op": "project", "a": "P020"},
        {"op": "members", "a": "P001"},
        {"op": "put_project", "a": enc(_p("P002", "beta"))},
        {"op": "members", "a": "P001"},
        {"op": "members", "a": "P002"},
        {"op": "canonical", "a": "P010"},
        {"op": "chain_of", "a": "P010"},
        {"op": "call", "a": {"ops": [{"op": "members", "a": "P001"}]}},
    ]


def _writes() -> list[dict[str, Any]]:
    mint = {
        "name": "fresh",
        "reason": "new",
        "provisional": None,
        "parent": "P001",
        "tenant": "T1",
        "folder": True,
        "boundary": False,
    }
    ops = [
        {"op": "mint_project", "a": mint},
        {"op": "mint_project", "a": {**mint, "name": "edge", "boundary": True}},
        {"op": "project", "a": "P011"},
        {"op": "projects"},
        {"op": "put_checkout", "a": enc(_k("K7", "P011"))},
        {"op": "checkouts_of", "a": "P011"},
        {
            "op": "event",
            "a": ["project_merged", {"$d": [["a", {"$t": [1, 2]}], ["b", None]]}],
        },
        {"op": "delete", "a": ["checkout", {"$d": [["id", "K1"]]}]},
        {"op": "checkout", "a": "K1"},
        {"op": "stamp"},
    ]
    return [
        {"op": "call", "a": {"ops": ops}},
        {"op": "project", "a": "P012"},
        {"op": "call", "a": {"ops": [{"op": "projects"}]}},
    ]


def _rollback() -> list[dict[str, Any]]:
    mint = {"name": "gone", "reason": "new"}
    return [
        {
            "op": "call",
            "a": {"ops": [{"op": "mint_project", "a": mint}], "raise": True},
        },
        {"op": "project", "a": "P011"},
        {
            "op": "call",
            "a": {
                "ops": [{"op": "mint_project", "a": mint}, {"op": "project", "a": "PX"}]
            },
        },
        {"op": "call", "a": {"ops": [{"op": "mint_project", "a": mint}]}},
        {"op": "projects"},
    ]


def _deep_event() -> list[dict[str, Any]]:
    return [
        {"op": "event", "a": ["deep", {"$d": [["x", {"$n": [1000, 1]}]]}]},
        {"op": "event", "a": ["fine", {"$d": [["x", {"$n": [5, 1]}]]}]},
        {"op": "put", "a": ["project", enc(_p("P030", "q"))]},
    ]


def _sequences() -> list[dict[str, Any]]:
    bad_project = {"projects": [_p("P001", "a"), {"id": "P002"}]}
    no_id = {"projects": [{"name": "x"}]}
    bad_views = {
        "checkouts": [_k("K1", "P001")],
        "checkout_views": [{"machine": "M1"}],
        "projects": [_p("P001", "a")],
    }
    bad_witness = {"project_witnesses": [{"project": "P001"}], "projects": []}
    ints = {
        "projects": [_p("P001", "a")],
        "project_witnesses": [
            _w("P001", int("9" * 40), 1),
            _w("P001", -int("9" * 39), 2),
        ],
        "project_files": [{"file_id": "F1", "project": 7}],
    }
    reads = _reads()
    stamps = [
        ["2026-03-04T05:06:07", 0],
        ["2026-03-04T00:30:00", 90],
        ["2026-12-31T23:59:59.999999", -600],
        ["0001-01-01T00:30:00", 60],
        ["9999-12-31T23:30:00", -60],
    ]
    out = [
        _sequence("reads", _TABLES, reads),
        _sequence(
            "reads_session", _TABLES, reads[:4], init={"session": "S1", "given": True}
        ),
        _sequence("cache", _TABLES, _cache()),
        _sequence("writes", _TABLES, _writes(), init={"session": "S2"}),
        _sequence("rollback", _TABLES, _rollback(), fail=["record", 2]),
        _sequence("fail_put", _TABLES, _rollback()[:2], fail=["put", 1]),
        _sequence("fail_read", _TABLES, reads[:12], fail=["rows", 1]),
        _sequence("outer", _TABLES, _rollback()[:2], outer=True),
        _sequence("deep_event", _TABLES, _deep_event()),
        _sequence("bad_project", bad_project, [{"op": "project", "a": "P001"}]),
        _sequence("no_id", no_id, [{"op": "projects"}]),
        _sequence("bad_views", bad_views, [{"op": "checkouts", "a": None}]),
        _sequence(
            "bad_witness",
            bad_witness,
            [{"op": "witnessed"}, {"op": "has_witnesses", "a": "P001"}],
        ),
        _sequence(
            "ints",
            ints,
            [
                {"op": "witness_commits", "a": "P001"},
                {"op": "witnessed"},
                {"op": "file_binding", "a": "F1"},
            ],
        ),
        _sequence("empty", {}, reads),
    ]
    out += [
        _sequence(f"stamp{i}", {}, [{"op": "stamp"}], init={"now": n})
        for i, n in enumerate(stamps)
    ]
    return out


# ---------------------------------------------------------------------------
# The digest over seeded merge graphs
# ---------------------------------------------------------------------------

_SEED = 20261121
_GRAPHS = 400
_NAMES = ["alpha", "Alpha", "ALPHA", "beta", "Beta", "gamma", "projects:"]
_SCOPES = [
    ["projects:alpha"],
    ["projects:beta", "projects:gamma"],
    ["projects:BETA"],
    ["projects:projects:"],
    ["projects:", "alpha"],
]


def _t(v: Any) -> str:
    if v is None:
        return "~"
    if isinstance(v, list):
        return ",".join(v)
    return str(v)


def _graph(rng: _Lcg) -> list[dict[str, Any]]:
    n = 1 + rng.next(9)
    ids = [f"P{i:03d}" for i in range(1, n + 1)]
    links = [None, None, None, "global", "PX", *ids]
    return [
        _p(
            pid,
            _NAMES[rng.next(len(_NAMES))],
            merged_into=links[rng.next(len(links))],
            parent=links[rng.next(len(links))],
        )
        for pid in ids
    ]


def _digest() -> dict[str, Any]:
    rng = _Lcg(_SEED)
    h = hashlib.sha256()
    count = 0
    for _ in range(_GRAPHS):
        projects = _graph(rng)
        stub = _Stub({"projects": projects}, None, False)
        m = mm.Membership(stub, "M1", now=lambda: _now(["2026-01-01T00:00:00", 0]))  # type: ignore[arg-type]
        asks = [p["id"] for p in projects] + ["global", "PX", None]
        for a in asks:
            h.update(f"C|{_t(a)}|{_t(m.canonical(a))}\n".encode())
            h.update(f"H|{_t(a)}|{_t(m.chain_of(a))}\n".encode())
            count += 2
        for a in [p["id"] for p in projects]:
            h.update(f"M|{a}|{_t(m._members(a))}\n".encode())
            count += 1
        for sc in _SCOPES:
            h.update(f"N|{_t(sc)}|{_t(sorted(m.named(sc)))}\n".encode())
            count += 1
    return {
        "seq": "digest",
        "seed": _SEED,
        "graphs": _GRAPHS,
        "names": _NAMES,
        "scopes": _SCOPES,
        "count": count,
        "sha256": h.hexdigest(),
    }


def generate() -> dict[str, bytes]:
    header = {"seq": "header", "base": _spelled(_TABLES)}
    return {"membership_core.jsonl": jsonl([header, *_sequences(), _digest()])}


_UNICODE_NAMES = ["Straße", "STRASSE", "ǅ", "İ", "i̇", "ﬃ", "FFI", "ΣΑΣ", "σας", "ẞ"]


def generate_unicode() -> dict[str, bytes]:
    projects = [_p(f"P{i + 1:03d}", n) for i, n in enumerate(_UNICODE_NAMES)]
    asks = [[f"projects:{n}"] for n in [*_UNICODE_NAMES, "strasse", "ǆ", "ffi", "σασ"]]
    seq = _sequence(
        "unicode", {"projects": projects}, [{"op": "named", "a": a} for a in asks]
    )
    return {"membership_core_unicode.jsonl": jsonl([seq])}
