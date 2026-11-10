"""Claim 32: a merge commit that itself changes a cited file is invisible
to the reachability walk (`git log --name-only` prints no diff for a
merge), so the anchored memory reads clean/0 while the author-date basis
reads drift/1 for the same memory text and the same history."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from bettermemory.origin import Origin, commits_since_anchor
from bettermemory.store import Store

from .test_server_commit_drift import (  # noqa: F401
    _REMOTE,
    _call,
    _commit_touching,
    _drift_counts_for,
    _git_env,
    _head,
    _init_repo,
    server_with_fake_origin,
)

__all__ = ["server_with_fake_origin"]


def _history_before_the_stamp(repo: Path) -> str:
    """base (notes.md) -> main work (other.md) = anchor; branch `feat`
    off base touching side.md only."""
    _commit_touching(repo, "base", when=datetime(2025, 1, 1, tzinfo=timezone.utc))
    subprocess.run(["git", "checkout", "-q", "-b", "feat"], cwd=repo, check=True)
    _commit_touching(
        repo,
        "feature",
        when=datetime(2025, 1, 2, tzinfo=timezone.utc),
        filename="side.md",
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    _commit_touching(
        repo,
        "main work",
        when=datetime(2025, 2, 1, tzinfo=timezone.utc),
        filename="other.md",
    )
    return _head(repo)


def _merge_changing_the_cited_file(repo: Path) -> str:
    """`git merge --no-ff --no-commit feat`, edit notes.md, commit: the
    merge commit itself is the only commit after the stamp that changes
    notes.md."""
    env = _git_env(datetime.now(timezone.utc))
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "--no-commit", "feat"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    with (repo / "notes.md").open("a") as fh:
        fh.write("changed in the merge itself\n")
    subprocess.run(["git", "add", "notes.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "merge feat"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return _head(repo)


async def test_a_merge_that_changes_the_cited_file_counts_on_both_bases(
    server_with_fake_origin, memory_dir: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    anchor = _history_before_the_stamp(repo)

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    server = server_with_fake_origin(origin)
    store = Store(memory_dir)

    anchored = await _call(
        server,
        "memory_write",
        content="durable thing about widgets in notes.md",
        scopes=["tools"],
    )
    unanchored = await _call(
        server,
        "memory_write",
        content="durable widgets lore also in notes.md",
        scopes=["tools"],
    )
    store.mark_verified(anchored["id"], verified_head=anchor)
    store.mark_verified(unanchored["id"])

    time.sleep(2.1)  # the merge's author second is strictly after the stamp
    merge = _merge_changing_the_cited_file(repo)

    # Sanity: the merge is a two-parent commit, in the range, and it did
    # change notes.md against both parents.
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", merge],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(parents) == 3
    for parent in parents[1:]:
        changed = subprocess.run(
            ["git", "diff", "--name-only", parent, merge, "--", "notes.md"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert changed == ["notes.md"]
    walk = commits_since_anchor(repo, anchor)
    assert walk is not None and merge in walk.commits

    # Author-date basis (no verified_head): the merge counts.
    shown = await _call(server, "memory_show", id=unanchored["id"])
    assert shown["commit_drift"]["basis"] == "author-date"
    assert shown["commit_drift"]["status"] == "drift"
    assert shown["commit_drift"]["commits_since_verify"] == 1
    assert await _drift_counts_for(server, unanchored["id"]) == (1, 1, 1)

    # Reachability basis (verified_head set): same memory text, same
    # history, so the same commit must count.
    shown = await _call(server, "memory_show", id=anchored["id"])
    assert shown["commit_drift"]["basis"] == "reachability"
    assert (
        shown["commit_drift"]["status"],
        shown["commit_drift"]["commits_since_verify"],
        shown["staleness_verdict"],
    ) == ("drift", 1, "spot_check_recommended")
    assert await _drift_counts_for(server, anchored["id"]) == (1, 1, 1)
