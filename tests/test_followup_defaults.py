"""Two follow-ups on defaults, each pinned with the
behaviour it promises:

1. **The pinned-key notice is for a choice.** A user line for a key the
   managed file pins is logged once as ignored, unless the line spells the
   code's own default for the key: it restates the default and expresses
   no choice the pin overrides. `config check` and `config show` still
   report the line, since they read the files themselves.
2. **The first-run config.toml pins nothing.** Every setting in
   `DEFAULT_CONFIG` is commented out with its prose kept, so a fresh
   install loads exactly `Config()` and a later release's changed default
   reaches it. A file that exists is never rewritten.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

import pytest
import tomllib

from bettermemory import config as _config
from bettermemory.cli import config_cmd
from bettermemory.config import DEFAULT_CONFIG, Config, load_config


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _pin_notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "pins" in r.getMessage()]


# ---------------------------------------------------------------------------
# 1. The pinned-key notice
# ---------------------------------------------------------------------------


def test_a_pinned_line_restating_the_default_draws_no_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`[telemetry] enabled = true` is in every config.toml an earlier
    release wrote, and it is the default: a managed file that pins
    telemetry off overrides it without a notice on every process."""
    managed_config_path.write_text("[telemetry]\nenabled = false\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, "[telemetry]\nenabled = true\n"))
    assert loaded.telemetry.enabled is False
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})
    assert _pin_notices(caplog) == []


def test_a_pinned_line_choosing_another_value_still_draws_the_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text("[daemon]\nport = 7400\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, "[daemon]\nport = 7401\n"))
    assert loaded.daemon.port == 7400
    notices = _pin_notices(caplog)
    assert len(notices) == 1, notices
    assert "[daemon] `port`" in notices[0]


@pytest.mark.parametrize(
    ("managed", "user", "notice"),
    [
        # The default spelled as written in an earlier DEFAULT_CONFIG.
        ("[daemon]\nport = 7400\n", "[daemon]\nport = 7397\n", False),
        # An int restates a float default of the same value.
        (
            "[behavior]\nrecency_boost_half_life_days = 7.0\n",
            "[behavior]\nrecency_boost_half_life_days = 30\n",
            False,
        ),
        ('[scopes]\nallowed = ["corp"]\n', "[scopes]\nallowed = []\n", False),
        (
            '[behavior]\nsearch_mode = "bm25"\n',
            '[behavior]\nsearch_mode = "hybrid"\n',
            False,
        ),
        # A boolean restates only a boolean, though Python takes False == 0.
        (
            "[behavior]\ntombstone_retention_days = 9\n",
            "[behavior]\ntombstone_retention_days = false\n",
            True,
        ),
        # A spelling the loader would coerce is the user's choice of it.
        ("[telemetry]\nenabled = false\n", '[telemetry]\nenabled = "true"\n', True),
        # The legacy prompt_recall boolean is not the default's spelling.
        (
            '[behavior]\nprompt_recall = "off"\n',
            "[behavior]\nprompt_recall = true\n",
            True,
        ),
    ],
)
def test_only_a_line_spelling_the_code_default_is_a_restatement(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    user: str,
    notice: bool,
) -> None:
    managed_config_path.write_text(managed, "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        load_config(_user_config(tmp_path, user))
    assert bool(_pin_notices(caplog)) is notice, _pin_notices(caplog)


def test_check_and_show_still_report_a_pinned_line_restating_the_default(
    managed_config_path: Path,
) -> None:
    """The commands read the files, not the log: the restating line is
    still a `pinned` note in `config check` and the user's value is still
    named in `config show`."""
    managed_config_path.write_text("[telemetry]\nenabled = false\n", "utf-8")
    _config.default_config_path().write_text(
        "[telemetry]\nenabled = true\n", encoding="utf-8"
    )
    report = config_cmd.check_report()
    pinned = [f for f in report["findings"] if f["code"] == "pinned"]
    assert [(f["level"], f["section"], f["key"]) for f in pinned] == [
        ("note", "telemetry", "enabled")
    ]
    assert report["exit_code"] == 0
    entry = config_cmd.show_report()["settings"]["telemetry"]["enabled"]
    assert entry == {"value": False, "source": "managed", "user_value": True}


# ---------------------------------------------------------------------------
# 2. The first-run config.toml
# ---------------------------------------------------------------------------


def _setting_keys() -> list[tuple[str, str]]:
    """Every (section, key) `Config` reads, from its dataclasses."""
    keys: list[tuple[str, str]] = []
    for section in dataclasses.fields(Config):
        value = getattr(Config(), section.name)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            keys.extend((section.name, f.name) for f in dataclasses.fields(value))
    return keys


def test_the_shipped_config_sets_no_key() -> None:
    """Each section header stays, with nothing under it but comments."""
    parsed = tomllib.loads(DEFAULT_CONFIG)
    assert set(parsed) == {section for section, _ in _setting_keys()}
    assert all(table == {} for table in parsed.values()), parsed


def test_every_setting_is_in_the_shipped_config_commented_out() -> None:
    """A user who opens the file finds every setting, under its section,
    ready to uncomment."""
    for section, key in _setting_keys():
        block = DEFAULT_CONFIG.split(f"\n[{section}]\n")[1].split("\n[")[0]
        assert f"\n# {key} = " in block, (section, key)
        assert f"\n{key} =" not in block, (section, key)


def test_the_shipped_config_loads_as_exactly_the_code_defaults(
    tmp_path: Path,
) -> None:
    loaded = load_config(_user_config(tmp_path, DEFAULT_CONFIG))
    assert dataclasses.replace(loaded, config_path=None) == Config()


def test_a_fresh_install_follows_a_changed_code_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the first-run file restates a default, so the default a
    release ships is the one in force. The loader's default for `[daemon]
    port` is patched here to stand in for a release that changes it."""
    path = tmp_path / "config.toml"
    load_config(path)
    real = _config._coerce_int

    def shifted(value: object, default: int, **kwargs: Any) -> int:
        if kwargs.get("label") == "[daemon] port":
            default = 7500
        return real(value, default, **kwargs)

    monkeypatch.setattr(_config, "_coerce_int", shifted)
    assert load_config(path).daemon.port == 7500


def test_the_first_run_writes_the_commented_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    load_config(path)
    assert path.read_text(encoding="utf-8") == DEFAULT_CONFIG


def test_an_existing_config_is_never_rewritten(tmp_path: Path) -> None:
    """A config.toml an earlier release wrote, every key spelled at that
    release's default, is read as it is and left byte for byte."""
    earlier = (
        "# bettermemory config\n[behavior]\ndefault_max_results = 5\n"
        "search_mode = 'keyword'\n[daemon]\nport = 7397\n"
    )
    path = _user_config(tmp_path, earlier)
    before = path.stat().st_mtime_ns
    loaded = load_config(path)
    assert loaded.behavior.search_mode == "keyword"
    assert path.read_text(encoding="utf-8") == earlier
    assert path.stat().st_mtime_ns == before
