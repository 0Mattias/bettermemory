"""Native membership values beside the UTF-8 legacy projection.

A byte path has no equivalent SQLite TEXT path. Its exact row is an ASCII
JSON string in a new table and new mutation kind, which older folds ignore.
The legacy row keeps IDs and conservative evidence where its key is UTF-8;
a native path key has no legacy row. Revision 5 keeps older writers read-only
so they cannot retire a checkout whose native view they cannot observe.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from .native_text import encoded_json, has_surrogates, mutation_payload, utf8_text
from .store_schema import StoreVersionError, _MembershipTable


def row_key(spec: _MembershipTable, row: Mapping[str, Any]) -> str:
    return encoded_json([row[c] for c in spec.key])


def legacy_row(spec: _MembershipTable, row: Mapping[str, Any]) -> dict[str, Any] | None:
    if any(has_surrogates(row[c]) for c in spec.key):
        return None
    out = dict(row)
    for column, value in row.items():
        if not has_surrogates(value):
            continue
        if column.endswith("_json"):
            out[column] = [] if isinstance(value, (list, tuple)) else {}
        elif column in {"name", "line", "reason"} and isinstance(value, str):
            out[column] = utf8_text(value)
        else:
            out[column] = None
    return out


def native_rows(
    conn: sqlite3.Connection, spec: _MembershipTable
) -> dict[str, dict[str, Any] | None]:
    if spec.kind == "native_member":
        return {}
    from .store_rows import _membership_values

    found: dict[str, dict[str, Any] | None] = {}
    try:
        rows = conn.execute(
            "SELECT key_text, row_text FROM native_membership_rows WHERE member_kind = ?",
            (spec.kind,),
        )
    except sqlite3.OperationalError as exc:
        if str(exc) == "no such table: native_membership_rows":
            return {}
        raise
    for key, text in rows:
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                found[key] = None
                continue
            _membership_values(spec, row)
            found[key] = row if row_key(spec, row) == key else None
        except (ValueError, TypeError, KeyError, RecursionError):
            found[key] = None
    return found


def matches(
    row: Mapping[str, Any],
    where: Mapping[str, Any] | None,
    column_in: tuple[str, list[Any]] | None,
) -> bool:
    return all(row.get(c) == v for c, v in (where or {}).items()) and (
        column_in is None or row.get(column_in[0]) in column_in[1]
    )


def require_native_writer(conn: sqlite3.Connection) -> None:
    revision = conn.execute(
        "SELECT value FROM meta WHERE key = 'min_writer_revision'"
    ).fetchone()
    if revision is None or int(revision[0]) < 5:
        raise StoreVersionError("open this store as its owner to preserve native paths")


def prepare_mutation(
    conn: sqlite3.Connection, kind: str, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Project a native record only once its store excludes older writers."""
    encoded = mutation_payload(kind, payload)
    if (
        kind in {"native_member_put", "native_member_delete"}
        or "native_origin" in encoded
    ):
        require_native_writer(conn)
    return encoded
