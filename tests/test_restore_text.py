"""The restore's text output shows the origin anchor and the key ring's
binding, before and after (ENG-103).

`backup restore` printed the moves, the keys and the head checkpoint, and
left the origin anchor and the binding to `--json` and the restore
record: a restore that moved the anchor, or bound the key ring to a new
file and so turned the store it was bound to read-only, said nothing of
it on a terminal. The plan and the result now print both, before and
after, and where the log the restore replaces ended.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import backup
from bettermemory.backup import create_backup
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.log import KeyRing
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


def _store_with_bundle(
    store_file: Path, keys_dir: Path, *, include_key: bool = False
) -> tuple[Path, str, int]:
    """A store, a bundle of it, and two rows after the bundle; the
    anchor's text and the last seq."""
    with Store.create(store_file) as created:
        created.put_memory(_memory("before the bundle"))
        store_id = created.store_id
    bundle = Path(create_backup(store_file, include_key=include_key)["bundle"])
    with Store.open(store_file) as opened:
        for n in range(2):
            opened.put_memory(_memory(f"after the bundle {n}"))
        end = opened.log_rows()[-1].seq
    origin = KeyRing(keys_dir, store_id).read_origin()
    assert origin is not None
    said = f"seq {origin.seq}, mac {origin.mac[:16]}"
    if origin.key_fingerprint is not None:
        said += f", key {origin.key_fingerprint[:16]}"
    return bundle, said, end


def test_a_restore_into_another_directory_prints_the_binding_moving(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = tmp_path / "old" / STORE_FILENAME
    bundle, anchor, end = _store_with_bundle(old, keys_dir)
    new = tmp_path / "new" / STORE_FILENAME
    monkeypatch.setenv("BETTERMEMORY_DIR", str(new.parent))
    moving = f"key ring bound to: {old.resolve()} -> {new.resolve()}"

    assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 3
    plan = capsys.readouterr().out
    assert f"origin anchor: {anchor}, unchanged" in plan
    assert moving in plan
    assert f"the log it replaces ended at seq {end} (read from {old.resolve()})" in plan

    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 0
    result = capsys.readouterr().out
    assert "backup restore: restored" in result
    assert f"origin anchor: {anchor}, unchanged" in result
    assert moving in result


def test_a_restore_onto_a_new_host_prints_the_anchor_and_binding_it_makes(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, anchor, _ = _store_with_bundle(
        tmp_path / "old" / STORE_FILENAME, keys_dir, include_key=True
    )
    new = tmp_path / "new-machine" / "store" / STORE_FILENAME
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "new-machine" / "keys"))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(new.parent))
    argv = ["backup", "restore", str(bundle), "--trust-bundle-key"]

    assert _run_cli(argv, monkeypatch) == 3
    plan = capsys.readouterr().out
    assert f"origin anchor: none -> {anchor}\n" in plan
    assert f"key ring bound to: no file -> {new.resolve()}" in plan
    assert "no copy of the store on this host says where it ended" in plan

    assert _run_cli([*argv, "--yes"], monkeypatch) == 0
    result = capsys.readouterr().out
    assert f"origin anchor: none -> {anchor}\n" in result
    assert f"key ring bound to: no file -> {new.resolve()}" in result


def test_a_restore_that_stops_before_the_swap_prints_them_unchanged(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_file = tmp_path / "store" / STORE_FILENAME
    bundle, anchor, _ = _store_with_bundle(store_file, keys_dir)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))

    def refused(*_args: object, **_kwargs: object) -> None:
        raise OSError("the swap is refused here")

    monkeypatch.setattr(backup, "_swap_in", refused)
    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 1
    result = capsys.readouterr().out
    assert f"origin anchor: {anchor}, not changed" in result
    assert f"key ring bound to: {store_file.resolve()}, not changed" in result
