"""Nothing the product says tells the model to ask the user before saving.

`category='user-inference'` used to stage every write, and every surface
that described it scripted the same interruption: the write's hint said
to ask "in plain language" before confirming, the server instructions
and the long-form addendum said to ask the user first, the proposal
accept and the user-claim refusals routed through the same handshake.
That handshake is gone. A claim about the user commits like a fact and
keeps its label; `require_write_confirmation` is the one opt-in that
stages a write, and its hint names the two calls without scripting a
question.

The instruction came back through prose before, one surface at a time,
so this module reads every place a model is told what to do — the served
tool descriptions on the full surface, the `instructions` block, the
paste-in addendum, the shipped docs, and the hints the live handlers
return on each path that used to carry the ask — and fails on the
phrasings that carried it. The patterns are pinned against the removed
wording below, so a rewrite that stopped matching them would fail here
rather than pass vacuously.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_REPO = Path(__file__).resolve().parents[1]

# Each shape an ask-before-save instruction took on a shipped surface.
# Case-insensitive; worded narrowly enough that "when the user asks you
# to remember" (the skill's trigger description) stays legal.
_ASK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bask(?:ing)?\s+(?:the\s+user|them)\b",
        r"\bask(?:ing)?\s+(?:before|first)\b",
        r"\bask(?:ing)?\s+for\s+(?:their\s+)?(?:confirmation|permission|approval)",
        r"\bask(?:ing)?\s+in\s+plain\s+language",
        r"\bwant\s+me\s+to\s+(?:remember|save|store)",
        r"\bgets?\s+the\s+veto\b",
        r"\bwithout\s+asking\b",
    )
)

# The wording that shipped before the handshake was removed, one sample
# per pattern family. If a pattern stops matching its sample, every scan
# below goes quiet without anyone noticing.
_REMOVED_WORDING = (
    "User-inference category — ask the user in plain language ('want me "
    "to remember that you prefer X?') and only then call "
    "memory_write_confirm(pending_id)",
    "(server stages pending; ask the user before confirming)",
    "(server stages pending; ask before confirming)",
    "Ask the user, then memory_write_confirm or memory_write_cancel.",
    "the model should call `memory_write` with "
    '`category="user-inference"`, ask for confirmation,',
    "Misattribution sticks — user gets the veto.",
    "filed as `fact`, which commits without asking them.",
)

# Shipped prose a model or an integrator reads as instructions.
_DOCS = (
    "README.md",
    "docs/api.md",
    "docs/internals.md",
    "docs/system_prompt.md",
    "plugin/README.md",
    "plugin/skills/bettermemory/SKILL.md",
)


def _asks(text: str) -> list[str]:
    return [m.group(0) for p in _ASK_PATTERNS for m in p.finditer(text)]


def _hints(obj: Any) -> Iterator[str]:
    """Every string under a `hint` key, at any depth of a response."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "hint" and isinstance(value, str):
                yield value
            else:
                yield from _hints(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _hints(value)


def _server(root: Path, *, confirm: bool = False) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(root)),
        behavior=BehaviorConfig(
            full_tool_surface=True, require_write_confirmation=confirm
        ),
    )
    return build_server(config=cfg, store=Store(root), state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


@pytest.mark.parametrize("sample", _REMOVED_WORDING)
def test_the_patterns_catch_the_removed_wording(sample: str) -> None:
    assert _asks(sample), f"no pattern matches removed wording: {sample!r}"


def test_the_patterns_leave_the_trigger_description_alone() -> None:
    """The skill's own trigger is the user asking, not the model."""
    assert _asks('when the user asks you to "remember" something') == []


async def test_no_description_or_instruction_asks_before_saving(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    surfaces = {
        f"description:{tool.name}": tool.description or ""
        for tool in await server.list_tools()
    }
    surfaces["instructions"] = server.instructions or ""
    surfaces["SYSTEM_PROMPT_ADDENDUM"] = SYSTEM_PROMPT_ADDENDUM
    found = {name: hits for name, text in surfaces.items() if (hits := _asks(text))}
    assert found == {}


@pytest.mark.parametrize("rel", _DOCS)
def test_no_shipped_doc_asks_before_saving(rel: str) -> None:
    text = (_REPO / rel).read_text(encoding="utf-8")
    assert _asks(text) == []


async def test_no_response_hint_asks_before_saving(tmp_path: Path) -> None:
    """Drive every path whose response used to carry the ask, and read
    what each one says now."""
    responses: list[Any] = []

    plain = _server(tmp_path / "plain")
    written = await _call(
        plain,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert written["status"] == "committed"
    responses.append(written)
    refused = await _call(
        plain,
        "memory_write",
        content="Mattias prefers dark mode in every editor.",
        scopes=["learning-style"],
    )
    assert refused["status"] == "user_claim_warning"
    responses.append(refused)
    seeded = await _call(
        plain,
        "memory_write",
        content="The deploy script lives in bin/deploy.",
        scopes=["infrastructure"],
    )
    updated = await _call(
        plain,
        "memory_update",
        id=seeded["id"],
        content="Mattias prefers the deploy script in bin/.",
    )
    assert updated["status"] == "user_claim_warning"
    responses.append(updated)
    ProposalQueue(tmp_path / "plain").append(
        [
            Proposal(
                id="p1",
                body="I prefer squash merges on every repository I own.",
                source_excerpt="I prefer squash merges on every repository I own.",
                suggested_category="user-inference",
                created="2026-01-01T00:00:00+00:00",
            )
        ]
    )
    accepted = await _call(
        plain,
        "memory_proposals",
        action="accept",
        proposal_id="p1",
        scopes=["learning-style"],
    )
    assert accepted["status"] == "accepted"
    responses.append(accepted)

    confirming = _server(tmp_path / "confirming", confirm=True)
    staged = await _call(
        confirming,
        "memory_write",
        content="Prefers terse code-driven explanations.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert staged["status"] == "pending"
    responses.append(staged)
    ep = await _call(
        confirming,
        "episode_write",
        body="Iter 1 — the user reached for keyboard shortcuts again.",
        takeaway="user prefers keyboard-first navigation",
    )
    promoted = await _call(
        confirming,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert promoted["status"] == "pending"
    responses.append(promoted)

    hints = [hint for res in responses for hint in _hints(res)]
    assert len(hints) >= 4, hints
    found = {hint: hits for hint in hints if (hits := _asks(hint))}
    assert found == {}
