"""Fixture: the guard on a change to a standing memory,
``handlers/_guard.py`` (``standing_guard.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/standing_guard.rs``. It pins
`read_words`, `record`, `Held`, `check` and `guard` over stub
collaborators: `standing.guarded_ids`, `standing.asking`,
`standing.refusal_rows`, `caller_anchoring`, `recall.WORDS` and the
recorder answer from the row's script, and every call made to them is
logged in order.

A row (values are written by `V`): ``k`` is ``check``,
``guard`` or ``words``; ``door``, ``records``, ``said``, ``rule_s`` and
``name`` are the arguments; the script is ``own`` (what `guarded_ids`
returns), ``asking``, ``caller`` (``{"co": checkout}`` or ``{"raise":
text}``, a RuntimeError out of `caller_anchoring`), ``seen``, ``holds``
and ``rows`` (what `refusal_rows` returns). ``log`` is the calls, each a
list: ``["ids", ids]``, ``["asking"]``, ``["caller"]``, ``["seen",
checkout]``, ``["holds", checkout, quote]``, ``["rows", ids]``,
``["rec", kind, fields]``. ``o`` is ``{"ok": response, "held": [door,
records, quote, checked] or None}`` or ``{"err": class, "m": text}``.

Not gated: `str.split` on whitespace no Unicode release since 14.0
changed, the same on every platform.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory import admission, recall, standing
from bettermemory.handlers import _guard

from ._common import dumps_line, has_surrogate


def V(value: Any) -> Any:
    """A value compactly: a `str` as itself (``{"$cps": [...]}`` when it
    holds a surrogate), a dict as ``{"$d": [[key, value], ...]}`` in its
    order, lists, bools, ints and None as JSON has them."""
    if isinstance(value, str):
        return {"$cps": [ord(c) for c in value]} if has_surrogate(value) else value
    if isinstance(value, dict):
        return {"$d": [[V(k), V(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [V(v) for v in value]
    assert value is None or isinstance(value, (bool, int)), value
    return value


N = V


class _Raised(RuntimeError):
    pass


def _stubs(sc: dict[str, Any], log: list[Any]) -> tuple[Any, list[Any]]:
    def guarded_ids(_store: Any, ids: list[str]) -> list[str]:
        log.append(["ids", list(ids)])
        return list(sc.get("own", []))

    def asking() -> bool:
        log.append(["asking"])
        return bool(sc.get("asking", True))

    def caller_anchoring(_store: Any, _config: Any) -> Any:
        log.append(["caller"])
        caller = sc.get("caller", {"co": "/c"})
        if "raise" in caller:
            raise _Raised(caller["raise"])
        res = types.SimpleNamespace(checkout=caller["co"])
        return None, types.SimpleNamespace(resolution=res)

    def refusal_rows(_store: Any, ids: list[str], _adm: Any) -> Any:
        log.append(["rows", list(ids)])
        return sc.get("rows", [{"id": i} for i in ids])

    class Words:
        def seen(self, _path: Any, checkout: str) -> bool:
            log.append(["seen", checkout])
            return bool(sc.get("seen", False))

        def holds(self, _path: Any, checkout: str, quote: str) -> bool:
            log.append(["holds", checkout, quote])
            return bool(sc.get("holds", False))

    class Recorder:
        def record(self, kind: str, **fields: Any) -> None:
            log.append(["rec", kind, fields])

    class Admission:
        @staticmethod
        def of(_strata: Any, caller: Any) -> Any:
            return caller

    deps = types.SimpleNamespace(
        store=types.SimpleNamespace(path="S"), config=None, recorder=Recorder()
    )
    patches = [
        mock.patch.object(standing, "guarded_ids", guarded_ids),
        mock.patch.object(standing, "asking", asking),
        mock.patch.object(standing, "refusal_rows", refusal_rows),
        mock.patch.object(_guard, "caller_anchoring", caller_anchoring),
        mock.patch.object(recall, "WORDS", Words()),
        mock.patch.object(admission, "Admission", Admission),
    ]
    return deps, patches


def _row(k: str, sc: dict[str, Any], **args: Any) -> dict[str, Any]:
    log: list[Any] = []
    deps, patches = _stubs(sc, log)
    for p in patches:
        p.start()
    try:
        try:
            if k == "words":
                co = sc.get("caller", {"co": "/c"})["co"]
                caller: Any = types.SimpleNamespace(
                    resolution=types.SimpleNamespace(checkout=co)
                )
                w = _guard.read_words(deps, caller, args["said"], name=args["name"])
                o: dict[str, Any] = {
                    "ok": N(None if w is None else [w.quote, w.checked])
                }
            elif k == "guard":
                o = {
                    "ok": N(
                        _guard.guard(deps, args["door"], args["records"], args["said"])
                    )
                }
            else:
                resp, held = _guard.check(
                    deps,
                    args["door"],
                    args["records"],
                    args["said"],
                    rule_s=args["rule_s"],
                )
                o = {
                    "ok": N(resp),
                    "held": N(
                        None
                        if held is None
                        else [
                            held.door,
                            list(held.records),
                            held.words.quote,
                            held.words.checked,
                        ]
                    ),
                }
        except _Raised as exc:
            o = {"err": "Caller", "m": N(str(exc))}
        except ValueError as exc:
            o = {"err": "Value", "m": N(str(exc))}
    finally:
        for p in reversed(patches):
            p.stop()
    return {"k": k, "sc": N(sc), "a": N(args), "log": N(log), "o": o}


def _rows() -> Iterator[dict[str, Any]]:
    sc: dict[str, Any]
    # read_words alone.
    for said in (
        None,
        "",
        "  \t",
        "\u3000\u0085",
        " the  user\tsaid\nit ",
        "a\u2028b",
        "a\u200bb",
    ):
        for sc in (
            {"caller": {"co": None}},
            {"caller": {"co": "/c"}, "seen": False},
            {"caller": {"co": "/c"}, "seen": True, "holds": True},
            {"caller": {"co": "/c"}, "seen": True, "holds": False},
        ):
            yield _row("words", sc, said=said, name="said")
    yield _row("words", {"caller": {"co": ""}, "seen": True}, said="x", name="answer")
    yield _row("words", {}, said="", name="x\u00e9")
    # check.
    base = {
        "door": "memory_update",
        "records": ["r1", "r2"],
        "said": None,
        "rule_s": None,
    }
    for sc in (
        {"own": []},
        {"own": ["r2"]},
        {"own": ["r2"], "asking": False},
        {"own": ["r2", "r1"], "rows": [{"id": "r2", "t": "x"}, None]},
    ):
        yield _row("check", sc, **base)
    yield _row("check", {"own": ["r9"]}, **{**base, "records": []})
    for rule_s in ([], ["s1"], ["s1", "r2", "s1", "s2", "r2"], ["r2"]):
        for own in ([], ["r2"]):
            yield _row("check", {"own": own}, **{**base, "rule_s": rule_s})
            yield _row(
                "check", {"own": own}, **{**base, "rule_s": rule_s, "records": []}
            )
    yield _row("check", {"own": [], "asking": False}, **{**base, "rule_s": ["s1"]})
    for said in ("the words", "", "  "):
        for sc in (
            {"caller": {"co": None}},
            {"seen": True, "holds": True},
            {"seen": True, "holds": False},
        ):
            yield _row("check", {"own": ["r1"], **sc}, **{**base, "said": said})
            yield _row(
                "check", {"own": [], **sc}, **{**base, "said": said, "rule_s": ["s1"]}
            )
    # The caller raises.
    for said in (None, "words"):
        yield _row(
            "check",
            {"own": ["r1"], "caller": {"raise": "no caller here"}},
            **{**base, "said": said},
        )
    yield _row(
        "check", {"own": [], "asking": False, "caller": {"raise": "never"}}, **base
    )
    # guard.
    g = {"door": "memory_remove", "records": ["r1"], "said": None}
    for sc in (
        {"own": []},
        {"own": ["r1"]},
        {"own": ["r1"], "asking": False},
        {"own": ["r1"], "caller": {"raise": "boom"}},
        {"own": ["r1"], "seen": True, "holds": True},
        {"own": ["r1"], "seen": True, "holds": False},
        {"own": ["r1"], "caller": {"co": None}},
    ):
        for said in (None, " said  it "):
            yield _row("guard", sc, **{**g, "said": said})
    yield _row("guard", {"own": ["x"]}, **{**g, "records": []})


def generate() -> dict[str, bytes]:
    rows = list(_rows())
    header = {"fixture": "standing_guard", "cases": len(rows)}
    lines = [dumps_line(header)] + [dumps_line(r) for r in rows]
    return {"standing_guard.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
