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
3. **Shipped-prose parity.** `DEFAULT_CONFIG`'s comments are written verbatim
   into every user's `config.toml`, so each claim in them is published
   documentation rather than an internal note. The prose tests assert a claim
   and the behaviour it describes in the same test — pinning either alone is
   what let the pair drift apart before.

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
    BehaviorConfig,
    Config,
    StorageConfig,
    _SEARCH_MODES,
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


async def test_default_config_scopes_prose_matches_the_update_surface(
    tmp_path: Path,
) -> None:
    """`memory_update` checks what an edit ADDS, and the comment says so.

    Its `scopes` argument REPLACES the stored list, so keeping a scope
    means resubmitting it. Checking the whole submitted list against
    `allowed` therefore refused a re-tag of any row carrying a scope the
    operator never typed (an imported row resubmits its provenance scope
    and type tag) and left no way to add a sanctioned scope without
    dropping the provenance stamp. The check now runs over the delta
    (handlers/update.py), which needs no list of a tool's tag names to
    stay correct.

    Three halves, so neither the rule nor the comment can drift alone:
    the permissive half fails if the delta rule is reverted to a
    whole-list check, the restrictive half fails if the exemption is
    widened into "anything on a re-tag passes", and the prose half fails
    if the comment stops scoping the exemption to what an edit adds.

    The stamps stand in for scopes a tool put on the record that the
    operator never typed (an importer's provenance scope and type tag);
    they are written straight into the store, past the allowlist.
    """
    from bettermemory.config import ScopesConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    from ._mcp import call_tool

    stamped = ["imported-from-claude-code", "type:project"]
    memory = Store(tmp_path).write(
        content="the demo project pins its formatter version in CI",
        scopes=[*stamped, "projects:demo"],
    )
    config = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        scopes=ScopesConfig(allowed=["projects:demo", "tools"]),
    )
    server = build_server(config=config, store=Store(tmp_path), state=SessionState())

    # PERMISSIVE HALF. Adding an ALLOWLISTED scope to an imported row: the
    # full list goes back, stamps and all. The stamps are carried, not
    # added, so they are not re-checked and the edit lands.
    await call_tool(
        server,
        "memory_update",
        {"id": memory.id, "scopes": [*stamped, "projects:demo", "tools"]},
    )
    assert sorted(Store(tmp_path).load_one(memory.id).scopes) == sorted(
        [*stamped, "projects:demo", "tools"]
    )

    # RESTRICTIVE HALF. A scope that is NOT already on the record is still
    # checked by name, so the exemption cannot be borrowed to plant one.
    with pytest.raises(Exception, match="not in allowed list") as excinfo:
        await call_tool(
            server,
            "memory_update",
            {"id": memory.id, "scopes": [*stamped, "projects:demo", "smuggled"]},
        )
    assert "smuggled" in str(excinfo.value), excinfo.value
    for stamp in stamped:
        assert stamp not in str(excinfo.value), excinfo.value

    block = DEFAULT_CONFIG.split("[scopes]")[1].split("[telemetry]")[0]
    assert "memory_update" in block, block
    assert "REPLACES" in block, block
    assert "ADDS" in block, block


def test_default_config_round_trips_through_load_config(tmp_path: Path) -> None:
    """Writing DEFAULT_CONFIG and loading it yields the same defaults as
    constructing `Config()` from scratch. Closes the loop on the
    first-run experience: a user who never edits the config file gets
    exactly the dataclass defaults. Since 9.0.0 there is no exception:
    the loader applies no deployment policy of its own."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")

    loaded = load_config(config_path)
    fresh = Config()

    assert loaded.behavior.default_max_results == fresh.behavior.default_max_results
    assert (
        loaded.behavior.recency_boost_half_life_days
        == fresh.behavior.recency_boost_half_life_days
    )
    assert loaded.behavior.prompt_recall == fresh.behavior.prompt_recall
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
    # own coercion call in `load_config` (search_mode goes through its
    # normaliser, max_content_bytes through `int(...)`); a
    # silent drop or reordering that changed the coercion would survive
    # the field-level coercion tests above but break the round-trip
    # equality with `Config()` defaults that this test pins.
    assert loaded.behavior.search_mode == fresh.behavior.search_mode
    assert loaded.behavior.max_content_bytes == fresh.behavior.max_content_bytes
    assert loaded.behavior.max_takeaway_bytes == fresh.behavior.max_takeaway_bytes
    # Document the takeaway cap default — the absolute number matters
    # for the bug class this field closes (a takeaway > 64 KB corrupts
    # the YAML frontmatter; default must stay well under that).
    assert loaded.behavior.max_takeaway_bytes == 4_096
    assert loaded.behavior.conversational == fresh.behavior.conversational
    assert loaded.behavior.write_supersession == fresh.behavior.write_supersession
    assert loaded.behavior.recall_in_project == fresh.behavior.recall_in_project
    assert loaded.behavior.min_content_tokens == fresh.behavior.min_content_tokens
    assert loaded.scopes.allowed == fresh.scopes.allowed
    assert loaded.telemetry.enabled == fresh.telemetry.enabled


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


def test_load_config_rejects_non_string_storage_directory(tmp_path: Path) -> None:
    """A mistyped `[storage] directory` (int, bool, list) must fail inside
    `load_config` with the located `_malformed_config_msg` error, like
    every other scalar key. It used to load cleanly and first blow up in
    `Config.resolved_directory()` at `Path(self.storage.directory)` with
    a bare stdlib TypeError naming neither the key nor the file — which
    crashed `bettermemory serve` startup opaquely and made the Stop-hook
    audit lane silently no-op (its broad except prints to stderr and
    exits 0)."""
    for name, toml_value in [
        ("int", "123"),
        ("bool", "true"),
        ("list", '["/a"]'),
    ]:
        config_path = tmp_path / f"config-{name}.toml"
        config_path.write_text(
            f"[storage]\ndirectory = {toml_value}\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match=r"malformed config") as excinfo:
            load_config(config_path)
        # The message must locate the culprit: section/key, offending
        # value, expected type, and the file path.
        message = str(excinfo.value)
        assert "[storage] directory" in message
        assert "must be a string path" in message
        assert str(config_path) in message


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
    """Half-life is a float. A TOML integer (`14`) is still accepted
    and coerced via `float(...)`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nrecency_boost_half_life_days = 14\n",  # integer — must coerce
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.recency_boost_half_life_days == 14.0
    assert isinstance(cfg.behavior.recency_boost_half_life_days, float)


def test_load_config_coerces_behavior_bool_fields(tmp_path: Path) -> None:
    """`bool(...)` wraps the lookup so a missing field defaults True via
    the dataclass without crashing, and an explicit value is coerced.
    Every remaining `[behavior]` bool is default-TRUE, so the explicit-
    false round-trip is the direction that proves each override reaches
    the loader (a default-true knob that ignored its TOML value would
    pass every default-shaped test in this file)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nprompt_recall = false\nrecall_in_project = false\n"
        "conversational = false\nwrite_supersession = false\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall is False
    assert cfg.behavior.recall_in_project is False
    assert cfg.behavior.conversational is False
    assert cfg.behavior.write_supersession is False


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
    config_path.write_text("[telemetry]\nenabled = false\n", encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.telemetry.enabled is False


def test_load_config_quoted_false_bool_keeps_the_opt_out(tmp_path: Path) -> None:
    """A QUOTED bool ("false") is the string "false", and `bool("false")`
    is True — a naive coercion would silently flip a default-true knob
    back ON when the user wrote "false" to opt out. The string-aware
    coercion must keep these False, in every section, case-insensitively."""
    config_path = tmp_path / "config.toml"

    config_path.write_text(
        "[behavior]\n"
        'conversational = "false"\n'
        'write_supersession = "off"\n'
        'recall_in_project = "0"\n'
        "[telemetry]\n"
        'enabled = "False"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.conversational is False
    assert cfg.behavior.write_supersession is False
    assert cfg.behavior.recall_in_project is False
    assert cfg.telemetry.enabled is False

    # Quoted truthy spellings still coerce to True (case-insensitive).
    config_path.write_text(
        '[behavior]\nprompt_recall = "TRUE"\n[telemetry]\nenabled = "on"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall is True
    assert cfg.telemetry.enabled is True


def test_load_config_unrecognized_bool_string_falls_back_to_default(
    tmp_path: Path,
) -> None:
    """An unrecognized string falls back to the FIELD DEFAULT, not to
    truthiness: a garbage value must land on the default rather than
    `bool(non_empty_str) == True`. Every shipped bool key defaults True,
    so the False-default direction is pinned on the coercion helper."""
    from bettermemory.config import _coerce_bool

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[telemetry]\nenabled = "maybe"\n[behavior]\nconversational = "sometimes"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.telemetry.enabled is True
    assert cfg.behavior.conversational is True
    assert _coerce_bool("maybe", False) is False
    assert _coerce_bool("maybe", True) is True


def test_load_config_missing_sections_use_defaults(tmp_path: Path) -> None:
    """A config file with only one section still loads — the rest fall back
    to dataclass defaults. Important for partial overrides ("I only care
    about flipping prompt_recall")."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nprompt_recall = false\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall is False
    # Untouched fields keep their dataclass defaults.
    assert cfg.behavior.default_max_results == 5
    assert cfg.behavior.write_supersession is True
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
        "[behavior]\nprompt_recall = true\n",
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


def test_load_config_both_keys_warning_is_one_shot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One-shot per (config_path, key+"+both") for the both-keys branch:
    three loads of the same config carrying BOTH the legacy and the new
    key emit ONE warning, not three. Sibling to
    `test_load_config_legacy_key_deprecation_warning_is_one_shot` —
    `_apply_legacy_endorsement_debt_alias` uses a distinct guard tuple
    (`f"{old_key}+both"`) for this branch so the old-only flow can't
    cross-suppress the both-keys nudge on the same config path. A
    regression that collapsed the `+both` suffix would either re-emit
    the BOTH warning on every signal-driven reload (`bettermemory serve`
    log spam) OR silently swap branches on a path that had already
    tripped the old-only guard. Cross-asserts new-key-wins under repeat
    loads — the value resolution shouldn't drift across reads either."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\n"
        "endorsement_debt_ratio_threshold = 0.99\n"  # stale value
        "cold_endorsement_ratio_threshold = 0.25\n",  # the intent
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg1 = load_config(config_path)
        cfg2 = load_config(config_path)
        cfg3 = load_config(config_path)
    # New-key value wins on every read — the shim is idempotent on the
    # value as well as on the warning.
    assert cfg1.behavior.cold_endorsement_ratio_threshold == 0.25
    assert cfg2.behavior.cold_endorsement_ratio_threshold == 0.25
    assert cfg3.behavior.cold_endorsement_ratio_threshold == 0.25
    both_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
        and "BOTH" in r.getMessage()
    ]
    assert len(both_records) == 1, (
        f"expected exactly one BOTH-keys warning across three loads of "
        f"the same config, got {[r.getMessage() for r in both_records]}"
    )


def test_load_config_legacy_key_cross_branch_no_cross_suppression(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The old-only and both-keys branches have INDEPENDENT one-shot guards
    on the same `config_path`. Y4's sibling test
    (`test_load_config_both_keys_warning_is_one_shot`) pins same-branch
    repeat-suppression but resets the guard at entry, so it can't catch a
    regression that collapses the `+both` suffix into a shared guard tuple.
    This test exercises the cross-branch transition WITHOUT a reset
    between loads:

    1. Old-only TOML on path P -> old-only warning fires; guard tuple
       `(P, "endorsement_debt_ratio_threshold")` is recorded.
    2. SAME path P, rewritten to both-keys content; no
       `_reset_deprecated_key_guard()` -> BOTH warning must fire fresh
       (with `BOTH` in the message), not be silently cross-suppressed by
       the old-only guard from step 1.

    Cross-asserts both guard tuples are present after the two loads, so
    a regression that merged the keys (or dropped the `+both` suffix)
    would either skip the second warning OR leave a single guard entry
    instead of two."""
    from bettermemory import config as _cfg

    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"

    # Step 1: old-only branch on path P.
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.15\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg_old_only = load_config(config_path)
    assert cfg_old_only.behavior.cold_endorsement_ratio_threshold == 0.15
    old_only_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
        and "BOTH" not in r.getMessage()
    ]
    assert len(old_only_records) == 1, (
        f"step 1 (old-only) should fire exactly one old-only warning, got "
        f"{[r.getMessage() for r in old_only_records]}"
    )

    # Step 2: SAME path P, rewritten to both-keys; NO reset between.
    caplog.clear()
    config_path.write_text(
        "[behavior]\n"
        "endorsement_debt_ratio_threshold = 0.99\n"  # stale value
        "cold_endorsement_ratio_threshold = 0.25\n",  # the intent
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg_both = load_config(config_path)
    # New key wins on the both-keys branch.
    assert cfg_both.behavior.cold_endorsement_ratio_threshold == 0.25
    both_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
        and "BOTH" in r.getMessage()
    ]
    assert len(both_records) == 1, (
        f"step 2 (both-keys after old-only on same path, no reset) should "
        f"fire a fresh BOTH warning; got "
        f"{[r.getMessage() for r in both_records]}"
    )

    # Both guard tuples now coexist for the same resolved path — that's the
    # invariant a `+both` collapse regression would break.
    resolved = config_path.resolve()
    assert (
        resolved,
        "endorsement_debt_ratio_threshold",
    ) in _cfg._DEPRECATED_KEY_WARNED_PATHS, (
        "old-only guard tuple must remain set after the cross-branch transition"
    )
    assert (
        resolved,
        "endorsement_debt_ratio_threshold+both",
    ) in _cfg._DEPRECATED_KEY_WARNED_PATHS, (
        "both-keys guard tuple must be set independently of the old-only guard"
    )


def test_load_config_legacy_key_cross_branch_reverse_no_cross_suppression(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reverse of `test_load_config_legacy_key_cross_branch_no_cross_suppression`:
    BOTH branch triggers first on path P, then old-only on the same path
    without a reset. The old-only warning must fire fresh; the BOTH guard
    from step 1 must not silently swallow it. Pins the symmetry of the
    two independent guards — a regression that wired the old-only branch
    to check the `+both` tuple (or vice versa) would only show up in one
    direction without both tests."""
    from bettermemory import config as _cfg

    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"

    # Step 1: both-keys branch on path P.
    config_path.write_text(
        "[behavior]\n"
        "endorsement_debt_ratio_threshold = 0.99\n"
        "cold_endorsement_ratio_threshold = 0.25\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg_both = load_config(config_path)
    assert cfg_both.behavior.cold_endorsement_ratio_threshold == 0.25
    both_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
        and "BOTH" in r.getMessage()
    ]
    assert len(both_records) == 1, (
        f"step 1 (both-keys) should fire exactly one BOTH warning, got "
        f"{[r.getMessage() for r in both_records]}"
    )

    # Step 2: SAME path P, rewritten to old-only; NO reset between.
    caplog.clear()
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.15\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg_old_only = load_config(config_path)
    assert cfg_old_only.behavior.cold_endorsement_ratio_threshold == 0.15
    old_only_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
        and "BOTH" not in r.getMessage()
    ]
    assert len(old_only_records) == 1, (
        f"step 2 (old-only after both-keys on same path, no reset) should "
        f"fire a fresh old-only warning; got "
        f"{[r.getMessage() for r in old_only_records]}"
    )

    # Both guard tuples set after the reverse-order cross-branch transition.
    resolved = config_path.resolve()
    assert (
        resolved,
        "endorsement_debt_ratio_threshold",
    ) in _cfg._DEPRECATED_KEY_WARNED_PATHS, (
        "old-only guard tuple must be set independently of the both-keys guard"
    )
    assert (
        resolved,
        "endorsement_debt_ratio_threshold+both",
    ) in _cfg._DEPRECATED_KEY_WARNED_PATHS, (
        "both-keys guard tuple must remain set after the cross-branch transition"
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


# ---------------------------------------------------------------------------
# [behavior] search_mode
# ---------------------------------------------------------------------------


def test_search_modes_match_the_ranker_literal() -> None:
    """`_SEARCH_MODES` is a hand-copy of `search.SearchMode` — the import
    can't go the other way, since `search` imports `config`. Cross-pin it,
    because the whole value of coercing at load is that the loader agrees
    with the dispatcher about what a valid mode is; a drifted copy would
    reject a real mode or admit one `search.search` raises on."""
    import typing

    from bettermemory.search import SearchMode

    assert set(_SEARCH_MODES) == set(typing.get_args(SearchMode)), (
        "config._SEARCH_MODES has drifted from search.SearchMode; update "
        "the copy in lockstep or the loader and the ranker disagree about "
        "which strings are modes"
    )


def test_search_mode_normalises_case_and_whitespace(tmp_path: Path) -> None:
    """The consumers of this knob disagreed about normalisation:
    `handlers.search` passed the raw string to a dispatcher that raises
    on anything outside the literals, while the web UI silently rewrote
    an unknown value to `hybrid` — so a capitalised value broke every
    `memory_search` call while rendering a working lexical page.
    Normalise once, at the source, so every consumer sees one value.
    The legacy pre-4.0 value `"semantic"` normalises to the `hybrid`
    fallback — the migration path for configs that predate the
    embedding lane's removal."""
    import json

    for raw in ("Hybrid", " hybrid ", "BM25", "\tKeyword  "):
        # `json.dumps` for the TOML string literal: both grammars escape
        # basic strings the same way, so a tab survives the round trip
        # instead of being written raw and breaking the parse.
        (tmp_path / "config.toml").write_text(
            f"[behavior]\nsearch_mode = {json.dumps(raw)}\n", encoding="utf-8"
        )
        cfg = load_config(tmp_path / "config.toml")
        expected = raw.strip().lower()
        assert cfg.behavior.search_mode == expected, (
            f"{raw!r} did not normalise to {expected!r}"
        )

    for legacy in ("semantic", "Semantic", " SEMANTIC "):
        (tmp_path / "config.toml").write_text(
            f"[behavior]\nsearch_mode = {json.dumps(legacy)}\n", encoding="utf-8"
        )
        cfg = load_config(tmp_path / "config.toml")
        assert cfg.behavior.search_mode == "hybrid", (
            f"legacy {legacy!r} must fall back to 'hybrid'"
        )


def test_search_mode_falls_back_loudly_on_an_unknown_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo must not take the server down (the `default_max_results`
    rule) and must not pass through either — passing through is what made
    every `memory_search` call raise `unknown search mode` mid-conversation
    while the web page looked healthy.

    The warning is the whole point of the fallback. A user who wrote
    `serantic` asked for semantic ranking and is about to get lexical, and
    no other surface reports that: `doctor`'s retrieval check reads the
    resolved mode, so once the fallback lands it sees a legitimate
    `hybrid`."""
    (tmp_path / "config.toml").write_text(
        '[behavior]\nsearch_mode = "serantic"\n', encoding="utf-8"
    )
    caplog.set_level("WARNING", logger="bettermemory.config")

    cfg = load_config(tmp_path / "config.toml")

    assert cfg.behavior.search_mode == "hybrid"
    # `getMessage()` and not `.message`: the warning is logged lazily with
    # %-args, so the offending value only appears once they are applied.
    assert any("serantic" in record.getMessage() for record in caplog.records), (
        "unknown search_mode fell back silently; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_search_mode_absent_and_valid_values_are_untouched(tmp_path: Path) -> None:
    """No warning and no rewriting on the happy paths — otherwise the
    fallback's warning becomes noise every load and stops being read."""
    (tmp_path / "config.toml").write_text("[behavior]\n", encoding="utf-8")
    assert load_config(tmp_path / "config.toml").behavior.search_mode == "hybrid"

    for mode in _SEARCH_MODES:
        (tmp_path / "config.toml").write_text(
            f'[behavior]\nsearch_mode = "{mode}"\n', encoding="utf-8"
        )
        assert load_config(tmp_path / "config.toml").behavior.search_mode == mode


# ---------------------------------------------------------------------------
# Removed [behavior] keys: a key a major removed must not fail the load of a
# config written for the previous line — the rule `search_mode = "semantic"`
# already follows. The line is ignored and the operator is told once per
# (config, key) on the log lane. `corroboration_boost` is the first:
# deprecated in 7.6.0, removed in 8.0.0, because the ranking nudge it gated
# provably could not fire.
# ---------------------------------------------------------------------------


def _removed_key_warnings(caplog: pytest.LogCaptureFixture, key: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and key in r.getMessage()
    ]


@pytest.mark.parametrize(
    "line",
    [
        "corroboration_boost = true",
        "corroboration_boost = false",
        # Quoted and junk spellings: the value is never read, so no
        # spelling of it can fail the load either.
        'corroboration_boost = "yes"',
        "corroboration_boost = 3",
    ],
)
def test_removed_behavior_key_is_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, line: str
) -> None:
    """A 7.x config still setting the key loads, keeps every other
    setting it carries, and gets exactly one warning naming the release
    that removed the key and the file to edit."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[behavior]\n{line}\ndefault_max_results = 9\nconversational = false\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)

    # The neighbouring keys load exactly as they would without the line.
    assert cfg.behavior.default_max_results == 9
    assert cfg.behavior.conversational is False
    assert not hasattr(cfg.behavior, "corroboration_boost")

    messages = _removed_key_warnings(caplog, "corroboration_boost")
    assert len(messages) == 1, messages
    assert "was removed in bettermemory 8.0.0" in messages[0]
    assert "The line is ignored" in messages[0]
    assert str(config_path.resolve()) in messages[0]

    # One-shot per (path, key): a reload — a long-lived server rereading
    # config on signal — stays quiet and still loads.
    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        reloaded = load_config(config_path)
    assert reloaded.behavior.default_max_results == 9
    assert not _removed_key_warnings(caplog, "corroboration_boost")


def test_config_without_the_removed_key_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The notice fires for a config that carries the line, not on every
    load — a config that never mentions the key must stay quiet."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text("[behavior]\ndefault_max_results = 5\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        load_config(config_path)
    assert not _removed_key_warnings(caplog, "corroboration_boost")


@pytest.mark.parametrize(
    ("section", "line", "key"),
    [
        ("behavior", "require_write_confirmation = true", "require_write_confirmation"),
        ("behavior", "rescue_expansion = true", "rescue_expansion"),
        ("behavior", "endorsement_boost = true", "endorsement_boost"),
        ("behavior", "outcome_demotion = true", "outcome_demotion"),
        ("behavior", "standing_tier = true", "standing_tier"),
        ("behavior", "full_tool_surface = true", "full_tool_surface"),
        ("behavior", "curation_hint_threshold = 3", "curation_hint_threshold"),
        ("behavior", "curation_hint_enabled = false", "curation_hint_enabled"),
        ("telemetry", "log_queries_verbatim = true", "log_queries_verbatim"),
        ("telemetry", "max_bytes = 5", "max_bytes"),
        ("consolidate", "auto_apply = true", "auto_apply"),
        ("proposals", "auto_propose = true", "auto_propose"),
        ("capture", "enabled = true", "enabled"),
    ],
)
def test_every_9_0_removal_loads_with_one_warning_and_no_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    section: str,
    line: str,
    key: str,
) -> None:
    """A config written for 8.x still loads under 9.0: each removed
    `[behavior]` / `[telemetry]` key and each key of a removed section
    is ignored with exactly one notice naming 9.0.0, and every setting
    that still exists loads as written."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    body = "[behavior]\ndefault_max_results = 9\n"
    if section == "behavior":
        body += f"{line}\n"
    else:
        body += f"[{section}]\n{line}\n"
    config_path.write_text(body, encoding="utf-8")
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.default_max_results == 9
    if section == "behavior":
        assert not hasattr(cfg.behavior, key)
    elif section == "telemetry":
        assert not hasattr(cfg.telemetry, key)
    else:
        assert not hasattr(cfg, section)
    messages = _removed_key_warnings(caplog, f"[{section}] `{key}`")
    assert len(messages) == 1, messages
    assert "was removed in bettermemory 9.0.0" in messages[0]
    assert "The line is ignored" in messages[0]


def test_a_removed_section_warns_once_per_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Three keys under a removed section: three notices, one load, no
    error, and a reload stays quiet."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[consolidate]\nauto_apply = true\nauto_apply_interval_hours = 6\n"
        "auto_apply_max_memories = 10\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert not hasattr(cfg, "consolidate")
    messages = _removed_key_warnings(caplog, "[consolidate]")
    assert len(messages) == 3, messages
    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        load_config(config_path)
    assert not _removed_key_warnings(caplog, "[consolidate]")


def test_every_removed_key_is_gone_from_the_config_surface() -> None:
    """A key in a removed registry that still exists on its dataclass
    or in the shipped `DEFAULT_CONFIG` has been only half removed: the
    loader would drop the operator's value and warn that the setting is
    gone while the field and the shipped prose say otherwise."""
    from bettermemory.config import (
        _REMOVED_BEHAVIOR_KEYS,
        _REMOVED_SECTIONS,
        _REMOVED_TELEMETRY_KEYS,
        TelemetryConfig,
    )

    assert _REMOVED_BEHAVIOR_KEYS, "the registry has no entry to pin"
    for key in _REMOVED_BEHAVIOR_KEYS:
        assert not hasattr(BehaviorConfig(), key), key
        assert key not in DEFAULT_CONFIG, key
    for key in _REMOVED_TELEMETRY_KEYS:
        assert not hasattr(TelemetryConfig(), key), key
        assert key not in DEFAULT_CONFIG, key
    for section in _REMOVED_SECTIONS:
        assert not hasattr(Config(), section), section
        assert f"[{section}]" not in DEFAULT_CONFIG, section


def _unreadable_dir_is_enforceable() -> bool:
    """True when this process can actually be locked out of a directory.
    Windows does not honour POSIX mode bits and root walks through them."""
    import os
    import sys

    if sys.platform == "win32":
        return False
    getuid = getattr(os, "geteuid", None)
    return getuid is not None and getuid() != 0


@pytest.mark.skipif(
    not _unreadable_dir_is_enforceable(),
    reason="needs POSIX mode bits and a non-root euid",
)
def test_resolved_directory_falls_back_to_global_when_cwd_children_are_unstattable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd whose children cannot be stat'd takes the global fallback,
    the same degrade the deleted-cwd branch already takes.

    `Path.is_dir()` re-raises EACCES on 3.11-3.13, so the project-store
    probe aborted every entry path — CLI, server startup, both hooks —
    with a traceback; on 3.14 it answered False and fell through, which
    is the answer, but only by accident of interpreter. The probe now
    asks `os.path.isdir`, which answers the same way on all four."""
    monkeypatch.delenv(ENV_DIR_OVERRIDE, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _set_fake_home(monkeypatch, fake_home)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    cwd.chmod(0o000)
    try:
        resolved = Config().resolved_directory(cwd=cwd)
    finally:
        cwd.chmod(0o755)
    assert resolved == (fake_home / ".claude-memory").resolve()
