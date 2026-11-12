"""The `Store` methods of the telemetry rows, the event reads and the log.

Recording and importing events (`record_event`, `import_event`,
`record_migration`, `record_capture`), the event reads (`iter_events`,
`events_of_kinds`, `events_since`), and the log itself (`log_rows`,
`refold`, the replay that applies every mutation row through
`_apply_mutation` into a scratch database, and `log_verify`). Each method
is a function of `self: Store` that `Store` binds by name. It logs as
`bettermemory.store`. It imports `store_schema` and `store_rows`, and
`store` only for type checking.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .events import _redact_event_fields, named_event_fields
from .identity import Actor
from .log import (
    CAPTURE_KINDS,
    MIGRATE_V8,
    LogRow,
    canonical_payload,
    iter_rows,
    verify_chain,
)
from .store_schema import (
    FOLDED_TABLES,
    IMPORTED_FROM_V8,
    MEMBERSHIP_SCHEMA,
    MEMBERSHIP_TABLES,
    MUTATION_KINDS,
    SCHEMA,
    _NOT_TELEMETRY,
)
from .store_index import tokenizer_drift
from .store_rows import (
    INDEX_TEXT_COLUMNS,
    _ROW_COLUMNS,
    _apply_mutation,
    _iso,
    _key_label,
    _table_rows,
    event_import_payload,
)

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")


# -- telemetry ----------------------------------------------------------


#: How long a retried event write sleeps before its next try, in seconds
#: (`record_event`'s ``attempts``): long enough for a long write run's
#: chunk boundary to come round (`store.CHUNK_YIELD_SECONDS`), short
#: beside the busy wait the try that failed has already spent.
EVENT_RETRY_PAUSE_SECONDS = 0.05


def _primary_code(exc: BaseException) -> int | None:
    """The SQLite primary result code `exc` carries, None for none."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(exc, sqlite3.OperationalError) or not isinstance(code, int):
        return None
    return code & 0xFF


def _busy(exc: BaseException) -> bool:
    """Another writer held the store (SQLITE_BUSY or SQLITE_LOCKED): the
    failure a later try of the same write can get past
    (`store.retryable_code`'s ``store_busy``)."""
    return _primary_code(exc) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _time_left(deadline: float | None) -> bool:
    """Whether another try fits before `deadline`: its pause and some
    wait for the lock. Always, without one."""
    if deadline is None:
        return True
    return deadline - time.monotonic() > EVENT_RETRY_PAUSE_SECONDS


@contextlib.contextmanager
def _waiting_until(store: Store, deadline: float | None) -> Iterator[None]:
    """Bound the connection's wait for the lock by `deadline` for the block,
    then give back the busy timeout it had. A connection inside another
    call's transaction already holds the lock and is left alone."""
    if deadline is None or store._tx_depth:
        yield
        return
    held = store.busy_timeout
    left = max(0.0, deadline - time.monotonic())
    if left >= held:
        yield
        return
    store.set_busy_timeout(left)
    try:
        yield
    finally:
        store.set_busy_timeout(held)


def record_event(
    self: Store,
    kind: str,
    *,
    session: str | None = None,
    actor: Actor | Mapping[str, Any] | None = None,
    best_effort: bool = True,
    attempts: int = 1,
    deadline: float | None = None,
    **fields: Any,
) -> LogRow | None:
    """One telemetry row. ``query`` and ``probe_query`` are redacted to
    a hash, a short preview and a length before the row is signed, as
    the v8 recorder redacted them. With ``best_effort`` a storage
    failure is logged and swallowed, so a measurement never fails the
    tool call it describes; without it the failure is raised. A write
    another writer's lock refused (`_busy`) is tried up to ``attempts``
    times in all, `EVENT_RETRY_PAUSE_SECONDS` apart, and once only inside
    another call's transaction, which already holds the lock (a busy
    there is not the lock's to give). With a ``deadline`` (a
    `time.monotonic()` reading: a hook body's, `events.within_hook_budget`)
    no try waits for the lock past it, and none is begun once it has
    passed but the first, which takes the lock only if it is free. A
    store open for reading only (its
    key not held here, `Store.key_state`, or opened read-only by its
    versions, SQLITE_READONLY) records no row of any class, with one
    warning, and its reads go on. A kind that names a mutation or a
    control row is a programming error and always raises."""
    if kind in _NOT_TELEMETRY:
        raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
    if self._key is None:
        log.warning(
            "event %s not recorded: %s", kind, self.key_state.refusal(self._path)
        )
        return None
    payload: dict[str, Any] = _redact_event_fields(dict(fields))
    if actor is not None:
        record = actor.to_record() if isinstance(actor, Actor) else dict(actor)
        if record:
            payload.setdefault("actor", record)
    tries = 1 if self._tx_depth else max(1, attempts)
    for attempt in range(1, tries + 1):
        try:
            with _waiting_until(self, deadline), self._transaction() as tx:
                return self._append(tx, kind, payload, session=session)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            if attempt < tries and _busy(exc) and _time_left(deadline):
                log.info(
                    "event %s: the store was busy (%s); try %d of %d",
                    kind,
                    exc,
                    attempt + 1,
                    tries,
                )
                time.sleep(EVENT_RETRY_PAUSE_SECONDS)
                continue
            if not best_effort and _primary_code(exc) != sqlite3.SQLITE_READONLY:
                raise
            log.warning("event %s not recorded: %s", kind, exc)
            return None
    return None  # pragma: no cover - the loop returns or raises


def import_event(
    self: Store, event: Mapping[str, Any], *, imported_from: str = IMPORTED_FROM_V8
) -> LogRow:
    """One v8 event as a telemetry row under its original ``ts`` and
    ``session``, the rest of its fields as the payload plus
    ``imported_from``, query fields redacted (``event_import_payload``
    says exactly what). An event without a ``ts`` is stamped now.
    Raises rather than swallowing: a migration wants to know."""
    ts, session, kind, payload = event_import_payload(
        event, imported_from=imported_from
    )
    with self._transaction() as tx:
        return self._append(tx, kind, payload, session=session, ts=ts)


def imported_event_keys(
    self: Store, imported_from: str = IMPORTED_FROM_V8
) -> set[tuple[str, str | None, str, str]]:
    """``(ts, session, kind, payload text)`` of every telemetry row
    that names ``imported_from``: what a re-run of the importer skips.
    The payload text is the canonical one the row stores, so the
    caller compares ``canonical_payload`` of what it would write.

    A row is keyed with its free-text fields as the writer keeps them now
    (`events.named_event_fields`): an earlier build imported a v8 event
    with its control tokens as given, and the same event read again is
    written with each by its bare name (ENG-189), so without it a re-run
    imported every such event a second time (the round-2 review's
    migrate probe). A row the naming leaves as it is keeps its stored
    text."""
    needle = canonical_payload({"imported_from": imported_from})[1:-1]
    out: set[tuple[str, str | None, str, str]] = set()
    for row in self._conn.execute(
        "SELECT ts, session, kind, payload FROM log WHERE payload LIKE ? ORDER BY seq",
        (f"%{needle}%",),
    ):
        kind = str(row["kind"])
        if kind in _NOT_TELEMETRY:
            continue
        out.add(
            (
                str(row["ts"]),
                None if row["session"] is None else str(row["session"]),
                kind,
                _named_payload(str(row["payload"])),
            )
        )
    return out


def _named_payload(text: str) -> str:
    """A stored payload's text with its free-text fields named as the
    writer names them (`events.named_event_fields`); `text` itself when
    that changes nothing or it is no JSON object."""
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if not isinstance(payload, dict):
        return text
    named = named_event_fields(payload)
    return text if named is payload else canonical_payload(named)


def iter_events(self: Store, since_seq: int = 0) -> Iterator[dict[str, Any]]:
    """The telemetry rows in the v8 event shape, seq order: ``ts``,
    ``session`` (when the row has one), ``kind``, then the payload's
    fields. Mutation and control rows are not events and are
    skipped; so is a row whose payload is not an object."""
    for row in iter_rows(self._conn, since_seq):
        if row.kind in _NOT_TELEMETRY:
            continue
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        event: dict[str, Any] = {"ts": row.ts}
        if row.session is not None:
            event["session"] = row.session
        event["kind"] = row.kind
        for key, value in payload.items():
            if key not in _ROW_COLUMNS:
                event[key] = value
        yield event


def record_migration(
    self: Store,
    *,
    source: str,
    counts: Mapping[str, Any],
    session: str | None = None,
) -> LogRow:
    """The ``migrate_v8`` control row: which v8 directory was read and
    how much of it this run imports, written before the imports it
    counts so the log says what was attested when."""
    from . import __version__

    payload = {
        "imported_from": IMPORTED_FROM_V8,
        "source": source,
        "counts": dict(counts),
        "engine_version": __version__,
    }
    with self._transaction() as tx:
        return self._append(tx, MIGRATE_V8, payload, session=session)


def record_capture(
    self: Store,
    kind: str,
    payload: Mapping[str, Any],
    *,
    session: str | None = None,
    ts: str | None = None,
    guard: Callable[[], None] | None = None,
) -> LogRow:
    """One capture control row (`log.CAPTURE_KINDS`), written whether or
    not telemetry is on: `record_event` refuses control kinds, and the
    event readers skip them, so capture has this writer and
    `capture_rows` as its reader. `guard` runs inside the transaction,
    under the write lock, before the row is appended; whatever it
    raises aborts the write, which is how opening a batch is atomic
    per session. Inside a `batch` the row commits with the batch."""
    if kind not in CAPTURE_KINDS:
        raise ValueError(f"{kind!r} is not a capture kind")
    with self._transaction() as tx:
        if guard is not None:
            guard()
        return self._append(tx, kind, dict(payload), session=session, ts=ts)


def capture_rows(
    self: Store, kinds: Collection[str], *, since_seq: int = 0
) -> Iterator[LogRow]:
    """The capture rows of `kinds` after `since_seq`, in log order,
    read through the log's kind index."""
    names = sorted(set(kinds) & CAPTURE_KINDS)
    if not names:
        return
    placeholders = ",".join("?" * len(names))
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
        (*names, int(since_seq)),
    ):
        yield LogRow(
            int(raw[0]),
            str(raw[1]),
            None if raw[2] is None else str(raw[2]),
            str(raw[3]),
            str(raw[4]),
            str(raw[5]),
            str(raw[6]),
        )


#: The most strings `events_of_kinds` narrows by (`naming`): one
#: ``instr`` term each. A longer list reads every row of the kinds.
NAMING_BOUND = 200


def events_of_kinds(
    self: Store,
    kinds: Collection[str],
    *,
    since: datetime,
    naming: Collection[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """The telemetry rows of `kinds` stamped at or after `since`, in
    log order. The cut is `events_since`'s (the instant to the second,
    compared against the whole stamp), and so is the start: the window's
    floor, the first row so stamped (`_window_floor`). From there the
    log's kind index gives each kind's rows by sequence number, so a
    window at the log's end costs the rows of those kinds inside it and
    not every row the log ever held of them (at a hundred times the
    owner's store, 1.5 million rows walked for 2,480 returned).

    `naming` keeps only the rows whose payload text holds one of those
    strings, each as the log spells a string (`log.canonical_payload`):
    every row that names one in any field, and possibly a row that holds
    the same characters elsewhere, so the reader still decides what a row
    says. SQLite makes the test before a row is parsed, which is what a
    reader of a few records' events over a long window pays for (a
    search's rejection notes, thirty days of ``use`` rows). An empty
    `naming` reads nothing."""
    names = sorted(set(kinds))
    if not names:
        return
    needles: list[str] = []
    if naming is not None:
        needles = sorted({json.dumps(str(n), ensure_ascii=False)[1:-1] for n in naming})
        if not needles:
            return
        if len(needles) > NAMING_BOUND:
            needles = []
    cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")[:19]
    floor = self._window_floor(cut)
    if floor is None:
        return
    placeholders = ",".join("?" * len(names))
    held = ""
    if needles:
        held = "AND (" + " OR ".join(["instr(payload, ?) > 0"] * len(needles)) + ") "
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({placeholders}) AND seq >= ? AND ts >= ? {held}"
        "ORDER BY seq",
        (*names, floor, cut, *needles),
    ):
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


def session_events_of_kinds(
    self: Store, session: str, kinds: Collection[str]
) -> Iterator[dict[str, Any]]:
    """The telemetry rows of `kinds` recorded under `session`, in log
    order, whenever they were stamped: what a session's recall state
    is rebuilt from. Read through `log_by_session`, named by ``INDEXED
    BY`` because the planner otherwise takes the kind index and compares
    the session on every row the log holds of those kinds. A store
    without the index (schema step 6 has not run on it) refuses the
    hint; the rows are then read by that walk."""
    names = sorted(set(kinds))
    if not names:
        return
    placeholders = ",".join("?" * len(names))
    hinted = (
        "SELECT seq, ts, session, kind, payload, prev_mac, mac "
        "FROM log INDEXED BY log_by_session "
        f"WHERE session = ? AND kind IN ({placeholders}) ORDER BY seq"
    )
    try:
        rows = self._conn.execute(hinted, (session, *names))
    except sqlite3.OperationalError:
        rows = self._conn.execute(
            hinted.replace(" INDEXED BY log_by_session", ""), (session, *names)
        )
    for raw in rows:
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


# -- the event reads --------------------------------------------------------


def events_since(self: Store, since: datetime) -> Iterator[dict[str, Any]]:
    """The telemetry rows stamped at or after `since`, in log order:
    what a window read wants. The cut is the instant to the second,
    nineteen characters, compared against the whole stamp: both
    stamp spellings (``Z`` and ``+00:00``) share that prefix and a
    nineteen-character string sorts below any longer one that starts
    with it, so ``ts >= cut`` reads as a cut on the prefix. The read
    starts at the window's floor, the first row so stamped, found
    through the `log_by_ts` index (`_window_floor`), and walks the rows
    from there in log order, so a window at the log's end costs its
    own rows and not the log's (measured 0.07 ms against 2.1 ms for a
    100-row window on 34,000 rows). A reader of some kinds' rows asks
    `events_of_kinds`, which parses only those."""
    cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")
    # Both spellings sort correctly against an ISO instant to the
    # second; a `Z` sorts after `+`, so the cut compares on the prefix.
    prefix = cut[:19]
    floor = self._window_floor(prefix)
    if floor is None:
        return
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        "WHERE seq >= ? AND ts >= ? ORDER BY seq",
        (floor, prefix),
    ):
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


def _window_floor(
    self: Store, prefix: str, *, head_seq: int | None = None
) -> int | None:
    """The sequence number of the first row stamped at or after `prefix`
    (at or before `head_seq` when given): where a window read starts.
    Found through `log_by_ts`, named by ``INDEXED BY`` because the
    planner otherwise takes the rows in sequence order for the MIN and
    reads most of the log for a window at its end (1.4 ms against
    4 µs on 34,000 rows). None when no row is stamped in the window.
    A row stamped later than the rows after it (an import, a clock set
    back) lowers the floor, and the read from it is then the scan it
    was before, never more. A store whose index is gone (it has been
    in the schema since 9.0.0rc1, and `open` re-creates nothing, so
    only a hand-altered one) refuses the hint; the floor is then read
    without it, by the scan every window read was before."""
    hinted = "SELECT MIN(seq) FROM log INDEXED BY log_by_ts WHERE ts >= ?"
    params: tuple[Any, ...] = (prefix,)
    if head_seq is not None:
        hinted += " AND seq <= ?"
        params = (prefix, head_seq)
    try:
        row = self._conn.execute(hinted, params).fetchone()
    except sqlite3.OperationalError:
        plain = hinted.replace(" INDEXED BY log_by_ts", "")
        row = self._conn.execute(plain, params).fetchone()
    value = None if row is None else row[0]
    return None if value is None else int(value)


def iter_events_backward(self: Store) -> Iterator[dict[str, Any]]:
    """The telemetry rows newest first."""
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        "ORDER BY seq DESC"
    ):
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


# -- the log ------------------------------------------------------------


def log_rows(self: Store, since_seq: int = 0) -> list[LogRow]:
    return list(iter_rows(self._conn, since_seq))


def refold(self: Store) -> dict[str, Any]:
    """Replay every mutation row into a scratch database and compare
    each folded table with the live one. ``unaccounted`` lists live
    rows the replay does not produce (planted or edited), ``missing``
    the replayed rows the live table lacks (deleted). A row whose
    payload cannot be applied is reported as ``payload_invalid`` and
    skipped. The membership tables are folded by the same replay and
    reported under ``membership``, table by table; a membership table
    the live store lacks (a read-only store an earlier build created)
    reads as empty.

    The replay spells each memory's index text (`INDEX_TEXT_COLUMNS`)
    with this build's tokenizer, so the live rows' text is compared only
    while the signed log names that tokenizer (the last `index_rebuilt`
    row, else `store_created`: `store_index.stored_fingerprint`): then an
    edit of the text alone reads as one of the row. While it names another
    (a store an older build wrote, before its owner's open re-spells it,
    `store_index.rebuild_index`), the two columns are left out on both
    sides, so a tokenizer change never reads as tampering (ENG-100). The
    FTS5 index itself is no table the fold compares: `doctor`'s `index`
    check and `backup verify` read it against the rows."""
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    scratch.row_factory = sqlite3.Row
    problems: list[dict[str, Any]] = []
    try:
        scratch.executescript(SCHEMA)
        scratch.executescript(MEMBERSHIP_SCHEMA)
        scratch.execute("BEGIN")
        for row in iter_rows(self._conn):
            if row.kind not in MUTATION_KINDS:
                continue
            try:
                payload = json.loads(row.payload)
                if not isinstance(payload, dict):
                    raise TypeError("payload is not an object")
                _apply_mutation(scratch, row.kind, payload, log_mac=row.mac)
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                problems.append(
                    {
                        "seq": row.seq,
                        "kind": row.kind,
                        "problem": "payload_invalid",
                        "detail": str(exc).splitlines()[0][:200],
                    }
                )
        scratch.execute("COMMIT")
        tables: dict[str, Any] = {}
        membership: dict[str, Any] = {}
        diverged = bool(problems)
        present = {
            str(r[0])
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        spelled_elsewhere = tokenizer_drift(self._conn) is not None
        for table in (*FOLDED_TABLES, *MEMBERSHIP_TABLES):
            skip = (
                INDEX_TEXT_COLUMNS if spelled_elsewhere and table == "memories" else ()
            )
            live = _table_rows(self._conn, table, skip=skip) if table in present else {}
            folded = _table_rows(scratch, table, skip=skip)
            unaccounted = sorted(
                (_key_label(table, k) for k, v in live.items() if folded.get(k) != v),
                key=str,
            )
            missing = sorted(
                (_key_label(table, k) for k in folded if k not in live), key=str
            )
            if unaccounted or missing:
                diverged = True
            report = tables if table in FOLDED_TABLES else membership
            report[table] = {
                "unaccounted": unaccounted,
                "missing": missing,
                "rows": len(live),
            }
        # In place: a connection `reopen` made shares this set, and a
        # worker may read it meanwhile. Add first, then drop the rest:
        # each step is one call on the set, and between them it holds
        # every id still unaccounted, never fewer.
        found = set(tables["memories"]["unaccounted"])
        self._unaccounted_memory_ids.update(found)
        self._unaccounted_memory_ids.intersection_update(found)
        return {
            "status": "diverged" if diverged else "ok",
            "tables": tables,
            "membership": membership,
            "problems": problems,
        }
    finally:
        scratch.close()


def log_verify(self: Store) -> dict[str, Any]:
    """The chain report merged with the fold: ``tampered`` when a row
    fails its MAC, the chain breaks, the head names a row the log no
    longer has, the origin anchor no longer matches it, or a table
    differs from the fold; ``unverifiable`` when a segment's key or
    the head is absent; ``ok`` otherwise. ``origin`` is the anchor
    check (`log.OriginCheck.to_dict`)."""
    chain = verify_chain(
        self._conn, keyring=self._keyring, current_fingerprint=self.key_fingerprint
    ).to_dict()
    fold = self.refold()
    problems = list(chain["problems"]) + list(fold["problems"])
    problems.sort(key=lambda p: (int(p["seq"]), str(p["problem"])))
    status = chain["status"]
    if fold["status"] != "ok":
        status = "tampered"
    return {
        "status": status,
        "path": str(self._path),
        "store_id": self._store_id,
        "key_fingerprint": self.key_fingerprint,
        "rows": chain["rows"],
        "segments": chain["segments"],
        "head": chain["head"],
        "origin": chain["origin"],
        "problems": problems,
        "fold": {
            "status": fold["status"],
            "tables": fold["tables"],
            "membership": fold["membership"],
        },
        "unaccounted_ids": list(fold["tables"]["memories"]["unaccounted"]),
    }
