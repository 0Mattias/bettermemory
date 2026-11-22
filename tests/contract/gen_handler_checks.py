"""Fixture: the write-payload validators of ``handlers/_shared.py``
(``handler_checks.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/handler_checks.rs``. It pins
`_validate_scope_count`, `_validate_content_size`,
`_validate_content_floor`, `_validate_write_payload` (and through it
`models.validate_scope` and `capture.token_of`).

Every value is written by `V`. A row's ``k`` names the
function; ``o`` is ``{"ok": value}`` or ``{"err": "Value"|"Encode",
"m": message}`` (``Encode`` for `UnicodeEncodeError`, ``Value`` for any
other `ValueError`).

The ``digest`` row pins `_validate_write_payload` over ``n`` payloads
drawn by a 64-bit linear congruential generator (`_draws`) from the pools
in the row: the SHA-256 of the answers, one per line (``ok:`` and the
confidence, source, category and scopes joined by ``|``, or the class and
the message), in UTF-8.

Not gated: the answers are `str` methods on whitespace no Unicode release
since 14.0 changed, `repr` of `str`, and `UnicodeEncodeError`'s text, the
same under Python 3.11 to 3.14 and on every platform; no row holds a path
the host spells.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from bettermemory import models
from bettermemory.handlers import _shared

from ._common import dumps_line, has_surrogate


def V(value: Any) -> Any:
    """A value compactly: a `str` as itself (``{"$cps": [...]}`` when it
    holds a surrogate), a dict as ``{"$d": [[key, value], ...]}`` in its
    order, lists, bools, ints and None as JSON has them."""
    if isinstance(value, str):
        return {"$cps": [ord(c) for c in value]} if has_surrogate(value) else value
    if isinstance(value, dict):
        return {"$d": [[V(k), V(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [V(v) for v in value]
    assert value is None or isinstance(value, (bool, int)), value
    return value


N = V


def _run(f: Any) -> dict[str, Any]:
    try:
        return {"ok": N(f())}
    except ValueError as exc:
        cls = "Encode" if isinstance(exc, UnicodeEncodeError) else "Value"
        return {"err": cls, "m": N(str(exc))}


def _payload(**kw: Any) -> dict[str, Any]:
    out = _shared._validate_write_payload(**kw)
    return {
        "content": out["content"],
        "scopes": out["scopes"],
        "confidence": out["confidence"].value,
        "source": out["source"].value,
        "category": out["category"].value,
    }


def _pay(**kw: Any) -> dict[str, Any]:
    args = {
        "content": "a body",
        "scopes": ["global"],
        "confidence": "high",
        "source": "inferred",
        "allowed_scopes": [],
        "category": "fact",
        "max_content_bytes": 0,
        "min_content_tokens": 0,
        "max_scopes_per_write": 0,
        "capture": False,
    }
    args.update(kw)
    return {"k": "pay", "a": N(args), "o": _run(lambda: _payload(**args))}


def _cp(*points: int) -> str:
    """Text from code points: a surrogate pair written as escapes in the
    source would be read as one character."""
    return "".join(map(chr, points))


# One surrogate, a run of three, and two runs apart.
SURR = (
    _cp(0xD800),
    _cp(0x61, 0xDFFF),
    _cp(0x61, 0x62, 0xD800, 0xDC00, 0xDFFF, 0x63),
    _cp(0xD800, 0x78, 0xDC00, 0xDC01),
)


def _leaves() -> Iterator[dict[str, Any]]:
    for n, cap in ((0, 0), (5, 0), (5, -1), (3, 3), (4, 3), (2**40, 5), (1, 1)):
        yield {
            "k": "count",
            "n": n,
            "max": cap,
            "o": _run(
                lambda n=n, cap=cap: _shared._validate_scope_count(
                    ["s"] * n if n <= 10 else _Len(n), cap
                )
            ),
        }
    texts = ["", "abc", "\u00e9\u00e9", "\u20ac", "\U0001f600", "x" * 9, *SURR]
    for text in texts:
        for cap in (0, -3, 1, 3, 4, 8, 9, 100):
            for field in ("content", "takeaway"):
                if field == "takeaway" and cap not in (1, 100):
                    continue
                yield {
                    "k": "size",
                    "c": N(text),
                    "max": cap,
                    "f": field,
                    "o": _run(
                        lambda t=text, c=cap, f=field: _shared._validate_content_size(
                            t, c, field_name=f
                        )
                    ),
                }
    floors = [
        "",
        " ",
        "a",
        "a b",
        " a\tb\nc ",
        "a\u3000b\u0085c",
        "a\x1cb\x1fc",
        "a\u200bb",
        "a\xa0b",
        "\ud800 \udc00",
    ]
    for text in floors:
        for m in (0, -1, 1, 2, 3, 4):
            yield {
                "k": "floor",
                "c": N(text),
                "min": m,
                "o": _run(lambda t=text, mm=m: _shared._validate_content_floor(t, mm)),
            }
    scopes = [
        "a",
        "global",
        "projects:foo",
        "a-b:c",
        "a--b",
        "a:-b",
        "-a",
        "a-",
        ":",
        "",
        "A",
        "a b",
        "projects:foo\n",
        "\u00e9",
        "a\ud800",
        "\uff41",
        "0",
        "a:b:c-d-e",
        "a_b",
        "\u0661",
    ]
    for s in scopes:
        yield {
            "k": "scope",
            "s": N(s),
            "o": _run(lambda ss=s: models.validate_scope(ss)),
        }


class _Len(list[str]):
    """A list whose length is `n` without holding `n` items."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n

    def __len__(self) -> int:
        return self._n


def _payloads() -> Iterator[dict[str, Any]]:
    yield _pay()
    for content in ("", " ", "\t\n\u3000", "\u0085", "x"):
        yield _pay(content=content)
    yield _pay(scopes=[])
    yield _pay(content="", scopes=[])
    # The order: floor before size before count before each scope.
    yield _pay(content="one", min_content_tokens=2, max_content_bytes=1)
    yield _pay(content="one two", min_content_tokens=2, max_content_bytes=1)
    yield _pay(content="a\ud800", min_content_tokens=3, max_content_bytes=1)
    yield _pay(content="a b \ud800", min_content_tokens=3, max_content_bytes=1)
    yield _pay(content=_cp(0x61, 0x20, 0xD800, 0xDC00), max_content_bytes=100)
    yield _pay(content=_cp(0x61, 0x20, 0xD800, 0xDC00), max_content_bytes=0)
    yield _pay(scopes=["Bad", "a", "b"], max_scopes_per_write=2)
    yield _pay(scopes=["a", "Bad"], max_scopes_per_write=2)
    yield _pay(scopes=["a", "b\n"])
    yield _pay(scopes=["a", "x"], allowed_scopes=["z", "a", "\u00e9", "B"])
    yield _pay(scopes=["q", "x", "q"], allowed_scopes=["\ud800", "a"])
    yield _pay(scopes=["a", "a"], allowed_scopes=["a"])
    yield _pay(scopes=["bad scope"], allowed_scopes=["a"])
    for conf in ("high", "medium", "low", "High", "", "high "):
        yield _pay(confidence=conf)
    yield _pay(confidence="x", source="x", category="x")
    for src in (
        "explicit-statement",
        "inferred",
        "user-correction",
        "Inferred",
        "capture:",
        "capture:abc",
        "capture: x ",
        "capture",
        " capture:a",
        "capture:\ud800",
    ):
        for cap in (False, True):
            yield _pay(source=src, capture=cap)
    for cat in ("fact", "user-inference", "ambient", "Fact", "", "x"):
        yield _pay(category=cat)
    yield _pay(source="x", category="x")


ALPHA = [
    "a",
    "b",
    " ",
    "\t",
    "\u3000",
    "\u0085",
    "\ud800",
    "\udfff",
    "\u00e9",
    "\U0001f600",
    "\x1c",
    "\n",
]
SCOPES = ["a", "a-b", "a:b", "A", "a--b", "-a", "a-", "1", "x:y-z", "a\n", "\u00e9", ""]
CONF = ["high", "low", "medium", "HIGH", "x", ""]
SRC = [
    "inferred",
    "explicit-statement",
    "user-correction",
    "capture:",
    "capture:ab",
    "x",
]
CAT = ["fact", "ambient", "user-inference", "Fact", ""]
MAXB = [0, -1, 1, 4, 8, 20]
MINT = [0, 1, 2, 3, -2]
MAXS = [0, 1, 2, 3]
SEED = 20261121
COUNT = 4000


def _draws(seed: int) -> Iterator[int]:
    x = seed
    while True:
        x = (x * 6364136223846793005 + 1442695040888963407) % 2**64
        yield x >> 33


def _digest() -> dict[str, Any]:
    r = _draws(SEED)

    def pick(pool: list[Any]) -> Any:
        return pool[next(r) % len(pool)]

    answers = []
    for _ in range(COUNT):
        content = "".join(pick(ALPHA) for _ in range(next(r) % 12))
        scopes = [pick(SCOPES) for _ in range(next(r) % 4)]
        a = next(r) % 3
        allowed = [pick(SCOPES) for _ in range(a)]
        kw = {
            "content": content,
            "scopes": scopes,
            "allowed_scopes": allowed,
            "confidence": pick(CONF),
            "source": pick(SRC),
            "category": pick(CAT),
            "max_content_bytes": pick(MAXB),
            "min_content_tokens": pick(MINT),
            "max_scopes_per_write": pick(MAXS),
            "capture": next(r) % 2 == 1,
        }
        try:
            p = _payload(**kw)
            answers.append(
                "ok:"
                + "|".join([p["confidence"], p["source"], p["category"], *p["scopes"]])
            )
        except ValueError as exc:
            cls = "Encode" if isinstance(exc, UnicodeEncodeError) else "Value"
            answers.append(f"{cls}:{exc}")
    data = "\n".join(answers).encode("utf-8")
    return {
        "k": "digest",
        "seed": SEED,
        "n": COUNT,
        "pools": N([ALPHA, SCOPES, CONF, SRC, CAT, MAXB, MINT, MAXS]),
        "sha": hashlib.sha256(data).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows = [*_leaves(), *_payloads(), _digest()]
    header = {"fixture": "handler_checks", "cases": len(rows)}
    lines = [dumps_line(header)] + [dumps_line(r) for r in rows]
    return {"handler_checks.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
