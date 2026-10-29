"""One key state, read the same way by `Store.open`, doctor and posture (ENG-174).

A store file's key is in one of six states: present (it matches the
fingerprint the store records), unreadable (there, and this account cannot
read it), missing, foreign (another key), bound elsewhere (the key ring
belongs to another store file that still holds the id, so every owner's
open of this one is read-only), or unclaimed (the ring is bound to no
file and records a history this one does not carry, ENG-93 item 6). `store_keys.key_state` answers it
and words it; `Store.open` keeps the answer (`Store.key_state`, and the
refusal of a write), doctor's `key` check renders it and posture's
`key.match` finding renders it, each with the same summary and remedy.

Before, doctor read a copy of a store whose ring is bound to the
original as "not this store's (c8c8..., expected c8c8...)" with the remedy
"restore the key", and posture read it ok, though every owner's open of
the copy refuses writes. Names this change adds are read through their
modules where they are used, so the file collects on a build without them
and each test fails there on its own.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client, claude_paths, doctor, posture, sandbox_settings
from bettermemory import config as _config
from bettermemory import store_keys
from bettermemory.log import KeyRing, KeyUnreadableError
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

from .conftest import set_git_discovery_ceiling

STATES = (
    "present",
    "unreadable",
    "missing",
    "foreign",
    "bound_elsewhere",
    "unclaimed",
)


@dataclass
class Case:
    """A store file whose key is in `state`, and where it came from."""

    state: str
    store_file: Path
    original: Path
    keys: Path
    store_id: str
    fingerprint: str

    @property
    def keyring(self) -> KeyRing:
        return KeyRing(self.keys, self.store_id)


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


@pytest.fixture
def quiet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Posture reads Claude Code's settings and the daemon's state: a fake
    home, the managed settings path and the state directory under the
    test's directory, as the posture tests set them."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(claude_paths.CLAUDE_CONFIG_DIR_ENV, raising=False)
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(
        sandbox_settings, "claude_managed_settings_path", lambda platform=None: managed
    )
    monkeypatch.setenv(_daemon_client.STATE_DIR_ENV, str(tmp_path / "state"))
    set_git_discovery_ceiling(tmp_path, monkeypatch)


def _case(state: str, tmp_path: Path, keys: Path) -> Case:
    original = (tmp_path / "original").resolve()
    with Store.create(original) as created:
        store_id = created.store_id
        fingerprint = created.key_fingerprint
    case = Case(
        state,
        original / STORE_FILENAME,
        original / STORE_FILENAME,
        keys.resolve(),
        store_id,
        fingerprint,
    )
    key = case.keyring.current_path()
    if state == "unreadable":
        key.unlink()
        try:
            key.symlink_to(keys / "on-a-volume-not-mounted.key")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"no symbolic links here: {exc}")
    elif state == "missing":
        key.unlink()
    elif state == "foreign":
        key.write_bytes(os.urandom(32))
    elif state == "bound_elsewhere":
        copy = (tmp_path / "repo" / ".claude-memory").resolve()
        copy.mkdir(parents=True)
        shutil.copy(case.original, copy / STORE_FILENAME)
        case.store_file = copy / STORE_FILENAME
    elif state == "unclaimed":
        # A copy taken before the original wrote again, and a ring left
        # unbound, as a release before rc13 left it.
        copy = (tmp_path / "repo" / ".claude-memory").resolve()
        copy.mkdir(parents=True)
        shutil.copy(case.original, copy / STORE_FILENAME)
        with Store.open(case.original, allow_rekey=True) as original_store:
            original_store.put_memory(_memory("the original writes on"))
        case.keyring.binding_path().unlink()
        case.store_file = copy / STORE_FILENAME
    return case


def _expected(case: Case) -> Any:
    """The state as the one function reads it for this file, quietly."""
    return store_keys.key_state(
        case.keyring, case.store_file, case.fingerprint, warn=False
    )


@pytest.mark.parametrize("state", STATES)
def test_the_function_reads_each_state(
    state: str, tmp_path: Path, keys_dir: Path
) -> None:
    case = _case(state, tmp_path, keys_dir)
    read = store_keys.key_state(
        case.keyring, case.store_file, case.fingerprint, warn=False
    )
    assert read.state == state
    assert (read.remedy is None) == (state == "present")
    if state == "bound_elsewhere":
        assert read.bound_to == case.original
        assert "bound elsewhere" in read.summary
        assert str(case.original) in read.summary
        assert read.remedy is not None
        assert "bettermemory key adopt --yes" in read.remedy
        assert "delete" not in read.remedy
    if state == "unreadable":
        assert read.error is not None


@pytest.mark.parametrize("state", STATES)
def test_store_open_keeps_the_state_and_refuses_writes_in_its_words(
    state: str, tmp_path: Path, keys_dir: Path
) -> None:
    """A reader's open of every state, and the owner's of the copy: the
    state it keeps is the function's, and a write it refuses says why in
    the same words."""
    case = _case(state, tmp_path, keys_dir)
    expected = _expected(case)
    with Store.open(case.store_file, allow_rekey=False) as store:
        kept = store.key_state
        assert kept.state == state
        assert kept.summary == expected.summary
        if state == "present":
            store.put_memory(_memory("the store's own key signs this"))
            return
        with pytest.raises(RuntimeError, match="open for reading only") as refused:
            store.put_memory(_memory("refused"))
        assert expected.summary in str(refused.value)
    if state in ("bound_elsewhere", "unclaimed"):
        with Store.open(case.store_file, allow_rekey=True) as owner:
            assert owner.key_state.state == state
            assert owner.key_state.summary == expected.summary
            assert not owner.has_key
    if state == "unreadable":
        with pytest.raises(KeyUnreadableError):
            Store.open(case.store_file, allow_rekey=True)


@pytest.mark.parametrize("state", STATES)
def test_doctor_renders_the_state(state: str, tmp_path: Path, keys_dir: Path) -> None:
    case = _case(state, tmp_path, keys_dir)
    expected = _expected(case)
    ctx = doctor._Context(cwd=tmp_path, directory=case.store_file.parent)
    store_check, key_check = doctor.check_store(ctx)
    assert store_check.status == "ok"
    assert key_check.status == ("ok" if state == "present" else "fail")
    assert key_check.summary == expected.summary
    assert key_check.remedy == expected.remedy
    assert key_check.details["key_state"] == state


@pytest.mark.parametrize("state", STATES)
def test_posture_renders_the_state(
    state: str,
    tmp_path: Path,
    keys_dir: Path,
    quiet_home: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(state, tmp_path, keys_dir)
    expected = _expected(case)
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(case.store_file.parent))
    [finding] = [f for f in posture.audit().findings if f.id == "key.match"]
    assert finding.status == ("ok" if state == "present" else "high")
    assert finding.summary.startswith(expected.summary)
    assert finding.fix == expected.remedy
    assert finding.details["key_state"] == state


def test_a_copy_bound_to_its_original_reads_bound_elsewhere_everywhere(
    tmp_path: Path,
    keys_dir: Path,
    quiet_home: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's case: store A created, copied to B, B opened. Doctor
    said "not this store's (c8c8..., expected c8c8...)" and "restore the
    key", and posture said ok. Each now says the ring is bound elsewhere,
    names A and `key adopt`, which rebinds the ring on the record (the
    binding file it rewrites named, none to delete), and posture is not
    ok."""
    case = _case("bound_elsewhere", tmp_path, keys_dir)
    binding = str(case.keyring.binding_path())
    ctx = doctor._Context(cwd=tmp_path, directory=case.store_file.parent)
    _, key_check = doctor.check_store(ctx)
    assert key_check.status == "fail"
    assert "bound elsewhere" in key_check.summary
    assert str(case.original) in key_check.summary
    assert "not this store's" not in key_check.summary
    assert key_check.remedy is not None
    assert binding in key_check.remedy and "restore" not in key_check.remedy
    assert "key adopt --yes" in key_check.remedy and "delete" not in key_check.remedy
    monkeypatch.setenv(_config.ENV_DIR_OVERRIDE, str(case.store_file.parent))
    [finding] = [f for f in posture.audit().findings if f.id == "key.match"]
    assert finding.status == "high"
    assert "bound elsewhere" in finding.summary
    assert str(case.original) in finding.summary
    assert finding.fix is not None and binding in finding.fix
