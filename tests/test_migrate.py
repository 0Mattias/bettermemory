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

    inferred = Origin(cwd="/projects/foo", repo="git@github.com:example/foo.git")
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
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])
    original = path.read_text(encoding="utf-8")

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred, dry_run=True)

    assert report.updated == 1  # would update
    assert path.read_text(encoding="utf-8") == original  # but didn't


def test_migration_skips_memories_with_existing_origin(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    legacy = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])
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
        _write_legacy(memory_dir, name=body, body=body, id_=_LEGACY_IDS[i])
    # And one already-tagged one.
    tagged_path = memory_dir / "2025-01-02-tagged.md"
    tagged_path.write_text(
        _LEGACY_TEMPLATE.format(id="01HXYZKEGACYTAGGED0000000Z", body="tagged") + "",
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
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])
    original = path.read_text(encoding="utf-8")

    report = migrate_origin_in_directory(memory_dir)

    assert report.scanned == 0
    assert report.updated == 0
    assert path.read_text(encoding="utf-8") == original


def test_migration_force_repo_takes_precedence(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])

    report = migrate_origin_in_directory(
        memory_dir,
        inferred=Origin(repo="git@github.com:auto/inferred.git"),
        force_repo="git@github.com:explicit/override.git",
    )

    assert report.updated == 1
    meta = _read_metadata(path)
    assert meta["origin"]["repo"] == "git@github.com:explicit/override.git"


def test_force_repo_does_not_fabricate_cwd(tmp_path: Path) -> None:
    """When the caller passes --repo on a global directory, cwd should
    stay null in the written origin. The memory_dir.parent on a global
    dir is `~/`, which would be actively misleading as a per-memory
    cwd value."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])

    migrate_origin_in_directory(memory_dir, force_repo="git@github.com:me/foo.git")

    origin = _read_metadata(path)["origin"]
    assert origin["repo"] == "git@github.com:me/foo.git"
    assert "cwd" not in origin


def test_scope_map_does_not_fabricate_cwd(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy_with_scopes(
        memory_dir,
        name="alpha",
        body="x",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
    )

    migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"projects:alpha": "git@github.com:me/alpha.git"},
    )

    origin = _read_metadata(path)["origin"]
    assert origin["repo"] == "git@github.com:me/alpha.git"
    assert "cwd" not in origin


def test_inferred_path_keeps_cwd(tmp_path: Path) -> None:
    """The auto-inferred path (project-scoped dir whose parent is a real
    git repo) is the *one* place we have legitimate evidence for cwd —
    it's the project root. Verify cwd survives in that path."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])

    migrate_origin_in_directory(
        memory_dir,
        inferred=Origin(
            cwd="/projects/foo",
            repo="git@github.com:example/foo.git",
        ),
    )

    origin = _read_metadata(path)["origin"]
    assert origin["cwd"] == "/projects/foo"
    assert origin["repo"] == "git@github.com:example/foo.git"


def test_migration_skips_malformed_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    good = _write_legacy(memory_dir, name="good", body="ok", id_=_LEGACY_IDS[0])
    bad = memory_dir / "2025-01-01-broken.md"
    bad.write_text("not yaml at all\n", encoding="utf-8")

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    # Bad file is reported but doesn't kill the migration.
    assert bad in report.malformed
    # Good file gets migrated.
    assert "origin" in _read_metadata(good)


def test_migration_continues_when_a_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (write-guard): a per-file write failure must not abort
    the whole migration loop, and must not inflate ``report.updated``.

    ``_atomic_write_post`` can raise ``OSError`` (ENOSPC/EACCES/EIO
    mid-write or on the rename) or ``ValueError`` (the dumps 64 KB YAML
    cap). Previously the write was outside any try/except and
    ``report.updated`` was incremented *before* it, so a single failing
    file (a) aborted the loop with a traceback, leaving every subsequent
    memory unprocessed, and (b) counted the never-persisted file as
    updated. The write must now be guarded mirroring the read path: the
    failed file is recorded in ``malformed``, the loop continues, and
    ``updated`` counts only files that actually persisted.

    We patch ``_atomic_write_post`` to raise ``OSError`` for exactly one
    of three files; the other two must still migrate and the count must
    be 2, not 3."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    first = _write_legacy(memory_dir, name="aaa-first", body="x", id_=_LEGACY_IDS[0])
    failing = _write_legacy(memory_dir, name="bbb-fails", body="y", id_=_LEGACY_IDS[1])
    last = _write_legacy(memory_dir, name="ccc-last", body="z", id_=_LEGACY_IDS[2])

    real_write = __import__(
        "bettermemory.store", fromlist=["_atomic_write_post"]
    )._atomic_write_post

    def flaky_write(path: Path, post: object) -> None:
        if Path(path) == failing:
            raise OSError("simulated ENOSPC")
        return real_write(path, post)

    # `migrate.py` imports the symbol directly
    # (`from .store import ... _atomic_write_post`), so patch it on the
    # `migrate` module namespace where the loop resolves it.
    monkeypatch.setattr("bettermemory.migrate._atomic_write_post", flaky_write)

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    # The loop didn't abort: all three files were scanned.
    assert report.scanned == 3
    # The failing file is recorded and NOT counted as updated.
    assert failing in report.malformed
    # Only the two files that actually persisted are counted.
    assert report.updated == 2
    # The two good files really did get origin written...
    assert "origin" in _read_metadata(first)
    assert "origin" in _read_metadata(last)
    # ...and the failing file was left untouched (atomic write is
    # all-or-nothing — no partial/origin-stamped persistence).
    assert "origin" not in _read_metadata(failing)


def test_mid_run_tombstone_not_reported_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (core-robustness): a memory tombstoned mid-run — a
    concurrent ``Store.tombstone`` writes the tombstone copy then
    ``unlink``s the active file — makes ``frontmatter.load(path)`` raise
    ``FileNotFoundError`` under the migrator's lock. The broad except
    used to catch that and label the (validly removed) memory
    ``malformed``, which the CLI surfaces as 'fix these files'. The
    narrowed ``except FileNotFoundError`` must skip it silently instead.

    We simulate the race by patching ``frontmatter.load`` to unlink the
    target file on first access (the same observable effect as a
    concurrent tombstone landing between the directory scan and the
    locked read), then raise ``FileNotFoundError`` like the real load
    would on the now-missing path."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    vanishing = _write_legacy(
        memory_dir, name="vanishing", body="will be tombstoned", id_=_LEGACY_IDS[0]
    )
    survivor = _write_legacy(
        memory_dir, name="survivor", body="stays put", id_=_LEGACY_IDS[1]
    )

    # `frontmatter` is the same module object `migrate.py` loads through
    # (`from . import _frontmatter as frontmatter`), so patching its
    # `load` here intercepts the migrator's read.
    real_load = frontmatter.load

    def vanishing_load(path: Path) -> object:
        if Path(path) == vanishing:
            # Model the concurrent tombstone: the active file is gone by
            # the time the migrator's locked read opens it.
            vanishing.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        return real_load(path)

    monkeypatch.setattr("bettermemory.migrate.frontmatter.load", vanishing_load)

    inferred = Origin(repo="git@github.com:example/foo.git")
    report = migrate_origin_in_directory(memory_dir, inferred=inferred)

    # The vanished/tombstoned memory must NOT be reported as malformed.
    assert vanishing not in report.malformed
    assert report.malformed == []
    # The surviving memory is still migrated normally.
    assert report.updated == 1
    assert "origin" in _read_metadata(survivor)


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
    active = _write_legacy(memory_dir, name="active", body="alive", id_=_LEGACY_IDS[1])

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
    path = _write_legacy(memory_dir, name="legacy", body="x", id_=_LEGACY_IDS[0])

    before = _read_metadata(path)

    inferred = Origin(repo="git@github.com:example/foo.git")
    migrate_origin_in_directory(memory_dir, inferred=inferred)

    after = _read_metadata(path)
    # Every original field is preserved unchanged.
    for key in ("id", "created", "updated", "scopes", "confidence", "source"):
        assert after[key] == before[key], f"field {key} should not change"
    # And origin was added.
    assert "origin" in after


# ---------------------------------------------------------------------------
# Scope-based routing
# ---------------------------------------------------------------------------


def _write_legacy_with_scopes(
    memory_dir: Path,
    *,
    name: str,
    body: str,
    id_: str,
    scopes: list[str],
) -> Path:
    template = (
        "---\n"
        f"id: {id_}\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        "scopes:\n" + "".join(f"- {s}\n" for s in scopes) + "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        f"{body}\n"
    )
    path = memory_dir / f"2025-01-01-{name}.md"
    path.write_text(template, encoding="utf-8")
    return path


def test_scope_repo_map_routes_by_scope(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    a = _write_legacy_with_scopes(
        memory_dir,
        name="alpha",
        body="alpha",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
    )
    b = _write_legacy_with_scopes(
        memory_dir,
        name="beta",
        body="beta",
        id_=_LEGACY_IDS[1],
        scopes=["projects:beta"],
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={
            "projects:alpha": "git@github.com:me/alpha.git",
            "projects:beta": "git@github.com:me/beta.git",
        },
    )

    assert report.updated == 2
    assert _read_metadata(a)["origin"]["repo"] == "git@github.com:me/alpha.git"
    assert _read_metadata(b)["origin"]["repo"] == "git@github.com:me/beta.git"


def test_scope_map_first_match_wins(tmp_path: Path) -> None:
    """A memory with multiple scopes routes to the first map entry that
    matches one of them. Insertion order of the dict determines priority."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    path = _write_legacy_with_scopes(
        memory_dir,
        name="multi",
        body="x",
        id_=_LEGACY_IDS[0],
        scopes=["infra", "projects:foo"],
    )

    report = migrate_origin_in_directory(
        memory_dir,
        # `projects:foo` comes first → that wins even though the memory
        # also has `infra`.
        scope_repo_map={
            "projects:foo": "git@github.com:me/foo.git",
            "infra": "git@github.com:me/infrastructure.git",
        },
    )
    assert report.updated == 1
    assert _read_metadata(path)["origin"]["repo"] == "git@github.com:me/foo.git"


def test_scope_map_misses_leave_memory_alone(tmp_path: Path) -> None:
    """When no map entry matches and there's no force_repo / inferred
    fallback, the memory keeps its (lack of) origin — no destructive
    default tagging."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    matched = _write_legacy_with_scopes(
        memory_dir,
        name="matched",
        body="x",
        id_=_LEGACY_IDS[0],
        scopes=["projects:foo"],
    )
    unmatched = _write_legacy_with_scopes(
        memory_dir,
        name="unmatched",
        body="y",
        id_=_LEGACY_IDS[1],
        scopes=["personal-context"],
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"projects:foo": "git@github.com:me/foo.git"},
    )

    assert report.updated == 1
    assert "origin" in _read_metadata(matched)
    assert "origin" not in _read_metadata(unmatched)


def test_scope_map_misses_fall_through_to_force_repo(tmp_path: Path) -> None:
    """If --scope-repo doesn't match but --repo is given, the unmatched
    memory still gets tagged."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    matched = _write_legacy_with_scopes(
        memory_dir,
        name="matched",
        body="x",
        id_=_LEGACY_IDS[0],
        scopes=["projects:foo"],
    )
    fallback = _write_legacy_with_scopes(
        memory_dir,
        name="fallback",
        body="y",
        id_=_LEGACY_IDS[1],
        scopes=["personal-context"],
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"projects:foo": "git@github.com:me/foo.git"},
        force_repo="git@github.com:me/global.git",
    )

    assert report.updated == 2
    assert _read_metadata(matched)["origin"]["repo"] == "git@github.com:me/foo.git"
    assert _read_metadata(fallback)["origin"]["repo"] == "git@github.com:me/global.git"


def test_scope_map_idempotent(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    _write_legacy_with_scopes(
        memory_dir,
        name="alpha",
        body="x",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
    )
    mapping = {"projects:alpha": "git@github.com:me/alpha.git"}

    first = migrate_origin_in_directory(memory_dir, scope_repo_map=mapping)
    second = migrate_origin_in_directory(memory_dir, scope_repo_map=mapping)

    assert first.updated == 1
    assert second.updated == 0
    assert second.already_had_origin == 1


def test_migrated_memory_loads_correctly_via_store(tmp_path: Path) -> None:
    """Round-trip: after migration, the Store must load the file with a
    populated `Origin`."""
    from bettermemory.store import Store

    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    _write_legacy(memory_dir, name="legacy", body="durable fact", id_=_LEGACY_IDS[0])

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
