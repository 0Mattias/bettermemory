"""Fixtures: ``re.Pattern`` and ``re.Match`` as ``re`` answers them (unit
pyre-6a, ``native/crates/bettermemory-core/src/pyre/regex.rs``).

A pattern is written as `gen_pyre_tree.spell` writes it, with ``flags``
as passed to ``re.compile``, ``sre_flags`` (``Pattern.flags``) and
``groups``. One call's answer is written by `_answer`:

- ``match``, ``fullmatch`` and ``search``: ``-`` for no match, otherwise
  the spans of the match and of each group (``-1,-1`` for a group that
  took no part) and ``lastindex`` (``-1`` for ``None``), joined by ``|``;
- ``finditer``: those of each match, joined by ``;`` inside ``[]``;
- ``findall``: each item inside ``[]``, joined by ``,``: a piece as ``'``
  and its code points joined by ``.``; a tuple as its pieces joined by
  ``/`` inside ``()``.

A kill also writes, for a match, ``pos``, ``endpos`` and ``lastgroup``
(`_detail`).

Parts:

- ``api.sweep``: one pattern of the reference (the first arguments of its
  calls of ``re`` that are one literal, `gen_pyre_atom._literals`, that the
  engine builds and that hold no lookaround, the first ``INVENTORY``) over
  every text over ``alphabet`` (the pattern's first literal code points)
  of at most ``long`` code points, in `itertools.product` order, through
  the windows of `gen_pyre_exec._windows`: the count of windows and the
  SHA-256 over the lines of the five answers joined by a space.
  ``pyre_api.jsonl`` digests the ordered windows; the reversed ones
  (`gen_pyre_exec._reversed`), whose ``match`` and ``fullmatch`` answers
  are 3.13.13's, are digested in ``pyre_python.jsonl``.
- ``api.fuzz``: the patterns `gen_pyre_parse._fuzz_case` draws from
  ``SEED``, in order: the first ``FUZZ_ACCEPTED`` the engine builds, each
  with the first 16 hex digits of the SHA-256 of its answers over every
  text over ``ab`` of at most ``FUZZ_LONG`` code points and every ordered
  window in ``[0, n]``; and the first ``FUZZ_REFUSED`` it refuses or
  ``re`` rejects, each with the name of the refusal or the error
  (`gen_pyre_parse._failed_full`).
- ``api.kill``: one call written out, for the cases a plausible wrong
  implementation fails: an empty match and the attempt after it, a group
  that took no part (``None`` against ``''``), ``lastgroup``, clamped and
  reversed windows.

A pattern under IGNORECASE reads the fold tables, so its rows are in
``pyre_unicode.jsonl``, with the sweeps whose alphabet goes past ASCII.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import re
from typing import Any

from ._common import jsonl
from .gen_pyre_atom import _literals
from .gen_pyre_exec import _reversed, _windows
from .gen_pyre_lower import _answer as _lowered
from .gen_pyre_lower import _ignorecase
from .gen_pyre_parse import SEED, _failed_full, _fuzz_case, _parsed
from .gen_pyre_tree import spell

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")

INVENTORY = 60
FUZZ_ACCEPTED = 300
FUZZ_REFUSED = 100
FUZZ_LONG = 4
METHODS = ("match", "fullmatch", "search", "finditer", "findall")

#: ``(pattern, flags, text, pos, endpos, method)``.
KILLS: list[tuple[str | bytes, int, str, int, int, str]] = [
    # The attempt after an empty match goes on backtracking.
    ("a*?", 0, "aab", 0, 3, "finditer"),
    ("a*?", 0, "aab", 0, 3, "findall"),
    ("a??", 0, "aa", 0, 2, "findall"),
    ("(?:ab)*?", 0, "abab", 0, 4, "findall"),
    ("a|", 0, "ab", 0, 2, "findall"),
    ("|a", 0, "ab", 0, 2, "findall"),
    ("|a", 0, "ab", 0, 2, "finditer"),
    ("b*|a", 0, "ab", 0, 2, "findall"),
    ("x*", 0, "", 0, 0, "findall"),
    ("x*", 0, "ab", 0, 2, "finditer"),
    ("a*", 0, "baaa", 0, 4, "findall"),
    ("", 0, "abc", 1, 2, "finditer"),
    ("$", re.M, "a\nb\n", 0, 4, "finditer"),
    (r"\b", 0, "ab cd", 0, 5, "finditer"),
    ("^", re.M, "a\nb", 0, 3, "finditer"),
    ("(?:a|)b*", 0, "aabb", 0, 4, "findall"),
    ("(a)??", 0, "aa", 0, 2, "finditer"),
    # findall's shape and a group that took no part.
    ("(a)|b", 0, "ab", 0, 2, "findall"),
    ("(a)|(b)", 0, "ab", 0, 2, "findall"),
    ("(a)(b)?", 0, "aab", 0, 3, "findall"),
    ("((a)b)", 0, "abab", 0, 4, "findall"),
    ("(a)|b", 0, "ab", 0, 2, "finditer"),
    ("(?:(a)|b)?b", 0, "ab", 0, 2, "findall"),
    ("(?:(a)|b)?b", 0, "bab", 0, 3, "finditer"),
    ("()", 0, "ab", 0, 2, "findall"),
    ("(a?)", 0, "ab", 0, 2, "findall"),
    (b"(a)|b", 0, "ab", 0, 2, "findall"),
    (b"a*", 0, "aba", 0, 3, "findall"),
    # lastgroup and lastindex.
    ("(?P<x>a)(?P<y>b)?", 0, "ab", 0, 2, "match"),
    ("(?P<x>a)(?P<y>b)?", 0, "a", 0, 1, "match"),
    ("(?P<x>a)(b)", 0, "ab", 0, 2, "match"),
    ("((?P<in>a))", 0, "a", 0, 1, "match"),
    ("(?P<x>(a))", 0, "a", 0, 1, "match"),
    ("(?P<x>a)|(?P<y>b)", 0, "b", 0, 1, "search"),
    # Clamped windows; Match.pos and Match.endpos are the clamped values.
    ("a", 0, "xa", -5, 99, "search"),
    ("a", 0, "xa", -5, 99, "finditer"),
    ("a*", 0, "aaa", 1, 2, "findall"),
    ("a*", 0, "aaa", 3, 3, "finditer"),
    ("a*", 0, "aaa", 9, 9, "findall"),
    ("a$", 0, "aab", 0, 2, "findall"),
    # Reversed windows: in the Python file.
    ("", 0, "abc", 2, 1, "finditer"),
    ("", 0, "abc", 2, 1, "findall"),
    ("", 0, "abc", 2, 1, "match"),
    ("a*", 0, "aaa", 2, 0, "findall"),
]


def _compiled(pattern: str | bytes, flags: int) -> re.Pattern[Any]:
    return re.compile(pattern, flags)


def _spans(m: re.Match[Any], groups: int) -> str:
    spans = [m.span(g) for g in range(groups + 1)]
    last = -1 if m.lastindex is None else m.lastindex
    return "|".join(f"{a},{b}" for a, b in spans) + f"|{last}"


def _piece(item: str | bytes) -> str:
    codes = item if isinstance(item, bytes) else [ord(ch) for ch in item]
    return "'" + ".".join(str(c) for c in codes)


def _item(item: Any) -> str:
    if isinstance(item, tuple):
        return "(" + "/".join(_piece(part) for part in item) + ")"
    return _piece(item)


def _detail(m: re.Match[Any], groups: int) -> str:
    return _spans(m, groups) + f"|{m.pos},{m.endpos}|{m.lastgroup}"


def _answer(
    compiled: re.Pattern[Any], method: str, text: Any, pos: int, endpos: int, show: Any
) -> str:
    groups = compiled.groups
    if method == "finditer":
        found = compiled.finditer(text, pos, endpos)
        return "[" + ";".join(show(m, groups) for m in found) + "]"
    if method == "findall":
        items = compiled.findall(text, pos, endpos)
        return "[" + ",".join(_item(item) for item in items) + "]"
    m = getattr(compiled, method)(text, pos, endpos)
    return "-" if m is None else show(m, groups)


def _line(compiled: re.Pattern[Any], text: Any, pos: int, endpos: int) -> str:
    return " ".join(
        _answer(compiled, method, text, pos, endpos, _spans) for method in METHODS
    )


def _text(pattern: str | bytes, chars: Any) -> Any:
    text = "".join(chars)
    return text.encode("latin-1") if isinstance(pattern, bytes) else text


def _head(pattern: str | bytes, flags: int) -> dict[str, Any]:
    compiled = _compiled(pattern, flags)
    text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
    row: dict[str, Any] = {"pattern": spell(text), "bytes": isinstance(pattern, bytes)}
    if flags:
        row["flags"] = flags
    row["sre_flags"] = compiled.flags
    row["groups"] = compiled.groups
    return row


def _has_look(data: Any) -> bool:
    for op, av in data:
        if op in (C.ASSERT, C.ASSERT_NOT):
            return True
        if op is C.BRANCH:
            if any(_has_look(body) for body in av[1]):
                return True
        elif op is C.SUBPATTERN:
            if _has_look(av[3]):
                return True
        elif op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
            if _has_look(av[2]):
                return True
        elif op is C.ATOMIC_GROUP and _has_look(av):
            return True
    return False


def _builds(pattern: str | bytes, flags: int) -> bool:
    """Whether the engine builds the pattern: the port's parse accepts it,
    lowering accepts it, and it holds no lookaround."""
    try:
        tree = _parsed(pattern, flags)
    except Exception:  # noqa: BLE001 - every failure is a refusal here
        return False
    is_bytes = isinstance(pattern, bytes)
    lowered = _lowered(tree, tree.state.flags, is_bytes)
    return lowered.startswith("=") and not _has_look(tree.data)


def _folds(pattern: str | bytes, flags: int) -> bool:
    tree = P.parse(pattern, flags)
    return _ignorecase(tree, tree.state.flags)


def _unicode(pattern: str | bytes) -> bool:
    """Whether a sweep reads a Unicode table: its pattern folds case, or
    its alphabet goes past ASCII (where a category or a boundary reads
    the tables)."""
    return _folds(pattern, 0) or any(ord(ch) > 127 for ch in _alphabet(pattern))


def _alphabet(pattern: str | bytes) -> str:
    """The first two or three distinct literal code points of the tree,
    depth first, padded from ``ab``."""
    found: list[int] = []

    def walk(data: Any) -> None:
        for op, av in data:
            if op is C.LITERAL:
                found.append(av)
            elif op is C.IN:
                found.extend(a for o, a in av if o is C.LITERAL)
                found.extend(a[0] for o, a in av if o is C.RANGE)
            elif op is C.BRANCH:
                for body in av[1]:
                    walk(body)
            elif op is C.SUBPATTERN:
                walk(av[3])
            elif op in (C.MAX_REPEAT, C.MIN_REPEAT):
                walk(av[2])

    walk(P.parse(pattern, 0).data)
    chars: list[str] = []
    for c in [*found, ord("a"), ord("b")]:
        if chr(c) not in chars:
            chars.append(chr(c))
    return "".join(chars[:3])


def _inventory() -> list[str | bytes]:
    return [p for p in _literals() if _builds(p, 0)][:INVENTORY]


def _sweep(pattern: str | bytes, reversed_: bool) -> dict[str, Any]:
    compiled = _compiled(pattern, 0)
    alphabet = _alphabet(pattern)
    short, long = (6, 10) if len(alphabet) == 2 else (4, 7)
    digest = hashlib.sha256()
    count = 0
    for n in range(long + 1):
        for chars in itertools.product(alphabet, repeat=n):
            text = _text(pattern, chars)
            for pos, endpos in _windows(n, short):
                if _reversed(n, pos, endpos) != reversed_:
                    continue
                digest.update(
                    (_line(compiled, text, pos, endpos) + "\n").encode("ascii")
                )
                count += 1
    return {
        "part": "api.sweep",
        **_head(pattern, 0),
        "alphabet": [ord(ch) for ch in alphabet],
        "short": short,
        "long": long,
        "reversed": reversed_,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def _fuzz_digest(pattern: str | bytes, flags: int) -> str:
    compiled = _compiled(pattern, flags)
    digest = hashlib.sha256()
    for n in range(FUZZ_LONG + 1):
        for chars in itertools.product("ab", repeat=n):
            text = _text(pattern, chars)
            for pos in range(n + 1):
                for endpos in range(pos, n + 1):
                    line = _line(compiled, text, pos, endpos)
                    digest.update((line + "\n").encode("ascii"))
    return digest.hexdigest()[:16]


def _fuzz_cases() -> tuple[
    list[tuple[int, str | bytes, int]], list[tuple[int, str | bytes, int]]
]:
    accepted: list[tuple[int, str | bytes, int]] = []
    refused: list[tuple[int, str | bytes, int]] = []
    i = 0
    while len(accepted) < FUZZ_ACCEPTED or len(refused) < FUZZ_REFUSED:
        pattern, flags = _fuzz_case(SEED + i)
        if _builds(pattern, flags):
            if len(accepted) < FUZZ_ACCEPTED:
                accepted.append((i, pattern, flags))
        elif len(refused) < FUZZ_REFUSED and _refusal_name(pattern, flags):
            refused.append((i, pattern, flags))
        i += 1
    return accepted, refused


def _refusal_name(pattern: str | bytes, flags: int) -> str | None:
    """What the port's parse answers, for a pattern it does not accept;
    ``None`` for one it accepts (refused later, by lowering)."""
    try:
        _parsed(pattern, flags)
    except Exception as exc:  # noqa: BLE001 - the name is the answer
        return _failed_full(exc)
    return None


def _fuzz_rows(with_ignorecase: bool) -> list[dict[str, Any]]:
    accepted, refused = _fuzz_cases()
    out = []
    for i, pattern, flags in accepted:
        if _folds(pattern, flags) != with_ignorecase:
            continue
        row = {"part": "api.fuzz", "draw": i, **_head(pattern, flags)}
        row["sha16"] = _fuzz_digest(pattern, flags)
        out.append(row)
    if not with_ignorecase:
        for i, pattern, flags in refused:
            text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
            row = {
                "part": "api.fuzz",
                "draw": i,
                "pattern": spell(text),
                "bytes": isinstance(pattern, bytes),
                "refused": _refusal_name(pattern, flags),
            }
            if flags:
                row["flags"] = flags
            out.append(row)
    return out


def _kill(case: tuple[str | bytes, int, str, int, int, str]) -> dict[str, Any]:
    pattern, flags, text, pos, endpos, method = case
    compiled = _compiled(pattern, flags)
    subject: Any = text.encode("latin-1") if isinstance(pattern, bytes) else text
    return {
        "part": "api.kill",
        **_head(pattern, flags),
        "text": [ord(ch) for ch in text],
        "pos": pos,
        "endpos": endpos,
        "method": method,
        "answer": _answer(compiled, method, subject, pos, endpos, _detail),
    }


def _kill_reversed(case: tuple[str | bytes, int, str, int, int, str]) -> bool:
    _, _, text, pos, endpos, _ = case
    return _reversed(len(text), pos, endpos)


def rows() -> list[dict[str, Any]]:
    out = [_sweep(p, False) for p in _inventory() if not _unicode(p)]
    out += _fuzz_rows(False)
    out += [
        _kill(case)
        for case in KILLS
        if not _kill_reversed(case) and not _folds(case[0], case[1])
    ]
    return out


def unicode_rows() -> list[dict[str, Any]]:
    """The rows under IGNORECASE, which read the fold tables."""
    out = [_sweep(p, False) for p in _inventory() if _unicode(p)]
    out += _fuzz_rows(True)
    return out


def python_rows() -> list[dict[str, Any]]:
    """The reversed windows, whose answers are the release's."""
    out = [_sweep(p, True) for p in _inventory()]
    out += [_kill(case) for case in KILLS if _kill_reversed(case)]
    return out


def generate() -> dict[str, bytes]:
    body = rows()
    head = {"part": "header", "fixture": "pyre_api", "rows": len(body)}
    return {"pyre_api.jsonl": jsonl([head, *body])}
