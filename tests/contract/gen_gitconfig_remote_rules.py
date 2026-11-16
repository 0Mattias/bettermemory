"""Fixture: where git's reader of remotes dies on one config variable, as
the reference reads it (``gitconfig_remote_rules.jsonl``).

Pins `bettermemory.gitconfig._remote_rule` (on the name as `_spelled`
splits it), `refname_valid` and `refspec_valid`. Cases are built by rule:
every key the rule names, in lower case as git's parser gives it and in
other spellings, under each section it reads, with no value, an empty one
and each boolean spelling `git_bool` reads or refuses; ref names at the
edges of ``check_refname_format`` (each forbidden byte, ``..``, ``@{``,
``.lock``, a trailing dot or slash, a leading dot or dash, a lone ``@``,
control bytes and bytes at or above 0x80) under each flag; refspecs in
each form (``+``, ``^``, one-sided, globs on either side, sides of 40 and
64 hex digits) as fetch and as push refspecs.

A byte string is ``=`` and itself where it is printable ASCII with no
backslash and no double quote, else its hex. The first line is a header
(the counts). Then:

- ``["rule", name, value, why]``: value null for a key with no ``=``; why
  null where the rule accepts.
- ``["ref", name, pattern, onelevel, valid]``.
- ``["spec", spec, fetch, valid]``: valid null where it turns on the
  object format.

Not gated: bytes in, plain answers out. The reference compares bytes and
ASCII text; `_spelled` decodes a section and a key as ASCII with
``replace`` and a subsection with ``surrogateescape``, which read the same
under every Python and platform. Checked: the same bytes under Python 3.11
and 3.13.
"""

from __future__ import annotations

from collections.abc import Iterator

from bettermemory import gitconfig

from ._common import dumps_line


def _b(data: bytes) -> str:
    if all(0x20 <= c <= 0x7E and c not in (0x5C, 0x22) for c in data):
        return "=" + data.decode("ascii")
    return data.hex()


_REMOTE_KEYS = [
    *sorted(gitconfig._REMOTE_STRINGS),
    *sorted(gitconfig._REMOTE_BOOLS),
    "promisor",
    "partialclonefilter",
    "pushdefault",
    "other",
]
_BRANCH_KEYS = [*sorted(gitconfig._BRANCH_STRINGS), "other"]
_URL_KEYS = [*sorted(gitconfig._URL_STRINGS), "other"]

#: Values for each key: none, empty, booleans and not, refspecs.
_VALUES: list[bytes | None] = [
    None,
    b"",
    b"TRUE",
    b"off",
    b"-1",
    b"2k",
    b"maybe",
    b"+refs/heads/*:refs/o/*",
    b"a..b",
    b"^" + b"a" * 40,
]


def _names() -> Iterator[bytes]:
    for sub in (b"o", b"", b"/o", b"o.x", b"O", b"\xc3\xa9"):
        for key in _REMOTE_KEYS:
            yield b"remote." + sub + b"." + key.encode()
    for key in _REMOTE_KEYS:
        yield b"remote." + key.encode()
    for sub in (b"main", b"", b"/x"):
        for key in _BRANCH_KEYS:
            yield b"branch." + sub + b"." + key.encode()
    for key in _BRANCH_KEYS:
        yield b"branch." + key.encode()
    for sub in (b"https://example.invalid/", b""):
        for key in _URL_KEYS:
            yield b"url." + sub + b"." + key.encode()
    for key in _URL_KEYS:
        yield b"url." + key.encode()
    # Other spellings: git's parser gives section and key in lower case,
    # but the rule reads what it is given.
    for name in (
        b"Remote.o.url",
        b"remote.o.URL",
        b"REMOTE.o.Fetch",
        b"Branch..merge",
        b"URL.x.insteadOf",
        b"remote.o.Promisor",
        b"core.url",
        b"remote",
        b"",
        b"remote.o.\xc3\xa9",
    ):
        yield name


#: Names for `refname_valid`, at the edges of check_refname_format.
def _refnames() -> Iterator[bytes]:
    yield from (
        b"",
        b"@",
        b"@@",
        b"a@",
        b"@a",
        b"a@{b",
        b"a@b{",
        b"a",
        b"a/b",
        b"refs/heads/main",
        b"a.",
        b"a/",
        b"/a",
        b"a//b",
        b".a",
        b"a/.b",
        b"a..b",
        b"a.b",
        b"a.lock",
        b"a.lock/b",
        b"a/b.lock",
        b"a.locked",
        b".lock",
        b"-a",
        b"a/-b",
        b"*",
        b"a*",
        b"a/*",
        b"*/*",
        b"a**",
        b"HEAD",
        b"\xc3\xa9",
        b"a\xff",
    )
    for byte in [*range(0x20), 0x7F, *b" ~^:?[\\*{}!\"#$%&'()<>|=,;`"]:
        yield b"a" + bytes([byte]) + b"b"


#: Refspec sides and forms.
def _refspecs() -> Iterator[bytes]:
    sides = [
        b"",
        b"a",
        b"refs/heads/a",
        b"refs/heads/*",
        b"refs/*/a",
        b"a*b",
        b"*",
        b"@",
        b"a..b",
        b"a.lock",
        b"a b",
        b"0" * 40,
        b"f" * 40,
        b"F" * 64,
        b"0" * 39,
        b"0" * 41,
        b"g" * 40,
        b"0" * 63,
    ]
    for prefix in (b"", b"+", b"^"):
        for left in sides:
            yield prefix + left
            for right in (b"", b"a", b"refs/r/*", b"a*b", b"a..b"):
                yield prefix + left + b":" + right
    yield from (
        b":",
        b"+:",
        b"::",
        b"a:b:c",
        b"^:",
        b"^",
        b"++a",
        b"+^a",
        b"a:" + b"0" * 40,
    )


def generate() -> dict[str, bytes]:
    rows: list[list[object]] = []
    for name in dict.fromkeys(_names()):
        for value in _VALUES:
            why = gitconfig._remote_rule(gitconfig._spelled(name), value)
            rows.append(["rule", _b(name), None if value is None else _b(value), why])
    for ref in dict.fromkeys(_refnames()):
        for pattern in (False, True):
            for onelevel in (False, True):
                valid = gitconfig.refname_valid(ref, pattern=pattern, onelevel=onelevel)
                rows.append(["ref", _b(ref), int(pattern), int(onelevel), valid])
    for spec in dict.fromkeys(_refspecs()):
        for fetch in (True, False):
            rows.append(
                [
                    "spec",
                    _b(spec),
                    int(fetch),
                    gitconfig.refspec_valid(spec, fetch=fetch),
                ]
            )
    header = {
        "fixture": "gitconfig_remote_rules",
        "rules": sum(1 for r in rows if r[0] == "rule"),
        "refusals": sum(1 for r in rows if r[0] == "rule" and r[3] is not None),
        "refs": sum(1 for r in rows if r[0] == "ref"),
        "specs": sum(1 for r in rows if r[0] == "spec"),
    }
    lines = [dumps_line(header)] + [dumps_line(r) for r in rows]
    return {"gitconfig_remote_rules.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
