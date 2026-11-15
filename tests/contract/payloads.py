"""Synthetic payloads shaped as the product writes them, for the log fixture.

Three sources, in order of preference for a kind:

- ``store``: a row the store-path build wrote (`gen_store_path.log_rows`),
  its payload with every value replaced by a synthetic one of the same
  type (`synthetic`);
- ``call_site``: the keys a call site names (`kinds.call_sites`), each
  with a value of the type its source expression reads as (`_typed`),
  telemetry put through the recorder's own redaction and given an actor;
- ``table``: a membership mutation, from its table's columns
  (`table_payload`);
- ``legacy``: a kind the tree writes only by importing a v8 event
  (`store_events.import_event` keeps any kind), shaped from the keys and
  value types such rows carry (`LEGACY_SHAPES`).

A kind none of them shapes gets ``{}`` and ``shape_source: none``.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from typing import Any

from bettermemory.events import _redact_event_fields
from bettermemory.store_schema import (
    _MEMBERSHIP_SPECS,
    ADDITIVE_SCHEMA,
    MEMBERSHIP_SCHEMA,
    SCHEMA,
)

from .kinds import Site

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_HEX = re.compile(r"[0-9a-f]+")

_ACTOR = {
    "client": "fixture-client",
    "client_version": "1.0.0",
    "model": "fixture-model",
    "sources": {
        "client": "declared",
        "client_version": "declared",
        "model": "declared",
    },
}

#: Keys and value types of the rows the tree writes only through a v8
#: import, read from such rows (types only; every value is synthetic).
LEGACY_SHAPES: dict[str, dict[str, Any]] = {
    "curate": {
        "dedup_method": "str",
        "demoted": "int",
        "failures": "int",
        "imported_from": "str",
        "tombstoned": "int",
        "window_days": "int",
        "worktree_root": "str",
    },
    "episode_search": {
        "actor": "actor",
        "auto_scope": "bool",
        "ids": "int",
        "imported_from": "str",
        "include_bodies": "bool",
        "max_results": "int",
        "parent_session_id": "str",
        "returned": "int",
        "scopes_filter": ["str"],
        "since": "str",
        "swarm_id": "str",
        "worktree_root": "str",
    },
    "list": {
        "actor": "actor",
        "client_filter": "str",
        "count": "int",
        "imported_from": "str",
        "model_filter": "null",
        "returned": ["str"],
        "scopes_filter": ["str"],
        "with_bodies": "bool",
        "worktree_root": "str",
    },
    "memory_proposals": {
        "action": "str",
        "found": "bool",
        "imported_from": "str",
        "proposal_id": "str",
        "returned": "int",
        "worktree_root": "str",
    },
    "migrate": {
        "action": "str",
        "attribution": "str",
        "ids": ["str"],
        "imported_from": "str",
        "repaired_anchored": "int",
        "repaired_demoted": "int",
        "updated": "int",
    },
    "pending_expired": {
        "actor": "actor",
        "category": "str",
        "imported_from": "str",
        "pending_id": "str",
        "ttl_seconds": "int",
        "worktree_root": "str",
    },
    "proposals_enqueued": {
        "count": "int",
        "imported_from": "str",
        "session_id": "str",
        "triggered_from": "str",
    },
    "scope_overview": {
        "actor": "actor",
        "auto_scope": "bool",
        "curation_pending": {
            name: "int"
            for name in (
                "cold",
                "cold_endorsement_memories",
                "conflicts",
                "dead",
                "drifted",
                "never_verified",
                "silent_misses",
                "stale",
                "unaccounted",
                "unique_silent_miss_memories",
            )
        },
        "curation_pending_new_since_last_session": "null",
        "curation_unmeasured": ["str"],
        "current_repo": "str",
        "imported_from": "str",
        "pending_writes": "int",
        "prior_session_boundary": "str",
        "proposals_pending": "int",
        "recently_removed_in_worktree": "int",
        "scope_count": "int",
        "total": "int",
        "worktree_root": "str",
    },
    "write_cancel": {
        "actor": "actor",
        "episode_id": "null",
        "existed": "bool",
        "imported_from": "str",
        "pending_id": "str",
        "worktree_root": "str",
    },
    "write_confirm": {
        "actor": "actor",
        "episode_id": "null",
        "id": "str",
        "imported_from": "str",
        "pending_id": "str",
        "scopes": ["str"],
        "worktree_root": "str",
    },
}


def synthetic(value: Any, key: str = "v") -> Any:
    """`value` with every leaf replaced by a fixed one of its type: a
    timestamp by a timestamp of the same spelling, hex by hex of the same
    length, other text by text naming its key, numbers by fixed numbers."""
    if isinstance(value, dict):
        return {k: synthetic(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [synthetic(v, key) for v in value[:2]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return 7
    if isinstance(value, float):
        return 0.625
    if isinstance(value, str):
        if _TS.match(value):
            stamp = "2026-01-02T03:04:05"
            rest = value[19:]
            if rest.startswith("."):
                stamp += ".123456"
            if value.endswith("Z"):
                stamp += "Z"
            elif value.endswith("+00:00"):
                stamp += "+00:00"
            return stamp
        if len(value) >= 16 and _HEX.fullmatch(value):
            return ("0123456789abcdef" * 8)[: len(value)]
        return f"<{key}>"
    return f"<{key}>"


def from_shape(shape: Any, key: str = "v") -> Any:
    if shape == "actor":
        return dict(_ACTOR)
    if isinstance(shape, dict):
        return {k: from_shape(v, k) for k, v in shape.items()}
    if isinstance(shape, list):
        return [from_shape(shape[0], key)] if shape else []
    return {"str": f"<{key}>", "int": 7, "float": 0.625, "bool": True, "null": None}[
        shape
    ]


_FLOAT_CUES = (
    "score",
    "similarity",
    "ratio",
    "rate",
    "elapsed",
    "seconds",
    "coverage",
    "weight",
    "jaccard",
    "round(",
    "float(",
    " / ",
    "_ms",
)
_BOOL_PREFIXES = ("is_", "has_", "auto", "forced", "found", "existed", "expand_top")


def _scalar(key: str, expr: str) -> Any:
    low = f"{key} {expr}".lower()
    if (
        expr in ("True", "False")
        or " is " in expr
        or expr.startswith(("not ", "bool("))
    ):
        return True
    if key.startswith(_BOOL_PREFIXES) or key.endswith("_acknowledged"):
        return True
    if any(cue in low for cue in _FLOAT_CUES):
        return 0.625
    if "len(" in expr or "count" in key or key.endswith(("_n", "_total")):
        return 7
    if expr.startswith("{") or "to_dict(" in expr or "model_dump(" in expr:
        return {"k": f"<{key}>"}
    return f"<{key}>"


def _typed(key: str, expr: str) -> Any:
    try:
        return ast.literal_eval(expr)
    except (ValueError, SyntaxError):
        pass
    if expr.startswith(("[", "list(", "sorted(", "tuple(")):
        return [_scalar(key, expr.split(" for ", 1)[0])]
    return _scalar(key, expr)


def from_site(site: Site) -> dict[str, Any]:
    assert site.keys is not None
    fields = {key: _typed(key, expr) for key, expr in site.keys.items()}
    if site.call in ("record", "record_event"):
        payload = _redact_event_fields(fields)
        payload.setdefault("actor", dict(_ACTOR))
        return payload
    return fields


def store_payloads(
    rows: list[tuple[str, str | None, str]],
) -> dict[str, dict[str, Any]]:
    """The first row of each kind the store-path build wrote, synthetic."""
    out: dict[str, dict[str, Any]] = {}
    for kind, _session, text in rows:
        if kind not in out:
            out[kind] = synthetic(json.loads(text))
    return out


def _column_types() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(MEMBERSHIP_SCHEMA)
        conn.executescript(ADDITIVE_SCHEMA)
        out: dict[tuple[str, str], str] = {}
        for spec in _MEMBERSHIP_SPECS.values():
            for _, name, kind, *_ in conn.execute(f'PRAGMA table_info("{spec.table}")'):
                out[(spec.table, str(name))] = str(kind).upper()
        return out
    finally:
        conn.close()


def table_payload(kind: str) -> dict[str, Any] | None:
    """A membership mutation as `store_membership.put_membership` and
    `delete_membership` write it: ``{<table kind>: {column: value}}`` with
    every column for a put, the key columns for a delete. A ``_json``
    column holds a JSON value, a `floats` column a float, the rest the
    column's SQL type."""
    for suffix in ("_put", "_delete"):
        if not kind.endswith(suffix):
            continue
        spec = _MEMBERSHIP_SPECS.get(kind[: -len(suffix)])
        if spec is None:
            return None
        types = _column_types()

        def value(column: str) -> Any:
            if column.endswith("_json"):
                return {"k": f"<{column}>"}
            if column in spec.floats:
                return 0.625
            if types.get((spec.table, column), "TEXT").startswith("INT"):
                return 7
            return f"<{column}>"

        if suffix == "_delete":
            return {column: value(column) for column in spec.key}
        return {spec.kind: {column: value(column) for column in spec.columns}}
    return None
