"""The one way the package runs ``git``: `run_git`.

`origin` (the capture of a memory's repository, branch and worktree) and
`location` (the history reader behind membership) each ran git their own
way: origin decoded UTF-8, logged in tiers and counted its failures but
inherited the caller's environment, so a shell with ``GIT_DIR`` exported
captured the environment's repository for an unrelated cwd; location
scrubbed the environment and pinned the configuration (design 8.8) but
logged nothing, not even a hang against its 20-second bound. Both now
call here, with all four properties:

* the environment is the caller's with ``GIT_OPTIONAL_LOCKS=0`` and
  ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_COMMON_DIR``,
  ``GIT_OBJECT_DIRECTORY`` and ``GIT_INDEX_FILE`` removed, so git reads
  the repository the call names (its `cwd`, or `git_dir`), never one the
  environment points at, and its walk tests each folder by that folder's
  own ``objects`` as membership's does (ENG-193); ``GIT_ALTERNATE_OBJECT_DIRECTORIES``
  and ``GIT_SHALLOW_FILE`` go too, for the same reason, and the variables
  that reshape a diff (`RESHAPING_ENV`: ``GIT_DIFF_OPTS``,
  ``GIT_ATTR_SOURCE`` and the external diff's); a caller's own
  variables (`run_git`'s `env`, and its `unset`, removed) are applied to
  it first, so none of them puts back a variable removed here or undoes
  a setting below;
* ``GIT_NO_LAZY_FETCH=1`` and an empty ``GIT_ALLOW_PROTOCOL`` are set, so
  no call fetches: a repository whose own config makes it a partial clone
  of an ``ext::`` remote (``protocol.ext.allow=always``) otherwise has git
  run that remote's command to fetch an object the call reads and the
  clone lacks (ENG-218). The package runs no git that fetches;
* ``core.fsmonitor``, ``core.hooksPath`` and ``log.showSignature`` are
  pinned off and ``diff.submodule`` to ``short``, so no daemon starts, no
  hook of the repository's runs, no git starts in a submodule and no
  ``gpg.program`` checks a signature on a read (the one diff reader,
  `origin.commit_patch_stream`, keeps textconv and external diff
  programs off on its own command line);
* stdout and stderr are UTF-8 with ``errors="replace"`` (git emits UTF-8,
  paths especially; a bare ``text=True`` would decode with the locale's
  encoding, cp1252 on Windows, and mojibake every non-ASCII path) or raw
  bytes for a caller that parses objects (``text=False``). The text is
  decoded from the bytes as git wrote them, every carriage return kept:
  git ends each line with a line feed alone, on Windows too, and a
  carriage return is content (a file's line in a patch, a path ``-z``
  lists), which universal newlines turned into a line break, so a
  file's ``\\r`` followed by a record mark or a ``---`` line read as that
  mark or that header (ENG-220's review). A caller splits on ``\\n``;
* a call git could not run at all (no binary on PATH, the timeout, an
  OSError from the spawn) comes back None with a WARNING, and is
  counted (`unanswered_calls` for the process, `failed_calls` for the
  calling thread, and `timed_out_calls` for a timeout); a non-zero exit
  is an ANSWER ("not a repository", "not an ancestor") and comes back as
  the completed process for the caller to read, `log_nonzero_exit`
  noting it at DEBUG.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Collection, Literal, Mapping, Sequence, overload

log = logging.getLogger("bettermemory._git")

#: Environment variables that would point git at another repository than
#: the call names; removed from every call's environment. Under
#: ``GIT_OBJECT_DIRECTORY`` git reads objects from the folder it names, and
#: its walk tests that folder in place of each candidate's ``objects``
#: (``is_git_directory`` in setup.c), so it stopped at a ``.git`` holding a
#: HEAD and refs alone, which membership, reading each repository by its
#: own files, and the origin's walk with it (`gitfile_rule._git_reads`)
#: pass over (ENG-193).
SCRUBBED_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SHALLOW_FILE",
    "GIT_INDEX_FILE",
)
#: Environment variables that reshape the diff a log prints; removed from
#: every call's environment, as the repository's own settings are pinned
#: (`origin_history`, ENG-220's review). ``GIT_DIFF_OPTS`` sets the
#: context lines over the command line's ``-U0``, which the patch parser
#: reads as a mismatch, so a hit fell back to the per-file count;
#: ``GIT_ATTR_SOURCE`` (git 2.40 and later) reads the attributes from a
#: tree no stamp covers; ``GIT_EXTERNAL_DIFF`` names a diff program, and
#: ``GIT_DIFF_PATH_COUNTER`` and ``GIT_DIFF_PATH_TOTAL`` are what git
#: hands one.
RESHAPING_ENV = (
    "GIT_DIFF_OPTS",
    "GIT_ATTR_SOURCE",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_PATH_COUNTER",
    "GIT_DIFF_PATH_TOTAL",
)
#: Set on every call so that no git the package runs fetches. A partial
#: clone fetches an object a read needs and the clone lacks from its
#: promisor remote, and the remote and the protocol's permission
#: (``protocol.ext.allow=always``, which runs the remote as a command) come
#: from the repository's own config, which a ``-c`` pin does not outrank
#: there (git 2.54); so do ``core.sshCommand`` for an ``ssh://`` remote
#: and ``remote.<name>.uploadpack`` for a local one. ``GIT_NO_LAZY_FETCH``
#: (git 2.44 and later) stops the fetch; ``GIT_ALLOW_PROTOCOL`` set empty
#: allows no transport, whatever any config says, on every git since
#: 2.6.1, earlier than partial clones (2.17) (ENG-218).
NO_FETCH = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
#: Configuration pinned on every call: no fsmonitor daemon, no hooks, no
#: signature check (``log.showSignature`` runs ``gpg.program`` for every
#: signed commit a log lists and writes its output among the log's lines),
#: and a gitlink's change in a patch kept to git's default ``-/+Subproject
#: commit`` lines:
#: ``diff.submodule=diff``, from the repository's config or the user's,
#: starts a git in the submodule for each one, and that git honours the
#: submodule's own ``diff.external``, which a planted repository names
#: (an absorbed submodule's git directory sits under ``.git/modules``), so
#: a ``log -p`` ran a program of its choosing (ENG-218).
PINS = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "log.showSignature=false",
    "-c",
    "diff.submodule=short",
)

#: Whether this is Windows, where no file name holds a CR (`printed_path`).
_WINDOWS = os.name == "nt"

# How many git invocations could not run at all since import, on any
# thread. Read through `unanswered_calls`.
_unanswered = 0


class _Failed(threading.local):
    """The number of git calls on the current thread that came back None
    from `run_git` or that a caller folded into a failure (`note_failed`),
    whatever the reason. Read through `failed_calls`."""

    count = 0


_FAILED = _Failed()


class _TimedOut(threading.local):
    """The number of git calls on the current thread that `run_git` ended
    at their timeout. Read through `timed_out_calls`."""

    count = 0


_TIMED_OUT = _TimedOut()


def unanswered_calls() -> int:
    """How many git invocations since import could not run at all (the
    None `run_git` returns), on every thread."""
    return _unanswered


def failed_calls() -> int:
    """How many git calls on the current thread have come back as a
    failure: git could not run, or the caller folded the answer into one
    (`note_failed`: a non-zero exit, an empty output it needed).

    The drift readers fold each such failure into a fallback (the
    unfiltered count, the author-date basis, the could-not-ask
    applicability), and a non-zero exit cannot be told from a failure that
    clears (an object briefly unreadable, an I/O error, a lock) without
    reading git's message. The uncached code takes the fallback on the
    call that met the failure and not on the next one, so a memo reads
    this count before and after computing a value and keeps the value
    only when it did not move."""
    return _FAILED.count


def timed_out_calls() -> int:
    """How many git calls on the current thread `run_git` ended at their
    timeout; each is also counted in `failed_calls`. A memo that keeps a
    value only when no call failed can tell from the two counts that
    every failure meanwhile was a timeout, which the same call meets
    again on the same history (`_response._DriftPass`)."""
    return _TIMED_OUT.count


def note_failed() -> None:
    """Count, on the calling thread, an answer the caller folded into a
    failure."""
    _FAILED.count += 1


def note_timed_out() -> None:
    """Count, on the calling thread, a timeout a caller remembered from an
    earlier call and serves again without running git, as a failure and a
    timeout both, as `run_git` counts one."""
    _FAILED.count += 1
    _TIMED_OUT.count += 1


def printed_path(stdout: str) -> str:
    """The one path git printed on `stdout` (``rev-parse
    --show-toplevel``), with only the line end git ends it with taken off.
    git prints such a path as it is, unquoted, so a ``strip`` took off the
    space or tab a folder's name ends with (``trail ``), and a split into
    lines cut a name holding a newline (``nl\\nx``) in two; each named
    another path. `run_git` keeps every CR git writes, and git ends a line
    with an LF alone, on Windows too (Git for Windows sets its standard
    streams to binary mode as it starts, ``compat/mingw.c``): a CR before
    the LF is the name's own, which a POSIX name may end in, and is kept
    (the text read once turned that CR LF into an LF, and the name into
    another). On Windows, where no file name holds a CR, one before the LF
    comes off too, should a git there end its line with CR LF."""
    path = stdout.removesuffix("\n")
    if _WINDOWS and path != stdout:
        path = path.removesuffix("\r")
    return path


def subcommand(args: Sequence[str]) -> str:
    """The subcommand in a git argv, for log lines: skips leading
    ``-c <key>=<val>`` pairs, so a config-pinned call logs as ``git log``,
    not ``git -c``."""
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else ""


def _environment(
    extra: Mapping[str, str] | None = None, unset: Collection[str] = ()
) -> dict[str, str]:
    env = dict(os.environ)
    for name in unset:
        env.pop(name, None)
    env.update(extra or {})
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.update(NO_FETCH)
    for name in (*SCRUBBED_ENV, *RESHAPING_ENV):
        env.pop(name, None)
    return env


@overload
def run_git(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    git_dir: str | None = None,
    stdin: bytes | None = None,
    timeout: float = 1.0,
    text: Literal[True] = True,
    env: Mapping[str, str] | None = None,
    unset: Collection[str] = (),
) -> subprocess.CompletedProcess[str] | None: ...


@overload
def run_git(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    git_dir: str | None = None,
    stdin: bytes | None = None,
    timeout: float = 1.0,
    text: Literal[False],
    env: Mapping[str, str] | None = None,
    unset: Collection[str] = (),
) -> subprocess.CompletedProcess[bytes] | None: ...


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    git_dir: str | None = None,
    stdin: bytes | None = None,
    timeout: float = 1.0,
    text: bool = True,
    env: Mapping[str, str] | None = None,
    unset: Collection[str] = (),
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None:
    """Run ``git <args>`` against the repository at `cwd` or, with
    `git_dir`, the repository directory itself (``--git-dir``), under the
    scrubbed environment and the pins, within `timeout` seconds; `env`'s
    variables are set, and `unset`'s removed, beneath the scrub and the
    pins, which neither can loosen (`location_git.RepositoryHistory.independent`
    sets the grafts file to the null device, ENG-203; `gitsystem` asks git
    for its own system config without the caller's override, ENG-207). The
    completed process, exit code and output intact, for the caller to
    read; None ONLY when git could not run at all, logged at WARNING and
    counted."""
    global _unanswered
    command = ["git", *PINS]
    if git_dir is not None:
        command.append(f"--git-dir={git_dir}")
    command.extend(args)
    where = str(cwd) if cwd is not None else git_dir
    try:
        done = subprocess.run(
            command,
            cwd=None if cwd is None else str(cwd),
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_environment(env, unset),
        )
        if not text:
            return done
        # Decoded from the bytes, never in text mode: universal newlines
        # would read a carriage return in a file's content as a line break.
        return subprocess.CompletedProcess(
            done.args,
            done.returncode,
            stdout=done.stdout.decode("utf-8", "replace"),
            stderr=done.stderr.decode("utf-8", "replace"),
        )
    except FileNotFoundError as exc:
        # On POSIX the spawn raises the same error for a working directory
        # that is not there; Windows reports that as another OSError, below.
        if cwd is not None and not os.path.isdir(cwd):
            log.warning("git cannot run in %s: the directory does not exist", cwd)
        else:
            log.warning("git binary not found on PATH: %s", exc)
    except subprocess.TimeoutExpired:
        log.warning(
            "git %s timed out after %ss in %s", subcommand(args), timeout, where
        )
        _TIMED_OUT.count += 1
    except OSError as exc:
        log.warning("git invocation failed in %s: %s", where, exc)
    _unanswered += 1
    _FAILED.count += 1
    return None


def log_nonzero_exit(
    args: Sequence[str],
    result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
    where: object,
) -> None:
    """DEBUG-level note of a non-zero git exit, trimmed to stderr's first
    line (git's "fatal: ..." messages are one-liners). Empty stderr is
    still logged so the return code itself is visible."""
    stderr = result.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lines = stderr.strip().splitlines()
    log.debug(
        "git %s exited %s in %s: %s",
        subcommand(args),
        result.returncode,
        where,
        lines[0] if lines else "",
    )
