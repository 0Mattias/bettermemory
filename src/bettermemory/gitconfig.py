"""Git's config files, read as git reads them (ENG-160).

`_config_values` reads a config file as bytes and bounded, its includes
followed in place as git's ``config.c`` follows them (``include.path``,
and ``includeIf.<condition>.path`` where the condition is ``gitdir:`` or
``gitdir/i:``), into a `ConfigValues` that says where it read less than
git would (`ConfigValues.complete`) and which names an include sets under
a condition not evaluated here (`ConfigValues.undecided`); `_truthy` reads
a boolean value as git does.

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
    taken."""

    def __init__(self) -> None:
        super().__init__()
        self.complete = True
        self.undecided: set[_Name] = set()


class _Overflow(Exception):
    """An include past `_INCLUDE_DEPTH` or `_INCLUDE_FILES`, or one naming
    a file it is read from: the read stops where it stands (review M D3)."""


@dataclass
class _Reading:
    """One `_config_values` read: the files it has taken, and the
    identity (device, inode) of each file open on the include chain."""

    files: int = 0
    chain: list[tuple[int, int]] = field(default_factory=list)


def _config_values(path: str, *, git_dir: str | None = None) -> ConfigValues:
    """``{(section, subsection, key): values}`` of a git config file, read
    as bytes and bounded, its includes followed in place as git follows
    them (ENG-160): ``include.path``, and ``includeIf.<condition>.path``
    where the condition is ``gitdir:`` or ``gitdir/i:`` matched against
    `git_dir`. Section and key names are lower case. An include past
    git's depth, one naming a file it is read from, or one past
    `_INCLUDE_FILES` files stops the read (review M D3): the values read
    before stand, and the answer is incomplete."""
    out = ConfigValues()
    try:
        _read_config(path, out, git_dir, 0, _Reading())
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
    the include chain is read from (compared as filesystem objects)."""
    reading.files += 1
    if reading.files > _INCLUDE_FILES:
        raise _Overflow(path)
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
