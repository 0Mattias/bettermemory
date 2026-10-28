"""`bettermemory stats`: a cheap, read-only usage and growth report.

What a store holds and how fast it grows, in one command: file sizes,
rows per table, memories by project, by scope, by the client and model
that wrote them and by category, records created and removed and log
rows written per day over a window, the tombstones, the journal, the log
by kind, and the oldest and newest records. `Store.status()`, which no
other command shows, is the report's ``store`` section.

Why not `health`: the health report goes through `cli_context`, which
creates, migrates and rekeys a store, and it computes commit and
cross-repo drift through git, so it is slow and opinionated. This report
opens the store read-only (``Store.open(..., allow_rekey=False)``, as
`log verify` does), never runs git, and is aggregate SQL only: no row's
MAC is checked and no body is read beyond the journal's byte count, so
its cost grows with the number of rows a ``GROUP BY`` walks, not with an
HMAC per row. Provenance is therefore not here; `log verify` and the
health report's census answer that question at that price.

The window: ``--since DAYS`` is the last DAYS UTC days, today included.
A day is the first ten characters of a stored timestamp, which every
writer stores in UTC (``isoformat_utc`` and the store's ``_iso``), so the
grouping needs no parsing. The log's per-day counts read the ``log_by_ts``
index for the window's range; ``created`` and ``removed`` read the record
tables (``tombstones_by_removed`` for the removals).

JSON: ``{"format": "bettermemory-stats", "version": 1, ...}``, every
section the text shows, and a zero-filled ``activity.days`` series with
one entry per day of the window, oldest first.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store

FORMAT = "bettermemory-stats"
FORMAT_VERSION = 1

DEFAULT_DAYS = 30
# Ten years of days: a window past it is a typo, and the JSON series has
# one entry per day.
MAX_DAYS = 3660
# How many scopes the report lists, most used first.
TOP_SCOPES = 20


def _days(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number of days: {text!r}")
    if not 1 <= value <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"DAYS must be 1 to {MAX_DAYS}, got {value}")
    return value


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``stats`` subparser."""
    help_text = (
        "Usage and growth of the store, read-only: file sizes, rows per table, "
        "memories by project, scope, client, model and category, records "
        "created and removed and log rows written per day over the last DAYS "
        "days, tombstones, episodes, log rows by kind, and the oldest and "
        "newest records. Aggregate SQL only: no MAC is checked (`log verify` "
        "does that). Exit 0 with the report, 2 when no store is found."
    )
    parser = sub.add_parser("stats", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON (format bettermemory-stats, version 1).",
    )
    parser.add_argument(
        "--since",
        type=_days,
        default=DEFAULT_DAYS,
        metavar="DAYS",
        help=(
            f"The activity window: the last DAYS days in UTC, today included. "
            f"Default {DEFAULT_DAYS}."
        ),
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory stats``."""
    from ..config import load_config
    from ..store import STORE_FILENAME, Store

    path = load_config().resolved_directory() / STORE_FILENAME
    if not path.is_file():
        sub_parser.error(f"no bettermemory 9 store at {path}")
    # `allow_rekey=False`: a report must not write a rekey row. Without
    # the key every count is still read; only `key_present` says so.
    store = Store.open(path, allow_rekey=False)
    try:
        report = collect(store, days=args.since)
    finally:
        store.close()
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render(report))
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Collecting
# ---------------------------------------------------------------------------


def _size(path: Path) -> int:
    with contextlib.suppress(OSError):
        return path.stat().st_size
    return 0


def _pairs(conn: sqlite3.Connection, column: str, name: str) -> list[dict[str, Any]]:
    """Active memories per value of `column` (null included), most first,
    as ``[{name: value, "memories": count}]``."""
    return [
        {name: row[0], "memories": int(row[1])}
        for row in conn.execute(
            f"SELECT {column}, COUNT(*) AS n FROM memories "
            f"GROUP BY {column} ORDER BY n DESC, {column}"
        )
    ]


def _first(conn: sqlite3.Connection, sql: str) -> dict[str, Any] | None:
    """The ``(id, timestamp)`` row `sql` selects first, or None."""
    row = conn.execute(sql).fetchone()
    return None if row is None else {"id": str(row[0]), "at": str(row[1])}


def _canonical(projects: dict[str, str | None], project: str) -> str:
    """The project `project` stands for now: its merges followed, as
    `Membership.canonical` follows them, a cycle stopping where it
    closes."""
    seen: set[str] = set()
    while project in projects and project not in seen:
        seen.add(project)
        merged_into = projects[project]
        if merged_into is None:
            return project
        project = merged_into
    return project


def _by_project(
    conn: sqlite3.Connection, tables: set[str], memories: int, episodes: int
) -> list[dict[str, Any]]:
    """Live memories and episodes per project, merged projects counted
    under the project they merged into, then ``global`` and the records
    no ``homes`` row homes (``project`` null). A read-only store an
    earlier build created has no membership tables: every record reads
    unhomed there."""
    counts: dict[str | None, dict[str, int]] = {}
    names: dict[str, str] = {}
    merges: dict[str, str | None] = {}
    if "projects" in tables:
        for row in conn.execute("SELECT id, name, merged_into FROM projects"):
            merges[str(row[0])] = None if row[2] is None else str(row[2])
            names[str(row[0])] = str(row[1])
    if "homes" in tables:
        for table, column in (("memories", "memories"), ("episodes", "episodes")):
            for row in conn.execute(
                f"SELECT h.project, COUNT(*) FROM {table} r "
                "JOIN homes h ON h.record_id = r.id GROUP BY h.project"
            ):
                project = _canonical(merges, str(row[0]))
                slot = counts.setdefault(project, {"memories": 0, "episodes": 0})
                slot[column] += int(row[1])
    homed_memories = sum(slot["memories"] for slot in counts.values())
    homed_episodes = sum(slot["episodes"] for slot in counts.values())
    unhomed = {
        "memories": memories - homed_memories,
        "episodes": episodes - homed_episodes,
    }
    if unhomed["memories"] or unhomed["episodes"]:
        counts[None] = unhomed
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            item[0] is None,
            -(item[1]["memories"] + item[1]["episodes"]),
            item[0] or "",
        ),
    )
    return [
        {
            "project": project,
            "name": None if project is None else names.get(project),
            "memories": slot["memories"],
            "episodes": slot["episodes"],
        }
        for project, slot in ordered
    ]


def _scope_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many active memories carry each scope, read from the scope
    column alone. Not `Store.scope_counts`: that one joins ``homes``,
    which a read-only store an earlier build created does not have."""
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT scopes_json FROM memories"):
        try:
            scopes = json.loads(str(row[0]))
        except ValueError:
            continue
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if isinstance(scope, str):
                counts[scope] = counts.get(scope, 0) + 1
    return counts


def _log_class(kind: str) -> str:
    """Which of the log's four classes a kind is: a table ``mutation``, a
    ``control`` row, a ``membership`` row of design 8.10, or
    ``telemetry`` (every other kind, as `Store.iter_events` reads them)."""
    from ..log import CONTROL_KINDS
    from ..store import MEMBERSHIP_EVENT_KINDS, MUTATION_KINDS

    if kind in MUTATION_KINDS:
        return "mutation"
    if kind in CONTROL_KINDS:
        return "control"
    if kind in MEMBERSHIP_EVENT_KINDS:
        return "membership"
    return "telemetry"


def collect(store: Store, *, days: int, now: datetime | None = None) -> dict[str, Any]:
    """The report for `store` over the last `days` UTC days up to `now`."""
    from ..store import FOLDED_TABLES, MEMBERSHIP_TABLES
    from ..time_utils import isoformat_utc

    conn = store.conn
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    status = store.status()
    path = Path(store.path)
    files = {
        "db": _size(path),
        "wal": _size(path.with_name(path.name + "-wal")),
        "shm": _size(path.with_name(path.name + "-shm")),
    }
    files["total"] = sum(files.values())

    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    tables = {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in (*FOLDED_TABLES, *MEMBERSHIP_TABLES, "log")
        if name in present
    }

    memories = int(status["memories"])
    episodes = int(status["episodes"])
    scope_counts = _scope_counts(conn)
    top_scopes = sorted(scope_counts.items(), key=lambda item: (-item[1], item[0]))
    record_sections = {
        "total": memories,
        "by_project": _by_project(conn, present, memories, episodes),
        "scopes_distinct": len(scope_counts),
        "by_scope": [
            {"scope": scope, "memories": count}
            for scope, count in top_scopes[:TOP_SCOPES]
        ],
        "by_client": _pairs(conn, "actor_client", "client"),
        "by_model": _pairs(conn, "actor_model", "model"),
        "by_category": _pairs(conn, "category", "category"),
        "oldest": _first(
            conn, "SELECT id, created FROM memories ORDER BY created, id LIMIT 1"
        ),
        "newest": _first(
            conn,
            "SELECT id, created FROM memories ORDER BY created DESC, id DESC LIMIT 1",
        ),
        "last_updated": _first(
            conn,
            "SELECT id, updated FROM memories ORDER BY updated DESC, id DESC LIMIT 1",
        ),
    }

    tombstones = {
        "total": int(status["tombstones"]),
        "oldest_removed": _first(
            conn, "SELECT id, removed FROM tombstones ORDER BY removed, id LIMIT 1"
        ),
        "newest_removed": _first(
            conn,
            "SELECT id, removed FROM tombstones ORDER BY removed DESC, id DESC LIMIT 1",
        ),
    }
    journal: dict[str, Any] = dict(store.episode_volume().to_dict())
    journal["oldest"] = _first(
        conn, "SELECT id, created FROM episodes ORDER BY created, id LIMIT 1"
    )
    journal["newest"] = _first(
        conn, "SELECT id, created FROM episodes ORDER BY created DESC, id DESC LIMIT 1"
    )

    by_kind: list[dict[str, Any]] = []
    by_class: dict[str, int] = {}
    for row in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM log GROUP BY kind ORDER BY n DESC, kind"
    ):
        kind, count = str(row[0]), int(row[1])
        kind_class = _log_class(kind)
        by_kind.append({"kind": kind, "class": kind_class, "rows": count})
        by_class[kind_class] = by_class.get(kind_class, 0) + count
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM log").fetchone()
    log_section = {
        "rows": int(status["log_rows"]),
        "first_ts": span[0],
        "last_ts": span[1],
        "by_class": dict(sorted(by_class.items())),
        "by_kind": by_kind,
    }

    activity = _activity(conn, start=start, end=end)
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "generated_at": isoformat_utc(moment.astimezone(timezone.utc)),
        "store": status,
        "files": files,
        "tables": tables,
        "memories": record_sections,
        "tombstones": tombstones,
        "episodes": journal,
        "log": log_section,
        "activity": {
            "window": {
                "days": days,
                "from": start.isoformat(),
                "to": today.isoformat(),
            },
            **activity,
        },
    }


# The per-day counts, in the order the text table prints them.
_DAY_COLUMNS = ("created", "removed", "memory_puts", "episode_puts", "log_rows")


def _activity(conn: sqlite3.Connection, *, start: date, end: date) -> dict[str, Any]:
    """Per day of [start, end): records created (active and tombstoned,
    by ``created``), memories removed (tombstones by ``removed``), and log
    rows written: ``memory_put`` rows (every write, edit, verification and
    corroboration), ``episode_put`` rows, and every row."""
    low, high = start.isoformat(), end.isoformat()
    series: dict[str, dict[str, int]] = {}
    day = start
    while day < end:
        series[day.isoformat()] = dict.fromkeys(_DAY_COLUMNS, 0)
        day += timedelta(days=1)

    def add(column: str, sql: str) -> None:
        for row in conn.execute(sql, (low, high)):
            slot = series.get(str(row[0]))
            if slot is not None:
                slot[column] += int(row[1])

    for table in ("memories", "tombstones"):
        add(
            "created",
            f"SELECT substr(created, 1, 10) AS day, COUNT(*) FROM {table} "
            "WHERE created >= ? AND created < ? GROUP BY day",
        )
    add(
        "removed",
        "SELECT substr(removed, 1, 10) AS day, COUNT(*) FROM tombstones "
        "WHERE removed >= ? AND removed < ? GROUP BY day",
    )
    for row in conn.execute(
        "SELECT substr(ts, 1, 10) AS day, kind, COUNT(*) FROM log "
        "WHERE ts >= ? AND ts < ? GROUP BY day, kind",
        (low, high),
    ):
        slot = series.get(str(row[0]))
        if slot is None:
            continue
        count = int(row[2])
        slot["log_rows"] += count
        if row[1] == "memory_put":
            slot["memory_puts"] += count
        elif row[1] == "episode_put":
            slot["episode_puts"] += count
    sessions = int(
        conn.execute(
            "SELECT COUNT(DISTINCT session) FROM log "
            "WHERE ts >= ? AND ts < ? AND session IS NOT NULL",
            (low, high),
        ).fetchone()[0]
    )
    totals = dict.fromkeys(_DAY_COLUMNS, 0)
    for slot in series.values():
        for column in _DAY_COLUMNS:
            totals[column] += slot[column]
    return {
        "sessions": sessions,
        "totals": totals,
        "days": [{"day": key, **slot} for key, slot in series.items()],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bytes(count: int) -> str:
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{count} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} bytes"  # pragma: no cover - the loop returns


def _plural(count: int, word: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {plural or word + 's'}"


def _memories(count: int) -> str:
    return _plural(count, "memory", "memories")


def _label(value: Any, missing: str) -> str:
    """A value read from the store, or `missing` in its place. `render`
    makes every line printable."""
    return missing if value is None else str(value)


def _at(entry: dict[str, Any] | None) -> str:
    return "none" if entry is None else f"{entry['id']} at {entry['at']}"


def _wrapped(items: Iterable[str], width: int = 88) -> list[str]:
    """`items` joined with commas into indented lines of at most `width`
    characters, never splitting an item (a table and its count stay on
    one line)."""
    lines: list[str] = []
    line = ""
    for item in items:
        candidate = f"{line}, {item}" if line else item
        if line and len(candidate) + 4 > width:
            lines.append(f"    {line},")
            line = item
        else:
            line = candidate
    if line:
        lines.append(f"    {line}")
    return lines


def render(report: dict[str, Any]) -> str:
    """The text form of a `collect` report. Every line goes through
    `history.printable`: scope, project and client names, ids, stamps and
    the engine version in ``meta`` are all whatever an editor of the file
    put there, and a raw escape sequence must not reach the terminal."""
    from ..history import printable

    store = report["store"]
    files = report["files"]
    memories = report["memories"]
    journal = report["episodes"]
    head = store["head_seq"]
    lines = [
        f"bettermemory stats: {store['path']}",
        f"  store {store['store_id']}, schema {store['schema_version']}, "
        f"engine {store['engine_version']}, "
        f"key {'present' if store['key_present'] else 'not held here'}, "
        f"head seq {'none' if head is None else head}",
        f"  files {_bytes(files['total'])} (db {_bytes(files['db'])}, "
        f"wal {_bytes(files['wal'])}, shm {_bytes(files['shm'])})",
        f"  records {_memories(memories['total'])}, "
        f"{_plural(report['tombstones']['total'], 'tombstone')}, "
        f"{_plural(journal['episodes'], 'episode')} in "
        f"{_plural(journal['sessions'], 'session')} ({_bytes(journal['bytes'])}; "
        f"{journal['prunable_sessions']} past the {journal['ttl_days']}-day ttl)",
        f"  memories: oldest {_at(memories['oldest'])}, newest "
        f"{_at(memories['newest'])}, last updated {_at(memories['last_updated'])}",
        f"  tombstones: oldest removal {_at(report['tombstones']['oldest_removed'])}, "
        f"newest {_at(report['tombstones']['newest_removed'])}",
        f"  episodes: oldest {_at(journal['oldest'])}, newest {_at(journal['newest'])}",
        "  tables",
        *_wrapped(f"{name} {count}" for name, count in report["tables"].items()),
    ]
    if memories["by_project"]:
        lines.append("  by project")
        for entry in memories["by_project"]:
            project = entry["project"]
            if project is None:
                name = "(unhomed)"
            elif entry["name"] is None:
                name = _label(project, "")
            else:
                name = f"{_label(project, '')} {_label(entry['name'], '')}"
            lines.append(
                f"    {name}: {_memories(entry['memories'])}, "
                f"{_plural(entry['episodes'], 'episode')}"
            )
    sections = (
        (
            f"top scopes ({memories['scopes_distinct']} distinct)",
            "by_scope",
            "scope",
            "",
        ),
        ("by client", "by_client", "client", "(undeclared)"),
        ("by model", "by_model", "model", "(undeclared)"),
        ("by category", "by_category", "category", "(none)"),
    )
    for title, key, column, missing in sections:
        if not memories[key]:
            continue
        lines.append(f"  {title}")
        for entry in memories[key]:
            lines.append(f"    {_label(entry[column], missing)} {entry['memories']}")
    log = report["log"]
    classes = ", ".join(f"{count} {name}" for name, count in log["by_class"].items())
    lines.append(f"  log {_plural(log['rows'], 'row')}: {classes or 'none'}")
    if log["first_ts"] is not None:
        lines.append(f"    first {log['first_ts']}, last {log['last_ts']}")
    for entry in log["by_kind"]:
        lines.append(
            f"    {_label(entry['kind'], '')} {entry['rows']} ({entry['class']})"
        )
    activity = report["activity"]
    window = activity["window"]
    totals = activity["totals"]
    lines.append(
        f"  last {_plural(window['days'], 'day')}, {window['from']} to "
        f"{window['to']}: {_plural(activity['sessions'], 'session')}, "
        f"{totals['created']} created, {totals['removed']} removed, "
        f"{totals['log_rows']} log rows"
    )
    busy = [day for day in activity["days"] if any(day[c] for c in _DAY_COLUMNS)]
    if busy:
        lines.append(
            "    day         created  removed  memory_puts  episode_puts  log_rows"
        )
        for day in busy:
            lines.append(
                f"    {day['day']}  {day['created']:>7}  {day['removed']:>7}  "
                f"{day['memory_puts']:>11}  {day['episode_puts']:>12}  "
                f"{day['log_rows']:>8}"
            )
    return "\n".join(printable(line) for line in lines) + "\n"


__all__ = [
    "DEFAULT_DAYS",
    "FORMAT",
    "FORMAT_VERSION",
    "add_subparser",
    "collect",
    "render",
    "run",
]
