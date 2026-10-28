"""`bettermemory security audit` grades the key ring's binding file.

`<keys_dir>/<store_id>.path` names the one store file the key ring belongs
to (`log.KeyRing.bind_store`), written ``0o600`` as the head checkpoint and
the origin anchor beside it are. It holds no secret, but another account
that can write it can name a file of its own as the ring's store: the
owner's open of the real store then reads as a second file with a bound
id and opens for reading only. The audit read the key, the retired keys,
the head and the anchor, and not the binding. What is pinned here:

* the binding is graded by the rule the head and the anchor get: read by
  group or others is a warning, write is high, the shipped ``0o600`` ok;
* a finding names the file and its mode, and its fix is the chmod;
* a ring with no binding (one from before bindings, read but never
  opened by a writer) has no binding finding, as a missing head has none;
* the audit leaves the mode as it found it.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from bettermemory import posture
from bettermemory.log import KeyRing
from bettermemory.posture import Finding
from bettermemory.store import STORE_FILENAME, Store

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX modes and owners"
)


@pytest.fixture
def ring(tmp_path: Path) -> tuple[Path, KeyRing]:
    """A store `Store.create` made, its key ring bound to it."""
    path = tmp_path / "store" / STORE_FILENAME
    keys = tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as created:
        store_id = created.store_id
    keyring = KeyRing(keys, store_id)
    assert keyring.binding_path().is_file()
    return path, keyring


def _modes(path: Path, keyring: KeyRing, tmp_path: Path) -> dict[str, Finding]:
    """The audit's mode findings for the store, by id (the last of an id
    that repeats, which only a retired key does)."""
    found = posture._modes(
        platform=sys.platform,
        store_dir=path.parent,
        store_file=path,
        keys_dir=keyring.keys_dir,
        keyring=keyring,
        state_dir=tmp_path / "state",
    )
    return {finding.id: finding for finding in found}


@posix_only
def test_the_shipped_binding_mode_is_ok(
    ring: tuple[Path, KeyRing], tmp_path: Path
) -> None:
    path, keyring = ring
    binding = _modes(path, keyring, tmp_path)["mode.binding"]
    assert binding.status == "ok"
    assert binding.path == str(keyring.binding_path())
    assert binding.details["mode"] == "0600"


@posix_only
def test_a_binding_readable_by_others_is_a_warning_naming_the_file_and_mode(
    ring: tuple[Path, KeyRing], tmp_path: Path
) -> None:
    path, keyring = ring
    binding_path = keyring.binding_path()
    os.chmod(binding_path, 0o644)
    binding = _modes(path, keyring, tmp_path)["mode.binding"]
    assert binding.status == "warn"
    assert str(binding_path) in binding.summary
    assert "0644" in binding.summary
    assert "key ring binding" in binding.summary
    assert binding.path == str(binding_path)
    assert binding.details["mode"] == "0644"
    assert binding.details["expected"] == "0600"
    assert binding.fix is not None and binding.fix.startswith("chmod 600 ")
    assert binding.fix.endswith(str(binding_path))
    # Reported, not healed.
    assert stat.S_IMODE(binding_path.stat().st_mode) == 0o644


@posix_only
@pytest.mark.parametrize(
    ("mode", "status"),
    [(0o600, "ok"), (0o640, "warn"), (0o644, "warn"), (0o664, "high"), (0o666, "high")],
)
def test_the_binding_is_graded_as_the_head_and_the_anchor_are(
    ring: tuple[Path, KeyRing], tmp_path: Path, mode: int, status: str
) -> None:
    """The three non-key files of the ring under one rule: none holds a
    secret, so read is a warning; any of them written by another account
    moves what the store trusts, so write is high."""
    path, keyring = ring
    keyring.write_origin(seq=1, mac="1" * 64)
    files = {
        "mode.binding": keyring.binding_path(),
        "mode.head": keyring.head_path(),
        "mode.origin": keyring.origin_path(),
    }
    for file in files.values():
        os.chmod(file, mode)
    found = _modes(path, keyring, tmp_path)
    assert {finding_id: found[finding_id].status for finding_id in files} == {
        finding_id: status for finding_id in files
    }


@posix_only
def test_a_ring_without_a_binding_has_no_binding_finding(
    ring: tuple[Path, KeyRing], tmp_path: Path
) -> None:
    path, keyring = ring
    keyring.binding_path().unlink()
    found = _modes(path, keyring, tmp_path)
    assert "mode.binding" not in found
    assert found["mode.key"].status == "ok"


@posix_only
def test_an_open_binding_sets_the_audits_exit_code(
    ring: tuple[Path, KeyRing], tmp_path: Path
) -> None:
    """Through `Audit`: a warning alone exits 2, a writable binding 1."""
    path, keyring = ring

    def exit_code() -> int:
        findings = list(_modes(path, keyring, tmp_path).values())
        report = posture.Audit(
            store=path, store_rule="test", platform=sys.platform, findings=findings
        )
        return report.exit_code

    assert exit_code() == 0
    os.chmod(keyring.binding_path(), 0o644)
    assert exit_code() == 2
    os.chmod(keyring.binding_path(), 0o646)
    assert exit_code() == 1


def test_windows_reports_no_binding_mode(
    ring: tuple[Path, KeyRing], tmp_path: Path
) -> None:
    """Modes are not read on Windows: one skip, and no binding finding."""
    path, keyring = ring
    found = list(
        posture._modes(
            platform="win32",
            store_dir=path.parent,
            store_file=path,
            keys_dir=keyring.keys_dir,
            keyring=keyring,
            state_dir=tmp_path / "state",
        )
    )
    assert [(f.id, f.status) for f in found] == [("mode", "skip")]
