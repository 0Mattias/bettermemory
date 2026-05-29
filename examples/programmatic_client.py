"""Programmatic client example: drive bettermemory from Python.

Most users interact with bettermemory through an MCP-aware host (Claude
Code, Cursor, etc.). The host spawns the server and routes the model's
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

    uv run python examples/programmatic_client.py

(or, if you have `bettermemory` installed globally via `uv tool
install`, any Python interpreter that can import `mcp`.)

Output is a small narrated walk through write (fact) → write
(user-inference, staged pending) → confirm → search → show → remove,
showing one of each round trip and calling out the `staleness_verdict`
field on the search hit.

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
            print("# 1. list_tools: what does the server expose?")
            tools_result = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools_result.tools)
            print(f"   {len(tool_names)} tools:")
            for name in tool_names:
                print(f"     - {name}")
            print()

            # ---- Step 2: write a fact memory ------------------------
            # A `category="fact"` write commits immediately. We use a
            # generic build-tool convention here — the kind of fact a
            # real project's first memory often looks like.
            print("# 2. memory_write (fact): commits immediately")
            fact_result = await session.call_tool(
                "memory_write",
                {
                    "content": (
                        "Project atlas uses pnpm with a workspace, not "
                        "npm or yarn. Install with `pnpm install` at the "
                        "root; per-package lockfiles are disallowed by "
                        "`.npmrc`. Add deps with "
                        "`pnpm --filter <pkg> add <dep>`."
                    ),
                    "scopes": ["projects:atlas", "tools"],
                    "confidence": "high",
                    "category": "fact",
                },
            )
            print(_pretty(fact_result))
            print()

            fact_written = json.loads(fact_result.content[0].text)
            fact_id = fact_written.get("id")
            assert fact_id, f"memory_write didn't return an id: {fact_written!r}"

            # ---- Step 3: write a user-inference (staged pending) ----
            # `category="user-inference"` ALWAYS routes through the
            # staged-write tier — the server returns `status="pending"`
            # plus a `pending_id` instead of committing. The user (or
            # caller) confirms or cancels explicitly. This is the
            # user's veto on claims about themselves.
            print("# 3. memory_write (user-inference): stages pending")
            staged_result = await session.call_tool(
                "memory_write",
                {
                    "content": (
                        "User prefers code-driven walkthroughs over GUI "
                        "tours — when asked for a tutorial, lead with "
                        "runnable snippets, not screenshots."
                    ),
                    "scopes": ["learning-style"],
                    "confidence": "medium",
                    "category": "user-inference",
                },
            )
            print(_pretty(staged_result))
            print()

            staged = json.loads(staged_result.content[0].text)
            assert staged.get("status") == "pending", (
                f"expected status='pending' for user-inference write, got {staged!r}"
            )
            pending_id = staged["pending_id"]

            # ---- Step 4: confirm the pending write ------------------
            # In a real session the host surfaces the proposal to the
            # user and only calls _confirm after they assent. Here we
            # confirm immediately so the demo finishes in one pass.
            print(f"# 4. memory_write_confirm: commit pending {pending_id}")
            confirm_result = await session.call_tool(
                "memory_write_confirm",
                {"pending_id": pending_id},
            )
            print(_pretty(confirm_result))
            print()

            confirmed = json.loads(confirm_result.content[0].text)
            inference_id = confirmed.get("id")
            assert inference_id, f"confirm didn't return an id: {confirmed!r}"

            # ---- Step 5: search ------------------------------------
            # Each hit carries a `staleness_verdict` field — fresh /
            # spot_check_recommended / spot_check_required — derived
            # from calendar age, drift against `verified_paths`, and
            # git commits since `last_verified_at`. Brand-new writes
            # surface as `spot_check_required` because the body has
            # never been spot-checked yet (`last_verified_at` is
            # null); a subsequent `memory_verify(id, verified_paths=
            # [...])` call drops the verdict to `fresh`. This is the
            # signal the model uses to decide whether to spot-check
            # before relying on the hit.
            print("# 5. memory_search: hits carry staleness_verdict")
            search_result = await session.call_tool(
                "memory_search",
                {"query": "pnpm workspace install", "max_results": 3},
            )
            # The MCP layer serialises a list-returning tool as one
            # `TextContent` per hit, each carrying a JSON-encoded hit
            # dict — `staleness_verdict` is a top-level field on each
            # one (not nested under `verification`, which carries the
            # raw status). We walk `content` directly rather than
            # treating the response as a single payload.
            print(f"   {len(search_result.content)} hit(s):")
            for item in search_result.content:
                text = getattr(item, "text", None)
                if not text:
                    continue
                hit = json.loads(text)
                verdict = hit.get("staleness_verdict", "?")
                print(f"     - {hit.get('id')} -> staleness_verdict={verdict}")
            print(_pretty(search_result))
            print()

            # ---- Step 6: fetch the fact's full body -----------------
            # API surface note: the MCP tool is `memory_show`. If you're
            # driving the store *without* going through MCP (i.e. `from
            # bettermemory.store import Store` and calling methods directly),
            # the read-one method is `Store.load_one(id)`; `Store.show(id)` is
            # a public alias for it, so either name works.
            print(f"# 6. memory_show: fetch the full body for {fact_id}")
            show_result = await session.call_tool(
                "memory_show",
                {"id": fact_id},
            )
            print(_pretty(show_result))
            print()

            # ---- Step 7: tombstone both -----------------------------
            print("# 7. memory_remove: tidy up after the demo")
            for victim_id in (fact_id, inference_id):
                remove_result = await session.call_tool(
                    "memory_remove",
                    {"id": victim_id, "reason": "programmatic-client demo cleanup"},
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
