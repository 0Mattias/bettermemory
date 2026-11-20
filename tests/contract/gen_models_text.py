"""Fixtures: the text helpers of `models` (``models_text.jsonl``,
``models_text_unicode.jsonl``, ``models_text_platform.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/models_text.rs``. This pins it to
`models`' `make_slug`, `build_filename`, `_word_ending_at`,
`looks_truncated`, `first_summary_line`, `snippet_for`, `snippet_window`
and `_truncate_at_word`, and through them `_SLUG_RE`, `_LEADING_DATE_RE`,
`_SENTENCE_END_RE`, `_ABBREVIATIONS` and `_BODY_TERMINALS`.

Each line after the header is one case, by ``k``, with the text ``t``
and the answer ``r``:

- ``slug``: `make_slug(t, w, c)`;
- ``file``: `build_filename(datetime(*d), t)`;
- ``word``: `_word_ending_at(t, e)`, or ``{"err": class}``;
- ``trunc``: `looks_truncated(t)`;
- ``summ``: `first_summary_line(t, m)`;
- ``snip``: `snippet_for(t, m)`;
- ``win``: `snippet_window(t, s, m)`;
- ``taw``: `_truncate_at_word(t, m)`;
- ``sweep``: a digest row (see below).

A text is a string, ``{"cps": [...]}`` (a lone surrogate) or
``{"rep": [[piece, count], ...]}`` (the pieces repeated and joined). An
answer longer than 120 characters is ``{"n": length, "sha": ...}``, the
SHA-256 of its code points as 4-byte big-endian words.

A digest row names a ``rule``, a ``count`` of inputs and the ``sha`` of
the answers in order: each answer as code points (a bool as one code
point, 1 or 0), 4-byte big-endian, then the word 0xFFFFFFFF.

- ``random`` (``models_text.jsonl``): ``count`` inputs from splitmix64
  seeded with ``seed``, as `_random_case` draws them; per input the
  answers of `make_slug`, `looks_truncated`, `first_summary_line`,
  `snippet_for`, `snippet_window` and `_truncate_at_word`.
- ``every`` (``models_text_unicode.jsonl``): every code point ``ch``; the
  answers of `_every_answers`.

Texts past ASCII go in ``_unicode`` (`str.strip`, `str.lower`,
`str.isalnum` and `re`'s `\\s` and `\\d` read the Unicode data). A year
before 1000 goes in ``_platform``: `strftime`'s ``%Y`` pads it on macOS
and not on every C library.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from bettermemory import models as m

from ._common import jsonl

_MASK = (1 << 64) - 1
SEED = 20261119
RANDOM_COUNT = 6000
_PIECES = [
    "a", "b", "word", "Zeta", " ", " ", "  ", "\n", "\t", "\r", ".", ". ", "!", "?",
    "-", "--", ",", ";", ":", "_", "/", "|", "0", "7", "2024-05-07", "2026-01-02T10:30",
    "T12:30:45Z", "+05:30", "-0800", "e.g.", "i.e.", "Mr.", "U.S.", "etc.", "vs.",
    "x" * 45, "user.name", "1.0.2", ")", "\"", "`", "*",
]  # fmt: skip


class _Rng:
    """splitmix64."""

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return self.next() % n


def _random_case(rng: _Rng) -> tuple[str, int, int, int]:
    """A text of 0 to 79 pieces, then max_words in -3..9, max_chars in
    -5..244 and start in -2..297, drawn in that order."""
    pieces = rng.below(80)
    text = "".join(_PIECES[rng.below(len(_PIECES))] for _ in range(pieces))
    return text, rng.below(13) - 3, rng.below(250) - 5, rng.below(300) - 2


def _feed(h: Any, answers: Iterable[str | bool]) -> None:
    for a in answers:
        if isinstance(a, bool):
            h.update(struct.pack(">I", int(a)))
        else:
            h.update(b"".join(struct.pack(">I", ord(c)) for c in a))
        h.update(b"\xff\xff\xff\xff")


def _random_answers(text: str, w: int, c: int, s: int) -> list[str | bool]:
    return [
        m.make_slug(text, w, c),
        m.looks_truncated(text),
        m.first_summary_line(text, c),
        m.snippet_for(text, c),
        m.snippet_window(text, s, c),
        m._truncate_at_word(text, c),
    ]


def _every_answers(ch: str) -> list[str | bool]:
    return [
        m.make_slug("2024-01-02" + ch + "ab " + ch + "cd"),
        m.make_slug("2024-01-0" + ch + " x"),
        m.make_slug(ch + "x\ny" + ch),
        m.looks_truncated("ab" + ch),
        m.first_summary_line("one vs" + ch + ". two"),
        m.first_summary_line("ab." + ch + "cd"),
        m.snippet_for(ch + "word " * 50 + ch),
    ]


def _random_row() -> dict[str, Any]:
    rng = _Rng(SEED)
    h = hashlib.sha256()
    for _ in range(RANDOM_COUNT):
        _feed(h, _random_answers(*_random_case(rng)))
    return {
        "k": "sweep",
        "rule": "random",
        "seed": SEED,
        "count": RANDOM_COUNT,
        "sha": h.hexdigest(),
    }


def _every_row() -> dict[str, Any]:
    h = hashlib.sha256()
    for cp in range(0x110000):
        _feed(h, _every_answers(chr(cp)))
    return {"k": "sweep", "rule": "every", "count": 0x110000, "sha": h.hexdigest()}


_Text = str | tuple[tuple[str, int], ...]


def _built(t: _Text) -> str:
    return t if isinstance(t, str) else "".join(p * n for p, n in t)


def _spelled(t: _Text) -> Any:
    if not isinstance(t, str):
        return {"rep": [[p, n] for p, n in t]}
    if any(0xD800 <= ord(c) <= 0xDFFF for c in t):
        return {"cps": [ord(c) for c in t]}
    return t


def _out(a: Any) -> Any:
    if isinstance(a, str):
        if len(a) > 120:
            h = hashlib.sha256(b"".join(struct.pack(">I", ord(c)) for c in a))
            return {"n": len(a), "sha": h.hexdigest()}
        return _spelled(a)
    return a


class _Cases:
    def __init__(self) -> None:
        self.plain: list[dict[str, Any]] = []
        self.unicode: list[dict[str, Any]] = []
        self.platform: list[dict[str, Any]] = []

    def _put(self, record: dict[str, Any], t: _Text) -> None:
        text = _built(t)
        record["t"] = _spelled(t)
        (self.plain if text.isascii() else self.unicode).append(record)

    def slug(self, t: _Text, w: int = 6, c: int = 60) -> None:
        self._put(
            {"k": "slug", "w": w, "c": c, "r": _out(m.make_slug(_built(t), w, c))}, t
        )

    def file(self, d: tuple[int, int, int], t: str) -> None:
        r = m.build_filename(datetime(*d), t)
        record = {"k": "file", "d": list(d), "r": _out(r)}
        if d[0] < 1000:
            record["t"] = _spelled(t)
            self.platform.append(record)
        else:
            self._put(record, t)

    def word(self, t: _Text, e: int) -> None:
        try:
            r: Any = _out(m._word_ending_at(_built(t), e))
        except IndexError:
            r = {"err": "IndexError"}
        self._put({"k": "word", "e": e, "r": r}, t)

    def trunc(self, t: _Text) -> None:
        self._put({"k": "trunc", "r": m.looks_truncated(_built(t))}, t)

    def summ(self, t: _Text, mc: int = 80) -> None:
        self._put(
            {"k": "summ", "m": mc, "r": _out(m.first_summary_line(_built(t), mc))}, t
        )

    def snip(self, t: _Text, mc: int = 200) -> None:
        self._put({"k": "snip", "m": mc, "r": _out(m.snippet_for(_built(t), mc))}, t)

    def win(self, t: _Text, s: int, mc: int = 200) -> None:
        r = m.snippet_window(_built(t), s, mc)
        self._put({"k": "win", "s": s, "m": mc, "r": _out(r)}, t)

    def taw(self, t: _Text, mc: int) -> None:
        self._put(
            {"k": "taw", "m": mc, "r": _out(m._truncate_at_word(_built(t), mc))}, t
        )


_BIG = (1 << 63) - 1
_SMALL = -(1 << 63)


def _cases() -> _Cases:
    c = _Cases()
    # make_slug: the first line, the leading date, the split, the bounds.
    for t in [
        "",
        "   ",
        "Hello World",
        "  Tightened the MVP scope today  ",
        "Title line\nsecond line words",
        "\n\nlead\nnext",
        "line\rnext",
        "line\x0bnext",
        "line\x1cnext",
        "2026-05-07",
        "2026-05-07 tightened the mvp",
        "2026-05-07T10:30 notes",
        "2026-05-07t10:30:15Z notes",
        "2026-05-07 10:30:15+02:00 - notes",
        "2026-05-07T10:30+0200 notes",
        "2026-05-07T10:30+02:0 notes",
        "2026-05-07T10:30+02 notes",
        "2026-05-07T10:3 notes",
        "2026-05-07T10:30: notes",
        "2026-05-07T10:30:1 notes",
        "2026-05-07T10:30-08:00|/.,;:_- notes",
        "2026-05-07x notes",
        "2026-5-07 notes",
        "x 2026-05-07 notes",
        "2026-05-07 2026-05-08 twice",
        "2026-05-0712 joined",
        "--- dashes --- only ---",
        "one two three four five six seven eight",
        "under_score and CamelCase 42 x9",
        "!!!",
        "a" * 70,
        "abc " * 20,
    ]:
        c.slug(t)
    for w, mc in [(0, 60), (1, 60), (-1, 60), (-7, 60), (100, 60), (6, 0), (6, 1),
                  (6, 4), (6, 5), (6, -1), (6, -3), (6, -100), (_BIG, _BIG),
                  (_SMALL, _SMALL), (2, 3)]:  # fmt: skip
        c.slug("ab cd ef gh ij kl mn", w, mc)
    c.slug("ab-cd", 1, 3)
    c.slug("Ünïcödé façade naïve", 6, 60)
    c.slug("İstanbul KELVIN K", 6, 60)
    c.slug("٠١٢٣-01-02 arabic digits")
    c.slug("2024-01-02　ideographic space")
    c.slug("2024-01-02 line sep")
    c.slug("x y")
    c.slug(" padded ")
    c.slug("\ud800 lone")
    # build_filename.
    for d, t in [((2026, 5, 7), "tightened-the-mvp"), ((1000, 1, 1), "x"),
                 ((9999, 12, 31), ""), ((999, 1, 2), "y"), ((1, 1, 1), "memory"),
                 ((2026, 5, 7), "naïve")]:  # fmt: skip
        c.file(d, t)
    # _word_ending_at.
    for t, e in [("e.g. python", 3), ("e.g. python", 0), ("e.g. python", -1),
                 ("e.g. python", 11), ("e.g. python", 12), ("e.g. python", 100),
                 ("a, b.", 4), ("`x`.", 3), ("U.S.A.", 5), ("", 0), ("", 1),
                 ("abc", _BIG), ("abc", _SMALL), ("naïve.", 5), ("x².", 2),
                 ("xⅧ.", 2)]:  # fmt: skip
        c.word(t, e)
    # looks_truncated.
    for t in ["", "  \n", "Done.", "Done", "(Hak5", "list:", "quote\"", "x'",
              "`code`", "»", "a]", "a}", "a>", "*", "_", "|", "end!  \n",
              "end?", "a;", "a)", "a,", "a-", "a”", "a。"]:  # fmt: skip
        c.trunc(t)
    # first_summary_line.
    for t in [
        "",
        "  One sentence. Two sentence.",
        "user.name is set. Then more.",
        "git config --global user.name x",
        "Version 1.0.2 shipped. Next.",
        "e.g. python works. Next.",
        "E.G. upper works. Next.",
        "Mr. Smith left. Next.",
        "Dr.Who is odd. Next.",
        "Use U.S. spelling here. Next.",
        "a.k.a. the thing. Next.",
        "approx. ten. Next.",
        "No. it is. Next.",
        "Done! Next.",
        "Really? Next.",
        "Wait... then. Next.",
        ". leading dot then words",
        "end at the end.",
        "no boundary at all here",
        "multi\nline. body",
        "tab\tafter.\tnext",
        "cr.\rnext",
        "x" * 81,
        "x" * 80,
        "word " * 20 + ". tail",
        "short. " + "y" * 100,
        "Q?",
        "`x`. Next",
        "etc.) next. Then",
        "vs. them. Next",
        "e.g. nbsp. Next",
        "ok.　next",
        "Ünïcödé. Next",
        "naïve. Next",
        "Ⅷ. Next",
    ]:
        c.summ(t)
    for t, mc in [("abc. def", 3), ("abc. def", 2), ("abc def ghi", 5),
                  ("abc def ghi", 0), ("abc def ghi", -1), ("abcdefghi", -2),
                  ("a" * 50, 39), ("a" * 50, 40), ("a" * 50, 41), ("ab cd", _BIG),
                  ("ab cd", _SMALL)]:  # fmt: skip
        c.summ(t, mc)
    # snippet_for.
    snip_texts: list[_Text] = [
        "",
        "  short body  ",
        "x" * 200,
        "x" * 201,
        (("word ", 40), ("tail", 1)),
        (("w", 159), (" ", 1), ("z", 60)),
        (("w", 160), (" ", 1), ("z", 60)),
        (("w", 160), ("\n", 1), ("z", 60)),
        (("w", 160), ("\t", 1), ("z", 60)),
        (("w", 160), ("\r", 1), ("z", 60)),
        (("w", 160), (" ", 1), ("z", 60)),
        (("w", 150), (" ,;:.- ", 1), ("z", 60)),
        (("w", 198), (" .", 1), ("z", 10)),
        (("-", 250),),
        (("é", 250),),
    ]
    for st in snip_texts:
        c.snip(st)
    for t, mc in [("ab cd ef", 0), ("ab cd ef", 1), ("ab cd ef", 5), ("ab cd ef", -1),
                  ("abcdef", -1), ("abcdef", 3), ("abcdefgh", 39), ("ab cd", _SMALL),
                  ("ab cd", _BIG)]:  # fmt: skip
        c.snip(t, mc)
    # snippet_window.
    long_t = (("word ", 60),)
    for s, mc in [(0, 200), (-5, 200), (5, 200), (100, 200), (103, 200), (104, 200),
                  (299, 200), (300, 200), (400, 200), (_BIG, 200), (_SMALL, 200),
                  (5, 3), (5, 2), (5, 0), (5, -10), (5, 20), (5, 43), (5, 44),
                  (0, 10), (5, _BIG), (5, _SMALL)]:  # fmt: skip
        c.win(long_t, s, mc)
    c.win("  lead  mid body tail  ", 4, 200)
    c.win("abcdefghijklmnop", 3, 8)
    c.win("ab　cd ef", 2, 6)
    # _truncate_at_word.
    for t, mc in [("abc def", 5), ("abc def", 4), ("abc def", 3), ("abcdef", 3),
                  ("abcdef", 0), ("abcdef", -2), ("a b", 39), ("ab", 39), ("ab", 40),
                  ("abc", 41), (" abc", 2), ("a.,;:- b", 6), ("ab -", 4),
                  ("x" * 50 + " y", 60), ("x" * 50 + " " + "y" * 50, 91),
                  ("x" * 50 + " " + "y" * 50, 92), ("", 0), ("", -1)]:  # fmt: skip
        c.taw(t, mc)
    return c


def _header(cases: list[dict[str, Any]], full: bool) -> dict[str, Any]:
    header: dict[str, Any] = {"k": "header", "cases": len(cases)}
    if full:
        header["abbreviations"] = sorted(m._ABBREVIATIONS)
        header["terminals"] = "".join(sorted(m._BODY_TERMINALS))
    return header


def generate() -> dict[str, bytes]:
    """``models_text.jsonl``: not gated."""
    cases = [*_cases().plain, _random_row()]
    return {"models_text.jsonl": jsonl([_header(cases, True), *cases])}


def generate_unicode() -> dict[str, bytes]:
    """``models_text_unicode.jsonl``: gated on the Unicode version."""
    cases = [*_cases().unicode, _every_row()]
    return {"models_text_unicode.jsonl": jsonl([_header(cases, False), *cases])}


def generate_platform() -> dict[str, bytes]:
    """``models_text_platform.jsonl``: gated on the platform and Python."""
    cases = _cases().platform
    return {"models_text_platform.jsonl": jsonl([_header(cases, False), *cases])}
