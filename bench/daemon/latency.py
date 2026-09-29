"""The daemon's latency: the hook round trip and memory_search through the
stdio shim, on the owner's live store migrated into a scratch store.

Phase 1's P5 in numbers, each with its method:

  hook_wall          `bettermemory hook session-start` as the harness runs
                     it: one process per call, stdin `{"cwd": <repo>}`,
                     wall time from spawn to exit; N warm calls after
                     three warm-ups; p50 and p95 in ms.
  hook_service       the same endpoint called from a warm client in this
                     process (`_daemon_client.post`), so the daemon's own
                     answer time without the interpreter's start.
  hook_service_idle  hook_service after an idle gap: N_cold calls (default
                     10), each after sleeping a fixed COLD_IDLE_SECONDS (2.2
                     s) with nothing else sent to the daemon, the shape of a
                     session-start hook firing alone. The gap is fixed, not
                     tied to ORIGIN_CACHE_SECONDS: at 9.0.0's ten minutes the
                     origin capture outlives it and is reused, where under
                     the two-second lifetime the call paid the four probes.
                     Both numbers are written into the artifact.
  hook_service_cold  hook_service_idle with the capture made stale before
                     each call: after the same gap, the ctime of the
                     repository's HEAD is moved (its bytes and its mtime
                     left as they were), so the daemon's capture signature
                     no longer matches and the call pays the four origin
                     probes, the path every lone hook paid before 9.0.0.
  shim_search        memory_search through one stdio shim process (the SDK
                     client speaking stdio to `bettermemory`, which forwards
                     to the daemon); N queries, p50 and p95 per call, measured
                     on a second pass over the queries: three calls and one
                     full pass warm the daemon's caches first, since the
                     per-hit commit-drift memo holds a hit only once a search
                     has resolved it. The first pass is reported beside it
                     as shim_search_first_pass: what each query costs the
                     first time the daemon sees its hits, the origin, the
                     whole-history dates and the token streams already warm.
  in_process_search  the same N queries against `build_server(...)`'s
                     `call_tool` in this process, the floor the shim adds
                     one hop to; the same two passes, the first reported as
                     in_process_search_first_pass.
  cold_first_list    one measurement: with no daemon running, the time from
                     spawning the shim to its first `tools/list` answer
                     (the daemon's start is inside it).
  serve_first_search one measurement of the 8.x shape for contrast: spawn
                     `bettermemory serve` (the in-process stdio server) and
                     time initialize plus one memory_search.

The queries are the first N `asked` probes of bench/retrieval's public
questions (`bench/retrieval/questions.jsonl`), so anyone can re-run this on
their own store. The store is the sealed live copy migrated into a scratch
directory (`--v8`), keys and state under the same scratch directory; nothing
under the user's directories is read or written.

    python bench/daemon/latency.py --v8 ~/.cache/bettermemory-v9/live-copy \\
        --out bench/daemon/results/latency-9.0.0-2026-09-26.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "bench" / "retrieval" / "questions.jsonl"
PYTHON = sys.executable

# The idle gap `hook_service_idle` and `hook_service_cold` sleep before each
# call: a lone hook call after the daemon and the machine sat idle. Fixed,
# so the numbers measure the same thing whatever the origin cache's
# lifetime is.
COLD_IDLE_SECONDS = 2.2


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _summary(values_s: list[float]) -> dict[str, float]:
    ms = [v * 1000.0 for v in values_s]
    return {
        "n": len(ms),
        "p50_ms": round(statistics.median(ms), 2),
        "p95_ms": round(_pct(ms, 95), 2),
        "min_ms": round(min(ms), 2),
        "max_ms": round(max(ms), 2),
    }


def _env(scratch: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["BETTERMEMORY_DIR"] = str(scratch / "store")
    env["BETTERMEMORY_KEYS_DIR"] = str(scratch / "keys")
    env["BETTERMEMORY_STATE_DIR"] = str(scratch / "state")
    env["PYTHONPATH"] = str(ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def _cli(
    args: list[str],
    env: dict[str, str],
    *,
    stdin: str | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "bettermemory", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def migrate(v8: Path, scratch: Path, env: dict[str, str]) -> dict[str, Any]:
    target = scratch / "store" / "memory.sqlite"
    started = time.perf_counter()
    proc = _cli(
        ["migrate", "v8", "--from", str(v8), "--to", str(target), "--json"], env
    )
    if proc.returncode != 0:
        raise SystemExit(f"migration failed: {proc.stderr[-2000:]}")
    report = json.loads(proc.stdout)
    report["seconds"] = round(time.perf_counter() - started, 2)
    return report


def queries(n: int) -> list[str]:
    out: list[str] = []
    with QUESTIONS.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.append(str(row["question"]))
            if len(out) == n:
                break
    return out


async def _shim_session(env: dict[str, str], args: list[str]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=PYTHON, args=["-m", "bettermemory", *args], env=env, cwd=str(ROOT)
    )
    return params, ClientSession, stdio_client


async def cold_first_list(env: dict[str, str]) -> float:
    params, ClientSession, stdio_client = await _shim_session(env, [])
    started = time.perf_counter()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.list_tools()
            return time.perf_counter() - started


async def shim_search(
    env: dict[str, str], qs: list[str]
) -> tuple[list[float], list[float]]:
    """Two passes over `qs` after three warm-up calls: the first pass, in
    which the daemon's per-hit memo sees each query's hits for the first
    time, and the second, warm one. Returns (first, second)."""
    params, ClientSession, stdio_client = await _shim_session(env, [])
    passes: list[list[float]] = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.list_tools()
            for q in qs[:3]:
                await session.call_tool("memory_search", {"query": q, "max_results": 5})
            for _ in range(2):
                times: list[float] = []
                for q in qs:
                    started = time.perf_counter()
                    result = await session.call_tool(
                        "memory_search", {"query": q, "max_results": 5}
                    )
                    times.append(time.perf_counter() - started)
                    if result.is_error:
                        raise SystemExit(
                            f"memory_search errored through the shim: {result}"
                        )
                passes.append(times)
    return passes[0], passes[1]


async def serve_first_search(env: dict[str, str], q: str) -> float:
    params, ClientSession, stdio_client = await _shim_session(env, ["serve"])
    started = time.perf_counter()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("memory_search", {"query": q, "max_results": 5})
            return time.perf_counter() - started


async def in_process_search(
    scratch: Path, qs: list[str]
) -> tuple[list[float], list[float]]:
    """The same two passes as `shim_search`, in this process."""
    from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    os.environ["BETTERMEMORY_KEYS_DIR"] = str(scratch / "keys")
    config = Config(
        storage=StorageConfig(directory=str(scratch / "store")),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )
    server = build_server(
        config=config, store=Store(scratch / "store"), state=SessionState()
    )
    for q in qs[:3]:
        await server.call_tool("memory_search", {"query": q, "max_results": 5})
    passes: list[list[float]] = []
    for _ in range(2):
        times: list[float] = []
        for q in qs:
            started = time.perf_counter()
            await server.call_tool("memory_search", {"query": q, "max_results": 5})
            times.append(time.perf_counter() - started)
        passes.append(times)
    return passes[0], passes[1]


def hook_wall(env: dict[str, str], n: int, cwd: Path) -> list[float]:
    payload = json.dumps({"cwd": str(cwd)})
    for _ in range(3):
        _cli(["hook", "session-start"], env, stdin=payload)
    times: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        proc = _cli(["hook", "session-start"], env, stdin=payload)
        times.append(time.perf_counter() - started)
        if proc.returncode != 0:
            raise SystemExit(f"hook exited {proc.returncode}: {proc.stderr[-500:]}")
    return times


def hook_service(scratch: Path, n: int, cwd: Path) -> list[float]:
    from bettermemory._daemon_client import post, read_state

    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state after the hook calls")
    payload = {"cwd": str(cwd)}
    for _ in range(3):
        post(
            state["port"],
            state["token"],
            "/api/v1/hook/session-start",
            payload,
            timeout=30,
        )
    times: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        post(
            state["port"],
            state["token"],
            "/api/v1/hook/session-start",
            payload,
            timeout=30,
        )
        times.append(time.perf_counter() - started)
    return times


def _make_capture_stale(cwd: Path) -> None:
    """Move the ctime of the HEAD of `cwd`'s repository and nothing else:
    its bytes and its mtime stay as they were, so git reads the repository
    as before, while the daemon's capture signature, which stamps HEAD,
    no longer matches the cached capture of `cwd`."""
    from bettermemory import githead

    gd = githead.find_gitdir(cwd)
    if gd is None:
        raise SystemExit(f"hook_service_cold: no repository the files settle at {cwd}")
    head = gd.gitdir / "HEAD"
    status = os.stat(head, follow_symlinks=False)
    os.utime(head, ns=(status.st_atime_ns, status.st_mtime_ns), follow_symlinks=False)


def hook_service_lone(scratch: Path, n: int, cwd: Path, *, stale: bool) -> list[float]:
    """`hook_service` for a lone call: the same endpoint from the same warm
    client, each call after sleeping COLD_IDLE_SECONDS with nothing else
    sent to the daemon; with `stale`, the capture of `cwd` made stale
    before each call (`_make_capture_stale`)."""
    from bettermemory._daemon_client import post, read_state

    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state after the hook calls")
    payload = {"cwd": str(cwd)}
    times: list[float] = []
    for _ in range(n):
        time.sleep(COLD_IDLE_SECONDS)
        if stale:
            _make_capture_stale(cwd)
        started = time.perf_counter()
        post(
            state["port"],
            state["token"],
            "/api/v1/hook/session-start",
            payload,
            timeout=30,
        )
        times.append(time.perf_counter() - started)
    return times


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--v8",
        type=Path,
        required=True,
        help="the v8 store directory to migrate into the scratch store",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument(
        "--cold-n",
        type=int,
        default=10,
        help=(
            "calls in hook_service_idle and in hook_service_cold, each after "
            f"a fixed idle gap of {COLD_IDLE_SECONDS} s; the cold ones with the "
            "origin capture made stale first"
        ),
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=None,
        help="scratch directory (default: a fresh temp dir)",
    )
    args = parser.parse_args()

    scratch = args.scratch or Path(
        tempfile.mkdtemp(prefix="bettermemory-daemon-bench-")
    )
    scratch.mkdir(parents=True, exist_ok=True)
    env = _env(scratch)
    qs = queries(args.n)
    if len(qs) < args.n:
        raise SystemExit(f"only {len(qs)} questions in {QUESTIONS}")

    print(f"scratch {scratch}", file=sys.stderr)
    report = migrate(args.v8, scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)

    results: dict[str, Any] = {}
    results["cold_first_list"] = {
        "seconds": round(asyncio.run(cold_first_list(env)), 3)
    }
    print(
        f"cold first tools/list {results['cold_first_list']['seconds']} s",
        file=sys.stderr,
    )
    results["hook_wall"] = _summary(hook_wall(env, args.n, ROOT))
    print(f"hook wall {results['hook_wall']}", file=sys.stderr)
    results["hook_service"] = _summary(hook_service(scratch, args.n, ROOT))
    print(f"hook service {results['hook_service']}", file=sys.stderr)
    results["hook_service_idle"] = _summary(
        hook_service_lone(scratch, args.cold_n, ROOT, stale=False)
    )
    print(f"hook service idle {results['hook_service_idle']}", file=sys.stderr)
    results["hook_service_cold"] = _summary(
        hook_service_lone(scratch, args.cold_n, ROOT, stale=True)
    )
    print(f"hook service cold {results['hook_service_cold']}", file=sys.stderr)
    shim_first, shim_warm = asyncio.run(shim_search(env, qs))
    results["shim_search_first_pass"] = _summary(shim_first)
    results["shim_search"] = _summary(shim_warm)
    print(f"shim search {results['shim_search']}", file=sys.stderr)
    proc_first, proc_warm = asyncio.run(in_process_search(scratch, qs))
    results["in_process_search_first_pass"] = _summary(proc_first)
    results["in_process_search"] = _summary(proc_warm)
    print(f"in-process search {results['in_process_search']}", file=sys.stderr)
    _cli(["down"], env)
    results["serve_first_search"] = {
        "seconds": round(asyncio.run(serve_first_search(env, qs[0])), 3)
    }
    print(
        f"serve first search {results['serve_first_search']['seconds']} s",
        file=sys.stderr,
    )

    from bettermemory import __version__
    from bettermemory.origin import ORIGIN_CACHE_SECONDS

    artifact = {
        "kind": "daemon-latency",
        "version": __version__,
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "store": {
            "source": str(args.v8),
            "memories_imported": report.get("memories", {}),
            "events_imported": report.get("events", {}),
            "migration_seconds": report["seconds"],
        },
        "method": {
            "n": args.n,
            "queries": f"the first {args.n} `question` fields of {QUESTIONS.relative_to(ROOT)}",
            "hook_payload": {"cwd": str(ROOT)},
            "warm_ups": {
                "calls": 3,
                "passes": 1,
                "note": (
                    "the searches are measured on the second pass over the "
                    "queries; the first pass is reported as *_first_pass"
                ),
            },
            "hook_wall": "subprocess.run of `python -m bettermemory hook session-start`, wall time",
            "hook_service": "_daemon_client.post to /api/v1/hook/session-start from a warm client",
            "cold_n": args.cold_n,
            "cold_idle_seconds": COLD_IDLE_SECONDS,
            "origin_cache_seconds": ORIGIN_CACHE_SECONDS,
            "hook_service_idle": (
                "_daemon_client.post to /api/v1/hook/session-start from a warm client, "
                f"each of cold_n calls after a fixed idle gap of {COLD_IDLE_SECONDS} s "
                "with nothing else sent to the daemon: a lone session-start hook. "
                f"ORIGIN_CACHE_SECONDS is {ORIGIN_CACHE_SECONDS} s, so the call "
                + (
                    "reuses the origin capture"
                    if ORIGIN_CACHE_SECONDS > COLD_IDLE_SECONDS
                    else "pays the origin probes"
                )
            ),
            "hook_service_cold": (
                "hook_service_idle with the origin capture made stale before each "
                "call: after the gap, the ctime of the repository's HEAD is moved "
                "(its bytes and mtime unchanged), so the capture signature no "
                "longer matches and the call pays the four origin probes"
            ),
            "shim_search": (
                "one stdio shim process, SDK ClientSession.call_tool memory_search, "
                "the second pass over the queries"
            ),
            "shim_search_first_pass": (
                "the same shim session's first pass over the queries, each query's "
                "hits new to the daemon's per-hit memo"
            ),
            "in_process_search": (
                "build_server(...).call_tool memory_search in this process, the "
                "second pass over the queries"
            ),
            "in_process_search_first_pass": (
                "the same server's first pass over the queries"
            ),
        },
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
