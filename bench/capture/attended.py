"""CAPS-P5: no capture request unless the hook says attended "1".

A scratch store (the v8 test fixture, migrated) with `[capture]
stop_checkpoint = true`, served by a daemon started as `up --foreground`
from an environment that carries CLAUDE_CODE_SESSION_ATTENDED=1, so the
daemon's own environment says attended. Each case then runs the real hook
client, `python -m bettermemory hook stop` (`_daemon_client.hook_main`),
with the Stop payload on stdin, for its own session over a transcript far
past the checkpoint, and counts the capture_open rows the store holds for
that session. The cases vary only what the client sees: the variable
absent, "0", empty or "true", and a payload that itself claims attended
"1" with the variable absent or "0". Prediction: 0 opens in every case;
MISSED at 1. A last control run with the variable "1" must open one batch,
so a 0 above is the gate and not a broken setup. The daemon's pid is
checked before and after, so every case reached the attended-env daemon.

    python bench/capture/attended.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bench/: scratch_store
from scratch_store import scratch_store  # noqa: E402

FIXTURE = _common.ROOT / "tests" / "fixtures" / "v8" / "store"
TRANSCRIPT_BYTES = 1024 * 1024
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"

#: (case, the variable's value or None for absent, the payload's own claim).
CASES: tuple[tuple[str, str | None, str | None], ...] = (
    ("env_absent", None, None),
    ("env_0", "0", None),
    ("env_empty", "", None),
    ("env_true", "true", None),
    ("env_absent_payload_claims_1", None, "1"),
    ("env_0_payload_claims_1", "0", "1"),
)


def _hook(session: str, transcript: Path, env_value: str | None, claim: str | None):
    env = dict(os.environ)
    env.pop(ATTENDED_ENV, None)
    if env_value is not None:
        env[ATTENDED_ENV] = env_value
    env["CLAUDE_CODE_SESSION_ID"] = session
    payload: dict[str, Any] = {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(_common.ROOT),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    if claim is not None:
        payload["attended"] = claim
    return subprocess.run(
        [_common.PYTHON, "-m", "bettermemory", "hook", "stop"],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _opens(store_dir: Path) -> dict[str, int]:
    from bettermemory import capture

    store = scratch_store(store_dir)
    try:
        out: dict[str, int] = {}
        for batch in capture.batches(store):
            out[batch.session_id] = out.get(batch.session_id, 0) + 1
        return out
    finally:
        store.close()


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="caps-attended-")).resolve()
    daemon: subprocess.Popen[bytes] | None = None
    pids: dict[str, int | None] = {}
    rows: list[dict[str, Any]] = []
    try:
        _common.scratch_env(scratch)
        from latency import import_copy, stop_transcript  # type: ignore[import-not-found]

        from bettermemory._daemon_client import read_state

        report = import_copy(FIXTURE, scratch / "store")
        _common.write_config("[capture]\nstop_checkpoint = true\ndaily_cap = 100000\n")
        tdir = scratch / "transcripts"
        tdir.mkdir()
        big = tdir / "big.jsonl"
        shape = stop_transcript(big, TRANSCRIPT_BYTES)

        daemon_env = dict(os.environ)
        daemon_env[ATTENDED_ENV] = "1"
        log = (scratch / "daemon.log").open("wb")
        daemon = subprocess.Popen(
            [_common.PYTHON, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
            env=daemon_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        store_file = scratch / "store" / "memory.sqlite"
        state = None
        for _ in range(200):
            state = read_state(scratch / "state", store_file)
            if state is not None:
                break
            time.sleep(0.05)
        if state is None:
            raise SystemExit("the attended-env daemon did not write its state")
        pids["started"] = daemon.pid
        pids["state_before"] = state.get("pid")

        for index, (case, env_value, claim) in enumerate(
            [*CASES, ("control_env_1", "1", None)]
        ):
            session = f"a77e0ded-0000-4a5b-8c6d-{index:012d}"
            transcript = tdir / f"{session}.jsonl"
            os.link(big, transcript)
            proc = _hook(session, transcript, env_value, claim)
            opened = _opens(scratch / "store").get(session, 0)
            rows.append(
                {
                    "case": case,
                    "env": "absent" if env_value is None else env_value,
                    "payload_attended": claim,
                    "hook_exit": proc.returncode,
                    "hook_stdout": bool(proc.stdout.strip()),
                    "hook_capture_instruction": "additionalContext" in proc.stdout,
                    "hook_stderr": bool(proc.stderr.strip()),
                    "capture_open_rows": opened,
                }
            )
        after = read_state(scratch / "state", store_file) or {}
        pids["state_after"] = after.get("pid")
    finally:
        down = _common.cli("down") if "HOME" in os.environ else None
        if daemon is not None:
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                daemon.wait(timeout=5)
        pids["exit_code"] = daemon.returncode if daemon is not None else None
        pids["down_exit"] = down.returncode if down is not None else None
        shutil.rmtree(scratch, ignore_errors=True)

    tested = [r for r in rows if not r["case"].startswith("control")]
    control = [r for r in rows if r["case"].startswith("control")]
    total = sum(r["capture_open_rows"] for r in tested)
    same_daemon = pids.get("state_before") == pids.get("state_after") is not None
    doc = {
        "kind": "capture-attended",
        "prediction": "CAPS-P5",
        "commit": _common.git("rev-parse", "HEAD"),
        "run_at": _common.stamp(),
        "method": {
            "store": "tests/fixtures/v8/store migrated into a scratch store",
            "memories": report.get("memories"),
            "config": "[capture] stop_checkpoint = true, daily_cap = 100000",
            "daemon": "python -m bettermemory up --foreground --port 0, its "
            "environment carrying CLAUDE_CODE_SESSION_ATTENDED=1",
            "client": "python -m bettermemory hook stop (hook_main), Stop payload on stdin",
            "transcript": shape,
            "checkpoint_chars": 12_000 * 4,
        },
        "cases": rows,
        "daemon": {**pids, "same_daemon_throughout": same_daemon},
        "capture_open_rows_without_attended_1": total,
        "control_opened": sum(r["capture_open_rows"] for r in control),
        "verdict": "HIT" if total == 0 else "MISSED",
        "valid": bool(control) and control[0]["capture_open_rows"] == 1 and same_daemon,
        "scratch_removed": not scratch.exists(),
    }
    out = _common.write_result("attended.json", doc)
    print(f"wrote {out}")
    for row in rows:
        print(row)
    print(
        f"opens without attended 1: {total} -> {doc['verdict']}; control "
        f"{doc['control_opened']}; daemon {doc['daemon']}"
    )


if __name__ == "__main__":
    main()
