"""Claim 8: a quoted ---/+++ header whose path also holds a space carries
git's trailing TAB, and `_dequote_patch_headers` leaves it quoted."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from datetime import timedelta

import pytest

from bettermemory.claims import build_binding_index, parse_claim
from bettermemory.origin import (
    _dequote_patch_headers,
    commit_patch_stream,
    commits_since_anchor,
)
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    resolve_commit_drift,
)

from .test_multiline_literal_drift import (
    _T0,
    _VERIFIED,
    _commit as dated_commit,
    _git as dated_git,
    neutral_git,
)

__all__ = ["neutral_git"]

NAME = 'we"ird file.py'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def _repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root: Path, name: str, content: str | None) -> None:
    if content is None:
        _git(root, "rm", "-q", "--", name)
    else:
        (root / name).write_text(content, encoding="utf-8")
        _git(root, "add", "--", name)
    _git(root, "commit", "-q", "-m", "c")


def _shas(root: Path) -> list[str]:
    return _git(root, "log", "--format=%H").split()


@pytest.mark.skipif(sys.platform == "win32", reason="'\"' illegal on Windows")
def test_edit_of_quoted_path_with_space_is_indexed_under_real_path(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    _commit(tmp_path, NAME, "LIMIT = 1\n")
    _commit(tmp_path, NAME, "LIMIT = 2\n")
    stream = commit_patch_stream(tmp_path, _shas(tmp_path), [NAME])
    assert stream is not None
    index = build_binding_index(stream)
    assert NAME in index["files"], sorted(index["files"])
    assert all(not p.startswith('"') for p in index["files"]), sorted(index["files"])
    assert {key[0] for key in index["bindings"]} == {NAME}


@pytest.mark.skipif(sys.platform == "win32", reason="'\"' illegal on Windows")
def test_deletion_of_quoted_path_with_space_is_recorded(tmp_path: Path) -> None:
    _repo(tmp_path)
    _commit(tmp_path, NAME, "LIMIT = 1\n")
    _commit(tmp_path, NAME, None)
    stream = commit_patch_stream(tmp_path, _shas(tmp_path), [NAME])
    assert stream is not None
    assert f"--- a/{NAME}" in stream
    index = build_binding_index(stream)
    assert NAME in index["deleted"], (sorted(index["deleted"]), sorted(index["files"]))
    assert all(not p.startswith('"') for p in index["files"]), sorted(index["files"])


@pytest.mark.skipif(sys.platform == "win32", reason="'\"' illegal on Windows")
def test_control_quoted_path_without_space_still_dequoted(tmp_path: Path) -> None:
    """Control: the same shape without a space is dequoted today."""
    name = 'we"ird.py'
    _repo(tmp_path)
    _commit(tmp_path, name, "LIMIT = 1\n")
    _commit(tmp_path, name, "LIMIT = 2\n")
    stream = commit_patch_stream(tmp_path, _shas(tmp_path), [name])
    assert stream is not None
    index = build_binding_index(stream)
    assert index["files"] == {name}


@pytest.mark.skipif(
    sys.platform == "win32", reason="double quote is not a Windows filename"
)
@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("kind", ["symbol", "literal", "path"])
def test_a_claim_on_the_quoted_file_keeps_its_drift_signal(
    tmp_path: Path, basis: str, kind: str
) -> None:
    _repo(tmp_path)
    dated_commit(tmp_path, NAME, "LIMIT = 1\ndef foo():\n    return 1\n", _T0)
    head = dated_git(tmp_path, "rev-parse", "HEAD")
    claim = parse_claim(
        NAME
        + ("::foo" if kind == "symbol" else "::LIMIT=1" if kind == "literal" else "")
    )
    if kind == "path":
        dated_git(tmp_path, "rm", "--", NAME)
        dated_git(
            tmp_path,
            "commit",
            "-q",
            "-m",
            "remove",
            when=_VERIFIED + timedelta(hours=1),
        )
    else:
        dated_commit(
            tmp_path,
            NAME,
            "LIMIT = 2\ndef bar():\n    return 1\n",
            _VERIFIED + timedelta(hours=1),
        )
    walk = commits_since_anchor(tmp_path, head, toplevel=tmp_path)
    assert walk is not None
    found = resolve_commit_drift(
        cwd=tmp_path,
        since=_VERIFIED,
        unfiltered=1,
        anchors=(),
        claims=[claim],
        toplevel=tmp_path,
        walk=walk if basis == BASIS_REACHABILITY else None,
    )
    assert found is not None and found.count == 1
    assert found.basis == basis
    assert found.claims_drifted == (claim.render(),)


@pytest.mark.parametrize("tail", ["\t\t", "\ttrailing", " trailing"])
def test_only_one_formatting_tab_is_an_accepted_label_suffix(tail: str) -> None:
    line = r'+++ "b/we\"ird file.py"' + tail
    assert _dequote_patch_headers(line) == line


def test_an_escaped_tab_inside_the_name_is_preserved() -> None:
    line = r'+++ "b/a\t file.py"' + "\t"
    assert _dequote_patch_headers(line) == "+++ b/a\t file.py"


def test_a_hunk_line_looking_like_a_tab_terminated_header_stays_a_hunk() -> None:
    line = r'+++ "b/we\"ird file.py"' + "\t"
    stream = "@@ -0,0 +1 @@\n" + line + "\n"
    assert _dequote_patch_headers(stream) == stream
