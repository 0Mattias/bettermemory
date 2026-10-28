"""No command that starts something on a store starts it on a backup
bundle's.

The openers refuse a store file inside a backup bundle
(`backup.bundle_refusal`, `tests/test_bundle_guard.py`). Three commands
act on the store without opening it, and went ahead: `backup create`
wrote a bundle into ``<bundle>/backups/``, `service install` wrote and
loaded a unit whose daemon refuses the store at every start and is
restarted for good, and a detached `up` spawned a daemon only for it to
refuse. Each now refuses first, in the openers' words, with exit 1: the
bundle is left byte for byte as it was and still verifies, nothing is
written beside it, no unit is written and no supervisor command runs, and
no daemon is spawned.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import _daemon_client
from bettermemory.backup import (
    BackupError,
    bundle_refusal,
    create_backup,
    verify_bundle,
)
from bettermemory.cli import _common, service_cmd
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store


def _memory(body: str) -> Memory:
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


def _tree(root: Path) -> dict[str, bytes]:
    """Every entry under `root`, files with their bytes and directories
    empty, so a directory added inside counts as a change."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else b""
        for path in sorted(root.rglob("*"))
    }


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`bettermemory <argv>` in process: its exit code, 0 for a command
    that returns."""
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _never(what: str) -> Callable[..., object]:
    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"{what} ran past the refusal")

    return refused


@pytest.fixture
def bundle(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A verified bundle of a store with one memory, and BETTERMEMORY_DIR
    naming the bundle, as a user who points the CLI at one sets it."""
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        created.put_memory(_memory("alpha"))
    made = Path(create_backup(path, out_dir=tmp_path / "bundles")["bundle"])
    assert verify_bundle(made)["status"] == "ok"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(made))
    return made


def _assert_untouched(bundle: Path, before: dict[str, bytes]) -> None:
    assert _tree(bundle) == before
    assert not (bundle / "backups").exists()
    assert verify_bundle(bundle)["status"] == "ok"


def _assert_the_refusal(text: str) -> None:
    assert "backup bundle" in text
    assert "copy memory.sqlite out of the bundle" in text


# ---------------------------------------------------------------------------
# backup create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "create"],
        ["backup", "create", "--json"],
        ["backup", "create", "--include-key"],
        ["backup", "create", "--out", "{tmp}/elsewhere"],
    ],
)
def test_backup_create_refuses_a_bundle_as_its_store(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Exit 1, the code for a bundle that could not be written; no
    ``backups`` directory in the bundle, and none where --out names."""
    before = _tree(bundle)
    argv = [word.replace("{tmp}", str(tmp_path)) for word in argv]
    assert _run_cli(argv, monkeypatch) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("bettermemory backup create: ")
    _assert_the_refusal(captured.err)
    _assert_untouched(bundle, before)
    assert not (tmp_path / "elsewhere").exists()


def test_create_backup_refuses_a_bundles_file_before_making_anything(
    bundle: Path, tmp_path: Path
) -> None:
    before = _tree(bundle)
    with pytest.raises(BackupError) as raised:
        create_backup(bundle / STORE_FILENAME, out_dir=tmp_path / "out")
    assert str(raised.value) == bundle_refusal(bundle / STORE_FILENAME)
    _assert_untouched(bundle, before)
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# service install
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("flags", [[], ["--no-load"], ["--json"]])
def test_service_install_refuses_a_bundle_and_writes_nothing(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    flags: list[str],
) -> None:
    """Refused before the state directory, the daemon's log or the unit is
    written, before a running daemon is asked to hand over, and before any
    launchctl or systemctl command; the supervisor is never reached (the
    module's seams stand in for the platform, the home and the user)."""
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(service_cmd, "_platform", lambda: platform)
    monkeypatch.setattr(service_cmd, "_home", lambda: home)
    monkeypatch.setattr(service_cmd, "_uid", lambda: 501)
    monkeypatch.setattr(
        service_cmd, "_interpreter", lambda: ("/opt/bm/python", "/opt/bm")
    )
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(subprocess, "run", _never("a supervisor command"))
    monkeypatch.setattr(_daemon_client, "identify", _never("the hand-over"))
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", _never("the hand-over"))
    before = _tree(bundle)
    assert _run_cli(["service", "install", *flags], monkeypatch) == 1
    captured = capsys.readouterr()
    if "--json" in flags:
        payload = json.loads(captured.out)
        assert payload["written"] is False
        assert payload["loaded"] is False
        assert payload["store"] == str(bundle.resolve() / STORE_FILENAME)
        _assert_the_refusal(payload["error"])
    else:
        assert captured.out == ""
        assert captured.err.startswith("bettermemory service: ")
        _assert_the_refusal(captured.err)
    assert not home.exists()
    assert not state_dir.exists()
    assert not (tmp_path / "xdg").exists()
    _assert_untouched(bundle, before)


# ---------------------------------------------------------------------------
# up, detached
# ---------------------------------------------------------------------------


def test_detached_up_refuses_a_bundle_before_spawning(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line and the exit 1 the daemon itself gives a bundle's store
    (`cli_context`), with no daemon spawned to give them."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(state_dir))
    monkeypatch.setattr(_daemon_client, "ensure_daemon", _never("the daemon's start"))
    monkeypatch.setattr(_daemon_client, "_spawn", _never("a spawn"))
    before = _tree(bundle)
    with pytest.raises(SystemExit) as raised:
        _common.cli_context()
    assert raised.value.code == 1
    the_daemons = capsys.readouterr().err
    assert _run_cli(["up"], monkeypatch) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == the_daemons
    _assert_the_refusal(captured.err)
    assert not state_dir.exists()
    _assert_untouched(bundle, before)


def test_detached_up_json_refuses_a_bundle_in_its_error(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(_daemon_client, "ensure_daemon", _never("the daemon's start"))
    monkeypatch.setattr(_daemon_client, "_spawn", _never("a spawn"))
    before = _tree(bundle)
    assert _run_cli(["up", "--json"], monkeypatch) == 1
    failure = json.loads(capsys.readouterr().out)
    store = bundle.resolve() / STORE_FILENAME
    assert failure["state"] == "failed"
    assert failure["running"] is False
    assert failure["exit_status"] is None
    assert failure["store"] == str(store)
    assert failure["error"] == f"bettermemory: {bundle_refusal(store)}"
    _assert_untouched(bundle, before)
