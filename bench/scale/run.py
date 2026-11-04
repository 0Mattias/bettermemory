"""The scale bench (ENG-109): synthetic stores at 1x, 10x and 100x of the
live store's shape, what the hooks and tools cost on each, the peak RSS
of the whole-store commands, a concurrent-writer arm, and ENG-159's
one-pass membership reading at 400 projects.

Every subcommand runs on a scratch directory it is given (`--scratch`),
under which it keeps the store, its keys, its state, a scratch HOME and
the repositories the store's checkouts stand in (`world.scratch_env`);
nothing reads or writes the user's store, keys or daemon. The arms run
in this process, as the tests drive the store and the tools, and make no
TCP connection (`world.no_network`); the whole-store commands run as the
CLI, in a child with the same scratch environment.

    python bench/scale/run.py calibrate --scratch S/templates --sessions 80 \\
        --out S/templates.json
    python bench/scale/run.py generate --scratch S/x10 --scale 10 \\
        --templates S/templates.json --out S/x10-build.json
    python bench/scale/run.py measure --scratch S/x10 --out bench/scale/results/...
    python bench/scale/run.py contend --scratch S/x10 --out ...
    python bench/scale/run.py members --scratch S/m400 --out ...
    python bench/scale/run.py generation --templates S/templates.json \\
        --builds S/build-x1.json ... --measures R/...-x1-measure.json ... --out ...

`calibrate` builds a template store at 1x's topology with back-dated
records and runs sessions through the public API (build.py); its times
are the public API's generation rate. `generate` replays the templates
into a store at the scale asked, through the store's own write and sign
code in batched transactions, and runs `log verify` on the result.
`measure` times the hooks, the tools, the brief and membership
resolution in process, and the peak RSS of `log verify`, `backup create`
and `backup verify` as the CLI. `contend` runs four tool workers, four
hook workers and the CLI at once and reads every write transaction's
wait for the lock. `members` is ENG-159's arm; with ``BENCH_SCALE_SRC``
naming another tree's `src` (an archive of the commit before ENG-159's
fix) it reads that tree. `generation` writes the generation's reading
from the calibration, the builds and the measured API sessions.

The timed arms wait for a quiet machine first (`world.wait_quiet`: the
1-minute load under 4, for up to 90 minutes) and record the load averages
at each arm's start and end; a result is `"quiet": false` when the wait
ran out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world  # noqa: E402


def _calibrate(args: argparse.Namespace) -> None:
    import build

    world.scratch_env(args.scratch)
    with world.no_network() as attempts:
        out = build.calibrate(
            args.scratch.resolve(),
            sessions=args.sessions,
            seed=args.seed,
            plan=build.SessionPlan(),
        )
    out["api"]["tcp_connects"] = attempts
    args.out.write_text(json.dumps(out), encoding="utf-8")
    print(json.dumps(out["api"], indent=1))


def _generate(args: argparse.Namespace) -> None:
    import build

    env = world.scratch_env(args.scratch)
    with world.no_network() as attempts:
        out = build.generate(
            args.scratch.resolve(),
            scale=args.scale,
            templates_file=args.templates.resolve(),
            seed=args.seed,
        )
    out["tcp_connects"] = attempts
    if not args.no_verify:
        out["log_verify"] = verify(env)
    world.write_json(args.out, {**world.stamp(), "build": out})
    print(json.dumps({k: v for k, v in out.items() if k != "census"}, indent=1))
    print(
        json.dumps(
            {k: v for k, v in out["census"].items() if k != "log_kinds"}, indent=1
        )
    )


def _generation(args: argparse.Namespace) -> None:
    """The generation's own reading: the public API's rate from the
    calibration and, per scale, the replay's, with how long the public API
    would have taken to write the same sessions (the sessions times the
    mean of the API's cost per session at 1x and at the scale, measured
    by `measure`'s api_sessions arm, as the store grows from one to the
    other)."""
    calibration = json.loads(args.templates.read_text(encoding="utf-8"))["api"]
    per_session_1x = calibration["seconds"] / calibration["sessions"]
    scales: dict[str, Any] = {}
    for build_file, measure_file in zip(args.builds, args.measures):
        built = json.loads(build_file.read_text(encoding="utf-8"))
        measured = json.loads(measure_file.read_text(encoding="utf-8"))
        b = built["build"]
        api = measured["measure"]["arms"].get("api_sessions", {})
        at_scale = (api.get("mean_ms") or per_session_1x * 1000) / 1000
        sessions = b["planned"]["sessions"]
        hours = sessions * (per_session_1x + at_scale) / 2 / 3600
        scales[f"{b['scale']:g}x"] = {
            "planned": b["planned"],
            "census": {k: v for k, v in b["census"].items() if k != "log_kinds"},
            "log_kinds": b["census"]["log_kinds"],
            "replay": {
                "seconds": b["seconds"],
                "rows_per_second": b["rows_per_second"],
                "peak_rss_mb": b["peak_rss_mb"],
                "counts": b["replay_counts"],
                "tcp_connects": b["tcp_connects"],
            },
            "log_verify": b.get("log_verify")
            or measured["measure"]["rss"].get("log_verify"),
            "api_session_s_at_scale": round(at_scale, 3),
            "api_estimate_hours": round(hours, 2),
            "build_commit": built["commit"],
        }
    out = {
        **world.stamp(),
        "live_shape": world.LIVE_SHAPE,
        "calibration": {
            **calibration,
            "per_session_s": round(per_session_1x, 3),
        },
        "scales": scales,
    }
    world.write_json(args.out, out)
    print(json.dumps({k: v["api_estimate_hours"] for k, v in scales.items()}))


def verify(env: dict[str, str]) -> dict[str, Any]:
    """`bettermemory log verify --json` on the scratch store, as the CLI,
    with its peak RSS."""
    run = world.run_measured(
        [sys.executable, "-m", "bettermemory", "log", "verify", "--json"], env
    )
    status = None
    try:
        status = json.loads(run["stdout"]).get("status")
    except ValueError:
        pass
    return {
        "exit": run["exit"],
        "status": status,
        "wall_s": run["wall_s"],
        "peak_rss_mb": run["peak_rss_mb"],
        "capped": run["capped"],
        "stderr_tail": run["stderr_tail"],
    }


def _measure(args: argparse.Namespace) -> None:
    env = world.scratch_env(args.scratch)
    import arms

    with world.no_network() as attempts:
        out = arms.measure(
            args.scratch.resolve(),
            env,
            n=args.n,
            seed=args.seed,
            api_sessions=args.api_sessions,
        )
    out["tcp_connects"] = attempts
    out["scale"] = args.scale
    world.write_json(args.out, {**world.stamp(), "measure": out})
    print(json.dumps({k: out[k] for k in ("quiet", "rss")}, indent=1))


def _contend(args: argparse.Namespace) -> None:
    env = world.scratch_env(args.scratch)
    import arms

    with world.no_network() as attempts:
        out = arms.contend(
            args.scratch.resolve(), env, seconds=args.seconds, seed=args.seed
        )
    out["tcp_connects"] = attempts
    out["scale"] = args.scale
    world.write_json(args.out, {**world.stamp(), "contend": out})
    print(json.dumps({k: out[k] for k in ("lock_waits", "errors", "quiet")}, indent=1))


def _members(args: argparse.Namespace) -> None:
    world.scratch_env(args.scratch)
    import arms

    quiet = arms.Quiet()
    with world.no_network() as attempts:
        out = arms.members(
            args.scratch.resolve(),
            commitless=args.commitless,
            unresolved=args.unresolved,
            n=args.n,
            quiet=quiet,
        )
    out.update(
        {
            "tcp_connects": attempts,
            "quiet": quiet.quiet,
            "quiet_waits": quiet.waits,
            "tree": args.tree,
        }
    )
    world.write_json(args.out, {**world.stamp(), "members": out})
    print(json.dumps(out, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    cal = sub.add_parser("calibrate", help="template sessions through the public API")
    cal.add_argument("--scratch", type=Path, required=True)
    cal.add_argument("--sessions", type=int, default=80)
    cal.add_argument("--seed", type=int, default=20261104)
    cal.add_argument("--out", type=Path, required=True)
    gen = sub.add_parser("generate", help="a store at a scale, from the templates")
    gen.add_argument("--scratch", type=Path, required=True)
    gen.add_argument("--scale", type=float, required=True)
    gen.add_argument("--templates", type=Path, required=True)
    gen.add_argument("--seed", type=int, default=109)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--no-verify", action="store_true")
    mea = sub.add_parser("measure", help="the latency and RSS arms on a built store")
    mea.add_argument("--scratch", type=Path, required=True)
    mea.add_argument("--scale", type=float, required=True)
    mea.add_argument("--n", type=int, default=60)
    mea.add_argument("--api-sessions", type=int, default=5)
    mea.add_argument("--seed", type=int, default=7)
    mea.add_argument("--out", type=Path, required=True)
    con = sub.add_parser(
        "contend", help="four tool workers, four hook workers, the CLI"
    )
    con.add_argument("--scratch", type=Path, required=True)
    con.add_argument("--scale", type=float, required=True)
    con.add_argument("--seconds", type=float, default=90)
    con.add_argument("--seed", type=int, default=11)
    con.add_argument("--out", type=Path, required=True)
    mem = sub.add_parser("members", help="ENG-159's arm at 400 projects")
    mem.add_argument("--scratch", type=Path, required=True)
    mem.add_argument("--commitless", type=int, default=300)
    mem.add_argument("--unresolved", type=int, default=100)
    mem.add_argument("--n", type=int, default=30)
    mem.add_argument("--tree", default="HEAD")
    mem.add_argument("--out", type=Path, required=True)
    sm = sub.add_parser("generation", help="the generation's reading, from the runs")
    sm.add_argument("--templates", type=Path, required=True)
    sm.add_argument("--builds", type=Path, nargs="+", required=True)
    sm.add_argument("--measures", type=Path, nargs="+", required=True)
    sm.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    commands = {
        "generation": _generation,
        "calibrate": _calibrate,
        "generate": _generate,
        "measure": _measure,
        "contend": _contend,
        "members": _members,
    }
    commands[args.cmd](args)


if __name__ == "__main__":
    main()
