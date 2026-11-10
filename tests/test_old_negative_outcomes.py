"""Search supplies the full rejection window to its per-hit annotation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from .test_server_negative_outcomes import _call, _seed, _unwrap


@pytest.mark.parametrize("hours", [1, 48, 31 * 24])
async def test_search_retains_a_rejection_for_its_documented_window(
    tmp_path: Path, hours: int
) -> None:
    cfg = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    state = SessionState()
    with Store(cfg.resolved_directory()) as store:
        recorder = Recorder(store=store, session_id=state.session_id, enabled=True)
        server = build_server(config=cfg, store=store, state=state, recorder=recorder)
        memory_id = await _seed(server, "python package dependency records")
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        store.import_event(
            {
                "kind": "use",
                "ts": when.isoformat(),
                "session": "older-session",
                "ids": [memory_id],
                "outcome": "contradicted",
            },
            imported_from="synthetic-test",
        )
        hits: list[dict[str, Any]] = _unwrap(
            await _call(server, "memory_search", query="python")
        )
        hit = next(row for row in hits if row["id"] == memory_id)
        if hours < 30 * 24:
            assert hit["recent_negative_outcomes"][0]["outcome"] == "contradicted"
            await _call(
                server, "memory_record_use", memory_ids=[memory_id], outcome="applied"
            )
            after = _unwrap(await _call(server, "memory_search", query="python"))
            assert "recent_negative_outcomes" not in after[0]
        else:
            assert "recent_negative_outcomes" not in hit
