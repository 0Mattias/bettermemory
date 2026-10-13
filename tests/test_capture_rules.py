"""The capture rules: the owner's bar (`capture.CAPTURE_RULES`), the
instruction that carries it, and the part of it the daemon enforces on a
capture write (`durability.find_capture_rule_hits`).

The bar is carried byte for byte from the in-session design on the
`capture-in-session` branch (217ed02), so the hash below is the one the
declaration names. The enforced rules are the four the declaration lists:
rule 3 (a plain subject), rule 1 (no to-dos, no counts that drift), and
rule 5 (absolute dates); the transient gate is rule 1's other half and
runs on a capture write with its acknowledge flag ignored
(tests/test_capture_batch.py).
"""

from __future__ import annotations

import hashlib

import pytest

from bettermemory import capture
from bettermemory.durability import CaptureRuleHit, find_capture_rule_hits

TOKEN = "0123456789abcdef"


def _names(body: str) -> set[str]:
    return {hit.name for hit in find_capture_rule_hits(body)}


# ---------------------------------------------------------------------------
# The bar and the instruction
# ---------------------------------------------------------------------------


def test_capture_rules_text_is_the_217ed02_bar() -> None:
    assert len(capture.CAPTURE_RULES) == 1_274
    digest = hashlib.sha256(capture.CAPTURE_RULES.encode("utf-8")).hexdigest()
    assert digest.startswith("69ab74f4b20bd067")


def test_the_instruction_carries_the_bar_and_the_added_lines() -> None:
    text = capture.in_session_instruction(
        token=TOKEN,
        since="2026-10-12T09:00:00Z",
        scopes=["projects:bettermemory"],
        prompt_times=["2026-10-12T09:01:00Z", "2026-10-12T09:20:00Z"],
    )
    assert capture.CAPTURE_RULES in text
    # The batch token, as the source every capture write carries.
    assert f'source="capture:{TOKEN}"' in text
    # The project scope from the hook's cwd, for that project's memories
    # only: another project's memory takes its own scope.
    assert '"projects:bettermemory"' in text
    assert "use it only for memories about that project" in text
    assert "its subject's own scope (projects:<name>)" in text
    # A body is what the user said or adopted, never the assistant's own
    # caveat; the line stands outside the owner's bar.
    assert (
        "A body records what the user said, decided or adopted, never your "
        "own suggestions or caveats unless the user adopted them."
    ) in text
    assert "caveats" not in capture.CAPTURE_RULES
    # Rule 9 amended for a capture: supersedes, never an update.
    assert "supersedes" in text
    assert "Do not update, remove or verify" in text
    # The UTC times of the user's messages.
    assert "2026-10-12T09:01:00Z" in text and "2026-10-12T09:20:00Z" in text
    # The write review is answered, source included.
    assert "review" in text
    assert "from the user's memory setup" in text


def test_without_a_folder_scope_no_scope_line_is_offered() -> None:
    text = capture.in_session_instruction(token=TOKEN, since=None, scopes=[])
    assert "This folder's project scope" not in text


def test_the_instruction_keeps_at_most_twenty_prompt_times() -> None:
    times = [f"2026-10-12T09:{m:02d}:00Z" for m in range(30)]
    text = capture.in_session_instruction(
        token=TOKEN, since=None, scopes=[], prompt_times=times
    )
    kept = [t for t in times if t in text]
    assert len(kept) == capture.MAX_PROMPT_TIMES == 20
    # The most recent twenty: the stretch's end is what the model holds best.
    assert kept == times[-20:]


def test_a_compacted_stretch_adds_one_line() -> None:
    plain = capture.in_session_instruction(token=TOKEN, since=None, scopes=[])
    compacted = capture.in_session_instruction(
        token=TOKEN, since=None, scopes=[], compacted=True
    )
    assert compacted.startswith(plain.split("\n\n")[0])
    assert "compacted" in compacted and "compacted" not in plain


def test_the_longest_instruction_fits_the_hook_cap() -> None:
    """Claude Code drops what passes 10,000 characters of a hook's
    additionalContext; the longest arguments the daemon passes stay far
    inside it."""
    text = capture.in_session_instruction(
        token=TOKEN,
        since="2026-10-12T09:00:00.000000Z",
        scopes=["projects:" + "x" * 200, "y" * 200],
        prompt_times=["2026-10-12T09:00:00.000000Z"] * 50,
        compacted=True,
    )
    assert len(text) < capture.INSTRUCTION_MAX_CHARS // 2


# ---------------------------------------------------------------------------
# The enforced rules
# ---------------------------------------------------------------------------

KNOWN_BAD: list[tuple[str, str, int]] = [
    ("The user's project bettermemory has zero GitHub stars.", "plain_subject", 3),
    ("The assistant said the daemon binds 127.0.0.1 only.", "plain_subject", 3),
    ("The assistant's recommendation was to pin ruff 0.9.", "plain_subject", 3),
    ("Release version 8.0.0 in the next session.", "no_todo", 1),
    ("A DNS record for the old staging host is still to delete.", "no_todo", 1),
    ("Next steps: declare U6, then run the rot arm.", "no_todo", 1),
    ("- [ ] port the undo command to the daemon", "no_todo", 1),
    (
        "The owner switched the live install back to uvx two days ago.",
        "absolute_date",
        5,
    ),
    ("bettermemory is now one daemon per store.", "absolute_date", 5),
    ("The v9 suite passes with 3,707 passed and 2 skipped.", "test_total", 1),
]


@pytest.mark.parametrize(("body", "name", "rule"), KNOWN_BAD)
def test_the_known_bad_bodies_are_refused_with_their_rule(
    body: str, name: str, rule: int
) -> None:
    hits = find_capture_rule_hits(body)
    assert name in {h.name for h in hits}, hits
    hit = next(h for h in hits if h.name == name)
    assert hit.rule == rule
    assert hit.snippet


CONSTRUCTED_GOOD: list[str] = [
    "The user prefers plain declarative prose.",
    "Claude Code reads the user's settings from ~/.claude/settings.json.",
    "Stop hooks receive stop_hook_active and last_assistant_message.",
    "2026-09-25: the owner ruled that capture uses the chat's own model or none.",
    "At 39d544b the v9 suite read 3,707 passed and 2 skipped in 265.67 s.",
    "bettermemory had 41 GitHub stars on 2026-10-01.",
    "The cache evicts least recently used entries first.",
    "Release 8.0.0 shipped the nine-tool surface on 2026-09-20.",
]


@pytest.mark.parametrize("body", CONSTRUCTED_GOOD)
def test_the_constructed_good_bodies_pass(body: str) -> None:
    assert find_capture_rule_hits(body) == []


def test_quoted_spans_are_exempt() -> None:
    body = (
        "The owner's ruling of 2026-09-25 reads \"Next steps: none, ship it "
        'now" and was recorded verbatim.'
    )
    assert find_capture_rule_hits(body) == []
    # The same phrase in the author's own voice still fires.
    assert "no_todo" in _names("Next steps: none, ship it.")


def test_a_how_to_apply_imperative_passes() -> None:
    body = (
        "The daemon refuses a capture write without its batch token.\n"
        "**Why:** subagents share the session id.\n"
        "**How to apply:** Copy the token into source on every write."
    )
    assert find_capture_rule_hits(body) == []


def test_dated_counts_and_anchored_test_totals_pass() -> None:
    assert _names("The repo has 41 GitHub stars.") == {"drifting_count"}
    assert find_capture_rule_hits("The repo had 41 GitHub stars on 2026-10-01.") == []
    assert _names("CI reports 12 failed tests.") == {"test_total"}
    assert find_capture_rule_hits("On 2026-10-12 CI reported 12 failed tests.") == []
    assert find_capture_rule_hits("At eceb9a5 the suite read 4836 passed.") == []


@pytest.mark.parametrize(
    "body",
    [
        "The fix landed recently.",
        "The owner moved the store last week.",
        "The release is planned for next quarter.",
        "The daemon was restarted a while ago.",
        "Builds are slow these days.",
    ],
)
def test_relative_dates_are_refused(body: str) -> None:
    assert "absolute_date" in _names(body)


@pytest.mark.parametrize(
    "body",
    [
        "The spec defines what is now called the Stop hook.",
        "The tool is now known as bettermemory.",
        "The most recently written memory wins a tie.",
        "The panel lists recently viewed files.",
    ],
)
def test_the_dual_use_date_words_pass(body: str) -> None:
    assert "absolute_date" not in _names(body)


@pytest.mark.parametrize(
    "body",
    [
        "TODO: wire the endpoint.",
        "The migration is yet to be reviewed.",
        "Two tests are left to do.",
        "Revisit the cap when we resume.",
        "Fix the flaky hook test.",
    ],
)
def test_to_dos_are_refused(body: str) -> None:
    assert "no_todo" in _names(body)


def test_a_bare_noun_opening_is_not_an_imperative() -> None:
    assert find_capture_rule_hits("Fix ownership lies with the release manager.") == []
    assert find_capture_rule_hits("Run times are logged per test.") == []


def test_one_hit_per_rule_name_with_the_first_phrase() -> None:
    hits = find_capture_rule_hits(
        "The user's notes were moved recently. The user's tool changed lately."
    )
    assert [h.name for h in hits].count("plain_subject") == 1
    assert [h.name for h in hits].count("absolute_date") == 1
    first = next(h for h in hits if h.name == "absolute_date")
    assert "recently" in first.snippet


def test_a_hit_names_the_rule_number_and_the_phrase() -> None:
    hit = find_capture_rule_hits("The assistant said the cap is 12.")[0]
    assert isinstance(hit, CaptureRuleHit)
    assert hit.to_dict() == {
        "rule": 3,
        "name": "plain_subject",
        "snippet": hit.snippet,
    }
    assert "assistant said" in hit.snippet
