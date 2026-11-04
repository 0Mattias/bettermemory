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
system or unknown scope (git 2.26 and later). `sources` answers both.

A hook client is a new process each prompt, and asking costs a git
process for each question (13 ms each measured, ENG-207), so the answer
is kept in the state directory (`RECORD`), for the PATH it was asked
under (on macOS with the developer directory, `_developer`), with the
stamp of the git binary found on that PATH then; a capture reads the
record and stats that binary, and asks git again only where the binary
stamps otherwise, the answer is older than `REASK_SECONDS` or none is
recorded. Both questions together are bound by `ASK_SECONDS`, the
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
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import _caches
from . import _git as _git_runner
from ._fsutil import atomic_write_bytes, ensure_owner_only_dir
from .gitconfig import _UNSTATED, Stamp, file_stamp

#: The record's file in the state directory.
RECORD = "git-config-sources.json"
#: The format the record carries in its ``format`` field.
RECORD_FORMAT = 1
#: How long both questions may take together: the capture's budget for a
#: fetch (`location_git.FETCH_BUDGET`; tests/test_location_git_config.py
#: pins the two equal), under every hook's. Measured (ENG-207, macOS, git
#: 2.54 through /usr/bin/git's xcrun shim, a load average of 12 to 15):
#: medians of 13 and 14 ms for ``git var``, 82 ms at most over 100 calls.
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


@dataclass(frozen=True)
class Sources:
    """What the git on a PATH reads before the global config, in its
    order: the files its build reads of its own (`own`), then the system
    file it was built to read (`system`; None where it names none)."""

    own: tuple[str, ...]
    system: str | None


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
    `RETRY_SECONDS` for a git that never answered) whose git binary
    stamps as it did (not stated where `stamp` is off)."""
    if answer.tried is not None:
        return 0 <= now - answer.tried < RETRY_SECONDS
    age = now - answer.asked
    if not 0 <= age < (REASK_SECONDS if answer.ok else RETRY_SECONDS):
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
    """git's answer for the PATH `key` names, both questions within
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
    return _Answer(key, git, stamp, now, True, Sources(own, system or _FALLBACK))


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
    """``<state>/git-config-sources.json``, under the state directory the
    daemon and the hook client resolve alike."""
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
            return _Answer(
                key=key,
                git=git,
                stamp=stamped,
                asked=float(entry["asked"]),
                ok=entry.get("ok") is True and git is not None and stamped is not None,
                sources=Sources(tuple(p for p in files if p), system),
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
