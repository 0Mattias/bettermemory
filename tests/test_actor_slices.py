"""Gates for the per-actor `memory_health` pivot (`actor_slices`).

The identity resolver's second consumer: 7.10.0 stamped a per-request
actor on every record and event, 7.12.0 read it back as `client` /
`model` filters, and this pivot answers "who wrote what, and which
spellings exist to filter on".

Every gate here asserts the PREMISE it depends on before asserting its
conclusion — a fixture that fails to create the condition would
otherwise let the assertion pass for the wrong reason.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bettermemory.health import compute_health, render_text
from bettermemory.identity import Actor
from bettermemory.models import Confidence, Memory, Source, generate_ulid


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


NOW = _utc(2026, 5, 1)
IN_WINDOW = _utc(2026, 4, 20)
OUT_OF_WINDOW = _utc(2026, 1, 1)


def _memory(*, actor: Actor | None = None, created: datetime = IN_WINDOW) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="x\n",
        actor=actor,
    )


def _event(kind: str, *, actor: Actor | None = None, **fields: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "ts": IN_WINDOW.isoformat().replace("+00:00", "Z"),
        "session": "sess_test",
        "kind": kind,
        **fields,
    }
    if actor is not None:
        ev["actor"] = actor.to_record()
    return ev


CODE = Actor(client="claude-code", model="opus", sources={"client": "client-info"})
HERMES = Actor(client="hermes", model="sonnet", sources={"client": "env"})


def _by_client(report: Any) -> dict[str | None, Any]:
    return {s.client: s for s in report.actor_slices.declared}


# ---------------------------------------------------------------------------
# G1 / G2 — the two exact reconciliation invariants
# ---------------------------------------------------------------------------


def test_memory_counts_reconcile_exactly_to_total_active() -> None:
    """G1. A memory carries exactly ONE actor, so unlike `scope_health`
    (where a memory is counted under each of its scopes and the counts
    deliberately over-sum) this pivot reconciles EXACTLY."""
    memories = [
        _memory(actor=CODE),
        _memory(actor=CODE),
        _memory(actor=HERMES),
        _memory(actor=None),
        _memory(actor=None),
        _memory(actor=None),
    ]
    report = compute_health(memories, [], now=NOW)
    slices = report.actor_slices

    # PREMISE: the fixture really produced all three populations. Without
    # this the sum below could reconcile trivially (e.g. every memory
    # landing in `undeclared` because the actor never attached).
    assert {s.client for s in slices.declared} == {"claude-code", "hermes"}
    assert slices.undeclared.memories == 3
    assert report.total_active_memories == 6

    total = sum(s.memories for s in slices.declared) + slices.undeclared.memories
    assert total == report.total_active_memories


def test_event_counts_reconcile_exactly_to_total_events() -> None:
    """G2. The events half reconciles against `total_events` for the
    same reason, which is why the attribution runs once in
    `handle_event` rather than per-kind: a per-kind hook would only see
    the kinds that have handlers and the sum would silently fall short.
    """
    events = [
        _event("search", actor=CODE, returned=[]),
        _event("search", actor=HERMES, returned=[]),
        _event("use", actor=CODE, outcome="applied", ids=[]),
        _event("write", actor=None),
        # A kind with NO entry in `_HANDLERS` — the sum must still
        # account for it, which is the property a per-kind hook loses.
        _event("scope_overview", actor=CODE),
    ]
    report = compute_health([], events, now=NOW)
    slices = report.actor_slices

    # PREMISE: an unhandled kind is genuinely present in the fixture.
    from bettermemory.health import _StatsAccumulator

    assert "scope_overview" not in _StatsAccumulator._HANDLERS
    assert report.total_events == 5

    total = sum(s.events for s in slices.declared) + slices.undeclared.events
    assert total == report.total_events
    assert _by_client(report)["claude-code"].events == 3


# ---------------------------------------------------------------------------
# G3 — the third value: undeclared is present even at zero
# ---------------------------------------------------------------------------


def test_undeclared_bucket_is_present_even_when_empty() -> None:
    """G3. "Nobody undeclared" and "the field isn't there" must not read
    the same. The bucket is emitted at zero on a fully-attributed store.
    """
    report = compute_health([_memory(actor=CODE)], [], now=NOW)

    # PREMISE: nothing in this fixture is undeclared.
    assert report.total_active_memories == 1
    assert _by_client(report)["claude-code"].memories == 1

    assert report.actor_slices.undeclared is not None
    assert report.actor_slices.undeclared.client is None
    assert report.actor_slices.undeclared.memories == 0
    assert report.actor_slices.undeclared.events == 0
    # And it survives serialisation as a present key, not a dropped one.
    assert report.to_dict()["actor_slices"]["undeclared"]["memories"] == 0


# ---------------------------------------------------------------------------
# G4 — no client is not an empty string
# ---------------------------------------------------------------------------


def test_actor_without_client_lands_in_undeclared_not_an_empty_string_slice() -> None:
    """G4. An actor that declared only a model has no `client`. It is
    undeclared ON THE AXIS THIS PIVOT IS KEYED ON — never a slice named
    `""` — but the spelling it DID declare still surfaces in that
    bucket, so nothing declared is lost.
    """
    model_only = Actor(model="opus")
    report = compute_health([_memory(actor=model_only)], [], now=NOW)

    # PREMISE: the actor exists and genuinely carries no client.
    assert model_only.client is None
    assert model_only.model == "opus"

    assert report.actor_slices.declared == []
    assert "" not in {s.client for s in report.actor_slices.declared}
    assert report.actor_slices.undeclared.memories == 1
    assert report.actor_slices.undeclared.models == {"opus": 1}


# ---------------------------------------------------------------------------
# G5 — the census half: which spellings exist
# ---------------------------------------------------------------------------


def test_model_spellings_surface_and_absence_is_an_empty_dict() -> None:
    """G5. The census the 7.12.0 filters deliberately refuse: a caller
    needs to know which `model` values exist before it can filter on
    one. An undeclared model is an EMPTY DICT, never a manufactured
    `<unset>` key — a census whose job is to report real spellings must
    not invent one.
    """
    two_spellings = [
        _memory(actor=Actor(client="hermes", model="opus")),
        _memory(actor=Actor(client="hermes", model="sonnet")),
        _memory(actor=Actor(client="hermes", model="opus")),
    ]
    no_model = [_memory(actor=Actor(client="claude-code"))]
    report = compute_health(two_spellings + no_model, [], now=NOW)
    by = _by_client(report)

    # PREMISE: both populations are present as written.
    assert by["hermes"].memories == 3
    assert by["claude-code"].memories == 1

    assert by["hermes"].models == {"opus": 2, "sonnet": 1}
    assert by["claude-code"].models == {}
    assert "<unset>" not in by["claude-code"].models


# ---------------------------------------------------------------------------
# G6 — the window split is real
# ---------------------------------------------------------------------------


def test_in_window_count_excludes_memories_created_before_the_cutoff() -> None:
    """G6. `memories` is store-wide (it has to be, for G1); the "what
    did this agent write this run" read is the separate windowed count.
    """
    memories = [
        _memory(actor=CODE, created=IN_WINDOW),
        _memory(actor=CODE, created=OUT_OF_WINDOW),
    ]
    report = compute_health(memories, [], window_days=30, now=NOW)
    code = _by_client(report)["claude-code"]

    # PREMISE: the fixture straddles the cutoff — one each side.
    assert (NOW - IN_WINDOW).days < 30
    assert (NOW - OUT_OF_WINDOW).days > 30

    assert code.memories == 2
    assert code.memories_in_window == 1


# ---------------------------------------------------------------------------
# G7 — both render surfaces carry it
# ---------------------------------------------------------------------------


def test_render_text_and_to_dict_carry_the_pivot() -> None:
    """G7."""
    report = compute_health(
        [_memory(actor=HERMES), _memory(actor=None)],
        [_event("search", actor=HERMES, returned=[])],
        now=NOW,
    )

    payload = report.to_dict()["actor_slices"]
    assert payload["declared"][0]["client"] == "hermes"
    assert payload["declared"][0]["searches"] == 1
    assert payload["undeclared"]["memories"] == 1

    text = render_text(report)
    assert "Actors (1 declared):" in text
    assert "hermes" in text
    # The undeclared row is rendered even though no actor named it.
    assert "(undeclared)" in text


# ---------------------------------------------------------------------------
# G8 — purity: no index, no store, no root
# ---------------------------------------------------------------------------


def test_pivot_is_computed_without_an_index_or_a_store_root() -> None:
    """G8. `compute_health` is pure over memories + events. The pivot is
    built from those arguments alone — deliberately not off the v11
    actor columns, so it cannot disagree with itself when the index
    sits a row behind disk.
    """
    report = compute_health(
        [_memory(actor=CODE)],
        [_event("search", actor=CODE, returned=[])],
        now=NOW,
    )
    # The index-backed field is null on this path, which is the proof
    # that no index was consulted for the run that populated the pivot.
    assert report.provenance is None
    assert _by_client(report)["claude-code"].memories == 1
    assert _by_client(report)["claude-code"].searches == 1


# ---------------------------------------------------------------------------
# Robustness — a census over an append-only log other writers append to
# ---------------------------------------------------------------------------


def test_a_malformed_actor_on_an_event_reads_as_undeclared_and_does_not_raise() -> None:
    """One bad line must not blank the whole pivot. Same defensive
    posture the `session` read takes two lines above it."""
    events: list[dict[str, Any]] = [
        {
            "ts": IN_WINDOW.isoformat(),
            "session": "s",
            "kind": "search",
            "actor": "nope",
        },
        {
            "ts": IN_WINDOW.isoformat(),
            "session": "s",
            "kind": "search",
            "actor": {"client": []},
        },
        {
            "ts": IN_WINDOW.isoformat(),
            "session": "s",
            "kind": "search",
            "actor": {"client": ""},
        },
    ]
    report = compute_health([], events, now=NOW)

    assert report.total_events == 3
    assert report.actor_slices.declared == []
    assert report.actor_slices.undeclared.events == 3
    assert report.actor_slices.undeclared.searches == 3
