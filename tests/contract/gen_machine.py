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
