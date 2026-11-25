"""Fixture: the owner's writing open of a store whose key is missing or
foreign (``store_open_rekey.jsonl``, ``store_open_rekey_python.jsonl``), and
the saved orphans a store was created after (``store_orphans.jsonl``).

`store_open._open_connected` reads the chain's segments under meta's
fingerprint and the key in hand. A key that signs a segment (its first row
verifies under it) is kept: `Store._reclaim` sets meta back to it and, when
the last segment is another key's, appends the ``rekey`` row that returns
the chain to it. Otherwise `store_open._open_rekeyed` retires the key in
hand, writes a new one, appends a ``rekey`` row from the last segment's key
to it, and anchors this machine's history at that row when no anchor is
there (`Store._take_over_origin`). The native build does both
(`store::open`, `open/rekey.rs`) and is held to these files.

Each case starts from a base of ``store_bases.json`` with a recipe of
edits (`gen_store_open_upkeep._setup`'s ops), bound to its file, and opens
it with the log's and the wall clock fixed (`_fixed.fixed`) and the key
draw fixed (`bettermemory.log.secrets` replaced: draw ``n`` of the case is
`drawn_key(n)`, counted across both opens). A case may name a hook:
``busy`` (another connection holds the write lock for the whole first
open), ``busy_reclaim`` (held until `_reclaim` returns), ``race`` (a second
owner's open runs between the dispatch and `_reclaim`'s lock),
``race_sql`` (the case's ``race_sql`` runs on another connection there),
``two_owners`` (a second owner's open runs right after the first wrote its
new key), ``readonly_keys`` (the keys directory 0500 for the first open;
skipped as root). Recorded for each: the outcome (an OSError as its class
and errno: its text names a random temporary file), the whole after-state
(`gen_store_open_upkeep.state`: each key file by name, mode and the
SHA-256 of its bytes, which is the key's fingerprint), every row the open
appended, every warning (format string and arguments; the head
checkpoint's after a commit among them), the clock's
readings, how many keys were drawn, and what a second open changed and
logged.

Gates: those of ``store_open_upkeep.jsonl``, for its reasons (Unicode: the
digest's rows are the tokenizer's; POSIX only: modes, a resolved binding,
``flock``). The Python-gated sibling holds the cases whose answer is where
`json.loads` stops recursing (a rekey payload or an anchor nested 100,000
deep) and the ones whose text is CPython's `sqlite3` module's (a log row
that is not UTF-8). ``store_orphans.jsonl`` is POSIX only: a name is
plain by the host's path rules (a backslash is a separator on Windows).
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bettermemory import log as blog
from bettermemory.log import KeyRing
from bettermemory.store import Store
from bettermemory.store_schema import CREATED_AFTER_ORPHANS

from ._common import jsonl, text_field
from ._fixed import Ticker, fixed
from .gen_store_files import load_bases
from .gen_store_open import _changed, _close_left, _engine, _made, _row_mac, _scrub
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
    _origin,
    _rekey_row,
    _rows_after,
    _setup,
    _skipped_as_root,
    _sql,
    state,
)

#: The functions whose warnings the fixture records: the upkeep's, the
#: two paths of this unit, and the head checkpoint's after a commit
#: (`_move_head`).
LOGGED = frozenset(
    {
        "_move_head",
        "_close_foreign_tail",
        "_settle_origin",
        "_record_origin",
        "_name_origin_key",
        "_reclaim",
        "_open_rekeyed",
    }
)


def drawn_key(n: int) -> bytes:
    """The ``n``-th key a case draws: 32 bytes of ``0xa0 + n``."""
    return bytes([0xA0 + n]) * 32


class _Draws:
    """`bettermemory.log.secrets` with `token_bytes` drawing `drawn_key`."""

    def __init__(self) -> None:
        self.count = 0

    def token_bytes(self, nbytes: int) -> bytes:
        assert nbytes == 32, nbytes
        key = drawn_key(self.count)
        self.count += 1
        return key


def _meta(value: str) -> dict[str, Any]:
    quoted = value.replace("'", "''")
    return _sql(f"UPDATE meta SET value = '{quoted}' WHERE key = 'key_fingerprint';")


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: name, base, recipe, an optional hook, and what the native
    open does (``same``: the reference's after-state, rows and warnings;
    ``raises``: its class and text, or errno, after the same writes)."""
    intact, rekeyed = bases["intact"], bases["rekeyed"]
    mac8 = _row_mac(intact, 8)
    mac10 = _row_mac(rekeyed, 10)
    k0 = intact["keys"][".key"]
    fp0 = _fp(k0)
    fp1 = _fp(rekeyed["keys"][".key"])
    foreign = "ee" * 32
    missing = {"unlink": ".key"}
    no_origin = {"unlink": ".origin"}
    engine_current = _sql(
        "UPDATE meta SET value = '9.0.0' WHERE key = 'engine_version';"
    )
    # A tail another host opened after the K1 segment of ``rekeyed``.
    tail_f = _sql(_rekey_row(11, _names("f" * 64, fp1), mac10))

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
        # No key: a new key and a rekey row from the last segment's key.
        case("missing_nothing_held", "intact", [missing]),
        case("missing_no_anchor", "intact", [missing, no_origin]),
        case("missing_log_empty", "intact", [missing, _sql("DELETE FROM log;")]),
        case("missing_retired_held", "rekeyed", [missing]),
        case("missing_retired_held_no_anchor", "rekeyed", [missing, no_origin]),
        case(
            "missing_anchor_unnamed",
            "rekeyed",
            [missing, _origin(1, _row_mac(rekeyed, 1))],
        ),
        case(
            "missing_tail_unnamed",
            "intact",
            [missing, _sql(_rekey_row(9, "{}", mac8))],
        ),
        case(
            "missing_tail_surrogate",
            "intact",
            [
                missing,
                _sql(
                    _rekey_row(
                        9, '{"new_fingerprint":"\\udc80ab","old_fingerprint":"x"}', mac8
                    )
                ),
            ],
        ),
        case(
            "missing_tail_payload_blob",
            "intact",
            [
                missing,
                _sql(_rekey_row(9, _names("f" * 64, fp0), mac8)),
                _sql("UPDATE log SET payload = CAST(payload AS BLOB) WHERE seq = 9;"),
            ],
        ),
        case(
            "missing_meta_blob",
            "intact",
            [
                missing,
                _sql(
                    "UPDATE meta SET value = CAST(value AS BLOB) "
                    "WHERE key = 'key_fingerprint';"
                ),
            ],
        ),
        case("missing_busy", "intact", [missing, engine_current], hook="busy"),
        case("missing_two_owners", "intact", [missing], hook="two_owners"),
        case("missing_readonly_keys", "intact", [missing], hook="readonly_keys"),
        # A key that signs nothing: retired, then a new key.
        case("foreign_signs_nothing", "intact", [{"write": ".key", "hex": foreign}]),
        case(
            "foreign_no_anchor",
            "intact",
            [{"write": ".key", "hex": foreign}, no_origin],
        ),
        case(
            "foreign_retire_target_taken",
            "intact",
            [
                {"write": ".key", "hex": foreign},
                {"write": f".{_fp(foreign)[:16]}.key", "hex": "dd" * 32},
            ],
        ),
        case(
            "foreign_anchor_unnamed_named",
            "intact",
            [
                {"write": f".{fp0[:16]}.key", "hex": k0},
                {"write": ".key", "hex": foreign},
                _origin(1, _row_mac(intact, 1)),
            ],
        ),
        # A segment named for the key in hand whose first row it does not
        # sign: the MAC decides, so the key is retired.
        case(
            "foreign_named_not_signed",
            "intact",
            [
                _sql(_rekey_row(9, _names(_fp(foreign), fp0), mac8)),
                {"write": ".key", "hex": foreign},
            ],
        ),
        case(
            "foreign_readonly_keys",
            "intact",
            [{"write": ".key", "hex": foreign}],
            hook="readonly_keys",
        ),
        # A key that signs a segment: kept, meta set back, the chain
        # returned to it when its segment is not the last.
        case("reclaim_leading", "intact", [_meta("b" * 64)]),
        case("reclaim_capitals", "intact", [_meta(fp0.upper())]),
        case("reclaim_last", "rekeyed", [_meta("b" * 64)]),
        case("reclaim_middle", "rekeyed", [tail_f, _meta("f" * 64)]),
        case("reclaim_middle_race", "rekeyed", [tail_f, _meta("f" * 64)], hook="race"),
        case(
            "reclaim_middle_no_anchor",
            "rekeyed",
            [tail_f, _meta("f" * 64), no_origin],
        ),
        case(
            "rekey_previous_from_a_forged_row",
            "intact",
            [
                _sql(_rekey_row(9, _names(fp0, "a" * 64), mac8)),
                _meta("a" * 64),
                {"write": ".key", "hex": foreign},
            ],
        ),
        case(
            "reclaim_busy",
            "intact",
            [_meta("b" * 64), engine_current],
            hook="busy_reclaim",
        ),
        # Two foreign segments after the key's: the warning lists both.
        case(
            "reclaim_two_foreign",
            "rekeyed",
            [
                tail_f,
                _sql(_rekey_row(12, _names("e" * 64, "f" * 64), "c" * 64)),
                _meta("e" * 64),
            ],
        ),
        # The reclaim's rekey row commits, and the head cannot be written
        # in a keys directory the open may not write: its warning.
        case(
            "reclaim_middle_readonly_keys",
            "rekeyed",
            [tail_f, _meta("f" * 64)],
            hook="readonly_keys",
        ),
        # Every rekey row gone between the dispatch and the reclaim's lock:
        # the reread names none, so the tail stays the segment map's (the
        # foreign one), and the rekey row returning the chain is appended.
        case(
            "reclaim_rekey_rows_gone_under_the_lock",
            "rekeyed",
            [tail_f, _meta("f" * 64)],
            hook="race_sql",
            race_sql="DELETE FROM log WHERE kind = 'rekey';",
        ),
    ]


def python_cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """The cases whose answer is the release's: a RecursionError where
    `json.loads` stops (`DEEP`), and `sqlite3`'s text for a log row that is
    not UTF-8 (which row and which column it names: the anchored row, the
    first row, then the ``rekey`` rows, each column in order)."""
    intact = bases["intact"]
    mac8 = _row_mac(intact, 8)
    fp0 = _fp(intact["keys"][".key"])
    rekeyed = bases["rekeyed"]
    mac10 = _row_mac(rekeyed, 10)
    fp1 = _fp(rekeyed["keys"][".key"])
    missing = {"unlink": ".key"}
    no_origin = {"unlink": ".origin"}
    nested_9 = {"nested_rekey": 9, "prev_mac": mac8, "mac": "c" * 64}
    bad_ts = "CAST(x'41ff42' AS TEXT)"

    def case(
        name: str,
        recipe: list[dict[str, Any]],
        *,
        base: str = "intact",
        bind: bool = True,
        **kw: Any,
    ) -> dict[str, Any]:
        return {
            "case": name,
            "base": base,
            "recipe": [*recipe, BIND] if bind else recipe,
            "native": "raises",
            **kw,
        }

    return [
        case(
            "missing_segments_nested",
            [missing, {"nested_rekey": 9, "prev_mac": mac8, "mac": "c" * 64}],
        ),
        case(
            "missing_segments_objects",
            [
                missing,
                {
                    "nested_rekey": 9,
                    "prev_mac": mac8,
                    "mac": "c" * 64,
                    "shape": "objects",
                },
            ],
        ),
        case("missing_then_origin_nested", [missing, {"nested_origin": True}]),
        case(
            "reclaim_then_origin_nested",
            [
                _sql(
                    "UPDATE meta SET value = '"
                    + "b" * 64
                    + "' WHERE key = 'key_fingerprint';"
                ),
                {"nested_origin": True},
            ],
        ),
        case(
            "missing_rekey_row_not_utf8",
            [
                missing,
                _sql(_rekey_row(9, _names("f" * 64), mac8)),
                _sql("UPDATE log SET ts = CAST(x'41ff42' AS TEXT) WHERE seq = 9;"),
            ],
        ),
        case(
            "missing_first_row_not_utf8",
            [
                missing,
                _sql("UPDATE log SET mac = CAST(x'ff' AS TEXT) WHERE seq = 1;"),
            ],
        ),
        # The anchored row is read first.
        case(
            "missing_anchored_row_not_utf8",
            [
                missing,
                _origin(3, _row_mac(intact, 3), fp0),
                _sql(f"UPDATE log SET ts = {bad_ts} WHERE seq = 3;"),
            ],
        ),
        # The first row before a rekey row.
        case(
            "missing_first_row_before_rekey_row",
            [
                missing,
                no_origin,
                _sql(_rekey_row(9, _names("a" * 64, fp0), mac8)),
                _sql(f"UPDATE log SET ts = {bad_ts} WHERE seq = 9;"),
                _sql(f"UPDATE log SET session = {bad_ts} WHERE seq = 1;"),
            ],
        ),
        # A row's columns in order: ts before mac.
        case(
            "missing_first_row_two_columns",
            [
                missing,
                no_origin,
                _sql(
                    f"UPDATE log SET ts = {bad_ts}, mac = CAST(x'ff' AS TEXT) "
                    "WHERE seq = 1;"
                ),
            ],
        ),
        # A refusal of a later step (an index another tokenizer spelled) is
        # held back: chain_segments raises first.
        case(
            "missing_segments_nested_drift",
            [
                missing,
                _sql(
                    "UPDATE log SET payload = json_set(payload, "
                    f"'$.tokenizer_fingerprint', '{'0' * 64}') WHERE seq = 1;"
                ),
                nested_9,
            ],
        ),
        # The binding is written before chain_segments raises.
        case("missing_segments_nested_unbound", [missing, nested_9], bind=False),
        # The last rekey row made unreadable by another opener between the
        # dispatch and the reclaim's lock, and a row of another kind after
        # it, so that only the reread under the lock meets it: logged, meta
        # not set back. The next open's dispatch raises.
        case(
            "reclaim_rekey_row_unreadable_under_the_lock",
            [
                _sql(_rekey_row(11, _names("f" * 64, fp1), mac10)),
                _meta("f" * 64),
            ],
            base="rekeyed",
            native="same",
            hook="race_sql",
            race_sql=(
                f"UPDATE log SET ts = {bad_ts} WHERE seq = 11; "
                "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
                "VALUES (12, '2001-02-03T04:05:06.000000Z', NULL, 'note', '{}', "
                f"'{'c' * 64}', '{'d' * 64}');"
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.funcName in LOGGED:
            self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Warnings]:
    handler = _Warnings()
    logger = logging.getLogger("bettermemory.store")
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
            "args": [_arg(a, root) for a in (r.args or ())],
        }
        for r in records
    ]


def _refusal(exc: BaseException, root: Path) -> dict[str, Any]:
    """The class and text; an OSError's errno instead of its text, which
    names the random temporary file `atomic_write_bytes` made."""
    if isinstance(exc, OSError) and not isinstance(exc, sqlite3.Error):
        return {"class": type(exc).__name__, "errno": errno.errorcode[exc.errno or 0]}
    return text_field("text", _scrub(str(exc), root)) | {"class": type(exc).__name__}


def _open(case: Path) -> None:
    store = Store.open(case / "memory.sqlite", keys_dir=case / "keys", allow_rekey=True)
    store.close()


@contextmanager
def _once(target: Any, name: str, wrap: Callable[[Any], Any]) -> Iterator[None]:
    with patch.object(target, name, wrap(getattr(target, name))):
        yield


@contextmanager
def _hooked(hook: str | None, case: Path, sql: str | None = None) -> Iterator[None]:
    """The case's hook around its first open (``race_sql``: the case's
    ``race_sql`` run on another connection between the dispatch and
    `_reclaim`'s lock)."""
    done = [False]

    def second_open() -> None:
        if not done[0]:
            done[0] = True
            _open(case)

    if hook in ("busy", "busy_reclaim"):
        holder = sqlite3.connect(case / "memory.sqlite", isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")

        def release() -> None:
            if holder.in_transaction:
                holder.execute("ROLLBACK")

        def released(real: Any) -> Any:
            def reclaim(self: Store, *a: Any, **k: Any) -> None:
                try:
                    real(self, *a, **k)
                finally:
                    release()

            return reclaim

        try:
            if hook == "busy_reclaim":
                with _once(Store, "_reclaim", released):
                    yield
            else:
                yield
        finally:
            release()
            holder.close()
    elif hook == "race":

        def raced(real: Any) -> Any:
            def reclaim(self: Store, *a: Any, **k: Any) -> None:
                second_open()
                real(self, *a, **k)

            return reclaim

        with _once(Store, "_reclaim", raced):
            yield
    elif hook == "race_sql":
        assert sql is not None

        def appended(real: Any) -> Any:
            def reclaim(self: Store, *a: Any, **k: Any) -> None:
                other = sqlite3.connect(case / "memory.sqlite")
                try:
                    other.executescript(sql)
                finally:
                    other.close()
                real(self, *a, **k)

            return reclaim

        with _once(Store, "_reclaim", appended):
            yield
    elif hook == "two_owners":

        def raced_create(real: Any) -> Any:
            def create_key(self: KeyRing) -> bytes:
                key = real(self)
                second_open()
                return key

            return create_key

        with _once(KeyRing, "create_key", raced_create):
            yield
    elif hook == "readonly_keys":
        os.chmod(case / "keys", 0o500)
        try:
            yield
        finally:
            os.chmod(case / "keys", 0o700)
    else:
        yield


def _attempt(run: Callable[[], None], root: Path) -> dict[str, Any]:
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - the refusal is the record
        _close_left(root)
        return {"opened": False, "refusal": _refusal(exc, root)}
    return {"opened": True}


def run_case(c: dict[str, Any], bases: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made(c["base"], bases, case)
        _setup(c["recipe"], case)
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
                with _hooked(c.get("hook"), case, c.get("race_sql")):
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
                "appended": _rows_after(case, _last_seq_of(appended, seq)),
                "warnings": _warnings(second.records, root),
                "clock": second_log.stamps(),
                "drawn": draws.count - drawn,
            },
        }


def _last_seq_of(appended: list[dict[str, Any]], seq: int) -> int:
    return int(appended[-1]["seq"]) if appended else seq


# ---------------------------------------------------------------------------
# store_orphans
# ---------------------------------------------------------------------------


#: A saved orphan's name with a stamp, and a counter when given.
def _orphan(stamp: str, n: str | None = None) -> str:
    return f"memory.sqlite.orphaned-{stamp}{'' if n is None else '-' + n}.sqlite"


def orphan_cases() -> list[dict[str, Any]]:
    """Each case: the meta value (``value``: a text, or ``sql`` for the
    literal SQL value, or none for no row), the files and directories
    beside the store, and the links (``links``: name to target)."""
    a = _orphan("20240101T000000Z")
    a2 = _orphan("20240101T000000Z", "2")
    a10 = _orphan("20240101T000000Z", "10")
    early = _orphan("20230101T000000Z")
    arabic = _orphan("20240101T000000Z", "٣")
    dashed = _orphan("2024-01-01", "3")
    import json as _json

    def names(*items: Any) -> str:
        return _json.dumps(list(items))

    return [
        {"case": "no_value", "files": [a]},
        {"case": "empty_list", "value": "[]", "files": [a]},
        {
            "case": "present_and_gone",
            "value": names(a, early, "gone.sqlite"),
            "files": [a, early],
        },
        {
            "case": "a_directory",
            "value": names(a, early),
            "files": [a],
            "dirs": [early],
        },
        {
            "case": "order",
            "value": names(a10, a, early, a2, dashed),
            "files": [a, a2, a10, early, dashed],
        },
        # A dash with no digits is part of the stamp; no counter is 1, after
        # a counter of 0.
        {
            "case": "counter_edges",
            "value": names(_orphan("a-"), _orphan("X"), _orphan("X", "0")),
            "files": [_orphan("a-"), _orphan("X"), _orphan("X", "0")],
        },
        {
            "case": "order_arabic_indic",
            "value": names(a10, arabic, a2),
            "files": [a10, arabic, a2],
        },
        {
            "case": "unordered_names",
            "value": names(
                "zeta",
                "x.orphaned-a.b.sqlite",
                "a.orphaned-1.orphaned-2.sqlite",
                "alpha",
            ),
            "files": [
                "zeta",
                "x.orphaned-a.b.sqlite",
                "a.orphaned-1.orphaned-2.sqlite",
                "alpha",
            ],
        },
        {
            "case": "ties_keep_their_order",
            "value": names(
                "b.orphaned-X.sqlite", "a.orphaned-X.sqlite", "b.orphaned-X.sqlite"
            ),
            "files": ["a.orphaned-X.sqlite", "b.orphaned-X.sqlite"],
        },
        {
            "case": "not_plain",
            "value": names("../x", "sub/x", "", ".", "..", 5, None, a),
            "files": [a],
            "dirs": ["sub"],
            "outer": ["x"],
        },
        # A backslash is no separator here; the value is spelled in SQL so
        # that no text the fixture holds doubles it twice.
        {
            "case": "backslash_is_plain",
            "sql": "'[\"a' || char(92, 92) || 'b\"]'",
            "files": ["a\\b"],
        },
        {"case": "not_json", "value": "not json", "files": [a]},
        {"case": "not_a_list", "value": '{"a": 1}', "files": [a]},
        {"case": "integer", "sql": "5", "files": [a]},
        {"case": "null", "sql": "NULL", "files": [a]},
        {"case": "blob", "sql": "CAST('[\"a\"]' AS BLOB)", "files": ["a"]},
        {"case": "not_utf8", "sql": "CAST(x'5b22ff225d' AS TEXT)", "files": [a]},
        {
            "case": "links",
            "value": names("to_file", "to_nothing", "to_dir"),
            "files": ["target"],
            "dirs": ["dir"],
            "links": {"to_file": "target", "to_nothing": "nothing", "to_dir": "dir"},
        },
        {"case": "nul_in_a_name", "value": names("a\u0000b", a), "files": [a]},
        {
            "case": "surrogate_in_a_name",
            "value": '["a\\udcff", "b\\ud800"]',
            "files": [],
        },
    ]


def _orphans_of(c: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        for name in c.get("outer", []):
            (root / name).write_bytes(b"")
        for name in c.get("dirs", []):
            (home / name).mkdir()
        for name in c.get("files", []):
            (home / name).write_bytes(b"")
        for name, target in c.get("links", {}).items():
            (home / name).symlink_to(target)
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            if "value" in c:
                conn.execute(
                    "INSERT INTO meta VALUES (?, ?)",
                    (CREATED_AFTER_ORPHANS, c["value"]),
                )
            elif "sql" in c:
                conn.execute(
                    f"INSERT INTO meta VALUES (?, {c['sql']})", (CREATED_AFTER_ORPHANS,)
                )
            fake = SimpleNamespace(_conn=conn, _path=home / "memory.sqlite")
            try:
                found = Store.store_orphans(fake)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - the refusal is the record
                # The class only: `sqlite3`'s text is the release's.
                return {**c, "raises": {"class": type(exc).__name__}}
        finally:
            conn.close()
        return {
            **c,
            "found": [text_field("name", str(p.relative_to(home))) for p in found],
        }


def generate() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_rekey")]
    lines += [run_case(c, bases) for c in cases(bases) if not _skipped_as_root(c)]
    return {"store_open_rekey.jsonl": jsonl(lines)}


def generate_orphans() -> dict[str, bytes]:
    lines = [{"fixture": "store_orphans"}]
    lines += [_orphans_of(c) for c in orphan_cases()]
    return {"store_orphans.jsonl": jsonl(lines)}


def generate_python() -> dict[str, bytes]:
    bases = load_bases()
    lines = [_header("store_open_rekey_python")]
    lines += [run_case(c, bases) for c in python_cases(bases)]
    return {"store_open_rekey_python.jsonl": jsonl(lines)}


__all__ = [
    "cases",
    "drawn_key",
    "generate",
    "generate_orphans",
    "generate_python",
    "orphan_cases",
    "python_cases",
]
