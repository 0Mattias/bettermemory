"""Fixtures: the result models of `models` (``models_hits.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/records/summaries.rs``. This pins
`MemoryHit` with its defaults, and `MemorySummary` and
`TombstonedSummary` as `store_records.list_summaries` and
`list_tombstones` build them, over a few records, under several scope
filters.

The header holds the records: each memory's and tombstone's
``model_dump(mode="json")``, which the Rust test validates again. A row:

- ``{"k": "ls" | "lt", "s": <scopes or null>, "r": [summary, ...]}``:
  `list_summaries` / `list_tombstones` with the records of the header;
- ``{"k": "hit", "in": [id, scopes, confidence, snippet, score, created,
  updated], "r": hit}``: `MemoryHit` with the defaults.

A model is spelled by field in its own order, datetimes as
``isoformat()``, floats as `_common.neutral`, an actor as its six fields
(sources as pairs). No row depends on the platform or the Python release.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import models as m
from bettermemory import store_records

from ._common import jsonl, neutral

_T0 = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def _id(n: int) -> str:
    return f"01J{n:023d}"


def _memories() -> list[m.Memory]:
    bodies = [
        "Short body.",
        "First sentence here. Second sentence follows.",
        "  leading spaces\nand a newline in the first line  ",
        "Unicode été café — résumé naïve. Next.",
        "x" * 200,
        "e.g. an abbreviation starts this one. Then more.",
        "日本語の文。次の文。 " * 5,
    ]
    scopes = [["a"], ["a", "b"], ["b"], ["c"], ["a", "c"], ["z"], ["b", "z"]]
    out = []
    for i, (body, sc) in enumerate(zip(bodies, scopes, strict=True)):
        extra: dict[str, Any] = {}
        if i % 2:
            extra["last_verified_at"] = _T0 + timedelta(days=i)
        if i % 3 == 0:
            extra["category"] = ["fact", "ambient", "user-inference"][i // 3]
        if i in (1, 4):
            extra["actor"] = {
                "client": "cc",
                "model": "mé",
                "sources": {"client": "env"},
            }
        out.append(
            m.Memory(
                id=_id(i),
                created=_T0 + timedelta(hours=i),
                updated=_T0 + timedelta(hours=2 * i),
                scopes=sc,
                confidence=["high", "medium", "low"][i % 3],
                source="inferred",
                body=body,
                **extra,
            )
        )
    return out


def _tombstones(memories: list[m.Memory]) -> list[m.TombstonedMemory]:
    out = []
    for i, mem in enumerate(memories[:5]):
        out.append(
            m.TombstonedMemory(
                **{
                    k: v
                    for k, v in mem.model_dump().items()
                    if k in m.TombstonedMemory.model_fields
                },
                removed=_T0 + timedelta(days=10 + i),
                removed_reason=["obsolete", "dup é", "", "wrong", "x"][i],
                removed_session=None if i % 2 else f"sess-{i}",
            )
        )
    return out


def _dt(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


def _actor(a: Any) -> Any:
    if a is None:
        return None
    return [
        a.client,
        a.client_version,
        a.model,
        a.principal,
        a.session,
        [[k, v] for k, v in a.sources.items()],
    ]


def _value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, float):
        return neutral(v)
    if hasattr(v, "value"):
        return v.value
    return v


def _spell(model: Any) -> list[Any]:
    out = []
    for name in type(model).model_fields:
        v = getattr(model, name)
        out.append([name, _actor(v) if name == "actor" else _value(v)])
    return out


def _hits() -> list[dict[str, Any]]:
    rows = []
    inputs = [
        [_id(1), ["a"], "high", "snip", 0.0],
        [_id(2), ["a", "b"], "low", "", 12.3456],
        [_id(3), ["é"], "medium", "café \U0001f600", -1.5],
        [_id(4), [], "high", "x", 1e300],
    ]
    for i, (rid, sc, conf, snip, score) in enumerate(inputs):
        created = _T0 + timedelta(minutes=i)
        updated = _T0 + timedelta(minutes=2 * i)
        hit = m.MemoryHit(
            id=rid,
            scopes=sc,
            confidence=conf,
            snippet=snip,
            score=score,
            created=created,
            updated=updated,
        )
        rows.append(
            {
                "k": "hit",
                "in": [
                    rid,
                    sc,
                    conf,
                    snip,
                    neutral(score),
                    created.isoformat(),
                    updated.isoformat(),
                ],
                "r": _spell(hit),
            }
        )
    return rows


def generate() -> dict[str, bytes]:
    """``models_hits.jsonl``: not gated."""
    memories = _memories()
    tombstones = _tombstones(memories)
    fake = types.SimpleNamespace(
        iter_memories=lambda: list(memories), load_tombstones=lambda: list(tombstones)
    )
    cases: list[dict[str, Any]] = []
    for scopes in (None, [], ["a"], ["b", "z"], ["c", "c"], ["none"], ["A"]):
        ls = store_records.list_summaries(fake, scopes)  # type: ignore[arg-type]
        cases.append({"k": "ls", "s": scopes, "r": [_spell(s) for s in ls]})
        lt = store_records.list_tombstones(fake, scopes)  # type: ignore[arg-type]
        cases.append({"k": "lt", "s": scopes, "r": [_spell(s) for s in lt]})
    cases.extend(_hits())
    header = {
        "k": "header",
        "cases": len(cases),
        "memories": [x.model_dump(mode="json") for x in memories],
        "tombstones": [x.model_dump(mode="json") for x in tombstones],
    }
    return {"models_hits.jsonl": jsonl([header, *cases])}
