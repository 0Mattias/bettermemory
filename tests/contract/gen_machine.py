"""Fixtures: the machine identity record (``machine_identity.jsonl``,
``machine_identity_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/machine.rs``. This pins it to
`machine`'s first unit: `MachineFacts.to_dict` and `from_dict`,
`_identity_from`, `identity_to_json`, `new_machine_id` and `is_clone`.

Randomness is the case's: `secrets.token_bytes` is replaced by a function
that returns the case's bytes and checks the count asked for.

The first line of each file is a header with the count. Then one row per
case, by ``f``:

- ``fd``: `MachineFacts.from_dict(a)`, ``r`` its `to_dict()` or null;
- ``id``: `_identity_from(a)`, ``r`` `identity_to_json` of it or null;
  ``a`` in both is in `_common.neutral` form (an `int` past 4,300 digits
  is written whole);
- ``cl``: `is_clone(MachineFacts(*a[0]), MachineFacts(*a[1]))`, each side
  as ``[hardware, hostname, id_file]``;
- ``nid``: `new_machine_id()` with bytes ``b`` (hex);
- ``nid_sweep``: ``n`` ids, the bytes of id ``i`` the first 17 of
  SHA-256(``b"machine-id " + i.to_bytes(4, "big")``), ``sha`` the SHA-256
  of the ids joined by newlines;
- ``cl_sweep``: `is_clone` over every pair of the facts the product of
  ``HARDWARE``, ``HOSTNAMES`` and ``ID_FILES`` gives (in that nesting,
  recorded outer), ``sha`` the SHA-256 of the answers as ``0`` and ``1``;
- ``deep`` (the Python-gated file): `from_dict` of facts whose id file
  part ``w`` (0 the device, 2 the birth time) is ``n`` containers of kind
  ``k`` (``l`` a list, ``t`` a 1-tuple, ``d`` a dict under key ``"k"``)
  around the int 1.

``r`` is the answer or ``x`` the class raised. The ``deep`` rows hold the
recursion bound of `str()`, which is the interpreter's, so they are gated
on the Python release; everything else is the same on every interpreter
and platform (the texts the walk writes with `repr` are ASCII or lone
surrogates, whose spelling no Unicode version changes).
"""

from __future__ import annotations

import hashlib
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from bettermemory import machine as m

from ._common import jsonl, neutral


@contextmanager
def _token_bytes(data: bytes) -> Iterator[None]:
    def fake(n: int) -> bytes:
        assert n == len(data), (n, len(data))
        return data

    with mock.patch("bettermemory.machine.secrets.token_bytes", fake):
        yield


@contextmanager
def _any_digits() -> Iterator[None]:
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(old)


def _call(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"x": type(exc).__name__}
    return {"r": value}


def _row(f: str, a: Any, answer: dict[str, Any]) -> dict[str, Any]:
    if "r" in answer and answer["r"] is not None and not isinstance(answer["r"], bool):
        answer = {"r": neutral(answer["r"])}
    with _any_digits():
        return {"f": f, "a": neutral(a), **answer}


def _fd(raw: Any) -> dict[str, Any]:
    def run() -> Any:
        facts = m.MachineFacts.from_dict(raw)
        return None if facts is None else facts.to_dict()

    return _row("fd", raw, _call(run))


def _id(raw: Any) -> dict[str, Any]:
    def run() -> Any:
        identity = m._identity_from(raw)
        return None if identity is None else m.identity_to_json(identity)

    return _row("id", raw, _call(run))


_BIG = 10**4300 - 1
_SURROGATE = "\ud800"

_FACTS_VALUES: list[Any] = [
    None,
    "x",
    {},
    [],
    {"hardware": "H", "hostname": "h", "id_file": ["1", "2", "3"]},
    {"hardware": "", "hostname": "", "id_file": ["", "", ""]},
    {"hardware": 1, "hostname": True, "id_file": None},
    {"hardware": None, "hostname": ["h"], "id_file": ("1", "2", "3")},
    {"hardware": "H" + _SURROGATE, "hostname": _SURROGATE},
    {"id_file": ["1", "2"]},
    {"id_file": ["1", "2", "3", "4"]},
    {"id_file": "123"},
    {"id_file": [1, 2, None]},
    {"id_file": [None, None, None]},
    {"id_file": [True, False, False]},
    {"id_file": [1.5, -0.0, 1e300]},
    {"id_file": [float("nan"), float("inf"), float("-inf")]},
    {"id_file": [[1, "a"], ("t",), {"k": [None, True]}]},
    {"id_file": [(), {}, []]},
    {"id_file": [["it's"], ['a"b'], ["\n\t\x00\x7f"]]},
    {"id_file": [[_SURROGATE], _SURROGATE, {_SURROGATE: 1}]},
    {"id_file": [_BIG, -_BIG, None]},
    {"id_file": [_BIG * 10, 1, None]},
    {"id_file": [-_BIG * 10, 1, None]},
    {"id_file": [1, 1, _BIG * 10]},
    {"id_file": [[_BIG * 10], 1, None]},
    {"id_file": ["a", "b", "c"], "id_file2": 1, 3: "x"},
    {1: "H", "hardware": "H2"},
]


def _fd_rows() -> list[dict[str, Any]]:
    return [_fd(raw) for raw in _FACTS_VALUES]


_FACTS = {"hardware": "H", "hostname": "h", "id_file": ["1", "2", None]}

_IDENTITY_VALUES: list[Any] = [
    None,
    [],
    "id",
    {},
    {"id": "A"},
    {"id": "A", "facts": None},
    {"id": "A", "facts": []},
    {"id": "A", "facts": {}},
    {"id": 1, "facts": _FACTS},
    {"id": None, "facts": _FACTS},
    {"id": "", "facts": _FACTS},
    {"id": _SURROGATE, "facts": _FACTS, "predecessor": _SURROGATE},
    {
        "id": "01ABC",
        "facts": _FACTS,
        "predecessor": "00ZZ",
        "minted": True,
        "cloned": True,
        "facts_updated": True,
        "recorded": _FACTS,
    },
    {
        "id": "A",
        "facts": _FACTS,
        "predecessor": 5,
        "minted": False,
        "cloned": None,
        "facts_updated": 0,
        "recorded": "x",
    },
    {"id": "A", "facts": {}, "minted": 1, "cloned": 0.0, "facts_updated": -0.0},
    {"id": "A", "facts": {}, "minted": 2, "cloned": float("nan"), "facts_updated": -1},
    {"id": "A", "facts": {}, "minted": "", "cloned": "0", "facts_updated": " "},
    {"id": "A", "facts": {}, "minted": [], "cloned": [0], "facts_updated": {}},
    {"id": "A", "facts": {}, "minted": {"": 0}, "cloned": (), "facts_updated": (0,)},
    {"id": "A", "facts": {}, "recorded": {}},
    {"id": "A", "facts": {}, "recorded": []},
    {"id": "A", "facts": {}, "recorded": {"id_file": [_BIG * 10, 1, None]}},
    {"id": "A", "facts": {"id_file": [_BIG * 10, 1, None]}, "recorded": {}},
    {"id": "A", "facts": None, "recorded": {"id_file": [_BIG * 10, 1, None]}},
    {"id": 0, "facts": {"id_file": [_BIG * 10, 1, None]}},
    {"facts": _FACTS, "id": "B", "extra": 1},
    {1: "A", "id": "A", "facts": {}},
]


def _id_rows() -> list[dict[str, Any]]:
    return [_id(raw) for raw in _IDENTITY_VALUES]


HARDWARE: list[str | None] = [None, "", "A", "B"]
HOSTNAMES: list[str | None] = [None, "", "h", "g"]
ID_FILES: list[tuple[str, str, str | None] | None] = [
    None,
    ("1", "2", None),
    ("1", "2", "3"),
    ("1", "3", None),
]


def _all_facts() -> list[m.MachineFacts]:
    return [
        m.MachineFacts(h, n, f) for h in HARDWARE for n in HOSTNAMES for f in ID_FILES
    ]


def _side(facts: m.MachineFacts) -> list[Any]:
    return [facts.hardware, facts.hostname, facts.id_file]


def _cl_rows() -> list[dict[str, Any]]:
    pairs = [
        (("A", "h", None), ("A", "g", ("1", "2", None))),
        (("A", "h", None), ("B", "h", None)),
        (("", "h", ("1", "2", None)), ("B", "g", ("1", "3", None))),
        (("", "h", ("1", "2", None)), ("B", "g", ("1", "2", None))),
        ((None, "h", None), (None, "g", ("1", "2", None))),
        ((None, "h", None), (None, "h", ("1", "2", None))),
        ((None, None, None), (None, None, None)),
        ((None, None, ("1", "2", "3")), (None, "", ("1", "2", None))),
        (("A", None, None), (None, "g", ("9", "9", "9"))),
        (("A", "h", ("1", "2", "3")), ("A", "g", ("4", "5", "6"))),
    ]
    rows = []
    for a, b in pairs:
        answer = m.is_clone(m.MachineFacts(*a), m.MachineFacts(*b))
        rows.append({"f": "cl", "a": neutral([list(a), list(b)]), "r": answer})
    facts = _all_facts()
    answers = "".join("1" if m.is_clone(r, s) else "0" for r in facts for s in facts)
    rows.append(
        {
            "f": "cl_sweep",
            "n": len(answers),
            "sha": hashlib.sha256(answers.encode("ascii")).hexdigest(),
        }
    )
    return rows


def _new_id(data: bytes) -> str:
    with _token_bytes(data):
        return m.new_machine_id()


_SWEEP = 4096


def sweep_bytes(i: int) -> bytes:
    return hashlib.sha256(b"machine-id " + i.to_bytes(4, "big")).digest()[:17]


def _nid_rows() -> list[dict[str, Any]]:
    fixed = [
        bytes(17),
        b"\xff" * 17,
        b"\x00" * 16 + b"\x01",
        b"\x00" * 16 + b"\x1f",
        b"\x00" * 16 + b"\x20",
        b"\x80" + b"\x00" * 16,
        b"\x03" + b"\xff" * 16,
        b"\xfc" + b"\x00" * 16,
        bytes(range(17)),
        bytes(range(0xEF, 0x100)),
    ]
    rows: list[dict[str, Any]] = [
        {"f": "nid", "b": b.hex(), "r": _new_id(b)} for b in fixed
    ]
    ids = "\n".join(_new_id(sweep_bytes(i)) for i in range(_SWEEP))
    rows.append(
        {
            "f": "nid_sweep",
            "n": _SWEEP,
            "sha": hashlib.sha256(ids.encode("ascii")).hexdigest(),
        }
    )
    return rows


def _nest(kind: str, n: int) -> Any:
    value: Any = 1
    for _ in range(n):
        if kind == "l":
            value = [value]
        elif kind == "t":
            value = (value,)
        else:
            value = {"k": value}
    return value


def _in_fresh_thread(raw: dict[str, Any]) -> dict[str, Any]:
    # `str()`'s recursion bound is the C recursion budget left to it, which
    # depends on the C frames below the caller (a test runner, a
    # `functools.partial`). A new thread starts with the whole budget, so
    # the answer is the same however the generator is reached.
    out: dict[str, Any] = {}

    def read() -> None:
        out.update(_call(lambda: m.MachineFacts.from_dict(raw)))

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    return out


def _deep_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind in ("l", "t", "d"):
        for where in (0, 2):
            for n in (9997, 9998, 9999, 10000):
                parts: list[Any] = [1, 1, 1]
                parts[where] = _nest(kind, n)
                raw: dict[str, Any] = {"id_file": parts}
                answer = _in_fresh_thread(raw)
                if "r" in answer:
                    answer = {"r": "ok"}
                rows.append({"f": "deep", "k": kind, "w": where, "n": n, **answer})
    return rows


def _file(rows: list[dict[str, Any]], name: str) -> bytes:
    return jsonl([{"fixture": name, "count": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    rows = _fd_rows() + _id_rows() + _cl_rows() + _nid_rows()
    return {"machine_identity.jsonl": _file(rows, "machine_identity")}


def generate_python() -> dict[str, bytes]:
    return {
        "machine_identity_python.jsonl": _file(_deep_rows(), "machine_identity_python")
    }


# -- unit 2: the facts and the files (``machine_facts*.jsonl``) ----------------
#
# Rows, by ``f``:
#
# - ``rid``/``rrec``: `_read_id`/`_read_record` of a file holding the bytes
#   ``b`` (hex), or of ``k``: ``missing`` (no file), ``dir`` (a folder),
#   ``nul`` (a path holding a NUL); ``r`` the answer (``rrec`` in neutral
#   form) or ``x`` the class raised;
# - ``rrec_deep``: `_read_record` of ``n`` nested lists inside an object.
#   The row of 100,000 lists is in ``machine_facts_python.jsonl`` (gated on
#   the Python release): whether `json.loads` raises RecursionError there
#   is the interpreter's and the system's (3.14 on Linux measures the C
#   stack and reads it); the row of 50 is in ``machine_facts.jsonl``;
# - ``dar``: `_darwin_hardware` with ``gethostuuid`` returning 0 and the
#   bytes ``g`` (hex), returning 1 (``g`` ``"fail"``) or raising
#   AttributeError (``"raise"``), and ``ioreg`` printing ``o`` (null: the
#   spawn raises OSError); ``asked`` whether ``ioreg`` ran;
# - ``lin``: `_linux_hardware` over files holding ``files`` (hex; null
#   missing, ``"dir"`` a folder);
# - ``win``: `_windows_hardware` with MachineGuid ``a`` (neutral; absent:
#   OSError);
# - ``host``: `current_hostname` with `socket.gethostname` giving ``a``
#   (null: raising OSError);
# - ``hw``: `read_hardware_id` under ``sys.platform`` ``p`` with each
#   platform's read replaced by one naming the platform; ``r`` the answer;
# - ``memo``: the memo: read, read, clear, read; ``fills`` the reads made;
# - ``fo``: `_file_object` with `os.stat` giving ``st`` (``ns`` an
#   ``st_birthtime_ns``, ``s`` an ``st_birthtime`` as `float.hex`) or
#   raising ``x``.
#
# The platform file's header records whether `os.stat_result` has
# ``st_birthtime_ns`` and ``st_birthtime``; its row ``real`` checks
# `_file_object` of a real file against `os.stat`.

import os  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

_HEADER = (m._ID_HEADER + "\n").encode()
_GOOD = "0123456789ABCDEFGHJKMNPQRS"

_ID_FILES: list[bytes] = [
    b"",
    _HEADER,
    _HEADER + _GOOD.encode(),
    _HEADER + _GOOD.encode() + b"\n",
    _HEADER + _GOOD.encode() + b"\r\n",
    _HEADER + _GOOD.encode() + b"\nextra\n",
    _HEADER.replace(b"\n", b"\r\n") + _GOOD.encode(),
    _HEADER.replace(b"\n", b"\r") + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\r\r" + _GOOD.encode(),
    _HEADER + b"\n" + _GOOD.encode(),
    b"  \t" + m._ID_HEADER.encode() + b" \x0c\n \t" + _GOOD.encode() + b" \x1f",
    b"\x1c" + m._ID_HEADER.encode() + b"\x1d\n" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x0b" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x0c" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x1c" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x1d" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x1e" + _GOOD.encode(),
    m._ID_HEADER.encode() + b"\x1f" + _GOOD.encode(),
    b"\xef\xbb\xbf" + _HEADER + _GOOD.encode(),
    _HEADER + b"\xff" + _GOOD.encode(),
    _HEADER + _GOOD.encode() + b"\n\xc3",
    _HEADER + b"\xed\xa0\x80",
    _HEADER + b"\xc0\xaf",
    _HEADER + _GOOD.encode() + b"\x00",
    _HEADER + b"\x00" + _GOOD.encode(),
    _HEADER + _GOOD[:-1].encode(),
    _HEADER + (_GOOD + "T").encode(),
    _HEADER + b"Z" * 26,
    _HEADER + b"0" * 26,
    _HEADER + (_GOOD[:-1] + "I").encode(),
    _HEADER + (_GOOD[:-1] + "L").encode(),
    _HEADER + (_GOOD[:-1] + "O").encode(),
    _HEADER + (_GOOD[:-1] + "U").encode(),
    _HEADER + (_GOOD[:-1] + "a").encode(),
    _HEADER + (_GOOD[:-1] + "@").encode(),
    _HEADER + (_GOOD[:-1] + "[").encode(),
    _HEADER + (_GOOD[:-1] + "/").encode(),
    _HEADER + (_GOOD[:-1] + ":").encode(),
    _HEADER + (_GOOD[:13] + " " + _GOOD[13:25]).encode(),
    _HEADER + _GOOD.lower().encode(),
    b"bettermemory machine id v2\n" + _GOOD.encode(),
    b"bettermemory machine id v1x\n" + _GOOD.encode(),
    b"Bettermemory machine id v1\n" + _GOOD.encode(),
    m._ID_HEADER.encode(),
    (_GOOD + "\n").encode() * 2,
    _HEADER + "０".encode() + _GOOD[1:].encode(),
]

_ID_FILES_UNICODE: list[bytes] = [
    m._ID_HEADER.encode() + "\x85".encode() + _GOOD.encode(),
    m._ID_HEADER.encode() + " ".encode() + _GOOD.encode(),
    m._ID_HEADER.encode() + " ".encode() + _GOOD.encode(),
    "　".encode() + _HEADER + _GOOD.encode() + " ".encode(),
    _HEADER + " ".encode() + _GOOD.encode() + " ".encode(),
    _HEADER + _GOOD.encode() + "​".encode(),
    _HEADER + _GOOD.encode() + "﻿".encode(),
    _HEADER + _GOOD.encode() + "᠎".encode(),
]

_RECORDS: list[bytes] = [
    b"",
    b"{}",
    b'{"id": "A", "facts": {}}',
    b'{"a": 1}\r\n',
    b'{"a": "x\ry"}',
    b'{"a": "x\r\ny"}',
    b'{"a": "\\r"}',
    b"[]",
    b"null",
    b"1",
    b'"s"',
    b"{",
    b'{"a": NaN, "b": -Infinity}',
    b'{"a": 1, "a": 2}',
    b"\xef\xbb\xbf{}",
    b'{"a": "\xff"}',
    b'{"a": "\xc3\xa9"}',
    b'{"a": "\\ud800"}',
    b'{"a": ' + b"9" * 4301 + b"}",
    b'{"a": ' + b"9" * 4300 + b"}",
    b" \t\n{} \n",
    b"{} x",
    b"\x0c{}",
]


def _call_x(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"r": fn()}
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"x": type(exc).__name__}


def _special(root: Path, kind: str) -> Path:
    if kind == "dir":
        folder = root / "folder"
        folder.mkdir(exist_ok=True)
        return folder
    if kind == "nul":
        return root / "a\x00b"
    return root / "missing"


def _read_rows(
    root: Path, f: str, fn: Callable[[Path], Any], files: list[bytes]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = root / "file"
    for data in files:
        path.write_bytes(data)
        answer = _call_x(lambda: fn(path))
        if f == "rrec" and "r" in answer and answer["r"] is not None:
            answer = {"r": neutral(answer["r"])}
        rows.append({"f": f, "b": data.hex(), **answer})
    if files is not _ID_FILES_UNICODE:
        for kind in ("missing", "dir", "nul"):
            rows.append(
                {"f": f, "k": kind, **_call_x(lambda: fn(_special(root, kind)))}
            )
    return rows


def _deep_record_rows(root: Path, depths: tuple[int, ...]) -> list[dict[str, Any]]:
    rows = []
    path = root / "deep"
    for n in depths:
        path.write_text('{"a": ' + "[" * n + "]" * n + "}", encoding="utf-8")
        answer = _call_x(lambda: m._read_record(path))
        if "r" in answer:
            answer = {"r": "ok" if answer["r"] is not None else None}
        rows.append({"f": "rrec_deep", "n": n, **answer})
    return rows


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _darwin(g: str | None, out: str | None) -> dict[str, Any]:
    asked: list[bool] = []

    def gethostuuid(buffer: Any, wait: Any) -> int:
        if g == "raise":
            raise AttributeError("gethostuuid")
        if g == "fail" or g is None:
            return 1
        for i, b in enumerate(bytes.fromhex(g)):
            buffer[i] = b
        return 0

    def run(*_a: Any, **_k: Any) -> _Completed:
        asked.append(True)
        if out is None:
            raise OSError("no ioreg")
        return _Completed(out)

    lib = types.SimpleNamespace(gethostuuid=gethostuuid)
    with (
        mock.patch("sys.platform", "darwin"),
        mock.patch("ctypes.CDLL", lambda _name: lib),
        mock.patch("subprocess.run", run),
    ):
        answer = m._darwin_hardware()
    return {"f": "dar", "g": g, "o": out, "asked": bool(asked), "r": answer}


_IOREG = '"IOPlatformUUID" = "'
_DARWIN_CASES: list[tuple[str | None, str | None]] = [
    (bytes(16).hex(), None),
    (bytes(range(16)).hex(), None),
    ((b"\xff" * 16).hex(), None),
    (bytes(range(0xA0, 0xB0)).hex(), "unused"),
    ("fail", None),
    ("raise", None),
    ("fail", ""),
    ("fail", '  | "IOPlatformUUID" = "ab-cd"\n'),
    ("raise", 'x "IOPlatformUUID" = "Z"'),
    ("fail", _IOREG + '"'),
    ("fail", _IOREG + '" ' + _IOREG + 'second"'),
    ("fail", _IOREG + "unterminated"),
    ("fail", _IOREG + 'a\nb"' + _IOREG + 'c"'),
    ("fail", '"IOPlatformUUID"  = "x"'),
    ("fail", '"IOPlatformuuid" = "x"'),
    ("fail", '""IOPlatformUUID" = "x"'),
    ("fail", _IOREG[:-1] + _IOREG + 'y"'),
    ("fail", _IOREG + 'é\U0001f600"tail'),
]


def _dar_rows() -> list[dict[str, Any]]:
    rows = []
    for g, out in _DARWIN_CASES:
        rows.append(_darwin(g, out))
    return rows


_LINUX_CASES: list[list[bytes | str | None]] = [
    [None, None],
    [b"abc\n", b"def\n"],
    [None, b"def\n"],
    [b"", b"def"],
    [b" \t\n\x0b\x0c\r\x1c\x1d\x1e\x1f", b"def"],
    [b"\xc3\xa9", b"def"],
    [b"\x80", None],
    ["dir", b" x y \n"],
    [b"a\r\nb\r", None],
    [b"\x00", None],
    [b"\x85x", None],
    [b"", b""],
]


def _lin_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for i, files in enumerate(_LINUX_CASES):
        paths = []
        for j, data in enumerate(files):
            path = root / f"lin{i}_{j}"
            if data == "dir":
                path.mkdir()
            elif isinstance(data, bytes):
                path.write_bytes(data)
            paths.append(str(path))
        with mock.patch.object(m, "_LINUX_MACHINE_ID", tuple(paths)):
            answer = _call_x(m._linux_hardware)
        spelled = [d.hex() if isinstance(d, bytes) else d for d in files]
        rows.append({"f": "lin", "files": spelled, **answer})
    with mock.patch.object(m, "_LINUX_MACHINE_ID", (str(root / "a\x00b"),)):
        rows.append({"f": "lin", "files": ["nul"], **_call_x(m._linux_hardware)})
    return rows


_MISSING = object()
_WIN_VALUES: list[Any] = [
    _MISSING,
    None,
    "",
    "8a1c2b3d-0000-4e5f-9a8b-7c6d5e4f3a2b",
    " ",
    0,
    5,
    -1,
    True,
    False,
    [],
    ["a", 1],
    "\ud800",
]


def _win(value: Any) -> dict[str, Any]:
    class _Key:
        def __enter__(self) -> _Key:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def open_key(*_a: Any) -> _Key:
        if value is _MISSING:
            raise OSError("no key")
        return _Key()

    winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=0,
        KEY_READ=1,
        KEY_WOW64_64KEY=2,
        OpenKey=open_key,
        QueryValueEx=lambda _k, _n: (value, 1),
    )
    with (
        mock.patch("sys.platform", "win32"),
        mock.patch.dict(sys.modules, {"winreg": winreg}),
    ):
        answer = _call_x(m._windows_hardware)
    row: dict[str, Any] = {"f": "win", **answer}
    if "r" in answer and answer["r"] is not None:
        row["r"] = neutral(answer["r"])
    if value is not _MISSING:
        row["a"] = neutral(value)
    return row


_HOSTS: list[str | None] = [None, "", "host", "h.local", "été", "a\udcff"]


def _host(name: str | None) -> dict[str, Any]:
    def gethostname() -> str:
        if name is None:
            raise OSError("no name")
        return name

    with mock.patch("socket.gethostname", gethostname):
        answer = m.current_hostname()
    return {"f": "host", "a": neutral(name), "r": neutral(answer)}


def _hw_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fakes: dict[str, Any] = {
        "_darwin_hardware": lambda: "D",
        "_windows_hardware": lambda: "W",
        "_linux_hardware": lambda: "L",
    }
    for platform in (
        "darwin",
        "win32",
        "linux",
        "linux2",
        "linuxish",
        "freebsd14",
        "cygwin",
        "Darwin",
        "win",
        "aix",
        "",
    ):
        m._clear_hardware()
        with (
            mock.patch("sys.platform", platform),
            mock.patch.multiple("bettermemory.machine", **fakes),
        ):
            rows.append({"f": "hw", "p": platform, "r": m.read_hardware_id()})
    fills: list[str | None] = []

    def counted() -> str | None:
        fills.append(None)
        return None

    m._clear_hardware()
    answers = []
    with (
        mock.patch("sys.platform", "linux"),
        mock.patch.object(m, "_linux_hardware", counted),
    ):
        answers.append(m.read_hardware_id())
        answers.append(m.read_hardware_id())
        m._clear_hardware()
        answers.append(m.read_hardware_id())
    m._clear_hardware()
    rows.append({"f": "memo", "r": answers, "fills": len(fills)})
    return rows


_STATS: list[dict[str, Any] | str] = [
    "OSError",
    "FileNotFoundError",
    "ValueError",
    {"dev": 1, "ino": 2},
    {"dev": 0, "ino": 0, "ns": 0},
    {"dev": 16777232, "ino": 2**64 - 1, "ns": 1_700_000_000_123_456_789},
    {"dev": 2**64 - 1, "ino": 1, "ns": -1},
    {"dev": 1, "ino": 2, "s": 1700000000.123456789},
    {"dev": 1, "ino": 2, "s": 0.1},
    {"dev": 1, "ino": 2, "s": 0.3},
    {"dev": 1, "ino": 2, "s": 1e-10},
    {"dev": 1, "ino": 2, "s": -1.5},
    {"dev": 1, "ino": 2, "s": 1.8446744073709552e19},
    {"dev": 1, "ino": 2, "s": 1e30},
    {"dev": 1, "ino": 2, "s": 1234567890.000000001},
    {"dev": 1, "ino": 2, "s": 0.0},
    {"dev": 1, "ino": 2, "s": -0.0},
]


def _fo(st: dict[str, Any] | str) -> dict[str, Any]:
    def stat(_path: Any) -> Any:
        if isinstance(st, str):
            raise {
                "OSError": OSError,
                "FileNotFoundError": FileNotFoundError,
                "ValueError": ValueError,
            }[st]("stat")
        fields: dict[str, Any] = {"st_dev": st["dev"], "st_ino": st["ino"]}
        if "ns" in st:
            fields["st_birthtime_ns"] = st["ns"]
        if "s" in st:
            fields["st_birthtime"] = st["s"]
        return types.SimpleNamespace(**fields)

    with mock.patch("os.stat", stat):
        answer = _call_x(lambda: m._file_object(Path("x")))
    if "r" in answer and answer["r"] is not None:
        answer = {"r": list(answer["r"])}
    spelled: Any = st
    if isinstance(st, dict) and "s" in st:
        spelled = {**st, "s": float(st["s"]).hex()}
    return {"f": "fo", "st": spelled, **answer}


def _facts_rows() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = _read_rows(root, "rid", m._read_id, _ID_FILES)
        rows += _read_rows(root, "rrec", m._read_record, _RECORDS)
        rows += _deep_record_rows(root, (50,))
        rows += _lin_rows(root)
    rows += _dar_rows()
    rows += [_win(v) for v in _WIN_VALUES]
    rows += [_host(h) for h in _HOSTS]
    rows += _hw_rows()
    rows += [_fo(st) for st in _STATS]
    return rows


def generate_facts() -> dict[str, bytes]:
    return {"machine_facts.jsonl": _file(_facts_rows(), "machine_facts")}


def generate_facts_python() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        rows = _deep_record_rows(Path(tmp), (100_000,))
    return {"machine_facts_python.jsonl": _file(rows, "machine_facts_python")}


def generate_facts_unicode() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        rows = _read_rows(Path(tmp), "rid", m._read_id, _ID_FILES_UNICODE)
    return {"machine_facts_unicode.jsonl": _file(rows, "machine_facts_unicode")}


def _real_row() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "machine-id"
        path.write_bytes(b"x")
        st = os.stat(path)
        found = m._file_object(path)
    assert found is not None
    ns = getattr(st, "st_birthtime_ns", None)
    seconds = getattr(st, "st_birthtime", None)
    return {
        "f": "real",
        "dev": found[0] == str(st.st_dev),
        "ino": found[1] == str(st.st_ino),
        "birth": found[2] is not None,
        "birth_from_ns": ns is not None and found[2] == str(ns),
        "birth_from_s": ns is None
        and seconds is not None
        and found[2] == str(int(seconds * 1_000_000_000)),
    }


def generate_facts_platform() -> dict[str, bytes]:
    rows = [_real_row()]
    header = {
        "fixture": "machine_facts_platform",
        "count": len(rows),
        "has_birthtime_ns": hasattr(os.stat_result, "st_birthtime_ns"),
        "has_birthtime": hasattr(os.stat_result, "st_birthtime"),
    }
    return {"machine_facts_platform.jsonl": jsonl([header, *rows])}
