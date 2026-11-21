"""Fixtures: the finish of a committed key rotation
(``store_rotation.jsonl``, POSIX only).

The native build reads them in
``native/crates/bettermemory-core/src/store/rotation.rs``. This pins it to
`store_keys`'s `pending_key_path`, `write_key_once`,
`_bound_here_or_nowhere`, `_current_named`, `finish_rotation`,
`_finish_for_signing` (with `_rename_read_attempts` as the case gives it),
`_signs_a_segment` and `KeysDirUnreadableError`'s reason and message.

A file-system row drives the reference over a scratch folder ``<root>``
whose ``keys`` folder is the keys directory of the store id ``STORE_ID``
and whose ``store.db`` is the store file. ``setup`` and ``after`` are
`gen_log_keyfiles`' (a file as hex bytes and mode, a folder and its mode,
a link and its target, ``deep`` a file nested past the JSON limit).
``op`` is one of:

- ``["finish", store, recorded]``: `finish_rotation` with the store file
  ``store`` (relative to ``<root>``, the working directory, or ``<root>``
  spelled in it) and meta's fingerprint ``recorded``;
- ``["signing", store, recorded, attempts]``: `_finish_for_signing` with
  `_rename_read_attempts` answering ``attempts`` and `time.sleep`
  recorded, not slept (``sleeps``);
- ``["once", name, hex]``: `write_key_once`;
- ``["pending"]``: `pending_key_path`.

``out`` is ``key`` (hex or null) with ``log`` (each line's level name and
message), or ``ok``, or ``raised`` (the class, the errno's name). A
message spells the scratch folder ``<root>`` and each ``strerror`` text
of the errnos macOS and glibc share as ``<ENAME>``: the C library's words
are not pinned.

A row with ``part`` ``signs`` is `_signs_a_segment` over segments (each
the fingerprint it is under and its first row or null) and a key; one
with ``part`` ``keysdir`` is `KeysDirUnreadableError(keys_dir, link,
reason)`'s ``reason`` and ``message`` over POSIX paths.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import log as blog
from bettermemory import store_keys

from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_log_keyfiles import _build, _snapshot, _sub, _unlock, _unsub

STORE_ID = "0123456789abcdef0123456789abcdef"
OLD = bytes(range(32))
NEW = bytes(range(32, 64))
OTHER = bytes(range(64, 96))
FN = blog.fingerprint(NEW)
FO = blog.fingerprint(OLD)
DEEP = 100_000

KEYS = ["dir", "keys", 0o700]
KEYS_RO = ["dir", "keys", 0o500]
STORE = ["file", "store.db", b"db".hex(), 0o600]
PEND = f"keys/{STORE_ID}.key.pending"
CUR = f"keys/{STORE_ID}.key"
BIND = f"keys/{STORE_ID}.path"
RET_OLD = f"keys/{STORE_ID}.{FO[:16]}.key"
RET_NEW = f"keys/{STORE_ID}.{FN[:16]}.key"

#: The errnos whose `strerror` a message may hold, by the name it shows.
SHOWN = ("EACCES", "EISDIR", "ENOENT", "ENOTDIR", "EEXIST")


def _f(name: str, data: bytes | str, mode: int = 0o600) -> list[Any]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return ["file", name, raw.hex(), mode]


def _bind(target: str) -> list[Any]:
    return _f(BIND, '{"path": "' + target + '"}')


Scenario = tuple[str, list[list[Any]], list[Any]]


def _finish_scenarios() -> list[Scenario]:
    fin = ["finish", "<root>/store.db", FN]
    base = [KEYS, STORE]
    return [
        ("nothing pending, no key", base, fin),
        ("nothing pending, current is meta's", [*base, _f(CUR, NEW)], fin),
        ("nothing pending, current is another", [*base, _f(CUR, OLD)], fin),
        ("nothing pending, current unreadable", [*base, _f(CUR, NEW, 0)], fin),
        ("nothing pending, current a folder", [*base, ["dir", CUR, 0o700]], fin),
        (
            "pending a dangling link",
            [*base, ["link", PEND, "nowhere"], _f(CUR, NEW)],
            fin,
        ),
        ("pending a folder", [*base, ["dir", PEND, 0o700]], fin),
        ("pending unreadable", [*base, _f(PEND, NEW, 0)], fin),
        ("pending short", [*base, _f(PEND, NEW[:31])], fin),
        ("pending long", [*base, _f(PEND, NEW + b"\n")], fin),
        ("pending empty", [*base, _f(PEND, b"")], fin),
        ("pending another key", [*base, _f(PEND, OTHER), _f(CUR, OLD)], fin),
        (
            "pending, meta names its prefix only",
            [*base, _f(PEND, NEW)],
            ["finish", "<root>/store.db", FN[:16]],
        ),
        (
            "pending, meta upper case",
            [*base, _f(PEND, NEW)],
            ["finish", "<root>/store.db", FN.upper()],
        ),
        ("pending, unbound, no current", [*base, _f(PEND, NEW)], fin),
        ("pending mode 0644 kept", [*base, _f(PEND, NEW, 0o644)], fin),
        ("pending, bound here", [*base, _bind("<root>/store.db"), _f(PEND, NEW)], fin),
        (
            "pending, bound elsewhere",
            [*base, _f("other.db", "x"), _bind("<root>/other.db"), _f(PEND, NEW)],
            fin,
        ),
        (
            "pending, bound to a missing file",
            [*base, _bind("<root>/gone.db"), _f(PEND, NEW)],
            fin,
        ),
        (
            "pending, bound to a link to the store",
            [
                *base,
                ["link", "alias.db", "store.db"],
                _bind("<root>/alias.db"),
                _f(PEND, NEW),
            ],
            fin,
        ),
        (
            "pending, bound to a link, store spelled with a trailing slash",
            [
                *base,
                ["link", "alias.db", "store.db"],
                _bind("<root>/alias.db"),
                _f(PEND, NEW),
            ],
            ["finish", "<root>/store.db/", FN],
        ),
        (
            "pending, bound to a link, store spelled relative",
            [
                *base,
                ["dir", "sub", 0o700],
                ["link", "alias.db", "store.db"],
                _bind("<root>/alias.db"),
                _f(PEND, NEW),
            ],
            ["finish", "sub/../store.db", FN],
        ),
        (
            "pending, unbound, store spelled with a dot and a double slash",
            [*base, _f(PEND, NEW)],
            ["finish", "<root>/.//store.db", FN],
        ),
        (
            "pending a folder, store spelled with a trailing slash",
            [*base, ["dir", PEND, 0o700]],
            ["finish", "<root>/store.db/", FN],
        ),
        (
            "pending, bound to a hard link of the store",
            [
                *base,
                _bind("<root>/hard.db"),
                _f(PEND, NEW),
                ["hard", "hard.db", "store.db"],
            ],
            fin,
        ),
        (
            "pending, store opened through a link",
            [
                *base,
                ["link", "alias.db", "store.db"],
                _bind("<root>/store.db"),
                _f(PEND, NEW),
            ],
            ["finish", "<root>/alias.db", FN],
        ),
        (
            "pending, store spelled with dot-dot",
            [*base, ["dir", "sub", 0o700], _bind("<root>/store.db"), _f(PEND, NEW)],
            ["finish", "<root>/sub/../store.db", FN],
        ),
        (
            "pending, store relative",
            [*base, _bind("<root>/store.db"), _f(PEND, NEW)],
            ["finish", "store.db", FN],
        ),
        ("pending, binding relative", [*base, _bind("store.db"), _f(PEND, NEW)], fin),
        (
            "pending, binding not json",
            [*base, _f(BIND, "not json"), _f(PEND, NEW)],
            fin,
        ),
        (
            "pending, binding a folder",
            [*base, ["dir", BIND, 0o700], _f(PEND, NEW)],
            fin,
        ),
        (
            "pending, binding unreadable",
            [*base, _bind("<root>/store.db"), _f(PEND, NEW)] + [["chmod", BIND, 0]],
            fin,
        ),
        (
            "pending, binding nested past the limit",
            [*base, ["deep", BIND, DEEP, 0o600], _f(PEND, NEW)],
            fin,
        ),
        (
            "pending, current is meta's already",
            [*base, _f(PEND, NEW), _f(CUR, NEW)],
            fin,
        ),
        (
            "pending, current another, retired written",
            [*base, _f(PEND, NEW), _f(CUR, OLD, 0o640)],
            fin,
        ),
        (
            "pending, current another, retired there",
            [*base, _f(PEND, NEW), _f(CUR, OLD), _f(RET_OLD, OLD, 0o400)],
            fin,
        ),
        (
            "pending, current another, retired holds another",
            [*base, _f(PEND, NEW), _f(CUR, OLD), _f(RET_OLD, OTHER)],
            fin,
        ),
        (
            "pending, current another, retired a folder",
            [*base, _f(PEND, NEW), _f(CUR, OLD), ["dir", RET_OLD, 0o700]],
            fin,
        ),
        (
            "pending, current another, retired unreadable",
            [*base, _f(PEND, NEW), _f(CUR, OLD), _f(RET_OLD, OLD, 0)],
            fin,
        ),
        (
            "pending, current another, retired a dangling link",
            [*base, _f(PEND, NEW), _f(CUR, OLD), ["link", RET_OLD, "nowhere"]],
            fin,
        ),
        (
            "pending, current short",
            [*base, _f(PEND, NEW), _f(CUR, b"k")],
            ["finish", "<root>/store.db", FN],
        ),
        ("pending, current unreadable", [*base, _f(PEND, NEW), _f(CUR, OLD, 0)], fin),
        ("pending, current a folder", [*base, _f(PEND, NEW), ["dir", CUR, 0o700]], fin),
        (
            "pending, current a dangling link",
            [*base, _f(PEND, NEW), ["link", CUR, "nowhere"]],
            fin,
        ),
        (
            "pending, current a link to the old key",
            [*base, _f(PEND, NEW), _f("old.key", OLD), ["link", CUR, "../old.key"]],
            fin,
        ),
        (
            "pending a link to the new key",
            [*base, _f("new.key", NEW), ["link", PEND, "../new.key"]],
            fin,
        ),
        ("pending, keys read only, no current", [KEYS_RO, STORE, _f(PEND, NEW)], fin),
        (
            "pending, keys read only, current another",
            [KEYS_RO, STORE, _f(PEND, NEW), _f(CUR, OLD)],
            fin,
        ),
        (
            "pending, keys read only, current meta's",
            [KEYS_RO, STORE, _f(PEND, NEW), _f(CUR, NEW)],
            fin,
        ),
        (
            "pending, retired name taken by meta's key",
            [*base, _f(PEND, NEW), _f(CUR, OLD), _f(RET_NEW, NEW)],
            fin,
        ),
        ("no keys folder", [STORE], fin),
        ("keys folder a file", [_f("keys", "x"), STORE], fin),
    ]


def _signing_scenarios() -> list[Scenario]:
    base = [KEYS, STORE]
    return [
        (
            "signing, finished",
            [*base, _f(PEND, NEW)],
            ["signing", "<root>/store.db", FN, 1],
        ),
        (
            "signing, nothing pending",
            [*base, _f(CUR, NEW)],
            ["signing", "<root>/store.db", FN, 1],
        ),
        (
            "signing, pending another, once",
            [*base, _f(PEND, OTHER)],
            ["signing", "<root>/store.db", FN, 1],
        ),
        (
            "signing, pending another, five",
            [*base, _f(PEND, OTHER)],
            ["signing", "<root>/store.db", FN, 5],
        ),
        (
            "signing, pending a dangling link, five",
            [*base, ["link", PEND, "nowhere"]],
            ["signing", "<root>/store.db", FN, 5],
        ),
        (
            "signing, pending unreadable, five",
            [*base, _f(PEND, NEW, 0)],
            ["signing", "<root>/store.db", FN, 5],
        ),
        (
            "signing, bound elsewhere, three",
            [*base, _f("other.db", "x"), _bind("<root>/other.db"), _f(PEND, NEW)],
            ["signing", "<root>/store.db", FN, 3],
        ),
        ("signing, nothing pending, five", base, ["signing", "<root>/store.db", FN, 5]),
        (
            "signing, finished, five",
            [*base, _f(PEND, NEW), _f(CUR, OLD)],
            ["signing", "<root>/store.db", FN, 5],
        ),
        (
            "signing, zero attempts",
            [*base, _f(PEND, NEW)],
            ["signing", "<root>/store.db", FN, 0],
        ),
    ]


def _once_scenarios() -> list[Scenario]:
    return [
        ("once, fresh", [KEYS], ["once", "keys/k", NEW.hex()]),
        ("once, empty key", [KEYS], ["once", "keys/k", ""]),
        (
            "once, a file there",
            [KEYS, _f("keys/k", OLD, 0o644)],
            ["once", "keys/k", NEW.hex()],
        ),
        (
            "once, a folder there",
            [KEYS, ["dir", "keys/k", 0o700]],
            ["once", "keys/k", NEW.hex()],
        ),
        (
            "once, a dangling link there",
            [KEYS, ["link", "keys/k", "nowhere"]],
            ["once", "keys/k", NEW.hex()],
        ),
        (
            "once, a link to a file there",
            [KEYS, _f("t", OLD), ["link", "keys/k", "../t"]],
            ["once", "keys/k", NEW.hex()],
        ),
        ("once, no folder", [], ["once", "keys/k", NEW.hex()]),
        ("once, folder read only", [KEYS_RO], ["once", "keys/k", NEW.hex()]),
        ("once, under a file", [_f("keys", "x")], ["once", "keys/k", NEW.hex()]),
        ("pending path", [KEYS], ["pending"]),
    ]


def _scenarios() -> list[Scenario]:
    return [*_finish_scenarios(), *_signing_scenarios(), *_once_scenarios()]


def _shown(text: str, root: Path) -> str:
    text = _sub(text, root)
    for name in SHOWN:
        text = text.replace(os.strerror(getattr(errno, name)), f"<{name}>")
    return text


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[list[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append([record.levelname, record.getMessage()])


def _setup(root: Path, setup: list[list[Any]]) -> None:
    plain = [s for s in setup if s[0] not in ("hard", "chmod")]
    _build(root, plain)
    for step in setup:
        if step[0] == "hard":
            os.link(root / step[2], root / step[1])
        elif step[0] == "chmod":
            os.chmod(root / step[1], step[2])


def _run(op: list[Any], root: Path) -> dict[str, Any]:
    ring = blog.KeyRing(root / "keys", STORE_ID)
    name = op[0]
    if name == "pending":
        return {"ok": _sub(str(store_keys.pending_key_path(ring)), root)}
    if name == "once":
        try:
            return {"ok": store_keys.write_key_once(root / op[1], bytes.fromhex(op[2]))}
        except OSError as exc:
            return {"raised": [type(exc).__name__, errno.errorcode.get(exc.errno or 0)]}
    handler = _Lines()
    logger = logging.getLogger("bettermemory.store")
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    sleeps: list[float] = []
    cwd = os.getcwd()
    os.chdir(root)
    try:
        store = Path(_unsub(op[1], root))
        if name == "finish":
            got = store_keys.finish_rotation(ring, store, op[2])
        else:
            with (
                mock.patch.object(store_keys, "_rename_read_attempts", lambda: op[3]),
                mock.patch("time.sleep", sleeps.append),
            ):
                got = store_keys._finish_for_signing(ring, store, op[2])
        out: dict[str, Any] = {"key": None if got is None else got.hex()}
    except RecursionError:
        out = {"raised": ["RecursionError", None]}
    finally:
        os.chdir(cwd)
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
    out["log"] = [[lvl, _shown(text, root)] for lvl, text in handler.lines]
    if name == "signing":
        out["sleeps"] = [repr(s) for s in sleeps]
    return out


def _deep(scenario: Scenario) -> bool:
    return any(step[0] == "deep" for step in scenario[1])


def _files(deep: bool) -> list[dict[str, Any]]:
    refuse_root()
    rows: list[dict[str, Any]] = []
    for name, setup, op in _scenarios():
        if _deep((name, setup, op)) != deep:
            continue
        # By its real path (macOS's temporary folder is under a link), so a
        # resolved path and the root spell alike.
        scratch = Path(os.path.realpath(tempfile.mkdtemp(prefix="bm-rotation-")))
        root = scratch / "r"
        root.mkdir()
        try:
            _setup(root, setup)
            out = _run(op, root)
            after = _snapshot(root)
        finally:
            _unlock(scratch)
            shutil.rmtree(scratch)
        rows.append(
            {"case": name, "setup": setup, "op": op, "out": out, "after": after}
        )
    return rows


def _row(key: bytes, seq: int, kind: str, mac: str | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "seq": seq,
        "ts": "2026-01-01T00:00:00+00:00",
        "session": None,
        "kind": kind,
        "payload": "{}",
        "prev_mac": "0" * 64,
    }
    fields["mac"] = mac if mac is not None else blog.compute_mac(key, **fields)
    return fields


def _signs() -> list[dict[str, Any]]:
    good = _row(NEW, 1, "store_created")
    other = _row(OLD, 1, "store_created")
    cases: list[tuple[list[Any], bytes]] = [
        ([], NEW),
        ([[FN, good]], NEW),
        ([[FN, None]], NEW),
        ([[FN, other]], NEW),
        ([[FO, good]], NEW),
        ([[FN.upper(), good]], NEW),
        ([[FO, other], [FN, _row(NEW, 7, "rekey")]], NEW),
        ([[FO, other], [FN, None], [FN, good]], NEW),
        ([[FN, _row(NEW, 1, "store_created", mac=good["mac"].upper())]], NEW),
        ([[FN, _row(NEW, 1, "store_created", mac="")]], NEW),
        ([[FO, other]], OLD),
        ([[blog.fingerprint(b""), _row(b"", 1, "store_created")]], b""),
        ([[FN, good]], NEW + b"\0"),
    ]
    rows = []
    for segments, key in cases:
        built = [
            blog.Segment(
                first_seq=1,
                key_fingerprint=fp,
                first_row=None if row is None else blog.LogRow(**row),
            )
            for fp, row in segments
        ]
        rows.append(
            {
                "part": "signs",
                "segments": segments,
                "key": key.hex(),
                "out": store_keys._signs_a_segment(built, key),
            }
        )
    return rows


def _keysdir() -> list[dict[str, Any]]:
    link = "it is a symbolic link to /vol/keys, which is not there"
    cases = [
        ("/s/keys", "/s/keys", link),
        ("/s/keys", "/s", link),
        ("/s/keys/", "/s/keys", link),
        ("/s/keys", "/s/./keys", link),
        ("/s/keys", "/s", "it it twice"),
        ("/s/keys", "/s", "no prefix"),
        ("/s/keys", "/s", "it "),
        ("/s/keys", "/s", ""),
        ("/k", "//k", link),
        ("//k", "/k", link),
        ("k", "./k", link),
        ("./k", "k", link),
        ("/s/kéys", "/s", "it is a symbolic link to /é, which is not there"),
    ]
    rows = []
    for keys, at, reason in cases:
        exc = store_keys.KeysDirUnreadableError(Path(keys), Path(at), reason)
        rows.append(
            {
                "part": "keysdir",
                "keys_dir": keys,
                "link": at,
                "reason": reason,
                "out": {"reason": exc.reason, "message": exc.message, "str": str(exc)},
            }
        )
    return rows


def _file(name: str, rows: list[dict[str, Any]]) -> bytes:
    header = {
        "fixture": name,
        "cases": len(rows),
        "store_id": STORE_ID,
        "shown": list(SHOWN),
    }
    return jsonl([header, *rows])


def generate() -> dict[str, bytes]:
    rows = [*_files(deep=False), *_signs(), *_keysdir()]
    return {"store_rotation.jsonl": _file("store_rotation", rows)}


def generate_python() -> dict[str, bytes]:
    """The finishes whose binding is nested deeper than `json.loads` recurses.

    Whether it raises RecursionError follows the interpreter and the C
    stack: 3.11 to 3.13, and 3.14 on macOS, raise; 3.14 on Linux reads it.
    """
    rows = _files(deep=True)
    return {"store_rotation_python.jsonl": _file("store_rotation_python", rows)}
