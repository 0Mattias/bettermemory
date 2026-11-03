"""No git the package runs fetches (ENG-218).

A repository's own config can make it a partial clone whose promisor
remote is an ``ext::`` transport, and allow that transport
(``protocol.ext.allow=always``); git then runs the remote's command to
fetch an object a read needs and the clone lacks. The history reader asks
git (``cat-file``) for a commit whenever its own object reader is
incomplete, which a garbage pack index is enough for, so a planted
repository ran a command of its choosing in the daemon or a hook client.
`_git.run_git` now sets ``GIT_NO_LAZY_FETCH=1`` and an empty
``GIT_ALLOW_PROTOCOL`` on every call; each alone stops the fetch, the
first on git 2.44 and later, the second on any git since 2.12.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bettermemory import _git
from bettermemory.location_git import RepositoryHistory

_POSIX = os.name == "posix"


def _plain_git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=True
    )
    return done.stdout.decode().strip()


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    digits = out.split()[2].split(".")[:2]
    return tuple(int(d) for d in digits if d.isdigit())


class Hostile:
    """A repository of six commits on one line whose third commit's object
    is gone, configured as a partial clone of an ``ext::`` remote whose
    command touches `marker`."""

    def __init__(self, base: Path) -> None:
        self.repo = base / "repo"
        self.repo.mkdir()
        self.marker = base / "MARKER"
        script = base / "fetch.sh"
        script.write_text(f"#!/bin/sh\ntouch '{self.marker}'\nexit 1\n")
        script.chmod(0o755)
        _plain_git(self.repo, "init", "-q")
        tree = _plain_git(
            self.repo, "hash-object", "-w", "-t", "tree", "--stdin", stdin=b""
        )
        self.ids: list[str] = []
        for i in range(1, 7):
            parent = f"parent {self.ids[-1]}\n" if self.ids else ""
            body = (
                f"tree {tree}\n{parent}author A <a> {1_600_000_000 + i} +0000\n"
                f"committer C <c> {1_600_000_000 + i} +0000\n\nc{i}\n"
            ).encode()
            self.ids.append(
                _plain_git(
                    self.repo,
                    "hash-object",
                    "-w",
                    "-t",
                    "commit",
                    "--stdin",
                    stdin=body,
                )
            )
        for key, value in (
            ("core.repositoryformatversion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.url", f"ext::{script}"),
            ("remote.origin.promisor", "true"),
            ("protocol.ext.allow", "always"),
        ):
            _plain_git(self.repo, "config", key, value)
        self.missing = self.ids[2]
        objects = self.repo / ".git" / "objects"
        (objects / self.missing[:2] / self.missing[2:]).unlink()
        self.common = str(self.repo / ".git")

    def leave_the_reader_incomplete(self) -> None:
        pack = self.repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True, exist_ok=True)
        name = "pack-" + "ab" * 20
        (pack / f"{name}.pack").write_bytes(b"PACK garbage")
        (pack / f"{name}.idx").write_bytes(b"garbage index")

    def ask(self) -> None:
        _git.run_git(
            ["cat-file", "commit", self.missing], git_dir=self.common, timeout=10.0
        )


@pytest.fixture
def hostile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Hostile:
    if not _POSIX:
        pytest.skip("the ext:: command is a POSIX shell script")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in ("GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)
    return Hostile(tmp_path)


def test_every_call_fetches_nothing_whatever_the_callers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file:https")
    for name in _git.SCRUBBED_ENV:
        monkeypatch.setenv(name, os.devnull)
    env = _git._environment()
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    for name in (
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_SHALLOW_FILE",
    ):
        assert name in _git.SCRUBBED_ENV
        assert name not in env


def test_the_hostile_clone_runs_its_command_for_a_git_that_may_fetch(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with neither pin, git runs the remote's command, so
    the tests below fail for the right reason when a pin goes."""
    monkeypatch.setattr(_git, "NO_FETCH", {})
    hostile.ask()
    assert hostile.marker.exists()


def test_a_partial_clones_promisor_command_never_runs(hostile: Hostile) -> None:
    hostile.ask()
    assert not hostile.marker.exists()


def test_no_transport_is_allowed_where_git_ignores_the_lazy_fetch_pin(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git before 2.44 ignores ``GIT_NO_LAZY_FETCH``; the empty
    ``GIT_ALLOW_PROTOCOL`` alone still stops the command."""
    monkeypatch.setattr(_git, "NO_FETCH", {"GIT_ALLOW_PROTOCOL": ""})
    hostile.ask()
    assert not hostile.marker.exists()


def test_the_lazy_fetch_pin_alone_stops_the_fetch(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _git_version() < (2, 44):
        pytest.skip("GIT_NO_LAZY_FETCH is read from git 2.44")
    monkeypatch.setattr(_git, "NO_FETCH", {"GIT_NO_LAZY_FETCH": "1"})
    hostile.ask()
    assert not hostile.marker.exists()


def test_the_history_reader_asks_git_without_running_the_remote(
    hostile: Hostile,
) -> None:
    hostile.leave_the_reader_incomplete()
    history = RepositoryHistory(hostile.common)
    assert not history.reader.complete
    assert history.parents(hostile.ids[3]) == (hostile.missing,)
    assert tuple(history.parents(hostile.missing)) == ()
    assert not hostile.marker.exists()
