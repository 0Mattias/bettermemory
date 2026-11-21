"""Fixtures: a parsed tree lowered to the engine's arena (unit pyre-4b,
``native/crates/bettermemory-core/src/pyre/lower.rs``).

Each row holds a tree as `gen_pyre_tree._item` writes one (from
``re._parser.parse`` of ``pattern`` when the row names one, otherwise
built by `gen_pyre_tree._built`), the flags ``_compile`` reads
(``re.compile(...).flags``: a str pattern carries UNICODE), ``bytes`` for
a bytes pattern, and ``answer``:

- ``=TRACE #N``: the tree lowers. ``TRACE`` is, in the order
  ``re._compiler._compile`` emits them, the opcode ``re`` itself emits
  for each repeat (REPEAT_ONE, MIN_REPEAT_ONE, or for REPEAT the
  MAX_UNTIL or MIN_UNTIL that closes it), anchor (its AT code), ANY or
  ANY_ALL, ASSERT, ASSERT_NOT and FAILURE, and for each literal under
  IGNORECASE; each item is compiled alone by ``_compile`` under the flags
  in force there and the name read from the code it emits. ``N`` is the
  size of the arena by the port's rule (`_count`).
- ``!LookbehindWidth`` and ``!LooksTooMuchBehind``: ``_compile`` raises
  "look-behind requires fixed-width pattern" or "looks too much behind".
- ``?Feature``: the engine refuses what ``re`` builds (`_refusal`, and
  ``Nodes`` past ``MAX_NODES``); ``?Nesting`` is checked before anything
  else.

Parts: ``lower.case`` (written cases), ``lower.inventory`` (the patterns of
`gen_pyre_atom._literals`, the reference's own, under no flag) and
``lower.sweep`` (``count`` trees drawn by `gen_pyre_tree._sub` from
``seed`` + i, each under ``FLAGS[i % 4]``; ``sha256`` over the lines
``answer``). A row whose flags hold IGNORECASE anywhere reads the fold
tables, so it is in ``pyre_unicode.jsonl`` (the ``lower.`` parts there),
and the sweep is split the same way: ``pyre_lower.jsonl`` digests the
trees without it, ``pyre_unicode.jsonl`` those with it.
"""

from __future__ import annotations

import hashlib
import importlib
from typing import Any

from ._common import jsonl
from .gen_pyre_atom import _literals
from .gen_pyre_tree import Lcg, _built, _sub, _tree, spell

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")
K: Any = importlib.import_module("re._compiler")

MAX_NODES = 4096
MAX_DEPTH = 32
SEED = 20261122
SWEEP = 3000
IC = int(C.SRE_FLAG_IGNORECASE)
M = int(C.SRE_FLAG_MULTILINE)
S = int(C.SRE_FLAG_DOTALL)
U = int(C.SRE_FLAG_UNICODE)
FLAGS = [U, U | M, U | S, U | IC]

#: Written cases: a pattern parsed by ``re`` (a ``bytes`` one is a bytes
#: pattern) with the flags passed to it, or a tree described as `_item`
#: writes one with its ``_compile`` flags and whether it is bytes.
PATTERNS: list[tuple[str | bytes, int]] = [
    ("a*", 0),
    ("a*?", 0),
    ("a+", 0),
    ("a{2,5}", 0),
    ("a{2,5}?", 0),
    ("x{65535}", 0),
    ("[ab]*", 0),
    ("[^a]*", 0),
    (".*", 0),
    (".*", S),
    ("(?s:.)*", 0),
    ("(?m:a)*", 0),
    ("(?:a|b)*", 0),
    ("(?:a|bc)*", 0),
    ("(a)*", 0),
    ("(a)?", 0),
    ("(?:ab)*", 0),
    ("(?:ab)*?", 0),
    ("(?:ab)+", 0),
    ("(?:ab)?", 0),
    ("(?:ab)??", 0),
    ("(?:ab){3}", 0),
    ("(?:ab){2,4}", 0),
    ("(?:ab){2,4}?", 0),
    ("(?:ab){2,}", 0),
    ("(?:a*)?", 0),
    ("(?:a*)*", 0),
    ("(?:a?b?)+", 0),
    ("(?:a?b?){2}", 0),
    ("(?:a?b?){0,1}", 0),
    ("(?:(?=a))*", 0),
    ("(?:a*)*(?<=a*)", 0),
    ("(?:(a)b)*", 0),
    ("^a$", 0),
    ("^a$", M),
    (r"\Aa\Z", 0),
    (r"\ba\b", 0),
    (r"\B", 0),
    ("a|b|c", 0),
    ("ab|cd|ef", 0),
    ("(?=a)b", 0),
    ("(?!a)b", 0),
    ("(?<=ab)c", 0),
    ("(?<!a|b)c", 0),
    ("(?<=a|bc)d", 0),
    ("(?<=a*)b", 0),
    ("(?<=(?:a{65535}){65537})b", 0),
    ("(?<=(?:a{65535}){65538})b", 0),
    ("(?:ab){2047}", 0),
    ("(?:ab){2047}c", 0),
    ("(?:ab){2047}cd", 0),
    ("(?:ab){2048}", 0),
    ("(?:xy){0,65535}", 0),
    ("(?:(?:(?:ab){0,30}c){0,30}d){0,30}", 0),
    ("(?a)a", 0),
    ("(?a:b)c", 0),
    (b"a*", 0),
    (rb"\b", 0),
    (rb"\d", 0),
    (rb"[\s]", 0),
    (b"(?i)a", 0),
    (b"(?i:a)", 0),
    (b"(?L)a", 0),
    (b"a", int(C.SRE_FLAG_ASCII)),
    # IGNORECASE: in the Unicode file.
    ("(?i)a", 0),
    ("(?i)1", 0),
    ("(?i)k", 0),
    ("(?i)[^a]", 0),
    ("(?i)[^k]", 0),
    ("(?i:a)*", 0),
    ("(?i)é+?", 0),
    ("(?i)İ", 0),
    ("(?i)\U00010400", 0),
    ("(?i)(?-i:a)b", 0),
]


def _deep(levels: int, inner: list[Any]) -> list[Any]:
    for _ in range(levels):
        inner = [["group", None, M, 0, inner]]
    return inner


BUILT: list[tuple[list[Any], int, bool]] = [
    # Group 0 is not None (so not `_simple`) and marks nothing.
    ([["max", 0, None, [["group", 0, 0, 0, [["lit", 97]]]]]], U, False),
    ([["group", 0, 0, 0, [["lit", 97]]]], U, False),
    ([["group", 1, 0, 0, [["lit", 97]]]], U, False),
    ([["branch", []]], U, False),
    ([["branch", [[["lit", 97], ["lit", 98]]]]], U, False),
    ([["failure"]], U, False),
    ([["assert_not", 1, []]], U, False),
    ([["max", 3, 2, [["lit", 97], ["lit", 98]]]], U, False),
    ([["min", 4294967294, 1, []]], U, False),
    ([["max", 0, 1, []]], U, False),
    ([["max", 2, 2, [["at", "AT_END"]]]], U, False),
    ([["min", 0, 2, [["failure"]]]], U, False),
    ([["max", 0, None, [["group", None, 0, 0, []]]]], U, False),
    (
        [["max", 0, None, [["group", None, M, 0, [["group", None, S, 0, [["any"]]]]]]]],
        U,
        False,
    ),
    ([["max", 0, None, [["group", None, 0, S, [["any"]]]]]], U | S, False),
    (_deep(32, [["lit", 97]]), U, False),
    (_deep(33, [["lit", 97]]), U, False),
    (_deep(40, [["assert", -1, [["max", 0, None, [["lit", 97]]]]]]), U, False),
    ([["lit", 97]], 0, False),
    ([["lit", 97]], U | int(C.SRE_FLAG_LOCALE), False),
    ([["at", "AT_BOUNDARY"]], 0, True),
]


def _bodies(op: Any, av: Any) -> list[Any]:
    if op is C.BRANCH:
        return list(av[1])
    if op is C.SUBPATTERN:
        return [av[3]]
    if op is C.MAX_REPEAT or op is C.MIN_REPEAT:
        return [av[2]]
    if op is C.ASSERT or op is C.ASSERT_NOT:
        return [av[1]]
    return []


def _depth(data: Any) -> int:
    deepest = 0
    for op, av in data:
        for body in _bodies(op, av):
            deepest = max(deepest, 1 + _depth(body))
    return deepest


def _ignorecase(data: Any, flags: int) -> bool:
    """Whether IGNORECASE is in force anywhere: the row reads the fold."""
    if flags & IC:
        return True
    for op, av in data:
        inner = K._combine_flags(flags, av[1], av[2]) if op is C.SUBPATTERN else flags
        if any(_ignorecase(body, inner) for body in _bodies(op, av)):
            return True
    return False


def _flag_refusal(flags: int, is_bytes: bool) -> str | None:
    if flags & C.SRE_FLAG_LOCALE:
        return "Locale"
    if flags & C.SRE_FLAG_ASCII or (not is_bytes and not flags & U):
        return "Ascii"
    if is_bytes and flags & IC:
        return "BytesIgnorecase"
    return None


def _refusal(data: Any, flags: int, is_bytes: bool) -> str | None:
    """The engine's refusals, in the order `_compile` walks the tree."""
    found = _flag_refusal(flags, is_bytes)
    if found:
        return found
    for op, av in data:
        found = None
        if op is C.IN:
            if is_bytes and any(m[0] is C.CATEGORY for m in av):
                return "BytesCategory"
        elif op is C.AT:
            if av is C.AT_NON_BOUNDARY:
                return "NotBoundary"
            if av is C.AT_BOUNDARY and is_bytes:
                return "BytesBoundary"
        elif op is C.SUBPATTERN:
            found = _refusal(av[3], K._combine_flags(flags, av[1], av[2]), is_bytes)
        elif op is C.MAX_REPEAT or op is C.MIN_REPEAT:
            if not K._simple(av[2]) and av[1] > 1 and av[2].getwidth()[0] == 0:
                return "NullableLoop"
            found = _refusal(av[2], flags, is_bytes)
        else:
            for body in _bodies(op, av):
                found = found or _refusal(body, flags, is_bytes)
        if found:
            return found
    return None


def _name(code: list[int]) -> str:
    return str(C.OPCODES[code[0]])


def _trace(data: Any, flags: int) -> list[str]:
    """What `_compile` emits, item by item, as the module text says."""
    out: list[str] = []
    for item in data:
        op, av = item
        code: list[int] = []
        if op in (C.LITERAL, C.NOT_LITERAL):
            if flags & IC:
                K._compile(code, [item], flags)
                out.append(_name(code))
        elif op is C.AT:
            K._compile(code, [item], flags)
            out.append(str(C.ATCODES[code[1]]))
        elif op in (C.ANY, C.FAILURE, C.ASSERT, C.ASSERT_NOT):
            K._compile(code, [item], flags)
            out.append(_name(code))
            if op is not C.ANY and op is not C.FAILURE:
                out += _trace(av[1], flags)
        elif op is C.MAX_REPEAT or op is C.MIN_REPEAT:
            K._compile(code, [item], flags)
            name = _name(code)
            out.append(str(C.OPCODES[code[-1]]) if name == "REPEAT" else name)
            out += _trace(av[2], flags)
        elif op is C.SUBPATTERN:
            out += _trace(av[3], K._combine_flags(flags, av[1], av[2]))
        elif op is C.BRANCH:
            for alternative in av[1]:
                out += _trace(alternative, flags)
        else:
            assert op is C.IN, op
    return out


def _count(data: Any) -> int:
    """The arena the port builds, node by node (`lower.rs`)."""
    total = 0
    for op, av in data:
        if op is C.BRANCH:
            alts = av[1]
            total += 1 if not alts else len(alts) - 1 + sum(_count(a) for a in alts)
        elif op is C.SUBPATTERN:
            total += (2 if av[0] else 0) + _count(av[3])
        elif op is C.MAX_REPEAT or op is C.MIN_REPEAT:
            low, high, body = av
            if K._simple(body):
                total += 1
                continue
            size = _count(body)
            if high == C.MAXREPEAT:
                total += low * size + 1 + size
            else:
                total += low * size + max(0, high - low) * (size + 1)
        elif op is C.ASSERT or op is C.ASSERT_NOT:
            total += 2 + _count(av[1])
        else:
            total += 1
    return total


def _answer(tree: Any, flags: int, is_bytes: bool) -> str:
    if _depth(tree) > MAX_DEPTH:
        return "?Nesting"
    try:
        K._compile([], tree, flags)
    except C.error as exc:
        return {
            "look-behind requires fixed-width pattern": "!LookbehindWidth",
            "looks too much behind": "!LooksTooMuchBehind",
        }[exc.msg]
    found = _refusal(tree, flags, is_bytes)
    if found:
        return "?" + found
    size = 1 + _count(tree)
    if size > MAX_NODES:
        return "?Nodes"
    return "=" + " ".join(_trace(tree, flags)) + f" #{size}"


def _case(tree: Any, flags: int, is_bytes: bool, **extra: Any) -> dict[str, Any]:
    return {
        "part": "lower.case",
        **extra,
        "tree": _tree(tree),
        "flags": flags,
        "bytes": is_bytes,
        "answer": _answer(tree, flags, is_bytes),
    }


def _cases() -> list[tuple[bool, dict[str, Any]]]:
    out = []
    for pattern, passed in PATTERNS:
        tree = P.parse(pattern, passed)
        flags = tree.state.flags
        is_bytes = isinstance(pattern, bytes)
        text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
        row = _case(tree, flags, is_bytes, pattern=spell(text))
        out.append((_ignorecase(tree, flags), row))
    for items, flags, is_bytes in BUILT:
        tree = _built(items, P.State())
        out.append((_ignorecase(tree, flags), _case(tree, flags, is_bytes)))
    return out


def _sweep(with_ignorecase: bool) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for i in range(SWEEP):
        tree = _sub(Lcg(SEED + i).draw, P.State(), 0)
        flags = FLAGS[i % 4]
        if _ignorecase(tree, flags) != with_ignorecase:
            continue
        digest.update((_answer(tree, flags, False) + "\n").encode("ascii"))
        count += 1
    return {
        "part": "lower.sweep",
        "seed": SEED,
        "draws": SWEEP,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def _inventory() -> list[dict[str, Any]]:
    rows = []
    for pattern in _literals():
        tree = P.parse(pattern, 0)
        is_bytes = isinstance(pattern, bytes)
        flags = tree.state.flags
        rows.append(
            {
                "part": "lower.inventory",
                "tree": _tree(tree),
                "flags": flags,
                "bytes": is_bytes,
                "answer": _answer(tree, flags, is_bytes),
            }
        )
    return rows


def unicode_rows() -> list[dict[str, Any]]:
    """The rows under IGNORECASE, for ``pyre_unicode.jsonl``."""
    return [row for folds, row in _cases() if folds] + [_sweep(True)]


def generate() -> dict[str, bytes]:
    rows = [row for folds, row in _cases() if not folds]
    rows += [*_inventory(), _sweep(False)]
    head = {
        "part": "header",
        "fixture": "pyre_lower",
        "rows": len(rows),
        "max_nodes": MAX_NODES,
        "max_depth": MAX_DEPTH,
    }
    return {"pyre_lower.jsonl": jsonl([head, *rows])}
