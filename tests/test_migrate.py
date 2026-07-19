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

    def flaky_write(
        path: Path,
        post: object,
        *,
        max_file_bytes: int = frontmatter._MAX_FILE_BYTES,
        max_yaml_bytes: int = frontmatter._MAX_YAML_BYTES,
    ) -> None:
        if Path(path) == failing:
            raise OSError("simulated ENOSPC")
        # Forward BOTH caps — migrate re-dumps the origin append at the
        # band-keyed file AND frontmatter-YAML lifecycle caps (a lifecycle
        # re-dump of an already-admitted record). The double must mirror the
        # real `_atomic_write_post` signature or the call raises `TypeError`.
        return real_write(
            path,
            post,
            max_file_bytes=max_file_bytes,
            max_yaml_bytes=max_yaml_bytes,
        )

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


def test_migration_survives_scalar_scopes_field(tmp_path: Path) -> None:
    """A malformed memory whose `scopes` is a scalar (not a list) must not
    abort scope-routed migration. Pre-fix, `scope in <scalar>` raised
    TypeError *outside* the per-file try/except, killing the whole loop so
    the migration call raised before returning — no report was produced and
    any file not yet scanned went unmigrated.

    Note: `_iter_active_memory_files` uses `iterdir()`, whose order is
    filesystem-dependent, so this test does NOT rely on scan order. Pre-fix
    the loop crashes whenever it reaches the scalar file (regardless of
    order), so the call raises and the test errors before its assertions.
    Post-fix both files are always scanned, so the assertions are
    order-independent too."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    # `scopes: 5` — the permissive frontmatter parser loads this as an int,
    # exactly the scalar-scopes class the store readers defensively coerce.
    scalar = (
        "---\n"
        f"id: {_LEGACY_IDS[0]}\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        "scopes: 5\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "malformed scalar scopes\n"
    )
    valid = (
        "---\n"
        f"id: {_LEGACY_IDS[1]}\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        "scopes:\n"
        "- tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "valid list scopes\n"
    )
    bad_path = memory_dir / "2025-01-01-bad.md"
    good_path = memory_dir / "2025-01-01-good.md"
    bad_path.write_text(scalar, encoding="utf-8")
    good_path.write_text(valid, encoding="utf-8")

    # Scope-routed migration only — no `inferred`/`force_repo`, so the
    # fallback never fires. The scalar file simply matches nothing; the
    # `tools`-scoped file must still get its origin backfilled.
    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"tools": "git@github.com:example/foo.git"},
    )

    # Both files scanned; the crash never happens (pre-fix, the call above
    # raised TypeError and never returned a report).
    assert report.scanned == 2
    # Only the valid, tools-scoped memory was routed and updated.
    assert report.updated == 1
    # The scalar file matched no scope and has no fallback, so it is left
    # alone — untouched and NOT flagged malformed (it loaded fine).
    assert bad_path not in report.malformed
    assert "origin" not in _read_metadata(bad_path)
    # The regression witness: the valid file still migrated.
    assert _read_metadata(good_path)["origin"] == {
        "repo": "git@github.com:example/foo.git",
    }


def _write_legacy_scopes_yaml(
    memory_dir: Path, *, name: str, id_: str, scopes_yaml: str
) -> Path:
    """Write a legacy memory file with a raw `scopes` YAML fragment, so a test
    can exercise dict-/set-shaped `scopes` the block-list helper can't express."""
    text = (
        "---\n"
        f"id: {id_}\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        f"{scopes_yaml}"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "body\n"
    )
    path = memory_dir / f"2025-01-01-{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_scope_repo_map_routes_dict_shaped_scopes(tmp_path: Path) -> None:
    """F4/item-6: a `scopes` field that YAML parsed as a MAPPING (`scopes:
    {tools: 1}`) is resolved by the store (`list(meta["scopes"])`) to its keys
    — the real scope list — but the old migrator coercion (`_load_str_list`)
    returned [] for any non-list, so the scope_repo_map never matched and the
    file was left untagged (or force-tagged with the wrong repo). The shared
    `_coerce_scopes` makes the migrator see the same scopes the store does.
    Reverting to `_load_str_list` leaves this record unrouted (updated==0)."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy_scopes_yaml(
        memory_dir,
        name="dict-scopes",
        id_=_LEGACY_IDS[0],
        scopes_yaml="scopes:\n  tools: 1\n",
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"tools": "git@github.com:me/tools.git"},
    )

    assert report.updated == 1
    assert _read_metadata(path)["origin"]["repo"] == "git@github.com:me/tools.git"


def test_scope_repo_map_routes_set_shaped_scopes(tmp_path: Path) -> None:
    """F4/item-6 twin: a `scopes` field that YAML parsed as a SET (`scopes:
    !!set {tools: null}`) resolves under `list(meta["scopes"])` to its elements.
    `_coerce_scopes` must route it the same way; `_load_str_list` returned []
    and left it unrouted."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_legacy_scopes_yaml(
        memory_dir,
        name="set-scopes",
        id_=_LEGACY_IDS[0],
        scopes_yaml="scopes: !!set {tools: null}\n",
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map={"tools": "git@github.com:me/tools.git"},
    )

    assert report.updated == 1
    assert _read_metadata(path)["origin"]["repo"] == "git@github.com:me/tools.git"


def test_migration_backfills_band_legacy_record(tmp_path: Path) -> None:
    """Item-1c (v2): the origin backfill only APPENDS a small `origin` block to
    an already-admitted, already-readable legacy record, so it re-dumps at the
    band-keyed `_lifecycle_redump_cap`. A pre-3.14.1 record whose serialized
    size sits in the reserved band — below the removal-budget ceiling — must
    still get its origin backfilled. Reverting the cap to the write-cap default
    makes the re-dump raise ValueError, which the loop records as `malformed` —
    the record silently never gets origin."""
    from datetime import datetime, timezone

    from bettermemory.store import _REMOVAL_META_BUDGET_BYTES

    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    post = frontmatter.Post(
        content="x" * 1_045_000,
        metadata={
            "id": _LEGACY_IDS[0],
            "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "scopes": ["tools"],
            "confidence": "medium",
            "source": "explicit-statement",
        },
    )
    text = frontmatter.dumps(post, max_file_bytes=frontmatter._MAX_FILE_BYTES)
    total = len(text.encode("utf-8"))
    # In the band, with room under the removal-budget ceiling for the origin
    # block — the shape the band arm exists to keep backfillable.
    assert frontmatter._MAX_WRITE_BYTES < total
    assert total < frontmatter._MAX_FILE_BYTES - _REMOVAL_META_BUDGET_BYTES - 200
    band = memory_dir / f"2025-01-01-band-{_LEGACY_IDS[0].lower()}.md"
    # Raw bytes: the store writes UTF-8 bytes (LF) on every platform;
    # `write_text` would CRLF-translate on Windows and inflate the on-disk
    # size past the LF `total` the band assertions above are computed on.
    band.write_bytes(text.encode("utf-8"))

    report = migrate_origin_in_directory(
        memory_dir, inferred=Origin(repo="git@github.com:example/foo.git")
    )

    assert band not in report.malformed
    assert report.updated == 1
    assert _read_metadata(band)["origin"]["repo"] == "git@github.com:example/foo.git"


def test_migration_refuses_to_grow_subcap_record_into_band(tmp_path: Path) -> None:
    """The band-discipline twin `mark_verified` / `rename_scope` got in 3.15.0,
    applied to the one mutator that release missed: the origin backfill's
    re-dump caps through the shared `_lifecycle_redump_cap`, so a legacy record
    admitted just under the write cap cannot be grown INTO the reserved band by
    a long caller-controlled origin (a `--force-repo` URL, a deep checkout
    `cwd`). At the flat read cap the backfill silently minted a band record —
    re-opening the exact un-removable / restore-refused / prune-hard-deletes
    chain the discipline closed. The refusal is loud: the file lands in
    `report.malformed` untouched. Reverting the cap to the flat read cap makes
    the backfill succeed here and this test fail."""
    from datetime import datetime, timezone

    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    post = frontmatter.Post(
        content="x" * 1_044_100,
        metadata={
            "id": _LEGACY_IDS[0],
            "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "scopes": ["tools"],
            "confidence": "medium",
            "source": "explicit-statement",
        },
    )
    text = frontmatter.dumps(post, max_file_bytes=frontmatter._MAX_FILE_BYTES)
    total = len(text.encode("utf-8"))
    # Just under the write cap: a normal, maintainable record whose headroom
    # the origin block below would eat.
    assert total <= frontmatter._MAX_WRITE_BYTES
    assert frontmatter._MAX_WRITE_BYTES - total < 400
    near_cap = memory_dir / f"2025-01-01-nearcap-{_LEGACY_IDS[0].lower()}.md"
    # Raw bytes so the on-disk size equals the LF `total` on every platform:
    # `write_text` CRLF-translates on Windows (+1 byte per newline), which is
    # both unfaithful to the store's byte-exact writer AND breaks the
    # size-unchanged assertion below. This is what failed the 3.15.1 release
    # on windows-latest.
    near_cap.write_bytes(text.encode("utf-8"))

    long_repo = "https://example.com/" + "r" * 400  # caller-controlled growth
    report = migrate_origin_in_directory(memory_dir, force_repo=long_repo)

    # Loud refusal, file untouched: no origin, size unchanged, reported.
    assert near_cap in report.malformed
    assert report.updated == 0
    assert near_cap.stat().st_size == total
    assert "origin" not in _read_metadata(near_cap)


def test_migration_refuses_to_grow_record_into_yaml_removal_band(
    tmp_path: Path,
) -> None:
    """YAML-axis twin of ``test_migration_refuses_to_grow_subcap_record_into_band``.

    The file-axis band discipline alone did NOT protect the frontmatter-YAML
    axis. ``_frontmatter.dumps`` enforces ``_MAX_YAML_BYTES`` on the serialized
    frontmatter region UNCONDITIONALLY, independent of total file size, so a
    densely-``verified_paths`` legacy record (a pre-3.15.1 verify, a ``sync
    pull``, a hand-edit) can have an enormous file-size budget yet sit only a
    few dozen bytes below the YAML cap. Backfilling ``origin`` against the flat
    ``_MAX_YAML_BYTES`` — the pre-fix behaviour, where migrate's
    ``_atomic_write_post`` call omitted ``max_yaml_bytes`` and re-dumped at the
    flat cap — silently grew such a record INTO the reserved removal band: the
    write reported ``updated``, but the record's own future tombstone's
    ``removed:`` line no longer fit under ``_MAX_YAML_BYTES`` even after the
    dual-axis adaptive trim. An un-removable record, minted silently.

    The fix mirrors ``mark_verified`` / ``rename_scope``: the re-dump caps
    through ``_lifecycle_redump_yaml_cap`` keyed on the record's PRISTINE
    frontmatter (its serialized ``post.metadata`` WITHOUT the appended
    ``origin``), so the backfill is refused loudly — the file lands in
    ``report.malformed`` untouched and stays removable.

    Mutation witness: reverting only the source (dropping the
    ``max_yaml_bytes=_lifecycle_redump_yaml_cap(...)`` argument, so the re-dump
    falls back to the flat ``_MAX_YAML_BYTES``) makes the backfill succeed here
    — ``report.updated == 1``, ``origin`` on disk — and every assertion below
    fails.
    """
    from datetime import datetime, timezone

    from bettermemory.store import (
        _REMOVAL_META_BUDGET_BYTES,
        Store,
        _serialized_frontmatter_bytes,
    )

    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()

    # 63 verified_paths (<= the model's 64-entry cap) whose serialized
    # frontmatter lands just below `_MAX_YAML_BYTES` but strictly inside the
    # reserved removal band once the ~47-byte `origin` block is appended. The
    # last entry is padded to tune the pristine size deterministically; the
    # band assertions below fail loudly if the tuning ever drifts (e.g. a cap
    # constant changes).
    paths = [f"src/{'p' * 1023}/{i:02d}" for i in range(63)]
    paths[-1] = paths[-1] + ("q" * 93)
    metadata: dict = {
        "id": _LEGACY_IDS[0],
        "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "scopes": ["tools"],
        "confidence": "medium",
        "source": "explicit-statement",
        "verified_paths": paths,
    }

    pristine_yaml = _serialized_frontmatter_bytes(metadata)
    reserved_ceiling = frontmatter._MAX_YAML_BYTES - _REMOVAL_META_BUDGET_BYTES
    # In the band: above the reserved ceiling (so `_lifecycle_redump_yaml_cap`
    # freezes AT the pristine size, and — being strictly below the flat cap —
    # its tighter ceiling actually binds on the re-dump), yet below the flat cap
    # (so the pristine record is itself admissible and, crucially, still
    # tombstoneable).
    assert reserved_ceiling < pristine_yaml < frontmatter._MAX_YAML_BYTES

    # Appending origin keeps the frontmatter <= the flat `_MAX_YAML_BYTES`, so
    # the PRE-FIX re-dump (flat cap) succeeds silently — the mutation witness.
    # The fix's lifecycle cap is what turns this silent growth into a refusal.
    with_origin = dict(metadata)
    with_origin["origin"] = {"repo": "git@github.com:example/foo.git"}
    assert _serialized_frontmatter_bytes(with_origin) <= frontmatter._MAX_YAML_BYTES

    post = frontmatter.Post(content="band body\n", metadata=metadata)
    text = frontmatter.dumps(post, max_file_bytes=frontmatter._MAX_FILE_BYTES)
    band = memory_dir / f"2025-01-01-yamlband-{_LEGACY_IDS[0].lower()}.md"
    # Raw bytes (LF) so the on-disk frontmatter size equals the LF measurement
    # above on every platform — `write_text` CRLF-translates on Windows and
    # would inflate the YAML region off the tuned band.
    band.write_bytes(text.encode("utf-8"))
    before = band.read_bytes()

    report = migrate_origin_in_directory(
        memory_dir, inferred=Origin(repo="git@github.com:example/foo.git")
    )

    # (a) Loud refusal: reported malformed, nothing counted updated, and (b) the
    # file is byte-for-byte untouched (the atomic write never landed).
    assert band in report.malformed
    assert report.updated == 0
    assert band.read_bytes() == before
    assert "origin" not in _read_metadata(band)

    # (c) The record is STILL removable by the normal path — the property the
    # silent backfill destroyed. `tombstone` moves the active file into
    # `.tombstones/`, so it must now exist there and be gone from the active dir.
    tombstone_path = Store(memory_dir).tombstone(_LEGACY_IDS[0], reason="cleanup")
    assert tombstone_path.exists()
    assert not band.exists()


# ---------------------------------------------------------------------------
# `--repair`: fixing an origin that was CAPTURED, but captured wrong.
#
# The backfill above only ever fires on memories with no origin at all.
# The damage seen in the wild is different: a write made from a parent
# directory or $HOME sits outside any checkout, so `capture()` records a
# cwd with `repo=None` — and per `repos_match` a null repo matches every
# caller, so the memory silently goes global and leaks into every
# project's auto-scoped search.
# ---------------------------------------------------------------------------

_REPO_A = "https://github.com/me/alpha.git"
_REPO_B = "https://github.com/me/beta.git"
_MAP = {"projects:alpha": _REPO_A, "projects:beta": _REPO_B}


def _write_with_origin(
    memory_dir: Path,
    *,
    name: str,
    id_: str,
    scopes: list[str],
    origin: dict,
) -> Path:
    path = _write_legacy(memory_dir, name=name, body="x", id_=id_)
    post = frontmatter.load(path)
    post.metadata["scopes"] = list(scopes)
    post.metadata["origin"] = dict(origin)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def test_repair_anchors_null_repo_to_its_only_mapped_scope(tmp_path: Path) -> None:
    """The core case: written from `~/Documents`, so cwd is set but repo
    is null. Exactly one mapped scope names the owning repo — adopt it."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="stray",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
        origin={"cwd": "/Users/me/Documents"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert (report.repaired_anchored, report.repaired_demoted) == (1, 0)
    assert report.updated == 1
    meta = _read_metadata(path)
    assert meta["origin"]["repo"] == _REPO_A
    # The captured cwd is history — repair rewrites the repo, not the record.
    assert meta["origin"]["cwd"] == "/Users/me/Documents"


def test_repair_demotes_memory_dark_in_a_scope_it_claims(tmp_path: Path) -> None:
    """Scoped `projects:alpha` but anchored to beta — invisible from
    alpha. No single repo satisfies both, so global is the honest origin.
    `worktree_root` must go too: it is the second auto-scope
    discriminator, and leaving it would keep the memory dark."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="dark",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha", "projects:beta"],
        origin={"cwd": "/w/beta", "repo": _REPO_B, "worktree_root": "/w/beta"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert (report.repaired_anchored, report.repaired_demoted) == (0, 1)
    meta = _read_metadata(path)
    assert "repo" not in meta["origin"]
    assert "worktree_root" not in meta["origin"]
    assert meta["origin"]["cwd"] == "/w/beta"


def test_repair_leaves_correctly_anchored_memory_alone(tmp_path: Path) -> None:
    """A memory carrying a cross-cutting scope alongside its project
    scope is still correctly anchored — repair must not touch it. This is
    the regression that matters most: treating `keep_global` as a demote
    trigger would strip the anchor off every `projects:x`+`tools` memory
    in the store and make the leak dramatically worse."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="fine",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha", "tools"],
        origin={"cwd": "/w/alpha", "repo": _REPO_A, "worktree_root": "/w/alpha"},
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map=_MAP,
        repair=True,
        keep_global=frozenset({"tools"}),
    )

    assert report.updated == 0
    assert report.already_had_origin == 1
    assert _read_metadata(path)["origin"]["repo"] == _REPO_A


def test_repair_keep_global_suppresses_anchoring(tmp_path: Path) -> None:
    """Null repo + a cross-cutting scope: anchoring would hide a
    genuinely project-spanning memory everywhere else, so leave it
    global."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="spanning",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha", "infrastructure"],
        origin={"cwd": "/Users/me"},
    )

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map=_MAP,
        repair=True,
        keep_global=frozenset({"infrastructure"}),
    )

    assert report.updated == 0
    assert "repo" not in _read_metadata(path)["origin"]


def test_repair_will_not_anchor_an_ambiguous_memory(tmp_path: Path) -> None:
    """Two mapped scopes, null repo: no single repo is right, so
    anchoring would be a guess. Stay global."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="ambiguous",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha", "projects:beta"],
        origin={"cwd": "/Users/me/Documents"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert report.updated == 0
    assert "repo" not in _read_metadata(path)["origin"]


def test_repair_is_off_by_default(tmp_path: Path) -> None:
    """Without `repair=True` an existing origin is skipped, exactly as
    before — the flag is strictly additive."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="stray",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
        origin={"cwd": "/Users/me/Documents"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP)

    assert (report.already_had_origin, report.updated) == (1, 0)
    assert "repo" not in _read_metadata(path)["origin"]


def test_repair_dry_run_writes_nothing(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="stray",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
        origin={"cwd": "/Users/me/Documents"},
    )
    before = path.read_text(encoding="utf-8")

    report = migrate_origin_in_directory(
        memory_dir, scope_repo_map=_MAP, repair=True, dry_run=True
    )

    assert (report.updated, report.repaired_anchored) == (1, 1)
    assert path.read_text(encoding="utf-8") == before


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """Second pass finds nothing left to do — anchored memories now match
    their scope, so neither rule fires."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    _write_with_origin(
        memory_dir,
        name="stray",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
        origin={"cwd": "/Users/me/Documents"},
    )

    first = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)
    second = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert first.updated == 1
    assert second.updated == 0


def test_repair_ignores_memories_with_no_mapped_scope(tmp_path: Path) -> None:
    """No routing evidence at all — never touch it."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="unmapped",
        id_=_LEGACY_IDS[0],
        scopes=["career"],
        origin={"cwd": "/Users/me"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert report.updated == 0
    assert "repo" not in _read_metadata(path)["origin"]


def test_repair_matches_equivalent_repo_spellings(tmp_path: Path) -> None:
    """ssh vs https spelling of the same remote is NOT a mismatch —
    demoting on a spelling difference would un-anchor a correct memory."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    path = _write_with_origin(
        memory_dir,
        name="ssh-spelled",
        id_=_LEGACY_IDS[0],
        scopes=["projects:alpha"],
        origin={"cwd": "/w/alpha", "repo": "git@github.com:me/alpha.git"},
    )

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP, repair=True)

    assert report.updated == 0
    assert _read_metadata(path)["origin"]["repo"] == "git@github.com:me/alpha.git"


def test_keep_global_also_guards_the_legacy_backfill_route(tmp_path: Path) -> None:
    """A legacy memory (no origin at all) carrying a cross-cutting scope
    must not be anchored either. `keep_global` promises "never anchored to
    one repo"; honouring it only on the repair route would let the older
    backfill path quietly hide a project-spanning memory."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    legacy = _write_legacy(memory_dir, name="span", body="x", id_=_LEGACY_IDS[0])
    post = frontmatter.load(legacy)
    post.metadata["scopes"] = ["projects:alpha", "infrastructure"]
    legacy.write_text(frontmatter.dumps(post), encoding="utf-8")

    report = migrate_origin_in_directory(
        memory_dir,
        scope_repo_map=_MAP,
        repair=True,
        keep_global=frozenset({"infrastructure"}),
    )

    assert report.updated == 0
    assert "origin" not in _read_metadata(legacy)


def test_legacy_backfill_still_anchors_without_keep_global(tmp_path: Path) -> None:
    """Guard the guard: the same memory IS backfilled when the caller
    hasn't declared that scope cross-cutting."""
    memory_dir = tmp_path / ".claude-memory"
    memory_dir.mkdir()
    legacy = _write_legacy(memory_dir, name="span", body="x", id_=_LEGACY_IDS[0])
    post = frontmatter.load(legacy)
    post.metadata["scopes"] = ["projects:alpha", "infrastructure"]
    legacy.write_text(frontmatter.dumps(post), encoding="utf-8")

    report = migrate_origin_in_directory(memory_dir, scope_repo_map=_MAP)

    assert report.updated == 1
    assert _read_metadata(legacy)["origin"]["repo"] == _REPO_A
