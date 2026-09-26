"""The commit HEAD names, and a change signature, read from the
repository's files.

A warm daemon keys its caches on the commit HEAD names, and its origin
cache on whether anything the origin capture reads has changed. Git
answers both at the cost of a process per question. The answers sit in a
few small files: the ``.git`` entry that names the git directory, the
``HEAD`` file in it, the loose ref HEAD names or its line in
``packed-refs``, and the repository's ``config``. This module reads them
with the standard library and no subprocess, and answers only where the
files settle the question the way git's own reader does. Everywhere else
it returns None, and the caller asks git.

Discovery (`find_gitdir`, `gitdir_at`) follows ``setup_git_directory_gently``
in git's ``setup.c``. Walking up from the resolved start directory, it
takes the first ``.git`` directory that passes ``is_git_directory`` (a
``HEAD`` that ``validate_headref`` accepts, ``objects`` and ``refs``
directories in the common directory) or ``.git`` file of the form
``gitdir: <path>``, and it stops where git stops: below a
``GIT_CEILING_DIRECTORIES`` entry, which git honours only as a strict
ancestor of the directory it probes, and at a filesystem boundary. It
declines wherever git would refuse the repository, would consult state
these files do not hold, or would block: an environment variable that
moves discovery (`_DISCOVERY_ENV`), a ``.git`` entry git would die on or
skip, a bare repository or the inside of a git directory, a path the
effective user does not own (git then reads ``safe.directory`` from the
user's configuration), a FIFO or a device where git opens a file, and
every platform but POSIX, where git's ownership check reads security
descriptors.

Resolution (`head_sha`, `head_ref`) follows ``refs_resolve_ref_unsafe``
over the files backend. A symbolic ``ref: <name>`` is read from the loose
ref file first, under the git directory for the per-worktree prefixes and
under the common directory otherwise, and from ``packed-refs`` when there
is no loose file. A loose ref that is itself symbolic is followed, and the
chain ends where git's does: five reads, HEAD's included. A name that
fails ``check_refname_format``, content of any other shape, a symbolic
link or any other file that is not regular where a ref file should be,
and any error other than a missing file decline the answer. `head_sha` answers only for a detached HEAD or a
branch under ``refs/heads/``: git refuses to write anything but a commit
to either (``write_ref_to_lockfile``), so the stored hash is the commit
``git rev-parse --verify HEAD^{commit}`` prints. Neither the object
database nor the repository's hash algorithm is consulted, so a ref
written by hand that names a missing object, or a hash of the other
algorithm's length, is returned as written; git writes neither.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitDir",
    "Signature",
    "find_gitdir",
    "gitdir_at",
    "head_bytes",
    "head_ref",
    "head_sha",
    "signature",
]

# Environment variables under which git's discovery or its check of a
# candidate differs from the walk below: an explicit git directory, work
# tree, common directory or object directory, discovery across
# filesystems, and git's switch that treats every path as foreign-owned.
_DISCOVERY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_TEST_ASSUME_DIFFERENT_OWNER",
)

# A module constant, so a test can take the non-POSIX branch anywhere.
_POSIX = os.name == "posix"

# git's isspace (sane_ctype in ctype.c): space, tab, LF and CR. Python's
# default strip set adds VT and FF, which git keeps.
_SPACE = b" \t\n\r"

# A full lowercase hash, the shape `origin.is_full_commit_sha` accepts: 40
# hex characters in a SHA-1 repository, 64 in a SHA-256 one.
_FULL_HASH = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")

# validate_headref accepts a HEAD whose first 40 characters are hex digits
# of either case (get_oid_hex_any), whatever follows them.
_HEX_PREFIX = re.compile(rb"[0-9a-fA-F]{40}")

# SYMREF_MAXDEPTH in git's refs.h: at most five ref reads, HEAD's included.
_MAX_READS = 5

# The refs git keeps per worktree, under each worktree's own git directory.
_PER_WORKTREE = (b"refs/bisect/", b"refs/rewritten/", b"refs/worktree/")

# Bytes check_refname_format rejects anywhere in a name: ASCII control
# characters, DEL, space, and ~ ^ : ? * [ \ .
_BAD_REF_BYTES = frozenset(range(0x20)) | frozenset(b"\x7f ~^:?*[\\")

# git refuses a .git file over 1 MiB (read_gitfile_gently). A ref file
# holds a hash or one ref name. packed-refs past its bound is read faster
# by git than here.
_GITFILE_LIMIT = 1 << 20
_REF_LIMIT = 1 << 16
_PACKED_LIMIT = 1 << 26
_READ_CHUNK = 1 << 16

#: ``(gitdir, head, config, GIT_DIR, GIT_WORK_TREE, GIT_CEILING_DIRECTORIES)``;
#: see `signature`.
Signature = tuple[object, ...]


@dataclass(frozen=True)
class GitDir:
    """Where a working tree's repository lives.

    `gitdir` holds HEAD and the per-worktree refs. `commondir` holds
    ``refs/``, ``packed-refs`` and ``config``: the git directory itself in
    a primary checkout or a submodule, the directory the ``commondir`` file
    names in a linked worktree. `worktree_root` is the directory that
    holds the ``.git`` entry; ``git rev-parse --show-toplevel`` names the
    same directory unless ``core.worktree`` or ``core.bare`` says
    otherwise, which this module does not read.
    """

    gitdir: Path
    commondir: Path
    worktree_root: Path


class _Undetermined:
    """The first slot of a signature the files cannot settle. An instance
    equals only itself and each `signature` call makes a new one, so no
    two such signatures compare equal."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<undetermined>"


class _Unsettled(Exception):
    """The files do not settle what git would answer."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read(path: str | bytes, limit: int, *, follow: bool = True) -> bytes:
    """Up to ``limit + 1`` leading bytes of the regular file `path`, so a
    caller can tell a file longer than `limit` apart. With `follow` False a
    symbolic link fails with ELOOP instead of being read through. A
    directory raises IsADirectoryError and any other kind of file an
    OSError; the open does not block, so a FIFO fails instead of waiting
    for a writer."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if not follow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR))
        if not stat.S_ISREG(mode):
            raise OSError(errno.EINVAL, "not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _real(path: str) -> str:
    """`path` with every symbolic link resolved. Where a component is
    missing git's ``strbuf_realpath`` dies or drops the path; here that
    declines."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError as exc:
        raise _Unsettled from exc


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _check_environment() -> None:
    if not _POSIX or any(name in os.environ for name in _DISCOVERY_ENV):
        raise _Unsettled


def _valid_headref(path: str) -> bool:
    """git's ``validate_headref``: HEAD is a symbolic link into ``refs/``,
    a ``ref:`` line naming something under ``refs/``, or begins with a
    hash. Git reads the first 255 bytes. A HEAD that is neither a regular
    file, a directory nor a symbolic link declines: git's open of a FIFO
    waits for a writer, and its read of a device reads the device."""
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            return os.readlink(os.fsencode(path)).startswith(b"refs/")
        if stat.S_ISDIR(mode):
            return False
        if not stat.S_ISREG(mode):
            raise _Unsettled
        content = _read(path, 255)[:255]
    except OSError:
        return False
    if content.startswith(b"ref:") and content[4:].lstrip(_SPACE).startswith(b"refs/"):
        return True
    return _HEX_PREFIX.match(content) is not None


def _common_dir(gitdir: str) -> str:
    """git's ``get_common_dir_noenv``: the directory a ``commondir`` file
    names, relative to `gitdir` unless absolute, or `gitdir` itself when
    there is no such file. Git dies on an empty or unreadable one."""
    try:
        content = _read(os.path.join(gitdir, "commondir"), _REF_LIMIT)
    except (FileNotFoundError, NotADirectoryError):
        return gitdir
    except OSError as exc:
        raise _Unsettled from exc
    target = content.rstrip(b"\r\n")
    if not content or len(content) > _REF_LIMIT or b"\0" in target:
        raise _Unsettled
    name = os.fsdecode(target)
    if not os.path.isabs(name):
        name = os.path.join(gitdir, name)
    return _real(name)


def _git_directory(candidate: str) -> str | None:
    """git's ``is_git_directory``: the common directory when `candidate`
    is a git directory, None when it is not."""
    if not _valid_headref(os.path.join(candidate, "HEAD")):
        return None
    common = _common_dir(candidate)
    if os.access(os.path.join(common, "objects"), os.X_OK) and os.access(
        os.path.join(common, "refs"), os.X_OK
    ):
        return common
    return None


def _read_gitfile(entry: str, directory: str) -> tuple[str, str]:
    """git's ``read_gitfile_gently``: the resolved git directory a ``.git``
    file names, and its common directory. Every shape git dies on
    declines: a file over 1 MiB, no ``gitdir: `` prefix, an empty path,
    a path that is not a git directory."""
    content = _read(entry, _GITFILE_LIMIT)
    if len(content) > _GITFILE_LIMIT or not content.startswith(b"gitdir: "):
        raise _Unsettled
    body = content.rstrip(b"\r\n")
    if len(body) < 9 or b"\0" in body:
        raise _Unsettled
    target = os.fsdecode(body[8:])
    if not os.path.isabs(target):
        target = os.path.join(directory, target)
    # Git checks the path as written and then resolves it; a path that
    # does not resolve is no git directory either, so checking the
    # resolved path answers the same and gives the common directory git
    # derives from it.
    gitdir = _real(target)
    common = _git_directory(gitdir)
    if common is None:
        raise _Unsettled
    return gitdir, common


def _check_owner(*paths: str) -> None:
    """git's ``ensure_valid_ownership`` for a repository it need not look
    up in ``safe.directory``: every path, not followed through a symbolic
    link, belongs to the effective user. Git compares root's paths against
    ``SUDO_UID`` instead; that case declines."""
    if sys.platform == "win32":
        # Discovery declines off POSIX before it asks; the branch also keeps
        # the POSIX-only call below out of the Windows type check.
        raise _Unsettled
    uid = os.geteuid()
    if uid == 0 and "SUDO_UID" in os.environ:
        raise _Unsettled
    for path in paths:
        if os.lstat(path).st_uid != uid:
            raise _Unsettled


def _probe(directory: str) -> GitDir | None:
    """The repository whose ``.git`` entry sits in `directory`; None when
    there is no entry and `directory` is not itself a git directory."""
    entry = os.path.join(directory, ".git")
    try:
        mode = os.stat(entry).st_mode
    except (FileNotFoundError, NotADirectoryError):
        if _git_directory(directory) is not None:
            # A bare repository or the inside of a git directory: git
            # answers with no working tree.
            raise _Unsettled from None
        return None
    if stat.S_ISREG(mode):
        gitdir, common = _read_gitfile(entry, directory)
        _check_owner(entry, directory, gitdir)
        return GitDir(Path(gitdir), Path(common), Path(directory))
    found = _git_directory(entry)
    if found is None:
        # Git skips a .git that is not a git directory and goes on to
        # judge what lies above it, which is not settled here.
        raise _Unsettled
    _check_owner(directory, entry)
    return GitDir(Path(entry), Path(found), Path(directory))


def _ceiling_offset(path: str) -> int:
    """git's ``longest_ancestor_length`` over the ``GIT_CEILING_DIRECTORIES``
    entries as git canonicalises them: the length of the longest entry
    that is a strict ancestor of `path`, -1 when none is. Relative entries
    are dropped, and an empty entry leaves the entries after it
    unresolved."""
    raw = os.environ.get("GIT_CEILING_DIRECTORIES")
    if not raw or path == "/":
        return -1
    best = -1
    verbatim = False
    for entry in raw.split(os.pathsep):
        if not entry:
            verbatim = True
            continue
        if not os.path.isabs(entry):
            continue
        ceiling = entry if verbatim else _real(entry)
        length = len(ceiling) - 1 if ceiling.endswith("/") else len(ceiling)
        if (
            len(path) > length + 1
            and path[length] == "/"
            and path.startswith(ceiling[:length])
        ):
            best = max(best, length)
    return best


def _discover(start: Path) -> GitDir | None:
    """The walk of `find_gitdir`. Raises `_Unsettled` or OSError where it
    declines, returns None where git finds no repository."""
    _check_environment()
    here = os.path.realpath(start)
    status = os.stat(here)
    if not stat.S_ISDIR(status.st_mode) or not os.access(here, os.X_OK):
        # Git cannot run from here at all.
        raise _Unsettled
    ceiling = _ceiling_offset(here)
    while True:
        found = _probe(here)
        if found is not None:
            return found
        parent = os.path.dirname(here)
        if parent == here:
            return None
        if (len(parent) if parent != "/" else 0) <= ceiling:
            return None
        if os.stat(parent).st_dev != status.st_dev:
            return None
        here = parent


def find_gitdir(start: Path) -> GitDir | None:
    """The repository git discovers from `start`, found by walking up from
    ``start.resolve()`` the way git does. None outside any repository and
    wherever the files do not settle the answer (the module docstring
    lists those cases); a caller that needs the two apart asks git."""
    try:
        return _discover(start)
    except (_Unsettled, OSError, ValueError):
        return None


def gitdir_at(root: Path) -> GitDir | None:
    """`find_gitdir` without the walk, for a caller that already knows
    the working tree's root: the origin capture's ``worktree_root``, which
    is ``git rev-parse --show-toplevel`` resolved. None when `root` holds
    no ``.git`` entry git would accept, and wherever `find_gitdir`
    declines."""
    try:
        _check_environment()
        directory = os.path.realpath(root)
        if not stat.S_ISDIR(os.stat(directory).st_mode):
            return None
        return _probe(directory)
    except (_Unsettled, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def head_bytes(gd: GitDir) -> bytes | None:
    """The raw content of the git directory's ``HEAD``: ``ref: <name>``
    and a newline on a branch, the hash and a newline when detached. None
    when it cannot be read, is larger than any ref file, or is a symbolic
    link, git's oldest HEAD format, whose content is the branch's hash
    rather than its name."""
    try:
        content = _read(os.path.join(gd.gitdir, "HEAD"), _REF_LIMIT, follow=False)
    except OSError:
        return None
    return content if len(content) <= _REF_LIMIT else None


def _valid_refname(name: bytes) -> bool:
    """git's ``check_refname_format`` for a name of two or more
    components: none empty, none starting with a dot or ending in
    ``.lock``, no ``..`` or ``@{``, no forbidden byte, no trailing dot."""
    if b".." in name or b"@{" in name or name.endswith(b"."):
        return False
    if any(byte in _BAD_REF_BYTES for byte in name):
        return False
    return all(
        part and not part.startswith(b".") and not part.endswith(b".lock")
        for part in name.split(b"/")
    )


def _parse(content: bytes) -> tuple[bytes | None, str | None]:
    """A ref file's content, trimmed the way ``files_read_raw_ref`` trims
    it: ``(name, None)`` for ``ref: <name>``, ``(None, hash)`` for a full
    hash. The name must lie under ``refs/`` and pass
    ``check_refname_format``, and the hash must be all there is; git
    accepts a few shapes beyond these (uppercase hex, text after the
    hash, a one-level target), which decline here."""
    text = content.rstrip(_SPACE)
    if text.startswith(b"ref:"):
        name = text[4:].lstrip(_SPACE)
        if name.startswith(b"refs/") and _valid_refname(name):
            return name, None
        raise _Unsettled
    if _FULL_HASH.fullmatch(text):
        return None, text.decode("ascii")
    raise _Unsettled


def _packed(commondir: Path, name: bytes) -> str | None:
    """The hash ``packed-refs`` records for `name`, None when it records
    none. The header line and the peeled ``^`` lines never match. A file
    git would reject (a malformed header, a last line with no newline), a
    second entry for the name, or a line of any other shape declines."""
    try:
        data = _read(os.path.join(commondir, "packed-refs"), _PACKED_LIMIT)
    except FileNotFoundError:
        return None
    if not data:
        return None
    header = data.startswith(b"#")
    if (
        len(data) > _PACKED_LIMIT
        or not data.endswith(b"\n")
        or (header and not data.startswith(b"# pack-refs with:"))
    ):
        raise _Unsettled
    # A ref name holds no space and no newline, so the name between the
    # record's single space and its newline is matched whole.
    needle = b" " + name + b"\n"
    found: str | None = None
    at = data.find(needle)
    while at >= 0:
        start = data.rfind(b"\n", 0, at) + 1
        if not (header and start == 0):
            value = data[start:at]
            if found is not None or not _FULL_HASH.fullmatch(value):
                raise _Unsettled
            found = value.decode("ascii")
        at = data.find(needle, at + 1)
    return found


def _lookup(gd: GitDir, name: bytes) -> tuple[bytes | None, str | None]:
    """What the ref `name` holds: ``(target, None)`` when it is symbolic,
    ``(None, hash)`` when it is direct, ``(None, None)`` when it does not
    exist. The loose file wins over a ``packed-refs`` line, as in
    ``files_read_raw_ref``; a per-worktree ref with no loose file declines,
    since git would go on to look in the shared ``packed-refs``."""
    per_worktree = name.startswith(_PER_WORKTREE)
    base = gd.gitdir if per_worktree else gd.commondir
    try:
        content = _read(os.fsencode(base) + b"/" + name, _REF_LIMIT, follow=False)
    except (FileNotFoundError, IsADirectoryError):
        # No loose file, or a directory where it would be: git reads the
        # packed store next.
        if per_worktree:
            raise _Unsettled from None
        return None, _packed(gd.commondir, name)
    if len(content) > _REF_LIMIT:
        raise _Unsettled
    return _parse(content)


def _resolve(gd: GitDir) -> tuple[bytes | None, str | None]:
    """HEAD followed to the end of its chain: ``(name, hash)`` on a
    branch, ``(name, None)`` when the ref it ends at does not exist (an
    unborn branch), ``(None, hash)`` when detached."""
    head = head_bytes(gd)
    if head is None:
        raise _Unsettled
    name, sha = _parse(head)
    reads = 1
    while name is not None:
        if reads == _MAX_READS:
            raise _Unsettled
        reads += 1
        target, sha = _lookup(gd, name)
        if target is None:
            return name, sha
        name = target
    return None, sha


def head_sha(gd: GitDir) -> str | None:
    """HEAD resolved to a full lowercase hash: the commit ``git rev-parse
    --verify HEAD^{commit}`` prints. None on an unborn branch or a missing
    ref, past git's chain depth, when HEAD ends at a ref outside
    ``refs/heads/`` (git would peel a tag object there), on any content of
    a shape the module docstring does not admit, and on any OSError."""
    try:
        name, sha = _resolve(gd)
    except (_Unsettled, OSError, ValueError):
        return None
    if name is not None and not name.startswith(b"refs/heads/"):
        return None
    return sha


def head_ref(gd: GitDir) -> str | None:
    """The ref HEAD names, followed to the end of its chain, as ``git
    symbolic-ref HEAD`` prints it: ``refs/heads/main`` on a branch, born
    or unborn. None when HEAD is detached and wherever the files do not
    settle the name."""
    try:
        name, _ = _resolve(gd)
        return None if name is None else name.decode("utf-8")
    except (_Unsettled, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def signature(start: Path) -> Signature:
    """A cheap change detector for the origin cache: equal across calls
    while nothing it covers has changed, different once something has.

    ``(gitdir, head, config, GIT_DIR, GIT_WORK_TREE, GIT_CEILING_DIRECTORIES)``:
    the git directory the walk from `start` reaches, as a string, or None
    outside any repository; the raw bytes of its HEAD, which name the
    branch; ``(st_mtime_ns, st_size)`` of the common directory's ``config``,
    which holds the remotes and ``core.worktree``, or None when there is no
    such file; and the three environment variables that move discovery.
    The walk, a read of HEAD and a stat of config; it never raises.

    Where the files do not settle what git would find (every case
    `find_gitdir` declines, and a HEAD `head_sha` cannot parse, such as a
    reftable repository's ``ref: refs/heads/.invalid``), the first slot is
    a new marker that equals nothing else, so the signature matches no
    stored one and the caller asks git every time. A None there would make
    each such call equal to the last, however the repository moved. A
    cache therefore compares the signature it stored beside a value with
    the current one; used as part of a key, an undetermined signature
    would add an entry on every call.

    It does not see configuration outside the repository's own config file
    (the global and system files, ``include.path`` targets,
    ``config.worktree``), refs other than HEAD's own bytes (a tag named
    like the checked-out branch turns ``git symbolic-ref --short HEAD`` from
    ``main`` into ``heads/main`` and leaves the signature as it was), or a
    same-size rewrite of ``config`` inside one tick of the filesystem's
    clock.
    """
    env = (
        os.environ.get("GIT_DIR"),
        os.environ.get("GIT_WORK_TREE"),
        os.environ.get("GIT_CEILING_DIRECTORIES"),
    )
    config: tuple[int, int] | None
    try:
        gd = _discover(start)
        if gd is None:
            return (None, None, None, *env)
        head = head_bytes(gd)
        if head is None:
            raise _Unsettled
        _parse(head)
        try:
            status = os.stat(os.path.join(gd.commondir, "config"))
        except FileNotFoundError:
            config = None
        else:
            config = (status.st_mtime_ns, status.st_size)
    except (_Unsettled, OSError, ValueError):
        return (_Undetermined(), None, None, *env)
    return (str(gd.gitdir), head, config, *env)
