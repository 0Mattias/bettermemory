"""The CLI of the scale bench's contention arm (ENG-109): one process that
runs the CLI's commands back to back for a span, as an owner at a
terminal would beside a busy daemon, and reads every write transaction's
wait for the lock as `arms.instrumented` does in the workers.

The commands, in turn: `rename-scope` of a scope and back (a writer over
every record holding it, in chunks), `backup create` (an online snapshot)
and `stats`. Each runs through `bettermemory.cli.main`, its exit code and
wall time kept. Run by `arms.contend` with the bench's scratch
environment, never on its own against a real store.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backups", type=Path, required=True)
    args = parser.parse_args()
    import arms
    from bettermemory.cli import main as cli_main

    waits = arms.Waits()
    waits.local.role = "cli"
    commands = [
        ["rename-scope", "tools", "tooling", "--json"],
        ["backup", "create", "--out", str(args.backups), "--json"],
        ["rename-scope", "tooling", "tools", "--json"],
        ["stats", "--json"],
    ]
    runs: list[dict[str, Any]] = []
    deadline = time.monotonic() + args.seconds
    with arms.instrumented(waits):
        index = 0
        while time.monotonic() < deadline:
            argv = commands[index % len(commands)]
            index += 1
            sys.argv = ["bettermemory", *argv]
            code: Any = 0
            t0 = time.perf_counter()
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                try:
                    cli_main()
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # noqa: BLE001 - recorded
                    code = f"{type(exc).__name__}: {exc}"[:200]
            runs.append(
                {
                    "command": " ".join(argv[:2]),
                    "exit": code,
                    "wall_s": round(time.perf_counter() - t0, 3),
                    "tail": sink.getvalue()[-200:] if code not in (0, None) else "",
                }
            )
    by: dict[str, list[float]] = {}
    for run in runs:
        by.setdefault(run["command"], []).append(run["wall_s"])
    report = {
        "runs": len(runs),
        "exits": sorted({str(r["exit"]) for r in runs}),
        "failed": [r for r in runs if r["exit"] not in (0, None)][:8],
        "wall": {k: world.summary(v) for k, v in by.items()},
        "lock_waits": waits.report(),
    }
    args.out.write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main()
