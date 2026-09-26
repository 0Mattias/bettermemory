"""Migrate a v8 store directory into a bettermemory 9 store.

The v8 store was a directory: markdown files for the active records,
``.tombstones/`` for the removed ones, ``episodes/<session>/`` for the
journal, JSONL event segments and archives for telemetry, and a handful
of sidecars. The bettermemory 9 store is one SQLite file whose tables are
a fold of its hash-chained log (``bettermemory.store``). This
module reads the directory with the frozen v8 readers
(``bettermemory.v8``: the file formats, kept when the v8 runtime was
retired) and writes what it finds into the store, in one batch.

What lands where. Active memories become ``memories`` rows with
provenance ``imported`` and their v8 filename, inserted in
``v8.iter_active`` order, the order a v8 rebuild indexed them in, so
rowids and the candidate query's tie order agree with the index.
Tombstones become ``tombstones`` rows with the links and corroboration
rollup the file kept, and with the active filename their name was made
from. Episodes, conflicts and the ingest watermark take their tables.
Every event, from every shard, archive and the legacy file, becomes a
telemetry row under its original ``ts`` and ``session``, its payload the
event's fields plus ``imported_from: "v8"``, with any verbatim query text
redacted the way every v9 row is. The run opens with one ``migrate_v8``
control row naming the directory and the counts it is about to import.

What does not. Pending writes and write proposals are transient by design
and are dropped, counted. The quarantine sidecar, the episode-pattern
dismissals, the captures directory, the derived index and the lock files
have no place in the phase 1 store and are left where they are, counted.
Anything else in the directory is named as unknown and left alone.

The directory is never written: the readers are pure, and the store file
lands where the caller says, by default beside the v8 files as
``memory.sqlite``. A run is idempotent: records already in the store, by
id, are reported as present and skipped, and so is an event whose exact
row is already there; a re-run after the directory grew imports only the
new rows.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .log import canonical_payload
from .models import Episode, Memory, TombstonedMemory
from .store import (
    IMPORTED,
    IMPORTED_FROM_V8,
    STORE_FILENAME,
    Store,
    event_import_payload,
)
from .time_utils import isoformat_utc
from .v8 import (
    ARCHIVE_PREFIX,
    CONFLICTS_FILENAME,
    EPISODES_DIR,
    EVENT_LOG_FILENAME,
    INDEX_FILENAME,
    INGEST_WATERMARK_FILENAME,
    PARSE_SKIP_EXCEPTIONS,
    PATTERNS_FILENAME,
    PENDING_WRITES_FILENAME,
    PROPOSALS_FILENAME,
    QUARANTINE_FILENAME,
    SEGMENT_TEMPLATE,
    SHARD_COUNT,
    TOMBSTONE_DIR,
    active_filename_for_tombstone,
    is_legacy_tombstone_name,
    iter_active,
    iter_active_memory_paths,
    iter_all_events,
    iter_session_ids,
    iter_tombstone_paths,
    list_by_session,
    load_conflicts,
    load_quarantine,
    load_watermark_sources,
    parse_memory_file,
    parse_tombstone_file,
)

CAPTURES_DIR = "captures"
_REDACTED_FIELDS = ("query", "probe_query")


# ---------------------------------------------------------------------------
# Inventory: what the directory holds, read with the v8 readers
# ---------------------------------------------------------------------------


@dataclass
class TombstoneSource:
    """One tombstone file: the record, what the file kept beside the
    tombstone model's fields, and the active filename its name encodes."""

    path: Path
    dead: TombstonedMemory
    links: list[dict[str, Any]]
    corroborations: int
    last_corroborated: datetime | None
    filename: str
    legacy_name: bool


@dataclass
class Inventory:
    root: Path
    memories: list[tuple[Path, Memory]]
    unparseable_memories: int
    tombstones: list[TombstoneSource]
    unparseable_tombstones: int
    episodes: list[Episode]
    episode_sessions: int
    conflicts: list[dict[str, Any]]
    imports: list[dict[str, Any]]
    dropped: dict[str, int]
    left: dict[str, int]
    unknown: list[str]


def _count_json_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            count += 1
    return count


def _is_event_file(name: str) -> bool:
    """The event log's own files, named from the recorder's fragments: the
    legacy file, the per-shard segments, and every rotated archive or
    holding file under the archive prefix."""
    if name == EVENT_LOG_FILENAME or name.startswith(ARCHIVE_PREFIX):
        return True
    return any(name == SEGMENT_TEMPLATE.format(shard) for shard in range(SHARD_COUNT))


def _count_entries(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir()) if path.is_dir() else 0
    except OSError:
        return 0


def inventory(root: Path | str) -> Inventory:
    """Read a v8 directory. Raises FileNotFoundError when it is not a
    directory; reads nothing else and writes nothing."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"no v8 store directory at {root}")

    active_paths = list(iter_active_memory_paths(root))
    memories = list(iter_active(root))
    unparseable_memories = len(active_paths) - len(memories)

    tombstones: list[TombstoneSource] = []
    unparseable_tombstones = 0
    for path in sorted(iter_tombstone_paths(root)):
        try:
            dead = parse_tombstone_file(path)
            # The tombstone model drops the links and the corroboration
            # rollup; the memory reader keeps them and ignores the
            # removal keys, so the same file read twice gives both.
            kept = parse_memory_file(path)
        except PARSE_SKIP_EXCEPTIONS:
            unparseable_tombstones += 1
            continue
        tombstones.append(
            TombstoneSource(
                path=path,
                dead=dead,
                links=[
                    link.model_dump(mode="json", exclude_none=True)
                    for link in kept.links
                ],
                corroborations=kept.corroborations,
                last_corroborated=kept.last_corroborated,
                filename=active_filename_for_tombstone(path.name),
                legacy_name=is_legacy_tombstone_name(path.name),
            )
        )

    sessions = sorted(iter_session_ids(root))
    episodes: list[Episode] = []
    for session_id in sessions:
        episodes.extend(list_by_session(root, session_id))

    conflicts = [candidate.to_dict() for candidate in load_conflicts(root)]

    imports: list[dict[str, Any]] = []
    for source, entry in sorted(load_watermark_sources(root).items()):
        content_hash = entry.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            continue
        memory_id = entry.get("memory_id")
        imports.append(
            {
                "source": source,
                "content_hash": content_hash,
                "memory_id": memory_id if isinstance(memory_id, str) else None,
            }
        )

    dropped = {
        "pending_writes": _count_json_lines(root / PENDING_WRITES_FILENAME),
        "proposals": _count_json_lines(root / PROPOSALS_FILENAME),
    }
    lock_files = 0
    unknown: list[str] = []
    known_sidecars = {
        PENDING_WRITES_FILENAME,
        PROPOSALS_FILENAME,
        PATTERNS_FILENAME,
        QUARANTINE_FILENAME,
        INGEST_WATERMARK_FILENAME,
        CONFLICTS_FILENAME,
    }
    for child in sorted(root.iterdir()):
        name = child.name
        if name.endswith(".lock"):
            lock_files += 1
        elif name in known_sidecars or _is_event_file(name):
            continue
        elif name.startswith(INDEX_FILENAME) or name.startswith(STORE_FILENAME):
            continue
        elif name in (TOMBSTONE_DIR, EPISODES_DIR, CAPTURES_DIR):
            continue
        elif child.is_file() and not child.is_symlink() and name.endswith(".md"):
            continue
        else:
            unknown.append(name)
    tombstone_dir = root / TOMBSTONE_DIR
    if tombstone_dir.is_dir():
        for child in sorted(tombstone_dir.iterdir()):
            if child.name.endswith(".lock"):
                lock_files += 1
            elif not (child.is_file() and child.suffix == ".md"):
                unknown.append(f"{TOMBSTONE_DIR}/{child.name}")
    left = {
        "quarantine": len(load_quarantine(root)),
        "episode_patterns": _count_json_lines(root / PATTERNS_FILENAME),
        "captures": _count_entries(root / CAPTURES_DIR),
        "index": 1 if (root / INDEX_FILENAME).is_file() else 0,
        "lock_files": lock_files,
    }
    return Inventory(
        root=root,
        memories=memories,
        unparseable_memories=unparseable_memories,
        tombstones=tombstones,
        unparseable_tombstones=unparseable_tombstones,
        episodes=episodes,
        episode_sessions=len(sessions),
        conflicts=conflicts,
        imports=imports,
        dropped=dropped,
        left=left,
        unknown=unknown,
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class MigrationReport:
    source: str
    store: str | None
    dry_run: bool
    memories: dict[str, int] = field(default_factory=dict)
    tombstones: dict[str, int] = field(default_factory=dict)
    episodes: dict[str, int] = field(default_factory=dict)
    events: dict[str, int] = field(default_factory=dict)
    event_kinds: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, int] = field(default_factory=dict)
    imports: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    left: dict[str, int] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log_rows: int | None = None
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "store": self.store,
            "dry_run": self.dry_run,
            "memories": dict(self.memories),
            "tombstones": dict(self.tombstones),
            "episodes": dict(self.episodes),
            "events": dict(self.events),
            "event_kinds": dict(self.event_kinds),
            "conflicts": dict(self.conflicts),
            "imports": dict(self.imports),
            "dropped": dict(self.dropped),
            "left": dict(self.left),
            "unknown": list(self.unknown),
            "seconds": round(self.seconds, 3),
            "log_rows": self.log_rows,
            "size_bytes": self.size_bytes,
        }

    def render_text(self) -> str:
        verb = "would import" if self.dry_run else "imported"
        lines = [
            f"{'Dry run of the' if self.dry_run else 'The'} migration of {self.source}"
            + ("" if self.store is None else f" into {self.store}")
        ]

        def row(label: str, counts: dict[str, int], *extra: str) -> str:
            parts = [
                f"{counts.get('found', 0)} found",
                f"{counts.get('imported', 0)} {verb}",
                f"{counts.get('present', 0)} already present",
                *extra,
            ]
            return f"  {label:<11} {', '.join(parts)}"

        lines.append(
            row(
                "memories",
                self.memories,
                f"{self.memories.get('unparseable', 0)} unparseable",
            )
        )
        lines.append(
            row(
                "tombstones",
                self.tombstones,
                f"{self.tombstones.get('unparseable', 0)} unparseable",
                f"{self.tombstones.get('legacy_names', 0)} with the pre-2.6.4 name",
            )
        )
        lines.append(
            row(
                "episodes",
                self.episodes,
                f"{self.episodes.get('sessions', 0)} sessions",
            )
        )
        lines.append(
            row(
                "events",
                self.events,
                f"{self.events.get('redacted', 0)} with query text redacted",
                f"{self.events.get('unstamped', 0)} without a timestamp",
                f"{self.events.get('malformed', 0)} malformed",
            )
        )
        lines.append(row("conflicts", self.conflicts))
        lines.append(row("imports", self.imports))
        lines.append(
            "  dropped     "
            + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in self.dropped.items())
        )
        lines.append(
            "  left        "
            + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in self.left.items())
        )
        if self.unknown:
            lines.append("  unknown     " + ", ".join(self.unknown))
        if self.dry_run:
            lines.append("Nothing was written (dry run).")
        else:
            lines.append(
                f"{self.log_rows} log rows appended; the store is "
                f"{self.size_bytes} bytes; {self.seconds:.2f} s."
            )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


@dataclass
class _Present:
    """What the target store already holds, by key."""

    records: set[str] = field(default_factory=set)
    episodes: set[str] = field(default_factory=set)
    conflicts: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    events: set[tuple[str, str | None, str, str]] = field(default_factory=set)

    @classmethod
    def read(cls, store: Store | None) -> _Present:
        if store is None:
            return cls()
        conn = store.conn
        records = {str(r["id"]) for r in conn.execute("SELECT id FROM memories")}
        records |= {str(r["id"]) for r in conn.execute("SELECT id FROM tombstones")}
        return cls(
            records=records,
            episodes={str(r["id"]) for r in conn.execute("SELECT id FROM episodes")},
            conflicts={c["id"] for c in store.list_conflicts()},
            imports={i["source"] for i in store.list_imports()},
            events=store.imported_event_keys(IMPORTED_FROM_V8),
        )


def _has_query_text(event: dict[str, Any]) -> bool:
    return any(isinstance(event.get(name), str) for name in _REDACTED_FIELDS)


def migrate(
    root: Path | str,
    store_path: Path | str,
    *,
    keys_dir: Path | str | None = None,
    dry_run: bool = False,
    session: str | None = None,
    now: datetime | None = None,
) -> MigrationReport:
    """Migrate the v8 directory at ``root`` into the store at
    ``store_path`` (created when absent, opened when present). With
    ``dry_run`` nothing is created or written and the report says what a
    run would import. ``now`` stamps rows that carry no time of their
    own (an import whose memory is not in the store, an event without a
    ``ts``)."""
    started = time.perf_counter()
    inv = inventory(root)
    store_path = Path(store_path).expanduser()
    instant = now or datetime.now(timezone.utc)
    # Two spellings, each the one its table already uses: the log's `ts`
    # column ends in Z like every other row's, the imports table holds
    # what `datetime.isoformat` gives, like the created it usually copies.
    stamp = isoformat_utc(instant)
    imported_now = instant.isoformat()
    report = MigrationReport(
        source=str(inv.root),
        store=None if dry_run and not store_path.is_file() else str(store_path),
        dry_run=dry_run,
        dropped=dict(inv.dropped),
        left=dict(inv.left),
        unknown=list(inv.unknown),
    )

    store: Store | None
    if dry_run:
        store = (
            Store.open(store_path, keys_dir=keys_dir, allow_rekey=False)
            if store_path.is_file()
            else None
        )
    else:
        store = Store.open_or_create(store_path, keys_dir=keys_dir)
    try:
        present = _Present.read(store)

        new_memories = [
            (path, memory)
            for path, memory in inv.memories
            if memory.id not in present.records
        ]
        new_tombstones = [
            source for source in inv.tombstones if source.dead.id not in present.records
        ]
        new_episodes = [e for e in inv.episodes if e.id not in present.episodes]
        new_conflicts = [c for c in inv.conflicts if c["id"] not in present.conflicts]
        new_imports = [i for i in inv.imports if i["source"] not in present.imports]

        kinds: Counter[str] = Counter()
        new_events: list[dict[str, Any]] = []
        found = present_events = redacted = unstamped = malformed = 0
        for event in iter_all_events(inv.root):
            found += 1
            kinds[str(event.get("kind"))] += 1
            try:
                ts, session_id, kind, payload = event_import_payload(
                    event, imported_from=IMPORTED_FROM_V8
                )
            except ValueError:
                malformed += 1
                continue
            if ts is None:
                unstamped += 1
                ts = stamp
                event = {**event, "ts": ts}
            key = (ts, session_id, kind, canonical_payload(payload))
            if key in present.events:
                present_events += 1
                continue
            if _has_query_text(event):
                redacted += 1
            new_events.append(event)

        report.memories = {
            "found": len(inv.memories),
            "imported": len(new_memories),
            "present": len(inv.memories) - len(new_memories),
            "unparseable": inv.unparseable_memories,
        }
        report.tombstones = {
            "found": len(inv.tombstones),
            "imported": len(new_tombstones),
            "present": len(inv.tombstones) - len(new_tombstones),
            "unparseable": inv.unparseable_tombstones,
            "legacy_names": sum(1 for t in inv.tombstones if t.legacy_name),
        }
        report.episodes = {
            "sessions": inv.episode_sessions,
            "found": len(inv.episodes),
            "imported": len(new_episodes),
            "present": len(inv.episodes) - len(new_episodes),
        }
        report.events = {
            "found": found,
            "imported": len(new_events),
            "present": present_events,
            "redacted": redacted,
            "unstamped": unstamped,
            "malformed": malformed,
        }
        report.event_kinds = dict(sorted(kinds.items()))
        report.conflicts = {
            "found": len(inv.conflicts),
            "imported": len(new_conflicts),
            "present": len(inv.conflicts) - len(new_conflicts),
        }
        report.imports = {
            "found": len(inv.imports),
            "imported": len(new_imports),
            "present": len(inv.imports) - len(new_imports),
        }
        if dry_run or store is None:
            report.seconds = time.perf_counter() - started
            return report

        counts = {
            "memories": len(new_memories),
            "tombstones": len(new_tombstones),
            "episodes": len(new_episodes),
            "conflicts": len(new_conflicts),
            "imports": len(new_imports),
            "events": len(new_events),
        }
        rows_before = int(store.conn.execute("SELECT COUNT(*) FROM log").fetchone()[0])
        nothing_new = not any(counts.values())
        if not nothing_new:
            with store.batch():
                store.record_migration(
                    source=str(inv.root), counts=counts, session=session
                )
                for path, memory in new_memories:
                    store.put_memory(
                        memory,
                        provenance=IMPORTED,
                        filename=path.name,
                        session=session,
                    )
                for source in new_tombstones:
                    store.put_tombstone(
                        source.dead,
                        provenance=IMPORTED,
                        filename=source.filename,
                        links=source.links,
                        corroborations=source.corroborations,
                        last_corroborated=source.last_corroborated,
                        session=session,
                    )
                for episode in new_episodes:
                    store.put_episode(episode, session=session)
                for conflict in new_conflicts:
                    store.put_conflict(conflict, session=session)
                for entry in new_imports:
                    memory_id = entry["memory_id"]
                    imported_at = imported_now
                    if memory_id is not None and store.has_memory(memory_id):
                        imported_at = store.get_memory(memory_id).created.isoformat()
                    elif memory_id is not None and memory_id in present.records:
                        imported_at = store.get_tombstone(memory_id).created.isoformat()
                    store.put_import(
                        entry["source"],
                        content_hash=entry["content_hash"],
                        imported_at=imported_at,
                        memory_id=memory_id,
                        session=session,
                    )
                for event in new_events:
                    store.import_event(event, imported_from=IMPORTED_FROM_V8)
        rows_after = int(store.conn.execute("SELECT COUNT(*) FROM log").fetchone()[0])
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report.log_rows = rows_after - rows_before
        report.size_bytes = int(store.path.stat().st_size)
        report.seconds = time.perf_counter() - started
        return report
    finally:
        if store is not None:
            store.close()


__all__ = [
    "CAPTURES_DIR",
    "Inventory",
    "MigrationReport",
    "TombstoneSource",
    "inventory",
    "migrate",
]
