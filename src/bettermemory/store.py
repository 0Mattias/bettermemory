"""Filesystem operations for bettermemory.

Pure file I/O — no search logic, no MCP awareness. The store owns the layout
of the memory directory and the on-disk format; callers pass in `Memory`
objects and get them back.
"""

from __future__ import annotations

import contextlib
import errno
import os
from dataclasses import dataclass
from datetime import datetime
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
    build_filename,
    first_summary_line,
    generate_ulid,
    is_valid_ulid,
    make_slug,
    utcnow,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MemoryNotFoundError(KeyError):
    """No active memory with that ID."""


class TombstonedError(KeyError):
    """ID exists but the memory is tombstoned."""


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------
#
# fcntl.flock on Unix; on systems without fcntl we fall back to a no-op so the
# code at least runs. The MVP assumes single-process access — see README.


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - non-unix
        yield
        return

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
        """All active (non-tombstoned) memories. Sort by `created` desc."""
        memories: list[Memory] = []
        for path in self._iter_active_paths():
            try:
                memories.append(self._load_path(path))
            except (ValueError, KeyError):
                # Skip malformed files rather than refusing to start.
                # The model can still operate on the rest.
                continue
        memories.sort(key=lambda m: m.created, reverse=True)
        return memories

    def list_summaries(
        self, scopes: list[str] | None = None
    ) -> list[MemorySummary]:
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
            return Memory(
                id=str(meta["id"]),
                created=_as_dt(meta["created"]),
                updated=_as_dt(meta["updated"]),
                scopes=list(meta["scopes"]),
                confidence=Confidence(meta["confidence"]),
                source=Source(meta["source"]),
                body=post.content.strip() + "\n",
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

    def tombstone(self, memory_id: str, reason: str) -> Path:
        """Move a memory to `.tombstones/`, adding removal frontmatter."""
        path = self._find_path_for_id(memory_id)
        if path is None:
            # Maybe it's already tombstoned — bubble up a clearer error.
            for tpath in self._iter_tombstone_paths():
                post = frontmatter.load(tpath)
                if post.metadata.get("id") == memory_id:
                    raise TombstonedError(
                        f"memory {memory_id} is already tombstoned"
                    )
            raise MemoryNotFoundError(f"no memory with id {memory_id}")

        post = frontmatter.load(path)
        post.metadata["removed"] = utcnow()
        post.metadata["removed_reason"] = reason

        target = self.tombstone_dir / f"{path.stem}.tombstone.md"
        # If a same-named tombstone already exists, append the ULID for
        # uniqueness rather than overwriting history.
        if target.exists():
            target = self.tombstone_dir / (
                f"{path.stem}.{memory_id}.tombstone.md"
            )

        with _locked(path):
            target.write_bytes(
                frontmatter.dumps(post).encode("utf-8")
            )
            try:
                path.unlink()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
        return target

    # ---- internals --------------------------------------------------------

    def _path_for(self, memory: Memory) -> Path:
        slug = make_slug(memory.body)
        filename = build_filename(memory.created, slug)
        candidate = self.root / filename

        # Avoid clobbering: if filename collides (same date + slug), append a
        # short ID suffix.
        if candidate.exists():
            short = memory.id[-6:].lower()
            candidate = self.root / build_filename(
                memory.created, f"{slug}-{short}"
            )
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
        post.metadata = {
            "id": memory.id,
            "created": memory.created,
            "updated": memory.updated,
            "scopes": list(memory.scopes),
            "confidence": memory.confidence.value,
            "source": memory.source.value,
        }
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
