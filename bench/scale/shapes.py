"""The hooks' event reads in the shape they have and in the shapes a fix
would give them, timed back to back on one clone of a built store.

    python bench/scale/shapes.py --scratch S/x100 \\
        --profile bench/scale/results/scale-2026-11-04-x100-profile.json \\
        --out bench/scale/results/scale-2026-11-04-x100-shapes.json

The clone is a copy-on-write copy of the scratch store (`cp -c`, a plain
copy where the file system has none), opened with SQLite alone: no key
is read and nothing is signed, and the scratch store is left as it was.
On the clone it adds two plain indexes on columns, `log(session, kind)`
and `log(kind, ts)`, timing each and the pages it takes, then reads each
case `--reps` times, the shape it has now (held to the kind index it uses
today) and each new shape in turn, with the load averages beside every
reading, and checks that each new shape returns the same rows:

- a session's state rows (`Store.session_events_of_kinds`), for a new
  session and for the session with the most such rows;
- each window read the profiled prompt made (`Store.events_of_kinds`,
  its kinds and its cut from `--profile`), comparing the stamp as it does
  now (`substr(ts, 1, 19) >= cut`) and as `ts >= cut`, which keeps the
  same rows for the log's ISO stamps and lets an index range over it:
  through `log(kind, ts)`, and through the `log_by_ts` index every store
  already has;
- the contradiction read (`Store.contradicted_at`) over every `use` row,
  against the read of only the rows appended since the window's cut, as a
  reader that kept its answer and the last row it read would make.

Added after the timed arms; see `costs.py` for the program hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world  # noqa: E402

_COLUMNS = "seq, ts, session, kind, payload, prev_mac, mac"
_CONTRADICTED = (
    "SELECT ts, payload FROM log {using} WHERE kind = 'use' "
    "AND json_valid(payload) "
    "AND json_extract(payload, '$.outcome') = 'contradicted'{since} ORDER BY seq"
)
_NEW_INDEXES = {
    "log_by_session_kind": "CREATE INDEX log_by_session_kind ON log(session, kind)",
    "log_by_kind_ts": "CREATE INDEX log_by_kind_ts ON log(kind, ts)",
}


def _clone(scratch: Path) -> Path:
    source = scratch / "store" / "memory.sqlite"
    into = scratch / "shapes"
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    for suffix in ("", "-wal"):
        src = Path(str(source) + suffix)
        if not src.exists():
            continue
        dst = into / ("memory.sqlite" + suffix)
        done = subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True)
        if done.returncode != 0:
            shutil.copy2(src, dst)
    return into / "memory.sqlite"


def _timed(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, Any]:
    load_start = world.loads()
    t0 = time.perf_counter()
    rows = conn.execute(sql, args).fetchall()
    wall = time.perf_counter() - t0
    digest = hashlib.sha256(",".join(str(r[0]) for r in rows).encode()).hexdigest()
    return {
        "ms": round(wall * 1000, 2),
        "rows": len(rows),
        "rows_sha256": digest[:16],
        "load_start": load_start,
        "load_end": world.loads(),
    }


def _plan(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[str]:
    return [str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]


def _pages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA page_count").fetchone()[0])


def _cases(
    conn: sqlite3.Connection, profile: dict[str, Any], seed: int
) -> list[dict[str, Any]]:
    """Each case: its name, the read as it is, and the new shapes. The
    kinds and the cut are the profiled calls' own."""
    state = next(
        w["kinds"]
        for w in profile["session_start"]["walked"]
        if w["read"] == "session_events_of_kinds"
    )
    marks = ",".join("?" * len(state))
    busiest = conn.execute(
        f"SELECT session, count(*) FROM log WHERE kind IN ({marks}) "
        "AND session IS NOT NULL GROUP BY session ORDER BY count(*) DESC LIMIT 1",
        state,
    ).fetchone()
    fresh = str(uuid.UUID(int=seed, version=4))
    out = []
    for label, session in (("new session", fresh), ("busiest session", busiest[0])):
        where = f"WHERE kind IN ({marks}) AND session = ? ORDER BY seq"
        out.append(
            {
                "read": "session_events_of_kinds",
                "case": label,
                "args": (*state, session),
                "now": f"SELECT {_COLUMNS} FROM log INDEXED BY log_by_kind {where}",
                "shapes": {"log(session, kind)": f"SELECT {_COLUMNS} FROM log {where}"},
            }
        )
    cut = None
    for walked in profile["prompt"]["walked"]:
        if walked["read"] != "events_of_kinds":
            continue
        kinds = walked["kinds"]
        cut = walked["cuts"][0]
        marks = ",".join("?" * len(kinds))
        out.append(
            {
                "read": "events_of_kinds",
                "case": f"{len(kinds)} kinds, cut {cut}",
                "kinds": kinds,
                "args": (*kinds, cut),
                "now": (
                    f"SELECT {_COLUMNS} FROM log INDEXED BY log_by_kind WHERE kind "
                    f"IN ({marks}) AND substr(ts, 1, 19) >= ? ORDER BY seq"
                ),
                "shapes": {
                    "log(kind, ts)": (
                        f"SELECT {_COLUMNS} FROM log WHERE kind IN ({marks}) "
                        "AND ts >= ? ORDER BY seq"
                    ),
                    # For one kind the planner keeps `log_by_kind`, whose
                    # order is the ORDER BY's, over the range: named here.
                    "log(kind, ts), named": (
                        f"SELECT {_COLUMNS} FROM log INDEXED BY log_by_kind_ts "
                        f"WHERE kind IN ({marks}) AND ts >= ? ORDER BY seq"
                    ),
                    "log_by_ts, no new index": (
                        f"SELECT {_COLUMNS} FROM log INDEXED BY log_by_ts WHERE kind "
                        f"IN ({marks}) AND ts >= ? ORDER BY seq"
                    ),
                },
            }
        )
    if cut is not None:
        since = conn.execute(
            "SELECT min(seq) FROM log INDEXED BY log_by_ts WHERE ts >= ?", (cut,)
        ).fetchone()[0]
        out.append(
            {
                "read": "contradicted_at",
                "case": f"every use row, against the rows since seq {since}",
                "args": (),
                "now": _CONTRADICTED.format(using="INDEXED BY log_by_kind", since=""),
                "shapes": {
                    "rows since the last read": _CONTRADICTED.format(
                        using="INDEXED BY log_by_kind",
                        since=f" AND seq >= {int(since)}",
                    )
                },
                "same_rows_expected": False,
            }
        )
    return out


def shapes(
    scratch: Path, profile: dict[str, Any], *, reps: int, seed: int
) -> dict[str, Any]:
    path = _clone(scratch)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    log_rows = int(conn.execute("SELECT max(seq) FROM log").fetchone()[0])
    indexes = {}
    for name, sql in _NEW_INDEXES.items():
        before = _pages(conn)
        load_start = world.loads()
        t0 = time.perf_counter()
        conn.execute(sql)
        indexes[name] = {
            "sql": sql,
            "build_s": round(time.perf_counter() - t0, 2),
            "mb": round((_pages(conn) - before) * page_size / 1e6, 1),
            "load_start": load_start,
            "load_end": world.loads(),
        }
    t0 = time.perf_counter()
    conn.execute("PRAGMA optimize")
    optimize_s = round(time.perf_counter() - t0, 2)
    cases = _cases(conn, profile, seed)
    for case in cases:
        args = case["args"]
        case["plans"] = {"now": _plan(conn, case["now"], args)}
        for name, sql in case["shapes"].items():
            case["plans"][name] = _plan(conn, sql, args)
        case["readings"] = {"now": [], **{name: [] for name in case["shapes"]}}
        for _ in range(reps):
            case["readings"]["now"].append(_timed(conn, case["now"], args))
            for name, sql in case["shapes"].items():
                case["readings"][name].append(_timed(conn, sql, args))
        now = case["readings"]["now"]
        case["summary"] = {}
        for name in case["shapes"]:
            new = case["readings"][name]
            best_now = min(r["ms"] for r in now)
            best_new = min(r["ms"] for r in new)
            case["summary"][name] = {
                "now_ms": [r["ms"] for r in now],
                "new_ms": [r["ms"] for r in new],
                "times_faster": round(best_now / best_new, 1) if best_new else None,
                "same_rows": all(
                    a["rows_sha256"] == b["rows_sha256"] for a, b in zip(now, new)
                ),
            }
        del case["args"]
    conn.close()
    shutil.rmtree(path.parent, ignore_errors=True)
    return {
        "log_rows": log_rows,
        "page_size": page_size,
        "indexes": indexes,
        "optimize_s": optimize_s,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--scale", type=float, required=True)
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--seed", type=int, default=109)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    world.scratch_env(args.scratch)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))["profile"]
    with world.no_network() as attempts:
        out = shapes(args.scratch.resolve(), profile, reps=args.reps, seed=args.seed)
    out.update(
        {
            "scale": args.scale,
            "profile": args.profile.name,
            "available_mb": world.available_mb(),
            "tcp_connects": attempts,
        }
    )
    world.write_json(args.out, {**world.stamp(), "shapes": out})
    for case in out["cases"]:
        print(case["read"], case["case"], json.dumps(case["summary"]))


if __name__ == "__main__":
    main()
