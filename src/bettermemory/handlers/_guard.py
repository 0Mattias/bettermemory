"""The guard on a change to a standing memory (unit RS-F7 part 5).

A standing memory (`standing.guarded_ids`: standing and in force, in
any project) is the user's rule. Through the tools it is replaced,
edited, removed, demoted or taken out of force only with ``said``, the
user's words for the change, and every door reads them here: memory_write
replacing it (`write._reviewed_write`), memory_update, memory_remove,
memory_admin answer_standing and restore.

A standing memory directs work once stored, so its taking any memory out
of force rests on the words too (rule S, ENG-113): a standing write that
replaces one (`write._reviewed_write`, `capture_relations`), an update
that makes a standing statement and replaces one (`standing_review`), and
a ``supersedes`` link memory_update adds from a record standing after the
edit (`check`'s `rule_s`). Each such row names those memories too.

The daemon checks the words the one way it checks a join's
(`handlers.questions`): the quote is in a prompt the prompt hook saw in
the caller's checkout (`recall.WORDS`). Where no hook saw the checkout
the words are taken as given and the row says so, as a join's are. The
meaning of the words is not read: the agent chose the quote, and the
quote is on the log. Every change the words carried is one
``standing_guard`` row with the door, the memories and the quote, so a
wrong one can be found and reverted. A door whose call can still stop
before its change (memory_update, whose standing question and roster
come after the guard) reads the words with `check` and writes the row
once the change commits (`Held`).

What the guard holds out is an agent that forgets or overreaches through
the tools. An agent that runs as the user with a shell holds the store's
key and is the user as far as the store can tell; separating the two is
an OS deployment choice, not a tool check (SECURITY.md).

Words carrying a chat-template control token are taken, not refused
(ENG-189): they are the user's, and a quote is checked word for word, so
no rewrite of them could pass. The check reads them as typed or with
each token named (`recall.WordsSeen.holds`), and the row keeps each
token by its bare name: the event writer writes a row's free-text
fields that way (`events.NAMED_EVENT_FIELDS`), the ``standing_guard``
row's quote and the user's words in a write row's review answers among
them, as a join's row keeps its quote (`control_tokens.folded_quote`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import standing as _standing
from ._shared import caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..location import Caller

__all__ = ["Held", "Words", "check", "guard", "read_words", "record"]

_HINT = (
    "Not changed: it would replace, remove, demote, re-scope or edit a "
    "standing memory, quoted in `standing`, which directs future work until "
    "the user changes it. Leave it as it is; or, when the user changed it in "
    "this conversation, re-send this same call with said=<the user's words "
    "as they typed them>."
)

_RULE_S_HINT = (
    "Not changed: the memory states a rule, and the supersedes links this "
    "call adds would take each memory under `standing` out of force. A "
    "rule replaces stored memories only on the user's words: if the user "
    "gave it in this conversation, re-send this same call with "
    "said=<the user's words as they typed them>; otherwise leave those "
    "links out."
)

_UNSEEN = (
    "{name} is not in any prompt the hook saw in this checkout (the last "
    "twenty, word for word, whitespace and case folded, eight characters or "
    "more): quote the user's words as they typed them."
)


@dataclass(frozen=True)
class Words:
    """The user's words a change rests on: the `quote`, and whether the
    prompt hook `checked` it (False where no hook saw the caller's
    checkout, so the quote is taken as given)."""

    quote: str
    checked: bool


def read_words(
    deps: ToolHandlers, caller: Caller, said: str | None, *, name: str = "said"
) -> Words | None:
    """`said` read: None when no words were given; a teaching ValueError
    when the quote is empty, or when the hook saw prompts in the caller's
    checkout and none holds it; else the words, checked or taken as
    given."""
    from ..recall import WORDS

    if said is None:
        return None
    quote = " ".join(said.split())
    if not quote:
        raise ValueError(
            f"{name} quotes the user's words that make this change; it cannot be empty"
        )
    checkout = caller.resolution.checkout
    if checkout is None or not WORDS.seen(deps.store.path, checkout):
        return Words(quote=quote, checked=False)
    if WORDS.holds(deps.store.path, checkout, quote):
        return Words(quote=quote, checked=True)
    raise ValueError(_UNSEEN.format(name=name))


def record(deps: ToolHandlers, door: str, records: list[str], words: Words) -> None:
    """The ``standing_guard`` row: the change to `records` through `door`
    that `words` carried. An audit row, not telemetry: the recorder writes
    it with ``[telemetry] enabled = false`` too (`events.is_audit_row`),
    since the trail is what lets a wrong change be found and reverted."""
    deps.recorder.record(
        "standing_guard",
        door=door,
        standing=list(records),
        said=words.quote,
        said_checked=words.checked,
    )


@dataclass(frozen=True)
class Held:
    """A change `check` let through on the user's words, whose
    ``standing_guard`` row is written once the change commits (`write`)."""

    door: str
    records: tuple[str, ...]
    words: Words

    def write(self, deps: ToolHandlers) -> None:
        """The row for the committed change."""
        record(deps, self.door, list(self.records), self.words)


def check(
    deps: ToolHandlers,
    door: str,
    records: list[str],
    said: str | None,
    *,
    rule_s: list[str] | None = None,
) -> tuple[dict[str, Any] | None, Held | None]:
    """`guard` without its row: (None, None) when the change to `records`
    may go ahead with no words (none of them is a standing memory, no
    memory is in `rule_s`, or the write review is off, under which a
    replacing write is not guarded either); (None, held) when `said` reads
    (`read_words`), the row to write once the change commits; else (the
    refused_standing response, quoting the memories, None). `rule_s`
    names memories a standing record takes out of force (rule S), guarded
    whether they are standing or not."""
    from ..admission import Admission
    from ..membership import REVIEW_STRATA

    own = _standing.guarded_ids(deps.store, records) if records else []
    extra = [i for i in dict.fromkeys(rule_s or []) if i not in own]
    guarded = [*own, *extra]
    if not guarded or not _standing.asking():
        return None, None
    _, caller = caller_anchoring(deps.store, deps.config)
    words = read_words(deps, caller, said)
    if words is not None:
        return None, Held(door=door, records=tuple(guarded), words=words)
    rows = _standing.refusal_rows(
        deps.store, guarded, Admission.of(REVIEW_STRATA, caller)
    )
    hints = ([_HINT] if own else []) + ([_RULE_S_HINT] if extra else [])
    response: dict[str, Any] = {
        "status": "refused_standing",
        "standing": rows,
        "hint": " ".join(hints),
    }
    if extra:
        response["standing_write"] = True
    return response, None


def guard(
    deps: ToolHandlers, door: str, records: list[str], said: str | None
) -> dict[str, Any] | None:
    """None when the change to `records` may go ahead (`check`), the row
    written when it rests on the user's words; else the refused_standing
    response. For a door whose change follows the guard at once."""
    refusal, held = check(deps, door, records, said)
    if held is not None:
        held.write(deps)
    return refusal
