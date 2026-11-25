"""Fixtures: `history`'s fifth unit, a record's history as `memory history`
prints it (``history_render.jsonl``, ``history_render_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/history_render.rs``. This pins it to
`render_text`, `render_json`, `_actor_text` and `_value_text`.

The functions are pure, so every `RecordHistory` and `HistoryRow` is built
directly: no store, no key, no clock. A rendered text is written as
`_common.text_field` (``v``, or ``v_cps``); an exception as ``e``, its
class name. Cases, by ``f``:

- ``actor``: ``a`` an actor in `_common.neutral` form; ``r`` what
  `_actor_text(a)` returns.
- ``value``: ``v`` a detail value, neutral; ``r`` `_value_text(v)`.
- ``render``: ``h`` a history (below); ``t`` and ``td`` `render_text`
  with ``diff`` False and True, ``j`` and ``jd`` `render_json` likewise.
- ``sweep``: ``count`` histories drawn from ``pools`` by the rule of
  `_draw` under splitmix64 seeded with ``seed``; ``sha256`` of the four
  renders of each, in that order, each a text or ``!`` and the
  exception's name, each followed by a NUL, UTF-8 with surrogates passed.

A history is ``{"id", "rk", "st", "pv", "kp", "rows"}`` (record id as a
`text_field`, record kind, state, provenance, key present) and a row
``{"seq", "ts", "se", "k", "ro", "a", "v", "ac", "as", "ch", "d", "df"}``
(seq, ts, session, kind, role, action, verified, actor neutral or
absent for None, actor source, changed neutral, detail as neutral
``[name, value]`` pairs, diff as a `text_field` or absent for None).

The ungated file holds ASCII, Latin-1 and code points whose category has
not moved since Unicode 14 (the oldest Unicode of the Pythons CI runs),
and no value nested near a recursion bound: 3.11 and 3.12 encode an
indented document in Python and stop near a thousand levels.
``history_render_unicode.jsonl`` (``generate_unicode``, gated on the
Unicode data) sweeps every code point through `render_text`, whose
`printable` escapes by category.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

from bettermemory import history as h

from ._common import jsonl, neutral, text_field

MASK = (1 << 64) - 1


class SplitMix:
    """splitmix64: the same draws in the generator and the native test."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return self.next() % n

    def choice(self, pool: list[Any]) -> Any:
        return pool[self.below(len(pool))]


def _out(name: str, call: Callable[[], str]) -> dict[str, Any]:
    try:
        return text_field(name, call())
    except Exception as e:  # noqa: BLE001 - the class is the answer
        return {f"{name}_e": type(e).__name__}


def _row(
    seq: int = 1,
    *,
    ts: str = "2026-01-02T03:04:05Z",
    session: str | None = None,
    kind: str = "memory_put",
    role: str = "record",
    action: str = "created",
    verified: bool | None = True,
    actor: Any = None,
    actor_source: str | None = None,
    changed: tuple[str, ...] = (),
    detail: dict[Any, Any] | None = None,
    diff: str | None = None,
) -> h.HistoryRow:
    return h.HistoryRow(
        seq=seq,
        ts=ts,
        session=session,
        kind=kind,
        role=role,
        action=action,
        verified=verified,
        actor=actor,
        actor_source=actor_source,
        changed=changed,
        detail={} if detail is None else detail,
        diff=diff,
    )


def _hist(
    *rows: h.HistoryRow,
    record_id: str = "01JHISTORYREC",
    record_kind: str | None = "memory",
    state: str = "active",
    provenance: str | None = None,
    key_present: bool = True,
) -> h.RecordHistory:
    return h.RecordHistory(
        record_id=record_id,
        record_kind=record_kind,
        state=state,
        provenance=provenance,
        key_present=key_present,
        rows=tuple(rows),
    )


def _row_form(r: h.HistoryRow) -> dict[str, Any]:
    out: dict[str, Any] = {
        "seq": r.seq,
        "ts": r.ts,
        "se": r.session,
        "k": r.kind,
        "ro": r.role,
        "a": r.action,
        "v": r.verified,
        "as": r.actor_source,
        "ch": neutral(r.changed),
        "d": [[neutral(k), neutral(v)] for k, v in r.detail.items()],
    }
    if r.actor is not None:
        out["ac"] = neutral(r.actor)
    if r.diff is not None:
        out.update(text_field("df", r.diff))
    return out


def _form(x: h.RecordHistory) -> dict[str, Any]:
    return {
        **text_field("id", x.record_id),
        "rk": x.record_kind,
        "st": x.state,
        "pv": x.provenance,
        "kp": x.key_present,
        "rows": [_row_form(r) for r in x.rows],
    }


def _render(x: h.RecordHistory) -> dict[str, Any]:
    return {
        "f": "render",
        "h": _form(x),
        **_out("t", lambda: h.render_text(x)),
        **_out("td", lambda: h.render_text(x, diff=True)),
        **_out("j", lambda: h.render_json(x)),
        **_out("jd", lambda: h.render_json(x, diff=True)),
    }


# ---------------------------------------------------------------------------
# `_actor_text` and `_value_text`
# ---------------------------------------------------------------------------

_ACTORS: list[Any] = [
    {},
    {"client": "c", "model": "m", "principal": "p"},
    {"principal": "p", "model": "m", "client": "c"},
    {"model": "m"},
    {"client": 1, "model": None, "principal": ""},
    {"client": ["c"], "model": {"m": 1}, "principal": True},
    {"client": 1.5, "model": "", "principal": "p"},
    {"client": "c", "Client": "X", "agent": "a", "session": "s"},
    {"client": "c d", "model": "m\nx\x1b[2J"},
    {"client": "\u00e9\U0001f600", "principal": "\u202eevil"},
    {"client": "\ud800"},
    [],
    ["client"],
    "client=c",
    None,
]


def _nested(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = [value]
    return value


_VALUES: list[Any] = [
    "",
    "x",
    "x" * 77,
    "x" * 80,
    "x" * 81,
    "x" * 200,
    "\u00e9" * 80,
    "\u00e9" * 81,
    "\U0001f600" * 80,
    "\U0001f600" * 81,
    "a" * 76 + "\U0001f600" * 5,
    "a" * 77 + "\U0001f600\U0001f600\U0001f600\U0001f600",
    "\ud800",
    "\ud800" * 81,
    "a" * 79 + "\ud800\udc00",
    "line\nfeed\x1b[31m\u202e",
    True,
    False,
    None,
    0,
    -1,
    2**64,
    10**4299,
    1.5,
    -0.0,
    1e16,
    1e-05,
    0.1 + 0.2,
    float("nan"),
    float("inf"),
    float("-inf"),
    [],
    {},
    [1, "a", None, True, 2.5],
    {"b": 1, "a": {"d": 2, "c": [3, {"z": 0, "y": 1}]}},
    {"\u00e9": 1, "e": 2, "z": 3, "E": 4},
    ['\u00e9\U0001f600\n\t"'],
    {"\ud800": "\udc00"},
    list(range(40)),
    {"k" + str(i): i for i in range(12)},
    {1: "a", 2: "b"},
    {2.5: "a", True: "b", None: "c"},
    {1: "a", "b": 2},
    (1, "a"),
    datetime(2026, 1, 2, 3, 4, 5),
    [datetime(2026, 1, 2)],
    _nested(50),
]


def _helper_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"f": "actor", "a": neutral(a), **_out("r", partial(h._actor_text, a))}
        for a in _ACTORS
    ]
    out += [
        {"f": "value", "v": neutral(v), **_out("r", partial(h._value_text, v))}
        for v in _VALUES
    ]
    return out


# ---------------------------------------------------------------------------
# Whole histories
# ---------------------------------------------------------------------------

_HOSTILE = "local\n  findings\x1b[2J\u202e\u2028x\u2029\x85\x7f\u00ad"


def _render_cases() -> list[h.RecordHistory]:
    full = _row(
        7,
        ts="2026-03-04T05:06:07.123456+00:00",
        session="s1",
        kind="memory_put",
        action="updated",
        verified=False,
        actor={"client": "cli", "model": "m1", "principal": "me", "x": "y"},
        actor_source=h.ACTOR_FROM_RECORD,
        changed=("body", "scope", "tags"),
        detail={"status": "ok", "count": 3, "fields": ["a", "b"], "nan": float("nan")},
        diff="--- before\n+++ after\n@@ -1 +1 @@\n-old\n+new\n",
    )
    out = [
        # Rows: none, one, two; the kind; each state; the key.
        _hist(record_kind=None, state="unknown"),
        _hist(_row(), provenance="local", key_present=False),
        _hist(_row(1), _row(2, action="updated", verified=None)),
        _hist(record_kind="episode", state="pruned", key_present=False),
        _hist(_row(), record_kind="", state="deleted"),
        _hist(_row(), state="missing"),
        _hist(_row(), state="tombstoned", provenance="unaccounted", key_present=False),
        _hist(_row(), state="tombstoned", provenance="unaccounted"),
        _hist(_row(), state="tombstoned", key_present=False),
        _hist(_row(), provenance="imported", key_present=False),
        _hist(_row(), provenance="imported"),
        _hist(_row(), provenance="unaccounted", key_present=False),
        _hist(_row(), provenance="local"),
        _hist(_row(), state="pruned", provenance="local", key_present=False),
        _hist(_row(), provenance="", key_present=False),
        # A hostile label, record id, kind, ts, action and detail.
        _hist(
            _row(
                3,
                ts="2026\n  seq 4",
                kind="memory_put\n  seq 9 t memory_put created [mac ok]",
                action="act\x1b]8;;x\x07",
                detail={
                    "reason": "v\n  findings\x1b[31m\u202e",
                    "n\x1b[0m\n": "w",
                },
                diff="a\x1b[2J\nb\u202e\r\n\u2028c",
            ),
            record_id="01J\n  key not held here\u202e",
            provenance=_HOSTILE,
        ),
        _hist(_row(), state="active\nx", provenance=_HOSTILE, key_present=False),
        # Verdicts and findings: none, one, all three kinds.
        _hist(_row(1, verified=True), _row(2, verified=False), _row(3, verified=None)),
        _hist(
            _row(1, verified=False, kind="tombstone_put"),
            _row(2, verified=False, kind="write", role="event"),
            state="missing",
            provenance="unaccounted",
        ),
        # Actors and their sources.
        _hist(
            _row(1, actor=None, actor_source=h.ACTOR_FROM_RECORD),
            _row(2, actor={}, actor_source=h.ACTOR_FROM_RECORD),
            _row(3, actor={}, actor_source=h.ACTOR_FROM_EVENT),
            _row(4, actor={"client": "c"}, actor_source=None),
            _row(5, actor={"client": "c", "model": "m", "principal": "p"}),
            _row(
                6,
                actor={"client": 2, "model": "", "principal": "p", "y": "z"},
                actor_source=h.ACTOR_FROM_EVENT,
            ),
            _row(7, actor={"model": "m"}, actor_source="Record"),
        ),
        # Changed: none, one, several.
        _hist(
            _row(1),
            _row(2, changed=("body",)),
            _row(3, changed=("body", "scope", "tags", "")),
        ),
        # Detail values of every JSON type, in insertion order.
        _hist(
            _row(
                detail={
                    "z": "text",
                    "a": True,
                    "n": None,
                    "i": -12,
                    "f": 0.5,
                    "l": [3, 1, 2],
                    "d": {"b": 1, "a": 2},
                    "e": "",
                    "u": "\u00e9\U0001f600",
                    "x80": "y" * 80,
                    "x81": "y" * 81,
                    "inf": float("inf"),
                    "ninf": float("-inf"),
                }
            )
        ),
        _hist(_row(detail={"s": "\ud800", "a": "\U0001f600" * 81, "l": ["\ud800"]})),
        # Detail names that are not text.
        _hist(_row(detail={1: "one", None: 0, 2.5: [1], False: "f"})),
        _hist(_row(detail={(1, "a"): "pair"})),
        # Diffs.
        _hist(
            _row(1, diff=None),
            _row(2, diff=""),
            _row(3, diff="one line"),
            _row(4, diff="a\nb\nc"),
            _row(5, diff="a\nb\n"),
            _row(6, diff="   \n  \n"),
            _row(7, diff="\n"),
            _row(8, diff="\ud800\n\U0001f600"),
        ),
        # Every optional part at once.
        _hist(full, provenance="local", key_present=False),
        _hist(full, full, state="tombstoned", provenance="unaccounted"),
        # Ids, seqs and texts at the edges.
        _hist(
            _row(0, ts="", kind="", action=""),
            _row(-5),
            _row(2**63 - 1, session="\u00e9"),
            record_id="",
        ),
        _hist(
            _row(changed=("\ud800", "b\u00e9")),
            record_id="R\ud800\U0001f600\u00e9",
        ),
        # Errors: an actor that is not a mapping, values json.dumps refuses.
        _hist(_row(actor=["client"])),
        _hist(_row(actor="client=c", actor_source=h.ACTOR_FROM_EVENT)),
        _hist(_row(detail={"when": datetime(2026, 1, 2)})),
        _hist(_row(detail={"mixed": {1: "a", "b": 2}})),
        _hist(_row(actor=[], detail={"when": datetime(2026, 1, 2)})),
        _hist(_row(detail={"deep": _nested(20)})),
    ]
    return out


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

SWEEP_SEED = 20261124
SWEEP_COUNT = 600

POOLS: dict[str, list[Any]] = {
    "ids": ["01JA", "01JB\n", "R\u202e", "", "\u00e9\U0001f600"],
    "rkinds": [None, "memory", "episode", ""],
    "states": ["active", "tombstoned", "unknown", "deleted", "pruned", "missing"],
    "provs": [None, "local", "imported", "unaccounted", "x\ny\x1b", ""],
    "ts": ["2026-01-02T03:04:05Z", "t\n", ""],
    "sessions": [None, "s", "s\u2028"],
    "kinds": ["memory_put", "tombstone_put", "write", "memory_delete", "k\x1b"],
    "roles": ["record", "event", "reference", "store"],
    "actions": ["created", "updated", "removed", "a\u0085"],
    "actors": [
        None,
        {},
        {"client": "c"},
        {"client": "c", "model": "m", "principal": "p"},
        {"model": 1, "principal": ""},
    ],
    "sources": [None, "record", "event"],
    "changed": ["body", "scope", "", "t\u00e9"],
    "names": ["status", "reason", "n\n", ""],
    "values": [
        "v",
        "",
        "y" * 81,
        "\U0001f600" * 79 + "ab",
        "l\n\u202e",
        0,
        -7,
        1.25,
        float("nan"),
        True,
        None,
        [],
        {},
        [1, "a"],
        {"b": 1, "a": [2]},
        {1: "a", "b": 2},
    ],
    "diffs": [None, "", "a", "a\nb\n", "  \n", "-x\n+y"],
}


def _draw(rng: SplitMix, pools: dict[str, list[Any]]) -> h.RecordHistory:
    """One history, its draws in this order."""
    record_id = rng.choice(pools["ids"])
    record_kind = rng.choice(pools["rkinds"])
    state = rng.choice(pools["states"])
    provenance = rng.choice(pools["provs"])
    key_present = rng.below(2) == 1
    rows = []
    for _ in range(rng.below(4)):
        seq = rng.below(1000)
        ts = rng.choice(pools["ts"])
        session = rng.choice(pools["sessions"])
        kind = rng.choice(pools["kinds"])
        role = rng.choice(pools["roles"])
        action = rng.choice(pools["actions"])
        verified = rng.choice([True, False, None])
        actor = rng.choice(pools["actors"])
        source = rng.choice(pools["sources"])
        changed = tuple(rng.choice(pools["changed"]) for _ in range(rng.below(3)))
        detail: dict[Any, Any] = {}
        for _ in range(rng.below(4)):
            name = rng.choice(pools["names"])
            detail[name] = rng.choice(pools["values"])
        diff = rng.choice(pools["diffs"])
        rows.append(
            _row(
                seq,
                ts=ts,
                session=session,
                kind=kind,
                role=role,
                action=action,
                verified=verified,
                actor=actor,
                actor_source=source,
                changed=changed,
                detail=detail,
                diff=diff,
            )
        )
    return _hist(
        *rows,
        record_id=record_id,
        record_kind=record_kind,
        state=state,
        provenance=provenance,
        key_present=key_present,
    )


def _answer(call: Callable[[], str]) -> str:
    try:
        return call()
    except Exception as e:  # noqa: BLE001 - the class is the answer
        return "!" + type(e).__name__


def _renders(x: h.RecordHistory) -> str:
    return "".join(
        _answer(call) + "\0"
        for call in (
            lambda: h.render_text(x),
            lambda: h.render_text(x, diff=True),
            lambda: h.render_json(x),
            lambda: h.render_json(x, diff=True),
        )
    )


def _sweep() -> dict[str, Any]:
    rng = SplitMix(SWEEP_SEED)
    blob = "".join(_renders(_draw(rng, POOLS)) for _ in range(SWEEP_COUNT))
    return {
        "f": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "pools": {k: [neutral(v) for v in pool] for k, pool in POOLS.items()},
        "sha256": hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    cases = _helper_cases() + [_render(x) for x in _render_cases()] + [_sweep()]
    return {"history_render.jsonl": jsonl([{"cases": len(cases)}, *cases])}


# ---------------------------------------------------------------------------
# Every code point through `render_text` (gated on the Unicode data)
# ---------------------------------------------------------------------------

UNICODE_CHUNK = 64
UNICODE_BLOCK = 0x10000


def unicode_history(text: str) -> h.RecordHistory:
    """The history a chunk of code points is rendered in: the chunk as
    the record id, an actor's client, a changed field, a detail name and
    value, inside a list value, and the diff."""
    return _hist(
        _row(
            actor={"client": text},
            actor_source=h.ACTOR_FROM_EVENT,
            changed=(text,),
            detail={text: text, "w": [text]},
            diff=text,
        ),
        record_id=text,
        provenance="p",
    )


def _unicode_block(lo: int) -> dict[str, Any]:
    parts = []
    for start in range(lo, lo + UNICODE_BLOCK, UNICODE_CHUNK):
        text = "".join(chr(c) for c in range(start, start + UNICODE_CHUNK))
        parts.append(h.render_text(unicode_history(text), diff=True))
    blob = "\0".join(parts).encode("utf-8", "surrogatepass")
    return {
        "f": "block",
        "lo": lo,
        "count": len(parts),
        "sha256": hashlib.sha256(blob).hexdigest(),
    }


def generate_unicode() -> dict[str, bytes]:
    rows = [_unicode_block(lo) for lo in range(0, 0x110000, UNICODE_BLOCK)]
    header = {"cases": len(rows), "chunk": UNICODE_CHUNK, "block": UNICODE_BLOCK}
    return {"history_render_unicode.jsonl": jsonl([header, *rows])}
