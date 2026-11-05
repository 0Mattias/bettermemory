"""A capture reads a repository's remotes with git's own parser, and
declines wherever git dies (ENG-207's round 6).

The line reader of remotes that read on past what git refuses
(`gitconfig._config_values`) is gone. Every config file a capture reads
for its remotes is parsed as git 2.54 parses it (`gitconfig.git_entries`,
through `gitconfig.config_tree`), its includes followed as git follows
them, and its remote variables judged as remote.c's reader judges them.
Where git dies (a line its parser refuses, a ``url``, ``insteadOf`` or
``include.path`` with no value, an include it cannot read, a remote
variable remote.c refuses) the capture declines (INDETERMINATE) and the
origin records no repository: never OUTSIDE, and never a key git would
not fetch from. An include under a condition not settled here declines
only where its rule could change what is keyed.

Each case is git's own answer first (``git ls-remote --get-url``, ``git
remote get-url --all``, ``git config``), on both of githead's legs; a
corpus of configs is keyed as git lists its remotes; git's wildmatch is
matched pattern by pattern against git's own ``hasconfig:`` matching; and
how this git reads a legacy remote file and an empty URL is asked of it
(`gitsystem.Sources.legacy`, `gitsystem.Sources.resets`).
"""

from __future__ import annotations

import dataclasses
import errno
import getpass
import inspect
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bettermemory import (
    _caches,
    gitconfig,
    gitfile_rule,
    gitsystem,
    location,
    location_git,
    origin,
)
from bettermemory import _git as git_runner
from bettermemory.location import remote_key

from .test_location_git_config import _aged, _FakeGit, _record
from .test_location_git_include_conditions import (
    _MIRRORED,
    _RULES,
    _append,
    _capture,
    _fetched,
    _git,
    _include,
    _key,
    _keys,
    _repo,
)
from .test_location_git_include_conditions import _neutral as _neutral
from .test_location_git_include_conditions import leg as leg


def _global() -> Path:
    """The global config git reads here (`neutral_git`)."""
    return Path(os.environ["GIT_CONFIG_GLOBAL"])


def _write(repo: Path, where: str, text: str) -> None:
    """`text` into the repository's config (``local``), the global one
    (``global``), or a file the repository's config includes
    (``include``, ``.git/bad.inc``)."""
    config = repo / ".git" / "config"
    if where == "local":
        _append(config, text)
    elif where == "global":
        _append(_global(), text)
    else:
        (repo / ".git" / "bad.inc").write_text(text, encoding="utf-8")
        _append(config, "[include]\n\tpath = bad.inc\n")


def _declines(repo: Path, *, git_dies: bool = True) -> location.Capture:
    """The capture of `repo`, which declines and is not kept; where git
    dies there (`git_dies`) the origin, which asks git, records no
    repository either."""
    cap = _capture(repo, cached=False)
    assert cap.state == location.INDETERMINATE, cap
    assert not cap.remote_keys
    assert _capture(repo) is not _capture(repo)
    if git_dies:
        assert origin.capture(repo).repo is None
    return cap


# ---------------------------------------------------------------------------
# Where git dies, and where it reads on
# ---------------------------------------------------------------------------

#: Each config git 2.54 dies on, and where it is written (`_write`). The
#: reader before this round keyed ``local:<repo>/true`` for the first and
#: read on past the valueless insteadOf, include.path and the bad line.
DIES = {
    "url": ("local", '[remote "origin"]\n\turl\n'),
    "pushurl": ("local", '[remote "origin"]\n\tpushurl\n'),
    "fetch": ("local", '[remote "origin"]\n\tfetch\n'),
    "tagopt": ("local", '[remote "origin"]\n\ttagopt\n'),
    "remote_bool": ("local", '[remote "origin"]\n\tprune = maybe\n'),
    "refspec": ("local", '[remote "origin"]\n\tfetch = refs/heads/*:\n'),
    "pushdefault": ("local", "[remote]\n\tpushDefault\n"),
    "branch_remote": ("local", '[branch "main"]\n\tremote\n'),
    "branch_unnamed": ("local", '[branch ""]\n\tremote = origin\n'),
    "insteadof": ("global", '[url "https://m.invalid/"]\n\tinsteadOf\n'),
    "pushinsteadof": ("local", '[url "https://m.invalid/"]\n\tpushInsteadOf\n'),
    "include_path": ("global", "[include]\n\tpath\n"),
    "include_empty": ("local", '[include]\n\tpath = ""\n'),
    "include_folder": ("local", "[include]\n\tpath = {tmp}\n"),
    "include_no_user": ("local", "[include]\n\tpath = ~bettermemory-no-user/x\n"),
    "include_itself": ("include", "[include]\n\tpath = bad.inc\n"),
    "onbranch_path": ("local", '[includeIf "onbranch:main"]\n\tpath\n'),
    "bad_line": ("global", "[core\n"),
    "bad_escape": ("include", "[x]\n\ty = a\\qb\n"),
    "open_quote": ("include", '[x]\n\ty = "open\n'),
}

#: Configs git reads on past, beside the remote, where it is written.
READS_ON = {
    "include_missing": ("local", "[include]\n\tpath = no-such.inc\n"),
    "include_named": ("local", '[include "x"]\n\tpath\n'),
    "includeif_bare": ("local", "[includeIf]\n\tpath\n"),
    "onbranch_false_path": ("local", '[includeIf "onbranch:other"]\n\tpath\n'),
    "remote_unnamed": ("local", "[remote]\n\turl\n"),
    "url_unnamed": ("global", "[url]\n\tinsteadOf\n"),
    "remote_slash": ("local", '[remote "/x"]\n\turl\n'),
    "tagopt_unknown": ("local", '[remote "origin"]\n\ttagopt = --bogus\n'),
    "mirror_bare": ("local", '[remote "origin"]\n\tmirror\n'),
    "negative_refspec": ("local", '[remote "origin"]\n\tfetch = ^refs/heads/x\n'),
    "unknown_key": ("local", '[remote "origin"]\n\tbettermemory\n'),
}


#: Configs git's reader of promisor remotes (promisor-remote.c) dies on,
#: which ``git status``, ``git fetch`` and ``git remote`` read and ``git
#: ls-remote --get-url`` does not (ENG-207's round 7), and configs it reads
#: on past.
PROMISOR_DIES = {
    "filter": ("local", '[remote "origin"]\n\tpartialCloneFilter\n'),
    "filter_other": ("global", '[remote "x"]\n\tpartialCloneFilter\n'),
    "filter_unnamed": ("local", "[remote]\n\tpartialCloneFilter\n"),
    "filter_empty_name": ("include", '[remote ""]\n\tpartialCloneFilter\n'),
    "promisor": ("local", '[remote "origin"]\n\tpromisor = maybe\n'),
    "promisor_slash": ("include", '[remote "/x"]\n\tpromisor = maybe\n'),
    "promisor_unnamed": ("global", "[remote]\n\tpromisor = maybe\n"),
}
PROMISOR_READS_ON = {
    "filter_slash": ("local", '[remote "/x"]\n\tpartialCloneFilter\n'),
    "filter_empty": ("local", '[remote "origin"]\n\tpartialCloneFilter =\n'),
    "promisor_bare": ("local", '[remote "origin"]\n\tpromisor\n'),
    "promisor_false": ("global", '[remote "origin"]\n\tpromisor = off\n'),
}


@pytest.mark.parametrize("case", [*PROMISOR_DIES, *PROMISOR_READS_ON])
def test_where_git_s_reader_of_promisor_remotes_dies_the_capture_declines(
    tmp_path: Path, case: str
) -> None:
    """``git status`` dies at each of `PROMISOR_DIES` though ``git
    ls-remote --get-url`` reads on, and the capture declines there, as the
    user's own git commands die in that repository; at each of
    `PROMISOR_READS_ON` both read on and the capture keys the URL git
    fetches from. The origin, which asks ``git rev-parse``, still records
    the repository."""
    repo = _repo(tmp_path / "repo")
    dies = case in PROMISOR_DIES
    where, text = (PROMISOR_DIES if dies else PROMISOR_READS_ON)[case]
    _write(repo, where, text)
    assert _fetched(repo) == "gh:acme/lib", "premise: get-url reads on"
    done = _git(repo, "status", "--porcelain", check=False)
    assert (done.returncode != 0) is dies, f"premise: git status ({done.stderr!r})"
    if dies:
        _declines(repo, git_dies=False)
    else:
        assert _keys(repo) == {_key(repo, "gh:acme/lib")}


#: Values of the repository's config only ``git rev-parse`` dies on, of
#: git's default config, which git's reader of remotes reads past, in the
#: config, in the global one and in an include (ENG-207's round 8), and
#: one only the reader of remotes dies on.
REV_PARSE_ONLY = {
    "excludesfile": ("local", "[core]\n\texcludesfile\n"),
    "quotepath": ("local", "[core]\n\tquotepath = maybe\n"),
    "user_name": ("global", "[user]\n\tname\n"),
    "push_default": ("include", "[push]\n\tdefault = bogus\n"),
}


@pytest.mark.parametrize("case", list(REV_PARSE_ONLY))
def test_where_only_rev_parse_dies_the_capture_keys_what_git_remote_lists(
    tmp_path: Path, case: str
) -> None:
    """The capture declines where git's reader of remotes dies, not
    wherever git does: at each of `REV_PARSE_ONLY` ``git rev-parse`` dies
    while ``git remote`` and ``git remote get-url`` read on, and the
    capture reads the repository and keys the URL they print, which the
    docs said it declined at ("where git dies")."""
    repo = _repo(tmp_path / "repo")
    where, text = REV_PARSE_ONLY[case]
    _write(repo, where, text)
    done = _git(repo, "rev-parse", "--show-toplevel", "HEAD", check=False)
    assert done.returncode != 0, "premise: rev-parse dies"
    assert _listed_urls(repo) == {"origin": ["gh:acme/lib"]}, (
        "premise: git remote reads"
    )
    assert _keys(repo) == {_key(repo, "gh:acme/lib")}


def test_where_only_git_s_reader_of_remotes_dies_the_capture_declines(
    tmp_path: Path,
) -> None:
    """A ``url`` with no value: ``git rev-parse`` reads on, ``git remote``
    dies, and the capture declines (INDETERMINATE), with no key."""
    repo = _repo(tmp_path / "repo")
    _write(repo, "local", '[remote "origin"]\n\turl\n')
    assert (
        _git(repo, "rev-parse", "--show-toplevel", "HEAD", check=False).returncode == 0
    )
    assert _git(repo, "remote", check=False).returncode != 0, "premise: git remote dies"
    _declines(repo, git_dies=False)


@pytest.mark.parametrize("case", list(DIES))
def test_where_git_dies_the_capture_declines(tmp_path: Path, case: str) -> None:
    """git dies at each of `DIES` (``git ls-remote --get-url`` exits
    non-zero), and the capture declines there, with no key and nothing
    kept, and the origin records no repository."""
    repo = _repo(tmp_path / "repo")
    where, text = DIES[case]
    _write(repo, where, text.format(tmp=tmp_path.as_posix()))
    done = _git(repo, "ls-remote", "--get-url", "origin", check=False)
    assert done.returncode != 0, f"premise: git dies ({done.stdout!r})"
    _declines(repo)


def test_a_folder_the_open_finds_nothing_at_is_one_git_dies_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows CI (ENG-207's round 8): an empty ``include.path`` names the
    including file's folder with a trailing separator, which Windows'
    open answers as missing and its ``_waccess`` finds, so Git for Windows
    goes on to open it and dies (``git ls-remote`` exits non-zero there,
    `test_where_git_dies_the_capture_declines`), where the capture read
    past it as a missing file and keyed the remote. git asks ``access``
    before it opens a config file (``access_or_die``), and the read asks
    it too where the open finds nothing (`gitconfig._accessible`):
    Windows' answer is played here, and the capture declines."""
    repo = _repo(tmp_path / "repo")
    _write(repo, "local", DIES["include_empty"][1])
    real = os.open

    def windows_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str) and path.endswith(os.sep) and os.path.isdir(path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", windows_open)
    _declines(repo)


@pytest.mark.parametrize("case", list(READS_ON))
def test_where_git_reads_on_the_capture_keys_what_git_fetches(
    tmp_path: Path, case: str
) -> None:
    """git reads on past each of `READS_ON`, and the capture keys the URL
    git fetches from."""
    repo = _repo(tmp_path / "repo")
    where, text = READS_ON[case]
    _write(repo, where, text)
    assert _fetched(repo) == "gh:acme/lib", "premise: git reads on"
    assert _keys(repo) == {_key(repo, "gh:acme/lib")}


@pytest.mark.parametrize("depth", [10, 11])
def test_includes_are_followed_to_git_s_depth(tmp_path: Path, depth: int) -> None:
    """git follows ten nested includes and dies at the eleventh: a chain
    of `depth` files below the repository's config, the last setting the
    remote's URL."""
    repo = _repo(tmp_path / "repo", remote=None)
    for i in range(1, depth + 1):
        last = '[remote "origin"]\n\turl = https://example.invalid/acme/deep.git\n'
        nxt = f"[include]\n\tpath = f{i + 1}.inc\n" if i < depth else last
        (repo / ".git" / f"f{i}.inc").write_text(nxt, encoding="utf-8")
    _append(repo / ".git" / "config", "[include]\n\tpath = f1.inc\n")
    done = _git(repo, "ls-remote", "--get-url", "origin", check=False)
    if depth == 10:
        assert done.stdout.strip() == "https://example.invalid/acme/deep.git"
        assert _keys(repo) == {"example.invalid/acme/deep"}
    else:
        assert done.returncode != 0, "premise: git dies past its depth"
        _declines(repo)


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="a mode that keeps a reader out",
)
@pytest.mark.parametrize("where", ["global", "system", "include"])
def test_a_config_that_cannot_be_read_is_read_as_git_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """git skips a global config it may not read (``ACCESS_EACCES_OK``),
    and dies at a system config or an include it may not read: the
    capture reads on past the first, and declines at the others."""
    repo = _repo(tmp_path / "repo")
    locked = tmp_path / "locked"
    locked.write_text(_RULES, encoding="utf-8")
    locked.chmod(0)
    try:
        if where == "global":
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(locked))
        elif where == "system":
            monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
            monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(locked))
        else:
            _append(
                repo / ".git" / "config", f"[include]\n\tpath = {locked.as_posix()}\n"
            )
        done = _git(repo, "ls-remote", "--get-url", "origin", check=False)
        if where == "global":
            assert done.stdout.strip() == "gh:acme/lib", "premise: git skips it"
            assert _keys(repo) == {_key(repo, "gh:acme/lib")}
        else:
            assert done.returncode != 0, "premise: git dies"
            _declines(repo)
    finally:
        locked.chmod(0o600)


def test_git_for_windows_names_no_user_s_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's round 7: Git for Windows' ``getpwnam`` is a stub that
    names no account, so an include of ``~<user>/x`` dies there and a
    ``gitdir:~<user>/`` pattern is matched as written; this reader read
    both as not settled. With the platform read as Windows, an include of
    this account's ``~<name>/`` dies, and the pattern holds for a git
    directory under a folder named ``~<name>`` (git's literal match)."""
    user = getpass.getuser()
    rules = tmp_path / "x.inc"
    rules.write_text("[t]\n\tv = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    config = tmp_path / "config"
    config.write_text(f"[include]\n\tpath = ~{user}/x.inc\n", encoding="utf-8")
    read = gitconfig.config_tree(str(config))
    assert read.dies is not None and "cannot expand" in read.dies
    git_dir = tmp_path / f"~{user}" / "repo" / ".git"
    git_dir.mkdir(parents=True)
    config.write_text(
        f'[includeIf "gitdir:~{user}/"]\n\tpath = {rules.as_posix()}\n',
        encoding="utf-8",
    )
    read = gitconfig.config_tree(str(config), git_dir=str(git_dir))
    assert read.complete and read.dies is None and not read.undecided
    assert read[("t", None, "v")] == [b"1"]


@pytest.mark.skipif(sys.platform != "win32", reason="Git for Windows' getpwnam")
def test_an_include_of_a_user_s_home_dies_under_git_for_windows(tmp_path: Path) -> None:
    """The same, against Git for Windows itself: an include of this
    account's ``~<name>/x`` makes git die, and the capture declines."""
    repo = _repo(tmp_path / "repo")
    _write(repo, "local", f"[include]\n\tpath = ~{getpass.getuser()}/x\n")
    done = _git(repo, "ls-remote", "--get-url", "origin", check=False)
    assert done.returncode != 0, "premise: git dies"
    _declines(repo)


@pytest.mark.parametrize(
    "tail",
    [b"[core]\n\tx = \\q\n", b"[core]\n\tworktree =\n"],
    ids=["bad_line", "empty_worktree"],
)
def test_a_git_directory_the_environment_names_and_git_dies_on_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: bytes
) -> None:
    """GIT_DIR naming a git directory whose config git dies on (a line its
    parser refuses, an empty ``core.worktree``): git reads no repository
    and names no folder outside one, and the capture declines where it
    read the folder as outside a repository (`location_git._from_env`)."""
    repo = _repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    config.write_bytes(config.read_bytes() + tail)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    done = _git(repo, "rev-parse", "--show-toplevel", check=False)
    assert done.returncode != 0, "premise: git dies"
    cap = _declines(repo)
    assert cap.detail.startswith("repository format: ")


# ---------------------------------------------------------------------------
# A corpus keyed as git lists its remotes
# ---------------------------------------------------------------------------

#: Configs a repository with no remote takes on, each with the bytes of a
#: file its config includes (``.git/extra.inc``), or none.
CORPUS: dict[str, tuple[str, bytes | None]] = {
    "plain": ('[remote "origin"]\n\turl = https://example.invalid/a/plain.git\n', None),
    "quoted": (
        '[remote "origin"]\n\turl = "https://example.invalid/a/q t.git"\n',
        None,
    ),
    "escapes": (
        '[remote "origin"]\n\turl = https://example.invalid/a/e\\"s\\\\c\\tx.git\n',
        None,
    ),
    "comments": (
        '# one\n; two\n[remote "origin"] # three\n'
        "\turl = https://example.invalid/a/c.git ; four\n",
        None,
    ),
    "hash_quoted": (
        '[remote "origin"]\n\turl = "https://example.invalid/a/h#x" # c\n',
        None,
    ),
    "continued": (
        '[remote "origin"]\n\turl = https://example.invalid/a/con\\\ntinued\n',
        None,
    ),
    "spaces": (
        '[remote "origin"]\n\turl   =   https://example.invalid/a/i  n   \n',
        None,
    ),
    "cases": ('[REMOTE "Origin"]\n\tURL = https://example.invalid/a/upper.git\n', None),
    "old_style": ("[remote.Legacy]\n\turl = https://example.invalid/a/old.git\n", None),
    "dotted": ('[remote "a.b"]\n\turl = https://example.invalid/a/dotted.git\n', None),
    "sub_escape": ('[remote "x\\y"]\n\turl = https://example.invalid/a/bs.git\n', None),
    "many": (
        '[remote "origin"]\n\turl = https://example.invalid/a/one.git\n'
        "\turl = https://example.invalid/a/two.git\n",
        None,
    ),
    "reset": (
        '[remote "origin"]\n\turl = https://example.invalid/a/gone.git\n\turl =\n'
        "\turl = https://example.invalid/a/kept.git\n",
        None,
    ),
    "reset_quoted": (
        '[remote "origin"]\n\turl = https://example.invalid/a/gone.git\n\turl = ""\n'
        '[remote "up"]\n\turl = https://example.invalid/a/up.git\n',
        None,
    ),
    "two_sections": (
        '[remote "origin"]\n\turl = https://example.invalid/a/first.git\n'
        '[core]\n\tx = 1\n[remote "origin"]\n\turl = https://example.invalid/a/second\n',
        None,
    ),
    "crlf": (
        '[remote "origin"]\r\n\turl = https://example.invalid/a/crlf.git\r\n',
        None,
    ),
    "inline": ('[remote "origin"] url = https://example.invalid/a/inline.git\n', None),
    "unended": ('[remote "origin"]\n\turl = https://example.invalid/a/end.git', None),
    "bom_include": (
        "[include]\n\tpath = extra.inc\n",
        b'\xef\xbb\xbf[remote "bom"]\n\turl = https://example.invalid/a/bom.git\n',
    ),
    "include_order": (
        '[remote "origin"]\n\turl = https://example.invalid/a/before.git\n'
        "[include]\n\tpath = extra.inc\n"
        '[remote "origin"]\n\turl = https://example.invalid/a/after.git\n',
        b'[remote "origin"]\n\turl =\n\turl = https://example.invalid/a/inside.git\n',
    ),
}


def _listed_urls(repo: Path) -> dict[str, list[str]]:
    """{remote: its URLs} as git names them (``git remote``, ``git remote
    get-url --all``), both answering. A remote left with no URL is named
    by git as its own URL, which a capture does not key (the ruling on
    git's fallback to a name), and is left out."""
    names = _git(repo, "remote").stdout.splitlines()
    out: dict[str, list[str]] = {}
    for name in names:
        urls = _git(repo, "remote", "get-url", "--all", name).stdout.splitlines()
        out[name] = [u for u in urls if u != name]
    return out


@pytest.mark.parametrize("case", list(CORPUS))
def test_a_corpus_of_configs_is_keyed_as_git_lists_its_remotes(
    tmp_path: Path, case: str
) -> None:
    """Each config of `CORPUS` is keyed as git lists its remotes: the
    capture's keys are the keys of every URL ``git remote get-url --all``
    gives for every remote ``git remote`` names, quoting, escapes,
    comments, continued lines, case, old-style sections, an empty URL,
    CRLF, a byte order mark and includes read as git reads them."""
    repo = _repo(tmp_path / "repo", remote=None)
    text, included = CORPUS[case]
    if included is not None:
        (repo / ".git" / "extra.inc").write_bytes(included)
    with (repo / ".git" / "config").open("ab") as fh:
        fh.write(text.encode("utf-8"))
    listed = _listed_urls(repo)
    assert listed, "premise: git names a remote"
    base = os.path.realpath(repo)
    wanted = {remote_key(u, base) for urls in listed.values() for u in urls} - {None}
    assert _keys(repo) == wanted, listed


# ---------------------------------------------------------------------------
# git's wildmatch
# ---------------------------------------------------------------------------

#: Remote URLs a pattern is matched against.
URLS = [
    "gh:acme/lib",
    "https://example.invalid/Acme/Lib9.git",
    "a]b",
    "a-b/c",
    "x\\y",
    "xy",
    "[ab]",
    "a b",
    "*star",
    "UP/low",
    "a.b,c;d",
]

#: Patterns as git reads them, after the subsection's own escapes.
PATTERNS = [
    "gh:*",
    "gh:**",
    "gh:acme/*",
    "gh:*/lib",
    "gh:**/lib",
    "**/lib",
    "**",
    "*",
    "?h:acme/lib",
    "gh:acme?lib",
    "https://**/*.git",
    "https://*/*/*.git",
    "HTTPS://**",
    "a/**",
    "**/c",
    "a*/c",
    "a**/c",
    "*/c",
    "[[:alpha:]]h:**",
    "[[:alpha:]][[:alpha:]]:acme/lib",
    "[[:digit:]]*",
    "*[[:digit:]].git",
    "[[:upper:]]*",
    "*[[:upper:]]*",
    "[[:lower:]]*",
    "[![:lower:]]*",
    "[^[:lower:]]*",
    "*[[:space:]]*",
    "*[[:blank:]]*",
    "*[[:punct:]]*",
    "[[:punct:]]*",
    "*[[:xdigit:]]",
    "[[:alnum:]]*",
    "*[[:cntrl:]]*",
    "*[[:graph:]]",
    "*[[:print:]]",
    "[[:upper:][:digit:]]*",
    "*[[:digit:][:upper:]]*",
    "[[:alpha:]-z]*",
    "[a-[:alpha:]]*",
    "[[:foo:]]*",
    "[[:alpha:]*",
    "[[:alpha]]*",
    "[a-]*",
    "[]a]*",
    "[!]]*",
    "a[]-]b",
    "*]*",
    "a\\]b",
    "a[\\]]b",
    "\\[ab]",
    "[ab]",
    "\\*star",
    "\\*s*",
    "*\\",
    "x\\\\y",
    "x\\y",
    "[z-a]*",
    "[a-c-e]*",
    "a?b",
    "a.b,c;d",
    "a.b,c[;]d",
    "UP/[[:lower:]]ow",
]


def _escaped(text: str) -> str:
    """`text` as a quoted subsection or value spells it."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def test_wildmatch_matches_as_git_matches(tmp_path: Path) -> None:
    """`gitconfig.wildmatch` against git's own matching of
    ``hasconfig:remote.*.url:`` (``WM_PATHNAME``): each of `PATTERNS`
    includes a file that sets a variable of its own, a repository for
    each of `URLS` sets that URL, and the variables git then lists are the
    patterns git matched. The classes, a backslash inside and outside a
    bracket, an unknown class, an unclosed bracket and a ``**`` that is
    not a whole path component each match as git matches them."""
    for i in range(len(PATTERNS)):
        (tmp_path / f"k{i}.inc").write_text(f"[bm]\n\tk{i} = 1\n", encoding="utf-8")
    wrong = []
    matched = 0
    for j, url in enumerate(URLS):
        repo = tmp_path / f"r{j}"
        repo.mkdir()
        _git(repo, "init", "-q")
        _append(
            repo / ".git" / "config",
            f'[remote "origin"]\n\turl = "{_escaped(url)}"\n'
            + "".join(
                f'[includeIf "hasconfig:remote.*.url:{_escaped(p)}"]\n'
                f"\tpath = {(tmp_path / f'k{i}.inc').as_posix()}\n"
                for i, p in enumerate(PATTERNS)
            ),
        )
        listed = _git(repo, "config", "--get-regexp", r"^bm\.", check=False)
        assert listed.returncode in (0, 1), listed.stderr
        hits = {line.split(" ", 1)[0] for line in listed.stdout.splitlines()}
        matched += len(hits)
        for i, pattern in enumerate(PATTERNS):
            git_says = f"bm.k{i}" in hits
            if gitconfig.wildmatch(pattern.encode(), url.encode()) is not git_says:
                wrong.append((pattern, url, git_says))
    assert wrong == []
    assert 0 < matched < len(PATTERNS) * len(URLS), "premise: git matches some"


def test_wildmatch_is_bounded_where_git_s_is_not() -> None:
    """ENG-207's round 7: the port recursed two Python frames a star and
    walked unbounded, so a pattern of 650 ``*a`` raised RecursionError
    and ``**/`` repeated 20 times took 3.2 s where git takes 61 ms. Past
    `gitconfig._WM_DEPTH` nested stars, past Python's own stack, or past
    the steps a read has left (shared by every match of one read), it now
    says it is spent (`gitconfig.WildmatchSpent`), and a real pattern
    takes a few dozen steps."""
    with pytest.raises(gitconfig.WildmatchSpent):
        gitconfig.wildmatch(b"*a" * 650, b"a" * 700)
    assert gitconfig.wildmatch(b"*a" * 140, b"a" * 700)
    started = time.perf_counter()
    with pytest.raises(gitconfig.WildmatchSpent):
        gitconfig.wildmatch(b"**/" * 24 + b"z", b"a/" * 24 + b"y")
    assert time.perf_counter() - started < 1.0
    steps = gitconfig.Steps()
    assert gitconfig.wildmatch(
        b"https://**/*.git", b"https://x.invalid/a/b.git", steps=steps
    )
    assert gitconfig._WM_STEPS - steps.left < 50
    with pytest.raises(gitconfig.WildmatchSpent):
        gitconfig.wildmatch(
            b"*[" + b"b" * gitconfig._WM_STEPS + b"]", b"a", steps=steps
        )
    with pytest.raises(gitconfig.WildmatchSpent):
        gitconfig.wildmatch(b"gh:*", b"gh:acme/lib", steps=steps)

    def deep(levels: int) -> bool:
        return (
            deep(levels - 1) if levels else gitconfig.wildmatch(b"*a" * 100, b"a" * 101)
        )

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 150)
    try:
        with pytest.raises(gitconfig.WildmatchSpent):
            deep(40)
    finally:
        sys.setrecursionlimit(limit)


#: The reviewer's deep pattern: 650 ``*a`` after a URL's start, which git
#: matches against a 700-character URL.
_DEEP = "https://x.invalid/" + "*a" * 650
_LONG_URL = "https://x.invalid/" + "a" * 700


@pytest.mark.parametrize("matters", [True, False], ids=["rule_matches", "neutral"])
def test_a_pattern_git_settles_and_the_port_does_not_reads_as_unsettled(
    tmp_path: Path, matters: bool
) -> None:
    """ENG-207's round 7: a global ``hasconfig:remote.*.url:`` include
    whose pattern nests 650 stars raised RecursionError out of
    `location.capture`; git answers it (the include holds). The condition
    now reads as not settled, and the capture declines where the include
    could change what is keyed (its rule matches the URL) and keys git's
    URL where it could not."""
    repo = _repo(tmp_path / "repo", remote=_LONG_URL)
    rules = tmp_path / "rules.inc"
    prefix = "https://x.invalid/" if matters else "zz:"
    rules.write_text(
        f'[url "https://m.invalid/"]\n\tinsteadOf = {prefix}\n', encoding="utf-8"
    )
    _include(_global(), f"hasconfig:remote.*.url:{_DEEP}", rules)
    fetched = "https://m.invalid/" + "a" * 700 if matters else _LONG_URL
    assert _fetched(repo) == fetched, "premise: git settles the include, which holds"
    if matters:
        _declines(repo, git_dies=False)
    else:
        assert _keys(repo) == {_key(repo, _LONG_URL)}


@pytest.mark.parametrize("matters", [True, False], ids=["rule_matches", "neutral"])
def test_a_slow_pattern_is_given_up_within_a_bound(
    tmp_path: Path, matters: bool
) -> None:
    """ENG-207's round 7: ``**/`` repeated 22 times, under ``onbranch:``
    against a branch of 22 folders, took the port about 10 s, and git a
    quarter of one (it does not match). The read's matching gives up
    within its steps (`gitconfig._WM_STEPS`, about 40 ms), well inside a
    second for the capture's every read, and the condition reads as not
    settled: the capture declines where the include could change what is
    keyed and keys git's URL where it could not."""
    repo = _repo(tmp_path / "repo")
    _git(repo, "switch", "-q", "-c", "a/" * 22 + "y")
    rules = tmp_path / "rules.inc"
    rules.write_text(
        _RULES if matters else _RULES.replace("gh:", "zz:"), encoding="utf-8"
    )
    _include(_global(), "onbranch:" + "**/" * 22 + "z", rules)
    assert _fetched(repo) == "gh:acme/lib", (
        "premise: git settles it, and it does not hold"
    )
    started = time.perf_counter()
    if matters:
        _declines(repo, git_dies=False)
    else:
        assert _keys(repo) == {_key(repo, "gh:acme/lib")}
    assert time.perf_counter() - started < 2.0


# ---------------------------------------------------------------------------
# An include under a condition: the reviewer's blocker, and an unsettled one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["gh:", "zz:"])
@pytest.mark.parametrize(
    ("condition", "branch"),
    [
        ("onbranch:release/[[:digit:]]*", "main"),
        ("onbranch:release/[[:digit:]]*", "release/1x"),
        ("onbranch:release/*", "main"),
        ("onbranch:release/*", "release/x"),
        ("hasconfig:remote.*.url:gh\\\\:acme/*", "main"),
        ("hasconfig:remote.*.url:gh\\\\:nothing/*", "main"),
    ],
)
@pytest.mark.parametrize("backend", ["files", "reftable"])
def test_a_global_include_is_read_as_git_reads_it_on_any_ref_backend(
    tmp_path: Path, backend: str, condition: str, branch: str, prefix: str
) -> None:
    """The reviewer's blocker at 7054bb06: a global include under a
    pattern with a class, a hasconfig pattern with a backslash, or any
    ``onbranch:`` in a reftable repository was not settled here, and its
    insteadOf rule had every capture decline, though it matched none of
    the repository's URLs (``zz:``). Each is now settled as git settles it
    (`gitconfig.wildmatch`; a reftable HEAD asked of ``git symbolic-ref``):
    the capture keys the URL git fetches from, rewritten exactly where
    git's is, whether the rule's prefix matches the URL (``gh:``) or not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    made = _git(
        repo, "init", "-q", "-b", "main", f"--ref-format={backend}", check=False
    )
    if made.returncode != 0:
        pytest.skip(f"this git makes no {backend} repository")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    if branch != "main":
        _git(repo, "checkout", "-q", "-b", branch)
    _git(repo, "remote", "add", "origin", "gh:acme/lib")
    rules = tmp_path / "rules.inc"
    rules.write_text(
        f'[url "https://mirror.invalid/"]\n\tinsteadOf = {prefix}\n', encoding="utf-8"
    )
    _include(_global(), condition, rules)
    fetched = _fetched(repo)
    keys = _keys(repo)
    assert _key(repo, fetched) in keys
    assert (_key(repo, _MIRRORED) in keys) is (fetched == _MIRRORED)
    assert keys <= {_key(repo, "gh:acme/lib"), _key(repo, _MIRRORED)}


def _prefix_unsaid(monkeypatch: pytest.MonkeyPatch) -> str:
    """A ``gitdir:%(prefix)/`` condition git settles (it does not hold)
    and the capture does not: git is taken as not having said what it
    expands ``%(prefix)`` to (`gitsystem.Sources.prefix`)."""
    real = gitsystem.sources
    monkeypatch.setattr(
        gitsystem, "sources", lambda **kw: dataclasses.replace(real(**kw), prefix=None)
    )
    return "gitdir:%(prefix)/bettermemory-nowhere/"


@pytest.mark.parametrize("unsettled", ["prefix", "branch"])
@pytest.mark.parametrize(
    ("text", "declines"),
    [
        ('[url "https://mirror.invalid/"]\n\tinsteadOf = gh:\n', True),
        ('[url "https://mirror.invalid/"]\n\tinsteadOf = zz:\n', False),
        ('[url "https://mirror.invalid/"]\n\tinsteadOf = gh:acme/lib/x\n', False),
        ('[remote "other"]\n\turl = https://example.invalid/other.git\n', True),
        ("[core\n", True),
        ("[user]\n\tname = Fixture\n", False),
    ],
    ids=["matching_rule", "other_rule", "longer_rule", "remote_url", "dies", "neutral"],
)
def test_an_unsettled_include_declines_only_where_it_could_change_the_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    declines: bool,
    unsettled: str,
) -> None:
    """ENG-207's round 6: an include under a condition not settled here
    (a ``%(prefix)`` path where git has not said what it expands that to,
    an ``onbranch:`` where git does not answer for HEAD) has the capture
    decline where what it reads could change the URLs keyed: an insteadOf
    rule whose prefix matches one, a remote URL, or a line git may die on.
    A rule that matches none of them, or a file that sets neither, leaves
    the capture as git reads it."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(text, encoding="utf-8")
    condition = (
        _prefix_unsaid(monkeypatch)
        if unsettled == "prefix"
        else _unsettled(monkeypatch, holds=False)
    )
    _include(_global(), condition, rules)
    assert _fetched(repo) == "gh:acme/lib", "premise: git reads no such include"
    if declines:
        _declines(repo, git_dies=False)
    else:
        assert _keys(repo) == {_key(repo, "gh:acme/lib")}


def _prefix() -> str:
    """The directory the git on PATH expands ``%(prefix)`` to, as
    `gitsystem` asked it, checked against git's own expansion of a path
    (``git config --type=path``)."""
    found = gitsystem.sources().prefix
    assert found is not None, "premise: git says"
    probe = gitsystem._probe_repository()
    assert probe is not None
    asked = _git(
        Path(probe[0]), "config", "--type=path", "--get", "bettermemory.prefix"
    )
    assert asked.stdout.rstrip("\r\n") == found + "/z"
    return found


@pytest.mark.parametrize(
    "case",
    [
        "include_relative",
        "include_absolute",
        "include_literal",
        "gitdir_holds",
        "gitdir_not",
    ],
)
def test_a_prefix_path_is_read_as_this_git_expands_it(
    tmp_path: Path, case: str
) -> None:
    """ENG-207's round 7: a ``%(prefix)/`` path was not settled here, and
    an include naming one declined the capture where its rule matched a
    URL. It is now git's ``system_path``: an absolute rest as it is (the
    reviewer's ``gitdir:%(prefix)//<git dir>/``, which git takes as
    holding, and an include of ``%(prefix)//<file>``), a relative one
    after the directory the git on PATH expands ``%(prefix)`` to, asked
    of git (`gitsystem.Sources.prefix`; here an include climbing from it
    to a file), and a ``%(prefix)`` with no ``/`` a file of that name;
    a ``gitdir:`` pattern under the prefix that names no git directory
    does not hold. Each is checked against the URL git fetches."""
    repo = _repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    if case == "include_relative":
        prefix = os.path.realpath(_prefix())
        try:
            climb = os.path.relpath(rules, prefix)
        except ValueError:
            pytest.skip("git's prefix is on another drive")
        _append(config, f"[include]\n\tpath = %(prefix)/{Path(climb).as_posix()}\n")
    elif case == "include_absolute":
        _append(config, f"[include]\n\tpath = %(prefix)/{rules.as_posix()}\n")
    elif case == "include_literal":
        (repo / ".git" / "%(prefix)").write_text(_RULES, encoding="utf-8")
        _append(config, "[include]\n\tpath = %(prefix)\n")
    else:
        real = Path(os.path.realpath(repo)).as_posix()
        where = real if case == "gitdir_holds" else "bettermemory-nowhere"
        _include(_global(), f"gitdir:%(prefix)/{where}/", rules)
    fetched = "gh:acme/lib" if case == "gitdir_not" else _MIRRORED
    assert _fetched(repo) == fetched, "premise: git's reading"
    keys = _keys(repo)
    assert _key(repo, fetched) in keys
    assert keys <= {_key(repo, "gh:acme/lib"), _key(repo, _MIRRORED)}


def test_a_git_that_leaves_prefix_as_written_is_read_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git that does not expand ``%(prefix)/`` prints the probe's path
    as written (`gitconfig.AS_WRITTEN`), and its include of
    ``%(prefix)/x`` is the file ``%(prefix)/x`` beside the including
    config, an absolute rest too: no file on POSIX, which git reads on
    past; on Windows a path holding a drive's colon past its start, a
    name Windows refuses (ERROR_INVALID_NAME), which Git for Windows'
    ``access`` (``_waccess``) answers EINVAL for and ``access_or_die``
    dies at, as the read does (ENG-207's round 8, Windows CI: the test
    took POSIX's answer there)."""
    real = gitsystem.sources
    monkeypatch.setattr(
        gitsystem,
        "sources",
        lambda **kw: dataclasses.replace(real(**kw), prefix=gitconfig.AS_WRITTEN),
    )
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "%(prefix)").mkdir()
    (repo / ".git" / "%(prefix)" / "x").write_text(_RULES, encoding="utf-8")
    _append(repo / ".git" / "config", "[include]\n\tpath = %(prefix)/x\n")
    assert _key(repo, _MIRRORED) in _keys(repo)
    _caches.clear_all()
    (repo / ".git" / "%(prefix)" / "x").unlink()
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _append(
        repo / ".git" / "config", f"[include]\n\tpath = %(prefix)/{rules.as_posix()}\n"
    )
    if os.name == "nt":
        # The git on PATH expands %(prefix) and reads the file: the origin,
        # which asks it, records the repository.
        _declines(repo, git_dies=False)
    else:
        assert _key(repo, _MIRRORED) not in _keys(repo)


def _windows_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gitconfig` reading paths as on Windows: `ntpath`, its separators
    and ``os.name``, the rest of `os` as it is."""
    import ntpath
    import types

    shim = types.SimpleNamespace(**{**vars(os), "name": "nt", "path": ntpath})
    shim.sep, shim.altsep = ntpath.sep, ntpath.altsep
    monkeypatch.setattr(gitconfig, "os", shim)


@pytest.mark.parametrize(
    ("prefix", "target"),
    [
        ("C:/Program Files/Git", "C:\\x\\rules.inc"),
        (gitconfig.AS_WRITTEN, "C:\\r\\.git\\%(prefix)\\C:\\x\\rules.inc"),
        (None, None),
    ],
    ids=["expanded", "as_written", "unsaid"],
)
def test_a_prefix_include_of_a_drive_s_path_is_joined_as_git_for_windows_joins_it(
    monkeypatch: pytest.MonkeyPatch, prefix: str | None, target: str | None
) -> None:
    """Git for Windows' ``%(prefix)/C:/x``, read as on Windows (`ntpath`):
    expanded, it is ``system_path("C:/x")``, which takes a path with a
    drive as it is (``is_absolute_path``; the git on CI's Windows leg
    reads it so, `test_a_prefix_path_is_read_as_this_git_expands_it`'s
    ``include_absolute``); left as written, by a git before 2.35, it is a
    relative path, joined under the including file's folder as
    ``handle_path_include`` joins one, a drive's colon inside it; where
    git has not said, nothing is read. ENG-207's round 8."""
    _windows_os(monkeypatch)
    value = b"%(prefix)/C:/x/rules.inc"
    if target is None:
        with pytest.raises(gitconfig._Overflow):
            gitconfig._include_target(value, "C:\\r\\.git\\config", lambda: prefix)
        return
    got = gitconfig._include_target(value, "C:\\r\\.git\\config", lambda: prefix)
    assert got == target


def _unsettled(monkeypatch: pytest.MonkeyPatch, *, holds: bool) -> str:
    """An ``onbranch:`` condition git settles, holding on the repository's
    branch (``main``) where `holds`, that the capture reads as not
    settled: git is taken as not answering for HEAD's branch in time
    (`location_git._asked_branch`)."""
    monkeypatch.setattr(location_git, "_head_branch", lambda *_a: gitconfig.UNSETTLED)
    return "onbranch:main" if holds else "onbranch:other"


#: Two bases whose ``gh:`` prefixes tie, in either order.
_TIED = {
    "c_first": '[url "https://C.invalid/"]\n\tinsteadOf = gh:\n'
    '[url "https://B.invalid/"]\n\tinsteadOf = gh:\n',
    "b_first": '[url "https://B.invalid/"]\n\tinsteadOf = gh:\n'
    '[url "https://C.invalid/"]\n\tinsteadOf = gh:\n',
}


@pytest.mark.parametrize("holds", [True, False], ids=["holds", "does_not_hold"])
@pytest.mark.parametrize("order", list(_TIED))
def test_an_unsettled_include_naming_a_tied_base_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str, holds: bool
) -> None:
    """ENG-207's round 7: git ranks insteadOf bases in the order it first
    meets them, so an include under a condition not settled here that
    names base B, with a rule matching nothing, has git meet B first where
    it holds, and take B's ``gh:`` over C's though the config lists C's
    first. The capture declines where the include could change the rule
    taken (B ties the rule that rewrites the URL, under another base), and
    keys what git fetches where B is the base taken either way. It keyed
    C's URL, which git does not fetch."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(
        '[url "https://B.invalid/"]\n\tinsteadOf = zz-nomatch:\n', encoding="utf-8"
    )
    _include(_global(), _unsettled(monkeypatch, holds=holds), rules)
    _append(_global(), _TIED[order])
    first = "C" if order == "c_first" else "B"
    fetched = f"https://{'B' if holds else first}.invalid/acme/lib"
    assert _fetched(repo) == fetched, "premise: git's rewrite"
    if order == "c_first":
        _declines(repo, git_dies=False)
    else:
        keys = _keys(repo)
        assert _key(repo, fetched) in keys
        assert _key(repo, "https://C.invalid/acme/lib") not in keys


# ---------------------------------------------------------------------------
# How this git reads a remote (gitsystem)
# ---------------------------------------------------------------------------


def test_how_git_reads_a_remote_is_asked_of_git_and_recorded() -> None:
    """The WITH_BREAKING_CHANGES ruling: whether the git on PATH reads the
    legacy ``remotes/`` and ``branches/`` files, and whether an empty URL
    empties a remote's list, is git's answer to ``ls-remote --get-url``
    in the probe's git directory (`gitsystem._probe_repository`), kept in
    the state directory's record with the rest. git 2.54 as built here
    reads both files and empties the list."""
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    found = gitsystem.sources()
    probe = gitsystem._probe_repository()
    assert probe is not None
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    asked = subprocess.run(
        ["git", f"--git-dir={probe[0]}", *gitsystem._REMOTE_ASK],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    said = gitsystem._REMOTE_ANSWERS[asked.stdout.strip()]
    assert (found.legacy, found.resets) == said
    entry = _record(state)["entries"][0]
    assert (entry["legacy"], entry["resets"]) == said
    assert entry["prefix"] == found.prefix == _prefix()
    assert _record(state)["format"] == gitsystem.RECORD_FORMAT == 4


def _compat_repository(path: Path, compat: str) -> bool:
    """A bare repository of git's own making at `path`, of format 1 and
    object format sha1, its ``compatObjectFormat`` set to `compat` by
    ``git config``: whether git reads it (``git rev-parse --git-dir``
    exits 0)."""
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.compatObjectFormat", compat),
    ):
        subprocess.run(["git", f"--git-dir={path}", "config", key, value], check=True)
    done = subprocess.run(
        ["git", f"--git-dir={path}", "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def test_whether_git_reads_a_compatibility_object_format_is_git_s_answer(
    tmp_path: Path,
) -> None:
    """ENG-207's round 8: whether the git on PATH reads a repository of
    format 1 and object format sha1 that sets ``compatObjectFormat`` to
    sha256 (`Sources.compat_differ`) is what git does with such a
    repository of its own making. git 2.54 built without Rust dies on it;
    git 2.56 built with it reads it."""
    git_reads = _compat_repository(tmp_path / "bare.git", "sha256")
    found = gitsystem.sources()
    assert found.compat_differ is git_reads
    assert gitsystem.reads_compat() is git_reads
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    entry = _record(state)["entries"][0]
    assert entry["compat_differ"] is git_reads


def test_a_compatibility_object_format_the_same_is_read_as_git_dying_unasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review of round 8: no git reads a repository whose
    ``compatObjectFormat`` is its object format (git 2.44 and earlier know
    no such extension, a build without Rust dies on any, and every other
    stops at repository.c's ``BUG()``, which aborts), so the format is
    read as one git dies on, and git is not asked: asking it aborted a git
    at each ask, a crash report or a core each time. The premise is the
    git on PATH, which dies on such a repository of its own making."""
    assert not _compat_repository(tmp_path / "bare.git", "sha1")

    def asked() -> bool | None:
        raise AssertionError("git asked of a pairing no git reads")

    monkeypatch.setattr(gitsystem, "reads_compat", asked)
    for version in (b"1", b"-2"):
        for name in (b"sha1", b"sha256"):
            data = (
                b"[core]\n\trepositoryformatversion = " + version + b"\n"
                b"[extensions]\n\tobjectFormat = " + name + b"\n"
                b"\tcompatObjectFormat = " + name + b"\n"
            )
            assert gitfile_rule.object_format(data) is None, (version, name)
    implied = b"[core]\n\trepositoryformatversion = 1\n"
    implied += b"[extensions]\n\tcompatObjectFormat = sha1\n"
    assert gitfile_rule.object_format(implied) is None


def _pairing(config: bytes) -> tuple[bytes, bytes | None]:
    """The object format and the compatibility object format (None where
    unset) a probe's config sets, by its ``key = value`` lines, the last
    of each standing as git takes it; sha1 where no object format is
    set."""
    found: dict[bytes, bytes] = {}
    for line in config.splitlines():
        key, eq, value = line.strip().partition(b"=")
        if eq:
            found[key.strip().lower()] = value.strip()
    return found.get(b"objectformat", b"sha1"), found.get(b"compatobjectformat")


def test_no_probe_ask_runs_git_where_the_compat_format_is_the_object_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENG-207's review of round 8: git asked in a git directory whose
    ``compatObjectFormat`` is its object format aborts (a ``BUG()`` in a
    build with Rust, and in git 2.45 to 2.53), so no git directory the
    probe is built with sets that pairing, and an ask runs git in none:
    each git directory an ask names holds a config whose compatibility
    object format, where it sets one, is another than its object format.
    The probe asked of that pairing once (``compat-same``), every hour
    and every minute while unsaid."""
    monkeypatch.setattr(gitsystem, "ASK_SECONDS", 30.0)
    real = git_runner.run_git
    asked: list[tuple[str, bytes]] = []

    def recording(args: Sequence[str], **kw: Any) -> Any:
        git_dir = kw.get("git_dir")
        if git_dir is not None:
            asked.append((git_dir, Path(git_dir, "config").read_bytes()))
        return real(args, **kw)

    monkeypatch.setattr(git_runner, "run_git", recording)
    answer = gitsystem._ask("key", time.time())
    assert answer.ok and answer.sources.compat_differ is not None, "premise: asked"
    probe = gitsystem._probe_repository()
    assert probe is not None
    compat_dir = os.path.join(probe[0], gitsystem._COMPAT_PROBE)
    assert [d for d, _c in asked].count(compat_dir) == 1
    for git_dir, config in asked:
        main, compat = _pairing(config)
        assert compat is None or compat != main, git_dir
    configs = [
        data
        for name, data in gitsystem._PROBE_FILES.items()
        if name.rsplit("/", 1)[-1] == "config"
    ]
    assert len(configs) == 2
    for data in configs:
        main, compat = _pairing(data)
        assert compat is None or compat != main


def _ask_probe() -> gitsystem._Probed:
    """`gitsystem._reads_remotes`, asked from git's folder with 5 s."""
    where = gitsystem._where()
    assert where is not None
    return gitsystem._reads_remotes(*where, time.monotonic() + 5.0)


def _this_git() -> gitsystem._Probed:
    """How the git on PATH reads a remote, expands ``%(prefix)`` and reads
    a compatibility object format, asked once in a fresh probe."""
    said = _ask_probe()
    assert said[:2] in gitsystem._REMOTE_ANSWERS.values(), "premise: git answers"
    assert None not in said[2:], "premise: git answers"
    return said


#: What another writer puts in ``remotes/p``: the content git answers
#: "reads no reset" for (its first URL alone) and "reads no legacy file"
#: for (an empty file, which names no URL).
_TAMPERED = [b"URL: bettermemory-probe-a\n", b""]


#: Another writer's content for the probe's config, which git expands to
#: another prefix.
_ELSEWHERE = b"[bettermemory]\n\tprefix = /bettermemory-elsewhere/z\n"
#: Another writer's content for the config of the git directory
#: `gitsystem._COMPAT_PROBE`, which git answers the other way for: one
#: setting no compatibility object format, which every git reads, where
#: this git dies on the probe's; one of a format version git refuses,
#: which every git dies on, where it reads the probe's.
_COMPAT_GONE = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
_COMPAT_DIES = b"[core]\n\trepositoryformatversion = 2\n\tbare = true\n"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("remotes/p", _TAMPERED[0]),
        ("remotes/p", _TAMPERED[1]),
        ("config", _ELSEWHERE),
        ("compat/config", None),
    ],
    ids=["no_reset", "no_legacy", "prefix", "compat"],
)
def test_a_probe_another_writes_while_git_reads_it_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, name: str, content: bytes | None
) -> None:
    """ENG-207's round 7: another writer rewriting ``remotes/p`` while git
    read it made git answer for its content, and the answer was kept (an
    hour for "reads no legacy file"). The probe is stamped before its
    content is checked and again after git answers, and the answer is kept
    only where nothing changed: here the writer replaces the file (or the
    config git expands ``%(prefix)`` from, or the config of a git
    directory git is asked in whether it reads a compatibility object
    format, ENG-207's round 8) in the middle of each ask, git answers for
    its content, and the ask says nothing, so it is asked again after
    `gitsystem.RETRY_SECONDS`. The next ask, with no writer, rebuilds the
    probe and answers as git does."""
    truth = _this_git()
    if content is None:
        content = _COMPAT_GONE if truth[3] is False else _COMPAT_DIES
    written = content
    real = git_runner.run_git
    question = {"config": gitsystem._PREFIX_ASK, "compat/config": gitsystem._COMPAT_ASK}
    asks = question.get(name, gitsystem._REMOTE_ASK)

    def tampered(args: Sequence[str], **kw: Any) -> Any:
        if tuple(args) == asks:
            path = os.path.join(kw["git_dir"], *name.removeprefix("compat/").split("/"))
            Path(path + ".t").write_bytes(written)
            os.replace(path + ".t", path)
        return real(args, **kw)

    monkeypatch.setattr(git_runner, "run_git", tampered)
    where = gitsystem._where()
    assert where is not None
    assert _ask_probe() == gitsystem._UNSAID
    root = Path(where[0]).parent / gitsystem.PROBE_FOLDER
    if name.startswith("compat/"):
        root = root / gitsystem._COMPAT_PROBE
    asked = subprocess.run(
        ["git", f"--git-dir={root}", *asks],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        check=not name.startswith("compat/"),
    )
    if name == "config":
        wrong = asked.stdout.strip() != f"{truth[2]}/z"
    elif name.startswith("compat/"):
        wrong = (asked.returncode == 0) is not truth[3]
    else:
        wrong = gitsystem._REMOTE_ANSWERS[asked.stdout.strip()] != truth[:2]
    assert wrong, "premise: git answers wrong"
    _caches.clear_all()
    found = gitsystem.sources()
    assert (found.legacy, found.resets, found.prefix) == (None, None, None)
    assert found.compat_differ is None
    monkeypatch.setattr(git_runner, "run_git", real)
    assert _ask_probe() == truth


def test_no_wrong_answer_is_kept_under_a_writer_racing_the_probe() -> None:
    """ENG-207's round 7, the reviewer's race: a thread rewrites
    ``remotes/p`` as fast as it can, each time with one of `_TAMPERED`,
    while asks run, which gave 79 "reads no legacy" and 66 "no reset"
    answers in 480 before. Every ask now answers as git does with the
    probe as built, or says nothing."""
    truth = _this_git()
    where = gitsystem._where()
    assert where is not None
    path = Path(where[0]).parent / gitsystem.PROBE_FOLDER / "remotes" / "p"
    stop = threading.Event()
    writes = 0

    def writer() -> None:
        nonlocal writes
        while not stop.is_set():
            try:
                path.with_name(".t").write_bytes(_TAMPERED[writes % 2])
                os.replace(path.with_name(".t"), path)
            except OSError:
                pass
            writes += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        said = [_ask_probe() for _ in range(60)]
    finally:
        stop.set()
        thread.join()
    assert writes > 60, "premise: the writer raced the asks"
    assert set(said) <= {truth, gitsystem._UNSAID}, said
    assert _ask_probe() == truth


@pytest.mark.parametrize(
    "stray",
    [
        "file",
        "folder",
        "link",
        "remote",
        "linked_remotes",
        "compat_file",
        "linked_compat",
    ],
)
def test_a_probe_holding_another_entry_is_rebuilt(tmp_path: Path, stray: str) -> None:
    """ENG-207's round 7: a probe holding an entry it is not built with
    stayed unknown, and git was asked again every minute. The entry is
    removed (a link unlinked, never followed) and the probe built again,
    and git's answer is kept; another build's probe, of other content,
    has a folder of its own (`gitsystem._probe_name`). So too in the git
    directory `gitsystem._COMPAT_PROBE` (ENG-207's round 8), a
    ``commondir`` there among them, which names git another config."""
    if "link" in stray and sys.platform == "win32":
        pytest.skip("a symbolic link needs a privilege Windows CI does not hold")
    truth = _this_git()
    where = gitsystem._where()
    assert where is not None
    root = Path(where[0]).parent / gitsystem.PROBE_FOLDER
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"x")
    if stray == "file":
        (root / "commondir").write_bytes(b"elsewhere\n")
    elif stray == "folder":
        (root / "branches").mkdir()
        (root / "branches" / "p").write_bytes(b"gh:planted\n")
    elif stray == "link":
        (root / "HEAD").unlink()
        (root / "HEAD").symlink_to(outside / "keep")
    elif stray == "remote":
        (root / "remotes" / "q").write_bytes(b"URL: gh:planted\n")
    elif stray == "compat_file":
        (root / gitsystem._COMPAT_PROBE / "commondir").write_bytes(b"..\n")
    elif stray == "linked_compat":
        shutil.rmtree(root / "compat-differ")
        (root / "compat-differ").symlink_to(outside, target_is_directory=True)
    else:
        shutil.rmtree(root / "remotes")
        (root / "remotes").symlink_to(outside, target_is_directory=True)
    assert _ask_probe() == truth
    assert gitsystem._probe_holds(str(root))
    assert (outside / "keep").read_bytes() == b"x"
    other = gitsystem._probe_name({**gitsystem._PROBE_FILES, "HEAD": b"x\n"}, ())
    assert other != gitsystem.PROBE_FOLDER
    assert gitsystem.PROBE_FOLDER == gitsystem._probe_name(
        gitsystem._PROBE_FILES, gitsystem._PROBE_FOLDERS
    )


def test_another_content_s_probe_is_swept_once_it_is_an_hour_old(
    tmp_path: Path,
) -> None:
    """ENG-207's round 8: a probe folder stayed per change of the probe's
    content (``git-probe-<digest>``), and so did the one before probes
    were named so (``git-probe``). An ask sweeps each last changed an
    hour or more before (`gitsystem.REASK_SECONDS`, its own time, a link
    not followed: unlinked, and one inside a folder too), and keeps a
    younger one, the probe it asks in and any other name."""
    truth = _this_git()
    where = gitsystem._where()
    assert where is not None
    state = Path(where[0]).parent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"x")
    old = time.time() - gitsystem.REASK_SECONDS - 60

    def aged(path: Path) -> Path:
        os.utime(path, (old, old), follow_symlinks=False)
        return path

    def probe(name: str) -> Path:
        path = state / name
        (path / "remotes").mkdir(parents=True)
        (path / "remotes" / "p").write_bytes(b"URL: gh:old\n")
        (path / "config").write_bytes(b"[core]\n\tbare = true\n")
        return path

    legacy = aged(probe("git-probe"))
    stale = aged(probe("git-probe-0123456789abcdef"))
    young = probe("git-probe-fedcba9876543210")
    named_otherwise = aged(probe("git-probe-notes"))
    stale_file = state / "git-probe-00000000000000ff"
    stale_file.write_bytes(b"")
    aged(stale_file)
    aged(state / gitsystem.PROBE_FOLDER)
    links = sys.platform != "win32"
    link = state / "git-probe-aaaaaaaaaaaaaaaa"
    holding = state / "git-probe-bbbbbbbbbbbbbbbb"
    if links:
        link.symlink_to(outside, target_is_directory=True)
        aged(link)
        probe(holding.name)
        (holding / "out").symlink_to(outside, target_is_directory=True)
        aged(holding)

    assert _ask_probe() == truth
    assert not legacy.exists() and not stale.exists() and not stale_file.exists()
    assert young.is_dir() and named_otherwise.is_dir()
    assert gitsystem._probe_holds(str(state / gitsystem.PROBE_FOLDER))
    if links:
        assert not os.path.lexists(link) and not holding.exists()
        assert (outside / "keep").read_bytes() == b"x"


@pytest.mark.parametrize(
    ("said", "legacy", "resets"),
    [
        ("bettermemory-probe-b\n", True, True),
        ("bettermemory-probe-a\n", True, False),
        ("p\n", False, True),
        ("something else\n", None, None),
        (None, None, None),
    ],
    ids=["git_2_54", "before_2_46", "breaking_changes", "unread", "no_answer"],
)
def test_a_remote_is_read_as_this_git_reads_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    said: str | None,
    legacy: bool | None,
    resets: bool | None,
) -> None:
    """A git that reads the legacy files keys a remote only they name; one
    built with breaking changes keys none there; a git before 2.46 keeps
    every URL around an empty one, and one from 2.46 empties the list at
    it. Where git did not say, a capture that needs the answer declines,
    one that does not reads on, and the answer is asked again after
    `gitsystem.RETRY_SECONDS`, not `gitsystem.REASK_SECONDS`."""
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    fake.remotes = said
    found = gitsystem.sources()
    assert (found.legacy, found.resets) == (legacy, resets)

    old = _repo(tmp_path / "old", remote=None)
    (old / ".git" / "remotes").mkdir(exist_ok=True)
    (old / ".git" / "remotes" / "up").write_text(
        "URL: https://example.invalid/acme/up.git\n", encoding="utf-8"
    )
    if legacy is None:
        _declines(old, git_dies=False)
    else:
        assert ("example.invalid/acme/up" in _keys(old)) is legacy

    emptied = _repo(tmp_path / "emptied", remote=None)
    _append(
        emptied / ".git" / "config",
        '[remote "origin"]\n\turl = https://example.invalid/acme/gone.git\n'
        "\turl =\n\turl = https://example.invalid/acme/kept.git\n",
    )
    if resets is None:
        _declines(emptied, git_dies=False)
    else:
        kept = {"example.invalid/acme/kept"}
        assert _keys(emptied) == (
            kept if resets else kept | {"example.invalid/acme/gone"}
        )

    plain = _repo(tmp_path / "plain")
    assert _keys(plain) == {_key(plain, "gh:acme/lib")}

    asked = len(fake.asked)
    _caches.clear_all()
    gitsystem.sources()
    assert len(fake.asked) == asked
    _aged(state, "asked", gitsystem.RETRY_SECONDS)
    _caches.clear_all()
    gitsystem.sources()
    # Asked again: the system file, the listing, the remote and, where git
    # answered the remote at all, the prefix and the compatibility object
    # format.
    again = 0 if legacy is not None else 3 if said is None else 5
    assert len(fake.asked) - asked == again
