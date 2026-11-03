"""What a hook endpoint answers: `HookResult`, one recall surface's
answer to a hook, which the session brief and the prompt recall return
and the daemon serves as the endpoint's JSON (`to_response`).

It lives apart from `recall.py` to keep that module under the package's
2,000-line bound (ENG-96). Of the recall family it imports
`recall_state` alone, for `RULE`, and never `recall`; `recall` imports
it and re-exports `HookResult`, so `recall.HookResult` keeps its path.
Beside it, `control_tokens`, which imports the standard library alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .control_tokens import defang_control_tokens
from .recall_state import RULE


# ---------------------------------------------------------------------------
# What a hook endpoint answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookResult:
    """One recall surface's answer to a hook: the text the harness
    injects (`stdout`, empty when nothing is shown) and what the adapters
    wrap beside it: which surface spoke, the ids it showed, the text's
    length, the rule and the id of the event that recorded the show (None
    when nothing was recorded; a resume's brief that omitted every
    candidate records its row and carries the id with nothing shown).

    `stdout` is kept with each chat-template control token written as its
    bare name (`control_tokens.defang_control_tokens`, ENG-189), whichever
    surface built it: the harness puts it in the model's context, and its
    lines can name what the store keeps as given (a checkout's path in a
    membership question or a resolution's line, a project's name) and a
    pointer's summary or snippet of a body written before 9.0.0rc14. A
    `chars` that was the text's length is the named text's."""

    stdout: str
    surface: str
    ids: tuple[str, ...] = ()
    chars: int = 0
    rule: str = RULE
    event_id: str | None = None
    membership: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        named = defang_control_tokens(self.stdout)
        if named is self.stdout:
            return
        if self.chars == len(self.stdout):
            object.__setattr__(self, "chars", len(named))
        object.__setattr__(self, "stdout", named)

    @classmethod
    def empty(cls, surface: str) -> HookResult:
        return cls(stdout="", surface=surface)

    def to_response(self) -> dict[str, Any]:
        """The endpoint's JSON fields, `stdout` first as it always was;
        ``membership`` only where the caller's resolution asks the hook
        client for something (`location.resolve_hook`: the next round of
        the witness exchange, or a token to re-mint in its namespace)."""
        out: dict[str, Any] = {
            "stdout": self.stdout,
            "surface": self.surface,
            "ids": list(self.ids),
            "chars": self.chars,
            "rule": self.rule,
            "event_id": self.event_id,
        }
        if self.membership:
            out["membership"] = dict(self.membership)
        return out
