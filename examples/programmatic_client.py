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

Output is a small narrated walk through write (fact) and write
(user-inference), each answered through the write review, then search,
show and remove, calling out the `staleness_verdict` field on the
search hit.

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
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _resolve_server_command() -> list[str]:
    """The command to launch the bettermemory MCP server.

    `bettermemory serve` runs the server over stdio in this process. Plain
    `bettermemory`, what an MCP host launches, is a shim in front of a
    detached daemon that outlives the session; after this example deletes
    its throwaway store, that daemon would still be running, and on
    noticing the deletion it saves the store it had open back into the
    temp directory. `serve` ends with the session and leaves nothing.

    Resolution order:
    1. `bettermemory` on PATH (a `uv tool install bettermemory` install).
    2. Fallback: run `python -m bettermemory` against the cloned repo so
       this example works in a development checkout without any install.
    """
    binary = shutil.which("bettermemory")
    if binary:
        return [binary, "serve"]
    # Dev-checkout fallback. `python -m bettermemory` resolves the
    # package via the editable install (or however the running Python
    # has it on sys.path); we don't need to point at the repo root
    # explicitly.
    return [sys.executable, "-m", "bettermemory", "serve"]


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


async def _write(session: ClientSession, args: dict[str, Any]) -> dict[str, Any]:
    """Write one memory through the write review, and return the result.

    A first `memory_write` writes nothing. It returns `status: "review"`
    with the stored statements the write resembles (`review.related`) and
    four questions, and the caller answers by re-sending the same call
    with a `review` argument: a relation label for every related id, and
    three probabilities from 0 to 1: that following the write would be
    unsafe, that its body holds a secret, and that the statement is
    standing. The standing question asks whether the statement directs
    future work in its scope until it is replaced: a ruling, a rule or a
    standing preference the user gave. When the related set changed in
    between, the next reply is another review that asks about the new ids
    too.

    This example labels every related statement `unrelated` and scores
    the three probabilities 0, which is what a demo into an empty store
    can say. A real agent answers from what it read in `review.related`
    and from the body it is writing; the preference in step 3, for one,
    could well score above 0 on the standing question.
    """
    result = await session.call_tool("memory_write", args)
    print(_pretty(result))
    payload = json.loads(result.content[0].text)
    relation: dict[str, str] = {}
    for _ in range(3):
        if payload.get("status") != "review":
            return payload
        review = payload["review"]
        relation.update({item["id"]: "unrelated" for item in review["related"]})
        answers = {
            "review_id": review["review_id"],
            "relation": relation,
            "unsafe": 0.0,
            "secret": 0.0,
            "standing": 0.0,
        }
        print("   (answering the review and re-sending the same call)")
        result = await session.call_tool("memory_write", {**args, "review": answers})
        print(_pretty(result))
        payload = json.loads(result.content[0].text)
    return payload


async def _walk_through_one_session(storage_dir: Path) -> None:
    server_cmd = _resolve_server_command()
    print(f"# Spawning bettermemory: {' '.join(server_cmd)}")
    print(f"# Storage dir: {storage_dir}")
    print()

    params = StdioServerParameters(
        command=server_cmd[0],
        args=server_cmd[1:],
        env={
            **os.environ,
            "BETTERMEMORY_DIR": str(storage_dir),
            # The store's key goes beside the throwaway store, not under
            # the user's config directory.
            "BETTERMEMORY_KEYS_DIR": str(storage_dir / "keys"),
        },
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
            # A `category="fact"` write passes the gates, comes back as a
            # review, and commits once `_write` answers it. We use a
            # generic build-tool convention here, the kind of fact a real
            # project's first memory often looks like.
            print("# 2. memory_write (fact): answer the write review, then it commits")
            fact_written = await _write(
                session,
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
            print()

            assert fact_written.get("status") == "committed", (
                f"expected status='committed' for the fact write, got {fact_written!r}"
            )
            fact_id = fact_written["id"]

            # ---- Step 3: write a user-inference ---------------------
            # `category="user-inference"` goes through the same review
            # and commits like a fact; the label is what keeps a stored
            # claim about the user distinguishable from an established
            # fact, and correctable. A body that reads as a claim about
            # the user filed as `fact` is refused with
            # `user_claim_warning` until it is relabelled.
            print("# 3. memory_write (user-inference): reviewed, committed, labelled")
            inference = await _write(
                session,
                {
                    "content": (
                        "User prefers code-driven walkthroughs over GUI "
                        "tours: when asked for a tutorial, lead with "
                        "runnable snippets, not screenshots."
                    ),
                    "scopes": ["learning-style"],
                    "confidence": "medium",
                    "category": "user-inference",
                },
            )
            print()

            assert inference.get("status") == "committed", (
                f"expected status='committed' for the user-inference write, "
                f"got {inference!r}"
            )
            inference_id = inference["id"]

            # ---- Step 4: search ------------------------------------
            # Each hit carries a `staleness_verdict` field (fresh,
            # spot_check_recommended or spot_check_required) derived
            # from calendar age, drift against `verified_paths`, and
            # git commits since `last_verified_at`. Brand-new writes
            # surface as `spot_check_required` because the body has
            # never been spot-checked yet (`last_verified_at` is
            # null); a subsequent `memory_verify(id, verified_paths=
            # [...])` call drops the verdict to `fresh`. This is the
            # signal the model uses to decide whether to spot-check
            # before relying on the hit.
            print("# 4. memory_search: hits carry staleness_verdict")
            search_result = await session.call_tool(
                "memory_search",
                {"query": "pnpm workspace install", "max_results": 3},
            )
            # The MCP layer serialises a list-returning tool as one
            # `TextContent` per hit, each carrying a JSON-encoded hit
            # dict; `staleness_verdict` is a top-level field on each
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

            # ---- Step 5: fetch the fact's full body -----------------
            # API surface note: the MCP tool is `memory_show`. If you're
            # driving the store *without* going through MCP (i.e. `from
            # bettermemory.store import Store` and calling methods directly),
            # the read-one method is `Store.load_one(id)`; `Store.show(id)` is
            # a public alias for it, so either name works.
            print(f"# 5. memory_show: fetch the full body for {fact_id}")
            show_result = await session.call_tool(
                "memory_show",
                {"id": fact_id},
            )
            print(_pretty(show_result))
            print()

            # ---- Step 6: tombstone both -----------------------------
            print("# 6. memory_remove: tidy up after the demo")
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
