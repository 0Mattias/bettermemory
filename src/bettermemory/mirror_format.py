"""The v8 file names and frontmatter the mirror writes.

bettermemory 8 kept a store as a directory of markdown files.
``bettermemory export --mirror`` (``mirror``) writes a bettermemory 9
store back out as one, byte for byte, and ``bettermemory migrate v8``
(``migrate_v8``) reads one in through the frozen readers in ``v8``. This
module holds what the mirror writes and the migration shares: where
tombstones and episodes live, the names the v8 writers gave a record's
file and its tombstone, and the frontmatter mapping each writer
persisted, copied from the v8 modules as they stood at 8.0.0. The mirror
is a 9.x export and stays when ``v8`` and ``migrate_v8`` go with the 8.x
import, so nothing here imports them: they import these names from here
(``tests/test_v8_import_graph.py``).
"""

from __future__ import annotations

import re

from .identity import SOURCE_PROCESS_CWD
from .models import SCHEMA_VERSION, Episode, Memory, build_filename, make_slug

# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------

TOMBSTONE_DIR = ".tombstones"
EPISODES_DIR = "episodes"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

TOMBSTONE_SUFFIX = ".tombstone.md"
# The name `Store.tombstone` wrote: the active stem, the id, the suffix.
# Before 2.6.4 the id was left out; those names still load by their id.
_ID_TOMBSTONE_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<id>[0-9A-HJKMNP-TV-Z]{26})\.tombstone\.md$"
)


def memory_filename(memory: Memory) -> str:
    """The name `Store.write` gave a new record: the date, the slug and the
    full lowercase id. The id is embedded whole so the name is unique by
    construction; older files with a short or no id suffix still load,
    since the readers key off the `id` field, never the name."""
    return build_filename(
        memory.created, f"{make_slug(memory.body)}-{memory.id.lower()}"
    )


def tombstone_filename(active_filename: str, memory_id: str) -> str:
    """The name `Store.tombstone` gave a removed record's file."""
    stem = active_filename[:-3] if active_filename.endswith(".md") else active_filename
    return f"{stem}.{memory_id}{TOMBSTONE_SUFFIX}"


def active_filename_for_tombstone(name: str) -> str:
    """The active filename a tombstone's name was made from, the id and
    the suffix stripped; a name that is not a tombstone's is returned as
    it is."""
    match = _ID_TOMBSTONE_RE.match(name)
    if match is not None:
        return match.group("stem") + ".md"
    if name.endswith(TOMBSTONE_SUFFIX):
        return name[: -len(TOMBSTONE_SUFFIX)] + ".md"
    return name


def is_legacy_tombstone_name(name: str) -> bool:
    """A tombstone named before 2.6.4, without the id."""
    return name.endswith(TOMBSTONE_SUFFIX) and _ID_TOMBSTONE_RE.match(name) is None


# ---------------------------------------------------------------------------
# Frontmatter the writers produced
# ---------------------------------------------------------------------------

# The keys a memory file can carry, in the order the writer assembled
# them (the dump then sorts them). The first seven are always present;
# the rest only when set, so a record with nothing optional keeps the
# shape it had before those fields existed.
MEMORY_METADATA_KEYS: tuple[str, ...] = (
    "schema_version",
    "id",
    "created",
    "updated",
    "scopes",
    "confidence",
    "source",
    "origin",
    "actor",
    "last_verified_at",
    "category",
    "corroborations",
    "last_corroborated",
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
    "claims",
    "verified_head",
    "links",
)
# A tombstone appends these to the active record's keys.
TOMBSTONE_METADATA_KEYS: tuple[str, ...] = (
    "removed",
    "removed_reason",
    "removed_session",
)
# The keys an episode file can carry, in the order the writer assembled
# them (the dump then sorts them). The first four are always present;
# `scopes`, `takeaway`, `origin`, `is_floor` and `swarm_id` only when set.
EPISODE_METADATA_KEYS: tuple[str, ...] = (
    "schema_version",
    "id",
    "session_id",
    "created",
    "scopes",
    "takeaway",
    "origin",
    "is_floor",
    "swarm_id",
)


def memory_metadata(memory: Memory) -> dict[str, object]:
    """The frontmatter mapping `Store._write_path` persisted for `memory`
    (its keys are `MEMORY_METADATA_KEYS`). Optional fields are left out
    while unset: no `last_verified_at: null`, no empty lists, no zero
    rollup, so a plain record's file is the pre-field shape. The
    process-cwd origin `source` stays implicit, being the only channel
    that existed before the field; a declared channel (`header`, `env`,
    `roots`) is written. `actor` is written only when it carries
    something. Each link is a plain mapping with the enum's value as its
    `type`."""
    meta: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "id": memory.id,
        "created": memory.created,
        "updated": memory.updated,
        "scopes": list(memory.scopes),
        "confidence": memory.confidence.value,
        "source": memory.source.value,
    }
    if memory.origin is not None:
        origin_dict = memory.origin.model_dump(mode="json", exclude_none=True)
        if origin_dict.get("source") == SOURCE_PROCESS_CWD:
            del origin_dict["source"]
        if origin_dict:
            meta["origin"] = origin_dict
    if memory.actor is not None:
        actor_dict = memory.actor.to_record()
        if actor_dict:
            meta["actor"] = actor_dict
    if memory.last_verified_at is not None:
        meta["last_verified_at"] = memory.last_verified_at
    if memory.category is not None:
        meta["category"] = memory.category.value
    if memory.corroborations:
        meta["corroborations"] = memory.corroborations
    if memory.last_corroborated is not None:
        meta["last_corroborated"] = memory.last_corroborated
    if memory.verified_paths:
        meta["verified_paths"] = list(memory.verified_paths)
    if memory.verified_commits:
        meta["verified_commits"] = list(memory.verified_commits)
    if memory.verified_versions:
        meta["verified_versions"] = list(memory.verified_versions)
    if memory.verified_absent_paths:
        meta["verified_absent_paths"] = list(memory.verified_absent_paths)
    if memory.claims:
        meta["claims"] = list(memory.claims)
    if memory.verified_head:
        meta["verified_head"] = memory.verified_head
    if memory.links:
        meta["links"] = [
            {
                "type": link.type.value,
                "target_id": link.target_id,
                **({"note": link.note} if link.note is not None else {}),
            }
            for link in memory.links
        ]
    return meta


def episode_metadata(episode: Episode) -> dict[str, object]:
    """The frontmatter mapping `EpisodeStore._write_path` persisted for
    `episode` (its keys are `EPISODE_METADATA_KEYS`). The optional keys
    appear only when set: `is_floor` only on a floor, `swarm_id` only on
    a swarm member, so a plain episode keeps the shape it had before
    those keys existed."""
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
    if episode.is_floor:
        meta["is_floor"] = True
    if episode.swarm_id is not None:
        meta["swarm_id"] = episode.swarm_id
    return meta


__all__ = [
    "EPISODES_DIR",
    "EPISODE_METADATA_KEYS",
    "MEMORY_METADATA_KEYS",
    "TOMBSTONE_DIR",
    "TOMBSTONE_METADATA_KEYS",
    "TOMBSTONE_SUFFIX",
    "active_filename_for_tombstone",
    "episode_metadata",
    "is_legacy_tombstone_name",
    "memory_filename",
    "memory_metadata",
    "tombstone_filename",
]
