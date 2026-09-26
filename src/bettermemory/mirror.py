"""The export mirror: the v8 file format, written from the bettermemory 9
store.

The store (``bettermemory.store``) is canonical; markdown is the
export and import format. ``write_mirror`` lays a store out as a v8
directory, the greppable and git-able mirror the plan promised: each
active memory at its v8 filename, each tombstone under ``.tombstones/``,
each episode under ``episodes/<session_id>/``. The renderers produce the
bytes the v8 writers produced (``Store._write_path``, ``Store.tombstone``,
``EpisodeStore._write_path``), from the frontmatter shapes frozen in
``bettermemory.v8``, which is what makes a migrated store's mirror
byte-identical to the directory it came from; the tests pin each renderer
against the golden fixture those writers produced at 8.0.0
(``tests/fixtures/v8``).

A mirror is derived, so the target has rules. It must be absent, empty,
or a directory this module made before, which it marks with
``.mirror.json``; anything else is refused, and so a v8 store directory
can never be written over. Inside a mirror, a file whose bytes already
match is left alone, a changed or new one is written atomically at
owner-only mode, and a ``.md`` in the three places above that the run did
not write is removed, so the tree always says what the store says.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _frontmatter as frontmatter
from ._fsutil import atomic_write_bytes, ensure_owner_only_dir
from .models import Episode, Memory, MemoryLink, TombstonedMemory
from .store import STORE_FILENAME, Store
from .time_utils import isoformat_utc
from .v8 import (
    EPISODES_DIR,
    TOMBSTONE_DIR,
    active_filename_for_tombstone,
    episode_metadata,
    is_legacy_tombstone_name,
    memory_filename,
    memory_metadata,
    tombstone_filename,
)

MIRROR_MARKER = ".mirror.json"
_MARKER_VERSION = 1


class MirrorRefused(ValueError):
    """The target is not a directory this module may write into."""


# ---------------------------------------------------------------------------
# Renderers: the bytes the v8 writers wrote
# ---------------------------------------------------------------------------


def _dump(body: str, metadata: dict[str, object]) -> bytes:
    post = frontmatter.Post(body.strip() + "\n")
    post.metadata = metadata
    # The read cap, not the write cap: a record already admitted by v8 is
    # re-rendered as it is, headroom or none.
    return frontmatter.dumps(post, max_file_bytes=frontmatter._MAX_FILE_BYTES).encode(
        "utf-8"
    )


def render_memory(memory: Memory) -> bytes:
    """The active record's file, as ``Store._write_path`` wrote it."""
    return _dump(memory.body, memory_metadata(memory))


def render_tombstone(
    dead: TombstonedMemory,
    *,
    links: Sequence[MemoryLink | Mapping[str, Any]] = (),
    corroborations: int = 0,
    last_corroborated: datetime | None = None,
) -> bytes:
    """The tombstone's file, as ``Store.tombstone`` wrote it: the active
    record's frontmatter with the removal keys appended."""
    as_memory = Memory(
        **{
            name: getattr(dead, name)
            for name in Memory.model_fields
            if name not in ("links", "corroborations", "last_corroborated")
        },
        links=[MemoryLink.model_validate(link) for link in links],
        corroborations=corroborations,
        last_corroborated=last_corroborated,
    )
    metadata = memory_metadata(as_memory)
    metadata["removed"] = dead.removed
    metadata["removed_reason"] = dead.removed_reason
    if dead.removed_session is not None:
        metadata["removed_session"] = dead.removed_session
    return _dump(dead.body, metadata)


def render_episode(episode: Episode) -> bytes:
    """The episode's file, as ``EpisodeStore._write_path`` wrote it: the
    optional keys only when set, so a plain episode keeps the shape it
    had before those keys existed."""
    return _dump(episode.body, episode_metadata(episode))


# ---------------------------------------------------------------------------
# The mirror
# ---------------------------------------------------------------------------


@dataclass
class MirrorReport:
    target: str
    active: int = 0
    tombstones: int = 0
    episodes: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    seconds: float = 0.0
    files: list[str] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "active": self.active,
            "tombstones": self.tombstones,
            "episodes": self.episodes,
            "written": self.written,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "seconds": round(self.seconds, 3),
        }

    def render_text(self) -> str:
        return (
            f"Mirrored {self.active} active memories, {self.tombstones} tombstones "
            f"and {self.episodes} episodes to {self.target}: {self.written} written, "
            f"{self.unchanged} unchanged, {self.removed} removed "
            f"({self.seconds:.2f} s).\n"
        )


def _read_marker(target: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((target / MIRROR_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _check_target(store: Store, target: Path) -> None:
    store_dir = store.path.resolve().parent
    if target == store_dir or (target / STORE_FILENAME).exists():
        raise MirrorRefused(
            f"{target} holds the store itself; a mirror lives elsewhere"
        )
    if not target.exists():
        return
    if not target.is_dir():
        raise MirrorRefused(f"{target} is not a directory")
    if not any(target.iterdir()):
        return
    marker = _read_marker(target)
    if marker is None:
        raise MirrorRefused(
            f"{target} is not empty and was not written by `bettermemory export "
            f"--mirror` (no {MIRROR_MARKER}); pick an empty directory"
        )
    if marker.get("store_id") != store.store_id:
        raise MirrorRefused(
            f"{target} is the mirror of another store ({marker.get('store_id')!r})"
        )


def _write_marker(target: Path, store: Store, counts: Mapping[str, int]) -> None:
    payload = {
        "bettermemory_mirror": _MARKER_VERSION,
        "store_id": store.store_id,
        "store": str(store.path),
        "written_at": isoformat_utc(datetime.now(timezone.utc)),
        "counts": dict(counts),
    }
    atomic_write_bytes(
        target / MIRROR_MARKER,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode_before_rename=0o600,
    )


def _sweep(directory: Path, keep: set[Path]) -> int:
    """Remove the ``.md`` files in ``directory`` the run did not write."""
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".md":
            continue
        if entry in keep:
            continue
        entry.unlink()
        removed += 1
    return removed


def write_mirror(store: Store, target: Path | str) -> MirrorReport:
    """Write the store as a v8 directory under ``target``; see the module
    docstring for the target's rules and what a run leaves behind."""
    started = time.perf_counter()
    target = Path(target).expanduser().resolve()
    _check_target(store, target)
    ensure_owner_only_dir(target, parents=True)
    report = MirrorReport(target=str(target))
    counts = {"active": 0, "tombstones": 0, "episodes": 0}
    _write_marker(target, store, counts)

    written: set[Path] = set()

    def put(path: Path, data: bytes) -> None:
        written.add(path)
        report.files.append(str(path.relative_to(target)))
        try:
            if path.read_bytes() == data:
                report.unchanged += 1
                return
        except OSError:
            pass
        ensure_owner_only_dir(path.parent, parents=True)
        atomic_write_bytes(path, data, mode_before_rename=0o600)
        report.written += 1

    for row in store.iter_memory_rows():
        name = row.filename or memory_filename(row.memory)
        put(target / name, render_memory(row.memory))
        report.active += 1
    for dead in store.iter_tombstone_rows():
        active_name = dead.filename or memory_filename(
            Memory(
                **{
                    name: getattr(dead.tombstone, name)
                    for name in Memory.model_fields
                    if name not in ("links", "corroborations", "last_corroborated")
                }
            )
        )
        put(
            target / TOMBSTONE_DIR / tombstone_filename(active_name, dead.tombstone.id),
            render_tombstone(
                dead.tombstone,
                links=dead.links,
                corroborations=dead.corroborations,
                last_corroborated=dead.last_corroborated,
            ),
        )
        report.tombstones += 1
    for episode in store.iter_episodes():
        put(
            target / EPISODES_DIR / episode.session_id / f"{episode.id}.md",
            render_episode(episode),
        )
        report.episodes += 1

    report.removed += _sweep(target, written)
    report.removed += _sweep(target / TOMBSTONE_DIR, written)
    episodes_dir = target / EPISODES_DIR
    if episodes_dir.is_dir():
        for session_dir in episodes_dir.iterdir():
            if session_dir.is_symlink() or not session_dir.is_dir():
                continue
            report.removed += _sweep(session_dir, written)
            if not any(session_dir.iterdir()):
                session_dir.rmdir()

    counts = {
        "active": report.active,
        "tombstones": report.tombstones,
        "episodes": report.episodes,
    }
    _write_marker(target, store, counts)
    report.seconds = time.perf_counter() - started
    return report


__all__ = [
    "MIRROR_MARKER",
    "MirrorRefused",
    "MirrorReport",
    "active_filename_for_tombstone",
    "is_legacy_tombstone_name",
    "memory_filename",
    "render_episode",
    "render_memory",
    "render_tombstone",
    "tombstone_filename",
    "write_mirror",
]
