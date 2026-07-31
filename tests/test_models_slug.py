"""Text-shaping regressions for `bettermemory.models` — `make_slug` and
the snippet truncation helpers.

The store calls `make_slug(memory.body)` then `build_filename(created, slug)`,
so anything `make_slug` puts at the front of the slug ends up duplicated
after the date prefix `build_filename` prepends. The regression captured
here is a real memory file from the maintainer's store —
`2026-05-07-2026-05-07-tightened-the-mvp.md` — whose body started with
"2026-05-07 tightened the mvp", so the slug builder pulled the date in as
three more "words" and the filename ended up with two prefixes.

The snippet section pins `_truncate_at_word`'s whitespace back-off (via
`snippet_for`): newlines count as word boundaries, so a markdown-list
body of paths/URLs is never hard-cut mid-token into a plausible-but-
wrong path. `snippet_window` — the mid-body form search hits use — is
pinned alongside it, because both spell the same ellipsis budget and a
shape test that lived next to only one of them would let them drift.
"""

from __future__ import annotations

import pytest

from bettermemory.models import make_slug, snippet_for, snippet_window


# ---------------------------------------------------------------------------
# Leading-date stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        # The original regression: ISO date + space + words.
        ("2026-05-07 tightened the mvp", "tightened-the-mvp"),
        # Hyphen separator instead of space.
        ("2026-05-07 - tightened the mvp", "tightened-the-mvp"),
        # Colon separator (date as a heading marker).
        ("2026-05-07: tightened the mvp", "tightened-the-mvp"),
        # Slash separator (date / title shape).
        ("2026-05-07 / tightened the mvp", "tightened-the-mvp"),
        # ISO datetime with `T` and seconds.
        ("2026-05-07T15:30:00 tightened the mvp", "tightened-the-mvp"),
        # ISO datetime with `Z` zone suffix.
        ("2026-05-07T15:30:00Z tightened the mvp", "tightened-the-mvp"),
        # ISO datetime with `+00:00` zone suffix.
        ("2026-05-07T15:30:00+00:00 tightened the mvp", "tightened-the-mvp"),
        # Lowercase `t` separator.
        ("2026-05-07t15:30 tightened the mvp", "tightened-the-mvp"),
    ],
)
def test_leading_iso_date_is_stripped(body: str, expected: str) -> None:
    assert make_slug(body) == expected


def test_leading_date_only_falls_back_to_memory_placeholder() -> None:
    # If the body is *only* a date, the stripped result is empty — the
    # `["memory"]` fallback should kick in instead of producing an empty
    # slug or the date.
    assert make_slug("2026-05-07") == "memory"


# ---------------------------------------------------------------------------
# Things the strip MUST NOT touch
# ---------------------------------------------------------------------------


def test_date_in_middle_is_preserved() -> None:
    # Only a *leading* date is stripped; a date inside the slug is part
    # of the meaningful content and must survive. Body kept short so the
    # date doesn't get truncated by the 6-word cap.
    body = "released 2026-05-07 cut"
    assert make_slug(body) == "released-2026-05-07-cut"


def test_year_only_is_preserved() -> None:
    # `2026 retro` is not an ISO date — the regex requires
    # `YYYY-MM-DD`. A bare year stays in the slug.
    assert make_slug("2026 retro") == "2026-retro"


def test_partial_date_is_preserved() -> None:
    # `2026-05` is not a full ISO date and must not be stripped.
    assert make_slug("2026-05 monthly review") == "2026-05-monthly-review"


def test_non_date_numeric_prefix_is_preserved() -> None:
    # A version-string prefix isn't a date — leave it alone.
    assert make_slug("1.2.3 release notes") == "1-2-3-release-notes"


# ---------------------------------------------------------------------------
# Existing behaviour the fix must not regress
# ---------------------------------------------------------------------------


def test_no_leading_date_unchanged() -> None:
    body = "tightened the mvp surface"
    assert make_slug(body) == "tightened-the-mvp-surface"


def test_first_line_only() -> None:
    body = "first line is the title\nsecond line gets ignored"
    assert make_slug(body) == "first-line-is-the-title"


def test_max_words_cap_still_applies_after_strip() -> None:
    # Strip the date, then cap to six words.
    body = "2026-05-07 alpha beta gamma delta epsilon zeta eta theta"
    assert make_slug(body) == "alpha-beta-gamma-delta-epsilon-zeta"


def test_empty_body_falls_back_to_memory() -> None:
    assert make_slug("") == "memory"


def test_whitespace_only_falls_back_to_memory() -> None:
    assert make_slug("   \n  \n") == "memory"


# ---------------------------------------------------------------------------
# Snippet truncation — `models._truncate_at_word` word boundaries
# ---------------------------------------------------------------------------


def test_snippet_backs_off_to_newline_boundary() -> None:
    """Regression: `_truncate_at_word` backed off only to `rfind(" ")` —
    ASCII space — while its docstring promises "the last whitespace
    boundary". A body shaped as a newline-separated list of paths (one
    per line, no spaces near the 200-char window) found no space in the
    back-off window and accepted the hard cut MID-PATH, so the snippet
    ended with a truncated token that read as a complete, plausible-but-
    wrong path (`/data/compose/backup/docker-co...`). Newlines (and
    tabs) now count as boundaries; the snippet must end on a complete
    line's path."""
    body = "\n".join(
        [
            "Compose files on helios:",
            "/data/compose/monitoring/docker-compose.yml",
            "/data/compose/reverse-proxy/docker-compose.yml",
            "/data/compose/media-stack/docker-compose.override.yml",
            "/data/compose/backup/docker-compose.yml restic side",
        ]
    )
    assert len(body) > 200  # must actually exercise the truncation path
    snippet = snippet_for(body, max_chars=200)
    assert snippet.endswith("...")
    # The body of the snippet (sans ellipsis) must end with a complete
    # path from the list, not a mid-token slice of the next one.
    head = snippet[:-3].rstrip()
    assert head.endswith(".yml"), (
        f"snippet hard-cut mid-path instead of backing off to the newline "
        f"boundary: {snippet!r}"
    )


# ---------------------------------------------------------------------------
# Mid-body windows — `models.snippet_window`
#
# Search hits window their snippet on the matched terms; this is the shape
# half of that, kept here beside `snippet_for` so the ellipsis arithmetic
# and the word-boundary back-off stay pinned in one place. Where the window
# SITS is the search side's decision and is pinned in `tests/test_search.py`.
# ---------------------------------------------------------------------------


def test_snippet_window_start_zero_is_snippet_for() -> None:
    """The head case delegates rather than re-deriving, so a future change
    to `snippet_for`'s back-off cannot leave the two spelling different
    truncations of the same body."""
    body = "abcdefgh " * 100

    assert snippet_window(body, 0) == snippet_for(body)
    assert snippet_window(body, -5) == snippet_for(body)
    assert snippet_window(body) == snippet_for(body)


def test_snippet_window_charges_the_leading_ellipsis_against_the_budget() -> None:
    """A mid-body window carries two ellipses and still fits the same
    budget a head snippet does — 3 + 197 + 3 == 200 + 3 — because the
    leading "..." is paid for out of the content allowance instead of
    being added on top."""
    body = (
        "lorem ipsum dolor sit amet consectetur adipiscing elit sed do " * 30
    ).strip()

    snippet = snippet_window(body, 500)

    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert len(snippet[3:-3]) <= 197
    assert len(snippet) <= 203
    # The bound holds wherever the window opens, including starts whose
    # tail has no whitespace near the edge to back off to.
    assert max(len(snippet_window(body, s)) for s in range(1, len(body))) <= 203


def test_snippet_window_short_tail_needs_no_trailing_ellipsis() -> None:
    """Second return shape: a window that reaches the end of the body has
    nothing left to elide, so it carries the leading ellipsis only. Callers
    that strip a fixed six characters would eat real content here."""
    body = ("abcdefgh " * 100).strip()

    snippet = snippet_window(body, len(body) - 50)

    assert snippet.startswith("...")
    assert not snippet.endswith("...")
    assert snippet[3:] == body[-50:]
