"""Fixture: git's wildmatch as the reference ports it
(``gitconfig_wildmatch.jsonl``).

Pins `bettermemory.gitconfig.wildmatch` (``WM_PATHNAME``, with
``WM_CASEFOLD`` where ``icase``), its step and depth budgets (`Steps`,
``_WM_STEPS``, ``_WM_DEPTH``), and the two helpers the ``includeIf``
conditions call, `_any_match` and `_literal_head`. Patterns and texts are
built by rule: each construct alone (``*``, ``**`` in every position, ``?``,
brackets with ranges, negation, ``]`` first, the named classes, escapes, a
trailing backslash, an unterminated bracket, bytes at or above 0x80, NUL,
ASCII and non-ASCII case) and in pairs, each against a fixed set of texts,
with and without ``icase``; then shapes built to spend each budget, at the
budget and one step past it.

A byte string is ``=`` and itself where it is printable ASCII with no
backslash and no double quote, else its hex; or a recipe
``[[piece, count], ...]`` (the pieces repeated and joined) for the long
ones. The first line is a header (counts and the budgets). Then:

- ``[pattern, text, icase, answer, taken]`` or, with a budget other than
  ``_WM_STEPS``, ``[pattern, text, icase, answer, taken, budget]``:
  ``answer`` is 1 (match), 0 (no match), ``"s"`` (spent: more steps than
  the budget) or ``"d"`` (spent: more than ``_WM_DEPTH`` nested stars);
  ``taken`` is the budget less the steps left after the call.
- ``{"any": [pattern, [text, ...], icase], "r": true|false|null, "taken": n}``:
  `_any_match`, null where none matched and one was spent.
- ``{"head": [pattern, text, literal, icase], "r": bool}``: `_literal_head`.

Not gated. The reference reads bytes by index and compares integers; it
consults no Unicode data, no locale and no platform. Its recursion is at
most 151 nested stars (two frames each), far inside Python's limit, so
`RecursionError` is never what spends a case here. Checked: the file is the
same byte for byte under Python 3.11 and 3.13.
"""

from __future__ import annotations

from collections.abc import Iterator

from bettermemory import gitconfig

from ._common import dumps_line

_Bytes = bytes | list[tuple[bytes, int]]


def _b(data: bytes) -> str:
    if all(0x20 <= c <= 0x7E and c not in (0x5C, 0x22) for c in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _enc(data: _Bytes) -> object:
    if isinstance(data, bytes):
        return _b(data)
    return [[_b(piece), n] for piece, n in data]


def _built(data: _Bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    return b"".join(piece * n for piece, n in data)


#: Each construct alone.
_ATOMS: list[bytes] = [
    b"",
    b"a",
    b"A",
    b"abc",
    b"aBc",
    b"*",
    b"**",
    b"***",
    b"?",
    b"??",
    b"/",
    b"a/",
    b"/a",
    b"a/b",
    b"*/",
    b"/*",
    b"a*",
    b"*a",
    b"a*c",
    b"a*/c",
    b"**/",
    b"/**",
    b"/**/",
    b"a/**",
    b"**/a",
    b"a/**/b",
    b"a**",
    b"**a",
    b"a**b",
    b"a/**b",
    b"a**/b",
    b"**\\/a",
    b"*\\/a",
    b"[a]",
    b"[A]",
    b"[abc]",
    b"[a-c]",
    b"[A-C]",
    b"[c-a]",
    b"[!a]",
    b"[^a]",
    b"[!a-c]",
    b"[]]",
    b"[]a]",
    b"[!]]",
    b"[^]a]",
    b"[a-]",
    b"[-a]",
    b"[a-\\]]",
    b"[\\]]",
    b"[\\-]",
    b"[\\a]",
    b"[/]",
    b"[!/]",
    b"[[]",
    b"[[:]",
    b"[[:a]",
    b"[[::]]",
    b"[[:alpha:]]",
    b"[[:alnum:]]",
    b"[[:blank:]]",
    b"[[:cntrl:]]",
    b"[[:digit:]]",
    b"[[:graph:]]",
    b"[[:lower:]]",
    b"[[:print:]]",
    b"[[:punct:]]",
    b"[[:space:]]",
    b"[[:upper:]]",
    b"[[:xdigit:]]",
    b"[![:upper:]]",
    b"[[:bogus:]]",
    b"[[:upper:]a]",
    b"[a[:digit:]-z]",
    b"[",
    b"[a",
    b"[a-",
    b"[\\",
    b"[!",
    b"[]",
    b"\\",
    b"a\\",
    b"\\a",
    b"\\A",
    b"\\*",
    b"\\?",
    b"\\[",
    b"\\\\",
    b"\\/",
    b"\xc3\xa9",
    b"\xc3\x89",
    b"\xff",
    b"[\x80-\xff]",
    b"[\xc3]",
    b"\x00",
    b"a\x00",
    b"[\x00]",
    b"*\x00",
    b"[a\x00]",
]

#: The texts each pattern is matched against.
_TEXTS: list[bytes] = [
    b"",
    b"a",
    b"A",
    b"abc",
    b"a/b",
    b"a/b/c",
    b"x/a/y/b",
    b"]",
    b"[",
    b"\\",
    b"\xc3\xa9",
    b"a\x00",
]

#: Pairs: atoms likely to interact, joined.
_PAIR_ATOMS: list[bytes] = [
    b"a",
    b"*",
    b"**",
    b"?",
    b"/",
    b"**/",
    b"/**",
    b"[a-c]",
    b"[!a]",
    b"\\",
    b"\\*",
    b"[[:upper:]]",
    b"[",
    b"\xc3",
]

_PAIR_TEXTS: list[bytes] = [
    b"a",
    b"ab",
    b"a/b",
    b"A/B/",
    b"/a/",
    b"aa/bb/cc",
    b"\xc3\xa9",
]


#: Where case folds and where it does not: a byte after a backslash and a
#: plain byte inside a bracket are compared as written, a range and
#: ``[:upper:]`` fold, alone, negated and inside longer patterns.
_CASE_ATOMS: list[bytes] = [
    b"[\\A]",
    b"[A]",
    b"\\A",
    b"[A-A]",
    b"[\\a]",
    b"[a]",
    b"\\a",
    b"[a-a]",
    b"A",
    b"a",
    b"[!\\A]",
    b"[!A]",
    b"[!a]",
    b"[!A-A]",
    b"[!a-a]",
    b"[[:upper:]]",
    b"[[:lower:]]",
    b"[![:upper:]]",
    b"[![:lower:]]",
    b"[[:alpha:]]",
    b"[B-Z]",
    b"[b-z]",
]
_CASE_TEXTS: list[bytes] = [b"a", b"A", b"xA/a", b"Xa/A"]


def _case_patterns() -> Iterator[bytes]:
    for atom in _CASE_ATOMS:
        yield atom
        yield b"x" + atom + b"/" + atom
        yield b"*" + atom + b"/*"


def _patterns() -> Iterator[tuple[bytes, list[bytes]]]:
    for atom in _ATOMS:
        yield atom, _TEXTS
    for case in _case_patterns():
        yield case, _CASE_TEXTS
    for left in _PAIR_ATOMS:
        for right in _PAIR_ATOMS:
            yield left + right, _PAIR_TEXTS


#: Shapes that spend a budget: (pattern, text), with a budget or None.
def _spenders() -> Iterator[tuple[_Bytes, _Bytes]]:
    for k in (4, 8, 12, 16):
        yield [(b"*a", k), (b"b", 1)], [(b"a", 3 * k)]
    for k in (8, 16, 24):
        yield [(b"**/", k), (b"x", 1)], [(b"a/", 3 * k)]
    yield [(b"*", 1), (b"[a-z]*", 12), (b"!", 1)], [(b"ab", 30)]
    for k in (148, 149, 150, 151, 152):
        yield [(b"*a", k)], [(b"a", k)]
        yield [(b"*/", k)], [(b"a/", k)]
    n = gitconfig._WM_STEPS
    # A literal takes one step a byte: at the budget, and one past it.
    for k in (n - 1, n, n + 1):
        yield [(b"a", k)], [(b"a", k)]
        yield [(b"?", k)], [(b"b", k)]


def _letters(data: bytes) -> bool:
    return any(0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A for c in data)


def _answer(pattern: bytes, text: bytes, icase: bool, budget: int) -> list[object]:
    steps = gitconfig.Steps(budget)
    try:
        found: object = int(
            gitconfig.wildmatch(pattern, text, icase=icase, steps=steps)
        )
    except gitconfig.WildmatchSpent as exc:
        found = "d" if "nests" in str(exc) else "s"
    return [found, budget - steps.left]


def _row(pattern: _Bytes, text: _Bytes, icase: bool, budget: int) -> list[object]:
    p, t = _built(pattern), _built(text)
    row = [_enc(pattern), _enc(text), int(icase), *_answer(p, t, icase, budget)]
    if budget != gitconfig._WM_STEPS:
        row.append(budget)
    return row


def generate() -> dict[str, bytes]:
    full = gitconfig._WM_STEPS
    rows: list[list[object]] = []
    seen: set[tuple[bytes, bytes, bool]] = set()
    for pattern, texts in _patterns():
        for text in texts:
            for icase in (False, True):
                # Case folds only ASCII letters: with none on either side
                # the flag changes nothing, and the case is taken once.
                if icase and not _letters(pattern + text):
                    continue
                if (pattern, text, icase) in seen:
                    continue
                seen.add((pattern, text, icase))
                row = _row(pattern, text, icase, full)
                rows.append(row)
                # Every 16th case again at exactly the steps it took and one
                # fewer: the budget's edge for each construct.
                taken = row[4]
                assert isinstance(taken, int)
                if len(rows) % 16 == 0 and taken > 0:
                    rows.append(_row(pattern, text, icase, taken))
                    rows.append(_row(pattern, text, icase, taken - 1))
    for spend, spent_on in _spenders():
        for icase in (False, True):
            rows.append(_row(spend, spent_on, icase, full))
    others: list[dict[str, object]] = []
    for pattern, texts, icase in [
        (b"a*", [b"b", b"ab"], False),
        (b"A*", [b"b", b"ab"], True),
        (b"x", [], False),
        (b"*a" * 20 + b"b", [b"a" * 60, b"ab"], False),
        (b"*a" * 20 + b"b", [b"a" * 60, b"c"], False),
        (b"*a" * 20 + b"b", [b"c", b"a" * 60], False),
        (b"*a" * 160, [b"a" * 160, b"a"], False),
    ]:
        steps = gitconfig.Steps()
        r = gitconfig._any_match(pattern, texts, steps, icase=icase)
        others.append(
            {
                "any": [_b(pattern), [_b(t) for t in texts], int(icase)],
                "r": r,
                "taken": full - steps.left,
            }
        )
    for pattern, text, literal in [
        (b"/a/b/*", b"/a/b/c", 5),
        (b"/A/b/*", b"/a/B/c", 5),
        (b"/a/b/*", b"/a/b", 5),
        (b"/a/b/*", b"/a/x/c", 5),
        (b"x", b"", 0),
        (b"/\xc3\x89/", b"/\xc3\xa9/", 4),
        (b"/\xc3\x89/", b"/\xc3\x89/", 4),
    ]:
        for icase in (False, True):
            others.append(
                {
                    "head": [_b(pattern), _b(text), literal, int(icase)],
                    "r": gitconfig._literal_head(pattern, text, literal, icase),
                }
            )
    header = {
        "fixture": "gitconfig_wildmatch",
        "cases": len(rows),
        "others": len(others),
        "steps": full,
        "depth": gitconfig._WM_DEPTH,
        "spent_steps": sum(1 for r in rows if r[3] == "s"),
        "spent_depth": sum(1 for r in rows if r[3] == "d"),
        "matched": sum(1 for r in rows if r[3] == 1),
    }
    lines = [dumps_line(header)] + [dumps_line(r) for r in [*rows, *others]]
    return {"gitconfig_wildmatch.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
