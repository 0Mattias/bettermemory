"""Derive the committed reduced register for W1's CI determinism check.

G3 of `bench/w/W1_DECLARATION.md` §7 needs a corpus CI can retrain
twice without the full register on disk. This script derives it from
the pinned Gutenberg bytes — public-domain text, so the derived slice
is committable — by a rule a reader can replay: PG-stripped prose of
the admitted books in ascending id order, concatenated with a blank
line between books, truncated exactly at the byte budget (safely
under the declaration's 2 MB cap). The output lands at
``bench/w/ci_register/reduced.txt`` and is pinned in
``bench/w/corpora.json`` as a derived entry with its own sha256.

Boilerplate is stripped before the slice is taken, so the committed
file carries no Project Gutenberg trademark, per the register's
license note.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from w1_corpus import iter_gutenberg_books, register_paths

_BYTE_BUDGET = 1_500_000


def main() -> int:
    _, gutenberg_dir = register_paths()
    parts: list[str] = []
    size = 0
    for prose in iter_gutenberg_books(gutenberg_dir):
        chunk = prose.strip() + "\n\n"
        data = chunk.encode("utf-8")
        if size + len(data) >= _BYTE_BUDGET:
            keep = data[: _BYTE_BUDGET - size]
            parts.append(keep.decode("utf-8", errors="ignore"))
            break
        parts.append(chunk)
        size += len(data)
    out = Path(__file__).parent / "ci_register" / "reduced.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out} {out.stat().st_size} bytes sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
