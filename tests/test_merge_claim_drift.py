"""Merge edits keep path/claim coverage beside ordinary parsed diffs."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from bettermemory.claims import (
    COMMIT_MARK,
    build_binding_index,
    check_claim,
    parse_claim,
)
from bettermemory.origin import commit_patch_stream, commits_since_anchor
from bettermemory.origin_dates import _touching_records
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ClaimAnchor,
    CommitDriftStatus,
    compute_commit_drift,
    resolve_commit_drift,
)

from .test_multiline_literal_drift import (
    _T0,
    _VERIFIED,
    _commit,
    _git,
    neutral_git,
)

__all__ = ["neutral_git"]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_SOURCE = "X = 30\ndef foo():\n    return 1\ndef helper():\n    return 1\n"


def _history(tmp_path: Path, change: str) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "mod.py", _SOURCE, _T0)
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "branch.py", "branch = 1\n", _T0 + timedelta(minutes=15))
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main.py", "main = 1\n", _T0 + timedelta(minutes=30))
    anchor = _git(repo, "rev-parse", "HEAD")
    churned = _SOURCE.replace(
        "def helper():\n    return 1", "def helper():\n    return 2"
    )
    _commit(repo, "mod.py", churned, _VERIFIED + timedelta(minutes=15))
    churn = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--no-ff", "--no-commit", "feature")
    module = repo / "mod.py"
    if change == "file rename":
        module.rename(repo / "new.py")
    elif change == "binding rename":
        module.write_text(churned.replace("def foo()", "def bar()"))
    elif change == "literal change":
        module.write_text(churned.replace("X = 30", "X = 300"))
    elif change == "path edit":
        module.write_text(churned + "# manual merge edit\n")
    else:
        assert change == "ordinary merge"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge", when=_VERIFIED + timedelta(hours=1))
    return repo, anchor, churn, _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("change", ["file rename", "binding rename", "literal change"])
def test_a_claims_merge_edit_cannot_hide_beside_an_unrelated_parsed_hunk(
    tmp_path: Path, basis: str, change: str
) -> None:
    repo, anchor, churn, merge = _history(tmp_path, change)
    text = "mod.py::X=30" if change == "literal change" else "mod.py::foo"
    claim = parse_claim(text)
    assert check_claim(claim, repo) is not None
    walk = commits_since_anchor(repo, anchor, toplevel=repo)
    assert walk is not None and len(walk.commits) == len(set(walk.commits)) == 3
    assert walk.shas_touching(["mod.py"]) == {churn, merge}
    if change == "file rename":
        assert walk.shas_touching(["new.py"]) == {merge}
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2,
        anchors=(),
        claims=[claim],
        toplevel=repo,
        walk=walk if basis == BASIS_REACHABILITY else None,
    )
    assert resolved is not None and resolved.count == 1
    assert resolved.basis == basis
    # The ordinary hunk touched helper's body; no binding claim was read
    # out of the omitted merge patch. The floor reports a file touch.
    assert resolved.claims_drifted == ()


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("change", ["path edit", "file rename", "ordinary merge"])
def test_cited_paths_count_merge_edits_once_and_plain_integrations_not_at_all(
    tmp_path: Path, basis: str, change: str
) -> None:
    repo, anchor, churn, merge = _history(tmp_path, change)
    paths = ["mod.py", "new.py"] if change == "file rename" else ["mod.py"]
    found = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        body="",
        verified_paths=paths,
        verified_head=anchor if basis == BASIS_REACHABILITY else None,
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert isinstance(found, CommitDriftStatus) and found.commits_since_verify == (
        1 if change == "ordinary merge" else 2
    )
    assert found.basis == basis
    walk = commits_since_anchor(repo, anchor, toplevel=repo)
    assert walk is not None
    assert walk.shas_touching(["mod.py"]) == (
        {churn} if change == "ordinary merge" else {churn, merge}
    )


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_an_ordinary_merge_keeps_precise_symbol_narrowing(
    tmp_path: Path, basis: str
) -> None:
    repo, anchor, _, _ = _history(tmp_path, "ordinary merge")
    walk = commits_since_anchor(repo, anchor, toplevel=repo)
    found = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2,
        anchors=(),
        claims=[parse_claim("mod.py::foo")],
        toplevel=repo,
        walk=walk if basis == BASIS_REACHABILITY else None,
    )
    assert found is not None and found.count == 0 and not found.claims_drifted


def test_an_unindexed_merge_record_stays_visible_beside_a_parsed_commit(
    tmp_path: Path,
) -> None:
    repo, _, churn, merge = _history(tmp_path, "binding rename")
    stream = commit_patch_stream(repo, [merge, churn], ["mod.py"], toplevel=repo)
    assert stream is not None and COMMIT_MARK + merge in stream
    index = build_binding_index(stream)
    assert index["files"] == {"mod.py"}
    assert index["unindexed_commits"] == {merge}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\x012025-01-01T00:00:00Z\n\nmod.py\n", "2025-01-01T00:00:00Z"),
        ("\x01ordinary merge\n\n\x01real change\n\nmod.py\n", "real change"),
        ("\x01\nmod.py\n", None),
        ("unframed output\n", None),
        ("\x01value\n   \n", "value"),
    ],
)
def test_touch_records_require_a_value_and_a_real_diff(raw, expected) -> None:
    assert _touching_records(raw) == expected
