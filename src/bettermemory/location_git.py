"""Git and capture readers for `location` (design 3.1).

The vocabulary and bounds a capture shares with the model, git's
discovery from a directory, the remotes its config names, the `Capture`
model and its token codec, the HEAD, ref, stamp and manifest readers, and
the history reader (`RepositoryHistory`, `walk`). `location` keeps the
capture, its cache, the witness exchange and the resolution, and
re-exports every name here that a caller reads, so no import path changes.
The rule a ``.git`` entry is followed by (`gitfile_rule.judge`,
`gitfile_rule.gitfile_refusal`) and the config reader it reads through
(`gitconfig`) moved out of this module so `githead` reads the same rule
(ENG-193): `githead` cannot import this module, which imports it. The
bounded reads of recorded paths are `location_reads`'s, re-exported here.

These readers live apart from `location` to keep each module under the
package's 2,000-line bound (ENG-96); `location` imports this module,
never the reverse. The hook client loads it with `location`, so it
imports only the standard library and the readers `_caches`, `_git`,
`_location_probe`, `githead`, `gitconfig`, `gitobjects`, `location_reads`
and `project_file`, which import the standard library and each other
alone, and `gitfile_rule` and `gitsystem`, which read records through
`gitdir_pin` and their own, which write through `_fsutil` and read the
state directory through `_daemon_client` (both loaded on every hook
already); never `location`
itself, the model, the store or the config
(tests/test_membership_capture.py pins that set).
"""

from __future__ import annotations

import os
import re
import stat
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _caches, githead, gitobjects, gitsystem, project_file
from . import _git as _git_runner
from ._location_probe import INDETERMINATE, TOKEN_VERSION
from .gitconfig import Stamp, _config_values, _home, _truthy
from .gitfile_rule import (
    _common_of,
    _foreign_commondir,
    _git_reads,
    _is_git_directory,
    _not_registered,
    inside_reading,
    judge,
)

# The bounded reads of recorded paths (ENG-156) live in `location_reads`;
# `location` and its callers read these names from here.
from .location_reads import (
    NOT_READ as NOT_READ,
    UNANSWERED as UNANSWERED,
    bounded_read as bounded_read,
    bounded_reads as bounded_reads,
)

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
#: once per path while that read is still in flight, and once for each
#: `location_reads.READ_SLOTS` such paths a batch reads beside each other
#: (`location_reads.READ_HANDOFF`).
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
#: `membership_records.WALK_LIMIT`, so a client's walk covers exactly the
#: model's. Measured (ENG-160) on this repository's history of 2,011 commits
#: from a fresh reader: 512 commits in 55 ms, 128 in 14 ms, all 2,011 in 268
#: ms. A witness older than the newest 512 commits is not reached by the walk,
#: and the other witnesses decide. `reachable_among` (ENG-151) reads the
#: same 512 commits in all, breadth first from every tied witness at once,
#: and expands no commit committed before the oldest witness, less the
#: largest rise of a parent over a child it has seen, until every commit
#: above that is read (ENG-203), where ENG-151 spent the bound evenly
#: below each witness, the oldest's included. Measured from a fresh reader
#: on a 20,000-commit line built by fast-import, a minute apart: a tie of
#: 128 witnesses spanning 480 commits (one project's first 16 together, its
#: newest 48 two apart from 165 commits above them, the other's 64 three
#: apart above those) found 6,336 of the 8,128 relations and asked in
#: 9.0.0rc17, and all 8,128 in 38 ms, joining; a young upstream's 64 three
#: apart with the fork's first 16 just above and its newest 48 400 higher
#: found 4,288 by both and joined. A tie whose witnesses lie further apart
#: than the 512 commits read is not ordered by the traversal (on this
#: repository's first-parent line, 128 witnesses five apart: 0 relations in
#: rc17, 1 now); `RepositoryHistory.independent` asks git for it where the
#: store's machine reads the repository, and elsewhere the most specific
#: line is not decided and the ladder asks (review M G2).
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
#: one kind of question (held, walk, ancestry or, from 9.0.0rc19, git's
#: reading of tied witnesses, blobs, views, remint) and the answers
#: accumulate; measured over the exchanges the suite runs
#: (tests/test_hook.py, test_membership_remint, test_membership_security,
#: test_membership_resolution, test_location_reachable; ENG-160): 16
#: exchanges took 1 round in 4, 2 in 10 and 3 in 2. Eight bounds a prompt
#: at eight posts.
EXCHANGE_ROUNDS = 8
#: Parent edges `reachable_among` reads and spreads before it stops, beside
#: its `WALK_LIMIT` commits: an octopus merge holds as many parents as its
#: object holds lines (about 87,000 under `gitobjects`' 4 MiB object cap),
#: and a commit is spread again each time its set of descendants grows.
#: Measured (ENG-203, macOS) on this repository's history: the most an
#: honest traversal handled was 67,655 edges (512 witnesses, consecutive
#: commits, 72 ms), random merge DAGs of up to 512 witnesses at most 4,728.
#: A hostile store (31 witnesses at depths 1 to 31 above 16 octopi of
#: 50,000 parents) handled 3,200,244 edges in 306 ms without the bound and
#: 300,154 in 33 ms with it (one commit's parents past the bound, read whole):
#: the bound is about four times the most an honest traversal handled.
EDGE_LIMIT = 1 << 18
#: How long `RepositoryHistory.independent`'s ``git merge-base
#: --independent`` may take; past it the traversal decides. Measured
#: (ENG-203, macOS, git 2.54, packed, no commit-graph; idle and under a
#: load average of 7): 11 to 70 ms in every case tried, among them 128
#: witnesses on two unrelated 10,000-commit lines (57 to 70 ms, each walked
#: to its root) and 128 on a 20,000-commit line whose oldest 16 are its
#: root's (53 to 60 ms); git reads about 3 µs a commit, so a second covers
#: about 300,000. The default of `_git.run_git`, as `origin.commit_reachable`'s
#: ``merge-base --is-ancestor`` is bound.
INDEPENDENT_SECONDS = 1.0
#: The most witness commits `RepositoryHistory.independent` hands git on
#: one command line; more go to the traversal. Windows' CreateProcess takes
#: at most 32,767 characters: 256 SHA-256 ids and their spaces are 16,640,
#: 512 are 33,280. A tie of four projects at `membership_records
#: .WITNESS_BOUND` (64 each) fits.
INDEPENDENT_MAX = 256

_FULL_HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: What `RepositoryHistory.independent` sets on its call: no grafts file
#: (the null device stands for ``info/grafts``). No fetch is the runner's,
#: on every call (`_git.NO_FETCH`, ENG-218).
_UNGRAFTED = {"GIT_GRAFT_FILE": os.devnull}
_INDEPENDENT_ARGS = ("merge-base", "--independent")
#: git's ``timestamp_t``, a ``uintmax_t`` (git-compat-util.h): 64 bits on
#: every platform git builds for, and where ``strtoumax`` saturates.
_TIME_MAX = (1 << 64) - 1
_DIGITS = re.compile(rb"[0-9]*")
_HEX = re.compile(rb"[0-9a-fA-F]*")
_HEAD_LIMIT = 1 << 16
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

# The reader of one config file and its includes (`_config_values`) is
# `gitconfig`'s, imported above; the system and global files and the
# remotes are read here.


def _env_true(raw: str) -> bool:
    """A variable git reads with ``git_env_bool`` set to `raw`: a boolean
    as git spells one, else an integer, true when it is not 0. A value
    that is neither stops git; it reads as true here, so nothing is read
    that git would not read."""
    told = _truthy([raw])
    if told is not None:
        return told
    try:
        return int(raw.strip()) != 0
    except ValueError:
        return True


def _system_configs() -> list[str]:
    """The system config files git reads, in its order, as config.c finds
    them (ENG-207): none under GIT_CONFIG_NOSYSTEM set true; else the
    files the git on PATH reads of its own build (Apple's developer
    directory's, `gitsystem.Sources.own`), then the file GIT_CONFIG_SYSTEM
    names (none where it is empty, which git cannot open), else the one
    that git was built to read. Only a repository with a remote asks
    (`_rewrites`), and `gitsystem` asks git once for each PATH and keeps
    the answer in the state directory, so a capture spawns no git while
    the answer stands (design 8.9, E16)."""
    raw = os.environ.get("GIT_CONFIG_NOSYSTEM")
    if raw is not None and _env_true(raw):
        return []
    found = gitsystem.sources()
    named = os.environ.get("GIT_CONFIG_SYSTEM")
    system = found.system if named is None else named
    return [*found.own, *([system] if system else [])]


def _global_configs() -> list[str]:
    """The global config files git reads, in its order: the one
    GIT_CONFIG_GLOBAL names alone (none where it is empty, which git cannot
    open), else ``$XDG_CONFIG_HOME/git/config`` (``~/.config/git/config``)
    and then ``~/.gitconfig``."""
    named = os.environ.get("GIT_CONFIG_GLOBAL")
    if named is not None:
        return [named] if named else []
    home = _home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out = [os.path.join(xdg, "git", "config")] if xdg else []
    if home:
        if not xdg:
            out.append(os.path.join(home, ".config", "git", "config"))
        out.append(os.path.join(home, ".gitconfig"))
    return out


#: A config file as a read stamped it (`gitconfig.ConfigValues.files`).
ConfigFile = tuple[str, Stamp | None]


#: A remote URL as written, the URL git rewrites it to (None where no
#: rule matches it), and whether the rule that does is the machine's (the
#: system or the global config) rather than the repository's own.
Spelling = tuple[str, str | None, bool]


def _rewrites(common_dir: str, git_dir: str) -> tuple[list[Spelling], list[ConfigFile]]:
    """Each remote URL in the repository's config with the URL git
    rewrites it to, where a ``url.<base>.insteadOf`` prefix in the system,
    the global or the repository's config matches it (the longest prefix
    wins, the first read of equal ones, as in remote.c's ``alias_url``;
    ENG-160, the system's since ENG-207), else None, and whether that
    prefix is the machine's (`Spelling`); and every config file
    read for them, stamped, the files each includes with it, but for the
    repository's own config, which the capture's signature stamps
    already (`location.signature`). A repository with no remote reads its
    own config alone. The config a process's environment gives git
    (``GIT_CONFIG_COUNT`` and its keys, ``GIT_CONFIG_PARAMETERS`` from
    ``git -c``) is not read: it is the setting of one shell, not of the
    repository or the machine, and the hook client or the daemon that
    captures is not the shell that runs git."""
    values = _config_values(
        os.path.join(common_dir, "config"),
        git_dir=git_dir,
        stamped=True,
        stamp_self=False,
    )
    files = list(values.files or ())
    urls = [
        u
        for (section, _sub, key), found in values.items()
        if section == "remote" and key == "url"
        for u in found
    ]
    if not urls:
        return [], files
    rules: list[tuple[str, str, bool]] = []
    configs = [
        _config_values(p, git_dir=git_dir, stamped=True)
        for p in [*_system_configs(), *_global_configs()]
    ]
    for config in configs:
        files.extend(config.files or ())
    for config in [*configs, values]:
        machine = config is not values
        for (section, sub, key), found in config.items():
            if section == "url" and sub is not None and key == "insteadof":
                rules.extend((prefix, sub, machine) for prefix in found if prefix)
    out: list[Spelling] = []
    for url in urls:
        best = max(
            (r for r in rules if url.startswith(r[0])),
            key=lambda r: len(r[0]),
            default=None,
        )
        if best is None:
            out.append((url, None, False))
        else:
            out.append((url, best[1] + url[len(best[0]) :], best[2]))
    return out, files


def remote_urls(common_dir: str, git_dir: str) -> list[str]:
    """Every remote URL in the repository's config, as written and, where a
    ``url.<base>.insteadOf`` prefix in the system, the global or the
    repository's config matches it, as git rewrites it (`_rewrites`):
    ENG-160. Both are kept, so a checkout keyed on the alias before
    matches as it did, and a clone of the URL the alias stands for
    matches too."""
    spellings, _files = _rewrites(common_dir, git_dir)
    return [u for u, _r, _m in spellings] + [r for _u, r, _m in spellings if r]


@dataclass(frozen=True)
class RemoteKeys:
    """A repository's remotes as a capture keys them (`read_remotes`):
    every key, the alias keys among them, and the config files read for
    them (`_rewrites`)."""

    keys: tuple[str, ...]
    aliases: tuple[str, ...]
    files: tuple[ConfigFile, ...]


def read_remotes(
    common_dir: str, git_dir: str, key: Callable[[str], str | None]
) -> RemoteKeys:
    """The keys (`key` of each URL) of the repository's remotes, each as
    written and as git rewrites it (`remote_urls`). An alias key is a
    remote's key as written where an ``insteadOf`` prefix of the system or
    the global config rewrites it to another key, and no remote is fetched
    from under it: a spelling the machine's config gives a meaning, never
    the URL git fetches (ENG-207). A prefix of the repository's own config
    makes none: the checkout's own files set it, and an alias key may
    join two projects (`membership_alias`). The config files read for the
    remotes are kept with their stamps, so a capture kept against them is
    read again once one changes (`gitconfig.unchanged`; ENG-207)."""
    spellings, files = _rewrites(common_dir, git_dir)
    keys: set[str] = set()
    fetched: set[str] = set()
    aliased: set[str] = set()
    for url, rewritten, machine in spellings:
        written = key(url)
        as_fetched = written if rewritten is None else key(rewritten)
        keys.update(k for k in (written, as_fetched) if k)
        fetched.update([as_fetched] if as_fetched else [])
        if written and as_fetched and machine and as_fetched != written:
            aliased.add(written)
    return RemoteKeys(
        keys=tuple(sorted(keys)),
        aliases=tuple(sorted(aliased - fetched)),
        files=tuple(files),
    )


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


# The rule a `.git` entry is followed by (`gitfile_refusal`, `judge`) and the
# readers it rests on (`_common_of`, `_foreign_commondir`, `_is_git_directory`
# and git's own stop test, `_git_reads`) are `gitfile_rule`'s, imported above.


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
    start: str,
    refused: list[tuple[str, str, str]] | None = None,
    *,
    read_head: bool = True,
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
    (review M G5). A git directory the walk reaches whose config names a
    working tree membership does not root it at goes there too (the
    directory, itself, why; `gitfile_rule.inside_reading`, ENG-211), and
    the walk stops at it with no repository, as git's stops there with no
    work tree. With `read_head` False a ``HEAD`` is taken by its
    presence where git's test reads it (`gitfile_rule._valid_headref`):
    the caller reads the one where the walk stops and tests it, as
    `location.signature` does."""
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
            # The entry itself: a link is read as a pointer (`_at_dotgit`).
            st = os.lstat(dotgit)
        except (OSError, ValueError):
            st = None
        if st is not None:
            found = _at_dotgit(d, dotgit, st, refused, read_head=read_head)
            if found is not None:
                return found
        # Git's own test first, as at a `.git` entry (`judge`), so this walk
        # stops where git's, and origin's, does (ENG-193).
        if _git_reads(d, fetch=False, read=read_head) and _is_git_directory(d):
            why, root = inside_reading(d)
            if why is not None:
                # Its config names a working tree membership does not root
                # it at, or is not read as git reads it (ENG-211): git's
                # walk stops here too, so membership joins no project.
                if refused is not None:
                    refused.append((d, d, why))
                return None
            common, linked = _common_of(d)
            if root is not None:
                return _Git(d, d, common, root, LINKED if linked else PRIMARY)
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
    *,
    read_head: bool = True,
) -> _Git | None:
    """The repository the ``.git`` entry `dotgit` in `directory` gives,
    `st` its ``lstat``, or None where it gives none or is not followed
    (each refusal to `refused`, as `_discover` says), as the rule
    `gitfile_rule.judge` reads it, which `githead` and the origin family
    read too (ENG-193). A provider's placeholder HEAD is not read (`fetch`
    False; design 3.1, D19), nor any HEAD with `read_head` False
    (`_discover`)."""
    seen = judge(directory, dotgit, st, fetch=False, read=read_head)
    if seen is None:
        return None
    if seen.why is not None:
        # Not followed (ENG-158): read as a `.git` file that names nothing,
        # so no other checkout's git directory keys this folder.
        if refused is not None:
            refused.append((dotgit, seen.git_dir, seen.why))
        return None
    if seen.own:
        return _Git(dotgit, dotgit, dotgit, directory, PRIMARY)
    target = seen.git_dir
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
    daemon tell whether it reads the same object at ``cwd``.
    ``alias_keys`` are the remote keys spelled with an ``insteadOf``
    prefix of the system or the global config (`read_remotes`, ENG-207).
    ``kept_apart`` is set where the repository was reached through a
    ``.git`` entry naming a git directory kept apart (`kept_apart`),
    ``tree_recorded`` where this capture made the first record of its
    working tree (`record_working_tree`, ENG-206), and ``config_files``
    holds the config files read for the remotes besides the repository's
    own, stamped, which a kept capture is checked against (ENG-207);
    none of these three is part of the token, and the last is no part of
    what a capture equals."""

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
    alias_keys: tuple[str, ...] = ()
    kept_apart: bool = False
    tree_recorded: bool = False
    config_files: tuple[ConfigFile, ...] = field(default=(), compare=False, repr=False)

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
            "aliases": list(self.alias_keys),
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
                alias_keys=tuple(_text(x) for x in _bounded(raw.get("aliases"), 256)),
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
    """The history reader of a capture (`membership_records.HistoryReader`,
    design 3.1): objects read from the repository's files through
    `gitobjects.ObjectReader`, made on first use; ``git cat-file`` only
    where the reader reports itself incomplete (a pack index it could not
    parse, an evicted file, an object format it does not know). Without
    git on PATH an incomplete reader's answer stands."""

    def __init__(self, common_dir: str) -> None:
        self._common = common_dir
        self._reader: gitobjects.ObjectReader | None = None
        self._parents: dict[str, tuple[str, ...]] = {}
        self._committed: dict[str, int] = {}

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
        when = 0
        if body is not None:
            hexsz = 64 if len(sha) == 64 else 40
            header = _parent_lines(body, hexsz)
            found = () if header is None else header[0]
            when = _committed_at(body, hexsz)
        self._parents[sha] = found
        self._committed[sha] = when
        return found

    def committed(self, commit: str) -> int:
        """The commit's committer time (`_committed_at`), read from the
        object its parents are read from, so ordering a commit costs no
        read beyond its parents'."""
        sha = commit.lower()
        if sha not in self._committed:
            self.parents(sha)
        return self._committed[sha]

    def reachable_among(self, commits: Sequence[str]) -> Reach:
        """`reachable_among` over this repository's parent lines (ENG-151),
        its committer times read with its parents (ENG-203): one bounded
        traversal, a shallow boundary ending it as git's own walk ends there."""
        return reachable_among(self, commits, committed=self.committed)

    def independent(self, commits: Sequence[str]) -> frozenset[str] | None:
        """The commits of `commits` (full ids) no other of them reaches, as
        ``git merge-base --independent`` lists them (ENG-203): the
        witnesses the most-specific rule keeps
        (`membership.Membership._most_specific`), read with no bound on how
        far apart they lie. Git is held to the parent lines the objects
        hold, as this reader reads them (`_parent_lines`): no replace refs
        (``--no-replace-objects``), no commit-graph (``core.commitGraph``
        off: a graph file can name parents no object holds), no grafts
        (``GIT_GRAFT_FILE`` the null device), and, as on every call the
        runner makes, no fetch (`_git.NO_FETCH`, ENG-218); a shallow
        boundary ends its walk as it ends this reader's. The ids are handed
        to git sorted. None, and the bounded traversal decides, where git
        cannot run or does not answer within `INDEPENDENT_SECONDS` (the
        runner's WARNING, as for any call: a second is some 14 times the
        slowest run measured, so a stall is worth a line), exits non-zero
        (an object it cannot read) or names no commit or one it was not
        given, and for more than `INDEPENDENT_MAX` commits. It spawns a
        process, so the model reads it before the resolution's write
        transaction, never inside it (`membership_resolve.resolve`). A hook
        client the daemon cannot read for runs it when asked, in its own
        namespace (`location_witness.independent_answer`, ENG-216)."""
        given = sorted(
            {c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower()) is not None}
        )
        if len(given) < 2 or len(given) > INDEPENDENT_MAX:
            return frozenset(given) if len(given) < 2 else None
        args = ["--no-replace-objects", "-c", "core.commitGraph=false"]
        args += [*_INDEPENDENT_ARGS, *given]
        done = _git_runner.run_git(
            args, git_dir=self._common, timeout=INDEPENDENT_SECONDS, env=_UNGRAFTED
        )
        if done is None:
            return None
        if done.returncode != 0:
            _git_runner.log_nonzero_exit(_INDEPENDENT_ARGS, done, self._common)
            return None
        top = frozenset(line.strip().lower() for line in done.stdout.splitlines())
        top -= {""}
        return top if top and top <= set(given) else None

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


class Reach(dict[str, frozenset[str]]):
    """`reachable_among`'s answer: {commit: the given commits it is
    reachable from}, with `bounded`, whether a bound stopped the traversal
    while a commit that may hold a relation was still to read or to spread
    (ENG-203), and `handled`, the parent edges it read and spread
    (`EDGE_LIMIT`). Where `bounded` is false and committer times never fall
    from parent to child, it holds every relation the unbounded walk
    finds."""

    def __init__(
        self,
        relations: Mapping[str, frozenset[str]],
        *,
        bounded: bool,
        handled: int = 0,
    ) -> None:
        super().__init__(relations)
        self.bounded = bounded
        self.handled = handled


def reachable_among(
    history: Any,
    commits: Sequence[str],
    *,
    limit: int = WALK_LIMIT,
    edges: int = EDGE_LIMIT,
    committed: Callable[[str], int] | None = None,
) -> Reach:
    """{commit: the given commits it is reachable from} over the full ids
    of `commits`, in one traversal from all of them at once (ENG-151):
    each commit's set of the given commits it descends from spreads to its
    parents, breadth first, so the bound is spent below every witness
    alike. A commit committed (`committed`; without it every commit reads
    time 0) before a floor, the oldest given commit's time, is read and held
    back, and expanded in a second pass once every commit above the floor
    is (ENG-203): where committer times never fall from parent to child, no
    path between two given commits passes below the oldest, so a commit
    held back holds no relation among them and the bound is spent where
    relations are. A clock is seldom that true: where a parent is seen
    committed after a child that reached it, the floor drops by the largest
    such rise seen so far and what it held above the new floor is released,
    so a path through commits skewed by no more than that is read in the
    first pass as breadth first reads it. A relation through a commit still
    held back is found only if the bound lasts to it. A commit is spread
    again only when its set has grown since it last spread.

    It reads the parents of at most `limit` commits, each once: the given
    commits first, sorted by id (where more are given than `limit`, the
    first `limit` of them); a commit held back counts, read for its time.
    It handles at most `edges` parent edges read and spread (`EDGE_LIMIT`),
    and one commit's parents past them. `Reach.bounded` says whether a
    bound stopped the first pass before every commit it reached was read
    and spread, or stopped the second where a rise was seen: with times
    that never fall from parent to child the second holds no relation.

    A relation is one it read along parent edges, never one inferred, so
    every relation it reports is the unbounded walk's, and one past a
    bound reads as none. `membership.Membership._specific` reads none as
    apart, which keeps more projects, never fewer, on a history without
    cycles (any whose objects hash to their ids): a forged object store
    can close a cycle the bound reads one way round (ENG-203)."""
    given = sorted(
        {c.lower() for c in commits if _FULL_HASH.fullmatch(c.lower()) is not None}
    )
    bit = {c: 1 << i for i, c in enumerate(given)}
    reach: dict[str, int] = dict(bit)
    spread: dict[str, int] = {}
    lines: dict[str, tuple[str, ...]] = {}
    times: dict[str, int] = {}
    # The oldest committer time of a child that reached each commit.
    child: dict[str, int] = {}
    held: dict[str, None] = {}
    todo: deque[str] = deque(given)
    queued = set(given)
    handled = rise = 0

    def read(sha: str) -> bool:
        nonlocal handled
        if sha not in lines:
            if len(lines) >= limit or handled >= edges:
                return False
            lines[sha] = tuple(p.lower() for p in history.parents(sha))
            times[sha] = 0 if committed is None else committed(sha)
            handled += len(lines[sha])
        return True

    def lower(by: int) -> None:
        # A parent committed `by` after a child that reached it: lower the
        # floor that much and release what it held above the new floor.
        nonlocal rise
        rise = by
        for sha in [s for s in held if times[s] >= floor - rise]:
            del held[sha]
            if sha not in queued:
                queued.add(sha)
                todo.append(sha)

    def reached(parent: str, when: int) -> None:
        # A child committed at `when` reached `parent`; a parent read
        # already and committed later shows how far the clock rose.
        if when < child.get(parent, when + 1):
            child[parent] = when
            if times.get(parent, when) - when > rise:
                lower(times[parent] - when)

    floor = min((times[c] for c in given if read(c)), default=0)
    bounded = cut = deep = False
    while True:
        while todo:
            sha = todo.popleft()
            queued.discard(sha)
            if not read(sha):
                bounded, cut = bounded or not deep, True
                continue
            when = times[sha]
            if when - child.get(sha, when) > rise:
                lower(when - child[sha])
            if not deep and when < floor - rise:
                held[sha] = None
                for p in lines[sha]:
                    reached(p, when)
                continue
            mask = reach[sha]
            parents = lines[sha]
            if spread.get(sha) == mask:
                continue
            if handled + len(parents) > edges:
                bounded, cut = bounded or not deep, True
                continue
            handled += len(parents)
            spread[sha] = mask
            for p in parents:
                reached(p, when)
                old = reach.get(p, 0)
                if old | mask != old:
                    reach[p] = old | mask
                    if p not in queued:
                        queued.add(p)
                        todo.append(p)
        if deep or not held:
            break
        deep = True
        todo.extend(held)
        queued.update(held)
        held.clear()
    return Reach(
        {c: frozenset(o for o in given if o != c and reach[c] & bit[o]) for c in given},
        bounded=bounded or (cut and rise > 0),
        handled=handled,
    )


def _committed_at(body: bytes, hexsz: int) -> int:
    """The committer time of the commit whose object body is `body` (its
    ids `hexsz` hex digits), as git's ``parse_commit_buffer`` and
    ``parse_commit_date`` (commit.c, git 2.54) read it: past the ``tree``
    line and the ``parent`` lines, each of fixed length, the next line must
    start ``author`` and the one after it ``committer`` and end in a
    newline; after that line's last ``>`` and any spaces, tabs and carriage
    returns (git's ``isspace``), a digit or ``-`` starts the number, which
    ``strtoumax`` reads into git's ``timestamp_t`` (`_TIME_MAX`): leading
    zeros skipped, a ``-`` wrapping it modulo 2**64, a number past 2**64 - 1
    saturating there. 0 where git reads none, and where git refuses the
    object (a short or non-hex ``tree`` or ``parent`` line). The same as
    ``git rev-list --no-walk --timestamp`` (git 2.54) on all 46 commits of
    tests/test_location_reachable.py, written by ``hash-object
    --literally`` (ENG-203). The time only orders `reachable_among`'s
    traversal, so no value read here makes one commit reachable from
    another."""
    header = _parent_lines(body, hexsz)
    if header is None:
        return 0
    at = header[1]
    if not body.startswith(b"author", at):
        return 0
    at = body.find(b"\n", at) + 1
    if at == 0 or not body.startswith(b"committer", at):
        return 0
    eol = body.find(b"\n", at)
    mark = body.rfind(b">", at, eol)
    if eol < 0 or mark < 0:
        return 0
    at = mark + 1
    while at < eol and body[at] in b" \t\r":
        at += 1
    if at == eol or body[at] not in b"0123456789-":
        return 0
    minus = body[at] == 45
    digits = _DIGITS.match(body, at + minus, eol)
    run = b"" if digits is None else digits.group().lstrip(b"0")
    if len(run) > 20:
        return _TIME_MAX
    value = int(run or b"0")
    if value > _TIME_MAX:
        return _TIME_MAX
    return -value % (1 << 64) if minus else value


def _parent_lines(body: bytes, hexsz: int) -> tuple[tuple[str, ...], int] | None:
    """The parents of the commit whose object body is `body` (its ids
    `hexsz` hex digits), and where its header goes on past them, as git's
    ``parse_commit_buffer`` (commit.c, git 2.54) reads them: the ``tree``
    line, then each ``parent`` line of fixed length directly after it; a
    ``parent`` line anywhere else is no parent. None where git refuses the
    object (a short or non-hex ``tree`` or ``parent`` line), so the
    traversal and ``git merge-base`` read one parent list (ENG-203)."""
    at = hexsz + 6
    if not (body.startswith(b"tree ") and len(body) > at and body[at - 1] == 10):
        return None
    if not _hex_at(body, 5, hexsz):
        return None
    step = hexsz + 8
    parents: list[str] = []
    while at + step - 1 < len(body) and body.startswith(b"parent ", at):
        if len(body) <= at + step or not _hex_at(body, at + 7, hexsz):
            return None
        if body[at + step - 1] != 10:
            return None
        parents.append(body[at + 7 : at + step - 1].decode("ascii").lower())
        at += step
    return tuple(parents), at


def _hex_at(body: bytes, at: int, hexsz: int) -> bool:
    """Whether `body` holds `hexsz` hex digits of either case at `at`, as
    git's ``get_oid_hex_algop`` reads an object id there."""
    found = _HEX.match(body, at, at + hexsz)
    return found is not None and found.end() == at + hexsz


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
