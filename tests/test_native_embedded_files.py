"""Every file the native build compiles in is checked out byte for byte.

`include_str!` and `include_bytes!` embed a file's bytes as they are on
disk; neither normalizes line endings. A file git treats as text is
rewritten with CRLF on a Windows checkout (the runners set core.autocrlf),
so the binary built there would carry bytes the reference never had: the
default configuration text did exactly that, and the Windows build wrote a
config file one carriage return per line longer than the reference's.
Pinned here: each file named by a literal `include_str!` or
`include_bytes!` path under native/crates exists, and git reports its
`text` attribute as unset (`-text` in .gitattributes).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
CRATES = _ROOT / "native" / "crates"
#: A literal path argument of either macro.
INCLUDE = re.compile(r'\binclude_(?:str|bytes)!\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
#: Files the scan must find, so a broken scan cannot pass by finding none.
KNOWN = (
    "native/crates/bettermemory-core/src/config/default_config.toml",
    "native/crates/bettermemory-core/src/lexicon/reference_spelling_tables.json",
    "native/crates/bettermemory-core/src/store/reference_schema.json",
)


def _sources() -> list[Path]:
    return sorted(
        path
        for path in CRATES.rglob("*.rs")
        if "target" not in path.relative_to(CRATES).parts
    )


def _embedded() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for source in _sources():
        for match in INCLUDE.finditer(source.read_text(encoding="utf-8")):
            target = (source.parent / match.group(1)).resolve()
            found[target.relative_to(_ROOT).as_posix()] = source
    return found


def _text_attribute(paths: list[str]) -> dict[str, str]:
    out = subprocess.run(
        ["git", "check-attr", "-z", "text", "--", *paths],
        cwd=_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    fields = out.split("\0")
    return {fields[i]: fields[i + 2] for i in range(0, len(fields) - 2, 3)}


def test_the_scan_finds_the_known_embedded_files() -> None:
    embedded = _embedded()
    assert set(KNOWN) <= set(embedded), sorted(embedded)


def test_every_embedded_file_exists() -> None:
    missing = {
        path: str(src)
        for path, src in _embedded().items()
        if not (_ROOT / path).is_file()
    }
    assert not missing, missing


def test_every_embedded_file_has_the_text_attribute_unset() -> None:
    embedded = sorted(_embedded())
    attributes = _text_attribute(embedded)
    wrong = {
        path: attributes.get(path)
        for path in embedded
        if attributes.get(path) != "unset"
    }
    assert not wrong, wrong
