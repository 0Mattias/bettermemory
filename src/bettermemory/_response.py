"""Response-shape helpers for the MCP tool surface.

Serialisation takes domain objects (`MemoryHit`, `MemorySummary`,
`Memory`, `Origin`, `SimilarHit`, `TransientMatch`) and the per-request
inputs they need (a pinned `now`, the caller's origin, etc.) and returns
JSON-serializable dicts. Post-rank annotations reuse candidate snapshots
and make bounded, best-effort store reads. Event recording and session
state live on `ToolHandlers`.

`ResponseBuilder` captures the one piece of config every method needs
(`verification_stale_days`) so handlers don't have to thread it through
on every call. Each method takes a `now: datetime` kwarg per request so
a single multi-hit response uses one consistent "now" across rows.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from .attestation_paths import commit_paths
from . import _caches, githead
from ._git import timed_out_calls
from . import git_read_budget
from ._response_commit_counts import count_hits as _count_hits
from ._response_links import candidate_link_rows
from .credentials import CredentialMatch
from .durability import TransientMatch
from .control_tokens import named_tokens
from .events import _event_id_items
from .models import (
    Memory,
    MemoryHit,
    MemorySummary,
    SimilarHit,
    TombstonedSummary,
)
from .identity import SOURCE_PROCESS_CWD, Actor
from .native_text import has_surrogates
from .origin import (
    Origin,
    attribute_files_signature,
    failed_git_calls,
    files_held,
    gitattributes_signature,
    located_head,
    repo_toplevel_and_head,
    toplevel_and_head_from_files,
    walk_files_signature,
)
from .origin_dates import commit_author_timestamps
from .origin_history import Walked, note_unread
from .verify_basis import CommitDriftUnread, unread_recommendation
from .time_utils import isoformat_utc as _isoformat_utc
from .time_utils import isoformat_utc_optional as _isoformat_utc_optional
from .time_utils import parse_event_ts
from .claims import Claim, governed_claim_paths, load_claims

if TYPE_CHECKING:
    from .admission import Admission, DriftScope
    from .commit_legs import CommitLegUnchecked
    from .verify_anchoring import Anchoring
from .verify import (
    PathDriftReport,
    ResolvedCommitDrift,
    commit_drift_anchor_paths,
    compute_staleness_verdict,
    remote_verification_status,
    compute_verification_status,
    resolve_hit_drift,
    verdict_from_signals,
)

# The recommendation a stamped row carries when the store was opened
# without its key (`ResponseBuilder.apply_trust_unavailable`). One
# string, so the read surfaces and the tests say the same thing.
TRUST_UNAVAILABLE_RECOMMENDATION = (
    "This machine does not hold the store's key, so whether this host "
    "made this stamp is unknown. Spot-check a claim before relying on "
    "the memory; `bettermemory log verify` reports the same condition."
)


__all__ = ["ResponseBuilder", "check_reason", "isoformat", "isoformat_optional"]


# `isoformat` / `isoformat_optional` are the public names this module
# has exported since 1.x; keep them as aliases over the canonical
# `time_utils` helpers so the wire-format definition lives in one
# place but downstream callers (handlers, tests) don't have to chase
# the rename.
isoformat = _isoformat_utc
isoformat_optional = _isoformat_utc_optional

# The window over which a negative use outcome (ignored / contradicted)
# stays live for `attach_recent_negative_outcomes`, the hit annotation.
# Ranking never reads it: the usage multipliers left in 9.0.0.
NEGATIVE_OUTCOME_WINDOW_DAYS = 30

# A write response lists at most this many similar memories per list, the
# most similar first, and counts the rest in a `<list>_more` field. On a
# store of a thousand memories a short body grazes dozens as "medium", and
# the full lists outgrew the clients' tool-output limits. The write event
# keeps every id.
SIMILAR_HITS_SHOWN = 5

# The per-hit commit-drift memo of
# `ResponseBuilder.attach_commit_drift_counts`: one hit's resolution, keyed
# on every input of it. `memory_show` and the session brief read and fill
# it too (`verdict`), through the same pass (`_DriftPass`) and so the same
# key. The key is the repository root and the commit HEAD names as the
# repository's files give them (where they do not, as the search's `git
# rev-parse` does, `_DriftPass._keyed_pair`), the caller's directory, the
# memory's anchors (derived from its body and verified_paths) and its
# declared claims as stored, the verify instant, the stamp's recorded head,
# for a hit with a governed claim the attribute files its patch stream
# reads (`origin_gitfiles.attribute_files_signature` and
# `origin_gitfiles.gitattributes_signature`; None for every other hit, which
# reads none), and the files beside the history its git reads
# (`origin_gitfiles.walk_files_signature`: the repository's config and, under
# extensions.worktreeConfig, its config.worktree, the working tree's
# .gitmodules, and the index's .gitmodules entry where git reads it:
# unmerged, or the working tree has none; the settings there that moved a
# log, log.follow, log.showRoot and a submodule's ignore, are pinned on
# every git `origin` runs, `origin_history.AS_STORED_CONFIG` and
# `AS_STORED_DIFF`, and the stamps stay); the value is the resolved count,
# basis and claim detail, None for a hit whose count is omitted, or
# `CommitDriftUnread` where the history was not read, with why. A
# commit moves the head and so every key; a new stamp, a rewritten body, a
# changed claim or an edited attributes file changes its own row's, and an
# edited config or
# .gitmodules, in the working tree or the index where git reads it, every
# row's; a write to the index that leaves .gitmodules as it was changes
# none. A value computed while a git process failed is never stored
# (`origin.failed_git_calls`), save one whose every failure was a timeout,
# kept apart for `_TIMED_OUT_SECONDS` (`_DRIFT_TIMED_OUT`), and a search
# during which the head, a keyed
# file or the index moved, even to move back, stores nothing
# (`origin_gitfiles.files_held`, `githead.signature`). What the key does not see
# (git's configuration outside the repository's config file, a change
# within one tick of the filesystem's clock that reuses an inode number,
# and the rest) is listed
# on `_DriftPass`. The values are frozen,
# so a hit can share one without a later hit's edit reaching it. Bounded
# LRU; the lock makes each look-up-and-touch and each merge-and-evict
# atomic, and the resolution runs outside it. Registered with `_caches`,
# which empties it before each test.
_DRIFT_MEMO_CAP = 5000
_DriftKey: TypeAlias = tuple[
    str,
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
    datetime,
    str | None,
    tuple[object, ...] | None,
    tuple[object, ...],
]
_Drift: TypeAlias = ResolvedCommitDrift | CommitDriftUnread | None
_DRIFT_MEMO: OrderedDict[_DriftKey, _Drift] = OrderedDict()
_DRIFT_MEMO_LOCK = threading.Lock()

# The resolutions a git timeout cut short, by key, with when each was kept
# (`time.monotonic`): the conservative count the timeout folded into (a walk,
# a path-filtered log or a patch stream past its 5 s bound), which the same
# call on the same history meets again, so a hit whose window times out
# paid the bound on every search (ENG-220's review).
# Kept only where every failure of the resolution was a timeout
# (`_git.timed_out_calls`), under the key a value is kept under and the same
# checks at the end of the search; for a minute, as a failed whole-history
# read is (`origin_history._FAILURE_SECONDS`), so a timeout a loaded host
# met clears within it. Bounded as `_DRIFT_MEMO` is, and emptied with it.
_TIMED_OUT_SECONDS = 60.0
_DRIFT_TIMED_OUT: OrderedDict[_DriftKey, tuple[float, _Drift]] = OrderedDict()


@_caches.register
def _clear_drift_memo() -> None:
    with _DRIFT_MEMO_LOCK:
        _DRIFT_MEMO.clear()
        _DRIFT_TIMED_OUT.clear()


def _recall_drift(key: _DriftKey) -> tuple[bool, _Drift]:
    """Whether the per-hit memo holds `key`, and the value it holds (or a
    timeout's, less than `_TIMED_OUT_SECONDS` old); a hit becomes the most
    recently used entry."""
    with _DRIFT_MEMO_LOCK:
        if key in _DRIFT_MEMO:
            _DRIFT_MEMO.move_to_end(key)
            return True, _DRIFT_MEMO[key]
        timed_out = _DRIFT_TIMED_OUT.get(key)
        if timed_out is not None:
            if 0.0 <= time.monotonic() - timed_out[0] < _TIMED_OUT_SECONDS:
                return True, timed_out[1]
            del _DRIFT_TIMED_OUT[key]
    return False, None


def _remember_drift(
    resolved: dict[_DriftKey, _Drift], timed_out: dict[_DriftKey, _Drift]
) -> None:
    """Merge one search's resolutions, and those a timeout cut short, into
    the per-hit memo and evict the least recently used entries past
    `_DRIFT_MEMO_CAP`."""
    now = time.monotonic()
    with _DRIFT_MEMO_LOCK:
        _DRIFT_MEMO.update(resolved)
        for key, value in timed_out.items():
            _DRIFT_TIMED_OUT[key] = (now, value)
            _DRIFT_TIMED_OUT.move_to_end(key)
        for memo in (_DRIFT_MEMO, _DRIFT_TIMED_OUT):
            while len(memo) > _DRIFT_MEMO_CAP:
                memo.popitem(last=False)


# Stands for the repository-wide attribute files before a search has read
# them.
_UNREAD = object()


class _DriftPass:
    """One pass of per-hit commit-drift resolution against one caller
    directory: the state a pass over many memories keeps between rows, and
    the one way a row's resolution is keyed, recalled from `_DRIFT_MEMO`,
    resolved and stored.

    `ResponseBuilder.attach_commit_drift_counts` runs one per search and
    `verdict.resolve_verdicts` / `verdict.memoised_verdicts` one per call,
    so `memory_search`'s hits and `memory_show` read one memo through one
    key. A search's cold cost is set out on `attach_commit_drift_counts`;
    the memo, its key and what it keeps are set out below, as a search's
    hits meet them.

    `caller_repo` is the caller's `origin.repo`; `cwd` its directory;
    `admission` what the pass reads under (`admission.DriftScope`: the
    request's admission, or the records one decided), which decides whose
    records' commits are counted here (`lookup`); a pass is never opened
    without one, so no caller counts drift for a record it did not admit. The
    repo root and the head are read from the repository's files as the
    pass opens (no process). The head's whole history (`timestamps`) and
    the root and head from git where the files decline (`located`) are
    read on first use, so a pass the memo answers whole runs no git
    process where the files settle the root and the head, and the one
    ``git rev-parse`` that keys its rows where they do not (below). The
    history is read at the head the rows key on, named by its hash, so a
    commit landing between the two reads is never counted under the other
    head's key; a row is keyed only where the history the pass read was
    read at its key's head (`_history_at`). What
    the pass resolves is kept by `store`, at its end, only while what it
    was keyed on still reads as it did when the pass opened.

    Not shared between threads: each caller opens its own. The memo it
    reads and merges into is lock-guarded, and every other memo the
    resolution reaches (the whole-history log, the walks, the config and
    index readers) is too, so passes on several threads, a search's and a
    worker thread's, can run at once.

    WARM, the count is zero for every hit the memo holds, that one
    ``git rev-parse`` aside. A hit it does
    not hold pays its cold cost on every search: one the key cannot
    settle (below), and one whose git work fails every time (the next
    paragraph). Each hit's resolution is memoised (`_DRIFT_MEMO`) on
    exactly its inputs: the root, the head, the caller's directory, the
    anchors, the declared claims, the verify instant and
    `verified_head`; for a hit with a governed claim, the attribute
    files its patch stream reads (the ``.gitattributes`` on each
    governed file's path, ``info/attributes``, the global attributes
    file and the repository's ``config``); and for every hit, the files
    beside the history its git reads (`origin_gitfiles.walk_files_signature`:
    the repository's ``config`` and, under ``extensions.worktreeConfig``,
    its ``config.worktree``, the working tree's ``.gitmodules`` and the
    index's ``.gitmodules`` entry where git reads it, unmerged or where
    the working tree has none; the walk memo keys on them too. What
    moved a log there, ``log.follow``, ``log.showRoot`` and a
    submodule's ``ignore``, is pinned on every git `origin` runs,
    `origin_history.AS_STORED_CONFIG` and `AS_STORED_DIFF`). A file is
    keyed on its mtime, ctime, size, inode and mode, or its absence, so a
    write, a replacement, a creation, a removal or a chmod changes the
    key; the index's entry is keyed on
    its mode and object, read from the index in-process
    (`origin_gitfiles._index_gitmodules`, once per state of the index). A
    governed path that is a directory or a pattern, a ``config`` that
    names an attributes file or tree, sets ``log.follow`` (under
    which the patch stream read the attributes of a rename source's
    path before every git pinned it off; the file is read as git
    reads it, so ``push.followTags`` or a branch
    named ``follow-up`` is no such config), holds a NUL byte or is
    refused by git, an index the reader does not read exactly (split, of
    an unknown version, a SHA-256 repository's, or GIT_INDEX_FILE set),
    and a file that cannot be stamped keep a hit out of the memo. A
    commit moves the head and so every key; a new stamp, a rewritten
    body, a changed claim or an edited attributes file changes its own
    row's, and an edited ``config`` or ``.gitmodules`` every row's, in
    the working tree or in the index where git reads it. A write to the
    index that leaves ``.gitmodules`` as it was (``git add`` of another
    file, a ``git status`` that refreshes the index) changes no key.

    A failure is not memoised, at a cost, save a timeout. A value is not
    kept when any git process its resolution ran failed, whether git
    exited non-zero or could not run (the fallback taken on a failure is
    what the uncached code answers on that call and not the next), and no
    memo keeps a walk that came back None. A hit whose walk,
    path-filtered log or patch stream fails on every search (a stamp
    naming a commit the repository lacks, a textconv driver that fails, a
    window of a partial clone whose patch reads a blob the clone lacks,
    since no git the package runs fetches: its local commits are narrowed
    after the stream fails, `origin_patch.local_patch_window`) therefore
    resolves again on every search and forks what a cold search forks for
    it, less the split's listing and check, which `origin_patch` keeps
    while the object store holds still. A resolution whose every failure
    was a timeout (a walk, a log or a patch past its 5 s bound) is kept
    for a minute under its key (`_DRIFT_TIMED_OUT`), so such a hit pays
    the bound once a minute, not on every search. The other way round, a
    memoised hit serves the value computed when git answered, so a failure that
    strikes a later search (an object made unreadable after the value
    was kept) is not observed on that hit, where the uncached code
    would take the fallback.

    Nothing is kept from a search during which the head or a keyed file
    moved, even to move back before the search ended. At the end the
    root and the head are read from the files again, and
    `githead.signature` (HEAD, the loose refs HEAD's chain reads,
    ``packed-refs`` and ``config``, which git rewrites through a lock
    file and a rename, leaving a new inode and ctime) must read as it
    did before the first resolution; the keyed files are stamped again;
    the directories that hold them (each directory of a keyed
    ``.gitattributes`` chain, ``info`` in the common directory, the
    global attributes file's directory, the root) must keep the (mtime,
    ctime) read when their files were keyed, which a file created and
    removed meanwhile moves; and so must the index file, so a search
    during which git wrote the index, which could move its
    ``.gitmodules`` entry and back, keeps nothing. Where the files do
    not settle the root and the head (off POSIX, another account's
    repository), ``git rev-parse --show-toplevel HEAD`` answers them
    once per search, and the rows key on its answer where `githead.
    locate` reads the repository at that root and the same head there
    (`_keyed_pair`), as the split's memo keys (`origin_patch._memo_key`);
    the head is read there again at the end, with `githead.signature`
    read as `locate` reads. Under a variable that moves git's discovery
    (GIT_DIR and the rest) no row is keyed, and neither memo is used.

    What the key and the checks do not see, read from the memo until
    the head moves: the working tree's symbolic links on an anchor's
    path and the home directory ``~`` expands to
    (`origin.resolve_repo_pathspecs` reads both); git's configuration
    outside the repository's ``config`` and ``config.worktree`` (the
    global and system files, a file an include there names, the
    environment, and what they set: a ``core.attributesFile`` naming a
    file other than
    the default, a diff driver, the program a textconv runs); the system
    attributes file (replace refs and grafts move no answer:
    `origin_history.AS_STORED_ENV`; a shallow file made, deepened or
    removed under an unchanged head is a new key, as the walk files
    stamp it); and a change that leaves every
    stamped field
    and every held directory as it was: a file rewritten, or a head
    moved and moved back, within one tick of the filesystem's clock
    with the inode number reused; a ``config`` created and removed
    during a search in a repository that has none, whose directory no
    check holds; and a ``config`` that is a symbolic link
    whose target's directory is swapped for another and back during a
    search, which leaves the file the link names with its old stamp. A
    repository whose refs `githead` cannot read (the reftable format)
    is not settled from the files, so no per-hit memo is used there.
    While files are created and removed steadily in a held directory
    (the root, a keyed ``.gitattributes`` chain's directories, the
    global attributes file's nearest existing ancestor), the search
    that held it keeps nothing, and every such search pays its cold
    cost (`origin_gitfiles._hold_directory`).
    """

    def __init__(
        self,
        cwd: Path,
        caller_repo: str,
        admission: DriftScope,
        *,
        at_home: bool = False,
    ) -> None:
        self.cwd = cwd
        self.caller_repo = caller_repo
        self.admission = admission
        # A pass at the root a record's leg resolves to away from the
        # caller (`commit_legs.CommitLegs`): its records need name no
        # remote, since the root was read from their home or recorded
        # worktree, not matched by URL.
        self.at_home = at_home
        self.settled = toplevel_and_head_from_files(cwd)
        # The root and the head the rows key on (`_keyed_pair`).
        self._keyed = self.settled
        self._keyed_read = self.settled is not None
        self._timestamps: list[datetime] | CommitDriftUnread | None = None
        # The head the whole history was read at (None where no pair named
        # one), once `timestamps` has read it.
        self._history_read = False
        self._history_head: str | None = None
        self._located: tuple[Path, str] | None = None
        self._located_read = False
        # What this pass resolved, stored at the end if nothing it is keyed
        # on moved while it ran; and what a timeout cut short, kept a while.
        self._resolved: dict[_DriftKey, _Drift] = {}
        self._timed_out: dict[_DriftKey, _Drift] = {}
        # The walks of this pass no memo keeps: several hits at one dead
        # anchor fork one walk, and so do several at one anchor where the
        # walk memo cannot key the walk (`commits_since_anchor`).
        self._walked: Walked = {}
        # The files the hits key on: the repository's attribute files read
        # once per pass, the working tree's once per set of governed
        # files, the files beside the walk once per pass; and the stamps
        # of the directories that hold them, for the check at the end.
        self._repository_attributes: object = _UNREAD
        self._attribute_chains: dict[tuple[str, ...], tuple[object, ...] | None] = {}
        self._walk_files: tuple[object, ...] | None = None
        self._walk_files_read = False
        self._held_directories: dict[str, tuple[int, int]] = {}
        # The signature of the files that name the head, read before the
        # first resolution and again at the end: a head moved away and back
        # meanwhile reads as the same head, but leaves HEAD or its ref
        # rewritten. No git process that reads HEAD runs before the first
        # resolution, and a pass the memo answers whole stores nothing, so
        # it reads none.
        self._signature: githead.Signature | None = None
        # Set by a `store` whose check failed: nothing this pass resolves
        # is stored after that.
        self._broken = False

    @git_read_budget.unread
    def timestamps(self) -> list[datetime] | CommitDriftUnread | None:
        """The head's whole history (`commit_author_timestamps`, memoised
        per root and head), read on first use and kept for the pass once it
        answers: the dates, or why they were not read, which each later row
        that reads it counts as a failure again (`origin_history.
        note_unread`, a timeout as a timeout), as the row that read it did,
        so no row counted on it is kept, save a timeout's for a while. A
        read where none applies is made again by the next row that needs
        it, as the uncached `verify.compute_commit_drift` makes it per show.

        Read at the root and the head the rows key on (`located`: the
        files' pair, else the pass's one ``git rev-parse``), the log naming
        the head by its hash. Read at the head `githead.locate` read, as it
        was off POSIX and in another account's repository, a commit landing
        before the rev-parse put the earlier head's dates under the later
        head's key, and a row counted on them read caught up there on every
        search after (ENG-220's review, round E)."""
        if self._timestamps is None:
            pair = self.located()
            self._history_read = True
            self._history_head = None if pair is None else pair[1]
            self._timestamps = commit_author_timestamps(self.cwd, located=pair)
        elif isinstance(self._timestamps, CommitDriftUnread):
            note_unread(self._timestamps)
        return self._timestamps

    def history_applies(self) -> bool:
        """Whether a history applies at the pass's root, for a row whose leg
        resolved to None (`commit_legs.CommitLegs`): the whole history's
        answer where the pass read it; else yes where the root and the head
        resolved (`located`: a repository whose branch has a commit, which
        git reads a history at), with no log; and the whole history read
        only where neither settles it. A leg on the walk from a stamp's head
        reads no dates, and reading them to answer this cost a cold show 2 s
        on a home of 400,000 commits (ENG-229's review)."""
        if self._history_read:
            return self._timestamps is not None
        if self.located() is not None:
            return True
        return self.timestamps() is not None

    def _history_at(self, head: str) -> bool:
        """Whether the whole history this pass read, if it read one, was
        read at `head`: a row keyed under another head than the history's is
        neither keyed nor kept (`_keyed_pair`, `lookup`)."""
        return not self._history_read or self._history_head == head

    def located(self) -> tuple[Path, str] | None:
        """The repo root and the head: the files' where they settled them,
        from one ``git rev-parse`` otherwise, read once per pass."""
        if not self._located_read:
            self._located = (
                self.settled
                if self.settled is not None
                else repo_toplevel_and_head(self.cwd)
            )
            self._located_read = True
        return self._located

    def _keyed_pair(self, resolve: bool) -> tuple[Path, str] | None:
        """The root and the head the rows key on: the files' (`settled`),
        or where they decline, git's (`located`) where `githead.locate`
        reads the repository at that root and the same head there, under no
        variable that moves git's discovery. Read once per pass; git is not
        run for it on a row that may run none (`resolve` False) until a row
        has run it."""
        if not self._keyed_read and (resolve or self._located_read):
            self._keyed_read = True
            located = self.located()
            if (
                located is not None
                and not githead.discovery_moved()
                and located_head(located[0]) == located[1]
            ):
                self._keyed = located
        if self._keyed is not None and not self._history_at(self._keyed[1]):
            return None
        return self._keyed

    @git_read_budget.hit
    def lookup(
        self,
        record: Any,
        last_verified_at: datetime | None,
        *,
        resolve: bool = True,
        anchor_paths: tuple[str, ...] | None = None,
    ) -> tuple[bool, _Drift]:
        """One row's commit drift: whether it is answered, and the
        resolution (None where the count is omitted, `CommitDriftUnread`
        where the history was not read, with why).

        Answered without git where the signal does not apply (never
        verified, written in no repository with a remote, a record the
        pass's admission does not count here (`drift_counts`: a home the
        surface does not admit, or global), no anchor and no declared
        claim), and where the memo, or this
        pass, holds the row's key. Otherwise the row is resolved, with
        whatever git that takes, and kept for `store`; with `resolve`
        False it is left unanswered, ``(False, None)``, and no process
        runs. `last_verified_at` is the stamp the row is counted from (a
        hit's, which `attach_commit_drift_counts` reads, or the record's).
        """
        if last_verified_at is None:
            return True, None
        origin_repo = record.origin.repo if record.origin else None
        if origin_repo is None and not self.at_home:
            return True, None
        if not self.admission.drift_counts(record.id):
            return True, None
        # Claim-anchored gate: a memory with no cited/attested path
        # anchors AND no declared claims is exempt — a bare repo-wide
        # commit count carries no information about a preference or
        # lesson that merely ORIGINATED in this repo (measured 100%
        # false-positive on the dogfood store). A memory whose ONLY
        # anchors are its declared claims is fully governed — the
        # declaration is the anchor (`verify._resolve_with_claims`) —
        # so the gate is two-term, the same condition
        # `verify.compute_commit_drift` and both health rollups apply.
        # Anchor derivation and stored-claim parsing are pure CPU
        # (bounded regex; lenient parse).
        anchors = commit_drift_anchor_paths(record.body, commit_paths(record))
        if anchor_paths is not None:
            anchors = anchor_paths
        parsed_claims = load_claims(record.claims) if record.claims else []
        if not anchors and not parsed_claims:
            return True, None
        since = last_verified_at
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        key = self._key(record, anchors, parsed_claims, since, resolve)
        if key is not None:
            found, resolved = _recall_drift(key)
            if found:
                return True, resolved
            for kept in (self._resolved, self._timed_out):
                if key in kept:
                    return True, kept[key]
        if not resolve:
            return False, None
        # Kept only when every git process the resolution ran answered: a
        # fallback taken on a failure is what the uncached code serves on
        # this call and not on the next. A timeout's is what the same call
        # meets again, and is kept a while (`_DRIFT_TIMED_OUT`).
        if self._keyed is not None and self._signature is None:
            self._signature = githead.signature(self.cwd, owner=self._owned)
        failed = failed_git_calls()
        timed_out = timed_out_calls()
        located = self.located()
        resolved = resolve_hit_drift(
            cwd=self.cwd,
            toplevel=located[0] if located is not None else None,
            head=located[1] if located is not None else None,
            timestamps=self.timestamps,
            anchors=anchors,
            claims=parsed_claims,
            since=since,
            verified_head=record.verified_head,
            walked=self._walked,
        )
        if (
            key is not None
            and resolved is not CommitDriftUnread.CALL_BUDGET
            and self._history_at(key[1])
        ):
            moved = failed_git_calls() - failed
            if not moved:
                self._resolved[key] = resolved
            elif moved == timed_out_calls() - timed_out:
                self._timed_out[key] = resolved
        return True, resolved

    def _key(
        self,
        record: Any,
        anchors: tuple[str, ...],
        parsed_claims: list[Claim],
        since: datetime,
        resolve: bool,
    ) -> _DriftKey | None:
        """The row's `_DRIFT_MEMO` key, from stats of files alone (and,
        where they do not settle the root and the head, the pass's one
        ``git rev-parse``), or None where no key is settled and the row
        runs uncached.

        Keyed on every input `verify.resolve_hit_drift` reads (the head's
        history, `timestamps`, is read under the same root and head). Only
        where the root and the head are keyed (`_keyed_pair`): `store`
        reads the head again as they were read, and elsewhere the row runs
        uncached, as before the memo. A governed claim's patch stream also
        reads the attribute
        files, so its row keys on them, and runs uncached where they do
        not settle it; no other row reads them. Every row keys on the
        files beside the history its git reads: the repository's config
        and the working tree's .gitmodules (what moved a log there is
        pinned, `origin_history.AS_STORED_CONFIG`), read once per pass;
        where they cannot be read, the row runs uncached."""
        keyed = self._keyed_pair(resolve)
        if keyed is None:
            return None
        root = keyed[0]
        governed = tuple(governed_claim_paths(parsed_claims))
        attributes: tuple[object, ...] | None = None
        if governed:
            if self._repository_attributes is _UNREAD:
                self._repository_attributes = attribute_files_signature(
                    self.cwd, root, directories=self._held_directories
                )
            if governed not in self._attribute_chains:
                self._attribute_chains[governed] = gitattributes_signature(
                    root, governed, directories=self._held_directories
                )
            chain = self._attribute_chains[governed]
            if self._repository_attributes is not None and chain is not None:
                attributes = (self._repository_attributes, chain)
        if not self._walk_files_read:
            self._walk_files = walk_files_signature(
                root, directories=self._held_directories
            )
            self._walk_files_read = True
        if self._walk_files is None or (governed and attributes is None):
            return None
        return (
            str(root),
            keyed[1],
            str(self.cwd),
            anchors,
            tuple(record.claims),
            since,
            record.verified_head,
            attributes,
            self._walk_files,
        )

    @property
    def _owned(self) -> bool:
        """Whether the rows key on the files' root and head, which
        `githead`'s discovery read; else on git's, read as `locate` reads."""
        return self.settled is not None

    @property
    def unstored(self) -> int:
        """How many resolutions this pass holds that `store` has not yet
        merged into `_DRIFT_MEMO`."""
        return len(self._resolved) + len(self._timed_out)

    def store(self) -> bool:
        """Merge this pass's resolutions into `_DRIFT_MEMO`, while what
        they are keyed on reads as it did; False when the check discarded
        them, True when they were merged or there were none.

        The path-filtered logs read HEAD as they run, the patch stream the
        attribute files and the walk the config and .gitmodules, so a
        commit landing or a file changing mid-pass would otherwise store
        the next state's answer under this one's key. The signature and
        the directories' stamps also catch a change undone before the end:
        a head moved away and back, a file created and removed.

        Every comparison is with what the pass read when it opened (or
        when it first keyed a part), never with an earlier `store`'s read,
        so a pass may store more than once: each store that holds covers
        every resolution since the pass opened. Merged resolutions leave the
        pass, so a later store merges only what came after. A store that
        fails discards what it held and breaks the pass: every later store
        on it fails too, so a stamp moved back by hand never readmits a
        resolution computed across the move, and the caller opens a new
        pass to go on."""
        if not self._resolved and not self._timed_out:
            return not self._broken
        keyed = self._keyed
        if (
            not self._broken
            and keyed is not None
            and (
                toplevel_and_head_from_files(self.cwd) == keyed
                if self._owned
                else located_head(keyed[0]) == keyed[1]
            )
            and githead.signature(self.cwd, owner=self._owned) == self._signature
            and files_held(
                self.cwd,
                keyed[0],
                None
                if self._repository_attributes is _UNREAD
                else self._repository_attributes,
                self._attribute_chains,
                self._walk_files,
                self._held_directories,
            )
        ):
            _remember_drift(self._resolved, self._timed_out)
            self._resolved, self._timed_out = {}, {}
            return True
        self._resolved, self._timed_out = {}, {}
        self._broken = True
        return False


class ResponseBuilder:
    """Serializer for the tool response shapes.

    Stateless except for `stale_after_days` (a config knob). Methods
    take `now` per request so a multi-row response uses one consistent
    "now" — preventing the awkward case where the first row in a result
    set is judged fresh and the last is judged stale because we crossed
    a day boundary mid-loop.
    """

    def __init__(self, *, stale_after_days: int) -> None:
        self._stale_after_days = stale_after_days

    # ---- search / show / list rows --------------------------------------

    def hit_to_dict(
        self,
        hit: MemoryHit,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Serialise a search hit, including the structured verification block.

        `last_verified_at` stays in the response as a raw timestamp for
        callers that already branch on it; the new `verification` field
        is the structured replacement.

        `staleness_verdict` is initialised here from verification +
        path_drift only; the commit-drift contribution is folded in by
        `attach_commit_drift_counts` once the per-search timestamp list
        has been read. Initial verdict is correct for hits where commit
        drift isn't applicable (hit never verified, naming no
        repository, citing nothing a commit could move) — those verdicts
        never get revisited.

        `path_drift_checked` / `path_drift_missing` stay around as cheap
        triage counts on every hit. `path_drift` is the rich shape that
        carries the actual paths (`{checked, missing, verified}`),
        emitted only when the missing / verified / expected-absent buckets
        are not all empty — the same three-bucket GATE `memory_show`
        applies, but not the same no-signal SHAPE: `memory_show` renders a
        failing gate as an explicit `null`, this surface omits the key
        entirely (`attach_commit_drift_counts` below has the long form —
        the two are easy to conflate and have been). The lists are what
        spare the caller a memory_show round-trip, but `missing` is not
        by itself the set to act on: only its `claim_anchored_missing`
        subset (below) moves the verdict, so a hit can ship `fresh` with
        entries in `missing`, and a `spot_check_recommended` hit can
        carry prose-scraped entries beside the anchored one that
        actually escalated it.

        A non-empty route-suppressed set (`PathDriftReport`'s
        `dropped_as_route` bucket, threaded through
        `MemoryHit.path_drift_dropped_as_route_paths`) rides along
        under a `dropped_as_route` key whenever the block fires — the
        parity `memory_show` and the expanded top hit already get by
        emitting the report's serialisation wholesale. Additive like
        `expected_absent`: the key appears only when the rule actually
        suppressed something. The emit gate itself is unchanged, so a
        report whose ONLY non-empty bucket is `dropped_as_route` still
        produces no `path_drift` block here — the same route-only
        invisibility `memory_show` has (see the reach note on
        `PathDriftReport`).

        `claim_anchored_missing` folds in the same additive way and
        answers the question the counts cannot: WHICH of the missing
        paths moved `staleness_verdict`. The verdict above is computed
        from that subset alone (attested paths that vanished, citations
        anchored to the memory's own worktree); the prose-scraped
        remainder still ships in `missing` and in the
        `path_drift_missing` count, as evidence the caller can weigh
        rather than a tier the caller is told to act on.
        """
        verification = compute_verification_status(
            hit.last_verified_at, now=now, stale_after_days=self._stale_after_days
        )
        verdict = compute_staleness_verdict(
            verification=verification,
            # The CLAIM-ANCHORED count, not `hit.path_drift_missing`.
            # The full count still ships in the dict below as triage
            # detail; what escalates the tier is the attested /
            # worktree-anchored subset. `verdict_from_signals` has the
            # measurement.
            path_drift_missing=len(hit.path_drift_claim_anchored_missing_paths),
            commit_drift_count=None,
            cited_missing=len(hit.path_drift_missing_paths),
            attested_absent_present=len(hit.path_drift_present_but_attested_absent),
        )
        out: dict[str, Any] = {
            "id": hit.id,
            "scopes": hit.scopes,
            "confidence": hit.confidence.value,
            "category": hit.category.value if hit.category is not None else None,
            "snippet": hit.snippet,
            "score": hit.score,
            "relevance": hit.relevance,
            "match_terms": hit.match_terms,
            "created": isoformat(hit.created),
            "updated": isoformat(hit.updated),
            "last_verified_at": isoformat_optional(hit.last_verified_at),
            "verification": verification.to_dict(),
            "path_drift_checked": hit.path_drift_checked,
            "path_drift_missing": hit.path_drift_missing,
            "staleness_verdict": verdict,
        }
        if (
            hit.path_drift_missing_paths
            or hit.path_drift_verified_paths
            or hit.path_drift_expected_absent_paths
            or hit.path_drift_present_but_attested_absent
        ):
            out["path_drift"] = {
                "checked": list(hit.path_drift_checked_paths),
                "missing": list(hit.path_drift_missing_paths),
                "verified": list(hit.path_drift_verified_paths),
            }
            # Additive: emitted only when the memory carries an
            # absent-attestation that actually fired, so consumers
            # pinned to the three-key shape never see a surprise key.
            if hit.path_drift_expected_absent_paths:
                out["path_drift"]["expected_absent"] = list(
                    hit.path_drift_expected_absent_paths
                )
            if hit.path_drift_present_but_attested_absent:
                out["path_drift"]["present_but_attested_absent"] = list(
                    hit.path_drift_present_but_attested_absent
                )

            # Additive for the same reason: the route-suppressed set
            # folds in only when the route rule actually ate a
            # candidate. Value-parity with what `memory_show` and the
            # expanded top hit ship inside the full report — before
            # `MemoryHit` carried the bucket, the per-hit dict was the
            # one surface that silently dropped it.
            if hit.path_drift_dropped_as_route_paths:
                out["path_drift"]["dropped_as_route"] = list(
                    hit.path_drift_dropped_as_route_paths
                )
            # Additive on the same rule, and the reason it is additive
            # here while `to_dict()` emits it unconditionally: this key
            # rides EVERY hit of every search, and the bucket is empty on
            # the overwhelming majority of them (the sweep found 3
            # anchored misses against 15 prose ones). Emitting it only
            # when it fired keeps the common hit the same size it was,
            # and a present key is then itself the signal — these are the
            # paths that moved the verdict, act on these first.
            if hit.path_drift_claim_anchored_missing_paths:
                out["path_drift"]["claim_anchored_missing"] = list(
                    hit.path_drift_claim_anchored_missing_paths
                )
        return out

    def render_hit(self, row: dict[str, Any]) -> dict[str, Any]:
        """The wire shape of one search hit, built from the row the search
        assembled (`hit_to_dict` and the attach passes).

        A hit carries what choosing and acting on it needs: its id, the
        snippet, the relevance, the verdict, a reason where the verdict
        asks for a check (`check_reason`), the ids of the records that
        supersede or contradict it, how it entered the store when not by a
        write on this host (`provenance`), whether it is held out of force, the
        body when `expand_top` inlined it, and its use token. The path
        lists, the verification block, the stamps, the actor and the
        commit detail are `memory_show`'s.
        """
        if "_contradicted_by" in row:
            contradicts = list(row["_contradicted_by"])
        else:
            contradicts = [entry["id"] for entry in row.get("contradicts") or ()]
        verdict, reason = verdict_and_reason(row)
        out: dict[str, Any] = {
            "id": row["id"],
            "snippet": row["snippet"],
            "relevance": row["relevance"],
            "staleness_verdict": verdict,
        }
        if reason is not None:
            out["reason"] = reason
        if row.get("trust_unavailable"):
            out["trust_unavailable"] = True
        else:
            provenance = row.get("provenance")
            if provenance != "local":
                # None: the store holds no trust row for the record.
                out["provenance"] = provenance
        superseded_by = [entry["id"] for entry in row.get("superseded_by") or ()]
        if superseded_by:
            out["superseded_by"] = superseded_by
        if contradicts:
            out["contradicts"] = contradicts
        if row.get("held"):
            out["held"] = True
        if "body" in row:
            out["body"] = row["body"]
        if "use_token" in row:
            out["use_token"] = row["use_token"]
        return out

    def render_show(
        self,
        state: dict[str, Any],
        memory: Memory,
        path_drift: PathDriftReport,
    ) -> dict[str, Any]:
        """The wire shape of `memory_show`, built from the state the show
        assembled (`handlers.show`).

        Null fields and fields at their default are left out, and each
        path appears once: `paths` maps every path the read checked or the
        record attests or claims to what the read found (`_path_states`).
        Links are grouped by type and note (`_grouped_links`), every id
        kept. The commit leg reads as its count and basis; why a check is
        asked for is the `reason`."""
        verdict, reason = verdict_and_reason(state)
        out: dict[str, Any] = {"id": state["id"], "staleness_verdict": verdict}
        if reason is not None:
            out["reason"] = reason
        out["use_token"] = state["use_token"]
        out["scopes"] = state["scopes"]
        out["body"] = state["body"]
        out["created"] = state["created"]
        if state["updated"] != state["created"]:
            out["updated"] = state["updated"]
        if state.get("last_verified_at") is not None:
            out["last_verified_at"] = state["last_verified_at"]
        verification = state["verification"]
        out["verification"] = {
            key: verification[key]
            for key in ("status", "age_days")
            if verification.get(key) is not None
        }
        for key, default in _SHOW_DEFAULTS:
            value = state.get(key)
            if value is not None and value != default:
                out[key] = value
        if state.get("provenance") is None and not state.get("trust_unavailable"):
            # The store holds no trust row for the record.
            out["provenance"] = None
        origin = _compact_origin(state.get("origin"))
        if origin:
            out["origin"] = origin
        actor = state.get("actor")
        if isinstance(actor, dict):
            declared = {key: value for key, value in actor.items() if value is not None}
            if declared:
                out["actor"] = declared
        paths, claims = _path_states(memory, path_drift)
        if paths:
            out["paths"] = paths
            if path_drift.anchor is not None:
                out["paths_read_at"] = path_drift.anchor
        commit = state.get("commit_drift")
        if isinstance(commit, dict):
            out["commit_drift"] = {
                "commits_since_verify": commit["commits_since_verify"],
                "basis": commit["basis"],
                **(
                    {"claim_drift": commit["claim_drift"]}
                    if "claim_drift" in commit
                    else {}
                ),
            }
        for flag in ("commit_drift_unread", "commit_drift_unchecked"):
            if state.get(flag):
                out[flag] = True
        for key in ("verified_commits", "verified_versions"):
            values = getattr(memory, key)
            if values:
                out[key] = list(values)
        if memory.verified_head is not None:
            out["verified_head"] = memory.verified_head
        if claims:
            out["claims"] = claims
        if memory.corroborations:
            out["corroborations"] = memory.corroborations
            out["last_corroborated"] = isoformat_optional(memory.last_corroborated)
        for key, end in (("links", "target_id"), ("reverse_links", "source_id")):
            if state.get(key):
                out[key] = _grouped_links(state[key], end)
        for key in (
            "trust_unavailable",
            "held",
            "held_hint",
            "standing_question",
        ):
            if state.get(key):
                out[key] = state[key]
        return out

    def summary_to_dict(
        self,
        summary: MemorySummary,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Serialise a summary, the row 8.x's memory_list served, with
        verification status attached.

        Same contract as `hit_to_dict`: `now` injected for consistency,
        `verification` carries the actionable signal, raw `last_verified_at`
        kept for back-compat. Listing is the cheap-triage view, exactly the
        surface where staleness should be visible — a curator scrolling the
        list shouldn't have to call memory_show to see whether a row is
        fresh.

        A list row carries no verdict word: the list view runs no check
        (it loads no body for path drift and resolves no commit leg), and a
        verdict it did not compute would read as one it had.
        """
        verification = compute_verification_status(
            summary.last_verified_at, now=now, stale_after_days=self._stale_after_days
        )
        row: dict[str, Any] = {
            "id": summary.id,
            "scopes": summary.scopes,
            "confidence": summary.confidence.value,
            "category": summary.category.value
            if summary.category is not None
            else None,
            "summary": summary.summary,
            "created": isoformat(summary.created),
            "updated": isoformat(summary.updated),
            "last_verified_at": isoformat_optional(summary.last_verified_at),
            "verification": verification.to_dict(),
        }
        # The COMPACT actor, not `actor_to_dict`'s full shape: this is
        # the cheap-triage view, and the two declared fields the `client`
        # / `model` filters read are the whole reason a triage row wants
        # one — so a curator can see the spellings to filter on. Omitted
        # entirely when the writer declared neither, which keeps a row
        # written before 7.10.0 the same shape it has always been.
        actor = summary.actor
        if actor is not None:
            declared = {
                key: value
                for key, value in (
                    ("client", actor.client),
                    ("model", actor.model),
                )
                if value is not None
            }
            if declared:
                row["actor"] = declared
        return row

    def tombstone_summary_to_dict(self, summary: TombstonedSummary) -> dict[str, Any]:
        """Same shape as `summary_to_dict` plus removal metadata.

        Mirroring the active shape lets a curator iterate uniformly: a row
        has `removed` set if and only if it's a tombstone. `removed_session`
        is `null` for legacy tombstones written before that field shipped.
        """
        return {
            "id": summary.id,
            "scopes": summary.scopes,
            "confidence": summary.confidence.value,
            "category": summary.category.value
            if summary.category is not None
            else None,
            "summary": summary.summary,
            "created": isoformat(summary.created),
            "updated": isoformat(summary.updated),
            "last_verified_at": isoformat_optional(summary.last_verified_at),
            "removed": isoformat(summary.removed),
            "removed_reason": summary.removed_reason,
            "removed_session": summary.removed_session,
        }

    # ---- write / update / restore commit responses ----------------------

    def committed(  # type: ignore[no-untyped-def]
        self,
        memory,
        *,
        related: list[SimilarHit] | None = None,
        removed_related: list[SimilarHit] | None = None,
        warnings: list[str] | None = None,
        supersedes: list[dict[str, Any]] | None = None,
        conflicts_filed: list[dict[str, Any]] | None = None,
        hint: str | None = None,
        update_suggested: list[str] | None = None,
        extends: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Serialise a freshly-written Memory into the tool response shape.

        `supersedes` and `conflicts_filed` are the write-time supersession
        rows the handler rendered (`handlers.write.SupersessionOutcome`):
        the stored memories this record now supersedes, and the pairs
        filed for `memory_conflicts`. `hint` rides with the latter and
        names the remedy. `update_suggested` names the stored records a
        review answered as updated but no link was set on (a record that
        is not claim-sized, or write-time supersession off), and `extends`
        the ones it answered as extended, which the record now links. All
        five are omitted when empty.

        `related` carries any medium-overlap matches that didn't block the
        write — surfaced so the caller can still consider memory_update on a
        similar existing entry, just without a hard refusal.

        `removed_related` carries medium-overlap matches against tombstoned
        memories. Same shape as `related` plus the populated `removed_at` /
        `removed_reason` fields. Surfaced advisorily — the writer hasn't
        duplicated a previously-removed fact, but they're working in the
        same neighbourhood and may want to consult the removal reason.

        `warnings` is a list of canonical advisory codes — non-blocking
        flags surfaced on a successful commit. Currently:
        - ``"ambient_body_long"``: an ambient memory exceeded the
          long-body threshold; consider splitting.
        Empty list omitted from the response so the shape stays minimal
        on the common no-warning case.
        """
        out: dict[str, Any] = {
            "status": "committed",
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "category": (
                memory.category.value if memory.category is not None else None
            ),
            "created": isoformat(memory.created),
            "updated": isoformat(memory.updated),
            "last_verified_at": isoformat_optional(memory.last_verified_at),
        }
        actor = self.actor_to_dict(memory.actor)
        if actor is not None:
            out["actor"] = actor
        if related:
            self.add_similar(out, "related", related)
        if removed_related:
            self.add_similar(out, "removed_related", removed_related)
        if warnings:
            out["warnings"] = list(warnings)
        if supersedes:
            out["supersedes"] = list(supersedes)
        if conflicts_filed:
            out["conflicts_filed"] = list(conflicts_filed)
        if update_suggested:
            out["update_suggested"] = list(update_suggested)
        if extends:
            out["extends"] = list(extends)
        if hint:
            out["hint"] = hint
        return out

    def review(
        self,
        *,
        review_id: str,
        related: list[dict[str, Any]],
        questions: dict[str, Any],
        hint: str,
    ) -> dict[str, Any]:
        """The write review's first-call shape: nothing was written, and
        the caller answers the questions about the related statements by
        re-sending the same call with `review`. The answer format is in
        `hint` only (`handlers.write._review_hint`)."""
        return {
            "status": "review",
            "written": False,
            "review": {
                "review_id": review_id,
                "related": related,
                "questions": questions,
            },
            "hint": hint,
        }

    def roster(
        self,
        *,
        review_id: str,
        roster: list[str],
        question: str,
        hint: str,
    ) -> dict[str, Any]:
        """The standing roster page (ENG-112): nothing was written, and a
        standing write's caller names the roster records it touches by
        re-sending the same call with `review.named`. Each roster entry is
        one line, ``<id> (<updated>) <text>``
        (`review.RosterLine.to_line`): a line costs its text and no keys,
        on a page that lists every standing record in force."""
        return {
            "status": "review",
            "written": False,
            "review": {
                "review_id": review_id,
                "roster": roster,
                "question": question,
            },
            "hint": hint,
        }

    # ---- minor row shapes ----------------------------------------------

    def similar_to_dict(self, hit: SimilarHit) -> dict[str, Any]:
        """Serialise a SimilarHit to the tool response shape.

        `removed_at` / `removed_reason` are emitted only when populated —
        active hits keep the response shape lean by omitting the keys, while
        tombstone hits carry both. Consumers can branch on
        `"removed_reason" in hit` or on `relevance.endswith("-removed")`.
        """
        out: dict[str, Any] = {
            "id": hit.id,
            "scopes": hit.scopes,
            "confidence": hit.confidence.value,
            "snippet": hit.snippet,
            "similarity": hit.similarity,
            "relevance": hit.relevance,
            "created": isoformat(hit.created),
            "updated": isoformat(hit.updated),
        }
        if hit.removed_at is not None:
            out["removed_at"] = isoformat(hit.removed_at)
        if hit.removed_reason is not None:
            out["removed_reason"] = hit.removed_reason
        return out

    def add_similar(
        self, out: dict[str, Any], key: str, hits: Sequence[SimilarHit]
    ) -> None:
        """Set `out[key]` to the first `SIMILAR_HITS_SHOWN` hits, already
        sorted most similar first, and `out[key + "_more"]` to the count
        left out, only when some were."""
        out[key] = [self.similar_to_dict(h) for h in hits[:SIMILAR_HITS_SHOWN]]
        if len(hits) > SIMILAR_HITS_SHOWN:
            out[f"{key}_more"] = len(hits) - SIMILAR_HITS_SHOWN

    def transient_to_dict(self, hit: TransientMatch) -> dict[str, Any]:
        """Serialize a transient-marker match for the tool response."""
        return {"marker": hit.marker, "snippet": hit.snippet}

    def credential_to_dict(self, hit: CredentialMatch) -> dict[str, Any]:
        """Serialize a credential-marker match for the tool response.

        `snippet` is already redacted by the detector — every matched secret
        span is replaced with a ``[redacted:kind]`` marker — so this row is
        safe to return to the model and to write to the event log.
        """
        return {"kind": hit.kind, "snippet": hit.snippet}

    def origin_to_dict(self, origin: Origin | None) -> dict[str, Any] | None:
        """Serialize an Origin for tool responses, or None if absent.

        Empty fields are stripped so the response carries only the data that
        was actually captured at write time. A memory written before this
        feature shipped returns None; a memory written outside any git repo
        returns `{"cwd": "..."}` without `repo` or `branch` keys.
        """
        if origin is None:
            return None
        payload = origin.model_dump(mode="json", exclude_none=True)
        if not payload:
            return None
        # The labeled fallback. A record that carries a directory but no
        # `source` was captured from the server's process cwd — the only
        # channel that existed before 7.10.0, and the one the frontmatter
        # still leaves implicit so a non-declaring client's file stays
        # byte-identical. Say so here rather than leave the reader to
        # infer it: an unlabeled cwd is the silent default this field
        # exists to retire.
        payload.setdefault("source", SOURCE_PROCESS_CWD)
        for field in ("cwd", "worktree_root", "repo", "branch"):
            value = payload.get(field)
            if isinstance(value, str) and has_surrogates(value):
                payload[field] = None
                payload[field + "_bytes_base64"] = base64.b64encode(
                    os.fsencode(value)
                ).decode("ascii")
        return payload

    def actor_to_dict(self, actor: Actor | None) -> dict[str, Any] | None:
        """Serialize a record's actor for tool responses, or None when the
        record carries none.

        The FULL shape, nulls included — unlike the frontmatter, which
        emits set fields only. A reader of `memory_show` should see
        `principal: null` beside a declared `client` and know the
        difference was recorded, not merely omitted.
        """
        if actor is None or actor.is_empty():
            return None
        return actor.model_dump(mode="json")

    # ---- per-search bulk decorators -------------------------------------

    def attach_provenance(self, out: list[dict[str, Any]], *, store: Any) -> None:
        """Mutate `out` in place, adding `provenance` to each row and
        applying the trust rule (`apply_trust`) to each, from one batched
        `trust_rows` read per response: the row's verified provenance
        label (the store checks each row's pointer into the log) and this
        host's verification stamp. Read from the store, never from the
        record, so nothing a writer put in a record can supply the label.

        When the store was opened without its key nothing can be
        verified, and every row says so (`apply_trust_unavailable`).

        Call it LAST in a response's assembly: the trust rule rewrites
        `verification` and `staleness_verdict`, and a later recompute
        from the record's stamp would put a remote stamp back."""
        ids = [row["id"] for row in out if isinstance(row.get("id"), str)]
        if not ids:
            return
        rows = store.trust_rows(ids)
        if rows is None:
            for row in out:
                self.apply_trust_unavailable(row)
            return
        for row in out:
            trust = rows.get(row.get("id", ""))
            if trust is not None:
                self.apply_trust(
                    row,
                    provenance=trust.provenance,
                    verified_locally_at=trust.verified_locally_at,
                )

    def apply_trust_unavailable(
        self,
        row: dict[str, Any],
        *,
        recommendation: str = TRUST_UNAVAILABLE_RECOMMENDATION,
    ) -> None:
        """Mutate one response row for a store whose key is not on this
        machine.

        The trust rule (`apply_trust`) needs the row's verified label and
        this host's verification stamp, and the label is verified against
        the log under the store's key. Without the key nobody can say how
        a row entered, so `trust_unavailable: true` on every row, and a
        row that carries a stamp drops to `spot_check_required` with a
        recommendation naming the remedy. The record's own verification
        block stays, but the rollup a model branches on must not read a
        stamp of unknown origin as fresh. Rows with no stamp already read
        `never`. Called in place of `apply_trust`, never alongside it.
        """
        row["trust_unavailable"] = True
        raw = row.get("last_verified_at")
        if not isinstance(raw, str) or not raw:
            return
        verification = row.get("verification")
        if isinstance(verification, dict):
            verification["recommendation"] = recommendation
        row["staleness_verdict"] = "spot_check_required"

    @staticmethod
    def apply_commit_drift_unread(row: dict[str, Any], why: CommitDriftUnread) -> None:
        """Mutate one stamped row whose commit history was not read
        (`verify_basis.CommitDriftUnread`, `why`): the count stays omitted,
        but `commit_drift_unread: true` says it was not read, the verdict
        drops to `spot_check_required`, as a row whose standing cannot be
        established does (`apply_trust_unavailable`), and the
        recommendation names the cause and its remedy
        (`verify_basis.unread_recommendation`), unless the trust rule's
        names its own (a `remote` stamp, or `trust_unavailable`)."""
        row["commit_drift_unread"] = True
        row["staleness_verdict"] = "spot_check_required"
        verification = row.get("verification")
        if (
            isinstance(verification, dict)
            and verification.get("status") != "remote"
            and not row.get("trust_unavailable")
        ):
            verification["recommendation"] = unread_recommendation(why)

    @staticmethod
    def apply_commit_drift_unchecked(
        row: dict[str, Any], why: CommitLegUnchecked
    ) -> None:
        """Mutate one stamped row whose commit leg applies and that nothing
        here read (`commit_legs.CommitLegUnchecked`, ENG-229): the count
        stays omitted, `commit_drift_unchecked: true` says it was not
        checked, the verdict is never ``fresh`` (at least
        ``spot_check_recommended``), and the recommendation names the cause,
        unless the trust rule's names its own."""
        from .commit_legs import unchecked_recommendation, unchecked_verdict

        row["commit_drift_unchecked"] = True
        row["staleness_verdict"] = unchecked_verdict(row["staleness_verdict"])
        verification = row.get("verification")
        if (
            isinstance(verification, dict)
            and verification.get("status") != "remote"
            and not row.get("trust_unavailable")
        ):
            verification["recommendation"] = unchecked_recommendation(why)

    def apply_trust(
        self,
        row: dict[str, Any],
        *,
        provenance: str | None,
        verified_locally_at: str | None,
    ) -> None:
        """Mutate one response row with the index's trust facts.

        Sets `provenance` when known. Then the rule (6.6.0): a `synced`
        row whose file carries a `last_verified_at` this host never made
        (`verified_locally_at` is None) reports the remote verification
        status and `spot_check_required`, whatever the file's stamp and
        the drift legs said. The stamp arrived in the file, by `sync
        pull`, and the frontmatter cannot say who wrote it; a local
        `memory_verify` stamps the row and lifts the rule. Rows without a
        stamp already read `never`; local, untracked and unaccounted rows
        are untouched (unaccounted is flagged by the label itself).
        """
        if provenance is not None:
            row["provenance"] = provenance
        if provenance != "synced" or verified_locally_at is not None:
            return
        raw = row.get("last_verified_at")
        if not isinstance(raw, str) or not raw:
            return
        stamp = parse_event_ts(raw)
        if stamp is None:
            return
        row["verification"] = remote_verification_status(
            stamp, stale_after_days=self._stale_after_days
        ).to_dict()
        row["staleness_verdict"] = "spot_check_required"

    def attach_commit_drift_counts(  # type: ignore[no-untyped-def]
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        memories,
        *,
        caller_origin: Origin,
        admission: DriftScope | None = None,
        anchoring: Anchoring | None = None,
    ) -> None:
        """Mutate `out` in-place, adding `commit_drift_count` to each hit
        whose memory has been verified and whose commit leg applies, where
        `commit_legs.CommitLegs` reads it. `admission` is what the counts
        are read under in the caller's checkout (`admission.DriftScope`;
        the request's LINKS admission on `memory_search`): a hit whose
        home it counts there (`commit_legs.counted_at_caller`) is counted
        in the caller's checkout, with no admission none is. With
        `anchoring` every other hit is counted where health's estate check
        counts it, at its home's live view (`ClaimAnchor.commit_root`) or
        its recorded worktree, or carries `commit_drift_unchecked: true`
        where nothing here reads it (`apply_commit_drift_unchecked`,
        ENG-229); without it, such a hit carries no count, as before.

        The signal mirrors `path_drift_checked` / `path_drift_missing` in
        KIND — a cheap integer the model self-triages on, picking which hit
        to expand without a memory_show round-trip — but NOT in coverage:
        those two are unconditional in `hit_to_dict`'s return literal and
        ride every hit, whereas this one is gated (the omission list
        below).

        COST, cold, scales with the result count: do NOT add per-hit work
        here believing the loop is free. The repo root and the commit HEAD
        names come from the repository's files
        (`origin.toplevel_and_head_from_files`), with no process. Read once
        per root when a hit needs author dates, and memoised per (root,
        head): `commit_author_timestamps` (``git log --format=%at <head>``).
        Paid per hit: a `bisect_right` against the
        sorted timestamp list and `commit_drift_anchor_paths` (both pure
        CPU), and a handful of stats of the attribute files for a hit with a
        governed claim (its memo key). Once per search, about a dozen stats
        more for the files beside the history
        (`origin_gitfiles.walk_files_signature`) and the directories that hold
        the keyed files; and where a hit resolves, `githead.signature` before
        the first resolution, and the signature, the keyed files and the
        directories again at the end. Plus, for every hit that reaches the
        narrowing (count > 0 AND at least one claim anchor or declared
        claim), git work whose shape depends on the hit's BASIS. An
        author-date hit (no `verified_head` on the record, or one HEAD no
        longer descends from) forks the path-filtered
        `commit_author_timestamps_touching_pathspecs` log. A reachability
        hit forks the walk from its anchor instead (`commits_since_anchor`,
        ``git log --boundary --name-only anchor..HEAD``), memoised per
        distinct anchor, so every further hit verified at the same commit
        reads its count off the walk without a process; a reachability hit
        whose range touched none of its anchors pays the phantom
        classification log on top, the same log a quiescent hit pays. A
        dead anchor (one whose walk comes back None) is walked once per
        search, remembered for that search only, and its hits count as
        author-date hits. The cold git-process count is therefore ``1 +
        <author-date hits that fork a path-filtered log> + <distinct
        anchors walked> + <hits that needed the phantom classification>``:
        every drifting author-date hit, one walk per anchor, plus every
        quiescent (count == 0) anchored hit with at least one in-repo
        anchor — the phantom half of the applicability classification
        (`verify._quiescent_drift_applicable`; an all-escaping anchor set
        is classified without git). Bounded above by ``1 + 2 * len(hits)``
        (`max_results` caps that at 50 on the MCP surface, 30 on the
        web's), and by ``1 + len(hits)`` for an all-author-date result.
        The narrowing call buys the SAME claim-anchored narrowing
        `memory_show` and the health rollups run — a cheaper per-surface
        shortcut here is precisely how the four surfaces used to disagree
        — and the untethered gate (no anchors, no declared claims) keeps
        that shape at zero extra forks.

        WARM, the memo and its key, what a failure keeps out of it, the
        checks that keep nothing from a search during which the head or a
        keyed file moved, and what the key does not see: `_DriftPass`.
        ``tests/test_server_commit_drift.py::test_commit_drift_count_git_cost_shape``,
        its quiescent sibling and its reachability sibling pin that
        arithmetic, cold and warm; ``tests/test_commit_drift_cache.py``
        pins the keys.

        Beside the count, `commit_drift_basis` names the axis it was
        measured on (`"reachability"` / `"author-date"`), so a reader of
        a zero knows whether it could be hiding a merged branch that
        predates the stamp.

        Each further root a hit's leg resolves to away from the caller
        pays what the caller's checkout pays: its history once per search,
        and each hit there its narrowing, so a search over hits homed in
        `r` other repositories forks at most `r` more history reads.

        Omitted (key absent from the dict, not set to null) when:

        - the hit's memory names no repository (written outside any git
          working tree and stamped at no commit),
        - the hit's memory is counted nowhere here: not in the caller's
          checkout (by `admission`), and no `anchoring` was passed,
        - the hit's memory has never been verified (no anchor to count from),
        - the hit's memory makes no claims this repo's commits could
          invalidate — no cited/attested path anchors AND no declared
          claims at all, or none that resolve inside the caller's repo
          (the claim-anchored drift policy; see
          `verify.resolve_commit_drift_count`),
        - the history was not read (a timeout, an error, a history past
          the memo's budget, a shallow clone); the hit then carries
          `commit_drift_unread: true` (`apply_commit_drift_unread`),
        - nothing here read the leg (its home checkout has no live view
          here and the caller's checkout does not hold the cited commit,
          no history applies where it resolves, the recorded worktree is
          gone); the hit then carries
          `commit_drift_unchecked: true` (`apply_commit_drift_unchecked`).

        Absence-as-signal IS the right analogy to `path_drift` here — but
        only once the surface is named, because `path_drift` answers to two
        opposite conventions and this docstring gets to claim only one of
        them. The dicts decorated here are `hit_to_dict` output (this
        method hard-requires that shape: it reads `hit_dict["verification"]`
        unguarded below), and on THAT shape `path_drift` is itself gated —
        on its own condition, but with the same absent-not-null result: a
        hit with nothing to report carries no `path_drift` key at all, never
        a null one. So omitting the count keeps the hit uniform: a consumer
        either sees a field or doesn't, no third "unknown" branch to
        filter. `memory_show` spends
        the same two names on the OPPOSITE convention — `path_drift` /
        `commit_drift` always emitted, `null` as the no-signal value, as
        `handlers.show._links_payload` spells out. Both statements are
        accurate about their own surface; don't reconcile this paragraph
        into the other's wording.
        """
        from .commit_legs import CommitLegs

        # The repo root and the head, from the repository's files (the
        # pass reads them as it opens). They key the whole-history log and
        # the per-hit memo; where the files do not settle them, git's
        # answer does, where `githead.locate` reads the same (`_keyed_pair`).
        # Resolve a caller's history only when a hit counts there. Reading
        # it before the scheduled hits would let one slow caller consume
        # every call's budget before any deferred home receives a turn.
        # The root and head still resolve once per pass, and a caller
        # where no history applies still falls back to the record's home.
        drift = CommitLegs(caller_origin, admission, anchoring)
        if not drift.active:
            return
        drift.prime(h.id for h in hits)
        _count_hits(out, hits, memories, drift)
        drift.store()

    def attach_link_annotations(  # type: ignore[no-untyped-def]
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        memories,
        *,
        store: Any,
        admission: Admission | None = None,
        excluded_scopes: set[str] | None = None,
        max_per_hit: int = 3,
        max_total: int = 10,
    ) -> dict[str, Any]:
        """Mutate `out` in-place, surfacing supersedes / contradicts edges as
        trust signals on each hit, and return the link rows read for the
        hits, which `contradicting_ids` reuses.

        `MemoryLink` has carried `supersedes` and `contradicts` since 2.x,
        but retrieval never acted on them — setting the link changed nothing
        the searcher saw. This activates them, purely additively (it never
        reorders or drops a hit — annotation only):

        - `superseded_by`: ACTIVE memories that supersede this hit (inbound
          `supersedes` edges). Per the LinkType contract the consumer should
          prefer the superseding memory; surfacing it lets the model do so.
        - `contradicts`: memories in unresolved contradiction with this hit,
          either direction (the relation is symmetric per the LinkType
          docstring — both surface, reconcile, typically via memory_verify).

        Each entry: `{id, scopes, summary, link_note}`. A key is omitted
        (absent, not null) when empty — the same absence-as-signal contract
        as `depends_on_resolved`. Bounded by `max_per_hit` / `max_total`.

        Edges come from the store's canonical `memory_links` table. A
        successful answer, including an empty one, wins over the older
        candidate snapshot. If the bulk query raises, the edges are rebuilt
        from the records' own links (`candidate_link_rows`): the loaded
        candidates' and those of the records whose rows name a hit
        (`Store.records_linking_to`), so an inbound source the query did
        not draw is still found; when that read fails too, the candidates
        alone are read. Resolution
        reuses the `depends_on` discipline: a side-map over the candidates, a
        targeted `store.load_one` for targets outside the search's pool,
        tombstoned / missing skipped silently, and the caller's scope filter
        and LINKS admission (`admission`, design 3.5) re-applied so a link
        can't leak a hidden-scope memory or one the caller is not admitted
        to; an omitted `admission` admits every target.
        """
        from .models import first_summary_line
        from .store import MemoryNotFoundError, TombstonedError

        memory_by_id = {m.id: m for m in memories}
        excluded = excluded_scopes or set()

        def _resolve(other_id: str, note: str | None) -> dict[str, Any] | None:
            # Admission is read before any load, so a record the caller is
            # not admitted to is never read on its behalf.
            if admission is not None and not admission.admits_id(other_id):
                return None
            target = memory_by_id.get(other_id)
            if target is None:
                try:
                    target = store.load_one(other_id)
                except (MemoryNotFoundError, TombstonedError, OSError, sqlite3.Error):
                    return None
                memory_by_id[other_id] = target
            if excluded and (set(target.scopes) & excluded):
                return None
            return {
                "id": target.id,
                "scopes": list(target.scopes),
                "summary": first_summary_line(target.body),
                "link_note": note,
            }

        # One read for all hits: the store's link rows, both directions,
        # in two IN (...) queries. Best-effort all the way down: a failure
        # degrades to no annotations, never a broken search.
        hit_ids = [h.id for h in hits]
        try:
            links_map = store.links_for_many(hit_ids)
        except Exception:  # noqa: BLE001 - the annotation is best-effort
            # The links table could not be read. The records carry their
            # own links: those that name a hit are read from their rows,
            # beside the candidates, and the link rows are rebuilt from
            # them. When that read fails too, from the candidates alone.
            try:
                for source in store.records_linking_to(
                    hit_ids, admit=_admitted_ids(admission)
                ):
                    memory_by_id.setdefault(source.id, source)
            except Exception:  # noqa: BLE001 - the annotation is best-effort
                pass
            links_map = candidate_link_rows(memory_by_id.values(), hit_ids)
        if admission is not None:
            admission.prime(
                oid
                for outbound, inbound in links_map.values()
                for _t, oid, _note in [*outbound, *inbound]
            )

        total = 0
        for hit_dict, hit in zip(out, hits):
            if total >= max_total:
                break
            outbound, inbound = links_map.get(hit.id, ([], []))
            if not outbound and not inbound:
                continue

            superseded_by = [
                (oid, note) for (t, oid, note) in inbound if t == "supersedes"
            ]
            # Contradiction is symmetric: collect both directions, dedup by
            # the other memory's id (a bidirectional pair links twice).
            contradicts: list[tuple[str, str | None]] = []
            seen_c: set[str] = set()
            for t, oid, note in list(outbound) + list(inbound):
                if t == "contradicts" and oid != hit.id and oid not in seen_c:
                    seen_c.add(oid)
                    contradicts.append((oid, note))

            emitted = 0
            for key, pairs in (
                ("superseded_by", superseded_by),
                ("contradicts", contradicts),
            ):
                resolved: list[dict[str, Any]] = []
                for oid, note in pairs:
                    if emitted >= max_per_hit or total >= max_total:
                        break
                    entry = _resolve(oid, note)
                    if entry is not None:
                        resolved.append(entry)
                        emitted += 1
                        total += 1
                if resolved:
                    hit_dict[key] = resolved
        return dict(links_map)

    def attach_recent_negative_outcomes(
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        events: list[dict[str, Any]],
        *,
        now: datetime,
        window_days: int = NEGATIVE_OUTCOME_WINDOW_DAYS,
    ) -> None:
        """Mutate `out` in-place, adding `recent_negative_outcomes` to any
        hit whose memory has been ignored or contradicted within the
        window AND not since been applied.

        Negative outcomes that have been superseded by a later applied
        event are filtered out — the user already validated the memory
        after the rejection, surfacing the rejection would be misleading.
        Only "ignored" and "contradicted" count as negative; "corrected"
        is audit-only (the model fixed and moved on); "applied" is the
        positive case.

        Shape per hit (when present):

            "recent_negative_outcomes": [
                {
                    "outcome": "ignored" | "contradicted",
                    "most_recent_ts": "...",
                    "count_in_window": N,
                    "session_id": "...",
                    "note": str | None,
                    "claim_excerpt": str | None,
                }
            ]

        Keyed by `outcome` so each hit gets at most one entry per outcome
        type (so 2 entries max — one ignored, one contradicted). The
        `claim_excerpt` field (T1.1) is the load-bearing claim recorded
        at rejection time, when present — it tells the model not just
        "this was rejected" but "*this specific claim* was rejected",
        which it can use to rephrase or skip the body's bad sentence.

        The field is OMITTED (key absent from the dict, not null) when
        no qualifying negative outcomes exist — same absence-as-signal
        contract as `commit_drift_count`.
        """
        if not out or not hits or not events:
            return

        hit_ids = {hit.id for hit in hits}
        cutoff = now.timestamp() - window_days * 86400

        # Walk events once. Build per-id timelines of use events filtered
        # to the window and to the hit ids — we don't care about events
        # for memories that aren't in this result set.
        per_id_events: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("kind") != "use":
                continue
            ts_str = event.get("ts")
            if not isinstance(ts_str, str):
                continue
            # Canonical parse: tz-aware UTC, or None on malformed input.
            # An offset-less event ts (e.g. a hand-written or legacy
            # "2026-05-31T12:00:00") would otherwise come back naive, and
            # then (a) `ts.timestamp()` below would read the wall-clock in
            # the host's LOCAL zone — silently mis-windowing the event by
            # the local UTC offset — and (b) the `timeline.sort(...)` further
            # down would mix naive and tz-aware datetimes and raise
            # `TypeError`. `parse_event_ts` stamps UTC on offset-less input
            # so both the window math and the sort stay correct everywhere.
            ts = parse_event_ts(ts_str)
            if ts is None or ts.timestamp() < cutoff:
                continue
            # Legacy fallback for `memory_ids` — same class as the
            # 70e41a4 llm.py fix. Old `use` archives have `memory_ids`.
            #
            # Never iterate the raw field: this attach runs on every
            # hit-producing memory_search with NO flag gate, so one
            # scalar / nested-list `ids` in the plaintext event log was
            # a full retrieval outage until rotation. `_event_id_items`
            # (the shared normalizer) preserves each id's ORIGINAL index
            # so `claim_excerpts` — recorded parallel to the raw list —
            # still attributes to the right slot when malformed elements
            # are dropped.
            seen_ids: set[str] = set()
            for i, mid in _event_id_items(event.get("ids") or event.get("memory_ids")):
                if mid not in hit_ids or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                # A row written before 9.0.0rc18 can hold a token in its
                # note or excerpt, read here as its bare name (ENG-189).
                per_id_events.setdefault(mid, []).append(
                    {
                        "ts": ts,
                        "outcome": event.get("outcome"),
                        "auto": bool(event.get("auto")),
                        "session": event.get("session") or event.get("session_id"),
                        "note": named_tokens(event.get("note")),
                        "claim_excerpt": named_tokens(_claim_at_index(event, i)),
                    }
                )

        for hit_dict, hit in zip(out, hits):
            timeline = per_id_events.get(hit.id)
            if not timeline:
                continue
            timeline.sort(key=lambda e: e["ts"])

            # Walk timeline chronologically. A GENUINE applied event after a
            # negative supersedes it: the user/model validated the memory
            # after rejecting it earlier, so the rejection no longer tells us
            # anything actionable. But the auto-`record_use` fallback emits
            # outcome="applied" with no model/user judgment — it fires merely
            # because a re-surfaced use-token was never explicitly settled. An
            # auto-apply must NOT clear a contradiction, or a memory the model
            # explicitly flagged as wrong would lose its warning the next time
            # it's retrieved. Only a non-auto applied event supersedes.
            ignored_active: list[dict[str, Any]] = []
            contradicted_active: list[dict[str, Any]] = []
            for entry in timeline:
                outcome = entry["outcome"]
                if outcome == "applied" and not entry.get("auto"):
                    ignored_active.clear()
                    contradicted_active.clear()
                elif outcome == "ignored":
                    ignored_active.append(entry)
                elif outcome == "contradicted":
                    contradicted_active.append(entry)
                # "corrected" is audit-only; skip.

            entries: list[dict[str, Any]] = []
            for bucket, outcome_label in (
                (ignored_active, "ignored"),
                (contradicted_active, "contradicted"),
            ):
                if not bucket:
                    continue
                most_recent = bucket[-1]
                entries.append(
                    {
                        "outcome": outcome_label,
                        "most_recent_ts": isoformat(most_recent["ts"]),
                        "count_in_window": len(bucket),
                        "session_id": most_recent.get("session"),
                        "note": most_recent.get("note"),
                        "claim_excerpt": most_recent.get("claim_excerpt"),
                    }
                )

            if entries:
                hit_dict["recent_negative_outcomes"] = entries


def _admitted_ids(
    admission: Admission | None,
) -> Callable[[list[str]], list[str]] | None:
    """The filter a link fallback runs over candidate ids before it loads
    them: the ids `admission` admits, read before any record is."""
    if admission is None:
        return None
    gate = admission

    def admit(ids: list[str]) -> list[str]:
        gate.prime(ids)
        return [record_id for record_id in ids if gate.admits_id(record_id)]

    return admit


def contradicting_ids(
    store: Any,
    ids: Sequence[str],
    *,
    admission: Admission | None = None,
    excluded_scopes: frozenset[str] | set[str] = frozenset(),
    links_map: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """The records each of `ids` is in a `contradicts` link with, in either
    direction, by id: uncapped, active (a removed record counts for
    nothing), outside `excluded_scopes` (the session's disabled scopes),
    and admitted by `admission` (an omitted admission admits every
    record). Both surfaces read a record's contradictions here
    (`verdict_and_reason`).

    Edges come from `links_map` when the caller already read the link rows
    for `ids` (a search's `attach_link_annotations`), else from the links
    table, else from the records' own links and those of the records whose
    links name them (`Store.records_linking_to`,
    `_response_links.candidate_link_rows`). Whether a candidate is active
    and in scope is read for all of them in one batched query
    (`Store.scopes_for`), after admission and without loading a record, so
    the cost does not grow with the number of links."""
    from .store import MemoryNotFoundError, TombstonedError

    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    primed = links_map is not None
    edges: dict[str, Any]
    if links_map is not None:
        edges = links_map
    else:
        try:
            edges = store.links_for_many(wanted)
        except Exception:  # noqa: BLE001 - the fallback reads the records instead
            records: list[Any] = []
            for record_id in wanted:
                try:
                    records.append(store.load_one(record_id))
                except (MemoryNotFoundError, TombstonedError, OSError, sqlite3.Error):
                    continue
            try:
                records.extend(
                    store.records_linking_to(wanted, admit=_admitted_ids(admission))
                )
            except Exception:  # noqa: BLE001 - the records' own links remain
                pass
            edges = candidate_link_rows(records, wanted)
    others = {
        record_id: list(
            dict.fromkeys(
                other
                for kind, other, _note in [*outbound, *inbound]
                if kind == "contradicts" and other != record_id
            )
        )
        for record_id, (outbound, inbound) in (
            (rid, edges.get(rid, ([], []))) for rid in wanted
        )
    }
    candidates = list(dict.fromkeys(oid for found in others.values() for oid in found))
    if not candidates:
        return {}
    if admission is not None:
        if not primed:
            admission.prime(candidates)
        candidates = [oid for oid in candidates if admission.admits_id(oid)]
    try:
        scopes = store.scopes_for(candidates) if candidates else {}
    except (OSError, sqlite3.Error):
        scopes = {}
    excluded = set(excluded_scopes)
    readable = {oid for oid, tags in scopes.items() if not (set(tags) & excluded)}
    return {
        record_id: kept
        for record_id, found in others.items()
        if (kept := [other for other in found if other in readable])
    }


def recent_rejections(
    store: Any, ids: Sequence[str], *, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """The uses recorded through memory_record_use that rejected each of
    `ids` (ignored or contradicted) within `NEGATIVE_OUTCOME_WINDOW_DAYS`
    and were not followed by an applied use, by id
    (`ResponseBuilder.attach_recent_negative_outcomes`'s entries).

    Two reads, as the search makes them: the window's negative rows
    (`Store.negative_uses`), and only when one names an id, the window's
    ``use`` rows that name it (`Store.events_of_kinds`), which say whether
    it was applied since. Ids nothing rejected read no ``use`` row."""
    from datetime import timedelta
    from types import SimpleNamespace

    since = now - timedelta(days=NEGATIVE_OUTCOME_WINDOW_DAYS)
    wanted = set(ids)
    rejected: set[str] = set()
    for event in store.negative_uses(since):
        named = event.get("ids") or event.get("memory_ids")
        rejected.update(mid for _, mid in _event_id_items(named) if mid in wanted)
    if not rejected:
        return {}
    events = list(store.events_of_kinds(("use",), since=since, naming=sorted(rejected)))
    rows: list[dict[str, Any]] = [{"id": mid} for mid in sorted(rejected)]
    ResponseBuilder(stale_after_days=0).attach_recent_negative_outcomes(
        rows,
        [SimpleNamespace(id=row["id"]) for row in rows],  # type: ignore[misc]
        events,
        now=now,
    )
    return {
        row["id"]: row["recent_negative_outcomes"]
        for row in rows
        if row.get("recent_negative_outcomes")
    }


def _rejection_note(entries: Sequence[dict[str, Any]]) -> str:
    """One sentence per outcome: what the user did, how often, and when."""
    parts = []
    for entry in entries:
        count = entry["count_in_window"]
        times = (
            ""
            if count == 1
            else f" {count} times in the last {NEGATIVE_OUTCOME_WINDOW_DAYS} days, last"
        )
        day = str(entry["most_recent_ts"])[:10]
        parts.append(f"A use marked it {entry['outcome']}{times} on {day}.")
    parts.append("It clears once a later use applies it.")
    return " ".join(parts)


#: The paths a reason names before it counts the rest.
_REASON_PATHS = 3

#: `memory_show` fields left out at these values: the write defaults, and
#: the label of a record entered on this host.
_SHOW_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("confidence", "medium"),
    ("source", "explicit-statement"),
    ("category", "fact"),
    ("provenance", "local"),
)


def _named(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_REASON_PATHS])
    more = len(items) - _REASON_PATHS
    return f"{shown} and {more} more" if more > 0 else shown


def check_reason(
    *,
    missing: Sequence[str] = (),
    anchored_missing: Sequence[str] = (),
    attested_absent_present: Sequence[str] = (),
    commits: int | None = None,
    claims_drifted: Sequence[str] = (),
    leg_note: str | None = None,
    remote: bool = False,
    trust_unavailable: bool = False,
    untrusted: str | None = None,
    contradicted_by: Sequence[str] = (),
    rejections: Sequence[dict[str, Any]] = (),
) -> str | None:
    """Why a row asks for a check, naming what to check: the missing
    paths, the paths attested absent that are present, the commits since the stamp and the claims they touch, a
    commit leg that could not be read and why, a stamp this host did not
    make, the records a link says it contradicts, and the uses that
    rejected it lately. None when there is nothing a check could act on."""
    parts: list[str] = []
    anchored = [path for path in missing if path in set(anchored_missing)]
    anchored += [path for path in anchored_missing if path not in anchored]
    prose = [path for path in missing if path not in set(anchored)]
    if anchored:
        parts.append(f"Attested or anchored path missing: {_named(anchored)}.")
    if prose:
        parts.append(f"Cited path missing: {_named(prose)}.")
    if attested_absent_present:
        parts.append(
            f"Attested absent but present: {_named(list(attested_absent_present))}."
        )
    if commits:
        plural = "" if commits == 1 else "s"
        if claims_drifted:
            parts.append(
                f"{commits} commit{plural} since the last verify touched the "
                f"claims {_named(list(claims_drifted))}."
            )
        else:
            parts.append(
                f"{commits} commit{plural} since the last verify touched the "
                "paths it cites."
            )
    if leg_note:
        parts.append(leg_note)
    if remote:
        parts.append("Its verification stamp was made on another host.")
    if trust_unavailable:
        parts.append(
            TRUST_UNAVAILABLE_RECOMMENDATION
            + " Nothing in these tools clears it; the store's owner restores the key."
        )
    if untrusted:
        parts.append(untrusted)
    if contradicted_by:
        parts.append(
            f"A recorded link says it contradicts {_named(contradicted_by)}: read "
            "it, then fix whichever is wrong or remove the link; memory_verify "
            "does not clear it."
        )
    if rejections:
        parts.append(_rejection_note(rejections))
    return " ".join(parts) or None


#: Why a record whose trust row says it was not written through this
#: host's log asks for a check, by its provenance label.
_UNTRUSTED_PROVENANCE = {
    "unaccounted": (
        "It entered the store without a log record on this host, so nothing "
        "says who wrote it; memory_verify does not clear that. Remove it, or "
        "restore it after a memory_remove if you recognise it."
    ),
    "synced": (
        "It arrived by sync and has not been verified on this host; "
        "memory_verify here clears that."
    ),
}


def _untrusted(row: dict[str, Any]) -> str | None:
    """The trust objection of a row: an unaccounted record, or a synced one
    this host never verified (a synced stamp from another host reads
    ``remote`` already)."""
    provenance = row.get("provenance")
    status = (row.get("verification") or {}).get("status")
    if provenance == "unaccounted":
        return _UNTRUSTED_PROVENANCE["unaccounted"]
    if provenance == "synced" and status == "never":
        return _UNTRUSTED_PROVENANCE["synced"]
    return None


def verdict_and_reason(row: dict[str, Any]) -> tuple[str, str | None]:
    """The verdict and reason the caller gets for an assembled hit or show
    row: the row's own verdict, raised by what on record objects to the
    record (`verdict.with_objection`): a contradiction among the
    admitted, active records (`contradicting_ids`, kept on the row as
    `_contradicted_by`), a recent rejection, a store opened without its
    key, or a trust row that says the record did not come through this
    host's log (`_untrusted`). One function for both surfaces, so they
    cannot read one record two ways."""
    from .verdict import with_objection

    contradicted_by = row.get("_contradicted_by") or ()
    untrusted = _untrusted(row)
    objected = bool(
        contradicted_by
        or row.get("recent_negative_outcomes")
        or row.get("trust_unavailable")
        or untrusted
    )
    verdict = with_objection(row["staleness_verdict"], objected)
    status = (row.get("verification") or {}).get("status")
    if row.get("provenance") == "unaccounted" and status not in (None, "never"):
        # A stamp on a row this host's log does not account for may be
        # planted: the least trustworthy stamp there is.
        verdict = "spot_check_required"
    if verdict == "fresh":
        return verdict, None
    return verdict, _row_reason(row, contradicted_by, untrusted)


def _row_reason(
    row: dict[str, Any], contradicted_by: Sequence[str], untrusted: str | None
) -> str | None:
    """`check_reason` over an assembled hit or show row."""
    drift = row.get("path_drift") or {}
    commit = row.get("commit_drift")
    if isinstance(commit, dict):
        commits = commit.get("commits_since_verify")
        claims_drifted = (commit.get("claim_drift") or {}).get("drifted") or ()
    else:
        commits = row.get("commit_drift_count")
        claims_drifted = (row.get("claim_drift") or {}).get("drifted") or ()
    verification = row.get("verification") or {}
    remote = verification.get("status") == "remote"
    trust_unavailable = bool(row.get("trust_unavailable"))
    leg_note: str | None = None
    if row.get("commit_drift_unread") or row.get("commit_drift_unchecked"):
        if not remote and not trust_unavailable:
            leg_note = verification.get("recommendation")
        else:
            # The verification block carries the trust remedy here, so the
            # leg is named on its own.
            leg_note = (
                "The commits since its stamp were not read."
                if row.get("commit_drift_unread")
                else "No checkout here can read the commits since its stamp."
            )
    return check_reason(
        missing=drift.get("missing") or (),
        anchored_missing=drift.get("claim_anchored_missing") or (),
        attested_absent_present=drift.get("present_but_attested_absent") or (),
        commits=commits if isinstance(commits, int) else None,
        claims_drifted=claims_drifted,
        leg_note=leg_note,
        remote=remote,
        trust_unavailable=trust_unavailable,
        untrusted=untrusted,
        contradicted_by=contradicted_by,
        rejections=row.get("recent_negative_outcomes") or (),
    )


#: The platform's separator when it is not ``/`` (Windows), else None.
_NATIVE_SEP: str | None = os.sep if os.sep != "/" else None


def _slashed(path: str) -> str:
    """`path` with the platform's own separator written as ``/``. A
    backslash is a separator only where the platform says so; on POSIX it
    is part of a name and stays."""
    return path if _NATIVE_SEP is None else path.replace(_NATIVE_SEP, "/")


def _path_states(
    memory: Memory, drift: PathDriftReport
) -> tuple[dict[str, str], list[str]]:
    """Every path the read checked, the record attests or its claims name,
    once, with what the read found; and the claims that name more than a
    path (a symbol or a literal), which the map cannot carry."""
    from .claims import parse_claim

    states: dict[str, list[str]] = {}

    def add(path: str, finding: str) -> None:
        found = states.setdefault(path, [])
        if finding not in found:
            found.append(finding)

    anchored = set(drift.claim_anchored_missing)
    for path in drift.missing:
        add(path, "missing, attested or anchored" if path in anchored else "missing")
    for path in drift.claim_anchored_missing:
        add(path, "missing, attested or anchored")
    for path in drift.verified:
        add(path, "present, attested")
    for path in drift.expected_absent:
        add(path, "absent, as attested")
    for path in drift.present_but_attested_absent:
        add(path, "present, attested absent")
    for path, why in zip(drift.unconfirmed, drift.unconfirmed_reasons, strict=True):
        add(path, f"unconfirmed: {why}")
    for path in drift.dropped_as_route:
        add(path, "read as a route, not checked")
    for path in drift.checked:
        if path not in states:
            add(path, "present")

    def spelled(path: str) -> str:
        """The key the read already holds for an attested or claimed
        spelling (the read resolves a relative one against the record's
        root, joined with the platform's separator), else the spelling
        itself."""
        if path in states:
            return path
        tail = "/" + _slashed(path).lstrip("./")
        return next((key for key in states if _slashed(key).endswith(tail)), path)

    for path in memory.verified_paths:
        key = spelled(path)
        if key not in states:
            add(key, "attested")
    for path in memory.verified_absent_paths:
        key = spelled(path)
        if key not in states:
            add(key, "attested absent")
    for targets in (memory.verified_path_targets, memory.verified_absent_path_targets):
        for spelling, target in (targets or {}).items():
            if target is not None and target != spelling:
                add(spelled(spelling), f"target {target}")
    claims: list[str] = []
    for raw in memory.claims:
        try:
            claim = parse_claim(raw)
        except ValueError:
            claims.append(raw)
            continue
        if claim.kind == "path":
            add(spelled(claim.rel_path), "claimed present")
        elif claim.kind == "absent":
            add(spelled(claim.rel_path), "claimed absent")
        else:
            claims.append(raw)
    return {path: "; ".join(found) for path, found in states.items()}, claims


def _compact_origin(origin: Any) -> dict[str, Any] | None:
    """`origin_to_dict`'s shape without a `cwd` that repeats the worktree
    root, in either spelling: the text, or the native bytes
    (`cwd_bytes_base64`) of a directory UTF-8 cannot represent."""
    if not isinstance(origin, dict):
        return None
    out = {key: value for key, value in origin.items() if value is not None}
    for cwd, root in (
        ("cwd", "worktree_root"),
        ("cwd_bytes_base64", "worktree_root_bytes_base64"),
    ):
        if cwd in out and out[cwd] == out.get(root):
            del out[cwd]
    return out or None


def _grouped_links(entries: list[dict[str, Any]], end: str) -> list[dict[str, Any]]:
    """Links grouped by type and note, in first-seen order, every id kept:
    `{type, ids, note?}`."""
    groups: dict[tuple[str, str | None], list[str]] = {}
    for entry in entries:
        groups.setdefault((entry["type"], entry.get("note")), []).append(entry[end])
    return [
        {"type": kind, "ids": ids, **({"note": note} if note is not None else {})}
        for (kind, note), ids in groups.items()
    ]


def _stamp_hit_drift(
    hit_dict: dict[str, Any],
    hit: MemoryHit,
    resolved: ResolvedCommitDrift | CommitDriftUnread | CommitLegUnchecked,
) -> None:
    """Take one hit and its resolved drift; stamp `hit_dict` with them, return None."""
    if isinstance(resolved, CommitDriftUnread):
        ResponseBuilder.apply_commit_drift_unread(hit_dict, resolved)
        return
    if not isinstance(resolved, ResolvedCommitDrift):
        ResponseBuilder.apply_commit_drift_unchecked(hit_dict, resolved)
        return
    # Additive, claim-carrying hits only; mirrors
    # `CommitDriftStatus.to_dict`'s claim_drift block so both
    # display surfaces speak the same sub-dict. Built fresh per
    # hit: the resolution it is read from may be shared.
    if resolved.claims_checked:
        hit_dict["claim_drift"] = {
            "checked": resolved.claims_checked,
            "drifted": list(resolved.claims_drifted),
        }
    count = resolved.count
    hit_dict["commit_drift_count"] = count
    hit_dict["commit_drift_basis"] = resolved.basis
    # Recompute the verdict now that we have the commit-drift
    # contribution. `hit_to_dict` initialised it without that
    # input; the upgrade only fires for hits where the count was
    # actually applicable.
    verification_dict = hit_dict["verification"]
    verification_status = verification_dict["status"]
    # Call the shared primitive rather than restating the
    # ladder. This site used to re-implement it against the
    # same two module-level constants, guarded only by a pair
    # of "mirror the gate in verify.py" comments — which is
    # exactly the arrangement that let a semantic change reach
    # one surface and not the other, so `memory_search`'s top
    # hit could report a different verdict than `memory_show`
    # for the same memory. `count` is a real measured integer
    # here (`attach_commit_drift_counts` skips a `resolved` of
    # None before calling this), so the stale-demotion arm gets
    # the input it needs rather than a None that would read as
    # "couldn't ask".
    hit_dict["staleness_verdict"] = verdict_from_signals(
        status=verification_status,
        # Read off the HIT, not the dict. The dict's
        # `path_drift_missing` is the full-set triage count and
        # would re-broaden the escalation this recompute is
        # supposed to mirror; the claim-anchored subset only
        # reaches the dict when it is non-empty (additive key),
        # so the hit is the one source that always has it. The
        # caller's zip already pairs them.
        path_drift_missing=len(hit.path_drift_claim_anchored_missing_paths),
        commit_drift_count=count,
        cited_missing=len(hit.path_drift_missing_paths),
        attested_absent_present=len(hit.path_drift_present_but_attested_absent),
    )


def _claim_at_index(event: dict[str, Any], index: int) -> str | None:
    """Pluck the `claim_excerpts[index]` entry from a use event if the
    event carries the field (T1.1). Returns None when claim_excerpts
    is absent or the index is out of range — both legitimate states
    for older events written before T1.1 landed."""
    excerpts = event.get("claim_excerpts")
    if not isinstance(excerpts, list) or index >= len(excerpts):
        return None
    value = excerpts[index]
    return value if isinstance(value, str) else None
