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
store's rowid order is the v8 index's, whether the candidate query
agrees with the index on a rank-parity fixture's specs, what a second run
imports (nothing), the store's size, and `log verify`.

WHAT IS FIXED. The public fixture is `bench/parity/run.py`'s v8 store from
bench/retrieval's 1,080 documents (`build_store`), plus a small
deterministic set written by the v8 code itself: five tombstones (one
renamed to the pre-2.6.4 name), four episodes in two sessions (one a
floor), events across a rotated archive, a shard and the legacy file
(one with verbatim query text, one of the v8 `migrate` kind), one
conflict and one ingest-watermark entry. A private run takes any v8
directory with `--from` and a rank-parity fixture with `--fixture`; its
artifact carries file names, so it stays outside the checkout.

Usage:

    .venv/bin/python bench/parity/migrate_v8.py \\
        --out bench/parity/results/migrate-v8-8.0.0-2026-09-26.json
    .venv/bin/python bench/parity/migrate_v8.py --from DIR \\
        --fixture ~/.cache/bettermemory-v9/private-fixture-8.0.0.json \\
        --out ~/.cache/bettermemory-v9/results/migrate-v8-live.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory import index as _index  # noqa: E402
from bettermemory.conflicts import CONFLICTS_FILENAME, ConflictCandidate  # noqa: E402
from bettermemory.episodes import EPISODES_DIR, EpisodeStore  # noqa: E402
from bettermemory.eval import compute_report, render_report_markdown  # noqa: E402
from bettermemory.events import EVENT_LOG_FILENAME, Recorder, iter_all_events  # noqa: E402
from bettermemory.ingest import INGEST_WATERMARK_FILENAME  # noqa: E402
from bettermemory.migrate_v8 import inventory, migrate  # noqa: E402
from bettermemory.mirror import tombstone_filename, write_mirror  # noqa: E402
from bettermemory.sqlite_store import STORE_FILENAME, SqliteStore  # noqa: E402
from bettermemory.store import TOMBSTONE_DIR, Store, iter_active_memory_paths  # noqa: E402


def _load_parity() -> ModuleType:
    """The rank-parity runner, loaded by path under its own module name:
    several benches ship a `run.py`, and a bare import would return
    whichever of them a process loaded first."""
    name = "bench_parity_run"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, _HERE / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


parity = _load_parity()

PINNED_NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
FIXTURE_SESSION = "sess_fixture"
OTHER_SESSION = "sess_other"


# ---------------------------------------------------------------------------
# The public fixture
# ---------------------------------------------------------------------------


def build_public_v8(root: Path, corpus_path: Path = parity.CORPUS) -> dict[str, Any]:
    """The rank-parity v8 store plus tombstones, episodes, events and
    sidecars written by the v8 code, all deterministic but the event
    timestamps. Returns the counts written."""
    root = Path(root)
    ids = parity.build_store(root, corpus_path)
    ordered = [memory_id for _, memory_id in sorted(ids.items())]
    store = Store(root)
    tombstoned = ordered[:5]
    for n, memory_id in enumerate(tombstoned):
        path = store.tombstone(
            memory_id,
            f"removed in the public migration fixture, entry {n}",
            session_id=FIXTURE_SESSION,
        )
        if n == 0:
            legacy = path.with_name(
                path.name.replace(f".{memory_id}.tombstone.md", ".tombstone.md")
            )
            path.rename(legacy)

    episodes = EpisodeStore(root)
    for n, session_id in enumerate((FIXTURE_SESSION, FIXTURE_SESSION, OTHER_SESSION)):
        episodes.write(
            session_id=session_id,
            body=f"fixture episode {n}\n\nwhat the session concluded",
            takeaway=f"takeaway {n}",
            scopes=["projects:fixture"],
            now=PINNED_NOW + timedelta(seconds=n),
        )
    episodes.write_floor(
        session_id=OTHER_SESSION, now=PINNED_NOW + timedelta(seconds=9)
    )

    remaining = ordered[5:]
    recorder = Recorder(
        root=root, session_id=FIXTURE_SESSION, log_queries_verbatim=True, max_bytes=2000
    )
    for n in range(40):
        memory_id = remaining[n % len(remaining)]
        kind = ("search", "show", "use", "verify", "write")[n % 5]
        if kind == "search":
            recorder.record(
                "search",
                query=f"fixture query {n} about the corpus",
                returned=[memory_id],
                relevance=["high"],
            )
        elif kind == "show":
            recorder.record("show", id=memory_id)
        elif kind == "use":
            recorder.record("use", ids=[memory_id], outcome="applied")
        elif kind == "verify":
            recorder.record("verify", id=memory_id, note="fixture")
        else:
            recorder.record("write", id=memory_id, status="committed")
    recorder.record("migrate", action="origin", ids=[remaining[0]], updated=1)
    recorder.record(
        "turn_audited",
        verdict="ok",
        probe_query="fixture probe about the corpus",
        session_id=FIXTURE_SESSION,
    )
    (root / EVENT_LOG_FILENAME).write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00.000000Z",
                "session": "sess_legacy",
                "kind": "list",
                "scopes": None,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (root / CONFLICTS_FILENAME).write_text(
        json.dumps(
            ConflictCandidate(
                id="fixture-pair",
                a_id=remaining[0],
                b_id=remaining[1],
                summary_a="a",
                summary_b="b",
                similarity=0.9,
                method="jaccard",
                detector="polarity",
                created="2026-09-01T00:00:00Z",
            ).to_dict(),
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (root / INGEST_WATERMARK_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    str(root.parent / "fixture-source.md"): {
                        "content_hash": "sha256:fixture",
                        "memory_id": remaining[0],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "active": len(ordered) - len(tombstoned),
        "tombstones": len(tombstoned),
        "episodes": 4,
        "events": sum(1 for _ in iter_all_events(root)),
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
    path = _index.index_path(v8_root)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            str(r[0]) for r in conn.execute("SELECT id FROM memories ORDER BY rowid")
        ]
    finally:
        conn.close()


def _candidate_parity(
    v8_root: Path, store: SqliteStore, fixture: Path, *, limit: int | None
) -> dict[str, Any]:
    rows = json.loads(fixture.read_text(encoding="utf-8"))["rows"]
    seen: set[tuple[Any, ...]] = set()
    specs: list[dict[str, Any]] = []
    for row in rows:
        key = (
            row["query"],
            tuple(row["scopes"]) if row.get("scopes") else None,
            row.get("client"),
            row.get("model"),
        )
        if key in seen:
            continue
        seen.add(key)
        specs.append(row)
    if limit is not None:
        specs = specs[:limit]
    differing: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for spec in specs:
        scopes = list(spec["scopes"]) if spec.get("scopes") else None
        expected = _index.query(
            v8_root,
            spec["query"],
            scopes=scopes,
            client=spec.get("client"),
            model=spec.get("model"),
            max_results=parity.PREFILTER_CAP,
        )
        got = store.query_candidates(
            spec["query"],
            scopes=scopes,
            client=spec.get("client"),
            model=spec.get("model"),
            max_results=parity.PREFILTER_CAP,
        )
        digest.update(json.dumps([spec["key"], [i for i, _ in got]]).encode("utf-8"))
        if got != expected:
            differing.append(
                {
                    "key": spec["key"],
                    "v8": [i for i, _ in expected],
                    "v9": [i for i, _ in got],
                }
            )
    return {
        "fixture": fixture.name,
        "specs": len(specs),
        "differing": differing,
        "digest": digest.hexdigest(),
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_migration(
    scratch: Path,
    v8_root: Path,
    *,
    fixture: Path | None = None,
    spec_limit: int | None = None,
) -> dict[str, Any]:
    scratch = Path(scratch)
    v8_root = Path(v8_root).resolve()
    store_path = scratch / "v9" / STORE_FILENAME
    keys_dir = scratch / "keys"

    started = time.perf_counter()
    first = migrate(v8_root, store_path, keys_dir=keys_dir, now=PINNED_NOW)
    migrate_seconds = time.perf_counter() - started
    second = migrate(v8_root, store_path, keys_dir=keys_dir, now=PINNED_NOW)

    store = SqliteStore.open(store_path, keys_dir=keys_dir, allow_rekey=False)
    try:
        started = time.perf_counter()
        mirror_report = write_mirror(store, scratch / "mirror")
        mirror_seconds = time.perf_counter() - started
        compare = _compare_trees(v8_root, scratch / "mirror")

        v8_kinds = Counter(str(ev.get("kind")) for ev in iter_all_events(v8_root))
        v9_kinds = Counter(str(ev.get("kind")) for ev in store.iter_events())

        v8_store = Store(v8_root)
        before = compute_report(
            memories=v8_store.load_all(),
            events=iter_all_events(v8_root),
            now=PINNED_NOW,
            since=timedelta(days=30),
            tombstoned_ids={t.id for t in v8_store.load_tombstones()},
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
        order = {
            "index_present": index_order is not None,
            "equal": index_order == store.memory_ids() if index_order else None,
            "rows": len(index_order) if index_order else None,
        }

        candidates = (
            _candidate_parity(v8_root, store, fixture, limit=spec_limit)
            if fixture is not None
            else None
        )

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
        "provenance": parity._provenance(),
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
        "candidates": candidates,
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
    candidates = artifact["candidates"]
    cand = (
        f"; candidates {candidates['specs']} specs, {len(candidates['differing'])} differing"
        if candidates
        else ""
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
        f"{cand}; second run appended {artifact['second_run']['log_rows']} rows; "
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
        default=None,
        help="A v8 directory to migrate. Default: the public fixture, built in scratch.",
    )
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--spec-limit", type=int, default=None)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        v8_root = args.source
        if v8_root is None:
            v8_root = scratch / "v8"
            build_public_v8(v8_root)
        artifact = run_migration(
            scratch / "run", v8_root, fixture=args.fixture, spec_limit=args.spec_limit
        )
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
        and (artifact["candidates"] is None or not artifact["candidates"]["differing"])
    )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
