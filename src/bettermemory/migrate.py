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
from ._fsutil import fsync_dir, fsync_file
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
    scope_repo_map: dict[str, str] | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Backfill `origin` frontmatter on legacy memories.

    Three layered routing rules, in priority order per memory:

    1. **`scope_repo_map`** (highest priority, applied per memory):
       a mapping of scope → repo URL. If any of the memory's scopes
       appears in the map, that scope's URL wins. This is the right
       answer for global memory directories whose memories are already
       tagged with `projects:<name>` style scopes — route by tag rather
       than force-tagging everything with one repo.
    2. **`force_repo`**: if no `scope_repo_map` entry matches, every
       memory still missing origin is tagged with this URL.
    3. **`inferred`** (lowest priority): the auto-inferred Origin from
       `infer_origin_for_memory_dir`. Used when neither of the above
       gives a match. None when `memory_dir` is global and the parent
       isn't a git repo.

    Memories whose scopes don't match any map entry, when there's also
    no `force_repo` and no `inferred`, are left alone — no origin is
    written. That's the safe default for "I don't know which repo this
    came from."

    Idempotent: memories that already have an `origin` field are
    skipped. Atomic per-file: each write goes via `.tmp` + rename so a
    crash mid-migration leaves no corrupt files.
    """
    if force_repo is not None:
        # `force_repo` is a coarse override — the caller is asserting "all
        # memories here came from this repo" but doesn't know the per-memory
        # cwd. We deliberately leave cwd null rather than fabricating one
        # from `memory_dir.parent`, which for a global memory dir would
        # resolve to `~/` and would be actively misleading.
        inferred = Origin(cwd=None, repo=force_repo, branch=None)
    elif inferred is None:
        # The auto-inference path *can* set a meaningful cwd: when
        # memory_dir is project-scoped, parent IS the project root.
        inferred = infer_origin_for_memory_dir(memory_dir)

    report = MigrationReport(memory_dir=memory_dir, inferred=inferred, dry_run=dry_run)

    # Pre-compute the per-mapping origin payloads so we don't rebuild
    # the dict on every memory. Like `force_repo`, scope-mapped writes
    # leave cwd null — we know the repo, not the cwd.
    mapped_payloads: dict[str, dict[str, object]] = {}
    if scope_repo_map:
        for scope, url in scope_repo_map.items():
            mapped_payloads[scope] = Origin(cwd=None, repo=url, branch=None).model_dump(
                mode="json", exclude_none=True
            )

    fallback_payload: dict[str, object] | None = None
    if inferred is not None:
        candidate = inferred.model_dump(mode="json", exclude_none=True)
        if candidate:
            fallback_payload = candidate

    # If neither route can ever fire, we can shortcut to "nothing to do".
    if not mapped_payloads and fallback_payload is None:
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

        # Route this memory: scope-map first, then fallback. The first
        # matching scope wins — order is determined by Python dict
        # insertion order, which is the order the caller passed flags.
        chosen: dict[str, object] | None = None
        if mapped_payloads:
            memory_scopes = post.metadata.get("scopes") or []
            for scope, payload in mapped_payloads.items():
                if scope in memory_scopes:
                    chosen = payload
                    break
        if chosen is None:
            chosen = fallback_payload
        if chosen is None:
            # No rule fired for this memory — leave alone. This is the
            # common case for a global directory where the user only
            # passed `--scope-repo` for some scopes; un-routed memories
            # stay un-tagged rather than getting force-tagged with a
            # wrong URL.
            continue

        post.metadata["origin"] = dict(chosen)
        report.updated += 1

        if dry_run:
            continue

        # Mirror the durability discipline of `store._atomic_write_post`:
        # tmp.write/flush/fsync, atomic rename, fsync the parent directory.
        # Without these fsyncs a power loss between `replace` and the next
        # background flush leaves a zero-byte file at `path` — the audit
        # caught this gap because the bare `write_bytes`+`replace` pattern
        # used here predated the helper that hardened the rest of the store.
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = frontmatter.dumps(post).encode("utf-8")
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            fsync_file(f.fileno())
        tmp.replace(path)
        fsync_dir(path.parent)

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
