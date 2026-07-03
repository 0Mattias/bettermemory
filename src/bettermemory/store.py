"""Filesystem operations for bettermemory.

Pure file I/O — no search logic, no MCP awareness. The store owns the layout
of the memory directory and the on-disk format; callers pass in `Memory`
objects and get them back.
"""

from __future__ import annotations

import errno
import logging as _logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import yaml

from . import _frontmatter as frontmatter
from ._decorators import best_effort
from ._fsutil import atomic_write_bytes, flock_excl, fsync_dir

# We use a vendored frontmatter parser (`_frontmatter.py`) which pins the
# pure-Python yaml.SafeLoader / yaml.SafeDumper. Two reasons:
#
# 1. CSafeDumper has a state-machine bug that surfaces under coverage
#    instrumentation: it raises `EmitterError: expected SCALAR, ...` when
#    coverage filters by a specific submodule (e.g. `--cov=bettermemory.store`).
#    Pure-Python yaml is unaffected.
# 2. `python-frontmatter` 1.1.0 (current release) calls `codecs.open()`,
#    which Python 3.14 emits a DeprecationWarning for. The library is
#    effectively unmaintained. Vendoring is shorter than living with the
#    warning or shimming around it.
#
# Memory frontmatter is dozens of bytes per write; the libyaml speedup is
# irrelevant here. Robustness wins.


from .models import (
    SCHEMA_VERSION,
    Category,
    Confidence,
    Memory,
    MemoryLink,
    MemorySummary,
    Source,
    TombstonedMemory,
    TombstonedSummary,
    build_filename,
    first_summary_line,
    generate_ulid,
    is_valid_ulid,
    make_slug,
    utcnow,
)
from .origin import Origin


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MemoryNotFoundError(KeyError):
    """No active memory with that ID."""


class TombstonedError(KeyError):
    """ID exists but the memory is tombstoned."""


class NotTombstonedError(KeyError):
    """`restore` was called on an active memory, not a tombstone."""


class ConcurrentUpdateError(Exception):
    """`Store.update` saw a different `updated` on disk than the caller's
    snapshot. The caller's edit was built on top of a now-stale read; the
    write was refused rather than silently clobbering whoever bumped the
    record in the interim.

    `current_updated` is the on-disk `updated` timestamp at the moment the
    CAS check failed — the caller should re-load via `Store.load_one` (or
    `memory_show` at the handler boundary), rebase the edit on top, and
    retry. The store retains the prior writer's change; this exception
    means "your snapshot is older than the file, retry on top," not
    "your write was lost."

    Not a subclass of `KeyError` (unlike the sibling errors above) because
    the record IS still findable by id — the failure mode is "stale
    snapshot," not "id gone." Subclassing `Exception` keeps a
    `try: ... except KeyError:` block from accidentally swallowing this
    one as if it were a not-found case.
    """

    def __init__(self, memory_id: str, current_updated: datetime) -> None:
        self.memory_id = memory_id
        self.current_updated = current_updated
        super().__init__(
            f"memory {memory_id} was updated concurrently "
            f"(your snapshot is stale; on-disk updated={current_updated.isoformat()})"
        )


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------
#
# `_locked` is the local alias for the canonical fcntl-based exclusive
# flock in `_fsutil.flock_excl`. Re-exported as `_locked` here so the
# rest of `store.py` keeps reading naturally (`with _locked(path):`).
# Single source of truth: a future fix to the locking discipline lands
# in `_fsutil.flock_excl` and applies to events.py and sync.py too —
# see the 2.6.3 pattern-generalization audit note.
#
# Top-level assignment (not `import flock_excl as _locked`) so mypy strict's
# no_implicit_reexport rule accepts external imports of `_locked` here —
# `migrate.py` and the locking tests reach into this module by name.
_locked = flock_excl


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


TOMBSTONE_DIR = ".tombstones"


@dataclass
class Store:
    """A memory store rooted at a single directory.

    Layout:
        <root>/2025-03-14-<slug>.md
        <root>/.tombstones/<original-filename>.tombstone.md
    """

    root: Path

    # ---- lifecycle --------------------------------------------------------

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Explicit 0o700 — don't rely on the caller's umask. Tombstones
        # carry the same trust boundary as active memories (paths cited
        # in `removed_reason`, body hashes for dedup), so directory-listing
        # them should require the owner just like the active store.
        (self.root / TOMBSTONE_DIR).mkdir(mode=0o700, exist_ok=True)
        # Schema-upgrade auto-heal, BEFORE the divergence check: when a
        # SCHEMA_VERSION bump emptied the index (`meta.needs_rebuild`),
        # rebuild it from the canonical .md files now, so the migration
        # resolves as an INFO note instead of the S4 WARNING below.
        _rebuild_index_if_flagged(self)
        # S4: one-shot startup divergence check. The FTS5 index is a
        # derived cache kept consistent with disk only via Store hooks
        # (`_index_upsert_quietly` / `_index_remove_quietly` under the
        # per-file flock in every mutator). Any code path that writes
        # `.md` files directly — an external editor, `sync pull`, a
        # sub-agent using the generic `Write` tool on a memory file
        # path instead of `memory_write` — leaves the index stale with
        # no warning. `memory_search` then ranks against stale
        # candidate ids and `filenames_for_ids` returns paths that may
        # not exist. The warning surfaces the divergence at the first
        # opportunity so the user can run `bettermemory reindex`
        # before it cascades into a wrong answer.
        _warn_on_index_divergence(self.root)

    @property
    def tombstone_dir(self) -> Path:
        return self.root / TOMBSTONE_DIR

    # ---- iteration --------------------------------------------------------

    def _iter_active_paths(self) -> Iterator[Path]:
        # `is_file()` follows symlinks; we explicitly reject them. With
        # `sync pull`, the memory directory is a worktree that a remote
        # can push to — a hostile remote pushing `something.md` that's
        # a symlink to `/etc/passwd` (or any other readable file) would
        # otherwise have its target loaded and parsed as frontmatter on
        # the next `load_all`. The parse would fail and `load_all` would
        # swallow it, so this isn't an exfiltration primitive today, but
        # the narrower contract — memories are regular files in this
        # directory, full stop — is what we want to enforce.
        for entry in self.root.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
                yield entry

    def _iter_tombstone_paths(self) -> Iterator[Path]:
        if not self.tombstone_dir.exists():
            return
        # Same symlink-rejection rule as `_iter_active_paths`.
        for entry in self.tombstone_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
                yield entry

    # ---- read -------------------------------------------------------------

    def load_all(self) -> list[Memory]:
        """All active (non-tombstoned) memories. Sort by `created` desc.

        Defensive against three failure modes:
        - **Malformed file** (ValueError, KeyError): skip and continue.
          Better to operate on the rest of the store than refuse to
          start because of one bad memory.
        - **Concurrent tombstone race** (FileNotFoundError): skip and
          continue. `_iter_active_paths` lists the dir, then `_load_path`
          opens each file; another writer can move a file to
          `.tombstones/` in between. The right answer is to act as if
          we'd listed the dir one moment later, not to crash whatever
          callable triggered the load (memory_search, memory_list,
          memory_health all call this).
        - **Other I/O errors** (PermissionError, etc.): skip too. A
          single inaccessible file shouldn't blind the rest of the
          store; the OS-level cause is logged via the file's absence
          from the result, and a fresh load picks up changes.
        """
        memories: list[Memory] = []
        for path in self._iter_active_paths():
            try:
                memories.append(self._load_path(path))
            except (ValueError, KeyError, OSError):
                continue
        memories.sort(key=lambda m: m.created, reverse=True)
        return memories

    def iter_active(self) -> Iterator[tuple[Path, Memory]]:
        """`(path, memory)` pairs for every active (non-tombstoned)
        memory. Skips malformed / racing files like `load_all` does,
        on ANY per-file exception rather than `load_all`'s tuple.

        Use this when the on-disk filename matters to the caller —
        notably `index.rebuild`, which needs the actual filename (not
        a re-derived one) so the `filename` column points at
        collision-suffixed files correctly."""
        for path in self._iter_active_paths():
            try:
                memory = self._load_path(path)
            except Exception:  # noqa: BLE001
                # Deliberately wider than `load_all`'s (ValueError,
                # KeyError, OSError): `_parse_memory_file` can raise
                # outside that tuple on adversarial-but-valid YAML —
                # `scopes: 5` makes `list(meta["scopes"])` raise
                # TypeError — and this iterator must skip exactly the
                # files `count_unparseable_memory_files` counts, or the
                # parse-aware divergence arithmetic (the S4 warning,
                # doctor's index_health) reports gaps a rebuild can
                # never clear.
                continue
            yield path, memory

    def list_summaries(self, scopes: list[str] | None = None) -> list[MemorySummary]:
        """Like `load_all` but body-stripped, filtered by scope match."""
        out: list[MemorySummary] = []
        for memory in self.load_all():
            if scopes and not _scope_intersect(memory.scopes, scopes):
                continue
            out.append(
                MemorySummary(
                    id=memory.id,
                    scopes=memory.scopes,
                    confidence=memory.confidence,
                    summary=first_summary_line(memory.body),
                    created=memory.created,
                    updated=memory.updated,
                    last_verified_at=memory.last_verified_at,
                    category=memory.category,
                )
            )
        return out

    def load_one(self, memory_id: str) -> Memory:
        """Load one memory by ID. Raises if missing or tombstoned."""
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")

        for path in self._iter_active_paths():
            try:
                memory = self._load_path(path)
            except (ValueError, KeyError):
                continue
            if memory.id == memory_id:
                return memory

        # If it's tombstoned, give a clearer error so the model can say so.
        # Match the discipline `load_tombstones` (store.py:583) uses for the
        # bulk reader: one corrupt/truncated tombstone or a race with
        # `prune_tombstones` (FileNotFoundError) must not crash the whole
        # callsite. `yaml.YAMLError` is redundant after the `_frontmatter`
        # boundary fix translates it to ValueError, but we list it
        # explicitly as defense-in-depth + signal to future readers.
        for path in self._iter_tombstone_paths():
            try:
                post = frontmatter.load(path)
            except (FileNotFoundError, ValueError, KeyError, OSError, yaml.YAMLError):
                continue
            if post.metadata.get("id") == memory_id:
                raise TombstonedError(
                    f"memory {memory_id} was removed: "
                    f"{post.metadata.get('removed_reason', '<no reason>')}"
                )

        raise MemoryNotFoundError(f"no memory with id {memory_id}")

    def show(self, memory_id: str) -> Memory:
        """Public alias matching the MCP `memory_show` tool name."""
        return self.load_one(memory_id)

    def _load_path(self, path: Path) -> Memory:
        # Instance hook over the module-level parser: kept as a method so
        # tests can monkeypatch per-Store read behavior (the CAS races in
        # test_concurrency), while Store-free callers
        # (`count_unparseable_memory_files`) share the exact same parse
        # semantics via `_parse_memory_file`.
        return _parse_memory_file(path)

    # ---- write ------------------------------------------------------------

    def write(
        self,
        *,
        content: str,
        scopes: list[str],
        confidence: Confidence = Confidence.MEDIUM,
        source: Source = Source.EXPLICIT,
        origin: Origin | None = None,
        category: Category | None = None,
    ) -> Memory:
        """Create a new memory. Generates ID, slug, filename.

        `category` is persisted on the record. Legacy callers that don't
        pass it land with `category=None`, which the runtime treats as
        the fact-default — same behavior as memories written before the
        field existed.
        """
        now = utcnow()
        memory = Memory(
            id=generate_ulid(),
            created=now,
            updated=now,
            scopes=list(scopes),
            confidence=confidence,
            source=source,
            body=content.strip() + "\n",
            origin=origin,
            category=category,
        )
        path = self._path_for(memory)
        with _locked(path):
            self._write_path(path, memory)
            # perf: index upsert under lock is intentional — see audit
            # H1. Two concurrent updates on the same id used to release
            # the file lock in order A→B, but their SQLite upserts
            # could still interleave so the index ended up with A's
            # body while disk had B's. The SQLite serialization
            # overhead is worth it: stale FTS5 ranking quietly
            # misleads `memory_search`, and the file-lock cost is
            # bounded (we're already holding it through `_write_path`).
            _index_upsert_quietly(self.root, memory, filename=path.name)
        return memory

    def update(
        self,
        memory: Memory,
        *,
        force: bool = False,
        preserve_verification: bool = False,
    ) -> Memory:
        """Overwrite an existing memory in place; bump `updated`.

        Optimistic concurrency (W2): the caller's `memory.updated` is the
        snapshot timestamp they READ when they built this edit (via
        `load_one(id).updated`). Under the lock, after the C2 recheck,
        we re-load the current Memory from disk and compare its `updated`
        to the caller's. On mismatch we raise `ConcurrentUpdateError` so
        the caller can re-fetch and retry on top of the current snapshot
        rather than silently clobbering whoever bumped the record in the
        interim. The two-agent disjoint-edit race that previously dropped
        one write now surfaces as a structured retry signal.

        `force=True` is a low-level escape hatch for callers who legitimately
        want to overwrite without the CAS — e.g. migration tooling that has
        already reconciled concurrent edits out-of-band. Not exposed through
        the MCP handler boundary; reach for it from in-process code only.

        `preserve_verification=True` keeps the on-disk `last_verified_at` and
        `verified_*` lists instead of the caller's snapshot copy. The
        metadata-update handler passes it so a metadata-only edit cannot
        clobber a `mark_verified` that landed concurrently: verify bumps
        `last_verified_at` but NOT `updated`, so the `updated` CAS alone
        wouldn't catch it. Content edits leave it False — they reset
        verification on purpose (the attested body no longer exists).

        Raises:
            MemoryNotFoundError: no active record with that id, or the file
              was tombstoned/renamed concurrently (by a parallel `tombstone`
              or any other path-moving mutator) between the find walk and
              the under-lock recheck.
            ConcurrentUpdateError: a parallel `update` landed between the
              caller's snapshot read and our CAS — the on-disk `updated`
              no longer matches `memory.updated`. The caller should re-fetch
              via `memory_show` and retry on top of the current snapshot.
              Not raised when `force=True`.
            OSError: a genuine disk-level failure in the atomic write path
              (`_write_path` → `_atomic_write_post`): EIO mid-write, ENOSPC
              on the tmp write or rename, EACCES on the directory. The MCP
              handler boundary translates this to a structured `ValueError`.
        """
        existing_path = self._find_path_for_id(memory.id)
        if existing_path is None:
            raise MemoryNotFoundError(f"no memory with id {memory.id}")

        now = utcnow()
        new_memory = memory.model_copy(update={"updated": now})
        with _locked(existing_path):
            # Re-verify the path under the lock. `_find_path_for_id`
            # above walked the directory unlocked, so a concurrent
            # `tombstone()` could have moved the file into
            # `.tombstones/` between the find and this lock; without
            # the recheck, our write would resurrect the tombstoned
            # memory by re-creating an active file at the original
            # path — silently overriding the removal and leaving the
            # tombstone orphaned in `.tombstones/`.
            #
            # Cheapest correct check: the file still exists AND its id
            # frontmatter still matches. If either fails, the path no
            # longer represents the same logical memory and the caller
            # must retry through `_find_path_for_id` (or accept the
            # tombstone). We surface MemoryNotFoundError rather than a
            # custom race-flag — the calling tool layer (`memory_update`)
            # treats it the same way as the find-time miss above.
            if not _id_still_at_path(existing_path, memory.id):
                raise MemoryNotFoundError(
                    f"no memory with id {memory.id} (raced with "
                    f"concurrent tombstone or rename)"
                )
            # W2: optimistic-concurrency CAS. Re-load under the lock and
            # confirm the on-disk `updated` matches the caller's snapshot.
            # If a second agent landed an update between the caller's
            # read and our lock, the on-disk timestamp will have moved
            # forward and our write would silently drop that agent's
            # change. Refuse with `ConcurrentUpdateError` so the caller
            # rebases. `_load_path` performs the same `_as_dt` UTC
            # coercion the caller's `load_one`/`load_all` path does, so
            # the comparison is between two aware datetimes in the same
            # tz; equality compares to the microsecond, which is the
            # resolution `utcnow()` writes.
            # Load the current on-disk record when needed: for the CAS
            # (not force) and/or to preserve verification fields below.
            current = (
                self._load_path(existing_path)
                if (not force or preserve_verification)
                else None
            )
            if not force and current is not None and current.updated != memory.updated:
                raise ConcurrentUpdateError(memory.id, current.updated)
            if preserve_verification and current is not None:
                # Cross-axis race: `mark_verified` bumps `last_verified_at`
                # (and the verified_* lists) but NOT `updated`, so a verify
                # that lands between the caller's snapshot read and this lock
                # passes the `updated` CAS above — yet the caller's
                # `new_memory` still carries the STALE pre-verify
                # verification and would silently clobber the attestation.
                # Metadata-only updates pass preserve_verification=True to
                # keep the freshest on-disk verification instead of the
                # caller's snapshot.
                new_memory = new_memory.model_copy(
                    update={
                        "last_verified_at": current.last_verified_at,
                        "verified_paths": list(current.verified_paths),
                        "verified_commits": list(current.verified_commits),
                        "verified_versions": list(current.verified_versions),
                        "verified_absent_paths": list(current.verified_absent_paths),
                    }
                )
            self._write_path(existing_path, new_memory)
            # perf: index upsert under lock is intentional — see audit H1.
            _index_upsert_quietly(self.root, new_memory, filename=existing_path.name)
        return new_memory

    def mark_verified(
        self,
        memory_id: str,
        *,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
        verified_absent_paths: list[str] | None = None,
        expected_last_verified_at: datetime | None = None,
        check_expected: bool = False,
    ) -> Memory:
        """Bump `last_verified_at` to now without touching `updated`.

        Verification is the orthogonal axis to content edits: a typo fix
        bumps `updated` (the body changed) but not `last_verified_at`
        (no claim to have spot-checked reality). A `memory_verify` call
        bumps `last_verified_at` (a human/agent confirmed reality matched
        the body) without bumping `updated` (the body itself didn't move).
        Calling this on a memory that's already verified-now is a no-op
        from the caller's perspective — the timestamp just slides forward.

        `verified_paths` / `verified_commits` / `verified_versions` /
        `verified_absent_paths` carry the structured claims the caller
        attested (`verified_absent_paths` being the mirror axis: paths
        confirmed *intentionally* absent on this machine, excluded from
        path-drift's `missing`). Passing None preserves whatever was
        previously stored (so a no-arg `mark_verified` keeps the prior
        attestation list); passing an explicit `[]` clears it. Passing a
        populated list replaces the prior list — verification is
        per-event, not append-only, and the event log is the audit
        trail for the history.

        Optimistic concurrency (W8): when `check_expected=True`, the
        caller's `expected_last_verified_at` is the snapshot value they
        READ when they decided to attest (via `load_one(id).last_verified_at`).
        Under the lock, after the C2 recheck, we compare the on-disk
        `last_verified_at` to the caller's snapshot. On mismatch we raise
        `ConcurrentUpdateError` so the caller can re-fetch, reassess
        their attestation against the now-current state, and retry —
        rather than silently clobbering whoever attested in the interim.
        REPLACE semantics for `verified_*` lists makes this race especially
        nasty: agent A attesting path #1 and agent B attesting path #2
        simultaneously would otherwise lose one of the attestations, and
        the contract is "reread + reattest," not "silent merge."

        `check_expected=False` (the default) is the back-compat escape
        hatch for callers that don't have a snapshot — e.g. the web UI
        verify form, the legacy direct-store callers in tests, the
        no-arg slide-the-timestamp-forward use case. Not exposed through
        the MCP `memory_verify` handler boundary; that handler always
        loads its snapshot first and opts in.

        Raises:
            MemoryNotFoundError: no active or tombstoned record with that
              id, or the active file was tombstoned/renamed concurrently
              between the find walk and the under-lock recheck.
            TombstonedError: id resolves to an existing tombstone — the
              caller should restore before attesting, or attest something
              else.
            ConcurrentUpdateError: only raised when `check_expected=True`;
              fires when a parallel `mark_verified` landed between the
              caller's snapshot read and our CAS, so the on-disk
              `last_verified_at` no longer matches
              `expected_last_verified_at`. The caller should re-fetch via
              `memory_show`, reassess the attestation against the
              now-current `verified_*` lists, and retry.
            OSError: a genuine disk-level failure in the atomic write path
              (`_write_path` → `_atomic_write_post`): EIO mid-write, ENOSPC
              on the tmp write or rename, EACCES on the directory. The MCP
              handler boundary translates this to a structured `ValueError`.
        """
        existing_path = self._find_path_for_id(memory_id)
        if existing_path is None:
            # Tombstone scan must tolerate corrupt/racing entries — same
            # defensive pattern as `load_tombstones` (store.py:583).
            # See `load_one` for the rationale on the explicit
            # `yaml.YAMLError` + `FileNotFoundError` in the catch tuple.
            for tpath in self._iter_tombstone_paths():
                try:
                    post = frontmatter.load(tpath)
                except (
                    FileNotFoundError,
                    ValueError,
                    KeyError,
                    OSError,
                    yaml.YAMLError,
                ):
                    continue
                if post.metadata.get("id") == memory_id:
                    raise TombstonedError(
                        f"memory {memory_id} was removed: "
                        f"{post.metadata.get('removed_reason', '<no reason>')}"
                    )
            raise MemoryNotFoundError(f"no memory with id {memory_id}")

        # Read-modify-write must happen under the same lock. Reading
        # outside the lock and writing inside it leaves a window where
        # a concurrent `update` / `tombstone` can land in between; our
        # write would then overwrite that change with the stale body
        # plus the new `last_verified_at`. Cheap to hold the lock during
        # the read — the file is the same one we're about to write.
        with _locked(existing_path):
            # Same C2 recheck as `update`: `_find_path_for_id` walked
            # unlocked, so a concurrent `tombstone()` may have moved
            # the file away between the find and this lock. Without
            # the recheck, our `_load_path` would raise
            # FileNotFoundError (passing through as OSError) or, worse,
            # the path could have been reused for a different memory
            # via a same-slug write — in which case we'd corrupt that
            # memory with a `last_verified_at` claim from a different
            # id. Recheck before the load.
            if not _id_still_at_path(existing_path, memory_id):
                raise MemoryNotFoundError(
                    f"no memory with id {memory_id} (raced with "
                    f"concurrent tombstone or rename)"
                )
            existing = self._load_path(existing_path)
            # W8: optimistic-concurrency CAS on `last_verified_at`. The
            # field that moves on every successful `mark_verified` is
            # `last_verified_at` (not `updated` — verification doesn't
            # bump `updated` by design), so it's the cheapest correct
            # fingerprint for detecting a concurrent attestation. Mirror
            # of W2 in shape: compare the on-disk snapshot fingerprint
            # to the caller's; on mismatch raise `ConcurrentUpdateError`
            # with the on-disk `updated` so the caller's rebase action
            # is identical to the W2 retry flow (re-fetch via
            # `memory_show`, retry on top). The on-disk `updated` is
            # used as the error's `current_updated` payload to keep the
            # exception's contract uniform with W2 — what the caller
            # needs is "something changed, re-fetch," not the specific
            # field that moved.
            if (
                check_expected
                and existing.last_verified_at != expected_last_verified_at
            ):
                raise ConcurrentUpdateError(memory_id, existing.updated)
            update: dict[str, object] = {"last_verified_at": utcnow()}
            if verified_paths is not None:
                update["verified_paths"] = list(verified_paths)
            if verified_commits is not None:
                update["verified_commits"] = list(verified_commits)
            if verified_versions is not None:
                update["verified_versions"] = list(verified_versions)
            if verified_absent_paths is not None:
                update["verified_absent_paths"] = list(verified_absent_paths)
            new_memory = existing.model_copy(update=update)
            self._write_path(existing_path, new_memory)
            # perf: index upsert under lock is intentional — see audit H1.
            _index_upsert_quietly(self.root, new_memory, filename=existing_path.name)
        return new_memory

    def tombstone(
        self,
        memory_id: str,
        reason: str,
        *,
        session_id: str | None = None,
    ) -> Path:
        """Move a memory to `.tombstones/`, adding removal frontmatter.

        `session_id` is captured into the tombstone frontmatter so the
        removal can be joined to the session that produced it without
        consulting the event log. The event log is still the canonical
        audit trail, but log archives can be pruned independently of
        the tombstone files; recording the session id on the file
        itself keeps the link durable across log rotation.

        Tombstones written before this field shipped have no
        `removed_session` and load with `None` in that slot — the
        join is unavailable for legacy entries but the rest of the
        record is intact.

        Raises:
            MemoryNotFoundError: no record (active or tombstoned) with
              that id.
            TombstonedError: id is already tombstoned — either the
              pre-lock active find missed and the tombstone scan found
              it, or a parallel `tombstone()` won the race and moved the
              file to `.tombstones/` between the find walk and the
              under-lock recheck.
            OSError: a genuine disk-level failure in the tombstone write
              (`_atomic_write_post`) or the source unlink — EIO mid-write,
              ENOSPC on the rename, EACCES on the unlink. The benign
              ENOENT-on-unlink race is swallowed; everything else
              propagates. `memory_remove` catches this and translates it
              to a structured `ValueError`.
        """
        path = self._find_path_for_id(memory_id)
        if path is None:
            # Maybe it's already tombstoned — bubble up a clearer error.
            # Tombstone scan must tolerate corrupt/racing entries — same
            # defensive pattern as `load_tombstones` (store.py:583).
            # See `load_one` for the rationale on the explicit
            # `yaml.YAMLError` + `FileNotFoundError` in the catch tuple.
            for tpath in self._iter_tombstone_paths():
                try:
                    post = frontmatter.load(tpath)
                except (
                    FileNotFoundError,
                    ValueError,
                    KeyError,
                    OSError,
                    yaml.YAMLError,
                ):
                    continue
                if post.metadata.get("id") == memory_id:
                    raise TombstonedError(
                        f"memory {memory_id} is already tombstoned "
                        f"(raced with concurrent tombstone)"
                    )
            raise MemoryNotFoundError(f"no memory with id {memory_id}")

        # Unconditionally include the ULID in the tombstone filename so
        # the name is unique by construction. Pre-2.6.4 the code picked
        # the unsuffixed `{path.stem}.tombstone.md` when the file
        # didn't yet exist and added the ULID only on collision — a
        # TOCTOU: two concurrent `tombstone()` calls on different
        # memories with the same `path.stem` (rare but possible when
        # slugs collide) both saw `target.exists() == False` and both
        # picked the unsuffixed name, with the second clobbering the
        # first's tombstone via `tmp.replace`. Always-suffixed kills
        # the race. Existing unsuffixed tombstones on disk continue to
        # load — the reader keys off the `id` field, not the filename.
        target = self.tombstone_dir / f"{path.stem}.{memory_id}.tombstone.md"

        # Read + write under the same lock. Reading the body outside the
        # lock and writing the tombstone inside it leaves a window where
        # a concurrent `update` can land in between; we'd then write a
        # tombstone containing the pre-update body, losing the in-flight
        # edit silently.
        with _locked(path):
            # Same C2 recheck as `update` / `mark_verified`:
            # `_find_path_for_id` above walked the directory unlocked, so
            # a concurrent `tombstone()` may have moved the file into
            # `.tombstones/` between the find and this lock. Without the
            # recheck, `frontmatter.load(path)` raises a bare
            # `FileNotFoundError` that escapes the handler layer as a
            # 500-shaped MCP error for what is semantically just "another
            # agent already tombstoned this id" — a clean, expected
            # outcome under sub-agent concurrency. Surface
            # `TombstonedError` with a message that mirrors the
            # find-time pre-lock fallback above so the user-facing
            # error is consistent regardless of which path detected
            # the race.
            if not _id_still_at_path(path, memory_id):
                raise TombstonedError(
                    f"memory {memory_id} is already tombstoned "
                    f"(raced with concurrent tombstone)"
                )
            post = frontmatter.load(path)
            post.metadata["removed"] = utcnow()
            post.metadata["removed_reason"] = reason
            # Only emit the field when a session_id was passed — keeps
            # legacy tests and ad-hoc callers from getting an opaque
            # `None` lying in frontmatter. The reader treats missing
            # or None identically.
            if session_id is not None:
                post.metadata["removed_session"] = session_id

            _atomic_write_post(target, post)
            try:
                path.unlink()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
            # fsync the source directory too: the unlink is a metadata
            # change that needs to survive a crash, otherwise we could
            # come back to BOTH the tombstone and the original active
            # file existing (a soft form of double-bookkeeping).
            fsync_dir(path.parent)
            # perf: index remove under lock is intentional — see audit H1.
            _index_remove_quietly(self.root, memory_id)
        return target

    # ---- tombstone read paths --------------------------------------------

    def load_tombstones(self) -> list[TombstonedMemory]:
        """All tombstoned memories, sorted by `removed` descending.

        Skips malformed files defensively, the same way `load_all` does
        for active memories — one corrupt removal record shouldn't blind
        the curation tooling to all the others. Tombstones without a
        `removed` timestamp (extremely unusual, but possible from a
        hand-edited file) are skipped: every legitimate tombstone
        produced by `Store.tombstone` carries the field.
        """
        out: list[TombstonedMemory] = []
        for path in self._iter_tombstone_paths():
            try:
                tombstone = self._load_tombstone_path(path)
            except (ValueError, KeyError, OSError):
                # Same race rationale as load_all: a concurrent
                # `prune_tombstones` could delete a file between
                # listdir and read.
                continue
            out.append(tombstone)
        out.sort(key=lambda t: t.removed, reverse=True)
        return out

    def list_tombstones(
        self, scopes: list[str] | None = None
    ) -> list[TombstonedSummary]:
        """Body-stripped tombstones for triage. Scope filter is
        intersection like `list_summaries`."""
        out: list[TombstonedSummary] = []
        for tombstone in self.load_tombstones():
            if scopes and not _scope_intersect(tombstone.scopes, scopes):
                continue
            out.append(
                TombstonedSummary(
                    id=tombstone.id,
                    scopes=tombstone.scopes,
                    confidence=tombstone.confidence,
                    summary=first_summary_line(tombstone.body),
                    created=tombstone.created,
                    updated=tombstone.updated,
                    last_verified_at=tombstone.last_verified_at,
                    category=tombstone.category,
                    removed=tombstone.removed,
                    removed_reason=tombstone.removed_reason,
                    removed_session=tombstone.removed_session,
                )
            )
        return out

    def load_tombstone(self, memory_id: str) -> TombstonedMemory:
        """Load one tombstone by ID. Raises if missing."""
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")

        for path in self._iter_tombstone_paths():
            try:
                tombstone = self._load_tombstone_path(path)
            except (ValueError, KeyError):
                continue
            if tombstone.id == memory_id:
                return tombstone

        raise MemoryNotFoundError(f"no tombstone with id {memory_id}")

    def _find_tombstone_path_for_id(self, memory_id: str) -> Path | None:
        if not is_valid_ulid(memory_id):
            return None
        for path in self._iter_tombstone_paths():
            try:
                post = frontmatter.load(path)
            except (ValueError, KeyError, OSError):
                continue
            if post.metadata.get("id") == memory_id:
                return path
        return None

    def _load_tombstone_path(self, path: Path) -> TombstonedMemory:
        post = frontmatter.load(path)
        meta = post.metadata
        # Schema-version gate, mirroring `_load_path` for active
        # memories. Tombstones share the on-disk format with active
        # memories (they're the same files moved into .tombstones/
        # with `removed` + `removed_reason` appended), so the same
        # rule applies — refuse forward-version files rather than
        # risk misinterpreting changed semantics.
        on_disk_version = meta.get("schema_version", 1)
        try:
            on_disk_int = int(on_disk_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{path}: schema_version is not an integer: {on_disk_version!r}"
            ) from exc
        if on_disk_int > SCHEMA_VERSION:
            raise ValueError(
                f"{path}: schema_version {on_disk_int} is newer than this "
                f"reader supports (max {SCHEMA_VERSION}); upgrade bettermemory."
            )
        try:
            origin_raw = meta.get("origin")
            origin = (
                Origin.model_validate(origin_raw)
                if isinstance(origin_raw, dict)
                else None
            )
            verified_raw = meta.get("last_verified_at")
            last_verified_at: datetime | None
            if verified_raw is None:
                last_verified_at = None
            else:
                try:
                    last_verified_at = _as_dt(verified_raw)
                except ValueError:
                    last_verified_at = None
            removed_session = meta.get("removed_session")
            category_raw = meta.get("category")
            category: Category | None
            if category_raw is None:
                category = None
            else:
                try:
                    category = Category(str(category_raw))
                except ValueError:
                    category = None
            return TombstonedMemory(
                id=str(meta["id"]),
                created=_as_dt(meta["created"]),
                updated=_as_dt(meta["updated"]),
                scopes=list(meta["scopes"]),
                confidence=Confidence(meta["confidence"]),
                source=Source(meta["source"]),
                body=post.content.strip() + "\n",
                origin=origin,
                last_verified_at=last_verified_at,
                category=category,
                verified_paths=_load_str_list(meta.get("verified_paths")),
                verified_commits=_load_str_list(meta.get("verified_commits")),
                verified_versions=_load_str_list(meta.get("verified_versions")),
                verified_absent_paths=_load_str_list(meta.get("verified_absent_paths")),
                removed=_as_dt(meta["removed"]),
                removed_reason=str(meta["removed_reason"]),
                removed_session=(
                    str(removed_session) if removed_session is not None else None
                ),
            )
        except KeyError as exc:
            raise ValueError(f"{path}: missing field {exc.args[0]}") from exc

    # ---- restore ---------------------------------------------------------

    def restore(self, memory_id: str) -> Memory:
        """Move a tombstone back to the active set, stripping removal
        frontmatter. The body and timestamps are preserved as-is — the
        body didn't change while it was tombstoned, and bumping
        `updated` on restore would let a freshly-restored ten-year-old
        memory rank like a new write in the recency boost.

        The event log is the audit trail for restore actions; we do
        not stamp a `restored_at` field on the file itself, which
        would accumulate over repeat tombstone/restore cycles.

        Raises:
            MemoryNotFoundError: no record (active or tombstoned) with
              that id, or the tombstone was removed concurrently (by a
              parallel restore, a prune, or any other tombstone-deleting
              path) between the find walk and the under-lock recheck.
            NotTombstonedError: id is active. The caller probably meant
              `memory_update`; restore is only for tombstones. Also raised
              if a parallel restore won the race and the id is now active
              under the lock.
            OSError: a genuine disk-level failure in the active-record write
              (`_atomic_write_post`) or the tombstone unlink — EIO mid-write,
              ENOSPC on the rename, EACCES on the unlink. The benign
              ENOENT-on-unlink race is swallowed; everything else
              propagates. `memory_restore` catches this and translates it
              to a structured `ValueError`.
            ValueError: the tombstone's `created` frontmatter is missing or
              unparseable, so the restored active filename's date prefix
              cannot be rebuilt. `memory_restore` re-raises this verbatim so
              the caller learns which field is malformed.
        """
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")

        # If the id resolves to an active memory, surface a distinct
        # error rather than silently returning it. "Restoring" something
        # that isn't gone is the kind of mistake worth flagging.
        if self._find_path_for_id(memory_id) is not None:
            raise NotTombstonedError(
                f"memory {memory_id} is active; nothing to restore"
            )

        tombstone_path = self._find_tombstone_path_for_id(memory_id)
        if tombstone_path is None:
            raise MemoryNotFoundError(f"no tombstone with id {memory_id}")

        # Lock on the tombstone path for the whole read-write-unlink
        # sequence. Two concurrent restores of the same id would both
        # see the tombstone outside the lock, both read it, and both
        # try to write — the second's `active_path.exists()` check
        # would land on the first's just-written active file and pick
        # a collision-suffixed name, resurrecting the memory twice. The
        # tombstone lock serializes the sequence; the second restore
        # then sees the tombstone is gone and raises clearly.
        with _locked(tombstone_path):
            # W7: under-lock recheck mirroring W1's `tombstone()` fix.
            # `_find_tombstone_path_for_id` above walked the tombstone
            # directory unlocked, so a concurrent `restore()` /
            # `prune_tombstones()` (or any tombstone-deleting path)
            # may have moved or unlinked the file between the find and
            # this lock acquisition. Without the recheck, the
            # `frontmatter.load(tombstone_path)` below raises a bare
            # `FileNotFoundError` that the handler layer historically
            # catches via the `except FileNotFoundError` arm below — but
            # the recheck also covers the tombstone-stem-reuse edge
            # (extremely unlikely with ULID-suffixed filenames, but
            # symmetric with the W1 discipline) and gives a uniform
            # "raced with concurrent restore/prune" message regardless
            # of which mutator detected the race.
            if not _id_still_at_path(tombstone_path, memory_id):
                raise MemoryNotFoundError(
                    f"no tombstone with id {memory_id} (raced with "
                    f"concurrent restore or prune)"
                )
            # W7: also re-check active-record absence under the lock.
            # The unlocked pre-lock check above can miss a concurrent
            # restore that completed in the window between our active
            # check and our tombstone-lock acquisition: a parallel
            # restorer would create the active record AND unlink the
            # tombstone before we got here. The tombstone-gone branch
            # above usually catches this first — but if for any reason
            # the tombstone is still present (e.g. a prune raced AGAINST
            # the parallel restore's unlink and the tombstone got
            # re-written elsewhere — extremely contrived), the active
            # recheck keeps `_atomic_write_post(active_path, …)` below
            # from silently clobbering the parallel restore's active
            # file. Symmetric with W1's `_id_still_at_path`: cheap
            # recheck under the lock that the pre-lock invariant still
            # holds.
            if self._find_path_for_id(memory_id) is not None:
                raise NotTombstonedError(
                    f"memory {memory_id} is active; nothing to restore "
                    f"(raced with concurrent restore)"
                )
            try:
                post = frontmatter.load(tombstone_path)
            except FileNotFoundError:
                # Defense in depth: the W7 recheck above is the primary
                # guard, but keep this arm for the narrow case where the
                # frontmatter.load fails mid-read on a file that vanished
                # AFTER the recheck and BEFORE the parse completed.
                raise MemoryNotFoundError(
                    f"no tombstone with id {memory_id} (raced with "
                    f"concurrent restore or prune)"
                ) from None
            post.metadata.pop("removed", None)
            post.metadata.pop("removed_reason", None)
            post.metadata.pop("removed_session", None)

            # Mirror `_path_for`'s always-suffix discipline so the restore
            # lands at the same shape a fresh `write()` would produce —
            # date-prefixed slug + short-id suffix, no `.tombstone.md`.
            # Pre-fix this used the legacy `bare.exists()` gate that
            # `bc47593` killed in `_path_for`: two concurrent restores of
            # differently-tombstoned memories whose bodies slugify
            # identically each locked their own (distinct) tombstone
            # path, both saw `active_path.exists() == False`, both wrote
            # — second silently clobbered the first. The lock is on the
            # tombstone, not on the destination, so it can't help here.
            # Always-suffixing the active filename closes the window the
            # same way it did for `write()`.
            try:
                created = _as_dt(post.metadata["created"])
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"{tombstone_path}: cannot restore — missing/invalid created"
                ) from exc
            slug = make_slug(post.content)
            short = memory_id[-6:].lower()
            active_path = self.root / build_filename(created, f"{slug}-{short}")

            _atomic_write_post(active_path, post)
            try:
                tombstone_path.unlink()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    # Active file already written; orphaning the tombstone
                    # is recoverable but we still want to surface the IO
                    # error so the caller doesn't think the move was clean.
                    raise
            # Symmetric to tombstone(): fsync the tombstone dir so the
            # unlink is durable. Without this, a crash could resurrect
            # the tombstone alongside the restored active file.
            fsync_dir(tombstone_path.parent)

            restored = self._load_path(active_path)
            # Restored memories rejoin the searchable set — keep the FTS5
            # index in step with the file system. The remove-on-tombstone
            # call dropped this id; the restore is the symmetric upsert.
            # perf: index upsert under lock is intentional — see audit H1.
            _index_upsert_quietly(self.root, restored, filename=active_path.name)
        return restored

    # ---- scope rename ----------------------------------------------------

    def rename_scope(
        self,
        old: str,
        new: str,
        *,
        include_tombstones: bool = True,
    ) -> dict[str, list[str]]:
        """Replace `old` with `new` across active memories' scope lists.

        Renaming is the cheap fix for typo'd or deprecated scopes —
        e.g. `projct:foo` → `projects:foo` after a misspell, or
        `infra` → `infrastructure` after deciding the long form is the
        canonical one. The body is unchanged, so `updated` is bumped
        (the metadata moved) but `last_verified_at` is preserved
        (the body's claims weren't touched).

        Tombstones are renamed too by default so the curation view
        (memory_list_tombstones, memory_health) stays consistent —
        otherwise a renamed scope would re-appear in `rare_scopes`
        every time a removed memory is the last carrier of the old
        spelling. Pass `include_tombstones=False` to leave the
        removal audit log unchanged.

        Returns `{"active": [ids], "tombstoned": [ids]}` — the lists
        of memory ids whose scope sets actually changed. A memory
        that already had `new` and didn't have `old` is not touched
        (and not listed). A memory whose only effect would be
        de-duplication of the new scope IS counted, since the on-disk
        list shrank.
        """
        if old == new:
            return {"active": [], "tombstoned": []}

        active_changed: list[str] = []
        for path in self._iter_active_paths():
            # Read-modify-write under the same lock per file. The prior
            # shape loaded outside the lock, opening a window where a
            # concurrent `update` or `tombstone` could land between our
            # read and write — our write would then clobber that change
            # with a stale body plus the renamed scope. The index update
            # stays inside the lock too: the FTS5 `scopes_text` column
            # is rebuilt from the new scope list, and without keeping
            # it in step with disk, BM25 ranking on the renamed scope
            # reads against stale indexed text until the next manual
            # `bettermemory reindex`.
            with _locked(path):
                # C2 recheck symmetric to `update`/`mark_verified`:
                # `_iter_active_paths` listed the directory unlocked,
                # so a concurrent `tombstone()` may have moved the
                # file between the iteration and this lock. Without
                # the recheck we'd either crash on a missing file or
                # (worst case on a legacy bare-name layout, where a
                # different memory could land at the same path after
                # the tombstone) silently rewrite the scopes of an
                # unrelated memory. Skip on miss; the next
                # `rename_scope` invocation will pick up any
                # newly-written files.
                try:
                    memory = self._load_path(path)
                except (ValueError, KeyError, FileNotFoundError):
                    continue
                new_scopes = self._scopes_after_rename(memory.scopes, old, new)
                if new_scopes is None:
                    continue
                # Bump `updated` because the metadata moved.
                # `last_verified_at` is preserved — the body's claims
                # are untouched, so the verification (if any) still
                # applies.
                refreshed = memory.model_copy(
                    update={"scopes": new_scopes, "updated": utcnow()}
                )
                self._write_path(path, refreshed)
                # perf: index upsert under lock is intentional — see audit H1.
                _index_upsert_quietly(self.root, refreshed, filename=path.name)
                active_changed.append(refreshed.id)

        tombstoned_changed: list[str] = []
        if include_tombstones:
            for tpath in self._iter_tombstone_paths():
                # Read-modify-write under the same lock — the active-side
                # branch above already does this; the tombstone branch
                # needs the same discipline. A concurrent `restore` can
                # land between an unlocked read and a locked write and
                # have its in-flight rewrite clobbered.
                with _locked(tpath):
                    # C2 recheck: tombstones can vanish under our feet
                    # via `restore()` or `prune_tombstones()`. The load
                    # below already handles the missing-file case, but
                    # we also need to make sure the file we lock still
                    # has the same id we'd have computed pre-lock
                    # (otherwise a `restore` + new tombstone of a
                    # different memory could swap which id sits at this
                    # path on legacy layouts). The frontmatter.load
                    # under the lock IS that check — we trust whatever
                    # id is in the file at lock time and act on it.
                    try:
                        post = frontmatter.load(tpath)
                    except (ValueError, KeyError, OSError):
                        continue
                    raw_scopes = post.metadata.get("scopes")
                    if not isinstance(raw_scopes, list):
                        continue
                    new_scopes_or_none = self._scopes_after_rename(
                        [str(s) for s in raw_scopes], old, new
                    )
                    if new_scopes_or_none is None:
                        continue
                    post.metadata["scopes"] = new_scopes_or_none
                    # Atomic in-place rewrite via tmp+rename. The previous
                    # implementation used `write_bytes`, which truncates
                    # and rewrites in place — a crash mid-write would leave
                    # the tombstone partially written. The active-side
                    # rename_scope path goes through `_write_path` which
                    # already does this; mirror it here.
                    _atomic_write_post(tpath, post)
                    tombstoned_changed.append(str(post.metadata.get("id")))

        return {"active": active_changed, "tombstoned": tombstoned_changed}

    @staticmethod
    def _scopes_after_rename(scopes: list[str], old: str, new: str) -> list[str] | None:
        """Return the new scope list if `old` appears, else None.

        Order of remaining scopes is preserved. If `new` is already
        present, `old` is removed and `new` is not duplicated. If the
        memory carried `old` only, the result is `[new]`.
        """
        if old not in scopes:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for s in scopes:
            if s == old:
                if new not in seen:
                    out.append(new)
                    seen.add(new)
                continue
            if s in seen:
                continue
            out.append(s)
            seen.add(s)
        return out

    # ---- prune -----------------------------------------------------------

    def prune_tombstones(
        self,
        older_than: timedelta,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Delete tombstones whose `removed` timestamp is older than the
        cutoff. Returns the list of pruned memory ids in chronological
        (oldest-first) removal order so the caller can log them.

        This is a hard delete — pruned tombstones are gone from disk
        with no further audit trail beyond whatever the event log
        already captured. The retention knob is per-user policy, not
        per-memory; if you want to keep a specific tombstone forever,
        either bump the retention window or restore it before pruning.
        """
        cutoff = (now or utcnow()) - older_than
        pruned: list[tuple[datetime, str]] = []
        # Sidecar `.lock` files to sweep AFTER their `_locked(path)` block
        # has exited. `flock_excl` deliberately never unlinks the lockfile
        # on release (per-inode identity; see `_fsutil.flock_excl`), so
        # without a sweep the 0-byte `<name>.lock` accumulates one orphan
        # per pruned tombstone, unbounded.
        sidecars: list[Path] = []
        for path in self._iter_tombstone_paths():
            # Acquire the per-tombstone lock for read + delete. Without
            # it, a concurrent `restore(id)` (which holds the same
            # `_locked(path)`) can rewrite the active file out of the
            # tombstone, and our subsequent `unlink` here removes a
            # tombstone the restore intended to keep audited. Matching
            # the 2.6.4 migrate.py fix — every mutator now goes through
            # the per-file lock.
            with _locked(path):
                try:
                    tombstone = self._load_tombstone_path(path)
                except (ValueError, KeyError):
                    # Malformed tombstones are left alone — pruning them
                    # would silently drop possibly-recoverable history.
                    continue
                if tombstone.removed >= cutoff:
                    continue
                try:
                    path.unlink()
                except OSError:
                    # Best-effort: a tombstone we can't delete (perms,
                    # mid-rotation race) will be retried on the next prune
                    # call. Don't kill the loop.
                    continue
                sidecars.append(path.with_suffix(path.suffix + ".lock"))
                pruned.append((tombstone.removed, tombstone.id))

        # Unlink the sidecar lockfiles AFTER their `_locked` block closed
        # the handle. POSIX could unlink while still inside the block (the
        # held fd keeps the inode alive), but Windows refuses to delete a
        # file with an open handle (`msvcrt.locking` keeps it open for the
        # duration of the `with`), so an in-lock unlink raised and the
        # sidecar leaked — caught by the Windows CI leg, invisible on macOS.
        # Sweeping here, post-release, deletes the name on both platforms.
        # Mirrors `episodes._cleanup_orphan_lockfiles`, which runs the same
        # sweep after its prune loop for the same reason.
        for sidecar in sidecars:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                # Best-effort: a sidecar we still can't unlink (perms, a
                # racing holder) is swept on a later prune. The tombstone
                # itself is already gone, so don't fail the prune over it.
                pass

        pruned.sort(key=lambda item: item[0])
        return [memory_id for _, memory_id in pruned]

    # ---- internals --------------------------------------------------------

    def _path_for(self, memory: Memory) -> Path:
        slug = make_slug(memory.body)

        # Unconditionally embed the short ULID in the filename so the
        # name is unique by construction. Pre-2.7 the code picked the
        # bare `<date>-<slug>.md` when the file didn't yet exist and
        # added the ULID only on collision — a TOCTOU silent-data-loss
        # bug: two concurrent `write()`s whose bodies slugify to the
        # same value both observed `bare.exists() == False`, both
        # picked the bare candidate, serialized on `_locked(<same
        # path>)` — and the second writer's `_atomic_write_post`
        # clobbered the first memory entirely. The on-disk file still
        # parsed, but it carried writer B's id; writer A's memory was
        # gone with no trace.
        #
        # Always-suffixed kills the race: with distinct ULIDs (which
        # `generate_ulid` makes vanishingly unlikely to collide), two
        # writers can never pick the same path even if their bodies
        # slugify identically. Matches the discipline `tombstone()`
        # adopted in 2.6.4 (see store.py:474-485) for the same reason.
        #
        # Existing unsuffixed memories on disk continue to load — the
        # reader keys off the `id` field, not the filename. The cost
        # is a slightly longer filename (6 hex chars + a hyphen) on
        # every new write; the benefit is no silent overwrites.
        short = memory.id[-6:].lower()
        return self.root / build_filename(memory.created, f"{slug}-{short}")

    def _find_path_for_id(self, memory_id: str) -> Path | None:
        if not is_valid_ulid(memory_id):
            return None
        for path in self._iter_active_paths():
            try:
                post = frontmatter.load(path)
            except (ValueError, KeyError, OSError):
                continue
            if post.metadata.get("id") == memory_id:
                return path
        return None

    def _write_path(self, path: Path, memory: Memory) -> None:
        post = frontmatter.Post(memory.body.strip() + "\n")
        meta: dict[str, object] = {
            # `schema_version` is the first key so it's visible at the top
            # of the file and unambiguously associated with the format
            # rather than the memory's content. Readers that don't know
            # this field default it to 1; readers that don't *recognize*
            # the value refuse to load the file.
            "schema_version": SCHEMA_VERSION,
            "id": memory.id,
            "created": memory.created,
            "updated": memory.updated,
            "scopes": list(memory.scopes),
            "confidence": memory.confidence.value,
            "source": memory.source.value,
        }
        # Origin is optional and only written when populated. We emit a
        # nested mapping with `exclude_none` so we never write
        # `origin: {cwd: null, repo: null, branch: null}` — that's noise.
        if memory.origin is not None:
            origin_dict = memory.origin.model_dump(mode="json", exclude_none=True)
            if origin_dict:
                meta["origin"] = origin_dict
        # `last_verified_at` is omitted from frontmatter when None — keeps
        # newly-written memories from carrying a `last_verified_at: null`
        # placeholder, which would be visual noise on every file. Once the
        # field is populated by `mark_verified`, the key is written.
        if memory.last_verified_at is not None:
            meta["last_verified_at"] = memory.last_verified_at
        # `category` is omitted when None (the legacy default — runtime
        # treats it as fact). Writing the key only when the caller
        # explicitly chose a category keeps fact memories visually
        # identical to legacy ones on disk.
        if memory.category is not None:
            meta["category"] = memory.category.value
        # Verified-claims lists are omitted when empty — same noise-floor
        # rationale as `last_verified_at`. They populate as a unit on
        # the `memory_verify` event that captured them.
        if memory.verified_paths:
            meta["verified_paths"] = list(memory.verified_paths)
        if memory.verified_commits:
            meta["verified_commits"] = list(memory.verified_commits)
        if memory.verified_versions:
            meta["verified_versions"] = list(memory.verified_versions)
        if memory.verified_absent_paths:
            meta["verified_absent_paths"] = list(memory.verified_absent_paths)
        # `links` is omitted when empty — same noise-floor rationale as
        # `verified_paths`. Each link is serialized as a plain dict
        # (`type` is the enum value, not the Python name) so a hand-
        # editing user can read and edit the frontmatter directly.
        if memory.links:
            meta["links"] = [
                {
                    "type": link.type.value,
                    "target_id": link.target_id,
                    **({"note": link.note} if link.note is not None else {}),
                }
                for link in memory.links
            ]
        post.metadata = meta
        _atomic_write_post(path, post)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_INDEX_LOG = _logging.getLogger("bettermemory.store")
_INDEX_REPAIR_HINT = "Run `bettermemory reindex` to repair."

# S4: one-shot startup divergence check. Tracks which `(root,)` paths
# have already emitted the FTS5-out-of-sync WARNING in this process so
# multiple Store constructions on the same root (e.g. tests, the
# `Store(memory_dir).write(...)` one-liner pattern) don't spam the log.
# Set lives for the process lifetime; two distinct roots each get
# their own warning. Module-level state (not weakref) because the keys
# are `Path` instances, which are value-types rather than ownership
# anchors — we want the warning suppressed across short-lived Store
# objects on the same root, not just within one Store's lifetime.
_DIVERGENCE_WARNED_ROOTS: set[Path] = set()


@best_effort(
    "index auto-rebuild after schema upgrade",
    logger=_INDEX_LOG,
    repair_hint=_INDEX_REPAIR_HINT,
)
def _rebuild_index_if_flagged(store: Store) -> None:
    """Rebuild the FTS5 index from disk when a schema-version migration
    flagged it rebuild-pending (`meta.needs_rebuild`, set by
    `index._ensure_schema`'s older-version path).

    The migration drops the data tables empty; the incremental Store
    hooks repopulate only whatever gets touched afterwards. On a store
    above the prefilter threshold that means `memory_search` would —
    without the flag — re-engage the FTS prefilter once enough
    post-upgrade upserts accumulate and silently lose every untouched
    legacy memory. Rebuilding here, at the first Store construction
    after the upgrade, closes that window at its earliest opportunity;
    `index.rebuild` is transactional and clears the flag only when the
    repopulation lands, and `_load_search_candidates` routes to
    `load_all` while the flag is set, so search stays correct (just
    linear-scan slow) before/without this rebuild.

    Best-effort via the decorator: a rebuild failure warns (with the
    reindex hint) and must not block construction — the flag stays set,
    so search keeps bypassing the index and the next construction
    retries. On success the log is INFO: this shape used to surface as
    the S4 divergence WARNING below, but a self-healed index is a
    resolution notice, not an operator action item.
    """
    from . import index as _index

    if not _index.status(store.root).get("needs_rebuild"):
        return
    count = _index.rebuild(store.root, store.iter_active())
    _INDEX_LOG.info(
        "bettermemory: index schema upgraded; rebuilt %d memories from "
        "canonical disk state.",
        count,
    )


def _parse_memory_file(path: Path) -> Memory:
    """Parse one on-disk memory file into a `Memory`. The canonical
    reader — `Store._load_path` delegates here, and
    `count_unparseable_memory_files` reuses it so "unparseable" means
    exactly "what `iter_active()` would skip"."""
    post = frontmatter.load(path)
    meta = post.metadata
    # Schema-version gate. Memories without `schema_version` are
    # implicitly version 1 (the format predates the field). Anything
    # *higher* than what this reader supports is refused — the caller
    # (`load_all`, etc.) catches ValueError and skips the file silently
    # (the skip path emits no log; `bettermemory doctor` surfaces the
    # count gap), so a user who downgrades bettermemory after writing
    # memories under a newer minor sees them drop out of the retrieval
    # surface rather than risk the reader misinterpreting fields whose
    # semantics changed.
    on_disk_version = meta.get("schema_version", 1)
    try:
        on_disk_int = int(on_disk_version)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path}: schema_version is not an integer: {on_disk_version!r}"
        ) from exc
    if on_disk_int > SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {on_disk_int} is newer than this "
            f"reader supports (max {SCHEMA_VERSION}); upgrade bettermemory "
            f"or remove the file from the active set."
        )
    try:
        # `origin` is additive — memories written before this field
        # existed have no entry, and that's intentionally fine: they're
        # treated as "global" by the auto-scope filter.
        origin_raw = meta.get("origin")
        origin = (
            Origin.model_validate(origin_raw) if isinstance(origin_raw, dict) else None
        )
        # `last_verified_at` is also additive — older memories have no
        # entry and read as "never verified". A malformed timestamp is
        # treated the same as missing (rather than raising) so a typo
        # in the file doesn't render the whole memory unloadable.
        verified_raw = meta.get("last_verified_at")
        last_verified_at: datetime | None
        if verified_raw is None:
            last_verified_at = None
        else:
            try:
                last_verified_at = _as_dt(verified_raw)
            except ValueError:
                last_verified_at = None
        # `category`, `verified_paths`, `verified_commits`,
        # `verified_versions` are additive — legacy memories load
        # with None / empty lists. Unknown category values fall back
        # to None rather than raising; the runtime treats None as
        # the legacy "fact" default, so a memory written by a newer
        # bettermemory that introduces a new category still loads
        # cleanly under an older reader (semantics revert to fact).
        category_raw = meta.get("category")
        category: Category | None
        if category_raw is None:
            category = None
        else:
            try:
                category = Category(str(category_raw))
            except ValueError:
                category = None
        # `links` is additive (T2.2). Legacy memories load with [].
        # Each entry must be a dict with `type` and `target_id`;
        # entries with unknown type or invalid target_id are
        # silently dropped rather than raising, so a forward-compat
        # downgrade (memory written under a newer reader that
        # introduced a new link type) doesn't break the older
        # reader for the whole file.
        links_raw = meta.get("links")
        links: list[MemoryLink] = []
        if isinstance(links_raw, list):
            for entry in links_raw:
                if not isinstance(entry, dict):
                    continue
                try:
                    links.append(MemoryLink.model_validate(entry))
                except (ValueError, KeyError):
                    continue
        return Memory(
            id=str(meta["id"]),
            created=_as_dt(meta["created"]),
            updated=_as_dt(meta["updated"]),
            scopes=list(meta["scopes"]),
            confidence=Confidence(meta["confidence"]),
            source=Source(meta["source"]),
            body=post.content.strip() + "\n",
            origin=origin,
            last_verified_at=last_verified_at,
            category=category,
            verified_paths=_load_str_list(meta.get("verified_paths")),
            verified_commits=_load_str_list(meta.get("verified_commits")),
            verified_versions=_load_str_list(meta.get("verified_versions")),
            verified_absent_paths=_load_str_list(meta.get("verified_absent_paths")),
            links=links,
        )
    except KeyError as exc:
        raise ValueError(f"{path}: missing field {exc.args[0]}") from exc


def count_active_memory_files(root: Path) -> int:
    """Count the active-memory ``.md`` files under `root` without
    parsing them — the `_iter_active_paths()` filter (regular file, not
    a symlink, `.md` suffix) as a bare count, for callers that have no
    Store instance and must not construct one (`Store.__post_init__`
    mkdirs and auto-rebuilds — write side effects). Shared by the S4
    divergence warning below and doctor's index-health check so the two
    disk-vs-`indexed_count` comparisons cannot drift apart. Propagates
    OSError from an unlistable directory; callers pick their own
    degraded answer."""
    count = 0
    for entry in root.iterdir():
        if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
            count += 1
    return count


def count_unparseable_memory_files(root: Path) -> int:
    """Count the active-memory ``.md`` files under `root` that
    `iter_active()` would skip (malformed frontmatter, missing required
    fields, a schema_version newer than this reader). Those files can
    never enter the FTS5 index — `index.rebuild` consumes
    `iter_active()` — so a disk-vs-`indexed_count` comparison that
    ignores them reports a divergence `bettermemory reindex` cannot
    clear. Same Store-free contract as `count_active_memory_files`, and
    the same shared role: the S4 warning below and doctor's index-health
    check both subtract this count before calling the index stale.

    Parses every file (unlike the bare count above), so callers reach
    for it only after the cheap raw-count comparison has already
    diverged. Any per-file exception matches `iter_active`'s skip set
    and counts as unparseable; OSError from the `iterdir` itself
    propagates."""
    count = 0
    for entry in root.iterdir():
        if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
            try:
                _parse_memory_file(entry)
            except Exception:  # noqa: BLE001
                # Any parse failure counts as unparseable — never a
                # crash. `_parse_memory_file`'s raise surface is wider
                # than the (ValueError, KeyError, OSError) tuple the
                # bulk readers catch: a valid-YAML-but-wrong-shape
                # value escapes it (`scopes: 5` → TypeError from
                # `list(meta["scopes"])`), and the pydantic/enum
                # internals it delegates to keep a full enumeration
                # fragile. This walk runs at every Store construction
                # (via `_warn_on_index_divergence`), so a missed type
                # would brick server boot on one weird file.
                # `iter_active` skips on the same width, keeping
                # "counted here" == "skipped by rebuild".
                count += 1
    return count


def _warn_on_index_divergence(root: Path) -> None:
    """Compare the on-disk active-memory count to the FTS5 index's
    `indexed_count` and emit a one-shot WARNING per root if they
    diverge. See ``Store.__post_init__`` for the motivating audit note
    (S4: out-of-band ``.md`` writes silently desync the FTS5 index).

    Three divergence shapes are surfaced:

    - Missing index file but on-disk memories present (typical when a
      `sync pull` populated the worktree before any hook ran).
    - Corrupt index file (a `status()["corrupt"]` flag); the indexed
      count is unknowable and the surface to fix it is the same:
      `bettermemory reindex`.
    - Mismatched counts on an otherwise healthy index.

    The count comparison is parse-aware: `index.rebuild` consumes
    `iter_active()`, which skips unparseable files, so the highest
    count a rebuild can reach is `disk - unparseable`. A gap fully
    explained by unparseable files gets a fix-the-files warning
    instead — recommending reindex there would send the user to a
    repair that can never clear the warning.

    Best-effort: failures inside the check (a transient OSError on
    `iterdir`, a sqlite issue inside `status`) are swallowed. The
    purpose is a friendly heads-up at construction; if we can't
    decide cleanly, staying silent is safer than firing a false
    positive that misleads the operator.

    Lazy import on `index` to keep the Store module loadable in the
    pure-file-store scenarios (`tests/test_store.py` runs without
    touching the SQLite extension; same rationale as
    `_index_upsert_quietly`).
    """
    if root in _DIVERGENCE_WARNED_ROOTS:
        return
    try:
        from . import index as _index

        status = _index.status(root)
        disk_count = count_active_memory_files(root)
    except OSError:
        # Best-effort. If we can't read the directory, the rest of
        # the Store will surface a clearer error on its first real
        # operation; don't compound it with a noisy startup warning.
        return

    if status.get("corrupt"):
        # An unreadable index is a divergence we should always flag —
        # the indexed count is unknowable, so we report what we can:
        # the disk count and the corruption signal.
        _DIVERGENCE_WARNED_ROOTS.add(root)
        _INDEX_LOG.warning(
            "bettermemory: FTS5 index at %s is corrupt (disk=%d memories). "
            "Run `bettermemory reindex` to rebuild the index from "
            "canonical disk state. Search results may be incomplete or "
            "include stale references until then.",
            status.get("path", root / ".index.sqlite"),
            disk_count,
        )
        return

    # `exists=False` is the "no index file yet" path. Treat as
    # `indexed_count=0` so a pre-populated directory with no index
    # (typical after a fresh `sync pull` into a worktree that's
    # never had the server started against it) trips the warning.
    indexed_count = (
        int(status.get("indexed_count", 0) or 0) if status.get("exists") else 0
    )
    if indexed_count == disk_count:
        return

    # Raw counts diverged — refine with the parse walk (paid only on
    # this rare path; the aligned common case above stays a bare
    # iterdir). `disk - unparseable` is the highest count a rebuild
    # can reach.
    try:
        unparseable_count = count_unparseable_memory_files(root)
    except OSError:
        return
    indexable_count = disk_count - unparseable_count

    _DIVERGENCE_WARNED_ROOTS.add(root)
    if indexed_count == indexable_count:
        # Not an index problem: the index already holds every file a
        # rebuild could parse. Point at the actual defect — the files.
        _INDEX_LOG.warning(
            "bettermemory: %d of %d memory file(s) at %s cannot be parsed "
            "and are invisible to memory_search (the FTS5 index already "
            "holds all %d parseable memories). `bettermemory reindex` will "
            "not change this — run `bettermemory doctor` to identify the "
            "files, then fix their frontmatter or remove them.",
            unparseable_count,
            disk_count,
            root,
            indexed_count,
        )
        return

    unparseable_note = (
        f" {unparseable_count} of the disk files cannot be parsed and will "
        f"never index; a rebuild reaches index={indexable_count}, "
        f"not {disk_count}."
        if unparseable_count
        else ""
    )
    _INDEX_LOG.warning(
        "bettermemory: FTS5 index appears out-of-sync with disk "
        "(index=%d memories, disk=%d). This usually means a memory "
        "file was added/edited outside the Store API (external editor, "
        "sync pull, or a process bypassing memory_write). "
        "Run `bettermemory reindex` to rebuild the index from "
        "canonical disk state. Search results may be incomplete or "
        "include stale references until then.%s",
        indexed_count,
        disk_count,
        unparseable_note,
    )


@best_effort(
    "index upsert",
    logger=_INDEX_LOG,
    repair_hint=_INDEX_REPAIR_HINT,
    id_getter=lambda root, memory, *, filename: memory.id,
)
def _index_upsert_quietly(root: Path, memory: Memory, *, filename: str) -> None:
    """Update the FTS5 index for one memory. Best-effort: a failure
    here (corrupt index, locked database, missing SQLite extension)
    logs a warning and continues so the on-disk write — the canonical
    record — still succeeds. The next ``bettermemory reindex`` will
    repair any drift.

    `filename` is the actual on-disk filename of the memory (the
    caller just wrote to it, so it has the path). Threading it
    through is what lets `filenames_for_ids` resolve
    collision-suffixed names; re-deriving from the Memory fields
    alone would silently point at the unsuffixed sibling.

    Lazy import so this module loads cleanly even when callers don't
    actually use the index (e.g. pure-Python tests against the file
    store directly). The ``@best_effort`` wrapper supplies the
    swallow-and-warn shape — the body below stays the bare
    happy-path call."""
    from . import index as _index

    _index.upsert(root, memory, filename=filename)


@best_effort(
    "index remove",
    logger=_INDEX_LOG,
    repair_hint=_INDEX_REPAIR_HINT,
    id_getter=lambda root, memory_id: memory_id,
)
def _index_remove_quietly(root: Path, memory_id: str) -> None:
    """Drop one memory from the FTS5 index. Same best-effort contract
    as the upsert: never block the on-disk tombstone on an index
    failure. The ``@best_effort`` wrapper supplies the swallow-and-warn
    shape — the body below stays the bare happy-path call."""
    from . import index as _index

    _index.remove(root, memory_id)


def _atomic_write_post(path: Path, post: frontmatter.Post) -> None:
    """Atomic, durable, 0o600 write of a frontmatter Post to `path`.

    Serialises the Post to UTF-8 bytes and delegates to
    `atomic_write_bytes(..., mode_before_rename=0o600)`, which owns the
    tmp + fchmod-before-rename + fsync + rename + dir-fsync discipline and
    the orphan-tmp cleanup. The fchmod-before-rename keeps the file 0o600
    from the instant it appears at `path` — memory bodies are
    privacy-critical, so they must never be world-readable at the visible
    name even briefly (see `_fsutil.atomic_write_bytes` for the
    closed-window rationale and the platform/filesystem caveats).

    One definition of "durable private write" for every persistent write
    in the store: new memories, tombstones, restores, and rename_scope
    in-place edits all route through here.
    """
    atomic_write_bytes(
        path, frontmatter.dumps(post).encode("utf-8"), mode_before_rename=0o600
    )


def _as_dt(value: object) -> datetime:
    """Coerce a frontmatter value to an aware datetime.

    Three branches normalise to UTC-aware. The `datetime` branch covers
    PyYAML's native timestamp parsing (unquoted ISO strings round-trip
    as `datetime` objects, which may be naive when no offset was
    written); the bare-`date` branch covers a YAML *date-only* scalar
    (`created: 2025-01-01`), which PyYAML parses as a `datetime.date`
    (NOT a `datetime`, NOT a `str`); the `str` branch covers any value
    YAML preserved as a quoted string. Without coercion in the `str`
    branch a hand-edited file with `last_verified_at: "2025-01-01T10:00:00"`
    (quoted, no offset) loaded as a naive datetime, then crashed
    downstream on the first comparison against an aware `now` — surfaced
    by the audit on `health.compute_health`'s verification-debt partition.

    The bare-`date` branch closes a silent-data-loss path: before it
    existed, a date-only `created`/`updated` fell through to the
    `ValueError` below, which `_load_path`'s caller (`load_all`,
    `load_one`) catches and SKIPS — the whole memory vanished from
    every read surface with no warning. `datetime` IS a subclass of
    `date`, so the `datetime` check above must come first; this branch
    only fires for a *pure* date (midnight UTC is the natural lift).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # `datetime` is a subclass of `date`, so this must come AFTER the
    # `datetime` check — it catches only a bare YAML date scalar.
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        # Allow trailing 'Z'.
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _scope_intersect(memory_scopes: list[str], filter_scopes: list[str]) -> bool:
    """True if memory has at least one of the requested scopes."""
    return bool(set(memory_scopes) & set(filter_scopes))


def _id_still_at_path(path: Path, memory_id: str) -> bool:
    """Re-verify under-lock that `path` still carries a memory with
    `memory_id` in its frontmatter.

    Cheap recheck callers use after acquiring `_locked(path)` to
    detect a concurrent `tombstone()` (or `rename_scope`, or any
    other mutator that moves the file) that landed between
    `_find_path_for_id` and the lock acquisition. Returns False when
    the file vanished, when the frontmatter can't be parsed, or when
    the id no longer matches — any of which means the path no longer
    represents the same logical memory and the in-flight write must
    not proceed (it would resurrect a tombstoned memory by recreating
    an active file at the original path, orphaning the tombstone).

    Defensive against IO failures — a transient unreadable file is
    treated the same as a vanished one. Callers raise
    `MemoryNotFoundError` on False.
    """
    try:
        post = frontmatter.load(path)
    except (FileNotFoundError, ValueError, KeyError, OSError):
        return False
    return post.metadata.get("id") == memory_id


def _load_str_list(value: object) -> list[str]:
    """Coerce a frontmatter value to a list[str].

    Accepts None (legacy entry, no field) and missing keys via the
    `meta.get(...)` callsite, returning the empty list. Any non-list
    or non-string element is silently dropped — defensive against a
    hand-edited file that put `~` or a YAML alias in there. The
    write path emits well-formed lists; this is the symmetric "be
    liberal in what we read" policy.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]
