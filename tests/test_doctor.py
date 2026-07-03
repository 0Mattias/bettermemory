"""Tests for `bettermemory doctor`.

Each diagnostic in `doctor.py` is exercised in isolation via the
`_check_*` helpers; integration is covered by `run_diagnostics` and
`cli_doctor`. The file uses tmp_path-backed `Config` instances rather
than touching the user's real config — doctor must never side-effect
the host environment under test.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.doctor import (
    CheckStatus,
    Diagnosis,
    DoctorReport,
    _binary_dist_version,
    _check_audit_turn_cadence,
    _check_binary_on_path,
    _check_config_loadable,
    _check_distinfo_metadata,
    _check_embeddings_extra,
    _check_event_log_writable,
    _check_index_health,
    _check_mcp_client_configs,
    _check_memory_parse_health,
    _check_python_version,
    _check_storage_directory,
    _discover_site_packages,
    _probe_index_integrity,
    _EXIT_CODE_BY_STATUS,
    _STATUS_GLYPH,
    cli_doctor,
    render_json,
    render_text,
    run_diagnostics,
)
from bettermemory.init import ClientPaths


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
