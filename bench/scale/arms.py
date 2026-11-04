"""The scale bench's arms (ENG-109): what the hooks, the tools, the brief
and membership resolution cost on a built store, the peak RSS of the
whole-store commands, the concurrent-writer arm, and ENG-159's arm.

Every timing is a call in this process, as the daemon's worker makes it:
the hook bodies (`recall.session_brief`, `recall.prompt_recall`) on the
store with the daemon's shared `RecallState`, the tools through
`build_server(...).call_tool`, resolution through `location.resolve_hook`
(a hook's) and `location.resolve_caller` (a first sight). What a hook
costs at the harness is this plus the hook client's start and the round
trip to the daemon, which do not grow with the store (measured apart in
bench/daemon/results/).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import build
import world

#: Untimed calls before each arm's timed ones.
WARM_UPS = 3

#: The prompts of one session in the prompt arm, and in a probed brief's
#: transcript.
SESSION_PROMPTS = 6


@dataclass
class Quiet:
    """The quiet-machine gate of the timed arms: before each, wait for
    the 1-minute load to fall under `world.QUIET_LOAD`, out of one budget
    for the run (90 minutes). `quiet` stays true while every arm began on
    a quiet machine."""

    budget: float = field(
        default_factory=lambda: (
            float(os.environ.get("BENCH_SCALE_QUIET_MINUTES", "90")) * 60
        )
    )
    quiet: bool = True
    waits: list[dict[str, Any]] = field(default_factory=list)

    def wait(self, arm: str) -> None:
        started = time.monotonic()
        seen = world.wait_quiet(max_wait=max(0.0, self.budget))
        self.budget -= time.monotonic() - started
        seen["arm"] = arm
        self.waits.append(seen)
        if not seen["quiet_at_start"]:
            self.quiet = False


@dataclass
class Place:
    path: Path
    name: str
    words: list[str]
    weight: float
    kind: str


def places(scratch: Path) -> list[Place]:
    """The checkouts the build saw, from its `checkouts.json`."""
    rows = json.loads((scratch / "checkouts.json").read_text(encoding="utf-8"))
    return [
        Place(Path(r["path"]), r["name"], r["words"], r["weight"], r["kind"])
        for r in rows
        if r["sighted"]
    ]


class Bench:
    """One built store, open in this process, with the daemon's shared
    recall state."""

    def __init__(self, scratch: Path, seed: int) -> None:
        from bettermemory.recall_state import RecallState

        self.scratch = scratch
        self.store = build.open_store(scratch / "store")
        self.config = build.config_for(scratch / "store")
        self.recall_state = RecallState()
        self.places = places(scratch)
        self.rng = random.Random(seed)
        self.transcripts = scratch / "transcripts" / "measure"
        self.transcripts.mkdir(parents=True, exist_ok=True)

    def place(self) -> Place:
        return self.rng.choices(self.places, weights=[p.weight for p in self.places])[0]

    def session(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def transcript(self, session: str, place: Place, prompts: int) -> build.Transcript:
        t = build.Transcript(self.transcripts / f"{session}.jsonl", session, place.path)
        for i in range(prompts):
            t.user(world.prompt(self.rng, place.words), f"p{i}")
            t.assistant(world.sentence(self.rng, place.words, 12))
        return t


def _timed(n: int, call: Callable[[], Any]) -> list[float]:
    for _ in range(WARM_UPS):
        call()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        call()
        out.append(time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------------------
# The latency arms
# ---------------------------------------------------------------------------


def arm_resolution(b: Bench, n: int) -> dict[str, Any]:
    """A hook's resolution of a known checkout (`resolve_hook`, a fresh
    session id each call), as every hook pays it."""
    from bettermemory.location import resolve_hook

    def call() -> None:
        place = b.place()
        resolve_hook(b.store, b.config, str(place.path), None, session=b.session())

    return world.summary(_timed(n, call))


def arm_first_sight(b: Bench, n: int, *, commit: bool) -> dict[str, Any]:
    """A first sight of a new repository (`resolve_caller`): with a commit
    of its own, or commit-less (the content candidates of ENG-159)."""
    from bettermemory.location import resolve_caller

    root = b.scratch / "repos" / ("fresh" if commit else "fresh-bare")
    times = []
    for i in range(n + 1):
        repo = world.make_repo(
            root / f"r{i:03d}" / f"fresh{i}",
            remote=f"git@example.invalid:new/fresh{i}.git" if commit else None,
            commit=commit,
        )
        t0 = time.perf_counter()
        resolve_caller(b.store, b.config, str(repo))
        if i:  # the first is a warm-up
            times.append(time.perf_counter() - t0)
    return world.summary(times)


def arm_session_start(b: Bench, n: int, *, probed: bool) -> dict[str, Any]:
    """The session-start hook body for a new session: with no transcript
    yet (a first start, the empty probe), or with `SESSION_PROMPTS`
    prompts in it, so the probe ranks the brief. Each transcript is
    written before its call's clock starts."""
    from bettermemory import recall

    shown: Counter[str] = Counter()
    pointers: list[int] = []
    times = []
    for i in range(n + WARM_UPS):
        place = b.place()
        session = b.session()
        if probed:
            path = str(b.transcript(session, place, SESSION_PROMPTS).path)
        else:
            path = str(b.transcripts / f"{session}.jsonl")
        t0 = time.perf_counter()
        result = recall.session_brief(
            session_id=session,
            source="startup",
            harness="claude-code",
            transcript_path=path,
            cwd=place.path,
            config=b.config,
            store=b.store,
            state=b.recall_state,
        )
        elapsed = time.perf_counter() - t0
        if i >= WARM_UPS:
            times.append(elapsed)
            shown["with_ids" if result.ids else "empty"] += 1
            pointers.append(len(result.ids))
    out = world.summary(times)
    out["shown"] = dict(shown)
    out["pointers_mean"] = round(sum(pointers) / max(1, len(pointers)), 2)
    return out


def arm_prompt(b: Bench, n: int) -> dict[str, Any]:
    """The prompt hook body: sessions of `SESSION_PROMPTS` prompts, each
    prompt appended to the session's transcript before its clock starts."""
    from bettermemory import recall

    times: list[float] = []
    delivered = 0
    count = 0
    while count < n + WARM_UPS:
        place = b.place()
        session = b.session()
        t = build.Transcript(b.transcripts / f"{session}.jsonl", session, place.path)
        for i in range(SESSION_PROMPTS):
            text = world.prompt(b.rng, place.words)
            t.user(text, f"p{i}")
            t0 = time.perf_counter()
            result = recall.prompt_recall(
                prompt=text,
                session_id=session,
                harness="claude-code",
                prompt_id=f"p{i}",
                transcript_path=str(t.path),
                cwd=place.path,
                config=b.config,
                store=b.store,
                state=b.recall_state,
            )
            elapsed = time.perf_counter() - t0
            t.assistant(world.sentence(b.rng, place.words, 12))
            count += 1
            if count > WARM_UPS:
                times.append(elapsed)
                delivered += bool(result.ids)
            if count >= n + WARM_UPS:
                break
    out = world.summary(times)
    out["delivered"] = delivered
    return out


def arm_search(b: Bench, n: int) -> dict[str, Any]:
    """memory_search through the tool, from a checkout drawn by weight."""
    from bettermemory.builder import build_server
    from bettermemory.session import SessionState

    server = build_server(config=b.config, store=b.store, state=SessionState())
    hits = []

    def call() -> None:
        place = b.place()
        os.chdir(place.path)
        result = asyncio.run(
            build._call(
                server, "memory_search", {"query": world.query(b.rng, place.words)}
            )
        )
        hits.append(len(result) if isinstance(result, list) else 0)

    out = world.summary(_timed(n, call))
    out["hits_mean"] = round(sum(hits) / max(1, len(hits)), 2)
    return out


def arm_write(b: Bench, n: int) -> dict[str, Any]:
    """memory_write through the tool: the first call (the gates and the
    review it issues) and the whole write with the review answered."""
    from bettermemory.builder import build_server
    from bettermemory.session import SessionState

    server = build_server(config=b.config, store=b.store, state=SessionState())
    first: list[float] = []
    whole: list[float] = []
    statuses: Counter[str] = Counter()
    calls: Counter[int] = Counter()
    for i in range(n + WARM_UPS):
        place = b.place()
        os.chdir(place.path)
        arguments = {
            "content": world.body(b.rng, place.words),
            "scopes": [f"projects:{place.name}"]
            if place.kind != "folder"
            else ["tools"],
        }
        t0 = time.perf_counter()
        response = asyncio.run(build._call(server, "memory_write", arguments))
        t1 = time.perf_counter()
        made = 1
        status = response.get("status") if isinstance(response, dict) else None
        if status == "review":
            status, more = asyncio.run(build.reviewed_write(server, arguments))
            made += more
        t2 = time.perf_counter()
        if i >= WARM_UPS:
            first.append(t1 - t0)
            whole.append(t2 - t0 - (0 if made == 1 else (t1 - t0)))
            statuses[str(status)] += 1
            calls[made] += 1
    return {
        "first_call": world.summary(first),
        "reviewed_write": world.summary(whole),
        "statuses": dict(statuses),
        "calls": {str(k): v for k, v in calls.items()},
    }


def arm_api_sessions(b: Bench, n: int) -> dict[str, Any]:
    """Whole sessions through the public API (`build.api_session`), as the
    calibration runs them: the public API's cost per session at this
    store's size, for the generation-rate estimate."""
    w = build.World(b.store, b.config, b.recall_state, b.transcripts)
    plan = build.SessionPlan()
    seconds = []
    turns = []
    for _ in range(n):
        place = b.place()
        ck = build.Checkout(
            0, place.path, 0, place.name, place.words, place.weight, None, place.kind
        )
        result = asyncio.run(build.api_session(w, ck, b.session(), b.rng, plan, []))
        seconds.append(result["seconds"])
        turns.append(result["turns"])
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    out = world.summary(seconds)
    out["turns_mean"] = round(sum(turns) / max(1, len(turns)), 2)
    out["per_turn_s"] = round(sum(seconds) / max(1, sum(turns)), 3)
    out["calls"] = {k: world.summary(v) for k, v in w.timings.items()}
    return out


# ---------------------------------------------------------------------------
# The whole-store commands
# ---------------------------------------------------------------------------


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "bettermemory", *args]


def _headroom(quiet: Quiet) -> tuple[float, float | None]:
    """The RSS cap for the next whole-store command: wait, out of the
    quiet budget, until the machine has `world.RSS_CAP_MB` and a GB to
    spare; then the cap is that, or what is free less a GB when the wait
    ran out (at least 1.5 GB). Returns the cap and what was available."""
    started = time.monotonic()
    available = world.available_mb()
    while (
        available is not None
        and available < world.RSS_CAP_MB + 1000
        and time.monotonic() - started < quiet.budget
    ):
        print(f"available {available:.0f} MB; waiting for memory", flush=True)
        time.sleep(60)
        available = world.available_mb()
    quiet.budget -= time.monotonic() - started
    if available is None:
        return world.RSS_CAP_MB, None
    return max(1500.0, min(world.RSS_CAP_MB, available - 1000)), available


def arm_rss(scratch: Path, env: dict[str, str], quiet: Quiet) -> dict[str, Any]:
    """Peak RSS and wall time of `log verify`, `backup create` and
    `backup verify` as the CLI, each in a child of its own, each under a
    cap the machine has room for (`_headroom`)."""
    out: dict[str, Any] = {}

    def measured(name: str, argv: list[str]) -> dict[str, Any]:
        cap, available = _headroom(quiet)
        run = world.run_measured(argv, env, cap_mb=cap)
        out[name] = {**_brief(run), "available_mb_at_start": available}
        return run

    measured("log_verify", _cli("log", "verify", "--json"))
    backups = scratch / "backups"
    measured("backup_create", _cli("backup", "create", "--out", str(backups), "--json"))
    bundles = (
        sorted(p for p in backups.iterdir() if p.is_dir()) if backups.exists() else []
    )
    if bundles:
        size = sum(f.stat().st_size for f in bundles[-1].rglob("*") if f.is_file())
        out["backup_create"]["bundle_mb"] = round(size / 1e6, 1)
        measured("backup_verify", _cli("backup", "verify", str(bundles[-1]), "--json"))
    shutil.rmtree(backups, ignore_errors=True)
    return out


def _brief(run: dict[str, Any]) -> dict[str, Any]:
    status = None
    with contextlib.suppress(ValueError, AttributeError):
        status = json.loads(run["stdout"]).get("status")
    return {
        "exit": run["exit"],
        "status": status,
        "wall_s": run["wall_s"],
        "peak_rss_mb": run["peak_rss_mb"],
        "rss_cap_mb": run["rss_cap_mb"],
        "capped": run["capped"],
        "gb_at_s": run["gb_at_s"],
        "stderr_tail": run["stderr_tail"][-300:],
    }


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def measure(
    scratch: Path, env: dict[str, str], *, n: int, seed: int, api_sessions: int
) -> dict[str, Any]:
    """Every latency arm on the store at `scratch`, each after the quiet
    gate and with the load averages at its start and end, then the
    whole-store commands."""
    b = Bench(scratch, seed)
    quiet = Quiet()
    record: dict[str, Any] = {"census": build.census(b.store), "arms": {}, "loads": {}}
    os.chdir(scratch / "home")
    plan: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("resolution_hook", lambda: arm_resolution(b, n)),
        ("hook_session_start", lambda: arm_session_start(b, n, probed=False)),
        ("brief_probed", lambda: arm_session_start(b, n, probed=True)),
        ("hook_prompt", lambda: arm_prompt(b, round(n * 1.5))),
        ("memory_search", lambda: arm_search(b, n)),
        ("memory_write", lambda: arm_write(b, max(10, n // 2))),
        (
            "resolution_first_sight",
            lambda: arm_first_sight(b, max(8, n // 4), commit=True),
        ),
        (
            "resolution_first_sight_commitless",
            lambda: arm_first_sight(b, max(5, n // 6), commit=False),
        ),
    ]
    if api_sessions:
        plan.append(("api_sessions", lambda: arm_api_sessions(b, api_sessions)))
    for name, run in plan:
        quiet.wait(name)
        print(f"measure: {name}", flush=True)
        with world.timed_arm(record["loads"], name):
            record["arms"][name] = run()
        print(f"  {json.dumps(record['arms'][name])[:300]}", flush=True)
    b.store.close()
    quiet.wait("rss")
    with world.timed_arm(record["loads"], "rss"):
        record["rss"] = arm_rss(scratch, env, quiet)
    record["quiet"] = quiet.quiet
    record["quiet_waits"] = quiet.waits
    return record


# ---------------------------------------------------------------------------
# contend: four tool workers, four hook workers and the CLI at once
# ---------------------------------------------------------------------------


class Waits:
    """Every write transaction's wait for the lock (`Store._transaction`'s
    entry, which is its ``BEGIN IMMEDIATE``), by role, and those that
    failed."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[tuple[str, float, str]] = []
        self.local = threading.local()

    def add(self, wait: float, outcome: str) -> None:
        role = getattr(self.local, "role", "main")
        with self.lock:
            self.rows.append((role, wait, outcome))

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        roles = sorted({r for r, _, _ in self.rows})
        for role in [*roles, "all"]:
            mine = [w for r, w, o in self.rows if role in (r, "all")]
            failed = Counter(
                o for r, _, o in self.rows if role in (r, "all") and o != "ok"
            )
            entry = world.summary(mine)
            entry["failed"] = dict(failed)
            entry["over_1s"] = sum(1 for w in mine if w > 1.0)
            out[role] = entry
        return out


@contextlib.contextmanager
def instrumented(waits: Waits) -> Iterator[None]:
    """`Store._transaction` timed from its entry to its yield: the wait
    for the write lock, with the outcome of a ``BEGIN`` that failed."""
    from bettermemory.store import Store

    real = Store._transaction

    @contextlib.contextmanager
    def timed(self: Any) -> Iterator[sqlite3.Connection]:
        if self._tx_depth:
            with real(self) as conn:
                yield conn
            return
        t0 = time.perf_counter()
        with contextlib.ExitStack() as stack:
            try:
                conn = stack.enter_context(real(self))
            except BaseException as exc:
                text = str(exc).lower()
                outcome = (
                    "busy"
                    if ("locked" in text or "busy" in text)
                    else type(exc).__name__
                )
                waits.add(time.perf_counter() - t0, outcome)
                raise
            waits.add(time.perf_counter() - t0, "ok")
            yield conn

    Store._transaction = timed  # type: ignore[method-assign]
    try:
        yield
    finally:
        Store._transaction = real  # type: ignore[method-assign]


class _Warnings(logging.Handler):
    """The bettermemory warnings a run logs, counted by their first words:
    a best-effort row that was not recorded is one."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen: Counter[str] = Counter()
        self.samples: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        key = " ".join(text.split()[:3])
        self.seen[key] += 1
        if len(self.samples) < 12:
            self.samples.append(text[:240])


def contend(
    scratch: Path, env: dict[str, str], *, seconds: float, seed: int
) -> dict[str, Any]:
    """Four hook workers and four tool workers in this process, each on a
    connection of its own as the daemon's workers are (at the daemon's
    busy timeouts), and the CLI in a child process, for `seconds`."""
    from bettermemory.store import HOOK_BUSY_TIMEOUT_SECONDS, TOOL_BUSY_TIMEOUT_SECONDS

    b = Bench(scratch, seed)
    quiet = Quiet()
    quiet.wait("contend")
    waits = Waits()
    warnings = _Warnings()
    logging.getLogger("bettermemory").addHandler(warnings)
    calls: dict[str, list[float]] = {}
    errors: Counter[str] = Counter()
    lock = threading.Lock()
    stop = threading.Event()
    record: dict[str, Any] = {"census": build.census(b.store), "loads": {}}
    # The tools resolve their caller at the process's directory: the
    # most used checkout.
    home_place = max(b.places, key=lambda p: p.weight)
    os.chdir(home_place.path)

    def note(name: str, elapsed: float) -> None:
        with lock:
            calls.setdefault(name, []).append(elapsed)

    def fail(name: str, exc: BaseException) -> None:
        text = str(exc).lower()
        kind = "busy" if ("locked" in text or "busy" in text) else type(exc).__name__
        with lock:
            errors[f"{name}:{kind}"] += 1

    def hook_worker(index: int) -> None:
        waits.local.role = "hook"
        store = b.store.reopen(busy_timeout=HOOK_BUSY_TIMEOUT_SECONDS)
        rng = random.Random(seed * 100 + index)
        try:
            _hook_loop(b, store, rng, stop, note, fail)
        finally:
            store.close()

    def tool_worker(index: int) -> None:
        waits.local.role = "tool"
        store = b.store.reopen(busy_timeout=TOOL_BUSY_TIMEOUT_SECONDS)
        rng = random.Random(seed * 1000 + index)
        try:
            asyncio.run(_tool_loop(b, store, rng, stop, note, fail, home_place))
        finally:
            store.close()

    threads = [
        threading.Thread(target=hook_worker, args=(i,), daemon=True) for i in range(4)
    ]
    threads += [
        threading.Thread(target=tool_worker, args=(i,), daemon=True) for i in range(4)
    ]
    cli_out = scratch / "cli-waits.json"
    cli_out.unlink(missing_ok=True)
    with instrumented(waits), world.timed_arm(record["loads"], "contend"):
        cli = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "cli_timed.py"),
                "--seconds",
                str(seconds),
                "--out",
                str(cli_out),
                "--backups",
                str(scratch / "backups-contend"),
            ],
            env=env,
            cwd=str(scratch / "home"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in threads:
            t.join(timeout=120)
        cli_text, _ = cli.communicate(timeout=600)
    logging.getLogger("bettermemory").removeHandler(warnings)
    b.store.close()
    shutil.rmtree(scratch / "backups-contend", ignore_errors=True)
    cli_report = (
        json.loads(cli_out.read_text(encoding="utf-8")) if cli_out.exists() else {}
    )
    record.update(
        {
            "seconds": seconds,
            "lock_waits": waits.report(),
            "cli": cli_report,
            "cli_tail": cli_text[-600:],
            "calls": {k: world.summary(v) for k, v in sorted(calls.items())},
            "errors": dict(errors),
            "warnings": dict(warnings.seen),
            "warning_samples": warnings.samples,
            "threads_alive": sum(t.is_alive() for t in threads),
            "quiet": quiet.quiet,
            "quiet_waits": quiet.waits,
        }
    )
    return record


def _hook_loop(
    b: Bench,
    store: Any,
    rng: random.Random,
    stop: threading.Event,
    note: Callable[[str, float], None],
    fail: Callable[[str, BaseException], None],
) -> None:
    """One hook worker: sessions of a start, prompts and Stops, back to
    back, as a daemon's hook pool sees a busy fleet's."""
    from bettermemory import hook, recall

    while not stop.is_set():
        place = rng.choices(b.places, weights=[p.weight for p in b.places])[0]
        session = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        t = build.Transcript(b.transcripts / f"c-{session}.jsonl", session, place.path)
        t0 = time.perf_counter()
        try:
            recall.session_brief(
                session_id=session,
                source="startup",
                harness="claude-code",
                transcript_path=str(t.path),
                cwd=place.path,
                config=b.config,
                store=store,
                state=b.recall_state,
            )
            note("hook_session_start", time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001 - counted
            fail("hook_session_start", exc)
        for i in range(rng.randint(2, 6)):
            if stop.is_set():
                return
            text = world.prompt(rng, place.words)
            t.user(text, f"p{i}")
            t0 = time.perf_counter()
            try:
                recall.prompt_recall(
                    prompt=text,
                    session_id=session,
                    harness="claude-code",
                    prompt_id=f"p{i}",
                    transcript_path=str(t.path),
                    cwd=place.path,
                    config=b.config,
                    store=store,
                    state=b.recall_state,
                )
                note("hook_prompt", time.perf_counter() - t0)
            except Exception as exc:  # noqa: BLE001
                fail("hook_prompt", exc)
            answer = world.sentence(rng, place.words, 12)
            t.assistant(answer)
            t0 = time.perf_counter()
            try:
                hook.audit_transcript(
                    transcript_path=t.path,
                    session_id=session,
                    config=b.config,
                    store=store,
                    cwd=place.path,
                    assistant_message=answer,
                    harness="claude-code",
                    prompt_id=f"p{i}",
                    state=b.recall_state,
                )
                note("hook_stop", time.perf_counter() - t0)
            except Exception as exc:  # noqa: BLE001
                fail("hook_stop", exc)


async def _tool_loop(
    b: Bench,
    store: Any,
    rng: random.Random,
    stop: threading.Event,
    note: Callable[[str, float], None],
    fail: Callable[[str, BaseException], None],
    place: Place,
) -> None:
    """One tool worker: writes answered through their review, searches,
    uses and episodes, back to back, on its own connection and server."""
    from bettermemory.builder import build_server
    from bettermemory.session import SessionState

    server = build_server(config=b.config, store=store, state=SessionState())
    known: list[str] = []
    while not stop.is_set():
        roll = rng.random()
        t0 = time.perf_counter()
        name = "?"
        try:
            if roll < 0.35:
                name = "memory_write"
                status, _ = await build.reviewed_write(
                    server,
                    {
                        "content": world.body(rng, place.words),
                        "scopes": [f"projects:{place.name}"],
                    },
                )
                if status in ("error", "None"):
                    raise RuntimeError(f"write ended {status}")
            elif roll < 0.75:
                name = "memory_search"
                hits = await build._call(
                    server, "memory_search", {"query": world.query(rng, place.words)}
                )
                if isinstance(hits, dict) and hits.get("status") == "error":
                    raise RuntimeError(str(hits.get("error")))
                known.extend(
                    h["id"] for h in hits if isinstance(h, dict) and "id" in h
                ) if isinstance(hits, list) else None
            elif roll < 0.9 and known:
                name = "memory_record_use"
                result = await build._call(
                    server,
                    "memory_record_use",
                    {"memory_ids": known[-2:], "outcome": "applied"},
                )
                if isinstance(result, dict) and result.get("status") == "error":
                    raise RuntimeError(str(result.get("error")))
            else:
                name = "episode"
                result = await build._call(
                    server,
                    "episode",
                    {
                        "action": "write",
                        "body": world.body(rng, place.words),
                        "takeaway": "noted",
                    },
                )
                if isinstance(result, dict) and result.get("status") == "error":
                    raise RuntimeError(str(result.get("error")))
            note(name, time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            fail(name, exc)


# ---------------------------------------------------------------------------
# members: ENG-159's one-pass members reading at 400 projects
# ---------------------------------------------------------------------------


def members(
    scratch: Path, *, commitless: int, unresolved: int, n: int, quiet: Quiet
) -> dict[str, Any]:
    """ENG-159's measurement, carried: a store of `commitless` commit-less
    repositories seen here and `unresolved` provisional projects of writes
    made where nothing could be read, a record each (the ENG-159 test's
    `_store_of`); then a commit-less first sight's content candidates,
    liveness held as in a resolution, and health, each timed with its
    store reads (`Store.membership_rows` calls) counted, a fresh model
    per sample. Also the whole first sight of a commit-less repository
    there (`resolve_caller`). Runs against whichever tree is on sys.path
    (`BENCH_SCALE_SRC`). The quiet gate is waited on after the store is
    built, before the first timed call."""
    from bettermemory import location
    from bettermemory.location import resolve_caller
    from bettermemory.membership import INDETERMINATE, Location, Membership

    store = build.open_store(scratch / "store")
    config = build.config_for(scratch / "store")
    started = time.perf_counter()
    repos = scratch / "repos"
    for i in range(commitless):
        path = world.make_repo(repos / f"p{i}" / f"proj{i}", remote=None, commit=False)
        resolve_caller(store, config, str(path))
    machine = location.process_machine().id
    model = Membership(store, machine)
    for i in range(unresolved):
        unread = Location(
            machine=model.machine,
            cwd=str(scratch / f"nowhere{i}"),
            state=INDETERMINATE,
            detail="synthetic",
            session=f"s{i}",
        )
        home = model.unresolved_home(unread, session=f"s{i}")
        store.write(
            content=f"note {i} about the {world.noun(random.Random(i))} job",
            scopes=["tools"],
            home=home,
        )
    build_s = time.perf_counter() - started
    projects = int(store.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])

    quiet.wait("members")
    loads: dict[str, Any] = {}
    with world.timed_arm(loads, "members"):
        timed = _members_timed(store, config, scratch / "repos", machine, n)
    store.close()
    return {
        "projects": projects,
        "commitless": commitless,
        "unresolved": unresolved,
        "build_s": round(build_s, 1),
        "loads": loads,
        **timed,
    }


def _members_timed(
    store: Any, config: Any, repos: Path, machine: str, n: int
) -> dict[str, Any]:
    """The timed half of `members`."""
    from bettermemory.location import capture, resolve_caller, to_location
    from bettermemory.membership import Membership
    from bettermemory.store import Store

    count = [0]
    real = Store.membership_rows

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        count[0] += 1
        return real(self, *args, **kwargs)

    candidates_s: list[float] = []
    candidates_reads: list[int] = []
    health_s: list[float] = []
    health_reads: list[int] = []
    Store.membership_rows = counted  # type: ignore[method-assign]
    try:
        for i in range(n + WARM_UPS):
            fresh = world.make_repo(
                repos / "new" / f"fresh{i}", remote=None, commit=False
            )
            m = Membership(store, machine)
            loc = to_location(
                capture(str(fresh), write_token=False), machine=m.machine, history=None
            )
            count[0] = 0
            t0 = time.perf_counter()
            with m.liveness_held():
                m._projects = None
                m._content_candidates(loc, None)
            elapsed = time.perf_counter() - t0
            if i >= WARM_UPS:
                candidates_s.append(elapsed)
                candidates_reads.append(count[0])
        for i in range(max(5, n // 3) + 1):
            m = Membership(store, machine)
            count[0] = 0
            t0 = time.perf_counter()
            m.health()
            elapsed = time.perf_counter() - t0
            if i:
                health_s.append(elapsed)
                health_reads.append(count[0])
    finally:
        Store.membership_rows = real  # type: ignore[method-assign]
    first_sight = []
    for i in range(max(5, n // 3) + 1):
        fresh = world.make_repo(
            repos / "sight" / f"fresh{i}", remote=None, commit=False
        )
        t0 = time.perf_counter()
        resolve_caller(store, config, str(fresh))
        if i:
            first_sight.append(time.perf_counter() - t0)
    return {
        "content_candidates": {
            **world.summary(candidates_s),
            "store_reads": sorted(set(candidates_reads)),
        },
        "health": {**world.summary(health_s), "store_reads": sorted(set(health_reads))},
        "first_sight_commitless": world.summary(first_sight),
    }
