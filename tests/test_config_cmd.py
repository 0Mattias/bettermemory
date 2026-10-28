"""The loader names what it cannot read, and the two loaders agree.

A section that is not a table is a ValueError naming the file and the
section, for every section the loader reads: four of them used to crash
with an AttributeError the callers' ``(ValueError, OSError)`` catch let
through, and four read as their defaults without a word. A TOML syntax
error, and a file that is not UTF-8, name the file. A byte-order mark is
dropped, as the managed loader drops it.
"""

from __future__ import annotations

import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from bettermemory import config as _config
from bettermemory.config import Config, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_path() -> Path:
    """The config.toml the commands read: the conftest's scratch file,
    which starts out holding the shipped defaults."""
    return _config.default_config_path()


def _write_user(text: str) -> Path:
    path = _user_path()
    path.write_text(text, encoding="utf-8")
    return path


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


# ---------------------------------------------------------------------------
# 1. The loader
# ---------------------------------------------------------------------------


#: Every section `Config` reads.
_SECTIONS = (
    "storage",
    "behavior",
    "scopes",
    "policy",
    "telemetry",
    "daemon",
    "capture",
    "membership",
    "security",
)


@pytest.mark.parametrize("section", _SECTIONS)
def test_a_section_that_is_not_a_table_is_a_located_value_error(
    tmp_path: Path, section: str
) -> None:
    """Every section the loader reads refuses a scalar where its table
    belongs with a ValueError, never an AttributeError and never a silent
    default, and the message names the file and the section."""
    path = tmp_path / "config.toml"
    path.write_text(f"{section} = 5\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert f"[{section}] = 5 must be a table" in message


def test_the_section_list_covers_every_section_config_reads() -> None:
    """The parametrisation above names every section `Config` holds, so a
    section a later release adds is pinned here too."""
    defaults = Config()
    held = {f.name for f in fields(defaults) if is_dataclass(getattr(defaults, f.name))}
    assert held == set(_SECTIONS)


def test_a_toml_syntax_error_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[behavior\nconversational = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed config in") as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_a_file_that_is_not_utf8_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'[behavior]\nsearch_mode = "\xff"\n')
    with pytest.raises(ValueError, match="malformed config in") as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_a_byte_order_mark_is_dropped_as_the_managed_loader_drops_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf[security]\nstrict_provenance = true\n")
    assert load_config(path).security.strict_provenance is True


def test_the_two_loaders_agree_on_a_section_that_is_not_a_table(
    tmp_path: Path, managed_config_path: Path
) -> None:
    """The same line fails both files, each error naming its own file."""
    user = tmp_path / "config.toml"
    user.write_text("storage = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="storage") as from_user:
        load_config(user)
    assert str(user) in str(from_user.value)
    user.write_text("", encoding="utf-8")
    managed_config_path.write_text("storage = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="storage") as from_managed:
        load_config(user)
    assert str(managed_config_path) in str(from_managed.value)


def test_a_command_that_catches_a_bad_config_now_reports_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`security sandbox` catches ``(ValueError, OSError)`` around the
    load; a non-table section used to escape it as an AttributeError
    traceback. It now exits 1 with a message naming the file."""
    path = _write_user("storage = 5\n")
    code, _out, err = _run_cli(["security", "sandbox"], monkeypatch, capsys)
    assert code == 1
    assert str(path) in err
    assert "[storage]" in err
