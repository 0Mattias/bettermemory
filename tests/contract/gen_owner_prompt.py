"""Fixtures: `owner_prompt` but `probe_text` (``owner_prompt.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/owner_prompt.rs``, which takes
`probe_text` as an argument until the regular-expression engine reads
`_ENVELOPES`. Here `probe_text` is replaced by the identity for the
generation, so `owner_text` and `owner_prompt` answer on their own rule.

Each case names ``f`` (``tb`` `text_blocks`, ``sub`` `is_submitted`,
``st`` `submitted_text`, ``qt`` `queued_text`, ``op`` `owner_prompt`,
``ot`` `owner_text`) and its input: ``in``, the ``json.dumps`` text of the
content or row (``ot``: the text itself, through `text_field`). The answer
``r`` is a bool for ``sub``; else null or the text through `text_field`.
"""

from __future__ import annotations

import json
from typing import Any

from bettermemory import owner_prompt as op

from ._common import jsonl, text_field


def _user(content: Any, **marks: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"content": content}, **marks}


def _queued(attachment: Any, **marks: Any) -> dict[str, Any]:
    return {"type": "attachment", "attachment": attachment, **marks}


TB: list[Any] = [
    None,
    "text",
    {"type": "text", "text": "x"},
    7,
    [],
    [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
    [{"type": "text", "text": ""}, {"type": "text", "text": "b"}],
    [{"type": "text", "text": 5}, {"type": "text"}, {"type": "text", "text": None}],
    [{"type": "thinking", "text": "t"}, {"type": "TEXT", "text": "u"}],
    ["text", 3, None, [{"type": "text", "text": "n"}]],
    [{"type": "text", "text": "\n"}, {"type": "text", "text": " "}],
    [{"type": "text", "text": "a", "type ": "x"}, {"text": "b"}],
    [{"type": "text", "text": "\ud800"}],
    [{"type": "tool_result", "text": "r"}, {"type": "text", "text": "s"}],
]

ROWS: list[Any] = [
    {},
    {"type": "user"},
    {"type": "assistant", "message": {"content": "x"}},
    {"type": "USER", "message": {"content": "x"}},
    {"type": "user", "message": "x"},
    {"type": "user", "message": ["content"]},
    {"type": "user", "message": None},
    {"type": "user", "message": {}},
    _user("hello"),
    _user(""),
    _user("   "),
    _user(7),
    _user({"type": "text", "text": "x"}),
    _user(None),
    _user([]),
    _user(["x", 1]),
    _user([{"type": "text", "text": "a"}, {"type": "image"}]),
    _user([{"type": "text", "text": "a"}, {"type": "tool_result", "content": "r"}]),
    _user([{"type": "tool_result"}]),
    _user(["tool_result", {"type": "Tool_result"}, {"type": "text", "text": "q"}]),
    _user([{"type": "text", "text": ""}]),
    _user("<system-reminder>x</system-reminder>"),
    _user("  <command-name>/x</command-name>"),
    _user("[Request interrupted by user]"),
    _user("ask <task-notification>"),
    _user("hi", isMeta=True),
    _user("hi", isMeta=0),
    _user("hi", isMeta=""),
    _user("hi", isMeta=[]),
    _user("hi", isMeta={}),
    _user("hi", isMeta="no"),
    _user("hi", isMeta=0.0),
    _user("hi", isMeta=None),
    _user("hi", isCompactSummary=True),
    _user("hi", isCompactSummary=False),
    _user("hi", isSidechain=True),
    _user("hi", isSidechain=False),
    _user("hi", agentId="a1"),
    _user("hi", agentId=""),
    _user("hi", agentId=0),
    _user("hi", turnOrigin="human"),
    _user("hi", turnOrigin=None),
    _user("hi", turnOrigin="peer"),
    _user("hi", turnOrigin="Human"),
    _user("hi", turnOrigin=""),
    _user("hi", turnOrigin=0),
    _user("hi", turnOrigin=False),
    _user("hi", turnOrigin=["human"]),
    _user("hi", turnOrigin={"human": 1}),
    _user(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        turnOrigin="human",
    ),
    _user("\ud83d"),
    _queued({"type": "queued_command", "prompt": "next"}),
    _queued({"type": "queued_command", "prompt": "  "}),
    _queued({"type": "queued_command", "prompt": "　\x85"}),
    _queued({"type": "queued_command", "prompt": ""}),
    _queued({"type": "queued_command", "prompt": 5}),
    _queued({"type": "queued_command"}),
    _queued({"type": "queued_command", "prompt": " p ", "isMeta": True}),
    _queued({"type": "queued_command", "prompt": "p", "isMeta": 0}),
    _queued({"type": "queued_command", "prompt": "p", "isMeta": ""}),
    _queued({"type": "queued_command", "prompt": "p", "isMeta": []}),
    _queued({"type": "queued_command", "prompt": "p", "isMeta": [0]}),
    _queued({"type": "queued_command", "prompt": "p"}, isSidechain=True),
    _queued({"type": "queued_command", "prompt": "p"}, agentId="x"),
    _queued({"type": "queued_command", "prompt": "p"}, agentId=""),
    _queued({"type": "queued_command", "prompt": "p"}, isMeta=True),
    _queued({"type": "Queued_command", "prompt": "p"}),
    _queued({"type": "other", "prompt": "p"}),
    _queued("queued_command"),
    _queued(["queued_command"]),
    _queued(None),
    {"type": "attachment"},
    {"type": "user", "attachment": {"type": "queued_command", "prompt": "p"}},
    _queued({"type": "queued_command", "prompt": "<task-notification>x"}),
]

TEXTS: list[str] = [
    "",
    " ",
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f\x85\xa0   　",
    "​",
    "hello",
    "  hello  ",
    "hello <system-reminder>",
    "<system-reminder>x</system-reminder>",
    "<system-reminderx",
    "<System-reminder>x",
    " \n<pasted_content>",
    "<artifact-view-context",
    "<artifact-view-contex",
    "　<task-notification>",
    "<task-notification",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<agent-messages",
    "<cross-session-message from",
    "<cross-session-message>",
    "<side-sessions-event",
    "<scheduled-task>",
    "<scheduled-task id=1>",
    "Another Claude session sent a message: hi",
    "another Claude session sent a message",
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
    "[Request interrupte",
    "\x85[Request interrupted",
    "​<command-name>",
    "x" * 2_500,
    "\ud800 tail",
]


def _row(f: str, value: Any) -> dict[str, Any]:
    text = json.dumps(value)
    if f == "tb":
        r: Any = op.text_blocks(value)
    elif f == "sub":
        return {"f": f, "in": text, "r": op.is_submitted(value)}
    elif f == "st":
        r = op.submitted_text(value)
    elif f == "qt":
        r = op.queued_text(value)
    else:
        r = op.owner_prompt(value)
    return {"f": f, "in": text, **({"r": None} if r is None else text_field("r", r))}


def generate() -> dict[str, bytes]:
    saved = op.probe_text
    op.probe_text = lambda text, *, cut=True: text
    try:
        lines: list[Any] = [_row("tb", c) for c in TB]
        for f in ("sub", "st", "qt", "op"):
            lines.extend(_row(f, row) for row in ROWS)
        for t in TEXTS:
            r = op.owner_text(t)
            lines.append(
                {
                    "f": "ot",
                    **text_field("in", t),
                    **({"r": None} if r is None else text_field("r", r)),
                }
            )
    finally:
        op.probe_text = saved
    header = {"fixture": "owner_prompt", "cases": len(lines)}
    return {"owner_prompt.jsonl": jsonl([header, *lines])}
