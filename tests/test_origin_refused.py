"""The origin of a caller in a repository git refuses to read (ENG-175).

Git refuses a repository another account owns while ``safe.directory`` does
not name it (a dev-container bind mount, an SMB or NFS share with mapped
ids, a colleague's shared tree): ``git rev-parse --show-toplevel`` exits 128
with "detected dubious ownership". `origin.capture` read that exit as "not a
repository" and returned a caller standing nowhere, determinate, while
membership's location capture (`githead.locate`) placed the same caller in
the repository; `memory_write` with claims was then refused "the caller is
not inside a git worktree". The exit means "no repository" only where the
files hold none; where they hold one, the root comes from the files and the
origin is indeterminate.

The refusal is made two ways: by git itself under
``GIT_TEST_ASSUME_DIFFERENT_OWNER`` (the switch git's own tests and
`githead` model), where this git honours it, and by a runner that answers
the first probe with git's exit and message, which holds on every platform.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import githead, origin
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_REMOTE = "https://example.invalid/acme/shared.git"

_DUBIOUS = (
    "fatal: detected dubious ownership in repository at '{root}'\n"
    "To add an exception for this directory, call:\n"
)


@pytest.fixture(autouse=True)
def git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited discovery override, a ceiling above ``tmp_path``, no
    global or system configuration (so no ``safe.directory``), and a fixed
    identity for commits."""
    for name in (*githead._DISCOVERY_ENV, "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


def _run(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(path: Path) -> Path:
    """A repository with a remote and one commit holding ``src/a.py``; its
    root as `capture` records a root git names."""
    (path / "src").mkdir(parents=True)
    _run(path, "init", "-q", "-b", "main")
    (path / "src" / "a.py").write_text("TIMEOUT = 30\n", encoding="utf-8")
    _run(path, "add", "-A")
    _run(path, "commit", "-q", "-m", "one")
    _run(path, "remote", "add", "origin", _REMOTE)
    return Path(_run(path, "rev-parse", "--show-toplevel")).resolve()


def _assume_another_owner(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Have git refuse `repo` as another account's; skip where this git does
    not honour the switch."""
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    refused = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if refused.returncode == 0 or "dubious ownership" not in refused.stderr:
        pytest.skip("this git does not honour GIT_TEST_ASSUME_DIFFERENT_OWNER")


def _refuse_every_probe(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> list[tuple[str, ...]]:
    """Answer every git call origin makes with git's dubious-ownership exit,
    and record the calls."""
    calls: list[tuple[str, ...]] = []

    def refused(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return subprocess.CompletedProcess(
            ["git", *args], 128, stdout="", stderr=_DUBIOUS.format(root=root)
        )

    monkeypatch.setattr(origin, "_git_result", refused)
    return calls


def _probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = origin._git_result

    def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git_result", spy)
    return calls


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_git_s_ownership_refusal_keeps_the_root_the_files_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git itself refuses the repository: the capture records the root
    membership's locate reads, is indeterminate, runs no probe after the
    first (git would refuse the remote and the branch too) and is not
    kept."""
    root = _repo(tmp_path / "shared")
    _assume_another_owner(monkeypatch, root)
    calls = _probes(monkeypatch)
    located = githead.locate(root / "src")
    assert located is not None, "premise: the files hold the repository"
    got = origin.capture(root / "src")
    assert got.worktree_root == str(root)
    assert root.samefile(located.worktree_root)
    assert got.git_indeterminate is True
    assert "dubious ownership" in (got.git_refusal or "")
    assert (got.repo, got.branch) == (None, None)
    assert calls == [("rev-parse", "--show-toplevel")]
    assert str((root / "src").resolve()) not in origin._ORIGIN_CACHE


def test_a_refusal_with_git_s_exit_and_message_keeps_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same refusal answered by the runner with git's exit and message,
    on every platform: `locate` answers off POSIX and for another account's
    repository alike."""
    root = _repo(tmp_path / "shared")
    calls = _refuse_every_probe(monkeypatch, root)
    got = origin.capture(root)
    assert got.worktree_root == str(root)
    assert got.git_indeterminate is True
    assert "dubious ownership" in (got.git_refusal or "")
    assert (got.repo, got.branch) == (None, None)
    assert calls == [("rev-parse", "--show-toplevel")]
    assert not origin._ORIGIN_CACHE


def test_where_the_files_hold_no_repository_the_exit_is_still_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside any repository the same exit is git's "not a git
    repository": nowhere, determinate, and kept as an answer."""
    plain = tmp_path / "plain"
    plain.mkdir()
    _refuse_every_probe(monkeypatch, plain)
    got = origin.capture(plain)
    assert (got.worktree_root, got.repo, got.branch) == (None, None, None)
    assert got.git_indeterminate is False
    assert got.git_refusal is None
    assert str(plain.resolve()) in origin._ORIGIN_CACHE


def test_a_refused_repository_whose_config_moves_the_root_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``core.worktree`` makes git name another directory as the root, which
    the files do not settle: the capture stays indeterminate with no root
    rather than recording the directory holding ``.git``."""
    root = _repo(tmp_path / "shared")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _run(root, "config", "core.worktree", str(elsewhere))
    _refuse_every_probe(monkeypatch, root)
    got = origin.capture(root)
    assert got.worktree_root is None
    assert got.git_indeterminate is True
    assert "dubious ownership" in (got.git_refusal or "")


# ---------------------------------------------------------------------------
# memory_write with claims
# ---------------------------------------------------------------------------


def _writing_from(
    root: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Store]:
    """A server whose writes capture `root`, as another account's checkout:
    git refuses it (skipped where this git does not honour the switch), and
    location's token write stays out of it."""
    _assume_another_owner(monkeypatch, root)
    owner = getattr(os, "geteuid", lambda: 0)()
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    import bettermemory._handlers as handlers_module

    monkeypatch.setattr(
        handlers_module, "capture_origin", lambda cwd=None: origin.capture(root)
    )
    state = SessionState()
    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    return server, store


def _claiming(claim: str) -> dict[str, Any]:
    return {
        "content": f"the shared tree pins its timeout in {claim}",
        "scopes": ["tools"],
        "claims": [claim],
    }


async def test_a_claim_write_in_a_refused_repository_is_checked_against_its_root(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write with claims from another account's checkout is checked
    against the root the files hold: a claim that holds there is stored (it
    was refused "the caller is not inside a git worktree"), and one that
    does not hold is refused for that."""
    root = _repo(tmp_path / "shared")
    server, store = _writing_from(root, memory_dir, monkeypatch)
    written = await call_tool(server, "memory_write", _claiming("src/a.py"))
    assert "not inside a git worktree" not in str(written)
    assert "id" in written, written
    stored = store.load_one(written["id"])
    assert stored.claims == ["src/a.py"]
    assert stored.origin is not None
    assert stored.origin.worktree_root == str(root)
    with pytest.raises(Exception, match="do not hold against the worktree"):
        await call_tool(server, "memory_write", _claiming("src/missing.py"))


async def test_a_claim_write_where_the_refused_root_is_unknown_names_the_refusal(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: where the refused repository's configuration moves its working
    tree, no root is known and the claims cannot be checked. The refusal
    says git refused the repository the caller stands in, quoting git, and
    what trusting it through ``safe.directory`` means, never "not inside a
    git worktree"."""
    root = _repo(tmp_path / "shared")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _run(root, "config", "core.worktree", str(elsewhere))
    server, _ = _writing_from(root, memory_dir, monkeypatch)
    with pytest.raises(Exception) as refused:
        await call_tool(server, "memory_write", _claiming("src/a.py"))
    message = str(refused.value)
    assert "git refused to read it: fatal: detected dubious ownership" in message
    assert "safe.directory if you trust its configuration" in message
    assert "not inside a git worktree" not in message
