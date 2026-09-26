"""The generator of the golden v8 fixture under ``tests/fixtures/v8``.

Run once at 8.0.0, with the v8 modules (``bettermemory.store``,
``bettermemory.episodes``, ``bettermemory.events``, ``bettermemory.index``,
``bettermemory.quarantine``) still in the tree, and kept for the record.
Those modules are retired in bettermemory 9, so this script no longer
runs; what it produced is committed under ``store/`` and described by
``manifest.json``, and the tests read those files instead of building a
directory with writers that no longer exist.

What it wrote, with the v8 code itself: three active memories (one
written through ``Store.write`` and then ``Store.update``, one with an
origin and a category then stamped by ``Store.mark_verified``, one
hand-built with every optional field and written through the store's own
file writer), two tombstones through ``Store.tombstone`` (one with a
session, one renamed to the pre-2.6.4 ``<stem>.tombstone.md`` name),
three episodes in two sessions (one a floor) through ``EpisodeStore``,
events through the ``Recorder`` with a small rotation threshold so they
span a rotated archive and an active shard (one with verbatim query text,
one of the v8 ``migrate`` kind, one with a ``probe_query``), a legacy
pre-sharding ``.events.jsonl`` line, a conflict, an ingest watermark with
two sources, pending writes, a proposal, a pattern dismissal, a quarantine
entry, a stray file, a ``.md.lock`` file, a captures directory and an
``.index.sqlite`` built by ``index.rebuild`` at schema 11.

Every stamp and id is deterministic: the clock is pinned and ticks one
second per read, the ids are minted from the clock and a counter, the
gzip header's compression time is pinned too, and the script builds the
directory twice and refuses to install a build that differs from the
other. The only file not held to that bar is the index, whose bytes
SQLite lays out as it likes; it is pinned as it came out.

Usage, at 8.0.0 only::

    /path/to/venv/bin/python tests/fixtures/v8/generate.py
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory.conflicts import ConflictCandidate  # noqa: E402
from bettermemory.identity import Actor  # noqa: E402
from bettermemory.models import (  # noqa: E402
    Category,
    Confidence,
    LinkType,
    Memory,
    MemoryLink,
    Source,
)
from bettermemory.origin import Origin  # noqa: E402
from bettermemory.v8 import (  # noqa: E402
    CONFLICTS_FILENAME,
    EVENT_LOG_FILENAME,
    INGEST_WATERMARK_FILENAME,
    PATTERNS_FILENAME,
    PENDING_WRITES_FILENAME,
    PROPOSALS_FILENAME,
    active_filename_for_tombstone,
    iter_all_events,
)

STORE_DIR = _HERE / "store"
MANIFEST = _HERE / "manifest.json"

NOW = datetime(2026, 9, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)
HEAD = "b" * 40
QUERY = "kubernetes networking secrets and the cluster"
PROBE_QUERY = "what about the cluster"
WORKTREE = "/w/foo"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _v8_module(name: str) -> ModuleType:
    """A v8 runtime module, imported by name so this file stays a record
    that type-checks after the modules are gone."""
    try:
        return importlib.import_module(f"bettermemory.{name}")
    except ImportError as exc:
        raise SystemExit(
            f"bettermemory.{name} is not in this tree: the generator ran once at "
            f"8.0.0 and the fixture it produced is committed ({exc})"
        ) from None


class _Clock:
    """A pinned clock that ticks one second per read, so every stamp the
    writers take is distinct and the archives never share a second."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.minted = 0

    def tick(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def ulid(self, at: datetime | None = None) -> str:
        """A ULID whose time part is the clock and whose random part is a
        digest of a counter."""
        self.minted += 1
        stamp = at or self.now
        ts_ms = int(stamp.timestamp() * 1000) & ((1 << 48) - 1)
        seed = f"bettermemory-v8-fixture-{self.minted}".encode()
        rand = int.from_bytes(hashlib.sha256(seed).digest()[:10], "big")
        return _encode_crockford(ts_ms, 10) + _encode_crockford(rand, 16)


def _encode_crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _write_lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def build(root: Path) -> dict[str, Any]:
    """Build the fixture directory at `root` and return its manifest."""
    clock = _Clock(NOW)
    store_mod = _v8_module("store")
    episodes_mod = _v8_module("episodes")
    events_mod = _v8_module("events")

    class _PinnedDatetime(datetime):
        """`datetime.now` reads the pinned clock; the Recorder stamps
        events and names archives through it."""

        @classmethod
        def now(cls, tz: Any = None) -> _PinnedDatetime:
            t = clock.tick()
            return cls(
                t.year,
                t.month,
                t.day,
                t.hour,
                t.minute,
                t.second,
                t.microsecond,
                t.tzinfo,
            )

    setattr(store_mod, "utcnow", clock.tick)  # noqa: B010
    setattr(store_mod, "generate_ulid", clock.ulid)  # noqa: B010
    setattr(episodes_mod, "generate_ulid", clock.ulid)  # noqa: B010
    setattr(events_mod, "datetime", _PinnedDatetime)  # noqa: B010
    # A gzip header carries the compression time, read from the gzip
    # module's own `time` binding; pin it so the rotated archive's bytes
    # are the same on every run.
    real_gzip_time = getattr(gzip, "time")  # noqa: B009
    setattr(gzip, "time", _PinnedTime())  # noqa: B010
    try:
        return _build(root, clock, store_mod, episodes_mod, events_mod)
    finally:
        setattr(gzip, "time", real_gzip_time)  # noqa: B010


class _PinnedTime:
    """The one call gzip makes on its `time` binding, answered with the
    fixture's pinned instant."""

    @staticmethod
    def time() -> float:
        return NOW.timestamp()


def _build(
    root: Path,
    clock: _Clock,
    store_mod: ModuleType,
    episodes_mod: ModuleType,
    events_mod: ModuleType,
) -> dict[str, Any]:
    index_mod = _v8_module("index")
    quarantine_mod = _v8_module("quarantine")
    store = store_mod.Store(root)
    store.ensure()
    origin = Origin(
        cwd=WORKTREE,
        repo="https://example.com/foo.git",
        branch="main",
        worktree_root=WORKTREE,
        source="roots",
    )

    plain = store.write(
        content="a plain memory about kubernetes networking\n", scopes=["tools"]
    )
    plain = store.update(
        plain.model_copy(
            update={"body": "a plain memory about kubernetes networking, edited\n"}
        )
    )
    verified = store.write(
        content="the release process runs from the tag\n",
        scopes=["projects:foo"],
        origin=origin,
        category=Category.FACT,
    )
    verified = store.mark_verified(
        verified.id,
        verified_paths=["docs/release.md"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_head=HEAD,
    )
    full_created = clock.tick()
    full = Memory(
        id=clock.ulid(full_created),
        created=full_created,
        updated=full_created + timedelta(minutes=1),
        scopes=["projects:foo", "tools"],
        confidence=Confidence.HIGH,
        source=Source.INFERRED,
        body="the whole record, every field set\n",
        origin=origin,
        actor=Actor(
            client="claude-code",
            client_version="2.1.281",
            model="claude-fable-5-1",
            sources={"client": "client-info", "model": "header"},
        ),
        last_verified_at=full_created + timedelta(hours=1),
        category=Category.USER_INFERENCE,
        verified_paths=["src/a.py", "src/b.py"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_absent_paths=["tools/gone.py"],
        claims=["src/a.py::main", "!tools/gone.py"],
        verified_head=HEAD,
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=plain.id, note="newer"),
            MemoryLink(type=LinkType.EXTENDS, target_id=verified.id),
        ],
        corroborations=2,
        last_corroborated=full_created + timedelta(days=1),
    )
    full_path = store._path_for(full)
    store._write_path(full_path, full)

    tombstoned_created = NOW - timedelta(days=3)
    tombstoned = Memory(
        id=clock.ulid(tombstoned_created),
        created=tombstoned_created,
        updated=tombstoned_created,
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="a record that was later removed\n",
        links=[MemoryLink(type=LinkType.EXTENDS, target_id=plain.id)],
        corroborations=1,
        last_corroborated=tombstoned_created + timedelta(days=1),
    )
    tombstoned_active_path = store._path_for(tombstoned)
    store._write_path(tombstoned_active_path, tombstoned)
    tombstoned_path = store.tombstone(
        tombstoned.id, "no longer true", session_id="sess_x"
    )
    legacy = store.write(content="a record removed long ago\n", scopes=["tools"])
    modern_path = store.tombstone(legacy.id, "legacy removal")
    legacy_path = modern_path.with_name(
        modern_path.name.replace(f".{legacy.id}.tombstone.md", ".tombstone.md")
    )
    modern_path.rename(legacy_path)

    episodes = episodes_mod.EpisodeStore(root)
    written = [
        episodes.write(
            session_id="sess_a",
            body="tried the thing\n\nit worked",
            scopes=["projects:foo"],
            takeaway="it worked",
            origin=origin,
            now=clock.tick(),
        ),
        episodes.write(
            session_id="sess_b",
            body="the second session",
            swarm_id="sess_coordinator",
            now=clock.tick(),
        ),
        episodes.write_floor(session_id="sess_b", origin=origin, now=clock.tick()),
    ]

    recorder = events_mod.Recorder(
        root=root, session_id="sess_a", log_queries_verbatim=True, max_bytes=400
    )
    recorder.record("search", query=QUERY, returned=[plain.id], relevance=["high"])
    recorder.record("show", id=plain.id)
    recorder.record("use", ids=[plain.id], outcome="applied")
    recorder.record("verify", id=verified.id, note="checked")
    recorder.record("migrate", action="origin", ids=[verified.id], updated=1)
    recorder.record("write", id=full.id, status="committed")
    recorder.record(
        "turn_audited", verdict="ok", probe_query=PROBE_QUERY, session_id="sess_a"
    )
    _write_lines(
        root / EVENT_LOG_FILENAME,
        [
            {
                "ts": "2026-01-01T00:00:00.000000Z",
                "session": "sess_legacy",
                "kind": "list",
                "scopes": None,
            }
        ],
    )

    _write_lines(
        root / CONFLICTS_FILENAME,
        [
            ConflictCandidate(
                id="pair1",
                a_id=plain.id,
                b_id=verified.id,
                summary_a="a",
                summary_b="b",
                similarity=0.9,
                method="jaccard",
                detector="polarity",
                created="2026-09-01T00:00:00Z",
            ).to_dict()
        ],
    )
    unknown_memory_id = clock.ulid()
    (root / INGEST_WATERMARK_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "/w/notes/src1.md": {
                        "content_hash": "sha256:abc",
                        "memory_id": plain.id,
                    },
                    "/w/notes/src2.md": {
                        "content_hash": "sha256:def",
                        "memory_id": unknown_memory_id,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _write_lines(
        root / PENDING_WRITES_FILENAME,
        [{"pending_id": "p1", "client": "c"}, {"pending_id": "p2", "client": "c"}],
    )
    _write_lines(root / PROPOSALS_FILENAME, [{"id": "prop1", "body": "x"}])
    _write_lines(root / PATTERNS_FILENAME, [{"key": "k", "dismissed": True}])
    quarantine_mod.save_quarantine(
        root,
        {
            "bad.md": quarantine_mod.QuarantineEntry(
                filename="bad.md",
                reason=quarantine_mod.REASON_UNPARSEABLE,
                detail="x",
                remote="origin",
                pulled_at="2026-01-01T00:00:00Z",
                size=3,
                sha256=None,
            )
        },
    )
    (root / ".stray.json").write_text("{}", encoding="utf-8")
    (root / "left-behind.md.lock").write_text("", encoding="utf-8")
    captures = root / "captures" / "sess_a"
    captures.mkdir(parents=True)
    (captures / "segment-1.md").write_text("captured\n", encoding="utf-8")

    index_mod.rebuild(root, store.iter_active())
    index_path = index_mod.index_path(root)
    conn = sqlite3.connect(str(index_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        schema = int(
            conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    kinds = Counter(str(ev["kind"]) for ev in iter_all_events(root))
    return {
        "bettermemory_version": "8.0.0",
        "now": NOW.isoformat(),
        "query": QUERY,
        "probe_query": PROBE_QUERY,
        "memories": {
            "plain": {"id": plain.id, "filename": store._path_for(plain).name},
            "verified": {
                "id": verified.id,
                "filename": store._path_for(verified).name,
            },
            "full": {"id": full.id, "filename": full_path.name},
        },
        "tombstones": {
            "tombstoned": {
                "id": tombstoned.id,
                "filename": tombstoned_path.name,
                "active_filename": tombstoned_active_path.name,
                "session": "sess_x",
                "reason": "no longer true",
                "legacy_name": False,
            },
            "legacy": {
                "id": legacy.id,
                "filename": legacy_path.name,
                "active_filename": active_filename_for_tombstone(legacy_path.name),
                "session": None,
                "reason": "legacy removal",
                "legacy_name": True,
            },
        },
        "episodes": [
            {"id": e.id, "session_id": e.session_id, "is_floor": e.is_floor}
            for e in written
        ],
        "event_kinds": dict(sorted(kinds.items())),
        "events_total": sum(kinds.values()),
        "events_redacted": 2,
        "conflicts": ["pair1"],
        "imports": {
            "/w/notes/src1.md": {"content_hash": "sha256:abc", "memory_id": plain.id},
            "/w/notes/src2.md": {
                "content_hash": "sha256:def",
                "memory_id": unknown_memory_id,
            },
        },
        "dropped": {"pending_writes": 2, "proposals": 1},
        "left": {"quarantine": 1, "episode_patterns": 1, "captures": 1, "index": 1},
        "unknown": [".stray.json"],
        "index_schema_version": schema,
    }


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        first = build(scratch / "a")
        second = build(scratch / "b")
        files_a = _files(scratch / "a")
        files_b = _files(scratch / "b")
        differing = sorted(
            name
            for name in set(files_a) | set(files_b)
            if files_a.get(name) != files_b.get(name)
        )
        unstable = [name for name in differing if not name.startswith(".index.")]
        if unstable or first != second:
            sys.stderr.write(
                "the two builds differ; refusing to install: "
                + ", ".join(unstable or ["manifest"])
                + "\n"
            )
            return 1
        if STORE_DIR.exists():
            shutil.rmtree(STORE_DIR)
        shutil.copytree(scratch / "a", STORE_DIR, symlinks=True)
    MANIFEST.write_text(
        json.dumps(first, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sys.stdout.write(
        f"wrote {len(files_a)} files to {STORE_DIR} and {MANIFEST.name}; "
        f"{first['events_total']} events, index schema "
        f"{first['index_schema_version']}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
