"""Tests for `bettermemory.config` — TOML coercion and directory resolution.

`config.py` had been exercised only indirectly via server integration tests.
Two gaps the integration coverage doesn't close:

1. **Field-level type coercion** through `load_config`. Each field has its
   own `bool(...)` / `int(...)` / `float(...)` / `str(...)` wrapper around
   `behavior_raw.get(...)`. If someone reorders one and accidentally drops
   the coercion, integration tests with well-typed TOML wouldn't notice.
2. **`Config.resolved_directory` decision tree**: env override → explicit
   `[storage] directory` → project-scoped `./.claude-memory/` → global
   `~/.claude-memory/`. The integration tests pass `StorageConfig(directory=...)`
   directly, which short-circuits past the interesting branches.

Tests use `tmp_path` for hermeticity and `monkeypatch` to scope env-var and
`Path.cwd()` overrides without leaking to other tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from bettermemory.config import (
    DEFAULT_CONFIG,
    ENV_DIR_OVERRIDE,
    Config,
    StorageConfig,
    load_config,
)


def _set_fake_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Cross-platform `~` redirect.

    `Path.home()` and `Path.expanduser()` consult different env vars per
    platform: POSIX reads `HOME`; Windows reads `USERPROFILE` first, then
    falls back to `HOMEDRIVE` + `HOMEPATH`. Setting only `HOME` works on
    Linux and macOS but is a no-op on Windows — `~` still expands to the
    real `C:\\Users\\runneradmin`, which is why the CI Windows jobs were
    hitting assertion failures against the runner's actual home.

    Set all three so the redirect is hermetic on every supported runner,
    and clear `HOMEDRIVE`/`HOMEPATH` to avoid the documented Windows
    fallback override when `USERPROFILE` is somehow ignored.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG sanity
# ---------------------------------------------------------------------------


def test_default_config_is_valid_toml() -> None:
    """The DEFAULT_CONFIG string is shipped to first-run users verbatim.

    A typo there would produce a `tomllib.TOMLDecodeError` on every first
    start until a human edited the file by hand. The string is small enough
    that a simple "does it parse" check is the right defense.
    """
    parsed = tomllib.loads(DEFAULT_CONFIG)
    # Spot-check that the documented sections survived the parse — guards
    # against a future edit that removes a section header by accident
    # while keeping the file syntactically valid.
    assert "behavior" in parsed
    assert "storage" in parsed
    assert "scopes" in parsed
    assert "telemetry" in parsed


def test_default_config_round_trips_through_load_config(tmp_path: Path) -> None:
    """Writing DEFAULT_CONFIG and loading it yields the same defaults as
    constructing `Config()` from scratch. Closes the loop on the
    first-run experience: a user who never edits the config file gets
    exactly the dataclass defaults."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")

    loaded = load_config(config_path)
    fresh = Config()

    assert (
        loaded.behavior.require_write_confirmation
        == fresh.behavior.require_write_confirmation
    )
    assert loaded.behavior.default_max_results == fresh.behavior.default_max_results
    assert (
        loaded.behavior.recency_boost_half_life_days
        == fresh.behavior.recency_boost_half_life_days
    )
    assert loaded.behavior.semantic_dedup == fresh.behavior.semantic_dedup
    assert loaded.behavior.semantic_model_name == fresh.behavior.semantic_model_name
    assert (
        loaded.behavior.semantic_high_threshold
        == fresh.behavior.semantic_high_threshold
    )
    assert (
        loaded.behavior.semantic_medium_threshold
        == fresh.behavior.semantic_medium_threshold
    )
    assert (
        loaded.behavior.heavily_used_min_applied
        == fresh.behavior.heavily_used_min_applied
    )
    assert (
        loaded.behavior.cold_endorsement_ratio_threshold
        == fresh.behavior.cold_endorsement_ratio_threshold
    )
    assert (
        loaded.behavior.tombstone_retention_days
        == fresh.behavior.tombstone_retention_days
    )
    assert (
        loaded.behavior.verification_stale_days
        == fresh.behavior.verification_stale_days
    )
    # Fields added after the original round-trip pin. Each one has its
    # own coercion call in `load_config` (search_mode/semantic_provider/
    # semantic_model_fastembed go through `str(...)`, max_content_bytes
    # through `int(...)`, log_queries_verbatim through `bool(...)`); a
    # silent drop or reordering that changed the coercion would survive
    # the field-level coercion tests above but break the round-trip
    # equality with `Config()` defaults that this test pins.
    assert loaded.behavior.search_mode == fresh.behavior.search_mode
    assert loaded.behavior.semantic_provider == fresh.behavior.semantic_provider
    assert (
        loaded.behavior.semantic_model_fastembed
        == fresh.behavior.semantic_model_fastembed
    )
    assert loaded.behavior.max_content_bytes == fresh.behavior.max_content_bytes
    assert loaded.behavior.max_takeaway_bytes == fresh.behavior.max_takeaway_bytes
    # Document the takeaway cap default — the absolute number matters
    # for the bug class this field closes (a takeaway > 64 KB corrupts
    # the YAML frontmatter; default must stay well under that).
    assert loaded.behavior.max_takeaway_bytes == 4_096
    assert (
        loaded.behavior.curation_hint_threshold
        == fresh.behavior.curation_hint_threshold
    )
    assert loaded.behavior.curation_hint_enabled == fresh.behavior.curation_hint_enabled
    assert loaded.telemetry.log_queries_verbatim == fresh.telemetry.log_queries_verbatim
    assert loaded.scopes.allowed == fresh.scopes.allowed
    assert loaded.telemetry.enabled == fresh.telemetry.enabled
    assert loaded.telemetry.max_bytes == fresh.telemetry.max_bytes


# ---------------------------------------------------------------------------
# load_config: field-level coercion
# ---------------------------------------------------------------------------


def test_load_config_creates_file_when_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """First run with no config writes DEFAULT_CONFIG to disk and reports it."""
    config_path = tmp_path / "config.toml"
    assert not config_path.exists()

    load_config(config_path)

    assert config_path.exists()
    # The "[bettermemory] created default config at ..." notice goes to
    # stderr — capture and assert so a future change that swallows the
    # notice doesn't slip past.
    captured = capsys.readouterr()
    assert "created default config" in captured.err
    assert str(config_path) in captured.err


def test_load_config_reads_storage_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[storage]\ndirectory = "/tmp/explicit-path"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.storage.directory == "/tmp/explicit-path"


def test_load_config_storage_directory_is_none_when_unset(tmp_path: Path) -> None:
    """Empty `[storage]` section leaves `directory` None so the resolution
    rule fires. The dataclass default is None; this guards against a
    future change that silently substitutes a string default."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[storage]\n", encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.storage.directory is None


def test_load_config_coerces_behavior_int_fields(tmp_path: Path) -> None:
    """Integer-typed fields go through `int(...)`. A TOML float would otherwise
    survive as a float and silently round at use-site."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\n"
        "default_max_results = 7\n"
        "heavily_used_min_applied = 5\n"
        "tombstone_retention_days = 365\n"
        "verification_stale_days = 14\n"
        "max_takeaway_bytes = 8192\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.default_max_results == 7
    assert isinstance(cfg.behavior.default_max_results, int)
    assert cfg.behavior.heavily_used_min_applied == 5
    assert cfg.behavior.tombstone_retention_days == 365
    assert cfg.behavior.verification_stale_days == 14
    assert cfg.behavior.max_takeaway_bytes == 8192
    assert isinstance(cfg.behavior.max_takeaway_bytes, int)


def test_load_config_coerces_behavior_float_fields(tmp_path: Path) -> None:
    """Half-life and the two semantic thresholds are floats. A TOML
    integer (`30`) is still accepted and coerced via `float(...)`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\n"
        "recency_boost_half_life_days = 14\n"  # integer — must coerce to float
        "semantic_high_threshold = 0.9\n"
        "semantic_medium_threshold = 0.5\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.recency_boost_half_life_days == 14.0
    assert isinstance(cfg.behavior.recency_boost_half_life_days, float)
    assert cfg.behavior.semantic_high_threshold == 0.9
    assert cfg.behavior.semantic_medium_threshold == 0.5


def test_load_config_coerces_behavior_bool_fields(tmp_path: Path) -> None:
    """`bool(...)` wraps the lookup so a missing field defaults False/True
    via the dataclass without crashing, and an explicit value is coerced."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nrequire_write_confirmation = true\nsemantic_dedup = true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.require_write_confirmation is True
    assert cfg.behavior.semantic_dedup is True


def test_load_config_coerces_behavior_str_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[behavior]\nsemantic_model_name = "all-mpnet-base-v2"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.semantic_model_name == "all-mpnet-base-v2"


def test_load_config_reads_scopes_allowed(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[scopes]\nallowed = ["tools", "infrastructure", "projects:foo"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.scopes.allowed == ["tools", "infrastructure", "projects:foo"]


def test_load_config_reads_telemetry(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[telemetry]\nenabled = false\nmax_bytes = 5000000\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.telemetry.enabled is False
    assert cfg.telemetry.max_bytes == 5_000_000


def test_load_config_missing_sections_use_defaults(tmp_path: Path) -> None:
    """A config file with only one section still loads — the rest fall back
    to dataclass defaults. Important for partial overrides ("I only care
    about flipping semantic_dedup")."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nsemantic_dedup = true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.semantic_dedup is True
    # Untouched fields keep their dataclass defaults.
    assert cfg.behavior.default_max_results == 5
    assert cfg.behavior.require_write_confirmation is False
    assert cfg.scopes.allowed == []
    assert cfg.telemetry.enabled is True


# ---------------------------------------------------------------------------
# T9: back-compat for the 3.1.x -> 3.2.0 `endorsement_debt_ratio_threshold`
# -> `cold_endorsement_ratio_threshold` rename. 3.2.0 (commit 7346ecc)
# renamed the key with no alias; a user upgrading from 3.1.x with the old
# key in their TOML would silently lose the threshold (fall back to 0.0).
# The shim accepts the old key, maps its value to the new field, and emits
# a one-shot deprecation warning naming both keys. If both are present the
# new key wins and a stronger warning fires.
# ---------------------------------------------------------------------------


def _reset_deprecated_key_guard() -> None:
    """Clear the module-level one-shot guard so each test sees a fresh
    warning state. Mirrors the `_DIVERGENCE_WARNED_ROOTS.discard(...)`
    pattern in test_index.py's once-per-root divergence tests."""
    from bettermemory import config as _cfg

    _cfg._DEPRECATED_KEY_WARNED_PATHS.clear()


def test_load_config_legacy_endorsement_debt_key_migrates_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Old-only: the legacy `endorsement_debt_ratio_threshold` key
    populates the new `cold_endorsement_ratio_threshold` field. Pins the
    actual data path the shim closes — a 3.1.x user's `0.15` survives
    the upgrade instead of silently reverting to the 0.0 default."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.15\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.15
    # Deprecation warning fired and named BOTH keys plus the resolved path,
    # so the operator has everything they need to fix their TOML without
    # grepping changelogs.
    deprecation_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one deprecation warning, got "
        f"{[r.getMessage() for r in deprecation_records]}"
    )
    message = deprecation_records[0].getMessage()
    assert "cold_endorsement_ratio_threshold" in message, (
        f"warning must name the new key, got: {message!r}"
    )
    assert "3.2.0" in message, (
        f"warning must name the release boundary, got: {message!r}"
    )


def test_load_config_new_key_only_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """New-only (the post-3.2.0 happy path): no deprecation warning fires.
    Locks the silence — the shim must not nag users who already migrated
    or who are on a fresh install."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\ncold_endorsement_ratio_threshold = 0.2\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.2
    assert not any(
        "endorsement_debt_ratio_threshold" in r.getMessage() for r in caplog.records
    ), (
        "no deprecation warning should fire when only the new key is "
        f"present, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_load_config_both_keys_new_wins_with_stronger_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both keys present: the new key wins (the user clearly added it
    explicitly; the old one is stale config). A STRONGER warning fires
    telling the user to delete the old key — distinguishing this case
    from the silent old-only migration so the user gets the right
    instruction."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\n"
        "endorsement_debt_ratio_threshold = 0.99\n"  # stale value
        "cold_endorsement_ratio_threshold = 0.25\n",  # the intent
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    # New-key value wins, NOT the legacy 0.99.
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.25
    both_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
    ]
    assert len(both_records) == 1, (
        f"expected exactly one both-keys warning, got "
        f"{[r.getMessage() for r in both_records]}"
    )
    message = both_records[0].getMessage()
    # The both-keys warning must steer the user to DELETE the old key
    # (not rename it — they already have the new one). The word "BOTH"
    # also distinguishes this from the old-only migration warning at
    # triage time.
    assert "BOTH" in message, (
        f"both-keys warning should call out the duplicate clearly, got: {message!r}"
    )
    assert "Delete" in message or "delete" in message, (
        f"both-keys warning should instruct deletion, not rename, got: {message!r}"
    )


def test_load_config_neither_key_uses_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither key present: dataclass default applies and no warning fires.
    The fresh-install / minimal-config path stays quiet."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    # A behavior section that touches neither key — proves the shim
    # doesn't fire on the absent-key path. The falsy-but-present case
    # (explicit `endorsement_debt_ratio_threshold = 0.0`) is covered by
    # `test_load_config_falsy_old_key_emits_warning` below, which pins
    # the "presence triggers, value doesn't matter" contract.
    config_path.write_text(
        "[behavior]\nsemantic_dedup = false\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.0  # dataclass default
    assert not any(
        "endorsement_debt_ratio_threshold" in r.getMessage() for r in caplog.records
    ), (
        "no deprecation warning should fire when neither key is present, "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


def test_load_config_falsy_old_key_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Old key explicitly set to a FALSY value (0.0) still triggers the
    deprecation warning and still migrates to the new field. Pins the
    "presence triggers, value doesn't matter" contract: the shim keys
    off `old_key in behavior_raw` (see `_apply_legacy_endorsement_debt_alias`),
    not off the value's truthiness, so a 3.1.x user who explicitly
    disabled the threshold by writing `endorsement_debt_ratio_threshold
    = 0.0` still sees the migration nudge."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.0\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    # The falsy value migrated to the new field — the shim didn't skip
    # the assignment on the basis of `if value:` or similar.
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.0
    deprecation_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one deprecation warning for falsy-old-key, got "
        f"{[r.getMessage() for r in deprecation_records]}"
    )
    message = deprecation_records[0].getMessage()
    assert "cold_endorsement_ratio_threshold" in message, (
        f"warning must name the new key, got: {message!r}"
    )
    assert "3.2.0" in message, (
        f"warning must name the release boundary, got: {message!r}"
    )


def test_load_config_legacy_key_deprecation_warning_is_one_shot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One-shot per (config_path, key): three loads of the same diverged
    config emit ONE warning, not three. Mirrors the
    `test_divergence_warning_fires_only_once_per_root` guard in
    test_index.py. Otherwise a long-lived server (`bettermemory serve`)
    that rereads its config on signal would spam the log on every
    reload."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.1\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        load_config(config_path)
        load_config(config_path)
        load_config(config_path)
    deprecation_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one warning across three loads of the same "
        f"config, got {[r.getMessage() for r in deprecation_records]}"
    )


# ---------------------------------------------------------------------------
# Config.resolved_directory: the resolution decision tree
# ---------------------------------------------------------------------------


def test_resolved_directory_env_var_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`BETTERMEMORY_DIR` env var takes priority over everything else.

    The env override is the documented escape hatch for tests/sandboxes;
    if it didn't beat `[storage] directory` we couldn't isolate test
    runs from a user's real config.
    """
    override_target = tmp_path / "env-target"
    override_target.mkdir()
    monkeypatch.setenv(ENV_DIR_OVERRIDE, str(override_target))

    cfg = Config(storage=StorageConfig(directory="/tmp/should-be-ignored"))
    assert cfg.resolved_directory() == override_target.resolve()


def test_resolved_directory_env_var_expands_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` in the env override expands via `Path.expanduser()` — otherwise
    it would be taken as a literal directory name and create a stray
    `./~` on the user's machine."""
    _set_fake_home(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_DIR_OVERRIDE, "~/from-env")

    cfg = Config()
    result = cfg.resolved_directory()
    assert result == (tmp_path / "from-env").resolve()


def test_resolved_directory_explicit_storage_directory_beats_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no env override is set, an explicit `[storage] directory`
    beats both the project-scoped and global fallbacks."""
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    explicit = tmp_path / "explicit"
    explicit.mkdir()

    cfg = Config(storage=StorageConfig(directory=str(explicit)))
    assert cfg.resolved_directory() == explicit.resolve()


def test_resolved_directory_project_scoped_wins_over_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `./.claude-memory/` exists in cwd, it wins over `~/.claude-memory/`.

    This is the load-bearing UX rule for project-scoped memory: drop a
    `.claude-memory/` into a repo and bettermemory writes there for any
    invocation rooted in that repo, no env-var or config edit needed.
    """
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _set_fake_home(monkeypatch, fake_home)

    project = tmp_path / "project"
    project.mkdir()
    project_memory = project / ".claude-memory"
    project_memory.mkdir()

    cfg = Config()  # no explicit storage.directory
    assert cfg.resolved_directory(cwd=project) == project_memory.resolve()


def test_resolved_directory_falls_back_to_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var, no explicit directory, no project-scoped dir: fall back
    to `~/.claude-memory/`. The default for fresh installs."""
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _set_fake_home(monkeypatch, fake_home)
    cwd = tmp_path / "cwd"
    cwd.mkdir()  # no .claude-memory subdir

    cfg = Config()
    assert cfg.resolved_directory(cwd=cwd) == (fake_home / ".claude-memory").resolve()


def test_resolved_directory_ignores_project_dir_that_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.claude-memory` file (not a directory) in cwd doesn't trigger the
    project-scoped branch — we fall through to the global default. Hostile
    case: a user accidentally created the entry as a file; we shouldn't
    explode trying to use it as a memory root."""
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _set_fake_home(monkeypatch, fake_home)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude-memory").write_text("not a directory", encoding="utf-8")

    cfg = Config()
    assert cfg.resolved_directory(cwd=cwd) == (fake_home / ".claude-memory").resolve()


def test_resolved_directory_when_cwd_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `Path.cwd()` raises FileNotFoundError because the process's working
    directory was deleted (a Stop-hook reality: user `rm -rf`s the dir they
    were working in before the turn ends), fall through to the global default
    instead of letting the exception escape. The hook would otherwise leak
    `[Errno 2] No such file or directory` to stderr and Claude Code surfaces
    that as a turn-end error banner.
    """
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _set_fake_home(monkeypatch, fake_home)

    def _boom() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_boom))

    cfg = Config()
    assert cfg.resolved_directory() == (fake_home / ".claude-memory").resolve()


# ---------------------------------------------------------------------------
# System-directory footgun warning (F-C1)
# ---------------------------------------------------------------------------


def test_resolved_directory_warns_on_system_dir_via_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setting `BETTERMEMORY_DIR=/etc/bettermemory` is almost always a
    misconfiguration (someone meant a relative path or a typo expanded
    against the wrong base). We still honour the value — there are
    legitimate ops-managed prefixes we can't predict — but we log a
    warning so the misconfiguration shows up in logs rather than
    silently scattering markdown files under /etc."""
    import sys

    if sys.platform == "win32":
        pytest.skip("system dir prefixes are POSIX-specific")

    monkeypatch.setenv(ENV_DIR_OVERRIDE, "/etc/bettermemory")
    caplog.set_level("WARNING", logger="bettermemory.config")

    cfg = Config()
    # Resolve macOS symlinks for the prefix-match (Path("/etc") becomes
    # `/private/etc` on macOS); the warning logic resolves prefixes too,
    # so the load-bearing assertion is that the warning fired and named
    # the source — not the literal string form of the path.
    cfg.resolved_directory()
    assert any(
        "system directory" in record.message and ENV_DIR_OVERRIDE in record.message
        for record in caplog.records
    ), f"no system-dir warning; got: {[r.message for r in caplog.records]}"


def test_resolved_directory_no_warn_for_user_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """User-writable paths must not trigger the warning. Lock the
    headroom so a future tightening of the prefix list doesn't start
    catching `/Users/...` / `/home/...` paths."""
    monkeypatch.setenv(ENV_DIR_OVERRIDE, str(tmp_path / "mem"))
    caplog.set_level("WARNING", logger="bettermemory.config")

    cfg = Config()
    cfg.resolved_directory()
    assert not any("system directory" in record.message for record in caplog.records), (
        f"unexpected warning on user path: {[r.message for r in caplog.records]}"
    )
