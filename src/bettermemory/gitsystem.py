"""The system config files the git on PATH reads (ENG-207).

git reads its system config before the global one: the file
``GIT_CONFIG_SYSTEM`` names, else the one built into the binary
(``ETC_GITCONFIG``: ``/etc/gitconfig`` for a git built for prefix /usr,
``<prefix>/etc/gitconfig`` for Homebrew's, the installation's own for
git for Windows). Some builds read files of their own before it, which
``GIT_CONFIG_SYSTEM`` does not replace and ``GIT_CONFIG_NOSYSTEM``
suppresses with it: Apple's git reads its developer directory's
``usr/share/git-core/gitconfig`` (scope ``unknown``), and git for
Windows has read ``%PROGRAMDATA%\\Git\\config``. Where these are is built
into the binary, so git alone names them: ``git var GIT_CONFIG_SYSTEM``
names the system file (git 2.42 and later), and ``git config --list
--show-scope --show-origin`` with the system and global files set to
the null device names each other file it reads that holds an entry, at
system or unknown scope (git 2.26 and later). `sources` answers both,
and a third (ENG-207's round 6): how that git reads a remote, which its
build decides and no option list names (`_reads_remotes`). A git built
with breaking changes reads no legacy ``remotes/`` or ``branches/`` file,
and git from 2.46 empties a remote's URLs at an empty one; git is asked
``ls-remote --get-url`` of a remote only a legacy file of a git
directory under the state directory names (`_probe_repository`), and
the URL it prints says both. A fourth (ENG-207's round 7) asks there
the directory that git expands ``%(prefix)`` to in a path, which a
RUNTIME_PREFIX build takes from where its binary runs (`_PREFIX_ASK`).
A fifth (ENG-207's round 8) asks, in a git directory in the probe
(`_COMPAT_PROBE`), whether git reads a repository whose format keeps
its extensions and sets a ``compatObjectFormat`` other than its object
format (`Sources.compat_differ`): git 2.44 and earlier die on it (setup.c
knows no such extension), 2.45 to 2.53 read it, and from 2.54 a build
with Rust reads it and one without dies ("compatibility hash algorithm
support requires Rust", repository.c's ``repo_set_compat_hash_algo``),
so git is asked what it does, not its build's options. git is never
asked of one the same as the object format, which no git from 2.45
reads: a build without Rust dies on any, and every other stops at
``BUG("hash_algo and compat_hash_algo match")``, which aborts the
process (usage.c's ``BUG_vfl``), leaving a crash report or a core at
each ask (ENG-207's review of round 8). git 2.44 and earlier know no
such extension: under format 1 they die on it, and under a version below
-1 they read the repository, since setup.c there verifies no extension
of a negative format (ENG-207's review of round 10). `gitfile_rule`
declines that pairing, asking nothing, which fails closed against those;
it reads the answer asked here, under format 1, below -1 too, so one
other than the object format that a git of 2.44 or earlier reads there
is declined as well.

A hook client is a new process each prompt, and asking costs a git
process for each question (13 ms each measured, ENG-207), so the answer
is kept in the state directory (`RECORD`), for the PATH it was asked
under (on macOS with the developer directory, `_developer`), with the
stamp of the git binary found on that PATH then; a capture reads the
record and stats that binary, and asks git again only where the binary
stamps otherwise, the answer is older than `REASK_SECONDS` (one that
did not say how git reads a remote, expands ``%(prefix)`` or reads a
compatibility object format, `RETRY_SECONDS`) or none is recorded. The
questions together are bound by `ASK_SECONDS`, the
capture's own fetch budget. A git that does not answer within it or
cannot run, or none on PATH, reads as the last answer git gave for that
PATH (`_kept`: one a racing process recorded meanwhile, else the
process's or the record's), and where it never answered as naming the
fallback file (`/etc/gitconfig`, none on Windows) and no file of its
own; either way it is asked again after `RETRY_SECONDS`, and a failed
ask never replaces a recorded answer of git's (ENG-207's review, R6).
git is asked in an empty owner-only folder under the state directory
(`ASK_FOLDER`), with ``GIT_CEILING_DIRECTORIES`` naming the state
directory and the caller's ``GIT_CONFIG_SYSTEM`` unset (`_where`), so git
finds no repository wherever the state directory sits, and no
repository's config and no override of this process reaches the answer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import _caches
from . import _git as _git_runner
from ._fsutil import atomic_write_bytes, ensure_owner_only_dir
from .gitconfig import _UNSTATED, Stamp, file_stamp

#: The format the record carries in its ``format`` field: 2 adds how git
#: reads a remote (`Sources.legacy`, `Sources.resets`), 3 how it expands
#: ``%(prefix)`` (`Sources.prefix`), 4 whether it reads a compatibility
#: object format other than the object format (`Sources.compat_differ`).
RECORD_FORMAT = 4
#: The record's file in the state directory: one of each format's own, so
#: builds of two formats sharing the state directory (a daemon of one
#: release beside hook clients of another, two installed releases) each
#: keep their answers. In one file each dropped the other's whole record
#: as it wrote its own, and each asked git again at its next process: on
#: every prompt where two releases alternate (ENG-207's review of round
#: 8). Builds of format 3 and before keep ``git-config-sources.json``.
RECORD = f"git-config-sources-v{RECORD_FORMAT}.json"
#: How long the questions may take together: the capture's budget
#: for a fetch (`location_git.FETCH_BUDGET`; tests/test_location_git_config.py
#: pins the two equal), under every hook's. Measured (ENG-207, macOS, git
#: 2.54 through /usr/bin/git's xcrun shim, a load average of 12 to 15):
#: medians of 13 and 14 ms for ``git var``, 82 ms at most over 100 calls;
#: ``ls-remote --get-url`` in the probe (round 6, the same git, a load
#: average of 4) a median of 9 ms, 11 ms at most over 100 calls, and
#: ``config --type=path`` there (round 7) about as long; ``rev-parse
#: --git-dir`` in `_COMPAT_PROBE` (round 8, a load average of 8 to 10) a
#: median of 10 ms, 14 ms at most over 50 calls, and the six questions a
#: first ask made then (one more than now, in a git directory whose
#: compat format was its object format) 95 ms together.
ASK_SECONDS = 0.3
#: How long an answer stands while its git binary stamps as it did: a git
#: installed ahead of it on the same PATH is asked within this.
REASK_SECONDS = 3600.0
#: How long the fallback stands for a git that did not answer, or none.
RETRY_SECONDS = 60.0
_FALLBACK = None if os.name == "nt" else "/etc/gitconfig"
_VAR = ("var", "GIT_CONFIG_SYSTEM")
_LIST = ("config", "--list", "--show-scope", "--show-origin", "-z")
_OVERRIDES = ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
_DEVNULL = {"GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull}
#: PATHs whose answer the record and the process keep, newest first.
_KEPT = 8
#: A build's own files kept of an answer, in git's order.
_OWN_MAX = 8
#: A record is a few hundred bytes for each PATH; a file past this is not
#: one of ours, and is not read.
_MAX_BYTES = 1 << 16
_MAX_PATH = 4096
#: The link ``xcode-select --switch`` points at the developer directory
#: Apple's ``/usr/bin/git`` shim runs git from, where DEVELOPER_DIR is
#: unset (`_developer`).
XCODE_SELECT_LINK = "/var/db/xcode_select_link"
#: Whether this is macOS, where `_developer` reads the developer directory.
_MACOS = sys.platform == "darwin"
#: The folder under the state directory git is asked in (`_where`).
ASK_FOLDER = "git-ask"
#: The git directory in the probe git is asked in whether it reads a
#: compatibility object format (`_COMPAT_ASK`): bare, of format 1 and of
#: object format sha1 (none set), with a ``compatObjectFormat`` of
#: `_COMPAT_FORMAT`, another. No git directory of the probe sets one the
#: same as its object format: git aborts there (the module's docstring).
_COMPAT_PROBE = "compat-differ"
_COMPAT_FORMAT = b"sha256"
#: The git directory under the state directory git is asked how it reads
#: a remote in (`_probe_repository`): a remote ``p`` its config gives no
#: URL and whose ``remotes/p`` file gives one, an empty one and another;
#: how it expands ``%(prefix)/`` (`_PREFIX_ASK`); and, in the git
#: directory `_COMPAT_PROBE` in it, whether it reads a compat format.
_PROBE_FILES = {
    "HEAD": b"ref: refs/heads/main\n",
    "config": b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
    b"[bettermemory]\n\tprefix = %(prefix)/z\n",
    "remotes/p": b"URL: bettermemory-probe-a\nURL:\nURL: bettermemory-probe-b\n",
    f"{_COMPAT_PROBE}/HEAD": b"ref: refs/heads/main\n",
    f"{_COMPAT_PROBE}/config": b"[core]\n\trepositoryformatversion = 1\n"
    b"\tbare = true\n[extensions]\n\tcompatObjectFormat = " + _COMPAT_FORMAT + b"\n",
}
_PROBE_FOLDERS = (
    "objects",
    "refs",
    "remotes",
    *(_COMPAT_PROBE + sub for sub in ("", "/objects", "/refs")),
)
#: The probe's folders whose every entry it names (`_probe_holds`): the
#: probe, ``remotes`` and `_COMPAT_PROBE`; git writes nothing in
#: an ``objects`` or ``refs`` folder answering these questions, and they
#: are not listed.
_PROBE_LISTED = ("", "remotes", _COMPAT_PROBE)


def _probe_name(files: dict[str, bytes], folders: tuple[str, ...]) -> str:
    """The probe's folder for `files` and `folders`: named by a digest of
    them, so a build that probes with other content keeps a folder of its
    own, and the two never rewrite each other's while git reads it
    (ENG-207's round 7)."""
    held = repr((sorted(files.items()), folders)).encode("ascii")
    return "git-probe-" + hashlib.sha256(held).hexdigest()[:16]


PROBE_FOLDER = _probe_name(_PROBE_FILES, _PROBE_FOLDERS)
#: The names the probe has had: `_probe_name`'s, of any content, and the
#: one folder before it was named by its content (`_sweep_probes`).
_PROBE_NAMES = re.compile(r"git-probe(?:-[0-9a-f]{16})?")
#: What of the probe is stamped around git's answer: the folder, its
#: folders and its files (`_probe_stamps`).
_PROBE_STAMPED = ("", *_PROBE_FOLDERS, *_PROBE_FILES)
#: The question (``git ls-remote --get-url p``), and the URL it prints for
#: each answer: the last of the file's (it reads the file and an empty
#: URL empties the list), the first (it reads the file, and an empty URL
#: is one more), or the remote's name (it reads no such file: a git built
#: with breaking changes, all of which empty the list, as git 2.46 does).
_REMOTE_ASK = ("ls-remote", "--get-url", "p")
_REMOTE_ANSWERS = {
    "bettermemory-probe-b": (True, True),
    "bettermemory-probe-a": (True, False),
    "p": (False, True),
}
#: The fourth question, in the probe (ENG-207's round 7): git expands its
#: config's ``bettermemory.prefix``, ``%(prefix)/z``, as it expands an
#: include's path (path.c's ``interpolate_path``: ``system_path``), and
#: what it prints less `_PREFIX_TAIL` is the directory a ``%(prefix)/``
#: path names before its relative rest (`Sources.prefix`): the runtime
#: prefix of a RUNTIME_PREFIX build (Apple's git, git for Windows), taken
#: from where the binary runs, so the record's key (PATH, and on macOS
#: the developer directory) names it; ``%(prefix)`` itself for a git that
#: leaves the path as written. The value does not start with
#: ``:(optional)``, which ``--type=path`` strips and git 2.54's include
#: path does not.
_PREFIX_ASK = ("config", "--type=path", "--get", "bettermemory.prefix")
_PREFIX_TAIL = "/z"
#: The fifth, in `_COMPAT_PROBE` (ENG-207's round 8): git reads
#: the format of a git directory it is named (setup.c's
#: ``apply_repository_format``, which sets the compatibility object
#: format), and exits 0 where it reads it, non-zero where it dies.
_COMPAT_ASK = ("rev-parse", "--git-dir")
#: The caller's variables that would reach the probe's config.
_PROBE_UNSET = (
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)


@dataclass(frozen=True)
class Sources:
    """What the git on a PATH reads before the global config, in its
    order: the files its build reads of its own (`own`), then the system
    file it was built to read (`system`; None where it names none); and
    how it reads a remote (ENG-207's round 6): whether it reads the
    legacy ``remotes/`` and ``branches/`` files (`legacy`: a git built
    with breaking changes reads neither), and whether an empty
    ``remote.<name>.url`` empties the remote's URLs (`resets`, git 2.46
    and later), each None where git did not say (`_reads_remotes`); the
    directory it expands ``%(prefix)`` to in a path (`prefix`,
    `_PREFIX_ASK`; ENG-207's round 7), None where git did not say; and
    whether it reads a repository whose format keeps its extensions and
    sets a ``compatObjectFormat`` other than its object format
    (`compat_differ`), None where git did not say (`_COMPAT_ASK`;
    ENG-207's round 8). One the same as the object format git never reads
    and is never asked of (the module's docstring)."""

    own: tuple[str, ...]
    system: str | None
    legacy: bool | None = None
    resets: bool | None = None
    prefix: str | None = None
    compat_differ: bool | None = None


@dataclass(frozen=True)
class _Answer:
    """git's answer for a PATH (`ok`, given at `asked`), or the fallback
    where it never gave one; `tried` is when an ask after a good answer
    failed, which keeps that answer for `RETRY_SECONDS` from then."""

    key: str
    git: str | None
    stamp: Stamp | None
    asked: float
    ok: bool
    sources: Sources
    tried: float | None = None


_LOCK = threading.Lock()
_HELD: dict[str, _Answer] = {}


@_caches.register
def _clear() -> None:
    with _LOCK:
        _HELD.clear()


def sources(*, stamp: bool = True) -> Sources:
    """The system config files of the git on this process's PATH, from
    the process's answer or the record while it stands (`_current`),
    else asked of git and recorded (`_ask`), or kept where the ask failed
    (`_kept`). `stamp` off takes the process's answer while it is young
    enough without stating its git binary: a kept capture's check
    (`location_git.config_unchanged`), whose capture is read again within
    `location.CACHE_SECONDS` anyway, where the binary is stated."""
    key = _key(os.environ.get("PATH"))
    now = time.time()
    with _LOCK:
        held = _HELD.get(key)
    if held is None or not _current(held, now, stamp=stamp):
        recorded = _recorded(key)
        if recorded is not None and _current(recorded, now):
            got = recorded
        else:
            got = _ask(key, now)
            if not got.ok:
                got = _kept(key, now, held, recorded) or got
            _record(got)
        held = got
        with _LOCK:
            _HELD.pop(key, None)
            _HELD[key] = held
            while len(_HELD) > _KEPT:
                del _HELD[next(iter(_HELD))]
    return held.sources


def reads_compat() -> bool | None:
    """Whether the git on this process's PATH reads a repository whose
    format keeps its extensions (a version set, of 1 or below -1) and sets
    a ``compatObjectFormat`` other than its object format
    (`Sources.compat_differ`): asked of git under format 1, object format
    sha1 and compat format sha256, which a repository of a version below
    -1 or of the other pairing is read as (repository.c's
    ``repo_set_compat_hash_algo`` compares the two names alone, after
    setup.c's ``verify_repository_format``, which a version below -1
    passes as 1 does); None where git has not said
    (`gitfile_rule._judge_format`, ENG-207's round 8). Of one the same as
    the object format git is not asked: none reads it."""
    return sources().compat_differ


def _key(path: str | None) -> str:
    """A PATH's name in the record: its digest, with the developer
    directory's on macOS (`_developer`), so the record holds no PATH; an
    unset PATH is named apart from an empty one."""
    raw = ("\0" if path is None else path) + "\0" + _developer()
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _developer() -> str:
    """On macOS, the developer directory Apple's ``/usr/bin/git`` shim
    runs git from, whose build reads that directory's gitconfig: the one
    DEVELOPER_DIR names, else the target of `XCODE_SELECT_LINK` (one
    readlink, no process). The shim's own stamp does not change with
    either, so each is in the record's key, and a switch is asked of git
    at once. Without either, xcrun's default (Xcode where it is
    installed, else the command line tools) is not read: a change there
    is asked within `REASK_SECONDS`. Elsewhere, nothing."""
    if not _MACOS:
        return ""
    named = os.environ.get("DEVELOPER_DIR")
    try:
        selected = os.readlink(XCODE_SELECT_LINK)
    except (OSError, ValueError):
        selected = None
    return "\0".join("\1" if x is None else x for x in (named, selected))


def _current(answer: _Answer, now: float, *, stamp: bool = True) -> bool:
    """Whether `answer` stands at `now` on a clock that has not gone
    back: one kept after a failed ask (`tried`) for `RETRY_SECONDS` from
    then; else one younger than its lifetime (`REASK_SECONDS`,
    `RETRY_SECONDS` for a git that never answered or did not say how it
    reads a remote, expands ``%(prefix)`` or reads a compatibility object
    format, `Sources.legacy`, `Sources.prefix`, `Sources.compat_differ`)
    whose git binary stamps as it did (not stated where `stamp` is
    off)."""
    if answer.tried is not None:
        return 0 <= now - answer.tried < RETRY_SECONDS
    age = now - answer.asked
    found = answer.sources
    said = (found.legacy, found.prefix, found.compat_differ)
    whole = answer.ok and None not in said
    if not 0 <= age < (REASK_SECONDS if whole else RETRY_SECONDS):
        return False
    return not stamp or answer.git is None or file_stamp(answer.git) == answer.stamp


def _kept(key: str, now: float, *seen: _Answer | None) -> _Answer | None:
    """Where an ask failed: the record's answer if one that stands was
    recorded meanwhile (a racing process's), else the newest answer git
    gave of those `seen` and the record's, kept from `now`
    (`_Answer.tried`); None where git never answered for the PATH."""
    again = _recorded(key)
    if again is not None and again.ok and _current(again, now):
        return again
    good = [a for a in (again, *seen) if a is not None and a.ok]
    if not good:
        return None
    return dataclasses.replace(max(good, key=lambda a: a.asked), tried=now)


def _where() -> tuple[str, dict[str, str]] | None:
    """Where git is asked, and the variable that keeps it there: the
    folder `ASK_FOLDER` under the state directory, made owner-only and
    empty, with ``GIT_CEILING_DIRECTORIES`` naming the state directory, so
    git's walk looks for a repository in that folder alone and finds
    none, and the setup git runs reads no repository's config, which
    neither question needs. The root of git's own drive, asked before,
    can be a working tree: a share's root or a ``subst`` drive on
    Windows, ``/`` in a container built with ``COPY . /`` (the reviewer's
    follow-up to ENG-207). None where the folder cannot be made or holds
    an entry, and git is not asked: the fallback stands, as for a git that
    cannot run."""
    from . import _daemon_client

    try:
        folder = _daemon_client.resolve_state_dir() / ASK_FOLDER
        ensure_owner_only_dir(folder, parents=True)
        real = os.path.realpath(folder)
        if os.listdir(real):
            return None
    except (OSError, ValueError):
        return None
    return real, {"GIT_CEILING_DIRECTORIES": os.path.dirname(real)}


def _absolute(path: str, flavour: ModuleType = os.path) -> bool:
    """Whether `path` names one file wherever it is read from: absolute,
    and on Windows with its drive (``C:/x``, ``\\\\server\\share\\x``). A
    path rooted with no drive (``/x``) names a file on the drive the
    reader stands on; Python before 3.13 took it for absolute on Windows
    and 3.13 does not, so the rule is spelled here, not left to
    ``isabs``. Git for Windows names its files with their drive."""
    if flavour.sep == "\\":
        drive, rest = flavour.splitdrive(path)
        return bool(drive) and rest[:1] in ("\\", "/")
    return bool(flavour.isabs(path))


def _ask(key: str, now: float) -> _Answer:
    """git's answer for the PATH `key` names, the questions within
    `ASK_SECONDS` together; the fallback, not ok, where git is not on
    PATH, cannot run or does not answer in time."""
    failed = _Answer(key, None, None, now, False, Sources((), _FALLBACK))
    git = shutil.which("git")
    stamp = None if git is None else file_stamp(git)
    if git is None or stamp is None or stamp == _UNSTATED:
        return failed
    deadline = time.monotonic() + ASK_SECONDS
    where = _where()
    if where is None:
        return failed
    folder, ceiling = where
    named = _git_runner.run_git(
        _VAR, cwd=folder, timeout=ASK_SECONDS, env=ceiling, unset=_OVERRIDES
    )
    left = deadline - time.monotonic()
    if named is None or left <= 0:
        return failed
    listed = _git_runner.run_git(
        _LIST,
        cwd=folder,
        timeout=left,
        text=False,
        env={**_DEVNULL, **ceiling},
        unset=_OVERRIDES,
    )
    if listed is None:
        return failed
    # A git before 2.42 has no such variable, one before 2.26 no scopes:
    # each answers with an error, which names nothing.
    system = named.stdout.strip() if named.returncode == 0 else ""
    own = _own_files(listed.stdout) if listed.returncode == 0 else ()
    found = Sources(
        own, system or _FALLBACK, *_reads_remotes(folder, ceiling, deadline)
    )
    return _Answer(key, git, stamp, now, True, found)


#: What git says in the probe (`_reads_remotes`): `Sources.legacy`,
#: `Sources.resets`, `Sources.prefix` and `Sources.compat_differ`.
_Probed = tuple[bool | None, bool | None, str | None, bool | None]
#: `_reads_remotes`' answer where git did not give one.
_UNSAID: _Probed = (None, None, None, None)


def _reads_remotes(folder: str, ceiling: dict[str, str], deadline: float) -> _Probed:
    """How the git on PATH reads a remote (`Sources.legacy`,
    `Sources.resets`), from its answer to `_REMOTE_ASK` in the probe's
    git directory (`_probe_repository`), the directory it expands
    ``%(prefix)`` to (`Sources.prefix`), from its answer to `_PREFIX_ASK`
    there, and whether it reads a compatibility object format other than
    the object format (`Sources.compat_differ`), from whether it exits 0
    asked `_COMPAT_ASK` in `_COMPAT_PROBE` (ENG-207's round 8), each
    asked from `folder` before the monotonic `deadline` with no config
    but the probe's own; `_UNSAID` where git does not answer the first two
    so, and it is asked again after `RETRY_SECONDS` (`_current`), as it
    is where it does not answer the last in time, which alone is unsaid. A build's own option list (``git
    version --build-options``) names no breaking changes (git 2.54's
    ``help.c``), so git is asked what it does. The answers are kept only
    where the probe stamps after git answers as it did before its content
    was checked (`_probe_stamps`): another writer's file there while git
    read it (ENG-207's round 7: one rewriting ``remotes/p`` made about one
    ask in three answer wrong) leaves them unsaid."""
    probe = _probe_repository()
    if probe is None:
        return _UNSAID
    root, before = probe
    said: list[str] = []
    for question in (_REMOTE_ASK, _PREFIX_ASK):
        done = _probe_ask(question, folder, root, ceiling, deadline)
        if done is None or done.returncode != 0:
            return _UNSAID
        said.append(done.stdout.rstrip("\r\n"))
    compat = _probe_ask(
        _COMPAT_ASK, folder, os.path.join(root, _COMPAT_PROBE), ceiling, deadline
    )
    if _probe_stamps(root) != before or not _probe_holds(root):
        return _UNSAID
    legacy, resets = _REMOTE_ANSWERS.get(said[0].strip(), (None, None))
    expanded = said[1].endswith(_PREFIX_TAIL)
    prefix = said[1][: -len(_PREFIX_TAIL)] if expanded else None
    differ = None if compat is None else compat.returncode == 0
    return legacy, resets, prefix, differ


def _probe_ask(
    question: tuple[str, ...],
    folder: str,
    git_dir: str,
    ceiling: dict[str, str],
    deadline: float,
) -> subprocess.CompletedProcess[str] | None:
    """git's answer to `question` in the git directory `git_dir` of the
    probe, asked from `folder` with no config but the probe's own, before
    the monotonic `deadline`; None where none is left or git does not
    answer in it."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    return _git_runner.run_git(
        question,
        cwd=folder,
        git_dir=git_dir,
        timeout=left,
        env={**_DEVNULL, "GIT_CONFIG_NOSYSTEM": "1", **ceiling},
        unset=_PROBE_UNSET,
    )


def _probe_repository() -> tuple[str, list[Stamp | None]] | None:
    """The git directory `_REMOTE_ASK` and `_PREFIX_ASK` are asked in,
    which holds the one `_COMPAT_ASK` is (`_COMPAT_PROBE`), with its stamps
    (`_probe_stamps`) taken before its content was checked:
    `PROBE_FOLDER` under the state directory, owner-only, holding
    `_PROBE_FILES` and the folders git's test of a git directory looks
    for, and nothing else (an entry of another's could change the answer:
    none is read). Where it does not hold so (`_probe_holds`), what does
    not belong is removed and it is built again, once, rather than left
    unknown and asked again each `RETRY_SECONDS`; None where it still
    does not, the folder is a link, or it cannot be made so. Another
    content's probe beside it is swept first (`_sweep_probes`)."""
    from . import _daemon_client

    try:
        root = _daemon_client.resolve_state_dir() / PROBE_FOLDER
        _sweep_probes(root.parent)
        for _attempt in range(2):
            ensure_owner_only_dir(root, parents=True)
            if root.is_symlink():
                return None
            for name in _PROBE_FOLDERS:
                ensure_owner_only_dir(root / name)
            for name, data in _PROBE_FILES.items():
                path = root / name
                try:
                    same = path.read_bytes() == data
                except FileNotFoundError:
                    same = False
                if not same:
                    atomic_write_bytes(path, data, mode_before_rename=0o600)
            real = os.path.realpath(root)
            before = _probe_stamps(real)
            if _probe_holds(real):
                return real, before
            _clear_probe(root)
    except (OSError, ValueError):
        pass
    return None


def _sweep_probes(state: Path) -> None:
    """Remove from the state directory `state` each probe of another
    name (`_PROBE_NAMES`: of other content, or the one folder before
    probes were named by it) last changed `REASK_SECONDS` or more before
    now (its own modification time, read without following a link): one
    would stay per change of the probe's content (ENG-207's round 8). A
    link is unlinked, never followed, and a folder removed with what it
    holds, a link in it unlinked. A build that still asks in its probe finds it gone, says
    nothing for that ask (its stamps move) and builds it again; one asking
    within the hour keeps it, younger."""
    if not state.is_dir():
        return
    now = time.time()
    for entry in list(os.scandir(state)):
        if entry.name == PROBE_FOLDER or not _PROBE_NAMES.fullmatch(entry.name):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if now - st.st_mtime < REASK_SECONDS:
                continue
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError:
            continue


def _probe_stamps(root: str) -> list[Stamp | None]:
    """The stamps of the probe at `root`: the folder and its folders,
    whose times move as an entry is made or removed, and each file."""
    return [file_stamp(os.path.join(root, name)) for name in _PROBE_STAMPED]


def _probe_children(folder: str) -> tuple[set[str], set[str]]:
    """The folders and the files the probe holds in its folder `folder`
    ("" the probe itself), by name."""
    prefix = folder + "/" if folder else ""

    def within(names: Iterable[str]) -> set[str]:
        return {
            n[len(prefix) :]
            for n in names
            if n.startswith(prefix) and "/" not in n[len(prefix) :]
        }

    return within(_PROBE_FOLDERS), within(_PROBE_FILES)


def _probe_holds(root: str) -> bool:
    """Whether the probe at `root` holds what it is built to and nothing
    else: each of `_PROBE_FOLDERS` a folder and each of `_PROBE_FILES` a
    regular file with its content (neither a link), and no other entry
    in a folder of `_PROBE_LISTED` (it, ``remotes`` and the git
    directory `_COMPAT_PROBE`)."""
    for folder in _PROBE_LISTED:
        folders, files = _probe_children(folder)
        if set(os.listdir(os.path.join(root, folder))) != folders | files:
            return False
    for name in _PROBE_FOLDERS:
        if not stat.S_ISDIR(os.lstat(os.path.join(root, name)).st_mode):
            return False
    for name, data in _PROBE_FILES.items():
        path = os.path.join(root, name)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        with open(path, "rb") as f:
            if f.read(len(data) + 1) != data:
                return False
    return True


def _clear_probe(root: Path) -> None:
    """Remove from the probe at `root` (no link: `_probe_repository`)
    each entry that does not belong (`_probe_holds`): one of another
    name, in a folder of `_PROBE_LISTED`, and one of a probe's name that
    is not of its kind, a link among them (a link is removed, never
    followed). A file whose content is another's is written again by the
    build."""
    for name in _PROBE_LISTED:
        folder = root / name if name else root
        keep_folders, keep_files = _probe_children(name)
        if folder.is_symlink() or not folder.is_dir():
            continue
        for entry in list(os.scandir(folder)):
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
            if (entry.name in keep_folders and is_dir) or (
                entry.name in keep_files and is_file
            ):
                continue
            if is_dir:
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)


def _own_files(listing: bytes) -> tuple[str, ...]:
    """The files a ``git config --list --show-scope --show-origin -z``
    run with the system and global files at the null device names at
    system or unknown scope, in its order, each once: the files a build
    reads of its own. Each entry is the scope, the origin and the
    ``key\\nvalue``, each ended by a NUL. A file named by a path that is
    not absolute (`_absolute`) is not kept."""
    parts = listing.split(b"\0")
    found: list[str] = []
    for i in range(0, len(parts) - 2, 3):
        scope, origin = parts[i], parts[i + 1]
        if scope not in (b"system", b"unknown") or not origin.startswith(b"file:"):
            continue
        path = os.fsdecode(origin[len(b"file:") :])
        if _absolute(path) and path not in found:
            found.append(path)
    return tuple(found[:_OWN_MAX])


def _record_path() -> Path:
    """`RECORD` (this format's) in the state directory the daemon and the
    hook client resolve alike."""
    from . import _daemon_client

    return _daemon_client.resolve_state_dir() / RECORD


def _entries() -> list[dict[str, Any]]:
    """The record's entries as written, each a mapping; none where there
    is no record, it is past `_MAX_BYTES` or is not one."""
    try:
        with open(_record_path(), "rb") as f:
            data = f.read(_MAX_BYTES + 1)
        raw = json.loads(data) if len(data) <= _MAX_BYTES else None
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or raw.get("format") != RECORD_FORMAT:
        return []
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return []
    return [e for e in entries[:_KEPT] if isinstance(e, dict)]


def _text(raw: Any) -> str | None:
    if isinstance(raw, str) and 0 < len(raw) <= _MAX_PATH and "\0" not in raw:
        return raw
    return None


def _recorded(key: str) -> _Answer | None:
    """The answer recorded for the PATH `key` names, or None: nothing
    recorded for it, or an entry that is not one."""
    for entry in _entries():
        if entry.get("path") != key:
            continue
        try:
            git = entry.get("git")
            stamp = entry.get("stamp")
            own = entry.get("own")
            system = entry.get("system")
            if not isinstance(own, list) or len(own) > _OWN_MAX:
                return None
            files = tuple(_text(p) for p in own)
            if (
                (git is not None and _text(git) is None)
                or (system is not None and _text(system) is None)
                or None in files
            ):
                return None
            stamped: Stamp | None = None
            if stamp is not None:
                if not isinstance(stamp, list) or len(stamp) != 5:
                    return None
                a, b, c, d, e = (int(x) for x in stamp)
                stamped = (a, b, c, d, e)
            tried = entry.get("tried")
            flags = [entry.get(name) for name in ("legacy", "resets", "compat_differ")]
            if not all(flag is None or isinstance(flag, bool) for flag in flags):
                return None
            legacy, resets, differ = flags
            prefix = entry.get("prefix")
            if prefix is not None and (not isinstance(prefix, str) or "\0" in prefix):
                return None
            return _Answer(
                key=key,
                git=git,
                stamp=stamped,
                asked=float(entry["asked"]),
                ok=entry.get("ok") is True and git is not None and stamped is not None,
                sources=Sources(
                    tuple(p for p in files if p),
                    system,
                    legacy,
                    resets,
                    prefix,
                    differ,
                ),
                tried=None if tried is None else float(tried),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _record(answer: _Answer) -> None:
    """Keep `answer` in the record, newest first, over any for its PATH
    but an answer git gave where `answer` is the fallback (a racing
    process's, recorded since this one read the record); where the state
    directory refuses the write nothing is kept, and the next process
    asks again."""
    if not answer.ok:
        prior = _recorded(answer.key)
        if prior is not None and prior.ok:
            return
    entry = {
        "path": answer.key,
        "git": answer.git,
        "stamp": None if answer.stamp is None else list(answer.stamp),
        "asked": answer.asked,
        "ok": answer.ok,
        "own": list(answer.sources.own),
        "system": answer.sources.system,
        "legacy": answer.sources.legacy,
        "resets": answer.sources.resets,
        "prefix": answer.sources.prefix,
        "compat_differ": answer.sources.compat_differ,
        "tried": answer.tried,
    }
    others = [e for e in _entries() if e.get("path") != answer.key]
    body = {"format": RECORD_FORMAT, "entries": [entry, *others][:_KEPT]}
    data = (json.dumps(body, ensure_ascii=True) + "\n").encode("ascii")
    try:
        path = _record_path()
        ensure_owner_only_dir(path.parent, parents=True)
        atomic_write_bytes(path, data, mode_before_rename=0o600)
    except (OSError, ValueError):
        pass
