"""ENG-200: a standing memory that comes back into force is held out of
force until its own memory_update reads it against the standing roster.

ENG-112 reads every standing statement against the standing roster before
it is in force: a standing write, and an update that makes a memory
standing. A memory already standing came back into force with no read:
the memory that replaced it unlinked or removed, itself restored, or made
standing while it was out of force and then brought back one of those
ways. A ruling written while it was out of force never saw it on its own
roster, which lists only memories in force.

The case is ENG-112's: the plan that breaks the ruling without naming it,
here standing and out of force while the ruling is written, then brought
back through each door. Before this change the plan was in force again at
once, the ruling never shown.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import review, standing, store_returned, store_schema
from bettermemory.admission import EVERY_RECORD
from bettermemory.handlers.restore import restore_with_trust_check
from bettermemory.in_force import in_force
from bettermemory.recall import WORDS
from bettermemory.store import StandingAnswer, Store

from ._mcp import call_tool
from ._review_rows import FACTS, Harness, fix_origin_and_identity, is_roster, set_tau
from .test_write_roster import NEAR, PLAN, RULING

DOORS = ("remove", "unlink", "answered_out_of_force", "restore", "cli_restore")


@pytest.fixture(autouse=True)
def _setup(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)
    WORDS.clear()
    yield
    WORDS.clear()


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _standing(h: Harness, record_id: str, probability: float = 0.9) -> None:
    h.store.put_standing(record_id, StandingAnswer(probability, "asked", "test/test"))


def _supersede(h: Harness, target: str, text: str = FACTS[3]) -> str:
    """A record that replaces `target`, written straight to the store."""
    return h.store.write(
        content=text,
        scopes=["tools"],
        origin=h.store.load_one(target).origin,
        home=h.home,
        links=[{"type": "supersedes", "target_id": target}],
    ).id


def _in_force(h: Harness, record_id: str) -> bool:
    """Whether `record_id` is a standing memory in force: listed on a
    roster and delivered."""
    return record_id in standing.standing_ids(h.store, EVERY_RECORD)


async def _tool(h: Harness, name: str, **args: Any) -> dict[str, Any]:
    return await call_tool(h.server, name, args)


def _roster_ids(page: dict[str, Any]) -> list[str]:
    return [line.split(" ", 1)[0] for line in page["review"]["roster"]]


async def _bring_back(h: Harness, door: str) -> tuple[str, str, dict[str, Any]]:
    """The plan standing and out of force while the ruling is written,
    then brought back through `door`: return the ruling, the plan and the
    door's response ({} for the CLI's restore)."""
    for text in NEAR:
        h.seed(text)
    plan = h.seed(PLAN)
    replacer = ""
    if door == "answered_out_of_force":
        replacer = _supersede(h, plan)
        answered = await _tool(h, "memory_update", id=plan, standing=0.9)
        assert answered["status"] == "committed", answered
    elif door in ("restore", "cli_restore"):
        _standing(h, plan)
        h.store.tombstone(plan, "set aside")
    else:
        _standing(h, plan)
        replacer = _supersede(h, plan)
    ruling = h.seed(RULING)
    _standing(h, ruling)
    assert not _in_force(h, plan)
    if door in ("remove", "answered_out_of_force"):
        res = await _tool(h, "memory_remove", id=replacer, reason="it was wrong")
        assert res["removed"] == replacer, res
    elif door == "unlink":
        res = await _tool(h, "memory_update", id=replacer, links=[])
        assert res["status"] == "committed", res
    elif door == "restore":
        res = await _tool(h, "memory_admin", action="restore", id=plan)
        assert res["status"] == "committed", res
    else:
        restore_with_trust_check(h.store, plan)
        res = {}
    return ruling, plan, res


# ---------------------------------------------------------------------------
# The case, through each door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("door", DOORS)
async def test_the_case_is_read_against_its_ruling_through_every_door(
    h: Harness, door: str
) -> None:
    """The plan brought back is held out of force until its own
    memory_update shows it the roster, the ruling on it; a conflict keeps
    it held, and an answer that clears it puts it in force."""
    ruling, plan, res = await _bring_back(h, door)
    assert not _in_force(h, plan)
    assert plan in standing.guarded_ids(h.store, [plan])
    if door != "cli_restore":
        assert [row["id"] for row in res["held"]] == [plan], res
        assert f'memory_update(id="{plan}"' in res["hint"]
    page = await _tool(h, "memory_update", id=plan, standing=0.9)
    assert is_roster(page), page
    assert ruling in _roster_ids(page)
    named = {"review_id": page["review"]["review_id"], "named": [ruling]}
    full = await _tool(h, "memory_update", id=plan, standing=0.9, review=named)
    assert [entry["id"] for entry in full["review"]["related"]] == [ruling], full
    answers = {**named, "review_id": full["review"]["review_id"]}
    refused = await _tool(
        h,
        "memory_update",
        id=plan,
        standing=0.9,
        review={**answers, "relation": {ruling: "conflict"}},
    )
    assert refused["status"] == review.ACTION_STANDING, refused
    assert not _in_force(h, plan)
    done = await _tool(
        h,
        "memory_update",
        id=plan,
        standing=0.9,
        review={**answers, "relation": {ruling: "unrelated"}},
    )
    assert done["status"] == "committed", done
    assert _in_force(h, plan)


async def test_a_restored_memory_still_replaced_stays_out_of_force(
    h: Harness,
) -> None:
    """Removing a memory dropped the index rows of the links into it, so a
    restore put it in force although the record replacing it still names
    it. The restore puts those rows back: it stays history, replaced."""
    plan = h.seed(PLAN)
    _standing(h, plan)
    replacer = _supersede(h, plan)
    h.store.tombstone(plan, "set aside")
    res = await _tool(h, "memory_admin", action="restore", id=plan)
    assert res["status"] == "committed", res
    assert "held" not in res
    forced = in_force(h.store, [plan], EVERY_RECORD)[plan]
    assert (forced.id, forced.replaces) == (replacer, plan)
    assert not _in_force(h, plan)


# ---------------------------------------------------------------------------
# The store's marks
# ---------------------------------------------------------------------------


def _write_linked(h: Harness, text: str, link_type: str, target: str) -> str:
    return h.store.write(
        content=text,
        scopes=["tools"],
        origin=h.store.load_one(target).origin,
        home=h.home,
        links=[{"type": link_type, "target_id": target}],
    ).id


def test_a_removal_marks_what_it_replaced_after_its_own_rows(h: Harness) -> None:
    """The mark lands at the commit, after the removal's own rows, so the
    history reads the delete and the tombstone as one removal."""
    plan = h.seed(PLAN)
    replacer = _supersede(h, plan)
    h.store.tombstone(replacer, "it was wrong")
    assert h.store.returned_rows()[plan]["via"] == replacer
    kinds = [row.kind for row in h.store.log_rows()][-3:]
    assert kinds == ["memory_delete", "tombstone_put", "returned_put"]
    assert h.store.log_verify()["status"] == "ok"


def test_an_unlink_marks_what_it_stopped_replacing(h: Harness) -> None:
    plan = h.seed(PLAN)
    replacer = _supersede(h, plan)
    current = h.store.load_one(replacer)
    h.store.update(current.model_copy(update={"links": []}))
    assert h.store.returned_rows()[plan]["via"] == replacer


def test_a_record_another_still_replaces_is_not_marked(h: Harness) -> None:
    plan = h.seed(PLAN)
    first = _supersede(h, plan)
    _supersede(h, plan, FACTS[4])
    h.store.tombstone(first, "it was wrong")
    assert h.store.returned_rows() == {}


def test_a_removed_record_loses_its_mark_and_a_restore_marks_it_again(
    h: Harness,
) -> None:
    plan = h.seed(PLAN)
    replacer = _supersede(h, plan)
    h.store.tombstone(replacer, "it was wrong")
    h.store.tombstone(plan, "set aside")
    assert plan not in h.store.returned_rows()
    restore_with_trust_check(h.store, plan)
    assert h.store.returned_rows()[plan]["via"] == plan
    assert h.store.log_verify()["status"] == "ok"


def test_a_record_that_replaces_it_again_drops_its_mark(h: Harness) -> None:
    """A mark is only ever on a record in force: one superseding it again
    drops the mark, and nothing names it held while it is history."""
    from bettermemory.history import record_history

    plan = h.seed(PLAN)
    _standing(h, plan)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert standing.held_ids(h.store) == {plan}
    again = _supersede(h, plan, FACTS[4])
    assert h.store.returned_rows() == {}
    assert standing.held_ids(h.store) == set()
    rows = [r for r in record_history(h.store, plan).rows if r.kind.startswith("ret")]
    assert [(r.action, r.detail.get("dropped")) for r in rows] == [
        ("returned", None),
        ("unmarked", "superseded"),
    ]
    h.store.tombstone(again, "wrong too")
    assert standing.held_ids(h.store) == {plan}
    assert h.store.log_verify()["status"] == "ok"


def test_a_restore_puts_back_no_row_the_log_does_not_account_for(
    h: Harness,
) -> None:
    """Putting a planted row again would sign it into the log: the
    restore leaves it as it is, and its link is not read."""
    plan = h.seed(PLAN)
    replacer = _supersede(h, plan)
    h.store.tombstone(plan, "set aside")
    h.store._conn.execute(
        "UPDATE memories SET log_mac = ? WHERE id = ?", ("00" * 32, replacer)
    )
    assert h.store.provenance_for([replacer])[replacer] == "unaccounted"
    seen = len(h.store.log_rows())
    restore_with_trust_check(h.store, plan)
    puts = [row for row in h.store.log_rows()[seen:] if row.kind == "memory_put"]
    assert len(puts) == 1
    assert h.store.provenance_for([replacer])[replacer] == "unaccounted"


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------


async def test_a_held_memory_is_changed_only_on_the_users_words(h: Harness) -> None:
    _, plan, _ = await _bring_back(h, "remove")
    res = await _tool(h, "memory_remove", id=plan, reason="tidying up")
    assert res["status"] == "refused_standing", res
    assert [row["id"] for row in res["standing"]] == [plan]


def test_a_held_memory_takes_no_slot_it_replaced_and_extends_nothing(
    h: Harness,
) -> None:
    older = h.seed(FACTS[5])
    plan = _write_linked(h, PLAN, "supersedes", older)
    _standing(h, plan)
    detail = _write_linked(h, FACTS[6], "extends", older)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    forced = in_force(h.store, [older, plan], EVERY_RECORD)
    assert forced[plan].held and not forced[plan].history
    assert (forced[older].id, forced[older].held) == (plan, True)
    # A record not held that replaces it too takes the slot instead.
    other = _supersede(h, older, FACTS[7])
    forced = in_force(h.store, [older], EVERY_RECORD)
    assert (forced[older].id, forced[older].held) == (other, False)
    assert detail not in standing.held_ids(h.store)


def test_a_held_extender_is_not_named(h: Harness) -> None:
    older = h.seed(FACTS[5])
    plan = _write_linked(h, PLAN, "extends", older)
    _standing(h, plan)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert in_force(h.store, [older], EVERY_RECORD)[older].extended_by is None


def test_a_memory_that_is_not_standing_comes_back_in_force(h: Harness) -> None:
    plan = h.seed(PLAN)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert plan in h.store.returned_rows()
    forced = in_force(h.store, [plan], EVERY_RECORD)[plan]
    assert not forced.history and not forced.held


async def test_with_no_other_ruling_in_force_its_answer_releases_it(
    h: Harness,
) -> None:
    plan = h.seed(PLAN)
    _standing(h, plan)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert not _in_force(h, plan)
    res = await _tool(h, "memory_update", id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert _in_force(h, plan)
    assert plan not in h.store.returned_rows()
    kinds = [
        row.kind
        for row in h.store.log_rows()
        if row.kind in store_schema.MUTATION_KINDS
    ]
    assert kinds[-2:] == ["standing_put", "returned_delete"]


async def test_an_update_that_makes_a_marked_memory_standing_releases_it(
    h: Harness,
) -> None:
    """A memory that came back while it was not standing is marked and not
    held; the update that makes it standing reads the roster and drops the
    mark, so it is in force, not held, once committed."""
    plan = h.seed(PLAN)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert plan in h.store.returned_rows()
    assert standing.held_ids(h.store) == set()
    res = await _tool(h, "memory_update", id=plan, standing=0.9)
    assert res["status"] == "committed", res
    assert plan not in h.store.returned_rows()
    assert _in_force(h, plan)


async def test_any_update_that_leaves_it_standing_reads_it(h: Harness) -> None:
    ruling, plan, _ = await _bring_back(h, "unlink")
    page = await _tool(h, "memory_update", id=plan, scopes=["tools", "ops"])
    assert is_roster(page), page
    assert ruling in _roster_ids(page)
    assert "came back into force" in page["hint"]


def test_with_the_review_off_nothing_is_held_and_the_mark_waits(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = h.seed(PLAN)
    _standing(h, plan)
    monkeypatch.setattr(review, "TAU_UNSAFE", None)
    h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert _in_force(h, plan)
    set_tau(monkeypatch)
    assert not _in_force(h, plan)


async def test_answer_standing_sends_a_held_memory_to_memory_update(
    h: Harness,
) -> None:
    _, plan, _ = await _bring_back(h, "remove")
    sent = await _tool(
        h, "memory_admin", action="answer_standing", standing={plan: 0.95}
    )
    assert sent["standing_review"] == [plan], sent


async def test_a_search_flags_a_held_hit(h: Harness) -> None:
    _, plan, _ = await _bring_back(h, "remove")
    found = await _tool(h, "memory_search", query="billing database migration")
    hits = found.get("result", found) if isinstance(found, dict) else found
    flagged = {hit["id"]: hit.get("held") for hit in hits}
    assert flagged.get(plan) is True, flagged


def test_a_cli_report_names_what_its_removals_left_held(h: Harness) -> None:
    from bettermemory.capture_review import UndoReport, render_undo

    plan = h.seed(PLAN)
    _standing(h, plan)
    replacer = _supersede(h, plan)
    other = h.seed(FACTS[4])
    h.store.tombstone(replacer, "it was wrong")
    h.store.tombstone(other, "unrelated")
    held = standing.held_by_removal(
        h.store, [replacer, other, "01M5K000000000000000000009"]
    )
    assert held == [plan]
    text = render_undo(UndoReport(batch="b1", committed=True, held=held))
    assert f'memory_update(id="{plan}", standing=<0 to 1>)' in text
    assert standing.held_lines([]) == []


async def test_memory_show_and_the_first_output_name_a_held_memory(
    h: Harness,
) -> None:
    from bettermemory.location import resolve_caller
    from bettermemory.recall_admission import held_line

    _, plan, _ = await _bring_back(h, "remove")
    shown = await _tool(h, "memory_show", id=plan)
    assert shown["held"] is True
    assert f'memory_update(id="{plan}"' in shown["held_hint"]
    caller = resolve_caller(h.store, h.config, os.getcwd())
    line = held_line(h.store, caller.resolution)
    assert plan in line and "held out of force" in line


# ---------------------------------------------------------------------------
# What earlier builds brought back
# ---------------------------------------------------------------------------


def test_the_backfill_marks_what_an_earlier_build_brought_back_once(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = h.seed(PLAN)
    still = h.seed(FACTS[8])
    _supersede(h, still, FACTS[9])
    never = h.seed(FACTS[10])
    with monkeypatch.context() as earlier:
        # An earlier build marked nothing.
        earlier.setattr(store_returned, "candidates", lambda *_args: [])
        h.store.tombstone(_supersede(h, plan), "it was wrong")
    assert h.store.returned_rows() == {}
    assert store_returned.ensure_backfilled(h.store) == []
    h.store._conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, 'pending')",
        (store_schema.RETURNED_BACKFILL_KEY,),
    )
    assert store_returned.ensure_backfilled(h.store) == [plan]
    assert set(h.store.returned_rows()) == {plan}
    assert never not in h.store.returned_rows()
    assert store_returned.ensure_backfilled(h.store) == []
    assert h.store.log_verify()["status"] == "ok"


def test_the_backfill_reads_an_earlier_builds_restores(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier build's restore marked nothing and left out the index
    rows of the links into the record. The backfill puts the rows back, so
    a restored record still replaced stays history, and marks a restored
    record in force."""
    replaced = h.seed(PLAN)
    _standing(h, replaced)
    _supersede(h, replaced)
    restored = h.seed(FACTS[8])
    _standing(h, restored)
    for record_id in (replaced, restored):
        h.store.tombstone(record_id, "set aside")
    with monkeypatch.context() as earlier:
        earlier.setattr(store_returned, "settle", lambda *_args, **_kw: ([], []))
        earlier.setattr(Store, "_relink", lambda *_args, **_kw: [])
        for record_id in (replaced, restored):
            restore_with_trust_check(h.store, record_id)
    # The earlier build's restore read the replaced record as in force.
    assert not in_force(h.store, [replaced], EVERY_RECORD)[replaced].history
    assert store_returned.missing_links(h.store._conn)
    h.store._conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, 'pending')",
        (store_schema.RETURNED_BACKFILL_KEY,),
    )
    assert store_returned.ensure_backfilled(h.store) == [restored]
    assert store_returned.missing_links(h.store._conn) == {}
    assert in_force(h.store, [replaced], EVERY_RECORD)[replaced].history
    assert standing.held_ids(h.store) == {restored}
    assert h.store.log_verify()["status"] == "ok"


def test_a_backfill_that_fails_runs_again_at_the_next_open(
    h: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h.store._conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, 'pending')",
        (store_schema.RETURNED_BACKFILL_KEY,),
    )

    def fails(*_args: Any, **_kw: Any) -> list[str]:
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as broken:
        broken.setattr(store_returned, "backfill", fails)
        assert store_returned.ensure_backfilled(h.store) == []
    assert "will run at the next open" in caplog.text
    key = (store_schema.RETURNED_BACKFILL_KEY,)
    query = "SELECT value FROM meta WHERE key = ?"
    assert h.store._conn.execute(query, key).fetchone() is not None
    assert store_returned.ensure_backfilled(h.store) == []
    assert h.store._conn.execute(query, key).fetchone() is None


def test_step_2_leaves_the_backfill_to_the_owners_open(tmp_path: Path) -> None:
    path = tmp_path / "store" / "memory.sqlite"
    with Store.create(path):
        pass
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_revision'")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'min_writer_revision'")
        conn.execute("DROP TABLE returned")
    conn.close()
    with Store.open(path, allow_rekey=True) as owner:
        row = owner._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (store_schema.RETURNED_BACKFILL_KEY,),
        ).fetchone()
        assert row is not None and row[0] == "pending"
        assert owner.returned_rows() == {}
        assert store_returned.ensure_backfilled(owner) == []
        row = owner._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (store_schema.RETURNED_BACKFILL_KEY,),
        ).fetchone()
        assert row is None
