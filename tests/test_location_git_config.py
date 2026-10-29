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
`_INCLUDE_FILES` files read in all.

Each expectation is git's own answer where git gives one: the remotes
``git config`` lists, the URL ``git remote get-url`` fetches from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bettermemory import location, location_git
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
    assert config.stat().st_size > location_git._CONFIG_LIMIT
    assert "example.invalid/team/big" in _keys(repo)
    assert not location_git._config_values(str(config)).complete


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
    values = location_git._config_values(str(gd / "config"), git_dir=str(gd))
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


def test_a_gitfile_target_whose_config_cannot_be_read_whole_is_refused(
    tmp_path: Path,
) -> None:
    """A ``.git`` file naming a ``--separate-git-dir`` is followed; once its
    config moves the working tree elsewhere through an include, it is
    refused, as it is when such an include stands under a condition not
    evaluated here, or when the config is over the bound. An include under
    such a condition that does not name the tree leaves it followed."""
    tree, sep, other = tmp_path / "tree", tmp_path / "sep", tmp_path / "other"
    other.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={sep}", str(tree))
    config = sep / "config"
    original = config.read_text(encoding="utf-8")

    def refusal(extra: str) -> str | None:
        config.write_text(original + extra, encoding="utf-8")
        return location_git.gitfile_refusal(str(tree), str(tree / ".git"), str(sep))

    (sep / "tree.inc").write_text(f"[core]\n\tworktree = {other.as_posix()}\n")
    (sep / "user.inc").write_text("[user]\n\tname = Someone\n")
    assert refusal("") is None
    assert refusal("[include]\n\tpath = tree.inc\n") is not None
    assert refusal('[includeIf "onbranch:main"]\n\tpath = tree.inc\n') is not None
    assert refusal('[includeIf "onbranch:main"]\n\tpath = user.inc\n') is None
    padding = "".join(f'[branch "b{i}"]\n\tremote = origin\n' for i in range(40000))
    assert refusal(padding) is not None


def test_an_include_naming_a_fifo_does_not_wait(tmp_path: Path) -> None:
    """An include that names a FIFO is not waited on: the open does not
    block, and the config reads as incomplete."""
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows")
    repo = _repo(tmp_path / "fifo")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    _append(repo / ".git" / "config", f"[include]\n\tpath = {fifo}\n")
    answer: list[location_git.ConfigValues] = []
    reader = threading.Thread(
        target=lambda: answer.append(
            location_git._config_values(str(repo / ".git" / "config"))
        ),
        daemon=True,
    )
    reader.start()
    reader.join(5)
    assert answer, "the read waited on the FIFO"
    assert not answer[0].complete


#: `location_git._INCLUDE_FILES`, read so the module loads where it is not
#: defined (the fail-before run).
_INCLUDE_FILES: int = getattr(location_git, "_INCLUDE_FILES", 64)


def _counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The config files `_config_values` reads, in order."""
    read: list[str] = []
    real = location_git._config_bytes

    def counted(
        path: str,
        out: location_git.ConfigValues,
        identity: list[tuple[int, int]] | None = None,
    ) -> bytes | None:
        read.append(path)
        if identity is None:
            return real(path, out)
        return real(path, out, identity)

    monkeypatch.setattr(location_git, "_config_bytes", counted)
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
    answer: list[tuple[location_git.ConfigValues, float]] = []

    def run() -> None:
        start = time.monotonic()
        values = location_git._config_values(str(config))
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

    values = location_git._config_values(str(tmp_path / "f0"))

    assert len(read) == _INCLUDE_FILES
    assert not values.complete
    remotes = {sub for (section, sub, _k) in values if section == "remote"}
    assert remotes == {f"r{i}" for i in range(10)}
