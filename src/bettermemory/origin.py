"""Working-context capture for memory_write — what cwd / repo / branch the
write happened in.

Captured at write time, never at retrieval. The cwd of a long-lived MCP
server can drift; capturing at retrieval would mean a memory's origin
silently changes meaning. Capture-at-write makes origin a durable
property of the memory record.

Used by `memory_search(auto_scope=True)` to default-filter results by the
caller's current repo. The most embarrassing failure mode this addresses is
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

import logging
import re
import subprocess
import warnings
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, PrivateAttr

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


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(cwd: Path | None = None) -> Origin:
    """Snapshot the current working context.

    `cwd` is parameterized for testability — production usage passes None
    and we read `Path.cwd()`. If git isn't on PATH or the directory isn't
    a repo, `repo` and `branch` come back null; `cwd` is always populated
    when the directory exists.

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
    """
    if cwd is None:
        try:
            resolved = Path.cwd().resolve()
        except (FileNotFoundError, OSError):
            return Origin()
    else:
        resolved = cwd.resolve()
    cwd_str = str(resolved)

    worktree_root = _git_worktree_root(resolved)
    repo_url: str | None = None
    repo_url_alternates: tuple[str, ...] = ()
    if worktree_root:
        repo_url, repo_url_alternates = _git_remote_url_and_alternates(resolved)
    branch = _git_branch(resolved) if worktree_root else None

    origin = Origin(
        cwd=cwd_str,
        repo=repo_url,
        branch=branch,
        worktree_root=worktree_root,
    )
    if repo_url is not None:
        # Register the remote's other official spellings (raw multi-URL
        # values, unexpanded insteadOf aliases) so `repos_match` can keep
        # recognizing stored origins captured under the pre-3.10
        # `git config --get` idiom — see `_CALLER_REPO_ALTERNATES`. Also
        # carried on the returned Origin (private, never persisted) for
        # callers that hold the object.
        _register_caller_alternates(repo_url, repo_url_alternates)
        origin._repo_url_alternates = repo_url_alternates
    return origin


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
       forever. When the recorded root no longer exists on disk,
       degrade to repo-level matching — there is no live workspace
       left to isolate from.
    """
    if memory_worktree is None or caller_worktree is None:
        return True
    if memory_worktree == caller_worktree:
        return True
    if _primary_root_of(caller_worktree) == memory_worktree:
        return True
    try:
        if not Path(memory_worktree).exists():
            return True
    except OSError:
        return True
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
    `verify`, `server._attach_commit_drift_counts`, and the
    `health.compute_commit_drift_*` rollups need a stricter check that
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


def _git(
    cwd: Path, *args: str, timeout: float = 1.0, empty_ok: bool = False
) -> str | None:
    """Run a git command from `cwd`. Returns trimmed stdout on success,
    None on any failure. Short timeout so a hanging git never stalls a
    memory_write — the write is the user-facing operation; origin is
    nice-to-have.

    `empty_ok` splits "git ran fine, no output" from "git could not run":
    with it True a zero-exit call with EMPTY stdout returns ``""`` instead
    of None, so a caller can tell a clean-but-empty result (`git log --
    <specs>` listing no commit) apart from an actual failure (non-zero
    exit, missing binary, timeout — still None). Default False keeps the
    historical ``out or None`` collapse every other caller relies on.

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
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        log.warning("git binary not found on PATH: %s", exc)
        return None
    except subprocess.TimeoutExpired:
        log.warning(
            "git %s timed out after %ss in %s",
            args[0] if args else "",
            timeout,
            cwd,
        )
        return None
    except OSError as exc:
        log.warning("git invocation failed in %s: %s", cwd, exc)
        return None
    if result.returncode != 0:
        # Trim stderr to the first line — git's "fatal: ..." messages are
        # one-liners; deeper output is rare and not worth flooding the log
        # with. Empty stderr is still logged so the returncode itself is
        # at least visible at DEBUG.
        stderr = (result.stderr or "").strip().splitlines()
        first = stderr[0] if stderr else ""
        log.debug(
            "git %s exited %s in %s: %s",
            args[0] if args else "",
            result.returncode,
            cwd,
            first,
        )
        return None
    out = result.stdout.strip()
    if empty_ok:
        return out
    return out or None


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


def _git_worktree_root(cwd: Path) -> str | None:
    # `rev-parse --show-toplevel` returns the absolute path of the
    # working tree root — for a primary checkout, the repo root; for
    # a worktree (`git worktree add`), the worktree's own root, which
    # *differs* between sibling worktrees of the same repository.
    # That difference is exactly what the auto-scope filter needs to
    # tell two worktrees of one repo apart. Resolved through `Path`
    # to normalise symlink hops on macOS' `/var` → `/private/var`
    # idiom, so a memory captured under one symlink form still
    # compares equal to a caller that resolves the other.
    raw = _git(cwd, "rev-parse", "--show-toplevel")
    if raw is None:
        return None
    try:
        return str(Path(raw).resolve())
    except OSError:
        return raw


def commits_since(cwd: Path | None, since: datetime) -> int | None:
    """Count commits in `cwd`'s repo authored at-or-after `since`.

    Returns the integer count when the directory is a git repo we can
    read, None on any failure (cwd is None, git not on PATH, not a repo,
    no commits, git timed out, output not parseable as an int). Zero is
    a real value — the repo is fine, nothing has landed since.

    `since` is normalised to UTC ISO-8601 before being handed to git so
    the comparison matches the timestamp semantics elsewhere in the store.
    A naive `since` is treated as UTC.

    Counted in COMMITTER-date space: `git rev-list --since` filters on
    committer date, NOT author date. Boundary semantics are git's:
    `--since` is INCLUSIVE and ignores sub-second precision.

    NOTE — no longer on the commit-drift hot path. `compute_commit_drift`
    in `verify.py` (the memory_show signal) used to call this, which made
    memory_show diverge from memory_search / the health rollup on TWO axes:
    committer-vs-author date (a rebase rewrites committer date but preserves
    author date — `sync` rebases on every pull) AND inclusive-whole-second
    vs strictly-greater-microsecond boundary (a commit in the same UTC
    second as `last_verified_at` counted on memory_show but not on the
    bisect path). memory_show now shares the `commit_author_timestamps`
    + `bisect_right` path, so all three surfaces agree. This function is
    retained as a standalone single-`since` author/committer-agnostic count
    (still unit-tested) and for any caller that explicitly wants committer-
    date inclusive semantics; the commit-drift surfaces deliberately do not.

    For batch use against many `since` values from the same repo, prefer
    `commit_author_timestamps` + bisect — one git call instead of N.
    """
    if cwd is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    iso = since.astimezone(timezone.utc).isoformat()
    raw = _git(cwd, "rev-list", "--count", f"--since={iso}", "HEAD")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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


def commits_touching_pathspecs(
    cwd: Path | None,
    since: datetime,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> int | None:
    """Count commits authored after `since` touching any of `pathspecs`.

    `pathspecs` must already be repo-root-relative forward-slash specs —
    the output of `resolve_repo_pathspecs`. Returns the integer count, or
    None on any git failure (cwd is None, empty pathspecs, git not on
    PATH, not a repo, parse error). Counted in COMMITTER-date space with
    git's inclusive `--since` boundary — see the callers' `count > 0`
    guards for why the author-date bisect count stays authoritative.
    """
    if cwd is None or not pathspecs:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    iso = since.astimezone(timezone.utc).isoformat()
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw_count = _git(
        toplevel,
        "rev-list",
        "--count",
        f"--since={iso}",
        "HEAD",
        "--",
        *pathspecs,
    )
    if raw_count is None:
        return None
    try:
        return int(raw_count)
    except ValueError:
        return None


def commits_since_touching_paths(
    cwd: Path | None,
    since: datetime,
    paths: list[str],
) -> int | None:
    """DEPRECATED — slated for removal in 4.0; every call emits a
    ``DeprecationWarning``. Count commits in `cwd`'s repo after `since`
    (COMMITTER-date space, git's inclusive ``--since``) that touched any
    of `paths`.

    Retained through the 3.x line only because it is shipped public API
    (`__all__`) and removal would be a semver break — NO production code
    path calls it anymore. The commit-drift surfaces replaced this
    composition with `resolve_repo_pathspecs` +
    `commit_author_timestamps_touching_pathspecs` (the author-date
    ``git log`` behind `verify.resolve_commit_drift_count`) because BOTH
    of its defining semantics were deliberately abandoned — do NOT wire
    it back into the drift path:

    - **Committer-date inflation.** ``git rev-list --since`` filters on
      COMMITTER date, which a rebase rewrites while preserving author
      date (`sync` rebases on every pull), so counts inflate past the
      author-date truth all three drift surfaces (memory_show,
      memory_search, the health rollup) now agree on.
    - **None-on-all-dropped ambiguity.** The legacy contract collapses
      "every path dropped" (the claims don't anchor this repo — drift
      *not applicable*) into the same ``None`` as "git couldn't answer"
      (fall back to the unfiltered count), erasing the ``[]``-vs-``None``
      distinction the claim-anchored policy depends on.

    Legacy contract, unchanged until removal: composition of
    `resolve_repo_pathspecs` + `commits_touching_pathspecs` returning
    None on ANY failure — cwd is None, no paths, not a repo, every path
    dropped — so a caller treats every non-answer as "no useful filter,
    fall back to the unfiltered count".
    """
    warnings.warn(
        "commits_since_touching_paths is deprecated and will be removed in "
        "bettermemory 4.0; use resolve_repo_pathspecs + "
        "commit_author_timestamps_touching_pathspecs (the author-date source "
        "behind verify.resolve_commit_drift_count) instead of this "
        "committer-date composition",
        DeprecationWarning,
        stacklevel=2,
    )
    if cwd is None or not paths:
        return None
    toplevel = repo_toplevel(cwd)
    if toplevel is None:
        return None
    pathspecs = resolve_repo_pathspecs(cwd, paths, toplevel=toplevel)
    if not pathspecs:
        return None
    return commits_touching_pathspecs(cwd, since, pathspecs, toplevel=toplevel)


def commit_author_timestamps(cwd: Path | None) -> list[datetime] | None:
    """All author timestamps from the HEAD history of `cwd`'s repo.

    Returns a list of timezone-aware datetimes, or None on any failure
    (cwd is None, git not on PATH, not a repo, no commits, parse error
    on every line). An empty list is theoretically possible but almost
    never happens — `git log` on a repo with no commits exits non-zero
    and we surface that as None. Lines that fail to parse are skipped
    individually rather than poisoning the whole result.

    Sort order is whatever git emits (newest-first by default) — callers
    that want sorted-ascending for bisect should sort explicitly. Used
    by the health rollup to count commits-since for many memories from
    one git invocation; the per-memory `commits_since` would otherwise
    pay a fork+exec for every row.
    """
    if cwd is None:
        return None
    raw = _git(cwd, "log", "--format=%aI", "HEAD")
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
    return out if out else None


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
      commits, in git's emit order (newest-first); sort before bisect. A
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
    raw = _git(toplevel, "log", "--format=%aI", "HEAD", "--", *pathspecs, empty_ok=True)
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
    return out or None


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
    "Origin",
    "capture",
    "commit_author_timestamps",
    "commit_author_timestamps_touching_pathspecs",
    "commits_since",
    "commits_since_touching_paths",
    "commits_touching_pathspecs",
    "repo_toplevel",
    "repos_match",
    "resolve_repo_pathspecs",
    "should_include_for_caller",
    "worktrees_match",
]
