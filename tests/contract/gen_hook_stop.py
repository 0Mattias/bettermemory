"""Fixtures: the Stop audit's front half (``hook_stop.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/hook_stop.rs``. This pins `hook`'s
`run_audit` (with its ``@hook_body("stop")``), `_audit_turn`,
`_stop_window`, `_stop_pool`, `_stop_probe`, `_stop_recorder` and
`_is_repeat_audit`: the order of the calls they make, and the arguments of
each.

Every name those functions resolve outside themselves is replaced by a
recording fake (`events.within_hook_budget`, `suppressed`, `load_config`,
`Store`, `_utcnow`, `utcnow`, `identity.bind_transcript`, `_origin_for`,
`bridged_session`, `recall.caller_admission`,
`_disabled_scopes_from_events`, `no_owner_prompt_report`,
`recall_settle._probe_message`, `Recorder`, `recall.RecallState`,
`recall.confirm_compaction`, `recall.previous_audit_at`, `redact_query`,
`is_duplicate_audit`, and the settlement steps `_settle_stop_turn`,
`_record_stop_rows`, `_count_stop_reviews`, `_settle_stop_interrupted`).
A fake object is its label; a clock read is ``U<k>`` (`_utcnow`) or
``N<k>`` (`utcnow`), and a time less a timedelta is ``<label>-<seconds>``.
A path is a label too, and ``/`` joins labels with ``/``: no host path is
spelled, so every platform writes the same bytes.

A case is the arguments of `run_audit` (``a``) and the world (``w``):
``cfg`` (the config's label, ``search_mode`` and ``telemetry``),
``window`` (what `events_since` answers), ``wr`` (the origin's
``worktree_root``), ``ss`` (`bridged_session`), ``scopes``, ``prev``
(`previous_audit_at`), ``dup`` (`is_duplicate_audit`), ``settled``
(`_settle_stop_turn`) and ``raise`` (the fake that raises ``Boom`` once
it has logged its call). Each row answers ``log`` (every call, in order:
``[name, {argument: value}]``, a positional argument under ``_<i>``) and
``r`` (what `run_audit` returned) or ``x`` (the label of what it raised).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest import mock

from bettermemory import events, hook, identity, recall, recall_settle
from bettermemory.membership import POOL_PREFILTER

from ._common import jsonl


class Boom(Exception):
    pass


class Label:
    def __init__(self, label: str) -> None:
        self.label = label

    def __truediv__(self, other: str) -> Label:
        return Label(f"{self.label}/{other}")


class FakeTime(Label):
    def __sub__(self, other: timedelta) -> FakeTime:
        return FakeTime(f"{self.label}-{int(other.total_seconds())}")


class FakeStore(Label):
    def __init__(self, label: str, world: World) -> None:
        super().__init__(label)
        self.world = world

    def set_busy_timeout(self, _ms: int) -> None:  # pragma: no cover - never called
        raise AssertionError("the budget is a fake")

    def events_since(self, since: Any) -> list[str]:
        self.world.call("events_since", self, since)
        return list(self.world.w.get("window", []))


class FakeCfg(Label):
    def __init__(self, world: World) -> None:
        spec = world.w.get("cfg", {})
        super().__init__(spec.get("label", "C"))
        self.world = world
        self.behavior = mock.Mock(search_mode=spec.get("search_mode"))
        self.telemetry = mock.Mock(enabled=spec.get("telemetry", True))

    def resolved_directory(self) -> Label:
        self.world.call("resolved_directory", self)
        return Label("D")


class FakeReport(Label):
    def __init__(self, label: str, world: World) -> None:
        super().__init__(label)
        self.world = world

    def to_dict(self) -> dict[str, Any]:
        self.world.call("to_dict", self)
        return {"report": self.label}


def _lab(value: Any) -> Any:
    if isinstance(value, Label):
        return value.label
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
        self.clock = {"U": 0, "N": 0}

    def call(self, name: str, *args: Any, **kw: Any) -> None:
        shown = {f"_{i}": _lab(a) for i, a in enumerate(args)}
        shown.update({k: _lab(v) for k, v in kw.items()})
        self.log.append([name, shown])
        if self.w.get("raise") == name:
            raise Boom(name)

    def tick(self, kind: str) -> FakeTime:
        self.clock[kind] += 1
        return FakeTime(f"{kind}{self.clock[kind]}")


def _fakes(world: World) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()

    @contextlib.contextmanager
    def within_hook_budget(event: str, store: Any = None) -> Iterator[None]:
        world.call("within_hook_budget", event, store)
        try:
            yield
        finally:
            world.log.append(["within_hook_budget_exit", {"_0": event}])

    @contextlib.contextmanager
    def suppressed() -> Iterator[None]:
        world.call("suppressed")
        try:
            yield
        finally:
            world.log.append(["suppressed_exit", {}])

    def load_config(path: Any) -> FakeCfg:
        world.call("load_config", path)
        return FakeCfg(world)

    class FakeStoreClass:
        @staticmethod
        def open_or_create(path: Any) -> FakeStore:
            world.call("open_or_create", path)
            return FakeStore("S0", world)

    def _utcnow() -> FakeTime:
        world.call("_utcnow")
        return world.tick("U")

    def utcnow() -> FakeTime:
        world.call("utcnow")
        return world.tick("N")

    def bind_transcript(**kw: Any) -> None:
        world.call("bind_transcript", **kw)

    def _origin_for(cwd: Any) -> Any:
        world.call("_origin_for", cwd)
        origin = Label("O")
        origin.worktree_root = world.w.get("wr")  # type: ignore[attr-defined]
        return origin

    def bridged_session(recent: Any, **kw: Any) -> Any:
        world.call("bridged_session", recent, **kw)
        return world.w.get("ss")

    def caller_admission(prefilter: Any, **kw: Any) -> Any:
        world.call("caller_admission", prefilter is POOL_PREFILTER, **kw)
        return Label("A"), Label("resolution")

    def _disabled_scopes_from_events(recent: Any, **kw: Any) -> set[str]:
        world.call("_disabled_scopes_from_events", recent, **kw)
        return set(world.w.get("scopes", []))

    def no_owner_prompt_report(**kw: Any) -> FakeReport:
        world.call("no_owner_prompt_report", **kw)
        return FakeReport("R-none", world)

    def _probe_message(**kw: Any) -> FakeReport:
        world.call("_probe_message", **kw)
        return FakeReport("R-probe", world)

    def recorder(**kw: Any) -> Label:
        world.call("Recorder", **kw)
        return Label("Rec")

    def recall_state() -> Label:
        world.call("RecallState")
        return Label("RS-new")

    def confirm_compaction(**kw: Any) -> None:
        world.call("confirm_compaction", **kw)

    def previous_audit_at(store: Any, session_id: Any, **kw: Any) -> Any:
        world.call("previous_audit_at", store, session_id, **kw)
        prev = world.w.get("prev")
        return None if prev is None else FakeTime(prev)

    def redact_query(text: str) -> dict[str, Any]:
        world.call("redact_query", text)
        return {"hash": f"h:{text}", "other": "unread"}

    def is_duplicate_audit(recent: Any, **kw: Any) -> bool:
        world.call("is_duplicate_audit", recent, **kw)
        return bool(world.w.get("dup", False))

    def settle_stop_turn(**kw: Any) -> int:
        world.call("_settle_stop_turn", **kw)
        return int(world.w.get("settled", 0))

    def record_stop_rows(*args: Any, **kw: Any) -> None:
        world.call("_record_stop_rows", *args, **kw)

    def count_stop_reviews(*args: Any, **kw: Any) -> None:
        world.call("_count_stop_reviews", *args, **kw)

    def settle_stop_interrupted(**kw: Any) -> None:
        world.call("_settle_stop_interrupted", **kw)

    patches: list[tuple[Any, str, Any]] = [
        (events, "within_hook_budget", within_hook_budget),
        (hook, "suppressed", suppressed),
        (hook, "load_config", load_config),
        (hook, "Store", FakeStoreClass),
        (hook, "_utcnow", _utcnow),
        (hook, "utcnow", utcnow),
        (identity, "bind_transcript", bind_transcript),
        (hook, "_origin_for", _origin_for),
        (hook, "bridged_session", bridged_session),
        (recall, "caller_admission", caller_admission),
        (hook, "_disabled_scopes_from_events", _disabled_scopes_from_events),
        (hook, "no_owner_prompt_report", no_owner_prompt_report),
        (recall_settle, "_probe_message", _probe_message),
        (hook, "Recorder", recorder),
        (recall, "RecallState", recall_state),
        (recall, "confirm_compaction", confirm_compaction),
        (recall, "previous_audit_at", previous_audit_at),
        (hook, "redact_query", redact_query),
        (hook, "is_duplicate_audit", is_duplicate_audit),
        (hook, "_settle_stop_turn", settle_stop_turn),
        (hook, "_record_stop_rows", record_stop_rows),
        (hook, "_count_stop_reviews", count_stop_reviews),
        (hook, "_settle_stop_interrupted", settle_stop_interrupted),
    ]
    for owner, name, fake in patches:
        stack.enter_context(mock.patch.object(owner, name, fake))
    return stack


_BASE: dict[str, Any] = {
    "user_message": "find the notes",
    "assistant_response": "here they are",
    "session_id": "sess-1",
    "client_model": "model-a",
    "config": True,
    "store": True,
    "cwd": "cwd",
    "harness": "codex",
    "prompt_id": "p-1",
    "reply_source": "payload",
    "transcript_path": "T",
    "state": True,
    "posted": "posted",
}
_WORLD: dict[str, Any] = {
    "cfg": {"label": "C", "search_mode": "bm25", "telemetry": True},
    "window": ["e1", "e2"],
    "wr": "W",
    "ss": "srv",
    "scopes": ["b", "a"],
    "prev": "P",
    "dup": False,
    "settled": 2,
}


def _case(
    a: dict[str, Any] | None = None, w: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"a": {**_BASE, **(a or {})}, "w": {**_WORLD, **(w or {})}}


def _cases() -> list[dict[str, Any]]:
    out = [
        _case(),
        # No owner prompt: the empty report, no redact, no dedup ask.
        _case({"user_message": None}),
        # An empty prompt is a prompt: probed and asked about.
        _case({"user_message": ""}),
        # No reply, and a reply from the tail.
        _case({"assistant_response": None, "reply_source": None}),
        _case({"assistant_response": "", "reply_source": "tail"}),
        _case({"reply_source": "tail"}),
        # A repeat audit.
        _case(w={"dup": True}),
        _case({"user_message": None}, {"dup": True}),
        # The default store and config.
        _case({"store": None}),
        _case({"config": None}),
        _case(_RAISE_BASE),
        # The harness's default: None and the empty string both read as it.
        _case({"harness": None}),
        _case({"harness": ""}),
        # The search mode's default: None and the empty string.
        _case(w={"cfg": {"label": "C", "search_mode": None, "telemetry": False}}),
        _case(w={"cfg": {"label": "C", "search_mode": "", "telemetry": True}}),
        # No state: a fresh one.
        _case({"state": None}),
        # Nothing known: no session, no root, no scopes, no previous audit.
        _case(
            {
                "client_model": None,
                "cwd": None,
                "posted": None,
                "prompt_id": None,
                "transcript_path": None,
            },
            {"wr": None, "ss": None, "scopes": [], "prev": None, "window": []},
        ),
        # Dry runs: suppressed, recursed once, state None.
        _case({"dry_run": True}),
        _case({"dry_run": True, "state": None, "store": None, "config": None}),
        _case({"dry_run": True, "user_message": None}),
    ]
    for name in (
        "within_hook_budget",
        "load_config",
        "resolved_directory",
        "open_or_create",
        "_utcnow",
        "events_since",
        "bind_transcript",
        "_origin_for",
        "bridged_session",
        "caller_admission",
        "_disabled_scopes_from_events",
        "_probe_message",
        "Recorder",
        "utcnow",
        "RecallState",
        "confirm_compaction",
        "previous_audit_at",
        "redact_query",
        "is_duplicate_audit",
        "_settle_stop_turn",
        "_record_stop_rows",
        "_count_stop_reviews",
        "_settle_stop_interrupted",
        "to_dict",
    ):
        out.append(_case(_RAISE_BASE, {"raise": name}))
    out.append(_case({"user_message": None}, {"raise": "no_owner_prompt_report"}))
    out.append(_case({"dry_run": True}, {"raise": "suppressed"}))
    out.append(_case({"dry_run": True}, {"raise": "_probe_message"}))
    return out


def _run(case: dict[str, Any]) -> dict[str, Any]:
    world = World(case["w"])
    a = dict(case["a"])
    if a["config"]:
        a["config"] = FakeCfg(world)
    if a["store"]:
        a["store"] = FakeStore("S", world)
    if a["state"]:
        a["state"] = Label("RS")
    if a["cwd"] is not None:
        a["cwd"] = Label(a["cwd"])
    if a["posted"] is not None:
        a["posted"] = Label(a["posted"])
    row: dict[str, Any] = {**case}
    with _fakes(world):
        try:
            row["r"] = hook.run_audit(**a)
        except Boom as exc:
            row["x"] = str(exc)
    row["log"] = world.log
    return row


_RAISE_BASE = {"store": None, "config": None, "state": None}


def _compact(row: dict[str, Any], base: list[Any]) -> dict[str, Any]:
    """A raising case's log as the length it shares with the raise-free
    case over the same arguments (``same``) and what follows (``log``)."""
    log = row["log"]
    same = 0
    while same < min(len(log), len(base)) and log[same] == base[same]:
        same += 1
    return {**row, "same": same, "log": log[same:]}


def generate() -> dict[str, bytes]:
    base = _run(_case(_RAISE_BASE))["log"]
    rows = []
    for case in _cases():
        row = _run(case)
        if "raise" in case["w"] and case["a"] == {**_BASE, **_RAISE_BASE}:
            row = _compact(row, base)
        rows.append(row)
    header = {"cases": len(rows), "file": "hook_stop"}
    return {"hook_stop.jsonl": jsonl([header, *rows])}
