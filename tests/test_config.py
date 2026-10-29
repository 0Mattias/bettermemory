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
    MembershipConfig,
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
    assert loaded.behavior.session_brief == fresh.behavior.session_brief
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
        "[behavior]\nsession_brief = false\n"
        "conversational = false\nwrite_supersession = false\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.session_brief is False
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
        'session_brief = "0"\n'
        "[telemetry]\n"
        'enabled = "False"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.conversational is False
    assert cfg.behavior.write_supersession is False
    assert cfg.behavior.session_brief is False
    assert cfg.telemetry.enabled is False

    # Quoted truthy spellings still coerce to True (case-insensitive);
    # a quoted legacy boolean on `prompt_recall` reads as its mode.
    config_path.write_text(
        '[behavior]\nprompt_recall = "TRUE"\n[telemetry]\nenabled = "on"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall == "project"
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
        '[behavior]\nprompt_recall = "off"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall == "off"
    # Untouched fields keep their dataclass defaults.
    assert cfg.behavior.default_max_results == 5
    assert cfg.behavior.write_supersession is True
    assert cfg.scopes.allowed == []
    assert cfg.telemetry.enabled is True


# ---------------------------------------------------------------------------
# `endorsement_debt_ratio_threshold`: renamed `cold_endorsement_ratio_threshold`
# in 3.2.0 and read under the new name until 9.0, a removed key since. The
# line is dropped unread with the notice every removed key gets
# (`_REMOVED_BEHAVIOR_KEYS`), and the notice names the successor.
# ---------------------------------------------------------------------------


def _reset_deprecated_key_guard() -> None:
    """Clear the module-level one-shot guard so each test sees a fresh
    notice."""
    from bettermemory import config as _cfg

    _cfg._DEPRECATED_KEY_WARNED_PATHS.clear()


def _old_key_notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
        and "endorsement_debt_ratio_threshold" in r.getMessage()
    ]


def test_the_pre_3_2_threshold_key_is_removed_with_a_notice_naming_its_successor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The old key loads with the removal notice, which names the key it
    was renamed to; its value is not read under the new name, and a value
    that would not parse cannot fail the load."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.15\n", encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.0
    (notice,) = _old_key_notices(caplog)
    assert "[behavior] `endorsement_debt_ratio_threshold`" in notice
    assert "was removed in bettermemory 9.0.0" in notice
    assert "renamed `cold_endorsement_ratio_threshold` in 3.2.0" in notice
    assert "The line is ignored" in notice

    _reset_deprecated_key_guard()
    config_path.write_text(
        '[behavior]\nendorsement_debt_ratio_threshold = "abc"\n', encoding="utf-8"
    )
    assert load_config(config_path).behavior.cold_endorsement_ratio_threshold == 0.0


def test_the_threshold_key_by_its_3_2_name_still_works(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`cold_endorsement_ratio_threshold` is read, alone with no notice,
    and beside the removed old name, which is dropped with its notice."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\ncold_endorsement_ratio_threshold = 0.15\n", encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.15
    assert _old_key_notices(caplog) == []

    config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.99\n"
        "cold_endorsement_ratio_threshold = 0.15\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.cold_endorsement_ratio_threshold == 0.15
    (notice,) = _old_key_notices(caplog)
    assert "was removed in bettermemory 9.0.0" in notice


# ---------------------------------------------------------------------------
# A value in a message is printed as config.toml writes it
# ---------------------------------------------------------------------------

# (value, its TOML spelling): a string, an integer, a boolean and a list.
_SPELLINGS = [
    ("x", '"x"'),
    ('say "hi"', r'"say \"hi\""'),
    (5, "5"),
    (True, "true"),
    (False, "false"),
    (["a", 1, True], '["a", 1, true]'),
]


@pytest.mark.parametrize(("value", "spelled"), _SPELLINGS)
def test_a_value_is_spelled_as_config_toml_writes_it(
    value: object, spelled: str
) -> None:
    """`config.toml_spelling` writes TOML, which TOML reads back as the
    same value, and `bettermemory config check` prints a value through the
    same function."""
    from bettermemory import config as _cfg
    from bettermemory.cli import config_cmd

    assert _cfg.toml_spelling(value) == spelled
    assert tomllib.loads(f"v = {spelled}\n")["v"] == value
    assert config_cmd._shown(value) == spelled


def test_a_table_and_the_infinities_are_spelled_as_toml() -> None:
    from bettermemory import config as _cfg

    table = {"port": 1, "a b": "x"}
    spelled = _cfg.toml_spelling(table)
    assert spelled == '{port = 1, "a b" = "x"}'
    assert tomllib.loads(f"v = {spelled}\n")["v"] == table
    assert _cfg.toml_spelling(float("inf")) == "inf"
    assert _cfg.toml_spelling(float("-inf")) == "-inf"
    assert _cfg.toml_spelling(1.5) == "1.5"


@pytest.mark.parametrize(("value", "spelled"), _SPELLINGS)
def test_the_scalar_section_warning_prints_the_line_as_toml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, value: object, spelled: str
) -> None:
    """`daemon = "x"` is warned about as `daemon = "x"`, never as the
    Python repr `daemon = 'x'` or `daemon = True`."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"daemon = {spelled}\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        load_config(config_path)
    (warning,) = [
        r.getMessage() for r in caplog.records if "section belongs" in r.getMessage()
    ]
    assert f"writes `daemon = {spelled}` where a [daemon] section belongs" in warning


def test_a_refused_section_and_a_key_given_a_table_print_toml(tmp_path: Path) -> None:
    """The located errors print the line as TOML too: a section that
    refuses a scalar, and a scalar key written as a table."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('security = "on"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r'\[security\] = "on" must be a table'):
        load_config(config_path)
    config_path.write_text("[daemon]\nport = {number = 1}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[daemon\] port = \{number = 1\} must be"):
        load_config(config_path)


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


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ('search_mode = "serantic"', 'search_mode = "serantic" is not one of'),
        ("search_mode = 5", "search_mode = 5 is not one of"),
        ('prompt_recall = "sometimes"', 'prompt_recall = "sometimes" is not one of'),
        ('prompt_recall = ["on"]', 'prompt_recall = ["on"] is not one of'),
    ],
)
def test_a_fallback_prints_the_line_as_config_toml_writes_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, line: str, shown: str
) -> None:
    """`toml_spelling`, as every other loader message: the value in double
    quotes or bare, never Python's repr, and the fallback the same way."""
    (tmp_path / "config.toml").write_text(f"[behavior]\n{line}\n", encoding="utf-8")
    caplog.set_level("WARNING", logger="bettermemory.config")
    load_config(tmp_path / "config.toml")
    messages = [r.getMessage() for r in caplog.records if shown in r.getMessage()]
    assert len(messages) == 1, [r.getMessage() for r in caplog.records]
    assert "'" not in messages[0].split(" is not one of ")[0]
    assert 'falling back to "' in messages[0]


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
        ("capture", "idle_minutes = 30", "idle_minutes"),
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
    elif section == "capture":
        assert not hasattr(cfg.capture, key)
        assert cfg.capture.stop_checkpoint is False
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
        _REMOVED_CAPTURE_KEYS,
        _REMOVED_SECTIONS,
        _REMOVED_TELEMETRY_KEYS,
        CaptureConfig,
        TelemetryConfig,
    )

    assert _REMOVED_BEHAVIOR_KEYS, "the registry has no entry to pin"
    for key in _REMOVED_BEHAVIOR_KEYS:
        assert not hasattr(BehaviorConfig(), key), key
        assert key not in DEFAULT_CONFIG, key
    for key in _REMOVED_TELEMETRY_KEYS:
        assert not hasattr(TelemetryConfig(), key), key
        assert key not in DEFAULT_CONFIG, key
    for key in _REMOVED_CAPTURE_KEYS:
        assert not hasattr(CaptureConfig(), key), key
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


# ---------------------------------------------------------------------------
# The recall surface's settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"project"', "project"),
        ('"everywhere"', "everywhere"),
        ('"off"', "off"),
        ('" Everywhere "', "everywhere"),
        ("true", "project"),
        ("false", "off"),
        ('"yes"', "project"),
        ('"no"', "off"),
    ],
)
def test_prompt_recall_takes_a_mode_and_reads_a_legacy_boolean(
    tmp_path: Path, raw: str, expected: str
) -> None:
    """`prompt_recall` names a delivery mode now. A config written when it
    was a boolean keeps its meaning: true was "deliver", which is
    "project" today, and false was "never", which is "off"."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[behavior]\nprompt_recall = {raw}\n", encoding="utf-8")
    assert load_config(config_path).behavior.prompt_recall == expected


def test_prompt_recall_garbage_falls_back_to_project_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[behavior]\nprompt_recall = "sometimes"\n', encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
    assert cfg.behavior.prompt_recall == "project"
    assert any("prompt_recall" in record.getMessage() for record in caplog.records)


def test_prompt_recall_and_session_brief_defaults() -> None:
    from bettermemory.config import BehaviorConfig

    assert BehaviorConfig().prompt_recall == "project"
    assert BehaviorConfig().session_brief is True
    assert not hasattr(BehaviorConfig(), "recall_in_project")


def test_recall_in_project_is_a_removed_key_with_a_notice(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`recall_in_project` retired with the recall surface: delivery in the
    caller's project is the default mode. A config that still carries the
    line loads, ignores it, and says so once."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[behavior]\nrecall_in_project = false\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
        load_config(config_path)
    assert cfg.behavior.prompt_recall == "project"
    notices = [
        record.getMessage()
        for record in caplog.records
        if "recall_in_project" in record.getMessage()
    ]
    assert len(notices) == 1
    assert "removed" in notices[0]


# ---------------------------------------------------------------------------
# [capture]: the turn-end checkpoint, off unless switched on by its v9 key
# ---------------------------------------------------------------------------


def test_capture_defaults_are_off_with_the_declared_bounds(tmp_path: Path) -> None:
    """No `[capture]` section: the checkpoint is off, at 12,000 tokens and
    12 requests per store per day, the same as the dataclass."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[behavior]\ndefault_max_results = 5\n", encoding="utf-8")
    loaded = load_config(config_path).capture
    fresh = Config().capture
    assert (loaded.stop_checkpoint, loaded.checkpoint_tokens, loaded.daily_cap) == (
        False,
        12_000,
        12,
    )
    assert loaded == fresh


def test_the_shipped_config_documents_capture_off(tmp_path: Path) -> None:
    """The first-run file carries `[capture]` with its three keys commented
    out at their defaults, the checkpoint off, so it sets none of them, and
    loading it gives the dataclass defaults; none of the keys 8.x shipped
    in the section comes back."""
    from bettermemory.config import _REMOVED_CAPTURE_KEYS

    assert tomllib.loads(DEFAULT_CONFIG)["capture"] == {}
    prose = DEFAULT_CONFIG.split("[capture]")[1].split("[membership]")[0]
    for line in (
        "# stop_checkpoint = false",
        "# checkpoint_tokens = 12000",
        "# daily_cap = 12",
    ):
        assert f"\n{line}\n" in prose, line
    for key in _REMOVED_CAPTURE_KEYS:
        assert f"# {key} =" not in prose, key
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    assert load_config(config_path).capture == Config().capture
    assert "capture undo" in DEFAULT_CONFIG.split("[capture]")[1]


def test_capture_keys_load_as_written(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[capture]\nstop_checkpoint = true\ncheckpoint_tokens = 8000\ndaily_cap = 3\n",
        encoding="utf-8",
    )
    capture = load_config(config_path).capture
    assert capture.stop_checkpoint is True
    assert capture.checkpoint_tokens == 8_000
    assert capture.daily_cap == 3


@pytest.mark.parametrize("written", [0, -5, 1, 499])
def test_a_checkpoint_below_the_floor_is_clamped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, written: int
) -> None:
    """Below the floor every attended Stop would open a capture on an
    all-but-empty stretch, up to the daily cap."""
    from bettermemory.config import MIN_CHECKPOINT_TOKENS

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[capture]\ncheckpoint_tokens = {written}\n", encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        capture = load_config(config_path).capture
    assert capture.checkpoint_tokens == MIN_CHECKPOINT_TOKENS == 500
    assert "[capture] checkpoint_tokens" in caplog.text


def test_capture_bad_numbers_raise_a_located_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[capture]\ndaily_cap = "many"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[capture\] daily_cap"):
        load_config(config_path)


def test_a_pre_v9_capture_enabled_never_turns_capture_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """8.x shipped `[capture] enabled` for a capture that called a model.
    The v9 checkpoint asks the chat model at a turn end, a different
    thing, so an 8.x `enabled = true` must not switch it on: the line is
    ignored with one notice naming the key that does, and the rest of
    the section still loads."""
    _reset_deprecated_key_guard()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[capture]\nenabled = true\ncheckpoint_tokens = 40000\nidle_minutes = 30\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="bettermemory.config"):
        cfg = load_config(config_path)
        load_config(config_path)
    assert cfg.capture.stop_checkpoint is False
    assert cfg.capture.checkpoint_tokens == 40_000
    enabled = _removed_key_warnings(caplog, "[capture] `enabled`")
    assert len(enabled) == 1, enabled
    assert "stop_checkpoint" in enabled[0]
    assert len(_removed_key_warnings(caplog, "[capture] `idle_minutes`")) == 1


def test_capture_is_no_longer_a_removed_section() -> None:
    from bettermemory.config import _REMOVED_SECTIONS

    assert "capture" not in _REMOVED_SECTIONS


# ---------------------------------------------------------------------------
# [membership]: the checkout token's policy switch and the A3 switch
# ---------------------------------------------------------------------------


def test_membership_defaults_write_the_token_and_join_silently(tmp_path: Path) -> None:
    """No `[membership]` section: the token is written (decision D11) and
    two agreeing witnesses join silently (decision D8), as the dataclass
    says."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[behavior]\ndefault_max_results = 5\n", encoding="utf-8")
    loaded = load_config(config_path).membership
    assert (loaded.write_token, loaded.strict) == (True, False)
    assert loaded == Config().membership


def test_the_shipped_config_documents_membership(tmp_path: Path) -> None:
    """The first-run file carries `[membership]` with both keys commented
    out at their defaults, and its prose names what the token is and
    where it lives."""
    assert tomllib.loads(DEFAULT_CONFIG)["membership"] == {}
    for line in ("# write_token = true", "# strict = false"):
        assert f"\n{line}\n" in DEFAULT_CONFIG.split("[membership]")[1], line
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    assert load_config(config_path).membership == Config().membership
    prose = DEFAULT_CONFIG.split("[membership]")[1]
    assert "info/bettermemory" in prose
    assert "never in the working tree" in prose
    assert "bare repository" in prose


def test_membership_keys_load_as_written(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[membership]\nwrite_token = false\nstrict = true\n", encoding="utf-8"
    )
    membership = load_config(config_path).membership
    assert membership.write_token is False
    assert membership.strict is True


@pytest.mark.parametrize(
    ("written", "write_token", "strict"),
    [
        ('write_token = "false"\nstrict = "yes"\n', False, True),
        ('write_token = "off"\nstrict = "1"\n', False, True),
        ('write_token = "maybe"\nstrict = "perhaps"\n', True, False),
        ("write_token = 0\nstrict = 1\n", True, False),
    ],
    ids=["quoted-words", "quoted-switches", "unrecognised-strings", "integers"],
)
def test_membership_keys_go_through_the_bool_coercion(
    tmp_path: Path, written: str, write_token: bool, strict: bool
) -> None:
    """A quoted "false" keeps the token switched off (the str trap
    `_coerce_bool` exists for); an unrecognised value falls back to the
    default."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[membership]\n{written}", encoding="utf-8")
    membership = load_config(config_path).membership
    assert (membership.write_token, membership.strict) == (write_token, strict)


def test_a_membership_value_that_is_not_a_table_reads_as_its_defaults(
    tmp_path: Path,
) -> None:
    """rc11 read it as the section's defaults, and so does this release, so
    the hooks keep running on such a config; the loader now warns
    (`config._section_table`)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("membership = 3\n", encoding="utf-8")
    assert load_config(config_path).membership == MembershipConfig()
