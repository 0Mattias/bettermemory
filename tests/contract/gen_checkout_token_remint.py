"""Fixtures: the checkout token's re-mint (``checkout_token_remint.jsonl``,
``checkout_token_remint_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/checkout_token/remint.rs``: `remint`,
`_replace_named`, `_claim`, `_wait_for`, `_take`, `_blocked`, `_holder`,
`_claim_read`, `_break`, `_break_marks`, `_release`, `_remove_holding`
and `replace_atomic`'s retry.

``checkout_token_remint.jsonl`` runs `remint` over the write file's
scripted OS (`gen_checkout_token_write`), extended: ``os.link`` takes the
next of the row's ``links`` (past the list ``ok``), ``os.replace`` the
next of ``replaces``, ``os.lstat`` the next of ``lstats`` (``reg``,
``dir`` or an errno name; past the list ``reg``), and ``os.stat`` of a
break mark the next of ``marks`` (an ``st_mtime`` or an errno name). Every
``_read_text``, of the token or of the claim, takes the next of
``reads``, past the list a read that finds nothing (``ENOENT``). The row's ``rep`` is the lineage replaced (token, device,
inode) or null, ``sib`` is ``only_siblings``. POSIX only, as the write
file is: on Windows the logged paths are spelled with backslashes and
errno names are the host's.

``checkout_token_remint_posix.jsonl`` (POSIX only) runs `remint` over
real git directories in a scratch folder: the token written is random, so
a row holds whether the file then reads as it, whether it succeeds the
lineage, and the names left in ``info``.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import checkout_token as ct

from . import gen_checkout_token_read as rd
from . import gen_checkout_token_write as wr
from ._common import jsonl

_TOK = rd._TOK
_OTHER = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"
_REP = [_TOK, "1", "2"]
_SIB = "0000000000000" + ct._link(ct.Lineage(*_REP), "0000000000000")

_ABSENT = {"open": "ENOENT"}
_TOKEN = rd._whole(rd._FILE)
_LSTAT = os.lstat


def _file(token: str) -> dict[str, Any]:
    return rd._whole(ct.render(token).decode("ascii"))


def _holds(text: str) -> dict[str, Any]:
    return rd._whole(text)


class _Script(wr._Script):
    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(row)
        self.links = list(row.get("links", []))
        self.replaces = list(row.get("replaces", []))
        self.lstats = list(row.get("lstats", []))
        self.marks = list(row.get("marks", []))

    def link(self, src: Any, dst: Any) -> None:
        self.log.append(f"link {self._nul(src)} {self._nul(dst)}")
        answer = self.links.pop(0) if self.links else "ok"
        if answer != "ok":
            raise wr._fail(answer)

    def replace(self, src: Any, dst: Any) -> None:
        self.log.append(f"replace {self._nul(src)} {self._nul(dst)}")
        answer = self.replaces.pop(0) if self.replaces else "ok"
        if answer != "ok":
            raise wr._fail(answer)

    def open(self, path: Any, flags: int, mode: int = 0o777) -> int:
        if flags & rd._NONBLOCK and not flags & os.O_CREAT and not self.passes:
            self.passes.append(_ABSENT)
        return super().open(path, flags, mode)

    def lstat(self, path: Any) -> Any:
        if ct._CLAIM_NAME not in os.fspath(path):
            # Not the re-mint's: the test session's own guard, say.
            return _LSTAT(path)
        self.log.append(f"lstat {self._nul(path)}")
        answer = self.lstats.pop(0) if self.lstats else "reg"
        return self._mode(answer)

    def stat(self, path: Any) -> Any:
        text = self._nul(path)
        if ct._BREAK_PREFIX not in text:
            return super().stat(path)
        self.log.append(f"stat {text}")
        m = self.marks.pop(0)
        if isinstance(m, str):
            raise wr._fail(m)
        return mock.Mock(st_mtime=m)


@contextmanager
def _scripted(script: _Script, plat: str) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(wr._scripted(script, plat))
        for name in ("lstat", "replace", "link", "stat"):
            stack.enter_context(mock.patch.object(os, name, getattr(script, name)))
        yield


def _row(name: str, **spec: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"n": name, "rep": _REP, **spec}
    script = _Script(row)
    rep = row["rep"]
    try:
        with _scripted(script, row.get("plat", "posix")):
            got = ct.remint(
                row.get("git", wr._GIT),
                policy=row.get("policy", True),
                bare=row.get("bare", False),
                replacing=None if rep is None else ct.Lineage(*rep),
                only_siblings=row.get("sib", False),
            )
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        assert not isinstance(exc, AssertionError), (name, exc)
        row["x"] = type(exc).__name__
    else:
        row["r"] = {"status": got.status, "token": got.token, "detail": got.detail}
        row["ok"] = got.ok
    if script.passes:
        # A read list longer than the run used is cut to what was read.
        row["reads"] = row["reads"][: len(row["reads"]) - len(script.passes)]
    for key in ("links", "replaces", "lstats", "marks"):
        assert not getattr(script, key), (name, key)
    row["log"] = script.log
    return row


def _held(nonce: str, waits: int) -> list[Any]:
    """The reads of a wait that finds the claim holding `nonce` each time."""
    return [_holds(nonce)] * waits


# Pauses of a whole wait: 0.01, 0.02, 0.04, 0.08, 0.16, then 0.2 six
# times, the last cut to what is left; twelve looks at the claim.
_WAITS = 12
_N = "HELDHELDHELD"


def _rows() -> list[dict[str, Any]]:
    busy = ["EEXIST"] * _WAITS
    return [
        _row("policy off", policy=False),
        _row("bare", bare=True),
        _row("foreign owner", owner=0),
        _row("no lineage", rep=None),
        _row("no lineage, win32", rep=None, plat="win32"),
        _row("no lineage, replace refused", rep=None, replaces=["EACCES"]),
        _row(
            "no lineage, win32 replace retried",
            rep=None,
            plat="win32",
            replaces=["EACCES", "EACCES", "ok"],
        ),
        _row(
            "no lineage, win32 replace gives up",
            rep=None,
            plat="win32",
            replaces=["EACCES"] * 5,
        ),
        _row(
            "no lineage, win32 replace other error",
            rep=None,
            plat="win32",
            replaces=["EXDEV"],
        ),
        _row("mkdir refused", mkdir="EACCES"),
        _row("temp open refused", creates=[{"open": "EROFS"}]),
        _row("claim taken first try, token named", reads=[_TOKEN, _holds("x")]),
        _row("claim taken, own create refused", creates=[{}, {"open": "EACCES"}]),
        _row("claim taken, own write fails", creates=[{}, {"writes": ["ENOSPC"]}]),
        _row(
            "claim taken, own write and close fail",
            creates=[{}, {"writes": ["ENOSPC"], "close": "EIO"}],
        ),
        _row(
            "claim taken, absent token",
            reads=[_ABSENT, _ABSENT],
        ),
        _row(
            "claim taken, malformed token replaced",
            reads=[rd._whole("hello\n"), _ABSENT],
        ),
        _row("claim taken, other token", reads=[_file(_OTHER), _ABSENT]),
        _row(
            "claim taken, other token, only siblings",
            sib=True,
            reads=[_file(_OTHER), _ABSENT],
        ),
        _row("claim taken, sibling", reads=[_file(_SIB), _ABSENT]),
        _row(
            "claim taken, sibling, only siblings",
            sib=True,
            reads=[_file(_SIB), _ABSENT],
        ),
        _row(
            "claim taken, replaced token, only siblings",
            sib=True,
            reads=[_TOKEN, _ABSENT],
        ),
        _row(
            "claim taken, unreadable token",
            reads=[{"open": "EACCES"}, _ABSENT],
        ),
        _row(
            "claim taken, replace refused",
            reads=[_TOKEN, _ABSENT],
            replaces=["EROFS"],
        ),
        _row(
            "claim taken, win32 replace retried",
            plat="win32",
            reads=[_TOKEN, _ABSENT],
            replaces=["EPERM", "ok"],
        ),
        _row(
            "claim release finds it held",
            reads=[_TOKEN, _holds("9XD6AW3VGT8S"), _ABSENT],
        ),
        _row(
            "claim release finds another's",
            reads=[_TOKEN, _holds("OTHEROTHERXX")],
        ),
        _row(
            "claim release, unreadable then gone",
            reads=[_TOKEN, {"open": "EACCES"}, _ABSENT],
        ),
        _row(
            "claim release, unlink refused then gone",
            reads=[_TOKEN, _holds("9XD6AW3VGT8S"), _ABSENT],
            unlinks=["ok", "EACCES"],
        ),
        _row(
            "claim release, unlink finds it gone",
            reads=[_TOKEN, _holds("9XD6AW3VGT8S")],
            unlinks=["ok", "ENOENT"],
        ),
        _row(
            "claim release, win32 unlink retried",
            plat="win32",
            reads=[_TOKEN, _holds("9XD6AW3VGT8S"), _holds("9XD6AW3VGT8S"), _ABSENT],
            unlinks=["ok", "EACCES", "ok"],
        ),
        _row(
            "claim release, win32 unlink taken meanwhile",
            plat="win32",
            reads=[_TOKEN, _holds("9XD6AW3VGT8S"), _holds("NEWNEWNEWNEW")],
            unlinks=["ok", "EACCES"],
        ),
        _row(
            "claim release, read close fails",
            reads=[_TOKEN, {**_holds("9XD6AW3VGT8S"), "close": "EIO"}],
        ),
        _row(
            "link refused, exclusive create",
            links=["EPERM"],
            reads=[_TOKEN, _ABSENT],
        ),
        _row(
            "link refused, create exists then taken",
            links=["EXDEV", "EXDEV"],
            creates=[{}, {}, {"open": "EEXIST"}],
            lstats=["reg"],
            reads=[_holds("OTHEROTHERXX"), _TOKEN, _ABSENT],
        ),
        _row(
            "link refused, create refused",
            links=["EPERM"],
            creates=[{}, {}, {"open": "EACCES"}],
        ),
        _row(
            "link refused, create refused, win32",
            plat="win32",
            links=["EPERM"],
            creates=[{}, {}, {"open": "EACCES"}],
            lstats=["reg"],
            reads=[_holds("OTHEROTHERXX"), _TOKEN, _ABSENT],
        ),
        _row(
            "link refused, create write fails",
            links=["EPERM"],
            creates=[{}, {}, {"writes": ["EIO"]}],
        ),
        _row(
            "link refused, create close fails",
            links=["EPERM"],
            creates=[{}, {}, {"close": "EIO"}],
        ),
        _row(
            "blocked claim",
            links=["EEXIST"],
            lstats=["dir"],
            reads=[_TOKEN],
        ),
        _row(
            "blocked claim, other token first",
            links=["EEXIST"],
            lstats=["dir"],
            reads=[_file(_OTHER)],
        ),
        _row(
            "lstat refused is not blocked",
            links=["EEXIST", "ok"],
            lstats=["EACCES"],
            reads=[_holds("OTHEROTHERXX"), _TOKEN, _ABSENT],
        ),
        _row(
            "held through the wait and broken",
            links=[*busy, "ok"],
            reads=[
                *_held(_N, _WAITS),
                _holds(_N),
                _holds(_N),
                _ABSENT,
                _TOKEN,
                _ABSENT,
            ],
        ),
        _row(
            "held through the wait, not taken after the break",
            links=[*busy, "EEXIST"],
            reads=[*_held(_N, _WAITS), _holds(_N), _holds(_N), _ABSENT, _TOKEN],
        ),
        _row(
            "held through the wait, a live mark",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}],
            marks=[4999.0],
            reads=[*_held(_N, _WAITS), _TOKEN],
        ),
        _row(
            "held through the wait, a mark at the bound",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}],
            marks=[4995.0],
            reads=[*_held(_N, _WAITS), _TOKEN],
        ),
        _row(
            "held through the wait, a stale mark",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}],
            marks=[4994.5],
            reads=[*_held(_N, _WAITS), _holds("OTHEROTHERXX"), _TOKEN],
        ),
        _row(
            "held through the wait, two stale marks",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}, {"open": "EEXIST"}],
            marks=[1.0, 2.0],
            reads=[*_held(_N, _WAITS), _ABSENT, _TOKEN],
        ),
        _row(
            "held through the wait, mark stat refused",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}],
            marks=["EACCES"],
            reads=[*_held(_N, _WAITS), _TOKEN],
        ),
        _row(
            "held through the wait, no clock",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EEXIST"}],
            mtime="EIO",
            scan=[],
            reads=[*_held(_N, _WAITS), _TOKEN],
        ),
        _row(
            "held through the wait, mark create refused",
            links=[*busy],
            creates=[{}, {}, {"open": "EACCES"}],
            reads=[*_held(_N, _WAITS)],
        ),
        _row(
            "held through the wait, win32 mark create refused",
            plat="win32",
            links=[*busy, "EEXIST"],
            creates=[{}, {}, {"open": "EACCES"}],
            reads=[*_held(_N, _WAITS), _TOKEN],
        ),
        _row(
            "held through the wait, mark create eio",
            links=[*busy],
            creates=[{}, {}, {"open": "EIO"}],
            reads=[*_held(_N, _WAITS)],
        ),
        _row(
            "held through the wait, mark close fails",
            links=[*busy],
            creates=[{}, {}, {"close": "EIO"}],
            reads=[*_held(_N, _WAITS)],
        ),
        _row(
            "held through the wait, mark unlink refused",
            links=[*busy, "ok"],
            reads=[*_held(_N, _WAITS), _holds(_N), _ABSENT, _TOKEN, _ABSENT],
            unlinks=["ok", "ok", "EACCES"],
        ),
        _row(
            "held through the wait, claim unlink refused to the end",
            links=[*busy, "EEXIST"],
            reads=[*_held(_N, _WAITS), *([_holds(_N)] * 24), _TOKEN],
            unlinks=["ok", *(["EIO"] * 12)],
        ),
        _row(
            "changing holders are not broken",
            links=[*busy],
            reads=[_holds(f"H{i:011d}") for i in range(_WAITS)],
        ),
        _row(
            "holder changes once, then holds to the end, not broken",
            links=[*busy],
            reads=[_holds("H00000000000"), *([_holds("H00000000001")] * (_WAITS - 1))],
        ),
        _row(
            "first look unreadable, then one holder",
            links=[*busy, "EEXIST"],
            reads=[{"open": "EACCES"}, *_held(_N, _WAITS - 1), _TOKEN],
        ),
        _row(
            "unreadable through the wait",
            links=[*busy],
            reads=[{"open": "EACCES"}] * _WAITS,
        ),
        _row(
            "absent through the wait",
            links=[*busy],
            reads=[_ABSENT] * _WAITS,
        ),
        _row(
            "empty claim through the wait",
            links=[*busy, "ok"],
            reads=[
                *([rd._whole("")] * _WAITS),
                rd._whole(""),
                rd._whole(""),
                _ABSENT,
                _TOKEN,
                _ABSENT,
            ],
        ),
        _row(
            "malformed claim through the wait",
            links=[*busy, "EEXIST"],
            reads=[*([rd._whole("hello\n")] * _WAITS), _ABSENT, _TOKEN],
        ),
        _row(
            "held, then taken in the wait",
            links=["EEXIST", "EEXIST", "ok"],
            reads=[_holds(_N), _holds(_N), _TOKEN, _ABSENT],
        ),
        _row(
            "nul in the git dir",
            git="r/\0",
        ),
    ]


def generate() -> dict[str, bytes]:
    rows = _rows()
    return {
        "checkout_token_remint.jsonl": jsonl(
            [{"fixture": "checkout_token_remint", "count": len(rows)}, *rows]
        )
    }


def _real_answer(
    got: ct.TokenWrite, git: Path, rep: ct.Lineage | None
) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": got.status, "detail": got.detail}
    if got.status == "written":
        found = ct._read_at(git / "info" / ct.TOKEN_NAME)
        answer["placed"] = found.state == "present" and found.token == got.token
        answer["succeeds"] = ct.succeeds(got.token, rep)
    else:
        answer["token"] = got.token
    info = git / "info"
    answer["info"] = sorted(p.name for p in info.iterdir()) if info.is_dir() else None
    return answer


def generate_posix() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    rep = ct.Lineage(*_REP)
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)

        def git(name: str, token: str | None = None) -> Path:
            path = root / name / ".git"
            (path / "info").mkdir(parents=True)
            if token is not None:
                (path / "info" / ct.TOKEN_NAME).write_bytes(ct.render(token))
            return path

        cases: list[tuple[str, Path, ct.Lineage | None, bool]] = [
            ("fresh, no lineage", git("a"), None, False),
            ("token, no lineage", git("b", _OTHER), None, False),
            ("token named", git("c", _TOK), rep, False),
            ("no token, lineage", git("d"), rep, False),
            ("other token", git("e", _OTHER), rep, False),
            ("other token, only siblings", git("f", _OTHER), rep, True),
            ("sibling, only siblings", git("g", _SIB), rep, True),
        ]
        g = git("h", _TOK)
        (g / "info" / ct._CLAIM_NAME).mkdir()
        cases.append(("claim name a folder", g, rep, False))
        g = git("i")
        (g / "info" / ct.TOKEN_NAME).write_bytes(b"hello\n")
        cases.append(("malformed, lineage", g, rep, False))
        cases.append(("missing git dir", root / "missing" / ".git", rep, False))
        for name, path, lineage, sib in cases:
            got = ct.remint(
                path, policy=True, bare=False, replacing=lineage, only_siblings=sib
            )
            rows.append(
                {
                    "n": name,
                    "rep": None if lineage is None else list(lineage),
                    "sib": sib,
                    "r": _real_answer(got, path, lineage),
                }
            )
    return {
        "checkout_token_remint_posix.jsonl": jsonl(
            [{"fixture": "checkout_token_remint_posix", "count": len(rows)}, *rows]
        )
    }
