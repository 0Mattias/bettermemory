"""Unit tests for the auto-`record_use` session-state plumbing.

The end-to-end behaviour is covered by tests in
``test_server_v12_features.py``; the tests here exercise the
``SessionState`` plumbing in isolation so a failure mode (TTL
arithmetic, override semantics, mis-attribution guard) lands in a
specific test rather than as a smell in a high-level scenario test.
"""

from __future__ import annotations

from bettermemory.session import (
    DEFAULT_USE_TOKEN_TTL_TURNS,
    SessionState,
)


def _ids(state: SessionState) -> set[str]:
    return set(state.pending_use_tokens.keys())


def test_issue_use_tokens_returns_one_per_id() -> None:
    state = SessionState()
    out = state.issue_use_tokens(["a", "b", "c"])
    assert set(out.keys()) == {"a", "b", "c"}
    assert all(v.startswith("use_") for v in out.values())
    # Tokens are unique per call.
    assert len(set(out.values())) == 3


def test_issue_use_tokens_replaces_pending_for_same_id() -> None:
    """A re-issue for the same id swaps the token value — the
    server can't have two pending tokens for one memory at once."""
    state = SessionState()
    first = state.issue_use_tokens(["a"])["a"]
    second = state.issue_use_tokens(["a"])["a"]
    assert first != second
    assert state.pending_use_tokens["a"].token == second


def test_consume_old_tokens_returns_empty_when_within_ttl() -> None:
    state = SessionState()
    state.issue_use_tokens(["a"])
    out = state.consume_old_tokens(ttl_turns=DEFAULT_USE_TOKEN_TTL_TURNS)
    assert out == []
    assert "a" in state.pending_use_tokens


def test_consume_old_tokens_returns_aged_ids() -> None:
    # `min_age_seconds=0` isolates the TURN axis under test; the
    # wall-clock floor (3.14) has its own coverage in
    # test_telemetry_v2.py.
    state = SessionState()
    state.issue_use_tokens(["a"])
    # Advance enough turns to age out the token.
    for _ in range(DEFAULT_USE_TOKEN_TTL_TURNS + 1):
        state.advance_turn()
    out = state.consume_old_tokens(
        ttl_turns=DEFAULT_USE_TOKEN_TTL_TURNS, min_age_seconds=0
    )
    assert out == ["a"]
    assert "a" not in state.pending_use_tokens


def test_consume_old_tokens_respects_override_ids() -> None:
    """Ids in the override set must not be returned even when their
    tokens are old enough — the explicit-record_use path uses this
    to prevent the auto-commit from beating an override."""
    state = SessionState()
    state.issue_use_tokens(["a", "b"])
    for _ in range(DEFAULT_USE_TOKEN_TTL_TURNS + 1):
        state.advance_turn()
    out = state.consume_old_tokens(
        ttl_turns=DEFAULT_USE_TOKEN_TTL_TURNS,
        min_age_seconds=0,
        override_ids={"a"},
    )
    # Only `b` came back.
    assert out == ["b"]
    # `a` is still pending — the override didn't purge it. The
    # explicit-record_use path purges it separately via
    # `purge_use_token`.
    assert "a" in state.pending_use_tokens
    assert "b" not in state.pending_use_tokens


def test_purge_use_token_returns_true_when_present_false_otherwise() -> None:
    state = SessionState()
    state.issue_use_tokens(["a"])
    assert state.purge_use_token("a") is True
    assert state.purge_use_token("a") is False
    assert "a" not in state.pending_use_tokens


def test_advance_turn_returns_monotonic_count() -> None:
    state = SessionState()
    a = state.advance_turn()
    b = state.advance_turn()
    c = state.advance_turn()
    assert (a, b, c) == (1, 2, 3)


def test_reset_clears_pending_use_tokens() -> None:
    state = SessionState()
    state.issue_use_tokens(["a", "b"])
    state.reset()
    assert _ids(state) == set()


def test_misattribution_guard_per_id_aging() -> None:
    """A token issued at turn N and another at turn N+5 should NOT
    age out together: the per-id aging is independent."""
    state = SessionState()
    state.issue_use_tokens(["old"])
    for _ in range(DEFAULT_USE_TOKEN_TTL_TURNS + 1):
        state.advance_turn()
    # Now issue a fresh token — at turn ~3.
    state.issue_use_tokens(["fresh"])
    out = state.consume_old_tokens(
        ttl_turns=DEFAULT_USE_TOKEN_TTL_TURNS, min_age_seconds=0
    )
    # Only `old` is aged out; `fresh` is still pending.
    assert out == ["old"]
    assert "fresh" in state.pending_use_tokens
    assert "old" not in state.pending_use_tokens
