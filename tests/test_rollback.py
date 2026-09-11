"""Gates for `bettermemory rollback --by-actor`.

The identity resolver's third and last consumer: 7.10.0 stamped a
per-request actor on every record, 7.12.0 read it back as filters,
7.14.0 pivoted the store by it, and this removes by it.

Every gate asserts the PREMISE it depends on before asserting its
conclusion — a fixture that fails to create the condition would
otherwise let the assertion pass for the wrong reason.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.cli import rollback as cli_rollback
from bettermemory.consolidate import consolidate
from bettermemory.identity import Actor
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.rollback import (
    apply_rollback,
    plan_rollback,
    render_text,
    tombstone_reason,
)
from bettermemory.store import Store

CODE = Actor(client="claude-code", model="opus", sources={"client": "client-info"})
HERMES = Actor(client="hermes", model="sonnet", sources={"client": "env"})
# An actor block that EXISTS but declares no client — the shape that
# must land in "undeclared" rather than in a `""` bucket.
NO_CLIENT = Actor(model="sonnet")


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _memory(*, actor: Actor | None, created: datetime = _utc(2026, 5, 1)) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="a body\n",
        actor=actor,
    )


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "by_actor": "hermes",
        "since": None,
        "apply": False,
        "yes": False,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture(autouse=True)
def _isolate_store(memory_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every CLI path in this module at the tmp store.

    `cli_rollback.run` goes through `cli_context()`, which resolves the
    REAL config — so without this a `--apply --yes` gate would commit a
    rollback against the developer's own `~/.claude-memory`. Autouse
    because the cost of remembering it per-test is a destroyed store.
    """
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))


# ---------------------------------------------------------------------------
# G1 — structural isolation: a rollback is NOT a consolidation pass
# ---------------------------------------------------------------------------


def test_g1_rollback_does_not_run_the_consolidation_passes(store: Store) -> None:
    """G1, the most important gate in the unit.

    `consolidate()` runs its four structural passes unconditionally
    before every mode flag fires, so a by-actor rollback implemented as
    one more `consolidate` flag would ALSO commit whole-store dedup
    tombstones and demotions. A separate command cannot: this asserts
    the store genuinely HAS dedup candidates the consolidation pass
    would have taken, then that a committed rollback left every one of
    them alone.
    """
    # Premise: a pair of near-duplicates consolidate would tombstone.
    store.write(
        content="the user prefers terse code-driven explanations over prose",
        scopes=["tools"],
    )
    dup = store.write(
        content="the user prefers terse code-driven explanations over long prose",
        scopes=["tools"],
    )
    store.update(dup)
    target = store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )

    premise = consolidate(store, apply=False)
    assert premise.dedup_candidates, (
        "premise failed: the fixture produced no dedup candidates, so this "
        "test could not tell an isolated rollback from a ride-along one"
    )
    would_be_tombstoned = {c.duplicate_id for c in premise.dedup_candidates}
    assert target.id not in would_be_tombstoned

    # Drive the CLI, not the library: the ride-along trap lives on the
    # command path (where a `consolidate` mode flag would fire AFTER the
    # four passes had already run), so a gate that called `plan_rollback`
    # directly would pass no matter how the command was wired.
    cli_rollback.run(_args(apply=True, yes=True))

    active = {m.id for m in store.load_all()}
    assert target.id not in active, "the rollback did not remove its own target"
    # The conclusion: nothing the consolidation passes wanted was taken.
    for dup_id in would_be_tombstoned:
        assert dup_id in active, (
            f"{dup_id} was tombstoned by a rollback — the consolidation "
            f"passes rode along, which is exactly what this command exists "
            f"to prevent"
        )


# ---------------------------------------------------------------------------
# G2 — selection is not admission
# ---------------------------------------------------------------------------


def test_g2_undeclared_records_are_reported_not_silently_skipped() -> None:
    """G2. `actor_matches` excludes a record with no actor, and on a
    real store that is ~90% of it. The count must be REPORTED, so an
    operator cannot read a small selection as "the store is mostly this
    actor's"."""
    memories = [
        _memory(actor=HERMES),
        _memory(actor=None),
        _memory(actor=None),
        _memory(actor=NO_CLIENT),
    ]
    # Premise: the fixture really does contain all three populations.
    assert sum(1 for m in memories if m.actor is None) == 2
    assert (
        sum(1 for m in memories if m.actor is not None and m.actor.client is None) == 1
    )

    report = plan_rollback(memories, client="hermes")

    assert len(report.candidates) == 1
    # An actor block with no client lands in undeclared, NOT in a "" bucket.
    assert report.declined_undeclared == 3
    assert report.other_actor == 0
    report.assert_reconciles()
    assert "3 declared no client at all" in render_text(report)


def test_g2b_populations_partition_the_active_set_exactly() -> None:
    """G2b. The four counts reconcile EXACTLY against the active set —
    a memory carries exactly one actor, so unlike a scope pivot these
    buckets cannot double-count."""
    memories = [
        _memory(actor=HERMES),
        _memory(actor=HERMES, created=_utc(2026, 1, 1)),
        _memory(actor=CODE),
        _memory(actor=None),
    ]
    report = plan_rollback(memories, client="hermes", since=_utc(2026, 4, 1))
    assert (len(report.candidates), report.other_actor, report.out_of_window) == (
        1,
        1,
        1,
    )
    assert report.declined_undeclared == 1
    assert report.total_active == 4
    report.assert_reconciles()


def test_g2c_reconciliation_is_asserted_not_merely_documented() -> None:
    """G2b's invariant is a raise, not a comment. A hand-corrupted
    report must be refused."""
    report = plan_rollback([_memory(actor=HERMES)], client="hermes")
    report.total_active = 99
    with pytest.raises(AssertionError, match="does not reconcile"):
        report.assert_reconciles()


# ---------------------------------------------------------------------------
# G3 — other actors untouched
# ---------------------------------------------------------------------------


def test_g3_other_actors_are_left_alone(store: Store) -> None:
    """G3. Rolling back one client leaves every other client's records
    in the active set."""
    kept = store.write(
        content="written by the other agent", scopes=["tools"], actor=CODE
    )
    doomed = store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    # Premise: two DIFFERENT declared clients really are present.
    clients = {m.actor.client for m in store.load_all() if m.actor}
    assert clients == {"claude-code", "hermes"}

    report = plan_rollback(store.load_all(), client="hermes")
    assert report.other_actor == 1
    apply_rollback(store, report)

    active = {m.id for m in store.load_all()}
    assert kept.id in active
    assert doomed.id not in active


# ---------------------------------------------------------------------------
# G4 / G5 — nothing commits without both flags
# ---------------------------------------------------------------------------


def test_g4_dry_run_commits_nothing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """G4. Without `--apply` the active set is unchanged."""
    store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    before = {m.id for m in store.load_all()}
    assert before, "premise failed: empty store proves nothing"

    cli_rollback.run(_args())

    assert {m.id for m in store.load_all()} == before
    assert "Dry run — nothing was removed" in capsys.readouterr().out


def test_g5_apply_without_yes_refuses_and_exits_nonzero(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """G5. `--apply` alone does not commit. This is the widest-blast-
    radius command in the tool, so it takes the strictest posture
    already in the tree (`consolidate --llm --apply` refuses to commit
    without `--yes`) and goes one step further by exiting non-zero — a
    script that asked to commit and did not must not read as success."""
    store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    before = {m.id for m in store.load_all()}

    with pytest.raises(SystemExit) as exc:
        cli_rollback.run(_args(apply=True))
    assert exc.value.code == 1

    assert {m.id for m in store.load_all()} == before, (
        "a refused apply still removed records"
    )
    assert "--apply requires --yes" in capsys.readouterr().out


def test_g5b_apply_with_yes_commits(store: Store) -> None:
    """G5b. Both flags together DO commit — otherwise G5 would pass on a
    command that never works at all."""
    doomed = store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    cli_rollback.run(_args(apply=True, yes=True))
    assert doomed.id not in {m.id for m in store.load_all()}


# ---------------------------------------------------------------------------
# G6 — the round trip is real
# ---------------------------------------------------------------------------


def test_g6_tombstone_names_actor_and_window_and_restores_with_actor(
    store: Store,
) -> None:
    """G6. The removal reason names the actor AND the window, so the
    rollback is auditable from the tombstone log alone; and a restored
    record comes back WITH its actor, so it is selectable again — which
    is what makes "reversible" true rather than aspirational."""
    doomed = store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    since = _utc(2026, 1, 1)
    report = plan_rollback(store.load_all(), client="hermes", since=since)
    assert report.candidates, "premise failed: nothing selected"
    apply_rollback(store, report)

    tombstones = store.load_tombstones()
    match = [t for t in tombstones if t.id == doomed.id]
    assert len(match) == 1
    reason = match[0].removed_reason or ""
    assert "client=hermes" in reason
    assert "2026-01-01T00:00:00Z" in reason, (
        "the window is missing from the audit trail"
    )

    restored = store.restore(doomed.id)
    assert restored.actor is not None and restored.actor.client == "hermes"
    # Selectable again: the round trip is re-targetable.
    again = plan_rollback(store.load_all(), client="hermes")
    assert [c.memory_id for c in again.candidates] == [doomed.id]


def test_g6b_reason_distinguishes_an_absent_window_from_a_forgotten_one() -> None:
    """G6b. "all time" and "a reason that forgot to record the window"
    must not read the same."""
    assert "all time" in tombstone_reason("hermes", None)
    assert "created since 2026-01-01T00:00:00Z" in tombstone_reason(
        "hermes", "2026-01-01T00:00:00Z"
    )


# ---------------------------------------------------------------------------
# G7 — the window filters `created`
# ---------------------------------------------------------------------------


def test_g7_since_filters_created_not_updated() -> None:
    """G7. `actor` and `created` are stamped by the same write event;
    `Store.update` never restamps the actor. Filtering on `updated`
    would pair a write-time actor with an edit-time timestamp."""
    old = _memory(actor=HERMES, created=_utc(2026, 1, 1))
    new = _memory(actor=HERMES, created=_utc(2026, 6, 1))
    # Premise: the fixture straddles the cutoff.
    cutoff = _utc(2026, 3, 1)
    assert old.created < cutoff < new.created

    report = plan_rollback([old, new], client="hermes", since=cutoff)

    assert [c.memory_id for c in report.candidates] == [new.id]
    assert report.out_of_window == 1


def test_g7b_updated_does_not_drag_a_record_into_the_window() -> None:
    """G7b. A record CREATED before the cutoff stays out even when it
    was edited well inside it — the half that pins DC4 as deliberate."""
    edited = _memory(actor=HERMES, created=_utc(2026, 1, 1))
    edited.updated = _utc(2026, 6, 1)
    assert edited.updated > _utc(2026, 3, 1) > edited.created

    report = plan_rollback([edited], client="hermes", since=_utc(2026, 3, 1))
    assert report.candidates == []
    assert report.out_of_window == 1


# ---------------------------------------------------------------------------
# G8 — the ISO contract, in both directions
# ---------------------------------------------------------------------------


def test_g8_naive_since_is_refused(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """G8. A naive timestamp from a non-UTC operator would silently
    shift the window by hours, so it is refused — the same contract
    `--acknowledge-misses-before` carries, now from one shared parser."""
    with pytest.raises(SystemExit) as exc:
        cli_rollback.run(_args(since="2026-09-01T10:00:00"))
    assert exc.value.code == 1
    assert "missing a UTC offset" in capsys.readouterr().err


def test_g8b_far_future_since_warns_rather_than_refusing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """G8b. The far-future rule INVERTS between the two callers, and
    that inversion is deliberate. On `--acknowledge-misses-before` a
    typo'd century writes a marker that hides the log forever, so it is
    refused. Here it selects NOTHING — visible in the next line of
    output, destructive of nothing — so a warning is the right weight.
    """
    store.write(
        content="written by the rolled-back agent", scopes=["tools"], actor=HERMES
    )
    future = (datetime.now(timezone.utc) + timedelta(days=365 * 100)).isoformat()

    cli_rollback.run(_args(since=future.replace("+00:00", "Z")))

    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "Selected for removal (0)" in captured.out


# ---------------------------------------------------------------------------
# G9 — authorship, not influence
# ---------------------------------------------------------------------------


def test_g9_selects_authorship_not_influence(store: Store) -> None:
    """G9. The actor is write-time only, so a record this client WROTE
    is selected even after another caller rewrote it. Defensible —
    that is what the stamp means — but not what "roll back an agent's
    contributions" sounds like, so the rendered report says which sense
    it acts on."""
    written = store.write(
        content="written by hermes, edited later", scopes=["tools"], actor=HERMES
    )
    rewritten = store.update(
        written.model_copy(update={"body": "edited by a different caller\n"})
    )
    # Premise: the edit really happened and the actor really survived it.
    assert rewritten.body != written.body
    assert rewritten.actor is not None and rewritten.actor.client == "hermes"

    report = plan_rollback(store.load_all(), client="hermes")

    assert [c.memory_id for c in report.candidates] == [written.id]
    assert "WROTE" in render_text(report)


# ---------------------------------------------------------------------------
# G10 — no selector, no run
# ---------------------------------------------------------------------------


def test_g10_bare_rollback_refuses_rather_than_selecting_everything() -> None:
    """G10. `--by-actor` is required. A rollback that defaulted to "no
    filter" would mean the whole store."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli_rollback.add_subparser(sub)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["rollback"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Purity — the plan half never touches a store
# ---------------------------------------------------------------------------


def test_plan_is_pure_over_a_synthetic_list() -> None:
    """`plan_rollback` takes memories, not a store: no root, no index,
    no events. That is what lets the report promise a reconciliation
    invariant, and what keeps the actor read off the record rather than
    off index columns that have been observed disagreeing with disk."""
    report = plan_rollback(
        [_memory(actor=HERMES), _memory(actor=None)], client="hermes"
    )
    assert len(report.candidates) == 1
    assert report.declined_undeclared == 1
    assert not report.committed
