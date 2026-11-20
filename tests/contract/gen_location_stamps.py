"""Fixtures: `location_stamps.py` whole (``location_stamps.jsonl``,
``location_stamps_platform.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_stamps.rs``. This pins
`in_ticks`, `kept_in_ticks`, `stat_each`, `capture_ticks`, `_drvfs`,
`_device_name`, `_init_stamps`, `filetime_ns` and `basic_info_ns`.

The machine is the case's: ``_WINDOWS`` and ``_LINUX`` are patched, the
module's ``sys`` is a stand-in naming the platform `_device_name` and
`basic_info_ns` read, ``_MOUNTINFO`` is a scratch file holding the case's
table (absent, or a folder, where the case says), the module's ``os`` is
a stand-in whose ``stat`` answers from the case's table and whose
``path`` is ``posixpath`` or ``ntpath``, and on Windows `basic_info_ns`
(for `_init_stamps`) or `_basic_info_calls` (for `basic_info_ns`) answer
from the case. No row holds a path of the machine.

Integers are decimal strings. A byte string is ``=`` and itself where it
is printable ASCII with no backslash, else its hex; a table is a list
of pieces, each such a string or ``[byte, count]`` for a run of 32 or
more of one byte. A raised answer is
``["err", class, message]``. Rows of ``location_stamps.jsonl``, after a
header holding the counts:

- ``["in", times, answer]``: `in_ticks`.
- ``["kept", windows, linux, table, device, times, answer]``:
  `kept_in_ticks`; table null for no file, ``"dir"`` for a folder.
- ``["drvfs", table, device, answer]``: `_drvfs` on Linux.
- ``["stat", paths, stats, answer]``: `stat_each`; a stat is
  ``[ino, dev, mtime, ctime, size]``, ``"value"`` (ValueError) or absent
  (FileNotFoundError); the answer each inode or null.
- ``["init", nt, windows, git_dir, linked, stats, basic, answer]``:
  `_init_stamps`; ``basic`` maps a path to its four FILETIMEs or
  ``"fail"``; the answer the stamps.
- ``["cap", windows, linux, table, git_st, git_key, read, stamps,
  answer]``: `capture_ticks`; a stat ``[dev, mtime, ctime]``; the key a
  list of null and 1.
- ``["ft", ticks, answer]``: `filetime_ns`.
- ``["basic", scenario, answer]``: `basic_info_ns`; ``"posix"`` off
  Windows, ``"nocalls"``, ``"nohandle"``, ``"invalid"``, ``"queryfail"``,
  or four FILETIMEs read.
- ``["digest", what, seed, count, sha256]``: a sweep (`_sweep`).

A device of the drvfs rows is below 256, whose name is ``0:N`` under
both Linux's and macOS's ``os.major``, so the file holds on either; the
generator is POSIX only (Windows' ``os`` has no ``major``).

``location_stamps_platform.jsonl`` holds `_device_name` over `_DEVICES`
for each way `os.major` names a device (`encoding`): a header listing
the sections, then each section's ``["section", name, provenance]`` line
and its ``["dev", device, answer]`` rows. This interpreter's section is
made here and the others are carried from the committed file as they
were recorded: macOS's by the pinned run, Linux's in containers, under a
CPython built against glibc and one built against musl (`_RECORDED`,
each made by `_OBSERVE`). So a run on Linux remakes its own section.
The file is gated on the platform and on the Python release.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import platform
import posixpath
import sys
import sysconfig
import tempfile
import types
from collections.abc import Callable, Iterator
from functools import partial
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import location_stamps as ls

from ._common import jsonl

_MASK = (1 << 64) - 1


def _b(data: bytes) -> str:
    if all(0x20 <= c < 0x7F and c != 0x5C for c in data):
        return "=" + data.decode("ascii")
    return data.hex()


def _t(data: bytes) -> list[Any]:
    """A table: pieces, each a byte string (`_b`) or ``[byte, count]`` for
    a run of 32 or more of one byte."""
    out: list[Any] = []
    plain = b""
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 32:
            if plain:
                out.append(_b(plain))
                plain = b""
            out.append([_b(data[i : i + 1]), j - i])
        else:
            plain += data[i:j]
        i = j
    if plain:
        out.append(_b(plain))
    return out


def _err(exc: BaseException) -> list[str]:
    return ["err", type(exc).__name__, str(exc)]


def splitmix(seed: int) -> Iterator[int]:
    """SplitMix64 from `seed`: the sweeps' inputs, which the native test
    regenerates by the same rule."""
    state = seed & _MASK
    while True:
        state = (state + 0x9E3779B97F4A7C15) & _MASK
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        yield z ^ (z >> 31)


def _sweep_times(gen: Iterator[int]) -> list[int]:
    """One `in_ticks` input: up to four times, each a multiple of 1, 100
    or 10**9 of a number in [-1000, 1000]."""
    out = []
    for _ in range(next(gen) % 5):
        x = next(gen)
        out.append(((x >> 8) % 2001 - 1000) * (1, 100, 10**9)[x % 3])
    return out


def _sweep() -> list[list[Any]]:
    rows: list[list[Any]] = []
    gen = splitmix(1)
    count = 20000
    answers = []
    for _ in range(count):
        v = next(gen)
        ticks = v - (1 << 64) if v >= 1 << 63 else v
        answers.append(str(ls.filetime_ns(ticks)))
    sha = hashlib.sha256("\n".join(answers).encode("ascii")).hexdigest()
    rows.append(["digest", "filetime", 1, count, sha])
    gen = splitmix(2)
    answers = [str(int(ls.in_ticks(_sweep_times(gen)))) for _ in range(count)]
    sha = hashlib.sha256("\n".join(answers).encode("ascii")).hexdigest()
    rows.append(["digest", "in_ticks", 2, count, sha])
    return rows


class _Stat:
    def __init__(self, *fields: int) -> None:
        ino, dev, mtime, ctime, size = fields
        self.st_ino = ino
        self.st_dev = dev
        self.st_mtime_ns = mtime
        self.st_ctime_ns = ctime
        self.st_size = size


def _nul_message() -> str:
    """What the real os.stat raises for a str path holding a NUL."""
    try:
        os.stat("x\x00y")
    except ValueError as e:
        return str(e)
    raise AssertionError("os.stat accepted a NUL")


def _fake_os(stats: dict[str, Any], nt: bool) -> types.SimpleNamespace:
    def stat(path: str) -> _Stat:
        got = stats.get(path)
        if got == "value":
            raise ValueError(_nul_message())
        if got is None:
            raise FileNotFoundError(2, "No such file or directory")
        return _Stat(*(int(v) for v in got))

    return types.SimpleNamespace(stat=stat, path=ntpath if nt else posixpath)


@contextmanager
def _machine(
    scratch: Path, *, windows: bool, linux: bool, table: bytes | str | None
) -> Iterator[None]:
    path = scratch / "mountinfo"
    if path.is_dir():
        path.rmdir()
    elif path.exists():
        path.unlink()
    if table == "dir":
        path.mkdir()
    elif isinstance(table, bytes):
        path.write_bytes(table)
    plat = "win32" if windows else "linux" if linux else "darwin"
    with (
        mock.patch.object(ls, "_WINDOWS", windows),
        mock.patch.object(ls, "_LINUX", linux),
        mock.patch.object(ls, "_MOUNTINFO", str(path)),
        mock.patch.object(ls, "sys", types.SimpleNamespace(platform=plat)),
    ):
        yield


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (OverflowError, ValueError, IndexError) as exc:
        return _err(exc)


def _line(dev: str, fstype: str, source: str, options: str, extra: str = "") -> bytes:
    return f"36 35 {dev} / /mnt/c rw,noatime {extra}- {fstype} {source} {options}\n".encode()


_DRVFS_TABLES: list[bytes | str | None] = [
    None,
    "dir",
    b"",
    _line("0:7", "9p", "drvfs", "rw,aname=drvfs;path=C:\\;uid=1000"),
    _line("0:7", "9p", "drvfs", "rw,aname=drvfs"),
    _line("0:7", "9p", "drvfs", "aname=drvfs,rw"),
    _line("0:7", "9p", "drvfs", "aname=drvfs"),
    _line("0:7", "9p", "drvfs", "rw;aname=drvfs;x"),
    _line("0:7", "9p", "drvfs", "rw,aname=drvfsX"),
    _line("0:7", "9p", "drvfs", "rw,xaname=drvfs"),
    _line("0:7", "9p", "drvfs", "rw,aname=drvfs:"),
    _line("0:7", "9p", "drvfs", "xaname=drvfs,aname=drvfs"),
    _line("0:7", "9p", "drvfs", "aname=DRVFS"),
    _line("0:7", "9p", "drvfs", ""),
    _line("0:7", "9P", "drvfs", "aname=drvfs"),
    _line("0:7", "drvfs", "C:\\", "rw"),
    _line("0:7", "drvfs", "", ""),
    _line("0:7", "virtiofs", "drvfsC0", "rw"),
    _line("0:7", "virtiofs", "drvfs", "rw"),
    _line("0:7", "virtiofs", "drvf", "rw"),
    _line("0:7", "virtiofs", "{a1b2}", "rw"),
    _line("0:7", "ext4", "/dev/sdc", "rw"),
    _line("0:8", "drvfs", "C:", "rw"),
    _line("0:7", "drvfs", "C:", "rw", "shared:1 master:2 "),
    b"36 35 0:7 / /mnt/c rw - drvfs C: rw\n",
    b"36 35 0:7 / /mnt/c - drvfs C: rw\n",
    b"36 35 0:7 / - drvfs\n",
    b"36 35 0:7 / /m rw - drvfs\n",
    b"36 35 0:7 / /m rw -\n",
    b"36 35 0:7 / /m rw - virtiofs\n",
    b"36 35 0:7 / /m rw - 9p\n",
    b"36 35 0:7 / /m rw - 9p x\n",
    b"36 35 0:7 / /m rw x\n",
    b"36 35 0:7 / /m - - drvfs\n",
    b"- - 0:7 - - - - drvfs\n",
    b"36 35 0:7 / /m rw -- drvfs\n",
    b"36 35 0:7 / /m rw - ext4 x y\n36 35 0:7 / /m rw - drvfs x y\n",
    b"36 35 0:7 / /m rw - drvfs x y",
    b"36 35 0:7 / /m rw - drvfs x y\r",
    b"36 35 0:7 / /m rw - drvfs x y\r\n",
    b"36 35 0:7 / /m\rrw - drvfs x y\n",
    b"36 35 0:7 / /m\r\nrw - drvfs x y\n",
    b"36 35 0:7 / /m rw\n- drvfs x y\n",
    b"36\t35\x0b0:7\x0c/\x1c/m\x1drw\x1e-\x1fdrvfs x y\n",
    b"36\xc2\xa035\xc2\x850:7\xe2\x80\xa8/\xe3\x80\x80/m rw - drvfs\n",
    b"36\xa035 0:7 / /m rw - drvfs\n",
    b"36 35 0:7 / /m rw - virtiofs drvfs\xff rw\n",
    b"36 35 0:7 / /m rw - 9p x aname=drvfs\xff\n",
    b"36 35 0:7 / /m rw - 9p x aname=drvfs\xe2\x80\n",
    b"36 35 0:7 / /m rw - d\xe2rvfs\n",
    b"36 35 0:7\xe2 / /m rw - drvfs\n",
    b"\n\n\r\r\n  \n36 35 0:7 / /m rw - drvfs\n",
    b"36 35 0:07 / /m rw - drvfs\n",
    b"36 35 00:7 / /m rw - drvfs\n",
    b"36 35 0:7 / /m rw - drvfs\x00 x\n",
    b"x" * 9000 + b" 0:7\n36 35 0:7 / /m rw - drvfs\n",
    b"36 35 0:7 / /m rw - ext4 " + b"y" * 8180 + b"\r\n36 35 0:7 / /m rw - drvfs\n",
    b"36 35 0:7 / /m rw - ext4 " + b"y" * 8166 + b"\r\n36 35 0:7 / /m rw - drvfs\n",
    b"36 35 0:255 / /m rw - drvfs\n",
    b"36 35 0:0 / /m rw - drvfs\n",
]

_DRVFS_DEVICES = [7, 0, 8, 255, -2, 1 << 64, -(1 << 64)]


def _drvfs_rows(scratch: Path) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for table in _DRVFS_TABLES:
        for device in _DRVFS_DEVICES:
            if device not in (7, 0) and table not in (None, _DRVFS_TABLES[3]):
                continue
            if device == 0 and not isinstance(table, bytes):
                continue
            with _machine(scratch, windows=False, linux=True, table=table):
                got = _call(partial(ls._drvfs, device))
            spelled = _t(table) if isinstance(table, bytes) else table
            rows.append(["drvfs", spelled, str(device), got])
    return rows


_TIMES: list[list[int]] = [
    [],
    [0],
    [100],
    [10**9],
    [10**9, 2 * 10**9],
    [10**9, 100],
    [10**9, 150],
    [-100],
    [-(10**9)],
    [-(10**9), -100],
    [-1],
    [1, 10**9 + 100],
    [10**18 + 100, 10**18],
    [1 << 100],
    [(1 << 100) * 100],
    [-(1 << 90) * 10**9, 300],
    [170141183460469231731687303715884105700],
    [-170141183460469231731687303715884105700],
    [100, 100, 100],
    [10**9, 10**9],
]


def _ints(values: list[int]) -> list[str]:
    return [str(v) for v in values]


def _kept_rows(scratch: Path) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for times in _TIMES:
        for windows, linux in ((False, False), (True, False), (False, True)):
            with _machine(scratch, windows=windows, linux=linux, table=None):
                got = _call(partial(ls.kept_in_ticks, iter(times)))
            rows.append(["kept", windows, linux, None, None, _ints(times), got])
    drvfs = _DRVFS_TABLES[15]
    for windows, linux, table, device in (
        (False, True, drvfs, 7),
        (False, True, drvfs, 8),
        (False, False, drvfs, 7),
        (True, True, drvfs, 7),
        (False, True, None, 7),
        (False, True, drvfs, -2),
        (True, False, None, -2),
        (False, False, None, -2),
    ):
        for times in ([], [100], [10**9]):
            with _machine(scratch, windows=windows, linux=linux, table=table):
                got = _call(partial(ls.kept_in_ticks, times, device))
            spelled = _t(table) if isinstance(table, bytes) else None
            rows.append(
                ["kept", windows, linux, spelled, str(device), _ints(times), got]
            )
    return rows


def _stat_rows() -> list[list[Any]]:
    stats = {"a": ["1", "2", "3", "4", "5"], "nul": "value"}
    rows: list[list[Any]] = []
    for paths in (
        [],
        ["a"],
        [None],
        ["missing"],
        ["nul"],
        ["a", None, "missing", "nul", "a"],
    ):
        with mock.patch.object(ls, "os", _fake_os(stats, False)):
            got = ls.stat_each(paths)
        rows.append(
            ["stat", paths, stats, [None if s is None else str(s.st_ino) for s in got]]
        )
    return rows


def _init_rows() -> list[list[Any]]:
    full = ["11", "1", "1700000000123456700", "1700000001000000000", "73"]
    other = ["12", "1", "-5", "0", "0"]
    big = ["18446744073709551615", "1", str(-(1 << 63)), str((1 << 63) - 1), "0"]
    cases: list[tuple[bool, bool, str, bool, dict[str, Any], dict[str, Any]]] = [
        (
            False,
            False,
            "/g",
            False,
            {"/g/description": full, "/g/info/exclude": other},
            {},
        ),
        (
            False,
            False,
            "/g/",
            False,
            {"/g/description": full, "/g/info/exclude": other},
            {},
        ),
        (False, False, "", False, {"description": full, "info/exclude": big}, {}),
        (False, False, "/g", False, {"/g/info/exclude": other}, {}),
        (False, False, "/g", False, {"/g/description": full}, {}),
        (False, False, "/g", False, {}, {}),
        (
            False,
            False,
            "/g",
            False,
            {"/g/description": "value", "/g/info/exclude": other},
            {},
        ),
        (False, False, "/g", False, {"/g/info/exclude": "value"}, {}),
        (False, False, "/g", True, {"/g/commondir": full, "/g/description": other}, {}),
        (False, False, "/g", True, {"/g/description": other}, {}),
        (False, False, "/g", True, {"/g/commondir": "value"}, {}),
        (True, False, "C:\\g", False, {"C:\\g\\description": full}, {}),
        (True, False, "C:", False, {"C:description": full, "C:info\\exclude": big}, {}),
        (True, False, "C:/g/", True, {"C:/g/commondir": full}, {}),
        (True, False, "\\\\s\\sh", True, {"\\\\s\\sh\\commondir": full}, {}),
    ]
    ticks = ["133500000001234567", "0", str(-(1 << 63)), str((1 << 63) - 1)]
    for nt, _w, git_dir, linked, stats, _basic in list(cases):
        if not stats or any(v == "value" for v in stats.values()):
            continue
        first = sorted(stats)[0]
        cases.append((nt, True, git_dir, linked, stats, {first: ticks}))
        cases.append((nt, True, git_dir, linked, stats, {first: "fail"}))
    cases.append((True, True, "C:\\g", False, {"C:\\g\\description": "value"}, {}))
    rows: list[list[Any]] = []
    for nt, windows, git_dir, linked, stats, basic in cases:

        def fake_basic(path: str, basic: dict[str, Any] = basic) -> Any:
            got = basic.get(path, "fail")
            if got == "fail":
                return None
            return tuple(ls.filetime_ns(int(t)) for t in got)

        with (
            mock.patch.object(ls, "os", _fake_os(stats, nt)),
            mock.patch.object(ls, "_WINDOWS", windows),
            mock.patch.object(ls, "basic_info_ns", fake_basic),
        ):
            got = _call(partial(ls._init_stamps, git_dir, linked=linked))
        if isinstance(got, tuple):
            got = [[s[0], *(str(v) for v in s[1:])] for s in got]
        rows.append(["init", nt, windows, git_dir, linked, stats, basic, got])
    return rows


def _cap_rows(scratch: Path) -> list[list[Any]]:
    tick = ["7", "1700000000000000100", "1700000000000000200"]
    second = ["7", "1700000000000000000", "1700000001000000000"]
    nanos = ["7", "1700000000000000001", "1700000000000000200"]
    away = ["9", "1700000000000000001", "1700000000000000003"]
    stamp_t = ["description", "1", "1700000000000000100", "1700000000000000000", "0"]
    stamp_n = ["description", "1", "1700000000000000101", "1700000000000000000", "0"]
    drvfs = _DRVFS_TABLES[15]
    cases: list[tuple[bool, bool, Any, Any, Any, list[Any], list[Any]]] = [
        (False, False, None, None, None, [], []),
        (False, False, None, None, None, [nanos], [stamp_n]),
        (True, False, None, None, None, [], []),
        (False, False, None, [None, 1], None, [], []),
        (False, False, tick, None, None, [], []),
        (False, False, second, None, None, [second], []),
        (False, False, second, None, None, [second, tick], []),
        (False, False, tick, None, None, [tick, None, away], []),
        (False, False, tick, None, None, [tick, nanos], []),
        (False, False, tick, None, None, [None], [stamp_t]),
        (False, False, tick, None, None, [None], [stamp_n]),
        (False, False, tick, None, None, [tick], [stamp_n]),
        (False, False, second, None, None, [second], [stamp_t]),
        (False, False, nanos, [1, 1, 1], None, [nanos], []),
        (False, True, nanos, [1, 1, None], drvfs, [nanos], []),
        (False, True, nanos, [1, 1, 1], drvfs, [nanos], []),
        (False, True, nanos, None, drvfs, [nanos], []),
        (False, False, nanos, [1, 1, None], drvfs, [nanos], []),
        (False, True, tick, [1, 1, None], None, [tick], []),
        (False, True, nanos, [1, 1, None], None, [nanos], []),
        (False, True, nanos, [1, 1, None, 1], drvfs, [], []),
        (False, False, tick, [1, 1], None, [tick], []),
        (False, False, tick, [], None, [], []),
        (True, False, tick, [1, 1], None, [tick], []),
        (True, False, tick, [1, 1, None], None, [nanos], [stamp_n]),
        (True, False, nanos, None, None, [nanos], []),
        (False, True, ["8", "100", "100"], [1, 1, None], drvfs, [], []),
    ]
    rows: list[list[Any]] = []
    for windows, linux, git_st, key, table, read, stamps in cases:
        st: Any = None if git_st is None else _Stat(0, *(int(v) for v in git_st), 0)
        read_st: list[Any] = [
            None if r is None else _Stat(0, *(int(v) for v in r), 0) for r in read
        ]
        stamp_t_ = [(s[0], *(int(v) for v in s[1:])) for s in stamps]
        with _machine(scratch, windows=windows, linux=linux, table=table):
            got = _call(partial(ls.capture_ticks, st, key, read_st, stamp_t_))
        spelled = _t(table) if isinstance(table, bytes) else None
        rows.append(["cap", windows, linux, spelled, git_st, key, read, stamps, got])
    return rows


def _ft_rows() -> list[list[Any]]:
    ticks = [
        0,
        1,
        -1,
        ls._FILETIME_UNIX,
        ls._FILETIME_UNIX + 1,
        ls._FILETIME_UNIX - 1,
        133_500_000_001_234_567,
        (1 << 63) - 1,
        -(1 << 63),
        1 << 64,
        -(1 << 120),
    ]
    return [["ft", str(t), str(ls.filetime_ns(t))] for t in ticks]


def _basic_rows() -> list[list[Any]]:
    invalid = ctypes.c_void_p(-1).value
    rows: list[list[Any]] = []
    sets = [
        ["133500000001234567", "116444736000000000", "0", "-1"],
        [str(-(1 << 63)), str((1 << 63) - 1), "1", "2"],
    ]
    scenarios: list[Any] = [
        "posix",
        "nocalls",
        "nohandle",
        "invalid",
        "queryfail",
        *sets,
    ]
    for scenario in scenarios:
        closed: list[int] = []

        def create(*_args: Any, scenario: Any = scenario) -> Any:
            if scenario == "nohandle":
                return None
            return invalid if scenario == "invalid" else 5

        def query(
            handle: int, _cls: int, ref: Any, _size: int, scenario: Any = scenario
        ) -> int:
            if scenario == "queryfail":
                return 0
            info = ref._obj
            for field, value in zip(
                ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime"),
                scenario,
                strict=True,
            ):
                setattr(info, field, int(value))
            return 1

        info_type = type(
            "FileBasicInfo",
            (ctypes.Structure,),
            {
                "_fields_": [
                    ("CreationTime", ctypes.c_int64),
                    ("LastAccessTime", ctypes.c_int64),
                    ("LastWriteTime", ctypes.c_int64),
                    ("ChangeTime", ctypes.c_int64),
                    ("FileAttributes", ctypes.c_uint32),
                ]
            },
        )
        calls = (
            None if scenario == "nocalls" else (create, query, closed.append, info_type)
        )
        plat = "darwin" if scenario == "posix" else "win32"
        with (
            mock.patch.object(ls, "sys", types.SimpleNamespace(platform=plat)),
            mock.patch.object(ls, "_basic_info_calls", lambda calls=calls: calls),
        ):
            got = ls.basic_info_ns("C:\\g\\description")
        rows.append(["basic", scenario, None if got is None else [str(v) for v in got]])
    return rows


def generate() -> dict[str, bytes]:
    rows: list[list[Any]] = [["in", _ints(t), ls.in_ticks(t)] for t in _TIMES]
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        rows += _kept_rows(scratch)
        rows += _drvfs_rows(scratch)
        rows += _cap_rows(scratch)
    rows += _stat_rows()
    rows += _init_rows()
    rows += _ft_rows()
    rows += _basic_rows()
    rows += _sweep()
    kinds: dict[str, int] = {}
    for row in rows:
        kinds[row[0]] = kinds.get(row[0], 0) + 1
    header = {"fixture": "location_stamps", "kinds": kinds}
    return {"location_stamps.jsonl": jsonl([header, *rows])}


_DEVICES = [
    0,
    1,
    255,
    256,
    4095,
    4096,
    0x12345,
    (1 << 20) - 1,
    1 << 20,
    (1 << 24) - 1,
    1 << 24,
    (1 << 31) - 1,
    1 << 31,
    (1 << 32) - 1,
    1 << 32,
    (1 << 32) + 0x12345,
    (1 << 36) - 1,
    (1 << 44) + 0x123456789,
    0x00000FFF_FFF000FF,
    (1 << 63) - 1,
    1 << 63,
    0xFFFFF000_00000000,
    0xFFFFF000_000FFF00,
    0xFFFFF000_000FFFFF,
    0xFFFFFFFF_00000000,
    (1 << 64) - (1 << 31) - 1,
    (1 << 64) - (1 << 31),
    (1 << 64) - 256,
    (1 << 64) - 2,
    (1 << 64) - 1,
    1 << 64,
    1 << 100,
    -1,
    -2,
    -(1 << 31),
    -(1 << 32),
    -(1 << 63),
    -(1 << 63) - 1,
    -(1 << 64),
    -(1 << 100),
]

_PLATFORM_FILE = "location_stamps_platform.jsonl"

#: The command that recorded a section on another machine, run from the
#: tree's root with ``IMAGE`` present: it prints `device_rows` of the
#: interpreter in the image, the section's rows. The state directories
#: point into the container's own scratch.
_OBSERVE = (
    "docker run --rm --pull never --network none --tmpfs /scratch"
    " -e BETTERMEMORY_DIR=/scratch -e BETTERMEMORY_STATE_DIR=/scratch"
    ' -e BETTERMEMORY_KEYS_DIR=/scratch -v "$PWD":/tree:ro IMAGE'
    " python3 -I -B -c \"import sys; sys.path[:0] = ['/tree/src', '/tree'];"
    ' from tests.contract.gen_location_stamps import observe; observe()"'
)

_GNU_IMAGE = (
    "ghcr.io/astral-sh/uv:0.5-python3.13-bookworm-slim@sha256:"
    "e92baaf2b696ff16448644b0c781de48605b9c0873d16a0fd27de598d17cbc8c"
)
_MUSL_IMAGE = (
    "python:3.13-alpine@sha256:"
    "2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a"
)

#: Where each section this machine cannot make was recorded (`_OBSERVE`).
#: Linux's `os.major` is as the C library CPython was built against
#: defines it: glibc's headers define ``NODEV`` (-1 is accepted, and a
#: part of 0xffffffff is answered -1), musl's do not (-1 raises, every
#: part is unsigned).
_RECORDED: dict[str, dict[str, str]] = {
    "linux-gnu": {
        "command": _OBSERVE.replace("IMAGE", _GNU_IMAGE),
        "libc": "glibc 2.36",
        "machine": "aarch64",
        "python": "3.13.2",
    },
    "linux-musl": {
        "command": _OBSERVE.replace("IMAGE", _MUSL_IMAGE),
        "libc": "musl 1.2.6",
        "machine": "aarch64",
        "python": "3.13.16",
    },
}


def encoding() -> str:
    """How this interpreter's `os.major` and `os.minor` name a device:
    `sys.platform`, and on Linux the C library CPython was built against,
    the last part of its ``SOABI`` (``linux-gnu``, ``linux-musl``)."""
    if sys.platform.startswith("linux"):
        abi = sysconfig.get_config_var("SOABI") or ""
        return "linux-" + abi.rsplit("-", 1)[-1]
    return sys.platform


def device_rows() -> list[list[Any]]:
    """`_device_name` over `_DEVICES` on this interpreter."""
    return [["dev", str(d), _call(partial(ls._device_name, d))] for d in _DEVICES]


def observe() -> None:
    """Print `device_rows` as the platform file holds them: the rows of a
    recorded section (`_OBSERVE`)."""
    sys.stdout.write(jsonl(device_rows()).decode("ascii"))


def _provenance() -> dict[str, dict[str, str]]:
    """Every section's provenance in the file's order: macOS's, made by
    the pinned run (`generate.PLATFORM`, `generate.PYTHON_VERSION`), then
    the recorded ones."""
    from .generate import PLATFORM, PYTHON_VERSION

    darwin = {
        "command": "python -m tests.contract.generate",
        "machine": PLATFORM.split("-", 1)[1],
        "python": PYTHON_VERSION,
    }
    return {"darwin": darwin, **_RECORDED}


def _committed_sections() -> dict[str, list[Any]]:
    """The committed platform file's sections by name: each one's lines,
    its ``section`` line first."""
    path = Path(__file__).resolve().parent / "golden" / _PLATFORM_FILE
    out: dict[str, list[Any]] = {}
    if not path.is_file():
        return out
    key = None
    for line in path.read_text(encoding="ascii").splitlines()[1:]:
        row = json.loads(line)
        if row[0] == "section":
            key = row[1]
            out[key] = []
        if key is not None:
            out[key].append(row)
    return out


def generate_platform() -> dict[str, bytes]:
    """`_device_name` per encoding of `os.major` (`encoding`), a section
    each, which names it and where its rows were made: this interpreter's
    is made here, and every other one is carried from the committed file,
    where it must hold `_DEVICES` in order under its recorded provenance.
    An encoding with no recorded section (Windows', every name ``""``) is
    made here and put last."""
    here = encoding()
    provenance = _provenance()
    if here not in provenance:
        provenance[here] = {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        }
    committed = _committed_sections()
    devices = [str(d) for d in _DEVICES]
    lines: list[Any] = [
        {"fixture": "location_stamps_platform", "sections": list(provenance)}
    ]
    for key, made in provenance.items():
        if key == here:
            lines += [["section", key, made], *device_rows()]
            continue
        carried = committed.get(key, [])
        if (
            not carried
            or carried[0] != ["section", key, made]
            or [row[1] for row in carried[1:]] != devices
        ):
            raise ValueError(
                f"{_PLATFORM_FILE} holds no {key} section of _DEVICES as "
                f"recorded; it is made on such a machine: {made['command']}"
            )
        lines += carried
    return {_PLATFORM_FILE: jsonl(lines)}
