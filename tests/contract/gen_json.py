"""Fixture 2: every JSON spelling the product persists, signs or hashes.

Each form is the exact `json.dumps` call (and any wrapping) of the named
call sites. Every input value is run through every form; the output is
the exact bytes (UTF-8 of the returned text, or the bytes the call site
encodes), or the exception type the form raises.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from bettermemory import native_text, review
from bettermemory.log import canonical_payload

from ._common import jsonl, neutral, output


def _review_json(value: Any) -> bytes:
    return review.canonical_json(value)


FORMS: dict[str, tuple[Callable[[Any], str | bytes], list[str], str]] = {
    "log_payload": (
        canonical_payload,
        ["log.canonical_payload (every log row's payload and MAC input)"],
        "sort_keys, separators=(',',':'), ensure_ascii=False, default=str, "
        "then str.encode('utf-8','backslashreplace').decode('utf-8')",
    ),
    "native_ascii": (
        native_text.encoded_json,
        ["native_text.encoded_json (native membership rows, origin extension)"],
        "sort_keys, ensure_ascii=True, separators=(',',':')",
    ),
    "column_text": (
        lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False),
        [
            "store_rows._json_text (record JSON columns)",
            "history._value_text (display, truncated after)",
        ],
        "sort_keys, ensure_ascii=False, default separators (', ', ': ')",
    ),
    "ascii_spaced": (
        lambda v: json.dumps(v, sort_keys=True, ensure_ascii=True),
        [
            "store_digest fingerprint text (hashed as ASCII)",
            "store_steps meta schema_steps",
            "restore_lock marker (+ '\\n')",
            "membership_records digest (hashed, first 24 hex)",
        ],
        "sort_keys, ensure_ascii=True, default separators",
    ),
    "ascii_compact_line": (
        lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")) + "\n",
        ["anchor.Anchor line", "audit_export line"],
        "sort_keys, ensure_ascii=True, separators=(',',':'), + '\\n'",
    ),
    "review_sha_bytes": (
        _review_json,
        ["review.canonical_json (review sha input, UTF-8 bytes)"],
        "sort_keys, separators=(',',':'), ensure_ascii=False, then .encode('utf-8')",
    ),
    "spelling_tables": (
        lambda v: json.dumps(
            v, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ),
        ["search_lexicon._spelling_tables"],
        "sort_keys, ensure_ascii=False, separators=(',',':')",
    ),
    "document_indent2": (
        lambda v: json.dumps(v, indent=2, sort_keys=True) + "\n",
        [
            "backup manifest",
            "mirror payload",
            "machine body",
            "key_lifecycle document",
            "restore_rows document",
        ],
        "indent=2, sort_keys, ensure_ascii=True, + '\\n'",
    ),
    "keyring_file": (
        lambda v: json.dumps(v),
        ["log.KeyRing head, origin and binding files", "backup head bytes"],
        "json.dumps defaults: insertion order, ensure_ascii=True, ', ' and ': '",
    ),
    "unsorted_compact_line": (
        lambda v: json.dumps(v, separators=(",", ":")) + "\n",
        ["rollback report line"],
        "insertion order, ensure_ascii=True, separators=(',',':'), + '\\n'",
    ),
}

_UTC = timezone.utc


def _floats() -> list[float]:
    return [
        0.0,
        -0.0,
        1.0,
        -1.0,
        2.0,
        100.0,
        0.1,
        0.2 + 0.1,
        1 / 3,
        2 / 3,
        1.5,
        123.456,
        1e15,
        1e16,
        1e17,
        123456789012345680.0,
        9007199254740993.0,
        1e21,
        1e22,
        1e300,
        -1e300,
        1.7976931348623157e308,
        1e-4,
        1e-5,
        1e-7,
        0.000123,
        2.2250738585072014e-308,
        5e-324,
        float("inf"),
        float("-inf"),
        math.nan,
    ]


def values() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = [
        ("empty_dict", {}),
        ("empty_list", []),
        ("null", None),
        ("bools", {"t": True, "f": False}),
        (
            "ints",
            {"zero": 0, "neg": -7, "big": 2**64, "i63": -(2**63), "i53": 2**53 + 1},
        ),
        (
            "key_order_mixed",
            {
                "b": 1,
                "a": 2,
                "B": 3,
                "_": 4,
                "aa": 5,
                "a\u0000": 6,
                "": 7,
                "10": 8,
                "9": 9,
            },
        ),
        (
            "key_order_unicode",
            {
                "￿": 1,
                "\U0001f600": 2,
                "": 3,
                "�": 4,
                "é": 5,
                "é": 6,
                "z": 7,
                "Ω": 8,
                "\U00010000": 9,
                "ÿ": 10,
            },
        ),
        ("int_keys", {10: "a", 2: "b", 1: "c"}),
        ("float_key", {1.5: "x"}),
        ("bool_none_keys_alone", {None: 1}),
        (
            "strings",
            {
                "ascii": "plain",
                "quote": 'a "b" \\c',
                "controls": "\x00\x01\x08\t\n\x0c\r\x1f\x7f",
                "latin": "Zürich",
                "cjk": "東京",
                "astral": "\U0001f600\U00020000",
                "bmp_edge": "￾￿",
                "seps": "  ",
                "slash": "a/b</script>",
                "nbsp": " ",
            },
        ),
        ("surrogate_value", {"name": "file\udcff"}),
        ("surrogate_key", {"\ud800": 1}),
        ("surrogate_pair_halves", ["😀"]),
        ("nested", {"a": [1, {"c": [], "b": {}}, [[]]], "z": {"y": {"x": [None]}}}),
        ("tuple", {"t": (1, "two", 3.0)}),
        (
            "datetimes",
            {
                "utc": datetime(2026, 11, 15, 8, 9, 10, tzinfo=_UTC),
                "utc_micro": datetime(2026, 11, 15, 8, 9, 10, 123456, tzinfo=_UTC),
                "offset": datetime(
                    2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2))
                ),
                "naive": datetime(2026, 1, 2, 3, 4, 5),
            },
        ),
        (
            "paths",
            {
                "rel": PurePosixPath("a/b.txt"),
                "abs": PurePosixPath("/tmp/x y/é.md"),
            },
        ),
    ]
    for f in _floats():
        out.append((f"float {f!r}", {"f": f}))
    out.append(("float_list", _floats()))
    out.append(("float_int_mix", [1, 1.0, -0, -0.0]))
    out.extend(more_values())
    return out


#: Exact ties of `float.__repr__` (17 significant digits, the exact value
#: one digit longer and ending in 5): CPython rounds them half to even. A
#: tie rounded down (the last kept digit even) and one rounded up (odd,
#: to the even digit above). Only values in fixed notation have them: a
#: double of 17 or more integer digits is even and ends in no 5, and one
#: below 1e-4 has a much longer exact expansion.
_TIES_DOWN = [
    254209913874244.625,
    17200606963.3828125,
    137480429030.765625,
    137646331265.828125,
    1099594578893.53125,
    1099926383363.65625,
    8796093041844.6875,
    17592186064052.6875,
    70368744216937.375,
]
_TIES_UP = [
    137563380148.296875,
    1099760481128.59375,
    1100092285598.71875,
    1100424090068.84375,
    1100755894538.96875,
    1099511637594.34375,
    2199023265370.34375,
    8796093068026.9375,
    17592186090234.9375,
]


def _edge_floats() -> list[float]:
    """Around the switch between fixed and exponent notation (exponent
    below -4 or from 16 on), the subnormal range and the largest values."""
    out = [
        9999999999999998.0,
        1e16,
        1.0000000000000002e16,
        9007199254740992.0,
        9007199254740994.0,
        18446744073709551616.0,
        9223372036854775808.0,
        123456789012345678.0,
        1e22,
        1e23,
        9.999999999999999e22,
        0.0001,
        math.nextafter(0.0001, 0.0),
        math.nextafter(0.0001, 1.0),
        0.00009999999999999999,
        0.001,
        0.1 + 0.7,
        2.2250738585072009e-308,
        2.225073858507201e-308,
        2.2250738585072014e-308,
        math.nextafter(2.2250738585072014e-308, 1.0),
        1e-323,
        1.5e-323,
        2e-323,
        2.5e-323,
        4.9e-322,
        math.nextafter(1.7976931348623157e308, 0.0),
        -5e-324,
        -9999999999999998.0,
        1e15 + 0.3,
        999999999999999.9,
        0.30000000000000004,
    ]
    return out


def _chunks(name: str, values: list[float], size: int = 40) -> list[tuple[str, Any]]:
    return [
        (f"{name} {i // size}", values[i : i + size])
        for i in range(0, len(values), size)
    ]


def _float_lists() -> list[tuple[str, Any]]:
    twos = sorted(
        {math.ldexp(1.0, e) for e in range(-1074, 1024, 11)}
        | {
            math.ldexp(1.0, e)
            for e in (-1074, -1073, -1023, -1022, -1, 0, 52, 53, 54, 63, 64, 1023)
        }
    )
    tens = [float(f"1e{e}") for e in range(-323, 309, 3)] + [
        float(f"1e{e}") for e in range(-7, 24)
    ]
    subnormals = [
        k * 5e-324 for k in (1, 2, 3, 7, 10, 99, 1000, 2**20, 2**51 - 1, 2**52 - 1)
    ]
    big_ints = [
        float(n)
        for n in (
            2**53 - 1,
            2**53 + 1,
            2**60 + 3,
            10**17 + 1,
            10**18,
            10**21,
            10**21 - 1,
            2**63 - 1,
            2**64 - 1,
            10**308,
        )
    ]
    out: list[tuple[str, Any]] = []
    out.extend(_chunks("floats powers_of_two", twos))
    out.extend(_chunks("floats powers_of_ten", tens))
    out.append(("floats subnormal_multiples", subnormals))
    out.append(("floats large_integers", big_ints))
    return out


def _mixed_keys() -> list[tuple[str, Any]]:
    """`sort_keys` sorts the items as Python compares their keys: a str
    with any non-str raises TypeError, numbers sort numerically and
    exactly (an int against a float, past i128 too), bool among them as 0
    and 1. Where a NaN key lands depends on the Python version (3.13
    changed `list.sort`'s runs); those cases are in ``json_edges.jsonl``."""
    return [
        ("keys str_and_int", {"a": 1, 1: 2}),
        ("keys int_then_str_equal_text", {1: "a", "1": "b"}),
        ("keys bool_and_str", {True: 1, "a": 2}),
        ("keys none_and_int", {None: 1, 0: 2}),
        ("keys none_and_str", {"a": 1, None: 2}),
        ("keys tuple", {(1, 2): 3}),
        ("keys bool_only", {True: 1, False: 2}),
        (
            "keys numbers",
            {2: "a", 0.5: "b", True: "c", -1: "d", 1.5: "e", False: "f", -0.25: "g"},
        ),
        ("keys int_beyond_float", {2**53 + 1: "a", 9007199254740992.0: "b"}),
        ("keys big_ints", {2**64: "a", -(2**64): "b", 1e20: "c", 10**30: "d"}),
        ("keys infinities", {float("inf"): 1, float("-inf"): 2, 0: 3}),
        (
            "keys int_beyond_i128",
            {1e40: "a", 10**40: "b", -1e300: "c", -(10**300): "d", 2.0**127: "e"},
        ),
        ("keys int_and_float_past_i128", {2**130: "a", -1.5: "b", 2.0**129: "c"}),
        ("keys nested", {"z": [{2: "a", 1: "b"}], "a": {1.5: {True: None}}}),
    ]


def more_values() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for f in _TIES_DOWN:
        out.append((f"float tie_down {f!r}", {"f": f}))
    for f in _TIES_UP:
        out.append((f"float tie_up {f!r}", {"f": f}))
    for f in _edge_floats():
        out.append((f"float edge {f!r}", {"f": f}))
    out.extend(_float_lists())
    out.extend(_mixed_keys())
    return out


def _apply(fn: Callable[[Any], str | bytes], value: Any) -> dict[str, Any]:
    try:
        result = fn(value)
    except Exception as exc:  # noqa: BLE001 - the error type is the fixture
        return {"error": type(exc).__name__}
    return output(result)


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "canonical_json",
            "forms": {
                name: {"sites": sites, "spec": spec}
                for name, (_, sites, spec) in FORMS.items()
            },
            "neutral": (
                "t=null|bool|int(v decimal)|float(bits IEEE-754 hex, repr)|"
                "str(v, or v_cps when it holds a surrogate)|datetime(iso; "
                "default=str spells str(dt))|path(v; default=str spells it)|"
                "tuple|list|dict(v=[[key,value],...] in insertion order)"
            ),
        }
    ]
    for name, value in values():
        records.append(
            {
                "name": name,
                "input": neutral(value),
                "out": {form: _apply(fn, value) for form, (fn, _, _) in FORMS.items()},
            }
        )
    return {"canonical_json.jsonl": jsonl(records)}
