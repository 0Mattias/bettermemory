"""The prompt recall's steps that read the log, probe and record its rows.

`recall.prompt_recall` resolves its caller into a `_PromptTurn` (the
store, the configuration, the owner's words, the caller's resolution and
the admission the switch names), then runs the steps here in order:
`_prompt_window` reads the segment's cuts and the log window,
`_settle_before_probe` settles the session's interrupted turns (best
effort), `_prompt_probe` probes the owner's words, `_record_hold` writes
the `recall_held` row when nothing is delivered, and `_record_delivery`
writes the `prompt_recall` row of a delivery. The steps that build the
hook's result, gate the hits on what the segment has seen and render the
block stay in `recall`, beside the names they read there.

This module lives apart from `recall.py` to keep each module under the
package's 2,000-line bound (ENG-97); from the family it imports
`recall_admission`, `recall_settle` and `recall_state`, never `recall`,
and `recall` imports it.

Where to patch. The probe is read through `recall_settle` at call time
(`recall_settle._probe_message`), where `hook`'s Stop audit reads it too,
so a patch on `recall_settle._probe_message` reaches the prompt hook and
the Stop audit alike. `settle_interrupted` and `previous_audit_at`
(`recall_settle`), `Recorder` (`events`), `_delivery_membership` and
`_rc3_admitted` (`recall_admission`), and `RULE` and `STATE_IDLE_SECONDS`
(`recall_state`) are bound here at import from those modules: a patch
that must reach the prompt hook's reads of them targets this module, and
a patch on `recall`'s re-export of one reaches `recall`'s own readers and
`hook`'s, not these.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .admission import Admission
from .audit import (
    REAUDIT_DEDUP_WINDOW_SECONDS,
    MissHit,
    MissReport,
    prompt_recall_fields,
    recall_held_fields,
)
from .config import Config
from .events import Recorder
from .in_force import InForce
from .membership import POOL_PREFILTER, PROMPT_RECALL, Resolution
from .models import Memory
from .origin import Origin
from .recall_admission import _delivery_membership, _rc3_admitted
from . import recall_settle
from .recall_settle import previous_audit_at, settle_interrupted
from .recall_state import (
    RULE,
    STATE_IDLE_SECONDS,
    RecallState,
    SegmentView,
)
from .sessions import _disabled_scopes_from_events, bridged_session
from .store import Store, TrustRow

# The recall surface's log, the one `recall` writes to.
log = logging.getLogger("bettermemory.recall")

#: A chosen hit: the hit, the record that takes its slot, that record's
#: trust row, and its in-force entry when the hit is history or extended.
_Chosen = tuple[MissHit, Memory, TrustRow, InForce | None]


@dataclass(frozen=True)
class _PromptTurn:
    """A prompt recall past its caller's gates: what every later step reads."""

    store: Store
    cfg: Config
    #: The `prompt_recall` switch: "project" or "everywhere".
    mode: str
    #: The owner's words as the probe reads them (`owner_prompt.owner_text`).
    query: str
    resolution: Resolution | None
    #: The admission the switch names: PROMPT_RECALL or SEARCH_UNSCOPED.
    surface: str
    admission: Admission
    #: ``{"remint": True}`` when the checkout's token is the client's to
    #: re-mint, else None; every answer carries it.
    remint: Mapping[str, Any] | None
    now: datetime
    #: (harness, session id).
    key: tuple[str, str]
    caller: Origin
    prompt_id: str | None
    transcript_path: str | None


def _prompt_window(
    turn: _PromptTurn, view: SegmentView
) -> tuple[datetime, list[dict[str, Any]], str | None, set[str]]:
    """Take the turn and segment; return (own cut, log, server session, scopes off)."""
    now, key, caller = turn.now, turn.key, turn.caller
    recent_cut = now - timedelta(seconds=REAUDIT_DEDUP_WINDOW_SECONDS)
    # A first segment began with the session; a later one when its
    # compact or clear opened it (`_seen_in_log` says why the cuts differ).
    own_since = (
        view.started_at
        if view.segment > 0
        else now - timedelta(seconds=STATE_IDLE_SECONDS)
    )
    recent = list(turn.store.events_since(recent_cut))
    # The conversation's in-process rows: the harness join first (tool
    # events stamped with this session id), the worktree bridge only
    # when nothing is stamped with it.
    server_session = bridged_session(
        recent, harness_session=key[1], worktree_root=caller.worktree_root
    )
    excluded_scopes = _disabled_scopes_from_events(
        recent, worktree_root=caller.worktree_root, server_session=server_session
    )
    return own_since, recent, server_session, excluded_scopes


def _settle_before_probe(
    turn: _PromptTurn,
    recent: list[dict[str, Any]],
    server_session: str | None,
    excluded_scopes: set[str],
) -> None:
    """Take the turn and its log window; settle the session's interrupted turns."""
    # Best effort, as every settlement is: a past turn's audit never costs
    # this prompt its recall.
    try:
        settle_interrupted(
            store=turn.store,
            cfg=turn.cfg,
            session_id=turn.key[1],
            transcript_path=turn.transcript_path,
            # An interrupted turn's row is a Stop's row: its probe draws
            # under the Stop's prefilter in both modes, so the verdict
            # does not depend on which hook settled the turn.
            admission=turn.admission.on(POOL_PREFILTER),
            excluded_scopes=excluded_scopes,
            recent=recent,
            server_session=server_session,
            worktree_root=turn.caller.worktree_root,
            now=turn.now,
            previous_audit=previous_audit_at(turn.store, turn.key[1], now=turn.now),
            triggered_from="prompt_hook",
            exclude_prompt_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - logged, never raised at the hook
        log.warning(
            "interrupted turns not settled at the prompt: %s: %s",
            exc.__class__.__name__,
            exc,
        )


def _prompt_probe(
    turn: _PromptTurn,
    recent: list[dict[str, Any]],
    server_session: str | None,
    excluded_scopes: set[str],
) -> MissReport:
    """Take the turn and its log window; return the probe's report on its words."""
    return recall_settle._probe_message(
        cfg=turn.cfg,
        store=turn.store,
        user_message=turn.query,
        session_id=turn.key[1],
        server_session=server_session,
        # The pool is drawn under the hooks' prefilter for "project" and
        # under the unscoped search's admission for "everywhere", so a
        # record the delivery may not admit never takes a rank.
        admission=turn.admission.on(POOL_PREFILTER)
        if turn.surface == PROMPT_RECALL
        else turn.admission,
        excluded_scopes=excluded_scopes,
        recent=recent,
        worktree_root=turn.caller.worktree_root,
        now=turn.now,
    )


def _record_hold(
    turn: _PromptTurn,
    report: MissReport,
    segment: int,
    cause: str,
    entries: Sequence[Mapping[str, str]],
) -> None:
    """Take the turn, its report and why nothing was delivered; record `recall_held`."""
    # Nothing delivered: the `recall_held` row says why (RS-F7 part 6),
    # a measurement row (`events.RowClass`), built only when it is
    # recorded.
    recorder = Recorder(
        store=turn.store,
        session_id=turn.key[1],
        enabled=turn.cfg.telemetry.enabled,
        worktree_root=turn.caller.worktree_root,
    )
    if recorder.records("recall_held"):
        fields = recall_held_fields(
            report,
            session_id=turn.key[1],
            probe_mode=turn.cfg.behavior.search_mode or "hybrid",
            cause=cause,
            held=entries,
            rule=RULE,
            segment=segment,
            prompt_id=turn.prompt_id,
            transcript_path=turn.transcript_path,
            harness=turn.key[0],
        )
        fields["admission"] = turn.surface
        fields.update(_delivery_membership(turn.resolution, turn.admission))
        recorder.record("recall_held", **fields)


def _record_delivery(
    turn: _PromptTurn,
    report: MissReport,
    state: RecallState,
    *,
    block: str,
    shown_ids: list[str],
    event_id: str,
    segment: int,
    chosen: Sequence[_Chosen],
    suppressed: list[dict[str, str]],
    rc3_candidates: list[Memory],
) -> dict[str, Any]:
    """Take a claimed delivery; record its `prompt_recall` row, return its fields."""
    recorder = Recorder(
        store=turn.store,
        session_id=turn.key[1],
        enabled=turn.cfg.telemetry.enabled,
        worktree_root=turn.caller.worktree_root,
    )
    fields = prompt_recall_fields(
        report,
        session_id=turn.key[1],
        probe_mode=turn.cfg.behavior.search_mode or "hybrid",
        injected_chars=len(block),
        delivered_reason=turn.mode,
        shown_ids=shown_ids,
        suppressed=suppressed,
        rule=RULE,
        segment=segment,
        prompt_id=turn.prompt_id,
        transcript_path=turn.transcript_path,
        harness=turn.key[0],
    )
    fields["event_id"] = event_id
    # The admission the delivery was decided under (decision B21).
    fields["admission"] = turn.surface
    fields.update(_delivery_membership(turn.resolution, turn.admission))
    # A pointer the render or the cooldown dropped is dropped whatever the
    # admission, so the column leaves it out as well.
    dropped = {hit.id for hit, _memory, _row, _forced in chosen} - set(shown_ids)
    fields["rc3_admitted"] = [
        memory_id
        for memory_id in _rc3_admitted(
            rc3_candidates, turn.caller, project_only=turn.surface == PROMPT_RECALL
        )
        if memory_id not in dropped
    ]
    try:
        recorder.record("prompt_recall", **fields)
    except BaseException:
        # The delivery's row could not land, so nothing is delivered and
        # the claim goes with the session's held state (`RecallState.
        # forget`), read back from the log, which never saw it.
        state.forget(turn.key)
        raise
    return fields
