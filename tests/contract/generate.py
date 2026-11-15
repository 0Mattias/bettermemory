"""Generate the contract golden files (Linear ENG-313).

``python -m tests.contract.generate`` rewrites ``tests/contract/golden``;
``tests/test_contract_golden.py`` regenerates every file in memory and
compares it byte for byte. Nothing here reads the network, the clock or a
random source in a way that reaches a golden byte.
"""

from __future__ import annotations

import sqlite3
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

from . import (
    gen_json,
    gen_lexicon,
    gen_log,
    gen_store,
    gen_store_path,
    gen_time,
    gen_unicode,
)

GOLDEN = Path(__file__).parent / "golden"

GENERATORS: dict[str, Callable[[], dict[str, bytes]]] = {
    "lexicon": gen_lexicon.generate,
    "canonical_json": gen_json.generate,
    "log_chain": gen_log.generate,
    "store": gen_store.generate,
    "store_path": gen_store_path.generate,
    "datetime": gen_time.generate,
    "unicode": gen_unicode.generate,
}

#: The Unicode data version the golden files were generated under. The
#: generators gated on it read `unicodedata` or `re`'s word class for
#: their output; under another version they are not compared.
UNIDATA_VERSION = "15.1.0"
UNICODE_GATED = frozenset({"lexicon", "unicode", "store", "store_path"})

#: The SQLite the golden files were generated under: the store fixture
#: holds FTS5's shadow-table definitions and ``bm25()`` scores.
SQLITE_VERSION = "3.50.4"
SQLITE_GATED = frozenset({"store", "store_path"})


def main() -> int:
    if unicodedata.unidata_version != UNIDATA_VERSION:
        print(
            f"refusing: Unicode {unicodedata.unidata_version}, golden files are "
            f"{UNIDATA_VERSION}",
            file=sys.stderr,
        )
        return 1
    if sqlite3.sqlite_version != SQLITE_VERSION:
        print(
            f"refusing: SQLite {sqlite3.sqlite_version}, golden files are "
            f"{SQLITE_VERSION}",
            file=sys.stderr,
        )
        return 1
    GOLDEN.mkdir(exist_ok=True)
    for generate in GENERATORS.values():
        for name, data in generate().items():
            (GOLDEN / name).write_bytes(data)
            print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
