"""`bettermemory migrate v8` and the mirror round trip (unit U3).

The v8 directory the tests read is the golden fixture under
`tests/fixtures/v8/store`, written once at 8.0.0 by the v8 code itself
(`tests/fixtures/v8/generate.py`): memories through `Store.write`,
`Store.update`, `Store.mark_verified` and the store's own file writer,
tombstones through `Store.tombstone` (one renamed to the pre-2.6.4 name),
episodes through `EpisodeStore`, events through the `Recorder` across a
rotated archive, a shard and the legacy file, and every sidecar the
migration reads, drops or leaves. `manifest.json` beside it names the ids
and counts the generator recorded. The tests copy the fixture to a temp
directory, then pin what the migration reports, what it writes, what it
never touches, that a second run imports nothing, and that the mirror of
the migrated store is byte-identical to the fixture.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory.eval import compute_report, render_report_markdown
from bettermemory.log import MIGRATE_V8, STORE_CREATED
from bettermemory.migrate_v8 import MigrationReport, inventory, migrate
from bettermemory.mirror import (
    active_filename_for_tombstone,
    tombstone_filename,
    write_mirror,
)
from bettermemory.models import Episode, Memory
from bettermemory.store import IMPORTED, STORE_FILENAME, Store
from bettermemory.v8 import (
    EPISODES_DIR,
    EVENT_LOG_FILENAME,
    INDEX_FILENAME,
    TOMBSTONE_DIR,
    iter_active,
    iter_all_events,
    iter_session_ids,
    iter_tombstones,
    list_by_session,
    memory_filename,
    parse_memory_file,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "v8"
MANIFEST: dict[str, Any] = json.loads(
    (FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
)
_NOW = datetime.fromisoformat(MANIFEST["now"])
_QUERY: str = MANIFEST["query"]


@dataclass
class V8Fixture:
    root: Path
    full: Memory
    plain: Memory
    verified: Memory
    tombstoned_id: str
    tombstoned_path: Path
    legacy_id: str
    legacy_tombstone_path: Path
    episodes: list[Episode]
    event_kinds: Counter[str]


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _append_event(root: Path, event: dict[str, Any]) -> None:
    """One more line in the legacy event file, written by hand the way
    the v8 recorder serialised a line."""
    with (root / EVENT_LOG_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


@pytest.fixture
def v8(tmp_path: Path) -> V8Fixture:
    root = tmp_path / "v8"
    shutil.copytree(FIXTURE_DIR / "store", root, symlinks=True)
    memories = MANIFEST["memories"]
    tombstones = MANIFEST["tombstones"]
    episodes = [
        episode
        for session_id in sorted(iter_session_ids(root))
        for episode in list_by_session(root, session_id)
    ]
    return V8Fixture(
        root=root,
        full=parse_memory_file(root / memories["full"]["filename"]),
        plain=parse_memory_file(root / memories["plain"]["filename"]),
        verified=parse_memory_file(root / memories["verified"]["filename"]),
        tombstoned_id=tombstones["tombstoned"]["id"],
        tombstoned_path=root / TOMBSTONE_DIR / tombstones["tombstoned"]["filename"],
        legacy_id=tombstones["legacy"]["id"],
        legacy_tombstone_path=root / TOMBSTONE_DIR / tombstones["legacy"]["filename"],
        episodes=episodes,
        event_kinds=Counter(ev["kind"] for ev in iter_all_events(root)),
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
# The fixture
# ---------------------------------------------------------------------------


def test_the_golden_fixture_covers_what_the_migration_reads(v8: V8Fixture) -> None:
    """The fixture is the subject every test below reads, so its shape is
    pinned once: the cases the v8 writers were asked to produce are there,
    and the manifest describes the files on disk."""
    assert {m.id for _, m in iter_active(v8.root)} == {
        v8.full.id,
        v8.plain.id,
        v8.verified.id,
    }
    # An update: the body edit moved `updated` past `created`.
    assert v8.plain.updated > v8.plain.created
    assert v8.plain.body.endswith(", edited\n")
    # A verify stamp with its anchor.
    assert v8.verified.last_verified_at is not None
    assert v8.verified.verified_paths == ["docs/release.md"]
    assert v8.verified.verified_head is not None
    assert v8.verified.origin is not None and v8.verified.category is not None
    # Every optional field at once.
    assert v8.full.actor is not None and v8.full.origin is not None
    assert [link.target_id for link in v8.full.links] == [v8.plain.id, v8.verified.id]
    assert v8.full.claims and v8.full.verified_absent_paths
    assert v8.full.corroborations == 2 and v8.full.last_corroborated is not None
    # Two tombstones, one renamed to the pre-2.6.4 name.
    assert {t.id for _, t in iter_tombstones(v8.root)} == {
        v8.tombstoned_id,
        v8.legacy_id,
    }
    assert v8.legacy_tombstone_path.name.endswith(".tombstone.md")
    assert v8.legacy_id not in v8.legacy_tombstone_path.name
    # Three episodes in two sessions, one a floor.
    assert len(v8.episodes) == 3
    assert {e.session_id for e in v8.episodes} == {"sess_a", "sess_b"}
    assert sum(e.is_floor for e in v8.episodes) == 1
    # Events across a rotated archive, a shard and the legacy file.
    names = {p.name for p in v8.root.iterdir()}
    assert any(n.startswith(".events-") and n.endswith(".jsonl.gz") for n in names)
    assert any(n.startswith(".events.") and n.endswith(".jsonl") for n in names)
    assert EVENT_LOG_FILENAME in names
    assert dict(v8.event_kinds) == MANIFEST["event_kinds"]
    assert sum(v8.event_kinds.values()) == MANIFEST["events_total"]
    search = next(ev for ev in iter_all_events(v8.root) if ev["kind"] == "search")
    assert search["query"] == _QUERY
    # The derived index, at the schema the v8 code last shipped.
    conn = sqlite3.connect(f"file:{v8.root / INDEX_FILENAME}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert int(row[0]) == MANIFEST["index_schema_version"] == 11


# ---------------------------------------------------------------------------
# The inventory and the dry run
# ---------------------------------------------------------------------------


def test_the_inventory_reads_every_source_with_the_v8_readers(v8: V8Fixture) -> None:
    inv = inventory(v8.root)
    assert {m.id for _, m in inv.memories} == {v8.full.id, v8.plain.id, v8.verified.id}
    assert inv.unparseable_memories == 0
    assert {t.dead.id for t in inv.tombstones} == {v8.tombstoned_id, v8.legacy_id}
    legacy = next(t for t in inv.tombstones if t.dead.id == v8.legacy_id)
    assert legacy.legacy_name is True
    assert legacy.filename == active_filename_for_tombstone(
        v8.legacy_tombstone_path.name
    )
    assert legacy.filename.endswith(".md") and ".tombstone" not in legacy.filename
    removed = next(t for t in inv.tombstones if t.dead.id == v8.tombstoned_id)
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

    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert store.count_memories() == 3
        assert store.count_tombstones() == 2
        assert store.count_episodes() == 3
        ids = [v8.full.id, v8.plain.id, v8.verified.id]
        assert store.provenance_for(ids) == {i: IMPORTED for i in ids}
        assert store.memory_ids() == [m.id for _, m in iter_active(v8.root)]
        assert store.filename_for(v8.full.id) == memory_filename(v8.full)
        assert (
            store.filename_for(v8.full.id) == MANIFEST["memories"]["full"]["filename"]
        )
        assert store.get_memory(v8.full.id) == v8.full
        assert store.get_memory(v8.plain.id) == v8.plain
        assert store.get_memory(v8.verified.id) == v8.verified

        dead = store.get_tombstone(v8.tombstoned_id)
        assert dead.removed_session == "sess_x"
        assert dead.removed_reason == "no longer true"
        row = store.conn.execute(
            "SELECT filename, provenance, links_json, corroborations, "
            "last_corroborated FROM tombstones WHERE id = ?",
            (v8.tombstoned_id,),
        ).fetchone()
        assert row["provenance"] == IMPORTED
        assert (
            row["filename"] == MANIFEST["tombstones"]["tombstoned"]["active_filename"]
        )
        assert row["filename"] == memory_filename(parse_memory_file(v8.tombstoned_path))
        assert [link["target_id"] for link in json.loads(row["links_json"])] == [
            v8.plain.id
        ]
        assert row["corroborations"] == 1
        assert row["last_corroborated"] == (_NOW - timedelta(days=2)).isoformat()
        legacy_row = store.conn.execute(
            "SELECT filename FROM tombstones WHERE id = ?", (v8.legacy_id,)
        ).fetchone()
        assert legacy_row["filename"] == active_filename_for_tombstone(
            v8.legacy_tombstone_path.name
        )

        floor = next(e for e in v8.episodes if e.is_floor)
        assert store.get_episode(floor.id).is_floor is True
        assert {e.id for e in store.iter_episodes()} == {e.id for e in v8.episodes}
        # The store carries what the v8 reader hands back, which is the
        # file's body without the trailing newline the writer added.
        as_read = list_by_session(v8.root, "sess_a")[0]
        assert store.get_episode(as_read.id) == as_read

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
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        rows_before = len(store.log_rows())
    second = migrate(v8.root, store_path, keys_dir=keys_dir)
    for name, counts in _counts(second).items():
        assert counts["imported"] == 0, name
        assert counts["present"] == _counts(first)[name]["imported"], name
    assert second.log_rows == 0
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert len(store.log_rows()) == rows_before
        assert store.log_verify()["status"] == "ok"


def test_a_record_removed_or_restored_in_the_new_store_is_left_alone(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    with Store.open(store_path, keys_dir=keys_dir) as store:
        store.tombstone(v8.plain.id, "removed after the migration")
        store.restore(v8.tombstoned_id)
    report = migrate(v8.root, store_path, keys_dir=keys_dir)
    assert report.memories == {
        "found": 3,
        "imported": 0,
        "present": 3,
        "unparseable": 0,
    }
    assert report.tombstones["imported"] == 0 and report.tombstones["present"] == 2
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        assert store.has_memory(v8.tombstoned_id)
        assert not store.has_memory(v8.plain.id)
        assert store.get_tombstone(v8.plain.id).removed_reason == (
            "removed after the migration"
        )
        assert store.log_verify()["status"] == "ok"


def test_new_v8_events_are_imported_by_a_later_run(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    _append_event(
        v8.root,
        {
            "ts": "2026-09-27T00:00:00.000000Z",
            "session": "sess_c",
            "kind": "show",
            "id": v8.plain.id,
        },
    )
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
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
        search = next(ev for ev in store.iter_events() if ev["kind"] == "search")
        assert set(search["query"]) == {"hash", "preview", "len"}
        assert search["query"]["preview"] == _QUERY[:32]
        assert search["query"]["len"] == len(_QUERY)
        assert search["imported_from"] == "v8"
        audited = next(ev for ev in store.iter_events() if ev["kind"] == "turn_audited")
        assert audited["probe_query"]["preview"] == MANIFEST["probe_query"]
        for row in store.log_rows():
            assert _QUERY not in row.payload


def test_the_v8_migrate_event_is_telemetry_and_the_control_row_is_migrate_v8(
    v8: V8Fixture, store_path: Path, keys_dir: Path
) -> None:
    migrate(v8.root, store_path, keys_dir=keys_dir)
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
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
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
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
            v8.legacy_id if source == v8.legacy_tombstone_path else v8.tombstoned_id
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
    before = compute_report(
        memories=sorted(
            (m for _, m in iter_active(v8.root)), key=lambda m: m.created, reverse=True
        ),
        events=iter_all_events(v8.root),
        now=now,
        since=timedelta(days=30),
        tombstoned_ids={t.id for _, t in iter_tombstones(v8.root)},
        version="test",
    )
    with Store.open(store_path, keys_dir=keys_dir, allow_rekey=False) as store:
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
    with Store.open(
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
