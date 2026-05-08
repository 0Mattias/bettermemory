"""Programmatic client example: drive bettermemory from Python.

Most users interact with bettermemory through an MCP-aware host (Claude
Code, Cursor, etc.) — the host spawns the server and routes the model's
tool calls to it. This script shows the other path: connecting to the
server directly from your own code, for use cases like:

- integration testing against bettermemory in CI
- a custom agent that wants memory tools but isn't using a third-party
  MCP host
- bulk-loading a fresh store from a script
- one-off curation passes too elaborate for the `bettermemory health`
  CLI

The protocol is the same MCP JSON-RPC 2.0 surface every client speaks;
we use the official `mcp` Python SDK (already a runtime dependency of
bettermemory itself, so no extra install) so we don't have to hand-code
the wire format.

Run:

    venv/bin/python examples/programmatic_client.py

Output is a small narrated walk through write → search → show →
remove, showing one of each round trip.

Connecting to a different storage directory than the host's default:
set `BETTERMEMORY_DIR` in `env=` below. We default to a fresh tmp dir
so the example never touches the user's real `~/.claude-memory/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _resolve_server_command() -> list[str]:
    """The command to launch the bettermemory MCP server.

    Resolution order:
    1. `bettermemory` on PATH (a `uv tool install bettermemory` install).
    2. Fallback: run `python -m bettermemory` against the cloned repo so
       this example works in a development checkout without any install.
    """
    binary = shutil.which("bettermemory")
    if binary:
        return [binary]
    # Dev-checkout fallback. `python -m bettermemory` resolves the
    # package via the editable install (or however the running Python
    # has it on sys.path); we don't need to point at the repo root
    # explicitly.
    return [sys.executable, "-m", "bettermemory"]


def _pretty(payload: object) -> str:
    """The MCP SDK returns CallToolResult objects whose `content` field
    is a list of TextContent / ImageContent / etc. items. For
    bettermemory all results are JSON-encoded text, so we extract that
    and re-pretty-print it for readability."""
    if hasattr(payload, "content") and payload.content:
        first = payload.content[0]
        text = getattr(first, "text", None)
        if text is not None:
            try:
                return json.dumps(json.loads(text), indent=2)
            except json.JSONDecodeError:
                return text
    return repr(payload)


async def _walk_through_one_session(storage_dir: Path) -> None:
    server_cmd = _resolve_server_command()
    print(f"# Spawning bettermemory: {' '.join(server_cmd)}")
    print(f"# Storage dir: {storage_dir}")
    print()

    params = StdioServerParameters(
        command=server_cmd[0],
        args=server_cmd[1:],
        env={**os.environ, "BETTERMEMORY_DIR": str(storage_dir)},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---- Step 1: list tools ---------------------------------
            print("# 1. list_tools — what does the server expose?")
            tools_result = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools_result.tools)
            print(f"   {len(tool_names)} tools:")
            for name in tool_names:
                print(f"     - {name}")
            print()

            # ---- Step 2: write a memory -----------------------------
            print("# 2. memory_write — durable architectural decision")
            write_result = await session.call_tool(
                "memory_write",
                {
                    "content": (
                        "bettermemory uses fcntl-based per-file locking "
                        "for multi-process safety on Unix. The store "
                        "writes to a `.tmp` file then renames atomically, "
                        "so concurrent readers never observe torn writes."
                    ),
                    "scopes": ["projects:bettermemory", "infrastructure"],
                    "confidence": "high",
                },
            )
            print(_pretty(write_result))
            print()

            # Capture the new memory's id from the response so we can
            # retrieve it. The bettermemory write response is JSON;
            # we already pretty-printed it above.
            written = json.loads(write_result.content[0].text)
            new_id = written.get("id")
            assert new_id, f"memory_write didn't return an id: {written!r}"

            # ---- Step 3: search for it ------------------------------
            print("# 3. memory_search — looking for the fact we just wrote")
            search_result = await session.call_tool(
                "memory_search",
                {"query": "fcntl locking concurrency", "max_results": 3},
            )
            print(_pretty(search_result))
            print()

            # ---- Step 4: fetch full body ----------------------------
            print(f"# 4. memory_show — fetch the full body for {new_id}")
            show_result = await session.call_tool(
                "memory_show",
                {"id": new_id},
            )
            print(_pretty(show_result))
            print()

            # ---- Step 5: tombstone it -------------------------------
            print("# 5. memory_remove — tidy up after the demo")
            remove_result = await session.call_tool(
                "memory_remove",
                {"id": new_id, "reason": "programmatic-client demo cleanup"},
            )
            print(_pretty(remove_result))
            print()


async def main() -> int:
    # Use a dedicated tmp dir so the example never touches the user's
    # real `~/.claude-memory/`. Cleanup on exit (success or failure).
    tmp = Path(tempfile.mkdtemp(prefix="bm-example-"))
    try:
        await _walk_through_one_session(tmp)
        print("# Demo complete; tmp dir cleaned up.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
