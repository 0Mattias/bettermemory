"""Fixtures: `events`' query redaction and named event fields,
`store_rows.event_import_payload` over them, and `time_utils`'
`isoformat_utc_optional` and `ensure_utc` (``event_redact.jsonl``, not
gated).

The native build reads them in
``native/crates/bettermemory-core/src/event_redact.rs``,
``src/store/rows.rs`` and ``src/time_utils.rs``.

`credentials.secret_spans` is not ported: the native functions take it
as an argument. Each row that redacts records ``sp``, every call the
reference makes to it, as ``[text, spans]`` (the text the five patterns
left, as `_common.text_field` spells it under ``t``, and the spans as
``[start, end, kind]``). The generator computes them by running the
module's own `_SECRET_PATTERNS` and calling the real `secret_spans`; no
module global is patched.

Rows (``k``):

- ``rq``: `redact_query(t)`; ``o`` the result (`neutral`).
- ``nf``: `named_event_fields(f)`; ``o`` the result.
- ``rf``: `_redact_event_fields(f)`; ``o`` the result.
- ``imp``: `event_import_payload(f, imported_from=from)` (the default
  when ``from`` is absent); ``o`` ``[ts, session, kind, payload]`` or
  ``{"err": text}`` of the ValueError.
- ``iso`` and ``eu``: `isoformat_utc_optional` and `ensure_utc` of ``d``
  (an isoformat, or null); ``o`` the text, or the result's isoformat.
- ``sweep``: `redact_query` over ``n`` texts made by the rule below,
  ``sp`` the calls whose spans are not empty (by case index), ``sha``
  the SHA-256 of one line per case: the hash, the preview's code points
  joined by ``,``, and the length, separated by spaces.

The sweep's rule: a 64-bit LCG (state times 6364136223846793005 plus
1442695040888963407, modulo 2**64) from ``seed``; each step's value is
the state shifted right by 33. A case is a length ``v % 12`` then that
many pieces, each ``PIECES[v % len(PIECES)]``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from bettermemory import events as ev
from bettermemory import store_rows
from bettermemory.credentials import find_credential_markers
from bettermemory import time_utils
from bettermemory.store_schema import IMPORTED_FROM_V8
from bettermemory.credentials import secret_spans

from ._common import jsonl, neutral, text_field

PIECES = (
    "sk-",
    "ant-",
    "ghp_",
    "github_pat_",
    "AKIA",
    "ABCD1234",
    "abcdefghij",
    "0123456789",
    "_",
    "-",
    " ",
    ".",
    "\ud800",
    "é",
    "٠",
    "<|im_start|>",
    "x",
    "Z",
)
SEED = 20261122
SWEEP = 3000


def _lcg(state: int) -> tuple[int, int]:
    state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
    return state, state >> 33


def sweep_texts() -> list[str]:
    state = SEED
    out = []
    for _ in range(SWEEP):
        state, v = _lcg(state)
        pieces = []
        for _ in range(v % 12):
            state, w = _lcg(state)
            pieces.append(PIECES[w % len(PIECES)])
        out.append("".join(pieces))
    return out


def _calls(*texts: str) -> list[list[Any]]:
    """The reference's calls to `secret_spans` for these redacted texts."""
    calls = []
    for text in texts:
        for pattern, marker in ev._SECRET_PATTERNS:
            text = pattern.sub(marker, text)
        spans = [list(s) for s in secret_spans(text)]
        calls.append([text_field("t", text), spans])
    return calls


def _rq(text: str) -> dict[str, Any]:
    return {
        "k": "rq",
        **text_field("t", text),
        "sp": _calls(text),
        "o": neutral(ev.redact_query(text)),
    }


def _redacted(fields: dict[Any, Any]) -> list[str]:
    return [
        v
        for k, v in fields.items()
        if k in ev._REDACTED_TEXT_FIELDS and isinstance(v, str)
    ]


def _rf(fields: dict[Any, Any]) -> dict[str, Any]:
    out = ev._redact_event_fields(fields)
    return {
        "k": "rf",
        "f": neutral(fields),
        "sp": _calls(*_redacted(fields)),
        "o": neutral(out),
    }


def _nf(fields: dict[Any, Any]) -> dict[str, Any]:
    return {
        "k": "nf",
        "f": neutral(fields),
        "o": neutral(ev.named_event_fields(fields)),
    }


def _imp(event: dict[Any, Any], imported_from: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"k": "imp", "f": neutral(event)}
    rest = {k: v for k, v in event.items() if k not in ("ts", "session", "kind")}
    row["sp"] = _calls(*_redacted(rest))
    try:
        if imported_from is None:
            ts, session, kind, payload = store_rows.event_import_payload(event)
        else:
            row["from"] = imported_from
            ts, session, kind, payload = store_rows.event_import_payload(
                event, imported_from=imported_from
            )
    except ValueError as exc:
        row["o"] = {"err": str(exc)}
        return row
    row["o"] = [neutral(ts), neutral(session), neutral(kind), neutral(payload)]
    return row


def _dt_row(k: str, iso: str | None) -> dict[str, Any]:
    dt = None if iso is None else datetime.fromisoformat(iso)
    if k == "iso":
        out: Any = time_utils.isoformat_utc_optional(dt)
    else:
        got = time_utils.ensure_utc(dt)
        out = None if got is None else got.isoformat()
    return {"k": k, "d": iso, "o": out}


_TOKEN = "<|im_start|>"
_SK = "sk-" + "A1b2C3d4" * 3
_ANT = "sk-ant-" + "a_b-" * 5


def _redaction_rows() -> list[dict[str, Any]]:
    texts = [
        "",
        "plain words",
        "a" * 31,
        "a" * 32,
        "a" * 33,
        "é" * 33,
        "\ud800" * 3 + "tail",
        "x\udfff" * 20,
        _TOKEN + " a query",
        # Each pattern at its bound, one under, and with a word boundary.
        "sk-ant-" + "a" * 20,
        "sk-ant-" + "a" * 19,
        "sk-ant-" + "a" * 20 + "_",
        "xsk-ant-" + "a" * 20,
        "sk-ant-" + "-" * 25,
        "sk-ant-" + "a" * 19 + "-",
        _ANT + "é",
        "sk-" + "a" * 20,
        "sk-" + "a" * 19,
        "sk-" + "a" * 20 + "_x",
        "sk-" + "a" * 20 + "-x",
        "_sk-" + "a" * 20,
        "-sk-" + "a" * 20,
        "sk-" + "٠" * 20,
        "SK-" + "a" * 20,
        _SK,
        "ghp_" + "b" * 30,
        "ghp_" + "b" * 29,
        "ghp_" + "Ab9" * 12,
        "ghp_" + "b" * 30 + "é",
        "github_pat_" + "c" * 20,
        "github_pat_" + "c" * 19,
        "github_pat_" + "_" * 20,
        "github_pat_" + "C1_d" * 10,
        "AKIA" + "Z" * 16,
        "AKIA" + "Z" * 15,
        "AKIA" + "Z" * 17,
        "AKIA" + "z" * 16,
        "AKIA" + "AB12" * 4 + " and AKIA" + "9" * 16,
        "key " + _ANT + " then " + _SK + " end",
        "ghp_" + "Ab9" * 12 + "ghp_" + "Ab9" * 12,
        # The detectors' own shapes the five patterns miss.
        "gho_" + "Ab9" * 12,
        "sk-proj-" + "A1b2" * 10,
        "password=hunter2x9 and more",
        "xoxb-" + "1234567890-" * 3 + "abcdef",
        "AIza" + "B1c2" * 9 + "x",
        "secret: " + "q9Z" * 10,
        "[REDACTED:openai-key] stays",
        "sk-ant-api03-" + "Q7w8E9r0" * 4 + " and gho_" + "Zz9" * 12,
        "\ud800 sk-proj-" + "A1b2" * 10 + " \udc00",
        _TOKEN * 4,
        "q" * 1000,
    ]
    return [_rq(t) for t in texts]


def _named_rows() -> list[dict[str, Any]]:
    t = "a " + _TOKEN + " b"
    shapes: list[dict[Any, Any]] = [
        {},
        {"kind": "x", "query": t},
        {
            name: t
            for name in ("claim_excerpts", "client_filter", "model_filter", "note")
        },
        {name: t for name in ("probe_query", "reason", "said")},
        {"session": t, "actor": t, "worktree_root": t},
        {"note": "no token", "reason": None, "said": 3},
        {"claim_excerpts": [t, "x", {t: t}, (t,)]},
        {"note": {"k": t, t: [t, 1.5]}},
        {"review_answers": {"said": t, "other": t}},
        {"review_answers": [{"said": t}, {"said": "fine"}, t, [t], ("said", t)]},
        {"review_answers": [{"said": {"deep": [t]}}]},
        {"review_answers": t},
        {"review_answers": ({"said": t},)},
        {"top_hits": [{"snippet": t, "id": t}, {"id": "m"}, None]},
        {"top_hits": {"snippet": t}},
        {"top_hits": []},
        {"top_hits": [{"snippet": 5, 1: t}]},
        {1: t, "note": t, None: t},
        {"note": "\ud800" + _TOKEN},
        {"query": {"hash": "h", "preview": _TOKEN}},
    ]
    return [_nf(f) for f in shapes]


def _redact_field_rows() -> list[dict[str, Any]]:
    t = "a " + _TOKEN + " b"
    shapes: list[dict[Any, Any]] = [
        {},
        {"query": "find " + _SK, "probe_query": "probe " + _TOKEN},
        {"query": 5, "probe_query": None},
        {"query": ["x"], "note": t},
        {"probe_query": "a" * 40, "x": 1},
        {"query": "", "probe_query": "gho_" + "Ab9" * 12},
        {"imported_from": "v7", "query": "q"},
    ]
    return [_rf(f) for f in shapes]


def _import_rows() -> list[dict[str, Any]]:
    rows = [
        _imp(
            {
                "ts": "2026-01-01T00:00:00Z",
                "session": "s1",
                "kind": "search",
                "query": "q",
            }
        ),
        _imp({"kind": "search", "ts": "", "session": ""}),
        _imp({"kind": "search", "ts": 5, "session": None, "n": 2}),
        _imp({"kind": "search", "ts": ["x"], "session": {"a": 1}}),
        _imp({"note": _TOKEN, "kind": "verify", "imported_from": "v1", "z": 0}),
        _imp({"kind": "search", "query": _SK + " tail"}, imported_from="v7"),
        _imp({"kind": "search", "probe_query": "\ud800" + "a" * 40}, imported_from=""),
        _imp({"kind": "\ud800x", "session": "\udc00"}),
        _imp({}),
        _imp({"kind": ""}),
        _imp({"kind": None}),
        _imp({"kind": 3}),
        _imp({"kind": ["search"]}),
        _imp({1: "search"}),
        _imp({"kind": "write"}),
        _imp({"kind": "remove"}),
        _imp({"kind": "key_rotated"}),
        _imp({"kind": "membership_migrated"}),
        _imp({"kind": "memory_put"}),
        _imp({"kind": "migrate_v8"}),
        _imp({"kind": 'it\'s "quoted"\n'}),
        _imp({"kind": "Write"}),
    ]
    return rows


def _time_rows() -> list[dict[str, Any]]:
    isos = [
        None,
        "2026-01-02T03:04:05",
        "2026-01-02T03:04:05.000001",
        "2026-01-02T03:04:05+00:00",
        "2026-01-02T03:04:05+02:00",
        "2026-01-02T03:04:05-00:00:30",
        "2026-01-02T03:04:05+00:00:30",
        "0001-01-01T00:00:00",
        "9999-12-31T23:59:59.999999-23:59",
    ]
    return [_dt_row(k, iso) for k in ("iso", "eu") for iso in isos]


def _sweep_row() -> dict[str, Any]:
    lines = []
    calls = []
    for i, text in enumerate(sweep_texts()):
        for call in _calls(text):
            if call[1]:
                calls.append([i, *call])
        out = ev.redact_query(text)
        preview = ",".join(str(ord(c)) for c in out["preview"])
        lines.append(f"{out['hash']} {preview} {out['len']}\n")
    sha = hashlib.sha256("".join(lines).encode("ascii")).hexdigest()
    return {"k": "sweep", "seed": SEED, "n": SWEEP, "sp": calls, "sha": sha}


def _header() -> dict[str, Any]:
    return {
        "k": "header",
        "named_event_fields": [
            [name, None if keys is None else sorted(keys)]
            for name, keys in sorted(ev.NAMED_EVENT_FIELDS.items())
        ],
        "secret_patterns": [[p.pattern, p.flags, m] for p, m in ev._SECRET_PATTERNS],
        "imported_from_v8": IMPORTED_FROM_V8,
        "row_columns": list(store_rows._ROW_COLUMNS),
        "pieces": [text_field("t", p) for p in PIECES],
    }


def _unshaped(value: Any) -> Any:
    """`value` with every text a credential detector reads as a key
    written as code points (``<name>_cps``, as `text_field` writes a
    surrogate), so no line of the file is shaped like a key."""
    if isinstance(value, list):
        return [_unshaped(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, str) and find_credential_markers(v):
            out[f"{k}_cps"] = [ord(c) for c in v]
        else:
            out[k] = _unshaped(v)
    return out


def generate() -> dict[str, bytes]:
    rows = [
        _header(),
        *_redaction_rows(),
        *_named_rows(),
        *_redact_field_rows(),
        *_import_rows(),
        *_time_rows(),
        _sweep_row(),
    ]
    return {"event_redact.jsonl": jsonl([_unshaped(r) for r in rows])}
