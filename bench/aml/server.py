"""The HTTP face of `service.MemoryService`, in the shape AML's API guide
specifies (agentmemoryleaderboard.ai/api-guide):

  POST /add     {request_id, messages[{role, content, timestamp?}], user_id, session_id}
                -> 200 {success: true, request_id, user_id, session_id}, echoed
                   byte for byte, only after the write is persisted and
                   searchable
  POST /search  {query, user_id, top_k, options?}
                -> 200 {data: [{id, content, score, created_at}]} in rank order
  GET  /health  -> 200 {status: "ok"}, unauthenticated

Auth: AML sends `Authorization: Bearer <key>`, `Authorization: Token <key>`
or `X-Api-Key: <key>`; all three are accepted against AML_ADAPTER_TOKEN.
The server refuses to start without a token, because the endpoint is
public by the contract's own requirement.

`options` (multiple-choice candidates, never carrying the gold label) is
appended to the lexical query: the candidates are words the question is
about, and a lexical ranker can only use words it is given.

A payload the contract calls invalid gets 400, never 500: AML retries a
500 up to 32 times, and a malformed request will not heal on retry.
Nothing here logs request bodies; the contract forbids keeping
evaluation data beyond the run, and access logs stay off.

Run:

    AML_ADAPTER_TOKEN=... AML_STORE_ROOT=/var/lib/bm-aml \\
        .venv/bin/python bench/aml/server.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import hmac
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aml.service import SERVING_BUDGET_CHARS, MemoryService  # noqa: E402


def _authorized(request: Request, token: str) -> bool:
    presented = request.headers.get("x-api-key")
    if presented is None:
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() in ("bearer", "token"):
            presented = value.strip()
    return presented is not None and hmac.compare_digest(presented, token)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _valid_messages(messages: Any) -> bool:
    return (
        isinstance(messages, list)
        and bool(messages)
        and all(
            isinstance(m, dict)
            and isinstance(m.get("role"), str)
            and m.get("content") not in (None, "", [])
            and (m.get("timestamp") is None or isinstance(m["timestamp"], (int, float)))
            for m in messages
        )
    )


def build_app(service: MemoryService, token: str) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def add(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _json_object(request)
        if body is None:
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
        missing = [
            k
            for k in ("request_id", "messages", "user_id", "session_id")
            if k not in body
        ]
        if missing or not _valid_messages(body.get("messages")):
            return JSONResponse(
                {"error": f"missing or invalid: {missing or ['messages']}"},
                status_code=400,
            )
        await run_in_threadpool(
            service.add,
            str(body["request_id"]),
            str(body["user_id"]),
            body["messages"],
            str(body["session_id"]),
        )
        return JSONResponse(
            {
                "success": True,
                "request_id": body["request_id"],
                "user_id": body["user_id"],
                "session_id": body["session_id"],
            }
        )

    async def search(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _json_object(request)
        if body is None:
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
        if "query" not in body or "user_id" not in body:
            return JSONResponse(
                {"error": "query and user_id are required"}, status_code=400
            )
        try:
            top_k = max(1, min(int(body.get("top_k") or 100), 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "top_k must be an integer"}, status_code=400)
        query = str(body["query"])
        options = body.get("options")
        if isinstance(options, list) and options:
            query = query + "\n" + "\n".join(str(o) for o in options)
        data = await run_in_threadpool(
            service.search, str(body["user_id"]), query, top_k
        )
        return JSONResponse({"data": data})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/add", add, methods=["POST"]),
            Route("/search", search, methods=["POST"]),
        ]
    )


def serving_service(root: Path) -> MemoryService:
    """The configuration the public endpoint runs, and nothing else."""
    return MemoryService(root, budget=SERVING_BUDGET_CHARS)


def main() -> None:
    p = argparse.ArgumentParser(description="bettermemory AML adapter")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    token = os.environ.get("AML_ADAPTER_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "AML_ADAPTER_TOKEN is required; the endpoint is public by contract"
        )
    root = Path(os.environ.get("AML_STORE_ROOT", "")).expanduser()
    if not str(root) or str(root) == ".":
        raise SystemExit("AML_STORE_ROOT is required")
    root.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        build_app(serving_service(root), token),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
