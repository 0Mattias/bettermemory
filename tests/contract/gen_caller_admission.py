"""Fixture: what a caller is admitted to see, given how it was resolved.

`admission.Admission` answers, for one surface and one caller's
`membership_records.Resolution`, which records the caller may see: by the
project a record's ``homes`` row names (`admits_project`), by that and a
global record's scope list (`admits_row`), the projects whose records it
admits (`admitted_projects`), whether it admits ``global`` records
(`admits_global`), and the home a recorded project reads as
(`project_home`). It reads the membership model's ``projects`` table, with
the ``project`` rows of ``native_membership_rows`` replacing the legacy
rows they key (`store_membership.membership_rows`, `store_native`).

Each table case is a store `Store.create` builds, its project rows put
through the reference's own write (`Store.put_membership`, which logs and
folds them; a name holding a lone surrogate goes to
``native_membership_rows`` that way) or, for what that write refuses
(cycles are accepted by it; malformed native rows, values of other types,
missing tables and columns are not), written by direct SQL into that
store: the case says which (``direct``). Each case records the two
tables' definitions where they differ from the header's and their rows,
every value tagged (`_cell`), and the reference's answers for every
caller and every surface: each answer is ``1``, ``0`` or ``E`` (the read
raised the table's ``raises``).

``callers`` also records the three callers of `gen_store_path`'s main
store (alpha, beta, plain), resolved the same way in the scratch world
(`_sandbox.world`, whose git environment is `gen_store_path._neutral_git`),
with the main store's ``projects`` rows (which ``search_store.jsonl`` does
not carry), so a native test computes their admission from the
resolution and holds it to the admission ``search_store.jsonl``
tabulates. The scratch path in a resolution's ``lines`` is written
``<scratch>`` with ``/`` separators, and each row's ``minted`` stamp is
`_MINTED`.

Gates: none. The bytes depend on the reference's code, on `str.casefold`
of the project names and scopes here (ASCII, ``ß`` and Greek sigma, whose
case folding has not changed since Unicode 3.0), on `json`, `repr` and
`str` of SQLite's values (the same from Python 3.11 to 3.14), and, for the
three callers, on how many log rows a resolution writes and the ids it
mints, which no platform, git version or SQLite version changes. No
ranking, no FTS5, no float arithmetic.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bettermemory import location
from bettermemory.admission import Admission
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.membership import SURFACES, Membership, Resolution
from bettermemory.store import Store
from bettermemory.store_membership import membership_rows

from ._common import jsonl
from ._sandbox import Scrubber, World, world
from .gen_store_path import _repo

_MINTED = "2026-06-01T12:00:00Z"
_CALLERS = ("alpha", "beta", "plain")
#: The surfaces in the order an answer string lists them.
_SURFACE_ORDER = tuple(SURFACES)


def _cell(value: Any) -> Any:
    """A SQLite value as JSON: text as a string (``{"cps": [...]}`` where
    it holds a lone surrogate), null, ``{"int": "<decimal>"}``,
    ``{"real": "<IEEE-754 bits, hex>"}``, ``{"blob": "<hex>"}``."""
    if value is None:
        return None
    if isinstance(value, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            return {"cps": [ord(c) for c in value]}
        return value
    if isinstance(value, bool):
        raise TypeError("SQLite holds no bool")
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, float):
        import struct

        return {"real": struct.pack(">d", value).hex()}
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    raise TypeError(type(value).__name__)


def _key(value: Any) -> Any:
    """A recorded project or a home's value as JSON: as `_cell`, and a
    bool as ``{"bool": true}``."""
    if isinstance(value, bool):
        return {"bool": value}
    return _cell(value)


class _Reader:
    """What `Membership` reads of a store: its connection, through
    `membership_rows`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def membership_rows(self, table: str, **kw: Any) -> list[dict[str, Any]]:
        return membership_rows(self, table, **kw)  # type: ignore[arg-type]


def _row(
    pid: str,
    name: str,
    *,
    merged_into: str | None = None,
    provisional: str | None = None,
    parent: str | None = None,
    tenant: str | None = None,
    boundary: bool = False,
) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "merged_into": merged_into,
        "provisional": provisional,
        "parent": parent,
        "tenant": tenant,
        "folder": 0,
        "boundary": int(boundary),
        "minted": _MINTED,
    }


_NO_AFFINITY = (
    "CREATE TABLE projects (id, name, merged_into, provisional, parent, "
    "tenant, folder, boundary, minted)"
)
_NATIVE_INSERT = (
    "INSERT INTO native_membership_rows(member_kind, key_text, row_text) "
    "VALUES (?, ?, ?)"
)


def _native(row: dict[str, Any]) -> str:
    import json

    return json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


#: The table cases: (label, rows put through `put_membership`, SQL run
#: directly after them (statement, parameters), extra recorded projects
#: asked about, callers named by (outcome, project, chain or None for the
#: model's, tenant or "model")).
_Case = tuple[
    str,
    list[dict[str, Any]],
    list[tuple[str, tuple[Any, ...]]],
    list[Any],
    list[tuple[str, str | None, Sequence[str] | None, Any]],
]


def _encoded(pid: str, name: str, codec: str) -> bytes:
    """A native project row's JSON as bytes in `codec`, a lone surrogate
    passed through."""
    text = json.dumps(p_row(pid, name), sort_keys=True, ensure_ascii=False)
    return text.encode(codec, "surrogatepass")


def p_row(pid: str, name: str) -> dict[str, Any]:
    return _row(pid, name)


def _byte_rows() -> list[tuple[str, tuple[Any, ...]]]:
    """Native rows whose JSON is bytes, as `json.loads` reads bytes:
    `json.detect_encoding` (BOMs, then the NUL pattern of the first four
    bytes) and a decode with ``surrogatepass``; a row that does not decode
    is malformed and still removes the legacy row of its key (P001)."""
    bom8, b16le, b16be = b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"
    b32le, b32be = b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"
    blobs: list[tuple[str, bytes]] = [
        ("P001", b"\xff\xfe"),
        ("P002", b'{"id": "P002", "name": "\xed\xa0\x80"}'),
        ("P020", _encoded("P020", "le16", "utf-16-le")),
        ("P021", _encoded("P021", "be16", "utf-16-be")),
        ("P022", b16le + _encoded("P022", "bom-le16", "utf-16-le")),
        ("P023", b16be + _encoded("P023", "bom-be16", "utf-16-be")),
        ("P024", _encoded("P024", "le32", "utf-32-le")),
        ("P025", _encoded("P025", "be32", "utf-32-be")),
        ("P026", b32le + _encoded("P026", "bom-le32", "utf-32-le")),
        ("P027", b32be + _encoded("P027", "bom-be32", "utf-32-be")),
        ("P028", bom8 + _encoded("P028", "sig", "utf-8")),
        ("P029", _encoded("P029", "s\udc80u8", "utf-8")),
        ("P030", _encoded("P030", "s\ud800u16", "utf-16-le")),
        ("P031", _encoded("P031", "odd", "utf-16-le") + b"\x00\x7b"[:1]),
        ("P032", _encoded("P032", "cut32", "utf-32-be")[:-2]),
        ("P033", b""),
        (
            "P034",
            _encoded("P034", "high", "utf-32-le").replace(
                b"h\x00\x00\x00", b"\x00\x00\x11\x00", 1
            ),
        ),
        ("P035", b16le + _encoded("P035", "pair\U0001f600", "utf-16-le")),
        ("P036", b"{}"),
        ("P037", _encoded("P037", "end\ud800", "utf-16-be")[:-2] + b"\xd8\x00"),
        ("P038", b"\xff"),
    ]
    return [(_NATIVE_INSERT, ("project", f'["{k}"]', b)) for k, b in blobs]


def _cases() -> list[_Case]:
    p = _row
    big_merge = [p("P100", "head")] + [
        p(f"P{100 + i:03d}", f"m{i}", merged_into=f"P{99 + i:03d}")
        for i in range(1, 40)
    ]
    big_parent = [p("P200", "root")] + [
        p(f"P{200 + i:03d}", f"c{i}", parent=f"P{199 + i:03d}") for i in range(1, 40)
    ]
    return [
        ("empty", [], [], ["P001"], []),
        (
            "two projects",
            [p("P001", "alpha"), p("P002", "beta")],
            [],
            [],
            [
                ("silent", "P001", None, "model"),
                ("ask", "P002", None, "model"),
                ("new", "P001", (), None),
                ("deferred", "P001", None, "model"),
                ("indeterminate", "P002", None, "model"),
            ],
        ),
        (
            "merges",
            [
                p("P001", "alpha"),
                p("P002", "alpha-old", merged_into="P001"),
                p("P003", "older", merged_into="P002"),
                p("P004", "beta"),
                p("P005", "to-missing", merged_into="P099"),
                p("P006", "to-global", merged_into="global"),
                p("P007", "to-empty", merged_into=""),
            ],
            [],
            ["P099"],
            [
                ("silent", "P001", None, "model"),
                ("ask", "P003", ("P003",), None),
                ("silent", "P004", None, "model"),
                ("new", "P099", ("P099",), None),
            ],
        ),
        (
            "parents",
            [
                p("P001", "workspace"),
                p("P002", "app", parent="P001"),
                p("P003", "lib", parent="P002"),
                p("P004", "other"),
                p("P005", "merged-parent", merged_into="P001"),
                p("P006", "child-of-merged", parent="P005"),
                p("P007", "orphan", parent="P098"),
            ],
            [],
            [],
            [
                ("silent", "P001", None, "model"),
                ("silent", "P002", None, "model"),
                ("new", "P003", None, "model"),
                ("ask", "P006", None, "model"),
                ("silent", "P007", None, "model"),
                ("silent", "P002", (), None),
            ],
        ),
        (
            "boundaries",
            [
                p("P001", "outer"),
                p("P002", "inside", parent="P001", tenant="P002", boundary=True),
                p("P003", "deep", parent="P002", tenant="P002"),
                p("P004", "beside", parent="P001"),
                p("P005", "empty-tenant", tenant=""),
            ],
            [],
            [],
            [
                ("silent", "P001", None, "model"),
                ("silent", "P002", None, "model"),
                ("silent", "P003", None, "model"),
                ("silent", "P004", None, "P002"),
                ("silent", "P001", None, ""),
                ("new", "P005", None, "model"),
            ],
        ),
        (
            "provisional",
            [
                p("P001", "alpha"),
                p("P002", "unresolved", provisional="indeterminate"),
                p("P003", "waiting", provisional="deferred"),
                p("P004", "first-sight", provisional="first_sight"),
                p("P005", "merged-unresolved", merged_into="P002"),
                p("P006", "sub-unresolved", parent="P002"),
            ],
            [],
            [],
            [
                ("silent", "P001", None, "model"),
                ("silent", "P002", None, "model"),
                ("silent", "P004", None, "model"),
                ("silent", "P006", None, "model"),
            ],
        ),
        (
            "named",
            [
                p("P001", "Alpha"),
                p("P002", "alpha"),
                p("P003", "Straße"),
                p("P004", "ΣΑΣ"),
                p("P005", "gone", merged_into="P001"),
                p("P006", "beta"),
            ],
            [],
            [],
            [
                ("silent", "P001", None, "model"),
                ("silent", "P003", None, "model"),
                ("silent", "P006", None, "model"),
                ("new", "P006", (), None),
            ],
        ),
        (
            "merge cycle",
            [
                p("P001", "a", merged_into="P002"),
                p("P002", "b", merged_into="P003"),
                p("P003", "c", merged_into="P001"),
                p("P004", "self", merged_into="P004"),
                p("P005", "into-cycle", merged_into="P002"),
            ],
            [],
            [],
            [("silent", "P001", None, "model"), ("silent", "P004", None, "model")],
        ),
        (
            "parent cycle",
            [
                p("P001", "a", parent="P002"),
                p("P002", "b", parent="P001"),
                p("P003", "self", parent="P003"),
                p("P004", "under", parent="P001"),
            ],
            [],
            [],
            [("silent", "P001", None, "model"), ("silent", "P004", None, "model")],
        ),
        ("long merge chain", big_merge, [], [], [("silent", "P100", None, "model")]),
        (
            "long parent chain",
            big_parent,
            [],
            [],
            [("silent", "P200", None, "model"), ("silent", "P239", None, "model")],
        ),
        (
            "native name",
            [p("P001", "alpha"), p("P002", "s\udcffx")],
            [],
            [],
            [("silent", "P002", None, "model")],
        ),
        (
            "native replaces",
            [p("P001", "alpha"), p("P002", "beta")],
            [
                (
                    _NATIVE_INSERT,
                    (
                        "project",
                        '["P001"]',
                        _native(dict(p("P001", "renamed"), merged_into="P002")),
                    ),
                ),
                (
                    _NATIVE_INSERT,
                    ("project", '["P003"]', _native(p("P003", "native-only"))),
                ),
                (_NATIVE_INSERT, ("project", '["P002"]', "not json")),
                (_NATIVE_INSERT, ("project", '["P009"]', _native(p("P008", "x")))),
                (
                    _NATIVE_INSERT,
                    ("project", '["P010"]', _native({"id": "P010", "name": "x"})),
                ),
                (
                    _NATIVE_INSERT,
                    ("project", '["P011"]', _native(dict(p("P011", "f"), parent=1.5))),
                ),
                (
                    _NATIVE_INSERT,
                    (
                        "project",
                        '["P012"]',
                        _native(dict(p("P012", "t"), tenant=True, parent=7)),
                    ),
                ),
                (_NATIVE_INSERT, ("project", '["P013"]', "[1]")),
                (_NATIVE_INSERT, ("project", '["P014"]', 14)),
                (_NATIVE_INSERT, ("project", b'["P015"]', b'{"id": "P015"}')),
                (
                    _NATIVE_INSERT,
                    ("project", '["P016"]', _native(p("P016", "bytes")).encode()),
                ),
                (
                    _NATIVE_INSERT,
                    (
                        "project",
                        '["P017"]',
                        b"\xef\xbb\xbf" + _native(p("P017", "bom")).encode(),
                    ),
                ),
                (_NATIVE_INSERT, ("checkout", '["P018"]', _native(p("P018", "k")))),
            ],
            [],
            [
                ("silent", "P001", None, "model"),
                ("silent", "P002", None, "model"),
                ("silent", "P012", None, "model"),
            ],
        ),
        (
            "native bytes",
            [p("P001", "alpha"), p("P002", "beta"), p("P003", "gamma")],
            _byte_rows(),
            [f"P{n:03d}" for n in range(20, 39)],
            [("silent", "P001", None, "model"), ("silent", "P021", None, "model")],
        ),
        (
            "malformed native only",
            [p("P001", "alpha")],
            [(_NATIVE_INSERT, ("project", '["P001"]', "{"))],
            [],
            [("silent", "P001", (), None)],
        ),
        (
            "no native table",
            [p("P001", "alpha")],
            [("DROP TABLE native_membership_rows", ())],
            [],
            [("silent", "P001", None, "model")],
        ),
        (
            "native table without a column",
            [p("P001", "alpha")],
            [
                ("DROP TABLE native_membership_rows", ()),
                ("CREATE TABLE native_membership_rows (member_kind, key_text)", ()),
            ],
            [],
            [("silent", "P001", ("P001",), None)],
        ),
        (
            "no projects table",
            [],
            [("DROP TABLE projects", ())],
            [],
            [("silent", "P001", ("P001",), None)],
        ),
        (
            "projects without a column",
            [],
            [
                ("DROP TABLE projects", ()),
                ("CREATE TABLE projects (id, name)", ()),
            ],
            [],
            [("silent", "P001", ("P001",), None)],
        ),
        (
            "values of other types",
            [],
            [
                ("DROP TABLE projects", ()),
                (_NO_AFFINITY, ()),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P001", None, 5, None, None, 1, 0, 0, None),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P002", 7, None, b"deferred", 2.0, b"t", 0, 0, 1.5),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P003", b"n", "", "deferred", "P002", 1.0, 0, 0, _MINTED),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P004", 2.5, "P001", None, None, "", 0, 0, _MINTED),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    (5, "five", None, None, None, None, 0, 0, _MINTED),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    (None, "nameless", None, None, None, None, 0, 0, _MINTED),
                ),
            ],
            ["5", "None"],
            [
                ("silent", "P001", ("P001",), "1"),
                ("silent", "P003", None, "model"),
                ("silent", "P004", ("P004",), None),
            ],
        ),
        (
            "blob id beside native",
            [],
            [
                ("DROP TABLE projects", ()),
                (_NO_AFFINITY, ()),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    (b"P1", "blob", None, None, None, None, 0, 0, _MINTED),
                ),
                (_NATIVE_INSERT, ("project", '["P002"]', _native(p("P002", "n")))),
            ],
            [],
            [("silent", "P002", ("P002",), None)],
        ),
        (
            "number id beside native",
            [],
            [
                ("DROP TABLE projects", ()),
                (_NO_AFFINITY, ()),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    (5, "five", None, None, None, None, 0, 0, _MINTED),
                ),
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P001", "text", None, None, None, None, 0, 0, _MINTED),
                ),
                (_NATIVE_INSERT, ("project", '["P002"]', _native(p("P002", "n")))),
            ],
            [],
            [("silent", "P001", ("P001",), None)],
        ),
        (
            "invalid UTF-8",
            [],
            [
                (
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
                    ("P001", "x", None, None, None, None, 0, 0, _MINTED),
                ),
                ("UPDATE projects SET name = CAST(X'ff' AS TEXT)", ()),
            ],
            [],
            [("silent", "P001", ("P001",), None)],
        ),
    ]


def _resolution_json(r: Resolution | None) -> Any:
    if r is None:
        return None
    out = dataclasses.asdict(r)
    out["witnesses"] = {k: list(v) for k, v in r.witnesses.items()}
    for name in ("candidates", "chain", "lines", "rows"):
        out[name] = list(out[name])
    return out


def _scope_lists(names: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[], ["notes"], ["projects:"], ["projects:nosuch", "x"]]
    for name in names[:4]:
        out.append([f"projects:{name}"])
        out.append(["notes", f"projects:{name.upper()}"])
    out.append(["projects:STRASSE"])
    out.append(["projects:σας"])
    unique: list[list[str]] = []
    for scopes in out:
        if scopes not in unique and not any(
            0xD800 <= ord(c) <= 0xDFFF for s in scopes for c in s
        ):
            unique.append(scopes)
    return unique


def _flag(call: Any) -> str:
    try:
        return "1" if call() else "0"
    except Exception:
        return "E"


def _answers(admission: Admission, keys: list[Any], rows: list[Any]) -> dict[str, Any]:
    try:
        found = admission.admitted_projects()
        admitted: Any = None if found is None else sorted(found)
        if admitted is not None:
            admitted = [_key(a) for a in admitted]
    except Exception:
        admitted = "E"
    return {
        "projects": "".join(
            _flag(lambda k=k: admission.admits_project(k)) for k in keys
        ),
        "rows": "".join(
            _flag(lambda s=s, r=r: admission.admits_row(s, r)) for s, r in rows
        ),
        "admitted": admitted,
        "global": _flag(admission.admits_global),
    }


def _raises(reader: _Reader) -> str | None:
    try:
        membership_rows(reader, "projects")  # type: ignore[arg-type]
    except Exception as exc:
        return type(exc).__name__
    return None


def _table(conn: sqlite3.Connection, name: str) -> Any:
    found = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if found is None:
        return None
    columns = [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
    picks = ", ".join(f'typeof("{c}"), "{c}", CAST("{c}" AS BLOB)' for c in columns)
    conn.text_factory = bytes
    try:
        rows = [
            [r[0], *[_raw_cell(*r[i : i + 3]) for i in range(1, len(r), 3)]]
            for r in conn.execute(f'SELECT rowid, {picks} FROM "{name}" ORDER BY rowid')
        ]
    finally:
        conn.text_factory = str
    return {"sql": str(found[0]), "columns": columns, "rows": rows}


def _raw_cell(kind: bytes, value: Any, raw: bytes | None) -> Any:
    """`_cell` of a value read with its SQL type (``text_factory`` bytes):
    text that is not UTF-8 as ``{"text_hex": ...}``."""
    if kind != b"text" or raw is None:
        return _cell(value)
    try:
        return _cell(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return {"text_hex": raw.hex()}


def _home(admission: Admission, recorded: str) -> Any:
    try:
        h = admission.project_home(recorded)
    except Exception:
        return "E"
    return [_key(h.project), list(h.chain), _key(h.tenant), h.unresolved]


def _case_records(tmp: Path, case: _Case, header_sql: dict[str, str]) -> list[Any]:
    label, rows, direct, extra, callers = case
    root = Path(tempfile.mkdtemp(prefix="case-", dir=tmp))
    store = Store.create(root / "store", keys_dir=root / "keys")
    try:
        conn = store._conn
        conn.execute("PRAGMA synchronous = OFF")
        for row in rows:
            store.put_membership("project", row)
        with conn:
            for sql, params in direct:
                conn.execute(sql, params)
        reader = _Reader(conn)
        model = Membership(reader, "M001")  # type: ignore[arg-type]
        raises = _raises(reader)
        tables = {}
        for name in ("projects", "native_membership_rows"):
            t = _table(conn, name)
            if t is not None and t["sql"] == header_sql[name]:
                del t["sql"]
            tables[name] = t
        ids = [str(r["id"]) for r in rows]
        for sql, params in direct:
            if sql.startswith("INSERT INTO projects"):
                ids.append(str(params[0]))
            elif sql == _NATIVE_INSERT and isinstance(params[2], str):
                if params[2].startswith("{") and params[0] == "project":
                    try:
                        import json

                        ids.append(str(json.loads(params[2])["id"]))
                    except (ValueError, KeyError):
                        pass
        keys: list[Any] = [*dict.fromkeys([*ids, *extra, "global", "P999"]), None]
        names = [str(r["name"]) for r in rows]
        recorded: list[Any] = ["global", ids[0] if ids else "P001", None]
        row_keys = [[s, r] for s in _scope_lists(names) for r in recorded]
        out: list[Any] = [
            {
                "table": label,
                "direct": bool(direct),
                "tables": tables,
                "raises": raises,
                "keys": [_key(k) for k in keys],
                "row_keys": row_keys,
                "homes": [
                    _home(Admission(_SURFACE_ORDER[0], None, model), k)
                    for k in keys
                    if k is not None
                ],
            }
        ]
        resolutions: list[Resolution | None] = [
            None,
            Resolution(outcome="global", project="global", checkout=None, rule="r"),
            Resolution(outcome="indeterminate", project=None, checkout=None, rule="r"),
            Resolution(outcome="deferred", project=None, checkout=None, rule="r"),
        ]
        for outcome, project, chain, tenant in callers:
            if chain is None:
                try:
                    chain = tuple(model.chain_of(project))
                except Exception:
                    chain = (project,) if project else ()
            if tenant == "model":
                try:
                    tenant = model.project(str(project)).tenant
                except Exception:
                    tenant = None
                # A resolution's tenant is text or None (its type); a
                # table's tenant of another type is asked through homes.
                if not isinstance(tenant, str):
                    tenant = None
            resolutions.append(
                Resolution(
                    outcome=outcome,
                    project=project,
                    checkout="K001",
                    rule="r",
                    chain=tuple(chain),
                    tenant=tenant,
                )
            )
        for r in resolutions:
            model = Membership(reader, "M001")  # type: ignore[arg-type]
            first = Admission(_SURFACE_ORDER[0], r, model)
            distinct: list[Any] = []
            by_surface = ""
            for surface in _SURFACE_ORDER:
                ans = _answers(first.on(surface), keys, row_keys)
                if ans not in distinct:
                    distinct.append(ans)
                by_surface += str(distinct.index(ans))
            out.append(
                {
                    "case": label,
                    "resolution": _resolution_json(r),
                    "nothing": first.nothing,
                    "answers": distinct,
                    "by_surface": by_surface,
                }
            )
        return out
    finally:
        store.close()


def _main_callers(w: World) -> dict[str, Any]:
    tmp = w.root
    if True:
        _repo(tmp / "beta", "https://example.invalid/fixture/beta.git", 2)
        (tmp / "plain").mkdir()
        store = Store.create(tmp / "store", keys_dir=tmp / "keys")
        try:
            cfg = Config(
                storage=StorageConfig(directory=str(store.path.parent)),
                behavior=BehaviorConfig(),
            )
            scrub = Scrubber(tmp, now=w.now)
            callers = {}
            for name in _CALLERS:
                c = location.resolve_caller(store, cfg, str(tmp / name), session=None)
                r = _resolution_json(c.resolution)
                r["lines"] = [scrub.paths(line) for line in r["lines"]]
                callers[name] = r
            rows = []
            for r in store.membership_rows("projects"):
                rows.append([_cell(r[c]) if c != "minted" else _MINTED for c in r])
            columns = list(store.membership_rows("projects")[0])
        finally:
            store.close()
    return {"resolutions": callers, "projects": {"columns": columns, "rows": rows}}


def generate() -> dict[str, bytes]:
    with world() as w:
        tmp = Path(tempfile.mkdtemp(prefix="cases-", dir=w.root))
        probe = Store.create(tmp / "probe" / "store", keys_dir=tmp / "probe" / "keys")
        try:
            header_sql = {
                name: str(
                    probe._conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                    ).fetchone()[0]
                )
                for name in ("projects", "native_membership_rows")
            }
        finally:
            probe.close()
        records: list[Any] = [
            {
                "fixture": "caller_admission",
                "surfaces": list(_SURFACE_ORDER),
                "sql": header_sql,
                "main_store": _main_callers(w),
                "legend": (
                    "a 'table' line: the projects and native_membership_rows "
                    "tables of a store (sql where it differs from the header's; "
                    "null: no such table), rows [rowid, cells...], each cell "
                    "text, null, {int}, {real: bits hex}, {blob: hex}, "
                    "{text_hex: bytes stored as text}, {cps}; raises: what "
                    "the read of the projects raises; keys: the recorded "
                    "projects asked about (null: no home row); homes: "
                    "project_home of each non-null key as [project, chain, "
                    "tenant, unresolved] or E. A 'case' line: a resolution "
                    "(null: none) on that table, nothing, and per surface "
                    "(by_surface indexes answers in the header's surface "
                    "order) admits_project of each key, admits_row of each "
                    "row_key [scopes, recorded], admitted_projects, "
                    "admits_global; 1, 0, or E (raised)"
                ),
            }
        ]
        for case in _cases():
            records.extend(_case_records(tmp, case, header_sql))
    return {"caller_admission.jsonl": jsonl(records)}
