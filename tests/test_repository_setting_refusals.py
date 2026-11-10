"""Claim 18: repository-settings variables `_rev_parse_dies` names in no
list. Where ``git rev-parse --show-toplevel HEAD`` dies on the repository's
config, `origin.toplevel_and_head_from_files` must answer None.

Git 2.34 does not initialize these settings during rev-parse; the oracle
is each tested Git, with minimum and newest releases in CI (ENG-227).
The expanded settings include the integer and boolean reads in
https://github.com/git/git/blob/v2.56.0/repo-settings.c.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bettermemory import _caches, origin

from .test_location_git_include_conditions import _git, _repo
from .test_location_git_include_conditions import leg as leg
from .test_membership_security import neutral_git
from .test_origin_config_dies import _text

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

CASES = [
    ("fetch.negotiationalgorithm", "banana"),
    ("pack.usesparse", "banana"),
    ("pack.readreverseindex", "banana"),
    ("pack.usebitmapboundarytraversal", "banana"),
    ("commitgraph.readchangedpaths", "banana"),
    ("commitgraph.generationversion", "banana"),
    ("gc.writecommitgraph", "banana"),
    ("fetch.writecommitgraph", "banana"),
    ("fetch.negotiationalgorithm", None),
    ("fetch.negotiationalgorithm", "true"),
    ("commitgraph.generationversion", None),
    ("commitgraph.generationversion", "9999999999"),
    ("pack.usepathwalk", "banana"),
    ("commitgraph.changedpathsversion", "banana"),
    ("commitgraph.changedpathsversion", None),
    ("commitgraph.changedpathsversion", "9999999999"),
]
#: Values git reads on past: the files should keep answering (the fix
#: must not decline what a checkout ordinarily carries).
FINE = [
    ("fetch.negotiationalgorithm", "skipping"),
    ("pack.usesparse", "true"),
    ("fetch.writecommitgraph", "false"),
    ("gc.writecommitgraph", "true"),
    ("fetch.negotiationalgorithm", "NoOp"),
    ("commitgraph.generationversion", "2"),
    ("commitgraph.generationversion", "1k"),
    ("pack.usesparse", None),
    ("pack.readreverseindex", "no"),
    ("pack.usebitmapboundarytraversal", "1"),
    ("commitgraph.readchangedpaths", "off"),
    ("pack.usepathwalk", None),
    ("pack.usepathwalk", "true"),
    ("commitgraph.changedpathsversion", "1"),
    ("commitgraph.changedpathsversion", "-1"),
]


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)


def _answers(repo: Path) -> tuple[bool, bool]:
    done = _git(repo, "rev-parse", "--show-toplevel", "HEAD", check=False)
    _caches.clear_all()
    return done.returncode == 0, origin.toplevel_and_head_from_files(repo) is not None


def test_files_decline_where_git_dies_on_repo_settings(
    tmp_path: Path, leg: str
) -> None:
    repo = _repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    base = config.read_text(encoding="utf-8")
    git_ok, files_ok = _answers(repo)
    assert git_ok and files_ok == (leg == "posix")
    wrong = []
    for name, value in CASES:
        config.write_text(base + _text(name, value), encoding="utf-8")
        git_answers, files_answer = _answers(repo)
        if files_answer and not git_answers:
            wrong.append((name, value))
    assert wrong == []


def test_files_still_answer_where_git_reads_on(tmp_path: Path, leg: str) -> None:
    repo = _repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    base = config.read_text(encoding="utf-8")
    for name, value in FINE:
        config.write_text(base + _text(name, value), encoding="utf-8")
        git_answers, files_answer = _answers(repo)
        assert git_answers, (name, value)
        assert files_answer == (leg == "posix"), (name, value)
