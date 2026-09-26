"""The v8 store's file formats, frozen: the readers the migration uses and
the metadata order the mirror writes.

bettermemory 8 kept a store as a directory of files. bettermemory 9 keeps
it in one SQLite file (``store``), and the modules that ran the
directory (``store``, ``index``, ``events``, ``episodes``, ``conflicts``,
``quarantine``) are retired. Two commands still need the directory's
formats: ``bettermemory migrate v8`` (``migrate_v8``) reads one into the
store, and ``bettermemory export --mirror`` (``mirror``) writes the store
back out as one, byte for byte. This module holds what they need, copied
from the v8 modules as they stood at 8.0.0. It reads files and renders
frontmatter; it never writes, locks, indexes or records.

The layout under a store root::

    <date>-<slug>-<ulid>.md                       an active memory
    .tombstones/<stem>.<ULID>.tombstone.md        a removed memory
    episodes/<session_id>/<ULID>.md               a journal entry
    .events.NN.jsonl                              an active event shard
    .events-<ts>-sNN[-<session>[-<k>]].jsonl.gz   a rotated event archive
    .events.jsonl                                 the pre-sharding event log
    .conflicts.jsonl                              the conflict queue
    .quarantine.json                              files a sync pull refused
    .ingest-watermark.json                        the ingest provenance
    .pending_writes.jsonl                         staged writes (transient)
    .write_proposals.jsonl                        write proposals (transient)
    .episode_patterns.jsonl                       pattern dismissals
    .index.sqlite                                 the derived search index

Memories, tombstones and episodes are markdown files with a YAML
frontmatter block, read and written through ``_frontmatter``, whose dump
sorts the keys. Which keys a file carries, and in what shape, is the
writers' decision, reproduced by ``memory_metadata`` and
``episode_metadata``; that is what lets a record rendered from the store
come out with the bytes the v8 writer produced. Events are one JSON
object per line; the readers here tolerate every corruption the v8
readers tolerated and merge the sources in the same order. The golden
fixture under ``tests/fixtures/v8`` is a directory the v8 writers produced
at 8.0.0; the tests pin the readers and the renderers against its files.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import os
import re
import zlib
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from . import _frontmatter as frontmatter
from .conflicts import ConflictCandidate
from .identity import SOURCE_PROCESS_CWD, Actor
from .models import (
    SCHEMA_VERSION,
    Category,
    Confidence,
    Episode,
    Memory,
    MemoryLink,
    Source,
    TombstonedMemory,
    build_filename,
    make_slug,
)
from .origin import Origin, is_full_commit_sha
from .time_utils import ensure_utc, parse_event_ts

log = logging.getLogger("bettermemory.v8")


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------

TOMBSTONE_DIR = ".tombstones"
EPISODES_DIR = "episodes"
INDEX_FILENAME = ".index.sqlite"

EVENT_LOG_FILENAME = ".events.jsonl"
ARCHIVE_PREFIX = ".events-"
ARCHIVE_SUFFIX = ".jsonl.gz"
# A rotation renamed the active segment to this holding name, gzipped it
# into the archive, then unlinked it. A holding file with no matching
# archive is a rotation that crashed before compression: the events' only
# copy.
ROTATING_SUFFIX = ".jsonl.rotating"
# The active log was striped over a fixed set of per-shard files,
# `.events.00.jsonl` to `.events.15.jsonl`, a session's shard being a
# stable crc32 of its id. Stores that predate sharding keep their single
# `.events.jsonl`, one more source the readers merge in.
SHARD_COUNT = 16
SEGMENT_TEMPLATE = ".events.{:02d}.jsonl"

CONFLICTS_FILENAME = ".conflicts.jsonl"
QUARANTINE_FILENAME = ".quarantine.json"
QUARANTINE_REASONS: tuple[str, ...] = (
    "credential",
    "oversize",
    "unparseable",
    "id_alias",
)
INGEST_WATERMARK_FILENAME = ".ingest-watermark.json"
PENDING_WRITES_FILENAME = ".pending_writes.jsonl"
PROPOSALS_FILENAME = ".write_proposals.jsonl"
PATTERNS_FILENAME = ".episode_patterns.jsonl"

# What a per-file read may raise and the walks skip: any parse failure
# (a malformed file, a schema_version newer than this reader, a missing
# field), a file that vanished between the listing and the read, and an
# I/O error on one file. The v8 readers skipped on the same width so that
# one bad file never blinded a read of the rest of the store.
PARSE_SKIP_EXCEPTIONS: tuple[type[Exception], ...] = (Exception,)


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
# Frontmatter values
# ---------------------------------------------------------------------------


def _as_dt(value: object) -> datetime:
    """Coerce a frontmatter value to an aware datetime.

    Three shapes normalise to UTC-aware. The `datetime` branch covers
    PyYAML's native timestamp parsing (an unquoted ISO string round-trips
    as a `datetime`, naive when no offset was written); the bare-`date`
    branch covers a date-only scalar (`created: 2025-01-01`), which PyYAML
    parses as a `datetime.date`; the `str` branch covers a value YAML
    preserved as a quoted string. `datetime` is a subclass of `date`, so
    the `datetime` check comes first."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _load_commit_sha(value: object) -> str | None:
    """A frontmatter `verified_head`, or None unless it is a full commit
    hash. A branch name, an abbreviation or an option-shaped string is
    dropped on read; the record then reads as verified without an
    anchor."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if is_full_commit_sha(candidate) else None


def _load_str_list(value: object) -> list[str]:
    """Coerce a frontmatter value to a list[str]. None (a legacy record
    without the field) and a missing key read as the empty list; a
    non-list, or a non-scalar element, is dropped rather than raised on,
    the liberal-reader policy for a hand-edited file."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _coerce_scopes(value: object) -> list[str]:
    """Coerce a frontmatter `scopes` value the way the v8 readers did: a
    list passes through, a dict or set gives its keys or elements, a
    scalar string is one scope, anything else is no scope (the model then
    rejects the record, so the file is skipped)."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (dict, set, frozenset)):
        return list(value)
    if isinstance(value, str):
        return [value]
    return []


def _schema_version_gate(meta: dict[str, Any], path: Path) -> None:
    """Refuse a file whose `schema_version` is newer than this reader.
    A file without the key is version 1, the format predating it."""
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


def _optional_dt(meta: dict[str, Any], key: str) -> datetime | None:
    """An additive timestamp field: absent reads as None, and a malformed
    value reads as None too, so a typo never makes the file unloadable."""
    raw = meta.get(key)
    if raw is None:
        return None
    try:
        return _as_dt(raw)
    except ValueError:
        return None


def _optional_category(meta: dict[str, Any]) -> Category | None:
    """`category` is additive and an unknown value reads as None rather
    than raising, so a record written by a newer bettermemory still loads
    under an older reader with the legacy fact semantics."""
    category_raw = meta.get("category")
    if category_raw is None:
        return None
    try:
        return Category(str(category_raw))
    except ValueError:
        return None


def _record_fields(meta: dict[str, Any], body: str) -> dict[str, Any]:
    """The constructor fields a memory and a tombstone share, read the
    same way from the same frontmatter: `origin`, `actor`,
    `last_verified_at`, `category` and the verified-claims lists are all
    additive, so a record written before any of them existed loads with
    the defaults."""
    origin_raw = meta.get("origin")
    actor_raw = meta.get("actor")
    return {
        "id": str(meta["id"]),
        "created": _as_dt(meta["created"]),
        "updated": _as_dt(meta["updated"]),
        "scopes": _coerce_scopes(meta["scopes"]),
        "confidence": Confidence(meta["confidence"]),
        "source": Source(meta["source"]),
        "body": body.strip() + "\n",
        "origin": (
            Origin.model_validate(origin_raw) if isinstance(origin_raw, dict) else None
        ),
        "actor": (
            Actor.model_validate(actor_raw) if isinstance(actor_raw, dict) else None
        ),
        "last_verified_at": _optional_dt(meta, "last_verified_at"),
        "category": _optional_category(meta),
        "verified_paths": _load_str_list(meta.get("verified_paths")),
        "verified_commits": _load_str_list(meta.get("verified_commits")),
        "verified_versions": _load_str_list(meta.get("verified_versions")),
        "verified_absent_paths": _load_str_list(meta.get("verified_absent_paths")),
        "claims": _load_str_list(meta.get("claims")),
        "verified_head": _load_commit_sha(meta.get("verified_head")),
    }


def parse_memory_file(path: Path) -> Memory:
    """Parse one memory file into a `Memory`, as `Store._load_path` did.

    Raises `ValueError` on a schema_version newer than this reader, a
    missing required field or an unparseable timestamp; the walks skip
    such a file. The additive fields degrade rather than raise: `links`
    entries that are not a `{type, target_id[, note]}` mapping with a
    known type are dropped, and a malformed corroboration rollup reads as
    0 / None. A tombstone file parses too, its removal keys ignored, which
    is how the migration reads the links and the rollup the tombstone
    model leaves out."""
    post = frontmatter.load(path)
    meta = post.metadata
    _schema_version_gate(meta, path)
    try:
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
        corroborations_raw = meta.get("corroborations")
        try:
            corroborations = (
                max(0, int(corroborations_raw)) if corroborations_raw is not None else 0
            )
        except (TypeError, ValueError):
            corroborations = 0
        return Memory(
            **_record_fields(meta, post.content),
            links=links,
            corroborations=corroborations,
            last_corroborated=_optional_dt(meta, "last_corroborated"),
        )
    except KeyError as exc:
        raise ValueError(f"{path}: missing field {exc.args[0]}") from exc


def parse_tombstone_file(path: Path) -> TombstonedMemory:
    """Parse one tombstone file into a `TombstonedMemory`, as
    `Store._load_tombstone_path` did. A tombstone is the active record's
    file with `removed`, `removed_reason` and, since the field shipped,
    `removed_session` appended; the model drops the links and the
    corroboration rollup, which `parse_memory_file` reads from the same
    file."""
    post = frontmatter.load(path)
    meta = post.metadata
    _schema_version_gate(meta, path)
    try:
        removed_session = meta.get("removed_session")
        return TombstonedMemory(
            **_record_fields(meta, post.content),
            removed=_as_dt(meta["removed"]),
            removed_reason=str(meta["removed_reason"]),
            removed_session=(
                str(removed_session) if removed_session is not None else None
            ),
        )
    except KeyError as exc:
        raise ValueError(f"{path}: missing field {exc.args[0]}") from exc


# ---------------------------------------------------------------------------
# The walks
# ---------------------------------------------------------------------------


def iter_active_memory_paths(root: Path) -> Iterator[Path]:
    """The active memory files under `root`: regular, non-symlink,
    top-level `.md` files that are not quarantined, in directory order.

    A symlink is rejected without being followed: with `sync pull` the
    directory was a worktree a remote could push to, and a pushed
    `note.md -> /etc/passwd` must not be read. The link test runs before
    the file test because `is_file` follows the link and could raise from
    wherever it pointed. A quarantined name (a pulled file the admission
    chain refused) is on disk and tracked, and is not a memory.

    A root that does not exist yields nothing: a store never provisioned
    is an empty store. Every other `OSError` propagates, so "cannot read"
    is never folded into "absent"."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return
    excluded = quarantined_names(root)
    for entry in entries:
        if entry.is_symlink() or not os.path.isfile(entry):
            continue
        if entry.suffix == ".md":
            if entry.name in excluded:
                continue
            yield entry


def iter_tombstone_paths(root: Path) -> Iterator[Path]:
    """The tombstone files under `root/.tombstones`, in directory order,
    with the same symlink rule as the active walk."""
    tombstone_dir = root / TOMBSTONE_DIR
    if not tombstone_dir.exists():
        return
    for entry in tombstone_dir.iterdir():
        if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
            yield entry


def iter_active(root: Path | str) -> Iterator[tuple[Path, Memory]]:
    """`(path, memory)` for every active memory, in the order and with
    the skips of `Store.iter_active`: directory order, a file the parser
    rejects left out. This is the order a v8 rebuild indexed the store
    in, so the migration inserts in it and the rowids agree with the
    index."""
    for path in iter_active_memory_paths(Path(root)):
        try:
            memory = parse_memory_file(path)
        except PARSE_SKIP_EXCEPTIONS:
            continue
        yield path, memory


def iter_tombstones(root: Path | str) -> Iterator[tuple[Path, TombstonedMemory]]:
    """`(path, tombstone)` for every tombstone the parser accepts, in
    directory order. `Store.load_tombstones` sorted these by `removed`,
    newest first; a caller that wants that order sorts."""
    for path in iter_tombstone_paths(Path(root)):
        try:
            dead = parse_tombstone_file(path)
        except PARSE_SKIP_EXCEPTIONS:
            continue
        yield path, dead


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


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

_SESSION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def session_dir(root: Path, session_id: str) -> Path:
    """`root/episodes/<session_id>`. A session id is alphanumeric plus
    `_` and `-`, and anything else is refused, so an id can never name a
    directory outside the episodes subtree."""
    if not session_id or any(c not in _SESSION_ID_CHARS for c in session_id):
        raise ValueError(f"invalid session_id for episode storage: {session_id!r}")
    return root / EPISODES_DIR / session_id


def parse_episode_file(path: Path) -> Episode:
    """Parse one episode file into an `Episode`, as
    `EpisodeStore._load_path` did. The body is the file's content as
    read, without the trailing newline the writer added. `scopes`,
    `takeaway`, `swarm_id` and `is_floor` are additive and coerced
    liberally (a scalar `scopes` reads as none, a numeric `takeaway` as
    its string form); a `created` that is missing or unparseable raises
    `ValueError`, the skip-this-row signal."""
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
    is_floor = bool(meta.get("is_floor", False))
    swarm_raw = meta.get("swarm_id")
    swarm_id = str(swarm_raw) if swarm_raw is not None else None
    scopes_raw = meta.get("scopes")
    scopes = [str(s) for s in scopes_raw] if isinstance(scopes_raw, list) else []
    takeaway_raw = meta.get("takeaway")
    takeaway = str(takeaway_raw) if takeaway_raw is not None else None
    created_raw = meta.get("created")
    created: datetime
    if isinstance(created_raw, datetime):
        normalised = ensure_utc(created_raw)
        assert normalised is not None
        created = normalised
    elif isinstance(created_raw, date):
        created = datetime(
            created_raw.year, created_raw.month, created_raw.day, tzinfo=timezone.utc
        )
    elif isinstance(created_raw, str):
        parsed = parse_event_ts(created_raw)
        if parsed is None:
            raise ValueError(f"{path}: 'created' is not a parseable timestamp")
        created = parsed
    else:
        raise ValueError(f"{path}: 'created' is missing, null, or unparseable")
    return Episode(
        id=str(meta["id"]),
        session_id=str(meta["session_id"]),
        created=created,
        body=post.content,
        scopes=scopes,
        takeaway=takeaway,
        swarm_id=swarm_id,
        origin=origin_obj,
        is_floor=is_floor,
    )


def _iter_session_paths(root: Path, session_id: str) -> Iterator[Path]:
    directory = session_dir(root, session_id)
    if not directory.exists():
        return
    for entry in directory.iterdir():
        if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
            yield entry


def list_by_session(root: Path | str, session_id: str) -> list[Episode]:
    """All episodes of one session, oldest first, as
    `EpisodeStore.list_by_session` returned them. A file the parser
    rejects is skipped silently; a file this process could not read is
    skipped with a warning."""
    out: list[Episode] = []
    for path in _iter_session_paths(Path(root), session_id):
        try:
            out.append(parse_episode_file(path))
        except (ValueError, KeyError):
            continue
        except OSError as exc:
            log.warning("episode %s could not be read: %s", path, exc)
            continue
    out.sort(key=lambda e: e.created)
    return out


def iter_session_ids(root: Path | str) -> Iterator[str]:
    """Every session id that has an episode directory."""
    episodes_dir = Path(root) / EPISODES_DIR
    if not episodes_dir.exists():
        return
    for entry in episodes_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            yield entry.name


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

# Trailing `-N` collision counter on a rotated segment's stem. `[1-9]\d*`
# rather than `\d+`: a generated counter starts at 1 and never carries a
# leading zero, while a zero-padded session id (`sess-00028`) is ordinary
# and must not read as a counter.
_TRAILING_COUNTER_RE = re.compile(r"-([1-9]\d*)$")
# Shard tag on a rotated segment's stem: `s{NN}` right after the
# timestamp. Absent on pre-3.25 archives, whose shard is unknown.
_SHARD_TAG_RE = re.compile(r"^s(\d{2})(?:-(.*))?$")


def _rotated_stem(name: str) -> str:
    """The `{ts}[-s{NN}][-{session}[-{counter}]]` body of a rotated
    segment's name, the prefix and the suffix stripped."""
    suffix = ROTATING_SUFFIX if name.endswith(ROTATING_SUFFIX) else ARCHIVE_SUFFIX
    return name[len(ARCHIVE_PREFIX) : -len(suffix)]


def _parse_rotated_name(name: str) -> tuple[str, int | None, int]:
    """`(ts, shard, in-second write order)` for a rotated segment name.

    Shapes produced since 3.25 (shard-partitioned rotation names):

        .events-{ts}-s{NN}.jsonl.gz                  -> (ts, NN, 0)
        .events-{ts}-s{NN}-{session}.jsonl.gz        -> (ts, NN, 1)
        .events-{ts}-s{NN}-{session}-{k}.jsonl.gz    -> (ts, NN, 1+k)

    Legacy shapes keep parsing with `shard = None`:

        .events-{ts}.jsonl.gz                        -> (ts, None, 0)
        .events-{ts}-{session}.jsonl.gz              -> (ts, None, 1)
        .events-{ts}-{session}-{k}.jsonl.gz          -> (ts, None, 1+k)

    The write order is a hint, not a fact: a session id whose own tail
    is `-{digits}` reads as a counter, and the index counts one
    session's rotations within a second, never the order between
    sessions. `_segments_in_write_order` resolves what the name cannot."""
    ts, _, remainder = _rotated_stem(name).partition("-")
    shard: int | None = None
    if remainder:
        tag = _SHARD_TAG_RE.match(remainder)
        if tag is not None:
            shard = int(tag.group(1))
            remainder = tag.group(2) or ""
    if not remainder:
        return (ts, shard, 0)
    counter = _TRAILING_COUNTER_RE.search(remainder)
    if counter is not None:
        return (ts, shard, 1 + int(counter.group(1)))
    return (ts, shard, 1)


def _rotated_segment_shard(path: Path) -> int | None:
    """Shard a rotated segment came from, or None when it predates the
    shard-tagged naming."""
    return _parse_rotated_name(path.name)[1]


def _parse_event_line(raw: bytes) -> dict[str, Any] | None:
    """Decode and parse one event-log line; None for anything that is not
    a JSON object: a blank line, an invalid-UTF-8 byte (decoded with
    replacement, so the line fails to parse and is dropped), or valid
    JSON that is not an object."""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _iter_json_lines(f: Any) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a binary line stream, degrading per
    record: a truncated gzip archive (`EOFError`) or a CRC-corrupt one
    (`zlib.error`) still yields its readable prefix."""
    while True:
        try:
            raw = f.readline()
        except (OSError, EOFError, zlib.error):
            return
        if not raw:
            return
        event = _parse_event_line(raw)
        if event is None:
            continue
        yield event


_TS_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _event_ts_key(event: dict[str, Any]) -> datetime:
    """Merge key: the event's parsed `ts`, or a UTC-min sentinel for a
    missing or unparseable one, so a corrupt stamp sorts first rather
    than raising inside the merge."""
    ts = parse_event_ts(event.get("ts"))
    return ts if ts is not None else _TS_MIN


def _active_segment_paths(root: Path) -> list[Path]:
    """Every active event segment on disk: a legacy pre-sharding
    `.events.jsonl` if present, then each per-shard segment that
    exists."""
    paths: list[Path] = []
    legacy = root / EVENT_LOG_FILENAME
    if legacy.exists():
        paths.append(legacy)
    for shard in range(SHARD_COUNT):
        segment = root / SEGMENT_TEMPLATE.format(shard)
        if segment.exists():
            paths.append(segment)
    return paths


def _merge_active_segments(paths: list[Path]) -> Iterator[dict[str, Any]]:
    """heapq-merge the active segments by event `ts`. A segment that
    cannot be opened is skipped."""
    if not paths:
        return
    handles: list[Any] = []
    try:
        streams: list[Iterator[dict[str, Any]]] = []
        for path in paths:
            try:
                f = path.open("rb")
            except OSError:
                continue
            handles.append(f)
            streams.append(_iter_json_lines(f))
        yield from heapq.merge(*streams, key=_event_ts_key)
    finally:
        for handle in handles:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass


def _archive_sort_key(path: Path) -> tuple[str, int, str]:
    """`(ts, write order, name)` for a rotated segment: the timestamp
    stamped in the name (fixed-width `%Y%m%dT%H%M%SZ`, so lexicographic
    is chronological), then the in-second index, then the name itself so
    the order is total. Derived from names alone, no `stat`."""
    ts, _, order = _parse_rotated_name(path.name)
    return (ts, order, path.name)


def _segment_mtime_ns(path: Path) -> int:
    """`st_mtime_ns` of a rotated segment, or 0 when it cannot be
    stat'd, which sorts it to the front of its second."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _segments_in_write_order(paths: Iterable[Path]) -> list[Path]:
    """Rotated segments, oldest rotation first. Segments whose names
    carry different seconds are ordered by name; segments that share a
    second are re-ranked on `st_mtime_ns`, the only evidence of which
    was written first, with the name as the tiebreak. A group of one is
    never stat'd."""
    ordered = sorted(paths, key=_archive_sort_key)
    resolved: list[Path] = []
    for _, group in groupby(ordered, key=lambda p: _parse_rotated_name(p.name)[0]):
        same_second = list(group)
        if len(same_second) > 1:
            same_second.sort(key=lambda p: (_segment_mtime_ns(p), _archive_sort_key(p)))
        resolved.extend(same_second)
    return resolved


def _rotated_segments(root: Path) -> list[Path]:
    """Every rotated segment worth reading, unordered: the `.jsonl.gz`
    archives plus any `.rotating` holding file with no matching archive
    (a rotation that crashed before compression). A holding file whose
    archive exists is a stale duplicate and is left out."""
    try:
        entries = list(root.iterdir())
    except OSError:  # pragma: no cover
        return []
    segments = [p for p in entries if p.name.startswith(ARCHIVE_PREFIX)]
    archives = [p for p in segments if p.name.endswith(ARCHIVE_SUFFIX) and p.is_file()]
    archive_stems = {p.name[: -len(ARCHIVE_SUFFIX)] for p in archives}
    return archives + [
        p
        for p in segments
        if p.name.endswith(ROTATING_SUFFIX)
        and p.name[: -len(ROTATING_SUFFIX)] not in archive_stems
        and p.is_file()
    ]


def _iter_segment(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the events of one rotated segment (`.jsonl.gz` or
    `.rotating`); an unreadable one contributes nothing."""
    if path.name.endswith(ARCHIVE_SUFFIX):
        try:
            with gzip.open(path, "rb") as gz:
                yield from _iter_json_lines(gz)
        except (OSError, EOFError, zlib.error):
            log.warning("events: skipping unreadable archive %s", path.name)
        return
    try:
        rf = path.open("rb")
    except OSError:  # pragma: no cover
        return
    with rf:
        yield from _iter_json_lines(rf)


def _iter_segment_chain(paths: list[Path]) -> Iterator[dict[str, Any]]:
    """Yield the events of an ordered run of rotated segments, opening
    one at a time."""
    for path in paths:
        yield from _iter_segment(path)


def iter_all_events(root: Path) -> Iterator[dict[str, Any]]:
    """Every event in the directory, in chronological order, as the v8
    `iter_all_events` yielded them.

    Streams merged by `ts`, in tie-break priority: one chain per shard
    over that shard's archives in rotation order (a shard rotates its
    own segment wholesale, so the chain is chronological), one chain for
    untagged pre-3.25 archives (best effort, since the shard that cut
    each is unknown), one stream per orphan `.rotating` holding file, and
    the active segments. Equal timestamps resolve toward the earlier
    stream, so a rotated segment precedes the active tail it was cut
    from. The active segments are listed first so a concurrent rotation
    cannot hide events from both listings."""
    if not root.exists():
        return
    active_paths = _active_segment_paths(root)
    by_shard: dict[int, list[Path]] = {}
    untagged: list[Path] = []
    orphans: list[Path] = []
    for path in _rotated_segments(root):
        if path.name.endswith(ROTATING_SUFFIX):
            orphans.append(path)
            continue
        shard = _rotated_segment_shard(path)
        if shard is None:
            untagged.append(path)
        else:
            by_shard.setdefault(shard, []).append(path)
    streams: list[Iterator[dict[str, Any]]] = []
    for shard in sorted(by_shard):
        streams.append(_iter_segment_chain(_segments_in_write_order(by_shard[shard])))
    if untagged:
        streams.append(_iter_segment_chain(_segments_in_write_order(untagged)))
    for orphan in _segments_in_write_order(orphans):
        streams.append(_iter_segment(orphan))
    streams.append(_merge_active_segments(active_paths))
    yield from heapq.merge(*streams, key=_event_ts_key)


# ---------------------------------------------------------------------------
# Sidecars
# ---------------------------------------------------------------------------


def load_conflicts(root: Path | str) -> list[ConflictCandidate]:
    """Every row of the conflict queue, in file order, as
    `ConflictQueue.load` returned them: one JSON object per line, a line
    that does not parse to an object with an `id`, or that the candidate
    model rejects, skipped."""
    path = Path(root) / CONFLICTS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[ConflictCandidate] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        try:
            out.append(ConflictCandidate.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _safe_filename(name: Any) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
    )


def load_quarantine(root: Path | str) -> dict[str, dict[str, Any]]:
    """The quarantine sidecar's entries by filename, each the mapping the
    file holds (`reason`, `detail`, `remote`, `pulled_at`, `size`,
    `sha256`) with the values coerced as the v8 reader coerced them. The
    v8 reader wrapped each in a dataclass; the keys and the count are
    what the migration needs. Never raises: an absent sidecar is the
    common case and reads as empty, and an unreadable or misshapen one
    reads as empty with a warning. An entry with an unknown reason, or a
    filename that could reach outside the root, is dropped."""
    path = Path(root) / QUARANTINE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("quarantine sidecar %s is unreadable: %s", path, exc)
        return {}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        log.warning("quarantine sidecar %s is not valid JSON: %s", path, exc)
        return {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        log.warning("quarantine sidecar %s has an unexpected shape", path)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for filename, raw in files.items():
        if not _safe_filename(filename) or not isinstance(raw, dict):
            continue
        reason = raw.get("reason")
        if reason not in QUARANTINE_REASONS:
            continue
        size = raw.get("size")
        sha256 = raw.get("sha256")
        out[filename] = {
            "reason": reason,
            "detail": str(raw.get("detail") or ""),
            "remote": str(raw.get("remote") or ""),
            "pulled_at": str(raw.get("pulled_at") or ""),
            "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
            "sha256": sha256 if isinstance(sha256, str) else None,
        }
    return out


def quarantined_names(root: Path) -> frozenset[str]:
    """The filenames the active walk skips. One `stat` when no sidecar
    exists, which is every store that never quarantined anything."""
    if not (root / QUARANTINE_FILENAME).exists():
        return frozenset()
    return frozenset(load_quarantine(root))


def load_watermark_sources(root: Path | str) -> dict[str, dict[str, Any]]:
    """The per-source entry map of the ingest watermark (`key ->
    {content_hash, memory_id, ...}`); `{}` when the file is missing,
    unreadable, corrupt or shaped wrong. Never raises: every failure
    reads as "no provenance recorded", the reading the v8 module gave a
    hand-mangled sidecar."""
    try:
        raw = (Path(root) / INGEST_WATERMARK_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in sources.items():
        if isinstance(key, str) and isinstance(entry, dict):
            out[key] = entry
    return out


__all__ = [
    "ARCHIVE_PREFIX",
    "ARCHIVE_SUFFIX",
    "CONFLICTS_FILENAME",
    "EPISODES_DIR",
    "EPISODE_METADATA_KEYS",
    "EVENT_LOG_FILENAME",
    "INDEX_FILENAME",
    "INGEST_WATERMARK_FILENAME",
    "MEMORY_METADATA_KEYS",
    "PARSE_SKIP_EXCEPTIONS",
    "PATTERNS_FILENAME",
    "PENDING_WRITES_FILENAME",
    "PROPOSALS_FILENAME",
    "QUARANTINE_FILENAME",
    "QUARANTINE_REASONS",
    "ROTATING_SUFFIX",
    "SEGMENT_TEMPLATE",
    "SHARD_COUNT",
    "TOMBSTONE_DIR",
    "TOMBSTONE_METADATA_KEYS",
    "TOMBSTONE_SUFFIX",
    "active_filename_for_tombstone",
    "episode_metadata",
    "is_legacy_tombstone_name",
    "iter_active",
    "iter_active_memory_paths",
    "iter_all_events",
    "iter_session_ids",
    "iter_tombstone_paths",
    "iter_tombstones",
    "list_by_session",
    "load_conflicts",
    "load_quarantine",
    "load_watermark_sources",
    "memory_filename",
    "memory_metadata",
    "parse_episode_file",
    "parse_memory_file",
    "parse_tombstone_file",
    "quarantined_names",
    "session_dir",
    "tombstone_filename",
]
