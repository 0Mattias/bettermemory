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
  hooks, so the index is stale until rebuilt). Refuses, naming the
  files, when a tracked memory has uncommitted edits: ``git pull
  --rebase`` cannot run against a dirty worktree.
- ``bettermemory sync auto`` — convenience: commit local edits,
  pull-rebase, then push. The "sync me" one-shot for cron / shell
  aliases. The commit comes FIRST precisely because the store is
  normally dirty when a user reaches for this command; ``auto``'s
  push step committed everything anyway, so only the order changed.

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
from .events import ARCHIVE_PREFIX, EVENT_LOG_FILENAME
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
    # Rotated event-log archives AND crashed-rotation `.rotating` holding
    # files — every `.events-*` sibling. Both carry the same session ids
    # and (verbatim mode) raw query text as the active log, so both are
    # host-local. The prior pattern `.events.jsonl.*.gz` matched NOTHING
    # real (archives are `.events-{ts}.jsonl.gz`, dash not dot), so
    # rotated archives were silently pushed to every clone — the
    # structural sidecar guard missed it because the archive name is
    # composed from ARCHIVE_PREFIX at runtime, not a `*_FILENAME`
    # constant. `.events-*` covers both suffixes.
    f"{ARCHIVE_PREFIX}*",
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


# Header for lines APPENDED to an existing `.gitignore` by
# `_reconcile_gitignore` (as opposed to the canonical block a fresh store
# gets). Marks the appended block as ours so a user reading their own
# gitignore can tell which lines they wrote and which the upgrade added.
_GITIGNORE_UPGRADE_HEADER = (
    "# bettermemory: added on sync — newly-excluded regenerable / transient"
)


def _missing_gitignore_patterns(current: str) -> list[str]:
    """Which `_GITIGNORE_LINES` patterns are absent from `current`.

    Comment lines are not patterns, so they are never "missing" — only
    the real ignore rules have to be present for the store to be safe.
    Existing lines are compared stripped, so a hand-edited file with
    trailing whitespace or indentation still counts as covering a
    pattern (git strips trailing whitespace too) and we don't append a
    near-duplicate of a line the user already has.
    """
    existing = {line.strip() for line in current.splitlines()}
    return [
        line
        for line in _GITIGNORE_LINES
        if line.strip()
        and not line.lstrip().startswith("#")
        and line.strip() not in existing
    ]


@dataclass(frozen=True)
class _GitignoreReconcile:
    """Explicit outcome of `_reconcile_gitignore`.

    The helper used to return a bare `list[str]` of added patterns, which
    collapsed two materially different results into the same value:
    "every pattern was already present" and "the file could not be
    read/written, so nothing was reconciled" both came back as `[]`. The
    caller could not tell them apart, and `init()` mapped both onto the
    action line ".gitignore already in canonical shape" — reporting an
    UNREADABLE gitignore to the user as correct. Making the failure a
    field rather than an indistinguishable empty list is what lets every
    caller report honestly.

    `error` is a human-readable reason the reconcile stood down (`None`
    on success). When it is set, `added` is always empty: a stand-down
    changes nothing on disk.

    `failed_stage` names WHICH half stood down — `"read"` or `"write"`,
    and `None` on success. The two are not interchangeable to every
    caller, so collapsing them onto `error` alone loses information a
    caller legitimately needs:

    * A READ stand-down means we never learned what the file contains,
      so we cannot enumerate what an overwrite would destroy. Declining
      is the CORRECT outcome, not a failure to do the job.
    * A WRITE stand-down means we knew exactly what to append and could
      not. The job was attempted and did not happen.

    `push`/`init` treat both the same (never fail the sync for a healing
    side-effect), which is why the distinction lives here rather than in
    two return types. `doctor --fix` does not: it reports an honest
    not-applied FixResult for the write case and stays silent for the
    read case, pinned by
    `test_fix_sync_gitignore_reports_write_failure` and
    `test_fix_sync_gitignore_leaves_an_unreadable_gitignore_alone`
    respectively.
    """

    added: list[str]
    error: str | None = None
    failed_stage: str | None = None


def _write_gitignore_or_stand_down(
    gitignore: Path, payload: bytes, missing: list[str]
) -> _GitignoreReconcile:
    """Write `payload` to `gitignore`, standing down on OSError.

    THE WRITE HALF OF THE STAND-DOWN POLICY. The read in
    `_reconcile_gitignore` has always been guarded; both writes were not,
    and that asymmetry was accidental rather than decided. It mattered
    because the reconcile moved onto the `push` path: an unwritable
    `.gitignore` (read-only file, read-only mount, a directory at the
    path, ENOSPC) turned a push that previously succeeded into a hard
    `OSError` — the user's memories stop reaching their remote entirely.

    The policy is now the same on both halves: NEVER raise, always
    report. Rationale, deliberately chosen rather than inherited:

    * The reconcile is a healing side-effect on the push path, not the
      push's purpose. Failing the push trades "some sidecar patterns are
      not enforced yet" for "no memories sync at all", which is the worse
      of the two outcomes.
    * It matches what `test_push_leaves_an_unreadable_gitignore_alone`
      already pins for the read half, so read and write now agree.
    * The stand-down is LOUD, not silent: the reason names the file, the
      errno, and every pattern left unenforced, and it is returned to the
      caller (surfaced by `init` in its action list and `gitignore_error`
      field, logged at WARNING by `push`).
    * `doctor`'s `sync_tracked_ignored` check remains the backstop that
      reports a store whose gitignore never caught up.
    """
    try:
        atomic_write_bytes(gitignore, payload)
    except OSError as exc:
        reason = (
            f"could not write {gitignore} ({exc.__class__.__name__}: {exc}); "
            f"{len(missing)} regenerable/transient ignore rule(s) remain "
            f"unenforced: {', '.join(missing)}"
        )
        log.warning("%s", reason)
        return _GitignoreReconcile(added=[], error=reason, failed_stage="write")
    return _GitignoreReconcile(added=missing)


def _reconcile_gitignore(root: Path) -> _GitignoreReconcile:
    """Make the store's on-disk `.gitignore` cover every pattern in
    `_GITIGNORE_LINES`, and report which lines had to be added.

    THE UPGRADE PATH. `_GITIGNORE_LINES` grows over releases (the
    sharded event segments `.events.*.jsonl` joined it in 3.24.0), but
    for years the only writer of a store's `.gitignore` was `init` — so
    every store initialised before a line was added kept its old file
    and the newly-excluded sidecar stayed UNIGNORED. `sync push` runs
    `git add -A`, so those stores committed and pushed the sidecar to
    their remote: raw event telemetry (search queries, session ids,
    memory ids) landing permanently in the user's git history. Six
    sidecar leaks in this class have now been closed by adding a line to
    `_GITIGNORE_LINES`; this reconcile is what makes the SEVENTH line
    actually reach the stores that already exist.

    Append-only, never a wholesale rewrite: a `.gitignore` in a memory
    store is a file users legitimately edit (their own machine-local
    exclusions live there too), and rewriting it to the canonical block
    would silently delete their lines. So we only ever add what is
    missing, which also makes the operation idempotent — a second sync
    finds nothing missing and does not touch the file, so repeated syncs
    cannot duplicate a line or churn the diff.

    Ordering is not load-bearing here and appending is therefore safe:
    `test_gitignore_lines_are_positive_and_slash_free` fences
    `_GITIGNORE_LINES` to positive, slash-free patterns, and for those
    git's semantics are order-independent (order only matters when a
    later `!` negation has to re-include what an earlier line excluded).

    An empty / whitespace-only / absent file is written as the canonical
    block instead, header comment included — that is the fresh-store
    shape `init` has always produced, and there is no user content to
    preserve. An UNREADABLE file is left alone with a warning: we cannot
    know what is in it, and clobbering a user's gitignore is a worse
    outcome than the caller's next `git add -A` behaving as it does
    today (`doctor`'s `sync_tracked_ignored` check still reports it). An
    UNWRITABLE file stands down the same way — see
    `_write_gitignore_or_stand_down` for why read and write share one
    policy rather than the accidental asymmetry they used to have.

    Returns a `_GitignoreReconcile`, NOT a bare list: "nothing was
    missing" and "the reconcile could not run" are different outcomes and
    the caller has to be able to tell them apart to report honestly.
    """
    gitignore = root / ".gitignore"
    try:
        current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError as exc:
        reason = (
            f"could not read {gitignore} ({exc.__class__.__name__}: {exc}) — "
            f"leaving it untouched; regenerable sidecars may not be excluded "
            f"from this sync"
        )
        log.warning("%s", reason)
        return _GitignoreReconcile(added=[], error=reason, failed_stage="read")

    if not current.strip():
        # No usable gitignore (absent, empty, whitespace-only). Nothing
        # to preserve, so write the canonical block verbatim — comments
        # and all. This is the shape a fresh `init` has always left.
        desired = "\n".join(_GITIGNORE_LINES) + "\n"
        return _write_gitignore_or_stand_down(
            gitignore,
            desired.encode("utf-8"),
            _missing_gitignore_patterns(current),
        )

    missing = _missing_gitignore_patterns(current)
    if not missing:
        return _GitignoreReconcile(added=[])
    # Preserve the file byte-for-byte and append. A file whose last line
    # has no trailing newline would otherwise get our first pattern glued
    # onto it, silently corrupting BOTH rules.
    prefix = current if current.endswith("\n") else current + "\n"
    block = "\n".join([_GITIGNORE_UPGRADE_HEADER, *missing]) + "\n"
    return _write_gitignore_or_stand_down(
        gitignore, (prefix + block).encode("utf-8"), missing
    )


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


# The unmerged `git status --porcelain` v1 codes, as listed in
# git-status(1)'s "Short Format" section. This IS still an enumeration —
# a code git introduced later would not be matched — but it enumerates
# the RIGHT axis. There are exactly seven ways git spells "unresolved
# conflict" and the list is documented and stable, whereas the ways to
# ARRIVE at one are open-ended: rebase, merge, cherry-pick, revert and a
# conflicted `git stash pop` all produce codes from this set, and the
# stash-pop case leaves no sentinel file at all, so an operation-shaped
# guard could never have covered it however many operations it listed.
_UNMERGED_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _porcelain_entries(porcelain: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain` (v1) into (XY code, path) pairs.

    Porcelain v1 is fixed-width: chars 0-1 are the XY status code, char 2
    is a separator space, the rest is the path. Splitting on the first
    space is wrong for codes like `" M"` (modified, not staged) where the
    first char is itself a space — that drops the leading status char
    into the path. Slice by position instead.

    THE ONE PARSER. `status()`, `_dirty_tracked_paths()` and
    `_unmerged_paths()` all go through here so they cannot drift: the
    conflict guard, `pull`'s dirty-worktree pre-check and the `sync
    status` the user runs to diagnose either one must agree on what a
    given line means, or the error message names files `status` does not
    show.
    """
    entries: list[tuple[str, str]] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        if not path:
            continue
        entries.append((code, path))
    return entries


def _parse_porcelain(porcelain: str) -> tuple[list[str], list[str]]:
    """Split porcelain output into (untracked, modified)."""
    untracked: list[str] = []
    modified: list[str] = []
    for code, path in _porcelain_entries(porcelain):
        if code == "??":
            untracked.append(path)
        else:
            modified.append(path)
    return untracked, modified


def _dirty_tracked_paths(root: Path) -> list[str]:
    """Tracked paths carrying staged or unstaged changes.

    This is exactly the set that makes `git pull --rebase` refuse to run
    ("cannot pull with rebase: You have unstaged changes"). UNTRACKED
    files are deliberately excluded — a rebase is happy to run alongside
    them, so listing them in the error would name files that are not the
    problem.
    """
    porcelain = _run_git(root, ["status", "--porcelain"], check=False).stdout
    _untracked, modified = _parse_porcelain(porcelain)
    return modified


def _unmerged_paths(root: Path) -> list[str]:
    """Tracked paths with an unresolved merge conflict.

    THE CONFLICT PREDICATE, and it asks about STATE rather than CAUSE.
    An earlier version of this guard probed for `rebase-merge` /
    `rebase-apply` sentinel directories, which answers "is a rebase
    unfinished" — a strictly narrower question than "does the worktree
    hold conflict markers". A repo left mid-merge (`MERGE_HEAD`),
    mid-cherry-pick (`CHERRY_PICK_HEAD`) or mid-revert (`REVERT_HEAD`)
    produces IDENTICAL `UU` porcelain entries and slipped straight
    through; a conflicted `git stash pop` leaves conflicts with NO
    sentinel file at all, so no amount of probing would ever have caught
    it. Verified empirically: on a two-clone conflicted `git merge`,
    `_rebase_in_progress` returns False while porcelain reports `UU`.

    Why it matters: everything that stages runs `git add -A`, which
    marks conflicts RESOLVED without resolving them and commits
    `<<<<<<<` into the user's memories — permanently, and onward to
    every clone on the next push.
    """
    porcelain = _run_git(root, ["status", "--porcelain"], check=False).stdout
    return [
        path for code, path in _porcelain_entries(porcelain) if code in _UNMERGED_CODES
    ]


def _rebase_in_progress(root: Path) -> bool:
    """True while an interrupted `git rebase` is still unfinished.

    NOT the conflict guard — `_unmerged_paths` is. This narrower probe
    survives for two jobs the porcelain codes cannot do: tailoring the
    error message to say `git rebase --continue` rather than `git
    commit`, and catching a rebase that is stopped with NOTHING unmerged
    (an interactive `edit` stop, or conflicts the user already staged but
    has not continued past) — a state with no `U` codes where a commit
    would still land somewhere the user does not expect.

    `git rev-parse --git-path` rather than `root / ".git" / …` because
    `.git` is a FILE, not a directory, in a linked worktree or a
    submodule — probing the literal path would report "no rebase" for
    every such store.
    """
    for name in ("rebase-merge", "rebase-apply"):
        result = _run_git(root, ["rev-parse", "--git-path", name], check=False)
        if result.returncode != 0:
            continue
        candidate = Path(result.stdout.strip())
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.exists():
            return True
    return False


def _unresolved_conflict_error(
    root: Path, unmerged: list[str], rebasing: bool
) -> SyncError:
    """The one message for "resolve your conflict first".

    Deliberately tells the user NOT to reach for `sync push` — and never
    RECOMMENDS it, which the dirty-worktree branch used to do on exactly
    this state. `push` runs `git add -A`, which would stage the conflict
    markers as if they were resolved content and commit
    `<<<<<<<`/`>>>>>>>` into their memories.

    The "finish it" half is tailored by `rebasing` because the commands
    genuinely differ: a stopped rebase wants `git rebase --continue`,
    while a merge / cherry-pick / revert / stash-pop conflict wants a
    plain `git commit`. Getting this wrong is not cosmetic — `git rebase
    --continue` in a repo with no rebase in progress just errors.
    """
    if rebasing:
        what = f"an unfinished rebase is in progress in {root}"
        finish = (
            "`git rebase --continue` from the memory directory; or "
            "`git rebase --abort` to recover the pre-pull state"
        )
    else:
        what = f"{root} has an unfinished merge, cherry-pick, revert or stash pop"
        finish = (
            "`git commit` to conclude it; or `git merge --abort` "
            "(`git cherry-pick --abort` / `git revert --abort`) to back out"
        )

    if unmerged:
        shown = ", ".join(unmerged[:10])
        if len(unmerged) > 10:
            shown += f", and {len(unmerged) - 10} more"
        conflicted = (
            f" {len(unmerged)} file(s) still have unresolved merge conflicts: {shown}."
        )
    else:
        conflicted = ""

    return SyncError(
        f"{what}.{conflicted} Resolve the conflicted file(s) by hand "
        f"(remove the `<<<<<<<` / `=======` / `>>>>>>>` markers), "
        f"`git add` them, then finish with {finish}. Do NOT run "
        f"`bettermemory sync push` or `bettermemory sync auto` first: they "
        f"run `git add -A`, which would mark the conflicts resolved without "
        f"resolving them and commit the markers into your memories."
    )


def _require_no_unresolved_conflict(root: Path) -> None:
    """Raise unless `root` is free of conflicts and half-finished rebases.

    Called by every path that is about to `git add -A` or `git pull
    --rebase`. Gates on `_unmerged_paths` FIRST (state, complete) and
    keeps `_rebase_in_progress` as a second condition only to catch the
    rebase-stopped-with-nothing-unmerged case that carries no `U` codes.
    """
    unmerged = _unmerged_paths(root)
    rebasing = _rebase_in_progress(root)
    if unmerged or rebasing:
        raise _unresolved_conflict_error(root, unmerged, rebasing)


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
    # `_reconcile_gitignore` is append-only and routes through
    # `atomic_write_bytes`: a plain `gitignore.write_text(...)` truncates
    # the file before writing the new content, so power loss / process
    # kill mid-write can leave a half-written `.gitignore` — and a stale
    # or truncated gitignore lets the next `sync push` commit event logs
    # / lockfiles to the remote. init is no longer the ONLY caller: every
    # `push` reconciles too, which is what carries a newly-added pattern
    # to stores that were initialised before it existed.
    # THREE outcomes, not two. `_reconcile_gitignore` used to return a
    # bare list, so "nothing was missing" and "the file could not be
    # read" were the same `[]` — and this branch reported an UNREADABLE
    # gitignore to the user as ".gitignore already in canonical shape",
    # i.e. told them the store was safe when we had no idea what was in
    # the file. The outcome is explicit now, so the failure is stated.
    reconcile = _reconcile_gitignore(root)
    if reconcile.error is not None:
        actions.append(f".gitignore NOT reconciled — {reconcile.error}")
    elif reconcile.added:
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
        # Machine-readable twin of the action line above: `None` when the
        # gitignore is in canonical shape, otherwise the reason the
        # reconcile stood down. `--json` consumers (and the tests) need
        # the failure as a field, not buried in prose.
        "gitignore_error": reconcile.error,
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
    untracked, modified = _parse_porcelain(porcelain)

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


def _stage_and_commit(root: Path, message: str) -> bool:
    """Reconcile the gitignore, `git add -A`, commit iff anything staged.

    Returns True iff a commit was created. THE CALLER MUST ALREADY HOLD
    the sync lock — this helper deliberately does not take it, because
    `flock` identity is per open-file-description: a nested
    `flock_excl` on the same `.sync.lock` from the same process opens a
    second descriptor and would DEADLOCK against the outer hold.

    Extracted from `push` so `auto` can commit local edits BEFORE it
    pulls (see `auto`) without duplicating the reconcile + stage +
    commit discipline or reordering it by accident.
    """
    # Reconcile the on-disk `.gitignore` with `_GITIGNORE_LINES` BEFORE
    # `git add -A` reads the tree, so a pattern added in a release AFTER
    # this store was initialised takes effect on this very commit rather
    # than never (see `_reconcile_gitignore`). Staging is the only place
    # a stale gitignore can leak a sidecar, so it is the only place that
    # has to reconcile.
    #
    # `pull` deliberately does NOT reconcile: `git pull --rebase` refuses
    # to run against a dirty worktree ("cannot pull with rebase: You have
    # unstaged changes", verified empirically), so writing an
    # uncommitted `.gitignore` change from inside pull would break the
    # NEXT pull of a pull-only clone — while fixing no leak, because pull
    # stages nothing.
    reconcile = _reconcile_gitignore(root)
    if reconcile.error is not None:
        # Stand down, do NOT abort: an unwritable gitignore must not take
        # down a push that would otherwise succeed. See
        # `_write_gitignore_or_stand_down` for the full policy.
        log.warning(
            "%s: .gitignore reconcile stood down before staging — %s",
            root,
            reconcile.error,
        )
    elif reconcile.added:
        log.info(
            "%s: added %d missing ignore rule(s) before staging: %s",
            root / ".gitignore",
            len(reconcile.added),
            ", ".join(reconcile.added),
        )
    _run_git(root, ["add", "-A"])
    diff = _run_git(root, ["diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        return False
    _run_git(root, ["commit", "-m", message])
    return True


def _commit_local_changes(root: Path, message: str) -> bool:
    """`_stage_and_commit` under its own sync-lock hold.

    Used by `auto` to clear the worktree before it pulls. Takes and
    releases the lock rather than holding it across the whole `auto`
    run, matching how `auto` has always treated its sub-operations: each
    git op is its own atomic boundary, and nesting the same lock in one
    process would deadlock (see `_stage_and_commit`).

    Refuses while ANY conflict is unresolved. `auto` commits BEFORE it
    pulls, which newly puts a `git add -A` ahead of the pull's own guard:
    on a repo holding conflict markers, that would stage them as resolved
    content and commit `<<<<<<<` into the user's memories. The guard sits
    here because that is where the commit-first reordering made the path
    reachable.

    It gates on `_require_no_unresolved_conflict`, NOT on
    `_rebase_in_progress`. The narrower probe missed every non-rebase way
    to hold a conflict — a plain `git merge`, a cherry-pick, a revert, a
    conflicted `git stash pop` — each of which produces the same `UU`
    porcelain entry. Demonstrated with a two-clone conflicted merge: the
    rebase-only guard let `auto` commit `<<<<<<< HEAD` onto the tip of
    `main`, where the next push would ship it to every clone.

    `push` carries the same guard, added after this one. Both callers of
    `_stage_and_commit` now check before they reach it, so no route to the
    single `git add -A` is unguarded. `auto` runs commit -> pull -> push
    and each step gates independently, but the first to see a conflict
    raises and aborts the run, so the user gets one message rather than
    three: on a conflicted store `auto` stops here and never reaches
    `pull` or `push` at all.
    """
    _require_no_unresolved_conflict(root)
    with flock_excl(root / _SYNC_LOCK_NAME):
        return _stage_and_commit(root, message)


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
    # deliberate, separate decision.
    #
    # Windows IS serialised here, contrary to what this comment claimed
    # until now. `_fsutil.flock_excl` has routed win32 to
    # `_flock_windows` since 2.7: that helper takes a REAL cross-process
    # advisory lock on the same `.sync.lock` sidecar via
    # `msvcrt.locking(fd, LK_NBLCK, 1)`, retrying with capped
    # exponential backoff (5ms doubling to a 100ms ceiling) until
    # `BETTERMEMORY_FLOCK_TIMEOUT` (default 30s) and then raising
    # `TimeoutError` rather than proceeding unlocked. So two `sync push`
    # processes on Windows serialise exactly as they do on POSIX. The
    # only degraded path is the fallback when `msvcrt` cannot be
    # imported or the lockfile cannot be opened at all, and that falls
    # back to an in-process-only yield with a one-shot `logger.warning`
    # — visible in operator logs, never silent.
    with flock_excl(root / _SYNC_LOCK_NAME):
        # UNRESOLVED CONFLICTS FIRST, before the `git add -A` inside
        # `_stage_and_commit` — the only `git add -A` in this package.
        # `push` was the caller that reached it without asking: on a repo
        # holding unmerged files it staged the `<<<<<<<` markers as if
        # they were resolved content, committed them, and then SHIPPED
        # them to the remote. Verified empirically on a two-clone
        # conflicted merge against the pre-guard commit — `push` returned
        # `committed=True, pushed=True` and left `<<<<<<< HEAD` in a
        # memory body at the tip of `main` on both the clone and the bare
        # remote.
        #
        # That distributing half is why this matters more here than on the
        # local-only paths: every clone that pulls afterwards gets memory
        # bodies full of markers, and undoing that reaches past the user's
        # own repo into every copy that already fetched it.
        #
        # INSIDE the lock, not before it. `push` may block here for as
        # long as `BETTERMEMORY_FLOCK_TIMEOUT` allows (default 30s), so a
        # check taken before the acquire describes a worktree that another
        # process — or the user's own hand-run `git merge` — has had that
        # whole window to conflict. Checking after the acquire makes
        # guard-then-stage one atomic boundary, which is also where `pull`
        # puts its copy.
        _require_no_unresolved_conflict(root)

        committed = _stage_and_commit(root, message)

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

    Raises `SyncError` NAMING THE FILES when the worktree has
    uncommitted changes to tracked memories. `git pull --rebase` refuses
    to run in that state, and a live store is dirty most of the time —
    editing a memory then syncing is the normal case, not the exotic
    one. The raw git failure ("cannot pull with rebase: You have
    unstaged changes") arrived wrapped in this wrapper's
    conflict-resolution hint, which told the user to run `git rebase
    --continue` for a situation where no rebase had started: advice that
    does nothing. The pre-check turns that into an error that says which
    files are dirty and which command fixes it.

    `pull` deliberately does NOT commit them for you — pull is a
    read-ward operation and silently committing a user's in-progress
    edits would be a surprising side effect. `auto` DOES commit first
    (it is going to commit everything in its push step anyway), so the
    "sync me" one-shot works on a dirty store.
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

        # UNRESOLVED CONFLICTS FIRST: their `UU` entries are indistinguishable
        # from ordinary dirty files in porcelain output, but the remedy is
        # the opposite one. Without this ordering the dirty-worktree branch
        # below would tell the user to run `sync push`, which would
        # `git add -A` the conflict markers straight into their memories.
        _require_no_unresolved_conflict(root)

        # Dirty-worktree pre-check. `git pull --rebase` hard-refuses when a
        # tracked file has uncommitted changes, and that is the NORMAL state
        # of a live store: edit a memory, run sync. Checked here — after the
        # remote check, which is the more fundamental misconfiguration —
        # so the user gets the files by name instead of git's generic
        # complaint plus an inapplicable `git rebase --continue` hint.
        dirty = _dirty_tracked_paths(root)
        if dirty:
            shown = ", ".join(dirty[:10])
            if len(dirty) > 10:
                shown += f", and {len(dirty) - 10} more"
            raise SyncError(
                f"{len(dirty)} tracked file(s) in {root} have uncommitted "
                f"changes, and `git pull --rebase` refuses to run against a "
                f"dirty worktree: {shown}. Run `bettermemory sync push` to "
                f"commit and send them first, or `bettermemory sync auto` "
                f"(which commits before it pulls), or commit / revert them "
                f"by hand."
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
    """Commit local edits, pull-rebase, then push. The shell-alias /
    cron one-shot for "sync everything". Returns the combined status of
    all three steps.

    THE COMMIT COMES FIRST, and that ordering is the fix for a bug that
    made this command unusable on a live store. `auto` used to pull
    before it committed anything, and `git pull --rebase` hard-refuses
    against a dirty worktree — so the moment a user edited a memory (the
    normal reason to run a sync at all) `auto` failed outright with
    git's "cannot pull with rebase: You have unstaged changes". Verified
    empirically: init, push, edit one existing memory, `auto` raises.

    Committing first is not a new side effect. `auto`'s push step has
    always run `git add -A` and committed everything in the worktree, so
    the same content reaches the same commit either way — the only
    change is that it happens before the rebase instead of after, which
    is the order git actually supports. The rebase then replays that
    commit on top of the remote, exactly as a hand-run
    `git commit && git pull --rebase && git push` would.

    A commit is deliberately preferred over `git stash` around the pull:
    a stash that fails to pop (conflict, crash between pull and pop)
    strands the user's edits in a place they have to know to look for,
    while a commit is durable, visible in `git log`, and is where the
    content was headed anyway.
    """
    _require_git_name("remote", remote)
    root = Path(root).expanduser().resolve()
    if not _is_repo(root):
        raise SyncError(
            f"{root} is not a git repo. Run `bettermemory sync init` first."
        )

    committed_before_pull = _commit_local_changes(root, DEFAULT_COMMIT_MESSAGE)
    pull_result = pull(root, remote=remote)
    push_result = push(root, remote=remote)
    return {
        "root": str(root),
        "remote": remote,
        "committed_before_pull": committed_before_pull,
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
