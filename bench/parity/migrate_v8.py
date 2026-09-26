"""Migration parity: a v8 directory into the bettermemory 9 store, and
back out as a mirror.

WHY THIS EXISTS. `bettermemory migrate v8` (src/bettermemory/migrate_v8.py)
claims to carry a v8 directory into the store without loss, and
`bettermemory export --mirror` (src/bettermemory/mirror.py) to write it
back out byte for byte. This harness makes both claims a list of
differing files and puts the numbers beside them: what the migration
imported and how long it took, whether the per-kind event histogram is
the same on both sides, whether `eval.compute_report` renders the same
markdown over the v8 files and events as over the store, whether the
store's rowid order is the v8 index's, what a second run imports
(nothing), the store's size, and `log verify`.

WHAT IS FIXED. The public input is the golden v8 directory under
tests/fixtures/v8/store, written once at 8.0.0 by the v8 code itself
(tests/fixtures/v8/generate.py): three memories, two tombstones (one with
the pre-2.6.4 name), three episodes in two sessions (one a floor), events
across a rotated archive, an active shard and the legacy file (one with
verbatim query text, one of the v8 `migrate` kind), one conflict, an
ingest watermark with two sources, and the sidecars the migration drops
or leaves. A private run takes any v8 directory with `--from`; its
artifact carries file names, so it stays outside the checkout.

The rowid-order check runs only when the input holds an `.index.sqlite`,
opened read-only with sqlite3 directly. A v8 rebuild indexed the memories
in directory-listing order, so `order.equal` holds on a filesystem that
lists the input's names as the one that built the index did; the
committed artifact was produced on such a filesystem. `order.same_set`,
that the index and the store name the same ids, holds everywhere.

Usage:

    .venv/bin/python bench/parity/migrate_v8.py \\
        --out bench/parity/results/migrate-v8-8.0.0-2026-09-26.json
    .venv/bin/python bench/parity/migrate_v8.py --from DIR \\
        --out ~/.cache/bettermemory-v9/results/migrate-v8-live.json
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory.eval import compute_report, render_report_markdown  # noqa: E402
from bettermemory.migrate_v8 import inventory, migrate  # noqa: E402
from bettermemory.mirror import tombstone_filename, write_mirror  # noqa: E402
from bettermemory.store import STORE_FILENAME, Store  # noqa: E402
from bettermemory.v8 import (  # noqa: E402
    EPISODES_DIR,
    INDEX_FILENAME,
    TOMBSTONE_DIR,
    iter_active,
    iter_active_memory_paths,
    iter_all_events,
    iter_tombstones,
)

FIXTURE = _ROOT / "tests" / "fixtures" / "v8" / "store"
PINNED_NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def _provenance() -> dict[str, Any]:
    def git(*argv: str) -> str:
        try:
            return subprocess.run(
                ["git", *argv],
                capture_output=True,
                text=True,
                cwd=str(_ROOT),
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    import bettermemory

    return {
        "bettermemory_version": bettermemory.__version__,
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "tree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


def _compare_trees(v8_root: Path, mirror: Path) -> dict[str, Any]:
    """Every migrated source file against its mirror counterpart, byte
    for byte. Names of differing files are listed; a tombstone with the
    pre-2.6.4 name is compared under the modern one and listed as renamed."""
    inv = inventory(v8_root)
    active_files = 0
    active_identical = 0
    active_differing: list[str] = []
    for path in iter_active_memory_paths(v8_root):
        active_files += 1
        counterpart = mirror / path.name
        if counterpart.is_file() and counterpart.read_bytes() == path.read_bytes():
            active_identical += 1
        else:
            active_differing.append(path.name)
    tomb_identical = 0
    tomb_differing: list[str] = []
    renamed: list[str] = []
    for source in inv.tombstones:
        expected = tombstone_filename(source.filename, source.dead.id)
        if source.legacy_name:
            renamed.append(f"{source.path.name} -> {expected}")
        counterpart = mirror / TOMBSTONE_DIR / expected
        if (
            counterpart.is_file()
            and counterpart.read_bytes() == source.path.read_bytes()
        ):
            tomb_identical += 1
        else:
            tomb_differing.append(source.path.name)
    episode_files = 0
    episode_identical = 0
    episode_differing: list[str] = []
    episodes_dir = v8_root / EPISODES_DIR
    if episodes_dir.is_dir():
        for session_dir in sorted(episodes_dir.iterdir()):
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            for path in sorted(session_dir.iterdir()):
                if not path.is_file() or path.suffix != ".md":
                    continue
                episode_files += 1
                counterpart = mirror / EPISODES_DIR / session_dir.name / path.name
                if (
                    counterpart.is_file()
                    and counterpart.read_bytes() == path.read_bytes()
                ):
                    episode_identical += 1
                else:
                    episode_differing.append(f"{session_dir.name}/{path.name}")
    mirrored = sorted(
        str(p.relative_to(mirror)) for p in mirror.rglob("*.md") if p.is_file()
    )
    return {
        "active": {
            "files": active_files,
            "identical": active_identical,
            "differing": active_differing,
        },
        "tombstones": {
            "files": len(inv.tombstones),
            "identical": tomb_identical,
            "differing": tomb_differing,
            "renamed": renamed,
        },
        "episodes": {
            "files": episode_files,
            "identical": episode_identical,
            "differing": episode_differing,
        },
        "mirror_files": len(mirrored),
        "source_files": active_files + len(inv.tombstones) + episode_files,
    }


def _index_order(v8_root: Path) -> list[str] | None:
    """The memory ids in the v8 index's rowid order, or None when the
    directory holds no index. Opened immutable: nothing is created beside
    a checked-in fixture."""
    path = v8_root / INDEX_FILENAME
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return [
            str(r[0]) for r in conn.execute("SELECT id FROM memories ORDER BY rowid")
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_migration(scratch: Path, v8_root: Path) -> dict[str, Any]:
    scratch = Path(scratch)
    v8_root = Path(v8_root).resolve()
    store_path = scratch / "v9" / STORE_FILENAME
    keys_dir = scratch / "keys"

    started = time.perf_counter()
    first = migrate(v8_root, store_path, keys_dir=keys_dir, now=PINNED_NOW)
    migrate_seconds = time.perf_counter() - started
    second = migrate(v8_root, store_path, keys_dir=keys_dir, now=PINNED_NOW)

    store = Store.open(store_path, keys_dir=keys_dir, allow_rekey=False)
    try:
        started = time.perf_counter()
        mirror_report = write_mirror(store, scratch / "mirror")
        mirror_seconds = time.perf_counter() - started
        compare = _compare_trees(v8_root, scratch / "mirror")

        v8_kinds = Counter(str(ev.get("kind")) for ev in iter_all_events(v8_root))
        v9_kinds = Counter(str(ev.get("kind")) for ev in store.iter_events())

        before = compute_report(
            memories=sorted(
                (m for _, m in iter_active(v8_root)),
                key=lambda m: m.created,
                reverse=True,
            ),
            events=iter_all_events(v8_root),
            now=PINNED_NOW,
            since=timedelta(days=30),
            tombstoned_ids={t.id for _, t in iter_tombstones(v8_root)},
            version="parity",
        )
        after = compute_report(
            memories=sorted(
                store.iter_memories(), key=lambda m: m.created, reverse=True
            ),
            events=store.iter_events(),
            now=PINNED_NOW,
            since=timedelta(days=30),
            tombstoned_ids={t.id for t in store.iter_tombstones()},
            version="parity",
        )
        before_md = render_report_markdown(before)
        after_md = render_report_markdown(after)
        differing_lines = sum(
            1 for a, b in zip(before_md.splitlines(), after_md.splitlines()) if a != b
        ) + abs(len(before_md.splitlines()) - len(after_md.splitlines()))

        index_order = _index_order(v8_root)
        store_order = store.memory_ids()
        order = {
            "index_present": index_order is not None,
            "equal": index_order == store_order if index_order else None,
            "same_set": set(index_order) == set(store_order) if index_order else None,
            "rows": len(index_order) if index_order else None,
        }

        started = time.perf_counter()
        verify = store.log_verify()
        verify_seconds = time.perf_counter() - started
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_bytes = int(store.path.stat().st_size)
        page_size = int(store.conn.execute("PRAGMA page_size").fetchone()[0])
        status = store.status()
    finally:
        store.close()

    source_bytes = sum(
        p.stat().st_size
        for p in v8_root.rglob("*")
        if p.is_file() and not p.name.endswith(".lock")
    )
    return {
        "kind": "migrate-v8/parity",
        "provenance": _provenance(),
        "source": {
            "root": str(v8_root.relative_to(_ROOT))
            if v8_root.is_relative_to(_ROOT)
            else str(v8_root),
            "bytes": source_bytes,
        },
        "migrate": {**first.to_dict(), "seconds": round(migrate_seconds, 3)},
        "second_run": {
            "memories": second.memories["imported"],
            "tombstones": second.tombstones["imported"],
            "episodes": second.episodes["imported"],
            "events": second.events["imported"],
            "conflicts": second.conflicts["imported"],
            "imports": second.imports["imported"],
            "log_rows": second.log_rows,
        },
        "mirror": {
            **mirror_report.to_dict(),
            "seconds": round(mirror_seconds, 3),
            "compare": compare,
        },
        "events": {
            "v8_kinds": dict(sorted(v8_kinds.items())),
            "v9_kinds": dict(sorted(v9_kinds.items())),
            "equal": v8_kinds == v9_kinds,
        },
        "eval": {
            "markdown_equal": before_md == after_md,
            "alltime_equal": before.alltime_eval.to_dict()
            == after.alltime_eval.to_dict(),
            "window_equal": before.window_eval.to_dict() == after.window_eval.to_dict(),
            "differing_lines": differing_lines,
            "total_events": [before.total_events, after.total_events],
        },
        "order": order,
        "verify": {
            "status": verify["status"],
            "rows": verify["rows"],
            "seconds": round(verify_seconds, 3),
        },
        "size": {
            "sqlite_bytes": size_bytes,
            "page_size": page_size,
            "log_rows": status["log_rows"],
            "ratio_to_source_bytes": round(size_bytes / source_bytes, 3)
            if source_bytes
            else None,
        },
    }


def summary(artifact: dict[str, Any]) -> str:
    compare = artifact["mirror"]["compare"]
    differing = (
        len(compare["active"]["differing"])
        + len(compare["tombstones"]["differing"])
        + len(compare["episodes"]["differing"])
    )
    return (
        f"migrated {artifact['migrate']['memories']['imported']} memories, "
        f"{artifact['migrate']['tombstones']['imported']} tombstones, "
        f"{artifact['migrate']['episodes']['imported']} episodes, "
        f"{artifact['migrate']['events']['imported']} events in "
        f"{artifact['migrate']['seconds']} s; mirror {artifact['mirror']['seconds']} s, "
        f"{compare['source_files']} files compared, {differing} differing, "
        f"{len(compare['tombstones']['renamed'])} renamed; histogram equal "
        f"{artifact['events']['equal']}; eval markdown equal "
        f"{artifact['eval']['markdown_equal']}; order equal {artifact['order']['equal']}"
        f"; second run appended {artifact['second_run']['log_rows']} rows; "
        f"verify {artifact['verify']['status']} in {artifact['verify']['seconds']} s; "
        f"{artifact['size']['sqlite_bytes']} bytes at {artifact['size']['page_size']} "
        f"byte pages, {artifact['size']['ratio_to_source_bytes']}x the source bytes\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=FIXTURE,
        help=(
            "A v8 directory to migrate. Default: the golden fixture under "
            "tests/fixtures/v8/store."
        ),
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        artifact = run_migration(Path(raw) / "run", args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(summary(artifact))
    compare = artifact["mirror"]["compare"]
    clean = (
        not compare["active"]["differing"]
        and not compare["tombstones"]["differing"]
        and not compare["episodes"]["differing"]
        and artifact["events"]["equal"]
        and artifact["eval"]["markdown_equal"]
        and artifact["verify"]["status"] == "ok"
    )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
