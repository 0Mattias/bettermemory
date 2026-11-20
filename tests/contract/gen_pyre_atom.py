"""Fixtures: the atoms `re._parser` reads, and `re.escape` (the ``atom.*``
parts of ``pyre.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/pyre/atom.rs``: `_class_escape`,
`_escape`, `_uniq`, the sets and flat sequences `_parse` reads, and
`re.escape`. The reference here is the interpreter's own ``re`` package.

The port reads less than `re._parser.parse` does: a repeat, a group and
an alternative wait for the parser's next unit, VERBOSE is refused at the
first token it changes, and where `re` warns or looks a name up in its
table the port refuses. `_hooked` states that edge without a second
parser: it runs `re._parser.parse` itself with a refusal raised at each
place the port refuses. ``SPECIAL_CHARS`` refuses ``(`` and the repeat
characters when `_parse` asks it, `_parse` refuses when `_parse_sub` calls
it for a second alternative, ``WHITESPACE`` refuses whitespace and ``#``
when it is asked (only under VERBOSE), `unicodedata.lookup` finds no name,
and FutureWarning is an error. The first event in the reference's own
order is the expected answer, as one line (`_line`): the tree's
``str(tree)``, ``!Name@pos`` for an error of ``re`` by the port's name of
its message, or ``?Feature@pos`` for a refusal at the position the port
gives it.

Parts, by ``part``:

- ``atom.parse``: ``pattern`` (a bytes pattern under ``bytes``, under
  VERBOSE under ``verbose``) and its ``answer``.
- ``atom.escape``: `_escape` (``in`` is ``item``) or `_class_escape`
  (``class``) called on ``Tokenizer(s)`` after its first token, an escape,
  with a ``State`` that the ``state`` ops built (as in ``tree.state``).
  ``out`` is the item or the member as ``str`` writes the tuple (a
  category member without the ``IN`` list `_class_escape` wraps it in), or
  the error or the refusal; then ``tell()`` and the next token. A group
  reference, ``GROUPREF``, is refused at the backslash.
- ``atom.sweep``: ``count`` patterns, each joined from up to seven of
  ``fragments`` by the draws of `gen_pyre_tree.Lcg` from ``seed`` and read
  as a str pattern, a bytes pattern (when every character is below 256)
  or under VERBOSE (`_sweep_case`); ``sha256`` is the digest of the
  answers' lines.
- ``atom.inventory``: each string literal (``str``) and bytes literal
  (``bytes``) the reference passes to a function of `re` (an `ast` scan of
  ``src/bettermemory``), read as a chain (`_chain`): after an error or a
  refusal at ``pos`` the rest of the pattern from ``pos + 1`` is read
  again, so every set and escape of the pattern is read though it holds a
  group or a repeat. ``chains`` holds, for each pattern in that order, the
  first 16 hexadecimal digits of the SHA-256 of its chain's lines.
- ``atom.re_escape``: `re.escape` of the text of every code point (its
  code points as UTF-32-LE, by digest), of the 256 bytes in full, and the
  code points it escapes. The generator checks that the escaped text of
  every code point parses to the literals of those code points, one by
  one.

Nothing here reads the Unicode database: `unicodedata.lookup` is replaced
for the parse. The parts are not gated; `rows` writes the same bytes under
CPython 3.11.15, 3.12.13 and 3.13.13. Two answers of the escape readers
are the release's (`PARSED_PYTHON`, in `rows_python`): ``\\z`` and a
``\\U`` escape of 2**31 or more. No pattern of `rows` holds either.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .gen_pyre_tree import SEED, Lcg, _raised, _state_op, _token, spell

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")
U: Any = importlib.import_module("unicodedata")

SWEEPS = 20000
SOURCE = Path(__file__).resolve().parents[2] / "src" / "bettermemory"
FUNCTIONS = frozenset(
    {
        "compile",
        "search",
        "match",
        "fullmatch",
        "findall",
        "finditer",
        "sub",
        "subn",
        "split",
    }
)

B = "\\"

#: What `_parse` and the parser's tables are when nothing is hooked.
PARSE = P._parse
SPECIAL = P.SPECIAL_CHARS
REPEAT = P.REPEAT_CHARS
WHITESPACE = P.WHITESPACE


class Refused(Exception):
    """A place where the port refuses: the feature and its position."""

    def __init__(self, what: str, at: int) -> None:
        super().__init__(what, at)
        self.what = what
        self.at = at


#: The tokenizer `_parse` reads, for the hooks that place a refusal.
_SOURCE: list[Any] = [None]


class _Special:
    """``SPECIAL_CHARS`` as the port reads it: `_parse` asks it of every
    bare character, and a group or a repeat is refused there."""

    def __contains__(self, char: object) -> bool:
        if char == "(":
            raise Refused("Group", _SOURCE[0].tell() - 1)
        if isinstance(char, str) and char in REPEAT:
            raise Refused("Repeat", _SOURCE[0].tell() - 1)
        return isinstance(char, str) and char in SPECIAL


class _Whitespace:
    """``WHITESPACE``, asked only under VERBOSE: refused at the first token
    the flag changes, whitespace or ``#``."""

    def __contains__(self, token: object) -> bool:
        if token in WHITESPACE or token == "#":
            assert isinstance(token, str)
            raise Refused("Verbose", _SOURCE[0].tell() - len(token))
        return False


def _parse(
    source: Any, state: Any, verbose: Any, nested: int, first: bool = False
) -> Any:
    """`_parse`, refused when `_parse_sub` calls it for an alternative
    after the first: the ``|`` is behind the tokenizer then."""
    _SOURCE[0] = source
    if not first:
        raise Refused("Alternation", source.tell() - 1)
    return PARSE(source, state, verbose, nested, first)


def _no_name(name: str) -> str:
    raise KeyError(name)


@contextlib.contextmanager
def _hooked() -> Iterator[None]:
    saved = (P.SPECIAL_CHARS, P.WHITESPACE, P._parse, U.lookup)
    P.SPECIAL_CHARS = _Special()
    P.WHITESPACE = _Whitespace()
    P._parse = _parse
    U.lookup = _no_name
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            yield
    finally:
        P.SPECIAL_CHARS, P.WHITESPACE, P._parse, U.lookup = saved


def _failed(exc: Exception) -> str:
    """The line of an exception the hooked reference raised."""
    if isinstance(exc, Refused):
        return f"?{exc.what}@{exc.at}"
    if isinstance(exc, FutureWarning):
        found = re.search(r"at position (\d+)$", str(exc))
        assert found, exc
        return f"?SetWarning@{found.group(1)}"
    assert isinstance(exc, C.error), exc
    if exc.msg.startswith("undefined character name"):
        # The lookup that finds no name, where the port refuses.
        return f"?NamedCharacter@{exc.pos}"
    return f"!{_raised(exc)}@{exc.pos}"


def _line(pattern: str | bytes, verbose: bool = False) -> str:
    """The answer for ``pattern``: ``str(tree)``, or the error or the
    refusal."""
    flags = C.SRE_FLAG_VERBOSE if verbose else 0
    try:
        with _hooked():
            tree = P.parse(pattern, flags)
    except (Refused, FutureWarning, C.error) as exc:
        return _failed(exc)
    return str(tree)


def _position(line: str) -> int | None:
    """Where an error or a refusal is, None for a tree."""
    if line.startswith(("!", "?")):
        return int(line.rsplit("@", 1)[1])
    return None


def _spelled(pattern: str | bytes) -> dict[str, Any]:
    if isinstance(pattern, bytes):
        return {"pattern": spell(pattern.decode("latin-1")), "bytes": True}
    return {"pattern": spell(pattern)}


#: Patterns, each read once: every branch of the two escape readers and of
#: a set, the errors and the refusals at their positions. A bytes pattern
#: is ``bytes``.
PARSED: list[str | bytes] = [
    "",
    "abc",
    "]}",
    ".^$",
    B + "a" + B + "f" + B + "v" + B + "n" + B + "r" + B + "t" + B + B,
    B + "b" + B + "B" + B + "A" + B + "Z",
    B + "d" + B + "D" + B + "s" + B + "S" + B + "w" + B + "W",
    # Octal escapes and group references.
    B + "0",
    B + "07",
    B + "077",
    B + "0777",
    B + "08",
    B + "100",
    B + "377",
    B + "400",
    B + "1",
    B + "12",
    B + "128",
    B + "8",
    B + "81",
    B + "18",
    B + "99",
    "a" + B + "1",
    "ab" + B + "17",
    # Hexadecimal and Unicode escapes.
    B + "x41",
    B + "xfF",
    B + "x4",
    B + "x",
    B + "xg",
    B + "x4g",
    B + "u00e9",
    B + "ud800",
    B + "u00e",
    B + "U0010ffff",
    B + "U00110000",
    B + "U7fffffff",
    B + "U0000004",
    B + "U0001f60",
    # Named characters: read as far as the lookup, then refused.
    B + "N{EM DASH}",
    "a" + B + "N{x}b",
    B + "N",
    B + "Nx",
    B + "N{",
    B + "N{}",
    B + "N{abc",
    B + "N{a" + B + "}b}",
    # Other escapes.
    B + "q",
    B + "\xe9",
    B + "-",
    B + "]",
    B + " ",
    B + "\U0001f600",
    B + "\ud800",
    B,
    "a" + B,
    # Digits past ASCII: `DIGITS`, `OCTDIGITS` and `HEXDIGITS` hold none,
    # so each is a literal after a backslash, ends a group reference or
    # an octal escape, and leaves a `\x` escape short.
    B + "\u0663",
    B + "\u0669",
    B + "\xb2",
    B + "0\u0661",
    B + "1\u0663",
    B + "10\u0661",
    B + "x\u0664\u0661",
    "[" + B + "\u0663]",
    "[" + B + "\u0669]",
    "[" + B + "1\u0661]",
    "[" + B + "x\u0664\u0661]",
    # Sets.
    "[a]",
    "[^a]",
    "[ab]",
    "[ba]",
    "[^ab]",
    "[aba]",
    "[aa]",
    "[a-ca-c]",
    "[]]",
    "[]a]",
    "[^]]",
    "[]",
    "[",
    "[^",
    "a[b",
    "[a",
    "[a-",
    "[a-]",
    "[-a]",
    "[-]",
    "[--]",
    "[---]",
    "[a-z]",
    "[z-a]",
    "[a-a]",
    "[" + B + "b]",
    "[" + B + "d]",
    "[^" + B + "d]",
    "[" + B + "d-]",
    "[" + B + "d-a]",
    "[a-" + B + "d]",
    "[" + B + "w" + B + "s" + B + "w]",
    "[" + B + "x41-" + B + "x5a]",
    "[" + B + "xff-" + B + "x00]",
    "[" + B + "u00e9-" + B + "U0001f600]",
    "[" + B + "1]",
    "[" + B + "378]",
    "[" + B + "400]",
    "[" + B + "8]",
    "[" + B + "A]",
    "[" + B + "B]",
    "[" + B + "Z]",
    "[" + B + "N{x}]",
    "[" + B + "N]",
    "[" + B + "-]",
    "[a" + B + "-z]",
    "[a-" + B + "-]",
    "[" + B + "]]",
    "[" + B + "x4]",
    "[" + B,
    "[ ]",
    "[\ud800-\udfff]",
    "[\U0001f600]",
    # The three places `re` warns, and their near misses.
    "[[]",
    "[^[]",
    "[a[]",
    "[a--b]",
    "[a-z--x]",
    "[a&&b]",
    "[a~~b]",
    "[a||b]",
    "[&&]",
    "[a&&]",
    "[a&" + B + "&]",
    "[" + B + "--]",
    # What the parser's next unit reads, refused where it begins.
    "a*",
    "a+",
    "a?",
    "a{2}",
    "a{",
    "{",
    "(a)",
    "a(",
    "a|b",
    "|",
    "a|" + B,
    "a)",
    ")",
    "a)|b",
    "[a](",
    # Bytes patterns.
    b"abc",
    b"\xff\x00",
    b"\\x41\\xff",
    b"\\u00e9",
    b"\\U00000041",
    b"\\N{x}",
    b"\\d[\\d\\xe9-\\xff]",
    b"[\xe9-\xff]",
    b"\\",
    b"\\\xb2",
    b"[\\\xb2]",
]

#: Patterns read under VERBOSE: refused at each character of
#: ``WHITESPACE`` and at ``#``, not at an escaped space, inside a set, or
#: at a space ``WHITESPACE`` does not hold.
PARSED_VERBOSE = [
    "abc",
    "a b",
    "a#b",
    "a\tb",
    "a\nb",
    "a\x0bb",
    "a\x0cb",
    "a\rb",
    "a\x1c\x1f\x85\xa0\u2028\u3000b",
    "a" + B + " b",
    "a" + B + "#",
    "[ #]",
    "[\n\x0b]",
    "a" + B + "tb",
]

#: The fragments the sweep joins.
FRAGMENTS = [
    "a",
    "7",
    "1",
    "0",
    "8",
    "]",
    "[",
    "[^",
    "^",
    "-",
    "--",
    "&&",
    "~~",
    "||",
    "a-z",
    # A reversed range that does not start with ``z``: after a backslash,
    # ``\z`` is a bad escape in 3.13 and the end of the string from 3.14.
    "y-a",
    "}",
    "{",
    "(",
    ")",
    "|",
    "*",
    ".",
    "$",
    " ",
    "#",
    "\t",
    "\xe9",
    "\U0001f600",
    "\ud800",
    B,
    B + B,
    B + "b",
    B + "B",
    B + "d",
    B + "W",
    B + "s",
    B + "A",
    B + "Z",
    B + "a",
    B + "q",
    B + "x4",
    B + "x41",
    B + "u00e9",
    B + "U0001F600",
    B + "U00110000",
    B + "N",
    B + "N{",
    B + "0",
    B + "1",
    B + "12",
    B + "377",
    B + "400",
    B + "8",
    B + " ",
    B + "-",
    B + "]",
    B + "\xe9",
]


def _parsed() -> list[dict[str, Any]]:
    rows = [
        {"part": "atom.parse", **_spelled(pattern), "answer": _line(pattern)}
        for pattern in PARSED
    ]
    for pattern in PARSED_VERBOSE:
        answer = _line(pattern, verbose=True)
        rows.append(
            {
                "part": "atom.parse",
                **_spelled(pattern),
                "verbose": True,
                "answer": answer,
            }
        )
    return rows


def _member(got: tuple[Any, Any]) -> str:
    """A member `_class_escape` returned, as ``str`` writes it: a category
    comes in the ``IN`` list a set unpacks."""
    op, av = got
    if op is C.IN:
        (member,) = av
        return str(member)
    assert op is C.LITERAL, got
    return str(got)


#: Direct calls of the escape readers: (``item`` or ``class``, the text,
#: the ops that build the state). Group 1 is closed and group 2 open
#: under ``OPENED``; ``BEHIND`` is that inside a lookbehind that began
#: after group 1.
OPENED: list[list[Any]] = [["open", None], ["close", 1, "1", "1"], ["open", None]]
BEHIND: list[list[Any]] = [*OPENED, ["lookbehind", 2]]
EARLY: list[list[Any]] = [*OPENED, ["lookbehind", 1]]
ESCAPED: list[tuple[str, str, list[list[Any]]]] = [
    ("item", B + "1", []),
    ("item", B + "1", OPENED),
    ("item", B + "2", OPENED),
    ("item", B + "3", OPENED),
    ("item", B + "1", BEHIND),
    ("item", B + "1", EARLY),
    ("item", B + "2", EARLY),
    ("item", B + "12", OPENED),
    ("item", B + "10x", OPENED),
    ("item", B + "100", OPENED),
    ("item", B + "0", OPENED),
    ("item", B + "1\u0663", OPENED),
    ("item", B + "\u0663", OPENED),
    ("item", B + "b", []),
    ("item", B + "d", []),
    ("item", B + "x41z", []),
    ("item", B + "N{x}y", []),
    ("item", B + "N{x", []),
    ("class", B + "b", []),
    ("class", B + "d", []),
    ("class", B + "W", []),
    ("class", B + "1", OPENED),
    ("class", B + "1234", []),
    ("class", B + "1\u0661", []),
    ("class", B + "\u0663", []),
    ("class", B + "8", []),
    ("class", B + "B", []),
    ("class", B + "x41z", []),
    ("class", B + "N{x}y", []),
    ("class", B + "U0010ffff", []),
]


def _escaped() -> list[dict[str, Any]]:
    rows = []
    for kind, text, ops in ESCAPED:
        state = P.State()
        for op in ops:
            _state_op(state, op)
        source = P.Tokenizer(text)
        escape = source.get()
        assert len(escape) == 2, text
        try:
            with _hooked():
                if kind == "item":
                    got = P._escape(source, escape, state)
                else:
                    got = P._class_escape(source, escape)
        except (Refused, FutureWarning, C.error) as exc:
            out = _failed(exc)
        else:
            if got[0] is C.GROUPREF:
                out = "?Backreference@0"
            elif kind == "item":
                out = str(got)
            else:
                out = _member(got)
        rows.append(
            {
                "part": "atom.escape",
                "in": kind,
                "s": spell(text),
                "state": ops,
                "out": f"{out} {source.tell()} {_token(source.next)}",
            }
        )
    return rows


def _sweep_case(seed: int) -> str:
    """One answer of the sweep: the pattern the draws join, read as the
    next draw says."""
    draw = Lcg(seed).draw
    text = "".join(FRAGMENTS[draw() % len(FRAGMENTS)] for _ in range(draw() % 8))
    mode = draw() % 4
    if mode == 2 and all(ord(ch) < 256 for ch in text):
        return _line(text.encode("latin-1"))
    return _line(text, mode == 3)


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(SWEEPS):
        digest.update((_sweep_case(SEED + i) + "\n").encode("ascii"))
    return {
        "part": "atom.sweep",
        "seed": SEED,
        "count": SWEEPS,
        "fragments": [spell(fragment) for fragment in FRAGMENTS],
        "sha256": digest.hexdigest(),
    }


def _literals() -> list[str | bytes]:
    """The first arguments of the reference's calls of `re` that are one
    string or bytes literal, each once."""
    found: set[str | bytes] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "re"
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func, first = node.func, node.args[0]
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in names
                and func.attr in FUNCTIONS
                and isinstance(first, ast.Constant)
                and isinstance(first.value, (str, bytes))
            ):
                found.add(first.value)
    texts = sorted(text for text in found if isinstance(text, str))
    raw = sorted(text for text in found if isinstance(text, bytes))
    return [*texts, *raw]


def _chain(pattern: str | bytes) -> list[str]:
    """The answers for ``pattern``, read again from one past each error or
    refusal until a read gives a tree."""
    lines = []
    start = 0
    while True:
        line = _line(pattern[start:])
        lines.append(line)
        at = _position(line)
        if at is None:
            return lines
        start += at + 1


def _inventory() -> dict[str, Any]:
    literals = _literals()
    assert len(literals) >= 100, len(literals)
    chains = []
    for pattern in literals:
        text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
        # `\z` is the release's (see the module's text).
        assert B + "z" not in text.replace(B + B, ""), text
        lines = "".join(line + "\n" for line in _chain(pattern))
        chains.append(_sha256(lines.encode("ascii"))[:16])
    return {
        "part": "atom.inventory",
        "str": [spell(text) for text in literals if isinstance(text, str)],
        "bytes": [
            spell(raw.decode("latin-1")) for raw in literals if isinstance(raw, bytes)
        ],
        "chains": chains,
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _re_escape() -> dict[str, Any]:
    every = "".join(map(chr, range(0x110000)))
    escaped = re.escape(every)
    special = [c for c in range(256) if re.escape(chr(c)) != chr(c)]
    assert len(escaped) == len(every) + len(special)
    # What `re.escape` writes reads back as the code points it was given.
    assert P.parse(escaped).data == [(C.LITERAL, c) for c in range(0x110000)]
    raw = re.escape(bytes(range(256)))
    assert P.parse(raw).data == [(C.LITERAL, c) for c in range(256)]
    return {
        "part": "atom.re_escape",
        "special": special,
        "sha256": _sha256(escaped.encode("utf-32-le", "surrogatepass")),
        "bytes": spell(raw.decode("latin-1")),
    }


#: Patterns the release reads otherwise: ``\z`` is a bad escape in 3.13
#: and the end of the string from 3.14; ``\U`` with a value of 2**31 or
#: more is a bad escape from 3.13, where 3.11 and 3.12 raise OverflowError
#: from `chr`, which is not ``re.error``.
PARSED_PYTHON = [
    B + "z",
    "a" + B + "z",
    "[" + B + "z]",
    B + "U80000000",
    B + "Uffffffff",
    "[" + B + "U80000000]",
]


def rows_python() -> list[dict[str, Any]]:
    """The rows whose answers are CPython 3.13's."""
    return [
        {"part": "atom.parse", **_spelled(pattern), "answer": _line(pattern)}
        for pattern in PARSED_PYTHON
    ]


def rows() -> list[dict[str, Any]]:
    return [*_parsed(), *_escaped(), _sweep(), _inventory(), _re_escape()]
