"""Where a proposals-accept transient override lands, and where it does not.

`accept_proposal` records `markers_acknowledged` on its accept event, the
same field `memory_write` records for the same escape hatch. The two do
NOT have the same reach: `health._StatsAccumulator` dispatches on event
KIND and only its `write` handler reads marker fields, so the accept
event's kind (`memory_proposals`) keeps that override as log evidence
without it reaching `MarkerStats`.

That is deliberate rather than an oversight, and this file is where the
reasoning is checkable instead of merely asserted in a comment. On the
write path the block-then-acknowledge loop logs a FIRE and then an
OVERRIDE, which is why `MarkerStats.override_rate` documents 0.500 as the
figure a fully rubber-stamped marker scores. The proposals surface
records nothing on a refusal (the proposal stays queued for the reviewer
instead), so it has overrides with no fires — counting them into the same
rows would push shared markers toward 1.000 and misread as rubber-
stamping. The test below therefore pins the ACCOUNTING rule, not today's
omission: a `MarkerStats` row fed by this surface has to arrive with its
fires, or not arrive.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers.proposals import accept_proposal
from bettermemory.health import report_for_directory
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


# One transient marker ("currently"), one body, used on both surfaces so
# the two rollups below are comparing the same row.
_TRANSIENT_BODY = "We are currently running the API on a single box."
_MARKER = "currently"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    return build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _queue_transient(root: Path) -> None:
    ProposalQueue(root).append(
        [
            Proposal(
                id="p1",
                body=_TRANSIENT_BODY,
                source_excerpt=_TRANSIENT_BODY,
                suggested_category="fact",
                created="2026-07-01T00:00:00Z",
            )
        ]
    )


def _accept(root: Path, **kwargs: Any) -> dict[str, Any]:
    """Call the accept core the way both entry points do."""
    return accept_proposal(
        store=Store(root),
        config=Config(storage=StorageConfig(directory=str(root))),
        recorder=Recorder(root=root, session_id="sess_marker_accounting"),
        proposal_id="p1",
        scopes=["tools"],
        **kwargs,
    )


def _marker_row(root: Path) -> Any:
    rows = {m.marker: m for m in report_for_directory(root).marker_stats}
    return rows.get(_MARKER)


async def test_write_path_block_then_acknowledge_scores_the_documented_half(
    server: Any, memory_dir: Path
) -> None:
    """The reference the proposals surface is measured against.

    `MarkerStats.override_rate` tells its reader to calibrate against
    0.500, and this is why: one refusal logs the fire, the retry with
    `acknowledge_transient` logs the override, and both land under kind
    `write` where the accumulator reads them.
    """
    refused = await _call(
        server, "memory_write", content=_TRANSIENT_BODY, scopes=["tools"]
    )
    assert refused["status"] == "transient_warning"
    committed = await _call(
        server,
        "memory_write",
        content=_TRANSIENT_BODY,
        scopes=["tools"],
        acknowledge_transient=True,
    )
    assert committed["status"] == "committed"

    row = _marker_row(memory_dir)
    assert row is not None, "the write path stopped feeding MarkerStats"
    assert (row.fire_count, row.override_count) == (1, 1)
    assert row.override_rate == 0.5


def test_accept_override_is_log_evidence_and_never_a_fireless_stat_row(
    memory_dir: Path,
) -> None:
    """The same block-then-acknowledge loop, through the review surface.

    Two claims, and the second is the durable one. The override IS in the
    log (that is what makes a too-loose marker greppable from here at
    all), and whatever `MarkerStats` says about this marker must not be
    built from overrides alone: a row with zero fires reports a rate the
    metric's own scale cannot mean. Wiring the `memory_proposals` kind
    into `health._StatsAccumulator._HANDLERS` is fine — it just has to
    come with an event carrying the refusal's `markers`, which is the
    half this surface does not record today.
    """
    _queue_transient(memory_dir)

    refused = _accept(memory_dir)
    assert refused["status"] == "transient_warning"
    accepted = _accept(memory_dir, acknowledge_transient=True)
    assert accepted["status"] == "accepted"
    assert accepted["markers_acknowledged"] == [_MARKER]

    accept_events = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert [e.get("markers_acknowledged") for e in accept_events] == [[_MARKER]]

    row = _marker_row(memory_dir)
    if row is not None:
        assert row.fire_count > 0, (
            "a MarkerStats row for `currently` was built from the "
            "proposals surface's overrides with no fire behind it. The "
            "refusal records no `markers` event, so an override-only row "
            "scores 1.000 where the identical loop through memory_write "
            "scores 0.500 — record the refusal's markers first, then "
            "dispatch the kind."
        )
