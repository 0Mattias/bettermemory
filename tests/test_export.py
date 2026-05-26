"""Tests for `bettermemory export`.

The CLI dumps a self-describing JSON document with all active memories
(and tombstones, by default). Round-trippability is the core promise:
every field needed to reconstruct a Memory or TombstonedMemory has to
land in the export. The shape carries `format_version` so future
breaking changes are detectable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.server import _cli_export
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config_for(tmp_path: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(),
    )


@pytest.fixture
def populated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Store]:
    """A store with two active memories and one tombstone, with
    `bettermemory.config.load_config` monkeypatched to return a Config
    pinned to tmp_path so `_cli_export` doesn't read the real user
    config. Pre-Round-3 this patched ``bettermemory.server.load_config``
    (a re-export) — the canonical home is ``bettermemory.config``, and
    ``cli/export.py`` now imports from there directly so the back-edge
    through ``server`` is gone."""
    monkeypatch.setattr(
        "bettermemory.config.load_config", lambda: _config_for(tmp_path)
    )
    store = Store(tmp_path)
    store.write(
        content="Project demo uses Postgres in prod.",
        scopes=["projects:demo"],
    )
    store.write(
        content="Infra: home lab runs on Tailscale.",
        scopes=["infrastructure"],
    )
    third = store.write(
        content="A memory that will be tombstoned.",
        scopes=["projects:demo"],
    )
    store.tombstone(third.id, reason="reconsidered")
    return tmp_path, store


# ---------------------------------------------------------------------------
# Stdout path
# ---------------------------------------------------------------------------


def test_export_to_stdout_includes_active_and_tombstones(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_export(output=None, include_tombstones=True, scopes=None)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["format_version"] == 1
    assert parsed["exported_at"].endswith("Z") or "+00:00" in parsed["exported_at"]
    assert parsed["source_directory"] == str(populated_store[0])

    # Two active memories were written; tombstoned one should not appear here.
    assert len(parsed["active_memories"]) == 2
    # Tombstone bucket present and contains the third memory.
    assert len(parsed["tombstoned_memories"]) == 1
    assert parsed["tombstoned_memories"][0]["removed_reason"] == "reconsidered"


def test_export_no_tombstones_omits_the_key_entirely(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-tombstones drops the key, not just empties it. Lets a
    consumer distinguish "didn't ask" from "asked, none present"."""
    _cli_export(output=None, include_tombstones=False, scopes=None)
    parsed = json.loads(capsys.readouterr().out)
    assert "tombstoned_memories" not in parsed
    assert len(parsed["active_memories"]) == 2


def test_export_active_memories_round_trip(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every field needed to reconstruct a Memory must land in the dump.
    Round-trippability is the contract."""
    _cli_export(output=None, include_tombstones=True, scopes=None)
    parsed = json.loads(capsys.readouterr().out)

    required_fields = {
        "id",
        "created",
        "updated",
        "scopes",
        "confidence",
        "source",
        "body",
        "origin",
        "last_verified_at",
    }
    for record in parsed["active_memories"]:
        assert required_fields <= set(record.keys()), (
            f"missing fields in active memory: {required_fields - set(record.keys())}"
        )

    required_tombstone_fields = required_fields | {
        "removed",
        "removed_reason",
        "removed_session",
    }
    for record in parsed["tombstoned_memories"]:
        assert required_tombstone_fields <= set(record.keys())


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------


def test_export_scope_filter_applies_to_active_and_tombstones(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_export(output=None, include_tombstones=True, scopes=["projects:demo"])
    parsed = json.loads(capsys.readouterr().out)

    # Only the projects:demo memory should land in active.
    assert len(parsed["active_memories"]) == 1
    assert "projects:demo" in parsed["active_memories"][0]["scopes"]

    # The tombstoned record is also tagged projects:demo, so it stays.
    assert len(parsed["tombstoned_memories"]) == 1
    assert "projects:demo" in parsed["tombstoned_memories"][0]["scopes"]


def test_export_scope_filter_with_no_matches_is_empty(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_export(output=None, include_tombstones=True, scopes=["projects:doesnotexist"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["active_memories"] == []
    assert parsed["tombstoned_memories"] == []


def test_export_invalid_scope_raises(
    populated_store: tuple[Path, Store],
) -> None:
    with pytest.raises(ValueError):
        _cli_export(output=None, include_tombstones=True, scopes=["NOT VALID"])


# ---------------------------------------------------------------------------
# --output PATH
# ---------------------------------------------------------------------------


def test_export_to_file_writes_to_path_with_status_on_stderr(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_path = tmp_path / "backup.json"
    _cli_export(output=str(out_path), include_tombstones=True, scopes=None)
    captured = capsys.readouterr()

    # Stdout stays clean; status line goes to stderr.
    assert captured.out == ""
    assert "Exported 2 active memories" in captured.err
    assert "+ 1 tombstones" in captured.err
    assert str(out_path) in captured.err

    # File on disk parses as the same payload.
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert parsed["format_version"] == 1
    assert len(parsed["active_memories"]) == 2
    assert len(parsed["tombstoned_memories"]) == 1


def test_export_to_file_no_tombstones_skips_count_in_summary(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_path = tmp_path / "active-only.json"
    _cli_export(output=str(out_path), include_tombstones=False, scopes=None)
    captured = capsys.readouterr()
    assert "tombstones" not in captured.err
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert "tombstoned_memories" not in parsed
