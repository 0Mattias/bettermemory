"""The settings 9.0.0rc20 took off the config file (ST-1).

Eleven `[behavior]` keys are fixed values now (`config.FIXED_SETTINGS`):
no user decides them, so the parser, the `config` commands and the shipped
config no longer offer them. A config.toml or managed file that still
carries one loads without error and without effect; `bettermemory config
check` names each such line once, as a note (exit 0), with the fixed value,
and says when the file's value is not used. No other command and no log
line mentions it, and a managed file's line pins nothing.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

import pytest

from bettermemory import config as _config
from bettermemory.cli import config_cmd
from bettermemory.config import FIXED_SETTINGS, Config, load_config

#: Every fixed key, each written with a value other than its fixed one
#: except `conversational`, which restates it.
_CARRIED = (
    "[behavior]\n"
    "default_max_results = 9\n"
    "recency_boost_half_life_days = 7\n"
    "conversational = true\n"
    'search_mode = "bm25"\n'
    "heavily_used_min_applied = 1\n"
    "cold_endorsement_ratio_threshold = 0.1\n"
    "verification_stale_days = 0\n"
    "max_content_bytes = 0\n"
    "max_takeaway_bytes = 0\n"
    "max_scopes_per_write = 0\n"
    'min_content_tokens = "six"\n'
)


def _run_cli(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    out, err = capsys.readouterr()
    code = exc.value.code
    return (code if isinstance(code, int) else 0), out, err


def test_every_fixed_value_is_the_default_it_replaces() -> None:
    defaults = Config()
    assert len(FIXED_SETTINGS) == 11
    for (section, key), fixed in FIXED_SETTINGS.items():
        default = getattr(getattr(defaults, section), key)
        assert type(default) is type(fixed) and default == fixed, (key, default)


def test_no_surface_offers_a_fixed_key() -> None:
    catalogue = config_cmd.settings_catalogue()
    assert sum(len(keys) for keys in catalogue.values()) == 18
    for section, key in FIXED_SETTINGS:
        assert key not in catalogue[section]
        assert f"{key} =" not in _config.DEFAULT_CONFIG
    assert set(catalogue["behavior"]) == {
        "write_supersession",
        "prompt_recall",
        "session_brief",
        "tombstone_retention_days",
    }


def test_a_file_carrying_every_fixed_key_loads_silently_as_the_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_CARRIED, encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="bettermemory"):
        loaded = load_config(path)
    assert dataclasses.replace(loaded, config_path=None) == Config()
    assert caplog.records == []


def test_config_check_names_each_fixed_line_once_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config.default_config_path().write_text(_CARRIED, encoding="utf-8")
    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 0, out
    for section, key in FIXED_SETTINGS:
        assert out.count(f"[{section}] {key} is no longer a setting") == 1, key
    assert (
        '[behavior] search_mode is no longer a setting: its value is fixed at "hybrid". '
        'This file\'s "bm25" is not used.'
    ) in out
    # A line restating the fixed value is still named, with nothing unused.
    assert (
        "[behavior] conversational is no longer a setting: its value is fixed "
        "at true.\n"
    ) in out
    assert "config check: ok (0 errors, 0 warnings, 11 notes)" in out

    report = config_cmd.check_report()
    fixed = [f for f in report["findings"] if f["code"] == "fixed"]
    assert {(f["level"], f["file"]) for f in fixed} == {("note", "user")}
    assert len(fixed) == 11
    assert report["loads"] is True


def test_no_other_command_mentions_a_fixed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config.default_config_path().write_text(_CARRIED, encoding="utf-8")
    for argv in (["config", "show"], ["config", "path"], ["config", "show", "--json"]):
        code, out, err = _run_cli(argv, monkeypatch, capsys)
        assert code == 0, (argv, out, err)
        for _section, key in FIXED_SETTINGS:
            assert key not in out + err, (argv, key)


def test_a_managed_file_pinning_a_fixed_key_is_reported_not_enforced(
    tmp_path: Path,
    managed_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The managed file's line pins nothing: the fixed value is in force,
    `managed_keys` does not list it, and `config check` names it in the
    managed file as it would in config.toml."""
    managed_config_path.write_text(
        "[behavior]\nmax_content_bytes = 5\n[telemetry]\nenabled = false\n",
        encoding="utf-8",
    )
    _config.default_config_path().write_text(
        "[behavior]\nmax_content_bytes = 7\n", encoding="utf-8"
    )
    loaded = load_config(_config.default_config_path(), create=False)
    assert loaded.behavior.max_content_bytes == 1_000_000
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})

    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 0, out
    report = config_cmd.check_report()
    fixed = [
        (f["file"], f["message"]) for f in report["findings"] if f["code"] == "fixed"
    ]
    assert fixed == [
        (
            "user",
            "[behavior] max_content_bytes is no longer a setting: its value is "
            "fixed at 1000000. This file's 7 is not used.",
        ),
        (
            "managed",
            "[behavior] max_content_bytes is no longer a setting: its value is "
            "fixed at 1000000. This file's 5 is not used.",
        ),
    ]
    assert not [f for f in report["findings"] if f["code"] == "pinned"]


@pytest.mark.parametrize(
    ("line", "unused"),
    [
        ('search_mode = "HYBRID"', False),
        ('search_mode = " hybrid "', False),
        # rc19 read an unknown mode as its fallback, "hybrid".
        ('search_mode = "semantic"', False),
        ('search_mode = "bm25"', True),
        ('conversational = "true"', False),
        ('conversational = "yes"', False),
        ('conversational = "off"', True),
        ("default_max_results = 5.0", False),
        ('default_max_results = "5"', False),
        ("default_max_results = 6", True),
        ("default_max_results = true", True),
        ("recency_boost_half_life_days = 30", False),
        ('max_content_bytes = "big"', True),
    ],
)
def test_the_note_says_not_used_only_when_rc19_read_another_value(
    line: str, unused: bool
) -> None:
    _config.default_config_path().write_text(f"[behavior]\n{line}\n", encoding="utf-8")
    (finding,) = config_cmd.check_report()["findings"]
    assert finding["code"] == "fixed"
    assert ("is not used" in finding["message"]) is unused, finding["message"]


def test_config_path_counts_the_managed_lines_that_pin_nothing(
    managed_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed_config_path.write_text(
        "[behavior]\nmax_content_bytes = 100\nmin_content_tokens = 5\n"
        "[telemetry]\nenabled = false\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.DEBUG, logger="bettermemory.config"):
        code, out, _err = _run_cli(["config", "path"], monkeypatch, capsys)
    assert code == 0
    assert caplog.records == []
    (line,) = [row for row in out.splitlines() if row.startswith("managed file")]
    assert line.endswith(
        "(present, pins 1 setting; 2 lines pin nothing, no longer settings: "
        "`bettermemory security audit` names them)"
    ), line
    report = config_cmd.path_report()
    assert report["managed"]["pinned"] == ["telemetry.enabled"]
    assert report["managed"]["pins_nothing"] == [
        "behavior.max_content_bytes",
        "behavior.min_content_tokens",
    ]
