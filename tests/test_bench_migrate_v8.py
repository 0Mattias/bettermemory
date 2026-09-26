"""Tests for the migration-parity harness, `bench/parity/migrate_v8.py`.

The harness migrates a v8 directory into the bettermemory 9 store,
mirrors it back out, and compares. Its run over the golden fixture under
tests/fixtures/v8/store is pinned here against the fixture's manifest,
and the committed artifact is checked against the tree: it names the
fixture, its counts are the fixture's, and it records no differing file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_HERE = _ROOT / "bench" / "parity"
_ARTIFACT = _HERE / "results" / "migrate-v8-8.0.0-2026-09-26.json"
_FIXTURE_DIR = _ROOT / "tests" / "fixtures" / "v8"
MANIFEST: dict[str, Any] = json.loads(
    (_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8")
)


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


def test_the_golden_fixture_migrates_and_mirrors_identically(tmp_path: Path) -> None:
    assert harness.FIXTURE == _FIXTURE_DIR / "store"
    artifact = harness.run_migration(tmp_path / "run", harness.FIXTURE)
    assert artifact["source"]["root"] == "tests/fixtures/v8/store"

    migrated = artifact["migrate"]
    active = len(MANIFEST["memories"])
    assert migrated["memories"]["imported"] == active == 3
    assert migrated["tombstones"]["imported"] == len(MANIFEST["tombstones"]) == 2
    assert migrated["tombstones"]["legacy_names"] == 1
    assert migrated["episodes"]["imported"] == len(MANIFEST["episodes"]) == 3
    assert migrated["episodes"]["sessions"] == 2
    assert migrated["events"]["imported"] == MANIFEST["events_total"]
    assert migrated["events"]["redacted"] == MANIFEST["events_redacted"]
    assert migrated["event_kinds"] == MANIFEST["event_kinds"]
    assert migrated["conflicts"]["imported"] == len(MANIFEST["conflicts"])
    assert migrated["imports"]["imported"] == len(MANIFEST["imports"])
    assert migrated["dropped"] == MANIFEST["dropped"]
    assert migrated["unknown"] == MANIFEST["unknown"]
    for key, count in MANIFEST["left"].items():
        assert migrated["left"][key] == count, key

    compare = artifact["mirror"]["compare"]
    assert compare["active"]["differing"] == []
    assert compare["active"]["identical"] == compare["active"]["files"] == active
    assert compare["tombstones"]["differing"] == []
    assert compare["tombstones"]["identical"] == 2
    assert len(compare["tombstones"]["renamed"]) == 1
    assert compare["episodes"]["differing"] == []
    assert compare["episodes"]["identical"] == 3
    assert compare["mirror_files"] == compare["source_files"] == 8

    assert artifact["events"]["equal"] is True
    assert artifact["events"]["v8_kinds"] == MANIFEST["event_kinds"]
    assert artifact["events"]["v8_kinds"]["migrate"] == 1
    assert artifact["eval"]["markdown_equal"] is True
    assert artifact["eval"]["alltime_equal"] is True
    # The index was built in the generator's directory-listing order; the
    # rowid order agrees on a filesystem that lists the same way, and the
    # set of ids agrees everywhere.
    assert artifact["order"]["index_present"] is True
    assert artifact["order"]["rows"] == active
    assert artifact["order"]["same_set"] is True
    assert artifact["second_run"]["log_rows"] == 0
    assert artifact["second_run"]["events"] == 0
    assert artifact["verify"]["status"] == "ok"
    assert artifact["size"]["sqlite_bytes"] > 0
    assert "differing" in harness.summary(artifact)


def test_the_committed_artifact_matches_the_tree() -> None:
    artifact = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["kind"] == "migrate-v8/parity"
    assert artifact["source"]["root"] == "tests/fixtures/v8/store"
    compare = artifact["mirror"]["compare"]
    assert compare["active"]["differing"] == []
    assert compare["tombstones"]["differing"] == []
    assert compare["episodes"]["differing"] == []
    assert compare["active"]["files"] == len(MANIFEST["memories"])
    assert compare["tombstones"]["files"] == len(MANIFEST["tombstones"])
    assert compare["episodes"]["files"] == len(MANIFEST["episodes"])
    assert artifact["migrate"]["events"]["imported"] == MANIFEST["events_total"]
    assert artifact["migrate"]["event_kinds"] == MANIFEST["event_kinds"]
    assert artifact["events"]["equal"] is True
    assert artifact["eval"]["markdown_equal"] is True
    assert artifact["order"]["equal"] is True
    assert artifact["order"]["same_set"] is True
    assert artifact["second_run"]["log_rows"] == 0
    assert artifact["verify"]["status"] == "ok"
