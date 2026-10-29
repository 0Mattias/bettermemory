"""The vocabulary and the schema `store.py` builds its tables from.

The schema version and page size, the provenance labels, the folded and
membership tables with their mutation and event kinds and the specs the
membership rows are checked against, the row types the reads return
(`MemoryRow`, `TombstoneRow`, `TrustRow`, `EpisodeVolume`, `RecordHome`,
`StandingAnswer`), the `SCHEMA`, `MEMBERSHIP_SCHEMA` and `ADDITIVE_SCHEMA`
texts with the additive step that brings an older store up to them, the
store's errors, the compatibility contract's versions and the check every
open makes of them (`check_versions`, ENG-107), the paths a store opens
by (`store_path`, the saved orphans, the v8 directory check,
`resolve_keys_dir`), and the check every opener of an existing store
file runs before it reads a value from it (`admit_store`,
`read_store_meta`, `StoreRefusedError`). `store` re-exports every name a
caller reads. It lives apart from `store.py` to keep each module under
the package's 2,000-line bound (ENG-96), at the bottom of the store
family: it imports no other store module.
"""

from __future__ import annotations

import contextlib
import glob
import re
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from . import log as _chain
from .config import KEYS_DIR_ENV, STORE_FILENAME
from .log import CONTROL_KINDS
from .models import Memory, TombstonedMemory

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")


# `STORE_FILENAME` is defined in `config` so the standard-library-only
# daemon client can name the file without importing the store.
SCHEMA_VERSION = 1

# The compatibility contract's versions (ENG-107, CONTRIBUTING.md). Two
# integers and a package version, each with one job:
#
# - `SCHEMA_VERSION`, meta's ``schema_version``: the major line's format.
#   It moves only with a major release, and `Store.open` refuses a store
#   whose value is higher, as every build before this contract did, so a
#   store of the next major is refused by those builds too.
# - `SCHEMA_REVISION`, meta's ``schema_revision``: what this build writes
#   within that format, counted in the store's schema steps. A step that
#   changes what a build writes raises it. meta's ``min_reader_revision``
#   and ``min_writer_revision`` are the lowest revisions that may read the
#   store and write it: an open below the first is refused
#   (`StoreVersionError`), and one below the second opens for reading only.
#   `MIN_READER_REVISION` and `MIN_WRITER_REVISION` are what a store this
#   build creates carries. A store from before the contract carries none of
#   the three keys, which read as 0, and gets them at its first writing open.
# - meta's ``engine_version``: the package version of the newest build that
#   opened the store to write, in the order `_daemon_client.version_key`
#   reads. It names a build in the open's refusal; no check gates on it.
SCHEMA_REVISION = 1
MIN_READER_REVISION = 1
MIN_WRITER_REVISION = 1
SCHEMA_REVISION_KEY = "schema_revision"
MIN_READER_KEY = "min_reader_revision"
MIN_WRITER_KEY = "min_writer_revision"
ENGINE_VERSION_KEY = "engine_version"

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


class UnknownFieldsError(ValueError):
    """A mutation of a record whose last put carries fields this build's
    model does not define (a later build wrote them). The model would drop
    them on the way back, and with them any rule that ties them to the
    fields this build edits, so the record is left as it is (ENG-107).
    `fields` names them and `kind` the mutation refused, which the
    message says in the words of the call that made it (`_REFUSED`)."""

    #: What a refused mutation was, as the call that made it says it.
    _REFUSED = {
        "memory_put": "the change",
        "memory_delete": "the removal",
        "tombstone_put": "the change to the removed record",
        "tombstone_delete": "the restore, purge or prune",
    }

    def __init__(self, record_id: str, kind: str, fields: list[str]) -> None:
        self.record_id = record_id
        self.kind = kind
        self.fields = fields
        shown = ", ".join(repr(name)[:66] for name in fields[:8])
        more = f" and {len(fields) - 8} more" if len(fields) > 8 else ""
        refused = self._REFUSED.get(kind, "the change")
        super().__init__(
            f"record {record_id} was last written by a build that defines "
            f"fields this one does not ({shown}{more}); this build would drop "
            f"them, so it refuses {refused}: upgrade bettermemory to change "
            "the record"
        )


class StoreVersionError(ValueError):
    """The store needs a newer build to open it: its minimum reader
    revision is above this build's `SCHEMA_REVISION`, or a version key in
    its meta does not read as a whole number. `key` is the meta key that
    refused the open and `needed` the schema revision a build must read to
    open the store (None when a revision is not what it needs), which
    `doctor` names in its remedy."""

    def __init__(
        self, message: str, *, key: str | None = None, needed: int | None = None
    ) -> None:
        super().__init__(message)
        self.key = key
        self.needed = needed


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


# ---------------------------------------------------------------------------
# The versions an open checks (ENG-107)
# ---------------------------------------------------------------------------


class VersionVerdict(NamedTuple):
    """What `check_versions` decided for one open. `writes`: the open goes
    on as the owner's writing open (``allow_rekey``); False for a reader's
    open and for a store this build may only read. `read_only`: the store's
    minimum writer revision is above this build's, and the connection
    refuses every write (``PRAGMA query_only``); `reason` says why.
    `revision`, `min_reader` and `min_writer` are the store's as the open
    read them, 0 for a key the store does not carry."""

    writes: bool
    read_only: bool
    reason: str | None
    revision: int
    min_reader: int
    min_writer: int


# The stores this process has said it opens for reading only, each with the
# minimum writer revision that decided it: said once, not at every
# connection a daemon's workers reopen.
_READ_ONLY_TOLD: set[tuple[str, int]] = set()

_WHOLE = re.compile(r"[0-9]{1,9}")


def version_meta() -> dict[str, str]:
    """The version keys `Store.create` writes into a new store's meta: this
    build's revision and the minimums a store it creates carries."""
    return {
        SCHEMA_REVISION_KEY: str(SCHEMA_REVISION),
        MIN_READER_KEY: str(MIN_READER_REVISION),
        MIN_WRITER_KEY: str(MIN_WRITER_REVISION),
    }


def _shown(text: str) -> str:
    """A value read from meta, fit for one line of a message: meta holds
    whatever an editor of the file wrote, so control and format characters
    are escaped (`repr`) and the text is cut at 64 characters."""
    escaped = repr(text)[1:-1]
    return escaped if len(escaped) <= 64 else escaped[:61] + "..."


def _stored_revision(meta: Mapping[str, str], key: str, path: Path) -> int:
    """meta's revision under `key`, 0 when the store carries none. A value
    that is not a whole number raises `StoreVersionError`: meta is not
    authenticated, and an open that guessed would admit a build the store's
    writer kept out, or keep out one it admitted."""
    raw = meta.get(key)
    if raw is None:
        return 0
    if not _WHOLE.fullmatch(raw):
        raise StoreVersionError(
            f"{path} carries a {key} that is not a whole number "
            f"('{_shown(raw)}'); the store is not opened: set meta's {key} "
            "back to a whole number, or restore the store from a backup taken "
            "before the edit",
            key=key,
        )
    return int(raw)


def _release_order(version: str | None) -> tuple[int, ...] | None:
    """`version`'s place in release order (`_daemon_client.version_key`,
    any local ``+...`` part left off), None when it has none."""
    if version is None:
        return None
    from ._daemon_client import version_key

    return version_key(version.split("+", 1)[0])


def _newer_engine(stored: str | None, version: str) -> bool:
    """Whether `version` names a newer build than ``engine_version``'s
    `stored`: later in release order, or the stored one has no place in it
    and `version` has."""
    ours = _release_order(version)
    theirs = _release_order(stored)
    return ours is not None and (theirs is None or ours > theirs)


def _record_engine(
    conn: sqlite3.Connection, path: Path, meta: dict[str, str], version: str
) -> None:
    """Name this build in ``engine_version`` at a writing open when it is
    newer than the stored one (`_newer_engine`), read again under the write
    lock so two builds opening at once leave the newer. An open of a store
    that already names this build or a newer one takes no lock. A store
    that cannot be written now (a read-only file, or one another writer
    holds past the busy timeout) is left as it is with one line logged; the
    next writing open records it. `meta` gains what was written."""
    if not _newer_engine(meta.get(ENGINE_VERSION_KEY), version):
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (ENGINE_VERSION_KEY,)
            ).fetchone()
            newer = _newer_engine(None if row is None else str(row[0]), version)
            if newer:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (ENGINE_VERSION_KEY, version),
                )
            conn.execute("COMMIT")
        except BaseException:
            # The COMMIT is inside, as in `_add_membership_tables`.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        log.info(
            "store %s: engine_version not recorded (%s); the next writing open "
            "records it",
            path,
            exc,
        )
        return
    if newer:
        meta[ENGINE_VERSION_KEY] = version
    elif row is not None:
        meta[ENGINE_VERSION_KEY] = str(row[0])


def check_versions(
    conn: sqlite3.Connection,
    path: Path,
    meta: dict[str, str],
    writing: bool,
    keys_dir: Path | str | None = None,
) -> VersionVerdict:
    """The compatibility contract at an open (ENG-107), run by `Store.open`
    once the store id and the schema version have passed. A store whose
    minimum reader revision is above this build's `SCHEMA_REVISION` is
    refused (`StoreVersionError`, naming the newest build that wrote it).
    One whose minimum writer revision is above it opens for reading only:
    the connection is set ``PRAGMA query_only``, so every write fails as on
    read-only media, nothing is recorded, an owner's open goes on as a
    reader's (no rekey, no key ring claim, no origin anchor), and the first
    such open in a process logs one warning saying why. Otherwise the store's
    schema steps run (`store_steps.run_steps`): at a writing open (`writing`,
    the owner's ``allow_rekey``) each step above the store's revision is
    applied and recorded, a step that is not additive after a backup signed
    with a key from `keys_dir`, the opener's keys directory; a reader's open
    applies none, and while a step that is not additive is pending it is
    set ``PRAGMA query_only`` and reads the store as it stands. A writing
    open then names this build in ``engine_version``
    when it is the newest (`_record_engine`). The verdict's `writes` is
    what the open goes on with."""
    from . import __version__

    revision = _stored_revision(meta, SCHEMA_REVISION_KEY, path)
    min_reader = _stored_revision(meta, MIN_READER_KEY, path)
    min_writer = _stored_revision(meta, MIN_WRITER_KEY, path)
    engine = meta.get(ENGINE_VERSION_KEY)
    newest = f" (its newest writer was bettermemory {_shown(engine)})" if engine else ""
    if min_reader > SCHEMA_REVISION:
        raise StoreVersionError(
            f"{path} needs a reader at schema revision {min_reader} or later "
            f"(meta's {MIN_READER_KEY}){newest}; this build, bettermemory "
            f"{__version__}, reads revision {SCHEMA_REVISION}: upgrade "
            "bettermemory to open it",
            key=MIN_READER_KEY,
            needed=min_reader,
        )
    if min_writer > SCHEMA_REVISION:
        reason = (
            f"{path} is open for reading only: it needs a writer at schema "
            f"revision {min_writer} or later{newest}, and this build, "
            f"bettermemory {__version__}, writes revision {SCHEMA_REVISION}: "
            "upgrade bettermemory to write to it"
        )
        conn.execute("PRAGMA query_only = ON")
        told = (str(path), min_writer)
        if told not in _READ_ONLY_TOLD:
            _READ_ONLY_TOLD.add(told)
            log.warning("%s", reason)
        return VersionVerdict(False, True, reason, revision, min_reader, min_writer)
    from .store_steps import run_steps

    run_steps(conn, path, meta, writing=writing, keys_dir=keys_dir)
    if writing:
        _record_engine(conn, path, meta, __version__)
    return VersionVerdict(writing, False, None, revision, min_reader, min_writer)


class _RolledBack(Exception):
    """Ends `writes_rolled_back`'s transaction, which then rolls back."""


@contextlib.contextmanager
def writes_rolled_back(store: Store) -> Iterator[bool]:
    """Run a read on `store` that records as it goes, whether or not its
    connection may write. On a connection that refuses writes (``PRAGMA
    query_only``, a store open for reading only by its versions,
    `check_versions`) the block runs in one write transaction that is
    rolled back at its end, the block's failure included, with
    ``query_only`` lifted for its length and set again after: the caller's
    membership resolution, which writes its events and, at first sight of
    a checkout, its rows, reads what it would read on a store it may
    write, and the store keeps none of it. Yields whether it does so,
    and the block then writes nothing outside the store either (the
    caller turns the checkout token's write off). On any other connection
    the block runs as it is and the yield is False.

    The block must not await: `store`'s connection serves one call at a
    time, and the lifted ``query_only`` holds for nothing but the block.
    The transaction takes the store's write lock (``BEGIN IMMEDIATE``)
    for the block's length, which another build's writer waits for within
    its busy timeout."""
    conn = store.conn
    if not conn.execute("PRAGMA query_only").fetchone()[0]:
        yield False
        return
    conn.execute("PRAGMA query_only = OFF")
    try:
        with contextlib.suppress(_RolledBack), store.batch():
            yield True
            raise _RolledBack
    finally:
        conn.execute("PRAGMA query_only = ON")


# Admitting a store file (ENG-172)
# ---------------------------------------------------------------------------

#: How long one statement may run on a store file while it is admitted
#: (`admit_store`, `read_store_meta`), in seconds, counted from the
#: statement's first progress callback: a wait for another connection's
#: lock comes before it and is the busy timeout's to bound. The statements
#: it bounds read ``sqlite_master`` (about 70 rows in every store this
#: build writes), each plain table's columns, and ``meta`` (under ten),
#: whatever the store's size: measured on this tree (scratch run, Apple
#: silicon, 200 opens each) all of it took 0.71 ms at the median and
#: 1.0 ms at most, on a fresh store and on one of 2,000 memories alike,
#: so 2 s cuts nothing this build wrote and stays well inside the 15 s a
#: hook's client waits (`_daemon_client.HOOK_TIMEOUT_SECONDS`). A meta of
#: 8 million rows, read whole, took 2.15 s; under this deadline its open
#: was refused at 2.09 s.
ADMISSION_DEADLINE_SECONDS = 2.0

#: SQLite virtual-machine steps between two looks at the clock while a
#: file is admitted (`sqlite3.Connection.set_progress_handler`). Small, so
#: a statement of few steps, each made costly, is still looked at.
_PROGRESS_STEPS = 100

#: The longest string or blob a statement may make while a file is
#: admitted (SQLITE_LIMIT_LENGTH, `admit_store`). One built-in call is one
#: virtual-machine step a progress handler cannot interrupt, and its work
#: can grow with its arguments' length (``instr`` is O(n*m)); SQLite runs a
#: schema's expressions (``sqlite_stat1`` is read while the schema loads)
#: before any check can look at them. Under 16 KiB one such call costs
#: about a millisecond; the longest text this build's schema holds is
#: 1,015 characters, and a statistics row is a few numbers.
_ADMISSION_MAX_LENGTH = 1 << 14

#: The tables SQLite itself writes into a database it is asked to analyse
#: (`ANALYZE`, ``PRAGMA optimize``), which a user or a tool may have run on
#: a store, with the text SQLite gives each: statistics the query planner
#: reads while the schema loads. Any other text (a generated column, say)
#: is refused.
_SQLITE_STATISTICS = {
    "sqlite_stat1": "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
    "sqlite_stat4": "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
}

#: A column default that is a literal: NULL, a number, a string or a blob,
#: in parentheses or not. Every default this build's tables give is one.
_LITERAL = re.compile(
    r"\(*\s*(NULL|[-+]?\d+(\.\d+)?|'(?:[^']|'')*'|X'[0-9A-Fa-f]*')\s*\)*", re.I
)
#: What a table's text quotes or comments out, so a word in it is not read
#: as a keyword (`table_refusal`).
_QUOTED = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/",
    re.S,
)

#: SQLite's primary result codes for a file whose schema cannot be read:
#: SQLITE_CORRUPT (a schema text SQLite cannot parse among them) and
#: SQLITE_NOTADB.
_UNREADABLE_SCHEMA_CODES = frozenset({11, 26})


class StoreRefusedError(ValueError):
    """A store file every opener refuses before it reads a value from it
    (`admit_store`): its schema holds an object this build does not
    create, or a read of its schema or its meta ran past
    `ADMISSION_DEADLINE_SECONDS`. A ValueError, as every other refusal of
    `Store.open` for a file this build does not read is, so each caller's
    handling of those reports this one too, in its own single line.
    ``timed_out`` tells the deadline from the schema: a file whose read
    ran long may be a store this build wrote, on a disk that stalled.
    ``holds_meta`` says whether the refused file has a ``meta`` table,
    None when its schema was not read to tell: one that has may still
    hold a store's id (another build's store with one more index), which
    the key ring's binding check reads as a file it cannot read to tell
    (`store_keys._store_id_at`), not as no store."""

    def __init__(
        self,
        path: Path,
        why: str,
        *,
        timed_out: bool = False,
        holds_meta: bool | None = None,
    ) -> None:
        self.path = path
        self.why = why
        self.timed_out = timed_out
        self.holds_meta = holds_meta
        super().__init__(f"{path} is not opened: {why}")


_BUILT_OBJECTS: dict[tuple[str, str], tuple[str, str | None, bool]] = {}


def _built_objects() -> dict[tuple[str, str], tuple[str, str | None, bool]]:
    """Every object this build's schema creates, by (type, name): the table
    it is on, its text as ``sqlite_master`` keeps it, and whether that text
    must match. Read once from an in-memory database built from the three
    schema texts, so the list is the schema itself and cannot drift from
    it. Indexes, triggers and the full-text table must match their text:
    each is the same text in every build since the store was one file
    (checked over the history of `store.py` and this module when the check
    was written). A plain table is matched by name, since
    `_add_membership_tables` adds columns to an older store's tables and
    SQLite rewrites their text when it does; what such a table can make
    SQLite run is checked on the file itself (`table_refusal`)."""
    if not _BUILT_OBJECTS:
        conn = sqlite3.connect(":memory:")
        try:
            for script in (SCHEMA, MEMBERSHIP_SCHEMA, ADDITIVE_SCHEMA):
                conn.executescript(script)
            built = {
                (str(kind), str(name)): (
                    str(table),
                    sql,
                    kind != "table" or str(sql or "").startswith("CREATE VIRTUAL"),
                )
                for kind, name, table, sql in conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master"
                )
            }
        finally:
            conn.close()
        _BUILT_OBJECTS.update(built)
    return _BUILT_OBJECTS


def schema_refusal(rows: Iterable[Sequence[Any]]) -> str | None:
    """Why a store file whose ``sqlite_master`` holds `rows` (type, name,
    table, text) is not one this build wrote, or None when it is. Every
    object must be one this build creates (`_built_objects`), on the same
    table, an index, a trigger or the full-text table with the same text,
    and a plain table must not be a virtual one. SQLite's own statistics
    tables pass with SQLite's own text. So a view of any name fails, a trigger this build did not
    write, and one of its own triggers with another body: the objects that
    run SQL of the file's choosing when this build reads or writes it. A
    store an earlier build created holds a subset of these objects, and
    passes."""
    built = _built_objects()
    for kind, name, table, sql in rows:
        kind, name, table = str(kind), str(name), str(table)
        if kind == "table" and name in _SQLITE_STATISTICS:
            if sql != _SQLITE_STATISTICS[name]:
                return f"its table {name!r} is not the one SQLite writes"
            continue
        expected = built.get((kind, name))
        if expected is None:
            return f"it holds a {kind} named {name!r}, which this build never creates"
        on, text, exact = expected
        if table != on:
            return f"its {kind} {name!r} is on {table!r}, not on {on!r}"
        if exact and sql != text:
            return f"its {kind} {name!r} is not the one this build creates"
        if not exact and str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL"):
            return f"its table {name!r} is a virtual table, which this build never makes it"
    return None


def table_refusal(conn: sqlite3.Connection, name: str, sql: str | None) -> str | None:
    """Why the plain table ``name`` this build creates, as the file on
    `conn` defines it (``sql``), could make SQLite run an expression of the
    file's choosing when this build reads or writes it, or None: a
    generated column, a default that is not a literal, a CHECK, or a
    foreign key. A table is admitted by its name, since an older store's
    has fewer columns (`schema_refusal`); this reads its columns, so a
    ``meta`` whose ``value`` is computed, or a ``log`` whose every insert
    runs a check, is refused. None of this build's own tables has any of
    them: their defaults are all ``''``, ``'[]'``, ``'{}'`` or ``0``."""
    quoted = '"' + name.replace('"', '""') + '"'
    for row in conn.execute(f"PRAGMA table_xinfo({quoted})"):
        column, default, hidden = row[1], row[4], row[6]
        if hidden:
            return f"its table {name!r} has a generated column {column!r}"
        if default is not None and not _LITERAL.fullmatch(str(default)):
            return (
                f"its table {name!r} gives {column!r} a default that is not a literal"
            )
    if conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchone() is not None:
        return f"its table {name!r} has a foreign key"
    if re.search(r"\bCHECK\b", _QUOTED.sub(" ", sql or ""), re.I):
        return f"its table {name!r} has a check"
    return None


@contextlib.contextmanager
def _deadline(
    conn: sqlite3.Connection, path: Path, what: str, *, length: int | None = None
) -> Iterator[None]:
    """Run the statements inside the block with `ADMISSION_DEADLINE_SECONDS`
    on each one's virtual-machine time: a progress handler that starts its
    clock at its first call and interrupts the statement once the deadline
    has passed, which SQLite reports as SQLITE_INTERRUPT and this raises as
    `StoreRefusedError` (``timed_out``). With ``length``, no string or blob
    longer than that is made inside the block (SQLITE_LIMIT_LENGTH), and
    one that would be is a refusal too. The handler is removed, and the
    limit put back, on the way out. Read at call time, so a test can
    shorten it."""
    seconds = ADMISSION_DEADLINE_SECONDS
    started: list[float] = []
    fired: list[bool] = []

    def over() -> int:
        now = time.monotonic()
        if not started:
            started.append(now)
            return 0
        if now - started[0] > seconds:
            fired.append(True)
            return 1
        return 0

    conn.set_progress_handler(over, _PROGRESS_STEPS)
    longest = None
    if length is not None:
        longest = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, length)
    try:
        yield
    except sqlite3.DataError as exc:
        raise StoreRefusedError(
            path, f"it is not a store this build wrote ({exc} in its {what})"
        ) from exc
    except sqlite3.OperationalError as exc:
        if not fired:
            raise
        raise StoreRefusedError(
            path,
            f"reading its {what} ran past the {seconds:g} s a store file is given "
            "before it is admitted",
            timed_out=True,
        ) from exc
    finally:
        conn.set_progress_handler(None, 0)
        if longest is not None:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, longest)


def admit_store(conn: sqlite3.Connection, path: Path) -> None:
    """Admit the store file `conn` was just opened on, before any other
    statement this build makes reads it or writes to it: ``trusted_schema``
    off for the connection's life, then, under the deadline and with no
    string longer than `_ADMISSION_MAX_LENGTH` (`_deadline`), its
    ``sqlite_master`` read and checked against this build's schema
    (`schema_refusal`), and each plain table this build creates read for
    what it could make SQLite run (`table_refusal`). A file that fails is
    refused with one `StoreRefusedError`, as is one whose schema SQLite
    cannot read (SQLITE_CORRUPT, SQLITE_NOTADB), and the caller closes the
    connection. `read_store_meta` then reads its meta under the deadline.

    What a refusal leaves: this build writes nothing to the file
    (`store._connect` switches it to WAL, sets the file modes and runs
    the additive step only after this). SQLite itself does what it does
    on opening any database: on a WAL-mode file it makes the ``-wal`` and
    ``-shm`` while it reads the schema and removes them at the close, and
    a ``-wal`` or a hot ``-journal`` that came with the file is replayed
    into it or rolled back, before the first statement's first step and
    so outside the deadline.

    Which files are checked: every existing file any opener reads, the
    state directory's own store included, and only a file `Store.create`
    has just written on the same connection goes unchecked. "A store this
    build did not create" cannot be told before the file is read: the path
    says nothing of the bytes now at it (a repository's
    ``.claude-memory/memory.sqlite``, reached through ``[storage]
    cwd_store``, is whatever the clone holds, and a ``git pull`` can
    replace one the user's own build wrote; the state directory's file can
    be a restored or synced copy), the key ring's binding
    (`log.KeyRing.bind_store`) names a path, not bytes, and the store id
    that finds the ring is read from the file itself. So the boundary is
    the connection: what this build wrote on it, and nothing else. The
    check costs one read of a table of about 70 rows per open.

    Why these three: a view, or a trigger of the file's choosing, runs
    SQL the file chose whenever this build reads the table it stands for
    or writes beside it, and a recursive one never ends (a ``meta`` that
    is a view over an endless recursive CTE hung `security audit`,
    `doctor`, the daemon and every hook); ``trusted_schema`` off keeps a
    table's defaults, checks and generated columns to functions SQLite
    marks harmless; the length limit bounds what one of those functions
    can cost while the schema loads (``sqlite_stat1`` is read then);
    the table check refuses a table of this build's name that computes a
    column, checks a row, defaults to an expression or refers to another
    table; and the deadline bounds the reads made before the file is
    known, a schema too large to parse or a meta too large to read among
    them. Once a file is admitted, its objects are this build's own
    and run nothing of the file's choosing, so each later statement's
    work is bounded by the rows it holds, as on any store this build
    created, and the deadline is lifted."""
    with _deadline(conn, path, "schema", length=_ADMISSION_MAX_LENGTH):
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
            rows = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in _UNREADABLE_SCHEMA_CODES:
                raise
            raise StoreRefusedError(
                path,
                "it is not a store this build wrote, or it is damaged: its "
                f"schema cannot be read ({exc})",
            ) from exc
        why = schema_refusal(rows)
        built = _built_objects()
        for kind, name, _table, sql in rows if why is None else ():
            expected = built.get((str(kind), str(name)))
            if expected is not None and str(kind) == "table" and not expected[2]:
                why = table_refusal(conn, str(name), sql)
                if why is not None:
                    break
    if why is not None:
        raise StoreRefusedError(
            path,
            f"it is not a store this build wrote ({why})",
            holds_meta=any(
                str(kind) == "table" and str(name) == "meta"
                for kind, name, _table, _sql in rows
            ),
        )


def read_store_meta(conn: sqlite3.Connection, path: Path) -> dict[str, str]:
    """The meta table of a store file `admit_store` admitted, read under
    the same deadline: its rows are the file's to choose, as many as it
    likes. A file with no meta table raises `sqlite3.OperationalError`, as
    the read always did."""
    with _deadline(conn, path, "meta table"):
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {str(row[0]): str(row[1]) for row in rows}
