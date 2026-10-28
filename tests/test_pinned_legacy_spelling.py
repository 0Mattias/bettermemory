"""The pinned-key notice reads a legacy spelling as the loader reads it.

A user line for a key the managed file pins is logged once as ignored,
unless it restates the pin or the code's default (`config._apply_managed`).
Releases v3.41.0 to v8.0.0 wrote ``prompt_recall = true`` into every
config.toml they created, and the loader reads that boolean as
``"project"``, the default; compared as written, it drew the notice under
a managed ``prompt_recall`` pin on every process, every hook among them,
for a line the user never chose. What is pinned here:

* a spelling of the default that the key's own coercion reads as the
  default (the legacy boolean, its string spellings, the mode in another
  case) restates it, and a spelling of the pinned value restates the pin;
* a spelling that reads as another mode still draws the notice, and so
  does one the coercion does not recognise, without the coercion's own
  fallback warning, since the line is never read;
* keys with no legacy spelling are compared as written, as before;
* the readers in `config._LEGACY_SPELLINGS` read every value as the
  loader does, so the notice and the load cannot drift apart.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bettermemory import config as _config
from bettermemory.config import load_config


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def _load(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    *,
    managed: str,
    user: str,
) -> tuple[_config.Config, list[str]]:
    managed_config_path.write_text(managed, "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, user))
    return loaded, _messages(caplog, "pins")


@pytest.mark.parametrize(
    "spelling",
    ["true", '"true"', '"yes"', '"on"', '"1"', '"project"', '"Project"', '" project "'],
)
def test_a_legacy_spelling_of_the_default_draws_no_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    spelling: str,
) -> None:
    loaded, notices = _load(
        tmp_path,
        managed_config_path,
        caplog,
        managed='[behavior]\nprompt_recall = "off"\n',
        user=f"[behavior]\nprompt_recall = {spelling}\n",
    )
    assert loaded.behavior.prompt_recall == "off"
    assert loaded.managed_keys == frozenset({"behavior.prompt_recall"})
    assert notices == []


@pytest.mark.parametrize(
    ("managed", "spelling"),
    [
        ('"off"', "false"),
        ('"off"', '"no"'),
        ('"off"', '"OFF"'),
        ('"everywhere"', '"Everywhere"'),
        # The managed file's own legacy spelling is read as it loads.
        ("false", '"off"'),
        ("false", '"0"'),
    ],
)
def test_a_legacy_spelling_of_the_pinned_value_draws_no_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    spelling: str,
) -> None:
    _loaded, notices = _load(
        tmp_path,
        managed_config_path,
        caplog,
        managed=f"[behavior]\nprompt_recall = {managed}\n",
        user=f"[behavior]\nprompt_recall = {spelling}\n",
    )
    assert notices == []


@pytest.mark.parametrize(
    ("managed", "spelling"),
    [
        ('"everywhere"', "false"),
        ('"everywhere"', '"no"'),
        ('"off"', '"everywhere"'),
        ('"project"', "false"),
    ],
)
def test_a_spelling_of_another_mode_still_draws_the_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    spelling: str,
) -> None:
    loaded, notices = _load(
        tmp_path,
        managed_config_path,
        caplog,
        managed=f"[behavior]\nprompt_recall = {managed}\n",
        user=f"[behavior]\nprompt_recall = {spelling}\n",
    )
    assert loaded.behavior.prompt_recall == managed.strip('"')
    assert len(notices) == 1, notices
    assert "[behavior] `prompt_recall`" in notices[0]


@pytest.mark.parametrize("spelling", ['"projetc"', "1", "[]", '"semantic"'])
def test_an_unrecognised_value_draws_the_notice_and_no_fallback_warning(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    spelling: str,
) -> None:
    """The coercion would replace it with the default, loudly, were the
    line read; it is not, so the pin's notice is its one word."""
    _loaded, notices = _load(
        tmp_path,
        managed_config_path,
        caplog,
        managed='[behavior]\nprompt_recall = "off"\n',
        user=f"[behavior]\nprompt_recall = {spelling}\n",
    )
    assert len(notices) == 1, notices
    assert _messages(caplog, "falling back") == []


@pytest.mark.parametrize(
    ("managed", "user"),
    [
        # A boolean's string spelling stays a coercion the user chose.
        ("[telemetry]\nenabled = false\n", '[telemetry]\nenabled = "true"\n'),
        # The pre-4.0 mode 4.0 removed falls back as a typo does.
        (
            '[behavior]\nsearch_mode = "bm25"\n',
            '[behavior]\nsearch_mode = "semantic"\n',
        ),
        # 2.x wrote "keyword", a mode of its own.
        ('[behavior]\nsearch_mode = "bm25"\n', '[behavior]\nsearch_mode = "keyword"\n'),
        # Compared as written: another case is not the default's spelling.
        ('[behavior]\nsearch_mode = "bm25"\n', '[behavior]\nsearch_mode = "Hybrid"\n'),
    ],
)
def test_keys_without_a_legacy_spelling_are_compared_as_written(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    user: str,
) -> None:
    _loaded, notices = _load(
        tmp_path, managed_config_path, caplog, managed=managed, user=user
    )
    assert len(notices) == 1, notices


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "true",
        "False",
        " yes ",
        "no",
        "on",
        "off",
        "1",
        "0",
        "",
        "project",
        "EVERYWHERE",
        " off ",
        "projetc",
        "semantic",
        1,
        0,
        2.5,
        [],
        {},
    ],
)
def test_the_legacy_readers_read_as_the_loader_does(
    value: object, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every reader agrees with `_config_from_data` on every value it
    recognises, and recognises none the loader would fall back on."""
    assert _config._LEGACY_SPELLINGS
    for (section, key), read in _config._LEGACY_SPELLINGS.items():
        with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
            caplog.clear()
            loaded = _config._config_from_data(
                {section: {key: value}}, tmp_path / "config.toml"
            )
        spelled = read(value)
        fell_back = bool(_messages(caplog, "falling back"))
        if spelled is None:
            assert fell_back, (section, key, value)
        else:
            assert not fell_back, (section, key, value)
            assert getattr(getattr(loaded, section), key) == spelled


def test_without_a_managed_file_the_legacy_boolean_loads_as_before(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(
            _user_config(tmp_path, "[behavior]\nprompt_recall = false\n")
        )
    assert loaded.behavior.prompt_recall == "off"
    assert loaded.managed_keys == frozenset()
    assert _messages(caplog, "pins") == []
