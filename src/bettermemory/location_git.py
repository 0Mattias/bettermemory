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
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
#: One decides it; with HEAD unborn the tips are where the rewritten-history
#: walk starts (`walk`), and each rides in the location token. Measured
#: (ENG-160, macOS APFS): 64 of 1,000 loose branch refs read in 1.6 ms, all
#: 1,000 in 20 ms (0.1 and 0.7 ms packed); 64 tips are about 2.8 KB of the
#: token.
REF_TIPS_MAX = 64
#: The most (path, size) entries of a content manifest (design 2.2, C),
#: read only for a commit-less repository and compared on the same sorted
#: walk on both sides. Measured (ENG-160) on a tree of 6,000 files: 4,096
#: entries listed in 27.5 ms and 89 KiB as JSON, 1,024 in 6 ms and 22 KiB;
#: the whole tree took 50 ms and 131 KiB.
MANIFEST_LIMIT = 4096
#: How long a fetch of an evicted HEAD or config may take before the
#: capture defers (design 3.1, decision D19; 300 ms in the prototype).
FETCH_BUDGET = 0.3
#: How long a read of a recorded path (a view's liveness, the migration's
#: probe of a recorded root) may take before the path reads unreachable,
#: unknown rather than gone (ENG-156). A view's liveness read takes about
#: 1 ms on a local disk (`view_state`: a few stats and the token, HEAD and
#: init-stamp files) and a round trip more on a healthy network mount; a
#: hard-mounted NFS export or a dropped SMB share that does not answer
#: holds the read indefinitely, so the bound is what a hook waits for it,
#: once per path while that read is still in flight.
PROBE_BUDGET = 0.5
#: How long a capture answers for its directory, as `origin.capture`'s
#: cache (`origin.ORIGIN_CACHE_SECONDS`), on both clocks.
CACHE_SECONDS = 600.0
#: Captures kept, oldest evicted first; the project files above a
#: directory and the per-location locks are bounded from it. A capture is
#: about 3 KB as JSON (this repository's 3,094 bytes, a folder's 646;
#: ENG-160), so 256 are under 1 MB as JSON, and a miss costs one capture
#: (0.8 ms for this repository uncached, 0.12 ms cached, measured on
#: macOS).
CACHE_CAP = 256
#: Commits a rewritten-history walk reads from HEAD; the model's
#: `membership.WALK_LIMIT`, so a client's walk covers exactly the model's.
#: Measured (ENG-160) on this repository's history of 2,011 commits from a
#: fresh reader: 512 commits in 55 ms, 128 in 14 ms, all 2,011 in 268 ms.
#: A witness older than the newest 512 commits is not reached by the walk,
#: and the other witnesses decide. `reachable_among` (ENG-151) reads the
#: same 512 commits in all, shared among the commits it starts from, about
#: 512/n below each of n: on a 20,000-commit line, 128 tied witnesses five
#: or more commits apart find no relation (review M G2), so the most
#: specific line is not decided and the ladder asks.
WALK_LIMIT = 512
#: Blob ids sampled per witness commit, and their floor in bytes: small
#: files (an empty ``__init__.py``) are shared by unrelated repositories.
BLOBS_PER_WITNESS = 8
BLOB_MIN_SIZE = 64
#: Tree entries looked at for the blob sample (the root and its first level).
#: Measured (ENG-160) on this repository's HEAD: 16 entries already gave the
#: 8 blobs in 2.6 ms, 64 in 5.6 ms, 256 in 18.5 ms; 64 keeps 8 where the
#: root holds folders and files under `BLOB_MIN_SIZE`.
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
#: Rounds of the witness exchange a client answers before it stops (a
#: prompt's hook then goes on without its resolution). Each round answers
#: one kind of question (held, walk, ancestry, blobs, views, remint) and the
#: answers accumulate; measured over the exchanges the suite runs
#: (tests/test_hook.py, test_membership_remint, test_membership_security,
#: test_membership_resolution, test_location_reachable; ENG-160): 16
#: exchanges took 1 round in 4, 2 in 10 and 3 in 2. Eight bounds a prompt
#: at eight posts.
EXCHANGE_ROUNDS = 8

_FULL_HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HEAD_LIMIT = 1 << 16
# A repository's config: git reads any size; the eight on the development
# machine measured 349 B at the median and 608 B at most (ENG-160). One
# over the bound is read to its last whole line within it.
_CONFIG_LIMIT = 1 << 20
# git refuses a .git file over 1 MiB (read_gitfile_gently in setup.c).
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


@dataclass
class _Flight:
    """One bounded read in flight: when it started, whether it ended, and
    its outcome (``(True, value)`` or ``(False, exception)``)."""

    started: float
    done: threading.Event = field(default_factory=threading.Event)
    outcome: list[tuple[bool, Any]] = field(default_factory=list)


_FLIGHTS_LOCK = threading.Lock()
#: The bounded reads in flight, by path and then by what each reads there.
_FLIGHTS: dict[str, dict[Any, _Flight]] = {}
#: What `bounded_read` returns for a read that did not answer within its
#: budget, and `bounded_reads` for one that did not answer or whose path
#: has a read in flight past the budget.
UNANSWERED: Any = object()
#: What `bounded_reads` returns for a read that raised or never started.
NOT_READ: Any = object()


def _board(
    path: str, what: Any, now: float, budget: float
) -> tuple[_Flight | None, bool]:
    """The flight to wait on for a read of `path` and `what`, and whether
    the caller is to run it: the one in flight, else a new one; None when
    another read of `path` is in flight past `budget` (it is stalled)."""
    with _FLIGHTS_LOCK:
        flights = _FLIGHTS.get(path, {})
        flight = flights.get(what)
        if flight is not None:
            return flight, False
        if any(now - f.started >= budget for f in flights.values()):
            return None, False
        flight = _FLIGHTS.setdefault(path, {})[what] = _Flight(now)
        return flight, True


def _fly(path: str, what: Any, flight: _Flight, read: Callable[[], Any]) -> None:
    """Run `read` for `flight` and land it: its outcome kept, the flight
    off the board, its waiters woken."""
    try:
        flight.outcome.append((True, read()))
    except BaseException as exc:  # noqa: BLE001 - raised in the caller
        flight.outcome.append((False, exc))
    finally:
        with _FLIGHTS_LOCK:
            mine = _FLIGHTS.get(path)
            if mine is not None and mine.get(what) is flight:
                del mine[what]
                if not mine:
                    del _FLIGHTS[path]
        flight.done.set()


def bounded_read(path: str, what: Any, read: Callable[[], Any], budget: float) -> Any:
    """`read()` of the recorded path `path`, run on a worker thread and
    waited on for `budget` seconds (ENG-156): its value, or `UNANSWERED`
    when it did not answer in time; an exception it raised is raised here.
    A read that outruns the budget keeps running, as a provider's fetch
    does (`_fetch`). One read of a path and `what` is in flight at a time:
    a caller that finds it waits on it within its own budget, and a path
    with a read in flight past the budget answers `UNANSWERED` at once,
    so a hung mount costs one blocked thread and one budget, not one per
    prompt."""
    now = time.monotonic()
    flight, mine = _board(path, what, now, budget)
    if flight is None:
        return UNANSWERED
    if mine:
        threading.Thread(
            target=_fly,
            args=(path, what, flight, read),
            name="bettermemory-probe",
            daemon=True,
        ).start()
    if not flight.done.wait(max(0.0, flight.started + budget - now)):
        return UNANSWERED
    ok, value = flight.outcome[0]
    if not ok:
        raise value
    return value


def bounded_reads(
    jobs: Sequence[tuple[str, Any, Callable[[], Any]]], budget: float
) -> list[Any]:
    """`bounded_read` of each job (path, what, read), the reads run in
    turn on one worker thread rather than one thread each, each waited on
    for `budget` seconds from its start: each job's value; `UNANSWERED`
    where the read did not answer in time or its path has a read in flight
    past the budget; `NOT_READ` where the read raised or another thread is
    reading that path and `what`, for the caller to read alone. A read that
    hangs is left on the board (its path answers every later read at once)
    and the reads after it go on, on a fresh thread."""
    out: list[Any] = [NOT_READ] * len(jobs)
    todo = list(range(len(jobs)))
    while todo:
        todo = _read_in_turn(jobs, todo, out, budget)
    return out


def _read_in_turn(
    jobs: Sequence[tuple[str, Any, Callable[[], Any]]],
    todo: list[int],
    out: list[Any],
    budget: float,
) -> list[int]:
    """One worker thread of `bounded_reads` over the jobs `todo` names:
    the ones it did not start, when one of its reads outran the budget."""
    lock = threading.Lock()
    # The read running (its job, flight and place in `todo`) and whether
    # the caller stopped waiting.
    current: list[tuple[int, _Flight, int]] = []
    stop: list[bool] = []
    finished = threading.Event()

    def run() -> None:
        try:
            for pos, i in enumerate(todo):
                path, what, read = jobs[i]
                with lock:
                    if stop:
                        return
                    flight, mine = _board(path, what, time.monotonic(), budget)
                    if flight is None:
                        out[i] = UNANSWERED
                        continue
                    if not mine:
                        continue
                    current.append((i, flight, pos))
                _fly(path, what, flight, read)
                with lock:
                    current.clear()
                    if stop:
                        return
                    ok, value = flight.outcome[0]
                    if ok:
                        out[i] = value
        finally:
            finished.set()

    threading.Thread(target=run, name="bettermemory-probes", daemon=True).start()
    wait = budget
    while not finished.wait(wait):
        with lock:
            if not current:
                wait = budget
                continue
            i, flight, pos = current[0]
            wait = flight.started + budget - time.monotonic()
            if wait > 0:
                continue
            stop.append(True)
            out[i] = UNANSWERED
            return todo[pos + 1 :]
    return []


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
#: The names that say where a git directory's working tree is.
_TREE_NAMES = frozenset({("core", None, "worktree"), ("core", None, "bare")})


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


def _global_configs() -> list[str]:
    """The global config files git reads, in its order: the one
    GIT_CONFIG_GLOBAL names alone, else ``$XDG_CONFIG_HOME/git/config``
    (``~/.config/git/config``) and then ``~/.gitconfig``."""
    named = os.environ.get("GIT_CONFIG_GLOBAL")
    if named:
        return [named]
    home = _home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out = [os.path.join(xdg, "git", "config")] if xdg else []
    if home:
        if not xdg:
            out.append(os.path.join(home, ".config", "git", "config"))
        out.append(os.path.join(home, ".gitconfig"))
    return out


def remote_urls(common_dir: str, git_dir: str) -> list[str]:
    """Every remote URL in the repository's config, as written and, where a
    ``url.<base>.insteadOf`` prefix in the global or the repository's
    config matches it, as git rewrites it (the longest prefix wins, as in
    remote.c's ``alias_url``): ENG-160. Both are kept, so a checkout keyed
    on the alias before matches as it did, and a clone of the URL the
    alias stands for matches too. The system config is not read."""
    values = _config_values(os.path.join(common_dir, "config"), git_dir=git_dir)
    urls = [
        u
        for (section, _sub, key), found in values.items()
        if section == "remote" and key == "url"
        for u in found
    ]
    if not urls:
        return []
    rules: list[tuple[str, str]] = []
    for config in [
        *(_config_values(p, git_dir=git_dir) for p in _global_configs()),
        values,
    ]:
        for (section, sub, key), found in config.items():
            if section == "url" and sub is not None and key == "insteadof":
                rules.extend((prefix, sub) for prefix in found if prefix)
    out = list(urls)
    for url in urls:
        best = max(
            (r for r in rules if url.startswith(r[0])),
            key=lambda r: len(r[0]),
            default=None,
        )
        if best is not None:
            out.append(best[1] + url[len(best[0]) :])
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
        return _not_registered(target, named)
    if linked:
        back = _small_text(os.path.join(target, "gitdir"))
        if not back:
            return f"{target} is a worktree's git directory that names no worktree"
        if _names_here(back, target, dotgit) is False:
            return f"{target} is the git directory of the worktree at {back}"
        return None
    values = _config_values(os.path.join(target, "config"), git_dir=target)
    if not values.complete or values.undecided & _TREE_NAMES:
        # Where its working tree is cannot be told (ENG-160): a config over
        # the bound, an include past git's depth or unreadable, or one under
        # a condition not evaluated here that names it.
        return f"{target}'s config could not be read whole"
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
    values = _config_values(os.path.join(common, "config"), git_dir=git_dir)
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


def _discover(
    start: str, refused: list[tuple[str, str, str]] | None = None
) -> _Git | None:
    """The repository git would find from `start` (``setup.c``'s walk):
    at each directory a ``.git`` entry first, then the directory itself as
    a git directory (a bare repository, or the inside of one); stopping
    below a ceiling and at a filesystem boundary (unless
    GIT_DISCOVERY_ACROSS_FILESYSTEM). Each ``.git`` file passed over as
    not followed (`gitfile_refusal`) goes to `refused` as (the file, the
    git directory it names, why), and so does a directory holding ``HEAD``
    and a ``commondir`` that names a repository it is not registered under
    (the directory, that repository's common directory, why): git reads
    it as a git directory of that repository, membership passes over it
    (review M G5)."""
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
            found = _at_dotgit(d, dotgit, st, refused)
            if found is not None:
                return found
        if os.path.isfile(os.path.join(d, "HEAD")) and _is_git_directory(d):
            common, linked = _common_of(d)
            values = _config_values(os.path.join(common, "config"), git_dir=d)
            if os.path.basename(d) == ".git" and not _truthy(
                values.get(("core", None, "bare"))
            ):
                parent = os.path.dirname(d)
                return _Git(d, d, common, parent, LINKED if linked else PRIMARY)
            return _Git(None, d, common, None, BARE)
        if refused is not None and os.path.isfile(os.path.join(d, "HEAD")):
            named = _foreign_commondir(d)
            if named is not None:
                refused.append((d, named, _not_registered(d, named)))
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


def _at_dotgit(
    directory: str,
    dotgit: str,
    st: os.stat_result,
    refused: list[tuple[str, str, str]] | None = None,
) -> _Git | None:
    if stat.S_ISDIR(st.st_mode):
        if not os.path.isfile(os.path.join(dotgit, "HEAD")):
            return None
        return _Git(dotgit, dotgit, dotgit, directory, PRIMARY)
    if not stat.S_ISREG(st.st_mode):
        return None
    target = _gitfile_target(dotgit)
    if target is None or not os.path.isfile(os.path.join(target, "HEAD")):
        return None
    why = gitfile_refusal(directory, dotgit, target)
    if why is not None:
        # Not followed (ENG-158): read as a `.git` file that names nothing,
        # so no other checkout's git directory keys this folder.
        if refused is not None:
            refused.append((dotgit, target, why))
        return None
    common, linked = _common_of(target)
    # A ``.git`` file names a linked worktree's git directory (it has a
    # ``commondir``) or a git directory kept elsewhere (``--separate-git-
    # dir``, a submodule's): the latter is the primary checkout of its
    # repository, its git directory elsewhere.
    return _Git(dotgit, target, common, directory, LINKED if linked else PRIMARY)


#: The security audit's finding id for the ``.git`` files above the cwd.
GITFILE_FINDING = "membership.gitfile"


def gitfile_posture(cwd: str | None) -> dict[str, Any]:
    """The security audit's finding on the ``.git`` files at `cwd` (the
    working directory when None) and above (ENG-158), as the keyword
    arguments of `posture.Finding`: ``warn`` naming each ``.git`` file
    git's discovery walk passes there that membership does not follow
    (`gitfile_refusal`; git itself still follows it, so a git command
    there acts on the repository it names), and each folder holding
    ``HEAD`` and a ``commondir`` naming a repository it is not registered
    under, with a check by hand as its fix (`_gitfile_fix`); ``info``
    where the repository found is a ``.git`` file naming a git directory
    with no pointer back (``--separate-git-dir``, design I22), which
    membership follows as git does, so a planted one naming a known path
    is followed too; ``ok`` otherwise; ``skip`` with no working
    directory. Reads only."""
    try:
        start = os.path.realpath(cwd if cwd is not None else os.getcwd())
    except (OSError, ValueError):
        return {
            "id": GITFILE_FINDING,
            "status": "skip",
            "summary": "no working directory to walk up from",
        }
    refused: list[tuple[str, str, str]] = []
    try:
        found = _discover(start, refused)
    except (OSError, ValueError):
        found = None
    if refused:
        first, _target, why = refused[0]
        return {
            "id": GITFILE_FINDING,
            "status": "warn",
            "summary": f"{first} names a git directory membership does not follow: {why}",
            "path": first,
            "fix": _gitfile_fix(first),
            "details": {
                "cwd": start,
                "refused": [
                    {"dotgit": d, "target": t, "reason": w} for d, t, w in refused
                ],
            },
        }
    dotgit = None if found is None else found.dotgit
    details: dict[str, Any] = {"cwd": start, "dotgit": dotgit}
    if (
        found is not None
        and dotgit not in (None, found.git_dir)
        and found.kind == PRIMARY
    ):
        config = os.path.join(found.git_dir, "config")
        if not _config_values(config, git_dir=found.git_dir).get(
            ("core", None, "worktree")
        ):
            return {
                "id": GITFILE_FINDING,
                "status": "info",
                "summary": (
                    f"{dotgit} names {found.git_dir}, a git directory kept apart "
                    "(--separate-git-dir) with no pointer back; membership follows "
                    "it as git does"
                ),
                "path": dotgit,
                "fix": (
                    "if this checkout was not made with --separate-git-dir, remove "
                    "the .git file: a planted one naming a git directory whose path "
                    "is known is followed too"
                ),
                "details": {**details, "target": found.git_dir},
            }
    return {
        "id": GITFILE_FINDING,
        "status": "ok",
        "summary": (
            "no .git file at or above the working directory names a git directory "
            "membership does not follow"
        ),
        "details": details,
    }


def _gitfile_fix(refused: str) -> str:
    """The warning's fix for the refused `.git` file or folder `refused`
    (review M D4): a check by hand of where it points and the removal of a
    plant. It suggests no command that rewrites a worktree's registration:
    one run in a copy of a live worktree moves the live worktree's
    registration to the copy, and membership then follows the copy."""
    plant = (
        "its .git file"
        if os.path.basename(refused) == ".git"
        else "the HEAD and commondir files in it"
    )
    return (
        "git still follows it, so a git command here acts on the repository it "
        "names. Check by hand where it points: a worktree's .git file names "
        "<common>/worktrees/<name>, whose gitdir file names that .git file back, "
        "and a submodule's names a git directory whose core.worktree names this "
        "folder; any other is not this folder's. If this folder came from an "
        f"archive, a copy or someone else, remove {plant}."
    )


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

# The git call a history read falls back to where the object reader is
# incomplete. Measured (ENG-160): a `git cat-file --batch-check` of 64
# ids took 12 ms here; the bound is for a git that hangs (a lock, a
# network disk), and the daemon's stop waits as long for one
# (`daemon.GRACEFUL_SHUTDOWN_SECONDS`,
# `_daemon_client.UNINTERRUPTED_STEP_SECONDS`; tests pin both).
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
