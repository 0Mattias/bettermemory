"""`bettermemory doctor`'s git check (ENG-227): the git on PATH, asked its
version once, and graded against `gitversion.MINIMUM`, the oldest git the
git-reading suites were run against and every read stood.

The fakes are shell scripts first on PATH that print what a git of each
build prints for ``git version``, so the check runs as it runs on a host:
through `_git.run_git`, the one way the package runs git. Windows runs
no shell script as ``git``; the fakes are POSIX's, and the real git on
PATH is checked everywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git as git_runner
from bettermemory import doctor, gitversion

posix_fakes = pytest.mark.skipif(
    os.name == "nt", reason="a shell script is no git on Windows"
)

_MINIMUM = gitversion.text(gitversion.MINIMUM)


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """A ``git`` first and alone on PATH that runs `body` (sh)."""
    folder = tmp_path / "fake-bin"
    folder.mkdir()
    fake = folder / "git"
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return fake


@pytest.mark.parametrize(
    ("output", "version"),
    [
        ("git version 2.34.1", (2, 34, 1)),
        ("git version 2.39.5 (Apple Git-154)", (2, 39, 5)),
        ("git version 2.45.2.windows.1", (2, 45, 2)),
        ("git version 2.56.GIT", (2, 56, 0)),
        ("git version 3.0.0-rc0", (3, 0, 0)),
        ("  git version 2.30.9\n", (2, 30, 9)),
        ("hub version 2.14.2", None),
        ("", None),
    ],
)
def test_the_version_is_read_as_each_build_prints_it(
    output: str, version: tuple[int, int, int] | None
) -> None:
    """A vendor's suffix is read past, a build between releases reads as
    its minor's first, and output that names no git version reads as
    none."""
    assert gitversion.parse(output) == version


@posix_fakes
def test_a_git_below_the_minimum_fails_naming_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_git(tmp_path, monkeypatch, 'echo "git version 2.30.9"')
    check = doctor.check_git()
    assert check.status == doctor.FAIL
    assert "git 2.30.9" in check.summary and f"older than {_MINIMUM}" in check.summary
    assert str(fake) in check.summary
    assert check.remedy == f"install git {_MINIMUM} or later and put it first on PATH"
    assert check.details == {
        "path": str(fake),
        "output": "git version 2.30.9",
        "version": "2.30.9",
        "minimum": _MINIMUM,
    }


@posix_fakes
@pytest.mark.parametrize("patch", [0, 9])
def test_the_minimum_itself_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: int
) -> None:
    _fake_git(tmp_path, monkeypatch, f'echo "git version {_MINIMUM}.{patch}"')
    check = doctor.check_git()
    assert check.status == doctor.OK, check
    assert check.summary.startswith(f"git {_MINIMUM}.{patch} (")
    assert check.remedy is None


@posix_fakes
def test_the_minor_below_the_minimum_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    major, minor = gitversion.MINIMUM
    _fake_git(tmp_path, monkeypatch, f'echo "git version {major}.{minor - 1}.99"')
    assert doctor.check_git().status == doctor.FAIL


@posix_fakes
@pytest.mark.parametrize(
    "body",
    ['echo "hub version 2.14.2"', 'echo "git version 2.56.0"; exit 1', "exit 129"],
    ids=["no_version", "non_zero", "silent"],
)
def test_a_git_whose_version_cannot_be_read_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    fake = _fake_git(tmp_path, monkeypatch, body)
    check = doctor.check_git()
    assert check.status == doctor.WARN
    assert str(fake) in check.summary and "could not be read" in check.summary
    assert check.details["version"] is None


def test_no_git_on_path_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    check = doctor.check_git()
    assert check.status == doctor.WARN
    assert check.summary.startswith("no `git` on PATH")
    assert check.details["path"] is None


def test_the_git_on_path_is_asked_its_version_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real git: one ``git version``, through the package's runner,
    whose answer is the version git itself prints, and supported (every
    leg of CI runs a git at or past the minimum)."""
    real = git_runner.run_git
    asked: list[tuple[str, ...]] = []

    def counting(args: Any, **kwargs: Any) -> Any:
        asked.append(tuple(args))
        return real(args, **kwargs)

    monkeypatch.setattr(git_runner, "run_git", counting)
    check = doctor.check_git()
    assert asked == [("version",)]
    printed = subprocess.run(["git", "version"], capture_output=True, text=True)
    assert check.details["output"] == printed.stdout.strip()
    parsed = gitversion.parse(printed.stdout)
    assert parsed is not None
    assert check.details["version"] == gitversion.text(parsed)
    assert check.status == doctor.OK, check


def test_the_git_check_follows_the_install_check() -> None:
    ids = list(doctor.CHECK_IDS)
    assert ids.index("git") == ids.index("install") + 1
