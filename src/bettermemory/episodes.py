"""Episode storage — sibling to `store.Store` for journal-shaped entries.

Episodes are the run-state / iteration-takeaway primitive Memory's
durability gate (`durability.TRANSIENT_PHRASE_MARKERS`) explicitly
rejects. They give /loop iterations and subagents a home for "what we
tried", "what worked", "what the prior iteration concluded" — content
that's transient by design but needs to survive one context reset.

On-disk layout::

    <root>/episodes/<session_id>/<ulid>.md

The session-id-keyed directory is what makes `episode_handoff` cheap
(list one dir, read takeaways, return) and `prune_old_sessions` cheap
(stat session dirs, drop ones whose newest mtime is past the TTL).

Episodes are deliberately excluded from `memory_search`,
`memory_health`, `memory_list`, and `Store.load_all` — they live in a
sibling subtree, so the existing iteration helpers never see them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from . import _frontmatter as frontmatter
from ._fsutil import fsync_dir, fsync_file
from .models import (
    Episode,
    SCHEMA_VERSION,
    generate_ulid,
    utcnow,
)
from .origin import Origin


EPISODES_DIR = "episodes"

# Default TTL for an episode directory. Sessions whose newest episode
# is older than this get pruned on the next write. 30 days is the same
# window `compute_health` uses for `window_days`, so the curation
# horizon stays consistent across primitives.
DEFAULT_EPISODE_TTL_DAYS = 30


@dataclass
class EpisodeStore:
    """An episode store rooted at `<root>/episodes/`.

    `root` is the memory root (same one `Store` uses). The episode
    subdirectory is created lazily on first write — a fresh install
    that never touches episodes incurs no directory creation.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        # Don't create the directory eagerly. `Store.__post_init__` already
        # made `root` exist; the episodes subdir is created on first write
        # so a fresh install with no episodes doesn't leave an empty dir.

    @property
    def episodes_dir(self) -> Path:
        return self.root / EPISODES_DIR

    def _session_dir(self, session_id: str) -> Path:
        # Filesystem-safe: ULID / session-id are alphanumeric + underscore.
        # Reject anything else so a hostile session_id can't traverse out
        # of the episodes subtree.
        if not session_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in session_id
        ):
            raise ValueError(f"invalid session_id for episode storage: {session_id!r}")
        return self.episodes_dir / session_id

    # ---- write ------------------------------------------------------------

    def write(
        self,
        *,
        session_id: str,
        body: str,
        scopes: list[str] | None = None,
        takeaway: str | None = None,
        origin: Origin | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """Append a new episode under `<root>/episodes/<session_id>/`."""
        if not body or not body.strip():
            raise ValueError("episode body must be a non-empty string")
        created = now or utcnow()
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=created,
            body=body.strip() + "\n",
            scopes=list(scopes or []),
            takeaway=takeaway.strip() if takeaway else None,
            origin=origin,
        )
        # Materialize the subdir on first write — the parent `episodes/`
        # gets the 0o700 treatment the tombstone directory does, since
        # episodes carry the same trust boundary as memories (origin
        # capture includes cwd, branch).
        self.episodes_dir.mkdir(mode=0o700, exist_ok=True)
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(mode=0o700, exist_ok=True)
        path = session_dir / f"{episode.id}.md"
        self._write_path(path, episode)
        return episode

    def _write_path(self, path: Path, episode: Episode) -> None:
        post = frontmatter.Post(episode.body.strip() + "\n")
        meta: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "id": episode.id,
            "session_id": episode.session_id,
            "created": episode.created,
        }
        if episode.scopes:
            meta["scopes"] = list(episode.scopes)
        if episode.takeaway is not None:
            meta["takeaway"] = episode.takeaway
        if episode.origin is not None:
            origin_dict = episode.origin.model_dump(mode="json", exclude_none=True)
            if origin_dict:
                meta["origin"] = origin_dict
        post.metadata = meta
        # Atomic + durable write: write to a per-process tmp file, fchmod
        # 0o600 on the open fd, fsync the file, rename into place, fsync
        # the parent directory. Mirrors `store._atomic_write_post`'s
        # discipline — reimplemented locally rather than importing a
        # private symbol from `store`, but the durability primitives are
        # the same.
        #
        # The rename is POSIX-atomic for the directory entry, but without
        # the file-fsync we can land a renamed-but-empty file on power
        # loss (the dirent exists, the page-cache bytes never reached
        # disk); without the dir-fsync the rename itself isn't durable
        # past a crash. Pre-fix this helper used `Path.write_text` +
        # `os.replace` with no fsyncs, which is exactly the zero-byte-on-
        # crash failure mode.
        #
        # Per-process tmp suffix via `NamedTemporaryFile` rather than a
        # deterministic `<path>.tmp` removes the secondary tmp-name
        # collision risk if two writers ever race on the same target.
        parent = path.parent
        tmp_file = tempfile.NamedTemporaryFile(
            dir=str(parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(tmp_file.name)
        renamed = False
        try:
            with tmp_file as f:
                f.write(frontmatter.dumps(post).encode("utf-8"))
                f.flush()
                # fchmod BEFORE the rename so the file is 0o600 the moment
                # it appears at `path`. Windows has no mode bits and
                # `os.fchmod` is missing from typeshed there, so guard on
                # `sys.platform`. Suppress OSError so sandbox filesystems
                # that reject fchmod don't break the write.
                if sys.platform != "win32":
                    with contextlib.suppress(OSError):
                        os.fchmod(f.fileno(), 0o600)
                fsync_file(f.fileno())
            os.replace(tmp_path, path)
            renamed = True
            # Defensive post-rename chmod: a no-op when fchmod succeeded
            # above, but recovers the mode if the filesystem dropped it
            # on rename (rare).
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            # `fsync_dir` no-ops on Windows; see `_fsutil.fsync_dir`.
            fsync_dir(parent)
        finally:
            if not renamed:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    # ---- read -------------------------------------------------------------

    def _iter_session_paths(self, session_id: str) -> Iterator[Path]:
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return
        for entry in session_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
                yield entry

    def list_by_session(self, session_id: str) -> list[Episode]:
        """All episodes for one session, oldest first (ULIDs sort by creation)."""
        out: list[Episode] = []
        for path in self._iter_session_paths(session_id):
            try:
                out.append(self._load_path(path))
            except (ValueError, KeyError, OSError):
                continue
        out.sort(key=lambda e: e.created)
        return out

    def iter_session_ids(self) -> Iterator[str]:
        """All session_ids that currently have an episode directory."""
        if not self.episodes_dir.exists():
            return
        for entry in self.episodes_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                yield entry.name

    def _load_path(self, path: Path) -> Episode:
        post = frontmatter.load(path)
        meta = post.metadata
        on_disk_version = meta.get("schema_version", 1)
        try:
            on_disk_int = int(on_disk_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{path}: schema_version is not an integer ({on_disk_version!r})"
            ) from exc
        if on_disk_int > SCHEMA_VERSION:
            raise ValueError(
                f"{path}: schema_version {on_disk_int} exceeds reader "
                f"max {SCHEMA_VERSION}; upgrade bettermemory"
            )
        origin_raw = meta.get("origin")
        origin_obj: Origin | None = None
        if isinstance(origin_raw, dict):
            origin_obj = Origin.model_validate(origin_raw)
        return Episode(
            id=str(meta["id"]),
            session_id=str(meta["session_id"]),
            created=meta["created"],
            body=post.content,
            scopes=list(meta.get("scopes", [])),
            takeaway=meta.get("takeaway"),
            origin=origin_obj,
        )

    # ---- prune ------------------------------------------------------------

    def prune_old_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        keep_session_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Drop session subdirectories whose newest episode is older
        than `ttl_days`. Returns the list of pruned session_ids.

        `keep_session_id`, when provided, is exempt from pruning even
        if its newest episode is past the TTL. Used by the write path
        to keep the active session's directory alive across a pause.
        """
        if ttl_days <= 0 or not self.episodes_dir.exists():
            return []
        cutoff = (now or utcnow()) - timedelta(days=ttl_days)
        cutoff_epoch = cutoff.timestamp()
        pruned: list[str] = []
        for session_dir in self.episodes_dir.iterdir():
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            session_name = session_dir.name
            if session_name == keep_session_id:
                continue
            newest_mtime = _newest_mtime_in_dir(session_dir)
            if newest_mtime is None:
                # Empty subdir — drop it.
                try:
                    session_dir.rmdir()
                    pruned.append(session_name)
                except OSError:
                    continue
                continue
            if newest_mtime < cutoff_epoch:
                try:
                    shutil.rmtree(session_dir)
                    pruned.append(session_name)
                except OSError:
                    continue
        return pruned


def _newest_mtime_in_dir(dir_path: Path) -> float | None:
    """Largest mtime over the directory's regular files. None when empty."""
    newest: float | None = None
    try:
        for entry in dir_path.iterdir():
            if entry.is_file() and not entry.is_symlink():
                mtime = entry.stat().st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
    except OSError:
        return None
    return newest


__all__ = [
    "DEFAULT_EPISODE_TTL_DAYS",
    "EPISODES_DIR",
    "EpisodeStore",
]
