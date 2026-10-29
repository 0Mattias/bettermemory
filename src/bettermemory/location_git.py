"""Git and capture readers for `location` (design 3.1).

The vocabulary and bounds a capture shares with the model, git's
discovery from a directory and its config reader, the `Capture` model and
its token codec, the HEAD, ref, stamp and manifest readers, and the
history reader (`RepositoryHistory`, `walk`). `location` keeps the
capture, its cache, the witness exchange and the resolution, and
re-exports every name here that a caller reads, so no import path changes.

These readers live apart from `location` to keep each module under the
package's 2,000-line bound (ENG-96); `location` imports this module,
never the reverse. The hook client loads it with `location`, so it
imports only the standard library and the readers `_caches`, `_git`,
`_location_probe`, `githead`, `gitobjects` and `project_file`, which
import the standard library and each other alone; never `location`
itself, the model, the store or the config
(tests/test_membership_capture.py pins that set).
"""

from __future__ import annotations

import os
import re
import stat
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _caches, githead, gitobjects, project_file
from . import _git as _git_runner
from ._location_probe import INDETERMINATE, TOKEN_VERSION

# -- vocabulary (equal to membership's; tests/test_membership_capture.py
# pins each pair) ---------------------------------------------------------

REPO = "repo"
OUTSIDE = "outside"
# INDETERMINATE is `_location_probe`'s, imported above: the probe a hook
# posts without this module says it too.
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
# TOKEN_VERSION, the location token's format version, is `_location_probe`'s.
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
# Git config files
# ---------------------------------------------------------------------------


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


def _small_text(path: str) -> str | None:
    """A small text file's content, stripped; None when it cannot be read
    or holds more than `_GITFILE_LIMIT` bytes."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_GITFILE_LIMIT + 1)
    except OSError:
        return None
    if len(data) > _GITFILE_LIMIT:
        return None
    return data.decode("utf-8", errors="replace").strip()


def _names_here(raw: str, base: str, here: str) -> bool | None:
    """Whether the path `raw` (taken from `base` when relative) names the
    entry `here`: True when it is the same file, False when it names
    another one, None when it names nothing (what it named was moved or
    deleted since git wrote it)."""
    path = raw if os.path.isabs(raw) else os.path.join(base, raw)
    try:
        theirs = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        return False
    try:
        return os.path.samestat(theirs, os.stat(here))
    except (OSError, ValueError):
        return False


def gitfile_refusal(directory: str, dotgit: str, target: str) -> str | None:
    """Why the ``.git`` file `dotgit` in `directory`, which names the git
    directory `target`, is not followed (ENG-158), or None when it is.

    A ``.git`` file is followed where git's own pointers say the folder
    is that git directory's working tree: a linked worktree's git
    directory (``<common>/worktrees/<name>``) whose ``gitdir`` file names
    this ``.git`` file, a git directory whose ``core.worktree`` names this
    folder (a submodule's, under the enclosing repository's ``modules/``),
    and a git directory kept apart by ``--separate-git-dir``, for which
    git writes no pointer back (design I22: a second tree naming it is the
    same checkout at a second path). A pointer that names nothing any more
    (the worktree or submodule moved by a plain ``mv``, which ``git
    worktree repair`` mends) is followed too. Anything else is refused:
    another working tree's own ``.git``, a worktree's git directory that
    names another worktree, a git directory whose ``core.worktree`` names
    another folder, and a bare repository. Followed, such a file would key
    the folder on another checkout's git directory, and an extracted
    archive or a cloned repository that planted one would be delivered
    that checkout's memories.

    Each is told by the filesystem object, not the spelling: a target
    that is the ``.git`` of the folder above it, through a symlink or in
    another case on a case-insensitive volume, is that tree's own; a
    ``commondir`` is trusted only for a git directory registered under it
    (``<common>/worktrees/<name>``, where git keeps a linked worktree's),
    so a forged worktree directory names no common directory."""
    _common, linked = _common_of(target)
    named = None if linked else _commondir(target)
    if named is not None:
        return f"{target} names the common directory {named} but is not one of its worktrees"
    if linked:
        back = _small_text(os.path.join(target, "gitdir"))
        if not back:
            return f"{target} is a worktree's git directory that names no worktree"
        if _names_here(back, target, dotgit) is False:
            return f"{target} is the git directory of the worktree at {back}"
        return None
    values = _config_values(os.path.join(target, "config"))
    listed = values.get(("core", None, "worktree")) or []
    configured = listed[-1] if listed else ""
    if configured:
        if _names_here(configured, target, directory) is False:
            return f"{target} is the git directory of the working tree {configured}"
        return None
    if _truthy(values.get(("core", None, "bare"))):
        return f"{target} is a bare repository"
    tree = _tree_of(target)
    if tree is not None:
        return f"{target} is the git directory of the working tree {tree}"
    return None


def _tree_of(git_dir: str) -> str | None:
    """The folder whose own ``.git`` the git directory `git_dir` is, or
    None: compared as filesystem objects, so a symlink to it or a spelling
    in another case on a case-insensitive volume is the same ``.git``."""
    try:
        real = os.path.realpath(git_dir)
        parent = os.path.dirname(real)
        if os.path.samestat(os.stat(real), os.stat(os.path.join(parent, ".git"))):
            return parent
    except (OSError, ValueError):
        pass
    return None


def _common_of(git_dir: str) -> tuple[str, bool]:
    """(common directory, whether a ``commondir`` file named it): the one
    GIT_COMMON_DIR names, else the one `git_dir`'s ``commondir`` file names
    where `git_dir` is registered under it (`_registered`), else `git_dir`
    itself. A ``commondir`` elsewhere is a forged worktree directory's
    (ENG-158): followed, it keyed a folder on another repository's object
    store."""
    env = os.environ.get("GIT_COMMON_DIR")
    if env:
        return os.path.normpath(os.path.join(git_dir, env)), True
    common = _commondir(git_dir)
    if common is None or not _registered(git_dir, common):
        return git_dir, False
    return common, True


def _commondir(git_dir: str) -> str | None:
    """The common directory `git_dir`'s ``commondir`` file names, or None."""
    try:
        with open(os.path.join(git_dir, "commondir"), "rb") as fh:
            rel = fh.read(_GITFILE_LIMIT).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not rel:
        return None
    return os.path.normpath(os.path.join(git_dir, rel))


def _registered(git_dir: str, common: str) -> bool:
    """Whether `git_dir` sits at ``<common>/worktrees/<name>``, the only
    place git keeps a linked worktree's git directory (``git worktree
    add``), compared as filesystem objects."""
    try:
        parent = os.path.dirname(os.path.realpath(git_dir))
        return os.path.samefile(parent, os.path.join(common, "worktrees"))
    except (OSError, ValueError):
        return False


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
    if gitfile_refusal(directory, dotgit, target) is not None:
        # Not followed (ENG-158): read as a `.git` file that names nothing,
        # so no other checkout's git directory keys this folder.
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


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

_GIT_TIMEOUT_SECONDS = 20.0


def _git(
    common_dir: str, args: Sequence[str], stdin: bytes | None = None
) -> bytes | None:
    """`git` against the repository at `common_dir` through `_git.run_git`:
    no optional locks, hooks or fsmonitor and no configuration of the
    caller's own beyond git's (design 8.8), raw bytes, within
    `_GIT_TIMEOUT_SECONDS`; None when git cannot run (logged there at
    WARNING, a hang against the bound included) or fails (at DEBUG)."""
    done = _git_runner.run_git(
        args, git_dir=common_dir, stdin=stdin, timeout=_GIT_TIMEOUT_SECONDS, text=False
    )
    if done is None:
        return None
    if done.returncode != 0:
        _git_runner.log_nonzero_exit(args, done, common_dir)
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

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        """`reachable_among` over this repository's parent lines (ENG-151):
        one bounded traversal, a shallow boundary ending it as git's own
        walk ends there."""
        return reachable_among(self, commits)

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


def reachable_among(
    history: Any, commits: Sequence[str], *, limit: int = WALK_LIMIT
) -> dict[str, frozenset[str]]:
    """{commit: the other commits of `commits` it is reachable from}, for
    each full commit id given (lowercase; any other id is left out): the
    given commits each one is an ancestor of, which the most-specific rule
    reads (`membership.Membership._most_specific`) and a client answers the
    daemon's ancestry question with (`location.answer`).

    One traversal of the parent lines from every given commit at once,
    breadth first, each commit carrying the set of given commits that
    reach it; the parents of a commit are read once, and of at most
    `limit` commits (`WALK_LIMIT`, the rewritten-history walk's bound,
    ENG-151). The pairwise walks this replaces read the whole history for
    every pair that is not related, k(k-1) walks for k witnesses, inside
    the resolution's write transaction. Past the bound a relation the
    traversal did not reach reads as none: the most-specific rule then
    keeps more candidates, and asks, never fewer."""
    given = list(
        dict.fromkeys(
            c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower()) is not None
        )
    )
    bit = {c: 1 << i for i, c in enumerate(given)}
    reach: dict[str, int] = dict(bit)
    lines: dict[str, tuple[str, ...]] = {}
    todo = deque(given)
    while todo:
        sha = todo.popleft()
        parents = lines.get(sha)
        if parents is None:
            if len(lines) >= limit:
                continue
            parents = lines[sha] = tuple(p.lower() for p in history.parents(sha))
        mask = reach[sha]
        for p in parents:
            old = reach.get(p, 0)
            if old | mask != old:
                reach[p] = old | mask
                todo.append(p)
    return {
        c: frozenset(o for o in given if o != c and reach[c] & bit[o]) for c in given
    }


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
