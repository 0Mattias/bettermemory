"""Git's config files, read as git reads them (ENG-160).

`config_tree` reads a config file as bytes and bounded, parsed as git's
own parser parses it (`git_entries`), its includes followed in place as
git's ``config.c`` follows them (``include.path``, and
``includeIf.<condition>.path`` where the condition is ``gitdir:`` or
``gitdir/i:``, and, given an `IncludeContext`, ``onbranch:`` and
``hasconfig:remote.*.url:``), into a `ConfigValues` that says where git
dies reading it (`ConfigValues.dies`), where it read less than git would
(`ConfigValues.complete`), what an include sets under a condition not
evaluated here (`ConfigValues.undecided`) and each file it read or looked
for, stamped as it read it (`ConfigValues.files`), so a reader kept
against them sees a change to any (`unchanged`, ENG-207). Given a context
that is not collecting remote URLs, it also dies where git's reader of
remotes dies (remote.c's ``handle_config``, `_remote_rule`). `wildmatch`
is git's matcher of the patterns an ``includeIf`` names, and
`refspec_valid` git's test of a refspec.

The reader lives apart from `location_git`, which read it alone before,
because the rule a ``.git`` entry is followed by (`gitfile_rule`) reads a
git directory's ``core.worktree`` and ``core.bare`` through it, and
`githead` reads that rule too (ENG-193): `githead` cannot import
`location_git`, which imports `githead`. `location_git` imports this
module and reads its names from here (`remote_urls`, `_from_env`,
`_discover`); this module imports the standard library and, of the
package, only `_location_probe` (the one reading of an error as nothing
at a path, `path_missing`) and `_caches` (where its memo of a format's
reading is registered), which import the standard library alone, so it
never loads `location_git` or `githead`
(tests/test_import_graph.py). The hook client loads it with `location`
(tests/test_membership_capture.py pins that set).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Generic, TypeVar

from . import _caches
from ._location_probe import path_missing

#: A config variable's name: section, subsection, key. The section and the
#: key are lower case; a subsection is as git reads it, its bytes decoded
#: with ``surrogateescape`` so they are read back whole (`_spelled`).
_Name = tuple[str, str | None, str]
#: A config variable's values, in the order git reads them, each to its
#: first NUL byte (a C string, as git's callbacks see it); None for a key
#: with no ``=``.
Values = list[bytes | None]
#: A config file's stamp, the fields `githead.stamp` reads of a path:
#: ``(st_mtime_ns, st_ctime_ns, st_size, st_ino, st_mode)``; `_UNSTATED`
#: where the path could not be stat-ed, None where nothing is there.
Stamp = tuple[int, int, int, int, int]
_UNSTATED: Stamp = (-1, -1, -1, -1, -1)
#: git's bound on nested includes (``MAX_INCLUDE_DEPTH`` in config.c), past
#: which git dies.
_INCLUDE_DEPTH = 10
#: Config files one read takes in all, the file itself and each include, a
#: file included again counted again (review M D3). git has no such bound,
#: but it stops at its depth, and one file naming itself in four
#: ``include.path`` lines was read 1,398,101 times here (38 s, every value
#: kept); past this many files the read stops and reads as incomplete.
#: Measured (`config_tree`, macOS, warm): 64 files (a two-line file
#: included 63 times) in 1.2 ms, the median of 50 reads; the self-including
#: config now stops at its first include in 0.05 ms.
_INCLUDE_FILES = 64


class ConfigValues(dict[_Name, Values]):
    """`config_tree`'s answer: ``{(section, subsection, key): values}``.

    `dies` says why git dies reading what was read (a line git's parser
    refuses, an ``include.path`` with no value or one git cannot expand,
    a file it cannot read, an include past its depth or naming a file it
    is read from, a remote variable git's reader of remotes refuses), and
    `complete` is False where git reads more than was read here: a file
    over `_FORMAT_LIMIT`, the includes past `_INCLUDE_FILES` files read
    in all, a file that is a FIFO or a socket, or an include of git's
    runtime prefix. Either stops the read where it stands, and its values
    do not stand: a reader declines there. `undecided` holds what an
    include sets under a condition not evaluated here (``onbranch:`` or
    ``hasconfig:remote.*.url:`` with no `IncludeContext`, or one that
    cannot settle it, a ``gitdir:`` with no git directory given), with the
    values it sets; they are not taken, and where git dies reading them,
    or would read more, `maybe_dies` says why. `asks_remote_urls` says a
    ``hasconfig:remote.*.url:`` condition was reached before the URLs were
    collected (`IncludeContext.remote_urls`), and `forbidden` that the
    collecting read (`IncludeContext.collecting`) found a remote URL set
    in a file an ``includeIf`` reads, which git refuses outright
    (config.c's ``forbid_remote_url``). `files`, for a read asked to stamp
    (`config_tree`'s `stamped`), holds each file the read opened or looked
    for, in the order it did, with its stamp taken before it was opened (a
    change after it reads as a change to what was read): None for one
    that was not there, which git skips and whose creation changes what
    git reads (ENG-207); an include under a condition not evaluated is
    among them, though none of its values is taken: git takes them where
    the condition holds, so a change to one may change what git reads. It
    is None for a read not asked to stamp."""

    def __init__(self, *, stamped: bool = False) -> None:
        super().__init__()
        self.complete = True
        self.dies: str | None = None
        self.maybe_dies: str | None = None
        self.undecided: dict[_Name, Values] = {}
        self.files: list[tuple[str, Stamp | None]] | None = [] if stamped else None
        self.asks_remote_urls = False
        self.forbidden = False


def _merge(into: dict[_Name, Values], values: dict[_Name, Values]) -> None:
    """Add each of `values`' values after those `into` holds of its name."""
    for name, found in values.items():
        into.setdefault(name, []).extend(found)


class IncludeContext:
    """What git evaluates an ``includeIf`` past ``gitdir:`` against
    (ENG-207's review): the branch HEAD names, for ``onbranch:``, from
    `branch`, called at the first such condition and kept (a branch name
    under ``refs/heads/``, as bytes; None where HEAD is detached or names
    another ref, where git's condition never holds; `UNSETTLED` where it is
    not known, where the condition is not evaluated); and, for
    ``hasconfig:remote.*.url:``, `remote_urls`, every ``remote.<name>.url``
    of the config git reads (None until they are collected), or, with
    `collecting`, the read that collects them, where git takes every such
    condition as holding and refuses a remote URL in a file an
    ``includeIf`` reads. A read with a context that is not collecting
    dies where git's reader of remotes dies (`_remote_rule`). Its
    `prefix`, called at the first ``%(prefix)/`` path and kept, is the
    directory the git on PATH expands that to (`gitsystem.Sources.prefix`;
    None where it is not known, where such a path of a relative rest is
    not settled; ENG-207's round 7)."""

    def __init__(
        self,
        branch: Callable[[], bytes | None],
        *,
        prefix: Callable[[], str | None] | None = None,
        remote_urls: frozenset[bytes] | None = None,
        collecting: bool = False,
    ) -> None:
        self.branch = branch if isinstance(branch, _Once) else _Once(branch)
        self.prefix: _Once[str | None] = (
            prefix if isinstance(prefix, _Once) else _Once(prefix or _unknown)
        )
        self.remote_urls = remote_urls
        self.collecting = collecting

    def collect(self) -> IncludeContext:
        """This context for the read that collects the remote URLs."""
        return IncludeContext(self.branch, prefix=self.prefix, collecting=True)

    def with_urls(self, urls: frozenset[bytes]) -> IncludeContext:
        """This context with the remote URLs collected."""
        return IncludeContext(self.branch, prefix=self.prefix, remote_urls=urls)


def _unknown() -> None:
    """A context's prefix where none is given: not known."""


_T = TypeVar("_T")


class _Once(Generic[_T]):
    """`of`'s answer, asked at the first call and kept, for every context
    made from one (`IncludeContext.collect`, `IncludeContext.with_urls`)."""

    __slots__ = ("_held", "_of")

    def __init__(self, of: Callable[[], _T]) -> None:
        self._of = of
        self._held: list[_T] = []

    def __call__(self) -> _T:
        if not self._held:
            self._held.append(self._of())
        return self._held[0]


#: What `IncludeContext.branch` answers where the branch HEAD names is not
#: known: no branch name holds a NUL byte.
UNSETTLED = b"\0unsettled"


class _Overflow(Exception):
    """A read past this reader's bounds, where git reads on: the read stops
    where it stands and is incomplete (review M D3)."""


class _Dies(Exception):
    """A read git dies at: the read stops where it stands."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


@dataclass
class _Reading:
    """One `config_tree` read: the files it has taken, the identity
    (device, inode) of each file open on the include chain, whether a
    stamped read stamps its own file, whether a file it cannot read
    for want of permission is skipped, as git skips a global config, and
    the steps its conditions' patterns have left (`Steps`)."""

    files: int = 0
    chain: list[tuple[int, int]] = field(default_factory=list)
    stamp_self: bool = True
    gentle: bool = False
    context: IncludeContext | None = None
    forbidding: bool = False
    asks_remote_urls: bool = False
    forbidden: bool = False
    steps: Steps = field(default_factory=lambda: Steps())

    @property
    def prefix(self) -> Callable[[], str | None] | None:
        """The directory git expands ``%(prefix)`` to, as the read's
        context says (`IncludeContext.prefix`); None with no context."""
        return None if self.context is None else self.context.prefix

    @property
    def remote_rules(self) -> bool:
        """Whether git's reader of remotes reads what this read takes: a
        read with a context, other than the one collecting the remote URLs,
        whose callbacks are config.c's own (`IncludeContext`)."""
        return self.context is not None and not self.context.collecting


def config_tree(
    path: str,
    *,
    git_dir: str | None = None,
    stamped: bool = False,
    stamp_self: bool = True,
    gentle: bool = False,
    context: IncludeContext | None = None,
) -> ConfigValues:
    """``{(section, subsection, key): values}`` of a git config file, read
    as bytes, bounded and parsed as git parses it (`git_entries`), its
    includes followed in place as git follows them (ENG-160):
    ``include.path``, and ``includeIf.<condition>.path`` where the
    condition is ``gitdir:`` or ``gitdir/i:`` matched against `git_dir`.
    Where git dies reading it the read stops and says why
    (`ConfigValues.dies`), and where it reads more than this reader does
    the read stops and is incomplete (`ConfigValues.complete`). `stamped`
    has the answer name each file read and its stamp
    (`ConfigValues.files`, ENG-207), `path` itself left out with
    `stamp_self` off (a caller that stamps it already). `gentle` skips
    `path` where it cannot be read for want of permission, as git skips a
    global config (``ACCESS_EACCES_OK``). `context` evaluates
    ``onbranch:`` and ``hasconfig:remote.*.url:`` as git does
    (`IncludeContext`)."""
    out = ConfigValues(stamped=stamped)
    reading = _Reading(stamp_self=stamp_self, gentle=gentle, context=context)
    try:
        _read_config(path, out, git_dir, 0, reading)
    except _Overflow:
        out.complete = False
    except _Dies as exc:
        out.dies = exc.why
    out.asks_remote_urls = reading.asks_remote_urls
    out.forbidden = reading.forbidden
    return out


def _config_bytes(
    path: str, identity: list[tuple[int, int]] | None = None, *, gentle: bool = False
) -> bytes | None:
    """The bytes of the config file `path`, read whole within
    `_FORMAT_LIMIT`: None where nothing is there, which git skips (and,
    with `gentle`, where it cannot be read for want of permission);
    `_Dies` where git dies at it (it cannot be read, or is a folder) and
    `_Overflow` where git reads what is not read here (one larger than the
    bound, a FIFO or a socket, on which git would wait). A device is read
    as a file is: the null device a config names is empty. The open does
    not block. The file's (device, inode) is appended to `identity` where
    the OS gives one."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        if path_missing(exc) or (gentle and isinstance(exc, PermissionError)):
            return None
        raise _Dies(f"git cannot read {path} ({type(exc).__name__})") from None
    chunks: list[bytes] = []
    size = 0
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise _Dies(f"{path} is a folder, which git cannot read as a config")
        if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
            raise _Overflow(path)
        if identity is not None and st.st_ino:
            identity.append((st.st_dev, st.st_ino))
        while size <= _FORMAT_LIMIT:
            chunk = os.read(fd, _FORMAT_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise _Dies(f"git cannot read {path} ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    if size > _FORMAT_LIMIT:
        raise _Overflow(path)
    return b"".join(chunks)


def _read_config(
    path: str, out: ConfigValues, git_dir: str | None, depth: int, reading: _Reading
) -> None:
    """Parse the config file `path` into `out`, its includes in place;
    `_Overflow` past `_INCLUDE_FILES` files, and `_Dies` past git's depth
    or where `path` is a file the include chain is read from (compared as
    filesystem objects), which git includes until it is past its depth.
    For a stamped read the file goes to `out.files` stamped before it is
    opened (`ConfigValues.files`), so a change after the stamp reads as a
    change to what was read; the read's own file only where
    `reading.stamp_self` says so."""
    reading.files += 1
    if reading.files > _INCLUDE_FILES:
        raise _Overflow(path)
    if out.files is not None and (depth or reading.stamp_self):
        out.files.append((path, file_stamp(path)))
    identity: list[tuple[int, int]] = []
    data = _config_bytes(path, identity, gentle=reading.gentle and not depth)
    if data is None:
        return
    if depth > _INCLUDE_DEPTH:
        raise _Dies(f"{path} is included past git's depth of {_INCLUDE_DEPTH}")
    if identity and identity[0] in reading.chain:
        raise _Dies(f"{path} is included in itself, which git follows past its depth")
    reading.chain.extend(identity)
    try:
        _parse_config(path, data, out, git_dir, depth, reading)
    finally:
        del reading.chain[len(reading.chain) - len(identity) :]


def _spelled(name: bytes) -> _Name:
    """A variable's name as git's ``parse_config_key`` splits it: the
    section to the first dot, the key from the last, a subsection between
    them where there are two (None where there is one; an empty one is
    one)."""
    section, _, rest = name.partition(b".")
    sub, dot, key = rest.rpartition(b".")
    return (
        section.decode("ascii", "replace"),
        sub.decode("utf-8", "surrogateescape") if dot else None,
        key.decode("ascii", "replace"),
    )


def subsection_bytes(sub: str) -> bytes:
    """A subsection's bytes as git read them (`_spelled`)."""
    return sub.encode("utf-8", "surrogateescape")


def _parse_config(
    path: str,
    data: bytes,
    out: ConfigValues,
    git_dir: str | None,
    depth: int,
    reading: _Reading,
) -> None:
    """The variables of the config file `path` (its bytes `data`) into
    `out`, as git's parser reads them (`git_entries`), each include read
    where it stands: ``include.path`` (no subsection), and the ``path`` of
    an ``includeIf`` with one. `_Dies` where git's parser refuses the file
    or, for a read git's reader of remotes takes (`_Reading.remote_rules`),
    where that reader refuses a variable (`_remote_rule`)."""
    entries = git_entries(data)
    if entries is None:
        raise _Dies(f"{path} holds a line git's parser refuses")
    for raw, found in entries:
        name = _spelled(raw)
        value = None if found is None else found.split(b"\0", 1)[0]
        out.setdefault(name, []).append(value)
        section, sub, key = name
        if reading.remote_rules and not reading.forbidding:
            why = _remote_rule(name, value)
            if why is not None:
                raise _Dies(f"{path} sets {raw.decode('utf-8', 'replace')}: {why}")
        if section == "include" and sub is None and key == "path":
            _include(path, None, value, out, git_dir, depth, reading)
        elif section == "includeif" and sub is not None:
            # git evaluates an includeIf's condition at each of its keys,
            # not only at ``path`` (config.c's ``git_config_include``):
            # a ``hasconfig:remote.*.url:`` one collects the remote URLs
            # there, which can make git refuse the configuration.
            if key == "path":
                _include(path, sub, value, out, git_dir, depth, reading)
            elif sub.startswith(_HAS_REMOTE_URL):
                _condition(sub, path, git_dir, reading)


def _include(
    including: str,
    condition: str | None,
    value: bytes | None,
    out: ConfigValues,
    git_dir: str | None,
    depth: int,
    reading: _Reading,
) -> None:
    """Read the file an include names into `out`, as git does (config.c's
    ``handle_path_include``): under `condition` (an ``includeIf``
    section's) only where it holds, evaluated first; a value that is
    missing or that git cannot expand (`_include_target`) dies; a missing
    file is skipped. Where the condition is not evaluated here, what the
    file sets goes to `out.undecided`, its values not taken, and where git
    dies reading it, or reads more than is read here, to
    `out.maybe_dies`."""
    holds = (
        True
        if condition is None
        else _condition(condition, including, git_dir, reading)
    )
    if holds is False:
        return
    maybe = None if holds else ConfigValues(stamped=out.files is not None)
    try:
        if value is None:
            what = "include.path" if condition is None else "an includeIf's path"
            raise _Dies(f"{including} sets {what} with no value")
        target = _include_target(value, including, reading.prefix)
        context = reading.context
        if (
            holds
            and condition is not None
            and context is not None
            and context.collecting
            and not reading.forbidding
        ):
            # git collects the remote URLs with every file an includeIf
            # reads forbidden to set one (config.c's ``forbid_remote_url``),
            # its includes with it: such a URL fails git's read.
            _forbidding(target, out, git_dir, depth, reading)
            return
        _read_config(
            target, out if maybe is None else maybe, git_dir, depth + 1, reading
        )
    except (_Dies, _Overflow) as exc:
        if maybe is None:
            raise
        maybe.maybe_dies = (
            exc.why if isinstance(exc, _Dies) else f"{exc} is not read whole"
        )
    finally:
        if maybe is not None:
            _merge(out.undecided, maybe)
            _merge(out.undecided, maybe.undecided)
            out.maybe_dies = out.maybe_dies or maybe.maybe_dies or maybe.dies
            if out.files is not None:
                out.files.extend(maybe.files or ())


#: The prefix git expands to the directory it runs from (``system_path``).
_RUNTIME_PREFIX = "%(prefix)/"
#: What `IncludeContext.prefix` answers for a git that leaves
#: ``%(prefix)/`` as written (it prints its own spelling).
AS_WRITTEN = "%(prefix)"


def _interpolate(
    path: str, *, real_home: bool, prefix: Callable[[], str | None] | None = None
) -> str | None:
    """git's ``interpolate_path``: a leading ``%(prefix)/`` is git's
    ``system_path`` of the rest (ENG-207's round 7): the rest where it is
    absolute (`_git_absolute`), else the rest after the directory the git
    on PATH expands ``%(prefix)`` to (`prefix`, `IncludeContext.prefix`)
    and a ``/``, the path as written for a git that does not expand it
    (`AS_WRITTEN`), and `_Overflow` where `prefix` does not say; a
    ``%(prefix)`` with no ``/`` is a name like any other. A leading
    ``~/`` (or ``~``) is HOME, resolved with `real_home`, and a leading
    ``~user/`` that user's home directory; None where git cannot expand
    it (HOME unset, no such user)."""
    if path.startswith(_RUNTIME_PREFIX):
        found = None if prefix is None else prefix()
        rest = path[len(_RUNTIME_PREFIX) :]
        if found == AS_WRITTEN:
            return path
        if _git_absolute(rest):
            return rest
        if found is None:
            raise _Overflow(path)
        return found + "/" + rest
    if not path.startswith("~"):
        return path
    slash = path.find("/")
    if slash < 0:
        slash = len(path)
    user = path[1:slash]
    home: str | None
    if user:
        home = _user_home(user)
    else:
        home = _home()
        if home is not None and real_home:
            home = os.path.realpath(home)
        if home is not None and sys.platform == "win32":
            home = home.replace("\\", "/")
    return None if home is None else home + path[slash:]


def _user_home(user: str) -> str | None:
    """The home directory of the account `user` (``getpwnam``), None where
    there is none; `_Overflow` on Windows, where git's own lookup is not
    read here."""
    if sys.platform == "win32":
        raise _Overflow(user)
    else:
        import pwd

        try:
            return str(pwd.getpwnam(user).pw_dir)
        except (KeyError, OSError, ValueError):
            return None


def _include_target(
    value: bytes, including: str, prefix: Callable[[], str | None] | None = None
) -> str:
    """The file an include's `value` names, as config.c's
    ``handle_path_include`` takes it: expanded (`_interpolate`, with git's
    `prefix`; `_Dies` where git cannot), a relative path taken from the
    including file's directory, and spelled with the platform's separator
    (`_native`). An empty value names that directory, which git dies
    reading (`_config_bytes`)."""
    raw = os.fsdecode(value)
    target = _interpolate(raw, real_home=False, prefix=prefix)
    if target is None:
        raise _Dies(f"{including} includes {raw!r}, which git cannot expand")
    if not _git_absolute(target):
        target = os.path.join(os.path.dirname(including), target)
    return _native(target)


def _git_absolute(path: str) -> bool:
    """git's ``is_absolute_path``: a leading separator, and on Windows a
    drive letter and its colon."""
    if os.name == "nt":
        return path[:1] in ("/", "\\") or (path[1:2] == ":" and path[:1].isalpha())
    return path.startswith("/")


def file_stamp(path: str) -> Stamp | None:
    """The stamp of the file at `path` now (`Stamp`): None where nothing
    is there (`path_missing`), `_UNSTATED` where it cannot be stat-ed. A
    file that is no regular file (the null device GIT_CONFIG_GLOBAL names
    in CI, a FIFO) is stamped by its inode and type alone: the reader
    takes no value from it, and the null device's times move with every
    write to it. A directory is stamped by its times too, which move as
    an entry is made or removed (the legacy remote directories,
    `_legacy_names`)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError) as exc:
        return None if path_missing(exc) else _UNSTATED
    if stat.S_ISDIR(st.st_mode):
        return (st.st_mtime_ns, st.st_ctime_ns, 0, st.st_ino, st.st_mode)
    if not stat.S_ISREG(st.st_mode):
        return (0, 0, 0, st.st_ino, st.st_mode)
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_mode)


def unchanged(files: Sequence[tuple[str, Stamp | None]]) -> bool:
    """Whether each of `files` (`ConfigValues.files`, of one read or
    several) stamps now as it did when it was read: a file written,
    replaced, created or removed since reads as changed (ENG-207). A file
    named twice is stamped once."""
    seen: dict[str, Stamp | None] = {}
    for path, was in files:
        if path not in seen:
            seen[path] = file_stamp(path)
        if seen[path] != was:
            return False
    return True


def _native(path: str, flavour: ModuleType = os.path) -> str:
    """`path` with the platform's other separator written as its own
    (`flavour` the path module: on Windows ``C:/x\\y`` is ``C:\\x\\y``;
    POSIX has none, and a backslash there is part of a name). A config
    file's path is spelled so where it enters (an include's target, the
    machine's config files), so one file is named one way wherever it is
    stamped, read or compared: ``~/x`` expanded to ``<home>/x`` and a
    relative include under git's ``C:/Program Files/Git/etc`` mixed the
    two (ENG-207, Windows CI). Nothing else is rewritten: a ``..`` is left
    for the system to resolve, as git leaves it."""
    other = flavour.altsep
    return path.replace(other, flavour.sep) if other else path


def _home() -> str | None:
    """The home directory git expands ``~`` to: HOME, as git reads it
    (None where it is unset, where git expands no ``~`` and reads no
    global config; an empty one is the root); on Windows, where git sets
    HOME at start where it is unset, the platform's then."""
    home = os.environ.get("HOME")
    if os.name != "nt" or home:
        return home
    found = os.path.expanduser("~")
    return None if found == "~" else found


def _forbidding(
    target: str, out: ConfigValues, git_dir: str | None, depth: int, reading: _Reading
) -> None:
    """Read `target`, which an ``includeIf`` names, for the read that
    collects the remote URLs: its values are not taken, and a remote URL
    it or a file it includes sets marks the read `forbidden`."""
    sink = ConfigValues(stamped=out.files is not None)
    reading.forbidding = True
    try:
        _read_config(target, sink, git_dir, depth + 1, reading)
    finally:
        reading.forbidding = False
        _merge(out.undecided, sink.undecided)
        out.maybe_dies = out.maybe_dies or sink.maybe_dies
        if out.files is not None:
            out.files.extend(sink.files or ())
        if _remote_url_names(sink) or _remote_url_names(sink.undecided):
            reading.forbidden = True


def _remote_url_names(names: Collection[_Name]) -> list[_Name]:
    """The names among `names` that set a remote's URL, as git parses
    them (``remote.<name>.url``; a ``remote.url`` with no name is none)."""
    return [n for n in names if n[0] == "remote" and n[1] is not None and n[2] == "url"]


# ---------------------------------------------------------------------------
# git's reader of remotes (remote.c's handle_config), as it dies
# ---------------------------------------------------------------------------

#: The ``remote.<name>`` keys git's reader of remotes dies on with no
#: value: a string it takes (``git_config_string``, ``config_error_nonbool``),
#: and ``tagopt`` and ``followRemoteHEAD``, which it reads with no test
#: and crashes on (checked against git 2.54's ``git ls-remote --get-url``,
#: tests/test_location_git_parse.py).
_REMOTE_STRINGS = frozenset(
    {
        "url",
        "pushurl",
        "push",
        "fetch",
        "receivepack",
        "uploadpack",
        "tagopt",
        "proxy",
        "proxyauthmethod",
        "vcs",
        "serveroption",
        "followremotehead",
    }
)
#: The ``remote.<name>`` keys git reads as booleans (``git_config_bool``).
_REMOTE_BOOLS = frozenset(
    {"mirror", "skipdefaultupdate", "skipfetchall", "prune", "prunetags"}
)
#: The ``branch.<name>`` keys git's reader of remotes dies on with no value.
_BRANCH_STRINGS = frozenset({"remote", "pushremote", "merge"})
#: The ``url.<base>`` keys git's reader of remotes dies on with no value.
_URL_STRINGS = frozenset({"insteadof", "pushinsteadof"})


def _remote_rule(name: _Name, value: bytes | None) -> str | None:
    """Why git's reader of remotes (remote.c's ``handle_config``, git 2.54)
    dies on the variable `name` set to `value`, or None where it does not:
    a ``branch`` section whose subsection is empty; a ``branch.<name>``,
    ``url.<base>`` or ``remote.<name>`` string with no value
    (`_BRANCH_STRINGS`, `_URL_STRINGS`, `_REMOTE_STRINGS`), and
    ``remote.pushDefault``; a ``remote.<name>`` boolean that is none
    (`_REMOTE_BOOLS`, `git_bool`); a ``fetch`` or ``push`` refspec git
    refuses (`refspec_valid`). git reads no ``remote.<name>`` whose name
    starts with ``/``.

    It also dies where git's reader of promisor remotes dies
    (promisor-remote.c's ``promisor_remote_config``), which ``git fetch``,
    ``git status`` and ``git remote`` run though ``git ls-remote
    --get-url`` does not: a ``remote.<name>.promisor`` that is no boolean,
    for any name, and a ``remote.<name>.partialCloneFilter`` with no
    value, for a name that does not start with ``/`` or for none (git
    reads ``remote.partialCloneFilter`` as the filter of a remote named
    nothing). The capture declines there, as the user's own git commands
    die in that repository (ENG-207's round 7)."""
    section, sub, key = name
    if section == "remote" and key == "promisor":
        return "no boolean" if git_bool(value) is None else None
    if section == "remote" and key == "partialclonefilter":
        named_slash = sub is not None and sub.startswith("/")
        return "no value" if value is None and not named_slash else None
    if section == "branch":
        if sub == "":
            return "an empty branch name"
        if sub is not None and key in _BRANCH_STRINGS and value is None:
            return "no value"
        return None
    if section == "url":
        if sub is not None and key in _URL_STRINGS and value is None:
            return "no value"
        return None
    if section != "remote":
        return None
    if sub is None:
        return "no value" if key == "pushdefault" and value is None else None
    if sub.startswith("/"):
        return None
    if key in _REMOTE_BOOLS:
        return "no boolean" if git_bool(value) is None else None
    if key in _REMOTE_STRINGS and value is None:
        return "no value"
    if key in ("fetch", "push") and value is not None:
        valid = refspec_valid(value, fetch=key == "fetch")
        if valid is None:
            return "a refspec whose reading turns on the object format"
        if not valid:
            return "a refspec git refuses"
    return None


#: Bytes ``check_refname_format`` refuses anywhere in a name (refs.c's
#: ``refname_disposition`` 4): ASCII control characters, DEL, space and
#: ``~ ^ : ? [ \\``; a ``*`` only in a pattern, once.
_REFNAME_BAD = frozenset(range(0x20)) | frozenset(b"\x7f ~^:?[\\")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def refname_valid(name: bytes, *, pattern: bool = False, onelevel: bool = True) -> bool:
    """git's ``check_refname_format`` (refs.c, git 2.54): components split
    at ``/``, none empty, none starting with ``.`` or ending in ``.lock``,
    no ``..``, no ``@{``, no byte of `_REFNAME_BAD`, not ``@`` alone, no
    trailing ``.``; one ``*`` in all where `pattern`
    (``REFNAME_REFSPEC_PATTERN``), and one component only where
    `onelevel` (``REFNAME_ALLOW_ONELEVEL``)."""
    if name == b"@" or not name or name.endswith(b"."):
        return False
    if b".." in name or b"@{" in name:
        return False
    stars = name.count(b"*")
    if stars > (1 if pattern else 0):
        return False
    if any(byte in _REFNAME_BAD for byte in name):
        return False
    parts = name.split(b"/")
    if not onelevel and len(parts) < 2:
        return False
    return all(
        part and not part.startswith(b".") and not part.endswith(b".lock")
        for part in parts
    )


def refspec_valid(spec: bytes, *, fetch: bool) -> bool | None:
    """git's ``parse_refspec`` (refspec.c, git 2.54): whether ``refspec_append``
    takes `spec` as a fetch or a push refspec, where it dies on one it
    refuses; None for a negative refspec of 40 or 64 hex digits, which git
    refuses only where the repository's object ids are that long."""
    lhs = spec
    negative = False
    if lhs[:1] == b"+":
        lhs = lhs[1:]
    elif lhs[:1] == b"^":
        negative, lhs = True, lhs[1:]
    colon = lhs.rfind(b":")
    rhs = None if colon < 0 else lhs[colon + 1 :]
    if negative and rhs is not None:
        return False
    if not fetch and colon == 0 and rhs == b"":
        return True  # ":" or "+:", the matching refs
    src = lhs if rhs is None else lhs[:colon]
    glob = rhs is not None and len(rhs) >= 1 and b"*" in rhs
    if src and b"*" in src:
        if (rhs is not None and not glob) or (rhs is None and not negative and fetch):
            return False
        glob = True
    elif rhs is not None and glob:
        return False
    if src == b"@":
        src = b"HEAD"

    def valid(name: bytes) -> bool:
        return refname_valid(name, pattern=glob)

    hexed = len(src) in (40, 64) and all(b in _HEX_DIGITS for b in src)
    if negative:
        if not src:
            return False
        if hexed:
            return None
        return valid(src)
    if fetch:
        if src and not hexed and not valid(src):
            return False
        return not rhs or valid(rhs)
    if src and glob and not valid(src):
        return False
    if rhs is None:
        return valid(src)
    return bool(rhs) and valid(rhs)


# ---------------------------------------------------------------------------
# The legacy remote files git still reads (remote.c)
# ---------------------------------------------------------------------------

#: A config file as a read stamped it (`ConfigValues.files`).
ConfigFile = tuple[str, Stamp | None]


#: The folders of the common directory whose files name a remote git
#: still reads (remote.c's ``read_remotes_file`` and
#: ``read_branches_file``, "nominated for removal"), in the order git
#: reads them for a remote; a git built with breaking changes reads
#: neither (`gitsystem.Sources.legacy`).
_LEGACY_FOLDERS = ("remotes", "branches")
#: The bytes read of one such file; git reads any size. One larger is not
#: read, and the capture declines (fail closed).
_LEGACY_LIMIT = 1 << 16
#: The remotes these folders name that a capture reads; past it, it
#: declines.
_LEGACY_NAMES = 256
#: The bytes git's ``isspace`` takes (git-compat-util.h's ``sane_ctype``:
#: space, tab, LF and CR, not VT or FF), which its ``strbuf_trim`` trims.
_GIT_SPACE = b" \t\n\r"


def _legacy_file(path: str, files: list[ConfigFile]) -> bytes | str | None:
    """The bytes of the legacy remote file `path`, stamped into `files`
    before it is read; None where git reads nothing from it (no file, a
    folder or one git cannot open: ``fopen_or_warn`` warns and reads
    none); why the capture declines where it is no regular file (a FIFO
    would block git), is larger than `_LEGACY_LIMIT` or cannot be read."""
    files.append((path, file_stamp(path)))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        if path_missing(exc) or isinstance(exc, (PermissionError, IsADirectoryError)):
            return None
        return f"{path} cannot be read ({type(exc).__name__})"
    chunks: list[bytes] = []
    size = 0
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
            return f"{path} is not a regular file"
        while size <= _LEGACY_LIMIT:
            chunk = os.read(fd, _LEGACY_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return f"{path} cannot be read ({type(exc).__name__})"
    finally:
        os.close(fd)
    if size > _LEGACY_LIMIT:
        return f"{path} is larger than {_LEGACY_LIMIT >> 10} KiB"
    return b"".join(chunks)


def _legacy_names(common_dir: str, files: list[ConfigFile]) -> list[str] | str:
    """The remotes the legacy folders of the common directory name
    (`_LEGACY_FOLDERS`), in order, each once; each folder is stamped into
    `files` (an entry made or removed moves its times). Why the capture
    declines where a folder cannot be listed or the folders name more
    than `_LEGACY_NAMES` remotes. git lists none of these remotes (``git
    remote``), but fetches from each, and ``git remote get-url`` names its
    URL."""
    names: dict[str, None] = {}
    for folder in _LEGACY_FOLDERS:
        path = os.path.join(common_dir, folder)
        files.append((path, file_stamp(path)))
        try:
            entries = os.listdir(path)
        except (OSError, ValueError) as exc:
            if path_missing(exc):
                continue
            return f"{path} cannot be listed ({type(exc).__name__})"
        names.update(dict.fromkeys(sorted(entries)))
        if len(names) > _LEGACY_NAMES:
            return f"the legacy remote folders name more than {_LEGACY_NAMES} remotes"
    return list(names)


class _Legacy:
    """What a legacy remote file reads against: whether this git empties a
    remote's URLs at an empty one (`gitsystem.Sources.resets`), and the
    branch git names where a ``branches`` file names none
    (``repo_default_branch_name``)."""

    def __init__(self, resets: Callable[[], bool], default: Callable[[], bytes | None]):
        self.resets = resets
        self.default = default


def _legacy_remote(
    common_dir: str, name: str, files: list[ConfigFile], against: _Legacy
) -> list[bytes] | str | None:
    """The URLs git reads for the remote `name` from its legacy files, as
    remote.c's ``read_remotes_file`` and then ``read_branches_file`` read
    them for a remote whose config gives it no URL (`_rewrites`): those of
    ``remotes/<name>``, else of ``branches/<name>``. None where git dies
    reading them, for this remote alone: a ``Push:`` or ``Pull:`` line, or
    the refspecs a ``branches`` file makes, that git refuses
    (`gitconfig.refspec_valid`), or a branch name git refuses. Why the
    capture declines where a file does (`_legacy_file`)."""
    found = _legacy_file(os.path.join(common_dir, "remotes", name), files)
    if isinstance(found, str):
        return found
    urls: list[bytes] = []
    for line in (found or b"").split(b"\n"):
        line = line.rstrip(_GIT_SPACE).split(b"\0", 1)[0]
        for prefix, fetch in ((b"Push:", False), (b"Pull:", True)):
            if line.startswith(prefix):
                valid = refspec_valid(
                    line[len(prefix) :].lstrip(_GIT_SPACE), fetch=fetch
                )
                if valid is None:
                    return f"{name}'s legacy refspec turns on the object format"
                if not valid:
                    return None
        if line.startswith(b"URL:"):
            _add_url(urls, line[len(b"URL:") :].lstrip(_GIT_SPACE), against.resets)
    if urls:
        return urls
    found = _legacy_file(os.path.join(common_dir, "branches", name), files)
    if isinstance(found, str):
        return found
    line = (found or b"").split(b"\n", 1)[0].strip(_GIT_SPACE)
    if not line:
        return []
    url, hashed, branch = line.split(b"\0", 1)[0].partition(b"#")
    if not hashed:
        default = against.default()
        if default is None:
            return None
        branch = default
    nick = os.fsencode(name)
    if not refspec_valid(b"refs/heads/" + branch + b":refs/heads/" + nick, fetch=True):
        return None
    if not refspec_valid(b"HEAD:refs/heads/" + branch, fetch=False):
        return None
    _add_url(urls, url, against.resets)
    return urls


def _add_url(urls: list[bytes], url: bytes, resets: Callable[[], bool]) -> None:
    """remote.c's ``add_url``: an empty URL empties the list where this git
    does (2.46 and later, `gitsystem.Sources.resets`), and is kept as one
    that names nothing otherwise."""
    if url:
        urls.append(url)
    elif resets():
        urls.clear()
    else:
        urls.append(url)


def _default_branch(reads: Sequence[ConfigValues]) -> bytes | None:
    """The branch git names a ``branches`` file's remote fetch from where
    the file names none (refs.c's ``repo_default_branch_name``):
    GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME, else the last
    ``init.defaultBranch`` of the config `reads` read, else ``master``;
    None where git dies on it (no value, or no branch name git takes)."""
    named = os.environ.get("GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME")
    found: bytes | None = os.fsencode(named) if named else b"master"
    if not named:
        for read in reads:
            for value in read.get(("init", None, "defaultbranch"), ()):
                found = value
    if found is None or not refname_valid(b"refs/heads/" + found, onelevel=False):
        return None
    return found


def legacy_remote_stamps(common_dir: str, name: str) -> tuple[Stamp | None, ...]:
    """The stamps of the legacy files git reads for the remote `name`
    where its config gives it no URL, ``remotes/<name>`` and
    ``branches/<name>`` in the common directory (`_legacy_remote`):
    `origin`'s capture asks git for the URL of ``origin``, which such a
    file can give (ENG-207's review), and is kept against them."""
    return tuple(
        file_stamp(os.path.join(common_dir, folder, name)) for folder in _LEGACY_FOLDERS
    )


# ---------------------------------------------------------------------------
# includeIf conditions
# ---------------------------------------------------------------------------

#: The prefix of the one ``hasconfig:`` condition git knows.
_HAS_REMOTE_URL = "hasconfig:remote.*.url:"


def _condition(
    condition: str, including: str, git_dir: str | None, reading: _Reading
) -> bool | None:
    """Whether an ``includeIf`` condition holds (config.c's
    ``include_condition_is_true``): ``gitdir:`` and ``gitdir/i:`` against
    `git_dir`; ``onbranch:`` and ``hasconfig:remote.*.url:`` against the
    read's `IncludeContext` (`_branch_condition`, `_remote_url_condition`);
    None where it is not evaluated here (no context or git directory
    given, or one that cannot settle it, a pattern the read's matching
    does not settle among them, `WildmatchSpent`); False for a condition
    git does not know, another ``hasconfig:`` among them, which git
    ignores."""
    for prefix, icase in (("gitdir:", False), ("gitdir/i:", True)):
        if condition.startswith(prefix):
            if git_dir is None:
                return None
            pattern = condition[len(prefix) :]
            return _gitdir_matches(pattern, including, git_dir, icase, reading)
    if condition.startswith("onbranch:"):
        pattern = condition[len("onbranch:") :]
        return _branch_condition(pattern, reading.context, reading.steps)
    if condition.startswith(_HAS_REMOTE_URL):
        return _remote_url_condition(condition[len(_HAS_REMOTE_URL) :], reading)
    return False


def _any_match(
    pattern: bytes, texts: Iterable[bytes], steps: Steps, *, icase: bool = False
) -> bool | None:
    """Whether `pattern` matches any of `texts` (`wildmatch`, its steps
    taken from `steps`); None where none does and the match of one is not
    settled (`WildmatchSpent`)."""
    spent = False
    for text in texts:
        try:
            if wildmatch(pattern, text, icase=icase, steps=steps):
                return True
        except WildmatchSpent:
            spent = True
    return None if spent else False


def _branch_condition(
    pattern: str, context: IncludeContext | None, steps: Steps
) -> bool | None:
    """config.c's ``include_by_branch``: the branch HEAD names, its name
    under ``refs/heads/``, matched as a path against `pattern`, with
    ``**`` put after one that ends in a separator; never where HEAD is detached
    or names another ref. None with no context, where the branch is not
    known, or where the match is not settled (`_any_match`)."""
    if context is None:
        return None
    branch = context.branch()
    if branch == UNSETTLED:
        return None
    if branch is None:
        return False
    spelled = subsection_bytes(pattern)
    if spelled[-1:] in (b"/", os.sep.encode()):
        spelled += b"**"
    return _any_match(spelled, (branch,), steps)


def _remote_url_condition(pattern: str, reading: _Reading) -> bool | None:
    """config.c's ``include_by_remote_url``: whether any remote URL of
    the config git reads matches `pattern` as a path, the pattern as it
    is (no ``**`` put after a ``/``); true, whatever the pattern, for the
    read that collects them (`IncludeContext.collecting`). None with no
    context, before the URLs are collected
    (`ConfigValues.asks_remote_urls`), and where no URL matches and the
    match of one is not settled (`_any_match`, the URLs in order, so the
    answer does not turn on how a set is hashed)."""
    context = reading.context
    if context is None:
        return None
    if context.collecting:
        return True
    if context.remote_urls is None:
        reading.asks_remote_urls = True
        return None
    spelled = subsection_bytes(pattern)
    return _any_match(spelled, sorted(context.remote_urls), reading.steps)


def _slashed(path: str) -> str:
    """`path` with the platform's separator written ``/``, as git spells
    the paths a ``gitdir:`` pattern is matched against."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _gitdir_matches(
    pattern: str, including: str, git_dir: str, icase: bool, reading: _Reading
) -> bool | None:
    """config.c's ``include_by_gitdir``: the pattern expanded as git
    expands a path (`_interpolate`, HOME resolved and ``%(prefix)/`` with
    the read's `_Reading.prefix`; left as written where git cannot expand
    it), ``./`` taken from the including file's real directory (that part
    matched literally), ``**/`` put before a relative one and ``**``
    after one that ends in a separator; matched as a path against the
    real git directory, then against it as given (the read's steps,
    `_any_match`). None where the pattern names git's runtime prefix and
    the directory it names is not known, and where neither matches and
    the match is not settled."""
    steps = reading.steps
    try:
        expanded = _interpolate(pattern, real_home=True, prefix=reading.prefix)
    except _Overflow:
        return None
    if expanded is not None:
        pattern = expanded
    literal = 0
    if pattern[:1] == "." and pattern[1:2] in ("/", os.sep):
        base = _slashed(os.path.dirname(os.path.realpath(including)))
        pattern = base + pattern[1:]
        literal = len(subsection_bytes(base)) + 1
    elif not _git_absolute(pattern):
        pattern = "**/" + pattern
    if pattern[-1:] in ("/", os.sep):
        pattern += "**"
    spelled = subsection_bytes(pattern)
    named = dict.fromkeys(
        os.fsencode(_slashed(text))
        for text in (os.path.realpath(git_dir), os.path.abspath(git_dir))
    )
    rests = [t[literal:] for t in named if _literal_head(spelled, t, literal, icase)]
    return _any_match(spelled[literal:], rests, steps, icase=icase)


def _literal_head(pattern: bytes, text: bytes, literal: int, icase: bool) -> bool:
    """Whether `include_by_gitdir`'s first `literal` bytes, compared as
    they are (without case where `icase`), agree; the rest is matched by
    `wildmatch`."""
    if not literal:
        return True
    if len(text) < literal:
        return False
    head, given = pattern[:literal], text[:literal]
    return (head.lower() == given.lower()) if icase else (head == given)


# ---------------------------------------------------------------------------
# git's wildmatch (wildmatch.c), with WM_PATHNAME
# ---------------------------------------------------------------------------

_WM_MATCH = 0
_WM_NOMATCH = 1
_WM_ABORT_ALL = -1
_WM_ABORT_TO_STARSTAR = -2
_STAR, _SLASH, _BACKSLASH, _QUESTION, _OPEN = 0x2A, 0x2F, 0x5C, 0x3F, 0x5B
#: The bytes git's ``is_glob_special`` names.
_GLOB_SPECIAL = frozenset(b"*?[\\")


def _upper(c: int) -> bool:
    return 0x41 <= c <= 0x5A


def _lower(c: int) -> bool:
    return 0x61 <= c <= 0x7A


#: git's character classes, as its own ``ctype`` reads them (ctype.c's
#: ``sane_ctype``: ASCII alone, and ``space`` is space, tab, LF and CR).
_CLASSES: dict[bytes, Callable[[int], bool]] = {
    b"alnum": lambda c: _upper(c) or _lower(c) or 0x30 <= c <= 0x39,
    b"alpha": lambda c: _upper(c) or _lower(c),
    b"blank": lambda c: c in (0x20, 0x09),
    b"cntrl": lambda c: c < 0x20 or c == 0x7F,
    b"digit": lambda c: 0x30 <= c <= 0x39,
    b"graph": lambda c: 0x21 <= c <= 0x7E,
    b"lower": _lower,
    b"print": lambda c: 0x20 <= c <= 0x7E,
    b"punct": lambda c: (
        0x21 <= c <= 0x2F or 0x3A <= c <= 0x40 or 0x5B <= c <= 0x60 or 0x7B <= c <= 0x7E
    ),
    b"space": lambda c: c in (0x20, 0x09, 0x0A, 0x0D),
    b"upper": _upper,
    b"xdigit": lambda c: c in _HEX_DIGITS,
}


#: The steps one read's matching may take (`Steps`): a step of ``dowild``,
#: of a ``*``'s walk along the text and of a bracket's, about 0.2 us each,
#: so a read spends at most about 40 ms matching (measured, macOS, Python
#: 3.13: ``**/`` repeated 24 times is given up in 40 ms; git, in C, takes
#: a second to answer it). A real pattern takes a few dozen steps
#: (tests/test_location_git_parse.py).
_WM_STEPS = 200_000
#: The stars `dowild` may nest (two Python frames each), well within
#: Python's recursion limit with a capture's own frames below them.
_WM_DEPTH = 150


class WildmatchSpent(Exception):
    """A pattern this port of git's wildmatch does not settle within its
    bounds (`_WM_STEPS` steps, `_WM_DEPTH` nested stars, or Python's
    recursion limit): git, whose matcher recurses in C unbounded, answers
    it, and the condition it is matched for reads as not settled, so the
    capture declines where its include could change what is keyed
    (ENG-207's round 7)."""


class Steps:
    """The steps a read's matching has left (`_WM_STEPS`), shared by every
    pattern one `config_tree` read matches, so a file of many slow
    patterns is bounded as one is."""

    __slots__ = ("left",)

    def __init__(self, left: int = _WM_STEPS) -> None:
        self.left = left


def wildmatch(
    pattern: bytes, text: bytes, *, icase: bool = False, steps: Steps | None = None
) -> bool:
    """git's ``wildmatch`` with ``WM_PATHNAME`` (wildmatch.c, git 2.54), and
    ``WM_CASEFOLD`` with `icase`, over bytes: ``*`` and ``?`` match no
    ``/``; a ``**`` that is a whole path component matches across it,
    ``**/`` matching no directory too; a bracket takes ranges, ``!`` or
    ``^`` to negate, a backslash escape and the character classes of
    `_CLASSES`, and matches no ``/``; a backslash elsewhere makes the next
    byte literal. Checked against git's matching of ``hasconfig:`` and
    ``onbranch:`` patterns (tests/test_location_git_parse.py).
    `WildmatchSpent` past `steps` (a fresh `Steps` where none is given) or
    `_WM_DEPTH`."""
    try:
        return _dowild(pattern, 0, text, 0, icase, steps or Steps(), 0) == _WM_MATCH
    except RecursionError:
        raise WildmatchSpent("the pattern nests deeper than Python's stack") from None


def _dowild(
    p: bytes, pi: int, text: bytes, ti: int, icase: bool, steps: Steps, depth: int
) -> int:
    """wildmatch.c's ``dowild``, from `pi` in the pattern and `ti` in the
    text, line for line, `depth` stars down; each step taken from `steps`
    (`WildmatchSpent`)."""
    if depth > _WM_DEPTH:
        raise WildmatchSpent(f"the pattern nests more than {_WM_DEPTH} stars")
    n, m = len(p), len(text)
    while pi < n:
        steps.left -= 1
        if steps.left < 0:
            raise WildmatchSpent(f"the patterns take more than {_WM_STEPS} steps")
        p_ch = p[pi]
        if ti >= m and p_ch != _STAR:
            return _WM_ABORT_ALL
        t_ch = text[ti] if ti < m else 0
        if icase and _upper(t_ch):
            t_ch += 0x20
        if icase and _upper(p_ch):
            p_ch += 0x20
        if p_ch == _BACKSLASH:
            pi += 1
            p_ch = p[pi] if pi < n else 0
            if t_ch != p_ch:
                return _WM_NOMATCH
        elif p_ch == _QUESTION:
            if t_ch == _SLASH:
                return _WM_NOMATCH
        elif p_ch == _STAR:
            found = _star(p, pi, text, ti, icase, steps, depth)
            if isinstance(found, int):
                return found
            pi, ti = found
        elif p_ch == _OPEN:
            found_at = _bracket(p, pi, t_ch, icase, steps)
            if found_at < 0:
                return found_at
            if found_at == 0:
                return _WM_NOMATCH
            pi = found_at
        elif t_ch != p_ch:
            return _WM_NOMATCH
        pi += 1
        ti += 1
    return _WM_NOMATCH if ti < m else _WM_MATCH


def _star(
    p: bytes, pi: int, text: bytes, ti: int, icase: bool, steps: Steps, depth: int
) -> int | tuple[int, int]:
    """``dowild``'s ``*`` at `pi`: its answer, or where the pattern and the
    text stand when the loop goes on (one ``*`` and a ``/``, which matches
    the next directory)."""
    depth += 1
    n, m = len(p), len(text)
    pi += 1
    if pi < n and p[pi] == _STAR:
        prev = pi - 2
        while pi < n and p[pi] == _STAR:
            pi += 1
        nxt = p[pi] if pi < n else 0
        if (prev < 0 or p[prev] == _SLASH) and (
            nxt in (0, _SLASH)
            or (nxt == _BACKSLASH and pi + 1 < n and p[pi + 1] == _SLASH)
        ):
            if (
                nxt == _SLASH
                and _dowild(p, pi + 1, text, ti, icase, steps, depth) == _WM_MATCH
            ):
                return _WM_MATCH
            match_slash = True
        else:
            match_slash = False
    else:
        match_slash = False
    if pi >= n:
        if not match_slash and _SLASH in text[ti:]:
            return _WM_NOMATCH
        return _WM_MATCH
    if not match_slash and p[pi] == _SLASH:
        slash = text.find(b"/", ti)
        if slash < 0:
            return _WM_NOMATCH
        return pi, slash
    t_ch = text[ti] if ti < m else 0
    while True:
        if ti >= m:
            break
        steps.left -= 1
        if steps.left < 0:
            raise WildmatchSpent(f"the patterns take more than {_WM_STEPS} steps")
        if p[pi] not in _GLOB_SPECIAL:
            p_ch = p[pi]
            if icase and _upper(p_ch):
                p_ch += 0x20
            while ti < m and (match_slash or text[ti] != _SLASH):
                t_ch = text[ti]
                if icase and _upper(t_ch):
                    t_ch += 0x20
                if t_ch == p_ch:
                    break
                ti += 1
            else:
                t_ch = text[ti] if ti < m else 0
            if t_ch != p_ch or ti >= m:
                return _WM_ABORT_ALL if match_slash else _WM_ABORT_TO_STARSTAR
        matched = _dowild(p, pi, text, ti, icase, steps, depth)
        if matched != _WM_NOMATCH:
            if not match_slash or matched != _WM_ABORT_TO_STARSTAR:
                return matched
        elif not match_slash and t_ch == _SLASH:
            return _WM_ABORT_TO_STARSTAR
        ti += 1
        t_ch = text[ti] if ti < m else 0
    return _WM_ABORT_ALL


def _bracket(p: bytes, pi: int, t_ch: int, icase: bool, steps: Steps) -> int:
    """``dowild``'s bracket at `pi` against the text's byte `t_ch` (case
    folded with `icase`): where it closes when it matches, 0 where it does
    not, `_WM_ABORT_ALL` where it is not closed or names no class git
    knows. Each byte of it is a step of `steps`."""
    n = len(p)

    def at(i: int) -> int:
        return p[i] if i < n else 0

    pi += 1
    p_ch = at(pi)
    if p_ch == 0x5E:  # "^"
        p_ch = 0x21
    negated = p_ch == 0x21  # "!"
    if negated:
        pi += 1
        p_ch = at(pi)
    prev_ch = 0
    matched = False
    while True:
        steps.left -= 1
        if steps.left < 0:
            raise WildmatchSpent(f"the patterns take more than {_WM_STEPS} steps")
        if not p_ch:
            return _WM_ABORT_ALL
        if p_ch == _BACKSLASH:
            pi += 1
            p_ch = at(pi)
            if not p_ch:
                return _WM_ABORT_ALL
            if t_ch == p_ch:
                matched = True
        elif p_ch == 0x2D and prev_ch and at(pi + 1) and at(pi + 1) != 0x5D:
            pi += 1
            p_ch = at(pi)
            if p_ch == _BACKSLASH:
                pi += 1
                p_ch = at(pi)
                if not p_ch:
                    return _WM_ABORT_ALL
            if prev_ch <= t_ch <= p_ch:
                matched = True
            elif icase and _lower(t_ch) and prev_ch <= t_ch - 0x20 <= p_ch:
                matched = True
            p_ch = 0
        elif p_ch == _OPEN and at(pi + 1) == 0x3A:  # "[:"
            start = pi + 2
            end = start
            while at(end) and at(end) != 0x5D:
                end += 1
            if not at(end):
                return _WM_ABORT_ALL
            if end - start - 1 < 0 or p[end - 1] != 0x3A:
                # No ":]": a bracket of its own, read as a "[".
                pi = start - 2
                p_ch = _OPEN
                if t_ch == p_ch:
                    matched = True
                prev_ch = p_ch
                pi += 1
                p_ch = at(pi)
                if p_ch == 0x5D:
                    break
                continue
            test = _CLASSES.get(p[start : end - 1])
            if test is None:
                return _WM_ABORT_ALL
            if test(t_ch) or (
                icase and p[start : end - 1] == b"upper" and _lower(t_ch)
            ):
                matched = True
            pi = end
            p_ch = 0
        elif t_ch == p_ch:
            matched = True
        prev_ch = p_ch
        pi += 1
        p_ch = at(pi)
        if p_ch == 0x5D:  # "]"
            break
    if matched == negated or t_ch == _SLASH:
        return 0
    return pi


# ---------------------------------------------------------------------------
# git's own parse of one config file, for the repository format (ENG-211)
# ---------------------------------------------------------------------------

#: The bytes a config file read for the repository's format
#: (`read_git_config`) or with its includes (`config_tree`) may hold. git
#: reads any size; one larger is not read here, and the git directory is
#: refused or the remotes declined (fail closed, ENG-211). A config of
#: 30,000 branch sections (1.8 MB, tests/test_location_git_config.py) is
#: read whole. Measured (`git_entries`, macOS, Python 3.13): 11 us for a config
#: `git init` and one remote write (302 B), 86 ms for those 1.8 MB, and at
#: most about 0.55 s for 2 MiB built to be slow (a one-letter key a line;
#: 0.63 s for `gitfile_rule.tree_format`'s whole read of it).
_FORMAT_LIMIT = 1 << 21
_UTF8_BOM = b"\xef\xbb\xbf"
# Blank space and comments between entries: git's isspace (space, tab, LF
# and CR, not VT or FF) and a comment to the end of its line.
_BETWEEN = re.compile(rb"(?:[ \t\n\r]+|[#;][^\n]*)*")
# A section header (config.c's ``get_base_var`` and
# ``get_extended_base_var``): a name of key characters and dots, closed at
# once, or followed by blank space other than a newline and a quoted
# subsection, in which a backslash keeps the next character, closed at once.
_HEADER = re.compile(rb'\[([A-Za-z0-9.-]*)(?:(\])|[ \t\r]+"((?:[^"\\\n]|\\[^\n])*)"\])')
_SUB_ESCAPE = re.compile(rb"\\(.)", re.DOTALL)
# A key (``get_value``): a letter, then letters, digits and dashes, then
# spaces and tabs.
_KEY = re.compile(rb"([A-Za-z][A-Za-z0-9-]*)[ \t]*")
# A value with no backslash and no quote, to its comment and its newline:
# the run before the comment is the value, git's blank space trimmed at
# both ends.
_PLAIN = re.compile(rb"([^\n\\\"#;]*)(?:[#;][^\n]*)?(?:\n|\Z)")
_TRIM = b" \t\r"
_ESCAPES = {0x74: 0x09, 0x62: 0x08, 0x6E: 0x0A, 0x5C: 0x5C, 0x22: 0x22}

#: One variable as git's callbacks see it: its name, as ``strcmp`` compares
#: it (lower case but for a subsection, cut at a NUL byte), and its value
#: as written, None for a key with no ``=``.
Entry = tuple[bytes, bytes | None]


@dataclass(frozen=True)
class GitConfigRead:
    """`read_git_config`'s answer: the `entries` git reads from a file, in
    order, or `why` they are not known (the file is larger than
    `_FORMAT_LIMIT`, cannot be read, is no regular file, or git's parse of
    it fails, where git dies), and then no entries."""

    entries: tuple[Entry, ...] = ()
    why: str | None = None


#: The readings `read_git_config` keeps, by the path and the names asked
#: for, each with the digest of the bytes it was parsed from: a later read
#: of the file, whose bytes have that digest, takes it and parses nothing.
#: The file is read whole at each read, and only the parse is spared: the
#: digest is of what was read, so no clock decides whether a reading still
#: holds, and a rewrite that leaves the inode, the size and every time as
#: they were (a server whose clock runs behind, whole-second times over
#: sshfs, a write within one tick of the one before) is read again. An
#: entry holds only the variables asked for (four, for a repository's
#: format), never a file's whole parse, so the memo stays small whatever a
#: config holds. Bounded at `_FORMAT_MEMO_SIZE` paths, the oldest used
#: dropped first. Without it a config of 2 MiB built to be slow cost each
#: warm membership capture 0.59 s, each warm origin capture 1.17 s and a
#: cold one 5.8 s (one, two and nine parses), against 0.12 s for git's own
#: read of it (review G, macOS, Python 3.13).
_FORMAT_MEMO: OrderedDict[tuple[str, frozenset[bytes]], tuple[bytes, GitConfigRead]]
_FORMAT_MEMO = OrderedDict()
_FORMAT_MEMO_SIZE = 256
_FORMAT_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_format_memo() -> None:
    with _FORMAT_MEMO_LOCK:
        _FORMAT_MEMO.clear()


def read_git_config(path: str, names: frozenset[bytes] | None = None) -> GitConfigRead:
    """The variables the config file `path` sets, read whole and parsed as
    git parses it (`git_entries`), with no include followed: git's read of
    a repository's format (setup.c's ``read_repository_format``) follows
    none, nor reads a global or system file (ENG-211). A file that does not
    exist sets nothing, as git reads it. The open does not block, so a FIFO
    fails instead of waiting.

    With `names`, only the entries of those names are kept, in order, and
    the reading is kept with the digest of the bytes read (`_FORMAT_MEMO`):
    a later read that finds the same bytes (the file is read whole each
    time) parses nothing."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        if path_missing(exc):
            return GitConfigRead()
        return GitConfigRead(why=f"{path} could not be read ({type(exc).__name__})")
    chunks: list[bytes] = []
    size = 0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return GitConfigRead(why=f"{path} is not a regular file")
        if st.st_size > _FORMAT_LIMIT:
            return GitConfigRead(why=f"{path} is larger than {_FORMAT_LIMIT >> 20} MiB")
        while size <= _FORMAT_LIMIT:
            chunk = os.read(fd, _FORMAT_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return GitConfigRead(why=f"{path} could not be read ({type(exc).__name__})")
    finally:
        os.close(fd)
    if size > _FORMAT_LIMIT:
        return GitConfigRead(why=f"{path} is larger than {_FORMAT_LIMIT >> 20} MiB")
    data = b"".join(chunks)
    if names is None:
        entries = git_entries(data)
        if entries is None:
            return GitConfigRead(why=f"{path} is a config git cannot parse")
        return GitConfigRead(tuple(entries))
    key = (path, names)
    digest = hashlib.blake2b(data).digest()
    with _FORMAT_MEMO_LOCK:
        kept = _FORMAT_MEMO.get(key)
        if kept is not None and kept[0] == digest:
            _FORMAT_MEMO.move_to_end(key)
            return kept[1]
    entries = git_entries(data)
    if entries is None:
        read = GitConfigRead(why=f"{path} is a config git cannot parse")
    else:
        read = GitConfigRead(tuple(e for e in entries if e[0] in names))
    with _FORMAT_MEMO_LOCK:
        _FORMAT_MEMO[key] = (digest, read)
        _FORMAT_MEMO.move_to_end(key)
        while len(_FORMAT_MEMO) > _FORMAT_MEMO_SIZE:
            _FORMAT_MEMO.popitem(last=False)
    return read


def git_entries(data: bytes) -> list[Entry] | None:
    """The variables the config file `data` sets, in order, as git's own
    parser reads them (config.c's ``git_parse_source``, ``get_next_char``,
    ``get_base_var``, ``get_value`` and ``parse_value``; checked against
    git 2.54), or None where that parse fails ("bad config line") and git
    dies.

    It is read as git reads it, byte by byte, not by lines: a leading
    UTF-8 byte order mark is dropped (a part of one fails); CR LF is LF and
    a lone CR is blank space, as space and tab are, but not VT, FF or any
    other byte Python splits lines at; a backslash before a newline joins
    the next line onto a value outside a comment, inside quotes too; a
    section header may be followed by a key on its line; the end of the
    file ends a line. A name is lower case but for a quoted subsection,
    whose escapes are undone, and is cut at its first NUL byte, as git's
    ``strcmp`` reads it: ``[core "worktree\\0"]`` names ``core.worktree``.
    A value is kept as written (`_value`), NUL bytes and all."""
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    elif data[:1] == _UTF8_BOM[:1]:
        return None
    text = data.replace(b"\r\n", b"\n")
    out: list[Entry] = []
    stem = b""
    pos, end = 0, len(text)
    while True:
        between = _BETWEEN.match(text, pos)
        pos = end if between is None else between.end()
        if pos >= end:
            return out
        if text[pos] == 0x5B:  # "["
            header = _HEADER.match(text, pos)
            if header is None:
                return None
            name, closed, sub = header.groups()
            if closed is not None and not name:
                return None
            stem = name.lower() + b"."
            if closed is None:
                stem += _SUB_ESCAPE.sub(rb"\1", sub) + b"."
            pos = header.end()
            continue
        key = _KEY.match(text, pos)
        if key is None:
            return None
        pos = key.end()
        value: bytes | None = None
        if pos < end and text[pos] == 0x3D:  # "="
            read = _value(text, pos + 1)
            if read is None:
                return None
            value, pos = read
        elif pos < end and text[pos] != 0x0A:
            return None
        else:
            pos += 1
        out.append(((stem + key.group(1).lower()).split(b"\0", 1)[0], value))


def _value(text: bytes, pos: int) -> tuple[bytes, int] | None:
    """The value that starts at `pos`, and the position past the newline
    ending it (config.c's ``parse_value``), or None where git refuses it: a
    quote open at its newline, or an escape other than ``\\t``, ``\\b``,
    ``\\n``, ``\\\\`` and ``\\"``. Blank space before it is skipped and
    after it trimmed, unless a quote follows (``a ""`` keeps its space);
    quotes group, a ``#`` or ``;`` outside them starts a comment to the
    newline, and a backslash before a newline joins the lines."""
    plain = _PLAIN.match(text, pos)
    if plain is not None:
        return plain.group(1).strip(_TRIM), plain.end()
    out = bytearray()
    end = len(text)
    quote = comment = False
    trim = 0
    while True:
        c = text[pos] if pos < end else 0x0A
        pos += 1
        if c == 0x0A:
            if quote:
                return None
            if trim:
                del out[trim:]
            return bytes(out), pos
        if comment:
            continue
        if c in _TRIM and not quote:
            if not trim:
                trim = len(out)
            if out:
                out.append(c)
            continue
        if not quote and c in (0x23, 0x3B):  # "#" ";"
            comment = True
            continue
        trim = 0
        if c == 0x5C:  # a backslash
            c = text[pos] if pos < end else 0x0A
            pos += 1
            if c == 0x0A:
                continue
            if c not in _ESCAPES:
                return None
            out.append(_ESCAPES[c])
        elif c == 0x22:
            quote = not quote
        else:
            out.append(c)


# C's isspace, which ``strtoimax`` skips before a number.
_C_SPACE = b" \t\n\v\f\r"
_INT_MAX = (1 << 31) - 1
_INTMAX_MAX = (1 << 63) - 1
_UNITS = {b"": 1, b"k": 1 << 10, b"m": 1 << 20, b"g": 1 << 30}
_HEX = re.compile(rb"0[xX]([0-9A-Fa-f]+)")
_OCTAL = re.compile(rb"0[0-7]*")
_DECIMAL = re.compile(rb"[0-9]*")


def git_int(value: bytes | None) -> int | None:
    """The int git reads from a config value (config.c's
    ``git_config_int``: ``strtoimax`` in base 0, so ``0x`` is hex and a
    leading ``0`` octal, then a unit ``k``, ``m`` or ``g`` in either case,
    within an int), or None where git dies on it, as on no value."""
    if value is None:
        return None
    text = value.split(b"\0", 1)[0]
    i, n = 0, len(text)
    while i < n and text[i] in _C_SPACE:
        i += 1
    sign = 1
    if i < n and text[i] in b"+-":
        sign = -1 if text[i] == 0x2D else 1
        i += 1
    hexed = _HEX.match(text, i)
    if hexed is not None:
        digits, base, after = hexed.group(1), 16, hexed.end()
    else:
        base = 8 if text[i : i + 1] == b"0" else 10
        found = (_OCTAL if base == 8 else _DECIMAL).match(text, i)
        assert found is not None
        digits, after = found.group(0), found.end()
    if not digits:
        return None
    number = sign * int(digits, base)
    factor = _UNITS.get(text[after:].lower())
    if factor is None or not -_INTMAX_MAX - 1 <= number <= _INTMAX_MAX:
        return None
    if (number < 0 and -(_INT_MAX // factor) > number) or (
        number > 0 and _INT_MAX // factor < number
    ):
        return None
    return number * factor


def git_bool(value: bytes | None) -> bool | None:
    """The boolean git reads from a config value (``git_config_bool``):
    true with no value, false where empty, ``true``, ``yes``, ``on``,
    ``false``, ``no`` and ``off`` in any case, else an int that is not
    zero (`git_int`); None where git dies on it."""
    if value is None:
        return True
    text = value.split(b"\0", 1)[0]
    if not text:
        return False
    word = text.lower()
    if word in (b"true", b"yes", b"on"):
        return True
    if word in (b"false", b"no", b"off"):
        return False
    number = git_int(text)
    return None if number is None else number != 0
