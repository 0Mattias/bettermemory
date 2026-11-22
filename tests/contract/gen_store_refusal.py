"""Fixtures: the stores a hook refuses before it starts a daemon
(`_store_refusal`), ``store_refusal.jsonl`` and ``store_refusal_posix.jsonl``.

The native build reads them in
``native/crates/bettermemory-core/src/store_refusal.rs``. Each case is a
scratch directory ``@`` built by its steps, then asked `bundle_refusal`
(``b``), `restore_state` (``st``: the state and the record's sorted keys,
or null), `restore_refusal` (``r``) and `store_refusal` (``sr``) of the
store path ``@/<p>`` (default ``s.db``). A text answer is written through
`text_field` (code points where it holds a surrogate), with ``@`` for the
case directory (as given or resolved) and ``/`` for the host's separator;
an exception is ``{"raise": class}``.

Steps: ``["w", rel, pieces]`` writes a file of ``[hex, count]`` pieces;
``["d", rel]`` makes a directory; ``["sparse", rel, size]`` a file
truncated to ``size``; ``["ln", rel, target]`` a hard link;
``["sym", rel, target]`` a symbolic link; ``["fifo", rel]`` a FIFO;
``["hold", rel]`` holds an exclusive ``flock`` on ``rel`` (a directory
or a file) through the calls; ``["rec"]`` takes the record branch, as
where ``flock`` cannot lock the store's directory (``flock`` made to fail
with ENOLCK on a directory), and then only ``st`` is asked.

``store_refusal_posix.jsonl`` holds every case with a lock, a FIFO, a
hard link, a symbolic link, a directory as the store, ``..`` past a
missing directory, or an unreadable record (a directory at the
record path, whose reason is the C library's: only its key and the
refusal are pinned). It is POSIX only: Windows takes the record branch
with ``msvcrt`` and has none of these. The rows have no symbolic link
loop, since `Path.resolve` answers one differently across releases.
"""

from __future__ import annotations

import contextlib
import errno
import importlib
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bettermemory import _store_refusal as sr

from ._common import jsonl, text_field


def _j(text: str) -> list[list[Any]]:
    return [[text.encode("utf-8").hex(), 1]]


def _bytes(pieces: list[list[Any]]) -> bytes:
    return b"".join(bytes.fromhex(h) * n for h, n in pieces)


_BUNDLE = _j('{"format": "bettermemory.backup", "files": []}')
_MARK = '{"started_at": "2026-01-01T00:00:00Z", "pid": 7, "bundle": "b.tar", "stage": "swap"}'

#: Manifest cases beside the store (a free directory, no record).
_MANIFESTS: list[tuple[str, list[list[Any]] | None]] = [
    ("bundle", _BUNDLE),
    ("other_format", _j('{"format": "other"}')),
    ("format_not_str", _j('{"format": 1}')),
    ("dup_format", _j('{"format": "bettermemory.backup", "format": "x"}')),
    ("dup_format_last", _j('{"format": "x", "format": "bettermemory.backup"}')),
    ("a_list", _j('["bettermemory.backup"]')),
    ("a_string", _j('"bettermemory.backup"')),
    ("null", _j("null")),
    ("empty_file", []),
    ("invalid_json", _j('{"format": ')),
    ("bad_utf8", [[b'{"format": "\xff"}'.hex(), 1]]),
    ("lone_surrogate_escape", _j('{"format": "\\ud800"}')),
    ("big_int", [["7b2278223a20", 1], ["31", 5000], ["7d", 1]]),
    (
        "utf16_le_no_bom",
        [['{"format": "bettermemory.backup"}'.encode("utf-16-le").hex(), 1]],
    ),
    (
        "utf16_be_no_bom",
        [['{"format": "bettermemory.backup"}'.encode("utf-16-be").hex(), 1]],
    ),
    (
        "utf16_bom_le",
        [["fffe" + '{"format": "bettermemory.backup"}'.encode("utf-16-le").hex(), 1]],
    ),
    (
        "utf16_bom_be",
        [["feff" + '{"format": "bettermemory.backup"}'.encode("utf-16-be").hex(), 1]],
    ),
    (
        "utf32_le_no_bom",
        [['{"format": "bettermemory.backup"}'.encode("utf-32-le").hex(), 1]],
    ),
    (
        "utf32_be_no_bom",
        [['{"format": "bettermemory.backup"}'.encode("utf-32-be").hex(), 1]],
    ),
    (
        "utf32_bom",
        [
            [
                "fffe0000"
                + '{"format": "bettermemory.backup"}'.encode("utf-32-le").hex(),
                1,
            ]
        ],
    ),
    ("utf16_odd", [['{"format": "x"}'.encode("utf-16-le").hex() + "00", 1]]),
    ("utf8_sig", [["efbbbf" + b'{"format": "bettermemory.backup"}'.hex(), 1]]),
    (
        "utf8_sig_twice",
        [["efbbbfefbbbf" + b'{"format": "bettermemory.backup"}'.hex(), 1]],
    ),
    (
        "at_cap",
        [
            [b'{"format": "bettermemory.backup"'.hex(), 1],
            ["20", (1 << 20) - 33],
            ["7d", 1],
        ],
    ),
    (
        "past_cap_by_one",
        [
            [b'{"format": "bettermemory.backup"'.hex(), 1],
            ["20", (1 << 20) - 32],
            ["7d", 1],
        ],
    ),
]

#: Record cases beside a free store with no manifest.
_RECORDS: list[tuple[str, list[list[Any]]]] = [
    ("record_empty", []),
    ("record_blank", [["200a0d090c0b", 1]]),
    ("record_vt_only", [["0b", 1]]),
    ("record_nul", [["00", 1]]),
    ("record_full", _j(_MARK)),
    ("record_started_only", _j('{"started_at": "t0"}')),
    ("record_pid_only", _j('{"pid": 12}')),
    ("record_pid_bool", _j('{"pid": true}')),
    ("record_pid_float", _j('{"pid": 7.0}')),
    ("record_pid_big", _j('{"pid": 123456789012345678901234567890}')),
    ("record_bundle_only", _j('{"bundle": "/x/y.tar"}')),
    ("record_stage_only", _j('{"stage": "copy"}')),
    (
        "record_wrong_types",
        _j('{"started_at": 1, "bundle": ["x"], "stage": null, "pid": "7"}'),
    ),
    (
        "record_unprintable",
        _j('{"bundle": "a\\nb\\u200b\\u2028", "stage": "x\\ty\\u007f"}'),
    ),
    ("record_lone_surrogate", _j('{"bundle": "\\ud800"}')),
    ("record_dup_keys", _j('{"stage": "a", "stage": "b"}')),
    ("record_empty_object", _j("{}")),
    ("record_list", _j("[1]")),
    ("record_string", _j('"x"')),
    ("record_invalid", _j('{"pid": 4')),
    ("record_bad_utf8", [[b'{"stage": "\xff"}'.hex(), 1]]),
    ("record_utf16_bom", [["fffe" + '{"stage": "le"}'.encode("utf-16-le").hex(), 1]]),
    ("record_utf32_no_bom", [['{"stage": "32"}'.encode("utf-32-be").hex(), 1]]),
    ("record_utf8_sig", [["efbbbf" + b'{"stage": "sig"}'.hex(), 1]]),
    ("record_past_cap_blank", [["20", 1 << 16], [b'{"pid": 1}'.hex(), 1]]),
    ("record_past_cap_cut", [[b'{"stage": "x"'.hex(), 1], ["20", 1 << 16], ["7d", 1]]),
    ("record_at_cap", [[b'{"stage": "x"'.hex(), 1], ["20", (1 << 16) - 14], ["7d", 1]]),
]


def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"n": "nothing", "steps": []},
        {"n": "store_present", "steps": [["w", "s.db", _j("x")]]},
        {"n": "manifest_dir", "steps": [["d", "manifest.json"]]},
        {
            "n": "manifest_sparse_past_cap",
            "steps": [["sparse", "manifest.json", (1 << 20) + 1]],
        },
        {
            "n": "bundle_and_record",
            "steps": [
                ["w", "manifest.json", _BUNDLE],
                ["w", "s.db.restore-lock", _j(_MARK)],
            ],
        },
        {"n": "missing_parent", "p": "no/s.db", "steps": []},
        {"n": "parent_is_file", "p": "f/s.db", "steps": [["w", "f", _j("x")]]},
        {"n": "record_is_dir_rdwr", "steps": [["d", "s.db.restore-lock"], ["rec"]]},
    ]
    for name, pieces in _MANIFESTS:
        out.append({"n": f"manifest_{name}", "steps": [["w", "manifest.json", pieces]]})
    for name, pieces in _RECORDS:
        out.append({"n": name, "steps": [["w", "s.db.restore-lock", pieces]]})
        out.append(
            {"n": f"{name}_rec", "steps": [["w", "s.db.restore-lock", pieces], ["rec"]]}
        )
    return out


def _posix_cases() -> list[dict[str, Any]]:
    mark = ["w", "s.db.restore-lock", _j(_MARK)]
    return [
        {"n": "running", "steps": [mark, ["hold", "."]]},
        {"n": "running_no_record", "steps": [["hold", "."]]},
        {
            "n": "running_blank_record",
            "steps": [["w", "s.db.restore-lock", []], ["hold", "."]],
        },
        {
            "n": "running_bundle_record",
            "steps": [["w", "s.db.restore-lock", _j('{"bundle": "z"}')], ["hold", "."]],
        },
        {"n": "record_held_on_file", "steps": [mark, ["hold", "s.db.restore-lock"]]},
        {"n": "rec_no_record", "steps": [["rec"]]},
        {"n": "rec_held", "steps": [mark, ["hold", "s.db.restore-lock"], ["rec"]]},
        {
            "n": "rec_held_blank",
            "steps": [
                ["w", "s.db.restore-lock", []],
                ["hold", "s.db.restore-lock"],
                ["rec"],
            ],
        },
        {"n": "rec_dir_held_ignored", "steps": [mark, ["hold", "."], ["rec"]]},
        {"n": "unreadable_record", "steps": [["d", "s.db.restore-lock"]]},
        {"n": "manifest_fifo", "steps": [["fifo", "manifest.json"]]},
        {
            "n": "manifest_link_to_bundle",
            "steps": [
                ["d", "b"],
                ["w", "b/m", _BUNDLE],
                ["sym", "manifest.json", "b/m"],
            ],
        },
        {
            "n": "manifest_link_to_fifo",
            "steps": [["fifo", "f"], ["sym", "manifest.json", "f"]],
        },
        {"n": "manifest_dangling_link", "steps": [["sym", "manifest.json", "gone"]]},
        {"n": "hard_link", "steps": [["w", "s.db", _j("x")], ["ln", "t.db", "s.db"]]},
        {
            "n": "hard_link_and_bundle",
            "steps": [
                ["w", "s.db", _j("x")],
                ["ln", "t.db", "s.db"],
                ["w", "manifest.json", _BUNDLE],
            ],
        },
        {
            "n": "hard_link_and_record",
            "steps": [["w", "s.db", _j("x")], ["ln", "t.db", "s.db"], mark],
        },
        {
            "n": "store_link_into_bundle",
            "steps": [
                ["d", "b"],
                ["w", "b/real.db", _j("x")],
                ["w", "b/manifest.json", _BUNDLE],
                ["sym", "s.db", "b/real.db"],
            ],
        },
        {
            "n": "store_link_into_other",
            "steps": [
                ["d", "b"],
                ["w", "b/real.db", _j("x")],
                ["w", "b/manifest.json", _j('{"format": "x"}')],
                ["sym", "s.db", "b/real.db"],
            ],
        },
        {
            "n": "store_dangling_link",
            "steps": [
                ["d", "b"],
                ["w", "b/manifest.json", _BUNDLE],
                ["sym", "s.db", "b/gone.db"],
            ],
        },
        {
            "n": "parent_link_into_bundle",
            "p": "l/s.db",
            "steps": [["d", "b"], ["w", "b/manifest.json", _BUNDLE], ["sym", "l", "b"]],
        },
        {
            "n": "parent_link_dotdot",
            "p": "l/../s.db",
            "steps": [
                ["d", "b/c"],
                ["w", "b/manifest.json", _BUNDLE],
                ["sym", "l", "b/c"],
            ],
        },
        # A link met a second time after it resolved is taken as resolved
        # (the spelled candidate fails at the missing directory).
        {
            "n": "link_met_twice",
            "p": "l/../l/missing/../s.db",
            "steps": [["d", "b"], ["w", "b/manifest.json", _BUNDLE], ["sym", "l", "b"]],
        },
        {"n": "hard_link_dir_store", "p": "d", "steps": [["d", "d/e"]]},
        # A directory's link count, and `..` past a missing directory,
        # are the host's (Windows counts one link and reads `..` lexically).
        {
            "n": "dotdot_name",
            "p": "d/..",
            "steps": [["d", "d"], ["w", "manifest.json", _BUNDLE]],
        },
        {
            "n": "dotdot_record",
            "p": "d/..",
            "steps": [["d", "d"], ["w", "d/...restore-lock", _j(_MARK)]],
        },
        {
            "n": "missing_parent_dotdot",
            "p": "no/../s.db",
            "steps": [["w", "manifest.json", _BUNDLE]],
        },
    ]


@contextlib.contextmanager
def _record_branch() -> Iterator[None]:
    if sys.platform == "win32":
        # Windows takes the record branch already.
        yield
        return
    fcntl: Any = importlib.import_module("fcntl")
    real = fcntl.flock

    def flock(fd: int, op: int) -> None:
        if _is_dir(fd):
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        real(fd, op)

    fcntl.flock = flock
    try:
        yield
    finally:
        fcntl.flock = real


def _is_dir(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def _shown(root: Path, text: str) -> str:
    """The case directory as ``@`` and, within each path that starts at
    ``@`` (up to a space, ``)``, ``;`` or ``,``), the host's separator as
    ``/``. The rest is left alone: `printable` writes backslash escapes."""
    for base in (str(root.resolve()), str(root)):
        text = text.replace(base, "@")
    out = []
    in_path = False
    for ch in text:
        if ch == "@":
            in_path = True
        elif ch in " );,":
            in_path = False
        out.append("/" if in_path and ch == os.sep else ch)
    return "".join(out)


def _answer(root: Path, key: str, call: Any) -> dict[str, Any]:
    try:
        got = call()
    except Exception as exc:  # the class is the answer
        return {key: {"raise": type(exc).__name__}}
    if got is None:
        return {key: None}
    return text_field(key, _shown(root, got))


def _run(base: Path, case: dict[str, Any]) -> dict[str, Any]:
    root = base / case["n"]
    root.mkdir()
    held = []
    rec = False
    for step in case["steps"]:
        op, rest = step[0], step[1:]
        if op == "rec":
            rec = True
            continue
        at = root / rest[0]
        if op == "w":
            at.write_bytes(_bytes(rest[1]))
        elif op == "d":
            at.mkdir(parents=True)
        elif op == "sparse":
            with at.open("wb") as fh:
                fh.truncate(rest[1])
        elif op == "ln":
            os.link(root / rest[1], at)
        elif op == "sym":
            os.symlink(root / rest[1], at)
        elif op == "fifo":
            posix: Any = os  # Windows has no mkfifo; no FIFO case runs there.
            posix.mkfifo(at)
        elif op == "hold":
            fcntl: Any = importlib.import_module("fcntl")
            fd = os.open(at, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
    store = root / case.get("p", "s.db")
    row: dict[str, Any] = {"n": case["n"], "steps": case["steps"]}
    if "p" in case:
        row["p"] = case["p"]
    try:
        if rec:
            with _record_branch():
                row.update(_state(store))
        else:
            row.update(_answer(root, "b", lambda: sr.bundle_refusal(store)))
            row.update(_state(store))
            row.update(_answer(root, "r", lambda: sr.restore_refusal(store)))
            row.update(_answer(root, "sr", lambda: sr.store_refusal(store)))
    finally:
        for fd in held:
            os.close(fd)
    return row


def _state(store: Path) -> dict[str, Any]:
    try:
        state, marker = sr.restore_state(store)
    except Exception as exc:  # the class is the answer
        return {"st": {"raise": type(exc).__name__}}
    return {"st": [state, None if marker is None else sorted(marker)]}


def _file(cases: list[dict[str, Any]]) -> bytes:
    with tempfile.TemporaryDirectory() as raw:
        rows = [_run(Path(raw), case) for case in cases]
    return jsonl([{"count": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"store_refusal.jsonl": _file(_cases())}


def generate_posix() -> dict[str, bytes]:
    return {"store_refusal_posix.jsonl": _file(_posix_cases())}
