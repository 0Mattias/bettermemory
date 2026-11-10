"""Claim 45: a part's question (L7) opened while the checkout was on a
provisional project stays open after the first-sight question is answered
`join`, although the file is then inside the project's chain."""

from __future__ import annotations

from pathlib import Path

from bettermemory import membership
from bettermemory.membership import ASK, SILENT

from .test_membership_resolution import (
    Lab,
    clone,
    git,
    lab,
    lab_of,
    make_repo,
    neutral_git,
)

__all__ = ["lab", "lab_of", "neutral_git"]


def test_part_question_closes_when_first_sight_joins(lab: Lab, tmp_path: Path) -> None:
    url = "https://example.invalid/acme/mono.git"
    m = make_repo(tmp_path / "work" / "mono", commits=2, remote=url)
    api = m / "packages" / "api"
    api.mkdir(parents=True)
    lab.write("root_note", m)
    lab.init_file(api, "api")
    git(m, "add", "-A")
    git(m, "commit", "-q", "-m", "part file")
    r_root, _ = lab.view(m)
    r_api, _ = lab.view(api)
    mono_pid, api_pid = r_root.project, r_api.project
    assert r_api.outcome == SILENT and api_pid != mono_pid
    assert lab.model().chain_of(api_pid) == [api_pid, mono_pid]

    c = clone(
        m, tmp_path / "elsewhere" / "clone", url="https://example.invalid/fork/x.git"
    )
    capi = c / "packages" / "api"
    first, _ = lab.view(capi)
    assert first.outcome == ASK and first.rule.startswith("ask:")
    kinds = sorted(
        p.kind for p in lab.model().pending() if p.checkout == first.checkout
    )
    assert kinds == [membership.FIRST_SIGHT, membership.SUBPROJECT_CONTRADICTION]

    # The user answers the first-sight question: join mono (as
    # `bettermemory project join` at the root does: it skips L7 questions).
    model = lab._declaring()
    fs = [
        p
        for p in model.pending()
        if p.checkout == first.checkout and p.kind == membership.FIRST_SIGHT
    ][0]
    assert fs.candidates == (mono_pid,) or list(fs.candidates) == [mono_pid]
    model.declare(
        membership.Declaration(
            "join",
            "os:fixture",
            checkout=first.checkout,
            proposal=fs.id,
            target=mono_pid,
        )
    )
    # The declaring checkout's obsolete question is settled in the same
    # model call, before another filesystem sighting is needed.
    assert [p for p in model.pending() if p.checkout == first.checkout] == []
    from bettermemory import location

    location.forget()

    after_api, _ = lab.view(capi)
    after_root, _ = lab.view(c)
    # The file is inside the chain now: the folder is the part.
    assert after_api.project == api_pid
    assert after_root.project == mono_pid
    still = [p.kind for p in lab.model().pending() if p.checkout == first.checkout]
    # Promise: only a file bound outside the chain asks (L7).
    assert still == [], still
    assert after_api.outcome == SILENT, after_api.lines
    assert after_root.outcome == SILENT, after_root.lines
