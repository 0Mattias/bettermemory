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
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urlparse

from pydantic import BaseModel, PrivateAttr

from . import _caches, githead, identity

log = logging.getLogger("bettermemory.origin")


class Origin(BaseModel):
    """Where a memory was written from. All fields optional.

    A memory with `origin = None` is global (e.g. written before this
    feature shipped). A memory with `origin.cwd` set but `origin.repo`
    null was written outside any git repo, or in a checkout with no
    remotes at all (`worktree_root` distinguishes the two: it is set in
    the latter case). A memory with `origin.repo` set but a different
    value from the caller's current repo is cross-project and gets
    filtered out by `auto_scope=True`.

    `worktree_root` is the path of the git worktree the write happened
    in — `git rev-parse --show-toplevel` from the cwd. Repo URL
    matching alone treats two worktrees of the same repository as the
    same workspace, which means notes written while debugging
    `feature-x` in `~/repo-feature-x/` would surface for the user when
    they switch to `~/repo-bug-fix/` and trigger an unrelated search.
    The audit named that "worktree leakage" — capturing the worktree
    root and using it as a secondary discriminator in the auto-scope
    filter closes the hole without forcing the user to re-tag every
    write. Null when the write didn't happen inside a git checkout
    (then there's no worktree distinction to draw) or for memories
    written before this field shipped (legacy memories pass through
    the worktree filter, mirroring how legacy `repo`-less memories
    are treated as global).
    """

    cwd: str | None = None
    repo: str | None = None  # raw remote URL or null
    branch: str | None = None  # current branch or null (detached HEAD → null)
    worktree_root: str | None = None  # `git rev-parse --show-toplevel` or null
    # Which channel named the directory this origin describes — one of
    # `identity.WORKSPACE_SOURCES` when `capture()` resolved it (`header`,
    # `env`, `roots`, or the labeled fallback `process-cwd`); None on a
    # record written before the field existed, or captured from an
    # explicit `cwd` whose caller said nothing. Persisted only when it is
    # NOT the process cwd, so a non-declaring client's frontmatter stays
    # byte-identical to the pre-field shape; the read surfaces label the
    # absent case `process-cwd` explicitly (`ResponseBuilder.origin_to_dict`),
    # because an unlabeled cwd is exactly the silent default 7.10.0 retired.
    source: str | None = None

    # Other official spellings of the remote `repo` was read from — every
    # raw `git config --get-all remote.<name>.url` value other than the
    # captured URL itself. Populated by `capture()` on the CALLER-side
    # Origin only; a PRIVATE attribute, so it is never serialized into
    # memory frontmatter or event payloads (the on-disk format and event
    # schemas are unchanged). Exists because releases through v3.9.0
    # captured `repo` via `git config --get remote.origin.url` (last URL
    # of a multi-valued remote, raw insteadOf alias) while the current
    # idiom (`git remote get-url origin`) returns the first URL with
    # aliases expanded — stored origins from old captures need the
    # alternate spellings to keep matching. See `_CALLER_REPO_ALTERNATES`
    # for how `repos_match` consumes them.
    _repo_url_alternates: tuple[str, ...] = PrivateAttr(default=())

    # Whether `capture()` could run git AT ALL. `repo` and `worktree_root`
    # are null both when git looked and said "not a repository" and when
    # git could not be asked — no binary on PATH, a timeout, an OSError
    # from the spawn — and the two mean opposite things to every consumer
    # that keys a shield on them: the first is a caller standing nowhere,
    # the second is a caller standing somewhere this process cannot see.
    # PRIVATE like the alternates above: never serialized into frontmatter
    # or an event payload; a fact about this capture, not about a memory.
    _git_indeterminate: bool = PrivateAttr(default=False)

    @property
    def git_indeterminate(self) -> bool:
        """True when this origin's null `repo` / `worktree_root` are the
        product of git not answering rather than of git answering "no".
        Consumers that would open a scope boundary on a null must treat
        this as "could not tell" and hold the boundary instead."""
        return self._git_indeterminate


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
    exists.

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
    A first probe that ran and exited non-zero is an answer, "not a
    repository", and is kept for the lifetime like any other. The lifetime
    is ten minutes, so what the signature does not see (a remote changed
    outside the repository's config file, among the rest
    `ORIGIN_CACHE_SECONDS` lists) can be answered from a capture up to ten
    minutes old.
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
        repo_url, repo_url_alternates = cached.repo, entry.alternates
        branch = cached.branch
    else:
        unanswered = _UNANSWERED.count
        worktree_root, indeterminate = _probe_worktree_root(resolved)
        repo_url = None
        repo_url_alternates = ()
        if worktree_root:
            repo_url, repo_url_alternates = _git_remote_url_and_alternates(resolved)
        branch = _git_branch(resolved) if worktree_root else None
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
#: exited non-zero, kept as "not a repository" after a failure that clears
#: on its own.
ORIGIN_CACHE_SECONDS = 600.0

# The most directories the cache holds; past it the oldest capture goes.
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
    discovery), with the stamp of config.worktree, where git reads
    core.worktree under extensions.worktreeConfig; the root spelled as git
    prints it (`_spelled_as_git`: the kernel's path on macOS, so a rename
    of the root or a directory above it that changes only the case or the
    Unicode normalisation of a name, under which the caller's old spelling
    still resolves, is seen; the resolved path elsewhere, which keeps the
    caller's spelling); and the stamp of each ref file that decides the
    branch's short name (`_shortening_refs`), in the git directory and in
    the common directory. Where `githead` does not settle the repository,
    the githead signature already equals no other; where a stamp or the
    spelling cannot be read, a new object makes this one equal no other
    either."""
    base = githead.signature(start)
    gd = githead.find_gitdir(start)
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
# exited non-zero: `_probe_worktree_root` reads that exit as "not a
# repository", an answer, and a failure that clears (an unreadable file, a
# lock) exits non-zero as well. Keyed by directory, never by process. The
# lock serialises the store, the eviction and the clear; a lookup is one
# dict read.
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
_CALLER_REPO_ALTERNATES: dict[str, tuple[str, ...]] = {}
_CALLER_REPO_ALTERNATES_CAP = 32


def _register_caller_alternates(repo_url: str, alternates: tuple[str, ...]) -> None:
    """Record (or refresh) the alternate spellings captured for `repo_url`.

    Registering an empty tuple is meaningful — it clears a stale entry
    after the remote's extra URLs were removed from git config.
    """
    if (
        repo_url not in _CALLER_REPO_ALTERNATES
        and len(_CALLER_REPO_ALTERNATES) >= _CALLER_REPO_ALTERNATES_CAP
    ):
        # FIFO eviction — dicts preserve insertion order.
        _CALLER_REPO_ALTERNATES.pop(next(iter(_CALLER_REPO_ALTERNATES)))
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
    """True if a memory whose origin.worktree_root is `memory_worktree`
    belongs to a caller currently in `caller_worktree`.

    Either side null → True. A legacy memory has no `worktree_root`
    field; a caller running outside any git checkout has no worktree
    to compare against; in either case we have no boundary to
    enforce, and the auto-scope filter falls back to repo-only
    matching.

    Both sides set and unequal → two relaxations before excluding,
    closing the "linked-worktree blackout" without reopening the
    worktree-leakage hole the strict check exists to plug:

    1. **Caller in a linked worktree of the memory's checkout.** Agent
       harnesses routinely spawn sessions in ephemeral `git worktree`
       checkouts (this project's own audit-loop fan-out does). Strict
       equality made EVERY memory written in the primary checkout —
       the repo's shared knowledge — invisible to those sessions. A
       linked worktree's root carries a `.git` FILE pointing at
       `<primary>/.git/worktrees/<name>`, so the caller's primary is
       derivable from the filesystem alone; when it equals the
       memory's recorded worktree, the memory surfaces. Asymmetric by
       design: notes written in a LIVE sibling worktree still stay
       isolated from the primary and from other siblings (their
       recorded root is the sibling path, which is neither the
       caller's root nor anyone's primary).

    2. **Dead-worktree degrade.** A memory written from a
       since-deleted worktree (ephemeral agent checkout, removed
       clone) would otherwise be invisible from EVERY worktree
       forever. When the recorded root is POSITIVELY GONE, degrade
       to repo-level matching — there is no live workspace left to
       isolate from.

       "Gone" is strictly narrower than "this process could not
       stat it". An indeterminate answer — permission denied on a
       parent, an unmounted volume, a detached network share, a
       path under another user account — is NOT evidence of death,
       and degrading on it would silently widen what the caller
       sees for as long as the condition lasts. Those hold the
       isolation instead. `_worktree_root_is_gone` owns the
       classification; read it rather than a restatement here.
    """
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
    """True only when the OS positively reported that nothing exists at
    `worktree` — the narrow condition the dead-worktree degrade needs.

    Deliberately not `Path.exists()`. That helper answers a different
    question: it collapses "nothing is there" together with a fixed
    subset of "I could not find out" into one `False`, and RAISES for
    the rest of that subset (`pathlib._ignore_error` — EACCES,
    ENOTCONN, ENAMETOOLONG and friends propagate). Under a
    degrade-on-falsey caller both halves of the collapse fail OPEN, and
    so does wrapping the raise in a bare `except OSError` — every
    unstattable path reads as a dead one and relaxes the isolation
    boundary for as long as it stays unstattable.

    `verify._path_exists` makes the opposite call from the same raw
    material, and the contrast is the point: an indeterminate stat
    there folds into the `missing` path-drift bucket, i.e. toward MORE
    signal, and over-reporting drift is that surface's safe direction.
    Here the safe direction is the other one, so the two cannot share
    an implementation.

    Follows symlinks (`os.stat`, not `os.lstat`) — a recorded root that
    is now a dangling symlink names no live checkout, and it keeps the
    resolution semantics `_git_worktree_root` captured under.
    Uncached on purpose: liveness genuinely changes under a
    long-running server (a worktree is removed, a volume is remounted),
    and a cache would freeze the first answer — including a transient
    failure — for the rest of the process.
    """
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
    """Primary checkout root for a LINKED worktree; None for a primary
    checkout, a bare path, or anything unreadable.

    A linked worktree's root contains a `.git` FILE (the primary's has a
    `.git` DIRECTORY) whose single line reads
    ``gitdir: <primary>/.git/worktrees/<name>`` — pure filesystem
    introspection, no subprocess. Cached per path for the process
    lifetime: worktree topology doesn't change under a running server,
    and the cache keeps the per-candidate filter cost at dict-lookup
    level during a search sweep.
    """
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
    """True if a memory with this origin should surface for a caller in `caller_repo`.

    Thin wrapper over `repos_match` that handles the
    `memory.origin.repo if memory.origin else None` extraction once. Every
    *surface* filter — `memory_search`'s auto-scope and the matching
    branch in `memory_scope_overview` — was repeating this pattern,
    with the scope-overview callsite explicitly noting that it was
    reimplementing the search filter to stay in sync. Folding the
    extraction-plus-match into one named helper means there is exactly
    one place that defines "this memory belongs to this caller's
    project", so the surface filter is provably consistent.

    Auto-scope semantics flow through `repos_match`: a null
    `memory_origin` (legacy file with no origin block, or a write from
    outside any repo) is treated as global and matches every caller;
    likewise a null `caller_repo` (running outside any repo) matches
    every memory.

    `caller_repo_alternates` passes through to `repos_match`'s
    `caller_alternates` — extra official spellings of the caller's own
    remote that also count as a repo match. When omitted, `repos_match`
    falls back to the spellings `capture()` registered for
    `caller_repo` in this process, so string-threading call sites get
    the old-capture-idiom bridge without any plumbing changes.

    `caller_worktree_root` opts into the secondary worktree filter: when
    both the memory and the caller carry a populated `worktree_root` and
    the two differ, the memory is excluded even if `repos_match` says
    yes. This is what keeps notes written in one worktree of a
    repository (`~/repo-feature-x/`) from leaking into searches run
    from a sibling worktree of the same repository
    (`~/repo-bug-fix/`); a single-tree checkout never has two
    worktree roots in play, so the secondary filter is a no-op there.
    Legacy memories (no `worktree_root`) always pass — adding the
    filter must not silently hide writes that predate it.

    **Not the right helper for commit-drift**: the commit-drift path in
    `verify`, `_response.attach_commit_drift_counts`, and the
    `health._compute_commit_drift_debt` rollup need a stricter check that
    rejects global memories (no repo anchor means nothing to count
    commits against). Those sites call `repos_match` directly after a
    `null → return None` check. Mixing the two would silently start
    reporting drift counts of "all commits since verify" for global
    memories, which would be both wrong and very noisy.
    """
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
    """The subcommand in a git argv, for failure log lines.

    Skips leading ``-c <key>=<val>`` pairs so a config-pinned call
    (`commit_patch_stream`) logs as ``git log``, not ``git -c``.
    """
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else ""


# How many git invocations could not run at all since import, on any
# thread: no binary on PATH, a timeout, an OSError from the spawn. Read
# through `unanswered_git_calls`.
_unanswered = 0


def unanswered_git_calls() -> int:
    """How many git invocations since import could not run at all (the
    None `_git_result` returns), on every thread. The drift memos read
    `failed_git_calls` instead, which counts these and every other None
    on the calling thread."""
    return _unanswered


class _Failed(threading.local):
    """The number of git calls on the current thread that came back None
    from `_git` or `_git_result`, whatever the reason. Read through
    `failed_git_calls`."""

    count = 0


_FAILED = _Failed()


def failed_git_calls() -> int:
    """How many git calls on the current thread have come back None from
    `_git` or `_git_result`: git could not run, it exited non-zero, or
    `_git` folded an empty output into None.

    The drift readers fold each such None into a fallback (the unfiltered
    count, the author-date basis, the could-not-ask applicability), and a
    non-zero exit cannot be told from a failure that clears (an object
    briefly unreadable, an I/O error, a lock) without reading git's
    message. The uncached code takes the fallback on the call that met the
    failure and not on the next one, so a memo reads this count before and
    after computing a value and keeps the value only when it did not
    move."""
    return _FAILED.count


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

    Failure logging is tiered so the operationally interesting failures
    (missing binary, timeouts) reach the log at WARNING while the
    per-call non-zero exits, which `_git` logs at DEBUG, stay out of it.
    """
    global _unanswered
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            # Git emits UTF-8 (paths especially, and unconditionally so
            # under the core.quotePath=false pin in commit_patch_stream).
            # Bare text=True decodes with locale.getpreferredencoding —
            # cp1252 on Windows — which mojibakes every non-ASCII path
            # and mis-binds the drift legs there. Decode UTF-8
            # explicitly; errors="replace" keeps failure soft, matching
            # this runner's origin-is-nice-to-have posture.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        log.warning("git binary not found on PATH: %s", exc)
    except subprocess.TimeoutExpired:
        log.warning(
            "git %s timed out after %ss in %s",
            _log_subcommand(args),
            timeout,
            cwd,
        )
    except OSError as exc:
        log.warning("git invocation failed in %s: %s", cwd, exc)
    _unanswered += 1
    _FAILED.count += 1
    return None


def _log_nonzero_exit(
    args: tuple[str, ...], result: subprocess.CompletedProcess[str], cwd: Path
) -> None:
    """DEBUG-level note of a non-zero git exit. Trimmed to stderr's first
    line — git's "fatal: ..." messages are one-liners; deeper output is
    rare and not worth flooding the log with. Empty stderr is still
    logged so the returncode itself is at least visible."""
    stderr = (result.stderr or "").strip().splitlines()
    first = stderr[0] if stderr else ""
    log.debug(
        "git %s exited %s in %s: %s",
        _log_subcommand(args),
        result.returncode,
        cwd,
        first,
    )


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

    * `FileNotFoundError` / `OSError` → WARNING. The git binary isn't
      reachable; every origin capture for this process will fail the
      same way. `doctor` and verbose-mode users want to see this.
    * `subprocess.TimeoutExpired` → WARNING. A hanging git is rare
      enough that surfacing it is worth more than the noise.
    * Non-zero exit → DEBUG with stderr. The vast majority of these are
      "fatal: not a git repository" from a non-repo cwd, which is fully
      expected (memories written outside any repo get `repo=None`).
      DEBUG keeps the signal available to anyone who flips the log
      level (or to `doctor`) without spamming WARNING for every memory
      written from a home directory or a freshly-cloned scratch dir.
    """
    result = _git_result(cwd, *args, timeout=timeout)
    if result is None:
        _UNANSWERED.count += 1
        return None
    if result.returncode != 0:
        _log_nonzero_exit(args, result, cwd)
        _FAILED.count += 1
        return None
    out = result.stdout.strip()
    if out or empty_ok:
        return out
    _FAILED.count += 1
    return None


def _git_remote_url_and_alternates(cwd: Path) -> tuple[str | None, tuple[str, ...]]:
    # `git remote get-url origin`, NOT `git config --get remote.origin.url`:
    # `config --get` on a multi-valued key returns the LAST value, so the
    # canonical push-mirror recipe (`git remote set-url --add origin
    # <mirror>`) would flip every subsequent capture to the mirror URL and
    # hide all previously written memories for the repo. `get-url` returns
    # the FIRST configured URL — the canonical fetch URL — and also expands
    # `url.<base>.insteadOf` aliases to the URL git actually fetches from
    # (same idiom sync.py uses). It exits non-zero when the remote doesn't
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


def _probe_worktree_root(cwd: Path) -> tuple[str | None, bool]:
    """``(worktree_root, indeterminate)`` for `cwd`.

    `rev-parse --show-toplevel` returns the absolute path of the working
    tree root — for a primary checkout, the repo root; for a worktree
    (`git worktree add`), the worktree's own root, which *differs*
    between sibling worktrees of the same repository. That difference
    is exactly what the auto-scope filter needs to tell two worktrees of
    one repo apart. Resolved through `Path` to normalise symlink hops on
    macOS' `/var` → `/private/var` idiom, so a memory captured under one
    symlink form still compares equal to a caller that resolves the
    other.

    `indeterminate` is True only when git could not RUN — the answer is
    then unknown, not "no". Git exiting non-zero ("not a git repository")
    is the measured no this probe has always reported as None.
    """
    result = _git_result(cwd, "rev-parse", "--show-toplevel")
    if result is None:
        return None, True
    if result.returncode != 0:
        _log_nonzero_exit(("rev-parse", "--show-toplevel"), result, cwd)
        return None, False
    raw = result.stdout.strip()
    if not raw:
        return None, False
    try:
        return str(Path(raw).resolve()), False
    except OSError:
        return raw, False


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


# The repository's own configuration file, read to see whether it moves
# the working tree git names; larger than any configuration a checkout
# carries.
_CONFIG_LIMIT = 1 << 20

# The one ``bare`` line that leaves the working tree where it is: a plain
# false, as ``git init`` writes it. Matched against a lowercased line.
_BARE_FALSE = re.compile(rb"[ \t]*bare[ \t]*=[ \t]*(?:false|no|off|0)[ \t]*\r?")


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


@dataclass(frozen=True)
class _ConfigFacts:
    """What the callers read from one configuration file: the variables it
    sets as git reads them (`_config_variable_names`; None where git
    refuses the file or it holds a NUL byte), whether it names an
    attributes file or an attribute tree (``attributesfile`` or ``[attr``
    anywhere in it, in any case), and whether a line names ``bare`` with
    anything but a plain false."""

    names: frozenset[bytes] | None
    attributes_elsewhere: bool
    bare_moves: bool


_NO_CONFIG = _ConfigFacts(
    names=frozenset(), attributes_elsewhere=False, bare_moves=False
)


def _facts_of(data: bytes) -> _ConfigFacts:
    names = _config_variable_names(data)
    lowered = data.lower()
    return _ConfigFacts(
        names=None if names is None else frozenset(names),
        attributes_elsewhere=b"attributesfile" in lowered or b"[attr" in lowered,
        bare_moves=any(
            b"bare" in line and _BARE_FALSE.fullmatch(line) is None
            for line in lowered.split(b"\n")
        ),
    )


# The facts of each configuration file read, memoised on the file's
# `githead.stamp`: the repository's config and config.worktree are read on
# every search (`toplevel_and_head_from_files`) and for every hit with a
# governed claim (`attribute_files_signature`), and a large file would
# otherwise be parsed again each time. A file is parsed once per stamp, the
# stamp taken from the open file before and after the read, and the facts
# are kept only where the two agree, so what is kept is what that state of
# the file holds; a rewrite in place at the same size within one tick of
# the filesystem's clock keeps its stamp and is the one change missed, as
# for every stamp. Bounded; registered with `_caches`.
_CONFIG_MEMO_CAP = 64
_CONFIG_MEMO: OrderedDict[str, tuple[githead.Stamp, _ConfigFacts]] = OrderedDict()
_CONFIG_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_config_memo() -> None:
    with _CONFIG_MEMO_LOCK:
        _CONFIG_MEMO.clear()


def _fd_stamp(fd: int) -> githead.Stamp:
    status = os.fstat(fd)
    return (
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_size,
        status.st_ino,
        status.st_mode,
    )


def _config_facts(path: Path) -> tuple[githead.Stamp | None, _ConfigFacts] | None:
    """``(stamp, facts)`` of the configuration file `path`: its `githead.
    stamp`, None when it does not exist (and then facts that set nothing),
    and what it holds. None when it cannot be read, is not a regular file
    or is larger than `_CONFIG_LIMIT`. The open does not block, so a FIFO
    fails instead of waiting."""
    key = os.fspath(path)
    try:
        current = githead.stamp(path)
    except (OSError, ValueError):
        return None
    if current is None:
        return None, _NO_CONFIG
    with _CONFIG_MEMO_LOCK:
        kept = _CONFIG_MEMO.get(key)
        if kept is not None and kept[0] == current:
            _CONFIG_MEMO.move_to_end(key)
            return kept
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, _NO_CONFIG
    except OSError:
        return None
    try:
        before = _fd_stamp(fd)
        if not stat.S_ISREG(before[4]):
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= _CONFIG_LIMIT:
            chunk = os.read(fd, _CONFIG_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = _fd_stamp(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if size > _CONFIG_LIMIT:
        return None
    read = (before, _facts_of(b"".join(chunks)))
    if before == after:
        with _CONFIG_MEMO_LOCK:
            _CONFIG_MEMO[key] = read
            _CONFIG_MEMO.move_to_end(key)
            while len(_CONFIG_MEMO) > _CONFIG_MEMO_CAP:
                _CONFIG_MEMO.popitem(last=False)
    return read


def _config_moves_worktree(gd: githead.GitDir) -> bool:
    """Whether the repository's own configuration could make git name a
    working tree other than the directory holding ``.git``: ``core.worktree``
    set in the common config or in config.worktree, read as git reads the
    files (a branch, a remote or ``extensions.worktreeConfig`` that merely
    holds the word sets nothing), a line naming ``bare`` with anything but
    a plain false, and a file git refuses, that holds a NUL byte or that
    cannot be read. The answer only decides whether the files or git
    answer, so a false yes costs one process and a false no would be a
    wrong root."""
    for path in (gd.commondir / "config", gd.gitdir / "config.worktree"):
        read = _config_facts(path)
        if read is None:
            return True
        facts = read[1]
        if facts.names is None or b"core.worktree" in facts.names or facts.bare_moves:
            return True
    return False


def _spelled_as_git(root: Path) -> Path | None:
    """`root` spelled the way git prints a working tree's root.

    Git names the root from the kernel's path for the directory
    (``getcwd``), which gives each component the case and Unicode form the
    directory holds it in. `githead` walks from ``os.path.realpath``,
    which keeps the spelling it was handed, so on a filesystem that
    ignores case or normalization (macOS's default) a caller that named
    ``~/documents`` for ``~/Documents`` gets a root git would not print.
    On macOS the kernel's spelling is read back with ``F_GETPATH``; None
    when the directory cannot be opened. Elsewhere the realpath stands,
    as it does on every filesystem that holds one spelling per name.
    """
    if sys.platform != "darwin":
        return root
    import fcntl

    try:
        fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except OSError:
        return None
    finally:
        os.close(fd)
    return Path(os.fsdecode(raw.split(b"\0", 1)[0]))


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
    _log_nonzero_exit(("merge-base", "--is-ancestor"), result, cwd)
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
# (git's configuration outside the repository's config file,
# config.worktree, a history rewritten under an unchanged head, and the
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
# `_caches`, which empties it before each test.
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
            _FAILED.count += 1
        return None
    failed = _FAILED.count
    walk = _walk_reachable(toplevel, anchor, head)
    if walk is None:
        if walked is not None:
            walked[within] = _FAILED.count != failed
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
            rel = candidate.relative_to(toplevel)
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

    One `utcoffset()` per element instead of one per comparison. See
    `commit_author_timestamps` for why that matters here.
    """
    return stamp.timestamp()


# The whole-history author dates, memoised per (root, head): the log names
# the head by its hash, whose history never changes, and a commit landing
# moves the head and so the key. A process serves the few checkouts its
# callers stand in, so the bound is small. A None is never stored. The
# lock makes each look-up-and-touch and each insert-and-evict atomic; the
# log runs outside it. Registered with `_caches`, which empties it before
# each test.
_TIMESTAMPS_MEMO_CAP = 16
_TIMESTAMPS_MEMO: OrderedDict[tuple[str, str], list[datetime]] = OrderedDict()
_TIMESTAMPS_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_timestamps_memo() -> None:
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_MEMO.clear()


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

    Sorting on the instant, not the datetime: `%aI` preserves each
    author's own UTC offset, so the list carries thousands of DISTINCT
    tzinfo objects and CPython's same-tzinfo comparison fast path never
    fires — without the key every comparison makes a Python-level
    `utcoffset()` call. Same ordering either way; both are the absolute
    instant.

    Used by the health rollup to count commits-since for many memories
    from one git invocation; a per-memory ``git rev-list --count`` would
    otherwise pay a fork+exec for every row.

    MEMOISED per (root, head) (`_TIMESTAMPS_MEMO`). `located` is the root
    and the head as the caller already resolved them for `cwd`; without
    it they are read from the repository's files
    (`toplevel_and_head_from_files`). The log then names the head by its
    hash, so the list stored under a head is that head's history even if
    a commit lands while git runs. Where the files do not settle the pair
    and the caller passed none, the log runs from HEAD uncached. A hit
    returns the stored list itself: every caller only bisects it. The key
    does not see a history rewritten under an unchanged head: a shallow
    clone deepened in place, a ``git replace`` ref or a graft reads the
    earlier walk until the head moves.
    """
    if cwd is None:
        return None
    if located is None:
        located = toplevel_and_head_from_files(cwd)
    if located is None:
        return _read_author_timestamps(cwd, "HEAD")
    key = (str(located[0]), located[1])
    with _TIMESTAMPS_MEMO_LOCK:
        stored = _TIMESTAMPS_MEMO.get(key)
        if stored is not None:
            _TIMESTAMPS_MEMO.move_to_end(key)
            return stored
    stamps = _read_author_timestamps(cwd, located[1])
    if stamps is not None:
        with _TIMESTAMPS_MEMO_LOCK:
            _TIMESTAMPS_MEMO[key] = stamps
            while len(_TIMESTAMPS_MEMO) > _TIMESTAMPS_MEMO_CAP:
                _TIMESTAMPS_MEMO.popitem(last=False)
    return stamps


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
    raw = _git(cwd, "log", "--format=%aI", revision, timeout=5.0)
    if raw is None:
        return None
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
        return None
    out.sort(key=_instant)
    return out


def commit_author_timestamps_touching_pathspecs(
    cwd: Path | None,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> list[datetime] | None:
    """Author timestamps of the commits touching any of `pathspecs`.

    `pathspecs` must already be repo-root-relative forward-slash specs (the
    output of `resolve_repo_pathspecs`). The path-filtered analogue of
    `commit_author_timestamps`: same ``--format=%aI`` author-date source, so
    a caller can `bisect_right` the result against a `since` instant and get
    a count that lives in the SAME date space as the unfiltered bisect — no
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


# The single-character escapes git's `quote_c_style` emits inside a
# quoted path header, mapped to their byte values. Everything else it
# deems unprintable comes out as 3-digit octal. Both decode to BYTES —
# an octal escape is one raw byte of the path's on-disk encoding, so a
# multi-byte UTF-8 character arrives as several escapes and the path
# must be reassembled as bytes and decoded once at the end.
_C_QUOTE_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def _unquote_c_path(quoted: str) -> str | None:
    """Decode ONE complete git C-quoted string, or None if `quoted` is
    not exactly that.

    "Exactly that" is the safety property: the input must be a leading
    quote, a well-formed escaped body with no unescaped interior quote,
    and a trailing quote with nothing after it. Anything else — an
    unterminated quote, an unknown escape, an interior close-quote with
    trailing text, bytes that don't decode as UTF-8 — returns None and
    the caller leaves the line untouched. Better to keep a quoted
    spelling (a false mismatch, the conservative direction downstream)
    than to guess at a path git didn't actually name.
    """
    if len(quoted) < 2 or not quoted.startswith('"') or not quoted.endswith('"'):
        return None
    body = quoted[1:-1]
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            # An unescaped interior quote means `quoted` was not ONE
            # complete quoted string (e.g. `"x" -> "y"` content).
            return None
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        esc = body[i]
        code = _C_QUOTE_ESCAPES.get(esc)
        if code is not None:
            out.append(code)
            i += 1
            continue
        if esc in "01234567":
            j = i + 1
            while j < n and j - i < 3 and body[j] in "01234567":
                j += 1
            out.append(int(body[i:j], 8) & 0xFF)
            i = j
            continue
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _dequote_patch_headers(stream: str) -> str:
    """Rewrite residually-quoted ``---``/``+++`` headers to the unquoted
    shape `claims.build_binding_index` hard-requires.

    Even with ``core.quotePath=false`` pinned, git still C-quotes a
    header whose path carries a double quote, a backslash, or control
    bytes — ``--- "a/we\\"ird.py"`` — and the parser's exact
    ``--- a/`` / ``+++ b/`` prefix match then misses it (a deletion of
    such a path would go unrecorded; an edit would be indexed under the
    quoted spelling and never equal a claim's rel_path). Decoding
    happens HERE, on the producer side, so the parser keeps its
    one-argument no-lookup guarantee and its exact prefix match.

    A header line is rewritten only when the whole remainder decodes as
    one complete quoted string AND the decoded path starts with the
    pinned ``a/`` (old side) or ``b/`` (new side) prefix. That guard
    keeps hunk-body content lines out: a removed source line
    ``-- "x"`` renders as ``--- "x"`` in the stream, but its decoded
    body doesn't start with a prefix, so it passes through byte-exact.
    (A content line spelling a quoted a/-path remains theoretically
    rewritable — the cost is one slightly-off change anchor, never a
    missed deletion.) The fast path keeps the zero-quoted-header case,
    i.e. essentially every real stream, allocation-free.
    """
    if '--- "' not in stream and '+++ "' not in stream:
        return stream
    lines = stream.split("\n")
    for idx, line in enumerate(lines):
        if not line.startswith(('--- "', '+++ "')):
            continue
        decoded = _unquote_c_path(line[4:])
        if decoded is None:
            continue
        prefix = "a/" if line.startswith("--- ") else "b/"
        if decoded.startswith(prefix):
            lines[idx] = line[:4] + decoded
    return "\n".join(lines)


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

    Each is pinned back to the parser's shape with ``-c``, which
    outranks every config file (older gits ignore the >= 2.45 keys).
    Paths git quotes even with quotePath off — embedded quotes,
    backslashes, control bytes — are rewritten to the unquoted spelling
    by `_dequote_patch_headers` before the stream is returned, so the
    normalization lives with the producer and the parser keeps its
    one-argument guarantee.

    What each file's diff looks like is left to its attributes (``-diff``
    and ``binary`` print "Binary files differ", a textconv driver prints
    its conversion), read from the working tree's ``.gitattributes``
    files, ``info/attributes`` and the global attributes file; ``--text``
    would change what a repository with such attributes is served.
    `attribute_files_signature` and `gitattributes_signature` name those
    files for a memo that keys on this stream.

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
        "log",
        "--no-walk=unsorted",
        "-p",
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


# Characters with which a pathspec matches more than the one path it
# spells: git's wildcards and their escape. A leading colon opens pathspec
# magic.
_PATHSPEC_WILDCARDS = frozenset("*?[\\")


# The stamp each attribute file, each file beside the walk and the
# githead signature's config carry: githead's, so the two agree.
_stamp = githead.stamp


def _hold_directory(path: str | Path, held: dict[str, tuple[int, int]]) -> None:
    """Record in `held` the ``(st_mtime_ns, st_ctime_ns)`` of the directory
    `path`, or of its nearest existing ancestor while it does not exist.
    Creating or removing an entry changes its directory's stamp, and
    creating a missing directory changes its parent's, so a file created
    and removed between two reads, absent at both, still moves a stamp
    recorded here. Read for a check before and after a computation, never
    for a key: a directory changes whenever any entry in it does. A
    directory already in `held` is not read again and keeps the stamp
    first recorded for it, which is the one the check must compare with.
    Raises OSError or ValueError where a stat fails for any reason but
    absence. A file is held the same way (`walk_files_signature` holds the
    index): a write to it, or its replacement through a rename, moves the
    pair.

    The cost is that of any change to such a directory, related or not.
    While something creates and removes files in a held directory steadily
    (an editor's swap files in the repository's root or in a governed
    file's directory; the global attributes file's nearest existing
    ancestor where its own directory does not exist, which is the home
    directory itself where ``~/.config`` does not exist either), a search or
    walk that held it keeps nothing it computed, and each one pays what an
    uncached one pays."""
    current = os.fspath(path)
    while current not in held:
        try:
            status = os.stat(current)
        except (FileNotFoundError, NotADirectoryError):
            parent = os.path.dirname(current)
            if parent == current:
                raise
            current = parent
            continue
        held[current] = (status.st_mtime_ns, status.st_ctime_ns)


def directories_held(held: Mapping[str, tuple[int, int]]) -> bool:
    """Whether every directory `_hold_directory` recorded in `held` still
    has the stamp recorded for it; False where one cannot be read."""
    try:
        for directory, recorded in held.items():
            status = os.stat(directory)
            if (status.st_mtime_ns, status.st_ctime_ns) != recorded:
                return False
    except (OSError, ValueError):
        return False
    return True


def _global_attributes_path(root: Path) -> str | None:
    """The global attributes file git reads when no ``core.attributesFile``
    names another, spelled as git's ``xdg_config_home_for`` spells it:
    ``$XDG_CONFIG_HOME/git/attributes`` when XDG_CONFIG_HOME is set and not
    empty, else ``$HOME/.config/git/attributes`` whenever HOME is set, an
    empty HOME naming ``/.config/git/attributes``, and none with neither. A
    relative name is read from the root, where the drift readers run
    git."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        candidate = f"{config_home}/git/attributes"
    else:
        home = os.environ.get("HOME")
        if home is None:
            return None
        candidate = f"{home}/.config/git/attributes"
    if os.path.isabs(candidate):
        return candidate
    return os.path.join(root, candidate)


# git's isspace (sane_ctype in ctype.c), the letters a variable's name
# starts with, the characters it continues with (iskeychar), and the
# escapes a value may hold. ASCII only, as git's are.
_CONFIG_SPACE = frozenset(b" \t\n\r")
_CONFIG_ALPHA = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_CONFIG_KEYCHAR = _CONFIG_ALPHA | frozenset(b"0123456789-")
_CONFIG_ESCAPES = frozenset(b'tbn\\"')
_UTF8_BOM = b"\xef\xbb\xbf"


def _config_variable_names(data: bytes) -> set[bytes] | None:
    """The names of the variables the config file `data` sets, as git's own
    parser reads the file (``git_parse_source`` in config.c) and ``git
    config --list --name-only`` prints them: ``section.key`` or
    ``section.subsection.key``, the section and the key in lower case, a
    quoted subsection as written. A section header wherever a key may
    start, a key with or without a value, comments, quoted and escaped
    values, a value continued onto the next line by a backslash outside a
    comment, CRLF line ends and a leading byte order mark are read as git
    reads them, so ``log.follow`` is named in any case or layout git
    accepts and a ``follow`` inside a value, a comment, a subsection or a
    longer key is not. None where git refuses the file ("bad config
    line"), and None for any file holding a NUL byte: git keeps a name only
    up to a NUL in it, so ``[log "follow\\0"] x`` sets ``log.follow`` to git
    (and an escaped letter spells the word without its bytes), which this
    reader does not model. Past those, the names are the ones git lists,
    pinned against git on the texts of tests/test_commit_drift_cache.py.
    Values are read only to find where they end."""
    if b"\0" in data:
        return None
    text = data.replace(b"\r\n", b"\n")
    end = len(text)
    pos = 0
    if text[:1] == _UTF8_BOM[:1]:
        if not text.startswith(_UTF8_BOM):
            return None
        pos = len(_UTF8_BOM)
    names: set[bytes] = set()
    section = b""
    comment = False
    while pos < end:
        c = text[pos]
        pos += 1
        if c == 0x0A:
            comment = False
        elif comment or c in _CONFIG_SPACE:
            continue
        elif c in b"#;":
            comment = True
        elif c == 0x5B:  # "["
            header = _config_section(text, pos)
            if header is None:
                return None
            section, pos = header
        elif c in _CONFIG_ALPHA:
            start = pos - 1
            while pos < end and text[pos] in _CONFIG_KEYCHAR:
                pos += 1
            key = text[start:pos].lower()
            while pos < end and text[pos] in b" \t":
                pos += 1
            if pos < end:
                c = text[pos]
                pos += 1
                if c == 0x3D:  # "="
                    after = _config_value_end(text, pos)
                    if after is None:
                        return None
                    pos = after
                elif c != 0x0A:
                    return None
            names.add(section + b"." + key if section else key)
        else:
            return None
    return names


def _config_section(text: bytes, pos: int) -> tuple[bytes, int] | None:
    """The name a section header gives the keys under it, and the position
    after its ``]``, for a header whose ``[`` ends at `pos`; None where git
    refuses the header (``get_base_var`` and ``get_extended_base_var`` in
    config.c). ``[name]`` and the older ``[name.sub]`` are lower-cased
    whole; ``[name "sub"]`` keeps the subsection as written, each
    backslash escape read as the character it escapes."""
    end = len(text)
    name = bytearray()
    while True:
        if pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c == 0x5D:  # "]"
            return (bytes(name), pos) if name else None
        if c in _CONFIG_SPACE:
            break
        if c not in _CONFIG_KEYCHAR and c != 0x2E:  # "."
            return None
        name.append(c + 32 if 0x41 <= c <= 0x5A else c)
    while True:
        if c == 0x0A or pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c not in _CONFIG_SPACE:
            break
    if c != 0x22:  # a quoted subsection must follow
        return None
    name.append(0x2E)
    while True:
        if pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c == 0x0A:
            return None
        if c == 0x22:
            break
        if c == 0x5C:  # a backslash escapes the next character
            if pos >= end or text[pos] == 0x0A:
                return None
            c = text[pos]
            pos += 1
        name.append(c)
    if pos >= end or text[pos] != 0x5D:
        return None
    return bytes(name), pos + 1


def _config_value_end(text: bytes, pos: int) -> int | None:
    """Where the value that starts at `pos` ends: past the newline that
    ends its line, or a later one where a backslash outside a comment
    continues it. None where git refuses the value (``parse_value`` in
    config.c): a quote still open at its end, or an escape git does not
    know."""
    end = len(text)
    quote = False
    comment = False
    while pos < end:
        c = text[pos]
        pos += 1
        if c == 0x0A:
            return None if quote else pos
        if comment or (c in _CONFIG_SPACE and not quote):
            continue
        if c in b"#;" and not quote:
            comment = True
        elif c == 0x5C:
            if pos < end:
                if text[pos] != 0x0A and text[pos] not in _CONFIG_ESCAPES:
                    return None
                pos += 1
        elif c == 0x22:
            quote = not quote
    return None if quote else pos


def attribute_files_signature(
    cwd: Path,
    root: Path,
    *,
    directories: dict[str, tuple[int, int]] | None = None,
) -> tuple[object, ...] | None:
    """The files outside the working tree that decide how
    `commit_patch_stream` diffs a file, for a memo to key on: the
    repository's ``config`` (its diff drivers, and whether it names an
    attributes file), ``info/attributes`` in the common directory, and the
    global attributes file with its path. Each is its `_stamp`
    (``st_mtime_ns``, ``st_ctime_ns``, ``st_size``, ``st_ino``,
    ``st_mode``), or None while it does not exist, so writing, creating,
    removing, replacing or changing the permissions of one changes the
    signature.

    `directories`, when given, receives the stamps of ``info`` in the
    common directory and of the global file's directory
    (`_hold_directory`), for a caller that checks at the end of its work
    that no attributes file there was created and removed meanwhile.

    None, and nothing to key on, where these files do not settle it: a
    repository `githead` does not read, a ``config`` that cannot be read,
    one that mentions ``attributesfile`` or an ``[attr`` section (a
    ``core.attributesFile`` or ``attr.tree`` there sends git to a file or a
    tree these stats do not cover), one that sets ``log.follow`` (it makes
    the single-pathspec patch stream follow a claimed file across a rename
    and diff the renaming commit with the attributes of the rename source's
    path, which no chain of the claimed path covers), one git refuses, one
    that holds a NUL byte (git ends a variable's name at it, so a quoted
    subsection can set ``log.follow`` or ``core.attributesFile`` without
    the words' bytes), and a stat that fails for any reason but absence.
    Whether the file sets ``log.follow`` is read as git reads it
    (`_config_variable_names`, memoised per state of the file in
    `_config_facts`), in any case or layout, so ``push.followTags``, a
    branch or a remote whose name holds ``follow``, or the word in a value
    or a comment, keys the files like any other config. A ``log.follow``
    set outside the repository's config (the global and system files, an
    include) is not seen. `root` is the root `cwd`'s repository names."""
    gd = githead.find_gitdir(cwd)
    if gd is None:
        return None
    read = _config_facts(gd.commondir / "config")
    if read is None:
        return None
    config_stamp, facts = read
    if (
        facts.names is None
        or facts.attributes_elsewhere
        or b"log.follow" in facts.names
    ):
        return None
    global_file = _global_attributes_path(root)
    try:
        if directories is not None:
            _hold_directory(gd.commondir / "info", directories)
            if global_file is not None:
                _hold_directory(os.path.dirname(global_file), directories)
        return (
            config_stamp,
            _stamp(gd.commondir / "info" / "attributes"),
            global_file,
            None if global_file is None else _stamp(global_file),
        )
    except (OSError, ValueError):
        return None


def gitattributes_signature(
    root: Path,
    pathspecs: Sequence[str],
    *,
    directories: dict[str, tuple[int, int]] | None = None,
) -> tuple[object, ...] | None:
    """The working tree's ``.gitattributes`` files that decide how
    `commit_patch_stream` diffs the files `pathspecs` name, for a memo to
    key on. Git reads the attributes of ``a/b/c.py`` from ``.gitattributes``
    at the root, in ``a`` and in ``a/b``, not through a symbolic link, and
    ``git log`` reads no index for them. Each is its `_stamp`, or None
    while it does not exist. `directories`, when given, receives the stamps
    of those directories (`_hold_directory`), for a caller that checks at
    the end of its work that no ``.gitattributes`` there was created and
    removed meanwhile.

    None, and nothing to key on, where the pathspecs reach files below
    those directories: a pathspec with a wildcard, an escape or pathspec
    magic, one that is not a plain relative path, and one the working tree
    holds as a directory, whose files take attributes from
    ``.gitattributes`` anywhere under it. A pathspec that is no directory
    now has no such file under it until it becomes one, which this check
    then sees. Also None when a stat fails for any reason but absence, a
    path holding a NUL byte among them."""
    chain: dict[Path, None] = {}
    try:
        for spec in pathspecs:
            parts = spec.split("/")
            if (
                spec.startswith(":")
                or not _PATHSPEC_WILDCARDS.isdisjoint(spec)
                or any(part in ("", ".", "..") for part in parts)
            ):
                return None
            try:
                if stat.S_ISDIR(os.stat(root.joinpath(*parts)).st_mode):
                    return None
            except (FileNotFoundError, NotADirectoryError):
                pass
            directory = root
            chain.setdefault(directory, None)
            for part in parts[:-1]:
                directory = directory / part
                chain.setdefault(directory, None)
        if directories is not None:
            for directory in chain:
                _hold_directory(directory, directories)
        return tuple(
            _stamp(directory / ".gitattributes", follow=False) for directory in chain
        )
    except (OSError, ValueError):
        return None


# The index's .gitmodules entry, read in-process the way git's
# repo_read_gitmodules consults it (submodule-config.c): git reads the index
# first, reads no .gitmodules at all while the index holds it unmerged
# (is_gitmodules_unmerged), then the working tree's file where it exists,
# the index's stage-0 blob where it does not, and HEAD's last. What the
# drift readers' walk lists depends on the index only through that entry, so
# the walk files key on the entry, not on the index's stamp, and a write to
# the index that leaves .gitmodules as it was keys nothing new.

#: `_index_gitmodules` for an index that holds .gitmodules only at stages 1
#: to 3, as a conflicted merge leaves it: git then reads no .gitmodules.
_INDEX_UNMERGED: tuple[str] = ("unmerged",)

_GITMODULES = b".gitmodules"

# Extended flags git writes and reads in a version 3 or 4 entry: CE_SKIP_
# WORKTREE and CE_INTENT_TO_ADD, in the second flags word. Any other bit
# makes git refuse the index ("unknown index entry format").
_EXTENDED_FLAGS = 0x4000 | 0x2000


class _IndexUnsettled(Exception):
    """The index is not one `_index_gitmodules` reads exactly."""


class _IndexShort(Exception):
    """The prefix of the index read so far ends before the scan does."""


# The first read of an index, and the factor each further read grows by
# while the scan runs past what was read.
_INDEX_FIRST_READ = 1 << 16
_INDEX_READ_GROWTH = 4


# The entry each index held, memoised on the index's stamp: the index is
# rewritten by most git commands (git add, a refreshing git status), and a
# search reads the entry at its start and end. Read once per stamp from the
# open file, kept only where its stamp before and after the read agree.
# Bounded; registered with `_caches`.
_INDEX_MEMO_CAP = 64
_INDEX_MEMO: OrderedDict[str, tuple[githead.Stamp, tuple[object, ...] | None]] = (
    OrderedDict()
)
_INDEX_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_index_memo() -> None:
    with _INDEX_MEMO_LOCK:
        _INDEX_MEMO.clear()


def _decode_varint(data: bytes, pos: int, *, complete: bool) -> tuple[int, int]:
    """git's ``decode_varint`` (varint.c): the value at `pos` and the
    position after it."""
    ends = _IndexUnsettled if complete else _IndexShort
    if pos >= len(data):
        raise ends
    byte = data[pos]
    pos += 1
    value = byte & 0x7F
    while byte & 0x80:
        value += 1
        if value >> 57:
            raise _IndexUnsettled
        if pos >= len(data):
            raise ends
        byte = data[pos]
        pos += 1
        value = (value << 7) + (byte & 0x7F)
    return value, pos


def _gitmodules_entries(data: bytes, *, complete: bool) -> list[tuple[int, int, bytes]]:
    """``(stage, mode, object id)`` of each entry named ``.gitmodules`` in
    the index `data`, a SHA-1 repository's, read from the first entry up to
    the first name that sorts after it (git keeps entries in name order,
    then stage). Versions 2 and 3 pad each entry to a multiple of eight
    bytes, version 4 compresses each name against the one before it; a name
    of 4,095 bytes or more is found by its terminating NUL. `data` is the
    whole index when `complete`, and otherwise a prefix of it, past whose
    end a scan that has not finished raises `_IndexShort`. Anything else
    git would refuse, or lay out otherwise, raises `_IndexUnsettled`."""
    ends = _IndexUnsettled if complete else _IndexShort
    if len(data) < 12:
        raise ends
    if data[:4] != b"DIRC":
        raise _IndexUnsettled
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3, 4):
        raise _IndexUnsettled
    count = int.from_bytes(data[8:12], "big")
    found: list[tuple[int, int, bytes]] = []
    pos = 12
    previous = b""
    for _ in range(count):
        start = pos
        flags_at = start + 40 + 20
        if flags_at + 2 > len(data):
            raise ends
        mode = int.from_bytes(data[start + 24 : start + 28], "big")
        oid = data[start + 40 : flags_at]
        flags = int.from_bytes(data[flags_at : flags_at + 2], "big")
        name_at = flags_at + 2
        if flags & 0x4000:
            if version < 3:
                raise _IndexUnsettled
            if name_at + 2 > len(data):
                raise ends
            if int.from_bytes(data[name_at : name_at + 2], "big") & ~_EXTENDED_FLAGS:
                raise _IndexUnsettled
            name_at += 2
        stage = (flags >> 12) & 0x3
        length = flags & 0x0FFF
        if version == 4:
            strip, name_at = _decode_varint(data, name_at, complete=complete)
            if strip > len(previous):
                raise _IndexUnsettled
            end = data.find(b"\0", name_at)
            if end < 0:
                raise ends
            name = previous[: len(previous) - strip] + data[name_at:end]
            if length != 0x0FFF and len(name) != length:
                raise _IndexUnsettled
            pos = end + 1
        else:
            if length == 0x0FFF:
                end = data.find(b"\0", name_at)
                if end < 0:
                    raise ends
                length = end - name_at
            name = data[name_at : name_at + length]
            if len(name) != length:
                raise ends
            pos = start + ((name_at - start + length + 8) & ~7)
        if name == _GITMODULES:
            found.append((stage, mode, oid))
        elif name > _GITMODULES:
            break
        previous = name
    return found


def _read_prefix(fd: int, length: int) -> bytes:
    """The first `length` bytes of the open file `fd`, fewer where it ends
    first."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while size < length:
        chunk = os.read(fd, length - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _index_gitmodules(gd: githead.GitDir) -> tuple[object, ...] | None:
    """What the index at `gd` holds for ``.gitmodules``: None when no entry
    (or no index), `_INDEX_UNMERGED` when only stages 1 to 3, and the
    stage-0 entry's ``(mode, object id in hex)`` otherwise, memoised on the
    index's stamp. Raises `_IndexUnsettled` where the index is not read
    exactly: a split index (a ``sharedindex.*`` file in the git directory,
    whose entries this does not open), an index version other than 2, 3 or
    4 or a layout git refuses, a SHA-256 repository (its config sets
    ``extensions.objectFormat``, or cannot be read), and an index rewritten
    in place while it was read; and OSError where a read fails."""
    config = _config_facts(gd.commondir / "config")
    if (
        config is None
        or config[1].names is None
        or b"extensions.objectformat" in config[1].names
    ):
        raise _IndexUnsettled
    path = gd.gitdir / "index"
    key = os.fspath(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        before = _fd_stamp(fd)
        if not stat.S_ISREG(before[4]):
            raise _IndexUnsettled
        with _INDEX_MEMO_LOCK:
            kept = _INDEX_MEMO.get(key)
            if kept is not None and kept[0] == before:
                _INDEX_MEMO.move_to_end(key)
                return kept[1]
        if any(name.startswith("sharedindex.") for name in os.listdir(gd.gitdir)):
            raise _IndexUnsettled
        # Read, not mapped (macOS maps a file git has just rewritten in about
        # 5 ms), and only as far as the scan goes: the entries up to
        # .gitmodules are near the start.
        size = before[2]
        want = _INDEX_FIRST_READ
        while True:
            data = _read_prefix(fd, min(want, size))
            try:
                entries = _gitmodules_entries(data, complete=len(data) >= size)
                break
            except _IndexShort:
                if want >= size:
                    raise _IndexUnsettled from None
                want *= _INDEX_READ_GROWTH
        if _fd_stamp(fd) != before:
            raise _IndexUnsettled
    finally:
        os.close(fd)
    entry: tuple[object, ...] | None = None
    for stage, mode, oid in entries:
        if stage == 0:
            entry = (mode, oid.hex())
    if entry is None and entries:
        entry = _INDEX_UNMERGED
    with _INDEX_MEMO_LOCK:
        _INDEX_MEMO[key] = (before, entry)
        _INDEX_MEMO.move_to_end(key)
        while len(_INDEX_MEMO) > _INDEX_MEMO_CAP:
            _INDEX_MEMO.popitem(last=False)
    return entry


def walk_files_signature(
    root: Path, *, directories: dict[str, tuple[int, int]] | None = None
) -> tuple[object, ...] | None:
    """The files beside the history that decide what the drift readers'
    logs list, for a memo to key on: the repository's ``config`` in the
    common directory (``log.showRoot`` there drops a root commit's paths
    from the reachable walk's ``--name-only``, ``diff.ignoreSubmodules`` a
    gitlink's, and ``log.follow`` makes a log over one path follow it
    across renames), the working tree's ``.gitmodules`` at the root
    (``submodule.<name>.ignore`` there drops a gitlink's changes from the
    walk), and what the index holds for ``.gitmodules`` where git reads it
    (`_index_gitmodules`). Git reads the index first: while it holds
    ``.gitmodules`` unmerged, git reads no ``.gitmodules`` at all, and the
    slot is `_INDEX_UNMERGED` whatever the working tree holds; otherwise the
    working tree's file where it exists, and the slot is None; and where it
    does not, the index's stage-0 entry, its mode and object, which is the
    slot, None where the index has none and git reads HEAD's copy, which
    the key's head names. A write to the index that leaves ``.gitmodules``
    as it was (``git add`` of another file, a ``git status`` that refreshes
    the index) is no new key. The config and the working tree's file are
    each their `_stamp`, or None while they do not exist. The walk memo
    (`commits_since_anchor`) and the per-hit drift memo key on it.
    `directories`, when given, receives the stamps of the root and of the
    index file (`_hold_directory`), for a caller that checks at the end of
    its work that no ``.gitmodules`` was created and removed meanwhile and
    that the index was not written, which leaves nothing kept from a search
    during which the index's entry moved and moved back.

    Read through `githead.gitdir_at`, which reads the repository only when
    `root` holds its ``.git`` entry, so a root git names elsewhere
    (``core.worktree``) is never stamped against an enclosing repository.
    None whenever GIT_INDEX_FILE is set (git reads the index it names), where
    `githead` declines (off POSIX, under GIT_DIR and the other variables
    that move discovery, a root that holds no ``.git`` entry), where the
    index is not one `_index_gitmodules` reads exactly (a split index, an
    unknown version, a SHA-256 repository), and where a stat or a read fails
    for any reason but absence; what would key on it is then not memoised.
    The configuration git reads outside that file (the global and system
    files, an include, ``config.worktree``) is not seen. Nor are two
    changes undone while a walk runs that leave every stamp as it was: a
    ``config`` created and removed in a repository that has none (the
    directory that would hold it is not held, only the root; a missing
    index is held through the git directory), and a ``config`` that is a
    symbolic link whose target's directory is swapped for another and back
    (the stamp follows the link to the file it named before, with its old
    stamp)."""
    if "GIT_INDEX_FILE" in os.environ:
        return None
    gd = githead.gitdir_at(root)
    if gd is None:
        return None
    try:
        if directories is not None:
            _hold_directory(root, directories)
            _hold_directory(gd.gitdir / "index", directories)
        gitmodules = _stamp(root / ".gitmodules")
        entry = _index_gitmodules(gd)
        if entry == _INDEX_UNMERGED or gitmodules is None:
            index = entry
        else:
            index = None
        return (_stamp(gd.commondir / "config"), gitmodules, index)
    except (OSError, ValueError, _IndexUnsettled):
        return None


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------
#
# We accept the forms `git remote get-url origin` typically emits:
#   [git@]github.com:owner/name.git         (scp-like SSH; user optional)
#   https://github.com/owner/name.git       (HTTPS)
#   ssh:// git:// git+ssh:// ssh+git://     (URL-form transports)
# plus single-segment (ownerless) paths — gitolite, Gerrit SSH, cgit-style
# HTTPS at the domain root. A small set of FIXED vendor shapes is then
# canonicalized: Azure DevOps's protocol-asymmetric clone URLs, Bitbucket
# Server's '/scm/' and Gerrit's authenticated '/a/' routing prefixes (each
# stripped only on hosts whose name carries the vendor's), and
# the first-party SSH-over-443 alias hosts. Arbitrary mount prefixes
# (GitLab installed under a relative URL root, smart-HTTP behind Apache's
# '/git/') are DELIBERATELY not stripped: an arbitrary prefix is
# syntactically indistinguishable from an owner or subgroup, and stripping
# it would merge distinct projects — the cross-project leakage this module
# fails closed against. Those remotes mismatch their SSH form (a false
# negative, the tolerated direction) rather than risk a false positive.

# Host charset excludes '/' (so slash-before-colon local paths like
# './local/path:odd' never parse as remotes) and requires >=2 chars (so
# Windows drive paths like 'C:/Users/...' fall through to the raw-equality
# fallback). The `(?!//)` lookahead keeps every scheme-prefixed URL —
# including unknown schemes — out of the scp branch.
_SSH_REMOTE_RE = re.compile(
    r"^(?:[a-zA-Z0-9_.+-]+@)?([^/:@]{2,}):(?!//)/?(?:([^/]+)/)?(.+?)(?:\.git)?/?$"
)

# Fixed, first-party alternate hostnames serving the same repositories as
# the canonical host — GitHub's and GitLab's documented SSH-over-443
# fallbacks for port-22-blocked networks. Deliberately NOT user-defined
# ssh-config aliases (github.com-work etc.), which would require reading
# the user's SSH config.
_HOST_ALIASES = {
    "ssh.github.com": "github.com",
    "altssh.gitlab.com": "gitlab.com",
}

# Fixed vendor HTTP(S) routing prefixes that precede the real owner/name:
# 'scm' (Bitbucket Server/Data Center clone URLs) and 'a' (Gerrit's
# authenticated-HTTP prefix), each paired with a host substring that must
# appear in the URL's hostname for the strip to fire. Stripping on EVERY
# host violated the never-widen invariant on nested-namespace hosts:
# GitLab subgroups make 'https://gitlab.com/scm/team/proj' a real
# top-level group named 'scm', and the unconditional strip merged it
# with the unrelated 'https://gitlab.com/team/proj' (while the same
# repo's unstripped SSH form failed to match its own HTTPS form).
# Bitbucket Server / Gerrit instances whose hostname doesn't carry the
# vendor name fall back to the tolerated false-negative direction —
# their http(s) form mismatches their ssh form — exactly like arbitrary
# mount prefixes. The strip is also gated on the remainder still
# containing '/', so a real single-char owner (github.com/a/repo) keeps
# parsing as owner='a'. Frozen by design — see the comment block above
# for why arbitrary mount prefixes stay unhandled.
_VENDOR_ROUTE_PREFIXES: dict[str, str] = {"scm": "bitbucket", "a": "gerrit"}


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    """Parse a remote URL into (host, owner, name). Returns None when the
    URL can't be parsed — caller falls back to raw string comparison.

    `owner` is "" for single-segment (ownerless) paths. The empty-owner
    sentinel keeps that relaxation collision-free: a single-segment remote
    can only ever match another single-segment remote on the same host,
    because a two-segment URL always parses with a non-empty owner.
    """
    url = url.strip()
    if not url:
        return None

    m = _SSH_REMOTE_RE.match(url)
    if m:
        host, owner, name = m.group(1), m.group(2) or "", m.group(3)
        # `name` may still carry a trailing `.git` if the regex's
        # non-greedy capture matched up to a slash before it.
        name = name.removesuffix(".git").rstrip("/")
        if not name:
            return None
        return _canonicalize(host, owner, name)

    if url.startswith(
        ("http://", "https://", "git://", "ssh://", "git+ssh://", "ssh+git://")
    ):
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = parsed.hostname or ""
        path = parsed.path.strip("/")
        if not host or not path:
            return None
        path = path.removesuffix(".git").rstrip("/")
        if not path:
            return None
        owner, _, name = path.partition("/")
        if not name:
            # Single path segment — an ownerless root-mounted repo.
            owner, name = "", owner
        elif (
            parsed.scheme in ("http", "https")
            and "/" in name
            and (hint := _VENDOR_ROUTE_PREFIXES.get(owner.lower())) is not None
            and hint in host.lower()
        ):
            # Fixed vendor routing prefix, not an owner — re-split the
            # remainder. The contains-'/' guard keeps github.com/a/repo
            # parsing as owner='a'; the host gate keeps a real top-level
            # group named 'scm'/'a' on a nested-namespace host (GitLab
            # subgroups) from merging with the repo at the stripped path.
            owner, name = name.split("/", 1)
        return _canonicalize(host, owner, name)

    return None


def _canonicalize(host: str, owner: str, name: str) -> tuple[str, str, str]:
    """Vendor normalization applied to every parsed triple.

    Maps fixed first-party alias hosts onto the canonical host and
    collapses Azure DevOps's protocol-asymmetric clone shapes onto one
    triple. Anything that doesn't exactly fit a known vendor shape passes
    through unchanged — normalization here may only ever MERGE official
    spellings of the same repository, never widen beyond them.
    """
    host = _HOST_ALIASES.get(host.lower(), host)
    azure = _canonicalize_azure(host, owner, name)
    if azure is not None:
        return azure
    return host, owner, name


def _canonicalize_azure(
    host: str, owner: str, name: str
) -> tuple[str, str, str] | None:
    """Collapse the official Azure DevOps clone forms onto
    ('dev.azure.com', org, '{project}/{repo}'). Returns None for anything
    that doesn't exactly fit an official shape — the caller keeps the
    generic parse, so non-conforming URLs degrade to today's behavior
    instead of widening matching.

    Official forms (protocol-asymmetric, hence never matching under the
    generic owner/name split):
      SSH     git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
      HTTPS   https://{org}@dev.azure.com/{org}/{project}/_git/{repo}
      legacy  https://{org}.visualstudio.com/[DefaultCollection/]{project}/_git/{repo}
      legacy  git@vs-ssh.visualstudio.com:v3/{org}/{project}/{repo}
    """
    h = host.lower()
    if h in ("ssh.dev.azure.com", "vs-ssh.visualstudio.com"):
        # Generic parse yields owner='v3', name='{org}/{project}/{repo}'.
        if owner.lower() == "v3":
            segs = name.split("/")
            if len(segs) == 3 and all(segs):
                org, project, repo = segs
                return "dev.azure.com", org, f"{project}/{repo}"
        return None
    if h == "dev.azure.com":
        # Generic parse yields owner='{org}', name='{project}/_git/{repo}'.
        segs = name.split("/")
        if len(segs) == 3 and segs[1] == "_git" and owner and segs[0] and segs[2]:
            return "dev.azure.com", owner, f"{segs[0]}/{segs[2]}"
        return None
    if h.endswith(".visualstudio.com"):
        # The org is the subdomain; the path may carry a leading
        # 'DefaultCollection' segment on older clones.
        org = h.removesuffix(".visualstudio.com")
        if not org or "." in org:
            return None
        segs = [owner, *name.split("/")] if owner else name.split("/")
        if segs and segs[0] == "DefaultCollection":
            segs = segs[1:]
        if len(segs) == 3 and segs[1] == "_git" and segs[0] and segs[2]:
            return "dev.azure.com", org, f"{segs[0]}/{segs[2]}"
        return None
    return None


__all__ = [
    "MAX_PATCH_STREAM_COMMITS",
    "Origin",
    "attribute_files_signature",
    "capture",
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
