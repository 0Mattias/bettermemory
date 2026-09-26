"""`bettermemory migrate v8` and the mirror round trip (unit U3).

A synthetic v8 store is built with the v8 code itself: memories through
`Store.write`, `Store.update`, `Store.mark_verified` and the store's own
file writer, tombstones through `Store.tombstone` (one renamed to the
pre-2.6.4 name), episodes through `EpisodeStore`, events through the
`Recorder` across a rotated archive, a shard and the legacy file, and
every sidecar the migration reads, drops or leaves. The tests then pin
what the migration reports, what it writes, what it never touches, that
a second run imports nothing, and that the mirror of the migrated store
is byte-identical to the source.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.conflicts import CONFLICTS_FILENAME, ConflictCandidate
from bettermemory.episodes import EPISODES_DIR, EpisodeStore
from bettermemory.eval import compute_report, render_report_markdown
from bettermemory.events import EVENT_LOG_FILENAME, Recorder, iter_all_events
from bettermemory.identity import Actor
from bettermemory.ingest import INGEST_WATERMARK_FILENAME
from bettermemory.log import MIGRATE_V8, STORE_CREATED
from bettermemory.migrate_v8 import MigrationReport, inventory, migrate
from bettermemory.mirror import (
    active_filename_for_tombstone,
    tombstone_filename,
    write_mirror,
)
from bettermemory.models import (
    Category,
    Confidence,
    Episode,
    LinkType,
    Memory,
    MemoryLink,
    Source,
    generate_ulid,
)
from bettermemory.origin import Origin
from bettermemory.patterns import PATTERNS_FILENAME
from bettermemory.proposals import PROPOSALS_FILENAME
from bettermemory.quarantine import (
    REASON_UNPARSEABLE,
    QuarantineEntry,
    save_quarantine,
)
from bettermemory.session import PENDING_WRITES_FILENAME
from bettermemory.sqlite_store import IMPORTED, STORE_FILENAME, SqliteStore
from bettermemory.store import TOMBSTONE_DIR, Store

_NOW = datetime(2026, 9, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)
_HEAD = "b" * 40
_QUERY = "kubernetes networking secrets and the cluster"


@dataclass
class V8Fixture:
    root: Path
    full: Memory
    plain: Memory
    verified: Memory
    tombstoned: Memory
    legacy: Memory
    legacy_tombstone_path: Path
    episodes: list[Episode]
    event_kinds: Counter[str]


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture
def v8(tmp_path: Path) -> V8Fixture:
    root = tmp_path / "v8"
    worktree = tmp_path / "w"
    worktree.mkdir()
    store = Store(root)
    store.ensure()
    origin = Origin(
        cwd=str(worktree),
        repo="https://example.com/foo.git",
        branch="main",
        worktree_root=str(worktree),
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
        verified_head=_HEAD,
    )
    full = Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW + timedelta(minutes=1),
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
        last_verified_at=_NOW + timedelta(hours=1),
        category=Category.USER_INFERENCE,
        verified_paths=["src/a.py", "src/b.py"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_absent_paths=["tools/gone.py"],
        claims=["src/a.py::main", "!tools/gone.py"],
        verified_head=_HEAD,
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=plain.id, note="newer"),
            MemoryLink(type=LinkType.EXTENDS, target_id=verified.id),
        ],
        corroborations=2,
        last_corroborated=_NOW + timedelta(days=1),
    )
    store._write_path(store._path_for(full), full)

    tombstoned = Memory(
        id=generate_ulid(),
        created=_NOW - timedelta(days=3),
        updated=_NOW - timedelta(days=3),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="a record that was later removed\n",
        links=[MemoryLink(type=LinkType.EXTENDS, target_id=plain.id)],
        corroborations=1,
        last_corroborated=_NOW - timedelta(days=2),
    )
    store._write_path(store._path_for(tombstoned), tombstoned)
    store.tombstone(tombstoned.id, "no longer true", session_id="sess_x")
    legacy = store.write(content="a record removed long ago\n", scopes=["tools"])
    modern_path = store.tombstone(legacy.id, "legacy removal")
    legacy_path = modern_path.with_name(
        modern_path.name.replace(f".{legacy.id}.tombstone.md", ".tombstone.md")
    )
    modern_path.rename(legacy_path)

    episodes = EpisodeStore(root)
    written = [
        episodes.write(
            session_id="sess_a",
            body="tried the thing\n\nit worked",
            scopes=["projects:foo"],
            takeaway="it worked",
            origin=origin,
            now=_NOW,
        ),
        episodes.write(
            session_id="sess_b",
            body="the second session",
            swarm_id="sess_coordinator",
            now=_NOW + timedelta(seconds=1),
        ),
        episodes.write_floor(
            session_id="sess_b", origin=origin, now=_NOW + timedelta(seconds=2)
        ),
    ]

    recorder = Recorder(
        root=root, session_id="sess_a", log_queries_verbatim=True, max_bytes=400
    )
    recorder.record("search", query=_QUERY, returned=[plain.id], relevance=["high"])
    recorder.record("show", id=plain.id)
    recorder.record("use", ids=[plain.id], outcome="applied")
    recorder.record("verify", id=verified.id, note="checked")
    recorder.record("migrate", action="origin", ids=[verified.id], updated=1)
    recorder.record("write", id=full.id, status="committed")
    recorder.record(
        "turn_audited",
        verdict="ok",
        probe_query="what about the cluster",
        session_id="sess_a",
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
    (root / INGEST_WATERMARK_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    str(tmp_path / "src1.md"): {
                        "content_hash": "sha256:abc",
                        "memory_id": plain.id,
                    },
                    str(tmp_path / "src2.md"): {
                        "content_hash": "sha256:def",
                        "memory_id": generate_ulid(),
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
    save_quarantine(
        root,
        {
            "bad.md": QuarantineEntry(
                filename="bad.md",
                reason=REASON_UNPARSEABLE,
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

    kinds = Counter(ev["kind"] for ev in iter_all_events(root))
    return V8Fixture(
        root=root,
        full=full,
        plain=plain,
        verified=verified,
        tombstoned=tombstoned,
        legacy=legacy,
        legacy_tombstone_path=legacy_path,
        episodes=written,
        event_kinds=kinds,
    )


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "v9" / STORE_FILENAME


def _counts(report: MigrationReport) -> dict[str, dict[str, int]]:
    return {
        "memories": report.memories,
        "tombstones": report.tombstones,
        "episodes": report.episodes,
        "events": report.events,
        "conflicts": report.conflicts,
        "imports": report.imports,
    }


# ---------------------------------------------------------------------------
# The inventory and the dry run
# ---------------------------------------------------------------------------


def test_the_inventory_reads_every_source_with_the_v8_readers(v8: V8Fixture) -> None:
    inv = inventory(v8.root)
    assert {m.id for _, m in inv.memories} == {v8.full.id, v8.plain.id, v8.verified.id}
    assert inv.unparseable_memories == 0
    assert {t.dead.id for t in inv.tombstones} == {v8.tombstoned.id, v8.legacy.id}
    legacy = next(t for t in inv.tombstones if t.dead.id == v8.legacy.id)
    assert legacy.legacy_name is True
    assert legacy.filename == active_filename_for_tombstone(
        v8.legacy_tombstone_path.name
    )
    assert legacy.filename.endswith(".md") and ".tombstone" not in legacy.filename
    removed = next(t for t in inv.tombstones if t.dead.id == v8.tombstoned.id)
    assert removed.legacy_name is False
    assert [link["target_id"] for link in removed.links] == [v8.plain.id]
    assert removed.corroborations == 1
    assert removed.last_corroborated == _NOW - timedelta(days=2)
    assert removed.dead.removed_session == "sess_x"
    assert {e.id for e in inv.episodes} == {e.id for e in v8.episodes}
    assert inv.episode_sessions == 2
    assert [c["id"] for c in inv.conflicts] == ["pair1"]
    assert sorted(i["content_hash"] for i in inv.imports) == [
        "sha256:abc",
        "sha256:def",
    ]
    assert inv.dropped == {"pending_writes": 2, "proposals": 1}
    assert inv.left["quarantine"] == 1
    assert inv.left["episode_patterns"] == 1
    assert inv.left["captures"] == 1
    assert inv.left["index"] == 1
    assert inv.left["lock_files"] >= 1
    assert inv.unknown == [".stray.json"]


def test_a_dry_run_reports_the_counts_and_creates_nothing(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    before = _snapshot(v8.root)
    report = migrate(v8.root, store_path, keys_dir=keys_dir, dry_run=True)
    assert report.dry_run is True
    assert not store_path.exists()
    assert not keys_dir.exists()
    assert _snapshot(v8.root) == before
    assert report.memories == {
        "found": 3,
        "imported": 3,
        "present": 0,
        "unparseable": 0,
    }
    assert report.tombstones == {
        "found": 2,
        "imported": 2,
        "present": 0,
        "unparseable": 0,
        "legacy_names": 1,
    }
    assert report.episodes == {"sessions": 2, "found": 3, "imported": 3, "present": 0}
    total = sum(v8.event_kinds.values())
    assert report.events == {
        "found": total,
        "imported": total,
        "present": 0,
        "redacted": 2,
        "unstamped": 0,
        "malformed": 0,
    }
    assert report.event_kinds == dict(v8.event_kinds)
    assert report.conflicts == {"found": 1, "imported": 1, "present": 0}
    assert report.imports == {"found": 2, "imported": 2, "present": 0}
    assert report.dropped == {"pending_writes": 2, "proposals": 1}
    assert report.unknown == [".stray.json"]
    assert report.log_rows is None and report.size_bytes is None
    text = report.render_text()
    assert "dry run" in text and ".stray.json" in text
    assert json.loads(json.dumps(report.to_dict()))["events"]["found"] == total


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def test_the_migration_imports_everything_and_reports_it(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    stamp = _NOW + timedelta(days=2)
    report = migrate(v8.root, store_path, keys_dir=keys_dir, now=stamp)
    assert report.dry_run is False
    assert report.store == str(store_path)
    assert report.memories["imported"] == 3 and report.tombstones["imported"] == 2
    assert report.episodes["imported"] == 3 and report.conflicts["imported"] == 1
    assert report.imports["imported"] == 2
    total = sum(v8.event_kinds.values())
    assert report.events["imported"] == total and report.events["redacted"] == 2
    assert report.log_rows == 1 + 3 + 2 + 3 + 1 + 2 + total
    assert report.size_bytes is not None and report.size_bytes > 0
    assert report.seconds >= 0

    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert store.count_memories() == 3
        assert store.count_tombstones() == 2
        assert store.count_episodes() == 3
        ids = [v8.full.id, v8.plain.id, v8.verified.id]
        assert store.provenance_for(ids) == {i: IMPORTED for i in ids}
        assert store.memory_ids() == [m.id for _, m in Store(v8.root).iter_active()]
        assert store.filename_for(v8.full.id) == Store(v8.root)._path_for(v8.full).name
        assert store.get_memory(v8.full.id) == v8.full
        assert store.get_memory(v8.plain.id) == v8.plain
        assert store.get_memory(v8.verified.id) == v8.verified

        dead = store.get_tombstone(v8.tombstoned.id)
        assert dead.removed_session == "sess_x"
        assert dead.removed_reason == "no longer true"
        row = store.conn.execute(
            "SELECT filename, provenance, links_json, corroborations, "
            "last_corroborated FROM tombstones WHERE id = ?",
            (v8.tombstoned.id,),
        ).fetchone()
        assert row["provenance"] == IMPORTED
        assert row["filename"] == Store(v8.root)._path_for(v8.tombstoned).name
        assert [link["target_id"] for link in json.loads(row["links_json"])] == [
            v8.plain.id
        ]
        assert row["corroborations"] == 1
        assert row["last_corroborated"] == (_NOW - timedelta(days=2)).isoformat()
        legacy_row = store.conn.execute(
            "SELECT filename FROM tombstones WHERE id = ?", (v8.legacy.id,)
        ).fetchone()
        assert legacy_row["filename"] == active_filename_for_tombstone(
            v8.legacy_tombstone_path.name
        )

        floor = next(e for e in v8.episodes if e.is_floor)
        assert store.get_episode(floor.id).is_floor is True
        assert {e.id for e in store.iter_episodes()} == {e.id for e in v8.episodes}
        # The store carries what the v8 reader hands back, which is the
        # file's body without the trailing newline the writer added.
        as_read = EpisodeStore(v8.root).list_by_session("sess_a")[0]
        assert store.get_episode(v8.episodes[0].id) == as_read

        assert Counter(ev["kind"] for ev in store.iter_events()) == v8.event_kinds
        assert [c["id"] for c in store.list_conflicts()] == ["pair1"]
        imports = {row["content_hash"]: row for row in store.list_imports()}
        assert imports["sha256:abc"]["memory_id"] == v8.plain.id
        assert imports["sha256:abc"]["imported_at"] == v8.plain.created.isoformat()
        assert imports["sha256:def"]["imported_at"] == stamp.isoformat()

        rows = store.log_rows()
        assert rows[0].kind == STORE_CREATED
        assert rows[1].kind == MIGRATE_V8
        control = json.loads(rows[1].payload)
        assert control["imported_from"] == "v8"
        assert control["source"] == str(v8.root)
        assert control["counts"] == {
            "memories": 3,
            "tombstones": 2,
            "episodes": 3,
            "conflicts": 1,
            "imports": 2,
            "events": total,
        }
        assert store.log_verify()["status"] == "ok"


def test_the_migration_never_writes_to_the_v8_directory(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    before = _snapshot(v8.root)
    migrate(v8.root, store_path, keys_dir=keys_dir)
    assert _snapshot(v8.root) == before


def test_a_second_run_imports_nothing_and_appends_no_row(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    first = migrate(v8.root, store_path, keys_dir=keys_dir)
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        rows_before = len(store.log_rows())
    second = migrate(v8.root, store_path, keys_dir=keys_dir)
    for name, counts in _counts(second).items():
        assert counts["imported"] == 0, name
        assert counts["present"] == _counts(first)[name]["imported"], name
    assert second.log_rows == 0
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert len(store.log_rows()) == rows_before
        assert store.log_verify()["status"] == "ok"


def test_a_record_removed_or_restored_in_the_new_store_is_left_alone(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    with SqliteStore.open(store_path, keys_dir=keys_dir) as store:
        store.tombstone(v8.plain.id, "removed after the migration")
        store.restore(v8.tombstoned.id)
    report = migrate(v8.root, store_path, keys_dir=keys_dir)
    assert report.memories == {
        "found": 3,
        "imported": 0,
        "present": 3,
        "unparseable": 0,
    }
    assert report.tombstones["imported"] == 0 and report.tombstones["present"] == 2
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert store.has_memory(v8.tombstoned.id)
        assert not store.has_memory(v8.plain.id)
        assert store.get_tombstone(v8.plain.id).removed_reason == (
            "removed after the migration"
        )
        assert store.log_verify()["status"] == "ok"


def test_new_v8_events_are_imported_by_a_later_run(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    Recorder(root=v8.root, session_id="sess_c").record("show", id=v8.plain.id)
    report = migrate(v8.root, store_path, keys_dir=keys_dir)
    total = sum(v8.event_kinds.values())
    assert report.events["found"] == total + 1
    assert report.events["imported"] == 1 and report.events["present"] == total
    assert report.log_rows == 2


def test_verbatim_query_text_is_redacted_on_import(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    report = migrate(v8.root, store_path, keys_dir=keys_dir)
    assert report.events["redacted"] == 2
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        search = next(ev for ev in store.iter_events() if ev["kind"] == "search")
        assert set(search["query"]) == {"hash", "preview", "len"}
        assert search["query"]["preview"] == _QUERY[:32]
        assert search["query"]["len"] == len(_QUERY)
        assert search["imported_from"] == "v8"
        audited = next(ev for ev in store.iter_events() if ev["kind"] == "turn_audited")
        assert audited["probe_query"]["preview"] == "what about the cluster"
        for row in store.log_rows():
            assert _QUERY not in row.payload


def test_the_v8_migrate_event_is_telemetry_and_the_control_row_is_migrate_v8(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        rows = store.log_rows()
        control = [r for r in rows if r.kind == MIGRATE_V8]
        telemetry = [r for r in rows if r.kind == "migrate"]
        assert len(control) == 1 and control[0].seq == 2
        assert len(telemetry) == 1
        payload = json.loads(telemetry[0].payload)
        assert payload["action"] == "origin" and payload["imported_from"] == "v8"
        assert telemetry[0].session == "sess_a"
        assert telemetry[0].ts.endswith("Z")
        legacy = next(r for r in rows if r.kind == "list")
        assert legacy.ts == "2026-01-01T00:00:00.000000Z"
        assert legacy.session == "sess_legacy"
        assert json.loads(legacy.payload) == {"scopes": None, "imported_from": "v8"}
        assert store.refold()["status"] == "ok"


# ---------------------------------------------------------------------------
# The mirror round trip and the eval fold
# ---------------------------------------------------------------------------


def test_the_mirror_of_the_migrated_store_is_byte_identical_to_the_source(
    v8: V8Fixture, store_path: Path, keys_dir: Path, tmp_path: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    target = tmp_path / "mirror"
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        report = write_mirror(store, target)
    assert report.active == 3 and report.tombstones == 2 and report.episodes == 3
    assert report.written == 8 and report.unchanged == 0 and report.removed == 0

    active = [p for p in v8.root.iterdir() if p.is_file() and p.suffix == ".md"]
    assert len(active) == 3
    for source in active:
        assert (target / source.name).read_bytes() == source.read_bytes(), source.name
    for source in (v8.root / TOMBSTONE_DIR).iterdir():
        if source.suffix != ".md":
            continue
        memory_id = (
            v8.legacy.id if source == v8.legacy_tombstone_path else (v8.tombstoned.id)
        )
        expected = tombstone_filename(
            active_filename_for_tombstone(source.name), memory_id
        )
        mirrored = target / TOMBSTONE_DIR / expected
        assert mirrored.read_bytes() == source.read_bytes(), source.name
    assert not (target / TOMBSTONE_DIR / v8.legacy_tombstone_path.name).exists()
    for session_dir in (v8.root / EPISODES_DIR).iterdir():
        if not session_dir.is_dir():
            continue
        for source in session_dir.iterdir():
            if source.suffix != ".md":
                continue
            mirrored = target / EPISODES_DIR / session_dir.name / source.name
            assert mirrored.read_bytes() == source.read_bytes(), source.name
    assert sorted(p.name for p in target.iterdir() if p.suffix == ".md") == sorted(
        p.name for p in active
    )


def test_the_eval_report_reads_the_same_on_both_sides(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    now = _NOW + timedelta(days=2)
    v8_store = Store(v8.root)
    before = compute_report(
        memories=v8_store.load_all(),
        events=iter_all_events(v8.root),
        now=now,
        since=timedelta(days=30),
        tombstoned_ids={t.id for t in v8_store.load_tombstones()},
        version="test",
    )
    with SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        after = compute_report(
            memories=sorted(
                store.iter_memories(), key=lambda m: m.created, reverse=True
            ),
            events=store.iter_events(),
            now=now,
            since=timedelta(days=30),
            tombstoned_ids={t.id for t in store.iter_tombstones()},
            version="test",
        )
    assert before.alltime_eval.to_dict() == after.alltime_eval.to_dict()
    assert before.window_eval.to_dict() == after.window_eval.to_dict()
    assert before.total_events == after.total_events == sum(v8.event_kinds.values())
    assert render_report_markdown(before) == render_report_markdown(after)


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_migrate_v8_command_dry_run_then_run_then_json(
    v8: V8Fixture,
    store_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("bettermemory.log.default_keys_dir", lambda: keys_dir)
    argv = ["migrate", "v8", "--from", str(v8.root), "--to", str(store_path)]
    assert _run_cli([*argv, "--dry-run"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "memories" in out
    assert not store_path.exists()

    assert _run_cli(argv, monkeypatch) == 0
    out = capsys.readouterr().out
    assert store_path.is_file() and str(store_path) in out

    assert _run_cli([*argv, "--json"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["memories"]["present"] == 3 and report["memories"]["imported"] == 0
    assert report["log_rows"] == 0


def test_migrate_v8_command_defaults_to_the_resolved_directory(
    v8: V8Fixture,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(v8.root))
    monkeypatch.setattr("bettermemory.log.default_keys_dir", lambda: keys_dir)
    assert _run_cli(["migrate", "v8"], monkeypatch) == 0
    capsys.readouterr()
    assert (v8.root / STORE_FILENAME).is_file()
    with SqliteStore.open(
        v8.root / STORE_FILENAME, keys_dir=keys_dir, allow_rekey=False
    ) as store:
        assert store.count_memories() == 3


def test_migrate_v8_command_refuses_a_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere"
    code = _run_cli(["migrate", "v8", "--from", str(missing)], monkeypatch)
    assert code == 2
    assert str(missing) in capsys.readouterr().err


def test_export_mirror_command_writes_the_tree_and_refuses_the_json_flags(
    v8: V8Fixture,
    store_path: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    monkeypatch.setattr("bettermemory.log.default_keys_dir", lambda: keys_dir)
    target = tmp_path / "mirror"
    code = _run_cli(
        ["export", "--mirror", str(target), "--store", str(store_path)], monkeypatch
    )
    assert code == 0
    out = capsys.readouterr().out
    assert str(target) in out and "3 active" in out
    assert len([p for p in target.iterdir() if p.suffix == ".md"]) == 3
    assert (target / TOMBSTONE_DIR).is_dir() and (target / EPISODES_DIR).is_dir()

    code = _run_cli(
        ["export", "--mirror", str(target), "--store", str(store_path), "-o", "x"],
        monkeypatch,
    )
    assert code == 2
    assert "--mirror" in capsys.readouterr().err

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "empty"))
    code = _run_cli(["export", "--mirror", str(tmp_path / "other")], monkeypatch)
    assert code == 2
    assert STORE_FILENAME in capsys.readouterr().err


def test_export_mirror_command_finds_the_store_in_the_resolved_directory(
    v8: V8Fixture,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(v8.root, v8.root / STORE_FILENAME, keys_dir=keys_dir)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(v8.root))
    monkeypatch.setattr("bettermemory.log.default_keys_dir", lambda: keys_dir)
    target = tmp_path / "mirror"
    assert _run_cli(["export", "--mirror", str(target)], monkeypatch) == 0
    capsys.readouterr()
    for source in v8.root.iterdir():
        if source.is_file() and source.suffix == ".md":
            assert (target / source.name).read_bytes() == source.read_bytes()
