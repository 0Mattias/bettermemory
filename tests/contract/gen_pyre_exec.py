"""Fixtures: a lowered pattern run on a text as ``re`` runs it (unit
pyre-5a, ``native/crates/bettermemory-core/src/pyre/exec.rs``).

Each pattern's tree is written as `gen_pyre_tree._item` writes one (from
``re._parser.parse``), with the flags ``_compile`` reads and the number of
groups. Its answers are those of ``Pattern.match``, ``Pattern.fullmatch``
and ``Pattern.search`` with ``pos`` and ``endpos``; an answer is ``-`` for
no match, otherwise the spans of the match and of each group (``-1,-1``
for a group that took no part) and ``lastindex`` (``-1`` for ``None``),
joined by ``|`` (`_answer`).

Parts:

- ``exec.sweep``: for every text over ``alphabet`` of at most ``long``
  code points, in order of length and then of `itertools.product`, the
  windows of `_windows`: every ``(pos, endpos)`` in ``[-1, n + 1]`` for a
  text of at most ``short`` code points, and ``(0, n)``, ``(1, n)``,
  ``(0, n - 1)`` and ``(1, n - 1)`` for a longer one. A window is
  *reversed* when, clamped to ``[0, n]`` as ``re`` clamps it, ``pos``
  lies past ``endpos``. The row holds the count of windows and the
  SHA-256 over the lines ``match fullmatch search`` (the three answers
  joined by a space, then a line feed). ``pyre_exec.jsonl`` digests the
  ordered windows; the reversed ones, whose answers are 3.13.13's
  (decision section 3, item 1), are digested in ``pyre_python.jsonl``.
- ``exec.kill``: one call written out, for the cases a plausible wrong
  engine fails.

A pattern under IGNORECASE reads the fold tables, so its rows are in
``pyre_unicode.jsonl`` (ordered windows) and ``pyre_python.jsonl``.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import re
from typing import Any

from ._common import jsonl
from .gen_pyre_lower import _ignorecase
from .gen_pyre_tree import _tree, spell

P: Any = importlib.import_module("re._parser")

#: ``(pattern, flags, alphabet, short, long)``. A bytes pattern runs on
#: bytes over the alphabet's code points.
PATTERNS: list[tuple[str | bytes, int, str, int, int]] = [
    ("a", 0, "ab", 6, 12),
    ("ab", 0, "ab", 6, 12),
    ("a*", 0, "ab", 6, 12),
    ("a*?", 0, "ab", 6, 12),
    ("a+b", 0, "ab", 6, 12),
    ("a+?b", 0, "ab", 6, 12),
    ("a{2,3}", 0, "ab", 6, 12),
    ("a{2,3}?b", 0, "ab", 6, 12),
    ("a*?$", 0, "ab", 6, 12),
    ("(a*)b", 0, "ab", 6, 12),
    ("(a+?)(b*)", 0, "ab", 6, 12),
    ("(?:ab)*", 0, "ab", 6, 12),
    ("(?:ab)*?b", 0, "ab", 6, 12),
    ("(?:ab)+", 0, "ab", 6, 12),
    ("(?:ab){1,2}?", 0, "ab", 6, 12),
    ("(?:ab){1,2}$", 0, "ab", 6, 12),
    ("(a|ab)(c|bc)", 0, "abc", 4, 8),
    ("(a)|b", 0, "ab", 6, 12),
    ("(?:(a)x|a)(b)", 0, "abx", 4, 8),
    ("(a)?(?:(b)x|b)", 0, "abx", 4, 8),
    ("((a)b)", 0, "ab", 6, 12),
    ("(a)(b)?", 0, "ab", 6, 12),
    ("^a", 0, "ab", 6, 12),
    ("a$", 0, "a\n", 6, 12),
    ("^a$", re.M, "a\n", 6, 12),
    ("$", re.M, "a\n", 6, 12),
    ("$", 0, "a\n", 6, 12),
    (r"\Aa\Z", 0, "ab", 6, 12),
    (r"\b", 0, "a ", 6, 12),
    (r"\ba\b", 0, "a ", 6, 12),
    (r"a\b|b", 0, "ab", 6, 12),
    (".", 0, "a\n", 6, 12),
    (".", re.S, "a\n", 6, 12),
    (".*", 0, "a\n", 6, 12),
    ("[ab]+", 0, "abc", 4, 8),
    ("[^a]*", 0, "ab", 6, 12),
    (r"\d\s", 0, "1 a", 4, 8),
    ("x*y*", 0, "xy", 6, 12),
    ("(?:a|b)*c", 0, "abc", 4, 8),
    ("(?:a?b)?", 0, "ab", 6, 12),
    ("a|", 0, "ab", 6, 12),
    ("", 0, "ab", 6, 12),
    ("a{0}b", 0, "ab", 6, 12),
    ("(?:a|ab)(?:c|bcd)", 0, "abcd", 3, 6),
    (b"a*b", 0, "ab", 6, 12),
    (rb"(?:[ \t\n\r]+|[#;][^\n]*)*", 0, " #\n", 4, 8),
    (b"[^\n]*$", re.M, "a\n", 6, 12),
    # IGNORECASE: in the Unicode file.
    ("(?i)k", 0, "kKK", 4, 8),
    ("(?i)s+", 0, "sSſ", 4, 8),
    ("(?i)ab*", 0, "aAB", 4, 8),
    ("(?i:a)*", 0, "aA", 6, 12),
]

#: ``(pattern, flags, text, pos, endpos, mode)``.
KILLS: list[tuple[str | bytes, int, str, int, int, str]] = [
    # \b at endpos, and at a window of none.
    (r"\b", 0, "ab", 1, 1, "match"),
    (r"a\b", 0, "ab", 0, 1, "match"),
    (r"a\b", 0, "ab", 0, 2, "match"),
    (r"\b", 0, "ab", 0, 0, "search"),
    (r"\b", 0, "", 0, 0, "search"),
    # $ under MULTILINE reads the character at the position.
    ("$", re.M, "a\nb", 1, 1, "match"),
    ("$", re.M, "ab", 1, 1, "match"),
    ("$", 0, "a\n", 0, 2, "search"),
    ("$", 0, "a\n\n", 0, 3, "search"),
    (r"\Z", 0, "a\n", 0, 2, "search"),
    ("^", re.M, "a\nb", 1, 3, "search"),
    ("^", 0, "a\nb", 1, 3, "search"),
    # fullmatch backtracks past a shorter match.
    ("a|ab", 0, "ab", 0, 2, "fullmatch"),
    ("a*?", 0, "aaa", 0, 3, "fullmatch"),
    ("(?:a|ab)(?:c|bcd)", 0, "abcd", 0, 4, "fullmatch"),
    ("(a|ab)(c|bcd)", 0, "abcd", 0, 4, "match"),
    ("a*", 0, "aab", 0, 3, "fullmatch"),
    ("a*", 0, "aab", 0, 2, "fullmatch"),
    # The first start that matches, even empty, wins the search.
    ("a*", 0, "baa", 0, 3, "search"),
    ("a+", 0, "baa", 0, 3, "search"),
    # Marks and lastindex of a path that failed.
    ("(?:(a)x|a)(b)", 0, "ab", 0, 2, "match"),
    ("(a)?(?:(b)x|b)", 0, "ab", 0, 2, "match"),
    ("(?:(a)x|ab)", 0, "ab", 0, 2, "match"),
    ("((a)b)", 0, "ab", 0, 2, "match"),
    ("(a)(b)?", 0, "ab", 0, 2, "match"),
    ("(a)(b)?", 0, "a", 0, 1, "match"),
    ("(a*)(a)", 0, "aaa", 0, 3, "match"),
    ("(a*?)(a)", 0, "aaa", 0, 3, "match"),
    # Lazy against greedy single-unit repeats.
    ("a*?", 0, "aaa", 0, 3, "match"),
    ("a*", 0, "aaa", 0, 3, "match"),
    ("a{2,}?", 0, "aaaa", 0, 4, "match"),
    ("a{2,3}", 0, "aaaa", 0, 4, "match"),
    ("a+?b", 0, "aaab", 0, 4, "match"),
    ("(?:ab)*?b", 0, "ababb", 0, 5, "match"),
    ("(?:ab)*", 0, "ababa", 0, 5, "match"),
    # Clamped windows.
    ("a", 0, "ba", -5, 99, "search"),
    ("", 0, "ab", 9, 9, "match"),
    # Reversed windows: in the Python file.
    ("", 0, "abc", 2, 1, "match"),
    ("a*", 0, "ab\nc", 2, 1, "match"),
    ("(?:ab)*", 0, "ab\nc", 2, 1, "match"),
    ("$", re.M, "ab\nc", 2, 1, "match"),
    (r"\b", 0, "ab\nc", 2, 1, "match"),
    (r"\b", 0, "ab", 1, 0, "match"),
    ("(?i:a)*", 0, "ab\nc", 2, 1, "match"),
    ("(?:c)?", 0, "ab\nc", 2, 1, "match"),
    ("(c)?", 0, "ab\nc", 2, 1, "match"),
    ("(?:cd)?", 0, "ab\nc", 2, 1, "match"),
    ("", 0, "abc", 2, 1, "fullmatch"),
    ("", 0, "abc", 2, 1, "search"),
    (rb"(?:[ \t\n\r]+|[#;][^\n]*)*", 0, "ab\ncd", 2, 1, "match"),
]


def _compiled(pattern: str | bytes, flags: int) -> tuple[Any, re.Pattern[Any]]:
    return P.parse(pattern, flags), re.compile(pattern, flags)


def _answer(m: re.Match[Any] | None, groups: int) -> str:
    if m is None:
        return "-"
    spans = [m.span(g) for g in range(groups + 1)]
    last = -1 if m.lastindex is None else m.lastindex
    return "|".join(f"{a},{b}" for a, b in spans) + f"|{last}"


def _windows(n: int, short: int) -> list[tuple[int, int]]:
    if n <= short:
        return [(p, e) for p in range(-1, n + 2) for e in range(-1, n + 2)]
    return [(0, n), (1, n), (0, n - 1), (1, n - 1)]


def _reversed(n: int, pos: int, endpos: int) -> bool:
    return min(max(pos, 0), n) > min(max(endpos, 0), n)


def _text(pattern: str | bytes, chars: tuple[str, ...]) -> Any:
    text = "".join(chars)
    return text.encode("latin-1") if isinstance(pattern, bytes) else text


def _head(pattern: str | bytes, flags: int) -> dict[str, Any]:
    tree, compiled = _compiled(pattern, flags)
    text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
    return {
        "pattern": spell(text),
        "bytes": isinstance(pattern, bytes),
        "tree": _tree(tree),
        "flags": tree.state.flags,
        "groups": compiled.groups,
    }


def _sweep(
    case: tuple[str | bytes, int, str, int, int], reversed_: bool
) -> dict[str, Any]:
    pattern, flags, alphabet, short, long = case
    _, compiled = _compiled(pattern, flags)
    groups = compiled.groups
    digest = hashlib.sha256()
    count = 0
    for n in range(long + 1):
        for chars in itertools.product(alphabet, repeat=n):
            text = _text(pattern, chars)
            for pos, endpos in _windows(n, short):
                if _reversed(n, pos, endpos) != reversed_:
                    continue
                line = " ".join(
                    _answer(call(text, pos, endpos), groups)
                    for call in (compiled.match, compiled.fullmatch, compiled.search)
                )
                digest.update((line + "\n").encode("ascii"))
                count += 1
    return {
        "part": "exec.sweep",
        **_head(pattern, flags),
        "alphabet": [ord(ch) for ch in alphabet],
        "short": short,
        "long": long,
        "reversed": reversed_,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def _kill(case: tuple[str | bytes, int, str, int, int, str]) -> dict[str, Any]:
    pattern, flags, text, pos, endpos, mode = case
    _, compiled = _compiled(pattern, flags)
    subject: Any = text.encode("latin-1") if isinstance(pattern, bytes) else text
    m = getattr(compiled, mode)(subject, pos, endpos)
    return {
        "part": "exec.kill",
        **_head(pattern, flags),
        "text": [ord(ch) for ch in text],
        "pos": pos,
        "endpos": endpos,
        "mode": mode,
        "answer": _answer(m, compiled.groups),
    }


def _folds(pattern: str | bytes, flags: int) -> bool:
    tree = P.parse(pattern, flags)
    return _ignorecase(tree, tree.state.flags)


def _kill_reversed(case: tuple[str | bytes, int, str, int, int, str]) -> bool:
    _, _, text, pos, endpos, _ = case
    return _reversed(len(text), pos, endpos)


def rows() -> list[dict[str, Any]]:
    out = [_sweep(case, False) for case in PATTERNS if not _folds(case[0], case[1])]
    out += [
        _kill(case)
        for case in KILLS
        if not _kill_reversed(case) and not _folds(case[0], case[1])
    ]
    return out


def unicode_rows() -> list[dict[str, Any]]:
    """The ordered windows of the patterns under IGNORECASE."""
    out = [_sweep(case, False) for case in PATTERNS if _folds(case[0], case[1])]
    out += [
        _kill(case)
        for case in KILLS
        if not _kill_reversed(case) and _folds(case[0], case[1])
    ]
    return out


def python_rows() -> list[dict[str, Any]]:
    """The reversed windows, whose answers are the release's."""
    out = [_sweep(case, True) for case in PATTERNS]
    out += [_kill(case) for case in KILLS if _kill_reversed(case)]
    return out


def generate() -> dict[str, bytes]:
    body = rows()
    head = {"part": "header", "fixture": "pyre_exec", "rows": len(body)}
    return {"pyre_exec.jsonl": jsonl([head, *body])}
