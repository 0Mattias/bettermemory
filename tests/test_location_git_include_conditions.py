"""A capture reads what git reads under ``onbranch:`` and ``hasconfig:``
includes, and from the legacy remote files (ENG-207's review).

A location capture took no value from an include under ``onbranch:`` or
``hasconfig:remote.*.url:``, where git does, and read no remote from the
``remotes/<name>`` and ``branches/<name>`` files git still honours, which
the origin's signature did not stamp either. Both conditions are now
evaluated as git 2.54 evaluates them (`gitconfig.IncludeContext`,
`location_git._rewrites`), the legacy files are read as remote.c reads
them (`location_git._legacy_remotes`), and where git's semantics are not
mirrored the capture declines (fail closed) instead of keying another
answer.

Each expectation is git's own answer: the remote URLs ``git config
--get-regexp`` lists and the URL ``git ls-remote --get-url`` fetches from.
Every test runs on both of githead's legs (`githead._POSIX`), as the
origin cache's tests do.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bettermemory import githead, location, origin
from bettermemory.location import remote_key

from .test_membership_security import neutral_git

_RULES = '[url "https://mirror.invalid/"]\n\tinsteadOf = gh:\n'
_MIRRORED = "https://mirror.invalid/acme/lib"


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture(autouse=True, params=["posix", "off-posix"])
def leg(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """githead's discovery as POSIX reads it, or declining as off POSIX."""
    if request.param == "off-posix":
        monkeypatch.setattr(githead, "_POSIX", False)
    elif os.name != "posix":
        pytest.skip("off POSIX the host's own leg is the off-posix one")
    return str(request.param)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check:
        assert done.returncode == 0, done.stderr
    return done


def _repo(path: Path, remote: str | None = "gh:acme/lib") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "first")
    if remote is not None:
        _git(path, "remote", "add", "origin", remote)
    return path


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _fetched(repo: Path, name: str = "origin") -> str:
    """The URL git fetches the remote `name` from, as git prints it (a
    blank that ends it kept)."""
    return _git(repo, "ls-remote", "--get-url", name).stdout.removesuffix("\n")


def _listed(repo: Path) -> dict[str, list[str]]:
    """{remote: its URLs} as ``git config`` lists them."""
    done = _git(repo, "config", "--get-regexp", r"^remote\..*\.url$", check=False)
    assert done.returncode in (0, 1), done.stderr
    out: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        name, url = line.split(" ", 1)
        out.setdefault(name[len("remote.") : -len(".url")], []).append(url)
    return out


def _capture(repo: Path, *, cached: bool = True) -> location.Capture:
    return location.capture(str(repo), write_token=False, use_cache=cached)


def _keys(repo: Path) -> set[str]:
    """The remote keys of a capture that read the repository's remotes:
    one that declined has none, and is not this."""
    cap = _capture(repo)
    assert cap.state == location.REPO, cap.detail
    return set(cap.remote_keys)


def _key(repo: Path, url: str) -> str:
    found = remote_key(url, str(repo))
    assert found is not None
    return found


def _include(config: Path, condition: str, target: Path) -> None:
    _append(config, f'[includeIf "{condition}"]\n\tpath = {target.as_posix()}\n')


@pytest.mark.parametrize(
    ("rules", "fetched"),
    [
        (
            '[url "https://a.invalid/"]\n\tinsteadOf = y\n'
            '[url "https://b.invalid/"]\n\tinsteadOf = x\n'
            '[url "https://a.invalid/"]\n\tinsteadOf = x\n',
            "https://a.invalid//foo",
        ),
        ('[url "https://all.invalid/"]\n\tinsteadOf =\n', "https://all.invalid/x/foo"),
        (
            '[url "https://all.invalid/"]\n\tinsteadOf =\n'
            '[url "https://x.invalid/"]\n\tinsteadOf = x/\n',
            "https://x.invalid/foo",
        ),
    ],
    ids=["first-base", "empty-prefix", "longest"],
)
def test_an_insteadof_rule_is_chosen_as_git_chooses_it(
    tmp_path: Path, rules: str, fetched: str
) -> None:
    """remote.c's ``alias_url``: the longest matching prefix wins; of equal
    ones, the first under the base git met first, the rules of one base
    kept together where the base recurs; an empty prefix matches every
    URL. The capture took the first rule read of equal ones and skipped an
    empty prefix."""
    repo = _repo(tmp_path / "repo", remote="x/foo")
    _append(repo / ".git" / "config", rules)
    assert _fetched(repo) == fetched, "premise: git's rewrite"
    assert _key(repo, fetched) in _keys(repo)


# ---------------------------------------------------------------------------
# onbranch:
# ---------------------------------------------------------------------------


def test_an_onbranch_include_is_taken_on_its_branch_and_a_switch_is_seen(
    tmp_path: Path,
) -> None:
    """Under ``onbranch:main`` git takes an insteadOf rule on main and not
    on another branch: the capture keys the URL git fetches from on each,
    and a branch switch is not answered from the kept capture, for a
    location capture and for the origin's (whose signature holds HEAD),
    there and back."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _include(repo / ".git" / "config", "onbranch:main", rules)
    assert _fetched(repo) == _MIRRORED, "premise: git takes the rule on main"
    kept = _capture(repo)
    assert _key(repo, _MIRRORED) in kept.remote_keys
    assert _capture(repo) is kept
    assert origin.capture(repo).repo == _MIRRORED

    _git(repo, "switch", "-q", "-c", "dev")
    assert _fetched(repo) == "gh:acme/lib", "premise: not on dev"
    moved = _capture(repo)
    assert moved is not kept and moved.state == location.REPO
    assert _key(repo, _MIRRORED) not in moved.remote_keys
    assert moved.remote_keys == _capture(repo, cached=False).remote_keys
    assert origin.capture(repo).repo == "gh:acme/lib"

    _git(repo, "switch", "-q", "main")
    back = _capture(repo)
    assert back is not moved and _key(repo, _MIRRORED) in back.remote_keys
    assert origin.capture(repo).repo == _MIRRORED


def test_an_onbranch_include_sets_a_remote_on_its_branch_alone(tmp_path: Path) -> None:
    """A remote set in a file ``onbranch:`` includes is git's on that
    branch alone (``git config`` lists it there): the capture keys it
    there and not elsewhere."""
    repo = _repo(tmp_path / "repo", remote=None)
    extra = tmp_path / "extra.inc"
    extra.write_text('[remote "extra"]\n\turl = https://extra.invalid/x\n')
    _include(repo / ".git" / "config", "onbranch:main", extra)
    assert _listed(repo) == {"extra": ["https://extra.invalid/x"]}
    assert _keys(repo) == {_key(repo, "https://extra.invalid/x")}
    _git(repo, "switch", "-q", "-c", "other")
    assert _listed(repo) == {}
    assert _keys(repo) == set()


@pytest.mark.parametrize(
    ("pattern", "branch", "holds"),
    [
        ("feature/", "feature/a/b", True),
        ("feature/", "featurex", False),
        ("feat*", "feature/a", False),
        ("feat*", "feature", True),
        ("feature/**", "feature/a/b", True),
        ("f?ature", "feature", True),
        ("[ef]eature", "feature", True),
        ("[!f]eature", "feature", False),
    ],
)
def test_an_onbranch_pattern_is_matched_as_git_matches_it(
    tmp_path: Path, pattern: str, branch: str, holds: bool
) -> None:
    """git's wildmatch with WM_PATHNAME against the branch's name, ``**``
    put after a pattern that ends in ``/``: ``*`` does not cross a
    ``/``."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _include(repo / ".git" / "config", f"onbranch:{pattern}", rules)
    _git(repo, "switch", "-q", "-c", branch)
    assert (_fetched(repo) == _MIRRORED) is holds, "premise: git's match"
    assert (_key(repo, _MIRRORED) in _keys(repo)) is holds


def test_an_onbranch_include_follows_heads_chain_and_never_holds_detached(
    tmp_path: Path,
) -> None:
    """git matches the name HEAD's chain of symbolic refs ends at, and no
    name where HEAD is detached or ends outside ``refs/heads/``. A
    symbolic ref deep in the chain moved, with HEAD's bytes and the ref it
    names as they were, is seen by a kept capture (`location_git._head_branch`
    stamps each ref file the chain reads)."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _include(repo / ".git" / "config", "onbranch:feature/q", rules)
    _git(repo, "branch", "feature/q")
    _git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/feature/q")
    _git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/inner")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/alias")
    assert _fetched(repo) == _MIRRORED, "premise: git follows the chain"
    kept = _capture(repo)
    assert _key(repo, _MIRRORED) in kept.remote_keys
    _git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/main")
    assert _fetched(repo) == "gh:acme/lib"
    moved = _capture(repo)
    assert moved is not kept and moved.state == location.REPO
    assert _key(repo, _MIRRORED) not in moved.remote_keys

    _git(repo, "checkout", "-q", "--detach", "feature/q")
    assert _fetched(repo) == "gh:acme/lib", "premise: never detached"
    assert _key(repo, _MIRRORED) not in _keys(repo)
    _git(repo, "update-ref", "refs/remotes/x", "HEAD")
    _git(repo, "symbolic-ref", "HEAD", "refs/remotes/x")
    _include(repo / ".git" / "config", "onbranch:**", rules)
    assert _fetched(repo) == "gh:acme/lib", "premise: never outside refs/heads"
    assert _key(repo, _MIRRORED) not in _keys(repo)


def test_an_onbranch_include_in_the_global_config_is_taken(tmp_path: Path) -> None:
    """git evaluates ``onbranch:`` in the global config against the
    repository's HEAD too."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _include(tmp_path / "gitconfig", "onbranch:main", rules)
    assert _fetched(repo) == _MIRRORED
    assert _key(repo, _MIRRORED) in _keys(repo)
    _git(repo, "switch", "-q", "-c", "dev")
    assert _fetched(repo) == "gh:acme/lib"
    assert _key(repo, _MIRRORED) not in _keys(repo)


# ---------------------------------------------------------------------------
# hasconfig:remote.*.url:
# ---------------------------------------------------------------------------


def test_a_hasconfig_include_is_taken_where_a_remote_url_matches(
    tmp_path: Path,
) -> None:
    """``hasconfig:remote.*.url:<pattern>`` holds where any remote URL of
    the config git reads matches the pattern as a path: the capture takes
    the rule where it matches and not where it does not, and a URL changed
    so it no longer matches, or so it matches again, is seen by the kept
    location capture and the origin's."""
    repo = _repo(tmp_path / "repo")
    _git(repo, "remote", "add", "two", "https://example.invalid/acme/two")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    config = repo / ".git" / "config"
    _include(config, "hasconfig:remote.*.url:https://example.invalid/**", rules)
    assert _fetched(repo) == _MIRRORED, "premise: it matches"
    kept = _capture(repo)
    assert _key(repo, _MIRRORED) in kept.remote_keys
    assert _capture(repo) is kept
    assert origin.capture(repo).repo == _MIRRORED

    _git(repo, "remote", "set-url", "two", "https://other.invalid/acme/two")
    assert _fetched(repo) == "gh:acme/lib", "premise: no longer"
    moved = _capture(repo)
    assert moved is not kept and moved.state == location.REPO
    assert _key(repo, _MIRRORED) not in moved.remote_keys
    assert origin.capture(repo).repo == "gh:acme/lib"

    _git(repo, "remote", "set-url", "two", "https://example.invalid/acme/two")
    assert _fetched(repo) == _MIRRORED
    assert _key(repo, _MIRRORED) in _keys(repo)
    assert origin.capture(repo).repo == _MIRRORED


@pytest.mark.parametrize(
    ("pattern", "holds"),
    [
        ("https://example.invalid/*", False),
        ("https://example.invalid/**", True),
        ("https://example.invalid/acme/", False),
        ("https://*.invalid/acme/lib", True),
        ("gh:**", False),
        ("nomatch", False),
    ],
)
def test_a_hasconfig_pattern_is_matched_as_git_matches_it(
    tmp_path: Path, pattern: str, holds: bool
) -> None:
    """git's wildmatch with WM_PATHNAME against each URL, the pattern as
    it is (no ``**`` after a ``/``): ``*`` does not cross a ``/``, and a
    ``**`` that is no whole component is a ``*``."""
    repo = _repo(tmp_path / "repo", remote="https://example.invalid/acme/lib")
    _git(repo, "remote", "add", "two", "gh:acme/two")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    _include(repo / ".git" / "config", f"hasconfig:remote.*.url:{pattern}", rules)
    mirrored = "https://mirror.invalid/acme/two"
    assert (_fetched(repo, "two") == mirrored) is holds, "premise: git's match"
    assert (_key(repo, mirrored) in _keys(repo)) is holds


def test_a_hasconfig_include_matches_every_scope_in_either_order(
    tmp_path: Path,
) -> None:
    """git collects the remote URLs of every config file it reads before
    it evaluates the first ``hasconfig:`` condition, wherever that sits: a
    URL of the global config matches a condition in the repository's,
    and a URL of the repository's one in the global config, read before
    it."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    global_config = tmp_path / "gitconfig"
    base = global_config.read_text(encoding="utf-8")
    global_config.write_text(
        base + '[remote "shared"]\n\turl = https://shared.invalid/x\n', encoding="utf-8"
    )
    config = repo / ".git" / "config"
    plain = config.read_text(encoding="utf-8")
    _include(config, "hasconfig:remote.*.url:https://shared.invalid/**", rules)
    assert _fetched(repo) == _MIRRORED, "premise: a global URL matches"
    assert _key(repo, _MIRRORED) in _keys(repo)

    config.write_text(plain, encoding="utf-8")
    global_config.write_text(base, encoding="utf-8")
    _include(global_config, "hasconfig:remote.*.url:gh:acme/lib", rules)
    assert _fetched(repo) == _MIRRORED, "premise: a later file's URL matches"
    assert _key(repo, _MIRRORED) in _keys(repo)


@pytest.mark.parametrize("key", ["path", "other"])
def test_a_remote_url_an_includeif_sets_under_hasconfig_declines(
    tmp_path: Path, key: str
) -> None:
    """Where a ``hasconfig:`` condition is evaluated (at any key of its
    section, git's ``git_config_include``), git collects the URLs with
    every file an includeIf reads forbidden to set one, and refuses the
    configuration where one does: git runs no command there, and the
    capture declines (indeterminate, not kept). Without the condition the
    same include is read as git reads it."""
    repo = _repo(tmp_path / "repo")
    url = tmp_path / "url.inc"
    url.write_text('[remote "x"]\n\turl = https://x.invalid/y\n', encoding="utf-8")
    config = repo / ".git" / "config"
    _include(config, f"gitdir:{Path(os.path.realpath(repo)).as_posix()}/", url)
    assert "x" in _listed(repo)
    assert _capture(repo).state == location.REPO
    _append(config, f'[includeIf "hasconfig:remote.*.url:nomatch"]\n\t{key} = x\n')
    done = _git(repo, "ls-remote", "--get-url", "origin", check=False)
    assert done.returncode == 128 and "remote URLs cannot be configured" in done.stderr
    cap = _capture(repo)
    assert cap.state == location.INDETERMINATE
    assert "git refuses a remote URL" in (cap.detail or "")
    assert _capture(repo) is not cap


def test_a_pattern_not_read_as_git_reads_it_declines(tmp_path: Path) -> None:
    """A character class (``[:alpha:]``) or a backslash in a pattern is not
    read here as git's wildmatch reads it: where the include sets an
    insteadOf rule or a remote URL the capture declines, and where it sets
    neither it reads on."""
    repo = _repo(tmp_path / "repo")
    rules = tmp_path / "rules.inc"
    rules.write_text(_RULES, encoding="utf-8")
    config = repo / ".git" / "config"
    plain = config.read_text(encoding="utf-8")
    for condition in ("onbranch:[[:alpha:]]ain", "hasconfig:remote.*.url:gh[:]**"):
        config.write_text(plain, encoding="utf-8")
        _include(config, condition, rules)
        assert _capture(repo, cached=False).state == location.INDETERMINATE, condition
    neutral = tmp_path / "neutral.inc"
    neutral.write_text("[user]\n\tname = Test\n", encoding="utf-8")
    config.write_text(plain, encoding="utf-8")
    _include(config, "onbranch:[[:alpha:]]ain", neutral)
    assert _capture(repo, cached=False).state == location.REPO


# ---------------------------------------------------------------------------
# The legacy remote files
# ---------------------------------------------------------------------------


def test_a_legacy_remotes_file_is_read_and_an_edit_is_seen(tmp_path: Path) -> None:
    """``remotes/<name>`` names a remote git fetches from (its ``URL:``
    lines; ``git remote`` does not list it): the capture keys its URLs as
    written and as insteadOf rewrites them, and the next call after an
    edit to the file, or its removal, reads it again."""
    repo = _repo(tmp_path / "repo", remote=None)
    _append(repo / ".git" / "config", _RULES)
    folder = repo / ".git" / "remotes"
    folder.mkdir()
    legacy = folder / "old"
    legacy.write_text("URL:   gh:acme/lib  \nPull: refs/heads/main:refs/heads/x\n")
    assert _fetched(repo, "old") == _MIRRORED, "premise: git reads it"
    kept = _capture(repo)
    assert set(kept.remote_keys) == {_key(repo, "gh:acme/lib"), _key(repo, _MIRRORED)}
    assert _capture(repo) is kept
    legacy.write_text("URL: https://legacy.invalid/two\n")
    assert _fetched(repo, "old") == "https://legacy.invalid/two"
    edited = _capture(repo)
    assert edited is not kept
    assert edited.remote_keys == (_key(repo, "https://legacy.invalid/two"),)
    legacy.unlink()
    emptied = _capture(repo)
    assert emptied.remote_keys == ()
    assert _capture(repo) is emptied
    (folder / "new").write_text("URL: https://legacy.invalid/new\n")
    assert _fetched(repo, "new") == "https://legacy.invalid/new"
    assert _keys(repo) == {_key(repo, "https://legacy.invalid/new")}
    (repo / ".git" / "branches").mkdir()
    (repo / ".git" / "branches" / "more").write_text("https://branch.invalid/m\n")
    assert _fetched(repo, "more") == "https://branch.invalid/m"
    assert _key(repo, "https://branch.invalid/m") in _keys(repo)


def test_a_legacy_file_is_read_only_where_git_reads_it(tmp_path: Path) -> None:
    """git reads ``remotes/<name>`` only for a remote no config file gives
    a URL, the global config's included and an emptied list not; then
    ``branches/<name>`` where the first gives no URL (its first line to a
    ``#``, a blank before it kept). An empty ``URL:`` empties the list, as
    an empty ``remote.<name>.url`` does in the config, where the capture
    reads it so too."""
    repo = _repo(tmp_path / "repo")
    remotes, branches = repo / ".git" / "remotes", repo / ".git" / "branches"
    remotes.mkdir()
    branches.mkdir()
    (remotes / "origin").write_text("URL: https://legacy.invalid/origin\n")
    (remotes / "both").write_text("Pull: x\n")
    (branches / "both").write_text("  https://branch.invalid/b #dev  \nsecond\n")
    (remotes / "reset").write_text(
        "URL: https://a.invalid/a\nURL:\nURL: https://b.invalid/b\n"
    )
    (remotes / "machine").write_text("URL: https://legacy.invalid/m\n")
    _append(
        tmp_path / "gitconfig",
        '[remote "machine"]\n\turl = https://machine.invalid/m\n',
    )
    _append(
        repo / ".git" / "config",
        '[remote "cleared"]\n\turl = https://a.invalid/c\n\turl = ""\n'
        "\turl = https://b.invalid/c\n",
    )
    expected = {
        "origin": "gh:acme/lib",
        "both": "https://branch.invalid/b ",
        "reset": "https://b.invalid/b",
        "machine": "https://machine.invalid/m",
        "cleared": "https://b.invalid/c",
    }
    for name, url in expected.items():
        assert _fetched(repo, name) == url, f"premise: git reads {name} so"
    keys = _keys(repo)
    for name, url in expected.items():
        assert (_key(repo, url) in keys) is (name != "machine"), name
    for gone in ("https://legacy.invalid/origin", "https://legacy.invalid/m"):
        assert _key(repo, gone) not in keys
    assert _key(repo, "https://a.invalid/a") not in keys
    assert _key(repo, "https://a.invalid/c") not in keys


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_legacy_file_that_is_no_regular_file_declines(tmp_path: Path) -> None:
    """A FIFO would block git's read; the capture declines on it instead of
    waiting or reading another answer. A folder git reads nothing from
    (git warns and goes on to ``branches/<name>``)."""
    repo = _repo(tmp_path / "repo", remote=None)
    remotes = repo / ".git" / "remotes"
    remotes.mkdir()
    (remotes / "folder").mkdir()
    (repo / ".git" / "branches").mkdir()
    (repo / ".git" / "branches" / "folder").write_text("https://branch.invalid/f\n")
    assert _fetched(repo, "folder") == "https://branch.invalid/f"
    assert _keys(repo) == {_key(repo, "https://branch.invalid/f")}
    make_fifo = getattr(os, "mkfifo")
    make_fifo(remotes / "pipe")
    cap = _capture(repo)
    assert cap.state == location.INDETERMINATE and "not a regular file" in (
        cap.detail or ""
    )


def test_origins_legacy_file_is_in_the_signature(tmp_path: Path) -> None:
    """``git remote get-url origin`` reads ``remotes/origin`` where the
    config gives ``origin`` no URL, and the origin's capture asks it: the
    signature stamps that file and ``branches/origin``, so an edit to
    either is seen by the next capture, not served for ten minutes."""
    repo = _repo(tmp_path / "repo", remote=None)
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    (repo / ".git" / "remotes").mkdir()
    legacy = repo / ".git" / "remotes" / "origin"
    legacy.write_text("URL: https://legacy.invalid/one\n")
    assert _fetched(repo) == "https://legacy.invalid/one"
    assert origin.capture(repo).repo == "https://legacy.invalid/one"
    legacy.write_text("URL: https://legacy.invalid/two\n")
    assert origin.capture(repo).repo == "https://legacy.invalid/two"
    legacy.unlink()
    (repo / ".git" / "branches").mkdir()
    (repo / ".git" / "branches" / "origin").write_text("https://branch.invalid/o\n")
    assert _fetched(repo) == "https://branch.invalid/o"
    assert origin.capture(repo).repo == "https://branch.invalid/o"
