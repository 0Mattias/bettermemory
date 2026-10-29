"""`health.render_text` escapes what the store and the log hand it (ENG-168).

A summary, a scope, a marker, an actor's spelling and a repo or work tree
come from a store an editor can write and a log a client stamps, so an
escape sequence or a line break in one is written as its escape
(`history.printable`), as posture and doctor print theirs: it cannot run in
the terminal or split a line of the report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from bettermemory.health import (
    CommitDriftDebt,
    CommitDriftRow,
    CrossRepoDrift,
    CrossRepoDriftGroup,
    MemoryStats,
    ScopeHealth,
    compute_health,
    render_text,
)
from bettermemory.identity import Actor
from bettermemory.models import Confidence, Memory, Source, generate_ulid

#: Every hostile shape the receipt names: an OSC 52 clipboard write ended
#: by BEL, a screen clear, a carriage return, a line feed and a pipe.
HOSTILE = "\x1b]52;c;cHduZWQ=\x07|\x1b[2J\rover\nwritten"


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _memory(*, body: str, scopes: list[str], actor: Actor | None = None) -> Memory:
    created = _utc(2026, 1, 1)
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=scopes,
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
        actor=actor,
    )


def _control_chars(text: str) -> set[str]:
    """The C0 and DEL characters `text` holds, a line feed and a tab apart:
    the report's own line breaks and its one kept whitespace."""
    return {
        ch for ch in text if (ord(ch) < 0x20 or ord(ch) == 0x7F) and ch not in "\n\t"
    }


def _report(text: str) -> str:
    """A report whose every store- and log-sourced field carries `text`."""
    # The model validates a scope's spelling, so the hostile scope rides
    # the rows built below; the body and the actor reach the report
    # through `compute_health` itself.
    memory = _memory(
        body=f"summary {text} tail",
        scopes=["tools"],
        actor=Actor(client=f"client{text}", model=f"model{text}", principal=f"p{text}"),
    )
    report = compute_health(
        [memory],
        [
            {
                "ts": "2026-04-01T00:00:00Z",
                "session": "s",
                "kind": "write",
                "markers": [f"marker{text}"],
            }
        ],
        window_days=30,
        now=_utc(2026, 5, 1),
    )
    stats = MemoryStats(
        id=f"id{text}",
        scopes=[f"scope{text}"],
        summary=f"summary {text}",
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        last_verified_at=None,
    )
    report.dead_weight = [stats]
    report.heavily_used = [stats]
    report.contradicted = [stats]
    report.rare_scopes = [f"scope{text}"]
    report.scope_health = [ScopeHealth(scope=f"scope{text}", active=1)]
    row = CommitDriftRow(
        id=f"id{text}",
        scopes=[f"scope{text}"],
        summary=f"summary {text}",
        last_verified_at=_utc(2026, 1, 1),
        commits_since_verify=2,
    )
    report.commit_drift_debt = CommitDriftDebt(
        current_repo=f"repo{text}",
        current_cwd=f"/cwd{text}",
        rows=[row],
        total_drifted=1,
    )
    report.cross_repo_drift = CrossRepoDrift(
        groups=[
            CrossRepoDriftGroup(
                repo=f"repo{text}",
                worktree_root=f"/wt{text}",
                rows=[row],
                total_drifted=1,
                candidates=1,
            )
        ],
        skipped=[
            {
                "repo": f"repo{text}",
                "worktree_root": f"/wt{text}",
                "reason": f"reason{text}",
            }
        ],
    )
    return render_text(report)


def test_render_text_escapes_every_store_and_log_sourced_string() -> None:
    """No control character survives into the text, every hostile field is
    written as its escape, and the report keeps the line count the same
    report has with plain strings: a line feed cannot add a line."""
    text = _report(HOSTILE)
    assert _control_chars(text) == set()
    escaped = "\\x1b]52;c;cHduZWQ=\\x07|\\x1b[2J\\rover\\nwritten"
    for field in (
        f"summary {escaped}",
        f"scope{escaped}",
        f"id{escaped}",
        f"marker{escaped}",
        f"client{escaped}",
        f"model{escaped}",
        f"p{escaped}",
        f"repo{escaped}",
        f"/wt{escaped}",
        f"reason{escaped}",
    ):
        assert field in text, field
    assert len(text.splitlines()) == len(_report("plain").splitlines())


def test_render_text_keeps_printable_text_as_it_is() -> None:
    """Plain text, a tab and a non-ASCII letter are printed unchanged."""
    text = _report("\tå|ok")
    assert "summary \tå|ok" in text
    assert "scope\tå|ok" in text
    assert "client\tå|ok" in text


def test_cli_membership_section_escapes_each_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bettermemory health` appends the membership section's lines, which
    quote checkout names and paths from the store: each is escaped too."""
    from bettermemory.cli import health_cmd, membership_cmd

    monkeypatch.setattr(membership_cmd, "health_model", lambda store: None)
    monkeypatch.setattr(membership_cmd, "health_dict", lambda model: {})
    monkeypatch.setattr(
        membership_cmd, "health_lines", lambda d: ["  checkout a\x1b[2J\nb: 1"]
    )
    store: Any = object()
    section = health_cmd._membership_section(store)
    assert section is not None
    assert section[1] == ["  checkout a\\x1b[2J\\nb: 1"]
