"""Where a prompt hook's time goes, on a scratch daemon over a copy of a v9
store: the `--arm breakdown` and `--arm uvx` of bench/daemon/latency.py.

The panel (bench/panel) reads the prompt hook's wall time as Claude Code
measures it, from the spawn of the hook's shell to its exit, and the daemon's
answer is a small part of it. This arm splits one round trip into parts,
each timed from outside the hook as its own process or call, interleaved
round by round so that drift falls on every part alike:

  shell           `sh -c :`: the shell Claude Code spawns for a hook command,
                  with nothing run in it.
  interpreter     `python -c pass` with the interpreter the installed
                  `bettermemory` script runs: the interpreter's start with
                  its site, and nothing of bettermemory.
  client_import   `python -c "import bettermemory._entry,
                  bettermemory._daemon_client"`: the interpreter's start and
                  the hook client's import.
  http_round_trip `_daemon_client.identify` from a warm client in this
                  process: one loopback connection and one GET /health with
                  a nonce, the daemon's cheapest route.
  service         `_daemon_client.post` of the prompt to /api/v1/hook/prompt
                  from a warm client: a proven connection (the same /health
                  exchange) and the POST, the daemon's work inside it.
  hook_process    `sh -c '<bin>/bettermemory hook prompt'`, the payload on
                  stdin: the whole hook as an installed launcher runs it.

Every daemon call (http_round_trip, service, hook_process) follows its own
idle gap, and http_round_trip and service swap order every round, so that
neither always meets a daemon the other just woke. Every prompt opens a
session of its own: each call measures the first prompt of a session, with
nothing yet delivered in it to withhold.

The derived parts are differences of medians, so they are estimates and not
one call's split: launcher start (shell), interpreter start (interpreter
less shell), client import (client_import less interpreter), the HTTP round
trip (http_round_trip), the daemon's own work (service less
http_round_trip, which keeps the POST's own framing and JSON in it), and the
rest of the hook process (hook_process less shell, client_import and
service: reading stdin, the location probe, the state file, the daemon's
identification and writing the reply).

The uvx arm times `sh -c 'uvx --offline bettermemory hook prompt'`, the
plugin's launch, with `UV_OFFLINE=1`. It first resolves the package from
uv's cache alone (`uvx --offline --from bettermemory python -c ...`). It
times nothing unless that resolves this tree's own version: a failure is
recorded as a fixed category (`no_uvx`, `not_in_cache`, `other`), and a
cached client of another version as `version_mismatch`, since such a client
need not honour the scratch's variables.

The store is a copy: `--store-copy` names a v9 store file (a bench/scale
store, or a copy taken earlier), `--snapshot` a store to copy from. Both go
through SQLite's backup API on a read-only connection
(`bench/panel/store_side.snapshot`), so a WAL's committed pages come along.
The daemon runs with a minimal environment whose HOME is inside the scratch,
so the config, keys and state directories platformdirs resolves are the
scratch's too, as are BETTERMEMORY_DIR, BETTERMEMORY_KEYS_DIR and
BETTERMEMORY_STATE_DIR. After the rounds the daemon is stopped, the stop's
result checked and the store's daemon lock taken, so no daemon outlives the
scratch. The artifact holds counts and timings only: no path, prompt, id or
project name.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

#: The seconds before each daemon call: a hook between a user's prompts
#: meets an idle daemon.
BREAKDOWN_GAP_SECONDS = 3.0
#: Untimed rounds first.
BREAKDOWN_WARM_UPS = 2
#: How long the stop may take to release the store's daemon lock.
RELEASE_SECONDS = 15.0
#: The parts each round times.
PARTS = (
    "shell",
    "interpreter",
    "client_import",
    "http_round_trip",
    "service",
    "hook_process",
)
CLIENT_IMPORT = "import bettermemory._entry, bettermemory._daemon_client"
#: The variables the minimal environment keeps from this one, when set.
KEPT_ENV = ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL")

Runner = Callable[
    [list[str], dict[str, str], str | None],
    tuple[float, subprocess.CompletedProcess[str]],
]


def summary_ms(values_s: list[float]) -> dict[str, float]:
    if not values_s:
        raise ValueError("no timings to summarize")
    ms = sorted(v * 1000.0 for v in values_s)
    p95 = ms[min(len(ms) - 1, max(0, round(0.95 * (len(ms) - 1))))]
    return {
        "n": len(ms),
        "p50_ms": round(statistics.median(ms), 2),
        "p95_ms": round(p95, 2),
        "min_ms": round(ms[0], 2),
        "max_ms": round(ms[-1], 2),
    }


def derived(results: dict[str, dict[str, float]]) -> dict[str, float]:
    """The parts as differences of the arms' medians, in ms."""
    p = {name: results[name]["p50_ms"] for name in PARTS if name in results}
    out = {
        "launcher_start": p["shell"],
        "interpreter_start": p["interpreter"] - p["shell"],
        "client_import": p["client_import"] - p["interpreter"],
        "http_round_trip": p["http_round_trip"],
        "daemon_work": p["service"] - p["http_round_trip"],
        "hook_process_rest": p["hook_process"]
        - p["shell"]
        - p["client_import"]
        - p["service"],
        "hook_process": p["hook_process"],
    }
    return {key: round(value, 2) for key, value in out.items()}


def scratch_env(scratch: Path, source: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment: the kept variables of `source`, HOME inside
    the scratch, and the three store variables on the scratch."""
    source = dict(os.environ) if source is None else source
    env = {key: source[key] for key in KEPT_ENV if key in source}
    env["HOME"] = str(scratch / "home")
    env["BETTERMEMORY_DIR"] = str(scratch / "store")
    env["BETTERMEMORY_KEYS_DIR"] = str(scratch / "keys")
    env["BETTERMEMORY_STATE_DIR"] = str(scratch / "state")
    return env


def launcher() -> Path:
    """The installed `bettermemory` script beside this interpreter."""
    script = Path(PYTHON).parent / "bettermemory"
    if not script.is_file():
        raise SystemExit(f"no bettermemory script beside {PYTHON}; run uv sync")
    return script


def store_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            table: int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            for table in ("memories", "log")
            if table in tables
        }
    finally:
        conn.close()


def place_store(
    scratch: Path,
    *,
    store_copy: Path | None,
    snapshot_from: Path | None,
    refuse: Callable[[Path], None],
) -> dict[str, Any]:
    """Put the store under `scratch/store` and say how it got there."""
    sys.path.insert(0, str(ROOT / "bench" / "panel"))
    import store_side

    target = scratch / "store" / "memory.sqlite"
    if target.exists():
        raise SystemExit(f"{target} exists; the scratch must be fresh")
    for sub in ("keys", "home", "state"):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    if store_copy is not None:
        refuse(store_copy)
        store_side.snapshot(store_copy, target)
        keys = store_copy.parent.parent / "keys"
        source = "store-copy"
        if store_copy.parent.name == "store" and keys.is_dir():
            # A bench/scale scratch keeps its keys beside its store.
            shutil.copytree(keys, scratch / "keys", dirs_exist_ok=True)
            source = "store-copy with its scratch keys"
    elif snapshot_from is not None:
        store_side.snapshot(snapshot_from, target)
        source = "read-only snapshot without keys"
    else:
        raise SystemExit("the breakdown needs --store-copy or --snapshot")
    return {
        "source": source,
        "bytes": target.stat().st_size,
        "rows": store_counts(target),
    }


def _run(
    argv: list[str], env: dict[str, str], stdin: str | None = None
) -> tuple[float, subprocess.CompletedProcess[str]]:
    started = time.perf_counter()
    proc = subprocess.run(
        argv, env=env, input=stdin, capture_output=True, text=True, timeout=120
    )
    return time.perf_counter() - started, proc


def _cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(launcher()), *args], env=env, capture_output=True, text=True, timeout=120
    )


def store_released(scratch: Path, wait: float = RELEASE_SECONDS) -> bool:
    """Whether the scratch store's daemon lock can be taken within `wait`
    seconds: no daemon still serves it."""
    from bettermemory._daemon_client import lock_file_for, try_lock

    lock = lock_file_for(scratch / "state", scratch / "store" / "memory.sqlite")
    deadline = time.monotonic() + wait
    while True:
        handle = try_lock(lock)
        if handle is not None:
            handle.close()
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


@contextlib.contextmanager
def scratch_daemon(
    scratch: Path,
    env: dict[str, str],
    *,
    cli: Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str]] = _cli,
    released: Callable[[Path], bool] = store_released,
    outcome: dict[str, Any] | None = None,
) -> Iterator[None]:
    """A daemon on the scratch store for the block's life. `down` runs
    whatever `up` did, its result is checked, and the store's lock must be
    free afterwards; either failing is a SystemExit after the block."""
    outcome = {} if outcome is None else outcome
    try:
        up = cli(["up", "--port", "0"], env)
        if up.returncode != 0:
            raise SystemExit(f"daemon did not start: {up.stderr[-500:]}")
        yield
    finally:
        down = cli(["down"], env)
        outcome["down_ok"] = down.returncode == 0
        outcome["store_released"] = released(scratch)
        if not (outcome["down_ok"] and outcome["store_released"]):
            raise SystemExit(
                f"the scratch daemon did not stop cleanly: {outcome}; "
                "the scratch is kept for inspection"
            )


def hook_failed(proc: subprocess.CompletedProcess[str]) -> bool:
    """A hook always exits 0; its failure is a stderr line naming it."""
    return proc.returncode != 0 or "bettermemory hook prompt:" in proc.stderr


def payload(prompt: str, round_index: int, role: str) -> dict[str, Any]:
    """The prompt hook's payload, in a session of its own."""
    return {
        "prompt": prompt,
        "session_id": f"bench-breakdown-{round_index:04d}-{role}",
        "cwd": str(ROOT),
    }


def run_rounds(
    scratch: Path,
    env: dict[str, str],
    prompts: list[str],
    gap: float,
    *,
    run: Runner = _run,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[float]]:
    """Every round's timings, the first BREAKDOWN_WARM_UPS rounds dropped."""
    if client is None:
        from bettermemory import _daemon_client as client
    state = client.read_state(scratch / "state", scratch / "store" / "memory.sqlite")
    if state is None:
        raise SystemExit("no daemon state for the breakdown")
    hook_cmd = f"'{launcher()}' hook prompt"
    times: dict[str, list[float]] = {name: [] for name in PARTS}

    def note(name: str, took: float, index: int) -> None:
        if index >= BREAKDOWN_WARM_UPS:
            times[name].append(took)

    def health(index: int) -> None:
        sleep(gap)
        started = time.perf_counter()
        answer = client.identify(state, timeout=5)
        note("http_round_trip", time.perf_counter() - started, index)
        if not answer.ours:
            raise SystemExit("the scratch daemon did not prove itself")

    def service(index: int, prompt: str) -> None:
        sleep(gap)
        body = payload(prompt, index, "service")
        started = time.perf_counter()
        client.post(
            state["port"], state["token"], "/api/v1/hook/prompt", body, timeout=30
        )
        note("service", time.perf_counter() - started, index)

    for index, prompt in enumerate(prompts):
        for name, argv in (
            ("shell", ["/bin/sh", "-c", ":"]),
            ("interpreter", [PYTHON, "-c", "pass"]),
            ("client_import", [PYTHON, "-c", CLIENT_IMPORT]),
        ):
            took, proc = run(argv, env, None)
            if proc.returncode != 0:
                raise SystemExit(f"{name} failed: {proc.stderr[-500:]}")
            note(name, took, index)
        if index % 2 == 0:
            health(index)
            service(index, prompt)
        else:
            service(index, prompt)
            health(index)
        sleep(gap)
        stdin = json.dumps(payload(prompt, index, "hook"))
        took, proc = run(["/bin/sh", "-c", hook_cmd], env, stdin)
        if hook_failed(proc):
            raise SystemExit(f"hook failed: {proc.stderr[-500:]}")
        note("hook_process", took, index)
    return times


def daemon_log_warnings(scratch: Path) -> int:
    from bettermemory._daemon_client import log_file_for

    log = log_file_for(scratch / "state", scratch / "store" / "memory.sqlite")
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if "WARNING" in line or "ERROR" in line)


def run_breakdown(
    *,
    scratch: Path,
    store_copy: Path | None,
    snapshot_from: Path | None,
    n: int,
    gap: float,
    prompts: list[str],
    refuse: Callable[[Path], None],
) -> dict[str, Any]:
    if n < 1:
        raise SystemExit("--n must be at least 1")
    refuse(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    scratch = scratch.resolve()
    store = place_store(
        scratch, store_copy=store_copy, snapshot_from=snapshot_from, refuse=refuse
    )
    env = scratch_env(scratch)
    stopped: dict[str, Any] = {}
    with scratch_daemon(scratch, env, outcome=stopped):
        times = run_rounds(scratch, env, prompts[: n + BREAKDOWN_WARM_UPS], gap)
    store["rows_after"] = store_counts(scratch / "store" / "memory.sqlite")
    store["daemon_log_warnings"] = daemon_log_warnings(scratch)
    results = {name: summary_ms(values) for name, values in times.items()}
    return {
        "store": store,
        "stop": stopped,
        "results": results,
        "derived_p50_ms": derived(results),
        "method": {
            "n": n,
            "warm_up_rounds": BREAKDOWN_WARM_UPS,
            "gap_seconds": gap,
            "hook": "prompt",
            "sessions": (
                "one session per prompt and per caller: each call is the first "
                "prompt of its session, with nothing delivered yet to withhold"
            ),
            "order": (
                "a gap before each daemon call; http_round_trip and service "
                "swap order every round"
            ),
            "cwd": "the repository root",
            "environment": (
                "PATH and locale kept; HOME, BETTERMEMORY_DIR, "
                "BETTERMEMORY_KEYS_DIR and BETTERMEMORY_STATE_DIR in the scratch"
            ),
            "launcher": "the installed bettermemory script of the bench's environment",
            "parts": {
                "shell": "sh -c :",
                "interpreter": "python -c pass",
                "client_import": f"python -c '{CLIENT_IMPORT}'",
                "http_round_trip": "_daemon_client.identify, warm client in the bench",
                "service": "_daemon_client.post to /api/v1/hook/prompt, warm client",
                "hook_process": "sh -c '<launcher> hook prompt', payload on stdin",
            },
            "derived": (
                "differences of p50s, not a split of one call: launcher_start = "
                "shell; interpreter_start = interpreter - shell; client_import = "
                "client_import - interpreter; daemon_work = service - "
                "http_round_trip (keeps the POST's own framing and JSON); "
                "hook_process_rest = hook_process - shell - client_import - "
                "service (stdin, location probe, state file, identification, "
                "output)"
            ),
        },
    }


# ---------------------------------------------------------------------------
# The uvx arm (ENG-111)
# ---------------------------------------------------------------------------


def uv_cache_dir(source: dict[str, str] | None = None) -> str | None:
    """uv's cache directory as this environment resolves it."""
    source = dict(os.environ) if source is None else source
    uv = shutil.which("uv", path=source.get("PATH"))
    if uv is None:
        return None
    proc = subprocess.run(
        [uv, "cache", "dir", "--offline"],
        env=source,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def uvx_env(scratch: Path, source: dict[str, str] | None = None) -> dict[str, str]:
    env = scratch_env(scratch, source)
    env["UV_OFFLINE"] = "1"
    cache = uv_cache_dir(source)
    if cache:
        # HOME moved into the scratch would otherwise move uv's cache too.
        env["UV_CACHE_DIR"] = cache
    return env


def failure_category(stderr: str) -> str:
    text = stderr.lower()
    if "offline" in text or "cache" in text or "network" in text:
        return "not_in_cache"
    return "other"


def uvx_resolves(env: dict[str, str], expected: str) -> dict[str, Any]:
    """Whether uvx resolves bettermemory from uv's cache alone at version
    `expected`. Never touches the network: `--offline` and `UV_OFFLINE=1`.
    uv's own words go to stderr only; the result names a fixed category."""
    uvx = shutil.which("uvx", path=env.get("PATH"))
    if uvx is None:
        return {"resolved": False, "why": "no_uvx"}
    probe = "import bettermemory; print(bettermemory.__version__)"
    proc = subprocess.run(
        [uvx, "--offline", "--from", "bettermemory", "python", "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        print(f"uvx: {lines[-1] if lines else ''}", file=sys.stderr)
        return {
            "resolved": False,
            "why": failure_category(proc.stderr),
            "exit": proc.returncode,
        }
    version = proc.stdout.strip()
    if version != expected:
        return {
            "resolved": True,
            "version": version,
            "expected": expected,
            "why": "version_mismatch",
        }
    return {"resolved": True, "version": version}


def run_uvx(
    *,
    scratch: Path,
    store_copy: Path | None,
    snapshot_from: Path | None,
    n: int,
    gap: float,
    prompts: list[str],
    refuse: Callable[[Path], None],
    run: Runner = _run,
) -> dict[str, Any]:
    from bettermemory import __version__

    if n < 1:
        raise SystemExit("--n must be at least 1")
    refuse(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    scratch = scratch.resolve()
    env = uvx_env(scratch)
    resolution = uvx_resolves(env, __version__)
    if not resolution["resolved"] or "why" in resolution:
        print(f"the uvx arm times nothing: {resolution['why']}", file=sys.stderr)
        return {"resolution": resolution, "results": None}
    store = place_store(
        scratch, store_copy=store_copy, snapshot_from=snapshot_from, refuse=refuse
    )
    times: list[float] = []
    stopped: dict[str, Any] = {}
    with scratch_daemon(scratch, env, outcome=stopped):
        for index, prompt in enumerate(prompts[: n + BREAKDOWN_WARM_UPS]):
            time.sleep(gap)
            took, proc = run(
                ["/bin/sh", "-c", "uvx --offline bettermemory hook prompt"],
                env,
                json.dumps(payload(prompt, index, "uvx")),
            )
            if hook_failed(proc):
                raise SystemExit(f"uvx hook failed: {proc.stderr[-500:]}")
            if index >= BREAKDOWN_WARM_UPS:
                times.append(took)
    return {
        "resolution": resolution,
        "store": store,
        "stop": stopped,
        "results": {"uvx_hook_process": summary_ms(times)},
        "method": {
            "n": n,
            "warm_up_rounds": BREAKDOWN_WARM_UPS,
            "gap_seconds": gap,
            "command": "sh -c 'uvx --offline bettermemory hook prompt', UV_OFFLINE=1",
            "sessions": "one session per prompt",
        },
    }
