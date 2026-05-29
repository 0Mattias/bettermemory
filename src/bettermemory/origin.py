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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

log = logging.getLogger("bettermemory.origin")


class Origin(BaseModel):
    """Where a memory was written from. All fields optional.

    A memory with `origin = None` is global (e.g. written before this
    feature shipped). A memory with `origin.cwd` set but `origin.repo`
    null was written outside any git repo. A memory with `origin.repo`
    set but a different value from the caller's current repo is
    cross-project and gets filtered out by `auto_scope=True`.

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


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(cwd: Path | None = None) -> Origin:
    """Snapshot the current working context.

    `cwd` is parameterized for testability — production usage passes None
    and we read `Path.cwd()`. If git isn't on PATH or the directory isn't
    a repo, `repo` and `branch` come back null; `cwd` is always populated
    when the directory exists.

    `worktree_root` is captured whenever we're inside any git directory
    (gated on `repo_url` so the helper bails on the same `_git` failure
    instead of paying a second subprocess). Two worktrees of the same
    repository will have the same `repo` but different `worktree_root`,
    which is what the auto-scope filter uses to keep a memory written
    from one worktree from leaking into a search run from a sibling
    worktree.

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

    repo_url = _git_remote_url(resolved)
    branch = _git_branch(resolved) if repo_url else None
    worktree_root = _git_worktree_root(resolved) if repo_url else None

    return Origin(
        cwd=cwd_str,
        repo=repo_url,
        branch=branch,
        worktree_root=worktree_root,
    )


# ---------------------------------------------------------------------------
# Repo equality — what "same project" means for the auto-scope filter
# ---------------------------------------------------------------------------


def repos_match(memory_repo: str | None, current_repo: str | None) -> bool:
    """True if a memory whose origin.repo is `memory_repo` belongs to a
    caller whose current repo is `current_repo`.

    Equality is normalized: `git@github.com:owner/repo.git` and
    `https://github.com/owner/repo` and `https://github.com/owner/repo.git`
    all describe the same project. We compare on `(host, owner, name)`
    rather than raw URL strings.

    A null `memory_repo` is "global" — matches any current_repo. A null
    `current_repo` (caller is not in a repo) also matches any
    `memory_repo` since we have no project boundary to enforce.
    """
    if memory_repo is None or current_repo is None:
        return True
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
    matching. Both sides set → string equality on the resolved
    paths captured by `_git_worktree_root`.
    """
    if memory_worktree is None or caller_worktree is None:
        return True
    return memory_worktree == caller_worktree


def should_include_for_caller(
    memory_origin: Origin | None,
    caller_repo: str | None,
    *,
    caller_worktree_root: str | None = None,
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
    if not repos_match(memory_repo, caller_repo):
        return False
    memory_worktree = memory_origin.worktree_root if memory_origin else None
    return worktrees_match(memory_worktree, caller_worktree_root)


# ---------------------------------------------------------------------------
# Git helpers — shell out, swallow failures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, timeout: float = 1.0) -> str | None:
    """Run a git command from `cwd`. Returns trimmed stdout on success,
    None on any failure. Short timeout so a hanging git never stalls a
    memory_write — the write is the user-facing operation; origin is
    nice-to-have.

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
    return out or None


def _git_remote_url(cwd: Path) -> str | None:
    return _git(cwd, "config", "--get", "remote.origin.url")


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

    Used by the commit-drift staleness signal in `verify.py`: a cwd-aware
    advisory that surfaces "the project moved while this memory's
    `last_verified_at` did not." `since` is normalised to UTC ISO-8601
    before being handed to git so the comparison matches the timestamp
    semantics elsewhere in the store. A naive `since` is treated as UTC.

    Counted in author-date space (git's default for `--since`). Boundary
    semantics are git's: `--since` is INCLUSIVE (a commit authored at
    exactly `since` IS counted) and git ignores sub-second precision, so a
    commit and a verify landing in the same whole second count as "since".
    This can diverge by one from the `commit_author_timestamps` + bisect
    path (which is exclusive and microsecond-precise) only at that
    same-second boundary — immaterial for an advisory signal. The
    author-vs-commit-date distinction likewise rarely matters here and
    matches what a human reading `git log --since` would expect.

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


def commits_since_touching_paths(
    cwd: Path | None,
    since: datetime,
    paths: list[str],
) -> int | None:
    """Count commits in `cwd`'s repo authored after `since` that touched
    any of `paths`.

    `paths` may contain absolute paths, ``~/``-prefixed paths, or paths
    relative to the repo root. We expand ``~`` and resolve absolute
    paths against the repo root so git sees a relative pathspec — git
    won't filter on absolute paths that escape the repo. Paths outside
    the repo (or that don't resolve) are dropped silently; if everything
    drops, we return None to signal "no useful filter, fall back to the
    unfiltered count".

    Returns the integer count when git is reachable and produced a
    parseable result, None on any failure (cwd is None, git not on
    PATH, not a repo, all paths filtered out, parse error). The
    semantics mirror `commits_since` so the verified-paths
    short-circuit in `verify.compute_commit_drift` can drop in cleanly.

    Used by the change-7 path-filtered drift downgrade: when a memory
    was verified for a known set of paths, commits that don't touch any
    of them shouldn't trip the drift signal — the world the memory was
    checking against hasn't moved even if the project as a whole has.
    """
    if cwd is None or not paths:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    iso = since.astimezone(timezone.utc).isoformat()

    # Resolve the repo root once. `git rev-parse --show-toplevel` returns
    # the repo's root absolute path; we compare each pathspec against it
    # so anything outside the repo is dropped before reaching git (git
    # would otherwise raise "ambiguous argument" or silently produce no
    # output, depending on the form).
    toplevel_raw = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel_raw is None:
        return None
    try:
        toplevel = Path(toplevel_raw).resolve()
    except OSError:
        return None

    # Pathspecs are repo-root-relative, but `_git` runs with `cwd` =
    # the caller's working directory, which may be a SUBDIRECTORY of the
    # repo (an MCP server / agent launched from or chdir'd into `src/`,
    # `packages/foo/`, …). Git resolves a plain pathspec relative to the
    # invocation cwd, so a root-relative `src/foo.py` would match nothing
    # from a subdir and rev-list would return 0 — silently reporting a
    # genuinely-drifted verified path as clean. Prefix each with git's
    # `:/` (`:(top)`) magic, which anchors the path at the top of the
    # working tree regardless of cwd.
    pathspecs: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            resolved = Path(raw).expanduser()
            if not resolved.is_absolute():
                # Treat a relative path as already-relative-to-repo-root.
                pathspecs.append(":/" + str(resolved))
                continue
            resolved = resolved.resolve()
        except (OSError, ValueError):
            continue
        try:
            rel = resolved.relative_to(toplevel)
        except ValueError:
            # Path escapes the repo — drop it. Git can't filter on
            # something outside its working tree.
            continue
        pathspecs.append(":/" + str(rel))

    if not pathspecs:
        return None

    raw_count = _git(
        cwd,
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


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------
#
# We accept the two forms `git config --get remote.origin.url` typically
# emits:
#   git@github.com:owner/name.git           (SSH)
#   https://github.com/owner/name.git       (HTTPS)
# plus a few less-common variants.

_SSH_REMOTE_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@([^:]+):/?([^/]+)/(.+?)(?:\.git)?/?$")


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    """Parse a remote URL into (host, owner, name). Returns None when the
    URL can't be parsed — caller falls back to raw string comparison."""
    url = url.strip()
    if not url:
        return None

    m = _SSH_REMOTE_RE.match(url)
    if m:
        host, owner, name = m.group(1), m.group(2), m.group(3)
        # `name` may still carry a trailing `.git` if the regex's
        # non-greedy capture matched up to a slash before it.
        name = name.removesuffix(".git").rstrip("/")
        return host, owner, name

    if url.startswith(("http://", "https://", "git://", "ssh://")):
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = parsed.hostname or ""
        path = parsed.path.strip("/")
        if not host or "/" not in path:
            return None
        path = path.removesuffix(".git").rstrip("/")
        parts = path.split("/", 1)
        if len(parts) != 2:
            return None
        return host, parts[0], parts[1]

    return None


__all__ = [
    "Origin",
    "capture",
    "commit_author_timestamps",
    "commits_since",
    "commits_since_touching_paths",
    "repos_match",
    "should_include_for_caller",
    "worktrees_match",
]
