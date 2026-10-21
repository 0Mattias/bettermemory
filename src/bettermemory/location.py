"""The caller's location (design 3.1, 8.1) and its resolution.

Each caller reads where it stands in its own namespace, from files: the
directory's repository (top level, git directory and common directory and
their filesystem objects), HEAD and the branch, up to `REF_TIPS_MAX` ref
tips (HEAD, ``refs/`` and ``packed-refs``) that decide whether any commit
is reachable, every remote URL of the config file as a remote key, the
shallow marker, the kind, the checkout token (read, and written at first
sight under the policy switch, decision D11), the init stamps, the project
files from the cwd up, the content manifest of a commit-less checkout or a
folder project, and the evicted-file state (decision D19). No git process
runs for any of it. `capture` returns that as a `Capture`; `to_location`
turns a capture into `membership.Location` with a history reader.

The history reader (`RepositoryHistory`) reads objects through
`gitobjects.ObjectReader` and asks ``git cat-file`` only where the reader
reports itself incomplete (design 3.1); without git on PATH it answers from
the files alone.

A capture is kept against a file signature (`signature`): the same lifetime
as `origin.capture`'s cache (`CACHE_SECONDS` on both the monotonic and the
wall clock) and the same rule, a capture is kept only when the signature
read the same before and after it. A known checkout then costs the
signature's stats and no git process (design 8.9, E16); its resolution is
the model's index lookup by object (rung 2, E29). The walk above a
repository's root is cached apart, on `project_file.above_start`.

`resolve_here` resolves the caller of this process: the machine opened
once per process (`machine.open_machine`, decision D16) and recorded in the
store before any resolution, the location captured, the resolution run
under a per-location lock inside the model's ``BEGIN IMMEDIATE``
transaction, which re-checks the object key (E28), and a token re-minted
where the resolution asks for one.

The hook client (design 8.1) posts a namespace probe (`probe`). Where the
daemon reads the same object at the posted cwd, it captures there itself.
Where it does not (a container, I39 to I42), it asks the client for the
location token (`Capture.to_token`) and the client's machine, and for each
question the model asks of the repository it names the ids, and the client
answers from its own files (`answer`); the daemon never reads the client's
paths (design 8.7). A posted request with no probe (an rc3 client) is
captured at its cwd as before; where that path cannot be read the
resolution is INDETERMINATE with line L8 (I42).

Beyond the standard library this module imports only `_caches`,
`checkout_token`, `githead`, `gitobjects` and `project_file`, so the hook
client imports it; the membership model, the store and the config are
imported where a function needs them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _caches, checkout_token, githead, gitobjects, project_file

if TYPE_CHECKING:
    from .config import Config
    from .machine import MachineIdentity
    from .membership import Checkout, Location, Membership, Resolution, View
    from .store import RecordHome, Store

__all__ = [
    "BARE",
    "CACHE_SECONDS",
    "FETCH_BUDGET",
    "FOLDER",
    "INDETERMINATE",
    "LINKED",
    "NO_CWD",
    "NO_MACHINE",
    "OUTSIDE",
    "PRIMARY",
    "REF_TIPS_MAX",
    "REPO",
    "TOKEN_VERSION",
    "WALK_LIMIT",
    "AnsweredHistory",
    "AnsweredLiveness",
    "Caller",
    "Capture",
    "Exchange",
    "Key",
    "NeedWitnesses",
    "RepositoryHistory",
    "answer",
    "capture",
    "capture_location",
    "capture_token",
    "continuity",
    "forget_session",
    "object_key",
    "path_liveness",
    "probe",
    "process_machine",
    "remote_key",
    "resolve_caller",
    "resolve_hook",
    "resolve_here",
    "signature",
    "to_location",
    "view_state",
]

# -- vocabulary (equal to membership's; tests/test_membership_capture.py
# pins each pair) ---------------------------------------------------------

REPO = "repo"
OUTSIDE = "outside"
INDETERMINATE = "indeterminate"
PRIMARY = "primary"
LINKED = "linked"
BARE = "bare"
FOLDER = "folder"

#: The most ref tips a capture reads to decide commit-less (design 2.1).
REF_TIPS_MAX = 64
#: The most (path, size) entries of a content manifest (design 2.2, C).
MANIFEST_LIMIT = 4096
#: How long a fetch of an evicted HEAD or config may take before the
#: capture defers (design 3.1, decision D19; 300 ms in the prototype).
FETCH_BUDGET = 0.3
#: How long a capture answers for its directory, as `origin.capture`'s
#: cache (`origin.ORIGIN_CACHE_SECONDS`), on both clocks.
CACHE_SECONDS = 600.0
#: Captures kept, oldest evicted first.
CACHE_CAP = 256
#: Commits a rewritten-history walk reads from HEAD; the model's
#: `membership.WALK_LIMIT`, so a client's walk covers exactly the model's.
WALK_LIMIT = 512
#: Blob ids sampled per witness commit, and their floor in bytes: small
#: files (an empty ``__init__.py``) are shared by unrelated repositories.
BLOBS_PER_WITNESS = 8
BLOB_MIN_SIZE = 64
#: Tree entries looked at for the blob sample (the root and its first level).
BLOB_LOOK = 64
#: The location token's format version.
TOKEN_VERSION = 1
#: The cwd of a caller that has none (its working directory was removed
#: under it): the path its indeterminate location and the unresolved home
#: of its writes are keyed on (decision B6).
NO_CWD = "<no working directory>"
#: The machine of a caller whose process could not open this machine's
#: state directory (`machine.open_machine` raised; the shim serves in
#: process for the same reason): no machine id was read, so the caller's
#: location is indeterminate and its writes are homed under this id at
#: the cwd it gave (decision B6).
NO_MACHINE = "<no machine>"
#: Rounds of the witness exchange a client answers before it stops.
EXCHANGE_ROUNDS = 8

_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG})
_FULL_HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HEAD_LIMIT = 1 << 16
_CONFIG_LIMIT = 1 << 20
_GITFILE_LIMIT = 1 << 20
# SF_DATALESS (macOS <sys/stat.h>) and the Windows recall attributes
# (winnt.h): the file's data is held by a provider and a read would wait
# on a download.
_SF_DATALESS = 0x40000000
_WINDOWS_RECALL = 0x00040000 | 0x00400000
# Environment variables that move git's discovery; under any of them the
# capture reads what they name and is never cached.
_DISCOVERY_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
_PROJECT_FILE = project_file.PROJECT_FILE_NAME
_TOKEN_SLOT = "token"

#: An object key: device, inode (the volume serial and file index on
#: Windows) and birth time in ns where the platform reports it, as text.
Key = tuple[str, str, str | None]


# ---------------------------------------------------------------------------
# Filesystem objects
# ---------------------------------------------------------------------------

#: Whether birth times are read; tests switch it off to act as a platform
#: or filesystem that reports none.
REPORT_BIRTH = True

_STATX_LOCK = threading.Lock()
_STATX: list[Any] = []


def _statx_birth(path: str) -> int | None:
    """Birth time in ns through statx(2) with STATX_BTIME, called through
    ctypes from the C library (glibc and musl alike) where the filesystem
    reports it (design 8.7); None where the mask says it does not or the
    call is missing. Linux only."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes

    with _STATX_LOCK:
        if not _STATX:
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                fn = libc.statx
                fn.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_uint,
                    ctypes.c_void_p,
                ]
                fn.restype = ctypes.c_int
                _STATX.append(fn)
            except (OSError, AttributeError):
                _STATX.append(None)
        fn = _STATX[0]
    if fn is None:
        return None
    buf = ctypes.create_string_buffer(256)
    at_fdcwd, statx_btime = -100, 0x800
    if fn(at_fdcwd, os.fsencode(path), 0, statx_btime, buf) != 0:
        return None
    raw = buf.raw
    if not int.from_bytes(raw[0:4], "little") & statx_btime:  # stx_mask
        return None
    seconds = int.from_bytes(raw[80:88], "little", signed=True)  # stx_btime
    nanos = int.from_bytes(raw[88:92], "little")
    return seconds * 1_000_000_000 + nanos


def _birth(path: str, st: os.stat_result) -> int | None:
    """The birth time of `path` in ns: ``st_birthtime`` on macOS and BSD,
    the creation time on Windows, statx on Linux (design 8.7)."""
    if not REPORT_BIRTH:
        return None
    exact = getattr(st, "st_birthtime_ns", None)
    if isinstance(exact, int):
        return exact
    seconds = getattr(st, "st_birthtime", None)
    if isinstance(seconds, (int, float)):
        return int(seconds * 1_000_000_000)
    if sys.platform == "win32":
        # Before 3.12 Windows reports the creation time as st_ctime.
        return st.st_ctime_ns
    return _statx_birth(path)


def object_key(path: str, st: os.stat_result | None = None) -> Key | None:
    """`path`'s filesystem object, or None when it cannot be stat'ed."""
    if st is None:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None
    birth = _birth(path, st)
    return (str(st.st_dev), str(st.st_ino), None if birth is None else str(birth))


def _evicted(st: os.stat_result) -> bool:
    """Whether a file provider holds the file's data elsewhere (design 3.1,
    D19): SF_DATALESS on macOS, the recall attributes on Windows."""
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _WINDOWS_RECALL)


def _read_one(path: str) -> None:
    with open(path, "rb") as fh:
        fh.read(1)


def _fetch(path: str, budget: float) -> bool:
    """Ask the provider for an evicted file: a read on a worker thread,
    which makes the provider download it, waited on for `budget` seconds.
    A fetch that outruns the budget keeps running; the caller defers
    (design 3.1, D19)."""
    done = threading.Event()

    def run() -> None:
        with contextlib.suppress(OSError):
            _read_one(path)
        done.set()

    threading.Thread(target=run, name="bettermemory-fetch", daemon=True).start()
    if not done.wait(budget):
        return False
    try:
        return not _evicted(os.stat(path))
    except OSError:
        return False


def _stamp(path: str) -> tuple[int, int, int, int, int] | None:
    try:
        return githead.stamp(path)
    except (OSError, ValueError):
        return (-1, -1, -1, -1, -1)


# ---------------------------------------------------------------------------
# Remote keys
# ---------------------------------------------------------------------------

_SCHEME_URL = re.compile(
    r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", re.IGNORECASE
)
_SCP_URL = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def remote_key(url: str | None, base: str | None = None) -> str | None:
    """``host/owner/name``, lower case, without a ``.git`` suffix: the
    spelling two URLs of one hosted repository share. A ``file://`` URL or
    a path is ``local:<absolute path>``, a relative path taken from `base`
    (the repository's top level), as the prototype keys them."""
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    if text.lower().startswith("file://"):
        return "local:" + _local(text[len("file://") :], base)
    m = _SCHEME_URL.match(text)
    if m:
        host, rest = m.group(1), m.group(2)
    else:
        scp = _SCP_URL.match(text)
        drive = len(text) > 1 and text[1] == ":" and text[0].isalpha()
        if scp is None or text.startswith(("/", ".", "\\")) or drive:
            return "local:" + _local(text, base)
        host, rest = scp.group(1), scp.group(2)
    rest = rest.strip("/")
    if rest.endswith(".git"):
        rest = rest[:-4]
    return f"{host.lower()}/{rest.lower()}"


def _local(path: str, base: str | None) -> str:
    joined = path if os.path.isabs(path) or base is None else os.path.join(base, path)
    resolved = os.path.realpath(joined)
    return resolved.rstrip("/\\") or resolved


def _unquote(value: str) -> str:
    """A git config value: an unquoted ``#`` or ``;`` starts a comment,
    double quotes group, a backslash escapes."""
    out: list[str] = []
    quoted = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')


def _config_values(path: str) -> dict[tuple[str, str | None, str], list[str]]:
    """``{(section, subsection, key): values}`` of a git config file, read
    as bytes and bounded; includes are not followed. Section and key names
    are lower case."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_CONFIG_LIMIT + 1)
    except OSError:
        return {}
    if len(data) > _CONFIG_LIMIT:
        return {}
    out: dict[tuple[str, str | None, str], list[str]] = {}
    section: tuple[str, str | None] | None = None
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            m = _SECTION.match(line)
            if m is None:
                section = None
                continue
            name, sub = m.group(1).lower(), m.group(2)
            if sub is None and "." in name:
                # The deprecated [section.subsection] spelling.
                name, _, sub = name.partition(".")
            section = (name, sub.replace('\\"', '"') if sub is not None else None)
            line = line[m.end() :].strip()
            if not line:
                continue
        if section is None:
            continue
        key, eq, value = line.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        out.setdefault((section[0], section[1], key), []).append(
            _unquote(value) if eq else "true"
        )
    return out


def _truthy(values: Sequence[str] | None) -> bool | None:
    if not values:
        return None
    last = values[-1].strip().lower()
    if last in ("true", "yes", "on", "1"):
        return True
    if last in ("false", "no", "off", "0", ""):
        return False
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Git:
    """A repository found from a directory: the ``.git`` entry (None for a
    bare repository, the inside of a git directory, or GIT_DIR), the git
    directory, the common directory, the top level (None when bare) and
    the kind."""

    dotgit: str | None
    git_dir: str
    common_dir: str
    root: str | None
    kind: str


def _ceiling(path: str) -> str | None:
    """The longest ``GIT_CEILING_DIRECTORIES`` entry that is a strict
    ancestor of `path`: git probes nothing at or above it."""
    raw = os.environ.get("GIT_CEILING_DIRECTORIES")
    if not raw:
        return None
    best: str | None = None
    for entry in raw.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        ceiling = os.path.normpath(entry)
        if path != ceiling and _inside(path, ceiling):
            if best is None or len(ceiling) > len(best):
                best = ceiling
    return best


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normpath(path), os.path.normpath(root)
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)


def _gitfile_target(dotgit: str) -> str | None:
    """The directory a ``gitdir: <path>`` file names, or None."""
    try:
        with open(dotgit, "rb") as fh:
            data = fh.read(_GITFILE_LIMIT + 1)
    except OSError:
        return None
    if len(data) > _GITFILE_LIMIT:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:") :].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(dotgit), target)
    return os.path.normpath(target)


def _common_of(git_dir: str) -> tuple[str, bool]:
    """(common directory, whether a ``commondir`` file named it)."""
    env = os.environ.get("GIT_COMMON_DIR")
    if env:
        return os.path.normpath(os.path.join(git_dir, env)), True
    try:
        with open(os.path.join(git_dir, "commondir"), "rb") as fh:
            rel = fh.read(_GITFILE_LIMIT).decode("utf-8", errors="replace").strip()
    except OSError:
        return git_dir, False
    if not rel:
        return git_dir, False
    return os.path.normpath(os.path.join(git_dir, rel)), True


def _is_git_directory(path: str) -> bool:
    """git's ``is_git_directory``: HEAD, and ``objects`` and ``refs`` in
    the common directory."""
    if not os.path.isfile(os.path.join(path, "HEAD")):
        return False
    common, _ = _common_of(path)
    return os.path.isdir(os.path.join(common, "objects")) and os.path.isdir(
        os.path.join(common, "refs")
    )


def _from_env(start: str) -> _Git | None:
    """The repository GIT_DIR (with GIT_WORK_TREE, GIT_COMMON_DIR or
    core.worktree) names (I22)."""
    raw = os.environ.get("GIT_DIR")
    if not raw:
        return None
    git_dir = os.path.normpath(os.path.join(start, raw))
    common, linked = _common_of(git_dir)
    work = os.environ.get("GIT_WORK_TREE")
    values = _config_values(os.path.join(common, "config"))
    root: str | None
    if work:
        root = os.path.normpath(os.path.join(start, work))
    else:
        listed = values.get(("core", None, "worktree")) or []
        configured = listed[-1] if listed else None
        if configured:
            root = os.path.normpath(os.path.join(git_dir, configured))
        elif _truthy(values.get(("core", None, "bare"))):
            root = None
        else:
            root = start
    kind = BARE if root is None else (LINKED if linked else PRIMARY)
    return _Git(None, git_dir, common, root, kind)


def _discover(start: str) -> _Git | None:
    """The repository git would find from `start` (``setup.c``'s walk):
    at each directory a ``.git`` entry first, then the directory itself as
    a git directory (a bare repository, or the inside of one); stopping
    below a ceiling and at a filesystem boundary (unless
    GIT_DISCOVERY_ACROSS_FILESYSTEM)."""
    if os.environ.get("GIT_DIR"):
        return _from_env(start)
    ceiling = _ceiling(start)
    across = bool(os.environ.get("GIT_DISCOVERY_ACROSS_FILESYSTEM"))
    device: int | None = None
    d = os.path.normpath(start)
    if not across:
        device = os.stat(d).st_dev
    while True:
        dotgit = os.path.join(d, ".git")
        try:
            st = os.stat(dotgit)
        except (OSError, ValueError):
            st = None
        if st is not None:
            found = _at_dotgit(d, dotgit, st)
            if found is not None:
                return found
        if os.path.isfile(os.path.join(d, "HEAD")) and _is_git_directory(d):
            common, linked = _common_of(d)
            values = _config_values(os.path.join(common, "config"))
            if os.path.basename(d) == ".git" and not _truthy(
                values.get(("core", None, "bare"))
            ):
                parent = os.path.dirname(d)
                return _Git(d, d, common, parent, LINKED if linked else PRIMARY)
            return _Git(None, d, common, None, BARE)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        if ceiling is not None and len(parent) <= len(ceiling):
            return None
        if device is not None:
            try:
                if os.stat(parent).st_dev != device:
                    return None
            except OSError:
                return None
        d = parent


def _at_dotgit(directory: str, dotgit: str, st: os.stat_result) -> _Git | None:
    if stat.S_ISDIR(st.st_mode):
        if not os.path.isfile(os.path.join(dotgit, "HEAD")):
            return None
        return _Git(dotgit, dotgit, dotgit, directory, PRIMARY)
    if not stat.S_ISREG(st.st_mode):
        return None
    target = _gitfile_target(dotgit)
    if target is None or not os.path.isfile(os.path.join(target, "HEAD")):
        return None
    common, linked = _common_of(target)
    # A ``.git`` file names a linked worktree's git directory (it has a
    # ``commondir``) or a git directory kept elsewhere (``--separate-git-
    # dir``, a submodule's): the latter is the primary checkout of its
    # repository, its git directory elsewhere.
    return _Git(dotgit, target, common, directory, LINKED if linked else PRIMARY)


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSeen:
    """A project file found at the cwd or above, with its directory's
    object."""

    id: str
    name: str
    dir: str
    path: str
    boundary: bool
    key: Key | None


@dataclass(frozen=True)
class Capture:
    """What a caller's directory is, read in its own namespace (design
    3.1). ``state`` is ``repo``, ``outside`` or ``indeterminate``; a plain
    folder with a project file at the cwd or above is ``outside`` with
    ``kind`` ``folder``. ``token`` is the token found, ``token_written``
    one this capture wrote, ``token_status`` what happened (``present``,
    ``written``, ``failed:<reason>``, ``skipped:<reason>``, ``n/a``).
    ``file_errors`` are the lines of the project files that name no
    project (decision B1). ``evicted`` is set when HEAD or config is a
    provider's placeholder the fetch did not bring within the budget;
    ``detail`` then names the file. ``cwd_key`` and ``hostname`` let the
    daemon tell whether it reads the same object at ``cwd``."""

    cwd: str
    state: str
    root: str | None = None
    kind: str | None = None
    git_dir: str | None = None
    common_dir: str | None = None
    object_key: Key | None = None
    common_object_key: Key | None = None
    root_object_key: Key | None = None
    cwd_key: Key | None = None
    head: str | None = None
    branch: str | None = None
    ref_tips: tuple[str, ...] = ()
    remote_keys: tuple[str, ...] = ()
    shallow: bool = False
    token: str | None = None
    token_written: str | None = None
    token_status: str = "n/a"
    stamps: tuple[tuple[str, int, int, int, int], ...] = ()
    files: tuple[FileSeen, ...] = ()
    file_errors: tuple[str, ...] = ()
    file_tracked: bool | None = None
    evicted: bool = False
    detail: str = ""
    manifest: tuple[str, tuple[tuple[str, int], ...]] | None = None
    hostname: str | None = None

    @property
    def born(self) -> bool:
        """A commit is reachable from HEAD or another ref (design 2.1)."""
        return self.head is not None or bool(self.ref_tips)

    def to_token(self) -> dict[str, Any]:
        """The location token the hook client posts (design 3.1, 8.1): the
        capture as JSON, format `TOKEN_VERSION`."""

        def key(k: Key | None) -> list[str | None] | None:
            return None if k is None else list(k)

        return {
            "v": TOKEN_VERSION,
            "cwd": self.cwd,
            "state": self.state,
            "root": self.root,
            "kind": self.kind,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "object": key(self.object_key),
            "common_object": key(self.common_object_key),
            "root_object": key(self.root_object_key),
            "cwd_object": key(self.cwd_key),
            "head": self.head,
            "branch": self.branch,
            "ref_tips": list(self.ref_tips),
            "remotes": list(self.remote_keys),
            "shallow": self.shallow,
            "token": self.token,
            "token_written": self.token_written,
            "token_status": self.token_status,
            "stamps": [list(s) for s in self.stamps],
            "files": [
                {
                    "id": f.id,
                    "name": f.name,
                    "dir": f.dir,
                    "path": f.path,
                    "boundary": f.boundary,
                    "object": key(f.key),
                }
                for f in self.files
            ],
            "file_errors": list(self.file_errors),
            "file_tracked": self.file_tracked,
            "evicted": self.evicted,
            "detail": self.detail,
            "manifest": (
                None
                if self.manifest is None
                else {
                    "name": self.manifest[0],
                    "entries": [list(e) for e in self.manifest[1]],
                }
            ),
            "hostname": self.hostname,
        }

    @classmethod
    def from_token(cls, raw: Any) -> Capture | None:
        """The capture a posted token describes, or None when it is not a
        well-formed token of this version. Every field is checked for its
        type and bounded in count; a token is the client's word on its
        own namespace, never read as a path on this side."""
        if not isinstance(raw, Mapping) or raw.get("v") != TOKEN_VERSION:
            return None
        try:
            return cls(
                cwd=_text(raw["cwd"]) or "",
                state=_choice(raw["state"], (REPO, OUTSIDE, INDETERMINATE)),
                root=_opt_text(raw.get("root")),
                kind=_opt_choice(raw.get("kind"), (PRIMARY, LINKED, BARE, FOLDER)),
                git_dir=_opt_text(raw.get("git_dir")),
                common_dir=_opt_text(raw.get("common_dir")),
                object_key=_key(raw.get("object")),
                common_object_key=_key(raw.get("common_object")),
                root_object_key=_key(raw.get("root_object")),
                cwd_key=_key(raw.get("cwd_object")),
                head=_opt_hash(raw.get("head")),
                branch=_opt_text(raw.get("branch")),
                ref_tips=tuple(
                    _hash(x) for x in _bounded(raw.get("ref_tips"), REF_TIPS_MAX)
                ),
                remote_keys=tuple(_text(x) for x in _bounded(raw.get("remotes"), 256)),
                shallow=bool(raw.get("shallow")),
                token=_opt_text(raw.get("token")),
                token_written=_opt_text(raw.get("token_written")),
                token_status=_text(raw.get("token_status", "n/a")),
                stamps=tuple(
                    (_text(s[0]), int(s[1]), int(s[2]), int(s[3]), int(s[4]))
                    for s in _bounded(raw.get("stamps"), 8)
                ),
                files=tuple(
                    FileSeen(
                        id=_text(f["id"]),
                        name=_text(f["name"]),
                        dir=_text(f["dir"]),
                        path=_text(f["path"]),
                        boundary=bool(f.get("boundary")),
                        key=_key(f.get("object")),
                    )
                    for f in _bounded(raw.get("files"), 64)
                ),
                file_errors=tuple(
                    _text(x) for x in _bounded(raw.get("file_errors"), 64)
                ),
                file_tracked=(
                    None
                    if raw.get("file_tracked") is None
                    else bool(raw.get("file_tracked"))
                ),
                evicted=bool(raw.get("evicted")),
                detail=_text(raw.get("detail", "")),
                manifest=_manifest_from(raw.get("manifest")),
                hostname=_opt_text(raw.get("hostname")),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("not text")
    return value


def _opt_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _choice(value: Any, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{value!r} not one of {allowed}")
    return str(value)


def _opt_choice(value: Any, allowed: Sequence[str]) -> str | None:
    return None if value is None else _choice(value, allowed)


def _hash(value: Any) -> str:
    text = _text(value).lower()
    if _FULL_HASH.fullmatch(text) is None:
        raise ValueError("not a full hash")
    return text


def _opt_hash(value: Any) -> str | None:
    return None if value is None else _hash(value)


def _key(value: Any) -> Key | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("not an object key")
    device, inode, birth = value
    return (str(device), str(inode), None if birth is None else str(birth))


def _bounded(value: Any, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("not a bounded list")
    return value


def _manifest_from(value: Any) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("not a manifest")
    entries = _bounded(value.get("entries"), MANIFEST_LIMIT)
    return (
        _text(value.get("name")),
        tuple((_text(e[0]), int(e[1])) for e in entries),
    )


def _path_state(path: str) -> tuple[str, os.stat_result | None, str]:
    """('present', stat, '') or ('gone' | 'unreachable', None, detail):
    'gone' when the OS answered that nothing is there, 'unreachable' when
    it could not say (permission, a dropped mount)."""
    try:
        st = os.stat(path)
    except ValueError as exc:
        return "gone", None, str(exc)
    except OSError as exc:
        reason = os.strerror(exc.errno) if exc.errno else str(exc)
        if exc.errno in _GONE_ERRNOS:
            return "gone", None, reason
        return "unreachable", None, reason
    return "present", st, ""


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _resolve_head(git: _Git) -> tuple[str | None, str | None]:
    """(commit HEAD names, branch) through githead's ref reader, which
    follows ``refs_resolve_ref_unsafe`` over the files backend and peels a
    tag through the object reader (`githead.head_commit`)."""
    gd = githead.GitDir(
        Path(git.git_dir), Path(git.common_dir), Path(git.root or git.git_dir)
    )
    sha = githead.head_commit(gd)
    ref = githead.head_ref(gd)
    branch = (
        ref[len("refs/heads/") :] if ref and ref.startswith("refs/heads/") else None
    )
    return (sha.lower() if sha else None), branch


def _ref_tips(git: _Git) -> tuple[str, ...]:
    """Up to `REF_TIPS_MAX` commit ids the branch and remote-tracking refs
    point at (loose refs, then packed-refs), sorted: with HEAD, what
    decides whether the repository has a commit (design 2.1)."""
    tips: set[str] = set()
    for sub in ("refs/heads", "refs/remotes"):
        top = os.path.join(git.common_dir, sub)
        for directory, dirs, files in os.walk(top):
            dirs.sort()
            for name in sorted(files):
                try:
                    with open(os.path.join(directory, name), "rb") as fh:
                        value = fh.read(128).decode("ascii", errors="replace").strip()
                except OSError:
                    continue
                if _FULL_HASH.fullmatch(value):
                    tips.add(value)
                if len(tips) >= REF_TIPS_MAX:
                    return tuple(sorted(tips))
    try:
        with open(os.path.join(git.common_dir, "packed-refs"), "rb") as fh:
            for raw in fh:
                parts = raw.decode("ascii", errors="replace").split()
                if (
                    len(parts) == 2
                    and parts[1].startswith(("refs/heads/", "refs/remotes/"))
                    and _FULL_HASH.fullmatch(parts[0])
                ):
                    tips.add(parts[0])
                    if len(tips) >= REF_TIPS_MAX:
                        break
    except OSError:
        pass
    return tuple(sorted(tips))


def _init_stamps(git_dir: str) -> tuple[tuple[str, int, int, int, int], ...]:
    """Stamps of the files ``git init`` writes and no git command
    rewrites, compared by equality only (amendment A2)."""
    out: list[tuple[str, int, int, int, int]] = []
    for name in ("description", "info/exclude"):
        try:
            st = os.stat(os.path.join(git_dir, *name.split("/")))
        except OSError:
            continue
        out.append((name, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size))
    return tuple(out)


def _manifest(root: str) -> tuple[str, tuple[tuple[str, int], ...]]:
    """The content witness (design 2.2, C): relative paths and sizes of the
    working tree outside ``.git`` and the root's project file, bounded by
    `MANIFEST_LIMIT`, with the folder's name."""
    entries: list[tuple[str, int]] = []
    todo = [root]
    while todo and len(entries) < MANIFEST_LIMIT:
        directory = todo.pop()
        try:
            with os.scandir(directory) as it:
                listed = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subdirs: list[str] = []
        for entry in listed:
            if entry.name == ".git":
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                    continue
                if directory == root and entry.name == _PROJECT_FILE:
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
            entries.append((rel, size))
            if len(entries) >= MANIFEST_LIMIT:
                break
        todo.extend(reversed(subdirs))
    name = os.path.basename(root.rstrip("/\\")) or root
    return name, tuple(sorted(entries))


_ABOVE_LOCK = threading.Lock()
_ABOVE: dict[str, tuple[float, float, project_file.Above]] = {}


@_caches.register
def _clear_above() -> None:
    with _ABOVE_LOCK:
        _ABOVE.clear()


def _walk_files(
    cwd: str, root: str | None
) -> tuple[project_file.Walk, tuple[FileSeen, ...], tuple[str, ...]]:
    """The project files from `cwd` up (`project_file.find`): inside a
    repository the part from the cwd to the root is read on every capture
    and the part above the root comes from its own cache, keyed on
    `project_file.above_start` with the capture's lifetime (design 3.1).
    Without a repository the whole walk is read."""
    above: project_file.Above | None = None
    start = project_file.above_start(cwd, root) if root is not None else None
    now, wall = time.monotonic(), time.time()
    if start is not None:
        with _ABOVE_LOCK:
            hit = _ABOVE.get(str(start))
        if hit is not None and _fresh(hit[0], hit[1], now, wall):
            above = hit[2]
    walk = project_file.find(cwd, root, above=above)
    if start is not None and above is None:
        with _ABOVE_LOCK:
            _ABOVE[str(start)] = (now, wall, walk.above)
            while len(_ABOVE) > CACHE_CAP:
                del _ABOVE[next(iter(_ABOVE))]
    files = tuple(
        FileSeen(
            id=f.id,
            name=f.name,
            dir=str(f.directory),
            path=str(f.path),
            boundary=f.boundary,
            key=object_key(str(f.directory)),
        )
        for f in walk.files
    )
    return walk, files, tuple(e.line for e in walk.errors)


def _file_tracked(common_dir: str, head: str) -> bool | None:
    """Whether HEAD's tree has the project file at its root, read through
    the object reader; None when the objects do not say."""
    reader = gitobjects.ObjectReader(common_dir)
    commit = reader.commit(head)
    if commit is None:
        return None
    entries = reader.tree(commit.tree)
    if entries is None:
        return None
    wanted = _PROJECT_FILE.encode()
    return any(e.name == wanted and not stat.S_ISDIR(e.mode) for e in entries)


def _token_fields(
    git: _Git, *, write_token: bool
) -> tuple[str | None, str | None, str]:
    """(token found, token written, status) for the git directory: read,
    and written where absent and the policy allows (decision D11). An
    existing token, or a file that is not one, is never replaced."""
    found = checkout_token.read(git.git_dir)
    if found.state == "present":
        return found.token, None, "present"
    if found.state != "absent":
        return None, None, f"failed:{found.state}"
    if not write_token:
        return None, None, "skipped:policy"
    wrote = checkout_token.write(git.git_dir, policy=True, bare=git.kind == BARE)
    if wrote.status == "written":
        return None, wrote.token, "written"
    if wrote.status == "exists":
        return wrote.token, None, "present"
    if wrote.status == "malformed":
        return None, None, "failed:malformed"
    return None, None, wrote.status


def _indeterminate(cwd: str, detail: str, key: Key | None = None) -> Capture:
    return Capture(
        cwd=cwd, state=INDETERMINATE, detail=detail, cwd_key=key, hostname=_hostname()
    )


def _capture_uncached(
    cwd: str, *, write_token: bool, budget: float, manifest: bool
) -> Capture:
    state, st, why = _path_state(cwd)
    if state != "present" or st is None:
        return _indeterminate(cwd, f"cwd {state}: {why}" if why else f"cwd {state}")
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError) as exc:
        return _indeterminate(cwd, f"cwd unreadable: {exc}")
    cwd_key = object_key(real, st)
    host = _hostname()
    try:
        git = _discover(real)
    except OSError as exc:
        return _indeterminate(real, f"unreadable: {exc}", cwd_key)
    if git is None:
        walk, files, errors = _walk_files(real, None)
        folder = walk.folder
        if folder is None:
            return Capture(
                cwd=real,
                state=OUTSIDE,
                files=files,
                file_errors=errors,
                cwd_key=cwd_key,
                hostname=host,
            )
        root = str(folder.directory)
        return Capture(
            cwd=real,
            state=OUTSIDE,
            kind=FOLDER,
            root=root,
            object_key=object_key(root),
            files=files,
            file_errors=errors,
            manifest=_manifest(root) if manifest else None,
            cwd_key=cwd_key,
            hostname=host,
        )
    git = _real_git(git)
    object_dir = git.git_dir
    common_key = object_key(git.common_dir)
    own_key = object_key(object_dir)
    root_key = object_key(git.root) if git.root is not None else None
    for name in ("HEAD", "config"):
        path = os.path.join(git.git_dir if name == "HEAD" else git.common_dir, name)
        try:
            fst = os.stat(path)
        except OSError:
            continue
        if _evicted(fst) and not _fetch(path, budget):
            # Not on this disk and not fetched within the budget: what stat
            # gives is kept and nothing is read (design 3.1, D19).
            token: str | None = None
            status = "not_read:evicted"
            tpath = str(checkout_token.token_path(git.git_dir))
            try:
                if not _evicted(os.stat(tpath)):
                    read = checkout_token.read(git.git_dir)
                    token = read.token
                    status = "present" if token else status
            except OSError:
                pass
            return Capture(
                cwd=real,
                state=REPO,
                root=git.root or git.git_dir,
                kind=git.kind,
                git_dir=git.git_dir,
                common_dir=git.common_dir,
                object_key=own_key,
                common_object_key=common_key,
                root_object_key=root_key,
                cwd_key=cwd_key,
                token=token,
                token_status=status,
                evicted=True,
                detail=path,
                hostname=host,
            )
    head, branch = _resolve_head(git)
    tips = _ref_tips(git)
    values = _config_values(os.path.join(git.common_dir, "config"))
    base = git.root or git.git_dir
    keys = sorted(
        {
            k
            for (section, _sub, key), urls in values.items()
            if section == "remote" and key == "url"
            for k in (remote_key(u, base) for u in urls)
            if k
        }
    )
    token, written, status = _token_fields(git, write_token=write_token)
    walk_root = git.root if git.root is not None else git.git_dir
    walk, files, errors = _walk_files(real, walk_root)
    born = head is not None or bool(tips)
    tracked: bool | None = None
    root_file = walk.root
    if root_file is not None and head is not None:
        tracked = _file_tracked(git.common_dir, head)
    keep_manifest = manifest and not born and git.root is not None
    return Capture(
        cwd=real,
        state=REPO,
        root=git.root or git.git_dir,
        kind=git.kind,
        git_dir=git.git_dir,
        common_dir=git.common_dir,
        object_key=own_key,
        common_object_key=common_key,
        root_object_key=root_key,
        cwd_key=cwd_key,
        head=head,
        branch=branch,
        ref_tips=tips,
        remote_keys=tuple(keys),
        shallow=os.path.isfile(os.path.join(git.common_dir, "shallow")),
        token=token,
        token_written=written,
        token_status=status,
        stamps=_init_stamps(git.git_dir),
        files=files,
        file_errors=errors,
        file_tracked=tracked,
        manifest=_manifest(git.root) if keep_manifest and git.root else None,
        hostname=host,
    )


def _real_git(git: _Git) -> _Git:
    """The repository's paths resolved through links, as the view and the
    checkout record them."""
    return _Git(
        dotgit=None if git.dotgit is None else os.path.realpath(git.dotgit),
        git_dir=os.path.realpath(git.git_dir),
        common_dir=os.path.realpath(git.common_dir),
        root=None if git.root is None else os.path.realpath(git.root),
        kind=git.kind,
    )


# ---------------------------------------------------------------------------
# The signature and the cache
# ---------------------------------------------------------------------------


def signature(cwd: str) -> tuple[Any, ...] | None:
    """The change detector a capture of `cwd` is kept against, or None when
    no capture there is kept (GIT_DIR and its kin set, an evicted HEAD or
    config, a cwd that cannot be stat'ed). In a repository: the cwd's
    object, the ``.git`` entry and the git directory, HEAD's bytes and
    stamp, the loose ref HEAD names, packed-refs, config, the shallow
    marker, the token, the project files from the cwd to the root, and
    while HEAD is unborn the ref tips themselves. Outside one: the cwd's
    object and its own project file; the discovery walk that found no
    repository runs on every call, so ``git init`` at the cwd or above is
    seen."""
    if any(os.environ.get(v) for v in _DISCOVERY_ENV):
        return None
    try:
        st = os.stat(cwd)
    except (OSError, ValueError):
        return None
    cwd_part = (cwd, st.st_dev, st.st_ino)
    try:
        git = _discover(cwd)
    except OSError:
        return None
    if git is None:
        return (OUTSIDE, cwd_part, _stamp(os.path.join(cwd, _PROJECT_FILE)))
    try:
        head_path = os.path.join(git.git_dir, "HEAD")
        hst = os.stat(head_path)
        if _evicted(hst):
            return None
        try:
            cst: os.stat_result | None = os.stat(os.path.join(git.common_dir, "config"))
        except FileNotFoundError:
            cst = None
        if cst is not None and _evicted(cst):
            return None
        with open(head_path, "rb") as fh:
            head_bytes = fh.read(_HEAD_LIMIT)
    except OSError:
        return None
    ref_part: Any = None
    refs_part: Any = None
    text = head_bytes.decode("utf-8", errors="replace").strip()
    if text.startswith("ref:"):
        name = text[4:].strip()
        per_worktree = name.startswith(
            ("refs/bisect/", "refs/rewritten/", "refs/worktree/")
        )
        base = git.git_dir if per_worktree else git.common_dir
        ref_part = _stamp(os.path.join(base, *name.split("/")))
        if ref_part is None:
            # No loose file for the branch: packed, or unborn. A first
            # branch or remote-tracking ref elsewhere makes an unborn
            # repository born (design 2.1), and adds an entry here.
            refs_part = (
                _stamp(os.path.join(git.common_dir, "refs", "heads")),
                _stamp(os.path.join(git.common_dir, "refs", "remotes")),
            )
    inner: list[Any] = []
    root = git.root if git.root is not None else git.git_dir
    d = cwd if _inside(cwd, root) else root
    while True:
        inner.append(_stamp(os.path.join(d, _PROJECT_FILE)))
        if os.path.normpath(d) == os.path.normpath(root):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return (
        REPO,
        cwd_part,
        git.dotgit,
        None if git.dotgit is None else _stamp(git.dotgit),
        git.git_dir,
        git.common_dir,
        head_bytes,
        (hst.st_mtime_ns, hst.st_ctime_ns, hst.st_size, hst.st_ino),
        ref_part,
        _stamp(os.path.join(git.common_dir, "packed-refs")),
        None
        if cst is None
        else (cst.st_mtime_ns, cst.st_ctime_ns, cst.st_size, cst.st_ino),
        _stamp(os.path.join(git.common_dir, "shallow")),
        (_TOKEN_SLOT, _stamp(str(checkout_token.token_path(git.git_dir)))),
        tuple(inner),
        refs_part,
    )


def _masked(sig: tuple[Any, ...] | None) -> tuple[Any, ...] | None:
    """The signature with the token's slot blanked: a capture that wrote
    the token compares its before and after on everything else."""
    if sig is None:
        return None
    return tuple(
        (_TOKEN_SLOT, None)
        if isinstance(part, tuple) and part and part[0] == _TOKEN_SLOT
        else part
        for part in sig
    )


@dataclass(frozen=True)
class _Entry:
    capture: Capture
    signature: tuple[Any, ...]
    monotonic: float
    wall: float


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, bool, bool], _Entry] = {}


@_caches.register
def _clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def forget(cwd: str | None = None) -> None:
    """Drop the kept captures (of `cwd` only, when given): after a token
    is re-minted, or `bettermemory project init` wrote a project file."""
    with _CACHE_LOCK:
        if cwd is None:
            _CACHE.clear()
        else:
            for key in [k for k in _CACHE if k[0] == cwd]:
                del _CACHE[key]
    if cwd is None:
        _clear_above()


def _fresh(monotonic: float, wall: float, now: float, now_wall: float) -> bool:
    return (
        0.0 <= now - monotonic < CACHE_SECONDS
        and 0.0 <= now_wall - wall < CACHE_SECONDS
    )


def capture(
    cwd: str,
    *,
    write_token: bool,
    budget: float = FETCH_BUDGET,
    manifest: bool = True,
    use_cache: bool = True,
) -> Capture:
    """The capture of `cwd` (design 3.1), from the cache while its
    signature reads as it did and it is younger than `CACHE_SECONDS` on
    both clocks. `write_token` is the policy switch
    (``membership.write_token``); `budget` the seconds a fetch of an
    evicted HEAD or config may take; `manifest` whether the content
    manifest is read (the hook client's first post leaves it out). A
    capture is kept only when the signature read the same before and
    after it (the token's own slot aside when this capture wrote the
    token), never an indeterminate or evicted one."""
    key = (cwd, write_token, manifest)
    before = signature(cwd) if use_cache else None
    now, wall = time.monotonic(), time.time()
    if before is not None:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
        if (
            entry is not None
            and entry.signature == before
            and _fresh(entry.monotonic, entry.wall, now, wall)
        ):
            return entry.capture
    got = _capture_uncached(
        cwd, write_token=write_token, budget=budget, manifest=manifest
    )
    if before is None or got.state == INDETERMINATE or got.evicted:
        return got
    after = signature(cwd)
    if after is None:
        return got
    same = (
        after == before
        if got.token_written is None
        else _masked(after) == _masked(before)
    )
    if not same:
        return got
    kept = got
    if got.token_written is not None:
        # The next capture reads the token this one wrote.
        kept = dataclasses.replace(
            got, token=got.token_written, token_written=None, token_status="present"
        )
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
        _CACHE[key] = _Entry(kept, after, now, wall)
        while len(_CACHE) > CACHE_CAP:
            del _CACHE[next(iter(_CACHE))]
    return got


def capture_token(
    cwd: str, *, write_token: bool, budget: float = FETCH_BUDGET, manifest: bool = True
) -> dict[str, Any]:
    """The location token of `cwd` (`Capture.to_token`): what the hook
    client posts where the daemon cannot read its path."""
    return capture(
        cwd, write_token=write_token, budget=budget, manifest=manifest
    ).to_token()


def probe(cwd: str) -> dict[str, Any]:
    """The hook client's first word on where it stands (design 8.1): the
    cwd resolved, its device and inode, and the host's name, which the
    daemon compares with its own reading of the same path. A stat and a
    ``realpath``; nothing else is read."""
    out: dict[str, Any] = {"v": TOKEN_VERSION, "probe": True, "cwd": cwd}
    state, st, why = _path_state(cwd)
    if state != "present" or st is None:
        out["state"] = INDETERMINATE
        out["detail"] = f"cwd {state}: {why}" if why else f"cwd {state}"
        return out
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError):
        real = cwd
    out["cwd"] = real
    # Device and inode: the daemon reads the path at the same moment, so a
    # reused inode number cannot stand between the two readings, and no
    # birth time is read (statx would load ctypes into every hook).
    out["cwd_object"] = [str(st.st_dev), str(st.st_ino), None]
    out["hostname"] = _hostname()
    return out


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

_GIT_TIMEOUT_SECONDS = 20.0


def _git(
    common_dir: str, args: Sequence[str], stdin: bytes | None = None
) -> bytes | None:
    """`git` against the repository at `common_dir`, with no optional
    locks, hooks or fsmonitor and no configuration of the caller's own
    beyond git's (design 8.8); None when git cannot run or fails."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(var, None)
    try:
        done = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                f"--git-dir={common_dir}",
                *args,
            ],
            input=stdin,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


class RepositoryHistory:
    """The history reader of a capture (`membership.HistoryReader`, design
    3.1): objects read from the repository's files through
    `gitobjects.ObjectReader`, made on first use; ``git cat-file`` only
    where the reader reports itself incomplete (a pack index it could not
    parse, an evicted file, an object format it does not know). Without
    git on PATH an incomplete reader's answer stands."""

    def __init__(self, common_dir: str) -> None:
        self._common = common_dir
        self._reader: gitobjects.ObjectReader | None = None
        self._parents: dict[str, tuple[str, ...]] = {}

    @property
    def reader(self) -> gitobjects.ObjectReader:
        if self._reader is None:
            self._reader = gitobjects.ObjectReader(self._common)
        return self._reader

    def _complete(self) -> bool:
        return self.reader.complete

    def _batch_check(self, ids: Sequence[str]) -> dict[str, tuple[str, str]] | None:
        """{id: (full id, type)} of the ids git holds, by ``git cat-file
        --batch-check``, which answers one line per id asked, in order, the
        full name first (an id it finds absent or ambiguous answers
        ``<id> missing`` or ``<id> ambiguous``). None when git could not be
        run. Should the lines not pair with the ids, only full ids are
        matched, by name."""
        out = _git(
            self._common,
            ["cat-file", "--batch-check"],
            ("\n".join(ids) + "\n").encode(),
        )
        if out is None:
            return None
        lines = out.decode("utf-8", errors="replace").splitlines()
        paired = (
            zip(ids, lines, strict=True)
            if len(lines) == len(ids)
            else ((i, ln) for ln in lines for i in ids if ln.split()[:1] == [i.lower()])
        )
        found: dict[str, tuple[str, str]] = {}
        for given, line in paired:
            parts = line.split()
            if len(parts) >= 2 and parts[1] not in ("missing", "ambiguous"):
                found[given] = (parts[0].lower(), parts[1])
        return found

    @staticmethod
    def _askable(given: str) -> bool:
        """An id git can be asked for: hex, from an abbreviation's shortest
        length to a SHA-256 name's."""
        return gitobjects.MIN_ABBREV <= len(given) <= 64 and all(
            c in "0123456789abcdefABCDEF" for c in given
        )

    def _expand(self, ids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
        """{id: (full id, type or None)} of `expand`; the type is known
        where git answered."""
        reader = self.reader
        length = reader.name_length
        whole = [i for i in ids if len(i) == length]
        short = [i for i in ids if len(i) != length]
        found: dict[str, tuple[str, str | None]] = {}
        held = reader.has_many([i.lower() for i in whole])
        for given in whole:
            if given.lower() in held:
                found[given] = (given.lower(), None)
        for given in short:
            full = reader.resolve(given)
            if full is not None:
                found[given] = (full, None)
        if self._complete():
            return found
        ask = [i for i in [*whole, *short] if i not in found or i in short]
        ask = [i for i in dict.fromkeys(ask) if self._askable(i)]
        told = self._batch_check(ask) if ask else None
        if told is None:
            return found
        # Git sees what the reader could not: its answer stands for an
        # abbreviation (one object, or ambiguous, or absent).
        for given in short:
            if given in told:
                found[given] = told[given]
            else:
                found.pop(given, None)
        for given in whole:
            if given not in found and given in told:
                found[given] = told[given]
        return found

    def expand(self, ids: Sequence[str]) -> dict[str, str]:
        """{id: full id} of the ids of `ids` this repository holds: a full
        id as it is (in lowercase); an abbreviated one (decision B19, as a
        memory's ``verified_commits`` record them) as the full name of the
        one object it names, through the reader's pack indexes and loose
        objects (`gitobjects.ObjectReader.resolve`), or ``git cat-file
        --batch-check`` where the reader is incomplete. An id held nowhere,
        or an abbreviation naming more than one object, is left out."""
        return {given: full for given, (full, _kind) in self._expand(ids).items()}

    def holds(self, commits: Sequence[str]) -> set[str]:
        """The commits of `commits` this repository holds, as given (in
        lowercase): a full id or an abbreviation naming one object
        (`expand`)."""
        return {
            given.lower()
            for given, (_full, kind) in self._expand(commits).items()
            if kind in (None, "commit")
        }

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        """The objects of any type of `ids` this repository holds, as given
        (in lowercase), an abbreviation counting as `expand` counts it."""
        return {given.lower() for given in self._expand(ids)}

    def _commit_body(self, sha: str) -> bytes | None:
        got = self.reader.read(sha)
        if got is not None:
            return got[1] if got[0] == "commit" else None
        if self._complete():
            return None
        return _git(self._common, ["cat-file", "commit", sha])

    def fingerprint(self, commit: str) -> str | None:
        body = self._commit_body(commit.lower())
        return None if body is None else gitobjects.commit_fingerprint(body)

    def parents(self, commit: str) -> Sequence[str]:
        sha = commit.lower()
        hit = self._parents.get(sha)
        if hit is not None:
            return hit
        body = self._commit_body(sha)
        found: tuple[str, ...] = ()
        if body is not None:
            head = body.split(b"\n\n", 1)[0]
            found = tuple(
                line[len(b"parent ") :].decode("ascii", errors="replace").strip()
                for line in head.split(b"\n")
                if line.startswith(b"parent ")
            )
        self._parents[sha] = found
        return found

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether `ancestor` is `descendant` or reachable from it over
        parent lines read from the objects; a shallow boundary ends the
        walk as git's own walk ends it there."""
        a, d = ancestor.lower(), descendant.lower()
        if a == d:
            return True
        seen = {d}
        todo = [d]
        while todo:
            for p in self.parents(todo.pop()):
                if p == a:
                    return True
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        return False

    def blobs(self, commit: str) -> Sequence[str]:
        """Up to `BLOBS_PER_WITNESS` blob ids of the commit's files (the
        root and its first level, at most `BLOB_LOOK` entries looked at),
        of at least `BLOB_MIN_SIZE` bytes by their headers, the project
        file left out (it is its own witness, F): the content a rewrite
        that changes dates or drops a file leaves (Hb)."""
        reader = self.reader
        found = reader.commit(commit.lower())
        if found is None:
            return ()
        out: list[str] = []
        looked = 0
        todo: list[tuple[str, int]] = [(found.tree, 0)]
        skip = _PROJECT_FILE.encode()
        while todo and looked < BLOB_LOOK:
            tree, depth = todo.pop(0)
            entries = reader.tree(tree)
            if entries is None:
                continue
            for entry in entries:
                if looked >= BLOB_LOOK:
                    break
                if stat.S_ISDIR(entry.mode):
                    if depth == 0:
                        todo.append((entry.sha, 1))
                    continue
                if entry.name == skip or not stat.S_ISREG(entry.mode):
                    continue
                looked += 1
                header = reader.header(entry.sha)
                if header is not None and header[1] >= BLOB_MIN_SIZE:
                    out.append(entry.sha)
        return sorted(out)[:BLOBS_PER_WITNESS]


def walk(
    history: Any, head: str | None, ref_tips: Sequence[str]
) -> dict[str, tuple[str | None, tuple[str, ...]]]:
    """{commit: (fingerprint, parents)} for the commits the model's
    rewritten-history walk reads (`membership.Membership._rewritten`):
    from HEAD, or the ref tips when HEAD is unborn, by the same stack
    order, up to `WALK_LIMIT`."""
    out: dict[str, tuple[str | None, tuple[str, ...]]] = {}
    todo: list[str] = [head] if head else list(ref_tips)
    while todo and len(out) < WALK_LIMIT:
        sha = todo.pop()
        if sha in out:
            continue
        parents = tuple(history.parents(sha))
        out[sha] = (history.fingerprint(sha), parents)
        todo.extend(parents)
    return out


# ---------------------------------------------------------------------------
# The witness exchange (design 8.7)
# ---------------------------------------------------------------------------


class NeedWitnesses(Exception):
    """A question the model asked of a repository the daemon cannot read:
    `question` names what the client is to answer."""

    def __init__(self, question: Mapping[str, Any]) -> None:
        super().__init__("the client's repository must answer")
        self.question = dict(question)


class AnsweredHistory:
    """A `membership.HistoryReader` over the client's answers: each
    question it holds an answer for is answered, any other raises
    `NeedWitnesses` naming it, so the daemon reads none of the client's
    paths. The first question for held commits names every witness id the
    caller passed, so one round answers the model's one batch (design
    3.2)."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self._asked = {str(x).lower() for x in answers.get("asked") or ()}
        self._held = {str(x).lower() for x in answers.get("held") or ()}
        raw_walk = answers.get("walk")
        self._walked = isinstance(raw_walk, Mapping)
        self._walk: dict[str, tuple[str | None, tuple[str, ...]]] = {}
        if isinstance(raw_walk, Mapping):
            for sha, pair in raw_walk.items():
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    fp = pair[0] if isinstance(pair[0], str) else None
                    parents = tuple(str(p).lower() for p in pair[1] or ())
                    self._walk[str(sha).lower()] = (fp, parents)
        self._ancestry_of = {str(x).lower() for x in answers.get("ancestry_of") or ()}
        self._ancestry = {
            (str(a).lower(), str(d).lower()) for a, d in (answers.get("ancestry") or ())
        }
        raw_blobs = answers.get("blobs")
        self._blobs: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_blobs, Mapping):
            for sha, ids in raw_blobs.items():
                self._blobs[str(sha).lower()] = tuple(str(i).lower() for i in ids or ())

    def _ids(self, ids: Sequence[str]) -> set[str]:
        wanted = {i.lower() for i in ids}
        missing = sorted(wanted - self._asked)
        if missing:
            raise NeedWitnesses({"held": missing})
        return wanted & self._held

    def holds(self, commits: Sequence[str]) -> set[str]:
        return self._ids(commits)

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        return self._ids(ids)

    def _walk_entry(self, commit: str) -> tuple[str | None, tuple[str, ...]]:
        sha = commit.lower()
        hit = self._walk.get(sha)
        if hit is not None:
            return hit
        raise NeedWitnesses({"walk": True, "commits": [sha]})

    def fingerprint(self, commit: str) -> str | None:
        return self._walk_entry(commit)[0]

    def parents(self, commit: str) -> Sequence[str]:
        return self._walk_entry(commit)[1]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        a, d = ancestor.lower(), descendant.lower()
        if a == d:
            return True
        if a in self._ancestry_of and d in self._ancestry_of:
            return (a, d) in self._ancestry
        raise NeedWitnesses({"ancestry": sorted(self._ancestry_of | {a, d})})

    def blobs(self, commit: str) -> Sequence[str]:
        sha = commit.lower()
        hit = self._blobs.get(sha)
        if hit is not None:
            return hit
        raise NeedWitnesses({"blobs": [sha]})


class AnsweredLiveness:
    """The `membership.Liveness` of a caller the daemon cannot read: each
    view's state as the client answered it (`view_state` in its own
    namespace); a view not answered raises `NeedWitnesses`."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        raw = answers.get("views")
        self._states: dict[str, str] = {}
        if isinstance(raw, Mapping):
            self._states = {str(k): str(v) for k, v in raw.items()}

    def __call__(self, checkout: Checkout, view: View) -> str:
        hit = self._states.get(view.path)
        if hit is not None:
            return hit
        key = checkout.object_key
        raise NeedWitnesses(
            {
                "views": [
                    {
                        "path": view.path,
                        "kind": checkout.kind,
                        "object": None
                        if key is None
                        else [key.device, key.inode, key.birth],
                        "token": checkout.token,
                        "last_head": checkout.last_head,
                        "stamps": [list(s) for s in checkout.stamps],
                        "common_object": None
                        if checkout.common_object_key is None
                        else [
                            checkout.common_object_key.device,
                            checkout.common_object_key.inode,
                            checkout.common_object_key.birth,
                        ],
                    }
                ]
            }
        )


def _git_dir_at(path: str, kind: str) -> str | None:
    if kind == BARE:
        return path
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    return _gitfile_target(dotgit)


def continuity(
    token: str | None,
    last_head: str | None,
    stamps: Iterable[Sequence[Any]],
    *,
    token_here: str | None,
    head_here: str | None,
    stamps_here: Iterable[Sequence[Any]],
    holds: Callable[[Sequence[str]], Collection[str]],
    confirms: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Witness O where birth time cannot decide (design 2.2, decision
    B18): an object with the recorded device and inode number continues
    the recorded checkout when the token there is the checkout's; without
    a recorded token, when the checkout's last HEAD is HEAD there or is
    held there (`holds`, asked only then); commit-less and token-less,
    when an init stamp is equal (amendment A2: equality, no clock); where
    none of those can be read (a folder project has none), when another
    witness confirms the same object (`confirms`: F for a folder project,
    S or H for a repository; decision B20). Returns whether it continues
    and the witness that decided (``token``, ``last_head``,
    ``init_stamps``, ``same_object`` or ``nothing_observable``). Rung 2
    of the model (`membership.Membership._by_object`) and the liveness of
    a view (`view_state`) both apply this one rule."""
    if token:
        return token_here == token, "token"
    if last_head is not None:
        held = last_head == head_here or bool(holds([last_head]))
        return held, "last_head"
    mine = {tuple(s) for s in stamps}
    there = {tuple(s) for s in stamps_here}
    if mine and there:
        return bool(mine & there), "init_stamps"
    if confirms is not None and confirms():
        return True, "same_object"
    return False, "nothing_observable"


def _head_at(git_dir: str, common: str, root: str | None) -> str | None:
    """The commit HEAD names in the git directory at a view's path."""
    try:
        head, _branch = _resolve_head(_Git(None, git_dir, common, root, PRIMARY))
    except (OSError, ValueError):
        return None
    return head


def view_state(
    path: str,
    kind: str,
    key: Key | None,
    token: str | None,
    *,
    last_head: str | None = None,
    stamps: Iterable[Sequence[Any]] = (),
    common_key: Key | None = None,
) -> str:
    """What `path` holds now, read in this namespace: ``live`` (the
    checkout's object: its birth time, or with no birth time on one side
    `continuity` over the token, the last HEAD and the init stamps read
    there against the checkout's, and where none of those can be read the
    folder's project file (F) or a linked worktree's object store (S, the
    recorded `common_key`) confirming the same object, decision B20),
    ``live_legacy`` (a repository at a legacy checkout's path),
    ``replaced``, ``gone`` or ``unreachable``. `path_liveness` reads a
    view with it, and the client answers the daemon's view questions with
    it."""
    state, _st, _why = _path_state(path)
    if state == "gone":
        return "gone"
    if state != "present":
        return "unreachable"
    if kind == FOLDER:
        here = object_key(path)
        git_dir = None
    else:
        git_dir = _git_dir_at(path, kind)
        here = object_key(git_dir) if git_dir is not None else None
    if here is None:
        return "replaced"
    if key is None:
        return "live_legacy" if git_dir is not None else "replaced"
    if key[:2] != here[:2]:
        return "replaced"
    if key[2] is not None and here[2] is not None:
        return "live" if key[2] == here[2] else "replaced"
    if git_dir is None:
        # A folder project: nothing but its file confirms its object.
        ok, _how = continuity(
            None,
            None,
            (),
            token_here=None,
            head_here=None,
            stamps_here=(),
            holds=lambda _commits: set(),
            confirms=lambda: _carries_project_file(path),
        )
        return "live" if ok else "replaced"
    common, linked = _common_of(git_dir)
    ok, _how = continuity(
        token,
        last_head,
        stamps,
        token_here=checkout_token.read(git_dir).token,
        head_here=_head_at(git_dir, common, None if kind == BARE else path),
        stamps_here=_init_stamps(git_dir),
        holds=RepositoryHistory(common).holds,
        confirms=lambda: linked and _same_number(object_key(common), common_key),
    )
    return "live" if ok else "replaced"


def _carries_project_file(folder: str) -> bool:
    """Whether `folder` holds a project file that names a project (F)."""
    found = project_file.parse(Path(folder) / _PROJECT_FILE)
    return isinstance(found, project_file.ProjectFile)


def _same_number(a: Key | None, b: Key | None) -> bool:
    """Two objects with one device and inode number (birth aside)."""
    return a is not None and b is not None and a[:2] == b[:2]


def path_liveness(checkout: Checkout, view: View) -> str:
    """The `membership.Liveness` of this process's namespace, which the
    model reads by default: what the view's path holds now (`view_state`)
    against the checkout's recorded object, token, last HEAD and init
    stamps, so a view is live by the same witness O that keeps the
    checkout at rung 2 (design 2.2, decision B18)."""
    key = checkout.object_key
    common = checkout.common_object_key
    return view_state(
        view.path,
        checkout.kind,
        None if key is None else (key.device, key.inode, key.birth),
        checkout.token,
        last_head=checkout.last_head,
        stamps=checkout.stamps,
        common_key=None
        if common is None
        else (common.device, common.inode, common.birth),
    )


def _asked_head(value: Any) -> str | None:
    """A last HEAD a view question names, when it is a full hash."""
    if isinstance(value, str) and _FULL_HASH.fullmatch(value.lower()):
        return value.lower()
    return None


def _asked_stamps(value: Any) -> tuple[tuple[str, int, int, int, int], ...]:
    """The init stamps a view question names, the malformed ones left out."""
    out: list[tuple[str, int, int, int, int]] = []
    if isinstance(value, list):
        for s in value[:8]:
            if not isinstance(s, (list, tuple)) or len(s) != 5:
                continue
            try:
                out.append((str(s[0]), int(s[1]), int(s[2]), int(s[3]), int(s[4])))
            except (TypeError, ValueError):
                continue
    return tuple(out)


def answer(
    question: Mapping[str, Any], answers: Mapping[str, Any], cap: Capture
) -> dict[str, Any]:
    """The client's answers to a daemon's `question` about its repository
    (design 8.7), added to the `answers` of earlier rounds: which of the
    named ids it holds, the commits of the model's rewritten-history walk
    with their fingerprints and parents, the ancestry among the held
    witnesses, the blob sample of HEAD and the asked commits, and the
    state of the named views, all read from its own files."""
    out: dict[str, Any] = {k: v for k, v in answers.items()}
    history = (
        RepositoryHistory(cap.common_dir)
        if cap.common_dir and cap.state == REPO
        else None
    )
    asked = set(out.get("asked") or ())
    held = set(out.get("held") or ())
    wanted = [str(x).lower() for x in question.get("held") or ()]
    if wanted:
        got = history.holds_objects(wanted) if history is not None else set()
        asked |= set(wanted)
        held |= got
    out["asked"] = sorted(asked)
    out["held"] = sorted(held)
    if history is not None:
        if "walk" not in out or question.get("walk"):
            walked = dict(out.get("walk") or {})
            for sha, (fp, parents) in walk(history, cap.head, cap.ref_tips).items():
                walked[sha] = [fp, list(parents)]
            for sha in question.get("commits") or ():
                s = str(sha).lower()
                walked[s] = [history.fingerprint(s), list(history.parents(s))]
            out["walk"] = walked
        among = sorted(held | {str(x).lower() for x in question.get("ancestry") or ()})
        among = [s for s in among if _FULL_HASH.fullmatch(s)]
        out["ancestry_of"] = among
        out["ancestry"] = [
            [a, d] for a in among for d in among if a != d and history.is_ancestor(a, d)
        ]
        blobs = dict(out.get("blobs") or {})
        for sha in [cap.head, *(question.get("blobs") or ())]:
            if sha:
                s = str(sha).lower()
                blobs[s] = list(history.blobs(s))
        out["blobs"] = blobs
    else:
        out.setdefault("walk", {})
    views = dict(out.get("views") or {})
    for v in question.get("views") or ():
        if isinstance(v, Mapping) and isinstance(v.get("path"), str):
            views[v["path"]] = view_state(
                v["path"],
                str(v.get("kind") or PRIMARY),
                _key(v.get("object")),
                v.get("token"),
                last_head=_asked_head(v.get("last_head")),
                stamps=_asked_stamps(v.get("stamps")),
                common_key=_key(v.get("common_object")),
            )
    out["views"] = views
    return out


@dataclass(frozen=True)
class Exchange:
    """The daemon's reply to a hook client it cannot read for: what it
    asks next (the location token, the machine, or the answers to a
    question), and the token policy the client's capture writes under."""

    question: Mapping[str, Any] = field(default_factory=dict)

    def to_reply(self) -> dict[str, Any]:
        return dict(self.question)


# ---------------------------------------------------------------------------
# To the model
# ---------------------------------------------------------------------------


def to_location(
    cap: Capture,
    *,
    machine: str,
    session: str | None = None,
    history: Any = None,
) -> Location:
    """The `membership.Location` of a capture, for `machine`, with
    `history` as its reader."""
    from . import membership as mb

    def key(k: Key | None) -> mb.ObjectKey | None:
        return None if k is None else mb.ObjectKey(k[0], k[1], k[2])

    return mb.Location(
        machine=machine,
        cwd=cap.cwd,
        state=cap.state,
        root=cap.root,
        kind=cap.kind,
        git_dir=cap.git_dir,
        object_key=key(cap.object_key),
        common_object_key=key(cap.common_object_key),
        root_object_key=key(cap.root_object_key),
        head=cap.head,
        branch=cap.branch,
        ref_tips=cap.ref_tips,
        remote_keys=frozenset(cap.remote_keys),
        shallow=cap.shallow,
        token=cap.token,
        token_written=cap.token_written,
        token_status=cap.token_status,
        stamps=tuple(mb.InitStamp(*s) for s in cap.stamps),
        files=tuple(
            mb.ProjectFileRef(
                id=f.id,
                name=f.name,
                dir=f.dir,
                path=f.path,
                boundary=f.boundary,
                object_key=key(f.key),
            )
            for f in cap.files
        ),
        file_tracked=cap.file_tracked,
        evicted=cap.evicted,
        detail=cap.detail,
        manifest=(
            None
            if cap.manifest is None
            else mb.Manifest(name=cap.manifest[0], entries=cap.manifest[1])
        ),
        session=session,
        history=history,
    )


def _history_of(cap: Capture) -> RepositoryHistory | None:
    if cap.state != REPO or cap.evicted or not cap.common_dir:
        return None
    return RepositoryHistory(cap.common_dir)


def capture_location(
    cwd: str,
    *,
    machine: str,
    session: str | None,
    config: Config,
    budget: float = FETCH_BUDGET,
) -> Location:
    """The caller's `membership.Location` at `cwd` (design 3.1): the
    capture, under the token policy of `config`, for `machine`, with a
    history reader over the repository's files."""
    cap = capture(cwd, write_token=config.membership.write_token, budget=budget)
    return to_location(cap, machine=machine, session=session, history=_history_of(cap))


# ---------------------------------------------------------------------------
# The machine, once per process
# ---------------------------------------------------------------------------

_MACHINE_LOCK = threading.Lock()
_MACHINE: list[MachineIdentity] = []
_RECORDED: set[tuple[str, str]] = set()


@_caches.register
def _clear_machine() -> None:
    with _MACHINE_LOCK:
        _MACHINE.clear()
        _RECORDED.clear()


def process_machine() -> MachineIdentity:
    """This process's machine, opened once (`machine.open_machine`,
    decision D16): the daemon and each CLI process compare the facts at
    their open, and every resolution of the process uses that id."""
    with _MACHINE_LOCK:
        if not _MACHINE:
            from .machine import open_machine

            _MACHINE.append(open_machine())
        return _MACHINE[0]


def _recorded(store: Store, model: Membership, identity: MachineIdentity) -> None:
    """`Membership.record_machine` before the first resolution of this
    machine in this store, once per process."""
    key = (str(store.path), identity.id)
    with _MACHINE_LOCK:
        if key in _RECORDED:
            return
    model.record_machine(identity)
    with _MACHINE_LOCK:
        _RECORDED.add(key)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def _location_lock(loc: Location) -> Iterator[None]:
    """The per-location lock of design 3.1 (E28): resolutions of one
    location in this process run one at a time, each inside the model's
    own ``BEGIN IMMEDIATE`` transaction, which re-checks the object key, so
    a process beside this one is ordered by the store's lock."""
    if loc.object_key is not None:
        name = f"{loc.machine}:{loc.object_key.device}:{loc.object_key.inode}"
    else:
        name = f"{loc.machine}:{loc.root or loc.cwd}"
    with _LOCKS_GUARD:
        lock = _LOCKS.get(name)
        if lock is None:
            if len(_LOCKS) >= CACHE_CAP * 4:
                for stale in [k for k, v in _LOCKS.items() if not v.locked()]:
                    del _LOCKS[stale]
            lock = _LOCKS[name] = threading.Lock()
    with lock:
        yield


def _cwd_of(cwd: str | os.PathLike[str] | None) -> str | None:
    if cwd is not None:
        return os.fspath(cwd)
    try:
        return os.getcwd()
    except OSError:
        return None


def _model(
    store: Store,
    identity: MachineIdentity,
    session: str | None,
    liveness: Any = None,
) -> Membership:
    from .membership import Membership

    model = Membership(store, identity.id, liveness=liveness, session=session)
    _recorded(store, model, identity)
    return model


@dataclass(frozen=True)
class Caller:
    """A resolved caller: the location read, its resolution and the model
    that decided it (whose rows a write in the same transaction joins)."""

    location: Location
    resolution: Resolution
    model: Membership

    def home(self) -> RecordHome:
        """The home of a record written now (design 2.1), inside the
        record's own transaction: the resolution's, or where nothing could
        be read or the read was deferred, the provisional project of the
        unresolved location (`Membership.unresolved_home`, decision B6),
        never global and never refused."""
        from .membership import DEFERRED, INDETERMINATE

        res = self.resolution
        if res.outcome in (INDETERMINATE, DEFERRED) or res.home is None:
            return self.model.unresolved_home(
                self.location, session=self.location.session
            )
        return res.home

    def add_head_witness(self, source: str) -> None:
        """HEAD at a write (or a verify) as a witness commit of the
        caller's project (design 2.1), with its fingerprint and blob
        sample, inside the record's own transaction."""
        from .membership import GLOBAL

        res, loc = self.resolution, self.location
        if res.project is None or res.project == GLOBAL or not loc.head:
            return
        self.model.add_witness(
            res.project,
            loc.head,
            source=f"{source}@{res.checkout}" if res.checkout else source,
            history=loc.history,
        )


# ---------------------------------------------------------------------------
# Where each harness session stands (decision B24)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stand:
    """A harness session's latest hook resolution: the machine it was
    resolved under, the path it was resolved at and the resolution."""

    machine: str
    path: str
    resolution: Resolution


_STANDS_LOCK = threading.Lock()
_STANDS: dict[tuple[str, str], _Stand] = {}
#: The most sessions whose stand the process keeps; the oldest goes first.
STANDS_CAP = CACHE_CAP * 4


@_caches.register
def _clear_stands() -> None:
    with _STANDS_LOCK:
        _STANDS.clear()


def _keep_stand(
    store: Store, session: str | None, loc: Location, res: Resolution
) -> None:
    """Keep `res` as the latest hook resolution of `session` in `store`,
    refreshed at every hook resolution; one without a checkout leaves the
    session no stand."""
    if session is None:
        return
    key = (str(store.path), session)
    with _STANDS_LOCK:
        _STANDS.pop(key, None)
        if res.checkout is None:
            return
        while len(_STANDS) >= STANDS_CAP:
            del _STANDS[next(iter(_STANDS))]
        _STANDS[key] = _Stand(loc.machine, loc.cwd, res)


def _stand_of(store: Store, session: str | None) -> _Stand | None:
    if session is None:
        return None
    with _STANDS_LOCK:
        return _STANDS.get((str(store.path), session))


def forget_session(session: str) -> None:
    """Drop what the process keeps of where `session` stands (decision
    B24): its writes where nothing can be read are then homed as decision
    B6 homes them, until its next hook resolution."""
    with _STANDS_LOCK:
        for key in [k for k in _STANDS if k[1] == session]:
            del _STANDS[key]


def resolve_caller(
    store: Store,
    config: Config,
    cwd: str | os.PathLike[str] | None,
    *,
    session: str | None = None,
    identity: MachineIdentity | None = None,
    budget: float = FETCH_BUDGET,
) -> Caller:
    """The resolution of a caller standing at `cwd` in this process's
    namespace, with what decided it. `cwd` None is a caller with no
    location (nothing readable, I20). The machine is opened once per
    process and recorded before any resolution; the location is captured
    (cached against its signature); the ladder runs under the per-location
    lock in one ``BEGIN IMMEDIATE`` transaction (design 3.1, E28); a token
    is re-minted where the resolution asks for one (design 3.3, E21 g).
    The lines of project files that name no project (decision B1) join
    the resolution's lines. `session` is the harness session id the
    caller carries (decision B6); `identity` overrides the process's
    machine. Where nothing can be read and the session's latest hook
    resolution has a checkout with a view at that path or above it, the
    caller stands in that checkout (decision B24,
    `Membership.session_resolution`)."""
    return _caller(
        store,
        config,
        cwd,
        session=session,
        identity=identity,
        budget=budget,
        stand=_stand_of(store, session),
    )


def _caller(
    store: Store,
    config: Config,
    cwd: str | os.PathLike[str] | None,
    *,
    session: str | None,
    identity: MachineIdentity | None,
    budget: float,
    stand: _Stand | None,
) -> Caller:
    """`resolve_caller`, with the session's `stand` (None at a hook's own
    resolution, which is what the stand is refreshed from)."""
    from .membership import INDETERMINATE as _UNREADABLE
    from .membership import Location as _Location

    if identity is None:
        try:
            identity = process_machine()
        except OSError as exc:
            return _machineless(store, config, cwd, session, exc, stand)
    model = _model(store, identity, session)
    errors: tuple[str, ...] = ()
    if cwd is None:
        loc: Location = _Location(
            machine=identity.id,
            cwd=NO_CWD,
            state=_UNREADABLE,
            detail="the caller has no working directory",
            session=session,
        )
        where = None
    else:
        where = os.fspath(cwd)
        cap = capture(where, write_token=config.membership.write_token, budget=budget)
        errors = cap.file_errors
        loc = to_location(
            cap, machine=identity.id, session=session, history=_history_of(cap)
        )
    with _location_lock(loc):
        res = _resolved(model, loc, config, stand)
        if res.remint_token and res.checkout is not None and loc.git_dir:
            wrote = checkout_token.remint(
                loc.git_dir,
                policy=config.membership.write_token,
                bare=loc.kind == BARE,
            )
            model.token_written(res.checkout, wrote.token, wrote.status)
            if where is not None:
                forget(where)
            res = dataclasses.replace(res, remint_token=False)
    if errors:
        res = dataclasses.replace(
            res, lines=(*res.lines, *(e for e in errors if e not in res.lines))
        )
    return Caller(loc, res, model)


def _resolved(
    model: Membership, loc: Location, config: Config, stand: _Stand | None
) -> Resolution:
    """The ladder's resolution of `loc`, or where nothing could be read
    there, the session's checkout that `stand` names when a view of it
    holds the path."""
    if stand is not None:
        res = model.session_resolution(loc, stand.resolution, stand.path)
        if res is not None:
            return res
    return model.resolve(loc, strict=config.membership.strict)


def _machineless(
    store: Store,
    config: Config,
    cwd: str | os.PathLike[str] | None,
    session: str | None,
    error: OSError,
    stand: _Stand | None,
) -> Caller:
    """A caller in a process that could not open this machine's state
    directory: without the machine nothing where it stands can be keyed,
    so its location is indeterminate (decision B6), keyed on `NO_MACHINE`
    and the cwd it gave, and a write there is homed in that location's
    provisional project, never refused and never global."""
    from .membership import INDETERMINATE as _UNREADABLE
    from .membership import Location as _Location
    from .membership import Membership

    loc = _Location(
        machine=NO_MACHINE,
        cwd=NO_CWD if cwd is None else os.fspath(cwd),
        state=_UNREADABLE,
        detail="this machine's state directory could not be opened: "
        f"{error.strerror or error}",
        session=session,
    )
    model = Membership(store, NO_MACHINE, session=session)
    with _location_lock(loc):
        res = _resolved(model, loc, config, stand)
    return Caller(loc, res, model)


def resolve_here(
    store: Store,
    config: Config,
    cwd: str | os.PathLike[str] | None,
    *,
    session: str | None = None,
    identity: MachineIdentity | None = None,
    budget: float = FETCH_BUDGET,
) -> Resolution:
    """The resolution of a caller standing at `cwd` in this process's
    namespace (the process cwd when None; `resolve_caller`)."""
    where = _cwd_of(cwd)
    return resolve_caller(
        store, config, where, session=session, identity=identity, budget=budget
    ).resolution


def _same_namespace(cap_cwd: str, key: Key | None, hostname: str | None) -> bool:
    """Whether this process reads the object the client read at its cwd,
    on the host the client named: then the client's paths are this
    process's and the daemon captures there itself."""
    if key is None:
        return False
    here = object_key(cap_cwd)
    if here is None or here[:2] != key[:2]:
        return False
    if here[2] is not None and key[2] is not None and here[2] != key[2]:
        return False
    return hostname is None or hostname == _hostname()


def _identity_from(raw: Any) -> MachineIdentity | None:
    """The client's machine as it posted it (`machine.MachineIdentity`)."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
        return None
    from .machine import MachineFacts, MachineIdentity

    facts = MachineFacts.from_dict(raw.get("facts"))
    if facts is None:
        return None
    predecessor = raw.get("predecessor")
    return MachineIdentity(
        id=raw["id"],
        facts=facts,
        predecessor=predecessor if isinstance(predecessor, str) else None,
        minted=bool(raw.get("minted")),
        cloned=bool(raw.get("cloned")),
        facts_updated=bool(raw.get("facts_updated")),
        recorded=MachineFacts.from_dict(raw.get("recorded")),
    )


def identity_to_json(identity: MachineIdentity) -> dict[str, Any]:
    """A machine identity as the hook client posts it."""
    return {
        "id": identity.id,
        "facts": identity.facts.to_dict(),
        "predecessor": identity.predecessor,
        "minted": identity.minted,
        "cloned": identity.cloned,
        "facts_updated": identity.facts_updated,
        "recorded": None if identity.recorded is None else identity.recorded.to_dict(),
    }


def resolve_hook(
    store: Store,
    config: Config,
    cwd: str | os.PathLike[str] | None,
    posted: Mapping[str, Any] | None,
    *,
    session: str | None = None,
) -> Resolution | Exchange:
    """The resolution of a hook's caller from what it posted (design 8.1,
    8.7). `posted` holds the request's ``location`` and, in later rounds,
    ``machine`` and ``answers``. With no location (an rc3 client) the
    daemon captures at `cwd`; where it reads the client's object at the
    posted cwd on the same host, it captures there itself. Otherwise the
    client's namespace is not this one: the daemon asks for the location
    token and the client's machine, then resolves from the token with the
    client answering each question of its repository (an `Exchange`
    names the next one); a re-mint is the client's to make
    (`Resolution.remint_token` stays set). Each resolution is kept as the
    session's latest for the process's lifetime (decision B24), until the
    session is forgotten (`forget_session`) or crowded out."""
    raw = posted.get("location") if posted is not None else None
    if not isinstance(raw, Mapping):
        return _hooked(store, config, _cwd_of(cwd), session)
    if raw.get("state") == INDETERMINATE and raw.get("probe"):
        # The client could not read its own cwd: nothing readable (I20).
        from .membership import INDETERMINATE as _UNREADABLE
        from .membership import Location as _Location

        try:
            model = _model(store, process_machine(), session)
        except OSError:
            from .membership import Membership

            model = Membership(store, NO_MACHINE, session=session)
        detail = raw.get("detail")
        unread = _Location(
            machine=model.machine,
            cwd=str(raw.get("cwd") or cwd or ""),
            state=_UNREADABLE,
            detail=detail if isinstance(detail, str) else "cwd unreadable",
            session=session,
        )
        res = model.resolve(unread, strict=config.membership.strict)
        _keep_stand(store, session, unread, res)
        return res
    try:
        posted_key = _key(raw.get("cwd_object"))
    except ValueError:
        posted_key = None
    posted_cwd = raw.get("cwd")
    hostname = raw.get("hostname")
    if (
        isinstance(posted_cwd, str)
        and posted_cwd
        and _same_namespace(
            posted_cwd, posted_key, hostname if isinstance(hostname, str) else None
        )
    ):
        return _hooked(store, config, posted_cwd, session)
    policy = config.membership.write_token
    cap = None if raw.get("probe") else Capture.from_token(raw)
    identity = _identity_from(posted.get("machine") if posted is not None else None)
    if cap is None or identity is None:
        return Exchange({"location": True, "machine": True, "write_token": policy})
    answers = posted.get("answers") if posted is not None else None
    answers = answers if isinstance(answers, Mapping) else {}
    model = _model(store, identity, session, liveness=AnsweredLiveness(answers))
    loc = to_location(
        cap, machine=identity.id, session=session, history=AnsweredHistory(answers)
    )
    try:
        with _location_lock(loc):
            res = model.resolve(loc, strict=config.membership.strict)
    except NeedWitnesses as need:
        return Exchange({**need.question, "write_token": policy})
    if cap.file_errors:
        res = dataclasses.replace(
            res,
            lines=(*res.lines, *(e for e in cap.file_errors if e not in res.lines)),
        )
    _keep_stand(store, session, loc, res)
    return res


def _hooked(
    store: Store, config: Config, cwd: str | None, session: str | None
) -> Resolution:
    """A hook's resolution of a caller this process reads itself, kept as
    the session's latest."""
    caller = _caller(
        store,
        config,
        cwd,
        session=session,
        identity=None,
        budget=FETCH_BUDGET,
        stand=None,
    )
    _keep_stand(store, session, caller.location, caller.resolution)
    return caller.resolution
