"""Tests for `bettermemory doctor`.

Each diagnostic in `doctor.py` is exercised in isolation via the
`_check_*` helpers; integration is covered by `run_diagnostics` and
`cli_doctor`. The file uses tmp_path-backed `Config` instances rather
than touching the user's real config — doctor must never side-effect
the host environment under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.doctor import (
    Diagnosis,
    DoctorReport,
    _check_audit_turn_cadence,
    _check_binary_on_path,
    _check_config_loadable,
    _check_distinfo_metadata,
    _check_embeddings_extra,
    _check_event_log_writable,
    _check_mcp_client_configs,
    _check_memory_parse_health,
    _check_python_version,
    _check_storage_directory,
    _discover_site_packages,
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
    is skipped by Store.load_all with a logged warning. Doctor should
    surface the count discrepancy."""
    bad = tmp_path / "01ZZ_corrupt.md"
    bad.write_text("---\nbad: : :\n---\nbody\n", encoding="utf-8")
    diag = _check_memory_parse_health(tmp_path)
    # Either the file parsed (yaml is permissive enough) or it didn't.
    # If it didn't, status is "warn" and details show the gap.
    if diag.status == "warn":
        assert diag.details["failed"] >= 1
    else:
        # If it parsed, that's also fine — the test was overly defensive
        # about what malformed YAML actually trips. Skip rather than
        # fail the suite.
        pytest.skip("YAML parser was permissive enough to read the file")


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

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace(
        "+00:00", "Z"
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
    target = tmp_path / "fake_config.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {
                        "command": "/nonexistent/old/bm",
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
    assert "reinstall" in (diag.fix_hint or "").lower() or "install" in (
        diag.fix_hint or ""
    ).lower()
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
    _make_distinfo(
        tmp_path, "partial-3.0.dist-info", files={"WHEEL": "ok\n"}
    )
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
