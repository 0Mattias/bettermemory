"""ENG-279: annotation caps and event counts apply once per search hit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory.models import Confidence, MemoryHit
from bettermemory.store import Store

_NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _hit(mid: str) -> MemoryHit:
    return MemoryHit(
        id=mid,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        snippet="annotation target",
        score=1.0,
        relevance="high",
        created=_NOW,
        updated=_NOW,
    )


@pytest.mark.parametrize(
    "per_hit,total", [(0, 10), (1, 10), (3, 10), (3, 2), (6, 10), (3, 0)]
)
def test_link_annotations_share_one_cap_across_edge_types(
    memory_dir: Path,
    per_hit: int,
    total: int,
) -> None:
    with Store(memory_dir) as store:
        target = store.write(content="annotation target", scopes=["tools"])
        for index, kind in enumerate(["supersedes"] * 2 + ["contradicts"] * 3):
            store.write(
                content=f"linked target {index}",
                scopes=["tools"],
                links=[{"type": kind, "target_id": target.id}],
            )
        out: list[dict[str, Any]] = [{"id": target.id}]
        ResponseBuilder(stale_after_days=30).attach_link_annotations(
            out,
            [_hit(target.id)],
            store.load_all(),
            store=store,
            max_per_hit=per_hit,
            max_total=total,
        )
    emitted = [*out[0].get("superseded_by", []), *out[0].get("contradicts", [])]
    assert len(emitted) == min(per_hit, total, 5)
    assert all(entry["id"] != target.id for entry in emitted)


@pytest.mark.parametrize("outcome", ["ignored", "contradicted"])
@pytest.mark.parametrize("id_field", ["ids", "memory_ids"])
def test_one_event_counts_once_for_a_repeated_id_and_keeps_its_first_claim(
    outcome: str,
    id_field: str,
) -> None:
    mid = "annotation-target"
    events: list[dict[str, Any]] = [
        {
            "kind": "use",
            "ts": (_NOW - timedelta(days=days)).isoformat(),
            id_field: [{"malformed": True}, mid, mid],
            "outcome": outcome,
            "claim_excerpts": [
                "malformed claim",
                "the first valid claim",
                "duplicate claim",
            ],
        }
        for days in (2, 1)
    ]
    events.insert(
        1,
        {
            "kind": "use",
            "ts": (_NOW - timedelta(days=1, hours=1)).isoformat(),
            "ids": [mid],
            "outcome": "applied",
            "auto": True,
        },
    )
    builder = ResponseBuilder(stale_after_days=30)
    out: list[dict[str, Any]] = [{"id": mid}]
    builder.attach_recent_negative_outcomes(out, [_hit(mid)], events, now=_NOW)
    entry = out[0]["recent_negative_outcomes"][0]
    assert entry["outcome"] == outcome and entry["count_in_window"] == 2
    assert entry["claim_excerpt"] == "the first valid claim"
    # Distinct rejection events still count separately; a later explicit
    # endorsement clears them even if that event repeats the id too.
    events.append(
        {"kind": "use", "ts": _NOW.isoformat(), "ids": [mid, mid], "outcome": "applied"}
    )
    cleared: list[dict[str, Any]] = [{"id": mid}]
    builder.attach_recent_negative_outcomes(cleared, [_hit(mid)], events, now=_NOW)
    assert "recent_negative_outcomes" not in cleared[0]
