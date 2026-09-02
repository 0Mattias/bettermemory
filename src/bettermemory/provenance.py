"""Provenance: how each memory entered the store, derived at index build.

Nothing on disk says whether a memory file was written by this host's
own code path through the write gates, arrived by `sync pull`, or was
placed by hand. Every trust field a file carries (`last_verified_at`,
`source`, `confidence`, `claims`) is frontmatter, and frontmatter is
whatever the last writer chose to put there; a hand-written stamp reads
fresh on every surface. This module derives one label per memory from
evidence the file cannot forge, and the index carries it.

The four labels
---------------
- ``local``: written, ingested, promoted, accepted or restored by this
  host's own code path. Stamped at the index upsert `Store.write` and
  `Store.restore` perform (so every in-process creation is covered even
  with telemetry off), and re-derived at rebuild from the write-side
  events that carry the memory id (`creation_id` is the join). Sticky:
  once local, a rebuild keeps it local.
- ``synced``: no local creation on record, but the file is named by a
  `sync_pull` event, or (for pulls made before that event existed) is
  tracked in the store's own sync repo while the event log covers the
  memory's creation window. The shape of a pull, or a push from another
  host.
- ``untracked``: the event log cannot speak to it. Either the store keeps
  no events at all, or the memory's `created` predates the oldest
  surviving event and no classified rebuild had a chance to see it
  arrive. Honest silence, not suspicion.
- ``unaccounted``: the log covers the memory's creation window, no
  write-side event names it, and nothing ties it to the sync repo; or it
  claims a `created` older than the log while appearing for the first
  time after a classified baseline already existed. The hand-planted
  shape. Sticky for the same reason `local` is: deleting old event
  archives must not launder a flag into silence.

Derivation order, per memory, at `index.rebuild`
------------------------------------------------
1. prior ``local`` or a creation event naming the id      -> local
2. a `sync_pull` event naming the file                     -> synced
3. prior ``unaccounted``                                   -> unaccounted
4. no events in the store at all                           -> untracked
5. `created` older than the oldest surviving event:
     a classified baseline exists and the id is new to it  -> unaccounted
     otherwise                                             -> untracked
6. tracked in the store's sync repo                        -> synced
7. otherwise                                               -> unaccounted

The prior labels come from the index being rebuilt (and from the
`meta.provenance_carry` stash `index._ensure_schema` takes before a
schema or tokenizer drop); "baseline" means a classified rebuild has
completed on this index before, recorded as `meta.provenance_classified`.
Deleting `.index.sqlite` and running `bettermemory reindex` discards
both and reclassifies from events alone. That is the documented reset.

Cost: one pass over `iter_all_events` per rebuild (the same pass health
pays per call) and one `git ls-files` when the store root is a sync
repo. Zero per-search cost; the label is read with the row.

What this cannot see: an injection-driven legitimate write. A memory the
model was talked into writing through the gates is ``local`` by every
test here, and truthfully so. Cause provenance (what was in context at
write time) is a different question; `groundedness_check` and
`source_transcript` are its seed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .events import iter_all_events
from .time_utils import ensure_utc, parse_event_ts

if TYPE_CHECKING:
    from .models import Memory

log = logging.getLogger("bettermemory.provenance")

LOCAL = "local"
SYNCED = "synced"
UNTRACKED = "untracked"
UNACCOUNTED = "unaccounted"
LABELS: tuple[str, ...] = (LOCAL, SYNCED, UNTRACKED, UNACCOUNTED)

# The event kinds whose `id` field establishes a local creation, and the
# statuses that count. `write` with status `pending` carries no id (the
# id is minted at confirm, which is why `write_confirm` is in the set);
# `search` / `list` / `show` return ids they did not create and are
# never consulted here. `update` and `verify` name ids too, but they
# prove a memory was touched locally, not that it entered locally.
_CREATION_KINDS: frozenset[str] = frozenset(
    {"write_confirm", "restore", "consolidate_write"}
)
_WRITE_STATUSES: frozenset[str] = frozenset({"committed", "ingested"})
_GIT_TIMEOUT_S = 10.0


def creation_id(event: Mapping[str, Any]) -> str | None:
    """The memory id a write-side event establishes as locally created.

    Returns None for every event that does not: the retrieval kinds, a
    pending write, a proposal that was listed or dismissed rather than
    accepted, an update. The function is the single definition of the
    join, so the rebuild and the tests read one rule.
    """
    kind = event.get("kind")
    if kind == "write":
        if event.get("status") in _WRITE_STATUSES:
            return _id_field(event, "id")
        return None
    if kind in _CREATION_KINDS:
        return _id_field(event, "id")
    if kind == "memory_proposals":
        if event.get("action") == "accept":
            return _id_field(event, "id")
        return None
    if kind == "episode_promote":
        return _id_field(event, "memory_id")
    return None


def pulled_files(event: Mapping[str, Any]) -> list[str]:
    """The store-relative files a `sync_pull` event says the rebase changed."""
    if event.get("kind") != "sync_pull":
        return []
    files = event.get("files")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and f]


def _id_field(event: Mapping[str, Any], key: str) -> str | None:
    value = event.get(key)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Evidence:
    """Everything the classifier reads, gathered once per rebuild.

    `tracked_files` is None when the store root is not a sync repo (or
    git is unavailable), which disables rule 6 rather than misreading a
    parent repo's tracking as the store's own.
    """

    has_events: bool
    oldest_event_at: datetime | None
    local_ids: frozenset[str]
    pulled_files: frozenset[str]
    tracked_files: frozenset[str] | None


def gather_evidence(root: Path) -> Evidence:
    """One pass over the event log plus one `git ls-files` in a sync repo."""
    has_events = False
    oldest: datetime | None = None
    local_ids: set[str] = set()
    pulled: set[str] = set()
    for event in iter_all_events(root):
        has_events = True
        if oldest is None:
            # `iter_all_events` is chronological, so the first parseable
            # stamp is the oldest surviving one.
            oldest = parse_event_ts(event.get("ts"))
        memory_id = creation_id(event)
        if memory_id is not None:
            local_ids.add(memory_id)
        for name in pulled_files(event):
            pulled.add(Path(name).name)
    return Evidence(
        has_events=has_events,
        oldest_event_at=oldest,
        local_ids=frozenset(local_ids),
        pulled_files=frozenset(pulled),
        tracked_files=_tracked_files(root),
    )


def classify(
    memory: Memory,
    filename: str,
    evidence: Evidence,
    prior: Mapping[str, str],
    *,
    baseline: bool,
) -> str:
    """Apply the derivation order in the module docstring to one memory.

    `prior` maps ids to the labels the index carried before this rebuild
    (hook-stamped rows included); `baseline` is whether a classified
    rebuild has completed on this index before. Both come from
    `index._read_prior_provenance`.
    """
    previous = prior.get(memory.id)
    if previous == LOCAL or memory.id in evidence.local_ids:
        return LOCAL
    if filename in evidence.pulled_files:
        return SYNCED
    if previous == UNACCOUNTED:
        return UNACCOUNTED
    if not evidence.has_events:
        return UNTRACKED
    created = ensure_utc(memory.created)
    oldest = evidence.oldest_event_at
    if created is not None and oldest is not None and created < oldest:
        if baseline and memory.id not in prior:
            return UNACCOUNTED
        return UNTRACKED
    if evidence.tracked_files is not None and filename in evidence.tracked_files:
        return SYNCED
    return UNACCOUNTED


def is_sync_repo(root: Path) -> bool:
    """True when the store root itself is a git checkout, the shape
    `bettermemory sync init` creates. A store nested inside some other
    repository is not one: git would answer `ls-files` for the parent,
    and a parent's tracking says nothing about how memories arrived."""
    return (root / ".git").exists()


def _tracked_files(root: Path) -> frozenset[str] | None:
    if not is_sync_repo(root):
        return None
    binary = shutil.which("git")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("provenance: git ls-files failed in %s: %s", root, exc)
        return None
    if result.returncode != 0:
        return None
    # Memories live at the store root; tombstones, episodes and the
    # sidecars live in subdirectories or dotfiles. Top-level entries only.
    return frozenset(
        entry for entry in result.stdout.split("\0") if entry and "/" not in entry
    )


__all__ = [
    "LABELS",
    "LOCAL",
    "SYNCED",
    "UNACCOUNTED",
    "UNTRACKED",
    "Evidence",
    "classify",
    "creation_id",
    "gather_evidence",
    "is_sync_repo",
    "pulled_files",
]
