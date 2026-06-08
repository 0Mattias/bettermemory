"""Regression tests for the post-3.6.4 whole-codebase shippability sweep
(audit round 77, Branch B-full @ c6b3277).

One file per audit round keeps the sweep's verification cohesive and
reviewable. Each test reproduces the confirmed finding's failure mode and
asserts the fix: the two 🔴 findings (frontmatter read-path DoS,
embedding-encode fail-open) get live reproductions; the 🟡 findings get
behavioural guards.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bettermemory.config import (
    BehaviorConfig,
    Config,
    StorageConfig,
    load_config,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

_VALID_MEMORY = (
    "---\n"
    "id: {id}\n"
    "created: 2025-01-01T00:00:00+00:00\n"
    "updated: 2025-01-01T00:00:00+00:00\n"
    "scopes:\n"
    "- tools\n"
    "confidence: medium\n"
    "source: explicit-statement\n"
    "---\n"
    "{body}\n"
)


def _memory(body: str, scopes: list[str] | None = None) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


class _RaisingSemanticModel:
    """Loads fine, but raises at encode() time — models a runtime device /
    OOM / tokenizer fault on an otherwise-healthy model. The search and
    write-dedup paths must degrade to lexical, never crash."""

    def encode(self, text: str, *, normalize_embeddings: bool = False) -> list[float]:
        raise RuntimeError("simulated encode-time device fault")


# --------------------------------------------------------------------------
# Finding 1 (🔴) — _frontmatter read-path RecursionError fail-open
# --------------------------------------------------------------------------


def test_frontmatter_nesting_bomb_raises_value_error_not_recursion() -> None:
    """A deeply-nested-YAML frontmatter file drives the pure-Python
    SafeLoader past the recursion limit. RecursionError is NOT a
    yaml.YAMLError, so without an explicit catch it escapes loads() — and
    the store's malformed-file skip only catches (ValueError, KeyError,
    OSError). It must be translated to ValueError."""
    from bettermemory._frontmatter import loads

    depth = 6000
    bomb = "---\nid: " + "[" * depth + "]" * depth + "\n---\n\nbody\n"
    with pytest.raises(ValueError):
        loads(bomb)


def test_store_skips_nesting_bomb_file_and_stays_readable(tmp_path: Path) -> None:
    """The actual fail-open: one crafted file must not DoS reads of the
    WHOLE store. load_all() must skip the bomb and return the valid
    memory, not propagate a RecursionError."""
    from bettermemory.store import Store

    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "2025-01-01-good.md").write_text(
        _VALID_MEMORY.format(id=generate_ulid(), body="kubernetes notes"),
        encoding="utf-8",
    )
    depth = 6000
    (memory_dir / "2025-01-01-bomb.md").write_text(
        "---\nid: " + "[" * depth + "]" * depth + "\n---\n\nbody\n",
        encoding="utf-8",
    )

    store = Store(memory_dir)
    loaded = store.load_all()  # must NOT raise
    assert [m.body.strip() for m in loaded] == ["kubernetes notes"]


# --------------------------------------------------------------------------
# Finding 2 (🔴) — embedding encode() not fail-soft on live paths
# --------------------------------------------------------------------------


def test_hybrid_search_degrades_when_loaded_model_raises_at_encode() -> None:
    from bettermemory.search import search

    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    hits = search(
        [a, b], "python list", mode="hybrid", semantic_model=_RaisingSemanticModel()
    )
    # Did not raise; fused keyword+bm25 and still surfaced the match.
    assert any(h.id == a.id for h in hits)


def test_semantic_mode_degrades_to_keyword_when_model_raises() -> None:
    from bettermemory.search import search

    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    hits = search(
        [a, b], "python list", mode="semantic", semantic_model=_RaisingSemanticModel()
    )
    assert any(h.id == a.id for h in hits)


def test_find_similar_falls_back_to_jaccard_when_model_raises() -> None:
    from bettermemory.search import find_similar

    existing = [_memory("python list comprehension tips")]
    hits = find_similar(
        "python list comprehension tips",
        existing,
        semantic_model=_RaisingSemanticModel(),
    )
    # Did not raise; the Jaccard fallback still flags the near-duplicate.
    assert hits


def test_find_similar_tombstones_falls_back_to_jaccard_when_model_raises() -> None:
    from bettermemory.models import TombstonedMemory
    from bettermemory.search import find_similar_tombstones

    now = datetime.now(timezone.utc)
    tomb = TombstonedMemory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="python list comprehension tips",
        removed=now,
        removed_reason="superseded",
    )
    hits = find_similar_tombstones(
        "python list comprehension tips",
        [tomb],
        semantic_model=_RaisingSemanticModel(),
    )
    assert hits


def test_find_similar_fallback_uses_jaccard_thresholds_not_cosine() -> None:
    """Diff-audit catch: when encode() fails, the Jaccard fallback must use
    the Jaccard-natural thresholds (0.75/0.40), NOT the cosine thresholds
    (0.85/0.65) the write-dedup gate passes. Otherwise a near-duplicate
    (Jaccard ~0.80) the gate should BLOCK slips through as merely 'medium'
    and a silent parallel duplicate is committed."""
    from bettermemory.search import find_similar

    existing = [
        _memory("The user prefers code-driven tutorials over conceptual explanations")
    ]
    hits = find_similar(
        "User prefers code-driven tutorials rather than conceptual explanations",
        existing,
        semantic_model=_RaisingSemanticModel(),
        high_threshold=0.85,  # cosine defaults — exactly what the write-dedup gate passes
        medium_threshold=0.65,
    )
    assert hits, "near-duplicate should still be flagged via the Jaccard fallback"
    assert hits[0].relevance == "high", (
        f"expected 'high' (Jaccard 0.75 natural threshold), got "
        f"{hits[0].relevance!r} — fallback wrongly applied the cosine 0.85 threshold"
    )


def test_find_similar_tombstones_fallback_uses_jaccard_thresholds() -> None:
    """The tombstone-aware path has the same threshold contract."""
    from bettermemory.models import TombstonedMemory
    from bettermemory.search import find_similar_tombstones

    now = datetime.now(timezone.utc)
    tomb = TombstonedMemory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="The user prefers code-driven tutorials over conceptual explanations",
        removed=now,
        removed_reason="superseded",
    )
    hits = find_similar_tombstones(
        "User prefers code-driven tutorials rather than conceptual explanations",
        [tomb],
        semantic_model=_RaisingSemanticModel(),
        high_threshold=0.85,
        medium_threshold=0.65,
    )
    assert hits
    assert hits[0].relevance == "high-removed", (
        f"expected 'high-removed', got {hits[0].relevance!r}"
    )


# --------------------------------------------------------------------------
# Finding 3 (🟡) — scope-blind FTS candidate prefilter under-returns
# --------------------------------------------------------------------------


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_scoped_search_finds_in_scope_match_outranked_by_50_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the index path active, a scoped query whose single in-scope
    match is outranked by 50+ out-of-scope matches must still find it.
    Pre-fix the FTS prefilter capped at 50 candidates scope-blind, so the
    in-scope match (BM25 rank 52) was dropped before the scope filter ran
    and the search returned []."""
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    memory_dir = tmp_path / "memories"
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())

    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")

    # 51 short out-of-scope memories that all match "alpha" and outrank the
    # one long in-scope memory by BM25 length-normalization. Distinct tokens
    # keep the Jaccard write-dedup gate from blocking the near-duplicates.
    for i in range(51):
        await _call(
            server, "memory_write", content=f"alpha betagroup{i}", scopes=["other"]
        )
    long_filler = " ".join(f"filler{j}" for j in range(40))
    await _call(server, "memory_write", content=f"alpha {long_filler}", scopes=["rare"])

    hits = _unwrap(await _call(server, "memory_search", query="alpha", scopes=["rare"]))
    assert hits, "scoped search dropped the only in-scope match (scope-blind prefilter)"
    assert all("rare" in h["scopes"] for h in hits)


# --------------------------------------------------------------------------
# Finding 4 (🟡) — migrate.py forward-gate bypass + symlink follow
# --------------------------------------------------------------------------


def _write_memory_file(memory_dir: Path, name: str, content: str) -> Path:
    path = memory_dir / f"2025-01-01-{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_migration_skips_future_schema_version(tmp_path: Path) -> None:
    """The migrator must honour the same forward-compat gate both store
    load paths enforce — never stamp a current-semantics origin into a
    file whose schema_version is newer than this reader supports."""
    from bettermemory import _frontmatter as frontmatter
    from bettermemory.migrate import migrate_origin_in_directory
    from bettermemory.origin import Origin

    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    future = _write_memory_file(
        memory_dir,
        "future",
        "---\n"
        f"id: {generate_ulid()}\n"
        "schema_version: 2\n"
        "created: 2025-01-01T00:00:00+00:00\n"
        "updated: 2025-01-01T00:00:00+00:00\n"
        "scopes:\n- tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\nfuture body\n",
    )

    report = migrate_origin_in_directory(
        memory_dir, inferred=Origin(repo="git@github.com:example/foo.git")
    )

    assert report.updated == 0
    assert "origin" not in frontmatter.load(future).metadata


def test_migration_does_not_follow_symlink(tmp_path: Path) -> None:
    """A symlink `.md` in the memory dir (a hostile `sync pull` plant)
    must be skipped, not followed — the store iterators reject symlinks
    and the migrator must too."""
    from bettermemory.migrate import migrate_origin_in_directory
    from bettermemory.origin import Origin

    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    target = tmp_path / "secret.md"
    target.write_text(
        _VALID_MEMORY.format(id=generate_ulid(), body="secret target"),
        encoding="utf-8",
    )
    link = memory_dir / "2025-01-01-evil.md"
    link.symlink_to(target)

    report = migrate_origin_in_directory(
        memory_dir, inferred=Origin(repo="git@github.com:example/foo.git")
    )

    # The symlink was not scanned/migrated, and the target was not rewritten.
    assert report.scanned == 0
    assert report.updated == 0


# --------------------------------------------------------------------------
# Finding 5 (🟡) — events.py per-append store-dir scan
# --------------------------------------------------------------------------


def test_orphan_recovery_not_run_on_subthreshold_append(tmp_path: Path) -> None:
    """Orphan recovery does a full iterdir() of the (shared) store dir, so
    it must NOT run on the common no-rotation append path — only when a
    rotation is actually due."""
    from bettermemory.events import Recorder

    rec = Recorder(root=tmp_path, session_id="sess_test", max_bytes=10_000_000)
    with patch.object(rec, "_recover_orphan_rotations") as spy:
        rec.record("write", id="01HXYZ", scopes=["tools"])
        rec.record("show", id="01HXYZ")
    spy.assert_not_called()


def test_orphan_recovery_still_runs_on_rotation(tmp_path: Path) -> None:
    """Deferring recovery to the rotation path must not lose the recovery:
    an orphan `.rotating` from a prior crash is still reclaimed the next
    time a rotation fires."""
    from bettermemory.events import ROTATING_SUFFIX, Recorder

    orphan = tmp_path / (".events-20200101T000000Z" + ROTATING_SUFFIX)
    orphan.write_text('{"kind":"old","ts":"2020"}\n', encoding="utf-8")

    rec = Recorder(root=tmp_path, session_id="sess_test", max_bytes=1)
    # First append creates the active log; the second crosses max_bytes and
    # triggers a rotation, which runs orphan recovery.
    rec.record("write", id="01HXYZ", scopes=["tools"])
    rec.record("show", id="01HXYZ")

    assert not orphan.exists(), "orphan .rotating was not reclaimed on rotation"
    assert any(p.suffix == ".gz" for p in tmp_path.iterdir())


# --------------------------------------------------------------------------
# Finding 6 (🟡) — config.py input validation
# --------------------------------------------------------------------------


def test_bad_numeric_config_value_raises_located_error(tmp_path: Path) -> None:
    """A non-numeric value under a numeric key must fail with a clear,
    located error naming the key — not an opaque `invalid literal for
    int()` traceback escaping load_config and crashing `serve` startup."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[behavior]\ndefault_max_results = "abc"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="default_max_results"):
        load_config(config_path)


def test_scopes_allowed_string_scalar_rejected(tmp_path: Path) -> None:
    """`allowed = "myproject"` (forgotten brackets) must be rejected, not
    silently char-exploded into ['m', 'y', 'p', ...]."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[scopes]\nallowed = "myproject"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="allowed"):
        load_config(config_path)


def test_valid_config_values_still_coerce(tmp_path: Path) -> None:
    """The new coercion helpers preserve behaviour for valid inputs."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\n"
        "default_max_results = 7\n"
        "recency_boost_half_life_days = 12.5\n"
        '[scopes]\nallowed = ["tools", "projects:foo"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.default_max_results == 7
    assert cfg.behavior.recency_boost_half_life_days == 12.5
    assert cfg.scopes.allowed == ["tools", "projects:foo"]


# --------------------------------------------------------------------------
# Finding 7 (🟡) — reindex CLI missing the exit-2 hardening
# --------------------------------------------------------------------------


def _reindex_ctx(tmp_path: Path) -> SimpleNamespace:
    from bettermemory.store import Store

    mem = tmp_path / "mem"
    cfg = Config(storage=StorageConfig(directory=str(mem)), behavior=BehaviorConfig())
    return SimpleNamespace(config=cfg, directory=mem, store=Store(mem))


def test_reindex_write_failure_routes_through_parser_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SQLite write failure during rebuild must surface as a clean
    parser.error -> exit 2, matching sibling write commands — not an
    uncaught traceback + exit 1."""
    from bettermemory.cli import reindex as reindex_cmd

    monkeypatch.setattr(reindex_cmd, "cli_context", lambda: _reindex_ctx(tmp_path))

    def _boom(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr("bettermemory.index.rebuild", _boom)

    parser = argparse.ArgumentParser(prog="bettermemory reindex")
    with pytest.raises(SystemExit) as exc:
        reindex_cmd._cli_reindex(json_out=False, parser=parser)
    assert exc.value.code == 2


def test_reindex_write_failure_reraises_without_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `parser is None` fallback keeps the raw exception for
    programmatic / test callers."""
    from bettermemory.cli import reindex as reindex_cmd

    monkeypatch.setattr(reindex_cmd, "cli_context", lambda: _reindex_ctx(tmp_path))

    def _boom(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr("bettermemory.index.rebuild", _boom)

    with pytest.raises(OSError, match="disk full"):
        reindex_cmd._cli_reindex(json_out=False, parser=None)
