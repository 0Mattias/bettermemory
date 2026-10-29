"""CAPS-P3: `capture undo --apply` leaves the export mirror as it was.

On each store: mirror it (`bettermemory export --mirror`), open a capture
batch, make CAPTURE_N tagged memory_write calls through the server into it,
close it settled, run `bettermemory capture undo <batch> --apply`, mirror
again into a fresh directory, and compare. Every file but the mirror marker
(`.mirror.json`, which carries written_at) and the batch's own tombstone
files must be byte-identical, with no file added or removed; then `log
verify` must be clean, and `tombstones restore` must bring each member back
with its body.

Two stores, each migrated into a scratch directory with
`latency.import_copy` (`migrate_v8`):

  fixture   tests/fixtures/v8/store, the v8 fixture the tests migrate.
  backup    the read-only backup ~/.cache/bettermemory-v9/live-backup-
            2026-10-11/memory.sqlite. It is a 9 store whose log is keyed by
            the live key, which the bench never reads, so it is copied with
            SQLite's backup API from an immutable read-only connection, the
            copy is mirrored to a v8 directory (`export --mirror --store`)
            under the scratch keys, and that directory is migrated into a
            fresh scratch store with its own key.

The workload is `latency.write_workload(CAPTURE_SEED, CAPTURE_N)`: made-up
nouns in durable sentences, distinct from each other by the dedup measure;
every write must commit. Only counts, statuses and shas are recorded; for
the backup no file name or body leaves the run. Every scratch directory is
removed at the end.

    python bench/capture/undo.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bench/: scratch_store
from scratch_store import scratch_store  # noqa: E402

CAPTURE_N = 10
CAPTURE_SEED = 20261013
FIXTURE = _common.ROOT / "tests" / "fixtures" / "v8" / "store"
BACKUP = Path(
    "~/.cache/bettermemory-v9/live-backup-2026-10-11/memory.sqlite"
).expanduser()


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _mirror(dest: Path, store_file: Path | None = None) -> None:
    args = ["export", "--mirror", str(dest)]
    if store_file is not None:
        args += ["--store", str(store_file)]
    proc = _common.cli(*args)
    if proc.returncode != 0:
        raise SystemExit(f"export --mirror failed: {proc.stderr[-400:]}")


def _copy_backup(dest_dir: Path) -> Path:
    """The backup's bytes into `dest_dir`/memory.sqlite, read through an
    immutable connection so nothing beside the backup is written."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "memory.sqlite"
    src = sqlite3.connect(f"file:{BACKUP}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


async def _capture(store_dir: Path, bodies: list[str]) -> tuple[str, list[str], dict]:
    world = _common.World(store_dir)
    batch = world.open()
    ids: list[str] = []
    statuses: dict[str, int] = {}
    for body in bodies:
        reply = await world.capture_write(batch, body, ["projects:capsbench"])
        status = str(reply.get("status"))
        statuses[status] = statuses.get(status, 0) + 1
        if status == "committed":
            ids.append(reply["id"])
    world.close(batch)
    world.store.close()
    return batch.batch, ids, statuses


def run_store(name: str, v8_root: Path, scratch: Path, bodies: list[str]) -> dict:
    from latency import import_copy  # type: ignore[import-not-found]

    from bettermemory.mirror import MIRROR_MARKER
    from bettermemory.mirror_format import TOMBSTONE_DIR

    store_dir = scratch / f"{name}-store"
    report = import_copy(v8_root, store_dir)
    # The CLI calls below resolve this store, not the scratch default.
    os.environ["BETTERMEMORY_DIR"] = str(store_dir)
    store_file = store_dir / "memory.sqlite"
    before_dir, after_dir = scratch / f"{name}-before", scratch / f"{name}-after"
    _mirror(before_dir, store_file)
    batch_id, ids, statuses = asyncio.run(_capture(store_dir, bodies))
    if len(ids) != CAPTURE_N:
        raise SystemExit(f"{name}: {statuses}, not {CAPTURE_N} committed writes")

    undo = _common.cli("capture", "undo", batch_id, "--apply", "--json")
    undo_doc = json.loads(undo.stdout) if undo.returncode == 0 else {}
    _mirror(after_dir, store_file)
    old, new = _files(before_dir), _files(after_dir)
    old.pop(MIRROR_MARKER, None)
    new.pop(MIRROR_MARKER, None)
    tombs = {
        k
        for k in new
        if k.startswith(TOMBSTONE_DIR + "/")
        and any(i in k for i in ids)
        and k not in old
    }
    rest = {k: v for k, v in new.items() if k not in tombs}
    differing = sum(old[k] != rest[k] for k in old.keys() & rest.keys())
    added = len(rest.keys() - old.keys())
    removed = len(old.keys() - rest.keys())

    verify = _common.cli("log", "verify", "--json")
    verify_doc = json.loads(verify.stdout) if verify.stdout.strip() else {}

    restored = 0
    for memory_id in ids:
        proc = _common.cli("tombstones", "restore", memory_id, "--json")
        restored += proc.returncode == 0
    store = scratch_store(store_dir)
    back = 0
    for memory_id, body in zip(ids, bodies, strict=True):
        try:
            back += store.load_one(memory_id).body.strip() == body.strip()
        except Exception:  # noqa: BLE001 - a member that did not come back
            continue
    after_restore = store.log_verify().get("status")
    store.close()

    byte_identical = differing == 0 and added == 0 and removed == 0
    verify_ok = verify.returncode == 0 and verify_doc.get("status") == "ok"
    return {
        "migration": {
            "memories": report.get("memories"),
            "seconds": report.get("seconds"),
        },
        "mirror_files_before": len(old),
        "capture_writes": statuses,
        "undo": {
            "exit": undo.returncode,
            "undone": len(undo_doc.get("undone") or undo_doc.get("to_undo") or []),
            "fields": sorted(undo_doc),
        },
        "tombstone_files_added": len(tombs),
        "files_differing": differing,
        "files_added": added,
        "files_removed": removed,
        "byte_identical_but_marker_and_tombstones": byte_identical,
        "log_verify": {"exit": verify.returncode, "status": verify_doc.get("status")},
        "restore": {
            "commands_ok": restored,
            "members_back_with_body": back,
            "log_verify_after": after_restore,
        },
        "verdict": "HIT" if byte_identical and verify_ok else "MISSED",
        "restore_verdict": "HIT" if back == CAPTURE_N else "MISSED",
    }


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="caps-undo-"))
    try:
        _common.scratch_env(scratch)
        from latency import file_sha256, write_workload  # type: ignore[import-not-found]

        bodies = write_workload(CAPTURE_SEED, CAPTURE_N)
        results: dict[str, Any] = {}
        results["fixture"] = run_store("fixture", FIXTURE, scratch, bodies)
        print(f"fixture {results['fixture']['verdict']}", file=sys.stderr)

        copy = _copy_backup(scratch / "backup-copy")
        v8_dir = scratch / "backup-v8"
        _mirror(v8_dir, copy)
        results["backup"] = run_store("backup", v8_dir, scratch, bodies)
        results["backup"]["source"] = {
            "path": "~/.cache/bettermemory-v9/live-backup-2026-10-11/memory.sqlite",
            "sha256": file_sha256(BACKUP),
            "route": "sqlite backup (immutable) -> export --mirror -> migrate_v8",
        }
        print(f"backup {results['backup']['verdict']}", file=sys.stderr)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    workload = hashlib.sha256("\n".join(bodies).encode("utf-8")).hexdigest()
    doc = {
        "kind": "capture-undo",
        "prediction": "CAPS-P3",
        "commit": _common.git("rev-parse", "HEAD"),
        "run_at": _common.stamp(),
        "method": {
            "writes": CAPTURE_N,
            "seed": CAPTURE_SEED,
            "workload_sha256": workload,
            "path": "open_batch, tagged memory_write through build_server's "
            "call_tool, close_batch settled, `capture undo --apply`",
            "compare": "export --mirror before and after, all files but "
            ".mirror.json and the batch's tombstone files",
        },
        "stores": results,
        "scratch_removed": not scratch.exists(),
    }
    out = _common.write_result("undo.json", doc)
    print(f"wrote {out}")
    for name, row in results.items():
        print(
            f"{name}: {row['verdict']} differing={row['files_differing']} "
            f"added={row['files_added']} removed={row['files_removed']} "
            f"tombs={row['tombstone_files_added']} verify={row['log_verify']} "
            f"restore={row['restore']} {row['restore_verdict']}"
        )


if __name__ == "__main__":
    main()
