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
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory.cli.export import _count_tombstone_files
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


def _plant_forward_version_copy(source: Path, destination: Path) -> None:
    """Copy `source` to `destination` with its `schema_version` bumped past
    anything this reader accepts.

    A forward schema_version is the realistic way a file gets skipped by the
    loader without being corrupt: `sync pull` from a machine running a newer
    bettermemory. It is also why the export's counts are named for the skip
    and not for a parse failure — this file is well-formed, and a fixture
    that could only produce garbage would let the export get away with the
    stronger claim. Copying a real store file and changing exactly one line
    keeps the forward version the ONLY difference, so a test built on it is
    measuring the version gate rather than incidental malformation. The
    substitution is asserted, not assumed — if the on-disk key is ever
    renamed, this fails loudly here instead of quietly planting a
    perfectly readable file and turning every assertion below into a
    tautology.
    """
    original = source.read_text(encoding="utf-8")
    bumped, substitutions = re.subn(
        r"^schema_version: \d+$", "schema_version: 99", original, count=1, flags=re.M
    )
    assert substitutions == 1, (
        f"no `schema_version: <int>` line in {source} to bump — the on-disk "
        f"frontmatter shape changed and this fixture no longer plants a "
        f"file the loader skips"
    )
    destination.write_text(bumped, encoding="utf-8")


@pytest.fixture
def store_with_skipped_files(populated_store: tuple[Path, Store]) -> tuple[Path, Store]:
    """`populated_store` plus one skipped active file AND one skipped
    tombstone, so both halves of the lifecycle are exercised at once.

    The tombstone half matters on its own: `doctor`'s memory_parse_health
    check compares `count_active_memory_files` against `load_all`, which
    catches the active half only. Nothing else in the tree notices a
    dropped tombstone, which is why export has to report it itself.
    """
    tmp_path, store = populated_store
    good_active = next(p for p in tmp_path.iterdir() if p.suffix == ".md")
    _plant_forward_version_copy(good_active, tmp_path / "forward-version-active.md")

    good_tombstone = next(p for p in store.tombstone_dir.iterdir() if p.suffix == ".md")
    _plant_forward_version_copy(
        good_tombstone, store.tombstone_dir / "forward-version-tombstone.md"
    )

    # Teeth for the fixture itself: prove the loader really drops these two
    # before any test asserts on the resulting counts. A fixture that
    # planted two READABLE files would make every count assertion below
    # pass for the wrong reason.
    fresh = Store(tmp_path)
    assert len(fresh.load_all()) == 2
    assert len([p for p in tmp_path.iterdir() if p.suffix == ".md"]) == 3
    assert len(fresh.load_tombstones()) == 1
    assert len([p for p in store.tombstone_dir.iterdir() if p.suffix == ".md"]) == 2
    return tmp_path, store


# ---------------------------------------------------------------------------
# Skipped files — the export must not report a short backup as clean
# ---------------------------------------------------------------------------


def test_export_reports_skipped_counts_for_both_lifecycle_halves(
    store_with_skipped_files: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One skipped active file and one skipped tombstone must each
    surface as a count in the payload. Before this landed, `load_all` and
    `load_tombstones` swallowed both and the document claimed 2 active + 0
    tombstones with no indication anything was missing."""
    _cli_export(output=None, include_tombstones=True, scopes=None)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["skipped_active_files"] == 1
    assert parsed["skipped_tombstone_files"] == 1

    # The surviving records are still exported — a partial capture beats
    # no capture, which is why this is a warning and not a hard failure.
    assert len(parsed["active_memories"]) == 2
    assert len(parsed["tombstoned_memories"]) == 1


def test_export_warns_on_stderr_naming_both_halves_and_doctor(
    store_with_skipped_files: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A count buried in JSON nobody opens is not a warning. The human
    running the backup has to see it on stderr, and be told where to go
    next."""
    _cli_export(output=None, include_tombstones=True, scopes=None)
    err = capsys.readouterr().err

    assert "WARNING" in err
    assert "1 active memory file(s)" in err
    assert "1 tombstone file(s)" in err
    assert "bettermemory doctor" in err
    # The claim the user most needs: this file is not a whole backup.
    assert "partial capture" in err
    # And the claim it must NOT make. The count is a two-walk delta, so it
    # cannot tell a malformed file from one this install skipped on purpose;
    # the fixture's planted files are in fact well-formed. Naming both causes
    # (doctor's wording for the same delta) keeps a user with a
    # newer-schema_version file from editing frontmatter that is already
    # correct.
    assert "were skipped by the loader" in err
    assert "schema_version newer than this install" in err
    assert "could not be read" not in err


def test_export_clean_store_still_emits_both_skip_counts_as_zero(
    populated_store: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The counts are unconditional. A key that appears only on failure is
    a key no consumer's tooling reads — a backup checker must be able to
    assert `== 0` rather than remember to probe for absence."""
    _cli_export(output=None, include_tombstones=True, scopes=None)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["skipped_active_files"] == 0
    assert parsed["skipped_tombstone_files"] == 0
    # And a clean store stays quiet — no warning to train users to ignore.
    assert "WARNING" not in captured.err


def test_export_no_tombstones_reports_null_not_zero_for_that_half(
    store_with_skipped_files: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-tombstones never reads the tombstone directory, so 0 there
    would assert an absence nobody checked. `null` says "not examined",
    matching how the `tombstoned_memories` key itself is omitted rather
    than emitted empty. The active count is still a real number."""
    _cli_export(output=None, include_tombstones=False, scopes=None)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert "tombstoned_memories" not in parsed
    assert "skipped_tombstone_files" in parsed
    assert parsed["skipped_tombstone_files"] is None
    assert parsed["skipped_active_files"] == 1
    # The unread tombstone half must not be described in the warning.
    assert "tombstone file(s)" not in captured.err
    assert "1 active memory file(s)" in captured.err


def test_export_scope_filter_does_not_inflate_the_skipped_count(
    store_with_skipped_files: tuple[Path, Store],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The drop is measured against the UNFILTERED read. A skipped file
    has no scopes to test, so subtracting after the scope filter would
    blame the filter for every out-of-scope memory and report a store-wide
    catastrophe on a routine `--scope` export."""
    _cli_export(output=None, include_tombstones=True, scopes=["infrastructure"])
    parsed = json.loads(capsys.readouterr().out)

    # One of the two readable actives matches; the other is filtered out —
    # and that exclusion must not be counted as a skip.
    assert len(parsed["active_memories"]) == 1
    assert parsed["skipped_active_files"] == 1
    assert parsed["tombstoned_memories"] == []
    assert parsed["skipped_tombstone_files"] == 1


def test_strict_help_states_the_two_cases_that_surprise_an_operator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--strict` has two behaviours a cron author only discovers the hard
    way, and the flag help is the only text they read — not this module's
    docstring.

    First: under --no-tombstones that half is never read, so --strict
    provably cannot fire on it, which is exactly why the payload records
    null rather than 0 there. Second: the loader makes no exception for a
    README.md a user drops in the store root (doctor records that decision
    and the bug that came of special-casing it), so such a file counts as a
    skip — and where doctor reports it as a warning, --strict turns it into
    a failed backup job. Both are asserted against the rendered `--help`
    rather than the source string, because that is what the operator sees.
    """
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)

    with pytest.raises(SystemExit):
        export_parser.parse_args(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())

    assert "--no-tombstones the tombstone half is never read" in help_text
    assert "README" in help_text
    assert "bettermemory doctor" in help_text


# ---------------------------------------------------------------------------
# The tombstone-side file count delegates rather than re-filters
# ---------------------------------------------------------------------------


def test_tombstone_file_count_is_the_store_iterator_counted(
    populated_store: tuple[Path, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_count_tombstone_files` must count what `Store._iter_tombstone_paths`
    yields, not restate that iterator's filter.

    A hand-rolled copy of the filter passes every behavioural test on the day
    it is written and disagrees with the store the day the store's rule
    changes — the same drift `count_active_memory_files` exists to prevent on
    the active half, where "counted here" and "skipped there" come from one
    definition. So the guard cannot be another end-to-end count: it feeds the
    iterator paths that no walk of a real directory could produce (they do not
    exist, and one has the wrong suffix), which only delegation can report as
    3. Whether a given path belongs in the walk is the store's decision, made
    once, in the iterator."""
    root, store = populated_store
    absent = root / "no-such-subdirectory"
    consumed = False

    def fake_iter(self: Store) -> Iterator[Path]:
        nonlocal consumed
        consumed = True
        yield absent / "a.md"
        yield absent / "b.md"
        yield absent / "c.rst"

    monkeypatch.setattr(Store, "_iter_tombstone_paths", fake_iter)

    assert _count_tombstone_files(store) == 3
    # Assert the iterator was actually reached, not merely that the number
    # matched: a counter that ignored the patch entirely could still return
    # some 3 from a differently-populated store.
    assert consumed, "_count_tombstone_files never called _iter_tombstone_paths"


# ---------------------------------------------------------------------------
# --strict
# ---------------------------------------------------------------------------


def test_export_default_exit_status_unchanged_when_files_were_dropped(
    store_with_skipped_files: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default stays exit 0. `export -o` is advertised as the scripted
    backup path; flipping its exit status unconditionally would turn a
    long-broken store into a suddenly-red cron job with no change on the
    user's part. The warning is the default's escalation, not the code."""
    out_path = tmp_path / "backup.json"
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "-o", str(out_path)])

    export_run(args, sub_parser=export_parser)  # must not raise SystemExit

    err = capsys.readouterr().err
    # Assert the drop condition was actually live in this run — otherwise
    # "did not exit" would pass on a store with nothing to report.
    assert "WARNING" in err
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["skipped_active_files"] == 1
    assert written["skipped_tombstone_files"] == 1


def test_export_strict_exits_nonzero_when_files_were_dropped(
    store_with_skipped_files: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--strict is the opt-in that makes a short backup fail the job. The
    document is still written first: the exit status is about alerting the
    operator, not about withholding the records that did survive."""
    out_path = tmp_path / "backup.json"
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "--strict", "-o", str(out_path)])

    with pytest.raises(SystemExit) as excinfo:
        export_run(args, sub_parser=export_parser)

    assert excinfo.value.code != 0
    assert "WARNING" in capsys.readouterr().err
    # The partial backup landed on disk despite the non-zero exit.
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(written["active_memories"]) == 2
    assert written["skipped_active_files"] == 1


def test_export_strict_on_a_clean_store_exits_zero(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--strict must only fire on a real drop. A flag that fails every run
    is a flag operators disable."""
    out_path = tmp_path / "backup.json"
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "--strict", "-o", str(out_path)])

    export_run(args, sub_parser=export_parser)  # must not raise SystemExit

    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["skipped_active_files"] == 0
    assert written["skipped_tombstone_files"] == 0
    assert "WARNING" not in capsys.readouterr().err


def test_export_strict_fires_on_a_dropped_tombstone_alone(
    populated_store: tuple[Path, Store],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The tombstone half has no other detector anywhere in the tree —
    `doctor`'s memory_parse_health only compares the active counts. A store
    whose actives all parse but whose tombstone dropped must still fail
    --strict, and the payload must not report `tombstoned_memories: []`
    (documented as "none present") beside a silent zero."""
    store_root, store = populated_store
    good_tombstone = next(p for p in store.tombstone_dir.iterdir() if p.suffix == ".md")
    _plant_forward_version_copy(
        good_tombstone, store.tombstone_dir / "forward-version-tombstone.md"
    )

    out_path = tmp_path / "backup.json"
    parser = argparse.ArgumentParser(prog="bettermemory")
    sub = parser.add_subparsers(dest="cmd")
    export_parser = export_add_subparser(sub)
    args = parser.parse_args(["export", "--strict", "-o", str(out_path)])

    with pytest.raises(SystemExit) as excinfo:
        export_run(args, sub_parser=export_parser)

    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "1 tombstone file(s)" in err
    # The active half is clean, so it must not appear in the warning.
    assert "active memory file(s)" not in err
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["skipped_active_files"] == 0
    assert written["skipped_tombstone_files"] == 1


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
