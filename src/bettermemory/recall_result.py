"""What a hook endpoint answers: `HookResult`, one recall surface's
answer to a hook, which the session brief and the prompt recall return
and the daemon serves as the endpoint's JSON (`to_response`).

It lives apart from `recall.py` to keep that module under the package's
2,000-line bound (ENG-96). Of the recall family it imports
`recall_state` alone, for `RULE`, and never `recall`; `recall` imports
it and re-exports `HookResult`, so `recall.HookResult` keeps its path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    candidate records its row and carries the id with nothing shown)."""

    stdout: str
    surface: str
    ids: tuple[str, ...] = ()
    chars: int = 0
    rule: str = RULE
    event_id: str | None = None
    membership: Mapping[str, Any] | None = None

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
