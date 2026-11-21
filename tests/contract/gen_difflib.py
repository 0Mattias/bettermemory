"""Fixtures: `history._body_diff` and `difflib.unified_diff`
(``difflib.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/difflib.rs``. This pins it to
`history._body_diff`, and through it `str.splitlines` and CPython
3.13.13's `difflib.unified_diff` with ``lineterm=""`` and no dates
(`SequenceMatcher` with autojunk: `__chain_b`, `find_longest_match`,
`get_matching_blocks`, `get_opcodes`, `get_grouped_opcodes(3)`,
`_format_range_unified`).

Every text is a list of code points, so a lone surrogate survives JSON.
Rows:

- ``body``: `_body_diff(before, after, old=old, new=new)`, ``before``
  null for None;
- ``lines``: ``"\\n".join(unified_diff(a, b, fromfile=old, tofile=new,
  lineterm=""))`` over line lists ``a`` and ``b``;
- ``split``: `str.splitlines` of ``t``;
- ``bulk``: seeded random line lists (the rule below, `_cases`), the count
  and the SHA-256 of each answer's UTF-8 followed by a NUL, in order.

The random rule is xorshift64* (`_Rng`). A ``near`` sweep makes ``b`` of
``n`` lines (199, 200 and 201: around autojunk's 200) from 1,000 distinct
lines plus the line ``P`` at ``c`` places (``n // 100 + 1`` and
``n // 100 + 2``: at and past the popular bound), and ``a`` from ``b``
with lines dropped, replaced and inserted. A ``small`` sweep makes both
lists of 0 to 12 lines over four distinct lines.

The file is not gated: `difflib.unified_diff`, `SequenceMatcher` and
`str.splitlines` answer these rows alike under 3.11 to 3.14 on every
system, and no row holds a path, an errno or a Unicode table's answer.
"""

from __future__ import annotations

import difflib
import hashlib
from typing import Any

from bettermemory import history

from ._common import jsonl

MASK = (1 << 64) - 1


def _cps(text: str) -> list[int]:
    return [ord(ch) for ch in text]


def _diff(a: list[str], b: list[str], old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(a, b, fromfile=old, tofile=new, lineterm=""))


class _Rng:
    """xorshift64*."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self) -> int:
        x = self.x
        x ^= x >> 12
        x = (x ^ (x << 25)) & MASK
        x ^= x >> 27
        self.x = x
        return (x * 0x2545F4914F6CDD1D) & MASK

    def below(self, n: int) -> int:
        return (self.next() >> 33) % n


def _near(rng: _Rng, n: int, c: int) -> tuple[list[str], list[str]]:
    b = [f"x{rng.below(1000)}" for _ in range(n)]
    for _ in range(c):
        b[rng.below(n)] = "P"
    while b.count("P") < c:
        i = rng.below(n)
        if b[i] != "P":
            b[i] = "P"
    a: list[str] = []
    for line in b:
        r = rng.below(16)
        if r < 2:
            continue
        if r < 4:
            a.append(f"y{rng.below(1000)}")
            continue
        if r == 4:
            a.append("P")
        a.append(line)
    return a, b


def _small(rng: _Rng) -> tuple[list[str], list[str]]:
    a = ["abcd"[rng.below(4)] for _ in range(rng.below(13))]
    b = ["abcd"[rng.below(4)] for _ in range(rng.below(13))]
    return a, b


def _cases(kind: str, seed: int, count: int, n: int = 0, c: int = 0) -> Any:
    rng = _Rng(seed)
    blob = hashlib.sha256()
    for _ in range(count):
        a, b = _near(rng, n, c) if kind == "near" else _small(rng)
        blob.update(_diff(a, b, "a", "b").encode("utf-8") + b"\0")
    return {
        "k": "bulk",
        "kind": kind,
        "seed": seed,
        "count": count,
        "n": n,
        "c": c,
        "sha": blob.hexdigest(),
    }


BOUNDARIES = [
    "\n",
    "\r",
    "\r\n",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    " ",
    " ",
]
NOT_BOUNDARIES = ["\x1f", "\t", "\x00", "\u0085̀"[1:], "​", "\n\r"]

_CTX = [f"l{i}" for i in range(20)]


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def _edit(lines: list[str], **at: str) -> list[str]:
    out = list(lines)
    for k, v in sorted(at.items(), key=lambda kv: -int(kv[0][1:])):
        i = int(k[1:])
        if v == "":
            del out[i]
        else:
            out[i] = v
    return out


def _bodies() -> list[tuple[str | None, str, str, str]]:
    ctx = _join(_CTX)
    out: list[tuple[str | None, str, str, str]] = [
        (None, "", "a", "b"),
        (None, "x", "a", "b"),
        (None, "x\ny\n", "old", "new"),
        ("", "", "a", "b"),
        ("", "x", "a", "b"),
        ("x", "", "a", "b"),
        ("x", "x", "a", "b"),
        ("x", "y", "a", "b"),
        ("x\n", "x", "a", "b"),
        ("x", "x\n", "a", "b"),
        ("x\n\n", "x", "a", "b"),
        ("\n", "", "a", "b"),
        ("a\r\nb", "a\nb", "a", "b"),
        ("a\r\r\nb", "a\nb", "a", "b"),
        ("a\nb\nc", "a\nB\nc", "", ""),
        ("a\nb\nc", "a\nB\nc", "x\ty", "z w"),
        ("a\nb", "b\na", "a", "b"),
        ("x\ny\nx\ny", "y\nx", "a", "b"),
        ("a\na\na", "a", "a", "b"),
        ("a", "a\na\na", "a", "b"),
        ("\ud800\nb", "\ud800\nc", "\udfff", "n"),
        ("\udc80", "😀", "a", "b"),
        (ctx, ctx, "a", "b"),
        (ctx, _join(_edit(_CTX, l10="L")), "a", "b"),
        (ctx, _join(_edit(_CTX, l0="L")), "a", "b"),
        (ctx, _join(_edit(_CTX, l19="L")), "a", "b"),
        (ctx, _join(_edit(_CTX, l3="L", l9="M")), "a", "b"),
        (ctx, _join(_edit(_CTX, l3="L", l10="M")), "a", "b"),
        (ctx, _join(_edit(_CTX, l3="L", l11="M")), "a", "b"),
        (ctx, _join(_edit(_CTX, l5="")), "a", "b"),
        (ctx, _join(["new", *_CTX]), "a", "b"),
        (ctx, _join([*_CTX, "new"]), "a", "b"),
        (ctx, "", "a", "b"),
        ("", ctx, "a", "b"),
        (ctx, _join(_CTX[::-1]), "a", "b"),
    ]
    for sep in BOUNDARIES + NOT_BOUNDARIES:
        out.append((f"a{sep}b", "a\nb", "a", "b"))
        out.append((f"a{sep}", f"{sep}a", "a", "b"))
    return out


def _line_pairs() -> list[tuple[list[str], list[str]]]:
    rng = _Rng(7)
    near = _near(rng, 200, 3)
    pop = _near(rng, 200, 4)
    return [
        (
            [],
            [],
        ),
        ([""], []),
        ([], [""]),
        ([""], [""]),
        (["a\nb"], ["a\nc"]),
        (["", ""], [""]),
        (["q"] * 3 + ["r"], ["r"] + ["q"] * 3),
        near,
        pop,
        (["P"] * 4 + ["u"] * 196, ["u"] * 196 + ["P"] * 4),
    ]


def generate() -> dict[str, bytes]:
    """``difflib.jsonl``: not gated."""
    rows: list[Any] = []
    for before, after, old, new in _bodies():
        rows.append(
            {
                "k": "body",
                "before": None if before is None else _cps(before),
                "after": _cps(after),
                "old": _cps(old),
                "new": _cps(new),
                "out": _cps(history._body_diff(before, after, old=old, new=new)),
            }
        )
    for a, b in _line_pairs():
        rows.append(
            {
                "k": "lines",
                "a": [_cps(x) for x in a],
                "b": [_cps(x) for x in b],
                "out": _cps(_diff(a, b, "o", "n")),
            }
        )
    texts = [
        "",
        "\n",
        "\n\n",
        "a",
        "a\n",
        "a\r\n\r\n",
        "\r\r\n\n",
        "\ud800\x85x",
        *(f"a{s}b{s}" for s in BOUNDARIES + NOT_BOUNDARIES),
    ]
    for t in texts:
        rows.append(
            {"k": "split", "t": _cps(t), "out": [_cps(x) for x in t.splitlines()]}
        )
    for n in (199, 200, 201):
        for c in (n // 100 + 1, n // 100 + 2):
            rows.append(_cases("near", 1000 * n + c, 40, n, c))
    rows.append(_cases("small", 99, 3000))
    header = {
        "k": "header",
        "cases": len(rows),
    }
    return {"difflib.jsonl": jsonl([header, *rows])}
