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

import errno
import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias

from . import _git as _git_runner
from . import _caches, githead, identity, origin_history

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
    _unquote_c_path,
    attribute_files_signature,
    capture_files,
    directories_held,
    first_not_a_directory,
    gitattributes_signature,
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
    (`Origin.git_indeterminate`).

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
    the signature does not see (a remote changed outside the repository's
    config file, among the rest `ORIGIN_CACHE_SECONDS` lists) can be
    answered from a capture up to ten minutes old.
    """
    if cwd is None:
        declared = identity.workspace_declaration()
        if declared is not None:
            cwd, source = declared
        elif source is None:
            source = identity.SOURCE_PROCESS_CWD
    if cwd is None:
        try:
            resolved = Path.cwd().resolve()
        except (FileNotFoundError, OSError):
            return Origin(source=source)
    else:
        resolved = cwd.resolve()
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
#: ten minutes where it was served for two seconds: a remote or its URL
#: changed outside the repository's config file (a `url.<base>.insteadOf`
#: rule in the global config, a legacy `.git/remotes` or `.git/branches`
#: file, a file an `include.path` names); such a rename on a platform
#: other than macOS whose filesystem ignores case or normalisation (the
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


# git's ref_rev_parse_rules (refs.c), in order: the names `git
# symbolic-ref --short HEAD` tries a short name against.
_REV_PARSE_RULES = (
    "{}",
    "refs/{}",
    "refs/tags/{}",
    "refs/heads/{}",
    "refs/remotes/{}",
    "refs/remotes/{}/HEAD",
)


def _shortening_refs(refname: str) -> list[str]:
    """The refs whose existence decides how `git symbolic-ref --short
    HEAD` shortens `refname` (``refs_shorten_unambiguous_ref``): for each
    rule after the first that `refname` matches, the short name it gives
    spelled under every other rule. Git keeps a short name only while none
    of the refs it tries for it exists, and it tries a subset of these (the
    rules before the matched one, not strict); for ``refs/heads/<b>`` it
    tries ``<b>``, ``refs/<b>`` and ``refs/tags/<b>``, then ``heads/<b>``,
    and ``refs/remotes/<b>`` and ``refs/remotes/<b>/HEAD`` are in the set
    too."""
    names: list[str] = []
    for index in range(len(_REV_PARSE_RULES) - 1, 0, -1):
        prefix, _, suffix = _REV_PARSE_RULES[index].partition("{}")
        if not (
            refname.startswith(prefix)
            and refname.endswith(suffix)
            and len(refname) > len(prefix) + len(suffix)
        ):
            continue
        short = refname[len(prefix) : len(refname) - len(suffix)]
        names.extend(
            rule.format(short)
            for other, rule in enumerate(_REV_PARSE_RULES)
            if other != index
        )
    return list(dict.fromkeys(names))


def _capture_signature(start: Path) -> githead.Signature:
    """The change detector a capture of `start` is kept against: the
    directory's `githead.signature` (the git directory, HEAD, the loose refs
    HEAD's chain reads, packed-refs, config and the variables that move
    discovery), from the repository `githead.locate` finds where githead's
    discovery declines (`origin_gitfiles.capture_files`: off POSIX, and in
    another account's repository), with the stamp of config.worktree, where git reads
    core.worktree under extensions.worktreeConfig; the root spelled as git
    prints it (`_spelled_as_git`: the kernel's path on macOS, so a rename
    of the root or a directory above it that changes only the case or the
    Unicode normalisation of a name, under which the caller's old spelling
    still resolves, is seen; the resolved path elsewhere, which keeps the
    caller's spelling); and the stamp of each ref file that decides the
    branch's short name (`_shortening_refs`), in the git directory and in
    the common directory. Where the files do not settle the repository,
    the base signature already equals no other; where a stamp or the
    spelling cannot be read, a new object makes this one equal no other
    either."""
    base, gd = capture_files(start)
    if gd is None:
        return base
    try:
        extra: list[object] = [githead.stamp(gd.gitdir / "config.worktree")]
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
# stamps of its config, of config.worktree, of HEAD itself, of the loose
# refs HEAD's chain reads, of packed-refs and of the ref files that decide
# the branch's short name, the root spelled as git prints it, and the
# GIT_DIR, GIT_WORK_TREE and GIT_CEILING_DIRECTORIES values, so a branch
# switch, a remote changed in the repository's config, core.worktree set in
# config.worktree, a tag or ref named like the branch, a repository created
# or removed on the path, a rename that changes only a name's case or
# normalisation (on macOS), or a change to one of those variables is never
# answered from an entry, and no capture is kept whose probes ran while
# HEAD moved to another branch and back (git rewrites HEAD through a
# rename, which leaves a new inode and ctime); where the files do not
# settle what git would find, the signature equals no other and every
# capture asks git. What the signature does not see (whether git can run,
# configuration outside the repository's config files, such a rename on
# another platform's case-ignoring filesystem) holds for at most the
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


# Errnos where the OS ANSWERED the liveness question — "is there
# anything at this path?" — with "no". Each is a property of the path
# itself failing to resolve to an object, independent of who is asking
# and of whether any device is reachable:
#
#   ENOENT        a component of the path does not exist
#   ENOTDIR       a component that would have to be a directory is not
#                 one, so nothing can live below it
#   ELOOP         the path cycles through symlinks and resolves to
#                 nothing
#   ENAMETOOLONG  this system cannot name an object at that path at
#                 all — the shape a store synced from a longer-path OS
#                 arrives in
#
# The complement is deliberately NOT enumerated: EACCES/EPERM on a
# parent, the unreachable-device family (ENOTCONN, EHOSTDOWN,
# ETIMEDOUT, ESTALE, EIO, ENODEV, …), and any errno a future platform
# invents all fall through to "cannot tell". Listing the gone side and
# treating every unclassified errno as indeterminate is what makes the
# never-widen direction the default: a new error class holds the
# isolation boundary rather than opening it.
_WORKTREE_GONE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG}
)

# Windows-only companion, for the codes CPython does not fold into one
# of the errnos above. Both are path-intrinsic in the same sense:
# ERROR_INVALID_NAME (123) is "that syntax cannot name anything",
# ERROR_CANT_RESOLVE_FILENAME (1921) is the reparse-point cycle.
# `pathlib` treats both as not-exists too, and we match it there.
#
# Where we deliberately DIVERGE from `pathlib`: ERROR_NOT_READY (21) —
# "the drive exists but is not accessible", i.e. a removable or
# disconnected volume — is absent. `Path.exists()` reports it as
# not-exists; for an isolation boundary that is the unmounted-volume
# fail-open this classification exists to close, so it lands in
# "cannot tell".
_WORKTREE_GONE_WINERRORS = frozenset({123, 1921})


def _worktree_root_is_gone(worktree: str) -> bool:
    """Whether the OS positively reported that nothing exists at
    `worktree`, as `worktrees_match` reads it for the rc3_admitted shadow
    column (design 8.11)."""
    try:
        os.stat(worktree)
    except ValueError:
        # `os.stat` rejected the string before the OS ever saw it (an
        # embedded NUL, an un-encodable surrogate). Nothing can live at
        # a path this process cannot even ask about, so this is an
        # answer, not a refusal to answer.
        return True
    except OSError as exc:
        winerror: object = getattr(exc, "winerror", None)
        if exc.errno in _WORKTREE_GONE_ERRNOS or winerror in _WORKTREE_GONE_WINERRORS:
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
    cwd: Path, *args: str, timeout: float = 1.0
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
    """
    return _git_runner.run_git(args, cwd=cwd, timeout=timeout)


def _git(
    cwd: Path, *args: str, timeout: float = 1.0, empty_ok: bool = False
) -> str | None:
    """Run a git command from `cwd`. Returns trimmed stdout on success,
    None on any failure. Short timeout so a hanging git never stalls a
    memory_write — the write is the user-facing operation; origin is
    nice-to-have. Built on `_git_result`, which keeps the two kinds of
    failure apart for the callers that need them apart.

    `empty_ok` splits "git ran fine, no output" from "git could not run":
    with it True a zero-exit call with EMPTY stdout returns ``""`` instead
    of None, so a caller can tell a clean-but-empty result (`git log --
    <specs>` listing no commit) apart from an actual failure (non-zero
    exit, missing binary, timeout — still None). Default False keeps the
    historical ``out or None`` collapse every other caller relies on.
    Every None is counted on the calling thread (`failed_git_calls`).

    Failure logging is tiered so the common "not a repo" case stays
    silent while operationally interesting failures (missing binary,
    timeouts, safe.directory rejection, corrupted .git) reach the log:
    the could-not-run failures at WARNING (`_git.run_git`), a non-zero
    exit at DEBUG with stderr's first line (`_git.log_nonzero_exit`),
    since the vast majority of those are "fatal: not a git repository"
    from a non-repo cwd, which is fully expected (memories written
    outside any repo get `repo=None`).
    """
    result = _git_result(cwd, *args, timeout=timeout)
    if result is None:
        _UNANSWERED.count += 1
        return None
    if result.returncode != 0:
        _git_runner.log_nonzero_exit(args, result, cwd)
        _git_runner.note_failed()
        return None
    out = result.stdout.strip()
    if out or empty_ok:
        return out
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
    it refused, None otherwise.

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
    raw = result.stdout.strip()
    if not raw:
        return None, False, None
    try:
        return str(Path(raw).resolve()), False, None
    except OSError:
        return raw, False, None


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
    toplevel_raw = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel_raw is None:
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
    raw = _git(cwd, "rev-parse", "--show-toplevel", "HEAD")
    if raw is None:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 2 or not is_full_commit_sha(lines[1]):
        return None
    try:
        return Path(lines[0]).resolve(), lines[1]
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
    plain false, and a file git refuses, that holds a NUL byte or that
    cannot be read decline (`_config_moves_worktree`), and so does
    everything `githead` declines.

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
    if head is None or _config_moves_worktree(gd):
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


@dataclass(frozen=True)
class ReachableWalk:
    """The commits reachable from `head` but not from `anchor` — the
    range ``anchor..HEAD`` — with the paths each one changed.

    Built only when `anchor` is an ancestor of `head` (or is `head`
    itself, an empty walk), so `commits` is exactly what has landed on
    top of the verified tree state: a branch authored before the stamp
    and merged after it is in here, which is what the author-date count
    cannot say. `touched` maps a repo-relative path to the commits in
    the range whose diff carried it; a merge commit contributes nothing
    there, as `git log --name-only` shows no diff for one, so its
    branch's own commits carry the paths.
    """

    anchor: str
    head: str
    commits: tuple[str, ...]
    touched: Mapping[str, frozenset[str]]

    def shas_touching(self, pathspecs: Sequence[str]) -> set[str]:
        """The commits in the range that changed a file matching any of
        `pathspecs` — repo-root-relative forward-slash specs, the output
        of `resolve_repo_pathspecs`, each a file or a directory prefix.
        A path is a file or a directory in git, never both, so an exact
        hit skips the prefix scan."""
        out: set[str] = set()
        for spec in pathspecs:
            exact = self.touched.get(spec)
            if exact is not None:
                out |= exact
                continue
            prefix = spec.rstrip("/") + "/"
            for path, shas in self.touched.items():
                if path.startswith(prefix):
                    out |= shas
        return out


# The reachable walk is memoised per process, keyed on (root, anchor,
# head) and the files beside the history that decide what its listing
# shows (`walk_files_signature`: the repository's config, the working
# tree's .gitmodules, and the index's .gitmodules entry where git reads
# it). The walk names both ends (`_walk_reachable` runs to the key's
# head, not to whatever HEAD names by then), and the range between two
# named commits never changes, so an entry is never stale while those
# files read as they did; the key's `head` is what lets a long-lived
# server stop reusing a walk the moment a commit lands, and the rest
# what stops it once log.showRoot or a submodule's ignore setting is
# written, in the working tree's .gitmodules or the index's copy, or the
# index's copy goes unmerged. A write to the index that leaves its
# .gitmodules entry as it was keys no new walk. A walk is kept only when
# those files read the same after it as before it and neither the root
# directory nor the index file moved meanwhile, so a .gitmodules created
# and removed while git ran is not kept, nor is a walk during which the
# index was written. Where they cannot be read (`githead` declines: off
# POSIX, under GIT_DIR; GIT_INDEX_FILE is set; the index is split), no
# walk is kept. What the stamps do not see
# (git's configuration outside the repository's own config files, a
# history rewritten under an unchanged head, and the
# two changes undone mid-walk `walk_files_signature` names) reads the
# memoised walk until the head moves; while files are created and removed
# in the root steadily, no walk is kept (`_hold_directory`). Bounded so a
# store with many distinct anchors cannot grow it without limit. Only a
# walk is stored, never a None: a None is also what a git process that
# failed yields (an object briefly unreadable, a timeout), and the
# fallback taken on a failure must not outlive the call that met it. A
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
_WALK_MEMO_CAP = 128
_WalkKey: TypeAlias = tuple[str, str, str, tuple[object, ...]]
_WALK_MEMO: OrderedDict[_WalkKey, ReachableWalk] = OrderedDict()
_WALK_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_walk_memo() -> None:
    with _WALK_MEMO_LOCK:
        _WALK_MEMO.clear()


# Record separator for the walk's format line, the same control
# character the patch stream carries: a path cannot contain it.
_WALK_MARK = "\x01"


def commits_since_anchor(
    cwd: Path | None,
    anchor: str,
    *,
    toplevel: Path | None = None,
    head: str | None = None,
    walked: dict[tuple[str, str, str], ReachableWalk | bool] | None = None,
) -> ReachableWalk | None:
    """The reachable walk from `anchor` to HEAD, or None when the count
    must fall back to author-date space.

    One git process per distinct (root, anchor, head) and stamps of the
    files beside the history (`walk_files_signature`), the walks memoised
    (see `_WALK_MEMO`) and the Nones not.
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
    the process-wide memo could not key (the stamps unreadable). A later
    call for the same key in that pass returns the remembered walk, or
    None without a process, counting the failure again on the calling
    thread (`failed_git_calls`), so a caller that keeps a value only when
    no git call failed judges every hit at that anchor as it judged the
    first.

    None, and the author-date fallback, when: `anchor` is not a full
    hash (never handed to git as a revision), git cannot answer, the
    anchor does not resolve here, it is not an ancestor of HEAD (a
    rewritten history), or HEAD sits behind it. `toplevel` and `head`
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
    if walked is not None and within in walked:
        seen = walked[within]
        if isinstance(seen, ReachableWalk):
            return seen
        if seen:
            _git_runner.note_failed()
        return None
    failed = _git_runner.failed_calls()
    walk = _walk_reachable(toplevel, anchor, head)
    if walk is None:
        if walked is not None:
            walked[within] = _git_runner.failed_calls() != failed
        return None
    if key is None:
        if walked is not None:
            walked[within] = walk
        return walk
    if walk_files_signature(toplevel) == files and directories_held(held):
        with _WALK_MEMO_LOCK:
            _WALK_MEMO[key] = walk
            while len(_WALK_MEMO) > _WALK_MEMO_CAP:
                _WALK_MEMO.popitem(last=False)
    return walk


def _walk_reachable(toplevel: Path, anchor: str, head: str) -> ReachableWalk | None:
    """The walk from `anchor` to `head`, both named: the memo keys it on
    `head`, so the range must end there even if HEAD has moved since the
    caller read it."""
    if anchor == head:
        return ReachableWalk(anchor=anchor, head=head, commits=(), touched={})
    raw = _git(
        toplevel,
        # Non-ASCII paths arrive as their UTF-8 spelling rather than
        # octal-escaped, so they compare equal to a resolved pathspec;
        # the residual quoting git still applies (embedded quotes,
        # control bytes) is decoded below.
        "-c",
        "core.quotePath=false",
        "log",
        "--boundary",
        # A rename is a change to BOTH paths: the cited old path stops
        # existing, which is exactly what a memory about it should see.
        "--no-renames",
        f"--format={_WALK_MARK}%m%H",
        "--name-only",
        f"{anchor}..{head}",
        timeout=5.0,
        empty_ok=True,
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
    for line in raw.split("\n"):
        if line.startswith(_WALK_MARK):
            mark, sha = line[1:2], line[2:].strip().lower()
            if mark == "-":
                boundary.add(sha)
                current = None
            elif is_full_commit_sha(sha):
                commits.append(sha)
                current = sha
            else:
                current = None
            continue
        path = line.strip()
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
    return ReachableWalk(
        anchor=anchor,
        head=head,
        commits=tuple(commits),
        touched={path: frozenset(shas) for path, shas in touched.items()},
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


def _instant(stamp: datetime) -> float:
    """Absolute-instant sort key for timezone-aware author timestamps.

    One `utcoffset()` per element instead of one per comparison: `%aI`
    keeps each author's own UTC offset, so a list parsed from it carries
    many distinct tzinfo objects and CPython's same-tzinfo comparison fast
    path never fires. Same ordering either way; both are the instant.
    """
    return stamp.timestamp()


def commit_author_timestamps(
    cwd: Path | None, *, located: tuple[Path, str] | None = None
) -> list[datetime] | None:
    """All author timestamps from the HEAD history of `cwd`'s repo.

    Returns a list of timezone-aware datetimes, or None on any failure
    (cwd is None, git not on PATH, not a repo, no commits, parse error
    on every line). An empty list is theoretically possible but almost
    never happens — `git log` on a repo with no commits exits non-zero
    and we surface that as None. Lines that fail to parse are skipped
    individually rather than poisoning the whole result.

    Returned ASCENDING, ready to `bisect_right`. Every caller wants that
    order and none wants git's; leaving the sort to them meant the
    per-memory `compute_commit_drift` re-sorted the repo's whole history
    on every row, which the rot benchmark measured at 38ms x 2,163 calls
    = 82s of a 90s scipy run. Sorting here happens once per git
    invocation, beside the fork+exec that already dominates the call.

    Read as ``%at``, the author date's seconds, each date built in UTC
    (`origin_history.author_dates`): the instant ``%aI`` names, with one tzinfo for the
    whole list, so the comparisons a sort and a bisect make take CPython's
    same-tzinfo fast path, and a date holds 56 bytes with its slot where
    one parsed with its author's own offset held about 106.

    Used by the health rollup to count commits-since for many memories
    from one git invocation; a per-memory ``git rev-list --count`` would
    otherwise pay a fork+exec for every row.

    MEMOISED per (root, head) (`origin_history`). `located` is the root
    and the head as the caller already resolved them for `cwd`; without
    it they are read from the repository's files
    (`toplevel_and_head_from_files`). The log then names the head by its
    hash, so the list stored under a head is that head's history even if
    a commit lands while git runs. A head whose repository holds an entry
    for an earlier head reads only what it adds (`_extended_author_
    timestamps`, one ``<earlier>..<head>`` log) where the earlier head is
    its ancestor and the files that change which commits a head reaches
    read as they did (`origin_history.history_guard`); elsewhere it reads
    its whole history. A read that fails is remembered for the key for a
    minute: a call meanwhile returns None without a process and counts the
    failure on the calling thread (`failed_git_calls`), as the read did, so
    a caller that keeps a value only when no git call failed keeps none
    from it. Where the files git's discovery reads do not settle the pair
    and the caller passed none (off POSIX, in another account's
    repository), the root and the head are those `githead.locate` reads
    (`origin_history.located_key`), kept and extended alike; only where
    those do not settle it either does the log run from HEAD, uncached. A
    history longer than the memo's budget (`origin_history.remember`) is
    returned, and remembered as a failed read is, so the calls of the next
    minute answer None without reading it again. A hit returns the stored
    list itself: every caller only bisects it. The key does not see a
    history rewritten under an unchanged head (a ``git replace`` ref or a
    graft written while the head stays), which reads the earlier walk
    until the head moves.
    """
    if cwd is None:
        return None
    if located is None:
        located = toplevel_and_head_from_files(cwd)
    key = (
        origin_history.located_key(cwd)
        if located is None
        else (str(located[0]), located[1])
    )
    if key is None:
        return _read_author_timestamps(cwd, "HEAD")
    stored, failed = origin_history.recall(key)
    if stored is not None:
        return stored
    if failed:
        _git_runner.note_failed()
        return None
    guard = origin_history.history_guard(Path(key[0]))
    base = None if guard is None else origin_history.base_for(key[0], guard)
    dates = None
    if base is not None:
        dates = _extended_author_timestamps(cwd, base[0], base[1], key[1])
    if dates is None:
        dates = _read_author_timestamps(cwd, key[1])
    if dates is None or not origin_history.remember(key, dates, guard):
        origin_history.remember_failure(key)
    return dates


def _read_author_timestamps(cwd: Path, revision: str) -> list[datetime] | None:
    """`commit_author_timestamps`' read: the author dates of every commit
    reachable from `revision`, ascending, or None."""
    # timeout=5.0, not the 1.0 default: the default is calibrated for
    # write-path origin capture, where origin is nice-to-have and a
    # hanging git must never stall a memory_write. This log and its two
    # path-filtered siblings below are READ legs — the drift verdicts
    # search/show/health surface ride on them, and at 1.0s a cold git
    # on a slow host (observed: the windows-latest CI runner) times out,
    # collapsing a real count into the omitted/conservative branch. Same
    # ceiling `commit_patch_stream` already runs at.
    raw = _git(cwd, "log", "--format=%at", revision, timeout=5.0)
    if raw is None:
        return None
    out = origin_history.author_dates(
        [
            value
            for line in raw.splitlines()
            if (value := origin_history.seconds(line)) is not None
        ]
    )
    return out or None


def _extended_author_timestamps(
    cwd: Path, base_head: str, base: list[datetime], head: str
) -> list[datetime] | None:
    """`base`, the ascending author dates of `base_head`'s history, with the
    dates of the commits `head` reaches and `base_head` does not, where
    `base_head` is an ancestor of `head`: ``git log --boundary
    <base_head>..<head>``, whose boundary holds `base_head` exactly then
    (`_walk_reachable` reads the same mark). The list is new and shares
    `base`'s dates. None, and the caller reads the history whole, where
    `base_head` is no ancestor (a switched or rewritten branch, a head that
    moved back, an object gone) or git did not answer; a non-zero exit is
    not counted as a failure, since the whole read follows it."""
    result = _git_result(
        cwd,
        "log",
        "--boundary",
        "--format=%m%H %at",
        f"{base_head}..{head}",
        timeout=5.0,
    )
    if result is None or result.returncode != 0:
        return None
    added: list[int] = []
    reaches = False
    for line in result.stdout.splitlines():
        sha, _, stamp = line[1:].partition(" ")
        if line.startswith("-"):
            reaches = reaches or sha == base_head
        elif (value := origin_history.seconds(stamp)) is not None:
            added.append(value)
    if not reaches:
        return None
    return sorted(base + origin_history.author_dates(added))


def commit_author_timestamps_touching_pathspecs(
    cwd: Path | None,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> list[datetime] | None:
    """Author timestamps of the commits touching any of `pathspecs`.

    `pathspecs` must already be repo-root-relative forward-slash specs (the
    output of `resolve_repo_pathspecs`). The path-filtered analogue of
    `commit_author_timestamps`: the same author dates (``%aI`` here, ``%at``
    there: one instant either way), so a caller can `bisect_right` the
    result against a `since` instant and get a count that lives in the SAME
    date space as the unfiltered bisect — no
    committer-vs-author mismatch. That is the whole point: `commits_touching_
    pathspecs` counts on COMMITTER date (git's ``--since``), which a rebase
    can inflate past the author-date truth, forcing a downstream clamp;
    reading author dates here removes the mismatch at the source.

    Three-valued return — the distinction the claim-anchored drift policy
    (`verify.resolve_commit_drift_count`) needs:

    - ``None`` — git could not answer (cwd is None, empty pathspecs, not a
      repo, non-zero git exit). The caller keeps its conservative default
      (never under-count on infrastructure failure).
    - ``[]`` — git answered cleanly and NO commit reachable from HEAD ever
      touched any spec. Every pathspec is a PHANTOM: a citation
      `resolve_repo_pathspecs` mapped LEXICALLY (no existence check) onto a
      repo-relative path no commit touched. No separate existence probe is
      needed: an empty author-date log IS the "no spec ever appeared in
      history" answer, so one git call answers both questions.
    - ``[ts, ...]`` — author timestamps (timezone-aware) of the touching
      commits, sorted ASCENDING and ready to `bisect_right` — same
      contract as `commit_author_timestamps`, for the same reason. A
      since-DELETED cited file still lands here: its removal is itself a
      commit that touched it, so it stays in the log — the real-not-phantom
      signal a drift anchor needs.

    The clean-empty (``[]``) vs failure (``None``) split rides on
    `_git(empty_ok=True)`: ``git log -- <specs>`` exits 0 with empty stdout
    for a phantom and non-zero when git itself can't run; the default
    ``out or None`` collapse would merge those two, so ``empty_ok`` keeps
    them apart. Non-empty stdout that parses to nothing (a git oddity, not a
    clean phantom) also degrades to ``None`` — stay conservative rather than
    mint a not-applicable exemption from garbage output.
    """
    if cwd is None or not pathspecs:
        return None
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw = _git(
        toplevel,
        "log",
        "--format=%aI",
        "HEAD",
        "--",
        *pathspecs,
        timeout=5.0,
        empty_ok=True,
    )
    if raw is None:
        # Git could not answer (non-zero exit, missing binary, timeout).
        return None
    if not raw:
        # Clean exit, empty log — no commit touches any spec (phantom).
        return []
    out: list[datetime] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = datetime.fromisoformat(line)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(ts)
    if not out:
        # Non-empty stdout that parsed to nothing is a git oddity, not an
        # answer. `None` (not `[]`) keeps it from minting the phantom
        # not-applicable exemption — the clean-empty case already returned
        # `[]` above, off `empty_ok`. Same split as the old `out or None`.
        return None
    out.sort(key=_instant)
    return out


def commit_author_sha_pairs_touching_pathspecs(
    cwd: Path | None,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> list[tuple[datetime, str]] | None:
    """`(author_instant, sha)` pairs for the commits touching `pathspecs`.

    The sibling of `commit_author_timestamps_touching_pathspecs` that
    keeps the commit identity beside each timestamp — the claim-level
    drift narrowing needs the POST-`since` SHAs by name (to fetch their
    patches and to union implicated commits into an exact distinct
    count), not just how many there are. Same three-valued contract and
    the same author-date space as the timestamps sibling: ``None`` when
    git could not answer, ``[]`` for the clean phantom (no commit ever
    touched any spec), pairs sorted ASCENDING on the instant otherwise —
    so a `bisect` against a `since` instant lands on the same boundary
    the count surfaces use.

    A separate function rather than a flag on the sibling because the
    return types differ and every existing caller of the sibling wants
    bare timestamps; threading a mode flag through the three-valued
    contract is how the None/[]-collapse bug gets reintroduced.
    """
    if cwd is None or not pathspecs:
        return None
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw = _git(
        toplevel,
        "log",
        "--format=%aI %H",
        "HEAD",
        "--",
        *pathspecs,
        timeout=5.0,
        empty_ok=True,
    )
    if raw is None:
        return None
    if not raw:
        return []
    out: list[tuple[datetime, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        stamp, _, sha = line.partition(" ")
        sha = sha.strip()
        if not sha:
            continue
        try:
            ts = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append((ts, sha))
    if not out:
        # Mirrors the timestamps sibling: non-empty stdout that parsed to
        # nothing is a git oddity, not a clean phantom.
        return None
    out.sort(key=lambda pair: _instant(pair[0]))
    return out


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
    post-`since` slice of `commit_author_sha_pairs_touching_pathspecs`,
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

    What each file's diff looks like is left to its attributes (``-diff``
    and ``binary`` print "Binary files differ"), read from the working
    tree's ``.gitattributes`` files, ``info/attributes`` and the global
    attributes file; ``--text`` would change what a repository with such
    attributes is served. `attribute_files_signature` and
    `gitattributes_signature` name those files for a memo that keys on
    this stream.

    No program the configuration names runs on this read. ``git log -p``
    runs a ``diff.<driver>.textconv`` program by default, for every file
    whose attributes name the driver; ``--no-textconv`` keeps it off, and
    the diff is then of the blob's own text, the text a claim reads from
    the file, not the program's conversion. ``--no-ext-diff`` keeps an
    external diff driver off, as ``log`` does by default. The signature
    check ``log.showSignature`` would run is pinned off for every call
    (`_git.PINS`).

    ``--format=<COMMIT_MARK>%H`` writes the same control-character
    record separator the bench streams carry; source content cannot
    collide with it. Returns the raw stream ("" is a real value: the
    named commits touched the specs only in merge diffs, which `-p`
    skips), or None on any git failure — the caller falls back to the
    incumbent count, never under-counting on infrastructure failure.

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
        "log",
        "--no-walk=unsorted",
        "-p",
        "--no-color",
        "--no-textconv",
        "--no-ext-diff",
        "-U0",
        f"--format={COMMIT_MARK}%H",
        *shas,
        "--",
        *pathspecs,
        timeout=5.0,
        empty_ok=True,
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
    "commit_author_sha_pairs_touching_pathspecs",
    "commit_author_timestamps",
    "commit_author_timestamps_touching_pathspecs",
    "commit_patch_stream",
    "commit_reachable",
    "commits_since_anchor",
    "directories_held",
    "failed_git_calls",
    "gitattributes_signature",
    "head_sha",
    "is_full_commit_sha",
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
