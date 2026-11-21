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

from pathlib import Path
from typing import Any

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
