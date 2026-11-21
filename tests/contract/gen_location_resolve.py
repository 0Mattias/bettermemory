"""Fixtures: a caller's resolution (``location_resolve.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_resolve.rs``. This pins it
to `location`'s `_caller`, `resolve_caller`, `resolve_here`, `_hooked`,
`capture_location`, `_model` and `Caller.home` and
`Caller.add_head_witness`, each driven with recording fakes.

Nothing reads the machine. `process_machine`'s `open_machine`, `capture`,
`forget`, `checkout_token.remint`, `_earlier_tree`, `_machineless`,
``os.getcwd`` and `membership.Membership` are the case's; each records
its call in ``calls``, in order.

Each line holds ``k``, the case's inputs and ``r``:

- ``caps``: the captures `capture` answers, in turn, each a dict of
  `Capture` fields;
- ``id``: the identity passed (null: `process_machine`, whose
  `open_machine` answers ``open`` or raises where it is null);
- ``cwd``, ``session``, ``wt`` (`write_token`), ``strict``, ``early``
  (what `_earlier_tree` answers), ``stand`` (null or ``[path,
  checkout]``), ``sr`` (what `session_resolution` answers), ``res`` (what
  `resolve` answers), ``writes`` (the lineage tokens `resolve` asks its
  token writer to re-mint), ``remint`` (``[token, status]``),
  ``fails`` (`record_machine` raises), ``early_raises`` (`_earlier_tree`
  raises), ``model_raises`` (`Membership(...)` raises), ``g`` (what
  ``os.getcwd`` answers, or raises where it is null);
- ``r``: ``calls``, the location's fields ``loc``, ``history`` and the
  resolution's fields ``res``, or ``"raised"``.
"""

from __future__ import annotations

import types
from typing import Any, cast
from unittest import mock

import dataclasses

from bettermemory import checkout_token as CT
from bettermemory import location as L
from bettermemory import machine as M
from bettermemory import membership as MB
from bettermemory.location_git import Capture
from bettermemory.membership_records import Location, Resolution

from ._common import jsonl


class _Raised(Exception):
    pass


def _resolution(spec: dict[str, Any]) -> Resolution:
    return Resolution(
        outcome=spec["o"],
        project=spec.get("p"),
        checkout=spec.get("c"),
        rule=spec.get("rule", "r"),
        lines=tuple(spec.get("lines", ())),
    )


def _res_json(res: Resolution) -> dict[str, Any]:
    return {
        "o": res.outcome,
        "p": res.project,
        "c": res.checkout,
        "rule": res.rule,
        "lines": list(res.lines),
    }


def _loc_json(loc: Location) -> dict[str, Any]:
    return {
        "machine": loc.machine,
        "cwd": loc.cwd,
        "state": loc.state,
        "kind": loc.kind,
        "git_dir": loc.git_dir,
        "head": loc.head,
        "session": loc.session,
        "detail": loc.detail,
    }


class _Model:
    """A recording `Membership`."""

    def __init__(self, case: dict[str, Any], calls: list[Any]) -> None:
        self.case = case
        self.calls = calls
        self.writer: Any = None

    def make(
        self,
        store: Any,
        machine: str,
        *,
        liveness: Any = None,
        token_writer: Any = None,
        session: str | None = None,
    ) -> _Model:
        self.calls.append(["model", machine, session, token_writer is not None])
        if self.case.get("model_raises"):
            raise _Raised
        self.writer = token_writer
        self.machine = machine
        return self

    def record_machine(self, identity: Any) -> None:
        self.calls.append(["record", identity.id])
        if self.case.get("fails"):
            raise _Raised

    def session_resolution(self, loc: Location, res: Resolution, path: str) -> Any:
        self.calls.append(["sr", loc.cwd, res.checkout, path])
        sr = self.case.get("sr")
        return None if sr is None else _resolution(sr)

    def resolve(self, loc: Location, *, strict: bool) -> Resolution:
        self.calls.append(["resolve", loc.cwd, strict])
        for token in self.case.get("writes", []):
            lineage = None if token is None else types.SimpleNamespace(token=token)
            got = self.writer(types.SimpleNamespace(lineage=lineage))
            self.calls.append(["wrote", None if got is None else list(got)])
        return _resolution(self.case.get("res", {"o": "silent", "p": "p", "c": "k"}))

    def unresolved_home(self, loc: Location, *, session: str | None) -> Any:
        self.calls.append(["unresolved", loc.cwd, session])
        return types.SimpleNamespace(project="prov", checkout=None, rule="b6")

    def add_witness(
        self, project: str, head: str, *, source: str, history: Any
    ) -> None:
        self.calls.append(["witness", project, head, source, _hist(history)])


def _hist(h: Any) -> str | None:
    return None if h is None else h.common_dir


class _History:
    def __init__(self, common_dir: str) -> None:
        self.common_dir = common_dir


def _drive(case: dict[str, Any], run: Any) -> Any:
    calls: list[Any] = []
    caps = list(case.get("caps", []))
    model = _Model(case, calls)

    def capture(
        cwd: str, *, write_token: bool, budget: float, use_cache: bool = True
    ) -> Capture:
        calls.append(["capture", cwd, write_token, budget, use_cache])
        spec = caps.pop(0)
        fields: dict[str, Any] = {
            k: (tuple(v) if isinstance(v, list) else v) for k, v in spec.items()
        }
        return Capture(cwd=cwd, **fields)

    def forget(cwd: str | None = None) -> None:
        calls.append(["forget", cwd])

    def remint(git_dir: str, *, policy: bool, bare: bool, replacing: Any) -> Any:
        calls.append(
            [
                "remint",
                git_dir,
                policy,
                bare,
                None if replacing is None else replacing.token,
            ]
        )
        token, status = case.get("remint", ["T", "written"])
        return types.SimpleNamespace(token=token, status=status)

    def earlier(m: Any, cap: Capture) -> bool:
        calls.append(["earlier", cap.cwd])
        if case.get("early_raises"):
            raise _Raised
        return bool(case.get("early"))

    def open_machine() -> Any:
        calls.append(["open"])
        if case.get("open") is None:
            raise OSError(2, "no state")
        return types.SimpleNamespace(id=case["open"])

    def machineless(
        store: Any, config: Any, cwd: Any, session: Any, exc: OSError, stand: Any
    ) -> Any:
        calls.append(
            [
                "machineless",
                exc.strerror,
                cwd,
                session,
                None if stand is None else stand.path,
            ]
        )
        loc = Location(
            machine="<no machine>", cwd="<x>", state="indeterminate", session=session
        )
        return L.Caller(
            loc,
            Resolution("indeterminate", None, None, "machineless"),
            cast(Any, model),
        )

    def history_of(cap: Capture) -> Any:
        if cap.state != L.REPO or cap.evicted or not cap.common_dir:
            return None
        return _History(cap.common_dir)

    L._clear_machine()
    L._clear_stands()
    with (
        mock.patch.object(L, "capture", capture),
        mock.patch.object(L, "forget", forget),
        mock.patch.object(CT, "remint", remint),
        mock.patch.object(L, "_earlier_tree", earlier),
        mock.patch.object(L, "_machineless", machineless),
        mock.patch.object(L, "_history_of", history_of),
        mock.patch.object(MB, "Membership", model.make),
        mock.patch.object(M, "open_machine", open_machine),
    ):
        try:
            out = run(calls, model)
        except _Raised:
            out = "raised"
    return {"calls": calls, **({"r": out} if out == "raised" else out)}


def _store() -> Any:
    return types.SimpleNamespace(path="/s")


def _config(case: dict[str, Any]) -> Any:
    mem = types.SimpleNamespace(
        write_token=case.get("wt", True), strict=case.get("strict", False)
    )
    return types.SimpleNamespace(membership=mem)


def _ident(case: dict[str, Any]) -> Any:
    i = case.get("id", "m1")
    return None if i is None else types.SimpleNamespace(id=i)


def _caller_out(c: Any) -> dict[str, Any]:
    return {
        "loc": _loc_json(c.location),
        "history": _hist(c.location.history),
        "res": _res_json(c.resolution),
    }


def _caller(case: dict[str, Any]) -> dict[str, Any]:
    stand = case.get("stand")

    def run(calls: list[Any], model: Any) -> Any:
        st = None
        if stand is not None:
            st = L._Stand("m1", stand[0], Resolution("silent", "p", stand[1], "r"))
        c = L._caller(
            _store(),
            _config(case),
            case.get("cwd"),
            session=case.get("session"),
            identity=_ident(case),
            budget=case.get("budget", 0.3),
            stand=st,
        )
        return _caller_out(c)

    return {"k": "caller", **case, "r": _drive(case, run)}


def _hooked(case: dict[str, Any]) -> dict[str, Any]:
    def run(calls: list[Any], model: Any) -> Any:
        res = L._hooked(_store(), _config(case), case.get("cwd"), case.get("session"))
        stands = [
            [k[1], s.machine, s.path, s.resolution.checkout]
            for k, s in L._STANDS.items()
        ]
        then = (
            L.resolve_caller(
                _store(),
                _config(case),
                case.get("cwd"),
                session=case.get("session"),
                identity=cast(Any, types.SimpleNamespace(id="m1")),
            )
            if case.get("again")
            else None
        )
        out: dict[str, Any] = {"res": _res_json(res), "stands": stands}
        if then is not None:
            out["again"] = _res_json(then.resolution)
        return out

    return {"k": "hooked", **case, "r": _drive(case, run)}


def _here(case: dict[str, Any]) -> dict[str, Any]:
    def run(calls: list[Any], model: Any) -> Any:
        def getcwd() -> str:
            calls.append(["getcwd"])
            if case.get("g") is None:
                raise OSError(2, "gone")
            return str(case["g"])

        with mock.patch("bettermemory.location.os.getcwd", getcwd):
            res = L.resolve_here(
                _store(),
                _config(case),
                case.get("cwd"),
                session=case.get("session"),
                identity=_ident(case),
            )
        return {"res": _res_json(res)}

    return {"k": "here", **case, "r": _drive(case, run)}


def _capture_location(case: dict[str, Any]) -> dict[str, Any]:
    def run(calls: list[Any], model: Any) -> Any:
        loc = L.capture_location(
            case["cwd"],
            machine="m1",
            session=case.get("session"),
            config=_config(case),
            budget=case.get("budget", 0.3),
        )
        return {"loc": _loc_json(loc), "history": _hist(loc.history)}

    return {"k": "capture_location", **case, "r": _drive(case, run)}


def _home(case: dict[str, Any]) -> dict[str, Any]:
    def run(calls: list[Any], model: Any) -> Any:
        loc = Location(
            machine="m1",
            cwd="/w",
            state="repo",
            head=case.get("head"),
            session=case.get("session"),
        )
        if case.get("hist"):
            loc = dataclasses.replace(loc, history=cast(Any, _History(case["hist"])))
        c = L.Caller(loc, _resolution(case["res"]), cast(Any, model))
        h = c.home()
        c.add_head_witness(case.get("source", "write"))
        return {"home": [h.project, h.checkout, h.rule]}

    return {"k": "home", **case, "r": _drive(case, run)}


REPO = {
    "state": "repo",
    "kind": "primary",
    "git_dir": "/w/.git",
    "common_dir": "/w/.git",
    "head": "h1",
}
BARE = {"state": "repo", "kind": "bare", "git_dir": "/b.git", "common_dir": "/b.git"}
OUT = {"state": "outside"}


def _all(cases: list[Any]) -> list[dict[str, Any]]:
    return cases


def _lines() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for case in _all(
        [
            {"cwd": None},
            {"cwd": None, "session": "s", "writes": ["a"]},
            {"cwd": "/w", "caps": [REPO]},
            {"cwd": "/w", "caps": [REPO], "session": "s", "wt": False, "strict": True},
            {"cwd": "/w", "caps": [REPO], "budget": 1.5},
            {"cwd": "/w", "caps": [OUT]},
            {"cwd": "/w", "caps": [OUT], "writes": ["a"]},
            {"cwd": "/w", "caps": [{**REPO, "git_dir": ""}], "writes": ["a"]},
            {"cwd": "/w", "caps": [REPO], "writes": [None]},
            {
                "cwd": "/w",
                "caps": [REPO],
                "writes": ["a", "b"],
                "remint": [None, "skipped:contended"],
            },
            {
                "cwd": "/w",
                "caps": [REPO],
                "writes": ["a"],
                "remint": ["T2", "exists"],
                "wt": False,
            },
            {"cwd": "/b.git", "caps": [BARE], "writes": ["a"]},
            {"cwd": "/w", "caps": [REPO, BARE], "early": True, "writes": ["a"]},
            {"cwd": "/w", "caps": [REPO, OUT], "early": True},
            {"cwd": "/w", "caps": [OUT, REPO], "early": True, "writes": ["x"]},
            {"cwd": "/w", "caps": [REPO], "early": False},
            {
                "cwd": "/w",
                "caps": [{**REPO, "file_errors": ["e1", "e\n2", "e1"]}],
                "res": {"o": "silent", "p": "p", "c": "k", "lines": ["e1"]},
            },
            {
                "cwd": "/w",
                "caps": [REPO, {**REPO, "file_errors": ["late"]}],
                "early": True,
            },
            {"cwd": "/w", "caps": [{**REPO, "evicted": True}]},
            {"cwd": "/w", "caps": [{**REPO, "common_dir": ""}]},
            {"cwd": "/w", "caps": [{**OUT, "common_dir": "/c"}]},
            {"cwd": "/w", "caps": [{"state": "indeterminate", "detail": "unreadable"}]},
            {"cwd": "/w", "caps": [REPO], "stand": ["/w", "k"], "sr": None},
            {
                "cwd": "/w",
                "caps": [REPO],
                "stand": ["/w", "k"],
                "sr": {"o": "silent", "p": "q", "c": "k2", "rule": "stand"},
                "writes": ["a"],
            },
            {
                "cwd": None,
                "stand": ["/w", "k"],
                "sr": {"o": "silent", "p": "q", "c": "k2"},
            },
            {
                "cwd": "/w",
                "caps": [{**REPO, "file_errors": ["fe"]}],
                "stand": ["/w", ""],
                "sr": {"o": "silent", "p": "q", "c": "k2"},
            },
            {"id": None, "open": "m9", "cwd": "/w", "caps": [REPO]},
            {"id": None, "open": None, "cwd": "/w", "session": "s"},
            {"id": None, "open": None, "cwd": None},
            {"cwd": "/w", "caps": [REPO], "fails": True},
            {"cwd": None, "fails": True},
            {"cwd": "/w", "caps": [REPO], "early_raises": True},
            {"cwd": "/w", "caps": [REPO], "model_raises": True},
            {"cwd": None, "model_raises": True},
            {
                "id": None,
                "open": None,
                "cwd": "/w",
                "session": "s",
                "stand": ["/w", "k"],
            },
            {
                "cwd": "/w",
                "caps": [REPO],
                "res": {"o": "indeterminate", "p": None, "c": None},
            },
        ]
    ):
        rows.append(_caller(case))
    for case in _all(
        [
            {"id": None, "open": "m1", "cwd": "/w", "caps": [REPO], "session": "s"},
            {
                "id": None,
                "open": "m1",
                "cwd": "/w",
                "caps": [REPO, REPO],
                "session": "s",
                "again": True,
            },
            {"id": None, "open": "m1", "cwd": "/w", "caps": [REPO], "session": None},
            {
                "id": None,
                "open": "m1",
                "cwd": "/w",
                "caps": [REPO],
                "session": "s",
                "res": {"o": "silent", "p": "p", "c": None},
            },
            {
                "id": None,
                "open": "m1",
                "cwd": None,
                "session": "s",
                "res": {"o": "silent", "p": "p", "c": "k"},
            },
            {"id": None, "open": None, "cwd": "/w", "session": "s"},
            {
                "id": None,
                "open": "m1",
                "cwd": "/w",
                "caps": [REPO, REPO],
                "session": "s",
                "again": True,
                "sr": {"o": "silent", "p": "q", "c": "k3"},
            },
        ]
    ):
        rows.append(_hooked(case))
    for case in _all(
        [
            {"cwd": "/w", "caps": [REPO]},
            {"cwd": None, "g": "/g", "caps": [OUT]},
            {"cwd": None, "g": None},
            {"cwd": None, "g": "/w", "id": None, "open": None},
            {"cwd": None, "g": "/g", "id": None, "open": None, "session": "s"},
            {"cwd": "/w", "g": "/g", "id": None, "open": None, "session": "s"},
            {"cwd": None, "g": None, "id": None, "open": None, "session": "s"},
            {"cwd": None, "g": "/g", "caps": [REPO], "session": "s"},
        ]
    ):
        rows.append(_here(case))
    for case in _all(
        [
            {"cwd": "/w", "caps": [REPO], "session": "s", "wt": False, "budget": 2.0},
            {"cwd": "/w", "caps": [{**REPO, "evicted": True}]},
            {"cwd": "/w", "caps": [OUT]},
        ]
    ):
        rows.append(_capture_location(case))
    for case in _all(
        [
            {"res": {"o": "silent", "p": "p", "c": "k"}, "head": "h", "hist": "/c"},
            {"res": {"o": "silent", "p": "p", "c": ""}, "head": "h"},
            {
                "res": {"o": "silent", "p": "p", "c": None},
                "head": "h",
                "source": "verify",
            },
            {"res": {"o": "silent", "p": "global", "c": "k"}, "head": "h"},
            {"res": {"o": "silent", "p": "p", "c": "k"}, "head": ""},
            {"res": {"o": "silent", "p": "p", "c": "k"}, "head": None},
            {
                "res": {"o": "indeterminate", "p": "p", "c": "k"},
                "head": "h",
                "session": "s",
            },
            {"res": {"o": "deferred", "p": "p", "c": "k"}, "head": "h"},
            {
                "res": {"o": "proposed", "p": None, "c": None},
                "head": "h",
                "session": "s",
            },
            {
                "res": {"o": "global", "p": "global", "c": None, "rule": "g"},
                "head": "h",
            },
        ]
    ):
        rows.append(_home(case))
    return rows


def generate() -> dict[str, bytes]:
    lines = _lines()
    header = {"fixture": "location_resolve", "cases": len(lines)}
    return {"location_resolve.jsonl": jsonl([header, *lines])}
