"""Fixtures: the generic keyword rule, the connection-URI password and the
spans, redactions, snippets and markers of `credentials.py`
(`_GENERIC_KEYWORD_RE`, `_URI_USERINFO_RE`, `_merged_secret_spans`,
`secret_spans`, `_redact_all`, `_safe_snippet`, `find_credential_markers`).

Two files, one JSON object per line, keys sorted, ASCII:

- ``credentials_spans.jsonl``, not gated. Its first line pins the two
  pattern texts and their flags and ``_CONTEXT_CHARS``. A case is a text
  (``t``, or ``t_cps`` when it holds a surrogate; a long text is
  ``{"r": [[piece, count], ...]}`` under ``t``) with ``g`` and ``u`` (the
  ``value`` span of every match of the generic and the URI pattern, before
  the value gate), ``s`` (`secret_spans`: ``[start, end, kind]``), ``r``
  (`_redact_all`, through ``_common.output``) and ``m``
  (`find_credential_markers`: ``[kind, snippet]``, each snippet through
  ``_common.output``). A snippet case ``{"k": "snip"}`` asks
  `_safe_snippet(t, start, end, kind)` directly (``a``: the three
  arguments; ``o``: the answer).

  A sweep (``{"k": "sweep"}``) is a digest of ``count`` texts in place of
  their rows. Text ``i`` joins pieces chosen by the bytes of
  ``SHA-256("credentials-spans <name> <i> <block>")`` for block 0, 1, ...
  (the digests joined): the first byte chooses the count of pieces from
  ``counts``, each next byte a piece from ``pieces`` (``b % len``; each
  piece written as the hex of its UTF-8 bytes). The
  ``sha256`` is of the lines ``_line(text)`` in order (see `_line`).

  Every text is ASCII or holds only characters whose space, word and case
  properties are the same in every Unicode version since 6.3, so these
  bytes depend on no Unicode table and no Python release.
- ``credentials_spans_unicode.jsonl``, gated on the Unicode version: each
  template is a list of pieces (each the hex of its UTF-8) joined by one
  code point; a line is a block
  of code points ``lo`` to ``hi`` (exclusive) with the SHA-256 of
  ``hex(cp) + ":" + _line(text) + "\\n"`` per template and code point
  asked (see `asked`). Blocks run to U+40000: Unicode 15.1.0 assigns no
  space, word or cased character above it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from bettermemory import credentials as cr

from ._common import dumps_line, has_surrogate, jsonl, text_field

# Written in pieces, as every key shape here is: no line of a generator
# may itself be a text the detectors take for a credential.
_SECRET = "Xy7kQ9" + "pLm2Zt"
_AWS = "AKIA" + "0189ABCDEFGHIJKL"


def _value_spans(regex: re.Pattern[str], text: str) -> list[list[int]]:
    return [[m.start("value"), m.end("value")] for m in regex.finditer(text)]


def _line(text: str) -> str:
    """One case's answers as one line: the spans, the redacted text and
    the markers."""
    spans = ";".join(f"{s},{e},{k}" for s, e, k in cr.secret_spans(text))
    marks = ";".join(f"{m.kind}={m.snippet}" for m in cr.find_credential_markers(text))
    return f"S{spans}|R{cr._redact_all(text)}|M{marks}"


def _text_field(text: str) -> dict[str, Any]:
    """The text as ``t_hex`` (its UTF-8 bytes), or ``t_cps`` when it holds
    a surrogate; a long repeated text as ``{"r": [[hex, count]]}`` under
    ``t``. Hex keeps every line of the file clear of the detectors' own
    shapes, which `test_no_fixture_line_is_shaped_like_a_key` requires."""
    if len(text) > 400:
        for unit in range(1, 40):
            piece = text[:unit]
            if len(text) % unit == 0 and piece * (len(text) // unit) == text:
                return {"t": {"r": [[piece.encode().hex(), len(text) // unit]]}}
    if has_surrogate(text):
        return text_field("t", text)
    return {"t_hex": text.encode().hex()}


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` with ``t_hex`` written as plain ``t`` where the line stays
    clear of the detectors' shapes."""
    if "t_hex" not in row:
        return row
    text = bytes.fromhex(row["t_hex"]).decode()
    plain = {k: v for k, v in row.items() if k != "t_hex"} | {"t": text}
    return row if cr.find_credential_markers(dumps_line(plain)) else plain


def _case(text: str) -> dict[str, Any]:
    return {
        **_text_field(text),
        "g": _value_spans(cr._GENERIC_KEYWORD_RE, text),
        "u": _value_spans(cr._URI_USERINFO_RE, text),
        "s": [list(s) for s in cr.secret_spans(text)],
        **text_field("r", cr._redact_all(text)),
        "m": [
            [m.kind, text_field("o", m.snippet)]
            for m in cr.find_credential_markers(text)
        ],
    }


def _hand() -> list[str]:
    s = _SECRET
    keywords = [
        "password",
        "passwd",
        "secret",
        "secret_key",
        "secretkey",
        "secret_access_key",
        "secretaccesskey",
        "api_key",
        "api key",
        "apikey",
        "access_token",
        "access token",
        "auth_token",
        "client_secret",
        "client secret",
        "private_key",
        "private key",
        "bearer",
        "bearer_token",
        "PASSWORD",
        "BEARER-TOKEN",
        "pass_word",
        "passwords",
        "secret_key_base",
        "token",
        "apikeys",
    ]
    seps = [
        "=",
        ":",
        ":=",
        "=>",
        "==",
        " = ",
        ": ",
        " is ",
        " was ",
        " is set to ",
        " was changed to ",
        " is now ",
        " was rotated to ",
        " is updated ",
        " was reset now ",
        " to ",
        " is set ",
        "\n=\n",
        " => ",
        "=  ",
        " isn't ",
        " is  set  to ",
    ]
    out: list[str] = []
    for k in keywords:
        out.append(f"{k}={s}")
    for sep in seps:
        out.append(f"password{sep}{s}")
    out += [
        # Prefix segments: the bound of five and of thirty.
        f"db_password={s}",
        f"a_b_c_d_e_password={s}",
        f"a_b_c_d_e_f_password={s}",
        f"{'x' * 30}_password={s}",
        f"{'x' * 31}_password={s}",
        f"MY-APP_DB-password={s}",
        f"9_password={s}",
        f"x__password={s}",
        f"_password={s}",
        f"-password={s}",
        f"apassword={s}",
        f"1password={s}",
        f".password={s}",
        f"key.password={s}",
        # Wrappers and quotes.
        f'"password": "{s}"',
        f"'password': '{s}'",
        f"**Password**: {s}",
        f"`api_key` = `{s}`",
        f'password="{s}',
        f"password='{s}'tail",
        f'password = "{s}" next',
        f"password: {s}, user: bob",
        # Value bounds.
        "password=Ab1cdefg",
        "password=Ab1cdef",
        "password=" + "Ab1cdefghijk",
        "password=" + "Ab1cdefghijkl",
        f"password={s}{s}",
        "password=changeme123",
        "password=${DB_PASS}",
        "password=<your-password>",
        "password=hunter2hunter2",
        "password=xxxxxxxxxxxxxxxx",
        "password=2026-01-15T00:30:00Z",
        "password=/usr/local/bin/thing",
        "password=os.environ.get(KEY)",
        "password=SOME_ENV_VAR_NAME",
        # Authorization headers.
        f"Authorization: Bearer {s}",
        f"authorization:bearer {s}",
        f"curl -H 'Authorization: Bearer {s}' https://x.test",
        f"AUTHORIZATION : BEARER   {s}",
        f"Authorization: Bearer{s}",
        f"Authorization: Basic {s}",
        # Prose near-misses.
        "my password is a secret I keep to myself",
        "the password was changed yesterday afternoon",
        "the secret is out now",
        "the api key is rotated every month",
        f"my Mailgun api key is {s}",
        f"The password is set to {s} for now",
        f"passwordless login uses {s}",
        f"the secretary is {s}",
        f"bearer of bad news {s}",
        "api keys are stored in the vault",
        f"compassword={s}",
        # URI userinfo.
        f"postgres://user:{s}@db.internal:5432/app",
        f"DATABASE_URL=postgres://user:{s}@db/app",
        "postgres://user:password@host/db",
        "redis://:Ab1cdefghijkl@cache:6379",
        "redis://u:${REDIS_PASSWORD}@cache",
        f"amqp://guest:{s}@rabbit/",
        f"x://u:{s}@h",
        f"ab://u:{s}@h",
        f"9db://u:{s}@h",
        f"POSTGRESQL+psycopg://User:{s}@h",
        f"postgres://user:{s}/x@h",
        f"postgres://us/er:{s}@h",
        f"postgres://user:{s}",
        f"postgres://{'u' * 64}:{s}@h",
        f"postgres://{'u' * 65}:{s}@h",
        f"{'s' * 31}://u:{s}@h",
        f"{'s' * 32}://u:{s}@h",
        f"postgres://user:{s}@h postgres://admin:" + "Zq8wE3rT5yU1@h2",
        f"password=postgres://user:{s}@h",
        # Prefixed shapes with and beside the generic rule.
        "aws_secret_access_key = " + _AWS,
        "api_key = " + _AWS,
        _AWS,
        "token=sk-ant-" + "aB3" * 10,
        "api_key=sk-proj-" + "Zz9" * 9 + " and " + _AWS,
        "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3 then gho_" + "Q9w8E7r6T5" * 3 + "Q9w8E7",
        "xoxb-" + "1234567890-abcdefghij then xapp-" + "1-A0123456789-abcdef",
        "AIza" + "Sy" + "A1b2C3d4E5" * 3 + "f6g",
        "SG." + "a1B2c3D4e5" * 2 + "f6" + "." + "Q9w8E7r6T5" * 4 + "Q9w",
        "hvs." + "a1B2c3D4e5" * 3,
        "jwt eyJ" + "hbGciOiJIUzI1.eyJzdWIiOiIx.SflKxwRJSMeK",
        "AKIAXXXXXXXXXXXXXXXX and " + _AWS,
        "sk-" + "x" * 30,
        "sk-ant-" + "aB3" * 10 + " sk-" + "Q1w" * 10,
        # PEM and its empty trailing window.
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890abcdef\n-----END RSA PRIVATE KEY-----",
        "key:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n",
        "prefix text that is long enough here -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n",
        # Snippet windows: edges, whitespace runs, two secrets in one window.
        f"{'a' * 30} password={s} {'b' * 30}",
        f"password={s}",
        f"   password={s}   ",
        f"\n\n  password=\t{s}\n\n  more",
        f"x\u3000\u3000password={s}\u2028tail",
        f"a password={s} b api_key={_SECRET[::-1]} c",
        f"password={s} {_AWS}",
        f"line one\nline two\r\npassword: {s}\r\nline four",
        f"{'w ' * 20}password={s}{' z' * 20}",
        # Odd but valid text: a surrogate, a NUL, the empty text.
        "",
        "password=",
        f"password=\ud800{s}",
        f"pass\x00word={s}",
        f"password={s}\udfff",
        # Pathological for backtracking: dense separators, long prefixes.
        "_" * 500,
        "a_" * 300,
        "-a" * 300,
        "a_" * 300 + f"password={s}",
        "password" * 60,
        "password=" * 50,
        "Authorization: Bearer " * 20,
        "postgres://" * 40,
    ]
    return out


_SNIPS: list[tuple[str, int, int, str]] = [
    ("short", 0, 5, "generic-secret-assignment"),
    ("short", 0, 5, "private-key-pem"),
    ("x" * 60, 30, 32, "aws-access-key-id"),
    ("x" * 60, 30, 32, "private-key-pem"),
    ("x" * 60, 0, 60, "jwt"),
    ("x" * 60, 59, 60, "jwt"),
    (f"{'q' * 40} password={_SECRET} {'r' * 40}", 50, 62, "generic-secret-assignment"),
    (f"{'q' * 40} password={_SECRET} {'r' * 40}", 45, 55, "generic-secret-assignment"),
    (f"{'q' * 40} password={_SECRET} {'r' * 40}", 58, 70, "private-key-pem"),
    (f"password={_SECRET}", 0, 0, "x"),
    (" \t a \n b \t ", 2, 3, "x"),
]

SWEEPS: list[dict[str, Any]] = [
    {
        "name": "paste",
        "count": 3000,
        "counts": [1, 2, 3, 4, 5, 6],
        "pieces": [
            "password",
            "api_key",
            "secret",
            "token",
            "bearer",
            "Authorization: Bearer ",
            "db_",
            "MY-",
            "=",
            ": ",
            " is ",
            " was set to ",
            '"',
            "'",
            " ",
            "\n",
            "\t",
            _SECRET,
            "changeme",
            "hunter2",
            _AWS,
            "postgres://u:",
            "@h",
            "redis://",
            "://",
            "_",
            "-",
            "a1",
            "${X}",
            "x" * 9,
            "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3",
            "sk-",
            "-----BEGIN PRIVATE KEY-----\n",
            "is",
            "now",
            "to",
            "Q",
            "9",
        ],
    },
]


def _stream(name: str, i: int) -> bytes:
    out = b""
    block = 0
    while len(out) < 64:
        out += hashlib.sha256(f"credentials-spans {name} {i} {block}".encode()).digest()
        block += 1
    return out


def sweep_text(sweep: dict[str, Any], i: int) -> str:
    data = _stream(sweep["name"], i)
    counts, pieces = sweep["counts"], sweep["pieces"]
    n = counts[data[0] % len(counts)]
    return "".join(pieces[data[1 + j] % len(pieces)] for j in range(n))


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "k": "patterns",
            "generic": cr._GENERIC_KEYWORD_RE.pattern,
            "generic_flags": int(cr._GENERIC_KEYWORD_RE.flags & ~re.UNICODE),
            "uri": cr._URI_USERINFO_RE.pattern,
            "uri_flags": int(cr._URI_USERINFO_RE.flags & ~re.UNICODE),
            "context": cr._CONTEXT_CHARS,
        }
    ]
    seen: set[str] = set()
    for text in _hand():
        if text not in seen:
            seen.add(text)
            records.append(_plain(_case(text)))
    for text, start, end, kind in _SNIPS:
        records.append(
            {
                "k": "snip",
                **_text_field(text),
                "a": [start, end, kind],
                **text_field("o", cr._safe_snippet(text, start, end, kind)),
            }
        )
    for sweep in SWEEPS:
        h = hashlib.sha256()
        for i in range(sweep["count"]):
            h.update((_line(sweep_text(sweep, i)) + "\n").encode())
        hexed = [p.encode().hex() for p in sweep["pieces"]]
        records.append(
            {"k": "sweep", **sweep, "pieces": hexed, "sha256": h.hexdigest()}
        )
    return {"credentials_spans.jsonl": jsonl(records)}


UNICODE_TEMPLATES: list[list[str]] = [
    [
        "password",
        f"= {_SECRET}\npa",
        f"sword={_SECRET}\napi_",
        f"ey={_SECRET}\n",
        f"db_password={_SECRET}\nx",
        f"_password={_SECRET}\n",
    ],
    [
        "password is",
        f"set to {_SECRET}\npassword=Xy7kQ9",
        "pLm2Zt\npostgre",
        f"://u:{_SECRET}@h postgres://u",
        f":{_SECRET}@h\nAuthorization:",
        f"Bearer {_SECRET}\n",
        "ab ",
        "",
        "x",
    ],
]
_BLOCK = 0x4000
_BELOW = 0x40000
_STRIDE = 13
#: Ranges asked whole: everything before the CJK ideographs (the spaces,
#: the letters that fold into ASCII, the scripts with case), Yi to
#: Hangul, and the end of the BMP (compatibility ideographs, fullwidth
#: forms, specials).
_WHOLE = ((0, 0x3400), (0xA000, 0xAC00), (0xD7A4, 0x10000))


def asked(cp: int) -> bool:
    """The ranges of `_WHOLE` but the surrogates; elsewhere every
    thirteenth code point (the CJK and Hangul blocks, whose characters are
    all word characters without case, and the planes past the BMP, which
    hold no space and nothing that folds into ASCII)."""
    if 0xD800 <= cp <= 0xDFFF:
        return False
    return any(lo <= cp < hi for lo, hi in _WHOLE) or cp % _STRIDE == 0


def generate_unicode() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "k": "unicode",
            "templates": [[p.encode().hex() for p in t] for t in UNICODE_TEMPLATES],
        }
    ]
    for lo in range(0, _BELOW, _BLOCK):
        h = hashlib.sha256()
        for t in UNICODE_TEMPLATES:
            for cp in filter(asked, range(lo, lo + _BLOCK)):
                text = chr(cp).join(t)
                h.update(f"{cp:x}:{_line(text)}\n".encode())
        records.append({"lo": lo, "hi": lo + _BLOCK, "sha256": h.hexdigest()})
    return {"credentials_spans_unicode.jsonl": jsonl(records)}
