"""The reads whose rule changed with a git release, read by the version
the git on PATH names (ENG-227).

Each rule is checked here with a version put in `gitsystem.Sources.version`:
one release below its cut, the cut itself, and none named, where every
one declines. Where each cut lies was found by running the git-reading
suites against git built from each release, and those suites check the
reads against the git on PATH (docs/operations.md, "Supported git").
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from bettermemory import gitfile_rule, gitsystem, location, origin_gitfiles

from .test_location_git_include_conditions import _capture, _key, _repo
from .test_membership_security import neutral_git

Version = tuple[int, int, int] | None


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)


def _named(monkeypatch: pytest.MonkeyPatch, version: Version) -> None:
    """The git on PATH taken as naming `version` (None: naming none)."""
    real = gitsystem.sources
    monkeypatch.setattr(
        gitsystem,
        "sources",
        lambda **kw: dataclasses.replace(real(**kw), version=version),
    )


def _below(release: tuple[int, int]) -> tuple[int, int, int]:
    """The last patch of the release before `release`."""
    return release[0], release[1] - 1, 99


@pytest.mark.parametrize(
    ("version", "release", "said"),
    [
        ((2, 45, 0), (2, 45), True),
        ((2, 44, 99), (2, 45), False),
        ((3, 0, 0), (2, 45), True),
        ((2, 100, 0), (3, 0), False),
        (None, (2, 45), None),
    ],
)
def test_a_release_is_compared_by_its_major_and_minor(
    monkeypatch: pytest.MonkeyPatch,
    version: Version,
    release: tuple[int, int],
    said: bool | None,
) -> None:
    _named(monkeypatch, version)
    assert gitsystem.is_at_least(release) is said


def _git_dir(where: Path, config: bytes) -> str:
    where.mkdir(parents=True)
    (where / "config").write_bytes(config)
    return str(where)


_V1 = b"[core]\n\trepositoryformatversion = 1\n\tbare = false\n[extensions]\n\t"
#: Each format-1 extension an older git dies on, as a config sets it.
_NEWER_EXTENSIONS = {
    b"refstorage": b"refStorage = files",
    b"relativeworktrees": b"relativeWorktrees = true",
    b"submodulepathconfig": b"submodulePathConfig = true",
}


@pytest.mark.parametrize(
    ("ext", "since"),
    [
        (b"refstorage", (2, 44)),
        (b"relativeworktrees", (2, 48)),
        (b"submodulepathconfig", (2, 54)),
    ],
)
def test_an_extension_an_older_git_does_not_know_is_refused_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ext: bytes, since: tuple[int, int]
) -> None:
    """`gitfile_rule._EXTENSIONS_SINCE`: a repository of format 1 setting
    an extension git dies on before the release that knows it is refused
    for a git older than that, read for one as new, and refused where git
    has named no version."""
    config = _V1 + _NEWER_EXTENSIONS[ext] + b"\n"
    name = ext.decode("ascii")
    for i, (version, why) in enumerate(
        [
            (_below(since), f"which git before {since[0]}.{since[1]} does not know"),
            ((*since, 0), None),
            (None, "has not named its version"),
        ]
    ):
        _named(monkeypatch, version)
        got = gitfile_rule.tree_format(_git_dir(tmp_path / str(i), config))
        if why is None:
            assert got == gitfile_rule.TreeFormat(), got
        else:
            assert got.why is not None and f"extensions.{name}, " in got.why, got
            assert why in got.why, got


@pytest.mark.parametrize(
    ("version_line", "version", "read"),
    [
        (b"-2", (2, 41, 99), False),
        (b"-2", (2, 42, 0), True),
        (b"-2", None, None),
        (b"1", None, True),
        (b"-1", None, False),
    ],
)
def test_config_worktree_below_version_minus_1_is_read_from_2_42(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_line: bytes,
    version: Version,
    read: bool | None,
) -> None:
    """`gitfile_rule.reads_worktree_config`: git reads ``config.worktree``
    under ``extensions.worktreeConfig`` at a format version below -1 from
    2.42 alone; at 1 on every git, at -1 on none."""
    _named(monkeypatch, version)
    common = _git_dir(
        tmp_path / "common",
        b"[core]\n\trepositoryformatversion = "
        + version_line
        + b"\n[extensions]\n\tworktreeConfig = true\n",
    )
    assert gitfile_rule.reads_worktree_config(common) is read


@pytest.mark.parametrize("blank", [b"\t", b"\r"], ids=["tab", "cr"])
def test_a_working_tree_holding_a_tab_or_a_cr_is_refused_before_2_45(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: bytes
) -> None:
    """`gitfile_rule.tree_format`: a ``core.worktree`` holding a tab or a
    CR, which git before 2.45 reads outside quotes as a space, names
    another folder to it: the format is refused there and where git has
    named no version, and read from 2.45."""
    config = b'[core]\n\trepositoryformatversion = 0\n\tworktree = "/a' + blank
    config += b'b"\n'
    for i, version in enumerate([(2, 44, 99), None, (2, 45, 0)]):
        _named(monkeypatch, version)
        got = gitfile_rule.tree_format(_git_dir(tmp_path / str(i), config))
        if version == (2, 45, 0):
            assert got == gitfile_rule.TreeFormat(worktree=f"/a{blank.decode()}b"), got
        else:
            assert got.worktree is None and got.why is not None, got
            assert "git before 2.45 reads as a space" in got.why, got


_MERGE = b"branch.autosetupmerge"


@pytest.mark.parametrize(
    ("value", "version", "dies"),
    [
        (b"always", None, False),
        (b"always", (2, 34, 99), False),
        (b"true", None, False),
        (None, None, False),
        (b"maybe", (2, 56, 0), True),
        (b"inherit", (2, 34, 99), True),
        (b"inherit", (2, 35, 0), False),
        (b"inherit", None, True),
        (b"simple", (2, 36, 99), True),
        (b"simple", (2, 37, 0), False),
        (b"simple", None, True),
        (b"ALWAYS", (2, 34, 99), False),
        (b"Always", (2, 35, 0), True),
        (b"ALWAYS", None, True),
        (b"Simple", (2, 56, 0), True),
    ],
)
def test_a_merge_word_is_read_from_the_release_that_takes_it(
    monkeypatch: pytest.MonkeyPatch, value: bytes | None, version: Version, dies: bool
) -> None:
    """`origin_gitfiles._MERGE_WORDS_SINCE`: ``branch.autoSetupMerge``
    takes ``inherit`` from 2.35 and ``simple`` from 2.37, ``always`` in any
    case before 2.35 and as written from it; where the answer turns on a
    version git has not named, git may die, and the files do not answer."""
    _named(monkeypatch, version)
    assert origin_gitfiles._rev_parse_dies(_MERGE, value) is dies


@pytest.mark.parametrize(
    ("name", "value", "dies"),
    [
        (b"core.notesref", None, True),
        (b"core.notesref", b"refs/notes/x", False),
        (b"core.hookspath", None, True),
        (b"core.hookspath", b"hooks", False),
        (b"mailmap.file", None, True),
        (b"mailmap.file", b"map", False),
        (b"core.preloadindex", b"maybe", True),
        (b"core.preloadindex", b"true", False),
        (b"core.bigfilethreshold", None, True),
        (b"core.bigfilethreshold", b"maybe", True),
        (b"core.bigfilethreshold", b"-1", True),
        (b"core.bigfilethreshold", b"512m", False),
    ],
)
@pytest.mark.parametrize("version", [(2, 56, 0), None])
def test_a_value_an_older_git_dies_on_is_read_as_dying_on_every_git(
    monkeypatch: pytest.MonkeyPatch,
    name: bytes,
    value: bytes | None,
    dies: bool,
    version: Version,
) -> None:
    """`origin_gitfiles._OLDER_DIES`: what git through 2.52 dies on and
    2.54 reads past is read as dying whatever git is on PATH, a process
    where the files answered for 2.54, on values no checkout sets on
    purpose."""
    _named(monkeypatch, version)
    assert origin_gitfiles._rev_parse_dies(name, value) is dies


_TABBED = "https://example.invalid/a\tb"


@pytest.mark.parametrize(
    "where",
    [
        f'[remote "origin"]\n\turl = "{_TABBED}"\n',
        '[remote "origin"]\n\turl = gh:acme/lib\n'
        '[url "https://mirror.invalid/"]\n\tinsteadOf = "gh:\tacme"\n',
    ],
    ids=["url", "insteadof"],
)
def test_a_url_holding_a_tab_declines_before_2_45(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """`location_git._KEEPS_SPACES_SINCE`: a remote URL or an insteadOf
    prefix holding a tab, which git before 2.45 reads outside quotes as a
    space, declines there and where git has named no version, and is read
    from 2.45."""
    repo = _repo(tmp_path / "repo", remote=None)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(where)
    for version in [(2, 44, 99), None, (2, 45, 0)]:
        _named(monkeypatch, version)
        cap = _capture(repo, cached=False)
        if version == (2, 45, 0):
            assert cap.state == location.REPO, cap.detail
            url = _TABBED if "insteadof" not in where.lower() else "gh:acme/lib"
            assert set(cap.remote_keys) == {_key(repo, url)}
        else:
            assert cap.state == location.INDETERMINATE, cap
            assert "git before 2.45 reads as a space" in cap.detail, cap.detail


def test_an_include_path_holding_a_tab_is_not_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gitconfig._include`: an include path holding a tab, which git
    before 2.45 reads as a space and names another file by, is a config
    not read whole, on every git: the capture declines."""
    repo = _repo(tmp_path / "repo", remote=None)
    # A literal tab cannot be created in a Windows filename. The refusal
    # precedes opening the target, whether that name exists or not. Keep
    # the alternate file an older git may name available on every OS.
    (repo / ".git" / "a b.inc").write_text(
        '[remote "origin"]\n\turl = gh:acme/lib\n', encoding="utf-8"
    )
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write('[include]\n\tpath = "a\tb.inc"\n')
    _named(monkeypatch, (2, 56, 0))
    cap = _capture(repo, cached=False)
    assert cap.state == location.INDETERMINATE, cap
    assert not cap.remote_keys
