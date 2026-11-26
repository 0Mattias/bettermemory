"""Fixtures: the hit one scored record makes and a page's claim anchors
(``search_hit.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/search/hit.rs``. This pins
`search`'s `_build_hit` and `_claim_anchors`, and through them
`_query_biased_snippet`, `_relevance_label`, `round(score, 4)` and
`attestation_paths.record_paths`.

`detect_path_drift` is a stand-in that logs its call and answers the
row's report, so no row reads the disk: the drift read itself is pinned
by ``verify_anchoring_detect.jsonl``. An `Anchoring` is a stand-in that
logs ``prime`` and ``for_record`` and answers each id's anchor from a
table.

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "memories": [...]}``, each memory
its ``model_dump(mode="json")``. Every other line is a case, ``f``
naming it:

- ``"hit"``: `_build_hit(memories[m], score, matched,
  query_unique=qu, anchor=, path_drift=pd)`; ``score`` as
  `_common.neutral`; ``a`` the anchor (null for None, else ``[root,
  rule]``, a `ClaimAnchor` with nothing else); ``rep`` the stand-in's
  report (its fields); ``o`` the hit, spelled by field in the model's
  order (datetimes as ``isoformat()``, floats as `_common.neutral`);
  ``log`` each call of the stand-in: ``[body, verified kind, verified,
  absent kind, absent, worktree_root, anchor]``, a kind the class name
  of the path sequence;
- ``"ca"``: `_claim_anchors(anchoring, [memories[i] for i in ms])`:
  ``t`` null for no anchoring, else the table id -> ``[root, rule]`` or
  null (no home row); ``o`` each answer (``[root, rule]`` or null);
  ``log`` ``["prime", ids]`` and ``["for_record", id, origin_root]``.

Nothing here depends on the Python release, the Unicode data or the
platform.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

from bettermemory import models as m
from bettermemory import search
from bettermemory import verify_anchoring as va

from ._common import jsonl, neutral
from .gen_models_hits import _T0, _id, _spell

_LONG = (
    "Opening words about nothing in particular, padding the body out. " * 4
    + "The deploy pipeline reads config from staging-config before it runs. "
    + "More filler follows to push the body past the snippet budget. " * 3
)


def _memories() -> list[m.Memory]:
    specs: list[dict[str, Any]] = [
        {
            "body": "Short body citing src/a.py.",
            "verified_paths": ["src/a.py"],
            "verified_absent_paths": ["gone.py"],
        },
        {
            "body": "Body with origin.",
            "origin": {"cwd": "/w/t", "worktree_root": "/w/t"},
            "verified_paths": ["src/a.py"],
            "verified_path_targets": {"src/a.py": "/w/t/src/a.py", "x": None, "z": "z"},
            "verified_absent_path_targets": {"y": "/w/t/y", "n": None},
        },
        {"body": "Origin without a root.", "origin": {"cwd": "/c"}, "category": "fact"},
        {
            "body": "Origin with an empty root.",
            "origin": {"worktree_root": ""},
            "verified_absent_paths": ["a", "b"],
        },
        {"body": _LONG, "scopes": ["a", "b"], "confidence": "low"},
        {"body": "  été café \U0001f600 body  ", "last_verified": True},
    ]
    out = []
    for i, spec in enumerate(specs):
        extra = {k: v for k, v in spec.items() if k not in ("body", "last_verified")}
        extra.setdefault("scopes", ["a"])
        extra.setdefault("confidence", ["high", "medium", "low"][i % 3])
        if spec.get("last_verified"):
            extra["last_verified_at"] = _T0 + timedelta(days=3)
        out.append(
            m.Memory(
                id=_id(100 + i),
                created=_T0 + timedelta(hours=i),
                updated=_T0 + timedelta(hours=2 * i),
                source="inferred",
                body=spec["body"],
                **extra,
            )
        )
    return out


_REPORT = {
    "checked": ["src/a.py", "b/c", "gone.py"],
    "missing": ["b/c"],
    "verified": ["src/a.py"],
    "expected_absent": ["gone.py"],
    "dropped_as_route": ["/api/v1"],
    "claim_anchored_missing": ["b/c"],
    "present_but_attested_absent": ["here.py"],
    "unconfirmed": ["u.py"],
    "unconfirmed_reasons": ["unanchored"],
    "anchor": "home_checkout",
}


def _anchor_desc(a: Any) -> Any:
    return None if a is None else [a.root, a.rule]


def _hit_case(
    memories: list[m.Memory],
    i: int,
    score: float,
    matched: list[str],
    qu: int,
    anchor: list[str | None] | None,
    pd: bool,
    rep: dict[str, Any],
) -> dict[str, Any]:
    log: list[Any] = []

    def detect(
        body: str,
        *,
        verified_paths: Any,
        absent_paths: Any,
        worktree_root: Any,
        anchor: Any,
    ) -> va.PathDriftReport:
        log.append(
            [
                body,
                type(verified_paths).__name__,
                list(verified_paths),
                type(absent_paths).__name__,
                list(absent_paths),
                worktree_root,
                _anchor_desc(anchor),
            ]
        )
        fields: dict[str, Any] = {
            k: (v if k == "anchor" else tuple(v)) for k, v in rep.items()
        }
        return va.PathDriftReport(**fields)

    claim = (
        None if anchor is None else va.ClaimAnchor(root=anchor[0], rule=anchor[1] or "")
    )
    with mock.patch.object(search, "detect_path_drift", detect):
        hit = search._build_hit(
            memories[i],
            score,
            list(matched),
            query_unique=qu,
            anchor=claim,
            path_drift=pd,
        )
    return {
        "f": "hit",
        "m": i,
        "score": neutral(score),
        "matched": matched,
        "qu": qu,
        "a": anchor,
        "pd": pd,
        "rep": rep,
        "o": _spell(hit),
        "log": log,
    }


def _hits(memories: list[m.Memory]) -> list[dict[str, Any]]:
    rows = []
    full = _REPORT
    some = {"checked": ["p"], "missing": []}
    for i in range(len(memories)):
        for pd in (True, False):
            rows.append(_hit_case(memories, i, 1.5, ["body"], 2, None, pd, full))
            rows.append(
                _hit_case(
                    memories, i, 1.5, ["body"], 2, ["/live", "home_checkout"], pd, some
                )
            )
    scores = [
        0.03125,
        -0.03125,
        0.09375,
        1.03125,
        0.12345,
        0.00005,
        0.00015,
        2.5,
        12345.678951,
        1e300,
        -1e300,
        5e-324,
        1e-5,
        0.0,
        -0.0,
        float("inf"),
        float("-inf"),
        float("nan"),
        0.1 + 0.2,
    ]
    for s in scores:
        rows.append(_hit_case(memories, 0, s, [], 0, None, False, {}))
    coverage = [
        ([], 0),
        (["a"], 0),
        (["a", "b", "c"], 4),
        (["a", "b"], 5),
        (["a"], 3),
        (["a", "b", "c"], 1),
        (["a", "b", "c"], 8),
        (["a", "b"], 3),
        (["a"], 1),
        ([], 4),
    ]
    for matched, qu in coverage:
        rows.append(_hit_case(memories, 2, 0.5, matched, qu, None, False, {}))
    for matched in (
        ["deploy"],
        ["staging-config"],
        ["config", "pipeline"],
        ["absent"],
        [],
    ):
        rows.append(_hit_case(memories, 4, 3.25, matched, 3, None, False, {}))
    rows.append(
        _hit_case(
            memories, 1, 0.5, ["origin"], 1, [None, "no_live_checkout"], True, some
        )
    )
    rows.append(
        _hit_case(
            memories, 3, 0.5, [], 1, None, True, {"checked": [], "missing": ["m"]}
        )
    )
    return rows


class _Anchoring:
    def __init__(
        self, table: dict[str, list[str | None] | None], log: list[Any]
    ) -> None:
        self.table = table
        self.log = log

    def prime(self, ids: list[str]) -> None:
        self.log.append(["prime", list(ids)])

    def for_record(
        self, record_id: str, *, origin_root: str | None
    ) -> va.ClaimAnchor | None:
        self.log.append(["for_record", record_id, origin_root])
        entry = self.table.get(record_id)
        if entry is None:
            return None
        return va.ClaimAnchor(root=entry[0], rule=entry[1] or "")


def _ca_case(
    memories: list[m.Memory], ms: list[int], table: dict[str, Any] | None
) -> dict[str, Any]:
    log: list[Any] = []
    anchoring = None if table is None else _Anchoring(table, log)
    got = search._claim_anchors(anchoring, [memories[i] for i in ms])  # type: ignore[arg-type]
    return {
        "f": "ca",
        "ms": ms,
        "t": table,
        "o": [_anchor_desc(a) for a in got],
        "log": log,
    }


def _cas(memories: list[m.Memory]) -> list[dict[str, Any]]:
    ids = [mem.id for mem in memories]
    table = {
        ids[0]: ["/live", "home_checkout"],
        ids[1]: [None, "no_live_checkout"],
        ids[3]: ["/p", "project_checkout"],
    }
    return [
        _ca_case(memories, [0, 1, 2], None),
        _ca_case(memories, [], None),
        _ca_case(memories, [], table),
        _ca_case(memories, [0, 1, 2, 3, 4, 5], table),
        _ca_case(memories, [3, 1, 3, 0], table),
        _ca_case(memories, [2, 4], {}),
    ]


def generate() -> dict[str, bytes]:
    memories = _memories()
    cases = _hits(memories) + _cas(memories)
    header = {
        "k": "header",
        "count": len(cases),
        "memories": [mem.model_dump(mode="json") for mem in memories],
    }
    return {"search_hit.jsonl": jsonl([header, *cases])}
