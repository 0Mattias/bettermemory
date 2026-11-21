"""Fixtures: the leaves of `claims.py` (unit 1): `Claim.render`,
`control_character`, `lone_surrogate`, `_IDENT_RE`, `claim_reason_is_indeterminate`,
`path_key`, `_component_forms` through `spellings_capped` and `stored_spellings`,
`claim_paths`, `_binding_token` (`_DEF_RE`, `_ASSIGN_RE`), `hunk_lines`
(`_HUNK_RE`), `_MARK_HASH_RE`, `_header_path` and `_patch_header_path`.

Unit 1 writes three files, one JSON object per line, keys sorted, ASCII
(unit 2's ``claims_literal.jsonl`` and its rows of ``claims_python.jsonl``
are described with its section below):

- ``claims.jsonl``, not gated. Every line is ``{"f": function, "a":
  [args], "o": output}``. Every text in it is made of code points below
  U+0100, the surrogates and a fixed list of characters assigned in
  Unicode 6.3 or earlier whose properties and normalisation have not
  changed since (`_FIXED`), so its bytes depend on no Unicode table and no
  Python release from 3.11 to 3.14.
- ``claims_unicode.jsonl``, gated on the Unicode version: every code point
  from U+0000 to U+10FFFF in blocks of 0x4000 through six probes
  (`_PROBES`), each block's answers recorded by SHA-256 of their ASCII
  lines (``{cp:x}:{answer}``).
- ``claims_python.jsonl``, gated on the Python release: hunk headers
  whose digit runs meet `int`'s 4,300-digit limit, the default since
  3.11 (and 3.10.7, 3.9.14, 3.8.14 and 3.7.14). The lengths below the
  limit are pinned in ``claims.jsonl`` too. Both generators set the limit
  to 4,300 while they run, so ``PYTHONINTMAXSTRDIGITS`` or
  ``-X int_max_str_digits`` does not change their bytes.

A text is a JSON string where it holds no surrogate and no backslash, else
a list of its code points, or ``{"r": [[text, count], ...]}`` (the pieces
repeated, joined). A raised exception is ``{"raised": class}``.
"""

from __future__ import annotations

import hashlib
import itertools
import sys
import warnings
from collections.abc import Callable
from typing import Any

from bettermemory import claims as c

from ._common import jsonl

_FIXED = (
    "\u0660\u0661\u0669\u0966\u096f\u0301\u0308\u030a\u212b\u2126\uac00"
    "\u1100\u1161\u4e00\u3042\u2028\u2029\u3000\u1680\u200b\ufeff\ue000"
)


def _t(text: str) -> Any:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text) or "\\" in text:
        return [ord(ch) for ch in text]
    return text


def _built(t: Any) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(chr(x) for x in t)
    return "".join(_built(p) * n for p, n in t["r"])


def _stable(text: str) -> bool:
    return all(
        ord(ch) < 0x100
        or 0xD800 <= ord(ch) <= 0xDFFF
        or 0x660 <= ord(ch) <= 0x669
        or 0x966 <= ord(ch) <= 0x96F
        or ch in _FIXED
        for ch in text
    )


#: A count longer than this many digits is written as its length and the
#: SHA-256 of its digits.
_LONG = 64

_ROWS: list[dict[str, Any]] = []


def _row(f: str, args: list[Any], out: Any) -> None:
    for a in args:
        if isinstance(a, str):
            assert _stable(a), (f, a)
    _ROWS.append({"f": f, "a": args, "o": out})


def _texts(texts: list[str]) -> list[Any]:
    return [_t(x) for x in texts]


def _claim(kind: str, path: str, name: str, value: str) -> c.Claim:
    return c.Claim(kind, path, name, value)


def _render_cases() -> None:
    for kind, path, name, value in [
        ("path", "src/a.py", "src/a.py", ""),
        ("absent", "src/gone.py", "src/gone.py", ""),
        ("symbol", "src/a.py", "f", ""),
        ("literal", "src/a.py", "N", "30"),
        ("literal", "", "", ""),
        ("symbol", "a\\b", "x\u0301", "ignored"),
        ("path", "\ud800/x", "y", "z"),
        ("absent", "", "", "v"),
        ("literal", "p::q", "a=b", "'::='"),
    ]:
        out = _claim(kind, path, name, value).render()
        _row("render", [kind, _t(path), _t(name), _t(value)], _t(out))


def _leaf_cases() -> None:
    samples = [
        "", "a", "src/a.py", "a\x00b", "\x1f", " ", "~", "\x7f", "\x80",
        "x\x7fy\x01", "\ud800", "a\udfffb\ud800", "\udc00", "\ue000",
        "caf\u00e9", "\x0a", "\t", "\u2028",
    ]  # fmt: skip
    for s in samples:
        cc = c.control_character(s)
        _row("control_character", [_t(s)], None if cc is None else ord(cc))
        ls = c.lone_surrogate(s)
        _row("lone_surrogate", [_t(s)], None if ls is None else ord(ls))
    idents = [
        "", "a", "_", "A1", "1a", "a b", "a\n", "a\n\n", "\n", "_\n", "a-b",
        "\u00e9", "a\u00e9", "a\u0660", "a\u0301", "\u4e00", "a\u4e00", "a\u2028",
        "a\r", "a\n ", "a\u00b2", "a\u00aa", "a\u00b5", "a\u00ba", "a\u00b7",
        "Z_9", "a\u00d7", "a\u00f7", "a\u0966", "a\u3042", "a\u00a0",
    ]  # fmt: skip
    for s in idents:
        _row("is_identifier", [_t(s)], bool(c._IDENT_RE.match(s)))
    for s in [
        "",
        "could not be read",
        "path 'x' could not be read in the worktree",
        "could not be rea",
        "Could not be read",
        "could not  be read",
        "xcould not be readx",
    ]:
        _row("claim_reason_is_indeterminate", [s], c.claim_reason_is_indeterminate(s))


def _spelling_cases() -> None:
    e_nfd = "e\u0301"
    paths = [
        "", "/", "a/b", "caf\u00e9", f"caf{e_nfd}", f"caf\u00e9/caf{e_nfd}",
        "\u212b", "\u212b/\u00c5/A\u030a", "\u2126", "\uac00", "\u1100\u1161",
        "\ud800", "\u00e9\ud800", "a//\u00e9", "\u00e9/" * 3, "\u00e9/" * 4,
        "\u212b/" * 3 + "\u212b", "\u212b/" * 4, "\u00e9/" * 6, "\u00e9/" * 7,
        "\u00e9/" * 5 + "x", "x\u0308\u0301", "\u00ff", "\u00e9" + "/a" * 70,
        "/".join(["\u212b"] * 4), "/".join(["\u212b"] * 3 + ["\u00e9"] * 2),
        "/".join(["\u212b"] * 3 + ["\u00e9"] * 3), "\u00e9" * 100,
    ]  # fmt: skip
    for p in paths:
        _row("path_key", [_t(p)], _t(c.path_key(p)))
        _row("spellings_capped", [_t(p)], c.spellings_capped(p))
        _row("stored_spellings", [_t(p)], [_t(x) for x in c.stored_spellings(p)])
    claim_sets = [
        [],
        [("path", "a"), ("symbol", "a"), ("path", "b"), ("absent", "a")],
        [("path", "caf\u00e9"), ("path", "cafe\u0301"), ("path", "caf\u00e9")],
        [("literal", ""), ("literal", "")],
    ]
    for cs in claim_sets:
        claims = [_claim(k, p, "n", "1") for k, p in cs]
        _row(
            "claim_paths",
            [[[k, _t(p)] for k, p in cs]],
            [_t(x) for x in c.claim_paths(claims)],
        )


def _token(content: str) -> Any:
    token = c._binding_token(content)
    return None if token is None else [token[0], _t(token[1])]


def _binding_cases() -> None:
    lines = [
        "def f(", "def f(x):", "class A:", "class A(B):", "class A[T]:",
        "def f[T](x):", "async def f(", "async\tdef f(", "async  class C:",
        "asyncdef f(", "async def", "async f(", " def f(", "\tdef f(",
        "def  f  (", "def\tf\t:", "deff(", "def f", "def f ", "def 1f(",
        "def f-(", "def f.g(", "def _(", "def \u00e9(", "def a\u00e9(",
        "def a\u0660(", "def a\u0301(", "def a\u4e00 (", "def a\u2028(",
        "def a\u00a0(", "def\u00a0f(", "class", "class A", "classA:",
        "def f\n(", "def f(\n", "async def\nf(", "DEF f(", "Def f(",
        "x = 1", "x=1", "x == 1", "x==1", "x =", "x=", "x", "x : int = 3",
        "x: int = 3", "x:int=3", "x: = 3", "x:= 3", "x := 3", "x: int == 3",
        "x: a: b = 1", "x: int += 1", "x: int //= 1", "x: \n= 1",
        "x: int\n = 1", "x += 1", "x -= 1", "x *= 1", "x /= 1", "x //= 1",
        "x %= 1", "x **= 1", "x >>= 1", "x <<= 1", "x &= 1", "x ^= 1",
        "x |= 1", "x @= 1", "x ~= 1", "x != 1", "x <= 1", "x >= 1",
        "x ===", "x+=", "x+==", "x **== 1", "x // = 1", "x ++= 1",
        "x\t= 1", "x \t = 1", " x = 1", "1x = 1", "_ = 1", "\u00e9 = 1",
        "a\u00e9 = 1", "a\u0660 = 1", "x\u00a0= 1", "foo(TIMEOUT=30)",
        '"TIMEOUT": 30', "x.y = 1", "x[0] = 1", "x, y = 1, 2", "x=(y==1)",
        "x: dict[str, int] = {}", "x: 'a=b' = 1", "x:a=", "x:a==",
        "x: =", "x::= 1", "x:: = 1", "def = 1", "class = 1", "async = 1",
        "def f(x=1):", "x = def", "", "=", "== 1", ":", "x:", "x: int",
        "x\n= 1", "x = 1\n", "x:\u2028= 1", "x:\r= 1", "x %== 1",
        "lambda_ = lambda: 0", "if x == 1:", "for x in y:",
    ]  # fmt: skip
    for line in lines:
        _row("binding_token", [_t(line)], _token(line))


def _hunk(line: str) -> Any:
    try:
        n = c.hunk_lines(line)
    except ValueError:
        return {"raised": "ValueError"}
    if n is None:
        return None
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        text = str(n)
    finally:
        sys.set_int_max_str_digits(limit)
    if len(text) > _LONG:
        return {"n": len(text), "h": hashlib.sha256(text.encode("ascii")).hexdigest()}
    return text


def _hunk_cases() -> None:
    lines = [
        "@@ -1 +1 @@", "@@ -1,0 +1,0 @@", "@@ -1,2 +3,4 @@", "@@ -1,2 +3,4 @@ def f():",
        "@@ -1,2 +3,4 @@@", "@@ -0,0 +1 @@", "@@ -007,010 +1,099 @@",
        "@@ -1 +1@@", "@@ -1+1 @@", "@@ -1, +1 @@", "@@ -1,2, +1 @@",
        "@@ -1 +1,", "@@ -1 +1, @@", "@@ - 1 +1 @@", "@@ -a +1 @@", "@@ +1 -1 @@",
        "@@-1 +1 @@", " @@ -1 +1 @@", "@@ -1 +1 @@\n", "@@ -1\n +1 @@", "@@",
        "", "@@ -1 +1", "@@ -1 +1 @", "@@  -1 +1 @@", "@@ -1  +1 @@",
        "@@@ -1 +1 @@@", "@@ -\u0661 +\u0662 @@", "@@ -\u0669\u0660,\u0966 +1 @@",
        "@@ -1\u0661 +\u096f,\u0966\u0966 @@", "@@ -\u00b2 +1 @@", "@@ -\u00bd +1 @@",
        "@@ -99999999999999999999 +1,99999999999999999999 @@",
        "@@ -1,18446744073709551615 +1,18446744073709551615 @@",
        "@@ -1,9223372036854775807 +1,1 @@", "@@ -1,0000 +1,0 @@",
        "@@ -1,-1 +1 @@", "@@ -1,+1 +1 @@", "@@ -1_0 +1 @@", "@@ -1,1_0 +1 @@",
        "@@ -1 +1 @@" + "x" * 50,
    ]  # fmt: skip
    for line in lines:
        _row("hunk_lines", [_t(line)], _hunk(line))
    for n in (1, 4299, 4300):
        for digits in ("9", "0"):
            for where in ("removed", "added", "start"):
                parts = {
                    "removed": [["@@ -1,", 1], [digits, n], [" +1 @@", 1]],
                    "added": [["@@ -1 +1,", 1], [digits, n], [" @@", 1]],
                    "start": [["@@ -", 1], [digits, n], [" +1 @@", 1]],
                }[where]
                recipe = {"r": parts}
                _row("hunk_lines", [recipe], _hunk(_built(recipe)))


def _mark_cases() -> None:
    marks = [
        "", "0" * 40, "a" * 40, "f" * 64, "0" * 39, "0" * 41, "0" * 63, "0" * 65,
        "A" * 40, "g" * 40, "0" * 40 + "\n", "0" * 64 + "\n", " " + "0" * 40,
        "0123456789abcdef" * 4, "0" * 104, "0" * 80, "\u0660" * 40,
        "0" * 39 + "\u0660", "0" * 39 + "F",
    ]  # fmt: skip
    for m in marks:
        _row("is_mark_hash", [_t(m)], bool(c._MARK_HASH_RE.fullmatch(m)))


def _header_cases() -> None:
    rests = [
        "", "a/x b/x", "a/x b/y", "a/xy b/xy", "a/x b/x ", "a/ b/", "a/a b/a b/a",
        "a/a b/a b/a b/a", "a/ b/ b/ b/", "a/b/ b/ b/ b/", "b/x a/x", "a/x  b/x",
        "a/x\tb/x", "a/xx b/x", "x/x b/x", "a/x c/x", "a/\u00e9 b/\u00e9",
        "a/\u00e9 b/e\u0301", "a/x b/x\n", "a/\ud800 b/\ud800", "a/xyz",
        "a/x b", "a/x b/", "a/xx b/xx", "a/\x00 b/\x00", "a/ab b/ab",
        "a/\" b/\"", "a/x\\ b/x\\",
    ]  # fmt: skip
    for r in rests:
        _row("header_path", [_t(r)], _t(c._header_path(r)))


def _patch_cases() -> None:
    cases: list[tuple[str, str, list[str], list[str]]] = [
        ("+++ b/x", "", [], []),
        ("+++ b/x", "y", ["y"], []),
        ("+++ /dev/null", "x", [], []),
        ("+++ /dev/null", "", [], []),
        ("+++ /dev/null ", "x", ["x"], ["x"]),
        ("+++ \t/dev/null\t", "x", [], []),
        ("+++ /dev/nullx", "x", [], []),
        ("+++ x", "", [], []),
        ("+++ b/", "", [], []),
        ("+++ ", "p", [], []),
        ("+++", "p", [], []),
        ("+++ b/a b\t", "", [], []),
        ("+++ b/x \t", "", [], []),
        ("+++ b/x\u00a0", "", [], []),
        ("+++ b/x\u3000", "", [], []),
        ("+++ b/x\u2028", "", [], []),
        ("+++ b/x\u200b", "", [], []),
        ("+++ b/x\ufeff", "", [], []),
        ("+++ b/x\x1c", "", [], []),
        ("+++ b/x\x85", "", [], []),
        ("+++ b/x\u1680", "", [], []),
        ('+++ "b/x"', "", [], []),
        ("+++ b/b/x", "", [], []),
        ("+++  b/x", "", [], []),
        ("--- a/x", "", [], []),
        ("--- a/x", "y", [], []),
        ("--- a/x ", "", ["x"], []),
        ("--- /dev/null", "p", [], []),
        ("--- b/x", "p", [], []),
        ("--- a/", "p", [], []),
        ("---  a/x", "p", [], []),
        ("--- a/ x", "p", [], []),
        ("---", "p", [], []),
        ("--- ", "", [], []),
        ("@@ -1 +1 @@", "p", [], []),
        ("", "p", [], []),
        ("diff --git a/x b/x", "p", [], []),
        ("++++ b/x", "p", [], []),
        ("+++ b/\ud800", "", [], []),
        ("+++ b/x", "x", ["x"], ["x"]),
    ]
    for line, path, files, deleted in cases:
        fs, ds = set(files), set(deleted)
        out = c._patch_header_path(line, path, fs, ds)
        _row(
            "patch_header_path",
            [_t(line), _t(path), _texts(files), _texts(deleted)],
            {
                "r": None if out is None else _t(out),
                "files": _texts(sorted(fs)),
                "deleted": _texts(sorted(ds)),
            },
        )


#: The default of `sys.get_int_max_str_digits`, which the port hardcodes.
_INT_DIGITS = 4300


def _default_digits(make: Callable[[], dict[str, bytes]]) -> dict[str, bytes]:
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        # `literal_eval` warns on an unknown escape (`'\\z'`); the answer is
        # the same, and the run stays quiet.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return make()
    finally:
        sys.set_int_max_str_digits(limit)


def _constant_cases() -> None:
    for name in ("MAX_CLAIMS", "MAX_CLAIM_LEN", "MAX_SPELLINGS"):
        _row("constant", [name], getattr(c, name))
    _row("constant", ["COMMIT_MARK"], _t(c.COMMIT_MARK))
    _row("constant", ["INDETERMINATE_REASON_MARKER"], _t(c.INDETERMINATE_REASON_MARKER))


def generate() -> dict[str, bytes]:
    return _default_digits(_generate)


def _generate() -> dict[str, bytes]:
    _ROWS.clear()
    _constant_cases()
    _render_cases()
    _leaf_cases()
    _spelling_cases()
    _binding_cases()
    _hunk_cases()
    _mark_cases()
    _header_cases()
    _patch_cases()
    rows = list(_ROWS)
    _ROWS.clear()
    return {"claims.jsonl": jsonl(rows)}


# --- every code point (gated on Unicode) ---------------------------------


def _spellings_answer(ch: str) -> str:
    out = c.stored_spellings(ch + "/e\u0301")
    return ",".join("-".join(f"{ord(x):x}" for x in s) for s in out)


def _patch_answer(ch: str) -> str:
    fs: set[str] = set()
    ds: set[str] = set()
    out = c._patch_header_path("+++ b/" + ch + "x" + ch, "", fs, ds)
    assert out is not None
    return "-".join(f"{ord(x):x}" for x in out)


def _token_answer(line: str) -> str:
    token = c._binding_token(line)
    if token is None:
        return "-"
    return token[0] + ":" + "-".join(f"{ord(x):x}" for x in token[1])


_PROBES: dict[str, Any] = {
    "is_identifier": lambda ch: str(int(bool(c._IDENT_RE.match("a" + ch)))),
    "def": lambda ch: _token_answer("def a" + ch + "("),
    "assign": lambda ch: _token_answer("a" + ch + " = 1"),
    "hunk": lambda ch: str(c.hunk_lines("@@ -1," + ch + " +1 @@")),
    "patch": _patch_answer,
    "spellings": _spellings_answer,
}


def generate_unicode() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for name, probe in _PROBES.items():
        for lo in range(0, 0x110000, 0x4000):
            h = hashlib.sha256()
            for cp in range(lo, lo + 0x4000):
                h.update(f"{cp:x}:{probe(chr(cp))}\n".encode("ascii"))
            rows.append({"f": name, "lo": lo, "hi": lo + 0x4000, "h": h.hexdigest()})
    return {"claims_unicode.jsonl": jsonl(rows)}


def generate_python() -> dict[str, bytes]:
    return _default_digits(_generate_python)


def _generate_python() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for n in (4300, 4301, 5000):
        for digits in ("9", "0", "\u0660"):
            for prefix, suffix in (
                ("@@ -1,", " +1 @@"),
                ("@@ -1 +1,", " @@"),
                ("@@ -", " +1 @@"),
                ("@@ -", " +1,1 @@"),
            ):
                recipe = {"r": [[prefix, 1], [_t(digits), n], [suffix, 1]]}
                rows.append(
                    {"f": "hunk_lines", "a": [recipe], "o": _hunk(_built(recipe))}
                )
    rows += _literal_python_rows()
    return {"claims_python.jsonl": jsonl(rows)}


# --- unit 2: the claim parser and the literal readers ----------------------
#
# ``claims_literal.jsonl``, not gated: `parse_claim`, `parse_claims`,
# `load_claims`, `claim_is_addressable`, `governed_claim_paths`,
# `_rhs_repr`, `string_fragment` and `anchors_from_value` per branch and
# message, on texts `_stable` admits, and a sweep row: every text of one to
# four characters over `_SWEEP_ALPHABET`, in `itertools.product` order,
# through the four text functions, recorded by count and SHA-256 of
# `_sweep_line` per text. Values whose reading follows the release (complex
# arithmetic, `int`'s digit limit, the nesting limit) go to
# ``claims_python.jsonl``. A refusal is ``{"raised": "ValueError", "m":
# message}``; a claim is ``[kind, rel_path, name, value]``.

_SWEEP_ALPHABET = " !:=a1'\\(),\n"
_SWEEP_MAX = 4


def _hx(text: str) -> str:
    return "-".join(f"{ord(x):x}" for x in text)


def _claim_out(claim: c.Claim) -> list[Any]:
    return [claim.kind, _t(claim.rel_path), _t(claim.name), _t(claim.value)]


def _parse(raw: str) -> Any:
    try:
        return _claim_out(c.parse_claim(raw))
    except ValueError as e:
        return {"raised": "ValueError", "m": _t(str(e))}


def _parse_list(raw: list[str]) -> Any:
    try:
        return [_claim_out(x) for x in c.parse_claims(raw)]
    except ValueError as e:
        return {"raised": "ValueError", "m": _t(str(e))}


def _opt(text: str | None) -> Any:
    return None if text is None else _t(text)


def _sweep_line(text: str) -> str:
    try:
        cl = c.parse_claim(text)
        pc = f"{cl.kind}:{_hx(cl.rel_path)}:{_hx(cl.name)}:{_hx(cl.value)}"
    except ValueError as e:
        pc = "E" + _hx(str(e))
    sf = c.string_fragment(text)
    rr = c._rhs_repr(text)
    an = ",".join(_hx(x) for x in c.anchors_from_value(text))
    sfs = "-" if sf is None else "s" + _hx(sf)
    rrs = "-" if rr is None else "s" + _hx(rr)
    return f"{_hx(text)}|{pc}|{sfs}|{rrs}|{an}\n"


_VALUES = [
    "1", "30", "30.0", "1_000", "0x1F", "0o17", "0b101", "1e3", "-1", "+1",
    "--1", "-(1)", "- 1", "None", "True", "False", "...", "Ellipsis", "'foo'",
    '"foo"', "'''a\nb'''", "b'x'", "rb'\\x'", "u'x'", "f'x'", "'a' 'b'",
    "[1, 2]", "[1,2,]", "(1,)", "()", "(1)", "((1,))", "{}", "{1, 2}",
    "{3, 1, 2}", "{'b': 1, 'a': 2}", "{'a': 1, 'a': 2}", "{1, True, 1.0}",
    "set()", "set([])", "set ()", "frozenset()", "foo", "foo()", "1 +", "[1,",
    "'\\z'", "'\\u00e9'", "'\\ud800'", "'\\x00'", "'a\x00'", "1 # c", "1;",
    "1 if 1 else 2", "x.y", "a[0]", "{**a}", "[*a]", "[1, [2, (3,)]]",
    "{'k': {2, 1}}", "'\u2028'", "'caf\u00e9'", "'\u00e9'", "'''x\n y'''",
    "(1,\n2)", "-0.0", "1e999", "-1e999", "0.1", "1.5e-7", "00", "01", "0_0",
    "1__0", "'a\\\nb'", "b'\\xff'", "Ellipsis()", "not 1", "~1", "1 + 1",
    "'a' + 'b'", "[] + []", "lambda: 1", "(yield)", "await x", "x := 1",
    "'''", "'a", "\"\"\"\"\"\"", "''", "b''", "{1: 2, 3: 4}[1]", "[]",
    "1\u30002", "\u0661", "'\u0661'",
]  # fmt: skip


def _parse_claim_cases() -> None:
    raws = [
        "", "   ", "\t\n", " \u3000 ", "!", "! ", "!  \t", "!\u3000",
        "!src/gone.py", "! src/gone.py", "!\u3000x", "!!x", "!x::y",
        "! x :: y=1", "!::", "! ::x", "!a\\b", "!a\\b::c", "::", " :: x",
        "::x", "a::", "a:: ", "a::  \t", "a:::b", "a::b::c", "a\\b", "a\\b::c",
        "a/b", " a/b ", "a b", "a/b::f", "a/b::1f", "a/b::f g", "a/b::f-g",
        "a/b:: f ", "a/b::\u00e9", "a/b::a\u00e9", "a/b::\u00aa", "a/b::_",
        "a/b::f=", "a/b::f= ", "a/b::f=1", "a/b::f = 1 ", "a/b::f==1",
        "a/b::=1", "a/b::f=1=2", "a/b::f='a=b'", "a/b::f\n=1", "a/b::f=\n1",
        "a\x01b::x", "\ud800::x", "a\x7f", "\x00", "a: :b", "a:b::c",
        "a::b:c", "a::f:int=1", "a::f\u3000=\u30001", "!\n", "!\x1c",
        "\x1ca\x1d", "\x85a\xa0", "a::f=1\n\n", "a::f=(1,\n 2)",
        "a::f='''x\n y'''", "a::f='x'\n", "a::f=#", "a::f= # c",
    ]  # fmt: skip
    raws += [f"src/m.py::NAME={v}" for v in _VALUES]
    for raw in raws:
        _row("parse_claim", [_t(raw)], _parse(raw))
    for piece, n in (
        ("a", 1024),
        ("a", 1025),
        ("\u00e9", 1024),
        ("\u00e9", 1025),
        ("\ud800", 1025),
        ("\u4e00", 1100),
    ):
        recipe = {"r": [[_t(piece), n]]}
        _row("parse_claim", [recipe], _parse(_built(recipe)))
    for pre, piece, n, post in (
        (" ", "a", 1024, " "),
        ("!", "a", 1023, ""),
        ("!", "a", 1024, ""),
        ("p::N='", "x", 1015, "'"),
        ("p::N='", "x", 1016, "'"),
        ("\t", "b", 1024, "\n\u3000"),
    ):
        recipe = {"r": [[_t(pre), 1], [_t(piece), n], [_t(post), 1]]}
        _row("parse_claim", [recipe], _parse(_built(recipe)))


def _parse_claims_cases() -> None:
    lists: list[list[str]] = [
        [], ["a"], ["a", " a ", "a::f", "a::f", "a::X=1", "a::X=0x1", "a::X=1.0"],
        ["a::X={1, 2}", "a::X={2, 1}", "a::X={2,1,}"], ["!a", "a", "! a"],
        ["a\x01b"], ["a\x7f"], ["a\tb"], ["\x00"], ["\ud800x"], ["a\udfff"],
        ["ok", "bad\\path", "a\x01"], ["ok", "a\x01", "bad\\path"],
        ["a\x01\ud800"], ["a::N=1", "a::N=+1", "a::N=-1", "a::N=--1"],
        ["a/\u00e9", "a/e\u0301"], ["x", ""], ["\x1fa::f"], ["!a\x7f"],
    ]  # fmt: skip
    cap = c.MAX_CLAIMS
    lists.append([f"p{i}" for i in range(cap)])
    lists.append([f"p{i}" for i in range(cap + 1)])
    lists.append(["bad\\"] + [f"p{i}" for i in range(cap)])
    lists.append(["bad\\"] * (cap - 1))
    lists.append(["p"] * cap)
    lists.append(["p::N=1"] * 200)
    for raw in lists:
        assert all(_stable(x) for x in raw), raw
        _row("parse_claims", [_texts(raw)], _parse_list(raw))


def _load_claims_cases() -> None:
    lists: list[list[Any]] = [
        [], ["a"], ["a", None, 1, "", "a::1x", "b::X=1", [], True, "a", "\\x"],
        ["a\x01", "\ud800", "a::X=foo", "a::X='foo'"], [None], [b"a"],
        ["a", "a", " a"], [("a",), "b"], ["!a::b", "!a"],
        [f"p{i}" for i in range(c.MAX_CLAIMS + 3)],
    ]  # fmt: skip
    for raw in lists:
        enc: list[Any] = []
        for x in raw:
            if isinstance(x, str):
                assert _stable(x)
                enc.append({"s": _t(x)})
            elif x is None or isinstance(x, (bool, int)):
                enc.append(x)
            else:
                enc.append({"other": type(x).__name__})
        _row("load_claims", [enc], [_claim_out(x) for x in c.load_claims(raw)])


_ADDR = [
    ("path", "a", "a", ""), ("symbol", "a", "f", ""), ("absent", "a", "a", ""),
    ("path", "a", "a", "[1]"), ("symbol", "a", "f", "not literal"),
    ("literal", "a", "N", "1"), ("literal", "a", "N", "'x'"),
    ("literal", "a", "N", "[1]"), ("literal", "a", "N", "()"),
    ("literal", "a", "N", "{}"), ("literal", "a", "N", "set()"),
    ("literal", "a", "N", "{1}"), ("literal", "a", "N", "(1,)"),
    ("literal", "a", "N", "not literal"), ("literal", "a", "N", ""),
    ("literal", "a", "N", "-1"), ("literal", "a", "N", "frozenset()"),
    ("literal", "a", "N", "'''a\nb'''"), ("literal", "a", "N", "None"),
    ("literal", "a", "N", "..."), ("literal", "a", "N", "b'x'"),
    ("literal", "a", "N", " [1]"), ("literal", "a", "N", "\t1"),
    ("literal", "a", "N", "(1)"), ("literal", "a", "N", "[1"),
    ("literal", "a", "N", "{'a': [1]}"), ("literal", "a", "N", "'[1]'"),
]  # fmt: skip


def _addressable_cases() -> None:
    for kind, path, name, value in _ADDR:
        cl = c.Claim(kind, path, name, value)
        _row(
            "claim_is_addressable",
            [kind, _t(path), _t(name), _t(value)],
            c.claim_is_addressable(cl),
        )
    groups: list[list[tuple[str, str, str, str]]] = [
        [],
        [("path", "a", "a", ""), ("literal", "b", "N", "1")],
        [("path", "a", "a", ""), ("literal", "a", "N", "[1]")],
        [("literal", "a", "N", "[1]"), ("path", "a", "a", ""), ("path", "b", "b", "")],
        [("literal", "a", "N", "bad"), ("symbol", "b", "f", ""), ("absent", "a", "a", "")],
        [("path", "caf\u00e9", "x", ""), ("literal", "cafe\u0301", "N", "{1}"),
         ("path", "cafe\u0301", "x", ""), ("path", "cafe", "x", "")],
        [("path", "\u212b", "x", ""), ("literal", "\u00c5", "N", "()")],
        [("path", "\u2126", "x", ""), ("literal", "\u03a9", "N", "[]"),
         ("path", "\u03a9", "x", "")],
        [("path", "b", "b", ""), ("path", "a", "a", ""), ("path", "b", "b", "")],
        [("literal", "a", "N", "1"), ("literal", "a", "M", "'x'")],
        [("path", "\ud800", "x", ""), ("literal", "\ud800", "N", "[]")],
    ]  # fmt: skip
    for group in groups:
        cls = [c.Claim(*g) for g in group]
        enc = [[k, _t(p), _t(n), _t(v)] for k, p, n, v in group]
        _row("governed_claim_paths", [enc], _texts(c.governed_claim_paths(cls)))


def _line_cases() -> None:
    rhs = [
        "X = 1", "X=1", "X", "", "=", "X == 1", "X: int = 30", "X = [1,",
        "X = 'a' # c", "X = {'b': 1, 'a': 2}", "X += 1", "x = y = 1", "=1",
        "X = ", "X = (", "X = '''", "X = 0x10", "X = 30.0", "X = -0.0",
        "X = {2, 1}", "X = 'a=b'", "X = \u30001\u3000", "X = '\\u00e9'",
        "X = b'\\x00'", "X = None", "X = foo", "X = (1,)", "'a=1'",
        "f(x=1)", "X = \"a\" \"b\"", "X = '''x\n'''", "X\n= 1",
    ]  # fmt: skip
    for line in rhs:
        _row("rhs_repr", [_t(line)], _opt(c._rhs_repr(line)))
    frags = [
        "'a'", "  'a',", "'a',,,", "('a')", "('a'),", "('a',)", "'a')",
        "('a'", "(('a'))", "'a' 'b'", "'a' + 'b'", "b'a'", "\"a\\\"b\"",
        "'a\\nb'", "'''a'''", "'''a", "  )", "(", "", ",", "'a' # c", "1",
        "'a'.strip()", "'a'[0]", "'1' if x else '2'", "f'a'", "'a',)",
        "'a'),", " ( 'a' ) ,", "(\"x\")", "\"\"", "''", "'a', 'b'",
        "'a'\u3000,", "\u3000'a'\u3000", "'a',\n", "'\\N'", "'\\z'",
        "\"it's\"", "'a' \\", "('a') + ('b')", "))", "((", "'a'))", "(('a')",
        "'caf\u00e9',", "'a\\\nb'", "'\\ud800'", "'''x\ny'''", "u'a'",
        "'a',)),", "),'a'", ",'a'",
    ]  # fmt: skip
    for line in frags:
        _row("string_fragment", [_t(line)], _opt(c.string_fragment(line)))
    values = [
        "'short\\nlines'", "'abcdefghijkl\\nabcdefghijk'",
        "'''  twelve chars ok  \n twelve chars ok\nx'''",
        "'no newline but long enough'", "1", "['a\\nb']",
        "b'abcdefghijklmn\\nabcdefghijklmn'", "'\\n'", "''",
        "'abcdefghijkl\\r\\nabcdefghijkl'",
        "'a\\u2028abcdefghijklmnop\\nq'", "not a literal",
        "'aaaaaaaaaaaa\\nbbbbbbbbbbbb\\naaaaaaaaaaaa\\n  aaaaaaaaaaaa  '",
        "'\\u3000abcdefghijkl\\u3000\\nab'", "'abc def ghij\\nk'",
        "'abc def gh\\nk'", "'\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\n'",
        "('first line is long\\n' 'second line is long')",
        "'\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\\n'",
        "'e\u0301e\u0301e\u0301e\u0301e\u0301e\u0301\\n'", "'\\ud800' * 2",
        "'\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\n'",
    ]  # fmt: skip
    for value in values:
        _row(
            "anchors_from_value", [_t(value)], _texts(list(c.anchors_from_value(value)))
        )
    recipe = {"r": [["'", 1], ["abcdefghijklmnop\\n", 300], ["'", 1]]}
    _row(
        "anchors_from_value",
        [recipe],
        _texts(list(c.anchors_from_value(_built(recipe)))),
    )


def _sweep_row() -> None:
    h = hashlib.sha256()
    n = 0
    for k in range(1, _SWEEP_MAX + 1):
        for chars in itertools.product(_SWEEP_ALPHABET, repeat=k):
            h.update(_sweep_line("".join(chars)).encode("ascii"))
            n += 1
    _ROWS.append(
        {
            "f": "sweep",
            "alphabet": _SWEEP_ALPHABET,
            "max": _SWEEP_MAX,
            "n": n,
            "h": h.hexdigest(),
        }
    )


def generate_literal() -> dict[str, bytes]:
    return _default_digits(_generate_literal)


def _generate_literal() -> dict[str, bytes]:
    _ROWS.clear()
    _row("constant", ["MIN_ANCHOR_CHARS"], c._MIN_ANCHOR_CHARS)
    _parse_claim_cases()
    _parse_claims_cases()
    _load_claims_cases()
    _addressable_cases()
    _line_cases()
    _sweep_row()
    rows = list(_ROWS)
    _ROWS.clear()
    return {"claims_literal.jsonl": jsonl(rows)}


#: Values whose reading follows the release: complex arithmetic, `int`'s
#: digit limit and the nesting depth.
_PYTHON_VALUES = [
    "1j", "-0j", "1+2j", "1-2j", "-1-0j", "(1+2j)", "1+-2j", "2j+1",
    "1j+1j", "1+2", "1.5-0.5j", "0j", "-(0j)", "1e999j", "-1e999-1e999j",
    "[1+1j]", "{1j, 1}", "{1j: 1}",
]  # fmt: skip


def _literal_python_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for v in _PYTHON_VALUES:
        raw = "p::N=" + v
        rows.append({"f": "parse_claim", "a": [raw], "o": _parse(raw)})
        rows.append(
            {"f": "rhs_repr", "a": ["N = " + v], "o": _opt(c._rhs_repr("N = " + v))}
        )
        cl = c.Claim("literal", "p", "N", v)
        rows.append(
            {
                "f": "claim_is_addressable",
                "a": ["literal", "p", "N", v],
                "o": c.claim_is_addressable(cl),
            }
        )
    recipes = [
        [["p::N=", 1], ["9", 1018]],
        [["p::N=", 1], ["(", 199], ["1", 1], [")", 199]],
        [["p::N=", 1], ["(", 200], ["1", 1], [")", 200]],
        [["p::N=", 1], ["[", 201], ["]", 201]],
        [["p::N=", 1], ["-", 500], ["1", 1]],
    ]
    for r in recipes:
        recipe = {"r": r}
        rows.append({"f": "parse_claim", "a": [recipe], "o": _parse(_built(recipe))})
    for n in (4300, 4301):
        for line in (("N = ", "7"),):
            recipe = {"r": [[line[0], 1], [line[1], n]]}
            rows.append(
                {"f": "rhs_repr", "a": [recipe], "o": _opt(c._rhs_repr(_built(recipe)))}
            )
    for hexes in (3571, 3572):
        recipe = {"r": [["N = 0x", 1], ["f", hexes]]}
        rows.append(
            {"f": "rhs_repr", "a": [recipe], "o": _opt(c._rhs_repr(_built(recipe)))}
        )
    return rows
