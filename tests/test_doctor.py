"""Tests for `bettermemory doctor`.

Each diagnostic in `doctor.py` is exercised in isolation via the
`_check_*` helpers; integration is covered by `run_diagnostics` and
`cli_doctor`. The file uses tmp_path-backed `Config` instances rather
than touching the user's real config — doctor must never side-effect
the host environment under test.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import typing
from pathlib import Path
from typing import Any

import pytest

from bettermemory import sync

from .conftest import set_git_discovery_ceiling
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.doctor import (
    CheckStatus,
    Diagnosis,
    DoctorReport,
    FixResult,
    _binary_dist_version,
    _check_audit_turn_cadence,
    _check_auto_memory_stranded,
    _check_binary_on_path,
    _check_config_loadable,
    _check_distinfo_metadata,
    _check_embeddings_extra,
    _check_event_log_writable,
    _check_index_health,
    _check_mcp_client_configs,
    _check_memory_parse_health,
    _check_python_version,
    _check_stale_config_lockfiles,
    _check_storage_directory,
    _check_store_nested_in_parent_repo,
    _check_sync_tracked_ignored,
    _discover_site_packages,
    _fix_context,
    _pattern_matches_tracked_path,
    _probe_index_integrity,
    _EXIT_CODE_BY_STATUS,
    _FIXERS,
    _STATUS_GLYPH,
    cli_doctor,
    render_fixes_text,
    render_json,
    render_text,
    run_diagnostics,
    run_fixes,
)
from bettermemory.init import ClientPaths
from bettermemory.proposals import PROPOSALS_FILENAME


# ---------------------------------------------------------------------------
# Tiny fixtures
# ---------------------------------------------------------------------------


def _config_for(tmp_path: Path, **behavior_kwargs: Any) -> Config:
    """A Config pinned at tmp_path so doctor never touches the user's
    real ~/.claude-memory/."""
    return Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(**behavior_kwargs),
    )


# ---------------------------------------------------------------------------
# python_version
# ---------------------------------------------------------------------------


def test_python_version_passes_on_current_runtime() -> None:
    diag = _check_python_version()
    assert diag.status == "ok"
    assert diag.details["version"]


# ---------------------------------------------------------------------------
# binary_on_path
# ---------------------------------------------------------------------------


def test_binary_on_path_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bettermemory.doctor.shutil.which",
        lambda _name: "/usr/local/bin/bettermemory",
    )
    diag = _check_binary_on_path()
    assert diag.status == "ok"
    assert "/usr/local/bin/bettermemory" in diag.message


def test_binary_on_path_warns_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the binary isn't on $PATH, doctor warns and emits a generic
    hint that points at `bettermemory init` / `which bettermemory`.
    Pre-Round-3 doctor substituted the resolved invocation path into
    the hint to save the user a lookup; the substitution turned into a
    footgun on machines with parallel installs (pipx + `uv tool
    install` + a venv shim — pasting the doctor-resolved path into the
    MCP config silently pinned a shim the user didn't intend). The
    resolved path now lives in `details.resolved_path` only — tooling
    that wants it can read it, but the user-facing hint never
    pretends to know which shim is canonical."""
    real_binary = tmp_path / "bettermemory"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("bettermemory.doctor.shutil.which", lambda _name: None)
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(real_binary))
    # `sys.argv[0]` could be anything in pytest's process — pin it so
    # the secondary "argv0 is absolute" branch doesn't accidentally fire.
    monkeypatch.setattr("bettermemory.doctor.sys.argv", ["pytest"])
    diag = _check_binary_on_path()
    assert diag.status == "warn"
    assert diag.fix_hint is not None
    # The resolved path must NOT appear in the hint — that was the
    # footgun. It should still be present in details for tooling.
    assert str(real_binary) not in (diag.fix_hint or "")
    assert "which bettermemory" in (diag.fix_hint or "") or "init" in (
        diag.fix_hint or ""
    )
    assert (diag.details or {}).get("resolved_path") == str(real_binary)


def test_binary_on_path_warn_hint_stays_generic_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `find_binary()` returns the bare `"bettermemory"`
    last-resort (no `shutil.which`, no absolute `sys.argv[0]` to fall
    back on), the hint must NOT embed the bare string — that would
    suggest `bettermemory` is the absolute path. Stay generic and
    point at `which`."""
    monkeypatch.setattr("bettermemory.doctor.shutil.which", lambda _name: None)
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: "bettermemory")
    # Pin argv to a non-bettermemory absolute path so the secondary
    # branch doesn't fire either.
    monkeypatch.setattr("bettermemory.doctor.sys.argv", ["/usr/bin/python3"])
    diag = _check_binary_on_path()
    assert diag.status == "warn"
    assert diag.fix_hint is not None
    hint = diag.fix_hint or ""
    # Bare "bettermemory" must not appear as a path-shaped substring in
    # the hint. The hint should suggest looking it up rather than
    # presenting the unresolved name as the answer.
    assert "configs: bettermemory" not in hint
    assert "which bettermemory" in hint or "init" in hint


# ---------------------------------------------------------------------------
# config_loadable
# ---------------------------------------------------------------------------


def test_config_loadable_ok() -> None:
    diag, cfg = _check_config_loadable()
    assert diag.status == "ok"
    assert cfg is not None


def test_config_loadable_fail_surfaces_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated config corruption")

    monkeypatch.setattr("bettermemory.doctor.load_config", boom)
    diag, cfg = _check_config_loadable()
    assert diag.status == "fail"
    assert cfg is None
    assert "RuntimeError" in diag.message


# ---------------------------------------------------------------------------
# storage_directory
# ---------------------------------------------------------------------------


def test_storage_directory_ok(tmp_path: Path) -> None:
    cfg = _config_for(tmp_path)
    diag, resolved = _check_storage_directory(cfg)
    assert diag.status == "ok"
    assert resolved == tmp_path.resolve()


def test_storage_directory_will_be_created(tmp_path: Path) -> None:
    """A storage path that doesn't exist is OK as long as the parent is
    writable — Store.create_dirs handles the lazy mkdir."""
    target = tmp_path / "fresh_install"
    cfg = Config(storage=StorageConfig(directory=str(target)))
    diag, resolved = _check_storage_directory(cfg)
    assert diag.status == "ok"
    assert "will be created" in diag.message
    assert resolved is not None


def test_storage_directory_fails_when_not_a_directory(tmp_path: Path) -> None:
    fake = tmp_path / "not_a_dir"
    fake.write_text("oops", encoding="utf-8")
    cfg = Config(storage=StorageConfig(directory=str(fake)))
    diag, _resolved = _check_storage_directory(cfg)
    assert diag.status == "fail"
    assert "not a directory" in diag.message


def test_storage_directory_fails_when_unwritable(tmp_path: Path) -> None:
    cfg = _config_for(tmp_path)
    tmp_path.chmod(0o555)
    try:
        diag, _resolved = _check_storage_directory(cfg)
    finally:
        tmp_path.chmod(0o755)  # restore so pytest can clean up
    # On some platforms (CI runners as root, Windows) the chmod doesn't
    # actually deny write — skip the assertion if the probe still passes.
    if diag.status == "ok":
        pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")
    assert diag.status == "fail"


# ---------------------------------------------------------------------------
# memory_parse_health
# ---------------------------------------------------------------------------


def test_memory_parse_health_clean(tmp_path: Path) -> None:
    # No memories at all is still "all parse cleanly" (count = 0).
    diag = _check_memory_parse_health(tmp_path)
    assert diag.status == "ok"
    assert diag.details["parsed"] == 0


def test_memory_parse_health_warns_on_corrupt_file(tmp_path: Path) -> None:
    """A .md file that looks like a memory but has malformed frontmatter
    is skipped by Store.load_all (silently — the skip path emits no log).
    Doctor should surface the count discrepancy."""
    bad = tmp_path / "01ZZ_corrupt.md"
    bad.write_text("---\nbad: : :\n---\nbody\n", encoding="utf-8")
    diag = _check_memory_parse_health(tmp_path)
    # Either the file parsed (yaml is permissive enough) or it didn't.
    # If it didn't, status is "warn" and details show the gap.
    if diag.status == "warn":
        assert diag.details["skipped"] >= 1
    else:
        # If it parsed, that's also fine — the test was overly defensive
        # about what malformed YAML actually trips. Skip rather than
        # fail the suite.
        pytest.skip("YAML parser was permissive enough to read the file")


def test_memory_parse_health_ignores_symlinks(tmp_path: Path) -> None:
    """A .md symlink is rejected by Store._iter_active_paths as a security
    boundary BEFORE parsing, so doctor must NOT count it as a parse failure
    (a false 'check frontmatter' steer pointing at a file never read)."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="a real memory body about widgets", scopes=["tools"])
    real_file = next(p for p in tmp_path.glob("*.md"))
    (tmp_path / "link-to-mem.md").symlink_to(real_file)

    diag = _check_memory_parse_health(tmp_path)
    assert diag.status == "ok", f"symlink miscounted as a parse failure: {diag.message}"


def test_memory_parse_health_counts_readme_and_dotfiles_like_the_store(
    tmp_path: Path,
) -> None:
    """The store's enumeration (`_iter_active_paths` / the shared count
    helpers) makes no exception for README.md or dot-prefixed `.md`
    names — they parse and index like any other memory file. Doctor's
    old hand-rolled filter excluded them, so on a store containing
    such files index_health counted them unparseable and deferred to
    memory_parse_health, which then reported fewer skips (or "all
    parse cleanly") — two checks in one report disagreeing about the
    same directory. Pin both checks to the store's enumeration: every
    file index_health subtracts must show up in parse-health's
    `skipped`."""
    from bettermemory.store import Store, count_unparseable_memory_files

    store = Store(tmp_path)
    store.write(content="a real memory body about widgets", scopes=["tools"])
    (tmp_path / "README.md").write_text("# Not a memory\n", encoding="utf-8")
    (tmp_path / ".notes.md").write_text("dot-prefixed, no fm\n", encoding="utf-8")
    (tmp_path / "junk.md").write_text("no frontmatter at all\n", encoding="utf-8")

    parse_diag = _check_memory_parse_health(tmp_path)
    assert parse_diag.status == "warn"
    assert parse_diag.details["parsed"] == 1
    assert parse_diag.details["files_on_disk"] == 4
    assert parse_diag.details["skipped"] == 3
    # Reconcile with the store helper index_health / the S4 warning share.
    assert parse_diag.details["skipped"] == count_unparseable_memory_files(tmp_path)

    # index_health on the same store: the gap is fully explained by the
    # unparseable files, and its arithmetic must agree file-for-file
    # with parse-health's (disk == files_on_disk, unparseable == skipped).
    index_diag = _check_index_health(tmp_path)
    assert index_diag.status == "ok"
    assert index_diag.details["disk_count"] == parse_diag.details["files_on_disk"]
    assert index_diag.details["unparseable_count"] == parse_diag.details["skipped"]


def test_memory_parse_health_does_not_create_missing_dir(tmp_path: Path) -> None:
    """The read-only probe must not materialize a non-existent storage dir
    (Store.__post_init__ would mkdir it + a .tombstones/). Early-return."""
    ghost = tmp_path / "ghost"
    diag = _check_memory_parse_health(ghost)
    assert diag.status == "ok"
    assert not ghost.exists()


# ---------------------------------------------------------------------------
# index_health
# ---------------------------------------------------------------------------


def test_index_health_missing_dir_does_not_create_it(tmp_path: Path) -> None:
    """A read-only probe against a never-created storage dir must not
    materialize anything (same contract as memory_parse_health)."""
    ghost = tmp_path / "ghost"
    diag = _check_index_health(ghost)
    assert diag.status == "ok"
    assert not ghost.exists()


def test_index_health_ok_on_empty_store(tmp_path: Path) -> None:
    """Existing-but-empty storage dir: no index file is the healthy
    state (it's created on the first write), not a divergence."""
    diag = _check_index_health(tmp_path)
    assert diag.status == "ok"
    assert diag.details["disk_count"] == 0
    assert list(tmp_path.iterdir()) == []  # probe stays read-only


def test_index_health_healthy_index_matches_disk(tmp_path: Path) -> None:
    """Writes through the Store keep the index live via hooks — a
    healthy store reports ok with indexed_count == disk_count."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    store.write(content="beta indexer note", scopes=["tools"])
    diag = _check_index_health(tmp_path)
    assert diag.status == "ok"
    assert diag.details["indexed_count"] == 2
    assert diag.details["disk_count"] == 2
    # The healthy verdict must say what was actually verified: counts
    # AND a real page walk, not counts alone (a torn interior page has
    # clean counts — see test_index_health_warns_on_torn_interior_page).
    assert "quick_check" in diag.message
    assert diag.details["quick_check"] == "ok"


def test_index_health_warns_on_corrupt_index(tmp_path: Path) -> None:
    """A garbage .index.sqlite makes `index.status()` report
    `corrupt=True` (never raises); doctor must surface it with the
    reindex repair instead of reporting all-ok while every
    memory_search silently degrades to a linear scan."""
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"not a sqlite database at all " * 16)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    assert "corrupt" in diag.message.lower()
    assert "reindex" in (diag.fix_hint or "")
    assert diag.details["corrupt"] is True


def test_index_health_warns_when_rebuild_pending(tmp_path: Path) -> None:
    """A schema-version migration drops the data tables and flags
    `needs_rebuild`; until `rebuild()` clears it, memory_search bypasses
    the index. Roll the on-disk version backwards (the idiom from
    test_index.py's migration test) — the check's own `status()` call
    runs the migration and must then report the pending rebuild."""
    import sqlite3

    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    conn = sqlite3.connect(index_path(tmp_path))
    try:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    assert "rebuild-pending" in diag.message
    assert "reindex" in (diag.fix_hint or "")
    assert diag.details["needs_rebuild"] is True


def _tear_fts_interior_page(index_file: Path) -> None:
    """Scribble over the root page of the `memories_fts_data` shadow
    table, leaving the header, sqlite_master, and `meta` pages intact —
    the extent-corruption shape `index.status()`'s meta-only reads
    cannot see. Checkpoints the WAL first so the main file is the
    authoritative copy of the page being torn."""
    import sqlite3

    conn = sqlite3.connect(index_file)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rootpage = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'memories_fts_data'"
        ).fetchone()[0]
    finally:
        conn.close()
    raw = index_file.read_bytes()
    # DB header offset 16 holds the page size (big-endian; 1 means 64 KiB).
    size_field = int.from_bytes(raw[16:18], "big")
    page_size = 65536 if size_field == 1 else size_field
    start = (rootpage - 1) * page_size
    assert len(raw) >= start + page_size, "fixture: rootpage past EOF"
    index_file.write_bytes(
        raw[:start] + b"\xde\xad\xbe\xef" * (page_size // 4) + raw[start + page_size :]
    )


def test_index_health_warns_on_torn_interior_page(tmp_path: Path) -> None:
    """Extent corruption: a torn interior page in the FTS shadow tables
    leaves the header, sqlite_master, and `meta` pages readable, so
    `index.status()` — meta-only by design; it runs on every Store
    construction — reports clean counts, and pre-fix doctor certified
    the index 'healthy: N memories indexed (matches disk)' while any
    read touching the damaged pages (an FTS MATCH, the next rebuild's
    table sweep) raises `database disk image is malformed`. Doctor runs
    on demand and can afford the page walk: PRAGMA quick_check must
    classify this as corrupt with the reindex repair — not crash, and
    not certify."""
    from bettermemory import index
    from bettermemory.store import Store

    store = Store(tmp_path)
    for i in range(3):
        store.write(content=f"memory number {i} about widgets", scopes=["tools"])
    _tear_fts_interior_page(index.index_path(tmp_path))

    # Premise pin: the meta-only surface sees nothing wrong. If this
    # ever starts reporting corrupt, status() grew data-page reads and
    # the doctor-local probe may be redundant — revisit the design
    # rather than patching the assertion.
    s = index.status(tmp_path)
    assert s.get("exists") and not s.get("corrupt") and not s.get("needs_rebuild")
    assert s.get("indexed_count") == 3

    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    assert "quick_check" in diag.message
    assert "reindex" in (diag.fix_hint or "")
    assert diag.details["quick_check"] != "ok"


def test_probe_index_integrity_missing_file_reports_without_creating(
    tmp_path: Path,
) -> None:
    """The probe opens read-only (URI mode=ro): a file that vanishes
    between status() and the probe (concurrent rebuild-recovery) must
    come back as a finding string — the degraded answer, never a raise
    — and must NOT be created as an empty database by the probe itself
    (a plain `sqlite3.connect` would create it)."""
    ghost = tmp_path / ".index.sqlite"
    err = _probe_index_integrity(ghost)
    assert err is not None
    assert not ghost.exists()


_OUT_OF_BAND_MEMORY = (
    "---\n"
    "schema_version: 1\n"
    "id: 01HXYZAAAAAAAAAAAAAAAAAAAA\n"
    "created: 2026-01-01T00:00:00Z\n"
    "updated: 2026-01-01T00:00:00Z\n"
    "scopes:\n  - tools\n"
    "confidence: medium\n"
    "source: explicit-statement\n"
    "---\n"
    "body written outside the Store API\n"
)


def test_index_health_warns_on_disk_divergence(tmp_path: Path) -> None:
    """A *parseable* .md file dropped outside the Store API (sync pull,
    hand copy, generic Write tool) leaves the index count behind disk —
    the S4 divergence shape, reported with the reindex repair (which,
    because the file parses, genuinely clears it — see the follow-up
    assertion)."""
    from bettermemory import index
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    (tmp_path / "hand-copied.md").write_text(_OUT_OF_BAND_MEMORY, encoding="utf-8")
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    assert "out of sync" in diag.message
    assert "reindex" in (diag.fix_hint or "")
    assert diag.details["indexed_count"] == 1
    assert diag.details["disk_count"] == 2
    assert diag.details["unparseable_count"] == 0
    # The prescribed repair must actually clear the warning.
    index.rebuild(tmp_path, store.iter_active())
    assert _check_index_health(tmp_path).status == "ok"


def test_index_health_ok_when_gap_is_only_unparseable_files(tmp_path: Path) -> None:
    """N parseable + 1 permanently-unparseable file with the index at N:
    the raw counts read N+1 vs N, but `index.rebuild` consumes
    `iter_active()` and can never index the junk file — a warn here
    prescribes a repair that can never clear it (the user reindexes,
    the warning persists, trust erodes). The index is as synced as a
    rebuild can make it; the junk file is memory_parse_health's finding."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    (tmp_path / "junk.md").write_text("no frontmatter at all\n", encoding="utf-8")
    diag = _check_index_health(tmp_path)
    assert diag.status == "ok"
    assert "unparseable" in diag.message
    assert diag.details["indexed_count"] == 1
    assert diag.details["disk_count"] == 2
    assert diag.details["unparseable_count"] == 1
    # This ok path certifies too — it must carry the same page-walk
    # attestation as the full-match branch.
    assert diag.details["quick_check"] == "ok"
    # ... and the sibling check owns the actual defect.
    assert _check_memory_parse_health(tmp_path).status == "warn"


def test_index_health_real_divergence_annotated_with_unparseable_count(
    tmp_path: Path,
) -> None:
    """Mixed shape: one parseable out-of-band file (real divergence a
    rebuild fixes) plus one junk file (a rebuild never indexes). The
    warn must fire, with the message annotating the unparseable count
    so the post-reindex arithmetic is visibly explained — and the
    prescribed reindex must then land the check on ok, not on a fresh
    warn about the junk file."""
    from bettermemory import index
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    (tmp_path / "hand-copied.md").write_text(_OUT_OF_BAND_MEMORY, encoding="utf-8")
    (tmp_path / "junk.md").write_text("no frontmatter at all\n", encoding="utf-8")
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    assert "out of sync" in diag.message
    assert "unparseable" in diag.message
    assert "index=2" in diag.message  # the reachable post-rebuild count
    assert diag.details["unparseable_count"] == 1
    assert "reindex" in (diag.fix_hint or "")
    index.rebuild(tmp_path, store.iter_active())
    diag = _check_index_health(tmp_path)
    assert diag.status == "ok"
    assert diag.details["indexed_count"] == 2


# ---------------------------------------------------------------------------
# event_log
# ---------------------------------------------------------------------------


def test_event_log_brand_new_dir_is_ok(tmp_path: Path) -> None:
    diag = _check_event_log_writable(tmp_path)
    assert diag.status == "ok"


def test_event_log_existing_writable(tmp_path: Path) -> None:
    log = tmp_path / ".events.jsonl"
    log.write_text('{"ts":"x","kind":"search"}\n', encoding="utf-8")
    diag = _check_event_log_writable(tmp_path)
    assert diag.status == "ok"
    assert diag.details["bytes"] > 0


def test_event_log_unwritable_fails(tmp_path: Path) -> None:
    log = tmp_path / ".events.jsonl"
    log.write_text("", encoding="utf-8")
    log.chmod(0o400)
    try:
        diag = _check_event_log_writable(tmp_path)
    finally:
        log.chmod(0o644)
    if diag.status == "ok":
        pytest.skip("filesystem ignored chmod; cannot exercise unwritable file")
    assert diag.status == "fail"


def test_event_log_probe_append_catches_access_false_green(tmp_path: Path) -> None:
    """os.access alone false-greens when the permission bits look
    writable but a real append fails — the Windows-ACL shape: on nt,
    os.access consults only FILE_ATTRIBUTE_READONLY, the exact bit
    `--fix`'s chmod(0o600) clears, so without a real probe the fixer's
    after re-run would verify its own chmod circularly. A directory
    squatting at the log path reproduces the divergence
    deterministically on every platform: W_OK passes, open('ab')
    raises."""
    from bettermemory.events import EVENT_LOG_FILENAME

    log = tmp_path / EVENT_LOG_FILENAME
    log.mkdir()
    assert os.access(log, os.W_OK)  # the cheap pre-guard passes...
    diag = _check_event_log_writable(tmp_path)
    assert diag.status == "fail"  # ...but the probe-append tells the truth
    assert "append" in diag.message
    assert diag.fix_hint is not None
    assert "chmod u+w" not in diag.fix_hint  # a chmod cannot fix this class


def test_event_log_unwritable_hint_executes_on_space_and_quote_path(
    tmp_path: Path,
) -> None:
    """The unwritable-log fix_hint interpolated the storage path into
    `chmod u+w {log_path}` RAW — the last unquoted command surface in
    doctor (32e2862 quoted the pathspec hints but missed this one). A
    space-bearing storage dir (the macOS `Application Support`
    neighbourhood is the DEFAULT config location) shell-splits the
    pasted command into two bogus operands, and a quote-bearing one
    additionally leaves it quote-imbalanced — so pin the hardest legal
    shape: a dir with BOTH. The emitted command must round-trip
    `shlex.split` to exactly ['chmod', 'u+w', <path>] and, through a
    real POSIX shell (the paste target the hint is written for),
    EXECUTE and actually restore writability."""
    from bettermemory.events import EVENT_LOG_FILENAME

    storage = tmp_path / "Application Support o'brien"
    storage.mkdir()
    log = storage / EVENT_LOG_FILENAME
    log.write_text("", encoding="utf-8")
    log.chmod(0o400)
    try:
        diag = _check_event_log_writable(storage)
        if diag.status == "ok":
            pytest.skip("filesystem ignored chmod; cannot exercise unwritable file")
        assert diag.status == "fail"
        match = re.search(r"`(chmod u\+w [^`]*)`", diag.fix_hint or "")
        assert match is not None
        command = match.group(1)
        # POSIX splitting must round-trip to the one path operand — the
        # raw interpolation dies right here: the space splits the path
        # in two and the quote raises "No closing quotation".
        assert shlex.split(command) == ["chmod", "u+w", str(log)]
        if sys.platform != "win32":
            # Execute the emitted remediation verbatim: rc=0, and the
            # prescribed fix genuinely clears the failing check.
            ran = subprocess.run(
                command, shell=True, capture_output=True, text=True, check=False
            )
            assert ran.returncode == 0, ran.stderr
            assert os.access(log, os.W_OK)
            assert _check_event_log_writable(storage).status == "ok"
    finally:
        log.chmod(0o644)


# ---------------------------------------------------------------------------
# audit_turn_cadence (M-doctor-hook)
# ---------------------------------------------------------------------------


def _write_event(directory: Path, kind: str, *, ts: str, session: str = "s1") -> None:
    """Append one event line directly to the event log.

    We write raw JSONL rather than going through `events.Recorder` so
    the test can pin the timestamp without monkey-patching the clock.
    The `audit_turn_cadence` check reads via `iter_all_events`, which
    parses the same JSONL.
    """
    import json

    log = directory / ".events.jsonl"
    payload = {"ts": ts, "session": session, "kind": kind}
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def test_audit_turn_cadence_empty_dir_is_ok(tmp_path: Path) -> None:
    """No events at all means we have nothing to compare against;
    don't false-warn the user on a brand-new install."""
    diag = _check_audit_turn_cadence(tmp_path)
    assert diag.status == "ok"


def test_audit_turn_cadence_recent_audits_pass(tmp_path: Path) -> None:
    """Several recent `turn_audited` events across multiple sessions:
    the hook is firing, nothing to warn about."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_event(tmp_path, "search", ts=now_iso, session="s1")
    _write_event(tmp_path, "turn_audited", ts=now_iso, session="s1")
    _write_event(tmp_path, "turn_audited", ts=now_iso, session="s2")
    diag = _check_audit_turn_cadence(tmp_path)
    assert diag.status == "ok"
    assert diag.details["turn_audited_events"] == 2


def test_audit_turn_cadence_silent_hook_warns(tmp_path: Path) -> None:
    """Recent session activity but zero `turn_audited` events: the
    Stop hook is mis-wired (or absent). Soft warning."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_event(tmp_path, "search", ts=now_iso, session="s1")
    _write_event(tmp_path, "write", ts=now_iso, session="s1")
    _write_event(tmp_path, "search", ts=now_iso, session="s2")
    diag = _check_audit_turn_cadence(tmp_path)
    assert diag.status == "warn"
    assert diag.fix_hint is not None
    assert "Stop hook" in diag.message or "audit-turn" in diag.message
    # Surface the session count to motivate the warning.
    assert diag.details["sessions"] == 2
    assert diag.details["turn_audited_events"] == 0


def test_audit_turn_cadence_single_session_does_not_warn(tmp_path: Path) -> None:
    """Round-3 fix: exactly one session in the 7-day window must NOT
    fire the warning. The prior heuristic (`n_sessions > 0`) false-
    positived for weekly-or-less Claude Code users — they had one
    session in any 7-day window and the next session hadn't happened
    yet, so the hook hadn't had a Stop trigger to fire on. Reporting
    "broken hook" in that case is wrong; the right answer is "not
    enough cadence to tell". Two distinct sessions (one of which
    completed without producing a turn_audited row) is the real
    signal."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_event(tmp_path, "search", ts=now_iso, session="lonely")
    _write_event(tmp_path, "write", ts=now_iso, session="lonely")
    diag = _check_audit_turn_cadence(tmp_path)
    assert diag.status == "ok"
    assert diag.details["sessions"] == 1
    assert diag.details["turn_audited_events"] == 0
    # The message should explain why we're not warning yet, not just
    # silently report ok.
    assert "1 session" in diag.message or "one session" in diag.message.lower()


def test_audit_turn_cadence_census_excludes_admin_event_kinds(
    tmp_path: Path,
) -> None:
    """`doctor --fix` records its `doctor_fix` audit rows under a fresh
    throwaway session id, outside any client session — a "session" that
    can never produce `turn_audited`. The census must skip admin/CLI
    kinds entirely: counting them lets a 1-real-session store (the "not
    enough cadence data" ok shape) trip the ≥2-session floor purely
    because doctor ran, corrupting the denominator the heuristic rests
    on."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_event(tmp_path, "search", ts=now_iso, session="the-real-one")
    _write_event(tmp_path, "doctor_fix", ts=now_iso, session="cli-run")
    diag = _check_audit_turn_cadence(tmp_path)
    assert diag.status == "ok"
    assert diag.details["sessions"] == 1
    assert diag.details["total_events"] == 1


def test_audit_turn_cadence_only_old_events_skips_warn(tmp_path: Path) -> None:
    """Events outside the 7-day window don't count — old activity from
    last month shouldn't trigger a warning today."""
    from datetime import datetime, timedelta, timezone

    old = (
        (datetime.now(timezone.utc) - timedelta(days=30))
        .isoformat()
        .replace("+00:00", "Z")
    )
    _write_event(tmp_path, "search", ts=old, session="s1")
    _write_event(tmp_path, "write", ts=old, session="s1")
    diag = _check_audit_turn_cadence(tmp_path)
    # No events in window -> ok (nothing to check).
    assert diag.status == "ok"
    assert diag.details["total_events"] == 0


# ---------------------------------------------------------------------------
# embeddings_extra
# ---------------------------------------------------------------------------


def test_embeddings_extra_skipped_when_disabled(tmp_path: Path) -> None:
    cfg = _config_for(tmp_path, semantic_dedup=False)
    diag = _check_embeddings_extra(cfg)
    assert diag.status == "ok"
    assert "disabled" in diag.message


def test_embeddings_extra_fails_when_enabled_but_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config_for(tmp_path, semantic_dedup=True)
    # Force the import to fail regardless of whether the extra is
    # actually installed in the test environment.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sentence_transformers":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    diag = _check_embeddings_extra(cfg)
    assert diag.status == "fail"
    assert "embeddings" in (diag.fix_hint or "")


# ---------------------------------------------------------------------------
# mcp_client_configs
# ---------------------------------------------------------------------------


def _tmp_clients(tmp_path: Path) -> dict[str, Any]:
    """Override KNOWN_CLIENTS so the doctor scans tmp_path-backed configs
    rather than the user's real ones."""
    return {
        "fakeclient": lambda: ClientPaths(
            name="fakeclient",
            description="Fake test client",
            paths=(tmp_path / "fake_config.json",),
        ),
    }


def test_mcp_client_configs_warn_when_no_entries_anywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr(
        "bettermemory.doctor.find_binary", lambda: "/usr/local/bin/bettermemory"
    )
    diag = _check_mcp_client_configs()
    assert diag.status == "warn"
    assert "no client" in diag.message.lower() or "no mcp" in diag.message.lower()


def test_mcp_client_configs_ok_when_path_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a registered command path exists on disk and matches what
    find_binary() would resolve to, doctor reports ok."""
    # Tmp binary must be named something containing "bettermemory" —
    # the check filters on that substring to avoid flagging unrelated
    # MCP servers in the same config.
    real_binary = tmp_path / "bettermemory"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")

    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"memory": {"command": str(real_binary), "args": []}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(real_binary))

    diag = _check_mcp_client_configs()
    assert diag.status == "ok"
    assert "1 client config(s)" in diag.message


def test_mcp_client_configs_ok_for_uvx_runner_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `uvx bettermemory` runner entry (the plugin .mcp.json shape) is a
    valid install — uvx resolves the binary dynamically. The old prefilter
    required "bettermemory" to be IN the command string, but the command is
    "uvx", so doctor missed the entry entirely and reported a healthy
    install as absent. It must now be recognized and NOT flagged."""
    real_binary = tmp_path / "bettermemory"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"memory": {"command": "uvx", "args": ["bettermemory"]}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(real_binary))
    diag = _check_mcp_client_configs()
    assert diag.status == "ok"
    assert "1 client config(s)" in diag.message


def test_mcp_client_configs_warns_on_stale_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The command must contain the "bettermemory" substring to pass the
    # entry filter — the old "/nonexistent/old/bm" fixture silently fell
    # through to the no-references branch and tested the wrong warn. It
    # must also be tmp_path-based: a bare "/..." string is NOT absolute
    # on Windows (no drive letter), so the missing-binary branch would
    # never fire there.
    missing = tmp_path / "gone" / "bettermemory"
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {
                        "command": str(missing),
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: "/new/bm")
    diag = _check_mcp_client_configs()
    assert diag.status == "warn"
    assert diag.fix_hint is not None
    assert "init" in (diag.fix_hint or "")


def test_mcp_client_configs_skips_files_without_betterentries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client config with mcpServers but no bettermemory entry should
    be ignored (not flagged as stale)."""
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps({"mcpServers": {"filesystem": {"command": "fs-mcp", "args": []}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: "/x/bm")
    diag = _check_mcp_client_configs()
    # No bettermemory entries found -> "no client config references
    # bettermemory" warn (same as no-files case).
    assert diag.status == "warn"


def _two_installs(tmp_path: Path) -> tuple[Path, Path]:
    """Two distinct on-disk binaries (different install locations) that
    both pass the "bettermemory" substring filter and do NOT resolve to
    the same inode — the same-string and symlink fast paths must miss so
    the version probe is what decides."""
    configured = tmp_path / "venvbin" / "bettermemory"
    resolved = tmp_path / "uvbin" / "bettermemory"
    for p in (configured, resolved):
        p.parent.mkdir()
        p.write_text("#!/bin/sh\n", encoding="utf-8")
    return configured, resolved


def test_mcp_client_configs_ok_when_different_install_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config pointing at a different install than PATH resolves is NOT
    stale when both binaries report the same version — deliberate
    dev-venv/uv-tool topologies must not warn forever."""
    configured, resolved = _two_installs(tmp_path)
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"memory": {"command": str(configured), "args": []}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(resolved))
    monkeypatch.setattr(
        "bettermemory.doctor._binary_dist_version", lambda _binary: "3.13.0"
    )

    diag = _check_mcp_client_configs()
    assert diag.status == "ok"
    assert "same version" in diag.message
    assert "fakeclient" in diag.message
    assert diag.details["findings"][0]["same_version"] == "3.13.0"


def test_mcp_client_configs_warns_on_version_mismatch_and_names_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different install AND different version is real staleness: the warn
    must name the client, both versions, and the fix_hint must carry the
    concrete `init --client <name>` command, not a placeholder."""
    configured, resolved = _two_installs(tmp_path)
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"memory": {"command": str(configured), "args": []}}}
        ),
        encoding="utf-8",
    )
    versions = {str(configured): "3.12.0", str(resolved): "3.13.0"}
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(resolved))
    monkeypatch.setattr(
        "bettermemory.doctor._binary_dist_version",
        lambda binary: versions.get(binary),
    )

    diag = _check_mcp_client_configs()
    assert diag.status == "warn"
    assert "fakeclient" in diag.message
    assert "3.12.0" in diag.message and "3.13.0" in diag.message
    assert "init --client fakeclient" in (diag.fix_hint or "")
    mismatch = diag.details["findings"][0]["version_mismatch"]
    assert mismatch == {"configured": "3.12.0", "resolved": "3.13.0"}


def test_mcp_client_configs_missing_binary_names_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing-on-disk warn names the client and config path directly
    in the human-readable message (previously only in --json details).
    tmp_path-based command so the path is absolute on Windows too."""
    missing = tmp_path / "gone" / "bettermemory"
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {
                        "command": str(missing),
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: "/new/bm")

    diag = _check_mcp_client_configs()
    assert diag.status == "warn"
    assert "fakeclient" in diag.message
    assert str(missing) in diag.message
    assert "init --client fakeclient" in (diag.fix_hint or "")


def test_binary_dist_version_parses_trailing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Proc:
        returncode = 0
        stdout = "bettermemory 9.9.9\n"
        stderr = ""

    monkeypatch.setattr("bettermemory.doctor.subprocess.run", lambda *_a, **_k: _Proc())
    assert _binary_dist_version("/any/bettermemory") == "9.9.9"


def test_binary_dist_version_none_on_failure_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real OSError path: the binary does not exist.
    assert _binary_dist_version(str(tmp_path / "missing" / "bm")) is None

    # Non-zero exit.
    class _Failed:
        returncode = 1
        stdout = "bettermemory 9.9.9\n"
        stderr = ""

    monkeypatch.setattr(
        "bettermemory.doctor.subprocess.run", lambda *_a, **_k: _Failed()
    )
    assert _binary_dist_version("/any/bettermemory") is None

    # Empty output.
    class _Silent:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "bettermemory.doctor.subprocess.run", lambda *_a, **_k: _Silent()
    )
    assert _binary_dist_version("/any/bettermemory") is None


# ---------------------------------------------------------------------------
# distinfo_metadata
# ---------------------------------------------------------------------------


def _make_distinfo(parent: Path, name: str, *, files: dict[str, str]) -> Path:
    """Build a fake `*.dist-info/` directory under `parent` with the
    given top-level files. Returns the dist-info path."""
    d = parent / name
    d.mkdir()
    for fname, body in files.items():
        (d / fname).write_text(body, encoding="utf-8")
    return d


def test_distinfo_metadata_ok_when_no_site_packages() -> None:
    """An empty discovery list (e.g. embedded interpreter) is treated as
    "nothing to check" — the check must not warn just because we
    couldn't find a site-packages dir."""
    diag = _check_distinfo_metadata(site_packages=[])
    assert diag.status == "ok"
    assert "skipping" in diag.message.lower()


def test_distinfo_metadata_ok_when_all_have_canonical(tmp_path: Path) -> None:
    """Healthy dist-info dirs (each with a `METADATA` file) pass
    silently — no warning even if other files are present."""
    _make_distinfo(tmp_path, "pkg-1.0.dist-info", files={"METADATA": "Name: pkg\n"})
    _make_distinfo(
        tmp_path,
        "other-2.0.dist-info",
        files={"METADATA": "Name: other\n", "WHEEL": "ok\n"},
    )
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "ok"
    assert diag.details["scanned"] == 2


def test_distinfo_metadata_warns_on_missing_canonical(tmp_path: Path) -> None:
    """A dist-info dir with only `METADATA 2` (the iCloud-conflict
    rename) and no canonical `METADATA` is the failure mode this
    check exists to catch — warn, name the dir, hint at the iCloud
    cause, and suggest re-install."""
    _make_distinfo(
        tmp_path, "healthy-1.0.dist-info", files={"METADATA": "Name: healthy\n"}
    )
    broken = _make_distinfo(
        tmp_path,
        "broken-2.0.dist-info",
        files={"METADATA 2": "Name: broken\n", "WHEEL": "ok\n"},
    )
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "warn"
    # The healthy dir must NOT be named in the warning.
    assert "healthy-1.0.dist-info" not in diag.message
    # The broken one MUST be named.
    assert "broken-2.0.dist-info" in diag.message
    # The iCloud-cause hint fires because `METADATA 2` matched the
    # duplicate regex.
    assert "iCloud" in diag.message
    # Fix hint should point at re-install (the safer recovery).
    assert diag.fix_hint is not None
    assert (
        "reinstall" in (diag.fix_hint or "").lower()
        or "install" in (diag.fix_hint or "").lower()
    )
    # `details.broken` should list the broken dir and its duplicates.
    assert len(diag.details["broken"]) == 1
    entry = diag.details["broken"][0]
    assert entry["dist_info"] == str(broken)
    assert "METADATA 2" in entry["duplicates"]


def test_distinfo_metadata_warns_without_icloud_hint_when_no_duplicate(
    tmp_path: Path,
) -> None:
    """A dist-info missing METADATA but with no iCloud-style duplicate
    (e.g. partial uninstall) still warns, but skips the iCloud-cause
    sentence — we only claim the cause when the evidence is there."""
    _make_distinfo(tmp_path, "partial-3.0.dist-info", files={"WHEEL": "ok\n"})
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "warn"
    assert "partial-3.0.dist-info" in diag.message
    assert "iCloud" not in diag.message  # no duplicate -> no cause hint
    assert diag.details["broken"][0]["duplicates"] == []


def test_distinfo_metadata_warns_on_empty_canonical_file(tmp_path: Path) -> None:
    """A zero-byte `METADATA` is the same failure mode as a missing
    one: `importlib.metadata.version()` returns None, which trips the
    downstream `-32000` MCP crash. The check must treat empty as broken
    even though the file technically exists."""
    _make_distinfo(
        tmp_path, "healthy-1.0.dist-info", files={"METADATA": "Name: healthy\n"}
    )
    # Build the broken dir by hand so we can write an empty METADATA
    # without `_make_distinfo` having to special-case empty strings.
    broken = tmp_path / "empty-2.0.dist-info"
    broken.mkdir()
    (broken / "METADATA").write_bytes(b"")  # zero-byte file
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "warn"
    assert "healthy-1.0.dist-info" not in diag.message
    assert "empty-2.0.dist-info" in diag.message
    # Empty file has no iCloud-style duplicate sibling.
    assert "iCloud" not in diag.message
    assert len(diag.details["broken"]) == 1
    assert diag.details["broken"][0]["dist_info"] == str(broken)


def test_distinfo_metadata_warns_on_whitespace_only_canonical(
    tmp_path: Path,
) -> None:
    """A `METADATA` containing only whitespace (e.g. `"   \\n   \\n"`
    from a manual edit or partial sync) passes both `is_file()` and
    `stat().st_size > 0`, but `importlib.metadata.version()` still
    returns None because the canonical `Name:` header is absent —
    the same `-32000` crash downstream. The check must require the
    `Name:` header, not just a non-zero file size."""
    _make_distinfo(
        tmp_path, "healthy-1.0.dist-info", files={"METADATA": "Name: healthy\n"}
    )
    # Build the broken dir by hand so we can write whitespace-only
    # bytes without `_make_distinfo` re-encoding through write_text.
    broken = tmp_path / "whitespace-2.0.dist-info"
    broken.mkdir()
    (broken / "METADATA").write_bytes(b"   \n   \n")  # non-zero, no Name:
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "warn"
    assert "healthy-1.0.dist-info" not in diag.message
    assert "whitespace-2.0.dist-info" in diag.message
    # Whitespace-only file has no iCloud-style duplicate sibling.
    assert "iCloud" not in diag.message
    assert len(diag.details["broken"]) == 1
    assert diag.details["broken"][0]["dist_info"] == str(broken)


def test_distinfo_metadata_ok_when_metadata_has_leading_metadata_version_header(
    tmp_path: Path,
) -> None:
    """Real-world wheels emit `Metadata-Version: <ver>` as the FIRST line
    of `METADATA` per Core Metadata convention, with `Name:` on a later
    line. The header check must find `Name:` anywhere in the read window,
    not only at byte 0 — otherwise `re.match` (anchored at position 0
    regardless of `(?m)`) returns None against every real wheel and the
    check flags all packages as broken. Pin a realistic multi-line header
    so the `re.match` vs `re.search` regression can't re-ship."""
    realistic = b"Metadata-Version: 2.4\nName: pkg\nVersion: 1.0\n"
    d = tmp_path / "pkg-1.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_bytes(realistic)
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "ok"
    assert diag.details["scanned"] == 1


def test_distinfo_metadata_ok_when_name_header_past_first_chunk(
    tmp_path: Path,
) -> None:
    """PEP 643 / Core Metadata doesn't fix header order. Wheels emitted
    by some packaging tools push `Name:` past the first few hundred
    bytes by leading with `Metadata-Version:`, long `License-Expression:`
    SPDX strings, multiple `Project-URL:` rows, or in-header
    `Description:` text. The header check used to read a fixed 256-byte
    window; this test pins a METADATA where `Name:` sits well past byte
    256 (still inside the RFC-822 header section) and asserts the check
    finds it. Under the old 256-byte cap this would false-positive
    (warn that a valid wheel is broken)."""
    # Build a realistic header section where `Name:` is pushed past
    # byte 256 by long, real-world-shaped fields. Each `Project-URL:`
    # line is ~80 bytes; six of them plus the `License-Expression:`
    # SPDX expression lands `Name:` somewhere around byte 600.
    header = (
        b"Metadata-Version: 2.4\n"
        b"License-Expression: (Apache-2.0 OR MIT) AND BSD-3-Clause\n"
        b"Project-URL: Homepage, https://example.com/some/long/url/path/to/project\n"
        b"Project-URL: Documentation, https://example.com/docs/very/long/path/here\n"
        b"Project-URL: Repository, https://github.com/example/some-long-repo-name\n"
        b"Project-URL: Issues, https://github.com/example/some-long-repo-name/issues\n"
        b"Project-URL: Changelog, https://example.com/some/long/url/to/changelog\n"
        b"Project-URL: Funding, https://example.com/some/long/funding/url/here\n"
        b"Name: pkg\n"
        b"Version: 1.0\n"
        b"\n"
        b"Body text after blank line - should not be scanned for headers.\n"
    )
    # Sanity: confirm the fixture actually exercises the bug class.
    name_offset = header.index(b"\nName: ") + 1
    assert name_offset > 256, (
        f"fixture must place Name: past byte 256 to pin the bug class; "
        f"got offset {name_offset}"
    )
    d = tmp_path / "pkg-1.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_bytes(header)
    diag = _check_distinfo_metadata(site_packages=[tmp_path])
    assert diag.status == "ok"
    assert diag.details["scanned"] == 1


def test_distinfo_metadata_scans_user_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pip install --user` lands packages in `site.getusersitepackages()`.
    When `ENABLE_USER_SITE` is true, the discoverer must include that
    directory so user-site dist-info dirs aren't silently skipped."""
    import site as _site

    user_site = tmp_path / "user-site"
    user_site.mkdir()
    _make_distinfo(
        user_site,
        "broken-1.0.dist-info",
        files={"METADATA 2": "Name: broken\n"},
    )
    monkeypatch.setattr(_site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(_site, "getusersitepackages", lambda: str(user_site))
    # Neutralize the other discoverers so the test exercises user-site
    # in isolation — otherwise the host's real site-packages would also
    # be scanned and could shift the assertion targets.
    monkeypatch.setattr(
        "bettermemory.doctor.sysconfig.get_paths",
        lambda: {"purelib": "", "platlib": ""},
    )
    monkeypatch.setattr(_site, "getsitepackages", lambda: [])

    discovered = _discover_site_packages()
    resolved_user_site = user_site.resolve()
    assert any(p.resolve() == resolved_user_site for p in discovered)

    diag = _check_distinfo_metadata()
    assert diag.status == "warn"
    assert "broken-1.0.dist-info" in diag.message


def test_distinfo_metadata_skips_user_site_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `ENABLE_USER_SITE` is False (the modern venv default), the
    discoverer must not call `getusersitepackages()` and must not scan
    that directory — a broken dist-info there should go unreported."""
    import site as _site

    user_site = tmp_path / "user-site"
    user_site.mkdir()
    _make_distinfo(
        user_site,
        "broken-1.0.dist-info",
        files={"METADATA 2": "Name: broken\n"},
    )
    monkeypatch.setattr(_site, "ENABLE_USER_SITE", False)

    called = {"n": 0}

    def _should_not_be_called() -> str:
        called["n"] += 1
        return str(user_site)

    monkeypatch.setattr(_site, "getusersitepackages", _should_not_be_called)
    monkeypatch.setattr(
        "bettermemory.doctor.sysconfig.get_paths",
        lambda: {"purelib": "", "platlib": ""},
    )
    monkeypatch.setattr(_site, "getsitepackages", lambda: [])

    discovered = _discover_site_packages()
    assert called["n"] == 0
    resolved_user_site = user_site.resolve()
    assert all(p.resolve() != resolved_user_site for p in discovered)


# ---------------------------------------------------------------------------
# Integration: run_diagnostics + rendering
# ---------------------------------------------------------------------------


def test_run_diagnostics_returns_report(tmp_path: Path) -> None:
    """Smoke test: run_diagnostics doesn't crash on a real environment.
    We don't pin the overall verdict because it depends on the host's
    actual install state (which is the whole point of the tool)."""
    report = run_diagnostics()
    assert isinstance(report, DoctorReport)
    assert len(report.checks) >= 4  # at least python, binary, config, storage
    assert report.overall in {"ok", "warn", "fail"}


def test_render_text_includes_all_check_names() -> None:
    report = DoctorReport(
        checks=[
            Diagnosis(name="alpha", status="ok", message="fine"),
            Diagnosis(name="beta", status="warn", message="iffy", fix_hint="do X"),
        ]
    )
    out = render_text(report)
    assert "alpha" in out
    assert "beta" in out
    assert "do X" in out  # fix hint surfaces when status != ok
    assert "passed with warnings" in out


def test_render_json_round_trip() -> None:
    report = run_diagnostics()
    parsed = json.loads(render_json(report))
    assert "overall" in parsed
    assert "checks" in parsed
    assert isinstance(parsed["checks"], list)


def test_cli_doctor_returns_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cli_doctor returns 0/1/2 depending on the worst diagnosis seen.
    Force a known state by stubbing run_diagnostics."""
    fake_report = DoctorReport(checks=[Diagnosis(name="x", status="ok", message="")])
    monkeypatch.setattr("bettermemory.doctor.run_diagnostics", lambda: fake_report)
    code = cli_doctor(json_out=False)
    assert code == 0

    fake_report = DoctorReport(checks=[Diagnosis(name="x", status="warn", message="")])
    monkeypatch.setattr("bettermemory.doctor.run_diagnostics", lambda: fake_report)
    capsys.readouterr()
    code = cli_doctor(json_out=False)
    assert code == 1

    fake_report = DoctorReport(checks=[Diagnosis(name="x", status="fail", message="")])
    monkeypatch.setattr("bettermemory.doctor.run_diagnostics", lambda: fake_report)
    capsys.readouterr()
    code = cli_doctor(json_out=False)
    assert code == 2


# ---------------------------------------------------------------------------
# Literal-keyed-dict parity for `_STATUS_GLYPH` (and the sibling inline
# `overall_label`) at `doctor.py:_STATUS_GLYPH` / `render_text`.
#
# `_STATUS_GLYPH: dict[CheckStatus, str]` and the inline `overall_label`
# dict at `render_text` are both keyed off the closed `CheckStatus` Literal
# (`doctor.py:CheckStatus = Literal["ok", "warn", "fail"]`). Both are
# consumed by direct subscript (`_STATUS_GLYPH[overall]`,
# `_STATUS_GLYPH[check.status]`, `overall_label[overall]`) — adding a
# fourth `CheckStatus` literal without updating both dicts would crash
# `bettermemory doctor`'s text renderer with `KeyError` on the first
# diagnosis that surfaces the new status.
#
# Hazard tier: low. Cosmetic rendering crash only — the underlying
# `run_diagnostics` and `render_json` paths don't touch either dict, so
# data integrity is intact. Still a closed-protocol pin worth taking now
# that we've swept the rest of the membership-guard class. Mirrors the
# Literal-derived guards in `test_ingest.py::test_actions_tuple_matches_
# action_literal` (basic shape) and `test_server.py::test_write_gates_
# match_expected_types_in_order` (ordered shape).
#
# `_EXPECTED_CHECK_STATUSES` is hardcoded alphabetised and NOT derived
# from `typing.get_args(CheckStatus)` — derivation would silently shrink
# the expected list when the source shrinks, defeating the deletion
# guard. Same shape as `_EXPECTED_INDEX_FILENAMES` (bde7602) and
# `_EXPECTED_USE_OUTCOMES` (db81630).
#
# Negative-control: temporarily adding `"info"` to the `CheckStatus`
# Literal in `doctor.py` fails `test_status_glyph_keys_match_check_
# status_literal` (set inequality: Literal has extra `"info"` that
# `_STATUS_GLYPH` doesn't). Revert restores green.
# ---------------------------------------------------------------------------


_EXPECTED_CHECK_STATUSES: tuple[str, ...] = ("fail", "ok", "warn")


def test_expected_check_statuses_match_literal() -> None:
    """Pin the hardcoded `_EXPECTED_CHECK_STATUSES` tuple against the
    canonical `CheckStatus` Literal — the indirection through this
    tuple is what makes the two dict-parity assertions below catch
    BOTH additions and deletions (a `get_args(CheckStatus)`-derived
    expectation would shrink with deletions and miss the deletion
    case)."""
    assert set(_EXPECTED_CHECK_STATUSES) == set(typing.get_args(CheckStatus))


def test_status_glyph_keys_match_check_status_literal() -> None:
    """`_STATUS_GLYPH` is direct-indexed in `render_text`
    (`_STATUS_GLYPH[overall]`, `_STATUS_GLYPH[check.status]`); a
    missing key crashes the doctor renderer with `KeyError`. Pin the
    keys against the hardcoded `_EXPECTED_CHECK_STATUSES` so a new
    `CheckStatus` literal trips this guard before it ships."""
    assert set(_STATUS_GLYPH.keys()) == set(_EXPECTED_CHECK_STATUSES), (
        "`_STATUS_GLYPH` keys drifted from `CheckStatus`; see "
        "doctor.py:_STATUS_GLYPH and the inline `overall_label` dict "
        "at render_text — both must mirror every CheckStatus literal."
    )


def test_render_text_overall_label_covers_every_check_status() -> None:
    """The sibling inline `overall_label` dict at `render_text` is
    keyed off CheckStatus the same way `_STATUS_GLYPH` is — same
    KeyError hazard. Drive `render_text` once per CheckStatus literal
    so a missing key here crashes the test instead of crashing
    `bettermemory doctor` in user-facing output."""
    for status in _EXPECTED_CHECK_STATUSES:
        # Use a single-check report so `report.overall` equals `status`.
        report = DoctorReport(
            checks=[Diagnosis(name="probe", status=status, message="")]  # type: ignore[arg-type]
        )
        out = render_text(report)
        # The header line carries the overall_label string — a
        # KeyError on the inline dict would have raised by now.
        assert "bettermemory doctor" in out


def test_exit_code_by_status_keys_match_check_status_literal() -> None:
    """`_EXIT_CODE_BY_STATUS` is direct-indexed in `cli_doctor`
    (`_EXIT_CODE_BY_STATUS[report.overall]`); a missing key crashes
    the `bettermemory doctor` CLI with `KeyError` rather than
    returning a clean exit code. Pin the keys against the hardcoded
    `_EXPECTED_CHECK_STATUSES` so a new `CheckStatus` literal trips
    this guard before it ships — exit codes are user-visible in
    shell pipelines, so the failure mode is worth pinning alongside
    the `_STATUS_GLYPH` / `overall_label` renderer guards."""
    assert set(_EXIT_CODE_BY_STATUS.keys()) == set(_EXPECTED_CHECK_STATUSES), (
        "`_EXIT_CODE_BY_STATUS` keys drifted from `CheckStatus`; see "
        "doctor.py:_EXIT_CODE_BY_STATUS and cli_doctor — the mapping "
        "must mirror every CheckStatus literal or the CLI crashes."
    )


# ---------------------------------------------------------------------------
# Cross-module parity: the event-log path probed by
# `_check_event_log_writable` (`doctor.py`) MUST resolve to the same
# filename the `Recorder` actually writes — `events.EVENT_LOG_FILENAME`.
# A rename of the canonical constant would, prior to this commit, have
# updated the writer but left the doctor's probe path pointing at a
# stale literal — silently passing the writability check against a file
# that the runtime never creates. Closes the doctor side of Class 6.
# ---------------------------------------------------------------------------


def test_check_event_log_uses_canonical_event_log_filename(
    tmp_path: Path,
) -> None:
    """Pin `doctor.py:_check_event_log_writable` to the canonical
    `events.EVENT_LOG_FILENAME`. Drop a file at `<dir>/EVENT_LOG_FILENAME`
    and confirm the probe finds it (i.e. takes the existing-file branch,
    not the "not yet created" branch a hardcoded literal would fall
    through to after a rename of the constant)."""
    from bettermemory.events import EVENT_LOG_FILENAME

    log_path = tmp_path / EVENT_LOG_FILENAME
    log_path.write_text('{"kind": "test"}\n', encoding="utf-8")
    diag = _check_event_log_writable(tmp_path)
    # Doctor saw the file (status is `ok` or `warn`, not the
    # "not yet created" message that fires when the path is missing).
    assert "not yet created" not in diag.message, (
        "doctor's log_path constructed a different filename than "
        "events.EVENT_LOG_FILENAME — see doctor.py:_check_event_log_writable"
    )


def test_mcp_client_configs_ok_for_pinned_uvx_runner_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version-pinned `uvx bettermemory@latest` runner entry is a valid
    install — the exact-arg gate matched only the bare name, so doctor fell
    through the substring prefilter and reported a healthy pinned install as
    absent (the false-negative the 3.15.0 changelog claimed eliminated). The
    shared recognizer must accept it."""
    real_binary = tmp_path / "bettermemory"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {"command": "uvx", "args": ["bettermemory@latest"]}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))
    monkeypatch.setattr("bettermemory.doctor.find_binary", lambda: str(real_binary))
    diag = _check_mcp_client_configs()
    assert diag.status == "ok"
    assert "1 client config(s)" in diag.message


def test_doctor_flags_stale_config_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 0-byte `<config>.lock` REGULAR FILE bettermemory 3.15.0 left wedges
    Claude Code's own mkdir-style config lock; doctor must surface it with the
    heal path. A DIRECTORY at that name is the client's own lock — not
    flagged."""
    from bettermemory.doctor import _check_stale_config_lockfiles

    target = tmp_path / "fake_config.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("bettermemory.doctor.KNOWN_CLIENTS", _tmp_clients(tmp_path))

    stale = tmp_path / "fake_config.json.lock"
    stale.touch()
    diag = _check_stale_config_lockfiles()
    assert diag.status == "warn"
    assert str(stale) in diag.message
    assert diag.fix_hint is not None and "init" in diag.fix_hint

    stale.unlink()
    stale.mkdir()  # the client's own directory lock — not ours to judge
    diag = _check_stale_config_lockfiles()
    assert diag.status == "ok"


# ---------------------------------------------------------------------------
# auto_memory_stranded
# ---------------------------------------------------------------------------


def _auto_memory_root_for(home: Path, cwd: Path) -> Path:
    """Mirror `ingest.discover_default_source_root`'s sanitiser so the
    fixture lands where discovery will look. Claude Code's real scheme
    folds EVERY non-alphanumeric char to `-`
    (`path.replace(/[^a-zA-Z0-9]/g, "-")`)."""
    resolved = cwd.resolve().as_posix().lstrip("/")
    sanitized = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
    return home / ".claude" / "projects" / sanitized / "memory"


def _write_auto_memory_file(root: Path, name: str, *, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: summary of {name}\n"
        "metadata:\n"
        "  type: fact\n"
        "---\n\n"
        f"{body}\n"
    )
    return path


def test_auto_memory_stranded_ok_when_no_auto_memory_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    storage = tmp_path / "store"
    storage.mkdir()
    diag = _check_auto_memory_stranded(storage, cwd=tmp_path / "proj")
    assert diag.status == "ok"
    assert "No Claude Code auto-memory" in diag.message
    assert diag.details["source_root"] is None


def test_auto_memory_stranded_warns_on_uningested_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    source = _auto_memory_root_for(fake_home, cwd)
    _write_auto_memory_file(source, "alpha", body="the alpha fact body")
    _write_auto_memory_file(source, "beta", body="a wholly different beta body")
    storage = tmp_path / "store"
    storage.mkdir()

    diag = _check_auto_memory_stranded(storage, cwd=cwd)
    assert diag.status == "warn"
    assert "2 Claude Code auto-memory files" in diag.message
    assert "invisible to bettermemory retrieval" in diag.message
    assert diag.fix_hint is not None and "bettermemory ingest" in diag.fix_hint
    assert diag.details["summary"]["write"] == 2


def test_auto_memory_stranded_goes_quiet_after_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest never mutates source files, so the check must key on the
    dedup classification, not the file count — a completed import
    flips the verdict to ok with the sources still on disk."""
    from bettermemory.ingest import apply_ingest_plan, compute_ingest_plan
    from bettermemory.store import Store

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    source = _auto_memory_root_for(fake_home, cwd)
    _write_auto_memory_file(source, "alpha", body="the alpha fact body")
    storage = tmp_path / "store"
    storage.mkdir()

    store = Store(storage)
    plan = compute_ingest_plan(
        source,
        existing_memories=store.load_all(),
        existing_tombstones=store.load_tombstones(),
    )
    apply_ingest_plan(plan, store)
    assert source.exists() and any(source.iterdir())  # sources untouched

    diag = _check_auto_memory_stranded(storage, cwd=cwd)
    assert diag.status == "ok"
    assert "nothing un-ingested" in diag.message
    assert diag.details["summary"]["write"] == 0


def test_auto_memory_stranded_stays_quiet_after_memory_curated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest, then substantively rewrite the resulting memory (routine
    curate-loop work), leaving the SOURCE file byte-for-byte untouched.

    The imported memory's body has now drifted far below the dedup
    threshold, so `compute_ingest_plan` re-classifies the unchanged source
    as a fresh `write`. The pre-fix check keyed off that classification
    (`plan.summary["write"]`) and false-alarmed on every run forever, with
    a fix_hint that would re-import the stale pre-edit body as a second
    near-duplicate. The provenance watermark ingest now persists keys the
    verdict on the source's content hash instead: unchanged bytes ==
    ingested, no matter how far the memory drifted. The check must stay
    `ok` and must NOT recommend re-ingesting."""
    from bettermemory.ingest import (
        INGEST_WATERMARK_FILENAME,
        apply_ingest_plan,
        compute_ingest_plan,
    )
    from bettermemory.store import Store

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    source = _auto_memory_root_for(fake_home, cwd)
    source_file = _write_auto_memory_file(
        source,
        "alpha",
        body="the alpha fact body about widget provisioning and tooling defaults",
    )
    source_bytes_before = source_file.read_bytes()
    storage = tmp_path / "store"
    storage.mkdir()

    store = Store(storage)
    plan = compute_ingest_plan(
        source,
        existing_memories=store.load_all(),
        existing_tombstones=store.load_tombstones(),
    )
    apply_ingest_plan(plan, store)
    # The watermark sidecar is the mechanism under test — it must exist.
    assert (storage / INGEST_WATERMARK_FILENAME).is_file()

    # Substantive curated rewrite of the imported memory: the new body
    # shares almost no tokens with the source, so body-Jaccard drops well
    # under the duplicate threshold and the source re-classifies as write.
    [mem] = store.load_all()
    curated = mem.model_copy(
        update={
            "body": (
                "completely unrelated curated rewrite concerning quantum "
                "teacup logistics and orbital ballet choreography\n"
            )
        }
    )
    store.update(curated)

    # Premise pin: the source is untouched but the plan now wants to write
    # it — exactly the state that made the pre-fix check false-alarm.
    assert source_file.read_bytes() == source_bytes_before
    drift_plan = compute_ingest_plan(
        source,
        existing_memories=store.load_all(),
        existing_tombstones=store.load_tombstones(),
    )
    assert drift_plan.summary["write"] == 1

    diag = _check_auto_memory_stranded(storage, cwd=cwd)
    assert diag.status == "ok", f"false stranded alarm after curation: {diag.message}"
    assert diag.details["stranded"] == 0
    # The harmful fix_hint (re-run ingest -> resurrects the stale body)
    # must not appear on an ok verdict.
    assert diag.fix_hint is None


def test_auto_memory_stranded_ignores_index_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEMORY.md / INDEX.md / README.md are navigation artefacts, not
    stored claims — a dir holding only those must not warn."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    source = _auto_memory_root_for(fake_home, cwd)
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("- [x](x.md) — index line\n")
    storage = tmp_path / "store"
    storage.mkdir()

    diag = _check_auto_memory_stranded(storage, cwd=cwd)
    assert diag.status == "ok"
    assert diag.details["summary"]["write"] == 0


def test_auto_memory_stranded_does_not_create_missing_storage_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only probe must not mkdir the store (mirrors the
    parse-health guard: Store.__post_init__ creates directories)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    source = _auto_memory_root_for(fake_home, cwd)
    _write_auto_memory_file(source, "alpha", body="the alpha fact body")
    missing = tmp_path / "store-not-created"

    diag = _check_auto_memory_stranded(missing, cwd=cwd)
    assert diag.status == "ok"
    assert not missing.exists()


# ---------------------------------------------------------------------------
# sync_tracked_ignored
# ---------------------------------------------------------------------------


_needs_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH — sync-repo doctor checks need git",
)

_needs_posix_store_names = pytest.mark.skipif(
    sys.platform == "win32",
    reason="store names containing ':' or '*' are illegal in Windows filenames",
)


def _git_in(cwd: Path, *args: str) -> str:
    """Ad-hoc git for fixtures — raises with stderr on failure (mirrors
    tests/test_sync.py's `_git`)."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout


def _store_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git-inited store dir with a hermetic global git config. The
    `GIT_CONFIG_GLOBAL` redirect mirrors tests/test_sync.py's
    `memory_dir` fixture — without it the identity writes below would
    land in the developer's real ~/.gitconfig. The discovery ceiling
    keeps the nested-store checks' upward walk inside the sandbox."""
    store = tmp_path / "store"
    store.mkdir()
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    _git_in(store, "init", "--initial-branch", "main")
    _git_in(store, "config", "--global", "user.email", "test@example.com")
    _git_in(store, "config", "--global", "user.name", "Test")
    return store


@_needs_git
def test_sync_tracked_ignored_fails_on_tracked_proposals_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store repo initialised BEFORE `PROPOSALS_FILENAME` joined
    `sync._GITIGNORE_LINES` committed the raw-capture queue. The
    init()-time gitignore refresh only stops FUTURE staging, so the
    file stays tracked and every `sync push` keeps shipping its
    plaintext captures to the remote. The check must FAIL with the
    concrete `git rm --cached` remediation plus the history-rewrite /
    secret-rotation note — and the prescribed local remediation must
    actually clear the check."""
    store = _store_repo(tmp_path, monkeypatch)
    # Pre-fix repo shape: the queue was committed before its ignore
    # line existed (no .gitignore at commit time).
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")
    # The upgrade path: init() refreshes .gitignore to canonical shape…
    sync.init(store)
    # …but gitignore is silent on already-tracked paths (premise pin —
    # if this ever unsticks, git learned to untrack and the check may
    # be obsolete).
    assert PROPOSALS_FILENAME in _git_in(store, "ls-files")

    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "fail"
    assert diag.name == "sync_tracked_ignored"
    assert PROPOSALS_FILENAME in diag.message
    # The path is emitted as a single-quoted `:(literal)` pathspec so
    # the copy-pasted command survives shell splitting and never feeds
    # pathspec magic / globbing (a raw join fails rc=128 on a
    # leading-`:` path and silently untracks the wrong sibling on a
    # bracketed one).
    assert f"git rm --cached ':(literal){PROPOSALS_FILENAME}'" in (diag.fix_hint or "")
    assert "git-filter-repo" in (diag.fix_hint or "")
    assert "rotate" in (diag.fix_hint or "")
    # Multi-host migration warning: pulling another host's untrack commit
    # deletes THIS host's tracked working copies (verified empirically in a
    # two-clone simulation during the 3.21.0 set-audit) — the hint must say
    # to untrack on every host BEFORE its next pull.
    assert "before its next `sync pull`" in (diag.fix_hint or "")
    assert diag.details["tracked_ignored"] == [PROPOSALS_FILENAME]
    # The rendered doctor output carries the check name and remediation.
    out = render_text(DoctorReport(checks=[diag]))
    assert "sync_tracked_ignored" in out
    assert "git rm --cached" in out

    # The prescribed local remediation clears the check (file stays on
    # disk, now untracked-and-ignored).
    _git_in(store, "rm", "--cached", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "untrack raw-capture queue")
    assert (store / PROPOSALS_FILENAME).exists()
    assert _check_sync_tracked_ignored(store).status == "ok"


@_needs_git
def test_sync_tracked_ignored_passes_when_untracked_and_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy post-fix shape: the queue exists on disk but the
    canonical .gitignore keeps it out of the index — `git add -A`
    stages nothing for it, so nothing matching `_GITIGNORE_LINES` is
    tracked and the check passes."""
    store = _store_repo(tmp_path, monkeypatch)
    sync.init(store)  # writes the canonical .gitignore
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "captured text that must stay host-local"}\n', encoding="utf-8"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(store, "add", "-A")
    _git_in(store, "commit", "-m", "memories only")
    # Premise pin: on disk but not tracked (the gitignore did its job).
    tracked = _git_in(store, "ls-files")
    assert PROPOSALS_FILENAME not in tracked
    assert "a-memory.md" in tracked

    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None


@_needs_git
def test_sidecar_pattern_matcher_agrees_with_git_check_ignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PARITY ORACLE for `_pattern_matches_tracked_path`, the one
    matcher both `_check_sync_tracked_ignored` and
    `_scan_parent_index_for_sidecars` run tracked paths through: git
    itself is the referee. A scratch repo's .gitignore holds exactly
    `sync._GITIGNORE_LINES`, and for a matrix of path shapes derived
    from every live pattern (root basename, nested basename, ignored
    directory at the root, ignored directory mid-path, and a
    cross-slash `*` span for each glob pattern) the matcher's verdict
    must agree with `git check-ignore -q` (rc 0 = ignored, 1 = not)
    cell for cell.

    The pre-fix spelling — `fnmatch` over the WHOLE path plus its
    basename — fails this matrix in both directions: `*` crossed `/`
    (`.embeddings.*.npz` claimed `.embeddings.cache/model.npz`, which
    git does NOT ignore — the destructive-advice false positive the
    sibling end-to-end test records), and a path nested under an
    ignored DIRECTORY (`hostA.tmp/deep/leaf.bin`) matched nothing — a
    silently missed leak. Per-component matching is exactly gitignore's
    rule for the positive, slash-free shape the structural guard in
    test_sync.py pins the list to; this matrix keeps that equivalence
    honest as the pattern list evolves."""
    repo = _store_repo(tmp_path, monkeypatch)
    (repo / ".gitignore").write_text(
        "\n".join(sync._GITIGNORE_LINES) + "\n", encoding="utf-8"
    )
    # Same comment/blank filter both doctor call sites apply.
    patterns = [
        line
        for line in sync._GITIGNORE_LINES
        if line and not line.lstrip().startswith("#")
    ]
    # Fixed control cells: the recorded regression shape (a tracked
    # path git legitimately keeps), plus plain and near-miss
    # non-matches.
    candidates: list[str] = [
        ".embeddings.cache/model.npz",
        ".embeddings.cache",
        "notes/2024-plan.md",
        f"sub/{PROPOSALS_FILENAME}.bak",
    ]
    for pattern in patterns:
        instance = pattern.replace("*", "hostA")
        # Self-check the materialiser: every generated instance must
        # actually match its source pattern, or the matrix quietly
        # degenerates. Extend the replacement if a glob character other
        # than `*` ever joins `sync._GITIGNORE_LINES`.
        assert fnmatch.fnmatch(instance, pattern), (
            f"matrix materialiser cannot instantiate {pattern!r}"
        )
        candidates += [
            instance,
            f"sub/dir/{instance}",
            f"{instance}/deep/leaf.bin",
            f"top/{instance}/inner.txt",
        ]
        if "*" in pattern:
            # The cross-slash `*` span. For internal-wildcard patterns
            # (`.embeddings.*.npz`) neither resulting component matches
            # — the false-positive shape; for prefix-`*` patterns
            # (`*.lock`) the second component still matches, so git
            # ignores it — either way the cell must agree with git.
            candidates.append(pattern.replace("*", "spanA/spanB"))
    verdicts: dict[str, tuple[bool, bool]] = {}
    for path in dict.fromkeys(candidates):
        probe = subprocess.run(
            ["git", "check-ignore", "-q", "--", path],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        # 0 = ignored, 1 = not ignored; anything else is a probe bug.
        assert probe.returncode in (0, 1), (path, probe.returncode, probe.stderr)
        matcher_says = any(
            _pattern_matches_tracked_path(path, pattern) for pattern in patterns
        )
        verdicts[path] = (matcher_says, probe.returncode == 0)
    # Matrix sanity: both verdicts are represented, so a degenerate
    # matrix cannot pass vacuously.
    assert any(git_says for _, git_says in verdicts.values())
    assert any(not git_says for _, git_says in verdicts.values())
    # The regression shape is a git-legitimate tracked path and the
    # matcher must leave it alone.
    assert verdicts[".embeddings.cache/model.npz"] == (False, False)
    disagreements = {
        path: {"matcher": matcher_says, "git": git_says}
        for path, (matcher_says, git_says) in verdicts.items()
        if matcher_says != git_says
    }
    assert not disagreements, (
        f"doctor's sidecar matcher diverges from git check-ignore: {disagreements}"
    )


@_needs_git
def test_sync_tracked_ignored_no_fail_on_cross_slash_wildcard_lookalike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.embeddings.cache/model.npz` fnmatches `.embeddings.*.npz` when
    `*` is allowed to cross `/` — but git does NOT ignore it (no single
    path component matches), so the store legitimately tracks it. The
    pre-fix whole-path match FAILed this store and handed its owner the
    `git rm --cached` + history-rewrite + secret-rotation walkthrough
    for a file that is supposed to be in the repo — the destructive
    false positive the per-component matcher exists to prevent. The
    check must stay ok."""
    store = _store_repo(tmp_path, monkeypatch)
    sync.init(store)  # writes the canonical .gitignore
    target = store / ".embeddings.cache" / "model.npz"
    target.parent.mkdir()
    target.write_bytes(b"legitimately tracked artefact")
    _git_in(store, "add", "-A")
    _git_in(store, "commit", "-m", "track a wildcard lookalike")
    tracked_rel = ".embeddings.cache/model.npz"
    # Premise pins: the store really tracks the path (`git add -A`
    # staged it under the canonical .gitignore), and git itself does
    # not ignore it (check-ignore rc=1).
    assert tracked_rel in _git_in(store, "ls-files")
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "--", tracked_rel],
        cwd=store,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 1, (probe.returncode, probe.stderr)

    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None


def test_sync_tracked_ignored_ok_on_non_git_store(tmp_path: Path) -> None:
    """A store that never ran `sync init` has no repo and nothing
    syncing — the check must pass (and must not demand git ceremony;
    it also passes when git itself is absent, via `_is_repo`'s
    SyncError-to-False degradation)."""
    (tmp_path / PROPOSALS_FILENAME).write_text(
        '{"content": "local only"}\n', encoding="utf-8"
    )
    diag = _check_sync_tracked_ignored(tmp_path)
    assert diag.status == "ok"
    assert "not a git sync repo" in diag.message


def test_sync_tracked_ignored_missing_dir_does_not_create_it(tmp_path: Path) -> None:
    """Read-only probe contract shared with the sibling checks: a
    never-created storage dir is reported ok and left uncreated."""
    ghost = tmp_path / "ghost"
    diag = _check_sync_tracked_ignored(ghost)
    assert diag.status == "ok"
    assert not ghost.exists()


# ---------------------------------------------------------------------------
# store_nested_in_parent_repo
# ---------------------------------------------------------------------------


def _parent_repo_with_nested_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_name: str = "memory-store",
) -> tuple[Path, Path]:
    """A parent git repo (the dotfiles-managed-$HOME shape) with the
    store as a PLAIN SUBDIRECTORY of its worktree. `sync._is_repo` is
    top-of-worktree-only, so this store is "not a sync repo" to the
    wrapper and to `sync_tracked_ignored` — only the parent-side check
    can see what the parent tracks. ``store_name`` lets the pathspec
    tests pick a directory name containing glob / pathspec-magic
    characters. Hermetic global git config and discovery ceiling,
    mirroring `_store_repo`.
    Returns ``(parent, store)``."""
    parent = tmp_path / "home"
    store = parent / store_name
    store.mkdir(parents=True)
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    _git_in(parent, "init", "--initial-branch", "main")
    _git_in(parent, "config", "--global", "user.email", "test@example.com")
    _git_in(parent, "config", "--global", "user.name", "Test")
    return parent, store


def _grandparent_repo_with_doubly_nested_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A doubly-nested chain: grandparent repo B (``grand/``) whose
    index tracked the store's plaintext sidecar BEFORE the intermediate
    directory (``grand/home/``) became repo A. B's stale entries
    survive A's later ``git init`` (git does not auto-untrack), while
    A's own index is clean — the shape where only a walk PAST the
    innermost enclosing worktree can see the leak. The store
    (``grand/home/memory-store/``) is left a plain directory so callers
    pick its final shape (plain, or `sync.init` for the
    store-as-own-repo chain). Hermetic global git config and discovery
    ceiling, mirroring `_store_repo` — the ceiling sits ABOVE tmp_path,
    so the walk still has to discover BOTH in-sandbox levels itself.
    Returns ``(grand, parent, store)``."""
    grand = tmp_path / "grand"
    parent = grand / "home"
    store = parent / "memory-store"
    store.mkdir(parents=True)
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    _git_in(grand, "init", "--initial-branch", "main")
    _git_in(grand, "config", "--global", "user.email", "test@example.com")
    _git_in(grand, "config", "--global", "user.name", "Test")
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    _git_in(grand, "add", "-A")
    _git_in(grand, "commit", "-m", "grand: add everything")
    # A becomes a repo only AFTER B tracked the sidecar — the true
    # upgrade shape, one level further out than the combined fixture.
    _git_in(parent, "init", "--initial-branch", "main")
    return grand, parent, store


@_needs_git
def test_store_nested_in_parent_repo_warns_on_parent_tracked_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dotfiles-style parent repo `git add -A`s everything under its
    worktree — including the plaintext raw-capture queue inside the
    nested store. The sync wrapper deliberately does not consider the
    nested store a repo, so `sync_tracked_ignored` stays silent and
    this leak route was previously undetected on the bettermemory
    side. The check must WARN (never FAIL — a nested store in a
    local-only parent repo is a legitimate setup), name the parent
    toplevel and the tracked path, and print the parent-side gitignore
    + `git rm --cached` + history-rewrite / secret-rotation
    remediation — and the prescribed untracking must actually clear
    the warn."""
    parent, store = _parent_repo_with_nested_store(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    tracked_rel = f"memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: the parent tracks the sidecar, while the sync
    # wrapper (and therefore the sibling check) does not read the
    # nested store as a repo at all — the exact blind spot under test.
    assert tracked_rel in _git_in(parent, "ls-files")
    assert not sync._is_repo(store)
    sibling = _check_sync_tracked_ignored(store)
    assert sibling.status == "ok"
    assert "not a git sync repo" in sibling.message

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.name == "store_nested_in_parent_repo"
    assert str(parent.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert ".gitignore" in (diag.fix_hint or "")
    assert "git-filter-repo" in (diag.fix_hint or "")
    assert "rotate" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]
    # The rendered doctor output carries the check name and remediation.
    out = render_text(DoctorReport(checks=[diag]))
    assert "store_nested_in_parent_repo" in out
    assert "git rm --cached" in out

    # The prescribed parent-side untracking clears the warn (the file
    # stays on disk, now merely untracked).
    _git_in(parent, "rm", "--cached", tracked_rel)
    _git_in(parent, "commit", "-m", "untrack the store capture queue")
    assert (store / PROPOSALS_FILENAME).exists()
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_clean_nested_store_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy nested setup: the parent repo's own .gitignore keeps
    the store's transient sidecars out of its index (while versioning
    the memory bodies is the user's own arrangement, not ours to
    judge). Nothing sidecar-shaped is tracked, so the check must not
    alarm — benign-but-notable states surface as an ok with the
    nesting noted (the `audit_turn_cadence` single-session shape), not
    a warn."""
    parent, store = _parent_repo_with_nested_store(tmp_path, monkeypatch)
    (parent / ".gitignore").write_text(
        f"memory-store/{PROPOSALS_FILENAME}\n*.tmp\n", encoding="utf-8"
    )
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "captured text that must stay host-local"}\n', encoding="utf-8"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: memories only")
    # Premise pin: the sidecar is on disk but the parent ignores it.
    tracked = _git_in(parent, "ls-files")
    assert PROPOSALS_FILENAME not in tracked
    assert "a-memory.md" in tracked

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None
    assert str(parent.resolve()) in diag.message  # nesting still noted
    assert diag.details["parent_toplevel"] == str(parent.resolve())


@_needs_git
def test_store_nested_in_parent_repo_stands_down_when_store_is_own_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that IS the top of its own git worktree is exactly the
    shape `sync_tracked_ignored` owns — the nested check must return
    ok and point at the owner, even with a sidecar tracked, so one
    leak never double-reports across two checks."""
    store = _store_repo(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "tracked in the store own repo"}\n', encoding="utf-8"
    )
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert "sync_tracked_ignored" in diag.message
    # The owner check still fires on the same fixture — the boundary
    # between the two checks moved nothing.
    assert _check_sync_tracked_ignored(store).status == "fail"


def test_store_nested_in_parent_repo_ok_on_non_git_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store outside any git worktree has no parent repo to leak
    through — the check must pass without demanding git ceremony (and
    when git itself is absent the probe degrades to the same verdict,
    mirroring `_is_repo`'s SyncError-to-False). The discovery ceiling
    keeps "outside any git worktree" true even when pytest's own
    basetemp sits under a real repo — without it the upward walk finds
    that repo and the verdict flips."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    (tmp_path / PROPOSALS_FILENAME).write_text(
        '{"content": "local only"}\n', encoding="utf-8"
    )
    diag = _check_store_nested_in_parent_repo(tmp_path)
    assert diag.status == "ok"
    assert "not inside any git worktree" in diag.message


def test_store_nested_in_parent_repo_missing_dir_does_not_create_it(
    tmp_path: Path,
) -> None:
    """Read-only probe contract shared with the sibling checks: a
    never-created storage dir is reported ok and left uncreated."""
    ghost = tmp_path / "ghost"
    diag = _check_store_nested_in_parent_repo(ghost)
    assert diag.status == "ok"
    assert not ghost.exists()


@_needs_git
def test_store_nested_in_parent_repo_warns_on_combined_nesting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combined nesting: the store began as a plain subdirectory of a
    dotfiles-style parent repo — whose `git add -A` tracked the
    plaintext raw-capture queue — and only LATER ran `bettermemory
    sync init`, becoming its own worktree toplevel. That shape evaded
    BOTH checks: `rev-parse --show-toplevel` from inside the store now
    answers with the store itself (so the pre-fix nested check stood
    down), while `sync_tracked_ignored` reads only the STORE repo's
    index — but the PARENT's stale index entries survive the nested
    `git init` (git does not auto-untrack) and keep shipping the
    sidecar blobs with every parent push. The check must probe upward
    from the store's parent DIRECTORY and WARN, naming the parent
    toplevel and the tracked paths with the same parent-side
    remediation — and the prescribed untracking must clear it."""
    parent, store = _parent_repo_with_nested_store(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    # The store becomes its own repo only AFTER the parent tracked it —
    # the real `sync init` path, so the fixture is the true upgrade shape.
    sync.init(store)
    tracked_rel = f"memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: the store IS its own toplevel now (the shape the
    # pre-fix check stood down on), the sibling check sees only the
    # store's clean index, and the PARENT still tracks the pre-init
    # sidecar — the exact blind spot under test.
    assert sync._is_repo(store)
    assert _check_sync_tracked_ignored(store).status == "ok"
    assert tracked_rel in _git_in(parent, "ls-files")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.name == "store_nested_in_parent_repo"
    assert str(parent.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert ".gitignore" in (diag.fix_hint or "")
    assert "git-filter-repo" in (diag.fix_hint or "")
    assert "rotate" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]

    # The prescribed parent-side untracking clears the warn (the file
    # stays on disk inside the store's own repo, merely untracked by
    # the parent).
    _git_in(parent, "rm", "--cached", tracked_rel)
    _git_in(parent, "commit", "-m", "untrack the store capture queue")
    assert (store / PROPOSALS_FILENAME).exists()
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_own_toplevel_in_clean_parent_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legitimate monorepo / home-repo shape: a store that is its
    own worktree toplevel sits nested inside a parent repo that tracks
    NOTHING under it — a normal `sync init` inside a git-managed $HOME
    looks exactly like this. The upward probe must key strictly on
    actually-tracked matching paths in the parent index; mere nesting
    never alarms."""
    parent, store = _parent_repo_with_nested_store(tmp_path, monkeypatch)
    (parent / "README.md").write_text("# dotfiles\n", encoding="utf-8")
    _git_in(parent, "add", "README.md")
    _git_in(parent, "commit", "-m", "dotfiles: nothing under the store")
    sync.init(store)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "captured text that must stay host-local"}\n', encoding="utf-8"
    )
    # Premise pins: own toplevel, and the parent tracks nothing under it.
    assert sync._is_repo(store)
    assert not _git_in(parent, "ls-files", "--", "memory-store").strip()

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None
    # The nesting is still noted with the parent named, and the
    # store-repo shape still deflects store-index sidecars to the
    # sibling check.
    assert str(parent.resolve()) in diag.message
    assert "sync_tracked_ignored" in diag.message
    assert diag.details["parent_toplevel"] == str(parent.resolve())


@_needs_git
def test_store_nested_in_parent_repo_scans_glob_metachar_store_path_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store path with glob metacharacters (`mem[0]ry-store` is a
    legal directory name on APFS, ext4 AND NTFS) must be scanned as a
    LITERAL path — the `:(literal)` pathspec magic pins that down. A
    raw pathspec hands `[0]` to git's pathspec engine as a character
    class; current git happens to rescue this exact shape through
    `match_pathspec_item`'s literal-prefix fast path, but that is an
    undocumented implementation detail, not a contract — the sibling
    magic-prefix / trailing-glob tests show the raw spelling really
    does miss and over-match on other legal names. This test is the
    Windows-runnable guard that metachar store paths stay detected —
    and that the emitted `git rm --cached` remediation, split exactly
    as a POSIX shell would split it, EXECUTES and untracks ONLY the
    bracketed target: `[0]` globs to `0`, so a raw spelling of this
    path silently untracks the innocent sibling `mem0ry-store/…`
    instead (rc=0) — the one outcome worse than failing."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="mem[0]ry-store"
    )
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    sibling = parent / "mem0ry-store"
    sibling.mkdir()
    (sibling / PROPOSALS_FILENAME).write_text(
        '{"content": "the innocent sibling the glob would hit"}\n', encoding="utf-8"
    )
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    tracked_rel = f"mem[0]ry-store/{PROPOSALS_FILENAME}"
    sibling_rel = f"mem0ry-store/{PROPOSALS_FILENAME}"
    # Premise pins: the parent really tracks the sidecar under the
    # bracketed store path, and the glob-shaped sibling is tracked too.
    tracked_before = _git_in(parent, "ls-files")
    assert tracked_rel in tracked_before
    assert sibling_rel in tracked_before

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert str(parent.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["store_prefix"] == "mem[0]ry-store"
    # The literal scan confines itself to the store: the sibling the
    # glob would match never enters the diagnosis.
    assert diag.details["tracked_sidecars"] == [tracked_rel]
    # Single-nesting diagnoses keep their established shape: the
    # multi-level rollup key only appears when the walk found more
    # than one enclosing repo.
    assert "scanned_parent_toplevels" not in diag.details

    # Execute the emitted remediation verbatim: rc=0, the bracketed
    # target is untracked, the innocent sibling stays tracked, and the
    # cleared index passes the re-check.
    match = re.search(r"run `(git rm --cached [^`]*)`", diag.fix_hint or "")
    assert match is not None
    rm = subprocess.run(
        shlex.split(match.group(1)),
        cwd=parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rm.returncode == 0, rm.stderr
    remaining = _git_in(parent, "ls-files")
    assert tracked_rel not in remaining
    assert sibling_rel in remaining
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_quote_in_store_path_hint_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store path with an embedded single quote (`o'brien-store` is a
    legal directory name on APFS, ext4 AND NTFS) lands verbatim in the
    emitted `git rm --cached` hint. The pre-fix fixed `'…'` wrap could
    not represent it: the path's own `'` terminated the quoted span
    early and left the command quote-imbalanced, so `shlex.split`
    raises "No closing quotation" and a real POSIX shell dies with
    "unexpected EOF while looking for matching `'`" (rc=2) — a dead
    command handed to someone remediating a plaintext leak, the loud
    sibling of the metachar test's silent wrong-target. Post-fix
    `shlex.quote` escapes the quote, so the command splits cleanly,
    EXECUTES, and untracks only the sidecar."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="o'brien-store"
    )
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    tracked_rel = f"o'brien-store/{PROPOSALS_FILENAME}"
    memory_rel = "o'brien-store/a-memory.md"
    tracked_before = _git_in(parent, "ls-files")
    assert tracked_rel in tracked_before
    assert memory_rel in tracked_before

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert str(parent.resolve()) in diag.message
    assert diag.details["tracked_sidecars"] == [tracked_rel]
    match = re.search(r"run `(git rm --cached [^`]*)`", diag.fix_hint or "")
    assert match is not None
    command = match.group(1)
    # POSIX splitting must round-trip the command to the one literal
    # pathspec — the pre-fix hint dies right here with "No closing
    # quotation".
    assert shlex.split(command) == [
        "git",
        "rm",
        "--cached",
        f":(literal){tracked_rel}",
    ]
    # Execute the emitted remediation: through a real POSIX shell where
    # there is one (the paste target the hint is written for); on
    # Windows, split exactly as that shell would.
    if sys.platform == "win32":
        rm = subprocess.run(
            shlex.split(command),
            cwd=parent,
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        rm = subprocess.run(
            command,
            shell=True,
            cwd=parent,
            capture_output=True,
            text=True,
            check=False,
        )
    assert rm.returncode == 0, rm.stderr
    remaining = _git_in(parent, "ls-files")
    assert tracked_rel not in remaining
    assert memory_rel in remaining
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_pattern_matching_store_dirname_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: the sidecar patterns' authoritative frame is the
    STORE root, but the parent scan matched every component of the
    TOPLEVEL-relative ls-files row — the store prefix included. A store
    directory literally named `state.tmp` (legal everywhere) then made
    EVERY tracked file beneath it — plain memory .md bodies included —
    fnmatch `*.tmp`, so doctor reported the user's legitimate memories
    as 'transient sidecar files' and handed them the untrack +
    history-rewrite + secret-rotation walkthrough; worse, that
    remediation never converges (the parent re-tracks the memories on
    its next `git add -A`). In the store frame — the one the store's
    own .gitignore and the check-ignore parity oracle speak — nothing
    ignores `a-memory.md`, so the check must stay ok. At EVERY walk
    level: the intermediate `cache.tmp` repo directory between the
    grandparent toplevel and the store puts a second pattern-matching
    component into the OUTER level's rows."""
    grand = tmp_path / "grand"
    parent = grand / "cache.tmp"  # repo A: its own dirname fnmatches *.tmp
    store = parent / "state.tmp"  # the store: dirname fnmatches *.tmp
    store.mkdir(parents=True)
    global_config = tmp_path / "test.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    _git_in(grand, "init", "--initial-branch", "main")
    _git_in(grand, "config", "--global", "user.email", "test@example.com")
    _git_in(grand, "config", "--global", "user.name", "Test")
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(grand, "add", "-A")
    _git_in(grand, "commit", "-m", "grand: add everything")
    # The intermediate dir becomes repo A only AFTER B tracked the
    # memory, mirroring the established doubly-nested upgrade shape.
    _git_in(parent, "init", "--initial-branch", "main")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "cache.tmp: add everything")
    # Premise pins: both enclosing indexes hold ONLY the memory body,
    # under prefixes whose components fnmatch a live pattern, while the
    # store-relative remainder matches nothing (the parity oracle pins
    # this matcher to `git check-ignore`, so this IS the store-frame
    # not-ignored verdict).
    assert _git_in(parent, "ls-files").splitlines() == ["state.tmp/a-memory.md"]
    assert _git_in(grand, "ls-files").splitlines() == [
        "cache.tmp/state.tmp/a-memory.md"
    ]
    patterns = [
        line
        for line in sync._GITIGNORE_LINES
        if line and not line.lstrip().startswith("#")
    ]
    assert any(_pattern_matches_tracked_path("state.tmp", p) for p in patterns)
    assert not any(_pattern_matches_tracked_path("a-memory.md", p) for p in patterns)

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok", (
        f"legitimate memories misreported as sidecars: {diag.message}"
    )
    assert diag.fix_hint is None
    # Both walk levels were really scanned and BOTH came back clean —
    # the ok is a full-depth verdict, not an early bail.
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["store_prefix"] == "state.tmp"
    assert diag.details["scanned_parent_toplevels"] == [
        str(parent.resolve()),
        str(grand.resolve()),
    ]


@_needs_git
def test_store_nested_in_parent_repo_sidecar_under_pattern_named_store_still_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction of the store-frame fix: a REAL leak nested
    under a pattern-named store must still surface. In the store frame
    `snapshots.tmp/file.md` IS ignored (a matching DIRECTORY component
    ignores everything beneath it — the silent-miss shape 6167cd3
    closed), so under a store named `state.tmp` the scan must warn
    about exactly that path, leave the sibling memory body alone, and
    the emitted remediation must CONVERGE: after untracking the one
    sidecar the re-check is ok even though the parent still tracks the
    memory under the pattern-named store dir (pre-fix the memory itself
    re-warned, so the walkthrough could never clear)."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="state.tmp"
    )
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    (store / "snapshots.tmp").mkdir()
    (store / "snapshots.tmp" / "file.md").write_text(
        "---\n---\nleaked snapshot body\n", encoding="utf-8"
    )
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    sidecar_rel = "state.tmp/snapshots.tmp/file.md"
    memory_rel = "state.tmp/a-memory.md"
    tracked_before = _git_in(parent, "ls-files")
    assert sidecar_rel in tracked_before
    assert memory_rel in tracked_before

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.details["tracked_sidecars"] == [sidecar_rel]
    assert f"git rm --cached ':(literal){sidecar_rel}'" in (diag.fix_hint or "")
    # The legitimate memory body never enters the diagnosis.
    assert memory_rel not in diag.message
    assert memory_rel not in (diag.fix_hint or "")

    # The prescribed untracking converges: the memory stays tracked
    # under the pattern-named store dir and the re-check is ok.
    _git_in(parent, "rm", "--cached", sidecar_rel)
    _git_in(parent, "commit", "-m", "untrack the leaked snapshot")
    assert memory_rel in _git_in(parent, "ls-files")
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_skips_store_gitlink_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store absorbed into the parent index as a SUBMODULE-style
    gitlink (`git add <store>` on an embedded repo records ONE
    mode-160000 row whose path EQUALS the store prefix — no files
    beneath). That row is the store itself, not a sidecar under it, and
    it has no store-relative remainder to match; with a
    pattern-matching store dirname the pre-fix whole-row match claimed
    it and told the user to `git rm --cached` their own submodule
    registration. The scan must skip the row — without crashing — and
    stay ok."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="state.tmp"
    )
    _git_in(store, "init", "--initial-branch", "main")
    (store / "a-memory.md").write_text("---\n---\nbody\n", encoding="utf-8")
    _git_in(store, "add", "-A")
    _git_in(store, "commit", "-m", "store: memories")
    _git_in(parent, "add", "state.tmp")  # records the gitlink, not the files
    _git_in(parent, "commit", "-m", "dotfiles: absorb the store as a gitlink")
    # Premise pins: the parent's only row under the literal prefix IS
    # the prefix itself, at gitlink mode 160000.
    rows = [
        row
        for row in _git_in(parent, "ls-files", "-z", "--", ":(literal)state.tmp").split(
            "\0"
        )
        if row
    ]
    assert rows == ["state.tmp"]
    assert "160000" in _git_in(parent, "ls-files", "-s", "--", ":(literal)state.tmp")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None
    assert str(parent.resolve()) in diag.message


@_needs_git
def test_store_nested_in_parent_repo_listing_failure_warns_and_hint_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `listing.returncode != 0` arm: a parent whose repo DISCOVERY
    resolves (rev-parse reads no index) but whose index cannot be read
    (corrupt `.git/index` — a crashed writer, a partial restore) makes
    the ls-files scan fail rc=128. The check must degrade to a WARN
    carrying git's own error — never a silent ok over a leak it could
    not rule out — and the investigate hint must carry the store prefix
    as a shell-safe `:(literal)` pathspec: with a quote-bearing store
    dirname (`o'brien-store`) a raw interpolation dies in the shell
    ("unexpected EOF") before git ever runs. The emitted command must
    round-trip `shlex.split` to the exact argv and, executed from the
    parent, reproduce the same git failure the check degraded on (git's
    own exit code, not a shell syntax-error death)."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="o'brien-store"
    )
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    # Corrupt the parent's index AFTER the commit: repo discovery still
    # resolves, every index read fails.
    (parent / ".git" / "index").write_bytes(b"garbage, not a git index")
    # Premise pins: discovery from the store still names the parent
    # (the walk reaches the scan), and the listing really fails there.
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=store,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0
    assert Path(probe.stdout.strip()).resolve() == parent.resolve()
    direct = subprocess.run(
        ["git", "ls-files"], cwd=parent, capture_output=True, text=True, check=False
    )
    assert direct.returncode != 0

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert "listing what that repo tracks under the store failed" in diag.message
    assert str(parent.resolve()) in diag.message
    assert diag.details["store_prefix"] == "o'brien-store"
    match = re.search(r"Run `(git ls-files -- [^`]*)` from", diag.fix_hint or "")
    assert match is not None
    command = match.group(1)
    # POSIX splitting must round-trip to the one literal pathspec — a
    # raw spelling dies right here with "No closing quotation".
    assert shlex.split(command) == [
        "git",
        "ls-files",
        "--",
        ":(literal)o'brien-store",
    ]
    # Execute the investigate command from the parent (real POSIX shell
    # where there is one; on Windows, split exactly as that shell
    # would): it must reach git and reproduce the failure the check
    # degraded on.
    if sys.platform == "win32":
        ran = subprocess.run(
            shlex.split(command),
            cwd=parent,
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        ran = subprocess.run(
            command,
            shell=True,
            cwd=parent,
            capture_output=True,
            text=True,
            check=False,
        )
    assert ran.returncode == direct.returncode
    assert ran.stderr.strip()


@_needs_git
@_needs_posix_store_names
def test_store_nested_in_parent_repo_magic_prefix_store_path_not_silently_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store directory whose name starts with `:` (legal on POSIX
    filesystems) is the reproducible SILENT-MISS shape for a raw
    pathspec: git parses a leading `:` as the pathspec-magic marker,
    strips it, and matches the remainder — `git ls-files --
    ':memory-store'` lists NOTHING (exit 0) for a repo that tracks
    `:memory-store/<sidecar>`, so the pre-fix scan reported a clean
    parent while the plaintext capture queue kept shipping with every
    parent push: a false negative in the exact leak class this check
    exists to close. The `:(literal)` magic pins the prefix to the
    real directory and the sidecar must surface as a WARN — and the
    emitted `git rm --cached` remediation must EXECUTE: quoting the
    raw path produced `git rm --cached :memory-store/…`, which aborts
    rc=128 on the same leading-`:` magic, a dead command handed to
    someone remediating a plaintext leak."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name=":memory-store"
    )
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    tracked_rel = f":memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: the parent tracks the sidecar, yet the RAW pathspec
    # spelling of the pre-fix scan lists NOTHING and exits 0 — the
    # silent miss captured verbatim (`_git_in` raises on failure, so
    # emptiness here really is silence, not an error).
    assert tracked_rel in _git_in(parent, "ls-files")
    assert _git_in(parent, "ls-files", "-z", "--", ":memory-store") == ""

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert str(parent.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]

    # Execute the emitted remediation verbatim (split exactly as a
    # POSIX shell would): it must succeed and clear the re-check — the
    # raw spelling of this same path is a hard rc=128.
    match = re.search(r"run `(git rm --cached [^`]*)`", diag.fix_hint or "")
    assert match is not None
    rm = subprocess.run(
        shlex.split(match.group(1)),
        cwd=parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rm.returncode == 0, rm.stderr
    assert tracked_rel not in _git_in(parent, "ls-files")
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
@_needs_posix_store_names
def test_store_nested_in_parent_repo_glob_store_path_cannot_hit_outside_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the raw-pathspec defect: a trailing-`*` store
    name fnmatches ACROSS `/` into sibling directories (`fnmatch` with
    no FNM_PATHNAME), so the pre-fix scan of a store named
    `memory-store-*` listed the parent's `memory-store-old/orphan.tmp`
    — a tracked path OUTSIDE the store — and warned about a leak the
    store does not have. Post-fix the `:(literal)` prefix confines the
    scan to the actual store directory: the sibling stays invisible
    and a clean store stays ok."""
    parent, store = _parent_repo_with_nested_store(
        tmp_path, monkeypatch, store_name="memory-store-*"
    )
    sibling = parent / "memory-store-old"
    sibling.mkdir()
    (sibling / "orphan.tmp").write_text("not the store's file", encoding="utf-8")
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    # Premise pins: the sibling's sidecar-shaped file is tracked, the
    # store itself has nothing tracked under it, and the RAW pathspec
    # really does over-match into the sibling directory.
    assert "memory-store-old/orphan.tmp" in _git_in(parent, "ls-files")
    assert "memory-store-old/orphan.tmp" in _git_in(
        parent, "ls-files", "-z", "--", "memory-store-*"
    )

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert diag.fix_hint is None
    assert str(parent.resolve()) in diag.message
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["store_prefix"] == "memory-store-*"


@_needs_git
def test_store_nested_in_parent_repo_warns_on_doubly_nested_grandparent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubly-nested chain: the store is its own worktree toplevel
    inside repo A (clean index), and A is nested inside grandparent B
    whose index tracked the store's sidecar BEFORE either nested repo
    existed. git never auto-untracks, so B keeps shipping the
    plaintext blob while both the store repo and A look clean — the
    pre-fix probe stopped at the innermost enclosing worktree (A) and
    reported ok, an arbitrary cut in the exact stale-index mechanism
    the combined-nesting fix closed. The upward walk must continue
    past A, scan B's index, and WARN naming B with the tracked path —
    and the prescribed grandparent-side untracking must clear it."""
    grand, parent, store = _grandparent_repo_with_doubly_nested_store(
        tmp_path, monkeypatch
    )
    # The store becomes its own repo only AFTER B tracked the sidecar.
    sync.init(store)
    tracked_rel = f"home/memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: the store IS its own toplevel, the innermost
    # enclosing repo A tracks nothing, the sibling check sees only the
    # store's clean index, and B still tracks the pre-init sidecar —
    # the exact blind spot under test.
    assert sync._is_repo(store)
    assert not _git_in(parent, "ls-files").strip()
    assert _check_sync_tracked_ignored(store).status == "ok"
    assert tracked_rel in _git_in(grand, "ls-files")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.name == "store_nested_in_parent_repo"
    assert str(grand.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert ".gitignore" in (diag.fix_hint or "")
    assert "git-filter-repo" in (diag.fix_hint or "")
    assert "rotate" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(grand.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]
    # Both enclosing repos were scanned, innermost first.
    assert diag.details["scanned_parent_toplevels"] == [
        str(parent.resolve()),
        str(grand.resolve()),
    ]

    # The prescribed grandparent-side untracking clears the warn (the
    # file stays on disk inside the store's own repo).
    _git_in(grand, "rm", "--cached", tracked_rel)
    _git_in(grand, "commit", "-m", "untrack the store capture queue")
    assert (store / PROPOSALS_FILENAME).exists()
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
def test_store_nested_in_parent_repo_warns_on_plain_doubly_nested_grandparent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same doubly-nested chain as the combined test but with the store
    left a PLAIN subdirectory (never `sync init`ed): the innermost
    enclosing worktree is repo A with a clean index, and grandparent
    B's stale entries are the only copy of the leak. The pre-fix scan
    stopped at A and reported the chain clean; the walk must reach B
    and WARN with the outer-level leak route (stale index entries that
    git never auto-untracked)."""
    grand, parent, store = _grandparent_repo_with_doubly_nested_store(
        tmp_path, monkeypatch
    )
    tracked_rel = f"home/memory-store/{PROPOSALS_FILENAME}"
    # Hermeticity premise: the discovery ceiling is pinned ABOVE
    # tmp_path (a ceiling AT tmp_path would be inert for the walk's
    # final probe, which launches from tmp_path itself), and the
    # `scanned_parent_toplevels` assertion below doubles as the proof
    # that both fixture repos BELOW the ceiling are still discovered —
    # the walk under test is real, not neutered. Without this env var
    # the walk escapes the sandbox and this test fails whenever
    # pytest's basetemp sits under any real git repo.
    assert os.environ["GIT_CEILING_DIRECTORIES"] == os.pathsep.join(
        [str(tmp_path.parent), str(tmp_path.parent.parent)]
    )
    # Premise pins: plain store (not a repo), clean innermost parent,
    # tracking grandparent.
    assert not sync._is_repo(store)
    assert not _git_in(parent, "ls-files").strip()
    assert tracked_rel in _git_in(grand, "ls-files")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert str(grand.resolve()) in diag.message
    assert "auto-untrack" in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(grand.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]
    assert diag.details["scanned_parent_toplevels"] == [
        str(parent.resolve()),
        str(grand.resolve()),
    ]


@_needs_git
def test_store_nested_in_parent_repo_aggregates_hits_across_enclosing_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When SEVERAL enclosing repos track sidecars under the store
    (innermost parent A via its own `git add -A`, grandparent B via
    pre-nesting stale entries), the walk must aggregate every level
    into ONE warn that names each offending parent toplevel with its
    tracked paths and per-parent remediation — and untracking in BOTH
    parents must clear it."""
    grand, parent, store = _grandparent_repo_with_doubly_nested_store(
        tmp_path, monkeypatch
    )
    # A tracks the (plain-subdirectory) store's sidecar itself.
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "home: add everything")
    inner_rel = f"memory-store/{PROPOSALS_FILENAME}"
    outer_rel = f"home/memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: both indexes hold the sidecar independently.
    assert inner_rel in _git_in(parent, "ls-files")
    assert outer_rel in _git_in(grand, "ls-files")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.name == "store_nested_in_parent_repo"
    assert str(parent.resolve()) in diag.message
    assert str(grand.resolve()) in diag.message
    assert outer_rel in diag.message
    assert f"from {parent.resolve()} run `git rm --cached ':(literal){inner_rel}'`" in (
        diag.fix_hint or ""
    )
    assert f"from {grand.resolve()} run `git rm --cached ':(literal){outer_rel}'`" in (
        diag.fix_hint or ""
    )
    assert "git-filter-repo" in (diag.fix_hint or "")
    assert "rotate" in (diag.fix_hint or "")
    assert diag.details["parent_toplevels"] == [
        str(parent.resolve()),
        str(grand.resolve()),
    ]
    assert diag.details["tracked_by_parent"] == {
        str(parent.resolve()): [inner_rel],
        str(grand.resolve()): [outer_rel],
    }
    # The aggregate diagnosis renders and JSON-serialises cleanly.
    report = DoctorReport(checks=[diag])
    assert "store_nested_in_parent_repo" in render_text(report)
    assert json.loads(render_json(report))["overall"] == "warn"

    # Untracking in BOTH parents clears the aggregate warn.
    _git_in(parent, "rm", "--cached", inner_rel)
    _git_in(parent, "commit", "-m", "untrack inner")
    _git_in(grand, "rm", "--cached", outer_rel)
    _git_in(grand, "commit", "-m", "untrack outer")
    assert _check_store_nested_in_parent_repo(store).status == "ok"


@_needs_git
@pytest.mark.parametrize(
    "gitfile_content",
    ["gitdir: /nonexistent/path\n", "not a gitfile\n"],
    ids=["dangling-gitdir", "garbage-content"],
)
def test_store_nested_in_parent_repo_warns_through_broken_store_gitfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gitfile_content: str
) -> None:
    """A broken `.git` gitfile in the store — a dangling `gitdir:`
    target after the linked worktree's main repo was deleted or moved,
    or garbage content from a backup tool that restored the store
    without the worktree admin dir — makes `rev-parse --show-toplevel`
    from inside the store abort rc=128 WITHOUT continuing upward. The
    pre-fix check read ANY failed probe as "not inside any git
    worktree" and stood down, and `sync_tracked_ignored` stands down
    on the same broken probe (`_is_repo` False — correct behavior, the
    nested check owns this shape), so a healthy enclosing parent kept
    tracking the plaintext queue AND its `git add -A` kept staging NEW
    sidecars under the store (git only honours a nested-repo boundary
    whose `.git` validates; a broken one is traversed like any plain
    directory): an ACTIVE leak with zero warnings from either check.
    The probe-failure branch must restart discovery from the store's
    parent directory and WARN naming the parent and the tracked path.
    Mutation proof: reverting the fallback (standing down on any
    failed probe, the pre-fix shape) fails this test with ok != warn.
    """
    parent, store = _parent_repo_with_nested_store(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "my staging DB password is hunter2"}\n', encoding="utf-8"
    )
    _git_in(parent, "add", "-A")
    _git_in(parent, "commit", "-m", "dotfiles: add everything")
    (store / ".git").write_text(gitfile_content, encoding="utf-8")
    tracked_rel = f"memory-store/{PROPOSALS_FILENAME}"
    # Premise pins: discovery from inside the store really aborts (the
    # false-negative trigger), the parent still tracks the sidecar, and
    # a NEW plaintext file under the store still gets staged by the
    # parent's `git add -A` — active leakage, not just a stale index.
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=store,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0
    assert tracked_rel in _git_in(parent, "ls-files")
    (store / "orphan.tmp").write_text("new plaintext capture", encoding="utf-8")
    assert "memory-store/orphan.tmp" in _git_in(parent, "add", "-A", "--dry-run")
    # The sibling check stands down cleanly on the same fixture — one
    # leak, one owner: this shape belongs to the nested check.
    sibling = _check_sync_tracked_ignored(store)
    assert sibling.status == "ok"
    assert "not a git sync repo" in sibling.message

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "warn"
    assert diag.name == "store_nested_in_parent_repo"
    assert str(parent.resolve()) in diag.message
    assert PROPOSALS_FILENAME in diag.message
    assert f"git rm --cached ':(literal){tracked_rel}'" in (diag.fix_hint or "")
    assert diag.details["parent_toplevel"] == str(parent.resolve())
    assert diag.details["tracked_sidecars"] == [tracked_rel]


@_needs_git
def test_store_nested_in_parent_repo_broken_gitfile_without_parent_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe-failure fallback keys on an ACTUAL enclosing repo: a
    store with the same broken `.git` gitfile but genuinely under no
    git repo walks to nothing and keeps the quiet "not inside any git
    worktree" ok — a failed probe alone must never manufacture a
    parent. The discovery ceiling pins the fallback walk inside the
    pytest sandbox so this verdict cannot flip when basetemp itself
    sits under a real repo."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    store = tmp_path / "store"
    store.mkdir()
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "local only"}\n', encoding="utf-8"
    )
    (store / ".git").write_text("gitdir: /nonexistent/path\n", encoding="utf-8")

    diag = _check_store_nested_in_parent_repo(store)
    assert diag.status == "ok"
    assert "not inside any git worktree" in diag.message


# ---------------------------------------------------------------------------
# `doctor --fix`
#
# One break→fix→green pair per AUTO-fixable check, the no-op contract,
# the manual/auto boundary (fixes never touch git indexes or client
# configs), the per-applied-fix event record, and the post-fix exit
# code. Fixers are exercised through `run_fixes` so the registry
# dispatch, the exception wrapper, and the event recording run on every
# pair — not just the fixer body.
# ---------------------------------------------------------------------------


def test_fix_storage_directory_chmods_unwritable_dir(tmp_path: Path) -> None:
    """break→fix→green: an existing-but-unwritable store dir is
    chmod'd to the private 0700 posture, and the re-run turns green."""
    cfg = _config_for(tmp_path)
    tmp_path.chmod(0o555)
    try:
        diag, _resolved = _check_storage_directory(cfg)
        if diag.status == "ok":
            pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")
        fixes = run_fixes(DoctorReport(checks=[diag]), cfg=cfg, directory=tmp_path)
        mode_after = stat.S_IMODE(tmp_path.stat().st_mode)
    finally:
        tmp_path.chmod(0o755)  # restore so pytest can clean up
    assert [f.action for f in fixes] == ["chmod_storage_dir"]
    fix = fixes[0]
    assert fix.applied is True
    assert fix.before_status == "fail"
    assert fix.after_status == "ok"
    assert fix.details["new_mode"] == "0o700"
    assert mode_after == 0o700


def test_fix_storage_directory_ignores_non_perms_failures(tmp_path: Path) -> None:
    """A storage failure that ISN'T the unwritable branch (here: the
    resolved path is a FILE) has no auto fix — the fixer's ground-truth
    guard declines and the finding stays manual with its hint."""
    target = tmp_path / "store-as-file"
    target.write_text("not a dir", encoding="utf-8")
    cfg = _config_for(target)
    diag, _resolved = _check_storage_directory(cfg)
    assert diag.status == "fail"
    assert run_fixes(DoctorReport(checks=[diag]), cfg=cfg, directory=target) == []
    assert target.read_text(encoding="utf-8") == "not a dir"


def test_fix_storage_directory_reports_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chmod itself being denied (immutable flag, ACL, read-only
    mount) is reported as an honest not-applied FixResult — the error
    named, before/after statuses unchanged — never a raise and never a
    claimed green."""
    cfg = _config_for(tmp_path)
    tmp_path.chmod(0o555)
    try:
        diag, _resolved = _check_storage_directory(cfg)
        if diag.status == "ok":
            pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")

        def _deny(self: Path, mode: int, **_kwargs: Any) -> None:
            raise PermissionError(f"chmod denied on {self}")

        monkeypatch.setattr(Path, "chmod", _deny)
        fixes = run_fixes(DoctorReport(checks=[diag]), cfg=cfg, directory=tmp_path)
        mode_after = stat.S_IMODE(tmp_path.stat().st_mode)
    finally:
        # os.chmod, not Path.chmod — the latter is still monkeypatched.
        os.chmod(tmp_path, 0o755)  # restore so pytest can clean up
    assert [f.action for f in fixes] == ["chmod_storage_dir"]
    fix = fixes[0]
    assert fix.applied is False
    assert fix.before_status == "fail"
    assert fix.after_status == "fail"
    assert "PermissionError" in (fix.error or "")
    assert fix.details["old_mode"] == oct(0o555)
    assert mode_after == 0o555  # nothing mutated


def test_fix_event_log_chmods_unwritable_file(tmp_path: Path) -> None:
    """break→fix→green: an unwritable event log is chmod'd to the 0600
    the Recorder itself sets on first write."""
    from bettermemory.events import EVENT_LOG_FILENAME

    log = tmp_path / EVENT_LOG_FILENAME
    log.write_text("", encoding="utf-8")
    log.chmod(0o400)
    diag = _check_event_log_writable(tmp_path)
    if diag.status == "ok":
        pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")
    assert diag.status == "fail"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert [f.action for f in fixes] == ["chmod_event_log"]
    assert fixes[0].applied is True
    assert fixes[0].after_status == "ok"
    if os.name == "nt":
        # Windows models only the write bit: chmod(0o600) clears the
        # read-only attribute and st_mode reads back 0o666, never the
        # POSIX owner-only mode. The functional contract — writable
        # again, check green — is what the fixer promises everywhere;
        # the exact 0o600 is a POSIX detail asserted only there.
        assert os.access(log, os.W_OK)
    else:
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert _check_event_log_writable(tmp_path).status == "ok"


def test_fix_event_log_declines_symlinked_log(tmp_path: Path) -> None:
    """A symlink at `.events.jsonl` is never chmod'd through: chmod
    follows symlinks, so 'fixing' it would mutate the TARGET's
    permissions — a file that may not be ours at all (the same
    refuse-on-symlink standard `_check_stale_config_lockfiles` and
    `init._heal_stale_sidecar_lockfile` hold). The fixer declines and
    the finding stays manual; the victim keeps its mode and contents."""
    if sys.platform == "win32":
        pytest.skip("symlink semantics differ on Windows; POSIX-only test")
    from bettermemory.events import EVENT_LOG_FILENAME

    victim = tmp_path / "victim.txt"
    victim.write_text("precious target content\n", encoding="utf-8")
    victim.chmod(0o400)
    store = tmp_path / "store"
    store.mkdir()
    (store / EVENT_LOG_FILENAME).symlink_to(victim)

    diag = _check_event_log_writable(store)
    if diag.status == "ok":
        pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")
    assert diag.status == "fail"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=store)
    assert fixes == []  # declined → manual-only, with the hint
    assert stat.S_IMODE(victim.stat().st_mode) == 0o400
    assert victim.read_text(encoding="utf-8") == "precious target content\n"


def test_fix_event_log_reports_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same honesty for the event log: a denied chmod becomes a
    not-applied FixResult with the error named and the log's mode
    untouched."""
    from bettermemory.events import EVENT_LOG_FILENAME

    log = tmp_path / EVENT_LOG_FILENAME
    log.write_text("", encoding="utf-8")
    log.chmod(0o400)
    diag = _check_event_log_writable(tmp_path)
    if diag.status == "ok":
        pytest.skip("filesystem ignored chmod; cannot exercise unwritable path")
    assert diag.status == "fail"
    mode_before = stat.S_IMODE(log.stat().st_mode)

    def _deny(self: Path, mode: int, **_kwargs: Any) -> None:
        raise PermissionError(f"chmod denied on {self}")

    monkeypatch.setattr(Path, "chmod", _deny)
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert [f.action for f in fixes] == ["chmod_event_log"]
    fix = fixes[0]
    assert fix.applied is False
    assert fix.before_status == "fail"
    assert fix.after_status == "fail"
    assert "PermissionError" in (fix.error or "")
    assert fix.details["old_mode"] == oct(mode_before)
    assert stat.S_IMODE(log.stat().st_mode) == mode_before  # nothing mutated


def test_fix_index_health_rebuilds_corrupt_index(tmp_path: Path) -> None:
    """break→fix→green: a garbage .index.sqlite is dropped and rebuilt
    through `index.rebuild` — the exact function `reindex` runs."""
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="alpha indexer note", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"not a sqlite database at all " * 16)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert [f.action for f in fixes] == ["rebuild_index"]
    fix = fixes[0]
    assert fix.applied is True
    assert fix.before_status == "warn"
    assert fix.after_status == "ok"
    assert fix.details["indexed"] == 1
    assert _check_index_health(tmp_path).status == "ok"


def test_fix_index_health_rebuilds_missing_index(tmp_path: Path) -> None:
    """The after-a-sync-pull shape: memory files on disk, no index —
    the same rebuild greens it."""
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="pulled memory body", scopes=["tools"])
    index_path(tmp_path).unlink()
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert fixes[0].applied is True
    assert fixes[0].after_status == "ok"


def test_fix_index_health_reports_rebuild_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`index.rebuild` failing — either arm of the fixer's
    `(OSError, sqlite3.Error)` catch (read-only dir / ENOSPC, a SQLite
    I/O error) — becomes an honest not-applied FixResult from the
    FIXER's own branch: the action stays `rebuild_index`, not the
    `fix_index_health` the run_fixes exception wrapper would stamp."""
    import sqlite3

    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="rebuild failure body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"

    for exc in (
        OSError("read-only file system"),
        sqlite3.OperationalError("disk I/O error"),
    ):

        def _raise(*_args: Any, _exc: Exception = exc, **_kwargs: Any) -> int:
            raise _exc

        monkeypatch.setattr("bettermemory.index.rebuild", _raise)
        fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
        assert [f.action for f in fixes] == ["rebuild_index"]
        fix = fixes[0]
        assert fix.applied is False
        assert fix.before_status == "warn"
        assert fix.after_status == "warn"
        assert exc.__class__.__name__ in (fix.error or "")
        assert fix.details["path"] == str(index_path(tmp_path))


def test_fix_stale_config_lockfiles_removes_artifact_leaves_live_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """break→fix→green for the 0-byte 3.15.0 artifact, PLUS the
    boundary: a directory at `<config>.lock` (a client's own
    mkdir-style lock) and a non-empty file (some other tool's lock with
    content) are never touched — the heal re-checks the artifact shape
    at unlink time."""
    config_a = tmp_path / "a_config.json"
    config_b = tmp_path / "b_config.json"
    config_c = tmp_path / "c_config.json"
    monkeypatch.setattr(
        "bettermemory.doctor.KNOWN_CLIENTS",
        {
            "clienta": lambda: ClientPaths(
                name="clienta", description="", paths=(config_a,)
            ),
            "clientb": lambda: ClientPaths(
                name="clientb", description="", paths=(config_b,)
            ),
            "clientc": lambda: ClientPaths(
                name="clientc", description="", paths=(config_c,)
            ),
        },
    )
    artifact = config_a.with_suffix(".json.lock")
    artifact.touch()  # 0-byte regular file — the 3.15.0 artifact shape
    live_dir_lock = config_b.with_suffix(".json.lock")
    live_dir_lock.mkdir()  # the client's own mkdir-style lock
    foreign = config_c.with_suffix(".json.lock")
    foreign.write_text("pid: 1234\n", encoding="utf-8")

    diag = _check_stale_config_lockfiles()
    assert diag.status == "warn"
    assert diag.details["stale_lockfiles"] == [str(artifact)]

    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=None)
    assert [f.action for f in fixes] == ["remove_stale_lockfiles"]
    fix = fixes[0]
    assert fix.applied is True
    assert fix.after_status == "ok"
    assert fix.details["removed"] == [str(artifact)]
    assert not artifact.exists()
    assert live_dir_lock.is_dir()
    assert foreign.read_text(encoding="utf-8") == "pid: 1234\n"
    assert _check_stale_config_lockfiles().status == "ok"


def test_fix_stale_config_lockfiles_vanished_artifact_is_honest_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact vanishing between diagnosis and fix (the user
    deleted it, the client cleaned up) yields the asymmetric shape:
    applied is False — no unlink actually happened — while the re-run
    reports ok, so the exit code still heals and nothing is claimed
    that didn't happen."""
    config_a = tmp_path / "a_config.json"
    monkeypatch.setattr(
        "bettermemory.doctor.KNOWN_CLIENTS",
        {
            "clienta": lambda: ClientPaths(
                name="clienta", description="", paths=(config_a,)
            )
        },
    )
    artifact = config_a.with_suffix(".json.lock")
    artifact.touch()  # the 0-byte 3.15.0 artifact shape
    diag = _check_stale_config_lockfiles()
    assert diag.status == "warn"
    artifact.unlink()  # vanishes between diagnosis and fix

    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=None)
    assert [f.action for f in fixes] == ["remove_stale_lockfiles"]
    fix = fixes[0]
    assert fix.applied is False
    assert fix.before_status == "warn"
    assert fix.after_status == "ok"
    assert "no 0-byte lockfile artifact matched at fix time" in fix.message


@_needs_git
def test_fix_sync_gitignore_refreshes_but_never_untracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PARTIAL fix: a stale-gitignore store repo with a tracked
    sidecar gets its .gitignore refreshed to canonical (so the manual
    untrack will stick — `sync push` never refreshes it), but the git
    index is NOT touched: the check stays red and the fix says so."""
    store = _store_repo(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "captured plaintext"}\n', encoding="utf-8"
    )
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")
    assert not (store / ".gitignore").exists()  # the stalest shape

    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "fail"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=store)
    assert [f.action for f in fixes] == ["refresh_gitignore"]
    fix = fixes[0]
    assert fix.applied is True
    assert fix.after_status == "fail"  # honest: the untrack is manual
    assert "git rm --cached" in fix.message
    desired = "\n".join(sync._GITIGNORE_LINES) + "\n"
    assert (store / ".gitignore").read_text(encoding="utf-8") == desired
    assert PROPOSALS_FILENAME in _git_in(store, "ls-files")
    # With the refreshed gitignore, the manual remediation now sticks.
    _git_in(store, "rm", "--cached", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "untrack")
    assert _check_sync_tracked_ignored(store).status == "ok"


@_needs_git
def test_fix_sync_gitignore_nothing_to_apply_when_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracked sidecar behind an already-canonical gitignore has no
    auto-applicable half — the finding stays purely manual and the git
    index is untouched."""
    store = _store_repo(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text("x\n", encoding="utf-8")
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")
    sync.init(store)  # writes the canonical .gitignore
    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "fail"
    assert run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=store) == []
    assert PROPOSALS_FILENAME in _git_in(store, "ls-files")


@_needs_git
def test_fix_sync_gitignore_reports_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`atomic_write_bytes` failing (read-only store repo, ENOSPC)
    becomes an honest not-applied FixResult; no .gitignore appears and
    the git index is untouched."""
    store = _store_repo(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text("x\n", encoding="utf-8")
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")
    assert not (store / ".gitignore").exists()  # stale shape → refresh applies
    diag = _check_sync_tracked_ignored(store)
    assert diag.status == "fail"

    def _deny(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("read-only store")

    monkeypatch.setattr("bettermemory._fsutil.atomic_write_bytes", _deny)
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=store)
    assert [f.action for f in fixes] == ["refresh_gitignore"]
    fix = fixes[0]
    assert fix.applied is False
    assert fix.before_status == "fail"
    assert fix.after_status == "fail"
    assert "PermissionError" in (fix.error or "")
    assert fix.details["gitignore"] == str(store / ".gitignore")
    assert not (store / ".gitignore").exists()
    assert PROPOSALS_FILENAME in _git_in(store, "ls-files")


def test_run_fixes_leaves_manual_findings_alone(tmp_path: Path) -> None:
    """Red checks with no registered fixer contribute nothing — the
    registry IS the manual/auto boundary, so client configs, parse
    health, stranded auto-memory, parent-repo tracking etc. are never
    mutated by --fix."""
    report = DoctorReport(
        checks=[
            Diagnosis(name="mcp_client_configs", status="warn", message="stale"),
            Diagnosis(name="memory_parse_health", status="warn", message="skipped"),
            Diagnosis(name="auto_memory_stranded", status="warn", message="stranded"),
            Diagnosis(
                name="store_nested_in_parent_repo", status="warn", message="tracked"
            ),
            Diagnosis(name="embeddings_extra", status="fail", message="missing"),
        ]
    )
    assert run_fixes(report, cfg=None, directory=tmp_path) == []


def test_run_fixes_records_doctor_fix_event(tmp_path: Path) -> None:
    """One `doctor_fix` event per APPLIED fix — check name, action, and
    the before/after statuses land in the store's event log."""
    from bettermemory.events import iter_all_events
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="event audit body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert fixes[0].applied is True
    events = [e for e in iter_all_events(tmp_path) if e.get("kind") == "doctor_fix"]
    assert len(events) == 1
    event = events[0]
    assert event["check"] == "index_health"
    assert event["action"] == "rebuild_index"
    assert event["before_status"] == "warn"
    assert event["after_status"] == "ok"
    assert event["detail"]["indexed"] == 1


def test_run_fixes_telemetry_opt_out_records_nothing(tmp_path: Path) -> None:
    """`[telemetry] enabled = false` must disable doctor's audit trail
    exactly like it disables the server's: an applied fix neither
    creates `.events.jsonl` nor appends to an existing one. Before the
    cfg threading, `_record_fix_events` built a Recorder with the
    default `enabled=True` and wrote to the log the user had opted out
    of."""
    from bettermemory.config import TelemetryConfig
    from bettermemory.events import EVENT_LOG_FILENAME
    from bettermemory.index import index_path
    from bettermemory.store import Store

    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        telemetry=TelemetryConfig(enabled=False),
    )
    store = Store(tmp_path)
    store.write(content="opt-out audit body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=cfg, directory=tmp_path)
    assert fixes[0].applied is True
    assert fixes[0].after_status == "ok"
    assert not (tmp_path / EVENT_LOG_FILENAME).exists()

    # Same with a pre-existing log: nothing may be appended either.
    log = tmp_path / EVENT_LOG_FILENAME
    log.write_text("", encoding="utf-8")
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    diag2 = _check_index_health(tmp_path)
    assert diag2.status == "warn"
    fixes2 = run_fixes(DoctorReport(checks=[diag2]), cfg=cfg, directory=tmp_path)
    assert fixes2[0].applied is True
    assert log.read_bytes() == b""


def test_run_fixes_wraps_fixer_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixer that raises becomes a failed FixResult — the same
    never-take-down-the-report tolerance `_safe` gives checks."""

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(_FIXERS, "index_health", _boom)
    report = DoctorReport(
        checks=[Diagnosis(name="index_health", status="warn", message="bad")]
    )
    fixes = run_fixes(report, cfg=None, directory=tmp_path)
    assert len(fixes) == 1
    assert fixes[0].applied is False
    assert "RuntimeError" in (fixes[0].error or "")
    assert fixes[0].after_status == "warn"
    # A failed fix is rendered, never recorded: the audit trail filter
    # is `f.applied`, and nothing applied here — no doctor_fix event.
    from bettermemory.events import iter_all_events

    assert [e for e in iter_all_events(tmp_path) if e.get("kind") == "doctor_fix"] == []


def test_run_fixes_mixed_outcomes_record_only_the_applied_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reds, one fixer applies and one raises: exactly ONE
    doctor_fix event lands (the applied fix's), and the JSON payload
    carries both attempts while counting fixes_applied == 1."""
    from bettermemory.events import iter_all_events
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="mixed outcome body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    index_diag = _check_index_health(tmp_path)
    assert index_diag.status == "warn"

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(_FIXERS, "event_log", _boom)
    report = DoctorReport(
        checks=[
            index_diag,
            Diagnosis(name="event_log", status="fail", message="unwritable"),
        ]
    )
    fixes = run_fixes(report, cfg=None, directory=tmp_path)
    assert [(f.check, f.applied) for f in fixes] == [
        ("index_health", True),
        ("event_log", False),
    ]
    events = [e for e in iter_all_events(tmp_path) if e.get("kind") == "doctor_fix"]
    assert len(events) == 1
    assert events[0]["check"] == "index_health"
    parsed = json.loads(render_json(report, fixes=fixes))
    assert parsed["fixes_applied"] == 1
    assert [f["applied"] for f in parsed["fixes"]] == [True, False]
    assert "RuntimeError" in parsed["fixes"][1]["error"]


def test_run_fixes_survives_recorder_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit trail is best-effort by contract: a Recorder.record
    that raises mid-append must never fail a fix that already landed —
    run_fixes returns the applied FixResult and the store stays
    healed."""
    from bettermemory.events import Recorder
    from bettermemory.index import index_path
    from bettermemory.store import Store

    store = Store(tmp_path)
    store.write(content="recorder failure body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    diag = _check_index_health(tmp_path)
    assert diag.status == "warn"

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("event log append exploded")

    monkeypatch.setattr(Recorder, "record", _boom)
    fixes = run_fixes(DoctorReport(checks=[diag]), cfg=None, directory=tmp_path)
    assert [(f.check, f.applied) for f in fixes] == [("index_health", True)]
    assert fixes[0].after_status == "ok"
    assert _check_index_health(tmp_path).status == "ok"


def test_fix_context_config_load_failure_degrades_to_none_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that fails to load mirrors _check_config_loadable's
    tolerance: (None, None) — --fix keeps running with only the
    directory-independent fixers reachable, it never crashes."""
    from bettermemory import doctor as doctor_mod

    def _explode() -> Config:
        raise ValueError("unparseable config")

    monkeypatch.setattr(doctor_mod, "load_config", _explode)
    assert _fix_context() == (None, None)


def test_fix_context_unresolvable_directory_keeps_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable storage directory (storage_directory's first
    fail branch) degrades to (cfg, None): config-only fixers keep
    their context, directory-scoped ones see None and decline."""
    from bettermemory import doctor as doctor_mod

    cfg = _config_for(tmp_path)
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)

    def _explode(self: Config, cwd: Path | None = None) -> Path:
        raise RuntimeError("unresolvable directory")

    monkeypatch.setattr(Config, "resolved_directory", _explode)
    got_cfg, got_directory = _fix_context()
    assert got_cfg is cfg
    assert got_directory is None


def test_cli_doctor_fix_exit_code_reflects_post_fix_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A red-then-healed run exits 0 (the post-fix state) where the dry
    run exits 1 — the `doctor --fix && …` contract scripts rely on."""
    from bettermemory import doctor as doctor_mod
    from bettermemory.index import index_path
    from bettermemory.store import Store

    cfg = _config_for(tmp_path)
    store = Store(tmp_path)
    store.write(content="cli exit body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    monkeypatch.setattr(
        doctor_mod,
        "run_diagnostics",
        lambda: DoctorReport(checks=[_check_index_health(tmp_path)]),
    )
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)

    assert cli_doctor(json_out=False) == 1  # dry run: the warn stays
    capsys.readouterr()
    code = cli_doctor(json_out=False, fix=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "--fix:" in out
    assert "fixed (was warn)" in out
    assert _check_index_health(tmp_path).status == "ok"


def test_cli_doctor_fix_does_not_corrupt_cadence_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-repro shape of the self-observation bug: a store with
    ONE real in-window session, zero `turn_audited` (the "not enough
    cadence data" ok shape) and one fixable red. `--fix` heals the red
    and records its `doctor_fix` audit row under a fresh session id;
    the post re-run then reads the log doctor just appended to. Without
    the admin-kind census exclusion that row's throwaway session trips
    the ≥2-session floor, and the fully-healed run exits 1 on a cadence
    warn doctor itself manufactured."""
    from datetime import datetime, timezone

    from bettermemory import doctor as doctor_mod
    from bettermemory.index import index_path
    from bettermemory.store import Store

    cfg = _config_for(tmp_path)
    store = Store(tmp_path)
    store.write(content="cadence exit body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)  # the fixable red
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_event(tmp_path, "search", ts=now_iso, session="the-real-one")
    monkeypatch.setattr(
        doctor_mod,
        "run_diagnostics",
        lambda: DoctorReport(
            checks=[
                _check_index_health(tmp_path),
                _check_audit_turn_cadence(tmp_path),
            ]
        ),
    )
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)
    code = cli_doctor(json_out=True, fix=True)
    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["overall"] == "ok"
    assert parsed["fixes_applied"] == 1
    statuses = {c["name"]: c["status"] for c in parsed["checks"]}
    assert statuses == {"index_health": "ok", "audit_turn_cadence": "ok"}


def test_cli_doctor_fix_no_manual_contradiction_when_neighbor_heals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flagship neighbour-heal shape: an unwritable store dir with
    NO event log yet reds BOTH storage_directory and event_log, and
    only storage has an applicable fixer (the event_log fixer declines
    — there is no file to chmod). The storage chmod unblocks event-log
    creation, so the post re-run shows event_log green with no hint —
    the old pre-only manual list still named it "manual-only, see
    hints above", contradicting the green check line it sat under and
    pointing at hints that no longer exist. The tail must render it as
    healed-by-another-fix instead; the exit code stays the post
    verdict (0)."""
    from bettermemory import doctor as doctor_mod

    cfg = _config_for(tmp_path)

    def _diagnose() -> DoctorReport:
        storage_diag, _resolved = _check_storage_directory(cfg)
        return DoctorReport(checks=[storage_diag, _check_event_log_writable(tmp_path)])

    monkeypatch.setattr(doctor_mod, "run_diagnostics", _diagnose)
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)
    tmp_path.chmod(0o555)
    try:
        if _diagnose().overall == "ok":
            pytest.skip("filesystem ignored chmod; cannot exercise unwritable dir")
        code = cli_doctor(json_out=False, fix=True)
        out = capsys.readouterr().out
    finally:
        tmp_path.chmod(0o755)
    assert code == 0
    assert "manual-only" not in out
    assert "healed by another fix" in out
    assert "✓ event_log" in out  # the post check list agrees
    assert "fixed (was fail)" in out  # storage_directory's own line


@_needs_git
def test_cli_doctor_fix_mixed_outcome_keeps_post_fix_exit_and_partial_render(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An APPLIED fix must never launder a still-red report: the
    sync_tracked_ignored partial fix (the gitignore refresh lands but
    the untrack stays manual, so the check stays fail) plus one
    unfixable red exits 2 — the POST-fix verdict, never
    0-because-something-applied — and the tail renders the partial fix
    as ⚠ applied (still fail), never as ✓ fixed, with the manual
    remainder computed against POST state."""
    from bettermemory import doctor as doctor_mod

    store = _store_repo(tmp_path, monkeypatch)
    (store / PROPOSALS_FILENAME).write_text(
        '{"content": "captured plaintext"}\n', encoding="utf-8"
    )
    _git_in(store, "add", PROPOSALS_FILENAME)
    _git_in(store, "commit", "-m", "pre-fix sync commit")
    assert not (store / ".gitignore").exists()  # stale shape → refresh applies
    cfg = _config_for(store)
    unfixable = Diagnosis(
        name="embeddings_extra",
        status="fail",
        message="semantic_dedup enabled but the embeddings extra is missing",
        fix_hint="Install the embeddings extra.",
    )
    monkeypatch.setattr(
        doctor_mod,
        "run_diagnostics",
        lambda: DoctorReport(checks=[_check_sync_tracked_ignored(store), unfixable]),
    )
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)

    code = cli_doctor(json_out=False, fix=True)
    out = capsys.readouterr().out
    # Exit code is the POST-fix overall (still fail): `doctor --fix &&
    # …` must not proceed on an applied-but-unhealed store.
    assert code == 2
    # The applied-but-still-red fix renders as ⚠; only a fix whose own
    # re-run turned green may render as ✓ fixed.
    assert "⚠ sync_tracked_ignored: applied (still fail)" in out
    assert "✓ sync_tracked_ignored" not in out
    # The manual remainder reflects POST state: the unfixable red is
    # listed, the attempted check is not (its ⚠ line owns it), and
    # nothing is called healed.
    assert "manual-only finding(s), see hints above: embeddings_extra" in out
    assert "healed by another fix" not in out
    # The partial fix genuinely landed on disk.
    desired = "\n".join(sync._GITIGNORE_LINES) + "\n"
    assert (store / ".gitignore").read_text(encoding="utf-8") == desired


def test_cli_doctor_fix_json_carries_fixes_array(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bettermemory import doctor as doctor_mod
    from bettermemory.index import index_path
    from bettermemory.store import Store

    cfg = _config_for(tmp_path)
    store = Store(tmp_path)
    store.write(content="json lane body", scopes=["tools"])
    index_path(tmp_path).write_bytes(b"garbage " * 8)
    monkeypatch.setattr(
        doctor_mod,
        "run_diagnostics",
        lambda: DoctorReport(checks=[_check_index_health(tmp_path)]),
    )
    monkeypatch.setattr(doctor_mod, "load_config", lambda: cfg)
    code = cli_doctor(json_out=True, fix=True)
    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["overall"] == "ok"
    assert parsed["fixes_applied"] == 1
    (fix,) = parsed["fixes"]
    assert fix["check"] == "index_health"
    assert fix["applied"] is True
    assert fix["before_status"] == "warn"
    assert fix["after_status"] == "ok"
    assert [c["status"] for c in parsed["checks"]] == ["ok"]


def test_render_json_without_fixes_keeps_pre_fix_shape() -> None:
    """Non---fix runs must not grow a `fixes` key — existing `doctor
    --json` consumers parse the exact prior shape. A --fix run that
    applied nothing still carries the array (empty), so ITS consumers
    can branch on presence rather than sniffing."""
    report = DoctorReport(checks=[Diagnosis(name="x", status="ok", message="")])
    parsed = json.loads(render_json(report))
    assert "fixes" not in parsed
    assert "fixes_applied" not in parsed
    parsed_fix = json.loads(render_json(report, fixes=[]))
    assert parsed_fix["fixes"] == []
    assert parsed_fix["fixes_applied"] == 0


def test_render_fixes_text_partial_failed_and_manual_lines() -> None:
    """The three non-green tail shapes on one report: ⚠ for
    applied-but-still-red, ✗ for not-applied (the error string
    preferred, the message as fallback when error is None), and the
    manual-only remainder listing exactly the post-state reds nobody
    attempted."""
    pre = DoctorReport(
        checks=[
            Diagnosis(name="sync_tracked_ignored", status="fail", message="tracked"),
            Diagnosis(name="event_log", status="fail", message="unwritable"),
            Diagnosis(name="stale_config_lockfiles", status="warn", message="stale"),
            Diagnosis(name="mcp_client_configs", status="warn", message="stale path"),
        ]
    )
    post = DoctorReport(
        checks=[
            Diagnosis(name="sync_tracked_ignored", status="fail", message="tracked"),
            Diagnosis(name="event_log", status="fail", message="unwritable"),
            Diagnosis(name="stale_config_lockfiles", status="ok", message="clean"),
            Diagnosis(name="mcp_client_configs", status="warn", message="stale path"),
        ]
    )
    fixes = [
        FixResult(
            check="sync_tracked_ignored",
            action="refresh_gitignore",
            applied=True,
            before_status="fail",
            after_status="fail",
            message="gitignore refreshed; the untrack stays manual",
        ),
        FixResult(
            check="event_log",
            action="chmod_event_log",
            applied=False,
            before_status="fail",
            after_status="fail",
            message="chmod 0600 failed",
            error="PermissionError: denied",
        ),
        FixResult(
            check="stale_config_lockfiles",
            action="remove_stale_lockfiles",
            applied=False,
            before_status="warn",
            after_status="ok",
            message="no 0-byte lockfile artifact matched at fix time",
        ),
    ]
    out = render_fixes_text(fixes, pre, post)
    assert (
        "⚠ sync_tracked_ignored: applied (still fail) — "
        "gitignore refreshed; the untrack stays manual" in out
    )
    # ✗ prefers the error string…
    assert "✗ event_log: not applied — PermissionError: denied" in out
    # …and falls back to the message when error is None.
    assert (
        "✗ stale_config_lockfiles: not applied — "
        "no 0-byte lockfile artifact matched at fix time" in out
    )
    # The manual remainder is POST state: only the untouched red.
    assert "manual-only finding(s), see hints above: mcp_client_configs" in out
    assert "fixed (was" not in out  # nothing may masquerade as ✓ fixed


def test_cli_doctor_fix_noop_says_so(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op contract: an all-green run applies nothing and says
    so, exit 0."""
    from bettermemory import doctor as doctor_mod

    fake = DoctorReport(checks=[Diagnosis(name="x", status="ok", message="")])
    monkeypatch.setattr(doctor_mod, "run_diagnostics", lambda: fake)
    monkeypatch.setattr(doctor_mod, "_fix_context", lambda: (None, None))
    code = cli_doctor(json_out=False, fix=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to fix" in out


def test_cli_doctor_fix_all_manual_says_so(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red findings that are all manual-only: --fix applies nothing,
    says so, and the exit code keeps the pre-fix verdict."""
    from bettermemory import doctor as doctor_mod

    fake = DoctorReport(
        checks=[Diagnosis(name="mcp_client_configs", status="warn", message="stale")]
    )
    monkeypatch.setattr(doctor_mod, "run_diagnostics", lambda: fake)
    monkeypatch.setattr(doctor_mod, "_fix_context", lambda: (None, None))
    code = cli_doctor(json_out=False, fix=True)
    out = capsys.readouterr().out
    assert code == 1
    assert "no auto-fixable findings" in out


# Hardcoded alphabetised, NOT derived from `_FIXERS` — derivation would
# silently shrink the expectation when an entry is deleted, defeating the
# deletion guard. Same shape as `_EXPECTED_CHECK_STATUSES` above.
_EXPECTED_FIXABLE_CHECKS: tuple[str, ...] = (
    "event_log",
    "index_health",
    "stale_config_lockfiles",
    "storage_directory",
    "sync_tracked_ignored",
)


def test_fixers_registry_matches_expected_fixable_checks() -> None:
    """Pin `_FIXERS` membership both ways. A new auto-fixable check is
    a deliberate safety decision (idempotent + reversible +
    target-regenerable, per the doctor module docstring); this pin
    makes it a REVIEWED one rather than a drive-by registry insert —
    and a deleted fixer trips it too."""
    assert set(_FIXERS) == set(_EXPECTED_FIXABLE_CHECKS)


def test_cli_doctor_subparser_wires_fix_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor --fix` parses, defaults to False, and dispatches
    `fix=True` through to `cli_doctor`."""
    import argparse

    from bettermemory.cli import doctor as cli_doctor_mod

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli_doctor_mod.add_subparser(sub)
    assert parser.parse_args(["doctor"]).fix is False
    args = parser.parse_args(["doctor", "--fix", "--json"])
    assert args.fix is True
    assert args.json is True

    seen: dict[str, Any] = {}

    def _fake_cli_doctor(*, json_out: bool, fix: bool = False) -> int:
        seen.update(json_out=json_out, fix=fix)
        return 0

    monkeypatch.setattr("bettermemory.doctor.cli_doctor", _fake_cli_doctor)
    with pytest.raises(SystemExit) as excinfo:
        cli_doctor_mod.run(args)
    assert excinfo.value.code == 0
    assert seen == {"json_out": True, "fix": True}
