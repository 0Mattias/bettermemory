"""Where one call's time goes on a built store: one session start (a first
start), one prompt and one write with its review answered, each under
cProfile, with the CPU time beside the wall time (the rest is waiting,
mostly for the disk once the file outgrows the free memory), the event
reads each call made and how many log rows those reads walked, and the
query plans of the two event reads.

    python bench/scale/costs.py --scratch S/x100 --scale 100 \\
        --out bench/scale/results/scale-2026-11-04-x100-profile.json

Added after the timed arms: their results' `program_sha256` is of the
other five files, which this one leaves as they were. No warm-up call is
made, so each reading is one cold call; the store is the one `measure`
ran on.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import os
import pstats
import resource
import sys
import time
from datetime import timezone
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world  # noqa: E402

#: The two event reads the hooks make, as `store_events` spells them.
_EVENT_SQL = {
    "events_of_kinds": (
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        "WHERE kind IN ({}) AND substr(ts, 1, 19) >= ? ORDER BY seq"
    ),
    "session_events_of_kinds": (
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        "WHERE kind IN ({}) AND session = ? ORDER BY seq"
    ),
}


def _cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


class _Reads:
    """Every call of the two event reads: the kinds asked, the rows they
    gave back."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wrap(self, name: str, real: Callable[..., Iterator[Any]]) -> Callable[..., Any]:
        def counted(store: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
            kinds = sorted(args[1] if name == "session_events_of_kinds" else args[0])
            entry: dict[str, Any] = {"read": name, "kinds": kinds, "rows_returned": 0}
            since = kwargs.get("since")
            if since is not None:
                # The cut `events_of_kinds` compares, to the second.
                cut = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                entry["cut"] = cut
            self.calls.append(entry)
            for event in real(store, *args, **kwargs):
                entry["rows_returned"] += 1
                yield event

        return counted


def _top(prof: cProfile.Profile, *, limit: int) -> dict[str, Any]:
    stats = pstats.Stats(prof).stats  # type: ignore[attr-defined]
    rows = []
    for (path, line, func), (_cc, calls, tottime, cumtime, _callers) in stats.items():
        where = path.split("/src/", 1)[-1] if "/src/" in path else Path(path).name
        rows.append(
            {
                "function": f"{where}:{line}({func})",
                "calls": calls,
                "tottime_s": round(tottime, 3),
                "cumtime_s": round(cumtime, 3),
            }
        )
    ours = [r for r in rows if r["function"].startswith("bettermemory/")]
    return {
        "by_cumulative": sorted(ours, key=lambda r: -r["cumtime_s"])[:limit],
        "by_own_time": sorted(rows, key=lambda r: -r["tottime_s"])[:limit],
    }


def _profiled(call: Callable[[], Any], reads: _Reads) -> tuple[dict[str, Any], Any]:
    reads.calls.clear()
    prof = cProfile.Profile()
    cpu0, t0 = _cpu(), time.perf_counter()
    prof.enable()
    result = call()
    prof.disable()
    wall, cpu = time.perf_counter() - t0, _cpu() - cpu0
    return {
        "wall_s": round(wall, 2),
        "cpu_s": round(cpu, 2),
        "waiting_s": round(max(0.0, wall - cpu), 2),
        "event_reads": [dict(c) for c in reads.calls],
        "top": _top(prof, limit=20),
    }, result


def _walked(store: Any, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per distinct read: the calls, the rows they gave back, and the rows
    they walked to find them (every log row of their kinds, which the kind
    index hands the read to test one by one). For a windowed read, also the
    rows of every kind stamped inside its window, which `log_by_ts` would
    hand a read that compared the stamp itself (`ts >= cut`, the same rows
    as `substr(ts, 1, 19) >= cut` for the log's ISO stamps)."""
    out: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    of_kinds: dict[tuple[str, ...], int] = {}
    in_window: dict[str, int] = {}
    for call in calls:
        kinds = tuple(call["kinds"])
        if kinds not in of_kinds:
            marks = ",".join("?" * len(kinds))
            of_kinds[kinds] = store._conn.execute(
                f"SELECT count(*) FROM log WHERE kind IN ({marks})", kinds
            ).fetchone()[0]
        entry = out.setdefault(
            (call["read"], kinds),
            {
                "read": call["read"],
                "kinds": list(kinds),
                "calls": 0,
                "rows_returned": 0,
                "rows_walked": 0,
            },
        )
        entry["calls"] += 1
        entry["rows_returned"] += call["rows_returned"]
        entry["rows_walked"] += of_kinds[kinds]
        cut = call.get("cut")
        if cut is not None:
            if cut not in in_window:
                in_window[cut] = store._conn.execute(
                    "SELECT count(*) FROM log WHERE ts >= ?", (cut,)
                ).fetchone()[0]
            entry["rows_in_window_all_kinds"] = (
                entry.get("rows_in_window_all_kinds", 0) + in_window[cut]
            )
            entry.setdefault("cuts", []).append(cut)
    return list(out.values())


def _plans(store: Any) -> dict[str, Any]:
    out = {}
    for name, sql in _EVENT_SQL.items():
        text = sql.format("?,?")
        args = ("prompt_recall", "session_brief", "x")
        plan = [
            str(r[3]) for r in store._conn.execute("EXPLAIN QUERY PLAN " + text, args)
        ]
        out[name] = {"sql": text, "plan": plan}
    out["indexes"] = [
        str(r[0])
        for r in store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'log' "
            "AND sql IS NOT NULL ORDER BY name"
        )
    ]
    return out


def profile(scratch: Path, *, seed: int) -> dict[str, Any]:
    import arms
    import build
    from bettermemory import recall
    from bettermemory.builder import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    b = arms.Bench(scratch, seed)
    reads = _Reads()
    real = {name: getattr(Store, name) for name in _EVENT_SQL}
    for name in _EVENT_SQL:
        setattr(Store, name, reads.wrap(name, real[name]))
    out: dict[str, Any] = {}
    try:
        place = b.place()
        session = b.session()

        def start() -> Any:
            return recall.session_brief(
                session_id=session,
                source="startup",
                harness="claude-code",
                transcript_path=str(b.transcripts / f"{session}.jsonl"),
                cwd=place.path,
                config=b.config,
                store=b.store,
                state=b.recall_state,
            )

        out["session_start"], result = _profiled(start, reads)
        out["session_start"]["pointers"] = len(result.ids)
        out["session_start"]["walked"] = _walked(b.store, reads.calls)

        # The prompt is the started session's first: its recall state was
        # rebuilt by the start, as a daemon's shared state holds it.
        prompting = session
        t = build.Transcript(
            b.transcripts / f"{prompting}.jsonl", prompting, place.path
        )
        text = world.prompt(b.rng, place.words)
        t.user(text, "p0")

        def prompt() -> Any:
            return recall.prompt_recall(
                prompt=text,
                session_id=prompting,
                harness="claude-code",
                prompt_id="p0",
                transcript_path=str(t.path),
                cwd=place.path,
                config=b.config,
                store=b.store,
                state=b.recall_state,
            )

        out["prompt"], result = _profiled(prompt, reads)
        out["prompt"]["delivered"] = len(result.ids)
        out["prompt"]["walked"] = _walked(b.store, reads.calls)

        server = build_server(config=b.config, store=b.store, state=SessionState())
        os.chdir(place.path)
        arguments = {
            "content": world.body(b.rng, place.words),
            "scopes": [f"projects:{place.name}"]
            if place.kind != "folder"
            else ["tools"],
        }

        def write() -> Any:
            first = asyncio.run(build._call(server, "memory_write", arguments))
            if isinstance(first, dict) and first.get("status") == "review":
                return asyncio.run(build.reviewed_write(server, arguments))
            return (first.get("status") if isinstance(first, dict) else None), 1

        out["reviewed_write"], (status, more) = _profiled(write, reads)
        out["reviewed_write"]["status"] = status
        out["reviewed_write"]["calls"] = 1 + more
    finally:
        for name in _EVENT_SQL:
            setattr(Store, name, real[name])
    out["plans"] = _plans(b.store)
    out["store"] = {
        "log_rows": b.store._conn.execute("SELECT max(seq) FROM log").fetchone()[0],
        "memories": b.store._conn.execute("SELECT count(*) FROM memories").fetchone()[
            0
        ],
        "file_mb": round((scratch / "store" / "memory.sqlite").stat().st_size / 1e6, 1),
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--scale", type=float, required=True)
    parser.add_argument("--seed", type=int, default=109)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    world.scratch_env(args.scratch)
    minutes = float(os.environ.get("BENCH_SCALE_QUIET_MINUTES", 90))
    waited = world.wait_quiet(max_wait=minutes * 60)
    load_start = world.loads()
    with world.no_network() as attempts:
        out = profile(args.scratch.resolve(), seed=args.seed)
    out.update(
        {
            "scale": args.scale,
            "quiet": waited["quiet_at_start"],
            "quiet_wait": waited,
            "loads": {"load_start": load_start, "load_end": world.loads()},
            "available_mb": world.available_mb(),
            "tcp_connects": attempts,
        }
    )
    world.write_json(args.out, {**world.stamp(), "profile": out})
    calls = ("session_start", "prompt", "reviewed_write")
    print(
        json.dumps(
            {k: {"wall_s": out[k]["wall_s"], "cpu_s": out[k]["cpu_s"]} for k in calls}
        )
    )


if __name__ == "__main__":
    main()
