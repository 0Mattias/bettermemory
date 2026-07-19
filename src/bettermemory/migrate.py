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
from .origin import Origin, capture, repos_match
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
    # Repair-mode counters (`repair=True` only). `updated` stays the
    # total of everything written, so these two are a breakdown of the
    # subset that came from repairing an EXISTING origin block rather
    # than backfilling a missing one.
    repaired_anchored: int = 0
    repaired_demoted: int = 0


def plan_repair(
    existing_origin: dict[str, object],
    memory_scopes: list[str],
    scope_repo_map: dict[str, str],
    keep_global: frozenset[str],
) -> tuple[str, str | None] | None:
    """Decide how to repair an EXISTING origin block. Returns
    ``("anchor", url)``, ``("demote", None)``, or None for "leave alone".

    Repair exists because the original backfill only ever ran on
    memories with NO origin block at all. The far more common real-world
    damage is an origin that was *captured*, but captured wrong: writes
    made from a parent directory (`~/Documents`) or `$HOME` sit outside
    any git checkout, so `capture()` records a cwd with `repo=None`. Per
    `repos_match`, a null repo matches EVERY caller — the memory silently
    becomes global and leaks into every project's auto-scoped search.

    Two rules, and they move in opposite directions on purpose:

    **anchor** (tightening) — `repo` is null and the memory's scopes name
    exactly one repo in `scope_repo_map`. Evidence is unambiguous, so
    adopt it. Guarded by `keep_global`: a memory that also carries a
    cross-cutting scope (`infrastructure`, `tools`, …) is genuinely
    project-spanning, and anchoring it would HIDE it everywhere else.
    `keep_global` only ever suppresses anchoring — it must never trigger
    a demote, or every correctly-anchored `projects:x`+`workflow` memory
    in the store would be stripped back to global, which is the exact
    opposite of the leak this repairs.

    **demote** (loosening) — `repo` is set, but the memory carries a
    mapped scope whose repo does NOT match it. The memory claims to
    belong to a project it is invisible from: scoped `projects:homelab`
    yet anchored to the Verendo checkout it happened to be written in.
    No single repo can satisfy a memory spanning two projects, so the
    honest origin is global. Clears `worktree_root` alongside `repo`
    because the worktree filter is the second auto-scope discriminator —
    leaving a stale root behind would keep the memory dark.

    Note the asymmetry in confidence: anchoring makes a memory *less*
    visible and so demands unambiguous evidence (exactly one repo, no
    cross-cutting scope), while demoting makes it *more* visible and is
    safe on any genuine mismatch.
    """
    if not scope_repo_map:
        return None
    # Route by the STRING scopes only. Both lookups below hash their
    # operand (`scope in <dict>`, `set(...) & keep_global`), and
    # `_coerce_scopes` passes list elements through verbatim — so a torn
    # record whose `scopes` list holds an unhashable element (`scopes: [[a]]`)
    # would raise TypeError from inside the plan. `migrate_origin_in_directory`
    # already screens those out via `_routable_scopes` and reports them as
    # skipped; this filter is the same guarantee for direct callers of the
    # exported `plan_repair`, so planning can never abort on one bad record.
    routable = [scope for scope in memory_scopes if isinstance(scope, str)]
    implied = {scope_repo_map[scope] for scope in routable if scope in scope_repo_map}
    if not implied:
        return None

    recorded = existing_origin.get("repo")
    if not recorded or not isinstance(recorded, str):
        if len(implied) == 1 and not (set(routable) & keep_global):
            return ("anchor", next(iter(implied)))
        return None

    if any(not repos_match(recorded, candidate) for candidate in implied):
        return ("demote", None)
    return None


def _routable_scopes(
    post: "frontmatter.Post", path: Path, report: MigrationReport
) -> list[str] | None:
    """Resolve a record's `scopes` to the scope STRINGS the migrator may
    route by. Returns None — after recording `path` in `report.malformed`
    — when the value carries an element the store itself would refuse.

    `_coerce_scopes` is deliberately shape-tolerant, and for a list value
    it passes the elements through verbatim (its docstring: "elements
    as-is"; the model validator is what rejects a non-string element).
    So a hand-edited or torn record can hand the migrator
    `scopes: [[projects:alpha]]` — a list whose element is itself a list.
    Every anchoring site downstream HASHES those elements
    (`scope in scope_repo_map` inside `plan_repair`, `set(scopes) &
    keep_global` in the guards), and an unhashable element raises
    TypeError from OUTSIDE the per-file try/except around the read —
    aborting the entire run mid-plan. For a bulk mutation whose safety
    story is "enumerate every action, then apply", one malformed record
    must cost one record, not the plan.

    Returning None rather than silently dropping the bad elements is
    deliberate: a non-string scope makes the record unloadable by the
    store, so it is malformed — not partially routable. Reporting it
    keeps the printed summary honest instead of quietly routing a record
    by half its scopes.
    """
    scopes = _coerce_scopes(post.metadata.get("scopes"))
    if any(not isinstance(scope, str) for scope in scopes):
        log.warning(
            "skipping %s (id=%s): `scopes` carries a non-string element — "
            "the record cannot be routed",
            path,
            post.metadata.get("id"),
        )
        report.malformed.append(path)
        return None
    return scopes


def migrate_origin_in_directory(
    memory_dir: Path,
    *,
    inferred: Origin | None = None,
    force_repo: str | None = None,
    scope_repo_map: dict[str, str] | None = None,
    repair: bool = False,
    keep_global: frozenset[str] | None = None,
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

    `repair=True` lifts exactly that skip: memories WITH an origin block
    are additionally run through `plan_repair`, which fixes an origin
    that was captured wrong (typically `repo=None` from a write made
    outside any checkout) rather than one that was never captured at
    all. `keep_global` is the set of cross-cutting scopes that must
    never be anchored to a single repo.

    The two flags take effect at DIFFERENT points, and only `repair` is
    gated on itself:

    * `repair` is inert when False: a memory that already carries an
      origin is skipped exactly as before, `plan_repair` is never
      consulted, and the default path is byte-for-byte the old
      behaviour.
    * `keep_global` is NOT gated on `repair`. It guards every route that
      can anchor a memory to one repo — including the legacy backfill of
      memories with NO origin block, which is precisely the
      `repair=False` path. It is inert only when EMPTY, which is its
      default. Passing a non-empty `keep_global` with `repair=False`
      does change the run: a legacy memory carrying one of those scopes
      is left un-backfilled rather than anchored. (The CLI additionally
      refuses `--keep-global` without `--repair`; that is a CLI-level
      pairing rule, not a property of this function.)

    Repair still only ever rewrites the `origin` block — the body, id,
    and every other frontmatter key are untouched.
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

            existing_origin = post.metadata.get("origin")
            if existing_origin:
                # Default path: an origin block means this memory is done.
                if not repair or not isinstance(existing_origin, dict):
                    report.already_had_origin += 1
                    continue
                # Screen the scopes BEFORE handing them to `plan_repair`,
                # whose routing hashes every element. A record whose
                # `scopes` list carries an unhashable element is reported
                # as skipped (with its id, via the helper's log line) and
                # the run keeps planning the rest — one bad record must
                # not abort a bulk mutation mid-plan.
                memory_scopes = _routable_scopes(post, path, report)
                if memory_scopes is None:
                    continue
                plan = plan_repair(
                    existing_origin,
                    memory_scopes,
                    scope_repo_map or {},
                    keep_global or frozenset(),
                )
                if plan is None:
                    report.already_had_origin += 1
                    continue
                action, repaired_repo = plan
                # Mutate a COPY: on the dry-run branch below we must not
                # leave the caller's parsed metadata altered, and on the
                # write branch `pristine_metadata` re-reads `post.metadata`
                # to size the YAML cap against the pre-repair record.
                repaired = dict(existing_origin)
                if action == "anchor":
                    repaired["repo"] = repaired_repo
                else:
                    # Drop rather than null out: `Origin` serializes with
                    # `exclude_none`, so a null key would be a shape the
                    # rest of the store never writes.
                    repaired.pop("repo", None)
                    repaired.pop("worktree_root", None)
                post.metadata["origin"] = repaired
                # Attempt the write BEFORE touching any counter. These two
                # breakdown counters used to be bumped up in the branches
                # above, so a failed write left them inflated while
                # `report.updated` (correctly) did not move — the printed
                # summary then overstated what actually landed. The counts
                # are what a user eyeballs to decide whether a bulk
                # mutation over their whole store did what the dry run
                # promised, so they have to mean "persisted", exactly like
                # `updated` and like the backfill route below.
                #
                # Dry run persists nothing, so there is no write that can
                # fail and the planned action IS the outcome; it falls
                # through to the same three increments. That is what keeps
                # "the dry-run action list equals what apply does" true on
                # the success path — one increment site, both modes.
                if not dry_run and not _write_repaired(path, post, report):
                    continue
                if action == "anchor":
                    report.repaired_anchored += 1
                else:
                    report.repaired_demoted += 1
                report.updated += 1
                continue

            # `keep_global` guards BOTH anchoring routes, not just repair's.
            # A legacy memory carrying a cross-cutting scope is exactly as
            # project-spanning as a repaired one, and tagging it here would
            # hide it from every other project — the same damage the repair
            # rule refuses to do, arrived at down a different code path. The
            # flag's contract is "never anchored to one repo", so it has to
            # hold wherever an anchor can be applied. Inert by default:
            # `keep_global` is empty unless the caller passed it, and the CLI
            # only accepts it alongside `--repair`.
            #
            # `set(...)` hashes every scope element, so this guard has the
            # same unhashable-element abort the repair route had — screen
            # through `_routable_scopes` first. Only reached when the caller
            # passed a non-empty `keep_global`, so the default backfill run
            # neither calls the helper nor gains a new `malformed` entry.
            if keep_global:
                guard_scopes = _routable_scopes(post, path, report)
                if guard_scopes is None:
                    continue
                if set(guard_scopes) & keep_global:
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


def _write_repaired(
    path: Path, post: "frontmatter.Post", report: MigrationReport
) -> bool:
    """Persist a repaired record. True on success; on failure records
    `path` in `report.malformed` and returns False so the caller skips it
    and the rest of the directory still migrates.

    Mirrors the backfill write below it — same atomic tmp+fsync+rename,
    same 0o600, same dual-axis lifecycle caps measured against the
    record's frontmatter WITHOUT `origin`. Sizing against the pristine
    frontmatter is deliberately conservative here: a repair rewrites an
    origin block that already existed, so the true pre-write baseline is
    slightly larger than what we measure, and a tighter cap can only
    refuse a borderline record — never let one through into the reserved
    removal band.
    """
    try:
        current_size = path.stat().st_size
    except OSError:
        current_size = 0
    try:
        pristine_metadata = {
            key: value for key, value in post.metadata.items() if key != "origin"
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
        return False
    return True


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
    "plan_repair",
]
