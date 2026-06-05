"""Tests for `bettermemory export`.

The CLI dumps a self-describing JSON document with all active memories
(and tombstones, by default). Round-trippability is the core promise:
every field needed to reconstruct a Memory or TombstonedMemory has to
land in the export. The shape carries `format_version` so future
breaking changes are detectable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from bettermemory.cli.export import add_subparser as export_add_subparser
from bettermemory.cli.export import run as export_run
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


def test_export_invalid_scope_via_cli_exits_clean_not_traceback(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Driven through `run()` with a real argparse subparser, a malformed
    --scope must exit 2 with a clean `bettermemory export: error: …`
    message on stderr — NOT an uncaught ValueError traceback. The
    `populated_store` fixture has already pinned `load_config` to tmp_path
    so the run reaches the scope-validation step against a real store."""
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "--scope", "NOT VALID"])

    with pytest.raises(SystemExit) as excinfo:
        export_run(args, sub_parser=export_parser)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    # No raw traceback should have leaked to the user.
    assert "Traceback (most recent call last)" not in err


def test_export_to_file_bad_parent_via_cli_exits_clean_not_traceback(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Driven through `run()` with a real subparser, `export -o` into a
    missing / non-directory parent must exit 2 with a clean error — NOT a
    raw FileNotFoundError traceback / exit 1. Same missing-OSError-arm
    class the 3.6.0 self-audit fixed for proposals / tombstones-restore /
    rename-scope; export was the missed sibling (caught by the post-3.6.0
    sweep). The direct `_cli_export` (parser=None) contract still raises
    raw — see test_export_to_file_missing_parent_dir_raises."""
    out_path = tmp_path / "nonexistent_subdir" / "backup.json"
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "-o", str(out_path)])

    with pytest.raises(SystemExit) as excinfo:
        export_run(args, sub_parser=export_parser)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback (most recent call last)" not in err


def test_export_to_file_write_oserror_via_cli_exits_clean_not_traceback(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine filesystem failure during the atomic write (read-only
    parent, ENOSPC, EACCES) — the parent IS a directory so the pre-check
    passes — also routes through `parser.error` -> exit 2 rather than a
    raw OSError traceback. Simulated by forcing atomic_write_bytes to raise."""
    import bettermemory.cli.export as export_mod

    out_path = tmp_path / "backup.json"  # parent (tmp_path) is a real dir

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(export_mod, "atomic_write_bytes", boom)

    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "-o", str(out_path)])

    with pytest.raises(SystemExit) as excinfo:
        export_run(args, sub_parser=export_parser)

    assert excinfo.value.code == 2
    assert "error:" in capsys.readouterr().err


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


def test_export_to_file_missing_parent_dir_raises(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
) -> None:
    """`bettermemory export -o /missing/dir/out.json` should raise loudly
    rather than silently creating the parent tree. Pre-3.2.1 the bare
    ``write_text`` raised FileNotFoundError for a missing parent; the
    Q29 migration to ``atomic_write_bytes`` inadvertently auto-created
    the parent (the helper has ``parents=True, exist_ok=True`` for
    fresh-install callers like ``init.py``). The pre-check in
    ``_cli_export`` restores the 3.2.0 contract — a typo'd ``-o`` path
    surfaces as a loud error, not a silently buried backup."""
    missing_parent = tmp_path / "nonexistent_subdir"
    out_path = missing_parent / "backup.json"
    assert not missing_parent.exists()

    with pytest.raises(FileNotFoundError) as exc_info:
        _cli_export(output=str(out_path), include_tombstones=True, scopes=None)

    # The message should name the missing parent so a user can spot the
    # typo without re-reading the command they just typed.
    assert str(missing_parent) in str(exc_info.value)

    # And the helper did not paper over it — the parent tree was not
    # created as a side effect of the failed call.
    assert not missing_parent.exists()


def test_export_to_file_parent_is_a_regular_file_raises_cleanly(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
) -> None:
    """`bettermemory export -o some-file/out.json` where ``some-file`` is a
    regular FILE must surface the same clean FileNotFoundError as the
    missing-parent case — NOT the confusing FileExistsError that
    ``atomic_write_bytes``'s ``mkdir(parents=True, exist_ok=True)`` would
    raise (naming an internal ``.tmp`` path) if the pre-check used
    ``parent.exists()`` instead of ``parent.is_dir()``. ``exists()``
    returns True for a regular file, so it would slip past the guard."""
    file_parent = tmp_path / "not_a_dir.txt"
    file_parent.write_text("i am a file, not a directory", encoding="utf-8")
    assert file_parent.is_file()
    out_path = file_parent / "backup.json"

    with pytest.raises(FileNotFoundError) as exc_info:
        _cli_export(output=str(out_path), include_tombstones=True, scopes=None)

    # The message names the bad parent the user typed, not an internal
    # tmp path — and it is FileNotFoundError, not FileExistsError.
    assert str(file_parent) in str(exc_info.value)
    assert not isinstance(exc_info.value, FileExistsError)

    # The regular file is untouched — no tmp sibling left behind, the
    # file's contents are intact.
    assert file_parent.read_text(encoding="utf-8") == "i am a file, not a directory"


def test_export_to_file_bare_filename_does_not_raise(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare filename has ``Path('.').parent == Path('.')``, which always
    exists — the pre-check must not raise in that case. Guard against a
    naïve ``if not parent.exists()`` regression that would reject every
    cwd-relative export path."""
    monkeypatch.chdir(tmp_path)
    _cli_export(output="bare.json", include_tombstones=False, scopes=None)
    assert (tmp_path / "bare.json").exists()
