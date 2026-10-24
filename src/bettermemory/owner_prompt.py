"""The one definition of an owner prompt: what the owner typed into a turn,
as the probe reads it, or None.

Every surface that judges or hashes a prompt reads it here: the prompt
recall (the UserPromptSubmit payload's text, `owner_text`), and the Stop
audit, capture's stretch reader and the recall review (a transcript row,
`owner_prompt`). A row is judged by its marks first and its text second.

Marks. A row flagged ``isMeta`` (a skill or command expansion, the wrapper
a message from another session is kept behind) or ``isCompactSummary`` (a
compaction's summary) is not the owner's. A row that carries
``turnOrigin`` (Claude Code 2.1.280 and later) is the owner's only when it
says ``human``; the other values name who else submitted it
(``task_notification``, ``peer``, ``sdk``, ``system``). A row without the
key is judged by its text alone, as the payload is.

Text. The complete app envelope blocks (`ENVELOPE_TAGS`) are removed
wherever they stand, and the rest is cut at `PROMPT_CUT_CHARS`
(`probe_text`). What is left is not the owner's when it is blank or opens
with a shape the harness composes (`COMPOSED_OPENINGS`): a harness tag, a
harness sentence, or an envelope tag left unclosed.

The cut is part of the definition, so the recall's `probe_query` hash and
the Stop's are computed over the same bytes; a reader that counts the
owner's words rather than probes them (capture) asks for them whole with
``cut=False``, and the test of whose words they are is the same. The
module imports nothing from the package, so any module may read it.

Queued commands. What the owner types while a turn runs, the harness
holds for the next turn and writes to the transcript as a row of type
``attachment`` carrying a ``queued_command`` (`queued_text`). Such a row
is no turn of its own, so `is_submitted` and `owner_prompt` do not read
it; the readers that account for every prompt the owner typed (the
recall review, the RS read scripts) read it beside the submitted rows
and judge its words by `owner_text`, as a submitted row's are judged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The most characters of a prompt the probe reads.
PROMPT_CUT_CHARS = 2_000

#: The app's envelope blocks: material it wraps around or ahead of what
#: the owner typed (a session's first prompt opens with reminder blocks,
#: a paste is handed over as a block, an artifact view names the page the
#: owner looks at). A complete block is removed wherever it stands, in
#: this order: reminders and pastes first, as the probe removed them
#: before artifact views joined the set, so a prompt without an artifact
#: view reads byte for byte as it did and the hashes the log holds keep
#: matching.
ENVELOPE_TAGS: tuple[str, ...] = (
    "system-reminder",
    "pasted_content",
    "artifact-view-context",
)

#: The openings of text the harness composes and no one typed: background
#: task notifications, slash-command bookkeeping and stdout wrappers, a
#: message from another session (the tagged body the prompt hook receives,
#: and the wrapper sentence the transcript keeps it behind), the app's
#: side-session notes, scheduled-task payloads, and the line the harness
#: writes when the owner interrupts a turn.
#: The line the harness writes into a turn when the owner interrupts it
#: ("[Request interrupted by user]", "... for tool use"): a turn that holds
#: one ended without a Stop (RS-F3-M5), and the next hook settles it
#: (`transcript.interrupted_turns`, RS-F7 part 6).
INTERRUPT_OPENING = "[Request interrupted"

HARNESS_OPENINGS: tuple[str, ...] = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<agent-message",
    "<cross-session-message",
    "<side-sessions-event",
    "<scheduled-task",
    "Another Claude session sent a message",
    INTERRUPT_OPENING,
)

#: What a probed text may not open with: a harness opening, or an
#: envelope tag the removal left in place because it never closes.
COMPOSED_OPENINGS: tuple[str, ...] = (
    tuple(f"<{tag}" for tag in ENVELOPE_TAGS) + HARNESS_OPENINGS
)

#: The ``turnOrigin`` of a row the owner submitted.
HUMAN_ORIGIN = "human"

_ENVELOPES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(
        rf"<{re.escape(tag)}\b[^>]*?(?:/>|>.*?</{re.escape(tag)}\s*>)",
        re.DOTALL | re.IGNORECASE,
    )
    for tag in ENVELOPE_TAGS
)


def probe_text(text: str, *, cut: bool = True) -> str:
    """`text` as the probe reads it: every complete envelope block
    (`ENVELOPE_TAGS`) replaced by a space, then cut at
    `PROMPT_CUT_CHARS`. With `cut` False the words are kept whole, for a
    reader that counts them rather than probes them (capture)."""
    for envelope in _ENVELOPES:
        text = envelope.sub(" ", text)
    return text[:PROMPT_CUT_CHARS] if cut else text


def owner_text(text: str, *, cut: bool = True) -> str | None:
    """The owner's words in `text` as the probe reads them (`probe_text`),
    or None when what is left is blank or opens, after leading
    whitespace, with a composed shape (`COMPOSED_OPENINGS`). A text that
    names a tag after its first word is the owner's. The cut changes
    neither test, so `cut` False answers the same question with the
    words kept whole."""
    probed = probe_text(text, cut=cut)
    if not probed.strip() or probed.lstrip().startswith(COMPOSED_OPENINGS):
        return None
    return probed


def text_blocks(content: Any) -> str | None:
    """The text blocks of a message content list, joined by newlines;
    None when the content is not a list or holds no text. Thinking,
    tool-use, tool-result and image blocks carry none."""
    if not isinstance(content, list):
        return None
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"]
    ]
    return "\n".join(parts) if parts else None


def is_submitted(row: Mapping[str, Any]) -> bool:
    """Whether `row` is a user row someone submitted: its content is a
    string, or a list none of whose blocks is a tool result. The rest of
    a turn's user rows carry the tools' results back to the model."""
    if row.get("type") != "user":
        return False
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def submitted_text(row: Mapping[str, Any]) -> str | None:
    """The text of a submitted user row (`is_submitted`): a string content
    as it is, a list's text blocks joined by newlines; None for any other
    row and for a row with no text."""
    if not is_submitted(row):
        return None
    content = row["message"]["content"]
    return content if isinstance(content, str) else text_blocks(content)


#: The attachment type of a prompt the owner typed while a turn ran and
#: the harness held for the next one.
QUEUED_COMMAND = "queued_command"


def queued_text(row: Mapping[str, Any]) -> str | None:
    """The prompt of a queued-command attachment row: a row of type
    ``attachment`` whose ``attachment`` is a `QUEUED_COMMAND` with a
    string ``prompt``. None for any other row, for an attachment flagged
    ``isMeta`` (a note the app queued, not the owner's words, the mark a
    user row carries for the same thing) and for a blank prompt. Whether
    the words are the owner's is `owner_text`'s question, as for a
    submitted row."""
    if row.get("type") != "attachment":
        return None
    attachment = row.get("attachment")
    if not isinstance(attachment, Mapping) or attachment.get("type") != QUEUED_COMMAND:
        return None
    if attachment.get("isMeta"):
        return None
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return prompt


def owner_prompt(row: Mapping[str, Any], *, cut: bool = True) -> str | None:
    """The owner's typed text of a transcript row as the probe reads it
    (`owner_text`), or None when the row is not the owner's: not a
    submitted user row, flagged ``isMeta`` or ``isCompactSummary``, or
    carrying a ``turnOrigin`` other than ``human``. `cut` as in
    `owner_text`."""
    if row.get("isMeta") or row.get("isCompactSummary"):
        return None
    origin = row.get("turnOrigin")
    if origin is not None and origin != HUMAN_ORIGIN:
        return None
    text = submitted_text(row)
    return None if text is None else owner_text(text, cut=cut)


__all__ = [
    "COMPOSED_OPENINGS",
    "ENVELOPE_TAGS",
    "HARNESS_OPENINGS",
    "HUMAN_ORIGIN",
    "PROMPT_CUT_CHARS",
    "QUEUED_COMMAND",
    "is_submitted",
    "owner_prompt",
    "owner_text",
    "probe_text",
    "queued_text",
    "submitted_text",
    "text_blocks",
]
