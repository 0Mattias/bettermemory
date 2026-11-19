"""Fixtures: the pure decoders of `origin_gitfiles`
(``origin_gitfiles_leaves.jsonl``, ``origin_gitfiles_leaves_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles.rs``. This pins it
to `_unquote_c_path`, `_quoted_header_path`, `_dequote_named_header`,
`_dequote_patch_headers` (and through it `claims.hunk_lines`),
`_decode_varint`, `_gitmodules_entries` and `_shortening_refs`. None of
them reads the machine.

A text is a JSON string, or its code points where it holds a surrogate
or two backslashes in a row.
Bytes are hex. An answer is ``["ok", value]`` or ``["raise", class]``.
The first line is a header (the count of each kind). Then:

- ``["unq", text, answer]``: `_unquote_c_path`.
- ``["qhp", text, answer]``: `_quoted_header_path`.
- ``["dnh", text, answer]``: `_dequote_named_header`.
- ``["dph", text, answer]``: `_dequote_patch_headers`.
- ``["hunk", text, answer]``: `claims.hunk_lines`, as decimal text.
- ``["var", data, pos, complete, answer]``: `_decode_varint`, the value
  as decimal text.
- ``["gm", data, complete, answer]``: `_gitmodules_entries`, each entry
  ``[stage, mode, oid]``; data is runs: each a hex string, or
  ``[byte, count]`` for a run of one byte.
- ``["gmcut", base, cut, complete, answer]``: the same on the first `cut`
  bytes of the header's ``bases[base]`` (runs as above).
- ``["short", text, answer]``: `_shortening_refs`.

A case goes in ``_python`` where `int` of a hunk's count reads digits past
ASCII (`re`'s ``\\d`` and `int` are the release's Unicode data) or more than
4,300 of them (the release's limit).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from bettermemory import claims, origin_gitfiles

from ._common import dumps_line, has_surrogate

_GATED_DIGITS = 4300


def _t(text: str) -> object:
    # Two backslashes in a row read, once JSON escapes them, as a UNC
    # path to the host-fact guard; such a text goes as code points too.
    if has_surrogate(text) or "\\\\" in text:
        return [ord(c) for c in text]
    return text


def _answer(call: Callable[[], Any], spell: Callable[[Any], object]) -> list[object]:
    try:
        value = call()
    except Exception as error:
        return ["raise", type(error).__name__]
    return ["ok", spell(value)]


def _decimal(value: int | None) -> object:
    return None if value is None else str(value)


def _opt(value: str | None) -> object:
    return None if value is None else _t(value)


# Quoted paths: every escape, octal runs of each length, bounds.
_QUOTED = [
    "",
    '"',
    '""',
    '"a"',
    '"a',
    'a"',
    '"a"b',
    '"a"b"',
    '"a" "b"',
    '"\\"',
    '"\\',
    '"x\\"',
    '"\\\\"',
    '"\\""',
    '"\\a\\b\\t\\n\\v\\f\\r"',
    '"\\e"',
    '"\\x41"',
    '"\\u0041"',
    '"\\0"',
    '"\\00"',
    '"\\000"',
    '"\\0000"',
    '"\\1"',
    '"\\7"',
    '"\\8"',
    '"\\18"',
    '"\\101"',
    '"\\1011"',
    '"\\377"',
    '"\\400"',
    '"\\777"',
    '"\\303\\251"',
    '"\\303"',
    '"\\251"',
    '"\\355\\240\\200"',
    '"\\300\\200"',
    '"\\360\\237\\230\\200"',
    '"\\364\\220\\200\\200"',
    '"\\303\\251x"',
    '"é"',
    '"\U0001f600"',
    '"\ud800"',
    '"\udcff"',
    '"a\ud800\\q"',
    '"\\q\ud800"',
    '"\x00"',
    '"a/b c"',
    '"a/\\n"',
    '"b/\\r"',
    '"a/x\\ny"',
    '"a/we\\"ird.py"',
    '"b/back\\\\slash"',
    '"a/\\t"',
    '"\\"\\"',
]

# Lines a header rewriter meets.
_LINES = [
    "",
    "diff --git a/x b/x",
    'diff --git "a/x" "b/x"',
    'diff --git "a/x" "b/y"',
    'diff --git "a/x" "c/x"',
    'diff --git "b/x" "b/x"',
    'diff --git "a/x"  "b/x"',
    'diff --git "a/x" "b/x" ',
    'diff --git "a/\\"q" "b/\\"q"',
    'diff --git "a/" "b/"',
    'diff --git "a/x" "b/x" "b/x"',
    'diff --git "a/x" "b/x" "b/x" "b/x"',
    'diff --git "a/a" "b/a" "b/a" "b/a"',
    'diff --git "a/\\" \\"" "b/\\" \\""',
    'diff --git "a/" "" "b/" ""',
    'diff --git "a/x" "b/x\\n"',
    'diff --git "a/x\\n" "b/x\\n"',
    'diff --git "a/\\303\\251" "b/\\303\\251"',
    'diff --git "a/\\303" "b/\\303"',
    'diff --git "a/\ud800" "b/\ud800"',
    'diff --git ""',
    'diff --git "',
    'diff --git "a" "b"',
    'diff --git "" ""',
    'diff --git "a/x" b/x',
    "rename from x",
    'rename from "x"',
    'rename from "a\\tb"',
    'rename from "a\\nb"',
    'rename from "a\\rb"',
    'rename from "a" b',
    'rename from "\ud800"',
    'rename to "y\\"z"',
    'copy from "\\303\\251"',
    'copy to "x',
    'copy to ""',
    'rename  from "x"',
    'Rename from "x"',
    '--- "a/x"',
    '+++ "b/x"',
]

_HUNKS = [
    "@@ -1 +1 @@",
    "@@ -1,2 +3,4 @@",
    "@@ -1,0 +1,0 @@",
    "@@ -0,0 +1 @@ ctx",
    "@@ -1,2 +3,4 @",
    "@@ -1, +1 @@",
    "@@ -,1 +1 @@",
    "@@ -1 +1",
    "@@ -1 + 1 @@",
    "@@  -1 +1 @@",
    " @@ -1 +1 @@",
    "@@ -1 +1 @@@",
    "@@ -1,\u00b2 +1 @@",
    "@@ -a +1 @@",
    "@@ -1 +1,2,3 @@",
    "@@ -01 +007,010 @@",
    "@@ -1 +18446744073709551615 @@",
    "@@ -1,18446744073709551615 +1,18446744073709551615 @@",
    "@@ -1,99999999999999999999999999 +1 @@",
    "@@@ -1 +1 @@",
    "",
    "@@",
    "@@ -" + "1" * _GATED_DIGITS + " +1 @@",
]

_HUNKS_PY = [
    "@@ -1,\u0663 +1,\u0662 @@",
    "@@ -\u0661 +\uff11,\uff12 @@",
    "@@ -1,\U0001d7d8 +1 @@",
    "@@ -1," + "1" * (_GATED_DIGITS + 1) + " +1 @@",
    "@@ -1 +1," + "0" * (_GATED_DIGITS + 1) + " @@",
]


def _streams() -> Iterator[str]:
    yield ""
    yield "plain\n--- a/x\n+++ b/x\n"
    yield 'commit\n--- "a/we\\"ird.py"\n+++ "b/we\\"ird.py"\n@@ -1 +1 @@\n-a\n+b\n'
    yield '--- "a/x"\t\n+++ "b/x"\t\n'
    yield '--- "a/x"\t\t\n+++ "b/x" \t\n'
    yield '--- "b/x"\n+++ "a/x"\n'
    yield '--- "a/x\\ny"\n+++ "b/x\\ry"\n'
    yield '--- "a/\\303\\251"\n+++ "b/\\303"\n'
    yield '--- "/dev/null"\n+++ "b/x"\n'
    yield '--- a/x\n+++ "b/x"\n@@ -1,2 +1,2 @@\n-x\n+++ "b/y"\n-z\n+w\n+++ "b/v"\n'
    yield '@@ -1 +1 @@\n--- "a/q"\n+++ "b/q"\n--- "a/r"\n'
    yield '@@ -1,3 +1 @@\n-a\n\\ No newline at end of file\n-b\n--- "a/s"\n+++ "b/s"\n'
    yield '@@ -1,3 +1 @@\n-a\n context\n--- "a/s"\n+++ "b/s"\n'
    yield '@@ -1,0 +1,0 @@\n--- "a/s"\n'
    yield '@@ -1,0 +1,0 @@\n-x\n--- "a/s"\n'
    yield 'diff --git "a/\\"" "b/\\""\nrename from "\\""\nrename to "\\"2"\n'
    yield 'diff --git "a/x" "b/x"\ndeleted file mode 100644\n'
    yield 'diff --git a/x b/x\ncopy from "\\303\\251"\ncopy to "\\303"\n'
    yield ' from "x"\n'
    yield ' to "x"'
    yield '--- "a/x"'
    yield '+++ "b/x"\r\n--- "a/y"\r'
    yield '--- "a/x"\n+++ "b/x"\n@@ -1 +1 @@\n+++ "b/x"\n+++ "b/x"\n'
    yield '--- "a/\ud800"\n+++ "b/x"\n'
    yield '--- "a/\\q\ud800"\n+++ "b/x"\n'
    yield '--- "a\n+++ "b\n'
    yield '+++ "b/\\"' + "\n" + '--- "a/\\"\\""\n'
    yield "@@ -1," + "1" * _GATED_DIGITS + ' +1 @@\n--- "a/x"\n'
    yield '@@ -1,3 +1 @@\n--- "a/x"\n'
    yield '@@ -1,\u00b2 +0,0 @@\n-x\n--- "a/x"\n'


def _streams_py() -> Iterator[str]:
    yield '@@ -1,\u0662 +0,0 @@\n-x\n-y\n--- "a/x"\n'
    yield "@@ -1," + "1" * (_GATED_DIGITS + 1) + ' +1 @@\n--- "a/x"\n'


def _hunk_gated(line: str) -> bool:
    match = claims._HUNK_RE.match(line)
    if match is None:
        return False
    groups = [g for g in match.groups() if g is not None]
    return any(len(g) > _GATED_DIGITS or not g.isascii() for g in groups)


def _stream_gated(stream: str) -> bool:
    return any(_hunk_gated(line) for line in stream.split("\n"))


def _varint(value: int) -> bytes:
    """git's encode_varint."""
    out = [value & 0x7F]
    value >>= 7
    while value:
        value -= 1
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


_VARINTS: list[bytes] = [
    b"",
    b"\x00",
    b"\x7f",
    b"\x80",
    b"\x80\x00",
    b"\x81\x00",
    b"\xff\x7f",
    b"\xff\xff",
    b"\x80" * 8 + b"\x00",
    b"\x80" * 8 + b"\x7f",
    b"\xff" * 8 + b"\x7f",
    b"\x80" * 9 + b"\x00",
    b"\xff" * 9 + b"\x7f",
    b"\xff" * 10,
    b"\xfe" + b"\xff" * 7 + b"\x7f",
    b"\x80" * 7 + b"\x81\x00",
    _varint((1 << 57) - 1),
    _varint(1 << 57),
    _varint((1 << 63) - 1),
    _varint((1 << 64) - 1),
    _varint(1 << 64),
    _varint(300),
    _varint(16511),
    _varint(16512),
]


def _entry(
    name: bytes,
    *,
    version: int,
    stage: int = 0,
    mode: int = 0o100644,
    oid: bytes = b"\x11" * 20,
    extended: int | None = None,
    length: int | None = None,
    strip: int = 0,
    suffix: bytes | None = None,
    flags_extra: int = 0,
) -> bytes:
    head = bytes(24) + mode.to_bytes(4, "big") + bytes(12) + oid
    size = min(len(name), 0x0FFF) if length is None else length
    flags = (stage << 12) | size | flags_extra
    if extended is not None:
        flags |= 0x4000
    out = head + flags.to_bytes(2, "big")
    if extended is not None:
        out += extended.to_bytes(2, "big")
    if version == 4:
        return out + _varint(strip) + (name if suffix is None else suffix) + b"\0"
    body = out + name
    pad = (len(out) + len(name) + 8) & ~7
    return body + bytes(pad - len(body))


def _index(version: int, entries: list[bytes], count: int | None = None) -> bytes:
    n = len(entries) if count is None else count
    return (
        b"DIRC" + version.to_bytes(4, "big") + n.to_bytes(4, "big") + b"".join(entries)
    )


def _runs(data: bytes) -> list[object]:
    out: list[object] = []
    plain = bytearray()
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 16:
            if plain:
                out.append(bytes(plain).hex())
                plain.clear()
            out.append([data[i], j - i])
        else:
            plain.extend(data[i:j])
        i = j
    if plain:
        out.append(bytes(plain).hex())
    return out


def _bases() -> list[bytes]:
    gm = b".gitmodules"
    return [
        _index(
            version,
            [
                _entry(b".gitignore", version=version),
                _entry(gm, version=version, stage=1, oid=b"\x01" * 20),
                _entry(gm, version=version, stage=2, mode=0o160000, oid=b"\x02" * 20),
                _entry(gm, version=version, stage=3, oid=b"\x03" * 20),
                _entry(b"a", version=version),
            ],
        )
        for version in (2, 3, 4)
    ]


def _indexes() -> Iterator[tuple[bytes, bool]]:
    gm = b".gitmodules"
    for version in (2, 3, 4):
        yield _bases()[version - 2] + b"trailing", True
        yield _index(version, []), True
        yield _index(version, [_entry(gm, version=version)], count=2), True
        yield _index(version, [_entry(gm, version=version)], count=2), False
        yield (
            _index(
                version, [_entry(b"z", version=version), _entry(gm, version=version)]
            ),
            True,
        )
        yield _index(version, [_entry(b".gitmodulez", version=version)]), True
        yield _index(version, [_entry(b".gitmodule", version=version)]), True
        yield _index(version, [_entry(b".gitmodules/x", version=version)]), True
        yield _index(version, [_entry(gm, version=version, flags_extra=0x8000)]), True
        for ext in (0, 0x4000, 0x2000, 0x6000, 0x1000, 0x8000, 0x0001):
            yield _index(version, [_entry(gm, version=version, extended=ext)]), True
        yield _index(version, [_entry(gm, version=version, extended=0)])[:-30], False
        yield _index(version, [_entry(gm, version=version, length=3)]), True
        yield _index(version, [_entry(gm, version=version, length=0)]), True
        yield _index(version, [_entry(gm, version=version, length=0x0FFF)]), True
        yield _index(version, [_entry(gm, version=version, length=0x0FFF)])[:72], False
        yield _index(version, [_entry(gm, version=version, length=0x0FFF)])[:72], True
        long = b".gitmodules" + b"x" * 4100
        yield (
            _index(
                version,
                [
                    _entry(b".a" + b"y" * 4093, version=version),
                    _entry(gm, version=version),
                ],
            ),
            True,
        )
        yield (
            _index(
                version, [_entry(gm, version=version), _entry(long, version=version)]
            ),
            True,
        )
    # Version 4's prefix compression.
    v4 = [
        _entry(b".gitignore", version=4),
        _entry(gm, version=4, strip=len(b"ignore"), suffix=b"modules"),
    ]
    yield _index(4, v4), True
    yield (
        _index(
            4,
            [
                _entry(b".gitignore", version=4),
                _entry(gm, version=4, strip=11, suffix=gm),
            ],
        ),
        True,
    )
    yield (
        _index(
            4,
            [
                _entry(b".gitignore", version=4),
                _entry(gm, version=4, strip=10, suffix=gm[1:]),
            ],
        ),
        True,
    )
    yield _index(4, [_entry(b"x", version=4, strip=1, suffix=b"x")]), True
    yield _index(4, [_entry(gm, version=4, strip=0, length=0x0FFF)]), True
    yield _index(4, [_entry(gm, version=4, suffix=b".gitmodules\0junk")]), True
    yield _index(4, [_entry(gm, version=4)])[:-1], True
    yield _index(4, [_entry(gm, version=4)])[:-1], False
    yield (
        _index(4, [_entry(b".gitignore", version=4)])[: 62 + 12] + b"\xff" * 10,
        True,
    )
    yield _index(4, [_entry(b".gitignore", version=4)])[: 62 + 12] + b"\x80", False
    yield _index(4, [_entry(b".gitignore", version=4)])[: 62 + 12] + b"\x80", True
    # Headers.
    yield b"", True
    yield b"", False
    yield b"DIRC\x00\x00\x00\x02\x00\x00\x00", False
    yield b"DIRC\x00\x00\x00\x02\x00\x00\x00", True
    yield b"DIRX\x00\x00\x00\x02\x00\x00\x00\x00", True
    yield b"DIRC\x00\x00\x00\x01\x00\x00\x00\x00", True
    yield b"DIRC\x00\x00\x00\x05\x00\x00\x00\x00", True
    yield b"DIRC\x00\x00\x01\x02\x00\x00\x00\x00", True
    yield b"DIRC\x00\x00\x00\x02\xff\xff\xff\xff", True
    yield b"DIRC\x00\x00\x00\x02\xff\xff\xff\xff", False
    # A version 2 entry claiming the extended flag.
    yield _index(2, [_entry(gm, version=3, extended=0)]), True


_REFS = [
    "",
    "main",
    "refs",
    "refs/",
    "refs/x",
    "refs/heads/",
    "refs/heads/main",
    "refs/heads/a/b",
    "refs/tags/v1",
    "refs/remotes/origin/main",
    "refs/remotes/origin/HEAD",
    "refs/remotes//HEAD",
    "refs/remotes/HEAD",
    "refs/remotes/x/HEAD",
    "refs/remotes/heads/HEAD",
    "refs/heads/refs/heads/x",
    "refs/heads/HEAD",
    "refs/tags/",
    "refs/remotes/",
    "refs/heads/é",
    "refs/heads/\ud800",
    "refs/heads/{}",
    "refs/heads/{0}",
    "HEAD",
    "refs/HEAD",
]


def _rows(gated: bool) -> list[list[object]]:
    rows: list[list[object]] = []
    if not gated:
        for text in _QUOTED:
            rows.append(
                [
                    "unq",
                    _t(text),
                    _answer(lambda: origin_gitfiles._unquote_c_path(text), _opt),
                ]
            )
        for text in _QUOTED:
            rows.append(
                [
                    "qhp",
                    _t(text),
                    _answer(lambda: origin_gitfiles._quoted_header_path(text), _opt),
                ]
            )
        for text in [*_LINES, *_QUOTED]:
            rows.append(
                [
                    "dnh",
                    _t(text),
                    _answer(lambda: origin_gitfiles._dequote_named_header(text), _opt),
                ]
            )
    hunks = _HUNKS_PY if gated else _HUNKS
    for text in hunks:
        assert _hunk_gated(text) == gated, text
        rows.append(
            ["hunk", _t(text), _answer(lambda: claims.hunk_lines(text), _decimal)]
        )
    streams = list(_streams_py() if gated else _streams())
    if not gated:
        streams += _LINES + [line + "\n" + line for line in _LINES]
    for text in streams:
        if _stream_gated(text) != gated:
            continue
        rows.append(
            [
                "dph",
                _t(text),
                _answer(lambda: origin_gitfiles._dequote_patch_headers(text), _t),
            ]
        )
    if gated:
        return rows
    for data in _VARINTS:
        for pos in (0, 1):
            for complete in (True, False):
                rows.append(
                    [
                        "var",
                        data.hex(),
                        pos,
                        int(complete),
                        _answer(
                            lambda: origin_gitfiles._decode_varint(
                                data, pos, complete=complete
                            ),
                            lambda v: [str(v[0]), v[1]],
                        ),
                    ]
                )
    gm_answer = lambda data, complete: _answer(  # noqa: E731
        lambda: origin_gitfiles._gitmodules_entries(data, complete=complete),
        lambda v: [[s, m, o.hex()] for s, m, o in v],
    )
    for number, base in enumerate(_bases()):
        for complete in (True, False):
            for cut in range(len(base) + 1):
                rows.append(
                    [
                        "gmcut",
                        number,
                        cut,
                        int(complete),
                        gm_answer(base[:cut], complete),
                    ]
                )
    for data, complete in _indexes():
        rows.append(
            [
                "gm",
                _runs(data),
                int(complete),
                _answer(
                    lambda: origin_gitfiles._gitmodules_entries(
                        data, complete=complete
                    ),
                    lambda v: [[s, m, o.hex()] for s, m, o in v],
                ),
            ]
        )
    for text in _REFS:
        rows.append(
            [
                "short",
                _t(text),
                _answer(
                    lambda: origin_gitfiles._shortening_refs(text),
                    lambda v: [_t(x) for x in v],
                ),
            ]
        )
    return rows


def _file(name: str, gated: bool) -> bytes:
    rows = _rows(gated)
    kinds = ("unq", "qhp", "dnh", "dph", "hunk", "var", "gm", "gmcut", "short")
    header: dict[str, object] = {"fixture": name}
    if not gated:
        header["bases"] = [_runs(base) for base in _bases()]
    for kind in kinds:
        header[kind] = sum(1 for r in rows if r[0] == kind)
    header["raised"] = sum(
        1 for r in rows if isinstance(r[-1], list) and r[-1][0] == "raise"
    )
    lines = [dumps_line(header)] + [dumps_line(r) for r in rows]
    return "".join(line + "\n" for line in lines).encode("ascii")


def generate() -> dict[str, bytes]:
    return {"origin_gitfiles_leaves.jsonl": _file("origin_gitfiles_leaves", False)}


def generate_python() -> dict[str, bytes]:
    return {
        "origin_gitfiles_leaves_python.jsonl": _file(
            "origin_gitfiles_leaves_python", True
        )
    }
