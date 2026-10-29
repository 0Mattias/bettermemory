"""The repository files `origin` reads in-process, with no git process.

The config reader (`_config_variable_names`, memoised per file state in
`_config_facts`), the root spelled as git prints it (`_spelled_as_git`),
the root of a repository git refused to read (`refused_worktree_root`),
the paths the capture has warned are no directory (`first_not_a_directory`),
the decoder of git's C-quoted patch headers, the index's ``.gitmodules``
entry, and the file signatures the drift memos key on
(`attribute_files_signature`, `gitattributes_signature`,
`walk_files_signature`, `directories_held`).

This module lives apart from `origin.py` to keep each module under the
package's 2,000-line bound (ENG-96). It imports the standard library,
`_caches` and `githead`; `origin` imports it, never the reverse, and
re-exports every name a caller reads. Patches of `_config_variable_names`
and `_INDEX_FIRST_READ` go here, where their readers are.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _caches, githead


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
    anywhere in it, in any case), and whether a line names ``bare`` with
    anything but a plain false."""

    names: frozenset[bytes] | None
    attributes_elsewhere: bool
    bare_moves: bool


_NO_CONFIG = _ConfigFacts(
    names=frozenset(), attributes_elsewhere=False, bare_moves=False
)


def _facts_of(data: bytes) -> _ConfigFacts:
    names = _config_variable_names(data)
    lowered = data.lower()
    return _ConfigFacts(
        names=None if names is None else frozenset(names),
        attributes_elsewhere=b"attributesfile" in lowered or b"[attr" in lowered,
        bare_moves=any(
            b"bare" in line and _BARE_FALSE.fullmatch(line) is None
            for line in lowered.split(b"\n")
        ),
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


def _config_facts(path: Path) -> tuple[githead.Stamp | None, _ConfigFacts] | None:
    """``(stamp, facts)`` of the configuration file `path`: its `githead.
    stamp`, None when it does not exist (and then facts that set nothing),
    and what it holds. None when it cannot be read, is not a regular file
    or is larger than `_CONFIG_LIMIT`. The open does not block, so a FIFO
    fails instead of waiting."""
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
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, _NO_CONFIG
    except OSError:
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
    a plain false, and a file git refuses, that holds a NUL byte or that
    cannot be read. config.worktree is read whether or not the extension
    is set, an over-decline that costs one process where a file git does
    not read sets either. The answer only decides whether the files or git
    answer, so a false yes costs one process and a false no would be a
    wrong root."""
    for path in (gd.commondir / "config", gd.gitdir / "config.worktree"):
        read = _config_facts(path)
        if read is None:
            return True
        facts = read[1]
        if facts.names is None or b"core.worktree" in facts.names or facts.bare_moves:
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
    finds no entry the exit is the answer, ``(None, False)``. Where it
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

    A header line is rewritten only when the whole remainder decodes as
    one complete quoted string AND the decoded path starts with the
    pinned ``a/`` (old side) or ``b/`` (new side) prefix. That guard
    keeps hunk-body content lines out: a removed source line
    ``-- "x"`` renders as ``--- "x"`` in the stream, but its decoded
    body doesn't start with a prefix, so it passes through byte-exact.
    (A content line spelling a quoted a/-path remains theoretically
    rewritable — the cost is one slightly-off change anchor, never a
    missed deletion.) The fast path keeps the zero-quoted-header case,
    i.e. essentially every real stream, allocation-free.
    """
    if '--- "' not in stream and '+++ "' not in stream:
        return stream
    lines = stream.split("\n")
    for idx, line in enumerate(lines):
        if not line.startswith(('--- "', '+++ "')):
            continue
        decoded = _unquote_c_path(line[4:])
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
        except (FileNotFoundError, NotADirectoryError):
            parent = os.path.dirname(current)
            if parent == current:
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


# git's isspace (sane_ctype in ctype.c), the letters a variable's name
# starts with, the characters it continues with (iskeychar), and the
# escapes a value may hold. ASCII only, as git's are.
_CONFIG_SPACE = frozenset(b" \t\n\r")
_CONFIG_ALPHA = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_CONFIG_KEYCHAR = _CONFIG_ALPHA | frozenset(b"0123456789-")
_CONFIG_ESCAPES = frozenset(b'tbn\\"')
_UTF8_BOM = b"\xef\xbb\xbf"


def _config_variable_names(data: bytes) -> set[bytes] | None:
    """The names of the variables the config file `data` sets, as git's own
    parser reads the file (``git_parse_source`` in config.c) and ``git
    config --list --name-only`` prints them: ``section.key`` or
    ``section.subsection.key``, the section and the key in lower case, a
    quoted subsection as written. A section header wherever a key may
    start, a key with or without a value, comments, quoted and escaped
    values, a value continued onto the next line by a backslash outside a
    comment, CRLF line ends and a leading byte order mark are read as git
    reads them, so ``log.follow`` is named in any case or layout git
    accepts and a ``follow`` inside a value, a comment, a subsection or a
    longer key is not. None where git refuses the file ("bad config
    line"), and None for any file holding a NUL byte: git keeps a name only
    up to a NUL in it, so ``[log "follow\\0"] x`` sets ``log.follow`` to git
    (and an escaped letter spells the word without its bytes), which this
    reader does not model. Past those, the names are the ones git lists,
    pinned against git on the texts of tests/test_commit_drift_cache.py.
    Values are read only to find where they end."""
    if b"\0" in data:
        return None
    text = data.replace(b"\r\n", b"\n")
    end = len(text)
    pos = 0
    if text[:1] == _UTF8_BOM[:1]:
        if not text.startswith(_UTF8_BOM):
            return None
        pos = len(_UTF8_BOM)
    names: set[bytes] = set()
    section = b""
    comment = False
    while pos < end:
        c = text[pos]
        pos += 1
        if c == 0x0A:
            comment = False
        elif comment or c in _CONFIG_SPACE:
            continue
        elif c in b"#;":
            comment = True
        elif c == 0x5B:  # "["
            header = _config_section(text, pos)
            if header is None:
                return None
            section, pos = header
        elif c in _CONFIG_ALPHA:
            start = pos - 1
            while pos < end and text[pos] in _CONFIG_KEYCHAR:
                pos += 1
            key = text[start:pos].lower()
            while pos < end and text[pos] in b" \t":
                pos += 1
            if pos < end:
                c = text[pos]
                pos += 1
                if c == 0x3D:  # "="
                    after = _config_value_end(text, pos)
                    if after is None:
                        return None
                    pos = after
                elif c != 0x0A:
                    return None
            names.add(section + b"." + key if section else key)
        else:
            return None
    return names


def _config_section(text: bytes, pos: int) -> tuple[bytes, int] | None:
    """The name a section header gives the keys under it, and the position
    after its ``]``, for a header whose ``[`` ends at `pos`; None where git
    refuses the header (``get_base_var`` and ``get_extended_base_var`` in
    config.c). ``[name]`` and the older ``[name.sub]`` are lower-cased
    whole; ``[name "sub"]`` keeps the subsection as written, each
    backslash escape read as the character it escapes."""
    end = len(text)
    name = bytearray()
    while True:
        if pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c == 0x5D:  # "]"
            return (bytes(name), pos) if name else None
        if c in _CONFIG_SPACE:
            break
        if c not in _CONFIG_KEYCHAR and c != 0x2E:  # "."
            return None
        name.append(c + 32 if 0x41 <= c <= 0x5A else c)
    while True:
        if c == 0x0A or pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c not in _CONFIG_SPACE:
            break
    if c != 0x22:  # a quoted subsection must follow
        return None
    name.append(0x2E)
    while True:
        if pos >= end:
            return None
        c = text[pos]
        pos += 1
        if c == 0x0A:
            return None
        if c == 0x22:
            break
        if c == 0x5C:  # a backslash escapes the next character
            if pos >= end or text[pos] == 0x0A:
                return None
            c = text[pos]
            pos += 1
        name.append(c)
    if pos >= end or text[pos] != 0x5D:
        return None
    return bytes(name), pos + 1


def _config_value_end(text: bytes, pos: int) -> int | None:
    """Where the value that starts at `pos` ends: past the newline that
    ends its line, or a later one where a backslash outside a comment
    continues it. None where git refuses the value (``parse_value`` in
    config.c): a quote still open at its end, or an escape git does not
    know."""
    end = len(text)
    quote = False
    comment = False
    while pos < end:
        c = text[pos]
        pos += 1
        if c == 0x0A:
            return None if quote else pos
        if comment or (c in _CONFIG_SPACE and not quote):
            continue
        if c in b"#;" and not quote:
            comment = True
        elif c == 0x5C:
            if pos < end:
                if text[pos] != 0x0A and text[pos] not in _CONFIG_ESCAPES:
                    return None
                pos += 1
        elif c == 0x22:
            quote = not quote
    return None if quote else pos


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
    ``log.follow`` (it makes the single-pathspec patch stream follow a
    claimed file across a rename and diff the renaming commit with the
    attributes of the rename source's path, which no chain of the claimed
    path covers), one git refuses, one that holds a NUL byte (git ends a
    variable's name at it, so a quoted subsection can set ``log.follow`` or
    ``core.attributesFile`` without the words' bytes), and a stat that
    fails for any reason but absence. Whether a file sets ``log.follow`` is
    read as git reads it (`_config_variable_names`, memoised per state of
    the file in `_config_facts`), in any case or layout, so
    ``push.followTags``, a branch or a remote whose name holds ``follow``,
    or the word in a value or a comment, keys the files like any other
    config. A ``log.follow`` set outside the repository's own files (the
    global and system files, an include, the environment) is not seen.
    `root` is the root `cwd`'s repository names."""
    gd = githead.find_gitdir(cwd)
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
            except (FileNotFoundError, NotADirectoryError):
                pass
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
# Bounded; registered with `_caches`.
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
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
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


def walk_files_signature(
    root: Path, *, directories: dict[str, tuple[int, int]] | None = None
) -> tuple[object, ...] | None:
    """The files beside the history that decide what the drift readers'
    logs list, for a memo to key on: the repository's ``config`` in the
    common directory (``log.showRoot`` there drops a root commit's paths
    from the reachable walk's ``--name-only``, ``diff.ignoreSubmodules`` a
    gitlink's, and ``log.follow`` makes a log over one path follow it
    across renames) and, where it sets ``extensions.worktreeConfig``, the
    ``config.worktree`` git then reads after it in the git directory, from
    which the same settings reach the logs (`_worktree_config_facts`), the
    working tree's ``.gitmodules`` at the root (``submodule.<name>.ignore``
    there drops a gitlink's changes from the walk), and what the index
    holds for ``.gitmodules`` where git reads it (`_index_gitmodules`). Git
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

    Read through `githead.gitdir_at`, which reads the repository only when
    `root` holds its ``.git`` entry, so a root git names elsewhere
    (``core.worktree``) is never stamped against an enclosing repository.
    None whenever GIT_INDEX_FILE is set (git reads the index it names), where
    `githead` declines (off POSIX, under GIT_DIR and the other variables
    that move discovery, a root that holds no ``.git`` entry), where a
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
    gd = githead.gitdir_at(root)
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
