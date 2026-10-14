"""`capture undo` (`bettermemory.capture_review`): a batch's memories are
tombstoned with the undo row in one transaction, `tombstones restore`
reverses it, the mirror after the undo is the mirror before the capture
but for the marker and the batch's tombstone files, and the log still
verifies. A member someone has since relied on is not undone silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory import capture, capture_review
from bettermemory.mirror import MIRROR_MARKER, write_mirror
from bettermemory.models import LinkType, MemoryLink
from bettermemory.v8 import TOMBSTONE_DIR

from .test_capture_batch import World, _fixed_origin  # noqa: F401
from .test_capture_batch import world as world  # noqa: F401

BODIES = (
    "The bettermemory daemon binds 127.0.0.1 only and listens on port 7397.",
    "Ruff 0.17.0 under /opt/homebrew/bin shadows the project's pinned ruff.",
)


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def _captured(world: World) -> tuple[capture.Batch, list[str]]:
    batch = world.open()
    ids = []
    for body in BODIES:
        result = await world.capture_write(batch, body)
        assert result["status"] == "committed", result
        ids.append(result["id"])
    capture.close_batch(
        world.store,
        batch,
        outcome=capture.OUTCOME_SETTLED,
        watermark=100_000,
        transcript_bytes=100_000,
    )
    return batch, ids


async def test_undo_tombstones_and_restore_reverses_it(
    world: World, tmp_path: Path
) -> None:
    kept = world.store.write(
        content="An unrelated memory the undo leaves.", scopes=["x"]
    )
    before = tmp_path / "before"
    write_mirror(world.store, before)
    batch, ids = await _captured(world)

    stored = world.store.load_one(ids[0])
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.refusal_reason is None and plan.to_undo == ids
    assert world.store.count_memories() == 3  # a plan writes nothing
    capture_review.apply_undo(world.store, plan)
    assert world.store.count_memories() == 1
    assert world.store.load_one(kept.id).body == kept.body
    assert capture.undone_hashes(world.store) == {capture.body_hash(b) for b in BODIES}
    # The undo row commits with the tombstones: the row right after them.
    assert world.kinds()[-5:] == [
        *["memory_delete", "tombstone_put"] * 2,
        "capture_undo",
    ]

    after = tmp_path / "after"
    write_mirror(world.store, after)
    old, new = _files(before), _files(after)
    old.pop(MIRROR_MARKER)
    new.pop(MIRROR_MARKER)
    tombs = {name for name in new if name.startswith(TOMBSTONE_DIR + "/")}
    assert len(tombs) == 2 and all(any(i in t for i in ids) for t in tombs)
    assert {k: v for k, v in new.items() if k not in tombs} == old
    assert world.store.log_verify()["status"] == "ok"

    world.store.restore(ids[0])
    assert world.store.load_one(ids[0]).body == stored.body
    # A second undo finds the restored member and nothing else to do.
    again = capture_review.plan_undo(world.store, batch.batch)
    assert (again.to_undo, again.gone) == ([ids[0]], [ids[1]])


async def test_a_member_changed_since_is_refused_then_skipped(
    world: World,
) -> None:
    batch, (updated, linked) = await _captured(world)
    memory = world.store.load_one(updated)
    world.store.update(memory.model_copy(update={"body": memory.body + " Since 9.0."}))
    world.store.write(
        content="A memory written later that leans on the captured one.",
        scopes=["x"],
        links=[MemoryLink(type=LinkType.EXTENDS, target_id=linked)],
    )
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.changed == {
        updated: [capture_review.CHANGED_UPDATED],
        linked: [capture_review.CHANGED_LINKED],
    }
    assert plan.refusal_reason is not None and plan.to_undo == []
    with pytest.raises(ValueError):
        capture_review.apply_undo(world.store, plan)
    assert world.store.count_memories() == 3

    skip = capture_review.plan_undo(world.store, batch.batch, skip_changed=True)
    assert skip.refusal_reason is None and skip.to_undo == []
    world.store.mark_verified(updated)
    world.store.put_conflict(
        {
            "id": "c1",
            "a_id": linked,
            "b_id": "other",
            "summary_a": "a",
            "summary_b": "b",
            "similarity": 0.5,
            "method": "polarity",
            "detector": "numeric",
            "created": "2026-10-12T00:00:00Z",
            "status": "pending",
        }
    )
    changed = capture_review.plan_undo(world.store, batch.batch).changed
    assert capture_review.CHANGED_VERIFIED in changed[updated]
    assert capture_review.CHANGED_CONFLICT in changed[linked]


def _conflict(pair: str, a_id: str, b_id: str, created: str) -> dict[str, object]:
    return {
        "id": pair,
        "a_id": a_id,
        "b_id": b_id,
        "summary_a": "a",
        "summary_b": "b",
        "similarity": 0.5,
        "method": "jaccard",
        "detector": "numeric",
        "created": created,
        "status": "pending",
    }


async def test_a_conflict_the_members_own_write_filed_does_not_block_its_undo(
    world: World,
) -> None:
    """The write path files a contradiction with the member as `a_id`
    right after the commit. That capture is the one the owner most wants
    back: it is undone, and the batch's own conflict rows go with it in
    the same transaction."""
    from datetime import timedelta

    from bettermemory.conflicts import _pair_id

    stored = world.store.write(content="An unrelated stored memory.", scopes=["x"])
    batch, (filer, other) = await _captured(world)
    created = world.store.load_one(filer).created + timedelta(milliseconds=5)
    own = _pair_id(filer, stored.id)
    world.store.put_conflict(_conflict(own, filer, stored.id, created.isoformat()))
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.changed == {} and plan.to_undo == [filer, other]
    capture_review.apply_undo(world.store, plan)
    assert world.store.list_conflicts() == []
    assert world.kinds()[-1] == "capture_undo"
    assert world.store.log_verify()["status"] == "ok"
    # Restore stays coherent: the member comes back with no dangling row.
    world.store.restore(filer)
    assert world.store.load_one(filer).id == filer
    assert world.store.list_conflicts() == []


async def test_a_conflict_filed_by_another_write_or_later_still_blocks(
    world: World,
) -> None:
    from datetime import timedelta

    stored = world.store.write(content="An unrelated stored memory.", scopes=["x"])
    batch, (later, against) = await _captured(world)
    afterwards = world.store.load_one(later).created + timedelta(hours=2)
    world.store.put_conflict(
        _conflict("c-later", later, stored.id, afterwards.isoformat())
    )
    world.store.put_conflict(
        _conflict("c-other", stored.id, against, afterwards.isoformat())
    )
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.changed == {
        later: [capture_review.CHANGED_CONFLICT],
        against: [capture_review.CHANGED_CONFLICT],
    }


async def test_a_change_between_plan_and_apply_is_caught_inside_the_undo(
    world: World,
) -> None:
    """The changed-since check runs again inside the transaction that
    tombstones: a member updated after the dry run is refused, or left in
    place under --skip-changed, exactly as the dry run would have said."""
    batch, (touched, untouched) = await _captured(world)
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.refusal_reason is None and plan.to_undo == [touched, untouched]
    memory = world.store.load_one(touched)
    world.store.update(memory.model_copy(update={"body": memory.body + " Since 9.0."}))
    applied = capture_review.apply_undo(world.store, plan)
    assert not applied.committed and applied.refusal_reason is not None
    assert applied.changed == {touched: [capture_review.CHANGED_UPDATED]}
    assert world.store.count_memories() == 2
    assert capture.undone_hashes(world.store) == frozenset()

    skip = capture_review.plan_undo(world.store, batch.batch, skip_changed=True)
    world.store.write(
        content="A memory written later that leans on the captured one.",
        scopes=["x"],
        links=[MemoryLink(type=LinkType.EXTENDS, target_id=untouched)],
    )
    applied = capture_review.apply_undo(world.store, skip)
    assert applied.committed and applied.to_undo == []
    assert applied.changed == {
        touched: [capture_review.CHANGED_UPDATED],
        untouched: [capture_review.CHANGED_LINKED],
    }
    assert world.store.count_memories() == 3


async def test_an_interrupted_batch_is_undoable_and_an_open_one_is_not(
    world: World,
) -> None:
    batch = world.open()
    result = await world.capture_write(batch, BODIES[0])
    assert result["status"] == "committed"
    refused = capture_review.plan_undo(world.store, batch.batch)
    assert "still open" in (refused.refusal_reason or "")
    capture.close_batch(
        world.store,
        batch,
        outcome=capture.OUTCOME_INTERRUPTED,
        watermark=0,
        transcript_bytes=100_000,
    )
    plan = capture_review.plan_undo(world.store, batch.batch)
    assert plan.outcome == capture.OUTCOME_INTERRUPTED
    capture_review.apply_undo(world.store, plan)
    assert world.store.count_memories() == 0
    assert capture_review.plan_undo(world.store, "nope").refusal_reason


async def test_review_lists_outcomes_members_refusals_and_undos(
    world: World,
) -> None:
    batch, ids = await _captured(world)
    refused = await world.capture_write(
        world.open(session_id="other"), "TODO: tidy the release notes next week."
    )
    assert refused["status"] != "committed"
    capture_review.apply_undo(
        world.store, capture_review.plan_undo(world.store, batch.batch)
    )
    doc = capture_review.review(world.store)
    first, second = doc["batches"]
    assert (first["outcome"], first["members"], first["undone"]) == ("settled", 2, 2)
    assert (second["outcome"], second["members"]) == ("open", 0)
    assert second["refusals"] and sum(second["refusals"].values()) >= 1
    assert doc["by_outcome"] == {"open": 1, "settled": 1}
    assert doc["refusals_by_rule"] == second["refusals"]
    assert doc["undos"] == 1
    text = capture_review.render_review(doc)
    assert batch.batch in text and "2 undone" in text
