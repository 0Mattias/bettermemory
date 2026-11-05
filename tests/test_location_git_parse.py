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

import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bettermemory import _caches, gitconfig, gitsystem, location, location_git, origin
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


#: An include git settles and this reader does not: ``%(prefix)`` names
#: git's own installation, which a capture does not ask for.
_UNSETTLED = "gitdir:%(prefix)/bettermemory-nowhere/"


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
    tmp_path: Path, text: str, declines: bool
) -> None:
    """ENG-207's round 6: an include under a condition not settled here
    has the capture decline where what it reads could change the URLs
    keyed: an insteadOf rule whose prefix matches one, a remote URL, or a
    line git may die on. A rule that matches none of them, or a file that
    sets neither, leaves the capture as git reads it."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(text, encoding="utf-8")
    _include(_global(), _UNSETTLED, rules)
    assert _fetched(repo) == "gh:acme/lib", "premise: git reads no such include"
    if declines:
        _declines(repo, git_dies=False)
    else:
        assert _keys(repo) == {_key(repo, "gh:acme/lib")}


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
        ["git", f"--git-dir={probe}", *gitsystem._REMOTE_ASK],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    said = gitsystem._REMOTE_ANSWERS[asked.stdout.strip()]
    assert (found.legacy, found.resets) == said
    entry = _record(state)["entries"][0]
    assert (entry["legacy"], entry["resets"]) == said
    assert _record(state)["format"] == gitsystem.RECORD_FORMAT == 2


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
    assert len(fake.asked) - asked == (0 if legacy is not None else 3)
