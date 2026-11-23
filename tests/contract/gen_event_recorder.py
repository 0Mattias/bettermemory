"""Fixtures: `events.Recorder` over a fake store (``event_recorder.jsonl``
and ``event_recorder_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/event_recorder.rs``. This pins it to
`Recorder.session_for_request`, `worktree_for_request`, `records` and
`record`.

Each row runs one recorder in a fresh context:

- ``rc``: the recorder's ``session_id``, ``enabled``, ``worktree_root``,
  ``attribution`` and ``triggered_from``;
- ``pub``: the published caller (``client``, ``session``, its ``source``,
  ``workspace`` path and source, ``harness``), or null for none
  published; with none published the workspace variables of the
  environment are removed for the row, so nothing is declared;
- ``sup``: run inside `events.suppressed`; ``dl``: a clock reading, the
  row running inside `within_hook_budget("stop")` with `time.monotonic`
  stubbed to it (null: outside any hook body);
- ``cap``: what `_handlers.capture_origin().worktree_root` answers, or
  ``{"raise": text}`` for a `RuntimeError`;
- ``fail``: null, or the class and text the fake store raises; optional
  ``warning_error`` makes the logging handler raise after receiving the warning;
- ``kind`` (code points when it holds a surrogate) and ``f``, the fields
  (`neutral` pairs);
- ``r``: ``records(kind)``, each store call as ``[kind, [[name, value],
  ...]]`` with ``actor`` spelt ``{"t": "published"}`` when it is the
  published caller's actor object, the warnings logged by
  ``bettermemory.events`` in order, and ``raised`` (class and text, or
  null); with ``session_for_request`` and ``worktree_for_request`` asked
  directly (``sfr``, ``wfr``; ``wfr`` is ``{"raise": text}`` when the
  capture raises).

A row where a field collides with a keyword the recorder passes holds
CPython's text for the `TypeError`, which names the callable as the
release spells it, so those rows go to ``event_recorder_python.jsonl``.

Every published caller, ContextVar and environment change is set and
reset inside its row.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import _handlers, events, identity

from ._common import captured_by, jsonl, neutral, text_field

_WORKSPACE_ENV = (identity.ENV_WORKSPACE, identity.ENV_HARNESS_SESSION)


def _message(value: str) -> Any:
    encoded = text_field("message", value)
    return encoded.get("message", encoded)


class _Sink:
    def __init__(self, fail: list[str] | None, published: Any) -> None:
        self.fail = fail
        self.published = published
        self.calls: list[Any] = []

    def record_event(self, kind: str, **kw: Any) -> None:
        spelt = []
        for name, value in kw.items():
            if name == "actor" and value is not None and value is self.published:
                spelt.append([name, {"t": "published"}])
            else:
                spelt.append([name, neutral(value)])
        self.calls.append([text_field("kind", kind), spelt])
        if self.fail is not None:
            cls = {"OSError": OSError, "ValueError": ValueError}[self.fail[0]]
            raise cls(self.fail[1])


class _Logged(logging.Handler):
    def __init__(self, failure: str | None) -> None:
        super().__init__(logging.WARNING)
        self.failure = failure
        self.lines: list[Any] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(text_field("w", record.getMessage()))
        if self.failure is not None:
            raise RuntimeError(self.failure)


def _caller(pub: dict[str, Any]) -> identity.Caller:
    sources = {} if pub.get("source") is None else {"session": pub["source"]}
    if pub.get("client") is not None:
        sources["client"] = "header"
    actor = identity.Actor(
        client=pub.get("client"), session=pub.get("session"), sources=sources
    )
    ws = pub.get("workspace")
    workspace = (
        identity.Workspace()
        if ws is None
        else identity.Workspace(path=ws[0], source=ws[1])
    )
    return identity.Caller(
        actor=actor, workspace=workspace, harness_session=pub.get("harness")
    )


def _run(case: dict[str, Any]) -> dict[str, Any]:
    return contextvars.Context().run(_run_here, case)


def _run_here(case: dict[str, Any]) -> dict[str, Any]:
    pub = case["pub"]
    caller = None if pub is None else _caller(pub)
    published_actor = None if caller is None else caller.actor
    sink = _Sink(case["fail"], published_actor)
    rc = case["rc"]
    recorder = events.Recorder(
        store=sink,  # type: ignore[arg-type]
        session_id=rc[0],
        enabled=rc[1],
        worktree_root=rc[2],
        attribution=rc[3],
        triggered_from=rc[4],
    )
    cap = case["cap"]

    def capture() -> Any:
        if isinstance(cap, dict):
            raise RuntimeError(cap["raise"])
        return SimpleNamespace(worktree_root=cap)

    handler = _Logged(case.get("warning_error"))
    out: dict[str, Any] = {}
    env = {k: v for k, v in os.environ.items() if k not in _WORKSPACE_ENV}
    token = None if caller is None else identity.publish(caller)
    try:
        with (
            captured_by(events.log, handler, logging.WARNING),
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(_handlers, "capture_origin", capture),
        ):
            out["sfr"] = recorder.session_for_request()
            try:
                out["wfr"] = recorder.worktree_for_request()
            except RuntimeError as exc:
                out["wfr"] = {"raise": _message(str(exc))}
            _inside(recorder, case, out)
    finally:
        if token is not None:
            identity.reset(token)
    out["calls"] = sink.calls
    out["warn"] = handler.lines
    return out


def _inside(
    recorder: events.Recorder, case: dict[str, Any], out: dict[str, Any]
) -> None:
    def body() -> None:
        out["records"] = recorder.records(case["kind"])
        try:
            recorder.record(case["kind"], **case["fields"])
            out["raised"] = None
        except Exception as exc:  # noqa: BLE001 - the row records what was raised
            out["raised"] = [type(exc).__name__, _message(str(exc))]

    def timed() -> None:
        if case["dl"] is None:
            body()
            return
        with (
            mock.patch.object(time, "monotonic", lambda: case["dl"]),
            events.within_hook_budget("stop"),
        ):
            body()

    if case["sup"]:
        with events.suppressed():
            timed()
    else:
        timed()


RC_PLAIN = ["proc-s", True, None, "", ""]
RC_OFF = ["proc-s", False, "/w/proc", "", ""]
RC_STAMPED = ["proc-s", True, "/w/proc", "cli_note", "hook"]
RC_OFF_STAMPED = ["proc-s", False, None, "cli_x", "stop-hook"]

PUBS: dict[str, Any] = {
    "none": None,
    "transport": {"client": "c1", "session": "req-t", "source": "transport"},
    "header": {"session": "req-h", "source": "header", "harness": "hs-1"},
    "env": {"session": "req-e", "source": "env"},
    "nosession": {"client": "c2", "source": "transport"},
    "empty": {},
    "declared": {
        "session": "req-d",
        "source": "header",
        "workspace": ["/w/decl", "header"],
    },
    "cwd": {"client": "c3", "workspace": ["/w/cwd", "process-cwd"]},
    "harness_only": {"harness": "hs-2"},
}

KINDS = [
    "search",
    "write",
    "use",
    "verify",
    "show",
    "update",
    "search_miss",
    "turn_audited",
    "recall_held",
    "capture_grade",
    "unknown_kind",
    "",
    "sea\udcffrch",
]

FIELD_SETS: list[dict[str, Any]] = [
    {},
    {"returned": ["m1", "m2"], "q": 3},
    {"id": "m1", "status": "ok", "extra": [1, 2]},
    {"actor": "handler-actor"},
    {"actor": None, "x": 1},
    {"actor": {"client": "d"}, "session": "given-s"},
    {"session": None},
    {"worktree_root": "/w/given"},
    {"worktree_root": None},
    {"attribution": "given-a", "triggered_from": "given-t"},
    {"harness_session": "given-h"},
    {"probe_query": "where is the config file", "verdict": "miss", "noise": True},
    {"top_hits": [{"id": "a", "score": 1.5}, "x"], "event_id": 7},
    {"forced": True, "policy_acknowledged": 1, "other_acknowledged": 0, "z": "y"},
    {"session_id": "sid", "y": 2.5},
]

DUP_FIELDS: list[dict[str, Any]] = [
    {"best_effort": True},
    {"attempts": 9, "a": 1},
    {"deadline": 1.0},
    {"z": 1, "deadline": 2.0, "best_effort": False},
]


def _case(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rc": RC_PLAIN,
        "pub": None,
        "sup": False,
        "dl": None,
        "cap": "/w/captured",
        "fail": None,
        "kind": "search",
        "fields": {},
    }
    base.update(over)
    return base


def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind in ("write", "recall_held"):
        for rc in (RC_STAMPED, RC_OFF_STAMPED):
            for fields in FIELD_SETS:
                out.append(_case(rc=rc, kind=kind, fields=fields, pub=PUBS["header"]))
    for kind in ("turn_audited", "search_miss"):
        for fields in FIELD_SETS[-4:-1]:
            out.append(_case(rc=RC_OFF, kind=kind, fields=fields, pub=PUBS["header"]))
    for kind in KINDS:
        out.append(
            _case(rc=RC_OFF, kind=kind, fields={"id": "m", "q": 1}, pub=PUBS["header"])
        )
    for pub in PUBS.values():
        out.append(_case(pub=pub, kind="write", fields={"k": 1}))
        out.append(_case(pub=pub, kind="write", cap=None))
        out.append(_case(pub=pub, kind="write", cap={"raise": "no origin"}))
        out.append(_case(pub=pub, kind="recall_held", rc=RC_OFF_STAMPED))
    for kind in ("write", "recall_held"):
        for rc in (RC_PLAIN, RC_OFF):
            for sup, dl in ((False, None), (False, 100.0), (False, -0.25), (True, 3.0)):
                out.append(
                    _case(kind=kind, rc=rc, sup=sup, dl=dl, pub=PUBS["transport"])
                )
            for fail in (["OSError", "disk full"], ["ValueError", ""]):
                out.append(
                    _case(kind=kind, rc=rc, fail=fail, dl=7.0, fields={"id": "m"})
                )
                out.append(_case(kind=kind, rc=rc, fail=fail, pub=PUBS["declared"]))
    for kind in ("write", "recall_held"):
        fail = ["OSError", "path" + chr(0xDCFF)]
        out.append(_case(kind=kind, fail=fail))
        out.append(_case(kind=kind, fail=fail, warning_error="handler" + chr(0xDCFE)))
    out.append(_case(pub=PUBS["declared"], cap={"raise": "origin" + chr(0xDCFF)}))
    return out


def _dup_cases() -> list[dict[str, Any]]:
    out = [
        _case(fields={"kind": 0, "self": 1}, sup=True),
        _case(fields={"self": 1, "kind": 0}, sup=True),
    ]
    for kind in ("write", "recall_held"):
        for rc in (RC_PLAIN, RC_OFF):
            for fields in DUP_FIELDS:
                for dl in (None, 5.0):
                    out.append(_case(kind=kind, rc=rc, fields=fields, dl=dl))
    return out


def _row(case: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in case.items() if k not in ("kind", "fields")}
    if case["fail"] is not None:
        cls, message = case["fail"]
        row["fail"] = [cls, _message(message)]
    if "warning_error" in case:
        row["warning_error"] = _message(case["warning_error"])
    if isinstance(case["cap"], dict):
        row["cap"] = {"raise": _message(case["cap"]["raise"])}
    row.update(text_field("kind", case["kind"]))
    row["f"] = [[k, neutral(v)] for k, v in case["fields"].items()]
    row["r"] = _run(case)
    return row


def generate() -> dict[str, bytes]:
    rows = [_row(c) for c in _cases()]
    header = {"k": "header", "count": len(rows)}
    return {"event_recorder.jsonl": jsonl([header, *rows])}


def generate_python() -> dict[str, bytes]:
    rows = [_row(c) for c in _dup_cases()]
    header = {"k": "header", "count": len(rows)}
    return {"event_recorder_python.jsonl": jsonl([header, *rows])}
