"""The disputed mark on recall pointers (`recall.disputed_marks`).

A memory is disputed when it is either member of a conflicts row whose
status is ``pending``, whatever the detector (polarity, numeric, value,
review, review-disagrees). Disputed rows are delivered with a mark naming
one pair, plus ``+n`` for the others, never held back; a dismissed pair
reads clean until a later scan resurrects it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory.conflicts import ConflictCandidate, ConflictQueue, _pair_id
from bettermemory.models import Memory, utcnow
from bettermemory.recall import disputed_marks

from .test_recall_brief import _World
from .test_recall_prompt import BODY, PromptWorld

DETECTORS = ("polarity", "numeric", "value", "review", "review-disagrees")


def _file(store: object, a: Memory, b: Memory, detector: str) -> str:
    queue = ConflictQueue(store)  # type: ignore[arg-type]
    pair_id = _pair_id(a.id, b.id)
    queue.file_pair(
        ConflictCandidate(
            id=pair_id,
            a_id=a.id,
            b_id=b.id,
            summary_a=a.body[:40],
            summary_b=b.body[:40],
            similarity=0.9,
            method="jaccard",
            detector=detector,
            created=utcnow().isoformat(),
        )
    )
    return pair_id


def test_the_mark_names_one_pair_and_counts_the_rest() -> None:
    marks = disputed_marks(
        [
            ("a", "b", "cf-bbbbbbbbbbbb"),
            ("a", "c", "cf-aaaaaaaaaaaa"),
            ("d", "a", "cf-cccccccccccc"),
        ]
    )
    assert marks["a"] == "[disputed: cf-aaaaaaaaaaaa +2]"
    assert marks["b"] == "[disputed: cf-bbbbbbbbbbbb]"
    assert marks["c"] == "[disputed: cf-aaaaaaaaaaaa]"
    assert "e" not in marks
    many = disputed_marks([("x", f"y{i}", f"cf-{i:012x}") for i in range(150)])
    assert many["x"] == "[disputed: cf-000000000000 +99]"
    assert max(len(mark) for mark in {**marks, **many}.values()) <= 31


@pytest.mark.parametrize("detector", DETECTORS)
def test_every_detector_marks_a_recall_pointer(tmp_path: Path, detector: str) -> None:
    world = PromptWorld(tmp_path)
    memory = world.write()
    other = world.write(f"{BODY} disagreeing", origin=None, scopes=["tools"])
    pair_id = _file(world.store, memory, other, detector)
    result = world.recall()
    assert result.ids == (memory.id,)
    assert f"[disputed: {pair_id}]" in result.stdout.splitlines()[1]


@pytest.mark.parametrize("detector", DETECTORS)
def test_every_detector_marks_a_brief_pointer(tmp_path: Path, detector: str) -> None:
    world = _World(tmp_path)
    memory = world.fresh("prefer short release notes in this project")
    other = world.store.write(content="prefer long release notes", scopes=["tools"])
    pair_id = _file(world.store, memory, other, detector)
    line = world.brief().stdout.splitlines()[1]
    assert line.endswith(f"[disputed: {pair_id}]")


def test_several_pairs_read_plus_n(tmp_path: Path) -> None:
    world = PromptWorld(tmp_path)
    memory = world.write()
    rivals = [
        world.write(
            f"rival {i} entirely unrelated words", origin=None, scopes=["tools"]
        )
        for i in range(3)
    ]
    pair_ids = sorted(_file(world.store, memory, r, "value") for r in rivals)
    line = world.recall().stdout.splitlines()[1]
    assert f"[disputed: {pair_ids[0]} +2]" in line


def test_dismissed_reads_clean_and_a_resurrection_marks_again(tmp_path: Path) -> None:
    world = PromptWorld(tmp_path)
    memory = world.write()
    other = world.write("backup policy rival note", origin=None, scopes=["tools"])
    pair_id = _file(world.store, memory, other, "polarity")
    queue = ConflictQueue(world.store)
    assert queue.resolve(
        pair_id,
        status="dismissed",
        member_bodies={memory.id: memory.body, other.id: other.body},
    )
    assert world.store.pending_conflict_pairs() == []
    assert "[disputed" not in world.recall(session_id="s1").stdout

    # The judged body changes; the next scan resurrects the pair.
    changed = world.store.update(
        world.store.load_one(other.id).model_copy(
            update={"body": "backup policy rival note, revised\n"}
        )
    )
    candidate = next(c for c in queue.load() if c.id == pair_id)
    counts = queue.upsert_scan(
        [candidate],
        {m.id: m for m in world.store.load_all()},
    )
    assert counts["resurrected"] == 1
    assert world.store.pending_conflict_pairs() == [(memory.id, changed.id, pair_id)]
    assert f"[disputed: {pair_id}]" in world.recall(session_id="s2").stdout


def test_the_store_query_reads_pending_rows_only(tmp_path: Path) -> None:
    world = PromptWorld(tmp_path)
    a, b, c = (world.write(f"note {i}") for i in range(3))
    first = _file(world.store, a, b, "numeric")
    second = _file(world.store, a, c, "review")
    ConflictQueue(world.store).resolve(second, status="confirmed")
    assert world.store.pending_conflict_pairs() == [(a.id, b.id, first)]
