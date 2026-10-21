"""The project file, `bettermemory.toml`, and the walk that finds every one
around a session's directory.

Design 2.1, decisions D12, D13 and D17. The file holds one table:

    [project]
    id = "bmp_<26 Crockford base-32 characters>"
    name = "<display name>"
    boundary = true            # optional

The nearest file at the cwd or an ancestor names the session's project. At
a repository's root it binds the repository's project and travels with
every clone; below the root it names a sub-project (D13); above the root it
is a workspace, a folder project around repositories; with `boundary =
true` it is a tenant boundary that nothing crosses (D17). In a plain folder
with no repository the nearest file makes the folder a project (E23).

`parse` never raises for a bad file: it returns a `ProjectFileError` naming
what is wrong, for the caller to log and to say in a line. `find` walks from
the cwd to the filesystem root and classifies what it finds; the part above
the repository's root comes back apart (`Above`) so that it can be cached
on its own key (design 3.1). A file evicted by a file provider is reported,
never read (design 3.1). `write` is called by `bettermemory project init`
and by nothing else. Beyond the standard library it imports only `_fsutil`
and `checkout_token`, which import nothing more, so the hook client can
read project files in its own namespace (design 8.1).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from ._fsutil import atomic_write_bytes, fsync_dir, fsync_file
from .checkout_token import CROCKFORD, random_crockford

#: The project file's name (decision D12).
PROJECT_FILE_NAME = "bettermemory.toml"
#: Every project file id starts with this.
ID_PREFIX = "bmp_"
#: Random Crockford base-32 characters after the prefix (130 bits).
ID_LENGTH = 26

_ID_RE = re.compile(f"{ID_PREFIX}[{CROCKFORD}]{{{ID_LENGTH}}}")
# A project file is a few lines; a longer file is not read.
_MAX_BYTES = 64 * 1024
# How much of a malformed id a message quotes.
_QUOTE_LIMIT = 40
_FILE_MODE = 0o644
_OPEN_READ = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
_OPEN_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
# SF_DATALESS in macOS <sys/stat.h>: the file's data is not on this disk.
_SF_DATALESS = 0x40000000
# FILE_ATTRIBUTE_RECALL_ON_OPEN and FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS in
# Windows' winnt.h: a cloud placeholder whose data a read would download.
_WINDOWS_RECALL = 0x00040000 | 0x00400000

ErrorReason = Literal[
    "unreadable",
    "evicted",
    "not_a_file",
    "too_large",
    "toml",
    "no_project_table",
    "id_missing",
    "id_malformed",
    "name_malformed",
    "wrong_type",
]


@dataclass(frozen=True)
class ProjectFile:
    """A well-formed project file. `id` decides; `name` is display only,
    and the name of the file's directory when the file gives none. `path`
    is absolute."""

    id: str
    name: str
    boundary: bool
    path: Path

    @property
    def directory(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class ProjectFileError:
    """A `bettermemory.toml` that names no project, and why: `reason` for
    the log, `detail` for a person."""

    path: Path
    reason: ErrorReason
    detail: str

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def line(self) -> str:
        """One line for the session's output or the log."""
        return f"bettermemory: {self.path} names no project: {self.detail}"


Found: TypeAlias = ProjectFile | ProjectFileError


@dataclass(frozen=True)
class Above:
    """The files above a repository's root, or around a plain folder.

    `start` is where this part of the walk began, the key it is cached on
    (design 3.1): the repository root's parent, or the cwd when there is no
    repository. It is None when the repository's root is the filesystem
    root and nothing lies above it. `items` are nearest first.
    """

    start: Path | None
    items: tuple[Found, ...]


Placement = Literal["root", "nested", "enclosing", "folder"]


@dataclass(frozen=True)
class Walk:
    """Every project file from the cwd up to the filesystem root, nearest
    first, with the malformed ones kept in place as `ProjectFileError`.

    In a repository `inner` holds what lies from the cwd up to the root,
    the root included, and `above` what lies beyond. Without a repository
    (`repository_root` None) `inner` is empty and the whole walk is
    `above`, starting at the cwd.
    """

    cwd: Path
    repository_root: Path | None
    inner: tuple[Found, ...]
    above: Above

    @property
    def items(self) -> tuple[Found, ...]:
        return self.inner + self.above.items

    @property
    def files(self) -> tuple[ProjectFile, ...]:
        """The well-formed files, nearest first."""
        return tuple(item for item in self.items if isinstance(item, ProjectFile))

    @property
    def errors(self) -> tuple[ProjectFileError, ...]:
        """The files that name no project, nearest first."""
        return tuple(item for item in self.items if isinstance(item, ProjectFileError))

    @property
    def root(self) -> ProjectFile | None:
        """The file at the repository's root: it binds the repository's
        project (design 2.1)."""
        for item in self.inner:
            if isinstance(item, ProjectFile) and item.directory == self.repository_root:
                return item
        return None

    @property
    def nested(self) -> tuple[ProjectFile, ...]:
        """Files below the repository's root, between it and the cwd,
        nearest first: sub-projects (D13). A sibling's file is never on
        the walk."""
        return tuple(
            item
            for item in self.inner
            if isinstance(item, ProjectFile) and item.directory != self.repository_root
        )

    @property
    def folder(self) -> ProjectFile | None:
        """Without a repository, the nearest file: it makes the folder a
        project (E23)."""
        if self.repository_root is not None:
            return None
        files = self.files
        return files[0] if files else None

    @property
    def enclosing(self) -> tuple[ProjectFile, ...]:
        """Files above the repository's root, or above a folder project's
        own file, nearest first: workspaces, and tenant boundaries where
        they say `boundary = true`."""
        files = tuple(
            item for item in self.above.items if isinstance(item, ProjectFile)
        )
        return files if self.repository_root is not None else files[1:]

    @property
    def nearest(self) -> ProjectFile | None:
        """The file that names the session's project: the nearest
        sub-project file, else the root's file in a repository; the folder
        project's file without one. A workspace above a repository's root
        does not name a session's project in it; its memories reach the
        repository's sessions through the chain (E23 h)."""
        if self.repository_root is None:
            return self.folder
        nested = self.nested
        return nested[0] if nested else self.root

    @property
    def boundaries(self) -> tuple[ProjectFile, ...]:
        """The files that set `boundary = true`, nearest first."""
        return tuple(item for item in self.files if item.boundary)

    def placement(self, file: ProjectFile) -> Placement:
        """Where `file`, one of `files`, sits on this walk."""
        if file not in self.files:
            raise ValueError(f"{file.path} is not on this walk")
        if self.repository_root is None:
            return "folder" if file == self.folder else "enclosing"
        if file in self.inner:
            return "root" if file.directory == self.repository_root else "nested"
        return "enclosing"


def mint_id() -> str:
    """A fresh project file id: `bmp_` and 26 random Crockford base-32
    characters (130 bits, decision D12)."""
    return ID_PREFIX + random_crockford(ID_LENGTH)


def parse(path: str | os.PathLike[str]) -> ProjectFile | ProjectFileError:
    """Read the project file at `path` with tomllib.

    A well-formed file gives a ProjectFile. Anything else gives a
    ProjectFileError and never raises: the file is missing or unreadable,
    evicted by a file provider (not read, design 3.1), not a regular file,
    too large, not TOML, without a `[project]` table, without an id or
    with one that is not `bmp_` and 26 Crockford base-32 characters, with
    a name holding control characters, or with a value of the wrong type
    (`name` must be a string, `boundary` a boolean). Without a name the
    directory's name is shown; the id decides. Keys the file has beyond
    these are left alone.
    """
    absolute = _absolute(path)
    try:
        st = os.stat(absolute)
    except OSError as exc:
        return ProjectFileError(absolute, "unreadable", _os_text(exc))
    return _parse_stat(absolute, st)


def above_start(
    cwd: str | os.PathLike[str], repository_root: str | os.PathLike[str] | None
) -> Path | None:
    """The key the walk above the root is cached on: the root's parent, the
    cwd without a repository, None when the root is the filesystem root."""
    if repository_root is None:
        return _absolute(cwd)
    root = _absolute(repository_root)
    return None if root.parent == root else root.parent


def find(
    cwd: str | os.PathLike[str],
    repository_root: str | os.PathLike[str] | None,
    *,
    above: Above | None = None,
) -> Walk:
    """Every project file from `cwd` up to the filesystem root, classified
    against `repository_root` (design 2.1).

    In a repository the files from the cwd up to the root are read on every
    call: the one at the root binds the repository's project, those below
    it are sub-projects, nearest first. The files above the root are
    workspaces; that part depends only on the root's parent, so a caller
    that cached `Walk.above` from an earlier call with the same
    `above_start` passes it back as `above` and nothing above the root is
    read again. With `repository_root` None (a plain folder) the nearest
    file is a folder project and the rest enclose it. A bare repository is
    passed with its git directory as the root.

    Paths are made absolute without resolving links; the capture passes
    resolved ones. When the cwd is not at or below the root (GIT_DIR with
    GIT_WORK_TREE elsewhere) the walk starts at the root, so the
    repository's own files decide. Malformed files are kept in place as
    ProjectFileError and never raise; an `above` whose `start` is not this
    call's `above_start` raises ValueError.
    """
    here = _absolute(cwd)
    start = above_start(here, repository_root)
    if above is not None and above.start != start:
        raise ValueError(
            f"the cached walk starts at {above.start}, this one at {start}"
        )
    if repository_root is None:
        root = None
        inner: tuple[Found, ...] = ()
    else:
        root = _absolute(repository_root)
        inner = _walk(here if here.is_relative_to(root) else root, stop=root)
    if above is None:
        above = Above(start, () if start is None else _walk(start, stop=None))
    return Walk(cwd=here, repository_root=root, inner=inner, above=above)


def write(
    directory: str | os.PathLike[str],
    *,
    name: str,
    boundary: bool = False,
    rebind: bool = False,
) -> ProjectFile:
    """Mint an id and write `bettermemory.toml` into `directory`.

    Called by `bettermemory project init` and by nothing else: it is the
    only file bettermemory writes into a working tree, and only on that
    command (design 1.1, decision D12). The user commits it. An existing
    file is never replaced unless `rebind` is true (`project init --rebind`
    or `--new`, which give the folder's file a new id); without it the call
    raises FileExistsError and the file stays as it was. A name holding
    control characters raises ValueError, and a missing directory
    FileNotFoundError: the directory is never made.
    """
    if _bad_name(name):
        raise ValueError("a project name cannot hold control characters or line breaks")
    folder = _absolute(directory)
    if not folder.is_dir():
        raise FileNotFoundError(f"no such directory: {folder}")
    project = ProjectFile(
        id=mint_id(), name=name, boundary=boundary, path=folder / PROJECT_FILE_NAME
    )
    body = render(project)
    if rebind:
        atomic_write_bytes(project.path, body, mode=_FILE_MODE)
        return project
    fd = os.open(project.path, _OPEN_CREATE, _FILE_MODE)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            fsync_file(fd)
        finally:
            os.close(fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(project.path)
        raise
    fsync_dir(folder)
    return project


def render(project: ProjectFile) -> bytes:
    """The bytes `write` puts in the file for `project`."""
    lines = [
        "# bettermemory project file, written by `bettermemory project init`.",
        "# It names this folder's project; committed, every clone carries it.",
        "# The id is random: the name can be edited, the id should stay as it is.",
        "[project]",
        f"id = {_toml_string(project.id)}",
        f"name = {_toml_string(project.name)}",
    ]
    if project.boundary:
        lines.append("boundary = true")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(path))


def _walk(start: Path, *, stop: Path | None) -> tuple[Found, ...]:
    """The project files at `start` and each ancestor, nearest first, up
    to `stop` (included) or the filesystem root."""
    found: list[Found] = []
    directory = start
    while True:
        item = _look(directory / PROJECT_FILE_NAME)
        if item is not None:
            found.append(item)
        if directory == stop or directory.parent == directory:
            return tuple(found)
        directory = directory.parent


def _look(path: Path) -> Found | None:
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return ProjectFileError(path, "unreadable", _os_text(exc))
    return _parse_stat(path, st)


def _evicted(st: os.stat_result) -> bool:
    """Whether a file provider holds the file's data elsewhere, so that
    reading it would wait on a download (design 3.1, decision D19)."""
    if sys.platform == "darwin" and getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _WINDOWS_RECALL)


def _parse_stat(path: Path, st: os.stat_result) -> ProjectFile | ProjectFileError:
    if not stat.S_ISREG(st.st_mode):
        return ProjectFileError(path, "not_a_file", "not a regular file")
    if _evicted(st):
        return ProjectFileError(
            path, "evicted", "not on this disk yet, evicted by the file provider"
        )
    raw = _read_bounded(path)
    if isinstance(raw, ProjectFileError):
        return raw
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return ProjectFileError(path, "toml", "not UTF-8 text")
    except tomllib.TOMLDecodeError as exc:
        return ProjectFileError(path, "toml", f"not valid TOML ({exc})")
    return _from_table(path, data)


def _read_bounded(path: Path) -> bytes | ProjectFileError:
    try:
        fd = os.open(path, _OPEN_READ)
    except OSError as exc:
        return ProjectFileError(path, "unreadable", _os_text(exc))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ProjectFileError(path, "not_a_file", "not a regular file")
        data = b""
        while len(data) <= _MAX_BYTES:
            chunk = os.read(fd, _MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        return ProjectFileError(path, "unreadable", _os_text(exc))
    finally:
        os.close(fd)
    if len(data) > _MAX_BYTES:
        return ProjectFileError(path, "too_large", f"larger than {_MAX_BYTES} bytes")
    return data


def _from_table(path: Path, data: dict[str, object]) -> ProjectFile | ProjectFileError:
    table = data.get("project")
    if table is None:
        return ProjectFileError(path, "no_project_table", "it has no [project] table")
    if not isinstance(table, dict):
        return ProjectFileError(path, "wrong_type", "`project` is not a table")
    project_id = table.get("id")
    if project_id is None:
        return ProjectFileError(path, "id_missing", "[project] has no id")
    if not isinstance(project_id, str):
        return ProjectFileError(path, "wrong_type", "[project] id is not a string")
    if _ID_RE.fullmatch(project_id) is None:
        quoted = repr(project_id[:_QUOTE_LIMIT])
        return ProjectFileError(
            path,
            "id_malformed",
            f"[project] id {quoted} is not {ID_PREFIX} and {ID_LENGTH} Crockford "
            "base-32 characters",
        )
    name = table.get("name")
    if name is None:
        # No name: the directory's, display only (the id decides).
        name = "".join(char for char in path.parent.name if not _bad_char(char))
    if not isinstance(name, str):
        return ProjectFileError(path, "wrong_type", "[project] name is not a string")
    if _bad_name(name):
        return ProjectFileError(
            path,
            "name_malformed",
            "[project] name holds control characters or line breaks",
        )
    boundary = table.get("boundary", False)
    if not isinstance(boundary, bool):
        return ProjectFileError(
            path, "wrong_type", "[project] boundary is not true or false"
        )
    return ProjectFile(id=project_id, name=name, boundary=boundary, path=path)


def _bad_name(name: str) -> bool:
    """A name is shown in the session's lines: control characters and line
    or paragraph separators could forge a line of their own."""
    return any(_bad_char(char) for char in name)


def _bad_char(char: str) -> bool:
    return ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 or char in "\u2028\u2029"


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string."""
    out = ['"']
    for char in value:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _os_text(exc: OSError) -> str:
    return exc.strerror or str(exc)
