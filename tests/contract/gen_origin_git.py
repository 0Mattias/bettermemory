"""Origin's Git processes, identity results and calling-thread counters."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import (
    _git,
    git_read_budget,
    gitfile_rule,
    origin,
    origin_gitfiles,
    origin_history,
)

from ._common import captured_by, jsonl

_FILE = "origin_git.jsonl"


@contextmanager
def _counts() -> Iterator[None]:
    """Give back exactly the counters and path memo the process supplied."""
    with (
        patch.object(_git, "_unanswered", 0),
        patch.object(_git._FAILED, "count", 0),
        patch.object(_git._TIMED_OUT, "count", 0),
        patch.object(_git._MISSING, "count", 0),
        patch.object(origin._UNANSWERED, "count", 0),
        patch.object(origin_gitfiles, "_NOT_A_DIRECTORY", OrderedDict()),
    ):
        yield


def _totals() -> list[int]:
    return [
        origin.unanswered_git_calls(),
        origin.failed_git_calls(),
        _git.timed_out_calls(),
        _git.missing_calls(),
        origin._UNANSWERED.count,
    ]


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[list[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append([record.name, record.levelname, record.getMessage()])


def _reply(text: str = "", code: int = 0, error: str = "") -> list[Any]:
    return [code, text, error]


def _cases() -> Iterator[dict[str, Any]]:
    options = [
        {},
        {"empty_ok": True},
        {"verbatim": True},
        {"empty_ok": True, "verbatim": True},
        {"paths": True},
    ]
    for mode in ("result", "identity", "git", "branch"):
        for reply in (
            _reply(" Ω\r\n"),
            _reply(" \n"),
            _reply(),
            _reply(code=1, error=" bad\u2028later\n"),
            "timeout",
            "missing",
            "os",
            "other",
            "refusal",
        ):
            for opts in options if mode == "git" else [{}]:
                yield {"mode": mode, "script": [reply], "options": opts}
    for replies in (
        [_reply(" main \r\n"), _reply(" main \r\0other\0other\0\0last")],
        [_reply("spaces \n"), _reply(code=3)],
        [_reply()],
        ["missing"],
        [_reply(code=2), "timeout"],
        [_reply(code=2), _reply(code=4)],
        [_reply(code=2), _reply("\norigin\n")],
        [_reply(code=2), _reply("upstream\norigin\n")],
        [_reply(code=2), _reply("else\n"), _reply(code=9)],
        [_reply(code=2), _reply("else\nmore\n"), _reply("url\n"), _reply("alt\0url\0")],
        [_reply("url\n"), "missing"],
    ):
        yield {"mode": "remote", "script": replies, "options": {}}


def _scripted(case: dict[str, Any]) -> dict[str, Any]:
    calls: list[list[str]] = []
    prefix = ["git", *_git.PINS, *origin_history.AS_STORED_CONFIG]
    script = iter(case["script"])
    mode = case["mode"]
    args = ("-c", "fixture=true", "status")
    options = case["options"]
    at = Path("sample")
    lines = _Lines()

    def run(command: list[str], **kw: Any) -> Any:
        assert command[: len(prefix)] == prefix
        assert kw["capture_output"] and not kw["check"]
        assert kw["cwd"] == str(at) and kw["timeout"] == 1.0
        assert kw["input"] == (b"data\0" if mode in {"result", "git"} else None)
        assert all(kw["env"][k] == v for k, v in origin_history.AS_STORED_ENV.items())
        calls.append(command[len(prefix) :])
        reply = next(script)
        if reply == "timeout":
            raise subprocess.TimeoutExpired(command, kw["timeout"])
        if reply == "missing":
            raise FileNotFoundError("no executable")
        if reply == "os":
            raise OSError("not available")
        if reply == "other":
            raise ValueError("other")
        code, stdout, stderr = reply
        return subprocess.CompletedProcess(
            command, code, stdout.encode(), stderr.encode()
        )

    reason = "separate checkout" if case["script"] == ["refusal"] else None
    token = git_read_budget._ACTIVE.set(None)
    try:
        with (
            _counts(),
            captured_by(_git.log, lines, logging.DEBUG),
            captured_by(origin.log, lines, logging.DEBUG),
            patch.object(gitfile_rule, "discovery_refusal", return_value=reason),
            patch("subprocess.run", run),
            patch("os.path.isdir", return_value=True),
        ):
            try:
                result: Any
                if mode == "git":
                    result = origin._git(at, *args, stdin=b"data\0", **options)
                elif mode == "result":
                    result = origin._git_result(at, *args, stdin=b"data\0")
                elif mode == "branch":
                    result = origin._git_branch(at)
                elif mode == "identity":
                    result = origin._identity_result(at, *args)
                else:
                    result = origin._git_remote_url_and_alternates(at)
                if isinstance(result, subprocess.CompletedProcess):
                    supplied = result.args
                    returned = (
                        supplied[len(prefix) :]
                        if supplied[: len(prefix)] == prefix
                        else supplied
                    )
                    result = [returned, result.returncode, result.stdout, result.stderr]
            except ValueError as error:
                result = {"raised": type(error).__name__}
            counts = _totals()
    finally:
        git_read_budget._ACTIVE.reset(token)
    return {
        **case,
        "calls": calls,
        "result": result,
        "counts": counts,
        "logs": lines.lines,
    }


def generate() -> dict[str, bytes]:
    return {_FILE: jsonl([_scripted(case) for case in _cases()])}


def generate_python() -> dict[str, bytes]:
    """3.14's Path.is_dir suppresses even EACCES, unlike the pinned 3.13."""
    import errno

    rows: list[Any] = []
    for name in (
        "ENOENT",
        "ENOTDIR",
        "ELOOP",
        "EBADF",
        "EACCES",
        "EPERM",
        "EIO",
        "ENAMETOOLONG",
    ):
        code = getattr(errno, name)
        at = Path("stat-check")
        with (
            _counts(),
            patch.object(Path, "stat", side_effect=OSError(code, "scripted")),
            patch.object(gitfile_rule, "discovery_refusal", return_value=None),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ) as asked,
            captured_by(origin.log, _Lines(), logging.DEBUG),
        ):
            root = origin._probe_worktree_root(at)
            rows.append([name, root, asked.call_count, _totals()])
    return {"origin_git_python.jsonl": jsonl(rows)}


def generate_posix() -> dict[str, bytes]:
    from .gen_origin_gitfiles_walk import _machine

    rows = []
    # The host executable and required process environment are inherited.
    # Git's own configuration, discovery and message locale are explicit.
    run = subprocess.run
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(root / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2001-02-03T04:05:06Z",
            "GIT_COMMITTER_DATE": "2001-02-03T04:05:06Z",
        }
        (root / "home").mkdir()
        for name in ("empty", "repo", "refused"):
            (root / name).mkdir()
        repo = root / "repo"

        def git(*args: str) -> None:
            run(
                ["git", *args],
                cwd=repo,
                env=environment,
                capture_output=True,
                check=True,
            )

        def observe(label: str, relative: str, foreign: bool = False) -> None:
            at = root / relative
            top_errors: list[bytes] = []

            def call(command: list[str], **kw: Any) -> Any:
                env = dict(environment)
                env.update(origin_history.AS_STORED_ENV)
                if foreign:
                    env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
                answer = run(command, **{**kw, "env": env})
                if command[-2:] == ["rev-parse", "--show-toplevel"]:
                    top_errors.append(answer.stderr)
                return answer

            with (
                _counts(),
                _machine(root),
                captured_by(_git.log, _Lines(), logging.DEBUG),
                captured_by(origin.log, _Lines(), logging.DEBUG),
                patch("subprocess.run", call),
            ):
                top, uncertain, reason = origin._probe_worktree_root(at)
                if reason is not None:
                    [stderr] = top_errors
                    assert (
                        reason
                        == stderr.decode("utf-8", "replace").strip().splitlines()[0]
                    )
                    reason = "<first stderr line>"
                answers = [
                    (top, uncertain, reason),
                    origin._git_branch(at),
                    origin._git_remote_url_and_alternates(at),
                ]
                rows.append([label, relative, foreign, answers, _totals()])

        observe("outside", "empty")
        git("init", "-q", "-b", "trunk")
        observe("unborn", "repo")
        git("remote", "add", "backup", "https://example.invalid/first")
        git("config", "--add", "remote.backup.url", "https://example.invalid/second ")
        observe("alternate", "repo")
        git("remote", "add", "origin", "alias:repo")
        git("config", "url.https://example.invalid/.insteadOf", "alias:")
        git(
            "-c",
            "core.hooksPath=" + os.devnull,
            "commit",
            "--allow-empty",
            "-qm",
            "root",
        )
        (repo / "child").mkdir()
        observe("rewrite", "repo/child")
        git("worktree", "add", "-qb", "linked", str(root / "linked"))
        observe("linked", "linked")
        git("checkout", "--detach", "-q")
        observe("detached", "repo")
        (root / "refused" / ".git").symlink_to(repo / ".git", target_is_directory=True)
        observe("refused-entry", "refused")
        observe("ownership", "repo", True)
        observe("missing", "missing")
        (root / "regular").touch()
        observe("file", "regular")
        data = jsonl([{"scenarios": len(rows)}, *rows]).replace(
            str(root).encode(), b"<root>"
        )
        return {"origin_git_posix.jsonl": data}
