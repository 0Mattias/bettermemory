"""The vocabulary and the schema `store.py` builds its tables from.

The schema version and page size, the provenance labels, the folded and
membership tables with their mutation and event kinds and the specs the
membership rows are checked against, the row types the reads return
(`MemoryRow`, `TombstoneRow`, `TrustRow`, `EpisodeVolume`, `RecordHome`,
`StandingAnswer`), the `SCHEMA`, `MEMBERSHIP_SCHEMA` and `ADDITIVE_SCHEMA`
texts with the additive step that brings an older store up to them, the
store's errors, and the paths a store opens by (`store_path`, the saved
orphans, the v8 directory check, `resolve_keys_dir`). `store` re-exports
every name a caller reads. It lives apart from `store.py` to keep each
module under the package's 2,000-line bound (ENG-96), at the bottom of
the store family: it imports no other store module.
"""

from __future__ import annotations

import glob
import re
import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from . import log as _chain
from .config import KEYS_DIR_ENV, STORE_FILENAME
from .log import CONTROL_KINDS
from .models import Memory, TombstonedMemory

log = logging.getLogger("bettermemory.store")


# `STORE_FILENAME` is defined in `config` so the standard-library-only
# daemon client can name the file without importing the store.
SCHEMA_VERSION = 1

# Set on a store's first open, before its first table and before WAL mode
# fixes it. A record row (body, its token stream, the JSON columns) runs
# past the largest payload a 4 KB page keeps in line, so at 4 KB most rows
# spill into an overflow page of their own; 8 KB keeps them in line and
# measured 16 percent smaller on the rank-parity corpus at the same write
# cost, where 16 KB saved nothing more and cost a third on every write
# (bench/parity/results/sqlite-candidates-8.0.0-2026-09-26.json records
# both sizes beside the parity).
PAGE_SIZE = 8192

# What `Store.create` writes as meta's `store_id`: 16 random bytes, hex.
# The key ring names its files after it (`<store_id>.key`, the retired
# keys, the head), and meta is only what the file says, so `open` refuses
# any other shape: an id holding a path would have the ring read a key and
# a head from wherever the path points, the store directory included,
# which a writer holding the store file but not the key can fill with a
# key of its own, and every row it signed would then verify.
_STORE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

# A daemon whose store path was removed or replaced saves the store it
# had open beside the path as `<store>.orphaned-<UTC stamp>.sqlite`
# (`daemon.save_orphan`). A store created while one or more sit beside it
# names them in `meta` under this key, as a JSON list of file names, so
# the notice that it started empty (`Store.store_orphans`) lasts until
# those files are deleted or the old store is copied back over this one.
# A store created at the path before the orphan was saved gets the name
# added by the daemon that saved it.
CREATED_AFTER_ORPHANS = "created_after_orphans"

# How a record entered the store. `local`: written through this store's
# own code path. `imported`: brought in by the migration or an import.
# `synced`: arrived from another machine (phase 3). `unaccounted`: a row
# the fold does not produce; reported by `log_verify`, never written.
LOCAL = "local"
IMPORTED = "imported"
SYNCED = "synced"
UNACCOUNTED = "unaccounted"
PROVENANCE_LABELS = frozenset({LOCAL, IMPORTED, SYNCED, UNACCOUNTED})
_WRITABLE_PROVENANCE = frozenset({LOCAL, IMPORTED, SYNCED})

# The tables a replay of the log reproduces, and the log kinds that
# mutate them: one put and one delete per table. `memory_links` is
# derived from each memory's link list and folded with it.
FOLDED_TABLES = (
    "memories",
    "memory_links",
    "tombstones",
    "episodes",
    "verifications",
    "conflicts",
    "imports",
    "quarantine",
)

# The membership tables (design 8.5, decision D9): additive beside the
# record tables, folded by the same replay. Each is a `_MembershipTable`
# below; its mutation kinds are `<kind>_put` and `<kind>_delete`. They are
# kept apart from `FOLDED_TABLES`, whose eight names the record fold
# reports under `tables`; `refold` reports these under `membership`.
MEMBERSHIP_TABLES = (
    "machines",
    "projects",
    "project_witnesses",
    "project_blobs",
    "project_remotes",
    "project_files",
    "declared_remotes",
    "checkouts",
    "checkout_views",
    "homes",
    "proposals",
    "standing",
)
_MEMBERSHIP_MUTATION_TABLES = (
    "machine",
    "project",
    "project_witness",
    "project_blob",
    "project_remote",
    "project_file",
    "declared_remote",
    "checkout",
    "checkout_view",
    "home",
    "proposal",
    "standing",
)

_MUTATION_TABLES = (
    "memory",
    "tombstone",
    "episode",
    "verification",
    "conflict",
    "import",
    "quarantine",
    *_MEMBERSHIP_MUTATION_TABLES,
)
MUTATION_KINDS = frozenset(
    f"{table}_{op}" for table in _MUTATION_TABLES for op in ("put", "delete")
)

# The membership observability rows (design 8.10, and `store_forked` of
# design 8.7): control rows of their own, written whatever the telemetry
# setting, MAC'd and chained like every row, never read as telemetry
# events. They change no table, so the fold passes over them.
MEMBERSHIP_EVENT_KINDS = frozenset(
    {
        "resolution_indeterminate",
        "resolution_deferred",
        "resolution_session",
        "resolution_joined",
        "resolution_new",
        "proposal_opened",
        "proposal_answered",
        "checkout_registered",
        "checkout_moved",
        "checkout_view_added",
        "checkout_rekeyed",
        "checkout_object_refreshed",
        "checkout_retired",
        "token_superseded",
        "token_reminted",
        "remote_declared",
        "project_minted",
        "project_merged",
        "project_absorbed",
        "project_unmerged",
        "project_file_bound",
        "project_reparented",
        "machine_cloned",
        "machine_facts_updated",
        "witness_put",
        "witness_released",
        "import_homed",
        "recall_suppressed",
        "membership_migrated",
        "store_forked",
    }
)
MEMBERSHIP_MUTATION_KINDS = frozenset(
    f"{table}_{op}" for table in _MEMBERSHIP_MUTATION_TABLES for op in ("put", "delete")
)


# Every kind the telemetry readers pass over: mutations, control rows and
# the membership rows.
_NOT_TELEMETRY = MUTATION_KINDS | CONTROL_KINDS | MEMBERSHIP_EVENT_KINDS

# Key columns per folded table, for the fold's row comparison.
_TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "memories": ("id",),
    "memory_links": ("source_id", "type", "target_id", "note"),
    "tombstones": ("id",),
    "episodes": ("id",),
    "verifications": ("memory_id", "machine_id"),
    "conflicts": ("id",),
    "imports": ("source",),
    "quarantine": ("name",),
    "machines": ("id",),
    "projects": ("id",),
    "project_witnesses": ("project", "commit_id"),
    "project_blobs": ("project", "blob"),
    "project_remotes": ("project", "remote_key"),
    "project_files": ("file_id",),
    "declared_remotes": ("remote_key",),
    "checkouts": ("id",),
    "checkout_views": ("checkout", "machine", "path"),
    "homes": ("record_id",),
    "proposals": ("id",),
    "standing": ("record_id",),
}


@dataclass(frozen=True)
class _MembershipTable:
    """One membership table as the mutation rows carry it: the SQL table,
    the kind prefix of its rows, its key and every column in order. A
    column whose name ends in ``_json`` holds a JSON value in the payload
    and its canonical text in the table. ``optional`` columns came after
    the table: a row written before one reads it as null, and the
    additive step adds it to a table that lacks it. ``floats`` are the
    columns that hold a finite float; no other column takes one."""

    table: str
    kind: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    optional: tuple[str, ...] = ()
    floats: tuple[str, ...] = ()


_MEMBERSHIP_SPECS: dict[str, _MembershipTable] = {
    spec.kind: spec
    for spec in (
        _MembershipTable(
            "machines",
            "machine",
            ("id",),
            ("id", "facts_json", "predecessor", "minted"),
        ),
        _MembershipTable(
            "projects",
            "project",
            ("id",),
            (
                "id",
                "name",
                "merged_into",
                "provisional",
                "parent",
                "tenant",
                "folder",
                "boundary",
                "minted",
            ),
        ),
        _MembershipTable(
            "project_witnesses",
            "project_witness",
            ("project", "commit_id"),
            ("project", "commit_id", "arrival", "source", "fingerprint"),
        ),
        _MembershipTable(
            "project_blobs",
            "project_blob",
            ("project", "blob"),
            ("project", "blob", "witness"),
        ),
        _MembershipTable(
            "project_remotes",
            "project_remote",
            ("project", "remote_key"),
            ("project", "remote_key"),
        ),
        _MembershipTable(
            "project_files",
            "project_file",
            ("file_id",),
            ("file_id", "project", "evidence"),
        ),
        _MembershipTable(
            "declared_remotes",
            "declared_remote",
            ("remote_key",),
            ("remote_key", "project", "principal"),
        ),
        _MembershipTable(
            "checkouts",
            "checkout",
            ("id",),
            (
                "id",
                "project",
                "machine",
                "kind",
                "object_device",
                "object_inode",
                "object_birth",
                "common_device",
                "common_inode",
                "common_birth",
                "git_dir",
                "token",
                "token_status",
                "stamps_json",
                "last_head",
                "branch",
                "remotes_json",
                "manifest_json",
                "legacy",
                "retired",
                "registered",
                "unresolved_json",
            ),
            optional=("unresolved_json",),
        ),
        _MembershipTable(
            "checkout_views",
            "checkout_view",
            ("checkout", "machine", "path"),
            ("checkout", "machine", "path", "first_seen", "last_seen", "seen_order"),
        ),
        _MembershipTable(
            "homes",
            "home",
            ("record_id",),
            ("record_id", "kind", "project", "checkout", "rule", "decided"),
        ),
        _MembershipTable(
            "proposals",
            "proposal",
            ("id",),
            (
                "id",
                "checkout",
                "kind",
                "candidates_json",
                "evidence_json",
                "reason",
                "line",
                "state",
                "answer",
                "answered_by",
                "signature_json",
                "opened",
            ),
        ),
        # A record's standing answer (unit RS-F7 part 1, `standing.py`):
        # additive beside the record like its home, and folded the same
        # way. Not a membership decision; it shares the machinery.
        _MembershipTable(
            "standing",
            "standing",
            ("record_id",),
            (
                "record_id",
                "probability",
                "body_sha256",
                "source",
                "answered_by",
                "answered",
            ),
            floats=("probability",),
        ),
    )
}
_MEMBERSHIP_BY_TABLE: dict[str, _MembershipTable] = {
    spec.table: spec for spec in _MEMBERSHIP_SPECS.values()
}

# The meta key of the store id of design 8.7 (decision D16): minted with
# the store, carried by exports, replaced when an import shows the store
# was copied whole and both copies wrote (`store_forked`). It is not the
# `store_id` the key files are named by, which a fork leaves as it is.
MEMBERSHIP_STORE_ID = "membership_store_id"


class StandingAnswer(NamedTuple):
    """A standing answer a put writes beside its record (unit RS-F7 part
    1): the probability from 0 to 1 that the record directs future work,
    where it came from (``standing.SOURCES``) and who gave it."""

    probability: float
    source: str
    answered_by: str


class RecordHome(NamedTuple):
    """The home a memory or an episode is written with (design 2.1): the
    project id (``global`` outside any repository and any project file),
    the checkout id, and the rule of the ladder that decided it."""

    project: str
    checkout: str | None
    rule: str


_CONFLICT_COLUMNS = (
    "id",
    "a_id",
    "b_id",
    "summary_a",
    "summary_b",
    "similarity",
    "method",
    "detector",
    "created",
    "status",
    "verdict_ts",
    "note",
    "verdict_hash_a",
    "verdict_hash_b",
)
_QUARANTINE_COLUMNS = ("name", "reason", "detail", "remote", "at", "size", "sha256")
_IMPORT_COLUMNS = ("source", "content_hash", "imported_at", "memory_id")
_VERIFICATION_LISTS = (
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
)

_PROVENANCE_BATCH = 500

# What an imported row's payload says about where it came from. The v8
# migration is the one importer today; a later `import` names itself.
IMPORTED_FROM_V8 = "v8"


class TrustRow(NamedTuple):
    """One record's trust facts the record itself cannot supply: how it
    entered the store (the verified provenance label) and when this host
    last verified it (None: never, or not since it arrived from
    elsewhere)."""

    provenance: str
    verified_locally_at: str | None


@dataclass(frozen=True)
class EpisodeVolume:
    """How big the journal is: the growth gauge the health report
    carries. ``prunable_sessions`` counts the sessions whose newest
    episode is past the TTL, which the next ``prune_episode_sessions``
    takes."""

    sessions: int
    episodes: int
    bytes: int
    prunable_sessions: int
    ttl_days: int

    def to_dict(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "episodes": self.episodes,
            "bytes": self.bytes,
            "prunable_sessions": self.prunable_sessions,
            "ttl_days": self.ttl_days,
        }


# How long a journal session lives before the write path prunes it.
DEFAULT_EPISODE_TTL_DAYS = 30


def _scopes_after_rename(scopes: list[str], old: str, new: str) -> list[str] | None:
    """The scope list with `old` replaced by `new` and duplicates collapsed,
    or None when `old` is not present."""
    if old not in scopes:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for scope in scopes:
        name = new if scope == old else scope
        if name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


@dataclass(frozen=True)
class MemoryRow:
    """An active record with the two columns the record itself does not
    carry: how it entered the store and the v8 filename it keeps."""

    memory: Memory
    provenance: str
    filename: str | None


@dataclass(frozen=True)
class TombstoneRow:
    """A tombstone with what the record carried while active and the
    tombstone model drops: its links, its corroboration rollup, and the
    active filename the mirror derives the tombstone's name from."""

    tombstone: TombstonedMemory
    provenance: str
    filename: str | None
    links: list[dict[str, Any]]
    corroborations: int
    last_corroborated: datetime | None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# `memories` opens with the v8 index's columns in the v8 order, minus
# `verified_locally_at` (this host's stamps are `verifications` rows
# now) and `content_sha256` (the log covers integrity), then carries the
# rest of the record and, last, `log_mac`: the MAC of the log row that
# produced the row, which `provenance_for` verifies on every read so a
# row that entered outside the log reads `unaccounted` without a refold.
# Tombstones and episodes carry the same column. The FTS5 table, its three triggers, the `updated`
# index, `memory_links` and its cleanup trigger are the v8 index's
# statements verbatim; tests/test_store.py compares the text.

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    body_fts TEXT NOT NULL DEFAULT '',
    scopes_text TEXT NOT NULL,
    scopes_fts TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT,
    log_mac TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    body_fts, scopes_fts,
    content='memories', content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;

CREATE INDEX IF NOT EXISTS memories_by_updated ON memories(updated DESC);

CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (source_id, type, target_id, note)
);

CREATE INDEX IF NOT EXISTS memory_links_by_target ON memory_links(target_id);

CREATE TRIGGER IF NOT EXISTS memory_links_cleanup AFTER DELETE ON memories BEGIN
    DELETE FROM memory_links
    WHERE source_id = old.id OR target_id = old.id;
END;

CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT,
    removed TEXT NOT NULL,
    removed_reason TEXT NOT NULL,
    removed_session TEXT,
    log_mac TEXT
);

CREATE INDEX IF NOT EXISTS tombstones_by_removed ON tombstones(removed);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created TEXT NOT NULL,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    takeaway TEXT,
    origin_json TEXT,
    is_floor INTEGER NOT NULL DEFAULT 0,
    swarm_id TEXT,
    log_mac TEXT
);

CREATE INDEX IF NOT EXISTS episodes_by_session ON episodes(session_id, created);

CREATE TABLE IF NOT EXISTS log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_mac TEXT NOT NULL,
    mac TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS log_by_kind ON log(kind, seq);
CREATE INDEX IF NOT EXISTS log_by_ts ON log(ts);
CREATE INDEX IF NOT EXISTS log_by_mac ON log(mac);

CREATE TABLE IF NOT EXISTS verifications (
    memory_id TEXT NOT NULL,
    machine_id TEXT,
    verified_at TEXT NOT NULL,
    verified_head TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS verifications_by_memory_machine
    ON verifications(memory_id, COALESCE(machine_id, ''));

CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    a_id TEXT NOT NULL,
    b_id TEXT NOT NULL,
    summary_a TEXT NOT NULL,
    summary_b TEXT NOT NULL,
    similarity REAL NOT NULL,
    method TEXT NOT NULL,
    detector TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict_ts TEXT,
    note TEXT,
    verdict_hash_a TEXT,
    verdict_hash_b TEXT
);

CREATE TABLE IF NOT EXISTS imports (
    source TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    memory_id TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
    name TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    detail TEXT,
    remote TEXT,
    at TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT
);
"""

# The membership tables and their indexes (design 8.5). Additive: a store
# created by an earlier build gets them at its first open by this one
# (`_add_membership_tables`), and an earlier build opening a store that
# has them reads its own tables as before. Object keys are text, so a
# 64-bit NTFS file index or a 128-bit ReFS id fits as it is.
MEMBERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL DEFAULT '{}',
    predecessor TEXT,
    minted TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    merged_into TEXT,
    provisional TEXT,
    parent TEXT,
    tenant TEXT,
    folder INTEGER NOT NULL DEFAULT 0,
    boundary INTEGER NOT NULL DEFAULT 0,
    minted TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS projects_by_merged_into ON projects(merged_into);

CREATE TABLE IF NOT EXISTS project_witnesses (
    project TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    arrival INTEGER NOT NULL,
    source TEXT NOT NULL,
    fingerprint TEXT,
    PRIMARY KEY (project, commit_id)
);

CREATE INDEX IF NOT EXISTS project_witnesses_by_commit ON project_witnesses(commit_id);
CREATE INDEX IF NOT EXISTS project_witnesses_by_fingerprint
    ON project_witnesses(fingerprint);

CREATE TABLE IF NOT EXISTS project_blobs (
    project TEXT NOT NULL,
    blob TEXT NOT NULL,
    witness TEXT NOT NULL,
    PRIMARY KEY (project, blob)
);

CREATE INDEX IF NOT EXISTS project_blobs_by_witness ON project_blobs(project, witness);

CREATE TABLE IF NOT EXISTS project_remotes (
    project TEXT NOT NULL,
    remote_key TEXT NOT NULL,
    PRIMARY KEY (project, remote_key)
);

CREATE INDEX IF NOT EXISTS project_remotes_by_key ON project_remotes(remote_key);

CREATE TABLE IF NOT EXISTS project_files (
    file_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    evidence TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS project_files_by_project ON project_files(project);

CREATE TABLE IF NOT EXISTS declared_remotes (
    remote_key TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    principal TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkouts (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    machine TEXT NOT NULL,
    kind TEXT NOT NULL,
    object_device TEXT,
    object_inode TEXT,
    object_birth TEXT,
    common_device TEXT,
    common_inode TEXT,
    common_birth TEXT,
    git_dir TEXT,
    token TEXT,
    token_status TEXT NOT NULL,
    stamps_json TEXT NOT NULL DEFAULT '[]',
    last_head TEXT,
    branch TEXT,
    remotes_json TEXT NOT NULL DEFAULT '[]',
    manifest_json TEXT,
    legacy INTEGER NOT NULL DEFAULT 0,
    retired INTEGER NOT NULL DEFAULT 0,
    registered TEXT NOT NULL,
    unresolved_json TEXT
);

CREATE INDEX IF NOT EXISTS checkouts_by_object
    ON checkouts(machine, object_device, object_inode);
CREATE INDEX IF NOT EXISTS checkouts_by_common
    ON checkouts(machine, common_device, common_inode);
CREATE INDEX IF NOT EXISTS checkouts_by_token ON checkouts(token);
CREATE INDEX IF NOT EXISTS checkouts_by_project ON checkouts(project);

CREATE TABLE IF NOT EXISTS checkout_views (
    checkout TEXT NOT NULL,
    machine TEXT NOT NULL,
    path TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    seen_order INTEGER NOT NULL,
    PRIMARY KEY (checkout, machine, path)
);

CREATE INDEX IF NOT EXISTS checkout_views_by_path ON checkout_views(machine, path);

CREATE TABLE IF NOT EXISTS homes (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    checkout TEXT,
    rule TEXT NOT NULL,
    decided TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS homes_by_project ON homes(project);
CREATE INDEX IF NOT EXISTS homes_by_checkout ON homes(checkout);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    checkout TEXT,
    kind TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    line TEXT NOT NULL,
    state TEXT NOT NULL,
    answer TEXT,
    answered_by TEXT,
    signature_json TEXT,
    opened TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS proposals_by_checkout ON proposals(checkout, state);

CREATE TABLE IF NOT EXISTS standing (
    record_id TEXT PRIMARY KEY,
    probability REAL NOT NULL,
    body_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    answered_by TEXT NOT NULL,
    answered TEXT NOT NULL
);
"""

# Indexes added beside the schema without a version bump: in a new store
# with its tables (`Store.create`), and in a store an earlier build created
# at its first open by this one (`_add_membership_tables`). Additive the
# same way: an earlier build opening a store that has them never reads
# them. `log_corroborations` holds the ``memory_put`` rows by session,
# which `Store.record_corroboration` reads under the write lock to tell
# whether a session already corroborated a memory: without it the read
# walked every ``memory_put`` row, measured at 1.4 ms over 10,000 rows and
# 7.7 ms over 50,000 (scratch run on this tree), growing with the log.
ADDITIVE_SCHEMA = """
CREATE INDEX IF NOT EXISTS log_corroborations ON log(session)
    WHERE kind = 'memory_put';
"""


class MemoryNotFoundError(KeyError):
    """No active memory with that id."""


class TombstonedError(KeyError):
    """The id exists but the memory is tombstoned."""


class NotTombstonedError(KeyError):
    """`restore` was called on an active memory, not a tombstone."""


class ConcurrentUpdateError(Exception):
    """`Store.update` saw a different `updated` in the store than the
    caller's snapshot. The caller's edit was built on a now-stale read
    and was refused rather than clobbering whoever bumped the record in
    between; `current_updated` is the stored stamp at the moment the
    check failed. Not a KeyError: the record is still findable, the
    failure is a stale snapshot."""

    def __init__(self, memory_id: str, current_updated: datetime) -> None:
        self.memory_id = memory_id
        self.current_updated = current_updated
        super().__init__(
            f"memory {memory_id} was updated concurrently "
            f"(your snapshot is stale; stored updated={current_updated.isoformat()})"
        )


class NotFoundError(KeyError):
    """No record with that key in the table asked."""


def resolve_keys_dir() -> Path:
    """The keys directory a store opens with when the caller names none:
    ``BETTERMEMORY_KEYS_DIR`` when set, else the user's config directory
    (`log.default_keys_dir`)."""
    override = os.environ.get(KEYS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _chain.default_keys_dir()


def store_path(path: Path | str) -> Path:
    """The store file for a path that names either the file or the
    directory it lives in: the file is always ``memory.sqlite``, so a path
    with any other name is the directory."""
    resolved = Path(path).expanduser()
    if resolved.name == STORE_FILENAME:
        return resolved
    return resolved / STORE_FILENAME


_ORPHAN_SUFFIX = re.compile(r"\.orphaned-(?P<stamp>[^.]*?)(?:-(?P<n>\d+))?\.sqlite\Z")


def orphan_order(path: Path | str) -> tuple[str, int]:
    """The key saved orphans sort by, oldest first: the UTC stamp in the
    name, then the counter a second orphan from the same second carries
    (``-2``, ``-3``), the first having none. A plain sort of the names
    puts ``-2.sqlite`` before ``.sqlite``."""
    found = _ORPHAN_SUFFIX.search(Path(path).name)
    if found is None:
        return (Path(path).name, 1)
    return (found["stamp"], int(found["n"] or 1))


def orphan_names(value: str | None) -> list[str]:
    """The file names a `CREATED_AFTER_ORPHANS` value holds: plain names
    beside the store, anything else dropped; empty when it is malformed."""
    if value is None:
        return []
    try:
        names = json.loads(value)
    except ValueError:
        return []
    if not isinstance(names, list):
        return []
    return [
        name
        for name in names
        if isinstance(name, str) and name and Path(name).name == name
    ]


def orphans_beside(path: Path) -> list[Path]:
    """The saved orphans beside the store file `path`, oldest first
    (`orphan_order`)."""
    pattern = f"{glob.escape(path.name)}.orphaned-*.sqlite"
    try:
        return sorted(
            (p for p in path.parent.glob(pattern) if p.is_file()), key=orphan_order
        )
    except OSError:
        return []


class UnmigratedV8DirectoryError(RuntimeError):
    """A directory holds bettermemory 8 files and no store file."""


def refuse_unmigrated_v8(path: Path) -> None:
    """Raise `UnmigratedV8DirectoryError` when no store file is at `path`
    and its directory holds a bettermemory 8 store."""
    if path.is_file() or not path.parent.is_dir() or not holds_v8_files(path.parent):
        return
    raise UnmigratedV8DirectoryError(
        f"{path.parent} holds a bettermemory 8 store and no "
        f"{STORE_FILENAME}; run `bettermemory migrate v8` to import it "
        "(`uvx bettermemory migrate v8` through the Claude Code plugin); "
        "the 8.x files are left as they are"
    )


#: A v8 memory file: `<date>-<slug>-<ULID>.md` (`v8.py`).
_V8_MEMORY_NAME = re.compile(r".*-[0-9A-HJKMNP-TV-Z]{26}\.md$")


def holds_v8_files(directory: Path) -> bool:
    """Whether `directory` carries a bettermemory 8 store: a memory
    file, the event log or a shard of it, or the tombstone directory."""
    try:
        children = list(directory.iterdir())
    except OSError:
        return False
    for child in children:
        name = child.name
        if name == ".events.jsonl" or name == ".tombstones":
            return True
        if name.startswith(".events.") and name.endswith(".jsonl"):
            return True
        if name.endswith(".md") and _V8_MEMORY_NAME.match(name):
            return True
    return False


_MEMBERSHIP_STATEMENTS = tuple(
    statement.strip() for statement in MEMBERSHIP_SCHEMA.split(";") if statement.strip()
)
_MEMBERSHIP_OBJECTS = frozenset(re.findall(r"IF NOT EXISTS (\w+)", MEMBERSHIP_SCHEMA))
_ADDITIVE_STATEMENTS = tuple(
    statement.strip() for statement in ADDITIVE_SCHEMA.split(";") if statement.strip()
)
_ADDITIVE_OBJECTS = frozenset(re.findall(r"IF NOT EXISTS (\w+)", ADDITIVE_SCHEMA))


def _add_membership_tables(
    conn: sqlite3.Connection, path: Path, meta: dict[str, str]
) -> None:
    """The additive schema step of decision D9, run at every open: the
    membership tables and indexes where any is missing, and the store id
    of design 8.7 where an earlier build created the store. Nothing is
    written when both are there. The step runs under the store's write
    lock (``BEGIN IMMEDIATE``), so two processes opening one store at once
    create the tables once, and every statement is ``IF NOT EXISTS``. A
    store file that cannot be written (a read-only copy) is left as it
    is, and the log names what it lacks; its membership reads then find
    no tables, and the fold reads a missing table as empty. ``meta`` gains the minted
    store id. A table an earlier build of these tables created without an
    optional column gains it (``ALTER TABLE ... ADD COLUMN``). The indexes
    of `ADDITIVE_SCHEMA` are added in the same step; on a read-only store
    their readers fall back to the table walk they replace."""
    master = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IS NOT NULL"
        )
    }
    lacking = [
        spec
        for spec in _MEMBERSHIP_SPECS.values()
        if spec.table in master
        and any(
            not re.search(rf"\b{column}\b", master[spec.table])
            for column in spec.optional
        )
    ]
    wanted = [
        *sorted((_MEMBERSHIP_OBJECTS | _ADDITIVE_OBJECTS) - set(master)),
        *([] if meta.get(MEMBERSHIP_STORE_ID) else ["the store id"]),
        *(
            f"{spec.table}.{column}"
            for spec in lacking
            for column in spec.optional
            if not re.search(rf"\b{column}\b", master[spec.table])
        ),
    ]
    if not wanted:
        return
    minted = secrets.token_hex(16)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in (*_MEMBERSHIP_STATEMENTS, *_ADDITIVE_STATEMENTS):
                conn.execute(statement)
            for spec in lacking:
                have = {
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({spec.table})")
                }
                for column in spec.optional:
                    if column not in have:
                        conn.execute(
                            f"ALTER TABLE {spec.table} ADD COLUMN {column} TEXT"
                        )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (MEMBERSHIP_STORE_ID, minted),
            )
            conn.execute("COMMIT")
        except BaseException:
            # The COMMIT is inside: one that fails leaves no transaction
            # open on the connection the store goes on to use.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).replace(" ", "").lower():
            raise
        log.warning("store %s is read-only; not added: %s", path, ", ".join(wanted))
        return
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (MEMBERSHIP_STORE_ID,)
    ).fetchone()
    meta[MEMBERSHIP_STORE_ID] = str(row[0])
