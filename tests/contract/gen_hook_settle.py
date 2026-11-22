"""Fixtures: the Stop audit's settlement half (``hook_settle.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/hook_settle.rs``. This pins `hook`'s
`_settle_stop_turn`, `_record_stop_rows`, `_count_stop_reviews` and
`_settle_stop_interrupted`: the order of the calls they make, the
arguments of each, and what they answer or raise.

Every name those functions resolve outside themselves is replaced by a
recording fake (`joined_sessions`, `_emit_hook_attributions`,
`recall.settle_shown`, `recall.RULE`, `turn_audited_fields`,
`search_miss_fields`, the recorder's `record`, the recall state's
`segment`, the store's `events_of_kinds`, `harness_joined_sessions`,
`recall.review_unanswered`, `recall.settle_interrupted` and the module's
logger). An object is its label; a time is a whole number of seconds
(``FakeTime``), so a time less a timedelta and the comparisons are plain
integer arithmetic.

A row names the function (``fn``), the Stop's state (``s``) and the world
(``w``): ``settled`` and ``shown`` (the ids the two settlements answer),
``miss`` (`report.is_miss`), ``first_seen`` (the segment's start),
``joined`` (`harness_joined_sessions`) and ``raise`` (``[call, class,
text]``: the fake that raises once it has logged its call; ``Boom`` is an
``Exception``, ``Halt`` a ``BaseException`` alone). Each row answers
``log`` (every call, in order: ``[name, {argument: value}]``, a positional
argument under ``_<i>``) and ``r`` (the answer) or ``x`` (the class of
what was raised). Nothing here depends on the Python release or the
platform: the warning's text is the fake exception's own.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any
from unittest import mock

from bettermemory import hook, recall
from bettermemory.events import HARNESS_STAMPED_KINDS

from ._common import jsonl


class Boom(Exception):
    pass


class Halt(BaseException):
    pass


class FakeTime(int):
    def __sub__(self, other: Any) -> FakeTime:
        if isinstance(other, timedelta):
            return FakeTime(int(self) - int(other.total_seconds()))
        return FakeTime(int(self) - int(other))


class Label:
    def __init__(self, label: str) -> None:
        self.label = label


def _lab(value: Any) -> Any:
    if isinstance(value, Label):
        return value.label
    if isinstance(value, FakeTime):
        return int(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_lab(v) for v in value]
    if isinstance(value, dict):
        return {k: _lab(v) for k, v in value.items()}
    return value


class World:
    def __init__(self, w: dict[str, Any]) -> None:
        self.w = w
        self.log: list[Any] = []

    def call(self, name: str, *args: Any, **kw: Any) -> None:
        shown = {f"_{i}": _lab(a) for i, a in enumerate(args)}
        shown.update({k: _lab(v) for k, v in kw.items()})
        self.log.append([name, shown])
        raised = self.w.get("raise")
        if raised and raised[0] == name:
            cls = {"Boom": Boom, "Halt": Halt}[raised[1]]
            raise cls(*raised[2:])


class FakeStore(Label):
    def __init__(self, world: World) -> None:
        super().__init__("S")
        self.world = world

    def events_of_kinds(self, kinds: Any, **kw: Any) -> Label:
        self.world.call("events_of_kinds", kinds is HARNESS_STAMPED_KINDS, **kw)
        return Label("E")


class FakeRecorder(Label):
    def __init__(self, world: World) -> None:
        super().__init__("Rec")
        self.world = world

    def record(self, kind: str, **fields: Any) -> None:
        self.world.call("record", kind, **fields)


class FakeState(Label):
    def __init__(self, world: World) -> None:
        super().__init__("RS")
        self.world = world

    def segment(self, store: Any, key: Any, **kw: Any) -> Any:
        self.world.call("segment", store, list(key), **kw)
        return mock.Mock(started_at=FakeTime(self.world.w["first_seen"]))


class FakeReport(Label):
    def __init__(self, world: World) -> None:
        super().__init__("R")
        self.is_miss = bool(world.w.get("miss", False))


def _fakes(world: World) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()

    def joined_sessions(recent: Any, **kw: Any) -> set[str]:
        world.call("joined_sessions", recent, **kw)
        return {"j2", "j1"}

    def emit(**kw: Any) -> list[str]:
        world.call("_emit_hook_attributions", **kw)
        return list(world.w.get("settled", []))

    def settle_shown(**kw: Any) -> list[str]:
        world.call("settle_shown", **kw)
        return list(world.w.get("shown", []))

    def turn_audited_fields(report: Any, **kw: Any) -> dict[str, Any]:
        world.call("turn_audited_fields", report, **kw)
        return {"f": "TA"}

    def search_miss_fields(report: Any, **kw: Any) -> dict[str, Any]:
        world.call("search_miss_fields", report, **kw)
        return {"f": "SM"}

    def harness_joined(events: Any, session_id: Any) -> set[str]:
        world.call("harness_joined_sessions", events, session_id)
        return set(world.w.get("joined", []))

    def review_unanswered(**kw: Any) -> None:
        world.call("review_unanswered", **kw)

    def settle_interrupted(**kw: Any) -> None:
        world.call("settle_interrupted", **kw)

    class Log:
        @staticmethod
        def warning(msg: str, *args: Any) -> None:
            world.log.append(["warning", {"_0": msg % args}])

    patches: list[tuple[Any, str, Any]] = [
        (hook, "joined_sessions", joined_sessions),
        (hook, "_emit_hook_attributions", emit),
        (recall, "settle_shown", settle_shown),
        (recall, "RULE", "RULE-x"),
        (hook, "turn_audited_fields", turn_audited_fields),
        (hook, "search_miss_fields", search_miss_fields),
        (hook, "harness_joined_sessions", harness_joined),
        (recall, "review_unanswered", review_unanswered),
        (recall, "settle_interrupted", settle_interrupted),
        (hook, "log", Log),
    ]
    for owner, name, fake in patches:
        stack.enter_context(mock.patch.object(owner, name, fake))
    return stack


_STOP: dict[str, Any] = {
    "recent": ["e1", "e2"],
    "session_id": "sess-1",
    "user_message": "find the notes",
    "assistant_response": "here they are",
    "worktree_root": "W",
    "client_model": "model-a",
    "server_session": "srv",
    "harness": "codex",
    "prompt_id": "p-1",
    "reply_source": "payload",
    "transcript_path": "T",
    "now": 100000,
    "previous_audit": 99000,
    "excluded_scopes": ["b", "a"],
    "probe_mode": "bm25",
    "repeat": False,
}
_WORLD: dict[str, Any] = {
    "settled": ["m1", "m2"],
    "shown": ["m3"],
    "miss": True,
    "first_seen": 98000,
    "joined": ["h1"],
}


def _run(fn: str, s: dict[str, Any], w: dict[str, Any]) -> dict[str, Any]:
    world = World(w)
    # The fakes stand in for the reference's types; mypy reads them as Any.
    ref: Any = hook
    store = FakeStore(world)
    recorder = FakeRecorder(world)
    state = FakeState(world)
    now = FakeTime(s["now"])
    prev = None if s["previous_audit"] is None else FakeTime(s["previous_audit"])
    row: dict[str, Any] = {"fn": fn, "s": s, "w": w}
    with _fakes(world):
        try:
            if fn == "settle_stop_turn":
                row["r"] = ref._settle_stop_turn(
                    store=store,
                    recorder=recorder,
                    recent=s["recent"],
                    session_id=s["session_id"],
                    user_message=s["user_message"],
                    assistant_response=s["assistant_response"],
                    worktree_root=s["worktree_root"],
                    client_model=s["client_model"],
                    server_session=s["server_session"],
                    recall_state=state,
                    harness=s["harness"],
                    prompt_id=s["prompt_id"],
                    now=now,
                )
            elif fn == "record_stop_rows":
                row["r"] = ref._record_stop_rows(
                    recorder,
                    FakeReport(world),
                    session_id=s["session_id"],
                    probe_mode=s["probe_mode"],
                    assistant_present=s["assistant_response"] is not None,
                    repeat=s["repeat"],
                    client_model=s["client_model"],
                    prompt_id=s["prompt_id"],
                    reply_source=s["reply_source"],
                    settled_count=s["settled_count"],
                )
            elif fn == "count_stop_reviews":
                row["r"] = ref._count_stop_reviews(
                    store,
                    recorder,
                    state,
                    session_id=s["session_id"],
                    harness=s["harness"],
                    server_session=s["server_session"],
                    previous_audit=prev,
                    now=now,
                )
            else:
                row["r"] = ref._settle_stop_interrupted(
                    store=store,
                    cfg=Label("C"),
                    session_id=s["session_id"],
                    transcript_path=s["transcript_path"],
                    admission=Label("A"),
                    excluded_scopes=set(s["excluded_scopes"]),
                    recent=s["recent"],
                    server_session=s["server_session"],
                    worktree_root=s["worktree_root"],
                    now=now,
                    previous_audit=prev,
                    prompt_id=s["prompt_id"],
                )
        except (Boom, Halt) as exc:
            row["x"] = exc.__class__.__name__
    row["log"] = world.log
    return row


def _case(fn: str, s: dict[str, Any] | None = None, w: dict[str, Any] | None = None):
    return fn, {**_STOP, "settled_count": 3, **(s or {})}, {**_WORLD, **(w or {})}


def _cases() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    turn = "settle_stop_turn"
    rows = "record_stop_rows"
    reviews = "count_stop_reviews"
    inter = "settle_stop_interrupted"
    nothing: dict[str, Any] = {
        "worktree_root": None,
        "client_model": None,
        "server_session": None,
        "harness": None,
        "prompt_id": None,
        "reply_source": None,
        "transcript_path": None,
        "previous_audit": None,
        "excluded_scopes": [],
        "recent": [],
    }
    return [
        # The turn: settled and shown add; no prompt, no reply, an empty
        # reply settle nothing but still join; an empty prompt settles.
        _case(turn),
        _case(turn, {"user_message": None}),
        _case(turn, {"assistant_response": None}),
        _case(turn, {"assistant_response": ""}),
        _case(turn, {"user_message": ""}),
        _case(turn, {"assistant_response": " "}),
        _case(turn, w={"settled": [], "shown": []}),
        _case(turn, w={"settled": ["m1", "m1"], "shown": ["m1"]}),
        _case(turn, nothing),
        _case(turn, w={"raise": ["joined_sessions", "Boom", "j"]}),
        _case(turn, w={"raise": ["_emit_hook_attributions", "Boom", "e"]}),
        _case(turn, w={"raise": ["settle_shown", "Halt", "s"]}),
        # The rows: a miss that is not a repeat records both; a repeat or a
        # hit records the audit alone.
        _case(rows),
        _case(rows, {"repeat": True}),
        _case(rows, w={"miss": False}),
        _case(rows, {"repeat": True}, {"miss": False}),
        _case(rows, {"assistant_response": None, "settled_count": 0}),
        _case(rows, {"assistant_response": ""}),
        _case(rows, nothing),
        _case(rows, w={"raise": ["turn_audited_fields", "Boom", "t"]}),
        _case(rows, w={"raise": ["record", "Boom", "r"]}),
        _case(rows, w={"raise": ["search_miss_fields", "Boom", "m"]}),
        # The reviews: since the previous audit; else the window, raised to
        # the segment's start strictly inside it.
        _case(reviews),
        _case(reviews, {"previous_audit": None}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 96400}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 96401}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 100000}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 99999}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 100001}),
        _case(reviews, {"previous_audit": None}, {"first_seen": 5}),
        _case(reviews, {"previous_audit": None, "harness": None}),
        _case(reviews, {"previous_audit": None, "harness": ""}),
        _case(reviews, {"previous_audit": 100500}),
        _case(reviews, w={"joined": []}),
        _case(reviews, {"server_session": None}, {"joined": []}),
        _case(reviews, {"server_session": ""}, {"joined": []}),
        _case(reviews, {"server_session": None}, {"joined": ["h2", "h1"]}),
        _case(reviews, nothing, {"joined": []}),
        _case(reviews, {"previous_audit": None}, {"raise": ["segment", "Boom", "g"]}),
        _case(reviews, w={"raise": ["events_of_kinds", "Boom", "k"]}),
        _case(reviews, w={"raise": ["harness_joined_sessions", "Boom", "h"]}),
        _case(reviews, w={"raise": ["review_unanswered", "Halt", "u"]}),
        # The interrupted turns: an Exception is one warning; a
        # BaseException alone goes through.
        _case(inter),
        _case(inter, nothing),
        _case(inter, w={"raise": ["settle_interrupted", "Boom", "db is locked"]}),
        _case(inter, w={"raise": ["settle_interrupted", "Boom"]}),
        _case(inter, w={"raise": ["settle_interrupted", "Boom", "a: %s\nb"]}),
        _case(inter, w={"raise": ["settle_interrupted", "Boom", "a", "b"]}),
        _case(inter, w={"raise": ["settle_interrupted", "Halt", "stop"]}),
    ]


def generate() -> dict[str, bytes]:
    rows = [_run(*case) for case in _cases()]
    header = {"cases": len(rows), "file": "hook_settle"}
    return {"hook_settle.jsonl": jsonl([header, *rows])}
