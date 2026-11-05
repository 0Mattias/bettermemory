"""Working-context capture for memory_write — what cwd / repo / branch the
write happened in.

Captured at write time, never at retrieval. The cwd of a long-lived MCP
server can drift; capturing at retrieval would mean a memory's origin
silently changes meaning. Capture-at-write makes origin a durable
property of the memory record.

Used by `memory_search(auto_scope=True)` to default-filter results by the
caller's current repo. The failure mode this addresses is
cross-project leakage: a memory written while working on Project A
surfacing in Project B's conversation. Origin metadata + the auto-scope
filter close that hole without forcing the model to manually tag every
write with `projects:foo`.

Existing memories on disk have no `origin` field. They're treated as
"global" — they pass the auto-scope filter regardless of the caller's
current repo, because we have no evidence of a project boundary. The
file format is additive only.

**Auto-scope is a UX filter, not access control.** It governs the
*defaults* of `memory_search` and `memory_scope_overview` so the
model's first-look surface stays focused on the current project. It
does NOT gate `memory_show(id)`, which serves any active id verbatim
regardless of the caller's repo. That asymmetry is intentional: if
the model already has an id (from a cross-project search with
`auto_scope=False`, from a previous conversation, or from the user
pasting one in), retrieval should work. The threat model here is
"don't surface irrelevant memories by accident", not "prevent
information flow across project boundaries". For real isolation,
use separate stores via the project-scoped resolution rule
(`./.claude-memory/`) or the `BETTERMEMORY_DIR` env var.

The repository files read without a git process (the config reader, the
index's ``.gitmodules`` entry, the drift memos' file signatures) live in
`origin_gitfiles`, the remote URL parser in `origin_remote` and the
`Origin` model in `origin_model`; this module re-exports every name of
theirs a caller reads.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
import dataclasses
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias

from . import _git as _git_runner
from . import _caches, githead, gitfile_rule, identity, origin_history
from ._location_probe import path_gone
from .location_git import (  # the config the cache is kept against
    legacy_remote_stamps,
    machine_config_reads,
    machine_config_stamps,
    repository_config_stamps,
)

# The repository files read in-process live in `origin_gitfiles`, which
# imports nothing from here. The plain imports are `__all__`'s names and
# the private names only this module reads; the `name as name` form marks
# a private name a test reads, which the type checkers would otherwise see
# as not exported. Five of those (`_INDEX_UNMERGED`,
# `_config_variable_names`, `_global_attributes_path`, `_hold_directory`,
# `_index_gitmodules`) have no reader left in this module: a test that
# patches one patches `origin_gitfiles`, where the readers are.
from .origin_gitfiles import (
    _config_moves_worktree,
    _machine_config_dies,
    _shortening_refs,
    _unquote_c_path,
    attribute_files_signature,
    capture_files,
    directories_held,
    files_held,
    first_not_a_directory,
    gitattributes_signature,
    located_head,
    refused_worktree_root,
    relative_to_root,
    walk_files_signature,
)
from .origin_gitfiles import (
    _INDEX_UNMERGED as _INDEX_UNMERGED,
    _config_variable_names as _config_variable_names,
    _dequote_patch_headers as _dequote_patch_headers,
    _global_attributes_path as _global_attributes_path,
    _hold_directory as _hold_directory,
    _index_gitmodules as _index_gitmodules,
    _spelled_as_git as _spelled_as_git,
)

# The reachable walk's type lives in `origin_history`, beside the history
# reads, to keep this module under the 2,000-line bound.
from .origin_history import ReachableWalk as ReachableWalk
from .origin_history import Walked as Walked

# The model lives in `origin_model`, which imports nothing from here.
from .origin_model import Origin as Origin

# The remote URL parser lives in `origin_remote`, which imports nothing
# from here; `_parse_remote` stays importable from here for the tests.
from .origin_remote import _parse_remote as _parse_remote

log = logging.getLogger("bettermemory.origin")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(cwd: Path | None = None, *, source: str | None = None) -> Origin:
    """Snapshot the current working context.

    `cwd` is parameterized for testability — production usage passes None,
    and the directory is then WHICHEVER the request declared: a
    `BETTERMEMORY_WORKSPACE` environment variable, an
    `x-bettermemory-workspace` header, or a `file://` root the client
    offered (`identity.workspace_declaration`, in that precedence) — and
    only when none of those spoke, `Path.cwd()`. `source` records the
    answering channel on the returned origin; the process-cwd fallback is
    labeled as such rather than left implicit. A caller passing an
    explicit `cwd` may name its own `source`; when it does not, the field
    stays None, which the read surfaces render as the process cwd — the
    only channel any explicit-cwd caller in this tree stands in for.
    If git isn't on PATH or the directory isn't a repo, `repo` and
    `branch` come back null; `cwd` is always populated when the directory
    exists. Where git refuses to read a repository the files place the
    directory in (another account's checkout with ``safe.directory``
    unset), `worktree_root` is read from the files, `repo` and `branch`
    stay null, no further probe runs, and the origin is indeterminate
    (`Origin.git_indeterminate`). Where the ``.git`` entry git's walk
    reaches is one membership refuses (ENG-158, ENG-206), the origin is
    that of a folder outside any repository, determinate and never kept,
    and no git runs (`_git_result`, ENG-193).

    `worktree_root` is captured whenever we're inside any git checkout —
    it is the FIRST probe (`rev-parse --show-toplevel` fails exactly when
    the directory isn't a repo, so outside repos we still pay a single
    `_git` subprocess and bail). `repo` and `branch` are gated on it: a
    checkout with no remotes, or one whose remote isn't named `origin`,
    must still record its worktree boundary instead of collapsing the
    whole origin to null (which would make its writes global AND open the
    caller's auto-scope filter to every project). Two worktrees of the
    same repository will have the same `repo` but different
    `worktree_root`, which is what the auto-scope filter uses to keep a
    memory written from one worktree from leaking into a search run from
    a sibling worktree.

    Returns an all-null Origin when the process's working directory has
    been deleted (`Path.cwd()` raises FileNotFoundError). Hits in the
    Stop hook, where the user can `rm -rf` the dir they were working in
    before the turn ends — we'd rather log a `null`-origin event than
    let the audit explode.

    Once the directory is resolved, the probes' answers come from the
    origin cache (`_ORIGIN_CACHE`) when a capture of the same directory
    began less than `ORIGIN_CACHE_SECONDS` ago, by the monotonic clock and
    by the wall clock, and the directory's `_capture_signature` reads as it
    did then; no git runs. The Origin is built from them as from fresh
    answers, with this call's `source`, and the remote's alternates are
    registered the same way, so the result is the uncached one field for
    field, private attributes included. A capture is kept only when every
    probe ran and the signature read the same before and after them, which
    a HEAD moved away and back while they ran does not (git rewrote HEAD).
    A first probe that ran and exited non-zero where the files hold no
    repository either is an answer, "not a repository", and is kept for the
    lifetime like any other; where they hold one, git refused it, and the
    indeterminate capture is not kept. The lifetime is ten minutes, so what
    the signature does not see (`ORIGIN_CACHE_SECONDS` lists it) can be
    answered from a capture up to ten minutes old.
    """
    if cwd is None:
        declared = identity.workspace_declaration()
        if declared is not None:
            cwd, source = declared
        elif source is None:
            source = identity.SOURCE_PROCESS_CWD
    resolved = _resolved(cwd)
    if resolved is None:
        return Origin(source=source)
    cwd_str = str(resolved)

    started = time.monotonic()
    started_wall = time.time()
    signature = _capture_signature(resolved)
    entry = _ORIGIN_CACHE.get(cwd_str)
    keep = False
    repo_url: str | None
    repo_url_alternates: tuple[str, ...]
    if (
        entry is not None
        and 0.0 <= started - entry.captured_at < ORIGIN_CACHE_SECONDS
        and 0.0 <= started_wall - entry.captured_wall < ORIGIN_CACHE_SECONDS
        and entry.signature == signature
    ):
        cached = entry.origin
        worktree_root, indeterminate = cached.worktree_root, cached.git_indeterminate
        refusal = None  # a refused capture is indeterminate, never kept
        repo_url, repo_url_alternates = cached.repo, entry.alternates
        branch = cached.branch
    else:
        unanswered = _UNANSWERED.count
        worktree_root, indeterminate, refusal = _probe_worktree_root(resolved)
        repo_url = None
        repo_url_alternates = ()
        # A root read from the files of a repository git refused comes back
        # indeterminate: git refuses the remote and the branch there too.
        answered = worktree_root is not None and not indeterminate
        if answered:
            repo_url, repo_url_alternates = _git_remote_url_and_alternates(resolved)
        branch = _git_branch(resolved) if answered else None
        # Kept only when every probe ran and nothing the signature covers
        # moved while they ran. A probe git could not run for may answer on
        # the next call, and a change undone before the next call would
        # leave the old signature beside what the probes saw.
        keep = (
            not indeterminate
            and _UNANSWERED.count == unanswered
            and _capture_signature(resolved) == signature
        )

    origin = Origin(
        cwd=cwd_str,
        repo=repo_url,
        branch=branch,
        worktree_root=worktree_root,
        source=source,
    )
    # The first probe is the one that decides whether git ran; the two
    # gated on it cannot fail differently once it has answered.
    origin._git_indeterminate = indeterminate
    origin._git_refusal = refusal
    if repo_url is not None:
        # Register the remote's other official spellings (raw multi-URL
        # values, unexpanded insteadOf aliases) so `repos_match` can keep
        # recognizing stored origins captured under the pre-3.10
        # `git config --get` idiom — see `_CALLER_REPO_ALTERNATES`. Also
        # carried on the returned Origin (private, never persisted) for
        # callers that hold the object.
        _register_caller_alternates(repo_url, repo_url_alternates)
        origin._repo_url_alternates = repo_url_alternates
    if keep:
        _remember(
            cwd_str,
            _OriginEntry(
                origin=origin.model_copy(),
                alternates=repo_url_alternates,
                signature=signature,
                captured_at=started,
                captured_wall=started_wall,
            ),
        )
    return origin


def _resolved(cwd: Path | None) -> Path | None:
    """`cwd`, or else the process's directory, resolved as
    `os.path.realpath` resolves it; None where the process's directory is
    gone (`capture`).

    Not `Path.resolve`: before Python 3.13 it stats the path once more
    after resolving it (pathlib's check for a symlink loop, which raised
    RuntimeError out of `capture` for one), so a cache hit cost a stat more
    on 3.11 and 3.12 than on 3.13 (tests/test_membership_cost.py).
    """
    if cwd is not None:
        return Path(os.path.realpath(cwd))
    try:
        return Path(os.path.realpath(Path.cwd()))
    except (FileNotFoundError, OSError):
        return None


def capture_caller(cwd: Path | None = None) -> Origin:
    """The origin of a hook's caller: `capture()` when `cwd` is None (a
    hook process spawned in the project), else the capture of the
    directory the hook posted to the daemon, labelled as the process cwd
    it was (`identity.SOURCE_PROCESS_CWD`).

    The Stop audit (`hook.run_audit`), the prompt recall and the session
    brief (`recall`) capture their caller here, and each reads this name
    through the module at the call (`origin.capture_caller`), so a test
    places the hooks' caller for all of them by patching this one name."""
    if cwd is None:
        return capture()
    return capture(cwd, source=identity.SOURCE_PROCESS_CWD)


# ---------------------------------------------------------------------------
# The origin cache
# ---------------------------------------------------------------------------

#: How long a capture answers for its directory, in seconds from the moment
#: it began: ten minutes, so a session-start hook that fires alone, long
#: after the burst of calls before it, reuses the capture instead of paying
#: the four probes. The ten minutes run on both clocks: `time.monotonic`,
#: which stands still while the machine sleeps, and `time.time`, so a
#: capture expires when either says they have passed, and when the wall
#: clock reads before the capture began. Every lookup still checks the
#: directory's signature (`_capture_signature`), so a branch switch, a
#: commit, a remote set in the repository's config, core.worktree set in
#: config.worktree, a ref that changes the branch's short name (a tag or a
#: ref named like the branch, refs/stash on a branch named stash), a
#: repository created or removed on the path, a rename of the root or a
#: directory above it that changes only the case or the Unicode
#: normalisation of a name (seen through the kernel's spelling of the root
#: on macOS, whose default filesystem ignores both and still resolves the
#: old spelling), or a change to GIT_DIR, GIT_WORK_TREE or
#: GIT_CEILING_DIRECTORIES is answered on the next call as before. The
#: cost is what the signature does not see, which is now served for up to
#: ten minutes where it was served for two seconds: such a rename on a
#: platform other than macOS whose filesystem ignores case or normalisation (the
#: signature then holds the resolved path, which keeps the caller's
#: spelling); whether git can run; and a capture whose first probe ran and
#: exited non-zero where the files hold no repository, kept as "not a
#: repository" after a failure that clears on its own.
ORIGIN_CACHE_SECONDS = 600.0

# The most directories the cache holds; past it the oldest capture goes.
# Measured with tracemalloc (Python 3.11, macOS), a capture keeps about
# 1.8 KB for a directory outside any repository and 3.5 KB for a checkout
# with a remote, with what its signature reads, so the cap holds the cache
# under 1 MB. One entry per directory a caller captures from (a workspace,
# a hook's cwd); an evicted one costs its next capture the probes (one
# process outside a repository, four in a checkout).
_ORIGIN_CACHE_CAP = 256


@dataclass(frozen=True)
class _OriginEntry:
    """One capture, kept for the directory it was taken in: the Origin the
    probes returned (a copy no caller holds), the alternates registered for
    its remote, the directory's `_capture_signature` as read before and
    after the probes, and the `time.monotonic` and `time.time` readings at
    which the capture began."""

    origin: Origin
    alternates: tuple[str, ...]
    signature: githead.Signature
    captured_at: float
    captured_wall: float


def _capture_signature(start: Path) -> githead.Signature:
    """The change detector a capture of `start` is kept against: the
    directory's `githead.signature` (the git directory, HEAD, the loose refs
    HEAD's chain reads, packed-refs, config and the variables that move
    discovery), from the repository `githead.locate` finds where githead's
    discovery declines (`origin_gitfiles.capture_files`: off POSIX, and in
    another account's repository); the root spelled as git
    prints it (`_spelled_as_git`: the kernel's path on macOS, so a rename
    of the root or a directory above it that changes only the case or the
    Unicode normalisation of a name, under which the caller's old spelling
    still resolves, is seen; the resolved path elsewhere, which keeps the
    caller's spelling); and the stamp of each ref file that decides the
    branch's short name (`_shortening_refs`), in the git directory and in
    the common directory; and the machine's git config, the global and
    system files git reads and the files they include, stamped
    (`location_git.machine_config_stamps`, ENG-207), and the repository's
    own, its config and, where git reads it, config.worktree (whose
    core.worktree git reads too), with the files each includes
    (`location_git.repository_config_stamps`), so an insteadOf rule or a
    remote set in any is seen as a location capture sees it; and the
    legacy files git reads the URL of ``origin`` from where the config
    gives none, ``remotes/origin`` and ``branches/origin`` in the common
    directory (`location_git.legacy_remote_stamps`). An include under
    ``onbranch:`` or ``hasconfig:`` is stamped whatever its condition, and
    HEAD is in the base signature, so a branch switch that changes what
    git includes is seen. Where the files do not settle the repository,
    the base signature already equals no other; where a stamp or the
    spelling cannot be read, a new object makes this one equal no other
    either."""
    base, gd = capture_files(start)
    if gd is None:
        return base
    try:
        extra: list[object] = [machine_config_stamps(str(gd.gitdir))]
        extra.append(repository_config_stamps(str(gd.commondir), str(gd.gitdir)))
        extra.append(legacy_remote_stamps(str(gd.commondir), "origin"))
        spelled = _spelled_as_git(gd.worktree_root)
        extra.append(object() if spelled is None else str(spelled))
        ref = githead.head_ref(gd)
        if ref is not None:
            directories = dict.fromkeys((gd.gitdir, gd.commondir))
            for name in _shortening_refs(ref):
                for directory in directories:
                    extra.append(githead.stamp(directory / name, follow=False))
    except (OSError, ValueError):
        extra = [object()]
    return (*base, tuple(extra))


# One entry per resolved directory, keyed by its string and ordered by when
# the capture began. `capture` answers from an entry while it is younger
# than ORIGIN_CACHE_SECONDS on both clocks and the directory's signature
# equals the entry's. The signature (`_capture_signature`) holds the git
# directory the walk from the directory reaches, the bytes of its HEAD, the
# stamps of its config, of config.worktree where git reads it, of HEAD
# itself, of the loose
# refs HEAD's chain reads, of packed-refs and of the ref files that decide
# the branch's short name, the root spelled as git prints it, the stamps of
# the machine's git config files, of the repository's and of the files
# each includes, of the legacy remotes/origin and branches/origin files,
# and the GIT_DIR, GIT_WORK_TREE and GIT_CEILING_DIRECTORIES
# values, so a branch switch, a remote changed in the repository's config
# or a file it includes or by a global or system insteadOf rule (ENG-207),
# core.worktree set in
# config.worktree, a tag or ref named like the branch, a repository created
# or removed on the path, a rename that changes only a name's case or
# normalisation (on macOS), or a change to one of those variables is never
# answered from an entry, and no capture is kept whose probes ran while
# HEAD moved to another branch and back (git rewrites HEAD through a
# rename, which leaves a new inode and ctime); where the files do not
# settle what git would find, the signature equals no other and every
# capture asks git. What the signature does not see (whether git can run,
# such a rename on another platform's case-ignoring filesystem) holds for
# at most the
# lifetime, ten minutes, and so does a capture whose first probe ran and
# exited non-zero where the files hold no repository: `_probe_worktree_root`
# reads that exit as "not a repository", an answer, and a failure that
# clears (an unreadable file, a lock) exits non-zero as well. Where the
# files hold one, git refused it, and that capture is indeterminate and
# never kept. Keyed by directory, never by process. The lock serialises the
# store, the eviction and the clear; a lookup is one dict read.
_ORIGIN_CACHE: dict[str, _OriginEntry] = {}
_ORIGIN_CACHE_LOCK = threading.Lock()


@_caches.register
def _clear_origin_cache() -> None:
    with _ORIGIN_CACHE_LOCK:
        _ORIGIN_CACHE.clear()


def _remember(key: str, entry: _OriginEntry) -> None:
    """Keep `entry` as the newest capture for `key`, and evict the oldest
    captures past `_ORIGIN_CACHE_CAP`."""
    with _ORIGIN_CACHE_LOCK:
        _ORIGIN_CACHE.pop(key, None)
        _ORIGIN_CACHE[key] = entry
        while len(_ORIGIN_CACHE) > _ORIGIN_CACHE_CAP:
            del _ORIGIN_CACHE[next(iter(_ORIGIN_CACHE))]


class _Unanswered(threading.local):
    """The number of git calls through `_git` on the current thread that
    git could not run for: no binary, a timeout, a failed spawn. `_git`
    folds such a failure into the same None as an answer of "no", and the
    probes after the first go through it; `capture` compares the count
    before and after its probes and keeps no capture in which it moved."""

    count = 0


_UNANSWERED = _Unanswered()


# ---------------------------------------------------------------------------
# Repo equality — what "same project" means for the auto-scope filter
# ---------------------------------------------------------------------------

# Process-local registry of the alternate official spellings of remotes
# `capture()` has recorded, keyed by the URL it returned. Why this exists:
# releases v1.4.1–v3.9.0 captured `origin.repo` via `git config --get
# remote.origin.url`, which returns the LAST value of a multi-valued key
# and the RAW (unexpanded) spelling of an insteadOf alias; the current
# capture idiom (`git remote get-url origin`) returns the FIRST value
# with aliases expanded. Stores written under the old idiom therefore
# hold spellings the new capture never produces, and `(host, owner,
# name)` parsing can't reconcile them: a push-mirror URL is a genuinely
# different triple, and a raw alias like `gh:owner/repo` parses with the
# alias as the host. Without a bridge, every such stored memory silently
# fails `repos_match` against its own project forever (migrate only
# backfills origin-LESS memories). The registry carries every official
# spelling of the caller's OWN remote — verbatim from the caller's git
# config — so `repos_match` can recognize an old-idiom stored spelling
# without reverting the forward capture semantics. The never-widen
# invariant holds: only spellings git itself reports for the caller's
# remote are merged, never a guess. Keyed by the captured URL because
# that exact string is what the surface filters thread through as
# `current_repo` (`caller_origin.repo`); process-local and never
# persisted, so the on-disk format is untouched. Bounded FIFO so a
# long-lived server hopping across many repos can't grow it unboundedly.
# The daemon's workers capture on their own threads, so the lock makes the
# check, the eviction and the store one step: two registrations past the
# cap otherwise read the same oldest key, and the second pop raised
# KeyError out of `capture` (or, had it tolerated the miss, the registry
# grew past its cap). A lookup is one dict read and takes no lock. An
# entry, a remote and one other spelling, measured about 250 bytes, so the
# cap of 32 holds it near 8 KB; one per remote a caller captured in, and
# every capture registers its remote again, cached or not, so an evicted
# remote's spellings return with the next capture in its checkout.
_CALLER_REPO_ALTERNATES: dict[str, tuple[str, ...]] = {}
_CALLER_REPO_ALTERNATES_CAP = 32
_CALLER_REPO_ALTERNATES_LOCK = threading.Lock()


def _register_caller_alternates(repo_url: str, alternates: tuple[str, ...]) -> None:
    """Record (or refresh) the alternate spellings captured for `repo_url`.

    Registering an empty tuple is meaningful — it clears a stale entry
    after the remote's extra URLs were removed from git config.
    """
    with _CALLER_REPO_ALTERNATES_LOCK:
        if (
            repo_url not in _CALLER_REPO_ALTERNATES
            and len(_CALLER_REPO_ALTERNATES) >= _CALLER_REPO_ALTERNATES_CAP
        ):
            # FIFO eviction — dicts preserve insertion order.
            _CALLER_REPO_ALTERNATES.pop(next(iter(_CALLER_REPO_ALTERNATES)), None)
        _CALLER_REPO_ALTERNATES[repo_url] = alternates


def repos_match(
    memory_repo: str | None,
    current_repo: str | None,
    *,
    caller_alternates: tuple[str, ...] | None = None,
) -> bool:
    """True if a memory whose origin.repo is `memory_repo` belongs to a
    caller whose current repo is `current_repo`.

    Equality is normalized: `git@github.com:owner/repo.git` and
    `https://github.com/owner/repo` and `https://github.com/owner/repo.git`
    all describe the same project. We compare on `(host, owner, name)`
    rather than raw URL strings.

    A null `memory_repo` is "global" — matches any current_repo. A null
    `current_repo` (caller is not in a repo) also matches any
    `memory_repo` since we have no project boundary to enforce.

    `caller_alternates` are additional official spellings of the
    CALLER's remote. When omitted, the spellings `capture()` registered
    for `current_repo` in this process are consulted (see
    `_CALLER_REPO_ALTERNATES`). A memory matching ANY spelling of the
    caller's own remote belongs to the caller's project — this keeps
    stores written under the pre-3.10 capture idiom (last URL of a
    multi-valued remote, raw insteadOf alias) from silently going dark
    under auto-scope after the switch to `git remote get-url`.
    Deliberately asymmetric: alternates apply to the caller side only,
    because the caller's git config is where the evidence comes from.
    """
    if memory_repo is None or current_repo is None:
        return True
    if _spellings_match(memory_repo, current_repo):
        return True
    if caller_alternates is None:
        caller_alternates = _CALLER_REPO_ALTERNATES.get(current_repo, ())
    return any(
        alternate != current_repo and _spellings_match(memory_repo, alternate)
        for alternate in caller_alternates
    )


def _spellings_match(memory_repo: str, current_repo: str) -> bool:
    """Single-spelling comparison — the pre-alternates `repos_match` core."""
    parsed_a = _parse_remote(memory_repo)
    parsed_b = _parse_remote(current_repo)
    if parsed_a is None or parsed_b is None:
        # Unparseable on either side — fall back to raw equality so we
        # don't let opaque URLs through under "global" by mistake.
        return memory_repo == current_repo
    # Compare host/owner/name case-insensitively. GitHub treats user/org
    # names as case-insensitive; in practice GitLab and Bitbucket too.
    a = tuple(s.lower() for s in parsed_a)
    b = tuple(s.lower() for s in parsed_b)
    return a == b


def worktrees_match(memory_worktree: str | None, caller_worktree: str | None) -> bool:
    """rc3's worktree test over the origin columns, kept for the
    rc3_admitted shadow column (design 8.11); no delivery decides by it."""
    if memory_worktree is None or caller_worktree is None:
        return True
    if memory_worktree == caller_worktree:
        return True
    if _primary_root_of(caller_worktree) == memory_worktree:
        return True
    return _worktree_root_is_gone(memory_worktree)


def _worktree_root_is_gone(worktree: str) -> bool:
    """Whether the OS positively reported that nothing exists at
    `worktree`, as `worktrees_match` reads it for the rc3_admitted shadow
    column (design 8.11): `_location_probe.path_gone`, the one reading of
    that answer membership and location share, whose comments give each
    errno and Windows code it takes as gone. Every other error, a share
    that does not answer among them, cannot tell, which holds the
    isolation boundary rather than opening it."""
    try:
        os.stat(worktree)
    except ValueError:
        # `os.stat` rejected the string before the OS ever saw it (an
        # embedded NUL, an un-encodable surrogate). Nothing can live at
        # a path this process cannot even ask about, so this is an
        # answer, not a refusal to answer.
        return True
    except OSError as exc:
        if path_gone(exc):
            return True
        # Indeterminate. Log it: "my project's memories went dark" and
        # "a stale mount is quietly widening auto-scope" are both
        # invisible from the outside, and this is the only place that
        # sees the errno.
        log.debug(
            "cannot determine whether worktree root %s still exists (%s); "
            "keeping worktree isolation in force",
            worktree,
            exc,
        )
        return False
    return False


@lru_cache(maxsize=64)
def _primary_root_of(worktree: str) -> str | None:
    """The primary checkout root of a linked worktree (None for a
    primary checkout, a bare path or anything unreadable), as
    `worktrees_match` reads it for the rc3_admitted shadow column."""
    gitfile = Path(worktree) / ".git"
    try:
        if not gitfile.is_file():
            return None
        content = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^gitdir:\s*(.+)$", content, re.MULTILINE)
    if m is None:
        return None
    gitdir = m.group(1).strip()
    for marker in ("/.git/worktrees/", "\\.git\\worktrees\\"):
        idx = gitdir.find(marker)
        if idx != -1:
            try:
                return str(Path(gitdir[:idx]).resolve())
            except OSError:
                return gitdir[:idx]
    return None


def should_include_for_caller(
    memory_origin: Origin | None,
    caller_repo: str | None,
    *,
    caller_worktree_root: str | None = None,
    caller_repo_alternates: tuple[str, ...] | None = None,
) -> bool:
    """rc3's membership test over the origin columns, kept to compute
    the rc3_admitted shadow column (design 8.11); every delivery decides
    through `membership_records.admitted` instead (design 3.5)."""
    memory_repo = memory_origin.repo if memory_origin else None
    if not repos_match(
        memory_repo, caller_repo, caller_alternates=caller_repo_alternates
    ):
        return False
    memory_worktree = memory_origin.worktree_root if memory_origin else None
    return worktrees_match(memory_worktree, caller_worktree_root)


# ---------------------------------------------------------------------------
# Git helpers — shell out, swallow failures
# ---------------------------------------------------------------------------


def _log_subcommand(args: tuple[str, ...]) -> str:
    """The subcommand in a git argv, for failure log lines
    (`_git.subcommand`)."""
    return _git_runner.subcommand(args)


def unanswered_git_calls() -> int:
    """How many git invocations since import could not run at all (the
    None `_git_result` returns), on every thread (`_git.unanswered_calls`).
    The drift memos read `failed_git_calls` instead, which counts these
    and every other None on the calling thread."""
    return _git_runner.unanswered_calls()


def failed_git_calls() -> int:
    """How many git calls on the current thread have come back None from
    `_git` or `_git_result`: git could not run, it exited non-zero, or
    `_git` folded an empty output into None (`_git.failed_calls`, where
    the drift memos' reason for reading it before and after a value is
    given)."""
    return _git_runner.failed_calls()


def _git_result(
    cwd: Path, *args: str, timeout: float = 1.0, stdin: bytes | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run a git command from `cwd` and return the completed process, or
    None ONLY when git could not run at all — no binary on PATH, the
    timeout, an OSError from the spawn. A non-zero exit is an ANSWER
    ("not a repository", "not an ancestor", "no such commit") and comes
    back as the process, exit code and stderr intact, for the caller to
    read. `_git` below folds both into one None for the callers that
    only want stdout; the probes that must tell "git said no" from "git
    could not be asked" (`_probe_worktree_root`, `commit_reachable`)
    read this directly. Each None is counted (`unanswered_git_calls`,
    and on the calling thread `failed_git_calls`).

    The run itself is `_git.run_git`: the caller's environment with
    ``GIT_DIR`` and its kin removed, so a shell that exports them never
    turns a capture of `cwd` into one of another repository; fsmonitor
    and hooks pinned off; UTF-8 with ``errors="replace"``; the could-not-
    run failures logged at WARNING there, the non-zero exits that `_git`
    folds at DEBUG.

    Git follows any ``.git`` file or link, so in a folder whose ``.git``
    entry names another checkout's git directory (a file planted in an
    extracted archive, a link to another tree's ``.git``, a pointer at a
    git directory kept apart that is recorded for another folder) every
    question here was answered by that checkout: its toplevel, remote,
    branch, HEAD and history became the origin of a record written there,
    which verify anchoring reads (ENG-193). Where git's walk from `cwd`
    reaches a repository membership does not follow
    (`gitfile_rule.discovery_refusal`, the rule membership's discovery and
    `githead` read), git is not run: the answer is the one git gives for a
    ``.git`` entry that names no repository, exit 128 and ``fatal: not a
    git repository``, and nothing is counted as unanswered. Every git
    process the origin family runs comes through here, so a refused entry
    gives no origin repository, toplevel, branch or HEAD along any path,
    and the folder's origin is that of a folder outside any repository;
    its signature equals no other (`githead.signature`), so `capture`
    keeps none of it. A git directory kept apart that no record names the
    folder for yet is followed, as membership's rule follows it, and
    nothing here records it: membership's capture of the same folder
    records the first tree it follows (in every hook, and in a write
    before it keeps its origin, `follow_unsettled`), and until then no
    capture of the folder is kept, so the next one reads the record.
    Where the state directory refuses the record (the audit warns of it,
    `location_audit`), the follow stays unrecorded and every capture there
    asks git: four processes, 37 ms (median of 40) against 0.6 ms for a
    kept capture, on the author's machine. That cost is left unbounded: a
    capture kept across it would have to see the records' folder in its
    signature, outside the repository's files and under the filesystem's
    clock tick, and one kept past a record that refuses the folder is the
    origin `follow_unsettled` exists to keep off a record.
    """
    why = gitfile_rule.discovery_refusal(os.fspath(cwd))
    if why is not None:
        log.debug("git is not run in %s: %s", cwd, why)
        return subprocess.CompletedProcess(
            ["git", *args],
            128,
            stdout="",
            stderr=f"fatal: not a git repository: {why}\n",
        )
    # The history as its objects hold it, whatever the repository sets
    # (`origin_history.AS_STORED_CONFIG`, ENG-220's review).
    return _git_runner.run_git(
        (*origin_history.AS_STORED_CONFIG, *args),
        cwd=cwd,
        timeout=timeout,
        stdin=stdin,
        env=origin_history.AS_STORED_ENV,
    )


def follow_unsettled(found: Origin) -> bool:
    """Whether the repository `found` places its folder in rests on a
    ``.git`` entry membership's next capture may judge otherwise (ENG-193):
    a git directory kept apart that no record names the folder for yet
    (`gitfile_rule.Entry.provisional`), whose record that capture makes
    and may make for another folder (`location._earlier_tree`), or one
    membership refuses now, a record made since `found` was captured
    naming another folder. A write that finds its origin so resolves its
    caller first, whose capture makes the record, and captures the origin
    again (`handlers.write._write_payload`): no such capture was kept
    (`githead.signature`), so the second reads the record, and a record
    never carries a follow membership refuses. False where `found` places
    the folder in no repository."""
    if found.worktree_root is None or not found.cwd:
        return False
    reading = gitfile_rule.discovery_reading(found.cwd)
    return reading is not None and (reading.why is not None or reading.provisional)


def _git(
    cwd: Path,
    *args: str,
    timeout: float = 1.0,
    empty_ok: bool = False,
    verbatim: bool = False,
    stdin: bytes | None = None,
) -> str | None:
    """Run a git command from `cwd`. Returns trimmed stdout on success,
    or with `verbatim` stdout as git wrote it, for a caller that reads
    paths from it (`_git_runner.printed_path`: a name may end in a space or
    hold a newline); None on any failure. Short timeout so a hanging git
    never stalls a memory_write — the write is the user-facing operation;
    origin is nice-to-have. Built on `_git_result`, which keeps the two kinds of
    failure apart for the callers that need them apart.

    `empty_ok` splits "git ran fine, no output" from "git could not run":
    with it True a zero-exit call with EMPTY stdout returns ``""`` instead
    of None, so a caller can tell a clean-but-empty result (`git log --
    <specs>` listing no commit) apart from an actual failure (non-zero
    exit, missing binary, timeout — still None). Default False keeps the
    historical ``out or None`` collapse every other caller relies on.
    Every None is counted on the calling thread (`failed_git_calls`).
    `stdin` is written to git's standard input (`cat-file --batch-check`).

    Failure logging is tiered so the common "not a repo" case stays
    silent while operationally interesting failures (missing binary,
    timeouts, safe.directory rejection, corrupted .git) reach the log:
    the could-not-run failures at WARNING (`_git.run_git`), a non-zero
    exit at DEBUG with stderr's first line (`_git.log_nonzero_exit`),
    since the vast majority of those are "fatal: not a git repository"
    from a non-repo cwd, which is fully expected (memories written
    outside any repo get `repo=None`).
    """
    result = _git_result(cwd, *args, timeout=timeout, stdin=stdin)
    if result is None:
        _UNANSWERED.count += 1
        return None
    if result.returncode != 0:
        _git_runner.log_nonzero_exit(args, result, cwd)
        _git_runner.note_failed()
        return None
    out = result.stdout.strip()
    if out or empty_ok:
        return result.stdout if verbatim and out else out
    _git_runner.note_failed()
    return None


def _git_remote_url_and_alternates(cwd: Path) -> tuple[str | None, tuple[str, ...]]:
    # `git remote get-url origin`, NOT `git config --get remote.origin.url`:
    # `config --get` on a multi-valued key returns the LAST value, so the
    # canonical push-mirror recipe (`git remote set-url --add origin
    # <mirror>`) would flip every subsequent capture to the mirror URL and
    # hide all previously written memories for the repo. `get-url` returns
    # the FIRST configured URL — the canonical fetch URL — and also expands
    # `url.<base>.insteadOf` aliases to the URL git actually fetches
    # from. It exits non-zero when the remote doesn't
    # exist, so `_git` maps that to None.
    #
    # The alternates are every RAW configured URL for the SAME remote
    # (`git config --get-all remote.<name>.url`) other than the captured
    # one. That covers exactly the two spellings the pre-3.10 capture
    # idiom (`config --get`) produced and `get-url` doesn't: the last URL
    # of a multi-valued remote, and the unexpanded insteadOf alias.
    # They're official spellings of this remote per the caller's own git
    # config — never persisted, only registered process-locally so
    # `repos_match` keeps old-idiom stored origins visible. Failure to
    # read them degrades to no alternates (the forward capture is
    # unaffected).
    name = "origin"
    url = _git(cwd, "remote", "get-url", "origin")
    if url is None:
        # No remote named 'origin' (`git clone -o <name>`, the
        # clone.defaultRemoteName config, `git remote rename`, upstream-only
        # fork workflows). Fall back to the first remote `git remote` lists so
        # the checkout keeps a repo identity instead of writing global
        # memories. A repo with no remotes at all yields empty output, which
        # `_git` already maps to None.
        remotes = _git(cwd, "remote")
        if remotes is None:
            return None, ()
        first = remotes.splitlines()[0].strip()
        if not first:
            return None, ()
        name = first
        url = _git(cwd, "remote", "get-url", name)
        if url is None:
            return None, ()
    raw = _git(cwd, "config", "--get-all", f"remote.{name}.url")
    if raw is None:
        return url, ()
    seen: set[str] = {url}
    alternates: list[str] = []
    for line in raw.splitlines():
        candidate = line.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            alternates.append(candidate)
    return url, tuple(alternates)


def _git_branch(cwd: Path) -> str | None:
    # `symbolic-ref --short HEAD` returns the branch name (e.g. "main") on
    # any branch, including a freshly-initialised repo before its first
    # commit. It exits non-zero on a detached HEAD — `_git` returns None
    # in that case, which is what we want. `rev-parse --abbrev-ref HEAD`
    # is the more common idiom but it returns the literal "HEAD" before
    # the first commit, which we'd incorrectly interpret as detached.
    return _git(cwd, "symbolic-ref", "--short", "HEAD")


def _probe_worktree_root(cwd: Path) -> tuple[str | None, bool, str | None]:
    """``(worktree_root, indeterminate, refusal)`` for `cwd`.

    `rev-parse --show-toplevel` returns the absolute path of the working
    tree root — for a primary checkout, the repo root; for a worktree
    (`git worktree add`), the worktree's own root, which *differs*
    between sibling worktrees of the same repository. That difference
    is exactly what the auto-scope filter needs to tell two worktrees of
    one repo apart. Resolved through `Path` to normalise symlink hops on
    macOS' `/var` → `/private/var` idiom, so a memory captured under one
    symlink form still compares equal to a caller that resolves the
    other.

    `indeterminate` is True when git could not RUN — the answer is then
    unknown, not "no" — and when git exited non-zero while the files hold
    a repository there (`origin_gitfiles.refused_worktree_root`: git
    refused it, for another account's ownership with ``safe.directory``
    unset among the rest; the root then comes from the files where they
    settle it). Git exiting non-zero where the files hold no repository
    either ("not a git repository") is the measured no this probe has
    always reported as None. `refusal` is the first line git wrote when
    it refused, None otherwise. A toplevel that is not the folder
    membership roots the checkout at is None too (`_agreed`, ENG-211).

    A path that is not a directory here (a declared workspace this host
    does not hold, a directory removed under a hook, a file) is not
    probed: git cannot run in it, the spawn's FileNotFoundError read as a
    missing git binary, and the answer is indeterminate, counted as a
    failure as the spawn was, with a warning that names the path, once
    per path per process and at DEBUG after (`first_not_a_directory`).
    """
    try:
        missing = not cwd.is_dir()
    except OSError:
        missing = False
    if missing:
        log.log(
            logging.WARNING if first_not_a_directory(cwd) else logging.DEBUG,
            "the origin of %s is indeterminate: it is not a directory this host holds",
            cwd,
        )
        _git_runner.note_failed()
        return None, True, None
    result = _git_result(cwd, "rev-parse", "--show-toplevel")
    if result is None:
        return None, True, None
    if result.returncode != 0:
        _git_runner.log_nonzero_exit(("rev-parse", "--show-toplevel"), result, cwd)
        root, refused = refused_worktree_root(cwd)
        if not refused:
            return root, False, None
        lines = (result.stderr or "").strip().splitlines()
        reason = lines[0] if lines else f"exit {result.returncode}"
        log.info(
            "git refused the repository holding %s (%s); its root is read "
            "from the files (%s) and the origin is indeterminate",
            cwd,
            reason,
            root,
        )
        return root, True, reason
    # Only git's newline comes off: a folder's name may end in a space or
    # hold a newline.
    raw = _git_runner.printed_path(result.stdout)
    if not raw or not _agreed(cwd, raw):
        return None, False, None
    try:
        return str(Path(raw).resolve()), False, None
    except OSError:
        return raw, False, None


def _agreed(cwd: Path, toplevel: str) -> bool:
    """Whether the `toplevel` git printed for `cwd` is the folder
    membership roots the checkout at (`gitfile_rule.toplevel_agrees`): an
    origin keeps no other, so a record never carries a root membership's
    reading of the repository's config missed (ENG-211)."""
    if gitfile_rule.toplevel_agrees(os.fspath(cwd), toplevel):
        return True
    log.info(
        "git names %s for %s, not membership's root; no origin is kept", toplevel, cwd
    )
    return False


def _git_worktree_root(cwd: Path) -> str | None:
    """The worktree root alone, for callers that treat "could not ask"
    and "not a repository" the same way (doctor's attestation-anchor
    scoping declines on either)."""
    return _probe_worktree_root(cwd)[0]


def is_full_commit_sha(value: object) -> bool:
    """Whether `value` is a complete lowercase commit hash — 40 hex
    characters for SHA-1 repositories, 64 for SHA-256 ones. The one
    shape the read side hands to git as a revision: an abbreviation, a
    ref name or an option-shaped string never reaches an argv."""
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(c in "0123456789abcdef" for c in value)
    )


def repo_toplevel(cwd: Path | None) -> Path | None:
    """Resolve the repo root for `cwd` via ``git rev-parse --show-toplevel``.

    Returns the resolved absolute root, or None when git can't answer
    (cwd is None, git not on PATH, not a repo, unresolvable output).
    Split out so batch callers (the health rollups walk many memories
    against ONE repo) can resolve the root once and thread it through
    `resolve_repo_pathspecs(..., toplevel=...)` instead of paying a
    ``rev-parse`` fork+exec per memory.
    """
    if cwd is None:
        return None
    raw = _git(cwd, "rev-parse", "--show-toplevel", verbatim=True)
    toplevel_raw = None if raw is None else _git_runner.printed_path(raw)
    if not toplevel_raw or not _agreed(cwd, toplevel_raw):
        return None
    try:
        return Path(toplevel_raw).resolve()
    except OSError:
        return None


def repo_toplevel_and_head(cwd: Path | None) -> tuple[Path, str] | None:
    """`repo_toplevel` and the commit HEAD names: read from the
    repository's files when they settle both (`toplevel_and_head_from_
    files`), from ONE git process otherwise.

    The commit-drift surfaces that count in reachability space need both,
    the root to resolve pathspecs against and the head to key the
    reachable walk and the drift memos on, once per search or show. The
    files answer without a process; where they decline, ``git rev-parse
    --show-toplevel HEAD`` prints the root and then the full hash, one
    per line. None when git cannot answer, including a repository with no
    commit yet (``HEAD`` does not resolve there; the author-date readers
    fail on it the same way).
    """
    if cwd is None:
        return None
    located = toplevel_and_head_from_files(cwd)
    if located is not None:
        return located
    raw = _git(cwd, "rev-parse", "--show-toplevel", "HEAD", verbatim=True)
    if raw is None:
        return None
    # git prints the root, then the hash, each with a newline after it: the
    # root is all before the last newline, whatever its name holds (a space
    # at its end, a newline), its line end taken off as a printed path's.
    root, newline, head = _git_runner.printed_path(raw).rpartition("\n")
    root = _git_runner.printed_path(root + newline)
    if not root or not is_full_commit_sha(head) or not _agreed(cwd, root):
        return None
    try:
        return Path(root).resolve(), head
    except OSError:
        return None


def toplevel_and_head_from_files(cwd: Path | None) -> tuple[Path, str] | None:
    """`repo_toplevel_and_head` read from the repository's files, with no
    process: the root git prints for `cwd` and the commit HEAD names. None
    wherever the files do not settle what git would print.

    The head is `githead.head_sha` of the repository the walk up from
    `cwd` finds (`githead.find_gitdir`). The root is the directory holding
    that walk's ``.git`` entry, which is the root git prints unless the
    repository's own configuration moves it: ``core.worktree`` names
    another directory and a true ``core.bare`` leaves none, and git reads
    both from ``config`` in the common directory and, under
    ``extensions.worktreeConfig``, from ``config.worktree`` in the git
    directory, and from no other file (a global or ``include.path`` file
    and a ``-c`` do not move a discovered repository's working tree).
    ``core.worktree`` set in either file, read as git reads it
    (`_config_variable_names`), a line naming ``bare`` with anything but a
    plain false, a file git refuses, that holds a NUL byte, that cannot be
    read or that ``git rev-parse`` may die reading (an include, a value
    its default config refuses: `origin_gitfiles._rev_parse_dies`)
    decline (`_config_moves_worktree`), as does a machine's config git may
    die reading (`_machine_config_dies`, from the reads the origin's
    capture keeps: none read once one is captured, else read once, the
    signature after reusing the stats), and so does
    everything `githead` declines, a ``.git`` entry membership refuses
    among them; the process `repo_toplevel_and_head` runs then answers no
    repository for such an entry without running (`_git_result`).

    The root is spelled the way git prints it (`_spelled_as_git`), which
    is the spelling the pathspec resolution needs: an absolute anchor is
    made relative to it by string prefix.
    """
    if cwd is None:
        return None
    gd = githead.find_gitdir(cwd)
    if gd is None:
        return None
    head = githead.head_sha(gd)
    if (
        head is None
        or _config_moves_worktree(gd)
        or _machine_config_dies(machine_config_reads, gd.gitdir)
    ):
        return None
    root = _spelled_as_git(gd.worktree_root)
    if root is None:
        return None
    return root, head


def head_sha(cwd: Path | None) -> str | None:
    """The full hash of the commit HEAD names in `cwd`'s repository, or
    None when git cannot answer or the repository has no commit yet.
    What `memory_verify` records as the stamp's anchor."""
    if cwd is None:
        return None
    raw = _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if raw is None:
        return None
    sha = raw.strip().lower()
    return sha if is_full_commit_sha(sha) else None


def commit_reachable(cwd: Path | None, sha: str) -> bool | None:
    """Whether `sha` is an ancestor of HEAD (or HEAD itself) in `cwd`'s
    repository: True, False when it is not — a rewritten history, or a
    commit the repository never had — or None when git could not answer
    (not a repository, no commit yet, no binary). What the restore
    re-check asks of a tombstone's `verified_head`: an anchor HEAD does
    not descend from cannot be counted from, whether or not the object
    still sits in the store. `sha` must already be a full hash — the
    loaders guarantee that — so the only argv it can form is a revision."""
    if cwd is None or not is_full_commit_sha(sha):
        return None
    result = _git_result(cwd, "merge-base", "--is-ancestor", sha, "HEAD")
    if result is None:
        # Git could not be asked — no binary, a timeout, a failed spawn.
        # That is no answer, and the restore re-check reads False as
        # "strip the anchor for good", so it must not be manufactured
        # from a question that never reached git. (Before this split a
        # timeout here landed on the `head_sha` probe below, which can
        # succeed on its own faster call and turn the timeout into a
        # False.)
        return None
    if result.returncode == 0:
        return True
    _git_runner.log_nonzero_exit(("merge-base", "--is-ancestor"), result, cwd)
    # A non-zero exit is "not an ancestor", "no such commit" and "not a
    # repository" alike. Tell the last apart with the cheapest question
    # that answers differently for it: can git see a HEAD here at all?
    return False if head_sha(cwd) is not None else None


# The reachable walk is memoised per process, keyed on (root, anchor,
# head) and the files beside the history that decide what its listing
# shows (`walk_files_signature`: the repository's config, the working
# tree's .gitmodules, and the index's .gitmodules entry where git reads
# it). The walk names both ends (`_walk_reachable` runs to the key's
# head, not to whatever HEAD names by then), and the range between two
# named commits never changes, so an entry is never stale while those
# files read as they did; the key's `head` is what lets a long-lived
# server stop reusing a walk the moment a commit lands, and the rest
# what stops it once a setting there is written, in the working tree's
# .gitmodules or the index's copy, or the index's copy goes unmerged
# (log.showRoot and a submodule's ignore, which moved the walk, are
# pinned: `origin_history.AS_STORED_CONFIG`, `AS_STORED_DIFF`). A write
# to the index that leaves its .gitmodules entry as it was keys no new
# walk. A walk is kept only when
# those files read the same after it as before it and neither the root
# directory nor the index file moved meanwhile, so a .gitmodules created
# and removed while git ran is not kept, nor is a walk during which the
# index was written. Where they cannot be read (under GIT_DIR and the
# variables like it; GIT_INDEX_FILE is set; the index is split), no walk
# is kept. What the stamps do not see
# (git's configuration outside the repository's own config files, a
# shallow clone deepened under an unchanged head, and the
# two changes undone mid-walk `walk_files_signature` names) reads the
# memoised walk until the head moves; while files are created and removed
# in the root steadily, no walk is kept (`_hold_directory`). Bounded so a
# store with many distinct anchors cannot grow it without limit. Only a
# walk that listed its names is stored, never a None nor the commits
# listed alone after a timeout: a None is also what a git process that
# failed yields (an object briefly unreadable, a timeout), and the
# fallback taken on a failure must not outlive the call that met it, save
# a timeout's, kept a minute apart (`_WALK_TIMED_OUT`, below). A
# caller that resolves many memories in one pass keeps the Nones for that
# pass, and the walks no memo keeps, in its `walked` mapping
# (`commits_since_anchor`). The lock makes each look-up-and-touch and each
# insert-and-evict atomic; the walk runs outside it. Registered with
# `_caches`, which empties it before each test. One entry per (root,
# anchor, head): an anchor is a memory's verified head, so a search adds
# at most one per distinct verified head among its hits in a repository. The
# cap counts entries, not bytes: measured with tracemalloc (Python 3.11,
# macOS, each commit changing one file), a walk holds about 141 bytes a
# commit, 183 KB at 1,000 commits and 1.4 MB at 10,000, so 128 walks of
# 10,000 commits hold about 180 MB; the walk took 15 us a commit, and the
# 5 s ceiling admits one of about 335,000 commits (about 47 MB).
#
# A walk that met its timeout is kept apart, under the same key, for
# `_WALK_TIMED_OUT_SECONDS` (`_WALK_TIMED_OUT`): the commits listed alone
# where its names met it, or why it was given up where the listing met it
# too or failed at an anchor the repository holds (`origin_history.
# unwalked`: ``TIMEOUT``, ``ERROR``). Each costs a 5 s bound or two, which every
# call in that minute would pay again: a health rollup walks each verified
# head again on every call, with no per-row memo (ENG-220's review, round
# E). It is served as the pass's `walked` serves it, counted as a timeout
# again, so a caller keeps nothing from it past its own timeout memo; a
# commit, a fetch or a change to the files beside the history moves its
# key, as it moves the walk's.
_WALK_MEMO_CAP = 128
_WALK_TIMED_OUT_SECONDS = 60.0
_WalkKey: TypeAlias = tuple[str, str, str, tuple[object, ...]]
_WALK_MEMO: OrderedDict[_WalkKey, ReachableWalk] = OrderedDict()
_WALK_TIMED_OUT: OrderedDict[
    _WalkKey, tuple[float, ReachableWalk | origin_history.HistoryUnread]
] = OrderedDict()
_WALK_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_walk_memo() -> None:
    with _WALK_MEMO_LOCK:
        _WALK_MEMO.clear()
        _WALK_TIMED_OUT.clear()


def _timed_out_walk(key: _WalkKey) -> tuple[bool, ReachableWalk | None]:
    """Whether a walk for `key` met its timeout less than
    `_WALK_TIMED_OUT_SECONDS` ago, and its outcome: the commits listed
    alone, or None where it was given up. A kept one is counted on the
    calling thread as the walk that met it was: a timeout, and a walk
    given up, and why (`origin_history.note_unwalked`), where it was."""
    with _WALK_MEMO_LOCK:
        kept = _WALK_TIMED_OUT.get(key)
        if kept is None:
            return False, None
        if not 0.0 <= time.monotonic() - kept[0] < _WALK_TIMED_OUT_SECONDS:
            del _WALK_TIMED_OUT[key]
            return False, None
        _WALK_TIMED_OUT.move_to_end(key)
    _git_runner.note_timed_out()
    if isinstance(kept[1], origin_history.HistoryUnread):
        origin_history.note_unwalked(kept[1])
        return True, None
    return True, kept[1]


def _keep_timed_out_walk(
    key: _WalkKey | None,
    walk: ReachableWalk | origin_history.HistoryUnread,
    toplevel: Path,
    held: dict[str, tuple[int, int]],
) -> None:
    """Keep the outcome of a walk for `key` that met its timeout, where
    the files beside the history are as they were before it ran."""
    if key is None:
        return
    if walk_files_signature(toplevel) != key[3] or not directories_held(held):
        return
    with _WALK_MEMO_LOCK:
        _WALK_TIMED_OUT[key] = (time.monotonic(), walk)
        _WALK_TIMED_OUT.move_to_end(key)
        while len(_WALK_TIMED_OUT) > _WALK_MEMO_CAP:
            _WALK_TIMED_OUT.popitem(last=False)


# Record separator for the walk's format line, the same control
# character the patch stream carries: a path cannot contain it.
_WALK_MARK = "\x01"


def commits_since_anchor(
    cwd: Path | None,
    anchor: str,
    *,
    toplevel: Path | None = None,
    head: str | None = None,
    walked: Walked | None = None,
) -> ReachableWalk | None:
    """The reachable walk from `anchor` to HEAD, or None when the count
    must fall back to author-date space.

    One git process per distinct (root, anchor, head) and stamps of the
    files beside the history (`walk_files_signature`), the walks memoised
    (see `_WALK_MEMO`) and the Nones not, save a timeout's for a minute
    (`_WALK_TIMED_OUT`).
    ``git log --boundary --name-only anchor..<head>``, the head named by
    its hash, lists every commit in the range with the paths it changed,
    and marks the range's boundary commits with ``-`` in ``%m``: the
    anchor is an ancestor of HEAD exactly when it appears among them (a
    boundary commit is a parent of a commit reachable from HEAD, and an
    ancestor that is not HEAD itself is the parent of the range's oldest
    commit on some path). An EMPTY listing means HEAD is reachable from
    the anchor (the same commit, or a checkout that moved backwards), and
    only the first of those is a measurement.

    `walked`, a mapping the caller keeps for one pass over many memories,
    remembers per (root, anchor, head) what no memo keeps: each walk that
    came back None, as whether a git process failed for it, and each walk
    the process-wide memo could not key (the stamps unreadable) or did not
    list its names. A later call for the same key in that pass returns the
    remembered walk, or None without a process, counting the failure again
    on the calling thread (`failed_git_calls`), a timeout for a walk listed
    alone after its names met theirs, so a caller that keeps a value only
    when no git call failed judges every hit at that anchor as it judged
    the first.

    A walk whose names git did not read, whether they met their timeout or
    git failed on them (a tree it cannot read: a treeless clone, a corrupt
    object), falls back to its commits listed alone, with no diff
    (`ReachableWalk.listed` False), whose whole length claim drift keeps;
    one after a timeout (`ReachableWalk.timed_out`) is kept a minute,
    apart from the walks (`_WALK_TIMED_OUT`). Where that listing meets its
    timeout too, the walk is given up on (`origin_history.unwalked`,
    ``TIMEOUT``), kept a minute as well; where git fails on it too and the
    repository holds the anchor (`_held_anchor`), it is given up on as
    ``ERROR``, kept a minute where the names met their timeout and for the
    pass otherwise. The caller then omits the count: it never reads the
    author dates in its place, which a rebased or back-dated commit moves
    (ENG-220's review, rounds C, F and G).

    None, and the author-date fallback, when: `anchor` is not a full
    hash (never handed to git as a revision), git fails on the listing
    too at an anchor this repository does not hold, the anchor is not an
    ancestor of HEAD (a rewritten history), or HEAD sits behind it.
    `toplevel` and `head`
    skip the root and head resolution (`repo_toplevel_and_head`) when
    the caller already has them, the way the batch surfaces thread a
    once-resolved root.
    """
    if cwd is None or not is_full_commit_sha(anchor):
        return None
    if toplevel is None or head is None:
        located = repo_toplevel_and_head(cwd)
        if located is None:
            return None
        toplevel, head = located
    held: dict[str, tuple[int, int]] = {}
    files = walk_files_signature(toplevel, directories=held)
    within = (str(toplevel), anchor, head)
    key = None if files is None else (*within, files)
    if key is not None:
        with _WALK_MEMO_LOCK:
            stored = _WALK_MEMO.get(key)
            if stored is not None:
                _WALK_MEMO.move_to_end(key)
                return stored
        kept, timed_out_walk = _timed_out_walk(key)
        if kept:
            return timed_out_walk
    if walked is not None and within in walked:
        seen = walked[within]
        if isinstance(seen, ReachableWalk):
            if seen.timed_out:
                # Its names met their timeout: the same one, served again.
                _git_runner.note_timed_out()
            elif not seen.listed:
                # Git failed on its names: counted as that failure again.
                _git_runner.note_failed()
            return seen
        if isinstance(seen, origin_history.HistoryUnread):
            if seen is origin_history.HistoryUnread.TIMEOUT:
                _git_runner.note_timed_out()
            else:
                _git_runner.note_failed()
            origin_history.note_unwalked(seen)
        elif seen:
            _git_runner.note_failed()
        return None
    failed = _git_runner.failed_calls()
    timed_out = _git_runner.timed_out_calls()
    walk = _walk_reachable(toplevel, anchor, head)
    if walk is None and _git_runner.failed_calls() != failed:
        # The names were not read, for whatever reason: a timeout, or a
        # tree git could not read (a treeless clone, a corrupt object),
        # where the author dates miss a rebased commit (ENG-220's review,
        # round F). The commits alone, no diff read.
        names_timed_out = _git_runner.timed_out_calls() != timed_out
        timed_out = _git_runner.timed_out_calls()
        listing = _git_runner.failed_calls()
        walk = _walk_reachable(toplevel, anchor, head, names=False)
        why: origin_history.HistoryUnread | None = None
        if walk is None and _git_runner.timed_out_calls() != timed_out:
            why = origin_history.HistoryUnread.TIMEOUT
        elif walk is None and _git_runner.failed_calls() != listing:
            # The listing failed too: the dates are the basis only where
            # the repository holds no such anchor, as for a walk git
            # refused (ENG-220's review, round G).
            why = _held_anchor(toplevel, anchor)
        if why is not None:
            origin_history.note_unwalked(why)
            if names_timed_out or why is origin_history.HistoryUnread.TIMEOUT:
                _keep_timed_out_walk(key, why, toplevel, held)
            if walked is not None:
                walked[within] = why
            return None
        if walk is not None and names_timed_out:
            walk = dataclasses.replace(walk, timed_out=True)
    if walk is None:
        if walked is not None:
            walked[within] = _git_runner.failed_calls() != failed
        return None
    if key is None or not walk.listed:
        if walk.timed_out:
            _keep_timed_out_walk(key, walk, toplevel, held)
        if walked is not None:
            walked[within] = walk
        return walk
    if walk_files_signature(toplevel) == files and directories_held(held):
        with _WALK_MEMO_LOCK:
            _WALK_MEMO[key] = walk
            while len(_WALK_MEMO) > _WALK_MEMO_CAP:
                _WALK_MEMO.popitem(last=False)
    return walk


def _held_anchor(toplevel: Path, anchor: str) -> origin_history.HistoryUnread | None:
    """Why a walk from `anchor` whose names and listing git both failed on
    is given up on: ``ERROR`` where the repository holds the commit
    (``git cat-file -e <anchor>^{commit}`` exits 0), since git then failed
    on a range it holds, where the author dates miss a rebased commit
    (ENG-220's review, round G); None, and the dates, where it exits
    non-zero (1 or 128: no such object, or one that is no commit), an
    anchor this repository does not hold. A check that gives no answer is
    no evidence the anchor is gone: one killed by a signal (a negative
    exit) reads ``ERROR``, one that met its timeout ``TIMEOUT``, one that
    found no binary or directory None and any other spawn failure
    ``ERROR`` (`origin_history.unanswered`). Git fetches no missing object
    for it (`_git.NO_FETCH`)."""
    before = origin_history.git_counts()
    held = _git_result(toplevel, "cat-file", "-e", f"{anchor}^{{commit}}", timeout=5.0)
    if held is None:
        return origin_history.unanswered(before, named=True)
    return None if held.returncode > 0 else origin_history.HistoryUnread.ERROR


def _walk_reachable(
    toplevel: Path, anchor: str, head: str, *, names: bool = True
) -> ReachableWalk | None:
    """The walk from `anchor` to `head`, both named: the memo keys it on
    `head`, so the range must end there even if HEAD has moved since the
    caller read it. Without `names`, the commits alone, no diff read."""
    if anchor == head:
        return ReachableWalk(anchor=anchor, head=head, commits=(), touched={})
    # Non-ASCII paths arrive as their UTF-8 spelling rather than
    # octal-escaped, so they compare equal to a resolved pathspec; the
    # residual quoting git still applies (embedded quotes, control bytes)
    # is decoded below. A rename is a change to BOTH paths: the cited old
    # path stops existing, which is exactly what a memory about it should
    # see.
    named = (*origin_history.AS_STORED_DIFF, "--no-renames", "--name-only")
    raw = _git(
        toplevel,
        *(("-c", "core.quotePath=false") if names else ()),
        "log",
        "--boundary",
        f"--format={_WALK_MARK}%m%H",
        *(named if names else ()),
        f"{anchor}..{head}",
        timeout=5.0,
        empty_ok=True,
        verbatim=True,
    )
    if raw is None:
        return None
    if not raw:
        # HEAD is reachable from the anchor but is not it: the checkout
        # moved backwards. Not a count of anything.
        return None
    commits: list[str] = []
    touched: dict[str, set[str]] = {}
    boundary: set[str] = set()
    current: str | None = None
    # One name a line, with only its newline taken off: git quotes a name
    # holding a control character (a newline, a CR, a tab) or a quote, so
    # no name spans two lines, and prints a space that begins or ends one
    # as it is. A CR in a name is carried as its escape, and git's output is
    # read with every CR kept (`_git_runner.run_git`), so no CR ends a line.
    for line in raw.split("\n"):
        if line.startswith(_WALK_MARK):
            mark, sha = line[1:2], line[2:].strip().lower()
            if not is_full_commit_sha(sha):
                # A mark git printed names a commit: one that names none
                # was garbled, and its commit would drop out of the count.
                _git_runner.note_failed()
                return None
            if mark == "-":
                boundary.add(sha)
                current = None
            else:
                commits.append(sha)
                current = sha
            continue
        path = line
        if not path or current is None:
            continue
        if path.startswith('"') and path.endswith('"'):
            decoded = _unquote_c_path(path)
            if decoded is not None:
                path = decoded
        touched.setdefault(path, set()).add(current)
    if anchor not in boundary:
        # Commits were listed, but none of them descends from the
        # anchor: a rewritten history. The range measures the distance
        # from the merge base, not from the verified tree state.
        return None
    cut = origin_history.cuts(toplevel)
    if cut is None or cut.intersection(commits):
        # The range holds a shallow file's cut, behind which git read none
        # of the range's commits (ENG-220's review).
        return None
    return ReachableWalk(
        anchor=anchor,
        head=head,
        commits=tuple(commits),
        touched={path: frozenset(shas) for path, shas in touched.items()},
        listed=names,
    )


def resolve_repo_pathspecs(
    cwd: Path | None,
    paths: list[str],
    *,
    toplevel: Path | None = None,
) -> list[str] | None:
    """Resolve `paths` into repo-root-relative, forward-slash pathspecs.

    `paths` may contain absolute paths, ``~/``-prefixed paths, or paths
    relative to the repo root. We expand ``~`` and resolve absolute
    paths against the repo root so git sees a relative pathspec — git
    won't filter on absolute paths that escape the repo. Paths outside
    the repo (or that don't resolve) are dropped silently, and so is the
    repo root itself: a root citation ("the project lives at X") is a
    location claim, not a content claim, and as a pathspec it would be
    ``.`` — matching every commit, i.e. the unfiltered count in
    disguise.

    The return-shape distinction is load-bearing for claim-anchored
    commit drift and must not be collapsed:

    - ``None`` — git itself couldn't answer (cwd is None, git not on
      PATH, not a repo). The caller can't judge anchoring at all and
      should fall back to its conservative default (the unfiltered
      commit count) rather than under-report drift.
    - ``[]`` (empty list) — git answered fine, but EVERY input path is
      unresolvable or escapes this repo. The claims exist, they just
      don't anchor into the repo the caller is sitting in — commit
      drift is *not applicable*, not merely unfilterable.

    `toplevel`, when provided, skips the per-call ``rev-parse`` — see
    `repo_toplevel`.
    """
    if cwd is None:
        return None
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
        if toplevel is None:
            return None

    # Build repo-root-relative, FORWARD-SLASH pathspecs; rev-list later runs
    # FROM the repo root (`toplevel`), not the caller's `cwd`. The caller's cwd
    # may be a SUBDIRECTORY of the repo (an MCP server / agent launched from or
    # chdir'd into `src/`, `packages/foo/`, …); git resolves a plain pathspec
    # relative to the invocation cwd, so a root-relative `src/foo.py` would
    # match nothing from a subdir and rev-list would return 0 — silently
    # reporting a genuinely-drifted verified path as clean. Anchoring rev-list
    # at `toplevel` makes the repo-root-relative pathspecs correct regardless
    # of cwd, with none of git's pathspec-magic. `as_posix()` keeps the
    # pathspecs forward-slashed (str(Path) yields backslashes on Windows, which
    # git pathspecs reject). A relative input is resolved against the repo root
    # (its documented meaning); anything that escapes the repo — including a
    # Windows drive-relative path like `\foo` that joins onto a different root
    # — is dropped, and the caller decides what an all-dropped (empty) result
    # means: not-applicable for the claim-anchored policy, unfiltered fallback
    # for the legacy composition below.
    pathspecs: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = toplevel / candidate
            candidate = candidate.resolve()
            # Anchored by the directory, not only the spelling: a path
            # spelled in another case on macOS still names this tree.
            rel = relative_to_root(candidate, toplevel)
        except (OSError, ValueError):
            # Unresolvable, or escapes the repo — git can't filter on
            # something outside its working tree.
            continue
        if not rel.parts:
            # The input resolved to the repo root itself. Its existence
            # is path drift's axis; as a commit anchor the pathspec
            # would be "." — every commit touches it — silently
            # reproducing the unfiltered noise claim-anchoring exists
            # to remove. Drop it like any other non-discriminating
            # input and let an all-dropped result mean what it always
            # means: nothing here anchors this repo's history.
            continue
        pathspecs.append(rel.as_posix())

    return pathspecs


# Ceiling on how many named commits `commit_patch_stream` will fetch
# patches for. Past this, the claim-level narrowing falls back to the
# incumbent per-file count rather than pulling megabytes of patch text
# onto a read path — a memory whose claimed files saw hundreds of
# commits since its last verify is loudly drifted under EITHER signal,
# so the expensive precision buys nothing there.
MAX_PATCH_STREAM_COMMITS = 256


def commit_patch_stream(
    cwd: Path | None,
    shas: list[str],
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> str | None:
    """The `-U0` patch stream for exactly `shas`, filtered to `pathspecs`.

    The input `claims.build_binding_index` parses. ``--no-walk=unsorted``
    diffs each NAMED commit against its parent without walking history —
    the caller has already decided which commits are in the window (the
    post-`since` slice of
    `origin_dates.commit_author_sha_pairs_touching_pathspecs`,
    author-date space), so a rev-range walk here would re-answer that
    question in commit-graph space and disagree under rebases.

    THE DIFF SHAPE IS PINNED AT THE INVOCATION. The parser hard-requires
    unquoted ``--- a/<path>`` / ``+++ b/<path>`` headers (``/dev/null``
    on the created/deleted side), and this call otherwise inherits the
    user's git config, which can silently reshape them:

    * ``diff.noprefix=true`` drops both prefixes. A DELETION then
      parses to nothing — ``--- mod.py`` no longer matches ``--- a/``,
      so no path is in hand when ``+++ /dev/null`` arrives, the hunk is
      skipped, and `parse_mismatches` stays 0. A deleted claimed file
      measures zero drift and reads FRESH: the exact false-fresh the
      trust machinery exists to prevent, with no loud-failure tripwire.
    * ``diff.srcPrefix`` / ``diff.dstPrefix`` (git >= 2.45) and
      ``diff.mnemonicPrefix`` substitute arbitrary prefixes — every
      file's path is then indexed under the wrong spelling.
    * ``core.quotePath=true`` (git's DEFAULT) octal-escapes non-ASCII
      bytes, so a claimed ``modül.py`` is indexed as its quoted
      spelling and never equals the claim's rel_path.
    * ``color.ui=always`` (or ``color.diff=always``) wraps every header
      and hunk line in ANSI escapes: no header matches, and a deletion
      parses to nothing as under ``diff.noprefix``, silently.
    * ``diff.interHunkContext`` merges two ``-U0`` hunks closer than its
      value into one with the lines between them as context, which the
      exact count reads as a mismatch, and the later hunk's bindings are
      lost.

    Each is pinned back to the parser's shape with ``-c``, which
    outranks every config file (older gits ignore the >= 2.45 keys), and
    colour with ``--no-color``, which outranks the configuration too.
    Paths git quotes even with quotePath off — embedded quotes,
    backslashes, control bytes — are rewritten to the unquoted spelling
    by `_dequote_patch_headers` before the stream is returned, so the
    normalization lives with the producer and the parser keeps its
    one-argument guarantee.

    Every file is diffed as text, whatever its attributes (``-diff``,
    ``binary``) or ``core.bigFileThreshold`` say: a "Binary files differ"
    line carries no header and no hunk, so beside an indexed file its
    claims read untouched; renames are read as a deletion and a creation,
    a root commit as its creation diff, and a gitlink's change is never
    ignored (`origin_history.AS_STORED_CONFIG`, `AS_STORED_DIFF`). What
    the attributes still decide (a driver's diff algorithm, which moves
    only lines whose text is unchanged) is read from the working tree's
    ``.gitattributes`` files, ``info/attributes`` and the global
    attributes file, which `attribute_files_signature` and
    `gitattributes_signature` name for a memo that keys on this stream.

    No program the configuration names runs on this read. ``git log -p``
    runs a ``diff.<driver>.textconv`` program by default, for every file
    whose attributes name the driver; ``--no-textconv`` keeps it off, and
    the diff is then of the blob's own text, the text a claim reads from
    the file, not the program's conversion. ``--no-ext-diff`` keeps an
    external diff driver off, as ``log`` does by default. The signature
    check ``log.showSignature`` would run is pinned off for every call,
    and a gitlink's change is kept to git's default ``-/+Subproject
    commit`` lines (``diff.submodule=short``, `_git.PINS`): ``diff`` would start a git in
    the submodule, which honours the submodule's own ``diff.external``
    (ENG-218).

    ``--format=<COMMIT_MARK>%H`` writes the same control-character
    record separator the bench streams carry; source content cannot
    collide with it. Returns the raw stream ("" is a real value: the
    named commits touched the specs only in merge diffs, which `-p`
    skips), or None on any git failure — the caller falls back to the
    incumbent count, never under-counting on infrastructure failure. A
    blob a partial clone lacks fails it too, and the caller then streams
    the commits whose blobs are local (`origin_patch`, ENG-220).

    The timeout is 5s rather than `_git`'s 1s default: a patch log over
    a bounded SHA list is more work than a `%aI` format log, and this
    call sits behind two gates (drift already measured positive, SHA
    count under `MAX_PATCH_STREAM_COMMITS`) so it is rare by
    construction.
    """
    if cwd is None or not shas or not pathspecs:
        return None
    if len(shas) > MAX_PATCH_STREAM_COMMITS:
        return None
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
        if toplevel is None:
            return None
    from .claims import COMMIT_MARK

    stdin = origin_history.log_stdin(shas, pathspecs)
    if stdin is None:
        return None
    raw = _git(
        toplevel,
        # Global `-c` options must precede the subcommand. See the
        # docstring for why each key is pinned.
        "-c",
        "diff.noprefix=false",
        "-c",
        "diff.srcPrefix=a/",
        "-c",
        "diff.dstPrefix=b/",
        "-c",
        "diff.mnemonicPrefix=false",
        "-c",
        "core.quotePath=false",
        "-c",
        "diff.interHunkContext=0",
        *origin_history.AS_STORED_PATHS,
        "log",
        *origin_history.AS_STORED_DIFF,
        "--no-walk=unsorted",
        "-p",
        "--no-color",
        "--no-textconv",
        "--no-ext-diff",
        "-U0",
        f"--format={COMMIT_MARK}%H",
        # The commits and pathspecs on standard input, so no window passes
        # a command line's bound (`origin_history.log_stdin`).
        "--stdin",
        timeout=5.0,
        empty_ok=True,
        stdin=stdin,
    )
    if raw is None:
        return None
    return _dequote_patch_headers(raw)


__all__ = [
    "MAX_PATCH_STREAM_COMMITS",
    "Origin",
    "attribute_files_signature",
    "capture",
    "capture_caller",
    "commit_patch_stream",
    "commit_reachable",
    "commits_since_anchor",
    "directories_held",
    "failed_git_calls",
    "files_held",
    "follow_unsettled",
    "gitattributes_signature",
    "head_sha",
    "is_full_commit_sha",
    "located_head",
    "ReachableWalk",
    "repo_toplevel_and_head",
    "repo_toplevel",
    "repos_match",
    "resolve_repo_pathspecs",
    "should_include_for_caller",
    "toplevel_and_head_from_files",
    "unanswered_git_calls",
    "walk_files_signature",
    "worktrees_match",
]
