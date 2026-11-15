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
