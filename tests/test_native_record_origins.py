"""Native origins survive the signed record and mirror boundaries."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from bettermemory._response import ResponseBuilder
from bettermemory.migrate_v8 import migrate
from bettermemory.mirror import write_mirror
from bettermemory.models import Episode, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_store import _memory

RAW = "/w/bytes-" + chr(0xDCFF)


def test_native_origins_survive_all_record_readers_and_lifecycle(
    tmp_path: Path,
) -> None:
    source = Origin(cwd=RAW, worktree_root=RAW, repo=RAW, branch=RAW)
    with Store.create(tmp_path / "store.sqlite") as store:
        record = store.put_memory(_memory("native origin", origin=source))
        reads = [
            store.get_memory(record.id),
            *store.get_many([record.id]),
            *store.load_many([record.id]),
            *store.iter_memories(),
            *(r.memory for r in store.iter_memory_rows()),
        ]
        assert all(r.origin == source for r in reads)
        store.update(record.model_copy(update={"body": "edited origin"}))
        assert store.get_memory(record.id).origin == source
        store.tombstone(record.id, reason="test")
        assert store.get_tombstone(record.id).origin == source
        assert store.restore(record.id).origin == source
        assert store.log_verify()["status"] == "ok"
        assert store.refold()["status"] == "ok"


def test_native_origins_survive_mirror_import_on_every_record_kind(
    tmp_path: Path,
) -> None:
    source = Origin(cwd=RAW, worktree_root=RAW, repo=RAW, branch=RAW)
    keys = tmp_path / "keys"
    with Store.create(tmp_path / "first.sqlite", keys_dir=keys) as store:
        active = store.put_memory(_memory("active origin", origin=source))
        removed = store.put_memory(_memory("removed origin", origin=source))
        store.tombstone(removed.id, reason="test")
        episode = store.put_episode(
            Episode(
                id=generate_ulid(),
                session_id="session",
                created=datetime.now(timezone.utc),
                body="native episode",
                origin=source,
                swarm_id="swarm",
            )
        )
        assert store.get_episode(episode.id).origin == source
        assert store.episodes_by_session("session")[0].origin == source
        assert store.episodes_by_swarm("swarm")[0].origin == source
        write_mirror(store, tmp_path / "mirror")
        assert store.log_verify()["status"] == "ok"
    migrate(tmp_path / "mirror", tmp_path / "second.sqlite", keys_dir=keys)
    with Store.open(
        tmp_path / "second.sqlite", keys_dir=keys, allow_rekey=True
    ) as store:
        assert store.get_memory(active.id).origin == source
        assert store.get_tombstone(removed.id).origin == source
        assert store.get_episode(episode.id).origin == source
        assert store.refold()["status"] == "ok"
        assert store.log_verify()["status"] == "ok"


def test_origin_response_carries_exact_bytes_without_invalid_unicode() -> None:
    origin = Origin(cwd=RAW, worktree_root=RAW, repo=RAW, branch=RAW)
    result = ResponseBuilder(stale_after_days=30).origin_to_dict(origin)
    assert result is not None
    for field in ("cwd", "worktree_root", "repo", "branch"):
        assert result[field] is None
        assert base64.b64decode(result[f"{field}_bytes_base64"]) == os.fsencode(RAW)
    json.dumps(result, ensure_ascii=False).encode("utf-8")
