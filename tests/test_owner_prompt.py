"""The one definition of an owner prompt (`owner_prompt`, RS-F5).

Pinned here: a row's marks are read first (``isMeta``,
``isCompactSummary``, ``turnOrigin`` when present) and its text second
(the app's envelope blocks removed, a composed opening refused, the cut at
2,000 characters); every surface that judges or hashes a prompt resolves
to it (`recall.clean_prompt`, `hook.human_prompt`,
`hook.is_synthetic_prompt`, capture's stretch reader, the recall review's
rows); and a prompt hashes to the same bytes on the recall's row and on
the Stop's. `clean_prompt` reads every prompt without an artifact view
byte for byte as 9.0.0rc3 read it, pinned against that implementation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from bettermemory import capture_hook
from bettermemory.events import redact_query
from bettermemory.hook import audit_transcript, human_prompt, is_synthetic_prompt
from bettermemory.owner_prompt import (
    COMPOSED_OPENINGS,
    ENVELOPE_TAGS,
    HARNESS_OPENINGS,
    PROMPT_CUT_CHARS,
    is_submitted,
    owner_prompt,
    owner_text,
    probe_text,
    queued_text,
    submitted_text,
    text_blocks,
)
from bettermemory.recall import clean_prompt
from bettermemory.recall_review import _user_rows

from . import _transcript_rows as tr
from .test_recall_prompt import QUERY, REMINDER, PromptWorld

TYPED = "where does the backup strategy run its restic job"
ARTIFACT_VIEW = (
    '<artifact-view-context url="https://example.test/a/1">\n'
    "page: release checklist\n</artifact-view-context>"
)


def _row(text: str | list[dict[str, Any]], **marks: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, **marks}


# ---------------------------------------------------------------------------
# Marks first
# ---------------------------------------------------------------------------


def test_an_is_meta_row_is_not_the_owners() -> None:
    assert owner_prompt(_row(TYPED, isMeta=True)) is None
    assert owner_prompt(_row(TYPED)) == TYPED


def test_a_compaction_summary_is_not_the_owners() -> None:
    assert owner_prompt(_row(TYPED, isCompactSummary=True)) is None


def test_a_task_notification_origin_with_plain_text_is_rejected() -> None:
    """The row's origin decides when it is present, whatever the text
    reads like."""
    assert owner_prompt(_row(TYPED, turnOrigin="task_notification")) is None


@pytest.mark.parametrize("origin", ["peer", "sdk", "system", "anything-else"])
def test_every_origin_but_human_is_rejected(origin: str) -> None:
    assert owner_prompt(_row(TYPED, turnOrigin=origin)) is None


def test_a_human_origin_is_still_judged_by_its_text() -> None:
    assert owner_prompt(_row(TYPED, turnOrigin="human")) == TYPED
    assert owner_prompt(_row(REMINDER, turnOrigin="human")) is None
    composed = "<task-notification>x</task-notification>"
    assert owner_prompt(_row(composed, turnOrigin="human")) is None


def test_a_tool_result_row_is_never_submitted() -> None:
    row = tr.tool_result(10)
    row["message"]["content"].append({"type": "text", "text": TYPED})
    assert not is_submitted(row)
    assert submitted_text(row) is None
    assert owner_prompt(row) is None


def test_text_and_image_blocks_are_a_submitted_row() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "data": "AA=="}},
        {"type": "text", "text": "first"},
        {"type": "text", "text": ""},
        {"type": "text", "text": "second"},
    ]
    row = _row(blocks)
    assert is_submitted(row)
    assert submitted_text(row) == "first\nsecond"
    assert owner_prompt(row) == "first\nsecond"
    assert owner_prompt(_row([{"type": "image", "source": {}}])) is None


def test_only_user_rows_are_submitted() -> None:
    assert not is_submitted({"type": "assistant", "message": {"content": TYPED}})
    assert not is_submitted({"type": "user", "message": {"content": None}})
    assert not is_submitted({"type": "user", "message": TYPED})
    assert owner_prompt({"type": "system", "message": {"content": TYPED}}) is None


# ---------------------------------------------------------------------------
# Then the text
# ---------------------------------------------------------------------------


def test_a_reminder_and_typed_text_gives_the_typed_text() -> None:
    assert owner_prompt(_row(f"{REMINDER}\n\n{TYPED}")) == f" \n\n{TYPED}"
    assert (owner_text(f"{REMINDER}\n\n{TYPED}") or "").strip() == TYPED


def test_a_reminder_only_row_gives_none() -> None:
    assert owner_prompt(_row(REMINDER)) is None
    assert owner_prompt(_row(f"{REMINDER}\n{REMINDER}\n  ")) is None


def test_an_artifact_view_and_typed_text_gives_the_typed_text() -> None:
    assert (owner_prompt(_row(f"{ARTIFACT_VIEW}\n{TYPED}")) or "").strip() == TYPED
    assert owner_prompt(_row(ARTIFACT_VIEW)) is None


def test_every_envelope_is_removed_wherever_it_stands() -> None:
    text = (
        f"{TYPED} <system-reminder>a</system-reminder> and "
        "<PASTED_CONTENT id=1>b</pasted_content> then "
        f"{ARTIFACT_VIEW} done <pasted_content ref=2 />."
    )
    assert probe_text(text) == f"{TYPED}   and   then   done  ."
    assert ENVELOPE_TAGS == (
        "system-reminder",
        "pasted_content",
        "artifact-view-context",
    )


def test_an_unclosed_envelope_at_the_start_is_composed() -> None:
    for tag in ENVELOPE_TAGS:
        assert owner_text(f"<{tag}>\n{TYPED}") is None
    # Further in, it stays in the owner's text, as it did.
    assert owner_text(f"{TYPED} <system-reminder>") == f"{TYPED} <system-reminder>"


@pytest.mark.parametrize("opening", HARNESS_OPENINGS)
def test_each_harness_opening_is_refused(opening: str) -> None:
    assert owner_text(f"  {opening} {TYPED}") is None
    assert owner_text(f"{REMINDER}\n{opening}{TYPED}") is None
    assert owner_text(f"{TYPED} {opening}") is not None


def test_the_composed_openings_are_the_envelopes_then_the_harness() -> None:
    assert COMPOSED_OPENINGS == (
        "<system-reminder",
        "<pasted_content",
        "<artifact-view-context",
        *HARNESS_OPENINGS,
    )
    assert "[Request interrupted" in HARNESS_OPENINGS


def test_blank_text_is_not_the_owners() -> None:
    assert owner_text("") is None
    assert owner_text(" \n\t ") is None
    assert owner_text("<system-reminder>x</system-reminder>   ") is None


def test_the_cut_is_part_of_the_definition() -> None:
    long = "a" * (PROMPT_CUT_CHARS + 500)
    assert owner_text(long) == "a" * PROMPT_CUT_CHARS
    assert owner_prompt(_row(long)) == "a" * PROMPT_CUT_CHARS
    # The envelopes go first, so a block beyond the cut costs nothing.
    padded = "b" * 10 + "<system-reminder>" + "c" * 5000 + "</system-reminder>" + "d"
    assert probe_text(padded) == "b" * 10 + " d"
    assert PROMPT_CUT_CHARS == 2_000


def test_the_words_are_kept_whole_on_request() -> None:
    """A reader that counts the owner's words rather than probes them
    (capture) asks for them uncut; the test of whose words they are does
    not change with the cut."""
    long = "a" * (PROMPT_CUT_CHARS + 500)
    assert owner_text(long, cut=False) == long
    assert owner_prompt(_row(long), cut=False) == long
    assert owner_prompt(_row(f"{REMINDER}\n{long}"), cut=False) == f" \n{long}"
    assert owner_text(REMINDER, cut=False) is None
    assert owner_prompt(_row(long, turnOrigin="sdk"), cut=False) is None


def test_text_blocks_reads_text_only() -> None:
    assert text_blocks(None) is None
    assert text_blocks("a string") is None
    assert text_blocks([{"type": "thinking", "thinking": "x"}]) is None
    assert text_blocks([{"type": "text", "text": "a"}, "junk", {"type": "text"}]) == "a"


# ---------------------------------------------------------------------------
# clean_prompt reads as it did
# ---------------------------------------------------------------------------

# 9.0.0rc3's `recall.clean_prompt`, verbatim, as the reference the hashes the
# log holds were computed with.
_RC3_PASTED = re.compile(
    r"<pasted_content\b[^>]*?(?:/>|>.*?</pasted_content\s*>)",
    re.DOTALL | re.IGNORECASE,
)
_RC3_REMINDER = re.compile(
    r"<system-reminder\b[^>]*>.*?</system-reminder\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _rc3_clean_prompt(prompt: str) -> str:
    text = _RC3_REMINDER.sub(" ", prompt)
    return _RC3_PASTED.sub(" ", text)[:PROMPT_CUT_CHARS]


@pytest.mark.parametrize(
    "prompt",
    [
        QUERY,
        TYPED,
        "",
        "   leading and trailing   ",
        "unicode: café € \U0001f600   line",
        "x" * 5_000,
        f"{REMINDER}\n\n{QUERY}",
        f"<system-reminder a=b>one</system-reminder><system-reminder>two</system-reminder>{QUERY}",
        f'see <pasted_content id="1">{QUERY}</pasted_content> and <pasted_content r=2 /> end',
        f"<pasted_content>A<system-reminder>B</system-reminder>C</pasted_content>{QUERY}",
        f"<system-reminder>A<pasted_content>B</pasted_content>C</system-reminder>{QUERY}",
        f"<pasted_content>A<system-reminder>B</pasted_content>C</system-reminder>{QUERY}",
        f"<system-reminder>unclosed {QUERY}",
        f"{'y' * 1990}<system-reminder>{'z' * 30}</system-reminder>tail",
        "the <task-notification> fired twice",
    ],
)
def test_clean_prompt_reads_a_prompt_without_an_artifact_view_as_rc3_did(
    prompt: str,
) -> None:
    assert clean_prompt(prompt) == _rc3_clean_prompt(prompt)
    assert redact_query(clean_prompt(prompt)) == redact_query(_rc3_clean_prompt(prompt))


def test_clean_prompt_differs_from_rc3_only_on_an_artifact_view() -> None:
    prompt = f"{ARTIFACT_VIEW}\n{QUERY}"
    assert clean_prompt(prompt) == f" \n{QUERY}"
    assert _rc3_clean_prompt(prompt) == prompt


# ---------------------------------------------------------------------------
# Every surface resolves to it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        TYPED,
        REMINDER,
        f"{REMINDER}\n{TYPED}",
        f"{ARTIFACT_VIEW}{TYPED}",
        "<task-notification>done</task-notification>",
        "[Request interrupted by user]",
        "",
        "x" * 3_000,
    ],
)
def test_the_wrappers_are_the_definition(text: str) -> None:
    assert clean_prompt(text) == probe_text(text)
    assert is_synthetic_prompt(text) is (owner_text(text) is None)
    assert human_prompt(_row(text)) == owner_prompt(_row(text)) == owner_text(text)


def test_capture_counts_the_owners_text_by_the_definition(tmp_path: Path) -> None:
    """A reminder-wrapped prompt is the owner's and counts its typed text;
    rows another origin submitted, or the harness composed, count
    nothing and leave the reply tail running."""

    def at(minute: int) -> str:
        return f"2026-10-19T10:{minute:02d}:00Z"

    transcript = tr.write(
        tmp_path / "t.jsonl",
        tr.user(f"{REMINDER}\n{TYPED}", timestamp=at(1)),
        tr.assistant("ok"),
        tr.user(TYPED, turnOrigin="sdk", timestamp=at(2)),
        tr.user(TYPED, turnOrigin="task_notification", timestamp=at(3)),
        tr.user("<task-notification>done</task-notification>", timestamp=at(4)),
        tr.assistant("fine"),
    )
    stretch = capture_hook.read_stretch(transcript, 0)
    owner = owner_text(f"{REMINDER}\n{TYPED}")
    assert owner is not None
    assert stretch.chars == len(owner) + len("ok") + len("fine")
    assert stretch.prompt_times == [at(1)]
    assert stretch.tail == "ok\nfine"


def test_capture_counts_a_long_prompt_whole(tmp_path: Path) -> None:
    """The probe's cut is the probe's: capture counts every character the
    owner typed."""
    long = "b" * (PROMPT_CUT_CHARS + 100)
    transcript = tr.write(
        tmp_path / "t.jsonl",
        tr.user(long, timestamp="2026-10-19T10:01:00Z"),
        tr.assistant("ok"),
    )
    stretch = capture_hook.read_stretch(transcript, 0)
    assert stretch.chars == len(long) + len("ok")


def test_the_review_marks_its_rows_by_the_definition() -> None:
    rows = [
        tr.user(f"{REMINDER}\n{TYPED}", "p1"),
        tr.user(TYPED, "p2", turnOrigin="task_notification"),
        tr.user(REMINDER, "p3"),
        tr.user(TYPED, "p4", isMeta=True),
    ]
    marked = _user_rows(rows)
    assert [(row.prompt_id, row.human) for row in marked] == [
        ("p1", True),
        ("p2", False),
        ("p3", False),
        ("p4", False),
    ]
    assert (
        marked[0].digest == redact_query(clean_prompt(f"{REMINDER}\n{TYPED}"))["hash"]
    )


def test_a_subagents_row_is_not_the_owners() -> None:
    """A row carrying ``isSidechain`` or ``agentId`` is a subagent's turn,
    whether it is a submitted row or a queued command; the harness writes
    both marks on the row, as it does for every row of a subagent's file."""
    assert owner_prompt(tr.user(TYPED, "p1", isSidechain=True)) is None
    assert owner_prompt(tr.user(TYPED, "p1", agentId="a1")) is None
    assert human_prompt(tr.user(TYPED, "p1", isSidechain=True)) is None
    assert queued_text({**tr.queued(TYPED), "isSidechain": True}) is None
    assert queued_text({**tr.queued(TYPED), "agentId": "a1"}) is None
    # The marks absent or false leave the row the owner's.
    assert owner_prompt(tr.user(TYPED, "p1", isSidechain=False)) == TYPED
    assert queued_text({**tr.queued(TYPED), "isSidechain": False}) == TYPED
    assert [row.human for row in _user_rows([tr.user(TYPED, "p1", agentId="a1")])] == [
        False
    ]


# ---------------------------------------------------------------------------
# Queued commands (RS-F7 part 7)
# ---------------------------------------------------------------------------


def test_a_queued_command_is_read_beside_the_submitted_rows() -> None:
    """A queued-command attachment row holds what the owner typed while a
    turn ran: `queued_text` returns it, and the hook's readers, which
    judge turns, do not see it as one."""
    row = tr.queued(TYPED)
    assert queued_text(row) == TYPED
    assert not is_submitted(row)
    assert submitted_text(row) is None
    assert owner_prompt(row) is None
    assert human_prompt(row) is None


def test_a_queued_note_the_app_flags_or_leaves_blank_is_not_read() -> None:
    assert queued_text(tr.queued(TYPED, isMeta=True)) is None
    assert queued_text(tr.queued("   ")) is None
    assert queued_text(tr.user(TYPED, "p1")) is None
    assert queued_text({"type": "attachment", "attachment": {"type": "other"}}) is None
    assert queued_text({"type": "attachment", "attachment": "queued_command"}) is None


def test_the_review_reads_a_queued_command_as_a_row_without_a_prompt_id() -> None:
    """The review's rows carry a queued command, hashed as the log hashes
    a probe, the owner's by `owner_text`'s test and never with a prompt
    id; one the app composed is not the owner's."""
    marked = _user_rows(
        [
            tr.user(TYPED, "p1"),
            tr.queued(f"{TYPED} and the retention"),
            tr.queued(HARNESS_OPENINGS[0] + " queued by the app"),
            tr.queued(TYPED, isMeta=True),
        ]
    )
    assert [(row.prompt_id, row.human) for row in marked] == [
        ("p1", True),
        (None, True),
        (None, False),
    ]
    assert marked[1].text == f"{TYPED} and the retention"
    assert (
        marked[1].digest
        == redact_query(clean_prompt(f"{TYPED} and the retention"))["hash"]
    )


# ---------------------------------------------------------------------------
# One hash on both paths
# ---------------------------------------------------------------------------


def test_a_reminder_wrapped_prompt_hashes_alike_on_the_recall_and_the_stop(
    tmp_path: Path,
) -> None:
    """The prompt recall probes the UserPromptSubmit payload's text and the
    Stop the transcript row of the same turn; both read it through the
    one definition, so the two rows carry one `probe_query` hash. Before
    RS-F5 the Stop did not probe a reminder-wrapped row at all."""
    world = PromptWorld(tmp_path)
    world.write()
    prompt = f"{REMINDER}\n\n{QUERY}"
    delivered = world.recall(prompt, session_id="transcript-hash", prompt_id="p-1")
    assert delivered.ids
    transcript = tr.write(
        tmp_path / "t.jsonl", tr.user(prompt, "p-1"), tr.assistant("noted")
    )
    audit_transcript(
        transcript_path=transcript,
        session_id="transcript-hash",
        config=world.config,
        store=world.store,
        prompt_id="p-1",
        cwd=world.worktree,
    )
    events = list(world.store.iter_events())
    [recall_row] = [e for e in events if e["kind"] == "prompt_recall"]
    [stop_row] = [e for e in events if e["kind"] == "turn_audited"]
    assert stop_row["probe_query"]["hash"] == recall_row["probe_query"]["hash"]
    assert stop_row["probe_query"] == recall_row["probe_query"]
    owner = owner_text(prompt)
    assert owner is not None
    assert recall_row["probe_query"]["hash"] == redact_query(owner)["hash"]
