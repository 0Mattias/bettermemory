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

Unit pyre-6b adds ``sub``, ``subn`` and ``split`` (``pyre/replace.rs``):

- ``api.replace``: one call written out (`REPLACE_KILLS`): ``subn`` with a
  literal, ``subn_fn`` with the function `_swap`, or ``split``, with a
  count. ``subn`` answers the result as a piece (`_piece`) and the count,
  joined by ``|``; ``split`` each item inside ``[]`` joined by ``,``, a
  group that took no part as ``None``. A literal holding a backslash is a
  template in the reference and refused by the engine: such a row holds
  ``refused`` and, under ``reference``, the reference's answer or ``!``
  and the name of its exception.
- ``api.replace_sweep``: the accepted ``api.fuzz`` patterns of this file,
  in order, each over every text over ``ab`` of at most ``FUZZ_LONG``
  code points and each count of ``REPLACE_COUNTS``: the count of calls
  and the SHA-256 over the lines of the three answers (``subn`` with
  ``<>``, ``subn_fn``, ``split``) joined by a space.

`_swap`, the function replacement, models the group swap of the
reference's function replacements: ``None`` for an empty match, else the
groups from the last to the first (``''`` for one that took no part), or
with no group the match's text reversed.

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


REPLACE_COUNTS = (0, 1, 2, -1)

#: ``(pattern, text, method, repl, count)``; ``repl`` is ``None`` for
#: ``subn_fn``.
REPLACE_KILLS: list[tuple[str | bytes, str, str, str | None, int]] = [
    # Empty matches, adjacent to non-empty ones, and the attempt after one.
    ("x*", "abc", "subn", "-", 0),
    ("a*", "baaac", "subn", "-", 0),
    ("a*?", "aab", "subn", "-", 0),
    ("|a", "ab", "subn", "-", 0),
    ("b*|a", "ab", "subn", "-", 0),
    ("", "", "subn", "-", 0),
    ("", "abc", "subn", "", 0),
    ("x*", "abc", "split", None, 0),
    ("a*", "baaac", "split", None, 0),
    ("", "abc", "split", None, 0),
    ("", "", "split", None, 0),
    ("a*?", "aab", "split", None, 0),
    ("|a", "ab", "split", None, 0),
    ("$", "a\nb\n", "split", None, 0),
    # The count: 0 is no bound, a negative count finds nothing.
    ("a", "aaaa", "subn", "-", 1),
    ("a", "aaaa", "subn", "-", 3),
    ("a", "aaaa", "subn", "-", 9),
    ("a", "aaaa", "subn", "-", -1),
    ("x*", "abc", "subn", "-", 2),
    ("a", "aaaa", "split", None, 1),
    ("a", "aaaa", "split", None, 2),
    ("a", "aaaa", "split", None, -1),
    ("x*", "abc", "split", None, 1),
    ("a", "bab", "split", None, 0),
    ("a", "a", "split", None, 0),
    # Groups in split: None for a group that took no part.
    ("(a)|b", "abc", "split", None, 0),
    ("(a)(x)?", "bab", "split", None, 0),
    ("(?:(a)|b)", "ab", "split", None, 1),
    ("()", "ab", "split", None, 0),
    # Function replacement: the group swap and None.
    ("(a)(b)", "xabyab", "subn_fn", None, 0),
    ("(a)(b)?", "aab", "subn_fn", None, 0),
    ("(a)|(b)", "ab", "subn_fn", None, 0),
    ("ab|", "abc", "subn_fn", None, 0),
    ("a*", "baac", "subn_fn", None, 0),
    ("(a)(b)", "abab", "subn_fn", None, 1),
    # Bytes.
    (b"a*", "baac", "subn", "-", 0),
    (b"(a)|b", "abc", "split", None, 0),
    (b"(a)(b)", "abab", "subn_fn", None, 0),
    # A literal with a backslash: a template in the reference, refused.
    ("a", "ab", "subn", "\\n", 0),
    ("(a)", "ab", "subn", "<\\1>", 0),
    ("a", "ab", "subn", "\\2", 0),
    ("a", "b", "subn", "\\", 0),
    (b"a", "ab", "subn", "\\g<0>", 0),
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


def _swap(m: re.Match[Any]) -> Any:
    """The function replacement of the fixtures (see the module text)."""
    if m.start() == m.end():
        return None
    empty = m.string[:0]
    if m.re.groups:
        return empty.join(m.group(g) or empty for g in range(m.re.groups, 0, -1))
    return m.group(0)[::-1]


def _replaced(
    compiled: re.Pattern[Any], method: str, repl: Any, text: Any, count: int
) -> str:
    if method == "split":
        items = compiled.split(text, count)
        return "[" + ",".join("None" if x is None else _piece(x) for x in items) + "]"
    out, n = compiled.subn(_swap if method == "subn_fn" else repl, text, count)
    return f"{_piece(out)}|{n}"


def _replace_kill(
    case: tuple[str | bytes, str, str, str | None, int],
) -> dict[str, Any]:
    pattern, text, method, repl, count = case
    compiled = _compiled(pattern, 0)
    as_bytes = isinstance(pattern, bytes)
    subject: Any = text.encode("latin-1") if as_bytes else text
    row: dict[str, Any] = {
        "part": "api.replace",
        **_head(pattern, 0),
        "text": [ord(ch) for ch in text],
        "method": method,
        "count": count,
    }
    if repl is not None:
        row["repl"] = [ord(ch) for ch in repl]
    literal: Any = repl.encode("latin-1") if as_bytes and repl is not None else repl
    try:
        answer = _replaced(compiled, method, literal, subject, count)
    except re.error:
        # ``re.error`` is named ``PatternError`` from 3.13, so the row
        # names the class as every release can.
        answer = "!re.error"
    if repl is not None and "\\" in repl:
        row["refused"] = "Template"
        row["reference"] = answer
    else:
        row["answer"] = answer
    return row


def _replace_sweep() -> dict[str, Any]:
    accepted, _ = _fuzz_cases()
    digest = hashlib.sha256()
    count = 0
    for _, pattern, flags in accepted:
        if _folds(pattern, flags):
            continue
        compiled = _compiled(pattern, flags)
        repl: Any = b"<>" if isinstance(pattern, bytes) else "<>"
        for n in range(FUZZ_LONG + 1):
            for chars in itertools.product("ab", repeat=n):
                text = _text(pattern, chars)
                for c in REPLACE_COUNTS:
                    line = " ".join(
                        _replaced(compiled, method, repl, text, c)
                        for method in ("subn", "subn_fn", "split")
                    )
                    digest.update((line + "\n").encode("ascii"))
                    count += 1
    return {
        "part": "api.replace_sweep",
        "counts": list(REPLACE_COUNTS),
        "long": FUZZ_LONG,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def rows() -> list[dict[str, Any]]:
    out = [_sweep(p, False) for p in _inventory() if not _unicode(p)]
    out += _fuzz_rows(False)
    out += [
        _kill(case)
        for case in KILLS
        if not _kill_reversed(case) and not _folds(case[0], case[1])
    ]
    out += [_replace_kill(case) for case in REPLACE_KILLS]
    out.append(_replace_sweep())
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
