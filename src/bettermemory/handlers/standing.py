"""memory_admin(action="answer_standing"): the standing question for the
records that wait for it (unit RS-F7 part 1, `standing.py`).

Without `standing`, the response shows the next `standing.BATCH` records
that have no answer for their current body, newest first, each as a
review shows a stored statement, with the question and how many wait.
The records are those the caller's auto-scoped search reads
(SEARCH_AUTO_SCOPE, design 3.5): what an agent standing here already
sees, and what the session's first output counts.

With `standing` ({id: probability}), every answer is checked before any
is written: each id an active memory, each value a probability from 0 to
1. Any active record may be answered, shown or not, since memory_show
asks about any record it opens. The answers are written in one
transaction, and the reply carries the next records.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timezone
from typing import TYPE_CHECKING, Any

from .. import standing as _standing
from . import _guard
from ._shared import Context, _advance_turn, caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers

__all__ = ["answer_standing"]


def _read(deps: ToolHandlers, standing: Any) -> dict[str, float]:
    """Every answer checked, or a teaching ValueError naming the first
    that is not one."""
    if not isinstance(standing, Mapping):
        raise ValueError(
            "standing must be an object of memory id to a probability from 0 to 1"
        )
    checked: dict[str, float] = {}
    for record_id, value in standing.items():
        if not isinstance(record_id, str) or not deps.store.has_memory(record_id):
            raise ValueError(
                f"standing names {record_id!r}, which is not an active memory; "
                "nothing was written"
            )
        checked[record_id] = _standing.check_probability(
            value, f"standing[{record_id!r}]"
        )
    return checked


def _category_of(deps: ToolHandlers, record_id: str) -> str | None:
    """The record's category value (None for none)."""
    category = deps.store.load_one(record_id).category
    return category.value if category is not None else None


async def answer_standing(
    deps: ToolHandlers,
    standing: Mapping[str, Any] | None,
    *,
    said: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Body of memory_admin(action="answer_standing"). An answer that
    takes a standing record out of standing rests on the user's words in
    `said` (unit RS-F7 part 5, `_guard`); without them none of the call's
    answers is written."""
    from ..admission import Admission
    from ..membership import SEARCH_AUTO_SCOPE

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    answered: list[str] = []
    if standing:
        checked = _read(deps, standing)
        demoted = [
            record_id
            for record_id in _standing.guarded_ids(deps.store, list(checked))
            if not _standing.is_standing(
                _category_of(deps, record_id), checked[record_id]
            )
        ]
        refusal = _guard.guard(deps, "answer_standing", demoted, said)
        if refusal is not None:
            deps.recorder.record(
                "answer_standing", status="refused_standing", standing=demoted
            )
            return refusal
        with deps.store.batch():
            for record_id, probability in checked.items():
                deps.store.put_standing(
                    record_id,
                    _standing.answer_of(probability, _standing.SOURCE_ASKED),
                )
        answered = list(checked)
    _, caller = caller_anchoring(deps.store, deps.config)
    waiting = _standing.backlog(deps.store, Admission.of(SEARCH_AUTO_SCOPE, caller))
    records = [
        {
            "id": memory.id,
            "scopes": list(memory.scopes),
            "category": memory.category.value if memory.category is not None else None,
            "updated": memory.updated.astimezone(timezone.utc).date().isoformat(),
            "text": _standing.record_text(memory.body),
        }
        for memory in deps.store.get_many(waiting.ids[: _standing.BATCH])
    ]
    out: dict[str, Any] = {
        "status": "answered" if answered else "waiting",
        "answered": len(answered),
        "waiting": waiting.count,
    }
    if records:
        out["question"] = {
            "type": "probability",
            "instructions": _standing.STANDING_STATEMENT,
        }
        out["records"] = records
    out["hint"] = _standing.batch_hint(waiting.count)
    deps.recorder.record(
        "answer_standing", status=out["status"], answered=len(answered)
    )
    return out
