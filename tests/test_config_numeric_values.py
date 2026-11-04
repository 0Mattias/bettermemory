"""A numeric config key refuses a boolean, and `config check` shows a
value as the line writes it (ENG-221).

Python's ``bool`` is an ``int``, so the loader's numeric coercions read
``port = true`` as 1 and ``ratio = false`` as 0.0 without a word, and
`bettermemory config check`, which parses each line through the loader,
found nothing. A boolean is now refused as a string that is no number is
refused: the loader raises its located ValueError, so the config does not
load (in config.toml and in the managed file alike), and the check reports
an ``invalid_value`` error in the same words.

The keys are read from the schema, the dataclasses `Config` is built from
(`config_cmd.settings_catalogue` and their annotations), not listed here,
so a numeric key a later release adds is covered the moment it exists,
however the loader reads it.

A ``clamped`` finding, and a port's, printed the value as parsed (a quoted
``"inf"`` as ``inf``) where ``invalid_value`` prints it as written; every
finding now prints it as written.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import pytest

from bettermemory import config as _config
from bettermemory.cli import config_cmd
from bettermemory.config import Config, load_config


def _numeric_keys() -> list[tuple[str, str, str]]:
    """``(section, key, expected)`` for every setting `Config` types as a
    number, ``expected`` the words the loader's refusal ends with: an
    integer for an ``int`` (alone or in a union), a number for a
    ``float``. A ``bool`` is no number here, though Python counts it as
    one."""
    defaults = Config()
    found: list[tuple[str, str, str]] = []
    for section, keys in config_cmd.settings_catalogue().items():
        hints = typing.get_type_hints(type(getattr(defaults, section)))
        for key in keys:
            hint = hints[key]
            kinds = set(typing.get_args(hint)) or {hint}
            if int in kinds:
                found.append((section, key, "an integer"))
            elif float in kinds:
                found.append((section, key, "a number"))
    return found


NUMERIC = _numeric_keys()
_IDS = [f"{section}.{key}" for section, key, _ in NUMERIC]


def test_the_schema_yields_the_numeric_keys(tmp_path: Path) -> None:
    """The walk finds integers and floats in more than one section, so a
    derivation that found none, or only some, cannot pass the table
    below by covering nothing."""
    assert ("daemon", "port", "an integer") in NUMERIC
    assert ("capture", "checkpoint_tokens", "an integer") in NUMERIC
    assert ("behavior", "cold_endorsement_ratio_threshold", "a number") in NUMERIC
    assert len({section for section, _, _ in NUMERIC}) >= 3
    # Each one is a number the loader reads: its default, written out,
    # loads as the default.
    defaults = Config()
    for section, key, _ in NUMERIC:
        default = getattr(getattr(defaults, section), key)
        assert type(default) in (int, float), (section, key)
        loaded = _config._config_from_data(
            {section: {key: default}}, tmp_path / "config.toml"
        )
        assert getattr(getattr(loaded, section), key) == default


def _refusal(section: str, key: str, spelled: str, expected: str) -> str:
    return f"[{section}] {key} = {spelled} must be {expected}"


@pytest.mark.parametrize("spelled", ["true", "false"])
@pytest.mark.parametrize(("section", "key", "expected"), NUMERIC, ids=_IDS)
def test_a_boolean_for_a_numeric_key_does_not_load(
    tmp_path: Path, section: str, key: str, expected: str, spelled: str
) -> None:
    """Refused as a string that is no number is: the same located error,
    the value spelled as the file spells it."""
    path = tmp_path / "config.toml"
    path.write_text(f"[{section}]\n{key} = {spelled}\n", encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        load_config(path)
    assert str(refused.value) == (
        f"malformed config in {path}: {_refusal(section, key, spelled, expected)}"
    )

    word = '"x"'
    other = tmp_path / "other.toml"
    other.write_text(f"[{section}]\n{key} = {word}\n", encoding="utf-8")
    with pytest.raises(ValueError) as string:
        load_config(other)
    assert str(string.value) == (
        f"malformed config in {other}: {_refusal(section, key, word, expected)}"
    )


@pytest.mark.parametrize("spelled", ["true", "false"])
@pytest.mark.parametrize(("section", "key", "expected"), NUMERIC, ids=_IDS)
def test_config_check_reports_a_boolean_for_a_numeric_key(
    section: str, key: str, expected: str, spelled: str
) -> None:
    _config.default_config_path().write_text(
        f"[{section}]\n{key} = {spelled}\n", encoding="utf-8"
    )
    report = config_cmd.check_report()
    assert [_essentials(f) for f in report["findings"]] == [
        {
            "level": "error",
            "code": "invalid_value",
            "file": "user",
            "section": section,
            "key": key,
            "message": _refusal(section, key, spelled, expected)
            + "; the config does not load.",
        }
    ]
    assert report["loads"] is False
    assert report["exit_code"] == config_cmd.EXIT_ERROR


@pytest.mark.parametrize(("section", "key", "expected"), NUMERIC, ids=_IDS)
def test_a_boolean_in_the_managed_file_stops_the_load(
    managed_config_path: Path, section: str, key: str, expected: str
) -> None:
    """A pin the loader cannot read fails the load, a boolean among them,
    and the check names the managed file's line."""
    managed_config_path.write_text(f"[{section}]\n{key} = true\n", encoding="utf-8")
    user = _config.default_config_path()
    user.write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        load_config(user)
    assert str(refused.value) == (
        f"malformed config in {managed_config_path}: "
        f"{_refusal(section, key, 'true', expected)}"
    )
    report = config_cmd.check_report()
    assert [_essentials(f) for f in report["findings"]] == [
        {
            "level": "error",
            "code": "invalid_value",
            "file": "managed",
            "section": section,
            "key": key,
            "message": _refusal(section, key, "true", expected)
            + "; the config does not load.",
        }
    ]
    assert report["loads"] is False


def _essentials(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        k: finding[k] for k in ("level", "code", "file", "section", "key", "message")
    }


@pytest.mark.parametrize(
    ("section", "key", "spelled", "code"),
    [
        ("behavior", "default_max_results", '"80"', "clamped"),
        ("behavior", "default_max_results", "0.5", "clamped"),
        ("capture", "checkpoint_tokens", '"100"', "clamped"),
        ("capture", "checkpoint_tokens", "100.9", "clamped"),
        ("behavior", "cold_endorsement_ratio_threshold", '"inf"', "clamped"),
        ("behavior", "cold_endorsement_ratio_threshold", '" 1.5"', "clamped"),
        ("daemon", "port", '"80"', "privileged_port"),
        ("daemon", "port", "70000.5", "port_out_of_range"),
        ("daemon", "port", '"x"', "invalid_value"),
    ],
)
def test_a_finding_shows_the_value_as_written(
    section: str, key: str, spelled: str, code: str
) -> None:
    """Each line here loads as a number spelled otherwise (``"inf"`` as
    ``inf``, ``0.5`` as 0); the finding names the line as the file writes
    it, as the loader's refusal (the last) does."""
    _config.default_config_path().write_text(
        f"[{section}]\n{key} = {spelled}\n", encoding="utf-8"
    )
    (finding,) = config_cmd.check_report()["findings"]
    assert finding["code"] == code
    assert finding["message"].startswith(f"[{section}] {key} = {spelled} "), finding
