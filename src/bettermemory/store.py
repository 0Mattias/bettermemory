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
    Confidence,
    Memory,
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
        (self.root / TOMBSTONE_DIR).mkdir(exist_ok=True)

    @property
    def tombstone_dir(self) -> Path:
        return self.root / TOMBSTONE_DIR

    # ---- iteration --------------------------------------------------------

    def _iter_active_paths(self) -> Iterator[Path]:
        for entry in self.root.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                yield entry

    def _iter_tombstone_paths(self) -> Iterator[Path]:
        if not self.tombstone_dir.exists():
            return
        for entry in self.tombstone_dir.iterdir():
            if entry.is_file() and entry.suffix == ".md":
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
    ) -> Memory:
        """Create a new memory. Generates ID, slug, filename."""
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
        )
        path = self._path_for(memory)
        with _locked(path):
            self._write_path(path, memory)
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
        return new_memory

    def mark_verified(self, memory_id: str) -> Memory:
        """Bump `last_verified_at` to now without touching `updated`.

        Verification is the orthogonal axis to content edits: a typo fix
        bumps `updated` (the body changed) but not `last_verified_at`
        (no claim to have spot-checked reality). A `memory_verify` call
        bumps `last_verified_at` (a human/agent confirmed reality matched
        the body) without bumping `updated` (the body itself didn't move).
        Calling this on a memory that's already verified-now is a no-op
        from the caller's perspective — the timestamp just slides forward.
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

        existing = self._load_path(existing_path)
        new_memory = existing.model_copy(update={"last_verified_at": utcnow()})
        with _locked(existing_path):
            self._write_path(existing_path, new_memory)
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

        post = frontmatter.load(path)
        post.metadata["removed"] = utcnow()
        post.metadata["removed_reason"] = reason
        # Only emit the field when a session_id was passed — keeps legacy
        # tests and ad-hoc callers from getting an opaque `None` lying in
        # frontmatter. The reader treats missing-or-None identically.
        if session_id is not None:
            post.metadata["removed_session"] = session_id

        target = self.tombstone_dir / f"{path.stem}.tombstone.md"
        # If a same-named tombstone already exists, append the ULID for
        # uniqueness rather than overwriting history.
        if target.exists():
            target = self.tombstone_dir / (f"{path.stem}.{memory_id}.tombstone.md")

        with _locked(path):
            target.write_bytes(frontmatter.dumps(post).encode("utf-8"))
            try:
                path.unlink()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
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

        post = frontmatter.load(tombstone_path)
        post.metadata.pop("removed", None)
        post.metadata.pop("removed_reason", None)
        post.metadata.pop("removed_session", None)

        # Reuse the active-side path-builder so a restore lands at the
        # same shape as a fresh write — date-prefixed slug filename in
        # the root, no `.tombstone.md` suffix. The slug is regenerated
        # from the body to handle the (unusual) case where the original
        # filename collided and got a short-id suffix; the restored
        # name may differ slightly but that's fine — filenames are
        # advisory, not part of the identity.
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

        with _locked(active_path):
            active_path.write_bytes(frontmatter.dumps(post).encode("utf-8"))
            try:
                tombstone_path.unlink()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    # Active file already written; orphaning the tombstone
                    # is recoverable but we still want to surface the IO
                    # error so the caller doesn't think the move was clean.
                    raise

        return self._load_path(active_path)

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
            try:
                memory = self._load_path(path)
            except (ValueError, KeyError):
                continue
            new_scopes = self._scopes_after_rename(memory.scopes, old, new)
            if new_scopes is None:
                continue
            # Bump `updated` because the metadata moved. `last_verified_at`
            # is preserved — the body's claims are untouched, so the
            # verification (if any) still applies.
            refreshed = memory.model_copy(
                update={"scopes": new_scopes, "updated": utcnow()}
            )
            with _locked(path):
                self._write_path(path, refreshed)
            active_changed.append(memory.id)

        tombstoned_changed: list[str] = []
        if include_tombstones:
            for tpath in self._iter_tombstone_paths():
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
                with _locked(tpath):
                    tpath.write_bytes(frontmatter.dumps(post).encode("utf-8"))
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
        post.metadata = meta
        # Atomic-ish write: write to .tmp then rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(frontmatter.dumps(post).encode("utf-8"))
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_dt(value: object) -> datetime:
    """Coerce a frontmatter value to an aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            from datetime import timezone

            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        # Allow trailing 'Z'.
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    raise ValueError(f"cannot parse datetime from {value!r}")


def _scope_intersect(memory_scopes: list[str], filter_scopes: list[str]) -> bool:
    """True if memory has at least one of the requested scopes."""
    return bool(set(memory_scopes) & set(filter_scopes))
