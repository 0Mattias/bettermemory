"""The ``store_restored`` row a restore could not write, appended by the
writing open that comes after it (ENG-208).

`backup.restore_bundle` keeps a record beside the head
(`restore_record_path`: ``<store_id>.backup_restore.json`` in the keys
directory) and writes a restore's entry there before it opens the
restored store to append the ``store_restored`` row carrying the same
entry (`log.append_history_replaced`); the entry names that row once it
is on the log (``row``, null until then, `record_row`). Two ways left an
entry with no row for good, both rc14 Known limitations:

* A restore that failed between its record and its row (the restored
  store's open failed, or the process was killed there). The restore
  reported failed and kept the restore lock's record; running it again
  wrote a second entry and its row, and nothing wrote the first one's.
* A restore onto a store waiting on a schema step this build runs. The
  restore opens the restored store as a reader (``allow_rekey=False``),
  and a reader's open of a store waiting on a step that is not additive
  reads it with every write refused (``PRAGMA query_only``,
  `store_steps.run_steps`), so the restore appended nothing, and the
  owner's open that later ran the step added nothing either.

Either way the record said a restore happened only on the host that ran
it, while the log, which `log export` ships, `log verify` lists and
`memory history` reads, said nothing. `append_missing` closes both: the
owner's writing open (`store_open`, beside the origin anchor's upkeep,
through `settle`) and the restore itself read the record's entries for
the store that still lack their row (`pending`) and append, in the
record's order, the row each would have had: the same payload, under the
store's current key, through the same helper the restore uses, so `log
verify`, the chain and the signatures read it as any row. No schema step
and no new field: the record's ``row`` field and the ``store_restored``
kind are both 9.0.0rc14's, so every candidate since reads a store and a
record this has touched.

Which entries. One the restore wrote (``kind`` ``store_restored``, or no
``kind`` and not an accepted history: `log.history_replaced_kind`), for
this store id, whose ``row`` is there and null. An entry from before
entries named their row (no ``row`` key, rc13 and earlier) is left
alone: its row may be on the log already. An accepted history
(`log accept-history`) writes its row before its entry, so it never
lacks one.

Which log (`_standing`). An entry's row goes only on a log that holds
the history the restore put in place, and only while nothing the
restore did not write stands past it:

* The row at the entry's ``head_after`` seq carries its MAC: the
  bundle's head, which the restore rewound the log to. Else the log is
  another history (``elsewhere``).
* The log is not the history the restore moved aside (``replaced``).
  That file holds the bundle's head too, since a bundle is taken from
  the store's own history, so an operator who put it back by hand after
  a failed restore would have it claim a restore it never had. Where the
  entry's ``head_before`` is past ``head_after`` it tells, by MAC: the
  moved-aside history holds the replaced head's MAC at that seq, and
  the restored one does not. Only where it says nothing (``head_before``
  null, or not past ``head_after``: no head checkpoint at the restore, or
  one at or before the bundle's head) does the time tell instead: the
  first row past ``head_after`` was written before the restore ran (its
  ``ts`` before the entry's ``restored_at``), as no row of a restored
  log past the bundle's head is. That is the one test a clock can
  mislead (a host clock stepped back after the restore), so it is not
  used where a MAC answers, and its warning names each reading
  (`_clock_reading`): the history the restore replaced, or another copy
  of the store where the restore read no log here, or a clock set back;
  that file is not placed again while the row stands.
  ``log_end_before`` cannot tell either: it names the replaced log's
  last seq and the file it was read from, not the MAC there, and a
  restored log reaches the same seq as it grows.
* No row past ``head_after`` is a record's (``record``): every row there
  is a control row (`log.CONTROL_KINDS`: a rekey, another restore's or
  an accepted history's row, an index rebuild, a capture's
  bookkeeping), which changes no record. A row appended now stands at
  the log's end, and `memory history` reads a `store_restored` row as a
  rewind of every record whose first row comes before it
  (`history._with_restores`): a late row after a record written since
  the restore would say the restore rewound that record, from a seq it
  never reached. Such a record is there when a build from before
  9.0.0rc18 wrote to the store after the restore and before this
  build's first owner open (those builds append no late row), or when
  an open that is not the owner's and so appends none (the `key`
  commands, `export`, `membership`) wrote one first.

Settled, and said once. An entry these keep off the log is withheld:
the open writes ``row_withheld`` into it, ``{seq, kind, mac, because}``,
``because`` the test that kept it off and ``seq``, ``kind`` and ``mac``
the row that decided it (the first record row; the replaced head, or
the first row past the bundle's head; the row at ``head_after``, or none
there), and logs it once, when ``because`` first takes that value
(`_report_withheld`). Every entry so reaches one of three states: its
row on the log and named, withheld, or (on a store that refuses writes)
waiting for the open that can write. A withheld entry is looked at
again at each owner open and each restore by that one row alone
(`_still_stands`, one indexed read): while the row is on the log with
its MAC, the reason stands, since a MAC is chained to every row before
it; once it is gone (a later restore took the record off the log, or
the file put back by hand was swapped again) the entry is placed anew,
and appended if it now can be, ``row_withheld`` dropped as ``row``
names the row. The record keeps a withheld restore, ``row`` null; `log
export` reads its rewind from there as before, `log verify` reads the
log ``ok`` and lists the restores whose rows it holds, and `doctor` does
not read the record, so nothing reads the missing row as damage: the
store is as rc14 to rc17 left such a restore, and the log says nothing
it cannot place.

Exactly once. The check and the append run in one write transaction
(``BEGIN IMMEDIATE``, `store.Store._transaction`), and the check first
looks for a ``store_restored`` row past ``head_after`` whose payload is
the entry's (`_logged`): two writing opens racing each other append one
row between them, and a row the restore appended before it died, or
before its record could be written, is named in the record rather than
appended again, records written after it or not.

Where the row stands. A row this appends stands where the open found the
log's end, after control rows only, with the time it was appended; its
payload's ``restored_at`` says when the restore ran. A restore that runs
again appends the earlier entry's row before its own, so the log tells
the two in the order they ran. A store that is part restored refuses
every writing open until a restore finishes or `backup unlock --yes`
(`restore_lock`), and a store waiting on a step refuses every write
until the owner's open runs the step; this build's owner open (every
command that writes records but `membership`, and the daemon) appends
the late row before it returns, and a record that came first withholds
it, so a late row never stands after a record written since the restore.
The row's payload is the entry's less ``row`` and ``row_withheld``, the
record's own bookkeeping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._fsutil import atomic_write_bytes, bounded_read, flock_excl
from .log import (
    CONTROL_KINDS,
    STORE_RESTORED,
    append_history_replaced,
    history_replaced_kind,
)
from .time_utils import parse_event_ts

if TYPE_CHECKING:
    from .store import Store

#: The rewind record beside the head: ``<store_id>.backup_restore.json``
#: (`restore_record_path`).
RESTORE_RECORD_SUFFIX = ".backup_restore.json"
#: What a read of the record reads at most: the cap `backup` reads and
#: rewrites the record with (`backup._MANIFEST_CAP_BYTES`, 1 MiB), so this
#: reads every record `backup` keeps; `tests/test_restore_row_late.py`
#: holds the two equal. An entry is mostly paths (seven of its values):
#: one restore's, with one file moved aside and paths of about 130
#: characters, measured 2,472 bytes as the record writes it (2026-11-03,
#: a scratch store), so the cap holds about 400 restores of one store.
RECORD_CAP_BYTES = 1 << 20
#: The key an entry carries while an open withholds its row (module
#: docstring, "Settled, and said once"): ``{seq, kind, mac, because}``.
#: Beside ``row``, which stays null, so a build from before it (9.0.0rc14
#: to 9.0.0rc17, which read ``row`` only through
#: `log.history_replaced_row`, and keep an entry's other keys as they are
#: when they rewrite the record) reads the entry as one whose row was
#: never appended, as it is.
WITHHELD_KEY = "row_withheld"
#: Why a row is withheld (``because``): a record row stands past the
#: bundle's head; the log is the history the restore replaced; the log
#: does not hold the bundle's head the restore put back.
BECAUSE_RECORD = "record"
BECAUSE_REPLACED = "replaced"
BECAUSE_ELSEWHERE = "elsewhere"
#: The kinds a row past an entry's ``head_after`` may have for the late
#: row to be appended after it: the control rows, which change no record
#: (`log.CONTROL_KINDS`, each kind's comment there), as SQL parameters.
_CONTROL = tuple(sorted(CONTROL_KINDS))
#: `_settle_one`'s answers: the row is appended (or named, when it is
#: there); it is withheld (`BECAUSE_RECORD` and the rest say why); it is
#: on the log already (`_decide`); the entry's withholding still stands
#: (`_still_stands`), and nothing was read past that.
_APPEND = "append"
_WITHHELD = "withheld"
_LOGGED = "logged"
_KEPT = "kept"

log = logging.getLogger(__name__)


def restore_record_path(keys_dir: Path, store_id: str) -> Path:
    """``<keys dir>/<store_id>.backup_restore.json``, beside the head it
    records the rewinds of, named after the store as the key ring's files
    are, since one keys directory serves every store on the host."""
    return Path(keys_dir) / f"{store_id}{RESTORE_RECORD_SUFFIX}"


def _write_private(path: Path, data: bytes) -> None:
    """Atomic and durable, owner-only from the moment it is visible, as
    `backup` writes the record."""
    atomic_write_bytes(path, data, mode_before_rename=0o600)


def record_row(path: Path, entry: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """Name `row`, the log row appended for `entry`, in `entry`'s place in
    the record at `path`: the last entry of its kind, store id and time
    with no row yet. The record is written before the row, so an entry
    whose row never came stays ``row: null`` until `append_missing`
    writes it. A record that no longer reads, holds no such entry or
    cannot be written is left as it is: the row is on the log either way
    (`log.history_replaced_row`), and the next writing open names it
    (`_logged`). A ``row_withheld`` the entry carried goes as the row is
    named: the reason it stood for is gone."""
    _fill(path, entry, "row", row, drop=WITHHELD_KEY)


#: `_fill`'s outcomes: the record was written; the entry held the value
#: already (another open wrote it first); the record does not read or
#: holds no such entry; the record could not be written.
_WRITTEN = "written"
_UNCHANGED = "unchanged"
_MISSING = "missing"
_FAILED = "failed"


def _fill(
    path: Path,
    entry: Mapping[str, Any],
    key: str,
    value: Mapping[str, Any],
    *,
    drop: str | None = None,
) -> tuple[str, object]:
    """Set `key` to `value` in `entry`'s place in the record at `path` (the
    last entry of its kind, store id and time whose row is null), removing
    `drop` from it; return the outcome (`_WRITTEN`, `_UNCHANGED`,
    `_MISSING`, `_FAILED`) and the value `key` held before. Under the
    record's lock, so of two opens writing the same value one writes it
    and the other finds it there."""
    keys = ("kind", "store_id", "restored_at")
    with flock_excl(path):
        try:
            document = json.loads(bounded_read(path, RECORD_CAP_BYTES))
            restores = document["restores"]
            found = [
                item
                for item in restores
                if isinstance(item, dict)
                and item.get("row") is None
                and all(item.get(name) == entry.get(name) for name in keys)
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return _MISSING, None
        if not found:
            return _MISSING, None
        target = found[-1]
        previous = target.get(key)
        if previous == dict(value) and (drop is None or drop not in target):
            return _UNCHANGED, previous
        target[key] = dict(value)
        if drop is not None:
            target.pop(drop, None)
        encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
        try:
            _write_private(path, encoded.encode())
        except OSError:
            return _FAILED, previous
        return _WRITTEN, previous


def pending(store: Store) -> list[dict[str, Any]]:
    """The entries of `store`'s restore record that a restore wrote and
    whose row is not on the log yet, in the record's order (the module
    docstring says which). None of them when there is no record; a record
    that does not read is logged once here and left for `backup`, which
    moves it aside at the next restore, to deal with."""
    path = restore_record_path(store.keys_dir, store.store_id)
    try:
        document = json.loads(bounded_read(path, RECORD_CAP_BYTES))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning(
            "store %s: the restore record %s does not read (%s); a restore it "
            "names with no row gets none",
            store.path,
            path,
            exc,
        )
        return []
    restores = document.get("restores") if isinstance(document, dict) else None
    if not isinstance(restores, list):
        return []
    return [
        dict(entry)
        for entry in restores
        if isinstance(entry, dict)
        and history_replaced_kind(entry) == STORE_RESTORED
        and "row" in entry
        and entry["row"] is None
        and entry.get("store_id") == store.store_id
    ]


def _seq_mac(value: object) -> tuple[int, str] | None:
    """A ``{seq, mac}`` an entry carries, or None when it is not one."""
    if not isinstance(value, Mapping):
        return None
    seq, mac = value.get("seq"), value.get("mac")
    if type(seq) is not int or not isinstance(mac, str):
        return None
    return seq, mac


def _mac_at(conn: sqlite3.Connection, seq: int) -> str | None:
    """The MAC of this log's row at `seq`, or None when it holds none."""
    found = conn.execute("SELECT mac FROM log WHERE seq = ?", (seq,)).fetchone()
    return None if found is None else str(found[0])


def _written_before(ts: object, restored_at: object) -> bool:
    """Whether a row stamped `ts` was written before the restore that ran
    at `restored_at`. False when either does not read as a time: an
    entry whose time is unreadable is placed by its MACs alone."""
    written, ran = parse_event_ts(ts), parse_event_ts(restored_at)
    return written is not None and ran is not None and written < ran


def _row_at(conn: sqlite3.Connection, seq: int) -> dict[str, Any] | None:
    """This log's row at `seq` as ``{seq, kind, mac}``, or None."""
    found = conn.execute("SELECT kind, mac FROM log WHERE seq = ?", (seq,)).fetchone()
    if found is None:
        return None
    return {"seq": seq, "kind": str(found[0]), "mac": str(found[1])}


def _replaced_head(entry: Mapping[str, Any]) -> tuple[int, str] | None:
    """The head the restore `entry` records replaced, as ``(seq, mac)``,
    when it tells the two histories apart by MAC: its ``head_before``,
    where that is past ``head_after``. None when it says nothing (no head
    checkpoint at the restore, or one behind the log, at or before the
    bundle's head), and the time places the log instead (`_standing`), as
    the warning says (`_report_withheld`)."""
    before = _seq_mac(entry.get("head_before"))
    after = _seq_mac(entry.get("head_after"))
    if before is None or after is None or before[0] <= after[0]:
        return None
    return before


def _withheld(row: Mapping[str, Any], because: str) -> tuple[str, dict[str, Any]]:
    return _WITHHELD, {
        "seq": row.get("seq"),
        "kind": row.get("kind"),
        "mac": row.get("mac"),
        "because": because,
    }


def _standing(
    conn: sqlite3.Connection, entry: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Where the restore `entry` records stands on this log (module
    docstring, "Which log"): ``(_APPEND, None)`` when this log is the
    history it put in place and only control rows, or none, stand past
    the bundle's head; else ``(_WITHHELD, {seq, kind, mac, because})``,
    the row that decided it and why (`BECAUSE_ELSEWHERE`,
    `BECAUSE_REPLACED`, `BECAUSE_RECORD`). An entry that names no
    readable head is withheld as elsewhere with no row (``seq`` None): no
    log places it."""
    after = _seq_mac(entry.get("head_after"))
    if after is None:
        return _withheld({}, BECAUSE_ELSEWHERE)
    at_after = _row_at(conn, after[0])
    if at_after is None or at_after["mac"] != after[1]:
        return _withheld(at_after or {"seq": after[0]}, BECAUSE_ELSEWHERE)
    before = _replaced_head(entry)
    if before is not None:
        # The replaced head tells the two histories apart by MAC; the
        # clock is not asked.
        at_before = _row_at(conn, before[0])
        if at_before is not None and at_before["mac"] == before[1]:
            return _withheld(at_before, BECAUSE_REPLACED)
    else:
        first = conn.execute(
            "SELECT seq, kind, mac, ts FROM log WHERE seq > ? ORDER BY seq LIMIT 1",
            (after[0],),
        ).fetchone()
        if first is not None and _written_before(first[3], entry.get("restored_at")):
            row = {"seq": int(first[0]), "kind": str(first[1]), "mac": str(first[2])}
            return _withheld(row, BECAUSE_REPLACED)
    marks = ", ".join("?" for _ in _CONTROL)
    record = conn.execute(
        f"SELECT seq, kind, mac FROM log WHERE seq > ? AND kind NOT IN ({marks}) "
        "ORDER BY seq LIMIT 1",
        (after[0], *_CONTROL),
    ).fetchone()
    if record is not None:
        row = {"seq": int(record[0]), "kind": str(record[1]), "mac": str(record[2])}
        return _withheld(row, BECAUSE_RECORD)
    return _APPEND, None


def _still_stands(conn: sqlite3.Connection, withheld: object) -> bool:
    """Whether the ``row_withheld`` an entry carries still holds on this
    log: the row that decided it is there with its MAC (or, where none was
    at that seq, still none is), so the reason it names stands and the
    entry is not placed again. A marker of another shape (none, or one
    without ``because``) does not hold, and the entry is placed."""
    if not isinstance(withheld, Mapping) or "because" not in withheld:
        return False
    seq, mac = withheld.get("seq"), withheld.get("mac")
    if seq is None:
        return True
    if type(seq) is not int or not (mac is None or isinstance(mac, str)):
        return False
    return _mac_at(conn, seq) == mac


def _payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The entry as its row carries it: less ``row_withheld``, the
    record's bookkeeping (`log.append_history_replaced` drops ``row``)."""
    return {name: value for name, value in entry.items() if name != WITHHELD_KEY}


def _logged(
    conn: sqlite3.Connection, entry: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The ``store_restored`` row past the entry's ``head_after`` whose
    payload is the entry's (less ``row``, which names the row,
    ``row_withheld``, and the ``engine_version`` the appending build
    adds), as ``{seq, mac}``, or None: the row a restore appended and
    could not name in its record, or one a racing writing open appended
    first. The ``log_by_kind`` index finds the candidates."""
    after = _seq_mac(entry.get("head_after"))
    if after is None:
        return None
    wanted = {name: value for name, value in _payload(entry).items() if name != "row"}
    for seq, mac, payload in conn.execute(
        "SELECT seq, mac, payload FROM log WHERE kind = ? AND seq > ? ORDER BY seq",
        (STORE_RESTORED, after[0]),
    ):
        try:
            carried = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(carried, dict):
            continue
        carried.pop("engine_version", None)
        if carried == wanted:
            return {"seq": int(seq), "mac": str(mac)}
    return None


def _decide(
    conn: sqlite3.Connection, entry: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """`_standing`, with the entry's row looked for first where this log
    is the restore's: ``(_LOGGED, {seq, mac})`` when it is on the log
    already (`_logged`), which is named whatever stands after it."""
    standing, value = _standing(conn, entry)
    if standing == _WITHHELD and value is not None:
        if value["because"] != BECAUSE_RECORD:
            return standing, value
    found = _logged(conn, entry)
    if found is not None:
        return _LOGGED, found
    return standing, value


def _settle_one(
    store: Store, entry: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """`entry`'s row on `store`'s log: ``(_KEPT, None)`` while the
    withholding it carries stands (`_still_stands`); ``(_LOGGED, {seq,
    mac})`` for the row there already; ``(_APPEND, {seq, mac})`` for one
    appended now; ``(_WITHHELD, {seq, kind, mac, because})`` when it is
    kept off this log (`_standing`). Read outside the write lock first,
    so an entry that is not to be appended takes no lock at any open;
    the append is checked again in its write transaction, so a racing
    open appends none."""
    if _still_stands(store.conn, entry.get(WITHHELD_KEY)):
        return _KEPT, None
    verdict, value = _decide(store.conn, entry)
    if verdict != _APPEND:
        return verdict, value
    with store._transaction() as tx:
        verdict, value = _decide(tx, entry)
        if verdict != _APPEND:
            return verdict, value
        return _APPEND, append_history_replaced(store, STORE_RESTORED, _payload(entry))


def append_missing(
    store: Store, entries: Sequence[Mapping[str, Any]] | None = None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Put on `store`'s log the ``store_restored`` row of each of
    `entries` (default: `pending`) whose restore put this log's history in
    place and that no record row stands after (`_standing`), in their
    order, and name each row in its entry in the record; return each
    entry so settled with its row (``{seq, mac}``). An entry kept off the
    log gets ``row_withheld`` in the record instead, said once
    (`_report_withheld`); one that carries it is looked at again only
    once the row that decided it is gone (`_still_stands`). Nothing, and
    nothing read, when the store refuses writes (``PRAGMA query_only``: a
    store still waiting on a schema step, or one whose versions keep this
    build to reading). Raises what the append raises (a busy store, a key
    not held), leaving the rows not yet appended for the next writing
    open; `settle` is the open's call, which logs it instead."""
    if store.conn.execute("PRAGMA query_only").fetchone()[0]:
        return []
    todo = pending(store) if entries is None else list(entries)
    if not todo:
        return []
    path = restore_record_path(store.keys_dir, store.store_id)
    settled: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in todo:
        verdict, value = _settle_one(store, entry)
        if verdict == _KEPT or value is None:
            continue
        if verdict == _WITHHELD:
            _report_withheld(store, path, entry, value)
            continue
        record_row(path, entry, value)
        settled.append((dict(entry), value))
    return settled


#: What an open says when it first withholds a row for a reason, by the
#: reason (`_report_withheld`): the store, the restore's time and bundle,
#: and the deciding row (its kind, or that there is none) and seq fill it
#: in.
_SAID = {
    BECAUSE_RECORD: (
        "store %s: the restore at %s (from %s) gets no store_restored row while "
        "%s at seq %s stands past the head it put back, written since by "
        "a build or command that appends no late row: a row appended after it "
        "would read as a rewind of that record. The restore record keeps the "
        "restore (row_withheld), `log export` reads its rewind from there, and "
        "the row is appended if a later restore takes that row off the log"
    ),
    BECAUSE_REPLACED: (
        "store %s: this store file holds the history the restore at %s (from "
        "%s) replaced, not the one it put in place (%s at seq %s is the "
        "replaced history's), so it gets no store_restored row; the restore "
        "record keeps the restore (row_withheld), and the row is appended if "
        "the restored file comes back"
    ),
    BECAUSE_ELSEWHERE: (
        "store %s: this store file does not hold the bundle head the restore at "
        "%s (from %s) put back (it has %s at seq %s), so it gets no "
        "store_restored row here; the restore record keeps the restore "
        "(row_withheld)"
    ),
}
#: What an open says for `BECAUSE_REPLACED` when the time decided it (the
#: entry names no replaced head past the bundle's: `_replaced_head`), with
#: the same fill and then what such a file may be and why nothing tells
#: which (`_clock_reading`). A first row past the bundle's head stamped
#: before the restore is a history written before it, or the restored one
#: under a host clock stepped back since, and no MAC tells which: the row
#: that decided it stays on that file, so the file is not placed again.
_SAID_BY_CLOCK = (
    "store %s: the restore at %s (from %s) gets no store_restored row here: "
    "this store file's first row past the bundle's head (%s at seq %s) is "
    "stamped before the restore: %s; with %s to compare, nothing tells "
    "which. The restore record keeps the restore (row_withheld), and this "
    "file is not placed again while that row stands"
)


def _clock_reading(entry: Mapping[str, Any]) -> tuple[str, str]:
    """For `_SAID_BY_CLOCK`: what a file whose first row past the bundle's
    head was written before the restore may be, and why the time had to
    decide, by what the entry `entry` holds.

    What: where the restore read the log it replaced (``log_end_before``),
    that history, or a clock set back. Where it read none but a head
    checkpoint (``head_before``), the history that head named, or another
    copy of the store, or a clock set back. Where it read neither, this
    host held no copy of the store when the restore ran
    (`backup._replaced_log_end`), so it replaced no history here: another
    copy of the store written before it (the source host's file, say), or
    a clock set back.

    Why: no head checkpoint at the restore (``head_before`` null), or one
    at or before the bundle's head, which a restored log reaches too
    (``head_before`` not past ``head_after``)."""
    before = _seq_mac(entry.get("head_before"))
    after = _seq_mac(entry.get("head_after"))
    if entry.get("log_end_before") is not None:
        what = "the history it replaced, or a clock set back"
    elif before is not None:
        what = (
            "a history written before the restore (the one it replaced, or "
            "another copy of the store), or a clock set back"
        )
    else:
        what = (
            "another copy of the store, written before the restore (this host "
            "held none when it ran: head_before and log_end_before null), or a "
            "clock set back"
        )
    if before is None:
        why = "no head checkpoint at the restore (head_before null)"
    else:
        bundle = "?" if after is None else after[0]
        why = (
            f"no replaced head past the bundle's (head_before at seq {before[0]}, "
            f"the bundle's head at seq {bundle})"
        )
    return what, why


def _said(
    store_path: object, entry: Mapping[str, Any], withheld: Mapping[str, Any]
) -> tuple[str, tuple[object, ...]]:
    """The warning `_report_withheld` logs for `withheld`, as its format and
    arguments: `_SAID` by the reason, filled with the store, the restore's
    time and bundle, and the deciding row (its kind, or that there is
    none) and seq; for a ``replaced`` the time decided, `_SAID_BY_CLOCK`,
    with `_clock_reading`'s two parts after them."""
    kind = withheld.get("kind")
    args: tuple[object, ...] = (
        store_path,
        entry.get("restored_at"),
        entry.get("bundle"),
        f"a {kind} row" if isinstance(kind, str) else "no row",
        withheld.get("seq"),
    )
    because = withheld["because"]
    if because == BECAUSE_REPLACED and _replaced_head(entry) is None:
        return _SAID_BY_CLOCK, (*args, *_clock_reading(entry))
    return _SAID[because], args


def _report_withheld(
    store: Store, path: Path, entry: Mapping[str, Any], withheld: Mapping[str, Any]
) -> None:
    """Write `withheld` as the entry's ``row_withheld`` and say so once:
    when the record took it and the reason is new to the entry (a new row
    that decided the same reason is written without a word), in the
    words `_said` picks: a ``replaced`` the time decided says what else
    the file may be (`_SAID_BY_CLOCK`). An open that finds another wrote
    it first says nothing; a record that cannot be written is said, and
    the next owner open decides again."""
    outcome, previous = _fill(path, entry, WITHHELD_KEY, withheld)
    because = withheld["because"]
    if outcome == _FAILED:
        log.warning(
            "store %s: the restore at %s gets no store_restored row here (%s), "
            "and the restore record %s could not be written to say so; the next "
            "owner open decides again",
            store.path,
            entry.get("restored_at"),
            because,
            path,
        )
        return
    if outcome != _WRITTEN:
        return
    if isinstance(previous, Mapping) and previous.get("because") == because:
        return
    said, args = _said(store.path, entry, withheld)
    log.warning(said, *args)


def append_for(store: Store, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The restore's own call (`backup._apply_restore`), on the store it
    has just restored: every entry still lacking its row that this log
    holds the history of, in the record's order, then `entry`, its own
    (read back from the record, where it was written a moment before, or
    taken as given when the record does not hold it); returns `entry`'s
    row, or None when the store refuses writes, which the writing open
    that later lifts that appends (`settle`)."""
    keys = ("kind", "store_id", "restored_at")
    own = json.loads(json.dumps(dict(entry)))
    todo = pending(store)
    if not any(all(e.get(k) == own.get(k) for k in keys) for e in todo):
        todo.append(own)
    for settled, row in append_missing(store, todo):
        if all(settled.get(k) == own.get(k) for k in keys):
            return row
    return None


def settle(store: Store) -> None:
    """The owner's writing open (`store_open`): `append_missing`, which
    never fails the open. A row that could not be appended (the store
    busy past the busy timeout, a full disk) is logged and left for the
    next writing open, as the origin anchor's upkeep is."""
    try:
        settled = append_missing(store)
    except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
        log.warning(
            "store %s: a restore's store_restored row was not appended (%s); "
            "the next writing open appends it",
            store.path,
            exc,
        )
        return
    for entry, row in settled:
        log.warning(
            "store %s: the store_restored row of the restore at %s (from %s) is "
            "at seq %d, and the restore record names it now",
            store.path,
            entry.get("restored_at"),
            entry.get("bundle"),
            row["seq"],
        )


__all__ = [
    "BECAUSE_ELSEWHERE",
    "BECAUSE_RECORD",
    "BECAUSE_REPLACED",
    "RECORD_CAP_BYTES",
    "RESTORE_RECORD_SUFFIX",
    "WITHHELD_KEY",
    "append_for",
    "append_missing",
    "pending",
    "record_row",
    "restore_record_path",
    "settle",
]
