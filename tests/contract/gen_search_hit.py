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

Nothing in ``search_hit.jsonl`` depends on the Python release, the
Unicode data or the platform.

``search_outer.jsonl`` (`generate_outer`, gated on the Unicode data and the
platform as ``search.jsonl`` is: it holds the ranker's scores and the
tokenizer's terms) pins `search.search` and `search.rank` around the
ranker over `_outer_memories` (the six above and six more with actors,
scopes and updated stamps that tie): `_filter_candidates` and
`candidate_admitted` with a stand-in admission, the browse branch,
`hydrate`, `matched_leg_out` and the hits built. The header is
``{"k": "header", "count": n, "now": iso, "pool": n, "hit_fields": [...],
"memories": [...]}``; each case:

- ``f``: ``"search"`` or ``"rank"``; ``q`` the query; ``kw`` the keyword
  arguments given (``excluded_scopes`` as a list the call reads as a set);
  ``only``: null for the header's first ``pool`` memories (every one but
  the last, whose ``updated`` is naive), else the memories ranked, by
  index (an index twice is a record the pool lists twice);
- ``adm``: null for no admission, else ``{"homes": {id: project},
  "admit": [[scopes, project], ...], "raise": ...}``: ``home`` answers a
  row naming the project or None, ``admits_row`` whether the pair is
  listed, and ``raise`` names the call that raises RuntimeError
  (``"prime"`` or a record id for its ``home``);
- ``legs``: null for no ``matched_leg_out``, else the dict passed in, as
  ``[id, leg]`` pairs in order; ``legs_o`` the dict after the call;
- ``hy``: null for no ``hydrate``, else ``{"swap": {id: memory index},
  "drop": n, "add": [memory index, ...]}``: each record swapped where
  listed, the last ``drop`` dropped, ``add`` appended;
- ``t``: the anchoring's table (as the ``"ca"`` cases), or null; ``rep``
  the stand-in drift report when ``path_drift`` is set;
- ``o``: the hits, each its fields' values in the model's order (the
  header's ``hit_fields``, as the ``"hit"`` cases spell them) with the
  score as its IEEE-754 bits; `rank`'s ``[id, match_terms]`` pairs; or
  ``{"error": class}``, an outcome row: the exception's class without its
  message (the stand-ins' RuntimeError, the bad mode's and the ranked
  path's ``zip(..., strict=True)`` ValueError, the browse's TypeError
  over a naive and an aware ``updated``);
- ``log``: every call of the stand-ins in order: ``["prime", ids]``,
  ``["home", id]``, ``["admits_row", scopes, project]``, ``["hydrate",
  ids]``, ``["anchors", ids]``, ``["for_record", id, origin_root]``,
  ``["detect", ...]`` (the ``"hit"`` cases' log entry).
"""

from __future__ import annotations

from datetime import timedelta, timezone
from types import SimpleNamespace
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


_NOW = _T0 + timedelta(days=10)
# The memories a case ranks when it names none: every one but the last,
# whose ``updated`` is naive (`_naive`).
_POOL = 12


def _outer_memories() -> list[m.Memory]:
    """`_memories` and six more: actors exact and in other cases, scopes
    to filter on, and two pairs of records whose ``updated`` tie (one pair
    at one instant spelled in two offsets)."""
    out = _memories()
    tied = _T0 + timedelta(hours=30)
    plus2 = timezone(timedelta(hours=2))
    specs: list[dict[str, Any]] = [
        {
            "body": "The removal of the old cache went fine.",
            "scopes": ["a", "b"],
            "actor": {"client": "claude-code", "model": "opus"},
        },
        {
            "body": "Rip the flag out of the parser body.",
            "scopes": ["b"],
            "actor": {"client": "Claude-Code"},
        },
        {"body": "A toggle to rip out and flag later.", "scopes": ["c"]},
        {
            "body": "Deploy notes: the staging-config rollout and its removal.",
            "scopes": ["a", "c"],
            "actor": {"client": "cursor", "model": "opus"},
            "updated": tied,
        },
        {
            "body": "Origin body of a record with the same updated stamp.",
            "scopes": ["d"],
            "actor": {"model": "opus"},
            "updated": tied,
        },
        {
            "body": "Cache removal toggle notes.",
            "actor": {"client": "claude-code", "model": "Opus"},
            "updated": (_T0 + timedelta(hours=12)).astimezone(plus2),
        },
    ]
    for j, spec in enumerate(specs):
        i = len(out)
        extra = {k: v for k, v in spec.items() if k not in ("body", "updated")}
        extra.setdefault("scopes", ["a"])
        out.append(
            m.Memory(
                id=_id(100 + i),
                created=_T0 + timedelta(hours=i),
                updated=spec.get("updated", _T0 + timedelta(hours=2 * i)),
                source="inferred",
                confidence=["high", "medium", "low"][j % 3],
                body=spec["body"],
                **extra,
            )
        )
    return out


def _naive(memory: m.Memory) -> m.Memory:
    """A record like `memory` under another id whose ``updated`` is naive:
    a browse that holds it beside an aware one compares the two."""
    fields = memory.model_dump()
    fields.update(id=_id(199), updated=memory.updated.replace(tzinfo=None))
    return m.Memory(**fields)


class _Admission:
    """The request's admission: each call logged, the homes and the
    admitted pairs from the case."""

    def __init__(self, spec: dict[str, Any], log: list[Any]) -> None:
        self.spec = spec
        self.log = log

    def prime(self, ids: Any) -> None:
        self.log.append(["prime", list(ids)])
        if self.spec.get("raise") == "prime":
            raise RuntimeError("prime")

    def home(self, record_id: str) -> Any:
        self.log.append(["home", record_id])
        if self.spec.get("raise") == record_id:
            raise RuntimeError("home")
        project = self.spec["homes"].get(record_id)
        return None if project is None else SimpleNamespace(recorded_project=project)

    def admits_row(self, scopes: Any, recorded: str | None) -> bool:
        self.log.append(["admits_row", list(scopes), recorded])
        return [list(scopes), recorded] in self.spec["admit"]


class _OuterAnchoring(_Anchoring):
    def prime(self, ids: list[str]) -> None:
        self.log.append(["anchors", list(ids)])


def _hydrate(spec: dict[str, Any], memories: list[m.Memory], log: list[Any]) -> Any:
    def hydrate(records: Any) -> list[m.Memory]:
        log.append(["hydrate", [r.id for r in records]])
        swap = spec.get("swap", {})
        out = [memories[swap[r.id]] if r.id in swap else r for r in records]
        out = out[: len(out) - spec.get("drop", 0)]
        return out + [memories[i] for i in spec.get("add", [])]

    return hydrate


def _compact(hit: m.MemoryHit) -> list[Any]:
    """A hit's fields' values in the model's order (`_spell` without the
    names, which the header lists), the score as its bits."""
    return [v["bits"] if k == "score" else v for k, v in _spell(hit)]


def _outer_case(
    memories: list[m.Memory],
    f: str,
    q: str,
    kw: dict[str, Any] | None = None,
    *,
    adm: dict[str, Any] | None = None,
    legs: list[list[str]] | None = None,
    hy: dict[str, Any] | None = None,
    t: dict[str, Any] | None = None,
    rep: dict[str, Any] | None = None,
    only: list[int] | None = None,
) -> dict[str, Any]:
    kw = kw or {}
    ranked = memories[:_POOL] if only is None else [memories[i] for i in only]
    log: list[Any] = []
    args: dict[str, Any] = {k: v for k, v in kw.items() if k != "excluded_scopes"}
    if "excluded_scopes" in kw:
        args["excluded_scopes"] = set(kw["excluded_scopes"])
    if adm is not None:
        args["admission"] = _Admission(adm, log)
    out_legs = None if legs is None else dict(legs)
    if f == "search":
        args["path_drift"] = kw.get("path_drift", False)
        if out_legs is not None:
            args["matched_leg_out"] = out_legs
        if hy is not None:
            args["hydrate"] = _hydrate(hy, memories, log)
        if t is not None:
            args["anchoring"] = _OuterAnchoring(t, log)

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
                "detect",
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
            k: (v if k == "anchor" else tuple(v)) for k, v in (rep or {}).items()
        }
        return va.PathDriftReport(**fields)

    o: Any
    try:
        with mock.patch.object(search, "detect_path_drift", detect):
            if f == "search":
                o = [_compact(h) for h in search.search(ranked, q, now=_NOW, **args)]
            else:
                o = [
                    [i, terms] for i, terms in search.rank(ranked, q, now=_NOW, **args)
                ]
    except (ValueError, RuntimeError, TypeError) as e:
        o = {"error": type(e).__name__}
    row: dict[str, Any] = {"f": f, "q": q, "kw": kw, "adm": adm, "legs": legs}
    row["only"] = only
    if f == "search":
        row.update({"hy": hy, "t": t, "rep": rep})
        row["legs_o"] = (
            None if out_legs is None else [[k, v] for k, v in out_legs.items()]
        )
    row.update({"o": o, "log": log})
    return row


def _outer_cases(ms: list[m.Memory]) -> list[dict[str, Any]]:
    ids = [mem.id for mem in ms]
    some = {"checked": ["p"], "missing": []}
    homes = {
        ids[0]: "p1",
        ids[1]: "p1",
        ids[4]: "p2",
        ids[6]: "p1",
        ids[7]: "p2",
        ids[9]: "global",
        ids[11]: "p1",
    }
    admit = [
        [["a"], "p1"],
        [["a", "b"], "p1"],
        [["a", "c"], "global"],
        [["c"], None],
    ]
    adm = {"homes": homes, "admit": admit}
    table = {
        ids[1]: ["/live", "home_checkout"],
        ids[2]: [None, "no_live_checkout"],
        ids[10]: ["/p", "project_checkout"],
    }
    browse = {"allow_empty_query": True, "max_results": 20}
    s, r = "search", "rank"
    rescue = {"rescue_expansion": True}
    c = _outer_case
    return [
        # The filters, alone and together.
        c(ms, s, "removal body"),
        c(ms, s, "removal body", {"scopes": ["b"]}),
        c(ms, s, "removal body", {"excluded_scopes": ["c"]}),
        c(ms, s, "removal body", {"scopes": ["a", "c"], "excluded_scopes": ["b"]}),
        c(ms, s, "removal body", {"scopes": [], "excluded_scopes": []}),
        c(ms, s, "removal toggle", {"client_filter": "claude-code"}),
        c(ms, s, "removal toggle", {"client_filter": "Claude-Code"}),
        c(ms, s, "removal body", {"model_filter": "opus"}),
        c(ms, s, "removal", {"client_filter": "claude-code", "model_filter": "opus"}),
        c(ms, s, "removal", {"client_filter": "nobody"}, adm=adm),
        c(ms, s, "removal body origin", adm=adm),
        c(ms, s, "removal body origin", {"excluded_scopes": ["b"]}, adm=adm),
        c(ms, s, "toggle flag", {"scopes": ["c"]}, adm=adm),
        c(ms, s, "", adm=adm),
        # Nothing admitted: [] before the anchoring is asked.
        c(ms, s, "removal", adm={"homes": {}, "admit": []}, t=table),
        c(ms, s, "removal", adm={**adm, "raise": ids[3]}),
        c(ms, s, "removal", adm={**adm, "raise": "prime"}),
        c(ms, s, "removal", adm=adm, only=[]),
        c(ms, s, "removal body", adm=adm, only=[6, 1, 6, 0]),
        # The IDF and the ranked list are the candidates'.
        c(ms, s, "removal toggle", {"mode": "bm25", "excluded_scopes": ["a"]}),
        c(ms, s, "removal toggle", {"mode": "bm25", "model_filter": "opus"}),
        c(ms, s, "removal toggle cache", {"mode": "bm25"}, adm=adm),
        c(ms, s, "removal body", {"mode": "keyword", "scopes": ["a"]}),
        # query_unique and the relevance count distinct tokens: a query
        # that repeats a token, and one that repeats a stem.
        c(ms, s, "removal removal body body", {"mode": "keyword", "max_results": 2}),
        c(ms, s, "removal removal body body", {"mode": "bm25", "max_results": 2}),
        c(ms, s, "toggle toggles toggled", {"max_results": 2}),
        # Browse.
        c(ms, s, "", browse),
        c(ms, s, "the", {**browse, "max_results": 3}),
        c(ms, s, "", {**browse, "scopes": ["a"], "max_results": 3}),
        c(ms, s, "", {**browse, "max_results": 4}, hy={"drop": 1}),
        c(
            ms,
            s,
            "",
            {**browse, "max_results": 2},
            hy={"add": [3], "swap": {ids[11]: 0}},
        ),
        c(ms, s, "", {**browse, "max_results": 3}, legs=[[ids[9], "stale"]]),
        c(ms, s, "", {**browse, "max_results": 0}, hy={"drop": 1}, t=table),
        c(ms, s, "", {**browse, "max_results": -9}),
        c(
            ms,
            s,
            "",
            {**browse, "scopes": ["d"], "path_drift": True},
            t=table,
            rep=some,
        ),
        c(ms, s, "", {"allow_empty_query": True}, adm=adm, legs=[]),
        c(ms, s, "", browse, only=[12, 1, 0]),
        c(ms, s, "", browse, only=[12]),
        # hydrate on the ranked path.
        c(ms, s, "body origin", hy={"swap": {ids[1]: 2}}, legs=[]),
        c(ms, s, "body origin", hy={"drop": 1}, legs=[]),
        c(ms, s, "body origin", hy={"add": [4]}, legs=[]),
        c(ms, s, "body origin", {"max_results": 0}, hy={"drop": 1}, legs=[], t=table),
        c(ms, s, "zebra quokka", hy={"add": [0]}, legs=[]),
        c(ms, s, "body origin", {"max_results": -2}, legs=[]),
        c(ms, s, "body origin", {"max_results": 1}, hy={}, legs=[]),
        # The legs each mode reports; the rescue leg joins only where the
        # base fusion's top hit covers under 0.6 of the query.
        c(ms, s, "removal toggle", {"mode": "keyword"}, legs=[]),
        c(ms, s, "removal toggle", {"mode": "bm25"}, legs=[]),
        c(ms, s, "removal toggle", legs=[]),
        c(ms, s, "removal toggle", rescue, legs=[]),
        c(ms, s, "removal toggle", {"scopes": ["b", "c"]}, legs=[]),
        c(ms, s, "removal toggle", {**rescue, "scopes": ["b", "c"]}, legs=[]),
        c(ms, s, "removal toggle", {**rescue, "scopes": ["b", "c"], "max_results": 1}),
        c(
            ms,
            s,
            "removal toggle",
            {**rescue, "scopes": ["b", "c"], "max_results": -3},
            legs=[],
        ),
        c(
            ms,
            s,
            "removal toggle",
            {**rescue, "scopes": ["b", "c"]},
            legs=[[ids[5], "x"], [ids[7], "stale"], [ids[8], "y"]],
        ),
        c(
            ms,
            s,
            "removal toggle",
            {**rescue, "mode": "bm25", "scopes": ["b", "c"]},
            legs=[],
        ),
        c(ms, s, "the", {**rescue, "scopes": ["b", "c"]}, legs=[]),
        c(ms, s, "removal toggle", {**rescue, "scopes": ["b"]}, legs=[]),
        # Errors, and the anchors and drift of ranked hits.
        c(ms, s, "removal", {"mode": "fuzzy"}, adm=adm, legs=[]),
        c(ms, s, "origin root", {"path_drift": True}, t=table, rep=some),
        # rank.
        c(ms, r, "removal body"),
        c(ms, r, "removal toggle", {"mode": "bm25", "excluded_scopes": ["a"]}),
        c(ms, r, "removal toggle", {"mode": "keyword"}, adm=adm),
        c(ms, r, "", adm=adm),
        c(ms, r, "the"),
        c(ms, r, "removal", adm={"homes": {}, "admit": []}),
        c(ms, r, "body origin removal toggle flag", {"mode": "keyword"}),
        c(ms, r, "removal toggle"),
        c(ms, r, "removal", {"mode": "fuzzy"}, adm=adm),
    ]


def generate_outer() -> dict[str, bytes]:
    memories = _outer_memories()
    memories.append(_naive(memories[0]))
    cases = _outer_cases(memories)
    header = {
        "k": "header",
        "count": len(cases),
        "now": _NOW.isoformat(),
        "pool": _POOL,
        "hit_fields": list(m.MemoryHit.model_fields),
        "memories": [mem.model_dump(mode="json") for mem in memories],
    }
    return {"search_outer.jsonl": jsonl([header, *cases])}
