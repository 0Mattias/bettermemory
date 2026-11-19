"""Fixtures: the checkout token's text (``checkout_token.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/checkout_token.rs``. This pins it to
`checkout_token`'s leaves: `random_crockford`, `mint`, `valid`,
`Lineage.read`, `lineage`, `successor`, `succeeds`, `_link`, `render`,
`parse`, `token_path`, `_cut_short` and `_malformed_detail`.

Randomness is the case's: ``random._urandom`` (what `secrets.randbits`
reads through `SystemRandom.getrandbits`) is replaced by a function that
returns the case's bytes and checks the count asked for. The bytes come
from a seeded generator, so every run writes the same file.
`token_path` runs with the module's ``Path`` replaced by `PurePosixPath`
or `PureWindowsPath`, so the file does not depend on the host.

The first line is a header with the count. Then one row per call, by
``f``:

- ``rc``: `random_crockford` of ``n`` with bytes ``b`` (hex); ``mint``:
  `mint` with bytes ``b``;
- ``valid``, ``lineage`` (``a`` is ``[token, key]``), ``lread``
  (`Lineage.read`): arguments in `_common.neutral` form;
- ``link``, ``succ`` (`successor`, bytes ``b``), ``sc`` (`succeeds`, token
  ``a`` in neutral form): the lineage ``l`` as three texts, or null;
- ``render``, ``parse``, ``detail`` (`_malformed_detail` with
  `_cut_short` as ``cs``): the text ``a`` (``a_cps`` where it holds a
  surrogate);
- ``path``: `str(token_path(a))` under flavour ``fl`` (``p`` or ``w``).

``r`` is the answer (a text as `_common.output`, bytes as hex) or ``x``
the class raised.

The Windows paths are drives, roots and relative forms, with no UNC share
and no device path; they come out the same under CPython 3.9, 3.13 and
3.15. Everything else is integers, ASCII and BLAKE2b, the same on every
interpreter and platform, so the file is not gated.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from unittest import mock

from bettermemory import checkout_token as ct

from ._common import jsonl, neutral, output, text_field

_SEED = 20261119


@contextmanager
def _urandom(data: bytes) -> Iterator[None]:
    def fake(n: int) -> bytes:
        assert n == len(data), (n, len(data))
        return data

    with mock.patch.object(random, "_urandom", fake):
        yield


def _bytes(rng: random.Random, n: int) -> bytes:
    return bytes(rng.randrange(256) for _ in range(n))


def _needed(length: int) -> int:
    return (5 * length + 7) // 8


def _call(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"x": type(exc).__name__}
    if isinstance(value, bytes):
        return {"r": value.hex()}
    if isinstance(value, str):
        return {"r": output(value)}
    return {"r": value}


def _lin(lin: ct.Lineage | None) -> Any:
    if lin is None:
        return None
    return [neutral(lin.token), neutral(lin.device), neutral(lin.inode)]


def _random_rows(rng: random.Random) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n in [0, 1, 2, 3, 7, 8, 13, 16, 25, 26, 27, 64, 100, 257]:
        patterns = [
            b"\x00" * _needed(n),
            b"\xff" * _needed(n),
            bytes(range(_needed(n))),
        ] + [_bytes(rng, _needed(n)) for _ in range(4)]
        for data in patterns:
            with _urandom(data):
                rows.append(
                    {
                        "f": "rc",
                        "n": n,
                        "b": data.hex(),
                        **_call(lambda: ct.random_crockford(n)),
                    }
                )
    for n in [-1, -5]:
        with _urandom(b""):
            rows.append(
                {"f": "rc", "n": n, "b": "", **_call(lambda: ct.random_crockford(n))}
            )
    for _ in range(20):
        data = _bytes(rng, 17)
        with _urandom(data):
            rows.append({"f": "mint", "b": data.hex(), **_call(ct.mint)})
    for data in (b"\x00" * 17, b"\xff" * 17):
        with _urandom(data):
            rows.append({"f": "mint", "b": data.hex(), **_call(ct.mint)})
    return rows


_TOK = "0123456789ABCDEFGHJKMNPQRS"
_TOK2 = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"


def _values() -> list[Any]:
    return [
        None,
        True,
        0,
        26,
        1.5,
        "",
        _TOK,
        _TOK2,
        "0" * 26,
        "0" * 25,
        "0" * 27,
        _TOK.lower(),
        _TOK[:-1] + "I",
        _TOK[:-1] + "L",
        _TOK[:-1] + "O",
        _TOK[:-1] + "U",
        _TOK[:-1] + "u",
        _TOK[:-1] + " ",
        _TOK + "\n",
        "\n" + _TOK[1:],
        _TOK[:-1] + "\uff21",
        _TOK[:-1] + "\u0661",
        _TOK[:-1] + "\ud800",
        "\ufeff" + _TOK[1:],
        [_TOK],
        (_TOK,),
        {_TOK: 1},
    ]


def _keys() -> list[Any]:
    return [
        None,
        "12",
        ["1", "2"],
        ("1", "2"),
        ("1", "2", "3"),
        ["1", "2", None],
        ("1",),
        (),
        ("-1", "-0"),
        ("007", "0"),
        ("0", "-"),
        ("", "1"),
        ("1", ""),
        ("--1", "1"),
        ("1-", "1"),
        ("+1", "1"),
        (" 1", "1"),
        ("1 ", "1"),
        ("1\n", "1"),
        ("1", "1\n"),
        ("\u0661", "1"),
        ("1", "\uff11"),
        ("1", 1),
        (1, "1"),
        ("1.0", "1"),
        ("99999999999999999999999999999999999999999999", "18446744073709551615"),
        [["1"], "2"],
        {"1": "2"},
        ("1", "2", object),
    ]


_LOW_BYTE_TOKENS = ["\u0141" + "0" * 25, "\u0130" * 26, "0" * 25 + "\u0141"]


def _lineage_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for token in _values():
        for key in [("1", "2"), ["16777220", "123456"]]:
            got = ct.lineage(token, key)
            rows.append(
                {"f": "lineage", "a": [neutral(token), neutral(key)], "r": _lin(got)}
            )
    for key in _keys():
        if key is not None and isinstance(key, tuple) and object in key:
            continue
        got = ct.lineage(_TOK, key)
        rows.append(
            {"f": "lineage", "a": [neutral(_TOK), neutral(key)], "r": _lin(got)}
        )
    for token in _values():
        rows.append({"f": "valid", "a": neutral(token), "r": ct.valid(token)})
    # Code points whose low byte is a Crockford character ('A', '0').
    for token in _LOW_BYTE_TOKENS:
        rows.append({"f": "valid", "a": neutral(token), "r": ct.valid(token)})
        got = ct.lineage(token, ("1", "2"))
        rows.append(
            {"f": "lineage", "a": [neutral(token), neutral(("1", "2"))], "r": _lin(got)}
        )
    reads: list[Any] = [
        None,
        _TOK,
        [_TOK, "1", "2"],
        (_TOK, "1", "2"),
        ct.Lineage(_TOK, "1", "2"),
        [_TOK, "1", "2", "3"],
        [_TOK, "1"],
        [_TOK.lower(), "1", "2"],
        [_TOK, "1", "x"],
        [_TOK, 1, "2"],
        [_TOK, "-1", "-0"],
        {"a": 1, "b": 2, "c": 3},
        [[_TOK], "1", "2"],
        ["1", "2", _TOK],
    ]
    for value in reads:
        rows.append(
            {"f": "lread", "a": neutral(value), "r": _lin(ct.Lineage.read(value))}
        )
    return rows


def _lineages() -> list[ct.Lineage]:
    return [
        ct.Lineage(_TOK, "1", "2"),
        ct.Lineage(_TOK, "2", "1"),
        ct.Lineage(_TOK, "12", ""),
        ct.Lineage(_TOK, "1", "2"[::-1]),
        ct.Lineage(_TOK2, "16777220", "8618311"),
        ct.Lineage(_TOK2, "-1", "-0"),
        ct.Lineage("0" * 26, "0", "0"),
        ct.Lineage(_TOK, "9" * 200, "1" * 300),
        ct.Lineage(_TOK, "1" * 50, "2" * 51),
        # Built directly: the NamedTuple takes any text.
        ct.Lineage("", "", ""),
        ct.Lineage("x", "\u00e9", "\U0001f600"),
        ct.Lineage(_TOK, "\ud800", "1"),
        ct.Lineage(_TOK.lower(), "1", "2"),
    ]


def _link_rows(rng: random.Random) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    nonces = ["0" * 13, "Z" * 13, _TOK[:13], "", "A", "x" * 200, "\u00e9" * 13]
    for lin in _lineages():
        for nonce in nonces:
            rows.append(
                {
                    "f": "link",
                    "l": _lin(lin),
                    **text_field("n", nonce),
                    **_call(lambda: ct._link(lin, nonce)),
                }
            )
        for _ in range(3):
            data = _bytes(rng, 9)
            with _urandom(data):
                rows.append(
                    {
                        "f": "succ",
                        "l": _lin(lin),
                        "b": data.hex(),
                        **_call(lambda: ct.successor(lin)),
                    }
                )
            with _urandom(data):
                try:
                    made: Any = ct.successor(lin)
                except UnicodeEncodeError:
                    made = None
            if made is None:
                continue
            for token in [
                made,
                made.lower(),
                made[:13] + _TOK[13:],
                _TOK[:13] + made[13:],
            ]:
                rows.append(
                    {
                        "f": "sc",
                        "a": neutral(token),
                        "l": _lin(lin),
                        **_call(lambda: ct.succeeds(token, lin)),
                    }
                )
            other = ct.Lineage(lin.token, lin.inode, lin.device)
            rows.append(
                {
                    "f": "sc",
                    "a": neutral(made),
                    "l": _lin(other),
                    **_call(lambda: ct.succeeds(made, other)),
                }
            )
        for token in [None, lin.token, _TOK, _TOK2, 7, "short"]:
            rows.append(
                {
                    "f": "sc",
                    "a": neutral(token),
                    "l": _lin(lin),
                    **_call(lambda: ct.succeeds(token, lin)),
                }
            )
    for token in [None, _TOK]:
        rows.append(
            {
                "f": "sc",
                "a": neutral(token),
                "l": None,
                **_call(lambda: ct.succeeds(token, None)),
            }
        )
    return rows


_H = ct.TOKEN_HEADER


def _texts() -> list[str]:
    out = [
        "",
        "\n",
        "\r\n",
        f"{_H}\n{_TOK}\n",
        f"{_H}\n{_TOK}",
        f"{_H}\r\n{_TOK}\r\n",
        f"{_H}\r\n{_TOK}\r",
        f"{_H}\n{_TOK}\r",
        f"{_H}\r\r\n{_TOK}\n",
        f"{_H}\n{_TOK}\r\r\n",
        f"{_H}\n{_TOK}\n\n",
        f"{_H}\n{_TOK}\n\r\n",
        f"\n{_H}\n{_TOK}\n",
        f"{_H} \n{_TOK}\n",
        f" {_H}\n{_TOK}\n",
        f"{_H}\n{_TOK} \n",
        f"{_H}\n{_TOK.lower()}\n",
        f"{_H}\n{_TOK[:-1]}\n",
        f"{_H}\n{_TOK}0\n",
        f"{_H}\n{_TOK}\n{_TOK}\n",
        f"{_H}\r{_TOK}\n",
        f"{_H}\n\n",
        f"{_H}\n",
        _H,
        _H[:5],
        "b",
        "x",
        f"{_H}\n0",
        f"{_H}\n{_TOK[:25]}",
        f"{_H}\n{'Z' * 25}",
        f"{_H}\n{_TOK[:25]}\n",
        f"{_H}\n{_TOK[:24]}u",
        f"{_H}\n{_TOK[:10]}\r",
        f"{_H}\r\n",
        f"{_H}\r\n{_TOK[:4]}",
        f"{_H}\n{_TOK}",
        f"{_H}\n\u0661{_TOK[1:]}\n",
        f"{_H}\n{_TOK[:-1]}\ud800\n",
        f"{_H}\n{_TOK[:5]}\ud800",
        "\ufeff" + f"{_H}\n{_TOK}\n",
        f"{_H}\u2028{_TOK}\n",
        f"{_H}\n{_TOK}\u0085",
        f"{_H}\x00\n{_TOK}\n",
        "\x00",
    ]
    for i in range(len(_H) + 1):
        out.append(_H[:i])
    for i in range(27):
        out.append(f"{_H}\n{'7' * i}")
    for token in _LOW_BYTE_TOKENS:
        out.append(f"{_H}\n{token}")
        out.append(f"{_H}\n{token[:5]}")
    return out


def _text_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for text in _texts():
        got = ct.parse(text)
        rows.append(
            {
                "f": "parse",
                **text_field("a", text),
                "r": None if got is None else output(got),
            }
        )
        rows.append(
            {
                "f": "detail",
                **text_field("a", text),
                "cs": ct._cut_short(text),
                "r": output(ct._malformed_detail(text)),
            }
        )
    for token in [_TOK, "", "x", "\u00e9", "\ud800", "a\nb", _TOK + "\x7f", "\x80"]:
        rows.append(
            {"f": "render", **text_field("a", token), **_call(lambda: ct.render(token))}
        )
    return rows


_POSIX_PATHS = [
    "",
    ".",
    "./",
    "..",
    "/",
    "//",
    "///",
    "/repo/.git",
    "/repo/.git/",
    "/repo//.git//",
    "//net/x",
    "///x",
    "repo/./.git",
    "repo/../.git",
    "a\\b",
    "C:\\x",
    "~/r",
    " ",
    "\u00e9/.git",
]

_WINDOWS_PATHS = [
    # No UNC or device path: the golden guard reads a doubled backslash as a
    # host path (`gen_store_files.host_fact`).
    *(p for p in _POSIX_PATHS if not p.startswith("//")),
    "C:",
    "C:\\",
    "C:/",
    "C:x",
    "c:\\repo\\.git",
    "C:\\repo\\.git\\",
    "a:b",
    "\\x",
    "/x",
    "x:",
]


def _path_rows(flavour: str) -> list[dict[str, Any]]:
    cls: Any = PurePosixPath if flavour == "p" else PureWindowsPath
    paths = _POSIX_PATHS if flavour == "p" else _WINDOWS_PATHS
    rows: list[dict[str, Any]] = []
    with mock.patch.object(ct, "Path", cls):
        for path in paths:
            rows.append(
                {"f": "path", "fl": flavour, "a": path, "r": str(ct.token_path(path))}
            )
    return rows


def _file(rows: list[dict[str, Any]], name: str) -> bytes:
    return jsonl([{"fixture": name, "count": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    rng = random.Random(_SEED)
    rows = (
        _random_rows(rng)
        + _lineage_rows()
        + _link_rows(rng)
        + _text_rows()
        + _path_rows("p")
        + _path_rows("w")
    )
    return {"checkout_token.jsonl": _file(rows, "checkout_token")}
