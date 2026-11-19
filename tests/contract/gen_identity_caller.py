"""Fixtures: a request's caller as `identity` resolves it
(``identity_caller.jsonl``, ``identity_caller_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/identity.rs``. This pins it to
`identity`'s `_strip_format_characters`, `_clean`, `_json_workspace`,
`actor_matches`, `Actor.is_empty`, `Actor.to_record`,
`Workspace.declared`, `registry_key`, `resolve` (and through it
`_headers_of`, `_carries_http`, `_client_info_of`, `_principal_of`,
`_request_id_of`, `_roots_of`), `bind`, `bind_transcript`, `publish`,
`reset`, `current`, `current_actor` and `current_harness_session`.

One JSON object per line, keys sorted, ASCII. A text is a JSON string or
``{"r": [[piece, n], ...]}``, each piece repeated n times, joined. Every
dict whose order the reference keeps is written as a list of pairs.

- ``{"f": "clean", "v", "sf", "o"}``: `_clean(v, strip_format=sf)`; ``v``
  is a text or ``{"o": kind}`` for a value that is not a ``str``.
- ``{"f": "strip", "v", "o"}``: `_strip_format_characters`.
- ``{"f": "jws", "v", "o"}``: `_json_workspace`.
- ``{"f": "actor", "a", "o"}``: an actor's `is_empty`, `to_record` (pairs)
  and `registry_key`; ``a`` is its fields and its sources as pairs.
- ``{"f": "matches", "a", "c", "m", "o"}``: `actor_matches`.
- ``{"f": "declared", "w", "o"}``: `Workspace(path, source).declared`.
- ``{"f": "resolve", "q", "e", "cwd", "o", "env", "ncwd"}``: `resolve`
  over a stand-in request ``q`` (null for none; else ``http``, ``h`` the
  header pairs, ``ci`` the client info's name and version or null,
  ``p`` the principal (``{"raise": 1}`` when the binding raises),
  ``rid``, ``root`` (``{"o": "weak"}`` when the session cannot be keyed
  weakly), ``nosession``), the environment ``e`` as pairs, the working
  directory ``cwd`` (null when it cannot be read); ``env`` is the keys the
  environment was asked, in order, and ``ncwd`` how often the working
  directory was. ``"nt": true`` where the working directory is a Windows
  path (below).
- ``{"f": "seq", "s", "o"}``: steps over the published caller (``bind``,
  ``transcript``, ``publish``, ``reset``), each answered by `current`,
  `current_actor`'s record and `current_harness_session`.

`resolve` names the workspace ``str(Path.cwd())`` where nothing else does
(identity.py, `resolve`), and ``str`` of a concrete `Path` is the host's
spelling: on Windows `Path("/cwd")` is a `WindowsPath`, ``\\cwd``. So
`Path.cwd` is patched to answer a pure path of the case's flavour, built
from ``cwd``: `PurePosixPath`, or `PureWindowsPath` for an ``nt`` case,
which is the reference's answer on Windows. Both are made on every host,
so the file is the same on every platform, and its ``nt`` spellings
(``\\``, a drive) read alike from 3.11 to 3.15.

``identity_caller_unicode.jsonl`` holds what `_clean` does with each code
point in the middle and at the end of a value, as runs over the whole
range: it reads `unicodedata.category` and `str.isspace`, so it is gated
on the Unicode version. The other file's texts use only code points
whose category and spacing are the same from Unicode 14.0 to 16.0.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterator
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import identity as I

from ._common import jsonl

_LONG = 40


def sp(text: str) -> Any:
    """A text as the file spells it."""
    if len(text) <= _LONG:
        return text
    runs: list[list[Any]] = []
    for ch in text:
        if runs and runs[-1][0] == ch:
            runs[-1][1] += 1
        else:
            runs.append([ch, 1])
    return {"r": runs}


def _opt(text: str | None) -> Any:
    return None if text is None else sp(text)


class _NotStr:
    def __init__(self, kind: str) -> None:
        self.kind = kind


_OBJECTS: dict[str, Any] = {"none": None, "int": 7, "bytes": b"abc", "list": ["a"]}


def _value(v: Any) -> Any:
    if isinstance(v, _NotStr):
        return _OBJECTS[v.kind]
    return v


def _spell_value(v: Any) -> Any:
    if isinstance(v, _NotStr):
        return {"o": v.kind}
    return sp(v)


# --- the leaves ------------------------------------------------------------------

_CLEAN_TEXTS: list[Any] = [
    "",
    " ",
    "claude-code",
    "  padded  ",
    "two words",
    "tab\there",
    "nl\nhere",
    "\tlead",
    "trail\r\n",
    "\x1c\x1d\x1e\x1fsep\x1f",
    "a\x1cb",
    "bell\x07",
    "del\x7f",
    "\x00",
    "a\x00b",
    "\x85next",
    "a\x85b",
    "nb\xa0sp",
    "\xa0lead",
    "\u2028",
    "a\u2028b",
    "\u3000wide\u3000",
    "zw\u200bsp",
    "\u200b",
    "\u200b\u200c\u200d\u2060\ufeff",
    "\ufeffbom",
    " \u200b x \u200b ",
    "\u200b \u200b",
    "\u200ba\u3000",
    "\u202eevil\u202c",
    "lrm\u200erlm\u200f",
    "\u2066iso\u2069",
    "soft\xadhyphen",
    "\U000e0041tag\U000e007f",
    "\u061calm",
    "caf\xe9",
    "\u00e9\u0301",
    "\U0001f600",
    "\U0001f468\u200d\U0001f4bb",
    "sur\udc80rogate",
    "\udc80",
    "\ud83d\ude00",
    "/work/caf\xe9",
    "C:\\work",
    "a" * 256,
    "a" * 257,
    " " + "a" * 256 + " ",
    "\xe9" * 256,
    "\xe9" * 257,
    "\u200b" + "\xe9" * 256,
    "\u200b" * 600 + "x",
    "\u200b" * 1024,
    "\u200b" * 1023 + "x",
    "\u200b" * 1024 + "x",
    " " * 2000 + "\u200b" * 1024 + "x" + " " * 3,
    "\u200b" * 1025,
    "\xe9" * 1024 + " ",
    "\xe9" * 1025,
    "a" * 5000,
    "x" + "\u200b" * 300,
    "\u200b" + "a" * 256,
    "\u200b" + "a" * 257,
    _NotStr("none"),
    _NotStr("int"),
    _NotStr("bytes"),
    _NotStr("list"),
]

_STRIP_TEXTS = [
    "",
    "plain",
    "\u200b\u200c\u200d\u2060\ufeff\xad\u061c\u200e\u200f",
    "a\u202ab\u202bc\u202cd\u202de\u202ef\u2066g\u2067h\u2068i\u2069j",
    "\U000e0001\U000e0020\U000e007f",
    "\udc80\u200b\udfff",
    "\u0600\u06dd\u070f\u110bd",
    " \u200b ",
]

_JWS: list[Any] = [
    "",
    '"/work"',
    '"/caf\\u00e9"',
    '"\\ud83d\\ude00"',
    '"\\udc80"',
    '"a\\u0000b"',
    '""',
    '" "',
    '" /padded "',
    '"tab\\there"',
    '"\\u200b"',
    '  "/ws"  \n',
    '\ufeff"/x"',
    '"/x" "/y"',
    '["/x"]',
    '{"p": "/x"}',
    "null",
    "7",
    "NaN",
    '"/x',
    "'/x'",
    "[" * 769 + "]" * 769,
    "[" * 1000,
    '"' + "a" * 256 + '"',
    '"' + "a" * 257 + '"',
    '"' + "\\u00e9" * 256 + '"',
    '"' + "\\u00e9" * 255 + '"',
    '"' + "\\ud83d\\ude00" * 128 + '"',
    '"' + "\\ud83d\\ude00" * 129 + '"',
    '"' + "\\ud83d\\ude00" * 127 + "\\u00e9" * 2 + '"',
    " " * 1281 + '"/x"' + " " * 253,
    " " * 1281 + '"/x"' + " " * 254,
    '"/x"' + " " * 1534,
    '"/x"' + " " * 1535,
    _NotStr("none"),
    _NotStr("int"),
]


def _actor(a: dict[str, Any]) -> I.Actor:
    return I.Actor(
        **{
            k: a.get(k)
            for k in ("client", "client_version", "model", "principal", "session")
        },
        sources=dict(a.get("sources", [])),
    )


def _spell_actor(a: dict[str, Any] | None) -> Any:
    if a is None:
        return None
    out: dict[str, Any] = {k: sp(v) for k, v in a.items() if k != "sources"}
    out["sources"] = [[k, v] for k, v in a.get("sources", [])]
    return out


def _pairs(d: dict[str, Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for k, v in d.items():
        out.append([k, _pairs(v) if isinstance(v, dict) else _opt(v)])
    return out


_ACTORS: list[dict[str, Any]] = [
    {},
    {"sources": [["client", "header"]]},
    {"client": "c"},
    {"client_version": "1"},
    {"model": "m"},
    {"principal": "p"},
    {"session": "s"},
    {"client": "c", "sources": [["client", "header"]]},
    {"client": "c", "sources": [["client", "env"]]},
    {"client": "c", "sources": [["client", "client-info"]]},
    {"client": "c", "sources": [["client", "transcript"]]},
    {"model": "m", "sources": [["model", "header"]]},
    {"model": "m", "sources": [["model", "env"]]},
    {"session": "s", "sources": [["session", "transport"]]},
    {"session": "s", "sources": [["session", "header"]]},
    {"session": "s", "sources": [["session", "transcript"]]},
    {"session": "s", "sources": [["session", "Header"]]},
    {"principal": "p", "sources": [["principal", "header"]]},
    {
        "client": "c",
        "client_version": "2",
        "model": "m",
        "principal": "p",
        "session": "s",
        "sources": [
            ["session", "transport"],
            ["model", "header"],
            ["client_version", "header"],
            ["client", "header"],
        ],
    },
    {"client": "a|model=b", "sources": [["client", "header"]]},
    {
        "client": "a",
        "model": "b",
        "sources": [["client", "header"], ["model", "header"]],
    },
    {"principal": "x|session=y"},
    {"principal": "x", "session": "y", "sources": [["session", "header"]]},
    {"client": "", "sources": [["client", "header"]]},
    {"principal": "", "session": "", "sources": [["session", "transport"]]},
    {"sources": [["session", "transport"], ["client", "header"]]},
    {"client": "c\udc80", "principal": "\u200bp", "sources": [["client", "header"]]},
    {"model": "m", "sources": [["other", "x"], ["model", "header"]]},
    {"client": "a" * 300, "sources": [["client", "header"]]},
]

_FILTERS: list[tuple[str | None, str | None]] = [
    (None, None),
    ("c", None),
    (None, "m"),
    ("c", "m"),
    ("C", None),
    ("", None),
    (None, ""),
    ("c", "x"),
    ("a|model=b", None),
]


def _leaves() -> Iterator[dict[str, Any]]:
    for v in _CLEAN_TEXTS:
        for sf in (True, False):
            yield {
                "f": "clean",
                "v": _spell_value(v),
                "sf": sf,
                "o": _opt(I._clean(_value(v), strip_format=sf)),
            }
    for t in _STRIP_TEXTS:
        yield {"f": "strip", "v": sp(t), "o": sp(I._strip_format_characters(t))}
    for v in _JWS:
        yield {
            "f": "jws",
            "v": _spell_value(v),
            "o": _opt(I._json_workspace(_value(v))),
        }
    for a in _ACTORS:
        actor = _actor(a)
        yield {
            "f": "actor",
            "a": _spell_actor(a),
            "o": [
                actor.is_empty(),
                _pairs(actor.to_record()),
                _opt(I.registry_key(actor)),
            ],
        }
    for b in [None, *_ACTORS[:13], _ACTORS[18], _ACTORS[20]]:
        for c, m in _FILTERS:
            matched = None if b is None else _actor(b)
            yield {
                "f": "matches",
                "a": _spell_actor(b),
                "c": c,
                "m": m,
                "o": I.actor_matches(matched, client=c, model=m),
            }
    for path in (None, "", "/w", "\u200b"):
        for source in (
            "process-cwd",
            "header",
            "env",
            "roots",
            "transcript",
            "",
            "Process-cwd",
        ):
            yield {
                "f": "declared",
                "w": [path, source],
                "o": I.Workspace(path=path, source=source).declared,
            }


# --- resolution --------------------------------------------------------------------


class _Env(dict[str, str]):
    """An environment that records the keys it is asked."""

    def __init__(self, pairs: list[list[str]]) -> None:
        super().__init__(pairs)
        self.asked: list[str] = []

    def get(self, key: str, default: Any = None) -> Any:
        self.asked.append(key)
        return super().get(key, default)


class _Session:
    client_params: Any


class _Unkeyable:
    __slots__ = ("client_params",)


def _request(q: dict[str, Any] | None) -> tuple[Any, Any]:
    """The stand-in request context and the roots table it is read with."""
    roots: weakref.WeakKeyDictionary[Any, str | None] = weakref.WeakKeyDictionary()
    if q is None:
        return None, roots
    request = SimpleNamespace(headers=dict(q.get("h", []))) if q.get("http") else None
    session: Any = None
    if not q.get("nosession"):
        root = q.get("root", "absent")
        session = _Unkeyable() if isinstance(root, dict) else _Session()
        ci = q.get("ci")
        info = None if ci is None else SimpleNamespace(name=ci[0], version=ci[1])
        session.client_params = SimpleNamespace(client_info=info)
        if root != "absent" and not isinstance(root, dict):
            roots[session] = root
    ctx = SimpleNamespace(session=session, request_id=q.get("rid"))
    if request is not None:
        ctx.request = request
    return ctx, roots


def _caller_out(c: I.Caller) -> list[Any]:
    a = c.actor
    return [
        [
            _opt(a.client),
            _opt(a.client_version),
            _opt(a.model),
            _opt(a.principal),
            _opt(a.session),
        ],
        [[k, v] for k, v in a.sources.items()],
        [_opt(c.workspace.path), c.workspace.source],
        _opt(c.request_id),
        _opt(c.harness_session),
    ]


class _Cwd:
    """`Path.cwd`: a pure path of the case's flavour built from `cwd`, the
    same on every host (a concrete `Path` is the host's), or
    FileNotFoundError where `cwd` is None."""

    def __init__(self, cwd: str | None, nt: bool = False) -> None:
        self.cwd = cwd
        self.flavour = PureWindowsPath if nt else PurePosixPath
        self.n = 0

    def __call__(self) -> PurePath:
        self.n += 1
        if self.cwd is None:
            raise FileNotFoundError(2, "gone")
        return self.flavour(self.cwd)


def _principal(q: dict[str, Any] | None) -> Any:
    p = None if q is None else q.get("p")

    def binding(ctx: Any) -> Any:
        if isinstance(p, dict):
            raise RuntimeError("broken auth context")
        return p

    return binding


def _run_resolve(
    q: dict[str, Any] | None, e: list[list[str]], cwd: str | None, nt: bool = False
) -> dict[str, Any]:
    ctx, roots = _request(q)
    env = _Env(e)
    where = _Cwd(cwd, nt)
    with (
        mock.patch.object(I, "_ROOTS", roots),
        mock.patch.object(Path, "cwd", where),
        mock.patch("mcp.server.request_state.authenticated_principal", _principal(q)),
    ):
        caller = I.resolve(ctx, environ=env)
    return {"o": _caller_out(caller), "env": env.asked, "ncwd": where.n}


def _spell_q(q: dict[str, Any] | None) -> Any:
    if q is None:
        return None
    out = dict(q)
    if "h" in q:
        out["h"] = [[sp(k), sp(v)] for k, v in q["h"]]
    if "ci" in q and q["ci"] is not None:
        out["ci"] = [
            _spell_value(x) if isinstance(x, _NotStr) else _opt(x) for x in q["ci"]
        ]
    if isinstance(q.get("p"), str):
        out["p"] = sp(q["p"])
    return out


def _resolve_q(q: dict[str, Any] | None) -> dict[str, Any] | None:
    if q is None:
        return None
    out = dict(q)
    if "ci" in q and q["ci"] is not None:
        out["ci"] = [_value(x) for x in q["ci"]]
    return out


H = "x-bettermemory-"
_FULL_ENV = [
    ["BETTERMEMORY_CLIENT", "env-client"],
    ["BETTERMEMORY_CLIENT_VERSION", "env-1"],
    ["BETTERMEMORY_MODEL", "env-model"],
    ["BETTERMEMORY_WORKSPACE", "/env/ws"],
    ["CLAUDE_CODE_SESSION_ID", "env-harness"],
]
_FULL_HEADERS = [
    [H + "client", "h-client"],
    [H + "client-version", "h-1"],
    [H + "model", "h-model"],
    [H + "workspace", "/h/ws"],
    [H + "workspace-json", '"/h/json"'],
    ["mcp-session-id", "mcp-s"],
    [H + "session", "h-s"],
    [H + "harness-session", "h-harness"],
]

_RESOLVES: list[tuple[Any, list[list[str]], str | None]] = [
    (None, [], "/cwd"),
    (None, [], None),
    (None, _FULL_ENV, "/cwd"),
    (None, [["BETTERMEMORY_CLIENT", " \u200bx\u200b "]], "/cwd"),
    (None, [["BETTERMEMORY_CLIENT", "\u200b"], ["BETTERMEMORY_MODEL", "a\tb"]], "/cwd"),
    (None, [["BETTERMEMORY_WORKSPACE", "\u200b/zw"]], "/cwd"),
    (None, [["BETTERMEMORY_WORKSPACE", " "]], None),
    (None, [["BETTERMEMORY_WORKSPACE", "/a\udc80"]], "/cwd"),
    (None, [["bettermemory_client", "lower"]], "/cwd"),
    ({}, [], "/cwd"),
    ({}, _FULL_ENV, "/cwd"),
    ({"http": True}, _FULL_ENV, "/cwd"),
    ({"http": True, "h": _FULL_HEADERS}, _FULL_ENV, "/cwd"),
    ({"http": False, "h": _FULL_HEADERS}, _FULL_ENV, "/cwd"),
    ({"http": True, "h": [[k.upper(), v] for k, v in _FULL_HEADERS]}, [], "/cwd"),
    ({"http": True, "h": [[H + "wor\u212aspace", "/kelvin"]]}, [], "/cwd"),
    (
        {"http": True, "h": [[H + "CLIENT", "first"], [H + "client", "second"]]},
        [],
        "/cwd",
    ),
    (
        {"http": True, "h": [[H + "client", "first"], [H + "Client", "second"]]},
        [],
        "/cwd",
    ),
    ({"http": True, "h": [[H + "client", "\u200b"]]}, [], "/cwd"),
    ({"http": True, "h": [[H + "client", " \u202ename\u202c "]]}, [], "/cwd"),
    ({"http": True, "h": [[H + "workspace", "\u200b/zw "]]}, [], "/cwd"),
    (
        {
            "http": True,
            "h": [[H + "workspace", "a\tb"], [H + "workspace-json", '"a\\tb"']],
        },
        [],
        "/cwd",
    ),
    (
        {"http": True, "h": [[H + "workspace", ""], [H + "workspace-json", '"/j"']]},
        [],
        "/cwd",
    ),
    ({"http": True, "h": [[H + "workspace-json", "bad"]], "root": "/root"}, [], "/cwd"),
    (
        {"http": True, "h": [[H + "workspace-json", "bad"]]},
        [["BETTERMEMORY_WORKSPACE", "/e"]],
        "/cwd",
    ),
    ({"http": False}, [["BETTERMEMORY_WORKSPACE", "/e"]], "/cwd"),
    ({"root": "/root"}, [], "/cwd"),
    ({"root": None}, [], "/cwd"),
    ({"root": {"o": "weak"}}, [], "/cwd"),
    ({"root": "/root"}, [["BETTERMEMORY_WORKSPACE", "\t"]], "/cwd"),
    ({"root": "/root", "nosession": True}, [], "/cwd"),
    ({"root": "/root"}, [["BETTERMEMORY_WORKSPACE", "/e"]], None),
    ({"ci": ["info", "9"]}, [], "/cwd"),
    ({"ci": ["info", "9"]}, [["BETTERMEMORY_CLIENT", "e"]], "/cwd"),
    ({"ci": ["info", "9"]}, [["BETTERMEMORY_CLIENT_VERSION", "e1"]], "/cwd"),
    ({"ci": ["info", "9"]}, _FULL_ENV, "/cwd"),
    ({"ci": [_NotStr("int"), _NotStr("none")]}, [], "/cwd"),
    ({"ci": [" \u200b ", "1\t2"]}, [], "/cwd"),
    ({"ci": ["\u200bzw", "\u200b9"]}, [], "/cwd"),
    ({"ci": ["info", "9"], "nosession": True}, [], "/cwd"),
    (
        {"http": True, "h": [[H + "client-version", "hv"]], "ci": ["info", "9"]},
        [],
        "/cwd",
    ),
    ({"http": True, "h": [[H + "client", "hc"]], "ci": ["info", "9"]}, [], "/cwd"),
    (
        {"http": True, "h": [["mcp-session-id", " "], [H + "session", "h-s"]]},
        [],
        "/cwd",
    ),
    (
        {"http": True, "h": [["mcp-session-id", "\u200b"], [H + "session", "\u200b"]]},
        [],
        "/cwd",
    ),
    ({"h": [[H + "session", "s"]]}, [["CLAUDE_CODE_SESSION_ID", "env-h"]], "/cwd"),
    (
        {"http": True, "h": [[H + "harness-session", "\ta"]]},
        [["CLAUDE_CODE_SESSION_ID", "e"]],
        "/cwd",
    ),
    (
        {"http": False, "h": [[H + "harness-session", "\ta"]]},
        [["CLAUDE_CODE_SESSION_ID", "e"]],
        "/cwd",
    ),
    ({"p": "principal\u200b"}, [], "/cwd"),
    ({"p": " \u202ep "}, [], "/cwd"),
    ({"p": ""}, [], "/cwd"),
    ({"p": {"raise": 1}}, [], "/cwd"),
    ({"p": "a" * 257}, [], "/cwd"),
    ({"rid": 7}, [], "/cwd"),
    ({"rid": "r-1"}, [], "/cwd"),
    ({"rid": ""}, [], "/cwd"),
    ({"http": True, "h": [[H + "client", "a" * 256]]}, [], "/cwd"),
    (
        {"http": True, "h": [[H + "client", "\xe9" * 1025]]},
        [["BETTERMEMORY_CLIENT", "e"]],
        "/cwd",
    ),
    (
        {
            "http": True,
            "h": [[H + "workspace-json", '"' + "\\ud83d\\ude00" * 129 + '"']],
        },
        [],
        "/cwd",
    ),
    # `str(PurePosixPath(...))` respells: separators collapse, `.` and a
    # trailing separator go.
    (None, [], "/w//x/./"),
]

#: Resolutions whose working directory is a `PureWindowsPath`, as
#: `Path.cwd()` answers on Windows: `/` is spelled `\`, a drive is kept.
#: A path a header, the environment or a root declares is not respelled.
_RESOLVES_NT: list[tuple[Any, list[list[str]], str | None]] = [
    (None, [], "/cwd"),
    (None, [], "C:/w//x/."),
    (None, [], "c:"),
    (None, [], "a/b"),
    (None, [], None),
    (None, [["BETTERMEMORY_WORKSPACE", "/e/f"]], "/cwd"),
    ({"root": "/root"}, [], "/cwd"),
]


def _resolves() -> Iterator[dict[str, Any]]:
    cases = [(*c, False) for c in _RESOLVES] + [(*c, True) for c in _RESOLVES_NT]
    for q, e, cwd, nt in cases:
        out = _run_resolve(_resolve_q(q), e, cwd, nt)
        yield {
            "f": "resolve",
            "q": _spell_q(q),
            "e": [[k, sp(v)] for k, v in e],
            "cwd": cwd,
            **({"nt": True} if nt else {}),
            **out,
        }


# --- the published caller ---------------------------------------------------------

_SEQS: list[list[dict[str, Any]]] = [
    [],
    [{"do": "bind", "q": None, "e": [], "cwd": "/cwd"}],
    [
        {
            "do": "bind",
            "q": {"http": True, "h": [[H + "client", "c"]]},
            "e": [],
            "cwd": "/cwd",
        }
    ],
    [
        {
            "do": "bind",
            "q": {"http": True, "h": [[H + "harness-session", "hs"]]},
            "e": [],
            "cwd": "/c",
        }
    ],
    [
        {
            "do": "bind",
            "q": {"http": True, "h": [[H + "model", "m"]]},
            "e": [],
            "cwd": "/c",
        },
        {"do": "bind", "q": None, "e": [], "cwd": "/c"},
    ],
    [
        {"do": "transcript", "sid": "t-1", "model": "opus", "client": "claude-code"},
    ],
    [{"do": "transcript", "sid": None, "model": None, "client": None}],
    [{"do": "transcript", "sid": " \u200b ", "model": "\u202em", "client": "a\tb"}],
    [
        {
            "do": "transcript",
            "sid": "t",
            "model": None,
            "client": "c",
            "e": [["BETTERMEMORY_WORKSPACE", "/e"], ["CLAUDE_CODE_SESSION_ID", "h"]],
        }
    ],
    [{"do": "transcript", "sid": "t", "model": "m", "client": "c", "cwd": None}],
    [
        {"do": "publish", "q": {"http": True, "h": [[H + "client", "a"]]}},
        {"do": "publish", "q": {"http": True, "h": [[H + "client", "b"]]}},
        {"do": "reset", "t": 1},
        {"do": "reset", "t": 0},
    ],
    [
        {"do": "publish", "q": {"http": True, "h": [[H + "harness-session", "x"]]}},
        {"do": "bind", "q": {"http": True}, "e": [], "cwd": "/c"},
        {"do": "reset", "t": 0},
    ],
]


def _seq(steps: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    tokens: list[Any] = []
    base = I._CURRENT.set(None)
    try:
        for step in steps:
            env = _Env(step.get("e", []))
            where = _Cwd(step.get("cwd", "/cwd"))
            with (
                mock.patch.object(I, "_ROOTS", weakref.WeakKeyDictionary()),
                mock.patch.object(Path, "cwd", where),
                mock.patch("os.environ", env),
                mock.patch(
                    "mcp.server.request_state.authenticated_principal", _principal(None)
                ),
            ):
                if step["do"] == "bind":
                    ctx, _ = _request(step["q"])
                    I.bind(
                        None if ctx is None else SimpleNamespace(request_context=ctx)
                    )
                elif step["do"] == "transcript":
                    I.bind_transcript(
                        session_id=step["sid"],
                        model=step["model"],
                        client=step["client"],
                    )
                elif step["do"] == "publish":
                    ctx, _ = _request(step["q"])
                    tokens.append(I.publish(I.resolve(ctx, environ={})))
                else:
                    I.reset(tokens[step["t"]])
            current = I.current()
            actor = I.current_actor()
            out.append(
                [
                    None if current is None else _caller_out(current),
                    None if actor is None else _pairs(actor.to_record()),
                    _opt(I.current_harness_session()),
                    env.asked,
                    where.n,
                ]
            )
    finally:
        I._CURRENT.reset(base)
    return out


def _spell_step(step: dict[str, Any]) -> dict[str, Any]:
    out = dict(step)
    if "q" in step:
        out["q"] = _spell_q(step["q"])
    return out


def _seqs() -> Iterator[dict[str, Any]]:
    for steps in _SEQS:
        yield {"f": "seq", "s": [_spell_step(s) for s in steps], "o": _seq(steps)}


# --- the code point sweep ---------------------------------------------------------


def _class(text: str, sf: bool) -> int:
    got = I._clean(text, strip_format=sf)
    if got is None:
        return 0
    if got == text:
        return 1
    return 2


def _runs(sf: bool, where: str) -> list[list[int]]:
    runs: list[list[int]] = []
    for cp in range(0x110000):
        ch = chr(cp)
        text = "a" + ch + "b" if where == "mid" else "a" + ch
        c = _class(text, sf)
        if not runs or runs[-1][1] != c:
            runs.append([cp, c])
    return runs


def generate() -> dict[str, bytes]:
    """``identity_caller.jsonl``: not gated."""
    rows = [*_leaves(), *_resolves(), *_seqs()]
    return {
        "identity_caller.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }


def generate_unicode() -> dict[str, bytes]:
    """``identity_caller_unicode.jsonl``: gated on the Unicode version."""
    rows = [
        {"f": "sweep", "sf": sf, "at": at, "runs": _runs(sf, at)}
        for sf in (True, False)
        for at in ("mid", "end")
    ]
    return {
        "identity_caller_unicode.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }
