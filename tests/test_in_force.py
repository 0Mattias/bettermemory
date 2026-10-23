"""What is in force at delivery (`in_force`, unit RS-F7 part 3).

The walk: a record an admitted active record supersedes is history and
its head (the newest admitted superseder, followed to the end, a cycle
closing the walk) takes its slot; a link from a record the caller is not
admitted to changes nothing; the newest in-force record extending a
record is named. Then the two surfaces: the prompt recall gives a history
hit's slot to its head (``replaces``, the head's own gates, ``superseded``
when the head is shown anyway) and names extenders; the session brief
shows no history row and names extenders. The last test is part 2's on
the brief: a global record naming the caller's project is briefed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory import audit
from bettermemory.admission import Admission
from bettermemory.in_force import InForce, in_force
from bettermemory.membership import PROMPT_RECALL
from bettermemory.models import LinkType, Memory, MemoryLink
from bettermemory.recall import RECALL_MAX_POINTERS, summarize

from .test_recall_brief import _World as _BriefWorld
from .test_recall_prompt import BODY, PromptWorld


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def _sup(target: Memory) -> list[dict[str, str]]:
    return [{"type": "supersedes", "target_id": target.id}]


def _ext(target: Memory) -> list[dict[str, str]]:
    return [{"type": "extends", "target_id": target.id}]


def _forced(world: PromptWorld, *records: Memory) -> dict[str, InForce]:
    admission = Admission.of(PROMPT_RECALL, world.resolve(world.worktree))
    return in_force(world.store, [r.id for r in records], admission)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def test_a_record_nothing_links_to_is_in_force(world: PromptWorld) -> None:
    alone = world.write("the cache lives in redis")
    assert _forced(world, alone) == {alone.id: InForce(alone.id, alone.id)}


def test_a_superseded_record_yields_to_its_head(world: PromptWorld) -> None:
    old = world.write("the cache lives in redis")
    mid = world.write("the cache moved to memcached", links=_sup(old))
    new = world.write("the cache moved again, to valkey", links=_sup(mid))
    forced = _forced(world, old, mid, new)
    assert forced[old.id] == InForce(old.id, new.id, replaces=old.id)
    assert forced[mid.id] == InForce(mid.id, new.id, replaces=mid.id)
    assert forced[new.id] == InForce(new.id, new.id)
    assert forced[old.id].history and not forced[new.id].history


def test_of_two_superseders_the_newest_is_followed(world: PromptWorld) -> None:
    old = world.write("the cache lives in redis")
    first = world.write("the cache moved to memcached", links=_sup(old))
    second = world.write("the cache moved to valkey", links=_sup(old))
    assert first.id < second.id
    assert _forced(world, old)[old.id].id == second.id


def test_a_cycle_ends_the_walk(world: PromptWorld) -> None:
    """Two records superseding each other: each walk stops where it would
    come back, so each yields once and the walk ends."""
    a = world.write("the cache lives in redis")
    b = world.write("the cache moved to memcached", links=_sup(a))
    link = MemoryLink(type=LinkType.SUPERSEDES, target_id=b.id)
    world.store.put_memory(a.model_copy(update={"links": [link]}))
    forced = _forced(world, a, b)
    assert (forced[a.id].id, forced[b.id].id) == (b.id, a.id)


def test_a_link_from_a_record_not_admitted_changes_nothing(
    world: PromptWorld,
) -> None:
    """The superseder sits in another project: the caller is never shown
    it, so the record stays in force and no line names it."""
    old = world.write("the cache lives in redis")
    world.write(
        "the cache moved to memcached",
        home=world.other_home("bar"),
        links=_sup(old),
    )
    world.write(
        "the cache is extended elsewhere", home=world.other_home("bar"), links=_ext(old)
    )
    assert _forced(world, old)[old.id] == InForce(old.id, old.id)


def test_the_newest_in_force_extender_is_named(world: PromptWorld) -> None:
    base = world.write("the deploy runs on fridays")
    older = world.write("the deploy also needs a changelog", links=_ext(base))
    newer = world.write("the deploy also pings the channel", links=_ext(base))
    assert _forced(world, base)[base.id].extended_by == newer.id
    # An extender that is itself history is named by its head.
    head = world.write("the deploy pings the incident channel", links=_sup(newer))
    assert _forced(world, base)[base.id].extended_by == head.id
    assert _forced(world, older)[older.id].extended_by is None


# ---------------------------------------------------------------------------
# The prompt recall
# ---------------------------------------------------------------------------


def _suppressed(world: PromptWorld) -> dict[str, str]:
    (event,) = world.recalls()
    return {entry["id"]: entry["reason"] for entry in event["suppressed_ids"]}


def test_a_history_hit_gives_its_slot_to_its_head(world: PromptWorld) -> None:
    old = world.write()
    new = world.write("the offsite copy job now runs nightly", links=_sup(old))
    result = world.recall()
    assert result.ids == (new.id,)
    (line,) = result.stdout.splitlines()[1:-1]
    assert line.startswith(f"- {new.id} [high] ")
    assert f"[replaces {old.id}]" in line
    # The query's snippet was the old record's text; the head has none.
    assert " — " not in line
    (event,) = world.recalls()
    assert event["shown_ids"] == [new.id]


def test_a_head_shown_in_its_own_slot_holds_the_history_slot(
    world: PromptWorld,
) -> None:
    old = world.write()
    new = world.write(f"{BODY} replaced", links=_sup(old))
    assert set(world.recall().ids) == {new.id}
    assert _suppressed(world) == {old.id: "superseded"}


def test_a_head_seen_in_the_segment_holds_the_history_slot(
    world: PromptWorld,
) -> None:
    old = world.write()
    new = world.write("the offsite copy job now runs nightly", links=_sup(old))
    world.store.record_event("show", session="transcript-recall", id=new.id)
    assert world.recall().stdout == ""
    assert world.recalls() == []


def test_a_head_is_decided_by_its_own_gates(world: PromptWorld) -> None:
    old = world.write()
    world.write(
        "the offsite copy job now runs nightly",
        provenance="synced",
        links=_sup(old),
    )
    assert world.recall().stdout == ""


def test_every_hit_has_a_slot() -> None:
    """A history hit whose head is a later hit holds its slot so the head
    keeps its own (and its snippet): that loses nothing only while the
    recall has a slot for every hit it is given."""
    assert audit._TOP_HITS_RETAINED <= RECALL_MAX_POINTERS


def test_a_recall_line_names_the_newest_extender(world: PromptWorld) -> None:
    base = world.write()
    extender = world.write("the copies are checked weekly", links=_ext(base))
    result = world.recall()
    assert result.ids == (base.id,)
    assert f"[extended by {extender.id}]" in result.stdout.splitlines()[1]


# ---------------------------------------------------------------------------
# The session brief
# ---------------------------------------------------------------------------


@pytest.fixture
def brief_world(tmp_path: Path) -> _BriefWorld:
    return _BriefWorld(tmp_path)


def test_the_brief_shows_no_history_row(brief_world: _BriefWorld) -> None:
    old = brief_world.fresh("the deploy window is tuesday mornings")
    new = brief_world.fresh(
        "the deploy window moved to wednesday mornings", links=_sup(old)
    )
    assert brief_world.brief().ids == (new.id,)


def test_a_brief_line_names_the_newest_extender(brief_world: _BriefWorld) -> None:
    base = brief_world.fresh("the deploy window is tuesday mornings")
    extender = brief_world.fresh(
        "the deploy window also needs a changelog entry", links=_ext(base)
    )
    result = brief_world.brief()
    assert result.ids == (extender.id, base.id)
    assert result.stdout.splitlines()[2] == (
        f"- {base.id} {summarize(base.body)} [extended by {extender.id}]"
    )


def test_the_brief_admits_a_global_record_naming_its_project(
    brief_world: _BriefWorld,
) -> None:
    """RS-F7 part 2 on the brief: a record written outside any project is
    briefed where its scopes name the caller's project, and nowhere else."""
    plain = brief_world.home_of(None)
    named = brief_world.store.write(
        content="prefer short release notes everywhere in repo",
        scopes=["projects:repo"],
        origin=None,
        home=plain,
    )
    loose = brief_world.store.write(
        content="prefer short release notes everywhere", scopes=["tools"], home=plain
    )
    brief_world.store.mark_verified(named.id)
    brief_world.store.mark_verified(loose.id)
    assert brief_world.brief().ids == (named.id,)
