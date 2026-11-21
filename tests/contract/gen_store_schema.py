"""The additive schema steps of `store_schema` and their leaves.

``store_schema_steps.jsonl``: each step (`_add_membership_tables`,
`_add_returned_marks`, `_add_proposals_index`, `_add_session_index`,
`_add_path_targets`) run on an in-memory store made from the five schema
texts and then changed by the row's own SQL (an object dropped, the store
id missing or empty, ``checkouts`` without ``unresolved_json``, a column
named only in a comment or in another case, an index named in capitals,
a trigger whose error spells "read only" with a space).
The row records whether the membership id was drawn, what the store lacks
or holds beyond the full schema after, ``checkouts``' text when it differs,
meta's rows, the meta dict, the warnings and any exception. The minted id
is the fixture's. Then `orphan_names` under both path flavours (the
reference's `Path` replaced by `PurePosixPath` and `PureWindowsPath`),
`holds_v8_files` over names (a directory object that lists them) and a
seeded sweep pinned by its digest, and `absent_text` over argument shapes.

``store_schema_posix.jsonl``: the steps on a store file made read-only
(mode 0444), and `refuse_unmigrated_v8` over real folders, the scratch
root spelled ``<T>``. POSIX only: modes, and the message spells the
host's path. The generator refuses to run as root, which writes a
mode-0444 file and lists a mode-0300 folder.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
import types
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from bettermemory import store_schema as ss

from ._common import captured_by, jsonl

_MINTED = "ab" * 16
_TEXTS = (
    ss.SCHEMA,
    ss.MEMBERSHIP_SCHEMA,
    ss.ADDITIVE_SCHEMA,
    ss.PROPOSALS_INDEX_SCHEMA,
    ss.SESSION_INDEX_SCHEMA,
)
_ID = ss.MEMBERSHIP_STORE_ID
_STEPS = {
    "membership": ss._add_membership_tables,
    "returned": ss._add_returned_marks,
    "proposals": ss._add_proposals_index,
    "session": ss._add_session_index,
    "path_targets": ss._add_path_targets,
}


def _base(conn: sqlite3.Connection) -> None:
    for text in _TEXTS:
        conn.executescript(text)
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (_ID, "cd" * 16))


def _master(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(n): str(s or "")
        for n, s in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IS NOT NULL"
        )
    }


def _full() -> dict[str, str]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _base(conn)
    return _master(conn)


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.said.append(record.getMessage())


def _run(conn: sqlite3.Connection, step: str, meta: dict[str, str]) -> dict[str, Any]:
    drawn: list[int] = []

    def token_hex(n: int) -> str:
        drawn.append(n)
        return _MINTED

    names = vars(ss)
    saved = names["secrets"]
    names["secrets"] = types.SimpleNamespace(token_hex=token_hex)
    handler = _Warnings()
    out: dict[str, Any] = {}
    try:
        with captured_by(ss.log, handler):
            _STEPS[step](conn, "STORE", meta)  # type: ignore[arg-type]
    except sqlite3.Error as exc:
        out["error"] = [type(exc).__name__, str(exc)]
    finally:
        names["secrets"] = saved
    out["drawn"] = bool(drawn)
    out["warnings"] = handler.said
    return out


def _after(conn: sqlite3.Connection, full: dict[str, str]) -> dict[str, Any]:
    master = _master(conn)
    return {
        "lacks": sorted(set(full) - set(master)),
        "extra": sorted(set(master) - set(full)),
        "checkouts": None
        if master.get("checkouts") == full["checkouts"]
        else master.get("checkouts"),
        "meta_rows": _meta_rows(conn),
    }


def _meta_rows(conn: sqlite3.Connection) -> list[list[str]] | None:
    try:
        return [
            [str(k), str(v)]
            for k, v in conn.execute("SELECT key, value FROM meta ORDER BY key")
        ]
    except sqlite3.Error:
        return None


def _cases(full: dict[str, str]) -> list[tuple[str, list[str]]]:
    checkouts = full["checkouts"]
    lines = checkouts.split("\n")
    without = "\n".join(line for line in lines if "unresolved_json" not in line)
    # The column list's last line loses its comma with the column after it.
    without = without.replace(",\n)", "\n)")
    commented = without.replace("\n)", "\n    -- unresolved_json\n)")
    other_case = checkouts.replace("unresolved_json", "Unresolved_JSON")
    no_id = f"DELETE FROM meta WHERE key = '{_ID}'"
    cases: list[tuple[str, list[str]]] = [
        ("complete", []),
        ("no store id", [no_id]),
        ("empty store id", [f"UPDATE meta SET value = '' WHERE key = '{_ID}'"]),
        ("integer store id", [f"UPDATE meta SET value = 7 WHERE key = '{_ID}'"]),
        ("without column", ["DROP TABLE checkouts", without]),
        (
            "bare checkouts",
            ["DROP TABLE checkouts", "CREATE TABLE checkouts (id TEXT)"],
        ),
        ("comment names column", ["DROP TABLE checkouts", commented]),
        ("comment names column, no id", ["DROP TABLE checkouts", commented, no_id]),
        ("column in capitals", ["DROP TABLE checkouts", other_case]),
        (
            "index in capitals",
            [
                "DROP INDEX log_corroborations",
                "CREATE INDEX LOG_CORROBORATIONS ON log(session) WHERE kind = 'memory_put'",
            ],
        ),
        (
            "proposals index in capitals",
            [
                "DROP INDEX proposals_by_state",
                "CREATE INDEX PROPOSALS_BY_STATE ON proposals(state, kind)",
            ],
        ),
        (
            "session index in capitals",
            [
                "DROP INDEX log_by_session",
                "CREATE INDEX LOG_BY_SESSION ON log(session, seq)",
            ],
        ),
        ("backfill done", ["INSERT INTO meta VALUES ('returned_backfill', 'done')"]),
        ("no meta table", ["DROP TABLE meta"]),
        # The error names a table spelled with a space, which the steps'
        # read-only test reads with the spaces removed.
        (
            "trigger names read only",
            [
                "CREATE TRIGGER meta_put AFTER INSERT ON meta "
                'BEGIN INSERT INTO "read only" VALUES (1); END'
            ],
        ),
    ]
    kinds = {
        str(n): str(t)
        for n, t in _plain().execute("SELECT name, type FROM sqlite_master")
    }
    for name in sorted(
        ss._MEMBERSHIP_OBJECTS
        | ss._ADDITIVE_OBJECTS
        | ss._PROPOSALS_INDEX_OBJECTS
        | ss._SESSION_INDEX_OBJECTS
    ):
        cases.append((f"drop {name}", [f"DROP {kinds[name].upper()} {name}"]))
    return cases


def _plain() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _base(conn)
    return conn


def _meta_of(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}


def _steps(full: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for case, setup in _cases(full):
        for step in _STEPS:
            conn = _plain()
            for sql in setup:
                conn.execute(sql)
            meta = _meta_of(conn)
            row: dict[str, Any] = {
                "k": "step",
                "case": case,
                "step": step,
                "meta_in": sorted(meta.items()),
            }
            row.update(_run(conn, step, meta))
            row["meta_out"] = sorted(meta.items())
            row.update(_after(conn, full))
            if row["checkouts"] is not None and row["checkouts"] in setup:
                row["checkouts"] = "=setup"
            conn.close()
            rows.append(row)
    # A meta dict that disagrees with the table: the table's id wins.
    conn = _plain()
    lacking: dict[str, str] = {}
    row = {"k": "step", "case": "dict lacks id", "step": "membership"}
    row["meta_in"] = []
    row.update(_run(conn, "membership", lacking))
    row["meta_out"] = sorted(lacking.items())
    row.update(_after(conn, full))
    rows.append(row)
    return rows


_ORPHAN_VALUES = [
    None,
    "",
    "[]",
    "{}",
    "null",
    '"a"',
    '["a", "b"]',
    '["", ".", "..", "...", "a/b", "/a", "a/", "a\\u005cb", "c:", "c:x", "ab:c", ":"]',
    '["memory.sqlite.orphaned-1.sqlite", 1, null, true, ["x"], {"a": 1}]',
    '["\\u00e9", "\\ud800", "a\\u0000b", " ", "a b"]',
    "[1,",
    "[" * 2000 + "]" * 2000,
    "[" * 100 + "]" * 100,
    '["a"] x',
    "﻿[]",
    '[1e999, "z"]',
]


def _orphans() -> list[dict[str, Any]]:
    rows = []
    names = vars(ss)
    saved = names["Path"]
    for value in _ORPHAN_VALUES:
        answers = {}
        for flavour, cls in (("posix", PurePosixPath), ("windows", PureWindowsPath)):
            names["Path"] = cls
            try:
                answers[flavour] = ss.orphan_names(value)
            finally:
                names["Path"] = saved
        shown: Any = value
        if value is not None and len(value) > 300:
            shown = {"open": value.count("["), "close": value.count("]")}
        spelled = {
            flavour: [_name(n) for n in names] for flavour, names in answers.items()
        }
        rows.append({"k": "orphans", "value": shown, **spelled})
    return rows


def _name(name: str) -> Any:
    """A name, or its code points where it holds a lone surrogate (which a
    JSON reader of the fixture may refuse)."""
    if any(0xD800 <= ord(c) <= 0xDFFF for c in name):
        return {"cps": [ord(c) for c in name]}
    return name


class _Listing:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def iterdir(self) -> list[types.SimpleNamespace]:
        return [types.SimpleNamespace(name=n) for n in self.names]


_ULID = "01HZY3K6V0A7Q8R9S0T1V2W3X4"
_V8_NAMES = [
    ".events.jsonl",
    ".tombstones",
    ".events.1.jsonl",
    ".events.jsonl.bak",
    ".events..jsonl",
    ".events.jsonl/",
    "events.jsonl",
    ".tombstones2",
    f"2024-01-01-slug-{_ULID}.md",
    f"-{_ULID}.md",
    f"{_ULID}.md",
    f"x-{_ULID}.MD",
    f"x-{_ULID}.md\n",
    f"x\n-{_ULID}.md",
    f"a\nb-c-{_ULID}.md",
    f"x-{_ULID.lower()}.md",
    f"x-{_ULID[:-1]}I.md",
    f"x-{_ULID[:-1]}L.md",
    f"x-{_ULID[:-1]}O.md",
    f"x-{_ULID[:-1]}U.md",
    f"x-{_ULID[:-1]}.md",
    f"x-{_ULID}0.md",
    f"x--{_ULID}.md",
    f"x-{_ULID}.md.md",
    "x-" + "Z" * 26 + ".md",
    "x-" + "0" * 26 + ".md",
    "é-" + _ULID + ".md",
    "",
    "memory.sqlite",
]

_SWEEP_PARTS = ["-", "A", "I", "0", "Z", ".md", "\n", "x", ".events.", ".jsonl"]
_SWEEP_COUNT = 4000


def _sweep_names() -> list[str]:
    """A linear congruential sweep the native test repeats: each name is
    `state % 8 + 26` parts, each `state % 10` of `_SWEEP_PARTS`, where
    `state = (state * 6364136223846793005 + 1442695040888963407) % 2**64`
    is stepped before each draw, starting at 1."""
    state = 1
    names = []

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        return state >> 33

    for _ in range(_SWEEP_COUNT):
        n = draw() % 8 + 26
        names.append("".join(_SWEEP_PARTS[draw() % 10] for _ in range(n)))
    return names


def _v8() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"k": "v8", "name": n, "holds": ss.holds_v8_files(_Listing([n]))}  # type: ignore[arg-type]
        for n in _V8_NAMES
    ]
    rows.append(
        {"k": "v8", "names": [], "holds": ss.holds_v8_files(_Listing([]))}  # type: ignore[arg-type]
    )
    answers = "".join(
        "1" if ss.holds_v8_files(_Listing([n])) else "0"  # type: ignore[arg-type]
        for n in _sweep_names()
    )
    rows.append(
        {
            "k": "v8_sweep",
            "count": _SWEEP_COUNT,
            "held": answers.count("1"),
            "sha256": hashlib.sha256(answers.encode()).hexdigest(),
        }
    )
    return rows


def _tagged(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"b": value}
    if isinstance(value, int):
        return {"i": value}
    if isinstance(value, str):
        return {"s": value}
    return {"t": [_tagged(v) for v in value]}


def _absent() -> list[dict[str, Any]]:
    shapes: list[tuple[type[KeyError], tuple[Any, ...]]] = [
        (ss.NotFoundError, ("k1",)),
        (ss.NotFoundError, ()),
        (ss.NotFoundError, ("a", "b")),
        (ss.NotFoundError, (None,)),
        (ss.NotFoundError, (3,)),
        (ss.NotFoundError, (("x", 1),)),
        (ss.MemoryNotFoundError, ("no memory with id x",)),
        (ss.MemoryNotFoundError, ('it\'s "quoted"',)),
        (ss.TombstonedError, (1, 2)),
        (ss.NotTombstonedError, ()),
        (ss.NotTombstonedError, (True,)),
        (ss.TombstonedError, ("",)),
    ]
    return [
        {
            "k": "absent",
            "class": cls.__name__,
            "args": [_tagged(a) for a in args],
            "text": ss.absent_text(cls(*args)),
        }
        for cls, args in shapes
    ]


def generate() -> dict[str, bytes]:
    full = _full()
    rows = [*_steps(full), *_orphans(), *_v8(), *_absent()]
    setups = dict(_cases(full))
    setups["dict lacks id"] = []
    header = {"cases": len(rows), "minted": _MINTED, "setups": setups}
    return {"store_schema_steps.jsonl": jsonl([header, *rows])}


def _refuse_root() -> None:
    """Stop where the process runs as root, which writes a mode-0444 file
    and lists a mode-0300 folder, so the rows would not be a user's."""
    if getattr(os, "geteuid", lambda: 1)() == 0:
        raise RuntimeError(
            "the read-only and unlistable cases are a user's; "
            "do not generate store_schema_posix as root"
        )


def _read_only(root: Path, full: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    no_id = f"DELETE FROM meta WHERE key = '{_ID}'"
    cases = [
        ("complete", []),
        ("no store id", [no_id]),
        ("drop proposals_by_state", ["DROP INDEX proposals_by_state"]),
        ("drop log_by_session", ["DROP INDEX log_by_session"]),
        ("no meta table", ["DROP TABLE meta"]),
        ("no proposals table", ["DROP TABLE proposals"]),
        (
            "proposals index in capitals",
            [
                "DROP INDEX proposals_by_state",
                "CREATE INDEX PROPOSALS_BY_STATE ON proposals(state, kind)",
            ],
        ),
        (
            "session index in capitals",
            [
                "DROP INDEX log_by_session",
                "CREATE INDEX LOG_BY_SESSION ON log(session, seq)",
            ],
        ),
    ]
    for i, (case, setup) in enumerate(cases):
        for step in _STEPS:
            path = root / f"ro-{i}-{step}.sqlite"
            conn = sqlite3.connect(path, isolation_level=None)
            _base(conn)
            for sql in setup:
                conn.execute(sql)
            conn.close()
            path.chmod(0o444)
            conn = sqlite3.connect(path, isolation_level=None)
            meta = _meta_of(conn)
            row: dict[str, Any] = {"k": "read_only", "case": case, "step": step}
            row["setup"] = setup
            row["meta_in"] = sorted(meta.items())
            row.update(_run(conn, step, meta))
            row["meta_out"] = sorted(meta.items())
            row.update(_after(conn, full))
            conn.close()
            rows.append(row)
    return rows


def _v8_dirs(root: Path) -> list[dict[str, Any]]:
    rows = []
    layouts: list[tuple[str, list[str], str]] = [
        ("store there", ["memory.sqlite", ".events.jsonl"], "memory.sqlite"),
        ("events log", [".events.jsonl"], "memory.sqlite"),
        ("events shard", [".events.3.jsonl"], "memory.sqlite"),
        ("tombstones folder", [".tombstones/"], "memory.sqlite"),
        ("memory file", [f"2024-01-01-a-{_ULID}.md"], "memory.sqlite"),
        ("nothing", ["notes.md"], "memory.sqlite"),
        ("store is a folder", ["memory.sqlite/", ".events.jsonl"], "memory.sqlite"),
        ("no parent", [], "missing/memory.sqlite"),
        ("parent a file", ["f"], "f/memory.sqlite"),
        ("doubled separator", [".events.jsonl"], "/memory.sqlite"),
        ("trailing slash", ["memory.sqlite", ".events.jsonl"], "memory.sqlite/"),
        ("trailing dot", ["memory.sqlite", ".events.jsonl"], "memory.sqlite/."),
        ("unlistable", ["sub/", "sub/.events.jsonl"], "sub/memory.sqlite"),
    ]
    for i, (case, made, name) in enumerate(layouts):
        top = root / f"v8-{i}"
        top.mkdir()
        for entry in made:
            if entry.endswith("/"):
                (top / entry).mkdir()
            else:
                (top / entry).write_text("")
        if case == "unlistable":
            (top / "sub").chmod(0o300)
        spelled = f"{top}/{name}"
        try:
            got = None
            ss.refuse_unmigrated_v8(Path(spelled))
        except ss.UnmigratedV8DirectoryError as exc:
            got = str(exc).replace(str(root), "<T>")
        finally:
            if case == "unlistable":
                (top / "sub").chmod(0o700)
        rows.append(
            {
                "k": "v8_dir",
                "case": case,
                "made": made,
                "path": spelled.replace(str(root), "<T>"),
                "refused": got,
            }
        )
    return rows


def generate_posix() -> dict[str, bytes]:
    _refuse_root()
    full = _full()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        rows = [*_read_only(root, full), *_v8_dirs(root)]
        for p in root.rglob("*"):
            if p.is_file():
                p.chmod(0o600)
    return {"store_schema_posix.jsonl": jsonl([{"cases": len(rows)}, *rows])}
