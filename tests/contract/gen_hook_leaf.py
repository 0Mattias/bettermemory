"""Fixtures: the hook module's pure leaves (``hook_leaf.jsonl``, not gated).

The native build reads them in
``native/crates/bettermemory-core/src/hook.rs``. This pins it to `hook`'s
`_read_payload`, `_payload_text`, `_pending_retrievals` and
`_emit_hook_attributions`, the last with a fake store, a recording
recorder and a scripted `_latest_in_process_session`.

One JSON object per line, keys sorted, ASCII. The first line is a header
``{"k": "header", "count": n, "lookback": 600, "cap": 65536}``. Every other
line is a case ``{"f": function, ..., "o": answer}``. A long stdin text
is a recipe ``{"r": [[text, n], ...]}``, the concatenation of each text
``n`` times. A structured input
or answer that may hold a lone surrogate is written as its ``json.dumps``
text (``ensure_ascii``), which the Rust test reads back with the crate's
``json::loads``; a plain text is ``{"c": [code points]}`` when it holds a
surrogate.

- ``"payload"``: ``a`` the stdin text; ``o`` the dict's dumps text, or
  ``{"e": exception class}``;
- ``"text"``: ``p`` a payload's dumps text, ``n`` a field name; ``o`` the
  field or None;
- ``"pending"``: ``ev`` the events' dumps text, ``now`` the clock, ``r``
  the retrieval session, ``u`` the used sessions, ``l`` the lookback;
  ``o`` the ids sorted, or ``{"e": class}``;
- ``"emit"``: ``ev``, ``now``, ``sid``, ``reply``, ``wt``, ``model``,
  ``server`` (absent for None), ``latest`` (what the join answers),
  ``bodies`` (id to body), ``gone`` (ids the store refuses with
  MemoryNotFoundError or TombstonedError), ``boom`` (ids it refuses with
  OSError), ``rfail`` (the recorder call, from 1, that raises OSError, or
  0); ``o`` ``{"rows": [[kind, [[name, value], ...]], ...], "ret": ids,
  "joins": [[events seen, worktree], ...]}`` or ``{"e": class, "rows":
  ...}``.

Nothing depends on the Python release, the Unicode version (bodies and
replies are ASCII; stripped whitespace is of Unicode's long-stable set)
or the platform. Nesting past the decoder's budget, whose depth follows
the release, is left to the robustness test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from bettermemory import hook
from bettermemory.store import MemoryNotFoundError, TombstonedError

from ._common import jsonl

_UTC = timezone.utc
_NOW = datetime(2026, 11, 21, 12, 0, 0, 1, tzinfo=_UTC)


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=True)


def _t(s: str | None) -> Any:
    if isinstance(s, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return {"c": [ord(c) for c in s]}
    return s


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(seconds: float, now: datetime = _NOW) -> str:
    return _iso(now - timedelta(seconds=seconds))


def _payload_cases() -> list[dict[str, Any]]:
    texts: list[Any] = [
        "",
        "   ",
        "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0       　",
        '{"session_id": "s1", "cwd": "/w"}',
        '\n\n  {"session_id": "s1"}  \n',
        '　{"a": 1} ',
        "﻿{}",
        "[1, 2]",
        '"text"',
        "3",
        "null",
        "true",
        "{",
        '{"a": 1} x',
        '{"a": 1}{"b": 2}',
        "{'a': 1}",
        '{"a": NaN, "b": Infinity}',
        '{"a": "\\ud800", "b": "\\udfff\\ud800"}',
        '{"a": "\ud800"}',
        '\ud800{"a": 1}',
        '{"a": 1, "a": 2}',
        '{"a": {"b": [1, {"c": null}]}}',
        ("r", [('{"n": ', 1), ("9", 4300), ("}", 1)]),
        ("r", [('{"n": ', 1), ("9", 4301), ("}", 1)]),
        ("r", [("-", 1), ("1", 4301)]),
        ("r", [(" ", 70000), ('{"big": 1}', 1), ("\u3000", 3)]),
        '{"n": 1e999}',
        '{"​": "zero width is not space"}',
        "​{}",
    ]
    out = []
    for spec in texts:
        text = spec if isinstance(spec, str) else "".join(t * n for t, n in spec[1])
        try:
            got: Any = _dumps(hook._read_payload(text))
        except Exception as exc:  # noqa: BLE001
            got = {"e": type(exc).__name__}
        spelled = (
            _t(text) if isinstance(spec, str) else {"r": [[t, n] for t, n in spec[1]]}
        )
        out.append({"f": "payload", "a": spelled, "o": got})
    payload = {
        "a": "x",
        "e": "",
        "n": None,
        "i": 1,
        "l": ["x"],
        "s": "\ud800",
        "t": True,
    }
    for name in ["a", "e", "n", "i", "l", "s", "t", "missing"]:
        out.append(
            {
                "f": "text",
                "p": _dumps(payload),
                "n": name,
                "o": _t(hook._payload_text(payload, name)),
            }
        )
    return out


def _ev(
    kind: Any, ago: float | None, session: Any = "S", **fields: Any
) -> dict[str, Any]:
    e: dict[str, Any] = {"kind": kind, **fields}
    if ago is not None:
        e["ts"] = _ago(ago)
    if session is not None:
        e["session"] = session
    return e


def _windows() -> list[tuple[list[dict[str, Any]], datetime, str, list[str], int]]:
    w: list[tuple[list[dict[str, Any]], datetime, str, list[str], int]] = []

    def add(
        events: list[dict[str, Any]],
        r: str = "S",
        u: list[str] | None = None,
        now: datetime = _NOW,
        lookback: int = 600,
    ) -> None:
        w.append((events, now, r, u if u is not None else ["S", "T"], lookback))

    add([])
    add([_ev("search", 10, returned=["b", "a", "c"])])
    add([_ev("search", 10, returned=["b", "a"]), _ev("use", 5, ids=["a"])])
    add([_ev("search", 10, returned=["a"]), _ev("use", 5, session="T", ids=["a"])])
    add([_ev("search", 10, returned=["a"]), _ev("use", 5, session="X", ids=["a"])])
    add(
        [
            _ev("list", 10, returned=["a"]),
            _ev("list_active", 10, returned=["b"]),
            _ev("show", 10, id="c"),
            _ev("other", 10, returned=["d"]),
            _ev("Search", 10, returned=["e"]),
        ]
    )
    add(
        [
            _ev("search", 10, session="T", returned=["a"]),
            _ev("show", 10, session="T", id="b"),
        ]
    )
    # Falsy fallbacks: session, returned, ids.
    add(
        [
            _ev(
                "search", 10, session="", session_id="S", returned=[], memory_ids=["m"]
            ),
            _ev(
                "search",
                10,
                session=None,
                session_id="S",
                returned=None,
                memory_ids=[],
                hit_ids=["h"],
            ),
            _ev("search", 10, session=0, session_id="S", returned=0, memory_ids=None),
            _ev("search", 10, session=False, returned=["no-session"]),
            _ev("search", 10, session="S", session_id="X", returned=["s-first"]),
        ]
    )
    add(
        [
            _ev("search", 10, returned=["a", "b", "c"]),
            _ev("use", 5, session="", session_id="T", ids=[], memory_ids=["a"]),
            _ev("use", 5, ids=None, memory_ids=["b"]),
        ]
    )
    # Non-list and non-str ids.
    add(
        [
            _ev("search", 10, returned="abc"),
            _ev("search", 10, returned={"a": 1}),
            _ev("search", 10, returned=[1, None, ["x"], "ok", 2.5, True]),
            _ev("show", 10, id=5),
            _ev("show", 10, id=["s"]),
            _ev("show", 10, id=""),
        ]
    )
    add(
        [
            _ev("search", 10, returned=["", "a"]),
            _ev("use", 5, ids="a"),
            _ev("use", 5, ids=[1, "x"]),
        ]
    )
    # Non-str sessions and kinds.
    add(
        [
            _ev("search", 10, session=1, returned=["a"]),
            _ev("search", 10, session=["S"], returned=["b"]),
            _ev(["search"], 10, returned=["c"]),
            _ev(None, 10, returned=["d"]),
            _ev(1, 10, returned=["e"]),
        ]
    )
    add(
        [
            _ev("search", 10, returned=["a"]),
            _ev("use", 5, session=1, ids=["a"]),
            _ev("use", 5, session=None, ids=["a"]),
            _ev("use", 5, session=2.5, ids=["a"]),
        ]
    )
    # Unhashable session on a use row raises; on a retrieval it is skipped.
    add([_ev("search", 10, returned=["a"]), _ev("use", 5, session=["S"], ids=["a"])])
    add([_ev("search", 10, returned=["a"]), _ev("use", 5, session={"s": 1}, ids=["a"])])
    add(
        [
            _ev("search", 10, returned=["a"]),
            _ev("use", 5, session=[], session_id=["S"], ids=["a"]),
        ]
    )
    # A falsy session falls back to session_id even when that is falsy too.
    add(
        [
            _ev("search", 10, returned=["a"]),
            _ev("use", 5, session=None, session_id=[], ids=["a"]),
        ]
    )
    add(
        [
            _ev("search", 10, returned=["a"]),
            _ev("use", 5, session="", session_id={}, ids=["a"]),
        ]
    )
    add(
        [
            _ev("search", 10, session={"x": 1}, returned=["a"]),
            _ev("show", 10, session=[1], id="b"),
        ]
    )
    add([_ev("search", 10, returned=["a"]), _ev("use", 900, session=["S"], ids=["a"])])
    # Window and malformed ts.
    add(
        [
            _ev("search", 599, returned=["in"]),
            _ev("search", 600, returned=["edge"]),
            _ev("search", 600.000001, returned=["out"]),
            _ev("search", -100, returned=["future"]),
        ]
    )
    add([_ev("search", 10, returned=["a"]), _ev("use", 601, ids=["a"])])
    add(
        [
            _ev("search", None, returned=["nots"]),
            {"kind": "search", "session": "S", "ts": "yesterday", "returned": ["bad"]},
            {"kind": "search", "session": "S", "ts": 1795000000, "returned": ["int"]},
            {"kind": "search", "session": "S", "ts": "", "returned": ["empty"]},
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T11:59:00Z",
                "returned": ["z"],
            },
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T11:59:00",
                "returned": ["naive"],
            },
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T13:59:00+02:00",
                "returned": ["offset"],
            },
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T11:59:00-02:00",
                "returned": ["west"],
            },
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T11:59:00ZZ",
                "returned": ["zz"],
            },
            {
                "kind": "search",
                "session": "S",
                "ts": "2026-11-21T11:59:00.000+00:00",
                "returned": ["ms"],
            },
        ]
    )
    add([_ev("search", 10, returned=["a"])], lookback=0)
    add(
        [_ev("search", 10, returned=["a"]), _ev("search", 0, returned=["b"])],
        lookback=5,
    )
    add([_ev("search", 10, returned=["a"])], lookback=-20)
    # The float comparison in year 9000, where it and the microsecond
    # comparison part.
    for now_s, ts_s in [
        ("9000-01-02T02:17:30.323160+00:00", "9000-01-02T02:07:30.323159+00:00"),
        ("9000-01-01T20:46:50.479771+00:00", "9000-01-01T20:36:50.479761+00:00"),
        ("9000-01-08T00:19:39.998169+00:00", "9000-01-08T00:09:39.998168+00:00"),
    ]:
        add(
            [{"kind": "search", "session": "S", "ts": ts_s, "returned": ["far"]}],
            now=datetime.fromisoformat(now_s),
        )
    # Code-point order with surrogates and astral text.
    add([_ev("search", 10, returned=["\ud800", "\U0001f600", "￿", "b", "B", "a\x00"])])
    add([_ev("search", 10, session="\ud800", returned=["s"])], r="\ud800", u=["\ud800"])
    add(
        [_ev("search", 10, returned=["a"]), _ev("use", 5, session="S", ids=["a"])], u=[]
    )
    return w


def _pending_case(
    events: list[dict[str, Any]], now: datetime, r: str, u: list[str], lb: int
) -> dict[str, Any]:
    with mock.patch.object(hook, "utcnow", lambda: now):
        try:
            got: Any = _dumps(
                sorted(
                    hook._pending_retrievals(
                        events,
                        retrieval_session_id=r,
                        used_session_ids=set(u),
                        lookback_seconds=lb,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001
            got = {"e": type(exc).__name__}
    return {
        "f": "pending",
        "ev": _dumps(events),
        "now": _iso(now),
        "r": _t(r),
        "u": [_t(x) for x in u],
        "l": lb,
        "o": got,
    }


class _Store:
    def __init__(
        self, bodies: dict[str, str], gone: list[str], boom: list[str]
    ) -> None:
        self.bodies, self.gone, self.boom = bodies, gone, boom

    def load_one(self, mid: str) -> Any:
        if mid in self.boom:
            raise OSError("boom")
        if mid in self.gone:
            raise (MemoryNotFoundError if len(mid) % 2 else TombstonedError)(mid)
        return mock.Mock(body=self.bodies[mid])


class _Recorder:
    def __init__(self, fail_at: int) -> None:
        self.rows: list[Any] = []
        self.fail_at = fail_at

    def record(self, kind: str, **fields: Any) -> None:
        if len(self.rows) + 1 == self.fail_at:
            raise OSError("recorder")
        self.rows.append([kind, [[k, v] for k, v in fields.items()]])


_LONG = "The deploy script must run from the repository root directory always"
_LONG2 = "Configure the cache eviction policy to least recently used entries"


def _emits() -> list[dict[str, Any]]:
    base = [
        _ev("search", 10, returned=["m2", "m1", "m3"]),
        _ev("show", 10, id="m4"),
    ]
    reply = f"As noted, {_LONG.lower()}. Done."
    cases: list[dict[str, Any]] = []

    def add(**kw: Any) -> None:
        spec = {
            "ev": base,
            "sid": "T",
            "reply": reply,
            "wt": None,
            "model": None,
            "latest": "S",
            "bodies": {"m1": _LONG, "m2": _LONG2, "m3": "short", "m4": _LONG2},
            "gone": [],
            "boom": [],
            "rfail": 0,
        }
        spec.update(kw)
        cases.append(spec)

    add()
    add(model="claude-x")
    add(model="")
    add(wt="/w", latest=None)
    add(latest="")
    add(server="S", latest="Q")
    add(server="", latest="S")
    add(server="", sid="S")
    add(server="Q")
    add(sid="S", latest=None)
    add(reply="")
    add(reply="nothing in common here at all")
    add(reply=f"{_LONG}. {_LONG2}.")
    add(gone=["m1", "m2", "m3", "m4"])
    add(
        gone=["m1", "m22"],
        bodies={"m1": _LONG, "m2": _LONG2, "m3": "x", "m4": "y", "m22": "z"},
        ev=[_ev("search", 10, returned=["m1", "m2", "m22"])],
    )
    add(boom=["m3"])
    add(rfail=1)
    add(rfail=2)
    add(rfail=1, reply="no match")
    add(ev=[])
    add(ev=[*base, _ev("use", 5, session="T", ids=["m1", "m2"])])
    add(ev=[*base, _ev("use", 5, session=["S"], ids=["m1"])])
    add(ev=[*base, 5])
    add(bodies={"m1": "", "m2": _LONG2, "m3": "", "m4": ""})
    add(
        ev=[_ev("search", 10, returned=["\ud800x", "a\U0001f600"])],
        bodies={"\ud800x": _LONG, "a\U0001f600": _LONG2},
        reply=_LONG,
    )
    return cases


def _emit_case(spec: dict[str, Any]) -> dict[str, Any]:
    joins: list[Any] = []

    def latest(events: list[Any], *, worktree_root: str | None = None) -> str | None:
        joins.append([len(events), worktree_root])
        return spec["latest"]

    store = _Store(spec["bodies"], spec["gone"], spec["boom"])
    rec = _Recorder(spec["rfail"])
    kw: dict[str, Any] = {}
    if "server" in spec:
        kw["server_session"] = spec["server"]
    out: dict[str, Any]
    with (
        mock.patch.object(hook, "utcnow", lambda: _NOW),
        mock.patch.object(hook, "_latest_in_process_session", latest),
    ):
        try:
            ret = hook._emit_hook_attributions(
                store=store,  # type: ignore[arg-type]
                recorder=rec,  # type: ignore[arg-type]
                recent_events=spec["ev"],
                session_id=spec["sid"],
                assistant_response=spec["reply"],
                worktree_root=spec["wt"],
                client_model=spec["model"],
                **kw,
            )
            out = {"ret": sorted(ret)}
        except Exception as exc:  # noqa: BLE001
            out = {"e": type(exc).__name__}
    out["rows"] = rec.rows
    out["joins"] = joins
    case = {k: v for k, v in spec.items() if k not in ("ev", "bodies")}
    case["ev"] = _dumps(spec["ev"])
    case["bodies"] = _dumps(spec["bodies"])
    case["gone"] = _dumps(spec["gone"])
    case["now"] = _iso(_NOW)
    return {"f": "emit", **case, "o": _dumps(out)}


def generate() -> dict[str, bytes]:
    """``hook_leaf.jsonl``: not gated."""
    cases = _payload_cases()
    cases += [_pending_case(*w) for w in _windows()]
    cases += [_emit_case(s) for s in _emits()]
    header = {
        "k": "header",
        "count": len(cases),
        "lookback": hook._ATTRIBUTION_LOOKBACK_SECONDS,
        "cap": hook._STDIN_PAYLOAD_CAP_BYTES,
    }
    return {"hook_leaf.jsonl": jsonl([header, *cases])}
