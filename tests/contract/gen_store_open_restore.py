"""Fixture: the owner's writing open settling a restore's late rows
(``store_open_restore.jsonl``, ``store_open_restore_python.jsonl``).

`store_open._open_connected` calls `restore_rows.settle` after the origin
anchor's upkeep on each of the owner's three paths: the key present (after
`Store._close_foreign_tail` and `Store._settle_origin`), a key that signs a
segment (after `Store._reclaim` and `_settle_origin`), and a new key (after
`_open_rekeyed`'s row and `Store._take_over_origin`). The native build
makes the same calls (`store::open`, `open_for_writing_mut`) and is held to
this file.

Each case starts from a base of ``store_bases.json`` with a recipe of
edits (`gen_store_open_upkeep._setup`'s ops), a restore record among
them (``{"write": ".backup_restore.json", ...}``, as `backup` writes one,
with entries built as `gen_restore_rows._entry` builds them), bound to its
file, and opens it as `gen_store_open_rekey` does: the log's and the wall
clock fixed, the key draw fixed (`gen_store_open_rekey.drawn_key`). A case
may name the hook ``readonly_keys`` (the keys directory 0500 for the first
open; skipped as root). Recorded as `gen_store_open_rekey` records a case
(the outcome, the after-state with the record's text, the rows appended,
the clock, the keys drawn, a second open), with the warnings of both
`bettermemory.store` (the upkeep's, the rekey's, the head checkpoint's)
and `bettermemory.restore_rows`, in the order they were logged: an
argument of the latter is a path, an exception's class and SQLite error
name, or ``str()`` of the value (`gen_restore_rows._arg`).

Gates: those of ``store_open_rekey.jsonl``, for its reasons (Unicode: the
digest's rows are the tokenizer's; POSIX only: modes, a resolved binding,
``flock``). The cases whose answer is an interpreter's text
(`python_cases`: CPython's `sqlite3` OverflowError, where `json.loads`
stops recursing) are in the Python-gated sibling.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import log as blog
from bettermemory.backup import RESTORE_RECORD_FORMAT

from ._common import jsonl
from .gen_log import STORE_ID
from .gen_restore_rows import _arg as _restore_arg
from .gen_store_files import load_bases
from .gen_store_open import _changed, _engine, _made, _row_mac
from .gen_store_open_rekey import LOGGED, _attempt, _Draws, _hooked, _meta, _open
from .gen_store_open_upkeep import (
    BIND,
    LOG0,
    LOG_STEP,
    WALL0,
    WALL_STEP,
    _arg,
    _fp,
    _header,
    _last_seq,
    _names,
    _rekey_row,
    _nested,
    _q,
    _rows_after,
    _setup,
    _skipped_as_root,
    _sql,
    state,
)
from ._fixed import Ticker, fixed

#: When the cases' restores ran: before every base row's stamp.
RESTORED_AT = "2000-01-01T00:00:00.000000Z"
RECORD = ".backup_restore.json"


def _entry(seq: int, mac: str, **fields: Any) -> dict[str, Any]:
    """A restore's entry with its row still null, putting back the head
    at `seq` (`gen_restore_rows._entry`)."""
    return {
        "kind": "store_restored",
        "row": None,
        "restored_at": RESTORED_AT,
        "bundle": "/b/1",
        "store_id": STORE_ID,
        "head_before": None,
        "head_after": {"seq": seq, "mac": mac},
        "log_end_before": None,
        **fields,
    }


def _record(*entries: Any) -> dict[str, Any]:
    document = {"format": RESTORE_RECORD_FORMAT, "restores": list(entries)}
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    return {"write": RECORD, "hex": text.encode("utf-8").hex()}


def _index_row(seq: int, payload: str, prev_mac: str, mac: str) -> str:
    """An ``index_rebuilt`` row whose payload is `payload`."""
    return (
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES "
        f"({seq}, '2001-02-03T04:05:06.000000Z', NULL, 'index_rebuilt', "
        f"{_q(payload)}, {_q(prev_mac)}, {_q(mac)});"
    )


def _setup_nested(recipe: list[dict[str, Any]], case: Path) -> None:
    """`gen_store_open_upkeep._setup`, with an ``index_rebuilt`` row nested
    past where `json.loads` stops (``nested_index``: its seq, ``prev_mac``,
    ``mac`` and ``shape`` as ``nested_rekey`` takes them)."""
    for op in recipe:
        if "nested_index" in op:
            payload = _nested(op.get("shape"))
            sql = _index_row(op["nested_index"], payload, op["prev_mac"], op["mac"])
            _setup([_sql(sql)], case)
        else:
            _setup([op], case)


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: name, base, recipe, an optional hook."""
    intact, rekeyed = bases["intact"], bases["rekeyed"]
    mac1, mac8 = _row_mac(intact, 1), _row_mac(intact, 8)
    mac10 = _row_mac(rekeyed, 10)
    fp1 = _fp(rekeyed["keys"][".key"])
    last = _record(_entry(8, mac8))
    missing = {"unlink": ".key"}
    tail_f = _sql(_rekey_row(11, _names("f" * 64, fp1), mac10))

    def case(name: str, base: str, recipe: list[Any], **kw: Any) -> dict[str, Any]:
        return {
            "case": name,
            "base": base,
            "recipe": [*recipe, BIND],
            "native": "same",
            **kw,
        }

    return [
        # The key present: the row appended after the upkeep.
        case("present_appended", "intact", [last]),
        case(
            "present_two_pending",
            "intact",
            [
                _record(
                    _entry(8, mac8),
                    _entry(
                        8,
                        mac8,
                        bundle="/b/2",
                        restored_at="2000-01-02T00:00:00.000000Z",
                    ),
                )
            ],
        ),
        case("present_withheld", "intact", [_record(_entry(1, mac1))]),
        case("present_elsewhere", "intact", [_record(_entry(8, "0" * 64))]),
        case("present_record_unread", "intact", [{"write": RECORD, "hex": b"{".hex()}]),
        case(
            "present_other_store",
            "intact",
            [_record(_entry(8, mac8, store_id="f" * 32))],
        ),
        case(
            "present_not_store_restored",
            "intact",
            [_record(_entry(8, mac8, kind="history_accepted"))],
        ),
        case(
            "present_after_foreign_tail",
            "intact",
            [_sql(_rekey_row(9, _names("f" * 64), mac8)), last],
        ),
        case("present_after_anchor", "intact", [{"unlink": ".origin"}, last]),
        case(
            "present_lock_a_directory",
            "intact",
            [last, {"mkdir": f"keys/{STORE_ID}{RECORD}.lock"}],
        ),
        case("present_readonly_keys", "intact", [last], hook="readonly_keys"),
        # A key that signs a segment: after the reclaim and the anchor.
        case("reclaim_appended", "intact", [_meta("b" * 64), last]),
        case(
            "reclaim_middle_appended",
            "rekeyed",
            [tail_f, _meta("f" * 64), _record(_entry(11, "c" * 64))],
        ),
        # A new key: after the rekey row and the anchor taken over.
        case("rekey_appended", "intact", [missing, last]),
        case(
            "rekey_no_anchor_appended", "intact", [missing, {"unlink": ".origin"}, last]
        ),
        case("rekey_withheld", "intact", [missing, _record(_entry(1, mac1))]),
    ]


def python_cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """The cases whose answer is the release's: the OverflowError
    `settle` lets through (CPython's `sqlite3` binding a seq past an
    SQLite INTEGER, its text), and the RecursionError of the tokenizer's
    read (`reconciles_tokenizer`, after the open's writes and a restore's
    row) and of the upkeep's (`chain_segments`, before `settle`, so no
    row), which where `json.loads` stops decides."""
    intact = bases["intact"]
    mac8 = _row_mac(intact, 8)
    nested = {"nested_index": 9, "prev_mac": mac8, "mac": "d" * 64}
    rekey = {"nested_rekey": 9, "prev_mac": mac8, "mac": "d" * 64}

    def case(name: str, recipe: list[Any]) -> dict[str, Any]:
        return {
            "case": name,
            "base": "intact",
            "recipe": [*recipe, BIND],
            "native": "same",
        }

    return [
        # Not caught by `settle`: the open raises after the upkeep.
        case("present_seq_past_an_int", [_record(_entry(2**64, "x"))]),
        case("tokenizer_index_nested", [nested]),
        case("tokenizer_index_nested_objects", [{**nested, "shape": "objects"}]),
        case(
            "tokenizer_index_nested_after_settle",
            [nested, _record(_entry(9, "d" * 64))],
        ),
        # The upkeep raises before `settle`: a rekey payload nested past
        # where `json.loads` stops (`chain_segments`), the key missing or
        # present; the pending restore gets no row.
        case(
            "rekey_nested_before_settle",
            [{"unlink": ".key"}, rekey, _record(_entry(8, mac8))],
        ),
        case("tail_nested_before_settle", [rekey, _record(_entry(8, mac8))]),
    ]


class _Warnings(logging.Handler):
    """The upkeep's, the rekey's and the head checkpoint's records on
    `bettermemory.store`, and every record of `bettermemory.restore_rows`,
    in the order logged."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "bettermemory.restore_rows" or record.funcName in LOGGED:
            self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Warnings]:
    handler = _Warnings()
    loggers = [
        logging.getLogger(n)
        for n in ("bettermemory.store", "bettermemory.restore_rows")
    ]
    kept = [(lg, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.propagate = False
    try:
        yield handler
    finally:
        for lg, propagate in kept:
            lg.propagate = propagate
            lg.removeHandler(handler)


def _warnings(records: list[logging.LogRecord], root: Path) -> list[dict[str, Any]]:
    def arg(r: logging.LogRecord, a: Any) -> dict[str, Any]:
        if r.name == "bettermemory.restore_rows":
            return _restore_arg(a, root)
        return _arg(a, root)

    return [
        {
            "level": r.levelname,
            "msg": r.msg,
            "args": [arg(r, a) for a in (r.args or ())],
        }
        for r in records
    ]


def run_case(c: dict[str, Any], bases: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made(c["base"], bases, case)
        _setup_nested(c["recipe"], case)
        before = state(case, root)
        seq = _last_seq(case)
        draws = _Draws()
        log = Ticker(LOG0, LOG_STEP)
        with (
            _captured() as first,
            fixed(log=log, wall=Ticker(WALL0, WALL_STEP)),
            _engine(),
            patch.object(blog, "secrets", draws),
        ):

            def hooked() -> None:
                with _hooked(c.get("hook"), case):
                    _open(case)

            outcome = _attempt(hooked, root)
        drawn = draws.count
        after = state(case, root)
        appended = _rows_after(case, seq)
        second_log = Ticker(LOG0 + timedelta(hours=1), LOG_STEP)
        with (
            _captured() as second,
            fixed(log=second_log, wall=Ticker(WALL0, WALL_STEP)),
            _engine(),
            patch.object(blog, "secrets", draws),
        ):
            second_outcome = _attempt(lambda: _open(case), root)
        again = state(case, root)
        last = int(appended[-1]["seq"]) if appended else seq
        return {
            **c,
            "before": {k: before.get(k) for k in ("master", "tables")},
            "outcome": outcome,
            "after": after,
            "appended": appended,
            "warnings": _warnings(first.records, root),
            "clock": log.stamps(),
            "drawn": drawn,
            "second": {
                "outcome": second_outcome,
                "changed": _changed(after, again),
                "appended": _rows_after(case, last),
                "warnings": _warnings(second.records, root),
                "clock": second_log.stamps(),
                "drawn": draws.count - drawn,
            },
        }


def generate() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_restore")]
    lines += [run_case(c, bases) for c in cases(bases) if not _skipped_as_root(c)]
    return {"store_open_restore.jsonl": jsonl(lines)}


def generate_python() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_restore_python")]
    lines += [run_case(c, bases) for c in python_cases(bases)]
    return {"store_open_restore_python.jsonl": jsonl(lines)}


__all__ = ["cases", "generate", "generate_python", "python_cases"]
