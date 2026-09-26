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
  hook_service_cold  hook_service with the daemon's origin cache expired
                     before each call: N_cold calls (default 10), each after
                     sleeping ORIGIN_CACHE_SECONDS plus 0.2 s, so every call
                     pays the origin probes.
  shim_search        memory_search through one stdio shim process (the SDK
                     client speaking stdio to `bettermemory`, which forwards
                     to the daemon); N queries, p50 and p95 per call.
  in_process_search  the same N queries against `build_server(...)`'s
                     `call_tool` in this process, the floor the shim adds
                     one hop to.
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

# How far past the origin cache's lifetime `hook_service_cold` sleeps
# before each call.
COLD_MARGIN_SECONDS = 0.2


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


async def shim_search(env: dict[str, str], qs: list[str]) -> list[float]:
    params, ClientSession, stdio_client = await _shim_session(env, [])
    times: list[float] = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.list_tools()
            for q in qs[:3]:
                await session.call_tool("memory_search", {"query": q, "max_results": 5})
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
    return times


async def serve_first_search(env: dict[str, str], q: str) -> float:
    params, ClientSession, stdio_client = await _shim_session(env, ["serve"])
    started = time.perf_counter()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("memory_search", {"query": q, "max_results": 5})
            return time.perf_counter() - started


async def in_process_search(scratch: Path, qs: list[str]) -> list[float]:
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
    times: list[float] = []
    for q in qs:
        started = time.perf_counter()
        await server.call_tool("memory_search", {"query": q, "max_results": 5})
        times.append(time.perf_counter() - started)
    return times


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


def hook_service_cold(scratch: Path, n: int, cwd: Path) -> list[float]:
    """`hook_service` with the origin cache expired before every call: the
    same endpoint from the same warm client, each call after sleeping
    ORIGIN_CACHE_SECONDS plus COLD_MARGIN_SECONDS, so each call pays the
    probes `origin.capture` runs."""
    from bettermemory._daemon_client import post, read_state
    from bettermemory.origin import ORIGIN_CACHE_SECONDS

    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state after the hook calls")
    payload = {"cwd": str(cwd)}
    times: list[float] = []
    for _ in range(n):
        time.sleep(ORIGIN_CACHE_SECONDS + COLD_MARGIN_SECONDS)
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
        help="calls in hook_service_cold, each after the origin cache expires",
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
    results["hook_service_cold"] = _summary(
        hook_service_cold(scratch, args.cold_n, ROOT)
    )
    print(f"hook service cold {results['hook_service_cold']}", file=sys.stderr)
    results["shim_search"] = _summary(asyncio.run(shim_search(env, qs)))
    print(f"shim search {results['shim_search']}", file=sys.stderr)
    results["in_process_search"] = _summary(asyncio.run(in_process_search(scratch, qs)))
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
            "warm_ups": 3,
            "hook_wall": "subprocess.run of `python -m bettermemory hook session-start`, wall time",
            "hook_service": "_daemon_client.post to /api/v1/hook/session-start from a warm client",
            "cold_n": args.cold_n,
            "hook_service_cold": (
                "_daemon_client.post to /api/v1/hook/session-start from a warm client, "
                f"each of cold_n calls after sleeping ORIGIN_CACHE_SECONDS ({ORIGIN_CACHE_SECONDS} s) "
                f"plus {COLD_MARGIN_SECONDS} s, so the origin cache has expired and the call "
                "pays the origin probes"
            ),
            "shim_search": "one stdio shim process, SDK ClientSession.call_tool memory_search",
            "in_process_search": "build_server(...).call_tool memory_search in this process",
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
