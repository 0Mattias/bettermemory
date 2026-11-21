"""Fixtures: the Stop's settlement of what was shown
(``recall_settle.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall_settle.rs`` and
``events.rs``. This pins `recall_settle`'s `_used_in_segment`,
`settle_shown`, `settle_unused`, `previous_audit_at` and
`review_unanswered`, with `sessions.event_joined` through them, and
`events._event_id_items` and `_event_id_list`.

One file, not gated: no row depends on the Python release or the
platform. One JSON object per line, keys sorted, ASCII. Its first line is
the header (``{"k": "consts", ...}``: `RULE`, `DEFAULT_HARNESS`,
`STATE_IDLE_SECONDS`, `REVIEW_UNANSWERED`, `REAUDIT_DEDUP_WINDOW_SECONDS`,
`REVIEW_ISSUED`, the events every ``used`` row reads and the count of the lines after it). Every other line is
one of:

- ``{"k": "ids", "v": value, "o": pairs, "l": ids}``: `_event_id_items`
  and `_event_id_list` of a `_common.neutral` value;
- ``{"k": "used", "since": iso, "hs": text, "ss": [...],
  "o": ids}``: `_used_in_segment` over the header's ``used_ev``, the
  answer sorted;
- ``{"k": "shown" | "unused" | "prev" | "review", "c": case, "log":
  calls, "o": answer}``: the function over scripted fakes, each call the
  fakes saw in order and the answer (or ``{"err": name}``), both in
  `_plain`'s compact form.

Events are plain JSON objects (no floats, no surrogates). A case's
``seg`` / ``closed`` is a view: ``at`` (its start), ``n`` (its segment),
``settled``, ``roots`` and ``by`` (``{id: [surface, shown_event_id]}``).
The store's event reads answer the case's lists as they are, unfiltered,
so the function's own filters are what the rows pin. ``bodies`` maps an id
to its body, or to ``"!nf"`` (`MemoryNotFoundError`) or ``"!tomb"``
(`TombstonedError`). ``joined`` is what `joined_sessions` answers and
``chains`` what `review.find_chain` reads (``[open, standing_refused]``,
default ``[true, false]``).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import audit, events, recall_settle, recall_state, review
from bettermemory.store_schema import MemoryNotFoundError, TombstonedError

from ._common import jsonl, neutral

T = "2026-01-01T12:00:00+00:00"
ULID_A = "01HZZZZZZZZZZZZZZZZZZZZZZA"
ULID_B = "01HZZZZZZZZZZZZZZZZZZZZZZB"
ULID_C = "01HZZZZZZZZZZZZZZZZZZZZZZC"
BODY = "Run the integration suite with pytest fixtures for the database setup."
REPLY = (
    "I will run the integration suite with pytest fixtures for the database "
    "setup, as before."
)


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _ts(seconds: int) -> str:
    """An event stamp `seconds` after T, in the recorder's Z form."""
    base = _dt(T).timestamp() + seconds
    return datetime.fromtimestamp(base, _dt(T).tzinfo).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _view(v: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        started_at=_dt(v.get("at", T)),
        segment=v.get("n", 1),
        settled=set(v.get("settled", [])),
        review_roots=set(v.get("roots", [])),
        shown_by={k: tuple(x) for k, x in v.get("by", {}).items()},
    )


class _Fakes:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.log: list[Any] = []

    # the store
    def events_of_kinds(self, kinds: Any, *, since: datetime) -> list[dict[str, Any]]:
        self.log.append(["eok", list(kinds), since])
        return [dict(e) for e in self.case.get("ev", [])]

    def events_since(self, since: datetime) -> list[dict[str, Any]]:
        self.log.append(["es", since])
        return [dict(e) for e in self.case.get("recent", [])]

    def load_one(self, memory_id: str) -> Any:
        self.log.append(["load", memory_id])
        body = self.case.get("bodies", {}).get(memory_id, "!nf")
        if body == "!nf":
            raise MemoryNotFoundError(memory_id)
        if body == "!tomb":
            raise TombstonedError(memory_id)
        return SimpleNamespace(body=body)

    # the recorder
    def record(self, kind: str, **fields: Any) -> None:
        self.log.append(["rec", kind, fields])

    # the state
    def segment(self, store: Any, key: tuple[str, str], *, now: datetime) -> Any:
        self.log.append(["seg", list(key), now])
        return _view(self.case.get("seg", {}))

    def take_closed(self, key: tuple[str, str]) -> Any:
        self.log.append(["take", list(key)])
        closed = self.case.get("closed")
        return None if closed is None else _view(closed)

    def note_settled(
        self, key: tuple[str, str], ids: list[str], *, now: datetime
    ) -> None:
        self.log.append(["settled", list(key), list(ids), now])

    def note_review_roots(
        self, key: tuple[str, str], roots: list[str], *, now: datetime
    ) -> None:
        self.log.append(["roots", list(key), list(roots), now])

    # the closures
    def recorder(self, **kw: Any) -> Any:
        self.log.append(
            [
                "recorder",
                kw["session_id"],
                kw["enabled"],
                kw["worktree_root"],
                kw["triggered_from"],
            ]
        )
        return self

    def joined(
        self, events_: list[Any], *, harness_session: str, worktree_root: str | None
    ) -> set[str]:
        self.log.append(["joined", len(events_), harness_session, worktree_root])
        return set(self.case.get("joined", []))

    def find_chain(self, store: Any, root: str) -> Any:
        self.log.append(["chain", root])
        found = self.case.get("chains", {}).get(root, [True, False])
        return SimpleNamespace(open=found[0], standing_refused=found[1])


def _plain(value: Any) -> Any:
    """The compact form of a logged value: a `str`, `None`, `bool` and
    `int` as JSON, a list as a list, a dict as ``{"d": [[key, value],
    ...]}`` in order, a tuple as ``{"tu": [...]}``, a datetime as ``{"dt":
    isoformat}``."""
    if value is None or isinstance(value, (bool, int, str)):
        assert not isinstance(value, str) or not any(
            0xD800 <= ord(c) < 0xE000 for c in value
        )
        return value
    if isinstance(value, datetime):
        return {"dt": value.isoformat()}
    if isinstance(value, tuple):
        return {"tu": [_plain(v) for v in value]}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {"d": [[_plain(k), _plain(v)] for k, v in value.items()]}
    raise TypeError(type(value).__name__)


def _answer(call: Any) -> Any:
    try:
        return _plain(call())
    except (OverflowError, TypeError) as exc:
        return {"err": type(exc).__name__}


def _run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    f = _Fakes(case)
    now = _dt(case.get("now", T))
    if kind == "shown":

        def call() -> Any:
            return recall_settle.settle_shown(
                store=f,  # type: ignore[arg-type]
                recorder=f,  # type: ignore[arg-type]
                state=f,  # type: ignore[arg-type]
                session_id=case.get("sid", "S"),
                assistant_response=case.get("reply", REPLY),
                sessions=case.get("ss", []),
                harness=case.get("h"),
                exclude=case.get("ex", ()),
                prompt_id=case.get("pid"),
                client_model=case.get("cm"),
                now=now,
            )

    elif kind == "unused":

        def call() -> Any:
            with (
                mock.patch.object(recall_settle, "Recorder", f.recorder),
                mock.patch.object(recall_settle, "joined_sessions", f.joined),
            ):
                return recall_settle.settle_unused(
                    store=f,  # type: ignore[arg-type]
                    state=f,  # type: ignore[arg-type]
                    key=tuple(case.get("key", ["claude-code", "S"])),
                    now=now,
                    worktree_root=case.get("wt"),
                    triggered_from=case.get("tf", "stop_hook"),
                    telemetry=case.get("tel", True),
                )

    elif kind == "prev":

        def call() -> Any:
            return recall_settle.previous_audit_at(
                f,  # type: ignore[arg-type]
                case.get("sid", "S"),
                now=now,
            )

    else:

        def call() -> Any:
            with mock.patch.object(review, "find_chain", f.find_chain):
                return recall_settle.review_unanswered(
                    store=f,  # type: ignore[arg-type]
                    recorder=f,  # type: ignore[arg-type]
                    state=f,  # type: ignore[arg-type]
                    session_id=case.get("sid", "S"),
                    sessions=set(case.get("ss", ["S"])),
                    since=_dt(case.get("since", T)),
                    harness=case.get("h"),
                    now=now,
                )

    out = _answer(call)
    return {"k": kind, "c": case, "log": _plain(f.log), "o": out}


_ID_VALUES: list[Any] = [
    None,
    "",
    "m1",
    "\ud800",
    3,
    0,
    True,
    1.5,
    [],
    ["a", "b"],
    ["a", 1, None, "b", ["c"], {"d": 1}, "", "a"],
    [["x"]],
    ("a", "b"),
    {"a": 1},
    {"ids": ["a"]},
]


def _used_events() -> list[dict[str, Any]]:
    def use(**kw: Any) -> dict[str, Any]:
        return {"kind": "use", "ts": _ts(5), **kw}

    evs = [
        use(session="S", ids=["a"]),
        use(session="X", ids=["b"]),
        use(session="X", harness_session="H", ids=["c"]),
        use(session="X", harness_session="other", ids=["d"]),
        use(session="", session_id="J", memory_ids=["e"]),
        use(session="J", ids=[], memory_ids=["f"]),
        use(session="J", ids="g"),
        use(session="J", ids=["h", 3, ["i"]]),
        {"kind": "use", "ts": _ts(-1), "session": "S", "ids": ["early"]},
        {"kind": "use", "ts": _ts(0), "session": "S", "ids": ["edge"]},
        {"kind": "use", "ts": "not a time", "session": "S", "ids": ["badts"]},
        {"kind": "use", "ts": 5, "session": "S", "ids": ["intts"]},
        {"kind": "use", "session": "S", "ids": ["nots"]},
        {"kind": "show", "ts": _ts(5), "session": "S", "ids": ["show"]},
        {"kind": "Use", "ts": _ts(5), "session": "S", "ids": ["Use"]},
        use(session=["S"], ids=["listsession"]),
        use(session_id="S", ids=["legacy"]),
        {
            "kind": "use",
            "ts": "2026-01-01T13:00:00+01:00",
            "session": "S",
            "ids": ["offset"],
        },
        {"kind": "use", "ts": "2026-01-01T12:00:00", "session": "S", "ids": ["naive"]},
    ]
    return evs


def _used_rows() -> list[dict[str, Any]]:
    evs = _used_events()
    rows = []
    for hs, ss in (("H", ["S", "J"]), ("", ["S"]), ("H", []), ("S", ["J"])):
        rows.append(
            {
                "k": "used",
                "since": T,
                "hs": hs,
                "ss": ss,
                "o": sorted(
                    recall_settle._used_in_segment(
                        evs, since=_dt(T), harness_session=hs, sessions=set(ss)
                    )
                ),
            }
        )
    return rows


def _shown_cases() -> list[dict[str, Any]]:
    by = {"m1": ["brief", "E1"], "m2": ["recall", None], "m3": ["recall", "E3"]}
    bodies = {"m1": BODY, "m2": BODY, "m3": BODY}
    seg = {"at": T, "n": 4, "by": by}
    use_m2 = {"kind": "use", "ts": _ts(3), "session": "S", "ids": ["m2"]}
    return [
        {"seg": {"by": {}}},
        {"seg": seg, "bodies": bodies, "reply": ""},
        {"seg": seg, "bodies": bodies},
        {"seg": seg, "bodies": bodies, "h": "", "pid": "", "cm": ""},
        {"seg": seg, "bodies": bodies, "h": "codex", "pid": "P1", "cm": "model-x"},
        {"seg": {**seg, "settled": ["m1", "m3"]}, "bodies": bodies},
        {"seg": seg, "bodies": bodies, "ex": ["m1", "m2", "m3"]},
        {"seg": seg, "bodies": bodies, "ex": ["m2"]},
        {"seg": seg, "bodies": bodies, "ev": [use_m2]},
        {"seg": seg, "bodies": bodies, "ev": [{**use_m2, "session": "J"}], "ss": ["J"]},
        {"seg": seg, "bodies": bodies, "ev": [{**use_m2, "session": "J"}]},
        {
            "seg": seg,
            "bodies": bodies,
            "sid": "H",
            "ev": [{**use_m2, "session": "Z", "harness_session": "H"}],
        },
        {"seg": seg, "bodies": bodies, "ev": [{**use_m2, "ts": _ts(-3)}]},
        {"seg": seg, "bodies": {"m1": "!nf", "m2": "!tomb", "m3": BODY}},
        {"seg": seg, "bodies": {"m1": "!nf", "m2": "!tomb"}},
        {
            "seg": seg,
            "bodies": {"m1": "Something else entirely.", "m2": "", "m3": BODY},
        },
        {"seg": seg, "bodies": bodies, "reply": "Nothing in common here."},
        {
            "seg": {"at": T, "n": None, "by": {"b": ["brief", 7], "a": ["x", ["y"]]}},
            "bodies": {"a": BODY, "b": BODY},
        },
    ]


def _unused_cases() -> list[dict[str, Any]]:
    by = {
        "m1": ["brief", "E1"],
        "m2": ["recall", None],
        "m3": ["recall", "E3"],
        "m4": ["brief", None],
    }
    closed = {"at": T, "n": 2, "by": by, "settled": ["m4"]}
    show = {"kind": "show", "ts": _ts(4), "session": "S", "id": "m1"}
    use = {"kind": "use", "ts": _ts(4), "session": "S", "ids": ["m2"]}
    return [
        {},
        {"closed": {"at": T, "n": 2, "by": {"m1": ["brief", None]}, "settled": ["m1"]}},
        {"closed": closed},
        {
            "closed": closed,
            "ev": [show, use],
            "recent": [show],
            "tel": False,
            "wt": "/w",
            "tf": "prompt_hook",
        },
        {"closed": closed, "ev": [{**show, "session": "J"}, {**use, "session": "J"}]},
        {
            "closed": closed,
            "ev": [{**show, "session": "J"}, {**use, "session": "J"}],
            "joined": ["J"],
        },
        {"closed": closed, "ev": [{**show, "harness_session": "S", "session": "Z"}]},
        {"closed": closed, "ev": [{**show, "ts": _ts(-1)}, {**use, "ts": "bad"}]},
        {
            "closed": closed,
            "ev": [{**show, "id": 5}, {**show, "id": ["m3"]}, {**show, "kind": "use"}],
        },
        {"closed": closed, "ev": [{**use, "kind": "show", "id": "m3"}]},
        {"closed": closed, "key": ["", "S"]},
        {"closed": closed, "now": "0001-01-01T00:30:00+00:00"},
        {"closed": closed, "now": "0001-01-01T01:00:00+00:00"},
    ]


def _prev_cases() -> list[dict[str, Any]]:
    def ta(seconds: Any, **kw: Any) -> dict[str, Any]:
        ts = _ts(seconds) if isinstance(seconds, int) else seconds
        return {"kind": "turn_audited", "ts": ts, "session": "S", **kw}

    return [
        {},
        {"ev": [ta(-5), ta(-2), ta(-9)]},
        {"ev": [ta(-5), ta(-2, session="X"), ta(-3, session="", session_id="S")]},
        {"ev": [ta(-5, session=None, session_id="S"), ta(-4, session=["S"])]},
        {"ev": [ta("2026-01-01T11:00:00+00:00"), ta("2026-01-01T12:00:00+01:00")]},
        {"ev": [ta("bad"), ta(7), {"kind": "turn_audited", "session": "S"}]},
        {"ev": [ta(-5, kind="other")]},
        {"now": "0001-01-01T12:00:00+00:00"},
        {"now": "0001-01-02T00:00:00+00:00"},
        {"ev": [ta(-5)], "sid": ""},
        {"ev": [ta(-5, session="", session_id="")], "sid": ""},
    ]


def _review_cases() -> list[dict[str, Any]]:
    def ri(seconds: int = 5, **kw: Any) -> dict[str, Any]:
        return {"kind": review.REVIEW_ISSUED, "ts": _ts(seconds), "session": "S", **kw}

    return [
        {"ss": []},
        {},
        {"ev": [ri(review_id=ULID_A)]},
        {"ev": [ri(review_id=ULID_A)], "h": "codex", "seg": {"n": 9}},
        {"ev": [ri(review_id=ULID_A, parent_review_id=ULID_B), ri(review_id=ULID_B)]},
        {"ev": [ri(review_id=ULID_A, parent_review_id="not-a-ulid")]},
        {"ev": [ri(review_id=ULID_A, parent_review_id=5)]},
        {"ev": [ri(review_id=ULID_A.lower())]},
        {"ev": [ri(review_id=5), ri()]},
        {"ev": [ri(-1, review_id=ULID_A), ri(0, review_id=ULID_B)]},
        {
            "ev": [
                ri(review_id=ULID_A, session="X"),
                ri(review_id=ULID_B, session="", session_id="S"),
            ]
        },
        {"ev": [ri(review_id=ULID_A)], "seg": {"roots": [ULID_A]}},
        {
            "ev": [ri(review_id=ULID_A), ri(review_id=ULID_B), ri(review_id=ULID_C)],
            "chains": {ULID_A: [False, False], ULID_B: [True, True]},
        },
        {"ev": [ri(review_id=ULID_A)], "chains": {ULID_A: [False, True]}},
        {"ev": [ri(review_id=ULID_A), ri(review_id=ULID_B, session=["S"])]},
        {"ev": [ri(review_id=ULID_A, session=["S"])], "ss": []},
        {"ev": [ri(review_id=ULID_A, session={"a": 1})]},
        {"ev": [ri(review_id=ULID_A, session=7)]},
        {"ev": [ri(review_id=ULID_A, session=[])]},
        {
            "ev": [
                ri(review_id=ULID_A, session="J"),
                ri(review_id=ULID_B, session="K"),
            ],
            "ss": ["K", "J"],
        },
        {"ev": [ri(review_id=ULID_A, harness_session="S", session="Z")]},
        {"ev": [{"kind": "other", "ts": _ts(5), "session": "S", "review_id": ULID_A}]},
        {"ev": [ri(review_id=ULID_A, ts="bad")]},
    ]


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for v in _ID_VALUES:
        items = events._event_id_items(v)
        rows.append(
            {
                "k": "ids",
                "v": neutral(v),
                "o": neutral([list(p) for p in items]),
                "l": neutral(events._event_id_list(v)),
            }
        )
        assert recall_state._str_ids(v) == events._event_id_list(v)
    rows.extend(_used_rows())
    for kind, cases in (
        ("shown", _shown_cases()),
        ("unused", _unused_cases()),
        ("prev", _prev_cases()),
        ("review", _review_cases()),
    ):
        rows.extend(_run(kind, case) for case in cases)
    applied = [r for r in rows if r["k"] == "shown" and r["o"]]
    assert len(applied) >= 5, "the matcher settles too few cases"
    header = {
        "k": "consts",
        "rule": recall_state.RULE,
        "default_harness": recall_state.DEFAULT_HARNESS,
        "state_idle_seconds": recall_state.STATE_IDLE_SECONDS,
        "review_unanswered": recall_state.REVIEW_UNANSWERED,
        "reaudit_dedup_window_seconds": audit.REAUDIT_DEDUP_WINDOW_SECONDS,
        "review_issued": review.REVIEW_ISSUED,
        "used_ev": _used_events(),
        "count": len(rows),
    }
    return {"recall_settle.jsonl": jsonl([header, *rows])}
