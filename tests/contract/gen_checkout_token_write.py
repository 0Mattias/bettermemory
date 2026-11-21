"""Fixtures: the checkout token's write (``checkout_token_write.jsonl``,
``checkout_token_write_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/checkout_token/write.rs``: `write`,
`_guard`, `_existing`, `_info_dir`, `_create`, `_write_temp`, `_fs_now`,
`_sweep`, `_place`, `_after_race`, `_failed` and `_unlink_quietly`.

``checkout_token_write.jsonl`` runs `write` over a scripted OS, built on
the read file's script (`gen_checkout_token_read`): each `_read_text`
takes the next of the row's ``reads`` as there, and the write-side calls
(``os.stat`` of the git directory and of the temporary file,
``os.geteuid``, ``os.mkdir``, the exclusive ``os.open``, ``os.write``,
``os.fsync``, ``os.close``, ``os.utime``, ``os.scandir``, ``os.unlink``,
``os.link``, the directory open of `fsync_dir`), the random bytes
(``random._urandom``), ``sys.platform``, ``time.monotonic`` and
``time.sleep`` answer as the row says and are logged in order. The clock
starts at 100.0 and moves only by the pauses taken. The k-th random draw
(from 0) of n bytes is ``(37 * k + 11 * i + 5) % 256`` for i below n.
POSIX only, as the read file is: on Windows the logged paths are spelled
with backslashes and errno names are the host's. The git directories are
relative (``r/.git``): the scripted OS touches no disk, and under the test
session the working directory is its scratch workspace, which the
session's guard on `write` asks a git directory to lie in.

A row: ``git`` (the git directory), ``plat``, ``policy``, ``bare``,
``owner`` (a uid or an errno name), ``euid``, ``reads`` (the read
scripts), ``mkdir``, ``creates`` (one per exclusive open: ``open``,
``writes`` (each a count written or an errno name; past the list, all
that is asked), ``fsync``, ``close``), ``utime``, ``mtime`` (the
temporary file's ``st_mtime`` or an errno name), ``scan`` (an errno name,
or entries ``name`` with ``m`` an ``st_mtime`` or an errno name, or
``err`` an errno the iteration raises), ``link``, ``unlinks`` (answers in
order, past the list ``ok``); then ``log`` and ``r`` (``status``,
``token``, ``detail``) or ``x`` (the class raised).

``checkout_token_write_posix.jsonl`` (POSIX only) runs `write` over real
git directories in a scratch folder: a fresh one (the token written is
random, so the row holds only that the file then parses as it), an
existing token, a malformed file, a missing ``info``, stale and young
temporary files, a token name taken by a folder, and a read-only ``info``
where the process is not the superuser.
"""

from __future__ import annotations

import errno
import os
import random
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import checkout_token as ct

from . import gen_checkout_token_read as rd
from ._common import jsonl

_TOK = rd._TOK
_FILE = rd._FILE
_HEAD = rd._HEAD
_GIT = "r/.git"
_UID = 501


def _fail(name: str) -> OSError:
    return OSError(getattr(errno, name), f"text of {name}")


class _Entry:
    def __init__(self, script: _Script, base: str, spec: dict[str, Any]) -> None:
        self.script = script
        self.name = spec["name"]
        self.path = os.path.join(base, self.name)
        self.spec = spec

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        assert not follow_symlinks
        self.script.log.append(f"lstat {self.name}")
        m = self.spec["m"]
        if isinstance(m, str):
            raise _fail(m)
        return mock.Mock(st_mtime=m)


class _Scan:
    def __init__(self, script: _Script, base: str, specs: list[Any]) -> None:
        self.items = [(s, base) for s in specs]
        self.script = script

    def __enter__(self) -> _Scan:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[_Entry]:
        for spec, base in self.items:
            if "err" in spec:
                raise _fail(spec["err"])
            yield _Entry(self.script, base, spec)


class _Script(rd._Script):
    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(row.get("reads", []))
        self.row = row
        self.token_path = str(ct.token_path(row.get("git", _GIT)))
        self.creates = list(row.get("creates", []))
        self.created: dict[int, dict[str, Any]] = {}
        self.writes: list[Any] = []
        self.unlinks = list(row.get("unlinks", []))
        self.clock = 100.0
        self.draws = 0
        self.next_fd = 20

    def _nul(self, path: Any) -> str:
        text = os.fspath(path)
        if "\0" in text:
            raise ValueError("embedded null byte")
        return text

    def _answer(self, key: str, default: Any = "ok") -> Any:
        return self.row.get(key, default)

    def open(self, path: Any, flags: int, mode: int = 0o777) -> int:
        if flags & os.O_CREAT:
            text = self._nul(path)
            self.log.append(f"create {text} {mode:o}")
            spec = self.creates.pop(0) if self.creates else {}
            if spec.get("open", "ok") != "ok":
                raise _fail(spec["open"])
            self.next_fd += 1
            self.created[self.next_fd] = spec
            self.writes = list(spec.get("writes", []))
            return self.next_fd
        if flags & getattr(os, "O_DIRECTORY", 0):
            self.log.append(f"dopen {self._nul(path)}")
            return 99
        return super().open(path, flags)

    def stat(self, path: Any) -> Any:
        text = self._nul(path)
        if text == self.token_path:
            return super().stat(path)
        self.log.append(f"stat {text}")
        if text == self.row.get("git", _GIT):
            owner = self._answer("owner", _UID)
            if isinstance(owner, str):
                raise _fail(owner)
            return mock.Mock(st_uid=owner)
        m = self._answer("mtime", 5000.0)
        if isinstance(m, str):
            raise _fail(m)
        return mock.Mock(st_mtime=m)

    def write(self, fd: int, data: Any) -> int:
        self.log.append(f"write {len(data)}")
        if not self.writes:
            return len(data)
        item = self.writes.pop(0)
        if isinstance(item, str):
            raise _fail(item)
        return int(item)

    def fsync(self, fd: int) -> None:
        self.log.append("fsync")
        if fd in self.created and self.created[fd].get("fsync", "ok") != "ok":
            raise _fail(self.created[fd]["fsync"])

    def close(self, fd: int) -> None:
        if fd == 99:
            self.log.append("dclose")
            return
        if fd in self.created:
            spec = self.created.pop(fd)
            self.log.append("close")
            if spec.get("close", "ok") != "ok":
                raise _fail(spec["close"])
            return
        super().close(fd)

    def mkdir(self, path: Any, mode: int = 0o777) -> None:
        self.log.append(f"mkdir {self._nul(path)}")
        answer = self._answer("mkdir")
        if answer != "ok":
            raise _fail(answer)

    def utime(self, path: Any, times: Any = None) -> None:
        assert times is None
        self.log.append(f"utime {self._nul(path)}")
        answer = self._answer("utime")
        if answer != "ok":
            raise _fail(answer)

    def scandir(self, path: Any) -> _Scan:
        text = self._nul(path)
        self.log.append(f"scandir {text}")
        scan = self._answer("scan", [])
        if isinstance(scan, str):
            raise _fail(scan)
        return _Scan(self, text, scan)

    def unlink(self, path: Any) -> None:
        self.log.append(f"unlink {self._nul(path)}")
        answer = self.unlinks.pop(0) if self.unlinks else "ok"
        if answer != "ok":
            raise _fail(answer)

    def link(self, src: Any, dst: Any) -> None:
        self.log.append(f"link {self._nul(src)} {self._nul(dst)}")
        answer = self._answer("link")
        if answer != "ok":
            raise _fail(answer)

    def geteuid(self) -> int:
        self.log.append("geteuid")
        return int(self._answer("euid", _UID))

    def urandom(self, n: int) -> bytes:
        self.log.append(f"urandom {n}")
        k = self.draws
        self.draws += 1
        return bytes((37 * k + 11 * i + 5) % 256 for i in range(n))

    def monotonic(self) -> float:
        self.log.append("now")
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.log.append(f"pause {seconds!r}")
        self.clock += seconds


_OS_CALLS = (
    "open",
    "stat",
    "fstat",
    "read",
    "close",
    "write",
    "fsync",
    "mkdir",
    "utime",
    "scandir",
    "unlink",
    "link",
    "geteuid",
)


@contextmanager
def _scripted(script: _Script, plat: str) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(rd._scripted(script, plat))
        for name in _OS_CALLS:
            stack.enter_context(mock.patch.object(os, name, getattr(script, name)))
        stack.enter_context(mock.patch.object(random, "_urandom", script.urandom))
        stack.enter_context(mock.patch.object(time, "monotonic", script.monotonic))
        yield


def _row(name: str, **spec: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"n": name, **spec}
    script = _Script(row)
    try:
        with _scripted(script, row.get("plat", "posix")):
            got = ct.write(
                row.get("git", _GIT),
                policy=row.get("policy", True),
                bare=row.get("bare", False),
            )
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        row["x"] = type(exc).__name__
    else:
        row["r"] = {"status": got.status, "token": got.token, "detail": got.detail}
        row["ok"] = got.ok
    if script.passes:
        # A read list longer than the deadline lets through is cut to what
        # was read, so the row holds the script the run consumed.
        assert len(script.passes) < len(row["reads"]), name
        row["reads"] = row["reads"][: len(row["reads"]) - len(script.passes)]
    row["log"] = script.log
    return row


_ABSENT = {"open": "ENOENT"}
_EMPTY: dict[str, Any] = {}
_CUT = rd._whole(_HEAD + "0123")
_TOKEN = rd._whole(_FILE)
_EXIST = {"open": "EEXIST"}


def _race(name: str, reads: list[Any], **spec: Any) -> dict[str, Any]:
    return _row(name, reads=[_ABSENT, *reads], link="EEXIST", **spec)


def _rows() -> list[dict[str, Any]]:
    long_absent = [_ABSENT] * 40
    return [
        _row("policy off", policy=False),
        _row("policy off and bare", policy=False, bare=True),
        _row("bare", bare=True),
        _row("foreign owner", owner=0),
        _row("owner stat refused", owner="EACCES"),
        _row("owner stat absent", owner="ENOENT"),
        _row("owner stat eio", owner="EIO"),
        _row("owner stat read-only fs", owner="EROFS"),
        _row("win32 skips the owner", plat="win32", owner=0, reads=[_ABSENT]),
        _row("nul in the git dir", git="r/\0"),
        _row("nul in the git dir, win32", git="r/\0", plat="win32"),
        _row("existing token", reads=[_TOKEN]),
        _row("existing malformed", reads=[rd._whole("hello\n")]),
        _row("existing empty twice", reads=[_EMPTY, _EMPTY]),
        _row("existing cut short then token", reads=[_CUT, _TOKEN]),
        _row("existing unreadable", reads=[{"open": "EACCES"}]),
        _row("existing fifo", reads=[{"fstat": "fifo"}]),
        _row("fresh", reads=[_ABSENT]),
        _row("fresh, info exists", reads=[_ABSENT], mkdir="EEXIST"),
        _row("fresh, trailing slash", git="r/.git/", reads=[_ABSENT]),
        _row("fresh, relative", git="rel/.git", reads=[_ABSENT]),
        _row("fresh, win32", plat="win32", reads=[_ABSENT]),
        _row("mkdir refused", reads=[_ABSENT], mkdir="EACCES"),
        _row("mkdir eperm", reads=[_ABSENT], mkdir="EPERM"),
        _row("mkdir read-only", reads=[_ABSENT], mkdir="EROFS"),
        _row("mkdir no git dir", reads=[_ABSENT], mkdir="ENOENT"),
        _row("mkdir not a folder", reads=[_ABSENT], mkdir="ENOTDIR"),
        _row("mkdir no space", reads=[_ABSENT], mkdir="ENOSPC"),
        _row("temp open exists", reads=[_ABSENT], creates=[_EXIST]),
        _row("temp open refused", reads=[_ABSENT], creates=[{"open": "EACCES"}]),
        _row("temp open eio", reads=[_ABSENT], creates=[{"open": "EIO"}]),
        _row(
            "temp short writes",
            reads=[_ABSENT],
            creates=[{"writes": [1, 10, 2]}],
        ),
        _row("temp write overlong count", reads=[_ABSENT], creates=[{"writes": [99]}]),
        _row(
            "temp write no space",
            reads=[_ABSENT],
            creates=[{"writes": [5, "ENOSPC"]}],
        ),
        _row(
            "temp write fails, unlink fails",
            reads=[_ABSENT],
            creates=[{"writes": ["EIO"]}],
            unlinks=["EACCES"],
        ),
        _row(
            "temp write fails, win32 unlink retried",
            plat="win32",
            reads=[_ABSENT],
            creates=[{"writes": ["EIO"]}],
            unlinks=["EACCES", "EACCES", "ok"],
        ),
        _row(
            "temp write fails, win32 unlink gives up",
            plat="win32",
            reads=[_ABSENT],
            creates=[{"writes": ["EIO"]}],
            unlinks=["EACCES"] * 5,
        ),
        _row(
            "temp write fails, win32 unlink other error",
            plat="win32",
            reads=[_ABSENT],
            creates=[{"writes": ["EIO"]}],
            unlinks=["ENOENT"],
        ),
        _row("temp fsync fails", reads=[_ABSENT], creates=[{"fsync": "EIO"}]),
        _row("temp close fails", reads=[_ABSENT], creates=[{"close": "EIO"}]),
        _row(
            "temp write and close fail",
            reads=[_ABSENT],
            creates=[{"writes": ["ENOSPC"], "close": "EROFS"}],
        ),
        _row("utime fails", reads=[_ABSENT], utime="EPERM", scan=[{"name": "x"}]),
        _row("temp stat fails", reads=[_ABSENT], mtime="EIO", scan=[{"name": "x"}]),
        _row(
            "sweep",
            reads=[_ABSENT],
            mtime=5000.0,
            scan=[
                {"name": "bettermemory.tmp-OLD", "m": 1399.0},
                {"name": "bettermemory.tmp-EDGE", "m": 1400.0},
                {"name": "bettermemory.tmp-NEAR", "m": 1399.9999999999998},
                {"name": "bettermemory.tmp-YOUNG", "m": 4999.0},
                {"name": "bettermemory.tmp-AHEAD", "m": 9999.0},
                {"name": "bettermemory.tmp-ERR", "m": "ENOENT"},
                {"name": "bettermemory.tmp-", "m": 1.0},
                {"name": "bettermemory.tmp-claim", "m": 0.0},
                {"name": "bettermemory.TMP-OLD", "m": 1.0},
                {"name": "bettermemory", "m": 1.0},
                {"name": "exclude", "m": 1.0},
                {"name": "xbettermemory.tmp-OLD", "m": 1.0},
            ],
            unlinks=["ok", "EACCES", "ok"],
        ),
        _row(
            "sweep, negative clock",
            reads=[_ABSENT],
            mtime=-10.0,
            scan=[{"name": "bettermemory.tmp-A", "m": -3610.5}],
        ),
        _row(
            "sweep, iteration fails",
            reads=[_ABSENT],
            scan=[
                {"name": "bettermemory.tmp-A", "m": 1.0},
                {"err": "EIO"},
                {"name": "bettermemory.tmp-B", "m": 1.0},
            ],
        ),
        _row("sweep, scandir refused", reads=[_ABSENT], scan="EACCES"),
        _row(
            "sweep, win32 unlink not retried",
            plat="win32",
            reads=[_ABSENT],
            scan=[{"name": "bettermemory.tmp-A", "m": 1.0}],
            unlinks=["EACCES"],
        ),
        _row("link other error, create", reads=[_ABSENT], link="EPERM"),
        _row("link xdev, create", reads=[_ABSENT], link="EXDEV"),
        _row("link emlink, create", reads=[_ABSENT], link="EMLINK"),
        _row(
            "link refused, create refused",
            reads=[_ABSENT],
            link="EPERM",
            creates=[{}, {"open": "EACCES"}],
        ),
        _row(
            "link refused, create eio",
            reads=[_ABSENT],
            link="EIO",
            creates=[{}, {"open": "EIO"}],
        ),
        _row(
            "link refused, create write fails",
            reads=[_ABSENT],
            link="EPERM",
            creates=[{}, {"writes": ["ENOSPC"]}],
        ),
        _row(
            "link refused, create close fails",
            reads=[_ABSENT],
            link="EPERM",
            creates=[{}, {"close": "EROFS"}],
        ),
        _row(
            "link refused, create exists, token",
            reads=[_ABSENT, _TOKEN],
            link="EPERM",
            creates=[{}, _EXIST],
        ),
        _row(
            "link refused, create exists, absent",
            reads=[_ABSENT, *long_absent],
            link="EPERM",
            creates=[{}, _EXIST],
        ),
        _row(
            "link refused, temp unlink fails",
            reads=[_ABSENT],
            link="EPERM",
            unlinks=["EIO"],
        ),
        _race("race, token", [_TOKEN]),
        _race("race, malformed", [rd._whole("hello\n")]),
        _race("race, absent then token", [_ABSENT, _ABSENT, _TOKEN]),
        _race("race, empty then token", [_EMPTY, _EMPTY, _TOKEN]),
        _race("race, unreadable then token", [{"open": "EACCES"}, _TOKEN]),
        _race("race, cut short then token", [_CUT, _CUT, _TOKEN]),
        _race("race, absent to the deadline", long_absent),
        _race("race, empty to the deadline", [_EMPTY] * 40),
        _race("race, unreadable to the deadline", [{"open": "EACCES"}] * 40),
        _race("race, cut short to the deadline", [_CUT] * 80),
        _race("race, eio open retried", [{"open": "EIO", "reads": [rd._t(_FILE)]}]),
        _race("race, fifo", [{"fstat": "fifo"}]),
        _race("race, win32 unlink retried", [_TOKEN], plat="win32", unlinks=["EPERM"]),
        _row("link enoent, create", reads=[_ABSENT], link="ENOENT"),
    ]


def generate() -> dict[str, bytes]:
    rows = _rows()
    return {
        "checkout_token_write.jsonl": jsonl(
            [{"fixture": "checkout_token_write", "count": len(rows)}, *rows]
        )
    }


def _real_answer(got: ct.TokenWrite, git: Path) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": got.status, "detail": got.detail}
    if got.status == "written":
        found = ct._read_at(git / "info" / ct.TOKEN_NAME)
        answer["placed"] = found.state == "present" and found.token == got.token
    else:
        answer["token"] = got.token
    info = git / "info"
    answer["info"] = sorted(p.name for p in info.iterdir()) if info.is_dir() else None
    return answer


def generate_posix() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)

        def git(name: str, info: bool = True) -> Path:
            path = root / name / ".git"
            path.mkdir(parents=True)
            if info:
                (path / "info").mkdir()
            return path

        cases: list[tuple[str, Path]] = []
        cases.append(("fresh", git("fresh")))
        cases.append(("no info", git("noinfo", info=False)))
        g = git("token")
        (g / "info" / ct.TOKEN_NAME).write_bytes(ct.render(_TOK))
        cases.append(("existing token", g))
        g = git("malformed")
        (g / "info" / ct.TOKEN_NAME).write_bytes(b"hello\n")
        cases.append(("existing malformed", g))
        g = git("folder")
        (g / "info" / ct.TOKEN_NAME).mkdir()
        cases.append(("token name a folder", g))
        g = git("stale")
        ages = (
            ("bettermemory.tmp-OLD", 7200),
            ("bettermemory.tmp-NEW", 60),
            ("other.tmp-OLD", 7200),
        )
        for name, age in ages:
            (g / "info" / name).write_bytes(b"x")
            past = time.time() - age
            os.utime(g / "info" / name, (past, past))
        cases.append(("stale temps", g))
        cases.append(("missing git dir", root / "missing" / ".git"))
        for name, path in cases:
            got = ct.write(path, policy=True, bare=False)
            rows.append({"n": name, "r": _real_answer(got, path)})
        if getattr(os, "geteuid")() != 0:  # noqa: B009 - POSIX only
            g = git("readonly")
            (g / "info").chmod(0o555)
            try:
                got = ct.write(g, policy=True, bare=False)
            finally:
                (g / "info").chmod(0o755)
            rows.append({"n": "read-only info", "r": _real_answer(got, g)})
    return {
        "checkout_token_write_posix.jsonl": jsonl(
            [{"fixture": "checkout_token_write_posix", "count": len(rows)}, *rows]
        )
    }
