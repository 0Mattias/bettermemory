"""The session brief measured on the sealed copy (unit RS, RS-P1 and RS-P2).

The store is the sealed live copy migrated into a scratch directory
(`--v8`), keys and state under the same directory; nothing under the
user's directories is read or written but the checkouts' own git
repositories, which are read as the hook reads them (the caller's origin
and the drift legs). The checkouts are the seven repositories with the
most memories (a repository is its remote, or its worktree root when the
memory carries none); each is measured from the local worktree most of
its memories were written in, when that directory still exists. The
artifact names each checkout by rank only, with its row count and whether
it is this repository; no path, remote, id or text is written.

Two arms:

  in_process  `recall.session_brief` in this process with one
              `RecallState(background=True)`, the daemon's shape without
              the HTTP hop. Per checkout: the first call (a fresh state:
              no HEAD noted and an empty drift memo, the path a call after
              a head move takes), the background fill joined, then a warm
              call. Each call reports the pointers it showed, the brief's
              characters, its wall time and the git processes spawned
              during it (`subprocess.run` with argv[0] ``git``, counted);
              every line is checked to be exactly an id, its summary and
              its disputed mark (`recall._brief_line`); and the warm
              selection is compared with a full scan (every eligible row
              of the window resolved, the first eight that read fresh).
              Beside the warm call: whether the repository's signature
              and walk files read the same before the first call and
              after the fill, and how many of the window's rows the memo
              still cannot answer after the fill (a fill keeps nothing
              when its pass sees the repository move under it).
              A checkout with no local worktree is called with its origin
              alone (no directory: its HEAD cannot be read, so it is never
              warm).
  daemon      `/api/v1/hook/session-start` through a daemon started on the
              scratch store for each checkout (`bettermemory up --port 0`,
              so it never binds the configured port), from a warm client:
              the first call on the fresh daemon, then, once the fill has
              gone quiet (no git process for two seconds), N warm calls.
              Git processes are counted by a `git` on the daemon's PATH
              that appends a line to a file and runs the real one. The
              first call on a fresh daemon stands in for the first call
              after a head move: both find no HEAD noted for the checkout
              and no commit leg memoised for it, and the fresh daemon also
              pays the origin capture and its own warm-up, so it is the
              costlier of the two. Only checkouts with a local worktree.

    python bench/recall/brief.py --v8 ~/.cache/bettermemory-v9/live-copy \\
        --scratch ~/.cache/bettermemory-v9/rs-slice5/brief \\
        --out bench/recall/results/brief-2026-10-11.json
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHECKOUTS = 7
WARM_N = 20
# How long the daemon's fill must spawn no git process before the warm
# calls start, and the most the arm waits for that.
FILL_QUIET_SECONDS = 2.0
FILL_WAIT_SECONDS = 120.0


def _latency() -> ModuleType:
    """bench/daemon/latency.py, for its scratch-store helpers."""
    path = ROOT / "bench" / "daemon" / "latency.py"
    spec = importlib.util.spec_from_file_location("bench_daemon_latency", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_daemon_latency", module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The checkouts
# ---------------------------------------------------------------------------


def checkouts(store: Any, count: int = CHECKOUTS) -> list[dict[str, Any]]:
    """The `count` repositories with the most memories, most first: each
    with its row count, its repository key, the existing local worktree
    most of its rows were written in (or None) and the origin to call
    with."""
    from bettermemory.models import Origin

    rows: Counter[str] = Counter()
    worktrees: dict[str, Counter[str]] = {}
    repos: dict[str, str | None] = {}
    for memory in store.load_all():
        origin = memory.origin
        if origin is None:
            continue
        key = origin.repo or (
            f"wt:{origin.worktree_root}" if origin.worktree_root else None
        )
        if key is None:
            continue
        rows[key] += 1
        repos[key] = origin.repo
        if origin.worktree_root:
            worktrees.setdefault(key, Counter())[origin.worktree_root] += 1
    out: list[dict[str, Any]] = []
    for rank, (key, count_rows) in enumerate(rows.most_common(count)):
        local = next(
            (
                path
                for path, _ in worktrees.get(key, Counter()).most_common()
                if Path(path).is_dir()
            ),
            None,
        )
        recorded = next(iter(worktrees.get(key, Counter())), None)
        out.append(
            {
                "rank": rank,
                "rows": count_rows,
                "is_this_repository": "bettermemory" in key,
                "repoless": repos[key] is None,
                "local": local,
                "origin": Origin(
                    repo=repos[key], worktree_root=local or recorded, cwd=local
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# The in-process arm
# ---------------------------------------------------------------------------


@contextmanager
def count_git() -> Iterator[Callable[[], int]]:
    """Count the git processes `subprocess.run` spawns inside the block;
    yields a reader of the running count."""
    real = subprocess.run
    seen = [0]

    def counting(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "git":
            seen[0] += 1
        return real(*args, **kwargs)

    subprocess.run = counting  # type: ignore[assignment]
    try:
        yield lambda: seen[0]
    finally:
        subprocess.run = real  # type: ignore[assignment]


def full_scan(store: Any, caller: Any, *, stale_after_days: int) -> list[str]:
    """The ids a brief shows when every eligible row of the window is
    resolved: `_select_brief`'s eligibility, every verdict resolved, the
    first `BRIEF_MAX_POINTERS` that read fresh."""
    from bettermemory._response import ResponseBuilder
    from bettermemory.models import Category, utcnow
    from bettermemory.recall import (
        _DELIVERED_PROVENANCE,
        BRIEF_MAX_POINTERS,
        BRIEF_WINDOW,
        _contradiction_open,
        _fresh,
        in_project,
    )
    from bettermemory.verdict import resolve_verdicts

    ids = store.brief_rows(
        admit=lambda memory_origin: in_project(memory_origin, caller),
        provenance=_DELIVERED_PROVENANCE,
        exclude_category=Category.AMBIENT.value,
    )[:BRIEF_WINDOW]
    trust = store.trust_rows(ids) or {}
    ids = [
        i for i in ids if i in trust and trust[i].provenance in _DELIVERED_PROVENANCE
    ]
    contradicted = store.contradicted_at(ids)
    rows = [m for m in store.load_many(ids) if not _contradiction_open(m, contradicted)]
    responses = ResponseBuilder(stale_after_days=stale_after_days)
    verdicts = resolve_verdicts(
        rows, caller_origin=caller, now=utcnow(), stale_after_days=stale_after_days
    )
    fresh = [
        m.id for m, v in zip(rows, verdicts) if _fresh(m, v, trust[m.id], responses)
    ]
    return fresh[:BRIEF_MAX_POINTERS]


def _line_faults(store: Any, result: Any) -> int:
    """Lines of the brief that are not exactly ``- <id> <summary>`` and the
    disputed mark: the header plus one line per shown id, nothing else."""
    from bettermemory.recall import _brief_line, disputed_marks

    if not result.stdout:
        return 0
    lines = result.stdout.split("\n")[1:]
    marks = disputed_marks(store.pending_conflict_pairs())
    memories = {m.id: m for m in store.load_many(list(result.ids))}
    expected = [_brief_line(memories[i], marks) for i in result.ids if i in memories]
    return sum(1 for a, b in zip(lines, expected) if a != b) + abs(
        len(lines) - len(expected)
    )


def _call(
    store: Any, config: Any, state: Any, checkout: dict[str, Any], session: str
) -> dict[str, Any]:
    from bettermemory.recall import session_brief

    local = checkout["local"]
    with count_git() as git:
        started = time.perf_counter()
        result = session_brief(
            session_id=session,
            source="startup",
            cwd=Path(local) if local else None,
            origin=None if local else checkout["origin"],
            config=config,
            store=store,
            state=state,
        )
        seconds = time.perf_counter() - started
        spawned = git()
    return {
        "pointers": len(result.ids),
        "chars": result.chars,
        "ms": round(seconds * 1000.0, 1),
        "git_processes": spawned,
        "line_faults": _line_faults(store, result),
        **_recorded(store, session),
        "ids": list(result.ids),
    }


def _recorded(store: Any, session: str) -> dict[str, Any]:
    """What the brief's own `session_brief` event recorded for `session`:
    the rows it examined and the rows it left to the fill."""
    from datetime import timedelta

    from bettermemory.models import utcnow

    for event in store.events_of_kinds(
        ("session_brief",), since=utcnow() - timedelta(hours=1)
    ):
        if event.get("session_id") == session:
            return {
                "rows_examined": event.get("rows_examined"),
                "unresolved": event.get("unresolved"),
            }
    return {"rows_examined": None, "unresolved": None}


def in_process(scratch: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from bettermemory.config import Config, StorageConfig
    from bettermemory.recall import RecallState, _origin_for
    from bettermemory.store import Store

    os.environ["BETTERMEMORY_KEYS_DIR"] = str(scratch / "keys")
    config = Config(storage=StorageConfig(directory=str(scratch / "store")))
    store = Store(scratch / "store")
    out: list[dict[str, Any]] = []
    for checkout in rows:
        state = RecallState(background=True)
        tag = f"bench-brief-{checkout['rank']}"
        signature = _signature(checkout["local"])
        first = _call(store, config, state, checkout, f"{tag}-first")
        filled = state.wait_for_fills(timeout=FILL_WAIT_SECONDS)
        held = signature == _signature(checkout["local"])
        left = _unresolved_now(store, checkout, config)
        warm = _call(store, config, state, checkout, f"{tag}-warm")
        caller = (
            _origin_for(Path(checkout["local"]))
            if checkout["local"]
            else checkout["origin"]
        )
        scan = full_scan(
            store, caller, stale_after_days=config.behavior.verification_stale_days
        )
        out.append(
            {
                **_public(checkout),
                "first_call": _strip(first),
                "fill_finished": filled,
                "repository_signature_held": held,
                "memo_unresolved_after_fill": left,
                "warm": _strip(warm),
                "full_scan_pointers": len(scan),
                "warm_equals_full_scan": warm["ids"] == scan,
                "first_call_within_full_scan": all(i in scan for i in first["ids"])
                or len(first["ids"]) == 0,
            }
        )
        print(f"in-process {out[-1]}", file=sys.stderr)
    store.close()
    return out


def _unresolved_now(store: Any, checkout: dict[str, Any], config: Any) -> int:
    """How many of the window's eligible rows the drift memo cannot answer
    now (`memoised_verdicts`): zero once a fill has kept everything it
    resolved."""
    from bettermemory.models import Category, utcnow
    from bettermemory.recall import (
        _DELIVERED_PROVENANCE,
        BRIEF_WINDOW,
        _origin_for,
        in_project,
    )
    from bettermemory.verdict import UnresolvedVerdict, memoised_verdicts

    local = checkout["local"]
    caller = _origin_for(Path(local)) if local else checkout["origin"]
    ids = store.brief_rows(
        admit=lambda memory_origin: in_project(memory_origin, caller),
        provenance=_DELIVERED_PROVENANCE,
        exclude_category=Category.AMBIENT.value,
    )[:BRIEF_WINDOW]
    verdicts = memoised_verdicts(
        store.load_many(ids),
        caller_origin=caller,
        now=utcnow(),
        stale_after_days=config.behavior.verification_stale_days,
    )
    return sum(1 for v in verdicts if isinstance(v, UnresolvedVerdict))


def _signature(local: str | None) -> Any:
    """The repository's `githead.signature` and its walk files' stamps
    (`walk_files_signature`), which a fill's pass must find unchanged at
    its end to keep what it resolved; None without a local worktree."""
    if local is None:
        return None
    from bettermemory import githead
    from bettermemory.origin import toplevel_and_head_from_files, walk_files_signature

    found = toplevel_and_head_from_files(Path(local))
    walk = walk_files_signature(found[0], directories={}) if found else None
    return (githead.signature(Path(local)), walk)


def _public(checkout: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": checkout["rank"],
        "rows": checkout["rows"],
        "is_this_repository": checkout["is_this_repository"],
        "repoless": checkout["repoless"],
        "local_worktree": checkout["local"] is not None,
    }


def _strip(call: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in call.items() if k != "ids"}


# ---------------------------------------------------------------------------
# The daemon arm
# ---------------------------------------------------------------------------


def git_shim(directory: Path, log: Path) -> Path:
    """A directory holding a `git` that appends a line to `log` and runs
    the real git, for the front of a daemon's PATH."""
    real = shutil.which("git")
    if real is None:
        raise SystemExit("no git on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "git"
    shim.write_text(f'#!/bin/sh\necho x >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    return directory


def _lines(path: Path) -> int:
    try:
        return len(path.read_bytes().splitlines())
    except FileNotFoundError:
        return 0


def daemon(
    scratch: Path, rows: list[dict[str, Any]], *, n: int, latency: ModuleType
) -> list[dict[str, Any]]:
    from bettermemory._daemon_client import post, read_state

    log = scratch / "git-calls.log"
    env = latency._env(scratch)
    env["PATH"] = (
        str(git_shim(scratch / "git-shim", log)) + os.pathsep + env.get("PATH", "")
    )
    out: list[dict[str, Any]] = []
    for checkout in rows:
        if checkout["local"] is None:
            continue
        latency._cli(["down"], env)
        up = latency._cli(["up", "--port", "0"], env)
        if up.returncode != 0:
            raise SystemExit(f"daemon did not start: {up.stderr[-500:]}")
        state = read_state(scratch / "state", scratch / "store" / "memory.sqlite")
        if state is None:
            raise SystemExit("no daemon state after `up`")
        tag = f"bench-brief-daemon-{checkout['rank']}"

        def call(session: str) -> tuple[float, dict[str, Any]]:
            started = time.perf_counter()
            reply = post(
                state["port"],
                state["token"],
                "/api/v1/hook/session-start",
                {"cwd": checkout["local"], "session_id": session, "source": "startup"},
                timeout=60,
            )
            return time.perf_counter() - started, reply

        before = _lines(log)
        first_s, first = call(f"{tag}-first")
        first_git = _lines(log) - before
        quiet_since, last, waited = time.monotonic(), _lines(log), 0.0
        deadline = time.monotonic() + FILL_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.25)
            now_lines = _lines(log)
            if now_lines != last:
                last, quiet_since = now_lines, time.monotonic()
            elif time.monotonic() - quiet_since >= FILL_QUIET_SECONDS:
                break
        waited = round(time.monotonic() - (deadline - FILL_WAIT_SECONDS), 1)
        before = _lines(log)
        warm_times: list[float] = []
        pointers: Counter[int] = Counter()
        for index in range(n):
            seconds, reply = call(f"{tag}-warm-{index}")
            warm_times.append(seconds)
            pointers[len(reply.get("ids") or [])] += 1
        out.append(
            {
                **_public(checkout),
                "first_call_ms": round(first_s * 1000.0, 1),
                "first_call_pointers": len(first.get("ids") or []),
                "first_call_git_processes": first_git,
                "fill_wait_seconds": waited,
                "warm": latency._summary(warm_times),
                "warm_git_processes": _lines(log) - before,
                "warm_pointers": dict(pointers),
            }
        )
        print(f"daemon {out[-1]}", file=sys.stderr)
        latency._cli(["down"], env)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--v8", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, default=None)
    parser.add_argument("--n", type=int, default=WARM_N, help="warm daemon calls")
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=("in_process", "daemon"),
        default=["in_process", "daemon"],
    )
    parser.add_argument("--note", default=None)
    args = parser.parse_args(argv)

    latency = _latency()
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="bettermemory-brief-bench-"))
    latency.refuse_live_store(scratch)
    if (scratch / "store").exists():
        raise SystemExit(f"{scratch} already holds a store; give a fresh --scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    env = latency._env(scratch)
    for name in ("BETTERMEMORY_DIR", "BETTERMEMORY_KEYS_DIR", "BETTERMEMORY_STATE_DIR"):
        os.environ[name] = env[name]
    report = latency.migrate(args.v8.expanduser(), scratch, env)
    print(f"migrated in {report['seconds']} s", file=sys.stderr)

    from bettermemory import __version__
    from bettermemory.store import Store

    store = Store(scratch / "store")
    rows = checkouts(store)
    store.close()
    results: dict[str, Any] = {}
    if "in_process" in args.arms:
        results["in_process"] = in_process(scratch, rows)
    if "daemon" in args.arms:
        results["daemon"] = daemon(scratch, rows, n=args.n, latency=latency)
    artifact = {
        "kind": "recall-brief",
        "version": __version__,
        "commit": latency._git("rev-parse", "HEAD"),
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "store": {
            "source": "the sealed live copy, migrated into a scratch store",
            "memories_imported": report.get("memories", {}),
        },
        "method": {
            "checkouts": f"the {CHECKOUTS} repositories with the most memories",
            "warm_n": args.n,
            "fill_quiet_seconds": FILL_QUIET_SECONDS,
            "first_call": (
                "a fresh RecallState (in process) or a fresh daemon: no HEAD "
                "noted, no commit leg memoised; stands in for the first call "
                "after a head move"
            ),
        },
        "note": args.note,
        "results": results,
    }
    latency.write_artifact(args.out, artifact)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
