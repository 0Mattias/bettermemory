"""Shared encoding for the contract fixtures.

Every golden file is ASCII: JSON objects with sorted keys and compact
separators, one per line for a ``.jsonl`` file. A string that holds a lone
surrogate is never written as a JSON string (many JSON readers refuse the
escape); it is written as a list of code points under a ``*_cps`` key.
"""

from __future__ import annotations

import json
import logging
import struct
import sys
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import PurePath
from typing import Any
from unittest.mock import patch


@contextmanager
def captured_by(
    logger: logging.Logger, handler: logging.Handler, level: int | None = None
) -> Iterator[None]:
    """Route ``logger``'s records to ``handler`` alone for the block.

    The logger stops propagating, so no ancestor (pytest's own capture
    among them) keeps a copy of a record; its handlers, level and
    propagate flag are restored afterwards whatever happens.
    """
    propagate, old_level = logger.propagate, logger.level
    logger.addHandler(handler)
    logger.propagate = False
    if level is not None:
        logger.setLevel(level)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(old_level)


def has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def text_field(name: str, text: str) -> dict[str, Any]:
    """``{name: text}``, or ``{name_cps: [...]}`` when ``text`` holds a
    surrogate."""
    if has_surrogate(text):
        return {f"{name}_cps": [ord(ch) for ch in text]}
    return {name: text}


def neutral(value: Any) -> dict[str, Any]:
    """A language-neutral description of a Python value."""
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": str(value)}
    if isinstance(value, float):
        bits = struct.unpack(">Q", struct.pack(">d", value))[0]
        return {"t": "float", "bits": f"{bits:016x}", "repr": repr(value)}
    if isinstance(value, str):
        return {"t": "str", **text_field("v", value)}
    if isinstance(value, datetime):
        return {"t": "datetime", "iso": value.isoformat()}
    if isinstance(value, PurePath):
        return {"t": "path", "v": value.as_posix()}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [neutral(v) for v in value]}
    if isinstance(value, list):
        return {"t": "list", "v": [neutral(v) for v in value]}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[neutral(k), neutral(v)] for k, v in value.items()]}
    raise TypeError(f"no neutral form for {type(value).__name__}")


def output(result: str | bytes) -> dict[str, Any]:
    """Exact output: bytes as hex; a str as its UTF-8 bytes, or as code
    points when it holds a surrogate UTF-8 cannot carry."""
    if isinstance(result, bytes):
        return {"hex": result.hex()}
    if has_surrogate(result):
        return {"cps": [ord(ch) for ch in result], "utf8": False}
    return {"hex": result.encode("utf-8").hex()}


def dumps_line(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def jsonl(records: list[Any]) -> bytes:
    return "".join(dumps_line(r) + "\n" for r in records).encode("ascii")


def json_doc(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=1) + "\n").encode(
        "ascii"
    )


def environment() -> dict[str, str]:
    """What produced a fixture, written into its header. Only the Unicode
    data version gates comparison (`UNICODE_GATED`)."""
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "unidata_version": unicodedata.unidata_version,
    }


@contextmanager
def raw_scores() -> Iterator[dict[str, float]]:
    """The unrounded score of each hit `search.search` builds, by record
    id, read at `search._build_hit` (a hit's ``score`` is rounded to four
    places there)."""
    from bettermemory import search as bsearch

    seen: dict[str, float] = {}
    original = bsearch._build_hit

    def capture(memory: Any, score: float, *args: Any, **kwargs: Any) -> Any:
        seen[memory.id] = score
        return original(memory, score, *args, **kwargs)

    with patch.object(bsearch, "_build_hit", capture):
        yield seen


def float_bits(value: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', value))[0]:016x}"


def hit_records(hits: list[Any], raw: dict[str, float]) -> list[dict[str, Any]]:
    """Ranked hits: id, the returned score (rounded by the product) and the
    unrounded one, each as Python repr and IEEE-754 bits."""
    return [
        {
            "rank": rank,
            "id": h.id,
            "score": repr(h.score),
            "bits": float_bits(h.score),
            "raw_score": repr(raw[h.id]),
            "raw_bits": float_bits(raw[h.id]),
            "relevance": h.relevance,
            "match_terms": list(h.match_terms),
        }
        for rank, h in enumerate(hits, start=1)
    ]
