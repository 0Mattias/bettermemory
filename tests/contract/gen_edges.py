"""Fixtures 5b and 2b: behaviour that depends on the CPython version.

``datetime_edges.jsonl``: ``datetime.fromisoformat`` as the product's
readers call it, on the spellings its C parser (3.11 on, changed in 3.12,
3.13 and 3.14) reads in ways a port would not guess; the formatters on
offsets below a minute; ``review.ulid_time`` and the
``datetime.fromtimestamp`` rounding under it.

``json_edges.jsonl``: dicts whose keys include a NaN through every JSON
form of ``canonical_json.jsonl`` (`list.sort` leaves a NaN, which
compares false both ways, where its run detection puts it, and 3.13
changed that), and how deep a value each JSON call site serialises or
reads before RecursionError. The limit is CPython's C recursion budget,
whose size differs by version (3.11 counts Python's recursion limit,
3.12 on a separate C budget); each probe runs at the top of a fresh
thread so the caller's stack does not lower it.

Both are compared only under the Python version they were generated
under (`generate.PYTHON_VERSION`).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import log as blog
from bettermemory.store_schema import SCHEMA

from . import gen_json, gen_log_ring, gen_time
from ._common import float_bits, jsonl, neutral, text_field

# ---------------------------------------------------------------------------
# datetime_edges.jsonl
# ---------------------------------------------------------------------------

_D = "2026-11-15"
_T = "08:09:10"


def _separators() -> list[str]:
    """Any one character between the date and the time."""
    seps = [
        "T",
        "t",
        " ",
        "x",
        "Z",
        "z",
        "+",
        "-",
        ":",
        ".",
        ",",
        "0",
        "1",
        "_",
        "\x00",
        "\t",
        "\n",
        "\u00e9",
        "\u00a0",
        "\u2028",
        "\u3000",
        "\uff34",
        "\U0001f600",
        "\udc80",
    ]
    return [_D + s + _T for s in seps] + [_D + "TT" + _T, _D + _T, _D + "  " + _T]


def _times() -> list[str]:
    """Fractions after the hour or the minute, a fourth ``:``, compact and
    truncated times."""
    return [
        f"{_D}T08,5",
        f"{_D}T08.5",
        f"{_D}T08:09,5",
        f"{_D}T08:09.5",
        f"{_D}T08:09:10,5",
        f"{_D}T08:09:10:5",
        f"{_D}T08:09:10:55",
        f"{_D}T08:09:10:5Z",
        f"{_D}T08:09:10.5:5",
        f"{_D}T08:09:10.",
        f"{_D}T08:09:10,",
        f"{_D}T08:09:10.1234567",
        f"{_D}T08:09:10.123456789",
        f"{_D}T08:09:10.1234",
        f"{_D}T08:09:10.12",
        f"{_D}T080910",
        f"{_D}T080910.5",
        f"{_D}T0809",
        f"{_D}T08:0910",
        f"{_D}T0809:10",
        f"{_D}T8:09:10",
        f"{_D}T08:9:10",
        f"{_D}T08:09:1",
        f"{_D}T08:",
        f"{_D}T",
        f"{_D}T08:09:10.\u0661",
        f"{_D}T\u0660\u0668:09:10",
        f"{_D}T24:00:00",
        f"{_D}T24:00:00.000001",
        f"{_D}T23:59:60",
        "2026-11-15T08:09:10.5+01:00:00.5",
        "20261115",
        "20261115T080910",
        "2026-1115",
        "202611-15",
        "2026W467",
        "2026-W46-7T08:09:10",
        "2026W46",
        "2026-W53-1",
        "2026-W46-8",
    ]


def _offsets() -> list[str]:
    """``.`` or ``,`` inside an offset, offsets below a minute and below a
    second, a NUL after ``Z``, ``Z`` where an offset or a separator goes."""
    stamp = f"{_D}T{_T}"
    tails = [
        "+02.00",
        "+02,00",
        "+02:00.5",
        "+02:00,5",
        "+0200.5",
        "+02:00:30",
        "+02:00:30.5",
        "+02:00:30.000001",
        "+02:00:30,25",
        "+020030",
        "+020030.5",
        "+00:00:30",
        "-00:00:30",
        "+00:00:01",
        "+00:00:00.5",
        "+00:00:00.000001",
        "-00:00:00.000001",
        "+00:00:00.000000",
        "+00:00:00",
        "-00:00",
        "+00:00:00.0",
        "+00:00:00.0000001",
        "+23:59:59.999999",
        "-23:59:59.999999",
        "+24:00",
        "+12:60",
        "+12:00:60",
        "+1",
        "+12:3",
        "+12:345",
        "+12:00:00:00",
        "Z\x00",
        "\x00",
        "Z\x00Z",
        "Z ",
        "ZZ",
        "Z+00:00",
        "+00:00Z",
        "z",
        "+02:00Z",
        "UTC",
        "+ 02:00",
        "\u221202:00",
        "+\u0660\u0662:00",
    ]
    return [stamp + t for t in tails] + [
        f"{_D}T08:09:10.123+02:00:30",
        f"{_D}T08:09+02:00:30",
        f"{_D}T08+02:00:30",
        f"{_D}T08Z",
        f"{_D}Z",
        f"{_D}+02:00",
        f"{_D}T08:09:10.123Z\x00",
        f"Z{_D}T{_T}",
        f"{_D}Z08:09:10Z",
    ]


def _ulid(ms: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    chars = []
    for _ in range(10):
        chars.append(alphabet[ms % 32])
        ms //= 32
    return "".join(reversed(chars)) + "0" * 16


_LAST_MS = 253402300799999


def ulid_millis() -> list[int]:
    """Millisecond counts a ULID spells. ``ms / 1000`` is a double whose
    spacing passes a microsecond near the year 9999, so ``fromtimestamp``
    rounds it (half to even) to a microsecond that is not ``ms % 1000 *
    1000``. The middle values come from a fixed linear congruence, no
    random source."""
    out = [0, 1, 2, 499, 500, 501, 999, 1000, 1001, 2**31, 2**32 - 1, 2**40]
    out += [1763194150123, 1763194150999, 4102444800000, 32503680000000]
    state = 12345
    for _ in range(40):
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        out.append(state % (_LAST_MS + 1))
    out += [_LAST_MS - k for k in (999, 501, 500, 499, 3, 2, 1, 0)]
    out += [_LAST_MS + 1, 2**48 - 1]
    return out


def fromtimestamp_inputs() -> list[float]:
    """Doubles whose microseconds, as `datetime.fromtimestamp` computes
    them (the fraction times 1e6 in double arithmetic), land on or next
    to a half: odd multiples of 1/128 s are exact halves of a microsecond
    (7812.5 us)."""
    out: list[float] = []
    for whole in (0.0, 1.0, 1e9, 1763194150.0, 2.0**33, -1.0, 253402300799.0):
        for k in (1, 3, 5, 127):
            x = whole + k / 128
            out += [x, math.nextafter(x, math.inf), math.nextafter(x, -math.inf)]
    out += [k * 0.5e-6 for k in (1, 3, 5, 7, 9, 1001)]
    out += [-0.5e-6, -1.5e-6, -0.0078125, -2.5e-6, 1e9 + 0.5e-6, 1e9 + 1.5e-6]
    out += [0.0000005, 0.0000025, 0.9999995, 0.9999994999999999, 1.9999995]
    out += [253402300799.9999995, 253402300800.0, -62135596800.0, -62135596801.0]
    return out


def _fromtimestamp(x: float) -> dict[str, Any]:
    try:
        return {"v": datetime.fromtimestamp(x, tz=timezone.utc).isoformat()}
    except Exception as exc:  # noqa: BLE001 - the error type is the fixture
        return {"error": type(exc).__name__}


#: Offsets below a minute and below a second, through every formatter:
#: `isoformat_utc` replaces every ``+00:00`` in the isoformat text, so an
#: offset of 30 s, ``+00:00:30``, comes out as ``Z:30``; pydantic rounds
#: the offset's seconds half away from zero before it writes ``Z`` or
#: whole minutes (0.5 s is ``+00:00``, 0.499999 s ``Z``).
_FORMAT_OFFSETS_US = [
    0,
    1,
    -1,
    499999,
    -499999,
    500000,
    -500000,
    90 * 1000000,
    -59999999,
    3599500000,
    30 * 1000000,
    -30 * 1000000,
    59 * 1000000 + 999999,
    3600 * 1000000 + 1,
    -(3600 * 1000000) - 1,
    (23 * 3600 + 59 * 60 + 59) * 1000000 + 999999,
]
_FORMAT_FIELDS = [(2026, 11, 15, 8, 9, 10, 0), (2026, 11, 15, 8, 9, 10, 123456)]


def _format_records() -> list[dict[str, Any]]:
    out = []
    for fields in _FORMAT_FIELDS:
        for us in _FORMAT_OFFSETS_US:
            dt = datetime(*fields, tzinfo=timezone(timedelta(microseconds=us)))
            out.append(
                {
                    "datetime": {"fields": list(fields), "offset_us": us},
                    "out": {
                        k: gen_time._format(k, fn, dt)
                        for k, (fn, _) in gen_time.FORMATTERS.items()
                    },
                }
            )
    return out


def datetime_texts() -> list[str]:
    texts = _separators() + _times() + _offsets()
    texts += [_ulid(ms) for ms in ulid_millis()]
    texts += [_ulid(1763194150123).lower(), _ulid(5)[:-1] + "U", _ulid(5)[:-1] + "I"]
    assert len(set(texts)) == len(texts)
    return texts


def _datetime_edges() -> bytes:
    records: list[Any] = [
        {
            "fixture": "datetime_edges",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "formatters": {k: v[1] for k, v in gen_time.FORMATTERS.items()},
            "parsers": {k: v[1] for k, v in gen_time.PARSERS.items()},
            "records": (
                "datetime: fields and offset_us (the offset in microseconds) "
                "through each formatter; text (or text_cps when it holds a "
                "surrogate) through each parser; timestamp (IEEE-754 bits and "
                "repr) through datetime.fromtimestamp(x, tz=timezone.utc), as "
                "isoformat(). Each result is v, or error: the exception type"
            ),
            "accidents": [
                "isoformat_utc replaces every '+00:00' of isoformat(), so an "
                "offset of +00:00:30 is written 'Z:30' and +00:00:00.000001 "
                "'Z:00.000001' (time_utils.py:70)",
                "store_rows._dt and v8._as_dt replace every 'Z' before parsing, "
                "so a Z anywhere becomes '+00:00' there: 'Z' as the date/time "
                "separator or a second Z is spliced into the text "
                "(store_rows.py:71, v8.py:145); parse_event_ts replaces only "
                "a trailing Z (time_utils.py:45)",
                "fromisoformat takes any one character, non-ASCII included, as "
                "the date/time separator, and every reader above inherits it",
                "fromisoformat (CPython's C parser) reads an offset of zero hours, "
                "minutes and seconds with a fraction, +00:00:00.5, as UTC, the "
                "fraction dropped, while +02:00:00.5 keeps it; reads offset "
                "minutes or seconds of 60 as carrying (+12:60 is +13:00); reads "
                "what follows a fourth ':' of the time as its fraction "
                "(08:09:10:5 is 08:09:10.5); takes a fraction after the hour or "
                "the minute (08,5 is 08:00:00.5); and takes '.' or ',' between "
                "an offset's hours and minutes (+02.00). Every reader above "
                "inherits these (time_utils.py:46, store_rows.py:71, v8.py:145, "
                "origin_history.py:398)",
                "pydantic's JSON spelling (responses) rounds an offset's "
                "total_seconds() half away from zero to whole seconds, then "
                "writes Z for zero and else whole minutes: +00:00:00.5 is "
                "'+00:00', +00:00:00.499999 'Z', +00:00:59.999999 '+00:01'",
            ],
        }
    ]
    records += _format_records()
    for text in datetime_texts():
        records.append(
            {
                **text_field("text", text),
                "out": {
                    k: gen_time._run(fn, text)
                    for k, (fn, _) in gen_time.PARSERS.items()
                },
            }
        )
    for x in fromtimestamp_inputs():
        records.append(
            {
                "timestamp": {"bits": float_bits(x), "repr": repr(x)},
                "out": _fromtimestamp(x),
            }
        )
    return jsonl(records)


# ---------------------------------------------------------------------------
# json_edges.jsonl
# ---------------------------------------------------------------------------


def nan_key_values() -> list[tuple[str, Any]]:
    """Dicts with NaN keys (each a distinct float object), few enough for
    `list.sort`'s binary insertion (under 64 items): ascending and
    descending runs before, around and after the NaN."""
    nan = [float("nan") for _ in range(3)]
    return [
        ("two_nans", {nan[0]: 1, nan[1]: 2}),
        ("nan_alone_with_one", {nan[0]: "a", 1: "b"}),
        ("one_then_nan", {1: "a", nan[0]: "b"}),
        ("nan_among_ints", {3: "a", nan[0]: "b", 1: "c", 2: "d"}),
        ("nan_first_descending", {nan[0]: "a", 2: "b", 1: "c"}),
        ("nan_first_ascending", {nan[0]: "a", 1: "b", 2: "c"}),
        ("descending_then_nan", {5: "a", 4: "b", nan[0]: "c", 3: "d", 2: "e"}),
        ("ascending_then_nan", {1: "a", 2: "b", nan[0]: "c", 0: "d", 3: "e"}),
        ("nan_last", {2: "a", 1: "b", 0: "c", nan[0]: "d"}),
        ("nans_apart", {nan[0]: "a", 2: "b", nan[1]: "c", 1: "d", 0: "e"}),
        ("three_nans_floats", {nan[0]: 1, 0.5: 2, nan[1]: 3, -0.5: 4, nan[2]: 5}),
        ("nan_with_infinities", {float("inf"): 1, nan[0]: 2, float("-inf"): 3}),
        ("nan_with_bools", {True: 1, nan[0]: 2, False: 3}),
        ("nan_nested", {"z": {nan[0]: 1, 3: 2, 2: 3}, "a": [{1: 0, nan[1]: 0}]}),
    ]


def nested(shape: str, depth: int) -> Any:
    """``depth`` containers, one inside the next, around 0: lists, or
    dicts keyed ``"a"``."""
    value: Any = 0
    for _ in range(depth):
        value = [value] if shape == "list" else {"a": value}
    return value


def nested_text(shape: str, depth: int) -> str:
    if shape == "list":
        return "[" * depth + "0" + "]" * depth
    return '{"a":' * depth + "0" + "}" * depth


def _attempt(call: Callable[[str, int], Any], shape: str, depth: int) -> dict[str, Any]:
    """One call: ``value`` is what it returned, or ``error`` what it
    raised. Every probe makes it from the same frame depth."""
    try:
        value = call(shape, depth)
    except Exception as exc:  # noqa: BLE001 - the error type is the fixture
        return {"error": type(exc).__name__}
    return {"ok": True, "value": value}


def _dumps_call(fn: Callable[[Any], str | bytes]) -> Callable[[str, int], Any]:
    return lambda shape, depth: fn(nested(shape, depth))


def _verify_with(
    head: bytes | None = None, origin: bytes | None = None, rekey: str | None = None
) -> tuple[str, str]:
    """`verify_chain` over the two-key chain of ``log_keyring.jsonl``, ring
    B current and A retired, with the head, origin or rekey payload given:
    the report's status and its head's."""
    rows = gen_log_ring.two_key(rekey)
    with tempfile.TemporaryDirectory() as tmp:
        kr = blog.KeyRing(Path(tmp), gen_log_ring.STORE_ID)
        kr.current_path().write_bytes(gen_log_ring.KEY_B)
        kr.retired_path(gen_log_ring.FP_A).write_bytes(gen_log_ring.KEY_A)
        if head is not None:
            kr.head_path().write_bytes(head)
        if origin is not None:
            kr.origin_path().write_bytes(origin)
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        try:
            report = blog.verify_chain(
                conn, keyring=kr, current_fingerprint=gen_log_ring.FP_B
            )
        finally:
            conn.close()
    return report.status, str(report.head.get("status"))


def _head_nested(shape: str, depth: int) -> tuple[str, str]:
    """A head file whose ``mac`` is ``depth`` containers deep (the file
    one deeper): `json.loads` reads it, then ``str()`` spells it."""
    text = '{"seq": 6, "mac": ' + nested_text(shape, depth) + "}"
    return _verify_with(head=text.encode())


def _origin_nested(shape: str, depth: int) -> tuple[str, str]:
    text = '{"seq": 1, "mac": "x", "extra": ' + nested_text(shape, depth) + "}"
    return _verify_with(origin=text.encode())


def _rekey_nested(shape: str, depth: int) -> tuple[str, str]:
    pair = gen_log_ring.rekey_text(gen_log_ring.FP_A, gen_log_ring.FP_B)
    return _verify_with(rekey=pair[:-1] + ',"x":' + nested_text(shape, depth) + "}")


def sites() -> dict[str, tuple[str, Callable[[str, int], Any]]]:
    """Each call site: what ``depth`` counts, and the call."""
    out: dict[str, tuple[str, Callable[[str, int], Any]]] = {}
    for name, (fn, _, _) in gen_json.FORMS.items():
        out[f"dumps {name}"] = (
            "the value is nested(shape, depth)",
            _dumps_call(fn),
        )
    out["dumps log_payload_in_dict"] = (
        "the payload is {'p': nested(shape, depth)} (depth + 1 containers)",
        lambda shape, depth: blog.canonical_payload({"p": nested(shape, depth)}),
    )
    out["loads"] = (
        "json.loads(nested_text(shape, depth))",
        lambda shape, depth: json.loads(nested_text(shape, depth)),
    )
    out["repr"] = (
        "repr(nested(shape, depth))",
        lambda shape, depth: repr(nested(shape, depth)),
    )
    out["verify head_mac"] = (
        "the head file {'seq': 6, 'mac': <nested_text>} (depth + 1 deep), "
        "two-key chain of log_keyring.jsonl, ring B current A retired",
        _head_nested,
    )
    out["verify origin_extra"] = (
        "the origin file {'seq': 1, 'mac': 'x', 'extra': <nested_text>}",
        _origin_nested,
    )
    out["verify rekey_extra"] = (
        "the rekey row's payload with 'x': <nested_text> added (signed)",
        _rekey_nested,
    )
    return out


def _probe(call: Callable[[str, int], Any], shape: str) -> dict[str, Any]:
    """The deepest ``depth`` the call takes and what it does one deeper,
    by bisection (the call is taken to succeed up to a limit)."""

    def ok(depth: int) -> bool:
        return "ok" in _attempt(call, shape, depth)

    lo, hi = 1, 1
    while ok(hi):
        lo, hi = hi, hi * 2
        if hi > 1 << 20:
            raise AssertionError("no limit found")
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    if lo < 9000:
        return _python_recursion(call, shape)
    result: dict[str, Any] = {"max_ok": lo, "first_failing": hi}
    value = _attempt(call, shape, lo).get("value")
    if isinstance(value, tuple):
        result["at_max"] = {"status": value[0], "head": value[1]}
    elif isinstance(value, (str, bytes)):
        data = (
            value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value
        )
        result["at_max"] = {
            "len": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    result["at_first_failing"] = _summary(_attempt(call, shape, hi))
    return result


def _summary(outcome: dict[str, Any]) -> dict[str, Any]:
    return {"error": outcome["error"]} if "error" in outcome else {"ok": True}


def _python_recursion(call: Callable[[str, int], Any], shape: str) -> dict[str, Any]:
    """A call site that recurses in Python stops at
    ``sys.getrecursionlimit()`` less the frames its caller already holds,
    so it has no depth of its own: recorded are a depth below the limit
    by more than any caller here holds, and the limit itself, which fails
    from any caller."""
    limit = sys.getrecursionlimit()
    below = limit - 100
    return {
        "python_recursion": {
            "recursionlimit": limit,
            "ok_at": below,
            "at_ok": _summary(_attempt(call, shape, below)),
            "fails_at": limit,
            "at_fails": _summary(_attempt(call, shape, limit)),
        }
    }


def _in_fresh_thread(work: Callable[[], Any]) -> Any:
    """``work()`` at the top of a new thread with a large stack: the
    recursion budget is then the version's, not what the caller left."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["v"] = work()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["e"] = exc

    old = threading.stack_size(256 * 1024 * 1024)
    try:
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
    finally:
        threading.stack_size(old)
    if "e" in box:
        raise box["e"]
    return box["v"]


def _json_edges() -> bytes:
    def work() -> list[Any]:
        out: list[Any] = []
        for name, (counts, call) in sites().items():
            for shape in ("list", "dict"):
                out.append(
                    {
                        "site": name,
                        "shape": shape,
                        "depth": counts,
                        **_probe(call, shape),
                    }
                )
        return out

    records: list[Any] = [
        {
            "fixture": "json_edges",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "nested": (
                "nested(list, n) = [[...[0]...]] (n lists); nested(dict, n) = "
                "{'a': {'a': ... 0}} (n dicts); nested_text is its compact JSON"
            ),
            "forms": "dumps <form>: the forms of canonical_json.jsonl",
            "sort": (
                "sort records: input (neutral, as canonical_json.jsonl) through "
                "every form, out as canonical_json.jsonl writes it"
            ),
            "result": (
                "depth records: max_ok: the deepest depth the call takes; at_max: the output's "
                "UTF-8 length and SHA-256 (dumps, repr), or the report's status "
                "and head status (verify); first_failing = max_ok + 1 and what "
                "the call does there. Each call runs at the top of a fresh "
                "thread."
            ),
        }
    ]
    for name, value in nan_key_values():
        records.append(
            {
                "sort": name,
                "input": neutral(value),
                "out": {
                    form: gen_json._apply(fn, value)
                    for form, (fn, _, _) in gen_json.FORMS.items()
                },
            }
        )
    records += _in_fresh_thread(work)
    return jsonl(records)


def generate() -> dict[str, bytes]:
    return {
        "datetime_edges.jsonl": _datetime_edges(),
        "json_edges.jsonl": _json_edges(),
    }
