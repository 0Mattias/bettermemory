"""`backup create` refuses an `--out` that is a bundle, and names a key
file it cannot read (ENG-93 items 2 and 3).

`--out` naming a directory that is already a backup bundle wrote the new
bundle inside it, so the old one held files its manifest does not list;
the store side of the same mistake (`BETTERMEMORY_DIR` naming a bundle)
was already refused. And a key file that is there and cannot be read was
read as no key: the bundle came out unsigned with a warning that no key
matched the store's fingerprint, which sends the owner looking for a key
that is in its place. Both now say what is wrong, and an unreadable key
refuses the backup: an unsigned bundle never verifies, so no restore
takes it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.backup import (
    BackupError,
    create_backup,
    verify_bundle,
)
from bettermemory.log import KeyRing, fingerprint
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


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def _listing(directory: Path) -> list[str]:
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path) as created:
        created.put_memory(_memory("a memory to back up"))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(path.parent))
    return path


@pytest.fixture
def bundle(store_file: Path, tmp_path: Path) -> Path:
    return Path(create_backup(store_file, out_dir=tmp_path / "bundles")["bundle"])


@pytest.mark.parametrize("where", ["bundle", "inside", "deeper", "link"])
def test_create_refuses_an_out_that_is_a_bundle_or_in_one(
    where: str,
    store_file: Path,
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if where == "link":
        out = tmp_path / "to-the-bundle"
        try:
            out.symlink_to(bundle, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"no symbolic links here: {exc}")
    else:
        out = {
            "bundle": bundle,
            "inside": bundle / "more",
            "deeper": bundle / "more" / "and" / "more",
        }[where]
    before = _listing(bundle)

    with pytest.raises(BackupError, match="is a backup bundle or is in one"):
        create_backup(store_file, out_dir=out)
    assert _run_cli(["backup", "create", "--out", str(out)], monkeypatch) == 1
    err = capsys.readouterr().err
    assert f"{out} is a backup bundle or is in one" in err
    assert "manifest.json" in err
    assert "name a directory outside every bundle with --out" in err

    assert _listing(bundle) == before
    assert verify_bundle(bundle)["status"] == "ok"


def test_create_writes_beside_bundles_under_their_parent(
    store_file: Path, bundle: Path
) -> None:
    """The directory that holds bundles is not one: a second bundle goes
    beside the first, as before."""
    second = Path(create_backup(store_file, out_dir=bundle.parent)["bundle"])
    assert second.parent == bundle.parent and second != bundle


def _key_in_the_way(store_file: Path, keys_dir: Path) -> tuple[Path, str]:
    """The store's current key file made unreadable: a directory where the
    file should be, which no read of it gets past on any platform, as root
    too. Returns the path and the key's fingerprint."""
    with Store.open(store_file, read_only=True) as opened:
        store_id = opened.store_id
    path = KeyRing(keys_dir, store_id).current_path()
    key_fp = fingerprint(path.read_bytes())
    path.unlink()
    path.mkdir()
    return path, key_fp


def test_an_unreadable_key_refuses_the_backup_by_its_file(
    store_file: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bundle would be unsigned, which `backup verify` reads as
    unverifiable and `backup restore` refuses even with
    --trust-bundle-key: exit 1, the file named, and no bundle left."""
    path, _ = _key_in_the_way(store_file, keys_dir)
    out = tmp_path / "bundles"
    argv = ["backup", "create", "--out", str(out), "--json"]
    assert _run_cli(argv, monkeypatch) == 1
    err = capsys.readouterr().err
    assert f"the key file {path} is there and cannot be read (" in err
    assert "no restore takes it; make it readable and run the backup again" in err
    assert "no key under" not in err
    assert not out.exists() or list(out.iterdir()) == []


def test_an_unreadable_key_refuses_include_key_by_its_file(
    store_file: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = _key_in_the_way(store_file, keys_dir)
    out = tmp_path / "bundles"
    argv = ["backup", "create", "--out", str(out), "--include-key"]
    assert _run_cli(argv, monkeypatch) == 1
    err = capsys.readouterr().err
    assert f"the key file {path} is there and cannot be read (" in err
    assert "make it readable and run the backup again" in err
    assert "no key under" not in err
    assert not out.exists() or list(out.iterdir()) == []


def test_an_unreadable_retired_key_is_named(tmp_path: Path) -> None:
    """A retired key's file, and the current one's, each named when it is
    there and does not read; an absent file is no such finding."""
    from bettermemory.backup import _unreadable_key_files

    ring = KeyRing(tmp_path, "a" * 32)
    ring.ensure_dir()
    current, retired, absent = "c" * 64, "r" * 64, "0" * 64
    ring.current_path().mkdir()
    ring.retired_path(retired).mkdir()
    found = _unreadable_key_files(ring, current, [current, retired, absent])
    assert set(found) == {current, retired}
    assert str(ring.current_path()) in found[current]
    assert str(ring.retired_path(retired)) in found[retired]
    assert all("is there and cannot be read (" in text for text in found.values())


def test_a_fingerprint_no_file_name_can_hold_is_no_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fingerprint the store's meta spells with characters a file name
    cannot hold (Windows refuses control characters: EINVAL), or longer
    than a name may be (ENAMETOOLONG), names no file that could be there
    to read; it is no finding, where it read as a key file that cannot
    be read (the Windows leg on c0110fca, `backup create` exit 1 for a
    fingerprint of `\\x1b]52;c;aGk=\\x07`)."""
    import errno

    from bettermemory.backup import _unreadable_key_files

    ring = KeyRing(tmp_path, "a" * 32)
    ring.ensure_dir()
    current, refused, retired = "c" * 64, "\x1b]52;c;aGk=\x07", "r" * 64
    ring.retired_path(retired).mkdir()
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if refused in self.name:
            raise OSError(errno.EINVAL, "Invalid argument", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    found = _unreadable_key_files(ring, current, [refused, retired, "x" * 300])
    assert set(found) == {retired}


@pytest.mark.parametrize("poison", ["view", "generated", "check"])
def test_create_admits_the_store_before_it_copies_it(
    store_file: Path, tmp_path: Path, poison: str
) -> None:
    """`backup create` read the copy's meta with nothing admitting the
    store first: one whose meta is an endless view hung it (killed after
    25 s, a partial bundle left behind), where a writing open refuses the
    file in under a second. It now admits the store, under the
    admission's deadline, before the online copy, and refuses with the
    admission's line, leaving no bundle (cold review M5)."""
    from .test_store_admission import _POISONS, _in_time, _sql

    _sql(store_file, *_POISONS[poison])
    out = tmp_path / "bundles"
    with pytest.raises(BackupError) as refused:
        _in_time(lambda: create_backup(store_file, out_dir=out))
    assert str(refused.value).startswith(f"{store_file} is not opened: ")
    assert not out.exists() or list(out.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_key_file_without_read_permission_is_named(
    store_file: Path, keys_dir: Path, tmp_path: Path
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    with Store.open(store_file, read_only=True) as opened:
        store_id = opened.store_id
    path = KeyRing(keys_dir, store_id).current_path()
    path.chmod(0)
    out = tmp_path / "bundles"
    try:
        with pytest.raises(BackupError) as refused:
            create_backup(store_file, out_dir=out)
    finally:
        path.chmod(0o600)
    assert str(refused.value).startswith(
        f"the key file {path} is there and cannot be read ("
    )
    assert not out.exists() or list(out.iterdir()) == []
