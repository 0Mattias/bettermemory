"""The session brief's ranking reads no path claims.

`recall_brief._ranked` ranks the brief's records through `search.search`
and reads only the hits' ids, yet every hit carried a path-drift read:
the browse fill alone asks for every delivered record of the project,
and each read stats the record's cited and attested paths. A path on a
network mount that stopped answering blocked the hook's worker in stat()
for a result it then discarded (ENG-169 G2). `search.search` takes
``path_drift=False`` for such a caller, and the brief passes it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import recall_brief, search
from bettermemory.config import Config, StorageConfig
from bettermemory.models import Confidence, Memory, Source, generate_ulid

_HUNG = "hung-mount-share"


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _memory(tmp_path: Path) -> Memory:
    cited = tmp_path / _HUNG / "src" / "loader.py"
    return Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 2),
        updated=_utc(2026, 1, 2),
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=f"The loader lives in `{cited}`.\n",
        verified_paths=[str(cited)],
    )


def _note(seen: list[str], path: Any) -> None:
    if isinstance(path, (str, bytes, os.PathLike)) and _HUNG in os.fsdecode(path):
        seen.append(os.fsdecode(path))


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every stat and lstat of a path on the hung mount, recorded (and
    then answered, so a failing test reports rather than hangs), and every
    existence read: on Windows since Python 3.12, `os.path.exists` and
    `os.path.lexists` (which `Path.exists` calls from 3.13) read the path
    through ``nt._path_exists``, not through `os.stat`."""
    seen: list[str] = []
    real_stat, real_lstat = os.stat, os.lstat
    real_exists, real_lexists = os.path.exists, os.path.lexists

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        _note(seen, path)
        return real_stat(path, *args, **kwargs)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        _note(seen, path)
        return real_lstat(path, *args, **kwargs)

    def exists(path: Any) -> bool:
        _note(seen, path)
        return real_exists(path)

    def lexists(path: Any) -> bool:
        _note(seen, path)
        return real_lexists(path)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setattr(os.path, "lexists", lexists)
    return seen


def test_search_without_path_drift_stats_no_claimed_path(
    tmp_path: Path, stats: list[str]
) -> None:
    memory = _memory(tmp_path)
    read = search.search([memory], "", allow_empty_query=True, now=_utc(2026, 3, 1))
    assert [hit.id for hit in read] == [memory.id]
    assert stats, "premise: a search that reads drift stats the cited path"
    stats.clear()
    hits = search.search(
        [memory], "", allow_empty_query=True, now=_utc(2026, 3, 1), path_drift=False
    )
    assert [hit.id for hit in hits] == [memory.id]
    assert stats == []
    assert hits[0].path_drift_checked == 0
    assert hits[0].path_drift_missing_paths == []


@pytest.mark.parametrize("probe", ["", "loader"])
def test_the_brief_ranking_stats_no_claimed_path(
    tmp_path: Path, stats: list[str], probe: str
) -> None:
    memory = _memory(tmp_path)
    cfg = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    ranked = recall_brief._ranked(
        [memory],
        probe,
        limit=8,
        admission=cast(Any, None),
        pool=cast(Any, SimpleNamespace(corpus_stats_provider=None)),
        cfg=cfg,
        now=_utc(2026, 3, 1),
    )
    assert [m.id for m in ranked] == [memory.id]
    assert stats == []
