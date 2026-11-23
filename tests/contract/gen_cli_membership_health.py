"""The two shared health presentations, taken from the live CLI membership module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from bettermemory import location
from bettermemory.cli import membership_cmd as cli
from bettermemory.membership import Membership
from bettermemory.membership_records import Health, Project, Proposal, UnresolvedProject
from bettermemory.store import Store

from ._common import jsonl
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_membership_rows import enc
from .gen_membership_views import _k, _p, _v
from .gen_store_open import _engine, _made


LOG0 = datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
LOG_STEP = timedelta(seconds=1)
WALL0 = LOG0


def _wire(v: Any) -> Any:
    # Decimal obtains the exact decimal of an already constructed int even past
    # the interpreter's digit limit. The production renderer still sees the limit.
    if isinstance(v, int) and abs(v) >= 2**53:
        digits = str(Decimal(v))
        if digits[0] == "1" and set(digits[1:]) == {"0"}:
            return {"$p": len(digits) - 1}
        return {"$i": digits}
    if isinstance(v, list):
        return [_wire(x) for x in v]
    if isinstance(v, dict):
        return {"$d": [[_wire(k), _wire(x)] for k, x in v.items()]}
    return enc(v)


def _project(row: list[Any]) -> Project:
    ident, name, reason = row
    return Project(ident, name, None, reason, None, None, False, False, "born")


def _proposal(row: list[Any]) -> Proposal:
    ident, checkout, kind, reason, line, opened = row
    return Proposal(
        ident, checkout, kind, (), {}, reason, line, "open", None, None, None, opened
    )


def _value(spec: dict[str, Any]) -> Health:
    return Health(
        tuple(_proposal(r) for r in spec["proposals"]),
        tuple(_project(r) for r in spec["projects"]),
        dict(spec["turns"]),
        spec["indeterminate"],
        spec["deferred"],
        tuple(spec["without"]),
        tuple(UnresolvedProject(*r) for r in spec["unresolved"]),
    )


def _spec(h: Health) -> dict[str, Any]:
    return dict(
        proposals=[
            [p.id, p.checkout, p.kind, p.reason, p.line, p.opened]
            for p in h.open_proposals
        ],
        projects=[[p.id, p.name, p.provisional] for p in h.provisional],
        unresolved=[
            [u.project, u.name, u.state, u.checkout, u.path, u.memories, u.episodes]
            for u in h.unresolved
        ],
        turns=list(map(list, h.suppressed_turns.items())),
        indeterminate=h.indeterminate,
        deferred=h.deferred,
        without=list(h.without_live_view),
    )


class MissingCheckout(KeyError):
    pass


class _Reads:
    def __init__(
        self,
        health: Health,
        responses: list[list[Any]],
        failure: bool = False,
        model: Membership | None = None,
    ):
        self.value = health
        self.responses = iter(responses)
        self.failure = failure
        self.model = model
        self.calls: list[Any] = []
        self.observed_paths: list[list[Any]] = []

    def health(self) -> Health:
        self.calls.append(["health"])
        if self.failure:
            raise KeyError("top-health")
        return self.model.health() if self.model is not None else self.value

    def checkout(self, ident: str) -> Any:
        self.calls.append(["checkout", ident])
        if self.model is not None:
            checkout = self.model.checkout(ident)
            newest = checkout.newest_view
            self.observed_paths.append(
                ["no-view", None] if newest is None else ["view", newest.path]
            )
            return checkout
        tag, value = next(self.responses)
        if tag == "key":
            raise MissingCheckout(value)
        if tag == "error":
            raise RuntimeError(value)
        if tag == "view-key":

            class MissingView:
                @property
                def newest_view(self) -> Any:
                    raise MissingCheckout(value)

            return MissingView()
        if tag == "path-error":

            class BadPath:
                @property
                def path(self) -> Any:
                    raise KeyError(value)

            return SimpleNamespace(newest_view=BadPath())
        return SimpleNamespace(
            newest_view=None if tag == "no-view" else SimpleNamespace(path=value)
        )


def _outcome(fn: Any) -> dict[str, Any]:
    try:
        return {"value": _wire(fn())}
    except Exception as exc:
        return {"error": enc([type(exc).__name__, str(exc)])}


def _run(
    spec: dict[str, Any],
    responses: list[list[Any]],
    failure: bool = False,
    model: Membership | None = None,
) -> dict[str, Any]:
    source = _Reads(_value(spec), responses, failure, model)
    projection = None

    def project() -> dict[str, Any]:
        nonlocal projection
        projection = cli.health_dict(cast(Membership, source))
        return projection

    d = _outcome(project)
    result = dict(
        spec=_wire(spec),
        responses=enc(source.observed_paths if model is not None else responses),
        failure=failure,
        projection=d,
        calls=enc(source.calls),
    )
    if projection is not None:
        # Supply a dictionary with a different iteration order to the public renderer.
        supplied = dict(projection)
        supplied["suppressed_recall_turns"] = dict(
            reversed(projection["suppressed_recall_turns"].items())
        )
        result.update(
            lines=_outcome(lambda: cli.health_lines(projection)),
            supplied_lines=_outcome(lambda: cli.health_lines(supplied)),
        )
    return result


def _real(root: Path) -> dict[str, Any]:
    path = root / "signed cli health"
    _made("created", {}, path)
    file = path / "memory.sqlite"
    writes: list[list[Any]] = [
        ["project", _p("P", name='Stable "name"', provisional="history")],
        ["checkout", _k("K", "P")],
        ["checkout", _k("Empty", "P")],
        ["checkout_view", _v("K", "/older", 1)],
        ["checkout_view", _v("K", '/new "seen"', 2)],
    ]
    with _engine(), Store.open(file, keys_dir=path / "keys") as store:
        with fixed(log=Ticker(LOG0, LOG_STEP), wall=Ticker(WALL0, LOG_STEP)):
            for kind, row in writes:
                store.put_membership(kind, row)
            store.record_membership("resolution_deferred", {})
        before = [
            list(r) for r in store._conn.execute("SELECT seq,mac FROM log ORDER BY seq")
        ]
        with patch.object(
            location, "process_machine", return_value=SimpleNamespace(id="M1")
        ) as machine:
            fresh = cli.health_model(store)
        assert machine.call_count == 1 and fresh.machine == "M1"
        # Deterministic liveness for the real model and its real SQLite reads.
        model = Membership(store, "M1", liveness=lambda _k, _v: "gone")
        spec = _spec(model.health())
        result = _run(spec, [], model=model)
        assert spec["without"] == ["Empty", "K"] and spec["deferred"] == 1
        after = [
            list(r) for r in store._conn.execute("SELECT seq,mac FROM log ORDER BY seq")
        ]
        assert before == after
        result.update(real=True, writes=enc(writes), signed=before)
        return result


def generate() -> dict[str, bytes]:
    empty = dict(
        proposals=[],
        projects=[],
        unresolved=[],
        turns=[],
        indeterminate=0,
        deferred=0,
        without=[],
    )
    full = dict(
        proposals=[
            ["Q1", "A", "merge", "why", "raw\nline\udcff", "early"],
            ["Q2", "A", "split", "why", "two", "late"],
            ["Q3", None, "ask", "reason", "no checkout", "late"],
            ["Q4", "", "ask", "why", "empty id", "late"],
        ],
        projects=[["Pα", '\rname"”', None], ["P\udcfe", "", "provisional"]],
        unresolved=[["U", "👀", "gone", None, "", 2**90, -(2**95)]],
        turns=[["z\udcfe", 2**140], ["Z", -(2**130)], ["😀", 4], ["", 2**64]],
        indeterminate=-(2**100),
        deferred=2**109,
        without=["A", "missing", ""],
    )
    responses: list[list[Any]] = [
        ["view", "/first\udcff"],
        ["view", ""],
        ["key", "missing"],
        ["view", "/new"],
        ["no-view", None],
        ["view-key", "not found"],
    ]
    cases = [_run(empty, []), _run(full, responses), _run(empty, [], True)]
    cases.append(
        _run(
            empty | {"turns": [["A", True], ["B", -1]], "without": ["empty"]},
            [["view", ""]],
        )
    )
    for idx, (tag, value) in enumerate(responses):
        for failure in [["error", "stop\udcff"], ["path-error", "after"]]:
            cases.append(_run(full, [*responses[:idx], failure, *responses[idx + 1 :]]))
    cases.append(
        _run(full | {"unresolved": [["U", "n", "gone", None, None, 0, 1]]}, responses)
    )
    for turns in [
        [["s", 10**4299]],
        [["s", 10**4300]],
        [["a", 9 * 10**4299], ["b", 10**4299]],
        [["a", -(10**4300)], ["b", 10**4300]],
        [["a", -(10**99)], ["b", 10**99 - 1]],
    ]:
        cases.append(_run(empty | {"turns": turns}, []))
    with world() as env:
        cases.append(_real(env.root))
    return {"cli_membership_health.jsonl": jsonl(cases)}
