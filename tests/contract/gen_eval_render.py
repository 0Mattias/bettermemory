"""Fixtures: the eval family's renderers, unit eval_render-1
(``eval_render.jsonl``, ``eval_render_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/eval_render.rs`` and ``src/eval.rs``.
This pins it to `eval_render.render_text` with `_text`,
`_review_text_lines`, `_rate_from_dict`, `_SURFACE_RATES`,
`_surface_text_lines`, `_rule_split_text_lines`, `_format_rate`, `_bar`,
`_truncate`, `_humanize_seconds` and `_short_ts`; `eval.EvalReport` and
its `to_dict`; and Python's `format(x, '0.2f')` and `format(x, 'g')`.

The first line is a header with the count of rows, the base review and
surfaces dicts (`pv`, below), `_SURFACE_RATES`, and the base texts the
rows edit: the lines of `render_text` of the empty report
(``report_lines``), of ``json.dumps(to_dict(), indent=1)`` of it
(``dict_lines``), and of the two sections over the base dicts
(``review_lines``, ``surfaces_lines``). Then one row per case, its kind
under ``k``. An answer is ``r`` (a text, `_common.text_field`), ``ops``
(the answer's lines as one edit of the base lines, `ops`), or ``x``, the
class of the exception the reference raises (messages are not compared):

- ``report``: ``in``, a report spec (below); `render_text` split at
  ``\n`` as ``ops`` of ``report_lines``, or ``x``; and ``dict``, the
  lines of ``json.dumps(to_dict(), indent=1)`` as ``ops`` of
  ``dict_lines``, or ``dict_sha256``, the SHA-256 of that text, when the
  edit passes 1,200 bytes (a review or surfaces dict passed through);
- ``review`` and ``surfaces``: ``in``, a dict spec (below), and the lines
  of `_review_text_lines` or `_surface_text_lines` as ``ops`` of the
  base's, or ``x``;
- ``rate``: ``label`` and ``rate``, the six fields of a `RateCI`, and
  ``r``, `_format_rate`;
- ``rate_dict``: ``label`` and ``in`` (`pv`), and ``r`` or ``x``,
  `_format_rate(label, _rate_from_dict(in))`;
- ``bar``, ``fixed``, ``general``: ``v``, a float as its ``repr``, and
  ``r``: `_bar(v)`, ``format(v, '0.2f')``, ``format(v, 'g')``;
- ``humanize``: ``v``, an int; ``short_ts``: ``v``; ``truncate``: ``v``
  and ``n``.

A report spec holds what differs from a report whose thirteen required
fields are zero, None and the empty text and the rest their defaults:
``gen``, ``start``, ``end`` (`isoformat` texts or null); ``ws``,
``scope``, ``rule``; ``n``, ints by field name; ``rates``, each rate by
field name as ``[numerator, denominator, rate, lower, upper,
torn_read]``; ``ratio`` (a float's ``repr``); ``suppressed``; ``cold``,
rows ``[id, scopes, summary, retrieval_count, auto, explicit]``;
``misses``, ``[ts, session_id, top_missed_id, top_missed_relevance,
threshold_rule, recent_retrieval_count]``; ``by_model``, ``[[model,
[[key, count], ...]], ...]``; ``review`` and ``surfaces``, dict specs. A
float in a spec is its ``repr``; a text is a JSON string, or
``{"cps": [...]}`` when it holds a surrogate. A dict spec is null (None),
``{"v": pv}``, or ``{"base": "review"|"surfaces", "with": [[path, pv],
[path]]}``: the header's dict with each key along ``path`` set
(an existing key keeps its place, a new one goes last) or, without a
value, removed.

The last line of ``eval_render.jsonl`` is a sweep: ``count`` reports
drawn by `draw_report` from ``seed``, the SHA-256 of each one's
`render_text` and ``json.dumps(to_dict())`` (UTF-8) in turn.

``eval_render.jsonl`` is not gated: its texts are ASCII but for the
renderer's own characters (U+2014, U+2500, U+2265, U+2026, U+00B7 and the
bar's U+2587 and U+2581), which every Unicode release since 3.2 calls
printable; float formatting is the exact binary value's, as every
release writes it.
``eval_render_unicode.jsonl`` holds every row whose `history.printable`
escape or code-point padding reads the Unicode database: ids, models,
summaries, scopes, keys and stamps past ASCII (combining, wide, astral,
format and unassigned code points, and a character Unicode 15.1
assigned).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from bettermemory import eval_render as er
from bettermemory.eval import EvalReport
from bettermemory.eval_model import (
    ColdEndorsementMemoriesRow,
    RateCI,
    SilentMissCandidate,
)

from ._common import has_surrogate, jsonl, text_field

_MASK = (1 << 64) - 1
I64_MAX = (1 << 63) - 1
I64_MIN = -(1 << 63)
SWEEP_SEED = 20261125
SWEEP_COUNT = 5000
GENERATED_AT = "2026-11-25T12:00:00.123456+00:00"
NAN = float("nan")
INF = float("inf")


class _Draw:
    """`nxt` steps x = x * 6364136223846793005 + 1442695040888963407 mod
    2**64 and gives x >> 33; `pick(n)` is `nxt() % n`."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def nxt(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & _MASK
        return self.x >> 33

    def pick(self, n: int) -> int:
        return self.nxt() % n


# ---------------------------------------------------------------------------
# the base dicts
# ---------------------------------------------------------------------------


def _rate_dict(k: int, n: int) -> dict[str, Any]:
    return RateCI.from_counts(k, n).to_dict()


def _tally(deliveries: int, k: int) -> dict[str, Any]:
    return {
        "deliveries": deliveries,
        "shown": deliveries * 3,
        "opened": k,
        "used": k // 2,
        "ignored": deliveries - k,
        "deliveries_helped": k // 3,
        "shown_opened_rate": _rate_dict(k, deliveries * 3),
        "shown_used_rate": _rate_dict(k // 2, deliveries * 3),
        "ignored_rate": _rate_dict(deliveries - k, deliveries),
        "delivery_helped_rate": _rate_dict(k // 3, deliveries),
    }


REVIEW: dict[str, Any] = {
    "chains": {
        "issued": 12,
        "answered": 9,
        "unanswered": 2,
        "pending": 1,
        "closed_by_call": 4,
    },
    "pre_answered": 3,
    "pre_answered_zero": {"total": 1, "by_model": {}},
    "response_chars_p50": 412,
    "calls_per_committed_write": 1.5,
    "actions": {"accept": 7, "revise": 2, "reject": 0},
    "filings_per_100_committed": {"accept": 77.8, "revise": None},
    "by_client": {
        "claude-code": {"issued": 10, "unanswered": 1, "pre_answered_zero": 1},
        "cursor": {"issued": 2, "unanswered": 1, "pre_answered_zero": 0},
    },
    "grades": {"n": 5, "right_both": 4, "pre_answered_n": 2},
}

SURFACES: dict[str, Any] = {
    "rules": ["rc3", "rc2"],
    "brief": _tally(8, 5),
    "recall": {**_tally(0, 0), "suppressed": {"cap": 2, "seen": 1}},
    "settlements": {"brief": 6, "recall": 0},
    "segments": {"sessions": 4, "opened": 3},
    "review_unanswered": {"turns": 2, "chains": 1},
    "grades": {
        "brief": {"graded": 5, "relevant_rate": _rate_dict(4, 5)},
        "recall": {"graded": 0, "relevant_rate": _rate_dict(0, 0)},
    },
    "by_rule": {
        "rc3": {"brief": _tally(2, 1), "recall": _tally(0, 0)},
        "earlier": {
            "rules": ["rc1", "rc2"],
            "brief": _tally(0, 0),
            "recall": _tally(0, 0),
        },
    },
    "membership_delta": {
        "rows": 9,
        "rows_differing": 2,
        "delivered_not_rc3": 1,
        "rc3_not_delivered": 1,
    },
    "note": "rates over the window's show rows",
}

BASES = {"review": REVIEW, "surfaces": SURFACES}
_ABSENT = object()


def _edited(base: str, edits: list[tuple[list[str], Any]]) -> Any:
    out = copy.deepcopy(BASES[base])
    for path, value in edits:
        at = out
        for key in path[:-1]:
            at = at[key]
        if value is _ABSENT:
            del at[path[-1]]
        else:
            at[path[-1]] = value
    return out


def _dict_spec(spec: Any) -> Any:
    """``(value, spec)``: a dict spec as written, and the value it names."""
    if spec is None:
        return None, None
    if isinstance(spec, tuple):
        base, edits = spec
        written = [[p] if v is _ABSENT else [p, pv(v)] for p, v in edits]
        return _edited(base, edits), {"base": base, "with": written}
    return spec, {"v": pv(spec)}


# ---------------------------------------------------------------------------
# report specs
# ---------------------------------------------------------------------------


def _text(value: str) -> Any:
    if has_surrogate(value):
        return {"cps": [ord(ch) for ch in value]}
    return value


def _opt_text(value: str | None) -> Any:
    return None if value is None else _text(value)


def _float(value: float | None) -> str | None:
    return None if value is None else repr(value)


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def build(spec: dict[str, Any]) -> tuple[EvalReport, dict[str, Any]]:
    """The report a spec names, and the spec as written."""
    ints = dict(spec.get("n", {}))
    required = {
        name: ints.pop(name, 0)
        for name in (
            "total_events_scanned",
            "events_in_window",
            "retrieval_occurrences",
            "explicit_endorsements_with_excerpt",
            "applied_total",
            "applied_explicit",
            "turns_audited",
            "turns_no_signal",
            "silent_misses",
        )
    }
    report = EvalReport(
        generated_at=datetime.fromisoformat(spec.get("gen", GENERATED_AT)),
        window_seconds=spec.get("ws"),
        scope_filter=spec.get("scope"),
        threshold_rule=spec.get("rule", ""),
        **required,
        **ints,
    )
    written: dict[str, Any] = {}
    if "gen" in spec:
        written["gen"] = spec["gen"]
    for key in ("ws", "suppressed", "start", "end"):
        if key in spec:
            written[key] = spec[key]
    if "scope" in spec:
        written["scope"] = _opt_text(spec["scope"])
    if "rule" in spec:
        written["rule"] = _text(spec["rule"])
    if "n" in spec:
        written["n"] = spec["n"]
    report.window_start = _dt(spec.get("start"))
    report.window_end = _dt(spec.get("end"))
    report.cold_endorsement_suppressed = spec.get("suppressed", False)
    if "rates" in spec:
        written["rates"] = {}
        for name, fields in spec["rates"].items():
            k, n, rate, lo, hi, torn = fields
            setattr(report, name, RateCI(k, n, rate, lo, hi, torn))
            written["rates"][name] = [k, n, _float(rate), _float(lo), _float(hi), torn]
    if "ratio" in spec:
        report.cold_endorsement_ratio_threshold = spec["ratio"]
        written["ratio"] = repr(spec["ratio"])
    if "cold" in spec:
        report.cold_endorsement_memories_rows = [
            ColdEndorsementMemoriesRow(
                id=i,
                scopes=list(s),
                summary=m,
                retrieval_count=r,
                auto_applied_count=a,
                explicit_applied_count=e,
            )
            for i, s, m, r, a, e in spec["cold"]
        ]
        written["cold"] = [
            [_text(i), [_text(x) for x in s], _text(m), r, a, e]
            for i, s, m, r, a, e in spec["cold"]
        ]
    if "misses" in spec:
        report.silent_miss_recent = [
            SilentMissCandidate(
                ts=t,
                session_id=s,
                top_missed_id=top,
                top_missed_relevance=rel,
                threshold_rule=rule,
                recent_retrieval_count=c,
            )
            for t, s, top, rel, rule, c in spec["misses"]
        ]
        written["misses"] = [
            [_text(t), _text(s), _opt_text(top), _opt_text(rel), _opt_text(rule), c]
            for t, s, top, rel, rule, c in spec["misses"]
        ]
    if "by_model" in spec:
        report.by_model = {m: dict(c) for m, c in spec["by_model"]}
        written["by_model"] = [
            [_text(m), [[_text(k), v] for k, v in c]] for m, c in spec["by_model"]
        ]
    for key in ("review", "surfaces"):
        if key in spec:
            value, w = _dict_spec(spec[key])
            setattr(report, key, value)
            written[key] = w
    return report, written


def _answer(fn: Callable[[], str]) -> dict[str, Any]:
    try:
        return text_field("r", fn())
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"x": type(exc).__name__}


def ops(base: list[str], lines: list[str]) -> list[Any]:
    """``lines`` as edits of ``base``: each ``[keep, drop, inserted]``
    keeps that many lines of the base, drops the next ones and inserts
    the given lines; the rest of the base follows the last edit. One edit
    at most, between the longest common head and tail (no `difflib`, so
    the bytes cannot follow its release)."""
    if lines == base:
        return []
    head = 0
    while head < min(len(base), len(lines)) and base[head] == lines[head]:
        head += 1
    tail = 0
    while (
        tail < min(len(base), len(lines)) - head
        and base[len(base) - 1 - tail] == lines[len(lines) - 1 - tail]
    ):
        tail += 1
    return [
        [
            head,
            len(base) - head - tail,
            [_text(x) for x in lines[head : len(lines) - tail]],
        ]
    ]


def _ops_answer(fn: Callable[[], list[str]], base: list[str]) -> dict[str, Any]:
    try:
        lines = fn()
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"x": type(exc).__name__}
    return {"ops": ops(base, lines)}


def _dump(report: EvalReport) -> str:
    return json.dumps(report.to_dict(), indent=1)


def _empty() -> EvalReport:
    return build({})[0]


def _report_row(spec: dict[str, Any]) -> dict[str, Any]:
    report, written = build(spec)
    empty = _empty()
    base_text = er.render_text(empty).split("\n")
    return {
        "k": "report",
        "in": written,
        **_ops_answer(lambda: er.render_text(report).split("\n"), base_text),
        **_dict_answer(empty, report),
    }


def _dict_answer(empty: EvalReport, report: EvalReport) -> dict[str, Any]:
    """`to_dict` as ``json.dumps(..., indent=1)``: its lines as edits of
    the empty report's (``dict``), or, past 1,200 bytes of edits (a
    review or surfaces dict passed through), the SHA-256 of the text."""
    edits = ops(_dump(empty).split("\n"), _dump(report).split("\n"))
    if len(json.dumps(edits)) <= 1200:
        return {"dict": edits}
    return {"dict_sha256": hashlib.sha256(_dump(report).encode("utf-8")).hexdigest()}


def _base_lines(kind: str) -> list[str]:
    fn = er._review_text_lines if kind == "review" else er._surface_text_lines
    return fn(copy.deepcopy(BASES[kind]))


def _lines_row(kind: str, spec: Any) -> dict[str, Any]:
    value, written = _dict_spec(spec)
    fn = er._review_text_lines if kind == "review" else er._surface_text_lines
    return {
        "k": kind,
        "in": written,
        **_ops_answer(lambda: fn(value), _base_lines(kind)),
    }


# ---------------------------------------------------------------------------
# the rows of eval_render.jsonl
# ---------------------------------------------------------------------------

_COLD = [
    ("mem-0001", ["repo:a", "global"], "a summary", 120, 4, 0),
    ("mem-0002", [], "x" * 50, 31, 0, 0),
    ("mem-0003", ["s"], "y" * 51, 30, 2, 1),
    ("mem-0004", [""], "", 0, 0, 0),
]

_MISSES = [
    ("2026-11-25T12:34:56.789Z", "sess-0123456789abcdef", "mem-0001", "0.42", "v1", 3),
    ("2026-11-25", "short", None, None, None, 0),
    ("2026-11-25 12:34:56Z", "", "", "", "", 1),
    ("2026-11-25T1", "0123456789ab", "mem-x", None, "v2", 2),
]

_RATES = {
    "memory_helped_rate": [3, 8, 0.375, 0.125, 0.005, False],
    "endorsement_rate": [1, 2, 0.5, 0.015, 2.675, False],
    "silent_miss_rate": [0, 0, None, None, None, False],
}


def _reports() -> list[dict[str, Any]]:
    counts = {
        "events_in_window": 1234,
        "total_events_scanned": 9999,
        "retrieval_occurrences": 77,
        "explicit_endorsements_with_excerpt": 5,
        "applied_total": 40,
        "applied_explicit": 9,
        "applied_model": 4,
        "applied_hook": 5,
        "turns_audited": 31,
        "turns_no_signal": 2,
        "silent_misses": 3,
        "repeat_audits": 6,
    }
    specs: list[dict[str, Any]] = [
        {},
        {"ws": 2592000, "scope": "repo:example", "n": counts, "rule": "v1"},
        {
            "ws": 3600,
            "scope": "",
            "n": {"events_in_window": -3, "applied_total": 123456},
        },
        {"ws": 90},
        {"ws": -86400},
        {
            "ws": 0,
            "n": {
                "turns_no_signal": -1,
                "repeat_audits": I64_MAX,
                "turns_audited": I64_MIN,
            },
        },
        {"ws": 86400, "end": "2026-11-25T00:00:00+00:00"},
        {
            "ws": 600,
            "start": "2026-10-01T00:00:00+02:00",
            "end": "2026-11-25T23:59:59.999999+00:00",
            "scope": "s",
        },
        {"start": "2026-10-01T00:00:00+00:00:30", "end": "2026-11-25T00:00:00-05:00"},
        {"gen": "2026-11-25T12:00:00", "end": "2026-11-25T12:00:00"},
        {"rates": _RATES},
        {
            "rates": {
                "memory_helped_rate": [9, 4, 1.0, 0.5, 1.0, True],
                "endorsement_rate": [0, 1, 0.0, 0.0, 0.79, False],
                "silent_miss_rate": [1, 1, NAN, -INF, INF, False],
            }
        },
        {"rates": {"silent_miss_rate": [5, 3, 1.0, 0.4, 1.0, True]}},
        {"rates": {"endorsement_rate": [1, 3, 1 / 3, 0.25, None, False]}},
        {
            "by_model": [
                ("b", [("audited", 3), ("misses", 1), ("no_signal", 0)]),
                ("a", [("audited", 12345), ("misses", 123456)]),
                ("B", []),
                ("aa", [("no_signal", 7)]),
                ("_", [("audited", -1), ("other", 9)]),
                ("x" * 40, [("misses", 2)]),
                ("", [("audited", 1)]),
                ("a\x1bz", [("audited", 2)]),
            ]
        },
        {"cold": _COLD, "n": {"cold_endorsement_memories_total": 4}},
        {"cold": _COLD, "n": {"cold_endorsement_memories_total": 4}, "ratio": 0.25},
        {
            "cold": _COLD[:1],
            "n": {"cold_endorsement_memories_total": 1},
            "ratio": 1e-05,
        },
        {
            "cold": _COLD[:1],
            "n": {"cold_endorsement_memories_total": 1},
            "ratio": 1234567.0,
        },
        {"cold": _COLD[:1], "ratio": 0.1 + 0.2},
        {"cold": _COLD[:1], "ratio": NAN},
        {"cold": _COLD[:1], "ratio": -0.5},
        {"cold": _COLD[:1], "ratio": INF},
        {"cold": _COLD[:1], "ratio": 5e-324},
        {
            "cold": [(f"m{i:02d}", ["s"], f"row {i}", i, i, 0) for i in range(12)],
            "n": {
                "cold_endorsement_memories_total": 15,
                "endorsement_min_retrievals": 7,
            },
        },
        {"n": {"cold_endorsement_memories_total": 3}},
        {"n": {"cold_endorsement_memories_total": -2}},
        {"n": {"cold_endorsement_memories_total": 11}, "suppressed": True},
        {"suppressed": True},
        {"cold": _COLD[:2], "suppressed": True},
        {
            "cold": [("m", ["a", "b"], "s", 30, I64_MAX, I64_MAX)],
            "ratio": 0.5,
        },
        {"misses": _MISSES},
        {"rule": "v2:relevance<0.3\n\x07"},
        {"review": ("review", [])},
        {"review": None, "surfaces": None},
        {"surfaces": ("surfaces", [])},
        {
            "ws": 604800,
            "scope": "repo:x",
            "n": counts,
            "rates": _RATES,
            "by_model": [("m1", [("audited", 3)])],
            "cold": _COLD[:2],
            "ratio": 0.25,
            "misses": _MISSES[:1],
            "rule": "v1",
            "review": ("review", []),
            "surfaces": ("surfaces", []),
        },
        {"review": ("review", [(["chains", "issued"], 0)]), "surfaces": {"rules": 1}},
        {"surfaces": ("surfaces", [(["note"], {"k": [1, 2.5, None, True]})])},
        {"rule": "ok", "review": ("review", [(["chains"], [])])},
    ]
    return [_report_row(s) for s in specs]


def _review_rows() -> list[dict[str, Any]]:
    big = 10**4301
    cases: list[Any] = [
        ("review", []),
        None,
        {},
        [],
        "",
        0,
        [1],
        "x",
        {"chains": {"issued": 0}},
        {"chains": {"issued": ""}},
        {"chains": {"issued": None}},
        {"chains": {"issued": []}},
        {"chains": {"issued": 0.0}},
        {"chains": {"issued": NAN}},
        {"chains": {}},
        {"chains": []},
        {"chains": "issued"},
        {"chains": None},
        {"x": 1},
        ("review", [(["chains", "pending"], _ABSENT)]),
        ("review", [(["chains", "issued"], True)]),
        ("review", [(["chains", "issued"], [0])]),
        ("review", [(["chains", "issued"], {"a": (1, 2)})]),
        ("review", [(["chains", "answered"], 2.5)]),
        ("review", [(["chains", "answered"], 1e16)]),
        ("review", [(["chains", "answered"], "nine")]),
        ("review", [(["chains", "answered"], None)]),
        ("review", [(["pre_answered"], _ABSENT)]),
        ("review", [(["pre_answered_zero"], 3)]),
        ("review", [(["pre_answered_zero"], {})]),
        (
            "review",
            [(["response_chars_p50"], None), (["calls_per_committed_write"], None)],
        ),
        ("review", [(["response_chars_p50"], 0.1 + 0.2)]),
        ("review", [(["actions"], {})]),
        ("review", [(["actions"], [("accept", 1)])]),
        ("review", [(["actions"], "accept")]),
        ("review", [(["actions"], None)]),
        ("review", [(["actions"], {3: 1, None: 2, 2.5: "x", True: [1]})]),
        ("review", [(["actions"], {"a\nb": 1, "c\x1b": 2})]),
        ("review", [(["actions"], {(1, "a"): 1})]),
        (
            "review",
            [(["filings_per_100_committed"], {"accept": 1e-05, "x": None, "y": 0})],
        ),
        ("review", [(["filings_per_100_committed"], 5)]),
        ("review", [(["by_client"], {})]),
        ("review", [(["by_client"], {"c": {"issued": 1}})]),
        ("review", [(["by_client"], {"c": []})]),
        (
            "review",
            [
                (
                    ["by_client"],
                    {"c": {"issued": 1, "unanswered": 0, "pre_answered_zero": None}},
                )
            ],
        ),
        ("review", [(["by_client"], [])]),
        ("review", [(["grades"], {"n": 0})]),
        ("review", [(["grades"], {"n": 0.0, "right_both": 1})]),
        ("review", [(["grades"], {"n": 1})]),
        (
            "review",
            [(["grades"], {"n": "x", "right_both": None, "pre_answered_n": -1})],
        ),
        ("review", [(["grades"], None)]),
        ("review", [(["grades"], _ABSENT)]),
        ("review", [(["chains", "issued"], big)]),
        ("review", [(["chains", "issued"], [big])]),
        ("review", [(["chains", "issued"], -(10**4299))]),
        (
            "review",
            [(["chains", "issued"], datetime(2026, 11, 25, 12, tzinfo=timezone.utc))],
        ),
        ("review", [(["chains", "issued"], PurePosixPath("/a/b"))]),
        ("review", [(["chains", "issued"], ("t",))]),
    ]
    return [_lines_row("review", c) for c in cases]


def _surface_rows() -> list[dict[str, Any]]:
    nested_rate = {**_rate_dict(1, 2)}
    cases: list[Any] = [
        ("surfaces", []),
        None,
        {},
        [],
        "x",
        ("surfaces", [(["rules"], [])]),
        ("surfaces", [(["rules"], "abc")]),
        ("surfaces", [(["rules"], ("a", "b"))]),
        ("surfaces", [(["rules"], {"k1": 1, "k2": 2})]),
        ("surfaces", [(["rules"], [1])]),
        ("surfaces", [(["rules"], None)]),
        ("surfaces", [(["rules"], ["", ""])]),
        ("surfaces", [(["rules"], ["a\x1b"])]),
        ("surfaces", [(["rules"], _ABSENT)]),
        ("surfaces", [(["brief"], _ABSENT)]),
        ("surfaces", [(["brief"], [])]),
        ("surfaces", [(["brief", "ignored_rate"], _ABSENT)]),
        ("surfaces", [(["brief", "ignored_rate", "torn_read"], _ABSENT)]),
        ("surfaces", [(["brief", "ignored_rate", "numerator"], _ABSENT)]),
        ("surfaces", [(["brief", "ignored_rate"], "r")]),
        ("surfaces", [(["brief", "shown"], _ABSENT)]),
        ("surfaces", [(["brief", "deliveries_helped"], _ABSENT)]),
        ("surfaces", [(["recall", "suppressed"], {})]),
        ("surfaces", [(["recall", "suppressed"], None)]),
        ("surfaces", [(["recall", "suppressed"], _ABSENT)]),
        ("surfaces", [(["recall", "suppressed"], [1])]),
        ("surfaces", [(["recall", "suppressed"], {1: None, "b\n": 2.0})]),
        ("surfaces", [(["settlements"], {})]),
        ("surfaces", [(["segments"], None)]),
        ("surfaces", [(["review_unanswered", "chains"], _ABSENT)]),
        ("surfaces", [(["grades"], {})]),
        ("surfaces", [(["grades"], [])]),
        ("surfaces", [(["grades"], {3: {"graded": 1, "relevant_rate": nested_rate}})]),
        ("surfaces", [(["grades"], {"g": {"graded": True}})]),
        ("surfaces", [(["grades"], {"g": {"graded": 0}})]),
        ("surfaces", [(["grades"], {"g": {}})]),
        ("surfaces", [(["grades"], {"g": {"graded": 1, "relevant_rate": {}}})]),
        ("surfaces", [(["by_rule"], _ABSENT)]),
        ("surfaces", [(["by_rule"], _ABSENT), (["membership_delta"], _ABSENT)]),
        ("surfaces", [(["by_rule"], [])]),
        ("surfaces", [(["by_rule"], None)]),
        ("surfaces", [(["by_rule"], {})]),
        ("surfaces", [(["by_rule"], {}), (["membership_delta"], [1])]),
        ("surfaces", [(["by_rule"], {}), (["membership_delta"], {})]),
        ("surfaces", [(["by_rule"], {}), (["membership_delta"], None)]),
        ("surfaces", [(["by_rule", "earlier", "rules"], [])]),
        ("surfaces", [(["by_rule", "earlier", "rules"], _ABSENT)]),
        ("surfaces", [(["by_rule", "earlier", "rules"], "xy")]),
        ("surfaces", [(["by_rule", "earlier", "rules"], [None])]),
        ("surfaces", [(["by_rule", "rc3", "rules"], [None])]),
        ("surfaces", [(["by_rule", "rc3", "recall"], _ABSENT)]),
        ("surfaces", [(["by_rule", "rc3"], [])]),
        ("surfaces", [(["by_rule", "rc3", "brief", "deliveries"], 0)]),
        ("surfaces", [(["by_rule", "rc3", "brief", "deliveries"], "")]),
        ("surfaces", [(["by_rule", "rc3", "recall", "deliveries"], 1)]),
        (
            "surfaces",
            [
                (["by_rule", "rc3", "recall", "deliveries"], 1),
                (["by_rule", "rc3", "recall", "ignored_rate"], _ABSENT),
            ],
        ),
        (
            "surfaces",
            [(["by_rule"], {5: {"brief": _tally(0, 0), "recall": _tally(0, 0)}})],
        ),
        (
            "surfaces",
            [(["by_rule"], {None: {"brief": _tally(0, 0), "recall": _tally(0, 0)}})],
        ),
        # A group other than `earlier` is spelled after `split[surface]` is
        # read: an int key past 4,300 digits over an empty split is KeyError
        # (TypeError over a list), never the ValueError of its `str()`. The
        # limit is the same on 3.11 to 3.14.
        ("surfaces", [(["by_rule"], {10**4300: {}})]),
        ("surfaces", [(["by_rule"], {10**4300: []})]),
        (
            "surfaces",
            [
                (
                    ["by_rule"],
                    {("a", 1): {"brief": _tally(0, 0), "recall": _tally(0, 0)}},
                )
            ],
        ),
        (
            "surfaces",
            [
                (
                    ["by_rule"],
                    {"earlier\x1b": {"brief": _tally(0, 0), "recall": _tally(0, 0)}},
                )
            ],
        ),
        ("surfaces", [(["membership_delta", "rows_differing"], _ABSENT)]),
        ("surfaces", [(["note"], _ABSENT)]),
        ("surfaces", [(["note"], None)]),
        ("surfaces", [(["note"], 1.0)]),
        ("surfaces", [(["note"], "a\nb")]),
    ]
    return [_lines_row("surfaces", c) for c in cases]


def _rate_rows() -> list[dict[str, Any]]:
    rates: list[tuple[str, list[Any]]] = [
        ("memory_helped_rate", [1, 8, 0.125, 0.125, 0.375, False]),
        ("x", [1, 200, 0.005, 0.015, 2.675, False]),
        ("x", [0, 0, None, None, None, False]),
        ("x", [0, 0, 0.5, None, 0.5, False]),
        ("x", [-1, I64_MIN, 0.5, 0.5, None, False]),
        ("x", [I64_MAX, 1, NAN, NAN, NAN, True]),
        ("x", [1, 1, INF, -INF, -0.0, False]),
        ("x", [1, 1, -1.0, -0.001, 0.995, False]),
        ("x", [1, 1, 2.0, 99.995, 1e300, False]),
        ("x", [1, 1, 0.25, 0.35, 0.45, False]),
        ("x", [1, 1, 0.35, 0.0, 1.0, False]),
        ("a label of more than twenty", [1, 2, 0.5, 0.2, 0.8, False]),
        ("", [1, 2, 0.05, 0.15, 0.65, False]),
    ]
    out = []
    for label, (k, n, rate, lo, hi, torn) in rates:
        out.append(
            {
                "k": "rate",
                "label": label,
                "rate": [k, n, _float(rate), _float(lo), _float(hi), torn],
                **_answer(
                    lambda: er._format_rate(label, RateCI(k, n, rate, lo, hi, torn))
                ),
            }
        )
    base = _rate_dict(1, 2)
    dicts: list[Any] = [
        base,
        {},
        [],
        "x",
        None,
        {**base, "rate": 1, "ci95_lower": True, "ci95_upper": 0},
        {**base, "rate": 0, "ci95_lower": False},
        {**base, "rate": -5, "ci95_lower": 10**300},
        {**base, "rate": 10**400},
        {**base, "ci95_lower": 10**400},
        {**base, "ci95_upper": -(10**400)},
        {**base, "rate": "0.5"},
        {**base, "ci95_lower": "0.5"},
        {**base, "rate": [0.5]},
        {**base, "ci95_upper": [0.5]},
        {**base, "ci95_lower": {"a": 1}},
        {**base, "ci95_lower": (1,)},
        {**base, "ci95_lower": datetime(2026, 1, 1)},
        {**base, "rate": datetime(2026, 1, 1)},
        {**base, "ci95_lower": PurePosixPath("/p")},
        {**base, "rate": PurePosixPath("/p")},
        {**base, "rate": None, "numerator": [1, {"a": None}], "denominator": 2.5},
        {**base, "rate": None, "numerator": 10**4301},
        {**base, "ci95_upper": None, "numerator": "k", "denominator": None},
        {**base, "torn_read": "anything"},
        {k: v for k, v in base.items() if k != "torn_read"},
        {k: v for k, v in base.items() if k != "ci95_upper"},
        {**base, "rate": NAN, "ci95_lower": -0.0},
        {**base, "rate": 0.25, "ci95_lower": 2**53 + 1},
    ]
    for d in dicts:
        out.append(
            {
                "k": "rate_dict",
                "label": "lbl",
                "in": pv(d),
                **_answer(lambda: er._format_rate("lbl", er._rate_from_dict(d))),
            }
        )
    return out


def pv(value: Any) -> Any:
    """A Python value as the fixtures spell it: JSON's own null, bool, int
    (within 64 bits), list and str (or ``{"cps": [...]}`` for a text with
    a surrogate); ``{"int": digits, "zeros": n}`` past 64 bits, the digits
    followed by ``n`` zeros (spelled without `str()`, which refuses more
    than 4,300 digits); ``{"f": repr}``;
    ``{"tuple": [...]}``; ``{"d": [[key, value], ...]}`` in insertion order;
    ``{"dt": isoformat}``; ``{"path": text}``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if I64_MIN <= value <= I64_MAX:
            return value
        chunks = []
        n = abs(value)
        while n:
            n, d = divmod(n, 10**100)
            chunks.append(d)
        top, *rest = reversed(chunks)
        digits = f"{top}" + "".join(f"{d:0100d}" for d in rest)
        body = digits.rstrip("0")
        return {
            "int": ("-" if value < 0 else "") + body,
            "zeros": len(digits) - len(body),
        }
    if isinstance(value, float):
        return {"f": repr(value)}
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        return [pv(v) for v in value]
    if isinstance(value, tuple):
        return {"tuple": [pv(v) for v in value]}
    if isinstance(value, dict):
        return {"d": [[pv(k), pv(v)] for k, v in value.items()]}
    if isinstance(value, datetime):
        return {"dt": value.isoformat()}
    if isinstance(value, PurePosixPath):
        return {"path": value.as_posix()}
    raise TypeError(f"no spelling for {type(value).__name__}")


_BARS = [
    0.0,
    0.25,
    0.35,
    0.45,
    0.65,
    0.05,
    0.15,
    0.95,
    0.04999999999999999,
    1.0,
    0.9999999999999999,
    -0.0,
    -1.0,
    2.0,
    NAN,
    INF,
    -INF,
    5e-324,
    0.5,
]
_FIXED = [
    0.125,
    0.375,
    0.625,
    0.875,
    0.005,
    0.015,
    0.025,
    2.675,
    0.995,
    9.995,
    99.995,
    -0.001,
    -0.0,
    0.0,
    NAN,
    INF,
    -INF,
    1e300,
    5e-324,
    1.0,
    0.5,
    1e22,
    123456789.125,
    -2.5,
    0.1 + 0.2,
    1.7976931348623157e308,
]
_GENERAL = [
    1e-05,
    0.0001,
    9.99999e-05,
    0.000123456789,
    123456.0,
    123456.5,
    1234567.0,
    1234565.0,
    1234575.0,
    999999.5,
    999999.4,
    100000.0,
    0.1 + 0.2,
    NAN,
    -0.0,
    0.0,
    1e16,
    1e-300,
    5e-324,
    1.7976931348623157e308,
    INF,
    -INF,
    0.5,
    -1.5,
    2.5e-05,
    0.25,
    1e21,
    1e22,
]
_SECONDS = [
    0,
    1,
    59,
    60,
    61,
    90,
    120,
    3600,
    3660,
    5400,
    7200,
    86400,
    90000,
    172800,
    2592000,
    -1,
    -60,
    -90,
    -3600,
    -86400,
    -86401,
    I64_MAX,
    I64_MIN,
    I64_MIN + 1,
    86400 * 10**12,
]
_SHORT_TS = [
    "",
    "T",
    "2026-11-25T12:3",
    "2026-11-25T12:34",
    "2026-11-25T12:34:56.789Z",
    "2026-11-25 12:34:56Z",
    "T123456789012345",
    "abcdefghijklmnoT",
    "2026-11-25TT12:34:56",
    "2026-11-25T1",
    "2026-11-25X12:34:56Z",
    "2026-11-25t12:34:56Z",
    "a" * 15 + "T",
]
_TRUNCATE = [
    ("", 50),
    ("a" * 49, 50),
    ("a" * 50, 50),
    ("a" * 51, 50),
    ("abc", 1),
    ("abc", 0),
    ("ab", 2),
    ("ab", 3),
]


def _scalar_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows += [{"k": "bar", "v": repr(v), **text_field("r", er._bar(v))} for v in _BARS]
    rows += [{"k": "fixed", "v": repr(v), "r": format(v, "0.2f")} for v in _FIXED]
    rows += [{"k": "general", "v": repr(v), "r": format(v, "g")} for v in _GENERAL]
    rows += [{"k": "humanize", "v": s, "r": er._humanize_seconds(s)} for s in _SECONDS]
    rows += [
        {"k": "short_ts", "v": _text(t), **text_field("r", er._short_ts(t))}
        for t in _SHORT_TS
    ]
    rows += [
        {"k": "truncate", "v": _text(s), "n": n, **text_field("r", er._truncate(s, n))}
        for s, n in _TRUNCATE
    ]
    return rows


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

WORDS = ["a", "repo:x", "global", "B", "zeta", "mem", "v1", "x y"]
MODELS = ["claude", "Claude", "gpt", "_", "a", "aa", "z9"]
COUNTS = [0, 1, 7, 99999, 100000, 123456, -3, I64_MAX, I64_MIN]
SECONDS = [0, 1, 59, 60, 90, 3600, 5400, 86400, 172800, -60, -86400, 2592000]
DATES = [
    "2026-10-01T00:00:00+00:00",
    "2026-11-25T12:30:45.123456+00:00",
    "2026-11-25T12:00:00+02:00",
]
RATIOS = [0.0, 0.0, 0.25, 1e-05, 0.1 + 0.2, 1234567.0, NAN, -0.5, INF]
STAMPS = ["2026-11-25T12:34:56Z", "2026-11-25", "x" * 20, "2026-11-25T1"]
SUMMARY = "abc " * 20
COUNT_KEYS = ["audited", "misses", "no_signal"]


def _draw_count(d: _Draw) -> int:
    if d.pick(2) == 0:
        return COUNTS[d.pick(len(COUNTS))]
    return d.pick(1000)


def _draw_text(d: _Draw) -> str | None:
    """None, the empty text or a word."""
    i = d.pick(3)
    return None if i == 0 else "" if i == 1 else WORDS[d.pick(len(WORDS))]


def draw_report(d: _Draw) -> EvalReport:
    """One report of the sweep; the native test draws it by the same steps."""
    report = EvalReport(
        generated_at=datetime.fromisoformat(GENERATED_AT),
        window_seconds=None,
        scope_filter=None,
        threshold_rule="",
        total_events_scanned=d.pick(1000),
        events_in_window=_draw_count(d),
        retrieval_occurrences=_draw_count(d),
        explicit_endorsements_with_excerpt=d.pick(1000),
        applied_total=_draw_count(d),
        applied_explicit=d.pick(1000),
        turns_audited=_draw_count(d),
        turns_no_signal=_draw_count(d),
        silent_misses=d.pick(1000),
    )
    w = d.pick(3)
    if w >= 1:
        report.window_seconds = SECONDS[d.pick(len(SECONDS))]
    if w == 2:
        report.window_end = datetime.fromisoformat(DATES[d.pick(len(DATES))])
        if d.pick(2) == 1:
            report.window_start = datetime.fromisoformat(DATES[d.pick(len(DATES))])
    report.scope_filter = _draw_text(d)
    report.threshold_rule = _draw_text(d) or ""
    report.repeat_audits = _draw_count(d)
    report.applied_model = d.pick(1000)
    report.applied_hook = d.pick(1000)
    for name in ("memory_helped_rate", "endorsement_rate", "silent_miss_rate"):
        if d.pick(4) != 0:
            setattr(report, name, RateCI.from_counts(d.pick(12), d.pick(12)))
    rows = []
    for _ in range(d.pick(13)):
        rows.append(
            ColdEndorsementMemoriesRow(
                id=f"m{d.pick(100000)}",
                scopes=[WORDS[d.pick(len(WORDS))] for _ in range(d.pick(3))],
                summary=SUMMARY[: d.pick(60)],
                retrieval_count=d.pick(500),
                auto_applied_count=d.pick(50),
                explicit_applied_count=d.pick(50),
            )
        )
    report.cold_endorsement_memories_rows = rows
    t = d.pick(3)
    report.cold_endorsement_memories_total = (
        len(rows) if t == 0 else 0 if t == 1 else len(rows) + d.pick(30)
    )
    if d.pick(2) == 1:
        report.endorsement_min_retrievals = d.pick(100)
    report.cold_endorsement_ratio_threshold = RATIOS[d.pick(len(RATIOS))]
    report.cold_endorsement_suppressed = d.pick(2) == 1
    misses = []
    for _ in range(d.pick(4)):
        misses.append(
            SilentMissCandidate(
                ts=STAMPS[d.pick(len(STAMPS))],
                session_id=f"sess-{d.pick(10**9)}",
                top_missed_id=_draw_text(d),
                top_missed_relevance=_draw_text(d),
                threshold_rule=_draw_text(d),
                recent_retrieval_count=d.pick(10),
            )
        )
    report.silent_miss_recent = misses
    start = d.pick(len(MODELS))
    by_model: dict[str, dict[str, int]] = {}
    for i in range(d.pick(4)):
        counts = {}
        for key in COUNT_KEYS:
            if d.pick(3) != 0:
                counts[key] = d.pick(1000)
        by_model[MODELS[(start + i) % len(MODELS)]] = counts
    report.by_model = by_model
    if d.pick(4) == 0:
        report.review = copy.deepcopy(REVIEW)
    if d.pick(4) == 0:
        report.surfaces = copy.deepcopy(SURFACES)
    return report


def sweep_digest() -> str:
    d = _Draw(SWEEP_SEED)
    digest = hashlib.sha256()
    for _ in range(SWEEP_COUNT):
        report = draw_report(d)
        digest.update(er.render_text(report).encode("utf-8"))
        digest.update(json.dumps(report.to_dict()).encode("utf-8"))
    return digest.hexdigest()


def _header(count: int) -> dict[str, Any]:
    empty = _empty()
    return {
        "fixture": "eval_render",
        "count": count,
        "generated_at": GENERATED_AT,
        "review": pv(REVIEW),
        "surfaces": pv(SURFACES),
        "surface_rates": list(er._SURFACE_RATES),
        "report_lines": [_text(x) for x in er.render_text(empty).split("\n")],
        "dict_lines": _dump(empty).split("\n"),
        "review_lines": [_text(x) for x in _base_lines("review")],
        "surfaces_lines": [_text(x) for x in _base_lines("surfaces")],
    }


def generate() -> dict[str, bytes]:
    rows = [
        *_reports(),
        *_review_rows(),
        *_surface_rows(),
        *_rate_rows(),
        *_scalar_rows(),
    ]
    sweep = {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "sha256": sweep_digest(),
    }
    return {"eval_render.jsonl": jsonl([_header(len(rows)), *rows, sweep])}


# ---------------------------------------------------------------------------
# past ASCII: eval_render_unicode.jsonl
# ---------------------------------------------------------------------------

# Combining acute, a wide CJK pair, an astral emoji, a zero-width joiner
# (a format character `printable` escapes), an unassigned code point,
# U+2FFC (assigned by Unicode 15.1), a lone surrogate, a fullwidth tilde.
_ODD = [
    "e\u0301",
    "\u8a18\u61b6",
    "\U0001f600",
    "a\u200db",
    "\U000e0080",
    "\u2ffc",
    "\ud800",
    "\uff5e",
    "\u00e9",
    "\u0378",
]


def _unicode_reports() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {"scope": "r\u00e9po", "rule": "r\u00e8gle\u200b", "ws": 60},
        {
            "by_model": [
                (m, [("audited", i)]) for i, m in enumerate([*_ODD, "z", "Z", "\x1b"])
            ]
        },
        {
            "cold": [(m, [m, "s"], m * 30, 31, 1, 0) for m in _ODD],
            "n": {"cold_endorsement_memories_total": len(_ODD)},
            "ratio": 0.25,
        },
        {"cold": [("id", [], "\u00e9" * 49 + "\U0001f600" * 3, 30, 0, 0)]},
        {"cold": [("id", ["\u8a18\u61b6" * 5], "s", 30, 0, 0)]},
        {
            "misses": [
                ("2026-11-25T12:34\u00e9", "\u00e9" * 20, "\u8a18", "\ud800", None, 1),
                ("\u00e9" * 15 + "T", "\U0001f600" * 13, None, "e\u0301", None, 2),
                (
                    "\u00e9\u00e9\u00e9T\U0001f600\U0001f600\U0001f600\U0001f600\U0001f600\U0001f600x"
                    * 2,
                    "s",
                    "a\u200db",
                    None,
                    None,
                    3,
                ),
            ]
        },
        {"surfaces": ("surfaces", [(["rules"], _ODD), (["note"], "\u00e9\u2ffc")])},
        {"review": ("review", [(["actions"], {m: 1 for m in _ODD})])},
    ]
    return [_report_row(s) for s in specs]


def _unicode_lines() -> list[dict[str, Any]]:
    cases = [
        (
            "review",
            [
                (
                    ["by_client"],
                    {
                        m: {"issued": 1, "unanswered": 0, "pre_answered_zero": 0}
                        for m in _ODD
                    },
                )
            ],
        ),
        (
            "review",
            [(["filings_per_100_committed"], {"\u00e9": "\u8a18", "\u200d": None})],
        ),
    ]
    out = [_lines_row("review", c) for c in cases]
    surface_cases = [
        ("surfaces", [(["recall", "suppressed"], {m: 1 for m in _ODD})]),
        (
            "surfaces",
            [
                (
                    ["grades"],
                    {
                        m: {"graded": 1, "relevant_rate": _rate_dict(1, 2)}
                        for m in _ODD[:4]
                    },
                )
            ],
        ),
        ("surfaces", [(["by_rule", "earlier", "rules"], _ODD)]),
    ]
    out += [_lines_row("surfaces", c) for c in surface_cases]
    out += [
        {
            "k": "rate",
            "label": _text(label),
            "rate": [1, 2, "0.5", "0.25", "0.75", False],
            **text_field("r", er._format_rate(label, RateCI(1, 2, 0.5, 0.25, 0.75))),
        }
        for label in ["\u00e9" * 19, "\u8a18" * 21, "e\u0301" * 10, "\U0001f600"]
    ]
    out += [
        {"k": "short_ts", "v": _text(t), **text_field("r", er._short_ts(t))}
        for t in [
            "\u00e9" * 15,
            "\u00e9" * 10 + "T" + "\U0001f600" * 6,
            "\U0001f600" * 16 + "T",
        ]
    ]
    out += [
        {"k": "truncate", "v": _text(s), "n": n, **text_field("r", er._truncate(s, n))}
        for s, n in [("\U0001f600" * 51, 50), ("\u00e9" * 50, 50), ("\ud800" * 51, 50)]
    ]
    return out


def generate_unicode() -> dict[str, bytes]:
    """What reads the Unicode database: `printable` and padding past ASCII."""
    rows = [*_unicode_reports(), *_unicode_lines()]
    header = {"fixture": "eval_render_unicode", "count": len(rows)}
    return {"eval_render_unicode.jsonl": jsonl([header, *rows])}
