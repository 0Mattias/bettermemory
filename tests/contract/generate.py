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
import zlib
from collections.abc import Callable
from pathlib import Path

from . import (
    gen_attribution,
    gen_caller_admission,
    gen_capture_vocabulary,
    gen_checkout_token,
    gen_checkout_token_read,
    gen_cli,
    gen_config,
    gen_credentials_prefixed,
    gen_credentials_gate,
    gen_identity_caller,
    gen_session,
    gen_daemon,
    gen_edges,
    gen_fold,
    gen_follow,
    gen_discovery,
    gen_fs_realpath,
    gen_fs_access,
    gen_fs_samestat,
    gen_gitdir,
    gen_identity,
    gen_trust,
    gen_fs_stat,
    gen_fsutil,
    gen_gitconfig,
    gen_location_stamps,
    gen_location_reads,
    gen_gitconfig_file,
    gen_gitconfig_legacy,
    gen_gitconfig_tree,
    gen_gitconfig_remote_rules,
    gen_origin_gitfiles,
    gen_origin_gitfiles_facts,
    gen_origin_gitfiles_machine,
    gen_gitconfig_wildmatch,
    gen_origin_remote,
    gen_location_remotes,
    gen_location_spellings,
    gen_location_stamped,
    gen_location_git_readers,
    gen_gitsystem_answers,
    gen_gitsystem_ask,
    gen_gitsystem_sources,
    gen_git_read_budget,
    gen_gitsystem_probe,
    gen_gitsystem_record,
    gen_githead,
    gen_githead_discovery,
    gen_location_probe,
    gen_gitobjects,
    gen_gitobjects_files,
    gen_gitobjects_streams,
    gen_gitfile_leaves,
    gen_json,
    gen_in_force,
    gen_lexicon,
    gen_verify_candidates,
    gen_log,
    gen_machine,
    gen_log_ring,
    gen_mcp,
    gen_membership_lines,
    gen_claims,
    gen_models,
    gen_origin_history,
    gen_restore_lock,
    gen_state,
    gen_search_ranker,
    gen_search_dedup,
    gen_store,
    gen_store_schema,
    gen_membership_records,
    gen_membership_rows,
    gen_store_create,
    gen_store_files,
    gen_store_open,
    gen_store_records,
    gen_store_records_b,
    gen_store_records_c,
    gen_store_records_d,
    gen_store_records_e,
    gen_history_rows,
    gen_store_rows_written,
    gen_store_tx,
    gen_store_write,
    gen_store_path,
    gen_time,
    gen_toml,
    gen_events_kinds,
    gen_control_tokens,
    gen_ulid,
    gen_durability,
    gen_unicode,
    gen_pyre,
    gen_models_text,
    gen_verify_status,
    gen_store_provenance,
    gen_store_bulk,
)

GOLDEN = Path(__file__).parent / "golden"

GENERATORS: dict[str, Callable[[], dict[str, bytes]]] = {
    "lexicon": gen_lexicon.generate,
    "verify_candidates": gen_verify_candidates.generate,
    "verify_candidates_unicode": gen_verify_candidates.generate_unicode,
    "canonical_json": gen_json.generate,
    "in_force": gen_in_force.generate,
    "log_chain": gen_log.generate,
    "machine_identity": gen_machine.generate,
    "machine_identity_python": gen_machine.generate_python,
    "log_keyring": gen_log_ring.generate,
    "store": gen_store.generate,
    "store_schema_steps": gen_store_schema.generate,
    "store_schema_posix": gen_store_schema.generate_posix,
    "membership_records_paths": gen_membership_records.generate,
    "membership_records_paths_python": gen_membership_records.generate_python,
    "membership_records_paths_platform": gen_membership_records.generate_platform,
    "membership_records_records": gen_membership_records.generate_records,
    "membership_rows": gen_membership_rows.generate,
    "membership_rows_python": gen_membership_rows.generate_python,
    "search_ranker": gen_search_ranker.generate,
    "search_dedup": gen_search_dedup.generate,
    "search_dedup_unicode": gen_search_dedup.generate_unicode,
    "store_path": gen_store_path.generate,
    "datetime": gen_time.generate,
    "origin_history": gen_origin_history.generate,
    "unicode": gen_unicode.generate,
    "models_text": gen_models_text.generate,
    "models_text_unicode": gen_models_text.generate_unicode,
    "models_text_platform": gen_models_text.generate_platform,
    "unicode_sequences": gen_unicode.sequences,
    "pyre": gen_pyre.generate,
    "pyre_unicode": gen_pyre.generate_unicode,
    "pyre_python": gen_pyre.generate_python,
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
    "identity_caller": gen_identity_caller.generate,
    "identity_caller_unicode": gen_identity_caller.generate_unicode,
    "session": gen_session.generate,
    "cli_surface": gen_cli.generate,
    "credentials_prefixed": gen_credentials_prefixed.generate,
    "credentials_prefixed_unicode": gen_credentials_prefixed.generate_unicode,
    "credentials_gate": gen_credentials_gate.generate,
    "credentials_gate_unicode": gen_credentials_gate.generate_unicode,
    "state_files": gen_state.generate,
    "store_files": gen_store_files.generate,
    "models": gen_models.generate,
    "models_rows": gen_models.rows,
    "models_stores": gen_models.stores,
    "store_restore_lock": gen_restore_lock.generate,
    "toml": gen_toml.generate,
    "events_kinds": gen_events_kinds.generate,
    "control_tokens": gen_control_tokens.generate,
    "control_tokens_unicode": gen_control_tokens.generate_unicode,
    "fold": gen_fold.generate,
    "store_create": gen_store_create.generate,
    "store_open": gen_store_open.generate,
    "store_write": gen_store_write.generate,
    "store_tx": gen_store_tx.generate,
    "store_records": gen_store_records.generate,
    "store_records_b": gen_store_records_b.generate,
    "store_records_c": gen_store_records_c.generate,
    "store_records_d": gen_store_records_d.generate,
    "store_records_e": gen_store_records_e.generate,
    "history_rows": gen_history_rows.generate,
    "history_rows_python": gen_history_rows.generate_python,
    "store_rows_written": gen_store_rows_written.generate,
    "gitconfig": gen_gitconfig.generate,
    "location_stamps": gen_location_stamps.generate,
    "location_stamps_python": gen_location_stamps.generate_python,
    "location_stamps_platform": gen_location_stamps.generate_platform,
    "location_reads": gen_location_reads.generate,
    "attribution": gen_attribution.generate,
    "attribution_unicode": gen_attribution.generate_unicode,
    "caller_admission": gen_caller_admission.generate,
    "gitconfig_wildmatch": gen_gitconfig_wildmatch.generate,
    "origin_remote": gen_origin_remote.generate,
    "origin_remote_unicode": gen_origin_remote.generate_unicode,
    "origin_remote_python": gen_origin_remote.generate_python,
    "gitconfig_remote_rules": gen_gitconfig_remote_rules.generate,
    "origin_gitfiles_leaves": gen_origin_gitfiles.generate,
    "origin_gitfiles_leaves_python": gen_origin_gitfiles.generate_python,
    "origin_gitfiles_machine": gen_origin_gitfiles_machine.generate,
    "origin_gitfiles_machine_python": gen_origin_gitfiles_machine.generate_python,
    "origin_gitfiles_darwin": gen_origin_gitfiles_machine.generate_darwin,
    "origin_gitfiles_facts": gen_origin_gitfiles_facts.generate,
    "origin_gitfiles_facts_posix": gen_origin_gitfiles_facts.generate_posix,
    "ulid": gen_ulid.generate,
    "durability": gen_durability.generate,
    "durability_unicode": gen_durability.generate_unicode,
    "verify_status": gen_verify_status.generate,
    "githead": gen_githead.generate,
    "githead_discovery": gen_githead_discovery.generate,
    "location_probe": gen_location_probe.generate,
    "location_probe_python": gen_location_probe.generate_python,
    "location_probe_errno": gen_location_probe.generate_errno,
    "gitobjects": gen_gitobjects.generate,
    "gitobjects_python": gen_gitobjects.generate_python,
    "gitobjects_files": gen_gitobjects_files.generate,
    "gitobjects_files_posix": gen_gitobjects_files.generate_posix,
    "gitobjects_streams": gen_gitobjects_streams.generate,
    "githead_posix": gen_githead.generate_posix,
    "capture_vocabulary": gen_capture_vocabulary.generate,
    "capture_vocabulary_python": gen_capture_vocabulary.generate_python,
    "checkout_token": gen_checkout_token.generate,
    "checkout_token_read": gen_checkout_token_read.generate,
    "checkout_token_read_posix": gen_checkout_token_read.generate_posix,
    "gitconfig_file": gen_gitconfig_file.generate,
    "gitconfig_tree": gen_gitconfig_tree.generate,
    "gitconfig_tree_rules": gen_gitconfig_tree.generate_rules,
    "gitconfig_legacy": gen_gitconfig_legacy.generate,
    "location_remotes": gen_location_remotes.generate,
    "location_spellings": gen_location_spellings.generate,
    "location_stamped": gen_location_stamped.generate,
    "location_git_readers": gen_location_git_readers.generate,
    "location_git_readers_posix": gen_location_git_readers.generate_posix,
    "gitsystem_record": gen_gitsystem_record.generate,
    "gitsystem_record_python": gen_gitsystem_record.generate_python,
    "gitsystem_answers": gen_gitsystem_answers.generate,
    "gitsystem_answers_unicode": gen_gitsystem_answers.generate_unicode,
    "gitsystem_answers_python": gen_gitsystem_answers.generate_python,
    "gitsystem_probe": gen_gitsystem_probe.generate,
    "gitsystem_ask": gen_gitsystem_ask.generate,
    "gitsystem_ask_unicode": gen_gitsystem_ask.generate_unicode,
    "gitsystem_ask_python": gen_gitsystem_ask.generate_python,
    "gitsystem_which_windows": gen_gitsystem_ask.generate_which_windows,
    "gitsystem_sources": gen_gitsystem_sources.generate,
    "git_read_budget": gen_git_read_budget.generate,
    "gitsystem_sources_python": gen_gitsystem_sources.generate_python,
    "gitsystem_which_windows_python": (gen_gitsystem_ask.generate_which_windows_python),
    "gitsystem_probe_python": gen_gitsystem_probe.generate_python,
    "gitsystem_probe_names": gen_gitsystem_probe.generate_names,
    "gitsystem_probe_names_unicode": gen_gitsystem_probe.generate_names_unicode,
    "gitconfig_legacy_python": gen_gitconfig_legacy.generate_python,
    "membership_lines": gen_membership_lines.generate,
    "membership_lines_unicode": gen_membership_lines.generate_unicode,
    "membership_lines_python": gen_membership_lines.generate_python,
    "claims": gen_claims.generate,
    "claims_unicode": gen_claims.generate_unicode,
    "claims_python": gen_claims.generate_python,
    "gitfile_leaves": gen_gitfile_leaves.generate,
    "gitfile_leaves_posix": gen_gitfile_leaves.generate_posix,
    "store_provenance": gen_store_provenance.generate,
    "fs_realpath": gen_fs_realpath.generate,
    "fs_realpath_python": gen_fs_realpath.generate_python,
    "store_bulk": gen_store_bulk.generate,
    "fs_stat": gen_fs_stat.generate,
    "errno_class": gen_fs_stat.generate_errno_class,
    "fs_access": gen_fs_access.generate,
    "fs_realpath_strict": gen_fs_realpath.generate_strict,
    "fs_realpath_strict_python": gen_fs_realpath.generate_strict_python,
    "fs_samestat": gen_fs_samestat.generate,
    "fsutil": gen_fsutil.generate,
    "fsutil_python": gen_fsutil.generate_python,
    "gitdir_read": gen_gitdir.generate,
    "gitdir_read_python": gen_gitdir.generate_python,
    "gitfile_identity": gen_identity.generate,
    "gitfile_identity_python": gen_identity.generate_python,
    "gitfile_trust": gen_trust.generate,
    "gitfile_trust_python": gen_trust.generate_python,
    "gitfile_discovery": gen_discovery.generate,
    "gitfile_discovery_python": gen_discovery.generate_python,
    "gitfile_pin_text": gen_follow.generate_text,
    "gitfile_pin_text_python": gen_follow.generate_text_python,
    "gitfile_follow": gen_follow.generate,
    "gitfile_follow_python": gen_follow.generate_python,
}

#: The Unicode data version the golden files were generated under. The
#: generators gated on it read `unicodedata` or `re`'s word class for
#: their output; under another version they are not compared.
UNIDATA_VERSION = "15.1.0"
UNICODE_GATED = frozenset(
    {
        "attribution_unicode",
        "lexicon",
        "verify_candidates_unicode",
        "unicode",
        "models_text_unicode",
        "unicode_sequences",
        "pyre_unicode",
        "store",
        "store_path",
        "store_files",
        "search_ranker",
        "search_dedup_unicode",
        "mcp",
        "hooks",
        "credentials_prefixed_unicode",
        "credentials_gate_unicode",
        "control_tokens_unicode",
        "durability_unicode",
        "fold",
        "store_create",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "identity_caller_unicode",
        "membership_lines_unicode",
        "origin_remote_unicode",
        "origin_remote_python",
        "claims_unicode",
        "store_rows_written",
        "store_provenance",
        "store_bulk",
        "gitsystem_record",
        "gitsystem_answers_unicode",
        "gitsystem_probe_names_unicode",
        "gitsystem_ask_unicode",
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
#: ``gitsystem_record_python`` holds records nested past 1,000 levels,
#: where `json.loads` raising RecursionError is the interpreter's.
#: ``gitsystem_answers_python`` holds Windows paths a separator then a colon
#: begins (3.11's `ntpath.splitdrive` reads a drive there) and digit runs
#: past `int`'s limit. ``gitsystem_probe_python`` checks links and folders
#: through a folder that cannot be searched, where 3.13's `Path.is_dir`
#: raises and 3.14's answers False. ``gitsystem_which_windows_python``
#: names ``NoDefaultCurrentDirectoryInExePath`` or a PATHEXT entry ending in
#: a dot, which `shutil.which` before 3.12 does not read as 3.13 does.
#: ``history_rows_python`` holds log payloads nested near `json.loads`'s
#: recursion budget (`gen_history_rows`).
#: ``origin_remote_python`` holds URLs with a bracket, whose host checks in
#: `urlsplit` and `ipaddress` are the release's (`gen_origin_remote`).
#: ``gitfile_pin_text_python`` holds pin records read through ``ntpath``
#: whose path is rooted without a drive (``/g``): `ntpath.isabs` reads it
#: as absolute in 3.11 and 3.12 and as relative from 3.13 (`gen_follow`).
#: ``pyre_python`` holds the interpreter's own version, ``_sre.MAGIC`` and
#: the digests of the ``re`` sources the engine was read from (`gen_pyre`),
#: and patterns with an empty negative lookaround, which `re._parser`
#: parses to FAILURE from 3.13 (`gen_pyre_tree`).
#: ``location_stamps_platform`` holds `os.major` and `os.minor` of NODEV
#: (-1) and of numbers past ``dev_t``. 3.11 truncates a number macOS's
#: 32-bit ``dev_t`` cannot hold, where 3.12.13 and 3.13 raise; 3.11 and
#: 3.12.3 raise for NODEV, which 3.12.13 and 3.13 accept, and these answer
#: a part of 0xffffffff on glibc as -1 (`gen_location_stamps`).
#: ``location_stamps_python`` and ``origin_gitfiles_machine_python`` hold
#: the ValueError `os.stat` raises for a NUL in a path, "embedded null
#: byte" in 3.11.15 and "stat: embedded null character in path" in
#: 3.12.13, 3.13 and 3.14 (`gen_location_stamps`,
#: `gen_origin_gitfiles_machine`).
#: ``membership_records_paths_platform`` holds rows of the Python-gated
#: file as well as release-stable ones (`gen_membership_records`).
#: ``location_probe_python`` probes link loops, which 3.11's `realpath`
#: walks otherwise, and paths holding a NUL, whose ValueError text 3.11
#: words otherwise (`gen_location_probe`).
PYTHON_GATED = frozenset(
    {
        "models_text_platform",
        "location_stamps_platform",
        "location_stamps_python",
        "origin_gitfiles_machine_python",
        "gitsystem_record_python",
        "gitsystem_answers_python",
        "gitsystem_probe_python",
        "gitsystem_ask_python",
        "gitsystem_which_windows_python",
        "gitsystem_sources_python",
        "history_rows_python",
        "origin_gitfiles_leaves_python",
        "pyre_python",
        "python_edges",
        "models_rows",
        "models_stores",
        "toml",
        "settings_python",
        "settings_paths_python",
        "membership_lines_python",
        "membership_records_paths_python",
        "membership_records_paths_platform",
        "membership_rows_python",
        "origin_remote_python",
        "claims_python",
        "capture_vocabulary_python",
        "fs_realpath_python",
        "fs_realpath_strict_python",
        "fsutil_python",
        "gitconfig_legacy_python",
        "gitdir_read_python",
        "gitfile_identity_python",
        "gitfile_trust_python",
        "gitfile_follow_python",
        "gitfile_pin_text_python",
        "gitfile_discovery_python",
        "gitobjects_python",
        "machine_identity_python",
        "location_probe_python",
    }
)

#: The zlib CPython links (`zlib.ZLIB_RUNTIME_VERSION`). ``gitobjects_streams``
#: pins what `zlib.decompressobj` takes of a stream and returns, and when
#: it raises: where that zlib's `inflate` stops and how much input it has
#: consumed there. The streams are written bit by bit, so no byte of them
#: is a compressor's, but the stopping points are the inflate's. macOS's
#: libz 1.2.12 and the native build's `zlib-rs` agree on them; no other
#: build has been compared, so under another the file is not compared.
ZLIB_VERSION = "1.2.12"
ZLIB_GATED = frozenset({"gitobjects_streams"})


def zlib_version() -> str:
    return zlib.ZLIB_RUNTIME_VERSION


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
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "store_rows_written",
        "store_provenance",
        "store_bulk",
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
#: ``location_stamps_platform`` makes the running platform's device names
#: and carries the others' as recorded (`gen_location_stamps`); on Windows
#: it adds a section of its own.
#: ``location_probe_errno`` raises every errno number, whose meaning and
#: whose text (the C library's `strerror`) are the platform's.
#: ``membership_records_paths_platform`` holds the cases whose reference
#: answer follows the host: on Windows `ntpath.normcase` lowers through
#: ``LCMapStringEx`` (U+0130 kept, no final sigma) and `ntpath.abspath`
#: reads the process's folder through ``_getfullpathname``, where on a
#: POSIX host both are Python (`gen_membership_records`).
PLATFORM = "darwin-arm64"
PLATFORM_GATED = frozenset(
    {
        "models_text_platform",
        "location_stamps_platform",
        "search_ranker",
        "store",
        "store_path",
        "store_files",
        "store_restore_lock",
        "mcp",
        "origin_gitfiles_darwin",
        "hooks",
        "location_probe_errno",
        "membership_records_paths_platform",
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
#: otherwise (`gen_githead`); ``githead_discovery`` walks trees with links,
#: FIFOs and modes and reads owners and devices through ``os.lstat`` and
#: ``os.stat`` (`gen_githead_discovery`); ``gitconfig_file`` reads links, FIFOs and
#: mode-000 files and opens directories (`gen_gitconfig_file`);
#: ``fs_realpath`` and ``fs_realpath_python`` resolve links and FIFOs by
#: ``posixpath`` (`gen_fs_realpath`); ``fs_stat`` states and lists links,
#: FIFOs and mode-000 folders (`gen_fs_stat`); ``errno_class`` names POSIX
#: errnos (`gen_fs_stat`); ``fs_access`` asks ``access`` and opens with
#: ``O_NOFOLLOW`` over links, FIFOs and modes (`gen_fs_access`);
#: ``fs_realpath_strict`` and ``fs_realpath_strict_python`` resolve the
#: same trees strictly; ``fs_samestat`` states pairs over links and hard
#: links (`gen_fs_samestat`); ``gitdir_read`` and ``gitdir_read_python``
#: read git directories over links, FIFOs and modes (`gen_gitdir`);
#: ``gitfile_identity`` and ``gitfile_identity_python`` state and list
#: trees with links, hard links and modes (`gen_identity`);
#: ``gitfile_trust`` and ``gitfile_trust_python`` read configs and
#: ``commondir`` files over links and modes (`gen_trust`);
#: ``gitfile_follow`` and ``gitfile_follow_python`` follow ``.git`` entries
#: over links, modes and pin records (`gen_follow`);
#: ``gitfile_discovery`` and ``gitfile_discovery_python`` judge ``.git``
#: entries and walk real trees with links, FIFOs and modes
#: (`gen_discovery`);
#: ``gitconfig_tree`` reads real trees with the host's ``os.path`` and ``pwd``;
#: ``gitconfig_legacy`` and ``gitconfig_legacy_python`` read real trees with
#: links, FIFOs and modes (`gen_gitconfig_legacy`; the second holds the link
#: loops); ``location_remotes`` and ``location_spellings`` read real trees
#: with modes, links and FIFOs (`gen_location_remotes`); ``location_stamped``
#: changes such trees between reads (`gen_location_stamped`);
#: ``gitobjects_files_posix`` walks alternates over links, FIFOs and modes,
#: its directories spelled by ``posixpath.realpath`` (`gen_gitobjects_files`);
#: ``location_git_readers_posix`` walks refs and trees with links and mode-000
#: entries (`gen_location_git_readers`);
#: ``gitsystem_record`` reads and writes the record over links, modes and
#: FIFOs (`gen_gitsystem_record`); ``gitsystem_probe`` builds, sweeps and
#: clears the probe over links, modes and FIFOs (`gen_gitsystem_probe`);
#: ``gitfile_leaves_posix`` reads links and mode-000 files
#: (`gen_gitfile_leaves`); ``checkout_token_read_posix`` reads links, a
#: FIFO and a loop (`gen_checkout_token_read`); ``checkout_token_read``
#: runs the reads over a scripted OS whose logged calls spell the path as
#: the host's ``Path`` does (backslashes on Windows) and whose errno names
#: are the host's ``errno`` module's (``WSAELOOP`` for ELOOP on Windows),
#: so its bytes follow the OS family (`gen_checkout_token_read`);
#: ``location_stamps`` and
#: ``location_stamps_python`` name devices by ``os.major``, which Windows'
#: ``os`` lacks (`gen_location_stamps`); ``location_probe``, ``location_probe_python``
#: and ``location_probe_errno`` probe trees with links and a mode-000
#: folder, the last raising POSIX errno numbers (`gen_location_probe`);
#: ``origin_gitfiles_facts_posix`` joins with the host's ``os.path``
#: (``ntpath`` on Windows) and holds trees with links and a mode-000
#: folder (`gen_origin_gitfiles_facts`);
#: ``store_schema_posix`` runs the schema steps on a mode-0444 store file
#: and spells a v8 folder's path as the host's ``Path`` does
#: (`gen_store_schema`).
POSIX_ONLY = frozenset(
    {
        "checkout_token_read",
        "checkout_token_read_posix",
        "store_restore_lock",
        "store_schema_posix",
        "settings_paths",
        "settings_paths_python",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "store_rows_written",
        "githead_posix",
        "gitobjects_files_posix",
        "githead_discovery",
        "location_probe",
        "location_probe_python",
        "location_probe_errno",
        "gitconfig_file",
        "location_stamps",
        "location_stamps_python",
        "gitconfig_tree",
        "gitconfig_legacy",
        "gitconfig_legacy_python",
        "location_remotes",
        "location_spellings",
        "location_stamped",
        "location_git_readers_posix",
        "origin_gitfiles_machine",
        "origin_gitfiles_machine_python",
        "origin_gitfiles_darwin",
        "origin_gitfiles_facts_posix",
        "gitsystem_record",
        "gitsystem_record_python",
        "gitsystem_probe",
        "gitsystem_probe_python",
        "gitsystem_ask",
        "gitsystem_ask_unicode",
        "gitsystem_ask_python",
        "gitsystem_sources",
        "gitsystem_sources_python",
        "gitfile_leaves_posix",
        "store_provenance",
        "fs_realpath",
        "fs_realpath_python",
        "store_bulk",
        "fs_stat",
        "errno_class",
        "fs_access",
        "fs_realpath_strict",
        "fs_realpath_strict_python",
        "fs_samestat",
        "gitdir_read",
        "gitdir_read_python",
        "gitfile_identity",
        "gitfile_identity_python",
        "gitfile_trust",
        "gitfile_trust_python",
        "gitfile_discovery",
        "gitfile_discovery_python",
        "gitfile_follow",
        "gitfile_follow_python",
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
    if name in ZLIB_GATED and zlib_version() != ZLIB_VERSION:
        out.append(f"zlib {zlib_version()} (files: {ZLIB_VERSION})")
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
        "zlib": zlib_version(),
        "pydantic_core": pydantic_core_version(),
        "committed": {
            "platform": PLATFORM,
            "python": PYTHON_VERSION,
            "sqlite": SQLITE_VERSION,
            "unicode": UNIDATA_VERSION,
            "zlib": ZLIB_VERSION,
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
    if zlib_version() != ZLIB_VERSION:
        print(
            f"refusing: zlib {zlib_version()}, golden files are {ZLIB_VERSION}",
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
