"""`bettermemory doctor`'s `orphan_keys` check and `--clear-orphan-keys`
(ENG-106).

One keys directory serves every store on the host, and a store deleted
leaves its key ring behind: keys, head, anchor and binding. Pinned here:
doctor names the rings whose store is shown gone (the bound file's
directory is there, can be read, is not empty and holds no file by its
name), `--clear-orphan-keys` prints the plan, and with `--yes` it moves
those rings, and nothing else, to `quarantine/<store_id>/` under the keys
directory (owner-only, beside a note naming the bound path and the
reason), from which `key import` takes a key back. Nothing is deleted.
Kept, whatever is asked, each named: a ring whose bound directory is not
there (a store moved or deleted with its directory, a link on the way to
nothing), is empty (a mount point), cannot be read or does not answer in
`location.PROBE_BUDGET`; one whose bound file is there but is damaged or
holds another store; one that names no store file; one a daemon still
serves; the ring of the store doctor checks; a ring an owner's open binds
again between the survey and the clear. A name the quarantine holds
already is never written over, and a clear cut short moves the rest on
the next run.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
from pathlib import Path

import pytest

from bettermemory import _daemon_client, doctor, location
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


def _remove_store_files(path: Path) -> None:
    for sidecar in path.parent.glob(f"{STORE_FILENAME}*"):
        sidecar.unlink()


def _gone(directory: Path) -> tuple[Path, str]:
    """A store whose file was deleted from a directory that holds other
    files still: shown gone."""
    path, store_id = _store(directory)
    (path.parent / "README.md").write_text("a project file\n", encoding="utf-8")
    _remove_store_files(path)
    return path, store_id


def _ring_files(keys: Path, store_id: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(keys.glob(f"{store_id}.*"))}


def _standing(
    keys: Path, store_id: str, current: Path | None = None
) -> lifecycle.RingStanding:
    survey = lifecycle.survey_rings(current)
    [ring] = [r for r in survey.rings if r.store_id == store_id]
    return ring


def _kept(keys: Path, store_id: str, fragment: str) -> None:
    """The ring reads kept with ``fragment`` in its reason, and a clear
    with --yes moves nothing of it."""
    before = _ring_files(keys, store_id)
    standing = _standing(keys, store_id)
    assert standing.state == lifecycle.RING_KEPT, standing
    assert fragment in standing.reason, standing.reason
    result = lifecycle.clear_orphan_rings(None, apply=True)
    assert result["moved"] == [] and result["failed"] == [], result
    assert _ring_files(keys, store_id) == before
    assert not lifecycle.quarantine_path(keys, store_id).exists()


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


def test_doctor_plans_then_moves_a_ring_whose_store_is_gone(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current, current_id = _store(tmp_path / "current")
    gone, gone_id = _gone(tmp_path / "gone")
    kept = _ring_files(keys_dir, current_id)
    ring = _ring_files(keys_dir, gone_id)
    assert ring

    _, out = _cli(["doctor"], current.parent, tmp_path, monkeypatch, capsys)
    assert "warn  orphan_keys" in out
    assert "bettermemory doctor --clear-orphan-keys" in out
    assert "nothing is deleted" in out

    code, out = _cli(
        ["doctor", "--clear-orphan-keys"], current.parent, tmp_path, monkeypatch, capsys
    )
    assert code == 2
    assert "would move 1 key ring(s)" in out
    assert gone_id in out
    assert "--clear-orphan-keys --yes" in out
    assert _ring_files(keys_dir, gone_id) == ring, "the plan moved a ring"

    _, out = _cli(
        ["doctor", "--clear-orphan-keys", "--json"],
        current.parent,
        tmp_path,
        monkeypatch,
        capsys,
    )
    details = _orphan_check(out)["details"]
    assert isinstance(details, dict)
    plan = details["clear"]
    assert plan["status"] == "planned"
    assert [r["store_id"] for r in plan["planned"]] == [gone_id]
    assert plan["planned"][0]["reason"].endswith(f"holds no {STORE_FILENAME}")

    code, out = _cli(
        ["doctor", "--clear-orphan-keys", "--yes", "--json"],
        current.parent,
        tmp_path,
        monkeypatch,
        capsys,
    )
    check = _orphan_check(out)
    assert check["status"] == "ok", check
    details = check["details"]
    assert isinstance(details, dict)
    [moved] = details["clear"]["moved"]
    target = lifecycle.quarantine_path(keys_dir, gone_id)
    assert (moved["store_id"], moved["to"]) == (gone_id, str(target))
    assert _ring_files(keys_dir, gone_id) == {}
    assert {p.name: p.read_bytes() for p in target.glob(f"{gone_id}.*")} == {
        **ring,
        **{
            name: b""
            for name in moved["files"]
            if name.endswith(".lock") and name not in ring
        },
    }
    [line] = (target / lifecycle.QUARANTINE_NOTE).read_text("utf-8").splitlines()
    assert str(gone) in line and "holds no" in line
    assert "bettermemory key import FILE" in line
    assert _ring_files(keys_dir, current_id) == kept
    with Store.open(current, read_only=True) as reopened:
        assert reopened.log_verify()["status"] == "ok"


@POSIX_ONLY
def test_the_quarantine_is_owner_only(tmp_path: Path, keys_dir: Path) -> None:
    _, store_id = _gone(tmp_path / "gone")
    lifecycle.clear_orphan_rings(None, apply=True)
    for directory in (
        keys_dir / lifecycle.QUARANTINE_DIR,
        lifecycle.quarantine_path(keys_dir, store_id),
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_yes_without_the_clear_is_a_usage_error(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current, _ = _store(tmp_path / "current")
    _, gone_id = _gone(tmp_path / "gone")
    ring = _ring_files(keys_dir, gone_id)
    code, _ = _cli(["doctor", "--yes"], current.parent, tmp_path, monkeypatch, capsys)
    assert code == 2
    assert _ring_files(keys_dir, gone_id) == ring


def test_key_import_takes_a_quarantined_key_back(
    tmp_path: Path, keys_dir: Path
) -> None:
    """A store file moved out of a directory that keeps other files reads
    gone, and the clear moves its ring; `key import` of the quarantined
    key at the store's new place files it back, and the store verifies."""
    old, store_id = _store(tmp_path / "old")
    (old.parent / "README.md").write_text("a project file\n", encoding="utf-8")
    new = (tmp_path / "new").resolve() / STORE_FILENAME
    new.parent.mkdir()
    shutil.move(str(old), str(new))
    _remove_store_files(old)
    result = lifecycle.clear_orphan_rings(None, apply=True)
    assert [m["store_id"] for m in result["moved"]] == [store_id]
    target = lifecycle.quarantine_path(keys_dir, store_id)
    assert not (keys_dir / f"{store_id}.key").exists()

    report = lifecycle.import_keys(new, target / f"{store_id}.key")
    assert report["status"] == "done", report
    assert (keys_dir / f"{store_id}.key").read_bytes() == (
        target / f"{store_id}.key"
    ).read_bytes()
    with Store.open_or_create(new) as store:
        assert store.has_key
        assert store.log_verify()["status"] == "ok"


@pytest.mark.parametrize("how", ["deleted_with_it", "link_to_nothing"])
def test_a_ring_whose_directory_is_not_there_is_kept(
    how: str, tmp_path: Path, keys_dir: Path
) -> None:
    """The store moved or deleted with its directory, or a directory on
    the way that became a link to nothing (a volume's link left behind):
    nothing shows the store gone rather than elsewhere."""
    path, store_id = _store(tmp_path / "vol" / "project")
    if how == "deleted_with_it":
        shutil.rmtree(path.parent)
    else:
        if sys.platform == "win32":
            pytest.skip("symbolic links need a privilege on Windows")
        shutil.move(str(tmp_path / "vol"), str(tmp_path / "elsewhere"))
        (tmp_path / "vol").symlink_to(tmp_path / "missing")
    _kept(keys_dir, store_id, "is not there")


def test_a_ring_whose_directory_is_empty_is_kept(
    tmp_path: Path, keys_dir: Path
) -> None:
    """An empty directory where the store was: the mount point of a volume
    that is not mounted, or a folder emptied by hand."""
    path, store_id = _store(tmp_path / "mnt")
    _remove_store_files(path)
    assert not any(path.parent.iterdir())
    _kept(keys_dir, store_id, "is empty")


@POSIX_ONLY
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root reads any directory"
)
def test_a_store_this_user_cannot_read_is_kept(tmp_path: Path, keys_dir: Path) -> None:
    locked = tmp_path / "locked"
    _, store_id = _store(locked / "project")
    locked.chmod(0)
    try:
        _kept(keys_dir, store_id, "cannot be read")
    finally:
        locked.chmod(0o700)


def test_a_damaged_store_file_is_kept(tmp_path: Path, keys_dir: Path) -> None:
    path, store_id = _store(tmp_path / "damaged")
    _remove_store_files(path)
    path.write_bytes(b"not a database at all, " * 64)
    _kept(keys_dir, store_id, "does not read as a store")


def test_a_ring_whose_file_holds_another_store_is_kept(
    tmp_path: Path, keys_dir: Path
) -> None:
    _, replaced_id = _gone(tmp_path / "here")
    _, other_id = _store(tmp_path / "here")
    assert replaced_id != other_id
    _kept(keys_dir, replaced_id, "holds another store")
    assert _ring_files(keys_dir, other_id)


def test_a_path_that_does_not_answer_in_the_budget_is_kept(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-mounted share that hangs: the read of the bound path is left
    running and the ring kept, within the budget (ENG-156), so doctor does
    not hang with it."""
    _, store_id = _gone(tmp_path / "hung")
    release = threading.Event()

    def hangs(bound: Path, ring_id: str) -> tuple[str, str]:
        release.wait(30)
        return lifecycle.RING_ORPHANED, "late"

    monkeypatch.setattr(lifecycle, "_shown_gone", hangs)
    monkeypatch.setattr(location, "PROBE_BUDGET", 0.05)
    try:
        _kept(keys_dir, store_id, "did not answer in 0.05 s")
    finally:
        release.set()


def test_a_ring_that_names_no_store_file_is_kept(
    tmp_path: Path, keys_dir: Path
) -> None:
    _, store_id = _gone(tmp_path / "gone")
    KeyRing(keys_dir, store_id).binding_path().unlink()
    before = _ring_files(keys_dir, store_id)
    survey = lifecycle.survey_rings(None)
    assert survey.to_dict()["unbound"] == [store_id]
    assert "names no store file" in survey.summary()
    assert lifecycle.clear_orphan_rings(None, apply=True)["moved"] == []
    assert _ring_files(keys_dir, store_id) == before


def test_the_checked_stores_ring_is_in_use_whatever_its_binding_says(
    tmp_path: Path, keys_dir: Path
) -> None:
    current, store_id = _store(tmp_path / "current")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "README.md").write_text("a project file\n", encoding="utf-8")
    KeyRing(keys_dir, store_id).rebind_store(elsewhere / STORE_FILENAME)
    assert _standing(keys_dir, store_id).state == lifecycle.RING_ORPHANED
    assert _standing(keys_dir, store_id, current).state == lifecycle.RING_IN_USE
    check = doctor.check_orphan_keys(
        doctor._Context(cwd=None, directory=current.parent), clear=True, apply=True
    )
    assert check.status == "ok"
    assert check.details["clear"]["moved"] == []
    assert _ring_files(keys_dir, store_id)


def test_a_ring_a_daemon_still_serves_is_kept(tmp_path: Path, keys_dir: Path) -> None:
    path, store_id = _gone(tmp_path / "gone")
    state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
    state_dir.mkdir(parents=True, exist_ok=True)
    held = _daemon_client.try_lock(_daemon_client.lock_file_for(state_dir, path))
    assert held is not None
    try:
        assert _standing(keys_dir, store_id).state == lifecycle.RING_IN_USE
        assert lifecycle.clear_orphan_rings(None, apply=True)["moved"] == []
    finally:
        _daemon_client.release_lock(held)
    assert _standing(keys_dir, store_id).state == lifecycle.RING_ORPHANED


def _moved(tmp_path: Path) -> tuple[Path, str]:
    """A store file moved to a new directory by hand, its old directory
    keeping other files, not opened since: its ring is still bound to the
    old path, which reads gone."""
    old, store_id = _store(tmp_path / "old")
    (old.parent / "README.md").write_text("a project file\n", encoding="utf-8")
    new = (tmp_path / "new").resolve() / STORE_FILENAME
    new.parent.mkdir()
    shutil.copyfile(old, new)
    _remove_store_files(old)
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
    result = lifecycle.clear_orphan_rings(None, apply=True)
    assert result["moved"] == []
    [kept] = result["kept"]
    assert (kept["store_id"], kept["state"]) == (store_id, lifecycle.RING_IN_USE)
    assert (keys_dir / f"{store_id}.key").is_file()


def test_key_adopt_keeps_the_ring_of_a_store_moved_by_hand(
    tmp_path: Path, keys_dir: Path
) -> None:
    """What the remedy says: adopting the ring from the moved store binds
    it there, and it no longer reads orphaned."""
    moved, store_id = _moved(tmp_path)
    assert _standing(keys_dir, store_id).state == lifecycle.RING_ORPHANED
    report = lifecycle.adopt_ring(moved, apply=True)
    assert report["status"] == "done", report
    assert _standing(keys_dir, store_id).state == lifecycle.RING_IN_USE
    assert lifecycle.clear_orphan_rings(None, apply=True)["moved"] == []


def test_a_name_the_quarantine_holds_is_never_written_over(
    tmp_path: Path, keys_dir: Path
) -> None:
    _, store_id = _gone(tmp_path / "gone")
    ring = _ring_files(keys_dir, store_id)
    target = lifecycle.quarantine_path(keys_dir, store_id)
    target.mkdir(parents=True)
    (target / f"{store_id}.key").write_bytes(b"an earlier quarantine's key")
    result = lifecycle.clear_orphan_rings(None, apply=True)
    assert result["moved"] == []
    [failed] = result["failed"]
    assert failed["store_id"] == store_id
    assert "FileExistsError" in failed["error"]
    assert _ring_files(keys_dir, store_id) == ring
    assert (target / f"{store_id}.key").read_bytes() == b"an earlier quarantine's key"


def test_a_clear_cut_short_moves_the_rest_on_the_next_run(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding moves last: a clear that fails before it leaves a ring
    that still reads orphaned, and the next run moves what is left into the
    same quarantine."""
    _, store_id = _gone(tmp_path / "gone")
    ring = _ring_files(keys_dir, store_id)
    binding = KeyRing(keys_dir, store_id).binding_path()
    real_rename = os.rename

    def no_binding(src: object, dst: object) -> None:
        if Path(str(src)) == binding:
            raise OSError("the disk went away")
        real_rename(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(lifecycle.os, "rename", no_binding)
    first = lifecycle.clear_orphan_rings(None, apply=True)
    assert [f["store_id"] for f in first["failed"]] == [store_id]
    assert _standing(keys_dir, store_id).state == lifecycle.RING_ORPHANED
    monkeypatch.setattr(lifecycle.os, "rename", real_rename)
    second = lifecycle.clear_orphan_rings(None, apply=True)
    assert [m["store_id"] for m in second["moved"]] == [store_id]
    target = lifecycle.quarantine_path(keys_dir, store_id)
    held = {p.name: p.read_bytes() for p in target.glob(f"{store_id}.*")}
    assert {name: held[name] for name in ring} == ring
    assert _ring_files(keys_dir, store_id) == {}


def test_no_keys_directory_is_ok(tmp_path: Path, keys_dir: Path) -> None:
    assert not keys_dir.exists()
    check = doctor.check_orphan_keys(doctor._Context(cwd=None, directory=None))
    assert check.status == "ok"
    assert check.summary.startswith("no key rings under")
    assert not keys_dir.exists()
