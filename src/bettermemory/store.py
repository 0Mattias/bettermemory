"""Filesystem operations for bettermemory.

Pure file I/O — no search logic, no MCP awareness. The store owns the layout
of the memory directory and the on-disk format; callers pass in `Memory`
objects and get them back.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from . import _frontmatter as frontmatter
from ._fsutil import fsync_dir, fsync_file

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


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------
#
# fcntl.flock on Unix; on Windows there's no fcntl so we fall back to a no-op.
# The MVP assumes single-process access — see README. The sys.platform guard
# is the form mypy understands as platform narrowing; a try/except ImportError
# also works at runtime but mypy still type-checks the unreachable Windows
# path against the linux fcntl stubs.


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    if sys.platform == "win32":  # pragma: no cover - non-unix
        yield
        return

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()


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
        memory. Skips malformed / racing files like `load_all` does.

        Use this when the on-disk filename matters to the caller —
        notably `index.rebuild`, which needs the actual filename (not
        a re-derived one) so the `filename` column points at
        collision-suffixed files correctly."""
        for path in self._iter_active_paths():
            try:
                memory = self._load_path(path)
            except (ValueError, KeyError, OSError):
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
        for path in self._iter_tombstone_paths():
            post = frontmatter.load(path)
            if post.metadata.get("id") == memory_id:
                raise TombstonedError(
                    f"memory {memory_id} was removed: "
                    f"{post.metadata.get('removed_reason', '<no reason>')}"
                )

        raise MemoryNotFoundError(f"no memory with id {memory_id}")

    def _load_path(self, path: Path) -> Memory:
        post = frontmatter.load(path)
        meta = post.metadata
        # Schema-version gate. Memories without `schema_version` are
        # implicitly version 1 (the format predates the field). Anything
        # *higher* than what this reader supports is refused — the caller
        # (`load_all`, etc.) catches ValueError and skips the file with a
        # logged warning, so a user who downgrades bettermemory after
        # writing memories under a newer minor sees them drop out of the
        # retrieval surface rather than risk the reader misinterpreting
        # fields whose semantics changed.
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
                Origin.model_validate(origin_raw)
                if isinstance(origin_raw, dict)
                else None
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
                links=links,
            )
        except KeyError as exc:
            raise ValueError(f"{path}: missing field {exc.args[0]}") from exc

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
        _index_upsert_quietly(self.root, memory, filename=path.name)
        return memory

    def update(self, memory: Memory) -> Memory:
        """Overwrite an existing memory in place; bump `updated`."""
        existing_path = self._find_path_for_id(memory.id)
        if existing_path is None:
            raise MemoryNotFoundError(f"no memory with id {memory.id}")

        now = utcnow()
        new_memory = memory.model_copy(update={"updated": now})
        with _locked(existing_path):
            self._write_path(existing_path, new_memory)
        _index_upsert_quietly(self.root, new_memory, filename=existing_path.name)
        return new_memory

    def mark_verified(
        self,
        memory_id: str,
        *,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
    ) -> Memory:
        """Bump `last_verified_at` to now without touching `updated`.

        Verification is the orthogonal axis to content edits: a typo fix
        bumps `updated` (the body changed) but not `last_verified_at`
        (no claim to have spot-checked reality). A `memory_verify` call
        bumps `last_verified_at` (a human/agent confirmed reality matched
        the body) without bumping `updated` (the body itself didn't move).
        Calling this on a memory that's already verified-now is a no-op
        from the caller's perspective — the timestamp just slides forward.

        `verified_paths` / `verified_commits` / `verified_versions` carry
        the structured claims the caller attested. Passing None preserves
        whatever was previously stored (so a no-arg `mark_verified` keeps
        the prior attestation list); passing an explicit `[]` clears it.
        Passing a populated list replaces the prior list — verification
        is per-event, not append-only, and the event log is the audit
        trail for the history.
        """
        existing_path = self._find_path_for_id(memory_id)
        if existing_path is None:
            for tpath in self._iter_tombstone_paths():
                post = frontmatter.load(tpath)
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
            existing = self._load_path(existing_path)
            update: dict[str, object] = {"last_verified_at": utcnow()}
            if verified_paths is not None:
                update["verified_paths"] = list(verified_paths)
            if verified_commits is not None:
                update["verified_commits"] = list(verified_commits)
            if verified_versions is not None:
                update["verified_versions"] = list(verified_versions)
            new_memory = existing.model_copy(update=update)
            self._write_path(existing_path, new_memory)
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
        """
        path = self._find_path_for_id(memory_id)
        if path is None:
            # Maybe it's already tombstoned — bubble up a clearer error.
            for tpath in self._iter_tombstone_paths():
                post = frontmatter.load(tpath)
                if post.metadata.get("id") == memory_id:
                    raise TombstonedError(f"memory {memory_id} is already tombstoned")
            raise MemoryNotFoundError(f"no memory with id {memory_id}")

        target = self.tombstone_dir / f"{path.stem}.tombstone.md"
        # If a same-named tombstone already exists, append the ULID for
        # uniqueness rather than overwriting history.
        if target.exists():
            target = self.tombstone_dir / (f"{path.stem}.{memory_id}.tombstone.md")

        # Read + write under the same lock. Reading the body outside the
        # lock and writing the tombstone inside it leaves a window where
        # a concurrent `update` can land in between; we'd then write a
        # tombstone containing the pre-update body, losing the in-flight
        # edit silently.
        with _locked(path):
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
            except Exception:
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
              that id.
            NotTombstonedError: id is active. The caller probably meant
              `memory_update`; restore is only for tombstones.
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
            try:
                post = frontmatter.load(tombstone_path)
            except FileNotFoundError:
                raise MemoryNotFoundError(f"no tombstone with id {memory_id}") from None
            post.metadata.pop("removed", None)
            post.metadata.pop("removed_reason", None)
            post.metadata.pop("removed_session", None)

            # Reuse the active-side path-builder so a restore lands at
            # the same shape as a fresh write — date-prefixed slug
            # filename in the root, no `.tombstone.md` suffix. The slug
            # is regenerated from the body to handle the (unusual) case
            # where the original filename collided and got a short-id
            # suffix; the restored name may differ slightly but that's
            # fine — filenames are advisory, not part of the identity.
            try:
                created = _as_dt(post.metadata["created"])
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"{tombstone_path}: cannot restore — missing/invalid created"
                ) from exc
            slug = make_slug(post.content)
            active_filename = build_filename(created, slug)
            active_path = self.root / active_filename
            if active_path.exists():
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
                    try:
                        post = frontmatter.load(tpath)
                    except Exception:
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
        for path in self._iter_tombstone_paths():
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
            pruned.append((tombstone.removed, tombstone.id))

        pruned.sort(key=lambda item: item[0])
        return [memory_id for _, memory_id in pruned]

    # ---- internals --------------------------------------------------------

    def _path_for(self, memory: Memory) -> Path:
        slug = make_slug(memory.body)
        filename = build_filename(memory.created, slug)
        candidate = self.root / filename

        # Avoid clobbering: if filename collides (same date + slug), append a
        # short ID suffix.
        if candidate.exists():
            short = memory.id[-6:].lower()
            candidate = self.root / build_filename(memory.created, f"{slug}-{short}")
        return candidate

    def _find_path_for_id(self, memory_id: str) -> Path | None:
        if not is_valid_ulid(memory_id):
            return None
        for path in self._iter_active_paths():
            try:
                post = frontmatter.load(path)
            except Exception:
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
    store directly)."""
    try:
        from . import index as _index

        _index.upsert(root, memory, filename=filename)
    except Exception as exc:  # noqa: BLE001 — never break the write
        import logging

        logging.getLogger("bettermemory.store").warning(
            "index upsert failed for %s: %s. Run `bettermemory reindex` to repair.",
            memory.id,
            exc,
        )


def _index_remove_quietly(root: Path, memory_id: str) -> None:
    """Drop one memory from the FTS5 index. Same best-effort contract
    as the upsert: never block the on-disk tombstone on an index
    failure."""
    try:
        from . import index as _index

        _index.remove(root, memory_id)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("bettermemory.store").warning(
            "index remove failed for %s: %s. Run `bettermemory reindex` to repair.",
            memory_id,
            exc,
        )


def _atomic_write_post(path: Path, post: frontmatter.Post) -> None:
    """Atomic, durable write of a frontmatter Post to `path`.

    Write-to-tmp, fsync the file, rename into place, fsync the parent
    directory. The rename alone is POSIX-atomic for the directory entry,
    but without fsync on the file we can end up with a renamed-but-empty
    file after power loss (the entry exists, the bytes never reached
    disk); without fsync on the directory the rename itself isn't
    durable past a crash. Both fsyncs are best-effort — see `_fsutil`
    for the platform/filesystem caveats.

    Mode 0o600 (owner read/write only) is set after the rename. Without
    this, files inherit the user umask — typically 0o644 on Linux/macOS,
    so memory content ends up world-readable on shared-user boxes.
    The lock-file path already uses 0o600 (see `_locked`); this brings
    the data path in line. Windows ignores the bits silently.

    One helper, one definition of "durable write" for every persistent
    write in the store: new memories, tombstones, restores, and
    rename_scope in-place edits all share this pattern.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = frontmatter.dumps(post).encode("utf-8")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        fsync_file(f.fileno())
    tmp.replace(path)
    # chmod after rename so a partially-written tmp file never sits at
    # the target path with relaxed permissions. `os.chmod` is a no-op
    # for the bits beyond the platform's permission model.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    fsync_dir(path.parent)


def _as_dt(value: object) -> datetime:
    """Coerce a frontmatter value to an aware datetime.

    Both branches normalise to UTC-aware. The `datetime` branch covers
    PyYAML's native timestamp parsing (unquoted ISO strings round-trip
    as `datetime` objects, which may be naive when no offset was
    written); the `str` branch covers any value YAML preserved as a
    quoted string. Without coercion in the `str` branch a hand-edited
    file with `last_verified_at: "2025-01-01T10:00:00"` (quoted, no
    offset) loaded as a naive datetime, then crashed downstream on the
    first comparison against an aware `now` — surfaced by the audit on
    `health.compute_health`'s verification-debt partition.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            from datetime import timezone

            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        # Allow trailing 'Z'.
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _scope_intersect(memory_scopes: list[str], filter_scopes: list[str]) -> bool:
    """True if memory has at least one of the requested scopes."""
    return bool(set(memory_scopes) & set(filter_scopes))


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
