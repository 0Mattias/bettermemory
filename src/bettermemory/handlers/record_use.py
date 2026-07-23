"""memory_record_use MCP tool — override the auto-`applied` outcome.

Description-edit history:

- H7 (Round 2): the description led with all four outcomes side by side,
  obscuring that `applied` is the auto-fallback the server handles
  itself ~2 turns later. Reframed around the three actionable
  outcomes (`ignored`, `contradicted`, `corrected`) with a 3-row
  mental model table; `applied` demoted to a one-liner. The handler
  still accepts `applied` so explicit callers keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import is_valid_ulid
from ._shared import (
    Context,
    _NOTE_MAX_LEN,
    _USE_OUTCOMES,
    _advance_turn,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_RECORD_USE = (
    "Override the auto-`applied` outcome. Default behavior: every "
    "memory_search hit settles as `applied` at turn end (excerpts "
    "when the reply used it, `auto=true` otherwise). The common "
    "case handles itself — "
    "only call this tool when the model needs to record one of the "
    "three actionable outcomes below.\n\n"
    "| Outcome | When | What it does |\n"
    "|---|---|---|\n"
    "| `ignored` | retrieved but off-topic | annotates later hits; "
    "under `outcome_demotion`, mild 30d demotion "
    "(applied/update/verify clears) |\n"
    "| `contradicted` | stored fact disagreed AND not yet fixed | "
    "raises the unresolved-contradiction flag until a later "
    "memory_update or memory_verify clears it; demotes 2x under the "
    "same flag |\n"
    "| `corrected` | drifted and you fixed it inline (memory_update "
    "and/or memory_verify already called) | audit-only; does NOT "
    "raise the flag, never demotes |\n\n"
    "`applied` is also accepted explicitly (rarely needed — auto "
    "handles it; call only to force-commit early).\n\n"
    "Parameters:\n"
    "- `memory_ids`: list (1+).\n"
    "- `outcome`: see table above.\n"
    "- `note` (optional, ≤500 chars): free-form context.\n"
    "- `claim_excerpts` (optional): list parallel to `memory_ids` "
    "(same length, `None` slots OK) carrying the load-bearing "
    "phrase that shaped the response. ≤500 chars per excerpt. Pass "
    "`None` for 'no specific claim' — empty strings are rejected "
    "(they're ambiguous: missing claim vs. zero-length claim). "
    "Especially useful on `contradicted` / `corrected` so the "
    "audit log records WHICH claim was wrong, not just that the "
    "memory drifted. Surfaces back in "
    "`recent_negative_outcomes` on later search hits."
)


async def memory_record_use(
    deps: "ToolHandlers",
    memory_ids: list[str],
    outcome: str,
    note: str | None = None,
    claim_excerpts: list[str | None] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if not memory_ids:
        raise ValueError("memory_ids must contain at least one entry")
    if outcome not in _USE_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_USE_OUTCOMES)}")
    # ULID-format check only — we don't load the store to confirm the
    # id exists. Recording a use against a just-tombstoned memory is a
    # legitimate signal (the user contradicted it, we removed it),
    # and a load_all on every record_use call is wasteful.
    for mid in memory_ids:
        if not is_valid_ulid(mid):
            raise ValueError(f"invalid memory id: {mid!r}")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string if provided")
    if note is not None and len(note) > _NOTE_MAX_LEN:
        raise ValueError(
            f"note is {len(note)} chars — cap is {_NOTE_MAX_LEN}. "
            "The note is a short rationale for the outcome, not a "
            "place to paste prose; trim it before recording."
        )

    # `claim_excerpts` is the provenance signal (T1.1 of the 1.6 plan).
    # When provided, it's a list parallel to `memory_ids` with one
    # entry per id — the specific claim the model applied (or ignored
    # / contradicted / corrected) from that memory. `None` in a slot
    # means "no specific claim noted for this id, just the outcome".
    # Length must match exactly so the audit log can pair claims to
    # ids without ambiguity; the alternative (sparse dict keyed by id)
    # is harder for the model to assemble and clutters small calls.
    # Empty-string claims are rejected — pass `None` for "no claim".
    # Excerpts are capped at 500 chars to keep the event log small
    # and discourage dumping whole bodies (the body's already on disk;
    # the excerpt is supposed to be a quote, not a copy).
    recorded_excerpts: list[str | None] | None = None
    if claim_excerpts is not None:
        if not isinstance(claim_excerpts, list):
            raise ValueError("claim_excerpts must be a list of strings or None")
        if len(claim_excerpts) != len(memory_ids):
            raise ValueError(
                f"claim_excerpts length {len(claim_excerpts)} does not "
                f"match memory_ids length {len(memory_ids)}"
            )
        recorded_excerpts = []
        for i, excerpt in enumerate(claim_excerpts):
            if excerpt is None:
                recorded_excerpts.append(None)
                continue
            if not isinstance(excerpt, str):
                raise ValueError(
                    f"claim_excerpts[{i}] must be a string or None, "
                    f"got {type(excerpt).__name__}"
                )
            excerpt = excerpt.strip()
            if not excerpt:
                raise ValueError(
                    f"claim_excerpts[{i}] is empty — pass None for "
                    "'no specific claim' instead of an empty string"
                )
            if len(excerpt) > 500:
                raise ValueError(
                    f"claim_excerpts[{i}] is {len(excerpt)} chars — "
                    "cap is 500. Quote the load-bearing phrase, not "
                    "the whole body."
                )
            recorded_excerpts.append(excerpt)

    # The explicit outcome overrides any pending auto-commit. Pass
    # the ids through `_advance_turn` so the auto pass that would
    # otherwise have fired skips them, then purge their tokens so a
    # *future* auto-commit for the same id doesn't fire either —
    # the model has spoken, the auto-commit is settled.
    override_set = set(memory_ids)
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder, override_ids=override_set)
    for mid in memory_ids:
        state.purge_use_token(mid)

    # Build the event payload conditionally so the on-disk shape is
    # byte-stable for calls that don't use the new field — existing
    # log parsers / tests that key off the kind="use" event keep
    # working without seeing a new claim_excerpts key with a null
    # value on every old event. `attribution="model"` distinguishes
    # the explicit-by-model path from the hook-attributed
    # (`attribution="hook"`) and auto-fallback (`attribution="auto"`)
    # paths in the eval CLI's rollups; older events without the
    # field fall back to `model` when auto=false, `auto` when
    # auto=true.
    event_fields: dict[str, Any] = {
        "ids": list(memory_ids),
        "outcome": outcome,
        "note": note,
        "attribution": "model",
    }
    if recorded_excerpts is not None:
        event_fields["claim_excerpts"] = recorded_excerpts
    deps.recorder.record("use", **event_fields)

    result: dict[str, Any] = {
        "recorded": list(memory_ids),
        "outcome": outcome,
    }
    if recorded_excerpts is not None:
        result["claim_excerpts"] = recorded_excerpts
    return result


__all__ = ["DESC_MEMORY_RECORD_USE", "memory_record_use"]
