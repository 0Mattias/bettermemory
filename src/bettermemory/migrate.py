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
from .models import SCHEMA_VERSION
from .origin import Origin, capture
from .store import (
    TOMBSTONE_DIR,
    _atomic_write_post,
    _coerce_scopes,
    _lifecycle_redump_cap,
    _lifecycle_redump_yaml_cap,
    _locked,
    _serialized_frontmatter_bytes,
)

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
        # Acquire the per-file lock for the whole read-modify-write.
        # Without this, a concurrent `Store.update` / `tombstone` /
        # `mark_verified` from a running MCP server can write its
        # version under the lock; the migrator's unlocked RMW then
        # `replace`s with the stale-body-plus-origin, silently losing
        # the in-flight edit. The lock matches the discipline every
        # other mutator in `store.py` uses — see 2.6.4 fix.
        with _locked(path):
            try:
                post = frontmatter.load(path)
            except FileNotFoundError:
                # The file vanished between the directory scan and our
                # locked read — almost always a concurrent
                # `Store.tombstone`, which writes the tombstone copy and
                # then `unlink`s the active file. That's a *valid*
                # mid-run removal, not corruption: don't pollute
                # `report.malformed` (which the CLI surfaces as "fix
                # these files"). Skip it silently — the memory still
                # lives in `.tombstones/`.
                log.debug("skipping %s: tombstoned/removed mid-migration", path)
                continue
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

            # Forward-compat gate — mirror `store._load_path` (schema_version
            # > SCHEMA_VERSION is refused). Both store load paths REFUSE to
            # load a future-schema file, deliberately, so its fields (whose
            # semantics a major bump may redefine) are never misinterpreted.
            # The migrator must honour the same gate: stamping a
            # current-semantics `origin` block into a file the reader won't
            # accept writes a v-current interpretation into a record the rest
            # of the system treats as unsupported. Leave it untouched (skip,
            # not malformed) — exactly as `load_all` leaves it out of the
            # active surface.
            raw_version = post.metadata.get("schema_version", 1)
            try:
                on_disk_version = int(raw_version)
            except (TypeError, ValueError):
                # Non-integer schema_version — `store._load_path` rejects this
                # too. Don't touch a file the reader won't load.
                continue
            if on_disk_version > SCHEMA_VERSION:
                continue

            if "origin" in post.metadata and post.metadata["origin"]:
                report.already_had_origin += 1
                continue

            # Route this memory: scope-map first, then fallback. The first
            # matching scope wins — order is determined by Python dict
            # insertion order, which is the order the caller passed flags.
            chosen: dict[str, object] | None = None
            if mapped_payloads:
                # Coerce with the SAME helper the store readers use
                # (`_coerce_scopes`), so the scopes the migrator routes by are
                # exactly the scopes the store sees. A hand-edited or torn file
                # can carry `scopes` as a scalar (`scopes: 5`) — which would
                # make the membership test `scope in <scalar>` raise TypeError,
                # aborting the whole loop from *outside* the per-file try/except
                # above and leaving every later file unmigrated — or as a dict /
                # YAML set, which the store resolves via `list(meta["scopes"])`
                # to the real scope list. The old `_load_str_list` returned []
                # for a dict/set, so the migrator saw no scopes where the store
                # saw them and silently stamped the wrong repo (F4). Do NOT
                # revert to `_load_str_list` here.
                memory_scopes = _coerce_scopes(post.metadata.get("scopes"))
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

            if dry_run:
                # Dry-run reports a "would update" count; nothing is
                # persisted, so there's no write that can fail.
                report.updated += 1
                continue

            # Use the shared `_atomic_write_post` helper: tmp+fsync+rename
            # +chmod 0o600+fsync_dir. The bare `write_bytes`+`replace`
            # pattern this code used pre-2.6.4 dropped the `0o600` chmod,
            # so post-migration files inherited the umask (typically
            # 0o644) and ended up world-readable — undoing the privacy
            # guarantee the store set on the original write.
            #
            # Mirror the read-side handling one block up: the write can
            # raise OSError (ENOSPC/EACCES/EIO mid-write or on the rename)
            # or ValueError (either dumps size cap — the flat `_MAX_YAML_BYTES`
            # / read cap, or the tighter lifecycle caps below — once `origin`
            # is appended). Without this guard a single failing file aborts
            # the whole loop with a traceback and every subsequent memory
            # goes unprocessed. Record the failure, leave the file
            # untouched (the atomic write is all-or-nothing), and continue
            # so the rest of the directory still migrates. The migration
            # is idempotent, so a later re-run picks up anything that
            # failed transiently.
            #
            # Lifecycle re-dump: this only APPENDS a small `origin` block to an
            # already-admitted, already-readable legacy record. Cap it on BOTH
            # axes with the store's shared band-keyed helpers — the same
            # dual-axis discipline `mark_verified` / `rename_scope` apply:
            #
            #   * file size — `_lifecycle_redump_cap`, keyed on `current_size`;
            #   * frontmatter YAML — `_lifecycle_redump_yaml_cap`, keyed on the
            #     record's PRISTINE serialized frontmatter (its `post.metadata`
            #     WITHOUT the `origin` block just inserted).
            #
            # The flat caps this replaces (`_MAX_WRITE_BYTES` on the file axis,
            # `_MAX_YAML_BYTES` on the YAML axis) let the backfill grow a record
            # sitting just under a cap into the reserved removal band — the
            # origin `repo` / `cwd` are caller- and environment-controlled, so
            # the appended block is unbounded from the record's point of view —
            # after which the record's own tombstone headroom was gone: the
            # exact un-removable / hard-delete chain the band discipline exists
            # to close. The YAML axis has to be capped on its own, not just the
            # file axis: a densely-`verified_paths` record can have a huge
            # file-size budget yet almost no YAML-cap room, so a file-axis cap
            # alone still let the origin block strand the record un-removable on
            # the YAML axis (its future tombstone's `removed:` line no longer
            # fits under `_MAX_YAML_BYTES` even after the dual-axis adaptive
            # trim). A band-resident legacy record still gets its origin
            # backfilled (the band arms reserve only the removal-metadata
            # budget); a record too close to either cap lands in
            # `report.malformed` below with the file untouched, and a re-run
            # after shrinking it picks it up.
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
            try:
                # Measure the pristine frontmatter WITHOUT the `origin` key
                # inserted above, so the YAML cap reserves the removal-metadata
                # budget against the record's real pre-backfill size — the exact
                # mirror of `current_size` on the file axis, and of
                # `_serialized_frontmatter_bytes(_memory_metadata(existing))` in
                # `mark_verified` / `rename_scope`. Computed inside the write
                # guard so an alias-bomb frontmatter (a hostile `sync pull` /
                # hand-edit) that makes `_serialized_frontmatter_bytes` raise
                # `ValueError` lands in `malformed` exactly as a `dumps` failure
                # would, rather than aborting the loop.
                pristine_metadata = {
                    key: value
                    for key, value in post.metadata.items()
                    if key != "origin"
                }
                current_yaml = _serialized_frontmatter_bytes(pristine_metadata)
                _atomic_write_post(
                    path,
                    post,
                    max_file_bytes=_lifecycle_redump_cap(current_size),
                    max_yaml_bytes=_lifecycle_redump_yaml_cap(current_yaml),
                )
            except (OSError, ValueError) as exc:
                log.warning("skipping file that failed to write %s: %s", path, exc)
                report.malformed.append(path)
                continue

            # Count only what actually persisted — incrementing before the
            # write would inflate `report.updated` to include files the
            # write never landed.
            report.updated += 1

    return report


def _iter_active_memory_files(memory_dir: Path) -> Iterator[Path]:
    """Yield active (non-tombstoned) `.md` files. Tombstones live in a
    sibling directory and are skipped — backfilling origin into a
    tombstone would change the on-disk audit log retroactively, which is
    not what we want."""
    if not memory_dir.exists():
        return
    for entry in memory_dir.iterdir():
        # Reject symlinks BEFORE `is_file()` (which follows them and would
        # return True for a symlink -> regular file). Memories are regular
        # files in this directory; a symlink `.md` is never one we wrote,
        # and following it would let the locked read-modify-write below
        # read — and rewrite through — an arbitrary target a hostile
        # `sync pull` planted in the memory dir. Mirrors the store
        # iterators' `not entry.is_symlink()` rejection (store.py).
        if entry.is_symlink():
            continue
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
