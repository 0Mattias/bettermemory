"""One-shot migrations for the on-disk memory format.

Memories on disk are intentionally additive — new optional fields can be
added without breaking older readers — so most schema changes don't need
a migration. The exception is when a new field would be useful retroactively,
where a one-shot backfill against the existing store is worth running.

Today there's exactly one such migration: `migrate_origin_in_directory`,
which backfills the `origin` block introduced in Phase 3. Memories written
before that phase have no origin; the auto-scope filter treats them as
global, which is *correct* but suboptimal — for a project-scoped memory
directory (sitting alongside a git repo), we can recover the repo URL
from the parent dir and stamp it on every legacy memory.

Branch is deliberately left null. We don't know the branch the memory was
originally written on, and stamping it with the *current* branch would be
misinformation. cwd is the parent of the memory directory, which is the
best stand-in we have.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import _frontmatter as frontmatter
from .origin import Origin, capture
from .store import TOMBSTONE_DIR

log = logging.getLogger("bettermemory.migrate")


# ---------------------------------------------------------------------------
# Origin inference
# ---------------------------------------------------------------------------


def infer_origin_for_memory_dir(memory_dir: Path) -> Origin | None:
    """Best-effort inference of the origin for memories in `memory_dir`.

    Returns None when nothing useful can be inferred — that's the safe
    answer for a global memory directory (`~/.claude-memory/`) that mixes
    notes from many projects. Returns a populated Origin (with `branch`
    null) when the memory directory's parent is itself a git repo.
    """
    parent = memory_dir.parent.resolve()

    # `~/.claude-memory/` and similar — parent is home, no project context.
    # Don't infer; let the caller pass `--repo` if they really want to
    # tag everything with one repo.
    if parent == Path.home().resolve():
        return None

    # Use the same git-shelling-out machinery as the live capture path
    # so behaviour matches between write-time and migration-time.
    candidate = capture(cwd=parent)
    if candidate.repo is None:
        return None

    return Origin(cwd=str(parent), repo=candidate.repo, branch=None)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@dataclass
class MigrationReport:
    """What `migrate_origin_in_directory` did or would do.

    `dry_run=True` runs produce the same report as a real run modulo the
    `updated` count being a "would update" count rather than a "did update".
    Callers should treat the dry-run path as read-only.
    """

    memory_dir: Path
    inferred: Origin | None
    dry_run: bool
    scanned: int = 0
    already_had_origin: int = 0
    updated: int = 0
    malformed: list[Path] = field(default_factory=list)


def migrate_origin_in_directory(
    memory_dir: Path,
    *,
    inferred: Origin | None = None,
    force_repo: str | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Backfill `origin` frontmatter on legacy memories.

    `inferred` (if provided) overrides automatic inference; otherwise we
    derive an Origin from `infer_origin_for_memory_dir(memory_dir)`.
    `force_repo` is a higher-priority shortcut: if given, we build an
    Origin around `(memory_dir.parent, force_repo, None)` regardless of
    whether the parent is itself a git repo.

    Idempotent: memories that already have an `origin` field are
    skipped. Atomic per-file: each write goes via `.tmp` + rename so a
    crash mid-migration leaves no corrupt files.
    """
    if force_repo is not None:
        inferred = Origin(
            cwd=str(memory_dir.parent.resolve()),
            repo=force_repo,
            branch=None,
        )
    elif inferred is None:
        inferred = infer_origin_for_memory_dir(memory_dir)

    report = MigrationReport(
        memory_dir=memory_dir, inferred=inferred, dry_run=dry_run
    )

    if inferred is None:
        # Nothing to do — no inference possible. Caller logs the why.
        return report

    origin_payload = inferred.model_dump(mode="json", exclude_none=True)
    if not origin_payload:
        # All-null origin would be useless; treat as no-op.
        return report

    for path in _iter_active_memory_files(memory_dir):
        report.scanned += 1
        try:
            post = frontmatter.load(path)
        except Exception as exc:  # noqa: BLE001 — defensive read.
            log.warning("skipping malformed file %s: %s", path, exc)
            report.malformed.append(path)
            continue

        # The vendored frontmatter parser is permissive — a file with no
        # YAML block at all loads with `metadata == {}`. That's *not* a
        # valid bettermemory memory; the store would refuse to load it
        # too. Treat the absence of `id` as the signal that this file
        # isn't ours and shouldn't be edited.
        if "id" not in post.metadata:
            log.warning(
                "skipping %s: no frontmatter `id` — not a bettermemory file",
                path,
            )
            report.malformed.append(path)
            continue

        if "origin" in post.metadata and post.metadata["origin"]:
            report.already_had_origin += 1
            continue

        post.metadata["origin"] = dict(origin_payload)
        report.updated += 1

        if dry_run:
            continue

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(frontmatter.dumps(post).encode("utf-8"))
        tmp.replace(path)

    return report


def _iter_active_memory_files(memory_dir: Path) -> Iterator[Path]:
    """Yield active (non-tombstoned) `.md` files. Tombstones live in a
    sibling directory and are skipped — backfilling origin into a
    tombstone would change the on-disk audit log retroactively, which is
    not what we want."""
    if not memory_dir.exists():
        return
    for entry in memory_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        if entry.parent.name == TOMBSTONE_DIR:
            continue
        yield entry


__all__ = [
    "MigrationReport",
    "infer_origin_for_memory_dir",
    "migrate_origin_in_directory",
]
