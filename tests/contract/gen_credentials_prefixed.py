"""Fixtures: the vendor-prefixed credential detectors
(`credentials._PREFIXED_DETECTORS`, `_is_masked_token`, `_is_spent_run`).

Two files, one JSON object per line, keys sorted, ASCII:

- ``credentials_prefixed.jsonl``, not gated. Its first line pins the
  detectors (``{"k": "detectors", "kinds": [...], "patterns": [...]}``, in
  the reference's order). Every other line is ``{"t": text, "o": [[d,
  start, end, flag], ...]}``: every match of every detector's
  ``finditer`` over the text, detectors in order, ``d`` the detector's
  index, offsets in code points, ``flag`` ``"h"`` (a hit), ``"m"`` (a
  masked token) or ``"s"`` (a spent run). A text holding a surrogate is
  ``t_cps``; a long text is ``{"r": [[piece, count], ...]}`` under ``t``
  (the pieces repeated, joined). A text or piece shaped like a key (a
  long token run, or one a detector fires on) is written under ``r`` as
  the list of its parts, at most twelve characters each, so that no line
  of the file, and no line of this source (`_s`), holds a whole key
  shape: a scanner that reads the repository takes one for a leaked key
  (GitHub's push protection refused the push that held them). Every
  character outside ASCII is one
  whose `re` word class is the same in every Unicode version from 6.3 on
  (Latin-1, the surrogates, a few fixed CJK, space and mark characters),
  so these bytes depend on no Unicode table and no Python release.
- ``credentials_prefixed_unicode.jsonl``, gated on the Unicode version:
  every code point from U+0000 to U+10FFFF, in blocks of 0x4000, placed
  before an AWS key, after an AWS key, and after a Slack token whose last
  hyphen leaves a boundary to fall back to; per block the SHA-256 of the
  lines ``hex(cp) + ":" + the matches`` (lowercase hex, no ``0x``) (as above, ``d,start,end,flag`` joined by
  ``;``, the three templates joined by ``|``). These bytes are `re`'s
  ``\\w`` of the running Python's Unicode data.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from bettermemory import credentials as cr

from ._common import jsonl

_DETECTORS = cr._PREFIXED_DETECTORS


def _matches(text: str, only: tuple[int, ...] | None = None) -> list[list[Any]]:
    out: list[list[Any]] = []
    for d, (_kind, regex) in enumerate(_DETECTORS):
        if only is not None and d not in only:
            continue
        for m in regex.finditer(text):
            if cr._is_spent_run(m):
                flag = "s"
            elif cr._is_masked_token(m):
                flag = "m"
            else:
                flag = "h"
            out.append([d, m.start(), m.end(), flag])
    return out


#: A run of token characters this long is written in pieces, in this
#: source and in the golden file: a text shaped like a vendor's key is
#: what the detectors are for, and a scanner reading the repository
#: (GitHub's push protection among them) takes a whole one for a leaked
#: key and refuses the push.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-.+/=]{16,}")
_PIECE = 12


def _s(*pieces: str) -> str:
    """A text spelled in pieces, so that no line of this file holds a
    whole key-shaped run."""
    return "".join(pieces)


_AWS = _s("AKIAIOSFODNN", "7EXAMPL0")
_SK = _s("sk-ABCDEFGHI", "J0123456789x", "yz")
_SLACK = _s("xoxb-1234567", "890-abcdefgh", "ij")
_JWT = _s("eyJhbGciOiJI", ".eyJzdWIiOiI", "x.SflKxwRJSM", "eKKF2QT4")
_PEM = _s("-----BEGIN R", "SA PRIVATE K", "EY-----\nMIIE", "owIBAAKCAQEA", "x4f5")
_B36 = _s("aB3dE5fG7hJ9", "kL1mN3pQ5rS7", "tU9vW1xY3zA5")

_FRAGMENTS = [
    _AWS, _s('ASIAZZZZ1234', '567890AB'), _s('AKIAXXXXXXXX', 'XXXXXXXX'), "AKIA0123",
    _SK, _s('sk-ant-ABCDE', 'FGH12345678a', 'bcdef'), _s('sk-proj-Abc-', 'Def_123-4567', '890abcd'),
    _s('sk-image-pro', 'cessing-tool', 'kit-v3'), "sk-", "sk-sk-sk-",
    _s('sk_live_4eC3', '9HqLyjWDarjt', 'T1zdp7dc'), _s('rk_test_abcd', 'efghijklmnop', 'qrstuvw'),
    "ghp_" + _B36, "ghs_" + "x" * 36, "gho_" + _B36[:35],
    _s('github_pat_1', '1ABCDEFG0123', '456789_abcde', 'fgh'), "github_pat_aaaa",
    _SLACK, _s('xoxb-your-bo', 't-token'), "xoxc-", _s('xapp-1-A0123', '-4567-abc'),
    "xapp-", _s('AIzaSyD-1234', '567890abcdef', 'ghijklmnopqr', 'stu'),
    _s('SG.abcdefghi', 'jklmnopqrstu', 'v.ABCDEFGHIJ', 'KLMNOPQRSTUV', 'WXYZabcdefgh', 'ijk0123456'),
    _s('hvs.CAESIJlR', 'HM-abcdefghi', 'jklmnop'), "hvb.short", _JWT, "eyJ", "eyJabc.def",
    _PEM, _s('----', '-BEG', 'IN P', 'RIVA', 'TE K', 'EY--', '--- ', '\r\nPr', 'oc-T', 'ype:', ' 4'), "-----BEGIN EC PRIVATE KEY-----",
    " ", "_", "-", ".", "\n", "\t", "x", "9", "Z", "é", " ", "一",
    "́", "\ud800", "/", "=", "+", "--", "__",
]  # fmt: skip


def _hand() -> list[str]:
    cases = [
        "",
        "plain prose with api_key and password mentioned",
        _AWS,
        "x" + _AWS,
        "_" + _AWS,
        "é" + _AWS,
        _AWS + "é",
        _AWS + "_",
        _AWS + "-",
        _AWS + "Q",
        "AKIA" + "ABCDEFGHIJKLMNO",
        "ASIA" + _s("ABCDEFGHIJKL", "MNOPQ"),
        "AKIA" + _s("AAAABBBBCCCC", "DDDD"),
        "AKIA" + _s("ABCDABCDABCD", "ABCD"),
        "AKIA" + _s("ABCDEABCDEAB", "CDEA"),
        _s("AKIAabcdefgh", "ijklmnop"),
        _s("AKIAAKIAIOSF", "ODNN7EXAMPLE"),
        _SK,
        "sk-" + "A1" + "b" * 17,
        "sk-" + "A1" + "b" * 18,
        "sk-" + "a" * 30,
        "sk-" + "A" * 30,
        "sk-" + "1" * 30,
        "sk-ant-" + "A1" + "b" * 18,
        "sk-ant-" + "A1" + "b" * 14,
        "sk-ant-" + "a" * 18 + "A1",
        "sk-proj-" + "A1" + "b" * 18,
        "sk-proj-" + "A1" + "b" * 12,
        _s("sk-ABCDEFGHI", "J0123456789-"),
        _s("sk-ABCDEFGHI", "J0123456789-", "é"),
        _s("sk-ABCDEFGHI", "J0123456789é"),
        _s("sk-ABCDEFGHI", "J0123456789x", "-y-é"),
        _s("sk-ABCDEFGHI", "J012345678-é"),
        _s("sk-ABC-DEF-G", "HI-J01-234-5", "67-89éz"),
        _s("xsk-ABCDEFGH", "IJ0123456789"),
        _s("-sk-ABCDEFGH", "IJ0123456789"),
        "sk-" * 20,
        _s("sk-sk-ABCDEF", "GHIJ01234567", "89"),
        "sk-sk-sk-" + _SK,
        "sk_live_" + "a" * 23,
        "sk_live_" + "a" * 24,
        "sk_live_" + "a" * 24 + "_",
        "sk_live_" + "a" * 24 + "é",
        "sk_live_" + "a" * 24 + "-",
        "rk_test_" + "a" * 40,
        "sk_prod_" + "a" * 30,
        "ghp_" + _B36,
        "ghp_" + _B36 + "x",
        "ghp_" + _B36 + "-x",
        "ghx_" + _B36,
        "ghu_" + "ab12" * 9,
        "ghr_" + "abcde" * 7 + "a",
        "github_pat_" + "a1B2" * 5 + "_x",
        "github_pat_" + "aB3" * 7,
        "github_pat_" + "aB3" * 7 + "a",
        "github_pat_" + "aaaa_" * 6,
        "github_pat_" + "abcde" * 5 + "-",
        "github_pat_" + "abcde" * 5 + "é",
        _SLACK,
        "xoxb-123456789",
        _s("xoxb", "-123", "4567", "890"),
        "xoxb-abcdefghij",
        "xoxd-1234567890",
        _s("xoxa-1-2-3-4", "-5-6-7"),
        "xoxe-1234567890é",
        _s("xoxe-1234567", "890-é"),
        _s("xoxs-1-xoxs-", "1-xoxs-1-xox", "s-1"),
        "xoxr-" * 10,
        _s("xapp-1-A0123", "-4567-abc"),
        _s("xapp-abcdefg", "hijklm"),
        _s("xapp-1234567", "890_"),
        "AIza" + "B" * 34 + "1",
        "AIza" + "B" * 35,
        "AIza" + "B" * 36,
        "AIza" + "B" * 34 + "-",
        "AIza" + "B" * 34 + "-x",
        "SG." + "a" * 22 + "." + "b" * 43,
        "SG." + "a" * 21 + "." + "b" * 43,
        "SG." + "a" * 22 + "." + "b" * 42,
        "SG." + "a" * 22 + "." + "b" * 44,
        "SG." + "a" * 22 + "." + "b" * 42 + "-",
        "hvs." + "a" * 23,
        "hvs." + "a" * 24,
        "hvr." + "a" * 23 + "-",
        "hvb." + "a" * 30 + "-é",
        "hvx." + "a" * 30,
        _JWT,
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8,
        "eyJ" + "a" * 7 + "." + "b" * 8 + "." + "c" * 8,
        "eyJ" + "a" * 8 + "." + "b" * 7 + "." + "c" * 8,
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 7,
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8 + "-",
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8 + "é",
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 7 + "-dé",
        "eyJ" + "a" * 8 + "." + "b" * 8 + ".." + "c" * 8,
        "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8 + "." + "d" * 9,
        "eyJeyJeyJeyJ",
        "eyJ" + "a" * 8 + ".eyJ" + "b" * 8 + "." + "c" * 8,
        _PEM,
        "-----BEGIN PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN PRIVATE KEY-----\n" + "A" * 15,
        "-----BEGIN PRIVATE KEY-----\n" + "A" * 15 + "!",
        "-----BEGIN PRIVATE KEY-----\r\n" + "A" * 16,
        "-----BEGIN PRIVATE KEY-----\r" + "A" * 16,
        "-----BEGIN PRIVATE KEY----- \t \n" + "+/=_-" * 4,
        "-----BEGIN PRIVATE KEY-----\n\n" + "A" * 16,
        _s(
            "----",
            "-BEG",
            "IN P",
            "RIVA",
            "TE K",
            "EY--",
            "---\n",
            "Proc",
            "-Typ",
            "e: 4",
            ",ENC",
            "RYPT",
            "ED",
        ),
        _s(
            "----", "-BEG", "IN P", "RIVA", "TE K", "EY--", "---\n", "DEK-", "Info", ":"
        ),
        "-----BEGIN PRIVATE KEY-----\nDEK-Info",
        "-----BEGIN PRIVATE KEY-----\nproc-type:",
        "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN EC PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN DSA PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN PGP PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN RSA  PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN PUBLIC KEY-----\n" + "A" * 16,
        "-----BEGIN RSA PRIVATE KEY-----",
        "files begin '-----BEGIN RSA PRIVATE KEY-----' then key",
        "x------BEGIN PRIVATE KEY-----\n" + "A" * 16,
        "-----BEGIN PRIVATE KEY----- \n" + "A" * 16,
        "-----BEGIN PRIVATE KEY-----\n" + "A" * 15 + "é",
        "api_key = " + _AWS + " and " + _SK + " and " + _SLACK,
        "\ud800" + _AWS + "\udfff",
        _AWS + "\n" + _AWS + "\n" + "AKIA" + "X" * 16,
    ]
    return cases


def _mixed(count: int) -> list[str]:
    """Texts joined from fragments, the choice read from SHA-256 of the
    case number (a fixed enumeration, the same on every run)."""
    out = []
    n = len(_FRAGMENTS)
    for i in range(count):
        digest = hashlib.sha256(f"credentials-prefixed {i}".encode()).digest()
        parts = 2 + digest[0] % 5
        out.append("".join(_FRAGMENTS[digest[1 + j] % n] for j in range(parts)))
    return out


def _long() -> list[list[list[Any]]]:
    return [
        [["sk-", 3000]],
        [["xoxb-", 2000]],
        [["xapp-", 2000]],
        [["eyJ", 3000]],
        [["sk-", 2000], [_SK, 1]],
        [["a", 5000], [_AWS, 1]],
        [[" " + _AWS, 50]],
        [["-", 4000], ["hvs.", 1], ["a-", 2000]],
        [["github_pat_", 300], ["a", 30]],
        [["eyJaaaaaaaa.", 500]],
        [["-----BEGIN PRIVATE KEY-----", 300], ["\n" + "A" * 16, 1]],
        [["sk-A1", 1], ["-", 3000], ["é", 1]],
    ]


def _pieces(text: str) -> list[str]:
    """`text` cut into pieces no detector fires on: twelve characters
    each, or four where a short shape (a Slack token) fits in twelve."""
    parts: list[str] = []
    for size in (_PIECE, 4):
        parts = [text[i : i + size] for i in range(0, len(text), size)]
        if not any(_matches(part) for part in parts):
            break
    return parts


def _written(recipe: list[list[Any]]) -> list[list[Any]]:
    """A recipe as the golden file holds it: a piece that is a key shape
    by itself is written as the list of its parts (`_pieces`), which the
    reader joins before it repeats the piece."""
    out: list[list[Any]] = []
    for piece, count in recipe:
        if _TOKEN_RUN.search(piece) or _matches(piece):
            out.append([_pieces(piece), count])
        else:
            out.append([piece, count])
    return out


def _case(text: str) -> dict[str, Any]:
    """One case. A text with a lone surrogate is written as code points;
    one with a long token run, or one a detector fires on, as a recipe
    of one piece written in parts (`_written`), which the reader joins."""
    case: dict[str, Any] = {"o": _matches(text)}
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        case["t_cps"] = [ord(c) for c in text]
    elif _TOKEN_RUN.search(text) or case["o"]:
        case["t"] = {"r": [[_pieces(text), 1]]}
    else:
        case["t"] = text
    return case


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "k": "detectors",
            "kinds": [kind for kind, _ in _DETECTORS],
            "patterns": [regex.pattern for _, regex in _DETECTORS],
            _s("min_body_div", "ersity"): cr._MIN_BODY_DIVERSITY,
        }
    ]
    for text in _hand() + _mixed(900):
        records.append(_case(text))
    for recipe in _long():
        text = "".join(piece * count for piece, count in recipe)
        records.append({"t": {"r": _written(recipe)}, "o": _matches(text)})
    return {_s("credentials_", "prefixed.jso", "nl"): jsonl(records)}


def _spell(found: list[list[Any]]) -> str:
    return ";".join(f"{d},{s},{e},{f}" for d, s, e, f in found)


#: The three templates of the Unicode sweep: (prefix, suffix, detectors).
UNICODE_TEMPLATES: list[tuple[str, str, tuple[int, ...]]] = [
    ("", _AWS, (0,)),
    (_AWS, "", (0,)),
    (_s("xoxb-1234567", "890-ab"), "", (5,)),
]


def generate_unicode() -> dict[str, bytes]:
    records: list[Any] = [{"k": "unicode", "block": 0x4000}]
    for lo in range(0, 0x110000, 0x4000):
        h = hashlib.sha256()
        for cp in range(lo, lo + 0x4000):
            c = chr(cp)
            parts = []
            for i, (before, after, only) in enumerate(UNICODE_TEMPLATES):
                text = c + after if i == 0 else before + c
                parts.append(_spell(_matches(text, only)))
            h.update(f"{cp:x}:{'|'.join(parts)}\n".encode())
        records.append({"lo": lo, "sha256": h.hexdigest()})
    return {_s("credentials_", "prefixed_uni", "code.jsonl"): jsonl(records)}
