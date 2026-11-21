"""Fixtures: audit.py's report types and event field builders
(``audit_fields.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/audit``. This pins the first unit
of `audit`: the module constants, `MissHit.to_dict` and
`to_compact_dict`, `MissReport.is_miss` and `to_dict`,
`no_owner_prompt_report`, `turn_audited_fields`, `search_miss_fields`,
`recall_held_fields` and `prompt_recall_fields`, each `ValueError` by its
message.

One file, not gated, one JSON object per line, keys sorted, ASCII. Its
first line is the header (``{"k": "consts", ...}`` with the count of the
lines after it). A report is given as ``{"v", "at", "sid", "lb", "rrc",
"rule", "hits", "pq", "nsr", "sb"}``, a hit as ``{"id", "score"
(neutral), "rel", "scopes", "snip", "mu", "qu", "rel2"}``. Every other
line names its builder in ``k``, its report by index into the header's
``reports`` in ``ri``, its arguments in ``a`` and its answer in ``o``:
the fields as ``json.dumps`` spells them with its defaults (key order
kept, so the order is pinned), or ``{"err": message}``.
`generate_ulid` answers ``MINTED`` throughout, so no row holds a random
id. Every text a message quotes is ASCII, so no message depends on the
Unicode tables behind ``str.isprintable``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from bettermemory import audit

from ._common import jsonl, neutral

_UTC = timezone.utc
_AT = [
    datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC),
    datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_UTC),
    datetime(1999, 12, 31, 23, 59, 59, 1000, tzinfo=timezone(timedelta(hours=2))),
    datetime(2026, 6, 1, tzinfo=timezone(timedelta(seconds=30))),
]

_HITS = [
    audit.MissHit(
        id="m1", score=0.5, relevance="high", scopes=("global",), snippet="body"
    ),
    audit.MissHit(
        id="m2",
        score=1e-07,
        relevance="medium",
        scopes=(),
        snippet="café \U0001f600",
        matched_unique=2,
        query_unique=3,
        relevance_v2="high",
    ),
    audit.MissHit(
        id="",
        score=-0.0,
        relevance="",
        scopes=("project:x", "global", "a"),
        snippet="",
        matched_unique=-1,
        query_unique=10**15,
        relevance_v2="",
    ),
    audit.MissHit(
        id="m4", score=12345678901234567.0, relevance="low", scopes=("s",), snippet="x"
    ),
]


def _report(
    verdict: str = "miss",
    at: int = 0,
    hits: tuple[int, ...] = (0, 1),
    pq: str | None = "deploy the backup",
    nsr: str | None = None,
    sb: str | None = None,
    rrc: int = 0,
) -> audit.MissReport:
    return audit.MissReport(
        verdict=verdict,  # type: ignore[arg-type]
        checked_at=_AT[at],
        session_id="sess",
        lookback_seconds=600,
        recent_retrieval_count=rrc,
        threshold_rule=audit.THRESHOLD_RULE_V1,
        top_hits=tuple(_HITS[i] for i in hits),
        probe_query=pq,
        no_signal_reason=nsr,
        suppressed_by=sb,
    )


def _hit_spec(h: audit.MissHit) -> dict[str, Any]:
    return {
        "id": h.id,
        "score": neutral(h.score),
        "rel": h.relevance,
        "scopes": list(h.scopes),
        "snip": h.snippet,
        "mu": h.matched_unique,
        "qu": h.query_unique,
        "rel2": h.relevance_v2,
    }


def _spec(r: audit.MissReport) -> dict[str, Any]:
    return {
        "v": r.verdict,
        "at": r.checked_at.isoformat(),
        "sid": r.session_id,
        "lb": r.lookback_seconds,
        "rrc": r.recent_retrieval_count,
        "rule": r.threshold_rule,
        "hits": [_hit_spec(h) for h in r.top_hits],
        "pq": r.probe_query,
        "nsr": r.no_signal_reason,
        "sb": r.suppressed_by,
    }


_REPORTS = [
    _report(),
    _report("ok", 1, (), None, None, "retrieval", 2),
    _report("no_signal", 2, (2,), "", "no_owner_prompt"),
    _report("miss", 3, (0, 1, 2, 3), "q\nr"),
    _report("ok", 0, (3,), None, None, "project_cohort"),
    _report("no_signal", 0, (), None, "semantic_model_unavailable"),
    _report("no_signal", 0, (), "x", "bad'reason\x7f"),
]


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        with patch.object(audit, "generate_ulid", lambda: "MINTED"):
            return json.dumps(fn(*args, **kwargs))
    except ValueError as exc:
        return {"err": str(exc)}


def _few(r: audit.MissReport, variants: list[Any], i: int, j: int) -> list[Any]:
    """Every variant over the first report, two over each other one."""
    return variants if r is _REPORTS[0] else [variants[i], variants[j]]


def _row(k: str, r: audit.MissReport, a: dict[str, Any], out: Any) -> dict[str, Any]:
    return {"k": k, "ri": _REPORTS.index(r), "a": a, "o": out}


def _turn_rows() -> list[dict[str, Any]]:
    rows = []
    variants: list[dict[str, Any]] = [
        {},
        {"repeat": True},
        {"repeat": False, "client_model": "claude-x", "prompt_id": "p1"},
        {"reply_source": "payload", "rule": "rs-v6", "settled_count": 0},
        {"reply_source": "tail", "settled_count": 3},
        {"reply_source": "interrupted", "settled_count": 10**20},
        {"reply_source": "Payload"},
        {"reply_source": ""},
        {"reply_source": "it's"},
        {"settled_count": -1},
        {"settled_count": True},
        {"settled_count": False},
        {"settled_count": 3.0},
        {"settled_count": "3"},
        {"settled_count": None},
        {"settled_count": -1, "triggered_from": "nope"},
        {"triggered_from": "nope", "reply_source": "nope"},
        {"triggered_from": "mcp_tool"},
        {"triggered_from": "prompt_hook"},
        {"triggered_from": "session_hook"},
        {"triggered_from": "Stop_hook"},
        {"triggered_from": ""},
        {"triggered_from": 'a"b'},
        {"triggered_from": "tab\there"},
    ]
    for report in _REPORTS:
        for v in _few(report, variants, 3, 6):
            kw = {k: x for k, x in v.items() if k != "triggered_from"}
            tf = v.get("triggered_from", "stop_hook")
            out = _call(
                audit.turn_audited_fields,
                report,
                session_id="s-turn",
                probe_mode="lexical",
                assistant_present=bool(len(kw) % 2),
                triggered_from=tf,
                **kw,
            )
            a = {"tf": tf, "ap": bool(len(kw) % 2), **kw}
            if "settled_count" in kw:
                a["settled_count"] = neutral(kw["settled_count"])
            rows.append(_row("turn", report, a, out))
    return rows


def _miss_rows() -> list[dict[str, Any]]:
    rows = []
    variants: list[dict[str, Any]] = [
        {},
        {"event_id": "E1"},
        {"event_id": "", "client_model": "cm", "rule": "r"},
        {"rule": ""},
        {"triggered_from": "prompt_hook"},
        {"triggered_from": "bad"},
    ]
    for report in _REPORTS:
        for v in _few(report, variants, 1, 5):
            kw = {k: x for k, x in v.items() if k != "triggered_from"}
            tf = v.get("triggered_from", "stop_hook")
            out = _call(
                audit.search_miss_fields,
                report,
                session_id="s-miss",
                triggered_from=tf,
                **kw,
            )
            rows.append(_row("miss", report, {"tf": tf, **kw}, out))
    return rows


_HELD = [
    [],
    [{"id": "m1", "reason": "other_project"}],
    [{"reason": "r", "id": "m2"}, {}],
]


def _pairs(entries: list[dict[str, str]]) -> list[list[list[str]]]:
    """Each mapping as its ``[key, value]`` pairs, so its order is kept."""
    return [[[k, v] for k, v in e.items()] for e in entries]


def _held_rows() -> list[dict[str, Any]]:
    rows = []
    variants: list[dict[str, Any]] = [
        {"cause": c} for c in sorted(audit.HELD_CAUSES)
    ] + [
        {
            "cause": "no_hits",
            "segment": 0,
            "prompt_id": "p",
            "transcript_path": "/t",
            "harness": "h",
        },
        {"cause": "no_hits", "segment": -5},
        {"cause": "No_hits"},
        {"cause": "bad", "triggered_from": "bad"},
        {"cause": "no_hits", "triggered_from": "stop_hook"},
        {"cause": "no_hits", "triggered_from": "x"},
    ]
    for report in _REPORTS:
        for i, v in enumerate(_few(report, variants, 5, 7)):
            held = _HELD[i % len(_HELD)]
            out = _call(
                audit.recall_held_fields,
                report,
                session_id="s-held",
                probe_mode="lexical",
                held=held,
                rule="rs-v6",
                **v,
            )
            a = {
                "held": _pairs(held),
                "tf": v.get("triggered_from", "prompt_hook"),
                **v,
            }
            a.pop("triggered_from", None)
            rows.append(_row("held", report, a, out))
    return rows


def _recall_rows() -> list[dict[str, Any]]:
    rows = []
    variants: list[dict[str, Any]] = [
        {},
        {"delivered_reason": "everywhere", "shown_ids": [], "suppressed": []},
        {"shown_ids": ["m1", "m2"], "suppressed": _HELD[2], "rule": "rs-v6"},
        {
            "rule": "",
            "segment": 3,
            "prompt_id": "p",
            "transcript_path": "/t",
            "harness": "h",
        },
        {"triggered_from": "stop_hook", "injected": -7},
        {"triggered_from": "nope"},
        {"triggered_from": "nope", "rule": "r"},
    ]
    for report in _REPORTS:
        for v in _few(report, variants, 2, 5):
            kw = {k: x for k, x in v.items() if k != "injected"}
            chars = v.get("injected", 120)
            out = _call(
                audit.prompt_recall_fields,
                report,
                session_id="s-recall",
                probe_mode="lexical",
                injected_chars=chars,
                **kw,
            )
            a = {"chars": chars, **kw}
            if "suppressed" in a:
                a["suppressed"] = _pairs(a["suppressed"])
            a["tf"] = a.pop("triggered_from", "prompt_hook")
            rows.append(_row("recall", report, a, out))
    return rows


def _consts() -> dict[str, Any]:
    return {
        "k": "consts",
        "retrieval_event_kinds": sorted(audit._RETRIEVAL_EVENT_KINDS),
        "threshold_rule_v1": audit.THRESHOLD_RULE_V1,
        "top_hits_retained": audit._TOP_HITS_RETAINED,
        "default_lookback_seconds": audit.DEFAULT_LOOKBACK_SECONDS,
        "attribution_lookback_seconds": audit.ATTRIBUTION_LOOKBACK_SECONDS,
        "reaudit_dedup_window_seconds": audit.REAUDIT_DEDUP_WINDOW_SECONDS,
        "default_creation_shield_seconds": audit.DEFAULT_CREATION_SHIELD_SECONDS,
        "min_probe_content_tokens": audit.MIN_PROBE_CONTENT_TOKENS,
        "ack_surface": list(audit._ACK_SURFACE),
        "valid_triggered_from": sorted(audit._VALID_TRIGGERED_FROM),
        "no_owner_prompt": audit.NO_OWNER_PROMPT,
        "valid_no_signal_reason": sorted(audit._VALID_NO_SIGNAL_REASON),
        "valid_reply_source": sorted(audit._VALID_REPLY_SOURCE),
        "held_causes": sorted(audit.HELD_CAUSES),
        "verdicts": list(audit.Verdict.__args__),  # type: ignore[attr-defined]
    }


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for r in _REPORTS:
        rows.append(
            {
                "k": "report",
                "r": _spec(r),
                "miss": r.is_miss,
                "o": json.dumps(r.to_dict()),
            }
        )
    for h in _HITS:
        rows.append(
            {
                "k": "hit",
                "h": _hit_spec(h),
                "o": json.dumps(h.to_dict()),
                "c": json.dumps(h.to_compact_dict()),
            }
        )
    for sid, at, lb in (("s", 0, 600), ("", 1, 0), ("x\ny", 2, -1)):
        rep = audit.no_owner_prompt_report(
            session_id=sid, now=_AT[at], lookback_seconds=lb
        )
        rows.append(
            {
                "k": "noowner",
                "sid": sid,
                "at": _AT[at].isoformat(),
                "lb": lb,
                "r": _spec(rep),
            }
        )
    rows += _turn_rows() + _miss_rows() + _held_rows() + _recall_rows()
    header = {**_consts(), "reports": [_spec(r) for r in _REPORTS], "count": len(rows)}
    return {"audit_fields.jsonl": jsonl([header, *rows])}
