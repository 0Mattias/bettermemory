"""One SQLite file per store: the bettermemory 9 store.

The v8 store was a directory of markdown files with a derived FTS5 index
beside it; markdown is now the export and import format (``mirror`` and
``migrate_v8``). This store is one file, ``memory.sqlite``, whose tables
ARE the records: memories,
tombstones, episodes, verifications, conflicts, imports and quarantine,
plus the hash-chained log every mutation and telemetry event is a row of
(``bettermemory.log``). There is no derived index to rebuild or to skew:
the FTS5 table and its triggers are the v8 index's own, verbatim, so the
candidate query and its bm25 order are unchanged.

The fold. A mutation row carries the whole record after the change, and
the live write path and ``refold`` apply a row through the same function,
``_apply_mutation``. Replaying the log into a scratch database therefore
reproduces the tables exactly, and a row the replay does not produce is
one that entered outside the store: ``unaccounted``, the label
``provenance_for`` reports for it. ``log_verify`` merges that fold with
the chain report from ``bettermemory.log``.

Transactions. Each mutating method runs ``BEGIN IMMEDIATE``, changes the
table, appends its log rows and commits, then moves the head checkpoint
forward. A failure anywhere rolls the table change and the rows back
together.

Keys. ``create`` writes the store's key under the keys directory
(``log.default_keys_dir`` unless the caller names one) and records only
its fingerprint in ``meta``. ``open`` finds the key by fingerprint; a
missing or foreign key leaves the store readable and refuses mutations,
naming the key directory, unless the caller opens with
``allow_rekey=True`` (the owner's openers, `open_or_create` and the
migrations, do), when the key is retired, a fresh one written, and a
``rekey`` row appended. Meta's fingerprint is only what the file says:
a key that signs a segment of the log is this store's own whatever meta
names, and stays (`Store.open`). A key file that cannot be read is not a
missing one, and is never replaced. The key ring is bound to one store
file (``<store_id>.path``, `log.KeyRing.bind_store`): an opener of a
second file with the same store id, while the bound file still holds it
(or cannot be read to tell), writes nothing and refuses writes, the
owner's and a reader whose key matches alike.

Foreign segments. A segment of the log whose key this machine does not
hold, after one whose key it does, is rows a writer without the key put
into history this machine signed (`log.chain_segments`). Every read
labels its records ``unaccounted``, as it labels a row whose MAC fails,
save a row this machine's own key signed into it (a daemon that had the
store open while the forged row went in); the leading segments of a
store copied here keep their labels unless ``[security]
strict_provenance`` is set.

The origin anchor. Where this machine's history of the store begins is
kept in the key ring, outside the file (`log.KeyRing.write_origin`):
`create` anchors seq 1, the owner's opener anchors the ``rekey`` row it
writes when it takes over a store copied here, and on a store from before
the anchor existed it anchors the earliest segment this machine holds,
once (`_settle_origin`). The reads check it with the segment map
(`log.chain_trust`). An anchor the log no longer matches means the
history was rewritten where this machine's begins, so every segment whose
key is not held reads as foreign, leading ones included. An anchor never
moves: one whose row no key here verifies because the key that signed it
was lost reads ``unverifiable``, and the reads keep the rule without an
anchor until the key is back.
"""

from __future__ import annotations

import contextlib
import errno
import glob
import hashlib
import re
import json
import logging
import math
import os
import secrets
import sqlite3
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from . import _caches
from ._fsutil import ensure_owner_only_dir
from .events import _redact_event_fields
from .identity import Actor
from . import log as _chain
from .config import KEYS_DIR_ENV, STORE_FILENAME
from .log import (
    CAPTURE_KINDS,
    CONTROL_KINDS,
    MIGRATE_V8,
    REKEY,
    STORE_CREATED,
    HistoryOrigin,
    KeyRing,
    KeyUnreadableError,
    LogRow,
    OriginCheck,
    Segment,
    append_row,
    canonical_payload,
    chain_segments,
    chain_trust,
    check_origin,
    compute_mac,
    fingerprint,
    iter_rows,
    last_rekey_name,
    row_verifies,
    same_mac,
    verify_chain,
)
from .models import (
    Category,
    Confidence,
    Episode,
    Memory,
    MemoryLink,
    MemorySummary,
    Source,
    TombstonedMemory,
    TombstonedSummary,
    first_summary_line,
    generate_ulid,
    is_valid_ulid,
    utcnow,
)
from .origin import Origin, is_full_commit_sha
from .search import fts_index_text, fts_match_query, tokenizer_fingerprint
from .time_utils import isoformat_utc

log = logging.getLogger("bettermemory.store")

# `STORE_FILENAME` is defined in `config` so the standard-library-only
# daemon client can name the file without importing this module.
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


def body_sha256(body: str) -> str:
    """The sha256 of a record's body as stored, which a standing row
    names: an answer holds for that body and no other."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    """The one way a store file is opened: the page size for a new file,
    WAL, synchronous NORMAL, a 5-second busy timeout, foreign keys on,
    autocommit off so every write is an explicit transaction, and
    owner-only modes on the file and its WAL siblings."""
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        # A no-op on an existing store: the page size is fixed once the
        # file holds a table, and WAL mode fixes it for good.
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        for sibling in (
            path,
            path.with_suffix(path.suffix + "-wal"),
            path.with_suffix(path.suffix + "-shm"),
        ):
            if sibling.exists():
                with contextlib.suppress(OSError):
                    os.chmod(sibling, 0o600)
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Records to rows and back
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_opt(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _dt(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _dt_opt(text: str | None) -> datetime | None:
    return None if text is None else _dt(text)


def _scopes_text(scopes: Sequence[str]) -> str:
    """The space-padded scope list the LIKE filter matches whole tokens
    in, exactly as the v8 index spelled it."""
    if not scopes:
        return " "
    return " " + " ".join(scopes) + " "


def _json_text(value: Any) -> str:
    """The one JSON spelling a column holds: sorted keys, so the text a
    live write stores equals the text the fold derives from the log row,
    whose payload is canonical (sorted) JSON too."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _json_or_none(value: Mapping[str, Any]) -> str | None:
    return _json_text(value) if value else None


def _load_json(text: str | None, default: Any) -> Any:
    return default if text is None else json.loads(text)


def _origin_json(origin: Origin | None) -> str | None:
    if origin is None:
        return None
    return _json_or_none(origin.model_dump(mode="json", exclude_none=True))


def _actor_json(actor: Actor | None) -> str | None:
    if actor is None:
        return None
    return _json_or_none(actor.to_record())


def _record_columns(
    record: Memory | TombstonedMemory,
) -> dict[str, Any]:
    """The columns a memory and a tombstone share, straight off the record."""
    origin = record.origin
    actor = record.actor
    return {
        "id": record.id,
        "created": _iso(record.created),
        "updated": _iso(record.updated),
        "last_verified_at": _iso_opt(record.last_verified_at),
        "confidence": record.confidence.value,
        "category": record.category.value if record.category is not None else None,
        "body": record.body,
        "scopes_json": _json_text(list(record.scopes)),
        "origin_repo": origin.repo if origin is not None else None,
        "origin_worktree": origin.worktree_root if origin is not None else None,
        "verified_head": record.verified_head,
        "actor_client": actor.client if actor is not None else None,
        "actor_model": actor.model if actor is not None else None,
        "source": record.source.value,
        "origin_json": _origin_json(origin),
        "actor_json": _actor_json(actor),
        "verified_paths_json": _json_text(list(record.verified_paths)),
        "verified_commits_json": _json_text(list(record.verified_commits)),
        "verified_versions_json": _json_text(list(record.verified_versions)),
        "verified_absent_paths_json": _json_text(list(record.verified_absent_paths)),
        "claims_json": _json_text(list(record.claims)),
    }


def _links_json(links: Iterable[MemoryLink]) -> str:
    return _json_text(
        [link.model_dump(mode="json", exclude_none=True) for link in links]
    )


_MEMORY_COLUMNS = (
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "body_fts",
    "scopes_text",
    "scopes_fts",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "log_mac",
)

_TOMBSTONE_COLUMNS = (
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "removed",
    "removed_reason",
    "removed_session",
    "log_mac",
)


def _upsert_sql(table: str, columns: Sequence[str], key: str) -> str:
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO {table}({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )


_UPSERT_MEMORY = _upsert_sql("memories", _MEMORY_COLUMNS, "id")
_UPSERT_TOMBSTONE = _upsert_sql("tombstones", _TOMBSTONE_COLUMNS, "id")


def _sync_links(conn: sqlite3.Connection, memory: Memory) -> None:
    """Replace the outbound link rows for the memory, exact duplicates
    collapsed the way the v8 index collapsed them."""
    conn.execute("DELETE FROM memory_links WHERE source_id = ?", (memory.id,))
    if not memory.links:
        return
    seen: set[tuple[str, str, str, str | None]] = set()
    rows: list[tuple[str, str, str, str | None]] = []
    for link in memory.links:
        key = (memory.id, link.type.value, link.target_id, link.note)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
    conn.executemany(
        "INSERT OR IGNORE INTO memory_links(source_id, type, target_id, note) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def _write_memory_row(
    conn: sqlite3.Connection,
    memory: Memory,
    *,
    provenance: str,
    filename: str | None,
    log_mac: str | None,
) -> None:
    columns = _record_columns(memory)
    columns.update(
        {
            "body_fts": fts_index_text(memory.body),
            "scopes_text": _scopes_text(memory.scopes),
            "scopes_fts": fts_index_text(" ".join(memory.scopes)),
            "filename": filename,
            "provenance": provenance,
            "links_json": _links_json(memory.links),
            "corroborations": memory.corroborations,
            "last_corroborated": _iso_opt(memory.last_corroborated),
            "log_mac": log_mac,
        }
    )
    conn.execute(_UPSERT_MEMORY, tuple(columns[c] for c in _MEMORY_COLUMNS))
    _sync_links(conn, memory)


def _write_tombstone_row(
    conn: sqlite3.Connection,
    dead: TombstonedMemory,
    *,
    provenance: str,
    filename: str | None,
    links: list[dict[str, Any]],
    corroborations: int,
    last_corroborated: str | None,
    log_mac: str | None,
) -> None:
    columns = _record_columns(dead)
    columns.update(
        {
            "filename": filename,
            "provenance": provenance,
            "links_json": _json_text(links),
            "corroborations": corroborations,
            "last_corroborated": last_corroborated,
            "removed": _iso(dead.removed),
            "removed_reason": dead.removed_reason,
            "removed_session": dead.removed_session,
            "log_mac": log_mac,
        }
    )
    conn.execute(_UPSERT_TOMBSTONE, tuple(columns[c] for c in _TOMBSTONE_COLUMNS))


def _anchor_or_none(value: Any) -> str | None:
    """The loader drops an anchor the store would have refused (a branch
    name or an abbreviation edited into the row), so a revision string
    planted there never reaches git and never fails a load."""
    if value is None:
        return None
    candidate = str(value).strip().lower()
    return candidate if is_full_commit_sha(candidate) else None


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        created=_dt(row["created"]),
        updated=_dt(row["updated"]),
        scopes=_load_json(row["scopes_json"], []),
        confidence=row["confidence"],
        source=row["source"],
        body=row["body"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        actor=(
            Actor.model_validate(_load_json(row["actor_json"], {}))
            if row["actor_json"] is not None
            else None
        ),
        last_verified_at=_dt_opt(row["last_verified_at"]),
        category=row["category"],
        verified_paths=_load_json(row["verified_paths_json"], []),
        verified_commits=_load_json(row["verified_commits_json"], []),
        verified_versions=_load_json(row["verified_versions_json"], []),
        verified_absent_paths=_load_json(row["verified_absent_paths_json"], []),
        claims=_load_json(row["claims_json"], []),
        verified_head=_anchor_or_none(row["verified_head"]),
        links=[
            MemoryLink.model_validate(entry)
            for entry in _load_json(row["links_json"], [])
        ],
        corroborations=int(row["corroborations"]),
        last_corroborated=_dt_opt(row["last_corroborated"]),
    )


def _row_to_tombstone(row: sqlite3.Row) -> TombstonedMemory:
    return TombstonedMemory(
        id=row["id"],
        created=_dt(row["created"]),
        updated=_dt(row["updated"]),
        scopes=_load_json(row["scopes_json"], []),
        confidence=row["confidence"],
        source=row["source"],
        body=row["body"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        actor=(
            Actor.model_validate(_load_json(row["actor_json"], {}))
            if row["actor_json"] is not None
            else None
        ),
        last_verified_at=_dt_opt(row["last_verified_at"]),
        category=row["category"],
        verified_paths=_load_json(row["verified_paths_json"], []),
        verified_commits=_load_json(row["verified_commits_json"], []),
        verified_versions=_load_json(row["verified_versions_json"], []),
        verified_absent_paths=_load_json(row["verified_absent_paths_json"], []),
        claims=_load_json(row["claims_json"], []),
        verified_head=_anchor_or_none(row["verified_head"]),
        removed=_dt(row["removed"]),
        removed_reason=row["removed_reason"],
        removed_session=row["removed_session"],
    )


def _write_episode_row(
    conn: sqlite3.Connection, episode: Episode, *, log_mac: str | None
) -> None:
    conn.execute(
        "INSERT INTO episodes(id, session_id, created, body, scopes_json, takeaway, "
        "origin_json, is_floor, swarm_id, log_mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET session_id = excluded.session_id, "
        "created = excluded.created, body = excluded.body, "
        "scopes_json = excluded.scopes_json, takeaway = excluded.takeaway, "
        "origin_json = excluded.origin_json, is_floor = excluded.is_floor, "
        "swarm_id = excluded.swarm_id, log_mac = excluded.log_mac",
        (
            episode.id,
            episode.session_id,
            _iso(episode.created),
            episode.body,
            _json_text(list(episode.scopes)),
            episode.takeaway,
            _origin_json(episode.origin),
            1 if episode.is_floor else 0,
            episode.swarm_id,
            log_mac,
        ),
    )


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        session_id=row["session_id"],
        created=_dt(row["created"]),
        body=row["body"],
        scopes=_load_json(row["scopes_json"], []),
        takeaway=row["takeaway"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        is_floor=bool(row["is_floor"]),
        swarm_id=row["swarm_id"],
    )


def _verification_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_id": row["memory_id"],
        "machine_id": row["machine_id"],
        "verified_at": row["verified_at"],
        "verified_head": row["verified_head"],
        **{name: _load_json(row[f"{name}_json"], []) for name in _VERIFICATION_LISTS},
    }


# ---------------------------------------------------------------------------
# Mutations: one function, used by the live path and by the fold
# ---------------------------------------------------------------------------


def _apply_mutation(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    *,
    log_mac: str | None,
) -> None:
    """Apply one mutation row to the tables on ``conn``; ``log_mac`` is
    the MAC of the row being applied, stamped on the record it puts. Raises
    ValueError, KeyError, TypeError or a pydantic ValidationError on a
    payload that does not carry what the kind needs; the fold reports
    those rows as ``payload_invalid``."""
    if kind == "memory_put":
        memory = Memory.model_validate(payload["memory"])
        provenance = str(payload["provenance"])
        if provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        _write_memory_row(
            conn,
            memory,
            provenance=provenance,
            filename=payload.get("filename"),
            log_mac=log_mac,
        )
    elif kind == "memory_delete":
        conn.execute("DELETE FROM memories WHERE id = ?", (str(payload["id"]),))
    elif kind == "tombstone_put":
        dead = TombstonedMemory.model_validate(payload["tombstone"])
        provenance = str(payload["provenance"])
        if provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        links = payload.get("links") or []
        if not isinstance(links, list):
            raise TypeError("links must be a list")
        _write_tombstone_row(
            conn,
            dead,
            provenance=provenance,
            filename=payload.get("filename"),
            links=links,
            corroborations=int(payload.get("corroborations") or 0),
            last_corroborated=payload.get("last_corroborated"),
            log_mac=log_mac,
        )
    elif kind == "tombstone_delete":
        conn.execute("DELETE FROM tombstones WHERE id = ?", (str(payload["id"]),))
    elif kind == "episode_put":
        _write_episode_row(
            conn, Episode.model_validate(payload["episode"]), log_mac=log_mac
        )
    elif kind == "episode_delete":
        conn.execute("DELETE FROM episodes WHERE id = ?", (str(payload["id"]),))
    elif kind == "verification_put":
        memory_id = str(payload["memory_id"])
        machine_id = payload.get("machine_id")
        lists = {name: list(payload.get(name) or []) for name in _VERIFICATION_LISTS}
        conn.execute(
            "DELETE FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (memory_id, machine_id),
        )
        conn.execute(
            "INSERT INTO verifications(memory_id, machine_id, verified_at, "
            "verified_head, verified_paths_json, verified_commits_json, "
            "verified_versions_json, verified_absent_paths_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                machine_id,
                str(payload["verified_at"]),
                payload.get("verified_head"),
                *(_json_text(lists[name]) for name in _VERIFICATION_LISTS),
            ),
        )
    elif kind == "verification_delete":
        conn.execute(
            "DELETE FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (str(payload["memory_id"]), payload.get("machine_id")),
        )
    elif kind == "conflict_put":
        record = payload["conflict"]
        conn.execute(
            f"INSERT OR REPLACE INTO conflicts({', '.join(_CONFLICT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _CONFLICT_COLUMNS)})",
            tuple(record.get(c) for c in _CONFLICT_COLUMNS),
        )
    elif kind == "conflict_delete":
        conn.execute("DELETE FROM conflicts WHERE id = ?", (str(payload["id"]),))
    elif kind == "import_put":
        conn.execute(
            f"INSERT OR REPLACE INTO imports({', '.join(_IMPORT_COLUMNS)}) "
            "VALUES (?, ?, ?, ?)",
            (
                str(payload["source"]),
                str(payload["content_hash"]),
                str(payload["imported_at"]),
                payload.get("memory_id"),
            ),
        )
    elif kind == "import_delete":
        conn.execute("DELETE FROM imports WHERE source = ?", (str(payload["source"]),))
    elif kind == "quarantine_put":
        conn.execute(
            f"INSERT OR REPLACE INTO quarantine({', '.join(_QUARANTINE_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(payload["name"]),
                str(payload["reason"]),
                payload.get("detail"),
                payload.get("remote"),
                str(payload["at"]),
                payload.get("size"),
                payload.get("sha256"),
            ),
        )
    elif kind == "quarantine_delete":
        conn.execute("DELETE FROM quarantine WHERE name = ?", (str(payload["name"]),))
    elif kind.endswith("_put") and kind[: -len("_put")] in _MEMBERSHIP_SPECS:
        spec = _MEMBERSHIP_SPECS[kind[: -len("_put")]]
        conn.execute(
            f"INSERT OR REPLACE INTO {spec.table}({', '.join(spec.columns)}) "
            f"VALUES ({', '.join('?' for _ in spec.columns)})",
            _membership_values(spec, payload[spec.kind]),
        )
    elif kind.endswith("_delete") and kind[: -len("_delete")] in _MEMBERSHIP_SPECS:
        spec = _MEMBERSHIP_SPECS[kind[: -len("_delete")]]
        conn.execute(
            f"DELETE FROM {spec.table} WHERE "
            + " AND ".join(f"{column} = ?" for column in spec.key),
            _membership_key(spec, payload),
        )
    else:
        raise ValueError(f"unknown mutation kind {kind!r}")


def _membership_values(spec: _MembershipTable, row: Any) -> tuple[Any, ...]:
    """The column values of a membership row, in the table's column order:
    every column present and no other, the key columns non-empty text,
    the rest text, integers or null (a finite float in a ``floats``
    column), a ``_json`` column's value as its canonical JSON text (null
    stays null). Raises KeyError, TypeError or
    ValueError on a row that does not fit, which the fold reports as
    ``payload_invalid``."""
    if not isinstance(row, Mapping):
        raise TypeError(f"a {spec.kind} row must be an object")
    unknown = set(row) - set(spec.columns)
    if unknown:
        raise ValueError(f"unknown {spec.kind} columns {sorted(unknown)}")
    values: list[Any] = []
    for column in spec.columns:
        value = row.get(column) if column in spec.optional else row[column]
        if column.endswith("_json"):
            values.append(None if value is None else _json_text(value))
        elif isinstance(value, bool):
            values.append(int(value))
        elif value is None or isinstance(value, (str, int)):
            values.append(value)
        elif (
            isinstance(value, float) and column in spec.floats and math.isfinite(value)
        ):
            values.append(value)
        else:
            raise TypeError(f"{spec.kind}.{column} must be text, an integer or null")
    _membership_key(spec, row)
    return tuple(values)


def _membership_spec(kind: str) -> _MembershipTable:
    spec = _MEMBERSHIP_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"{kind!r} is not a membership row kind")
    return spec


def _membership_column(spec: _MembershipTable, column: str) -> None:
    if column not in spec.columns:
        raise ValueError(f"{spec.table} has no column {column!r}")


def _sort_rows(rows: list[dict[str, Any]], order_by: Sequence[str]) -> None:
    """Sort rows read in several batches as one ``ORDER BY`` would: null
    first ascending, last descending, the first name deciding first."""
    for name in reversed(order_by):
        column = name.lstrip("-")
        rows.sort(
            key=lambda row: (
                row[column] is not None,
                row[column] if row[column] is not None else 0,
            ),
            reverse=name.startswith("-"),
        )


def _membership_key(
    spec: _MembershipTable, payload: Mapping[str, Any]
) -> tuple[str, ...]:
    """The key column values a membership row or deletion names, each
    non-empty text."""
    key: list[str] = []
    for column in spec.key:
        value = payload[column]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{spec.kind}.{column} must be non-empty text")
        key.append(value)
    return tuple(key)


def _table_rows(
    conn: sqlite3.Connection, table: str
) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    """Every row of a folded table keyed by its key columns, rowid left
    out: the fold compares records, and rowids are the tie order the
    candidate query is graded on elsewhere."""
    columns = [
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({table})")
        if r[1] != "rowid"
    ]
    keys = _TABLE_KEYS[table]
    out: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
        values = tuple(row[c] for c in columns)
        out[tuple(row[k] for k in keys)] = values
    return out


def _key_label(table: str, key: tuple[Any, ...]) -> Any:
    if len(key) == 1:
        return key[0]
    return list(key)


_ROW_COLUMNS = ("ts", "session", "kind")


class _WindowRow(NamedTuple):
    """One telemetry row of an event window, as the log holds it: its
    stamp's first nineteen characters (the window's cut compares on
    them), its columns, and its payload text, parsed again for each
    read so no reader holds another's event."""

    cut: str
    ts: str
    session: str | None
    kind: str
    payload: str


@dataclass(frozen=True)
class _EventWindow:
    """The rows `Store.events_since` read for one store's window: every
    telemetry row whose stamp's first nineteen characters are at or after
    `cut`, in log order, among the rows up to `head_seq`, the log's last
    row when they were read, whose MAC was `head_mac`."""

    cut: str
    head_seq: int
    head_mac: str
    rows: tuple[_WindowRow, ...]


# The event window memo of `Store.events_since(since, memoised=True)`, the
# read `memory_search` makes on every search that returns a hit. The
# uncached read filters the whole log on `substr(ts, 1, 19)`, which no
# index serves, for the few rows inside the window. The log only grows:
# a row is appended at the next sequence number under the write lock and
# never changed, and each row's MAC covers the MAC of the row before it.
# So the memo keeps, per store, the window's rows up to the log's head as
# it read it, keyed on that head: a later read whose cut is at or after the
# memo's checks that the row at the head's sequence number still carries
# the head's MAC, drops the kept rows the new cut excludes, and reads only
# the rows past the head, the new cut applied in SQL. A cut before the
# memo's, a head row gone or carrying another MAC (the tail removed, a row
# appended at its number) or a read inside an open transaction reads the
# whole log as the uncached code does. An edit outside the store that
# leaves the head row's MAC as it was is not seen: a row below the head
# edited with its MAC kept or deleted, and the head row edited with its MAC
# kept (the chain then fails `bettermemory log verify` in each case); the
# memo serves the rows it read. Keyed by the store file and its
# id, so two connections to one store share it; bounded to the most
# recently read stores, not in rows: a store's entry keeps every row in the
# window, its payload text and four short strings, about 1.3 KB a row for
# payloads of about 1 KB (26 MB for a window of 20,000 such rows, measured
# with tracemalloc on Python 3.13), and a window holds whatever the
# lookback's span logged. The lock guards each look-up and each store, the
# reads run outside it. Registered with `_caches`, which empties it before
# each test.
EVENT_WINDOW_STORES = 8

_EVENT_WINDOWS: OrderedDict[tuple[str, str], _EventWindow] = OrderedDict()
_EVENT_WINDOWS_LOCK = threading.Lock()


@_caches.register
def _clear_event_windows() -> None:
    with _EVENT_WINDOWS_LOCK:
        _EVENT_WINDOWS.clear()


# Every memory row's admission inputs for the document frequencies: its
# scopes, the project its ``homes`` row names (null for a row with none)
# and its declared writer, in one query.
_ADMISSION_INPUTS = (
    "SELECT m.id AS id, m.scopes_json AS scopes_json, h.project AS home_project, "
    "m.actor_client AS actor_client, m.actor_model AS actor_model "
    "FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
)


@dataclass
class _DocFreqInputs:
    """What `Store.document_frequencies(..., memoised=True)` keeps for one
    store while its memories stay as they were: the admission inputs of
    every memory row whose scopes parse, grouped by equal inputs, and each
    term's FTS hits. A row is a bit, its position in the scan; `groups`
    maps `(scopes, home project, actor)` to the mask of the rows that carry
    them (the project the row's ``homes`` row names, None for a row with
    none), and `terms` maps a term to the masks of the rows whose body
    matches it and whose body or scopes match it. `head` is the log's last
    memory or home row, `(seq, mac)`, when the inputs were read."""

    head: tuple[int, str]
    groups: tuple[tuple[list[str], str | None, Actor | None, int], ...]
    index: dict[str, int]
    terms: OrderedDict[str, tuple[int, int]]


# The document frequency memo of `Store.document_frequencies(...,
# memoised=True)`, the corpus statistics every search above the prefilter
# threshold prices its BM25 legs with: the per-prompt recall's probe,
# `memory_search` and the review sample. The uncached call reads every
# memory row and runs two FTS queries per term on every search, though the
# rows and the hits change only when a memory does. Every change to the
# memories table is a `memory_put` or `memory_delete` row appended to the
# log in the same transaction (`_mutate`; the fold applies the same rows),
# and each row's MAC covers the rows before it, so the log's last memory
# row names the table's state: the memo keeps a store's admission inputs
# and its terms' hits keyed on that row's sequence number and MAC, and a
# call whose key differs reads them again. A write, an update, a
# tombstone, a restore, a deletion and an import each append such a row; a
# telemetry row does not, so the searches' own events leave the memo warm.
# A record's home is a `home_put` row (beside its memory's, or alone from
# the migration and an import), so the key is the last row of either.
# What is not kept is the admission itself: `admit` is the search's
# predicate (`admission.Admission`), which follows the project table's
# merges and boundaries for the caller, so it runs on every call, once per
# distinct `(scopes, home project, actor)` instead of once per row; the
# counts are then those of the uncached call. A read inside an open
# transaction is served
# uncached, and a result is kept only when the key read after it is the
# key read before it, so a memory committed meanwhile by another
# connection is never kept under the older key. An edit to the memories
# table outside the store, with no log row, is not seen, as with the event
# window memo. Keyed by the store file and its id, so the daemon's worker
# connections share it; bounded to the most recently read stores, and to
# the most recently used terms per store (two masks of one bit per memory
# row each, about 170 bytes a term at 662 memories). The lock guards each
# look-up and each store, the reads run outside it. Registered with
# `_caches`, which empties it before each test.
DOC_FREQ_STORES = 8
DOC_FREQ_TERMS = 4096


def _row_mask(positions: Iterable[int], width: int) -> int:
    """The int whose bit `p` is set for each `p` in `positions`, each
    below `width`; built in a bytearray, linear in `width`."""
    bits = bytearray((width + 7) // 8)
    for position in positions:
        bits[position >> 3] |= 1 << (position & 7)
    return int.from_bytes(bits, "little")


_DOC_FREQS: OrderedDict[tuple[str, str], _DocFreqInputs] = OrderedDict()
_DOC_FREQS_LOCK = threading.Lock()


@_caches.register
def _clear_doc_freqs() -> None:
    with _DOC_FREQS_LOCK:
        _DOC_FREQS.clear()


def _event_from_columns(
    ts: str, session: str | None, kind: str, payload: str
) -> dict[str, Any] | None:
    """A telemetry row's columns in the v8 event shape: ``ts``,
    ``session`` (when the row has one), ``kind``, then the payload's
    fields. None for a mutation or control row, and for a payload that is
    not a JSON object."""
    if kind in _NOT_TELEMETRY:
        return None
    try:
        fields = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None
    event: dict[str, Any] = {"ts": ts}
    if session is not None:
        event["session"] = session
    event["kind"] = kind
    for key, value in fields.items():
        if key not in _ROW_COLUMNS:
            event[key] = value
    return event


def event_import_payload(
    event: Mapping[str, Any], *, imported_from: str = IMPORTED_FROM_V8
) -> tuple[str | None, str | None, str, dict[str, Any]]:
    """The row a v8 event becomes: ``(ts, session, kind, payload)``.

    ``ts``, ``session`` and ``kind`` leave the event for the row's own
    columns; every other field stays in the payload, ``query`` and
    ``probe_query`` redacted the way ``record_event`` redacts them, and
    ``imported_from`` names the source. An absent or empty ``ts`` comes
    back as None (the caller stamps the row). Raises ValueError on an
    event without a string kind, or whose kind names a mutation or a
    control row: those are not events in any store.
    """
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("an event needs a string kind")
    if kind in _NOT_TELEMETRY:
        raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
    ts = event.get("ts")
    session = event.get("session")
    payload = _redact_event_fields(
        {k: v for k, v in event.items() if k not in _ROW_COLUMNS}
    )
    payload["imported_from"] = imported_from
    return (
        ts if isinstance(ts, str) and ts else None,
        session if isinstance(session, str) and session else None,
        kind,
        payload,
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


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
    is, said in the log; its membership reads then find no tables, and
    the fold reads a missing table as empty. ``meta`` gains the minted
    store id. A table an earlier build of these tables created without an
    optional column gains it (``ALTER TABLE ... ADD COLUMN``)."""
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
    if (
        _MEMBERSHIP_OBJECTS <= set(master)
        and meta.get(MEMBERSHIP_STORE_ID)
        and not lacking
    ):
        return
    minted = secrets.token_hex(16)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _MEMBERSHIP_STATEMENTS:
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
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).replace(" ", "").lower():
            raise
        log.warning("store %s is read-only; membership tables not added", path)
        return
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (MEMBERSHIP_STORE_ID,)
    ).fetchone()
    meta[MEMBERSHIP_STORE_ID] = str(row[0])


def _signs_a_segment(segments: Sequence[Segment], key: bytes) -> bool:
    """Does ``key`` sign a segment of the log: is a segment under its
    fingerprint, and does that segment's first row verify under it? The
    MAC is the evidence. A fingerprint is public (meta and every rekey row
    carry it), so a rekey row naming this key proves nothing until the
    key verifies it; the leading segment's first row is the store's own
    ``store_created`` row."""
    own = fingerprint(key)
    return any(
        segment.key_fingerprint == own
        and segment.first_row is not None
        and row_verifies(key, segment.first_row)
        for segment in segments
    )


def _current_key(keyring: KeyRing, path: Path, *, writing: bool) -> bytes | None:
    """The key ring's current key for the store at ``path`` (`open`). A key
    file that is there and cannot be read (`log.KeyUnreadableError`) fails
    the owner's open, naming the file: read as a missing key it would be
    replaced and the log rekeyed, for a failure that may pass in a moment.
    A reader opens without it, as it opens without a missing key, and says
    why in the log."""
    try:
        return keyring.load_current()
    except KeyUnreadableError as exc:
        if writing:
            raise
        log.warning("store %s opens without its key: %s", path, exc)
        return None


# SQLite's primary result codes that, from the query, answer what a file
# is rather than fail to read it: SQLITE_ERROR (here, no ``meta`` table: a
# database that is no store), SQLITE_CORRUPT and SQLITE_NOTADB. Any other
# (SQLITE_CANTOPEN, SQLITE_IOERR, SQLITE_BUSY, SQLITE_PERM, ...) is a read
# that failed, which may pass in a moment, and so is every failure of the
# open itself, whatever its code: an open reads nothing of the file, and
# SQLite answers SQLITE_ERROR there for a URI it will not take, such as
# the one a Windows UNC path gives, whose server becomes the URI's
# authority (`_store_id_at`).
_NOT_A_STORE_CODES = frozenset({1, 11, 26})


def _store_id_at(path: Path) -> str | None:
    """The store id meta records in the store file at ``path``, read
    without opening it as a store (`Store.open` runs the additive schema
    step and sets WAL mode, which would write to it): ``immutable=1``, no
    lock and no ``-wal`` or ``-shm`` created, or, when a ``-wal`` and its
    ``-shm`` are beside the file (a daemon has it open, or crashed with
    it open), ``mode=ro`` first, so the read sees a store whose meta is
    still only in its WAL, as a store a running daemon just created is.

    None when there is no file there or it does not read as a store (no
    database, or one with no store id). OSError when it cannot be read to
    tell (a directory on the way that cannot be searched, a file that
    cannot be opened, by any SQLite error, an I/O error, a store whose
    meta is only in its WAL and whose lock is held): a failure that may
    pass in a moment, or that SQLite meets before it reads a byte, says
    nothing about whether the file still holds a store, and the caller
    keeps the key ring bound to it (`_claim_key_ring`). A symbolic link
    that loops is no file there, as `Path.is_file` reads it: ELOOP, or on
    Windows ERROR_CANT_RESOLVE_FILENAME (1921), the reparse-point cycle,
    which CPython does not fold into ELOOP (`origin.py` reads it the
    same way)."""
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP or getattr(exc, "winerror", None) == 1921:
            return None
        raise
    if not stat.S_ISREG(status.st_mode):
        return None
    resolved = path.resolve()
    wal = resolved.with_name(resolved.name + "-wal")
    shm = resolved.with_name(resolved.name + "-shm")
    queries = ["mode=ro&immutable=1"]
    if wal.exists() and shm.exists():
        queries.insert(0, "mode=ro")
    failed: sqlite3.Error | None = None
    for query in queries:
        try:
            conn = sqlite3.connect(
                f"{resolved.as_uri()}?{query}",
                uri=True,
                timeout=2.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            # The open failed, so nothing of the file was read: no answer.
            # Read as no store, a bound store SQLite cannot open by this
            # URI (one on a network share, on Windows) would release the
            # ring to every second file.
            failed = exc
            continue
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'store_id'"
            ).fetchone()
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in _NOT_A_STORE_CODES:
                failed = exc
            continue
        finally:
            conn.close()
        if row is not None:
            return str(row[0])
    if failed is not None:
        raise OSError(f"{path} could not be read ({failed})")
    return None


def _bound_store_check(store_id: str) -> tuple[Callable[[Path], bool], list[str]]:
    """The ``holds_store`` answer `log.KeyRing.bind_store` asks for, for a
    store with id ``store_id``, and the list it fills with why a bound
    file could not be read. A bound file that cannot be read to tell
    (`_store_id_at` raises) is taken to hold the store still: binding the
    ring to this file instead, for a failure that may pass in a moment,
    would let this file sign for the bound store and move its head, and
    leave the bound store opening for reading only once it is back."""
    unread: list[str] = []

    def holds(other: Path) -> bool:
        try:
            return _store_id_at(other) == store_id
        except OSError as exc:
            unread.append(str(exc))
            return True

    return holds, unread


#: The (store file, bound file) pairs this process has warned about
#: (`_warn_bound_elsewhere`). Once per process, by `config`'s
#: `_DEPRECATED_KEY_WARNED_PATHS` rule: the daemon opens a worker
#: connection per thread (`Store.reopen`), and each is a reader's open
#: that reads the binding again, so a copy left beside the bound store
#: repeated the same warning for every worker of a long-lived daemon.
#: Keyed on both files, so a second copy, or the same copy once the ring
#: is bound to a third file, still gets its own warning.
_BOUND_ELSEWHERE_WARNED: set[tuple[Path, Path]] = set()
#: Makes the guard's test and its add one step. The workers are the case
#: the guard is for, and they can open at once: each worker thread reopens
#: on its first job, and jobs that arrive together reach several workers
#: together. A thread switch between one worker's test and its add would
#: let another find the pair new too, and both would warn.
_BOUND_ELSEWHERE_LOCK = threading.Lock()


def _resolved_for_guard(path: Path) -> Path:
    """`path` resolved for the one-shot guard's key, so two spellings of
    one file share a warning; as given when it cannot be resolved (the
    rule of `config._resolved_for_guard`, for a store file). A symlink
    loop raises RuntimeError before Python 3.13, and a log line must not
    fail the open that says it."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _warn_bound_elsewhere(
    keyring: KeyRing, path: Path, store_id: str, other: Path, unread: Sequence[str]
) -> None:
    """Say in the log that the key ring is another store file's, naming both
    files and the remedy (`_claim_key_ring`, `_key_ring_elsewhere`): a
    warning the first time this process opens ``path`` while the ring is
    bound to ``other``, the same line at DEBUG on every later open of the
    pair (`_BOUND_ELSEWHERE_WARNED`). Every open still reads the binding
    and still opens for reading only; only the repetition is quieter."""
    guard_key = (_resolved_for_guard(path), _resolved_for_guard(other))
    with _BOUND_ELSEWHERE_LOCK:
        first = guard_key not in _BOUND_ELSEWHERE_WARNED
        _BOUND_ELSEWHERE_WARNED.add(guard_key)
    level = logging.WARNING if first else logging.DEBUG
    holds = (
        f"which could not be read to tell whether it still holds that id ({unread[-1]})"
        if unread
        else "which still holds that id"
    )
    log.log(
        level,
        "store %s carries store id %s, and the key ring under %s belongs to "
        "the store at %s, %s; this one opens for reading only, and nothing is "
        "rekeyed or written. Move or remove one of the two stores, or delete %s "
        "to bind the key ring to this one",
        path,
        store_id,
        keyring.keys_dir,
        other,
        holds,
        keyring.binding_path(),
    )


def _key_ring_elsewhere(keyring: KeyRing, path: Path, store_id: str) -> Path | None:
    """The other store file the key ring is bound to when it still holds
    this store id, for a reader's open whose key matches (`open`), else
    None; reads the binding and writes nothing. A reader's open with the
    key can write (the session-start hook records the brief it shows, and
    a worker connection writes as its owner does), so on a second file
    with the bound store's id it would sign rows with that store's key and
    move its head past that store's log. A binding that cannot be read is
    none, as it was before rings were bound."""
    holds, unread = _bound_store_check(store_id)
    try:
        other = keyring.bound_elsewhere(path, holds_store=holds)
    except OSError:
        return None
    if other is not None:
        _warn_bound_elsewhere(keyring, path, store_id, other, unread)
    return other


def _claim_key_ring(keyring: KeyRing, path: Path, store_id: str) -> Path | None:
    """Claim the key ring for the store file ``path``, the owner's opener's
    first step (`open`): bind the ring to it when it is bound to no file,
    or to one that no longer holds this store id (moved, deleted, another
    store there), and return None; return the other store file when the
    ring is bound to one that still does, or that cannot be read to tell,
    and is not this file under another spelling (`log.KeyRing.bind_store`).
    That file is the ring's store: its key, head and anchor are that
    file's, and an open of a second file with its id (a copy in another
    directory, or a file made to borrow a known id) must not retire the
    key, write a new one, set meta, move the head or write the anchor, all
    of which the owner's opener would otherwise do. A ring whose binding
    cannot be read or written is said in the log and the open goes on, as
    it did before rings were bound; the binding is left as it is."""
    holds, unread = _bound_store_check(store_id)
    try:
        binding = keyring.bind_store(path, holds_store=holds)
    except OSError as exc:
        log.warning(
            "store %s: the key ring under %s could not be bound to it (%s)",
            path,
            keyring.keys_dir,
            exc,
        )
        return None
    if binding.elsewhere:
        assert binding.previous is not None
        _warn_bound_elsewhere(keyring, path, store_id, binding.previous, unread)
        return binding.previous
    if binding.written and binding.previous is not None:
        log.info(
            "store %s: the key ring under %s was bound to %s, which no longer "
            "holds store id %s; it is bound to this file now",
            path,
            keyring.keys_dir,
            binding.previous,
            store_id,
        )
    return None


def configured_strict_provenance() -> bool:
    """``[security] strict_provenance`` as `load_config` reads it, so a
    value an organisation pins in a managed config applies here too. A
    config that cannot be read reads as strict, with a warning: it is a
    switch that narrows what is trusted, and a broken file must not widen
    trust by default. Nor may it fail the read that asked: a daemon reads
    this long after its own start, from a file edited since, and a
    section of the wrong type makes `load_config` raise TypeError or
    AttributeError, not only OSError or ValueError.

    Read without the first-run write (``create=False``): a missing file
    reads as the defaults it would be written with, the managed file still
    applied. A read-only command (`memory history`) labels rows through
    this, and must not write a config.toml, nor read a config directory it
    cannot write as a config it cannot read. Every writing entry point
    (the daemon, the hooks, the CLI's writers) has written the file at its
    own start already."""
    from .config import load_config

    try:
        return load_config(create=False).security.strict_provenance
    except Exception as exc:  # noqa: BLE001 - a bad config must not fail a read
        log.warning(
            "[security] strict_provenance could not be read (%s); rows under a "
            "key this machine does not hold read unaccounted",
            exc,
        )
        return True


class Store:
    """One store, one file, one connection. ``Store(directory)`` opens the
    store in the directory, creating it on first use, which is what every
    runtime entry point wants; ``create``, ``open`` and ``open_or_create``
    are the explicit forms, and each takes the directory or the file
    itself."""

    _path: Path
    _conn: sqlite3.Connection
    _keyring: KeyRing
    _key: bytes | None
    # A reader's key for checking, never for signing: the key ring's
    # current key, bound by a read-only open whose meta names another key
    # when the key ring holds the store's origin anchor or that key signs
    # a segment of the log (`open`), or by an owner's open refused because
    # the key ring belongs to another store file (`_bound_elsewhere`).
    # None otherwise; `_key` is the one key a mutation signs with.
    _read_key: bytes | None
    # The store file the key ring is bound to when it is not this one and
    # still holds this store id (`_claim_key_ring`): this connection then
    # writes nothing, and says why. None otherwise.
    _bound_elsewhere: Path | None
    _store_id: str
    _last_appended: LogRow | None
    _in_batch: bool
    _unaccounted_memory_ids: set[str]
    # (id, log_mac) pairs whose pointer into the log verified; a row gets
    # a new MAC on every mutation, so a stale entry never matches.
    _pointer_cache: dict[tuple[str, str], bool]
    # The chain's segments (`log.chain_segments`), each with its key when
    # this machine holds it and whether it is foreign: read off the rekey
    # rows once per connection, on the first pointer that needs it.
    _segments: list[Segment] | None
    # The check of the origin anchor the segment map was read under
    # (`log.chain_trust`); None while `_segments` is.
    _origin: OriginCheck | None
    # `[security] strict_provenance` for this connection: None until the
    # first row in a segment whose key is not held needs it, then read
    # once through `load_config` (`strict_provenance`). An opener that
    # passes it explicitly sets it here and nothing is read.
    _strict_provenance: bool | None

    def __init__(
        self,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> None:
        opened = self.open_or_create(
            path, keys_dir=keys_dir, strict_provenance=strict_provenance
        )
        self._adopt(
            opened._path,
            opened._conn,
            opened._keyring,
            opened._key,
            opened._store_id,
            strict_provenance=opened._strict_provenance,
        )
        self._read_key = opened._read_key
        self._bound_elsewhere = opened._bound_elsewhere

    def _adopt(
        self,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
        *,
        strict_provenance: bool | None = None,
    ) -> None:
        self._path = path
        self._conn = conn
        self._keyring = keyring
        self._key = key
        self._read_key = None
        self._bound_elsewhere = None
        self._store_id = store_id
        self._last_appended = None
        self._in_batch = False
        self._unaccounted_memory_ids = set()
        self._pointer_cache = {}
        self._segments = None
        self._origin = None
        self._strict_provenance = strict_provenance

    @classmethod
    def _bind(
        cls,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
        *,
        strict_provenance: bool | None = None,
    ) -> Store:
        self = cls.__new__(cls)
        self._adopt(
            path, conn, keyring, key, store_id, strict_provenance=strict_provenance
        )
        return self

    @classmethod
    def create(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> Store:
        """A new store at ``path`` with a fresh key. Refuses an existing file.
        Its ``store_created`` row, seq 1, is where this machine's history
        of it begins, and the origin anchor names it; the key ring is bound
        to this file (`log.KeyRing.bind_store`). ``strict_provenance`` is
        `open`'s."""
        path = store_path(path)
        if path.exists():
            raise FileExistsError(f"a store already exists at {path}")
        ensure_owner_only_dir(path.parent, parents=True)
        orphans = orphans_beside(path)
        conn = _connect(path)
        try:
            conn.executescript(SCHEMA)
            conn.executescript(MEMBERSHIP_SCHEMA)
            store_id = secrets.token_hex(16)
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
                store_id,
            )
            key = keyring.create_key()
            from . import __version__

            created = isoformat_utc(datetime.now(timezone.utc))
            meta = {
                "store_id": store_id,
                "schema_version": str(SCHEMA_VERSION),
                "tokenizer_fingerprint": tokenizer_fingerprint(),
                "engine_version": __version__,
                "key_fingerprint": fingerprint(key),
                "created": created,
                MEMBERSHIP_STORE_ID: secrets.token_hex(16),
            }
            if orphans:
                meta[CREATED_AFTER_ORPHANS] = json.dumps([p.name for p in orphans])
            store = cls._bind(
                path, conn, keyring, key, store_id, strict_provenance=strict_provenance
            )
            with store._transaction() as tx:
                tx.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", list(meta.items())
                )
                first = store._append(
                    tx,
                    STORE_CREATED,
                    {
                        "store_id": store_id,
                        "schema_version": SCHEMA_VERSION,
                        "tokenizer_fingerprint": meta["tokenizer_fingerprint"],
                        "engine_version": __version__,
                        "created": created,
                    },
                    session=None,
                )
            store._record_origin(first, key)
            # The new store's id is fresh, so the key ring was bound to no
            # file: this one is the file it belongs to.
            _claim_key_ring(keyring, path, store_id)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            return store
        except Exception:
            conn.close()
            raise

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        allow_rekey: bool = False,
        strict_provenance: bool | None = None,
    ) -> Store:
        """An existing store. Without ``allow_rekey`` (the default) a store
        whose key under the key directory is missing or foreign opens for
        reading and refuses to mutate, naming the directory; with it the
        key is replaced and a ``rekey`` row appended, which splits the
        chain. The owner's own openers pass it (`open_or_create`, which
        the hooks, the daemon and the CLI open through, and the
        migrations); a reader, a copy on another machine or a daemon
        under the wrong key directory never rekeys by accident.

        Meta's ``key_fingerprint`` is not authenticated: anyone with the
        file can change it. So the owner's opener does not take a
        mismatch at its word. When the key under the key directory signs
        a segment of this log (`_signs_a_segment`), the store is this
        machine's own with rows in it that it did not sign: the key stays
        current, meta is set back to it, and a ``rekey`` row closes the
        chain's last segment when that segment is not the key's
        (`_reclaim`). Only a key that signs none of the log is retired
        and replaced, as for a store copied here without its key. With
        the key in place, the owner's opener still closes a last segment
        a ``rekey`` row it did not write named (`_close_foreign_tail`),
        so its own rows after that segment verify. Neither repair can
        fail the open: a store that cannot be written logs why and opens.

        The owner's opener also keeps the origin anchor (the module
        docstring): the ``rekey`` row it writes for a store copied here is
        where this machine's history of it begins (`_take_over_origin`),
        and on any other writing open a store with no anchor gets one
        (`_settle_origin`). Nothing moves an anchor that exists, a lost key
        included. A reader never writes one.

        Before any of that, the owner's opener claims the key ring for this
        file (`_claim_key_ring`): the ring is found by the store id the
        file carries, so a second file with the same id (a copy in another
        directory, or one made to borrow a known id) would otherwise retire
        the first one's key, or sign with it, and move its head. When the
        ring is bound to another file that still holds this store id (or
        that cannot be read to tell), this open writes nothing and opens as
        a reader does, refusing writes even when its key matches, with a
        warning naming both files. A reader never binds the ring; a reader
        whose key matches reads the binding, and on such a second file
        opens the same way, since a reader with the key can write.

        A key file that is there and cannot be read fails the owner's open
        (`log.KeyUnreadableError`) instead of being replaced; a reader
        opens without it.

        ``strict_provenance`` is ``[security] strict_provenance`` for this
        connection; None (the default) reads it from the config the first
        time a row needs it (`strict_provenance`)."""
        path = store_path(path)
        if not path.is_file():
            raise FileNotFoundError(f"no store at {path}")
        conn = _connect(path)
        try:
            meta = {
                str(r["key"]): str(r["value"])
                for r in conn.execute("SELECT key, value FROM meta")
            }
            store_id = meta.get("store_id")
            expected = meta.get("key_fingerprint")
            if not store_id or not expected:
                raise ValueError(f"{path} carries no store id or key fingerprint")
            if not _STORE_ID_PATTERN.fullmatch(store_id):
                raise ValueError(
                    f"{path} carries a store id this build never writes "
                    f"({store_id[:40]!r}); the key directory is searched by it, "
                    f"so the store is not opened"
                )
            schema = int(meta.get("schema_version", "0"))
            if schema > SCHEMA_VERSION:
                raise ValueError(
                    f"{path} is schema {schema}; this build reads up to {SCHEMA_VERSION}"
                )
            _add_membership_tables(conn, path, meta)
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
                store_id,
            )
            key = _current_key(keyring, path, writing=allow_rekey)
            matches = key is not None and fingerprint(key) == expected
            # The owner's opener first claims the key ring for this file
            # (`_claim_key_ring`): one that is bound to another file still
            # holding this store id is that file's, and this open writes
            # nothing to it or with it. A reader whose key matches would
            # sign with it too, and asks the same, writing no binding
            # (`_key_ring_elsewhere`); one without the key writes nothing.
            if allow_rekey:
                elsewhere = _claim_key_ring(keyring, path, store_id)
            elif matches:
                elsewhere = _key_ring_elsewhere(keyring, path, store_id)
            else:
                elsewhere = None
            if matches and elsewhere is None:
                store = cls._bind(
                    path,
                    conn,
                    keyring,
                    key,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                if allow_rekey:
                    store._close_foreign_tail()
                    store._settle_origin()
                return store
            if not allow_rekey or elsewhere is not None:
                reader = cls._bind(
                    path,
                    conn,
                    keyring,
                    None,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                reader._bound_elsewhere = elsewhere
                # Meta's fingerprint is only what the file says. When this
                # machine has a history of the store (its key ring holds
                # an origin anchor for it, or its key signs a segment of
                # the log, as the owner's opener reads it below), an editor
                # who pointed meta at another key must not thereby switch
                # the reads' checks off, which left a planted record its
                # stored label here while `log verify` read the store
                # tampered. The key is bound for checking pointers, never
                # for writing, and nothing is written to set meta back.
                # Binding it can only take a stored label away: a row
                # still verifies under its own segment's key. An owner's
                # open refused for a key ring bound elsewhere checks under
                # a key meta names too, as the bound store's reads would.
                if key is not None and (
                    matches
                    or keyring.read_origin() is not None
                    or _signs_a_segment(
                        chain_segments(
                            conn,
                            keyring=keyring,
                            current_fingerprint=expected,
                            current_key=key,
                        ),
                        key,
                    )
                ):
                    reader._read_key = key
                return reader
            segments = chain_segments(
                conn, keyring=keyring, current_fingerprint=expected, current_key=key
            )
            if key is not None and _signs_a_segment(segments, key):
                store = cls._bind(
                    path,
                    conn,
                    keyring,
                    key,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                store._reclaim(segments, meta_fingerprint=expected)
                store._settle_origin()
                return store
            # The key that signed the chain's last segment, as the log
            # names it: the rekey row records what it replaced, and meta
            # is only what the file says.
            previous = segments[-1].key_fingerprint if segments else expected
            if key is not None:
                keyring.retire_current()
            key = keyring.create_key()
            store = cls._bind(
                path, conn, keyring, key, store_id, strict_provenance=strict_provenance
            )
            with store._transaction() as tx:
                tx.execute(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                    (fingerprint(key),),
                )
                rekeyed = store._append(
                    tx,
                    REKEY,
                    {"old_fingerprint": previous, "new_fingerprint": fingerprint(key)},
                    session=None,
                )
            log.warning(
                "store %s was opened without its key (fingerprint %s); a new key "
                "%s was written and the log rekeyed",
                path,
                previous[:16],
                fingerprint(key)[:16],
            )
            store._take_over_origin(rekeyed)
            return store
        except Exception:
            conn.close()
            raise

    @classmethod
    def open_or_create(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> Store:
        """Open the store at `path`, or create one. Refuses to create one
        in a directory that holds an un-migrated bettermemory 8 store: a
        server pointed at such a directory would otherwise serve an empty
        store and say nothing, and `bettermemory migrate v8` is the
        answer. `create` itself is unchanged. ``strict_provenance`` is
        `open`'s."""
        path = store_path(path)
        if path.is_file():
            # The owner-process opener: a store this machine writes to
            # whose key moved is its owner's to rekey.
            return cls.open(
                path,
                keys_dir=keys_dir,
                allow_rekey=True,
                strict_provenance=strict_provenance,
            )
        refuse_unmigrated_v8(path)
        return cls.create(path, keys_dir=keys_dir, strict_provenance=strict_provenance)

    def _close_foreign_tail(self) -> None:
        """Close the chain's last segment when a ``rekey`` row this key did
        not write named it: the owner's opener, with meta and the key in
        agreement. Rows this store appends after a forged ``rekey`` row
        would otherwise sit in the segment that row opened, under a key
        nobody here holds, where `log verify` never checks them and only
        the reads' fallback to this machine's own key verifies them
        (`_pointer_verified`). One indexed lookup when there is nothing to
        close; the write lock only when there is."""
        assert self._key is not None
        own = fingerprint(self._key)
        named = last_rekey_name(self._conn)
        if named is None or named == own:
            return
        row: LogRow | None = None
        try:
            with self._transaction() as tx:
                # Another opener may have closed it while this one waited
                # for the lock.
                named = last_rekey_name(tx)
                if named is not None and named != own:
                    row = self._append(
                        tx,
                        REKEY,
                        {"old_fingerprint": named, "new_fingerprint": own},
                        session=None,
                    )
        except (sqlite3.Error, OSError) as exc:
            log.warning(
                "store %s: the log's last segment is under key %s, not this "
                "store's key %s, and it could not be closed (%s); rows written "
                "here will not verify until it is",
                self._path,
                (named or "")[:16],
                own[:16],
                exc,
            )
            return
        if row is None or named is None:
            return
        self._forget_segments()
        log.warning(
            "store %s: the log's last segment, opened by a rekey row this host "
            "did not write, is under key %s; its rows read unaccounted and a "
            "rekey row at seq %d returns the chain to this store's key %s. "
            "`bettermemory log verify` reports the segment",
            self._path,
            named[:16],
            row.seq,
            own[:16],
        )

    def _reclaim(self, segments: Sequence[Segment], *, meta_fingerprint: str) -> None:
        """Keep this machine's key for a store whose meta names another key
        although that key signs a segment of the log (`open`): set meta back
        to the key and, when the chain's last segment is not the key's,
        append the ``rekey`` row that returns the chain to it, naming that
        segment's key as the log gives it, never meta's. The foreign
        segments stay in the log, where every read labels their rows
        ``unaccounted`` and `log verify` reports them."""
        assert self._key is not None
        own = fingerprint(self._key)
        tail = segments[-1].key_fingerprint if segments else own
        foreign = ", ".join(
            f"seq {segment.first_seq} under key {segment.key_fingerprint[:16]}"
            for segment in segments
            if segment.foreign
        )
        appended: LogRow | None = None
        try:
            with self._transaction() as tx:
                tx.execute(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", (own,)
                )
                # The last rekey row names the tail when there is one: read
                # under the lock, so a rekey row another opener appended
                # while this one waited (closing the same segment) counts.
                named = last_rekey_name(tx)
                if named is not None:
                    tail = named
                if tail != own:
                    appended = self._append(
                        tx,
                        REKEY,
                        {"old_fingerprint": tail, "new_fingerprint": own},
                        session=None,
                    )
        except (sqlite3.Error, OSError) as exc:
            log.warning(
                "store %s: meta names key %s but this host's key %s signs the "
                "log; the store opens with that key, and meta could not be "
                "set back (%s)",
                self._path,
                meta_fingerprint[:16],
                own[:16],
                exc,
            )
            return
        self._forget_segments()
        closing = (
            "meta is set back to it"
            if appended is None
            else f"a rekey row at seq {appended.seq} returns the chain to it"
        )
        log.warning(
            "store %s: meta named key %s, but this host's key %s signs the log, "
            "so it stays the store's key and no new key is written; %s. Rows no "
            "key on this host signed, after rows it did: %s. They read "
            "unaccounted, and `bettermemory log verify` reports them",
            self._path,
            meta_fingerprint[:16],
            own[:16],
            closing,
            foreign or "none",
        )

    def _forget_segments(self) -> None:
        """Drop this connection's segment map and the anchor check it was
        read under, so the next pointer check reads both again: after a
        repair appended a ``rekey`` row, or an anchor was written."""
        self._segments = None
        self._origin = None

    def _record_origin(self, row: LogRow, key: bytes) -> None:
        """Anchor this machine's history of the store at ``row``, signed
        by ``key``, unless an anchor is already there
        (`log.KeyRing.write_origin`). A key ring that cannot be written
        logs why, as the head does: the store opens, and a later writing
        open anchors it (`_settle_origin`)."""
        try:
            written = self._keyring.write_origin(
                seq=row.seq, mac=row.mac, key_fingerprint=fingerprint(key)
            )
        except OSError as exc:
            log.warning("origin anchor for %s not written: %s", self._path, exc)
            return
        if written:
            self._forget_segments()

    def _settle_origin(self) -> None:
        """The owner's opener, on a store with no origin anchor: a store
        from before the anchor existed, or one only read here so far. The
        anchor goes on the first row of the earliest segment whose key this
        machine holds, current or retired, and only when that row verifies
        under the key its segment is held by; with no such segment nothing
        is written. Trust on first use: whatever was done to the file
        before this open is taken as it stands, so a history rewritten
        before it is not caught by the anchor (the rule without one still
        is). On a store whose anchor names no key (written before anchors
        named one) it names the key instead (`_name_origin_key`). Costs one
        file read on a store whose anchor names its key, and never fails
        the open."""
        assert self._key is not None
        existing = self._keyring.read_origin()
        if existing is not None:
            if existing.key_fingerprint is None:
                self._name_origin_key(existing)
            return
        try:
            segments = chain_segments(
                self._conn,
                keyring=self._keyring,
                current_fingerprint=fingerprint(self._key),
                current_key=self._key,
            )
            held = next((segment for segment in segments if segment.held), None)
            if held is None or held.key is None or held.first_row is None:
                return
            verifies = row_verifies(held.key, held.first_row)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            # A row an editor left unreadable (a seq that is not a number;
            # a MAC that is not ASCII reads as one that does not verify,
            # below): no anchor, and the open goes on.
            log.warning(
                "origin anchor for %s not written: the log could not be read (%s)",
                self._path,
                exc,
            )
            return
        if not verifies:
            log.warning(
                "store %s: the first row of the earliest segment this host holds "
                "(seq %d) does not verify under its key; no origin anchor is "
                "written. `bettermemory log verify` reports the row",
                self._path,
                held.first_seq,
            )
            return
        self._record_origin(held.first_row, held.key)

    def _name_origin_key(self, origin: HistoryOrigin) -> None:
        """Name the key that signed the anchored row, on an anchor written
        before anchors named their key, when a key this machine holds still
        verifies the row (the anchor holds): from then on a row rewritten
        under its own MAC reads as the mismatch it is, and one whose key is
        later lost reads ``unverifiable`` rather than as tampering
        (`log.check_origin`). The anchor itself does not move. An anchor
        that does not hold stays as it is and keeps reading as before.
        Never fails the open."""
        try:
            check = check_origin(
                self._conn, keyring=self._keyring, current_key=self._key
            )
        except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            log.warning(
                "store %s: the origin anchor's key not named: the log could not "
                "be read (%s)",
                self._path,
                exc,
            )
            return
        if not check.holds or check.signer is None:
            return
        try:
            self._keyring.name_origin_key(origin, check.signer)
        except OSError as exc:
            log.warning(
                "store %s: the origin anchor's key not named: %s", self._path, exc
            )

    def _take_over_origin(self, row: LogRow) -> None:
        """The owner's opener wrote a new key and the ``rekey`` row ``row``
        for a store whose key it did not hold: where this machine's history
        of the store begins, when no anchor names an earlier row.

        An anchor that exists stays where it is, always. When this machine
        lost the key that signed the anchored row (the key file is gone),
        no key here verifies that row any more; the anchor then reads
        ``unverifiable`` (`log.check_origin`), the store's reads fall back
        to the rule without an anchor, and putting the key file back makes
        it hold again. Moving it to ``row`` instead would accept, for good,
        whatever an editor did to the anchored row before the key went. An
        anchor that names no key is given one when a held key, the one just
        retired among them, still verifies its row (`_settle_origin`)."""
        if self._keyring.read_origin() is None:
            assert self._key is not None
            self._record_origin(row, self._key)
            return
        self._settle_origin()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def keyring(self) -> KeyRing:
        return self._keyring

    @property
    def keys_dir(self) -> Path:
        return self._keyring.keys_dir

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def key_fingerprint(self) -> str:
        return self.meta()["key_fingerprint"]

    @property
    def has_key(self) -> bool:
        return self._key is not None

    @property
    def can_verify(self) -> bool:
        """Whether this connection checks pointers into the log: it holds
        the key it writes with, or, opened for reading with meta naming a
        key this machine does not hold, this machine has a history of the
        store (an origin anchor, or a segment its current key signs; see
        `open`). ``has_key`` is the narrower question, whether it may
        write."""
        return self._verify_key is not None

    @property
    def _verify_key(self) -> bytes | None:
        """The key reads check this machine's own rows under: the one a
        mutation signs with, else a reader's (`_read_key`)."""
        return self._key if self._key is not None else self._read_key

    @property
    def strict_provenance(self) -> bool:
        """``[security] strict_provenance`` for this connection: as the
        opener passed it, or read once through the config the first time a
        row in a segment whose key is not held is labelled. A store under
        one key throughout never reads it."""
        strict = self._strict_provenance
        if strict is None:
            strict = self._strict_provenance = configured_strict_provenance()
        return strict

    def meta(self) -> dict[str, str]:
        return {
            str(r["key"]): str(r["value"])
            for r in self._conn.execute("SELECT key, value FROM meta ORDER BY key")
        }

    def store_orphans(self) -> list[Path]:
        """The saved orphans this store was created after that are still
        beside it, oldest first; empty for a store that existed before,
        including one copied back from an orphan, which carries no such
        names."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CREATED_AFTER_ORPHANS,)
        ).fetchone()
        if row is None:
            return []
        directory = self._path.parent
        return sorted(
            (
                directory / name
                for name in orphan_names(str(row[0]))
                if (directory / name).is_file()
            ),
            key=orphan_order,
        )

    # -- transactions -------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One mutating method's transaction. Inside a ``batch`` the
        batch owns the transaction and the head write, and this yields
        the connection as it is."""
        conn = self._conn
        if self._in_batch:
            yield conn
            return
        self._last_appended = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        self._move_head()

    @contextlib.contextmanager
    def batch(self) -> Iterator[Store]:
        """One transaction around many mutations: everything put or
        deleted inside the block commits together or not at all, and the
        head checkpoint moves once, at the commit. A migration runs in
        one, so a failure part way leaves the store as it was. Batches
        do not nest."""
        if self._in_batch:
            raise RuntimeError(f"a batch is already open on store {self._path}")
        conn = self._conn
        self._last_appended = None
        # BEGIN first, then the flag: a BEGIN that fails (the busy wait
        # against another writer ran out) must leave the store as it
        # was. With the flag set first and only the body guarded, that
        # failure left _in_batch set for the store's lifetime: every
        # later mutation ran in autocommit, the head never moved, and
        # every later batch was refused as already open.
        conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield self
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._in_batch = False
        conn.execute("COMMIT")
        self._move_head()

    def _move_head(self) -> None:
        appended = self._last_appended
        if appended is not None:
            try:
                self._keyring.write_head(seq=appended.seq, mac=appended.mac)
            except OSError as exc:
                log.warning("head checkpoint for %s not written: %s", self._path, exc)

    def _append(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None,
        ts: str | None = None,
    ) -> LogRow:
        if self._key is None:
            if self._bound_elsewhere is not None:
                raise RuntimeError(
                    f"store {self._path} is open for reading only: the key ring "
                    f"under {self._keyring.keys_dir} belongs to the store at "
                    f"{self._bound_elsewhere}, which still holds store id "
                    f"{self._store_id}. Move or remove one of the two stores, or "
                    f"delete {self._keyring.binding_path()} to bind the key ring "
                    f"to this one."
                )
            raise RuntimeError(
                f"store {self._path} is open for reading only: no key under "
                f"{self._keyring.keys_dir} matches its fingerprint. Put the "
                f"store's key back there, or open it with allow_rekey=True from "
                f"the process that owns the store to write a new key and start a "
                f"new chain segment."
            )
        row = append_row(
            conn, key=self._key, kind=kind, payload=payload, session=session, ts=ts
        )
        self._last_appended = row
        return row

    def _mutate(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None,
    ) -> LogRow:
        """Log a mutation and apply it, inside the caller's transaction.
        The row is appended first so the record it puts can carry the
        row's MAC."""
        row = self._append(conn, kind, payload, session=session)
        _apply_mutation(conn, kind, payload, log_mac=row.mac)
        return row

    def _home_row(
        self, record_id: str, kind: str, home: RecordHome | None
    ) -> dict[str, Any] | None:
        """The ``homes`` row a put writes beside its record, or None: no
        home given, or the record already has this very home. A record's
        home is written once (design 1); a put that names another home for
        a homed record raises ValueError before anything is written."""
        if home is None:
            return None
        project, checkout, rule = home
        if not isinstance(project, str) or not project:
            raise ValueError("a home names a project (or global)")
        if not isinstance(rule, str) or not rule:
            raise ValueError("a home names the rule that decided it")
        existing = self._conn.execute(
            "SELECT project, checkout, rule FROM homes WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if existing is not None:
            if (existing["project"], existing["checkout"], existing["rule"]) == (
                project,
                checkout,
                rule,
            ):
                return None
            raise ValueError(f"{record_id} already has a home; a home is written once")
        return {
            "record_id": record_id,
            "kind": kind,
            "project": project,
            "checkout": checkout,
            "rule": rule,
            "decided": isoformat_utc(datetime.now(timezone.utc)),
        }

    # -- memories -----------------------------------------------------------

    def put_memory(
        self,
        memory: Memory,
        *,
        provenance: str | None = None,
        filename: str | None = None,
        session: str | None = None,
        home: RecordHome | None = None,
        standing: StandingAnswer | None = None,
    ) -> Memory:
        """Insert or replace the record. ``provenance`` None keeps the row's
        label (``local`` for a new row); ``filename`` None keeps the row's
        name. Both are resolved before the row is logged, so the log
        carries what the table holds. ``home`` (design 8.5) writes the
        record's ``homes`` row in the same transaction; None leaves the
        homes table as it is. ``standing`` (unit RS-F7 part 1) writes the
        record's ``standing`` row for this body in the same transaction;
        None leaves the row as it is, and a row naming another body is
        read as no answer (`standing.py`)."""
        if provenance is not None and provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(
                f"provenance must be one of {sorted(_WRITABLE_PROVENANCE)}, got {provenance!r}"
            )
        existing = self._conn.execute(
            "SELECT provenance, filename FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()
        if existing is not None:
            provenance = provenance or str(existing["provenance"])
            filename = filename if filename is not None else existing["filename"]
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": provenance or LOCAL,
            "filename": filename,
        }
        standing_row = (
            None if standing is None else self._standing_row(memory, standing)
        )
        with self._transaction() as tx:
            home_row = self._home_row(memory.id, "memory", home)
            self._mutate(tx, "memory_put", payload, session=session)
            if home_row is not None:
                self._mutate(tx, "home_put", {"home": home_row}, session=session)
            if standing_row is not None:
                self._mutate(
                    tx, "standing_put", {"standing": standing_row}, session=session
                )
        return memory

    @staticmethod
    def _standing_row(memory: Memory, answer: StandingAnswer) -> dict[str, Any]:
        """The ``standing`` row an answer for `memory`'s body is written
        as, checked before anything is written."""
        probability = answer.probability
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(float(probability))
            or not 0.0 <= float(probability) <= 1.0
        ):
            raise ValueError("a standing answer is a probability from 0 to 1")
        if not answer.source or not answer.answered_by:
            raise ValueError("a standing answer names its source and who gave it")
        return {
            "record_id": memory.id,
            "probability": float(probability),
            "body_sha256": body_sha256(memory.body),
            "source": answer.source,
            "answered_by": answer.answered_by,
            "answered": isoformat_utc(datetime.now(timezone.utc)),
        }

    def put_standing(
        self,
        record_id: str,
        answer: StandingAnswer,
        *,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Write the standing answer for the active record's current body
        and return the row. Raises NotFoundError for a record that is not
        active."""
        row = self._standing_row(self.get_memory(record_id), answer)
        with self._transaction() as tx:
            self._mutate(tx, "standing_put", {"standing": row}, session=session)
        return row

    def standing_rows(self, ids: Collection[str] | None = None) -> list[dict[str, Any]]:
        """Every active record (or those of `ids`) with what its standing
        is read from: id, category, ``updated``, scopes, the project its
        ``homes`` row names (None for none), its row's ``log_mac`` (None
        for a row written without one) and its ``standing`` row's
        probability and body sha (None for no row), newest ``updated``
        first. Bodies are not read: `standing.py` hashes one only when its
        row changed. A store whose file could not take the membership
        tables reads as having no home and no answer."""
        present = {
            str(r[0])
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('homes', 'standing')"
            )
        }
        home = "h.project" if "homes" in present else "NULL"
        answer = (
            "s.probability, s.body_sha256" if "standing" in present else "NULL, NULL"
        )
        joins = ""
        if "homes" in present:
            joins += " LEFT JOIN homes h ON h.record_id = m.id"
        if "standing" in present:
            joins += " LEFT JOIN standing s ON s.record_id = m.id"
        select = (
            f"SELECT m.id, m.category, m.updated, m.scopes_json, {home}, "
            f"m.log_mac, {answer} FROM memories m{joins}"
        )
        order = " ORDER BY m.updated DESC, m.id DESC"
        if ids is None:
            batches: list[tuple[str, list[str]]] = [(select + order, [])]
        else:
            wanted = list(dict.fromkeys(ids))
            batches = [
                (
                    f"{select} WHERE m.id IN ({','.join('?' * len(chunk))}){order}",
                    chunk,
                )
                for chunk in (
                    wanted[start : start + _PROVENANCE_BATCH]
                    for start in range(0, len(wanted), _PROVENANCE_BATCH)
                )
            ]
        out: list[dict[str, Any]] = [
            {
                "id": str(raw[0]),
                "category": None if raw[1] is None else str(raw[1]),
                "updated": str(raw[2]),
                "scopes": tuple(_load_json(raw[3], [])),
                "recorded": None if raw[4] is None else str(raw[4]),
                "log_mac": None if raw[5] is None else str(raw[5]),
                "probability": None if raw[6] is None else float(raw[6]),
                "body_sha256": None if raw[7] is None else str(raw[7]),
            }
            for sql, params in batches
            for raw in self._conn.execute(sql, params)
        ]
        if len(batches) > 1:
            out.sort(key=lambda row: (row["updated"], row["id"]), reverse=True)
        return out

    def body_of(self, record_id: str) -> str | None:
        """The active record's body, or None."""
        row = self._conn.execute(
            "SELECT body FROM memories WHERE id = ?", (record_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def get_memory(self, memory_id: str) -> Memory:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return _row_to_memory(row)

    def has_memory(self, memory_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            is not None
        )

    def get_many(self, memory_ids: Sequence[str]) -> list[Memory]:
        """The records found, in the order asked; unknown ids are skipped."""
        out: list[Memory] = []
        for memory_id in memory_ids:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is not None:
                out.append(_row_to_memory(row))
        return out

    def iter_memories(self) -> Iterator[Memory]:
        for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
            yield _row_to_memory(row)

    def iter_memory_rows(self) -> Iterator[MemoryRow]:
        """Every active record with its provenance and filename, in
        rowid order: what the mirror writes."""
        for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
            yield MemoryRow(
                memory=_row_to_memory(row),
                provenance=str(row["provenance"]),
                filename=None if row["filename"] is None else str(row["filename"]),
            )

    def memory_ids(self) -> list[str]:
        return [
            str(r["id"])
            for r in self._conn.execute("SELECT id FROM memories ORDER BY rowid")
        ]

    def count_memories(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def filename_for(self, memory_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT filename FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return None if row["filename"] is None else str(row["filename"])

    # -- tombstones ---------------------------------------------------------

    def tombstone(
        self,
        memory_id: str,
        reason: str,
        *,
        session: str | None = None,
        now: datetime | None = None,
    ) -> TombstonedMemory:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        memory = _row_to_memory(row)
        removed = now or datetime.now(timezone.utc)
        dead = TombstonedMemory(
            **{
                name: getattr(memory, name)
                for name in TombstonedMemory.model_fields
                if name not in ("removed", "removed_reason", "removed_session")
            },
            removed=removed,
            removed_reason=reason,
            removed_session=session,
        )
        payload = {
            "tombstone": dead.model_dump(mode="json"),
            "provenance": str(row["provenance"]),
            "filename": row["filename"],
            "links": json.loads(row["links_json"]),
            "corroborations": int(row["corroborations"]),
            "last_corroborated": row["last_corroborated"],
        }
        with self._transaction() as tx:
            self._mutate(tx, "memory_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "tombstone_put", payload, session=session)
        return dead

    def put_tombstone(
        self,
        dead: TombstonedMemory,
        *,
        provenance: str | None = None,
        filename: str | None = None,
        links: Sequence[MemoryLink | Mapping[str, Any]] = (),
        corroborations: int = 0,
        last_corroborated: datetime | None = None,
        session: str | None = None,
    ) -> TombstonedMemory:
        """Insert or replace a tombstone row directly, with the link list
        and the corroboration rollup the record carried while active, so
        a later ``restore`` is as lossless as one after ``tombstone``.
        ``provenance`` None keeps the row's label (``local`` for a new
        row) and ``filename`` None the row's name, as ``put_memory`` does.
        Refuses an id that is active: a record is in one table or the
        other."""
        if provenance is not None and provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(
                f"provenance must be one of {sorted(_WRITABLE_PROVENANCE)}, got {provenance!r}"
            )
        if self.has_memory(dead.id):
            raise ValueError(f"{dead.id} is active; tombstone it instead")
        existing = self._conn.execute(
            "SELECT provenance, filename FROM tombstones WHERE id = ?", (dead.id,)
        ).fetchone()
        if existing is not None:
            provenance = provenance or str(existing["provenance"])
            filename = filename if filename is not None else existing["filename"]
        payload = {
            "tombstone": dead.model_dump(mode="json"),
            "provenance": provenance or LOCAL,
            "filename": filename,
            "links": [
                MemoryLink.model_validate(link).model_dump(
                    mode="json", exclude_none=True
                )
                for link in links
            ],
            "corroborations": int(corroborations),
            "last_corroborated": _iso_opt(last_corroborated),
        }
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_put", payload, session=session)
        return dead

    def get_tombstone(self, memory_id: str) -> TombstonedMemory:
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return _row_to_tombstone(row)

    def tombstone_links(self, memory_id: str) -> list[MemoryLink]:
        """The links a tombstone keeps beside its record, which `restore`
        brings back with it."""
        row = self._conn.execute(
            "SELECT links_json FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return [
            MemoryLink.model_validate(entry)
            for entry in _load_json(row["links_json"], [])
        ]

    def iter_tombstones(self) -> Iterator[TombstonedMemory]:
        for row in self._conn.execute(
            "SELECT * FROM tombstones ORDER BY removed, rowid"
        ):
            yield _row_to_tombstone(row)

    def iter_tombstone_rows(self) -> Iterator[TombstoneRow]:
        """Every tombstone with what its row keeps beside the record, in
        removal order: what the mirror writes."""
        for row in self._conn.execute(
            "SELECT * FROM tombstones ORDER BY removed, rowid"
        ):
            yield TombstoneRow(
                tombstone=_row_to_tombstone(row),
                provenance=str(row["provenance"]),
                filename=None if row["filename"] is None else str(row["filename"]),
                links=list(_load_json(row["links_json"], [])),
                corroborations=int(row["corroborations"]),
                last_corroborated=_dt_opt(row["last_corroborated"]),
            )

    def count_tombstones(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0])

    def restore(self, memory_id: str, *, session: str | None = None) -> Memory:
        """Bring a tombstoned record back as an active one, labelled
        ``local``: this store's own code path put it back."""
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        dead = _row_to_tombstone(row)
        memory = Memory(
            **{
                name: getattr(dead, name)
                for name in Memory.model_fields
                if name not in ("links", "corroborations", "last_corroborated")
            },
            links=[
                MemoryLink.model_validate(entry)
                for entry in json.loads(row["links_json"])
            ],
            corroborations=int(row["corroborations"]),
            last_corroborated=_dt_opt(row["last_corroborated"]),
        )
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": LOCAL,
            "filename": row["filename"],
        }
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "memory_put", payload, session=session)
        return memory

    def delete_tombstone(self, memory_id: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM tombstones WHERE id = ?", (memory_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(memory_id)
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)

    # -- episodes -----------------------------------------------------------

    def put_episode(
        self,
        episode: Episode,
        *,
        session: str | None = None,
        home: RecordHome | None = None,
    ) -> Episode:
        """Insert or replace the episode; ``home`` writes its ``homes`` row
        in the same transaction, as ``put_memory`` does."""
        payload = {"episode": episode.model_dump(mode="json")}
        with self._transaction() as tx:
            home_row = self._home_row(episode.id, "episode", home)
            self._mutate(tx, "episode_put", payload, session=session)
            if home_row is not None:
                self._mutate(tx, "home_put", {"home": home_row}, session=session)
        return episode

    def get_episode(self, episode_id: str) -> Episode:
        row = self._conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(episode_id)
        return _row_to_episode(row)

    def iter_episodes(self, *, session_id: str | None = None) -> Iterator[Episode]:
        if session_id is None:
            cursor = self._conn.execute("SELECT * FROM episodes ORDER BY rowid")
        else:
            cursor = self._conn.execute(
                "SELECT * FROM episodes WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            )
        for row in cursor:
            yield _row_to_episode(row)

    def count_episodes(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def delete_episode(self, episode_id: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(episode_id)
        with self._transaction() as tx:
            self._mutate(tx, "episode_delete", {"id": episode_id}, session=session)

    # -- verifications ------------------------------------------------------

    def put_verification(
        self,
        memory_id: str,
        *,
        verified_at: datetime,
        machine_id: str | None = None,
        verified_head: str | None = None,
        verified_paths: Sequence[str] = (),
        verified_commits: Sequence[str] = (),
        verified_versions: Sequence[str] = (),
        verified_absent_paths: Sequence[str] = (),
        session: str | None = None,
    ) -> None:
        """This host's stamp on a record (``machine_id`` None until the
        per-machine identity of phase 3 fills it). One row per memory and
        machine; a later stamp replaces the earlier."""
        payload = {
            "memory_id": memory_id,
            "machine_id": machine_id,
            "verified_at": _iso(verified_at),
            "verified_head": verified_head,
            "verified_paths": list(verified_paths),
            "verified_commits": list(verified_commits),
            "verified_versions": list(verified_versions),
            "verified_absent_paths": list(verified_absent_paths),
        }
        with self._transaction() as tx:
            self._mutate(tx, "verification_put", payload, session=session)

    def verifications_for(
        self, memory_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        ids = list(dict.fromkeys(memory_ids))
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT * FROM verifications "
                f"WHERE memory_id IN ({placeholders}) ORDER BY memory_id, machine_id",
                batch,
            ):
                out.setdefault(str(row["memory_id"]), []).append(_verification_row(row))
        return out

    def delete_verification(
        self,
        memory_id: str,
        *,
        machine_id: str | None = None,
        session: str | None = None,
    ) -> None:
        found = self._conn.execute(
            "SELECT 1 FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (memory_id, machine_id),
        ).fetchone()
        if found is None:
            raise NotFoundError((memory_id, machine_id))
        with self._transaction() as tx:
            self._mutate(
                tx,
                "verification_delete",
                {"memory_id": memory_id, "machine_id": machine_id},
                session=session,
            )

    # -- conflicts, imports, quarantine ------------------------------------

    def put_conflict(
        self, record: Mapping[str, Any], *, session: str | None = None
    ) -> None:
        conflict = {c: record.get(c) for c in _CONFLICT_COLUMNS}
        for required in ("id", "a_id", "b_id"):
            if not conflict[required]:
                raise ValueError(f"a conflict needs {required}")
        with self._transaction() as tx:
            self._mutate(tx, "conflict_put", {"conflict": conflict}, session=session)

    def list_conflicts(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _CONFLICT_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_CONFLICT_COLUMNS)} FROM conflicts ORDER BY created, id"
            )
        ]

    def delete_conflict(self, conflict_id: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(conflict_id)
        with self._transaction() as tx:
            self._mutate(tx, "conflict_delete", {"id": conflict_id}, session=session)

    def put_import(
        self,
        source: str,
        *,
        content_hash: str,
        imported_at: str,
        memory_id: str | None = None,
        session: str | None = None,
    ) -> None:
        payload = {
            "source": source,
            "content_hash": content_hash,
            "imported_at": imported_at,
            "memory_id": memory_id,
        }
        with self._transaction() as tx:
            self._mutate(tx, "import_put", payload, session=session)

    def list_imports(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _IMPORT_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_IMPORT_COLUMNS)} FROM imports ORDER BY source"
            )
        ]

    def delete_import(self, source: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM imports WHERE source = ?", (source,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(source)
        with self._transaction() as tx:
            self._mutate(tx, "import_delete", {"source": source}, session=session)

    def put_quarantine(
        self,
        name: str,
        *,
        reason: str,
        at: str,
        detail: str | None = None,
        remote: str | None = None,
        size: int | None = None,
        sha256: str | None = None,
        session: str | None = None,
    ) -> None:
        payload = {
            "name": name,
            "reason": reason,
            "detail": detail,
            "remote": remote,
            "at": at,
            "size": size,
            "sha256": sha256,
        }
        with self._transaction() as tx:
            self._mutate(tx, "quarantine_put", payload, session=session)

    def list_quarantine(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _QUARANTINE_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_QUARANTINE_COLUMNS)} FROM quarantine ORDER BY name"
            )
        ]

    def delete_quarantine(self, name: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM quarantine WHERE name = ?", (name,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(name)
        with self._transaction() as tx:
            self._mutate(tx, "quarantine_delete", {"name": name}, session=session)

    # -- membership (design 8.5) --------------------------------------------
    #
    # The tables `membership.py` reads and writes. Every change is a
    # mutation row of the table's own kind, applied by `_apply_mutation`
    # as the fold applies it; the membership rows of design 8.10 are
    # control rows written by `record_membership`. Nothing here decides
    # membership: these are the rows, `membership.py` is the rule.

    @property
    def in_batch(self) -> bool:
        """Whether a `batch` is open on this store."""
        return self._in_batch

    @property
    def membership_store_id(self) -> str:
        """The store id of design 8.7: what exports carry and what, beside
        the machine id, keys a machine at sync and import."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (MEMBERSHIP_STORE_ID,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"store {self._path} carries no membership store id")
        return str(row[0])

    def put_membership(
        self, kind: str, row: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """Insert or replace one row of the membership table whose mutation
        kind prefix is `kind` (``home``, ``checkout``, ...): every column
        given, a ``_json`` column as its JSON value. The row is checked
        before it is logged, so a malformed one raises and writes
        nothing."""
        spec = _membership_spec(kind)
        payload = {spec.kind: dict(row)}
        _membership_values(spec, payload[spec.kind])
        with self._transaction() as tx:
            return self._mutate(tx, f"{spec.kind}_put", payload, session=session)

    def delete_membership(
        self, kind: str, key: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """Delete the membership row `key` names (its key columns)."""
        spec = _membership_spec(kind)
        payload = {column: key[column] for column in spec.key}
        _membership_key(spec, payload)
        with self._transaction() as tx:
            return self._mutate(tx, f"{spec.kind}_delete", payload, session=session)

    def record_membership(
        self, kind: str, payload: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """One membership row of design 8.10 (`MEMBERSHIP_EVENT_KINDS`),
        written whether or not telemetry is on. Inside a `batch` it commits
        with the batch."""
        if kind not in MEMBERSHIP_EVENT_KINDS:
            raise ValueError(f"{kind!r} is not a membership row kind")
        with self._transaction() as tx:
            return self._append(tx, kind, dict(payload), session=session)

    def membership_rows(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        column_in: tuple[str, Collection[Any]] | None = None,
        order_by: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Rows of a membership table as dicts, a ``_json`` column parsed
        back to its value: those whose columns equal `where` (None matches
        null) and whose `column_in` column holds one of its values, ordered
        by `order_by` (a leading ``-`` sorts that column descending), else
        by the key. Column names are checked against the table."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None:
            raise ValueError(f"{table!r} is not a membership table")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (where or {}).items():
            _membership_column(spec, column)
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        order: list[str] = []
        for name in order_by or spec.key:
            column = name.lstrip("-")
            _membership_column(spec, column)
            order.append(f"{column} DESC" if name.startswith("-") else column)
        select = f"SELECT {', '.join(spec.columns)} FROM {table}"
        batches: list[list[Any]] = [[]]
        if column_in is not None:
            column, values = column_in
            _membership_column(spec, column)
            wanted = list(dict.fromkeys(values))
            if not wanted:
                return []
            batches = [
                wanted[start : start + _PROVENANCE_BATCH]
                for start in range(0, len(wanted), _PROVENANCE_BATCH)
            ]
        out: list[dict[str, Any]] = []
        for batch in batches:
            batch_clauses = list(clauses)
            if column_in is not None:
                batch_clauses.append(
                    f"{column_in[0]} IN ({','.join('?' * len(batch))})"
                )
            sql = select
            if batch_clauses:
                sql += " WHERE " + " AND ".join(batch_clauses)
            sql += " ORDER BY " + ", ".join(order)
            for raw in self._conn.execute(sql, (*params, *batch)):
                out.append(
                    {
                        column: (
                            _load_json(raw[column], None)
                            if column.endswith("_json")
                            else raw[column]
                        )
                        for column in spec.columns
                    }
                )
        if len(batches) > 1:
            _sort_rows(out, list(order_by or spec.key))
        return out

    def next_membership_id(self, table: str, prefix: str) -> str:
        """The next local id of a membership table: `prefix` and one more
        than the highest number its ids carry, at least three digits
        (``P001``). Read inside the caller's transaction, so two writers
        never mint one id."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None or spec.key != ("id",):
            raise ValueError(f"{table!r} has no minted id")
        row = self._conn.execute(
            f"SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) FROM {table} WHERE id LIKE ?",
            (len(prefix) + 1, f"{prefix}%"),
        ).fetchone()
        number = int(row[0] or 0) + 1
        return f"{prefix}{number:03d}"

    def next_membership_number(self, table: str, column: str) -> int:
        """One more than the highest value of an integer column of a
        membership table (a witness's arrival, a view's sighting order)."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None:
            raise ValueError(f"{table!r} is not a membership table")
        _membership_column(spec, column)
        row = self._conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        return int(row[0] or 0) + 1

    def membership_log(
        self, kinds: Collection[str] | None = None, *, since_seq: int = 0
    ) -> Iterator[LogRow]:
        """The membership rows after `since_seq`, in log order: the
        mutation rows of the membership tables and the rows of design
        8.10, or only those of `kinds`, read through the log's kind
        index."""
        allowed = MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS
        names = sorted(allowed if kinds is None else set(kinds) & allowed)
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
            (*names, int(since_seq)),
        ):
            yield LogRow(
                int(raw[0]),
                str(raw[1]),
                None if raw[2] is None else str(raw[2]),
                str(raw[3]),
                str(raw[4]),
                str(raw[5]),
                str(raw[6]),
            )

    def wrote_record(self, record_id: str) -> bool:
        """Whether this store's own log ever put the record `record_id`: a
        memory, tombstone or episode row naming it, or its home."""
        if (
            self._conn.execute(
                "SELECT 1 FROM homes WHERE record_id = ?", (record_id,)
            ).fetchone()
            is not None
        ):
            return True
        needle = canonical_payload({"id": record_id})[1:-1]
        return (
            self._conn.execute(
                "SELECT 1 FROM log WHERE kind IN "
                "('memory_put', 'tombstone_put', 'episode_put') "
                "AND instr(payload, ?) > 0 LIMIT 1",
                (needle,),
            ).fetchone()
            is not None
        )

    def reissue_membership_store_id(
        self, *, record_id: str, source: str, session: str | None = None
    ) -> tuple[str, str]:
        """A new store id after a fork (design 8.7): the meta value
        replaced and a ``store_forked`` row naming the old and new ids,
        the record that showed the fork and where it came from, in one
        transaction. Returns ``(old, new)``."""
        old = self.membership_store_id
        new = secrets.token_hex(16)
        with self._transaction() as tx:
            tx.execute(
                "UPDATE meta SET value = ? WHERE key = ?", (new, MEMBERSHIP_STORE_ID)
            )
            self._append(
                tx,
                "store_forked",
                {
                    "old_store": old,
                    "new_store": new,
                    "record": record_id,
                    "source": source,
                },
                session=session,
            )
        return old, new

    # -- telemetry ----------------------------------------------------------

    def record_event(
        self,
        kind: str,
        *,
        session: str | None = None,
        actor: Actor | Mapping[str, Any] | None = None,
        best_effort: bool = True,
        **fields: Any,
    ) -> LogRow | None:
        """One telemetry row. ``query`` and ``probe_query`` are redacted to
        a hash, a short preview and a length before the row is signed, as
        the v8 recorder redacted them. With ``best_effort`` a storage
        failure is logged and swallowed, so telemetry never fails the tool
        call it describes; a kind that names a mutation or a control row
        is a programming error and always raises."""
        if kind in _NOT_TELEMETRY:
            raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
        payload: dict[str, Any] = _redact_event_fields(dict(fields))
        if actor is not None:
            record = actor.to_record() if isinstance(actor, Actor) else dict(actor)
            if record:
                payload.setdefault("actor", record)
        try:
            with self._transaction() as tx:
                return self._append(tx, kind, payload, session=session)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            if not best_effort:
                raise
            log.warning("event %s not recorded: %s", kind, exc)
            return None

    def import_event(
        self, event: Mapping[str, Any], *, imported_from: str = IMPORTED_FROM_V8
    ) -> LogRow:
        """One v8 event as a telemetry row under its original ``ts`` and
        ``session``, the rest of its fields as the payload plus
        ``imported_from``, query fields redacted (``event_import_payload``
        says exactly what). An event without a ``ts`` is stamped now.
        Raises rather than swallowing: a migration wants to know."""
        ts, session, kind, payload = event_import_payload(
            event, imported_from=imported_from
        )
        with self._transaction() as tx:
            return self._append(tx, kind, payload, session=session, ts=ts)

    def imported_event_keys(
        self, imported_from: str = IMPORTED_FROM_V8
    ) -> set[tuple[str, str | None, str, str]]:
        """``(ts, session, kind, payload text)`` of every telemetry row
        that names ``imported_from``: what a re-run of the importer skips.
        The payload text is the canonical one the row stores, so the
        caller compares ``canonical_payload`` of what it would write."""
        needle = canonical_payload({"imported_from": imported_from})[1:-1]
        out: set[tuple[str, str | None, str, str]] = set()
        for row in self._conn.execute(
            "SELECT ts, session, kind, payload FROM log WHERE payload LIKE ? "
            "ORDER BY seq",
            (f"%{needle}%",),
        ):
            kind = str(row["kind"])
            if kind in _NOT_TELEMETRY:
                continue
            out.add(
                (
                    str(row["ts"]),
                    None if row["session"] is None else str(row["session"]),
                    kind,
                    str(row["payload"]),
                )
            )
        return out

    def iter_events(self, since_seq: int = 0) -> Iterator[dict[str, Any]]:
        """The telemetry rows in the v8 event shape, seq order: ``ts``,
        ``session`` (when the row has one), ``kind``, then the payload's
        fields. Mutation and control rows are not events and are
        skipped; so is a row whose payload is not an object."""
        for row in iter_rows(self._conn, since_seq):
            if row.kind in _NOT_TELEMETRY:
                continue
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            event: dict[str, Any] = {"ts": row.ts}
            if row.session is not None:
                event["session"] = row.session
            event["kind"] = row.kind
            for key, value in payload.items():
                if key not in _ROW_COLUMNS:
                    event[key] = value
            yield event

    def record_migration(
        self,
        *,
        source: str,
        counts: Mapping[str, Any],
        session: str | None = None,
    ) -> LogRow:
        """The ``migrate_v8`` control row: which v8 directory was read and
        how much of it this run imports, written before the imports it
        counts so the log says what was attested when."""
        from . import __version__

        payload = {
            "imported_from": IMPORTED_FROM_V8,
            "source": source,
            "counts": dict(counts),
            "engine_version": __version__,
        }
        with self._transaction() as tx:
            return self._append(tx, MIGRATE_V8, payload, session=session)

    def record_capture(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None = None,
        ts: str | None = None,
        guard: Callable[[], None] | None = None,
    ) -> LogRow:
        """One capture control row (`log.CAPTURE_KINDS`), written whether or
        not telemetry is on: `record_event` refuses control kinds, and the
        event readers skip them, so capture has this writer and
        `capture_rows` as its reader. `guard` runs inside the transaction,
        under the write lock, before the row is appended; whatever it
        raises aborts the write, which is how opening a batch is atomic
        per session. Inside a `batch` the row commits with the batch."""
        if kind not in CAPTURE_KINDS:
            raise ValueError(f"{kind!r} is not a capture kind")
        with self._transaction() as tx:
            if guard is not None:
                guard()
            return self._append(tx, kind, dict(payload), session=session, ts=ts)

    def capture_rows(
        self, kinds: Collection[str], *, since_seq: int = 0
    ) -> Iterator[LogRow]:
        """The capture rows of `kinds` after `since_seq`, in log order,
        read through the log's kind index."""
        names = sorted(set(kinds) & CAPTURE_KINDS)
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
            (*names, int(since_seq)),
        ):
            yield LogRow(
                int(raw[0]),
                str(raw[1]),
                None if raw[2] is None else str(raw[2]),
                str(raw[3]),
                str(raw[4]),
                str(raw[5]),
                str(raw[6]),
            )

    # -- search seams ---------------------------------------------------------

    def query_candidates(
        self,
        text: str,
        *,
        scopes: Sequence[str] | None = None,
        client: str | None = None,
        model: str | None = None,
        max_results: int = 100,
    ) -> list[tuple[str, float]]:
        """``[(memory_id, bm25), ...]`` ascending, the v8 index query
        verbatim: the MATCH from ``fts_match_query``, scopes as an OR over
        the space-padded list, the actor filters as exact equality that an
        undeclared row never matches, and the cap applied in SQL."""
        if not text.strip():
            return []
        match_query = fts_match_query(text)
        if not match_query:
            return []
        sql = (
            "SELECT m.id, bm25(memories_fts) AS score "
            "FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? "
        )
        params: list[Any] = [match_query]
        if scopes:
            sql += "AND (" + " OR ".join(["m.scopes_text LIKE ?"] * len(scopes)) + ") "
            params.extend(f"% {s} %" for s in scopes)
        if client is not None:
            sql += "AND m.actor_client IS NOT NULL AND m.actor_client = ? "
            params.append(client)
        if model is not None:
            sql += "AND m.actor_model IS NOT NULL AND m.actor_model = ? "
            params.append(model)
        sql += "ORDER BY score ASC LIMIT ?"
        params.append(int(max_results))
        rows = self._conn.execute(sql, params).fetchall()
        return [(str(row["id"]), float(row["score"])) for row in rows]

    def links_for_many(
        self, memory_ids: Iterable[str]
    ) -> dict[
        str, tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]
    ]:
        """Outbound and inbound link rows per id, the v8 shape: outbound
        ``(type, target_id, note)``, inbound ``(type, source_id, note)``."""
        ids = list(dict.fromkeys(memory_ids))
        out: dict[
            str,
            tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]],
        ] = {mid: ([], []) for mid in ids}
        if not ids:
            return out
        placeholders = ",".join("?" * len(ids))
        for row in self._conn.execute(
            "SELECT source_id, type, target_id, note FROM memory_links "
            f"WHERE source_id IN ({placeholders}) ORDER BY source_id, type, target_id",
            ids,
        ):
            out[row["source_id"]][0].append(
                (row["type"], row["target_id"], row["note"])
            )
        for row in self._conn.execute(
            "SELECT target_id, type, source_id, note FROM memory_links "
            f"WHERE target_id IN ({placeholders}) ORDER BY target_id, type, source_id",
            ids,
        ):
            out[row["target_id"]][1].append(
                (row["type"], row["source_id"], row["note"])
            )
        return out

    def inbound_links(
        self, target_ids: Iterable[str], types: Collection[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        """``{target: [(type, source_id, source updated)]}`` for the links of
        `types` into each of `target_ids`, oldest source first (id breaking
        a tie). A link's source is always an active memory: the cleanup
        trigger drops a removed one's rows. What `in_force` walks."""
        ids = list(dict.fromkeys(target_ids))
        kinds = sorted(set(types))
        out: dict[str, list[tuple[str, str, str]]] = {}
        if not ids or not kinds:
            return out
        marks = ",".join("?" * len(kinds))
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT l.target_id AS target_id, l.type AS type, "
                "l.source_id AS source_id, m.updated AS updated "
                "FROM memory_links l JOIN memories m ON m.id = l.source_id "
                f"WHERE l.type IN ({marks}) AND l.target_id IN ({placeholders}) "
                "ORDER BY l.target_id, m.updated, l.source_id",
                (*kinds, *batch),
            ):
                out.setdefault(str(row["target_id"]), []).append(
                    (str(row["type"]), str(row["source_id"]), str(row["updated"]))
                )
        return out

    def provenance_for(self, memory_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the active records asked. The label is the
        row's, unless the row's pointer into the log fails: the ``log_mac``
        names no row, names a row that put a different record, names a row
        whose MAC does not verify under the segment's key, or names a row
        in a foreign segment (one whose key this machine does not hold,
        after one whose key it does, or at or after the origin anchor),
        or, with ``strict_provenance`` or an origin anchor the log no
        longer matches, in any segment whose key is not held, that this
        machine's own key did not sign either. Such a row reads
        ``unaccounted``, as does one the last ``refold`` in this process
        found so. A row in a leading segment whose key is not held (a
        store copied here without its key) keeps its label. A store
        opened without its key cannot verify pointers and returns the row
        labels as stored; ``trust_rows`` says when that is the case."""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT id, provenance, log_mac FROM memories "
                f"WHERE id IN ({placeholders})",
                batch,
            ):
                memory_id = str(row["id"])
                label = str(row["provenance"])
                if memory_id in self._unaccounted_memory_ids:
                    label = UNACCOUNTED
                elif (
                    self._pointer_verified(memory_id, "memory_put", row["log_mac"])
                    is False
                ):
                    label = UNACCOUNTED
                out[memory_id] = label
        return out

    def _segment_for(self, seq: int) -> Segment | None:
        """The segment that holds the row at ``seq``, with its key when
        this machine holds it and whether it is foreign. The map is read
        off the rekey rows once per connection (`log.chain_segments`), so a
        row costs this walk over a handful of segments and nothing more.
        A connection that was open before a rekey row was appended keeps
        its map, and verifies a later row under the last segment it knows,
        which a row signed by another key fails. The origin anchor is
        checked with it (`log.chain_trust`): one more row and one HMAC."""
        if self._segments is None:
            trust = chain_trust(
                self._conn,
                keyring=self._keyring,
                current_fingerprint=self.key_fingerprint,
                current_key=self._verify_key,
            )
            self._segments, self._origin = trust.segments, trust.origin
        found: Segment | None = None
        for segment in self._segments:
            if found is not None and seq < segment.first_seq:
                break
            found = segment
        return found

    def _origin_broken(self) -> bool:
        """Does the origin anchor this connection's segment map was read
        under no longer match the log (`log.OriginCheck.broken`)? False
        before the map is read, and for a store with no anchor."""
        return self._origin is not None and self._origin.broken

    def _pointer_verified(self, record_id: str, kind: str, log_mac: Any) -> bool | None:
        """Does the record's ``log_mac`` name a log row of ``kind`` that
        put this very record, and does that row's MAC verify under its
        segment's key? A row in a foreign segment is checked against this
        connection's own key instead, since nobody here holds the
        segment's: it verifies only when this machine appended it, and
        anything else is False. A row in a leading segment whose key is
        not held answers None (its label stands), or is checked the same
        way under ``strict_provenance`` or when the origin anchor no
        longer matches the log, since which segments lead is then not
        known. A row below seq 1 is False whatever its segment. None too
        when the store was opened without its key. The True and False
        answers are cached per (id, mac)."""
        own_key = self._verify_key
        if own_key is None:
            return None
        if not isinstance(log_mac, str) or not log_mac:
            return False
        cache_key = (record_id, log_mac)
        cached = self._pointer_cache.get(cache_key)
        if cached is not None:
            return cached
        raw = self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE mac = ?",
            (log_mac,),
        ).fetchone()
        verdict = False
        if raw is not None and str(raw["kind"]) == kind:
            try:
                payload = json.loads(raw["payload"])
                record = payload[kind.split("_", 1)[0]]
                named = str(record["id"])
            except (ValueError, KeyError, TypeError):
                named = ""
            # No store writes a row below seq 1 (`log.append_row`), and no
            # segment holds one (`log.chain_trust`): a record that names
            # one was put by an editor of the file, whatever its label.
            if named == record_id and int(raw["seq"]) > 0:
                segment = self._segment_for(int(raw["seq"]))
                if segment is None:
                    return None
                key = segment.key
                if key is None:
                    if (
                        not segment.foreign
                        and not self._origin_broken()
                        and not self.strict_provenance
                    ):
                        # A leading segment: signed elsewhere, before this
                        # machine first signed the store. Its label stands.
                        return None
                    # No key here signed the segment, but this machine
                    # may have appended rows to it: a daemon that had the
                    # store open while a forged rekey row went in, or an
                    # opener whose repair could not be written, writes
                    # under its own key into the segment that row opened.
                    # Those rows verify under that key; a forged one
                    # cannot, so it still reads unaccounted.
                    key = own_key
                expected = compute_mac(
                    key,
                    seq=int(raw["seq"]),
                    ts=str(raw["ts"]),
                    session=None if raw["session"] is None else str(raw["session"]),
                    kind=str(raw["kind"]),
                    payload=str(raw["payload"]),
                    prev_mac=str(raw["prev_mac"]),
                )
                verdict = same_mac(expected, log_mac)
        self._pointer_cache[cache_key] = verdict
        return verdict

    def trust_rows(self, memory_ids: Sequence[str]) -> dict[str, TrustRow] | None:
        """``{id: TrustRow}`` for the active records asked: the provenance
        verdict ``provenance_for`` gives and this host's verification stamp
        (the ``verifications`` row with no machine id), or None when the
        store was opened without its key, so no pointer can be verified and
        the response must say trust is unavailable."""
        if self._verify_key is None:
            return None
        labels = self.provenance_for(memory_ids)
        if not labels:
            return {}
        stamps: dict[str, str] = {}
        ids = list(labels)
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT memory_id, verified_at FROM verifications "
                f"WHERE machine_id IS NULL AND memory_id IN ({placeholders})",
                batch,
            ):
                stamps[str(row["memory_id"])] = str(row["verified_at"])
        return {
            memory_id: TrustRow(label, stamps.get(memory_id))
            for memory_id, label in labels.items()
        }

    def unaccounted_ids(self) -> list[str]:
        """Every active record whose pointer into the log fails, newest
        first: the planted shape, found by one pass over the rows rather
        than by a refold."""
        out: list[tuple[str, str]] = []
        for row in self._conn.execute(
            "SELECT id, created, log_mac FROM memories ORDER BY created DESC, id"
        ):
            memory_id = str(row["id"])
            if (
                memory_id in self._unaccounted_memory_ids
                or self._pointer_verified(memory_id, "memory_put", row["log_mac"])
                is False
            ):
                out.append((str(row["created"]), memory_id))
        return [memory_id for _, memory_id in out]

    def provenance_counts(self) -> dict[str, int]:
        """How many active records carry each label, with ``unaccounted``
        counted by the pointer check rather than by any stored label."""
        counts: dict[str, int] = {}
        unaccounted = set(self.unaccounted_ids())
        for row in self._conn.execute("SELECT id, provenance FROM memories"):
            label = (
                UNACCOUNTED if str(row["id"]) in unaccounted else str(row["provenance"])
            )
            counts[label] = counts.get(label, 0) + 1
        return counts

    # -- the record surface the runtime uses -----------------------------------
    #
    # The v8 store's names, kept where the semantics are unchanged, so the
    # handlers, the CLI and the benches port without a rename of their own.

    def write(
        self,
        *,
        content: str,
        scopes: list[str],
        confidence: Confidence = Confidence.MEDIUM,
        source: Source = Source.EXPLICIT,
        origin: Origin | None = None,
        category: Category | None = None,
        claims: list[str] | None = None,
        links: list[Any] | None = None,
        actor: Actor | dict[str, Any] | None = None,
        session: str | None = None,
        home: RecordHome | None = None,
        standing: StandingAnswer | None = None,
    ) -> Memory:
        """Create a new memory: mints the id, stamps created and updated,
        stores the body stripped with one trailing newline. `claims` are
        stored verbatim; parsing and the declare-time check are the write
        handler's job, since they need the origin worktree. `home` and
        `standing` are written with the record (`put_memory`)."""
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
            actor=Actor.model_validate(actor) if isinstance(actor, dict) else actor,
            category=category,
            claims=list(claims) if claims else [],
            links=[
                entry
                if isinstance(entry, MemoryLink)
                else MemoryLink.model_validate(entry)
                for entry in (links or [])
            ],
        )
        return self.put_memory(
            memory, provenance=LOCAL, session=session, home=home, standing=standing
        )

    def update(
        self,
        memory: Memory,
        *,
        force: bool = False,
        preserve_verification: bool = False,
        session: str | None = None,
        standing: StandingAnswer | None = None,
    ) -> Memory:
        """Replace an active record with the caller's edit. The caller's
        `memory.updated` is the snapshot it read; unless `force`, a stored
        `updated` that differs raises ConcurrentUpdateError so the caller
        re-reads and retries. `preserve_verification` keeps the stored
        verification fields over the snapshot's (a metadata edit must not
        undo a verify that landed in between); a content edit leaves it
        False and resets them on purpose. The corroboration rollup is
        store-owned and always carried over. ``standing`` writes the
        edited body's standing answer in the same transaction."""
        current = self._active_or_raise(memory.id)
        if not force and current.updated != memory.updated:
            raise ConcurrentUpdateError(memory.id, current.updated)
        carried: dict[str, object] = {
            "updated": utcnow(),
            "corroborations": current.corroborations,
            "last_corroborated": current.last_corroborated,
        }
        if preserve_verification:
            carried.update(
                {
                    "last_verified_at": current.last_verified_at,
                    "verified_paths": list(current.verified_paths),
                    "verified_commits": list(current.verified_commits),
                    "verified_versions": list(current.verified_versions),
                    "verified_absent_paths": list(current.verified_absent_paths),
                    "claims": list(current.claims),
                    "verified_head": current.verified_head,
                }
            )
        new_memory = memory.model_copy(update=carried)
        Memory.model_validate(new_memory.model_dump())
        return self.put_memory(new_memory, session=session, standing=standing)

    def mark_verified(
        self,
        memory_id: str,
        *,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
        verified_absent_paths: list[str] | None = None,
        claims: list[str] | None = None,
        verified_head: str | None = None,
        expected_last_verified_at: datetime | None = None,
        expected_updated: datetime | None = None,
        check_expected: bool = False,
        session: str | None = None,
    ) -> Memory:
        """Stamp `last_verified_at` now and replace whichever attestation
        lists the caller passed (None leaves a list as it is; `verified_head`
        is always written). Writes the record and this host's verification
        row in one transaction. With `check_expected`, a stored stamp or
        `updated` that differs from the caller's snapshot raises
        ConcurrentUpdateError."""
        if verified_head is not None:
            verified_head = verified_head.strip().lower()
            if not is_full_commit_sha(verified_head):
                raise ValueError(
                    "verified_head must be a full commit hash (40 or 64 hex "
                    f"characters), got {verified_head!r}"
                )
        existing = self._active_or_raise(memory_id)
        if check_expected and (
            existing.last_verified_at != expected_last_verified_at
            or (expected_updated is not None and existing.updated != expected_updated)
        ):
            raise ConcurrentUpdateError(memory_id, existing.updated)
        verified_now = utcnow()
        update: dict[str, object] = {"last_verified_at": verified_now}
        if verified_paths is not None:
            update["verified_paths"] = list(verified_paths)
        if verified_commits is not None:
            update["verified_commits"] = list(verified_commits)
        if verified_versions is not None:
            update["verified_versions"] = list(verified_versions)
        if verified_absent_paths is not None:
            update["verified_absent_paths"] = list(verified_absent_paths)
        if claims is not None:
            update["claims"] = list(claims)
        update["verified_head"] = verified_head
        new_memory = existing.model_copy(update=update)
        Memory.model_validate(new_memory.model_dump())
        row = self._conn.execute(
            "SELECT provenance, filename FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        payload = {
            "memory": new_memory.model_dump(mode="json"),
            "provenance": str(row["provenance"]),
            "filename": row["filename"],
        }
        stamp = {
            "memory_id": memory_id,
            "machine_id": None,
            "verified_at": _iso(verified_now),
            "verified_head": verified_head,
            "verified_paths": list(new_memory.verified_paths),
            "verified_commits": list(new_memory.verified_commits),
            "verified_versions": list(new_memory.verified_versions),
            "verified_absent_paths": list(new_memory.verified_absent_paths),
        }
        with self._transaction() as tx:
            self._mutate(tx, "memory_put", payload, session=session)
            self._mutate(tx, "verification_put", stamp, session=session)
        return new_memory

    def record_corroboration(
        self, memory_id: str, *, session: str | None = None
    ) -> Memory:
        """Bump the corroboration rollup without touching `updated`: a
        dedup-rejected write is the stored claim recurring, evidence it
        still holds, not a rewrite."""
        existing = self._active_or_raise(memory_id)
        bumped = existing.model_copy(
            update={
                "corroborations": existing.corroborations + 1,
                "last_corroborated": utcnow(),
            }
        )
        return self.put_memory(bumped, session=session)

    def _active_or_raise(self, memory_id: str) -> Memory:
        """The active record, or the v8 errors: TombstonedError for a
        removed id, MemoryNotFoundError otherwise."""
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is not None:
            return _row_to_memory(row)
        dead = self._conn.execute(
            "SELECT removed_reason FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if dead is not None:
            raise TombstonedError(
                f"memory {memory_id} was removed: {dead['removed_reason']}"
            )
        raise MemoryNotFoundError(f"no memory with id {memory_id}")

    def load_one(self, memory_id: str) -> Memory:
        """One active memory by id; raises if missing or tombstoned."""
        return self._active_or_raise(memory_id)

    def show(self, memory_id: str) -> Memory:
        return self._active_or_raise(memory_id)

    def load_many(self, memory_ids: list[str]) -> list[Memory]:
        """The active records among `memory_ids`, in the order asked;
        unknown and tombstoned ids are skipped."""
        return self.get_many(memory_ids)

    def load_all(self) -> list[Memory]:
        """Every active memory in row order, which is insertion order."""
        return list(self.iter_memories())

    def iter_active(self) -> Iterator[Memory]:
        return self.iter_memories()

    def list_summaries(self, scopes: list[str] | None = None) -> list[MemorySummary]:
        """Like `load_all` but body-stripped, filtered to memories carrying
        at least one of `scopes` when given."""
        out: list[MemorySummary] = []
        wanted = set(scopes) if scopes else None
        for memory in self.iter_memories():
            if wanted is not None and not (set(memory.scopes) & wanted):
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
                    actor=memory.actor,
                )
            )
        return out

    def load_tombstones(self) -> list[TombstonedMemory]:
        """Every tombstone, most recently removed first."""
        return [
            _row_to_tombstone(row)
            for row in self._conn.execute(
                "SELECT * FROM tombstones ORDER BY removed DESC, rowid DESC"
            )
        ]

    def list_tombstones(
        self, scopes: list[str] | None = None
    ) -> list[TombstonedSummary]:
        out: list[TombstonedSummary] = []
        wanted = set(scopes) if scopes else None
        for dead in self.load_tombstones():
            if wanted is not None and not (set(dead.scopes) & wanted):
                continue
            out.append(
                TombstonedSummary(
                    id=dead.id,
                    scopes=dead.scopes,
                    confidence=dead.confidence,
                    summary=first_summary_line(dead.body),
                    created=dead.created,
                    updated=dead.updated,
                    last_verified_at=dead.last_verified_at,
                    category=dead.category,
                    removed=dead.removed,
                    removed_reason=dead.removed_reason,
                    removed_session=dead.removed_session,
                )
            )
        return out

    def load_tombstone(self, memory_id: str) -> TombstonedMemory:
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"no tombstone with id {memory_id}")
        return _row_to_tombstone(row)

    def restore_trimmed(
        self,
        memory_id: str,
        *,
        drop_claims: Iterable[str] = (),
        drop_verified_paths: Iterable[str] = (),
        clear_verification: bool = False,
        drop_verified_head: bool = False,
        session: str | None = None,
    ) -> Memory:
        """`restore`, minus the trust fields the caller found no longer
        hold on this machine (the restore handler's trust strip). Raises
        NotTombstonedError for an active id."""
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
        if self.has_memory(memory_id):
            raise NotTombstonedError(
                f"memory {memory_id} is active; nothing to restore"
            )
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"no tombstone with id {memory_id}")
        dead = _row_to_tombstone(row)
        fields: dict[str, Any] = {
            name: getattr(dead, name)
            for name in Memory.model_fields
            if name not in ("links", "corroborations", "last_corroborated")
        }
        gone_claims = set(drop_claims)
        if gone_claims:
            fields["claims"] = [c for c in dead.claims if c not in gone_claims]
        gone_paths = set(drop_verified_paths)
        if gone_paths:
            fields["verified_paths"] = [
                v for v in dead.verified_paths if v not in gone_paths
            ]
        if clear_verification:
            fields["last_verified_at"] = None
        if drop_verified_head:
            fields["verified_head"] = None
        memory = Memory(
            **fields,
            links=[
                MemoryLink.model_validate(entry)
                for entry in json.loads(row["links_json"])
            ],
            corroborations=int(row["corroborations"]),
            last_corroborated=_dt_opt(row["last_corroborated"]),
        )
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": LOCAL,
            "filename": row["filename"],
        }
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "memory_put", payload, session=session)
        return memory

    def rename_scope(
        self,
        old: str,
        new: str,
        *,
        include_tombstones: bool = True,
        session: str | None = None,
    ) -> dict[str, list[Any]]:
        """Replace scope `old` with `new` on every record carrying it, the
        list deduplicated in place; active records get a fresh `updated`.
        Returns the ids changed under `active` and `tombstoned`; `failed`
        is present only when a record could not be rewritten."""
        result: dict[str, list[Any]] = {"active": [], "tombstoned": []}
        if old == new:
            return result
        failed: list[dict[str, str]] = []
        with self.batch():
            for memory in self.load_all():
                renamed = _scopes_after_rename(memory.scopes, old, new)
                if renamed is None:
                    continue
                refreshed = memory.model_copy(
                    update={"scopes": renamed, "updated": utcnow()}
                )
                try:
                    self.put_memory(refreshed, session=session)
                except (ValueError, ValidationError) as exc:
                    failed.append({"id": memory.id, "reason": str(exc)})
                    continue
                result["active"].append(memory.id)
            if include_tombstones:
                for tomb_row in list(self.iter_tombstone_rows()):
                    dead = tomb_row.tombstone
                    renamed = _scopes_after_rename(dead.scopes, old, new)
                    if renamed is None:
                        continue
                    payload = {
                        "tombstone": dead.model_copy(
                            update={"scopes": renamed}
                        ).model_dump(mode="json"),
                        "provenance": tomb_row.provenance,
                        "filename": tomb_row.filename,
                        "links": tomb_row.links,
                        "corroborations": tomb_row.corroborations,
                        "last_corroborated": _iso_opt(tomb_row.last_corroborated),
                    }
                    with self._transaction() as tx:
                        self._mutate(tx, "tombstone_put", payload, session=session)
                    result["tombstoned"].append(dead.id)
        if failed:
            result["failed"] = failed
        return result

    def prune_tombstones(
        self,
        older_than: timedelta,
        *,
        now: datetime | None = None,
        session: str | None = None,
    ) -> list[str]:
        """Delete tombstones removed longer than `older_than` ago. Returns
        the ids pruned, oldest removal first."""
        cutoff = (now or utcnow()) - older_than
        doomed = [
            (dead.removed, dead.id)
            for dead in self.iter_tombstones()
            if dead.removed < cutoff
        ]
        doomed.sort()
        with self.batch():
            for _, memory_id in doomed:
                with self._transaction() as tx:
                    self._mutate(
                        tx, "tombstone_delete", {"id": memory_id}, session=session
                    )
        return [memory_id for _, memory_id in doomed]

    # -- the reads the index used to serve ----------------------------------------

    def document_frequencies(
        self,
        terms: Sequence[str],
        *,
        admit: Callable[[list[str], str | None, Actor | None], bool],
        memoised: bool = False,
    ) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """Document frequencies for `terms` over the admitted collection,
        the v8 index's `corpus_document_frequencies`: `(size, body_df,
        scope_df)`, where `admit` is the search's own admission predicate
        over `(scopes, home project, actor)` (the project the record's
        ``homes`` row names, read in the same query; None for a record
        with none) so the denominator is the ranked set. None when no term
        or no admitted document. With `memoised`,
        the rows' admission inputs and the terms' hits come from the
        store's document frequency memo (`_DOC_FREQS`), read again only
        after a memory changed; the answer is the same."""
        unique = sorted({t for t in terms if t})
        if not unique:
            return None
        if memoised and not (self._in_batch or self._conn.in_transaction):
            return self._memoised_document_frequencies(unique, admit)
        admitted: set[str] = set()
        for row in self._conn.execute(_ADMISSION_INPUTS):
            client = row["actor_client"]
            model = row["actor_model"]
            actor = (
                Actor(client=client, model=model)
                if (client is not None or model is not None)
                else None
            )
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if admit(list(scopes), row["home_project"], actor):
                admitted.add(str(row["id"]))
        if not admitted:
            return None

        def ids_matching(match_expr: str) -> set[str]:
            return {
                str(r["id"])
                for r in self._conn.execute(
                    "SELECT m.id AS id FROM memories m "
                    "JOIN memories_fts f ON f.rowid = m.rowid "
                    "WHERE memories_fts MATCH ?",
                    (match_expr,),
                )
            }

        body_df: dict[str, int] = {}
        scope_df: dict[str, int] = {}
        for term in unique:
            quoted = '"' + term.replace('"', '""') + '"'
            body_hits = ids_matching(f"body_fts : {quoted}") & admitted
            any_hits = ids_matching(quoted) & admitted
            if body_hits:
                body_df[term] = len(body_hits)
            if any_hits:
                scope_df[term] = len(any_hits)
        return len(admitted), body_df, scope_df

    def _memory_head(self) -> tuple[int, str]:
        """The log's last memory or home row, `(seq, mac)`: the key of the
        document frequency memo. `(0, "")` on a log with none. Each MAX is
        one probe of `log_by_kind`."""
        row = self._conn.execute(
            "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM ("
            "SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete'))"
        ).fetchone()
        if row is None:
            return 0, ""
        return int(row["seq"]), str(row["mac"])

    def _read_doc_freq_inputs(self, head: tuple[int, str]) -> _DocFreqInputs:
        """Every memory row's admission inputs, as the uncached
        `document_frequencies` reads them, grouped by equal inputs."""
        index: dict[str, int] = {}
        grouped: dict[
            tuple[Any, ...], tuple[list[str], str | None, Actor | None, list[int]]
        ] = {}
        for row in self._conn.execute(_ADMISSION_INPUTS):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            position = len(index)
            index[str(row["id"])] = position
            home_project = row["home_project"]
            client = row["actor_client"]
            model = row["actor_model"]
            group_key = (row["scopes_json"], home_project, client, model)
            group = grouped.get(group_key)
            if group is None:
                actor = (
                    Actor(client=client, model=model)
                    if (client is not None or model is not None)
                    else None
                )
                group = (list(scopes), home_project, actor, [])
                grouped[group_key] = group
            group[3].append(position)
        return _DocFreqInputs(
            head=head,
            groups=tuple(
                (scopes, home_project, actor, _row_mask(positions, len(index)))
                for scopes, home_project, actor, positions in grouped.values()
            ),
            index=index,
            terms=OrderedDict(),
        )

    def _term_masks(self, term: str, inputs: _DocFreqInputs) -> tuple[int, int]:
        """The rows of `inputs` whose body matches `term`, and whose body
        or scopes do, by the uncached call's two FTS queries."""

        def rows_matching(match_expr: str) -> int:
            positions = []
            for r in self._conn.execute(
                "SELECT m.id AS id FROM memories m "
                "JOIN memories_fts f ON f.rowid = m.rowid "
                "WHERE memories_fts MATCH ?",
                (match_expr,),
            ):
                position = inputs.index.get(str(r["id"]))
                if position is not None:
                    positions.append(position)
            return _row_mask(positions, len(inputs.index))

        quoted = '"' + term.replace('"', '""') + '"'
        return rows_matching(f"body_fts : {quoted}"), rows_matching(quoted)

    def _memoised_document_frequencies(
        self,
        unique: list[str],
        admit: Callable[[list[str], str | None, Actor | None], bool],
    ) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """`document_frequencies` through the document frequency memo."""
        key = (str(self._path), self._store_id)
        head = self._memory_head()
        known: dict[str, tuple[int, int]] = {}
        with _DOC_FREQS_LOCK:
            entry = _DOC_FREQS.get(key)
            if entry is not None and entry.head != head:
                entry = None
            if entry is not None:
                _DOC_FREQS.move_to_end(key)
                for term in unique:
                    masks = entry.terms.get(term)
                    if masks is not None:
                        entry.terms.move_to_end(term)
                        known[term] = masks
        fresh = entry is None
        if entry is None:
            entry = self._read_doc_freq_inputs(head)
        # The predicate runs on every call, once per group: it is not a
        # function of the store alone.
        admitted = 0
        for scopes, home_project, actor, mask in entry.groups:
            if admit(list(scopes), home_project, actor):
                admitted |= mask
        found: dict[str, tuple[int, int]] = {}
        if admitted:
            found = {t: self._term_masks(t, entry) for t in unique if t not in known}
        if (fresh or found) and self._memory_head() == head:
            self._keep_doc_freqs(key, entry, found, fresh=fresh)
        if not admitted:
            return None
        body_df: dict[str, int] = {}
        scope_df: dict[str, int] = {}
        for term in unique:
            body_mask, any_mask = known.get(term) or found[term]
            body_count = (body_mask & admitted).bit_count()
            any_count = (any_mask & admitted).bit_count()
            if body_count:
                body_df[term] = body_count
            if any_count:
                scope_df[term] = any_count
        return admitted.bit_count(), body_df, scope_df

    @staticmethod
    def _keep_doc_freqs(
        key: tuple[str, str],
        entry: _DocFreqInputs,
        found: dict[str, tuple[int, int]],
        *,
        fresh: bool,
    ) -> None:
        """Store `entry`, read at the store's current memory head, or add
        `found` to the entry already kept at that head."""
        with _DOC_FREQS_LOCK:
            current = _DOC_FREQS.get(key)
            if current is not entry:
                # A kept entry was replaced or dropped since this call read
                # it, or another connection kept one at this head first:
                # its masks index its own scan, so `found` is not merged.
                if not fresh or (current is not None and current.head == entry.head):
                    return
                _DOC_FREQS[key] = entry
            entry.terms.update(found)
            for term in found:
                entry.terms.move_to_end(term)
            while len(entry.terms) > DOC_FREQ_TERMS:
                entry.terms.popitem(last=False)
            _DOC_FREQS.move_to_end(key)
            while len(_DOC_FREQS) > DOC_FREQ_STORES:
                _DOC_FREQS.popitem(last=False)

    def scope_counts(
        self, *, admit: Callable[[list[str], str | None], bool]
    ) -> tuple[int, dict[str, int]]:
        """How many admitted memories there are and how many carry each
        scope, without reading a body. `admit` reads the scopes and the
        project the record's ``homes`` row names (None for none), joined
        in the one query."""
        total = 0
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT m.scopes_json AS scopes_json, h.project AS home_project "
            "FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
        ):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if not admit(list(scopes), row["home_project"]):
                continue
            total += 1
            for scope in scopes:
                counts[scope] = counts.get(scope, 0) + 1
        return total, counts

    def category_rows(
        self,
        *,
        category: str,
        admit: Callable[[list[str], str | None], bool],
    ) -> list[str]:
        """The ids of admitted memories in `category`, in id order; `admit`
        reads the scopes and the home project as `scope_counts` does."""
        out: list[str] = []
        for row in self._conn.execute(
            "SELECT m.id AS id, m.scopes_json AS scopes_json, "
            "h.project AS home_project "
            "FROM memories m LEFT JOIN homes h ON h.record_id = m.id "
            "WHERE m.category = ? ORDER BY m.id",
            (category,),
        ):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if admit(list(scopes), row["home_project"]):
                out.append(str(row["id"]))
        return out

    def declared_scopes(self, record_ids: Sequence[str]) -> dict[str, list[str]]:
        """``{id: scopes}`` for the memories and episodes asked, as written;
        an id that is neither (a removed memory) is absent. What
        `membership.Membership.homes` reads a global record's named
        projects from."""
        wanted = list(dict.fromkeys(record_ids))
        out: dict[str, list[str]] = {}
        for table in ("memories", "episodes"):
            for start in range(0, len(wanted), _PROVENANCE_BATCH):
                batch = wanted[start : start + _PROVENANCE_BATCH]
                placeholders = ",".join("?" * len(batch))
                for row in self._conn.execute(
                    f"SELECT id, scopes_json FROM {table} WHERE id IN ({placeholders})",
                    batch,
                ):
                    scopes = _load_json(row["scopes_json"], None)
                    if isinstance(scopes, list):
                        out[str(row["id"])] = [str(scope) for scope in scopes]
        return out

    def brief_rows(
        self,
        *,
        admit: Callable[[list[str], str | None], bool],
        provenance: Collection[str],
        exclude_category: str | None = None,
    ) -> list[str]:
        """The ids of memories newest ``updated`` first (id breaking a tie):
        stored label in `provenance`, category other than
        `exclude_category` (any category when None), home admitted
        by `admit`, which reads the record's scopes and the project its
        ``homes`` row names (None for a record with none) as `scope_counts`
        does (a global record's admission reads the projects its scopes
        name), joined in the one query. Never reads a body. The stored label is a pre-filter
        only: the caller re-reads the verified one (`trust_rows`), which a
        failed pointer turns into ``unaccounted``."""
        labels = sorted(set(provenance))
        if not labels:
            return []
        placeholders = ",".join("?" * len(labels))
        category = (
            ""
            if exclude_category is None
            else "AND (m.category IS NULL OR m.category != ?) "
        )
        params: tuple[str, ...] = (
            tuple(labels) if exclude_category is None else (*labels, exclude_category)
        )
        out: list[str] = []
        for row in self._conn.execute(
            "SELECT m.id AS id, m.scopes_json AS scopes_json, "
            "h.project AS home_project FROM memories m "
            "LEFT JOIN homes h ON h.record_id = m.id "
            f"WHERE m.provenance IN ({placeholders}) "
            f"{category}"
            "ORDER BY m.updated DESC, m.id DESC",
            params,
        ):
            scopes = _load_json(row["scopes_json"], None)
            if admit(
                list(scopes) if isinstance(scopes, list) else [], row["home_project"]
            ):
                out.append(str(row["id"]))
        return out

    def pending_conflict_pairs(self) -> list[tuple[str, str, str]]:
        """``(a_id, b_id, pair id)`` for every conflict row whose status is
        ``pending``, whatever the detector: the recall surface's disputed
        mark, one query per brief and per prompt."""
        return [
            (str(row["a_id"]), str(row["b_id"]), str(row["id"]))
            for row in self._conn.execute(
                "SELECT a_id, b_id, id FROM conflicts WHERE status = 'pending' "
                "ORDER BY id"
            )
        ]

    def contradicted_at(self, memory_ids: Sequence[str]) -> dict[str, datetime]:
        """The newest ``use`` event with outcome ``contradicted`` naming
        each of `memory_ids`, by id; ids never contradicted are absent.
        Reads the ``use`` rows through the log's kind index, every
        session and the whole log, since a contradiction stays open until
        the record is updated or verified, however long ago it was
        recorded. The outcome is filtered in SQL, so the rows of every
        other outcome (nearly all of them) are never parsed here."""
        wanted = set(memory_ids)
        if not wanted:
            return {}
        out: dict[str, datetime] = {}
        for row in self._conn.execute(
            "SELECT ts, payload FROM log WHERE kind = 'use' "
            "AND json_valid(payload) "
            "AND json_extract(payload, '$.outcome') = 'contradicted' ORDER BY seq"
        ):
            try:
                fields = json.loads(row["payload"])
            except ValueError:
                continue
            if not isinstance(fields, dict) or fields.get("outcome") != "contradicted":
                continue
            ids = fields.get("ids") or fields.get("memory_ids")
            if not isinstance(ids, list):
                continue
            try:
                stamp = _dt(str(row["ts"]))
            except ValueError:
                continue
            for memory_id in ids:
                if isinstance(memory_id, str) and memory_id in wanted:
                    previous = out.get(memory_id)
                    if previous is None or stamp > previous:
                        out[memory_id] = stamp
        return out

    def events_of_kinds(
        self, kinds: Collection[str], *, since: datetime
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows of `kinds` stamped at or after `since`, in
        log order, read through the log's kind index rather than the
        whole log `events_since` walks. The cut compares the stamp's
        first nineteen characters, as `events_since` does."""
        names = sorted(set(kinds))
        if not names:
            return
        cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")[:19]
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND substr(ts, 1, 19) >= ? "
            "ORDER BY seq",
            (*names, cut),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def session_events_of_kinds(
        self, session: str, kinds: Collection[str]
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows of `kinds` recorded under `session`, in log
        order, whenever they were stamped: what a session's recall state
        is rebuilt from. Read through the log's kind index, the session
        compared on each row of those kinds."""
        names = sorted(set(kinds))
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND session = ? ORDER BY seq",
            (*names, session),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def reopen(self) -> Store:
        """Another connection to this store, for another thread: SQLite
        binds a connection to the thread that opened it, so a worker
        thread reads and records through its own. The same key directory,
        and never a rekey: a store this process opened without its key
        stays unable to mutate on the new connection too, and so does one
        whose key ring belongs to another store file. The set of
        memory ids a refold found unaccounted is shared, not copied, so a
        refold on either connection reaches the other, and so is
        ``strict_provenance``: a value the opener passed, or one this
        connection already read, is the new one's too."""
        other = Store.open(
            self._path,
            keys_dir=self._keyring.keys_dir,
            allow_rekey=False,
            strict_provenance=self._strict_provenance,
        )
        other._unaccounted_memory_ids = self._unaccounted_memory_ids
        if self._bound_elsewhere is not None:
            # The owner's open found the key ring bound to another store
            # file. A reader's open looks too, but reads the binding again,
            # and what it finds now (the bound file gone a moment, say) is
            # not this connection's owner's answer. Keep the key for
            # checking only, as the owner's connection does.
            if other._key is not None:
                other._read_key, other._key = other._key, None
            other._bound_elsewhere = self._bound_elsewhere
        return other

    def links_with_status(
        self, memory_id: str
    ) -> tuple[
        list[tuple[str, str, str | None]],
        list[tuple[str, str, str | None]],
        TrustRow | None,
    ]:
        """One record's outbound and inbound links and its trust row
        (None for an id the store does not hold, or when trust is
        unavailable)."""
        links = self.links_for_many([memory_id])[memory_id]
        trust = self.trust_rows([memory_id])
        return links[0], links[1], None if trust is None else trust.get(memory_id)

    # -- the event reads --------------------------------------------------------

    def events_since(
        self, since: datetime, *, memoised: bool = False
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows stamped at or after `since`, in log order:
        what a window read wants. The cut is the instant to the second,
        nineteen characters, compared against the whole stamp: both
        stamp spellings (``Z`` and ``+00:00``) share that prefix and a
        nineteen-character string sorts below any longer one that starts
        with it, so ``ts >= cut`` reads as a cut on the prefix. The read
        starts at the window's floor, the first row so stamped, found
        through the `log_by_ts` index (`_window_floor`), and walks the rows
        from there in log order, so a window at the log's end costs its
        own rows and not the log's (measured 0.07 ms against 2.1 ms for a
        100-row window on 34,000 rows). With `memoised`, the rows come from the store's event
        window memo (`_EVENT_WINDOWS`), which reads only the rows past the
        log's head as it last read it; the answer is the same, and each
        call gets events of its own. For a caller whose `since` only moves
        forward, as `memory_search`'s lookback does; a `since` earlier than
        the last memoised one reads the whole log again."""
        cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")
        # Both spellings sort correctly against an ISO instant to the
        # second; a `Z` sorts after `+`, so the cut compares on the prefix.
        prefix = cut[:19]
        if memoised:
            for row in self._window_rows(prefix):
                event = _event_from_columns(row.ts, row.session, row.kind, row.payload)
                if event is not None:
                    yield event
            return
        floor = self._window_floor(prefix)
        if floor is None:
            return
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE seq >= ? AND ts >= ? ORDER BY seq",
            (floor, prefix),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def _window_floor(self, prefix: str, *, head_seq: int | None = None) -> int | None:
        """The sequence number of the first row stamped at or after `prefix`
        (at or before `head_seq` when given): where a window read starts.
        Found through `log_by_ts`, named by ``INDEXED BY`` because the
        planner otherwise takes the rows in sequence order for the MIN and
        reads most of the log for a window at its end (1.4 ms against
        4 µs on 34,000 rows). None when no row is stamped in the window.
        A row stamped later than the rows after it (an import, a clock set
        back) lowers the floor, and the read from it is then the scan it
        was before, never more. A store whose index is gone (it has been
        in the schema since 9.0.0rc1, and `open` re-creates nothing, so
        only a hand-altered one) refuses the hint; the floor is then read
        without it, by the scan every window read was before."""
        hinted = "SELECT MIN(seq) FROM log INDEXED BY log_by_ts WHERE ts >= ?"
        params: tuple[Any, ...] = (prefix,)
        if head_seq is not None:
            hinted += " AND seq <= ?"
            params = (prefix, head_seq)
        try:
            row = self._conn.execute(hinted, params).fetchone()
        except sqlite3.OperationalError:
            plain = hinted.replace(" INDEXED BY log_by_ts", "")
            row = self._conn.execute(plain, params).fetchone()
        value = None if row is None else row[0]
        return None if value is None else int(value)

    def _window_rows(self, prefix: str) -> tuple[_WindowRow, ...]:
        """The telemetry rows whose stamp's first nineteen characters are at
        or after `prefix`, in log order, through the event window memo."""
        key = (str(self._path), self._store_id)
        in_transaction = self._in_batch or self._conn.in_transaction
        with _EVENT_WINDOWS_LOCK:
            window = _EVENT_WINDOWS.get(key)
            if window is not None:
                _EVENT_WINDOWS.move_to_end(key)
        rows: list[_WindowRow] | None = None
        head_seq, head_mac = 0, ""
        if window is not None and window.cut <= prefix and not in_transaction:
            head = self._conn.execute(
                "SELECT mac FROM log WHERE seq = ?", (window.head_seq,)
            ).fetchone()
            if head is not None and str(head["mac"]) == window.head_mac:
                rows = [row for row in window.rows if row.cut >= prefix]
                head_seq, head_mac = window.head_seq, window.head_mac
                # Past the head, the cut is applied by SQLite, as the
                # uncached read applies it.
                read = self._conn.execute(
                    "SELECT seq, ts, session, kind, payload, mac, "
                    "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                    "FROM log WHERE seq > ? ORDER BY seq",
                    (prefix, head_seq),
                )
                head_seq, head_mac = self._gather_window(read, rows, head_seq, head_mac)
        if rows is None:
            rows = []
            # The head first, then the window's rows at or before it, so the
            # memo's head and its rows are one snapshot without a
            # transaction: a row appended between the two reads has a higher
            # sequence number, and the next read, from the head, picks it
            # up. The floor comes through `log_by_ts` (`_window_floor`), so
            # the read touches the window's rows where one statement
            # scanned the log.
            head = self._conn.execute(
                "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if head is not None:
                head_seq, head_mac = int(head["seq"]), str(head["mac"])
                floor = self._window_floor(prefix, head_seq=head_seq)
                if floor is not None:
                    read = self._conn.execute(
                        "SELECT seq, ts, session, kind, payload, mac, "
                        "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                        "FROM log WHERE seq >= ? AND seq <= ? AND ts >= ? "
                        "ORDER BY seq",
                        (prefix, floor, head_seq, prefix),
                    )
                    # The head stays the log's last row, not the window's.
                    self._gather_window(read, rows, head_seq, head_mac)
        kept = tuple(rows)
        if not in_transaction and all(isinstance(row.cut, str) for row in kept):
            with _EVENT_WINDOWS_LOCK:
                _EVENT_WINDOWS[key] = _EventWindow(
                    cut=prefix, head_seq=head_seq, head_mac=head_mac, rows=kept
                )
                _EVENT_WINDOWS.move_to_end(key)
                while len(_EVENT_WINDOWS) > EVENT_WINDOW_STORES:
                    _EVENT_WINDOWS.popitem(last=False)
        return kept

    @staticmethod
    def _gather_window(
        read: sqlite3.Cursor, rows: list[_WindowRow], head_seq: int, head_mac: str
    ) -> tuple[int, str]:
        """Append the in-window telemetry rows of `read` to `rows`; return
        the last row's sequence number and MAC, the new head."""
        for raw in read:
            head_seq, head_mac = int(raw["seq"]), str(raw["mac"])
            kind = str(raw["kind"])
            if not raw["inside"] or kind in _NOT_TELEMETRY:
                continue
            rows.append(
                _WindowRow(
                    cut=raw["cut"],
                    ts=str(raw["ts"]),
                    session=None if raw["session"] is None else str(raw["session"]),
                    kind=kind,
                    payload=raw["payload"],
                )
            )
        return head_seq, head_mac

    def iter_events_backward(self) -> Iterator[dict[str, Any]]:
        """The telemetry rows newest first."""
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "ORDER BY seq DESC"
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def _event_from_raw(self, raw: sqlite3.Row) -> dict[str, Any] | None:
        return _event_from_columns(
            str(raw["ts"]),
            None if raw["session"] is None else str(raw["session"]),
            str(raw["kind"]),
            raw["payload"],
        )

    # -- the journal ------------------------------------------------------------

    def write_episode(
        self,
        *,
        session_id: str,
        body: str,
        scopes: list[str] | None = None,
        takeaway: str | None = None,
        swarm_id: str | None = None,
        origin: Origin | None = None,
        now: datetime | None = None,
        home: RecordHome | None = None,
    ) -> Episode:
        """Append one journal entry for `session_id`, with its home when
        `home` is given (`put_episode`)."""
        if not body or not body.strip():
            raise ValueError("episode body must be a non-empty string")
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=now or utcnow(),
            body=body.strip() + "\n",
            scopes=list(scopes or []),
            takeaway=takeaway.strip() if takeaway else None,
            swarm_id=swarm_id,
            origin=origin,
        )
        return self.put_episode(episode, session=session_id, home=home)

    def write_floor(
        self,
        *,
        session_id: str,
        origin: Origin | None = None,
        now: datetime | None = None,
        home: RecordHome | None = None,
    ) -> Episode:
        """The session-tag floor the handoff writes at its entry, so a
        session that crashes before its first takeaway still marks its
        worktree; homed like any episode when `home` is given."""
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=now or utcnow(),
            body="(session-tag floor, no takeaway recorded)\n",
            scopes=[],
            takeaway=None,
            origin=origin,
            is_floor=True,
        )
        return self.put_episode(episode, session=session_id, home=home)

    def episodes_by_session(self, session_id: str) -> list[Episode]:
        out = list(self.iter_episodes(session_id=session_id))
        out.sort(key=lambda e: e.created)
        return out

    def episodes_by_swarm(self, swarm_id: str) -> list[Episode]:
        out = [
            _row_to_episode(row)
            for row in self._conn.execute(
                "SELECT * FROM episodes WHERE swarm_id = ? ORDER BY created, rowid",
                (swarm_id,),
            )
        ]
        return out

    def episode_session_ids(self) -> list[str]:
        """Every session with at least one episode, oldest first."""
        return [
            str(row["session_id"])
            for row in self._conn.execute(
                "SELECT session_id, MIN(rowid) AS first FROM episodes "
                "GROUP BY session_id ORDER BY first"
            )
        ]

    def prunable_episode_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> list[str]:
        """Sessions whose newest episode is older than the TTL."""
        if ttl_days <= 0:
            return []
        cutoff = _iso((now or utcnow()) - timedelta(days=ttl_days))
        return [
            str(row["session_id"])
            for row in self._conn.execute(
                "SELECT session_id, MAX(created) AS newest FROM episodes "
                "GROUP BY session_id HAVING newest < ? ORDER BY newest",
                (cutoff,),
            )
        ]

    def prune_episode_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        keep_session_id: str | None = None,
        now: datetime | None = None,
        session: str | None = None,
    ) -> list[str]:
        """Delete every episode of the sessions past the TTL, except the
        session named by `keep_session_id`. Returns the pruned session
        ids."""
        pruned = [
            sid
            for sid in self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
            if sid != keep_session_id
        ]
        if not pruned:
            return []
        with self.batch():
            for sid in pruned:
                for episode in self.iter_episodes(session_id=sid):
                    with self._transaction() as tx:
                        self._mutate(
                            tx, "episode_delete", {"id": episode.id}, session=session
                        )
        return pruned

    def episode_provenance(self, episode_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the episodes asked: ``local`` when the row's
        pointer into the log verifies, ``unaccounted`` when it does not
        (the planted shape, a row in a foreign segment, or one in any
        segment whose key is not held under ``strict_provenance``, that
        this machine's own key did not sign either; `_pointer_verified`), the
        row's label as stored when the key is not on this machine. Unknown
        ids are omitted."""
        ids = list(dict.fromkeys(episode_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT id, log_mac FROM episodes WHERE id IN ({placeholders})", batch
            ):
                episode_id = str(row["id"])
                verified = self._pointer_verified(
                    episode_id, "episode_put", row["log_mac"]
                )
                out[episode_id] = UNACCOUNTED if verified is False else LOCAL
        return out

    def episode_volume(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> EpisodeVolume:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS episodes, "
            "COALESCE(SUM(LENGTH(CAST(body AS BLOB)) + "
            "LENGTH(CAST(COALESCE(takeaway, '') AS BLOB))), 0) AS bytes "
            "FROM episodes"
        ).fetchone()
        return EpisodeVolume(
            sessions=int(row["sessions"]),
            episodes=int(row["episodes"]),
            bytes=int(row["bytes"]),
            prunable_sessions=len(
                self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
            ),
            ttl_days=ttl_days,
        )

    # -- the log ------------------------------------------------------------

    def log_rows(self, since_seq: int = 0) -> list[LogRow]:
        return list(iter_rows(self._conn, since_seq))

    def refold(self) -> dict[str, Any]:
        """Replay every mutation row into a scratch database and compare
        each folded table with the live one. ``unaccounted`` lists live
        rows the replay does not produce (planted or edited), ``missing``
        the replayed rows the live table lacks (deleted). A row whose
        payload cannot be applied is reported as ``payload_invalid`` and
        skipped. The membership tables are folded by the same replay and
        reported under ``membership``, table by table; a membership table
        the live store lacks (a read-only store an earlier build created)
        reads as empty."""
        scratch = sqlite3.connect(":memory:", isolation_level=None)
        scratch.row_factory = sqlite3.Row
        problems: list[dict[str, Any]] = []
        try:
            scratch.executescript(SCHEMA)
            scratch.executescript(MEMBERSHIP_SCHEMA)
            scratch.execute("BEGIN")
            for row in iter_rows(self._conn):
                if row.kind not in MUTATION_KINDS:
                    continue
                try:
                    payload = json.loads(row.payload)
                    if not isinstance(payload, dict):
                        raise TypeError("payload is not an object")
                    _apply_mutation(scratch, row.kind, payload, log_mac=row.mac)
                except (ValueError, KeyError, TypeError, ValidationError) as exc:
                    problems.append(
                        {
                            "seq": row.seq,
                            "kind": row.kind,
                            "problem": "payload_invalid",
                            "detail": str(exc).splitlines()[0][:200],
                        }
                    )
            scratch.execute("COMMIT")
            tables: dict[str, Any] = {}
            membership: dict[str, Any] = {}
            diverged = bool(problems)
            present = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in (*FOLDED_TABLES, *MEMBERSHIP_TABLES):
                live = _table_rows(self._conn, table) if table in present else {}
                folded = _table_rows(scratch, table)
                unaccounted = sorted(
                    (
                        _key_label(table, k)
                        for k, v in live.items()
                        if folded.get(k) != v
                    ),
                    key=str,
                )
                missing = sorted(
                    (_key_label(table, k) for k in folded if k not in live), key=str
                )
                if unaccounted or missing:
                    diverged = True
                report = tables if table in FOLDED_TABLES else membership
                report[table] = {
                    "unaccounted": unaccounted,
                    "missing": missing,
                    "rows": len(live),
                }
            # In place: a connection `reopen` made shares this set, and a
            # worker may read it meanwhile. Add first, then drop the rest:
            # each step is one call on the set, and between them it holds
            # every id still unaccounted, never fewer.
            found = set(tables["memories"]["unaccounted"])
            self._unaccounted_memory_ids.update(found)
            self._unaccounted_memory_ids.intersection_update(found)
            return {
                "status": "diverged" if diverged else "ok",
                "tables": tables,
                "membership": membership,
                "problems": problems,
            }
        finally:
            scratch.close()

    def log_verify(self) -> dict[str, Any]:
        """The chain report merged with the fold: ``tampered`` when a row
        fails its MAC, the chain breaks, the head names a row the log no
        longer has, the origin anchor no longer matches it, or a table
        differs from the fold; ``unverifiable`` when a segment's key or
        the head is absent; ``ok`` otherwise. ``origin`` is the anchor
        check (`log.OriginCheck.to_dict`)."""
        chain = verify_chain(
            self._conn, keyring=self._keyring, current_fingerprint=self.key_fingerprint
        ).to_dict()
        fold = self.refold()
        problems = list(chain["problems"]) + list(fold["problems"])
        problems.sort(key=lambda p: (int(p["seq"]), str(p["problem"])))
        status = chain["status"]
        if fold["status"] != "ok":
            status = "tampered"
        return {
            "status": status,
            "path": str(self._path),
            "store_id": self._store_id,
            "key_fingerprint": self.key_fingerprint,
            "rows": chain["rows"],
            "segments": chain["segments"],
            "head": chain["head"],
            "origin": chain["origin"],
            "problems": problems,
            "fold": {
                "status": fold["status"],
                "tables": fold["tables"],
                "membership": fold["membership"],
            },
            "unaccounted_ids": list(fold["tables"]["memories"]["unaccounted"]),
        }

    # -- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        size = 0
        for sibling in (
            self._path,
            self._path.with_suffix(self._path.suffix + "-wal"),
        ):
            with contextlib.suppress(OSError):
                size += sibling.stat().st_size
        head = self._keyring.read_head()
        meta = self.meta()
        return {
            "path": str(self._path),
            "store_id": self._store_id,
            "schema_version": int(meta.get("schema_version", "0")),
            "engine_version": meta.get("engine_version"),
            "memories": self.count_memories(),
            "tombstones": self.count_tombstones(),
            "episodes": self.count_episodes(),
            "log_rows": int(
                self._conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
            ),
            "size_bytes": size,
            "key_fingerprint": meta.get("key_fingerprint"),
            "key_present": self._key is not None,
            "keys_dir": str(self._keyring.keys_dir),
            "head_seq": None if head is None else head.seq,
        }


# ---------------------------------------------------------------------------
# Per-request store resolution
# ---------------------------------------------------------------------------


class StoreSource(Protocol):
    """Where a request's store comes from. `ToolHandlers.for_request` asks
    this at every tool call; the shipped source returns one store for
    every request. A hosted, multi-tenant deployment replaces the
    policy behind this seam and nothing else."""

    def for_request(self, ctx: Any | None) -> Store: ...


class DefaultStoreSource:
    """The one-store policy: every request is served from the process
    store. `principal_of` exposes the attested caller a later policy
    would key on, resolved through `identity.bind`, the same binder the
    session registry uses."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def for_request(self, ctx: Any | None) -> Store:
        return self._store

    def principal_of(self, ctx: Any | None) -> str | None:
        from . import identity

        return identity.bind(ctx).actor.principal


__all__ = [
    "Store",
    "DEFAULT_EPISODE_TTL_DAYS",
    "EpisodeVolume",
    "TrustRow",
    "FOLDED_TABLES",
    "IMPORTED",
    "IMPORTED_FROM_V8",
    "LOCAL",
    "MEMBERSHIP_EVENT_KINDS",
    "MEMBERSHIP_MUTATION_KINDS",
    "MEMBERSHIP_SCHEMA",
    "MEMBERSHIP_STORE_ID",
    "MEMBERSHIP_TABLES",
    "MUTATION_KINDS",
    "RecordHome",
    "StandingAnswer",
    "PROVENANCE_LABELS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "STORE_FILENAME",
    "SYNCED",
    "UNACCOUNTED",
    "ConcurrentUpdateError",
    "DefaultStoreSource",
    "MemoryNotFoundError",
    "MemoryRow",
    "NotFoundError",
    "NotTombstonedError",
    "StoreSource",
    "store_path",
    "TombstoneRow",
    "TombstonedError",
    "event_import_payload",
]
