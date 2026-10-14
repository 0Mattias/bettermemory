"""Tool-schema context cost — what an MCP server charges you every turn.

WHY THIS IS THE FIRST COMPARATIVE NUMBER. It is the axis bettermemory
LOSES on, and publishing a loss first is the only way the wins that
follow get read. It is also the cheapest honest comparison available:
no corpus, no labels, no judge, no API key, no network. Two processes,
one JSON-RPC round trip each, a byte count.

WHAT IS COUNTED. Every MCP server advertises its tools in a `tools/list`
response, and a client pastes that schema into the model's context on
every single turn whether or not a tool is ever called. So the honest
unit is the full serialized `tools` array — names, descriptions AND
inputSchema — not the description subset. bettermemory's own docs have
quoted the name+description subset, which understates the real cost;
this measures both and reports the full figure as the headline.

FAIRNESS RULES, because the number is worthless if it can be waved away:
- Both servers are measured through the same code path, at their own
  DEFAULT configuration. Turning off another project's features to
  improve its score, or ours to improve ours, would be rigging.
- Every server is spawned with HOME redirected to a throwaway directory,
  so it starts at its SHIPPED DEFAULT rather than at whatever the person
  running the benchmark happens to have configured. This is not a
  detail: the first run of this harness reported 27 tools for
  bettermemory instead of 18, because it read the author's own
  `full_tool_surface = true`.
- Serialization is `json.dumps(tools, sort_keys=True, separators=(",",":"))`
  for every server, so no formatting difference can move the result.
- Byte counts are UTF-8. Character counts are reported alongside because
  the project's existing CI budget is expressed in characters.

Usage:

    venv/bin/python bench/toolcost/run.py                  # bettermemory only
    venv/bin/python bench/toolcost/run.py --spec specs.json --json
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

_PROTOCOL = "2025-06-18"
#: The prefix of every variable bettermemory reads; none is inherited.
_OWN_ENV_PREFIX = "BETTERMEMORY_"


class ProbeResult(NamedTuple):
    """What one server put on the wire: its `tools/list` array and the
    `instructions` string its `initialize` reply carried (empty when it
    carried none)."""

    tools: list[dict[str, Any]]
    instructions: str


def probe_tools(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 90.0,
) -> ProbeResult:
    """Spawn an MCP server over stdio and return its advertised tools and
    server instructions.

    Hand-rolled JSON-RPC rather than an SDK client: the point is to
    measure exactly the bytes a server puts on the wire, and a client
    library that normalises or re-serialises the payload would put its own
    formatting between us and the number.
    """
    # HOME is redirected to a throwaway directory so `platformdirs`
    # resolves a FRESH config, and the server starts at its SHIPPED
    # DEFAULT. Without this the probe silently measures whoever happens
    # to be running it — the first run of this harness reported 27 tools
    # instead of 18 because it picked up the author's own
    # `full_tool_surface = true`. A benchmark that reads the operator's
    # config is measuring the operator.
    #
    # Everything after the mkdtemp sits inside the try, so the sandbox
    # goes even when the spawn itself fails (a missing binary used to
    # leave one empty `toolcost-home-*` directory per probe in $TMPDIR).
    sandbox = tempfile.mkdtemp(prefix="toolcost-home-")
    child_env: dict[str, str] = {}
    try:
        child_env = {
            # No BETTERMEMORY_* variable from the runner's environment: an
            # exported store, state or keys directory (or port) would point
            # the sandbox shim at a daemon outside the sandbox, which the
            # probe would then reuse and stop. The spec's own env, laid on
            # last, may still name them.
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(_OWN_ENV_PREFIX)
            },
            "HOME": sandbox,
            "XDG_CONFIG_HOME": str(Path(sandbox) / ".config"),
            "XDG_DATA_HOME": str(Path(sandbox) / ".local" / "share"),
            # The state and cache directories too: bettermemory's daemon
            # writes its state file (port, token) under the user state
            # directory, and an operator's XDG_STATE_HOME would put the
            # sandbox daemon's record outside the sandbox.
            "XDG_STATE_HOME": str(Path(sandbox) / ".local" / "state"),
            "XDG_CACHE_HOME": str(Path(sandbox) / ".cache"),
            **(env or {}),
        }
        return _probe(command, child_env=child_env, cwd=cwd, timeout=timeout)
    finally:
        if child_env:
            stop_sandbox_daemons(command, sandbox=sandbox, env=child_env, cwd=cwd)
        shutil.rmtree(sandbox, ignore_errors=True)


def _probe(
    command: list[str],
    *,
    child_env: dict[str, str],
    cwd: str | None,
    timeout: float,
) -> ProbeResult:
    """The JSON-RPC exchange itself: spawn, initialize, tools/list, and
    stop the server process whatever happened."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=child_env,
        cwd=cwd,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        handshake = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "toolcost", "version": "1"},
            },
        }
        proc.stdin.write(json.dumps(handshake) + "\n")
        proc.stdin.flush()
        init = _read_response(proc, want_id=1, timeout=timeout)
        instructions = init.get("result", {}).get("instructions")

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
            + "\n"
        )
        proc.stdin.flush()
        reply = _read_response(proc, want_id=2, timeout=timeout)
        tools = reply.get("result", {}).get("tools", [])
        return ProbeResult(
            tools=list(tools),
            instructions=instructions if isinstance(instructions, str) else "",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# The daemon a probed bettermemory leaves behind
# ---------------------------------------------------------------------------
#
# bettermemory's stdio shim starts a DETACHED daemon for the store it
# resolves (`_daemon_client.start_daemon`: `bettermemory up --foreground`
# in a new session), and that daemon outlives the shim by design. Under
# the sandbox HOME the store, and the state file naming the daemon's port
# and token, are inside the sandbox, so removing the sandbox without
# stopping the daemon first leaves a process `bettermemory down` can no
# longer find, serving a deleted store. The probe therefore stops every
# daemon whose state file is inside the sandbox before it removes it. A
# state file inside a fresh sandbox can only have been written by a
# daemon started during this probe, so nothing else is ever stopped, and
# nothing is ever matched by process name.

#: The daemon's state file name (`_daemon_client.state_file_for`).
_STATE_GLOB = "daemon-*.json"
#: The keys every state record carries (`_daemon_client._STATE_KEYS`).
_STATE_KEYS = ("pid", "port", "token", "version", "store", "started")
_STOP_TIMEOUT = 10.0


def _down_command(command: list[str]) -> list[str] | None:
    """`bettermemory down` spelled with the binary `command` ran, or None
    when `command` is not recognisably bettermemory."""
    if len(command) >= 3 and command[1:3] == ["-m", "bettermemory"]:
        return [*command[:3], "down"]
    if command and Path(command[0]).name in ("bettermemory", "bettermemory.exe"):
        return [command[0], "down"]
    return None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _down_state_dirs(env: dict[str, str]) -> list[Path] | None:
    """The state directories `bettermemory down` may resolve under `env`
    (`_daemon_client.resolve_state_dir`): `BETTERMEMORY_STATE_DIR` when
    set, else the platform's user state directory. platformdirs reads
    `XDG_STATE_HOME` on macOS in some releases and not in others, so both
    are named there. None where the directory is not one the environment
    decides (Windows, or no HOME)."""
    named = env.get("BETTERMEMORY_STATE_DIR")
    if named:
        return [Path(named).expanduser()]
    home = env.get("HOME")
    if not home or sys.platform == "win32":
        return None
    xdg = env.get("XDG_STATE_HOME")
    linux = Path(xdg or str(Path(home) / ".local" / "state")) / "bettermemory"
    if sys.platform != "darwin":
        return [linux]
    darwin = Path(home) / "Library" / "Application Support" / "bettermemory"
    return [darwin, linux] if xdg else [darwin]


def _health_pid(port: int, timeout: float = 1.0) -> int | None:
    """The pid the daemon on 127.0.0.1:`port` reports at ``GET /health``,
    None when nothing answers there or the answer names none."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/health", timeout=timeout
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    pid = body.get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _may_signal(record: dict[str, Any], sandbox: str) -> bool:
    """Whether the daemon `record` names is provably this probe's: its
    state file is inside the sandbox and the daemon answering on its port
    reports its pid. A pid alone may since belong to anything."""
    pid = int(record["pid"])
    if not _within(Path(str(record.get("state_file", ""))), Path(sandbox)):
        print(
            f"toolcost: not signalling pid {pid}: its state file is outside "
            "the sandbox",
            file=sys.stderr,
        )
        return False
    try:
        port = int(record["port"])
    except (TypeError, ValueError):
        port = 0
    answered = _health_pid(port) if port > 0 else None
    if answered != pid:
        print(
            f"toolcost: not signalling pid {pid}: the daemon on port "
            f"{record.get('port')} reports pid {answered}",
            file=sys.stderr,
        )
        return False
    return True


def sandbox_daemons(sandbox: str, env: dict[str, str]) -> list[dict[str, Any]]:
    """The state records of the daemons whose state file is inside the
    sandbox: under the sandbox HOME, or under `BETTERMEMORY_STATE_DIR` when
    the environment points it inside the sandbox. One pointed outside is
    not searched: a daemon recorded there stays reachable by `bettermemory
    down` after the sandbox goes, and need not be this probe's."""
    root = Path(sandbox)
    named = env.get("BETTERMEMORY_STATE_DIR")
    search = Path(named).expanduser() if named else root
    if not _within(search, root) or not search.is_dir():
        return []
    records = []
    for path in sorted(search.rglob(_STATE_GLOB)):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or any(key not in raw for key in _STATE_KEYS):
            continue
        if not isinstance(raw["pid"], int) or raw["pid"] <= 0:
            continue
        records.append({**raw, "state_file": str(path)})
    return records


def _pid_alive(pid: int) -> bool:
    """Whether a process with `pid` exists, as `_daemon_client.pid_alive`
    answers it. Not `os.kill(pid, 0)` on Windows: there signal 0 is
    CTRL_C_EVENT, sent to every process on the console, the test runner
    included (the first two CI runs on v9 died of it at the wait below)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop_sandbox_daemons(
    command: list[str], *, sandbox: str, env: dict[str, str], cwd: str | None
) -> list[int]:
    """Stop every daemon started for the sandbox; the pids found.

    `bettermemory down` first, with the binary the spec ran and the
    probe's own environment and working directory, so it resolves the
    store and state directory the shim did and stops the daemon the
    supported way (its shutdown endpoint, then a signal); only when every
    state directory `down` may resolve is inside the sandbox, so a `down`
    never reaches a daemon outside it. A state record still in the
    sandbox afterwards (a `down` that failed or was skipped, or a command
    not recognisably bettermemory) has its pid signalled, that pid only,
    and only when the daemon answering on the record's port reports that
    pid (`_may_signal`). Returns once every pid found is gone, so the
    sandbox is not removed from under a daemon still closing its store."""
    found = sandbox_daemons(sandbox, env)
    if not found:
        return []
    down = _down_command(command)
    state_dirs = _down_state_dirs(env)
    if down is not None and (
        state_dirs is None
        or not all(_within(state_dir, Path(sandbox)) for state_dir in state_dirs)
    ):
        print(
            f"toolcost: skipping `{' '.join(down)}`: its state directory "
            "is not inside the sandbox",
            file=sys.stderr,
        )
        down = None
    if down is not None:
        try:
            subprocess.run(
                down,
                env=env,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"toolcost: `{' '.join(down)}` failed: {exc}", file=sys.stderr)
    for record in sandbox_daemons(sandbox, env):
        pid = int(record["pid"])
        if not _pid_alive(pid) or not _may_signal(record, sandbox):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        if not _wait_gone(pid, _STOP_TIMEOUT) and hasattr(signal, "SIGKILL"):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    pids = [int(record["pid"]) for record in found]
    for pid in pids:
        if not _wait_gone(pid, _STOP_TIMEOUT):
            print(f"toolcost: daemon pid {pid} did not stop", file=sys.stderr)
    return pids


def _read_response(
    proc: subprocess.Popen[str], *, want_id: int, timeout: float
) -> dict[str, Any]:
    """Read newline-delimited JSON-RPC until the matching id arrives.

    Servers legitimately interleave notifications and log lines, so
    non-matching frames are skipped rather than treated as protocol
    errors.
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no reply to id={want_id} within {timeout}s")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout before replying")
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == want_id:
            assert isinstance(payload, dict)
            return dict(payload)


def measure(tools: list[dict[str, Any]], instructions: str = "") -> dict[str, Any]:
    """Byte/char cost of a tool list, full and by component, plus the
    server instructions the `initialize` reply carried. `full_bytes`
    keeps its meaning (the tools array alone) so every dated result file
    stays comparable; `instructions_bytes` and `session_bytes` (the two
    together) are the additive keys the bettermemory 9 budget reads."""

    def blob(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    full = blob(tools)
    names_descs = blob(
        [
            {"name": t.get("name", ""), "description": t.get("description", "")}
            for t in tools
        ]
    )
    schemas = blob([t.get("inputSchema", {}) for t in tools])
    full_bytes = len(full.encode("utf-8"))
    instructions_bytes = len(instructions.encode("utf-8"))
    return {
        "tool_count": len(tools),
        "full_bytes": full_bytes,
        "full_chars": len(full),
        "name_description_bytes": len(names_descs.encode("utf-8")),
        "name_description_chars": len(names_descs),
        "input_schema_bytes": len(schemas.encode("utf-8")),
        "instructions_bytes": instructions_bytes,
        "instructions_chars": len(instructions),
        "session_bytes": full_bytes + instructions_bytes,
        "bytes_per_tool": round(full_bytes / len(tools)) if tools else None,
    }


def _default_specs() -> list[dict[str, Any]]:
    repo = Path(__file__).resolve().parents[2]
    venv_python = repo / "venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    return [
        {
            "label": "bettermemory (shipped default)",
            "command": [python, "-m", "bettermemory"],
            "env": {},
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure per-turn tool-schema context cost of MCP servers."
    )
    parser.add_argument(
        "--spec",
        default=None,
        help=(
            "JSON file: a list of {label, command[], env{}, cwd}. Defaults to "
            "bettermemory's two configurations."
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    specs = (
        json.loads(Path(args.spec).read_text(encoding="utf-8"))
        if args.spec
        else _default_specs()
    )

    results = []
    for spec in specs:
        label = spec["label"]
        try:
            probe = probe_tools(
                spec["command"], env=spec.get("env"), cwd=spec.get("cwd")
            )
            row: dict[str, Any] = {
                "label": label,
                **measure(probe.tools, probe.instructions),
            }
            row["tools"] = sorted(t.get("name", "") for t in probe.tools)
        except Exception as exc:  # noqa: BLE001 - report, never fabricate
            row = {"label": label, "error": f"{type(exc).__name__}: {exc}"}
        results.append(row)
        print(f"probed {label}", file=sys.stderr)

    if args.json:
        print(json.dumps({"protocol": _PROTOCOL, "results": results}, indent=2))
    else:
        print(f"\nMCP tool-schema cost (protocol {_PROTOCOL})")
        print("full serialized tools/list — what a client pastes every turn\n")
        print("| server                              | tools | full bytes | per tool |")
        print("|-------------------------------------|-------|------------|----------|")
        for r in results:
            if "error" in r:
                print(f"| {r['label']:<35} |   —   |   ERROR    |    —     |")
                continue
            print(
                f"| {r['label']:<35} | {r['tool_count']:>5} "
                f"| {r['full_bytes']:>10,} | {r['bytes_per_tool']:>8,} |"
            )
        print()
        for r in results:
            if "error" in r:
                print(f"  {r['label']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
