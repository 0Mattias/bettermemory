"""Write what the running interpreter's ``re`` answers, value by value, for
the native regular expression engine (``pyre``) to be compared with.

``python native/tools/pyre_differential.py fold FILE`` writes, for unit
pyre-1 (the case folding), every value the golden files pin by digest, so a
difference names the first code point and not a block:

- one line of text: ``pyre fold 1``, the Python version, the Unicode
  version;
- for every code point from U+0000 to U+10FFFF, four bytes little-endian of
  ``_sre.unicode_tolower``, then the same of the first code point of
  ``str.upper``;
- for every code point, one byte (0 or 1) of ``_sre.unicode_iscased``, then
  of ``\\w``, ``\\d`` and ``\\s`` as ``re`` matches them on a ``str``
  pattern;
- the number of cased code points, and for each as a pattern under
  IGNORECASE: the code point, the number of code points ``findall`` returns
  over the string of every code point, and those code points (each four
  bytes little-endian).

The file is about 14 MB and is not committed. The native side reads it with

    BM_PYRE_FOLD_DIFF=FILE cargo test --offline --release \\
        --test golden_pyre_fold -- --ignored pyre_fold_differential_file

Run it under the reference Python (3.13.13, Unicode 15.1.0): the first line
says what made the file.
"""

from __future__ import annotations

import _sre
import re
import struct
import sys
import unicodedata
from pathlib import Path

N = 0x110000


def fold(out: Path) -> None:
    every = "".join(map(chr, range(N)))
    version = ".".join(str(part) for part in sys.version_info[:3])
    parts = [f"pyre fold 1 {version} {unicodedata.unidata_version}\n".encode("ascii")]
    lower = [_sre.unicode_tolower(c) for c in range(N)]
    parts.append(struct.pack(f"<{N}I", *lower))
    parts.append(struct.pack(f"<{N}I", *(ord(chr(c).upper()[0]) for c in range(N))))
    cased = bytes(_sre.unicode_iscased(c) for c in range(N))
    parts.append(cased)
    for pattern in (r"\w", r"\d", r"\s"):
        flags = bytearray(N)
        for ch in re.compile(pattern).findall(every):
            flags[ord(ch)] = 1
        parts.append(bytes(flags))
    patterns = [p for p in range(N) if cased[p]]
    parts.append(struct.pack("<I", len(patterns)))
    pairs = 0
    for p in patterns:
        found = re.compile(re.escape(chr(p)), re.IGNORECASE).findall(every)
        parts.append(
            struct.pack(f"<{2 + len(found)}I", p, len(found), *map(ord, found))
        )
        pairs += N
    data = b"".join(parts)
    out.write_bytes(data)
    print(
        f"{out}: {len(data)} bytes; Python {version}, Unicode "
        f"{unicodedata.unidata_version}; {len(patterns)} cased literals, "
        f"{pairs} pairs asked of re"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "fold":
        print(__doc__, file=sys.stderr)
        return 2
    fold(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
