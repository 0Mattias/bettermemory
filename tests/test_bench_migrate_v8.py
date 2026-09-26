"""Tests for the migration-parity harness, `bench/parity/migrate_v8.py`.

The harness migrates a v8 directory into the bettermemory 9 store,
mirrors it back out, and compares. Its determinism is pinned here on a
small corpus, and the committed artifact is checked against the tree:
the corpus it names is the one on disk, and it records no differing file.
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
_ARTIFACT = _HERE / "results" / "migrate-v8-8.0.0-2026-09-26.json"


def _load() -> ModuleType:
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    spec = importlib.util.spec_from_file_location(
        "bench_parity_migrate_v8", _HERE / "migrate_v8.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_parity_migrate_v8"] = module
    spec.loader.exec_module(module)
    return module


harness = _load()


def test_the_public_fixture_migrates_and_mirrors_identically_on_a_subset(
    tmp_path: Path,
) -> None:
    corpus_path, _ = _subset(tmp_path)
    v8_root = tmp_path / "v8"
    built = harness.build_public_v8(v8_root, corpus_path)
    assert built["tombstones"] == 5 and built["episodes"] == 4
    assert built["events"] == 43

    artifact = harness.run_migration(tmp_path / "run", v8_root)
    migrated = artifact["migrate"]
    assert migrated["memories"]["imported"] == built["active"]
    assert migrated["tombstones"]["imported"] == 5
    assert migrated["tombstones"]["legacy_names"] == 1
    assert migrated["episodes"]["imported"] == 4
    assert migrated["events"]["imported"] == 43
    assert migrated["events"]["redacted"] == 9
    assert migrated["conflicts"]["imported"] == 1
    assert migrated["imports"]["imported"] == 1

    compare = artifact["mirror"]["compare"]
    assert compare["active"]["differing"] == []
    assert (
        compare["active"]["identical"] == compare["active"]["files"] == built["active"]
    )
    assert compare["tombstones"]["differing"] == []
    assert compare["tombstones"]["identical"] == 5
    assert len(compare["tombstones"]["renamed"]) == 1
    assert compare["episodes"]["differing"] == []
    assert compare["episodes"]["identical"] == 4
    assert compare["mirror_files"] == compare["source_files"]

    assert artifact["events"]["equal"] is True
    assert artifact["events"]["v8_kinds"]["migrate"] == 1
    assert artifact["eval"]["markdown_equal"] is True
    assert artifact["eval"]["alltime_equal"] is True
    assert artifact["order"] == {
        "index_present": True,
        "equal": True,
        "rows": built["active"],
    }
    assert artifact["candidates"] is None
    assert artifact["second_run"]["log_rows"] == 0
    assert artifact["second_run"]["events"] == 0
    assert artifact["verify"]["status"] == "ok"
    assert artifact["size"]["sqlite_bytes"] > 0
    assert "differing" in harness.summary(artifact)


def test_the_committed_artifact_matches_the_tree() -> None:
    artifact = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["kind"] == "migrate-v8/parity"
    compare = artifact["mirror"]["compare"]
    assert compare["active"]["differing"] == []
    assert compare["tombstones"]["differing"] == []
    assert compare["episodes"]["differing"] == []
    assert compare["active"]["files"] == 1075
    assert compare["tombstones"]["files"] == 5
    assert compare["episodes"]["files"] == 4
    assert artifact["events"]["equal"] is True
    assert artifact["eval"]["markdown_equal"] is True
    assert artifact["order"]["equal"] is True
    assert artifact["second_run"]["log_rows"] == 0
    assert artifact["verify"]["status"] == "ok"
