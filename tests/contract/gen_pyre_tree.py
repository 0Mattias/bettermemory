"""Fixtures: the tree, the state and the tokenizer of `re._parser` (the
``tree.*`` and ``token.*`` parts of ``pyre.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/pyre/tree.rs`` and ``token.rs``. The
reference here is the interpreter's own ``re`` package, not a module of
bettermemory: every answer below is what ``re._parser`` gives.

A text that goes into a row is spelled by `spell`: its backslashes are
written as ``~`` (a golden file may hold no run of backslashes, which the
host-fact guard reads as a path), and a text that holds a ``~`` or a
surrogate is a list of code points instead.

Parts, by ``part``:

- ``tree.constants``: ``MAXREPEAT``, ``MAXGROUPS``, ``MAXWIDTH`` (a decimal
  text), the flag bits, the names of the ``AT`` and ``CATEGORY`` codes the
  parser writes.
- ``tree.names``: ``why`` maps each name the port gives an error of
  ``re`` to the message it stands for, in the order of the port's enum.
  Each message is checked here against the string constants of the
  interpreter's ``re/_parser.py``. ``feature`` lists the port's own names
  for what it refuses.
- ``tree.dump``: ``pattern`` parsed by ``re._parser.parse``; ``tree`` is
  the tree it returned, item by item (`_item`), and ``dump`` is
  ``str(tree)``. The native test builds ``tree`` and prints it. A row
  without a ``pattern`` is a tree built here from ``tree`` (`_built`), for
  shapes no pattern of every release parses to.
- ``tree.eq``: ``items`` are items as in ``tree.dump``; ``equal[i][j]`` is
  ``1`` when item ``i`` of one parse ``==`` item ``j`` of another parse of
  the same patterns, as `_parse_sub` compares items. A row without
  ``patterns`` compares two builds of ``items`` (`_built`): shapes no
  pattern parses to, a branch without alternatives among them.
- ``tree.sweep``: ``count`` random trees drawn by `_tree_case` from the
  linear congruence of `Lcg` seeded with ``seed``. Each case is the line
  ``str(tree)``, a space, and for every pair of items of two builds of
  the case ``1`` or ``0`` for ``==``; ``sha256`` is the digest of the
  lines, each ended by a newline.
- ``tree.state``: ``ops`` run on one ``State()`` and what each returned
  (``out``), then its ``flags``, its ``groupdict`` (by group number) and
  its ``groupwidths`` (decimal texts). An error is ``!`` and its name.
  Under ``maxgroups`` every ``open`` runs with the parser's ``MAXGROUPS``
  set to that number, which is how a script reaches "too many groups".
- ``token.script``: ``ops`` run on ``Tokenizer(s)`` and one line per op
  (``out``): the result, ``tell()`` and the next token (`_line`).
  ``new`` is the error when the constructor raised.
- ``token.sweep``: ``count`` random scripts over random texts drawn by
  `_token_case`; ``sha256`` is the digest of every line of every case.

Nothing here reads the Unicode database, so the parts of `rows` are not
gated. `rows` writes the same bytes under CPython 3.11.15, 3.12.13,
3.13.13 and 3.15.2 (run on each). CI also compares the file under 3.14,
which was not at hand to run; a row that a release answers otherwise
belongs in `rows_python`.

One answer is the release's: an empty negative lookaround, ``(?!)`` or
``(?<!)``, parses to ``(FAILURE, ())`` from 3.13 and to
``(ASSERT_NOT, (dir, []))`` before. The patterns that hold one are in
`rows_python` (``pyre_python.jsonl``, gated on the Python release); the
dump of a ``FAILURE`` item is pinned for every release by the built rows
and the sweep.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import re
from typing import Any

from ._common import has_surrogate

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")

SEED = 20261119
MULT = 6364136223846793005
INC = 1442695040888963407
MASK = (1 << 64) - 1
TREES = 4000
SCRIPTS = 20000

#: The port's name for each message of ``re.error`` it can raise so far,
#: in the order of ``pyre::tree::Why``.
WHY = {
    "BadEscapeEnd": "bad escape (end of pattern)",
    "BadEscape": "bad escape %s",
    "IncompleteEscape": "incomplete escape %s",
    "OctalRange": "octal escape value %s outside of range 0-0o377",
    "MissingBrace": "missing {",
    "MissingCharacterName": "missing character name",
    "MissingGroupName": "missing group name",
    "UnterminatedName": "missing %s, unterminated name",
    "InvalidGroupReference": "invalid group reference %d",
    "OpenGroup": "cannot refer to an open group",
    "LookbehindGroup": "cannot refer to group defined in the same lookbehind subpattern",
    "UnterminatedSet": "unterminated character set",
    "BadRange": "bad character range %s-%s",
    "UnbalancedParenthesis": "unbalanced parenthesis",
    "TooManyGroups": "too many groups",
    "Redefinition": "redefinition of group name %r as group %d; was group %d",
    "NothingToRepeat": "nothing to repeat",
    "MultipleRepeat": "multiple repeat",
    "MinGreaterThanMax": "min repeat greater than max repeat",
    "UnexpectedEnd": "unexpected end of pattern",
    "UnknownExtension": "unknown extension ?%s",
    "UnknownGroupName": "unknown group name %r",
    "UnterminatedComment": "missing ), unterminated comment",
    "UnterminatedSubpattern": "missing ), unterminated subpattern",
    "GlobalFlagsNotAtStart": "global flags not at the start of the expression",
    "BadGroupName": "bad character in group name %r",
    "InlineLocaleStr": "bad inline flags: cannot use 'L' flag with a str pattern",
    "InlineUnicodeBytes": "bad inline flags: cannot use 'u' flag with a bytes pattern",
    "InlineIncompatible": "bad inline flags: flags 'a', 'u' and 'L' are incompatible",
    "MissingFlagEnd": "missing -, : or )",
    "UnknownFlag": "unknown flag",
    "MissingFlag": "missing flag",
    "TurnOffTypeFlags": "bad inline flags: cannot turn off flags 'a', 'u' and 'L'",
    "MissingColon": "missing :",
    "FlagOnAndOff": "bad inline flags: flag turned on and off",
    "LocaleStr": "cannot use LOCALE flag with a str pattern",
    "AsciiUnicode": "ASCII and UNICODE flags are incompatible",
    "UnicodeBytes": "cannot use UNICODE flag with a bytes pattern",
    "AsciiLocale": "ASCII and LOCALE flags are incompatible",
}

#: ``pyre::tree::Feature``, in its order.
FEATURE = [
    "Backreference",
    "NamedCharacter",
    "SetWarning",
    "Verbose",
    "Ascii",
    "Locale",
    "Debug",
    "Conditional",
    "AtomicGroup",
    "Possessive",
    "NotBoundary",
    "NotDigit",
    "NotWord",
    "NullableLoop",
    "CaptureInRepeat",
    "CaptureInLookaround",
    "ChoiceInLookbehind",
    "ClassPastBmpIgnorecase",
    "BytesCategory",
    "BytesBoundary",
    "BytesIgnorecase",
    "Nesting",
    "Nodes",
    "Template",
    "Group",
    "Alternation",
    "UnicodeGroupName",
]

AT = [
    "AT_BEGINNING",
    "AT_BEGINNING_STRING",
    "AT_BOUNDARY",
    "AT_NON_BOUNDARY",
    "AT_END",
    "AT_END_STRING",
]
CATEGORY = [
    "CATEGORY_DIGIT",
    "CATEGORY_NOT_DIGIT",
    "CATEGORY_SPACE",
    "CATEGORY_NOT_SPACE",
    "CATEGORY_WORD",
    "CATEGORY_NOT_WORD",
]
FLAGS = [
    "IGNORECASE",
    "LOCALE",
    "MULTILINE",
    "DOTALL",
    "UNICODE",
    "VERBOSE",
    "DEBUG",
    "ASCII",
]

B = "\\"

#: Patterns whose trees hold every kind of item and every field's shapes.
DUMPED = [
    "",
    "a",
    B + "x00" + B + "U0010ffff" + B + "ud800",
    "[^a]",
    ".",
    "^$" + B + "A" + B + "Z" + B + "b" + B + "B",
    B + "d" + B + "D" + B + "s",
    "[" + B + "S" + B + "w" + B + "W]",
    "[a-z]",
    "[^a-z" + B + "d_]",
    "[]a-]",
    "ab|cd",
    "a|b|",
    "x(?:a|b(?:c|de))",
    "a|[b-c]|d",
    "(a)",
    "(?P<n>a)(b)",
    "(?:a)",
    "(?i:a)",
    "(?-i:a)",
    "(?ims-x:.)",
    "((((((((((a))))))))))",
    "a*",
    "a?",
    "a{2}",
    "a{2,}",
    "a{,3}",
    "a{2,3}",
    "a{0,4294967294}",
    "a*?",
    "a{1,2}?",
    "(?:ab)*",
    "(a|b)+c",
    "(?=a)",
    "(?!a)",
    "(?<=a)",
    "(?<!a)",
    "[0-9a-fA-F]{32,128}",
    B + "b[Tt]he new" + B + "b",
    "^(?P<k>[^=]+)=(?:(?P<v>.*))?$",
]

#: Patterns with an empty negative lookaround: 3.13 parses it to FAILURE.
DUMPED_PYTHON = ["(?!)", "(?<!)", "(?=(?<!)" + B + "w)", "a(?!)|a(?!)"]
COMPARED_PYTHON = ["(?!)", "(?<!)", "(?!a)", "a"]

#: Trees built here, not parsed: shapes that must print the same under
#: every release, and shapes no pattern parses to.
BUILT: list[list[Any]] = [
    [["failure"]],
    [["assert_not", 1, []], ["assert_not", -1, []]],
    [["assert", -1, [["failure"], ["any"]]]],
    [["branch", [[], []]]],
    [["group", None, 0, 0, []], ["group", 0, 64, 2, []]],
    [["max", 4294967294, None, []], ["min", 0, 0, []]],
    [["in", [["negate"]]], ["in", []], ["lit", 4294967295]],
]

#: First items compared with `==` across two parses. Each set has a near
#: miss: the same members negated, one more member, another end of the
#: range, another category.
COMPARED = [
    "a",
    "b",
    "[^a]",
    "[^b]",
    ".",
    "[ab]",
    "[ba]",
    "[^ab]",
    "[abc]",
    "[a-b]",
    "[a-c]",
    "(?:a|b)",
    B + "d",
    "[" + B + "d]",
    "[" + B + "s]",
    "^",
    B + "A",
    "a*",
    "a+",
    "a*?",
    "(a)",
    "(?=a)",
    "(?!a)",
    "(?i:a)",
    "ab|cd",
]

#: Items built here and compared with `==` across two builds. A branch
#: holds a list of sub-patterns, and a sub-pattern equals only itself: two
#: branches are equal when neither has an alternative, and never else.
COMPARED_BUILT: list[Any] = [
    ["branch", []],
    ["branch", [[]]],
    ["branch", [[], []]],
    ["in", []],
    ["in", [["negate"]]],
    ["in", [["lit", 97]]],
    ["in", [["range", 97, 97]]],
    ["lit", 97],
    ["not", 97],
    ["any"],
    ["failure"],
    ["at", "AT_END"],
    ["at", "AT_END_STRING"],
    ["group", None, 0, 0, []],
    ["max", 0, None, []],
    ["min", 0, None, []],
    ["assert", 1, []],
    ["assert_not", 1, []],
]

CODES = [0, 10, 97, 98, 0x5C, 0xE9, 0xD800, 0x1F600, 0x10FFFF]
BOUNDS = [0, 1, 2, 65535, 4294967294, 4294967295]
FLAG_SETS = [0, 2, 16, 26]

ALPHABET = [0x5C, 0x5C, 0x61, 0x37, 0x66, 0x3E, 0x7D, 0x7B, 0xE9, 0x1F600, 0xD800]
MATCHED = "a7>}{"
CHARSETS = {"hex": P.HEXDIGITS, "oct": P.OCTDIGITS, "dec": P.DIGITS}
MISSING = ["group name", "character name"]


def spell(text: str) -> Any:
    if has_surrogate(text) or "~" in text:
        return [ord(ch) for ch in text]
    return text.replace(B, "~")


class Lcg:
    """``s = (MULT * s + INC) mod 2**64``; a draw is ``s >> 33``."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK

    def draw(self) -> int:
        self.state = (MULT * self.state + INC) & MASK
        return self.state >> 33


def _name_of(message: str) -> str:
    """The port's name for a message of ``re.error``: the first whose
    text it is, a ``%`` field standing for anything."""
    for name, template in WHY.items():
        parts = re.split("(%[sdr])", template)
        shape = "".join(".*" if part[:1] == "%" else re.escape(part) for part in parts)
        if re.fullmatch(shape, message, re.DOTALL):
            return name
    raise AssertionError(f"a message without a name: {message!r}")


def _raised(exc: Exception) -> str:
    assert isinstance(exc, C.error), exc
    return _name_of(exc.msg)


def _constants() -> dict[str, Any]:
    assert [str(getattr(C, name)) for name in AT] == AT
    assert [str(getattr(C, name)) for name in CATEGORY] == CATEGORY
    return {
        "part": "tree.constants",
        "MAXREPEAT": int(C.MAXREPEAT),
        "MAXGROUPS": int(C.MAXGROUPS),
        "MAXWIDTH": str(P.MAXWIDTH),
        "flags": {name: int(getattr(C, f"SRE_FLAG_{name}")) for name in FLAGS},
        "at": AT,
        "category": CATEGORY,
    }


def _names() -> dict[str, Any]:
    """Each message must be text of the interpreter's parser: a string
    constant of its source, or ``"missing "`` joined to one."""
    source = ast.parse(inspect.getsource(P))
    texts = {
        node.value
        for node in ast.walk(source)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for name, message in WHY.items():
        joined = message.startswith("missing ") and message[8:] in texts
        # A constant the source joins to the character it read.
        suffixed = message.endswith("%s") and message[:-2] in texts
        assert message in texts or joined or suffixed, (name, message)
    return {"part": "tree.names", "why": WHY, "feature": FEATURE}


def _member(member: tuple[Any, Any]) -> list[Any]:
    op, av = member
    if op is C.NEGATE:
        return ["negate"]
    if op is C.LITERAL:
        return ["lit", av]
    if op is C.RANGE:
        return ["range", av[0], av[1]]
    assert op is C.CATEGORY, op
    return ["cat", str(av)]


def _item(item: tuple[Any, Any]) -> list[Any]:
    """One ``(op, av)`` of a tree, by its kind."""
    op, av = item
    if op is C.LITERAL:
        return ["lit", av]
    if op is C.NOT_LITERAL:
        return ["not", av]
    if op is C.ANY:
        return ["any"]
    if op is C.IN:
        return ["in", [_member(m) for m in av]]
    if op is C.AT:
        return ["at", str(av)]
    if op is C.BRANCH:
        assert av[0] is None
        return ["branch", [_tree(alt) for alt in av[1]]]
    if op is C.SUBPATTERN:
        return ["group", av[0], av[1], av[2], _tree(av[3])]
    if op is C.MAX_REPEAT or op is C.MIN_REPEAT:
        # Unbounded is the named constant; no written bound can equal it.
        unbounded = av[1] == C.MAXREPEAT
        assert unbounded == (str(av[1]) == "MAXREPEAT")
        kind = "max" if op is C.MAX_REPEAT else "min"
        return [kind, av[0], None if unbounded else av[1], _tree(av[2])]
    if op is C.ASSERT or op is C.ASSERT_NOT:
        kind = "assert" if op is C.ASSERT else "assert_not"
        return [kind, av[0], _tree(av[1])]
    assert op is C.FAILURE and av == (), item
    return ["failure"]


def _tree(tree: Any) -> list[Any]:
    return [_item(item) for item in tree.data]


def _built_member(member: list[Any]) -> tuple[Any, Any]:
    if member[0] == "negate":
        return (C.NEGATE, None)
    if member[0] == "lit":
        return (C.LITERAL, member[1])
    if member[0] == "range":
        return (C.RANGE, (member[1], member[2]))
    assert member[0] == "cat", member
    return (C.CATEGORY, getattr(C, member[1]))


def _built(items: list[Any], state: Any) -> Any:
    """The sub-pattern the descriptions of `_item` stand for."""
    data: list[tuple[Any, Any]] = []
    for item in items:
        kind = item[0]
        if kind == "lit":
            data.append((C.LITERAL, item[1]))
        elif kind == "not":
            data.append((C.NOT_LITERAL, item[1]))
        elif kind == "any":
            data.append((C.ANY, None))
        elif kind == "in":
            data.append((C.IN, [_built_member(m) for m in item[1]]))
        elif kind == "at":
            data.append((C.AT, getattr(C, item[1])))
        elif kind == "branch":
            alts = [_built(alt, state) for alt in item[1]]
            data.append((C.BRANCH, (None, alts)))
        elif kind == "group":
            body = _built(item[4], state)
            data.append((C.SUBPATTERN, (item[1], item[2], item[3], body)))
        elif kind in ("max", "min"):
            op = C.MAX_REPEAT if kind == "max" else C.MIN_REPEAT
            high = C.MAXREPEAT if item[2] is None else item[2]
            data.append((op, (item[1], high, _built(item[3], state))))
        elif kind in ("assert", "assert_not"):
            op = C.ASSERT if kind == "assert" else C.ASSERT_NOT
            data.append((op, (item[1], _built(item[2], state))))
        else:
            assert kind == "failure", item
            data.append((C.FAILURE, ()))
    return P.SubPattern(state, data)


def _dumps(patterns: list[str]) -> list[dict[str, Any]]:
    rows = []
    for pattern in patterns:
        tree = P.parse(pattern, 0)
        rows.append(
            {
                "part": "tree.dump",
                "pattern": spell(pattern),
                "tree": _tree(tree),
                "dump": str(tree),
            }
        )
    return rows


def _built_dumps() -> list[dict[str, Any]]:
    rows = []
    for items in BUILT:
        tree = _built(items, P.State())
        assert _tree(tree) == items, items
        rows.append({"part": "tree.dump", "tree": items, "dump": str(tree)})
    return rows


def _equal(patterns: list[str]) -> dict[str, Any]:
    ours = [P.parse(pattern, 0).data[0] for pattern in patterns]
    theirs = [P.parse(pattern, 0).data[0] for pattern in patterns]
    return {
        "part": "tree.eq",
        "patterns": [spell(pattern) for pattern in patterns],
        "items": [_item(item) for item in ours],
        "equal": ["".join("01"[a == b] for b in theirs) for a in ours],
    }


def _equal_built(items: list[Any]) -> dict[str, Any]:
    state = P.State()
    ours = _built(items, state).data
    theirs = _built(items, state).data
    assert [_item(item) for item in ours] == items
    return {
        "part": "tree.eq",
        "items": items,
        "equal": ["".join("01"[a == b] for b in theirs) for a in ours],
    }


def _sub(draw: Any, state: Any, depth: int) -> Any:
    """A random sub-pattern, as the parser would hold it."""
    data = [_random_item(draw, state, depth) for _ in range(draw() % 4)]
    return P.SubPattern(state, data)


def _random_item(draw: Any, state: Any, depth: int) -> tuple[Any, Any]:
    kind = draw() % (12 if depth < 3 else 6)
    code = lambda: CODES[draw() % len(CODES)]  # noqa: E731
    if kind == 0:
        return (C.LITERAL, code())
    if kind == 1:
        return (C.NOT_LITERAL, code())
    if kind == 2:
        return (C.ANY, None)
    if kind == 3:
        members: list[tuple[Any, Any]] = [(C.NEGATE, None)] if draw() % 2 else []
        for _ in range(1 + draw() % 3):
            which = draw() % 3
            if which == 0:
                members.append((C.LITERAL, code()))
            elif which == 1:
                members.append((C.RANGE, (code(), code())))
            else:
                members.append((C.CATEGORY, getattr(C, CATEGORY[draw() % 6])))
        return (C.IN, members)
    if kind == 4:
        return (C.AT, getattr(C, AT[draw() % 6]))
    if kind == 5:
        return (C.FAILURE, ())
    if kind == 6:
        # None to three alternatives: the parser makes two or more, and
        # two branches of none are the only equal ones.
        alts = [_sub(draw, state, depth + 1) for _ in range(draw() % 4)]
        return (C.BRANCH, (None, alts))
    if kind == 7:
        numbered = draw() % 3
        group = draw() % 12 if numbered else None
        add = FLAG_SETS[draw() % 4]
        delete = FLAG_SETS[draw() % 4]
        return (C.SUBPATTERN, (group, add, delete, _sub(draw, state, depth + 1)))
    if kind in (8, 9):
        low = BOUNDS[draw() % 5]
        high = BOUNDS[draw() % 6]
        bound = C.MAXREPEAT if high == C.MAXREPEAT else high
        op = C.MAX_REPEAT if kind == 8 else C.MIN_REPEAT
        return (op, (low, bound, _sub(draw, state, depth + 1)))
    direction = 1 if draw() % 2 else -1
    op = C.ASSERT if kind == 10 else C.ASSERT_NOT
    return (op, (direction, _sub(draw, state, depth + 1)))


def _tree_case(seed: int) -> str:
    """One line of the tree sweep: two builds of the same draws, the text
    of the first and every item of it compared with every item of the
    second."""
    state = P.State()
    ours = _sub(Lcg(seed).draw, state, 0)
    theirs = _sub(Lcg(seed).draw, state, 0)
    bits = "".join("01"[a == b] for a in ours.data for b in theirs.data)
    return f"{ours} {bits}"


def _tree_sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(TREES):
        digest.update((_tree_case(SEED + i) + "\n").encode("ascii"))
    return {
        "part": "tree.sweep",
        "seed": SEED,
        "count": TREES,
        "sha256": digest.hexdigest(),
    }


class _Width:
    """What `closegroup` asks of the sub-pattern it is given."""

    def __init__(self, width: tuple[int, int]) -> None:
        self.width = width

    def getwidth(self) -> tuple[int, int]:
        return self.width


def _state_op(state: Any, op: list[Any], maxgroups: int | None = None) -> Any:
    """One op of a state script and what it returns. `opengroup` reads
    the parser's global ``MAXGROUPS``; under `maxgroups` it is that number
    for the one call, and nothing else of `re` runs meanwhile."""
    try:
        if op[0] == "open":
            if maxgroups is None:
                return state.opengroup(op[1])
            P.MAXGROUPS = maxgroups
            try:
                return state.opengroup(op[1])
            finally:
                P.MAXGROUPS = C.MAXGROUPS
        if op[0] == "close":
            try:
                state.closegroup(op[1], _Width((int(op[2]), int(op[3]))))
            except IndexError:
                return "!IndexError"
            return None
        if op[0] == "check":
            return state.checkgroup(op[1])
        if op[0] == "behind":
            state.checklookbehindgroup(op[1], P.Tokenizer(""))
            return None
        if op[0] == "lookbehind":
            state.lookbehindgroups = op[1]
            return None
        assert op[0] == "groups", op
        return state.groups
    except C.error as exc:
        return "!" + _raised(exc)


STATE_SCRIPTS: list[list[list[Any]]] = [
    [["groups"], ["check", 0], ["check", 1], ["behind", 0], ["behind", 7]],
    [["open", None], ["groups"], ["check", 1], ["close", 1, "1", "1"], ["check", 1]],
    [
        ["open", "a"],
        ["open", None],
        ["open", "b"],
        ["close", 3, "0", "18446744073709551616"],
        ["check", 1],
        ["check", 2],
        ["check", 3],
        ["check", 4],
    ],
    # A name opened twice: the group is counted before the name is refused.
    [
        ["open", "a"],
        ["open", "a"],
        ["groups"],
        ["open", "A"],
        ["open", "a"],
        ["groups"],
    ],
    # Group 0 is never closed, so a lookbehind may not refer to it.
    [["lookbehind", 1], ["behind", 0], ["behind", 1], ["behind", 9]],
    [
        ["open", None],
        ["close", 1, "2", "5"],
        ["lookbehind", 2],
        ["open", None],
        ["behind", 1],
        ["behind", 2],
        ["close", 2, "0", "0"],
        ["behind", 2],
        ["behind", 3],
        ["lookbehind", None],
        ["behind", 2],
        ["behind", 3],
    ],
    # A group that was never opened cannot be closed: IndexError.
    [
        ["open", ""],
        ["open", chr(0xE9)],
        ["close", 2, "3", "4"],
        ["close", 3, "1", "1"],
        ["groups"],
    ],
    # A group closed twice keeps the second width.
    [
        ["open", None],
        ["close", 1, "1", "1"],
        ["close", 1, "2", "5"],
        ["check", 1],
        ["close", 0, "7", "9"],
        ["close", 0, "0", "0"],
        ["check", 0],
    ],
]

#: Scripts run with ``MAXGROUPS`` set to the first number. A group past
#: the bound is counted, then refused, before its name is looked at.
BOUNDED_SCRIPTS: list[tuple[int, list[list[Any]]]] = [
    (
        3,
        [
            ["open", None],
            ["open", "a"],
            ["groups"],
            ["open", "b"],
            ["groups"],
            ["open", "a"],
            ["groups"],
            ["check", 3],
            ["close", 3, "1", "1"],
            ["check", 3],
            ["close", 5, "1", "1"],
        ],
    ),
    (2, [["open", "a"], ["groups"], ["open", "a"], ["open", None], ["groups"]]),
    (1, [["open", "a"], ["groups"], ["open", None], ["groups"]]),
    (0, [["groups"], ["open", None], ["groups"]]),
]


def _states() -> list[dict[str, Any]]:
    rows = []
    scripts: list[tuple[int | None, list[list[Any]]]] = [
        *((None, ops) for ops in STATE_SCRIPTS),
        *BOUNDED_SCRIPTS,
    ]
    for maxgroups, ops in scripts:
        state = P.State()
        out = [_state_op(state, op, maxgroups) for op in ops]
        assert P.MAXGROUPS == C.MAXGROUPS
        bound = {} if maxgroups is None else {"maxgroups": maxgroups}
        rows.append(
            {
                "part": "tree.state",
                **bound,
                "ops": ops,
                "out": out,
                "flags": state.flags,
                "groupdict": sorted(
                    ([spell(name), gid] for name, gid in state.groupdict.items()),
                    key=lambda pair: pair[1],
                ),
                "groupwidths": [
                    None if width is None else [str(width[0]), str(width[1])]
                    for width in state.groupwidths
                ],
            }
        )
    return rows


def _shown(value: Any) -> str:
    """A result of a tokenizer op as ASCII: ``-`` for None, ``T`` or ``F``,
    a token as ``c`` or ``e`` (escaped) and its code point, characters as
    their code points joined by dots in brackets."""
    if value is None:
        return "-"
    if value is True or value is False:
        return "TF"[not value]
    if isinstance(value, int):
        return str(value)
    assert isinstance(value, str), value
    return "[" + ".".join(str(ord(ch)) for ch in value) + "]"


def _token(token: str | None) -> str:
    if token is None:
        return "-"
    return ("e" if len(token) == 2 else "c") + str(ord(token[-1]))


def _token_op(source: Any, op: list[Any]) -> str:
    try:
        if op[0] == "get":
            return _token(source.get())
        if op[0] == "match":
            return _shown(source.match(op[1]))
        if op[0] == "while":
            return _shown(source.getwhile(op[1], CHARSETS[op[2]]))
        if op[0] == "until":
            return _shown(source.getuntil(op[1], op[2]))
        if op[0] == "seek":
            source.seek(op[1])
            return "-"
        if op[0] == "error":
            return _shown(source.error("x", op[1]).pos)
        assert op[0] == "tell", op
        return "-"
    except C.error as exc:
        return f"!{_raised(exc)}@{exc.pos}"


def _line(source: Any, op: list[Any]) -> str:
    """One op's line: its result, then ``tell()`` and the next token."""
    result = _token_op(source, op)
    return f"{result} {source.tell()} {_token(source.next)}"


def _script(text: str | bytes, ops: list[list[Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"part": "token.script"}
    if isinstance(text, bytes):
        row["bytes"] = True
        row["s"] = spell(text.decode("latin-1"))
    else:
        row["s"] = spell(text)
    try:
        source = P.Tokenizer(text)
    except C.error as exc:
        assert not ops
        row["new"] = f"!{_raised(exc)}@{exc.pos}"
        return row
    row["start"] = f"- {source.tell()} {_token(source.next)}"
    row["ops"] = ops
    row["out"] = [_line(source, op) for op in ops]
    return row


GET = ["get"]
TOKEN_SCRIPTS: list[tuple[str | bytes, list[list[Any]]]] = [
    ("", [GET, GET, ["match", "a"], ["while", 2, "hex"], ["seek", 3], ["tell"]]),
    ("ab", [GET, GET, GET]),
    ("a" + B + "b" + B + B + "c", [GET, GET, GET, GET, GET]),
    # A backslash that ends the pattern: raised by the constructor, or by
    # the step that would load it, which then leaves the tokenizer alone.
    (B, []),
    (B + B + B, [GET, ["tell"]]),
    ("a" + B, [GET, GET, ["match", "a"], ["tell"]]),
    ("ab" + B, [GET, ["match", "b"], ["while", 3, "hex"], GET, ["error", 1]]),
    (b"\xff" + B.encode() + b"\xfe\x00", [GET, GET, GET, GET]),
    ("\U0001f600" + B + "\ud800" + "\x00", [GET, GET, GET]),
    # `match` compares whole tokens: an escaped character is not the character.
    (
        "a" + B + "aa",
        [["match", "b"], ["match", "a"], ["match", "a"], GET, ["match", "a"]],
    ),
    # `getwhile`: at most n, only of the charset, never an escaped digit.
    (
        "1aF" + B + "7g",
        [["while", 0, "hex"], ["while", 2, "hex"], ["while", 9, "hex"], GET],
    ),
    (
        "789",
        [
            ["while", 3, "oct"],
            ["while", 1, "dec"],
            ["while", 3, "oct"],
            ["while", 3, "dec"],
        ],
    ),
    (chr(0xFF11) + "1", [["while", 2, "dec"], GET, ["while", 2, "dec"]]),
    # `getuntil`: the name, the three errors and their positions.
    ("ab>c", [["until", ">", "group name"], GET, GET]),
    ("a" + B + ">b>", [["until", ">", "group name"], GET]),
    (">x", [["until", ">", "group name"], GET]),
    ("x>}", [GET, GET, ["until", "}", "character name"]]),
    ("", [["until", "}", "character name"], ["until", ">", "group name"]]),
    ("abc", [["until", ")", "group name"], ["tell"]]),
    ("ab" + B, [["until", ">", "group name"], ["tell"], GET]),
    # `seek` to any index, a token boundary or not, and past the end.
    (
        "a" + B + "bc",
        [["seek", 3], GET, ["seek", 2], GET, ["seek", 0], ["seek", 9], GET],
    ),
    # A failed `seek` has moved the index: `tell` is then negative.
    (B + B, [["seek", 1], ["tell"], ["error", 0], ["error", 2], GET, ["tell"]]),
    ("ab" + B, [["seek", 2], GET, GET, ["seek", 5], ["error", 7]]),
    (
        "abc",
        [GET, ["error", 0], ["error", 1], ["error", 2], GET, GET, GET, ["error", 1]],
    ),
    # Offsets and an index at the ends of a 64-bit integer: the position
    # is the exact difference. Every number here fits 64 signed bits.
    ("abc", [["error", 2**63], GET, ["error", 2**63], GET, ["error", 2**63 + 2]]),
    (
        "abc",
        [
            ["seek", 2**63 - 1],
            ["error", 0],
            ["error", 2**63 - 1],
            ["error", 2**63],
            ["error", 2**64 - 1],
        ],
    ),
]


def _token_case(seed: int) -> list[str]:
    """The lines of one case of the tokenizer sweep."""
    draw = Lcg(seed).draw
    text = "".join(chr(ALPHABET[draw() % len(ALPHABET)]) for _ in range(draw() % 9))
    try:
        source = P.Tokenizer(text)
    except C.error as exc:
        return [f"!{_raised(exc)}@{exc.pos}"]
    lines = [f"- {source.tell()} {_token(source.next)}"]
    for _ in range(1 + draw() % 8):
        kind = draw() % 8
        if kind < 2:
            op: list[Any] = ["get"]
        elif kind == 2:
            op = ["match", MATCHED[draw() % len(MATCHED)]]
        elif kind == 3:
            count = draw() % 4
            op = ["while", count, ["hex", "oct", "dec"][draw() % 3]]
        elif kind == 4:
            terminator = ">}"[draw() % 2]
            op = ["until", terminator, MISSING[draw() % 2]]
        elif kind == 5:
            op = ["seek", draw() % (len(text) + 2)]
        elif kind == 6:
            op = ["error", draw() % 4]
        else:
            op = ["tell"]
        lines.append(_line(source, op))
    return lines


def _token_sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(SCRIPTS):
        for line in _token_case(SEED + i):
            digest.update((line + "\n").encode("ascii"))
    return {
        "part": "token.sweep",
        "seed": SEED,
        "count": SCRIPTS,
        "alphabet": ALPHABET,
        "sha256": digest.hexdigest(),
    }


def rows_python() -> list[dict[str, Any]]:
    """The rows whose answers are CPython 3.13's."""
    return [*_dumps(DUMPED_PYTHON), _equal(COMPARED_PYTHON)]


def rows() -> list[dict[str, Any]]:
    return [
        _constants(),
        _names(),
        *_dumps(DUMPED),
        *_built_dumps(),
        _equal(COMPARED),
        _equal_built(COMPARED_BUILT),
        _tree_sweep(),
        *_states(),
        *(_script(text, ops) for text, ops in TOKEN_SCRIPTS),
        _token_sweep(),
    ]
