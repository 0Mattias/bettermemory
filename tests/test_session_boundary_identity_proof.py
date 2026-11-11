"""Only a declared conversation id joins a hook to the current session."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bettermemory.health import find_prior_session_boundary


@pytest.mark.parametrize("session_field", ["session", "session_id"])
@pytest.mark.parametrize("declared", ["transcript_current", "transcript_other", None])
def test_hook_boundary_uses_the_declared_conversation(
    session_field: str, declared: str | None
) -> None:
    prior = datetime(2026, 4, 1, tzinfo=timezone.utc)
    hook = datetime(2026, 4, 2, tzinfo=timezone.utc)
    events = [
        {session_field: "sess_prior", "ts": prior.isoformat()},
        {
            session_field: "transcript_current",
            "ts": hook.isoformat(),
            "triggered_from": "prompt_hook",
        },
        {session_field: "sess_current", "ts": "2026-04-03T00:00:00Z"},
    ]
    assert find_prior_session_boundary(
        events, "sess_current", current_harness_session_id=declared
    ) == (prior if declared == "transcript_current" else hook)


@pytest.mark.parametrize("session_field", ["session", "session_id"])
def test_own_hook_alone_does_not_invent_a_prior_session(session_field: str) -> None:
    events = [
        {
            session_field: "transcript_current",
            "ts": "2026-04-02T00:00:00Z",
            "triggered_from": "stop_hook",
        }
    ]
    assert (
        find_prior_session_boundary(
            events, "sess_current", current_harness_session_id="transcript_current"
        )
        is None
    )


@pytest.mark.parametrize("current", [None, ""])
def test_harness_declaration_does_not_replace_a_missing_tool_session(
    current: str | None,
) -> None:
    events = [{"session": "sess_prior", "ts": "2026-04-01T00:00:00Z"}]
    assert (
        find_prior_session_boundary(
            events, current, current_harness_session_id="transcript_current"
        )
        is None
    )
