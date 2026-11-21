"""Fixtures: one path candidate validated (``verify_candidates.jsonl``,
``verify_candidates_unicode.jsonl``), and the extraction patterns under
``finditer`` (``verify_candidates_patterns.jsonl``,
``verify_candidates_patterns_unicode.jsonl``, described at
``PATTERNS``).

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

import functools
import hashlib
import itertools
import re
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


# Unit 2: the three extraction patterns under ``finditer``
# (``verify_candidates_patterns.jsonl``,
# ``verify_candidates_patterns_unicode.jsonl``). A line is one of:
#
# - ``{"p", "t", "m"}``: ``finditer`` of pattern ``p`` (``backtick``,
#   ``bare``, ``domain``) over the text ``t`` answers the matches ``m``,
#   each ``[start, end, start(1), end(1)]``;
# - ``{"p", "frags", "depth", "count", "sha256"}``: every text joined
#   from zero to ``depth`` fragments (``itertools.product``'s order), its
#   answer spelled ``s,e,gs,ge`` per match joined by ``;`` with a newline
#   after each text, and the SHA-256 of those lines in order.
#
# ``re`` answers these three patterns alike under 3.11 to 3.14 and on
# every platform: no row holds a path, an error text or a repr. A text or
# fragment holding a code point past Latin-1 other than the four
# typographic quotes (where `\w` and `\s` follow the Unicode data) goes
# in ``_unicode``, gated on the Unicode data; the rest are ungated.

PATTERNS: dict[str, re.Pattern[str]] = {
    "backtick": vc._BACKTICK_RE,
    "bare": vc._BARE_RE,
    "domain": vc._DOMAIN_ROUTE_RE,
}

_QUOTES = "\u201c\u201d\u2018\u2019"

PATTERN_TEXTS: dict[str, list[str]] = {
    "backtick": [
        "",
        "`",
        "``",
        "`a`",
        "`a`b`c`",
        "`a``b`",
        "x `a\nb` y `c`",
        "`unclosed",
        "`a\n`b`",
        "`a` `b` `c`",
        "``a``",
        "`\u00e9\t\r`",
        "`\u3042`",
    ],
    "bare": [
        "/a",
        " /a",
        "x/a",
        "/",
        "x /a /b",
        "/a /b",
        "/a/b /c",
        "~/a",
        "$HOME/x",
        "${HOME}/x",
        "$HOME",
        "~x",
        "C:\\x",
        "c:/x",
        "C:x",
        "\u00e9:/x",
        "\u0141:/x",
        "\u0161:/x",
        "/a [b/c",
        "/a @b/c",
        "/a Zb/c",
        "\u2019/a",
        "=/a|/b(/c[/d{/e</f\"/g'/h,/i;/j",
        "\u201c/a\u201d \u2018/b\u2019",
        "\t/a\n/b\r/c\x0b/d\x0c/e\x1c/f\x85/g\xa0/h",
        "~/Library/Application Support/x",
        "~/Library/Application Support",
        "~/a Bb Cc/d Ee",
        "~/a Bb Cc/d Ee/f",
        "/a B/c",
        "/a Bc d/e",
        "/a bc/d",
        "/a Bc/ x",
        "C:\\Program Files\\x y",
        "/a@1+2%3~4_5.6-7\\8",
        "/a Bc/d Ee/ Ff",
        "x=/a",
        "/a\u3000/b",
        "/\u3042 \u0391\u03b2/c",
    ],
    "domain": [
        "pypi.org/pypi/x/json",
        "a.b/c",
        "a/b",
        ".a.b/c",
        "x.a.b/c",
        "-a.b/c",
        "a-b.c/d.e-f/g",
        "x a.b/c",
        "a.b./c",
        "a..b.c/d",
        "see...example.com/x",
        "a.b/",
        "a.b/.",
        "a.b/c a.b/d",
        "1.2/3",
        "_.x/y",
        "\u00e9.f/g",
        "a.b/c/d.e/f",
        "a.b/c.d/e",
        "\u3042.b/c",
    ],
}

PATTERN_SWEEPS: list[tuple[str, list[str], int]] = [
    ("backtick", ["`", "a", "\n", " "], 8),
    ("domain", ["a", "-", ".", "/", "\u00e9", "_", " ", "b.c", "x/y", "..", "1"], 5),
    (
        "bare",
        [
            "/",
            "~/",
            "$HOME/",
            "${HOME}/",
            "C:\\",
            "c:/",
            "a",
            ".",
            " ",
            " A",
            " Ab",
            "b/",
            "\\",
            "=",
            "\u201c",
            "\n",
            "\u00e9",
            "$",
            "x y",
        ],
        4,
    ),
    ("bare", ["/", " ", "Ab", "a", "/b", " A", "\\", "~/"], 6),
    ("backtick", ["`", "\u3042", "\u2028", "a"], 6),
    ("domain", ["\u3042", ".", "/", "-", "\u3000", "a"], 6),
    ("bare", ["/", "\u3000", "\u3042", " \u0391b", "\u2028", "A"], 5),
]


def _wide(text: str) -> bool:
    return any(ord(c) > 0xFF and c not in _QUOTES for c in text)


def _spans(name: str, text: str) -> list[list[int]]:
    return [[*m.span(), *m.span(1)] for m in PATTERNS[name].finditer(text)]


def _sweep(name: str, frags: list[str], depth: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for k in range(depth + 1):
        for combo in itertools.product(frags, repeat=k):
            spans = _spans(name, "".join(combo))
            digest.update(
                (";".join(",".join(map(str, s)) for s in spans) + "\n").encode()
            )
            count += 1
    return {
        "p": name,
        "frags": [_t(f) for f in frags],
        "depth": depth,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def _pattern_rows(wide: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"p": name, "t": _t(text), "m": _spans(name, text)}
        for name, texts in PATTERN_TEXTS.items()
        for text in texts
        if _wide(text) == wide
    ]
    rows += [
        _sweep(name, frags, depth)
        for name, frags, depth in PATTERN_SWEEPS
        if any(_wide(f) for f in frags) == wide
    ]
    return rows


def generate_patterns() -> dict[str, bytes]:
    rows = _pattern_rows(wide=False)
    return {"verify_candidates_patterns.jsonl": jsonl([{"cases": len(rows)}, *rows])}


def generate_patterns_unicode() -> dict[str, bytes]:
    rows = _pattern_rows(wide=True)
    header = {"cases": len(rows), "unicode": unicodedata.unidata_version}
    return {"verify_candidates_patterns_unicode.jsonl": jsonl([header, *rows])}


# Unit 3: `_normalize_for_compare` and `_scan_candidates`
# (``verify_candidates_scan.jsonl``, ``verify_candidates_scan_unicode.jsonl``),
# POSIX only: the reference reads `Path`, whose Windows flavour spells
# other texts. The home directory is recorded, never read: ``$HOME`` is set
# to the row's ``home`` (or removed when it is null, with the password
# entry of the running user unknown), and ``pwd.getpwnam`` answers from
# ``USERS`` in the header (an unknown name raises `KeyError`; a name the
# real ``getpwnam`` refuses, one holding NUL or one ``os.fsencode`` cannot
# encode, raises `ValueError` as it does). A line is
# one of:
#
# - ``{"f": "nfc", "a", "home", "r"}``: `_normalize_for_compare(a)`;
# - ``{"f": "scan", "a", "home", "r"}``: `_scan_candidates(a)`, each
#   candidate ``[path, drop_if_missing, bare_spaced]``;
# - ``{"f": "scan", "runs", "home", "r"}``: the body is the runs
#   (``[text, times]``) joined;
# - ``{"f": "scan", "frags", "depth", "home", "count", "sha256"}``: every
#   body joined from up to ``depth`` fragments in `itertools.product`'s
#   order, each answer spelled ``path|AS`` (``A`` and ``S`` the flags as
#   0 or 1) joined by newlines, or ``!`` and the class raised, then a
#   newline, encoded UTF-8 with surrogates passed.
#
# A raise is ``{"raise": class name}``.

HOME = "/hd/u"
USERS = {
    "alice": "/hd/alice",
    "root": "/",
    "empty": "",
    "slashy": "//srv//x/",
    "tilde": "~elsewhere",
    "rel": "rel/./dir/",
    "triple": "///t",
}


class _FakePwd:
    @staticmethod
    def getpwnam(name: str) -> Any:
        import os

        if "\x00" in name:
            raise ValueError("embedded null character")
        os.fsencode(name)
        if name not in USERS:
            raise KeyError(name)
        return type("pw", (), {"pw_dir": USERS[name]})()

    @staticmethod
    def getpwuid(uid: int) -> Any:
        raise KeyError(uid)


def _with_home(home: str | None, fn: Callable[[], Any]) -> Any:
    import os
    import sys
    from unittest.mock import patch

    env = dict(os.environ)
    env.pop("HOME", None)
    if home is not None:
        env["HOME"] = home
    with (
        patch.dict(os.environ, env, clear=True),
        patch.dict(sys.modules, {"pwd": _FakePwd}),
    ):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the class is the answer
            return {"raise": type(exc).__name__}


def _scan(body: str, home: str | None) -> Any:
    got = _with_home(home, lambda: vc._scan_candidates(body))
    if isinstance(got, dict):
        return got
    return [[_t(p), a, s] for p, a, s in got]


NFC_TEXTS = [
    "",
    "/a/b",
    "//a//b/",
    "///a/./b/.",
    "a/b/",
    ".",
    "./",
    "./~x/y",
    "~",
    "~/",
    "~/a//b/./c",
    "~/../x",
    "~alice",
    "~alice/p",
    "~root/p",
    "~empty/p",
    "~slashy/p",
    "~tilde/p",
    "~rel/p",
    "~triple/p",
    "~nobody/p",
    "~~/p",
    "/~/p",
    "a\\b",
    "~\\x",
    "~a\x00b/x",
    "./~a\x00b//x",
    "~\ud800/x",
    "~\udc7f/x",
    "~\udc80/x",
]
NFC_HOMES: list[str | None] = [HOME, "/", "", "//h/", "~h", "rel", None]

SCAN_TEXTS = [
    "",
    "see /etc/hosts here",
    "`/etc/hosts` and /etc/hosts",
    "/etc/hosts and `/etc/hosts`",
    "`  /a/b/c  `",
    "`~/x/y` then /hd/u/x/y",
    "/hd/u/x/y then ~/x/y",
    "`/a/b`c/d/e`",
    "/opt/app Config here",
    "/opt/app config here",
    "/opt/app. The end",
    "/opt/app Config then /opt/app ok",
    "/opt/app ok then /opt/app Config",
    "/opt/app (2).pdf",
    "/opt/app 1x",
    "/opt/app X",
    "/opt/app  X",
    "/etc/hosts TCP/IP keepalive",
    "https://ex.com/api/v1 and /api/v1/x and /apix/y",
    "`ex.com/api` /api/users",
    "x.y.com/srv /srv/data/z",
    "a=/x/y,b=/x/z",
    "`/a/b:12` /a/b:13",
    "`\\ \\ .` /a/b",
    "/a/b `\\ \\ .`",
    "/a/b /a/c /a/d /a/e /a/f /a/g /a/h /a/i /a/j",
    "`/a/b` `/a/c` `/a/d` `/a/e` `/a/f` `/a/g` `/a/h` `/a/i` `\\ \\ .`",
    "`/a/b` `/a/c` `/a/d` `/a/e` `/a/f` `/a/g` `/a/h` /a/i /a/j",
    "/a/b `/a/b` /a/b",
    "~/x",
    "`~/x`",
    "C:\\dir\\file.txt and C:/dir/f",
    "/a/b\n/c/d\t/e/f",
    "/a/b/ /a/b",
    "/a//b",
    "/a/./b and /a/b",
    "/path/to/thing /etc/foo",
    "/a/b \u00e9tat /a/c \u00c9tat",
    "/a/b \u0391x /a/c \u03b1x",
    "\u201c/a/b\u201d and \u2018/c/d\u2019",
    "/a/b\u3000Xy",
    "/a/b \u02b0x",
    "`/\u00e9/b` /\u00e9/b",
]
SCAN_HOMES: list[str | None] = [HOME, None]
SCAN_RUNS: list[tuple[list[list[Any]], str | None]] = [
    ([["/a/b ", 6600]], HOME),
    ([["x", 32760], [" /a/bc", 2]], HOME),
    ([["`/q/r` ", 4000], ["/q/s", 1]], HOME),
]
SCAN_SWEEPS: list[tuple[list[str], int, str | None]] = [
    (["`", "/a", " ", "Ab", "~/x", "x.co/a", ".", "\n", "b/"], 4, HOME),
    (["`", "/a", " ", "~/", "X", "/hd/u"], 5, None),
]


def _spell(got: Any) -> bytes:
    if isinstance(got, dict):
        text = "!" + got["raise"]
    else:
        text = "\n".join(f"{p}|{int(a)}{int(s)}" for p, a, s in got)
    return (text + "\n").encode("utf-8", "surrogatepass")


def _scan_sweep(frags: list[str], depth: int, home: str | None) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0

    def run() -> None:
        nonlocal count
        for k in range(depth + 1):
            for combo in itertools.product(frags, repeat=k):
                try:
                    got: Any = vc._scan_candidates("".join(combo))
                except Exception as exc:  # noqa: BLE001 - the class is the answer
                    got = {"raise": type(exc).__name__}
                digest.update(_spell(got))
                count += 1

    _with_home(home, run)
    return {
        "f": "scan",
        "frags": [_t(f) for f in frags],
        "depth": depth,
        "home": home,
        "count": count,
        "sha256": digest.hexdigest(),
    }


def _nfc(text: str) -> Any:
    return _t(vc._normalize_for_compare(text))


def _scan_rows(wide: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for home in NFC_HOMES:
        for text in NFC_TEXTS:
            if _wide_text(text) == wide:
                got = _with_home(home, functools.partial(_nfc, text))
                rows.append({"f": "nfc", "a": _t(text), "home": home, "r": got})
    for home in SCAN_HOMES:
        for text in SCAN_TEXTS:
            if _wide_text(text) == wide:
                rows.append(
                    {"f": "scan", "a": _t(text), "home": home, "r": _scan(text, home)}
                )
    if not wide:
        for runs, home in SCAN_RUNS:
            body = "".join(t * n for t, n in runs)
            rows.append(
                {"f": "scan", "runs": runs, "home": home, "r": _scan(body, home)}
            )
        rows += [_scan_sweep(f, d, h) for f, d, h in SCAN_SWEEPS]
    return rows


def _wide_text(text: str) -> bool:
    return not text.isascii()


def generate_scan() -> dict[str, bytes]:
    rows = _scan_rows(wide=False)
    header = {"cases": len(rows), "home": HOME, "users": USERS}
    return {"verify_candidates_scan.jsonl": jsonl([header, *rows])}


def generate_scan_unicode() -> dict[str, bytes]:
    rows = _scan_rows(wide=True)
    header = {
        "cases": len(rows),
        "home": HOME,
        "users": USERS,
        "unicode": unicodedata.unidata_version,
    }
    return {"verify_candidates_scan_unicode.jsonl": jsonl([header, *rows])}
