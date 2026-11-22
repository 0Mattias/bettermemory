"""Fixtures: `project_file.py`, unit 1 (``project_file.jsonl``, not gated).

The native build reads them in
``native/crates/bettermemory-core/src/project_file.rs``.

A ``line`` row spells `ProjectFileError.line` as code points (its quoted
escapes, escaped again by JSON, would read as a UNC prefix to the host-fact
guard; its path has no separator, so the host's `Path` keeps it); ``render`` spells `render`
(``null`` where it raises), its name as code points; ``mint`` spells
`mint_id` over given random bytes; ``id`` spells `_ID_RE.fullmatch` of a
text; ``bad`` lists every code point `_bad_char` refuses.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import project_file as pf

from ._common import jsonl
from .gen_checkout_token import _urandom

GOOD_ID = "bmp_" + "0123456789ABCDEFGHJKMNPQRS"
GOOD = f'[project]\nid = "{GOOD_ID}"\nname = "n"\n'


def _other_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, detail in (
        ("bettermemory.toml", "not a regular file"),
        ('a "b"\nc\u2028', "x"),
        ("\x1b[31m\u00e9", "d\u00e9tail"),
    ):
        line = pf.ProjectFileError(Path(path), "toml", detail).line
        rows.append(
            {"k": "line", "path": path, "d": detail, "o": [ord(c) for c in line]}
        )
    # A backslash is left out: escaped twice it reads as a UNC prefix to
    # the host-fact guard (`project_file_kills` holds it).
    every = (
        "".join(chr(c) for c in range(0x80) if c != 0x5C)
        + "\u0085\u2028\u00e9\U0001f600"
    )
    for name, boundary in (
        ("n", False),
        ("n", True),
        (every, False),
        ('"', True),
        ("\ud800", False),
    ):
        project = pf.ProjectFile(
            id=GOOD_ID, name=name, boundary=boundary, path=Path("/x")
        )
        try:
            out: Any = pf.render(project).decode("utf-8")
        except UnicodeEncodeError:
            out = None
        rows.append(
            {"k": "render", "name": [ord(c) for c in name], "b": boundary, "o": out}
        )
    for raw in ("00" * 17, "ff" * 17, "0123456789abcdef0123456789abcdef42"):
        with _urandom(bytes.fromhex(raw)):
            rows.append({"k": "mint", "x": raw, "o": pf.mint_id()})
    for text in (
        GOOD_ID,
        GOOD_ID.lower(),
        GOOD_ID + "\n",
        GOOD_ID[:-1],
        GOOD_ID + "0",
        GOOD_ID[:-1] + "I",
        GOOD_ID[:-1] + "U",
        GOOD_ID[:-1] + "\uff33",
        "BMP_" + GOOD_ID[4:],
        "bmp_",
        "",
    ):
        rows.append({"k": "id", "t": text, "o": pf._ID_RE.fullmatch(text) is not None})
    rows.append({"k": "bad", "o": [c for c in range(0x110000) if pf._bad_char(chr(c))]})
    return rows


def generate() -> dict[str, bytes]:
    """``project_file.jsonl``: not gated."""
    rows = _other_rows()
    return {"project_file.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}


# ---------------------------------------------------------------------------
# Unit 2: `parse` and its reads.
#
# A detail holding a backslash is written as code points (``d_cps``):
# escaped by JSON it would read as a UNC prefix to the host-fact guard.
#
# ``project_file_parse.jsonl`` (not gated) runs `_parse_stat` over a stat
# the row describes (``kind``: ``reg``, ``dir`` or ``fifo``; ``flags`` and
# ``attrs``, the ``st_flags`` and ``st_file_attributes`` a stat carries,
# absent where the attribute is) under ``plat`` (``sys.platform``), with
# `_read_bounded` answering the row's bytes (``raw`` a text, ``hex`` bytes),
# for the file ``/p/<dir>/bettermemory.toml``. ``project_file_parse_python``
# (Python-gated) holds the rows `tomllib` refuses: its messages, and the
# ValueError of `int()`'s digit limit, are the release's. An answer is
# ``{"r": reason, "d": detail}``, ``{"id", "name", "b"}`` or ``{"x": class}``.
#
# ``project_file_parse_posix.jsonl`` (POSIX only) runs `parse` over a real
# tree in a scratch directory: sizes 65,535 to 65,537, a FIFO, a folder,
# links, a loop, a name too long, a mode-000 file (left out when run as
# root) and a path through a file, with the C library's texts, which macOS
# and glibc share for these errnos. ``n`` names the entry under the scratch directory; an answer's
# path is given relative to it.
# ---------------------------------------------------------------------------


def _answer(found: Any, root: str | None = None) -> dict[str, Any]:
    if isinstance(found, pf.ProjectFileError):
        out: dict[str, Any] = {"r": found.reason}
        if "\\" in found.detail:
            # Code points: a doubled backslash, escaped again by JSON,
            # reads as a UNC prefix to the host-fact guard.
            out["d_cps"] = [ord(c) for c in found.detail]
        else:
            out["d"] = found.detail
    else:
        out = {"id": found.id, "name": found.name, "b": found.boundary}
    if root is not None:
        out["p"] = str(found.path)[len(root) :]
    return out


class _Stat:
    def __init__(self, kind: str, flags: int | None, attrs: int | None) -> None:
        bits = {"reg": stat.S_IFREG, "dir": stat.S_IFDIR, "fifo": stat.S_IFIFO}
        self.st_mode = bits[kind] | 0o644
        if flags is not None:
            self.st_flags = flags
        if attrs is not None:
            self.st_file_attributes = attrs


def _stat_row(
    raw: str | bytes,
    *,
    kind: str = "reg",
    plat: str = "linux",
    flags: int | None = None,
    attrs: int | None = None,
    folder: str = "d",
) -> dict[str, Any]:
    row: dict[str, Any] = {"k": "stat", "plat": plat, "kind": kind, "dir": folder}
    if flags is not None:
        row["flags"] = flags
    if attrs is not None:
        row["attrs"] = attrs
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if isinstance(raw, str):
        row["raw"] = raw
    else:
        row["hex"] = raw.hex()
    path = Path("/p") / folder / "bettermemory.toml"
    try:
        with (
            mock.patch.object(sys, "platform", plat),
            mock.patch.object(pf, "_read_bounded", lambda _p: data),
        ):
            found = pf._parse_stat(path, _Stat(kind, flags, attrs))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        row["o"] = {"x": type(exc).__name__}
    else:
        row["o"] = _answer(found)
    return row


def _p(body: str) -> str:
    return "[project]\n" + body


def _stat_rows() -> list[dict[str, Any]]:
    big = 0x4000_0000
    recall = (0x0004_0000, 0x0040_0000)
    rows = [
        _stat_row(GOOD),
        _stat_row(GOOD, kind="dir"),
        _stat_row(GOOD, kind="fifo"),
        _stat_row(GOOD, kind="dir", plat="darwin", flags=big),
        _stat_row(GOOD, plat="darwin", flags=big),
        _stat_row(GOOD, plat="darwin", flags=big | 1),
        _stat_row(GOOD, plat="darwin", flags=0x2000_0000),
        _stat_row(GOOD, plat="linux", flags=big),
        _stat_row(GOOD, plat="win32", flags=big),
        _stat_row(GOOD, plat="win32", attrs=recall[0]),
        _stat_row(GOOD, plat="win32", attrs=recall[1]),
        _stat_row(GOOD, plat="win32", attrs=0x0040_0001),
        _stat_row(GOOD, plat="win32", attrs=0x20),
        _stat_row(GOOD, plat="linux", attrs=recall[0]),
        _stat_row(GOOD, plat="darwin", attrs=recall[1]),
        _stat_row(b"\xff"),
        _stat_row(b'[project]\nname = "\xc3"\n'),
        _stat_row(b"\xed\xa0\x80"),
        _stat_row(""),
        _stat_row("x = 1\n"),
        _stat_row("project = 1\n"),
        _stat_row('project = "t"\n'),
        _stat_row(f'[[project]]\nid = "{GOOD_ID}"\n'),
        _stat_row(f'project = {{ id = "{GOOD_ID}" }}\n'),
        _stat_row("[project.sub]\nx = 1\n"),
        _stat_row(_p("")),
        _stat_row(_p('name = "n"\n')),
        _stat_row(_p("id = 1\n")),
        _stat_row(_p("id = true\n")),
        _stat_row(_p("id = [1]\n")),
        _stat_row(_p("id = 1979-05-27\n")),
        _stat_row(_p(f'id = "{GOOD_ID.lower()}"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}\\n"\n')),
        _stat_row(_p(f'id = """{GOOD_ID}\n"""\n')),
        _stat_row(_p(f'id = "{GOOD_ID}0"\n')),
        _stat_row(_p(f'id = "{GOOD_ID[:-1]}"\n')),
        _stat_row(_p(f'id = "{GOOD_ID[:-1]}I"\n')),
        _stat_row(_p(f'id = " {GOOD_ID}"\n')),
        _stat_row(_p('id = ""\n')),
        _stat_row(_p('id = "' + "a" * 39 + '\\u00e9x"\n')),
        _stat_row(_p('id = "' + "a" * 40 + 'zzz"\n')),
        _stat_row(_p('id = "' + "a" * 41 + '"\n')),
        _stat_row(_p('id = "\\u001b[31m\\"q\\"\\n"\n')),
        # Bytes: the escaped backslash would be doubled again by JSON.
        _stat_row(_p('id = "\\u2028\\u0085\\\\"\n').encode("ascii")),
        _stat_row(_p('id = "\\U0001F600"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\n'), folder="caf\u00e9"),
        _stat_row(_p(f'id = "{GOOD_ID}"\n'), folder="a\x01b\x7fc\u0085d\u2028e"),
        _stat_row(_p(f'id = "{GOOD_ID}"\n'), folder="\x1b"),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = ""\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "x\\ty"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "x\\u007f"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "x\\u009f"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "x\\u00a0"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "x\\u2029"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = 1\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = 1\nboundary = 1\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nname = "\\n"\nboundary = 1\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nboundary = true\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nboundary = false\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nboundary = 1\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nboundary = "true"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\nother = 1\n[other]\nx = 2\n')),
        _stat_row(_p(f'name = 1\nid = "{GOOD_ID}"\n')),
        _stat_row(_p('boundary = 1\nname = 2\nid = "x"\n')),
        _stat_row(_p(f'id = "{GOOD_ID}"\r\nname = "n"\r\n')),
        _stat_row(f'[project]\nid = "{GOOD_ID}"\n[project.x]\ny = 1\n'),
    ]
    return rows


def _python_rows() -> list[dict[str, Any]]:
    return [
        _stat_row("[project"),
        _stat_row("[project]\nid = \n"),
        _stat_row("[project]\n[project]\n"),
        _stat_row('a = "\\q"\n'),
        _stat_row("a = 1\na = 2\n"),
        _stat_row("\x00"),
        _stat_row(b"\xef\xbb\xbf" + GOOD.encode()),
        _stat_row(f'[project]\nid = "{GOOD_ID}"\nname = "n'),
        _stat_row("a = 1979-02-30\n"),
        _stat_row("a = 1\r\n\rb = 2\n"),
        _stat_row("a = " + "1" * 4301 + "\n"),
        _stat_row("a = " + "1" * 4300 + "\n"),
    ]


def generate_parse() -> dict[str, bytes]:
    """``project_file_parse.jsonl``: not gated."""
    rows = _stat_rows()
    return {
        "project_file_parse.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }


def generate_parse_python() -> dict[str, bytes]:
    """``project_file_parse_python.jsonl``: `tomllib`'s messages, gated on
    the Python release."""
    rows = _python_rows()
    return {
        "project_file_parse_python.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }


def _sized(size: int) -> bytes:
    head = GOOD.encode("ascii") + b"#"
    return head + b"a" * (size - len(head) - 1) + b"\n"


def generate_parse_posix() -> dict[str, bytes]:
    """``project_file_parse_posix.jsonl``: POSIX only."""
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch)
        root = base / "proj"
        root.mkdir()
        (root / "good").write_bytes(GOOD.encode("ascii"))
        for size in (65535, 65536, 65537, 70000):
            (root / f"s{size}").write_bytes(_sized(size))
        (root / "latin").write_bytes(b"\xe9")
        (root / "empty").write_bytes(b"")
        getattr(os, "mkfifo")(root / "fifo")  # noqa: B009 - POSIX only
        (root / "dir").mkdir()
        (root / "link").symlink_to("good")
        (root / "dangling").symlink_to("nowhere")
        (root / "loop").symlink_to("loop")
        (root / "fifolink").symlink_to("fifo")
        (root / "locked").write_bytes(GOOD.encode("ascii"))
        (root / "locked").chmod(0)
        names = [
            "good",
            "s65535",
            "s65536",
            "s65537",
            "s70000",
            "latin",
            "empty",
            "fifo",
            "dir",
            "link",
            "dangling",
            "loop",
            "fifolink",
            "missing",
            "good/below",
            "x" * 300,
            "dir/../good",
            "dir//./../link",
        ]
        # Mode 000 refuses the read with EACCES only to a user other than
        # root: as uid 0 the read succeeds and the row would pin nothing,
        # so a root runner leaves it out.
        if getattr(os, "geteuid")() != 0:  # noqa: B009 - POSIX only
            names.insert(names.index("missing"), "locked")
        try:
            for name in names:
                found = pf.parse(str(root) + "/" + name)
                entry = name if len(name) < 100 else f"x*{len(name)}"
                rows.append({"n": entry, "o": _answer(found, str(root))})
        finally:
            (root / "locked").chmod(0o644)
    return {
        "project_file_parse_posix.jsonl": jsonl(
            [{"k": "header", "cases": len(rows)}, *rows]
        )
    }
