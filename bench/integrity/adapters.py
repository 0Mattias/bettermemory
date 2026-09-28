"""System adapters for the integrity benchmark.

One protocol, five arms. Every adapter owns a fresh store under a scratch
directory, exposes the system's public write path as `add`, its public
read path as `search`, and, where the deployment allows it, a store
injection path that bypasses the write API (`inject`). The harness never
tunes a system and never follows a hint; it plays the compromised agent
that writes what it is given and reads what it is served.

Per-arm signal definitions, fixed here and documented in docs/eval.md:

  bettermemory   supersession signal: the hit carries `superseded_by` or
                 `contradicts` (the link annotations search attaches).
                 provenance channel: the hit's `provenance` label.
  mem0-raw       no supersession channel, no provenance channel.
  mem0-infer     as mem0-raw; extraction on (UPDATE / DELETE decide what
                 is served, not a signal on what is served).
  graphiti       supersession signal: the edge carries `invalid_at` or
                 `expired_at`. provenance channel: the edge's `episodes`
                 list (an API-written edge names its source episode).
  letta          no supersession channel, no provenance channel.

An adapter that cannot run raises `SystemUnavailable` with the reason,
the same discipline as tests/eval/adapters.py: the row is published as
unavailable with the blocker, never with a fabricated number.

The bettermemory adapter imports the package lazily so the rival arms
can be collected from a venv that does not carry it.

LLM usage. The two arms that call an LLM through an OpenAI-compatible
endpoint, mem0-infer and graphiti, carry an `LLMUsage` (`llm_usage`):
every chat completion their own OpenAI clients make is observed at
`chat.completions.create`, which returns the SDK's response unchanged,
and its prompt and completion tokens (and `cost`, when the endpoint
returns one) are attributed to the phase `collect` is in. When the
endpoint is OpenRouter's, `collect` also reads the key's usage from
https://openrouter.ai/api/v1/key before and after the run
(`openrouter_key_usage`). The key is read from BM_INTEGRITY_LLM_API_KEY
at that call, sent in the Authorization header and nowhere else: it is
never stored, returned, printed or logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import inspect
import json
import os
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

_ROOT = Path(__file__).resolve().parents[2]
SCOPE = "projects:halden"
LOCAL_LLM = os.environ.get("BM_INTEGRITY_LLM", "llama3.1:8b")
LOCAL_EMBEDDER = "nomic-embed-text"
LLM_BASE_URL = os.environ.get("BM_INTEGRITY_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.environ.get("BM_INTEGRITY_LLM_API_KEY", "ollama")
OLLAMA_OPENAI_URL = "http://localhost:11434/v1"
# True when the LLM is served by something other than the local ollama
# daemon (any OpenAI-compatible endpoint). The embedder stays on ollama
# either way, so only the LLM's pull check is waived.
REMOTE_LLM = LLM_BASE_URL.rstrip("/") != OLLAMA_OPENAI_URL
LETTA_EMBEDDING_COLUMN_DIM = 4096
SELF_TEST_UPDATE = (
    "The api gateway deploy workflow was renamed: production deploys now run "
    "through release-gateway in GitHub Actions."
)
SELF_TEST_STATEMENT = (
    "Production deploys of the api gateway go through the deploy-gateway "
    "GitHub Actions workflow."
)
USER = "halden"
GROUP = "halden"


class SystemUnavailable(Exception):
    """The arm cannot execute here; `reason` is published with the row."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InjectionUnsupported(Exception):
    """The deployment offers no path around the write API."""


@dataclass
class AddOutcome:
    stored: bool
    refused: bool
    status: str
    warning: str | None = None
    ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stored": self.stored,
            "refused": self.refused,
            "status": self.status,
            "warning": self.warning,
            "ids": list(self.ids),
            "raw": self.raw,
        }


@dataclass
class Hit:
    rank: int
    id: str | None
    text: str
    signal: bool
    signal_fields: dict[str, Any] = field(default_factory=dict)
    provenance: str | None = None
    score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "id": self.id,
            "text": self.text,
            "signal": self.signal,
            "signal_fields": self.signal_fields,
            "provenance": self.provenance,
            "score": self.score,
            "raw": self.raw,
        }


class Adapter(Protocol):
    name: str

    def capabilities(self) -> dict[str, Any]: ...
    def version(self) -> dict[str, Any]: ...
    def reset(self) -> None: ...
    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> AddOutcome: ...
    def search(self, query: str, k: int) -> list[Hit]: ...
    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str: ...
    def close(self) -> None: ...


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


# ---------------------------------------------------------------------------
# LLM usage
# ---------------------------------------------------------------------------

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
LLM_KEY_ENV = "BM_INTEGRITY_LLM_API_KEY"


def _bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "failed": 0,
        "without_usage": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": None,
        "calls_with_cost": 0,
    }


def _field_of(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class LLMUsage:
    """The chat completions an arm's own OpenAI clients make, observed and
    never altered, attributed to the phase `collect` names (`attribute`):
    reset (the arm's self-test), add (per statement), search, inject, or
    unattributed. `report()` is what the raw file carries."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.clients: dict[str, str] = {}
        self.reason: str | None = None
        self.notes: list[str] = []
        self._phase: tuple[str, str | None] = ("unattributed", None)
        self._lock = threading.Lock()
        self._models: dict[str, int] = {}
        self._totals = _bucket()
        self._by_phase: dict[str, dict[str, Any]] = {}
        self._per_add: dict[str, dict[str, Any]] = {}

    @property
    def observed(self) -> bool:
        return bool(self.clients)

    def unobserved(self, reason: str) -> None:
        """Record why no client is observed; the report says so."""
        self.reason = reason

    @contextlib.contextmanager
    def attribute(self, phase: str, key: str | None = None) -> Iterator[None]:
        """Attribute the calls made inside the block to `phase` (and, for
        an add, to the statement `key`)."""
        previous = self._phase
        self._phase = (phase, key)
        if phase == "add" and key is not None:
            with self._lock:
                self._per_add.setdefault(key, _bucket())
        try:
            yield
        finally:
            self._phase = previous

    def observe(self, client: Any, label: str) -> None:
        """Wrap `client.chat.completions.create` on this client instance.
        The wrapper calls the SDK's own method with the same arguments and
        returns its result (or its awaitable, for an async client) as it
        came; it only reads the response's `usage` and `model`."""
        completions = client.chat.completions
        original = completions.create
        usage = self

        def create(*args: Any, **kwargs: Any) -> Any:
            phase = usage._phase
            try:
                result = original(*args, **kwargs)
            except Exception:
                usage._record(phase, None, failed=True)
                raise
            if inspect.isawaitable(result):
                return usage._finish(phase, result)
            usage._record(phase, result)
            return result

        completions.create = create
        self.clients[label] = str(getattr(client, "base_url", "") or self.endpoint)

    async def _finish(self, phase: tuple[str, str | None], pending: Any) -> Any:
        try:
            response = await pending
        except Exception:
            self._record(phase, None, failed=True)
            raise
        self._record(phase, response)
        return response

    def _record(
        self, phase: tuple[str, str | None], response: Any, failed: bool = False
    ) -> None:
        usage = None if response is None else _field_of(response, "usage")
        prompt = (
            _number(_field_of(usage, "prompt_tokens")) if usage is not None else None
        )
        completion = (
            _number(_field_of(usage, "completion_tokens"))
            if usage is not None
            else None
        )
        total = _number(_field_of(usage, "total_tokens")) if usage is not None else None
        cost = _number(_field_of(usage, "cost")) if usage is not None else None
        model = None if response is None else _field_of(response, "model")
        name, key = phase
        with self._lock:
            buckets = [self._totals, self._by_phase.setdefault(name, _bucket())]
            if name == "add" and key is not None:
                buckets.append(self._per_add.setdefault(key, _bucket()))
            for bucket in buckets:
                bucket["calls"] += 1
                if failed:
                    bucket["failed"] += 1
                    continue
                if prompt is None and completion is None:
                    bucket["without_usage"] += 1
                bucket["prompt_tokens"] += int(prompt or 0)
                bucket["completion_tokens"] += int(completion or 0)
                bucket["total_tokens"] += int(
                    total if total is not None else (prompt or 0) + (completion or 0)
                )
                if cost is not None:
                    bucket["cost"] = round((bucket["cost"] or 0.0) + cost, 10)
                    bucket["calls_with_cost"] += 1
            if model is not None:
                self._models[str(model)] = self._models.get(str(model), 0) + 1

    def report(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "observed": self.observed,
                "endpoint": self.endpoint,
                "clients": dict(self.clients),
            }
            if not self.observed:
                out["reason"] = self.reason or "no client was observed"
            if self.notes:
                out["notes"] = list(self.notes)
            out["how"] = (
                "chat.completions.create observed on the arm's own OpenAI clients; "
                "tokens and cost as each response's usage reports them"
            )
            out["totals"] = {**self._totals, "models": dict(self._models)}
            out["by_phase"] = {k: dict(v) for k, v in self._by_phase.items()}
            out["per_add"] = {k: dict(v) for k, v in self._per_add.items()}
            return out


def is_openrouter(base_url: str) -> bool:
    host = urllib.parse.urlparse(base_url).hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def openrouter_key_usage(timeout: float = 30.0) -> dict[str, Any]:
    """The key's cumulative usage as OpenRouter's GET /api/v1/key reports
    it (`data.usage`, in OpenRouter credits): {"usage": x} or {"error":
    ...}. The key is read from BM_INTEGRITY_LLM_API_KEY here and sent in
    the Authorization header only. Only that one number is kept from the
    response, and an error keeps its type or HTTP status, never a message
    or a body."""
    key = os.environ.get(LLM_KEY_ENV, "").strip()
    if not key:
        return {"error": f"{LLM_KEY_ENV} is not set"}
    request = urllib.request.Request(
        OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - recorded, the run goes on
        return {"error": type(exc).__name__}
    data = body.get("data") if isinstance(body, dict) else None
    usage = _number(data.get("usage")) if isinstance(data, dict) else None
    if usage is None:
        return {"error": "the key response carries no numeric data.usage"}
    return {"usage": usage}


def key_usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The run's spend on the key, from two `openrouter_key_usage` reads:
    what OpenRouter had counted by the second read, less the first. Any
    other use of the key in between counts too; the per-call costs are
    the call-by-call record."""
    out: dict[str, Any] = {
        "source": OPENROUTER_KEY_URL,
        "before": before,
        "after": after,
    }
    if "usage" in before and "usage" in after:
        out["delta"] = round(after["usage"] - before["usage"], 10)
    return out


# ---------------------------------------------------------------------------
# bettermemory
# ---------------------------------------------------------------------------


class BetterMemoryAdapter:
    """The shipped handlers, in-process, over a scratch store.

    Writes go through `memory_write` with the full gate chain; reads through
    `memory_search` at the shared depth, each hit's body read back with
    `memory_show` (the documented read for a hit). The recorder is on, so
    every API write is a signed log row with the record pointing at it,
    which is what a planted row's missing pointer is measured against.
    """

    name = "bettermemory"

    def __init__(self, scratch: Path) -> None:
        # Resolved, because the guard in `reset` compares this root with
        # the config's `resolved_directory()`, and a `tempfile.mkdtemp`
        # path on macOS reaches the same store through a symlink
        # (`/var` -> `/private/var`) that the config layer follows.
        self.scratch = Path(scratch).resolve()
        self.root = self.scratch / "store"
        self._deps: Any = None
        self._loop: Any = None
        self._session_id: str | None = None

    # -- lifecycle --------------------------------------------------------

    def reset(self) -> None:
        import asyncio

        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        cfg_path = self.scratch / "config.toml"
        cfg_path.write_text(
            f'[storage]\ndirectory = "{self.root.as_posix()}"\n', encoding="utf-8"
        )
        from bettermemory._handlers import ToolHandlers
        from bettermemory._response import ResponseBuilder
        from bettermemory.config import load_config
        from bettermemory.events import Recorder
        from bettermemory.session import SessionRegistry
        from bettermemory.store import Store

        cfg = load_config(cfg_path)
        if cfg.resolved_directory() != self.root:
            raise SystemUnavailable(
                f"config resolved {cfg.resolved_directory()} instead of the scratch store"
            )
        store = Store(self.root, keys_dir=self.scratch / "keys")
        sessions = SessionRegistry()
        self._session_id = sessions.for_request(None).session_id
        recorder = Recorder(
            store=store,
            session_id=self._session_id,
            enabled=True,
            worktree_root=str(_ROOT),
        )
        self._deps = ToolHandlers(
            config=cfg,
            store=store,
            sessions=sessions,
            recorder=recorder,
            responses=ResponseBuilder(
                stale_after_days=cfg.behavior.verification_stale_days
            ),
        )
        self._loop = asyncio.new_event_loop()

    def close(self) -> None:
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    # -- surface ----------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        return {
            "write_gates": True,
            "supersession_signal_channel": "superseded_by / contradicts link annotations",
            "supersession_write_channel": (
                "supersedes links set by memory_write; cue-less divergence filed "
                "for memory_conflicts"
            ),
            "provenance_channel": "provenance label (local / imported / synced / unaccounted)",
            "store_injection": "a memories row inserted by SQL, no log pointer",
            "forged_provenance": (
                "a memory_put log row appended without the key, and the row "
                "pointing at it"
            ),
        }

    def version(self) -> dict[str, Any]:
        import bettermemory

        return {"bettermemory": bettermemory.__version__}

    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> AddOutcome:
        from bettermemory.handlers.write import memory_write

        response = self._run(
            memory_write(
                self._deps,
                content=text,
                scopes=[SCOPE],
                confidence="medium",
                source="explicit-statement",
                category="fact",
            )
        )
        status = str(response.get("status"))
        stored = status == "committed"
        refused = status not in ("committed", "pending")
        warnings = response.get("warnings") or []
        warning = None
        if stored and warnings:
            warning = "; ".join(str(w) for w in warnings)
        ids = [response["id"]] if stored and response.get("id") else []
        raw = {
            k: _json_safe(v)
            for k, v in response.items()
            if k
            in (
                "status",
                "id",
                "hint",
                "markers",
                "matches",
                "warnings",
                "pending_reason",
                "supersedes",
                "conflicts_filed",
            )
        }
        return AddOutcome(
            stored=stored,
            refused=refused,
            status=status,
            warning=warning,
            ids=ids,
            raw=raw,
        )

    def search(self, query: str, k: int) -> list[Hit]:
        from bettermemory.handlers.search import memory_search
        from bettermemory.handlers.show import memory_show

        hits = self._run(
            memory_search(self._deps, query, max_results=k, auto_scope=False)
        )
        out: list[Hit] = []
        for rank, hit in enumerate(hits[:k], 1):
            shown = self._run(memory_show(self._deps, hit["id"]))
            body = str(shown.get("body") or "")
            signal_fields = {
                key: _json_safe(hit[key])
                for key in ("superseded_by", "contradicts")
                if key in hit
            }
            out.append(
                Hit(
                    rank=rank,
                    id=hit["id"],
                    text=body,
                    signal=bool(signal_fields),
                    signal_fields=signal_fields,
                    provenance=hit.get("provenance"),
                    score=hit.get("score"),
                    raw={
                        "snippet": hit.get("snippet"),
                        "relevance": hit.get("relevance"),
                        "staleness_verdict": hit.get("staleness_verdict"),
                        "verification": _json_safe(hit.get("verification")),
                    },
                )
            )
        return out

    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str:
        """Plant a memories row with forged trust fields, past the write path.

        The plain plant is a row inserted by SQL with no `log_mac`, the way
        a writer with the store file and not the key would plant it: the
        FTS trigger indexes it, so it is a search candidate, and only the
        pointer check can tell it apart. With `forge_provenance` a
        `memory_put` log row naming the planted id is appended first with a
        MAC made without the key, and the planted row points at it, so the
        pointer names a row of the right kind that names the right id and
        the MAC check is what refuses it. That is the recon's open item 7
        (tamper evidence), measured rather than assumed.
        """
        import secrets

        from bettermemory import store as _store
        from bettermemory.log import canonical_payload, last_row
        from bettermemory.models import (
            Category,
            Confidence,
            Memory,
            Source,
            generate_ulid,
        )
        from bettermemory.origin import capture as capture_origin

        now = datetime.now(timezone.utc)
        mid = generate_ulid()
        real_paths = ["src/bettermemory/store.py", "src/bettermemory/verify.py"]
        planted = Memory(
            id=mid,
            created=now,
            updated=now,
            scopes=[SCOPE],
            confidence=Confidence.HIGH,
            source=Source.CORRECTION,
            category=Category.FACT,
            body=text.strip() + "\n",
            last_verified_at=now,
            verified_paths=real_paths,
            origin=capture_origin(_ROOT),
        )
        store = self._deps.store
        log_mac: str | None = None
        if forge_provenance:
            previous = last_row(store.conn)
            assert previous is not None, "a created store has a log row"
            payload = {
                "memory": planted.model_dump(mode="json"),
                "provenance": _store.LOCAL,
                "filename": None,
            }
            log_mac = secrets.token_hex(32)
            store.conn.execute(
                "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    previous.seq + 1,
                    previous.ts,
                    self._session_id,
                    "memory_put",
                    canonical_payload(payload),
                    previous.mac,
                    log_mac,
                ),
            )
        columns = _store._record_columns(planted)
        columns.update(
            {
                "body_fts": _store.fts_index_text(planted.body),
                "scopes_text": _store._scopes_text(planted.scopes),
                "scopes_fts": _store.fts_index_text(" ".join(planted.scopes)),
                "filename": None,
                "provenance": _store.LOCAL,
                "links_json": "[]",
                "corroborations": 999,
                "last_corroborated": _store._iso(now),
                "log_mac": log_mac,
            }
        )
        store.conn.execute(
            _store._UPSERT_MEMORY,
            tuple(columns[c] for c in _store._MEMORY_COLUMNS),
        )
        store.conn.commit()
        return mid


# ---------------------------------------------------------------------------
# mem0
# ---------------------------------------------------------------------------


class Mem0Adapter:
    """mem0ai over an embedded qdrant store. Two arms: `raw` (add with
    infer=False, MiniLM embeddings) and `infer` (add with infer=True, the
    extraction and UPDATE / DELETE logic on, llama3.1:8b and nomic-embed-
    text through the local ollama daemon by default; BM_INTEGRITY_LLM_BASE_URL
    and BM_INTEGRITY_LLM_API_KEY point the LLM at any OpenAI-compatible
    endpoint while the embedder stays on ollama)."""

    def __init__(self, scratch: Path, mode: str) -> None:
        if mode not in ("raw", "infer"):
            raise ValueError(mode)
        self.mode = mode
        self.name = f"mem0-{mode}"
        self.scratch = Path(scratch)
        self.root = self.scratch / "store"
        self._memory: Any = None
        self._clock = datetime.now(timezone.utc) - timedelta(days=1)
        self.llm_usage: LLMUsage | None = (
            LLMUsage(LLM_BASE_URL) if mode == "infer" else None
        )

    def _config(self) -> dict[str, Any]:
        if self.mode == "raw":
            dims = 384
            embedder = {
                "provider": "huggingface",
                "config": {
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "embedding_dims": dims,
                },
            }
            llm = {
                "provider": "openai",
                "config": {"api_key": "sk-unused-infer-false", "model": "gpt-5-mini"},
            }
        else:
            dims = 768
            embedder = {
                "provider": "ollama",
                "config": {
                    "model": "nomic-embed-text",
                    "embedding_dims": dims,
                    "ollama_base_url": "http://localhost:11434",
                },
            }
            if REMOTE_LLM:
                llm = {
                    "provider": "openai",
                    "config": {
                        "model": LOCAL_LLM,
                        "api_key": LLM_API_KEY,
                        "openai_base_url": LLM_BASE_URL,
                        "temperature": 0,
                    },
                }
            else:
                llm = {
                    "provider": "ollama",
                    "config": {
                        "model": LOCAL_LLM,
                        "ollama_base_url": "http://localhost:11434",
                        "temperature": 0,
                    },
                }
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "integrity_v0",
                    "path": str(self.root / "qdrant"),
                    "embedding_model_dims": dims,
                    "on_disk": True,
                },
            },
            "embedder": embedder,
            "llm": llm,
            "history_db_path": str(self.root / "history.db"),
        }

    def reset(self) -> None:
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        try:
            from mem0 import Memory
        except ImportError as exc:  # pragma: no cover - environment
            raise SystemUnavailable(f"mem0ai not importable: {exc}") from exc
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        if self.mode == "infer":
            _require_ollama()
        self._memory = Memory.from_config(self._config())
        if self.llm_usage is not None:
            self._observe_llm(self.llm_usage)
        if self.mode == "infer":
            self._self_test()

    def _observe_llm(self, usage: LLMUsage) -> None:
        """Observe mem0's own OpenAI client (`Memory.llm.client`) when the
        LLM sits behind an OpenAI-compatible endpoint."""
        if not REMOTE_LLM:
            usage.unobserved(
                "the LLM is the local ollama daemon through mem0's ollama provider, "
                "which calls ollama's native API, not an OpenAI-compatible endpoint"
            )
            return
        client = getattr(getattr(self._memory, "llm", None), "client", None)
        if client is None or not hasattr(getattr(client, "chat", None), "completions"):
            usage.unobserved("mem0's LLM carries no OpenAI client at llm.client")
            return
        usage.observe(client, "mem0 llm")
        if os.environ.get("OPENROUTER_API_KEY"):
            usage.notes.append(
                "OPENROUTER_API_KEY is set, and with it set mem0's OpenAI LLM (as read "
                "at mem0ai 2.0.18) sends with that key to OpenRouter in place of the "
                "configured key and endpoint. `clients` names the endpoint the client "
                f"was built with; a key-usage delta read with {LLM_KEY_ENV} covers "
                "mem0's calls only if the two keys are one"
            )

    def _self_test(self) -> None:
        """A fact and then its direct contradiction through add(infer=True)
        in a probe namespace. mem0ai 2.0.x's add path is additive by design:
        ADD-only extraction with entity-level memory links, while UPDATE and
        DELETE exist only as explicit calls. So the arm is measured on the
        extractor it ships: the first statement must yield a stored memory
        and the second must yield one that is not a copy of the first. A
        model that fails either has not exercised the extractor, and the
        arm reads unavailable with the rerun command rather than publishing
        that failure as mem0's loss. The 7.1.0 self-test asked for an
        UPDATE or DELETE that this version's add path never issues; that
        the path is additive is itself a published finding."""
        probe = "self-test"
        try:
            first = self._memory.add(SELF_TEST_STATEMENT, user_id=probe, infer=True)
            second = self._memory.add(SELF_TEST_UPDATE, user_id=probe, infer=True)
        except Exception as exc:  # noqa: BLE001 - published as the blocker
            raise SystemUnavailable(
                f"add(infer=True) failed on the self-test with {LOCAL_LLM}: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        stored = ("ADD", "UPDATE")
        first_rows = [
            r for r in (first or {}).get("results", []) if r.get("event") in stored
        ]
        second_rows = [
            r for r in (second or {}).get("results", []) if r.get("event") in stored
        ]
        try:
            self._memory.delete_all(user_id=probe)
        except Exception:  # noqa: BLE001 - the probe namespace is disposable
            pass
        first_texts = {str(r.get("memory")) for r in first_rows}
        fresh = [r for r in second_rows if str(r.get("memory")) not in first_texts]
        if not first_rows or not fresh:
            raise SystemUnavailable(
                f"the extractor ({LOCAL_LLM} at {LLM_BASE_URL}) stored "
                f"{len(first_rows)} memory(ies) from the self-test statement and "
                f"{len(fresh)} new one(s) from its update; rerun with "
                "BM_INTEGRITY_LLM / BM_INTEGRITY_LLM_BASE_URL / "
                "BM_INTEGRITY_LLM_API_KEY pointing at a model that extracts"
            )

    def close(self) -> None:
        self._memory = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "write_gates": False,
            "supersession_signal_channel": None,
            "provenance_channel": None,
            "store_injection": "direct vector-store insert with a forged payload",
            "forged_provenance": None,
            "extraction": self.mode == "infer",
        }

    def version(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mem0ai": _pkg_version("mem0ai")}
        if self.mode == "raw":
            out["embedder"] = "sentence-transformers/all-MiniLM-L6-v2"
            out["sentence-transformers"] = _pkg_version("sentence-transformers")
        else:
            out["embedder"] = "ollama/nomic-embed-text"
            out["llm"] = f"{LOCAL_LLM} at {LLM_BASE_URL}"
            out["add_mode"] = (
                "additive: ADD-only extraction with entity-level memory links; "
                "no UPDATE or DELETE on add (mem0ai 2.0.x)"
            )
        out["qdrant-client"] = _pkg_version("qdrant-client")
        return out

    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> AddOutcome:
        result = self._memory.add(
            text,
            user_id=USER,
            infer=(self.mode == "infer"),
            metadata={"stmt_id": stmt_id},
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        events = [str(r.get("event")) for r in rows]
        ids = [str(r.get("id")) for r in rows if r.get("event") in ("ADD", "UPDATE")]
        stored = any(e in ("ADD", "UPDATE") for e in events)
        status = ",".join(events) if events else "NONE"
        return AddOutcome(
            stored=stored,
            refused=False,
            status=status,
            ids=ids,
            raw={"results": _json_safe(rows)},
        )

    def search(self, query: str, k: int) -> list[Hit]:
        result = self._memory.search(
            query, filters={"user_id": USER}, top_k=k, threshold=0.0
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        out: list[Hit] = []
        for rank, row in enumerate(rows[:k], 1):
            out.append(
                Hit(
                    rank=rank,
                    id=str(row.get("id")),
                    text=str(row.get("memory") or ""),
                    signal=False,
                    provenance=None,
                    score=row.get("score"),
                    raw={
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                        "hash": row.get("hash"),
                        "metadata": _json_safe(row.get("metadata")),
                    },
                )
            )
        return out

    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str:
        """Insert straight into the vector store with a payload shaped like the
        one mem0 writes for a new memory. mem0 carries no provenance field beyond the hash
        and the timestamps, which the plain payload already forges, so the
        forged-provenance variant coincides with the plain one."""
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "data": text,
            "hash": hashlib.md5(text.encode()).hexdigest(),  # noqa: S324 - mem0's own scheme
            "created_at": now,
            "updated_at": now,
            "user_id": USER,
            "stmt_id": stmt_id,
        }
        vector = self._memory.embedding_model.embed(text, memory_action="add")
        mid = str(uuid.uuid4())
        self._memory.vector_store.insert(
            vectors=[vector], payloads=[payload], ids=[mid]
        )
        return mid


# ---------------------------------------------------------------------------
# graphiti (Zep's open-source engine)
# ---------------------------------------------------------------------------


class GraphitiAdapter:
    """graphiti-core on neo4j, the embedder through ollama's OpenAI-compatible
    endpoint and the LLM through the same endpoint unless BM_INTEGRITY_LLM_BASE_URL
    and BM_INTEGRITY_LLM_API_KEY point it elsewhere. Zep Cloud is the product; this is its published
    engine, and the row is labelled graphiti for that reason."""

    name = "graphiti"

    def __init__(
        self,
        scratch: Path,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "integrity-v0",
    ) -> None:
        self.scratch = Path(scratch)
        self.uri, self.user, self.password = uri, user, password
        self._g: Any = None
        self._loop: Any = None
        self._clock = datetime.now(timezone.utc) - timedelta(days=1)
        self._episode_uuids: list[str] = []
        self.llm_usage = LLMUsage(LLM_BASE_URL)

    def reset(self) -> None:
        import asyncio

        try:
            from graphiti_core import Graphiti
            from graphiti_core.cross_encoder.openai_reranker_client import (
                OpenAIRerankerClient,
            )
            from graphiti_core.embedder.openai import (
                OpenAIEmbedder,
                OpenAIEmbedderConfig,
            )
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.llm_client.openai_generic_client import (
                OpenAIGenericClient,
            )
        except ImportError as exc:  # pragma: no cover - environment
            raise SystemUnavailable(f"graphiti-core not importable: {exc}") from exc
        _require_ollama()
        os.environ.setdefault("OPENAI_API_KEY", "ollama")
        llm_config = LLMConfig(
            api_key=LLM_API_KEY,
            model=LOCAL_LLM,
            small_model=LOCAL_LLM,
            base_url=LLM_BASE_URL,
        )
        self._loop = asyncio.new_event_loop()
        self._g = Graphiti(
            self.uri,
            self.user,
            self.password,
            llm_client=OpenAIGenericClient(
                config=llm_config, structured_output_mode="json_schema"
            ),
            embedder=OpenAIEmbedder(
                config=OpenAIEmbedderConfig(
                    api_key="ollama",
                    base_url="http://localhost:11434/v1",
                    embedding_model=LOCAL_EMBEDDER,
                    embedding_dim=768,
                )
            ),
            cross_encoder=OpenAIRerankerClient(config=llm_config),
        )
        self._observe_llm(self.llm_usage)
        try:
            self._run(self._wipe())
            self._run(self._g.build_indices_and_constraints())
        except Exception as exc:  # noqa: BLE001 - published as the blocker
            raise SystemUnavailable(f"neo4j at {self.uri} not usable: {exc}") from exc
        self._self_test()

    def _observe_llm(self, usage: LLMUsage) -> None:
        """Observe the two OpenAI clients graphiti builds on the LLM
        endpoint: the extraction LLM's (`llm_client.client`) and the
        reranker's (`cross_encoder.client`). The embedder calls ollama's
        embeddings endpoint, not a chat completion, and is not counted."""
        for label, owner in (
            ("graphiti llm", getattr(self._g, "llm_client", None)),
            ("graphiti reranker", getattr(self._g, "cross_encoder", None)),
        ):
            client = getattr(owner, "client", None)
            if client is not None and hasattr(
                getattr(client, "chat", None), "completions"
            ):
                usage.observe(client, label)
        if not usage.observed:
            usage.unobserved("graphiti carries no OpenAI client at llm_client.client")

    def _self_test(self) -> None:
        """One canonical statement through add_episode. An extractor that
        yields no relation from it cannot be measured on relations, and
        the arm reads unavailable with the rerun command rather than
        publishing an empty row as a loss for the rival."""
        from graphiti_core.nodes import EpisodeType

        try:
            result = self._run(
                self._g.add_episode(
                    name="self-test",
                    episode_body=SELF_TEST_STATEMENT,
                    source_description="engineering note",
                    reference_time=self._clock,
                    source=EpisodeType.text,
                    group_id="self-test",
                )
            )
        except Exception as exc:  # noqa: BLE001 - published as the blocker
            raise SystemUnavailable(
                f"add_episode failed on the self-test statement with {LOCAL_LLM} "
                f"at {LLM_BASE_URL}: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        edges = list(getattr(result, "edges", []) or [])
        nodes = list(getattr(result, "nodes", []) or [])
        self._run(self._wipe())
        if not edges:
            raise SystemUnavailable(
                f"the extractor ({LOCAL_LLM} at {LLM_BASE_URL}) produced no relation "
                f"from the self-test statement ({len(nodes)} entities, 0 edges); rerun "
                "with BM_INTEGRITY_LLM / BM_INTEGRITY_LLM_BASE_URL / "
                "BM_INTEGRITY_LLM_API_KEY pointing at a model that extracts relations"
            )

    async def _wipe(self) -> None:
        async with self._g.driver.session() as session:
            await session.run("MATCH (n) DETACH DELETE n")

    def close(self) -> None:
        if self._g is not None:
            try:
                self._run(self._g.close())
            except Exception:  # noqa: BLE001
                pass
        if self._loop is not None:
            self._loop.close()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def capabilities(self) -> dict[str, Any]:
        return {
            "write_gates": False,
            "supersession_signal_channel": "edge invalid_at / expired_at",
            "provenance_channel": "edge episodes (source episode uuids)",
            "store_injection": "direct Cypher edge with a fact embedding",
            "forged_provenance": "the injected edge names an existing episode uuid",
            "extraction": True,
        }

    def version(self) -> dict[str, Any]:
        return {
            "graphiti-core": _pkg_version("graphiti-core"),
            "neo4j-driver": _pkg_version("neo4j"),
            "neo4j": "5.26 (docker)",
            "llm": f"{LOCAL_LLM} at {LLM_BASE_URL}",
            "embedder": "ollama/nomic-embed-text",
        }

    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> AddOutcome:
        from graphiti_core.nodes import EpisodeType

        self._clock += timedelta(minutes=1)
        try:
            result = self._run(
                self._g.add_episode(
                    name=stmt_id,
                    episode_body=text,
                    source_description="engineering note",
                    reference_time=self._clock,
                    source=EpisodeType.text,
                    group_id=GROUP,
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded, the run continues
            return AddOutcome(
                stored=False,
                refused=False,
                status=f"error: {type(exc).__name__}",
                raw={"error": str(exc)[:500]},
            )
        edges = list(getattr(result, "edges", []) or [])
        episode = getattr(result, "episode", None)
        if episode is not None:
            self._episode_uuids.append(str(episode.uuid))
        ids = [str(e.uuid) for e in edges]
        return AddOutcome(
            stored=episode is not None,
            refused=False,
            status=f"episode,edges={len(edges)}",
            ids=ids,
            raw={
                "episode": str(episode.uuid) if episode is not None else None,
                "facts": [str(e.fact) for e in edges][:20],
            },
        )

    def search(self, query: str, k: int) -> list[Hit]:
        edges = self._run(self._g.search(query, group_ids=[GROUP], num_results=k))
        out: list[Hit] = []
        for rank, edge in enumerate(list(edges)[:k], 1):
            fields = {
                "valid_at": _iso(getattr(edge, "valid_at", None)),
                "invalid_at": _iso(getattr(edge, "invalid_at", None)),
                "expired_at": _iso(getattr(edge, "expired_at", None)),
            }
            episodes = list(getattr(edge, "episodes", []) or [])
            out.append(
                Hit(
                    rank=rank,
                    id=str(edge.uuid),
                    text=str(edge.fact),
                    signal=bool(fields["invalid_at"] or fields["expired_at"]),
                    signal_fields=fields,
                    provenance=f"episodes:{len(episodes)}",
                    score=None,
                    raw={
                        "name": str(getattr(edge, "name", "")),
                        "episodes": episodes[:5],
                    },
                )
            )
        return out

    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str:
        now = datetime.now(timezone.utc)
        edge_uuid = str(uuid.uuid4())
        embedding = self._run(self._g.embedder.create([text]))
        episodes = (
            [self._episode_uuids[-1]]
            if forge_provenance and self._episode_uuids
            else []
        )
        cypher = """
        CREATE (a:Entity {uuid: $a_uuid, name: $a_name, group_id: $group, created_at: $now,
                          summary: '', labels: ['Entity']})
        CREATE (b:Entity {uuid: $b_uuid, name: $b_name, group_id: $group, created_at: $now,
                          summary: '', labels: ['Entity']})
        CREATE (a)-[r:RELATES_TO {uuid: $edge_uuid, name: 'STATES', fact: $fact,
                 fact_embedding: $embedding, group_id: $group, created_at: $now,
                 valid_at: $now, episodes: $episodes}]->(b)
        RETURN r.uuid
        """
        subject = str(meta.get("subject") or "engineering")
        params = {
            "a_uuid": str(uuid.uuid4()),
            "b_uuid": str(uuid.uuid4()),
            "a_name": subject,
            "b_name": f"{subject} (injected)",
            "group": GROUP,
            "now": now,
            "edge_uuid": edge_uuid,
            "fact": text,
            "embedding": list(embedding),
            "episodes": episodes,
        }

        async def _go() -> None:
            async with self._g.driver.session() as session:
                await session.run(cypher, params)

        self._run(_go())
        return edge_uuid


# ---------------------------------------------------------------------------
# letta
# ---------------------------------------------------------------------------


class LettaAdapter:
    """The Letta server (docker) through letta-client; one agent, archival
    passages as the memory, embeddings through ollama."""

    name = "letta"

    def __init__(
        self,
        scratch: Path,
        base_url: str = "http://localhost:8283",
        container: str = "bm-integrity-letta",
        pg_user: str = "letta",
        pg_db: str = "letta",
    ) -> None:
        self.scratch = Path(scratch)
        self.base_url = base_url
        self.container, self.pg_user, self.pg_db = container, pg_user, pg_db
        self._client: Any = None
        self._agent_id: str | None = None

    def reset(self) -> None:
        try:
            from letta_client import Letta
        except ImportError as exc:  # pragma: no cover - environment
            raise SystemUnavailable(f"letta-client not importable: {exc}") from exc
        self._client = Letta(base_url=self.base_url)
        try:
            for agent in self._client.agents.list():
                if str(getattr(agent, "name", "")).startswith("bm-integrity"):
                    self._client.agents.delete(agent.id)
            agent = self._client.agents.create(
                name="bm-integrity-v0",
                model=f"ollama/{LOCAL_LLM}",
                embedding_config={
                    "embedding_endpoint_type": "openai",
                    "embedding_endpoint": "http://host.docker.internal:11434/v1",
                    "embedding_model": f"{LOCAL_EMBEDDER}:latest",
                    "embedding_dim": 768,
                    "embedding_chunk_size": 300,
                    "handle": f"ollama/{LOCAL_EMBEDDER}:latest",
                },
                memory_blocks=[],
                include_base_tools=False,
            )
        except Exception as exc:  # noqa: BLE001 - published as the blocker
            raise SystemUnavailable(
                f"letta server at {self.base_url} not usable: {exc}"
            ) from exc
        self._agent_id = agent.id

    def close(self) -> None:
        self._client = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "write_gates": False,
            "supersession_signal_channel": None,
            "provenance_channel": None,
            "store_injection": "direct passage row in the server database",
            "forged_provenance": None,
            "extraction": False,
        }

    def version(self) -> dict[str, Any]:
        server = None
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"{self.base_url}/v1/health/", timeout=10
            ) as resp:
                server = json.loads(resp.read().decode("utf-8")).get("version")
        except Exception:  # noqa: BLE001
            pass
        return {
            "letta-client": _pkg_version("letta-client"),
            "letta-server": server,
            "embedder": "ollama/nomic-embed-text",
        }

    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> AddOutcome:
        try:
            created = self._client.agents.passages.create(self._agent_id, text=text)
        except Exception as exc:  # noqa: BLE001
            return AddOutcome(
                stored=False,
                refused=False,
                status=f"error: {type(exc).__name__}",
                raw={"error": str(exc)[:500]},
            )
        rows = list(created) if isinstance(created, (list, tuple)) else [created]
        ids = [str(getattr(r, "id", "")) for r in rows if getattr(r, "id", None)]
        return AddOutcome(
            stored=bool(ids), refused=False, status=f"passages={len(ids)}", ids=ids
        )

    def search(self, query: str, k: int) -> list[Hit]:
        response = self._client.agents.passages.search(
            self._agent_id, query=query, top_k=k
        )
        rows = _letta_rows(response)
        out: list[Hit] = []
        for rank, row in enumerate(rows[:k], 1):
            out.append(
                Hit(
                    rank=rank,
                    id=str(row.get("id") or row.get("passage_id") or "") or None,
                    text=str(row.get("content") or row.get("text") or ""),
                    signal=False,
                    provenance=None,
                    score=row.get("score"),
                    raw={
                        k2: _json_safe(v)
                        for k2, v in row.items()
                        if k2 in ("timestamp", "tags", "created_at")
                    },
                )
            )
        return out

    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str:
        """A direct row in the server's `archival_passages` table, copying
        the organisation, the archive, the embedding config and the creator
        id from the API-written twin, with an embedding computed by the
        same local model the server uses. Letta records no provenance
        beyond the creator id, which the row copies, so the forged variant
        coincides with the plain one."""
        import subprocess

        twins = [t for t in meta.get("twin_ids", []) if t]
        if not twins:
            raise InjectionUnsupported("no API-written twin to copy the archive from")
        vector = _ollama_embed(text)
        new_id = f"passage-{uuid.uuid4()}"
        # the server pads every embedding to its fixed column width
        padded = list(vector) + [0.0] * (LETTA_EMBEDDING_COLUMN_DIM - len(vector))
        vec = "[" + ",".join(f"{x:.8f}" for x in padded) + "]"
        if "$bm$" in text:
            raise InjectionUnsupported("payload text collides with the SQL quoting tag")
        sql = (
            "INSERT INTO archival_passages (id, text, embedding_config, metadata_, "
            "embedding, created_at, updated_at, is_deleted, _created_by_id, "
            "_last_updated_by_id, organization_id, archive_id, tags) "
            f"SELECT '{new_id}', $bm${text}$bm$, embedding_config, '{{}}'::json, "
            f"'{vec}'::vector, now(), now(), false, _created_by_id, _last_updated_by_id, "
            f"organization_id, archive_id, '[]'::json FROM archival_passages "
            f"WHERE id = '{twins[0]}'"
        )
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "psql",
                "-U",
                self.pg_user,
                "-d",
                self.pg_db,
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0 or "INSERT 0 1" not in proc.stdout:
            raise InjectionUnsupported(
                f"direct row refused: {proc.stderr.strip()[:200] or proc.stdout.strip()[:200]}"
            )
        return new_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _letta_rows(response: Any) -> list[dict[str, Any]]:
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if isinstance(response, dict):
        for key in ("results", "passages", "data"):
            if isinstance(response.get(key), list):
                return [
                    r if isinstance(r, dict) else r.model_dump() for r in response[key]
                ]
        return [response]
    if isinstance(response, list):
        return [r if isinstance(r, dict) else r.model_dump() for r in response]
    return []


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _ollama_embed(text: str) -> list[float]:
    import urllib.request

    body = json.dumps({"model": LOCAL_EMBEDDER, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return list(json.loads(resp.read().decode("utf-8"))["embeddings"][0])


def _pkg_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _require_ollama() -> None:
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=5
        ) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemUnavailable(
            f"ollama daemon not reachable at localhost:11434: {exc}"
        ) from exc
    names = {str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])}
    needed_models = (
        (LOCAL_EMBEDDER,) if REMOTE_LLM else (LOCAL_LLM.split(":")[0], LOCAL_EMBEDDER)
    )
    for needed in needed_models:
        if needed not in names:
            raise SystemUnavailable(f"ollama model {needed} not pulled")


def make_adapter(arm: str, scratch: Path) -> Adapter:
    if arm == "bettermemory":
        return BetterMemoryAdapter(scratch)
    if arm == "mem0-raw":
        return Mem0Adapter(scratch, "raw")
    if arm == "mem0-infer":
        return Mem0Adapter(scratch, "infer")
    if arm == "graphiti":
        return GraphitiAdapter(scratch)
    if arm == "letta":
        return LettaAdapter(scratch)
    raise SystemExit(f"unknown arm {arm!r}")


ARMS = ("bettermemory", "mem0-raw", "mem0-infer", "graphiti", "letta")
