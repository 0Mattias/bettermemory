"""Git-based sync for the memory store (T4.1 of the v1.6 plan).

The memory directory is plain markdown files plus a few regenerable
caches; git already handles every interesting case (history,
distributed copies, conflict resolution, three-way merge). Rather
than build a custom sync protocol, this module wraps git into a
small CLI surface that does the right thing by default:

- ``bettermemory sync init [--remote URL]`` — initialise the memory
  directory as a git repo and write a ``.gitignore`` that excludes
  the regenerable caches (the FTS5 index, the event log + its
  rotations, the embedding cache, lock files, doctor probes).
- ``bettermemory sync status`` — porcelain-style summary of pending
  changes plus the remote tracking position when a remote is set.
- ``bettermemory sync push [--remote NAME]`` — stage everything,
  commit with a default message, push to the named remote (default
  ``origin``). No-op when there are no changes.
- ``bettermemory sync pull [--remote NAME] [--no-reindex]`` — pull
  with rebase, then rebuild the FTS5 index from the new file
  contents (changes that landed via merge bypassed the Store
  hooks, so the index is stale until rebuilt).
- ``bettermemory sync auto`` — convenience: pull-rebase, then push.
  The "sync me" one-shot for cron / shell aliases.

Out of scope: conflict resolution for clashing memory edits. Git's
default three-way merge handles non-overlapping edits perfectly;
true content conflicts surface as merge conflicts the user resolves
by hand. The memory dir is small enough and edits rare enough that
this hasn't been a problem in practice.

Why not git directly? The wrapper buys:

- A vetted ``.gitignore`` so users don't accidentally check the
  derived caches into their history.
- A post-pull ``reindex`` so the FTS5 index reflects the new
  files (the Store hooks only fire on runtime writes).
- Sensible defaults (no commit when nothing changed, --rebase for
  pull) so a five-second sync from a shell alias doesn't grow
  into a five-line ceremony.
- Future hook points: a pre-push doctor check, a sync-time
  conflict-detection pass against the tombstone audit trail,
  etc., when those become useful.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._fsutil import atomic_write_bytes, flock_excl
from .consolidate import AUTO_CONSOLIDATE_CLOCK_FILENAME
from .doctor import DOCTOR_PROBE_FILENAME
from .episodes import EPISODES_DIR
from .events import EVENT_LOG_FILENAME
from .index import INDEX_FILENAME
from .ingest import INGEST_WATERMARK_FILENAME
from .proposals import PROPOSALS_FILENAME
from .semantic import EMBEDDING_FILENAME_PREFIX, EMBEDDING_FILENAME_SUFFIX

# Coarse store-wide lock for push/pull. The git operations the sync
# wrapper invokes (`git add -A`, `git commit`, `git pull --rebase`)
# are not atomic with respect to the in-process Store: `add -A`
# snapshots whatever is on disk at that instant, so a concurrent
# `Store.write` mid-`add` ships a half-written file-set; `pull
# --rebase` can `checkout` over a file another process holds open
# for write. The lock turns each sync op into an atomic boundary.
# The lockfile lives at `<root>/.sync.lock` (created by
# `flock_excl` from the input path `<root>/.sync`).
_SYNC_LOCK_NAME = ".sync"


log = logging.getLogger("bettermemory.sync")


# Files the runtime regenerates from the canonical markdown — never
# check these into a sync repo. Keeping them out of git also keeps
# the diff noise down to actual memory edits, which is what a
# `git log` over the store should show.
_GITIGNORE_LINES = [
    "# bettermemory: regenerable / transient — never check in",
    INDEX_FILENAME,
    # SQLite WAL/SHM sidecar files for the same index DB. Suffixes are
    # SQLite-side, not bettermemory-side, so they're concatenated here
    # rather than living as their own module-level constants.
    f"{INDEX_FILENAME}-shm",
    f"{INDEX_FILENAME}-wal",
    EVENT_LOG_FILENAME,
    f"{EVENT_LOG_FILENAME}.*.gz",
    # Per-shard active event segments (`.events.00.jsonl` …). Same
    # host-local, regenerable status as the legacy single log above —
    # and the same privacy stake: they carry session ids and (verbatim
    # mode) raw query text. `git add -A` would otherwise push them to
    # every clone. The pattern matches the sharded names but not the
    # legacy `.events.jsonl` (already covered on the line above).
    ".events.*.jsonl",
    f"{EMBEDDING_FILENAME_PREFIX}*{EMBEDDING_FILENAME_SUFFIX}",
    # Write-reflex proposal queue. Host-local, transient state like the
    # event log — but with a sharper edge: it holds RAW captured user text
    # that never passed the write-path credential gate, so a secret-shaped
    # capture ("my staging DB password is …") sits here verbatim until the
    # model reviews it. Without this line `sync push`'s `git add -A` would
    # stage, commit, and push that plaintext secret to every clone, and git
    # history is permanent. The queue is never meant to leave the host that
    # captured it. (`extract_proposals` also drops credential-shaped
    # sentences at capture as defense-in-depth; this keeps the whole queue —
    # including non-secret captures the user may not want synced — local.)
    PROPOSALS_FILENAME,
    # Ingest watermark. Maps ABSOLUTE source-file paths on THIS host (e.g.
    # `~/.claude/projects/<sanitized-cwd>/memory/*.md`) to the content hashes
    # already imported, so `doctor`'s stranded-auto-memory check can tell
    # "never ingested" from "ingested, then curated". Both halves are
    # host-local: the paths do not exist on another machine, and a clone that
    # inherited them would believe it had already imported sources it has
    # never seen — suppressing the very check the watermark exists to feed.
    INGEST_WATERMARK_FILENAME,
    # Auto-consolidate debounce clock: the ISO timestamp of the last
    # auto-consolidate DECISION on THIS host. Syncing it makes every clone
    # overwrite the others' debounce state — a pull could mark a host as
    # "just consolidated" when it never has, or conflict on a file that is
    # rewritten on every decision. Host-local by construction, like the
    # event log it was deliberately decoupled from.
    AUTO_CONSOLIDATE_CLOCK_FILENAME,
    # Episode tier — host-local BY DESIGN (decided 2026-07-11; before this
    # line it synced only by omission). Episodes are the transient sibling of
    # memory: session run-state whose bodies carry host-absolute
    # `origin.worktree_root` paths, pruned by an mtime-based TTL that a
    # clone's `git checkout` would silently defeat (checkout rewrites mtimes,
    # so a pulled session dir looks freshly written and never ages out on
    # schedule). Cross-host continuity wouldn't even work: `episode_handoff`
    # adoption is worktree-strict on those absolute paths, so a synced
    # episode is filtered on arrival. The slash-free name matches the
    # directory at any depth and gitignore ignores everything beneath it.
    # `.tombstones/`, by contrast, is canonical store data and stays synced —
    # a removal on one host must remain restorable from every clone.
    EPISODES_DIR,
    "*.lock",
    # Orphaned atomic-write temp files. `_fsutil.atomic_write_bytes` writes
    # `<target>.<random>.tmp` next to its target and only unlinks it inside a
    # caught-exception `finally` — a hard crash / SIGKILL / power loss between
    # tmp creation and `os.replace` leaves the orphan behind. Crucially that
    # orphan carries the SAME payload as the file it was about to become: a
    # full memory body (`<id>.md.<rand>.tmp`) or the raw-capture proposals
    # queue (`.write_proposals.jsonl.<rand>.tmp`, host-local by design, never
    # to sync). Without this glob the next `sync push`'s `git add -A` stages,
    # commits, and pushes that plaintext orphan to every clone, where git
    # history makes it permanent — precisely the leak class the
    # PROPOSALS_FILENAME line above closes for the committed queue, reopened
    # through the tmp sidecar. Every atomic writer in the codebase shares this
    # `.tmp` suffix (`tempfile.NamedTemporaryFile(..., suffix=".tmp")`), so one
    # glob covers them all.
    "*.tmp",
    DOCTOR_PROBE_FILENAME,
]


# Default commit message when push is run without a custom one. The
# format is deliberately mechanical — the per-memory commit history
# isn't meaningful (writes are batched), so the wrapper commit just
# anchors a sync point. Users who want richer history should
# commit by hand and run `sync push` without staging changes.
DEFAULT_COMMIT_MESSAGE = "bettermemory: sync"


class SyncError(RuntimeError):
    """Raised on git or filesystem failures the wrapper can't recover
    from. The CLI catches this and prints a clean error rather than
    a traceback."""


@dataclass
class SyncStatus:
    """Snapshot of the memory dir's git state. Returned by
    ``status()``; rendered to text by the CLI."""

    is_repo: bool
    branch: str | None
    has_changes: bool
    untracked: list[str]
    modified: list[str]
    remote_url: str | None
    ahead: int
    behind: int

    def to_dict(self) -> dict[str, object]:
        return {
            "is_repo": self.is_repo,
            "branch": self.branch,
            "has_changes": self.has_changes,
            "untracked_count": len(self.untracked),
            "modified_count": len(self.modified),
            "remote_url": self.remote_url,
            "ahead": self.ahead,
            "behind": self.behind,
        }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_git() -> str:
    """Resolve the git binary or raise a clear error. We resolve once
    per command rather than caching so a user who installs git
    mid-session doesn't have to restart the server to use sync."""
    binary = shutil.which("git")
    if binary is None:
        raise SyncError(
            "git executable not found on PATH. Install git, then re-run "
            "this command. The wrapper only delegates — there's no "
            "vendored git client."
        )
    return binary


# Conservative charset for remote and branch names passed positionally
# into git. Git's own rules are wider, but we only let a `remote` /
# `default_branch` parameter through if it's clearly a name, not a flag.
# Leading `-` is the specific footgun: a positional arg that starts with
# a dash can get parsed as an option in some argv contexts (e.g.
# `git remote add --exec=…`). The check is belt-and-suspenders alongside
# the existing argv-list invocation pattern, which already closes the
# shell-injection surface.
_GIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _require_git_name(label: str, value: str) -> str:
    if not _GIT_NAME_RE.fullmatch(value):
        raise SyncError(
            f"{label} {value!r} contains characters outside the safe "
            f"set [A-Za-z0-9._/-] (or starts with a dash). Pick a name "
            f"like 'origin' / 'main', not a flag."
        )
    return value


def _redact_url(url: str | None) -> str | None:
    """Strip embedded credentials (``user:token@host``) from a remote
    URL before it's printed to stdout / status JSON / error messages.

    Git accepts URLs like ``https://user:ghp_token@github.com/...`` for
    HTTPS auth. The credential lives in the user's git config (intended
    storage) but our CLI was also surfacing it in init action strings
    and status output — so a `bettermemory sync status --json` piped
    into CI logs would leak the token. Redact the userinfo segment
    while keeping enough of the URL to recognise which remote it is.
    """
    if url is None or "@" not in url:
        return url
    # Use urlparse rather than regex so e.g. ssh URLs (`git@host:path`)
    # without a scheme are left alone — they don't have credentials to
    # leak, the `git@` is a username not a token.
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.hostname:
        return url
    if not (parsed.username or parsed.password):
        return url
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    redacted = parsed._replace(netloc=host)
    return urlunparse(redacted)


def _run_git(
    root: Path,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand in `root`. By default raises `SyncError`
    on non-zero exit so the CLI can catch one exception type. Pass
    `check=False` for commands where the exit code is informational
    (e.g. `git diff --quiet` returns 1 to mean "there are diffs")."""
    binary = _require_git()
    cmd = [binary, *args]
    result = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        # Redact credentials from stderr / stdout before they land in
        # the SyncError message — `git push` failures often echo the
        # full remote URL, which for HTTPS auth includes the token.
        stderr = _redact_text(result.stderr.strip())
        stdout = _redact_text(result.stdout.strip())
        raise SyncError(f"`git {' '.join(args)}` failed in {root}: {stderr or stdout}")
    return result


def _redact_text(text: str) -> str:
    """Mask `scheme://user:token@host` patterns inside arbitrary text.

    Git error output isn't structured, so we can't always isolate the
    URL field — this is a regex fallback for the SyncError path. The
    pattern is conservative: it only matches when both a scheme and
    an `@` separator are present, so unrelated `@` characters in error
    text (e.g. branch refs like `@{u}`) are untouched.
    """
    return re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^@\s/]+)@",
        r"\1<redacted>@",
        text,
    )


def _is_repo(root: Path) -> bool:
    """True iff `root` is the top of a git working tree. Avoids the
    edge case where `root` is *inside* a parent repo but not itself a
    repo — that's a different setup the wrapper shouldn't conflate
    with an initialised store. The nested shape isn't unwatched,
    though: doctor's `store_nested_in_parent_repo` check flags any
    `_GITIGNORE_LINES` sidecar the PARENT repo tracks under the store
    (a leak surface this top-of-worktree gate deliberately makes
    invisible to sync itself)."""
    try:
        result = _run_git(
            root,
            ["rev-parse", "--show-toplevel"],
            check=False,
        )
    except SyncError:
        return False
    if result.returncode != 0:
        return False
    return Path(result.stdout.strip()).resolve() == root.resolve()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def init(
    root: Path,
    *,
    remote: str | None = None,
    default_branch: str = "main",
) -> dict[str, object]:
    """Initialise the memory dir as a git repo and write the
    ``.gitignore``. Idempotent: if the directory is already a repo,
    we only refresh the gitignore (so a future expansion of the
    excluded-files list takes effect without a re-init) and
    optionally update the remote URL.

    Returns a small status dict the CLI surfaces to the caller.
    """
    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise SyncError(f"memory directory {root} does not exist")
    if not root.is_dir():
        raise SyncError(f"memory directory {root} is not a directory")

    _require_git_name("default_branch", default_branch)

    actions: list[str] = []
    already_repo = _is_repo(root)
    if not already_repo:
        _run_git(root, ["init", "--initial-branch", default_branch])
        actions.append(f"initialised git repo on branch {default_branch!r}")
    else:
        actions.append("repo already initialised")

    # Refreshing `.gitignore` here heals an existing sync repo for FUTURE
    # writes: a newly-ignored path (e.g. the proposals queue, added to
    # `_GITIGNORE_LINES`) stops being staged on the next `sync push`. It does
    # NOT untrack a file that a PRE-fix repo already committed — gitignore is
    # silent on tracked paths, so a repo initialised before a pattern joined
    # `_GITIGNORE_LINES` keeps pushing that sidecar (e.g. the plaintext
    # `.write_proposals.jsonl` capture queue) until it is untracked. That
    # migration gap is doctor's `sync_tracked_ignored` check
    # (`doctor._check_sync_tracked_ignored`): it fnmatches `git ls-files`
    # output against `_GITIGNORE_LINES` and prints the `git rm --cached` +
    # history-rewrite (git-filter-repo / BFG) remediation for any sidecar a
    # pre-fix repo still tracks. Untracking is deliberately NOT automated
    # here — rewriting the user's index (let alone pushed history) is an
    # operator decision, not an init() side effect.
    gitignore = root / ".gitignore"
    desired = "\n".join(_GITIGNORE_LINES) + "\n"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if current != desired:
        # Atomic + durable write: a plain `gitignore.write_text(...)`
        # truncates the file before writing the new content, so power
        # loss / process kill mid-write can leave a half-written
        # `.gitignore` — and a stale or truncated gitignore lets the
        # next `sync push` commit event logs / lockfiles to the remote.
        atomic_write_bytes(gitignore, desired.encode("utf-8"))
        actions.append(".gitignore written")
    else:
        actions.append(".gitignore already in canonical shape")

    if remote is not None:
        # Replace any existing origin URL; we don't want stale remotes
        # silently overriding the user's most recent intent.
        existing = _run_git(root, ["remote"], check=False).stdout.split()
        display = _redact_url(remote)
        if "origin" in existing:
            _run_git(root, ["remote", "set-url", "origin", remote])
            actions.append(f"updated origin → {display}")
        else:
            _run_git(root, ["remote", "add", "origin", remote])
            actions.append(f"added origin → {display}")

    return {
        "root": str(root),
        "already_repo": already_repo,
        "actions": actions,
    }


def status(root: Path) -> SyncStatus:
    """Snapshot of the repo state. Returns a `SyncStatus` even for
    non-repo directories — the caller can branch on `is_repo`. Never
    raises; structural problems are surfaced as `is_repo=False`
    plus empty fields."""
    root = Path(root).expanduser().resolve()
    if not _is_repo(root):
        return SyncStatus(
            is_repo=False,
            branch=None,
            has_changes=False,
            untracked=[],
            modified=[],
            remote_url=None,
            ahead=0,
            behind=0,
        )

    branch_result = _run_git(root, ["branch", "--show-current"], check=False)
    branch = branch_result.stdout.strip() or None

    porcelain = _run_git(root, ["status", "--porcelain"], check=False).stdout
    untracked: list[str] = []
    modified: list[str] = []
    for line in porcelain.splitlines():
        # Porcelain v1 format is fixed-width: chars 0-1 are the XY status
        # code, char 2 is a separator space, the rest is the path. Splitting
        # on the first space is wrong for codes like " M" (modified, not
        # staged) where the first char is itself a space — that drops the
        # leading status char into the path. Slice by position instead.
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        if not path:
            continue
        if code == "??":
            untracked.append(path)
        else:
            modified.append(path)

    remote_url = (
        _run_git(root, ["remote", "get-url", "origin"], check=False).stdout.strip()
        or None
    )
    # SyncStatus surfaces in CLI output and `--json` payloads — strip
    # embedded credentials so a piped status doesn't leak the token.
    remote_url = _redact_url(remote_url)

    ahead = 0
    behind = 0
    if remote_url and branch:
        # `@{u}` is the upstream tracking ref. Counts how many commits
        # are on each side of the ancestor — same shape git's status
        # uses. The check=False is important: a fresh repo with no
        # upstream tracking returns 128, which we want to read as
        # "no info" rather than raise.
        rev_list = _run_git(
            root,
            ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
            check=False,
        )
        if rev_list.returncode == 0:
            parts = rev_list.stdout.split()
            if len(parts) == 2:
                ahead = int(parts[0])
                behind = int(parts[1])

    return SyncStatus(
        is_repo=True,
        branch=branch,
        has_changes=bool(porcelain.strip()),
        untracked=untracked,
        modified=modified,
        remote_url=remote_url,
        ahead=ahead,
        behind=behind,
    )


def push(
    root: Path,
    *,
    remote: str = "origin",
    message: str = DEFAULT_COMMIT_MESSAGE,
) -> dict[str, object]:
    """Stage everything, commit if there are changes, push.

    Returns a small dict so the CLI can render either text or JSON
    cleanly. A push with no local changes is a no-op (returns
    ``committed=False, pushed=False``) — git is happy to re-push an
    empty commit, but for a sync wrapper the right default is
    "say nothing and skip".
    """
    _require_git_name("remote", remote)
    root = Path(root).expanduser().resolve()
    if not _is_repo(root):
        raise SyncError(
            f"{root} is not a git repo. Run `bettermemory sync init` first."
        )

    # Serialize concurrent `bettermemory sync` operations. Two `sync
    # push` runs, or a `push` racing a `pull`, would otherwise
    # interleave their `git add` / `commit` / `push` — the lock makes
    # each sync op an atomic boundary against the other.
    #
    # This does NOT coordinate against the in-process `Store`.
    # `Store.write` holds a per-memory-file lock (`<id>.md.lock`) — a
    # different inode from this `.sync.lock`, so `flock` does not
    # serialize the two. A `Store.write` landing mid-`git add -A` can
    # still stage a half-written file-set; that snapshot is at worst
    # one commit stale and the next sync corrects it. True sync↔Store
    # coordination would require `Store`'s mutators to take this same
    # lock — a global write-serialization tradeoff left as a
    # deliberate, separate decision. On Windows `flock_excl` is a
    # no-op (MVP single-process there).
    with flock_excl(root / _SYNC_LOCK_NAME):
        _run_git(root, ["add", "-A"])
        diff = _run_git(root, ["diff", "--cached", "--quiet"], check=False)
        has_staged = diff.returncode != 0
        committed = False
        if has_staged:
            _run_git(root, ["commit", "-m", message])
            committed = True

        # Even when nothing was committed this turn, prior commits may
        # not yet be on the remote. So push unconditionally — but only
        # when a remote of the requested name exists. Otherwise raise a
        # clear error.
        remotes = _run_git(root, ["remote"], check=False).stdout.split()
        if remote not in remotes:
            raise SyncError(
                f"no remote named {remote!r}. Run "
                f"`bettermemory sync init --remote <url>` or add it manually."
            )

        # `--set-upstream` is harmless on subsequent pushes once tracking
        # exists and avoids the "your branch has no upstream" foot-gun on
        # the first push. The downstream `pull --rebase` needs the
        # tracking branch to know what to rebase onto.
        push_result = _run_git(
            root, ["push", "--set-upstream", remote, "HEAD"], check=False
        )
        if push_result.returncode != 0:
            # Same redaction discipline as `_run_git`'s default error path:
            # `git push` failures often echo the full remote URL, which
            # for HTTPS auth includes the token. The push/pull paths build
            # their own SyncError (because they want to attach the
            # "rebase --continue" hint for the conflict case), so the
            # redaction wrapper has to be applied here too — otherwise
            # this branch is the one outlet that leaks credentials.
            stderr = _redact_text(push_result.stderr.strip())
            stdout = _redact_text(push_result.stdout.strip())
            raise SyncError(
                f"`git push --set-upstream {remote} HEAD` failed: {stderr or stdout}"
            )

    return {
        "root": str(root),
        "committed": committed,
        "pushed": True,
        "remote": remote,
    }


def pull(
    root: Path,
    *,
    remote: str = "origin",
    reindex: bool = True,
) -> dict[str, object]:
    """Rebase-pull from the remote, then rebuild the FTS5 index
    (which the Store hooks bypassed during the file-level merge).

    Set `reindex=False` to skip the post-pull rebuild — useful in
    scripts that batch multiple sync operations and want to defer
    the index rebuild to the end.
    """
    _require_git_name("remote", remote)
    root = Path(root).expanduser().resolve()
    if not _is_repo(root):
        raise SyncError(
            f"{root} is not a git repo. Run `bettermemory sync init` first."
        )

    # Serialize concurrent `bettermemory sync` operations — see the
    # note in `push()`. This lock makes `pull` atomic against another
    # `pull` / `push`; it does NOT coordinate against the in-process
    # `Store` (a different lockfile inode), so `git pull --rebase`
    # can still `checkout` over a file a racing `Store.write` holds
    # open — a known gap pending Store-side coordination. On crash
    # mid-rebase the repo is left in `.git/rebase-merge/` — operator
    # runs `git rebase --abort` from the memory directory to recover.
    # The lock is held across both the pull AND the reindex so the
    # FTS5 rebuild sees the same on-disk set the rebase landed.
    with flock_excl(root / _SYNC_LOCK_NAME):
        remotes = _run_git(root, ["remote"], check=False).stdout.split()
        if remote not in remotes:
            raise SyncError(
                f"no remote named {remote!r}. Run "
                f"`bettermemory sync init --remote <url>` or add it manually."
            )

        # `--no-tags` keeps a hostile (or sloppy) remote from injecting refs
        # under `refs/tags/` that shadow branch names, and keeps the local
        # `.git/refs/tags/` clean — the memory store has no concept of tags,
        # so anything that lands there is at best clutter and at worst a
        # foot-gun ("git checkout main" picking up the wrong ref).
        pull_result = _run_git(
            root, ["pull", "--rebase", "--no-tags", remote], check=False
        )
        if pull_result.returncode != 0:
            # See the redaction note on the push branch — credentialed
            # URLs can land in pull stderr too (e.g. when the remote is
            # unreachable git echoes the URL with the auth segment).
            stderr = _redact_text(pull_result.stderr.strip())
            stdout = _redact_text(pull_result.stdout.strip())
            raise SyncError(
                f"`git pull --rebase {remote}` failed: "
                f"{stderr or stdout}\n"
                "If the failure is a merge conflict, resolve it by hand and "
                "run `git rebase --continue` from the memory directory. "
                "On crash mid-rebase, `git rebase --abort` recovers the "
                "pre-pull state."
            )

        indexed: int | None = None
        if reindex:
            # Lazy import — same pattern the Store hooks use. Avoids
            # paying the SQLite import cost on sync runs that pass
            # --no-reindex.
            from . import index as _index
            from .store import Store

            store = Store(root)
            indexed = _index.rebuild(root, store.iter_active())

    return {
        "root": str(root),
        "remote": remote,
        "pulled": True,
        "reindexed": reindex,
        "indexed_count": indexed,
    }


def auto(root: Path, *, remote: str = "origin") -> dict[str, object]:
    """Pull-rebase, then push. The shell-alias / cron one-shot for
    "sync everything". Returns the combined status of both
    operations."""
    pull_result = pull(root, remote=remote)
    push_result = push(root, remote=remote)
    return {
        "root": str(root),
        "remote": remote,
        "pull": pull_result,
        "push": push_result,
    }


__all__ = [
    "DEFAULT_COMMIT_MESSAGE",
    "SyncError",
    "SyncStatus",
    "auto",
    "init",
    "pull",
    "push",
    "status",
]
