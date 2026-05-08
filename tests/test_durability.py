"""Unit tests for durability.py — the transient-marker detector."""

from __future__ import annotations

import pytest

from bettermemory.durability import (
    TRANSIENT_PHRASE_MARKERS,
    find_transient_markers,
)


# ---------------------------------------------------------------------------
# Negative cases — durable bodies should never trip the check
# ---------------------------------------------------------------------------


def test_empty_body_no_markers() -> None:
    assert find_transient_markers("") == []


def test_durable_body_no_markers() -> None:
    body = (
        "The auth service uses JWT with rotating refresh tokens. The refresh "
        "token TTL is 14 days; access tokens are 5 minutes."
    )
    assert find_transient_markers(body) == []


def test_word_boundary_currently_in_concurrently() -> None:
    """`currently` mustn't fire inside `concurrently` — distinct word."""
    body = "The lock manager handles concurrently-issued requests fairly."
    hits = find_transient_markers(body)
    assert all(h.marker != "currently" for h in hits)


def test_word_boundary_new_in_news() -> None:
    """`the new` mustn't fire inside `the news`."""
    body = "Skim the news feed once a week to catch breaking changes."
    hits = find_transient_markers(body)
    assert all(h.marker != "the new" for h in hits)


def test_six_char_hex_does_not_trigger_sha_marker() -> None:
    """SHA detection requires 7+ chars (git short-SHA default)."""
    body = "The colour value abc123 is blue."  # 6 chars.
    assert find_transient_markers(body) == []


def test_uppercase_hex_does_not_trigger_sha_marker() -> None:
    """ULIDs (and other uppercase hex IDs) shouldn't be misread as SHAs."""
    body = "Memory id 01HXYZABCDEF identifies the entry."
    hits = find_transient_markers(body)
    assert all(not h.marker.startswith("sha:") for h in hits)


# ---------------------------------------------------------------------------
# Positive cases — every marker phrase fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", TRANSIENT_PHRASE_MARKERS)
def test_each_phrase_marker_fires(phrase: str) -> None:
    body = f"Some context, {phrase} and more context after."
    hits = find_transient_markers(body)
    assert any(h.marker == phrase for h in hits), (
        f"expected marker {phrase!r} to fire, got {[h.marker for h in hits]}"
    )


def test_phrase_match_is_case_insensitive() -> None:
    body = "CURRENTLY the database is Postgres."
    hits = find_transient_markers(body)
    assert any(h.marker == "currently" for h in hits)


def test_seven_char_sha_fires() -> None:
    body = "Look at commit a1b2c3d for the change."
    hits = find_transient_markers(body)
    sha_hits = [h for h in hits if h.marker.startswith("sha:")]
    assert sha_hits, "expected SHA hit"
    assert sha_hits[0].marker == "sha:a1b2c3d"


def test_forty_char_sha_fires() -> None:
    sha = "a" * 40
    body = f"Pinned to commit {sha} for the refactor."
    hits = find_transient_markers(body)
    assert any(h.marker.startswith("sha:") for h in hits)


def test_forty_one_char_hex_does_not_fire() -> None:
    """Above the SHA upper bound — large hex blobs shouldn't trip."""
    body = "Hash digest " + ("a" * 41) + " is in the cache."
    hits = find_transient_markers(body)
    # The 41-char run has no \b at position 40, so no 40-char prefix
    # match either — the regex only considers maximal hex runs.
    assert all(not h.marker.startswith("sha:") for h in hits)


# ---------------------------------------------------------------------------
# Deduplication and bucketing
# ---------------------------------------------------------------------------


def test_repeated_phrase_reported_once() -> None:
    """Same marker twice in one body collapses to one TransientMatch."""
    body = (
        "Currently the tests pass, and the build is currently fine. "
        "But currently we have no CI."
    )
    hits = find_transient_markers(body)
    currently_hits = [h for h in hits if h.marker == "currently"]
    assert len(currently_hits) == 1


def test_multiple_distinct_markers_each_reported() -> None:
    body = "Today I refactored the auth flow. Currently the tests pass."
    hits = find_transient_markers(body)
    markers = {h.marker for h in hits}
    assert "today i" in markers
    assert "currently" in markers


def test_multiple_shas_reported_once_under_one_marker() -> None:
    """Five SHAs in a row collapse to one bucket — reading 5 entries adds
    no signal beyond 'you're putting branch state in memory'."""
    body = (
        "Branch is at a1b2c3d, parent of e4f5a6b, sibling of c7d8e9f, "
        "cherry-picked from b1a2c3d, into trunk b7e8f9a."
    )
    hits = find_transient_markers(body)
    sha_hits = [h for h in hits if h.marker.startswith("sha:")]
    assert len(sha_hits) == 1


# ---------------------------------------------------------------------------
# Snippet helper — error-message context
# ---------------------------------------------------------------------------


def test_snippet_includes_match_and_surrounding_context() -> None:
    body = (
        "The deployment pipeline is currently using GitHub Actions for "
        "the runner pool."
    )
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert "currently" in currently.snippet.lower()


def test_snippet_collapses_whitespace_to_one_line() -> None:
    body = "Some\n\nlong\n\ncontext\n\ncurrently\n\nspans\n\nlines."
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert "\n" not in currently.snippet


def test_snippet_uses_ellipses_when_truncated() -> None:
    body = ("a" * 100) + " currently the answer is " + ("b" * 100)
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert currently.snippet.startswith("...")
    assert currently.snippet.endswith("...")


def test_snippet_no_leading_ellipsis_at_body_start() -> None:
    body = "Currently the answer is forty-two."
    hits = find_transient_markers(body)
    currently = next(h for h in hits if h.marker == "currently")
    assert not currently.snippet.startswith("...")
