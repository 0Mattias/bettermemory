"""Claim 6: resolve_repo_pathspecs promises that unresolvable paths are
dropped silently; a symlink loop / unknown ~user raises RuntimeError
(Python 3.11/3.12 pathlib), which the (OSError, ValueError) clause misses."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.origin import Origin, resolve_repo_pathspecs

from .test_server_commit_drift import (
    _REMOTE,
    _call,
    _commit_touching,
    _init_repo,
    server_with_fake_origin,
)

from .test_multiline_literal_drift import neutral_git

__all__ = ["neutral_git", "server_with_fake_origin"]


def _loop(repo: Path) -> None:
    try:
        os.symlink("loop2", repo / "loop1")
        os.symlink("loop1", repo / "loop2")
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows account cannot create symlinks")
        raise


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_touching(repo, "first", when=datetime(2020, 1, 1, tzinfo=timezone.utc))
    return repo


def test_symlink_loop_path_is_dropped_and_rest_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _loop(repo)
    assert resolve_repo_pathspecs(repo, ["loop1/x.py", "notes.md"]) == ["notes.md"]


def test_absolute_symlink_loop_path_is_dropped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _loop(repo)
    assert resolve_repo_pathspecs(repo, [str(repo / "loop1" / "x.py"), "notes.md"]) == [
        "notes.md"
    ]


@pytest.mark.parametrize("raw", ["~nosuchuser_hunt06/x.py", "~$report.docx"])
def test_unknown_tilde_user_is_dropped_and_rest_kept(tmp_path: Path, raw: str) -> None:
    repo = _repo(tmp_path)
    assert resolve_repo_pathspecs(repo, [raw, "notes.md"]) == ["notes.md"]


async def test_memory_show_survives_symlink_loop_citation(
    server_with_fake_origin, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _loop(repo)
    server = server_with_fake_origin(Origin(cwd=str(repo), repo=_REMOTE, branch="main"))
    written = await _call(
        server,
        "memory_write",
        content="durable memory body about notes.md and loop1/x.py",
        scopes=["tools"],
    )
    await _call(server, "memory_verify", id=written["id"], note="initial check")
    _commit_touching(repo, "later", when=datetime(2099, 1, 1, tzinfo=timezone.utc))
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["commit_drift"] is not None
    assert shown["commit_drift"]["status"] == "drift"
    assert shown["commit_drift"]["commits_since_verify"] == 1


def test_missing_claim_and_resolved_symlink_remain_anchors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "target.py").write_text("x = 1\n", encoding="utf-8")
    try:
        (repo / "alias.py").symlink_to("target.py")
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows account cannot create symlinks")
        raise
    assert resolve_repo_pathspecs(
        repo, ["alias.py", "missing/never.py", "../outside.py"], toplevel=repo
    ) == ["target.py", "missing/never.py"]


def test_runtime_error_drops_only_the_unresolvable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path.name == "bad.py":
            raise RuntimeError("cannot resolve")
        return real(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert resolve_repo_pathspecs(
        tmp_path, ["bad.py", "missing.py"], toplevel=tmp_path
    ) == ["missing.py"]
