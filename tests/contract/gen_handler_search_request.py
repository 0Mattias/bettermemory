"""Exercise the Python request inputs, cache identities, and expansion event fields."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from bettermemory.handlers.search import (
    Hydrator,
    _request_width_and_mode,
    _TopExpansion,
)
from ._common import jsonl, neutral


def _wire(value: Any) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        return {"t": type(value).__name__, "hex": value.hex()}
    return neutral(value)


def _width_rows() -> Iterator[dict[str, Any]]:
    default_width: Any = 9
    default_mode: Any = "bm25"
    calls: list[str] = []

    class Behavior:
        @property
        def default_max_results(self) -> Any:
            calls.append("width")
            return default_width

        @property
        def search_mode(self) -> Any:
            calls.append("mode")
            return default_mode

    deps: Any = SimpleNamespace(config=SimpleNamespace(behavior=Behavior()))
    width: Any
    mode: Any
    result: dict[str, Any]
    for width in (
        None,
        False,
        True,
        -501,
        0,
        1,
        49,
        50,
        51,
        10**90,
        -(10**90),
        1.99,
        -1.99,
        1e290,
        float("nan"),
        float("inf"),
        -float("inf"),
        " \t+٠_٢\n",
        "１_０",
        "1__9",
        "bad\ud800",
        {},
        [],
        (),
        b" +5_1\t",
        bytearray(b" 7 "),
        b"\xff",
        bytearray(b"a"),
        "2" * 4301,
    ):
        for mode in (None, "keyword"):
            calls.clear()
            try:
                number, resolved = _request_width_and_mode(deps, width, mode)
                result = {"ok": [number, resolved]}
            except (TypeError, ValueError, OverflowError) as exc:
                result = {"error": type(exc).__name__, "text": neutral(str(exc))}
            yield {
                "kind": "resolve",
                "width": _wire(width),
                "mode": neutral(mode),
                "defaults": [neutral(default_width), neutral(default_mode)],
                "calls": list(calls),
                "result": result,
            }

    for default_width, default_mode in (
        (0, ""),
        (71, "keyword"),
        ("bad", "bm25"),
        (9, "other"),
    ):
        for mode in (
            None,
            "",
            False,
            [],
            {},
            "bm25",
            "hybrid",
            "B M25",
            "a'\udcff",
            ["β"],
            ("nested", 1),
            17,
        ):
            calls.clear()
            try:
                number, resolved = _request_width_and_mode(deps, None, mode)
                result = {"ok": [number, resolved]}
            except (TypeError, ValueError, OverflowError) as exc:
                result = {"error": type(exc).__name__, "text": neutral(str(exc))}
            yield {
                "kind": "resolve",
                "width": neutral(None),
                "mode": neutral(mode),
                "defaults": [neutral(default_width), neutral(default_mode)],
                "calls": list(calls),
                "result": result,
            }


def _hydration() -> dict[str, Any]:
    names = ["light", "missing", "extra", "first", "last", "unasked", "light2"]
    ids = ["x", "missing", "z\udcff", "x", "x", "z\udcff", "x"]
    items = {
        name: SimpleNamespace(id=rid) for name, rid in zip(names, ids, strict=True)
    }
    by_identity = {id(item): name for name, item in items.items()}
    requests: list[list[str]] = []
    replies: list[str] = []
    failure = RuntimeError("loader stopped")

    class Store:
        def load_many(self, selected: list[str]) -> Iterator[Any]:
            requests.append(list(selected))
            for label in replies:
                if label == "raise":
                    raise failure
                yield items[label]

    loader: Any = Store()
    hydrator = Hydrator(loader)
    steps = []
    result: dict[str, Any]
    for action, inputs, response in (
        ("replace", ["light", "light2"], []),
        ("load", ["light", "missing", "light"], ["first", "extra", "last", "raise"]),
        ("replace", ["light2", "missing", "unasked"], []),
        ("load", ["missing", "light", "unasked"], []),
        ("load", ["missing", "light2"], ["missing"]),
        ("load", ["missing", "light", "light2"], []),
        ("new", ["light2", "light", "light"], []),
    ):
        replies[:] = response
        prior = len(requests)
        if action == "new":
            hydrator = Hydrator(loader)
        try:
            read = hydrator.replaced if action == "replace" else hydrator
            result = {
                "ok": [
                    by_identity[id(item)] for item in read([items[n] for n in inputs])
                ]
            }
        except RuntimeError as exc:
            assert exc is failure
            result = {"error": str(exc)}
        steps.append(
            {
                "action": action,
                "inputs": inputs,
                "replies": response,
                "result": result,
                "requested": [[neutral(i) for i in ids] for ids in requests[prior:]],
                "cache": [
                    [neutral(rid), by_identity[id(item)]]
                    for rid, item in hydrator.full.items()
                ],
            }
        )
    return {
        "kind": "hydrate",
        "records": [[n, neutral(rid)] for n, rid in zip(names, ids, strict=True)],
        "steps": steps,
    }


def generate() -> dict[str, bytes]:
    rows = list(_width_rows())
    rows.append(_hydration())
    for fields in ((None, 0, None, None), ("x\udcff", 10**120, "ok", -(2**160))):
        expansion = _TopExpansion(*fields)
        rows.append(
            {
                "kind": "expand",
                "fields": [neutral(x) for x in fields],
                "names": list(expansion._fields),
                "event": [
                    neutral(expansion.expanded_id),
                    neutral(expansion.drift_missing),
                    neutral(expansion.commit_drift_status),
                    neutral(expansion.commits_since_verify),
                ],
            }
        )
    return {"handler_search_request.jsonl": jsonl(rows)}
