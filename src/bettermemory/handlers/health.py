"""The health report behind `memory_admin(action="health")`.

The tool's description is `DESC_MEMORY_ADMIN`'s `health` clause; the
fields are documented in docs/api.md, where
`tests/test_health_description.py` holds the list to the report's keys.
The standalone `memory_health` description went with the nine-tool
surface in 9.0 and is not kept here.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..health import report_for_store
from ._shared import Context, _advance_turn, caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..store import Store


async def memory_health(
    deps: ToolHandlers,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    min_applied: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    from .. import _handlers as _h
    from ..admission import Admission
    from ..membership import LINKS

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # The caller resolved once, as `memory_show` resolves it (the tool
    # runs inside a session, as most handlers that resolve it do). Its
    # LINKS admission decides whose commit drift `commit_drift_debt`
    # counts, as it decides it for `memory_show`, and the count is taken
    # in the caller's checkout as `memory_show` takes it; its anchoring
    # reads `cross_repo_drift`'s homed groups where design 3.6 resolves
    # their cited commit. `bettermemory health` resolves no caller: it
    # writes nothing (`cli.health_cmd._cli_health`).
    anchoring, caller = caller_anchoring(deps.store, deps.config)
    # Falling through to the configured default lets the tool stay
    # ergonomic for the common case (don't pass anything, get the
    # tuned threshold) while still allowing a per-call override
    # ("show me everything that's been applied at least once on this
    # young store").
    threshold = (
        int(min_applied)
        if min_applied is not None
        else deps.config.behavior.heavily_used_min_applied
    )
    report = report_for_store(
        deps.store,
        window_days=int(window_days),
        heavily_used_top_k=int(heavily_used_top_k),
        heavily_used_min_applied=threshold,
        verification_stale_days=deps.config.behavior.verification_stale_days,
        cold_endorsement_ratio_threshold=(
            deps.config.behavior.cold_endorsement_ratio_threshold
        ),
        # Pass caller_origin so the cwd-aware `commit_drift_debt`
        # rollup populates when the server is running inside a repo
        # whose memories live in this store.
        caller_origin=_h.capture_origin(),
        anchoring=anchoring,
        admission=Admission.of(LINKS, caller),
    )
    out = report.to_dict()
    out["membership"] = _membership(deps.store)
    return out


def _membership(store: Store) -> dict[str, Any] | None:
    """The membership section of design 8.10 (`Membership.health`), None
    for a store an earlier build left without membership tables."""
    from ..cli.membership_cmd import health_dict, health_model

    try:
        return health_dict(health_model(store))
    except (sqlite3.Error, RuntimeError):
        return None


__all__ = ["memory_health"]
