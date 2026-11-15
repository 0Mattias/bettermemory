"""Fixtures 4b and 4c: search through the store, by caller.

A real store file holds the synthetic records of ``search.jsonl``, homed
in two projects (two git repositories with remotes), in ``global`` (a
caller outside any repository) and with no home at all. Three callers are
resolved where a request resolves them (`location.resolve_caller`, with
the directory as the request's), and each query runs the way
`handlers.search.memory_search` runs it: the pool from the FTS5 index
through the caller's admission (`resolve_search_pool`), ranked by
`search.search` with the pool's store statistics, the caller's anchoring
and the configured defaults. The clock is the fixture's (`_NOW`); the
handler reads the wall clock. One run has no admission (an offline
caller) and is compared with ``search.jsonl``'s in-memory ranking.

The same build also exercises store writes beyond the records (tombstone,
restore, episodes, verification, conflict, import, quarantine), and its
log rows are the shapes `gen_log` reads for those kinds (`log_rows`).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from bettermemory import _daemon_client, location
from bettermemory import search as bsearch
from bettermemory.admission import Admission
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.verify_anchoring import Anchoring
from bettermemory.handlers.search import resolve_search_pool
from bettermemory.membership import SEARCH_AUTO_SCOPE, SEARCH_UNSCOPED
from bettermemory.models import Memory
from bettermemory.store import Store

from ._common import hit_records, jsonl, raw_scores
from .gen_store import _NOW, _QUERIES, _memories

#: The git environment a resolution runs under: no user or system
#: configuration, a fixed identity, nothing pointing git elsewhere.
_GIT_CLEAR = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "BETTERMEMORY_WORKSPACE",
    _daemon_client.HARNESS_SESSION_ENV,
)

#: Where each record lives, by its index in the corpus (cycled): a
#: project, ``global``, or no home row.
_HOMES = ("alpha", "beta", "plain", None)
_CALLERS = ("alpha", "beta", "plain")
_MODES = ("hybrid", "bm25", "keyword")
_SURFACES = {"auto_scope": SEARCH_AUTO_SCOPE, "unscoped": SEARCH_UNSCOPED}


@contextmanager
def _neutral_git(tmp: Path) -> Iterator[None]:
    saved = dict(os.environ)
    empty = tmp / "gitconfig-empty"
    empty.write_text("")
    try:
        for var in _GIT_CLEAR:
            os.environ.pop(var, None)
        os.environ.update(
            GIT_CONFIG_GLOBAL=str(empty),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _repo(path: Path, remote: str, day: int) -> None:
    path.mkdir()
    stamp = f"2026-01-{day:02d}T00:00:00Z"
    env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, env=env
        )

    git("init", "-q", "-b", "main")
    (path / "README.md").write_text(f"{path.name}\n")
    git("add", "-A")
    git("commit", "-q", "-m", f"{path.name} initial")
    git("remote", "add", "origin", remote)


class _Built:
    def __init__(self) -> None:
        self.homes: dict[str, str | None] = {}
        self.callers: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.rows: list[tuple[str, str | None, str]] = []


def _labels(project: str | None, ids: dict[str, str]) -> str | None:
    return None if project is None else ids.get(project, project)


def _run(
    store: Store,
    cfg: Config,
    query: str,
    mode: str,
    admission: Admission | None,
    anchoring: Anchoring | None,
) -> dict[str, Any]:
    pool = resolve_search_pool(store, query, admission=admission)
    with raw_scores() as raw:
        hits = bsearch.search(
            pool.memories,
            query,
            admission=admission,
            max_results=10,
            now=_NOW,
            half_life_days=cfg.behavior.recency_boost_half_life_days,
            mode=mode,  # type: ignore[arg-type]
            corpus_stats_provider=pool.corpus_stats_provider,
            matched_leg_out={},
            rescue_expansion=False,
            conversational=cfg.behavior.conversational,
            anchoring=anchoring,
            hydrate=pool.hydrate,
        )
    return {"pool": [m.id for m in pool.memories], "hits": hit_records(hits, raw)}


def _extra_writes(store: Store, memories: list[Memory]) -> None:
    """The store writes beyond the records, for their log rows."""
    victim = memories[-1].id
    store.tombstone(victim, "superseded", now=_NOW)
    store.restore(victim)
    episode = store.write_episode(
        session_id="session-0001", body="Synthetic journal entry.", now=_NOW
    )
    store.delete_episode(episode.id)
    store.mark_verified(memories[0].id, verified_paths=["README.md"])
    store.put_conflict(
        {
            "id": "conflict-0001",
            "a_id": memories[0].id,
            "b_id": memories[1].id,
            "summary_a": "a",
            "summary_b": "b",
            "similarity": 0.75,
            "method": "jaccard",
            "detector": "fixture",
            "created": _NOW.isoformat(),
            "status": "open",
        }
    )
    store.delete_conflict("conflict-0001")
    store.put_import(
        "v8:fixture",
        content_hash="0" * 64,
        imported_at=_NOW.isoformat(),
        memory_id=None,
    )
    store.delete_import("v8:fixture")
    store.put_quarantine(
        "fixture.md", reason="unparsable", at=_NOW.isoformat(), detail="d", size=3
    )
    store.delete_quarantine("fixture.md")


@lru_cache(maxsize=1)
def build() -> _Built:
    built = _Built()
    with tempfile.TemporaryDirectory() as raw, _neutral_git(Path(raw)):
        tmp = Path(raw)
        _repo(tmp / "alpha", "https://example.invalid/fixture/alpha.git", 1)
        _repo(tmp / "beta", "https://example.invalid/fixture/beta.git", 2)
        (tmp / "plain").mkdir()
        store = Store.create(tmp / "store", keys_dir=tmp / "keys")
        try:
            cfg = Config(
                storage=StorageConfig(directory=str(store.path.parent)),
                behavior=BehaviorConfig(),
            )
            callers = {
                name: location.resolve_caller(store, cfg, str(tmp / name), session=None)
                for name in _CALLERS
            }
            ids = {
                str(c.resolution.project): name
                for name, c in callers.items()
                if c.resolution.project is not None
            }
            ids["global"] = "global"
            memories = _memories()
            for i, memory in enumerate(memories):
                home = _HOMES[i % len(_HOMES)]
                store.put_memory(
                    memory, home=None if home is None else callers[home].home()
                )
                built.homes[memory.id] = (
                    None if home is None else _labels(callers[home].home().project, ids)
                )
            for name, c in callers.items():
                built.callers[name] = {
                    "directory": name,
                    "outcome": c.resolution.outcome,
                    "project": _labels(c.resolution.project, ids),
                }
            for query in _QUERIES:
                for mode in _MODES:
                    run: dict[str, Any] = {"query": query, "mode": mode}
                    run["offline"] = _run(store, cfg, query, mode, None, None)
                    for name, c in callers.items():
                        anchoring = Anchoring(c.model, c.resolution)
                        for label, surface in _SURFACES.items():
                            run[f"{name}/{label}"] = _run(
                                store,
                                cfg,
                                query,
                                mode,
                                Admission.of(surface, c),
                                anchoring,
                            )
                    built.runs.append(run)
            _extra_writes(store, memories)
            built.rows = [
                (str(kind), session, str(payload))
                for kind, session, payload in store._conn.execute(
                    "SELECT kind, session, payload FROM log ORDER BY seq"
                )
            ]
        finally:
            store.close()
    return built


def log_rows() -> list[tuple[str, str | None, str]]:
    """(kind, session, payload text) of every row the build wrote."""
    return build().rows


def _in_memory(query: str, mode: str) -> list[dict[str, Any]]:
    bsearch.clear_token_cache()
    with raw_scores() as raw:
        hits = bsearch.search(
            _memories(),
            query,
            now=_NOW,
            max_results=10,
            path_drift=False,
            mode=mode,  # type: ignore[arg-type]
        )
    return hit_records(hits, raw)


def generate() -> dict[str, bytes]:
    built = build()
    records: list[Any] = [
        {
            "fixture": "search_store",
            "now": _NOW.isoformat(),
            "max_results": 10,
            "records": [{"id": rid, "home": home} for rid, home in built.homes.items()],
            "home_legend": (
                "alpha and beta: a project (a git repository with a remote); "
                "global: written outside any repository; null: no homes row"
            ),
            "callers": built.callers,
            "surfaces": _SURFACES,
            "runs": (
                "offline: no admission; '<caller>/<surface>': the caller's "
                "admission on that surface. pool: the candidate ids in pool "
                "order (FTS5 rank through the admission); hits: ranked."
            ),
        }
    ]
    for run in built.runs:
        offline = run["offline"]["hits"]
        memory = _in_memory(run["query"], run["mode"])
        records.append({**run, "in_memory_equal": offline == memory})
    return {"search_store.jsonl": jsonl(records)}
