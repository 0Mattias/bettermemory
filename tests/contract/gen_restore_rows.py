"""Fixture: a restore's late ``store_restored`` row (`restore_rows`):
``restore_rows.jsonl`` and its Python-gated sibling
``restore_rows_python.jsonl``.

Each case starts from the store `Store.create` makes under
``store_create.json``'s fixed values, opened as the owner
(``allow_rekey=True``) under the fixture's engine version. Only then are
the case's log rows inserted on the store's connection (raw rows: their
kinds, stamps and MACs are the case's, and no chain is asked of them) and
its restore record written beside the head, as `backup._append_record`
writes one (``json.dumps(document, indent=2, sort_keys=True)`` and a line
feed, its ``.lock`` beside it), or as a hostile file. The record comes
after the open, so the open's own `restore_rows.settle` finds none: that
call is not this fixture's (`gen_store_open_restore` holds the writing
open's settle).

Each step may first change the store (SQL, the record, the keys
directory's mode), then calls `restore_rows.settle`, `append_missing`
(with no entries, or the step's), or `append_for` with the step's entry,
under the fixed log and wall clocks (`_fixed.fixed`). A step may name a
hook around its call: ``query_only`` (``PRAGMA query_only`` on the
store's connection), ``busy`` (another connection holds the write lock;
the store's busy timeout is 100 ms for the call) or ``readonly_keys``
(the keys directory 0500; skipped as root). Recorded per step: the
answer (or the class the call raised), every warning the module logged
(the format, and each argument: a path with the scratch directory as
``<scratch>``, an exception's class and SQLite error name, anything else
as ``str()`` through `_common.text_field`), the log rows the step
appended (every column), the record's text after it (``unchanged`` when
the step left it as it was), the head file's text and the clocks read.

Gates: those of ``store_open_upkeep.jsonl``, for its reasons (Unicode:
the created store's digest is the tokenizer's; POSIX only: modes, a
``flock``). The Python-gated sibling holds the cases whose answer is
where `json.loads` stops recursing (a record and a log payload nested
100,000 deep), which follows the interpreter and the system.
"""

from __future__ import annotations

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

from bettermemory import restore_rows
from bettermemory.backup import RESTORE_RECORD_FORMAT
from bettermemory.store import Store

from ._common import jsonl, text_field
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, _engine, _made
from .gen_store_write import _last_seq, log_rows

LOG0 = datetime(2003, 4, 5, 6, 7, 8, 123456, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1)
WALL0 = datetime(2004, 5, 6, 7, 8, 9, 654321, tzinfo=timezone.utc)
WALL_STEP = timedelta(seconds=1)

#: When the cases' restores ran, and stamps after and before it.
RESTORED_AT = "2002-01-01T00:00:00.000000Z"
LATE = "2002-06-01T00:00:00.000000Z"
EARLY = "2001-06-01T00:00:00.000000Z"

#: How deep the Python-gated cases nest.
DEEP = 100_000

RECORD = f"{STORE_ID}.backup_restore.json"
_MISSING = object()


def _entry(mac1: str, **fields: Any) -> dict[str, Any]:
    """A restore's entry as `backup` writes it, cut to the fields these
    functions read, with `fields` over it (`_MISSING` removes one)."""
    entry: dict[str, Any] = {
        "kind": "store_restored",
        "row": None,
        "restored_at": RESTORED_AT,
        "bundle": "/b/1",
        "store_id": STORE_ID,
        "head_before": None,
        "head_after": {"seq": 1, "mac": mac1},
        "log_end_before": None,
    }
    entry.update(fields)
    return {k: v for k, v in entry.items() if v is not _MISSING}


def _record(*entries: Any) -> dict[str, Any]:
    document = {"format": RESTORE_RECORD_FORMAT, "restores": list(entries)}
    return {"text": json.dumps(document, indent=2, sort_keys=True) + "\n"}


def _row(seq: int, kind: Any, ts: str = LATE, mac: Any = None, payload: Any = "{}"):
    """A log row: kind and mac a text, or ``{"blob": hex}``."""
    return {
        "seq": seq,
        "ts": ts,
        "kind": kind,
        "payload": payload,
        "mac": mac or f"m{seq}",
    }


def _blob(raw: bytes) -> dict[str, str]:
    return {"blob": raw.hex()}


def _logged_payload(entry: dict[str, Any], engine: str = "8.0.0") -> str:
    """The payload `log.append_history_replaced` gives `entry`'s row."""
    payload = {k: v for k, v in entry.items() if k not in ("row", "row_withheld")}
    payload["kind"] = "store_restored"
    payload["engine_version"] = engine
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _call(name: str, **more: Any) -> dict[str, Any]:
    return {"call": name, **more}


SETTLE = _call("settle")


def cases(mac1: str) -> list[dict[str, Any]]:
    """Each: name, rows, record, whether its lock file is there, steps."""

    def e(**fields: Any) -> dict[str, Any]:
        return _entry(mac1, **fields)

    def case(name, steps, record=None, rows=(), lock=True):
        return {
            "case": name,
            "rows": list(rows),
            "record": record,
            "lock": lock,
            "steps": steps,
        }

    plain = e()
    control = [_row(2, "index_rebuilt"), _row(3, "capture_close")]
    early = [_row(2, "index_rebuilt", EARLY), _row(3, "capture_open", EARLY)]
    no_kind = e(kind=_MISSING)
    tru, one = e(restored_at=True, bundle="/b/t"), e(restored_at=1, bundle="/b/1")
    big = e(head_after={"seq": 2**64, "mac": "x"})
    return [
        case("no_record", [SETTLE, _call("append_missing")]),
        case("record_not_json", [SETTLE, _call("append_missing")], {"text": "{"}),
        case(
            "record_over_cap",
            [SETTLE],
            {"parts": [['{"restores": []}', 1], [" ", restore_rows.RECORD_CAP_BYTES]]},
        ),
        case(
            "record_at_the_cap", [SETTLE], {"parts": [["[]", 1], [" ", (1 << 20) - 2]]}
        ),
        case("record_a_directory", [SETTLE], {"dir": True}),
        case("record_bad_utf16", [SETTLE], {"hex": "fffe00"}),
        case(
            "record_long_int",
            [SETTLE],
            {"parts": [['{"restores": [', 1], ["1", 4301], ["]}", 1]]},
        ),
        case("record_not_a_dict", [SETTLE], {"text": "[1, 2]"}),
        case("restores_not_a_list", [SETTLE], {"text": '{"restores": {"a": 1}}'}),
        case(
            "filtered",
            [SETTLE, SETTLE],
            _record(
                e(kind="history_accepted"),
                e(kind=_MISSING, accepted_history=True),
                e(store_id="f" * 32),
                e(store_id=_MISSING),
                e(row={"seq": 1, "mac": mac1}),
                e(row=_MISSING),
                "a string",
                e(kind="bogus", bundle="/b/bogus"),
            ),
        ),
        case(
            "record_utf16",
            [SETTLE],
            {"hex": _record(plain)["text"].encode("utf-16").hex()},
        ),
        case(
            "record_utf8_bom",
            [SETTLE],
            {"hex": "efbbbf" + _record(plain)["text"].encode().hex()},
        ),
        case("head_after_missing", [SETTLE, SETTLE], _record(e(head_after=_MISSING))),
        case(
            "head_after_absent",
            [SETTLE, SETTLE],
            _record(e(head_after={"seq": 9, "mac": "x"})),
        ),
        case(
            "head_after_other_mac",
            [SETTLE, SETTLE],
            _record(e(head_after={"seq": 1, "mac": "0" * 64})),
        ),
        case(
            "head_after_bool_seq",
            [SETTLE],
            _record(e(head_after={"seq": True, "mac": mac1})),
        ),
        case(
            "head_after_blob_mac",
            [SETTLE],
            _record(e(head_after={"seq": 2, "mac": "b'ab'"})),
            [_row(2, "index_rebuilt", mac=_blob(b"ab")), _row(3, "capture_open")],
        ),
        case(
            "replaced_by_mac",
            [SETTLE, SETTLE],
            _record(e(head_before={"seq": 3, "mac": "m3"})),
            control,
        ),
        case(
            "replaced_head_not_here",
            [SETTLE],
            _record(e(head_before={"seq": 3, "mac": "other"})),
            early,
        ),
        case("clock_no_checkpoint", [SETTLE, SETTLE], _record(plain), early),
        case(
            "clock_log_end",
            [SETTLE],
            _record(e(log_end_before={"seq": 7, "path": "/s/memory.sqlite"})),
            early,
        ),
        case(
            "clock_head_behind",
            [SETTLE],
            _record(e(head_before={"seq": 1, "mac": mac1})),
            early,
        ),
        case(
            "unreadable_times",
            [SETTLE],
            _record(e(), e(restored_at=5, bundle="/b/2")),
            [_row(2, "index_rebuilt", "yesterday"), _row(3, "capture_open", EARLY)],
        ),
        case(
            "record_past_head",
            [
                SETTLE,
                SETTLE,
                _call(
                    "settle",
                    before=[{"sql": "UPDATE log SET mac = 'm3b' WHERE seq = 3"}],
                ),
                _call(
                    "settle",
                    before=[
                        {"sql": "UPDATE log SET mac = 'zz' WHERE seq = 1"},
                        {"sql": "UPDATE log SET mac = 'm3c' WHERE seq = 3"},
                    ],
                ),
            ],
            _record(plain),
            [_row(2, "index_rebuilt"), _row(3, "memory_put")],
        ),
        case(
            "control_rows_only",
            [SETTLE],
            _record(plain),
            [
                _row(2, "index_rebuilt"),
                _row(3, "capture_close"),
                _row(4, "store_restored", payload='{"other": 1}'),
                _row(5, "history_accepted"),
                _row(6, "key_adopted"),
            ],
        ),
        case(
            "already_logged",
            [SETTLE, SETTLE],
            _record(plain),
            [
                _row(2, "store_restored", payload=_logged_payload(plain)),
                _row(3, "memory_put"),
            ],
        ),
        case(
            "logged_payload_unread",
            [SETTLE],
            _record(plain),
            [
                _row(2, "store_restored", payload="nope"),
                _row(3, "store_restored", payload="[1]"),
                _row(4, "store_restored", payload=_blob(b"\xff")),
            ],
        ),
        case(
            "logged_blob_payload",
            [SETTLE],
            _record(plain),
            [_row(2, "store_restored", payload=_blob(_logged_payload(plain).encode()))],
        ),
        case(
            "no_kind_appended_twice",
            [SETTLE],
            _record(no_kind),
            [_row(2, "store_restored", payload=_logged_payload(no_kind))],
        ),
        case(
            "withheld_stands",
            [SETTLE],
            _record(
                e(
                    row_withheld={
                        "seq": 2,
                        "kind": "memory_put",
                        "mac": "m2",
                        "because": "record",
                    }
                )
            ),
            [_row(2, "memory_put")],
        ),
        case(
            "withheld_gone",
            [SETTLE],
            _record(
                e(
                    row_withheld={
                        "seq": 2,
                        "kind": "memory_put",
                        "mac": "gone",
                        "because": "record",
                    }
                )
            ),
            [_row(2, "index_rebuilt")],
        ),
        case(
            "withheld_other_shapes",
            [SETTLE],
            _record(
                e(row_withheld={"seq": 2, "mac": "m2"}),
                e(
                    row_withheld={"seq": "2", "mac": "m2", "because": "record"},
                    restored_at="2",
                ),
                e(
                    row_withheld={"seq": 2, "mac": 5, "because": "record"},
                    restored_at="3",
                ),
                e(
                    row_withheld={"seq": 9, "mac": None, "because": "elsewhere"},
                    restored_at="4",
                ),
            ),
            [_row(2, "index_rebuilt")],
        ),
        case(
            "record_unwritable",
            [_call("settle", hook="readonly_keys"), SETTLE],
            _record(e(head_after=_MISSING)),
        ),
        case(
            "record_unwritable_after_append",
            [_call("settle", hook="readonly_keys"), SETTLE],
            _record(plain),
        ),
        case(
            "lock_unmade",
            [_call("settle", hook="readonly_keys"), SETTLE],
            _record(plain),
            lock=False,
        ),
        case(
            "lock_unmade_withheld",
            [
                _call("settle", hook="readonly_keys"),
                _call("append_missing", hook="readonly_keys"),
            ],
            _record(e(head_after=_MISSING)),
            lock=False,
        ),
        case(
            "query_only",
            [
                _call("settle", hook="query_only"),
                _call("append_missing", hook="query_only"),
                _call("append_for", entry=json.dumps(plain), hook="query_only"),
            ],
            _record(plain),
        ),
        case(
            "query_only_unread_record",
            [
                _call("settle", hook="query_only"),
                _call("append_for", entry=json.dumps(plain), hook="query_only"),
            ],
            {"text": "{"},
        ),
        case(
            "append_for_own_missing",
            [_call("append_for", entry=json.dumps(plain))],
            _record(),
        ),
        case(
            "append_for_own_in_record",
            [_call("append_for", entry=json.dumps(plain))],
            _record(plain),
        ),
        case(
            "append_for_after_earlier",
            [_call("append_for", entry=json.dumps(e(restored_at=LATE, bundle="/b/2")))],
            _record(plain, e(restored_at=LATE, bundle="/b/2")),
        ),
        case(
            "append_for_one_and_float",
            [_call("append_for", entry=json.dumps(e(restored_at=1.0)))],
            _record(e(restored_at=1)),
        ),
        case(
            "append_for_nan",
            [_call("append_for", entry=json.dumps(e(restored_at=float("nan"))))],
            _record(e(restored_at=float("nan"))),
        ),
        case(
            "append_for_nan_in_list",
            [_call("append_for", entry=json.dumps(e(restored_at=[float("nan")])))],
            _record(e(restored_at=[float("nan")])),
        ),
        case(
            "append_missing_given",
            [
                _call(
                    "append_missing", entries=json.dumps([e(bundle="/b/given"), plain])
                )
            ],
            _record(plain),
        ),
        case("true_and_one", [SETTLE], _record(tru, one)),
        case("twins_appended", [SETTLE], _record(plain, plain)),
        case(
            "twins_withheld",
            [SETTLE],
            _record(e(head_after=_MISSING), e(head_after=_MISSING)),
        ),
        case(
            "busy",
            [_call("settle", hook="busy"), _call("append_missing", hook="busy")],
            _record(plain),
        ),
        case("seq_overflow", [SETTLE, _call("append_missing")], _record(big)),
        case(
            "odd_values",
            [SETTLE],
            _record(e(head_after=_MISSING, restored_at={"a": [1, 2.5]}, bundle=None)),
        ),
        case(
            "blob_kind_record",
            [SETTLE],
            _record(plain),
            [_row(2, _blob(b"k"))],
        ),
        # The first row past the head stamped at the restore's very time
        # is not written before it.
        case(
            "clock_row_at_restored_at",
            [SETTLE],
            _record(plain),
            [_row(2, "index_rebuilt", RESTORED_AT)],
        ),
        # A withheld entry the record does not hold is not said.
        case(
            "withheld_entry_not_in_record",
            [
                _call(
                    "append_missing",
                    entries=json.dumps(
                        [
                            e(
                                head_after=_MISSING,
                                bundle="/b/elsewhere",
                                restored_at="2009-01-01T00:00:00Z",
                            )
                        ]
                    ),
                )
            ],
            _record(plain),
        ),
        # The early first row is the entry's own logged row: the clock
        # withholds the entry before the log is looked through for it.
        case(
            "clock_row_is_the_logged_row",
            [SETTLE],
            _record(plain),
            [_row(2, "store_restored", EARLY, payload=_logged_payload(plain))],
        ),
        # An entry sharing a pending one's place but not its bundle is
        # not appended beside it.
        case(
            "append_for_same_place_other_bundle",
            [_call("append_for", entry=json.dumps(e(bundle="/b/other")))],
            _record(plain),
        ),
        # The clock's words name the bundle's head, not head_before's.
        case(
            "clock_head_before_at_seq_0",
            [SETTLE],
            _record(e(head_before={"seq": 0, "mac": "x"})),
            early,
        ),
    ]


def python_cases(mac1: str) -> list[dict[str, Any]]:
    """The cases whose answer is where `json.loads` stops recursing."""
    plain = _entry(mac1)
    nested = [["[", DEEP], ["]", DEEP]]
    return [
        {
            "case": "record_nested",
            "rows": [],
            "record": {"parts": [['{"restores": ', 1], *nested, ["}", 1]]},
            "lock": True,
            "steps": [SETTLE, _call("append_missing")],
        },
        {
            "case": "payload_nested",
            "rows": [_row(2, "store_restored", payload={"parts": nested})],
            "record": _record(plain),
            "lock": True,
            "steps": [SETTLE, _call("append_missing")],
        },
    ]


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


def _text(spec: Any) -> str | bytes:
    """A text a case spells: as itself, by its parts (each text repeated
    so many times), or ``{"blob": hex}``."""
    if isinstance(spec, str):
        return spec
    if "blob" in spec:
        return bytes.fromhex(spec["blob"])
    if "text" in spec:
        return spec["text"]
    return "".join(text * count for text, count in spec["parts"])


def _insert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, NULL, ?, ?, 'p', ?)",
        (
            row["seq"],
            row["ts"],
            _text(row["kind"]),
            _text(row["payload"]),
            _text(row["mac"]),
        ),
    )


def _put_record(path: Path, spec: dict[str, Any] | None) -> None:
    if spec is None:
        return
    if spec.get("dir"):
        path.mkdir()
    elif "hex" in spec:
        path.write_bytes(bytes.fromhex(spec["hex"]))
    else:
        text = _text(spec)
        assert isinstance(text, str)
        path.write_bytes(text.encode("utf-8"))


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Warnings]:
    handler = _Warnings()
    logger = logging.getLogger("bettermemory.restore_rows")
    logger.addHandler(handler)
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
    if isinstance(value, Path):
        return {"path": str(value).replace(str(root), "<scratch>", 1)}
    return text_field("text", str(value))


def _warnings(records: list[logging.LogRecord], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "level": r.levelname,
            "msg": r.msg,
            "args": [_arg(a, root) for a in (r.args or ())],
        }
        for r in records
    ]


@contextmanager
def _hooked(hook: str | None, store: Store, case: Path) -> Iterator[None]:
    if hook == "query_only":
        store.conn.execute("PRAGMA query_only = ON")
        try:
            yield
        finally:
            store.conn.execute("PRAGMA query_only = OFF")
    elif hook == "busy":
        holder = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        store.conn.execute("PRAGMA busy_timeout = 100")
        try:
            yield
        finally:
            store.conn.execute("PRAGMA busy_timeout = 5000")
            holder.execute("ROLLBACK")
            holder.close()
    elif hook == "readonly_keys":
        # The head checkpoint the append's commit cannot write is the
        # store's warning, not this module's: kept from the root's
        # handlers, so a run prints nothing.
        store_log = logging.getLogger("bettermemory.store")
        quiet = logging.NullHandler()
        store_log.addHandler(quiet)
        propagate, store_log.propagate = store_log.propagate, False
        os.chmod(case / "keys", 0o500)
        try:
            yield
        finally:
            os.chmod(case / "keys", 0o700)
            store_log.propagate = propagate
            store_log.removeHandler(quiet)
    else:
        yield


def _row_out(row: Any) -> Any:
    return None if row is None else {"seq": row["seq"], "mac": row["mac"]}


def _call_it(store: Store, step: dict[str, Any]) -> Any:
    name = step["call"]
    if name == "settle":
        restore_rows.settle(store)
        return None
    if name == "append_for":
        return _row_out(restore_rows.append_for(store, json.loads(step["entry"])))
    entries = json.loads(step["entries"]) if "entries" in step else None
    return [
        [json.dumps(entry, sort_keys=True), _row_out(row)]
        for entry, row in restore_rows.append_missing(store, entries)
    ]


def _read(path: Path) -> str | None:
    return path.read_text(encoding="latin-1") if path.is_file() else None


def run_case(c: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        keys = case / "keys"
        record = keys / RECORD
        head = keys / f"{STORE_ID}.head"
        with _engine():
            store = Store.open(file, keys_dir=keys, allow_rekey=True)
        steps: list[dict[str, Any]] = []
        try:
            for row in c["rows"]:
                _insert(store.conn, row)
            _put_record(record, c["record"])
            if c["lock"]:
                (keys / f"{RECORD}.lock").touch(mode=0o600)
            for i, step in enumerate(c["steps"]):
                for op in step.get("before", []):
                    store.conn.execute(op["sql"])
                before = _last_seq(file)
                text_before = _read(record)
                log = Ticker(LOG0 + timedelta(minutes=i), LOG_STEP)
                wall = Ticker(WALL0 + timedelta(minutes=i), WALL_STEP)
                out: dict[str, Any] = {}
                with _captured() as said, fixed(log=log, wall=wall), _engine():
                    with _hooked(step.get("hook"), store, case):
                        try:
                            out["answer"] = _call_it(store, step)
                        except Exception as exc:  # noqa: BLE001 - the class is the record
                            out["raised"] = type(exc).__name__
                after = _read(record)
                steps.append(
                    {
                        **step,
                        **out,
                        "warnings": _warnings(said.records, root),
                        "rows": log_rows(file, before),
                        "record_after": "unchanged" if after == text_before else after,
                        "head": head.read_text(encoding="utf-8")
                        if head.is_file()
                        else None,
                        "log_clock": log.stamps(),
                        "wall_clock": wall.stamps(),
                        "in_transaction": store.conn.in_transaction,
                    }
                )
        finally:
            store.close()
    return {**c, "steps": steps}


def _mac1() -> str:
    with tempfile.TemporaryDirectory() as raw:
        case = Path(raw) / "c"
        _made("created", {}, case)
        conn = sqlite3.connect(case / "memory.sqlite")
        try:
            return str(conn.execute("SELECT mac FROM log WHERE seq = 1").fetchone()[0])
        finally:
            conn.close()


def _skipped_as_root(c: dict[str, Any]) -> bool:
    """A case with a ``readonly_keys`` step as root, where the mode refuses
    nothing."""
    if sys.platform == "win32" or os.geteuid() != 0:
        return False
    return any(s.get("hook") == "readonly_keys" for s in c["steps"])


def _header(name: str) -> dict[str, Any]:
    return {
        "fixture": name,
        "engine_version": ENGINE,
        "store_id": STORE_ID,
        "result": (
            "each step: settle, append_missing or append_for on the store, after "
            "its edits and under its hook; the answer or the class raised, the "
            "warnings (format and arguments), the rows appended, the record's "
            "text after, the head's text, the clocks read"
        ),
    }


def generate() -> dict[str, bytes]:
    with world():
        mac1 = _mac1()
        lines: list[Any] = [_header("restore_rows")]
        lines += [run_case(c) for c in cases(mac1) if not _skipped_as_root(c)]
    return {"restore_rows.jsonl": jsonl(lines)}


def generate_python() -> dict[str, bytes]:
    with world():
        mac1 = _mac1()
        lines: list[Any] = [_header("restore_rows_python")]
        lines += [run_case(c) for c in python_cases(mac1)]
    return {"restore_rows_python.jsonl": jsonl(lines)}


__all__ = ["cases", "generate", "generate_python", "python_cases"]
