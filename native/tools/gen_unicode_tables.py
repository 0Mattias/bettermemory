"""Generate the native build's Unicode tables from the reference Python.

``python native/tools/gen_unicode_tables.py`` writes
``native/crates/bettermemory-core/src/unicode/tables.rs``; with ``--check``
it writes nothing and exits 1 when the committed file differs from what it
would write. Run it with the Python the golden files were generated under
(3.13, Unicode 15.1.0): every table is read from that interpreter's
``unicodedata``, ``str`` methods and ``re``, the data the reference build
behaves by, and the output is a pure function of that data (no clock, no
environment), so the same interpreter writes the same bytes.

The tables:

- general category and canonical combining class, as runs;
- full canonical (NFD) and compatibility (NFKD) decompositions of each code
  point, already expanded and reordered, without the Hangul syllables
  (decomposed algorithmically); the canonical composition pairs, read as
  "NFC of the pair gives the code point back";
- full lower, upper and case-fold mappings, as ``str.lower``/``upper``/
  ``casefold`` give them for a code point alone;
- the predicates ``str.isalpha``, ``isalnum``, ``isdigit``, ``isdecimal``,
  ``isnumeric``, ``isspace``, ``re``'s ``\\w`` and ``\\d``, and the two
  properties ``str.lower`` reads for the final sigma (case-ignorable and
  cased), probed through ``str.lower`` itself, as ranges;
- ``re._casefix._EXTRA_CASES``, the code points ``re`` adds to a lowered
  character under IGNORECASE, by ascending key, each key's partners in the
  interpreter's order.

It also writes ``xid_tables.rs`` beside it: XID_Start and XID_Continue as
``str.isidentifier`` answers them (``generate_xid``).
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path

EXPECTED_UNIDATA = "15.1.0"
OUT = (
    Path(__file__).resolve().parents[1]
    / "crates"
    / "bettermemory-core"
    / "src"
    / "unicode"
    / "tables.rs"
)
OUT_XID = OUT.with_name("xid_tables.rs")

HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3
_WORD = re.compile(r"\w")
_DIGIT = re.compile(r"\d")


def scalars() -> Iterable[int]:
    for cp in range(0x110000):
        if not 0xD800 <= cp <= 0xDFFF:
            yield cp


def runs(fn: Callable[[str], object]) -> list[tuple[int, object]]:
    out: list[tuple[int, object]] = []
    for cp in range(0x110000):
        value = fn(chr(cp))
        if not out or out[-1][1] != value:
            out.append((cp, value))
    return out


def ranges(fn: Callable[[str], bool]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for cp in range(0x110000):
        if not fn(chr(cp)):
            continue
        if out and out[-1][1] == cp - 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return [(a, b) for a, b in out]


def final_sigma_props() -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Case-ignorable and cased, as `str.lower`'s final-sigma rule reads
    them. ``'A' + X + 'Σ'`` ends in a final sigma when X is case-ignorable
    or cased; ``'1' + X + 'Σ'`` when X is cased and not case-ignorable.
    So case-ignorable is the first without the second, and the second is
    "cased" for every code point the rule ever asks it of (the rule skips
    case-ignorable ones before it asks)."""

    def final(prefix: str, ch: str) -> bool:
        return (prefix + ch + "Σ").lower()[-1] == "ς"

    ignorable = ranges(lambda c: final("A", c) and not final("1", c))
    cased = ranges(lambda c: final("1", c))
    return ignorable, cased


def mapping_table(fn: Callable[[str], str], skip_hangul: bool) -> list[tuple[int, str]]:
    out = []
    for cp in scalars():
        if skip_hangul and HANGUL_FIRST <= cp <= HANGUL_LAST:
            continue
        ch = chr(cp)
        result = fn(ch)
        if result != ch:
            out.append((cp, result))
    return out


def composition_pairs() -> list[tuple[int, int, int]]:
    pairs = []
    for cp in scalars():
        if HANGUL_FIRST <= cp <= HANGUL_LAST:
            continue
        raw = unicodedata.decomposition(chr(cp))
        if not raw or raw.startswith("<"):
            continue
        parts = [int(p, 16) for p in raw.split()]
        if len(parts) != 2:
            continue
        if unicodedata.normalize("NFC", chr(parts[0]) + chr(parts[1])) == chr(cp):
            pairs.append((parts[0], parts[1], cp))
    pairs.sort()
    return pairs


def extra_cases() -> list[tuple[int, tuple[int, ...]]]:
    """``re._casefix._EXTRA_CASES``: for a lowered code point, the other
    lowered code points ``re`` takes for the same letter under IGNORECASE."""
    from re import _casefix

    return sorted((key, tuple(others)) for key, others in _casefix._EXTRA_CASES.items())


def emit_list(name: str, ty: str, items: list[str], per_line: int) -> list[str]:
    """``items`` packed into lines of at most 99 characters (``per_line``
    caps the count per line)."""
    lines = [f"pub static {name}: &[{ty}] = &["]
    line: list[str] = []
    for item in items:
        if line and (
            len(line) == per_line or len("    " + ", ".join([*line, item]) + ",") > 99
        ):
            lines.append("    " + ", ".join(line) + ",")
            line = []
        line.append(item)
    if line:
        lines.append("    " + ", ".join(line) + ",")
    lines.append("];")
    return lines


def emit_mapping(name: str, table: list[tuple[int, str]]) -> list[str]:
    index: list[str] = []
    data: list[str] = []
    offset = 0
    for cp, text in table:
        cps = [ord(c) for c in text]
        assert len(cps) < 256 and offset < (1 << 24)
        index.append(f"(0x{cp:X}, {offset << 8 | len(cps)})")
        data.extend(f"0x{c:X}" for c in cps)
        offset += len(cps)
    return emit_list(f"{name}_INDEX", "(u32, u32)", index, 16) + emit_list(
        f"{name}_DATA", "u32", data, 16
    )


def emit_ranges(name: str, table: list[tuple[int, int]]) -> list[str]:
    return emit_list(name, "(u32, u32)", [f"(0x{a:X}, 0x{b:X})" for a, b in table], 16)


def generate() -> str:
    categories = runs(unicodedata.category)
    names = sorted({str(v) for _, v in categories})
    combining = runs(unicodedata.combining)
    ignorable, cased = final_sigma_props()
    lines = [
        "// @generated by native/tools/gen_unicode_tables.py from Python "
        f"{sys.version_info.major}.{sys.version_info.minor}'s",
        f"// unicodedata {unicodedata.unidata_version}. Do not edit; regenerate.",
        "",
        f'pub const UNIDATA_VERSION: &str = "{unicodedata.unidata_version}";',
        "",
        f"pub static CATEGORY_NAMES: [&str; {len(names)}] = [",
    ]
    lines += [
        "    " + ", ".join(f'"{n}"' for n in names[i : i + 10]) + ","
        for i in range(0, len(names), 10)
    ]
    lines.append("];")
    lines += emit_list(
        "CATEGORY_RUNS",
        "(u32, u8)",
        [f"(0x{cp:X}, {names.index(str(v))})" for cp, v in categories],
        16,
    )
    lines += emit_list(
        "COMBINING_RUNS", "(u32, u8)", [f"(0x{cp:X}, {v})" for cp, v in combining], 16
    )
    lines += emit_mapping(
        "NFD", mapping_table(lambda c: unicodedata.normalize("NFD", c), True)
    )
    lines += emit_mapping(
        "NFKD", mapping_table(lambda c: unicodedata.normalize("NFKD", c), True)
    )
    lines += emit_list(
        "COMPOSE",
        "(u32, u32, u32)",
        [f"(0x{a:X}, 0x{b:X}, 0x{c:X})" for a, b, c in composition_pairs()],
        16,
    )
    lines += emit_mapping("LOWER", mapping_table(str.lower, False))
    lines += emit_mapping("UPPER", mapping_table(str.upper, False))
    lines += emit_mapping("CASEFOLD", mapping_table(str.casefold, False))
    lines += emit_ranges("ALPHA", ranges(str.isalpha))
    lines += emit_ranges("ALNUM", ranges(str.isalnum))
    lines += emit_ranges("DIGIT", ranges(str.isdigit))
    lines += emit_ranges("DECIMAL", ranges(str.isdecimal))
    lines += emit_ranges("NUMERIC", ranges(str.isnumeric))
    lines += emit_ranges("SPACE", ranges(str.isspace))
    lines += emit_ranges("RE_WORD", ranges(lambda c: _WORD.match(c) is not None))
    lines += emit_ranges("RE_DIGIT", ranges(lambda c: _DIGIT.match(c) is not None))
    lines += emit_ranges("CASE_IGNORABLE", ignorable)
    lines += emit_ranges("CASED", cased)
    lines += emit_list(
        "SRE_EXTRA_CASES",
        "(u32, &[u32])",
        [
            f"(0x{key:X}, &[{', '.join(f'0x{c:X}' for c in others)}])"
            for key, others in extra_cases()
        ],
        16,
    )
    return "\n".join(lines) + "\n"


def generate_xid() -> str:
    """XID_Start and XID_Continue as ``str.isidentifier`` answers them:
    start is a code point that is an identifier alone, without ``_``
    (which CPython accepts first by name, not as XID_Start); continue is
    one that ``'a'`` followed by it makes an identifier."""
    lines = [
        "// @generated by native/tools/gen_unicode_tables.py from Python "
        f"{sys.version_info.major}.{sys.version_info.minor}'s",
        f"// str.isidentifier, Unicode {unicodedata.unidata_version}. Do not edit; regenerate.",
    ]
    lines += emit_ranges("XID_START", ranges(lambda c: c != "_" and c.isidentifier()))
    lines += emit_ranges("XID_CONTINUE", ranges(lambda c: ("a" + c).isidentifier()))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if unicodedata.unidata_version != EXPECTED_UNIDATA:
        print(
            f"refusing: Unicode {unicodedata.unidata_version}, the native tables "
            f"are {EXPECTED_UNIDATA}",
            file=sys.stderr,
        )
        return 2
    status = 0
    for out, text in ((OUT, generate()), (OUT_XID, generate_xid())):
        if "--check" in argv:
            current = out.read_text(encoding="utf-8") if out.exists() else ""
            if current != text:
                print(f"{out} differs from what the generator writes", file=sys.stderr)
                status = 1
            else:
                print(f"{out}: up to date")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"{out}: {len(text)} bytes")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
