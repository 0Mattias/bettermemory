"""Exact native strings at UTF-8 storage and transport boundaries.

Python represents undecodable POSIX filename bytes with surrogateescape.
JSON can carry those code points as escapes, while SQLite TEXT and the
signed log must hold valid UTF-8. Existing Unicode spellings stay intact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def has_surrogates(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, Mapping):
        return any(has_surrogates(k) or has_surrogates(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_surrogates(v) for v in value)
    return False


def utf8_text(text: str) -> str:
    """Escape only code points UTF-8 cannot encode; ordinary text is unchanged."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def encoded_json(value: Any) -> str:
    """An ASCII JSON string preserving every code point, including surrogates."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def origin_projection(origin: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Legacy Unicode fields and an exact extension when a native string needs it."""
    if not has_surrogates(origin):
        return dict(origin), None
    legacy = {k: v for k, v in origin.items() if not has_surrogates(v)}
    return legacy, encoded_json(dict(origin))


def mutation_payload(kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep legacy record columns UTF-8; the signed envelope holds native origin."""
    if kind not in {"memory_put", "tombstone_put", "episode_put"}:
        return payload
    field = kind.removesuffix("_put")
    record = payload.get(field)
    if not isinstance(record, Mapping) or not isinstance(record.get("origin"), Mapping):
        return payload
    legacy, native = origin_projection(record["origin"])
    if native is None:
        return payload
    return {**payload, field: {**record, "origin": legacy}, "native_origin": native}
