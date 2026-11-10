"""Claim 14: a legacy question the migration itself opens (candidate carries
one of the records' remotes) is closed by settle_legacy_questions at the
next open when the records also name a remote nobody carries."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bettermemory import membership_migrate as mm
from bettermemory.machine import open_machine
from bettermemory.membership import LEGACY, Membership
from bettermemory.membership_migrate import migrate
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_membership_migration import _NOW, _now, git, make_repo, neutral_git  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_BETA_URL = "https://example.invalid/acme/beta.git"
_OTHER_URL = "https://example.invalid/acme/other.git"


def _memory(store: Store, label: str, root: Path, repo: str | None, cited=()) -> None:
    store.put_memory(
        Memory(
            id=generate_ulid(),
            created=_NOW,
            updated=_NOW,
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=f"memory {label}\n",
            origin=Origin(cwd=str(root), repo=repo, worktree_root=str(root)),
            verified_commits=list(cited),
        )
    )


def _run(tmp_path: Path, *, second_remote: bool):
    tmp = tmp_path.resolve()
    beta, beta_copy = tmp / "Developer" / "beta", tmp / "Developer" / "beta-copy"
    (c1,) = make_repo(beta, commits=1)
    shutil.copytree(beta, beta_copy, symlinks=True)  # remote-less, holds c1 only
    (beta / "more.txt").write_text("second\n")
    git(beta, "add", "-A")
    git(beta, "commit", "-q", "-m", "second")
    c2 = git(beta, "rev-parse", "HEAD")
    git(beta, "remote", "add", "origin", _BETA_URL)
    gone = tmp / "old" / "beta"
    store = Store(tmp / "store")
    identity = open_machine(tmp / "state")
    _memory(store, "live-beta", beta, _BETA_URL)
    _memory(store, "live-copy", beta_copy, None)
    _memory(store, "gone-1", gone, _BETA_URL, cited=(c1, c2))
    if second_remote:
        _memory(store, "gone-2", gone, _OTHER_URL)
    report = migrate(store, identity, now=_now)
    model = Membership.open(store, identity)
    roots = {r.path: r for r in report.roots}
    (q,) = [q for q in report.proposals if q.root == str(gone)]
    pr = model.proposal(q.proposal)
    # The migration's own decision: rule none, same name, compatible holder.
    assert pr.kind == LEGACY and pr.state == "open"
    assert pr.evidence["rule"] == "none"
    assert pr.candidates == (roots[str(beta)].project,) or list(pr.candidates) == [
        roots[str(beta)].project
    ]
    legacy = pr.evidence["unresolved_project"]
    assert model.project(legacy).name == model.project(pr.candidates[0]).name == "beta"
    settled = mm.settle_at_open(store, identity)
    after = model.proposal(q.proposal)
    store.close()
    return q.proposal, settled, after


def test_control_question_stays_open_when_records_name_only_the_carried_remote(
    tmp_path: Path,
) -> None:
    _qid, settled, after = _run(tmp_path, second_remote=False)
    assert settled == [] and after.state == "open"


def test_question_the_migration_opened_is_not_closed_at_the_next_open(
    tmp_path: Path,
) -> None:
    """The candidate (beta) carries acme/beta, which the records name: the
    migration's compatible() says so and opens the question; the settle
    pass promises to close only questions held 'through a remote the
    candidate does not carry'."""
    qid, settled, after = _run(tmp_path, second_remote=True)
    assert settled == [], (
        f"settle closed {settled} ({after.answer} by {after.answered_by})"
    )
    assert after.state == "open"
