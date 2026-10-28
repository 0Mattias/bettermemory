"""`bettermemory security scan`: where a credential-shaped string persists.

The credential gate (`credentials.py`) runs at one moment: when
`memory_write` or `memory_update` is about to commit a body. Everything
that reaches the store any other way is never looked at again. An episode
skips the gate entirely (`handlers/episode_write.py` checks size and
scope count only). A body written before a detector existed, carried in
by `migrate v8` or an import, or put through the Store API directly, was
never checked by today's detectors. A body the caller waved through with
`acknowledge_credential=True` was checked and kept on purpose. And the
signed log keeps every version of every record: a `memory_put` row for
each write, update, verify and corroboration, a `tombstone_put` for each
removal, an `episode_put` for each journal entry. Editing a secret out,
removing the record and pruning the tombstone each append a row; none of
them takes the earlier payload out of the file, because no path re-signs
history. Before this module, nothing could answer "where does this secret
live now", which is the first question of an incident and the last one
before an export is shared.

What it reads, through the gate's own detectors so it finds exactly what
the gate would refuse (precision over recall, the detector discipline
`credentials.py` documents):

- the record tables: every text column of `memories`, `tombstones`,
  `episodes` and `conflicts` (a conflict row quotes the first hundred
  characters of each record's body, so it is a copy of body text in its
  own right), and every string inside their JSON columns, so claims, link
  notes and the origin's raw remote URL (`origin.repo`, which carries a
  token when the remote was cloned with one) are covered with the body;
- with `include_log`, the payload of every log row, whatever its kind:
  the record versions above, the membership rows (a checkout's raw
  remotes) and the telemetry events (a write event's claims, a
  `standing_guard` quote).

The tables are read as raw rows rather than through `Store.iter_memories`
and its siblings. Those validate each row into a model and raise on one
that does not validate, and a row that entered outside the store (the
`unaccounted` rows `log verify` reports) is exactly where an unvetted
secret sits. The FTS columns and `scopes_text` are skipped: they are
derived from the body and the scopes of the same row, so scanning them
would report every body hit twice. For the same reason a text a row
holds twice (the mirrored `origin_repo` column beside `origin_json`) is
scanned once, under the first field that holds it.

What it reports, per hit: the location (a table, or the log with the
row's seq and kind), the record id, the field as a path into the row
(`body`, `claims[0]`, `origin.repo`, `memory.body` inside a log payload),
the detector kind, the character offset of the first hit of that kind in
the field and how many there are, the record's state (live, tombstoned,
or pruned: in no table, so only the log still holds it), whether the log
shows the write was acknowledged, and what to do about it.

What it never prints is the value. Not the secret, and not the gate's
leak-free snippet either: `credentials._safe_snippet` redacts every span
a detector matched, but its context window runs 24 characters past the
match, and the text right after a detected secret is very often its
undetected partner. An AWS access key id followed by its secret access
key is the plain case: no detector matches the secret key on its own, so
the gate's snippet for the pair carries its first 23 characters
(tests/test_secret_scan.py pins both halves). The gate returns that
snippet to the model that already holds the body; the scan's output goes
to a terminal, a ticket or a CI log, so it reports an offset into the
field instead, which locates the hit for `memory_show` without carrying a
byte of it. Field paths are printed only where a JSON key is a plain
identifier and no detector fires on it, and a record id or a log kind
only where it is a plain token no detector fires on: the id column and
an event's `id` are scanned text like any other.

The acknowledgement comes from the telemetry events. A committed
`write` and a successful `update` event always carry
`credentials_acknowledged`, the detector kinds the caller waved through
(kinds only, never the value). For a record with such events, a hit
whose kind one of them names is `acknowledged: true`, and a hit no event
acknowledged is `false`: the secret reached the record past the gate (an
import, the Store API, an episode copied in) or through a detector added
since. A record with no such event (telemetry off, an imported v8 record,
an episode, a conflict row) is `null`: the log cannot say.

Read-only. The CLI opens the store with `Store.open(...,
allow_rekey=False)`, as `log verify` and `export` do: a scan must never
write a rekey row into the log it is reading, and a store copied from
another machine scans without its key. Nothing here writes.

Not covered, and said in every report: freed pages and earlier page
images in the file and its WAL, the full-text index's token pages, saved
orphans beside the store (named in the report: copied to `memory.sqlite`
in a directory of its own, each scans with `--store`), backups and
exports. The report names them because a clean scan of the records is
not a clean file.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from .credentials import _merged_secret_spans
from .log import iter_rows
from .store import Store, orphans_beside, store_path

# The table pass, in report order. `memories` and `episodes` hold live
# records; `tombstones` holds removed ones until `tombstones prune`;
# `conflicts` holds body quotes for the curation queue.
TABLES = ("memories", "tombstones", "episodes", "conflicts")
LOG = "log"

# Record states. `pruned` is a record no table holds any more: a pruned
# tombstone, a deleted episode, a memory removed by a path that left no
# tombstone. Only the log still has its payload.
LIVE = "live"
TOMBSTONED = "tombstoned"
PRUNED = "pruned"

# Singular and plural, for the text report's counts.
_NOUNS = {
    "memories": ("memory", "memories"),
    "tombstones": ("tombstone", "tombstones"),
    "episodes": ("episode", "episodes"),
    "conflicts": ("conflict", "conflicts"),
    LOG: ("log row", "log rows"),
}

_TABLE_STATE = {
    "memories": LIVE,
    "tombstones": TOMBSTONED,
    "episodes": LIVE,
    "conflicts": LIVE,
}

# Columns derived from other columns of the same row (the FTS token
# streams and the flattened scope text) or structural (the log pointer).
# Scanning a derived column would report a body hit a second time.
_SKIPPED_COLUMNS = frozenset({"body_fts", "scopes_fts", "scopes_text", "log_mac"})

# The log kinds whose payload is one record, and the key it sits under.
_RECORD_PUTS = {
    "memory_put": "memory",
    "tombstone_put": "tombstone",
    "episode_put": "episode",
}
_RECORD_DELETES = frozenset({"memory_delete", "tombstone_delete", "episode_delete"})

# The telemetry kinds that carry `credentials_acknowledged`: the committed
# write and the successful update (handlers/write.py, handlers/update.py).
_ACK_EVENT_KINDS = ("write", "update")
_ACK_FIELD = "credentials_acknowledged"
# Before any event a store holds: `events_of_kinds` cuts on a stamp.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# A JSON key printed as a path segment: a plain identifier. Anything else
# (and an identifier a detector fires on) prints as `*`, so a field path
# can never carry a value.
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_MASKED_KEY = "*"

# A record id or a log kind printed as it is: a plain token (a ULID, a
# `cf-` conflict id, a log kind). The id column and an event's `id` are
# scanned text, and a row written outside the store can hold anything in
# them or in a log row's kind, a secret or a terminal escape included, so
# anything else, and a token a detector fires on, prints as `*`.
_PLAIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_ROTATE = "Rotate the credential at its issuer"
REMEDIATION = {
    "memories": (
        f"{_ROTATE}, then rewrite the record without it (memory_update) or "
        "remove it (memory_remove, then `bettermemory tombstones prune`). "
        "The log keeps the earlier versions."
    ),
    "tombstones": (
        f"{_ROTATE}, then hard-delete the tombstone with `bettermemory "
        "tombstones prune --older-than DAYS` (at least one day after the "
        "removal). The log keeps its payload."
    ),
    "episodes": (
        f"{_ROTATE}. No command edits or removes a single episode: "
        "`bettermemory episodes prune --ttl-days DAYS` deletes whole sessions "
        "past the TTL. The log keeps the payload."
    ),
    "conflicts": (
        f"{_ROTATE}, then fix the quoted record (memory_update or "
        "memory_remove): the conflict scan refreshes a pending row's quote "
        "and drops a row whose record is gone. The log keeps the payload."
    ),
    LOG: (
        f"{_ROTATE}. The signed log keeps every version of every record and "
        "no command erases a log row today: removing or pruning the record "
        "leaves this copy in the store file and in every backup of it."
    ),
}

NOT_SCANNED = (
    "freed pages and earlier page images in the store file and its WAL",
    "the full-text index's token pages",
    "saved orphans beside the store",
    "backups and exports",
)


@dataclass(frozen=True)
class SecretFinding:
    """One detector kind in one field of one stored copy.

    `location` is a table name or ``"log"``; `seq` and `log_kind` are set
    for a log row only. `offset` is the character offset of the first hit
    of `kind` in the field and `occurrences` how many hits of that kind
    the field holds; neither carries any of the value. `record_id` and
    `log_kind` are `*` when they are not plain tokens or a detector fires
    on them. `record_state` is None for a log row that names no record (a
    membership or control row, a telemetry event about something that is
    not a record this store holds). `acknowledged` is None when the log
    holds no gated write for the record."""

    location: str
    record_id: str | None
    seq: int | None
    log_kind: str | None
    field: str
    kind: str
    offset: int
    occurrences: int
    record_state: str | None
    acknowledged: bool | None

    @property
    def remediation(self) -> str:
        return REMEDIATION[self.location]

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "record_id": self.record_id,
            "seq": self.seq,
            "log_kind": self.log_kind,
            "field": self.field,
            "kind": self.kind,
            "offset": self.offset,
            "occurrences": self.occurrences,
            "record_state": self.record_state,
            "acknowledged": self.acknowledged,
            "remediation": self.remediation,
        }


@dataclass
class ScanReport:
    """What `scan_store` read and found. `scanned` counts rows per table,
    and log rows under ``"log"`` when the log was read."""

    path: str
    log_scanned: bool
    scanned: dict[str, int] = field(default_factory=dict)
    findings: list[SecretFinding] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "found" if self.findings else "clean"

    def counts(self) -> dict[str, int]:
        """Findings per location, every location present."""
        out = {name: 0 for name in (*TABLES, LOG)}
        for finding in self.findings:
            out[finding.location] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "log_scanned": self.log_scanned,
            "scanned": dict(self.scanned),
            "counts": self.counts(),
            "findings": [finding.to_dict() for finding in self.findings],
            "orphans": list(self.orphans),
            "not_scanned": list(NOT_SCANNED),
        }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _hits(text: str) -> dict[str, tuple[int, int]]:
    """Detector kind -> (offset of its first span, span count) in `text`.

    The spans are `credentials._merged_secret_spans`, the one place the
    gate's detectors, their masked-token guard and the generic rule's
    `_looks_like_secret` gate are applied together, so the scan fires on
    exactly what the gate would refuse. Overlapping spans are merged
    under the first kind there, so one secret two detectors both match
    (`api_key = AKIA...`) is one hit, not two."""
    out: dict[str, tuple[int, int]] = {}
    for start, _end, kind in _merged_secret_spans(text):
        first, count = out.get(kind, (start, 0))
        out[kind] = (first, count + 1)
    return out


def _segment(key: object) -> str:
    """A JSON key as a printable path segment, or `*` when it is not a
    plain identifier or a detector fires on it."""
    text = str(key)
    if _PLAIN_KEY_RE.fullmatch(text) and not _merged_secret_spans(text):
        return text
    return _MASKED_KEY


def _shown(value: str | None) -> str | None:
    """A record id or a log kind as a finding prints it: as it is when it
    is a plain token no detector fires on, `*` otherwise. The state and
    acknowledgement lookups use the value as stored."""
    if value is None:
        return None
    if _PLAIN_TOKEN_RE.fullmatch(value) and not _merged_secret_spans(value):
        return value
    return _MASKED_KEY


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string inside a parsed JSON value as (path, text), in
    document order. Iterative, so a deeply nested payload (a row written
    outside the store) cannot exhaust the interpreter's stack."""
    stack: list[tuple[str, Any]] = [(path, value)]
    while stack:
        where, item = stack.pop()
        if isinstance(item, str):
            yield where, item
        elif isinstance(item, Mapping):
            children = [
                (f"{where}.{_segment(k)}" if where else _segment(k), v)
                for k, v in item.items()
            ]
            stack.extend(reversed(children))
        elif isinstance(item, list):
            stack.extend(reversed([(f"{where}[{i}]", v) for i, v in enumerate(item)]))


def _row_strings(row: sqlite3.Row) -> Iterator[tuple[str, str]]:
    """The text a table row holds, as (field, text): each text column by
    its name, and each string inside a `*_json` column by its path under
    the column's name without the suffix (`claims_json` -> `claims[0]`).
    A JSON column that does not parse is scanned as text."""
    for column in row.keys():
        if column in _SKIPPED_COLUMNS:
            continue
        value = row[column]
        if not isinstance(value, str):
            continue
        if not column.endswith("_json"):
            yield column, value
            continue
        name = column[: -len("_json")]
        try:
            parsed = json.loads(value)
        except ValueError:
            yield name, value
            continue
        yield from _strings(parsed, name)


def _distinct(pairs: Iterator[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    """Each text once per row, under the first field that holds it.

    `memories` and `tombstones` mirror the origin's remote and worktree
    and the actor's client and model into columns of their own
    (`origin_repo` beside `origin_json`), so a token-bearing remote URL
    would otherwise be two findings in one row. Comparing the text rather
    than skipping the mirror columns keeps a mirror that disagrees with
    its JSON (a row written outside the store) in the scan."""
    seen: set[str] = set()
    for field_name, text in pairs:
        if text in seen:
            continue
        seen.add(text)
        yield field_name, text


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _acknowledgements(store: Store) -> dict[str, frozenset[str]]:
    """Record id -> the detector kinds its gated writes acknowledged.

    Only events that carry `credentials_acknowledged` count, so a record
    in this map had at least one write or update pass the gate; an empty
    set means none of them acknowledged anything. A record outside the
    map has no gated write on record."""
    acks: dict[str, set[str]] = {}
    for event in store.events_of_kinds(_ACK_EVENT_KINDS, since=_EPOCH):
        record_id = event.get("id")
        listed = event.get(_ACK_FIELD)
        if not isinstance(record_id, str) or not isinstance(listed, list):
            continue
        acks.setdefault(record_id, set()).update(
            kind for kind in listed if isinstance(kind, str)
        )
    return {record_id: frozenset(kinds) for record_id, kinds in acks.items()}


def _acknowledged(
    acks: Mapping[str, frozenset[str]], record_id: str | None, kind: str
) -> bool | None:
    if record_id is None or record_id not in acks:
        return None
    return kind in acks[record_id]


def _ids(store: Store, table: str) -> set[str]:
    return {str(row[0]) for row in store.conn.execute(f"SELECT id FROM {table}")}


def _log_record_id(kind: str, payload: Any) -> str | None:
    """The record a log row is about: the id inside a record put, the
    `id` of a record delete or a telemetry event; None otherwise."""
    if not isinstance(payload, Mapping):
        return None
    inner = _RECORD_PUTS.get(kind)
    if inner is not None:
        record = payload.get(inner)
        value = record.get("id") if isinstance(record, Mapping) else None
    else:
        value = payload.get("id")
    return value if isinstance(value, str) and value else None


def _scan_table(
    store: Store, table: str, acks: Mapping[str, frozenset[str]]
) -> tuple[int, list[SecretFinding]]:
    """One record table in rowid order (tombstones in removal order, as
    `Store.iter_tombstones` reads them): the rows read and the findings.
    Only memories and tombstones pass the gate, so only they carry an
    acknowledgement."""
    gated = table in ("memories", "tombstones")
    order = "removed, rowid" if table == "tombstones" else "rowid"
    rows = 0
    findings: list[SecretFinding] = []
    for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
        rows += 1
        record_id = str(row["id"])
        for field_name, text in _distinct(_row_strings(row)):
            for kind, (offset, count) in _hits(text).items():
                findings.append(
                    SecretFinding(
                        location=table,
                        record_id=_shown(record_id),
                        seq=None,
                        log_kind=None,
                        field=field_name,
                        kind=kind,
                        offset=offset,
                        occurrences=count,
                        record_state=_TABLE_STATE[table],
                        acknowledged=(
                            _acknowledged(acks, record_id, kind) if gated else None
                        ),
                    )
                )
    return rows, findings


def _log_state(
    kind: str, record_id: str | None, live: set[str], tombstoned: set[str]
) -> str | None:
    """The state of the record a log row names. A record put or delete
    whose record no table holds is `pruned`; any other row whose id is no
    current record names something that is not a record (None)."""
    if record_id is None:
        return None
    if record_id in live:
        return LIVE
    if record_id in tombstoned:
        return TOMBSTONED
    if kind in _RECORD_PUTS or kind in _RECORD_DELETES:
        return PRUNED
    return None


def _scan_log(
    store: Store, acks: Mapping[str, frozenset[str]]
) -> tuple[int, list[SecretFinding]]:
    """Every log row's payload in seq order, whatever its kind: the rows
    read and the findings. A payload that is not JSON is scanned as text
    under the field name ``payload``."""
    live = _ids(store, "memories") | _ids(store, "episodes")
    tombstoned = _ids(store, "tombstones")
    rows = 0
    findings: list[SecretFinding] = []
    for log_row in iter_rows(store.conn):
        rows += 1
        try:
            payload: Any = json.loads(log_row.payload)
        except ValueError:
            payload = log_row.payload
        record_id = _log_record_id(log_row.kind, payload)
        state = _log_state(log_row.kind, record_id, live, tombstoned)
        for field_name, text in _distinct(_strings(payload, "")):
            for kind, (offset, count) in _hits(text).items():
                findings.append(
                    SecretFinding(
                        location=LOG,
                        record_id=_shown(record_id),
                        seq=log_row.seq,
                        log_kind=_shown(log_row.kind),
                        field=field_name or "payload",
                        kind=kind,
                        offset=offset,
                        occurrences=count,
                        record_state=state,
                        acknowledged=_acknowledged(acks, record_id, kind),
                    )
                )
    return rows, findings


def scan_store(store: Store, *, include_log: bool = False) -> ScanReport:
    """Scan `store`'s record tables, and its log with `include_log`.

    Reads only: the table pass is plain SELECTs, the log pass walks
    `log.iter_rows`, and the acknowledgements come from
    `Store.events_of_kinds`. The findings are in table order, then log
    order."""
    report = ScanReport(
        path=str(store.path),
        log_scanned=include_log,
        orphans=[p.name for p in orphans_beside(store.path)],
    )
    acks = _acknowledgements(store)
    for table in TABLES:
        rows, findings = _scan_table(store, table, acks)
        report.scanned[table] = rows
        report.findings.extend(findings)
    if include_log:
        rows, findings = _scan_log(store, acks)
        report.scanned[LOG] = rows
        report.findings.extend(findings)
    return report


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _ack_text(acknowledged: bool | None) -> str:
    if acknowledged is None:
        return "no gated write on record"
    return "acknowledged at write" if acknowledged else "not acknowledged"


def _count(count: int, location: str) -> str:
    singular, plural = _NOUNS[location]
    return f"{count} {singular if count == 1 else plural}"


def _findings(count: int) -> str:
    return f"{count} finding" if count == 1 else f"{count} findings"


def _finding_line(finding: SecretFinding) -> str:
    hits = "" if finding.occurrences == 1 else f" ({finding.occurrences} hits)"
    where = f"{finding.field}: {finding.kind} at offset {finding.offset}{hits}"
    if finding.location != LOG:
        return f"{finding.record_id} {where}; {_ack_text(finding.acknowledged)}"
    head = f"seq {finding.seq} {finding.log_kind}"
    if finding.record_id is not None:
        head += f" {finding.record_id}"
    state = "" if finding.record_state is None else f"; record {finding.record_state}"
    return f"{head} {where}{state}; {_ack_text(finding.acknowledged)}"


def render_report(report: ScanReport) -> str:
    """The text form of a scan: one line per finding, grouped by where it
    sits, each group closed by what to do about it. Never a value."""
    total = len(report.findings)
    lines = [
        f"bettermemory security scan: {report.status}, {_findings(total)}",
        f"  store {report.path}",
    ]
    read = ", ".join(_count(report.scanned.get(t, 0), t) for t in TABLES)
    if report.log_scanned:
        read += f", {_count(report.scanned.get(LOG, 0), LOG)}"
    else:
        read += "; log not scanned (pass --log)"
    lines.append(f"  scanned {read}")
    for location in (*TABLES, LOG):
        group = [f for f in report.findings if f.location == location]
        if not group:
            continue
        lines.append("")
        lines.append(f"  {location}: {_findings(len(group))}")
        lines.extend(f"    {_finding_line(f)}" for f in group)
        lines.append(f"    {REMEDIATION[location]}")
    lines.append("")
    lines.append(f"  not scanned: {'; '.join(NOT_SCANNED)}")
    for name in report.orphans:
        lines.append(
            f"  saved orphan not scanned: {name} (copy it to memory.sqlite in a "
            "directory of its own and scan that with --store)"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SCAN_HELP = (
    "Find where a credential-shaped string persists in the store, using the "
    "write gate's own detectors: every text field and JSON string of the "
    "memories, tombstones, episodes and conflicts tables, and with --log "
    "every log row's payload (the log keeps every version of every record, "
    "after an edit, a removal and a prune). Each finding names the table or "
    "log seq, the record, the field, the detector kind, the record's state "
    "and whether the write was acknowledged; it never prints the value, "
    "only an offset into the field. Read-only: the store is opened without "
    "rekeying and nothing is written. Exit 0 when nothing is found, 1 when "
    "anything is, 2 on a usage error or when there is no store it can read."
)


def add_scan_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``scan`` on `subparsers` (the ``security`` group's).

    The destinations are prefixed so they cannot collide with an option
    the group itself declares."""
    parser = subparsers.add_parser("scan", help=_SCAN_HELP, description=_SCAN_HELP)
    parser.add_argument(
        "--log",
        dest="scan_log",
        action="store_true",
        help=(
            "Also scan the payload of every log row, whatever its kind: "
            "every version of every record, the membership rows and the "
            "telemetry events. Slower on a large log."
        ),
    )
    parser.add_argument(
        "--store",
        dest="scan_store",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "The store to scan instead of the configured one: a directory "
            "holding memory.sqlite, or that file itself, such as a restored "
            "backup or a copy from another machine (it needs no key). "
            "Default: memory.sqlite in the resolved store directory."
        ),
    )
    parser.add_argument(
        "--json",
        dest="scan_json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    return parser


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"bettermemory security scan: error: {message}\n")
    raise SystemExit(2)


def run_scan(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory security scan``. Always exits:
    0 clean, 1 with findings, 2 when there is no store it can read."""
    from .config import STORE_FILENAME, load_config

    store_arg = getattr(args, "scan_store", None)
    if store_arg is not None:
        path = store_path(store_arg)
    else:
        path = store_path(load_config().resolved_directory())
    if not path.is_file():
        named = None if store_arg is None else Path(store_arg).expanduser()
        if named is not None and named.is_file():
            # `store_path` reads any other file name as a directory, the
            # rule every store opener follows; say so rather than leave
            # "no store at backup.sqlite/memory.sqlite" to puzzle over.
            _fail(
                f"no bettermemory 9 store at {path}: --store names a store "
                f"directory or its {STORE_FILENAME} file, so copy {named.name} "
                f"to {STORE_FILENAME} in a directory of its own and scan that"
            )
        _fail(f"no bettermemory 9 store at {path}")
    # `allow_rekey=False`: a scan reads; it must not write a rekey row
    # into the log it is scanning, nor a key beside a copied store.
    try:
        store = Store.open(path, allow_rekey=False)
    except (ValueError, sqlite3.DatabaseError) as exc:
        _fail(f"cannot read the store at {path}: {exc}")
    try:
        report = scan_store(store, include_log=bool(getattr(args, "scan_log", False)))
    finally:
        store.close()
    if getattr(args, "scan_json", False):
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))
    raise SystemExit(1 if report.findings else 0)


__all__ = [
    "LIVE",
    "LOG",
    "NOT_SCANNED",
    "PRUNED",
    "REMEDIATION",
    "TABLES",
    "TOMBSTONED",
    "ScanReport",
    "SecretFinding",
    "add_scan_parser",
    "render_report",
    "run_scan",
    "scan_store",
]
