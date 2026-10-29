"""`bettermemory doctor`'s `orphan_keys` check and `--clear-orphan-keys`
(ENG-106).

One keys directory serves every store on the host, and a store deleted
with its directory leaves its key ring behind: keys, head, anchor and
binding. Pinned here: doctor names the rings whose bound store file is
gone, or holds another store or none, and `--clear-orphan-keys` deletes
them and nothing else. Kept, whatever is asked: a ring whose store file
cannot be read to tell (a directory this user cannot search), one whose
nearest existing directory is a filesystem root or a mount directory (a
volume or share that is not mounted), one that names no store file, one
a daemon still serves, the ring of the store doctor checks, and a ring an
owner's open binds again between the survey and the clear. A store moved
by hand reads as gone until `key adopt --yes` binds its ring to it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from bettermemory import _daemon_client, doctor
from bettermemory import key_lifecycle as lifecycle
from bettermemory.cli import main as cli_main
from bettermemory.log import KeyRing
from bettermemory.store import STORE_FILENAME, Store

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _store(directory: Path) -> tuple[Path, str]:
    path = directory.resolve() / STORE_FILENAME
    with Store.create(path) as store:
        store.write(content="the cache holds 3 entries", scopes=["infra"])
        return path, store.store_id


def _gone(directory: Path) -> tuple[Path, str]:
    """A store created and then deleted with its directory."""
    path, store_id = _store(directory)
    shutil.rmtree(directory)
    return path, store_id


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(keys.glob(f"{store_id}.*"))}


def _standing(keys: Path, store_id: str, current: Path | None = None) -> str:
    survey = lifecycle.survey_rings(current)
    [ring] = [r for r in survey.rings if r.store_id == store_id]
    return ring.state


def _cli(
    argv: list[str],
    store_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    code = exc.value.code
    return (code if isinstance(code, int) else 1), capsys.readouterr().out


def _orphan_check(out: str) -> dict[str, object]:
    [check] = [c for c in json.loads(out)["checks"] if c["id"] == "orphan_keys"]
    assert isinstance(check, dict)
    return check


def test_doctor_names_a_ring_whose_store_is_gone_and_clears_it(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current, current_id = _store(tmp_path / "current")
    _, gone_id = _gone(tmp_path / "gone")
    kept = _ring_files(keys_dir, current_id)
    assert _ring_files(keys_dir, gone_id)

    _, out = _cli(["doctor"], current.parent, tmp_path, monkeypatch, capsys)
    assert "warn  orphan_keys" in out
    assert "bettermemory doctor --clear-orphan-keys" in out

    _, out = _cli(["doctor", "--json"], current.parent, tmp_path, monkeypatch, capsys)
    check = _orphan_check(out)
    assert check["status"] == "warn"
    details = check["details"]
    assert isinstance(details, dict)
    assert [r["store_id"] for r in details["orphaned"]] == [gone_id]
    assert details["orphaned"][0]["reason"].endswith("is gone")
    assert _ring_files(keys_dir, gone_id), "doctor without the flag deleted a ring"

    _, out = _cli(
        ["doctor", "--clear-orphan-keys", "--json"],
        current.parent,
        tmp_path,
        monkeypatch,
        capsys,
    )
    check = _orphan_check(out)
    assert check["status"] == "ok", check
    details = check["details"]
    assert isinstance(details, dict)
    [cleared] = details["cleared"]["cleared"]
    assert cleared["store_id"] == gone_id
    assert f"{gone_id}.key" in cleared["files"]
    assert f"{gone_id}.path" in cleared["files"]
    assert _ring_files(keys_dir, gone_id) == {}
    assert _ring_files(keys_dir, current_id) == kept
    with Store.open(current, read_only=True) as reopened:
        assert reopened.log_verify()["status"] == "ok"


def test_a_ring_whose_file_holds_another_store_is_orphaned(
    tmp_path: Path, keys_dir: Path
) -> None:
    _, replaced_id = _gone(tmp_path / "here")
    _, other_id = _store(tmp_path / "here")
    assert replaced_id != other_id
    survey = lifecycle.survey_rings(None)
    [orphan] = survey.of(lifecycle.RING_ORPHANED)
    assert orphan.store_id == replaced_id
    assert "holds another store" in orphan.reason
    result = lifecycle.clear_orphan_rings(None)
    assert [c["store_id"] for c in result["cleared"]] == [replaced_id]
    assert _ring_files(keys_dir, other_id)


@POSIX_ONLY
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root reads any directory"
)
def test_a_store_this_user_cannot_read_is_kept(tmp_path: Path, keys_dir: Path) -> None:
    locked = tmp_path / "locked"
    _, store_id = _store(locked / "project")
    before = _ring_files(keys_dir, store_id)
    locked.chmod(0)
    try:
        assert _standing(keys_dir, store_id) == lifecycle.RING_UNREACHABLE
        result = lifecycle.clear_orphan_rings(None)
    finally:
        locked.chmod(0o700)
    assert result["cleared"] == []
    assert _ring_files(keys_dir, store_id) == before


@pytest.mark.parametrize("where", ["root", "mount", "user_mount"])
def test_a_store_on_a_volume_not_mounted_is_kept(
    where: str, tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound file is not there and the nearest directory on its way is
    a filesystem root, a mount directory or a user's one under /media."""
    _, store_id = _store(tmp_path / "live")
    name = f"bm-not-mounted-{uuid.uuid4().hex}"
    if where == "root":
        away = Path(tmp_path.anchor) / name / "project" / STORE_FILENAME
    elif where == "mount":
        mounts = tmp_path / "Volumes"
        mounts.mkdir()
        monkeypatch.setattr(lifecycle, "_MOUNT_DIRS", frozenset({mounts.as_posix()}))
        away = mounts / name / "project" / STORE_FILENAME
    else:
        media = tmp_path / "media"
        (media / "someone").mkdir(parents=True)
        monkeypatch.setattr(
            lifecycle, "_USER_MOUNT_DIRS", frozenset({media.resolve().as_posix()})
        )
        away = media / "someone" / name / STORE_FILENAME
    ring = KeyRing(keys_dir, store_id)
    ring.rebind_store(away)
    before = _ring_files(keys_dir, store_id)
    survey = lifecycle.survey_rings(None)
    [kept] = survey.of(lifecycle.RING_UNREACHABLE)
    assert kept.store_id == store_id
    assert "not mounted" in kept.reason
    assert "cannot be reached now" in survey.summary()
    assert survey.remedy() is None
    assert lifecycle.clear_orphan_rings(None)["cleared"] == []
    assert _ring_files(keys_dir, store_id) == before


def test_a_ring_that_names_no_store_file_is_kept(
    tmp_path: Path, keys_dir: Path
) -> None:
    _, store_id = _gone(tmp_path / "gone")
    KeyRing(keys_dir, store_id).binding_path().unlink()
    before = _ring_files(keys_dir, store_id)
    survey = lifecycle.survey_rings(None)
    assert survey.to_dict()["unbound"] == [store_id]
    assert "names no store file" in survey.summary()
    assert lifecycle.clear_orphan_rings(None)["cleared"] == []
    assert _ring_files(keys_dir, store_id) == before


def test_the_checked_stores_ring_is_in_use_whatever_its_binding_says(
    tmp_path: Path, keys_dir: Path
) -> None:
    current, store_id = _store(tmp_path / "current")
    KeyRing(keys_dir, store_id).rebind_store(tmp_path / "gone" / STORE_FILENAME)
    assert _standing(keys_dir, store_id) == lifecycle.RING_ORPHANED
    assert _standing(keys_dir, store_id, current) == lifecycle.RING_IN_USE
    check = doctor.check_orphan_keys(
        doctor._Context(cwd=None, directory=current.parent), clear=True
    )
    assert check.status == "ok"
    assert check.details["cleared"]["cleared"] == []
    assert _ring_files(keys_dir, store_id)


def test_a_ring_a_daemon_still_serves_is_kept(tmp_path: Path, keys_dir: Path) -> None:
    path, store_id = _gone(tmp_path / "gone")
    state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
    state_dir.mkdir(parents=True, exist_ok=True)
    held = _daemon_client.try_lock(_daemon_client.lock_file_for(state_dir, path))
    assert held is not None
    try:
        assert _standing(keys_dir, store_id) == lifecycle.RING_IN_USE
        assert lifecycle.clear_orphan_rings(None)["cleared"] == []
    finally:
        _daemon_client.release_lock(held)
    assert _standing(keys_dir, store_id) == lifecycle.RING_ORPHANED


def _moved(tmp_path: Path) -> tuple[Path, str]:
    """A store copied to a new directory by hand and deleted where it was,
    not opened since: its ring is still bound to the old path."""
    old, store_id = _store(tmp_path / "old")
    new = (tmp_path / "new").resolve() / STORE_FILENAME
    new.parent.mkdir()
    shutil.copyfile(old, new)
    shutil.rmtree(old.parent)
    return new, store_id


def test_a_ring_bound_again_before_the_clear_is_kept(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The survey reads the ring orphaned; an owner's open of the moved
    store binds it before the clear takes the binding's lock, and the
    clear, reading it again under the lock, keeps it."""
    moved, store_id = _moved(tmp_path)
    original = lifecycle.survey_rings

    def then_opened(*args: object, **kwargs: object) -> lifecycle.RingSurvey:
        survey = original(*args, **kwargs)  # type: ignore[arg-type]
        with Store.open_or_create(moved) as store:
            assert store.has_key
        return survey

    monkeypatch.setattr(lifecycle, "survey_rings", then_opened)
    result = lifecycle.clear_orphan_rings(None)
    assert result["cleared"] == []
    [kept] = result["kept"]
    assert (kept["store_id"], kept["state"]) == (store_id, lifecycle.RING_IN_USE)
    assert (keys_dir / f"{store_id}.key").is_file()


def test_key_adopt_keeps_the_ring_of_a_store_moved_by_hand(
    tmp_path: Path, keys_dir: Path
) -> None:
    """What the remedy says: adopting the ring from the moved store binds
    it there, and it no longer reads orphaned."""
    moved, store_id = _moved(tmp_path)
    assert _standing(keys_dir, store_id) == lifecycle.RING_ORPHANED
    report = lifecycle.adopt_ring(moved, apply=True)
    assert report["status"] == "done", report
    assert _standing(keys_dir, store_id) == lifecycle.RING_IN_USE
    assert lifecycle.clear_orphan_rings(None)["cleared"] == []


def test_no_keys_directory_is_ok(tmp_path: Path, keys_dir: Path) -> None:
    assert not keys_dir.exists()
    check = doctor.check_orphan_keys(doctor._Context(cwd=None, directory=None))
    assert check.status == "ok"
    assert check.summary.startswith("no key rings under")
    assert not keys_dir.exists()
