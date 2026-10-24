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

The write arm (`--arm write`, unit D1's P10) is a separate run with its own
file:

  memory_write       a single memory_write through `build_server(...)`'s
                     `call_tool` in this process (the daemon's own answer
                     time, no HTTP hop), for each of WRITE_N workload bodies
                     after WRITE_WARM_UPS untimed writes; p50 and p95 in ms.
  reviewed_write     the same, for a write the daemon answers with a
                     review: the arm re-sends the same call with every
                     related id answered "unrelated", unsafe 0 and secret 0,
                     and the time covers both calls together. Recorded only
                     when a review was issued, so one arm gives the baseline
                     before the review exists and the review-era file after.

The workload is WRITE_N synthetic bodies from a fixed seed
(`write_workload`): made-up nouns in durable, factual sentences, each
distinct from the others by the dedup measure. Before any timed call every
body must pass the write gates on the scratch copy and overlap nothing
active or removed there (`check_workload`); a body that does not stops the
run. The store is imported from the sealed tarball (`--tgz`), whose
sha256 must equal the `.sha256` file beside it, into a scratch directory;
the arm refuses a scratch directory that is, or sits inside, the live
store (`~/.bettermemory`, `~/.claude-memory` or `$BETTERMEMORY_DIR`).

    python bench/daemon/latency.py --arm write \\
        --tgz ~/.cache/bettermemory-v9/live-copy-2026-09-25.tgz \\
        --out bench/daemon/results/latency-9.0.0-2026-09-30-write-baseline.json

The prompt arm (`--arm prompt`, unit RS's RS-P3) times the per-prompt
recall, each prompt PROMPT_GAP_SECONDS (3 s) after the last:

  prompt_service_spaced    `/api/v1/hook/prompt` from a warm client in this
                           process (`_daemon_client.post`), the daemon's own
                           answer time; N prompts.
  prompt_hook_wall_spaced  `bettermemory hook prompt` as the harness runs it:
                           one process per call, the payload on stdin, wall
                           time from spawn to exit; the same N prompts.

The prompts are a fixed workload (`prompt_workload`): every third one a
public question as written (under 200 characters), the others the public
questions joined in a seeded order and cut at a length drawn uniformly
from 200 to 2,000 characters, so the probe reads long prompts as well as
short ones. They are sent from the repository's root, in sessions of
PROMPT_SESSION_LEN prompts each, so the session cooldown sees a session's
later prompts as well as its first, after PROMPT_WARM_UPS untimed prompts
from a session of their own. The daemon is started with `up --port
0` on the scratch store, never on the configured port. The artifact
records the deliveries (prompts answered with at least one pointer), the
pointers, and the ids a session was shown twice (which the cooldown
forbids).

    python bench/daemon/latency.py --arm prompt \\
        --v8 ~/.cache/bettermemory-v9/live-copy \\
        --out bench/daemon/results/latency-9.0.0-2026-10-11-prompt.json

The brief arm (`--arm brief`, RS-F7 part 8) times the session brief as
rs-v6 builds it, every call from a fresh session (a session's second
brief omits what its first showed), on two daemon starts:

  brief_service_empty      `/api/v1/hook/session-start` from a warm client
                           with the transcript path of a session not yet
                           written, as a first start sends it: the probe is
                           empty and both lists rank by recency; N.
  brief_service_probed     the same with a synthetic transcript holding
                           BRIEF_TRANSCRIPT_PROMPTS prompts of the prompt
                           workload as user rows, so the probe reads a
                           session's worth of owner words and ranks the
                           lists; N.
  brief_hook_wall_probed   `bettermemory hook session-start` as the harness
                           runs it: one process per call, the probed
                           payload on stdin, wall time from spawn to exit;
                           N.

The first start (as_migrated) runs on the store as migrated, where only
a record of category ambient is standing (`standing.is_standing`), so the
brief is its WORK list and the ambient records. Then, with the daemon
down, standing is answered in the scratch store on the first
`recall.BRIEF_STANDING` WORK ids the first probed brief showed, the ids
after its in-force lines (probability BRIEF_STANDING_PROBABILITY, source
asked, `Store.put_standing`), and the second start (standing_answered)
times the brief with both lists filled. The
artifact records each pass's timings and the shape of its briefs (pointers
and characters per brief, the header's in-force and recent counts), never
an id or a line.

    python bench/daemon/latency.py --arm brief \
        --v8 ~/.cache/bettermemory-v9/live-copy \
        --out bench/daemon/results/latency-9.0.0rc7-2026-10-24-brief.json

The stop arm (`--arm stop`, unit CAPS's CAPS-P4) times the Stop endpoint
and the capture open decision (`run_stop_arm`):

  stop_service_capture_off/on  `/api/v1/hook/stop` from a warm client,
                               attended "1", one session over a transcript
                               below the checkpoint, on one daemon start
                               with `[capture] stop_checkpoint` off and the
                               next with it on; N each.
  open_decision                `capture_hook.on_stop` in this process over a
                               synthetic 5 MB transcript in Claude Code
                               2.1.295's row shape, a fresh session per
                               sample so each one opens a batch; N.
  open_endpoint_service        the same opening Stop through the daemon.

The config lives under a scratch HOME, and the scratch directory is removed
at the end.

    python bench/daemon/latency.py --arm stop --v8 tests/fixtures/v8/store \\
        --n 200 --out bench/daemon/results/latency-9.0.0-2026-10-13-stop.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
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

# The prompt arm: the gap between prompts, the workload's seed, the share
# of short prompts (every PROMPT_SHORT_EVERY-th is a question as written),
# the long prompts' length range, and the prompts per session.
PROMPT_GAP_SECONDS = 3.0
PROMPT_SEED = 20261011
PROMPT_SHORT_EVERY = 3
PROMPT_LONG_RANGE = (200, 2_000)
PROMPT_SESSION_LEN = 6
# Untimed prompts sent first, from their own session, as the other arms
# warm up with three calls.
PROMPT_WARM_UPS = 3


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


# ---------------------------------------------------------------------------
# The prompt arm (unit RS, RS-P3)
# ---------------------------------------------------------------------------


def prompt_workload(n: int, seed: int = PROMPT_SEED) -> list[str]:
    """`n` prompts: every PROMPT_SHORT_EVERY-th a public question as
    written, the rest the questions joined in a seeded order and cut at a
    length drawn uniformly from PROMPT_LONG_RANGE."""
    pool = queries(10_000)
    rng = random.Random(seed)
    out: list[str] = []
    for index in range(n):
        if index % PROMPT_SHORT_EVERY == 0:
            out.append(pool[rng.randrange(len(pool))])
            continue
        target = rng.randint(*PROMPT_LONG_RANGE)
        order = list(pool)
        rng.shuffle(order)
        text = ""
        for question in order:
            text = f"{text} {question}" if text else question
            if len(text) >= target:
                break
        out.append(text[:target])
    return out


def _prompt_session(index: int) -> str:
    return f"bench-prompt-{index // PROMPT_SESSION_LEN:03d}"


def prompt_service_spaced(
    scratch: Path, prompts: list[str], cwd: Path, *, gap: float = PROMPT_GAP_SECONDS
) -> tuple[list[float], list[dict[str, Any]]]:
    """Each prompt posted to /api/v1/hook/prompt from a warm client, `gap`
    seconds after the last answer. Returns the times and the replies."""
    from bettermemory._daemon_client import post, read_state

    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state for the prompt arm")
    for prompt in prompts[:PROMPT_WARM_UPS]:
        post(
            state["port"],
            state["token"],
            "/api/v1/hook/prompt",
            {"prompt": prompt, "session_id": "bench-prompt-warm-up", "cwd": str(cwd)},
            timeout=30,
        )
    times: list[float] = []
    replies: list[dict[str, Any]] = []
    for index, prompt in enumerate(prompts):
        time.sleep(gap)
        payload = {
            "prompt": prompt,
            "session_id": f"{_prompt_session(index)}-service",
            "cwd": str(cwd),
        }
        started = time.perf_counter()
        reply = post(
            state["port"], state["token"], "/api/v1/hook/prompt", payload, timeout=30
        )
        times.append(time.perf_counter() - started)
        replies.append(reply)
    return times, replies


def prompt_hook_wall_spaced(
    env: dict[str, str],
    prompts: list[str],
    cwd: Path,
    *,
    gap: float = PROMPT_GAP_SECONDS,
) -> tuple[list[float], list[dict[str, Any]]]:
    """`bettermemory hook prompt` once per prompt, the payload on stdin,
    `gap` seconds after the last exit. Returns the times and the replies
    (an empty one where the hook printed nothing)."""
    for prompt in prompts[:PROMPT_WARM_UPS]:
        _cli(
            ["hook", "prompt"],
            env,
            stdin=json.dumps(
                {
                    "prompt": prompt,
                    "session_id": "bench-prompt-warm-up",
                    "cwd": str(cwd),
                }
            ),
        )
    times: list[float] = []
    replies: list[dict[str, Any]] = []
    for index, prompt in enumerate(prompts):
        time.sleep(gap)
        payload = json.dumps(
            {
                "prompt": prompt,
                "session_id": f"{_prompt_session(index)}-wall",
                "cwd": str(cwd),
            }
        )
        started = time.perf_counter()
        proc = _cli(["hook", "prompt"], env, stdin=payload)
        times.append(time.perf_counter() - started)
        if proc.returncode != 0:
            raise SystemExit(f"hook exited {proc.returncode}: {proc.stderr[-500:]}")
        replies.append({"stdout": proc.stdout})
    return times, replies


def prompt_deliveries(
    prompts: list[str], replies: list[dict[str, Any]], suffix: str
) -> dict[str, Any]:
    """What the replies delivered: prompts answered with a pointer, the
    pointers, and the ids a session was shown more than once."""
    shown: dict[str, set[str]] = {}
    delivered = pointers = twice = 0
    for index, reply in enumerate(replies):
        ids = [str(i) for i in reply.get("ids") or []]
        if not ids:
            continue
        delivered += 1
        pointers += len(ids)
        seen = shown.setdefault(f"{_prompt_session(index)}-{suffix}", set())
        twice += sum(1 for i in ids if i in seen)
        seen.update(ids)
    return {
        "prompts": len(prompts),
        "deliveries": delivered,
        "pointers": pointers,
        "ids_shown_twice_in_a_session": twice,
    }


def _length_buckets(prompts: list[str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for prompt in prompts:
        size = len(prompt)
        counts[
            "under_200" if size < 200 else "200_999" if size < 1000 else "1000_2000"
        ] += 1
    return dict(counts)


def run_prompt_arm(*, v8: Path, scratch: Path, n: int) -> dict[str, Any]:
    refuse_live_store(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    env = _env(scratch)
    prompts = prompt_workload(n)
    report = migrate(v8, scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)
    up = _cli(["up", "--port", "0"], env)
    if up.returncode != 0:
        raise SystemExit(f"daemon did not start: {up.stderr[-500:]}")
    results: dict[str, Any] = {}
    try:
        service, service_replies = prompt_service_spaced(scratch, prompts, ROOT)
        results["prompt_service_spaced"] = _summary(service)
        print(f"prompt service {results['prompt_service_spaced']}", file=sys.stderr)
        wall, _ = prompt_hook_wall_spaced(env, prompts, ROOT)
        results["prompt_hook_wall_spaced"] = _summary(wall)
        print(f"prompt hook wall {results['prompt_hook_wall_spaced']}", file=sys.stderr)
    finally:
        _cli(["down"], env)

    from bettermemory import __version__

    return {
        "kind": "daemon-latency-prompt",
        "version": __version__,
        "commit": _git("rev-parse", "HEAD"),
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": _machine(),
        "store": {
            "source": str(v8),
            "memories_imported": report.get("memories", {}),
            "events_imported": report.get("events", {}),
            "migration_seconds": report["seconds"],
        },
        "method": {
            "n": n,
            "gap_seconds": PROMPT_GAP_SECONDS,
            "seed": PROMPT_SEED,
            "short_every": PROMPT_SHORT_EVERY,
            "long_range": list(PROMPT_LONG_RANGE),
            "session_len": PROMPT_SESSION_LEN,
            "warm_ups": PROMPT_WARM_UPS,
            "lengths": _length_buckets(prompts),
            "cwd": "the repository root",
            "prompt_service_spaced": (
                "_daemon_client.post to /api/v1/hook/prompt from a warm client, "
                "each prompt gap_seconds after the last answer"
            ),
            "prompt_hook_wall_spaced": (
                "subprocess.run of `python -m bettermemory hook prompt`, wall "
                "time, each prompt gap_seconds after the last exit"
            ),
        },
        "results": results,
        "deliveries": prompt_deliveries(prompts, service_replies, "service"),
    }


# ---------------------------------------------------------------------------
# The brief arm (RS-F7 part 8)
# ---------------------------------------------------------------------------

#: The SessionStart source the brief arm posts: a session's first start.
BRIEF_SOURCE = "startup"
#: Untimed briefs sent first, from sessions of their own, as the other
#: arms warm up with three calls.
BRIEF_WARM_UPS = 3
#: User rows per synthetic transcript: a session's worth of the prompt
#: workload (PROMPT_SESSION_LEN), about 6,600 characters, inside the
#: probe's `recall.BRIEF_PROBE_CHARS` so the probe reads every row.
BRIEF_TRANSCRIPT_PROMPTS = PROMPT_SESSION_LEN
#: The probability standing is answered with between the passes: the one
#: the brief's tests answer (tests/test_recall_brief.py), above
#: `standing.TAU_STANDING`.
BRIEF_STANDING_PROBABILITY = 0.9
BRIEF_TRANSCRIPT_VERSION = "2.1.295"


def brief_transcripts(tdir: Path, prompts: list[str], n: int) -> list[Path]:
    """`n` synthetic transcripts in Claude Code's row shape, the i-th
    holding prompts i to i + BRIEF_TRANSCRIPT_PROMPTS - 1 of `prompts` as
    user rows, an assistant row after each, so `transcript.owner_words`
    reads a session's worth of owner words from each."""
    paths: list[Path] = []
    for index in range(n):
        session = f"bench-brief-transcript-{index:03d}"
        path = tdir / f"{session}.jsonl"
        parent: str | None = None
        with path.open("w", encoding="utf-8") as fh:
            window = prompts[index : index + BRIEF_TRANSCRIPT_PROMPTS]
            for offset, prompt in enumerate(window):
                stamp = f"2026-10-24T00:{(offset * 2) % 60:02d}:00.000Z"
                rows: list[tuple[str, dict[str, Any]]] = [
                    ("user", {"role": "user", "content": prompt}),
                    (
                        "assistant",
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "noted"}],
                        },
                    ),
                ]
                for turn, (kind, message) in enumerate(rows):
                    uuid = f"{index:08x}-{offset:04x}-4000-8000-{turn:012d}"
                    row: dict[str, Any] = {
                        "parentUuid": parent,
                        "isSidechain": False,
                        "userType": "external",
                        "cwd": str(ROOT),
                        "sessionId": session,
                        "version": BRIEF_TRANSCRIPT_VERSION,
                        "type": kind,
                        "message": message,
                        "uuid": uuid,
                        "timestamp": stamp,
                    }
                    if kind == "user":
                        row["promptId"] = f"p{offset}"
                    fh.write(json.dumps(row) + "\n")
                    parent = uuid
        paths.append(path)
    return paths


def brief_payload(session: str, transcript: Path, cwd: Path) -> dict[str, Any]:
    """The SessionStart payload as the harness posts it for a first start."""
    return {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(cwd),
        "source": BRIEF_SOURCE,
        "harness": "claude-code",
        "hook_event_name": "SessionStart",
    }


def brief_service(
    scratch: Path, payloads: list[dict[str, Any]], warm_ups: list[dict[str, Any]]
) -> tuple[list[float], list[dict[str, Any]]]:
    """Each payload posted to /api/v1/hook/session-start from a warm client,
    after the warm-ups. Returns the times and the replies."""
    from bettermemory._daemon_client import post, read_state

    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state for the brief arm")
    for payload in warm_ups:
        post(
            state["port"],
            state["token"],
            "/api/v1/hook/session-start",
            payload,
            timeout=30,
        )
    times: list[float] = []
    replies: list[dict[str, Any]] = []
    for payload in payloads:
        started = time.perf_counter()
        reply = post(
            state["port"],
            state["token"],
            "/api/v1/hook/session-start",
            payload,
            timeout=30,
        )
        times.append(time.perf_counter() - started)
        replies.append(reply)
    return times, replies


def brief_hook_wall(
    env: dict[str, str], payloads: list[dict[str, Any]], warm_ups: list[dict[str, Any]]
) -> list[float]:
    """`bettermemory hook session-start` once per payload, the payload on
    stdin, after the warm-ups; wall time from spawn to exit."""
    for payload in warm_ups:
        _cli(["hook", "session-start"], env, stdin=json.dumps(payload))
    times: list[float] = []
    for payload in payloads:
        started = time.perf_counter()
        proc = _cli(["hook", "session-start"], env, stdin=json.dumps(payload))
        times.append(time.perf_counter() - started)
        if proc.returncode != 0:
            raise SystemExit(f"hook exited {proc.returncode}: {proc.stderr[-500:]}")
    return times


def brief_shape(replies: list[dict[str, Any]]) -> dict[str, Any]:
    """What the briefs showed, as counts: briefs, briefs with no pointer,
    pointers and characters per brief at the median, and the in-force and
    recent counts the first brief's header names (0 where the header
    names none)."""
    pointers = [len(reply.get("ids") or []) for reply in replies]
    chars = [int(reply.get("chars") or 0) for reply in replies]
    # The brief's header follows the membership lines in the hook's stdout.
    header = re.search(
        r"^bettermemory brief: .*$",
        str(replies[0].get("stdout") or "") if replies else "",
        re.MULTILINE,
    )
    head = header.group(0) if header else ""
    in_force = re.search(r"(\d+) in force", head)
    recent = re.search(r"(\d+) recent", head)
    return {
        "briefs": len(replies),
        "empty": sum(1 for count in pointers if count == 0),
        "pointers_p50": statistics.median(pointers) if pointers else 0,
        "chars_p50": statistics.median(chars) if chars else 0,
        "first_in_force": int(in_force.group(1)) if in_force else 0,
        "first_recent": int(recent.group(1)) if recent else 0,
    }


def answer_standing(scratch: Path, ids: list[str]) -> int:
    """Standing answered on `ids` in the scratch store, in this process
    while the daemon is down: BRIEF_STANDING_PROBABILITY, source asked,
    answered by this bench. Returns how many were answered."""
    from bettermemory.standing import SOURCE_ASKED
    from bettermemory.store import StandingAnswer, Store

    store = Store(scratch / "store", keys_dir=scratch / "keys")
    answer = StandingAnswer(
        BRIEF_STANDING_PROBABILITY, SOURCE_ASKED, "bench/daemon/latency.py"
    )
    try:
        for record_id in ids:
            store.put_standing(record_id, answer)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    return len(ids)


def _brief_pass(
    env: dict[str, str],
    scratch: Path,
    *,
    label: str,
    transcripts: list[Path],
    missing: list[Path],
    n: int,
) -> dict[str, Any]:
    """One daemon start: the service time of a brief whose transcript is not
    yet written (`missing`), then of one with a transcript, then the hook
    wall with a transcript; every call from a fresh session named for the
    pass and the measure. Returns the summaries, the briefs' shape and the
    ids the first probed brief showed, its in-force lines first."""

    def payloads(
        measure: str, paths: list[Path]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = [
            brief_payload(f"bench-brief-{label}-{measure}-{index:03d}", path, ROOT)
            for index, path in enumerate(paths[: n + BRIEF_WARM_UPS])
        ]
        return rows[:BRIEF_WARM_UPS], rows[BRIEF_WARM_UPS:]

    up = _cli(["up", "--port", "0"], env)
    if up.returncode != 0:
        raise SystemExit(f"daemon did not start: {up.stderr[-500:]}")
    results: dict[str, Any] = {}
    shape: dict[str, Any] = {}
    first_ids: list[str] = []
    try:
        warm, timed = payloads("empty", missing)
        times, replies = brief_service(scratch, timed, warm)
        results["brief_service_empty"] = _summary(times)
        shape["empty"] = brief_shape(replies)
        print(
            f"{label} brief service, empty {results['brief_service_empty']}",
            file=sys.stderr,
        )
        warm, timed = payloads("probed", transcripts)
        times, replies = brief_service(scratch, timed, warm)
        results["brief_service_probed"] = _summary(times)
        shape["probed"] = brief_shape(replies)
        first_ids = [str(i) for i in (replies[0].get("ids") or [])] if replies else []
        print(
            f"{label} brief service, probed {results['brief_service_probed']}",
            file=sys.stderr,
        )
        warm, timed = payloads("wall", transcripts)
        results["brief_hook_wall_probed"] = _summary(brief_hook_wall(env, timed, warm))
        print(
            f"{label} brief hook wall, probed {results['brief_hook_wall_probed']}",
            file=sys.stderr,
        )
    finally:
        _cli(["down"], env)
    return {"results": results, "briefs": shape, "first_ids": first_ids}


def run_brief_arm(*, v8: Path, scratch: Path, n: int) -> dict[str, Any]:
    from bettermemory import __version__
    from bettermemory.recall import BRIEF_PROBE_CHARS, BRIEF_STANDING, BRIEF_WORK

    refuse_live_store(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    # Resolved: the daemon keys its state file by the resolved store path.
    scratch = scratch.resolve()
    env = _env(scratch)
    report = migrate(v8, scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)
    calls = n + BRIEF_WARM_UPS
    prompts = prompt_workload(calls + BRIEF_TRANSCRIPT_PROMPTS - 1)
    tdir = scratch / "transcripts"
    tdir.mkdir()
    transcripts = brief_transcripts(tdir, prompts, calls)
    missing = [
        tdir / f"bench-brief-unwritten-{index:03d}.jsonl" for index in range(calls)
    ]

    passes: dict[str, Any] = {}
    first = _brief_pass(
        env,
        scratch,
        label="as_migrated",
        transcripts=transcripts,
        missing=missing,
        n=n,
    )
    passes["as_migrated"] = {"results": first["results"], "briefs": first["briefs"]}
    # The first probed brief's WORK ids follow its in-force lines.
    in_force = int(first["briefs"]["probed"]["first_in_force"])
    answered = answer_standing(
        scratch, first["first_ids"][in_force : in_force + BRIEF_STANDING]
    )
    print(f"standing answered on {answered} records", file=sys.stderr)
    second = _brief_pass(
        env,
        scratch,
        label="standing_answered",
        transcripts=transcripts,
        missing=missing,
        n=n,
    )
    passes["standing_answered"] = {
        "results": second["results"],
        "briefs": second["briefs"],
    }

    return {
        "kind": "daemon-latency-brief",
        "version": __version__,
        "commit": _git("rev-parse", "HEAD"),
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": _machine(),
        "store": {
            "source": str(v8),
            "memories_imported": report.get("memories", {}),
            "events_imported": report.get("events", {}),
            "migration_seconds": report["seconds"],
        },
        "method": {
            "n": n,
            "warm_ups": BRIEF_WARM_UPS,
            "source": BRIEF_SOURCE,
            "cwd": "the repository root",
            "transcript_prompts": BRIEF_TRANSCRIPT_PROMPTS,
            "probe_chars": BRIEF_PROBE_CHARS,
            "brief_standing": BRIEF_STANDING,
            "brief_work": BRIEF_WORK,
            "standing_answered": answered,
            "standing_probability": BRIEF_STANDING_PROBABILITY,
            "brief_service_empty": (
                "_daemon_client.post to /api/v1/hook/session-start from a warm "
                "client, the transcript path of a session not yet written, a "
                "fresh session per call"
            ),
            "brief_service_probed": (
                "the same with a synthetic transcript of transcript_prompts "
                "prompts of the prompt workload as user rows"
            ),
            "brief_hook_wall_probed": (
                "subprocess.run of `python -m bettermemory hook session-start`, "
                "the probed payload on stdin, wall time from spawn to exit"
            ),
            "passes": (
                "as_migrated on the store as migrated (ambient records standing by "
                "category, no answers); standing_answered after standing was "
                "answered, the daemon down, on the first brief_standing WORK ids "
                "the first probed brief of as_migrated showed"
            ),
        },
        "passes": passes,
    }


# ---------------------------------------------------------------------------
# The write arm (unit D1, P10)
# ---------------------------------------------------------------------------

#: The workload: its seed, the timed count and the untimed warm-ups before
#: it. Fixed, so the baseline, the cache run and the review-era run time
#: the same bodies; each file records the workload's sha256 beside them.
WRITE_SEED = 20260930
WRITE_N = 50
WRITE_WARM_UPS = 3
WRITE_SCOPES: tuple[str, ...] = ("bench:daemon-write",)
#: Calls one write may take: the first, the answer to its review, and one
#: follow-up review should the store move between the two. A write still
#: under review after that stops the run.
MAX_WRITE_CALLS = 3
#: The answer the arm gives every related id of a review, with unsafe and
#: secret 0: the cost of the round trip, not of any decision.
REVIEW_LABEL = "unrelated"
#: The live store's directory names under the home directory: the v9 store
#: and the v8 default (`config.GLOBAL_DIR_NAME`). `$BETTERMEMORY_DIR` is
#: read at call time.
LIVE_STORE_NAMES = (".bettermemory", ".claude-memory")

# The workload's made-up nouns are built from these syllables, so a body
# shares next to no content word with a real memory and names no project
# scope, and no word spells a transient marker or a secret's shape.
_ONSETS = (
    "b", "d", "f", "g", "k", "l", "m", "n", "p", "r", "s", "t", "v", "z",
    "br", "dr", "gr", "kl", "pl", "st", "tr", "vl",
)  # fmt: skip
_VOWELS = ("a", "e", "i", "o", "u")
_CODAS = ("", "", "", "n", "r", "l", "k", "s")

# Factual sentences in the register of a stored memory: no state, no dates,
# no numbers, no claim about a person, no path. Each sentence holds four
# made-up nouns and one real verb, and every other word is a stopword, so
# at most one content token in five can be shared with any stored body:
# a body stays under the dedup medium line (0.40) against the longest
# memory, whose vocabulary the containment score otherwise matches.
_TEMPLATES = (
    "{A} forwards {b} to {c} and {d}.",
    "{A} stores {b} in {c} by {d}.",
    "{A} drains {b} into {c} for {d}.",
    "{A} pins {b} to {c} of {d}.",
    "{A} retries {b} with {c} and then {d}.",
    "{A} reads {b} from {c} and {d}.",
    "{A} shares {b} with {c} and {d}.",
    "{A} exports {b} to {c} as {d}.",
    "{A} tags {b} with {c} from {d}.",
    "{A} skips {b} and {c} for {d}.",
    "{A} signs {b} for {c} with {d}.",
    "{A} rebuilds {b} from {c} and {d}.",
    "{A} listens to {b} on {c} by {d}.",
    "{A} writes {b} to {c} in {d}.",
    "{A} caches {b} in {c} for {d}.",
    "{A} depends on {b}, {c} and {d}.",
    "{A} compresses {b} with {c} for {d}.",
    "{A} routes {b} from {c} to {d}.",
    "{A} validates {b} with {c} and {d}.",
    "{A} batches {b} into {c} for {d}.",
    "{A} mirrors {b} to {c} and {d}.",
    "{A} hashes {b} with {c} and {d}.",
    "{A} spawns {b} for {c} and {d}.",
    "{A} serves {b} from {c} to {d}.",
)
_SENTENCES_PER_BODY = 6

#: `call(name, arguments)` -> the tool's JSON object.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _noun(rng: random.Random, used: set[str]) -> str:
    while True:
        word = "".join(
            rng.choice(_ONSETS) + rng.choice(_VOWELS) + rng.choice(_CODAS)
            for _ in range(rng.choice((2, 3)))
        )
        if len(word) >= 5 and word not in used:
            used.add(word)
            return word


def _candidate(rng: random.Random, used: set[str]) -> str:
    sentences: list[str] = []
    for template in rng.sample(_TEMPLATES, _SENTENCES_PER_BODY):
        sentences.append(
            template.format(
                A=_noun(rng, used).capitalize(),
                b=_noun(rng, used),
                c=_noun(rng, used),
                d=_noun(rng, used),
            )
        )
    return " ".join(sentences)


def _as_memory(body: str) -> Any:
    from bettermemory.models import Confidence, Memory, Source, generate_ulid

    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=when,
        updated=when,
        scopes=list(WRITE_SCOPES),
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def write_workload(seed: int = WRITE_SEED, count: int = WRITE_N) -> list[str]:
    """`count` bodies from `seed`, each distinct from the others by the
    dedup measure (`search.find_similar` returns nothing against the ones
    before it). Sequential, so `write_workload(seed, n)` is a prefix of
    `write_workload(seed, n + k)`, and a function of the seed alone: no
    store is read. Whether the bodies are new to a store is
    `check_workload`'s question."""
    from bettermemory.search import find_similar

    rng = random.Random(seed)
    used: set[str] = set()
    bodies: list[str] = []
    accepted: list[Any] = []
    while len(bodies) < count:
        body = _candidate(rng, used)
        if find_similar(body, accepted):
            continue
        bodies.append(body)
        accepted.append(_as_memory(body))
    return bodies


def workload_sha256(bodies: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(bodies, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_arguments(body: str) -> dict[str, Any]:
    return {"content": body, "scopes": list(WRITE_SCOPES)}


def check_workload(store: Any, config: Any, bodies: list[str]) -> dict[str, Any]:
    """Every body passes the write gates on `store` and overlaps nothing
    active or removed there, nor any other body, before a timed call.

    The gates are `handlers.write._WRITE_GATES`, run through
    `apply_write_gates` with every acknowledge flag off and `force` off,
    as the timed call will run them. A decision of status "review" counts
    as passing, because the review sits after the gates. Overlap is the
    dedup measure at its medium line (`find_similar` and
    `find_similar_tombstones` return nothing), stricter than the gates'
    high line, so a body is new and not merely not a duplicate. Raises
    SystemExit naming each failing body by index and status only."""
    from bettermemory.handlers._shared import _validate_write_payload
    from bettermemory.handlers.write import (
        _WRITE_GATES,
        GateBundle,
        GateContext,
        apply_write_gates,
    )
    from bettermemory.search import find_similar, find_similar_tombstones

    deps = GateBundle.for_store(store, config)
    active = store.load_all()
    removed = store.load_tombstones()
    known = {f.name for f in dataclasses.fields(GateContext)}
    flags: dict[str, Any] = {
        "force": False,
        "acknowledge_transient": False,
        "acknowledge_scope_mismatch": False,
        "acknowledge_credential": False,
        "acknowledge_user_claim": False,
    }
    behavior = config.behavior
    problems: list[str] = []
    counts = Counter[str]()
    for index, body in enumerate(bodies):
        payload = _validate_write_payload(
            content=body,
            scopes=list(WRITE_SCOPES),
            confidence="medium",
            source="explicit-statement",
            allowed_scopes=config.scopes.allowed,
            category="fact",
            max_content_bytes=behavior.max_content_bytes,
            min_content_tokens=behavior.min_content_tokens,
            max_scopes_per_write=behavior.max_scopes_per_write,
        )
        gc = GateContext(
            payload=payload, **{k: v for k, v in flags.items() if k in known}
        )
        decision = apply_write_gates(deps, gc)
        status = None if decision is None else decision.response.get("status")
        if decision is not None and status != "review":
            counts["refused"] += 1
            problems.append(f"body {index} refused as {status}")
        if find_similar(body, active):
            counts["overlapping_active"] += 1
            problems.append(f"body {index} overlaps an active memory")
        if find_similar_tombstones(body, removed):
            counts["overlapping_removed"] += 1
            problems.append(f"body {index} overlaps a removed memory")
        others = [_as_memory(b) for j, b in enumerate(bodies) if j != index]
        if find_similar(body, others):
            counts["overlapping_each_other"] += 1
            problems.append(f"body {index} overlaps another workload body")
    if problems:
        raise SystemExit(
            "the write workload is not gate-clean on this store: " + "; ".join(problems)
        )
    return {
        "gates": [type(gate).__name__ for gate in _WRITE_GATES],
        "bodies": len(bodies),
        "active_in_store": len(active),
        "removed_in_store": len(removed),
        "refused": counts["refused"],
        "overlapping_active": counts["overlapping_active"],
        "overlapping_removed": counts["overlapping_removed"],
        "overlapping_each_other": counts["overlapping_each_other"],
    }


def review_answer(response: dict[str, Any]) -> dict[str, Any]:
    """The arm's answer to a review response: every related id
    "unrelated", unsafe 0, secret 0, standing 0, and the review_id when
    one was given."""
    review = response.get("review") or {}
    related = review.get("related") or []
    ids = [entry["id"] if isinstance(entry, dict) else entry for entry in related]
    answer: dict[str, Any] = {
        "relation": {str(i): REVIEW_LABEL for i in ids},
        "unsafe": 0,
        "secret": 0,
        "standing": 0,
    }
    if review.get("review_id"):
        answer["review_id"] = review["review_id"]
    return answer


async def time_writes(
    call: ToolCall,
    bodies: list[str],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """One memory_write per body, timed from the first call to the
    decision. A write answered with status "review" is re-sent as the
    same call plus `review_answer`, and its time covers every call; it
    lands in `reviewed_write`, a write decided in one call in
    `memory_write`. Every write must end committed."""
    single: list[float] = []
    reviewed: list[float] = []
    statuses = Counter[str]()
    calls = Counter[int]()
    for index, body in enumerate(bodies):
        arguments = _write_arguments(body)
        started = clock()
        response = await call("memory_write", arguments)
        made = 1
        while response.get("status") == "review":
            if made >= MAX_WRITE_CALLS:
                raise SystemExit(f"write {index} still under review after {made} calls")
            response = await call(
                "memory_write", {**arguments, "review": review_answer(response)}
            )
            made += 1
        elapsed = clock() - started
        status = response.get("status")
        if status != "committed":
            raise SystemExit(f"write {index} ended {status!r}, not committed")
        (reviewed if made > 1 else single).append(elapsed)
        statuses[str(status)] += 1
        calls[made] += 1
    return {
        "memory_write": single,
        "reviewed_write": reviewed,
        "statuses": dict(statuses),
        "calls_per_write": {str(k): v for k, v in sorted(calls.items())},
    }


def server_call(server: Any) -> ToolCall:
    """`ToolCall` over `build_server(...)`'s `call_tool`, in this process."""

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await server.call_tool(name, arguments)
        if getattr(result, "is_error", False):
            raise SystemExit(f"{name} returned an error in process")
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None)
        if content and hasattr(content[0], "text"):
            parsed = json.loads(content[0].text)
            if isinstance(parsed, dict):
                return parsed
        raise SystemExit(f"{name} returned no JSON object")

    return call


def live_store_dirs() -> list[Path]:
    """The directories the arm never writes under: the live store in the
    home directory, under both names, and `$BETTERMEMORY_DIR` when set."""
    from bettermemory.config import ENV_DIR_OVERRIDE

    dirs = [Path.home() / name for name in LIVE_STORE_NAMES]
    override = os.environ.get(ENV_DIR_OVERRIDE)
    if override:
        dirs.append(Path(override).expanduser())
    return dirs


def refuse_live_store(path: Path) -> None:
    """SystemExit when `path` is a live store directory or inside one."""
    target = path.expanduser().resolve()
    for live in live_store_dirs():
        resolved = live.resolve()
        if target == resolved or target.is_relative_to(resolved):
            raise SystemExit(
                f"refusing {path}: the write arm runs on a scratch copy only, "
                f"and this is the live store at {live} or inside it"
            )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_tgz(tgz: Path) -> str:
    """The tarball's sha256, after checking it against the `.sha256` file
    beside it; SystemExit when that file is missing or disagrees."""
    sidecar = tgz.with_name(tgz.name + ".sha256")
    if not sidecar.is_file():
        raise SystemExit(f"no {sidecar.name} beside {tgz}")
    fields = sidecar.read_bytes().decode("utf-8").split()
    recorded = fields[0].lower() if fields else ""
    digest = file_sha256(tgz)
    if recorded != digest:
        raise SystemExit(
            f"{tgz.name} hashes to {digest}, not the {recorded or 'nothing'} "
            f"its {sidecar.name} records"
        )
    return digest


def extract_copy(tgz: Path, dest: Path) -> Path:
    """Unpack `tgz` under `dest` with the data filter and return the v8
    directory: the archive's single top directory, or `dest` itself.

    AppleDouble members (a basename starting "._") are skipped. macOS tar
    writes one beside every entry to carry extended attributes; they are
    no part of the store, and the sealed copy holds one per file. Its
    unpacked twin at ~/.cache/bettermemory-v9/live-copy, which the read
    arm's runs migrated, has none, so skipping them imports what those
    runs imported."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if not Path(member.name).name.startswith("._")
        ]
        archive.extractall(dest, members=members, filter="data")
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def import_copy(v8_root: Path, store_dir: Path) -> dict[str, Any]:
    """`migrate_v8.migrate` in this process, into `store_dir`."""
    from bettermemory.migrate_v8 import migrate as migrate_v8
    from bettermemory.store import STORE_FILENAME

    store_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = migrate_v8(v8_root, store_dir / STORE_FILENAME).to_dict()
    report["seconds"] = round(time.perf_counter() - started, 2)
    return report


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


async def _write_pass(
    call: ToolCall,
    warm: list[str],
    timed: list[str],
    clock: Callable[[], float],
) -> dict[str, Any]:
    await time_writes(call, warm, clock=clock)
    return await time_writes(call, timed, clock=clock)


def run_write_arm(
    *,
    tgz: Path,
    scratch: Path,
    n: int = WRITE_N,
    seed: int = WRITE_SEED,
    warm_ups: int = WRITE_WARM_UPS,
    note: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Import `tgz` into a scratch store under `scratch`, check the
    workload there, write `warm_ups` bodies untimed and time `n`, and
    return the artifact. Refuses a live store before any file is read."""
    from bettermemory import __version__
    from bettermemory.config import (
        KEYS_DIR_ENV,
        BehaviorConfig,
        Config,
        ScopesConfig,
        StorageConfig,
    )
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    refuse_live_store(scratch)
    store_dir = scratch / "store"
    refuse_live_store(store_dir)
    digest = verify_tgz(tgz)
    bodies = write_workload(seed, n + warm_ups)
    timed, warm = bodies[:n], bodies[n:]

    previous_keys = os.environ.get(KEYS_DIR_ENV)
    os.environ[KEYS_DIR_ENV] = str(scratch / "keys")
    store = None
    try:
        v8_root = extract_copy(tgz, scratch / "copy")
        report = import_copy(v8_root, store_dir)
        if not report.get("memories", {}).get("found"):
            raise SystemExit(f"the copy in {tgz.name} imported no memories")
        config = Config(
            storage=StorageConfig(directory=str(store_dir)),
            behavior=BehaviorConfig(),
            scopes=ScopesConfig(),
        )
        store = Store(store_dir)
        server = build_server(config=config, store=store, state=SessionState())
        gate_check = check_workload(store, config, bodies)
        timings = asyncio.run(_write_pass(server_call(server), warm, timed, clock))
    finally:
        if store is not None:
            store.close()
        if previous_keys is None:
            os.environ.pop(KEYS_DIR_ENV, None)
        else:
            os.environ[KEYS_DIR_ENV] = previous_keys

    results: dict[str, Any] = {}
    if timings["memory_write"]:
        results["memory_write"] = _summary(timings["memory_write"])
    if timings["reviewed_write"]:
        results["reviewed_write"] = _summary(timings["reviewed_write"])
    lengths = [len(body) for body in timed]
    commit = _git("rev-parse", "HEAD")
    return {
        "kind": "daemon-latency-write",
        "version": __version__,
        "commit": commit,
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": _machine(),
        "provenance": {
            "commit": commit,
            "src_tree": _git("rev-parse", "HEAD:src"),
            "src_clean": _git("status", "--porcelain", "--", "src") == "",
            "note": note,
        },
        "store": {
            "source": str(tgz),
            "tgz_sha256": digest,
            "memories_imported": report.get("memories", {}),
            "tombstones_imported": report.get("tombstones", {}),
            "events_imported": report.get("events", {}),
            "import_seconds": report["seconds"],
        },
        "workload": {
            "seed": seed,
            "count": n,
            "warm_ups": warm_ups,
            "sha256": workload_sha256(timed),
            "generator": "write_workload in bench/daemon/latency.py",
            "arguments": {"scopes": list(WRITE_SCOPES)},
            "chars": {
                "min": min(lengths) if lengths else 0,
                "p50": statistics.median(lengths) if lengths else 0,
                "max": max(lengths) if lengths else 0,
            },
        },
        "gate_check": gate_check,
        "method": {
            "memory_write": (
                "build_server(...).call_tool memory_write in this process, from "
                "the first call to the decision, one call per write; each body "
                "commits, so the store grows by one per write"
            ),
            "reviewed_write": (
                "the same for a write answered with status review: the same "
                "call re-sent with review {review_id, relation: every related "
                f"id {REVIEW_LABEL!r}, unsafe 0, secret 0, standing 0}}, both "
                "calls timed "
                "together; absent when no review was issued"
            ),
            "warm_ups": (
                f"{warm_ups} further workload bodies written untimed first, "
                "through the same server"
            ),
            "gate_check": (
                "before any timed call, every body passed the write gates on "
                "the scratch copy with every acknowledge flag off, and "
                "overlapped nothing active or removed there nor another body "
                "at the dedup medium line"
            ),
        },
        "outcomes": {
            "statuses": timings["statuses"],
            "calls_per_write": timings["calls_per_write"],
        },
        "results": results,
    }


def write_artifact(out: Path, artifact: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


# ---------------------------------------------------------------------------
# The stop arm (unit CAPS, CAPS-P4)
# ---------------------------------------------------------------------------

#: Untimed warm-ups before each stop pass, the workload seed, and the size
#: of the synthetic transcript the open decision reads.
STOP_WARM_UPS = 5
STOP_SEED = 20261013
STOP_TRANSCRIPT_BYTES = 5 * 1024 * 1024
#: The Claude Code version whose transcript rows the synthetic one copies.
STOP_TRANSCRIPT_VERSION = "2.1.295"
STOP_SESSION = "c4b1e2d3-0000-4a5b-8c6d-stopbench0001"
STOP_WORDS = (
    "daemon store batch capture hook transcript session config release branch "
    "schema index migration parser token cache worker request reply mirror "
    "tombstone review gate rule scope project service endpoint latency sample"
).split()


def _stop_text(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(STOP_WORDS) for _ in range(words)) + "."


def stop_transcript(
    path: Path, target_bytes: int, seed: int = STOP_SEED
) -> dict[str, int]:
    """A synthetic transcript of about `target_bytes` in Claude Code
    2.1.295's row shape: user rows whose message.content is the prompt
    string, assistant rows whose message.content is a list of thinking,
    text and tool_use blocks, and user rows carrying a tool_result, the
    bulk of a real transcript's bytes. Returns the row counts, the bytes,
    and the characters of prompt and assistant text (what the checkpoint
    counts)."""
    rng = random.Random(seed)
    counts = {"user": 0, "assistant": 0, "tool_result": 0, "text_chars": 0}
    written = 0
    turn = 0
    parent: str | None = None

    def row(kind: str, message: dict[str, Any], **extra: Any) -> dict[str, Any]:
        nonlocal parent
        uuid = f"{turn:08x}-0000-4000-8000-{rng.getrandbits(48):012x}"
        doc = {
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/work/stop-bench",
            "sessionId": STOP_SESSION,
            "version": STOP_TRANSCRIPT_VERSION,
            "gitBranch": "main",
            "type": kind,
            "message": message,
            "uuid": uuid,
            "timestamp": (
                f"2026-10-13T{(turn // 60) % 24:02d}:{turn % 60:02d}:00.000Z"
            ),
            **extra,
        }
        parent = uuid
        return doc

    with path.open("w", encoding="utf-8") as fh:
        while written < target_bytes:
            prompt = _stop_text(rng, rng.randint(20, 120))
            answer = _stop_text(rng, rng.randint(60, 300))
            tool_id = f"toolu_{turn:010d}"
            rows = [
                row("user", {"role": "user", "content": prompt}),
                row(
                    "assistant",
                    {
                        "id": f"msg_{turn:010d}",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5-5",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": _stop_text(rng, 40),
                                "signature": "s",
                            },
                            {"type": "text", "text": answer},
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "Read",
                                "input": {"file_path": "/work/stop-bench/app.py"},
                            },
                        ],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 1200, "output_tokens": 300},
                    },
                    requestId=f"req_{turn:010d}",
                ),
                row(
                    "user",
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": _stop_text(rng, rng.randint(300, 1500)),
                            }
                        ],
                    },
                    toolUseResult={"type": "text"},
                ),
            ]
            for doc in rows:
                line = json.dumps(doc) + "\n"
                fh.write(line)
                written += len(line.encode("utf-8"))
            counts["user"] += 1
            counts["assistant"] += 1
            counts["tool_result"] += 1
            counts["text_chars"] += len(prompt) + len(answer)
            turn += 1
    counts["bytes"] = written
    return counts


def _stop_config(env: dict[str, str], *, enabled: bool) -> None:
    """The scratch HOME's config.toml with `[capture]` on or off."""
    proc = subprocess.run(
        [
            PYTHON,
            "-c",
            "from bettermemory.config import default_config_path; "
            "print(default_config_path())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    path = Path(proc.stdout.strip())
    if not str(path).startswith(env["HOME"]):
        raise SystemExit(f"config path {path} is outside the scratch HOME")
    path.parent.mkdir(parents=True, exist_ok=True)
    flag = "true" if enabled else "false"
    path.write_text(
        f"[capture]\nstop_checkpoint = {flag}\ndaily_cap = 100000\n", encoding="utf-8"
    )


def _stop_pass(
    env: dict[str, str],
    scratch: Path,
    *,
    enabled: bool,
    payloads: list[dict[str, Any]],
    warm_ups: list[dict[str, Any]],
) -> tuple[list[float], list[dict[str, Any]], int | None]:
    """One daemon start with capture on or off: the warm-ups, then each
    payload posted to /api/v1/hook/stop and timed; the daemon is stopped
    with `down` under the scratch env. Returns the times, replies and pid."""
    from bettermemory._daemon_client import post, read_state

    _stop_config(env, enabled=enabled)
    up = _cli(["up", "--port", "0"], env)
    if up.returncode != 0:
        raise SystemExit(f"daemon did not start: {up.stderr[-500:]}")
    state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        _cli(["down"], env)
        raise SystemExit("no daemon state for the stop arm")
    times: list[float] = []
    replies: list[dict[str, Any]] = []
    try:
        for payload in warm_ups:
            post(
                state["port"], state["token"], "/api/v1/hook/stop", payload, timeout=30
            )
        for payload in payloads:
            started = time.perf_counter()
            reply = post(
                state["port"], state["token"], "/api/v1/hook/stop", payload, timeout=30
            )
            times.append(time.perf_counter() - started)
            replies.append(reply)
    finally:
        _cli(["down"], env)
    return times, replies, state.get("pid")


def run_stop_arm(*, v8: Path, scratch: Path, n: int) -> dict[str, Any]:
    """CAPS-P4. The /hook/stop service time with `[capture] stop_checkpoint`
    off against on (attended "1", nothing due: one session over a transcript
    far below the threshold), each from a warm client on its own daemon
    start; then the open decision over a 5 MB transcript, as
    `capture_hook.on_stop` in this process (the graded number: a fresh
    session per sample, so each reads the whole stretch and opens a batch)
    and as the daemon's service time for the same kind of opening Stop."""
    refuse_live_store(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    # Resolved: the daemon keys its state file by the resolved store path
    # (/var is /private/var on macOS).
    scratch = scratch.resolve()
    env = _env(scratch)
    env["HOME"] = str(scratch / "home")
    env.pop("CLAUDE_CODE_SESSION_ATTENDED", None)
    (scratch / "home").mkdir(parents=True, exist_ok=True)
    report = migrate(v8, scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)

    tdir = scratch / "transcripts"
    tdir.mkdir()
    small = tdir / f"{STOP_SESSION}.jsonl"
    small_shape = stop_transcript(small, 20_000)

    def stop(session: str, path: Path) -> dict[str, Any]:
        return {
            "session_id": session,
            "transcript_path": str(path),
            "cwd": str(ROOT),
            "attended": "1",
            "quiet": True,
        }

    idle = [stop(STOP_SESSION, small) for _ in range(n)]
    warm = idle[:STOP_WARM_UPS]
    pids: list[int | None] = []
    off, _, pid = _stop_pass(env, scratch, enabled=False, payloads=idle, warm_ups=warm)
    pids.append(pid)
    print(f"stop service, capture off {_summary(off)}", file=sys.stderr)
    on, on_replies, pid = _stop_pass(
        env, scratch, enabled=True, payloads=idle, warm_ups=warm
    )
    pids.append(pid)
    print(f"stop service, capture on {_summary(on)}", file=sys.stderr)

    # A transcript is acted on only when its file is named for its session,
    # so each fresh session gets a hard link to the one 5 MB file.
    big = tdir / "big.jsonl"
    big_shape = stop_transcript(big, STOP_TRANSCRIPT_BYTES)

    def linked(prefix: str) -> list[dict[str, Any]]:
        out = []
        for i in range(n + STOP_WARM_UPS):
            session = f"c4b1e2d3-{prefix}-4a5b-8c6d-{i:012d}"
            os.link(big, tdir / f"{session}.jsonl")
            out.append(stop(session, tdir / f"{session}.jsonl"))
        return out

    opening = linked("0000")
    open_times, open_replies, pid = _stop_pass(
        env,
        scratch,
        enabled=True,
        payloads=opening[STOP_WARM_UPS:],
        warm_ups=opening[:STOP_WARM_UPS],
    )
    pids.append(pid)
    print(f"opening stop service {_summary(open_times)}", file=sys.stderr)

    # The decision itself, in this process, on the same store with the
    # daemon down.
    for name in ("HOME", "BETTERMEMORY_DIR", "BETTERMEMORY_KEYS_DIR"):
        os.environ[name] = env[name]
    from bettermemory import capture_hook
    from bettermemory.config import CaptureConfig, Config, StorageConfig
    from bettermemory.store import Store

    config = Config(
        storage=StorageConfig(directory=str(scratch / "store")),
        capture=CaptureConfig(stop_checkpoint=True, daily_cap=100_000),
    )
    store = Store(scratch / "store")
    decision: list[float] = []
    decided = Counter[str]()
    for index, payload in enumerate(linked("1111")):
        started = time.perf_counter()
        step = capture_hook.on_stop(config, store, payload)
        elapsed = time.perf_counter() - started
        if index >= STOP_WARM_UPS:
            decision.append(elapsed)
            decided[step.action if step else "none"] += 1
    read_only: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        capture_hook.read_stretch(big, 0)
        read_only.append(time.perf_counter() - started)
    store.close()

    from bettermemory import __version__

    off_s, on_s, decision_s = _summary(off), _summary(on), _summary(decision)
    delta = round(on_s["p50_ms"] - off_s["p50_ms"], 2)
    return {
        "kind": "daemon-latency-stop",
        "prediction": "CAPS-P4",
        "version": __version__,
        "commit": _git("rev-parse", "HEAD"),
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": _machine(),
        "store": {
            "source": str(v8),
            "memories_imported": report.get("memories", {}),
            "migration_seconds": report["seconds"],
        },
        "method": {
            "n": n,
            "warm_ups": STOP_WARM_UPS,
            "seed": STOP_SEED,
            "transcript_shape": STOP_TRANSCRIPT_VERSION,
            "small_transcript": small_shape,
            "big_transcript": big_shape,
            "service": (
                "_daemon_client.post to /api/v1/hook/stop from a warm client, "
                "attended '1', one session over a transcript below the "
                "checkpoint; capture off on one daemon start, then on "
                "(daily_cap 100000) on the next"
            ),
            "open_decision": (
                "capture_hook.on_stop in this process on the scratch store, "
                "capture on, a fresh session per sample over a hard link of the "
                "5 MB transcript, so each reads the whole stretch and opens a batch"
            ),
            "open_endpoint": (
                "the same kind of opening Stop posted to the daemon's "
                "/api/v1/hook/stop, which also runs the turn audit"
            ),
        },
        "results": {
            "stop_service_capture_off": off_s,
            "stop_service_capture_on": on_s,
            "stop_service_p50_delta_ms": delta,
            "stop_service_on_replies_with_capture": sum(
                "capture" in r for r in on_replies
            ),
            "open_decision": decision_s,
            "open_decision_actions": dict(decided),
            "open_endpoint_service": _summary(open_times),
            "open_endpoint_opened": sum(
                (r.get("capture") or {}).get("action") == "opened" for r in open_replies
            ),
            "read_stretch_5mb": _summary(read_only),
        },
        "bounds": {
            "service_delta": {
                "prediction": "capture-on p50 within 5 ms of capture-off",
                "missed_if": "capture-on p50 more than 10 ms above capture-off",
                "observed_ms": delta,
                "verdict": "MISSED" if delta > 10 else "HIT",
            },
            "open_decision": {
                "prediction": "p50 under 60 ms",
                "missed_if": "p50 over 120 ms",
                "observed_ms": decision_s["p50_ms"],
                "verdict": "MISSED" if decision_s["p50_ms"] > 120 else "HIT",
            },
        },
        "daemon_pids": pids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--arm",
        choices=("read", "write", "prompt", "stop", "brief"),
        default="read",
        help=(
            "read: the hook and memory_search arms (the default); write: the "
            "memory_write arm on a copy imported from --tgz; prompt: the "
            "per-prompt recall, prompts spaced 3 s apart; stop: the /hook/stop "
            "service time with capture off and on, and the capture open decision; "
            "brief: the session brief, standing off and on"
        ),
    )
    parser.add_argument(
        "--v8",
        type=Path,
        default=None,
        help="the v8 store directory to migrate into the scratch store (read and prompt arms)",
    )
    parser.add_argument(
        "--tgz",
        type=Path,
        default=None,
        help="the sealed store tarball, its .sha256 beside it (write arm)",
    )
    parser.add_argument(
        "--seed", type=int, default=WRITE_SEED, help="the write arm's workload seed"
    )
    parser.add_argument(
        "--note",
        default=None,
        help="a provenance note written into the write arm's file",
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

    if args.arm == "write":
        if args.tgz is None:
            parser.error("--arm write needs --tgz")
        scratch = args.scratch or Path(
            tempfile.mkdtemp(prefix="bettermemory-daemon-write-bench-")
        )
        artifact = run_write_arm(
            tgz=args.tgz,
            scratch=scratch,
            n=args.n,
            seed=args.seed,
            note=args.note,
        )
        write_artifact(args.out, artifact)
        print(f"wrote {args.out}", file=sys.stderr)
        return
    if args.v8 is None:
        parser.error(f"the {args.arm} arm needs --v8")
    if args.arm == "prompt":
        scratch = args.scratch or Path(
            tempfile.mkdtemp(prefix="bettermemory-daemon-prompt-bench-")
        )
        artifact = run_prompt_arm(v8=args.v8, scratch=scratch, n=args.n)
        write_artifact(args.out, artifact)
        print(f"wrote {args.out}", file=sys.stderr)
        return
    if args.arm == "brief":
        scratch = args.scratch or Path(
            tempfile.mkdtemp(prefix="bettermemory-daemon-brief-bench-")
        )
        try:
            artifact = run_brief_arm(v8=args.v8, scratch=scratch, n=args.n)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        artifact["scratch_removed"] = not scratch.exists()
        write_artifact(args.out, artifact)
        print(f"wrote {args.out}", file=sys.stderr)
        return
    if args.arm == "stop":
        scratch = args.scratch or Path(
            tempfile.mkdtemp(prefix="bettermemory-daemon-stop-bench-")
        )
        try:
            artifact = run_stop_arm(v8=args.v8, scratch=scratch, n=args.n)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        artifact["scratch_removed"] = not scratch.exists()
        write_artifact(args.out, artifact)
        print(f"wrote {args.out}", file=sys.stderr)
        return

    scratch = args.scratch or Path(
        tempfile.mkdtemp(prefix="bettermemory-daemon-bench-")
    )
    scratch.mkdir(parents=True, exist_ok=True)
    # The daemon keys its state file on the store's resolved path, so a
    # scratch under a symlink (macOS's /var) must be resolved to be found.
    scratch = scratch.resolve()
    env = _env(scratch)
    qs = queries(args.n)
    if len(qs) < args.n:
        raise SystemExit(f"only {len(qs)} questions in {QUESTIONS}")

    print(f"scratch {scratch}", file=sys.stderr)
    report = migrate(args.v8, scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)

    results: dict[str, Any] = {}
    try:
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
    finally:
        # A daemon left running outlives the scratch it serves.
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
    write_artifact(args.out, artifact)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
