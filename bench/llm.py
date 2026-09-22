"""One OpenAI-compatible chat client for the bench harnesses that need a
model in the loop: a judge, a stand-in reader, an item generator.

WHY ONE CLIENT. Every harness that calls a model needs the same four
properties, and each one is a way a benchmark run goes wrong when it is
missing:

  cache     A response is keyed by (model, messages, sampling params) and
            kept on disk, so a rerun of a finished cell costs nothing and
            returns byte-identical text. Re-scoring never re-samples.
  budget    Spend is summed from the provider's own `usage.cost` and the
            run refuses the next call once the cap is reached, instead of
            discovering the bill afterwards.
  retries   429 and 5xx back off and retry; a 4xx that is not a rate
            limit fails loudly, because retrying a malformed request only
            spends money.
  no key    The key is read from OPENROUTER_API_KEY at call time and never
            written to any artifact, cache entry or log line.

The cache lives in bench/.llm-cache/ (gitignored). Deleting it forces a
fresh sample on the next run; nothing else depends on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("BENCH_LLM_BASE_URL", "https://openrouter.ai/api/v1")
CACHE_DIR = Path(__file__).resolve().parent / ".llm-cache"

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    cost: float
    prompt_tokens: int
    completion_tokens: int
    cached: bool
    model: str


@dataclass
class Client:
    """Async client with a disk cache and a spend cap.

    `budget_usd` caps NEW spend in this process; cached responses are free
    and do not count. `concurrency` bounds in-flight requests.
    """

    budget_usd: float
    concurrency: int = 16
    timeout_s: float = 180.0
    spent_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0
    _sem: asyncio.Semaphore = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Client:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is not set")
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.timeout_s,
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    @staticmethod
    def cache_key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return CACHE_DIR / key[:2] / f"{key}.json"

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
        response_json: bool = False,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if reasoning_effort == "off":
            # Thinking disabled outright, the way AML's pipelines call
            # Qwen3-14B (`enable_thinking: False` on SiliconFlow).
            payload["reasoning"] = {"enabled": False}
        elif reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort, "exclude": True}
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        key = self.cache_key(payload)
        path = self._cache_path(key)
        if path.exists():
            hit = json.loads(path.read_text(encoding="utf-8"))
            self.cache_hits += 1
            return Completion(
                text=hit["text"],
                cost=0.0,
                prompt_tokens=hit.get("prompt_tokens", 0),
                completion_tokens=hit.get("completion_tokens", 0),
                cached=True,
                model=model,
            )
        async with self._sem:
            async with self._lock:
                if self.spent_usd >= self.budget_usd:
                    raise BudgetExceeded(
                        f"budget ${self.budget_usd:.2f} reached "
                        f"(spent ${self.spent_usd:.4f})"
                    )
            body = await self._post(payload)
        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        async with self._lock:
            self.spent_usd += cost
            self.calls += 1
        record = {
            "model": model,
            "served_model": body.get("model"),
            "text": text,
            "finish_reason": choice.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost": cost,
        }
        if not text.strip():
            # An empty completion is never an answer: a reasoning model that
            # spent its whole max_tokens thinking returns "" with
            # finish_reason "length". Caching it would make every rerun
            # replay the failure, so it is returned uncached.
            return Completion(
                text=text,
                cost=cost,
                prompt_tokens=int(record["prompt_tokens"] or 0),
                completion_tokens=int(record["completion_tokens"] or 0),
                cached=False,
                model=model,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return Completion(
            text=text,
            cost=cost,
            prompt_tokens=int(record["prompt_tokens"] or 0),
            completion_tokens=int(record["completion_tokens"] or 0),
            cached=False,
            model=model,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._http is not None
        delay = 2.0
        last: str = ""
        for _ in range(8):
            try:
                resp = await self._http.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                last = f"transport: {exc!r}"
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("error"):
                        last = f"provider error: {body['error']}"
                    elif body.get("choices"):
                        return body
                    else:
                        last = "empty choices"
                elif resp.status_code in _RETRY_STATUS:
                    last = f"http {resp.status_code}: {resp.text[:200]}"
                else:
                    raise RuntimeError(
                        f"{payload['model']}: http {resp.status_code}: "
                        f"{resp.text[:400]}"
                    )
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)
        raise RuntimeError(f"{payload['model']}: gave up after retries ({last})")
