"""Verified online backups and a safe restore (`bettermemory backup`).

A bundle is a consistent snapshot taken through SQLite's online backup API
while the store stays open for writing, the head checkpoint at the snapshot
and a manifest signed with the store's key. The tests pin the three
commands' contracts: `create` writes an owner-only bundle that excludes an
uncommitted transaction; `verify` catches a changed byte, a rewritten
snapshot whether or not the manifest is re-signed, and a truncated log,
reads `unverifiable` where no key is held, and never changes the bundle;
`restore` refuses while a daemon holds the store and refuses a bundle that
does not verify, keeps the files it replaces, installs the keys the
restored log needs, and rewinds the head on the record so `log verify`
passes where a plain file copy reads `tampered`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client
from bettermemory import _fsutil
from bettermemory import backup
from bettermemory.backup import (
    BackupError,
    create_backup,
    manifest_mac,
    restore_bundle,
    restore_record_path,
    verify_bundle,
)
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.log import REKEY, KeyRing, fingerprint
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


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


def _put(store_file: Path, keys_dir: Path, *bodies: str) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        for body in bodies:
            opened.put_memory(_memory(body))


def _bodies(path: Path) -> set[str]:
    """The memory bodies of the store file at `path`, read without
    `Store.open` (which would change the file and wants `memory.sqlite`)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {
            str(row[0]).strip() for row in conn.execute("SELECT body FROM memories")
        }
    finally:
        conn.close()


def _tree(*roots: Path) -> dict[str, bytes]:
    """Every file under `roots` and its bytes, to compare before and after."""
    found: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                found[str(path)] = path.read_bytes()
    return found


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store of three log rows (created, two memories) that the CLI
    resolves, its key under the test's keys directory."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    created = Store.create(directory / STORE_FILENAME, keys_dir=keys_dir)
    created.close()
    path = directory / STORE_FILENAME
    _put(path, keys_dir, "alpha", "beta")
    return path


@pytest.fixture
def bundle(store_file: Path) -> Path:
    return Path(create_backup(store_file)["bundle"])


def _manifest(bundle_dir: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((bundle_dir / "manifest.json").read_text())
    return loaded


def _write_manifest(bundle_dir: Path, manifest: dict[str, Any]) -> None:
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _rehash(bundle_dir: Path, manifest: dict[str, Any], *names: str) -> None:
    for name in names:
        data = (bundle_dir / name).read_bytes()
        manifest["files"][name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }


def _key(keys_dir: Path, store_id: str) -> bytes:
    key = KeyRing(keys_dir, store_id).load_current()
    assert key is not None
    return key


def _problems(report: dict[str, Any]) -> set[str]:
    return {str(p["problem"]) for p in report["problems"]}


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_writes_a_signed_bundle_with_the_manifest_fields(
    store_file: Path, keys_dir: Path
) -> None:
    result = create_backup(store_file)
    bundle_dir = Path(result["bundle"])
    assert bundle_dir.parent == store_file.parent / "backups"
    manifest = result["manifest"]
    assert manifest == _manifest(bundle_dir)
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        store_id = opened.store_id
        key_fingerprint = opened.key_fingerprint
        last = opened.log_rows()[-1]
    assert manifest["format"] == "bettermemory.backup"
    assert manifest["format_version"] == 1
    assert manifest["bettermemory_version"]
    assert manifest["created_at"].endswith("Z")
    assert manifest["store_id"] == store_id
    assert manifest["source"] == str(store_file)
    assert manifest["key_fingerprint"] == key_fingerprint
    assert manifest["key_fingerprints"] == [key_fingerprint]
    assert manifest["key_included"] is False
    assert manifest["head"] == {"seq": 3, "mac": last.mac}
    assert manifest["counts"]["memories"] == 2
    assert manifest["counts"]["log"] == 3
    assert manifest["counts"]["tombstones"] == 0
    assert set(manifest["files"]) == {STORE_FILENAME, f"{store_id}.head"}
    for name, entry in manifest["files"].items():
        data = (bundle_dir / name).read_bytes()
        assert entry == {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    assert manifest["mac"] == manifest_mac(_key(keys_dir, store_id), manifest)
    head = json.loads((bundle_dir / f"{store_id}.head").read_text())
    assert (head["seq"], head["mac"]) == (3, last.mac)
    # One self-contained file: no journal beside it, and no key without
    # --include-key.
    assert sorted(p.name for p in bundle_dir.iterdir()) == sorted(
        [STORE_FILENAME, f"{store_id}.head", "manifest.json"]
    )
    assert result["warnings"] == []


@POSIX_ONLY
def test_the_bundle_is_owner_only(store_file: Path) -> None:
    result = create_backup(store_file, include_key=True)
    bundle_dir = Path(result["bundle"])
    assert stat.S_IMODE(bundle_dir.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(bundle_dir.stat().st_mode) == 0o700
    names = {p.name for p in bundle_dir.iterdir()}
    assert f"{result['manifest']['store_id']}.key" in names
    for path in bundle_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


@POSIX_ONLY
def test_create_leaves_the_live_store_as_it_was_and_its_wal_owner_only(
    store_file: Path, keys_dir: Path
) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        before = [tuple(row) for row in opened.log_rows()]
    create_backup(store_file)
    for suffix in ("-wal", "-shm"):
        sibling = store_file.with_name(store_file.name + suffix)
        if sibling.exists():
            assert stat.S_IMODE(sibling.stat().st_mode) == 0o600
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        assert [tuple(row) for row in opened.log_rows()] == before
        assert opened.log_verify()["status"] == "ok"


def test_a_snapshot_under_an_open_write_leaves_it_out_and_verifies(
    store_file: Path, keys_dir: Path
) -> None:
    """The daemon keeps writing while a backup runs. A transaction open in
    another connection is not in the snapshot, and the snapshot verifies."""
    writer = Store.open(store_file, keys_dir=keys_dir)
    try:
        with writer.batch():
            writer.put_memory(_memory("gamma, uncommitted at the snapshot"))
            result = create_backup(store_file)
        assert writer.count_memories() == 3
    finally:
        writer.close()
    bundle_dir = Path(result["bundle"])
    assert result["manifest"]["counts"]["memories"] == 2
    assert _bodies(bundle_dir / STORE_FILENAME) == {"alpha", "beta"}
    assert verify_bundle(bundle_dir)["status"] == "ok"


def test_bundles_in_one_second_get_their_own_directories(
    store_file: Path, tmp_path: Path
) -> None:
    moment = datetime(2026, 10, 12, 5, 39, tzinfo=timezone.utc)
    out = tmp_path / "elsewhere" / "bundles"
    first = Path(create_backup(store_file, out_dir=out, now=moment)["bundle"])
    second = Path(create_backup(store_file, out_dir=out, now=moment)["bundle"])
    assert first == out / "20261012T053900Z"
    assert second == out / "20261012T053900Z-2"
    assert verify_bundle(second)["status"] == "ok"


def test_include_key_refuses_a_store_whose_key_is_not_held(
    store_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "no-keys"
    monkeypatch.setenv(KEYS_DIR_ENV, str(empty))
    assert _run_cli(["backup", "create", "--include-key"], monkeypatch) == 1
    assert "no key" in capsys.readouterr().err
    backups = store_file.parent / "backups"
    assert not backups.exists() or not any(backups.iterdir())


def test_a_bundle_written_without_the_key_is_unsigned_and_unverifiable(
    store_file: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "no-keys"))
    assert _run_cli(["backup", "create", "--json"], monkeypatch) == 0
    captured = capsys.readouterr()
    assert "not signed" in captured.err
    result = json.loads(captured.out)
    assert "mac" not in result["manifest"]
    report = verify_bundle(Path(result["bundle"]), keys_dir=keys_dir)
    assert report["status"] == "unverifiable"
    assert report["manifest_mac"] == "absent"
    assert "manifest_unsigned" in _problems(report)
    assert report["log"]["status"] == "ok"


def test_create_cleans_up_a_bundle_it_could_not_finish(
    store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> Any:
        raise sqlite3.DatabaseError("disk image is malformed")

    monkeypatch.setattr(backup, "_snapshot_facts", broken)
    with pytest.raises(BackupError, match="malformed"):
        create_backup(store_file)
    assert list((store_file.parent / "backups").iterdir()) == []


def test_create_refuses_a_path_with_no_store(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no bettermemory 9 store"):
        create_backup(tmp_path / STORE_FILENAME)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_a_bundle_verifies_and_verifying_changes_nothing_in_it(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(bundle)
    report = verify_bundle(bundle)
    assert report["status"] == "ok"
    assert report["manifest_mac"] == "ok"
    assert report["problems"] == []
    assert all(f["status"] == "ok" for f in report["files"])
    assert report["keys"] == [
        {"fingerprint": report["log"]["key_fingerprint"], "source": "keys_dir"}
    ]
    assert report["log"]["path"] == str(bundle / STORE_FILENAME)
    assert report["log"]["head"]["status"] == "ok"
    assert _tree(bundle) == before

    assert _run_cli(["backup", "verify", str(bundle), "--json"], monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert _run_cli(["backup", "verify", str(bundle)], monkeypatch) == 0
    text = capsys.readouterr().out
    assert "backup verify: ok" in text and "manifest signature ok" in text
    assert _tree(bundle) == before


def test_a_flipped_byte_fails_the_sha_check(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = bundle / STORE_FILENAME
    data = bytearray(snapshot.read_bytes())
    data[len(data) // 2] ^= 0xFF
    snapshot.write_bytes(bytes(data))
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert {"problem": "sha256_mismatch", "file": STORE_FILENAME} in report["problems"]
    assert _run_cli(["backup", "verify", str(bundle)], monkeypatch) == 1
    assert "sha256_mismatch" in capsys.readouterr().out


def test_a_rewritten_snapshot_with_a_rehashed_manifest_fails_the_signature(
    bundle: Path,
) -> None:
    """An editor without the key changes a body in the bundle's snapshot
    and rewrites the manifest's hash to match; the signature does not."""
    snapshot = bundle / STORE_FILENAME
    conn = sqlite3.connect(str(snapshot))
    conn.execute("UPDATE memories SET body = 'planted\n' WHERE body LIKE 'alpha%'")
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert report["manifest_mac"] == "mismatch"
    assert "manifest_mac_mismatch" in _problems(report)


def test_a_key_holder_who_resigns_the_manifest_is_caught_by_the_log(
    bundle: Path, keys_dir: Path
) -> None:
    snapshot = bundle / STORE_FILENAME
    conn = sqlite3.connect(str(snapshot))
    conn.execute("UPDATE memories SET body = 'planted\n' WHERE body LIKE 'alpha%'")
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    manifest["mac"] = manifest_mac(_key(keys_dir, manifest["store_id"]), manifest)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["manifest_mac"] == "ok"
    assert report["status"] == "tampered"
    assert report["log"]["fold"]["status"] == "diverged"
    assert backup.VERIFY_EXIT[report["status"]] == 1


def test_a_truncated_log_with_a_rewritten_head_is_caught(bundle: Path) -> None:
    """The deleted tail the head checkpoint exists to catch, done inside a
    bundle where the head sits beside the snapshot: the last row and the
    memory it wrote are removed, and the head file and the manifest's head
    and hashes rewritten to the row before. Only the signature is left."""
    snapshot = bundle / STORE_FILENAME
    conn = sqlite3.connect(str(snapshot))
    conn.execute("DELETE FROM memories WHERE body LIKE 'beta%'")
    conn.execute("DELETE FROM log WHERE seq = 3")
    seq, mac = conn.execute("SELECT seq, mac FROM log ORDER BY seq DESC").fetchone()
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    head_name = f"{manifest['store_id']}.head"
    (bundle / head_name).write_text(json.dumps({"seq": seq, "mac": mac, "ts": ""}))
    manifest["head"] = {"seq": seq, "mac": mac}
    _rehash(bundle, manifest, STORE_FILENAME, head_name)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert "manifest_mac_mismatch" in _problems(report)


def test_a_head_file_that_disagrees_with_the_manifest_is_caught(
    bundle: Path,
) -> None:
    manifest = _manifest(bundle)
    head_name = f"{manifest['store_id']}.head"
    (bundle / head_name).write_text(json.dumps({"seq": 2, "mac": "00", "ts": ""}))
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert {"head_mismatch", "size_mismatch"} <= _problems(report)


def test_without_a_key_a_bundle_is_unverifiable_until_one_is_named(
    store_file: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain = Path(create_backup(store_file)["bundle"])
    with_key = Path(create_backup(store_file, include_key=True)["bundle"])
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "no-keys"))

    report = verify_bundle(plain)
    assert report["status"] == "unverifiable"
    assert report["manifest_mac"] == "unverifiable"
    assert report["keys"][0]["source"] is None
    assert "key_absent" in _problems(report)
    assert _run_cli(["backup", "verify", str(plain)], monkeypatch) == 2
    assert "held nowhere" in capsys.readouterr().out

    argv = ["backup", "verify", str(plain), "--keys-dir", str(keys_dir)]
    assert _run_cli(argv, monkeypatch) == 0
    capsys.readouterr()

    # A bundle that carries its key is whole wherever it goes, but its
    # signature is under that same key: where the keys directory does not
    # hold it, the signature is the bundle's word for itself.
    report = verify_bundle(with_key)
    assert report["status"] == "unverifiable"
    assert report["manifest_mac"] == "self_signed"
    assert _problems(report) == {"manifest_self_signed"}
    assert report["log"]["status"] == "ok"
    assert report["key_included"] is True
    assert report["keys"][0]["source"] == "bundle"
    assert _run_cli(["backup", "verify", str(with_key)], monkeypatch) == 2
    assert "proves the bundle whole but not who made it" in capsys.readouterr().out

    # Where the keys directory holds the key, the signature counts and the
    # key is reported as the keys directory's, though the bundle has it too.
    report = verify_bundle(with_key, keys_dir=keys_dir)
    assert report["status"] == "ok"
    assert report["manifest_mac"] == "ok"
    assert report["keys"][0]["source"] == "keys_dir"


def test_a_rekeyed_store_bundles_and_verifies_every_segment(
    store_file: Path, keys_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        store_id = opened.store_id
        first = opened.key_fingerprint
    ring = KeyRing(keys_dir, store_id)
    ring.retire_current()
    with Store.open(store_file, keys_dir=keys_dir, allow_rekey=True) as opened:
        second = opened.key_fingerprint
        opened.put_memory(_memory("gamma, under the second key"))
    assert first != second

    result = create_backup(store_file, include_key=True)
    bundle_dir = Path(result["bundle"])
    assert result["manifest"]["key_fingerprints"] == [first, second]
    assert (bundle_dir / f"{store_id}.{first[:16]}.key").is_file()
    assert (bundle_dir / f"{store_id}.key").is_file()

    report = verify_bundle(bundle_dir)
    assert report["status"] == "ok"
    assert {k["source"] for k in report["keys"]} == {"keys_dir"}

    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "no-keys"))
    report = verify_bundle(bundle_dir)
    assert report["status"] == "unverifiable"
    assert _problems(report) == {"manifest_self_signed"}
    assert report["log"]["status"] == "ok"
    assert [s["key_fingerprint"] for s in report["log"]["segments"]] == [first, second]
    assert {k["source"] for k in report["keys"]} == {"bundle"}


def test_a_missing_retired_key_is_a_warning_at_create(
    store_file: Path, keys_dir: Path
) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        store_id = opened.store_id
        first = opened.key_fingerprint
    ring = KeyRing(keys_dir, store_id)
    ring.retire_current()
    ring.retired_path(first).unlink()
    Store.open(store_file, keys_dir=keys_dir, allow_rekey=True).close()
    result = create_backup(store_file, include_key=True)
    assert any(first[:16] in warning for warning in result["warnings"])
    report = verify_bundle(Path(result["bundle"]))
    assert report["status"] == "unverifiable"
    assert {"problem": "key_absent", "fingerprint": first} in report["problems"]


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        ("no manifest", "manifest_missing"),
        ("not json", "manifest_invalid"),
        ("not a manifest", "manifest_invalid"),
        ("future format", "format_unsupported"),
        ("escaping name", "manifest_invalid"),
        ("unlisted snapshot", "manifest_invalid"),
        ("missing file", "file_missing"),
        ("wrong size", "size_mismatch"),
        ("non-ascii hash", "manifest_invalid"),
        ("non-ascii signature", "manifest_invalid"),
        ("non-ascii head", "manifest_invalid"),
    ],
)
def test_a_bundle_that_does_not_match_its_manifest_is_mismatched(
    bundle: Path, damage: str, problem: str
) -> None:
    manifest = _manifest(bundle)
    if damage == "no manifest":
        (bundle / "manifest.json").unlink()
    elif damage == "not json":
        (bundle / "manifest.json").write_text("{")
    elif damage == "not a manifest":
        (bundle / "manifest.json").write_text("[]")
    elif damage == "future format":
        manifest["format_version"] = 2
        _write_manifest(bundle, manifest)
    elif damage == "escaping name":
        manifest["files"]["../memory.sqlite"] = manifest["files"][STORE_FILENAME]
        _write_manifest(bundle, manifest)
    elif damage == "unlisted snapshot":
        del manifest["files"][STORE_FILENAME]
        _write_manifest(bundle, manifest)
    elif damage == "missing file":
        (bundle / f"{manifest['store_id']}.head").unlink()
    elif damage == "wrong size":
        manifest["files"][STORE_FILENAME]["size"] += 1
        _write_manifest(bundle, manifest)
    elif damage == "non-ascii hash":
        # `hmac.compare_digest` raises on a non-ASCII string: a digest of
        # any other shape is refused before anything compares it.
        manifest["files"][STORE_FILENAME]["sha256"] = "é" * 64
        _write_manifest(bundle, manifest)
    elif damage == "non-ascii signature":
        manifest["mac"] = "é"
        _write_manifest(bundle, manifest)
    elif damage == "non-ascii head":
        manifest["head"]["mac"] = "é"
        _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert problem in _problems(report)
    assert backup.VERIFY_EXIT[report["status"]] == 1


@POSIX_ONLY
@pytest.mark.parametrize("name", [STORE_FILENAME, "head"])
def test_a_bundle_file_that_cannot_be_read_is_mismatched_not_raised(
    bundle: Path,
    store_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """A file this account cannot read is reported as `file_unreadable`,
    and verify and restore say so, where both ended in a PermissionError
    traceback."""
    import os

    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX file modes")
    if os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    if name == "head":
        name = f"{_manifest(bundle)['store_id']}.head"
    target = bundle / name
    target.chmod(0)
    try:
        report = verify_bundle(bundle)
        assert report["status"] == "mismatched"
        assert {"file_unreadable"} <= _problems(report)
        assert {f["name"]: f["status"] for f in report["files"]}[name] == "unreadable"
        assert _run_cli(["backup", "verify", str(bundle)], monkeypatch) == 1
        assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 1
    finally:
        target.chmod(0o600)
    out = capsys.readouterr().out
    assert f"{name} unreadable" in out
    assert "does not verify (mismatched)" in out


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["log", "verify"], 1),
        (["log", "export"], 2),
        (["log", "anchor"], 2),
        (["stats"], 2),
        (["memory", "history", "01J0000000000000000000000A"], 2),
    ],
)
def test_a_read_only_command_refuses_a_bundle_as_the_store(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
) -> None:
    """With `BETTERMEMORY_DIR` naming a bundle, the read-only commands
    opened its snapshot in place, which switches the file to WAL: the
    bundle then failed its hash, and `backup restore` refused it. They
    refuse it now, with the command's code for a store it cannot read."""
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    assert _run_cli(argv, monkeypatch) == code
    assert "backup bundle" in capsys.readouterr().err
    assert _tree(bundle) == before
    assert verify_bundle(bundle)["status"] == "ok"


def test_a_snapshot_that_is_not_a_database_is_unreadable(bundle: Path) -> None:
    manifest = _manifest(bundle)
    (bundle / STORE_FILENAME).write_bytes(b"not a database" * 100)
    _rehash(bundle, manifest, STORE_FILENAME)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert "snapshot_unreadable" in _problems(report)


@pytest.mark.parametrize(
    "edit",
    [
        "UPDATE meta SET value = 'x' WHERE key = 'schema_version'",
        "CREATE TABLE old AS SELECT * FROM log; DROP TABLE log; "
        "CREATE TABLE log (seq, ts, session, kind, payload, prev_mac, mac); "
        "INSERT INTO log SELECT * FROM old; DROP TABLE old; "
        "UPDATE log SET seq = 'last' WHERE seq = 3",
        "UPDATE log SET mac = 'é' WHERE seq = 1",
    ],
    ids=["schema version", "seq", "row mac"],
)
def test_rows_that_do_not_read_as_a_stores_are_reported_not_crashed_on(
    bundle: Path, edit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot edited so a number is not one, or a MAC is not a digest,
    reads as ``snapshot_unreadable`` and the command exits 1, rather than
    raising out of `verify_bundle`."""
    conn = sqlite3.connect(str(bundle / STORE_FILENAME))
    conn.executescript(edit)
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert "snapshot_unreadable" in _problems(report)
    assert _run_cli(["backup", "verify", str(bundle)], monkeypatch) == 1


def test_a_snapshot_from_another_store_contradicts_the_manifest(
    bundle: Path, store_file: Path, tmp_path: Path, keys_dir: Path
) -> None:
    other = Store.create(tmp_path / "other" / STORE_FILENAME, keys_dir=keys_dir)
    other.close()
    foreign = Path(create_backup(other.path)["bundle"])
    manifest = _manifest(bundle)
    (bundle / STORE_FILENAME).write_bytes((foreign / STORE_FILENAME).read_bytes())
    _rehash(bundle, manifest, STORE_FILENAME)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    fields = {p.get("field") for p in report["problems"]}
    assert {"store_id", "key_fingerprint", "head"} <= fields


def test_a_store_id_that_names_a_path_is_refused_everywhere(
    bundle: Path, store_file: Path, keys_dir: Path, tmp_path: Path
) -> None:
    """The store id names the key ring's files, in the bundle and in the
    keys directory (`log.KeyRing`). Here a holder of the key builds a
    bundle whose snapshot and signed manifest agree on an id carrying
    ``..``, with the key ring laid out where that id points: restoring it
    would write the key, the head and the record outside the keys
    directory. Verify reads the manifest as invalid and restore refuses
    it, and create refuses a live store whose meta names such an id
    rather than write its head outside the bundle."""
    escape = "../planted"
    manifest = _manifest(bundle)
    store_id = manifest["store_id"]
    key = _key(keys_dir, store_id)
    conn = sqlite3.connect(str(bundle / STORE_FILENAME))
    conn.execute("UPDATE meta SET value = ? WHERE key = 'store_id'", (escape,))
    conn.commit()
    conn.close()
    ring = KeyRing(bundle, escape)
    assert ring.current_path().parent.resolve() == bundle.parent.resolve()
    head = bundle / f"{store_id}.head"
    ring.head_path().write_bytes(head.read_bytes())
    head.rename(bundle / ring.head_path().name)
    ring.current_path().write_bytes(key)
    laid = {ring.current_path().resolve(), ring.head_path().resolve()}
    manifest["store_id"] = escape
    del manifest["files"][head.name]
    _rehash(bundle, manifest, STORE_FILENAME, ring.head_path().name)
    manifest["mac"] = manifest_mac(key, manifest)
    _write_manifest(bundle, manifest)

    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    assert "manifest_invalid" in _problems(report)

    victim = tmp_path / "victim"
    report = restore_bundle(
        bundle, victim / "store" / STORE_FILENAME, apply=True, keys_dir=victim / "keys"
    )
    assert report["status"] == "refused"
    assert not list(victim.rglob("planted*"))

    conn = sqlite3.connect(str(store_file))
    conn.execute("UPDATE meta SET value = ? WHERE key = 'store_id'", (escape,))
    conn.commit()
    conn.close()
    with pytest.raises(BackupError, match="not one"):
        create_backup(store_file)
    found = {p.resolve() for p in tmp_path.rglob("planted*")}
    assert found - {(bundle / ring.head_path().name).resolve()} == laid


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _lock(store_file: Path, *, spawn: bool = False) -> Any:
    state_dir = _daemon_client.resolve_state_dir()
    path = (
        _daemon_client.spawn_lock_file_for(state_dir, store_file)
        if spawn
        else _daemon_client.lock_file_for(state_dir, store_file)
    )
    handle = _daemon_client.try_lock(path)
    assert handle is not None
    return handle


@pytest.mark.parametrize("spawn", [False, True])
def test_restore_refuses_while_a_daemon_holds_the_store(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    spawn: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put(store_file, keys_dir, "gamma")
    before = _tree(store_file.parent, keys_dir)
    handle = _lock(store_file, spawn=spawn)
    try:
        code = _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch)
    finally:
        _daemon_client.release_lock(handle)
    assert code == 1
    out = capsys.readouterr().out
    assert "refused" in out and "bettermemory down" in out
    assert _tree(store_file.parent, keys_dir) == before


def test_restore_without_yes_prints_the_plan_and_changes_nothing(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put(store_file, keys_dir, "gamma")
    before = _tree(store_file.parent, keys_dir)
    assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 3
    out = capsys.readouterr().out
    assert "plan (nothing changed" in out
    assert "move aside" in out and "seq 4 -> seq 3" in out
    assert _tree(store_file.parent, keys_dir) == before

    assert _run_cli(["backup", "restore", str(bundle), "--json"], monkeypatch) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["plan"]["head"]["before"]["seq"] == 4
    assert report["plan"]["head"]["after"]["seq"] == 3
    assert report["plan"]["current_store_id"] == report["plan"]["bundle_store_id"]
    assert report["plan"]["keys"] == []
    assert _tree(store_file.parent, keys_dir) == before


def test_a_plain_copy_of_an_older_store_reads_tampered(
    bundle: Path, store_file: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The weakness the restore exists for: the head only moves forward, so
    an older store copied back by hand reads tampered under `log verify`,
    first as a deleted tail and, after the next write, as a head mismatch."""
    _put(store_file, keys_dir, "gamma")
    store_file.write_bytes((bundle / STORE_FILENAME).read_bytes())
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        report = opened.log_verify()
    assert report["status"] == "tampered"
    assert report["head"]["status"] == "tail_deleted"
    _put(store_file, keys_dir, "delta")
    assert _run_cli(["log", "verify"], monkeypatch) == 1


def test_restore_round_trips_rewinds_the_head_on_the_record_and_keeps_the_old(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put(store_file, keys_dir, "gamma, after the backup")
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        store_id = opened.store_id
        old_head = opened.log_rows()[-1]
    assert old_head.seq == 4

    argv = ["backup", "restore", str(bundle), "--yes", "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "restored"
    assert report["log"]["status"] == "ok"
    manifest = _manifest(bundle)
    assert report["head"] == {
        "before": {"seq": 4, "mac": old_head.mac},
        "after": manifest["head"],
    }

    assert _run_cli(["log", "verify"], monkeypatch) == 0
    capsys.readouterr()
    assert _bodies(store_file) == {"alpha", "beta"}

    # The replaced store is kept beside it, with the row the bundle lacks.
    moved = report["moved_aside"]
    assert moved and moved[0]["from"] == str(store_file)
    aside = Path(moved[0]["to"])
    assert aside.name.startswith(f"{STORE_FILENAME}.pre-restore-")
    assert _bodies(aside) == {"alpha", "beta", "gamma, after the backup"}

    record = json.loads(restore_record_path(keys_dir, store_id).read_text())
    assert record["format"] == "bettermemory.backup_restore"
    [entry] = record["restores"]
    assert entry["head_before"] == {"seq": 4, "mac": old_head.mac}
    assert entry["head_after"] == manifest["head"]
    assert entry["bundle"] == str(bundle)
    assert entry["store_id"] == store_id
    assert entry["moved_aside"] == moved
    assert report["record"] == str(restore_record_path(keys_dir, store_id))

    # The next write moves the head on from the rewound seq.
    _put(store_file, keys_dir, "delta")
    assert _run_cli(["log", "verify"], monkeypatch) == 0


def test_restore_text_names_the_moves_and_the_rewind(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put(store_file, keys_dir, "gamma")
    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "backup restore: restored" in out
    assert "moved aside:" in out and ".pre-restore-" in out
    assert "head checkpoint: seq 4 -> seq 3" in out
    assert "log verify of the restored store: ok" in out


def test_restore_refuses_a_bundle_that_does_not_verify(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = bundle / STORE_FILENAME
    data = bytearray(snapshot.read_bytes())
    data[len(data) // 2] ^= 0xFF
    snapshot.write_bytes(bytes(data))
    before = _tree(store_file.parent, keys_dir)
    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "does not verify (mismatched)" in out and "sha256_mismatch" in out
    assert _tree(store_file.parent, keys_dir) == before


def test_restore_on_a_machine_without_the_key_installs_it_from_the_bundle(
    store_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    carried = Path(create_backup(store_file, include_key=True)["bundle"])
    manifest = _manifest(carried)
    new_store = tmp_path / "new-machine" / "store" / STORE_FILENAME
    new_keys = tmp_path / "new-machine" / "keys"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(new_store.parent))
    monkeypatch.setenv(KEYS_DIR_ENV, str(new_keys))

    # The bundle's signature is under the key it carries, which this
    # machine has never held: refused until the caller trusts that key.
    report = restore_bundle(carried, new_store, apply=True)
    assert report["status"] == "refused"
    assert not new_keys.exists() or not any(new_keys.iterdir())
    assert not new_store.exists()

    report = restore_bundle(carried, new_store, apply=True, trust_bundle_key=True)
    assert report["status"] == "restored", report["detail"]
    assert report["plan"]["trusted_bundle_key"] == manifest["key_fingerprint"]
    record = json.loads(restore_record_path(new_keys, manifest["store_id"]).read_text())
    assert record["restores"][-1]["trusted_bundle_key"] == manifest["key_fingerprint"]
    assert report["moved_aside"] == []
    assert report["head"]["before"] is None
    assert [a["action"] for a in report["keys_installed"]] == ["make_current"]
    installed = KeyRing(new_keys, manifest["store_id"]).current_path()
    assert fingerprint(installed.read_bytes()) == manifest["key_fingerprint"]
    if sys.platform != "win32":
        assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert _run_cli(["log", "verify"], monkeypatch) == 0

    # The owner's opener finds the store's own key: no rekey row.
    with Store.open_or_create(new_store) as opened:
        assert [r.kind for r in opened.log_rows()].count(REKEY) == 0
        assert len(opened.log_rows()) == 3


def test_restore_makes_the_bundles_key_current_and_retires_a_later_one(
    bundle: Path, store_file: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(bundle)
    store_id = manifest["store_id"]
    ring = KeyRing(keys_dir, store_id)
    ring.retire_current()
    with Store.open(store_file, keys_dir=keys_dir, allow_rekey=True) as opened:
        later = opened.key_fingerprint
    assert later != manifest["key_fingerprint"]

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "restored", report["detail"]
    [action] = report["keys_installed"]
    assert action == {
        "fingerprint": manifest["key_fingerprint"],
        "action": "make_current",
        "source": "keys_dir",
        "retires": later,
    }
    assert ring.load_current() is not None
    assert fingerprint(ring.load_current() or b"") == manifest["key_fingerprint"]
    assert ring.key_for_fingerprint(later) is not None
    assert _run_cli(["log", "verify"], monkeypatch) == 0


def test_restore_adds_a_retired_key_the_bundle_carries(
    store_file: Path, keys_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        store_id = opened.store_id
        first = opened.key_fingerprint
    KeyRing(keys_dir, store_id).retire_current()
    Store.open(store_file, keys_dir=keys_dir, allow_rekey=True).close()
    carried = Path(create_backup(store_file, include_key=True)["bundle"])
    new_keys = tmp_path / "new-keys"
    monkeypatch.setenv(KEYS_DIR_ENV, str(new_keys))
    new_store = tmp_path / "new-store" / STORE_FILENAME
    report = restore_bundle(carried, new_store, apply=True, trust_bundle_key=True)
    assert report["status"] == "restored", report["detail"]
    actions = {a["action"]: a["fingerprint"] for a in report["keys_installed"]}
    assert actions["add_retired"] == first
    assert KeyRing(new_keys, store_id).retired_path(first).is_file()


def test_restore_over_another_store_names_both_and_keeps_the_other(
    bundle: Path, tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_dir = tmp_path / "other"
    other = Store.create(other_dir / STORE_FILENAME, keys_dir=keys_dir)
    other_id = other.store_id
    other.put_memory(_memory("the other store's"))
    other.close()
    report = restore_bundle(bundle, other_dir / STORE_FILENAME, apply=False)
    assert report["status"] == "planned"
    assert report["plan"]["current_store_id"] == other_id
    assert report["plan"]["bundle_store_id"] != other_id
    report = restore_bundle(bundle, other_dir / STORE_FILENAME, apply=True)
    assert report["status"] == "restored", report["detail"]
    aside = Path(report["moved_aside"][0]["to"])
    assert _bodies(aside) == {"the other store's"}
    assert KeyRing(keys_dir, other_id).load_current() is not None


def test_an_unreadable_restore_record_is_moved_aside_not_overwritten(
    bundle: Path, store_file: Path, keys_dir: Path
) -> None:
    record = restore_record_path(keys_dir, _manifest(bundle)["store_id"])
    record.write_text("not json")
    moment = datetime(2026, 10, 12, 5, 39, tzinfo=timezone.utc)
    report = restore_bundle(bundle, store_file, apply=True, now=moment)
    assert report["status"] == "restored", report["detail"]
    kept = record.with_name(record.name + ".unreadable-20261012T053900Z")
    assert kept.read_text() == "not json"
    assert len(json.loads(record.read_text())["restores"]) == 1

    later = moment + timedelta(minutes=1)
    assert restore_bundle(bundle, store_file, apply=True, now=later)["status"] == (
        "restored"
    )
    assert len(json.loads(record.read_text())["restores"]) == 2


def test_a_failed_step_reports_where_the_files_are(
    bundle: Path,
    store_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*args: Any) -> None:
        raise OSError("the keys directory is read-only")

    monkeypatch.setattr(backup, "_install_keys", broken)
    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "backup restore: failed" in out and "read-only" in out
    assert "moved aside:" in out
    # The failure came before the rewind: the text says so, and does not
    # print the plan's rewind as if it had happened.
    assert "head checkpoint: seq 3, not rewound" in out
    assert "recorded in" not in out


def test_a_failed_record_write_says_the_rewind_is_not_recorded(
    store_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    carried = Path(create_backup(store_file, include_key=True)["bundle"])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "new" / "store"))
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "new" / "keys"))

    def broken(*args: Any) -> None:
        raise OSError("the keys directory is full")

    monkeypatch.setattr(backup, "_append_record", broken)
    argv = ["backup", "restore", str(carried), "--yes", "--trust-bundle-key"]
    assert _run_cli(argv, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "installed as the current key from the bundle" in out
    assert "head checkpoint: none -> seq 3, the rewind not recorded" in out


def test_a_restored_store_that_does_not_verify_is_a_failure(
    bundle: Path, store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Store.log_verify
    calls: list[int] = []

    def second_call_tampered(self: Store) -> dict[str, Any]:
        calls.append(1)
        report = real(self)
        if len(calls) > 1:
            report["status"] = "tampered"
        return report

    monkeypatch.setattr(Store, "log_verify", second_call_tampered)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert "reads tampered" in str(report["detail"])
    assert backup.RESTORE_EXIT[report["status"]] == 1


def test_a_failed_swap_puts_the_store_back(store_file: Path, tmp_path: Path) -> None:
    wal = store_file.with_name(STORE_FILENAME + "-wal")
    wal.write_bytes(b"")
    before = store_file.read_bytes()
    moved: list[dict[str, str]] = []
    aside = store_file.with_name(f"{STORE_FILENAME}.pre-restore-x")
    with pytest.raises(OSError):
        backup._swap_in(tmp_path / "never-prepared", store_file, aside, moved)
    assert moved == []
    assert store_file.read_bytes() == before
    assert wal.exists()
    assert not aside.exists()


def test_a_store_that_reappears_mid_swap_is_refused(
    store_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that opens the store while it is being moved aside would
    leave a file at the store's path; the swap refuses to put the bundle's
    over it, and the moved store stays where the report says."""
    prepared = tmp_path / "prepared"
    prepared.write_bytes(b"prepared")
    aside = store_file.with_name(f"{STORE_FILENAME}.pre-restore-x")
    real = _fsutil.replace_atomic

    def reappearing(src: Path, dst: Path) -> None:
        real(src, dst)
        if Path(src) == store_file:
            store_file.write_bytes(b"a store another process created")

    monkeypatch.setattr(backup, "replace_atomic", reappearing)
    moved: list[dict[str, str]] = []
    with pytest.raises(BackupError, match="reappeared"):
        backup._swap_in(prepared, store_file, aside, moved)
    assert moved == [{"from": str(store_file), "to": str(aside)}]
    assert prepared.read_bytes() == b"prepared"


def test_a_wal_stays_with_its_store_when_the_swap_backs_out(
    store_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store file that reappears after the store moved aside, its -wal
    with it. Putting the -wal back would pair it with a file it was never
    part of (SQLite replays a WAL into whatever database carries its name)
    and leave the store aside without the rows only its WAL holds, so
    nothing moves back and the store's files stay aside together."""
    wal = store_file.with_name(STORE_FILENAME + "-wal")
    wal.write_bytes(b"rows the moved store committed")
    prepared = tmp_path / "prepared"
    prepared.write_bytes(b"prepared")
    aside = store_file.with_name(f"{STORE_FILENAME}.pre-restore-x")
    aside_wal = aside.with_name(aside.name + "-wal")
    real = _fsutil.replace_atomic

    def reappearing(src: Path, dst: Path) -> None:
        real(src, dst)
        if Path(src) == store_file:
            store_file.write_bytes(b"a store another process created")

    monkeypatch.setattr(backup, "replace_atomic", reappearing)
    moved: list[dict[str, str]] = []
    with pytest.raises(BackupError, match="reappeared"):
        backup._swap_in(prepared, store_file, aside, moved)
    assert moved == [
        {"from": str(store_file), "to": str(aside)},
        {"from": str(wal), "to": str(aside_wal)},
    ]
    assert aside_wal.read_bytes() == b"rows the moved store committed"
    assert not wal.exists()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_backup_without_a_subcommand_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["backup"], monkeypatch) == 2
    out = capsys.readouterr().out
    assert "create" in out and "verify" in out and "restore" in out


def test_create_refuses_a_directory_without_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["backup", "create"], monkeypatch) == 2
    assert STORE_FILENAME in capsys.readouterr().err


@pytest.mark.parametrize("command", ["verify", "restore"])
def test_a_missing_bundle_is_a_usage_error(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["backup", command, str(tmp_path / "nope")], monkeypatch) == 2
    assert "no bundle directory" in capsys.readouterr().err


def test_create_text_and_json(
    store_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_dir = tmp_path / "out"
    assert _run_cli(["backup", "create", "--out", str(out_dir)], monkeypatch) == 0
    text = capsys.readouterr().out
    assert f"wrote {out_dir}" in text
    assert "not included" in text and "backup verify" in text
    assert "2 memories" in text

    argv = ["backup", "create", "--out", str(out_dir), "--include-key", "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["bundle"]).parent == out_dir
    assert result["manifest"]["key_included"] is True

    assert _run_cli(["backup", "create", "--include-key"], monkeypatch) == 0
    assert "INCLUDED: anyone holding this bundle can sign rows" in (
        capsys.readouterr().out
    )


def test_the_render_functions_cover_every_report_shape(
    bundle: Path, store_file: Path, keys_dir: Path
) -> None:
    from bettermemory.cli import backup_cmd

    (bundle / "manifest.json").unlink()
    text = backup_cmd.render_verify(verify_bundle(bundle))
    assert "mismatched" in text and "manifest_missing" in text

    report: dict[str, Any] = {
        "status": "refused",
        "detail": "a bettermemory daemon serves it",
        "bundle": str(bundle),
        "store": str(store_file),
        "verify": None,
        "plan": None,
        "moved_aside": [],
        "keys_installed": [],
        "head": None,
        "record": None,
        "log": None,
    }
    assert "daemon serves it" in backup_cmd.render_restore(report)


def test_a_manifest_naming_another_key_is_reported_not_crashed_on(
    bundle: Path,
) -> None:
    """The manifest's key and the snapshot's must agree; when they do not,
    both are looked up and the disagreement is reported."""
    manifest = _manifest(bundle)
    manifest["key_fingerprint"] = "ab" * 32
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    [claim] = [p for p in report["problems"] if p["problem"] == "claim_mismatch"]
    assert claim["field"] == "key_fingerprint"
    assert {"problem": "key_absent", "fingerprint": "ab" * 32} in report["problems"]
    assert report["manifest_mac"] == "unverifiable"


def test_an_edited_log_row_is_named_by_seq_in_the_text_report(
    bundle: Path, keys_dir: Path
) -> None:
    from bettermemory.cli import backup_cmd

    snapshot = bundle / STORE_FILENAME
    conn = sqlite3.connect(str(snapshot))
    conn.execute("UPDATE log SET session = 'forged' WHERE seq = 2")
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    manifest["mac"] = manifest_mac(_key(keys_dir, manifest["store_id"]), manifest)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "tampered"
    text = backup_cmd.render_verify(report)
    assert "seq 2 (memory_put): mac_mismatch" in text


def test_a_bundles_text_reaches_the_terminal_without_its_escapes(
    bundle: Path,
    store_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The text reports quote what a bundle and a store file say about
    themselves: the manifest's creation time, the fingerprints rekey rows
    name, the store id in meta. Anyone who can write those can put an
    escape sequence in them; it reaches the terminal as text."""
    conn = sqlite3.connect(str(store_file))
    conn.execute("UPDATE meta SET value = 'x\x1b[2J' WHERE key = 'store_id'")
    conn.commit()
    conn.close()
    assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 3
    out = capsys.readouterr().out
    assert "current store x" in out and "the bundle is a different store" in out
    assert "\x1b" not in out

    manifest = _manifest(bundle)
    manifest["created_at"] = "\x1b]0;owned\x07\x1b[8m"
    _write_manifest(bundle, manifest)
    assert _run_cli(["backup", "verify", str(bundle)], monkeypatch) == 1
    assert _run_cli(["backup", "restore", str(bundle)], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "created" in out and "does not verify (mismatched)" in out
    assert "\x1b" not in out and "\x07" not in out
    # The verify report a refused restore carries keeps its own lines.
    assert "\nbettermemory backup verify: mismatched\n    bundle " in out
    assert "\\n    bundle" not in out


@pytest.mark.parametrize("include_key", [False, True])
def test_backup_creates_warnings_and_errors_reach_the_terminal_as_text(
    store_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    include_key: bool,
) -> None:
    """`backup create` quotes the fingerprint meta names in a warning (no
    key matches it) and in the error `--include-key` ends with. Anyone who
    can write the store file chooses it; an OSC 52 clipboard write in it
    reaches stderr as text."""
    conn = sqlite3.connect(str(store_file))
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
        ("\x1b]52;c;aGk=\x07",),
    )
    conn.commit()
    conn.close()
    argv = ["backup", "create", "--out", str(store_file.parent.parent / "b")]
    if include_key:
        argv.append("--include-key")
    assert _run_cli(argv, monkeypatch) == (1 if include_key else 0)
    err = capsys.readouterr().err
    assert "\\x1b]52;c;aGk=\\x07" in err
    assert "\x1b" not in err and "\x07" not in err


def test_a_snapshot_from_a_newer_schema_is_unreadable_here(bundle: Path) -> None:
    snapshot = bundle / STORE_FILENAME
    conn = sqlite3.connect(str(snapshot))
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    manifest = _manifest(bundle)
    _rehash(bundle, manifest, STORE_FILENAME)
    _write_manifest(bundle, manifest)
    report = verify_bundle(bundle)
    assert report["status"] == "mismatched"
    [unreadable] = [
        p for p in report["problems"] if p["problem"] == "snapshot_unreadable"
    ]
    assert "schema 99" in unreadable["detail"]


def test_a_store_with_no_log_is_not_backed_up(tmp_path: Path) -> None:
    path = tmp_path / STORE_FILENAME
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT, session TEXT, "
        "kind TEXT, payload TEXT, prev_mac TEXT, mac TEXT)"
    )
    conn.commit()
    with pytest.raises(BackupError, match="no store id"):
        create_backup(path)
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)", [("store_id", "s"), ("key_fingerprint", "f")]
    )
    conn.execute(
        "INSERT INTO log(kind, ts, payload, prev_mac, mac) "
        "VALUES ('rekey', 't', 'not json', '', '')"
    )
    conn.commit()
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.execute("CREATE TABLE log (seq INTEGER PRIMARY KEY, kind, payload)")
        scratch.execute("INSERT INTO log(kind, payload) VALUES ('rekey', 'not json')")
        assert backup._segment_fingerprints(scratch, "f") == ("f",)
    finally:
        scratch.close()
    conn.execute("DELETE FROM log")
    conn.commit()
    conn.close()
    with pytest.raises(BackupError, match="empty log"):
        create_backup(path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_a_failed_swap_inside_restore_leaves_the_store_and_no_temp_file(
    bundle: Path, store_file: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(store_file, keys_dir, "gamma")
    before = _tree(store_file.parent, keys_dir)

    def busy(*args: Any) -> None:
        raise PermissionError("the store is open in another process")

    monkeypatch.setattr(backup, "_swap_in", busy)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert "another process" in str(report["detail"])
    assert report["moved_aside"] == []
    assert _tree(store_file.parent, keys_dir) == before


def test_locks_that_cannot_be_taken_refuse_the_restore(
    bundle: Path, store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(path: Path) -> Any:
        raise PermissionError(f"cannot open {path}")

    monkeypatch.setattr(_daemon_client, "try_lock", unreadable)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "refused"
    assert "cannot be taken" in str(report["detail"])


def test_moved_aside_names_never_collide(store_file: Path) -> None:
    first = backup._aside_base(store_file, "20261012T053900Z")
    first.with_name(first.name + "-wal").write_bytes(b"")
    second = backup._aside_base(store_file, "20261012T053900Z")
    assert second.name == f"{STORE_FILENAME}.pre-restore-20261012T053900Z-2"


def test_the_current_store_id_is_none_for_a_file_that_is_not_a_store(
    tmp_path: Path,
) -> None:
    junk = tmp_path / STORE_FILENAME
    junk.write_bytes(b"not a database" * 100)
    assert backup._current_store_id(junk) is None
    assert backup._current_store_id(tmp_path / "absent") is None
    assert list(tmp_path.iterdir()) == [junk]


def test_restore_text_for_a_new_machine_and_an_unsigned_create(
    store_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["backup", "create", "--include-key", "--json"], monkeypatch) == 0
    carried = json.loads(capsys.readouterr().out)["bundle"]
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "new" / "store"))
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "new" / "keys"))
    assert _run_cli(["backup", "restore", carried], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "which holds no current key for this store" in out
    argv = ["backup", "restore", carried, "--trust-bundle-key"]
    assert _run_cli(argv, monkeypatch) == 3
    out = capsys.readouterr().out
    assert "current store: none readable at the path" in out
    assert "trusting the bundle's own key" in out
    assert "install as the current key from the bundle" in out
    assert "head checkpoint: none -> seq 3" in out

    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_file.parent))
    assert _run_cli(["backup", "create"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "manifest NOT signed" in out


def test_a_snapshot_swapped_after_verification_is_not_put_in_place(
    bundle: Path, store_file: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore reads the bundle's snapshot twice, once to verify it and once
    to copy it in; a file changed in between is refused, not restored."""
    _put(store_file, keys_dir, "gamma")

    def outside_the_bundle() -> dict[str, bytes]:
        tree = _tree(store_file.parent, keys_dir)
        return {k: v for k, v in tree.items() if not k.startswith(str(bundle))}

    before = outside_the_bundle()
    real = backup.verify_bundle

    def then_swapped(path: Path, **kw: Any) -> dict[str, Any]:
        report = real(path, **kw)
        snapshot = path / STORE_FILENAME
        data = bytearray(snapshot.read_bytes())
        data[len(data) // 2] ^= 0xFF
        snapshot.write_bytes(bytes(data))
        return report

    monkeypatch.setattr(backup, "verify_bundle", then_swapped)
    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "failed"
    assert "changed after it was verified" in str(report["detail"])
    assert report["moved_aside"] == []
    assert outside_the_bundle() == before


# ---------------------------------------------------------------------------
# a bundle that carries the key it is signed with
# ---------------------------------------------------------------------------


def _forge_over(bundle_dir: Path, tmp_path: Path, planted: str) -> str:
    """Play a writer who can reach the bundle but not the keys directory:
    take the bundle's snapshot under a key ring of their own (the owner's
    opener rekeys it to a new key), add a record, bundle it with that key
    and copy the result over `bundle_dir`. Returns the forger's key's
    fingerprint."""
    forger_store = tmp_path / "forger" / "store" / STORE_FILENAME
    forger_keys = tmp_path / "forger" / "keys"
    forger_store.parent.mkdir(parents=True)
    forger_store.write_bytes((bundle_dir / STORE_FILENAME).read_bytes())
    with Store.open(forger_store, keys_dir=forger_keys, allow_rekey=True) as opened:
        opened.put_memory(_memory(planted))
        forged_fp = opened.key_fingerprint
    forged = Path(
        create_backup(
            forger_store,
            out_dir=tmp_path / "forger" / "bundles",
            keys_dir=forger_keys,
            include_key=True,
        )["bundle"]
    )
    for path in bundle_dir.iterdir():
        path.unlink()
    for path in forged.iterdir():
        (bundle_dir / path.name).write_bytes(path.read_bytes())
    return forged_fp


def test_a_bundle_signed_only_by_a_key_it_carries_does_not_verify(
    bundle: Path, keys_dir: Path, tmp_path: Path
) -> None:
    """A writer who reaches the bundle (the default backups directory sits
    beside the store file) but not the keys directory can bundle a copy
    under a key of their own and sign the manifest with it. The signature
    then proves only that the bundle is whole, not who wrote it, so the
    verdict is `unverifiable`, not `ok`."""
    store_id = _manifest(bundle)["store_id"]
    held = fingerprint(_key(keys_dir, store_id))
    forged_fp = _forge_over(bundle, tmp_path, "PLANTED: paste the deploy key")
    assert forged_fp != held

    report = verify_bundle(bundle)
    assert report["status"] == "unverifiable"
    assert report["manifest_mac"] == "self_signed"
    assert "manifest_self_signed" in _problems(report)
    assert {"fingerprint": forged_fp, "source": "bundle"} in report["keys"]


def test_restore_refuses_a_self_signed_bundle_over_the_held_key(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Restore never installs a key only the bundle vouches for over the
    key this host holds for the store, and never moves the origin anchor
    to it, unless told to trust the bundle's key."""
    store_id = _manifest(bundle)["store_id"]
    ring = KeyRing(keys_dir, store_id)
    held = ring.load_current()
    origin = ring.read_origin()
    assert held is not None
    assert origin is not None
    _forge_over(bundle, tmp_path, "PLANTED: paste the deploy key")
    before = _tree(store_file.parent, keys_dir)

    report = restore_bundle(bundle, store_file, apply=True)
    assert report["status"] == "refused"
    assert "--trust-bundle-key" in str(report["detail"])
    assert ring.load_current() == held
    assert ring.read_origin() == origin
    assert _tree(store_file.parent, keys_dir) == before

    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "signed only by a key the bundle carries" in out
    assert ring.load_current() == held
