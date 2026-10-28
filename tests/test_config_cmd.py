"""`bettermemory config path|show|check` and the loader fixes under them.

Four contracts, each pinned with the behaviour it promises:

1. **The loader names what it cannot read.** A section that is not a
   table is a ValueError naming the file and the section, for every
   section the loader reads (four of them used to crash with an
   AttributeError the callers' ``(ValueError, OSError)`` catch let
   through, four read as their defaults without a word). A TOML syntax
   error, and a file that is not UTF-8, name the file. A byte-order mark
   is dropped, as the managed loader drops it. ``create=False`` writes
   nothing and still applies the managed file.
2. **`config path`** names every location and what chose it, says so on
   Windows instead of printing a managed path nothing reads, and resolves
   the store from ``BETTERMEMORY_DIR`` alone when the config does not load.
3. **`config show`** labels every setting default, user or managed, and
   names a user value a managed pin overrides.
4. **`config check`** reports what the loader passes over in silence
   (unknown sections and keys with a did-you-mean, fallbacks, clamps) as
   warnings, what stops a load or can never take effect as errors, and a
   pinned user line as a note; exit 0, 1, 2. The shipped default config
   checks clean.

All three commands are read-only: a snapshot of the scratch tree before
and after shows nothing created.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from bettermemory import config as _config
from bettermemory import managed as _managed
from bettermemory.cli import _build_parser, config_cmd
from bettermemory.config import DEFAULT_CONFIG, Config, load_config


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


def _set_fake_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """`~` redirected on every platform (see tests/test_config.py)."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


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


def _check(text: str | None = None) -> dict[str, Any]:
    if text is not None:
        _write_user(text)
    return config_cmd.check_report()


def _codes(report: dict[str, Any]) -> list[tuple[str, str, str | None, str | None]]:
    return [(f["level"], f["code"], f["section"], f["key"]) for f in report["findings"]]


def _only(report: dict[str, Any]) -> dict[str, Any]:
    assert len(report["findings"]) == 1, report["findings"]
    finding: dict[str, Any] = report["findings"][0]
    return finding


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


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


def test_doctor_reads_a_byte_order_mark_as_the_loader_does(tmp_path: Path) -> None:
    """`bettermemory doctor` parses config.toml itself before it loads it.
    Decoded as plain UTF-8 it called a file with a byte-order mark one that
    "does not parse" and no daemon would start on, while the loader, and
    so the daemon, load it."""
    from bettermemory import doctor

    path = _write_user("﻿[security]\nstrict_provenance = true\n")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    ctx = doctor._Context(cwd=tmp_path)
    check = doctor.check_config(ctx)
    assert check.status == doctor.OK, check.summary
    assert ctx.config is not None
    assert ctx.config.security.strict_provenance is True


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


def test_create_false_writes_nothing_and_still_applies_the_managed_file(
    tmp_path: Path, managed_config_path: Path
) -> None:
    missing = tmp_path / "absent" / "config.toml"
    managed_config_path.write_text(
        "[security]\nstrict_provenance = true\n", encoding="utf-8"
    )
    loaded = load_config(missing, create=False)
    assert not missing.parent.exists()
    assert loaded.config_path == missing
    assert loaded.security.strict_provenance is True
    assert loaded.managed_keys == frozenset({"security.strict_provenance"})
    managed_config_path.unlink()
    plain = load_config(missing, create=False)
    assert plain.behavior == Config().behavior
    assert plain.managed_keys == frozenset()


def test_create_false_reads_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[daemon]\nport = 7400\n", encoding="utf-8")
    assert load_config(path, create=False).daemon.port == 7400


def test_a_missing_file_reads_as_the_defaults_it_would_be_written_with(
    tmp_path: Path,
) -> None:
    """``create=False`` reads a missing file as ``{}``; that is the config
    the first-run write would produce, because DEFAULT_CONFIG sets every
    key to its dataclass default."""
    written = tmp_path / "written.toml"
    written.write_text(DEFAULT_CONFIG, encoding="utf-8")
    absent = asdict(load_config(tmp_path / "absent.toml", create=False))
    shipped = asdict(load_config(written))
    absent.pop("config_path")
    shipped.pop("config_path")
    assert absent == shipped


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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_group_is_registered_between_completion_and_up() -> None:
    _, subparsers = _build_parser()
    names = list(subparsers)
    assert names.index("completion") < names.index("config") < names.index("up")


def test_the_group_alone_prints_its_help_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run_cli(["config"], monkeypatch, capsys)
    assert code == 2
    for name in ("path", "show", "check"):
        assert name in out


def test_completion_offers_the_config_subcommands() -> None:
    """Completion walks the parser, so the group needs no list of its own:
    each subcommand is a path of the tree, with its --json."""
    from bettermemory.cli import completion_cmd

    parser, _ = _build_parser()
    tree = {command.path: command for command in completion_cmd.command_tree(parser)}
    assert [name for name, _ in tree["config"].subcommands] == ["path", "show", "check"]
    for name in ("path", "show", "check"):
        strings = {
            s for option in tree[f"config {name}"].options for s in option.strings
        }
        assert "--json" in strings
    # Every name passes the generator's quoting check in every shell.
    for shell in completion_cmd.SHELLS:
        assert "config" in completion_cmd.render(shell, parser)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["path"], ["show"], ["check"], ["check", "--json"]])
def test_no_subcommand_creates_the_config_store_or_keys(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    keys_dir: Path,
) -> None:
    config_dir = tmp_path / "cfg"
    monkeypatch.setattr(_config, "default_config_path", lambda: config_dir / "c.toml")
    store_dir = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    state_dir = tmp_path / "state"
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(state_dir))
    before = _snapshot(tmp_path)
    code, _out, _err = _run_cli(["config", *argv], monkeypatch, capsys)
    assert code == 0
    assert _snapshot(tmp_path) == before
    assert not config_dir.exists()
    assert not store_dir.exists()
    assert not state_dir.exists()
    assert not keys_dir.exists()


# ---------------------------------------------------------------------------
# 2. config path
# ---------------------------------------------------------------------------


def test_path_names_every_location_and_what_chose_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    managed_config_path: Path,
    keys_dir: Path,
) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    _user_path().unlink()
    code, out, _err = _run_cli(["config", "path", "--json"], monkeypatch, capsys)
    assert code == 0
    report = json.loads(out)
    assert report["config"] == {"path": str(_user_path()), "exists": False}
    assert report["managed"]["path"] == str(managed_config_path)
    assert report["managed"]["exists"] is False
    assert report["managed"]["read"] is True
    assert report["store"]["path"] == str(store_dir.resolve())
    assert report["store"]["rule"] == "BETTERMEMORY_DIR"
    assert report["store"]["exists"] is True
    assert report["keys"]["path"] == str(keys_dir)
    assert report["keys"]["source"] == "BETTERMEMORY_KEYS_DIR"
    assert report["state"]["source"] == "BETTERMEMORY_STATE_DIR"
    assert report["error"] is None
    code, text, _err = _run_cli(["config", "path"], monkeypatch, capsys)
    assert code == 0
    assert "(not created yet: the defaults apply)" in text
    assert "(none deployed: nothing is pinned)" in text
    assert f"{store_dir.resolve()}  (BETTERMEMORY_DIR)" in text
    assert "  (BETTERMEMORY_KEYS_DIR; not created yet)" in text


def test_path_says_no_managed_file_is_read_on_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    report = config_cmd.path_report()
    assert report["managed"]["path"] is None
    assert report["managed"]["read"] is False
    assert "Windows" in report["managed"]["note"]
    text = config_cmd.render_path(report)
    assert "managed file  none (no managed file is read on Windows" in text


def test_path_reports_a_managed_file_and_its_pins(managed_config_path: Path) -> None:
    managed_config_path.write_text(
        "[telemetry]\nenabled = false\n[policy]\nforbid_force = true\n",
        encoding="utf-8",
    )
    report = config_cmd.path_report()
    assert report["managed"]["exists"] is True
    assert report["managed"]["pinned"] == ["policy.forbid_force", "telemetry.enabled"]
    assert "(present, pins 2 settings)" in config_cmd.render_path(report)


def test_path_names_the_config_key_rule_and_a_managed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed_config_path: Path
) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    chosen = tmp_path / "chosen"
    _write_user(f'[storage]\ndirectory = "{chosen.as_posix()}"\n')
    store = config_cmd.path_report()["store"]
    assert store["rule"] == "[storage] directory"
    assert store["exists"] is False
    managed_config_path.write_text(
        f'[storage]\ndirectory = "{chosen.as_posix()}"\n', encoding="utf-8"
    )
    store = config_cmd.path_report()["store"]
    assert store["rule"] == "[storage] directory (managed)"
    assert store["path"] == str(chosen.resolve())


def test_path_notes_a_cwd_store_the_switch_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed_config_path: Path
) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    _set_fake_home(monkeypatch, tmp_path / "home")
    project = tmp_path / "repo"
    (project / _config.PROJECT_DIR_NAME).mkdir(parents=True)
    monkeypatch.chdir(project)
    store = config_cmd.path_report()["store"]
    assert store["rule"] == "./.claude-memory"
    assert store["notes"] == []
    managed_config_path.write_text("[storage]\ncwd_store = false\n", encoding="utf-8")
    store = config_cmd.path_report()["store"]
    assert store["rule"] == "home default"
    assert store["path"] == str((tmp_path / "home" / ".claude-memory").resolve())
    assert len(store["notes"]) == 1
    assert "cwd_store = false (managed)" in store["notes"][0]


def test_path_with_a_config_that_does_not_load_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The store comes from BETTERMEMORY_DIR alone then, as the hook
    client reads it; without the variable it is not resolved."""
    _write_user("[daemon]\nport = 'x'\n")
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "s"))
    code, out, _err = _run_cli(["config", "path", "--json"], monkeypatch, capsys)
    assert code == 1
    report = json.loads(out)
    assert "[daemon] port" in report["error"]
    assert report["store"]["rule"] == "BETTERMEMORY_DIR"
    assert report["managed"]["pinned"] is None
    monkeypatch.delenv("BETTERMEMORY_DIR")
    code, out, _err = _run_cli(["config", "path"], monkeypatch, capsys)
    assert code == 1
    assert "(not resolved)" in out
    assert "the config does not load:" in out


# ---------------------------------------------------------------------------
# 3. config show
# ---------------------------------------------------------------------------


def test_show_labels_default_user_and_managed(
    managed_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_user(
        "[behavior]\nsearch_mode = 'bm25'\n"
        "[telemetry]\nenabled = true\n"
        "[security]\nstrict_provenance = true\n"
    )
    managed_config_path.write_text(
        "[telemetry]\nenabled = false\n[security]\nstrict_provenance = true\n",
        encoding="utf-8",
    )
    code, out, _err = _run_cli(["config", "show", "--json"], monkeypatch, capsys)
    assert code == 0
    settings = json.loads(out)["settings"]
    assert settings["behavior"]["search_mode"] == {"value": "bm25", "source": "user"}
    assert settings["behavior"]["conversational"] == {
        "value": True,
        "source": "default",
    }
    # A pin the user's file sets differently names the user's value...
    assert settings["telemetry"]["enabled"] == {
        "value": False,
        "source": "managed",
        "user_value": True,
    }
    # ...and one it sets the same does not.
    assert settings["security"]["strict_provenance"] == {
        "value": True,
        "source": "managed",
    }
    assert settings["scopes"]["allowed"] == {"value": [], "source": "default"}
    code, text, _err = _run_cli(["config", "show"], monkeypatch, capsys)
    assert code == 0
    assert "[telemetry]" in text
    assert "# managed (your config's true is ignored)" in text
    assert 'search_mode = "bm25"' in text
    assert "directory = (not set)" in text


def test_show_lists_every_setting_config_reads() -> None:
    assert tuple(config_cmd.settings_catalogue()) == _SECTIONS
    report = config_cmd.show_report()
    shown = {(s, k) for s, table in report["settings"].items() for k in table}
    expected = {
        (s, k) for s, keys in config_cmd.settings_catalogue().items() for k in keys
    }
    assert shown == expected
    assert all(
        entry["source"] == "user"
        for table in report["settings"].values()
        for key, entry in table.items()
        if key in ("default_max_results", "enabled", "strict_provenance")
    ), "the shipped default config sets these keys itself"


def test_show_counts_a_legacy_spelling_as_the_users(tmp_path: Path) -> None:
    _write_user("[behavior]\nendorsement_debt_ratio_threshold = 0.25\n")
    entry = config_cmd.show_report()["settings"]["behavior"][
        "cold_endorsement_ratio_threshold"
    ]
    assert entry == {"value": 0.25, "source": "user"}


def test_show_with_no_config_file_shows_the_managed_pins(
    managed_config_path: Path,
) -> None:
    """The read-only load applies the managed file over the defaults, so a
    machine with a managed file and no config.toml shows what is in force."""
    _user_path().unlink()
    managed_config_path.write_text("[storage]\ncwd_store = false\n", encoding="utf-8")
    report = config_cmd.show_report()
    assert report["config"]["exists"] is False
    assert report["settings"]["storage"]["cwd_store"] == {
        "value": False,
        "source": "managed",
    }
    assert not _user_path().exists()


def test_show_lists_the_loaders_notices() -> None:
    _write_user("[behavior]\nsearch_mode = 'semantic-unique-show'\n")
    report = config_cmd.show_report()
    assert any("semantic-unique-show" in n for n in report["notices"])
    assert "# notice:" in config_cmd.render_show(report)


def test_show_with_a_config_that_does_not_load_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_user("[daemon]\nport = 'x'\n")
    code, out, err = _run_cli(["config", "show"], monkeypatch, capsys)
    assert code == 1
    assert out == ""
    assert str(path) in err
    assert "config check" in err
    code, out, _err = _run_cli(["config", "show", "--json"], monkeypatch, capsys)
    assert code == 1
    assert "[daemon] port" in json.loads(out)["error"]


# ---------------------------------------------------------------------------
# 4. config check
# ---------------------------------------------------------------------------


def test_the_shipped_default_config_checks_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_user(DEFAULT_CONFIG)
    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 0, out
    assert "config check: ok (0 errors, 0 warnings, 0 notes)" in out


def test_no_files_at_all_check_clean() -> None:
    _user_path().unlink()
    report = _check()
    assert report["status"] == "ok"
    assert report["exit_code"] == 0
    assert report["loads"] is True
    assert report["findings"] == []
    assert [f["exists"] for f in report["files"]] == [False, False]
    assert "nothing to check" in config_cmd.render_check(report)


def test_the_shipped_managed_example_checks_clean(managed_config_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "managed.toml"
    managed_config_path.write_bytes(example.read_bytes())
    _write_user("")
    report = _check()
    assert report["findings"] == []
    assert report["exit_code"] == 0


@pytest.mark.parametrize(
    ("text", "code", "section", "key", "suggestion"),
    [
        (
            "[behaviour]\nconversational = true\n",
            "unknown_section",
            "behaviour",
            None,
            "did you mean [behavior]?",
        ),
        (
            "[Storage]\ncwd_store = false\n",
            "unknown_section",
            "Storage",
            None,
            "did you mean [storage]?",
        ),
        (
            "[telemetry]\nenabeld = false\n",
            "unknown_key",
            "telemetry",
            "enabeld",
            "did you mean enabled?",
        ),
        (
            "[security]\nstrict_provenence = true\n",
            "unknown_key",
            "security",
            "strict_provenence",
            "did you mean strict_provenance?",
        ),
        (
            "[storage]\nport = 7400\n",
            "unknown_key",
            "storage",
            "port",
            "did you mean [daemon] port? Move the line under that section.",
        ),
        (
            "strict_provenance = true\n",
            "unknown_key",
            None,
            "strict_provenance",
            "did you mean [security] strict_provenance? Put the line under its "
            "section header.",
        ),
        ("[telemetry]\nzzqx = 1\n", "unknown_key", "telemetry", "zzqx", None),
    ],
)
def test_unknown_sections_and_keys_are_warnings_with_a_guess(
    text: str, code: str, section: str | None, key: str | None, suggestion: str | None
) -> None:
    report = _check(text)
    finding = _only(report)
    assert (finding["level"], finding["code"]) == ("warning", code)
    assert (finding["section"], finding["key"]) == (section, key)
    assert finding["suggestion"] == suggestion
    assert report["loads"] is True
    assert report["exit_code"] == 2


@pytest.mark.parametrize(
    ("line", "default"),
    [
        ('[behavior]\nconversational = "flase"\n', "true"),
        ("[security]\nstrict_provenance = 1\n", "false"),
        ('[security]\nstrict_provenance = "ture"\n', "false"),
        ("[telemetry]\nenabled = 0\n", "true"),
        ('[behavior]\nsearch_mode = "semantic"\n', '"hybrid"'),
        ('[behavior]\nprompt_recall = "always"\n', '"project"'),
        ("[behavior]\nprompt_recall = 3\n", '"project"'),
    ],
)
def test_a_value_that_falls_back_to_the_default_is_a_warning(
    line: str, default: str
) -> None:
    report = _check(line)
    finding = _only(report)
    assert (finding["level"], finding["code"]) == ("warning", "fallback")
    assert f"the default, {default}, is in force" in finding["message"]
    assert report["loads"] is True
    assert report["exit_code"] == 2


@pytest.mark.parametrize(
    "line",
    [
        '[security]\nstrict_provenance = "yes"\n',
        "[security]\nstrict_provenance = true\n",
        '[behavior]\nconversational = "OFF"\n',
        '[behavior]\nsearch_mode = " BM25 "\n',
        "[behavior]\nprompt_recall = false\n",
        '[behavior]\nprompt_recall = "everywhere"\n',
        '[behavior]\nprompt_recall = "no"\n',
        "[daemon]\nport = 0\n",
        "[daemon]\nport = 7397\n",
        '[scopes]\nallowed = ["projects:foo", "lang-py"]\n',
        '[policy]\nforbid_acknowledge = ["credential", "truncation"]\n',
        "[behavior]\ndefault_max_results = 50\n",
        "[capture]\ncheckpoint_tokens = 500\n",
    ],
)
def test_values_the_loader_reads_as_written_check_clean(line: str) -> None:
    assert _check(line)["findings"] == []


def test_a_value_the_loader_refuses_is_an_error_without_the_file_prefix() -> None:
    report = _check("[daemon]\nport = 'x'\n")
    finding = _only(report)
    assert (finding["level"], finding["code"]) == ("error", "invalid_value")
    assert finding["message"].startswith("[daemon] port = 'x' must be an integer")
    assert str(_user_path()) not in finding["message"]
    assert report["loads"] is False
    assert report["exit_code"] == 1


@pytest.mark.parametrize(
    ("line", "level", "code"),
    [
        ("[daemon]\nport = 70000\n", "error", "port_out_of_range"),
        ("[daemon]\nport = -1\n", "error", "port_out_of_range"),
        ("[daemon]\nport = 80\n", "warning", "privileged_port"),
        ("[behavior]\ndefault_max_results = 80\n", "warning", "clamped"),
        ("[behavior]\ndefault_max_results = 0\n", "warning", "clamped"),
        ("[capture]\ncheckpoint_tokens = 100\n", "warning", "clamped"),
    ],
)
def test_ranges(line: str, level: str, code: str) -> None:
    finding = _only(_check(line))
    assert (finding["level"], finding["code"]) == (level, code)


def test_a_port_out_of_range_loads_but_still_fails_the_check() -> None:
    """The loader takes any integer; the daemon's bind then raises before
    its ephemeral fallback can run, so the check calls it an error."""
    report = _check("[daemon]\nport = 70000\n")
    assert report["loads"] is True
    assert report["exit_code"] == 1


def test_allowed_scopes_outside_the_grammar_are_errors() -> None:
    report = _check('[scopes]\nallowed = ["projects:foo", "Projects:Bar", "a b"]\n')
    assert _codes(report) == [
        ("error", "invalid_scope", "scopes", "allowed"),
        ("error", "invalid_scope", "scopes", "allowed"),
    ]
    first, second = report["findings"]
    assert '"Projects:Bar"' in first["message"]
    assert first["suggestion"] == 'did you mean "projects:bar"?'
    assert '"a b"' in second["message"]
    assert "did you mean" not in second["suggestion"]


def test_a_typo_exception_outside_the_grammar_is_a_warning() -> None:
    finding = _only(_check('[scopes]\ntypo_exceptions = ["Has_Underscore"]\n'))
    assert (finding["level"], finding["code"]) == ("warning", "invalid_scope")
    assert "inert" in finding["message"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX system directories")
def test_a_store_directory_under_a_system_directory_is_a_warning(
    tmp_path: Path,
) -> None:
    """The loader's own test, which it logs only when the store resolves."""
    finding = _only(_check('[storage]\ndirectory = "/etc/bettermemory-store"\n'))
    assert (finding["level"], finding["code"]) == ("warning", "system_directory")
    assert "/etc/bettermemory-store" in finding["message"]
    assert _check(f'[storage]\ndirectory = "{tmp_path.as_posix()}"\n')["findings"] == []


def test_every_unknown_acknowledge_name_is_named() -> None:
    """The loader stops at the first; the check names each, with the
    bare name for a prefixed one and a close match for a misspelling."""
    report = _check(
        "[policy]\nforbid_acknowledge = "
        '["credentials", "acknowledge_transient", "user_claim", "qqq"]\n'
    )
    assert [f["code"] for f in report["findings"]] == ["unknown_acknowledge_name"] * 3
    suggestions = [f["suggestion"] for f in report["findings"]]
    assert suggestions[0] == 'did you mean "credential"?'
    assert suggestions[1] == 'write "transient", without the acknowledge_ prefix'
    assert suggestions[2].startswith("one of credential, transient")
    assert report["loads"] is False


def test_a_forbid_acknowledge_that_is_not_a_list_is_the_loaders_error() -> None:
    finding = _only(_check('[policy]\nforbid_acknowledge = "credential"\n'))
    assert (finding["level"], finding["code"]) == ("error", "invalid_value")
    assert "brackets" in finding["message"]


def test_removed_and_renamed_lines_are_warnings() -> None:
    report = _check(
        "[behavior]\ncorroboration_boost = true\n"
        "endorsement_debt_ratio_threshold = 0.1\n"
        "[telemetry]\nmax_bytes = 10\n"
        "[proposals]\nenabled = true\n"
    )
    assert _codes(report) == [
        ("warning", "renamed", "behavior", "endorsement_debt_ratio_threshold"),
        ("warning", "removed", "behavior", "corroboration_boost"),
        ("warning", "removed", "telemetry", "max_bytes"),
        ("warning", "removed", "proposals", None),
    ]
    assert "8.0.0" in report["findings"][1]["message"]


def test_both_spellings_of_the_renamed_key_name_the_stale_one() -> None:
    report = _check(
        "[behavior]\nendorsement_debt_ratio_threshold = 0.1\n"
        "cold_endorsement_ratio_threshold = 0.2\n"
    )
    finding = _only(report)
    assert (finding["code"], finding["key"]) == (
        "renamed",
        "endorsement_debt_ratio_threshold",
    )
    assert finding["suggestion"] == "delete endorsement_debt_ratio_threshold"


def test_a_renamed_key_with_a_bad_value_is_checked_under_its_new_name() -> None:
    report = _check("[behavior]\nendorsement_debt_ratio_threshold = 'abc'\n")
    assert _codes(report) == [
        ("warning", "renamed", "behavior", "endorsement_debt_ratio_threshold"),
        ("error", "invalid_value", "behavior", "cold_endorsement_ratio_threshold"),
    ]


def test_a_section_that_is_not_a_table_is_an_error_in_the_check() -> None:
    report = _check("security = true\n")
    finding = _only(report)
    assert (finding["level"], finding["code"], finding["section"]) == (
        "error",
        "not_a_table",
        "security",
    )
    assert report["loads"] is False


def test_a_file_that_does_not_parse_is_one_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_user("[behavior\n")
    code, out, _err = _run_cli(["config", "check", "--json"], monkeypatch, capsys)
    assert code == 1
    report = json.loads(out)
    finding = _only(report)
    assert (finding["level"], finding["code"]) == ("error", "unreadable")
    assert str(path) in finding["message"]
    assert report["files"][0]["parses"] is False


def test_a_directory_at_the_config_path_is_unreadable() -> None:
    path = _user_path()
    path.unlink()
    path.mkdir()
    finding = _only(_check())
    assert finding["code"] == "unreadable"
    assert "could not be read" in finding["message"]


def test_the_managed_file_is_checked_too(managed_config_path: Path) -> None:
    """Its unknown section is ignored by the loader (a warning); a scalar
    at its top level stops the load, whatever its name (an error)."""
    _write_user("")
    managed_config_path.write_text(
        "[securty]\nstrict_provenance = true\n", encoding="utf-8"
    )
    finding = _only(_check())
    assert (finding["file"], finding["code"], finding["suggestion"]) == (
        "managed",
        "unknown_section",
        "did you mean [security]?",
    )
    managed_config_path.write_text("strict_provenance = true\n", encoding="utf-8")
    report = _check()
    finding = _only(report)
    assert (finding["file"], finding["level"], finding["code"]) == (
        "managed",
        "error",
        "not_a_table",
    )
    assert report["loads"] is False


def test_a_managed_file_that_is_not_utf8_is_unreadable(
    managed_config_path: Path,
) -> None:
    _write_user("")
    managed_config_path.write_bytes(b'[telemetry]\nenabled = "\xff"\n')
    finding = _only(_check())
    assert (finding["file"], finding["code"]) == ("managed", "unreadable")
    assert str(managed_config_path) in finding["message"]


def test_a_dangling_managed_symlink_is_unreadable(
    tmp_path: Path, managed_config_path: Path
) -> None:
    try:
        managed_config_path.symlink_to(tmp_path / "gone.toml")
    except OSError:
        pytest.skip("this account cannot create symlinks")
    _write_user("")
    finding = _only(_check())
    assert (finding["file"], finding["code"]) == ("managed", "unreadable")
    assert "symlink" in finding["message"]


def test_a_pinned_user_line_is_a_note_and_never_fails_the_check(
    managed_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loader ignores a pinned user line unread, a mistyped one too,
    so the check notes it and does not grade its value."""
    managed_config_path.write_text(
        "[daemon]\nport = 7400\n[telemetry]\nenabled = false\n", encoding="utf-8"
    )
    _write_user("[daemon]\nport = 'x'\n[telemetry]\nenabled = false\n")
    code, out, _err = _run_cli(["config", "check", "--json"], monkeypatch, capsys)
    assert code == 0
    report = json.loads(out)
    finding = _only(report)
    assert (finding["level"], finding["code"], finding["key"]) == (
        "note",
        "pinned",
        "port",
    )
    assert "pins it to 7400" in finding["message"]
    assert report["counts"] == {"error": 0, "warning": 0, "note": 1}


def test_exit_codes_through_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_user("[telemetry]\nenabled = true\n")
    assert _run_cli(["config", "check"], monkeypatch, capsys)[0] == 0
    _write_user("[telemetry]\nenabeld = true\n")
    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 2
    assert "config check: warning (0 errors, 1 warning, 0 notes)" in out
    assert "did you mean enabled?" in out
    _write_user("[daemon]\nport = 70000\n")
    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 1
    assert out.splitlines()[1].startswith("  error    [daemon] port = 70000")


def test_the_json_report_has_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_user("[telemetry]\nenabeld = true\n")
    _code, out, _err = _run_cli(["config", "check", "--json"], monkeypatch, capsys)
    report = json.loads(out)
    assert set(report) == {
        "status",
        "exit_code",
        "loads",
        "load_error",
        "files",
        "counts",
        "findings",
    }
    assert set(report["findings"][0]) == {
        "level",
        "code",
        "file",
        "section",
        "key",
        "message",
        "suggestion",
    }
    assert [f["role"] for f in report["files"]] == ["user", "managed"]


def test_a_load_failure_no_finding_explains_is_a_finding_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that does not load can never check clean, whatever the
    per-line checks model."""

    def broken(*_args: object, **_kwargs: object) -> Config:
        raise TypeError("an unmodelled failure")

    monkeypatch.setattr(_config, "load_config", broken)
    report = config_cmd.check_report()
    finding = _only(report)
    assert (finding["level"], finding["code"], finding["file"]) == (
        "error",
        "does_not_load",
        None,
    )
    assert "TypeError: an unmodelled failure" in finding["message"]
    assert "the config as a whole" in config_cmd.render_check(report)


def test_the_check_on_windows_names_why_no_managed_file_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_managed, "managed_config_path", lambda: None)
    report = _check(DEFAULT_CONFIG)
    assert report["files"][1]["path"] is None
    assert "Windows" in report["files"][1]["note"]
    assert "managed file: none (no managed file is read on Windows" in (
        config_cmd.render_check(report)
    )


def test_text_output_cannot_write_control_characters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A quoted key may hold any character; the report prints it escaped."""
    _write_user('[telemetry]\n"\\u001b[31mred\\u202e" = 1\n')
    code, out, _err = _run_cli(["config", "check"], monkeypatch, capsys)
    assert code == 2
    assert "\x1b" not in out
    assert "‮" not in out
    assert "\\u001b" in out


def test_terminal_safe_keeps_text_and_escapes_controls() -> None:
    assert config_cmd._terminal_safe("a\tb\nc") == "a\tb\nc"
    assert config_cmd._terminal_safe("x\x07y​z") == "x\\u0007y\\u200bz"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_an_unreadable_config_file_is_unreadable_not_a_crash() -> None:
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX file modes")
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    path = _write_user("[telemetry]\nenabled = true\n")
    path.chmod(0o000)
    try:
        finding = _only(_check())
    finally:
        path.chmod(0o600)
    assert finding["code"] == "unreadable"
    assert str(path) in finding["message"]
