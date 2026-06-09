"""memory_curate MCP tool — execute the curation memory_health diagnoses.

memory_health returns a recommendations digest, but its `action` fields
are prose pointing the model at the `bettermemory consolidate` CLI — which
an in-session model can't run. The read side (diagnose) and the write side
(consolidate) lived on opposite sides of the MCP boundary. This tool closes
that gap by wrapping the same hardened `consolidate()` engine the Stop-hook
`run_auto_consolidate` path uses, exposed as a model-callable tool with a
dry-run-by-default safety contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..consolidate import consolidate

from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_CURATE = (
    "Execute store curation — the actions memory_health only describes "
    "(its recommendations point at a CLI an in-session model can't run). "
    "Runs the consolidate engine over the whole store. dry_run defaults "
    "to True: the call returns the full PREVIEW with the store UNTOUCHED "
    "— dedup_candidates, demotion_candidates, and the suggest-only "
    "cold_scope_suggestions / scope_typo_pairs. Inspect it, then re-call "
    "with dry_run=False to COMMIT the two reversible actions: tombstone "
    "near-duplicate memories (undo via memory_restore) and demote "
    "dead-weight facts — created before the window, retrieved at least "
    "once, never applied — to the ambient category (undo via "
    "memory_update). Cold-scope and scope-typo findings are NEVER applied "
    "automatically; act on them via memory_rename_scope. Dedup uses "
    "Jaccard overlap (no embedding model is loaded). `window_days` "
    "(default 30) bounds the dead-weight age. Returns the consolidate "
    "report dict plus `dry_run`; on apply, `applied=True`, `actions_taken` "
    "(each .kind is 'tombstoned' or 'demoted_to_ambient') and any "
    "`failures`. Nothing is hard-deleted; an apply records one `curate` "
    "event for the tool-usage rollup."
)


async def memory_curate(
    deps: "ToolHandlers",
    dry_run: bool = True,
    window_days: int = 30,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if window_days < 1:
        raise ValueError("window_days must be a positive integer")

    report = consolidate(
        deps.store,
        window_days=window_days,
        apply=not dry_run,
        session_id=state.session_id,
    )
    result = report.to_dict()
    result["dry_run"] = dry_run

    # Only the apply path mutates, so only it records a `curate` event.
    # The dry-run preview leaves the store — and the event log — untouched
    # so a model can inspect the diff with zero side effects. The Store
    # layer records no events of its own, so this single rollup is the
    # complete telemetry for an in-session curation (mirrors the
    # `auto_consolidate` rollup the Stop-hook path emits).
    if not dry_run:
        tombstoned = sum(1 for a in report.actions_taken if a.kind == "tombstoned")
        demoted = sum(1 for a in report.actions_taken if a.kind == "demoted_to_ambient")
        deps.recorder.record(
            "curate",
            tombstoned=tombstoned,
            demoted=demoted,
            failures=len(report.failures),
            dedup_method=report.dedup_method,
            window_days=window_days,
        )
    return result


__all__ = ["DESC_MEMORY_CURATE", "memory_curate"]
