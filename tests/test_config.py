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
        loaded.behavior.tombstone_retention_days
        == fresh.behavior.tombstone_retention_days
    )
    assert (
        loaded.behavior.verification_stale_days
        == fresh.behavior.verification_stale_days
    )
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
        "verification_stale_days = 14\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.default_max_results == 7
    assert isinstance(cfg.behavior.default_max_results, int)
    assert cfg.behavior.heavily_used_min_applied == 5
    assert cfg.behavior.tombstone_retention_days == 365
    assert cfg.behavior.verification_stale_days == 14


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
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(fake_home))

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
    monkeypatch.setenv("HOME", str(fake_home))
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
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude-memory").write_text("not a directory", encoding="utf-8")

    cfg = Config()
    assert cfg.resolved_directory(cwd=cwd) == (fake_home / ".claude-memory").resolve()
