"""Tests for the candidate-parity harness, `bench/parity/sqlite_candidates.py`.

The harness compares the bettermemory 9 store's FTS5 candidate query with
the v8 index on the rank-parity fixture. Its own determinism is pinned
here on a small corpus, and the committed artifact is checked against the
tree: the corpus it names is the one on disk, and it records no differing
query.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from .test_bench_parity import _subset

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "parity"
_ARTIFACT = _HERE / "results" / "sqlite-candidates-8.0.0-2026-09-26.json"


def _load() -> ModuleType:
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    spec = importlib.util.spec_from_file_location(
        "bench_parity_sqlite_candidates", _HERE / "sqlite_candidates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_parity_sqlite_candidates"] = module
    spec.loader.exec_module(module)
    return module


harness = _load()


def test_the_v9_store_gives_the_v8_index_candidates_on_a_subset(
    tmp_path: Path,
) -> None:
    corpus_path, questions_path = _subset(tmp_path)
    first = harness.run_candidates(tmp_path / "a", corpus_path, questions_path)
    second = harness.run_candidates(tmp_path / "b", corpus_path, questions_path)
    assert first["differing"] == []
    assert first["queries"] == 36
    assert first["digest"] == second["digest"]
    assert first["verify"]["status"] == "ok"
    assert first["cost"]["puts"] == 40


def test_the_committed_artifact_matches_the_tree() -> None:
    artifact = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["differing"] == []
    assert artifact["queries"] == 360
    assert artifact["corpus"]["sha256"] == harness.parity._sha256(harness.parity.CORPUS)
    assert artifact["verify"]["status"] == "ok"
    assert len(artifact["rows"]) == artifact["queries"]
    assert all(row["same"] for row in artifact["rows"])
