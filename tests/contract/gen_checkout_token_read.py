"""Fixtures: the checkout token's reads (``checkout_token_read.jsonl``,
``checkout_token_read_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/checkout_token/read.rs``: `read`,
`_read_at`, `_read_text`, `_open_plain`, `_read_all` and
`_clear_nonblock`, with `_os_text` wording an unreadable file.

``checkout_token_read.jsonl`` runs the reference over a scripted OS:
``os.open``, ``os.stat``, ``os.fstat``, ``os.read``, ``os.close``,
``fcntl.fcntl``, ``sys.platform`` and ``time.sleep`` are replaced by a
script that answers each call as the row says and logs it. Errors are
``OSError(errno.<name>, "text of <name>")``, so the class follows the
errno as CPython picks it and the text is the script's, the same on every
platform: the file is not gated.

A row: ``path`` (what `_read_at` is given; with ``git`` instead, `read`
of that git directory), ``plat`` (``posix`` or ``win32``), ``passes``
(one script per `_read_text` call), ``log`` (the calls made, in order)
and ``r`` (``state``, ``token``, ``detail``) or ``x`` (the class raised).
A script's keys, each optional: ``open`` (the non-blocking open: ``ok``
or an errno name), ``stat`` (``reg``, ``fifo`` or an errno name),
``open2`` (the plain open), ``fstat`` (``reg``, ``fifo``, ``dir`` or an
errno name), ``reads`` (in order: ``t`` an ASCII text, ``rep`` a byte in
hex and a count, or ``e`` an errno name; a chunk longer than the count
asked is cut and its rest answered next; past the list, end of file),
``getfl`` and ``setfl`` (``ok`` or an errno name), ``close``.
``O_NONBLOCK`` is 4 and ``fcntl`` a stand-in module; ``F_GETFL`` answers
``0o2000 | 4``; ``F_SETFL`` must be given ``0o2000``.

``checkout_token_read_posix.jsonl`` (POSIX only) reads a real tree in a
scratch directory: links, a FIFO, a folder, a loop, a name too long, and
``/dev/null``. A row names the entry under the scratch directory (or the
absolute path) and holds the answer and the pauses taken. Its texts are
those macOS and glibc share.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import checkout_token as ct

from ._common import jsonl

_TOK = "0123456789ABCDEFGHJKMNPQRS"
_FILE = ct.render(_TOK).decode("ascii")
_HEAD = ct.TOKEN_HEADER + "\n"
_BASE = 0o2000
# The non-blocking flag of the scripted platform, set on the module so the
# rows do not depend on the host's value (0 on Windows).
_NONBLOCK = 4
_F_GETFL = 3
_F_SETFL = 4


class _Script:
    def __init__(self, passes: list[dict[str, Any]]) -> None:
        self.passes = list(passes)
        self.now: dict[str, Any] = {}
        self.reads: list[bytes | str] = []
        self.log: list[str] = []

    def _fail(self, name: str) -> OSError:
        code = getattr(errno, name)
        return OSError(code, f"text of {name}")

    def open(self, path: Any, flags: int) -> int:
        if "\0" in str(path):
            raise ValueError("embedded null byte")
        if flags & _NONBLOCK:
            if not self.passes:
                raise AssertionError("an open past the script")
            self.now = self.passes.pop(0)
            self.reads = []
            for item in self.now.get("reads", []):
                if "e" in item:
                    self.reads.append(item["e"])
                elif "t" in item:
                    self.reads.append(item["t"].encode("ascii"))
                else:
                    byte, count = item["rep"]
                    self.reads.append(bytes.fromhex(byte) * count)
            self.log.append(f"open {path}")
            answer = self.now.get("open", "ok")
        else:
            self.log.append("open2")
            answer = self.now.get("open2", "ok")
        if answer != "ok":
            raise self._fail(answer)
        return 7

    def _mode(self, kind: str) -> Any:
        if kind in ("reg", "fifo", "dir"):
            bits = {"reg": stat.S_IFREG, "fifo": stat.S_IFIFO, "dir": stat.S_IFDIR}
            return mock.Mock(st_mode=bits[kind] | 0o644)
        raise self._fail(kind)

    def stat(self, path: Any) -> Any:
        self.log.append("stat")
        return self._mode(self.now.get("stat", "reg"))

    def fstat(self, fd: int) -> Any:
        self.log.append("fstat")
        return self._mode(self.now.get("fstat", "reg"))

    def read(self, fd: int, n: int) -> bytes:
        self.log.append(f"read {n}")
        if not self.reads:
            return b""
        item = self.reads.pop(0)
        if isinstance(item, str):
            raise self._fail(item)
        if len(item) > n:
            self.reads.insert(0, item[n:])
            item = item[:n]
        return item

    def close(self, fd: int) -> None:
        self.log.append("close")
        answer = self.now.get("close", "ok")
        if answer != "ok":
            raise self._fail(answer)

    def fcntl(self, fd: int, cmd: int, arg: int = 0) -> int:
        if cmd == _F_GETFL:
            self.log.append("getfl")
            answer = self.now.get("getfl", "ok")
            if answer != "ok":
                raise self._fail(answer)
            return _BASE | _NONBLOCK
        assert cmd == _F_SETFL and arg == _BASE, (cmd, arg)
        self.log.append("setfl")
        answer = self.now.get("setfl", "ok")
        if answer != "ok":
            raise self._fail(answer)
        return 0

    def sleep(self, seconds: float) -> None:
        self.log.append(f"pause {seconds}")


@contextmanager
def _scripted(script: _Script, plat: str) -> Iterator[None]:
    with ExitStack() as stack:
        for name in ("open", "stat", "fstat", "read", "close"):
            stack.enter_context(mock.patch.object(os, name, getattr(script, name)))
        fake = types.ModuleType("fcntl")
        fake.F_GETFL = _F_GETFL  # type: ignore[attr-defined]
        fake.F_SETFL = _F_SETFL  # type: ignore[attr-defined]
        fake.fcntl = script.fcntl  # type: ignore[attr-defined]
        stack.enter_context(mock.patch.dict(sys.modules, {"fcntl": fake}))
        stack.enter_context(mock.patch.object(ct, "_NONBLOCK", _NONBLOCK))
        stack.enter_context(
            mock.patch.object(ct, "_OPEN_READ", os.O_RDONLY | _NONBLOCK)
        )
        stack.enter_context(mock.patch.object(time, "sleep", script.sleep))
        stack.enter_context(
            mock.patch.object(sys, "platform", "win32" if plat == "win32" else "posix")
        )
        yield


def _answer(found: ct.TokenRead) -> dict[str, Any]:
    return {"state": found.state, "token": found.token, "detail": found.detail}


def _row(
    name: str,
    passes: list[dict[str, Any]],
    path: str = "/r/.git/info/bettermemory",
    plat: str = "posix",
    git: str | None = None,
) -> dict[str, Any]:
    script = _Script(passes)
    row: dict[str, Any] = {"n": name, "plat": plat, "passes": passes}
    if git is None:
        row["path"] = path
    else:
        row["git"] = git
    try:
        with _scripted(script, plat):
            if git is None:
                found = ct._read_at(Path(path))
            else:
                found = ct.read(git)
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        row["x"] = type(exc).__name__
    else:
        row["r"] = _answer(found)
    assert not script.passes, name
    row["log"] = script.log
    return row


def _t(text: str) -> dict[str, Any]:
    return {"t": text}


def _whole(text: str) -> dict[str, Any]:
    return {"reads": [_t(text)]}


def _rows() -> list[dict[str, Any]]:
    rows = [
        _row("token", [_whole(_FILE)]),
        _row("token by git dir", [_whole(_FILE)], git="/r/.git"),
        _row("token by git dir, trailing slash", [_whole(_FILE)], git="/r/.git/"),
        _row("crlf", [_whole(ct.TOKEN_HEADER + "\r\n" + _TOK + "\r\n")]),
        _row("no final line end", [_whole(_HEAD + _TOK)]),
        _row("lower case", [_whole(_HEAD + _TOK.lower() + "\n")]),
        _row("token in bytes one at a time", [{"reads": [_t(c) for c in _FILE]}]),
        _row("token in two reads", [{"reads": [_t(_FILE[:10]), _t(_FILE[10:])]}]),
        _row("empty then token", [{}, _whole(_FILE)]),
        _row("empty twice", [{}, {}]),
        _row("header part then token", [_whole("bettermem"), _whole(_FILE)]),
        _row("header then empty", [_whole(_HEAD), {}]),
        _row("cut short twice", [_whole(_HEAD + "0123"), _whole(_HEAD + "01234")]),
        _row("cut short then other", [_whole(_HEAD), _whole("other\n")]),
        _row("cut short then absent", [_whole(_HEAD), {"open": "ENOENT"}]),
        _row("cut short then unreadable", [_whole(_HEAD), {"open": "EACCES"}]),
        _row("cut short then fifo", [_whole(_HEAD), {"fstat": "fifo"}]),
        _row("cut short crlf", [_whole(ct.TOKEN_HEADER + "\r\n")]),
        _row("other text", [_whole("hello\n")]),
        _row("two tokens", [_whole(_FILE + _TOK + "\n")]),
        _row("4095 bytes", [{"reads": [{"rep": ["61", 4095]}]}]),
        _row("4096 bytes", [{"reads": [{"rep": ["61", 4096]}]}]),
        _row("4097 bytes", [{"reads": [{"rep": ["61", 4097]}]}]),
        _row("5000 bytes", [{"reads": [{"rep": ["61", 5000]}]}]),
        _row(
            "4097 bytes in small reads",
            [{"reads": [{"rep": ["61", 1000]}] * 4 + [{"rep": ["61", 97]}]}],
        ),
        _row("not ascii", [{"reads": [{"rep": ["ff", 1]}]}]),
        _row("not ascii and large", [{"reads": [{"rep": ["ff", 4097]}]}]),
        _row("nul byte", [{"reads": [{"rep": ["00", 3]}]}]),
        _row("fifo", [{"fstat": "fifo"}]),
        _row("folder", [{"fstat": "dir"}]),
        _row("absent", [{"open": "ENOENT"}]),
        _row("not a folder on the way", [{"open": "ENOTDIR"}]),
        _row("open refused", [{"open": "EACCES"}]),
        _row("open loop", [{"open": "ELOOP"}]),
        _row("open too long", [{"open": "ENAMETOOLONG"}]),
        _row("open is a folder", [{"open": "EISDIR"}]),
        _row("open interrupted", [{"open": "EINTR"}]),
        _row("open would block", [{"open": "EAGAIN"}]),
        _row("open no device", [{"open": "ENXIO"}]),
        _row("open einval, plain", [{"open": "EINVAL", "reads": [_t(_FILE)]}]),
        _row("open eio, plain", [{"open": "EIO", "reads": [_t(_FILE)]}]),
        _row("open einval, stat fifo", [{"open": "EINVAL", "stat": "fifo"}]),
        _row("open einval, stat folder", [{"open": "EINVAL", "stat": "dir"}]),
        _row("open einval, stat absent", [{"open": "EINVAL", "stat": "ENOENT"}]),
        _row("open einval, stat notdir", [{"open": "EINVAL", "stat": "ENOTDIR"}]),
        _row("open einval, stat refused", [{"open": "EINVAL", "stat": "EACCES"}]),
        _row("open einval, stat eio", [{"open": "EINVAL", "stat": "EIO"}]),
        _row("open einval, plain absent", [{"open": "EINVAL", "open2": "ENOENT"}]),
        _row("open einval, plain einval", [{"open": "EINVAL", "open2": "EINVAL"}]),
        _row("open einval, plain refused", [{"open": "EIO", "open2": "EACCES"}]),
        _row(
            "open einval, plain, fstat fifo",
            [{"open": "EINVAL", "fstat": "fifo"}],
        ),
        _row(
            "open einval, plain, close fails",
            [{"open": "EINVAL", "reads": [_t(_FILE)], "close": "EIO"}],
        ),
        _row(
            "open einval, cut short twice",
            [{"open": "EINVAL", "reads": [_t(_HEAD)]}, {"open": "EINVAL"}],
        ),
        _row("fstat fails", [{"fstat": "EIO"}]),
        _row("fstat refused", [{"fstat": "EACCES"}]),
        _row("read eagain", [{"reads": [{"e": "EAGAIN"}]}]),
        _row("read eisdir", [{"reads": [{"e": "EISDIR"}]}]),
        _row("read ebadf", [{"reads": [{"e": "EBADF"}]}]),
        _row("read eintr", [{"reads": [{"e": "EINTR"}]}]),
        _row("read einval, cleared", [{"reads": [{"e": "EINVAL"}, _t(_FILE)]}]),
        _row("read eio, cleared", [{"reads": [{"e": "EIO"}, _t(_FILE)]}]),
        _row(
            "read einval after a chunk",
            [{"reads": [_t(_FILE[:5]), {"e": "EINVAL"}, _t(_FILE[5:])]}],
        ),
        _row(
            "read einval twice in a row",
            [{"reads": [{"e": "EINVAL"}, {"e": "EINVAL"}]}],
        ),
        _row(
            "read einval then eio",
            [{"reads": [{"e": "EINVAL"}, {"e": "EIO"}, _t(_FILE)]}],
        ),
        _row(
            "read einval each time",
            [
                {
                    "reads": [
                        _t(_FILE[:5]),
                        {"e": "EINVAL"},
                        _t(_FILE[5:20]),
                        {"e": "EIO"},
                        _t(_FILE[20:]),
                    ]
                }
            ],
        ),
        _row(
            "read einval, getfl fails",
            [{"reads": [{"e": "EINVAL"}, _t(_FILE)], "getfl": "EBADF"}],
        ),
        _row(
            "read einval, setfl fails",
            [{"reads": [{"e": "EINVAL"}, _t(_FILE)], "setfl": "EPERM"}],
        ),
        _row(
            "read einval, win32",
            [{"reads": [{"e": "EINVAL"}, _t(_FILE)]}],
            plat="win32",
        ),
        _row("read eacces, not retried", [{"reads": [{"e": "EACCES"}, _t(_FILE)]}]),
        _row("close fails", [{"reads": [_t(_FILE)], "close": "EIO"}]),
        _row("close interrupted", [{"reads": [_t(_FILE)], "close": "EINTR"}]),
        _row("close fails on fifo", [{"fstat": "fifo", "close": "EBADF"}]),
        _row(
            "close fails after a read error",
            [{"reads": [{"e": "EACCES"}], "close": "EIO"}],
        ),
        _row(
            "close fails on the reread",
            [_whole(_HEAD), {"reads": [_t(_FILE)], "close": "EIO"}],
        ),
        _row("nul in the path", [], path="/r/a\0b"),
        _row("nul in the git dir", [], git="/r/\0"),
        _row("relative path", [_whole(_FILE)], path="rel/bettermemory"),
    ]
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows()
    return {
        "checkout_token_read.jsonl": jsonl(
            [{"fixture": "checkout_token_read", "count": len(rows)}, *rows]
        )
    }


def generate_posix() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "token").write_bytes(ct.render(_TOK))
        (root / "empty").write_bytes(b"")
        (root / "big").write_bytes(b"a" * 5000)
        (root / "exact").write_bytes(b"a" * 4096)
        (root / "nonascii").write_bytes(b"\xff")
        (root / "nonascii80").write_bytes(b"\x80")
        getattr(os, "mkfifo")(root / "fifo")  # noqa: B009 - POSIX only
        (root / "dir").mkdir()
        (root / "link").symlink_to("token")
        (root / "dangling").symlink_to("nowhere")
        (root / "loop").symlink_to("loop")
        (root / "fifolink").symlink_to("fifo")
        info = root / "repo" / ".git" / "info"
        info.mkdir(parents=True)
        (info / "bettermemory").write_bytes(ct.render(_TOK))
        names = [
            "token",
            "empty",
            "big",
            "exact",
            "nonascii",
            "nonascii80",
            "fifo",
            "dir",
            "link",
            "dangling",
            "loop",
            "fifolink",
            "missing",
            "token/below",
            "x" * 300,
            "/dev/null",
        ]
        for name in names:
            pauses: list[float] = []
            with mock.patch.object(time, "sleep", pauses.append):
                found = ct._read_at(root / name)
            entry = name if len(name) < 100 else f"x*{len(name)}"
            rows.append({"n": entry, "r": _answer(found), "pauses": pauses})
        found = ct.read(root / "repo" / ".git")
        rows.append({"n": "git:repo/.git", "r": _answer(found), "pauses": []})
    return {
        "checkout_token_read_posix.jsonl": jsonl(
            [{"fixture": "checkout_token_read_posix", "count": len(rows)}, *rows]
        )
    }
