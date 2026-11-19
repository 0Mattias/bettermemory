"""Fixtures: one path candidate validated (``verify_candidates.jsonl``,
``verify_candidates_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/verify_candidates/validate.rs``.
This pins it to `verify_candidates`' `_normalize_candidate`,
`_trim_candidate`, `_command_shaped`, `_anchored_candidate`,
`_LINE_SUFFIX_RE`, `_is_placeholder_path`, `_is_single_segment_routelike`,
`_refused_spelling`, `_escapes_root`, `_fold_altsep` and
`_bounded_scan_text`, and to the module's constants (the header).

Nothing here reads the machine. A line is one case:

- ``{"f", "a", "r"}``: the function ``f`` called with the arguments ``a``
  answers ``r``;
- ``{"f", "alphabet", "max", "hits"}``: ``f`` called with every text of
  one to ``max`` code points over ``alphabet``, in `itertools.product`'s
  order, answers its default (None, False, or the text itself for
  ``trim_candidate``) except for the texts ``hits`` lists, each
  ``[text, answer]``;
- ``{"f": "bounded_scan_text", "runs", "r"}``: the body is the runs
  (``[text, times]``) joined, and ``r`` the length of the answer, which is
  always the body's prefix.

A text holding a surrogate or two backslashes in a row (which the
golden directory's host-fact guard reads as a UNC prefix) is
``{"cps": [...]}``; a raise is
``{"raise": class name}``. A case whose text is not all ASCII goes in
``_unicode``, which also holds the code points `str.islower` reads as
lowercase (``lower``, as ranges): its answers are the Unicode data's.
"""

from __future__ import annotations

import itertools
import unicodedata
from collections.abc import Callable, Iterator
from typing import Any

from bettermemory import verify_candidates as vc

from ._common import has_surrogate, jsonl


def _t(value: Any) -> Any:
    if isinstance(value, str) and (has_surrogate(value) or "\\\\" in value):
        return {"cps": [ord(c) for c in value]}
    return value


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        got = fn(*args)
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raise": type(exc).__name__}
    if isinstance(got, bool) or got is None or isinstance(got, str):
        return _t(got)
    if isinstance(got, int):
        return got
    return _t(got.group(1)) if got is not None else None


def _line_suffix(s: str) -> Any:
    return vc._LINE_SUFFIX_RE.match(s)


FUNCS: dict[str, Callable[..., Any]] = {
    "normalize_candidate": vc._normalize_candidate,
    "trim_candidate": vc._trim_candidate,
    "command_shaped": vc._command_shaped,
    "anchored_candidate": vc._anchored_candidate,
    "line_suffix": _line_suffix,
    "is_placeholder_path": vc._is_placeholder_path,
    "is_single_segment_routelike": vc._is_single_segment_routelike,
    "refused_spelling": vc._refused_spelling,
    "escapes_root": vc._escapes_root,
    "fold_altsep": vc._fold_altsep,
}

# A path's spellings, prose and the edges of each rule.
PATHS = [
    "",
    " ",
    "/",
    "~",
    "~/",
    "~/a",
    "/a",
    "/ab",
    "/abc",
    "/a/b",
    "/srv/",
    "/srv///",
    "//",
    "/\\x",
    "\\/x",
    "\\\\host\\share",
    "//host/share/x",
    "a\0b",
    "/a/b\0",
    "http://x.example/y",
    "file:///etc/hosts",
    "$HOME/x/y",
    "${HOME}/x/y",
    "$HOME",
    "$HOME/",
    "${HOME}",
    "$HOMEx/y",
    "~/Google\\ Drive/notes.txt",
    "/a\\ b/c\\ d",
    "\\ \\ .",
    "\\ \\ ",
    "\\\\ x",
    "/a/b.,;:!?)>]}\"'",
    "/a/b (x86)",
    "/a/b (x86))",
    "/a/b)",
    "(/a/b)",
    "/a/(b)).",
    "/etc/foo",
    "/etc/foo.conf",
    "/etc/foo.bar.baz",
    "/etc/foo/",
    "/foo",
    "/foo.txt",
    "/foo/bar/baz.py",
    "/path/to",
    "/path/to/x",
    "/path/to.d",
    "~/path/to/x",
    "~/path/to",
    "~/.config",
    "~/.claude-memory",
    "/robots.txt",
    "/ROBOTS.TXT",
    "/Index.Html",
    "/x/robots.txt",
    "/verify",
    "/healthz",
    "/api/v1/events",
    "/etc",
    "user@host:/srv/x",
    "git@github.com:o/r.git",
    "/opt/homebrew/opt/python@3.12",
    "/a@b:c/d",
    "a@b/c:d",
    "@:",
    "src/foo.py:407",
    "/a/foo.py:407",
    "/a/foo.py:407-461",
    "/a/foo.py:12:5",
    "/a/foo.py:12:5:6",
    "/a/foo.py:",
    "/a/foo.py:x",
    "/a/foo.py:1\n",
    "/a/foo.py:1\n.",
    "/a/foo.py:1\n\n",
    "/a/b:c:1",
    "/a/:1",
    "a\n/b:1",
    "/a\n/b:1",
    "/a/b\n:1",
    "C:\\Program Files\\x",
    "C:/Home/me/x",
    "C:x",
    "C:",
    "c:\\",
    "1:\\x",
    "/var/log/app/*.log",
    "~/.config/<app>/settings.toml",
    "/opt/stacks/{service}/data",
    "/a/b?",
    "/a/b?x",
    "/usr/bin/env python -m bettermemory",
    "/opt/homebrew/bin/brew upgrade",
    "/plugin install foo",
    "/plugin install owner/repo",
    "/Srv/My Stuff/file",
    "~/Calibre Library/x",
    "~/Library/Application Support/x",
    "C:\\Program Files (x86)\\x",
    "/a/b Word/c",
    "/a/b -x",
    "/a/b\tc/d",
    "/a/b\tx",
    "/a/b c",
    "/a/b C",
    "/a/b C1",
    "/a/b c1",
    "/a/b c/d",
    "a b",
    "~/a b",
    "~/a/b c",
    " /a/b ",
    "\t/a/b/\t",
    "/a/b/..",
    "../x",
    "a/../../x",
    "a/b/../../c",
    "a/./b",
    "./x",
    "x/..",
    "x\\..\\..",
    "\\x",
    "C:/x",
    "c:x",
    "a/b/c",
]

# Code points past ASCII, each where a table decides.
UNICODE = [
    "/ünï/x",
    "/a/b É/c",
    "/a/b ǅx/c",
    "/a/b ªb/c",
    "/a/b ᴬx/c",
    "/a/b ⅰ/c",
    "/a/b éa/c",
    "/a/b ﬁ/c",
    "/a/b 中文/c",
    "/a/b ß/c",
    "/a/b\u00a0x/c",
    "/a/b\u00a0x c/d",
    "/a\u2028b c/d",
    "\u3000/a/b\u3000",
    "\u2028\\ \\ .",
    "Ⅰ:\\x",
    "é:/x",
    "中:/x",
    "/a/b:١٢",
    "/a/b:١-٢",
    "/a/b:²",
    "/İNDEX.HTML",
    "/ROBOTS.TXT\u0130",
    "/\u212aads.txt",
    "/Ａ.txt",
    "/a/\ud800",
    "/a/\ud800 b/c",
    "~/\udfff",
    "/a/b \ud800/c",
    "\ud800:/x",
]

# The `str.islower` and `str.isalpha` of one code point, read in a later word.
UNICODE_WORDS = [
    chr(c)
    for c in (0xAA, 0x2B0, 0x345, 0x2170, 0x24D0, 0x1E030, 0x1C5, 0xE9, 0xC9, 0x4E2D)
]


def _rows(texts: list[str]) -> Iterator[dict[str, Any]]:
    for name in (
        "normalize_candidate",
        "trim_candidate",
        "command_shaped",
        "anchored_candidate",
        "line_suffix",
        "is_placeholder_path",
        "is_single_segment_routelike",
        "refused_spelling",
        "escapes_root",
    ):
        for text in texts:
            yield {"f": name, "a": [_t(text)], "r": _call(FUNCS[name], text)}


def _fold_rows() -> Iterator[dict[str, Any]]:
    for p, sep, alt in (
        ("C:/Home/me/x", "\\", "/"),
        ("C:\\Home\\me/x", "\\", "/"),
        ("a\\b", "/", None),
        ("a\\b", "/", ""),
        ("aaaa", "b", "aa"),
        ("abab", "x", "ab"),
        ("", "\\", "/"),
        ("x//y", "", "/"),
    ):
        yield {
            "f": "fold_altsep",
            "a": [_t(p), _t(sep), _t(alt)],
            "r": _t(vc._fold_altsep(p, sep, alt)),
        }


def _length_rows() -> Iterator[str]:
    cap = vc._MAX_PATH_LENGTH
    for n in (cap - 2, cap - 1, cap, cap + 1):
        yield "/a/" + "b" * (n - 3)
    for n in (cap + 62, cap + 63, cap + 64, cap + 65):
        yield "/a/" + "b" * (cap - 3) + "." * (n - cap)
        yield "/a/" + "b" * (cap - 2) + "." * (n - cap - 1)
    yield "/" + "a" * (cap - 1) + ")" * 64


def _exhaustive(name: str, alphabet: str, top: int, default: Any) -> dict[str, Any]:
    hits = []
    for n in range(1, top + 1):
        for chars in itertools.product(alphabet, repeat=n):
            text = "".join(chars)
            got = _call(FUNCS[name], text)
            want = text if default == "self" else default
            if got != want:
                hits.append([_t(text), got])
    return {
        "f": name,
        "alphabet": alphabet,
        "max": top,
        "default": default,
        "hits": hits,
    }


def _bounded_rows() -> Iterator[dict[str, Any]]:
    cap = vc._MAX_BODY_SCAN_BYTES
    shapes: list[list[tuple[str, int]]] = [
        [],
        [("a", cap)],
        [("a", cap + 1)],
        [(" ", 1), ("a", cap)],
        [(" ", 1), ("a", cap + 5)],
        [("a", 1), (" ", 1), ("a", cap)],
        [("a", 10), ("\n", 1), ("a", cap), ("\t", 1)],
        [("a", cap - 1), ("\t", 1), ("b", 3)],
        [("a", cap), (" ", 1)],
        [("a", 100), ("\r", 1), ("\u00a0", 1), ("\u2028", 1), ("a", cap)],
        [("a", 5), (" ", 1), ("a", 5), ("\n", 1), ("a", cap)],
        [("ab ", 20000)],
        [("\ud800", cap + 1)],
    ]
    for runs in shapes:
        body = "".join(text * times for text, times in runs)
        got = vc._bounded_scan_text(body)
        assert body.startswith(got)
        yield {
            "f": "bounded_scan_text",
            "runs": [[_t(t), n] for t, n in runs],
            "r": len(got),
        }


def _lower_ranges() -> list[list[int]]:
    out: list[list[int]] = []
    start = None
    for cp in range(0x110001):
        low = cp < 0x110000 and chr(cp).islower()
        if low and start is None:
            start = cp
        elif not low and start is not None:
            out.append([start, cp - 1])
            start = None
    return out


def _header() -> dict[str, Any]:
    return {
        "constants": {
            "MAX_PATHS_PER_BODY": vc._MAX_PATHS_PER_BODY,
            "MAX_PATH_LENGTH": vc._MAX_PATH_LENGTH,
            "MAX_BODY_SCAN_BYTES": vc._MAX_BODY_SCAN_BYTES,
            "MAX_ANCHOR_CITATIONS": vc._MAX_ANCHOR_CITATIONS,
            "MAX_ANCHORED_CITATION_STATS": vc._MAX_ANCHORED_CITATION_STATS,
            "BODY_PARSE_ENTRIES": vc.BODY_PARSE_ENTRIES,
            "TRAILING_PUNCT": vc._TRAILING_PUNCT,
            "WELLKNOWN_ROUTE_SEGMENTS": sorted(vc._WELLKNOWN_ROUTE_SEGMENTS),
            "PLACEHOLDER_PATHS": sorted(vc._PLACEHOLDER_PATHS),
            "PLACEHOLDER_PREFIXES": list(vc._PLACEHOLDER_PREFIXES),
            "CHECKABLE_CITATION_EXTENSIONS": sorted(vc._CHECKABLE_CITATION_EXTENSIONS),
        }
    }


def _ascii(row: dict[str, Any]) -> bool:
    texts = [*row.get("a", []), row.get("alphabet", "")]
    texts += [t for t, _ in row.get("runs", [])]
    return all(isinstance(t, str) and t.isascii() for t in texts if t is not None)


def _split() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = [
        *_rows(PATHS),
        *_rows(list(_length_rows())),
        *_fold_rows(),
        *_bounded_rows(),
        _exhaustive("normalize_candidate", "/~aA.: 1\\@)(-$", 3, None),
        _exhaustive("command_shaped", "/a -~C:\t", 4, False),
        _exhaustive("line_suffix", "a/\\:1-\n", 6, None),
        _exhaustive("trim_candidate", " \\)(./a$", 3, "self"),
        _exhaustive("escapes_root", "/\\.aC:", 4, False),
        _exhaustive("anchored_candidate", "/~aC:.\\", 3, None),
        _exhaustive("is_single_segment_routelike", "/a.A", 4, False),
    ]
    words = [f"/a/b/c {w}{x}" for w in UNICODE_WORDS for x in ("", "x", "X", "1")]
    rows += _rows(words + UNICODE)
    return [r for r in rows if _ascii(r)], [r for r in rows if not _ascii(r)]


def generate() -> dict[str, bytes]:
    plain, _ = _split()
    return {"verify_candidates.jsonl": jsonl([_header(), *plain])}


def generate_unicode() -> dict[str, bytes]:
    _, wide = _split()
    wide.append({"f": "lower", "r": _lower_ranges()})
    header = {"unicode": unicodedata.unidata_version}
    return {"verify_candidates_unicode.jsonl": jsonl([header, *wide])}
