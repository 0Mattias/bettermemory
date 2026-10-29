"""The `Store` methods of the telemetry rows, the event reads and the log.

Recording and importing events (`record_event`, `import_event`,
`record_migration`, `record_capture`), the event reads (`iter_events`,
`events_of_kinds`, `events_since` with its window memo, `_EVENT_WINDOWS`,
registered with `_caches`), and the log itself (`log_rows`, `refold`, the
replay that applies every mutation row through `_apply_mutation` into a
scratch database, and `log_verify`). Each method is a function of
`self: Store` that `Store` binds by name. It logs as `bettermemory.store`.
A test that patches `EVENT_WINDOW_STORES` patches this module. It lives
apart from `store.py` to keep each module under the package's 2,000-line
bound (ENG-96); it imports `store_schema` and `store_rows`, and `store`
only for type checking.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError

from . import _caches
from .events import _redact_event_fields
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
from .store_rows import (
    _ROW_COLUMNS,
    _apply_mutation,
    _event_from_columns,
    _iso,
    _key_label,
    _table_rows,
    event_import_payload,
)

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")


class _WindowRow(NamedTuple):
    """One telemetry row of an event window, as the log holds it: its
    stamp's first nineteen characters (the window's cut compares on
    them), its columns, and its payload text, parsed again for each
    read so no reader holds another's event."""

    cut: str
    ts: str
    session: str | None
    kind: str
    payload: str


@dataclass(frozen=True)
class _EventWindow:
    """The rows `Store.events_since` read for one store's window: every
    telemetry row whose stamp's first nineteen characters are at or after
    `cut`, in log order, among the rows up to `head_seq`, the log's last
    row when they were read, whose MAC was `head_mac`."""

    cut: str
    head_seq: int
    head_mac: str
    rows: tuple[_WindowRow, ...]


# The event window memo of `Store.events_since(since, memoised=True)`, the
# read `memory_search` makes on every search that returns a hit. The
# uncached read filters the whole log on `substr(ts, 1, 19)`, which no
# index serves, for the few rows inside the window. The log only grows:
# a row is appended at the next sequence number under the write lock and
# never changed, and each row's MAC covers the MAC of the row before it.
# So the memo keeps, per store, the window's rows up to the log's head as
# it read it, keyed on that head: a later read whose cut is at or after the
# memo's checks that the row at the head's sequence number still carries
# the head's MAC, drops the kept rows the new cut excludes, and reads only
# the rows past the head, the new cut applied in SQL. A cut before the
# memo's, a head row gone or carrying another MAC (the tail removed, a row
# appended at its number) or a read inside an open transaction reads the
# whole log as the uncached code does. An edit outside the store that
# leaves the head row's MAC as it was is not seen: a row below the head
# edited with its MAC kept or deleted, and the head row edited with its MAC
# kept (the chain then fails `bettermemory log verify` in each case); the
# memo serves the rows it read. Keyed by the store file and its
# id, so two connections to one store share it; bounded to the most
# recently read stores, not in rows: a store's entry keeps every row in the
# window, its payload text and four short strings, about 1.3 KB a row for
# payloads of about 1 KB (26 MB for a window of 20,000 such rows, measured
# with tracemalloc on Python 3.13), and a window holds whatever the
# lookback's span logged. The lock guards each look-up and each store, the
# reads run outside it. Registered with `_caches`, which empties it before
# each test.
EVENT_WINDOW_STORES = 8

_EVENT_WINDOWS: OrderedDict[tuple[str, str], _EventWindow] = OrderedDict()
_EVENT_WINDOWS_LOCK = threading.Lock()


@_caches.register
def _clear_event_windows() -> None:
    with _EVENT_WINDOWS_LOCK:
        _EVENT_WINDOWS.clear()


# -- telemetry ----------------------------------------------------------


def record_event(
    self: Store,
    kind: str,
    *,
    session: str | None = None,
    actor: Actor | Mapping[str, Any] | None = None,
    best_effort: bool = True,
    **fields: Any,
) -> LogRow | None:
    """One telemetry row. ``query`` and ``probe_query`` are redacted to
    a hash, a short preview and a length before the row is signed, as
    the v8 recorder redacted them. With ``best_effort`` a storage
    failure is logged and swallowed, so telemetry never fails the tool
    call it describes; a kind that names a mutation or a control row
    is a programming error and always raises."""
    if kind in _NOT_TELEMETRY:
        raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
    payload: dict[str, Any] = _redact_event_fields(dict(fields))
    if actor is not None:
        record = actor.to_record() if isinstance(actor, Actor) else dict(actor)
        if record:
            payload.setdefault("actor", record)
    try:
        with self._transaction() as tx:
            return self._append(tx, kind, payload, session=session)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        if not best_effort:
            raise
        log.warning("event %s not recorded: %s", kind, exc)
        return None


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
    caller compares ``canonical_payload`` of what it would write."""
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
                str(row["payload"]),
            )
        )
    return out


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


def events_of_kinds(
    self: Store, kinds: Collection[str], *, since: datetime
) -> Iterator[dict[str, Any]]:
    """The telemetry rows of `kinds` stamped at or after `since`, in
    log order, read through the log's kind index rather than the
    whole log `events_since` walks. The cut compares the stamp's
    first nineteen characters, as `events_since` does."""
    names = sorted(set(kinds))
    if not names:
        return
    cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")[:19]
    placeholders = ",".join("?" * len(names))
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({placeholders}) AND substr(ts, 1, 19) >= ? "
        "ORDER BY seq",
        (*names, cut),
    ):
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


def session_events_of_kinds(
    self: Store, session: str, kinds: Collection[str]
) -> Iterator[dict[str, Any]]:
    """The telemetry rows of `kinds` recorded under `session`, in log
    order, whenever they were stamped: what a session's recall state
    is rebuilt from. Read through the log's kind index, the session
    compared on each row of those kinds."""
    names = sorted(set(kinds))
    if not names:
        return
    placeholders = ",".join("?" * len(names))
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({placeholders}) AND session = ? ORDER BY seq",
        (*names, session),
    ):
        event = self._event_from_raw(raw)
        if event is not None:
            yield event


# -- the event reads --------------------------------------------------------


def events_since(
    self: Store, since: datetime, *, memoised: bool = False
) -> Iterator[dict[str, Any]]:
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
    100-row window on 34,000 rows). With `memoised`, the rows come from the store's event
    window memo (`_EVENT_WINDOWS`), which reads only the rows past the
    log's head as it last read it; the answer is the same, and each
    call gets events of its own. For a caller whose `since` only moves
    forward, as `memory_search`'s lookback does; a `since` earlier than
    the last memoised one reads the whole log again."""
    cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")
    # Both spellings sort correctly against an ISO instant to the
    # second; a `Z` sorts after `+`, so the cut compares on the prefix.
    prefix = cut[:19]
    if memoised:
        for row in self._window_rows(prefix):
            event = _event_from_columns(row.ts, row.session, row.kind, row.payload)
            if event is not None:
                yield event
        return
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


def _window_rows(self: Store, prefix: str) -> tuple[_WindowRow, ...]:
    """The telemetry rows whose stamp's first nineteen characters are at
    or after `prefix`, in log order, through the event window memo."""
    key = (str(self._path), self._store_id)
    in_transaction = bool(self._tx_depth) or self._conn.in_transaction
    with _EVENT_WINDOWS_LOCK:
        window = _EVENT_WINDOWS.get(key)
        if window is not None:
            _EVENT_WINDOWS.move_to_end(key)
    rows: list[_WindowRow] | None = None
    head_seq, head_mac = 0, ""
    if window is not None and window.cut <= prefix and not in_transaction:
        head = self._conn.execute(
            "SELECT mac FROM log WHERE seq = ?", (window.head_seq,)
        ).fetchone()
        if head is not None and str(head["mac"]) == window.head_mac:
            rows = [row for row in window.rows if row.cut >= prefix]
            head_seq, head_mac = window.head_seq, window.head_mac
            # Past the head, the cut is applied by SQLite, as the
            # uncached read applies it.
            read = self._conn.execute(
                "SELECT seq, ts, session, kind, payload, mac, "
                "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                "FROM log WHERE seq > ? ORDER BY seq",
                (prefix, head_seq),
            )
            head_seq, head_mac = self._gather_window(read, rows, head_seq, head_mac)
    if rows is None:
        rows = []
        # The head first, then the window's rows at or before it, so the
        # memo's head and its rows are one snapshot without a
        # transaction: a row appended between the two reads has a higher
        # sequence number, and the next read, from the head, picks it
        # up. The floor comes through `log_by_ts` (`_window_floor`), so
        # the read touches the window's rows where one statement
        # scanned the log.
        head = self._conn.execute(
            "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if head is not None:
            head_seq, head_mac = int(head["seq"]), str(head["mac"])
            floor = self._window_floor(prefix, head_seq=head_seq)
            if floor is not None:
                read = self._conn.execute(
                    "SELECT seq, ts, session, kind, payload, mac, "
                    "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                    "FROM log WHERE seq >= ? AND seq <= ? AND ts >= ? "
                    "ORDER BY seq",
                    (prefix, floor, head_seq, prefix),
                )
                # The head stays the log's last row, not the window's.
                self._gather_window(read, rows, head_seq, head_mac)
    kept = tuple(rows)
    if not in_transaction and all(isinstance(row.cut, str) for row in kept):
        with _EVENT_WINDOWS_LOCK:
            _EVENT_WINDOWS[key] = _EventWindow(
                cut=prefix, head_seq=head_seq, head_mac=head_mac, rows=kept
            )
            _EVENT_WINDOWS.move_to_end(key)
            while len(_EVENT_WINDOWS) > EVENT_WINDOW_STORES:
                _EVENT_WINDOWS.popitem(last=False)
    return kept


def _gather_window(
    read: sqlite3.Cursor, rows: list[_WindowRow], head_seq: int, head_mac: str
) -> tuple[int, str]:
    """Append the in-window telemetry rows of `read` to `rows`; return
    the last row's sequence number and MAC, the new head."""
    for raw in read:
        head_seq, head_mac = int(raw["seq"]), str(raw["mac"])
        kind = str(raw["kind"])
        if not raw["inside"] or kind in _NOT_TELEMETRY:
            continue
        rows.append(
            _WindowRow(
                cut=raw["cut"],
                ts=str(raw["ts"]),
                session=None if raw["session"] is None else str(raw["session"]),
                kind=kind,
                payload=raw["payload"],
            )
        )
    return head_seq, head_mac


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
    reads as empty."""
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
        for table in (*FOLDED_TABLES, *MEMBERSHIP_TABLES):
            live = _table_rows(self._conn, table) if table in present else {}
            folded = _table_rows(scratch, table)
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
