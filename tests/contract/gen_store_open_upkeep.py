"""Fixture: the upkeep the owner's writing open does with its key present
(``store_open_upkeep.jsonl``, ``store_open_upkeep_python.jsonl``).

`store_open._open_connected` binds a store whose key is present and then,
for the owner's opener, closes a foreign tail (`Store._close_foreign_tail`:
a ``rekey`` row when the log's last ``rekey`` row names another key) and
settles the origin anchor (`Store._settle_origin`: an anchor written on the
first row of the earliest segment this host holds, or the key named on an
anchor that names none, `Store._name_origin_key`). The native build does
both (`store::open`, `open/upkeep.rs`) and is held to these files.

Each case starts from a base of ``store_bases.json`` with a recipe of
edits (`gen_store_open.setup`'s ops), bound to its file, and opens it as
`gen_store_open` does, with the log's and the wall clock fixed
(`_fixed.fixed`), so the rekey row and the head carry known stamps. A case
may name a hook: ``busy`` (another connection holds the write lock from
before the open until `_close_foreign_tail` returns), ``race`` (a second
owner's open runs between the tail's first read and its lock), or
``readonly_keys`` (the keys directory made 0500 for the open; skipped as
root, where the mode does not refuse). Recorded for each: the outcome, the
whole after-state (`gen_store_open.state`; a text that is not UTF-8 as
`NOT_UTF8` and its bytes), every log row the open
appended (every column), every warning the upkeep logged (the format
string and its arguments: the path with the scratch directory as
``<scratch>``, texts through `_common.text_field`, the exception as its
class and SQLite error name, never its text), the clock's readings, and
what a second open changed and logged (nothing, for a settled store).

Gates: those of ``store_open.jsonl``, for its reasons (Unicode: the
digest's rows are the tokenizer's; POSIX only: modes, a resolved binding,
``flock``). The Python-gated sibling holds the cases whose answer is where
`json.loads` stops recursing (a rekey payload or an anchor nested 100,000
deep: RecursionError out of the open, which 3.14 on Linux may not raise)
and the one whose text is CPython's `sqlite3` module's (an OverflowError).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory.log import last_rekey_name
from bettermemory.store import Store

from ._common import jsonl, text_field
from ._fixed import Ticker, fixed
from .gen_log import STORE_ID
from .gen_store_files import load_bases
from .gen_store_open import (
    ENGINE,
    PRAGMAS,
    _changed,
    _close_left,
    _engine,
    _hex,
    _made,
    _row_mac,
    _scrub,
    setup,
)
from .gen_store_open import state as _open_state

LOG0 = datetime(2001, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1)
WALL0 = datetime(2002, 3, 4, 5, 6, 7, 654321, tzinfo=timezone.utc)
WALL_STEP = timedelta(seconds=1)

#: The functions whose warnings are the upkeep's.
UPKEEP = frozenset(
    {"_close_foreign_tail", "_settle_origin", "_record_origin", "_name_origin_key"}
)

#: How deep the Python-gated cases nest (past every interpreter's limit
#: that refuses it).
DEEP = 100_000

#: A key ring text longer than this is recorded as its SHA-256 (the nested
#: anchor's 200,000 characters).
LONG_TEXT = 4096

BIND = {"bind": "memory.sqlite"}


def _sql(text: str) -> dict[str, Any]:
    return {"sql": text}


def _set(seq: int, assignment: str) -> dict[str, Any]:
    return _sql(f"UPDATE log SET {assignment} WHERE seq = {seq};")


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _fp(key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(key_hex)).hexdigest()


def _rekey_row(seq: int, payload: str, prev_mac: str, mac: str = "c" * 64) -> str:
    return (
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES "
        f"({seq}, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', {_q(payload)}, "
        f"{_q(prev_mac)}, {_q(mac)});"
    )


def _origin(seq: int, mac: str, key: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"seq": seq, "mac": mac}
    if key is not None:
        body["key_fingerprint"] = key
    return {"write": ".origin", "hex": _hex(json.dumps(body))}


def _names(new: str, old: str = "e" * 64) -> str:
    return json.dumps(
        {"new_fingerprint": new, "old_fingerprint": old}, separators=(",", ":")
    )


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: name, base, recipe, an optional hook, and what the native
    open does (``same``: the reference's after-state, appended rows and
    warnings; ``raises``: the reference's class and text after the same
    writes)."""
    intact = bases["intact"]
    mac1, mac2, mac8 = (_row_mac(intact, s) for s in (1, 2, 8))
    own = _fp(intact["keys"][".key"])
    tail = _sql(_rekey_row(9, _names("f" * 64), mac8))
    row10 = _sql(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES "
        f"(10, '2001-02-03T04:05:06.000000Z', NULL, 'note', '{{}}', "
        f"{_q('c' * 64)}, {_q('d' * 64)});"
    )
    unnamed = _origin(1, mac1)
    no_origin = {"unlink": ".origin"}
    row1_rewritten = _sql("UPDATE log SET ts = 'rewritten' WHERE seq = 1;")
    engine_current = _sql(
        f"UPDATE meta SET value = '{ENGINE}' WHERE key = 'engine_version';"
    )

    def case(
        name: str, base: str, recipe: list[dict[str, Any]], **kw: Any
    ) -> dict[str, Any]:
        return {
            "case": name,
            "base": base,
            "recipe": [*recipe, BIND],
            "native": "same",
            **kw,
        }

    return [
        case("settled", "intact", []),
        case("rekeyed_settled", "rekeyed", []),
        # The tail.
        case("tail_foreign", "intact", [tail]),
        case("tail_empty_name", "intact", [_sql(_rekey_row(9, "{}", mac8))]),
        case(
            "tail_no_fingerprint",
            "intact",
            [_sql(_rekey_row(9, _names("not a key", "x"), mac8))],
        ),
        case(
            "tail_own",
            "intact",
            [_sql(_rekey_row(9, _names(own), mac8))],
        ),
        case(
            "tail_surrogate",
            "intact",
            [
                _sql(
                    _rekey_row(
                        9, '{"new_fingerprint":"\\udc80ab","old_fingerprint":"x"}', mac8
                    )
                )
            ],
        ),
        case(
            "tail_wide",
            "intact",
            [_sql(_rekey_row(9, _names("\U0001f600" * 20 + "z"), mac8))],
        ),
        case(
            "tail_last_of_two",
            "intact",
            [
                _sql(_rekey_row(9, _names(own), mac8, "d" * 64)),
                _sql(_rekey_row(10, _names("a" * 64, own), "d" * 64)),
            ],
        ),
        case("tail_busy", "intact", [engine_current, tail], hook="busy"),
        case("tail_race", "intact", [tail], hook="race"),
        case("tail_then_origin", "intact", [tail, no_origin]),
        case(
            "tail_then_origin_on_the_new_row",
            "intact",
            [tail, no_origin, row1_rewritten],
        ),
        case(
            "tail_busy_nothing_held",
            "intact",
            [engine_current, tail, no_origin, row1_rewritten],
            hook="busy",
        ),
        # The last row as `append_row` reads it (`log._row`): `str()` of a
        # BLOB chains the rekey row; a text that is not UTF-8 raises
        # OperationalError in the fetch, logged.
        case("tail_mac_blob", "intact", [tail, _set(9, "mac = x'41'")]),
        case(
            "tail_payload_blob",
            "intact",
            [tail, _set(9, "payload = CAST(payload AS BLOB)")],
        ),
        case("tail_after_ts_blob", "intact", [tail, row10, _set(10, "ts = x'41'")]),
        case(
            "tail_after_session_blob",
            "intact",
            [tail, row10, _set(10, "session = x'41'")],
        ),
        case(
            "tail_after_mac_not_utf8",
            "intact",
            [tail, row10, _set(10, "mac = CAST(x'ff' AS TEXT)")],
        ),
        # `last_rekey_name` reads seq 1 on: a rekey row at seq 0 is none.
        case("tail_at_seq_0", "intact", [_sql(_rekey_row(0, _names("f" * 64), mac8))]),
        # The anchor.
        case("origin_missing", "intact", [no_origin]),
        case("origin_junk", "intact", [{"write": ".origin", "hex": _hex("{")}]),
        case("origin_not_an_anchor", "intact", [_origin(0, mac1, own)]),
        case("origin_missing_rekeyed", "rekeyed", [no_origin]),
        case("origin_first_row_unverified", "intact", [no_origin, row1_rewritten]),
        case("origin_log_empty", "intact", [no_origin, _sql("DELETE FROM log;")]),
        case(
            "origin_first_row_unreadable",
            "intact",
            [
                no_origin,
                _sql("UPDATE log SET ts = CAST(x'ff' AS TEXT) WHERE seq = 1;"),
            ],
        ),
        case(
            "origin_rekey_unreadable",
            "intact",
            [
                no_origin,
                _sql(_rekey_row(9, _names(own), mac8, "d" * 64)),
                _sql(_rekey_row(10, _names(own, own), "d" * 64)),
                _sql("UPDATE log SET ts = CAST(x'ff' AS TEXT) WHERE seq = 9;"),
            ],
        ),
        case("origin_unnamed", "intact", [unnamed]),
        case(
            "origin_unnamed_retired",
            "rekeyed",
            [_origin(1, _row_mac(bases["rekeyed"], 1))],
        ),
        case(
            "origin_unnamed_unverified",
            "intact",
            [_origin(2, mac2), _sql("UPDATE log SET ts = 'rewritten' WHERE seq = 2;")],
        ),
        case("origin_unnamed_gone", "intact", [_origin(99, mac2)]),
        case("origin_unnamed_other_mac", "intact", [_origin(2, mac1)]),
        case(
            "origin_unnamed_unreadable",
            "intact",
            [unnamed, _sql("UPDATE log SET ts = CAST(x'ff' AS TEXT) WHERE seq = 1;")],
        ),
        case("origin_named_other_key", "intact", [_origin(1, mac1, "b" * 64)]),
        case("readonly_keys_origin", "intact", [no_origin], hook="readonly_keys"),
        case("readonly_keys_unnamed", "intact", [unnamed], hook="readonly_keys"),
        # Read through one reader at both steps: the key is named on an
        # anchor past the ring's 1 MiB cap on a key file.
        case(
            "origin_unnamed_past_a_mebibyte",
            "intact",
            [
                {
                    "padded_origin": f'{{"seq": 1, "mac": "{mac1}", "pad": "',
                    "pad": 1_100_000,
                }
            ],
        ),
    ]


def python_cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """The cases a RecursionError ends: where `json.loads` stops is the
    release's and the system's. A nested payload or anchor is spelled as
    an op (``nested_rekey``, ``nested_origin``: `DEEP` arrays, or objects
    with ``shape``), not as its 200,000 characters. The text is the read
    that raised: the last rekey row, the anchor, or the first nested rekey
    row `chain_segments` reads. Also the OverflowError of a tail at the
    last seq an SQLite INTEGER holds (CPython's `sqlite3` text)."""
    intact = bases["intact"]
    mac8 = _row_mac(intact, 8)
    own = _fp(intact["keys"][".key"])
    objects = "objects"
    drift = _sql(
        "UPDATE log SET payload = json_set(payload, '$.tokenizer_fingerprint', "
        f"'{'0' * 64}') WHERE seq = 1;"
    )

    def case(name: str, recipe: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "case": name,
            "base": "intact",
            "recipe": [*recipe, BIND],
            "native": "raises",
        }

    return [
        case("tail_nested", [{"nested_rekey": 9, "prev_mac": mac8, "mac": "c" * 64}]),
        case(
            "origin_segments_nested",
            [
                {"unlink": ".origin"},
                {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64},
                _sql(_rekey_row(10, _names(own, own), "d" * 64)),
            ],
        ),
        case("origin_file_nested", [{"nested_origin": True}]),
        case(
            "tail_objects_after_arrays",
            [
                {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64},
                {
                    "nested_rekey": 10,
                    "prev_mac": "d" * 64,
                    "mac": "c" * 64,
                    "shape": objects,
                },
            ],
        ),
        case(
            "origin_arrays_rekey_objects",
            [
                {"nested_origin": True},
                {
                    "nested_rekey": 9,
                    "prev_mac": mac8,
                    "mac": "d" * 64,
                    "shape": objects,
                },
                _sql(_rekey_row(10, _names(own, own), "d" * 64)),
            ],
        ),
        case(
            "origin_objects_rekey_arrays",
            [
                {"nested_origin": True, "shape": objects},
                {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64},
                _sql(_rekey_row(10, _names(own, own), "d" * 64)),
            ],
        ),
        case(
            "origin_segments_objects_then_arrays",
            [
                {"unlink": ".origin"},
                {
                    "nested_rekey": 9,
                    "prev_mac": mac8,
                    "mac": "d" * 64,
                    "shape": objects,
                },
                {"nested_rekey": 10, "prev_mac": "d" * 64, "mac": "e" * 64},
                _sql(_rekey_row(11, _names(own, own), "e" * 64)),
            ],
        ),
        # The tail closed and logged, then the anchor's step raises.
        case(
            "tail_closed_then_origin_nested",
            [_sql(_rekey_row(9, _names("f" * 64), mac8)), {"nested_origin": True}],
        ),
        case(
            "tail_closed_then_segments_nested",
            [
                {"unlink": ".origin"},
                {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64},
                _sql(_rekey_row(10, _names("f" * 64), "d" * 64)),
            ],
        ),
        # An index another tokenizer spelled is refused after the upkeep,
        # which raises first.
        case(
            "drift_tail_nested",
            [drift, {"nested_rekey": 9, "prev_mac": mac8, "mac": "c" * 64}],
        ),
        case("drift_origin_nested", [drift, {"nested_origin": True}]),
        case(
            "tail_at_the_last_seq",
            [_sql(_rekey_row(2**63 - 1, _names("f" * 64), mac8))],
        ),
    ]


def _nested(shape: str | None = None) -> str:
    if shape == "objects":
        return '{"a":' * DEEP + "1" + "}" * DEEP
    return "[" * DEEP + "]" * DEEP


def _setup(recipe: list[dict[str, Any]], case: Path) -> None:
    """`gen_store_open.setup`, with this fixture's two nested ops and a
    padded anchor (``padded_origin``: its text up to the pad, then ``pad``
    times ``x`` and the closing ``"}``)."""
    for op in recipe:
        if "nested_rekey" in op:
            sql = _rekey_row(
                op["nested_rekey"], _nested(op.get("shape")), op["prev_mac"], op["mac"]
            )
            setup([_sql(sql)], case)
        elif "nested_origin" in op:
            (case / "keys" / f"{STORE_ID}.origin").write_text(
                '{"seq": ' + _nested(op.get("shape")) + "}", encoding="utf-8"
            )
        elif "padded_origin" in op:
            (case / "keys" / f"{STORE_ID}.origin").write_text(
                op["padded_origin"] + "x" * op["pad"] + '"}', encoding="utf-8"
            )
        else:
            setup([op], case)


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


class _Warnings(logging.Handler):
    """The upkeep's log records, as the logger receives them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.funcName in UPKEEP:
            self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Warnings]:
    handler = _Warnings()
    logger = logging.getLogger("bettermemory.store")
    logger.addHandler(handler)
    # Kept from the root's handlers: a name holding a lone surrogate
    # (`tail_surrogate`) is no text a test runner's log capture can carry.
    propagate, logger.propagate = logger.propagate, False
    try:
        yield handler
    finally:
        logger.propagate = propagate
        logger.removeHandler(handler)


def _arg(value: Any, root: Path) -> dict[str, Any]:
    if isinstance(value, BaseException):
        return {
            "exc": type(value).__name__,
            "sqlite": getattr(value, "sqlite_errorname", None),
        }
    if isinstance(value, bool):
        raise TypeError(value)
    if isinstance(value, int):
        return {"int": value}
    if isinstance(value, Path):
        return {"path": _scrub(str(value), root)}
    if isinstance(value, str):
        return text_field("text", value)
    raise TypeError(type(value))


def _warnings(records: list[logging.LogRecord], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "level": r.levelname,
            "msg": r.msg,
            "args": [_arg(a, root) for a in (r.args or ())],
        }
        for r in records
    ]


#: How `state` spells a text that is not UTF-8, before its bytes in hex
#: and a closing ``>`` (a case leaves such a row in the log, which
#: `gen_store_open.state` cannot read otherwise; the native test spells it
#: the same, `opened::NOT_UTF8`).
NOT_UTF8 = "<not UTF-8: "


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{NOT_UTF8}{raw.hex()}>"


def _spelled(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.text_factory = _text
    return conn


def state(case: Path, root: Path) -> dict[str, Any]:
    """`gen_store_open.state`, spelling a text that is not UTF-8 as
    `NOT_UTF8` and its bytes, and a key ring text past `LONG_TEXT` as
    ``text_sha256``."""
    real = sqlite3.connect
    with patch.object(sqlite3, "connect", lambda *a, **k: _spelled(real(*a, **k))):
        out = _open_state(case, root)
    for item in out.get("keys", []):
        text = item.get("text")
        if text is not None and len(text) > LONG_TEXT:
            del item["text"]
            item["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out


def _last_seq(case: Path) -> int:
    conn = sqlite3.connect(f"file:{case / 'memory.sqlite'}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT coalesce(max(seq), 0) FROM log").fetchone()[0])
    finally:
        conn.close()


def _rows_after(case: Path, seq: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{case / 'memory.sqlite'}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE seq > ? ORDER BY seq",
            (seq,),
        ).fetchall()
        # Appended rows are the open's: UTF-8 always.
    finally:
        conn.close()
    names = ("seq", "ts", "session", "kind", "payload", "prev_mac", "mac")
    return [dict(zip(names, row, strict=True)) for row in rows]


def _open(case: Path) -> None:
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys", allow_rekey=True)
    store.close()


@contextmanager
def _hooked(hook: str | None, case: Path) -> Iterator[None]:
    """The case's hook around its first open."""
    if hook == "busy":
        holder = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        real = Store._close_foreign_tail

        def released(self: Store) -> None:
            try:
                real(self)
            finally:
                if holder.in_transaction:
                    holder.execute("ROLLBACK")

        try:
            with patch.object(Store, "_close_foreign_tail", released):
                yield
        finally:
            holder.close()
    elif hook == "race":
        done = [False]

        def name_then_race(conn: sqlite3.Connection) -> str | None:
            named = last_rekey_name(conn)
            if not done[0]:
                done[0] = True
                _open(case)
            return named

        # `store` reads the name through its own module global.
        with patch("bettermemory.store.last_rekey_name", name_then_race):
            yield
    elif hook == "readonly_keys":
        os.chmod(case / "keys", 0o500)
        try:
            yield
        finally:
            os.chmod(case / "keys", 0o700)
    else:
        yield


def run_case(c: dict[str, Any], bases: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made(c["base"], bases, case)
        _setup(c["recipe"], case)
        before = state(case, root)
        seq = _last_seq(case)
        log = Ticker(LOG0, LOG_STEP)
        wall = Ticker(WALL0, WALL_STEP)
        outcome: dict[str, Any] = {"opened": True}
        with _captured() as first, fixed(log=log, wall=wall), _engine():
            try:
                with _hooked(c.get("hook"), case):
                    _open(case)
            except Exception as exc:  # noqa: BLE001 - the refusal is the record
                outcome = {
                    "opened": False,
                    "refusal": {
                        "class": type(exc).__name__,
                        "text": _scrub(str(exc), root),
                    },
                }
        if not outcome["opened"]:
            _close_left(root)
        after = state(case, root)
        appended = _rows_after(case, seq)
        stamps = log.stamps()
        second_log = Ticker(LOG0 + timedelta(hours=1), LOG_STEP)
        with (
            _captured() as second,
            fixed(log=second_log, wall=Ticker(WALL0, WALL_STEP)),
            _engine(),
        ):
            try:
                _open(case)
                second_outcome: dict[str, Any] = {"opened": True}
            except Exception as exc:  # noqa: BLE001 - the refusal is the record
                second_outcome = {"opened": False, "class": type(exc).__name__}
        if not second_outcome["opened"]:
            _close_left(root)
        again = state(case, root)
        record: dict[str, Any] = {
            **c,
            "before": {k: before.get(k) for k in ("master", "tables")},
            "outcome": outcome,
            "after": after,
            "appended": appended,
            "warnings": _warnings(first.records, root),
            "clock": stamps,
            "second": {
                "outcome": second_outcome,
                "changed": _changed(after, again),
                "warnings": _warnings(second.records, root),
                "clock": second_log.stamps(),
            },
        }
    return record


def _header(name: str) -> dict[str, Any]:
    return {
        "fixture": name,
        "engine_version": ENGINE,
        "store_id": STORE_ID,
        "pragmas": list(PRAGMAS),
        "deep": DEEP,
    }


def _skipped_as_root(c: dict[str, Any]) -> bool:
    """A ``readonly_keys`` case as root, where the mode refuses nothing."""
    if c.get("hook") != "readonly_keys" or sys.platform == "win32":
        return False
    return os.geteuid() == 0


def generate() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_upkeep")]
    lines += [run_case(c, bases) for c in cases(bases) if not _skipped_as_root(c)]
    return {"store_open_upkeep.jsonl": jsonl(lines)}


def generate_python() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_upkeep_python")]
    lines += [run_case(c, bases) for c in python_cases(bases)]
    return {"store_open_upkeep_python.jsonl": jsonl(lines)}


__all__ = ["cases", "generate", "generate_python", "python_cases"]
