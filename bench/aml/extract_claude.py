"""Claude-extracted memory units for the AML harness (declaration E3).

WHY. E1/E2 measured a rule-based distiller (bench/aml/distill.py): first-
person sentences lifted out of user turns, relative dates written beside
them. It gained on LongMemEval-S and lost on LoCoMo-Refined, and the
reader ceiling (bench/aml/oracle.py) showed better ranking of raw rounds
cannot reach the board's leader. The leaders have a model turn each
conversation into dated facts at write time. This module does that with
the prompt bettermemory ships (src/bettermemory/session_capture.py), so
the number measured here is the product's number, not a bench-only
prompt's.

HOW IT RUNS. Extraction is a network call and MemoryService.add is
synchronous, so it happens in two passes. `warm` fetches every chunk's
reply concurrently through bench/llm.py (disk cache, spend cap) before a
single Add is made; `units_from_cache` then answers the service from the
cache without touching the network. A chunk the warm pass did not fetch
raises instead of silently storing no units, so an arm can never be
measured on a partial extraction.

Each request is built with a nonce derived from the chunk's own content
(`session_capture.content_nonce`), so the same chunk builds the same
request and a rerun of a finished arm costs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from llm import Client

from bettermemory import session_capture as sc

MODEL = "anthropic/claude-haiku-4.5"
MAX_TOKENS = 2500


class MissingExtraction(KeyError):
    """A chunk reached Add with no cached extraction: run `warm` first."""


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def turns_of(messages: list[dict[str, Any]]) -> list[sc.Turn]:
    out: list[sc.Turn] = []
    for m in messages:
        ts = m.get("timestamp")
        out.append(
            sc.Turn(
                role=str(m.get("role", "user")),
                text=_text(m.get("content")),
                ts_ms=int(ts) if isinstance(ts, (int, float)) else None,
            )
        )
    return out


def payload(messages: list[dict[str, Any]], model: str = MODEL) -> dict[str, Any]:
    turns = turns_of(messages)
    return Client.payload(
        model,
        sc.build_capture_messages(turns, nonce=sc.content_nonce(turns)),
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )


def units_from_reply(
    reply: str, messages: list[dict[str, Any]]
) -> list[tuple[str, str, int]]:
    """(kind, body, index of the first message it cites) per memory."""
    return [
        (m.kind, m.body, m.turns[0])
        for m in sc.parse_capture(reply, turns_of(messages))
    ]


def units_from_cache(
    messages: list[dict[str, Any]], model: str = MODEL
) -> list[tuple[str, str, int]]:
    if not messages:
        return []
    reply = Client.cached_text(payload(messages, model))
    if reply is None:
        raise MissingExtraction("no cached extraction for this chunk; run warm()")
    return units_from_reply(reply, messages)


async def warm(
    chunks: Iterable[list[dict[str, Any]]],
    *,
    budget_usd: float,
    concurrency: int = 16,
    model: str = MODEL,
) -> dict[str, Any]:
    """Fetch every distinct chunk's extraction. Chunks already cached cost
    nothing; identical chunks are fetched once."""
    todo: dict[str, list[dict[str, Any]]] = {}
    for messages in chunks:
        if messages:
            todo.setdefault(Client.cache_key(payload(messages, model)), messages)
    async with Client(budget_usd=budget_usd, concurrency=concurrency) as client:

        async def one(messages: list[dict[str, Any]]) -> int:
            turns = turns_of(messages)
            reply = await client.complete(
                model,
                sc.build_capture_messages(turns, nonce=sc.content_nonce(turns)),
                max_tokens=MAX_TOKENS,
                temperature=0.0,
            )
            return len(units_from_reply(reply.text, messages))

        results = await asyncio.gather(
            *(one(m) for m in todo.values()), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        return {
            "chunks": len(todo),
            "units": sum(r for r in results if isinstance(r, int)),
            "errors": len(errors),
            "first_error": repr(errors[0]) if errors else None,
            "new_spend_usd": round(client.spent_usd, 4),
            "new_calls": client.calls,
            "cache_hits": client.cache_hits,
        }
