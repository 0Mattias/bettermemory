"""Fixtures: the roots and the workspace declaration of `identity`
(``identity_roots.jsonl``, ``identity_roots_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/identity.rs``. This pins it to
`identity`'s `_first_file_root` (and through it `urlparse` and
`unquote(errors="surrogateescape")`) and `workspace_declaration`.

One JSON object per line, keys sorted, ASCII. The first line is a header
(each kind's count). A text is a JSON string, or a list of code points
where it holds a lone surrogate. Then:

- ``["r", roots, out]``: `_first_file_root` over stand-in roots, each a
  text (its ``uri``) or null (a root without ``uri``); ``out`` the path,
  null, or ``{"raise": [class, message]}``.
- ``["w", flavour, published, env, cwd, out, reads, ncwd]``:
  `workspace_declaration` with ``published`` null (nothing published) or
  ``[path, source]`` the published caller's workspace, the environment
  ``env`` as pairs, the working directory ``cwd``; ``out`` is ``[path,
  source]``, null, or a raise; ``reads`` the environment keys asked, in
  order, and ``ncwd`` how often the working directory was read.
- ``["d", seed, count, sha256]``: `_first_file_root` over ``count`` URIs
  built by the splitmix64 rule below from ``seed``; the digest is over
  each answer's JSON line, joined by newlines.

`Path` is patched with a stand-in of the case's flavour
(``PurePosixPath`` for ``posix``, ``PureWindowsPath`` for ``nt``) on
every host: its ``cwd`` answers ``cwd`` and its ``expanduser`` is
pathlib's (3.13) over a fixed home, ``/fixture-home/me`` for POSIX
(``posixpath.expanduser`` with ``HOME`` set) and ``C:\\Profiles\\fixture``
for Windows (``ntpath.expanduser`` with ``USERPROFILE`` and ``USERNAME``
set). A ``~name`` in a POSIX case names no user on any runner. No case
spells a path whose parse `PureWindowsPath` changed across releases
(``~:x`` has no drive under 3.11).

Gates. A row whose URI holds a bracket goes into ``_python``: the
bracketed-host checks of `urlsplit` and the grammar of `ipaddress` are the
release's. The NFKC rows use only code points whose NFKC form is the same
from Unicode 14.0 to 16.0 (the crate's `unicode::UNIDATA_VERSION` is
CPython 3.13's), and every other text past ASCII is passed through
without reading the Unicode data, or is a letter or symbol `str.strip`
and `str.isspace` answer alike in every version, so the main file is not
gated. Nothing follows the platform.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
from collections.abc import Iterator
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import identity as I

from ._common import jsonl

POSIX_HOME = "/fixture-home/me"
NT_HOME = "C:\\Profiles\\fixture"


def _s(text: str) -> str | list[int]:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return [ord(c) for c in text]
    return text


def _o(text: str | None) -> Any:
    return None if text is None else _s(text)


def _raise(exc: BaseException) -> dict[str, Any]:
    return {"raise": [type(exc).__name__, _s(str(exc))]}


def _root(uri: str | None) -> Any:
    return SimpleNamespace() if uri is None else SimpleNamespace(uri=uri)


def _first(roots: list[str | None]) -> Any:
    try:
        return _o(I._first_file_root([_root(u) for u in roots]))
    except ValueError as exc:
        return _raise(exc)


A256 = "a" * 255

_URIS: list[str] = [
    "file:///work",
    "file:///work/",
    "file:///",
    "file://",
    "file:",
    "file:/abs",
    "file:rel/x",
    "file:x",
    "FILE:///up",
    "File:///Mixed",
    "file+x:///x",
    "1file:///x",
    "fi\tle:///tab",
    "fi\nle:///nl",
    "file:///a\rb",
    "\x01\x1ffile:///c0",
    " file:///space",
    "file:///trail ",
    "file:///trail\x0b",
    "file:///in\x0bside",
    "file:///in side",
    "file:///in\u00a0side",
    "http://host/x",
    "https:///x",
    "",
    "/plain/path",
    "C:/work",
    "c:\\work",
    "file:///a;b",
    "file:///a;b/c;d",
    "file:///a?q=1",
    "file:///a#frag",
    "file:///a?q#f",
    "file:///?only",
    "file:///#only",
    "file:///a%20b",
    "file:///a%2Fb",
    "file:///a%2fb",
    "file:///%41%42",
    "file:///%7e",
    "file:///%00",
    "file:///a%0Ab",
    "file:///a%09",
    "file:///%20x%20",
    "file:///%",
    "file:///%4",
    "file:///%4g",
    "file:///%%41",
    "file:///%ZZ",
    "file:///100%",
    "file:///%C3%A9",
    "file:///%c3%a9",
    "file:///%C3",
    "file:///%C3x",
    "file:///%C3\u00e9%A9",
    "file:///%E2%82",
    "file:///%E2%82%AC",
    "file:///%E2%82%ACx%E2",
    "file:///%ED%A0%80",
    "file:///%ED%9F%BF",
    "file:///%EE%80%80",
    "file:///%F0%9F%98%80",
    "file:///%F0%9F%98",
    "file:///%F4%8F%BF%BF",
    "file:///%F4%90%80%80",
    "file:///%F5%80",
    "file:///%C0%AF",
    "file:///%C1%BF",
    "file:///%E0%80%AF",
    "file:///%E0%A0%80",
    "file:///%80",
    "file:///%BF%80",
    "file:///%FE%FF",
    "file:///%FF",
    "file:///caf\u00e9",
    "file:///\U0001f600",
    "file:///\udc80x",
    "file:///\ud800",
    "file:///a\udcff%41",
    "file:///C:/work",
    "file:///C:",
    "file:///C:/",
    "file:///C",
    "file:///CC:/x",
    "file:///:/x",
    "file:///::",
    "file:////:x",
    "file:///\u00e9:/x",
    "file:///\udc80:/x",
    "file:///%43:/x",
    "file:///C%3A/x",
    "file:///C|/x",
    "file:/C:/x",
    "file:C:/x",
    "file://C:/x",
    "file://server/share/dir",
    "file://server",
    "file://server/",
    "file://server/C:/x",
    "file://localhost/x",
    "file://localhost",
    "file://LOCALHOST/x",
    "file://localhost:80/x",
    "file://user@host/x",
    "file://host:8/x",
    "file://%41/x",
    "file://h\u00e9/x",
    "file://\u00e9",
    "file://a\uff21/x",
    "file://a\uff0fb/x",
    "file://a\uff1fb/x",
    "file://a\uff03b/x",
    "file://a\uff20b/x",
    "file://a\uff1ab/x",
    "file://\udc80\uff0f/x",
    "http://a\uff0fb/x",
    "file://a\u2100/x",
    "file:///a\u200bb",
    "file:///\u0080",
    " file://a\uff0fb/x",
    "file://a\r\uff0fb/x",
    "a.b://a\uff0fb/x",
    "file://[x/y",
    "file://a]/y",
    "http://[x",
    "file:///" + A256,
    "file:///" + A256[:-1],
    "file:///" + A256 + "a",
    "file:///" + "%41" * 255,
    "file:///" + "%41" * 256,
    "file://" + "h" * 254,
    "file://" + "h" * 253 + "/",
]

_BRACKETED: list[str] = [
    "file://[::1]/x",
    "file://[::1]:8/x",
    "file://[::1]x/x",
    "file://a[::1]/x",
    "file://[v1.x]/p",
    "file://[v1.]/p",
    "file://[vz.x]/p",
    "file://[1.2.3.4]/p",
    "file://[zz]/p",
    "file://[]/p",
    "file://u@[::1]/p",
    "file://u@v@[zz]/p",
    # The last '@' splits: a first-'@' split would name 'x'.
    "file://u@[x]@[zz]/p",
    "file://[a@b]/p",
    "http://[::1]/x",
    "http://[nope]/x",
]

_MULTI: list[list[str | None]] = [
    [],
    [None],
    [None, "file:///b"],
    ["http://x/", "file:///b"],
    ["file:///%00", "file:///b"],
    ["file:///a", "file:///b"],
    ["file:///a", "file://[x/"],
    ["file://[x/", "file:///a"],
    ["file://a\uff0fb/", "file:///a"],
    ["file:///a", "file://a\uff0fb/"],
    ["file://", "", None, "file:///last"],
]

_MULTI_BRACKETED: list[list[str | None]] = [
    ["file://[zz]/", "file:///a"],
    ["file:///a", "file://[zz]/"],
]


class _Env(dict[str, str]):
    """An environment that records the keys it is asked."""

    def __init__(self, pairs: list[list[str]]) -> None:
        super().__init__(pairs)
        self.reads: list[str] = []

    def get(self, key: str, default: Any = None) -> Any:
        self.reads.append(key)
        return super().get(key, default)


def _expanduser(flavour: type, text: str) -> str:
    """`Path(text).expanduser()` of CPython 3.13 over the fixed home."""
    path = flavour(text)
    if not (path.drive or path.root) and path.parts and path.parts[0][:1] == "~":
        if flavour is PurePosixPath:
            home = {"HOME": POSIX_HOME}
            parser: Any = posixpath
        else:
            home = {"USERPROFILE": NT_HOME, "USERNAME": "fixture"}
            parser = ntpath
        with mock.patch.object(os, "environ", dict(home)):
            expanded = parser.expanduser(path.parts[0])
        if expanded[:1] == "~":
            raise RuntimeError("Could not determine home directory.")
        return str(flavour(expanded, *path.parts[1:]))
    return str(path)


def _declaration(
    nt: bool,
    published: list[Any] | None,
    env: list[list[str]],
    cwd: str | None,
) -> list[Any]:
    flavour: type = PureWindowsPath if nt else PurePosixPath
    record = {"ncwd": 0}

    class _Path:
        def __init__(self, text: str) -> None:
            self.text = text

        @staticmethod
        def cwd() -> Any:
            record["ncwd"] += 1
            if cwd is None:
                raise FileNotFoundError(2, "No such file or directory")
            return flavour(cwd)

        def expanduser(self) -> str:
            return _expanduser(flavour, self.text)

    environ = _Env(env)
    # Every row sets the caller it depends on: a row with none publishes
    # None, so a caller an earlier generator left in the process does not
    # answer `workspace_declaration` ahead of the environment.
    caller = None
    if published is not None:
        workspace = I.Workspace(path=published[0], source=published[1])
        caller = I.Caller(workspace=workspace)
    token = I._CURRENT.set(caller)
    try:
        with (
            mock.patch.object(I, "Path", _Path),
            mock.patch.object(os, "environ", environ),
        ):
            try:
                got = I.workspace_declaration()
                out: Any = None if got is None else [_s(str(got[0])), _s(got[1])]
            except RuntimeError as exc:
                out = _raise(exc)
    finally:
        I._CURRENT.reset(token)
    return [
        "w",
        "nt" if nt else "posix",
        None if published is None else [_o(published[0]), _s(published[1])],
        [[k, _s(v)] for k, v in env],
        _o(cwd),
        out,
        environ.reads,
        record["ncwd"],
    ]


_W = "BETTERMEMORY_WORKSPACE"

_DECLARATIONS: list[tuple[bool, list[Any] | None, list[list[str]], str | None]] = [
    (False, None, [], "/cwd"),
    (False, None, [], None),
    (False, None, [[_W, "/proj"]], "/cwd"),
    (False, None, [[_W, "~/proj"]], "/cwd"),
    (False, None, [[_W, "~"]], "/cwd"),
    (False, None, [[_W, "  ~/p  "]], "/cwd"),
    (False, None, [[_W, "~nosuchuser-fixture/p"]], "/cwd"),
    (False, None, [[_W, ""]], "/cwd"),
    (False, None, [[_W, "a\tb"]], "/cwd"),
    (False, None, [[_W, "rel//x/./y/"]], "/cwd"),
    (False, None, [[_W, "/p\udc80"]], "/cwd"),
    (False, None, [["BETTERMEMORY_CLIENT", "c"]], "/cwd"),
    (False, ["/pub", "header"], [[_W, "/env"]], "/cwd"),
    (False, ["~/w", "header"], [], "/cwd"),
    (False, ["~", "roots"], [], "/cwd"),
    (False, ["~/", "env"], [], "/cwd"),
    (False, ["~//x//y", "header"], [], "/cwd"),
    (False, ["~x", "header"], [], "/cwd"),
    (False, ["~~", "header"], [], "/cwd"),
    (False, ["/a/~/b", "header"], [], "/cwd"),
    (False, ["a/../b", "header"], [], "/cwd"),
    (False, ["//two", "header"], [], "/cwd"),
    (False, ["///three", "header"], [], "/cwd"),
    (False, ["./~/x", "header"], [], "/cwd"),
    (False, ["/pub", "process-cwd"], [[_W, "/env"]], "/cwd"),
    (False, [None, "header"], [], "/cwd"),
    (False, [None, "process-cwd"], [], "/cwd"),
    (False, ["/pub", "transcript"], [], "/cwd"),
    (False, ["C:\\w", "header"], [], "/cwd"),
    (True, None, [], "C:\\cwd"),
    (True, None, [[_W, "~\\proj"]], "C:\\cwd"),
    (True, None, [[_W, "~/proj"]], "C:\\cwd"),
    (True, None, [[_W, "D:/x/y"]], "C:\\cwd"),
    (True, ["~", "header"], [], "C:\\cwd"),
    (True, ["~bob\\x", "header"], [], "C:\\cwd"),
    (True, ["~fixture/x", "header"], [], "C:\\cwd"),
    (True, ["C:~x", "header"], [], "C:\\cwd"),
    (True, ["\\~\\x", "header"], [], "C:\\cwd"),
    (True, ["a:b/~", "header"], [], "C:\\cwd"),
    (True, ["/posix/style", "roots"], [], "C:\\cwd"),
    (True, ["~/a/../b", "env"], [], "C:\\cwd"),
]


# The digest: URIs built from pieces by splitmix64.
DIGEST_SEED = 20261122
DIGEST_COUNT = 3000
_PIECES: list[str] = [
    "file:",
    "FILE:",
    "http:",
    "//",
    "/",
    "localhost",
    "srv",
    "C:",
    ":",
    "%",
    "%4",
    "%41",
    "%C3",
    "%A9",
    "%E2%82",
    "%FF",
    "%00",
    "\u00e9",
    ";",
    "?",
    "#",
    " ",
    "\t",
    "\x01",
    "a",
    "\uff0f",
    "@",
]

_MASK = (1 << 64) - 1


def _splitmix(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & _MASK
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return state, z ^ (z >> 31)


def digest_uris(seed: int, count: int) -> Iterator[str]:
    """Each URI: a draw mod 9 pieces, each a draw mod the piece count."""
    state = seed
    for _ in range(count):
        state, n = _splitmix(state)
        parts = []
        for _ in range(n % 9):
            state, k = _splitmix(state)
            parts.append(_PIECES[k % len(_PIECES)])
        yield "".join(parts)


def _digest() -> list[Any]:
    lines = [
        json.dumps(
            _first([uri]), sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        for uri in digest_uris(DIGEST_SEED, DIGEST_COUNT)
    ]
    sha = hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()
    return ["d", DIGEST_SEED, DIGEST_COUNT, sha]


def _file(name: str, rows: list[list[Any]]) -> bytes:
    counts = {k: sum(1 for r in rows if r[0] == k) for k in ("r", "w", "d")}
    return jsonl([{"fixture": name, **counts}, *rows])


def generate() -> dict[str, bytes]:
    rows: list[list[Any]] = [["r", [_s(u)], _first([u])] for u in _URIS]
    rows += [["r", [_o(u) for u in roots], _first(roots)] for roots in _MULTI]
    rows += [_declaration(*case) for case in _DECLARATIONS]
    rows.append(_digest())
    return {"identity_roots.jsonl": _file("identity_roots", rows)}


def generate_python() -> dict[str, bytes]:
    rows: list[list[Any]] = [["r", [_s(u)], _first([u])] for u in _BRACKETED]
    rows += [["r", [_o(u) for u in roots], _first(roots)] for roots in _MULTI_BRACKETED]
    return {"identity_roots_python.jsonl": _file("identity_roots_python", rows)}
