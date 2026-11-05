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
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _caches, githead, gitobjects, gitsystem, project_file
from . import _git as _git_runner
from ._location_probe import INDETERMINATE, TOKEN_VERSION
from .gitconfig import (
    _LEGACY_FOLDERS as _LEGACY_FOLDERS,
)
from .gitconfig import (
    UNSETTLED,
    ConfigFile,
    ConfigValues,
    IncludeContext,
    _default_branch,
    _home,
    _Legacy,
    _legacy_names,
    _legacy_remote,
    _native,
    _remote_url_names,
    config_tree,
    file_stamp,
    git_bool,
    subsection_bytes,
    unchanged,
)
from .gitconfig import (
    legacy_remote_stamps as legacy_remote_stamps,
)
from .gitfile_rule import (
    FormatUnknown,
    _common_of,
    _foreign_commondir,
    _git_reads,
    _is_git_directory,
    _not_registered,
    _Unsettled,
    inside_reading,
    judge,
    reads_worktree_config,
    tree_format,
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

# The reader of one config file and its includes (`config_tree`, parsed as
# git's own parser parses it) is `gitconfig`'s, imported above; the system
# and global files and the remotes are read here.


def _env_true(raw: str) -> bool:
    """A variable git reads with ``git_env_bool`` set to `raw`, as git
    reads a boolean (`gitconfig.git_bool`). A value that is none stops
    git; it reads as true here, so nothing is read that git would not
    read."""
    told = git_bool(raw.encode("utf-8", "surrogateescape"))
    return True if told is None else told


#: The answer of the git on PATH about what it reads (`gitsystem.sources`),
#: asked of `gitsystem` once a capture and kept for it.
SourcesOf = Callable[[], gitsystem.Sources]


def _sources_once() -> SourcesOf:
    """`gitsystem.sources`, asked once however often the answer is read:
    each ask states the git binary."""
    held: list[gitsystem.Sources] = []

    def sources() -> gitsystem.Sources:
        if not held:
            held.append(gitsystem.sources())
        return held[0]

    return sources


def _system_configs(
    *, stamp: bool = True, sources: SourcesOf | None = None
) -> list[str]:
    """The system config files git reads, in its order, as config.c finds
    them (ENG-207): none under GIT_CONFIG_NOSYSTEM set true; else the
    files the git on PATH reads of its own build (Apple's developer
    directory's, `gitsystem.Sources.own`), then the file GIT_CONFIG_SYSTEM
    names (none where it is empty, which git cannot open), else the one
    that git was built to read. Only a repository with a remote asks
    (`_rewrites`), and `gitsystem` asks git once for each PATH and keeps
    the answer in the state directory, so a capture spawns no git while
    the answer stands (design 8.9, E16); `stamp` as `gitsystem.sources`,
    and `sources` the capture's own ask of it where it has one."""
    raw = os.environ.get("GIT_CONFIG_NOSYSTEM")
    if raw is not None and _env_true(raw):
        return []
    found = sources() if sources is not None else gitsystem.sources(stamp=stamp)
    named = os.environ.get("GIT_CONFIG_SYSTEM")
    system = found.system if named is None else named
    return [*found.own, *([system] if system else [])]


def _global_configs() -> list[str]:
    """The global config files git reads, in its order: the one
    GIT_CONFIG_GLOBAL names alone (none where it is empty, which git cannot
    open), else ``$XDG_CONFIG_HOME/git/config`` (``$HOME/.config/git/config``
    where XDG_CONFIG_HOME is unset or empty) and then ``$HOME/.gitconfig``;
    none of HOME's where HOME is unset, as git reads none
    (`gitconfig._home`)."""
    named = os.environ.get("GIT_CONFIG_GLOBAL")
    if named is not None:
        return [named] if named else []
    home = _home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out = [os.path.join(xdg, "git", "config")] if xdg else []
    if home is not None:
        if not xdg:
            out.append(home + "/.config/git/config")
        out.append(home + "/.gitconfig")
    return out


def _machine_configs(
    *, stamp: bool = True, sources: SourcesOf | None = None
) -> tuple[str, ...]:
    """The machine's config files git reads, in its order: the system's
    (`_system_configs`), then the global ones (`_global_configs`), each
    spelled with the platform's separator (`gitconfig._native`)."""
    return tuple(
        _native(p)
        for p in (*_system_configs(stamp=stamp, sources=sources), *_global_configs())
    )


def _repository_paths(common_dir: str, git_dir: str) -> list[str] | str:
    """The repository's own config files git reads, in its order: the
    common config, then, where git reads it
    (`gitfile_rule.reads_worktree_config`: the common config file itself
    sets ``extensions.worktreeConfig`` true, as git's read of a format
    takes it, no include followed), the git directory's
    ``config.worktree`` (ENG-207's review: a remote or an insteadOf rule
    set there is the repository's too). Why not, where git dies on the
    common config's format and reads no variable of the repository (the
    reviewer's R14)."""
    worktree_config = reads_worktree_config(common_dir)
    if worktree_config is None:
        return f"git dies reading the format of {os.path.join(common_dir, 'config')}"
    paths = [os.path.join(common_dir, "config")]
    if worktree_config:
        paths.append(os.path.join(git_dir, "config.worktree"))
    return paths


def _read_configs(
    paths: Sequence[str],
    git_dir: str,
    context: IncludeContext,
    files: list[ConfigFile],
    *,
    stamp_first: bool = True,
    gentle: Collection[str] = (),
) -> list[ConfigValues]:
    """Each of the config files `paths` read with its includes, as git
    reads it under `context` (`gitconfig.IncludeContext`), the files each
    read stamped into `files`; the first file itself left out where
    `stamp_first` is off (the common config, which the capture's
    signature stamps, `location.signature`). A file of `gentle` (the
    global ones) is skipped where it cannot be read for want of
    permission, as git skips it."""
    out: list[ConfigValues] = []
    for i, path in enumerate(paths):
        read = config_tree(
            path,
            git_dir=git_dir,
            stamped=True,
            stamp_self=stamp_first or i > 0,
            gentle=path in gentle,
            context=context,
        )
        files.extend(read.files or ())
        out.append(read)
    return out


def _failed(reads: Sequence[ConfigValues]) -> str | None:
    """Why the config `reads` read does not settle what git reads: git
    dies reading it, or reads more than was read (`ConfigValues.dies`,
    `ConfigValues.complete`); None where it does."""
    for read in reads:
        if read.dies is not None:
            return f"git dies reading its config: {read.dies}"
        if not read.complete:
            return "a config file is not read whole here"
    return None


def _head_branch(
    common_dir: str, git_dir: str, files: list[ConfigFile]
) -> bytes | None:
    """The branch an ``onbranch:`` include is matched against, read from
    HEAD the way the capture reads it (githead's reader of
    ``refs_resolve_ref_unsafe`` over the files backend): the name under
    ``refs/heads/`` of the ref HEAD's chain ends at, born or unborn; None
    where HEAD is detached or ends at another ref, where git's condition
    never holds. Where the files do not settle it (a reftable repository,
    a shape githead declines, a chain past git's depth) git is asked
    (`_asked_branch`). HEAD and each loose ref file the chain reads go
    into `files`, stamped before they are read, so a branch switch that
    leaves HEAD's bytes as they were (a symbolic ref along the chain
    moved) is seen too."""
    gd = githead.GitDir(Path(git_dir), Path(common_dir), Path(git_dir))
    head = os.path.join(git_dir, "HEAD")
    files.append((head, file_stamp(head)))
    try:
        content = githead.head_bytes(gd)
        if content is None:
            return _asked_branch(common_dir, git_dir, files)
        name, _sha = githead._parse(content)
        reads = 1
        while name is not None:
            if reads == githead._MAX_READS:
                return _asked_branch(common_dir, git_dir, files)
            reads += 1
            base = git_dir if name.startswith(githead._PER_WORKTREE) else common_dir
            loose = os.path.join(base, *os.fsdecode(name).split("/"))
            files.append((loose, file_stamp(loose)))
            target, _sha = githead._lookup(gd, name)
            if target is None:
                break
            name = target
    except (_Unsettled, OSError, ValueError):
        return _asked_branch(common_dir, git_dir, files)
    if name is None or not name.startswith(b"refs/heads/"):
        return None
    return name[len(b"refs/heads/") :]


def _asked_branch(
    common_dir: str, git_dir: str, files: list[ConfigFile]
) -> bytes | None:
    """The branch HEAD names, as git answers it (``git symbolic-ref -q
    HEAD``, which follows HEAD's chain as ``include_by_branch`` does),
    where the files do not settle it: a reftable repository's HEAD lives
    in its tables. One git, made only for a capture whose config has an
    ``onbranch:`` include (`gitconfig.IncludeContext.branch`), so a
    steady prompt makes none (tests/test_membership_cost.py). The tables'
    lists and packed-refs go into `files`, stamped before git reads them
    (git rewrites a list whole at each ref update), so a branch switch is
    seen. None where HEAD is detached or names no branch;
    `gitconfig.UNSETTLED` where git does not answer within
    `FETCH_BUDGET`."""
    lists = dict.fromkeys(
        os.path.join(folder, "reftable", "tables.list")
        for folder in (git_dir, common_dir)
    )
    for path in (*lists, os.path.join(common_dir, "packed-refs")):
        files.append((path, file_stamp(path)))
    done = _git_runner.run_git(
        _SYMBOLIC_HEAD, git_dir=git_dir, timeout=FETCH_BUDGET, text=False
    )
    if done is None or done.returncode not in (0, 1):
        return UNSETTLED
    name = done.stdout.rstrip(b"\n") if done.returncode == 0 else b""
    if not name.startswith(b"refs/heads/"):
        return None
    return name[len(b"refs/heads/") :]


#: git's question for the branch HEAD names (`_asked_branch`).
_SYMBOLIC_HEAD = ("symbolic-ref", "-q", "HEAD")


class _Unknown(Exception):
    """What the git on PATH does is not known (`gitsystem.Sources`), and
    what it reads turns on it: the capture declines."""


#: A remote URL as written, the URL git rewrites it to (None where no
#: rule matches it), and whether the rule that does is the machine's (the
#: system or the global config) rather than the repository's own.
Spelling = tuple[str, str | None, bool]


@dataclass(frozen=True)
class _Remotes:
    """What `_rewrites` reads of a repository's remotes: each URL keyed
    (`Spelling`), the config files read for them, stamped, the machine's
    config files named, and why the files do not settle what git reads,
    where they do not (`declined`)."""

    spellings: list[Spelling]
    files: list[ConfigFile]
    named: tuple[str, ...] = ()
    declined: str | None = None


def _url_text(raw: bytes) -> str:
    """A URL as a capture keys it: its bytes as UTF-8, a byte that is none
    replaced."""
    return raw.decode("utf-8", "replace")


def _rewrites(common_dir: str, git_dir: str) -> _Remotes:
    """Each remote URL of the repository with the URL git rewrites it to,
    where a ``url.<base>.insteadOf`` prefix in the system, the global or
    the repository's config matches it, else None, and whether that prefix
    is the machine's (`Spelling`); every config file read for them,
    stamped, the files each includes with it, but for the repository's
    common config, which the capture's signature stamps already
    (`location.signature`); and the machine's config files read
    (`_machine_configs`). Read as git 2.54 reads them, checked against
    ``git remote``, ``git remote get-url`` and ``git ls-remote --get-url``
    (tests/test_location_git_include_conditions.py,
    tests/test_location_git_parse.py):

    - The config is read in git's order, the machine's files and then the
      repository's (`_repository_paths`), each parsed as git's parser
      parses it (`gitconfig.config_tree`). Its includes are followed,
      under ``onbranch:`` against the branch HEAD names (`_head_branch`)
      and under ``hasconfig:remote.*.url:`` against every
      ``remote.<name>.url`` of that config, read as git collects them
      first: every includeIf that holds there is forbidden to set one,
      and one that does has git refuse the configuration.
    - A remote's URLs are its ``remote.<name>.url`` values in that order,
      an empty one emptying the list where this git does
      (`gitsystem.Sources.resets`, remote.c's ``add_url``); a remote with
      none, the machine's included, takes them from its legacy files
      (`_legacy_remote`) where this git reads them
      (`gitsystem.Sources.legacy`), and one whose legacy files git dies
      on is not keyed. A remote whose name starts with ``/`` is none. The
      capture keys those the repository's own config or files give, not
      the machine's: a remote the global config sets is every
      repository's.
    - A URL is rewritten as remote.c's ``alias_url`` rewrites it: the
      longest matching prefix wins, an empty one matching every URL, and
      of equal ones the first under the base git met first.

    `declined` says why the files do not settle what git reads: git's
    reader of remotes dies reading the config (a line its parser refuses,
    an include it cannot read or follow, a remote variable it refuses,
    `gitconfig.ConfigValues.dies`; a value only another reader refuses,
    as ``git rev-parse`` a ``core.excludesFile`` with no value, is read
    past, as ``git remote`` reads past it), or reads more than is read here; git
    refuses the configuration (a remote URL in a file a hasconfig
    collection forbids); the remote URLs a ``hasconfig:`` condition is
    matched against are not settled; an include under a condition not
    settled here (git does not answer for HEAD within `FETCH_BUDGET`) sets
    a remote URL or an insteadOf rule whose prefix matches a URL keyed,
    or is one git may die on (a rule that cannot touch a URL keyed is not
    one); what this git does is not known where it matters (`_Unknown`);
    or a legacy file is not read. The capture declines there rather than
    read another answer. A repository with no remote reads its own config
    and folders alone, and names none of the machine's: git names no
    remote there either way. The config a process's environment gives git
    (``GIT_CONFIG_COUNT`` and its keys, ``GIT_CONFIG_PARAMETERS`` from
    ``git -c``) is not read: it is the setting of one shell, not of the
    repository or the machine, and the hook client or the daemon that
    captures is not the shell that runs git."""
    paths = _repository_paths(common_dir, git_dir)
    if isinstance(paths, str):
        return _Remotes([], [], (), paths)
    files: list[ConfigFile] = []
    sources = _sources_once()
    context = IncludeContext(
        lambda: _head_branch(common_dir, git_dir, files),
        prefix=lambda: sources().prefix,
    )
    own = _read_configs(paths, git_dir, context, files, stamp_first=False)
    why = _failed(own)
    if why is not None:
        return _Remotes([], files, (), why)
    legacy = _legacy_names(common_dir, files)
    if isinstance(legacy, str):
        return _Remotes([], files, (), legacy)
    if not legacy and not any(
        _remote_url_names(read) or _remote_url_names(read.undecided) for read in own
    ):
        return _Remotes([], files)
    named = _machine_configs(sources=sources)
    gentle = frozenset(_native(p) for p in _global_configs())
    machine = _read_configs(named, git_dir, context, files, gentle=gentle)
    why = _failed(machine)
    if why is None and any(read.asks_remote_urls for read in (*machine, *own)):
        collect = context.collect()
        collected = [
            *_read_configs(named, git_dir, collect, files, gentle=gentle),
            *_read_configs(paths, git_dir, collect, files, stamp_first=False),
        ]
        why = _failed(collected)
        if why is None and any(read.forbidden for read in collected):
            why = "git refuses a remote URL a file an includeIf reads sets"
        if why is None and any(_remote_url_names(read.undecided) for read in collected):
            why = "the remote URLs a hasconfig include is matched against are unsettled"
        if why is None:
            settled = context.with_urls(
                frozenset(
                    url
                    for read in collected
                    for name in _remote_url_names(read)
                    for url in read[name]
                    if url is not None
                )
            )
            machine = _read_configs(named, git_dir, settled, files, gentle=gentle)
            own = _read_configs(paths, git_dir, settled, files, stamp_first=False)
            why = _failed([*machine, *own])
    if why is not None:
        return _Remotes([], files, named, why)
    try:
        spellings = _spellings(
            [(r, False) for r in machine] + [(r, True) for r in own],
            legacy,
            lambda name, against: _legacy_remote(common_dir, name, files, against),
            sources,
        )
    except _Unknown as exc:
        return _Remotes([], files, named, str(exc))
    if isinstance(spellings, str):
        return _Remotes([], files, named, spellings)
    return _Remotes(spellings, files, named)


#: Each insteadOf base, in the order git met it, with its prefixes, each
#: with whether the machine's config gives it.
_Rewrites = dict[str, list[tuple[bytes, bool]]]


def _spellings(
    reads: Sequence[tuple[ConfigValues, bool]],
    legacy: Sequence[str],
    legacy_remote: Callable[[str, _Legacy], list[bytes] | str | None],
    sources: SourcesOf,
) -> list[Spelling] | str:
    """`_rewrites`' keyed URLs (`Spelling`) of the config `reads` read,
    each with whether it is the repository's own, and of the legacy
    remotes `legacy` names (`legacy_remote`), as `_rewrites` says; why
    the capture declines where an undecided include could change them,
    or a legacy file is not read. `_Unknown` where what this git does is
    not known (`sources`) and matters."""

    def resets() -> bool:
        flag = sources().resets
        if flag is None:
            raise _Unknown("whether this git empties a remote's URLs at an empty one")
        return flag

    remotes: dict[str, list[tuple[bytes, bool]]] = {}
    rewrites: _Rewrites = {}
    for read, mine in reads:
        for (section, sub, key), found in read.items():
            if sub is None:
                continue
            if section == "remote" and key == "url" and not sub.startswith("/"):
                entries = remotes.setdefault(sub, [])
                for value in found:
                    if value:
                        entries.append((value, mine))
                    elif resets():
                        entries.clear()
                    else:
                        entries.append((b"", mine))
            elif section == "url" and key == "insteadof":
                rewrites.setdefault(sub, []).extend(
                    (p, not mine) for p in found if p is not None
                )
    pending = [name for name in legacy if not remotes.get(name)]
    if pending:
        reads_legacy = sources().legacy
        if reads_legacy is None:
            raise _Unknown("whether this git reads legacy remote files")
        if reads_legacy:
            against = _Legacy(resets, lambda: _default_branch([r for r, _m in reads]))
            for name in pending:
                got = legacy_remote(name, against)
                if isinstance(got, str):
                    return got
                if got is not None:
                    remotes[name] = [(url, True) for url in got]
    keyed = [url for found in remotes.values() for url, mine in found if mine and url]
    best = {url: _best_rule(url, rewrites) for url in keyed}
    why = _undecided_matters([r for r, _m in reads], best, bool(pending), rewrites)
    if why is not None:
        return why
    out: list[Spelling] = []
    for url, rule in best.items():
        if rule is None:
            out.append((_url_text(url), None, False))
        else:
            base, prefix, by_machine = rule
            rewritten = subsection_bytes(base) + url[len(prefix) :]
            out.append((_url_text(url), _url_text(rewritten), by_machine))
    return out


def _best_rule(url: bytes, rewrites: _Rewrites) -> tuple[str, bytes, bool] | None:
    """The insteadOf rule git rewrites `url` by (remote.c's ``alias_url``),
    as its base, its prefix and whether it is the machine's: the longest
    prefix of `rewrites` (each base's prefixes, the bases in the order git
    met them) that `url` starts with, of equal ones the first; or None."""
    best: tuple[str, bytes, bool] | None = None
    for base, prefixes in rewrites.items():
        for prefix, by_machine in prefixes:
            if url.startswith(prefix) and (best is None or len(best[1]) < len(prefix)):
                best = (base, prefix, by_machine)
    return best


def _undecided_matters(
    reads: Sequence[ConfigValues],
    best: dict[bytes, tuple[str, bytes, bool] | None],
    legacy: bool,
    rewrites: _Rewrites,
) -> str | None:
    """Why an include under a condition not settled here could change the
    URLs keyed (`best`, each with the rule that rewrites it, `_best_rule`
    over `rewrites`), or None where none could (ENG-207's round 6: one
    that cannot touch them leaves the capture as git reads it): it sets a
    remote URL, an insteadOf rule whose prefix matches a URL keyed, or,
    where a legacy ``branches`` file is read (`legacy`), the default
    branch; or git may die reading it (`gitconfig.ConfigValues.maybe_dies`).
    An insteadOf rule of any prefix counts too where it names a base
    another rule gives the prefix that rewrites a keyed URL under another
    base: git ranks bases in the order it first meets them, so where the
    include holds, it can meet that base first and take its prefix
    (ENG-207's round 7)."""
    keyed = list(best)
    for read in reads:
        if read.maybe_dies is not None:
            return f"an include under an unsettled condition may make git die: {read.maybe_dies}"
        for (section, sub, key), found in read.undecided.items():
            if (
                section == "remote"
                and key == "url"
                and sub is not None
                and not sub.startswith("/")
            ):
                return "an include sets a remote URL under an unsettled condition"
            if section == "url" and key == "insteadof" and sub is not None:
                if any(p is not None and u.startswith(p) for p in found for u in keyed):
                    return "an include sets an insteadOf rule matching a remote URL under an unsettled condition"
                tied = {p for p, _m in rewrites.get(sub, ())}
                if any(r and r[0] != sub and r[1] in tied for r in best.values()):
                    return "an include names the base of an insteadOf rule tied with the one rewriting a remote URL under an unsettled condition"
            if legacy and (section, sub, key) == ("init", None, "defaultbranch"):
                return "an include sets the default branch under an unsettled condition"
    return None


def remote_urls(common_dir: str, git_dir: str) -> list[str]:
    """Every remote URL of the repository, as written and, where a
    ``url.<base>.insteadOf`` prefix in the system, the global or the
    repository's config matches it, as git rewrites it (`_rewrites`):
    ENG-160. Both are kept, so a checkout keyed on the alias before
    matches as it did, and a clone of the URL the alias stands for
    matches too. None are where the files do not settle them
    (`_Remotes.declined`)."""
    spellings = _rewrites(common_dir, git_dir).spellings
    return [u for u, _r, _m in spellings] + [r for _u, r, _m in spellings if r]


@dataclass(frozen=True)
class RemoteKeys:
    """A repository's remotes as a capture keys them (`read_remotes`):
    every key, the alias keys among them, the config files read for them,
    the machine's config files named, and why the files do not settle
    them, where they do not (`_rewrites`), where the capture declines."""

    keys: tuple[str, ...]
    aliases: tuple[str, ...]
    files: tuple[ConfigFile, ...]
    sources: tuple[str, ...] = ()
    declined: str | None = None


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
    remotes are kept with their stamps, and the machine's config files
    git read, so a capture kept against them is read again once one
    changes or git names others (`config_unchanged`; ENG-207). Where the
    files do not settle the remotes, none is keyed and `declined` says
    why."""
    found = _rewrites(common_dir, git_dir)
    keys: set[str] = set()
    fetched: set[str] = set()
    aliased: set[str] = set()
    for url, rewritten, machine in found.spellings:
        written = key(url)
        as_fetched = written if rewritten is None else key(rewritten)
        keys.update(k for k in (written, as_fetched) if k)
        fetched.update([as_fetched] if as_fetched else [])
        if written and as_fetched and machine and as_fetched != written:
            aliased.add(written)
    return RemoteKeys(
        keys=tuple(sorted(keys)),
        aliases=tuple(sorted(aliased - fetched)),
        files=tuple(found.files),
        sources=found.named,
        declined=found.declined,
    )


def config_unchanged(cap: Capture) -> bool:
    """Whether a kept capture's config reads as it did (ENG-207): the
    machine's config files git reads now are the ones read for its
    remotes (`Capture.config_sources`), so one taken while git named the
    fallback is read again once git answers (ENG-207's review, R7), and
    each config file read stamps as it did (`gitconfig.unchanged`). The
    git binary is not stated here (`gitsystem.sources`)."""
    if cap.config_sources and _machine_configs(stamp=False) != cap.config_sources:
        return False
    return unchanged(cap.config_files)


#: The config files `_stamped_read` last read, each for a repository's
#: git directory and the home directory ``~/`` named, with the files it
#: included there, stamped; the most kept, the oldest evicted first.
_STAMPED_READS: dict[tuple[str, str, str | None], ConfigValues] = {}
_STAMPED_LOCK = threading.Lock()
_STAMPED_KEPT = 256
#: The reads `machine_config_reads` took from the files, by `_STAMPED_READS`'
#: key, with when (`time.monotonic`): the stamps `machine_config_stamps`
#: takes next within `FRESH_SECONDS` are those reads' own, the files not
#: stated again (ENG-207's review of round 8: a cold process stated each
#: twice, its files' answer and then the origin's signature, 9 stats
#: become 14). Each is taken once; a later signature states the files.
_FRESH_READS: dict[tuple[str, str, str | None], float] = {}
#: How long a read `machine_config_reads` took stands for the next stamps
#: unstated: a change to a file within it is seen at the signature after,
#: as one made just after the stamps were stated is.
FRESH_SECONDS = 1.0


@_caches.register
def _clear_stamped_reads() -> None:
    with _STAMPED_LOCK:
        _STAMPED_READS.clear()
        _FRESH_READS.clear()


def _stamped_read(path: str, git_dir: str) -> ConfigValues:
    """The config file `path` read for the repository whose git directory
    is `git_dir`, stamped, its includes with it (`gitconfig.config_tree`): kept
    for that directory and the home directory an include's ``~/`` names,
    and read again only where one of the files it read stamps otherwise
    (`gitconfig.unchanged`), so a steady call states them and reads none.
    It is read with no `gitconfig.IncludeContext`: an include under
    ``onbranch:`` or ``hasconfig:`` is read and stamped whatever its
    condition (`gitconfig.ConfigValues.undecided`), so the stamps name
    every file git may read there, whichever branch HEAD names (which the
    signatures hold already) and whichever remote URLs the files set."""
    key = (path, git_dir, _home())
    with _STAMPED_LOCK:
        read = _STAMPED_READS.get(key)
    if read is None or not unchanged(read.files or ()):
        read = config_tree(path, git_dir=git_dir, stamped=True)
        with _STAMPED_LOCK:
            _STAMPED_READS.pop(key, None)
            _STAMPED_READS[key] = read
            while len(_STAMPED_READS) > _STAMPED_KEPT:
                del _STAMPED_READS[next(iter(_STAMPED_READS))]
    return read


def machine_config_stamps(git_dir: str) -> tuple[object, ...]:
    """The machine's config as git reads it for the repository whose git
    directory is `git_dir`, as stamps (ENG-207's review): the config
    files git reads besides the repository's own (`_machine_configs`:
    the system's, as `gitsystem` keeps git's answer, then the global
    ones), each with the files it includes there, stamped as a capture
    stamps them (`_rewrites`). `origin`'s capture is kept against it, so
    a rule set there reaches its next capture as it reaches a capture's
    (`config_unchanged`). Each file and its includes are read once and
    then only stated (`_stamped_read`), save where `machine_config_reads`
    read it within `FRESH_SECONDS`: its stamps are then that read's, not
    stated again, once (`_FRESH_READS`)."""
    named = _machine_configs(stamp=False)
    home, now = _home(), time.monotonic()
    stamps: list[object] = [named]
    for path in named:
        key = (path, git_dir, home)
        with _STAMPED_LOCK:
            taken = _FRESH_READS.pop(key, None)
            read = _STAMPED_READS.get(key)
        if read is None or taken is None or not 0 <= now - taken < FRESH_SECONDS:
            read = _stamped_read(path, git_dir)
        stamps.append(tuple(read.files or ()))
    return tuple(stamps)


def machine_config_reads(git_dir: str) -> tuple[ConfigValues, ...]:
    """The reads of the machine's config `machine_config_stamps` keeps for
    the repository whose git directory is `git_dir` (`_stamped_read`), as
    kept, with no file read or stated: `origin`'s capture states them at
    each signature (a changed one is read again there), so one read here
    is as fresh as the origin captured before it (ENG-207's round 8,
    `origin_gitfiles._machine_config_dies`). A file no capture of this
    process read yet is read, opened and stated, once, and kept, and the
    stamps the origin's signature takes next reuse that read's
    (`_FRESH_READS`), so the two together state it once."""
    home, out = _home(), []
    for path in _machine_configs(stamp=False):
        key = (path, git_dir, home)
        with _STAMPED_LOCK:
            read = _STAMPED_READS.get(key)
        if read is None:
            started = time.monotonic()
            read = _stamped_read(path, git_dir)
            with _STAMPED_LOCK:
                _FRESH_READS.pop(key, None)
                _FRESH_READS[key] = started
                while len(_FRESH_READS) > _STAMPED_KEPT:
                    del _FRESH_READS[next(iter(_FRESH_READS))]
        out.append(read)
    return tuple(out)


def repository_config_stamps(common_dir: str, git_dir: str) -> tuple[object, ...]:
    """The repository's own config as git reads it, as stamps (ENG-207's
    review): the common config and, where git reads it
    (`gitfile_rule.reads_worktree_config`), the git directory's
    ``config.worktree``, each with the files it includes there under any
    condition but ``gitdir:``, stamped as a capture stamps them
    (`_repository_paths`), and read once and then only stated
    (`_stamped_read`). `origin`'s capture is kept
    against it, so a rule set in an included file reaches its next
    capture as it reaches a capture's (`config_unchanged`). Where git
    does not read ``config.worktree`` it is not stated: only the common
    config, stamped here, decides that, so the change that has git read
    it is seen."""
    common = _stamped_read(os.path.join(common_dir, "config"), git_dir)
    out: list[object] = [tuple(common.files or ())]
    if reads_worktree_config(common_dir):
        worktree = _stamped_read(os.path.join(git_dir, "config.worktree"), git_dir)
        out.append(tuple(worktree.files or ()))
    return tuple(out)


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


class GitDies(Exception):
    """git dies reading the format of the repository discovery reaches
    (`gitfile_rule.FormatUnknown`: a config line its parser refuses, a
    value it dies on), or that format is not known here: git reads no
    repository there and goes no further, and the capture declines
    (INDETERMINATE), never reading the folder as outside one."""


def _from_env(
    start: str, refused: list[tuple[str, str, str]] | None = None
) -> _Git | None:
    """The repository GIT_DIR (with GIT_WORK_TREE, GIT_COMMON_DIR or
    core.worktree) names (I22), its working tree as setup.c's
    ``setup_explicit_git_dir`` places it: GIT_WORK_TREE, else none where
    the format git reads (`gitfile_rule.tree_format`) sets ``core.bare``,
    else its ``core.worktree`` taken from the git directory, else the
    directory git runs in. `GitDies` where git dies on that format, or on
    an empty ``core.worktree``, the refusal to `refused` first."""
    raw = os.environ.get("GIT_DIR")
    if not raw:
        return None
    git_dir = os.path.normpath(os.path.join(start, raw))
    common, linked = _common_of(git_dir)
    found = tree_format(git_dir)
    work = os.environ.get("GIT_WORK_TREE")
    why = None
    if found.why is not None:
        why = f"{git_dir}'s config cannot be read as git reads it: {found.why}"
    elif not work and not found.bare and found.worktree == "":
        why = f"{git_dir}'s config names no working tree (an empty core.worktree)"
    if why is not None:
        if refused is not None:
            refused.append((git_dir, git_dir, why))
        raise GitDies(why)
    root: str | None
    if work:
        root = os.path.normpath(os.path.join(start, work))
    elif found.bare:
        root = None
    elif found.worktree is not None:
        root = os.path.normpath(os.path.join(git_dir, found.worktree))
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
    `location.signature` does. `GitDies` where git dies on the format of
    a git directory the walk reaches (`gitfile_rule.FormatUnknown`), its
    refusal to `refused` first: git stops there, reading nothing."""
    if os.environ.get("GIT_DIR"):
        return _from_env(start, refused)
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
                if isinstance(why, FormatUnknown):
                    raise GitDies(why)
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
        if isinstance(seen.why, FormatUnknown):
            raise GitDies(seen.why)
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
    working tree (`record_working_tree`, ENG-206); ``config_files``
    holds the config files read for the remotes besides the repository's
    own, stamped, and ``config_sources`` the machine's config files git
    read then, which a kept capture is checked against (ENG-207,
    `config_unchanged`); none of these four is part of the token, and the
    last two are no part of what a capture equals."""

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
    config_sources: tuple[str, ...] = field(default=(), compare=False, repr=False)

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
            header = gitobjects.parent_lines(body, hexsz)
            found = () if header is None else header[0]
            when = gitobjects.committed_at(body, hexsz)
        self._parents[sha] = found
        self._committed[sha] = when
        return found

    def committed(self, commit: str) -> int:
        """The commit's committer time (`gitobjects.committed_at`), read from the
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
        hold, as this reader reads them (`gitobjects.parent_lines`): no replace refs
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
