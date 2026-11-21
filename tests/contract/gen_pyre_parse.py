"""Fixtures: the repeats `re._parser._parse` reads and
`SubPattern.getwidth` (the ``parse.*`` parts of ``pyre.jsonl`` and
``pyre_python.jsonl``, unit pyre-3).

The native build reads them in
``native/crates/bettermemory-core/src/pyre/parse.rs`` (the repeat branch
of `_parse`, through `parse_flat`) and ``tree.rs`` (`getwidth`). The
reference here is the interpreter's own ``re`` package, run with the hooks
of `gen_pyre_atom._hooked`, which refuse a group, an alternative and a
possessive repeat where the port does.

An answer that holds no tree text is the same under every release and is
in ``pyre.jsonl``: the width of the tree as ``=lo,hi`` (decimal), or
``!Name@pos`` for an error of ``re``, ``?Feature@pos`` for a refusal, and
``%Overflow`` for the OverflowError "the repetition number is too large",
and ``%DigitLimit`` for the ValueError of ``int`` on a bound of more than
4,300 digits, neither of which is ``re.error`` or has a position
(`_outcome`). The tree's
``str(tree)`` names the opcodes of ``re._constants``, so it follows the
release and is in ``pyre_python.jsonl``.

Parts, by ``part``:

- ``parse.repeat`` (``pyre.jsonl``): ``pattern`` (a bytes pattern under
  ``bytes``) and its ``answer`` (`_outcome`).
- ``parse.tree`` (``pyre_python.jsonl``): each pattern of ``REPEATS`` that
  parses, and ``dump``, its ``str(tree)``.
- ``parse.width`` (``pyre.jsonl``): a tree built from ``tree`` as
  `gen_pyre_tree._built` builds one, for shapes no flat pattern parses to
  (a branch, a group, nested repeats, the cap at ``MAXWIDTH``), and
  ``width``, what `getwidth` returns, as two decimal texts.
- ``parse.sweep`` (``pyre.jsonl``): ``count`` patterns, each joined from
  up to seven of ``fragments`` by the draws of `gen_pyre_tree.Lcg` from
  ``seed`` + i; ``sha256`` is the digest of their `_outcome` lines. The
  same row in ``pyre_python.jsonl`` holds the digest of their
  `gen_pyre_atom._line` lines, the tree text included.
- ``parse.width_sweep`` (``pyre.jsonl``): ``count`` random trees drawn by
  `gen_pyre_tree._sub` from ``seed`` + i; ``sha256`` is the digest of the
  lines ``lo,hi``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
from collections.abc import Iterator
from typing import Any

from .gen_pyre_atom import (
    PARSE,
    SPECIAL,
    _SOURCE,
    Refused,
    _failed,
    _hooked,
    _literals,
    _position,
    _spelled,
)
from .gen_pyre_tree import Lcg, _built, _sub, _tree, spell

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")

SEED = 20261121
SWEEPS = 20000
WIDTHS = 4000
B = "\\"
TOP = 4294967294

#: Written patterns: every branch, bound and error of the repeat reader.
REPEATS: list[str | bytes] = [
    "a*",
    "a+",
    "a?",
    "a{2}",
    "a{2,}",
    "a{,3}",
    "a{2,3}",
    "a{,}",
    "a{0}",
    "a{0,0}",
    "a{00002}",
    "a{3,}",
    "a{,0}",
    "a{3,3}",
    # A `{` that is a literal.
    "a{}",
    "{}",
    "x{}}",
    "a{",
    "a{2",
    "a{2,",
    "a{,3",
    "a{x}",
    "a{2x}",
    "a{-1}",
    "a{ 2}",
    "a{2 }",
    "a{2,3,4}",
    "a{" + B + "2}",
    "a{2" + B + "}",
    "a" + B + "{2}",
    "a{١}",
    "a*{",
    "a*{x",
    "a*{}",
    "{",
    "{x",
    # The bounds at MAXREPEAT and past it.
    "a{4294967294}",
    "a{4294967294,4294967294}",
    "a{0004294967294}",
    "a{4294967295}",
    "a{4294967296}",
    "a{99999999999999999999}",
    "a{1,4294967295}",
    "a{,4294967295}",
    "a{4294967295,}",
    "a{4294967295,1}",
    "a{99999999999,5}",
    "a{5,99999999999}",
    # int's digit limit, leading zeros included, ahead of every check of
    # the bound and of what the repeat goes over.
    "a{" + "0" * 4299 + "1}",
    "a{" + "0" * 4300 + "1}",
    "{" + "0" * 4301 + "}",
    "a{" + "9" * 4301 + "}",
    "a{1," + "0" * 4301 + "}",
    "a{" + "9" * 4301 + ",1}",
    "a{3,2}",
    "a{1,0}",
    "a{4294967294,1}",
    "a{3,2",
    # Lazy and possessive.
    "a*?",
    "a+?",
    "a??",
    "a{2}?",
    "a{2,}?",
    "a{,3}?",
    "a*+",
    "a++",
    "a?+",
    "a{2}+",
    "a{2,3}+",
    "a{3,2}+",
    "a{3,2}?",
    "a*?+",
    "a*?" + B,
    "a*+" + B,
    "a*++",
    # Nothing to repeat and multiple repeat.
    "*",
    "+",
    "?",
    "{2}",
    "{2,3}",
    "^*",
    "$*",
    "a^*",
    "a$+",
    B + "A*",
    B + "Z+",
    B + "b?",
    B + "B{2}",
    "*a",
    "a**",
    "a*{2}",
    "a{2}*",
    "a{2}{3}",
    "a??*",
    "a*??",
    "a*?*",
    "^{4294967295}",
    "*{3,2}",
    "a**" + B,
    # What a repeat goes over.
    "ab*",
    "a*b*",
    "[ab]*",
    "[^a]+",
    ".*",
    B + "d*",
    B + "w{2}",
    B + "x41*",
    "[a-z]{1,3}?",
    "\xe9*",
    "\U0001f600+",
    "a" + B + "*",
    B + "**",
    "a*b",
    "a*|b",
    "a*(",
    "a*)",
    "(a)*",
    "a*" + B,
    "a{2}" + B,
    "a{" + B,
    "a{2," + B,
    "a" + B + "1*",
    # Bytes patterns.
    b"a*",
    b"\\xff{2}",
    b"a{4294967295}",
    b"*",
]

#: Trees built by hand, as `gen_pyre_tree._item` describes them.
WIDTH_TREES: list[list[Any]] = [
    [],
    [["lit", 97], ["at", "AT_END"], ["failure"]],
    [["max", 0, None, [["lit", 97]]]],
    [["max", 0, None, []]],
    [["max", 0, None, [["at", "AT_END"]]]],
    [["max", 1, None, [["max", 0, 1, [["lit", 97]]]]]],
    [["max", 0, None, [["lit", 97]]], ["lit", 98]],
    [["lit", 98], ["max", 0, None, [["lit", 97]]]],
    [["lit", 98], ["min", 2, None, [["lit", 97]]], ["lit", 99]],
    [["branch", []]],
    [["branch", []], ["lit", 97]],
    [["branch", [[["lit", 97]], [["lit", 97], ["lit", 98]], []]]],
    [["branch", [[["max", 1, None, [["lit", 97]]]], [["lit", 98]]]], ["lit", 99]],
    [["group", 1, 0, 0, [["lit", 97], ["any"]]]],
    [["group", None, 2, 0, [["max", 2, 3, [["lit", 97]]]]]],
    [["assert", 1, [["lit", 97]]], ["assert_not", -1, [["lit", 97]]]],
    [["in", [["negate"], ["range", 97, 122]]], ["not", 97]],
    [["min", 2, 5, [["lit", 97], ["lit", 98]]]],
    [["max", 3, 1, [["lit", 97]]]],
    [["max", TOP, TOP, [["lit", 97]]]],
    [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]],
    [["max", TOP, TOP, [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]]]],
    [["max", TOP, None, [["max", TOP, None, [["max", TOP, None, [["any"]]]]]]]],
    [["max", 2, 2, [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]]]],
    [["max", 0, TOP, [["max", 0, None, [["lit", 97]]]]]],
    [["max", TOP, TOP, [["max", 0, 0, [["lit", 97]]]]]],
]

#: The fragments the sweep joins.
FRAGMENTS = [
    "a",
    "b",
    "[ab]",
    ".",
    "^",
    "$",
    B + "b",
    B + "d",
    B + "A",
    "*",
    "+",
    "?",
    "{",
    "}",
    ",",
    "0",
    "2",
    "{2}",
    "{,3}",
    "{2,}",
    "{1,2}",
    "{3,2}",
    "{0}",
    "{4294967295}",
    "{4294967294}",
    "*?",
    "+?",
    "??",
    "*+",
    "{2}?",
    "{2}+",
    "(",
    ")",
    "|",
    B,
    B + "{",
    B + "*",
]


#: How deep groups may nest before the port refuses (``MAX_DEPTH``).
MAX_DEPTH = 100

#: The ValueErrors of `fix_flags`, by the port's names.
FIX_FLAGS = {
    "cannot use LOCALE flag with a str pattern": "LocaleStr",
    "ASCII and UNICODE flags are incompatible": "AsciiUnicode",
    "cannot use UNICODE flag with a bytes pattern": "UnicodeBytes",
    "ASCII and LOCALE flags are incompatible": "AsciiLocale",
}

TOKENIZER = P.Tokenizer
STATE = P.State
PARSE_SUB = P._parse_sub


class _Tokens(P.Tokenizer):  # type: ignore[misc]
    """The tokenizer as the port reads it. `_parse` takes ``?`` after a
    ``(`` with ``match`` and the character after it with ``get``: after
    ``(?`` a ``(`` (a conditional) or a ``>`` (an atomic group) is refused
    at the ``(``. ``match`` is asked for ``+`` only after a repeat, where
    the port refuses the possessive repeat. A group name of a str pattern
    past ASCII is refused where ``re`` would place its error."""

    def __init__(self, string: Any) -> None:
        super().__init__(string)
        # Where `gen_pyre_atom._Whitespace` places a refusal of VERBOSE.
        _SOURCE[0] = self
        self.last: str | None = None
        self.opened: int | None = None

    def get(self) -> Any:
        this = super().get()
        opened, self.opened = self.opened, None
        if opened is not None and this in ("(", ">"):
            raise Refused("Conditional" if this == "(" else "AtomicGroup", opened)
        self.last = this
        return this

    def match(self, char: str) -> bool:
        taken: bool = super().match(char)
        if taken and char == "+":
            raise Refused("Possessive", self.tell() - 1)
        if taken and char == "?" and self.last == "(":
            self.opened = self.tell() - 2
        self.last = None
        return taken

    def checkgroupname(self, name: str, offset: int, *nested: int) -> None:
        # 3.11 passes ``nested`` too.
        if self.istext and not name.isascii():
            raise Refused("UnicodeGroupName", self.tell() - len(name) - offset)
        super().checkgroupname(name, offset, *nested)


class _State(P.State):  # type: ignore[misc]
    """``State``, refusing a reference to a group where ``re`` would
    return ``GROUPREF``: at the backslash of ``\\1``, at the ``(`` of
    ``(?P=name)``."""

    def checklookbehindgroup(self, gid: int, source: Any) -> None:
        super().checklookbehindgroup(gid, source)
        text = source.decoded_string
        end = source.tell()
        if text[end - 1 : end] == ")":
            raise Refused("Backreference", text.rindex("(?P=", 0, end))
        raise Refused("Backreference", text.rindex(B, 0, end))


def _parse_sub(source: Any, state: Any, verbose: Any, nested: int) -> Any:
    """`_parse_sub`, refused past ``MAX_DEPTH`` levels of groups: a group's
    body is read at ``nested`` twice its depth."""
    if nested > 2 * MAX_DEPTH:
        raise Refused("Nesting", source.tell())
    return PARSE_SUB(source, state, verbose, nested)


@contextlib.contextmanager
def _full() -> Iterator[None]:
    """`gen_pyre_atom._hooked` with groups and alternatives read."""
    with _hooked():
        P.SPECIAL_CHARS = SPECIAL
        P._parse = PARSE
        P.Tokenizer, P.State, P._parse_sub = _Tokens, _State, _parse_sub
        try:
            yield
        finally:
            P.Tokenizer, P.State, P._parse_sub = TOKENIZER, STATE, PARSE_SUB


def _parsed(pattern: str | bytes, flags: int) -> Any:
    with _full():
        return P.parse(pattern, flags)


def _failed_full(exc: Exception) -> str:
    if isinstance(exc, ValueError) and str(exc) in FIX_FLAGS:
        return "%" + FIX_FLAGS[str(exc)]
    return _failed(exc)


def _outcome(pattern: str | bytes, flags: int = 0) -> str:
    """The answer for ``pattern`` without the tree's text: its width."""
    try:
        tree = _parsed(pattern, flags)
    except Exception as exc:
        return _failed_full(exc)
    lo, hi = tree.getwidth()
    return f"={lo},{hi}"


def _counted(pattern: str | bytes, flags: int = 0) -> str:
    """`_outcome` with the groups and the flags the state is left with."""
    try:
        tree = _parsed(pattern, flags)
    except Exception as exc:
        return _failed_full(exc)
    lo, hi = tree.getwidth()
    return f"={lo},{hi} g{tree.state.groups} f{tree.state.flags}"


def _line(pattern: str | bytes, flags: int = 0) -> str:
    """The tree's ``str(tree)``, or the error or the refusal."""
    try:
        tree = _parsed(pattern, flags)
    except Exception as exc:
        return _failed_full(exc)
    return str(tree)


def _sweep_text(seed: int) -> str:
    draw = Lcg(seed).draw
    return "".join(FRAGMENTS[draw() % len(FRAGMENTS)] for _ in range(draw() % 8))


def _sweep(answer: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(SWEEPS):
        digest.update((answer(_sweep_text(SEED + i)) + "\n").encode("ascii"))
    return {
        "part": "parse.sweep",
        "seed": SEED,
        "count": SWEEPS,
        "fragments": [spell(fragment) for fragment in FRAGMENTS],
        "sha256": digest.hexdigest(),
    }


def _widths() -> list[dict[str, Any]]:
    rows = []
    for items in WIDTH_TREES:
        tree = _built(items, P.State())
        assert _tree(tree) == items, items
        lo, hi = tree.getwidth()
        rows.append({"part": "parse.width", "tree": items, "width": [str(lo), str(hi)]})
    return rows


def _width_sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(WIDTHS):
        lo, hi = _sub(Lcg(SEED + i).draw, P.State(), 0).getwidth()
        digest.update(f"{lo},{hi}\n".encode("ascii"))
    return {
        "part": "parse.width_sweep",
        "seed": SEED,
        "count": WIDTHS,
        "sha256": digest.hexdigest(),
    }


IC = C.SRE_FLAG_IGNORECASE
L = C.SRE_FLAG_LOCALE
M = C.SRE_FLAG_MULTILINE
S = C.SRE_FLAG_DOTALL
U = C.SRE_FLAG_UNICODE
X = C.SRE_FLAG_VERBOSE
A = C.SRE_FLAG_ASCII

#: Written patterns of pyre-3b, a pattern alone or with the flags it is
#: parsed under: each group form, the inline flags, the rewrites of
#: `_parse_sub`, `fix_flags`, every error at its position and every
#: refusal.
GROUPS: list[str | bytes | tuple[str | bytes, int]] = [
    # Groups and their unpacking.
    "(a)",
    "()",
    "(?:a)",
    "(?:)",
    "(a)(b)",
    "((a))",
    "(a(b)c)",
    "a(?:bc)d",
    "(?:a(?:b)c)d",
    "(?:a)*",
    "(?:ab)*",
    "(a)*",
    "(a|b)",
    "(?:a|b)",
    "(?:ab|cd)e",
    # Named groups and the group-name check.
    "(?P<n>a)",
    "(?P<n>a)(?P<m>b)",
    "(?P<n>a)(?P<n>b)",
    "(?P<a1_>x)",
    "(?P<_>x)",
    "(?P<Z9>x)",
    "(?P<a\n>x)",
    "(?P<1a>x)",
    "(?P<0a>x)",
    "(?P<\x80>a)",
    "(?P<\x7f>a)",
    "(?P<a-b>x)",
    "(?P<a b>x)",
    "(?P<a" + B + "b>x)",
    "(?P<\xe9>x)",
    "(?P<a\xe9>x)",
    "(?P<>x)",
    "(?P<a",
    "(?P<",
    "(?P",
    "(?Px)",
    "(?P" + B + "x)",
    "(?P>x)",
    b"(?P<n>a)",
    # References to groups.
    "(?P<n>a)(?P=n)",
    "(?P=n)",
    "(?P<n>(?P=n))",
    "(?P<n>a)(?P=1a)",
    "(?P<n>a)(?P=\xe9)",
    "(?P=)",
    "(?P=n",
    "(a)" + B + "1",
    B + "1",
    "(a" + B + "1)",
    "(a)(?<=" + B + "1)",
    "(?<=(a)" + B + "1)",
    "(?P<n>a)(?<=(?P=n))",
    "(?<=(?P<n>a)(?<=b)(?P=n))",
    "(?<=(?P<n>a)(?P=n))",
    "(a)(b)" + B + "2" + B + "3",
    # Comments.
    "(?#x)",
    "(?#)",
    "a(?#x)b",
    "(?#x",
    "(?#" + B + ")",
    "(?#" + B + ")x)",
    "(?#a)(?i)b",
    # Lookarounds.
    "(?=a)",
    "(?!a)",
    "(?<=a)",
    "(?<!a)",
    "(?!)",
    "(?<!)",
    "(?=)",
    "(?<=)",
    "(?<=a|b)",
    "(?<=(?<=a)b)",
    "(?<=(a))(b)",
    "(?<x)",
    "(?<",
    "(?<" + B + "=a)",
    "(?=a",
    "(?<=a",
    "a(?=b)*",
    "(?=a)+",
    # What is refused: conditionals, atomic groups, possessive repeats.
    "(?(1)a|b)",
    "(a)(?(1)a|b)",
    "(?(",
    "(?>a)",
    "(?>",
    "(?:a)*+",
    "(a)++",
    # Errors of the group's opening and closing.
    "(",
    "(?",
    "(a",
    "((a)",
    "(a))",
    "a)",
    ")",
    "(?:a",
    "(?Q)",
    "(?" + B + "x)",
    "(?" + B,
    "(?)",
    "(?=a)(",
    # Global flags.
    "(?i)a",
    "(?im)a",
    "(?s)(?m)a",
    "(?#c)(?i)a",
    "a(?i)",
    "(a)(?i)",
    "a|(?i)b",
    "(?:(?i))",
    "(?i)|b",
    "(?x)a",
    "(?x)a b",
    "(?x)a|b c",
    "(?x)a|(?-x:b c)",
    "(?a)a",
    "(?u)a",
    "(?L)a",
    b"(?L)a",
    b"(?u)a",
    b"(?a)a",
    "(?au)a",
    "(?ua)a",
    "(?aa)a",
    "(?ia)a",
    "(?i",
    "(?iz)",
    "(?i1)",
    "(?i\xe9)",
    "(?\xe9)",
    "(?i" + B + "m)",
    # Scoped flags.
    "(?i:a)",
    "(?i:a)*",
    "(?-i:a)",
    "(?i-m:a)",
    "(?s:.)*",
    "(?x:a b)",
    "(?-x:a b)",
    ("(?-x:a b)", X),
    ("(?x:(?-x:a b))", 0),
    "(?x:a| )",
    ("(a| )", X),
    "(?x)(a| b)",
    "(?ai:x)",
    "(?ai)x",
    b"(?Li:x)",
    "(?ui)x",
    "(?i-i:a)",
    "(?im-sm:a)",
    "(?-:a)",
    "(?-",
    "(?i-",
    "(?i-m",
    "(?i-z:a)",
    "(?i-1:a)",
    "(?-\xe9:a)",
    "(?-a:a)",
    "(?-u:a)",
    "(?-L:a)",
    "(?-im)",
    "(?i-m)",
    "(?a:a)",
    b"(?L:a)",
    b"(?-L:a)",
    # Alternatives: the common prefix, the folding into a set.
    "a|b",
    "a|b|c",
    "ab|ac",
    "ab|ab",
    "a|a",
    "abc|abd|abe",
    "ab|a",
    "a|",
    "|",
    "|a",
    "a||b",
    "x(?:ab|ac)",
    "a+b|a+c",
    "(?:a)b|(?:a)c",
    "(a)b|(a)c",
    "(?=a)b|(?=a)c",
    "a(?!)|a(?!)",
    "^a|^b",
    "a{2}|a{2}",
    "[ab]|c",
    "[^a]|b",
    "[a-c]|[d]|e",
    B + "d|a",
    B + "w|" + B + "W",
    ".|a",
    "a|.",
    "(a)|(b)",
    "a|b|[^c]",
    "[^ab]|c",
    "a|[^bc]",
    "a|bc|d",
    "a|a|b",
    "[ab]|[ba]",
    "(?:a|b)*",
    "(?i:a)|(?i:a)",
    "(?:)|(?:)",
    b"a|b",
    # fix_flags, after the parse and before "unbalanced parenthesis".
    ("a", A),
    ("a", L),
    ("a", A | U),
    ("a", U),
    ("a)", L),
    ("(?a)a", U),
    (b"a", U),
    (b"a", A),
    (b"a", L),
    (b"a", A | L),
    (b"(?L)a", A),
    (b"(?a)a", L),
    ("a", IC | M | S),
    ("a b", X),
    ("a" + B + " b", X),
    ("(?-x:a b)c d", X),
    # Nesting at the port's bound and past it.
    "(" * MAX_DEPTH + ")" * MAX_DEPTH,
    "(" * (MAX_DEPTH + 1) + ")" * (MAX_DEPTH + 1),
    "(?:" * (MAX_DEPTH + 1),
    "(?=" * MAX_DEPTH + ")" * MAX_DEPTH,
]


#: Patterns the release reads otherwise: a bytes group name past ASCII
#: is an error from 3.12 and a DeprecationWarning in 3.11.
GROUPS_PYTHON: list[str | bytes | tuple[str | bytes, int]] = [
    b"(?P<\xe9>x)",
]


def _case(case: str | bytes | tuple[str | bytes, int]) -> tuple[str | bytes, int]:
    if isinstance(case, tuple):
        return case
    return case, 0


def _row(part: str, case: Any, key: str, answer: Any) -> dict[str, Any]:
    pattern, flags = _case(case)
    row = {"part": part, **_spelled(pattern)}
    if flags:
        row["flags"] = flags
    row[key] = answer(pattern, flags)
    return row


#: The fragments the fuzz joins, and the flags it draws.
FUZZ_FRAGMENTS = [
    "a",
    "b",
    "ab",
    "[ab]",
    ".",
    "^",
    "(",
    ")",
    "|",
    "(?:",
    "(?P<a>",
    "(?P<b>",
    "(?P=a)",
    "(?=",
    "(?!",
    "(?<=",
    "(?<!",
    "(?i)",
    "(?x)",
    "(?a)",
    "(?i:",
    "(?-i:",
    "(?#c)",
    "(?(1)",
    "(?>",
    B + "1",
    "*",
    "+?",
    "{2}",
    " ",
    "\xe9",
]
FUZZ_FLAGS = [0, 0, 0, IC, M | S, X, A, L, U]
FUZZ = 20000


def _fuzz_case(seed: int) -> tuple[str | bytes, int]:
    draw = Lcg(seed).draw
    text = "".join(
        FUZZ_FRAGMENTS[draw() % len(FUZZ_FRAGMENTS)] for _ in range(draw() % 8)
    )
    flags = FUZZ_FLAGS[draw() % len(FUZZ_FLAGS)]
    if draw() % 3 == 0 and all(ord(ch) < 256 for ch in text):
        return text.encode("latin-1"), flags
    return text, flags


def _fuzz(answer: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(FUZZ):
        digest.update((answer(*_fuzz_case(SEED + i)) + "\n").encode("ascii"))
    return {
        "part": "parse.fuzz",
        "seed": SEED,
        "count": FUZZ,
        "fragments": [spell(fragment) for fragment in FUZZ_FRAGMENTS],
        "flags": FUZZ_FLAGS,
        "sha256": digest.hexdigest(),
    }


def _chain(pattern: str | bytes, answer: Any) -> list[str]:
    """`gen_pyre_atom._chain` read by the whole parser."""
    lines = []
    start = 0
    while True:
        line = answer(pattern[start:], 0)
        lines.append(line)
        at = _position(line)
        if at is None:
            return lines
        start += at + 1


def _inventory(answer: Any) -> dict[str, Any]:
    """The chains of the patterns of ``atom.inventory``, in its order: the
    first 16 hexadecimal digits of the SHA-256 of each chain's lines."""
    chains = []
    for pattern in _literals():
        lines = "".join(line + "\n" for line in _chain(pattern, answer))
        chains.append(hashlib.sha256(lines.encode("ascii")).hexdigest()[:16])
    return {"part": "parse.inventory", "chains": chains}


def rows() -> list[dict[str, Any]]:
    repeats = [
        {"part": "parse.repeat", **_spelled(pattern), "answer": _outcome(pattern)}
        for pattern in REPEATS
    ]
    groups = [_row("parse.group", case, "answer", _counted) for case in GROUPS]
    return [
        *repeats,
        *_widths(),
        _sweep(_outcome),
        _width_sweep(),
        *groups,
        _fuzz(_counted),
        _inventory(_counted),
    ]


def rows_python() -> list[dict[str, Any]]:
    """The tree texts, which follow the release."""
    trees = []
    for pattern in REPEATS:
        line = _line(pattern)
        if line.startswith("["):
            trees.append({"part": "parse.tree", **_spelled(pattern), "dump": line})
    for case in GROUPS:
        row = _row("parse.tree", case, "dump", _line)
        if row["dump"].startswith("["):
            trees.append(row)
    trees.extend(
        _row("parse.group", case, "answer", _counted) for case in GROUPS_PYTHON
    )
    return [*trees, _sweep(_line), _fuzz(_line), _inventory(_line)]
