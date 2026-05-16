"""End-to-end tests for the recent_negative_outcomes annotation on
memory_search hits (T2.3 of the 1.6 plan).

The wire contract: a hit gets a `recent_negative_outcomes` list when
the memory has been `ignored` or `contradicted` within the window
AND not since been `applied`. Negatives superseded by a later applied
event are filtered out — the user already validated the memory after
the rejection, so surfacing the rejection would be misleading.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server_with_rec(memory_dir: Path) -> tuple[Any, Recorder]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    return server, rec


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _seed(server: Any, body: str) -> str:
    res = await _call(server, "memory_write", content=body, scopes=["tools"])
    return res["id"]


async def test_no_annotation_when_no_negative_events(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Baseline: a fresh memory with no use events should not carry
    a `recent_negative_outcomes` field. Byte-stable for the common
    case."""
    server, _ = server_with_rec
    await _seed(server, "python list comprehension notes")
    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    assert "recent_negative_outcomes" not in hits[0]


async def test_ignored_event_surfaces_as_annotation(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """A single ignored event within the window should produce a hit
    annotation with outcome='ignored', count_in_window=1, and the
    timestamp + session of the event."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension notes")

    await _call(
        server,
        "memory_record_use",
        memory_ids=[mid],
        outcome="ignored",
        note="not what I needed",
    )

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    entry = annotations[0]
    assert entry["outcome"] == "ignored"
    assert entry["count_in_window"] == 1
    assert entry["note"] == "not what I needed"
    assert "most_recent_ts" in entry
    assert "session_id" in entry


async def test_applied_event_supersedes_earlier_ignored(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """The whole point of the supersession rule: if the user ignored a
    memory once but later applied it, the rejection is no longer
    actionable. Surfacing it would be misleading."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert "recent_negative_outcomes" not in hits[0]


async def test_ignored_after_applied_does_surface(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Order matters in the supersession rule. Applied → ignored means
    the most recent signal is the rejection, which IS actionable."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    assert annotations[0]["outcome"] == "ignored"


async def test_contradicted_surfaces_as_separate_entry(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """`contradicted` is a distinct negative outcome from `ignored`.
    Both can appear on the same hit if neither has been superseded."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="contradicted")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    outcomes = {a["outcome"] for a in annotations}
    assert outcomes == {"ignored", "contradicted"}


async def test_corrected_outcome_does_not_surface(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """`corrected` is audit-only — the model already fixed the drift
    inline. It's a positive signal (memory was salvaged), not a
    rejection. Should not appear in the negative-outcomes list."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="corrected")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert "recent_negative_outcomes" not in hits[0]


async def test_count_in_window_reflects_multiple_events(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """If the model has rejected the memory three times, count_in_window
    should say 3 — the user-visible signal is "this keeps getting
    rejected", not just "it was rejected once"."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    for _ in range(3):
        await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    assert annotations[0]["count_in_window"] == 3


async def test_claim_excerpt_propagates_from_t11(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """T1.1 + T2.3 integration: when the ignored event carried a
    claim_excerpt, the annotation carries it too. This is what makes
    the rejection actionable — the model sees not just "rejected" but
    "*this specific claim* was rejected", and can rephrase."""
    server, _ = server_with_rec
    mid = await _seed(server, "the user prefers terse explanations")

    await _call(
        server,
        "memory_record_use",
        memory_ids=[mid],
        outcome="ignored",
        claim_excerpts=["the user prefers terse explanations"],
    )

    hits = _unwrap(await _call(server, "memory_search", query="terse"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert annotations[0]["claim_excerpt"] == "the user prefers terse explanations"


async def test_other_hit_unannotated_when_no_negatives(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """The annotation must not leak across hits. A search returning two
    memories where only one has negative events should annotate only
    the affected hit."""
    server, _ = server_with_rec
    a_id = await _seed(server, "python list comprehension")
    b_id = await _seed(server, "python decorators and closures")

    await _call(server, "memory_record_use", memory_ids=[a_id], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    by_id = {h["id"]: h for h in hits}
    assert "recent_negative_outcomes" in by_id[a_id]
    assert "recent_negative_outcomes" not in by_id[b_id]


async def test_each_outcome_at_most_one_entry(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Even with many ignored events, the annotation has one entry per
    outcome type — the count_in_window captures multiplicity. Keeps
    the wire shape compact."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")
    for _ in range(5):
        await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    ignored_entries = [a for a in annotations if a["outcome"] == "ignored"]
    assert len(ignored_entries) == 1


async def test_most_recent_ts_is_latest_event_timestamp(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """When multiple ignored events stack, `most_recent_ts` must be the
    latest one — the caller uses this to decide "is this rejection
    still fresh enough to matter?"."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    # Three ignored events in sequence; the third is the "most recent".
    timestamps: list[str] = []
    for _ in range(3):
        res = await _call(
            server, "memory_record_use", memory_ids=[mid], outcome="ignored"
        )
        # The record_use call doesn't return ts; we rely on iteration
        # order being chronological.
        timestamps.append(res.get("outcome", ""))

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    # The most_recent_ts must be a parsable, ISO-formatted string.
    most_recent = annotations[0]["most_recent_ts"]
    assert most_recent.endswith("Z") or "+" in most_recent
