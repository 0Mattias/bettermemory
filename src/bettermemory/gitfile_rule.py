"""The rule a ``.git`` entry is followed by (ENG-158, ENG-206, ENG-193).

A folder's ``.git`` entry is a directory (the folder's own git directory),
a ``gitdir: <path>`` file, or a link to either; git follows every pointer.
Membership follows one only where git's own pointers say the folder is
that git directory's working tree, or where bettermemory recorded it as
the working tree of a git directory kept apart (`gitfile_refusal`,
`gitdir_pin`), and judges a ``.git`` that is a symbolic link, a hard link
or a Windows junction as the pointer it is (`judge`). Followed, any other
pointer keys the folder on another checkout's git directory: an
extracted archive or a cloned repository that planted one would be
delivered that checkout's memories, and an origin captured there would
carry that checkout's toplevel, remote and HEAD.

Three readers apply the rule, so it lives here, once: membership's
discovery (`location_git._discover`, through `location_git._at_dotgit`),
the repository files `githead` reads with no git process (its `_probe`,
under `find_gitdir`, `gitdir_at`, `locate` and `signature`), and the
origin family's git processes, which `origin` runs only where the
repository git's walk would reach is one membership follows
(`discovery_refusal`, ENG-193). `githead` cannot import `location_git`,
which imports `githead`, so neither holds the rule. Where git's walk
stops is git's own test of a git directory, ``is_git_directory``
(`_git_directory`), which `githead` reads from here too: the walk
`discovery_refusal` makes stops only where git's does, so an entry git
passes over hides no pointer above it.

This module imports the standard library, `gitconfig` (whose parse of a
config file as git parses it the rule reads a git directory's working
tree through, `tree_format`), `gitdir_pin`
(the records it reads; it writes none: membership's capture records a
tree, `location.record_working_tree`) and `location_reads` (the bounded
reads a recorded tree and the store's earlier views are read through,
`_recorded_reading` and `earliest_naming`), and `_location_probe` for the
one reading of an error as nothing at a path (`path_gone`, `path_missing`,
ENG-217). It never
imports `location_git`, `location` or `githead`
(tests/test_import_graph.py), and the hook client loads it with
`location` (tests/test_membership_capture.py pins that set).
"""

from __future__ import annotations

import errno
import functools
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from . import gitconfig, gitdir_pin
from ._location_probe import path_gone, path_missing
from .location_reads import NOT_READ, UNANSWERED, bounded_read, bounded_reads

# git refuses a .git file over 1 MiB (read_gitfile_gently in setup.c).
_GITFILE_LIMIT = 1 << 20
# A ref file, and a ``commondir`` file, hold a hash, one ref name or one
# path: 64 KiB is past any git writes (a path is at most PATH_MAX, 4096
# bytes on Linux). Files are read in chunks of the same size.
_REF_LIMIT = 1 << 16
_READ_CHUNK = 1 << 16
# validate_headref reads the first 255 bytes of HEAD (setup.c).
_HEADREF_READ = 255
# git's isspace (sane_ctype in ctype.c): space, tab, LF and CR. Python's
# default strip set adds VT and FF, which git keeps.
_SPACE = b" \t\n\r"
# validate_headref accepts a HEAD whose first 40 characters are hex digits
# of either case (get_oid_hex_any), whatever follows them.
_HEX_PREFIX = re.compile(rb"[0-9a-fA-F]{40}")
# SF_DATALESS (macOS <sys/stat.h>) and the Windows recall attributes
# (winnt.h): the file's data is held by a provider and a read would wait
# on a download.
_SF_DATALESS = 0x40000000
_WINDOWS_RECALL = 0x00040000 | 0x00400000
#: The ends of the refusals whose recorded working tree could not be read or
#: did not answer (`_recorded_reading`): the security audit's fix for them
#: says how the record is freed (`location_audit._gitfile_fix`, ENG-217).
UNREAD_TREE = ("whose .git could not be read", "which did not answer")
# How long a read of a recorded tree waits (`_recorded_reading`): the bound
# every recorded path is read within, `location_git.PROBE_BUDGET`, whose
# evidence is a view's read of about 1 ms on a local disk against a hung
# share that holds it indefinitely. This module cannot import
# `location_git`, which imports it, so tests/test_location_bounds.py holds
# the two equal.
_RECORDED_BUDGET = 0.5


def _gitfile_target(dotgit: str) -> str | None:
    """The directory a ``gitdir: <path>`` file names, or None."""
    try:
        with open(dotgit, "rb") as fh:
            data = fh.read(_GITFILE_LIMIT + 1)
    except OSError:
        return None
    return _parse_gitfile(data, dotgit)


def _parse_gitfile(data: bytes, dotgit: str) -> str | None:
    """The directory the bytes `data` of the ``.git`` file `dotgit` name
    after ``gitdir:`` (taken from the file's folder when relative), or None
    when they are not such a file."""
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
    deleted since git wrote it: the OS answered so, `path_missing`; a share
    that does not answer is False, ENG-217)."""
    path = raw if os.path.isabs(raw) else os.path.join(base, raw)
    try:
        theirs = os.stat(path)
    except (OSError, ValueError) as exc:
        return None if path_missing(exc) else False
    try:
        return os.path.samestat(theirs, os.stat(here))
    except (OSError, ValueError):
        return False


def gitfile_refusal(directory: str, dotgit: str, target: str) -> str | None:
    """Why the ``.git`` entry `dotgit` in `directory`, which names the git
    directory `target` (a ``.git`` file's ``gitdir:``, or the directory a
    ``.git`` symbolic link reaches), is not followed (ENG-158), or None
    when it is.

    A ``.git`` file is followed where git's own pointers say the folder
    is that git directory's working tree: a linked worktree's git
    directory (``<common>/worktrees/<name>``) whose ``gitdir`` file names
    a ``.git`` in this folder, and a git directory whose ``core.worktree``
    names this folder (a submodule's, under the enclosing repository's
    ``modules/``). For a git directory kept apart by
    ``--separate-git-dir`` git writes no pointer back, so bettermemory
    keeps one (`gitdir_pin`, ENG-206): it is followed from the folder the
    first capture followed it from, and from no other while that folder's
    ``.git`` still names it, or cannot be read. A pointer that names
    nothing any more (the worktree, the submodule or the separate git
    directory's tree moved by a plain ``mv``, which ``git worktree
    repair`` mends for a worktree) is followed too. Anything else is
    refused: another working tree's own ``.git``, a worktree's git
    directory that names another worktree, a git directory whose
    ``core.worktree`` names another folder, a separate git directory
    recorded for another folder, and a bare repository. Followed, such a
    file would key the folder on another checkout's git directory, and an
    extracted archive or a cloned repository that planted one would be
    delivered that checkout's memories.

    Each is told by the filesystem object, not the spelling, and each
    pointer back is compared with this folder, not with the ``.git``
    entry, so a ``.git`` here that is a symbolic or hard link to the right
    ``.git`` file names this folder no more than a copy of it does: a
    target that is the ``.git`` of a folder, through a symlink or in
    another case on a case-insensitive volume, is that folder's own; a
    ``commondir`` is trusted only for a git directory registered under it
    (``<common>/worktrees/<name>``, where git keeps a linked worktree's),
    so a forged worktree directory names no common directory."""
    return _refusal(directory, target)[0]


def _refusal(
    directory: str, target: str, common: tuple[str, bool] | None = None
) -> tuple[str | None, bool]:
    """`gitfile_refusal`'s answer for the folder `directory` and the git
    directory `target`, and whether a follow is provisional (`Entry`): it
    follows a git directory kept apart that no record names `directory` for.
    `common` is `target`'s common directory as git reads it, where the
    caller read it (`tree_format`)."""
    _common, linked = _common_of(target)
    named = None if linked else _commondir(target)
    if named is not None:
        return _not_registered(target, named), False
    found = tree_format(target, common)
    if linked:
        back = _small_text(os.path.join(target, "gitdir"))
        if not back:
            return (
                f"{target} is a worktree's git directory that names no worktree",
                False,
            )
        if _names_folder(back, target, directory) is False:
            return f"{target} is the git directory of the worktree at {back}", False
        # Its config.worktree, under extensions.worktreeConfig, can move it.
        return _tree_refusal(target, found, directory), False
    why = _tree_refusal(target, found, directory, moved=True)
    if why is not None or found.worktree is not None:
        return why, False
    tree = _working_tree_of(target)
    if tree is not None and _same_folder(tree, directory) is not True:
        return f"{target} is the git directory of the working tree {tree}", False
    why, here = _recorded_reading(gitdir_pin.recorded(target), target, directory)
    # Followed with no record naming this folder: membership's next
    # capture records a tree, which may be another folder (`Entry`).
    return why, why is None and not here


@dataclass(frozen=True)
class TreeFormat:
    """Where a git directory's own config puts its working tree, as git
    reads it (`tree_format`): `why` git's read of it is not known (git dies
    reading it, or it is not read whole here), else the ``core.worktree``
    git takes from it (None where it takes none) and whether the
    ``core.bare`` it takes is true."""

    why: str | None = None
    worktree: str | None = None
    bare: bool = False


# The variables git's read of a repository's format takes (setup.c's
# ``check_repo_format`` and ``read_worktree_config``), as `gitconfig`
# spells a name.
_VERSION = b"core.repositoryformatversion"
_WORKTREE_CONFIG = b"extensions.worktreeconfig"
_OBJECT_FORMAT = b"extensions.objectformat"
_COMPAT_FORMAT = b"extensions.compatobjectformat"
_BARE = b"core.bare"
_WORKTREE = b"core.worktree"
_EXTENSIONS = b"extensions."
# The four and every extension, as `gitconfig.read_git_config` keeps a
# reading of them alone (a name ending in "." keeps those it starts).
_FORMAT_NAMES = frozenset({_VERSION, _WORKTREE_CONFIG, _BARE, _WORKTREE, _EXTENSIONS})
# The extensions git 2.54 reads in a repository of any format (setup.c's
# ``handle_extension_v0``) and those of format 1 alone (``handle_extension``),
# each with the values it reads: a boolean (`gitconfig.git_bool`), any
# value but none, one of a set of names, or anything. Checked against
# ``git rev-parse`` (tests/test_location_gitdir_format.py).
_BOOLEAN, _SOME_VALUE, _ANY = "boolean", "some value", "any"
_EXTENSIONS_V0: dict[bytes, str | frozenset[bytes]] = {
    b"noop": _ANY,
    b"preciousobjects": _BOOLEAN,
    b"partialclone": _SOME_VALUE,
    b"worktreeconfig": _BOOLEAN,
}
_EXTENSIONS_V1: dict[bytes, str | frozenset[bytes]] = {
    b"noop-v1": _ANY,
    b"objectformat": frozenset({b"sha1", b"sha256"}),
    b"refstorage": frozenset({b"files", b"reftable"}),
    b"relativeworktrees": _BOOLEAN,
    b"submodulepathconfig": _BOOLEAN,
    # Read once (a second dies) and kept where a version is set, where git
    # reads it as it was built (`_judge_format`).
    b"compatobjectformat": frozenset({b"sha1", b"sha256"}),
}


def tree_format(git_dir: str, common: tuple[str, bool] | None = None) -> TreeFormat:
    """Where git puts the working tree of the git directory `git_dir` by
    its own config (ENG-211), read as setup.c's
    ``check_repository_format_gently`` reads it (git 2.54).

    git reads ``config`` in the common directory (the one a ``commondir``
    file names, `_git_common`, else `git_dir`), parsed as git parses a
    file (`gitconfig.read_git_config`: whole, no include, a NUL byte
    ending a name), and takes ``core.repositoryformatversion``,
    ``extensions.worktreeConfig``, ``core.bare`` and ``core.worktree``
    from it, the last of each standing. A value git dies on (no version
    number, no boolean, a ``core.worktree`` with no ``=``) is `why`, as is
    a file git cannot parse, one larger than `gitconfig._FORMAT_LIMIT` and
    one that cannot be read (fail closed). A version that is not set or is
    below 0 makes git take neither ``core.bare`` nor ``core.worktree``
    (``read_repository_format`` clears the format), and one above 1 makes
    it die. Under ``extensions.worktreeConfig`` git reads
    ``config.worktree`` in `git_dir` after it, whose ``core.bare`` and
    ``core.worktree`` win, and takes the two whether or not a
    ``commondir`` named the common directory; without it, a git directory
    with a ``commondir`` (a linked worktree's) takes neither from the
    common config. Its extensions are read as git 2.54 reads them
    (`_common_format`; ENG-207's round 7, which they were not): a value
    git refuses, a format-1 extension under format 0 and one git does not
    know under format 1 make git die, and are `why`. `common` is
    `_git_common`'s answer where the caller read it already (`judge`)."""
    try:
        where, has_common = common or _git_common(git_dir)
    except _Unsettled:
        return TreeFormat(f"git cannot read {os.path.join(git_dir, 'commondir')}")
    read = _common_format(where)
    if isinstance(read, str):
        return TreeFormat(read)
    version, worktree_config, taken = read.version, read.worktree_config, read.taken
    if version < 0:
        return TreeFormat()
    if version > 1:
        path = os.path.join(where, "config")
        return TreeFormat(f"{path} sets a repository format version git does not read")
    if worktree_config:
        path = os.path.join(git_dir, "config.worktree")
        own = gitconfig.read_git_config(path, _FORMAT_NAMES)
        if own.why is not None:
            return TreeFormat(own.why)
        for name, value in own.entries:
            if not _take(name, value, taken):
                return TreeFormat(_dies(path, name))
    elif has_common:
        return TreeFormat()
    worktree = taken.get(_WORKTREE)
    return TreeFormat(
        worktree=None if worktree is None else os.fsdecode(worktree.split(b"\0", 1)[0]),
        bare=_BARE in taken and gitconfig.git_bool(taken[_BARE]) is True,
    )


class _Format(NamedTuple):
    """A repository's format as `_judge_format` reads it."""

    #: ``core.repositoryformatversion``, -1 where it is not set.
    version: int
    #: ``extensions.worktreeConfig``.
    worktree_config: bool
    #: The ``core.bare`` and ``core.worktree`` set (`_take`).
    taken: dict[bytes, bytes | None]
    #: ``extensions.objectFormat``, b"sha1" where it is not set; git
    #: reads sha1 where no version is set, whatever it says (`object_format`).
    object_format: bytes


def _common_format(common_dir: str) -> _Format | str:
    """The format the config in `common_dir` gives a git directory, as
    setup.c's ``read_repository_format`` reads it (`tree_format`,
    `_judge_format`); or why it is not known: a value git dies on, an
    extension the version set refuses, a file git cannot parse, one larger
    than `gitconfig._FORMAT_LIMIT` and one that cannot be read."""
    path = os.path.join(common_dir, "config")
    read = gitconfig.read_git_config(path, _FORMAT_NAMES)
    if read.why is not None:
        return read.why
    return _judge_format(path, read.entries)


def object_format_at(common_dir: str) -> bytes | None:
    """The object format in `common_dir`'s bounded config (`object_format`).

    This uses the same format judgment and no includes, global config or
    worktree config: those do not set the repository's storage algorithm.
    None where that file or its declared format cannot be read.
    """
    read = _common_format(common_dir)
    if isinstance(read, str) or read.version > 1:
        return None
    return b"sha1" if read.version == -1 else read.object_format


def object_format(data: bytes) -> bytes | None:
    """The object format git 2.54 reads a repository in, from the bytes
    `data` of its common directory's ``config`` (setup.c's
    ``read_repository_format``, parsed as git parses the file, no include
    followed): b"sha1" or b"sha256", b"sha1" where no
    ``core.repositoryformatversion`` is set (git clears every extension
    there, ``objectFormat`` with them) or the file sets none; None where
    git dies on the format (`_judge_format`, a version above 1, a
    ``compatObjectFormat`` the git on PATH dies on or has not said it
    reads), cannot parse the file, or the file is larger than
    `gitconfig._FORMAT_LIMIT`.
    `gitobjects.ObjectReader` sizes object names by it (ENG-207), where a
    line reader of its own took ``objectFormat`` under no version."""
    if len(data) > gitconfig._FORMAT_LIMIT:
        return None
    entries = gitconfig.git_entries(data)
    if entries is None:
        return None
    read = _judge_format("config", entries)
    if isinstance(read, str) or read.version > 1:
        return None
    return b"sha1" if read.version == -1 else read.object_format


def _judge_format(
    path: str, entries: Sequence[tuple[bytes, bytes | None]]
) -> _Format | str:
    """The format the config file `path`, whose entries are `entries`,
    gives a git directory (`_Format`), the last of each name standing; or
    why git dies reading it: a value git dies on (an extension's among
    them, `_EXTENSIONS_V0`, `_EXTENSIONS_V1`) or an extension the version
    set refuses."""
    version, worktree_config, hash_name = -1, False, b"sha1"
    taken: dict[bytes, bytes | None] = {}
    v1_only = unknown = False
    compat: bytes | None = None
    for name, value in entries:
        if name == _VERSION:
            number = gitconfig.git_int(value)
            if number is None:
                return _dies(path, name)
            version = number
        elif name.startswith(_EXTENSIONS):
            ext = name[len(_EXTENSIONS) :]
            rule = _EXTENSIONS_V0.get(ext, _EXTENSIONS_V1.get(ext))
            if rule is not None and not _extension_reads(rule, value):
                return _dies(path, name)
            if name == _COMPAT_FORMAT:
                # setup.c's ``handle_extension`` takes it once, whatever
                # the version: a second dies ("already specified").
                if compat is not None:
                    return _dies(path, name)
                compat = b"" if value is None else value.split(b"\0", 1)[0]
            v1_only = v1_only or ext in _EXTENSIONS_V1
            unknown = unknown or rule is None
            if name == _WORKTREE_CONFIG:
                worktree_config = gitconfig.git_bool(value) is True
            elif name == _OBJECT_FORMAT and value is not None:
                hash_name = value.split(b"\0", 1)[0]
        elif not _take(name, value, taken):
            return _dies(path, name)
    # setup.c's ``verify_repository_format``, on a version that is set: a
    # format-1 extension in a repository of format 0, an extension git
    # does not know in one of format 1 (ENG-207's round 7).
    if version == 0 and v1_only:
        return f"{path} sets an extension of format 1 in a repository of format 0"
    if version >= 1 and unknown:
        return f"{path} sets a repository extension git does not know"
    # A format git does not clear (a version of 1 or below -1) keeps the
    # compat format. One the same as the object format no git from 2.45
    # reads: a build without Rust dies on any ("compatibility hash
    # algorithm support requires Rust"), and every other aborts at
    # repository.c's ``BUG("hash_algo and compat_hash_algo match")``, so
    # git is not asked of it (gitsystem's docstring). git 2.44 and earlier
    # know no such extension: they die on it under format 1, and below -1
    # they read the repository, as setup.c there verifies no extension of
    # a negative format; the format is declined either way, which fails
    # closed against those (ENG-207's review of round 10). Another git
    # reads as it was built, which the file does not say: the git on PATH
    # is asked under format 1 (`gitsystem.reads_compat`, ENG-207's round
    # 8), and the answer read below -1 too, as git from 2.45 reads both
    # alike; one that dies under format 1 declines below -1 where 2.44 and
    # earlier read it (fail closed again), and where git has not said the
    # format is not known here (fail closed).
    if compat is not None and version != -1 and version <= 1:
        if compat == hash_name:
            return (
                f"{path} sets a compatibility object format the same as its "
                "object format, which no git from 2.45 reads"
            )
        reads = _reads_compat()
        if reads is None:
            return (
                f"{path} sets a compatibility object format git has not said it reads"
            )
        if not reads:
            return (
                f"{path} sets a compatibility object format this git dies on "
                "under format 1"
            )
    return _Format(version, worktree_config, taken, hash_name)


def _reads_compat() -> bool | None:
    """Whether the git on PATH reads a compatibility object format other
    than its repository's object format (`gitsystem.reads_compat`),
    imported here, where a repository sets one: no other format's
    reading states its binary."""
    from . import gitsystem

    return gitsystem.reads_compat()


def _extension_reads(rule: str | frozenset[bytes], value: bytes | None) -> bool:
    """Whether git reads an extension's `value` by its `rule`
    (`_EXTENSIONS_V0`, `_EXTENSIONS_V1`), and does not die on it."""
    if isinstance(rule, frozenset):
        return value is not None and value.split(b"\0", 1)[0] in rule
    if rule == _BOOLEAN:
        return gitconfig.git_bool(value) is not None
    return rule == _ANY or value is not None


def reads_worktree_config(common_dir: str) -> bool | None:
    """Whether git reads ``config.worktree`` in a git directory of the
    repository whose common directory is `common_dir`, after its common
    config, for every variable (ENG-207's review, R14): where the common
    config file itself sets ``extensions.worktreeConfig`` true, read as
    setup.c reads a format (`_common_format`: no include followed, so one
    an include sets is not taken, as git does not take it) and as git
    then keeps it: a ``core.repositoryformatversion`` that is not set
    clears it (``read_repository_format``), and git 2.54 keeps it under
    any other version it reads. None where git dies on that config: a
    version above 1, a value or a file git refuses (`_common_format`),
    where git reads no variable of the repository at all. Checked against
    git's ``git remote`` (tests/test_location_git_config.py)."""
    read = _common_format(common_dir)
    if isinstance(read, str):
        return None
    if read.version > 1:
        return None
    return read.worktree_config and read.version != -1


def _take(name: bytes, value: bytes | None, taken: dict[bytes, bytes | None]) -> bool:
    """Keep `value` in `taken` where `name` is ``core.bare`` or
    ``core.worktree`` (setup.c's ``read_worktree_config``); False where git
    dies on it: a ``core.bare`` that is no boolean, a ``core.worktree``
    with no ``=``."""
    if name == _BARE:
        if gitconfig.git_bool(value) is None:
            return False
    elif name == _WORKTREE:
        if value is None:
            return False
    else:
        return True
    taken[name] = value
    return True


def _dies(path: str, name: bytes) -> str:
    """Why a git directory's format is not known where git dies on the
    variable `name` in the config file `path`."""
    return f"{path} sets {name.decode()} to a value git dies on"


class FormatUnknown(str):
    """A refusal (`_tree_refusal`) where git dies on the format of the git
    directory it reads (a config line its parser refuses, a value it dies
    on, an empty ``core.worktree``) or that format is not known here: git
    goes no further there, and a capture declines rather than read the
    folder as outside a repository (`location_git.GitDies`, ENG-207's
    round 6). It is the refusal's text, kept as the type it is through
    the readers that pass it on."""

    __slots__ = ()


def _tree_refusal(
    git_dir: str, found: TreeFormat, directory: str, *, moved: bool = False
) -> str | None:
    """Why the git directory `git_dir`, whose own config puts its working
    tree as `found` says (`tree_format`), is not read as the git directory
    of the folder `directory`, or None where it is: its format is not
    known, its ``core.bare`` is true (git reads no working tree, and it
    wins over a ``core.worktree``), or its ``core.worktree`` is empty (git
    dies) or names another folder, compared as filesystem objects, a
    relative path taken from `git_dir` as git takes it. With `moved` a
    ``core.worktree`` naming nothing is read as this folder's (a
    submodule's tree moved with a plain ``mv``, ENG-158). Where git dies,
    or the format is not known, the refusal is a `FormatUnknown`."""
    if found.why is not None:
        return FormatUnknown(
            f"{git_dir}'s config cannot be read as git reads it: {found.why}"
        )
    if found.bare:
        return f"{git_dir}'s config names no working tree (core.bare)"
    if found.worktree is None:
        return None
    if not found.worktree:
        return FormatUnknown(
            f"{git_dir}'s config names no working tree (an empty core.worktree, "
            "on which git dies)"
        )
    here = _names_here(found.worktree, git_dir, directory)
    if here is True or (here is None and moved):
        return None
    return f"{git_dir}'s config names the working tree {found.worktree} (core.worktree)"


def own_refusal(
    dotgit: str, directory: str, common: tuple[str, bool] | None = None
) -> str | None:
    """Why the folder `directory`'s own ``.git`` directory `dotgit` (no
    link) is not followed (ENG-211), or None where it is.

    git reads that directory's config for where its working tree is
    (`tree_format`), and a ``core.worktree`` there moves the tree off the
    folder: ``git rev-parse --show-toplevel`` from the folder prints the
    folder it names, so an origin captured there carried that toplevel
    while membership rooted the checkout at the folder. git writes no
    pointer back from that folder (a ``git init`` with ``GIT_DIR`` and
    ``GIT_WORK_TREE``, or a ``git config``), so nothing tells the folder it
    names from any other, and an archive can carry such a config naming a
    folder of the user's. Rooted there, membership would read that
    folder's project file and content for a checkout that is not its own.
    So it is followed only where git puts its working tree at this folder,
    or puts none by its config (`_tree_refusal`), and refused otherwise, as
    a ``.git`` file naming such a git directory is (`gitfile_refusal`): a
    ``core.worktree`` naming another folder or nothing, a ``core.bare``
    that is true, and a config git dies on or that is not read whole here.
    Refused, it is passed over, and the origin family runs no git in the
    folder (`discovery_refusal`); where git runs, the origin keeps only a
    toplevel that is the folder membership roots the checkout at
    (`toplevel_agrees`), so the two never name two roots. `common` is as
    `tree_format` takes it."""
    return _tree_refusal(dotgit, tree_format(dotgit, common), directory)


def inside_reading(git_dir: str) -> tuple[str | None, str | None]:
    """``(why, root)`` for the git directory `git_dir` where git's walk
    stops at it as the folder it reads (a walk started inside a git
    directory, ENG-211): `why` it is not read as a repository, else the
    folder membership roots it at, None for a bare repository.

    git reads no working tree there unless the config names one
    (``core.worktree``, `tree_format`), and then roots the repository
    there. Membership reads such a folder as the git directory of the
    folder above it where it is a ``.git`` that is not bare, and as a bare
    repository otherwise; a ``core.worktree`` (a submodule's git directory
    under ``modules/``, a ``.git`` set by ``git config``) makes it a
    refusal unless it names the folder above a ``.git`` that is not bare,
    and so does a config whose format is not known (`_tree_refusal`)."""
    found = tree_format(git_dir)
    parent = os.path.dirname(git_dir)
    dotgit = os.path.basename(git_dir) == ".git"
    if found.why is None and found.worktree is None:
        return None, parent if dotgit and not found.bare else None
    why = _tree_refusal(git_dir, found, parent)
    if why is None and not dotgit:
        why = f"{git_dir}'s config names the working tree {found.worktree} (core.worktree)"
    return why, parent if why is None else None


def inside_refusal(git_dir: str) -> str | None:
    """`inside_reading`'s `why` alone."""
    return inside_reading(git_dir)[0]


def _recorded_refusal(
    pin: gitdir_pin.Pin | None, target: str, directory: str
) -> str | None:
    """Why the git directory kept apart `target` is not followed from the
    folder `directory`, by the working tree `pin` records for it
    (ENG-206), or None: nothing recorded, the record names `directory`, or
    the recorded folder's ``.git`` is gone or names another git directory
    now (the tree moved or was removed), so the follow is recorded in its
    place. A recorded ``.git`` that cannot be read (a share answering with
    an error, a permission refused, a provider's placeholder:
    `_entry_names`) or does not answer within `_RECORDED_BUDGET` refuses
    the follow and keeps the record: what cannot be told is not given
    away."""
    return _recorded_reading(pin, target, directory)[0]


def _recorded_reading(
    pin: gitdir_pin.Pin | None, target: str, directory: str
) -> tuple[str | None, bool]:
    """`_recorded_refusal`'s answer, and whether `pin` names `directory`.

    The recorded folder may sit on a share that no longer answers (a
    hard-mounted NFS export, a dropped SMB share), and every reader of the
    rule reaches it from a folder naming its git directory: membership's
    capture, `githead.signature` under each origin capture and commit-drift
    check, and origin's walk before git runs (ENG-193). So it is read as
    membership reads every recorded path (ENG-156), on a worker thread
    through `location_reads.bounded_read` and waited on for
    `_RECORDED_BUDGET`: one that does not answer refuses the follow and
    keeps the record, as one that cannot be read does, and while that read
    is in flight every later read of the folder answers at once. A record
    naming `directory` by its spelling or by its resolved path (the one
    records keep, `location.record_working_tree`) names it with no read
    there and no thread: the common case of a tree read from itself, by
    whatever symbolic link the caller reached it through (membership's
    `location.signature` walks the cwd as given). Resolving `directory`
    touches only what reaching it already touched, the caller's own path."""
    if pin is None:
        return None, False
    if _spells(pin.tree, directory):
        return None, True
    found = bounded_read(
        pin.tree,
        ("recorded tree", target, directory),
        lambda: _read_recorded(pin, target, directory),
        _RECORDED_BUDGET,
    )
    if found is UNANSWERED:
        return (
            f"{target} is recorded as the git directory of the working tree "
            f"{pin.tree}, {UNREAD_TREE[1]}"
        ), False
    answer: tuple[str | None, bool] = found
    return answer


def _spells(recorded: str, directory: str) -> bool:
    """Whether the recorded path `recorded` spells the folder `directory`
    as given or as resolved through its symbolic links."""
    if os.path.normcase(recorded) == os.path.normcase(directory):
        return True
    try:
        real = os.path.realpath(directory)
    except (OSError, ValueError):
        return False
    return os.path.normcase(recorded) == os.path.normcase(real)


def _read_recorded(
    pin: gitdir_pin.Pin, target: str, directory: str
) -> tuple[str | None, bool]:
    """`_recorded_reading` of the recorded folder, unbounded: the reads it
    makes there."""
    if _same_folder(pin.tree, directory) is True:
        return None, True
    entry = _entry_names(os.path.join(pin.tree, ".git"), target)
    if entry == "names":
        return f"{target} is the git directory of the working tree {pin.tree}", False
    if entry == "unknown":
        return (
            f"{target} is recorded as the git directory of the working tree "
            f"{pin.tree}, {UNREAD_TREE[0]}"
        ), False
    return None, False


def earliest_naming(views: Sequence[str], tree: str | None, git_dir: str) -> str | None:
    """The folder to record as the working tree of the git directory
    `git_dir` in place of the folder `tree`, which made its first record,
    from the folders `views`, earliest first (the store's views of its
    checkout, `location._earlier_tree`): the first that does not answer or
    cannot be read, as the store spells it, or the first whose ``.git``
    entry names the git directory (`names_git_dir`), resolved through its
    links; None where none does, or where one before them is the folder
    `tree` itself.

    The views are recorded paths, and one may sit on a share that does
    not answer, or answers late (a cold SMB share, a provider's folder).
    Each is read as a recorded tree is (`_recorded_reading`, ENG-156), on
    a worker thread within `_RECORDED_BUDGET`, all side by side
    (`location_reads.bounded_reads`), so the views of one dead share cost
    one budget together. A view that does not answer may still name the
    git directory, and so may one whose ``.git`` read fails with any error
    but those that say nothing is there (`_entry_names`: a share answering
    with ENOTCONN, EIO, ESTALE or EACCES, a provider's placeholder, which
    is not read, or a read that raised), so each takes the record, failing
    closed as a recorded tree does (ENG-217): the folder `tree` then reads
    it as a recorded tree that did not answer, or could not be read, and
    is refused (a plant seen first while the checkout's own share is slow
    or failing takes nothing). The record stands while the view does not
    answer or cannot be read, as a recorded tree's does; once its ``.git``
    reads, or is gone (the share unmounted: ENOENT), it stands while that
    ``.git`` still names the git directory and gives way to the next
    folder followed otherwise. Passed over, such a view left the record
    with `tree` for good. The cost is that of a recorded tree that hangs
    or fails: a checkout moved off a share that hangs or answers with
    errors is refused at its new folder until the old share answers
    readably or is unmounted (the security audit's fix says so,
    `UNREAD_TREE`), and so is a folder naming a git directory whose inode
    number such a view's checkout shares on another device, where either
    filesystem reports no birth time that tells objects apart
    (`ObjectKey.on_any_device`)."""
    jobs = [
        (
            view,
            ("earlier view", git_dir, tree),
            functools.partial(_view, view, tree, git_dir),
        )
        for view in views
    ]
    for view, seen in zip(views, bounded_reads(jobs, _RECORDED_BUDGET)):
        if seen is UNANSWERED or seen is NOT_READ:
            return view
        verdict, path = seen
        if verdict == "here":
            return None
        if verdict == "unknown":
            return view
        if verdict == "names":
            return str(path)
    return None


def _view(view: str, tree: str | None, git_dir: str) -> tuple[str, str]:
    """`earliest_naming`'s read of the folder `view`, unbounded: ``here``
    where it is the folder `tree`, else what its ``.git`` names
    (`_entry_names`), with the folder resolved where that is the git
    directory, so no read of it is made outside the bound."""
    if tree is not None and _same_folder(view, tree) is True:
        return "here", view
    seen = _entry_names(os.path.join(view, ".git"), git_dir)
    return seen, os.path.realpath(view) if seen == "names" else view


def names_git_dir(entry: str, git_dir: str) -> bool:
    """Whether the ``.git`` entry `entry` (a ``.git`` file, or a link to a
    directory) names the git directory `git_dir`, compared as filesystem
    objects."""
    return _entry_names(entry, git_dir) == "names"


def _entry_names(entry: str, git_dir: str) -> str:
    """What the ``.git`` entry `entry` names, against the git directory
    `git_dir`: ``names`` (it is a ``.git`` file whose ``gitdir:`` reaches
    `git_dir`, or reaches it as a directory itself), ``other`` (it reaches
    something else, or is not a pointer), ``gone`` (the OS answered that
    nothing is there, `path_gone`, or a link that reaches nothing) or
    ``unknown`` (a read failed with any other error, a share's ENOTCONN,
    EIO or ESTALE, a refused permission, so what is there cannot be told,
    or it is a provider's placeholder, which is not read: a read would
    fetch it, and wait on the download, ENG-217)."""
    try:
        lst = os.lstat(entry)
    except (OSError, ValueError) as exc:
        return "gone" if path_gone(exc) else "unknown"
    if _is_link(lst):
        # A link reaching another working tree's own `.git` names nothing
        # for this folder, as discovery reads it (`judge`).
        owner = _working_tree_of(entry)
        if (
            owner is not None
            and _same_folder(owner, os.path.dirname(entry)) is not True
        ):
            return "other"
    try:
        st = os.stat(entry)
        want = os.stat(git_dir)
    except (OSError, ValueError) as exc:
        return "gone" if path_gone(exc) else "unknown"
    if stat.S_ISDIR(st.st_mode):
        return "names" if os.path.samestat(st, want) else "other"
    if not stat.S_ISREG(st.st_mode):
        return "other"
    if _placeholder(st):
        return "unknown"
    try:
        with open(entry, "rb") as fh:
            data = fh.read(_GITFILE_LIMIT + 1)
    except (OSError, ValueError) as exc:
        return "gone" if path_gone(exc) else "unknown"
    named = _parse_gitfile(data, entry)
    if named is None:
        return "other"
    return "names" if _names_here(named, git_dir, git_dir) is True else "other"


def _names_folder(raw: str, base: str, directory: str) -> bool | None:
    """Whether the ``.git`` path `raw` (taken from `base` when relative),
    as a worktree's ``gitdir`` file names it, sits in the folder
    `directory`: None when nothing is at `raw` any more (the worktree was
    moved or removed: the OS answered so, `path_missing`), True when its
    folder is `directory`, False otherwise, a share that does not answer
    included (ENG-217)."""
    path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
    try:
        os.lstat(path)
    except (OSError, ValueError) as exc:
        return None if path_missing(exc) else False
    return _same_folder(os.path.dirname(path), directory) is True


def _same_folder(a: str, b: str) -> bool | None:
    """Whether the paths `a` and `b` reach one folder: None when `a`
    reaches nothing (`path_missing`), False when it reaches another or
    cannot be told."""
    try:
        theirs = os.stat(a)
    except (OSError, ValueError) as exc:
        return None if path_missing(exc) else False
    try:
        return os.path.samestat(theirs, os.stat(b))
    except (OSError, ValueError):
        return False


# The reparse tag of a Windows directory junction (``mklink /J``): CPython's
# ``lstat`` sets ``S_IFLNK`` only for a symbolic link's tag, so a junction
# reads as a directory, yet it leads elsewhere as a link does.
_JUNCTION_TAG = 0xA0000003


def _is_link(st: os.stat_result) -> bool:
    """Whether the ``lstat`` result `st` is of an entry that leads
    elsewhere: a symbolic link, or a Windows directory junction."""
    return stat.S_ISLNK(st.st_mode) or (
        getattr(st, "st_reparse_tag", 0) == _JUNCTION_TAG
    )


def _working_tree_of(path: str) -> str | None:
    """The folder whose own ``.git`` the entry `path` is (`_tree_of`),
    where that folder is a working tree: it holds something besides its
    ``.git``, or cannot be listed. A folder holding its ``.git`` alone (a
    git directory moved out of a synced folder, with a link left in its
    place) is no working tree, so a ``.git`` that reaches it is judged as
    one reaching a git directory kept apart (ENG-206)."""
    owner = _tree_of(path)
    if owner is None:
        return None
    try:
        with os.scandir(owner) as entries:
            if all(entry.name == ".git" for entry in entries):
                return None
    except OSError:
        pass
    return owner


def _tree_of(git_dir: str) -> str | None:
    """The folder whose own ``.git`` the entry `git_dir` (a git directory,
    or a ``.git`` file) is, or None: compared as filesystem objects, so a
    symlink to it or a spelling in another case on a case-insensitive
    volume is the same ``.git``."""
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


def _not_registered(git_dir: str, common: str) -> str:
    """Why a git directory whose ``commondir`` names `common` is not
    followed: it is not one of that repository's worktrees."""
    return (
        f"{git_dir} names the common directory {common} but is not one of its worktrees"
    )


def _foreign_commondir(git_dir: str) -> str | None:
    """The common directory `git_dir`'s ``commondir`` file names, where
    that is a repository's (``objects`` and ``refs``) and `git_dir` is not
    registered under it: git reads `git_dir` as a git directory of that
    repository, membership as none (ENG-158, review M G5). None
    otherwise."""
    named = _commondir(git_dir)
    if named is None or _registered(git_dir, named):
        return None
    if os.path.isdir(os.path.join(named, "objects")) and os.path.isdir(
        os.path.join(named, "refs")
    ):
        return named
    return None


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
    """Membership's reading of a folder git reads as a git directory (a
    bare repository, or the inside of one; `_git_reads` says whether git
    does): a ``HEAD``, and ``objects`` and ``refs`` in the common directory
    `_common_of` trusts, which a ``commondir`` names only for a git
    directory registered under it."""
    if not os.path.lexists(os.path.join(path, "HEAD")):
        return False
    common, _ = _common_of(path)
    return os.path.isdir(os.path.join(common, "objects")) and os.path.isdir(
        os.path.join(common, "refs")
    )


def _placeholder(st: os.stat_result) -> bool:
    """Whether a file provider holds the data of the file `st` is the
    ``stat`` of elsewhere, so a read of it would wait on a download
    (design 3.1, D19): SF_DATALESS on macOS, the recall attributes on
    Windows."""
    return bool(
        getattr(st, "st_flags", 0) & _SF_DATALESS
        or getattr(st, "st_file_attributes", 0) & _WINDOWS_RECALL
    )


# ---------------------------------------------------------------------------
# git's own reading of a git directory
# ---------------------------------------------------------------------------


class _Unsettled(Exception):
    """The files do not settle what git would answer: git would die, block
    or read state these files do not hold."""


def _read(path: str | bytes, limit: int, *, follow: bool = True) -> bytes:
    """Up to ``limit + 1`` leading bytes of the regular file `path`, so a
    caller can tell a file longer than `limit` apart. With `follow` False a
    symbolic link fails with ELOOP instead of being read through. A
    directory raises IsADirectoryError and any other kind of file an
    OSError; the open does not block, so a FIFO fails instead of waiting
    for a writer. Windows refuses to open a directory at all, with
    PermissionError where POSIX opens it and the stat below tells it: a
    directory there raises IsADirectoryError too, so a caller reads a
    directory where a ref's file would be as git reads it on every
    platform (`githead._lookup`: no loose file)."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if not follow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except PermissionError:
        if _is_directory(path, follow=follow):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR)) from None
        raise
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


def _is_directory(path: str | bytes, *, follow: bool) -> bool:
    """Whether `path` is a directory, through a symbolic link only with
    `follow`; False where it cannot be stated."""
    try:
        status = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISDIR(status.st_mode)


def _real(path: str) -> str:
    """`path` with every symbolic link resolved. Where a component is
    missing git's ``strbuf_realpath`` dies or drops the path; here that
    declines."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError as exc:
        raise _Unsettled from exc


def _real_to_last(path: str) -> str:
    """`path` resolved as git's ``strbuf_realpath`` resolves it where it
    may die (``strbuf_add_real_path``): every component resolved through
    its symbolic links, the last allowed to be missing, any other missing
    component declining as git dies there. A path whose last component is
    missing comes back with that name below its resolved folder, which
    holds no ``objects`` then, so git's walk goes on past it."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError as exc:
        # Only ENOENT is git's missing component; a share that does not
        # answer, raised as the same class on Windows, declines (ENG-217).
        if not (isinstance(exc, FileNotFoundError) and path_missing(exc)):
            raise _Unsettled from exc
    parent, last = os.path.split(path.rstrip("/" + os.sep) or path)
    if last in ("", ".", ".."):
        raise _Unsettled
    return os.path.join(_real(parent), last)


def _valid_headref(path: str, *, fetch: bool = True, read: bool = True) -> bool:
    """git's ``validate_headref``: HEAD is a symbolic link into ``refs/``,
    a ``ref:`` line naming something under ``refs/``, or begins with a
    hash (`_headref_content`). Git reads the first 255 bytes. A HEAD that
    is neither a regular file, a directory nor a symbolic link declines:
    git's open of a FIFO waits for a writer, and its read of a device
    reads the device. With `fetch` False a HEAD that is a file provider's
    placeholder (`_placeholder`) is not read and passes by its presence:
    membership's walk reads no placeholder HEAD (design 3.1, D19), and git,
    which fetches it, finds the HEAD git wrote there; the walk's other
    small files are read as they are (`_git_common`). With `read` False a
    HEAD that is a regular file passes by its presence, unread: the caller
    reads it itself and tests what it read (membership's signature,
    `location.signature`)."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return os.readlink(os.fsencode(path)).startswith(b"refs/")
        if stat.S_ISDIR(st.st_mode):
            return False
        if not stat.S_ISREG(st.st_mode):
            raise _Unsettled
        if not read or (not fetch and _placeholder(st)):
            return True
        content = _read(path, _HEADREF_READ)
    except OSError:
        return False
    return _headref_content(content)


def _headref_content(content: bytes) -> bool:
    """git's ``validate_headref`` of a HEAD file whose leading bytes are
    `content`, of which git reads 255: a ``ref:`` line naming something
    under ``refs/``, or a hash."""
    content = content[:_HEADREF_READ]
    if content.startswith(b"ref:") and content[4:].lstrip(_SPACE).startswith(b"refs/"):
        return True
    return _HEX_PREFIX.match(content) is not None


def _git_common(gitdir: str) -> tuple[str, bool]:
    """git's ``get_common_dir_noenv``: the directory a ``commondir`` file
    names, relative to `gitdir` unless absolute and resolved as git
    resolves it (`_real_to_last`), or `gitdir` itself when there is no
    such file; and whether such a file named it, which decides what git
    takes from the common config (`tree_format`). Git dies on an empty or
    unreadable one, and on one whose path is missing a folder before its
    last. Unlike `_common_of`, which membership trusts a ``commondir`` by,
    this follows any, as git does. A ``commondir`` that is a file
    provider's placeholder is read, and so fetched, as `_common_of` reads
    it, and as membership reads a ``.git`` file, a worktree's ``gitdir``
    and the config the rule reads: of the walk's files only HEAD passes
    unread (`_valid_headref`)."""
    try:
        content = _read(os.path.join(gitdir, "commondir"), _REF_LIMIT)
    except OSError as exc:
        if path_missing(exc):
            return gitdir, False
        raise _Unsettled from exc
    target = content.rstrip(b"\r\n")
    if not content or len(content) > _REF_LIMIT or b"\0" in target:
        raise _Unsettled
    name = os.fsdecode(target)
    if not os.path.isabs(name):
        name = os.path.join(gitdir, name)
    return _real_to_last(name), True


def _git_directory(
    candidate: str, *, fetch: bool = True, read: bool = True
) -> str | None:
    """git's ``is_git_directory``: the common directory when `candidate`
    is a git directory, None when it is not (`_git_found`). Raises
    `_Unsettled` where git would die or block reading it."""
    found = _git_found(candidate, fetch=fetch, read=read)
    return None if found is None else found[0]


def _git_found(
    candidate: str, *, fetch: bool = True, read: bool = True
) -> tuple[str, bool] | None:
    """git's ``is_git_directory``: the common directory and whether a
    ``commondir`` named it (`_git_common`) when `candidate` is a git
    directory, None when it is not. Raises `_Unsettled` where git would
    die or block reading it. `fetch` and `read` are `_valid_headref`'s. Git
    checks ``$GIT_OBJECT_DIRECTORY`` in place of ``objects`` where that is
    set; no git the package runs sees it (`_git.SCRUBBED_ENV`), and
    membership reads each repository by its own files, so this checks
    ``objects`` whatever the environment holds."""
    if not _valid_headref(os.path.join(candidate, "HEAD"), fetch=fetch, read=read):
        return None
    common = _git_common(candidate)
    if os.access(os.path.join(common[0], "objects"), os.X_OK) and os.access(
        os.path.join(common[0], "refs"), os.X_OK
    ):
        return common
    return None


def _git_reads(candidate: str, *, fetch: bool = True, read: bool = True) -> bool | None:
    """Whether git's walk reads `candidate` as a git directory
    (`_git_directory`): None where git would die or block reading it
    instead (a ``HEAD`` that is a FIFO or a device, a ``commondir`` that
    cannot be read or whose path is missing a folder before its last), so
    the walk goes no further either. Membership's discovery and origin's
    walk both stop by this test (ENG-193), membership with `fetch` False
    and its signature with `read` False too (`_valid_headref`)."""
    found = _git_read(candidate, fetch=fetch, read=read)
    return None if found is False else found is not None


def _git_read(
    candidate: str, *, fetch: bool = True, read: bool = True
) -> tuple[str, bool] | Literal[False] | None:
    """`_git_found`, False where git would die or block reading
    `candidate` (`_git_reads`): the common directory read once, which the
    rule takes the git directory's format from (`tree_format`)."""
    try:
        return _git_found(candidate, fetch=fetch, read=read)
    except (_Unsettled, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# One `.git` entry, one folder, and the walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """What a ``.git`` entry names, as membership reads it (`judge`).

    `git_dir` is the git directory it reaches: the entry itself for a
    folder's own ``.git`` directory (`own`), else the directory a ``.git``
    file names or a link reaches. `why` says why it is not followed, None
    where it is. `provisional` marks a follow of a git directory kept apart
    (``--separate-git-dir``) that no record names this folder for: nothing
    is recorded, or the record gives way (`_recorded_refusal`). The next
    capture membership makes there records a tree, after checking it
    against the store's earlier views (`location._earlier_tree`), and that
    tree may be another folder, which refuses this one; so a reader that
    keeps what it read past the call, against stamps of the repository's
    files that do not see the record, keeps no such follow
    (`githead.signature`)."""

    git_dir: str
    why: str | None = None
    own: bool = False
    provisional: bool = False


def judge(
    directory: str,
    dotgit: str,
    st: os.stat_result,
    *,
    fetch: bool = True,
    read: bool = True,
) -> Entry | None:
    """The ``.git`` entry `dotgit` in `directory`, `st` its ``lstat``, as
    membership reads it: None where it names no git directory, else the
    git directory it names and whether it is followed.

    A git directory is what git's own walk reads as one (`_git_reads`,
    `fetch` and `read` as there): a ``.git`` directory, or one a link
    reaches, that fails git's test is none, and git's walk passes over it
    to the folder itself and the one above, as membership's does, so the
    two stop at one entry (ENG-193); one git would die or block reading is
    not followed. A
    ``.git`` file that names no git directory git reads, a link that
    reaches nothing and an entry that is neither a file nor a directory
    name none either.

    A ``.git`` directory that is no link is the folder's own, followed
    where its config gives the folder as its working tree (`own_refusal`,
    ENG-211). A link (a
    symbolic link, or a Windows junction, `_is_link`) that reaches another
    working tree's own ``.git`` is that tree's, whatever it holds
    (`_working_tree_of`, ENG-206); any other link to a git directory, and
    every ``.git`` file, is judged as a pointer to the git directory it
    reaches (`gitfile_refusal`)."""
    linked_entry = _is_link(st)
    if linked_entry:
        try:
            st = os.stat(dotgit)
        except (OSError, ValueError):
            return None
    common: tuple[str, bool] | Literal[False] | None = None
    if stat.S_ISDIR(st.st_mode):
        common = _git_read(dotgit, fetch=fetch, read=read)
        if common is False:
            return Entry(
                os.path.realpath(dotgit), f"git cannot read {dotgit} as a git directory"
            )
        if common is None:
            return None
    if linked_entry:
        # A `.git` that is a symbolic link reaching another folder's own
        # `.git` is that folder's, whatever it holds (ENG-206).
        owner = _working_tree_of(dotgit)
        if owner is not None and _same_folder(owner, directory) is not True:
            return Entry(
                os.path.realpath(dotgit), f"{dotgit} links to the .git of {owner}"
            )
    if stat.S_ISDIR(st.st_mode):
        if not linked_entry:
            return Entry(
                dotgit, own_refusal(dotgit, directory, common or None), own=True
            )
        # A link to a git directory is a pointer to it, read by the rule a
        # `.git` file's is (ENG-206).
        target = os.path.realpath(dotgit)
    elif stat.S_ISREG(st.st_mode):
        named = _gitfile_target(dotgit)
        common = None if named is None else _git_read(named, fetch=fetch, read=read)
        if named is None or not common:
            return None
        target = named
    else:
        return None
    why, provisional = _refusal(directory, target, common or None)
    return Entry(target, why, provisional=provisional)


@dataclass(frozen=True)
class Reading:
    """What one folder gives git's discovery, by membership's rule
    (`reading_at`): `why` where git would read a repository there that
    membership does not follow, None where the repository there is one
    membership follows; `provisional` as `Entry` says; `root` the folder
    membership roots that repository at (the folder holding a followed
    ``.git`` entry, or the one above a ``.git`` a walk started inside),
    None where it is refused or bare."""

    why: str | None = None
    provisional: bool = False
    root: str | None = None


def reading_at(directory: str) -> Reading | None:
    """What git's discovery meets in the folder `directory`, by
    membership's rule; None where git goes on to the folder above.

    Git reads the folder's ``.git`` entry first, then the folder itself as
    a git directory (a bare repository, or the inside of a git directory),
    and goes on where neither gives a repository (``setup.c``). A ``.git``
    file stops the walk whatever it names: git follows it, or dies on it.
    A ``.git`` directory, or a link reaching one, stops it only where it
    passes git's own test (`_git_directory`: a ``HEAD`` git's
    ``validate_headref`` accepts, ``objects`` and ``refs`` in the common
    directory), and so does the folder itself; git skips any other and
    goes on, so this does too (ENG-193), or a pointer above an entry git
    skips would read to git as its repository while the walk here stopped
    below it. Where git would die or block reading the candidate
    (a ``HEAD`` that is a FIFO, a ``commondir`` that names nothing), the
    walk stops too, with no repository.

    Where it stops, the entry gives what `judge` reads, which stops by the
    same test (membership's discovery reads it so too, ENG-193). A ``.git``
    file `judge` reads as naming no git directory (one that is not UTF-8,
    or names one git dies on) is a refusal, since git follows it or dies.
    An entry that cannot be read or leads nowhere is one git cannot follow
    either.

    A ``commondir`` is trusted only for a git directory registered under
    it (``<common>/worktrees/<name>``, `_registered`), as `_common_of`
    trusts one: a folder's own ``.git`` directory, or the folder itself,
    whose ``commondir`` names a repository it is not registered under is a
    refusal (`_foreign_commondir`, review M G5), since git reads that
    repository's refs, objects and config through it while membership
    reads none of them; so is the folder itself where its config names a
    working tree membership does not root it at (`inside_refusal`,
    ENG-211)."""
    dotgit = os.path.join(directory, ".git")
    try:
        st: os.stat_result | None = os.lstat(dotgit)
    except (OSError, ValueError):
        st = None
    if st is not None:
        found = _entry_reading(directory, dotgit, st)
        if found is not None:
            return found
    if not os.path.lexists(os.path.join(directory, "HEAD")):
        return None
    reads = _git_reads(directory)
    if reads is None:
        return Reading(f"git cannot read {directory} as a git directory")
    if not reads:
        return None
    named = _foreign_commondir(directory)
    if named is not None:
        return Reading(_not_registered(directory, named))
    why, root = inside_reading(directory)
    return Reading(why, root=root)


def _entry_reading(directory: str, dotgit: str, st: os.stat_result) -> Reading | None:
    """`reading_at` of the ``.git`` entry `dotgit` in `directory`, `st` its
    ``lstat``: None where git goes on to the folder itself."""
    seen = judge(directory, dotgit, st)
    if seen is None:
        try:
            through = os.stat(dotgit)
        except (OSError, ValueError):
            return None
        # Git follows a `.git` file or dies on it, and passes over anything
        # else `judge` reads as no git directory.
        if stat.S_ISREG(through.st_mode):
            return Reading(f"{dotgit} names no git directory membership follows")
        return None
    if seen.why is None and seen.own:
        named = _foreign_commondir(dotgit)
        if named is not None:
            return Reading(_not_registered(dotgit, named))
    return Reading(seen.why, seen.provisional, None if seen.why else directory)


def discovery_reading(start: str) -> Reading | None:
    """What git, run in the folder `start` as `_git.run_git` runs it (with
    ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_COMMON_DIR`` and
    ``GIT_OBJECT_DIRECTORY`` removed, `_git.SCRUBBED_ENV`), reads by
    membership's rule (`reading_at`): the reading of the first folder from
    `start` up where git's walk stops, or None where it stops at none.

    The walk goes from ``realpath(start)``, the path git starts from, up to
    the filesystem's root, past every ``GIT_CEILING_DIRECTORIES`` entry and
    filesystem boundary git stops at: it covers every folder git's walk
    reaches, so no entry git would follow is missed, and a refusal beyond
    where git stops refuses only what git answers the same way there, no
    repository."""
    try:
        here = os.path.realpath(start)
    except (OSError, ValueError):
        return None
    while True:
        found = reading_at(here)
        if found is not None:
            return found
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def discovery_refusal(start: str) -> str | None:
    """Why git, run in the folder `start`, would read a repository
    membership does not follow, or would die or block in its walk
    (`discovery_reading`), or None: the first folder from `start` up where
    git's walk stops gives a repository membership follows, or none does."""
    found = discovery_reading(start)
    return None if found is None else found.why


def toplevel_agrees(start: str, toplevel: str) -> bool:
    """Whether `toplevel`, the working tree git named for the folder
    `start` (``git rev-parse --show-toplevel``, run as `discovery_reading`
    says), is the folder membership's rule roots the repository git's walk
    reaches at (`Reading.root`), compared as filesystem objects, as
    membership keys a root (`location.object_key`, its device and inode).

    This is what keeps the two readers from naming two roots whatever the
    rule's reading of a config misses (ENG-211): git reads the config
    itself, and an origin keeps the toplevel it prints only where this
    holds (`origin._probe_worktree_root`, `origin.repo_toplevel`,
    `origin.repo_toplevel_and_head`). False where the walk reaches no
    repository membership follows, or a bare one, or either path cannot be
    read."""
    found = discovery_reading(start)
    if found is None or found.why is not None or found.root is None:
        return False
    return _same_folder(toplevel, found.root) is True
