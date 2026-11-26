"""Fixtures: the transient markers' helpers, unit 1 of ``durability.py``
(``durability.jsonl``, ``durability_unicode.jsonl``), the transient
markers, unit 2 (``durability_markers.jsonl``,
``durability_markers_unicode.jsonl``), and the capture rules, unit 3
(``durability_capture.jsonl``, ``durability_capture_unicode.jsonl``); the
files of units 2 and 3 are described where their rows are made below.

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


# --- unit 3: the capture rules ----------------------------------------------
#
# ``durability_capture.jsonl`` (not gated) and ``durability_capture_unicode
# .jsonl`` (gated on the Unicode version) pin `find_capture_rule_hits`,
# `_unanchored_test_total` and `CaptureRuleHit.to_dict`, and through them
# every capture pattern and `_first_unquoted`. Rows, after a header with the
# count of the lines after it:
#
# - ``{"k": "pat", "id": name, "p": text, "f": flags}`` (not gated only):
#   each pattern's text and `re.Pattern.flags`, by its inventory id;
# - ``{"k": "hits", "t": text, "o": hits}``: `find_capture_rule_hits`, each
#   hit as ``{"rule": n, "name": name, "s": snippet}`` (``s_cps`` where the
#   snippet holds a surrogate), which is `to_dict` with the snippet's key
#   shortened;
# - ``{"k": "tt", "t": sentence, "o": bool}``: `_unanchored_test_total`;
# - ``{"k": "rep", "head": text, "unit": text, "n": count, "tail": text,
#   "o": hits}``: the hostile bodies, ``head + unit * n + tail``, with the
#   reference's hits (their timing is the robustness test's);
# - ``{"k": "sweep", ...}`` (not gated only): ``count`` bodies drawn from
#   ``words`` and ``seps`` by the LCG of `_lcg` from ``seed``: per body,
#   ``1 + (s >> 33) % most`` words, each followed by a separator, each pick
#   ``(s >> 33) % len(list)`` after one LCG step. The answer of a body is
#   each hit as ``f"{rule}|{name}|{snippet}\n"``, then NUL; ``sha`` is the
#   SHA-256 of the answers in order, as UTF-8; ``rules`` counts the bodies
#   each rule name fired on.
#
# The ungated file's texts are ASCII, the curly quotes U+2018, U+2019,
# U+201C and U+201D (literals of the patterns and the span reader, of
# category ``Pi`` or ``Pf`` and outside ``\w``, ``\s`` and ``\d`` in every
# Unicode version) and lone surrogates. The Unicode file holds the case
# folding, ``\b``, ``\w``, ``\s`` and ``\d`` past ASCII. No answer follows
# the Python release or the platform.

_CAPTURE_PATTERN_IDS: list[tuple[str, re.Pattern[str]]] = [
    ("_USERS_OPENER_RE", d._USERS_OPENER_RE),
    ("_ASSISTANT_SAID_RE", d._ASSISTANT_SAID_RE),
    ("_ASSISTANTS_ANSWER_RE", d._ASSISTANTS_ANSWER_RE),
    ("_TODO_HEADING_RE", d._TODO_HEADING_RE),
    *[
        (f"_TODO_PATTERNS{'' if i == 0 else f'#{i + 1}'}", p)
        for i, p in enumerate(d._TODO_PATTERNS)
    ],
    ("_IMPERATIVE_OPENER_RE", d._IMPERATIVE_OPENER_RE),
    *[
        (f"_RELATIVE_DATE_PATTERNS{'' if i == 0 else f'#{i + 1}'}", p)
        for i, p in enumerate(d._RELATIVE_DATE_PATTERNS)
    ],
    ("_ISO_DATE_RE", d._ISO_DATE_RE),
    ("_COMMIT_HASH_RE", d._COMMIT_HASH_RE),
    ("_DRIFTING_COUNT_RE", d._DRIFTING_COUNT_RE),
    ("_TEST_TOTAL_RE", d._TEST_TOTAL_RE),
    ("_SUITE_WORD_RE", d._SUITE_WORD_RE),
]

_CAPTURE_STABLE = frozenset({0x2018, 0x2019, 0x201C, 0x201D})


def _capture_stable(text: str) -> bool:
    return all(
        ord(ch) < 0x80 or ord(ch) in _CAPTURE_STABLE or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in text
    )


_PAD = "Some words lead in here, " * 2
_LONG_TAIL = " and the rest of the line runs on for a while"

_CAPTURE_TEXTS: list[str] = [
    "",
    "A plain durable fact about the hook.",
    # Rule 3: the user's opener, at a sentence start only.
    "The user's project uses Rust.",
    "The user’s project uses Rust.",
    "the user's project uses Rust.",
    "THE USER'S project",
    "The users project",
    "The user'sx project",
    "XThe user's project",
    "Notes: The user's project",
    "Notes:  \t The user's project",
    "- The user's project",
    "(The user's project)",
    "[The user's project]",
    "'The user's project'",
    "We saw The user's project",
    "We saw The user's project. The user's laptop",
    "We saw The user's project, and the assistant said so",
    "\"The user's view\" is quoted. The user's laptop",
    "“The user's view” is quoted",
    # Rule 3: what the assistant said.
    "The assistant said to use the cache.",
    "the assistant then said so",
    "the assistant then quickly said so",
    "the assistant then very quickly said so",
    "the assistant pointed out the flaw",
    "the assistant pointed  out the flaw",
    "the assistant saidx so",
    "the assistants said so",
    "THE ASSISTANT SAID so",
    "the assistant's-own reply said",
    "the assistant o’brien-like said",
    "the assistant\nsaid so",
    "the assistant's recommendation was Rust",
    "the assistant’s answers were Rust",
    "the assistant's summarys",
    "the assistant's view's",
    "the assistant 's view",
    "the user's x and the assistant said y",
    "The user's x and the assistant said y",
    "x the assistant said so. The user's y",
    "We saw The user's x; the assistant's advice was y",
    '"the assistant said" and the assistant said',
    "the assistant said “the assistant said” twice",
    # Rule 1: to-dos.
    "TODO: fix the hook test in the daemon before release",
    "TODO fix the hook test in the daemon before release",
    "todo: fix the hook",
    "the TODO tag",
    "TODOs remain",
    "## Next steps:\n- ship it",
    "Intro line\nAction items: ship it",
    "Intro line Action items: ship it",
    "**Follow-ups**: ship it",
    "> to-do : ship it",
    "__Remaining work__ :ship",
    "open item: one",
    "Open items\t:\tone",
    "Followups: one",
    "next step - one",
    "We still need to fix it",
    "We still needs to fix it",
    "It is still to decide",
    "It is still to 9",
    "It is still  to decide",
    "STILL TO DECIDE",
    "It is yet to be decided",
    "It is yet to be 9",
    "nothing left to do here",
    "nothing left to doing",
    "- [ ] write the docs",
    "  * [ ] write the docs",
    "\t+ [ ] write the docs",
    "x\n- [ ] write the docs",
    "x - [ ] write the docs",
    "- [x] wrote the docs",
    "-  [ ] two spaces",
    "We continue in the next session",
    "Next time we try harder",
    "when we resume the port",
    "the next timer",
    # Rule 1: the imperative opener.
    "Fix the hook test",
    "fix  the hook test",
    "Fix ownership lies with the team",
    "Release 8.0.0 shipped",
    "Release the build",
    "**Fix the** hook",
    "> Release the build",
    "  \tRun it again",
    "#- *Run it again",
    "Clean up the logs",
    "Clean  up the logs",
    "Cleanup the logs",
    "Fix them all",
    "Fix themselves",
    "Port its parser",
    "x\nFix the hook",
    " x Fix the hook",
    "Fix the hook. TODO later",
    "Fix the hook. Then the next session",
    "“Fix the hook” is an order",
    '"Fix the hook" said nobody',
    # Rule 1: drifting counts.
    "The repo has 1,200 stars.",
    "The repo has 1,200 stars as of 2025-01-01.",
    "The repo had 1.2k GitHub stars on 2025-01-01. It has 30 forks.",
    "It has 5 stars. Checked 2025-01-01.",
    "three followers",
    "ten open issues",
    "10 open  issues",
    "5 watchers",
    "eleven stars",
    "5stars",
    "5 GitHub  stars",
    "5 github stars",
    "1.2.3 downloads",
    "2,5k forks",
    "x5 stars",
    "1 star 2025-1-01",
    "1 star 20250-01-01",
    "1 star on 2025-01-01x",
    "Version 3.13 has 5 stars 2025-01-01",
    "v1.2 5 stars. 2025-01-01",
    'The repo has "5 stars" today',
    '"5 stars" and 7 forks',
    # Rule 1: test totals.
    "The suite: 1,024 passed.",
    "1,024 passed.",
    "Tests: 1024 passed at abc1234.",
    "Tests: 1024 passed at 1234567.",
    "Tests: 1024 passed at abcdefa.",
    "Tests: 1024 passed at ABC1234.",
    "Tests: 1024 passed at deadbeef.",
    "Tests: 1024 passed at deadbee1.",
    "Tests: 1024 passed at abc123.",
    "Tests: 5 failed at " + "a1" * 20 + ".",
    "Tests: 5 failed at " + "a1" * 20 + "b.",
    "Tests: 5 failed at x" + "a1" * 4 + ".",
    "pytest 12 tests passed",
    "pytest 12 test  passed",
    "CI: 3 errors",
    "CI: 3 error",
    "ci: 3 errors",
    "CI 2025-01-01: 3 failed",
    "CI at abc1234: 5 passed. CI: 7 failed.",
    "The tests ran. 5 passed.",
    "testsuite 5 passed",
    "suite 5 passing, 2 failing, 1 skipped",
    "suite 1.5 passed",
    "suite 1,,5 passed",
    'suite "5 passed" then 6 passed',
    "suite: 5 passed and 12 stars",
    # Rule 5: relative dates.
    "We changed it recently.",
    "Recently, we changed it.",
    "the least recently used entry",
    "the most recently opened file",
    "the least-recently used entry",
    "the most-recently added",
    "the least  recently used entry",
    "LEAST RECENTLY USED",
    "a recently used cache",
    "a recently-viewed page",
    "a recently  used cache",
    "recently, used",
    "recentlyish",
    "lately we ship",
    "nowadays",
    "these days",
    "these  days",
    "the other day",
    "just now",
    "a while ago",
    "not long ago",
    "3 days ago",
    "a few weeks ago",
    "an hour ago",
    "a couple of months ago",
    "several years ago",
    "two  days\tago",
    "eleven days ago",
    "3 days agog",
    "last friday",
    "Next Week",
    "this quarter",
    "coming weekend",
    "past fall",
    "last Fridays",
    "last\nnight",
    "it is now done",
    "they are now done",
    "what is now done",
    "WHAT IS NOW done",
    "what  is now done",
    "it is now known as X",
    "it is now known  as X",
    "it is nowhere",
    "this is now",
    '"recently" we left',
    '"recently" and recently',
    "“recently” we left",
    "“recently“ recently”",
    '"open quote recently',
    'two "quotes" then "recently"',
    "‘recently’ is not a span",
    # Several rules, and the snippet's edges.
    "The user's repo has 5 stars. TODO: tests 3 passed recently.",
    "Fix the hook; the assistant said the repo has 9 forks and CI shows 4 failed lately.",
    _PAD + "recently" + _LONG_TAIL,
    _PAD[:-2] + ". The user's\n\nproject   uses\t\tRust" + _LONG_TAIL,
    "." * 19 + "recently" + "." * 19,
    "." * 20 + "recently" + "." * 20,
    "." * 21 + "recently" + "." * 21,
    # Lone surrogates.
    "\ud800 recently \udfff",
    "\ud800The user's x",
    "the assistant \udfff said",
    "Tests \ud800 5 passed",
]

_TEST_TOTAL_SENTENCES: list[str] = [
    "",
    "tests 5 passed",
    "5 passed",
    "tests 5 passed 2025-01-01",
    "tests 5 passed abc1234",
    "tests 5 passed abcdefg",
    "Suite x",
    "CI abc1234",
    "CI 2025-13-45",
    "PYTEST",
    "tested",
]

#: (head, unit, count at 2,000 characters, tail); a body is ``head +
#: unit * (count * size // 2000) + tail`` at each of `_HOSTILE_SIZES`.
_HOSTILE: list[tuple[str, str, int, str]] = [
    ("", "1.", 1000, ""),
    ("", "1,", 1000, ""),
    ("tests ", "1,", 1000, " passed"),
    ("tests 1 passed ", "a1", 1000, ""),
    ("tests 1 passed ", "a1", 1000, " 2025-01-01"),
    ("the assistant", " x", 1000, ""),
    ("", "the assistant ", 142, ""),
    ("", " " * 30 + "\n", 64, ""),
    ("1", " ", 2000, ""),
    ("", '"1 star" ', 222, ""),
    ('"', "1 star ", 285, ""),
    ("2025-01-01 ", "1 star ", 284, ""),
    ("tests 2025-01-01 ", "1 passed ", 222, ""),
]
_HOSTILE_SIZES = (250, 500, 1000, 2000)

#: The words, split on single spaces.
_SWEEP_WORDS: list[str] = (
    "The the user's user’s assistant assistant's said suggested "
    "pointed out then recommendation TODO todo TODO: Next steps: "
    "Action items: still to need needs yet be left do [ ] in next "
    "session time when we resume Fix fix add Run Clean up it a this "
    "recently least most used viewed lately these days ago 3 1,024 "
    "1.2k few weeks last friday week is are now what known as stars "
    "GitHub forks open issues tests passed failed suite CI pytest "
    '2025-01-01 abc1234 deadbeef 1234567 " “ ” x'
).split(" ")
_SWEEP_SEPS: list[str] = [" ", " ", " ", " ", ". ", "\n", ", ", ": ", "\n- [ ] ", "**"]
_SWEEP_SEED = 20261125
_SWEEP_COUNT = 20000
_SWEEP_MOST = 12


def _lcg(state: int) -> int:
    return (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)


def capture_sweep_bodies() -> Iterator[str]:
    state = _SWEEP_SEED
    for _ in range(_SWEEP_COUNT):
        state = _lcg(state)
        n = 1 + (state >> 33) % _SWEEP_MOST
        parts: list[str] = []
        for _ in range(n):
            state = _lcg(state)
            parts.append(_SWEEP_WORDS[(state >> 33) % len(_SWEEP_WORDS)])
            state = _lcg(state)
            parts.append(_SWEEP_SEPS[(state >> 33) % len(_SWEEP_SEPS)])
        yield "".join(parts)


def _hits(text: str) -> list[dict[str, Any]]:
    out = []
    for hit in d.find_capture_rule_hits(text):
        row = hit.to_dict()
        assert list(row) == ["rule", "name", "snippet"]
        out.append({"rule": hit.rule, "name": hit.name, **text_field("s", hit.snippet)})
    return out


def _capture_sweep() -> dict[str, Any]:
    h = hashlib.sha256()
    rules: dict[str, int] = {}
    count = 0
    for body in capture_sweep_bodies():
        count += 1
        for hit in d.find_capture_rule_hits(body):
            rules[hit.name] = rules.get(hit.name, 0) + 1
            h.update(f"{hit.rule}|{hit.name}|{hit.snippet}\n".encode())
        h.update(b"\0")
    assert all(_capture_stable(w) for w in _SWEEP_WORDS + _SWEEP_SEPS)
    return {
        "k": "sweep",
        "seed": _SWEEP_SEED,
        "count": count,
        "most": _SWEEP_MOST,
        "words": _SWEEP_WORDS,
        "seps": _SWEEP_SEPS,
        "rules": rules,
        "sha": h.hexdigest(),
    }


def _unicode_capture_texts() -> Iterator[str]:
    # IGNORECASE as `re` folds one code point against one.
    yield "THE ASSİSTANT SAID so"
    yield "the assiſtant said so"
    yield "the assistant ſaid so"
    yield "the aßistant said so"
    yield "the assistant SAİD so"
    yield "the assistant’s ANſWER"
    yield "5\u212a stars"
    yield "Chec\u212a the hook"
    yield "ſtill to decide"
    yield "\u212anown as"
    yield "it is now \u212anown as X"
    yield "leaſt recently used"
    yield "ᴛᴏᴅᴏ: x"
    # \b, \w, \s and \d past ASCII.
    yield "érecently"
    yield "—recently"
    yield "the\u00a0assistant said so"
    yield "the assistant\u00a0\u2003said so"
    yield "the assistant été said so"
    yield "the assistant été très said so"
    yield "3\u2009days ago"
    yield "٣ stars"
    yield "٣ stars on ٢٠٢٥-٠١-٠١"
    yield "５ stars"
    yield "Tests: ٣ passed"
    yield "Tests: 5 passed at abc12٣4"
    yield "Tests: 5 passed at abc123٣"
    yield "x:\u00a0The user's project"
    yield "— The user's project"
    yield "★ The user's project"
    yield "→ The user's project"
    yield "• The user's project"
    yield "x。The user's project"
    yield "\u3000Fix the hook"
    yield "\u00a0- [ ] item"
    yield "\u00a0TODO: item"
    yield "x\u2028Action items: one"
    yield "x\u0085Action items: one"
    yield "It has 5 stars。 Checked 2025-01-01."


def _unicode_test_total_sentences() -> Iterator[str]:
    yield "ᴛests 5 passed"
    yield "tests 5 passed ٢٠٢٥-٠١-٠١"
    yield "Suite ５ passed"


def _capture_rows(unicode: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not unicode:
        for name, pattern in _CAPTURE_PATTERN_IDS:
            rows.append(
                {"k": "pat", "id": name, "p": pattern.pattern, "f": pattern.flags}
            )
    texts = list(_unicode_capture_texts()) if unicode else _CAPTURE_TEXTS
    for t in texts:
        assert _capture_stable(t) != unicode, t
        rows.append({"k": "hits", **text_field("t", t), "o": _hits(t)})
    sentences = (
        list(_unicode_test_total_sentences()) if unicode else _TEST_TOTAL_SENTENCES
    )
    for s in sentences:
        assert _capture_stable(s) != unicode, s
        rows.append({"k": "tt", "t": s, "o": d._unanchored_test_total(s)})
    if not unicode:
        for head, unit, full, tail in _HOSTILE:
            for size in _HOSTILE_SIZES:
                n = full * size // 2000
                body = head + unit * n + tail
                rows.append(
                    {
                        "k": "rep",
                        "head": head,
                        "unit": unit,
                        "n": n,
                        "tail": tail,
                        "o": _hits(body),
                    }
                )
        rows.append(_capture_sweep())
    return rows


def generate_capture() -> dict[str, bytes]:
    """``durability_capture.jsonl``: not gated."""
    rows = _capture_rows(unicode=False)
    return {
        "durability_capture.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }


def generate_capture_unicode() -> dict[str, bytes]:
    """``durability_capture_unicode.jsonl``: gated on the Unicode version."""
    rows = _capture_rows(unicode=True)
    return {
        "durability_capture_unicode.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }


# ---------------------------------------------------------------------------
# Unit 2: the transient markers.
# ---------------------------------------------------------------------------
#
# ``durability_markers.jsonl`` (not gated) and
# ``durability_markers_unicode.jsonl`` (gated on the Unicode version) pin
# the marker tables, every pattern of `_PHRASE_REGEXES` and
# `_AS_OF_DATE_RE` by its text and flags, `_anchored_new_reference` and
# `find_transient_markers`. One JSON object per line, keys sorted, ASCII,
# a header first with the count of the lines after it. Every other line is
# one of:
#
# - ``{"k": "tables", "phrases": [...], "titlecase": [...], "overrides":
#   [...], "in_flight": text, "as_of": text}``: `TRANSIENT_PHRASE_MARKERS`
#   in order, `_TITLECASE_SKIP_MARKERS` sorted, `_PATTERN_OVERRIDES`' keys
#   in order, `_IN_FLIGHT_WORK_OBJECT` and `_AS_OF_DATE_MARKER`;
# - ``{"k": "pat", "id": phrase or "_AS_OF_DATE_RE", "p": text, "f":
#   flags}``: the pattern's `.pattern` and `.flags`;
# - ``{"k": "hits", "t": text, "o": [{"m": marker, "s": snippet}, ...]}``:
#   `find_transient_markers`;
# - ``{"k": "anc", "t": text, "s": start, "o": bool}``:
#   `_anchored_new_reference` of the match ``the new`` at ``s``;
# - ``{"k": "sweep", "seed", "count", "most", "words", "seps", "markers",
#   "sha"}`` (first file only): 20,000 bodies drawn by the LCG of the
#   capture sweep from a word pool rich in the phrases, pinned by count,
#   hits per marker and the SHA-256 of ``marker|snippet\n`` per hit with a
#   NUL after each body.
#
# A text (``t``, ``s``) is a string or, where it holds a surrogate, its
# code points (``t_cps``, ``s_cps``). The first file's texts are ASCII,
# the curly double quotes and lone surrogates, whose readings (`\b`, `\w`,
# `\s`, IGNORECASE on ASCII, `str.istitle`, the general category, the
# tokenizer) no Unicode version changes; every other text is in the
# second. No answer depends on the Python release or the platform.

_MARKER_STABLE = frozenset({0x201C, 0x201D})


def _marker_stable(text: str) -> bool:
    return all(
        ord(ch) < 0x80 or ord(ch) in _MARKER_STABLE or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in text
    )


def _phrase_texts(phrase: str) -> Iterator[str]:
    """Each phrase hit and missed at its edges."""
    title = " ".join(w.capitalize() for w in phrase.split())
    yield f"x {phrase} y"
    yield f"{phrase.capitalize()} we said."
    yield phrase.upper()
    yield f"x{phrase} y"
    yield f"x {phrase}x"
    yield f"see {phrase}.io and {phrase}.x"
    yield f"Deploy {phrase}."
    yield f"{phrase}.\n"
    yield f'"{phrase}" and then {phrase}'
    yield f"“{phrase}” only"
    yield f"the {title} Show"
    yield f"{title} We Ship"


_MARKER_TEXTS: list[str] = [
    "",
    "A durable fact about the hook.",
    "concurrently news renewal",
    # today's override: the possessive and the domain.
    "today's date",
    "TODAY'S date",
    "todays date",
    "today.dev is a site",
    "merged earlier today",
    "Today, I merged it",
    "today' is odd",
    "today's and today",
    # the new: lowercase new only, [Tt] only, no domain lookahead.
    "The New York office",
    "The new schema",
    "THE NEW SCHEMA",
    "tHe new schema",
    "the New schema",
    "the news today",
    "the newer one",
    "see the new.io site",
    "athe new",
    # at the moment: the event-trigger heads.
    "at the moment of release",
    "at the moment when it runs",
    "at the moment that it runs",
    "At the moment the plan is",
    "at the moment  of release",
    "at the moment\tof release",
    "at the momentof release",
    "at the moment ofx release",
    "at the moment.io",
    "at the moment.",
    "AT THE MOMENT OF release",
    # temporarily: the habitual third-person form.
    "the limiter temporarily blocks an IP",
    "temporarily disabled",
    "temporarily s",
    "temporarily ss",
    "temporarily blocks.",
    "temporarily blocksx",
    "temporarily.io",
    "temporarily  blocks",
    "temporarily is",
    "temporarily",
    # The escaped space is one ASCII space.
    "right  now",
    "right\tnow",
    "it is in progress",
    # the two idioms: an in-flight-work object only.
    "in the middle of migrating the database",
    "in the middle of morning",
    "in the middle of evening",
    "in the middle of mornings",
    "in the middle of morningx",
    "in the middle of thing",
    "in the middle of ing",
    "in the middle of a migration",
    "in the middle of an upgrade",
    "in the middle of the rollout",
    "in the middle of the  rollout",
    "in the middle of deploy",
    "in the middle of deployment",
    "in the middle of deployments",
    "in the middle of refactor",
    "in the middle of refactoring",
    "in the middle of a rewrite",
    "in the middle of a rebase",
    "in the middle of a merge",
    "in the middle of a release",
    "in the middle of a focus block",
    "in the middle of  migrating",
    "in the middle ofmigrating",
    "In The Middle Of A Migration",
    "in the middle of migratingx1",
    "in the middle of a the migration",
    "halfway through rebasing",
    "halfway through the upgrade",
    "the installer reboots halfway through",
    "halfway through evening",
    "halfway through a rewrite.",
    "HALFWAY THROUGH A MERGE",
    # Anchored "the new": a cue and an identifier in the match's sentence.
    "We switched to Bugsnag with the new SDK release.",
    "We upgraded to Python 3.13 with the new base image.",
    "The new schema replaces the old layout.",
    "Bugsnag ships the new SDK.",
    "We switched to Bugsnag. The new schema is live.",
    "The new schema is live. We switched to Bugsnag.",
    "We moved to v2 and the new flow is live; it works.",
    "We moved to the new flow on billing-db-green.",
    "We moved to the new flow on read-only.",
    "We moved to the new flow on prod-hlx-2.",
    "Now the new parser runs 3 passes.",
    "We moved to Bugsnag with the new SDK! Then the new cache landed.",
    "We moved to Bugsnag with the new SDK\nthe new cache landed.",
    '"the new" is quoted; we moved to Bugsnag with the new SDK.',
    "Version 3.13 moved the new parser in",
    "WE MOVED TO V2 WITH THE NEW FLOW",
    "We MOVED to Bugsnag with the new SDK.",
    "We moved 3.13's the new parser",
    # The title-case skip: the time words only.
    "the Tomorrow Night theme",
    "Tomorrow Night is a theme",
    "Tomorrow we ship",
    "Yesterday I broke the build",
    "This Week in Rust is a newsletter",
    "This week in Rust",
    "a USA Today column",
    "the Currently Show",
    "the Right Now Show",
    "Tomorrow Night and tomorrow",
    "Tomorrow Night. Tomorrow we ship.",
    "- Today Matters",
    "x - Today Matters",
    "THIS WEEK in Rust",
    # The dated form.
    "as of 2025-01-01 the cluster is on k8s",
    "As Of 2025-01-01 Things Changed",
    "AS OF 2025-01-01",
    '"as of 2025-01-01" and as of 2025-02-02',
    '"as of 2025-01-01" only',
    "“as of 2025-01-01” only",
    "as of 2025-1-01",
    "as of 20250-01-01",
    "xas of 2025-01-01",
    "as of 2025-01-012",
    "as of  2025-01-01",
    "as of 2025-01-01.io",
    "as of 2.7.0 and as of Python 3.12",
    "as of 2025-01-01 currently",
    "as of 2025-01-01 and as of 2025-02-02",
    "as of today and as of yesterday",
    # Quotes: line-bounded, unpaired, curly, the quoted use then the use.
    '"currently" we are currently here',
    '"currently\nwe are" here',
    'odd " currently',
    '"a" currently "b"',
    "“currently“ later”",
    "”currently“",
    '"currently “currently”" x',
    '"today" "currently"',
    '"Tomorrow" Night and Tomorrow Night',
    # Order: phrases in table order, the date last; one hit per phrase.
    "the new thing, currently, today and as of 2025-01-01",
    "currently currently currently",
    "now relying now relies now rely now using now use now uses",
    "i just did it; I just did; we just did",
    "it is unpushed and stashed; it is stashed",
    # The snippet's window.
    "x" * 30 + " currently " + "y" * 30,
    "line one\ncurrently   spaced\t\ttabs\nline three",
    # Lone surrogates.
    "currently \ud800",
    "\ud800currently\udfff",
    "\ud800 today \ud800",
    '"\ud800currently" currently',
    "the new \ud800 thing",
    "We moved to Bugsnag \ud800 with the new SDK.",
    "as of 2025-01-01\ud800",
]


def _every_phrase_body() -> str:
    objects = {
        "in the middle of": "in the middle of migrating",
        "halfway through": "halfway through a merge",
    }
    parts = [objects.get(p, p) for p in d.TRANSIENT_PHRASE_MARKERS]
    return ". ".join(parts) + ". as of 2025-01-01."


def _anc_texts() -> list[str]:
    return [
        "We switched to Bugsnag with the new SDK release.",
        "The new schema replaces the old layout.",
        "Bugsnag ships the new SDK.",
        "We switched to Bugsnag. The new schema is live.",
        "Python 3.13. We moved the new parser",
        "We moved 3.13 with the new parser",
        "no cue and no anchor: the new thing",
        "now the new thing",
        "Now the New Thing is here",
    ]


def _marker_hits(text: str) -> list[dict[str, Any]]:
    out = []
    for hit in d.find_transient_markers(text):
        assert isinstance(hit, d.TransientMatch)
        out.append({"m": hit.marker, **text_field("s", hit.snippet)})
    return out


_THE_NEW = re.compile(r"the new", re.IGNORECASE)


def _anc_rows(texts: list[str]) -> list[dict[str, Any]]:
    rows = []
    for t in texts:
        for m in _THE_NEW.finditer(t):
            rows.append(
                {
                    "k": "anc",
                    **text_field("t", t),
                    "s": m.start(),
                    "o": d._anchored_new_reference(t, m),
                }
            )
    return rows


_MARKER_SWEEP_WORDS: list[str] = [
    *d.TRANSIENT_PHRASE_MARKERS,
    "Today",
    "Tomorrow Night",
    "This Week",
    "The new",
    "The New",
    "THE NEW",
    "today's",
    "tomorrow.io",
    "concurrently",
    "news",
    "of",
    "when",
    "blocks",
    "disabled",
    "migrating",
    "a migration",
    "morning",
    "switched to",
    "moved",
    "now",
    "Bugsnag",
    "3.13",
    "v2",
    "the",
    "I",
    "is",
    "as of 2025-01-01",
    "as of",
    "2025-01-01",
    '"',
    "“",
    "”",
    "x",
]
_MARKER_SWEEP_SEPS: list[str] = [" ", " ", " ", " ", ". ", "\n", ", ", '"', "-"]
_MARKER_SWEEP_SEED = 20261126
_MARKER_SWEEP_COUNT = 20000
_MARKER_SWEEP_MOST = 12


def marker_sweep_bodies() -> Iterator[str]:
    state = _MARKER_SWEEP_SEED
    for _ in range(_MARKER_SWEEP_COUNT):
        state = _lcg(state)
        n = 1 + (state >> 33) % _MARKER_SWEEP_MOST
        parts: list[str] = []
        for _ in range(n):
            state = _lcg(state)
            parts.append(_MARKER_SWEEP_WORDS[(state >> 33) % len(_MARKER_SWEEP_WORDS)])
            state = _lcg(state)
            parts.append(_MARKER_SWEEP_SEPS[(state >> 33) % len(_MARKER_SWEEP_SEPS)])
        yield "".join(parts)


def _marker_sweep() -> dict[str, Any]:
    h = hashlib.sha256()
    markers: dict[str, int] = {}
    count = 0
    for body in marker_sweep_bodies():
        count += 1
        for hit in d.find_transient_markers(body):
            markers[hit.marker] = markers.get(hit.marker, 0) + 1
            h.update(f"{hit.marker}|{hit.snippet}\n".encode())
        h.update(b"\0")
    assert all(_marker_stable(w) for w in _MARKER_SWEEP_WORDS + _MARKER_SWEEP_SEPS)
    return {
        "k": "sweep",
        "seed": _MARKER_SWEEP_SEED,
        "count": count,
        "most": _MARKER_SWEEP_MOST,
        "words": _MARKER_SWEEP_WORDS,
        "seps": _MARKER_SWEEP_SEPS,
        "markers": markers,
        "sha": h.hexdigest(),
    }


def _unicode_marker_texts() -> Iterator[str]:
    # IGNORECASE as `re` folds one code point against one.
    yield "next weeK we ship"
    yield "it is ſtashed"
    yield "yeſterday it broke"
    yield "İ just did it"
    yield "ı just did it"
    yield "now uſes it"
    yield "THE NEW ſCHEMA"
    yield "at the moment ſf release"
    yield "temporarily blockſ an IP"
    yield "in the middle of migratİng"
    yield "in the middle of a migratıon"
    yield "Keep it for now"
    yield "commits aħead"
    yield "the latest straße"
    # \b and \w past ASCII.
    yield "écurrently"
    yield "currentlyé"
    yield "—currently"
    yield "currently.é"
    yield "currently.—"
    yield "tomorrow.ио"
    yield "١currently"
    yield "currentlỳ"
    yield "in the middle of réstoring it"
    yield "in the middle of migratingé"
    yield "in the middle of migratéing"
    yield "halfway through ١ing"
    yield "temporarily és"
    yield "as of ٢٠٢٥-٠١-٠١"
    yield "as of 2025-01-01é"
    yield "as of ２０２５-01-01"
    # \s past ASCII in the lookaheads; the generic patterns' escaped space.
    yield "at the moment of release"
    yield "at the moment when"
    yield "temporarily blocks"
    yield "the limiter temporarily blocks"
    yield "right now"
    yield "in the middle of the rollout"
    # The curly apostrophe.
    yield "today’s date"
    yield "Today’s date and today"
    yield "today‘s date"
    # Title case past ASCII.
    yield "Today Été Fest"
    yield "Today été fest"
    yield "Today ǅungla"
    yield "x — Today Night"
    yield "x → Today Night"
    yield "\U0001f680 Today Night"
    yield "This ǅeek"
    yield "É Tomorrow Night"
    # Anchored "the new" past ASCII.
    yield "We moved to Python ٣.١٣ with the new image."
    yield "We moved to Zürich with the new office."
    yield "We moved to v2。the new flow is live"
    yield "We moved to v2 the new flow is live"
    # Quotes and snippets past ASCII.
    yield "“currently” é currently"
    yield "é" * 30 + " today " + "é" * 30
    yield "　today　"


def _marker_rows(unicode: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not unicode:
        rows.append(
            {
                "k": "tables",
                "phrases": list(d.TRANSIENT_PHRASE_MARKERS),
                "titlecase": sorted(d._TITLECASE_SKIP_MARKERS),
                "overrides": list(d._PATTERN_OVERRIDES),
                "in_flight": d._IN_FLIGHT_WORK_OBJECT,
                "as_of": d._AS_OF_DATE_MARKER,
            }
        )
        pats = [*d._PHRASE_REGEXES, ("_AS_OF_DATE_RE", d._AS_OF_DATE_RE)]
        for name, pattern in pats:
            rows.append(
                {"k": "pat", "id": name, "p": pattern.pattern, "f": pattern.flags}
            )
    if unicode:
        texts = list(_unicode_marker_texts())
    else:
        texts = [
            *_MARKER_TEXTS,
            *[t for p in d.TRANSIENT_PHRASE_MARKERS for t in _phrase_texts(p)],
            _every_phrase_body(),
        ]
    for t in texts:
        assert _marker_stable(t) != unicode, t
        rows.append({"k": "hits", **text_field("t", t), "o": _marker_hits(t)})
    if not unicode:
        rows.extend(_anc_rows(_anc_texts()))
        rows.append(_marker_sweep())
    else:
        rows.extend(_anc_rows([t for t in texts if "the new" in t.lower()]))
    return rows


def generate_markers() -> dict[str, bytes]:
    """``durability_markers.jsonl``: not gated."""
    rows = _marker_rows(unicode=False)
    return {
        "durability_markers.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }


def generate_markers_unicode() -> dict[str, bytes]:
    """``durability_markers_unicode.jsonl``: gated on the Unicode version."""
    rows = _marker_rows(unicode=True)
    return {
        "durability_markers_unicode.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }
