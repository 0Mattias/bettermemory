"""Fixture 3b: what `log.verify_chain` reads besides the rows.

Each case is a small chain, built row by row with `log.compute_mac` so a
payload text can be any text a row may hold (a ``rekey`` payload that is
not the canonical spelling), and a key ring given as the files a keys
directory holds. The report is the one `verify_chain` gives, or the
exception type it raises. The cases pin:

- the head file `KeyRing.read_head` reads (``json.loads``, then
  ``int(raw["seq"])`` and ``str(raw["mac"])``; OSError, ValueError,
  KeyError and TypeError read as no head, an OverflowError raises);
- the origin anchor `KeyRing.read_origin` / `parse_origin` reads, and the
  check of it against the log (`_check_origin`, `_anchored`);
- the ring's keys: a missing key, a retired file whose content is not
  the key its name gives, the leading-key fallback (`_leading_key`);
- a ``rekey`` payload as `_rekey_fingerprints` reads it (``json.loads``:
  NaN and Infinity literals, escapes, duplicate keys, huge integers,
  wrong types, extra keys);
- names that are not fingerprints (`is_fingerprint`), which name no key;
- seqs at the ends of i64 (`iter_rows` reads seq > 0 only).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bettermemory import log as blog
from bettermemory.store_schema import SCHEMA

from ._common import has_surrogate, jsonl, text_field

KEY_A = bytes(range(32))
KEY_B = bytes(range(32, 64))
KEY_C = bytes(range(64, 96))
STORE_ID = "0123456789abcdef0123456789abcdef"
TS = "2026-01-02T03:04:05.000Z"
FP_A = blog.fingerprint(KEY_A)
FP_B = blog.fingerprint(KEY_B)
FP_C = blog.fingerprint(KEY_C)
_PREFIX = 16
I64_MAX = 2**63 - 1
I64_MIN = -(2**63)

Spec = tuple[bytes, str, str]  # key, kind, payload text


def _payload(value: dict[str, Any]) -> str:
    return blog.canonical_payload(value)


def rekey_text(old: str, new: str) -> str:
    return _payload({"old_fingerprint": old, "new_fingerprint": new})


def chain(specs: list[Spec], seqs: list[int] | None = None) -> list[blog.LogRow]:
    """Rows signed and linked as `append_row` links them, at ``seqs``
    (1, 2, ... by default)."""
    rows: list[blog.LogRow] = []
    numbers = seqs if seqs is not None else list(range(1, len(specs) + 1))
    for seq, (key, kind, text) in zip(numbers, specs, strict=True):
        prev = blog.GENESIS_MAC if not rows else rows[-1].mac
        mac = blog.compute_mac(
            key, seq=seq, ts=TS, session=None, kind=kind, payload=text, prev_mac=prev
        )
        rows.append(blog.LogRow(seq, TS, None, kind, text, prev, mac))
    return rows


def _note(key: bytes, n: int) -> Spec:
    return (key, "note", _payload({"n": n}))


def two_key(rekey: str | None = None) -> list[blog.LogRow]:
    """A signs rows 1-3, a ``rekey`` row (signed by B) at 4, B signs 5-6."""
    return chain(
        [
            (KEY_A, blog.STORE_CREATED, _payload({})),
            _note(KEY_A, 1),
            _note(KEY_A, 2),
            (KEY_B, blog.REKEY, rekey if rekey is not None else rekey_text(FP_A, FP_B)),
            _note(KEY_B, 3),
            _note(KEY_B, 4),
        ]
    )


def head_text(row: blog.LogRow) -> bytes:
    return json.dumps({"seq": row.seq, "mac": row.mac, "ts": ""}).encode("utf-8")


def ring(
    current: bytes | None = KEY_B,
    retired: list[tuple[str, bytes]] | None = None,
    head: bytes | None = None,
    origin: bytes | None = None,
) -> dict[str, Any]:
    """A keys directory: ``current`` as ``<store_id>.key``, each retired
    ``(name, key)`` as ``<store_id>.<name>.key``, the head and origin
    files' bytes."""
    if retired is None:
        retired = [(FP_A[:_PREFIX], KEY_A)]
    return {
        "current": current,
        "retired": retired,
        "head": head,
        "origin": origin,
    }


def _verify(
    rows: list[blog.LogRow], keys: dict[str, Any], current_fingerprint: str
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        kr = blog.KeyRing(Path(tmp), STORE_ID)
        if keys["current"] is not None:
            kr.current_path().write_bytes(keys["current"])
        for name, key in keys["retired"]:
            (Path(tmp) / f"{STORE_ID}.{name}.key").write_bytes(key)
        if keys["head"] is not None:
            kr.head_path().write_bytes(keys["head"])
        if keys["origin"] is not None:
            kr.origin_path().write_bytes(keys["origin"])
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        try:
            report = blog.verify_chain(
                conn, keyring=kr, current_fingerprint=current_fingerprint
            )
        except Exception as exc:  # noqa: BLE001 - the error type is the fixture
            return {"error": type(exc).__name__}
        finally:
            conn.close()
    return {"report": encode(report.to_dict())}


def encode(value: Any) -> Any:
    """A report as JSON every reader takes: an int outside i64 as
    ``{"int": decimal}``, a str holding a lone surrogate as
    ``{"cps": [...]}``."""
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if I64_MIN <= value <= I64_MAX else {"int": str(value)}
    if isinstance(value, str):
        return {"cps": [ord(c) for c in value]} if has_surrogate(value) else value
    raise TypeError(type(value).__name__)


def _row_record(row: blog.LogRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "ts": row.ts,
        "session": row.session,
        "kind": row.kind,
        **text_field("payload", row.payload),
        "prev_mac": row.prev_mac,
        "mac": row.mac,
    }


Case = tuple[str, str, list[blog.LogRow], dict[str, Any], str]


def _head_cases() -> list[Case]:
    rows = two_key()
    last, first = rows[-1], rows[0]

    def raw(text: str, note: str) -> Case:
        return (note, "head", rows, ring(head=text.encode("utf-8")), FP_B)

    def obj(name: str, seq: str, mac: str | None = None) -> Case:
        m = json.dumps(last.mac) if mac is None else mac
        return (
            name,
            "head",
            rows,
            ring(head=f'{{"seq": {seq}, "mac": {m}}}'.encode()),
            FP_B,
        )

    big = "9" * 4300
    out: list[Case] = [
        ("head_intact", "head", rows, ring(head=head_text(last)), FP_B),
        ("head_absent", "head", rows, ring(), FP_B),
        obj("head_seq_string", '"6"'),
        obj("head_seq_string_spaces", '" 6\\n"'),
        obj("head_seq_string_underscore", '"0_6"'),
        obj("head_seq_string_plus", '"+6"'),
        obj("head_seq_string_arabic_digit", '"\\u0666"'),
        obj("head_seq_string_fullwidth_digit", '"\\uff16"'),
        obj("head_seq_string_float", '"6.0"'),
        obj("head_seq_string_hex", '"0x6"'),
        obj("head_seq_string_empty", '""'),
        obj("head_seq_string_surrogate", '"\\ud800"'),
        obj("head_seq_float", "6.0"),
        obj("head_seq_float_truncated", "6.9"),
        obj("head_seq_float_negative_half", "-0.5"),
        obj("head_seq_float_exponent", "6e0"),
        obj("head_seq_float_huge", "1e20"),
        obj("head_seq_float_overflow_literal", "1e400"),
        obj("head_seq_nan", "NaN"),
        obj("head_seq_infinity", "Infinity"),
        obj("head_seq_negative_infinity", "-Infinity"),
        obj("head_seq_true", "true", json.dumps(first.mac)),
        obj("head_seq_false", "false"),
        obj("head_seq_null", "null"),
        obj("head_seq_list", "[6]"),
        obj("head_seq_object", '{"seq": 6}'),
        obj("head_seq_negative", "-1"),
        obj("head_seq_zero", "0"),
        obj("head_seq_i64_max", str(I64_MAX)),
        obj("head_seq_past_i64", str(2**63)),
        obj("head_seq_below_i64", str(-(2**63) - 1)),
        obj("head_seq_2_pow_70", str(2**70)),
        obj("head_seq_4300_digits", big),
        obj("head_seq_4301_digits", big + "9"),
        obj("head_seq_stale", "2", json.dumps(rows[1].mac)),
        obj("head_seq_stale_wrong_mac", "2", json.dumps(last.mac)),
        obj("head_mac_int", "6", "5"),
        obj("head_mac_float", "6", "1.5"),
        obj("head_mac_float_exponent", "6", "1e400"),
        obj("head_mac_nan", "6", "NaN"),
        obj("head_mac_null", "6", "null"),
        obj("head_mac_true", "6", "true"),
        obj("head_mac_list", "6", '[1, "a", null, 2.0]'),
        obj("head_mac_object", "6", '{"b": [true], "a": "x"}'),
        obj("head_mac_non_ascii", "6", '"\\u00e9\\ud83d\\ude00"'),
        obj("head_mac_lone_surrogate", "6", '"\\udcff"'),
        obj("head_mac_uppercase", "6", json.dumps(last.mac.upper())),
        obj("head_mac_carriage_return", "6", '"a\\rb"'),
        raw('{"seq": 1, "seq": 6, "mac": "' + last.mac + '"}', "head_duplicate_seq"),
        raw('{"mac": "' + last.mac + '"}', "head_seq_missing"),
        raw('{"seq": 6}', "head_mac_missing"),
        raw(
            '{"seq": 6, "mac": "' + last.mac + '", "ts": 5, "x": NaN}',
            "head_extra_keys",
        ),
        raw("{", "head_not_json"),
        raw("", "head_empty_file"),
        raw("[]", "head_array"),
        raw('"head"', "head_string"),
        raw("6", "head_number"),
        raw("null", "head_null"),
        raw('\r\n{"seq":\r6,\r\n"mac": "' + last.mac + '"}\r\n', "head_crlf"),
        raw('\ufeff{"seq": 6, "mac": "' + last.mac + '"}', "head_bom"),
        raw('{"seq": 6, "mac": "' + last.mac + '"} x', "head_trailing_text"),
    ]
    out.append(
        (
            "head_invalid_utf8",
            "head",
            rows,
            ring(head=b'{"seq": 6, "mac": "\xff"}'),
            FP_B,
        )
    )
    out.append(
        (
            "head_on_empty_log",
            "head",
            [],
            ring(head=head_text(last)),
            FP_B,
        )
    )
    return out


def _origin_doc(row: blog.LogRow, named: str | None) -> bytes:
    doc: dict[str, Any] = {"seq": row.seq, "mac": row.mac}
    if named is not None:
        doc["key_fingerprint"] = named
    return json.dumps(doc).encode("utf-8")


def _origin_cases() -> list[Case]:
    rows = two_key()
    head = head_text(rows[-1])

    def at(name: str, origin: bytes, keys: dict[str, Any] | None = None) -> Case:
        base = keys if keys is not None else ring(head=head)
        return (name, "origin", rows, {**base, "origin": origin}, FP_B)

    def raw(name: str, doc: str) -> Case:
        return at(name, doc.encode("utf-8"))

    mac1 = rows[0].mac
    rewritten = list(rows)
    rewritten[1] = rewritten[1]._replace(payload=_payload({"n": 99}))
    from_c = chain(
        [
            (KEY_C, blog.STORE_CREATED, _payload({})),
            _note(KEY_C, 1),
            (KEY_B, blog.REKEY, rekey_text(FP_C, FP_B)),
            _note(KEY_B, 2),
        ]
    )
    to_c = chain(
        [
            (KEY_A, blog.STORE_CREATED, _payload({})),
            _note(KEY_A, 1),
            (KEY_C, blog.REKEY, rekey_text(FP_A, FP_C)),
            _note(KEY_C, 2),
        ]
    )
    only_b = ring(current=KEY_B, retired=[], head=head_text(from_c[-1]))
    only_a = ring(current=KEY_A, retired=[], head=head_text(to_c[-1]))
    return [
        at("origin_ok_named", _origin_doc(rows[0], FP_A)),
        at("origin_ok_unnamed", _origin_doc(rows[0], None)),
        at("origin_ok_at_rekey", _origin_doc(rows[3], FP_B)),
        at("origin_ok_named_other_key", _origin_doc(rows[0], FP_C)),
        at("origin_mac_mismatch", json.dumps({"seq": 1, "mac": rows[1].mac}).encode()),
        at("origin_row_missing", json.dumps({"seq": 99, "mac": mac1}).encode()),
        (
            "origin_rewritten_named_held",
            "origin",
            rewritten,
            {**ring(head=head), "origin": _origin_doc(rows[1], FP_A)},
            FP_B,
        ),
        (
            "origin_rewritten_unnamed",
            "origin",
            rewritten,
            {**ring(head=head), "origin": _origin_doc(rows[1], None)},
            FP_B,
        ),
        (
            "origin_rewritten_named_unheld",
            "origin",
            rewritten,
            {**ring(head=head), "origin": _origin_doc(rows[1], FP_C)},
            FP_B,
        ),
        at(
            "origin_ring_empty",
            _origin_doc(rows[0], FP_A),
            ring(current=None, retired=[], head=head),
        ),
        (
            "origin_names_unheld_signer",
            "origin",
            from_c,
            {**only_b, "origin": _origin_doc(from_c[0], FP_C)},
            FP_B,
        ),
        (
            "origin_unnamed_unheld_signer",
            "origin",
            from_c,
            {**only_b, "origin": _origin_doc(from_c[0], None)},
            FP_B,
        ),
        ("copied_store_no_origin", "origin", from_c, only_b, FP_B),
        (
            "copied_store_origin_at_rekey",
            "origin",
            from_c,
            {**only_b, "origin": _origin_doc(from_c[2], FP_B)},
            FP_B,
        ),
        (
            "copied_store_origin_after_rekey",
            "origin",
            from_c,
            {**only_b, "origin": _origin_doc(from_c[3], FP_B)},
            FP_B,
        ),
        ("foreign_tail_no_origin", "origin", to_c, only_a, FP_A),
        (
            "foreign_tail_origin_at_1",
            "origin",
            to_c,
            {**only_a, "origin": _origin_doc(to_c[0], FP_A)},
            FP_A,
        ),
        (
            "foreign_tail_origin_mismatch",
            "origin",
            to_c,
            {**only_a, "origin": json.dumps({"seq": 1, "mac": "0" * 64}).encode()},
            FP_A,
        ),
        raw("origin_seq_true", json.dumps({"seq": True, "mac": mac1})),
        raw("origin_seq_zero", json.dumps({"seq": 0, "mac": mac1})),
        raw("origin_seq_negative", json.dumps({"seq": -1, "mac": mac1})),
        raw("origin_seq_past_i64", json.dumps({"seq": 2**63, "mac": mac1})),
        raw("origin_seq_i64_max", json.dumps({"seq": I64_MAX, "mac": mac1})),
        raw("origin_seq_string", json.dumps({"seq": "1", "mac": mac1})),
        raw("origin_seq_float", json.dumps({"seq": 1.0, "mac": mac1})),
        raw("origin_seq_missing", json.dumps({"mac": mac1})),
        raw("origin_mac_uppercase", json.dumps({"seq": 1, "mac": mac1.upper()})),
        raw("origin_mac_short", json.dumps({"seq": 1, "mac": mac1[:63]})),
        raw("origin_mac_newline", json.dumps({"seq": 1, "mac": mac1 + "\n"})),
        raw("origin_mac_int", json.dumps({"seq": 1, "mac": 5})),
        raw(
            "origin_fingerprint_uppercase",
            json.dumps({"seq": 2, "mac": rows[1].mac, "key_fingerprint": FP_A.upper()}),
        ),
        raw(
            "origin_fingerprint_int",
            json.dumps({"seq": 1, "mac": mac1, "key_fingerprint": 7}),
        ),
        raw("origin_duplicate_seq", '{"seq": 99, "seq": 1, "mac": "' + mac1 + '"}'),
        raw("origin_array", json.dumps([1, mac1])),
        raw("origin_not_json", "{seq: 1}"),
        raw("origin_empty_file", ""),
        raw("origin_nan_seq", '{"seq": NaN, "mac": "' + mac1 + '"}'),
        at("origin_invalid_utf8", b'{"seq": 1, "mac": "\xff"}'),
    ]


def _ring_cases() -> list[Case]:
    rows = two_key()
    head = head_text(rows[-1])
    from_c_named_a = chain(
        [
            (KEY_C, blog.STORE_CREATED, _payload({})),
            _note(KEY_C, 1),
            (KEY_B, blog.REKEY, rekey_text(FP_A, FP_B)),
            _note(KEY_B, 2),
        ]
    )
    head_c = head_text(from_c_named_a[-1])
    return [
        ("ring_intact", "ring", rows, ring(head=head), FP_B),
        ("ring_lacks_leading_key", "ring", rows, ring(retired=[], head=head), FP_B),
        (
            "ring_lacks_trailing_key",
            "ring",
            rows,
            ring(current=KEY_A, retired=[], head=head),
            FP_B,
        ),
        ("ring_empty", "ring", rows, ring(current=None, retired=[], head=head), FP_B),
        (
            "ring_retired_only",
            "ring",
            rows,
            ring(
                current=None,
                retired=[(FP_A[:_PREFIX], KEY_A), (FP_B[:_PREFIX], KEY_B)],
                head=head,
            ),
            FP_B,
        ),
        (
            "retired_file_holds_another_key",
            "ring",
            rows,
            ring(retired=[(FP_A[:_PREFIX], KEY_C)], head=head),
            FP_B,
        ),
        (
            "retired_file_under_odd_name",
            "ring",
            rows,
            ring(retired=[("old", KEY_A)], head=head),
            FP_B,
        ),
        (
            "leading_key_fallback",
            "ring",
            from_c_named_a,
            ring(retired=[(FP_A[:_PREFIX], KEY_C)], head=head_c),
            FP_B,
        ),
        (
            "leading_key_fallback_none_verifies",
            "ring",
            from_c_named_a,
            ring(retired=[(FP_A[:_PREFIX], KEY_A)], head=head_c),
            FP_B,
        ),
        (
            "current_fingerprint_names_retired",
            "ring",
            rows,
            ring(head=head),
            FP_A,
        ),
        (
            "current_fingerprint_not_a_fingerprint",
            "ring",
            rows,
            ring(head=head),
            FP_B.upper(),
        ),
        (
            "no_rekey_named_by_current_fingerprint",
            "ring",
            chain([(KEY_C, blog.STORE_CREATED, _payload({})), _note(KEY_C, 1)]),
            ring(current=KEY_C, retired=[]),
            FP_A,
        ),
    ]


def _rekey_cases() -> list[Case]:
    """``rekey`` payload texts as `_rekey_fingerprints` reads them, each
    in the two-key chain (signed, so only the reading differs)."""
    a, b = json.dumps(FP_A), json.dumps(FP_B)
    big = "1" * 4300

    def text(name: str, payload: str, current: str = FP_B) -> Case:
        rows = two_key(payload)
        return (name, "rekey_payload", rows, ring(head=head_text(rows[-1])), current)

    def pair(old: str, new: str) -> str:
        return '{"old_fingerprint":' + old + ',"new_fingerprint":' + new + "}"

    escaped_b = '"' + "".join(f"\\u{ord(c):04x}" for c in FP_B) + '"'
    return [
        text("rekey_canonical", pair(a, b)),
        text(
            "rekey_spaced",
            '{ "new_fingerprint" : ' + b + ' , "old_fingerprint" : ' + a + " }\n",
        ),
        text("rekey_escaped_name", pair(a, escaped_b)),
        text("rekey_extra_keys", pair(a, b)[:-1] + ',"x":[1,{"y":null}]}'),
        text("rekey_extra_nan", pair(a, b)[:-1] + ',"x":NaN}'),
        text("rekey_extra_infinity", pair(a, b)[:-1] + ',"x":-Infinity}'),
        text("rekey_extra_overflow_float", pair(a, b)[:-1] + ',"x":1e400}'),
        text("rekey_extra_4300_digits", pair(a, b)[:-1] + ',"x":' + big + "}"),
        text("rekey_extra_4301_digits", pair(a, b)[:-1] + ',"x":' + big + "1}"),
        text("rekey_extra_lone_surrogate", pair(a, b)[:-1] + ',"x":"\\ud800"}'),
        text("rekey_old_nan", pair("NaN", b)),
        text("rekey_new_infinity", pair(a, "Infinity")),
        text("rekey_new_int", pair(a, "7")),
        text("rekey_new_null", pair(a, "null")),
        text("rekey_new_list", pair(a, "[" + b + "]")),
        text("rekey_new_true", pair(a, "true")),
        text("rekey_new_lone_surrogate", pair(a, '"\\udc80' + FP_B[1:] + '"')),
        text("rekey_new_missing", '{"old_fingerprint":' + a + "}"),
        text("rekey_old_missing", '{"new_fingerprint":' + b + "}"),
        text(
            "rekey_duplicate_new",
            '{"old_fingerprint":'
            + a
            + ',"new_fingerprint":'
            + json.dumps(FP_C)
            + ',"new_fingerprint":'
            + b
            + "}",
        ),
        text(
            "rekey_duplicate_new_last_wrong",
            '{"old_fingerprint":'
            + a
            + ',"new_fingerprint":'
            + b
            + ',"new_fingerprint":'
            + json.dumps(FP_C)
            + "}",
        ),
        text("rekey_array", "[" + a + "," + b + "]"),
        text("rekey_string", json.dumps(FP_B)),
        text("rekey_null", "null"),
        text("rekey_number", "4"),
        text("rekey_empty_object", "{}"),
        text("rekey_empty_text", ""),
        text("rekey_not_json", "{old_fingerprint: 1}"),
        text("rekey_trailing_comma", pair(a, b)[:-1] + ",}"),
        text("rekey_bom", "\ufeff" + pair(a, b)),
        text("rekey_raw_control_char", pair(a, '"' + FP_B[:5] + "\t" + FP_B[5:] + '"')),
        text("rekey_nan_lowercase", pair(a, b)[:-1] + ',"x":nan}'),
        text("rekey_trailing_text", pair(a, b) + " x"),
    ]


def _name_cases() -> list[Case]:
    """`rekey` rows whose names are not 64 lowercase hex digits."""

    def named(name: str, old: str, new: str, current: str = FP_B) -> Case:
        rows = two_key(rekey_text(old, new))
        return (name, "key_name", rows, ring(head=head_text(rows[-1])), current)

    return [
        named("new_uppercase", FP_A, FP_B.upper(), FP_B.upper()),
        named("new_uppercase_current_lower", FP_A, FP_B.upper()),
        named("new_63_chars", FP_A, FP_B[:63]),
        named("new_65_chars", FP_A, FP_B + "0"),
        named("new_trailing_newline", FP_A, FP_B + "\n"),
        named("new_prefix_only", FP_A, FP_B[:_PREFIX]),
        named("new_path_traversal", FP_A, "../../../../etc/passwd"),
        named("new_dotdot_prefix", FP_A, ".." + FP_B[2:]),
        named("new_empty", FP_A, ""),
        named("new_arabic_digit", FP_A, "\u0660" + FP_B[1:]),
        named("new_fullwidth", FP_A, "\uff10" + FP_B[1:]),
        named("new_space_padded", FP_A, " " + FP_B[1:]),
        named("old_uppercase", FP_A.upper(), FP_B),
        named("old_empty", "", FP_B),
        named("old_path_traversal", "../" + FP_A[3:], FP_B),
        named("old_names_c", FP_C, FP_B),
    ]


def _seq_cases() -> list[Case]:
    """Seqs at the ends of i64, signed by A; the ring holds A."""

    def seqs(name: str, numbers: list[int], head: bytes | None = None) -> Case:
        rows = chain([_note(KEY_A, n) for n in numbers], numbers)
        keys = ring(current=KEY_A, retired=[], head=head)
        return (name, "seq_bounds", rows, keys, FP_A)

    max_rows = chain([_note(KEY_A, n) for n in (1, I64_MAX)], [1, I64_MAX])
    return [
        seqs("jump_to_max", [1, I64_MAX - 1, I64_MAX]),
        seqs("one_then_max", [1, I64_MAX]),
        seqs("max_only", [I64_MAX]),
        seqs("min_upward", [I64_MIN, I64_MIN + 1, -1, 0, 1]),
        seqs("min_only", [I64_MIN]),
        seqs("near_max_run", [I64_MAX - 2, I64_MAX - 1, I64_MAX]),
        seqs("zero_only", [0]),
        seqs("negative_then_two", [-5, 2, 3]),
        seqs("head_at_max", [1, I64_MAX], head_text(max_rows[-1])),
        seqs(
            "head_past_max",
            [1, I64_MAX],
            json.dumps({"seq": 2**63, "mac": max_rows[-1].mac}).encode(),
        ),
        seqs("empty_log_no_head", []),
        seqs(
            "empty_log_head_at_min",
            [],
            json.dumps({"seq": I64_MIN, "mac": "0" * 64}).encode(),
        ),
    ]


def cases() -> list[Case]:
    groups: list[Callable[[], list[Case]]] = [
        _head_cases,
        _origin_cases,
        _ring_cases,
        _rekey_cases,
        _name_cases,
        _seq_cases,
    ]
    return [case for group in groups for case in group()]


def _hex(data: bytes | None) -> str | None:
    return None if data is None else data.hex()


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "log_keyring",
            "keys": {
                "A": KEY_A.hex(),
                "B": KEY_B.hex(),
                "C": KEY_C.hex(),
                "A_fingerprint": FP_A,
                "B_fingerprint": FP_B,
                "C_fingerprint": FP_C,
            },
            "store_id": STORE_ID,
            "rows": (
                "the rows the log table holds, in seq order; verify_chain reads "
                "those with seq > 0 (iter_rows)"
            ),
            "ring": (
                "the keys directory: current -> <store_id>.key, each retired "
                "[name, key] -> <store_id>.<name>.key, head -> <store_id>.head, "
                "origin -> <store_id>.origin; each a hex of the file's bytes, "
                "null when there is no such file"
            ),
            "result": (
                "report: verify_chain(conn, keyring, current_fingerprint)."
                "to_dict(), with an int outside i64 as {'int': decimal} and a "
                "str holding a lone surrogate as {'cps': [...]}; or error: the "
                "exception type verify_chain raised"
            ),
            "accidents": [
                "read_head reads seq with int(): a string, a float (truncated), "
                "a bool and a non-ASCII digit string all read as a seq "
                "(log.py:629-636); an infinite float raises OverflowError out "
                "of verify_chain, which read_head does not catch",
                "read_head reads mac with str(): any JSON value is a mac, "
                "spelled as Python's str() spells it (log.py:633)",
                "a rekey payload whose extra key holds an integer of more than "
                "4300 digits fails json.loads (CPython's int digit limit), so "
                "the row reads as naming no key (payload_invalid) though its "
                "fingerprints are well formed (log.py:941)",
            ],
        }
    ]
    names: set[str] = set()
    for name, group, rows, keys, current in cases():
        assert name not in names, name
        names.add(name)
        records.append(
            {
                "case": name,
                "group": group,
                "rows": [_row_record(r) for r in rows],
                "ring": {
                    "current": _hex(keys["current"]),
                    "retired": [[n, k.hex()] for n, k in keys["retired"]],
                    "head": _hex(keys["head"]),
                    "origin": _hex(keys["origin"]),
                },
                "current_fingerprint": current,
                **_verify(rows, keys, current),
            }
        )
    return {"log_keyring.jsonl": jsonl(records)}
