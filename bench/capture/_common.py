"""What the capture benches share: a scratch environment, an in-process
server over a scratch store, a tool call, and the CLI.

`scratch_env` must run before anything imports bettermemory: HOME,
BETTERMEMORY_DIR, BETTERMEMORY_STATE_DIR and BETTERMEMORY_KEYS_DIR all point
under one temporary directory, so the config file, the keys, the daemon
state and the store are the bench's own, never the user's. Every CLI call
inherits the same environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
RESULTS = Path(__file__).resolve().parent / "results"
#: The Stop session the benches' batches and tagged writes belong to.
SESSION = "5e1c0b2a-9d3f-4c11-8e7a-capturebench1"


def scratch_env(scratch: Path) -> dict[str, str]:
    """Point this process's environment (and so every child's) at
    `scratch`, and return it. Refuses a scratch dir inside a live store."""
    sys.path.insert(0, str(ROOT / "bench" / "daemon"))
    from latency import refuse_live_store  # type: ignore[import-not-found]

    refuse_live_store(scratch)
    for name in ("home", "store", "state", "keys"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(scratch / "home")
    os.environ["BETTERMEMORY_DIR"] = str(scratch / "store")
    os.environ["BETTERMEMORY_STATE_DIR"] = str(scratch / "state")
    os.environ["BETTERMEMORY_KEYS_DIR"] = str(scratch / "keys")
    os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
    os.environ.pop("CLAUDE_CODE_SESSION_ATTENDED", None)
    os.environ["PYTHONPATH"] = str(ROOT / "src")
    return dict(os.environ)


def write_config(text: str) -> Path:
    """The scratch user's config.toml, where `load_config` looks for it."""
    from bettermemory.config import default_config_path

    path = default_config_path()
    if not str(path).startswith(os.environ["HOME"]):
        raise SystemExit(f"config path {path} is outside the scratch HOME")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class World:
    """One in-process server over the store at `root`, as the daemon builds
    it, with its own session state."""

    def __init__(self, root: Path) -> None:
        from bettermemory.builder import build_server
        from bettermemory.config import Config, StorageConfig
        from bettermemory.events import Recorder
        from bettermemory.session import SessionState
        from bettermemory.store import Store

        self.root = root
        self.config = Config(storage=StorageConfig(directory=str(root)))
        self.store = Store(root)
        self.state = SessionState()
        self.server = build_server(
            config=self.config,
            store=self.store,
            state=self.state,
            recorder=Recorder(store=self.store, session_id=self.state.session_id),
        )

    def open(self, transcript_bytes: int = 100_000) -> Any:
        from bettermemory import capture

        return capture.open_batch(
            self.store,
            session_id=SESSION,
            transcript_bytes=transcript_bytes,
            offset=0,
            worktree_root=str(ROOT),
        )

    def close(self, batch: Any, outcome: str = "settled") -> None:
        from bettermemory import capture

        capture.close_batch(
            self.store,
            batch,
            outcome=outcome,
            watermark=batch.transcript_bytes if outcome == "settled" else batch.offset,
            transcript_bytes=batch.transcript_bytes,
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.server.call_tool(name, arguments)
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None) or []
        parsed = json.loads(content[0].text) if content else None
        if not isinstance(parsed, dict):
            raise SystemExit(f"{name} returned no JSON object")
        return parsed

    async def capture_write(
        self, batch: Any, content: str, scopes: list[str]
    ) -> dict[str, Any]:
        """A tagged memory_write. A write answered with a review is re-sent
        with the latency bench's answer (every related id unrelated, unsafe
        0, secret 0), as the chat model would answer it, at most twice."""
        from latency import review_answer  # type: ignore[import-not-found]

        arguments = {
            "content": content,
            "scopes": scopes,
            "source": f"capture:{batch.token}",
        }
        reply = await self.call("memory_write", arguments)
        for _ in range(2):
            if reply.get("status") != "review":
                break
            reply = await self.call(
                "memory_write", {**arguments, "review": review_answer(reply)}
            )
        return reply


def cli(*args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    """`python -m bettermemory ...` under this process's (scratch) env."""
    return subprocess.run(
        [PYTHON, "-m", "bettermemory", *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def stamp() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def write_result(name: str, doc: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / name
    out.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out
