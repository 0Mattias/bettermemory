"""A git directory's working tree, read as git reads it (ENG-211).

ENG-211 refused a git directory whose config moves git's working tree off
the folder holding it, and read that config through the reader of
remotes, which splits lines wherever Python does, follows includes and
reads 1 MiB of a file. git reads a repository's format otherwise
(setup.c's ``read_repository_format``): LF alone ends a line, a backslash
before it joins the next one, a leading byte order mark is dropped, no
include is followed and the whole file is read. Each of those let a config
name another folder to git and none to the rule, and membership rooted the
checkout at the folder while git, and the origin, rooted it in the other.

The rule now reads the names that decide the root as git does
(`gitfile_rule.tree_format`, `gitconfig.git_entries`), and an origin keeps
only a toplevel that is the folder membership roots the checkout at
(`gitfile_rule.toplevel_agrees`), so the two never name two roots whatever
that reading misses. Every expectation is git's own answer first, read from
a real git: its toplevel, and the variables ``git config --list`` lists.
The one exception is a compatibility object format the same as the object
format, which a git built with Rust aborts on: git's source is the premise
there, and the git on PATH is asked only under
``BETTERMEMORY_TEST_GIT_ABORTS=1``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import (
    gitconfig,
    gitfile_rule,
    githead,
    gitsystem,
    location,
    location_audit,
    origin,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from . import test_location_gitdir_worktree as gitdir_worktree
from ._mcp import call_tool
from .test_location_gitdir_worktree import (
    REMOTE,
    _config_path,
    _inside,
    _membership,
    _no_origin,
    _same,
    _set,
)
from .test_membership_security import _git, checkout, neutral_git
from .test_origin_patch import _git_version

pytestmark = gitdir_worktree.pytestmark


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, git's walk stopped above
    `tmp_path` (`neutral_git`)."""
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture(name="runs")
def _runs(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """The folder of every git process the package starts from here on."""
    seen: list[Path | None] = []
    real = git_runner.run_git

    def spy(args: Any, **kwargs: Any) -> Any:
        cwd = kwargs.get("cwd")
        seen.append(None if cwd is None else Path(cwd))
        return real(args, **kwargs)

    monkeypatch.setattr(git_runner, "run_git", spy)
    return seen


def _toplevel(where: Path) -> subprocess.CompletedProcess[str]:
    """git's own answer for `where`: ``rev-parse --show-toplevel``."""
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=where,
        capture_output=True,
        text=True,
    )


def _git_names(where: Path, other: Path) -> None:
    done = _toplevel(where)
    assert done.returncode == 0, done.stderr
    assert _same(done.stdout.strip(), other), done.stdout


def _apps(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with a remote, and another checkout of the user's that a
    config may name."""
    app = checkout(tmp_path / "work" / "app", remote=REMOTE)
    other = checkout(tmp_path / "work" / "other")
    return app, other


def _padding(sections: int) -> bytes:
    return b"".join(
        b'[branch "b%d"]\n\tremote = origin\n\tmerge = refs/heads/b%d\n' % (i, i)
        for i in range(sections)
    )


#: Each spelling of ``core.worktree = <other>`` git reads and the rule's
#: earlier reader did not, as a change to the config's bytes.
SPELLINGS: dict[str, Callable[[bytes, bytes], bytes]] = {
    "continuation": lambda c, o: c + b"[core]\n\tx = y\\\n[other]\n\tworktree = " + o,
    "lone_cr": lambda c, o: c + b"[core]\n\tx = y\r[other]\n\tworktree = " + o,
    "vt": lambda c, o: c + b"[core]\n\tx = y\x0b[other]\n\tworktree = " + o,
    "ff": lambda c, o: c + b"[core]\n\tx = y\x0c[other]\n\tworktree = " + o,
    "fs": lambda c, o: c + b"[core]\n\tx = y\x1c[other]\n\tworktree = " + o,
    "nel": lambda c, o: c + b"[core]\n\tx = y\xc2\x85[other]\n\tworktree = " + o,
    "line_separator": lambda c, o: (
        c + b"[core]\n\tx = y\xe2\x80\xa8[o]\n\tworktree = " + o
    ),
    "byte_order_mark": lambda c, o: b"\xef\xbb\xbf" + c + b"[core]\n\tworktree = " + o,
    "case": lambda c, o: c + b"[CORE]\n\tWorkTree = " + o,
    "on_the_header_line": lambda c, o: c + b"[core]worktree=" + o,
    "crlf": lambda c, o: c + b"[core]\r\n\tworktree = " + o + b"\r\n",
    "include_after": lambda c, o: (
        c + b"[core]\n\tworktree = " + o + b"\n[include]\n\tpath = back.inc\n"
    ),
    "include_if_after": lambda c, o: (
        c
        + b"[core]\n\tworktree = "
        + o
        + b'\n[includeIf "gitdir:**"]\n\tpath = back.inc\n'
    ),
    "past_a_mebibyte": lambda c, o: c + _padding(30000) + b"[core]\n\tworktree = " + o,
}


@pytest.mark.parametrize("how", list(SPELLINGS))
def test_each_spelling_git_reads_is_refused_by_both_readers(
    tmp_path: Path, runs: list[Path | None], how: str
) -> None:
    """The review's spellings: each names another folder as the working
    tree to git (its toplevel says so) and was read by the rule as naming
    none, an include setting it back to the folder, or the folder past the
    1 MiB the rule read. Each is now refused: membership passes over the
    entry, and the origin is a folder's outside any repository, no git
    run."""
    app, other = _apps(tmp_path)
    config = app / ".git" / "config"
    (app / ".git" / "back.inc").write_text("[core]\n\tworktree = ..\n")
    config.write_bytes(SPELLINGS[how](config.read_bytes(), _config_path(other)) + b"\n")
    _git_names(app, other)
    runs.clear()

    assert _membership(app).state == location.OUTSIDE
    _no_origin(app)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []


#: A Windows path in ``core.worktree`` as a user's config may hold it, and
#: the value git reads from it: escaped as ``git config`` writes it, with
#: forward slashes, quoted around a space, a share, and written as it is,
#: where git dies on an escape it does not know (``\U``: None) and reads
#: the ones it knows (``\t``, ``\n``) as their characters. Strings, not
#: paths, so each is checked against git off Windows too.
WINDOWS_VALUES: dict[str, tuple[bytes, str | None]] = {
    "escaped": (b"C:\\\\Users\\\\me\\\\app", "C:\\Users\\me\\app"),
    "forward_slashes": (b"C:/Users/me/app", "C:/Users/me/app"),
    "quoted": (b'"C:\\\\Program Files\\\\app"', "C:\\Program Files\\app"),
    "share": (b"\\\\\\\\host\\\\share\\\\app", "\\\\host\\share\\app"),
    "known_escapes": (b"C:\\temp\\new", "C:\temp\new"),
    "unknown_escape": (b"C:\\Users\\me\\app", None),
}


def _windows_config(value: bytes) -> bytes:
    return b"[core]\n\trepositoryformatversion = 0\n\tworktree = " + value + b"\n"


#: Config texts git parses, or refuses, in the ways the rule's parse follows:
#: values (quotes, escapes, comments, trimmed and kept blank space, a NUL
#: byte), names (case, subsections, the older dotted form, a NUL byte),
#: headers and keys git refuses, the end of the file, a partial byte order
#: mark, the spellings above and the Windows paths.
TEXTS: list[bytes] = [
    b'[a]\n\tk\n\tv = 1\n\tn = x\\ny\n[b "s\x00t"]\n\tz = q\x00r\n',
    b'[a]\n\tq = " a ; b " ; c\n\tr = a "" \n\ts = "x"y"z"\n\tt = \\t\\b\\\\\\"\n',
    b"[a]\n\tu = a \\\n  b\n\tw = a ; c \\\n\tx = 1\n",
    b'[A "Sub\\\\Q\\"x"]\n\tK-1 = v\n[A.B]\n\tk = v\n',
    b"[a]\n\tk = v\\",
    b"[a]\n\tk = v",
    b"[a]\n\tk\t=\tv\r\n\tj = w\r",
    b'[ "x"]\n\ty = 1\n',
    b"k = before any section\n[a]\n",
    b"[a]\n\tk = \x0bv\x0c\n",
    b'[a]\n\tk = \xff\xfe\n[b "\xc3\xa9"]\n\tz = 1\n',
    b"[]\n",
    b"[a\n",
    b'[a "x" ]\n',
    b"[a]\n\t1k = v\n",
    b"[a]\n\tk\x00 = v\n",
    b"[a]\n\tk = \\q\n",
    b'[a]\n\tk = "open\n',
    b"[a]\n\tk\r= v\n",
    b"\xef\xbb[a]\n",
    b"\xef\xbb\xbf\xef\xbb\xbf[a]\n",
    *(
        spell(b"[core]\n\tbare = false\n", b"/elsewhere") + b"\n"
        for spell in SPELLINGS.values()
    ),
    *(_windows_config(value) for value, _ in WINDOWS_VALUES.values()),
]


@pytest.mark.parametrize("text", TEXTS, ids=range(len(TEXTS)))
def test_the_parse_is_git_s(tmp_path: Path, text: bytes) -> None:
    """`gitconfig.git_entries` reads a file as git's parser does: the
    variables ``git config --file <f> --list --null`` lists (each name, and
    each value as git's callbacks see it, up to a NUL byte; no include
    followed), and none where git refuses the file."""
    path = tmp_path / "config"
    path.write_bytes(text)
    done = subprocess.run(
        ["git", "config", "--file", str(path), "--list", "--null"],
        capture_output=True,
    )
    ours = gitconfig.git_entries(text)
    if done.returncode != 0:
        assert b"bad config" in done.stderr, done.stderr
        assert ours is None
        return
    listed = [
        (item.split(b"\n", 1)[0], item.split(b"\n", 1)[1] if b"\n" in item else None)
        for item in done.stdout.split(b"\0")[:-1]
    ]
    assert ours is not None
    assert [
        (name, None if value is None else value.split(b"\0", 1)[0])
        for name, value in ours
    ] == listed


@pytest.mark.parametrize("how", list(WINDOWS_VALUES))
def test_a_windows_path_is_the_working_tree_git_reads(tmp_path: Path, how: str) -> None:
    """A Windows user's ``core.worktree``: `gitfile_rule.tree_format` takes
    the value git takes (``git config --get``), escapes undone as git
    undoes them, and refuses the config where git dies on an escape."""
    value, expected = WINDOWS_VALUES[how]
    git_dir = tmp_path / "dot_git"
    git_dir.mkdir()
    config = git_dir / "config"
    config.write_bytes(_windows_config(value))
    done = subprocess.run(
        ["git", "config", "--file", str(config), "--null", "core.worktree"],
        capture_output=True,
    )
    got = gitfile_rule.tree_format(str(git_dir))
    if expected is None:
        assert b"bad config" in done.stderr, done.stderr
        assert got.why is not None and got.worktree is None, got
        return
    assert done.returncode == 0, done.stderr
    assert done.stdout == os.fsencode(expected) + b"\0"
    assert got == gitfile_rule.TreeFormat(worktree=expected), got


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (b"0", 0),
        (b"1", 1),
        (b"0x0", 0),
        (b"-1", -1),
        (b"\x0b1", 1),
        (b"1k", 1024),
        (b"2097151k", 2097151 * 1024),
        (b"08", None),
        (b"0xg", None),
        (b"", None),
        (None, None),
        (b"2147483648", None),
        (b"-2097152k", None),
    ],
)
def test_a_number_is_read_as_git_reads_it(
    value: bytes | None, number: int | None
) -> None:
    """``git_config_int``: ``strtoimax`` in base 0, a unit, within an int,
    and git dying (None) on anything else; checked against git where it
    decides a root (the format version, below)."""
    assert gitconfig.git_int(value) == number


def _without_version(app: Path, tail: bytes) -> None:
    config = app / ".git" / "config"
    body = config.read_bytes()
    assert b"\trepositoryformatversion = 0\n" in body
    config.write_bytes(body.replace(b"\trepositoryformatversion = 0\n", b"") + tail)


@pytest.mark.parametrize("how", ["worktree", "bare", "negative"])
def test_a_config_git_reads_no_format_from_leaves_the_tree_at_the_folder(
    tmp_path: Path, how: str
) -> None:
    """git takes neither ``core.worktree`` nor ``core.bare`` from a config
    with no ``core.repositoryformatversion``, or one below 0
    (``read_repository_format`` clears the format): git roots the checkout
    at the folder, and both readers do too. The rule refused it."""
    app, other = _apps(tmp_path)
    tail = {
        "worktree": b"[core]\n\tworktree = " + _config_path(other) + b"\n",
        "bare": b"[core]\n\tbare = true\n",
        "negative": b"[core]\n\trepositoryformatversion = -1\n\tworktree = "
        + _config_path(other)
        + b"\n",
    }[how]
    _without_version(app, tail)
    _git_names(app, app)

    cap = _membership(app)
    assert cap.state == location.REPO and _same(cap.root, app), cap
    got = origin.capture(app)
    assert got.repo == REMOTE and _same(got.worktree_root, app)


@pytest.mark.parametrize(
    "tail",
    [
        b"[core]\n\tworktree =\n",
        b'[core]\n\tworktree = ""\n',
        b"[core]\n\tworktree\n",
        b"[core]\n\tbare = maybe\n",
        b"[core]\n\trepositoryformatversion = 2\n",
        b"[core]\n\tx = \\q\n",
    ],
    ids=["empty", "empty_quoted", "no_value", "no_boolean", "version_2", "bad_line"],
)
def test_a_config_git_dies_on_is_refused_by_both_readers(
    tmp_path: Path, runs: list[Path | None], tail: bytes
) -> None:
    """An empty ``core.worktree`` (git dies: "cannot chdir to ''"), one with
    no value, a ``core.bare`` that is no boolean, a format version git does
    not read and a line git cannot parse: git names no toplevel, and
    neither reader names a root. The rule followed the folder at the first
    two (an empty value read as none). git dies there, and reads no folder
    outside a repository: membership declines (INDETERMINATE), where it
    read the folder as OUTSIDE (ENG-207's round 6)."""
    app, _other = _apps(tmp_path)
    config = app / ".git" / "config"
    config.write_bytes(config.read_bytes() + tail)
    assert _toplevel(app).returncode != 0
    runs.clear()

    cap = _membership(app)
    assert cap.state == location.INDETERMINATE
    assert cap.detail.startswith("repository format: ")
    _no_origin(app)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []


_V1 = b"[core]\n\trepositoryformatversion = 1\n"

#: Extensions git dies on however it was built, as a tail to a config of
#: format 0 (a version a tail sets standing, the last of two), and with
#: no version set ("unset_"): a value its reader refuses, one of format 1
#: under format 0, one it does not know under format 1; a compatibility
#: object format set twice.
EXTENSION_DIES = {
    "partial_clone_no_value": b"[extensions]\n\tpartialClone\n",
    "precious_no_boolean": b"[extensions]\n\tpreciousObjects = maybe\n",
    "worktree_config_no_boolean": b"[extensions]\n\tworktreeConfig = maybe\n",
    "v1_only_in_v0": b"[extensions]\n\trefStorage = files\n",
    "unknown_in_v1": _V1 + b"[extensions]\n\tbogus = 1\n",
    "object_format_unknown": _V1 + b"[extensions]\n\tobjectFormat = sha3\n",
    "object_format_case": _V1 + b"[extensions]\n\tobjectFormat = SHA1\n",
    "ref_storage_no_value": _V1 + b"[extensions]\n\trefStorage\n",
    "relative_no_boolean": _V1 + b"[extensions]\n\trelativeWorktrees = maybe\n",
    "compat_in_v0": b"[extensions]\n\tcompatObjectFormat = sha1\n",
    "unset_precious_no_boolean": b"[extensions]\n\tpreciousObjects = maybe\n",
    "unset_compat_unknown": b"[extensions]\n\tcompatObjectFormat = sha3\n",
    "unset_compat_no_value": b"[extensions]\n\tcompatObjectFormat\n",
    "unset_compat_twice": b"[extensions]\n\tcompatObjectFormat = sha256\n"
    + b"[extensions]\n\tcompatObjectFormat = sha256\n",
}

#: Extensions git reads on however it was built: one it does not know
#: under format 0, a value each reader takes, and one of format 1 with no
#: version set (a compatibility object format in each pairing among
#: them, which git clears there).
EXTENSION_READS = {
    "unknown_in_v0": b"[extensions]\n\tbogus = 1\n",
    "partial_clone_named": b"[extensions]\n\tpartialClone = origin\n",
    "precious": b"[extensions]\n\tpreciousObjects = true\n",
    "v1_known": _V1
    + b"[extensions]\n\tobjectFormat = sha1\n\trefStorage = files\n\tnoop-v1\n"
    + b"\trelativeWorktrees\n\tsubmodulePathConfig = true\n\tnoop = x\n",
    "unset_v1_only": b"[extensions]\n\trefStorage = files\n\tbogus = 1\n",
    "unset_compat_sha1": b"[extensions]\n\tcompatObjectFormat = sha1\n",
    "unset_compat_sha256": b"[extensions]\n\tobjectFormat = sha1\n"
    + b"\tcompatObjectFormat = sha256\n",
    "unset_compat_over_sha256": b"[extensions]\n\tobjectFormat = sha256\n"
    + b"\tcompatObjectFormat = sha1\n",
    "unset_compat_same": b"[extensions]\n\tobjectFormat = sha256\n"
    + b"\tcompatObjectFormat = sha256\n",
}


def _extend(app: Path, case: str, tail: bytes) -> None:
    if case.startswith("unset_"):
        _without_version(app, tail)
    else:
        config = app / ".git" / "config"
        config.write_bytes(config.read_bytes() + tail)


@pytest.mark.parametrize("case", sorted(EXTENSION_DIES))
def test_an_extension_git_dies_on_is_refused_by_both_readers(
    tmp_path: Path, runs: list[Path | None], case: str
) -> None:
    """The repository's extensions, read as setup.c reads them: a value git
    refuses, a format-1 extension under format 0 and one git does not know
    under format 1 make git die (``git rev-parse``), and neither reader
    names a root. The rule read none but ``worktreeConfig``, and rooted the
    checkout at the folder git names no toplevel of (ENG-207's round 7)."""
    app, _other = _apps(tmp_path)
    _extend(app, case, EXTENSION_DIES[case])
    assert _toplevel(app).returncode != 0
    runs.clear()

    cap = _membership(app)
    assert cap.state == location.INDETERMINATE
    assert cap.detail.startswith("repository format: "), cap.detail
    _no_origin(app)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []


@pytest.mark.parametrize("case", sorted(EXTENSION_READS))
def test_an_extension_git_reads_keeps_the_root(tmp_path: Path, case: str) -> None:
    """Where git reads the extensions and names the folder its toplevel,
    both readers root the checkout there (no extension read as fatal that
    git reads on)."""
    app, _other = _apps(tmp_path)
    _extend(app, case, EXTENSION_READS[case])
    _git_names(app, app)

    cap = _membership(app)
    assert cap.state == location.REPO and _same(cap.root, app), cap
    got = origin.capture(app)
    assert got.repo == REMOTE and _same(got.worktree_root, app)


_BELOW_0 = b"[core]\n\trepositoryformatversion = -2\n"

#: A compatibility object format other than the object format under a
#: version git keeps it at (1, and below -1), which git reads as it was
#: built (ENG-207's round 8; git 2.54 built without Rust dies on it,
#: "compatibility hash algorithm support requires Rust", git 2.56 built
#: with it reads it).
EXTENSION_BUILT = {
    "v1_differ": _V1 + b"[extensions]\n\tcompatObjectFormat = sha256\n",
    "below_0_differ": _BELOW_0 + b"[extensions]\n\tcompatObjectFormat = sha256\n",
}

#: A compatibility object format the same as the object format under those
#: versions, which no git from 2.45 reads (ENG-207's review of round 8).
#: From git 2.45 to 2.53 repository.c's ``repo_set_compat_hash_algo``
#: stops at ``BUG("hash_algo and compat_hash_algo match")``. From 2.54
#: (repository.c in v2.54.0 and on master) a build with Rust stops at the
#: same ``BUG()``, and one without it dies on any compatibility object
#: format ("compatibility hash algorithm support requires Rust"). usage.c's
#: ``BUG_vfl`` ends in ``abort()``: a crash report or a core for each git
#: run on such a repository. git 2.44 and earlier know no such extension
#: (setup.c in v2.44.0): they die on it under format 1, as on any they do
#: not know, and read the repository below -1, whose extensions they do
#: not verify; the readers decline it there too, which fails closed
#: (ENG-207's review of round 10).
EXTENSION_SAME = {
    "v1_same": _V1 + b"[extensions]\n\tcompatObjectFormat = sha1\n",
    "below_0_same": _BELOW_0 + b"[extensions]\n\tcompatObjectFormat = sha1\n",
}

#: The opt-in to tests that run the git on PATH where it may abort.
_GIT_ABORTS = pytest.mark.skipif(
    os.environ.get("BETTERMEMORY_TEST_GIT_ABORTS") != "1",
    reason="runs git where a build with Rust aborts; set BETTERMEMORY_TEST_GIT_ABORTS=1",
)


@pytest.mark.parametrize("case", sorted(EXTENSION_BUILT))
def test_a_compatibility_object_format_is_read_as_this_git_reads_it(
    tmp_path: Path, runs: list[Path | None], case: str
) -> None:
    """A ``compatObjectFormat`` other than the object format, under a
    version git keeps it at, is read as the git on PATH reads it, as its
    build decides, asked of that git in the probe (`gitsystem.reads_compat`,
    under format 1). Where git reads it and names the folder its toplevel,
    both readers root the checkout there; where it dies, neither names a
    root, and no git runs in the folder. The readers refused each, and the
    test took git 2.54's answer, dying, for its premise, which git 2.56
    built with Rust does not give (ENG-207's round 8, CI). A git of 2.44
    or earlier reads one below -1 and dies on it under format 1, which it
    is asked under: the readers decline it, fail closed (ENG-207's review
    of round 10)."""
    app, _other = _apps(tmp_path)
    _extend(app, case, EXTENSION_BUILT[case])
    git_reads = _toplevel(app).returncode == 0
    said = gitsystem.reads_compat()
    if _unverified_below_0(case):
        assert (git_reads, said) == (True, False), "premise: git 2.44's reading"
        git_reads = False
    assert said is git_reads, "the probe answers as git reads the repository"
    runs.clear()

    cap = _membership(app)
    if git_reads:
        assert cap.state == location.REPO and _same(cap.root, app), cap
        got = origin.capture(app)
        assert got.repo == REMOTE and _same(got.worktree_root, app)
        return
    assert cap.state == location.INDETERMINATE
    assert cap.detail.startswith("repository format: "), cap.detail
    _no_origin(app)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []


@pytest.mark.parametrize("case", sorted(EXTENSION_SAME))
def test_a_compatibility_object_format_the_same_is_refused_by_both_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[Path | None],
    case: str,
) -> None:
    """A ``compatObjectFormat`` the same as the object format, under a
    version git keeps it at, is declined, as git's source decides
    (`EXTENSION_SAME`; ENG-207's review of round 8): neither reader
    names a root, the git on PATH is not asked how it reads the pairing,
    and no git runs in the folder. No git runs on the repository here at
    all, since a build with Rust aborts there; the git on PATH is asked of
    it only under ``BETTERMEMORY_TEST_GIT_ABORTS=1``
    (`test_git_dies_on_a_compatibility_object_format_the_same`)."""
    app, _other = _apps(tmp_path)
    _extend(app, case, EXTENSION_SAME[case])
    asked: list[bool] = []

    def reads() -> bool | None:
        asked.append(True)
        return True

    monkeypatch.setattr(gitsystem, "reads_compat", reads)
    runs.clear()

    cap = _membership(app)
    assert cap.state == location.INDETERMINATE
    assert cap.detail.startswith("repository format: "), cap.detail
    _no_origin(app)
    assert [cwd for cwd in runs if _inside(cwd, app)] == []
    assert asked == []


@_GIT_ABORTS
@pytest.mark.parametrize("case", sorted(EXTENSION_SAME))
def test_git_dies_on_a_compatibility_object_format_the_same(
    tmp_path: Path, case: str
) -> None:
    """The premise of `EXTENSION_SAME`, of the git on PATH: it names no
    toplevel of a checkout whose ``compatObjectFormat`` is its object
    format, save a git of 2.44 or earlier below -1, which reads it (and
    the readers decline, fail closed). A git built with Rust aborts here,
    so the test runs only under ``BETTERMEMORY_TEST_GIT_ABORTS=1``."""
    app, _other = _apps(tmp_path)
    _extend(app, case, EXTENSION_SAME[case])
    reads = _toplevel(app).returncode == 0
    assert reads is _unverified_below_0(case), "premise: as git's source says"


def _unverified_below_0(case: str) -> bool:
    """Whether `case` sets a version below -1 and the git on PATH is 2.44
    or earlier, whose setup.c verifies no extension of a negative format,
    and so reads a compatibility object format it does not know."""
    return case.startswith("below_0") and _git_version() < (2, 45)


#: How a git may read a compatibility object format other than the object
#: format, as `gitsystem.reads_compat` answers: as git 2.54 built without
#: Rust (it dies), one built with it or a git from 2.45 to 2.53 (it
#: reads), and one that has not said.
_COMPAT_SAID: dict[str, bool | None] = {
    "without_rust": False,
    "with_rust": True,
    "unsaid": None,
}


@pytest.mark.parametrize("build", list(_COMPAT_SAID))
def test_a_compatibility_object_format_is_judged_by_what_git_said(
    monkeypatch: pytest.MonkeyPatch, build: str
) -> None:
    """The format's reading of a ``compatObjectFormat`` other than the
    object format follows the git on PATH's answer
    (`gitfile_rule._judge_format`), sha1 or sha256, under format 1 and
    below -1: where git reads it the object format stands (that of a
    version below -1 too, which keeps it), and where git dies or has not
    said there is none (fail closed: git is asked). The same one is
    declined, unasked: no git from 2.45 reads it. With no version set git
    clears it, and no git is asked; under format 0 it dies, unasked."""
    differ = _COMPAT_SAID[build]
    same = False
    asked: list[bool] = []

    def reads() -> bool | None:
        asked.append(True)
        return differ

    monkeypatch.setattr(gitsystem, "reads_compat", reads)
    wrong = []
    for version in (b"1", b"-2"):
        for main, compat in (
            (b"sha1", b"sha256"),
            (b"sha256", b"sha1"),
            (b"sha1", b"sha1"),
            (b"sha256", b"sha256"),
        ):
            data = (
                b"[core]\n\trepositoryformatversion = " + version + b"\n"
                b"[extensions]\n\tobjectFormat = " + main + b"\n"
                b"\tcompatObjectFormat = " + compat + b"\n"
            )
            expect = main if (same if main == compat else differ) else None
            if gitfile_rule.object_format(data) != expect:
                wrong.append((version, main, compat))
    assert wrong == []
    assert asked == [True, True] * 2
    asked.clear()
    cleared = b"[extensions]\n\tobjectFormat = sha256\n\tcompatObjectFormat = sha1\n"
    assert gitfile_rule.object_format(cleared) == b"sha1"
    assert (
        gitfile_rule.object_format(b"[core]\n\trepositoryformatversion = 0\n" + cleared)
        is None
    )
    assert asked == []


def test_a_config_past_the_format_bound_is_refused(tmp_path: Path) -> None:
    """git reads a config of any size; one past `gitconfig._FORMAT_LIMIT`
    is not read here, and is refused (fail closed) rather than judged by a
    part of it: membership declines, never reading the folder as outside a
    repository (ENG-207's round 6)."""
    app, _other = _apps(tmp_path)
    config = app / ".git" / "config"
    config.write_bytes(config.read_bytes() + _padding(40000))
    assert config.stat().st_size > gitconfig._FORMAT_LIMIT
    _git_names(app, app)

    cap = _membership(app)
    assert cap.state == location.INDETERMINATE
    assert "larger than 2 MiB" in cap.detail
    _no_origin(app)


def test_a_worktree_s_config_worktree_is_read_where_git_reads_it(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """A linked worktree's git directory takes ``core.worktree`` from its own
    ``config.worktree`` under ``extensions.worktreeConfig`` (git roots the
    worktree in the folder it names), and from the common config not at
    all without it: the first is refused, the second leaves the worktree
    where git keeps it."""
    main, other = _apps(tmp_path)
    tree = tmp_path / "work" / "tree"
    _git(main, "worktree", "add", "-q", str(tree), "-b", "tree")
    with (main / ".git" / "config").open("ab") as fh:
        fh.write(b"[core]\n\tworktree = " + _config_path(other) + b"\n")
    _git_names(tree, tree)
    assert _same(_membership(tree).root, tree)
    assert _same(origin.capture(tree).worktree_root, tree)

    _set(main / ".git", "extensions.worktreeConfig", "true")
    own = main / ".git" / "worktrees" / "tree" / "config.worktree"
    own.write_bytes(b"[core]\n\tworktree = " + _config_path(other) + b"\n")
    _git_names(tree, other)
    runs.clear()
    assert _membership(tree).state == location.OUTSIDE
    _no_origin(tree)
    assert [cwd for cwd in runs if _inside(cwd, tree)] == []


async def test_an_origin_keeps_no_toplevel_membership_does_not_root_at(
    tmp_path: Path,
    memory_dir: Path,
    runs: list[Path | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard that holds whatever the rule's reading of a config misses:
    with the parse made to miss ``core.worktree``, membership roots the
    checkout at its folder, git runs there and names the other folder, and
    the origin keeps no toplevel (`gitfile_rule.toplevel_agrees`). A write
    whose claim holds only in the other folder is refused, and nothing is
    stored."""
    app, _ = _apps(tmp_path)
    other = checkout(tmp_path / "work" / "other2", files={"only_there.txt": "x\n"})
    _set(app / ".git", "core.worktree", str(other))
    _git_names(app, other)
    real = gitconfig.read_git_config

    def missing(
        path: str, names: frozenset[bytes] | None = None
    ) -> gitconfig.GitConfigRead:
        read = real(path, names)
        kept = tuple(e for e in read.entries if e[0] != b"core.worktree")
        return gitconfig.GitConfigRead(kept, read.why)

    monkeypatch.setattr(gitconfig, "read_git_config", missing)
    cap = _membership(app)
    assert cap.state == location.REPO and _same(cap.root, app), cap
    runs.clear()

    got = origin.capture(app)
    assert any(_inside(cwd, app) for cwd in runs), "git ran: the rule missed it"
    assert (got.worktree_root, got.repo, got.git_indeterminate) == (None, None, False)
    assert origin.repo_toplevel(app) is None
    assert origin.repo_toplevel_and_head(app) is None

    import bettermemory._handlers as handlers_module

    monkeypatch.setattr(
        handlers_module, "capture_origin", lambda cwd=None: origin.capture(app)
    )
    store = Store(memory_dir)
    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    write: dict[str, Any] = {
        "content": "the setting lives in only_there.txt",
        "scopes": ["tools"],
        "claims": ["only_there.txt"],
    }
    with pytest.raises(Exception, match="not inside a git worktree"):
        await call_tool(server, "memory_write", write)
    assert store.load_all() == []


def test_a_session_inside_a_submodule_s_git_directory_joins_no_project(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """Inside ``.git/modules/<sub>``, git reads a git directory whose
    ``core.worktree`` names the submodule's tree, and roots there; the rule
    refuses that directory, and membership's walk went on to the
    superproject's ``.git`` and rooted the session there. It stops where
    git's walk stops: no project, no origin, and the audit says where
    sessions belong without a warning. (9.0.0rc18 read the directory as a
    bare repository keyed on itself.)"""
    lib = checkout(tmp_path / "src" / "lib")
    sup = checkout(tmp_path / "work" / "super", remote=REMOTE)
    _git(
        sup,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(lib),
        "sub",
    )
    modules = sup / ".git" / "modules" / "sub"
    for here in (modules, modules / "refs"):
        _git_names(here, sup / "sub")
        runs.clear()
        cap = _membership(here)
        assert cap.state == location.OUTSIDE and cap.root is None, cap
        _no_origin(here)
        assert [cwd for cwd in runs if _inside(cwd, sup)] == []
        found = location_audit.gitfile_posture(str(here))
        assert found["status"] == "info", found
        assert "sessions belong in the working tree" in found["summary"]
        assert "fix" not in found
    assert _same(_membership(sup / "sub").root, sup / "sub")
    assert _same(origin.capture(sup / "sub").worktree_root, sup / "sub")


def test_a_git_directory_named_otherwise_joins_no_project_from_inside(
    tmp_path: Path, runs: list[Path | None]
) -> None:
    """Inside a git directory that is not a ``.git`` and whose
    ``core.worktree`` names the folder holding it (``GIT_DIR=x/meta.git
    GIT_WORK_TREE=x git init`` writes one), git roots at that folder.
    The folder holds no ``.git``, so neither reader finds a repository
    there, and inside the git directory membership joins none either: it
    follows a ``core.worktree`` from inside a git directory only where it
    is a ``.git`` naming the folder above, the folder's own repository."""
    tree = checkout(tmp_path / "work" / "x")
    meta = tree / "meta.git"
    (tree / ".git").rename(meta)
    _git(tree, "--git-dir", str(meta), "config", "core.worktree", "..")
    assert _toplevel(tree).returncode != 0
    assert _membership(tree).state == location.OUTSIDE
    for here in (meta, meta / "refs"):
        _git_names(here, tree)
        runs.clear()
        cap = _membership(here)
        assert cap.state == location.OUTSIDE and cap.root is None, cap
        _no_origin(here)
        assert [cwd for cwd in runs if _inside(cwd, tree)] == []
        found = location_audit.gitfile_posture(str(here))
        assert found["status"] == "info", found
        assert "sessions belong in the working tree" in found["summary"]


def test_a_bare_repository_cloned_into_dot_git_is_told_not_warned(
    tmp_path: Path,
) -> None:
    """``git clone --bare <url> proj/.git`` with ``git worktree add main``
    beside it: git reads no working tree in ``proj``, and neither reader
    roots one there; each worktree is its own checkout. The audit warned,
    with a fix to remove ``core.bare`` that would break the layout; it now
    says the sessions belong inside a worktree, and suggests no change."""
    src = checkout(tmp_path / "src" / "app", remote=REMOTE)
    proj = tmp_path / "work" / "proj"
    proj.mkdir(parents=True)
    _git(proj, "clone", "-q", "--bare", str(src), ".git")
    _git(proj, "worktree", "add", "-q", "main", "main")
    assert "must be run in a work tree" in _toplevel(proj).stderr
    _git_names(proj / "main", proj / "main")

    assert _membership(proj).state == location.OUTSIDE
    assert githead.locate(proj) is None
    assert _same(_membership(proj / "main").root, proj / "main")
    found = location_audit.gitfile_posture(str(proj))
    assert found["status"] == "info", found
    assert "sessions belong inside one of the worktrees" in found["summary"]
    assert os.fspath(proj / "main") in found["summary"]
    assert "fix" not in found and "core.bare" not in str(found)
