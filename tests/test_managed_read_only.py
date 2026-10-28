"""The read-only commands read the config the daemon loads, and write none.

`config.load_config` creates a default config.toml where none exists, and
overlays the organisation-managed file. A command that promises to write
nothing cannot let it create the file, and the fallback several of them
used instead, a bare `Config()` when the file was missing, dropped the
managed file with it: on a freshly provisioned account (a managed file, no
config.toml yet) `security sandbox`, `security audit`, `doctor` and the
service unit reported the home store where the daemon serves the one the
managed file pins. Both are `load_config(create=False)` now: nothing is
written, and the managed pins apply whether or not a user file exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import config as _config
from bettermemory import doctor, posture, sandbox_settings
from bettermemory.backup import create_backup
from bettermemory.cli import service_cmd
from bettermemory.config import PROJECT_DIR_NAME, STORE_FILENAME
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import Store


def _set_fake_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """`~` redirected on every platform (see tests/test_config.py)."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


@pytest.fixture
def no_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config.toml path where no file exists, as on a fresh account."""
    path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(_config, "default_config_path", lambda: path)
    return path


@pytest.fixture
def pinned_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_config_path: Path,
    no_user_config: Path,
) -> Path:
    """A managed file that pins `[storage] directory`, no user config and
    no BETTERMEMORY_DIR: the pinned directory is the store's."""
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    _set_fake_home(monkeypatch, tmp_path / "home")
    pinned = (tmp_path / "srv-bettermemory").resolve()
    managed_config_path.write_text(
        f'[storage]\ndirectory = "{pinned.as_posix()}"\n', encoding="utf-8"
    )
    return pinned


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    return 0


# ---------------------------------------------------------------------------
# the managed pins apply without a user file
# ---------------------------------------------------------------------------


def test_the_sandbox_denies_the_store_the_managed_file_pins(
    pinned_store: Path,
    no_user_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sandbox_settings.load_config_read_only().resolved_directory() == (
        pinned_store
    )
    argv = ["security", "sandbox", "--format", "managed", "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    plan: dict[str, Any] = json.loads(capsys.readouterr().out)
    # Compared as paths, not as a substring of the JSON text: on Windows
    # the text escapes every backslash of `str(path)`, and the plan's other
    # spellings of the directory are POSIX forms.
    assert any(
        loc["role"] == "store" and loc["denied"] and Path(loc["path"]) == pinned_store
        for loc in plan["locations"]
    )
    assert not no_user_config.exists()


def test_the_posture_audit_reads_the_managed_cwd_store_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_config_path: Path,
    no_user_config: Path,
) -> None:
    """A managed `cwd_store = false` keeps a repository's `.claude-memory`
    from being the store; the audit reports the store the daemon would
    serve, not the repository's."""
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    home = tmp_path / "home"
    _set_fake_home(monkeypatch, home)
    repo = tmp_path / "repo"
    (repo / PROJECT_DIR_NAME).mkdir(parents=True)
    managed_config_path.write_text("[storage]\ncwd_store = false\n", encoding="utf-8")
    report = posture.audit(cwd=repo)
    assert report.store == (home / ".claude-memory" / STORE_FILENAME).resolve()
    assert not no_user_config.exists()


def test_doctor_reports_the_store_the_managed_file_pins(
    pinned_store: Path, no_user_config: Path
) -> None:
    checks = {check.id: check for check in doctor.run_checks(cwd=None)}
    assert checks["config"].status == "ok"
    assert checks["store_location"].details["directory"] == str(pinned_store)
    assert checks["store_location"].details["rule"] == "config"
    assert not no_user_config.exists()


def test_doctor_fails_a_managed_file_that_does_not_load(
    tmp_path: Path, managed_config_path: Path, no_user_config: Path
) -> None:
    managed_config_path.write_text("storage = 5\n", encoding="utf-8")
    check = {check.id: check for check in doctor.run_checks(cwd=None)}["config"]
    assert check.status == "fail"
    assert str(managed_config_path) in check.summary
    assert not no_user_config.exists()


def test_the_service_unit_names_the_store_the_managed_file_pins(
    pinned_store: Path, tmp_path: Path, no_user_config: Path
) -> None:
    store = service_cmd._service_store({}, tmp_path / "state")
    assert store == pinned_store / STORE_FILENAME
    assert not no_user_config.exists()


# ---------------------------------------------------------------------------
# no config.toml is written
# ---------------------------------------------------------------------------


def _memory(body: str) -> Memory:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


@pytest.fixture
def store_with_a_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    memory = _memory("a record the read-only commands read")
    with Store.create(directory / STORE_FILENAME) as created:
        created.put_memory(memory)
    return memory.id


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["log", "export"], 0),
        (["log", "anchor"], 0),
        (["log", "verify"], 0),
        (["stats", "--json"], 0),
        (["memory", "history", "{id}"], 0),
        (["security", "scan", "--json"], 0),
        (["backup", "create", "--json"], 0),
    ],
    ids=lambda value: "-".join(value) if isinstance(value, list) else str(value),
)
def test_read_only_commands_write_no_config(
    store_with_a_record: str,
    no_user_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: int,
) -> None:
    argv = [arg.format(id=store_with_a_record) for arg in argv]
    assert _run_cli(argv, monkeypatch) == expected
    captured = capsys.readouterr()
    assert "created default config" not in captured.err
    assert not no_user_config.exists()
    assert not no_user_config.parent.exists()


@pytest.mark.parametrize("diff", [False, True])
def test_memory_history_on_a_copied_store_writes_no_config(
    tmp_path: Path,
    no_user_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    diff: bool,
) -> None:
    """A store copied here and taken over has a leading segment under a
    key this host does not hold. Labelling one of its records reads the
    `strict_provenance` switch, which went through `load_config` with its
    first-run write: `memory history` wrote a default config.toml."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    arrived = _memory("a record written on the machine the store came from")
    with Store.create(
        directory / STORE_FILENAME, keys_dir=tmp_path / "keys-elsewhere"
    ) as elsewhere:
        elsewhere.put_memory(arrived)
    with Store.open_or_create(directory / STORE_FILENAME, strict_provenance=False):
        pass
    argv = ["memory", "history", arrived.id, *(["--diff"] if diff else [])]
    assert _run_cli(argv, monkeypatch) == 0
    captured = capsys.readouterr()
    assert "local" in captured.out
    assert "created default config" not in captured.err
    assert not no_user_config.exists()


def test_a_restore_plan_writes_no_config(
    store_with_a_record: str,
    no_user_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_file = tmp_path / "store" / STORE_FILENAME
    bundle = create_backup(store_file)["bundle"]
    assert _run_cli(["backup", "restore", bundle], monkeypatch) == 3
    assert "created default config" not in capsys.readouterr().err
    assert not no_user_config.exists()


# ---------------------------------------------------------------------------
# a config or a store file that cannot be read is a message, not a traceback
# ---------------------------------------------------------------------------


_READ_ONLY = [
    (["log", "export"], 2),
    (["log", "anchor"], 2),
    (["stats"], 2),
    (["stats", "--json"], 2),
    (["memory", "history", "01J0000000000000000000000A"], 2),
    # `log verify` reads a store it cannot read as one it cannot verify.
    (["log", "verify"], 1),
]


@pytest.mark.parametrize("content", [b'{"a":1}', b""], ids=["not-sqlite", "empty"])
@pytest.mark.parametrize(
    ("argv", "expected"),
    _READ_ONLY,
    ids=lambda value: "-".join(value) if isinstance(value, list) else str(value),
)
def test_a_store_file_that_is_not_a_store_is_reported_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    argv: list[str],
    expected: int,
) -> None:
    """Exit 1 is a finding for `memory history` and a closed stdout for
    `log export`: a traceback's exit 1 on a damaged store file read as
    either. The reason goes to stderr with the code each command gives
    for a store it cannot read."""
    directory = tmp_path / "store"
    directory.mkdir()
    (directory / STORE_FILENAME).write_bytes(content)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    assert _run_cli(argv, monkeypatch) == expected
    err = capsys.readouterr().err
    assert "cannot read the store at" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [*_READ_ONLY, (["backup", "create"], 2), (["security", "scan"], 2)],
    ids=lambda value: "-".join(value) if isinstance(value, list) else str(value),
)
def test_a_config_that_does_not_load_is_reported_not_raised(
    store_with_a_record: str,
    no_user_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: int,
) -> None:
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text("storage = 5\n", encoding="utf-8")
    assert _run_cli(argv, monkeypatch) == expected
    err = capsys.readouterr().err
    assert "the config does not load" in err
    assert str(no_user_config) in err
    assert "Traceback" not in err
