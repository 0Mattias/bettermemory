"""Health's override rate counts both sides on one path (ENG-108).

`MarkerStats.override_rate` divides the overrides (a commit carrying
`markers_acknowledged`) by the fires plus the overrides (a
`transient_warning` refusal carrying `markers`). The overrides were audit
rows, recorded with telemetry off since 9.0.0rc12, and the fires were
telemetry: with `[telemetry] enabled = false` every fire was dropped and
the rate read 1.0 for a marker blocked once and overridden once, whose
true reading is 0.5; with telemetry on, a fire that met another writer's
lock was dropped and the rate read high. Every refusal an escape hatch
passes is now an audit row (`events.OVERRIDABLE_REFUSALS`), and health
counts both sides off the state class (`events.row_class`).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import events, store_events
from bettermemory.config import (
    ACKNOWLEDGE_NAMES,
    Config,
    StorageConfig,
    TelemetryConfig,
)
from bettermemory.events import Recorder
from bettermemory.health import compute_health, report_for_store
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool

_TRANSIENT = "The build currently runs on the old runner pool."
_AWS = "".join(("AKIA", "IOSFODNN7EXAMPLE"))
_CLEAN = "The deploy pipeline pins its formatter version in CI."
_ALPHA = "The alpha service keeps its feature flags in a TOML file."
_CROSS = "The beta release reuses the alpha flag loader unchanged."


class World:
    def __init__(self, root: Path, *, telemetry: bool) -> None:
        self.store = Store(root)
        self.server = build_server(
            config=Config(
                storage=StorageConfig(directory=str(root)),
                telemetry=TelemetryConfig(enabled=telemetry),
            ),
            store=self.store,
            state=(state := SessionState()),
            recorder=Recorder(
                store=self.store, session_id=state.session_id, enabled=telemetry
            ),
        )

    async def write(self, content: str, scopes: list[str], **flags: Any) -> Any:
        return await call_tool(
            self.server, "memory_write", {"content": content, "scopes": scopes, **flags}
        )

    def marker(self, name: str) -> dict[str, Any]:
        report = report_for_store(self.store, window_days=0)
        return {m.marker: m.to_dict() for m in report.marker_stats}[name]


async def _blocked_then_overridden(world: World, body: str = _TRANSIENT) -> None:
    refused = await world.write(body, ["infrastructure"])
    assert refused["status"] == "transient_warning", refused
    committed = await world.write(body, ["infrastructure"], acknowledge_transient=True)
    assert committed["status"] == "committed", committed


async def test_with_telemetry_off_a_blocked_and_overridden_marker_reads_half(
    tmp_path: Path,
) -> None:
    """The 1.0 this issue reproduced: one fire, one override."""
    world = World(tmp_path / "store", telemetry=False)
    await _blocked_then_overridden(world)
    row = world.marker("currently")
    assert (row["fire_count"], row["override_count"]) == (1, 1)
    assert row["override_rate"] == 0.5


async def test_with_telemetry_on_a_fire_that_meets_a_lock_is_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fire's first try meets another writer's lock, released before
    the second: the row lands, where it was dropped, and the rate reads
    0.5, where it read 1.0."""
    world = World(tmp_path / "store", telemetry=True)
    store = world.store
    store.set_busy_timeout(0.05)
    real = store.record_event
    holders: list[sqlite3.Connection] = []

    def release(_seconds: float) -> None:
        for holder in holders:
            holder.execute("ROLLBACK")
            holder.close()
        holders.clear()

    def record_event(kind: str, **fields: Any) -> Any:
        """The fire's row meets the lock; the lock goes at the retry's
        pause, or when the row's write returns, whichever comes first."""
        if fields.get("status") != "transient_warning":
            return real(kind, **fields)
        holder = sqlite3.connect(store.path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        holders.append(holder)
        try:
            return real(kind, **fields)
        finally:
            release(0)

    monkeypatch.setattr(store_events, "time", SimpleNamespace(sleep=release))
    monkeypatch.setattr(store, "record_event", record_event)
    try:
        await _blocked_then_overridden(world)
    finally:
        release(0)
    row = world.marker("currently")
    assert (row["fire_count"], row["override_count"]) == (1, 1)
    assert row["override_rate"] == 0.5


async def test_with_telemetry_on_the_rate_reads_as_before(tmp_path: Path) -> None:
    world = World(tmp_path / "store", telemetry=True)
    await _blocked_then_overridden(world)
    await _blocked_then_overridden(
        world, "The release notes are currently drafted by hand each sprint."
    )
    refused = await world.write(
        "The staging database is currently a copy of last week's dump.",
        ["infrastructure"],
    )
    assert refused["status"] == "transient_warning"
    row = world.marker("currently")
    assert (row["fire_count"], row["override_count"]) == (3, 2)
    assert row["override_rate"] == 0.4


@pytest.mark.parametrize(
    ("setup", "content", "scopes", "flags", "status"),
    [
        (None, f"The example key is {_AWS} in the docs.", ["reference"], {}, None),
        (None, "The user prefers tabs over spaces.", ["tools"], {}, None),
        ((_CLEAN, ["tools"]), _CLEAN, ["tools"], {}, "duplicate"),
        ((_ALPHA, ["projects:alpha"]), _CROSS, ["projects:beta"], {}, None),
    ],
    ids=["credential", "user_claim", "duplicate", "scope_mismatch"],
)
async def test_with_telemetry_off_every_overridable_refusal_lands(
    tmp_path: Path,
    setup: tuple[str, list[str]] | None,
    content: str,
    scopes: list[str],
    flags: dict[str, Any],
    status: str | None,
) -> None:
    world = World(tmp_path / "store", telemetry=False)
    if setup is not None:
        await world.write(*setup)
    refused = await world.write(content, scopes, **flags)
    assert refused["status"] in events.OVERRIDABLE_REFUSALS, refused
    if status is not None:
        assert refused["status"] == status
    rows = [e for e in world.store.iter_events() if e["kind"] == "write"]
    # The setup's own commit is a write row too (every write row is state);
    # the refusal's is the last, and the one audit row.
    assert rows[-1]["status"] == refused["status"]
    assert [e for e in rows if events.is_audit_row("write", e)] == [rows[-1]]
    assert _AWS not in str(rows)


def test_the_overridable_refusals_are_the_gates_own() -> None:
    """The gates spell their statuses as literals; the table is tied to
    them here: the write handler's refusals (`_CAPTURE_REFUSAL_HINTS`, one
    answer per status a gate refuses with) and memory_update's warnings,
    but the control-token refusal no flag passes."""
    from bettermemory.handlers import update as update_handler
    from bettermemory.handlers import write as write_handler

    source = Path(update_handler.__file__).read_text(encoding="utf-8")
    update_warnings = set(re.findall(r'"status": "([a-z_]+_warning)"', source))
    assert "truncation_warning" in update_warnings
    gates = set(write_handler._CAPTURE_REFUSAL_HINTS) | update_warnings
    assert set(events.OVERRIDABLE_REFUSALS) == gates - {"control_token_warning"}


def test_each_refusal_names_a_hatch_the_tools_take() -> None:
    hatches = {"force"} | {f"acknowledge_{name}" for name in ACKNOWLEDGE_NAMES}
    assert set(events.OVERRIDABLE_REFUSALS.values()) <= hatches
    for status in events.OVERRIDABLE_REFUSALS:
        assert events.is_audit_row("write", {"status": status}), status
        assert events.is_audit_row("update", {"status": status}), status
    # A refusal no flag passes is no fire.
    assert not events.is_audit_row("write", {"status": "control_token_warning"})
    assert not events.is_audit_row("update", {"status": "stale"})


def test_health_counts_no_side_off_a_row_that_is_not_state() -> None:
    """A row carrying `markers` the writer did not record as a refusal (a
    hand-edited log, a malformed status) is neither side, and a status
    that is not a string does not take the report down."""
    from datetime import datetime, timezone

    rows: list[dict[str, Any]] = [
        {"ts": "2026-04-01T00:00:00Z", "kind": "write", "markers": ["currently"]},
        {
            "ts": "2026-04-01T00:00:01Z",
            "kind": "write",
            "status": ["transient_warning"],
            "markers": ["currently"],
        },
        {
            "ts": "2026-04-01T00:00:02Z",
            "kind": "write",
            "status": "transient_warning",
            "markers": ["currently"],
        },
    ]
    report = compute_health(
        [], rows, window_days=30, now=datetime(2026, 5, 1, tzinfo=timezone.utc)
    )
    [row] = [m.to_dict() for m in report.marker_stats]
    assert (row["fire_count"], row["override_count"]) == (1, 0)
