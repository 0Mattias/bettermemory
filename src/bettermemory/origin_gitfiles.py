"""The repository files `origin` reads in-process, with no git process.

The config reader (`_config_variable_names`, memoised per file state in
`_config_facts`), the root spelled as git prints it (`_spelled_as_git`),
the root of a repository git refused to read (`refused_worktree_root`),
the paths the capture has warned are no directory (`first_not_a_directory`),
the decoder of git's C-quoted patch headers, the index's ``.gitmodules``
entry, and the file signatures the drift memos key on
(`attribute_files_signature`, `gitattributes_signature`,
`walk_files_signature`, `directories_held`, `files_held`) and the head
they are read under where `githead`'s discovery declines
(`located_head`).

This module lives apart from `origin.py` to keep each module under the
package's 2,000-line bound (ENG-96). It imports the standard library,
`_caches`, `githead` and `gitconfig` (git's parser of a config file,
which `githead` loads already), and `claims.hunk_lines` where it reads a
patch; `origin` imports it, never the reverse, and re-exports every name
a caller reads. Patches of `_config_entries`
and `_INDEX_FIRST_READ` go here, where their readers are.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _caches, gitconfig, githead
from ._location_probe import path_missing


# The repository's own configuration file, read to see whether it moves
# the working tree git names; larger than any configuration a checkout
# carries.
_CONFIG_LIMIT = 1 << 20

# The one ``bare`` line that leaves the working tree where it is: a plain
# false, as ``git init`` writes it. Matched against a lowercased line.
_BARE_FALSE = re.compile(rb"[ \t]*bare[ \t]*=[ \t]*(?:false|no|off|0)[ \t]*\r?")


@dataclass(frozen=True)
class _ConfigFacts:
    """What the callers read from one configuration file: the variables it
    sets as git reads them (`_config_variable_names`; None where git
    refuses the file or it holds a NUL byte), whether it names an
    attributes file or an attribute tree (``attributesfile`` or ``[attr``
    anywhere in it, in any case), whether it sets a variable of an
    ``include`` or ``includeIf`` section (whose file may set a diff driver
    or anything else no stat here covers), whether a line names ``bare``
    with anything but a plain false, and whether ``git rev-parse`` may die
    reading it (`_rev_parse_dies`; ENG-207's round 7)."""

    names: frozenset[bytes] | None
    attributes_elsewhere: bool
    bare_moves: bool
    git_dies: bool = False
    includes: bool = False


_NO_CONFIG = _ConfigFacts(
    names=frozenset(), attributes_elsewhere=False, bare_moves=False
)

#: The sections whose ``path`` names another file git reads as config,
#: as `gitconfig.git_entries` names them (lower case).
_INCLUDE_SECTIONS = (b"include.", b"includeif.")


def _facts_of(data: bytes) -> _ConfigFacts:
    entries = _config_entries(data)
    lowered = data.lower()
    return _ConfigFacts(
        names=None if entries is None else frozenset(n for n, _v in entries),
        attributes_elsewhere=b"attributesfile" in lowered or b"[attr" in lowered,
        bare_moves=any(
            b"bare" in line and _BARE_FALSE.fullmatch(line) is None
            for line in lowered.split(b"\n")
        ),
        git_dies=entries is not None
        and any(
            _rev_parse_dies(n, None if v is None else v.split(b"\0", 1)[0])
            for n, v in entries
        ),
        includes=entries is not None
        and any(n.startswith(_INCLUDE_SECTIONS) for n, _v in entries),
    )


# The facts of each configuration file read, memoised on the file's
# `githead.stamp`: the repository's config and config.worktree are read on
# every search (`toplevel_and_head_from_files`, `walk_files_signature`),
# once more on a search with a hit under a governed claim
# (`attribute_files_signature`, read once per search, not per hit), and
# again by the check at the end of a search that stores what it resolved;
# a large file would otherwise be parsed again each time. A file is parsed
# once per stamp, the stamp taken from the open file before and after the
# read, and the facts are kept only where the two agree, so what is kept
# is what that state of the file holds; a rewrite in place at the same
# size within one tick of the filesystem's clock keeps its stamp and is the
# one change missed, as for every stamp. Bounded in entries, not bytes: an
# entry holds the names the file sets, about 1.3 KB for a config `git init`
# writes with one remote and about 2.6 MB for a config of a megabyte
# (12,000 branch sections, the largest the reader accepts), measured with
# tracemalloc on Python 3.13 as 84 KB and 166 MB for 64 such entries, the
# bound. Registered with `_caches`.
_CONFIG_MEMO_CAP = 64
_CONFIG_MEMO: OrderedDict[str, tuple[githead.Stamp, _ConfigFacts]] = OrderedDict()
_CONFIG_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_config_memo() -> None:
    with _CONFIG_MEMO_LOCK:
        _CONFIG_MEMO.clear()


def _fd_stamp(fd: int) -> githead.Stamp:
    status = os.fstat(fd)
    return (
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_size,
        status.st_ino,
        status.st_mode,
    )


def _read_flags() -> int:
    """The flags `_config_facts` and `_index_gitmodules` open with: read
    only, without blocking on a FIFO, not inherited, and binary. Windows'
    C runtime opens a file in text mode unless told ``O_BINARY``: a read
    then ends at the first Ctrl-Z byte (0x1A) and folds each CR LF into
    LF, which git, reading every file in binary, does not. An index whose
    first entry holds a 0x1A (a 26-byte file's size, a timestamp's byte,
    an object id's) was read short and declined as unsettled, so no row
    was keyed: the drift memo kept nothing on Windows wherever one fell
    there (`walk_files_signature`). A config read short would lose what
    follows the byte. Read when called, as every flag is: off Windows it
    adds nothing."""
    return (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _config_facts(path: Path) -> tuple[githead.Stamp | None, _ConfigFacts] | None:
    """``(stamp, facts)`` of the configuration file `path`: its `githead.
    stamp`, None when it does not exist (and then facts that set nothing),
    and what it holds. None when it cannot be read, is not a regular file
    or is larger than `_CONFIG_LIMIT`. The open does not block, so a FIFO
    fails instead of waiting, and is binary (`_read_flags`)."""
    key = os.fspath(path)
    try:
        current = githead.stamp(path)
    except (OSError, ValueError):
        return None
    if current is None:
        return None, _NO_CONFIG
    with _CONFIG_MEMO_LOCK:
        kept = _CONFIG_MEMO.get(key)
        if kept is not None and kept[0] == current:
            _CONFIG_MEMO.move_to_end(key)
            return kept
    flags = _read_flags()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # A share that does not answer is unread, not a missing config.
        if isinstance(exc, FileNotFoundError) and path_missing(exc):
            return None, _NO_CONFIG
        return None
    try:
        before = _fd_stamp(fd)
        if not stat.S_ISREG(before[4]):
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= _CONFIG_LIMIT:
            chunk = os.read(fd, _CONFIG_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = _fd_stamp(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if size > _CONFIG_LIMIT:
        return None
    read = (before, _facts_of(b"".join(chunks)))
    if before == after:
        with _CONFIG_MEMO_LOCK:
            _CONFIG_MEMO[key] = read
            _CONFIG_MEMO.move_to_end(key)
            while len(_CONFIG_MEMO) > _CONFIG_MEMO_CAP:
                _CONFIG_MEMO.popitem(last=False)
    return read


#: The variable that makes git read config.worktree, as
#: `_config_variable_names` spells it.
_WORKTREE_CONFIG_EXTENSION = b"extensions.worktreeconfig"


def _worktree_config_facts(
    gd: githead.GitDir, common: _ConfigFacts
) -> tuple[githead.Stamp | None, _ConfigFacts] | None:
    """`_config_facts` of ``config.worktree`` in the git directory, which
    git reads after the common config where that config sets
    ``extensions.worktreeConfig``: the file's stamp and facts then, and
    ``(None, _NO_CONFIG)``, nothing to key on, where the extension is not
    set, since git reads the file only under it and a write to it then
    changes nothing git reads. The extension's name decides, not its value:
    a config that sets it false stamps the file and declines on what it
    sets as though git read it, which costs a stat and nothing else. None
    where the file cannot be read, as `_config_facts` answers."""
    if common.names is None or _WORKTREE_CONFIG_EXTENSION not in common.names:
        return None, _NO_CONFIG
    return _config_facts(gd.gitdir / "config.worktree")


def _config_moves_worktree(gd: githead.GitDir) -> bool:
    """Whether the repository's own configuration could make git name a
    working tree other than the directory holding ``.git``: ``core.worktree``
    set in the common config or in config.worktree, read as git reads the
    files (a branch, a remote or ``extensions.worktreeConfig`` that merely
    holds the word sets nothing), a line naming ``bare`` with anything but
    a plain false, a file git refuses, that holds a NUL byte or that
    cannot be read, and a file ``git rev-parse`` may die reading
    (`_rev_parse_dies`: ENG-207's round 7, where the files answered a root
    and a head git does not print). config.worktree is read whether or not
    the extension is set, an over-decline that costs one process where a
    file git does not read sets either. The answer only decides whether
    the files or git answer, so a false yes costs one process and a false
    no would be a wrong root, or one where git dies."""
    for path in (gd.commondir / "config", gd.gitdir / "config.worktree"):
        read = _config_facts(path)
        if read is None:
            return True
        facts = read[1]
        if (
            facts.names is None
            or b"core.worktree" in facts.names
            or facts.bare_moves
            or facts.git_dies
        ):
            return True
    return False


def _spelled_as_git(root: Path) -> Path | None:
    """`root` spelled the way git prints a working tree's root.

    Git names the root from the kernel's path for the directory
    (``getcwd``), which gives each component the case and Unicode form the
    directory holds it in. `githead` walks from ``os.path.realpath``,
    which keeps the spelling it was handed, so on a filesystem that
    ignores case or normalization (macOS's default) a caller that named
    ``~/documents`` for ``~/Documents`` gets a root git would not print.
    On macOS the kernel's spelling is read back with ``F_GETPATH``; None
    when the directory cannot be opened. Elsewhere the realpath stands,
    as it does on every filesystem that holds one spelling per name.
    """
    if sys.platform != "darwin":
        return root
    import fcntl

    try:
        fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except OSError:
        return None
    finally:
        os.close(fd)
    return Path(os.fsdecode(raw.split(b"\0", 1)[0]))


# git's ref_rev_parse_rules (refs.c), in order: the names `git
# symbolic-ref --short HEAD` tries a short name against. With
# `_shortening_refs`, moved here from `origin` to keep it under the
# package's 2,000-line bound (ENG-207's round 6); the capture's signature
# stamps the refs it names (`origin._capture_signature`).
_REV_PARSE_RULES = (
    "{}",
    "refs/{}",
    "refs/tags/{}",
    "refs/heads/{}",
    "refs/remotes/{}",
    "refs/remotes/{}/HEAD",
)


def _shortening_refs(refname: str) -> list[str]:
    """The refs whose existence decides how `git symbolic-ref --short
    HEAD` shortens `refname` (``refs_shorten_unambiguous_ref``): for each
    rule after the first that `refname` matches, the short name it gives
    spelled under every other rule. Git keeps a short name only while none
    of the refs it tries for it exists, and it tries a subset of these (the
    rules before the matched one, not strict); for ``refs/heads/<b>`` it
    tries ``<b>``, ``refs/<b>`` and ``refs/tags/<b>``, then ``heads/<b>``,
    and ``refs/remotes/<b>`` and ``refs/remotes/<b>/HEAD`` are in the set
    too."""
    names: list[str] = []
    for index in range(len(_REV_PARSE_RULES) - 1, 0, -1):
        prefix, _, suffix = _REV_PARSE_RULES[index].partition("{}")
        if not (
            refname.startswith(prefix)
            and refname.endswith(suffix)
            and len(refname) > len(prefix) + len(suffix)
        ):
            continue
        short = refname[len(prefix) : len(refname) - len(suffix)]
        names.extend(
            rule.format(short)
            for other, rule in enumerate(_REV_PARSE_RULES)
            if other != index
        )
    return list(dict.fromkeys(names))


def relative_to_root(candidate: Path, root: Path) -> Path:
    """`candidate`, a resolved path, relative to the working tree `root`.

    Where its spelling does not begin with the root's, the ancestor that
    is the root's directory on disk (`os.path.samestat`) anchors it: on a
    filesystem that ignores case or normalization (macOS's default, and
    Windows'), ``/Users/me/documents/repo/x.py`` names a file of the tree
    git spells ``/Users/me/Documents/repo``, and `os.path.realpath` keeps
    the spelling it was handed. The part below that ancestor keeps the
    caller's spelling. ValueError where no ancestor is the root or the
    root cannot be read.
    """
    try:
        return candidate.relative_to(root)
    except ValueError:
        pass
    try:
        held = root.stat()
    except OSError:
        raise ValueError(f"{root} cannot be read") from None
    for parent in candidate.parents:
        try:
            if os.path.samestat(parent.stat(), held):
                return candidate.relative_to(parent)
        except OSError:
            continue
    raise ValueError(f"{candidate} is not in {root}")


def refused_worktree_root(cwd: Path) -> tuple[str | None, bool]:
    """``(worktree_root, indeterminate)`` for `cwd` where ``git rev-parse
    --show-toplevel`` exited non-zero, read from the files as
    `githead.locate` reads them.

    A non-zero exit is git's "not a git repository", and also its refusal
    of a repository it will not read: one another account owns (a
    dev-container bind mount, an SMB or NFS share with mapped ids, a
    colleague's shared tree) while ``safe.directory`` does not name it, an
    extension or a config file git does not accept. `locate` reads the
    ``.git`` entry without git's ownership check, since reading the
    repository's files runs nothing its configuration names. Where it
    finds no entry the exit is the answer, ``(None, False)``, and so it is
    at a ``.git`` entry membership refuses, which `locate` reads as none
    and for which `origin._git_result` answers git's "not a repository"
    without running git (ENG-193). Where it
    finds one, git did not answer, and the result is indeterminate: the
    root is the directory holding the entry, spelled as git prints a root
    (`_spelled_as_git`) and resolved as the probe resolves git's, unless
    the repository's configuration could move the working tree
    (`_config_moves_worktree`) or the spelling cannot be read, and then
    None."""
    found = githead.locate(cwd)
    if found is None:
        return None, False
    if _config_moves_worktree(found):
        return None, True
    spelled = _spelled_as_git(found.worktree_root)
    if spelled is None:
        return None, True
    try:
        return str(spelled.resolve()), True
    except OSError:
        return str(spelled), True


# The paths `first_not_a_directory` has seen, oldest first. An indeterminate
# capture is never kept, so a declared workspace this host does not hold is
# probed on every hook call; `origin` warns of it once per path per process
# and logs at DEBUG after (review Ob, G7). Bounded at `_NOT_A_DIRECTORY_CAP`
# paths; registered with `_caches`.
_NOT_A_DIRECTORY: dict[str, None] = {}
_NOT_A_DIRECTORY_CAP = 64
_NOT_A_DIRECTORY_LOCK = threading.Lock()


@_caches.register
def _clear_not_a_directory() -> None:
    with _NOT_A_DIRECTORY_LOCK:
        _NOT_A_DIRECTORY.clear()


def first_not_a_directory(path: Path) -> bool:
    """Whether `path` is reported as no directory this host holds for the
    first time in this process (among the last `_NOT_A_DIRECTORY_CAP`
    paths reported)."""
    with _NOT_A_DIRECTORY_LOCK:
        if str(path) in _NOT_A_DIRECTORY:
            return False
        _NOT_A_DIRECTORY[str(path)] = None
        if len(_NOT_A_DIRECTORY) > _NOT_A_DIRECTORY_CAP:
            del _NOT_A_DIRECTORY[next(iter(_NOT_A_DIRECTORY))]
        return True


# ---------------------------------------------------------------------------
# The origin cache's signature
# ---------------------------------------------------------------------------


def capture_files(start: Path) -> tuple[githead.Signature, githead.GitDir | None]:
    """The repository files the origin cache keys a capture of `start` on:
    `githead.signature` and `githead.find_gitdir` where `githead`'s
    discovery settles the repository (its git directory, or None outside
    any), and where it declines, for the platform (off POSIX, where git's
    ownership check reads security descriptors) or for another account's
    ownership, `githead.signature(owner=False)` and the repository
    `githead.locate` finds, so the cache holds on every platform. The
    repository is None where the signature is not settled; where the walk
    found another git directory than the signature read (the tree changed
    between the two), the signature is made to equal no other."""
    base = githead.signature(start)
    if base[0] is None or isinstance(base[0], str):
        return base, githead.find_gitdir(start)
    located = githead.signature(start, owner=False)
    if not isinstance(located[0], str):
        return located, None
    gd = githead.locate(start)
    if gd is None or str(gd.gitdir) != located[0]:
        return (object(), *located[1:]), None
    return located, gd


# The single-character escapes git's `quote_c_style` emits inside a
# quoted path header, mapped to their byte values. Everything else it
# deems unprintable comes out as 3-digit octal. Both decode to BYTES —
# an octal escape is one raw byte of the path's on-disk encoding, so a
# multi-byte UTF-8 character arrives as several escapes and the path
# must be reassembled as bytes and decoded once at the end.
_C_QUOTE_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def _unquote_c_path(quoted: str) -> str | None:
    """Decode ONE complete git C-quoted string, or None if `quoted` is
    not exactly that.

    "Exactly that" is the safety property: the input must be a leading
    quote, a well-formed escaped body with no unescaped interior quote,
    and a trailing quote with nothing after it. Anything else — an
    unterminated quote, an unknown escape, an interior close-quote with
    trailing text, bytes that don't decode as UTF-8 — returns None and
    the caller leaves the line untouched. Better to keep a quoted
    spelling (a false mismatch, the conservative direction downstream)
    than to guess at a path git didn't actually name.
    """
    if len(quoted) < 2 or not quoted.startswith('"') or not quoted.endswith('"'):
        return None
    body = quoted[1:-1]
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            # An unescaped interior quote means `quoted` was not ONE
            # complete quoted string (e.g. `"x" -> "y"` content).
            return None
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        esc = body[i]
        code = _C_QUOTE_ESCAPES.get(esc)
        if code is not None:
            out.append(code)
            i += 1
            continue
        if esc in "01234567":
            j = i + 1
            while j < n and j - i < 3 and body[j] in "01234567":
                j += 1
            out.append(int(body[i:j], 8) & 0xFF)
            i = j
            continue
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


#: What a stream holds where some header in it is quoted.
_QUOTED_HEADER_MARKS = ('--- "', '+++ "', 'diff --git "', ' from "', ' to "')
#: The extended header lines that name a path of their own.
_NAMING_HEADERS = ("rename from ", "rename to ", "copy from ", "copy to ")


def _quoted_header_path(quoted: str) -> str | None:
    """The path a quoted header names, decoded (`_unquote_c_path`), or
    None where it is not one complete quoted string or names a line break:
    a header is one line, and a decoded LF or CR written back into the
    stream would start another (ENG-220's re-review)."""
    decoded = _unquote_c_path(quoted)
    if decoded is None or "\n" in decoded or "\r" in decoded:
        return None
    return decoded


def _dequote_named_header(line: str) -> str | None:
    """`line` with its quoted path unquoted, where it is a ``diff --git``
    line whose two sides are one complete quoted path each, ``a/P`` and
    ``b/P``, or a rename or copy line whose remainder is one; None for any
    other line, left as it stands."""
    if line.startswith('diff --git "'):
        rest = line[11:]
        for k in range(1, len(rest) - 2):
            if rest[k : k + 3] != '" "':
                continue
            old = _quoted_header_path(rest[: k + 1])
            new = _quoted_header_path(rest[k + 2 :])
            if old and new and old.startswith("a/") and new == "b/" + old[2:]:
                return f"diff --git {old} {new}"
        return None
    for prefix in _NAMING_HEADERS:
        if line.startswith(prefix + '"'):
            decoded = _quoted_header_path(line[len(prefix) :])
            return None if decoded is None else prefix + decoded
    return None


def _dequote_patch_headers(stream: str) -> str:
    """Rewrite residually-quoted ``---``/``+++`` headers to the unquoted
    shape `claims.build_binding_index` hard-requires.

    Even with ``core.quotePath=false`` pinned, git still C-quotes a
    header whose path carries a double quote, a backslash, or control
    bytes — ``--- "a/we\\"ird.py"`` — and the parser's exact
    ``--- a/`` / ``+++ b/`` prefix match then misses it (a deletion of
    such a path would go unrecorded; an edit would be indexed under the
    quoted spelling and never equal a claim's rel_path). Decoding
    happens HERE, on the producer side, so the parser keeps its
    one-argument no-lookup guarantee and its exact prefix match.

    Only a header is rewritten, never a hunk's line: a hunk's lines are
    read past by the count its ``@@`` line gives (`claims.hunk_lines`), as
    the parser reads them, so a file's line that reads as a quoted header
    (an added ``++ "b/x"`` prints as ``+++ "b/x"``) stays as git printed
    it. Read as a header, such a line's ``\\n`` escape was written back as
    a line break, and what followed it, a record mark and a hash or a
    ``--- a/<another path>`` line, read as the stream's own: foo's hunk went
    to a commit outside the window or to another file, and the count read
    0 on both bases (ENG-220's re-review). A header is rewritten where its
    whole remainder decodes as one complete quoted string, naming no line
    break (`_quoted_header_path`), whose path starts with the pinned ``a/`` (old
    side) or ``b/`` (new side) prefix. The fast path keeps the
    zero-quoted-header case, i.e. essentially every real stream,
    allocation-free.

    The ``diff --git`` line and the rename and copy lines are rewritten
    the same way (`_dequote_named_header`): a section whose header alone
    says its path changed (an empty file deleted or created) has no
    ``---``/``+++`` line, and the parser reads its path from them.
    """
    if not any(mark in stream for mark in _QUOTED_HEADER_MARKS):
        return stream
    from .claims import hunk_lines

    lines = stream.split("\n")
    left = 0
    for idx, line in enumerate(lines):
        if left:
            # A hunk's line, which the parser counts (a no-newline marker
            # it reads past), or the line after a hunk cut short, where
            # the parser resynchronises too.
            if line.startswith("\\"):
                continue
            if line.startswith(("-", "+")):
                left -= 1
                continue
            left = 0
        size = hunk_lines(line)
        if size is not None:
            left = size
            continue
        named = _dequote_named_header(line)
        if named is not None:
            lines[idx] = named
            continue
        if not line.startswith(('--- "', '+++ "')):
            continue
        decoded = _quoted_header_path(line[4:])
        if decoded is None:
            continue
        prefix = "a/" if line.startswith("--- ") else "b/"
        if decoded.startswith(prefix):
            lines[idx] = line[:4] + decoded
    return "\n".join(lines)


# Characters with which a pathspec matches more than the one path it
# spells: git's wildcards and their escape. A leading colon opens pathspec
# magic.
_PATHSPEC_WILDCARDS = frozenset("*?[\\")


# The stamp each attribute file, each file beside the walk and the
# githead signature's config carry: githead's, so the two agree.
_stamp = githead.stamp


def _hold_directory(path: str | Path, held: dict[str, tuple[int, int]]) -> None:
    """Record in `held` the ``(st_mtime_ns, st_ctime_ns)`` of the directory
    `path`, or of its nearest existing ancestor while it does not exist.
    Creating or removing an entry changes its directory's stamp, and
    creating a missing directory changes its parent's, so a file created
    and removed between two reads, absent at both, still moves a stamp
    recorded here. Read for a check before and after a computation, never
    for a key: a directory changes whenever any entry in it does. A
    directory already in `held` is not read again and keeps the stamp
    first recorded for it, which is the one the check must compare with.
    Raises OSError or ValueError where a stat fails for any reason but
    absence. A file is held the same way (`walk_files_signature` holds the
    index): a write to it, or its replacement through a rename, moves the
    pair.

    The cost is that of any change to such a directory, related or not.
    While something creates and removes files in a held directory steadily
    (an editor's swap files in the repository's root or in a governed
    file's directory; the global attributes file's nearest existing
    ancestor where its own directory does not exist, which is the home
    directory itself where ``~/.config`` does not exist either), a search or
    walk that held it keeps nothing it computed, and each one pays what an
    uncached one pays."""
    current = os.fspath(path)
    while current not in held:
        try:
            status = os.stat(current)
        except (FileNotFoundError, NotADirectoryError) as exc:
            parent = os.path.dirname(current)
            if parent == current or not path_missing(exc):
                raise
            current = parent
            continue
        held[current] = (status.st_mtime_ns, status.st_ctime_ns)


def directories_held(held: Mapping[str, tuple[int, int]]) -> bool:
    """Whether every directory `_hold_directory` recorded in `held` still
    has the stamp recorded for it; False where one cannot be read."""
    try:
        for directory, recorded in held.items():
            status = os.stat(directory)
            if (status.st_mtime_ns, status.st_ctime_ns) != recorded:
                return False
    except (OSError, ValueError):
        return False
    return True


def _global_attributes_path(root: Path) -> str | None:
    """The global attributes file git reads when no ``core.attributesFile``
    names another, spelled as git's ``xdg_config_home_for`` spells it:
    ``$XDG_CONFIG_HOME/git/attributes`` when XDG_CONFIG_HOME is set and not
    empty, else ``$HOME/.config/git/attributes`` whenever HOME is set, an
    empty HOME naming ``/.config/git/attributes``, and none with neither. A
    relative name is read from the root, where the drift readers run
    git."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        candidate = f"{config_home}/git/attributes"
    else:
        home = os.environ.get("HOME")
        if home is None:
            return None
        candidate = f"{home}/.config/git/attributes"
    if os.path.isabs(candidate):
        return candidate
    return os.path.join(root, candidate)


#: The variables of a repository's config ``git rev-parse --show-toplevel
#: HEAD`` dies on where they hold no boolean (`gitconfig.git_bool`; no
#: value is true), as git 2.54 reads them: config.c's ``git_default_config``
#: and the repository settings resolving HEAD reads (ENG-207's round 7,
#: each checked against git, tests/test_origin_config_dies.py). Every
#: ``advice.<key>`` is read so too.
_DIES_UNLESS_BOOL = frozenset(
    {
        b"core.filemode",
        b"core.ignorecase",
        b"core.precomposeunicode",
        b"core.symlinks",
        b"core.sparsecheckout",
        b"core.sparsecheckoutcone",
        b"core.trustctime",
        b"core.quotepath",
        b"core.ignorestat",
        b"core.protecthfs",
        b"core.protectntfs",
        b"core.warnambiguousrefs",
        b"core.multipackindex",
        b"core.commitgraph",
        b"user.useconfigonly",
        b"color.pager",
        b"pager.color",
        b"sparse.expectfilesoutsideofpatterns",
        b"extensions.preciousobjects",
        b"extensions.worktreeconfig",
        b"feature.manyfiles",
        # Git for Windows' own, booleans there and read any way elsewhere:
        # its system config sets core.fscache (ENG-207's round 8).
        b"core.fscache",
        b"core.longpaths",
    }
)
#: The variables it dies on with no value, and reads any value of.
_DIES_UNSET = frozenset(
    {
        b"core.excludesfile",
        b"core.attributesfile",
        b"core.editor",
        b"core.askpass",
        b"core.whitespace",
        b"core.fsync",
        b"core.fsyncmethod",
        b"core.checkroundtripencoding",
        b"core.untrackedcache",
        b"user.name",
        b"user.email",
        b"author.name",
        b"author.email",
        b"committer.name",
        b"committer.email",
        b"i18n.commitencoding",
        b"i18n.logoutputencoding",
        b"attr.tree",
        b"extensions.partialclone",
    }
)
#: The variables that take a boolean or one word more (any case), and
#: whether no value dies.
_DIES_UNLESS_WORD = {
    b"core.autocrlf": (b"input", False),
    b"core.safecrlf": (b"warn", False),
    b"core.logallrefupdates": (b"always", True),
}
#: The variables that take one of a set of words, as written (``strcmp``),
#: and die on any other value and on none: config.c's
#: ``git_default_push_config`` and ``git_default_branch_config``.
_DIES_UNLESS_NAMED = {
    b"push.default": frozenset(
        {b"nothing", b"matching", b"simple", b"upstream", b"tracking", b"current"}
    ),
    b"branch.autosetuprebase": frozenset({b"never", b"local", b"remote", b"always"}),
}
#: The variables that take one of a set of words, as written, or a
#: boolean (no value is true).
_DIES_UNLESS_BOOL_OR_NAMED = {
    b"branch.autosetupmerge": frozenset({b"always", b"inherit", b"simple"}),
}
#: The variables of the sections git reads whole that it reads any value
#: of; any other variable there (`_READ_WHOLE`) is one whose reading is
#: not ported here, and declines.
_READ_ANY = frozenset(
    {
        b"core.bare",
        b"core.worktree",
        b"core.hookspath",
        b"core.pager",
        b"core.sshcommand",
        b"core.eol",
        b"core.fsmonitor",
        b"core.splitindex",
        b"core.bigfilethreshold",
        b"core.notesref",
        b"core.preloadindex",
        b"core.alternaterefscommand",
        b"core.alternaterefsprefixes",
        b"extensions.noop",
    }
)
#: Sections of whose variables git reads more than are named here.
_READ_WHOLE = (b"core.", b"extensions.", b"feature.", b"index.", b"color.advice")
#: Variables of other sections git reads by rules not ported here.
_READ_ELSEWHERE = frozenset({b"pack.packsizelimit", b"pack.compression"})


def _rev_parse_dies(name: bytes, value: bytes | None) -> bool:
    """Whether ``git rev-parse --show-toplevel HEAD`` may die on the
    repository's config setting `name` (as `gitconfig.git_entries` names
    it) to `value`: a value of a variable git reads that it refuses
    (`_DIES_UNLESS_BOOL`, `_DIES_UNSET`, `_DIES_UNLESS_WORD`,
    `_DIES_UNLESS_NAMED`, `_DIES_UNLESS_BOOL_OR_NAMED`, a
    ``core.repositoryformatversion`` that is no int or is above 1), a
    variable git reads by a rule not ported here (`_READ_WHOLE`,
    `_READ_ELSEWHERE`), and an include, whose file is not read here (git
    dies on one with no value, an empty one and one of a folder). A false
    yes costs one process (`_config_moves_worktree`)."""
    if name in _DIES_UNLESS_BOOL or name.startswith(b"advice."):
        return gitconfig.git_bool(value) is None
    if name in _DIES_UNSET:
        return value is None
    if name in _DIES_UNLESS_WORD:
        word, unset_dies = _DIES_UNLESS_WORD[name]
        if value is None:
            return unset_dies
        said = value.split(b"\0", 1)[0].lower()
        return said != word and gitconfig.git_bool(value) is None
    if name in _DIES_UNLESS_NAMED:
        return value is None or value.split(b"\0", 1)[0] not in _DIES_UNLESS_NAMED[name]
    if name in _DIES_UNLESS_BOOL_OR_NAMED:
        if (
            value is not None
            and value.split(b"\0", 1)[0] in _DIES_UNLESS_BOOL_OR_NAMED[name]
        ):
            return False
        return gitconfig.git_bool(value) is None
    if name == b"core.repositoryformatversion":
        number = gitconfig.git_int(value)
        return number is None or number > 1
    if name == b"include.path" or (
        name.startswith(b"includeif.") and name.endswith(b".path")
    ):
        return True
    if name in _READ_ANY:
        return False
    return name in _READ_ELSEWHERE or name.startswith(_READ_WHOLE)


def _machine_config_dies(
    reads: Callable[[str], Sequence[gitconfig.ConfigValues]], git_dir: Path
) -> bool:
    """Whether ``git rev-parse --show-toplevel HEAD`` may die reading the
    machine's config (the system's and the global files, with what they
    include) for the repository whose git directory is `git_dir`, from
    `reads` (`location_git.machine_config_reads`: the reads `origin`'s
    capture keeps against its stamps, so this costs no read where an
    origin was captured, and where none was the file is read once, the
    origin's signature after taking that read's stats where no capture is
    kept for its folder): one git
    dies reading or may (a line its parser refuses, an include it cannot
    read or follow: `gitconfig.ConfigValues.dies`, `maybe_dies`), one read
    in part, and a value `_machine_value_dies` refuses, under an include
    whose condition is not evaluated too (`ConfigValues.undecided`), an
    include itself aside, which the read followed (ENG-207's round 8: the
    files answered a root and a head where git dies on the machine's
    config). True where the reads cannot be had."""
    try:
        found = reads(str(git_dir))
    except (OSError, ValueError):
        return True
    for read in found:
        if read.dies is not None or read.maybe_dies is not None or not read.complete:
            return True
        for values in (read, read.undecided):
            for (section, sub, key), held in values.items():
                if section in ("include", "includeif"):
                    continue
                name = b".".join(
                    (
                        section.encode("ascii"),
                        *(() if sub is None else (gitconfig.subsection_bytes(sub),)),
                        key.encode("ascii"),
                    )
                )
                if any(_machine_value_dies(name, value) for value in held):
                    return True
    return False


def _machine_value_dies(name: bytes, value: bytes | None) -> bool:
    """`_rev_parse_dies` for a variable of the machine's config, where
    ``core.bare``, which only a repository's own config moves the working
    tree by (`_config_moves_worktree`), is read as git's default config
    reads it at every scope: a boolean, or git dies."""
    if name == b"core.bare":
        return gitconfig.git_bool(value) is None
    return _rev_parse_dies(name, value)


def _config_entries(data: bytes) -> list[gitconfig.Entry] | None:
    """The variables the config file `data` sets, as git's own parser
    reads them (`gitconfig.git_entries`, ENG-207's round 7, which replaced
    a parser of this module's own), each name to its first NUL byte and
    each value too, as git's callbacks see them; None where git refuses
    the file, and None for any file holding a NUL byte: git keeps a name
    only up to a NUL in it, so ``[log "follow\\0"] x`` sets ``log.follow``
    to git (and an escaped letter spells the word without its bytes),
    which the raw text `_facts_of` also reads (``attributesfile``,
    ``bare``) does not show."""
    if b"\0" in data:
        return None
    return gitconfig.git_entries(data)


def _config_variable_names(data: bytes) -> set[bytes] | None:
    """The names of the variables the config file `data` sets, as git's own
    parser reads the file (`_config_entries`) and ``git config --list
    --name-only`` prints them: ``section.key`` or
    ``section.subsection.key``, the section and the key in lower case, a
    quoted subsection as written, so ``log.follow`` is named in any case
    or layout git accepts and a ``follow`` inside a value, a comment, a
    subsection or a longer key is not; None where git refuses the file
    or it holds a NUL byte. Pinned against git on the texts of
    tests/test_commit_drift_cache.py."""
    entries = _config_entries(data)
    return None if entries is None else {name for name, _value in entries}


def attribute_files_signature(
    cwd: Path,
    root: Path,
    *,
    directories: dict[str, tuple[int, int]] | None = None,
) -> tuple[object, ...] | None:
    """The files outside the working tree that decide how
    `commit_patch_stream` diffs a file, for a memo to key on: the
    repository's ``config`` (its diff drivers, and whether it names an
    attributes file) and, where it sets ``extensions.worktreeConfig``, the
    ``config.worktree`` git then reads after it in the git directory
    (`_worktree_config_facts`; its own in a linked worktree, which `git
    sparse-checkout set` writes there), ``info/attributes`` in the common
    directory, and the global attributes file with its path. Each is its
    `_stamp` (``st_mtime_ns``, ``st_ctime_ns``, ``st_size``, ``st_ino``,
    ``st_mode``), or None while it does not exist, so writing, creating,
    removing, replacing or changing the permissions of one changes the
    signature.

    `directories`, when given, receives the stamps of ``info`` in the
    common directory and of the global file's directory
    (`_hold_directory`), for a caller that checks at the end of its work
    that no attributes file there was created and removed meanwhile.

    None, and nothing to key on, where these files do not settle it: a
    repository `githead` does not read, a ``config`` or ``config.worktree``
    that cannot be read, one that mentions ``attributesfile`` or an
    ``[attr`` section (a ``core.attributesFile`` or ``attr.tree`` there
    sends git to a file or a tree these stats do not cover), one that sets
    a variable of an ``include`` or ``includeIf`` section (the file it
    names may set a diff driver, an attributes file or anything else, and
    no stat here covers it: ENG-220's review, round F), one that sets
    ``log.follow`` (it made the single-pathspec patch stream follow a
    claimed file across a rename and diff the renaming commit with the
    attributes of the rename source's path, which no chain of the claimed
    path covers; every git `origin` runs pins it off now,
    `origin_history.AS_STORED_CONFIG`, and the decline stays), one git
    refuses, one that holds a NUL byte (git ends a
    variable's name at it, so a quoted subsection can set ``log.follow`` or
    ``core.attributesFile`` without the words' bytes), and a stat that
    fails for any reason but absence. Whether a file sets ``log.follow`` is
    read as git reads it (`_config_variable_names`, memoised per state of
    the file in `_config_facts`), in any case or layout, so
    ``push.followTags``, a branch or a remote whose name holds ``follow``,
    or the word in a value or a comment, keys the files like any other
    config. A ``log.follow`` set outside the repository's own files (the
    global and system files, an include, the environment) is not seen.
    `root` is the root `cwd`'s repository names.

    The repository is the one `githead.find_gitdir` discovers from `cwd`
    and, where it declines (off POSIX, a repository another account owns),
    the one whose ``.git`` entry sits at `root`, read by `githead.locate`
    without its walk, as the split's memo reads it (`origin_patch.
    _memo_key`); under a variable that moves git's discovery, none. Read
    that way it is None too while neither XDG_CONFIG_HOME nor HOME is set:
    Git for Windows then derives a home of its own (from HOMEDRIVE and
    HOMEPATH, or USERPROFILE), whose global attributes file is not known
    here."""
    gd = githead.find_gitdir(cwd)
    if gd is None and not githead.discovery_moved():
        gd = githead.locate(root, walk=False)
        if not os.environ.get("XDG_CONFIG_HOME") and "HOME" not in os.environ:
            return None
    if gd is None:
        return None
    read = _config_facts(gd.commondir / "config")
    if read is None or read[1].names is None:
        return None
    config_stamp, facts = read
    worktree = _worktree_config_facts(gd, facts)
    if worktree is None:
        return None
    worktree_stamp, worktree_facts = worktree
    for each in (facts, worktree_facts):
        if (
            each.names is None
            or each.attributes_elsewhere
            or each.includes
            or b"log.follow" in each.names
        ):
            return None
    global_file = _global_attributes_path(root)
    try:
        if directories is not None:
            _hold_directory(gd.commondir / "info", directories)
            if global_file is not None:
                _hold_directory(os.path.dirname(global_file), directories)
        return (
            config_stamp,
            worktree_stamp,
            _stamp(gd.commondir / "info" / "attributes"),
            global_file,
            None if global_file is None else _stamp(global_file),
        )
    except (OSError, ValueError):
        return None


def gitattributes_signature(
    root: Path,
    pathspecs: Sequence[str],
    *,
    directories: dict[str, tuple[int, int]] | None = None,
) -> tuple[object, ...] | None:
    """The working tree's ``.gitattributes`` files that decide how
    `commit_patch_stream` diffs the files `pathspecs` name, for a memo to
    key on. Git reads the attributes of ``a/b/c.py`` from ``.gitattributes``
    at the root, in ``a`` and in ``a/b``, not through a symbolic link, and
    ``git log`` reads no index for them. Each is its `_stamp`, or None
    while it does not exist. `directories`, when given, receives the stamps
    of those directories (`_hold_directory`), for a caller that checks at
    the end of its work that no ``.gitattributes`` there was created and
    removed meanwhile.

    None, and nothing to key on, where the pathspecs reach files below
    those directories: a pathspec with a wildcard, an escape or pathspec
    magic, one that is not a plain relative path, and one the working tree
    holds as a directory, whose files take attributes from
    ``.gitattributes`` anywhere under it. A pathspec that is no directory
    now has no such file under it until it becomes one, which this check
    then sees. Also None when a stat fails for any reason but absence, a
    path holding a NUL byte among them."""
    chain: dict[Path, None] = {}
    try:
        for spec in pathspecs:
            parts = spec.split("/")
            if (
                spec.startswith(":")
                or not _PATHSPEC_WILDCARDS.isdisjoint(spec)
                or any(part in ("", ".", "..") for part in parts)
            ):
                return None
            try:
                if stat.S_ISDIR(os.stat(root.joinpath(*parts)).st_mode):
                    return None
            except (FileNotFoundError, NotADirectoryError) as exc:
                if not path_missing(exc):
                    raise
            directory = root
            chain.setdefault(directory, None)
            for part in parts[:-1]:
                directory = directory / part
                chain.setdefault(directory, None)
        if directories is not None:
            for directory in chain:
                _hold_directory(directory, directories)
        return tuple(
            _stamp(directory / ".gitattributes", follow=False) for directory in chain
        )
    except (OSError, ValueError):
        return None


# The index's .gitmodules entry, read in-process the way git's
# repo_read_gitmodules consults it (submodule-config.c): git reads the index
# first, reads no .gitmodules at all while the index holds it unmerged
# (is_gitmodules_unmerged), then the working tree's file where it exists,
# the index's stage-0 blob where it does not, and HEAD's last. What the
# drift readers' walk lists depends on the index only through that entry, so
# the walk files key on the entry, not on the index's stamp, and a write to
# the index that leaves .gitmodules as it was keys nothing new.

#: `_index_gitmodules` for an index that holds .gitmodules only at stages 1
#: to 3, as a conflicted merge leaves it: git then reads no .gitmodules.
_INDEX_UNMERGED: tuple[str] = ("unmerged",)

_GITMODULES = b".gitmodules"

# Extended flags git writes and reads in a version 3 or 4 entry: CE_SKIP_
# WORKTREE and CE_INTENT_TO_ADD, in the second flags word. Any other bit
# makes git refuse the index ("unknown index entry format").
_EXTENDED_FLAGS = 0x4000 | 0x2000


class _IndexUnsettled(Exception):
    """The index is not one `_index_gitmodules` reads exactly."""


class _IndexShort(Exception):
    """The prefix of the index read so far ends before the scan does."""


# The first read of an index, and the factor each further read grows by
# while the scan runs past what was read.
_INDEX_FIRST_READ = 1 << 16
_INDEX_READ_GROWTH = 4


# The entry each index held, memoised on the index's stamp: the index is
# rewritten by most git commands (git add, a refreshing git status), and a
# search reads the entry at its start and end. Read once per stamp from the
# open file, kept only where its stamp before and after the read agree.
# Bounded; registered with `_caches`. One entry per git directory whose
# index a search reads, the repositories its anchors live in; measured
# with tracemalloc (Python 3.11, macOS) at about 2.2 KB with what the read
# keeps, so the cap holds the memo near 140 KB.
_INDEX_MEMO_CAP = 64
_INDEX_MEMO: OrderedDict[str, tuple[githead.Stamp, tuple[object, ...] | None]] = (
    OrderedDict()
)
_INDEX_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_index_memo() -> None:
    with _INDEX_MEMO_LOCK:
        _INDEX_MEMO.clear()


def _decode_varint(data: bytes, pos: int, *, complete: bool) -> tuple[int, int]:
    """git's ``decode_varint`` (varint.c): the value at `pos` and the
    position after it."""
    ends = _IndexUnsettled if complete else _IndexShort
    if pos >= len(data):
        raise ends
    byte = data[pos]
    pos += 1
    value = byte & 0x7F
    while byte & 0x80:
        value += 1
        if value >> 57:
            raise _IndexUnsettled
        if pos >= len(data):
            raise ends
        byte = data[pos]
        pos += 1
        value = (value << 7) + (byte & 0x7F)
    return value, pos


def _gitmodules_entries(data: bytes, *, complete: bool) -> list[tuple[int, int, bytes]]:
    """``(stage, mode, object id)`` of each entry named ``.gitmodules`` in
    the index `data`, a SHA-1 repository's, read from the first entry up to
    the first name that sorts after it (git keeps entries in name order,
    then stage). Versions 2 and 3 pad each entry to a multiple of eight
    bytes, version 4 compresses each name against the one before it; a name
    of 4,095 bytes or more is found by its terminating NUL. `data` is the
    whole index when `complete`, and otherwise a prefix of it, past whose
    end a scan that has not finished raises `_IndexShort`. Anything else
    git would refuse, or lay out otherwise, raises `_IndexUnsettled`."""
    ends = _IndexUnsettled if complete else _IndexShort
    if len(data) < 12:
        raise ends
    if data[:4] != b"DIRC":
        raise _IndexUnsettled
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3, 4):
        raise _IndexUnsettled
    count = int.from_bytes(data[8:12], "big")
    found: list[tuple[int, int, bytes]] = []
    pos = 12
    previous = b""
    for _ in range(count):
        start = pos
        flags_at = start + 40 + 20
        if flags_at + 2 > len(data):
            raise ends
        mode = int.from_bytes(data[start + 24 : start + 28], "big")
        oid = data[start + 40 : flags_at]
        flags = int.from_bytes(data[flags_at : flags_at + 2], "big")
        name_at = flags_at + 2
        if flags & 0x4000:
            if version < 3:
                raise _IndexUnsettled
            if name_at + 2 > len(data):
                raise ends
            if int.from_bytes(data[name_at : name_at + 2], "big") & ~_EXTENDED_FLAGS:
                raise _IndexUnsettled
            name_at += 2
        stage = (flags >> 12) & 0x3
        length = flags & 0x0FFF
        if version == 4:
            strip, name_at = _decode_varint(data, name_at, complete=complete)
            if strip > len(previous):
                raise _IndexUnsettled
            end = data.find(b"\0", name_at)
            if end < 0:
                raise ends
            name = previous[: len(previous) - strip] + data[name_at:end]
            if length != 0x0FFF and len(name) != length:
                raise _IndexUnsettled
            pos = end + 1
        else:
            if length == 0x0FFF:
                end = data.find(b"\0", name_at)
                if end < 0:
                    raise ends
                length = end - name_at
            name = data[name_at : name_at + length]
            if len(name) != length:
                raise ends
            pos = start + ((name_at - start + length + 8) & ~7)
        if name == _GITMODULES:
            found.append((stage, mode, oid))
        elif name > _GITMODULES:
            break
        previous = name
    return found


def _read_prefix(fd: int, length: int) -> bytes:
    """The first `length` bytes of the open file `fd`, fewer where it ends
    first."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while size < length:
        chunk = os.read(fd, length - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _index_gitmodules(gd: githead.GitDir) -> tuple[object, ...] | None:
    """What the index at `gd` holds for ``.gitmodules``: None when no entry
    (or no index), `_INDEX_UNMERGED` when only stages 1 to 3, and the
    stage-0 entry's ``(mode, object id in hex)`` otherwise, memoised on the
    index's stamp. Raises `_IndexUnsettled` where the index is not read
    exactly: a split index (a ``sharedindex.*`` file in the git directory,
    whose entries this does not open), an index version other than 2, 3 or
    4 or a layout git refuses, a version-2 entry carrying the extended flag
    (git reads such an index but never writes one, so the decline costs a
    walk memo and misreads nothing), a SHA-256 repository (its config sets
    ``extensions.objectFormat``, or cannot be read), and an index rewritten
    in place while it was read; and OSError where a read fails."""
    config = _config_facts(gd.commondir / "config")
    if (
        config is None
        or config[1].names is None
        or b"extensions.objectformat" in config[1].names
    ):
        raise _IndexUnsettled
    path = gd.gitdir / "index"
    key = os.fspath(path)
    flags = _read_flags()
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        if not path_missing(exc):
            raise
        return None
    try:
        before = _fd_stamp(fd)
        if not stat.S_ISREG(before[4]):
            raise _IndexUnsettled
        with _INDEX_MEMO_LOCK:
            kept = _INDEX_MEMO.get(key)
            if kept is not None and kept[0] == before:
                _INDEX_MEMO.move_to_end(key)
                return kept[1]
        if any(name.startswith("sharedindex.") for name in os.listdir(gd.gitdir)):
            raise _IndexUnsettled
        # Read, not mapped (macOS maps a file git has just rewritten in about
        # 5 ms), and only as far as the scan goes: the entries up to
        # .gitmodules are near the start.
        size = before[2]
        want = _INDEX_FIRST_READ
        while True:
            data = _read_prefix(fd, min(want, size))
            try:
                entries = _gitmodules_entries(data, complete=len(data) >= size)
                break
            except _IndexShort:
                if want >= size:
                    raise _IndexUnsettled from None
                want *= _INDEX_READ_GROWTH
        if _fd_stamp(fd) != before:
            raise _IndexUnsettled
    finally:
        os.close(fd)
    entry: tuple[object, ...] | None = None
    for stage, mode, oid in entries:
        if stage == 0:
            entry = (mode, oid.hex())
    if entry is None and entries:
        entry = _INDEX_UNMERGED
    with _INDEX_MEMO_LOCK:
        _INDEX_MEMO[key] = (before, entry)
        _INDEX_MEMO.move_to_end(key)
        while len(_INDEX_MEMO) > _INDEX_MEMO_CAP:
            _INDEX_MEMO.popitem(last=False)
    return entry


def _gitdir_at_root(root: Path) -> githead.GitDir | None:
    """The repository whose ``.git`` entry sits at `root`: `githead.
    gitdir_at`'s and, where it declines (off POSIX, a repository another
    account owns), `githead.locate`'s without its walk, as the split's memo
    reads it (`origin_patch._memo_key`). None under a variable that moves
    git's discovery, where git may read another repository than either
    names."""
    found = githead.gitdir_at(root)
    if found is None and not githead.discovery_moved():
        found = githead.locate(root, walk=False)
    return found


def walk_files_signature(
    root: Path, *, directories: dict[str, tuple[int, int]] | None = None
) -> tuple[object, ...] | None:
    """The files beside the history that decide what the drift readers'
    logs list, for a memo to key on: the repository's ``config`` in the
    common directory and, where it sets ``extensions.worktreeConfig``, the
    ``config.worktree`` git then reads after it in the git directory
    (`_worktree_config_facts`), the working tree's ``.gitmodules`` at the
    root, and what the index holds for ``.gitmodules`` where git reads it
    (`_index_gitmodules`). What moved a log there (``log.showRoot``
    dropped a root commit's paths from the reachable walk's
    ``--name-only``, ``diff.ignoreSubmodules`` and
    ``submodule.<name>.ignore`` a gitlink's, and ``log.follow`` made a
    log over one path follow it across renames) is pinned on every git
    `origin` runs (`origin_history.AS_STORED_CONFIG`, `AS_STORED_DIFF`);
    the stamps stay, so a setting the pins leave is read again when its
    file moves. Git
    reads the index first: while it holds
    ``.gitmodules`` unmerged, git reads no ``.gitmodules`` at all, and the
    slot is `_INDEX_UNMERGED` whatever the working tree holds; otherwise the
    working tree's file where it exists, and the slot is None; and where it
    does not, the index's stage-0 entry, its mode and object, which is the
    slot, None where the index has none and git reads HEAD's copy, which
    the key's head names. A write to the index that leaves ``.gitmodules``
    as it was (``git add`` of another file, a ``git status`` that refreshes
    the index) is no new key. The configs and the working tree's file are
    each their `_stamp`, or None while they do not exist (config.worktree
    also None where git does not read it). The walk memo
    (`commits_since_anchor`) and the per-hit drift memo key on it.
    `directories`, when given, receives the stamps of the root and of the
    index file (`_hold_directory`), for a caller that checks at the end of
    its work that no ``.gitmodules`` was created and removed meanwhile and
    that the index was not written, which leaves nothing kept from a search
    during which the index's entry moved and moved back.

    Read through `githead.gitdir_at` and, where it declines (off POSIX, a
    repository another account owns), `githead.locate` without its walk
    (`_gitdir_at_root`), both of which read the repository only when
    `root` holds its ``.git`` entry, so a root git names elsewhere
    (``core.worktree``) is never stamped against an enclosing repository.
    None whenever GIT_INDEX_FILE is set (git reads the index it names), where
    both decline (under GIT_DIR and the other variables that move
    discovery, a root that holds no ``.git`` entry), where a
    config cannot be read or is one git refuses, where the index is not one
    `_index_gitmodules` reads exactly (a split index, an unknown version, a
    SHA-256 repository), and where a stat or a read fails for any reason
    but absence; what would key on it is then not memoised. The
    configuration git reads outside those files (the global and system
    files, an include, the environment) is not seen. Nor are two changes
    undone while a walk runs that leave every stamp as it was: a ``config``
    or ``config.worktree`` created and removed in a repository that has
    none (the directory that would hold it is not held, only the root; a
    missing index is held through the git directory), and a ``config`` that
    is a symbolic link whose target's directory is swapped for another and
    back (the stamp follows the link to the file it named before, with its
    old stamp)."""
    if "GIT_INDEX_FILE" in os.environ:
        return None
    gd = _gitdir_at_root(root)
    if gd is None:
        return None
    try:
        if directories is not None:
            _hold_directory(root, directories)
            _hold_directory(gd.gitdir / "index", directories)
        common = _config_facts(gd.commondir / "config")
        if common is None or common[1].names is None:
            return None
        worktree = _worktree_config_facts(gd, common[1])
        if worktree is None:
            return None
        gitmodules = _stamp(root / ".gitmodules")
        entry = _index_gitmodules(gd)
        if entry == _INDEX_UNMERGED or gitmodules is None:
            index = entry
        else:
            index = None
        return (common[0], worktree[0], gitmodules, index)
    except (OSError, ValueError, _IndexUnsettled):
        return None


def located_head(root: Path) -> str | None:
    """The head `githead.locate` reads in the repository whose ``.git``
    entry sits at `root`, for a key settled on git's answer where
    `githead`'s discovery declines (`_response._DriftPass`)."""
    found = githead.locate(root, walk=False)
    return None if found is None else githead.head_sha(found)


def files_held(
    cwd: Path,
    root: Path,
    repository: object,
    chains: dict[tuple[str, ...], tuple[object, ...] | None],
    walk_files: tuple[object, ...] | None,
    directories: dict[str, tuple[int, int]],
) -> bool:
    """Whether the files a search keyed its hits on still read as they did
    when it keyed them: `repository` is what `attribute_files_signature`
    gave, `chains` what `gitattributes_signature` gave for each set of
    governed files, `walk_files` what `walk_files_signature` gave, and
    `directories` the stamps those three recorded of the directories that
    hold the files (`directories_held`), which move when a file there was
    created and removed in between, absent both times. A part that keyed
    nothing (never read, or None) is not read again."""
    if repository is not None and attribute_files_signature(cwd, root) != repository:
        return False
    if walk_files is not None and walk_files_signature(root) != walk_files:
        return False
    if not directories_held(directories):
        return False
    return all(
        chain is None or gitattributes_signature(root, specs) == chain
        for specs, chain in chains.items()
    )
