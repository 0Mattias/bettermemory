"""Live competitor adapters for the comparative harness.

Where `adapters.py` carries honest capability-row stubs, this module
actually EXECUTES the two competitors that can run hermetically on a
maintainer machine, so the published comparison carries measured
recall@k rows instead of only unavailable-reasons:

- **mem0** (`mem0ai`, Python): fully local and keyless — a HuggingFace
  MiniLM embedder, an embedded qdrant store in a tempdir, and
  ``add(..., infer=False)`` so mem0's LLM extraction pipeline is never
  invoked (the configured LLM key is a dummy that no call ever uses).
  This deliberately measures mem0's *retrieval stack over verbatim
  facts*, not its extraction quality — the write-up states this.
- **server-memory** (`@modelcontextprotocol/server-memory`, Node): the
  Anthropic reference knowledge-graph server, bridged over stdio using
  the `mcp` client this repo already depends on. Its native
  ``search_nodes`` is a whole-query case-insensitive substring match
  with no tokenization, so the workload's keyword-bag probes would
  match nothing verbatim; the adapter donates a tokenized-OR ranker
  (`rank_entities`) on top. That accommodation is an editorial choice
  in the competitor's favor and is documented in the results.

Neither system is a dependency of this project: the default harness
(and CI) keeps seeing the `SystemUnavailable` stub rows. Live runs
happen only via `tests/eval/run_live.sh`, which installs the
competitor stack into a throwaway `.eval-venv/` and never touches the
dev venv, uv.lock, or published metadata. Inside the adapters every
prerequisite is still probed at runtime (import / PATH), so even under
`--live` an unprepared environment degrades to the honest stub row
rather than a crash.

The trio stays bettermemory-only *structurally*: live competitor rows
carry ``recall_at_k`` and leave ``eval_report`` as ``None`` — no
fabricated helped/endorsement/miss lanes (see docs/eval.md).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .adapters import (
    MEM0_CAPS,
    SERVER_MEMORY_CAPS,
    BetterMemoryAdapter,
    Capabilities,
    RunResult,
    SystemAdapter,
    SystemUnavailable,
    agentmemory_adapter,
    claude_mem_adapter,
)
from .workload import Workload

# One entity per fact, named by the fact's stable key — recall is then a
# set-membership check on entity names, robust to id-shape differences
# between systems.
_ENTITY_TYPE = "fact"

# Tokens shorter than this add only noise to the substring-OR search
# (articles, bare digits); the threshold matches what the design doc
# reviewed against the workload's probe vocabulary.
_MIN_TOKEN_LEN = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def query_tokens(query: str) -> list[str]:
    """Lowercased, deduplicated (order-preserving) query tokens of
    length >= _MIN_TOKEN_LEN — the OR-fan the server-memory bridge
    issues one `search_nodes` call per."""
    seen: dict[str, None] = {}
    for tok in _TOKEN_RE.findall(query.lower()):
        if len(tok) >= _MIN_TOKEN_LEN:
            seen.setdefault(tok, None)
    return list(seen)


def rank_entities(token_hits: dict[str, set[str]], *, k: int) -> list[str]:
    """Rank entity names by how many distinct query tokens matched them.

    `token_hits` maps token -> set of entity names that token's
    `search_nodes` call returned. Score = distinct-token match count;
    ties break lexicographically so the ranking is deterministic. Pure
    function — unit-testable without Node.
    """
    scores: dict[str, int] = {}
    for names in token_hits.values():
        for name in names:
            scores[name] = scores.get(name, 0) + 1
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:k]]


class Mem0LiveAdapter:
    """Runs mem0 locally and keyless over the workload (see module doc)."""

    name = "mem0"

    def capabilities(self) -> Capabilities:
        return MEM0_CAPS

    def run(self, workload: Workload, *, k: int) -> RunResult:
        # MEM0_TELEMETRY must be set BEFORE the import — mem0 reads it at
        # module load when constructing its posthog client.
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        try:
            from mem0 import Memory  # pyright: ignore[reportMissingImports]
        except ImportError:
            raise SystemUnavailable(
                "mem0ai not importable — run tests/eval/run_live.sh (installs "
                "the competitor stack into a throwaway .eval-venv)"
            ) from None

        with tempfile.TemporaryDirectory(prefix="bm-eval-mem0-") as td:
            config = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "path": str(Path(td) / "qdrant"),
                        "collection_name": "bm_eval",
                        "embedding_model_dims": 384,
                    },
                },
                "embedder": {
                    "provider": "huggingface",
                    "config": {
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                        "embedding_dims": 384,
                    },
                },
                # Never called: every add() passes infer=False and search()
                # makes no chat calls. The dummy key only satisfies mem0's
                # eager client construction.
                "llm": {
                    "provider": "openai",
                    "config": {
                        "api_key": "sk-unused-infer-false",
                        "model": "gpt-5-mini",
                    },
                },
            }
            memory = Memory.from_config(config)
            for fact in workload.facts:
                memory.add(
                    fact.body,
                    user_id="eval",
                    infer=False,
                    metadata={"key": fact.key},
                )

            gold = workload.gold_probes
            recalled = 0
            for probe in gold:
                # mem0 2.0 rejects top-level entity params in search();
                # the documented form is filters= (add() still takes
                # user_id= directly).
                res = memory.search(probe.query, filters={"user_id": "eval"}, limit=k)
                hits = res.get("results", res) if isinstance(res, dict) else res
                keys = {
                    (hit.get("metadata") or {}).get("key")
                    for hit in hits
                    if isinstance(hit, dict)
                }
                if probe.gold_key in keys:
                    recalled += 1

        return RunResult(
            name=self.name,
            capabilities=self.capabilities(),
            ran=True,
            k=k,
            probes_total=len(workload.probes),
            gold_total=len(gold),
            recalled=recalled,
            recall_at_k=(recalled / len(gold)) if gold else None,
            eval_report=None,
            system_version=importlib.metadata.version("mem0ai"),
        )


class ServerMemoryLiveAdapter:
    """Bridges the reference MCP memory server over stdio (see module doc)."""

    name = "server-memory"

    # Generous: `npx -y` may download the package on first run.
    _STARTUP_TIMEOUT_S = 120.0

    def capabilities(self) -> Capabilities:
        return SERVER_MEMORY_CAPS

    def run(self, workload: Workload, *, k: int) -> RunResult:
        if shutil.which("npx") is None:
            raise SystemUnavailable(
                "node/npx not on PATH — install Node 20+ to run "
                "@modelcontextprotocol/server-memory live"
            )
        try:
            import mcp  # noqa: F401  (probe only; used inside _run_async)
        except ImportError:
            raise SystemUnavailable(
                "the `mcp` client package is not importable in this venv"
            ) from None
        # Nothing in the harness calls run() from an async context, so a
        # blocking bridge keeps the SystemAdapter protocol synchronous.
        return asyncio.run(self._run_async(workload, k=k))

    async def _run_async(self, workload: Workload, *, k: int) -> RunResult:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="bm-eval-srvmem-") as td:
            params = StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-memory"],
                # Belt and suspenders for hermeticity: point the memory file
                # into the tempdir (honored on current main) AND cwd there
                # (the published build once ignored MEMORY_FILE_PATH —
                # servers issue #1018 — falling back to a cwd-relative file).
                env={**os.environ, "MEMORY_FILE_PATH": str(Path(td) / "memory.jsonl")},
                cwd=td,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await asyncio.wait_for(
                        session.initialize(), timeout=self._STARTUP_TIMEOUT_S
                    )
                    server_info = getattr(init, "serverInfo", None)
                    version = getattr(server_info, "version", None)

                    # Hermeticity guard: if the env-var fallback ever leaves
                    # us on a shared graph, empty it before ingesting.
                    existing = _entity_names(await session.call_tool("read_graph", {}))
                    if existing:
                        await session.call_tool(
                            "delete_entities", {"entityNames": sorted(existing)}
                        )

                    await session.call_tool(
                        "create_entities",
                        {
                            "entities": [
                                {
                                    "name": fact.key,
                                    "entityType": _ENTITY_TYPE,
                                    "observations": [fact.body],
                                }
                                for fact in workload.facts
                            ]
                        },
                    )

                    gold = workload.gold_probes
                    recalled = 0
                    for probe in gold:
                        token_hits: dict[str, set[str]] = {}
                        for tok in query_tokens(probe.query):
                            res = await session.call_tool(
                                "search_nodes", {"query": tok}
                            )
                            token_hits[tok] = _entity_names(res)
                        if probe.gold_key in rank_entities(token_hits, k=k):
                            recalled += 1

        return RunResult(
            name=self.name,
            capabilities=self.capabilities(),
            ran=True,
            k=k,
            probes_total=len(workload.probes),
            gold_total=len(gold),
            recalled=recalled,
            recall_at_k=(recalled / len(gold)) if gold else None,
            eval_report=None,
            system_version=version,
        )


def _entity_names(result: Any) -> set[str]:
    """Entity names out of a server-memory tool result (JSON text block)."""
    names: set[str] = set()
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        for entity in payload.get("entities", []) if isinstance(payload, dict) else []:
            name = entity.get("name") if isinstance(entity, dict) else None
            if name:
                names.add(name)
    return names


def live_adapters() -> list[SystemAdapter]:
    """The `--live` roster: same five names as `default_adapters()`, with
    mem0 and server-memory swapped for executing implementations.
    agentmemory and claude-mem stay documented-unavailable — see their
    stub reasons for why a live run would not be honest."""
    return [
        BetterMemoryAdapter(),
        Mem0LiveAdapter(),
        ServerMemoryLiveAdapter(),
        agentmemory_adapter(),
        claude_mem_adapter(),
    ]
