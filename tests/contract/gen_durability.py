"""Fixtures: the transient markers' helpers, unit 1 of ``durability.py``
(``durability.jsonl``, ``durability_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/durability.rs``. This pins
`canonical_marker`, `quoted_spans`, `in_quoted_span`, `_snippet_around`,
`_at_sentence_start`, `_is_titlecase_name` and `sentence_around`, and
through them `_LEGACY_SHA_MARKER_RE`, `_QUOTED_SPAN_RE`,
`_NEXT_WORD_RE`, `_SENTENCE_END_RE`, `_SENTENCE_BOUNDARY_CHARS` and
`_CONTEXT_CHARS`.

Two files, one JSON object per line, keys sorted, ASCII. The first line
of each is a header with the count of the lines after it. Every other
line is one of:

- ``{"k": "cm", "m": text, "o": text}``: `canonical_marker`;
- ``{"k": "text", "t": text, "qs": spans, "sa": [...], "ss": [...]}``:
  `quoted_spans` of the text as ``[start, end]`` pairs, and for every
  position from 0 to ``len(t) + 1``, `_at_sentence_start` (``null``
  where it raises ``IndexError``) and `sentence_around` (a text, or its
  code points where it holds a surrogate);
- ``{"k": "iq", "sp": spans, "s": start, "e": end, "o": bool}``:
  `in_quoted_span`;
- ``{"k": "sn", "t": text, "s": start, "e": end, "o": text}``:
  `_snippet_around`;
- ``{"k": "tn", "t": text, "s": start, "e": end, "o": bool or null}``:
  `_is_titlecase_name` of the match ``t[s:e]`` (``null`` where it raises
  ``IndexError``);
- ``{"k": "sweep", "lo": first, "n": count, "sha": hex}`` (Unicode file
  only): every code point ``c`` from ``lo`` for ``n``, read as
  `_at_sentence_start(c + " x", 2)`, `_is_titlecase_name(c, 0, 1)`,
  `_is_titlecase_name("x " + c + "a", 2, 3)`,
  `_is_titlecase_name("Today" + c + "Ab", 0, 5)`,
  `sentence_around(c + "." + c, 3)`, `_snippet_around(c + "a" + c + c +
  "b" + c, 0, 6)` and `quoted_spans('"' + c + '"' + c)`'s count; each
  answer as `str` gives it, the answers joined by NUL and hashed as UTF-8
  with surrogates passed (SHA-256).

A text is ``t`` (or ``m``, ``o``) or, where it holds a surrogate,
``t_cps`` (code points), as `_common.text_field` writes.

``durability.jsonl`` is not gated: its texts are ASCII and surrogates
only (and the curly double quotes U+201C and U+201D), whose `str.isspace`, `str.istitle`, general category and ``\\w``
and ``\\d`` classes no Unicode version has changed. ``durability_unicode
.jsonl`` holds texts with other code points and the sweep, and is gated
on the Unicode version. No answer depends on the Python release or the
platform.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from typing import Any

from bettermemory import durability as d

from ._common import has_surrogate, jsonl, text_field

_SPAN = re.compile(r"(?s).*")


#: Code points outside ASCII whose readings no Unicode version changes:
#: the curly double quotes the span pattern names.
_STABLE = frozenset({0x201C, 0x201D})


def _stable(text: str) -> bool:
    return all(
        ord(ch) < 0x80 or ord(ch) in _STABLE or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in text
    )


def _match(text: str, start: int, end: int) -> re.Match[str]:
    m = _SPAN.match(text, start, end)
    assert m is not None and m.start() == start and m.end() == end
    return m


def _sentence_start(text: str, pos: int) -> bool | None:
    try:
        return d._at_sentence_start(text, pos)
    except IndexError:
        return None


def _titlecase(text: str, start: int, end: int) -> bool | None:
    try:
        return d._is_titlecase_name(text, _match(text, start, end))
    except IndexError:
        return None


_MARKERS = [
    "",
    "sha:abcdef0",
    "sha:0000000",
    "sha:1234567",
    "sha:abcdef0\n",
    "sha:abcdef0\n\n",
    "sha:abcdef0\r",
    "sha:abcdef0 ",
    "\nsha:abcdef0",
    "sha:abcdeF0",
    "sha:abcdeg0",
    "sha:abcdef",
    "sha:abcdef01",
    "SHA:abcdef0",
    "sha:<commit>",
    "sha:١٢٣٤٥٦٧",
    "currently",
    "as of <date>",
    "sha:abc\ud800ef0",
]

_TEXTS = [
    "",
    "a",
    '"',
    '""',
    '"a"',
    'say "now" and "then"',
    'odd " one "two" three',
    '"a\nb"',
    '"open\n"closed"',
    "“now”",
    "“now“ later”",
    "“now\n”",
    "”back“",
    '“"mixed"”',
    '"“" “"”',
    "It ships. Then it waits! Why? Yes\nNo",
    "Python 3.13 is out. Next.",
    "v3.13.0 and 3. 13 and .5 and 5.",
    "x9.9y",
    "end.",
    "...",
    "a.b",
    "Today we ship.",
    "  \tToday",
    "- Today",
    "* Tomorrow Night",
    "(Today) [Today] 'Today' \"Today\"",
    "x: Today; y",
    "word Today",
    "a\ud800b. c",
    "\ud800.\ud800",
]

_SNIPPETS = [
    ("", 0, 0),
    ("short", 0, 5),
    ("short", 2, 3),
    ("x" * 30 + "currently" + "y" * 30, 30, 39),
    ("x" * 20 + "today" + "y" * 20, 20, 25),
    ("x" * 21 + "today" + "y" * 21, 21, 26),
    ("line one\nline two\n\nthree   spaced\t\ttabs", 9, 13),
    ("\n\n  hit  \n\n", 4, 7),
    (" " * 50, 25, 26),
    ("a" * 50, 40, 30),
    ("a" * 50, 100, 120),
    ("a" * 50, 0, 1000),
    ("ab\ud800cd", 1, 3),
    ("\x1c\x1d\x1e\x1fhit\x1f x", 4, 7),
    ("a\x0b\x0c\rb", 0, 5),
]

_TITLES = [
    ("Today we ship", 0, 5),
    ("Today We Ship", 0, 5),
    ("Today I broke it", 0, 5),
    ("Today Is here", 0, 5),
    ("Today", 0, 5),
    ("Today ", 0, 5),
    ("Today -", 0, 5),
    ("Today O'Neil", 0, 5),
    ("Today o'Neil", 0, 5),
    ("Today Rust-Lang", 0, 5),
    ("Today Rust-lang", 0, 5),
    ("Today\nNight", 0, 5),
    ("today Night", 0, 5),
    ("TODAY Night", 0, 5),
    ("the Tomorrow Night theme", 4, 12),
    ("a USA Today column", 6, 11),
    ("It ships. Today Matters", 10, 15),
    ("It ships, Today Matters", 10, 15),
    ("This Week in Rust", 0, 9),
    ("This week in Rust", 0, 9),
    ("this Week", 0, 9),
    ("THIS WEEK", 0, 9),
    ("next  month", 0, 11),
    ("Next MONTH", 0, 10),
    ("Next 2Month", 0, 11),
    ("   ", 0, 3),
    ("Today", 3, 5),
    ("ToDay Night", 0, 5),
    ("Today_ Night", 0, 6),
    ("\ud800Today Night", 1, 6),
]

_SPAN_SETS: list[tuple[tuple[tuple[int, int], ...], int, int]] = [
    ((), 0, 0),
    (((0, 5),), 0, 5),
    (((0, 5),), 1, 4),
    (((0, 5),), 0, 6),
    (((0, 5),), 5, 5),
    (((0, 5),), 6, 5),
    (((0, 3), (3, 6)), 2, 4),
    (((0, 3), (2, 6)), 2, 4),
    (((5, 1),), 3, 3),
]


def _unicode_texts() -> Iterator[str]:
    yield "x — Today"
    yield "x –Today"
    yield "→ Today"
    yield "\U0001f680 Today Night"
    yield "• Today"
    yield "été Today"
    yield "Release 3·13"
    yield "٣.٤ ok. ٣. x"
    yield "１.２"
    yield "x。y"
    yield "“quoted” — “open"
    yield " Today "


def _unicode_titles() -> Iterator[tuple[str, int, int]]:
    yield ("ǅungla", 0, 6)
    yield ("Today ǅungla", 0, 5)
    yield ("Today ⅠⅡ", 0, 5)
    yield ("Today Ⓐb", 0, 5)
    yield ("Today Été", 0, 5)
    yield ("Today Night", 0, 5)
    yield ("Today　Night", 0, 5)
    yield ("Today ’Night", 0, 5)
    yield ("x — Today Night", 4, 9)
    yield ("x → Today Night", 4, 9)
    yield ("\U0001f680 Today Night", 2, 7)
    yield ("This Week", 0, 9)
    yield ("ẞß", 0, 2)


def _unicode_snippets() -> Iterator[tuple[str, int, int]]:
    yield ("  hit　　x", 2, 5)
    yield ("é" * 30 + "hit" + "é" * 30, 30, 33)


def _str_or_cps(text: str) -> str | list[int]:
    return [ord(ch) for ch in text] if has_surrogate(text) else text


def _text_row(text: str) -> dict[str, Any]:
    n = len(text)
    return {
        "k": "text",
        **text_field("t", text),
        "qs": [list(s) for s in d.quoted_spans(text)],
        "sa": [_sentence_start(text, p) for p in range(n + 2)],
        "ss": [_str_or_cps(d.sentence_around(text, p)) for p in range(n + 2)],
    }


def _rows(unicode: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in _MARKERS:
        if _stable(m) != unicode:
            rows.append(
                {
                    "k": "cm",
                    **text_field("m", m),
                    **text_field("o", d.canonical_marker(m)),
                }
            )
    texts = list(_unicode_texts()) if unicode else _TEXTS
    for t in texts:
        rows.append(_text_row(t))
    if not unicode:
        for spans, s, e in _SPAN_SETS:
            rows.append(
                {
                    "k": "iq",
                    "sp": [list(x) for x in spans],
                    "s": s,
                    "e": e,
                    "o": d.in_quoted_span(spans, s, e),
                }
            )
    snippets = list(_unicode_snippets()) if unicode else _SNIPPETS
    for t, s, e in snippets:
        rows.append(
            {
                "k": "sn",
                **text_field("t", t),
                "s": s,
                "e": e,
                **text_field("o", d._snippet_around(t, s, e)),
            }
        )
    titles = list(_unicode_titles()) if unicode else _TITLES
    for t, s, e in titles:
        rows.append(
            {"k": "tn", **text_field("t", t), "s": s, "e": e, "o": _titlecase(t, s, e)}
        )
    for r in rows:
        for key in ("t", "m"):
            if key in r:
                assert _stable(r[key]) != unicode, r
    return rows


def _answers(c: str) -> list[str]:
    return [
        str(_sentence_start(c + " x", 2)),
        str(_titlecase(c, 0, 1)),
        str(_titlecase("x " + c + "a", 2, 3)),
        str(_titlecase("Today" + c + "Ab", 0, 5)),
        d.sentence_around(c + "." + c, 3),
        d._snippet_around(c + "a" + c + c + "b" + c, 0, 6),
        str(len(d.quoted_spans('"' + c + '"' + c))),
    ]


_PLANE = 0x10000


def _sweep() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lo in range(0, 0x110000, _PLANE):
        parts = [a for cp in range(lo, lo + _PLANE) for a in _answers(chr(cp))]
        blob = "\0".join(parts).encode("utf-8", "surrogatepass")
        rows.append(
            {
                "k": "sweep",
                "lo": lo,
                "n": _PLANE,
                "sha": hashlib.sha256(blob).hexdigest(),
            }
        )
    return rows


def generate() -> dict[str, bytes]:
    """``durability.jsonl``: not gated."""
    rows = _rows(unicode=False)
    return {"durability.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}


def generate_unicode() -> dict[str, bytes]:
    """``durability_unicode.jsonl``: gated on the Unicode version."""
    rows = [*_rows(unicode=True), *_sweep()]
    return {
        "durability_unicode.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }
