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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_PROTOCOL = "2025-06-18"


def probe_tools(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 90.0,
) -> list[dict[str, Any]]:
    """Spawn an MCP server over stdio and return its advertised tools.

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
    sandbox = tempfile.mkdtemp(prefix="toolcost-home-")
    child_env = {
        **os.environ,
        "HOME": sandbox,
        "XDG_CONFIG_HOME": str(Path(sandbox) / ".config"),
        "XDG_DATA_HOME": str(Path(sandbox) / ".local" / "share"),
        **(env or {}),
    }
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
        _read_response(proc, want_id=1, timeout=timeout)

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
        return list(tools)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(sandbox, ignore_errors=True)


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


def measure(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Byte/char cost of a tool list, full and by component."""

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
    return {
        "tool_count": len(tools),
        "full_bytes": len(full.encode("utf-8")),
        "full_chars": len(full),
        "name_description_bytes": len(names_descs.encode("utf-8")),
        "name_description_chars": len(names_descs),
        "input_schema_bytes": len(schemas.encode("utf-8")),
        "bytes_per_tool": round(len(full.encode("utf-8")) / len(tools))
        if tools
        else None,
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
            tools = probe_tools(
                spec["command"], env=spec.get("env"), cwd=spec.get("cwd")
            )
            row: dict[str, Any] = {"label": label, **measure(tools)}
            row["tools"] = sorted(t.get("name", "") for t in tools)
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
