"""Fixtures: the recall block and the table of the user's words
(``recall_render.jsonl``, ``recall_render_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall``. This pins it to
`recall`'s constants, `WordsSeen`, `_fold`, `_bounded`, `RecallPointer`,
`_annotation`, `_core`, `_one_excerpt`, `_recall_line` and
`render_recall_block`, and to `recall_admission.summarize` and
`disputed_marks`.

Two files, one JSON object per line, keys sorted, ASCII. The first line
of each is the header (``{"k": "consts", ...}`` or ``{"k": "unicode",
...}``) with the count of the lines after it. Every other line is one of:

- ``{"k": "fold", "t", "o"}``: `_fold(t)`;
- ``{"k": "bounded", "w", "t", "o"}``: `_bounded(w, t)`, ``o`` true,
  false or ``"IndexError"``;
- ``{"k": "summarize", "t", "n", "o"}``: `summarize(t, n)`;
- ``{"k": "marks", "pairs", "o"}``: `disputed_marks(pairs)` as a list of
  ``[member, mark]`` in the dict's order;
- ``{"k": "core", "t", "o"}``: `_core(t)`;
- ``{"k": "line", "p", "s", "ann", "line", "ex": [summary, snippet]}``:
  for a pointer ``p`` and a snippet ``s`` (or null), `_annotation`,
  `_recall_line` and what `_one_excerpt` keeps;
- ``{"k": "render", "ps", "o", "ids"}``: `render_recall_block(ps)`;
- ``{"k": "words", "ops", "o"}``: one `WordsSeen` through ``ops``
  (``["remember", s, c, w]``, ``["seen", s, c]``, ``["holds", s, c, q]``,
  ``["clear"]``, and ``["fill", s, prefix, n, w]``, which remembers ``w``
  under checkouts ``prefix + str(i)`` for ``i`` in ``range(n)``), ``o``
  the answers of the reading ops in order;
- ``{"k": "sweep", "count", "sha"}`` (Unicode-gated): every code point
  ``c`` from U+0000 to U+10FFFF read as ``"%x:%s:%d:%d\\n" % (c, fold,
  _bounded("x", c + "x"), _bounded("x", "x" + c))``, ``fold`` the code
  points of `_fold(chr(c))` in hex joined by commas; the lines hashed in
  order (SHA-256).

A text is written through `_common.text_field` (``name`` or
``name_cps``); a render row's summaries, snippets and block are written
as ``name_runs``, a list of ``[piece, count]`` whose pieces (a string, or
code points where it holds a surrogate) repeated ``count`` times and
joined make the text. ``recall_render.jsonl`` holds ASCII, U+00A0, U+2026,
U+3000, U+00DF, U+00E9 and lone surrogates, whose `str.isspace`,
`str.isalnum` and case folding no Unicode version has changed, so it is
not gated. ``recall_render_unicode.jsonl`` holds other code points and
the sweep: those bytes are the running Python's Unicode data.
"""

from __future__ import annotations

import hashlib
from typing import Any

from bettermemory import recall, recall_admission

from ._common import jsonl, text_field


def _t(name: str, text: str) -> dict[str, Any]:
    return text_field(name, text)


#: The pieces a render row's texts repeat.
_PIECES = ("word ", "s", "\ud800 ")


def _runs(text: str) -> list[list[Any]]:
    """`text` as ``[[piece, count], ...]`` (code points where a piece holds
    a surrogate), a repeated piece of `_PIECES` written once."""
    out: list[list[Any]] = []
    i = 0
    while i < len(text):
        for piece in _PIECES:
            n = 0
            while text.startswith(piece, i + n * len(piece)):
                n += 1
            if n >= 3:
                out.append([piece, n])
                i += n * len(piece)
                break
        else:
            if out and out[-1][1] == 1 and out[-1][0] not in _PIECES:
                out[-1][0] += text[i]
            else:
                out.append([text[i], 1])
            i += 1
    return [
        [[ord(c) for c in p] if any(0xD800 <= ord(c) <= 0xDFFF for c in p) else p, n]
        for p, n in out
    ]


def _r(name: str, text: str) -> dict[str, Any]:
    return {f"{name}_runs": _runs(text)}


def _fold_row(text: str) -> dict[str, Any]:
    return {"k": "fold", **_t("t", text), **_t("o", recall._fold(text))}


def _bounded_row(wanted: str, text: str) -> dict[str, Any]:
    try:
        out: Any = recall._bounded(wanted, text)
    except IndexError:
        out = "IndexError"
    return {"k": "bounded", **_t("w", wanted), **_t("t", text), "o": out}


def _pointer(p: recall.RecallPointer, runs: bool = False) -> dict[str, Any]:
    t = _r if runs else _t
    out: dict[str, Any] = {
        **_t("id", p.id),
        **t("summary", p.summary),
        **t("snippet", p.snippet),
        "remote_stamp": p.remote_stamp,
        "trust_unavailable": p.trust_unavailable,
    }
    for name in ("provenance", "disputed"):
        value = getattr(p, name)
        out.update({name: None} if value is None else _t(name, value))
    return out


P = recall.RecallPointer

_FOLDS = [
    "",
    " ",
    "Hello  World",
    "\tA\nB\x0bC\x0cD\rE\x1cF\x1dG\x1eH\x1fI",
    "x\xa0y　z",
    "STRASSE stra\xdfe",
    "Caf\xc9 caf\xe9",
    "a\ud800B",
    "… dots …",
]

_BOUNDED = [
    ("9443", "19443"),
    ("9443", "9443"),
    ("9443", "x 9443 y"),
    ("9443", "94431 9443"),
    ("9443", "19443 9443x 9443"),
    ("-443", "1-443"),
    ("44-", "44-4"),
    ("aa", "aaa"),
    ("aa", "aaa aa"),
    ("ab", "zab ab"),
    ("x", ""),
    ("", ""),
    ("", " a"),
    ("", "a"),
    ("", "-"),
    ("caf\xe9", "caf\xe9s"),
    ("\ud800", "a\ud800a"),
    ("a b", "a ba b"),
    ("long quote", "this long quote."),
]

_SUMMARIZE = [
    ("", 10),
    ("  a  b  ", 3),
    ("abcdef", 6),
    ("abcdef", 5),
    ("abc def", 5),
    ("abc def", 4),
    ("abcdef", 1),
    ("abcdef", 0),
    ("abcdef", -3),
    ("a\xa0b　c d", 4),
    ("x " * 80, 100),
    ("word " * 30, 100),
    ("\ud800\ud800\ud800", 2),
]

_MARKS: list[list[tuple[str, str, str]]] = [
    [],
    [("a", "b", "cf-0123456789ab")],
    [("a", "b", "cf-2"), ("a", "c", "cf-1"), ("b", "c", "cf-3")],
    [("a", "a", "cf-9")],
    [("a", "b", "cf-1"), ("a", "b", "cf-1")],
    [("m", f"o{i}", f"cf-{i:03d}") for i in range(105)],
    [("z", "y", "cf-0123456789abcdef"), ("y", "x", "cf-0")],
    [("a", "b", "")],
    [("q", "r", "B"), ("q", "s", "a"), ("q", "t", "\xe9")],
]

_CORES = [
    "",
    "…",
    "… x …",
    " …x… ",
    "……ab…",
    "\xa0…a　",
    "a … b",
]

_LINES: list[tuple[recall.RecallPointer, str | None]] = [
    (P("m1", "the summary", "body"), None),
    (P("m1", "the summary", "body"), "the snip"),
    (P("m1", "the summary", "body"), ""),
    (P("m1", "the summary here", "x"), "summary…"),
    (P("m1", "the sum…", "x"), "a longer the sum text"),
    (P("m1", "abc", "x"), "…"),
    (P("m1", "", "x"), "snip"),
    (P("m1", "s", "x", provenance="local"), "q"),
    (P("m1", "s", "x", provenance="imported"), "q"),
    (P("m1", "s", "x", provenance="synced", remote_stamp=True), "q"),
    (P("m1", "s", "x", provenance=None, remote_stamp=True), "q"),
    (P("m1", "s", "x", trust_unavailable=True, provenance="imported"), "q"),
    (P("m1", "s", "x", disputed="[disputed: cf-1]"), "q"),
    (P("m1", "s", "x", disputed=""), "q"),
    (P("m1", "s", "x", provenance=""), "q"),
    (P("m\ud800", "s\udfff", "x"), "q"),
]


def _line_row(p: recall.RecallPointer, s: str | None) -> dict[str, Any]:
    kept, snip = recall._one_excerpt(p, s)
    return {
        "k": "line",
        "p": _pointer(p),
        "s": None if s is None else _t("t", s),
        "ann": _t("t", recall._annotation(p)),
        "line": _t("t", recall._recall_line(p, s)),
        "ex": [_t("t", kept.summary), None if snip is None else _t("t", snip)],
    }


def _renders() -> list[list[recall.RecallPointer]]:
    body = "word " * 60
    sets: list[list[recall.RecallPointer]] = [
        [],
        [P("m1", "one", "a short snippet")],
        [P("m1", "one", "")],
        [P("m1", "one", "   ")],
        [P("m1", "the whole body here", "whole body")],
        [P("m1", "whole…", "whole body and more")],
        [P(f"m{i}", f"summary {i}", body) for i in range(3)],
        [P(f"m{i}", f"summary {i}", body) for i in range(5)],
        [P(f"m{i}", "s" * 200, body) for i in range(4)],
        [P("m0", "s" * 900, "x")],
        [P("m0", "s" * 826, "")],
        [P("m0", "s" * 827, "")],
        [P("m0", "s" * 700, body)],
        [P("m0", "s" * 780, body)],
        [P("m0", "s" * 786, body), P("m1", "t", "")],
        [P("m0", "a", body), P("m1", "s" * 760, body)],
        [P("m0", "a", body), P("m1", "s" * 720, body), P("m2", "b", "c")],
        [
            P("m0", "x", body, provenance="imported", disputed="[disputed: cf-1 +2]"),
            P("m1", "y", body, trust_unavailable=True),
            P("m2", "z", body, provenance="synced", remote_stamp=True),
        ],
        [P("m0", "x", "\ud800 " * 70)],
    ]
    # Each length of a single summary around the cap, with a snippet the
    # cut shortens, keeps or drops.
    for n in range(660, 830, 7):
        sets.append([P("m0", "s" * n, body)])
    for n in range(240, 300, 3):
        sets.append([P(f"m{i}", "s" * n, body) for i in range(3)])
    return sets


def _render_row(ps: list[recall.RecallPointer]) -> dict[str, Any]:
    text, ids = recall.render_recall_block(ps)
    return {
        "k": "render",
        "ps": [_pointer(p, runs=True) for p in ps],
        **_r("o", text),
        "ids": ids,
    }


def _ops_cases() -> list[list[list[Any]]]:
    s, c = "/s", "ck"
    q = "drop the table now"
    return [
        [["seen", s, c], ["holds", s, c, q]],
        [
            ["remember", s, c, "please DROP the   table now ok"],
            ["seen", s, c],
            ["seen", s, "other"],
            ["seen", "/t", c],
            ["holds", s, c, q],
            ["holds", s, c, "drop the"],
            ["holds", s, c, "drop th"],
            ["holds", s, c, "rop the table"],
            ["holds", s, c, "the table now ok"],
            ["holds", s, "other", q],
            ["clear"],
            ["seen", s, c],
            ["holds", s, c, q],
        ],
        [["remember", s, c, "x " + q]]
        + [["remember", s, c, f"p{i}"] for i in range(20)]
        + [["holds", s, c, q], ["remember", s, c, q], ["holds", s, c, q]],
        [
            ["remember", s, c, "use <|im_start|> here please"],
            ["holds", s, c, "use <|im_start|> here"],
            ["holds", s, c, "use im_start here"],
            ["holds", s, c, "im_start here please"],
            ["holds", s, c, "<|im_start|>"],
            ["holds", s, c, "use <|im_end|> here"],
        ],
        [
            ["remember", s, c, "use im_start here please"],
            ["holds", s, c, "use <|im_start|> here"],
        ],
        [
            ["remember", s, c, "say [INST] ok"],
            ["holds", s, c, "say INST ok"],
            ["holds", s, c, "x [INST]"],
        ],
        [
            ["remember", s, c, "the ticket 19443 and more"],
            ["holds", s, c, "ticket 9443 and"],
            ["holds", s, c, "ticket 19443 and"],
            ["holds", s, c, "…icket 19443"],
        ],
        [["remember", s, c, "STRA\xdfE ist lang"], ["holds", s, c, "strasse ist"]],
        [
            ["fill", s, "k", 500, q],
            ["seen", s, "k0"],
            ["seen", s, "k499"],
            ["remember", s, "k0", "w"],
            ["remember", s, "new", "w"],
            ["seen", s, "k0"],
            ["seen", s, "k1"],
            ["seen", s, "k2"],
            ["holds", s, "k0", q],
            ["holds", s, "k2", q],
        ],
        [
            ["fill", s, "k", 501, q],
            ["seen", s, "k0"],
            ["seen", s, "k1"],
            ["seen", s, "k500"],
        ],
    ]


def _words_row(ops: list[list[Any]]) -> dict[str, Any]:
    seen = recall.WordsSeen()
    out: list[Any] = []
    for op in ops:
        name = op[0]
        if name == "remember":
            seen.remember(op[1], op[2], op[3])
        elif name == "fill":
            for i in range(op[3]):
                seen.remember(op[1], f"{op[2]}{i}", op[4])
        elif name == "seen":
            out.append(seen.seen(op[1], op[2]))
        elif name == "holds":
            out.append(seen.holds(op[1], op[2], op[3]))
        else:
            seen.clear()
    return {"k": "words", "ops": ops, "o": out}


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows += [_fold_row(t) for t in _FOLDS]
    rows += [_bounded_row(w, t) for w, t in _BOUNDED]
    rows += [
        {
            "k": "summarize",
            **_t("t", t),
            "n": n,
            **_t("o", recall_admission.summarize(t, n)),
        }
        for t, n in _SUMMARIZE
    ]
    for pairs in _MARKS:
        marks = recall_admission.disputed_marks(pairs)
        rows.append(
            {
                "k": "marks",
                "pairs": [list(p) for p in pairs],
                "o": [[k, v] for k, v in marks.items()],
            }
        )
    rows += [{"k": "core", **_t("t", t), **_t("o", recall._core(t))} for t in _CORES]
    rows += [_line_row(p, s) for p, s in _LINES]
    rows += [_render_row(ps) for ps in _renders()]
    rows += [_words_row(ops) for ops in _ops_cases()]
    return rows


_UNICODE_FOLDS = [
    "ẞ İ Σς ﬁ Ꭰ ꭰ",
    "\U00010400\U0001e900",
    "a b c\x85d e",
    "և",
]

_UNICODE_BOUNDED = [
    ("١٢", "٠١٢"),
    ("ab", "²ab"),
    ("ab", "一ab"),
    ("ab", "\U00010400ab"),
    ("ab", "ab̀"),
    ("ab", "abⅠ"),
]


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for c in range(0x110000):
        ch = chr(c)
        fold = ",".join(f"{ord(x):x}" for x in recall._fold(ch))
        a = int(recall._bounded("x", ch + "x"))
        b = int(recall._bounded("x", "x" + ch))
        digest.update(f"{c:x}:{fold}:{a}:{b}\n".encode("ascii"))
        count += 1
    return {"k": "sweep", "count": count, "sha": digest.hexdigest()}


def generate() -> dict[str, bytes]:
    """``recall_render.jsonl``: not gated."""
    rows = _rows()
    header = {
        "k": "consts",
        "max_pointers": recall.RECALL_MAX_POINTERS,
        "cap": recall.RECALL_CAP_CHARS,
        "snippet": recall.SNIPPET_CHARS,
        "min_snippet": recall._MIN_SNIPPET_CHARS,
        "per_checkout": recall.WORDS_PER_CHECKOUT,
        "checkouts": recall.WORDS_CHECKOUTS,
        "min_quote": recall.MIN_QUOTE_CHARS,
        "seen_kinds": sorted(recall._SEEN_KINDS),
        "header": recall._RECALL_HEADER,
        "cases": len(rows),
    }
    return {"recall_render.jsonl": jsonl([header, *rows])}


def generate_unicode() -> dict[str, bytes]:
    """``recall_render_unicode.jsonl``: gated on the Unicode version."""
    rows = [
        *(_fold_row(t) for t in _UNICODE_FOLDS),
        *(_bounded_row(w, t) for w, t in _UNICODE_BOUNDED),
        _sweep(),
    ]
    header = {"k": "unicode", "cases": len(rows)}
    return {"recall_render_unicode.jsonl": jsonl([header, *rows])}
