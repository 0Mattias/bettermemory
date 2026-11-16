"""Generate the contract golden files (Linear ENG-313).

``python -m tests.contract.generate`` rewrites ``tests/contract/golden``;
``tests/test_contract_golden.py`` regenerates every file in memory and
compares it byte for byte.

``python -m tests.contract.generate --out DIR --as-found`` writes the whole
set as this interpreter, its SQLite, its Unicode data and this platform
make it, into DIR, with a manifest (``platform.json``) saying what made it
and how each file compares with the committed one. It refuses nothing: a
generator that raises is recorded in the manifest and the run goes on. The
native build's tests read such a directory through ``BM_GOLDEN_DIR``
(native/README.md, "Parity"). Nothing here reads the network, the clock or a
random source in a way that reaches a golden byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
import traceback
import unicodedata
from collections.abc import Callable
from pathlib import Path

from . import (
    gen_caller_admission,
    gen_capture_vocabulary,
    gen_cli,
    gen_config,
    gen_daemon,
    gen_edges,
    gen_fold,
    gen_fs_realpath,
    gen_gitconfig,
    gen_gitconfig_file,
    gen_gitconfig_tree,
    gen_gitconfig_remote_rules,
    gen_gitconfig_wildmatch,
    gen_githead,
    gen_gitfile_leaves,
    gen_json,
    gen_lexicon,
    gen_log,
    gen_log_ring,
    gen_mcp,
    gen_membership_lines,
    gen_models,
    gen_restore_lock,
    gen_state,
    gen_search_ranker,
    gen_store,
    gen_store_create,
    gen_store_files,
    gen_store_open,
    gen_store_records,
    gen_store_records_b,
    gen_store_rows_written,
    gen_store_tx,
    gen_store_write,
    gen_store_path,
    gen_time,
    gen_toml,
    gen_ulid,
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
    "settings_python": gen_config.generate_python,
    "settings_paths": gen_config.generate_paths,
    "settings_paths_python": gen_config.generate_paths_python,
    "cli_surface": gen_cli.generate,
    "state_files": gen_state.generate,
    "store_files": gen_store_files.generate,
    "models": gen_models.generate,
    "models_rows": gen_models.rows,
    "models_stores": gen_models.stores,
    "store_restore_lock": gen_restore_lock.generate,
    "toml": gen_toml.generate,
    "fold": gen_fold.generate,
    "store_create": gen_store_create.generate,
    "store_open": gen_store_open.generate,
    "store_write": gen_store_write.generate,
    "store_tx": gen_store_tx.generate,
    "store_records": gen_store_records.generate,
    "store_records_b": gen_store_records_b.generate,
    "store_rows_written": gen_store_rows_written.generate,
    "gitconfig": gen_gitconfig.generate,
    "caller_admission": gen_caller_admission.generate,
    "gitconfig_wildmatch": gen_gitconfig_wildmatch.generate,
    "gitconfig_remote_rules": gen_gitconfig_remote_rules.generate,
    "ulid": gen_ulid.generate,
    "githead": gen_githead.generate,
    "githead_posix": gen_githead.generate_posix,
    "capture_vocabulary": gen_capture_vocabulary.generate,
    "capture_vocabulary_python": gen_capture_vocabulary.generate_python,
    "gitconfig_file": gen_gitconfig_file.generate,
    "gitconfig_tree": gen_gitconfig_tree.generate,
    "gitconfig_tree_rules": gen_gitconfig_tree.generate_rules,
    "membership_lines": gen_membership_lines.generate,
    "membership_lines_unicode": gen_membership_lines.generate_unicode,
    "gitfile_leaves": gen_gitfile_leaves.generate,
    "gitfile_leaves_posix": gen_gitfile_leaves.generate_posix,
    "fs_realpath": gen_fs_realpath.generate,
    "fs_realpath_python": gen_fs_realpath.generate_python,
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
        "store_files",
        "search_ranker",
        "mcp",
        "hooks",
        "fold",
        "store_create",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "membership_lines_unicode",
        "store_rows_written",
    }
)

#: The SQLite the golden files were generated under: the store fixture
#: holds FTS5's shadow-table definitions and ``bm25()`` scores. The tool
#: responses rank hits through FTS5 and tokenize through `re`'s word class,
#: so ``mcp`` is gated on both; so is ``hooks``, whose hooks rank the
#: prompt recall's pointers and the Stop audit's hits the same way, and
#: ``store_files``, whose FTS case records ``bm25()`` score bits.
SQLITE_VERSION = "3.50.4"
SQLITE_GATED = frozenset(
    {"store", "store_path", "store_files", "mcp", "hooks", "models_stores"}
)

#: The CPython the golden files were generated under, to the patch
#: release. ``python_edges`` records what changes between versions
#: (`datetime.fromisoformat`'s C parser, where `list.sort` leaves a NaN
#: key, the recursion budget JSON stops at); under another version it is
#: not compared. The patch release is part of it: CI installs the newest
#: 3.13 (3.13.18 on 2026-11-15) and its `datetime_edges.jsonl` differs
#: from 3.13.13's, so a minor-version gate compared files it should have
#: skipped (CI run 43752288601).
PYTHON_VERSION = "3.13.13"
#: ``models_rows`` and ``models_stores`` read row datetimes through
#: `datetime.fromisoformat`, whose grammar is the release's.
#: ``toml`` and ``settings_python`` / ``settings_paths_python`` hold the
#: interpreter's wording (`tomllib`'s messages, codec, `int()` and `repr`
#: texts, pathlib's parsing); 3.11 and 3.12 ship another `tomllib`.
PYTHON_GATED = frozenset(
    {
        "python_edges",
        "models_rows",
        "models_stores",
        "toml",
        "settings_python",
        "settings_paths_python",
        "capture_vocabulary_python",
        "fs_realpath_python",
    }
)


#: The pydantic-core the record fixtures were generated under (uv.lock pins
#: pydantic 2.14.1 on it): the models' coercions and errors are its.
PYDANTIC_CORE_VERSION = "2.50.1"
PYDANTIC_GATED = frozenset(
    {
        "models",
        "models_rows",
        "models_stores",
        "fold",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_rows_written",
    }
)


def pydantic_core_version() -> str:
    import pydantic_core

    return str(pydantic_core.__version__)


#: The platform the files that hold floating-point score bits were
#: generated on. A ranking's scores go through the platform's `log`,
#: `exp` and `log1p` and its compiler's double arithmetic, in Python and
#: in SQLite's ``bm25()``, and CI run 43759029312 showed 10 of
#: search_ranker.jsonl's 208 queries scoring other bits on Linux x86-64
#: under the same Python minor and Unicode. So bit-equality of scores is a
#: property of one platform: a build is held to the reference on the
#: platform it runs on, and elsewhere a difference is skipped and named.
PLATFORM = "darwin-arm64"
PLATFORM_GATED = frozenset(
    {
        "search_ranker",
        "store",
        "store_path",
        "store_files",
        "store_restore_lock",
        "mcp",
        "hooks",
    }
)


#: Generators with no case on Windows: ``store_restore_lock`` drives a
#: POSIX directory ``flock`` (the lock there is a record file and a handle
#: on the store, ENG-321). Under ``--as-found`` on Windows they are named in
#: the manifest (``not_here``) and write no file; that is no raise, and the
#: golden test skips them there.
#: ``settings_paths`` and ``settings_paths_python`` expand, resolve and
#: print paths by POSIX rules (`~name`, the root, `//`, a NUL), which the
#: native tests' world reproduces; on Windows the reference's paths differ.
#: ``store_open`` reads modes and writes the key ring's binding as a
#: resolved POSIX path under a POSIX ``flock``; ``store_write`` records the
#: same state after each write, ``store_tx`` after each transaction,
#: ``store_records`` after each writer call.
#: ``githead_posix`` reads links, FIFOs and ENOTDIR, which Windows answers
#: otherwise (`gen_githead`); ``gitconfig_file`` reads links, FIFOs and
#: mode-000 files and opens directories (`gen_gitconfig_file`);
#: ``fs_realpath`` and ``fs_realpath_python`` resolve links and FIFOs by
#: ``posixpath`` (`gen_fs_realpath`);
#: ``gitconfig_tree`` reads real trees with the host's ``os.path`` and ``pwd``;
#: ``gitfile_leaves_posix`` reads links and mode-000 files
#: (`gen_gitfile_leaves`).
POSIX_ONLY = frozenset(
    {
        "store_restore_lock",
        "settings_paths",
        "settings_paths_python",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_rows_written",
        "githead_posix",
        "gitconfig_file",
        "gitconfig_tree",
        "gitfile_leaves_posix",
        "fs_realpath",
        "fs_realpath_python",
    }
)


def platform_key() -> str:
    """``sys.platform``-``machine``; ``BM_PLATFORM_KEY`` overrides it, to
    simulate another platform (the native tests read it too)."""
    override = os.environ.get("BM_PLATFORM_KEY")
    if override:
        return override
    return f"{sys.platform}-{platform.machine().lower()}"


def _libc() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    name, version = platform.libc_ver()
    return f"{name} {version}".strip() or None


def drift(name: str) -> list[str]:
    """The pins generator ``name``'s committed files were made under that
    this interpreter and platform do not share."""
    out = []
    if name in UNICODE_GATED and unicodedata.unidata_version != UNIDATA_VERSION:
        out.append(f"Unicode {unicodedata.unidata_version} (files: {UNIDATA_VERSION})")
    if name in SQLITE_GATED and sqlite3.sqlite_version != SQLITE_VERSION:
        out.append(f"SQLite {sqlite3.sqlite_version} (files: {SQLITE_VERSION})")
    if name in PYTHON_GATED and python_version() != PYTHON_VERSION:
        out.append(f"Python {python_version()} (files: {PYTHON_VERSION})")
    if name in PLATFORM_GATED and platform_key() != PLATFORM:
        out.append(f"platform {platform_key()} (files: {PLATFORM})")
    if name in PYDANTIC_GATED and pydantic_core_version() != PYDANTIC_CORE_VERSION:
        out.append(
            f"pydantic-core {pydantic_core_version()} (files: {PYDANTIC_CORE_VERSION})"
        )
    return out


def as_found(out: Path) -> int:
    """Every generator under whatever is present, into ``out``, with
    ``platform.json``. Exit 0 when every generator ran, 1 when one raised
    (the files of the others are written all the same)."""
    from ._diff import _differing

    out.mkdir(parents=True, exist_ok=True)
    generators: dict[str, dict[str, object]] = {}
    files: dict[str, object] = {}
    failed = 0
    for name, generate in GENERATORS.items():
        entry: dict[str, object] = {"gated_by": drift(name)}
        if name in POSIX_ONLY and sys.platform == "win32":
            entry["not_here"] = "POSIX only: no case on Windows"
            generators[name] = entry
            print(f"{name}: not generated here (POSIX only)")
            continue
        started = time.monotonic()
        try:
            produced = generate()
        except Exception as exc:  # recorded, and the run goes on
            failed += 1
            entry["raised"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc().splitlines()[-12:]
            generators[name] = entry
            print(f"{name}: raised {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        entry["files"] = sorted(produced)
        entry["seconds"] = round(time.monotonic() - started, 1)
        generators[name] = entry
        # Flushed: a runner's log then shows where the minutes go.
        print(f"{name}: {entry['seconds']} s", flush=True)
        for filename, data in sorted(produced.items()):
            (out / filename).write_bytes(data)
            committed_path = GOLDEN / filename
            committed = committed_path.read_bytes() if committed_path.exists() else None
            info: dict[str, object] = {
                "generator": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "identical": data == committed,
            }
            if committed is None:
                info["committed"] = None
            elif data != committed:
                if filename.endswith(".sqlite"):
                    # A store file as this SQLite wrote it: its bytes are
                    # not lines; store_real.json records its answers.
                    info["differs"] = (
                        f"binary, {len(data)} bytes here, {len(committed)} committed"
                    )
                else:
                    ours, theirs = data.splitlines(), committed.splitlines()
                    info["lines_differing"] = sum(
                        1
                        for i in range(max(len(ours), len(theirs)))
                        if (ours[i] if i < len(ours) else None)
                        != (theirs[i] if i < len(theirs) else None)
                    )
                    info["differs"] = _differing(data, committed)
            files[filename] = info
            print(
                f"{filename}: {len(data)} bytes, "
                f"{'identical' if info['identical'] else 'differs'}"
            )
    manifest = {
        "platform": platform_key(),
        "machine": platform.machine(),
        "system": platform.platform(),
        "libc": _libc(),
        "python": python_version(),
        "python_build": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "unicode": unicodedata.unidata_version,
        "pydantic_core": pydantic_core_version(),
        "committed": {
            "platform": PLATFORM,
            "python": PYTHON_VERSION,
            "sqlite": SQLITE_VERSION,
            "unicode": UNIDATA_VERSION,
            "pydantic_core": PYDANTIC_CORE_VERSION,
        },
        "generators": generators,
        "raised": sorted(n for n, e in generators.items() if "raised" in e),
        "not_here": sorted(n for n, e in generators.items() if "not_here" in e),
        "files": files,
    }
    (out / "platform.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"platform.json: {platform_key()}, Python {python_version()}, "
        f"SQLite {sqlite3.sqlite_version}, Unicode {unicodedata.unidata_version}; "
        f"{failed} generator(s) raised"
    )
    return 1 if failed else 0


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.contract.generate")
    parser.add_argument("--out", type=Path, help="write into this directory")
    parser.add_argument(
        "--as-found",
        action="store_true",
        help="run under whatever is present, write platform.json (needs --out)",
    )
    args = parser.parse_args(argv)
    if args.as_found:
        if args.out is None:
            parser.error("--as-found needs --out DIR")
        if args.out.resolve() == GOLDEN.resolve():
            parser.error("--as-found never writes the committed directory")
        return as_found(args.out)
    if args.out is not None:
        parser.error(
            "--out without --as-found: the pinned run writes the committed set"
        )
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
    if pydantic_core_version() != PYDANTIC_CORE_VERSION:
        print(
            f"refusing: pydantic-core {pydantic_core_version()}, golden files are "
            f"{PYDANTIC_CORE_VERSION}",
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
