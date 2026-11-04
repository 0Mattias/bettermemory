"""A repository's git config is read as git reads it (ENG-160).

The capture read a repository's config in-process: a file over its 1 MiB
bound was dropped whole (every remote with it), includes were not
followed, and ``url.<base>.insteadOf`` was not applied, so a remote
spelled with an alias keyed apart from a clone of the URL it stands for.
The config is now read to its last whole line within the bound, includes
are followed as git follows them (``include.path``, ``includeIf`` on
``gitdir:``), each remote is keyed as written and as git rewrites it, and
a ``.git`` file's target whose config cannot be read whole is refused.
Following includes is bounded (review M D3): a cycle or an include past
git's depth stops the read, as git stops, and so do the includes past
`_INCLUDE_FILES` files read in all. The system config is read as git
finds it, and a kept capture is read again once a config file read for
its remotes changes (ENG-207).

Each expectation is git's own answer where git gives one: the remotes
``git config`` lists, the URL ``git remote get-url`` fetches from.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import (
    _caches,
    gitconfig,
    gitfile_rule,
    gitsystem,
    location,
    location_git,
)
from bettermemory.location import remote_key

from .test_membership_security import neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    return path


def _keys(repo: Path) -> set[str]:
    cap = location.capture(str(repo), write_token=False, use_cache=False)
    return set(cap.remote_keys)


def _git_remotes(repo: Path) -> dict[str, list[str]]:
    """{remote: its URLs} as git reads the repository's config."""
    out: dict[str, list[str]] = {}
    listed = _git(repo, "config", "--get-regexp", r"^remote\..*\.url$")
    for line in listed.splitlines():
        name, url = line.split(" ", 1)
        out.setdefault(name[len("remote.") : -len(".url")], []).append(url)
    return out


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_a_config_over_the_bound_keeps_the_remotes_read_within_it(
    tmp_path: Path,
) -> None:
    """A config grown past 1 MiB (a branch section per branch) kept no
    remote at all; the remotes within the bound are keyed."""
    repo = _repo(tmp_path / "big")
    _git(repo, "remote", "add", "origin", "https://example.invalid/team/big.git")
    config = repo / ".git" / "config"
    _append(
        config,
        "".join(
            f'[branch "b{i}"]\n\tremote = origin\n\tmerge = refs/heads/b{i}\n'
            for i in range(30000)
        ),
    )
    assert config.stat().st_size > gitconfig._CONFIG_LIMIT
    assert "example.invalid/team/big" in _keys(repo)
    assert not gitconfig._config_values(str(config)).complete


def test_includes_are_followed_as_git_follows_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote in an included file is keyed; an ``includeIf`` holds
    exactly where git's does, for absolute, globbed, relative, ``**``,
    bracketed, home-relative and case-folded ``gitdir:`` patterns and an
    unknown condition."""
    repo = _repo(tmp_path / "work" / "proj")
    monkeypatch.setenv("HOME", str((tmp_path / "work").resolve()))
    gd = repo / ".git"
    base = tmp_path.resolve().as_posix()
    conditions = {
        "abs": f"gitdir:{base}/work/",
        "glob": f"gitdir:{base}/*/proj/.git",
        "relative": "gitdir:proj/.git",
        "deep": f"gitdir:{base}/**/.git",
        "bracket": f"gitdir:{base}/wor[jk]/proj/.git",
        "home": "gitdir:~/proj/",
        "icase": f"gitdir/i:{base.upper()}/WORK/",
        "miss": f"gitdir:{base}/other/",
        "one-level": f"gitdir:{base}/*/.git",
        "case-miss": f"gitdir:{base.upper()}/WORK/",
        "unknown": "nosuch:anything",
    }
    (gd / "plain.inc").write_text(
        '[remote "plain"]\n\turl = https://example.invalid/a/plain.git\n'
    )
    text = "[include]\n\tpath = plain.inc\n"
    for name, cond in conditions.items():
        (gd / f"{name}.inc").write_text(
            f'[remote "{name}"]\n\turl = https://example.invalid/a/{name}.git\n'
        )
        text += f'[includeIf "{cond}"]\n\tpath = {name}.inc\n'
    _append(gd / "config", text)

    git_remotes = _git_remotes(repo)
    assert {"plain", "abs", "glob", "relative", "deep", "home", "icase"} <= set(
        git_remotes
    ), git_remotes
    assert not {"miss", "one-level", "case-miss", "unknown"} & set(git_remotes)
    expected = {remote_key(u) for urls in git_remotes.values() for u in urls}
    assert _keys(repo) == expected
    values = gitconfig._config_values(str(gd / "config"), git_dir=str(gd))
    ours = {
        sub for (section, sub, key) in values if section == "remote" and key == "url"
    }
    assert ours == set(git_remotes)
    assert values.complete and not values.undecided


def test_a_remote_is_keyed_as_insteadof_rewrites_it_too(tmp_path: Path) -> None:
    """A remote spelled with an alias keys as the URL git fetches from (the
    longest matching prefix, global or the repository's own rule), and as
    written, so a checkout keyed on the alias before still matches."""
    repo = _repo(tmp_path / "alias")
    _git(repo, "remote", "add", "origin", "gh:acme/lib")
    _git(repo, "remote", "add", "fork", "gh:acme-team/lib")
    _git(repo, "remote", "add", "up", "lc:team/tool")
    _append(
        Path(os.environ["GIT_CONFIG_GLOBAL"]),
        '[url "https://github.com/"]\n\tinsteadOf = gh:\n'
        '[url "https://mirror.example.invalid/acme/"]\n\tinsteadOf = gh:acme/\n',
    )
    _git(repo, "config", "url.https://local.example.invalid/.insteadOf", "lc:")
    keys = _keys(repo)
    for name in ("origin", "fork", "up"):
        fetched = remote_key(_git(repo, "remote", "get-url", name))
        assert fetched in keys, (name, fetched, keys)
    assert {"gh/acme/lib", "gh/acme-team/lib", "lc/team/tool"} <= keys
    assert "mirror.example.invalid/acme/lib" in keys
    assert "github.com/acme/lib" not in keys


def test_config_worktree_is_read_where_git_reads_it(tmp_path: Path) -> None:
    """ENG-207's review: where ``extensions.worktreeConfig`` is true, git
    reads the git directory's ``config.worktree`` after the common config,
    and a remote or an insteadOf rule set there, or in a file it includes,
    is the repository's; the capture read the common config alone. It
    reads both now (`location_git._repository_config`), and a kept capture
    is read again once ``config.worktree`` or a file it includes changes,
    and served while neither does. Where the extension is not set git
    reads no ``config.worktree``, and neither does the capture."""
    repo = _repo(tmp_path / "wt")
    _git(repo, "remote", "add", "origin", "gh:acme/lib")
    worktree_config = repo / ".git" / "config.worktree"
    rules = tmp_path / "rules.inc"
    rules.write_text("[user]\n\tname = Test\n", encoding="utf-8")
    worktree_config.write_text(
        '[remote "wt"]\n\turl = https://example.invalid/team/wt\n'
        f"[include]\n\tpath = {rules.as_posix()}\n",
        encoding="utf-8",
    )
    assert "wt" not in _git_remotes(repo)
    assert "example.invalid/team/wt" not in _keys(repo)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    assert "wt" in _git_remotes(repo)
    assert "example.invalid/team/wt" in _keys(repo)

    kept = location.capture(str(repo), write_token=False)
    assert location.capture(str(repo), write_token=False) is kept
    _append(rules, '[url "https://github.com/"]\n\tinsteadOf = gh:\n')
    fetched = remote_key(_git(repo, "remote", "get-url", "origin"))
    assert fetched == "github.com/acme/lib"
    rewritten = location.capture(str(repo), write_token=False)
    assert fetched in rewritten.remote_keys
    assert location.capture(str(repo), write_token=False) is rewritten
    _append(worktree_config, '[remote "more"]\n\turl = https://example.invalid/m\n')
    assert "example.invalid/m" in _git_remotes(repo)["more"][0]
    added = location.capture(str(repo), write_token=False)
    assert "example.invalid/m" in added.remote_keys
    assert location.capture(str(repo), write_token=False) is added


def test_a_gitfile_target_s_working_tree_is_read_as_git_reads_its_format(
    tmp_path: Path,
) -> None:
    """A ``.git`` file naming a ``--separate-git-dir`` is followed where
    git roots the tree at its folder, and git reads a git directory's
    working tree from its config file alone (setup.c's
    ``read_repository_format``, ENG-211): an include, under a condition or
    not, that sets ``core.worktree`` moves nothing, and git's toplevel says
    so, while a ``core.worktree`` in the file naming another folder is
    refused whatever an include sets after it. A config past the 1 MiB
    remote bound is read whole for it; one past `gitconfig._FORMAT_LIMIT`
    is refused (fail closed)."""
    tree, sep, other = tmp_path / "tree", tmp_path / "sep", tmp_path / "other"
    other.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={sep}", str(tree))
    config = sep / "config"
    original = config.read_text(encoding="utf-8")

    def refusal(extra: str) -> str | None:
        config.write_text(original + extra, encoding="utf-8")
        return gitfile_rule.gitfile_refusal(str(tree), str(tree / ".git"), str(sep))

    def toplevel() -> Path:
        return Path(_git(tree, "rev-parse", "--show-toplevel")).resolve()

    (sep / "tree.inc").write_text(f"[core]\n\tworktree = {other.as_posix()}\n")
    (sep / "back.inc").write_text(f"[core]\n\tworktree = {tree.as_posix()}\n")
    assert refusal("") is None
    for include in ("[include]", '[includeIf "onbranch:main"]'):
        assert refusal(f"{include}\n\tpath = tree.inc\n") is None
        assert toplevel() == tree.resolve()
    named = f"[core]\n\tworktree = {other.as_posix()}\n[include]\n\tpath = back.inc\n"
    assert refusal(named) is not None
    assert toplevel() == other.resolve()
    padding = "".join(f'[branch "b{i}"]\n\tremote = origin\n' for i in range(40000))
    assert len(padding) > 1 << 20
    assert refusal(padding) is None
    assert refusal(padding + f"[core]\n\tworktree = {other.as_posix()}\n") is not None
    over = "".join(f'[branch "b{i}"]\n\tremote = origin\n' for i in range(70000))
    assert len(over) > gitconfig._FORMAT_LIMIT
    assert "larger than" in (refusal(over) or "")


def test_an_include_naming_a_fifo_does_not_wait(tmp_path: Path) -> None:
    """An include that names a FIFO is not waited on: the open does not
    block, and the config reads as incomplete."""
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows")
    repo = _repo(tmp_path / "fifo")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    _append(repo / ".git" / "config", f"[include]\n\tpath = {fifo}\n")
    answer: list[gitconfig.ConfigValues] = []
    reader = threading.Thread(
        target=lambda: answer.append(
            gitconfig._config_values(str(repo / ".git" / "config"))
        ),
        daemon=True,
    )
    reader.start()
    reader.join(5)
    assert answer, "the read waited on the FIFO"
    assert not answer[0].complete


#: `gitconfig._INCLUDE_FILES`, read so the module loads where it is not
#: defined (the fail-before run).
_INCLUDE_FILES: int = getattr(gitconfig, "_INCLUDE_FILES", 64)


def _counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The config files `_config_values` reads, in order."""
    read: list[str] = []
    real = gitconfig._config_bytes

    def counted(
        path: str,
        out: gitconfig.ConfigValues,
        identity: list[tuple[int, int]] | None = None,
    ) -> bytes | None:
        read.append(path)
        if identity is None:
            return real(path, out)
        return real(path, out, identity)

    monkeypatch.setattr(gitconfig, "_config_bytes", counted)
    return read


def test_a_config_including_itself_stops_at_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M D3: a config naming itself in four ``include.path`` lines
    was read 1,398,101 times (38 s, every value kept), where git stops at
    its first include past its depth. The read now stops at the cycle, in
    well under a second and within `_INCLUDE_FILES` files; the remote read
    before stands, once, and the config reads as incomplete."""
    config = tmp_path / "config"
    config.write_text(
        '[remote "origin"]\n\turl = https://example.invalid/a/b.git\n'
        + "[include]\n\tpath = config\n" * 4,
        encoding="utf-8",
    )
    read = _counted(monkeypatch)
    answer: list[tuple[gitconfig.ConfigValues, float]] = []

    def run() -> None:
        start = time.monotonic()
        values = gitconfig._config_values(str(config))
        answer.append((values, time.monotonic() - start))

    reader = threading.Thread(target=run, daemon=True)
    reader.start()
    reader.join(5)
    assert answer, f"the read was still going after 5 s ({len(read)} files read)"
    values, elapsed = answer[0]
    assert elapsed < 0.5
    assert len(read) <= _INCLUDE_FILES
    assert not values.complete
    assert values[("remote", "origin", "url")] == ["https://example.invalid/a/b.git"]


def test_includes_fanning_out_stop_at_the_file_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M D3: ten files, each including the next twice, are 1,023
    reads within git's depth; the read stops at `_INCLUDE_FILES` files,
    keeps what it read (the first chain down, read whole) and reads as
    incomplete, where git reads more."""
    for i in range(10):
        nxt = f"[include]\n\tpath = f{i + 1}\n" * 2 if i < 9 else ""
        (tmp_path / f"f{i}").write_text(
            f'[remote "r{i}"]\n\turl = https://example.invalid/a/{i}.git\n' + nxt,
            encoding="utf-8",
        )
    read = _counted(monkeypatch)

    values = gitconfig._config_values(str(tmp_path / "f0"))

    assert len(read) == _INCLUDE_FILES
    assert not values.complete
    remotes = {sub for (section, sub, _k) in values if section == "remote"}
    assert remotes == {f"r{i}" for i in range(10)}


# ---------------------------------------------------------------------------
# The system config, and a cached capture's config files (ENG-207)
# ---------------------------------------------------------------------------

_SYSTEM_RULE = '[url "https://system.example.invalid/"]\n\tinsteadOf = sy:\n'


def _read_system(monkeypatch: pytest.MonkeyPatch, path: str | None) -> None:
    """Have the system config read: the file `path` names, or with None
    the one git names."""
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    if path is None:
        monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    else:
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", path)


def test_the_system_config_is_read_as_git_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207: a remote alias set in the system config, or in a file it
    includes, keys the remote as git fetches it, and an equal prefix in the
    global config gives way to the system's, which git reads first. Under
    GIT_CONFIG_NOSYSTEM, or with GIT_CONFIG_SYSTEM empty, git reads no
    system config and neither does the capture."""
    repo = _repo(tmp_path / "sys")
    _git(repo, "remote", "add", "origin", "sy:acme/lib")
    _git(repo, "remote", "add", "up", "inc:team/tool")
    system = tmp_path / "system-gitconfig"
    system.write_text(_SYSTEM_RULE + "[include]\n\tpath = system.inc\n")
    (tmp_path / "system.inc").write_text(
        '[url "https://included.example.invalid/"]\n\tinsteadOf = inc:\n'
    )
    _append(
        Path(os.environ["GIT_CONFIG_GLOBAL"]),
        '[url "https://global.example.invalid/"]\n\tinsteadOf = sy:\n',
    )

    def agrees() -> set[str]:
        keys = _keys(repo)
        for name in ("origin", "up"):
            fetched = remote_key(_git(repo, "remote", "get-url", name))
            assert fetched in keys, (name, fetched, keys)
        return keys

    _read_system(monkeypatch, str(system))
    keys = agrees()
    assert {
        "system.example.invalid/acme/lib",
        "included.example.invalid/team/tool",
    } <= keys
    assert "global.example.invalid/acme/lib" not in keys
    for name, value in (("GIT_CONFIG_NOSYSTEM", "1"), ("GIT_CONFIG_SYSTEM", "")):
        _read_system(monkeypatch, str(system))
        monkeypatch.setenv(name, value)
        keys = agrees()
        assert "global.example.invalid/acme/lib" in keys
        assert "system.example.invalid/acme/lib" not in keys
        assert "included.example.invalid/team/tool" not in keys


def test_an_empty_global_config_variable_names_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207: git reads no global config where GIT_CONFIG_GLOBAL is set
    empty, not ``~/.gitconfig``, which the capture read; with it unset,
    the home folder's files are read, as git reads them."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[url "https://home.example.invalid/"]\n\tinsteadOf = hm:\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    repo = _repo(tmp_path / "empty-global")
    _git(repo, "remote", "add", "origin", "hm:acme/lib")
    for value, read in (("", False), (None, True)):
        if value is None:
            monkeypatch.delenv("GIT_CONFIG_GLOBAL")
        else:
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", value)
        fetched = remote_key(_git(repo, "remote", "get-url", "origin"))
        assert (fetched == "home.example.invalid/acme/lib") is read
        assert fetched in _keys(repo)
        assert ("home.example.invalid/acme/lib" in _keys(repo)) is read


def _listing(*entries: tuple[str, str]) -> bytes:
    """What ``git config --list --show-scope --show-origin -z`` prints for
    `entries`, each (scope, origin), with one key and value each."""
    return b"".join(
        f"{scope}\0{origin}\0probe.key\nvalue\0".encode() for scope, origin in entries
    )


class _FakeGit:
    """`_git.run_git` answering `gitsystem`'s two questions with `named`
    and `own` (None: git does not answer), every other call run by git;
    the calls asked, each (args, keywords), are kept in `asked`, and
    `during` (where set) runs as the first is asked: another process's
    turn."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, binary: Path) -> None:
        self.named: str | None = ""
        self.own: list[str] | None = []
        self.asked: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.during: Callable[[], None] | None = None
        self.binary = binary
        binary.write_text("a git")
        real = git_runner.run_git

        def run_git(args: Any, **kw: Any) -> Any:
            args = tuple(args)
            if args == gitsystem._VAR:
                if self.during is not None:
                    self.during()
                said: Any = self.named
            elif args == gitsystem._LIST:
                said = (
                    None
                    if self.own is None
                    else _listing(*(("unknown", f"file:{p}") for p in self.own))
                )
            else:
                return real(args, **kw)
            self.asked.append((args, kw))
            return (
                None if said is None else subprocess.CompletedProcess(args, 0, said, "")
            )

        monkeypatch.setattr(git_runner, "run_git", run_git)
        monkeypatch.setattr(shutil, "which", lambda _name: str(binary))


def test_the_system_config_is_the_file_git_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENG-207: with neither variable set the system config files are the
    ones the git on PATH reads: the file it names (``git var
    GIT_CONFIG_SYSTEM``, git 2.42 and later) after the files its build
    reads of its own, which git lists at system or unknown scope with the
    system and global files at the null device (Apple's developer
    directory's gitconfig); git's own answers, asked here directly."""
    _read_system(monkeypatch, None)
    asked = subprocess.run(
        ["git", "var", "GIT_CONFIG_SYSTEM"], capture_output=True, text=True
    )
    env = {
        **os.environ,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    listed = subprocess.run(
        list(("git", *gitsystem._LIST)),
        capture_output=True,
        env=env,
        cwd=os.path.abspath(os.sep),
    )
    found = gitsystem.sources()
    if asked.returncode == 0 and asked.stdout.strip():
        assert found.system == asked.stdout.strip()
    if listed.returncode == 0:
        assert found.own == gitsystem._own_files(listed.stdout)
    assert location_git._system_configs() == [
        *found.own,
        *([found.system] if found.system else []),
    ]


def test_the_files_a_git_build_reads_of_its_own_are_read_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review: Apple's git reads its developer directory's
    gitconfig before the system file, and GIT_CONFIG_SYSTEM replaces only
    the system file; GIT_CONFIG_NOSYSTEM, read as git reads a boolean
    variable, suppresses both. An equal ``insteadOf`` prefix in the two is
    the build's, which git reads first. git is asked at the root, without
    the caller's GIT_CONFIG_SYSTEM, the listing with the system and global
    files at the null device."""
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    own = tmp_path / "vendor-gitconfig"
    own.write_text('[url "https://vendor.example.invalid/"]\n\tinsteadOf = sy:\n')
    named = tmp_path / "named-gitconfig"
    named.write_text(_SYSTEM_RULE)
    fake.named, fake.own = f"{named}\n", [str(own)]
    _read_system(monkeypatch, None)
    repo = _repo(tmp_path / "named")
    _git(repo, "remote", "add", "origin", "sy:acme/lib")
    keys = _keys(repo)
    assert "vendor.example.invalid/acme/lib" in keys
    assert "system.example.invalid/acme/lib" not in keys
    assert location_git._system_configs() == [str(own), str(named)]
    (var, var_kw), (listing, list_kw) = fake.asked
    assert (var, listing) == (gitsystem._VAR, gitsystem._LIST)
    for kw in (var_kw, list_kw):
        assert kw["cwd"] == os.path.abspath(os.sep)
        assert "GIT_CONFIG_SYSTEM" in kw["unset"]
    assert list_kw["env"] == {
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    other = tmp_path / "other-gitconfig"
    _read_system(monkeypatch, str(other))
    assert location_git._system_configs() == [str(own), str(other)]
    for value, read in (
        ("1", False),
        ("true", False),
        ("Yes", False),
        ("2", False),
        ("not a boolean", False),
        ("0", True),
        ("false", True),
        ("", True),
    ):
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", value)
        expected = [str(own), str(other)] if read else []
        assert location_git._system_configs() == expected, value
    assert len(fake.asked) == 2


def _record(state: Path) -> dict[str, Any]:
    return json.loads((state / gitsystem.RECORD).read_text())


def test_git_is_asked_once_for_a_path_and_the_answer_kept_in_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review, R3: a hook client is a new process each prompt,
    and asking git cost each one a process. The answer is kept in the
    state directory for the PATH (as its digest), so a new process (the
    caches cleared) asks nothing while it stands; git is asked again where
    the binary found on that PATH changed, the answer is older than
    `REASK_SECONDS`, another PATH asks, or the record is not one. Both
    questions together are bound by `ASK_SECONDS`, the capture's fetch
    budget."""
    assert gitsystem.ASK_SECONDS == location_git.FETCH_BUDGET
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    fake.named, fake.own = "/opt/example/etc/gitconfig\n", ["/opt/example/vendor"]
    answer = gitsystem.Sources(("/opt/example/vendor",), "/opt/example/etc/gitconfig")

    def asks() -> int:
        before = len(fake.asked)
        _caches.clear_all()
        assert gitsystem.sources() == answer
        return len(fake.asked) - before

    assert asks() == 2
    for _ in range(2):
        assert gitsystem.sources() == answer
    assert asks() == 0
    (_v, var_kw), (_l, list_kw) = fake.asked
    assert var_kw["timeout"] <= gitsystem.ASK_SECONDS
    assert list_kw["timeout"] <= gitsystem.ASK_SECONDS
    record = (state / gitsystem.RECORD).read_text()
    assert os.environ["PATH"] not in record

    fake.binary.write_text("another git")
    assert asks() == 2
    assert asks() == 0
    raw = _record(state)
    raw["entries"][0]["asked"] -= gitsystem.REASK_SECONDS
    (state / gitsystem.RECORD).write_text(json.dumps(raw))
    assert asks() == 2
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(tmp_path))
    assert asks() == 2
    assert len(_record(state)["entries"]) == 2
    (state / gitsystem.RECORD).write_text("not a record")
    assert asks() == 2
    assert asks() == 0


def test_a_git_that_does_not_answer_reads_as_the_fallback_until_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review: a git that does not answer within the bound, or
    none on PATH, reads as naming ``/etc/gitconfig`` (none on Windows)
    and no file of its own, for `RETRY_SECONDS`, in this process and the
    next, and is then asked again; it is never kept as git's answer."""
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    fallback = gitsystem.Sources((), None if os.name == "nt" else "/etc/gitconfig")
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    fake.named = None
    assert gitsystem.sources() == fallback
    _caches.clear_all()
    assert gitsystem.sources() == fallback
    assert len(fake.asked) == 1
    raw = _record(state)
    assert raw["entries"][0]["ok"] is False
    raw["entries"][0]["asked"] -= gitsystem.RETRY_SECONDS
    (state / gitsystem.RECORD).write_text(json.dumps(raw))
    _caches.clear_all()
    fake.named, fake.own = "/opt/example/etc/gitconfig\n", None
    assert gitsystem.sources() == fallback
    assert len(fake.asked) == 3

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    assert gitsystem.sources() == fallback
    assert len(fake.asked) == 3


def _aged(state: Path, field: str, seconds: float) -> None:
    """The record's newest entry's `field` moved `seconds` back."""
    raw = _record(state)
    raw["entries"][0][field] -= seconds
    (state / gitsystem.RECORD).write_text(json.dumps(raw))


def test_a_failed_ask_keeps_the_answer_git_gave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review, R6: the hourly ask timing out under load, or a
    racing process whose ask failed, recorded the fallback over git's
    answer for a minute. A failed ask now keeps the last answer git gave
    for the PATH, in this process and the record, and asks again after
    `RETRY_SECONDS`; a failed answer is never recorded over a good one;
    and an ask that fails while another process records git's answer
    takes that answer."""
    state = Path(os.environ["BETTERMEMORY_STATE_DIR"])
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    fake.named, fake.own = "/opt/example/etc/gitconfig\n", ["/opt/example/vendor"]
    good = gitsystem.Sources(("/opt/example/vendor",), "/opt/example/etc/gitconfig")
    assert gitsystem.sources() == good
    asked = len(fake.asked)

    fake.named = None
    _aged(state, "asked", gitsystem.REASK_SECONDS)
    _caches.clear_all()
    assert gitsystem.sources() == good
    assert len(fake.asked) == asked + 1
    entry = _record(state)["entries"][0]
    assert entry["ok"] is True and entry["system"] == "/opt/example/etc/gitconfig"
    assert entry["tried"] is not None
    _caches.clear_all()
    assert gitsystem.sources() == good
    assert len(fake.asked) == asked + 1
    _aged(state, "tried", gitsystem.RETRY_SECONDS)
    _caches.clear_all()
    assert gitsystem.sources() == good
    assert len(fake.asked) == asked + 2

    key = gitsystem._key(os.environ.get("PATH"))
    fallback = gitsystem.Sources((), "/etc/gitconfig")
    gitsystem._record(gitsystem._Answer(key, None, None, time.time(), False, fallback))
    assert _record(state)["entries"][0]["ok"] is True

    fake.named, fake.own = "/opt/other/etc/gitconfig\n", []
    _aged(state, "tried", gitsystem.RETRY_SECONDS)
    _caches.clear_all()
    assert gitsystem.sources() == gitsystem.Sources((), "/opt/other/etc/gitconfig")
    assert _record(state)["entries"][0]["tried"] is None

    racer = gitsystem._Answer(
        key,
        str(fake.binary),
        gitconfig.file_stamp(str(fake.binary)),
        time.time(),
        True,
        good,
    )
    (state / gitsystem.RECORD).unlink()
    _caches.clear_all()
    fake.named = None
    fake.during = lambda: gitsystem._record(racer)
    assert gitsystem.sources() == good
    assert _record(state)["entries"][0]["system"] == "/opt/example/etc/gitconfig"


def test_a_capture_taken_under_the_fallback_is_read_again_once_git_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review, R7: a capture kept while git did not answer read
    the fallback's system files, and was served for its whole lifetime
    after git answered, since only the stamps of the files it read were
    checked. A kept capture is now checked against the machine's config
    files git names, and read again once they are others."""
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    vendor = tmp_path / "vendor-gitconfig"
    vendor.write_text('[url "https://vendor.example.invalid/"]\n\tinsteadOf = sy:\n')
    fake.named = None
    _read_system(monkeypatch, None)
    repo = _repo(tmp_path / "cached")
    _git(repo, "remote", "add", "origin", "sy:acme/lib")
    first = location.capture(str(repo), write_token=False)
    assert "vendor.example.invalid/acme/lib" not in first.remote_keys
    assert location.capture(str(repo), write_token=False) is first

    fake.named, fake.own = f"{tmp_path / 'none'}\n", [str(vendor)]
    monkeypatch.setattr(gitsystem, "RETRY_SECONDS", 0.0)
    again = location.capture(str(repo), write_token=False)
    assert again is not first
    assert "vendor.example.invalid/acme/lib" in again.remote_keys
    monkeypatch.setattr(gitsystem, "RETRY_SECONDS", 60.0)
    assert location.capture(str(repo), write_token=False) is again


def test_the_developer_directory_names_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review: Apple's ``/usr/bin/git`` is a shim whose stamp
    does not change when ``xcode-select --switch`` or DEVELOPER_DIR picks
    another developer directory, whose git reads another gitconfig of its
    own. On macOS both are in the record's key (one readlink, no
    process), so a switch is asked of git at once, and switching back
    asks nothing; elsewhere neither is read."""
    fake = _FakeGit(monkeypatch, tmp_path / "git")
    link = tmp_path / "xcode_select_link"
    link.symlink_to(tmp_path / "Xcode")
    monkeypatch.setattr(gitsystem, "XCODE_SELECT_LINK", str(link))
    monkeypatch.setattr(gitsystem, "_MACOS", True)
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)

    def asks() -> int:
        before = len(fake.asked)
        gitsystem.sources()
        return len(fake.asked) - before

    assert asks() == 2
    assert asks() == 0
    link.unlink()
    link.symlink_to(tmp_path / "CommandLineTools")
    assert asks() == 2
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "Beta"))
    assert asks() == 2
    monkeypatch.delenv("DEVELOPER_DIR")
    link.unlink()
    link.symlink_to(tmp_path / "Xcode")
    _caches.clear_all()
    assert asks() == 0

    monkeypatch.setattr(gitsystem, "_MACOS", False)
    assert gitsystem._developer() == ""
    assert asks() == 2
    link.unlink()
    assert asks() == 0


@pytest.mark.skipif(os.name != "posix", reason="the null device is a file on POSIX")
def test_a_kept_capture_outlives_a_write_to_the_null_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review, round 3: GIT_CONFIG_GLOBAL at the null device,
    as CI and this suite set it. The device's times move with every write
    to it on macOS, and a capture kept against its stamp was read again
    at random. A file that is no regular file is stamped by its inode and
    type alone (`gitconfig.file_stamp`): the reader takes nothing from it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repo = _repo(tmp_path / "cached")
    _git(repo, "remote", "add", "origin", "https://example.invalid/acme/lib")
    first = location.capture(str(repo), write_token=False)
    assert os.devnull in [path for path, _stamp in first.config_files]
    for _ in range(3):
        subprocess.run(["sh", "-c", f"echo moved > {os.devnull}"], check=True)
        assert location.capture(str(repo), write_token=False) is first
    assert gitconfig.file_stamp(os.devnull) == gitconfig.file_stamp(os.devnull)


def test_the_files_a_build_reads_are_read_off_gits_listing() -> None:
    """`gitsystem._own_files`: the files at system or unknown scope, in
    git's order, each once; never a global, local or command line entry,
    nor an origin that is not an absolute file."""
    root = os.path.abspath(os.sep)
    vendor = os.path.join(root, "vendor", "gitconfig")
    data = os.path.join(root, "ProgramData", "Git", "config")
    listing = _listing(
        ("unknown", f"file:{vendor}"),
        ("unknown", f"file:{vendor}"),
        ("system", f"file:{data}"),
        ("global", f"file:{os.path.join(root, 'home', '.gitconfig')}"),
        ("local", "file:.git/config"),
        ("command", "command line:"),
        ("unknown", "file:relative/gitconfig"),
        ("unknown", "standard input:"),
    )
    assert gitsystem._own_files(listing) == (vendor, data)
    assert gitsystem._own_files(b"") == ()
    assert gitsystem._own_files(b"garbage") == ()


def test_a_change_to_a_config_file_read_reaches_a_cached_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207: a capture is kept against every config file read for its
    remotes, so a change to the global or the system config, or the
    creation of a file either includes or the repository's own config
    includes, is read at the next capture, where it waited for the
    repository's own files to change; unchanged, the kept capture is
    served."""
    repo = _repo(tmp_path / "cached")
    _git(repo, "remote", "add", "origin", "gh:acme/lib")
    system = tmp_path / "system-gitconfig"
    system.write_text("")
    _read_system(monkeypatch, str(system))
    global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    _append(global_config, "[include]\n\tpath = rules.inc\n")

    def keys() -> set[str]:
        return set(location.capture(str(repo), write_token=False).remote_keys)

    first = location.capture(str(repo), write_token=False)
    assert location.capture(str(repo), write_token=False) is first
    assert "github.com/acme/lib" not in first.remote_keys
    _append(global_config, '[url "https://github.com/"]\n\tinsteadOf = gh:\n')
    assert "github.com/acme/lib" in keys()
    _append(
        system, '[url "https://mirror.example.invalid/acme/"]\n\tinsteadOf = gh:acme/\n'
    )
    assert "mirror.example.invalid/acme/lib" in keys()
    (tmp_path / "rules.inc").write_text(
        '[url "https://inc.example.invalid/lib"]\n\tinsteadOf = gh:acme/lib\n'
    )
    assert "inc.example.invalid/lib" in keys()

    # Outside the git directory, whose own stamp would see the file made.
    extra = tmp_path / "extra.inc"
    _git(repo, "config", "include.path", str(extra))
    kept = location.capture(str(repo), write_token=False)
    assert location.capture(str(repo), write_token=False) is kept
    extra.write_text('[remote "more"]\n\turl = https://example.invalid/team/more\n')
    assert "example.invalid/team/more" in keys()
    kept = location.capture(str(repo), write_token=False)
    assert location.capture(str(repo), write_token=False) is kept
    _append(
        extra,
        '[url "https://more.example.invalid/"]\n'
        "\tinsteadOf = https://example.invalid/team/\n",
    )
    assert "more.example.invalid/more" in keys()
