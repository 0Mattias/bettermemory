"""Fixtures: `session.py` whole (``session.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/session.rs``. This pins it to
`SessionState` (every method, `_new_session_id`, `PendingUseToken`) and
`SessionRegistry` (`for_request` with `_key_for_ctx`'s fallback,
`known_keys`, `stats`, the LRU and its eviction count).

The effects are scripted: ``time.time`` reads the case's clock and
``secrets.token_hex(8)`` gives a counter from the case's seed, as 16 hex
digits, counted up before each draw. `identity.bind` and
`identity.registry_key` are replaced so that a request's context is its
registry key itself (null for none): what `_key_for_ctx` does with the
key is the reference's own.

A case is ``m`` (the registry's ``max_clients``; absent for a bare
`SessionState`), ``seed`` and the steps ``s``, each:

- ``t``: the clock from this step on (its ``repr``), when it changes;
- ``do``: ``["new"]`` (`SessionState()`, now current), ``["req", key]``
  (`for_request`, its state now current), ``["keys"]``, ``["stats"]``,
  ``["dis", scope]``, ``["ena", scope]``, ``["adv"]``, ``["iss", ids]``,
  ``["con", {"ttl", "min" (a repr), "ovr"}]`` (keywords given only where
  present), ``["pur", id]``, ``["pop"]``, ``["rst"]`` or ``["turn", n]``
  (the field assigned);
- answers: ``r`` the return, ``n`` the clock reads, ``k`` the draws and
  ``st`` the current state after (``sid``, ``ck``, ``sc`` the scopes
  sorted, ``p`` the pending tokens and ``x`` the expired ones in order,
  each ``[id, token, repr(issued_at), issued_at_turn]``, ``tc``).

``req`` answers ``i`` (the state's order of creation in the case), ``ck``
and ``sid``; ``keys`` answers ``o`` (the keys, least recently used first)
and ``s`` (`known_keys` sorted); ``stats`` the dict's items in order.

The last line is a digest: ``count`` random cases from splitmix64
``seed`` (`_random_case`), each case's step answers as compact sorted
JSON, one per line, hashed with SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory import identity, session

from ._common import has_surrogate, jsonl
from .gen_location_stamps import splitmix


class _Fx:
    def __init__(self, seed: int) -> None:
        self.now = 0.0
        self.n = seed
        self.reads = 0
        self.draws = 0

    def time(self) -> float:
        self.reads += 1
        return self.now

    def token_hex(self, nbytes: int | None = None) -> str:
        assert nbytes == 8
        self.draws += 1
        self.n += 1
        return f"{self.n:016x}"


def _tok(mid: str, t: session.PendingUseToken) -> list[Any]:
    assert t.memory_id == mid
    return [mid, t.token, repr(t.issued_at), t.issued_at_turn]


def _snap(st: session.SessionState) -> dict[str, Any]:
    return {
        "sid": st.session_id,
        "ck": st.client_key,
        "sc": sorted(st.disabled_scopes),
        "p": [_tok(m, t) for m, t in st.pending_use_tokens.items()],
        "x": [_tok(m, t) for m, t in st._expired_use_tokens.items()],
        "tc": st.turn_counter,
    }


def run(case: dict[str, Any]) -> list[dict[str, Any]]:
    fx = _Fx(case["seed"])
    reg = session.SessionRegistry(case["m"]) if "m" in case else None
    cur: session.SessionState | None = None
    made: list[session.SessionState] = []
    out = []
    with (
        mock.patch("bettermemory.session.time.time", fx.time),
        mock.patch("bettermemory.session.secrets.token_hex", fx.token_hex),
        mock.patch.object(
            identity, "bind", lambda ctx: types.SimpleNamespace(actor=ctx)
        ),
        mock.patch.object(identity, "registry_key", lambda actor: actor),
    ):
        for step in case["s"]:
            if "t" in step:
                fx.now = float(step["t"])
            fx.reads = fx.draws = 0
            op, *a = step["do"]
            r: Any = None
            if op == "new":
                cur = session.SessionState()
                made.append(cur)
                r = cur.session_id
            elif op == "req":
                assert reg is not None
                cur = reg.for_request(a[0])
                if not any(cur is s for s in made):
                    made.append(cur)
                i = next(j for j, s in enumerate(made) if s is cur)
                r = {"i": i, "ck": cur.client_key, "sid": cur.session_id}
            elif op == "keys":
                assert reg is not None
                r = {"o": list(reg._states), "s": sorted(reg.known_keys())}
            elif op == "stats":
                assert reg is not None
                r = [[k, v] for k, v in reg.stats().items()]
            else:
                assert cur is not None
                if op == "dis":
                    r = sorted(cur.disable(a[0]))
                elif op == "ena":
                    r = sorted(cur.enable(a[0]))
                elif op == "adv":
                    r = cur.advance_turn()
                elif op == "iss":
                    r = [[m, t] for m, t in cur.issue_use_tokens(a[0]).items()]
                elif op == "con":
                    kw: dict[str, Any] = {}
                    if "ttl" in a[0]:
                        kw["ttl_turns"] = a[0]["ttl"]
                    if "min" in a[0]:
                        kw["min_age_seconds"] = (
                            None if a[0]["min"] is None else float(a[0]["min"])
                        )
                    if "ovr" in a[0]:
                        kw["override_ids"] = (
                            None if a[0]["ovr"] is None else set(a[0]["ovr"])
                        )
                    r = cur.consume_old_tokens(**kw)
                elif op == "pur":
                    r = cur.purge_use_token(a[0])
                elif op == "pop":
                    r = [_tok(t.memory_id, t) for t in cur.pop_expired_use_tokens()]
                elif op == "rst":
                    cur.reset()
                elif op == "turn":
                    cur.turn_counter = a[0]
                else:
                    raise ValueError(op)
            out.append(
                {
                    "r": r,
                    "n": fx.reads,
                    "k": fx.draws,
                    "st": None if cur is None else _snap(cur),
                }
            )
    return out


I64 = 2**63


def _st(*steps: Any) -> list[dict[str, Any]]:
    """Steps from ``(t, op...)`` tuples (t None for no change)."""
    out = []
    for t, *do in steps:
        s: dict[str, Any] = {"do": list(do)}
        if t is not None:
            s["t"] = t
        out.append(s)
    return out


def _wire(v: Any) -> Any:
    """A text holding a lone surrogate as ``{"cps": [...]}``, which JSON
    readers that hold UTF-8 can carry; every other value as it is."""
    if isinstance(v, str):
        return {"cps": [ord(ch) for ch in v]} if has_surrogate(v) else v
    if isinstance(v, list):
        return [_wire(x) for x in v]
    if isinstance(v, dict):
        return {k: _wire(x) for k, x in v.items()}
    return v


def _cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []

    def plain(seed: int, *steps: Any) -> None:
        c.append({"seed": seed, "s": _st((None, "new"), *steps)})

    def reg(m: int, seed: int, *steps: Any) -> None:
        c.append({"m": m, "seed": seed, "s": _st(*steps)})

    # Scopes.
    plain(
        0,
        (None, "dis", "b"),
        (None, "dis", "a"),
        (None, "dis", "a"),
        (None, "ena", "zz"),
        (None, "ena", "a"),
        (None, "dis", ""),
        (None, "dis", "é\U0001f600"),
        (None, "rst"),
    )
    # Issue: one time read, draws in list order, a repeated id keeps its
    # first place and its last token; a re-issue keeps the pending place.
    plain(
        41,
        ("1000.0", "iss", ["m1", "m2", "m1", "m3"]),
        ("1001.5", "adv"),
        ("1002.0", "iss", ["m2", "m4"]),
        (None, "iss", []),
        (None, "pur", "m2"),
        (None, "iss", ["m2"]),
        (None, "pur", "nope"),
        (None, "pur", "m2"),
    )
    # Consume: both axes, the default ttl and floor, overrides.
    plain(
        7,
        ("1000.0", "iss", ["a", "b", "c"]),
        (None, "adv"),
        (None, "con", {}),
        (None, "adv"),
        ("1599.0", "con", {}),
        ("1600.0", "con", {"ovr": ["b"]}),
        (None, "con", {"ovr": None}),
        (None, "iss", ["d"]),
        (None, "con", {"ttl": 0, "min": "0.0"}),
        (None, "iss", ["e"]),
        (None, "con", {"ttl": -1, "min": "0.0"}),
        (None, "con", {"ttl": 0, "min": "-0.5"}),
        (None, "iss", ["f", "g"]),
        (None, "con", {"ttl": 0, "min": None, "ovr": []}),
        ("2200.0", "con", {"ttl": 0, "min": None}),
    )
    # Non-finite floors and clocks.
    plain(
        9,
        ("1000.0", "iss", ["a"]),
        ("5000.0", "con", {"ttl": 0, "min": "nan"}),
        (None, "con", {"ttl": 0, "min": "inf"}),
        (None, "con", {"ttl": 0, "min": "-inf"}),
        ("nan", "iss", ["n"]),
        ("1e300", "adv"),
        (None, "con", {"ttl": 0, "min": "0.0"}),
        ("inf", "iss", ["i"]),
        (None, "adv"),
        (None, "pop"),
        ("-inf", "iss", ["j"]),
        ("inf", "adv"),
        (None, "pop"),
    )
    # The turn axis at the integer bounds.
    plain(
        3,
        ("0.0", "turn", -I64),
        (None, "iss", ["a"]),
        (None, "con", {"ttl": I64 - 1, "min": "-1e308"}),
        (None, "con", {"ttl": -I64, "min": "-1e308"}),
        (None, "turn", I64 - 2),
        (None, "con", {"ttl": -I64, "min": "-1e308"}),
        (None, "iss", ["b"]),
        (None, "adv"),
        (None, "con", {"ttl": I64 - 1, "min": "-1e308"}),
        (None, "con", {"ttl": 1, "min": "-1e308"}),
    )
    # Wall-clock eviction: strict at 1800 s, stashed in eviction order,
    # drained once.
    plain(
        100,
        ("1000.0", "iss", ["a", "b"]),
        ("1500.0", "iss", ["c"]),
        ("2800.0", "adv"),
        ("2800.5", "adv"),
        (None, "iss", ["a"]),
        ("3300.25", "adv"),
        (None, "pop"),
        (None, "pop"),
        ("9000.0", "adv"),
        (None, "iss", ["z"]),
        (None, "rst"),
        (None, "pop"),
        (None, "adv"),
        (None, "dis", "s"),
        (None, "rst"),
    )
    # A token evicted again after a re-issue moves to the stash's end.
    plain(
        5,
        ("0.0", "iss", ["a", "b"]),
        ("1800.5", "adv"),
        (None, "iss", ["a"]),
        ("3601.0", "adv"),
        (None, "pop"),
    )
    # The registry.
    reg(
        256,
        0,
        (None, "req", None),
        (None, "req", "__default__"),
        (None, "req", "actor=x"),
        (None, "adv"),
        (None, "req", None),
        (None, "req", "actor=x"),
        (None, "keys"),
        (None, "stats"),
        (None, "req", ""),
        (None, "req", "é\U0001f600"),
        (None, "keys"),
    )
    reg(
        1,
        10,
        (None, "req", "a"),
        (None, "dis", "s"),
        (None, "req", "a"),
        (None, "req", "b"),
        (None, "keys"),
        (None, "stats"),
        (None, "req", "a"),
        (None, "stats"),
        (None, "keys"),
    )
    reg(
        2,
        20,
        (None, "req", "a"),
        (None, "req", "b"),
        (None, "req", "a"),
        (None, "req", "c"),
        (None, "keys"),
        (None, "req", "b"),
        (None, "stats"),
        (None, "req", "a"),
        (None, "req", "c"),
        (None, "keys"),
        (None, "stats"),
    )
    reg(
        0,
        30,
        (None, "req", "a"),
        (None, "keys"),
        (None, "stats"),
        (None, "adv"),
        (None, "req", "a"),
        (None, "stats"),
    )
    reg(-3, 40, (None, "req", "a"), (None, "req", "b"), (None, "keys"), (None, "stats"))
    fill = [(None, "req", f"k{i}") for i in range(257)]
    reg(
        256,
        50,
        *fill,
        (None, "stats"),
        (None, "req", "k0"),
        (None, "req", "k1"),
        (None, "stats"),
        (None, "req", "k256"),
    )
    # Lone surrogates and text past the BMP: code-point order in sorted
    # answers, keys and ids kept as they came.
    odd = ["\ud800", "\udfff", "\U0001f600", "\uffff", "a\udc80", "\ue000"]
    plain(
        60,
        *[(None, "dis", x) for x in odd],
        ("1000.0", "iss", odd),
        (None, "pur", "\udfff"),
        (None, "iss", ["\udfff", "\ud800"]),
        ("1001.0", "con", {"ttl": 0, "min": "0.0", "ovr": ["\ud800"]}),
        (None, "ena", "\ud800"),
    )
    reg(
        3,
        70,
        *[(None, "req", x) for x in odd],
        (None, "keys"),
        (None, "req", "\uffff"),
        (None, "keys"),
    )
    # Past 32 places with removals the pending map is compacted: order and
    # contents after it, with re-issues going last.
    many = [f"m{i:02d}" for i in range(40)]
    plain(
        80,
        ("1000.0", "iss", many),
        *[(None, "pur", m) for m in many[::3] + many[1:12:3]],
        (None, "iss", ["m00", "m39", "m05", "m02"]),
        *[(None, "pur", m) for m in many[14:20]],
        (None, "iss", ["m03"]),
        ("1700.0", "con", {"ttl": 0, "min": "0.0", "ovr": ["m26", "m00"]}),
    )
    return c


IDS = ["m1", "m2", "m3", "m4", "m5"]
KEYS = [None, "__default__", "a", "b", "c"]
DELTAS = ["0.5", "1.0", "599.0", "600.0", "601.0", "1199.0", "1800.0", "1801.0"]
MINS = [None, "0.0", "600.0", "1800.0", "-1.0"]
SEED = 0x5E5510
COUNT = 3000


def _random_case(gen: Iterator[int]) -> dict[str, Any]:
    """One digest case; the native test draws in the same order."""

    def r(n: int) -> int:
        return next(gen) % n

    kind = r(5)
    case: dict[str, Any] = {"seed": r(1 << 32)}
    steps: list[dict[str, Any]] = []
    if kind == 0:
        steps.append({"do": ["new"]})
    else:
        case["m"] = kind - 1
        steps.append({"do": ["req", KEYS[r(5)]]})
    t = 1000.0
    for _ in range(24):
        step: dict[str, Any] = {}
        if r(3) == 0:
            t += float(DELTAS[r(len(DELTAS))])
            step["t"] = repr(t)
        op = r(10)
        if op == 0:
            do: list[Any] = ["adv"]
        elif op == 1:
            do = ["iss", [IDS[r(5)] for _ in range(r(4))]]
        elif op == 2:
            kw: dict[str, Any] = {"ttl": r(5) - 1, "min": MINS[r(5)]}
            if r(2):
                kw["ovr"] = [IDS[r(5)]]
            do = ["con", kw]
        elif op == 3:
            do = ["pur", IDS[r(5)]]
        elif op == 4:
            do = ["pop"]
        elif op == 5:
            do = ["dis", IDS[r(3)]]
        elif op == 6:
            do = ["ena", IDS[r(3)]]
        elif op == 7:
            do = ["rst"] if r(4) == 0 else ["adv"]
        elif op == 8:
            do = ["req", KEYS[r(5)]] if "m" in case else ["adv"]
        else:
            if "m" in case:
                do = ["keys"] if r(2) else ["stats"]
            else:
                do = ["iss", [IDS[r(5)]]]
        step["do"] = do
        steps.append(step)
    case["s"] = steps
    return case


def _digest() -> dict[str, Any]:
    gen = splitmix(SEED)
    h = hashlib.sha256()
    for _ in range(COUNT):
        for ans in run(_random_case(gen)):
            h.update(
                json.dumps(ans, sort_keys=True, separators=(",", ":")).encode("ascii")
            )
            h.update(b"\n")
    return {"digest": {"seed": SEED, "count": COUNT, "sha256": h.hexdigest()}}


def generate() -> dict[str, bytes]:
    rows = []
    for case in _cases():
        answers = run(case)
        rows.append(
            _wire(
                {
                    **case,
                    "s": [{**s, **a} for s, a in zip(case["s"], answers, strict=True)],
                }
            )
        )
    header = {"file": "session", "cases": len(rows)}
    return {"session.jsonl": jsonl([header, *rows, _digest()])}
