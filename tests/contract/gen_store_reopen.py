"""Fixture: another connection to a store open for writing
(``store_reopen.jsonl``, ``store_reopen_python.jsonl``).

`Store.reopen` opens the store's file again (`Store.open` with
``allow_rekey`` False, as the owner's connection was not), so a worker
thread reads and records through its own connection: no claim of the key
ring, no upkeep, no restore rows, no digest; a present key binds the new
connection with it, any other state opens a reader (`_open_reader`) on
the writing connection; a later writer's minimum holds it to reading
(``PRAGMA query_only``) with a warning once a process; a tokenizer's drift
is said once a process (`store_index._tell_drift`) and the index left as
it stands. The native build (`WritableStore::reopen`, `open/reopen.rs`)
is held to this file.

Each case starts from a base of ``store_bases.json`` bound to its file,
opened by its owner (``allow_rekey=True``) as `gen_store_open_upkeep`
opens one, with the clocks fixed. Then the case's edits
(`gen_store_open_upkeep._setup`'s ops: the key file replaced or removed,
the ring bound to a copy, SQL run on another connection), then each of
the case's reopens in turn, its ``busy_timeout`` given (None: the
owner's), all of them open until the last, under the case's hook
(``busy``: another connection holds the write lock throughout). Recorded
per reopen: the outcome (or the class and text it raised), whether it
holds the key it writes with and whether it checks pointers, the
fingerprints of its key and of a reader's read key, the key state's name,
its busy timeout and SQLite's, ``query_only``, and every warning logged
under ``bettermemory`` at WARNING and above (the format string, and each
argument as ``str()`` with the scratch directory as ``<scratch>``). Then
which parts of the store and keys directory the reopens changed
(`gen_store_open._changed`: none, for every case here).

Gates: those of ``store_open_upkeep.jsonl``, for its reasons (Unicode: a
drift's warning names the tokenizer's fingerprint, and the digest's rows
are its tokens; POSIX only: modes, a resolved binding). The reopens a
RecursionError ends (`python_cases`: an anchor, a rekey payload or an
``index_rebuilt`` payload nested past where `json.loads` stops, which the
release and the system decide) are in the Python-gated sibling.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bettermemory.log import fingerprint
from bettermemory.store import Store

from ._common import jsonl, text_field
from ._fixed import Ticker, fixed
from .gen_store_files import load_bases
from .gen_store_open import _changed, _engine, _made, _row_mac, _scrub
from .gen_store_open_restore import _entry, _index_row, _record, _setup_nested
from .gen_store_open_upkeep import (
    BIND,
    LOG0,
    LOG_STEP,
    WALL0,
    WALL_STEP,
    _header,
    _names,
    _rekey_row,
    _setup,
    _sql,
    state,
)

#: A key that is not the store's: 32 bytes of ``0xee``.
OTHER_KEY = "ee" * 32
#: The tokenizer an inserted ``index_rebuilt`` row names.
STALE_TOKENIZER = "5" * 64


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: name, base, edits after the owner's open, the reopens'
    busy timeouts, an optional hook."""
    intact = bases["intact"]
    mac8 = _row_mac(intact, 8)
    drift = _sql(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES "
        "(9, '2001-02-03T04:05:06.000000Z', NULL, 'index_rebuilt', "
        f"'{{\"new_fingerprint\":\"{STALE_TOKENIZER}\"}}', '{mac8}', '{'d' * 64}');"
    )
    later = _sql(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('min_writer_revision', '99');"
    )
    missing = {"unlink": ".key"}

    def case(name: str, edits: list[Any], reopens: list[Any], **kw: Any) -> dict:
        return {
            "case": name,
            "base": "intact",
            "recipe": [BIND],
            "edits": edits,
            "reopens": reopens,
            **kw,
        }

    return [
        case("plain", [], [None, 0, 2.5]),
        case("plain_busy", [], [None], hook="busy"),
        case("busy_timeout_negative", [], [-1.0]),
        case("busy_timeout_fraction", [], [0.0005]),
        case("key_replaced", [{"write": ".key", "hex": OTHER_KEY}], [None]),
        case(
            "key_replaced_no_anchor",
            [{"write": ".key", "hex": OTHER_KEY}, {"unlink": ".origin"}],
            [None],
        ),
        case("key_removed", [missing], [None]),
        case(
            "bound_elsewhere",
            [{"copy_store": "other/memory.sqlite"}, {"bind": "other/memory.sqlite"}],
            [None],
        ),
        case(
            "bound_to_a_store_gone",
            [{"mkdir": "gone"}, {"bind": "gone/memory.sqlite"}],
            [None],
        ),
        case("unbound", [{"unlink": ".path"}], [None]),
        case(
            "unclaimed",
            [
                {"unlink": ".path"},
                {
                    "write": ".origin",
                    "hex": ('{"mac": "' + "0" * 64 + '", "seq": 1}').encode().hex(),
                },
            ],
            [None],
        ),
        case("later_writer", [later], [None, None]),
        case("drift", [drift], [None, None]),
        case("drift_without_the_key", [drift, missing], [None]),
        case("drift_later_writer", [drift, later], [None]),
        # `cannot_write` asks for the key before `query_only`.
        case("drift_without_the_key_later_writer", [drift, missing, later], [None]),
        # A name with a lone surrogate reaches the warning as it is.
        case(
            "drift_surrogate",
            [_sql(_index_row(9, '{"new_fingerprint": "\\ud800abc"}', mac8, "d" * 64))],
            [None],
        ),
        # A payload stored as bytes is read as `json.loads` reads bytes.
        case(
            "drift_blob_payload",
            [
                _sql(
                    "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
                    "VALUES (9, '2001-02-03T04:05:06.000000Z', NULL, 'index_rebuilt', "
                    f"X'{json.dumps({'new_fingerprint': STALE_TOKENIZER}).encode().hex()}', "
                    f"'{mac8}', '{'d' * 64}');"
                )
            ],
            [None],
        ),
        # No row and no meta names a tokenizer: the drift names none.
        case(
            "no_tokenizer_named",
            [
                _sql(
                    "UPDATE log SET payload = json_remove(payload, "
                    "'$.tokenizer_fingerprint') WHERE kind = 'store_created'; "
                    "DELETE FROM meta WHERE key = 'tokenizer_fingerprint';"
                )
            ],
            [None, None],
        ),
        case(
            "restore_pending",
            [_record(_entry(8, mac8))],
            [None],
        ),
        case(
            "foreign_tail",
            [_sql(_rekey_row(9, _names("f" * 64), mac8))],
            [None],
        ),
        case("no_anchor", [{"unlink": ".origin"}], [None]),
        case(
            "rotation_pending_not_named",
            [{"write": ".key.pending", "hex": "cc" * 32}],
            [None],
        ),
    ]


def python_cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """The reopens a RecursionError ends: the reader's key kept for
    checking (`_open_reader`) reads the anchor, then the rekey payloads
    (`chain_segments`); the tokenizer's read (`stored_fingerprint`) reads
    the newest ``index_rebuilt`` payload. The text is `json.loads`'s for
    the text that raised."""
    mac8 = _row_mac(bases["intact"], 8)
    other = {"write": ".key", "hex": OTHER_KEY}

    def case(name: str, edits: list[Any]) -> dict[str, Any]:
        return {
            "case": name,
            "base": "intact",
            "recipe": [BIND],
            "edits": edits,
            "reopens": [None],
        }

    rekey = {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64}
    index = {"nested_index": 9, "prev_mac": mac8, "mac": "d" * 64}
    return [
        case("reader_origin_nested", [other, {"nested_origin": True}]),
        case(
            "reader_origin_nested_objects",
            [other, {"nested_origin": True, "shape": "objects"}],
        ),
        case("reader_segments_nested", [other, {"unlink": ".origin"}, rekey]),
        case(
            "reader_segments_nested_objects",
            [other, {"unlink": ".origin"}, {**rekey, "shape": "objects"}],
        ),
        case("index_nested", [index]),
        case("index_nested_objects", [{**index, "shape": "objects"}]),
        case("index_nested_reader", [other, index]),
    ]


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Warnings]:
    handler = _Warnings()
    logger = logging.getLogger("bettermemory")
    logger.addHandler(handler)
    propagate, logger.propagate = logger.propagate, False
    try:
        yield handler
    finally:
        logger.propagate = propagate
        logger.removeHandler(handler)


def _warnings(records: list[logging.LogRecord], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "level": r.levelname,
            "msg": r.msg,
            "args": [text_field("text", _scrub(str(a), root)) for a in (r.args or ())],
        }
        for r in records
    ]


@contextmanager
def _hooked(hook: str | None, case: Path) -> Iterator[None]:
    if hook == "busy":
        holder = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            holder.execute("ROLLBACK")
            holder.close()
    else:
        yield


def _fp(key: bytes | None) -> str | None:
    return None if key is None else fingerprint(key)


def _held(other: Store) -> dict[str, Any]:
    conn = other.conn
    return {
        "has_key": other.has_key,
        "can_verify": other.can_verify,
        "key": _fp(other._key),
        "read_key": _fp(other._read_key),
        "key_state": other.key_state.state,
        "busy_timeout": other.busy_timeout,
        "sqlite_busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "query_only": conn.execute("PRAGMA query_only").fetchone()[0],
        "in_transaction": conn.in_transaction,
    }


def run_case(c: dict[str, Any], bases: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made(c["base"], bases, case)
        _setup(c["recipe"], case)
        with (
            fixed(log=Ticker(LOG0, LOG_STEP), wall=Ticker(WALL0, WALL_STEP)),
            _engine(),
        ):
            owner = Store.open(
                case / "memory.sqlite", keys_dir=case / "keys", allow_rekey=True
            )
        opened: list[Store] = []
        reopens: list[dict[str, Any]] = []
        try:
            _setup_nested(c["edits"], case)
            before = state(case, root)
            with _hooked(c.get("hook"), case):
                for wait in c["reopens"]:
                    out: dict[str, Any]
                    with _captured() as said, _engine():
                        try:
                            other = owner.reopen(busy_timeout=wait)
                        except Exception as exc:  # noqa: BLE001 - the refusal is the record
                            out = {
                                "opened": False,
                                "refusal": text_field("text", _scrub(str(exc), root))
                                | {"class": type(exc).__name__},
                            }
                        else:
                            opened.append(other)
                            out = {"opened": True, **_held(other)}
                    out["warnings"] = _warnings(said.records, root)
                    reopens.append(out)
            after = state(case, root)
        finally:
            for other in opened:
                other.close()
            owner.close()
        return {
            **c,
            "before": {k: before.get(k) for k in ("master", "tables")},
            "reopened": reopens,
            "changed": _changed(before, after),
        }


def generate() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_reopen")]
    lines += [run_case(c, bases) for c in cases(bases)]
    return {"store_reopen.jsonl": jsonl(lines)}


def generate_python() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_reopen_python")]
    lines += [run_case(c, bases) for c in python_cases(bases)]
    return {"store_reopen_python.jsonl": jsonl(lines)}


__all__ = ["cases", "generate", "generate_python", "python_cases"]
