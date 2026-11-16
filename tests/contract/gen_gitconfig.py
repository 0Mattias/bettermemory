"""Fixture: the text of a git config file as the reference reads it
(``gitconfig.jsonl``).

The native build reads git config files with a reader of its own; this
pins it to the text half of `bettermemory.gitconfig`: `git_entries` (the
lexer and parser), `_spelled` (a name split into section, subsection and
key), the NUL cut `_parse_config` makes of each value, `spaced` with
`KEEPS_SPACES_SINCE` (the only place a git version bears on the text) and
the typed reads `git_int` and `git_bool`. Includes, files, stamps and
conditions are the next unit's and are not read here: an ``include`` or
``includeIf`` line is an entry like any other.

Every byte string is written as ``=`` and itself where it is made of
letters, digits, space and a few marks (`_SAFE`: no slash, no backslash, no
control byte), else as its hex, so that no text a case holds (a backslash,
a path-shaped value, bytes that are not UTF-8) reaches the file as itself.
"hex" below means this form. The first line is a header: the counts, and ``versions``, the
git versions each case is answered under (none known, and below, at and
above 2.45, git's ``KEEPS_SPACES_SINCE``, plus the two releases CI
builds). Then one line per config text:

- ``{"k": "text", "c": hex}`` or ``{"k": "text", "p": [[hex, n], ...]}``
  (the text is each piece repeated ``n`` times, joined: a recipe for the
  long and hostile texts), or ``{"k": "text", "m": [i, op, at]}``, the
  header's ``reals[i]`` mutated (`mutated`: ``as``, ``crlf``, ``cr``,
  ``strip`` the whole text; ``flip``, ``del``, ``dup`` the byte at
  ``at``; ``cut`` before it), with ``"e": null`` where `git_entries` refuses
  the text, or ``"e": [entry, ...]`` in order, each
  ``[name, value, section, subsection, key, spaced]``: name and value hex
  (value null for a key with no ``=``; a value longer than 256 bytes as
  ``{"n": length, "sha256": hex}``), section and key text, subsection hex
  or null, spaced 0 or 1, and, only where the value holds a NUL byte, a
  seventh: the value cut at its first NUL as `_parse_config` takes it
  (where those rows written as ``dumps_line``
  writes them pass 400 bytes, ``"e": {"n": count, "sha256": hex}`` of
  that line instead); and ``"w"``, one character a version, ``1``
  where every value reads as written under that version (no value is
  `spaced`, or the version is at least 2.45), ``0`` where not, written only where some version reads ``0`` (left out,
  every version reads ``1``).
- ``{"k": "value", "v": hex or null, "i": text or null, "b": bool or
  null}``: `git_int` (its decimal text) and `git_bool` of a value.

Not gated. The reference reads bytes with `re` patterns of ASCII classes
and bytes methods (``lower``, ``strip``, ``replace``), which consult no
Unicode data, no locale and no platform; it words nothing (a refusal is
None, and the reasons a caller gives name its file, which is the next
unit's). Checked: the file is the same byte for byte under Python 3.11
and 3.13. No git is run here: what real git makes of these texts is
cross-checked apart from the fixture.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from bettermemory import gitconfig

from ._common import dumps_line

#: The versions each case is answered under: None (git has not named its
#: version), around `KEEPS_SPACES_SINCE`, and the releases CI builds.
VERSIONS: list[tuple[int, int, int] | None] = [
    None,
    (2, 34, 8),
    (2, 44, 4),
    (2, 45, 0),
    (2, 56, 0),
]

_LONG = 256
#: A case: a text, a recipe of pieces and counts, or a mutation of a real
#: config, ``("m", index, op, at)`` (`mutated`).
_Case = bytes | list[tuple[bytes, int]] | tuple[str, int, str, int]
#: Entries written longer than this are recorded by their count and the
#: SHA-256 of their line (`dumps_line`).
_ROWS = 400

_REAL = [
    b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
    b"\tlogallrefupdates = true\n\tignorecase = true\n\tprecomposeunicode = true\n"
    b'[remote "origin"]\n\turl = https://example.invalid/team/alpha.git\n'
    b"\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    b'[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
    b'[url "https://example.invalid/"]\n\tinsteadOf = ex:\n'
    b'\tpushInsteadOf = "git@example.invalid:"\n'
    b"[include]\n\tpath = ~/extra.inc\n"
    b'[includeIf "gitdir:~/work/"]\n\tpath = work.inc\n'
    b'[includeIf "onbranch:feature/*"]\n\tpath = branch.inc\n'
    b'[includeIf "hasconfig:remote.*.url:https://example.invalid/**"]\n'
    b"\tpath = has.inc\n",
    b"# a comment\n; another\n[user]\n\tname = Fixture Person ; trailing\n"
    b'\temail = "fixture@example.invalid" # q\n[alias]\n\tst = status -sb\n'
    b'\tlg = "log --graph --format=\\"%h %s\\""\n'
    b"\tlong = !sh -c 'echo \\\n  continued'\n",
    b'[remote "up stream"]\r\n\turl = ssh://example.invalid/r.git\r\n'
    b"\tpushurl = \r\n\tmirror\r\n\ttagOpt = --no-tags\r\n"
    b'[Remote "Mixed\\"Case"]\r\n\tURL = x\r\n',
    b"[core]\n\tworktree = /srv/a\tb\n\tsshCommand = ssh -i key\r\n"
    b"\tbigFileThreshold = 512m\n\tcompression = -1\n\tpackedGitLimit = 0x10k\n",
]

_SECTIONS = [
    b"[core]",
    b"[Core]",
    b"[CORE]",
    b"[a.b]",
    b"[a.B.c]",
    b"[a-b0]",
    b"[0]",
    b"[-]",
    b"[.]",
    b"[]",
    b"[ ]",
    b"[a b]",
    b"[a_b]",
    b"[a",
    b"[",
    b"[a]]",
    b'[a "x"]',
    b'[a "X y"]',
    b'[a\t"x"]',
    b'[a \t "x"]',
    b'[a \r"x"]',
    b'[a\n"x"]',
    b'[a"x"]',
    b'[a "x" ]',
    b'[a "x"',
    b'[a "x',
    b'[a ""]',
    b'[ "x"]',
    b'[a.b "x"]',
    b'[A "MiXeD"]',
    b'[a "\\""]',
    b'[a "\\\\"]',
    b'[a "\\x\\y"]',
    b'[a "x\\"]',
    b'[a "x\\\ny"]',
    b'[a "x\ny"]',
    b'[a "x.y"]',
    b'[a "x\x00y"]',
    b'[a "\xff\xfe"]',
    b'[a "\xc3\xa9"]',
    b'[a "x]"]',
    b'[a "x" "y"]',
    b"[a\x00]",
    b"[\xc3\xa9]",
    b'[remote "origin"]',
    b'[includeIf "gitdir:~/x/"]',
    b'[url "https://example.invalid/"]',
]

_KEYS = [
    b"k",
    b"K",
    b"Key",
    b"k1",
    b"k-",
    b"k-x",
    b"1k",
    b"-k",
    b"k_x",
    b"k.x",
    b"k ",
    b"k\t",
    b"k\r",
    b"k\v",
    b"k #c",
    b"k;c",
    b"k x",
    b"\xc3\xa9",
    b"k\x00",
    b"=v",
    b" k",
]

_VALUES = [
    b"",
    b" ",
    b"v",
    b" v ",
    b"\tv\t",
    b"a b",
    b"a  b",
    b"a\tb",
    b"a\rb",
    b"v\r",
    b"\rv",
    b"a\x0bb",
    b"a\x0cb",
    b'"v"',
    b'" v "',
    b'""',
    b'a ""',
    b'"" a',
    b'"a"b"c"',
    b'"a#b"',
    b'"a;b"',
    b"a#b",
    b"a;b",
    b"a #b",
    b'a"',
    b'"a',
    b'"a\\"',
    b"\\t",
    b"\\b",
    b"\\n",
    b"\\\\",
    b'\\"',
    b"\\a",
    b"\\x41",
    b"\\0",
    b"\\r",
    b"\\ ",
    b"\\#",
    b"\\",
    b"a\\",
    b'"a\\',
    b"a\\\nb",
    b"a \\\n b",
    b'"a\\\nb"',
    b"a\\\r\nb",
    b"a\\\rb",
    b"a#\\\nb",
    b'"a\nb"',
    b"a\x00b",
    b"\x00",
    b"\xff",
    b"a\xc3\xa9",
    b"\xe2\x82",
    b"\x01\x1f\x7f",
    b"https://example.invalid/team/r.git",
    b"~/x/y",
    b"%(prefix)/etc/x",
    b"true",
    b"0x10k",
]

_ENDINGS = [b"\n", b"\r\n", b"\r", b""]

_INTS = [
    None,
    b"",
    b"0",
    b"1",
    b"-1",
    b"+1",
    b" 7",
    b"\t\n\v\f\r7",
    b"7 ",
    b"07",
    b"08",
    b"0x",
    b"0x1f",
    b"0X1F",
    b"-0x10",
    b"0xg",
    b"1k",
    b"1K",
    b"1m",
    b"1M",
    b"1g",
    b"1G",
    b"1t",
    b"1kb",
    b"1 k",
    b"k",
    b"-",
    b"+",
    b"--1",
    b"2147483647",
    b"2147483648",
    b"-2147483648",
    b"-2147483649",
    b"2097151k",
    b"2097152k",
    b"-2097152k",
    b"-2097153k",
    b"2047m",
    b"2048m",
    b"1g",
    b"2g",
    b"-2g",
    b"-3g",
    b"9223372036854775807",
    b"9223372036854775808",
    b"-9223372036854775808",
    b"-9223372036854775809",
    b"99999999999999999999999999",
    b"0x7fffffff",
    b"0x80000000",
    b"017777777777",
    b"020000000000",
    b"1\x002",
    b"\x001",
    b"true",
    b"TRUE",
    b"Yes",
    b"on",
    b"false",
    b"No",
    b"OFF",
    b"tru",
    b"truee",
    b" true",
    b"0x0",
    b"00",
    b"0k",
    b"\xff",
    b"1\xff",
    b"12345678901234567890k",
]


def _texts() -> Iterator[_Case]:
    """Each config text, built by rule; a recipe (pieces and counts) for
    the long ones."""
    # Each section header alone, closed by each line ending, then a key.
    for sec in _SECTIONS:
        for end in _ENDINGS:
            yield sec + end
            yield sec + end + b"\tk = v" + end
        yield sec + b"k=v\n"
        yield sec + b" k = v\n"
        yield sec + b"#c\nk\n"
    # Each key alone and after a section, valueless, with a value, and at
    # the end of the file.
    for key in _KEYS:
        for end in _ENDINGS:
            yield key + end
            yield b"[s]" + end + key + end
            yield b"[s]\n" + key + b"=v" + end
            yield b"[s]\n" + key + b" = v" + end
    # Each value after a key, closed by each line ending and by none.
    for value in _VALUES:
        for end in _ENDINGS:
            yield b"[s]\nk =" + value + end
        yield b"[s]\nk = " + value + b"\nj = w\n"
        yield b'[s "t"]\n\tk = ' + value + b" # c\n"
        yield b"k=" + value
    # Pairs: two values one after the other, each pair of a sample.
    sample = _VALUES[::5]
    for a in sample:
        for b in sample:
            yield b"[s]\nk = " + a + b"\nk = " + b + b"\n"
    # Pairs of headers, a key between them.
    hsample = _SECTIONS[::4]
    for a in hsample:
        for b in hsample:
            yield a + b"\nk\n" + b + b"\nk=1\n"
    # Each escape, valid and not, plain and in quotes, at the end of the
    # line and of the file.
    for byte in range(256):
        esc = b"\\" + bytes([byte])
        yield b"[s]\nk = a" + esc + b"z\n"
        if byte % 8 == 0 or byte in b'tbn\\"\n\r ':
            yield b'[s]\nk = "' + esc + b'"\n'
            yield b"[s]\nk = " + esc
    # Each byte as a value, as a key and in a quoted subsection.
    for byte in range(256):
        one = bytes([byte])
        yield b"[s]\nk = a" + one + b"b\n"
        if byte % 4 == 0 or byte < 0x21 or byte > 0x7E:
            yield one + b"\n"
            yield b'[s "' + one + b'"]\nk\n'
    # Quoting states at the end of a line and of the file.
    for opened in (b'"', b'"a', b'a"', b'"a""', b'"a" "', b'"\\"'):
        for end in _ENDINGS:
            yield b"[s]\nk = " + opened + end
            yield b"[s]\nk = " + opened + end + b"j = w\n"
    # The byte order mark, whole, in part, and elsewhere.
    for lead in (
        b"\xef\xbb\xbf",
        b"\xef\xbb",
        b"\xef",
        b"\xef\xbb\xbf\xef\xbb\xbf",
        b"\xbb\xbf",
        b"\xfe\xff",
        b"\xff\xfe",
    ):
        yield lead
        yield lead + b"[s]\nk = v\n"
        yield lead + b"\n[s]\nk = v\n"
    yield b"[s]\n\xef\xbb\xbfk = v\n"
    yield b"[s]\nk = \xef\xbb\xbfv\n"
    # Blank space and comments alone.
    for text in (
        b"",
        b"\n",
        b"\r\n",
        b"\r",
        b" \t\r\n",
        b"\v",
        b"\f",
        b"#",
        b"# c",
        b";c\n",
        b"#c\r\n;d\r",
        b"  # c\n\t; d",
        b"#\\\nk",
        b"\x00",
        b"\x00\x00\x00",
        b"[s]\n\x00",
        b"[s]\nk = a\x00\tb\n",
        b"[s]\nk = a\x00\rb\n",
        b"[s]\nk = a\x00b\tc\n",
        b"[s]\nk = \x00a\n",
        b"[s]\nk = \x00\ta\n",
        b"[s]\nk = a\x00\n",
        b"[s]\nk = a\x00b\x00c\x00\n",
        b"[s]\nk = \x00\x00\x00\n",
        b'[s]\nk = "a\x00 b\t"\n',
        b'[s]\nk = "\x00"\n',
        b'[s]\nk = x "a\x00\r" y\n',
        b"[s]\nk = a\tb\x00c\n",
        b'[s "x\x00\ty"]\nk = v\n',
        b'[s "\x00"]\nk = a\x00\tb\n',
        b'[s "x\x00y"]\nk\n[s "x\x00z"]\nk\n',
        b"[s]\nk = a\\\n\x00\tb\n",
        b"[s]\nk\x00= v\n",
    ):
        yield text
    # Real-shaped configs, and each mutated: a byte flipped, deleted or
    # doubled, and the text cut, at positions spread through it.
    for i, real in enumerate(_REAL):
        for op in ("as", "crlf", "cr", "strip"):
            yield ("m", i, op, 0)
        step = max(1, len(real) // 9)
        for at in range(0, len(real), step):
            for op in ("flip", "del", "dup", "cut"):
                yield ("m", i, op, at)
    # An error at the start, in the middle and at the end of a file.
    body = _REAL[0]
    for bad in (b"[", b"k x\n", b'k = "a\n', b"k = \\x\n", b"1k\n", b"[a b]\n"):
        yield bad + body
        half = body.index(b"[remote")
        yield body[:half] + bad + body[half:]
        yield body + bad
    # Boundary sizes and hostile texts, as recipes.
    for n in (255, 256, 257, 4096, 65536):
        yield [(b"[s]\nk = ", 1), (b"x", n), (b"\n", 1)]
        yield [(b'[s "', 1), (b"y", n), (b'"]\nk\n', 1)]
        yield [(b"[s]\n", 1), (b"k", n), (b" = v\n", 1)]
    yield [(b"[s]\nk = ", 1), (b"x", (1 << 21) + 1), (b"\n", 1)]
    yield [(b"[s]\n", 1), (b"k\n", 1 << 16)]
    yield [(b"[s]\n", 1), (b"k = a\\\n", 1 << 14), (b"b\n", 1)]
    yield [(b"[s]\nk = ", 1), (b'""', 1 << 15), (b"\n", 1)]
    yield [(b"[s]\nk = ", 1), (b'"', (1 << 15) + 1), (b"\n", 1)]
    yield [(b"[s]\nk = ", 1), (b"\\t", 1 << 14), (b"\n", 1)]
    yield [(b"[s]\nk = ", 1), (b"\\\\", 1 << 14)]
    yield [(b"\\", 1 << 12)]
    yield [(b"\x00", 1 << 14)]
    yield [(b"\xff", 1 << 14)]
    yield [(b"[s]\nk = ", 1), (b"\xff\xfe", 1 << 13), (b"\n", 1)]
    yield [(b"#", 1 << 14), (b"\n[s]\nk\n", 1)]
    yield [(b" \t\r\n", 1 << 12), (b"[s]\nk\n", 1)]
    yield [(b"[a]", 1 << 12)]
    yield [(b'[a "\\', 1), (b'\\"', 1 << 12), (b'"]\nk\n', 1)]
    yield [(b"[s]\nk = ", 1), (b"a \t", 1 << 13), (b"\n", 1)]
    yield [(b"[s]\nk = ", 1), (b'a "', 1 << 12), (b"\n", 1)]
    yield [(b"[s]\nk = v ;", 1), (b"x", 1 << 14), (b"\n", 1)]


_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,=_:-+*~%()[]#;!@'\"\n\t\r"
)


def _b(data: bytes) -> str:
    """`data` as ``=`` and itself where it is made of `_SAFE` bytes (LF, tab and CR among them, which JSON escapes; no
    slash, no backslash, nothing a path is spelled with), else its hex."""
    if all(c in _SAFE for c in data):
        return "=" + data.decode("ascii")
    return data.hex()


def mutated(real: bytes, op: str, at: int) -> bytes:
    """`real` as it is (``as``), its LF written CR LF or CR, its final
    LFs stripped, or its byte at `at` flipped (XOR 0x20), deleted or
    doubled, or the text cut before it."""
    if op == "as":
        return real
    if op == "crlf":
        return real.replace(b"\n", b"\r\n")
    if op == "cr":
        return real.replace(b"\n", b"\r")
    if op == "strip":
        return real.rstrip(b"\n")
    if op == "flip":
        return real[:at] + bytes([real[at] ^ 0x20]) + real[at + 1 :]
    if op == "del":
        return real[:at] + real[at + 1 :]
    if op == "dup":
        return real[:at] + real[at : at + 1] * 2 + real[at + 1 :]
    assert op == "cut"
    return real[:at]


def _built(text: _Case) -> bytes:
    if isinstance(text, bytes):
        return text
    if isinstance(text, tuple):
        return mutated(_REAL[text[1]], text[2], text[3])
    return b"".join(p * n for p, n in text)


def _hex_or_digest(value: bytes) -> str | dict[str, object]:
    if len(value) > _LONG:
        return {"n": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    return _b(value)


def _reads_as_written(
    values: list[bytes | None], version: tuple[int, int, int] | None
) -> bool:
    """The rule callers apply (`location_git._spellings`,
    `gitfile_rule.tree_format`, `_include`): a value holding a tab or a
    CR reads as written only under a git known to be at least
    `KEEPS_SPACES_SINCE`."""
    if not any(v is not None and gitconfig.spaced(v) for v in values):
        return True
    return version is not None and version[:2] >= gitconfig.KEEPS_SPACES_SINCE


def _text_record(text: _Case) -> dict[str, object]:
    data = _built(text)
    record: dict[str, object] = {"k": "text"}
    if isinstance(text, bytes):
        record["c"] = _b(text)
    elif isinstance(text, tuple):
        record["m"] = list(text[1:])
    else:
        record["p"] = [[_b(piece), n] for piece, n in text]
    entries = gitconfig.git_entries(data)
    if entries is None:
        record["e"] = None
        return record
    rows: list[list[object]] = []
    cut: list[bytes | None] = []
    for raw, value in entries:
        section, sub, key = gitconfig._spelled(raw)
        value_cut = None if value is None else value.split(b"\0", 1)[0]
        cut.append(value_cut)
        rows.append(
            [
                _b(raw),
                None if value is None else _hex_or_digest(value),
                section,
                key,
                None if sub is None else _b(gitconfig.subsection_bytes(sub)),
                int(value is not None and gitconfig.spaced(value)),
            ]
        )
        if value is not None and value_cut != value:
            assert value_cut is not None
            rows[-1].append(_hex_or_digest(value_cut))
    shown = dumps_line(rows)
    if len(shown) > _ROWS:
        record["e"] = {
            "n": len(rows),
            "sha256": hashlib.sha256(shown.encode()).hexdigest(),
        }
    else:
        record["e"] = rows
    answers = "".join(str(int(_reads_as_written(cut, v))) for v in VERSIONS)
    if "0" in answers:
        record["w"] = answers
    return record


def generate() -> dict[str, bytes]:
    seen: set[bytes] = set()
    records: list[dict[str, object]] = []
    for text in _texts():
        key = _built(text) if isinstance(text, bytes) else repr(text).encode()
        if key in seen:
            continue
        seen.add(key)
        records.append(_text_record(text))
    for value in dict.fromkeys(_INTS):
        number = gitconfig.git_int(value)
        records.append(
            {
                "k": "value",
                "v": None if value is None else _b(value),
                "i": None if number is None else str(number),
                "b": gitconfig.git_bool(value),
            }
        )
    texts = [r for r in records if r["k"] == "text"]
    header = {
        "fixture": "gitconfig",
        "texts": len(texts),
        "refused": sum(1 for r in texts if r["e"] is None),
        "values": len(records) - len(texts),
        "reals": [_b(real) for real in _REAL],
        "versions": [None if v is None else list(v) for v in VERSIONS],
    }
    lines = [dumps_line(header)] + [dumps_line(r) for r in records]
    return {"gitconfig.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
