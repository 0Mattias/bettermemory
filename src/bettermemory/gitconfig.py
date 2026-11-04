"""Git's config files, read as git reads them (ENG-160).

`_config_values` reads a config file as bytes and bounded, its includes
followed in place as git's ``config.c`` follows them (``include.path``,
and ``includeIf.<condition>.path`` where the condition is ``gitdir:`` or
``gitdir/i:``), into a `ConfigValues` that says where it read less than
git would (`ConfigValues.complete`), which names an include sets under a
condition not evaluated here (`ConfigValues.undecided`) and each file it
read or looked for, stamped as it read it (`ConfigValues.files`), so a
reader kept against them sees a change to any (`unchanged`, ENG-207);
`_truthy` reads a boolean value as git does.

The reader lives apart from `location_git`, which read it alone before,
because the rule a ``.git`` entry is followed by (`gitfile_rule`) reads a
git directory's ``core.worktree`` and ``core.bare`` through it, and
`githead` reads that rule too (ENG-193): `githead` cannot import
`location_git`, which imports `githead`. `location_git` imports this
module and reads its names from here (`remote_urls`, `_from_env`,
`_discover`); this module imports the standard library alone and nothing
of the package, so it never loads `location_git` or `githead`
(tests/test_import_graph.py). The hook client loads it with `location`
(tests/test_membership_capture.py pins that set).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field

# A repository's config: git reads any size; the eight on the development
# machine measured 349 B at the median and 608 B at most (ENG-160). One
# over the bound is read to its last whole line within it.
_CONFIG_LIMIT = 1 << 20


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


#: A config variable's name: section, subsection, key.
_Name = tuple[str, str | None, str]
#: A config file's stamp, the fields `githead.stamp` reads of a path:
#: ``(st_mtime_ns, st_ctime_ns, st_size, st_ino, st_mode)``; `_UNSTATED`
#: where the path could not be stat-ed, None where nothing is there.
Stamp = tuple[int, int, int, int, int]
_UNSTATED: Stamp = (-1, -1, -1, -1, -1)
#: git's bound on nested includes (``MAX_INCLUDE_DEPTH`` in config.c), past
#: which git refuses the configuration.
_INCLUDE_DEPTH = 10
#: Config files one read takes in all, the file itself and each include, a
#: file included again counted again (review M D3). git has no such bound,
#: but it stops at its depth, and one file naming itself in four
#: ``include.path`` lines was read 1,398,101 times here (38 s, every value
#: kept); past this many files the read stops and reads as incomplete.
#: Measured (`_config_values`, macOS, warm): 64 files (a two-line file
#: included 63 times) in 1.2 ms, the median of 50 reads; the self-including
#: config now stops at its first include in 0.05 ms.
_INCLUDE_FILES = 64


class ConfigValues(dict[_Name, list[str]]):
    """`_config_values`'s answer: ``{(section, subsection, key): values}``.

    `complete` is False where git reads more than was read here: a file
    over `_CONFIG_LIMIT` (read to its last whole line within the bound),
    an include nested past `_INCLUDE_DEPTH`, one naming a file it is read
    from (a cycle, which git follows to its depth and refuses), the
    includes past `_INCLUDE_FILES` files read in all, or a file that could
    not be read or is not a regular file (one that does not exist is
    skipped, as git skips it). The first three stop the read where it
    stands, as git stops at its depth; what was read before stands. `undecided` holds the names an include sets under a
    condition not evaluated here (``onbranch:``, ``hasconfig:``, a
    ``gitdir:`` with no git directory given); their values are not
    taken. `files`, for a read asked to stamp (`_config_values`'s
    `stamped`), holds each file the read opened or looked for, in the
    order it did, with its stamp taken before it was opened (a change
    after it reads as a change to what was read): None for one that was
    not there, which git skips and whose creation changes what git reads
    (ENG-207); an include under a condition not evaluated is not among
    them, since none of its values is taken. It is None for a read not
    asked to stamp."""

    def __init__(self, *, stamped: bool = False) -> None:
        super().__init__()
        self.complete = True
        self.undecided: set[_Name] = set()
        self.files: list[tuple[str, Stamp | None]] | None = [] if stamped else None


class _Overflow(Exception):
    """An include past `_INCLUDE_DEPTH` or `_INCLUDE_FILES`, or one naming
    a file it is read from: the read stops where it stands (review M D3)."""


@dataclass
class _Reading:
    """One `_config_values` read: the files it has taken, the identity
    (device, inode) of each file open on the include chain, and whether a
    stamped read stamps its own file."""

    files: int = 0
    chain: list[tuple[int, int]] = field(default_factory=list)
    stamp_self: bool = True


def _config_values(
    path: str,
    *,
    git_dir: str | None = None,
    stamped: bool = False,
    stamp_self: bool = True,
) -> ConfigValues:
    """``{(section, subsection, key): values}`` of a git config file, read
    as bytes and bounded, its includes followed in place as git follows
    them (ENG-160): ``include.path``, and ``includeIf.<condition>.path``
    where the condition is ``gitdir:`` or ``gitdir/i:`` matched against
    `git_dir`. Section and key names are lower case. An include past
    git's depth, one naming a file it is read from, or one past
    `_INCLUDE_FILES` files stops the read (review M D3): the values read
    before stand, and the answer is incomplete. `stamped` has the answer
    name each file read and its stamp (`ConfigValues.files`, ENG-207),
    `path` itself left out with `stamp_self` off (a caller that stamps it
    already)."""
    out = ConfigValues(stamped=stamped)
    try:
        _read_config(path, out, git_dir, 0, _Reading(stamp_self=stamp_self))
    except _Overflow:
        out.complete = False
    return out


def _config_bytes(
    path: str, out: ConfigValues, identity: list[tuple[int, int]] | None = None
) -> bytes | None:
    """The bytes of the config file `path`, at most `_CONFIG_LIMIT` of
    them: one larger is cut at its last whole line within the bound (ENG-160:
    it was dropped whole, and its remotes with it) and `out` is incomplete.
    None where the file does not exist; None and `out` incomplete where it
    cannot be read or is not a regular file. The open does not block, so
    an include naming a FIFO fails instead of waiting. The file's (device,
    inode) is appended to `identity` where the OS gives one."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        out.complete = False
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            out.complete = False
            return None
        if identity is not None and st.st_ino:
            identity.append((st.st_dev, st.st_ino))
        while size <= _CONFIG_LIMIT:
            chunk = os.read(fd, _CONFIG_LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        out.complete = False
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if size > _CONFIG_LIMIT:
        out.complete = False
        data = data[: data.rfind(b"\n", 0, _CONFIG_LIMIT) + 1]
    return data


def _read_config(
    path: str, out: ConfigValues, git_dir: str | None, depth: int, reading: _Reading
) -> None:
    """Parse the config file `path` into `out`, its includes in place;
    `_Overflow` past `_INCLUDE_FILES` files, or where `path` is a file
    the include chain is read from (compared as filesystem objects). For
    a stamped read the file goes to `out.files` stamped before it is
    opened (`ConfigValues.files`), so a change after the stamp reads as a
    change to what was read; the read's own file only where
    `reading.stamp_self` says so."""
    reading.files += 1
    if reading.files > _INCLUDE_FILES:
        raise _Overflow(path)
    if out.files is not None and (depth or reading.stamp_self):
        out.files.append((path, file_stamp(path)))
    identity: list[tuple[int, int]] = []
    data = _config_bytes(path, out, identity)
    if data is None:
        return
    if identity and identity[0] in reading.chain:
        raise _Overflow(path)
    reading.chain.extend(identity)
    try:
        _parse_config(path, data, out, git_dir, depth, reading)
    finally:
        del reading.chain[len(reading.chain) - len(identity) :]


def _parse_config(
    path: str,
    data: bytes,
    out: ConfigValues,
    git_dir: str | None,
    depth: int,
    reading: _Reading,
) -> None:
    """The lines of the config file `path` (its bytes `data`) into `out`,
    each include read where it stands."""
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
        text = _unquote(value) if eq else "true"
        out.setdefault((section[0], section[1], key), []).append(text)
        if key == "path" and section[0] in ("include", "includeif"):
            _include(
                path,
                section[1] if section[0] == "includeif" else None,
                text,
                out,
                git_dir,
                depth,
                reading,
            )


def _include(
    including: str,
    condition: str | None,
    value: str,
    out: ConfigValues,
    git_dir: str | None,
    depth: int,
    reading: _Reading,
) -> None:
    """Read the file an include names into `out`, as git does (config.c's
    ``handle_path_include``): ``~/`` is the home directory and a relative
    path is taken from the including file's directory; a missing file is
    skipped. Under `condition` (an ``includeIf`` section's) only where it
    holds; where it is not evaluated here, the names the file sets go to
    `out.undecided` and its values are not taken. Past git's depth,
    `_Overflow`: git refuses the configuration there (review M D3)."""
    target = _expand_home(value)
    if not target:
        return
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(including), target)
    holds = True if condition is None else _condition(condition, including, git_dir)
    if holds is False:
        return
    if depth + 1 > _INCLUDE_DEPTH:
        raise _Overflow(target)
    if holds:
        _read_config(target, out, git_dir, depth + 1, reading)
        return
    maybe = ConfigValues()
    try:
        _read_config(target, maybe, git_dir, depth + 1, reading)
    finally:
        out.undecided.update(maybe, maybe.undecided)
        out.complete = out.complete and maybe.complete


def file_stamp(path: str) -> Stamp | None:
    """The stamp of the file at `path` now (`Stamp`): None where nothing
    is there, `_UNSTATED` where it cannot be stat-ed."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        return _UNSTATED
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


def _home() -> str | None:
    """The home directory git expands ``~`` to: HOME, which git for Windows
    sets at start where it is unset; else the platform's."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return None if home == "~" else home


def _expand_home(value: str) -> str:
    """`value` with a leading ``~/`` (or ``~user/``) expanded; empty when it
    names a home that cannot be found."""
    if value == "~" or value.startswith(("~/", "~" + os.sep)):
        home = _home()
        return home + value[1:] if home else ""
    if value.startswith("~"):
        expanded = os.path.expanduser(value)
        return "" if expanded == value else expanded
    return value


def _condition(condition: str, including: str, git_dir: str | None) -> bool | None:
    """Whether an ``includeIf`` condition holds (config.c's
    ``include_condition_is_true``): ``gitdir:`` and ``gitdir/i:`` against
    `git_dir`; None where it is not evaluated here (``onbranch:``,
    ``hasconfig:``, or no git directory given); False for a condition git
    does not know, which git ignores."""
    for prefix, icase in (("gitdir:", False), ("gitdir/i:", True)):
        if condition.startswith(prefix):
            if git_dir is None:
                return None
            return _gitdir_matches(condition[len(prefix) :], including, git_dir, icase)
    if condition.startswith(("onbranch:", "hasconfig:")):
        return None
    return False


def _slashed(path: str) -> str:
    """`path` with the platform's separator written ``/``, as git spells
    the paths a ``gitdir:`` pattern is matched against."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _gitdir_matches(pattern: str, including: str, git_dir: str, icase: bool) -> bool:
    """config.c's ``include_by_gitdir``: the pattern with ``~/`` expanded,
    ``./`` taken from the including file's directory (that part matched
    literally), ``**/`` put before a relative one and ``**`` after one that
    ends in ``/``; matched as a path against the real git directory, then
    against it as given."""
    literal = 0
    if pattern == "~" or pattern.startswith("~/"):
        home = _home()
        if not home:
            return False
        pattern = _slashed(home) + pattern[1:]
    if pattern.startswith("./"):
        base = _slashed(os.path.dirname(os.path.realpath(including)))
        pattern = base + pattern[1:]
        literal = len(base) + 1
    elif not os.path.isabs(pattern):
        pattern = "**/" + pattern
    if pattern.endswith("/"):
        pattern += "**"
    seen = set()
    for text in (os.path.realpath(git_dir), os.path.abspath(git_dir)):
        text = _slashed(text)
        if text not in seen and _wildmatch(pattern, text, literal, icase):
            return True
        seen.add(text)
    return False


def _wildmatch(pattern: str, text: str, literal: int, icase: bool) -> bool:
    """git's ``wildmatch`` with ``WM_PATHNAME`` over `text`, the first
    `literal` characters of `pattern` taken as they are: ``*``, ``?`` and a
    bracket do not match ``/``; a ``**`` that is a whole path component
    matches across it, ``**/`` matching no directory too."""
    out = [re.escape(pattern[:literal])]
    i, n = literal, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            whole = (i == 0 or pattern[i - 1] == "/") and (j == n or pattern[j] == "/")
            if j - i > 1 and whole:
                out.append("(?:.*/)?" if j < n else ".*")
                j += 1 if j < n else 0
            else:
                out.append("[^/]*")
            i = j
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[" and (end := _bracket_end(pattern, i)) is not None:
            body = pattern[i + 1 : end]
            negated = body[:1] in ("!", "^")
            body = body[1:] if negated else body
            spelled = "".join(c if c == "-" else re.escape(c) for c in body)
            out.append(f"(?!/)[{'^' if negated else ''}{spelled}]")
            i = end + 1
        elif ch == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(ch))
            i += 1
    flags = re.IGNORECASE | re.DOTALL if icase else re.DOTALL
    return re.fullmatch("".join(out), text, flags) is not None


def _bracket_end(pattern: str, start: int) -> int | None:
    """Where the bracket expression at `start` closes, or None (an
    unclosed ``[`` is matched as itself)."""
    i = start + 1
    if i < len(pattern) and pattern[i] in "!^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    end = pattern.find("]", i)
    return None if end < 0 else end


def _truthy(values: Sequence[str] | None) -> bool | None:
    """The boolean the last of `values` (a variable's values, in the order
    `_config_values` read them) spells as git spells one, or None: no
    value, or one that is no boolean."""
    if not values:
        return None
    last = values[-1].strip().lower()
    if last in ("true", "yes", "on", "1"):
        return True
    if last in ("false", "no", "off", "0", ""):
        return False
    return None


# ---------------------------------------------------------------------------
# git's own parse of one config file, for the repository format (ENG-211)
# ---------------------------------------------------------------------------

#: The bytes a config file read for the repository's format may hold
#: (`read_git_config`). git reads any size; one larger is not read here,
#: and the git directory is refused (fail closed, ENG-211). Twice the
#: bound `_config_values` reads remotes within, so a config of 30,000
#: branch sections (1.8 MB, tests/test_location_git_config.py) is read
#: whole. Measured (`git_entries`, macOS, Python 3.13): 11 us for a config
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


def read_git_config(path: str) -> GitConfigRead:
    """The variables the config file `path` sets, read whole and parsed as
    git parses it (`git_entries`), with no include followed: git's read of
    a repository's format (setup.c's ``read_repository_format``) follows
    none, nor reads a global or system file (ENG-211). A file that does not
    exist sets nothing, as git reads it. The open does not block, so a FIFO
    fails instead of waiting."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return GitConfigRead()
    except (OSError, ValueError) as exc:
        return GitConfigRead(why=f"{path} could not be read ({type(exc).__name__})")
    chunks: list[bytes] = []
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return GitConfigRead(why=f"{path} is not a regular file")
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
    entries = git_entries(b"".join(chunks))
    if entries is None:
        return GitConfigRead(why=f"{path} is a config git cannot parse")
    return GitConfigRead(tuple(entries))


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
