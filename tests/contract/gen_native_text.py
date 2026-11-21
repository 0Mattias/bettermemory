"""Fixtures: `native_text`'s exact strings (``native_text.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/native_text.rs``. Rows:

- ``k: "has"``: a value (`neutral`) and `has_surrogates` of it;
- ``k: "utf8"``: a text (``t`` or ``t_cps``) and `utf8_text` of it;
- ``k: "proj"``: an origin (`neutral` dict) and `origin_projection` of
  it: ``legacy`` (`neutral`) and ``native`` (the text, or null), or
  ``raises`` (the exception's class name);
- ``k: "sweep"``: `utf8_text` of every code point alone, in order, as the
  SHA-256 of the UTF-8 answers joined, and their count.

Not gated: the answers follow neither the Python release nor Unicode.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from bettermemory import native_text as nt

from ._common import jsonl, neutral, text_field

HAS: list[Any] = [
    "",
    "plain",
    "\ud800",
    "a\udfffb",
    "\U0001f600",
    "\ue000\U0001f600",
    PurePosixPath("/x/y"),
    [],
    ["a", ("b", "\udc80")],
    ("x", 1, None),
    {"\ud83d": 1},
    {"k": ["v", {"n": "\udcff"}]},
    {"k": "v", 1: 2.5},
    1,
    True,
]

TEXTS = [
    "",
    "plain ascii",
    "caf\u00e9 \u65e5\u672c \U0001f600",
    "\ud800",
    "\udfff",
    "\udc80\udcff",
    "a\U0001f600b",
    "\\ud800 already",
    "\x00\x7f\x80",
]

ORIGINS: list[dict[Any, Any]] = [
    {},
    {"repo": "r", "path": "p", "n": 3},
    {"path": "/x/\udc80y", "repo": "r"},
    {"\udc80key": "clean", "repo": "r"},
    {"\udc80key": "\udc81", "b": "ok"},
    {"z": ["a", "\udc80"], "a": {"n": "\udcff"}, "m": None, "f": 1.5},
    {"\U0001f600": "astral", "\ud800": "lone", "\uffff": "bmp", "a": "\udc80"},
    {"\U00010000": 1, "\ud7ff": 2, "\ue000": 3, "\udfff": "\udfff"},
    {"p": PurePosixPath("/clean"), "q": "clean"},
    {"p": PurePosixPath("/a"), "q": "\udc80"},
    {float("nan"): 1.5},
    {"f": float("nan"), "g": [float("-inf")], "s": "\udc80"},
]


def _utf8(text: str) -> dict[str, Any]:
    return {"k": "utf8", **text_field("t", text), "r": nt.utf8_text(text)}


def _proj(origin: dict[Any, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"k": "proj", "o": neutral(origin)}
    try:
        legacy, native = nt.origin_projection(origin)
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        row["raises"] = type(exc).__name__
        return row
    row["legacy"] = neutral(legacy)
    row["native"] = native
    return row


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for c in range(0x110000):
        digest.update(nt.utf8_text(chr(c)).encode("utf-8"))
    return {"k": "sweep", "count": 0x110000, "sha": digest.hexdigest()}


def generate() -> dict[str, bytes]:
    rows = [
        *({"k": "has", "v": neutral(v), "r": nt.has_surrogates(v)} for v in HAS),
        *(_utf8(t) for t in TEXTS),
        *(_proj(o) for o in ORIGINS),
        _sweep(),
    ]
    return {"native_text.jsonl": jsonl([{"cases": len(rows)}, *rows])}
