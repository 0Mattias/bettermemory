"""Generate the contract golden files (Linear ENG-313).

``python -m tests.contract.generate`` rewrites ``tests/contract/golden``;
``tests/test_contract_golden.py`` regenerates every file in memory and
compares it byte for byte. Nothing here reads the network, the clock or a
random source in a way that reaches a golden byte.
"""

from __future__ import annotations

import platform
import sqlite3
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

from . import (
    gen_cli,
    gen_config,
    gen_daemon,
    gen_edges,
    gen_json,
    gen_lexicon,
    gen_log,
    gen_log_ring,
    gen_mcp,
    gen_state,
    gen_search_ranker,
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
    "log_keyring": gen_log_ring.generate,
    "store": gen_store.generate,
    "search_ranker": gen_search_ranker.generate,
    "store_path": gen_store_path.generate,
    "datetime": gen_time.generate,
    "unicode": gen_unicode.generate,
    "unicode_sequences": gen_unicode.sequences,
    "python_edges": gen_edges.generate,
    "mcp_surface": gen_mcp.surface,
    "mcp_plain": gen_mcp.plain,
    "mcp": gen_mcp.responses,
    "daemon_http": gen_daemon.http_api,
    "hooks": gen_daemon.hooks,
    "settings": gen_config.generate,
    "cli_surface": gen_cli.generate,
    "state_files": gen_state.generate,
}

#: The Unicode data version the golden files were generated under. The
#: generators gated on it read `unicodedata` or `re`'s word class for
#: their output; under another version they are not compared.
UNIDATA_VERSION = "15.1.0"
UNICODE_GATED = frozenset(
    {
        "lexicon",
        "unicode",
        "unicode_sequences",
        "store",
        "store_path",
        "search_ranker",
        "mcp",
        "hooks",
    }
)

#: The SQLite the golden files were generated under: the store fixture
#: holds FTS5's shadow-table definitions and ``bm25()`` scores. The tool
#: responses rank hits through FTS5 and tokenize through `re`'s word class,
#: so ``mcp`` is gated on both; so is ``hooks``, whose hooks rank the
#: prompt recall's pointers and the Stop audit's hits the same way.
SQLITE_VERSION = "3.50.4"
SQLITE_GATED = frozenset({"store", "store_path", "mcp", "hooks"})

#: The CPython the golden files were generated under, to the patch
#: release. ``python_edges`` records what changes between versions
#: (`datetime.fromisoformat`'s C parser, where `list.sort` leaves a NaN
#: key, the recursion budget JSON stops at); under another version it is
#: not compared. The patch release is part of it: CI installs the newest
#: 3.13 (3.13.18 on 2026-11-15) and its `datetime_edges.jsonl` differs
#: from 3.13.13's, so a minor-version gate compared files it should have
#: skipped (CI run 43752288601).
PYTHON_VERSION = "3.13.13"
PYTHON_GATED = frozenset({"python_edges"})


#: The platform the files that hold floating-point score bits were
#: generated on. A ranking's scores go through the platform's `log`,
#: `exp` and `log1p` and its compiler's double arithmetic, in Python and
#: in SQLite's ``bm25()``, and CI run 43759029312 showed 10 of
#: search_ranker.jsonl's 208 queries scoring other bits on Linux x86-64
#: under the same Python minor and Unicode. So bit-equality of scores is a
#: property of one platform: a build is held to the reference on the
#: platform it runs on, and elsewhere a difference is skipped and named.
PLATFORM = "darwin-arm64"
PLATFORM_GATED = frozenset({"search_ranker", "store", "store_path", "mcp", "hooks"})


def platform_key() -> str:
    return f"{sys.platform}-{platform.machine().lower()}"


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


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
    if python_version() != PYTHON_VERSION:
        print(
            f"refusing: Python {python_version()}, golden files are {PYTHON_VERSION}",
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
