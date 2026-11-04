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
  and ``GIT_SHALLOW_FILE`` go too, for the same reason; a caller's own
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
  bytes for a caller that parses objects (``text=False``);
* a call git could not run at all (no binary on PATH, the timeout, an
  OSError from the spawn) comes back None with a WARNING, and is
  counted (`unanswered_calls` for the process, `failed_calls` for the
  calling thread); a non-zero exit is an ANSWER ("not a repository",
  "not an ancestor") and comes back as the completed process for the
  caller to read, `log_nonzero_exit` noting it at DEBUG.
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

# How many git invocations could not run at all since import, on any
# thread. Read through `unanswered_calls`.
_unanswered = 0


class _Failed(threading.local):
    """The number of git calls on the current thread that came back None
    from `run_git` or that a caller folded into a failure (`note_failed`),
    whatever the reason. Read through `failed_calls`."""

    count = 0


_FAILED = _Failed()


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


def note_failed() -> None:
    """Count, on the calling thread, an answer the caller folded into a
    failure."""
    _FAILED.count += 1


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
    for name in SCRUBBED_ENV:
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
        if text:
            return subprocess.run(
                command,
                cwd=None if cwd is None else str(cwd),
                input=None if stdin is None else stdin.decode("utf-8", "replace"),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=_environment(env, unset),
            )
        return subprocess.run(
            command,
            cwd=None if cwd is None else str(cwd),
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_environment(env, unset),
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
