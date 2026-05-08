"""Tests for migrate.py — the one-shot origin backfill."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import _frontmatter as frontmatter
from bettermemory.migrate import (
    infer_origin_for_memory_dir,
    migrate_origin_in_directory,
)
from bettermemory.origin import Origin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_GIT_AVAILABLE = shutil.which("git") is not None


def _init_repo(path: Path, *, remote: str | None = None) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if remote is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=path,
            check=True,
            capture_output=True,
        )


_LEGACY_TEMPLATE = """\
---
id: {id}
created: 2025-01-01T00:00:00+00:00
updated: 2025-01-01T00:00:00+00:00
scopes:
- tools
confidence: medium
source: explicit-statement
---
{body}
"""


# Valid Crockford-Base32 ULID (no I/L/O/U). Used for synthetic legacy files.
_LEGACY_IDS = (
    "01HXYZKEGACYJDKEGACY00000Z",
    "01HXYZKEGACYJDKEGACY00001Z",
    "01HXYZKEGACYJDKEGACY00002Z",
)


def _write_legacy(memory_dir: Path, *, name: str, body: str, id_: str) -> Path:
    path = memory_dir / f"2025-01-01-{name}.md"
    path.write_text(_LEGACY_TEMPLATE.format(id=id_, body=body), encoding="utf-8")
    return path


def _read_metadata(path: Path) -> dict:
    return dict(frontmatter.load(path).metadata)


# ---------------------------------------------------------------------------
# infer_origin_for_memory_dir
# ---------------------------------------------------------------------------


def test_infer_origin_returns_none_for_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory dir whose parent is `~/` — global, no inference. The
    monkeypatch makes `Path.home()` resolve to `tmp_path` for hermeticity."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    assert infer_origin_for_memory_dir(memory_dir) is None


def test_infer_origin_returns_none_outside_git(tmp_path: Path) -> None:
    """Parent isn't a git repo and isn't home — still nothing to infer."""
    project = tmp_path / "project"
    project.mkdir()
    memory_dir = project / ".claude-memory"
    memory_dir.mkdir()
    assert infer_origin_for_memory_dir(memory_dir) is None


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_infer_origin_picks_up_repo_remote(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_repo(project, remote="git@github.com:example/foo.git")
    memory_dir = project / ".claude-memory"
    memory_dir.mkdir()

    origin = infer_origin_for_memory_dir(memory_dir)
    assert origin is not None
    assert origin.repo == "git@github.com:example/foo.git"
    assert origin.cwd == str(project.resolve())
    # Branch is deliberately null — we don't know the original.
    assert origin.branch is None


# ---------------------------------------------------------------------------
# migrate_origin_in_directory — happy path
# ---------------------------------------------------------------------------


def test_migration_backfills_legacy_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    path = _write_legacy(
        memory_dir,
        name="legacy",
        body="kubernetes networking notes",
        id_=_LEGACY_IDS[0],
    )

    inferred = Origin(
        cwd="/projects/foo", repo="git@github.com:example/foo.git"
    )
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    assert report.scanned == 1
    assert report.updated == 1
    assert report.already_had_origin == 0

    meta = _read_metadata(path)
    assert meta["origin"] == {
        "cwd": "/projects/foo",
        "repo": "git@github.com:example/foo.git",
    }


def test_migration_is_idempotent(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    _write_legacy(
        memory_dir,
        name="legacy",
        body="x",
        id_=_LEGACY_IDS[0],
    )

    inferred = Origin(repo="git@github.com:example/foo.git")
    first = migrate_origin_in_directory(memory_dir, inferred=inferred)
    second = migrate_origin_in_directory(memory_dir, inferred=inferred)

    assert first.updated == 1
    assert second.updated == 0
    assert second.already_had_origin == 1


def test_migration_dry_run_writes_nothing(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(
        memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0]
    )
    original = path.read_text(encoding="utf-8")

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(
        memory_dir, inferred=inferred, dry_run=True
    )

    assert report.updated == 1  # would update
    assert path.read_text(encoding="utf-8") == original  # but didn't


def test_migration_skips_memories_with_existing_origin(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    legacy = _write_legacy(
        memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0]
    )
    # Manually add origin so the file looks "modern".
    post = frontmatter.load(legacy)
    post.metadata["origin"] = {
        "cwd": "/old",
        "repo": "git@github.com:original/repo.git",
    }
    legacy.write_text(frontmatter.dumps(post), encoding="utf-8")

    inferred = Origin(repo="git@github.com:other/repo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    assert report.already_had_origin == 1
    assert report.updated == 0
    # Existing origin preserved unchanged.
    meta = _read_metadata(legacy)
    assert meta["origin"]["repo"] == "git@github.com:original/repo.git"


def test_migration_handles_mixed_directory(tmp_path: Path) -> None:
    """Directory with three legacy + one already-tagged memory: three
    updates, one skip."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    for i, body in enumerate(("alpha", "beta", "gamma")):
        _write_legacy(
            memory_dir, name=body, body=body, id_=_LEGACY_IDS[i]
        )
    # And one already-tagged one.
    tagged_path = memory_dir / "2025-01-02-tagged.md"
    tagged_path.write_text(
        _LEGACY_TEMPLATE.format(id="01HXYZKEGACYTAGGED0000000Z", body="tagged")
        + "",
        encoding="utf-8",
    )
    post = frontmatter.load(tagged_path)
    post.metadata["origin"] = {"cwd": "/x", "repo": "git@github.com:x/y.git"}
    tagged_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    assert report.scanned == 4
    assert report.updated == 3
    assert report.already_had_origin == 1


def test_migration_no_inferred_origin_is_a_noop(tmp_path: Path) -> None:
    """When no Origin can be inferred and no force_repo is given, the
    migration is a no-op rather than a destructive null-write."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(
        memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0]
    )
    original = path.read_text(encoding="utf-8")

    report = migrate_origin_in_directory(memory_dir)

    assert report.scanned == 0
    assert report.updated == 0
    assert path.read_text(encoding="utf-8") == original


def test_migration_force_repo_takes_precedence(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(
        memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0]
    )

    report = migrate_origin_in_directory(
        memory_dir,
        inferred=Origin(repo="git@github.com:auto/inferred.git"),
        force_repo="git@github.com:explicit/override.git",
    )

    assert report.updated == 1
    meta = _read_metadata(path)
    assert meta["origin"]["repo"] == "git@github.com:explicit/override.git"


def test_migration_skips_malformed_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    good = _write_legacy(
        memory_dir, name="good", body="ok", id_=_LEGACY_IDS[0]
    )
    bad = memory_dir / "2025-01-01-broken.md"
    bad.write_text("not yaml at all\n", encoding="utf-8")

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    # Bad file is reported but doesn't kill the migration.
    assert bad in report.malformed
    # Good file gets migrated.
    assert "origin" in _read_metadata(good)


def test_migration_skips_tombstone_directory(tmp_path: Path) -> None:
    """Tombstones live in `.tombstones/` and represent removal events.
    Backfilling origin into a tombstone would change the audit log
    retroactively — don't."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    tombs = memory_dir / ".tombstones"
    tombs.mkdir()
    tombstone = tombs / "2025-01-01-old-fact.tombstone.md"
    tombstone.write_text(
        _LEGACY_TEMPLATE.format(id=_LEGACY_IDS[0], body="dead"),
        encoding="utf-8",
    )

    # And one active memory at the top level.
    active = _write_legacy(
        memory_dir, name="active", body="alive", id_=_LEGACY_IDS[1]
    )

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    # Only the active memory was scanned.
    assert report.scanned == 1
    assert report.updated == 1
    # Tombstone untouched.
    tomb_meta = frontmatter.load(tombstone).metadata
    assert "origin" not in tomb_meta
    # Active memory got origin.
    assert "origin" in _read_metadata(active)


def test_migration_preserves_all_other_frontmatter_fields(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(
        memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0]
    )

    before = _read_metadata(path)

    inferred = Origin(repo="git@github.com:example/foo.git")
    migrate_origin_in_directory(memory_dir, inferred=inferred)

    after = _read_metadata(path)
    # Every original field is preserved unchanged.
    for key in ("id", "created", "updated", "scopes", "confidence", "source"):
        assert after[key] == before[key], f"field {key} should not change"
    # And origin was added.
    assert "origin" in after


def test_migrated_memory_loads_correctly_via_store(tmp_path: Path) -> None:
    """Round-trip: after migration, the Store must load the file with a
    populated `Origin`."""
    from bettermemory.store import Store

    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    _write_legacy(
        memory_dir, name="legacy", body="durable fact", id_=_LEGACY_IDS[0]
    )

    inferred = Origin(
        cwd="/projects/foo",
        repo="git@github.com:example/foo.git",
    )
    migrate_origin_in_directory(memory_dir, inferred=inferred)

    store = Store(memory_dir)
    loaded = store.load_one(_LEGACY_IDS[0])
    assert loaded.origin is not None
    assert loaded.origin.repo == "git@github.com:example/foo.git"
    assert loaded.origin.cwd == "/projects/foo"
    assert loaded.origin.branch is None
