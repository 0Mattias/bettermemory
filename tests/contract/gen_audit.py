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
from typing import Any, cast
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


# ---------------------------------------------------------------------------
# audit_probe.jsonl: the re-audit dedup, the miss probe and the retrieval
# shield (audit.py 859-1491) with search_lane._relevance_label_v2.
#
# One file, not gated. Every message and event text is ASCII, so no row
# follows the Unicode tables. The ranker (`run_search`) is replaced by a
# recorder that answers the row's hits and keeps each call's pool ids,
# message, `now` and keyword arguments; the admission is a fake that
# keeps what `prime` and `same_project` were given. Events, memories and
# arguments are plain JSON (every value is a JSON type; the one float
# knob, `half_life_days`, is exact in binary), and no row holds a time
# read from the clock. A row's answer is a report spec (as `_spec`) or
# ``{"err": type}`` (with the message for a ValueError).

_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=_UTC)
_ULIDS = [f"01J{'0' * 22}{c}" for c in "ABCDEFGH"]
#: The memory payloads the probe rows name by index, in first-use order.
_MEMS: list[dict[str, Any]] = []


def _mem_index(i: int, created: datetime) -> int:
    payload = _memory(i, created)
    if payload not in _MEMS:
        _MEMS.append(payload)
    return _MEMS.index(payload)


def _memory(i: int, created: datetime) -> dict[str, Any]:
    from bettermemory.models import Memory

    m = Memory(
        id=_ULIDS[i],
        scopes=["global"],
        body=f"body {i}",
        confidence="high",
        source="explicit-statement",
        created=created,
        updated=created,
    )
    return m.model_dump(mode="json")


class _Admission:
    def __init__(self, same: list[str]) -> None:
        self.same = same
        self.primed: list[list[str]] = []
        self.asked: list[str] = []

    def prime(self, ids: Any) -> None:
        self.primed.append(list(ids))

    def same_project(self, rid: str) -> bool:
        self.asked.append(rid)
        return rid in self.same


def _hit(
    i: str,
    rel: str = "high",
    scopes: tuple[str, ...] = ("global",),
    mu: int = 2,
    qu: int = 2,
) -> dict[str, Any]:
    return {
        "id": i,
        "score": 0.5,
        "rel": rel,
        "scopes": list(scopes),
        "mu": mu,
        "qu": qu,
    }


def _ns_hit(h: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=h["id"],
        score=h["score"],
        relevance=h["rel"],
        scopes=list(h["scopes"]),
        snippet=f"snip {h['id']}",
        match_terms=[f"t{n}" for n in range(h["mu"])],
        query_unique=h["qu"],
    )


def _ev(kind: Any, ts: Any = "2026-03-01T11:59:30.000Z", **kw: Any) -> dict[str, Any]:
    return {"kind": kind, "ts": ts, **kw}


def _err(exc: Exception) -> dict[str, Any]:
    out: dict[str, Any] = {"err": type(exc).__name__}
    if isinstance(exc, ValueError):
        out["msg"] = str(exc)
    return out


def _probe_row(
    name: str,
    msg: str = "deploy the backup server",
    *,
    mems: list[tuple[int, datetime]] | None = None,
    events: list[dict[str, Any]] | None = None,
    hits: list[dict[str, Any]] | None = None,
    same: list[str] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    mems = (
        [(0, datetime(2026, 1, 1, tzinfo=_UTC)), (1, _NOW - timedelta(seconds=30))]
        if mems is None
        else mems
    )
    events = events or []
    hits = [_hit("m1")] if hits is None else hits
    adm = None if same is None else _Admission(same)
    kw.setdefault("session_id", "S")
    kw.setdefault("now", _NOW)
    from bettermemory.models import Memory

    memories = [
        Memory(
            id=_ULIDS[i],
            scopes=["global"],
            body=f"body {i}",
            confidence="high",
            source="explicit-statement",
            created=c,
            updated=c,
        )
        for i, c in mems
    ]
    calls: list[dict[str, Any]] = []

    def fake(pool: Any, message: str, **k: Any) -> list[Any]:
        calls.append(
            {
                "pool": [m.id for m in pool],
                "msg": message,
                "now": k["now"].isoformat(),
                "mode": k["mode"],
                "ex": sorted(k["excluded_scopes"])
                if k["excluded_scopes"] is not None
                else None,
                "max": k["max_results"],
                "hl": k["half_life_days"],
                "conv": k["conversational"],
                "rescue": k["rescue_expansion"],
                "adm": k["admission"] is adm,
            }
        )
        return [_ns_hit(h) for h in hits]

    try:
        with patch.object(audit, "run_search", fake):
            rep = audit.probe_for_miss(
                memories,
                msg,
                recent_events=iter(events),
                admission=cast(Any, adm),
                **kw,
            )
        out: Any = _spec(rep)
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        out = _err(exc)
    args = {
        k: (
            v.isoformat()
            if isinstance(v, datetime)
            else sorted(v)
            if isinstance(v, set)
            else v
        )
        for k, v in kw.items()
    }
    return {
        "k": "probe",
        "name": name,
        "mem": [_mem_index(i, c) for i, c in mems],
        "msg": msg,
        "ev": events,
        "hits": hits,
        "same": same,
        "args": args,
        "o": out,
        "calls": calls,
        "primed": adm.primed if adm else None,
        "asked": adm.asked if adm else None,
    }


def _probe_rows() -> list[dict[str, Any]]:
    old = datetime(2026, 1, 1, tzinfo=_UTC)
    early = datetime(1, 1, 1, 0, 0, 30, tzinfo=_UTC)
    r = _probe_row
    proj = ("projects:x", "global")
    return [
        r("empty_message", ""),
        r("space_message", " \t\n "),
        r("no_memories", mems=[]),
        r("no_memories_searched", mems=[], searched=True),
        r("no_memories_searched_thin", "deploy", mems=[], searched=True),
        r("empty_message_searched", "", mems=[], searched=True),
        r("thin_one_token", "deploy"),
        r("thin_repeated_token", "deploy deploys deploying"),
        r("thin_stopwords", "go for it and the"),
        r("thin_digits", "deploy 123 456"),
        r("thin_ack", "yes sounds good thanks"),
        r("ack_plus_word", "yes sounds good deploy"),
        r("thin_floor_off", "deploy", floor=False),
        r("empty_floor_off", "", floor=False),
        r("miss"),
        r("ok_medium", hits=[_hit("m1", "medium")]),
        r("ok_case", hits=[_hit("m1", "High")]),
        r("no_hits", hits=[]),
        r(
            "four_hits",
            hits=[_hit("a"), _hit("b", "low"), _hit("c"), _hit("d", "medium")],
        ),
        r(
            "v2_grid",
            hits=[
                _hit("a", "low", (), 4, 10),
                _hit("b", "low", (), 0, 0),
                _hit("c", "low", (), 3, 4),
                _hit("d", "low", (), 2, 5),
                _hit("e", "low", (), 1, 5),
                _hit("f", "low", (), 5, -1),
            ],
        ),
        r("bad_mode", mode="semantic"),
        r("bad_mode_quote", mode="it's"),
        r(
            "modes",
            mode="keyword",
            excluded_scopes={"b", "a"},
            half_life_days=7.5,
            conversational=False,
        ),
        r("mode_bm25", mode="bm25", excluded_scopes=set()),
        r("shield_all", mems=[(1, _NOW - timedelta(seconds=30))], hits=[]),
        r(
            "shield_bound",
            mems=[
                (0, _NOW - timedelta(seconds=60)),
                (1, _NOW - timedelta(seconds=59, microseconds=999999)),
            ],
        ),
        r(
            "shield_zero",
            creation_shield_seconds=0,
            mems=[(0, _NOW), (1, _NOW + timedelta(microseconds=1))],
        ),
        r(
            "shield_negative",
            creation_shield_seconds=-10,
            mems=[(0, _NOW + timedelta(seconds=10)), (1, _NOW + timedelta(seconds=11))],
        ),
        r(
            "shield_offset_created",
            mems=[
                (
                    0,
                    datetime(
                        2026, 3, 1, 13, 59, 0, tzinfo=timezone(timedelta(hours=2))
                    ),
                ),
                (
                    1,
                    datetime(
                        2026, 3, 1, 13, 59, 30, tzinfo=timezone(timedelta(hours=2))
                    ),
                ),
            ],
        ),
        r(
            "shield_naive_created",
            mems=[
                (0, datetime(2026, 3, 1, 11, 58)),
                (1, datetime(2026, 3, 1, 11, 59, 30)),
            ],
        ),
        r("shield_overflow", now=early, mems=[(0, old)]),
        r("shield_overflow_bad_mode", now=early, mems=[(0, old)], mode="x"),
        r(
            "lookback_overflow",
            now=early,
            creation_shield_seconds=0,
            mems=[(0, datetime(1, 1, 1, tzinfo=_UTC))],
            lookback_seconds=31,
        ),
        r(
            "lookback_overflow_no_hits",
            now=early,
            creation_shield_seconds=0,
            mems=[(0, datetime(1, 1, 1, tzinfo=_UTC))],
            lookback_seconds=31,
            hits=[],
        ),
        r("lookback_huge", lookback_seconds=10**12),
        r("naive_now", now=datetime(2026, 3, 1, 12, 0, 0)),
        r(
            "offset_now",
            now=datetime(2026, 3, 1, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
            events=[_ev("search", session="S")],
        ),
        r("retrieval_session", events=[_ev("search", session="S")]),
        r("retrieval_session_id_field", events=[_ev("show", session_id="S")]),
        r("retrieval_session_falsy", events=[_ev("list", session="", session_id="S")]),
        r("retrieval_session_wins", events=[_ev("list", session="T", session_id="S")]),
        r("retrieval_other_session", events=[_ev("search", session="T")]),
        r(
            "retrieval_anchor",
            retrieval_session_id="R",
            events=[
                _ev("search", session="R"),
                _ev("prompt_recall", session="S"),
                _ev("search", session="T"),
            ],
        ),
        r(
            "retrieval_empty_ids",
            session_id="",
            retrieval_session_id="",
            events=[_ev("search", session="")],
        ),
        r(
            "retrieval_old",
            events=[_ev("search", "2026-03-01T11:58:59.999999Z", session="S")],
        ),
        r(
            "retrieval_bound",
            events=[_ev("search", "2026-03-01T11:59:00Z", session="S")],
        ),
        r(
            "retrieval_naive_ts",
            events=[_ev("search", "2026-03-01T11:59:30", session="S")],
        ),
        r(
            "retrieval_offset_ts",
            events=[_ev("search", "2026-03-01T13:59:30+02:00", session="S")],
        ),
        r(
            "retrieval_bad_ts",
            events=[
                _ev("search", "yesterday", session="S"),
                _ev("search", 5, session="S"),
                _ev("search", None, session="S"),
            ],
        ),
        r(
            "retrieval_future",
            events=[_ev("search", "2027-01-01T00:00:00Z", session="S")],
        ),
        r(
            "retrieval_kinds",
            events=[
                _ev(k, session="S")
                for k in ("write", "search_miss", "turn_audited", None, 3, "Search")
            ],
        ),
        r(
            "retrieval_wide_window",
            lookback_seconds=600,
            events=[_ev("search", "2026-03-01T11:50:00Z", session="S")],
        ),
        r(
            "worktree_match",
            worktree_root="/w",
            events=[_ev("search", session="T", worktree_root="/w")],
        ),
        r(
            "worktree_empty",
            worktree_root="",
            events=[_ev("search", session="T", worktree_root="")],
        ),
        r("worktree_none", events=[_ev("search", session="T", worktree_root=None)]),
        r(
            "worktree_other",
            worktree_root="/w",
            events=[_ev("search", session="T", worktree_root="/v")],
        ),
        r(
            "admission_rid",
            same=["r2"],
            events=[
                _ev("search", session="T", returned=["r1", "r2"]),
                _ev("show", session="T", id="r3"),
            ],
        ),
        r(
            "admission_fields",
            same=["zz"],
            events=[
                _ev(
                    "search",
                    session="T",
                    id="i",
                    ids=["j", 4, "k"],
                    shown_ids=["s"],
                    hit_ids="h",
                    memory_ids=["m"],
                    returned=["r"],
                )
            ],
        ),
        r(
            "admission_none_match",
            same=[],
            events=[_ev("search", session="T", returned=["r1"])],
        ),
        r(
            "admission_session_first",
            same=["r1"],
            events=[
                _ev("search", session="S", returned=["r1"]),
                _ev("search", "2026-03-01T10:00:00Z", returned=["r1"]),
            ],
        ),
        r(
            "admission_empty_id",
            same=[""],
            events=[_ev("search", session="T", id="", returned=[""])],
        ),
        r("cohort", same=[_ULIDS[0], "m1"], hits=[_hit("m1", scopes=proj)]),
        r(
            "cohort_prefix",
            same=["m1"],
            hits=[_hit("m1", scopes=("project:x", "projectsx"))],
        ),
        r("cohort_global", same=["m1"], hits=[_hit("m1")]),
        r(
            "cohort_other_home",
            same=["m2"],
            hits=[_hit("m1", scopes=proj), _hit("m2", scopes=proj)],
        ),
        r("cohort_rank_two", same=["m2"], hits=[_hit("m1"), _hit("m2", scopes=proj)]),
        r("cohort_no_admission", hits=[_hit("m1", scopes=proj)]),
        r(
            "cohort_after_retrieval",
            same=["m1"],
            hits=[_hit("m1", scopes=proj)],
            events=[_ev("search", session="S")],
        ),
        r("cohort_low", same=["m1"], hits=[_hit("m1", "low", proj)]),
        r("type_error_session", events=[_ev("search", session=["S"])]),
        r("type_error_session_dict", events=[_ev("search", session={"a": 1})]),
        r("type_error_kind", events=[_ev(["search"], session="S")]),
        r(
            "type_error_old_session",
            events=[_ev("search", "2020-01-01T00:00:00Z", session=["S"])],
        ),
        r("type_error_kind_no_hits", events=[_ev(["search"])], hits=[]),
        r("session_tuple_like", events=[_ev("search", session=1)]),
    ]


def _dup_row(name: str, events: list[Any], **kw: Any) -> dict[str, Any]:
    kw.setdefault("session_id", "S")
    kw.setdefault("probe_query_hash", "H")
    kw.setdefault("probe_query_text", "deploy it")
    kw.setdefault("now", _NOW)
    try:
        out: Any = audit.is_duplicate_audit(events, **kw)
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        out = _err(exc)
    args = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in kw.items()}
    return {"k": "dup", "name": name, "ev": events, "args": args, "o": out}


def _dup_rows() -> list[dict[str, Any]]:
    t = "turn_audited"
    pq = {"hash": "H", "preview": "p", "len": 9}
    d = _dup_row
    return [
        d("empty", []),
        d("dict_match", [_ev(t, session_id="S", probe_query=pq)]),
        d("dict_other_hash", [_ev(t, session_id="S", probe_query={"hash": "G"})]),
        d("dict_no_hash", [_ev(t, session_id="S", probe_query={})]),
        d(
            "dict_hash_none",
            [_ev(t, session_id="S", probe_query={"hash": None})],
            probe_query_hash=None,
        ),
        d("str_match", [_ev(t, session_id="S", probe_query="deploy it")]),
        d("str_other", [_ev(t, session_id="S", probe_query="deploy")]),
        d("str_vs_hash", [_ev(t, session_id="S", probe_query="H")]),
        d("no_probe_query", [_ev(t, session_id="S")]),
        d("list_probe_query", [_ev(t, session_id="S", probe_query=["deploy it"])]),
        d("legacy_session", [_ev(t, session="S", probe_query=pq)]),
        d("session_id_wins", [_ev(t, session_id="T", session="S", probe_query=pq)]),
        d("session_id_falsy", [_ev(t, session_id="", session="S", probe_query=pq)]),
        d("other_session", [_ev(t, session_id="T", probe_query=pq)]),
        d("other_kind", [_ev("search", session_id="S", probe_query=pq)]),
        d("bad_ts_skipped", [_ev(t, "nope", session_id="S", probe_query=pq)]),
        d(
            "old_stops_walk",
            [
                _ev(t, session_id="S", probe_query=pq),
                _ev(t, "2026-03-01T10:59:59Z", session_id="S", probe_query="x"),
            ],
        ),
        d(
            "old_other_session_skipped",
            [
                _ev(t, session_id="S", probe_query=pq),
                _ev(t, "2026-03-01T10:00:00Z", session_id="T"),
            ],
        ),
        d("bound", [_ev(t, "2026-03-01T11:00:00Z", session_id="S", probe_query=pq)]),
        d(
            "past_bound",
            [_ev(t, "2026-03-01T10:59:59.999999Z", session_id="S", probe_query=pq)],
        ),
        d(
            "window_zero",
            [_ev(t, "2026-03-01T12:00:00Z", session_id="S", probe_query=pq)],
            window_seconds=0,
        ),
        d(
            "future",
            [_ev(t, "2030-01-01T00:00:00+05:00", session_id="S", probe_query=pq)],
        ),
        d(
            "naive_now",
            [_ev(t, session_id="S", probe_query=pq)],
            now=datetime(2026, 3, 1, 12),
        ),
        d("overflow", [], now=datetime(1, 1, 1, 0, 30, tzinfo=_UTC)),
        d("empty_session", [_ev(t, session_id="", probe_query=pq)], session_id=""),
        d("none_session", [_ev(t, probe_query=pq)], session_id=""),
        d("unhashable_kind", [_ev(["x"], session_id="S", probe_query=pq)]),
        d("not_a_dict", [["turn_audited"]]),
        d(
            "naive_now_other_kind",
            [_ev("search", session_id="S")],
            now=datetime(2026, 3, 1, 12),
        ),
        d(
            "naive_now_naive_ts",
            [_ev(t, "2026-03-01T11:59:00", session_id="S", probe_query="deploy it")],
            now=datetime(2026, 3, 1, 12),
        ),
    ]


def _count_row(
    name: str, events: list[Any], same: list[str] | None = None, **kw: Any
) -> dict[str, Any]:
    kw.setdefault("session_ids", ["S"])
    kw.setdefault("now", _NOW)
    kw.setdefault("lookback_seconds", 60)
    adm = None if same is None else _Admission(same)
    try:
        out: Any = audit._count_recent_retrievals(
            iter(events),
            admission=cast(Any, adm),
            **{**kw, "session_ids": set(kw["session_ids"])},
        )
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        out = _err(exc)
    args = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in kw.items()}
    return {
        "k": "count",
        "name": name,
        "ev": events,
        "same": same,
        "args": args,
        "o": out,
        "primed": adm.primed if adm else None,
        "asked": adm.asked if adm else None,
    }


def _count_rows() -> list[dict[str, Any]]:
    c = _count_row
    many = [_ev("search", session=f"s{i}", returned=[f"r{i}"]) for i in range(5)]
    return [
        c("empty", []),
        c("empty_admission", [], same=[]),
        c("no_sessions", [_ev("search", session="S")], session_ids=[]),
        c(
            "two_sessions",
            [
                _ev("search", session="S"),
                _ev("show", session="R"),
                _ev("list", session="Q"),
            ],
            session_ids=["S", "R"],
        ),
        c("prime_order", many, same=["r3"]),
        c(
            "prime_skips_old",
            [_ev("search", "2026-03-01T10:00:00Z", returned=["old"]), *many[:2]],
            same=[],
        ),
        c("not_a_dict", [["search"]]),
        c(
            "old_unhashable_session",
            [_ev("search", "2026-03-01T10:00:00Z", session=["x"])],
        ),
        c(
            "worktree_and_admission",
            [_ev("search", session="T", worktree_root="/w", returned=["r"])],
            same=["r"],
            worktree_root="/w",
        ),
        c("overflow", [], now=datetime(1, 1, 1, tzinfo=_UTC)),
        c("naive_now", [_ev("search", session="S")], now=datetime(2026, 3, 1, 12)),
        c("naive_now_no_events", [_ev("write")], now=datetime(2026, 3, 1, 12)),
        c(
            "negative_lookback",
            [
                _ev("search", "2026-03-01T12:00:05Z", session="S"),
                _ev("search", "2026-03-01T12:00:15Z", session="S"),
            ],
            lookback_seconds=-10,
        ),
    ]


def _v2_rows() -> list[dict[str, Any]]:
    from bettermemory.search_lane import _V2_HIGH_MATCHED_FLOOR, _relevance_label_v2

    rows: list[dict[str, Any]] = [{"k": "v2floor", "o": _V2_HIGH_MATCHED_FLOOR}]
    pairs = [(m, q) for q in range(-1, 9) for m in range(-1, 10)]
    pairs += [
        (3, 4),
        (2, 5),
        (1, 5),
        (4, 10**15),
        (3, 10**15),
        (10**15, 10**15),
        (-(10**15), 1),
        (7, 9),
        (3, 7),
        (2**53 + 1, 2**53 + 2),
    ]
    for m, q in pairs:
        rows.append({"k": "v2", "m": m, "q": q, "o": _relevance_label_v2(m, q)})
    return rows


def _thin_rows() -> list[dict[str, Any]]:
    msgs = [
        "",
        "deploy",
        "deploy backup",
        "the and of",
        "yes ok",
        "yes ok lgtm",
        "looks good to me",
        "works works",
        "sound work",
        "sounds works",
        "12 34",
        "deploy 12",
        "deploy 12 backup",
        "Yes, Sounds GOOD!",
        "all done thanks",
        "all done deploy",
        "x-y z",
        "a b",
        "it's fine",
    ]
    rows: list[dict[str, Any]] = [{"k": "ack", "o": sorted(audit._ACK_TOKENS)}]
    for m in msgs:
        rows.append({"k": "thin", "msg": m, "o": audit._too_thin_to_probe(m)})
    return rows


def generate_probe() -> dict[str, bytes]:
    rows = _thin_rows() + _v2_rows() + _probe_rows() + _dup_rows() + _count_rows()
    header = {"k": "header", "count": len(rows), "mems": _MEMS}
    return {"audit_probe.jsonl": jsonl([header, *rows])}
