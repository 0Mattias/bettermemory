"""The organisation-managed config (`managed.py`) and `[storage] cwd_store`.

Three contracts, each pinned with the behaviour it promises:

1. **Where the file is.** One fixed path per OS that nothing in the
   environment moves, read through the one seam the suite's autouse
   fixture redirects (`conftest.managed_config_path`), so no test reads
   the machine's real file.
2. **What it does.** Every key it sets is pinned: the user's value for the
   key is ignored, with one notice per key naming the managed file; the
   effective `Config` lists the pinned keys. A malformed managed file is an
   error naming it, never skipped. With no file, `load_config` is exactly
   what it was before managed files existed.
3. **`[storage] cwd_store`.** False skips resolution rule 3, and the hook
   client follows through `load_config().resolved_directory()` with no
   code of its own.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from bettermemory import _daemon_client
from bettermemory import config as _config
from bettermemory import managed as _managed
from bettermemory.config import DEFAULT_CONFIG, Config, load_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _REPO_ROOT / "examples" / "managed.toml"
#: The seam as production has it, taken at import time, before the autouse
#: fixture replaces it for each test. Only called, never used to read.
_REAL_MANAGED_CONFIG_PATH = _managed.managed_config_path


def _set_fake_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """`~` redirected on every platform (see tests/test_config.py)."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _comparable(config: Config) -> dict[str, object]:
    """Every setting of `config`, without the path it was read from."""
    out = asdict(config)
    out.pop("config_path")
    return out


# ---------------------------------------------------------------------------
# Where the file is
# ---------------------------------------------------------------------------


def test_the_suite_never_reads_the_machine_managed_file(
    managed_config_path: Path,
) -> None:
    """The autouse fixture points the seam at a scratch path, which is
    not the system location and holds no file until a test writes one."""
    assert _managed.managed_config_path() == managed_config_path
    assert managed_config_path != _managed.system_path(sys.platform)
    assert not managed_config_path.exists()


def test_the_production_seam_answers_the_system_path() -> None:
    expected = None if sys.platform == "win32" else _managed.system_path(sys.platform)
    assert _REAL_MANAGED_CONFIG_PATH() == expected


def test_windows_reads_no_managed_file_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    # A standard user can create folders under ProgramData, so a file
    # there is not yet known to be an administrator's.
    monkeypatch.setattr(sys, "platform", "win32")
    assert _REAL_MANAGED_CONFIG_PATH() is None


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", Path("/Library/Application Support/bettermemory/managed.toml")),
        ("linux", Path("/etc/bettermemory/managed.toml")),
        ("freebsd14", Path("/etc/bettermemory/managed.toml")),
    ],
)
def test_the_location_is_fixed_per_platform(platform: str, expected: Path) -> None:
    assert _managed.system_path(platform) == expected


def test_windows_reads_program_data_from_the_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(_managed, "_windows_program_data", lambda: program_data)
    assert _managed.system_path("win32") == (
        program_data / "bettermemory" / "managed.toml"
    )


def test_no_environment_variable_moves_the_managed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`platformdirs.site_config_dir` honours `$XDG_CONFIG_DIRS` and a
    Windows lookup by variable honours `%ProgramData%`; both are the
    user's to set, so neither may move the file. Nor may the variables
    that move the store, the keys or the user config."""
    before = _managed.system_path(sys.platform)
    program_data_before = _managed._windows_program_data()
    for name in (
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
        "ProgramData",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        _config.ENV_DIR_OVERRIDE,
        _config.KEYS_DIR_ENV,
    ):
        monkeypatch.setenv(name, str(tmp_path / name))
    _set_fake_home(monkeypatch, tmp_path / "home")
    assert _managed.system_path(sys.platform) == before
    assert _managed._windows_program_data() == program_data_before
    assert tmp_path not in _managed._windows_program_data().parents


def test_off_windows_program_data_is_the_fixed_fallback() -> None:
    if sys.platform == "win32":
        pytest.skip("the shell answers on Windows")
    assert _managed._windows_program_data() == Path("C:\\ProgramData")


# ---------------------------------------------------------------------------
# No managed file: nothing changes
# ---------------------------------------------------------------------------


def test_without_a_managed_file_the_config_is_the_user_file_alone(
    tmp_path: Path,
) -> None:
    """The round trip of the shipped defaults equals `Config()` field for
    field, with no managed path and no pinned key."""
    loaded = load_config(_user_config(tmp_path, DEFAULT_CONFIG))
    assert loaded.managed_path is None
    assert loaded.managed_keys == frozenset()
    assert _comparable(loaded) == _comparable(Config())


def test_read_managed_answers_none_only_for_a_missing_file(tmp_path: Path) -> None:
    assert _managed.read_managed(tmp_path / "absent.toml") is None
    empty = tmp_path / "empty.toml"
    empty.write_text("", encoding="utf-8")
    assert _managed.read_managed(empty) == {}


def test_an_empty_managed_file_pins_nothing(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_text("# nothing pinned yet\n", encoding="utf-8")
    loaded = load_config(_user_config(tmp_path, "[telemetry]\nenabled = false\n"))
    assert loaded.managed_path == managed_config_path
    assert loaded.managed_keys == frozenset()
    assert loaded.telemetry.enabled is False


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


def test_a_managed_scalar_beats_the_user_value_with_one_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text("[telemetry]\nenabled = true\n", encoding="utf-8")
    user = _user_config(tmp_path, "[telemetry]\nenabled = false\n")

    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(user)
        load_config(user)

    assert loaded.telemetry.enabled is True
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})
    assert loaded.managed_path == managed_config_path
    notices = [r.getMessage() for r in caplog.records if "pins" in r.getMessage()]
    # One per key, however often the config is read in this process.
    assert len(notices) == 1, notices
    assert str(managed_config_path) in notices[0]
    assert "[telemetry] `enabled`" in notices[0]


def test_a_user_value_equal_to_the_pin_gets_no_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text("[daemon]\nport = 7400\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, "[daemon]\nport = 7400\n"))
    assert loaded.daemon.port == 7400
    assert loaded.managed_keys == frozenset({"daemon.port"})
    assert not [r for r in caplog.records if "pins" in r.getMessage()]


def test_a_managed_list_replaces_the_user_list_whole(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_text(
        '[scopes]\nallowed = ["projects:corp"]\n', encoding="utf-8"
    )
    loaded = load_config(
        _user_config(tmp_path, '[scopes]\nallowed = ["tools", "projects:corp"]\n')
    )
    assert loaded.scopes.allowed == ["projects:corp"]


def test_keys_the_managed_file_leaves_out_stay_the_users(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_text("[behavior]\nsession_brief = false\n", "utf-8")
    loaded = load_config(
        _user_config(
            tmp_path,
            '[behavior]\nsession_brief = true\nprompt_recall = "off"\n'
            "default_max_results = 9\n",
        )
    )
    assert loaded.behavior.session_brief is False
    assert loaded.behavior.prompt_recall == "off"
    assert loaded.behavior.default_max_results == 9
    assert loaded.managed_keys == frozenset({"behavior.session_brief"})


def test_a_pinned_key_the_user_mistyped_is_ignored_not_fatal(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """The user's value for a pinned key is dropped before it is parsed:
    it is not in force, so its type cannot stop the load."""
    managed_config_path.write_text("[daemon]\nport = 7401\n", encoding="utf-8")
    loaded = load_config(_user_config(tmp_path, '[daemon]\nport = "seven"\n'))
    assert loaded.daemon.port == 7401


def test_a_managed_storage_directory_beats_the_users_but_not_the_env(
    tmp_path: Path, managed_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[storage] directory` is pinned against the user's file. The
    environment variable still decides first: the managed file binds the
    user's config, not a process that sets its own environment."""
    pinned = tmp_path / "corp-store"
    managed_config_path.write_text(
        f'[storage]\ndirectory = "{pinned.as_posix()}"\n', encoding="utf-8"
    )
    user = _user_config(
        tmp_path, f'[storage]\ndirectory = "{(tmp_path / "mine").as_posix()}"\n'
    )
    monkeypatch.delenv(_config.ENV_DIR_OVERRIDE, raising=False)
    loaded = load_config(user)
    assert loaded.resolved_directory() == pinned.resolve()

    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(tmp_path / "env"))
    assert loaded.resolved_directory() == (tmp_path / "env").resolve()


def test_the_managed_file_pins_through_the_default_config_path(
    managed_config_path: Path,
) -> None:
    """`load_config()` with no argument, the call the daemon, the CLI and
    the hooks make, reads the managed file too."""
    managed_config_path.write_text("[capture]\nstop_checkpoint = true\n", "utf-8")
    loaded = load_config()
    assert loaded.capture.stop_checkpoint is True
    assert "capture.stop_checkpoint" in loaded.managed_keys


def test_unknown_sections_and_keys_are_ignored_and_not_listed(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """The managed file is read with config.toml's own vocabulary: a key
    the loader does not read is ignored there too, and only settings that
    exist are reported as pinned."""
    managed_config_path.write_text(
        "[nonsense]\nx = 1\n[behavior]\nno_such_key = true\nsession_brief = false\n",
        encoding="utf-8",
    )
    loaded = load_config(_user_config(tmp_path, ""))
    assert loaded.managed_keys == frozenset({"behavior.session_brief"})


def test_a_dunder_table_cannot_reach_a_class_object(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_text("[__class__]\nstorage = 1\n", encoding="utf-8")
    loaded = load_config(_user_config(tmp_path, ""))
    assert loaded.managed_keys == frozenset()
    assert isinstance(Config().storage, _config.StorageConfig)


def test_a_removed_key_in_the_managed_file_gets_its_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text(
        "[behavior]\nendorsement_boost = true\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, ""))
    assert loaded.managed_keys == frozenset()
    removed = [
        r.getMessage() for r in caplog.records if "was removed" in r.getMessage()
    ]
    assert len(removed) == 1, removed
    assert str(managed_config_path.resolve()) in removed[0]


def test_the_legacy_alias_pins_its_successor(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_text(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.25\n", encoding="utf-8"
    )
    loaded = load_config(
        _user_config(tmp_path, "[behavior]\ncold_endorsement_ratio_threshold = 0.5\n")
    )
    assert loaded.behavior.cold_endorsement_ratio_threshold == 0.25
    assert loaded.managed_keys == frozenset(
        {"behavior.cold_endorsement_ratio_threshold"}
    )


def test_a_users_legacy_alias_of_a_pinned_key_is_ignored_with_its_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The user's file spells the pinned key by its pre-3.2 name. The
    line is the successor's, so it is dropped unread like any pinned
    line: a value that would not parse cannot fail the load, and the
    pinned-key notice names the setting it lost to."""
    managed_config_path.write_text(
        "[behavior]\ncold_endorsement_ratio_threshold = 0.25\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(
            _user_config(
                tmp_path, '[behavior]\nendorsement_debt_ratio_threshold = "abc"\n'
            )
        )
    assert loaded.behavior.cold_endorsement_ratio_threshold == 0.25
    notices = [r.getMessage() for r in caplog.records if "pins" in r.getMessage()]
    assert len(notices) == 1, notices
    assert "[behavior] `cold_endorsement_ratio_threshold`" in notices[0]


def test_the_shipped_example_loads_as_a_managed_file(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """examples/managed.toml is what an administrator starts from, so it
    must load and pin what its comments say. `[security]` is pinned once
    the build reads that section; until then it is ignored like any key
    the loader does not read."""
    managed_config_path.write_bytes(_EXAMPLE.read_bytes())
    loaded = load_config(_user_config(tmp_path, DEFAULT_CONFIG))
    assert loaded.storage.cwd_store is False
    assert loaded.policy.forbid_acknowledge == ["credential"]
    assert loaded.policy.forbid_force is False
    assert loaded.telemetry.enabled is False
    expected = {"storage.cwd_store", "policy.forbid_acknowledge", "telemetry.enabled"}
    security = getattr(loaded, "security", None)
    if security is not None:
        assert security.strict_provenance is True
        expected.add("security.strict_provenance")
    assert loaded.managed_keys == frozenset(expected)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[telemetry\nenabled = true\n", "malformed managed config"),
        ('policy = "strict"\n', "must be a [policy] section table"),
        ('[daemon]\nport = "seven"\n', "[daemon] port"),
        ('[policy]\nforbid_acknowledge = ["credentials"]\n', "forbid_acknowledge"),
        ('[policy]\nforbid_force = "ture"\n', "[policy] forbid_force"),
        ('[storage]\ncwd_store = "nope"\n', "[storage] cwd_store"),
    ],
)
def test_a_malformed_managed_file_is_an_error_naming_it(
    tmp_path: Path, managed_config_path: Path, text: str, fragment: str
) -> None:
    managed_config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_config(_user_config(tmp_path, ""))
    message = str(excinfo.value)
    assert str(managed_config_path) in message, message
    assert fragment in message, message


def test_a_managed_file_that_is_not_utf8_is_an_error_naming_it(
    tmp_path: Path, managed_config_path: Path
) -> None:
    managed_config_path.write_bytes(b"[telemetry]\nenabled = \xff\n")
    with pytest.raises(ValueError, match="malformed managed config") as excinfo:
        load_config(_user_config(tmp_path, ""))
    assert str(managed_config_path) in str(excinfo.value)


def test_a_byte_order_mark_is_not_a_malformed_file(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """Windows PowerShell 5.1 writes UTF-8 with a byte-order mark, and a
    managed file deployed through it must still load and pin."""
    managed_config_path.write_bytes(b"\xef\xbb\xbf[policy]\nforbid_force = true\n")
    loaded = load_config(_user_config(tmp_path, ""))
    assert loaded.policy.forbid_force is True
    assert loaded.managed_keys == frozenset({"policy.forbid_force"})


def test_a_dangling_symlink_at_the_managed_path_fails_closed(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """The path exists, as a link to a file that is gone: the pins it
    would carry are unknown, so the load stops rather than run without
    them, as for any other unreadable managed file."""
    try:
        managed_config_path.symlink_to(tmp_path / "gone.toml")
    except OSError:
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(ValueError, match="symlink") as excinfo:
        load_config(_user_config(tmp_path, ""))
    assert str(managed_config_path) in str(excinfo.value)


def test_an_unreadable_managed_path_fails_closed(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """A directory where the file belongs exists but cannot be read as
    one: the pins it would carry are unknown, so the load stops."""
    managed_config_path.mkdir()
    with pytest.raises(ValueError, match="could not be read") as excinfo:
        load_config(_user_config(tmp_path, ""))
    assert str(managed_config_path) in str(excinfo.value)


def test_a_managed_file_this_account_cannot_read_fails_closed(
    tmp_path: Path, managed_config_path: Path
) -> None:
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX file modes")
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    managed_config_path.write_text("[telemetry]\nenabled = true\n", "utf-8")
    managed_config_path.chmod(0o000)
    try:
        with pytest.raises(ValueError, match="could not be read"):
            load_config(_user_config(tmp_path, ""))
    finally:
        managed_config_path.chmod(0o600)


# ---------------------------------------------------------------------------
# [storage] cwd_store
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory that ships `.claude-memory/`, a faked home and
    no `BETTERMEMORY_DIR`: the shape of a cloned repository that carries
    a store."""
    repo = tmp_path / "repo"
    (repo / _config.PROJECT_DIR_NAME).mkdir(parents=True)
    _set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(_config.ENV_DIR_OVERRIDE, raising=False)
    monkeypatch.chdir(repo)
    return repo


def test_cwd_store_defaults_on_and_keeps_rule_three(
    tmp_path: Path, repo_with_store: Path
) -> None:
    loaded = load_config(_user_config(tmp_path, DEFAULT_CONFIG))
    assert loaded.storage.cwd_store is True
    assert (
        loaded.resolved_directory()
        == (repo_with_store / _config.PROJECT_DIR_NAME).resolve()
    )


@pytest.mark.parametrize("spelling", ["false", '"false"', '"off"', '"0"'])
def test_cwd_store_false_skips_rule_three(
    tmp_path: Path, repo_with_store: Path, spelling: str
) -> None:
    loaded = load_config(_user_config(tmp_path, f"[storage]\ncwd_store = {spelling}\n"))
    assert loaded.storage.cwd_store is False
    assert (
        loaded.resolved_directory()
        == (tmp_path / "home" / _config.GLOBAL_DIR_NAME).resolve()
    )
    # An explicit cwd is skipped the same way.
    assert (
        loaded.resolved_directory(cwd=repo_with_store)
        == (tmp_path / "home" / _config.GLOBAL_DIR_NAME).resolve()
    )


def test_cwd_store_false_leaves_rules_one_and_two_first(
    tmp_path: Path, repo_with_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit"
    loaded = load_config(
        _user_config(
            tmp_path,
            f'[storage]\ncwd_store = false\ndirectory = "{explicit.as_posix()}"\n',
        )
    )
    assert loaded.resolved_directory() == explicit.resolve()
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(tmp_path / "env"))
    assert loaded.resolved_directory() == (tmp_path / "env").resolve()


def test_a_cwd_store_typo_is_a_located_error(tmp_path: Path) -> None:
    """A security switch whose typo fell back to the loose default would
    read as set while doing nothing."""
    user = _user_config(tmp_path, '[storage]\ncwd_store = "flase"\n')
    with pytest.raises(ValueError, match=r"\[storage\] cwd_store") as excinfo:
        load_config(user)
    assert str(user) in str(excinfo.value)


def test_the_hook_client_follows_a_managed_cwd_store(
    tmp_path: Path,
    repo_with_store: Path,
    managed_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `BETTERMEMORY_DIR` unset the hook client and the shim resolve
    the store through `load_config().resolved_directory()`
    (`_daemon_client.resolved_store_path`), so a pinned `cwd_store =
    false` keeps the repository's store out of them with no code of their
    own. The user's own `cwd_store = true` does not loosen it."""
    default_config = _config.default_config_path()
    default_config.write_text("[storage]\ncwd_store = true\n", encoding="utf-8")
    assert (
        _daemon_client.resolved_store_path()
        == (
            repo_with_store / _config.PROJECT_DIR_NAME / _config.STORE_FILENAME
        ).resolve()
    )

    managed_config_path.write_text("[storage]\ncwd_store = false\n", "utf-8")
    assert (
        _daemon_client.resolved_store_path()
        == (
            tmp_path / "home" / _config.GLOBAL_DIR_NAME / _config.STORE_FILENAME
        ).resolve()
    )
