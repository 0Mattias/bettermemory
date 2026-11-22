"""Fixtures: the prompt recall's steps (``recall_prompt.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall_prompt.rs``. This pins
`recall_prompt`'s `_prompt_window`, `_settle_before_probe`,
`_prompt_probe`, `_record_hold` and `_record_delivery` over a real
`_PromptTurn` and scripted fakes.

One file, not gated: no row depends on the Python release or the
platform. One JSON object per line, keys sorted, ASCII. Its first line is
the header (``{"k": "consts", ...}``: `POOL_PREFILTER`, `PROMPT_RECALL`,
`RULE` and the count of the lines after it). Every other line is
``{"k": "win" | "settle" | "probe" | "hold" | "deliv", "c": case, "log":
calls, "o": answer}``: the step over the case, each call the fakes saw
in order and the answer (or ``{"err": name}``), in `_plain`'s compact
form.

The fakes: the store answers ``recent`` to `events_since` and ``audits``
to `events_of_kinds`; `bridged_session` answers ``bridge`` and
`_disabled_scopes_from_events` ``scopes``; the admission is a name, and
`Admission.on(surface)` answers ``name@surface``; `settle_interrupted`
raises ``raise`` (``[class, message]``) when given; `_probe_message`
answers the case's report ``rep``; `_delivery_membership` answers ``dm`` (pairs)
and `_rc3_admitted` ``rc3``; the recorder's write raises ``rraise``; a
case with ``sup`` runs inside `events.suppressed`. Event ids are minted
``E1``, ``E2``, ... in order. A report is ``{"v": verdict, "hits":
[[id, score, relevance, scopes, snippet, matched, query, relevance_v2]],
"pq": probe query, "nsr": no-signal reason}``, checked at ``T``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import audit, events, recall_prompt, recall_settle, recall_state
from bettermemory.membership import POOL_PREFILTER, PROMPT_RECALL

from ._common import jsonl

T = "2026-01-01T12:00:00+00:00"
EARLY = "0001-01-01T00:30:00+00:00"
EARLY2 = "0001-01-01T02:00:00+00:00"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _plain(value: Any) -> Any:
    """A `str`, `None`, `bool`, `int` and `float` as JSON, a list or tuple
    as a list, a set as its sorted list, a dict as ``{"d": [[key, value],
    ...]}`` in order, a datetime as ``{"dt": isoformat}``."""
    if value is None or isinstance(value, (bool, int, float, str)):
        assert not isinstance(value, str) or not any(
            0xD800 <= ord(c) < 0xE000 for c in value
        )
        return value
    if isinstance(value, datetime):
        return {"dt": value.isoformat()}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {"d": [[_plain(k), _plain(v)] for k, v in value.items()]}
    raise TypeError(type(value).__name__)


class _Adm:
    def __init__(self, name: str, log: list[Any]) -> None:
        self.name = name
        self.log = log

    def on(self, surface: str) -> _Adm:
        self.log.append(["on", self.name, surface])
        return _Adm(f"{self.name}@{surface}", self.log)


def _report(spec: dict[str, Any]) -> audit.MissReport:
    hits = tuple(
        audit.MissHit(
            id=h[0],
            score=h[1],
            relevance=h[2],
            scopes=tuple(h[3]),
            snippet=h[4],
            matched_unique=h[5],
            query_unique=h[6],
        )
        for h in spec.get("hits", [])
    )
    for h, hit in zip(spec.get("hits", []), hits, strict=True):
        assert h[7] == hit.to_dict()["relevance_v2"]
    return audit.MissReport(
        verdict=spec.get("v", "miss"),
        checked_at=_dt(T),
        session_id="S",
        lookback_seconds=300,
        recent_retrieval_count=0,
        threshold_rule="r1",
        top_hits=hits,
        probe_query=spec.get("pq"),
        no_signal_reason=spec.get("nsr"),
    )


class _Fakes:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.log: list[Any] = []
        self.minted = 0

    def events_since(self, since: datetime) -> list[dict[str, Any]]:
        self.log.append(["es", since])
        return [dict(e) for e in self.case.get("recent", [])]

    def events_of_kinds(self, kinds: Any, *, since: datetime) -> list[dict[str, Any]]:
        self.log.append(["eok", list(kinds), since])
        return [dict(e) for e in self.case.get("audits", [])]

    def bridged(
        self, events_: list[Any], *, harness_session: str, worktree_root: str | None
    ) -> str | None:
        self.log.append(["bridge", len(events_), harness_session, worktree_root])
        return self.case.get("bridge")

    def scopes(
        self,
        events_: list[Any],
        *,
        worktree_root: str | None,
        server_session: str | None,
    ) -> set[str]:
        self.log.append(["scopes", len(events_), worktree_root, server_session])
        return set(self.case.get("scopes", []))

    def settle(self, **kw: Any) -> None:
        self.log.append(
            [
                "settle",
                kw["session_id"],
                kw["transcript_path"],
                kw["admission"].name,
                kw["excluded_scopes"],
                len(kw["recent"]),
                kw["server_session"],
                kw["worktree_root"],
                kw["now"],
                kw["previous_audit"],
                kw["triggered_from"],
                kw["exclude_prompt_id"],
            ]
        )
        raised = self.case.get("raise")
        if raised is not None:
            cls = {"ValueError": ValueError, "OSError": OSError, "KeyError": KeyError}
            raise cls[raised[0]](*raised[1:])

    def probe(self, **kw: Any) -> audit.MissReport:
        self.log.append(
            [
                "probe",
                kw["user_message"],
                kw["session_id"],
                kw["server_session"],
                kw["admission"].name,
                kw["excluded_scopes"],
                len(kw["recent"]),
                kw["worktree_root"],
                kw["now"],
                kw["floor"],
            ]
        )
        return _report(self.case.get("rep", {}))

    def warning(self, fmt: str, *args: Any) -> None:
        self.log.append(["warn", fmt % args])

    def membership(self, resolution: Any, admission: Any) -> dict[str, Any]:
        self.log.append(["dm", resolution, admission.name])
        return dict(self.case.get("dm", [["checkout", None], ["membership_rule", "r"]]))

    def rc3(self, memories: Any, caller: Any, *, project_only: bool) -> list[str]:
        self.log.append(["rc3", list(memories), caller.worktree_root, project_only])
        return list(self.case.get("rc3", []))

    def forget(self, key: tuple[str, str]) -> None:
        self.log.append(["forget", list(key)])

    def mint(self) -> str:
        self.minted += 1
        return f"E{self.minted}"

    def recorder(self, **kw: Any) -> Any:
        fakes = self

        class _Rec(events.Recorder):
            def record(self, kind: str, **fields: Any) -> None:
                fakes.log.append(["rec", kind, fields])
                raised = fakes.case.get("rraise")
                if raised is not None:
                    raise OSError(raised)

        self.log.append(
            [
                "recorder",
                kw["session_id"],
                kw["enabled"],
                kw["worktree_root"],
                kw.get("triggered_from", ""),
            ]
        )
        return _Rec(**kw)


def _turn(f: _Fakes, case: dict[str, Any]) -> Any:
    return recall_prompt._PromptTurn(
        store=f,  # type: ignore[arg-type]
        cfg=SimpleNamespace(  # type: ignore[arg-type]
            telemetry=SimpleNamespace(enabled=case.get("tel", True)),
            behavior=SimpleNamespace(search_mode=case.get("sm", "")),
        ),
        mode=case.get("mode", "project"),
        query=case.get("q", "how do I run the tests"),
        resolution=case.get("res"),
        surface=case.get("surface", PROMPT_RECALL),
        admission=_Adm("adm", f.log),  # type: ignore[arg-type]
        remint=None,
        now=_dt(case.get("now", T)),
        key=tuple(case.get("key", ["claude-code", "S"])),
        caller=SimpleNamespace(worktree_root=case.get("wt")),  # type: ignore[arg-type]
        prompt_id=case.get("pid"),
        transcript_path=case.get("tp"),
    )


def _run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    f = _Fakes(case)
    turn = _turn(f, case)
    recent = [dict(e) for e in case.get("recent", [])]
    ss = case.get("bridge")
    excluded = set(case.get("scopes", []))
    patches: list[Any] = [
        mock.patch.object(recall_prompt, "bridged_session", f.bridged),
        mock.patch.object(recall_prompt, "_disabled_scopes_from_events", f.scopes),
        mock.patch.object(recall_prompt, "settle_interrupted", f.settle),
        mock.patch.object(recall_settle, "_probe_message", f.probe),
        mock.patch.object(recall_prompt, "log", f),
        mock.patch.object(recall_prompt, "_delivery_membership", f.membership),
        mock.patch.object(recall_prompt, "_rc3_admitted", f.rc3),
        mock.patch.object(recall_prompt, "Recorder", f.recorder),
        mock.patch.object(audit, "generate_ulid", f.mint),
    ]

    def call() -> Any:
        if kind == "win":
            seg = case.get("seg", {})
            view = SimpleNamespace(
                started_at=_dt(seg.get("at", T)), segment=seg.get("n", 0)
            )
            own, rec, server, scopes = recall_prompt._prompt_window(turn, view)  # type: ignore[arg-type]
            return [own, len(rec), server, scopes]
        if kind == "settle":
            return recall_prompt._settle_before_probe(turn, recent, ss, excluded)
        if kind == "probe":
            return recall_prompt._prompt_probe(turn, recent, ss, excluded).to_dict()
        report = _report(case.get("rep", {}))
        if kind == "hold":
            return recall_prompt._record_hold(
                turn,
                report,
                case.get("seg", 1),
                case.get("cause", "no_hits"),
                case.get("held", []),
            )
        state = SimpleNamespace(forget=f.forget)
        chosen = [
            (SimpleNamespace(id=i), None, None, None) for i in case.get("chosen", [])
        ]
        return recall_prompt._record_delivery(
            turn,
            report,
            state,  # type: ignore[arg-type]
            block=case.get("block", "block"),
            shown_ids=case.get("shown", []),
            event_id=case.get("eid", "D1"),
            segment=case.get("seg", 1),
            chosen=chosen,  # type: ignore[arg-type]
            suppressed=case.get("supp", []),
            rc3_candidates=case.get("cand", []),
        )

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        if case.get("sup"):
            stack.enter_context(events.suppressed())
        try:
            out = _plain(call())
        except (OverflowError, TypeError, ValueError, OSError) as exc:
            out = {"err": type(exc).__name__}
    return {"k": kind, "c": case, "log": _plain(f.log), "o": out}


def _ts(minutes: int) -> str:
    return f"2026-01-01T{11 + minutes // 60:02d}:{minutes % 60:02d}:00.000Z"


_HIT = [
    "01HZZZZZZZZZZZZZZZZZZZZZZA",
    0.5,
    "high",
    ["project"],
    "run the suite",
    3,
    4,
    "low",
]
_HIT2 = ["01HZZZZZZZZZZZZZZZZZZZZZZB", 0.25, "low", [], "other", 0, 0, "low"]


def _win_cases() -> list[dict[str, Any]]:
    ev = [{"kind": "search", "ts": _ts(10)}, {"kind": "show", "ts": _ts(20)}]
    return [
        {},
        {"seg": {"n": 1, "at": "2026-01-01T09:00:00+00:00"}},
        {
            "seg": {"n": 2, "at": "2025-12-31T09:00:00+05:00"},
            "recent": ev,
            "bridge": "srv",
        },
        {"seg": {"n": True}},
        {"seg": {"n": False}},
        {"seg": {"n": -1}},
        {"seg": {"n": None}},
        {"seg": {"n": "1"}},
        {"seg": {"n": [1]}},
        {"seg": {"n": 0.0}},
        {"seg": {"n": -0.0}},
        {"seg": {"n": 0.5}},
        {"now": EARLY},
        {"now": EARLY, "seg": {"n": None}},
        {"now": EARLY2},
        {"now": EARLY2, "seg": {"n": 1}},
        {"now": EARLY2, "seg": {"n": "x"}},
        {"wt": "/w/tree", "bridge": "srv", "scopes": ["b", "a"], "recent": ev},
        {"key": ["codex", "K"], "wt": "/w", "now": "2026-03-01T00:00:00-07:00"},
    ]


def _settle_cases() -> list[dict[str, Any]]:
    audits = [
        {"kind": "turn_audited", "ts": _ts(5), "session": "S"},
        {"kind": "turn_audited", "ts": _ts(30), "session_id": "S"},
        {"kind": "turn_audited", "ts": _ts(40), "session": "T"},
    ]
    return [
        {},
        {
            "audits": audits,
            "tp": "/t/x.jsonl",
            "wt": "/w",
            "bridge": "srv",
            "scopes": ["a"],
        },
        {"raise": ["ValueError", "boom"]},
        {"raise": ["OSError"]},
        {"raise": ["OSError", 2, "No such file"]},
        {"raise": ["KeyError", "k"]},
        {"raise": ["KeyError", "x y\n"]},
        {"raise": ["KeyError", "it's"]},
        {"raise": ["KeyError", 'it\'s "x"\t\x01\x7f']},
        # C1 controls, no-break space, line separator, a format character
        # and an astral private use point: repr escapes what isprintable
        # rejects (categories stable across Unicode releases).
        {"raise": ["KeyError", "\x85\n\xa0\u2028\u200b\U000f0000\xe9"]},
        {"raise": ["ValueError"]},
        {"now": EARLY2},
        {"now": EARLY2, "raise": ["ValueError", "never"]},
        {"key": ["codex", "K"], "audits": audits, "recent": [{"kind": "x"}]},
    ]


def _probe_cases() -> list[dict[str, Any]]:
    return [
        {"rep": {"v": "miss", "hits": [_HIT], "pq": "run tests"}},
        {"surface": "search_unscoped", "rep": {"v": "ok"}},
        {"surface": "other", "q": "", "rep": {"v": "no_signal", "nsr": "empty_query"}},
        {
            "q": "deploy",
            "bridge": "srv",
            "scopes": ["s2", "s1"],
            "wt": "/w",
            "recent": [{"kind": "show"}],
            "rep": {"hits": [_HIT, _HIT2]},
        },
    ]


def _hold_cases() -> list[dict[str, Any]]:
    held = [{"id": "m1", "why": "seen"}, {"id": "m2", "why": "cooldown"}]
    return [
        {},
        {"tel": False},
        {"tel": False, "sup": True},
        {"sup": True},
        {"sm": "bm25", "held": held, "pid": "P1", "tp": "/t", "seg": 3},
        {
            "sm": None,
            "rep": {"v": "no_signal", "nsr": "empty_store"},
            "cause": "no_signal",
        },
        {"cause": "bogus"},
        {"cause": "bogus", "tel": False},
        {"rraise": "disk full"},
        {
            "surface": "search_unscoped",
            "res": "R",
            "dm": [["membership_rule", "x"], ["checkout", "/c"]],
        },
        {"dm": [["extra", 1], ["admission", "over"], ["event_id", "over"]]},
        {
            "key": ["codex", "K"],
            "wt": "/w",
            "rep": {"hits": [_HIT]},
            "cause": "every_hit_held",
        },
    ]


def _deliv_cases() -> list[dict[str, Any]]:
    a, b, c = (
        "01HZZZZZZZZZZZZZZZZZZZZZZA",
        "01HZZZZZZZZZZZZZZZZZZZZZZB",
        "01HZZZZZZZZZZZZZZZZZZZZZZC",
    )
    return [
        {},
        {
            "block": "Recalled: one\nhi é\U0001f600",
            "shown": [a],
            "chosen": [a, b],
            "cand": [a, b, c],
            "rc3": [a, b, c],
            "rep": {"hits": [_HIT, _HIT2], "pq": "q"},
        },
        {
            "surface": "search_unscoped",
            "mode": "everywhere",
            "rc3": [c, c],
            "chosen": [c],
            "shown": [],
        },
        {"rraise": "locked", "shown": [a]},
        {"tel": False, "sup": True, "sm": "vector", "pid": "P", "tp": "/t", "seg": 0},
        {"supp": [{"id": "m9", "why": "held"}], "eid": "", "block": ""},
        {
            "dm": [["rc3_admitted", "x"], ["event_id", "over"], ["checkout", None]],
            "rc3": [a],
        },
        {
            "key": ["codex", "K"],
            "wt": "/w",
            "res": "R",
            "seg": -2,
            "shown": [b, a],
            "chosen": [a],
        },
    ]


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for kind, cases in (
        ("win", _win_cases()),
        ("settle", _settle_cases()),
        ("probe", _probe_cases()),
        ("hold", _hold_cases()),
        ("deliv", _deliv_cases()),
    ):
        rows.extend(_run(kind, case) for case in cases)
    header = {
        "k": "consts",
        "pool_prefilter": POOL_PREFILTER,
        "prompt_recall": PROMPT_RECALL,
        "rule": recall_state.RULE,
        "count": len(rows),
    }
    return {"recall_prompt.jsonl": jsonl([header, *rows])}
