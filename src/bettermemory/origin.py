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

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel


class Origin(BaseModel):
    """Where a memory was written from. All fields optional.

    A memory with `origin = None` is global (e.g. written before this
    feature shipped). A memory with `origin.cwd` set but `origin.repo`
    null was written outside any git repo. A memory with `origin.repo`
    set but a different value from the caller's current repo is
    cross-project and gets filtered out by `auto_scope=True`.
    """

    cwd: str | None = None
    repo: str | None = None  # raw remote URL or null
    branch: str | None = None  # current branch or null (detached HEAD → null)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(cwd: Path | None = None) -> Origin:
    """Snapshot the current working context.

    `cwd` is parameterized for testability — production usage passes None
    and we read `Path.cwd()`. If git isn't on PATH or the directory isn't
    a repo, `repo` and `branch` come back null; `cwd` is always populated
    when the directory exists.
    """
    resolved = (cwd or Path.cwd()).resolve()
    cwd_str = str(resolved)

    repo_url = _git_remote_url(resolved)
    branch = _git_branch(resolved) if repo_url else None

    return Origin(cwd=cwd_str, repo=repo_url, branch=branch)


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


# ---------------------------------------------------------------------------
# Git helpers — shell out, swallow failures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, timeout: float = 1.0) -> str | None:
    """Run a git command from `cwd`. Returns trimmed stdout on success,
    None on any failure. Short timeout so a hanging git never stalls a
    memory_write — the write is the user-facing operation; origin is
    nice-to-have."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
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


def commits_since(cwd: Path | None, since: datetime) -> int | None:
    """Count commits in `cwd`'s repo authored after `since`.

    Returns the integer count when the directory is a git repo we can
    read, None on any failure (cwd is None, git not on PATH, not a repo,
    no commits, git timed out, output not parseable as an int). Zero is
    a real value — the repo is fine, nothing has landed since.

    Used by the commit-drift staleness signal in `verify.py`: a cwd-aware
    advisory that surfaces "the project moved while this memory's
    `last_verified_at` did not." `since` is normalised to UTC ISO-8601
    before being handed to git so the comparison matches the timestamp
    semantics elsewhere in the store. A naive `since` is treated as UTC.

    Counted in author-date space (git's default for `--since`). The
    distinction from commit-date rarely matters for an advisory signal
    and matches what a human reading `git log --since` would expect.

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

    pathspecs: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            resolved = Path(raw).expanduser()
            if not resolved.is_absolute():
                # Treat a relative path as already-relative-to-repo-root.
                pathspecs.append(str(resolved))
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
        pathspecs.append(str(rel))

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
]
