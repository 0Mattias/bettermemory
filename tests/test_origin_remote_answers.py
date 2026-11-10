"""Remote identity distinguishes absence, unread probes and native URL bytes."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import origin
from bettermemory._response import ResponseBuilder

from .test_origin_patch import _plain, neutral_git

__all__ = ["neutral_git"]


def _repo(path: Path) -> None:
    path.mkdir()
    _plain(path, "init", "-q")
    _plain(path, "commit", "-qm", "one", "--allow-empty")


def test_an_unread_origin_url_is_not_replaced_with_another_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    _plain(repo, "remote", "add", "origin", "https://example.invalid/original.git")
    _plain(repo, "remote", "add", "fork", "https://example.invalid/fork.git")
    real_run = subprocess.run
    refused = False

    def run(args: Any, *positional: Any, **kwargs: Any) -> Any:
        nonlocal refused
        if list(args[-3:]) == ["remote", "get-url", "origin"] and not refused:
            refused = True
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))
        return real_run(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    captured = origin.capture(repo)
    assert captured.repo is None
    assert captured.git_indeterminate
    assert origin.capture(repo).repo == "https://example.invalid/original.git"


def test_a_missing_origin_still_uses_the_first_existing_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    url = "https://example.invalid/fork.git"
    _plain(repo, "remote", "add", "fork", url)
    captured = origin.capture(repo)
    assert captured.repo == url and not captured.git_indeterminate


@pytest.mark.parametrize("failed", ["origin", "listing", "fallback"])
def test_an_unread_remote_does_not_become_an_absent_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: str
) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    if failed == "origin":
        _plain(repo, "remote", "add", "origin", "https://example.invalid/original.git")
    _plain(repo, "remote", "add", "fork", "https://example.invalid/fork.git")
    real = origin._identity_result
    command = {
        "origin": ("remote", "get-url", "origin"),
        "listing": ("remote",),
        "fallback": ("remote", "get-url", "fork"),
    }[failed]

    def probe(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if args == command:
            return subprocess.CompletedProcess(args, 128, "", "fatal: unread config\n")
        return real(cwd, *args)

    monkeypatch.setattr(origin, "_identity_result", probe)
    captured = origin.capture(repo)
    assert captured.repo is None and captured.git_indeterminate


def test_raw_remote_aliases_preserve_whitespace_and_embedded_line_breaks(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    primary = "https://example.invalid/original.git"
    alternate = "file:///nonexistent/alias\nsecond-line.git "
    _plain(repo, "remote", "add", "origin", primary)
    _plain(repo, "config", "--add", "remote.origin.url", alternate)
    captured = origin.capture(repo)
    assert captured.repo == primary
    assert captured._repo_url_alternates == (alternate,)


@pytest.mark.skipif(
    os.name == "nt", reason="raw URL byte argv uses POSIX surrogateescape"
)
@pytest.mark.parametrize(
    "raw",
    [b"file:///nonexistent/native-\xff.git", b"file:///nonexistent/native-\xff.git "],
)
def test_native_local_remote_url_bytes_keep_their_identity_and_a_utf8_response(
    tmp_path: Path,
    raw: bytes,
) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    url = os.fsdecode(raw)
    _plain(repo, "remote", "add", "origin", url)
    captured = origin.capture(repo)
    assert captured.repo is not None and os.fsencode(captured.repo) == raw
    result = ResponseBuilder(stale_after_days=30).origin_to_dict(captured)
    assert result is not None and result["repo"] is None
    assert base64.b64decode(result["repo_bytes_base64"]) == raw
    json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_a_branch_name_keeps_its_valid_non_ascii_whitespace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    name = "topic-\u00a0"
    _plain(repo, "checkout", "-qb", name)
    assert origin.capture(repo).branch == name


@pytest.mark.skipif(os.name == "nt", reason="raw branch bytes use POSIX argv")
def test_a_native_branch_name_keeps_its_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    raw = b"topic-\xff"
    # An unborn ref needs no filename containing the raw byte, which APFS
    # refuses even though HEAD and Git's ref protocol can carry it.
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/" + os.fsdecode(raw)],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    captured = origin.capture(repo)
    assert captured.branch is not None and os.fsencode(captured.branch) == raw
    result = ResponseBuilder(stale_after_days=30).origin_to_dict(captured)
    assert result is not None and result["branch"] is None
    assert base64.b64decode(result["branch_bytes_base64"]) == raw
    json.dumps(result, ensure_ascii=False).encode("utf-8")
