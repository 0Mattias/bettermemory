"""Fixtures: the event kinds and their row classes (``events_kinds.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/events.rs``. This pins the first
unit of `events`: the static kind tables (`HARNESS_STAMPED_KINDS`,
`AUDIT_KINDS`, `OVERRIDE_EVIDENCE_KINDS`, `AUDIT_STATUSES`,
`OVERRIDABLE_REFUSALS`, `STATE_STAMP_FIELDS`, `STATE_FIELDS` as `_kept`
builds it, `MEASUREMENT_KINDS`, `ROW_KINDS` and the scalars beside them),
`is_audit_row`, `row_class`, `prunable`, `state_fields` and `_keys_of`.

One file, not gated, one JSON object per line, keys sorted, ASCII. Its
first line is the header (``{"k": "tables", ...}``: every table, each
frozenset sorted, each mapping in its order, and the count of the lines
after it). Every other line is one of:

- ``{"k": "class", "kind": text, "c": class value, "p": prunable}``;
- ``{"k": "audit", "kind": text, "f": fields, "o": bool or {"err": name}}``;
- ``{"k": "state", "kind": text, "f": fields, "o": fields or {"err":
  name}}``: `state_fields`;
- ``{"k": "keys", "v": value, "keys": [...], "o": value}``: `_keys_of`.

Fields and values are in `_common.neutral`'s form; a kind is
`_common.text_field`'s. A `probe_query` still in text goes through
`redact_query`; every such text here is one `_strip_known_secrets` leaves
as it is, so its kept hash is the SHA-256 of its UTF-8 (surrogates
replaced by ``?``) and its length the count of its code points, which the
native test computes in place of the redaction a later unit builds.
"""

from __future__ import annotations

from typing import Any

from bettermemory import events as ev

from ._common import jsonl, neutral, text_field

_ODD_KINDS = [
    "",
    "Write",
    "write ",
    " write",
    "writes",
    "unknown",
    "standing_guard\x00",
    "\ud800",
    "shöw",
    "telemetry",
]

_EVERY_STAMP = {name: f"<{name}>" for name in sorted(ev.STATE_STAMP_FIELDS)}

_PROBE_TEXTS = ["", "kubernetes networking", "a" * 40, "café \U0001f600", "x\ud800y"]


#: The kinds every audit case runs on: the override-evidence kinds, the
#: audit kind, one other state kind and a near miss. Every other kind runs on `_AUDIT_FEW`.
_AUDIT_SWEPT = frozenset({"write", "update", "standing_guard", "use", "Write"})

_AUDIT_FEW: list[dict[Any, Any]] = [
    {},
    {"forced": True, "status": "duplicate", "credentials_acknowledged": True},
    {1: True},
]


def _all_kinds() -> list[str]:
    return [*ev.STATE_FIELDS, *sorted(ev.MEASUREMENT_KINDS), *_ODD_KINDS]


def _audit_fields() -> list[dict[Any, Any]]:
    rows: list[dict[Any, Any]] = [
        {},
        {"status": "ok"},
        {"status": "committed", "id": "m1"},
        {"forced": True},
        {"forced": 1},
        {"forced": "true"},
        {"forced": False},
        {"forced": 1.0},
        {"status": None},
        {"status": 3},
        {"status": ["duplicate"]},
        {"status": "Duplicate"},
        {"status": "duplicate "},
        {"status": "stale"},
        {"status": "control_token_warning"},
        {"status": "policy_refused"},
        {"credentials_acknowledged": True},
        {"credentials_acknowledged": False},
        {"credentials_acknowledged": 0},
        {"credentials_acknowledged": 0.0},
        {"credentials_acknowledged": -0.0},
        {"credentials_acknowledged": float("nan")},
        {"credentials_acknowledged": ""},
        {"credentials_acknowledged": "no"},
        {"credentials_acknowledged": []},
        {"credentials_acknowledged": [0]},
        {"credentials_acknowledged": {}},
        {"credentials_acknowledged": ()},
        {"credentials_acknowledged": None},
        {"credentials_acknowledged": 10**30},
        {"_acknowledged": True},
        {"acknowledged": True},
        {"x_Acknowledged": True},
        {"x_acknowledged ": True},
        {"brand_new_acknowledged": 2},
        {"a_acknowledged": 0, "b_acknowledged": 1},
        {1: True},
        {"credentials_acknowledged": True, 1: True},
        {1: True, "credentials_acknowledged": True},
        {"forced": True, 1: True},
        {"status": "duplicate", 1: True},
        {(1, 2): False},
        {None: 1, "status": "ok"},
        {"status": "ok", "forced": False, "markers_acknowledged": False},
    ]
    rows += [{"status": s} for s in sorted(ev.OVERRIDABLE_REFUSALS)]
    return rows


def _state_fields_for(kind: str) -> list[dict[Any, Any]]:
    if kind not in ev.STATE_FIELDS:
        return [
            {"note": "n", "session_id": "s", "forced": True, "a_acknowledged": 1},
            {7: "seven", "id": "m"},
        ]
    full: dict[Any, Any] = {name: f"v:{name}" for name in ev.STATE_FIELDS[kind]}
    return [
        {},
        {**full, "note": "free text", "query": "q", "session_id": "s"},
        {"note": "n", "credentials_acknowledged": True, "forced": True},
        {"reason": "r", 7: "seven"},
        {7: "seven", "session_id": "s"},
    ]


_TOP_HITS: list[Any] = [
    None,
    "text",
    3,
    [],
    {},
    {"id": "m1", "snippet": "body", "score": 0.5},
    [{"id": "m1", "snippet": "body"}, "m2", 3, None, {"snippet": "x"}, {}],
    [[{"id": "inner"}]],
    ({"id": "m1"},),
    [{1: "one", "id": "m3", "ID": "m4"}],
]

_PROBE_QUERIES: list[Any] = [
    None,
    7,
    {"hash": "abc", "preview": "p", "len": 3, "extra": True},
    {"preview": "p"},
    [{"hash": "h", "len": 1, "preview": "x"}, "y"],
    ("t",),
    *_PROBE_TEXTS,
]


def _class_row(kind: str) -> dict[str, Any]:
    return {
        "k": "class",
        **text_field("kind", kind),
        "c": ev.row_class(kind).value,
        "p": ev.prunable(kind),
    }


def _audit_row(kind: str, fields: dict[Any, Any]) -> dict[str, Any]:
    try:
        out: Any = ev.is_audit_row(kind, fields)
    except AttributeError:
        out = {"err": "AttributeError"}
    return {"k": "audit", **text_field("kind", kind), "f": neutral(fields), "o": out}


def _state_row(kind: str, fields: dict[Any, Any]) -> dict[str, Any]:
    for name in ("probe_query", "query"):
        value = fields.get(name)
        if isinstance(value, str):
            assert ev._strip_known_secrets(value) == value, value
    try:
        out: Any = neutral(ev.state_fields(kind, fields))
    except AttributeError:
        out = {"err": "AttributeError"}
    return {"k": "state", **text_field("kind", kind), "f": neutral(fields), "o": out}


def _keys_row(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    return {
        "k": "keys",
        "v": neutral(value),
        "keys": sorted(keys),
        "o": neutral(ev._keys_of(value, keys)),
    }


def _tables() -> dict[str, Any]:
    return {
        "k": "tables",
        "acknowledged_suffix": ev._ACKNOWLEDGED_SUFFIX,
        "audit_kinds": sorted(ev.AUDIT_KINDS),
        "audit_statuses": sorted(ev.AUDIT_STATUSES),
        "harness_session_field": ev.HARNESS_SESSION_FIELD,
        "harness_stamped_kinds": sorted(ev.HARNESS_STAMPED_KINDS),
        "measurement_kinds": sorted(ev.MEASUREMENT_KINDS),
        "overridable_refusals": [[k, v] for k, v in ev.OVERRIDABLE_REFUSALS.items()],
        "override_evidence_kinds": sorted(ev.OVERRIDE_EVIDENCE_KINDS),
        "query_hash_prefix": ev._QUERY_HASH_PREFIX,
        "query_preview_chars": ev._QUERY_PREVIEW_CHARS,
        "redacted_text_fields": sorted(ev._REDACTED_TEXT_FIELDS),
        "row_classes": [[c.name, c.value] for c in ev.RowClass],
        "row_kinds": [[c.value, sorted(kinds)] for c, kinds in ev.ROW_KINDS.items()],
        "state_fields": [
            [
                kind,
                [
                    [name, None if keys is None else sorted(keys)]
                    for name, keys in kept.items()
                ],
            ]
            for kind, kept in ev.STATE_FIELDS.items()
        ],
        "state_kinds": sorted(ev.STATE_KINDS),
        "state_stamp_fields": sorted(ev.STATE_STAMP_FIELDS),
        "state_write_attempts": ev.STATE_WRITE_ATTEMPTS,
    }


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    kinds = _all_kinds()
    rows += [_class_row(kind) for kind in kinds]
    for kind in kinds:
        full = kind in _AUDIT_SWEPT
        rows += [_audit_row(kind, f) for f in (_audit_fields() if full else _AUDIT_FEW)]
    for kind in kinds:
        rows += [_state_row(kind, f) for f in _state_fields_for(kind)]
    rows += [
        _state_row("search_miss", {"top_hits": v, "event_id": "e"}) for v in _TOP_HITS
    ]
    rows += [
        _state_row("turn_audited", {"probe_query": v, "verdict": "x"})
        for v in _PROBE_QUERIES
    ]
    rows += [
        _state_row("show", {**_EVERY_STAMP, "zzz_acknowledged": 0}),
        _state_row("write", {**_EVERY_STAMP, "zzz_acknowledged": 0}),
        _state_row("search", {"query": "plain", "returned": ["a"]}),
        _state_row("write", {"query": "kept as given", "credentials_acknowledged": 0}),
        _state_row("update", {"status": "ok", 2: "two"}),
        _state_row("show", {2: "two", "id": "m"}),
        _state_row("write", {"forced": True, "said": "words", "x_acknowledged": None}),
    ]
    for keys in (frozenset({"id"}), frozenset({"hash", "len"}), frozenset()):
        rows += [_keys_row(v, keys) for v in _TOP_HITS + _PROBE_QUERIES[:6]]
    header = {**_tables(), "count": len(rows)}
    return {"events_kinds.jsonl": jsonl([header, *rows])}
