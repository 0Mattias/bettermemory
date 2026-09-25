"""Tests for the rank-parity harness, `bench/parity/run.py`.

The harness is the instrument bettermemory 9 is gated on: the same
fixed queries must return the same ranked ids and relevance labels at
8.0.0 and at v9. An instrument that is not itself deterministic cannot
carry that claim, so its own determinism is pinned here, on a small
corpus, before any engine is compared.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "parity"
_RUNNER = _HERE / "run.py"
_RETRIEVAL = _ROOT / "bench" / "retrieval"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_parity_run", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_parity_run"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _subset(
    tmp_path: Path, *, n_docs: int = 40, n_questions: int = 12
) -> tuple[Path, Path]:
    """A small corpus whose first questions all have their gold document."""
    corpus = _rows(_RETRIEVAL / "corpus.jsonl")
    questions = _rows(_RETRIEVAL / "questions.jsonl")[:n_questions]
    wanted = {q["slug"] for q in questions}
    gold = [r for r in corpus if r["slug"] in wanted]
    filler = [r for r in corpus if r["slug"] not in wanted][: n_docs - len(gold)]
    corpus_path = tmp_path / "corpus.jsonl"
    questions_path = tmp_path / "questions.jsonl"
    corpus_path.write_text("".join(json.dumps(r) + "\n" for r in gold + filler))
    questions_path.write_text("".join(json.dumps(q) + "\n" for q in questions))
    return corpus_path, questions_path


def test_deterministic_id_is_a_valid_ulid_that_orders_by_ordinal() -> None:
    from bettermemory.models import is_valid_ulid

    first = runner.deterministic_id("alpha", 0)
    second = runner.deterministic_id("beta", 1)
    assert is_valid_ulid(first) and is_valid_ulid(second)
    assert first < second
    assert runner.deterministic_id("alpha", 0) == first
    assert runner.deterministic_id("alpha", 1) != first


def test_build_store_is_byte_deterministic(tmp_path: Path) -> None:
    corpus_path, _ = _subset(tmp_path)
    ids_a = runner.build_store(tmp_path / "a", corpus_path)
    ids_b = runner.build_store(tmp_path / "b", corpus_path)
    assert ids_a == ids_b
    files_a = sorted(p.name for p in (tmp_path / "a").glob("*.md"))
    files_b = sorted(p.name for p in (tmp_path / "b").glob("*.md"))
    assert files_a == files_b and len(files_a) == 40
    for name in files_a:
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()


def test_public_rows_are_deterministic_and_the_prefilter_engages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "10")
    corpus_path, questions_path = _subset(tmp_path)
    first = runner.run_public(tmp_path / "s1", corpus_path, questions_path)
    second = runner.run_public(tmp_path / "s2", corpus_path, questions_path)
    assert first["digest"] == second["digest"]
    assert first["rows"] == second["rows"]
    assert len(first["rows"]) == 12 * len(runner.PROBES)
    store_ids = set(runner.build_store(tmp_path / "s3", corpus_path).values())
    for row in first["rows"]:
        assert row["prefilter"]["engaged"] is True
        assert row["prefilter"]["pool_size"] <= runner.PREFILTER_CAP
        assert row["full"]["pool_size"] == 40
        for arm in ("full", "prefilter"):
            assert set(row[arm]["ids"]) <= store_ids
            assert len(row[arm]["ids"]) <= runner.MAX_RESULTS
            assert set(row[arm]["relevance"]) <= {"high", "medium", "low"}
            assert len(row[arm]["relevance"]) == len(row[arm]["ids"])


def test_compare_reports_the_differing_query_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "10")
    corpus_path, questions_path = _subset(tmp_path)
    base = runner.run_public(tmp_path / "s", corpus_path, questions_path)
    assert runner.compare(base, base) == []
    other = copy.deepcopy(base)
    row = next(r for r in other["rows"] if len(r["prefilter"]["ids"]) >= 2)
    row["prefilter"]["ids"][0], row["prefilter"]["ids"][1] = (
        row["prefilter"]["ids"][1],
        row["prefilter"]["ids"][0],
    )
    diffs = runner.compare(base, other)
    assert len(diffs) == 1
    assert diffs[0]["slug"] == row["slug"]
    assert diffs[0]["probe"] == row["probe"]
    assert diffs[0]["arm"] == "prefilter"


def test_committed_public_artifacts_match_the_tree() -> None:
    artifacts = sorted((_HERE / "results").glob("public-*.json"))
    if not artifacts:
        pytest.skip("no committed public artifact yet")
    corpus_sha = hashlib.sha256((_RETRIEVAL / "corpus.jsonl").read_bytes()).hexdigest()
    questions_sha = hashlib.sha256(
        (_RETRIEVAL / "questions.jsonl").read_bytes()
    ).hexdigest()
    for path in artifacts:
        artifact = json.loads(path.read_text())
        assert artifact["corpus_sha256"] == corpus_sha, path.name
        assert artifact["questions_sha256"] == questions_sha, path.name
        assert artifact["params"]["max_results"] == runner.MAX_RESULTS, path.name
        assert artifact["engine"]["prefilter_cap"] == runner.PREFILTER_CAP, path.name
        assert len(artifact["rows"]) == artifact["questions_n"] * len(runner.PROBES)
        assert artifact["digest"] == runner.digest(artifact["rows"]), path.name
        for row in artifact["rows"]:
            assert row["prefilter"]["engaged"] is True, (path.name, row["slug"])
