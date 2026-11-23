"""Fixture: the event writes of ``store_events.py``
(``store_record_event_posix.jsonl``).

The native build ports ``record_event`` with its retry and deadline
(``_primary_code``, ``_busy``, ``_time_left``, ``_waiting_until``),
``import_event``, ``imported_event_keys``, ``_named_payload``,
``record_migration`` and ``record_capture`` (``native/``,
`store::record_event`) and is held to this file.

Each case starts from the store `Store.create` makes under
``store_create.json``'s fixed values, opened as the owner (as
``store_tx.jsonl``'s are), and runs its steps. A real SQLITE_BUSY comes
from a second connection that holds ``BEGIN IMMEDIATE`` on the same file;
the store's busy timeout is set to 0 first where a case asks, so a busy
try fails at once. ``store_events.time`` is replaced for the call by a
stub whose ``monotonic`` answers the step's scripted readings in order and
whose ``sleep`` counts the pauses and, at the pause a step names, commits
the blocker's transaction; it is given back after every step. SQLITE_READONLY
comes from ``PRAGMA query_only``.

Each step records the readings taken, the pauses, the log levels on
``bettermemory.store`` (the texts hold sqlite3's wording, which is the
interpreter's), the outcome (the row's seq, none, or the raise's class,
with its text where it is the product's), the rows appended (every
column) and the busy timeout after the call, in milliseconds.
``imported_event_keys`` is recorded as its sorted keys, and a re-run of
``import_event`` over the same events records which events the keys
already hold (an event whose note holds a control token among them).

Gates: POSIX only, as ``store_tx.jsonl`` (the open binds the key ring by
its resolved POSIX path under ``flock``). Not on the Python release:
every text compared is the product's own. Not on the SQLite release: the
busy and read-only codes are SQLite's stable primary codes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import bettermemory
from bettermemory import log as blog
from bettermemory import store_events
from bettermemory.capture import BatchAlreadyOpenError, BatchClosedError
from bettermemory.log import canonical_payload
from bettermemory.store import Store
from bettermemory.store_rows import event_import_payload

from ._common import captured_by, text_field
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, _engine, _made
from .gen_store_write import log_rows

LOG0 = datetime(2002, 3, 4, 5, 6, 7, 123456, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1, microseconds=7)
WALL0 = datetime(2004, 5, 6, 7, 8, 9, 0, tzinfo=timezone.utc)
TOKEN = "<|im_start|>"


def ev(kind: str, **kw: Any) -> dict[str, Any]:
    """A ``record_event`` step: fields under ``fields``, the call's
    arguments, and the harness's: ``busy`` (hold the lock), ``zero``
    (busy timeout 0 first), ``release`` (commit the blocker at that
    pause), ``mono`` (the scripted readings), ``readonly``."""
    step = {"op": "event", "kind": kind, "fields": kw.pop("fields", {})}
    step.update(kw)
    return step


def imp(event: dict[str, Any], imported_from: str = "v8") -> dict[str, Any]:
    return {"op": "import", "event": event, "imported_from": imported_from}


def keys(imported_from: str = "v8") -> dict[str, Any]:
    return {"op": "keys", "imported_from": imported_from}


def cap(kind: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"op": "capture", "kind": kind, "payload": payload, **kw}


V8: list[dict[str, Any]] = [
    {"ts": "2020-01-01T00:00:00Z", "session": "s1", "kind": "search", "query": "q one"},
    {"ts": "2020-01-02T00:00:00Z", "kind": "recall", "note": "a " + TOKEN + " b"},
    {"session": "s2", "kind": "write", "status": "committed"},
    {"ts": "", "session": "", "kind": "audit", "said": 1},
    {"ts": "2020-01-03T00:00:00Z", "kind": "search", "imported_from": "other"},
]


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "plain",
            "steps": [
                ev("search", session="s1", fields={"query": "hello world", "n": 3}),
                ev("search", fields={"note": TOKEN}, actor={"client": "c"}),
                ev("search", fields={"z": None}, actor={"client": "c", "a": [1]}),
                ev("search", actor={}),
                ev("search", attempts=0),
                ev("search", zero=True, held=0.003),
                ev(
                    "search",
                    zero=False,
                    busy=False,
                    readonly=False,
                    keyless=False,
                    held=2,
                ),
                ev("search", kind_codes=[97, 0xDCFF]),
                ev("search", best_effort=False, session_codes=[0xD800, 0xDCFF]),
                ev("search", session_codes=[0xDCFF], busy=True, zero=True),
                ev("search", attempts=-5, best_effort=False),
            ],
        },
        {
            "name": "refused_kinds",
            "steps": [
                ev("memory_put"),
                ev("migrate_v8"),
                ev("capture_open"),
                ev("search"),
            ],
        },
        {
            "name": "raising_store_logger",
            "steps": [
                ev("search", clock_failure="clock unavailable", check_reason=True),
                ev("search", head_failure="checkpoint unavailable"),
                ev(
                    "search",
                    head_failure="checkpoint unavailable",
                    log_failure="handler Ω",
                ),
                ev("search", keyless=True, log_failure="cannot emit"),
                ev("search", readonly=True, best_effort=False, log_failure="disk"),
                ev(
                    "search",
                    busy=True,
                    zero=True,
                    attempts=2,
                    release=1,
                    log_failure_codes=[98, 0xDCFF],
                    check_reason=True,
                ),
                ev("search"),
            ],
        },
        {
            "name": "busy_once",
            "steps": [
                ev("search", busy=True, zero=True),
                ev("search", busy=True, zero=True, best_effort=False),
                ev("search", busy=True, zero=True, attempts=3),
                ev("search", busy=True, zero=True, attempts=3, best_effort=False),
            ],
        },
        {
            "name": "busy_then_free",
            "steps": [
                ev("search", busy=True, zero=True, attempts=3, release=1),
                ev("search", busy=True, zero=True, attempts=2**32 + 1, release=1),
                ev("search", busy=True, zero=True, attempts=4, release=2),
            ],
        },
        {
            "name": "deadline",
            "steps": [
                # Time left for one more try, then none.
                ev(
                    "search",
                    busy=True,
                    zero=True,
                    attempts=3,
                    deadline=10.0,
                    mono=[1.0, 2.0, 3.0, 9.96],
                ),
                # Exactly the pause left: no retry.
                ev(
                    "search",
                    busy=True,
                    zero=True,
                    attempts=3,
                    deadline=10.0,
                    mono=[9.0, 9.95],
                ),
                # The wait lowered to what is left, then given back.
                ev("search", busy=True, attempts=2, deadline=10.0, mono=[10.0, 10.0]),
                ev("search", busy=True, attempts=1, deadline=7.0, mono=[9.0]),
                # Free: the deadline only bounds the wait.
                ev("search", deadline=100.0, mono=[1.0]),
                ev("search", deadline=1.0, mono=[50.0]),
                ev("search", deadline=4.0, mono=[0.0]),
                ev("search", held=0.0009, deadline=0.0002, mono=[0.0]),
                ev("search", sample_wait=True, deadline=0.2, mono=[0.0]),
            ],
        },
        {
            "name": "readonly",
            "steps": [
                ev("search", keyless=True, best_effort=False, fields={"query": "q"}),
                ev("memory_put", keyless=True),
                ev("search", kind_codes=[0xDCFF], keyless=True, busy=True, zero=True),
                ev("search", readonly=True),
                ev("search", readonly=True, best_effort=False),
                ev("search", readonly=True, best_effort=False, attempts=3),
                ev("search"),
            ],
        },
        {
            "name": "in_transaction",
            "steps": [
                {
                    "op": "in_tx",
                    "events": [ev("search", fields={"n": 1}), ev("recall")],
                },
                {"op": "in_tx", "events": [ev("search", attempts=5), ev("memory_put")]},
            ],
        },
        {
            "name": "import",
            "steps": [
                *[imp(e) for e in V8],
                imp({"kind": "search"}, imported_from="v_9%"),
                imp({"ts": "x"}),
                imp({"kind": ""}),
                imp({"kind": "memory_put"}),
                imp({"kind": 5}),
                {**imp({"kind": "search"}), "event_session_codes": [0xDCFF]},
                keys(),
                keys("v_9%"),
                keys("v%"),
                keys("none"),
                {"op": "rerun", "events": V8},
                {"op": "edit_token"},
                keys(),
                {"op": "rerun", "events": V8},
            ],
        },
        {
            "name": "migration_and_capture",
            "steps": [
                {
                    "op": "migration",
                    "source": "/v8/dir",
                    "counts": {"events": 3, "memories": 0},
                },
                {"op": "migration", "source": "s", "counts": {}, "session": "m"},
                cap("capture_open", {"batch": "b1"}, session="s1"),
                cap(
                    "capture_member",
                    {"batch": "b1", "id": "x"},
                    ts="2021-01-01T00:00:00Z",
                ),
                cap("capture_close", {"batch": "b1"}, ts=""),
                cap("search", {}),
                cap("unused", {}, kind_codes=[97, 0xDCFF]),
                cap("capture_open", {}, session_codes=[0xDCFF], guard="refuse"),
                cap("capture_open", {}, session_codes=[0xDCFF], guard="pass"),
                cap(
                    "capture_close",
                    {},
                    ts_codes=[97, 0xD800, 0xDCFF],
                    session_codes=[0xDCFF],
                ),
                cap("capture_open", {"batch": "b2"}, guard="refuse"),
                cap("capture_open", {"batch": "b3"}, guard="pass"),
                cap(
                    "capture_open",
                    {},
                    guard="BatchAlreadyOpenError",
                    guard_text="already open",
                ),
                cap(
                    "capture_close",
                    {},
                    guard="BatchClosedError",
                    guard_text_codes=[98, 0xDCFE],
                ),
                {"op": "batch_writes", "rollback": True},
                {"op": "batch_writes", "rollback": False},
            ],
        },
    ]


class LogFailure(Exception):
    """A handler failed while emitting a real store diagnostic."""


class Guarded(Exception):
    """What a capture step's guard raises."""


def _outcome(fn: Any) -> dict[str, Any]:
    try:
        row = fn()
    except (
        sqlite3.Error,
        ValueError,
        Guarded,
        LogFailure,
        BatchAlreadyOpenError,
        BatchClosedError,
    ) as exc:
        details = (
            {"text": None}
            if isinstance(exc, sqlite3.Error)
            else text_field("text", str(exc))
        )
        return {"raised": type(exc).__name__, **details}
    return {"seq": None if row is None else row.seq}


def _column(step: dict[str, Any], name: str) -> str | None:
    points = step.get(name + "_codes")
    if points is not None:
        return "".join(chr(cp) for cp in points)
    return step.get(name)


def _event_call(store: Store, step: dict[str, Any]) -> Any:
    kind = _column(step, "kind")
    assert kind is not None
    return store.record_event(
        kind,
        session=_column(step, "session"),
        actor=step.get("actor"),
        best_effort=step.get("best_effort", True),
        attempts=step.get("attempts", 1),
        deadline=step.get("deadline"),
        **step["fields"],
    )


def _run_event(store: Store, file: Path, step: dict[str, Any]) -> dict[str, Any]:
    if step.get("keyless"):
        with (
            _engine(),
            Store.open(file, keys_dir=file.parent / "missing-keys") as reader,
        ):
            assert reader._key is None
            return _run_event(
                reader, file, {k: v for k, v in step.items() if k != "keyless"}
            )

    readings = list(step.get("mono", []))
    taken: list[float] = []
    pauses: list[float] = []
    blocker = None
    if step.get("busy"):
        blocker = sqlite3.connect(file, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")

    def monotonic() -> float:
        # Past the script, the last reading again: the clock stands.
        value = readings.pop(0) if readings else (taken[-1] if taken else 0.0)
        taken.append(value)
        return value

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        if blocker is not None and step.get("release") == len(pauses):
            blocker.execute("COMMIT")

    held = store.busy_timeout
    if "held" in step:
        store.set_busy_timeout(step["held"])
    if step.get("zero"):
        store.set_busy_timeout(0.0)
    if step.get("sample_wait"):
        store._conn.executescript(
            "CREATE TEMP TABLE wait_samples(ms INTEGER);"
            "CREATE TEMP TRIGGER sample_wait BEFORE INSERT ON log BEGIN "
            "INSERT INTO wait_samples SELECT timeout FROM pragma_busy_timeout; END;"
        )
    if step.get("readonly"):
        store._conn.execute("PRAGMA query_only = 1")
    result: dict[str, Any] = {}
    try:
        with mock.patch.object(
            store_events, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep)
        ):
            if "head_failure" in step:
                stamp = blog._utcnow_iso
                calls = 0

                def checkpoint_clock() -> str:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError(step["head_failure"])
                    return stamp()

                with mock.patch.object(blog, "_utcnow_iso", checkpoint_clock):
                    outcome = _outcome(lambda: _event_call(store, step))
                head = store._keyring.read_head()
                result["checkpoint_seq"] = head.seq if head is not None else None
            elif "clock_failure" in step:
                with mock.patch.object(
                    blog, "_utcnow_iso", side_effect=OSError(step["clock_failure"])
                ):
                    outcome = _outcome(lambda: _event_call(store, step))
            else:
                outcome = _outcome(lambda: _event_call(store, step))
        after = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        held_after = store.busy_timeout
        if step.get("sample_wait"):
            result["in_write_ms"] = store._conn.execute(
                "SELECT max(ms) FROM wait_samples"
            ).fetchone()[0]
    finally:
        if blocker is not None:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
            blocker.close()
        store._conn.execute("PRAGMA query_only = 0")
        if step.get("sample_wait"):
            store._conn.executescript(
                "DROP TRIGGER sample_wait; DROP TABLE wait_samples;"
            )
        store.set_busy_timeout(held)
    return {
        "outcome": outcome,
        "mono": taken,
        "pauses": len(pauses),
        "busy_ms": after,
        "held_after": held_after,
        **result,
    }


def _run_step(store: Store, file: Path, step: dict[str, Any]) -> dict[str, Any]:
    op = step["op"]
    if op == "event":
        return _run_event(store, file, step)
    if op == "in_tx":
        outs = []
        with store._transaction():
            for e in step["events"]:
                outs.append(_outcome(lambda e=e: _event_call(store, e)))
        return {"outcomes": outs}
    if op == "import":
        event, source = dict(step["event"]), step["imported_from"]
        if "event_session_codes" in step:
            event["session"] = _column(step, "event_session")
        return {
            "outcome": _outcome(lambda: store.import_event(event, imported_from=source))
        }
    if op == "keys":
        got = store.imported_event_keys(step["imported_from"])
        return {"keys": sorted([list(k) for k in got], key=json.dumps)}
    if op == "rerun":
        held = store.imported_event_keys()
        found = []
        for e in step["events"]:
            ts, session, kind, payload = event_import_payload(e)
            found.append(
                [ts, session, kind, canonical_payload(payload)]
                in [list(k) for k in held]
            )
        return {"found": found}
    if op == "edit_token":
        # An earlier build's row: the note kept with its token as given.
        store._conn.execute(
            "UPDATE log SET payload = ? WHERE kind = 'recall'",
            (canonical_payload({"imported_from": "v8", "note": "a " + TOKEN + " b"}),),
        )
        store._conn.commit()
        return {}
    if op == "batch_writes":
        outs = []
        try:
            with store.batch(), mock.patch.object(bettermemory, "__version__", ENGINE):
                outs.append(
                    _outcome(lambda: store.record_migration(source="v8", counts={}))
                )
                outs.append(_outcome(lambda: store.import_event({"kind": "recall"})))
                outs.append(_outcome(lambda: store.record_capture("capture_undo", {})))
                if step["rollback"]:
                    raise Guarded("abort batch")
        except Guarded:
            pass
        return {"outcomes": outs}
    if op == "migration":
        # The installed version is the release's: the fixture's is fixed.
        with mock.patch.object(bettermemory, "__version__", ENGINE):
            outcome = _outcome(
                lambda: store.record_migration(
                    source=step["source"],
                    counts=step["counts"],
                    session=step.get("session"),
                )
            )
        return {"outcome": outcome}
    if op == "capture":
        capture_kind = _column(step, "kind")
        assert capture_kind is not None
        guard = None
        guard_class = {
            "BatchAlreadyOpenError": BatchAlreadyOpenError,
            "BatchClosedError": BatchClosedError,
        }.get(step.get("guard", ""))
        if guard_class is not None:

            def guard() -> None:
                assert guard_class is not None
                store._conn.execute("CREATE TEMP TABLE capture_guard_probe(value)")
                raise guard_class(_column(step, "guard_text"))

        elif step.get("guard") == "refuse":

            def guard() -> None:
                raise Guarded("refused")

        elif step.get("guard") == "pass":

            def guard() -> None:
                assert store._conn.in_transaction

        result = {
            "outcome": _outcome(
                lambda: store.record_capture(
                    capture_kind,
                    step["payload"],
                    session=_column(step, "session"),
                    ts=_column(step, "ts"),
                    guard=guard,
                )
            )
        }
        if guard_class is not None:
            result["guard_effect_after"] = store._conn.execute(
                "SELECT count(*) FROM sqlite_temp_master WHERE name='capture_guard_probe'"
            ).fetchone()[0]
        return result
    raise AssertionError(op)


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    caught: list[str] = []
    retries: list[list[int]] = []
    arguments: list[list[list[int]]] = []

    class Catch(logging.Handler):
        failure: str | None = None

        def emit(self, record: logging.LogRecord) -> None:
            caught.append(record.levelname)
            if isinstance(record.args, tuple) and len(record.args) >= 2:
                arguments.append(
                    [[ord(c) for c in str(arg)] for arg in record.args[:2]]
                )
            if record.levelno == logging.INFO and isinstance(record.args, tuple):
                attempt, tries = record.args[-2:]
                assert isinstance(attempt, int) and isinstance(tries, int)
                retries.append([attempt, tries])
            if self.failure is not None:
                raise LogFailure(self.failure)

    handler = Catch(level=logging.DEBUG)
    logger = logging.getLogger("bettermemory.store")
    with (
        captured_by(logger, handler, logging.DEBUG),
        tempfile.TemporaryDirectory() as raw,
    ):
        case = Path(raw) / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            for i, step in enumerate(case_def["steps"]):
                log = Ticker(LOG0 + timedelta(minutes=i), LOG_STEP)
                wall = Ticker(WALL0, LOG_STEP)
                before = log_rows(file, 0)
                last = before[-1][0] if before else 0
                handler.failure = _column(step, "log_failure")
                arguments.clear()
                caught.clear()
                retries.clear()
                with fixed(log=log, wall=wall):
                    record = _run_step(store, file, step)
                if step.get("check_reason"):
                    record["outcome"]["logged_arguments"] = list(arguments)
                if wall.read:
                    raise AssertionError("the wall clock was read")
                record.update(
                    step=step,
                    log_clock=log.stamps(),
                    levels=list(caught),
                    retries=list(retries),
                    rows=log_rows(file, last),
                )
                out.append(record)
        finally:
            store.close()
    return {"case": case_def["name"], "steps": out}


def generate_posix() -> dict[str, bytes]:
    scenarios = cases()
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_record_event_posix",
            "count": len(scenarios),
            "engine_version": ENGINE,
            "store_id": STORE_ID,
        }
    ]
    with world():
        lines += [run_case(c) for c in scenarios]
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_record_event_posix.jsonl": text.encode("ascii")}
