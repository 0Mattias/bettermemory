"""Fixtures: `ResponseBuilder`'s search and list serializers
(``response_hits.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/response_rows.rs``. This pins
`_response.ResponseBuilder.hit_to_dict`, `summary_to_dict`,
`tombstone_summary_to_dict` and `SIMILAR_HITS_SHOWN`.

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "shown": SIMILAR_HITS_SHOWN}``.
Every other line has ``k`` (``"hit"``, ``"summary"`` or ``"tomb"``),
``s`` the builder's ``stale_after_days``, ``now`` (absent for ``tomb``)
and ``i`` the value built: every datetime as its ``isoformat()`` text
(read back by ``fromisoformat``), ``None`` as null, a text that may hold a
surrogate through `text_field`, the enums by value, ``score`` as the hex
of its IEEE-754 bits, an actor as ``{client, model}`` or null. The answer
is ``o``, the text ``json.dumps`` writes for the returned dict with its
defaults (insertion order, ASCII escapes, ``NaN`` allowed), or ``e`` the
exception class raised.
"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import _response
from bettermemory.identity import Actor
from bettermemory.models import (
    Category,
    Confidence,
    MemoryHit,
    MemorySummary,
    TombstonedSummary,
)

from ._common import jsonl, text_field

UTC = timezone.utc
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
ID = "01J0000000000000000000000A"

_LISTS = (
    "scopes",
    "match_terms",
    "path_drift_checked_paths",
    "path_drift_missing_paths",
    "path_drift_verified_paths",
    "path_drift_expected_absent_paths",
    "path_drift_present_but_attested_absent",
    "path_drift_dropped_as_route_paths",
    "path_drift_claim_anchored_missing_paths",
)


def _bits(x: float) -> str:
    return struct.pack(">d", x).hex()


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def _answer(call: Any) -> dict[str, Any]:
    try:
        return {"o": json.dumps(call())}
    except TypeError as exc:
        return {"e": type(exc).__name__}


def _hit(s: int, now: datetime, **over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": ID,
        "scopes": ["global"],
        "confidence": Confidence.HIGH,
        "snippet": "a snippet",
        "score": 1.5,
        "relevance": "high",
        "match_terms": ["alpha"],
        "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "updated": datetime(2025, 6, 7, 8, 9, 10, 123456, tzinfo=UTC),
        "last_verified_at": None,
        "path_drift_checked": 0,
        "path_drift_missing": 0,
        "category": None,
        "query_unique": 0,
    }
    for name in _LISTS[2:]:
        fields[name] = []
    fields.update(over)
    hit = MemoryHit.model_construct(**fields)
    spelled: dict[str, Any] = {name: fields[name] for name in _LISTS}
    spelled.update(
        {
            "id": fields["id"],
            "confidence": fields["confidence"].value,
            "category": None
            if fields["category"] is None
            else fields["category"].value,
            "score": _bits(fields["score"]),
            "relevance": fields["relevance"],
            "created": _iso(fields["created"]),
            "updated": _iso(fields["updated"]),
            "last_verified_at": _iso(fields["last_verified_at"]),
            "path_drift_checked": fields["path_drift_checked"],
            "path_drift_missing": fields["path_drift_missing"],
            **text_field("snippet", fields["snippet"]),
        }
    )
    builder = _response.ResponseBuilder(stale_after_days=s)
    return {
        "k": "hit",
        "s": s,
        "now": now.isoformat(),
        "i": spelled,
        **_answer(lambda: builder.hit_to_dict(hit, now=now)),
    }


def _common_fields(over: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": ID,
        "scopes": ["global", "repo:x"],
        "confidence": Confidence.MEDIUM,
        "summary": "one line",
        "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "updated": datetime(2025, 1, 3, 3, 4, 5, tzinfo=UTC),
        "last_verified_at": None,
        "category": None,
    }
    fields.update(over)
    return fields


def _spell_common(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fields["id"],
        "scopes": fields["scopes"],
        "confidence": fields["confidence"].value,
        "category": None if fields["category"] is None else fields["category"].value,
        "created": _iso(fields["created"]),
        "updated": _iso(fields["updated"]),
        "last_verified_at": _iso(fields["last_verified_at"]),
        **text_field("summary", fields["summary"]),
    }


def _summary(
    s: int, now: datetime, actor: dict[str, Any] | None, **over: Any
) -> dict[str, Any]:
    fields = _common_fields(over)
    fields["actor"] = None if actor is None else Actor(**actor)
    summary = MemorySummary.model_construct(**fields)
    spelled = _spell_common(fields)
    spelled["actor"] = actor
    builder = _response.ResponseBuilder(stale_after_days=s)
    return {
        "k": "summary",
        "s": s,
        "now": now.isoformat(),
        "i": spelled,
        **_answer(lambda: builder.summary_to_dict(summary, now=now)),
    }


def _tomb(**over: Any) -> dict[str, Any]:
    removed = over.pop("removed", datetime(2026, 2, 1, tzinfo=UTC))
    reason = over.pop("removed_reason", "obsolete")
    session = over.pop("removed_session", None)
    fields = _common_fields(over)
    fields.update(
        {"removed": removed, "removed_reason": reason, "removed_session": session}
    )
    tomb = TombstonedSummary.model_construct(**fields)
    spelled = _spell_common(fields)
    spelled.update(
        {"removed": _iso(removed), "removed_reason": reason, "removed_session": session}
    )
    builder = _response.ResponseBuilder(stale_after_days=30)
    return {
        "k": "tomb",
        "s": 30,
        "i": spelled,
        **_answer(lambda: builder.tombstone_summary_to_dict(tomb)),
    }


def _hits() -> list[dict[str, Any]]:
    fresh = NOW - timedelta(days=2)
    stale = NOW - timedelta(days=90)
    naive_now = NOW.replace(tzinfo=None)
    rows = [
        # Never verified, no drift: fresh, no path_drift key.
        _hit(30, NOW),
        # Fresh stamp, prose-scraped missing only: recommended, three keys.
        _hit(
            30,
            NOW,
            last_verified_at=fresh,
            path_drift_checked=2,
            path_drift_missing=1,
            path_drift_checked_paths=["a.py", "b.py"],
            path_drift_missing_paths=["b.py"],
        ),
        # Stale stamp, every bucket set: required, every additive key.
        _hit(
            30,
            NOW,
            last_verified_at=stale,
            category=Category.FACT,
            path_drift_checked=3,
            path_drift_missing=2,
            path_drift_checked_paths=["a", "b", "c"],
            path_drift_missing_paths=["b", "c"],
            path_drift_verified_paths=["a"],
            path_drift_expected_absent_paths=["gone"],
            path_drift_present_but_attested_absent=["back"],
            path_drift_dropped_as_route_paths=["/api/x"],
            path_drift_claim_anchored_missing_paths=["c"],
        ),
        # Verified only: the block fires with nothing missing.
        _hit(30, NOW, path_drift_verified_paths=["a"], path_drift_checked_paths=["a"]),
        # Route-suppressed only: no block.
        _hit(30, NOW, path_drift_dropped_as_route_paths=["/v1/y"]),
        # Expected-absent only: block with that key.
        _hit(30, NOW, path_drift_expected_absent_paths=["old.txt"]),
        # Attested-absent present only, fresh: recommended, block.
        _hit(
            30,
            NOW,
            last_verified_at=fresh,
            path_drift_present_but_attested_absent=["back.txt"],
        ),
        # Same under never: required.
        _hit(30, NOW, path_drift_present_but_attested_absent=["back.txt"]),
        # Claim-anchored without missing (inconsistent input): no block,
        # yet the verdict reads the anchored count.
        _hit(30, NOW, path_drift_claim_anchored_missing_paths=["z"]),
        _hit(
            30,
            NOW,
            last_verified_at=fresh,
            path_drift_claim_anchored_missing_paths=["z"],
        ),
        # Counts disagreeing with the lists ship as given.
        _hit(30, NOW, path_drift_checked=-1, path_drift_missing=2**62),
        # Threshold edges.
        _hit(0, NOW, last_verified_at=NOW),
        _hit(0, NOW, last_verified_at=NOW - timedelta(days=1)),
        _hit(-5, NOW, last_verified_at=NOW - timedelta(days=1)),
        _hit(30, NOW, last_verified_at=NOW - timedelta(days=30)),
        _hit(30, NOW, last_verified_at=NOW - timedelta(days=30, microseconds=1)),
        _hit(30, NOW, last_verified_at=NOW + timedelta(days=3)),
        _hit(2**40, NOW, last_verified_at=datetime(1, 1, 1, tzinfo=UTC)),
        # Offsets: kept unless +00:00; naive stamps render bare.
        _hit(
            30,
            NOW,
            created=datetime(
                2025, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30))
            ),
            updated=datetime(2025, 1, 1, 0, 0, 0, 7),
            last_verified_at=datetime(
                2026, 2, 28, 23, 0, tzinfo=timezone(-timedelta(hours=8))
            ),
        ),
        _hit(30, NOW, last_verified_at=datetime(2026, 2, 27, 12, 0)),
        # A naive now against an aware stamp: TypeError.
        _hit(30, naive_now, last_verified_at=fresh),
        _hit(30, naive_now),
        # Scores json spells its own way.
        _hit(30, NOW, score=float("nan")),
        _hit(30, NOW, score=float("inf")),
        _hit(30, NOW, score=float("-inf")),
        _hit(30, NOW, score=-0.0),
        _hit(30, NOW, score=1e-7),
        _hit(30, NOW, score=1e16),
        _hit(30, NOW, score=0.1 + 0.2),
        # Text: non-ASCII, a lone surrogate, empty lists.
        _hit(
            30,
            NOW,
            snippet='café \U0001f600 "q" \n',
            scopes=[],
            match_terms=[],
            relevance="",
            category=Category.AMBIENT,
            confidence=Confidence.LOW,
        ),
        _hit(30, NOW, snippet="bad \udc80 byte", path_drift_missing_paths=["é.md"]),
    ]
    for cat in Category:
        rows.append(_hit(30, NOW, category=cat))
    return rows


def _summaries() -> list[dict[str, Any]]:
    fresh = NOW - timedelta(days=1)
    return [
        _summary(30, NOW, None),
        _summary(30, NOW, {"client": None, "model": None}),
        _summary(
            30, NOW, {"client": "claude-code", "model": None}, last_verified_at=fresh
        ),
        _summary(30, NOW, {"client": None, "model": "m-1"}, category=Category.FACT),
        _summary(30, NOW, {"client": "", "model": "m"}),
        _summary(1, NOW, None, last_verified_at=NOW - timedelta(days=5)),
        _summary(30, NOW.replace(tzinfo=None), None, last_verified_at=fresh),
        _summary(30, NOW, None, summary="é\udc81", confidence=Confidence.HIGH),
    ]


def _tombs() -> list[dict[str, Any]]:
    return [
        _tomb(),
        _tomb(removed_session="sess-1", last_verified_at=NOW, category=Category.FACT),
        _tomb(
            removed=datetime(2026, 2, 1),
            removed_reason="",
            summary="x\udc82",
            confidence=Confidence.LOW,
        ),
        _tomb(removed=datetime(2026, 2, 1, tzinfo=timezone(timedelta(hours=-1)))),
    ]


def generate() -> dict[str, bytes]:
    """``response_hits.jsonl``: not gated."""
    cases = [*_hits(), *_summaries(), *_tombs()]
    header = {"k": "header", "count": len(cases), "shown": _response.SIMILAR_HITS_SHOWN}
    return {"response_hits.jsonl": jsonl([header, *cases])}
