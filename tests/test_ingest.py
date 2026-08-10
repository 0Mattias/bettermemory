"""Tests for the `bettermemory ingest` module + CLI subcommand.

Covers the auto-memory parser (frontmatter `name`/`description`/`type`),
the type→category mapping, dedup against the active store and
tombstones, the skip reasons (invalid / empty / duplicate / tombstone),
plus an end-to-end CLI smoke through `main()`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import typing

from bettermemory.config import BehaviorConfig, Config, ScopesConfig
from bettermemory.ingest import (
    DEFAULT_PROVENANCE_SCOPE,
    _ACTIONS,
    _INDEX_FILENAMES,
    Action,
    apply_ingest_plan,
    compute_ingest_plan,
    discover_default_source_root,
    render_ingest_text,
)
from bettermemory.models import Category
from bettermemory.store import Store

from .conftest import set_git_discovery_ceiling


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_auto_memory(
    root: Path,
    name: str,
    *,
    description: str = "summary line",
    body: str = "body prose",
    auto_type: str | None = "feedback",
) -> Path:
    """Drop one auto-memory-format file into the source directory.

    Matches the layout Claude Code's auto-memory writes: YAML
    frontmatter with `name`, `description`, and `metadata.type`,
    followed by markdown body.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    parts = ["---", f"name: {name}", f"description: {description}"]
    if auto_type is not None:
        parts += ["metadata:", f"  type: {auto_type}"]
    parts += ["---", "", body, ""]
    path.write_text("\n".join(parts))
    return path


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    return tmp_path / "source"


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def store(store_dir: Path) -> Store:
    return Store(store_dir)


_GIT_AVAILABLE = shutil.which("git") is not None


def _init_git_repo(path: Path, *, remote: str | None = None) -> None:
    """Minimal git repo for origin-capture tests — mirrors the
    `_init_repo` helper in `tests/test_origin.py`."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if remote is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=path,
            check=True,
            capture_output=True,
        )


def _auto_memory_dir_for(cwd: Path, home: Path) -> Path:
    """The auto-memory path Claude Code would use for `cwd` under `home`.

    Mirrors the production sanitiser in `discover_default_source_root`:
    Claude Code's real scheme folds EVERY non-alphanumeric character to
    `-` (`path.replace(/[^a-zA-Z0-9]/g, "-")`) after stripping the
    leading `/`, so a fixture landed here is found by that primary
    probe."""
    resolved = cwd.resolve().as_posix().lstrip("/")
    sanitised = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
    return home / ".claude" / "projects" / sanitised / "memory"


# ---------------------------------------------------------------------------
# Closed-protocol pin for the index-filename whitelist consumed at
# `ingest.py:436` (`if path.name in _INDEX_FILENAMES`). The
# `_INDEX_FILENAMES` frozenset (`ingest.py:110`, `{"MEMORY.md",
# "INDEX.md", "README.md"}`) names every navigation-artefact filename
# the ingest walker must skip — auto-memory's `MEMORY.md` index file,
# the conventional `INDEX.md` / `README.md` siblings. The hazard tier
# is low (additions are non-blocking — a new auto-memory release
# shipping, say, `TOC.md` would just slip through and ingest as a
# memory until somebody noticed; deletions silently allow stored-as-
# memory copies of the named index files), but per Agent-4's bulk-pass
# recommendation we close the failure-mode class for completeness.
# The existing `test_index_files_are_skipped` in `TestComputeIngestPlan`
# below covers all three current members in one shot (skipping all
# three at once and asserting they don't surface in the plan rows) —
# catches a deletion (the asserted-skipped file would surface) but
# never imports `_INDEX_FILENAMES`, so an addition couldn't be caught.
#
# The hardcoded tuple is alphabetised and NOT derived from the source
# set — derivation would silently shrink the expected list when the
# source shrinks, defeating the deletion guard. Mirrors the
# `_EXPECTED_USE_OUTCOMES` shape (db81630) on the ingest-filter
# surface.
#
# Negative-control: adding `"bogus.md"` to `_INDEX_FILENAMES` fails
# `test_index_filenames_match_frozenset` (set inequality). Revert
# restores green.
_EXPECTED_INDEX_FILENAMES: tuple[str, ...] = (
    "INDEX.md",
    "MEMORY.md",
    "README.md",
)


def test_index_filenames_match_frozenset() -> None:
    """Guard so additions to ``_INDEX_FILENAMES`` (the closed-protocol
    nav-artefact whitelist consumed by the ingest walker) are mirrored
    in the hardcoded ``_EXPECTED_INDEX_FILENAMES`` tuple — otherwise a
    new index-filename convention could land in the source set and the
    existing ``test_index_files_are_skipped`` regression case wouldn't
    cover it. Closes the addition side of the membership-guard pattern
    on the ingest surface — mirrors ``test_use_outcomes_match_frozenset``
    in ``tests/test_server_record_use_provenance.py``."""
    assert set(_EXPECTED_INDEX_FILENAMES) == set(_INDEX_FILENAMES)


# Parity pin between the `Action` Literal (`ingest.py:118`) and the
# `_ACTIONS` tuple (`ingest.py:133`) consumed by `IngestPlan.summary`
# at `ingest.py:181` (`out: dict[str, int] = {a: 0 for a in _ACTIONS}`).
# The two MUST stay in lockstep: a new Literal value without a matching
# `_ACTIONS` entry means `IngestPlan.summary` pre-seeds zero rows for
# every known action except the new one, and any downstream renderer
# that does `summary["new_action"]` would KeyError on the missing
# bucket. The comment at `ingest.py:127-132` documents that this
# matching assertion exists in `tests/test_ingest.py` — this guard
# makes the comment true (prior to this commit, no such assertion
# existed in this file).
#
# Set-equality (not tuple-equality) is correct here: `_ACTIONS` is
# consumed as the keys of a dict comprehension, so order doesn't
# affect the summary's contents (Python 3.7+ dict iteration order is
# insertion-order but the summary is consumed as a value-keyed lookup
# downstream, not by ordered iteration). Contrast with
# `_SECRET_PATTERNS` in `tests/test_events.py` where the iteration
# order IS load-bearing (regex precedence).
#
# Negative-control: temporarily adding a bogus `"skip_bogus"` member
# to the `Action` Literal in `ingest.py` fails
# `test_actions_tuple_matches_action_literal` (set inequality:
# Literal has extra `"skip_bogus"` that `_ACTIONS` doesn't). Revert
# restores green.
def test_actions_tuple_matches_action_literal() -> None:
    """Guard so additions to (or deletions from) the ``Action`` Literal
    at ``ingest.py:118`` are mirrored in the ``_ACTIONS`` tuple at
    ``ingest.py:133`` — otherwise ``IngestPlan.summary`` would
    pre-seed zero rows for every known action except the new one,
    and any downstream renderer that does ``summary["new_action"]``
    would KeyError on the missing bucket. Set equality is correct
    here because ``_ACTIONS`` is consumed as the keys of a dict
    comprehension (order isn't load-bearing for pre-seeding); the
    matching ordered-tuple guards live in ``tests/test_events.py``
    (``_SECRET_PATTERNS``) and ``tests/test_server.py``
    (``_WRITE_GATES``) where iteration precedence IS load-bearing.

    This assertion is the one the comment at ``ingest.py:127-132``
    refers to — prior to this commit the comment was aspirational."""
    assert set(_ACTIONS) == set(typing.get_args(Action))


# ---------------------------------------------------------------------------
# compute_ingest_plan — frontmatter parsing + category mapping
# ---------------------------------------------------------------------------


class TestComputeIngestPlan:
    def test_feedback_type_maps_to_fact_category(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(source_root, "feedback-1", auto_type="feedback")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = [r for r in plan.rows]
        assert row.action == "write"
        assert row.category == Category.FACT
        assert row.auto_memory_type == "feedback"

    def test_user_type_maps_to_user_inference(
        self, source_root: Path, store: Store
    ) -> None:
        """User-inference is the always-pending tier on the MCP write
        path. Ingest writes them directly with that category — see the
        module docstring for the reasoning."""
        _write_auto_memory(source_root, "user-1", auto_type="user")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.category == Category.USER_INFERENCE

    def test_reference_type_maps_to_ambient(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(source_root, "ref-1", auto_type="reference")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.category == Category.AMBIENT

    def test_intra_batch_dedup_skips_second_identical_file(
        self, source_root: Path, store: Store
    ) -> None:
        """Two near-identical source files in ONE run: the first writes, the
        second is caught as a duplicate of the in-flight write. The interactive
        memory_write path reloads the store between writes; ingest classifies
        against one frozen snapshot, so without folding planned writes back in,
        both would write — silent duplicate memories."""
        body = "the deployment pipeline runs argo rollouts every tuesday afternoon"
        _write_auto_memory(
            source_root, "dup-1", body=body, description="deploy cadence"
        )
        _write_auto_memory(
            source_root, "dup-2", body=body, description="deploy cadence"
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        assert sorted(r.action for r in plan.rows) == ["skip_duplicate", "write"]

    def test_unknown_type_falls_back_to_fact(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(source_root, "x", auto_type="something-new")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.category == Category.FACT

    def test_provenance_scope_always_present(
        self, source_root: Path, store: Store
    ) -> None:
        """The `imported-from-claude-code` tag lets the user retrieve
        just the imported set with a single memory_search later."""
        _write_auto_memory(source_root, "x", auto_type="feedback")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert DEFAULT_PROVENANCE_SCOPE in row.scopes

    def test_extra_scopes_are_appended(self, source_root: Path, store: Store) -> None:
        _write_auto_memory(source_root, "x", auto_type="feedback")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["tools", "platform:linux"],
        )
        [row] = plan.rows
        assert "tools" in row.scopes
        assert "platform:linux" in row.scopes

    def test_extra_scopes_dedup_with_provenance(
        self, source_root: Path, store: Store
    ) -> None:
        """Passing the provenance scope via --scope should not duplicate
        it in the row's scope list."""
        _write_auto_memory(source_root, "x")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=[DEFAULT_PROVENANCE_SCOPE],
        )
        [row] = plan.rows
        assert row.scopes.count(DEFAULT_PROVENANCE_SCOPE) == 1

    def test_index_files_are_skipped(self, source_root: Path, store: Store) -> None:
        """MEMORY.md / INDEX.md / README.md are nav artefacts, not
        memories — they must not be ingested."""
        source_root.mkdir()
        (source_root / "MEMORY.md").write_text("# Index of memories\n- foo\n")
        (source_root / "INDEX.md").write_text("# Index of memories\n- foo\n")
        (source_root / "README.md").write_text("# Auto-memory readme\n")
        _write_auto_memory(source_root, "real-mem", auto_type="feedback")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        names = {r.source_path.name for r in plan.rows}
        assert names == {"real-mem.md"}

    def test_compute_ingest_plan_skips_symlinks(
        self, source_root: Path, store: Store
    ) -> None:
        """A `.md` file that's actually a symlink (e.g. pointing at
        `/etc/hosts`) must NOT be ingested as a memory — `Path.is_file`
        alone follows symlinks, so the walker has to detect and skip
        them up front. Surfaces as `skip_symlink` in the summary."""
        # Real, well-formed source file alongside a symlink to a
        # never-readable-as-memory host file. `/etc/hosts` is always
        # present on macOS and Linux; the test would skip on a system
        # where it's absent.
        target = Path("/etc/hosts")
        if not target.exists():
            pytest.skip("requires /etc/hosts (always present on macOS/Linux)")
        _write_auto_memory(source_root, "good", auto_type="feedback")
        bad = source_root / "bad.md"
        os.symlink(target, bad)

        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )

        rows_by_name = {r.source_path.name: r for r in plan.rows}
        assert "bad.md" in rows_by_name
        assert rows_by_name["bad.md"].action == "skip_symlink"
        # The symlink's row carries no body — we must never read the
        # target. Defense-in-depth: even if a future refactor swapped
        # `path.is_symlink()` for a target read, this assertion would
        # catch it before /etc/hosts content lands in an IngestRow.
        assert rows_by_name["bad.md"].body == ""
        assert "good.md" in rows_by_name
        assert rows_by_name["good.md"].action == "write"
        # Summary surfaces the count alongside the other skip reasons.
        assert plan.summary["skip_symlink"] == 1

    def test_empty_body_and_description_is_skipped(
        self, source_root: Path, store: Store
    ) -> None:
        source_root.mkdir()
        (source_root / "empty.md").write_text(
            "---\nname: empty\ndescription: \n---\n\n"
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.action == "skip_empty"

    def test_parse_error_is_skipped_invalid_not_raised(
        self, source_root: Path, store: Store
    ) -> None:
        """A malformed source file surfaces as skip_invalid rather than
        crashing the whole ingest run — important so one bad file
        doesn't lose the rest of the batch.

        The frontmatter is a YAML sequence (list, not mapping); the
        loader explicitly rejects non-dict frontmatter at
        `_frontmatter.py:113`. An earlier fixture used `: : not valid`
        which PyYAML happily parses as `{None: ": not valid"}` — the
        test passed for the wrong reason."""
        source_root.mkdir()
        (source_root / "broken.md").write_text(
            "---\n- list item one\n- list item two\n---\nbody"
        )
        _write_auto_memory(source_root, "ok-file")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        actions = {r.source_path.name: r.action for r in plan.rows}
        assert actions["broken.md"] == "skip_invalid"
        broken_row = next(r for r in plan.rows if r.source_path.name == "broken.md")
        assert "parse error" in broken_row.reason or "mapping" in broken_row.reason
        assert actions["ok-file.md"] == "write"

    def test_flat_type_key_is_honored(self, source_root: Path, store: Store) -> None:
        """Later Claude Code auto-memory revisions flattened the type
        key from `metadata.type` to a top-level `type:`. Both forms
        appear in real auto-memory directories, so the parser must
        consult the flat key when the nested form is absent. A bug
        here silently routed every flat-form file to `Category.FACT`."""
        source_root.mkdir()
        path = source_root / "flat.md"
        path.write_text(
            "---\nname: flat\ndescription: summary\ntype: user\n---\nbody\n"
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.auto_memory_type == "user"
        assert row.category == Category.USER_INFERENCE

    def test_nested_type_wins_when_both_present(
        self, source_root: Path, store: Store
    ) -> None:
        """A transitional file mid-migration could in principle carry
        both shapes. The parser's documented precedence is "nested
        wins"; this test pins it so a future refactor that flipped
        the lookup order would surface. The conflict is rare in
        production today but locking in the rule keeps it
        observable rather than discovered after a silent regression."""
        source_root.mkdir()
        path = source_root / "both.md"
        path.write_text(
            "---\n"
            "name: both\n"
            "description: summary\n"
            "type: reference\n"  # flat
            "metadata:\n"
            "  type: user\n"  # nested — should win
            "---\nbody\n"
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.auto_memory_type == "user"
        assert row.category == Category.USER_INFERENCE

    def test_non_string_type_falls_back_to_fact(
        self, source_root: Path, store: Store
    ) -> None:
        """A non-string `type:` value (YAML int, list, mapping) is a
        torn file. `_classify_one` clamps to None → `Category.FACT`
        rather than raising, so one weird source file never blocks
        the rest of the batch."""
        source_root.mkdir()
        path = source_root / "bad-type.md"
        path.write_text(
            "---\nname: bad-type\ndescription: summary\ntype: 42\n---\nbody\n"
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.auto_memory_type is None
        assert row.category == Category.FACT
        assert row.action == "write"

    def test_type_derived_scope_is_appended(
        self, source_root: Path, store: Store
    ) -> None:
        """Each ingested record carries a type-derived scope alongside
        the provenance tag (`feedback` → `feedback`, `project` →
        `project-context`, etc.). A downstream curation pass can branch
        on the scope without re-reading the body. Renaming any of the
        four scope keys silently degrades that downstream signal."""
        mapping = {
            "feedback": "feedback",
            "project": "project-context",
            "user": "user-inferences",
            "reference": "reference",
        }
        for auto_type in mapping:
            _write_auto_memory(source_root, f"{auto_type}-x", auto_type=auto_type)
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        by_type = {r.auto_memory_type: r for r in plan.rows}
        for auto_type, expected_scope in mapping.items():
            assert expected_scope in by_type[auto_type].scopes, (
                f"{auto_type} → expected scope {expected_scope!r}, "
                f"got {by_type[auto_type].scopes!r}"
            )

    def test_missing_source_root_raises_file_not_found(
        self, tmp_path: Path, store: Store
    ) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            compute_ingest_plan(
                tmp_path / "no-such-dir",
                existing_memories=store.load_all(),
                existing_tombstones=store.load_tombstones(),
            )

    def test_source_root_not_a_directory_raises(
        self, tmp_path: Path, store: Store
    ) -> None:
        (tmp_path / "file.md").write_text("body")
        with pytest.raises(NotADirectoryError):
            compute_ingest_plan(
                tmp_path / "file.md",
                existing_memories=store.load_all(),
                existing_tombstones=store.load_tombstones(),
            )

    def test_body_composes_description_plus_content(
        self, source_root: Path, store: Store
    ) -> None:
        """bettermemory's first-summary-line convention expects the
        summary on line 1 of the body. The composed body puts
        `description` first, then a blank line, then `post.content`."""
        _write_auto_memory(
            source_root,
            "x",
            description="Short summary line",
            body="Longer body prose\nwith multiple lines.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.body.startswith("Short summary line")
        assert "Longer body prose" in row.body


# ---------------------------------------------------------------------------
# Dedup gate
# ---------------------------------------------------------------------------


class TestDedup:
    def test_force_bypasses_active_dedup_but_not_tombstones(
        self, source_root: Path, store: Store
    ) -> None:
        """`--force` parity with `memory_write`'s `force=True`: skips
        the active-store dedup but keeps the tombstone dedup so a
        user-removed memory can't be resurrected by re-ingest. Locks
        in the asymmetry the audit specifically called out — the two
        gates have to be controllable independently.

        Runs THROUGH `apply_ingest_plan`, not just to the plan. The
        plan-only version of this test passed for the entire lifetime of
        a regression in which the apply loop's own `DedupActiveGate`
        refused every forced row right back — `--force` produced a green
        plan and wrote nothing."""
        # Seed an active memory that would otherwise dedup.
        _write_auto_memory(
            source_root,
            "dup",
            description="ripgrep over grep",
            body="The team uses ripgrep instead of grep.",
        )
        plan_first = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan_first, store)
        assert len(store.load_all()) == 1

        # Without --force the same source file is suppressed as duplicate.
        plan_skip = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        assert plan_skip.rows[0].action == "skip_duplicate"

        # With --force the dedup gate is bypassed; a new write lands.
        plan_force = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            force=True,
        )
        assert plan_force.rows[0].action == "write"
        apply_ingest_plan(plan_force, store, force=True)
        [forced_row] = plan_force.rows
        assert forced_row.action == "write", forced_row.reason
        assert forced_row.written_id is not None
        # The decisive assertion the plan-only version never made: the
        # duplicate row `--force` documents is DURABLE.
        active = {m.id for m in store.load_all()}
        assert forced_row.written_id in active
        assert len(active) == 2

    def test_force_does_not_resurrect_tombstoned_memory(
        self, source_root: Path, store: Store
    ) -> None:
        """Tombstone dedup stays in force even under --force. Removing
        a memory is a deliberate act; re-ingesting it should still be
        blocked at the tombstone gate."""
        _write_auto_memory(
            source_root,
            "tomb-1",
            description="remove me",
            body="this body is going to be tombstoned",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.written_id is not None
        store.tombstone(row.written_id, "test-removed", session_id="sess-test")

        # Re-ingest with --force — the active-store dedup is bypassed
        # but the tombstone dedup is still checked, so this stays out.
        plan_replay = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            force=True,
        )
        assert plan_replay.rows[0].action == "skip_tombstone"
        # And the apply pass agrees rather than quietly overturning it.
        apply_ingest_plan(plan_replay, store, force=True)
        assert plan_replay.rows[0].written_id is None
        assert store.load_all() == []

    def test_force_refuses_a_tombstone_twin_at_apply_time_too(
        self, source_root: Path, store: Store
    ) -> None:
        """The apply-side half of the asymmetry, isolated.

        `--force` reaches the gate chain by DROPPING `DedupActiveGate`
        from the tuple, never by setting `GateContext.force=True` — one
        field both dedup gates read, so threading it would take
        `DedupTombstoneGate` down with it and let `--force` resurrect a
        deliberately-removed memory. The compute side does not cover
        this: it can only refuse rows it saw the tombstone for, and it
        scores under whatever threshold the caller resolved, so a
        tombstone that lands between plan and commit (or a twin only the
        semantic scorer recognises) arrives here with `action="write"`.
        """
        _write_auto_memory(
            source_root,
            "twin",
            description="remove me",
            body="this body is going to be tombstoned",
        )
        seeded = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(seeded, store)
        [seed_row] = seeded.rows
        assert seed_row.written_id is not None

        # Plan while the memory is still ACTIVE, so the row is "write"...
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            force=True,
        )
        assert plan.rows[0].action == "write"
        # ...then remove it, and commit the already-computed forced plan.
        store.tombstone(seed_row.written_id, "test-removed", session_id="sess-test")
        apply_ingest_plan(plan, store, force=True)

        [row] = plan.rows
        assert row.action == "skip_invalid"
        assert "previously_removed" in (row.reason or "")
        assert row.written_id is None
        assert store.load_all() == []

    def test_duplicate_against_active_store_is_skipped(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(
            source_root,
            "dup",
            description="ripgrep is the preferred grep tool",
            body="The team uses ripgrep instead of grep.",
        )
        # Pre-load the same content into the store.
        plan_first = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan_first, store)

        # Now re-run — the second pass should see the just-committed
        # memory in the active store and skip the source file.
        plan_second = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan_second.rows
        assert row.action == "skip_duplicate"
        assert "matches active memory" in row.reason

    def test_duplicate_against_tombstone_is_skipped(
        self, source_root: Path, store: Store
    ) -> None:
        """If the user already removed a near-duplicate of the source
        file's content, ingest must NOT silently resurrect it — that's
        the negative-results-suppression contract."""
        _write_auto_memory(
            source_root,
            "removed-twin",
            description="ripgrep is the preferred grep tool",
            body="Use ripgrep instead of grep.",
        )
        # Write a near-duplicate into the store, then tombstone it.
        existing = store.write(
            content=(
                "ripgrep is the preferred grep tool\n\nUse ripgrep instead of grep.\n"
            ),
            scopes=["tools"],
        )
        store.tombstone(existing.id, reason="duplicate-of-something-else")

        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.action == "skip_tombstone"
        assert "matches tombstoned memory" in row.reason


# ---------------------------------------------------------------------------
# apply_ingest_plan — actual writes land in the store
# ---------------------------------------------------------------------------


class TestApplyIngestPlan:
    def test_write_action_lands_in_store(self, source_root: Path, store: Store) -> None:
        _write_auto_memory(source_root, "fresh", description="hello", body="world")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.written_id is not None
        # The store now holds one new memory with the same body.
        new = [m for m in store.load_all() if m.id == row.written_id]
        assert new
        assert "hello" in new[0].body and "world" in new[0].body

    def test_skip_actions_do_not_write(self, source_root: Path, store: Store) -> None:
        source_root.mkdir()
        (source_root / "empty.md").write_text("---\nname: empty\n---\n\n")
        before_count = len(store.load_all())
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        after_count = len(store.load_all())
        assert after_count == before_count

    def test_user_inference_lands_in_active_store_not_pending(
        self, source_root: Path, store: Store
    ) -> None:
        """The MCP write handler routes `category=user-inference`
        through a pending-confirm gate (the model staging a user claim
        needs human ack before commit). Ingest deliberately bypasses
        that gate because the source file is itself the user's act of
        commit. The bypass is structural — `apply_ingest_plan` calls
        `store.write` directly rather than going through the handler
        — but this test locks in the contract: a USER_INFERENCE
        ingested row materialises as an active memory, not a pending
        write awaiting confirmation."""
        _write_auto_memory(
            source_root,
            "user-claim",
            auto_type="user",
            description="user prefers terse responses",
            body="explicitly stated 2026-05-24",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.category == Category.USER_INFERENCE
        assert row.written_id is not None
        # The decisive assertion: the record is in the active store,
        # not parked in a pending tier.
        active = {m.id: m for m in store.load_all()}
        assert row.written_id in active
        assert active[row.written_id].category == Category.USER_INFERENCE


class TestApplyIngestPlanContentGates:
    """`apply_ingest_plan` runs the shared `CONTENT_GATES` chain.

    Before the `apply_write_gates` extraction this path called
    `store.write` directly and ran NO write policy at all: a pasted
    credential, a transient marker, or a duplicate of a tombstoned memory
    imported silently. These tests pin that the shared chain now fires
    here, and — just as importantly — that the one gate ingest bypasses on
    purpose still stays bypassed (see the class below).
    """

    def test_credential_in_source_file_is_skipped_not_written(
        self, source_root: Path, store: Store
    ) -> None:
        """The gate ingest most needed. An auto-memory file is authored by
        the user, but authorship is not a claim about content — a secret
        pasted mid-session rides into the file and the store is
        plain-markdown that `sync` pushes across hosts."""
        _write_auto_memory(
            source_root,
            "leaky",
            description="deploy notes",
            body="The deploy token is sk-ant-api03-AA00bbCCddEEffGGhhIIjjKKllMMnnOOpp",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.written_id is None
        assert row.action == "skip_invalid"
        assert "credential" in (row.reason or "")
        # The decisive assertion: nothing reached the durable store.
        assert store.load_all() == []

    def test_transient_marker_in_source_file_is_skipped(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(
            source_root,
            "fleeting",
            description="current state",
            body="We just switched the queue over to Redis this afternoon.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.action == "skip_invalid"
        assert "transient" in (row.reason or "")
        assert store.load_all() == []

    def test_clean_row_still_writes(self, source_root: Path, store: Store) -> None:
        """The gates must not become a blanket refusal — the ordinary
        import path stays open. Without this, every assertion above would
        also pass if `apply_write_gates` rejected unconditionally."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.action == "write"
        assert row.written_id is not None
        assert len(store.load_all()) == 1

    def test_gate_skip_reason_names_the_status(
        self, source_root: Path, store: Store
    ) -> None:
        """The row reason carries the gate's own `status` vocabulary, so an
        operator reading `render_ingest_text` sees the same word
        `memory_write` would have returned rather than a paraphrase."""
        _write_auto_memory(
            source_root,
            "leaky2",
            body="token: ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIII",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert (row.reason or "").startswith("write gate refused: ")


class TestApplyIngestPlanOnNonEmptyStore:
    """The gates as they behave once the store has content in it.

    Every other gate test in this file runs against an EMPTY store, which
    is where `ScopeMismatchGate` is structurally inert: its project-name
    pass needs an existing `projects:*` scope to compare against, so with
    nothing seeded it can never fire. That blind spot hid a gate that
    hard-refused realistic imports on every store a real user would have.
    """

    def test_body_citing_a_seeded_project_name_still_lands(
        self, source_root: Path, store: Store
    ) -> None:
        """The reproduced refusal, as a regression test.

        Ingested rows carry no `projects:*` scope unless the operator
        passed one on `--scope` — by default just
        `imported-from-claude-code` plus the type-derived tag — while the
        source files come from a per-cwd auto-memory directory, so a body
        naming its own project is the norm. On any store holding one
        `projects:*` memory, that combination tripped the gate and the
        whole import came back `skip_invalid`. This test exercises the
        default: `compute_ingest_plan` below is called with no
        `extra_scopes`, so the two stamps are the whole scope list.
        """
        store.write(
            content="The webapp deploy pipeline is documented in the runbook.",
            scopes=["projects:webapp"],
        )
        _write_auto_memory(
            source_root,
            "deploy",
            description="deploy notes",
            body="The webapp deploy runs through GitHub Actions.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        assert plan.rows[0].action == "write"
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.action == "write", row.reason
        assert row.written_id is not None
        assert len(store.load_all()) == 2

    def test_content_gates_still_fire_on_a_non_empty_store(
        self, source_root: Path, store: Store
    ) -> None:
        """The scope-mismatch acknowledgement is not a blanket amnesty.

        Without this, the fix above would also pass if `_gate_context`
        had simply stopped running the chain — the credential refusal is
        the one ingest most needed and it has to survive on exactly the
        stores where the scope gate was disabled.
        """
        store.write(
            content="The webapp deploy pipeline is documented in the runbook.",
            scopes=["projects:webapp"],
        )
        _write_auto_memory(
            source_root,
            "leaky",
            description="webapp deploy notes",
            body=(
                "The webapp deploy token is "
                "sk-ant-api03-AA00bbCCddEEffGGhhIIjjKKllMMnnOOpp"
            ),
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        [row] = plan.rows
        assert row.action == "skip_invalid"
        assert "credential" in (row.reason or "")
        assert len(store.load_all()) == 1

    def test_scope_mismatch_ack_flag_has_exactly_one_reader(self) -> None:
        """`_gate_context` acknowledges `acknowledge_scope_mismatch` for the
        whole batch path. That is only safe while exactly one gate reads
        the field: a gate added later that reuses it would inherit an
        acknowledgement reasoned about for `ScopeMismatchGate` alone,
        silently and with no ingest-side test failing.
        """
        import inspect

        from bettermemory.handlers.write import _WRITE_GATES, ScopeMismatchGate

        readers = [
            type(gate).__name__
            for gate in _WRITE_GATES
            if "acknowledge_scope_mismatch" in inspect.getsource(type(gate))
        ]
        assert readers == [ScopeMismatchGate.__name__], (
            "ingest acknowledges `acknowledge_scope_mismatch` for every row; "
            "a second gate reading that field now inherits the "
            "acknowledgement. Give the new gate its own flag, or extend "
            "`ingest._gate_context`'s rationale to cover it deliberately."
        )


class TestApplyIngestPlanScopeAllowlist:
    """`[scopes] allowed` is enforced on the ingest path — against the
    scopes the CALLER supplied, and identically in the plan and the apply.

    Three regressions are pinned here, in the order they happened.

    1. The knob was a no-op. `apply_ingest_plan` builds its `Store.write`
       payload by hand, so it never reaches `_validate_write_payload` — the
       one place every other write path enforces the whitelist — and no gate
       in `CONTENT_GATES` reads `config.scopes.allowed` either, which is why
       `consolidate._apply_llm_proposal` hand-rolls the same check. Under
       `allowed=["tools"]` a probe wrote a memory scoped `rogue` anyway.
    2. Closing (1) broke ingest outright for every store with a non-empty
       allowlist. Every row is stamped `imported-from-claude-code` plus a
       type-derived tag that the user never typed and cannot opt out of, so
       an allowlist that did not happen to name all five strings refused
       every row: `--scope projects:demo` under `allowed=["projects:demo"]`
       imported nothing. Those stamps are exempt now
       (`_tool_stamped_scopes`); the list is a policy about what the user
       may scope a memory to.
    3. The check ran at apply time only, so `--dry-run` printed "would
       write N" for rows the commit then refused. `compute_ingest_plan`
       takes the same `Config` and runs the same predicate in the same
       position.

    The refusal is per ROW, not per run: consolidate raises because it
    judges one cluster at a time, but an ingest plan is a batch and one
    unsanctioned row must not cost the operator the conforming ones.
    """

    @staticmethod
    def _apply(
        source_root: Path,
        store: Store,
        *,
        allowed: list[str] | None,
        extra_scopes: list[str] | None = None,
        expect_plan_actions: set[str] | None = None,
    ) -> dict[str, Any]:
        """Plan + apply, keyed by source filename.

        `allowed=None` passes `config=None` — the library caller that
        holds no `Config`, which `_gate_deps` resolves to `Config()`.

        `expect_plan_actions` asserts what the PLAN said before the apply
        ran. Without it an assertion below could pass on a row the plan had
        already skipped for an unrelated reason; with it, each test also
        states whether the plan and the apply agreed.
        """
        config = (
            None if allowed is None else Config(scopes=ScopesConfig(allowed=allowed))
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=extra_scopes or [],
            config=config,
        )
        assert {r.action for r in plan.rows} == (expect_plan_actions or {"write"})
        apply_ingest_plan(plan, store, config=config)
        return {r.source_path.name: r for r in plan.rows}

    def test_empty_allowlist_enforces_nothing(
        self, source_root: Path, store: Store
    ) -> None:
        """The default, and the reason the check is guarded on a non-empty
        list: an unset `[scopes] allowed` means "any scope" in
        `_validate_write_payload`, so enforcing it here would turn an
        untouched knob into a total refusal of every import."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        rows = self._apply(source_root, store, allowed=[], extra_scopes=["rogue"])
        assert rows["ordinary.md"].action == "write"
        assert rows["ordinary.md"].written_id is not None
        [stored] = store.load_all()
        assert stored.scopes == [DEFAULT_PROVENANCE_SCOPE, "feedback", "rogue"]

    def test_absent_config_enforces_nothing(
        self, source_root: Path, store: Store
    ) -> None:
        """`config=None` is the library caller `_gate_deps` documents, and
        it must not start refusing rows: the `Config()` fallback carries an
        empty allowlist, which is the case above."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        rows = self._apply(source_root, store, allowed=None, extra_scopes=["rogue"])
        assert rows["ordinary.md"].written_id is not None
        assert len(store.load_all()) == 1

    def test_offending_row_skipped_and_conforming_row_still_lands(
        self, source_root: Path, store: Store
    ) -> None:
        """The decisive one for per-ROW containment: one unsanctioned row
        must not cost the operator the conforming ones. With the
        whole-batch abort consolidate uses, both rows would be lost;
        without the check at all, both would land.

        `extra_scopes` is per RUN, and no source-file field carries scopes
        today, so the only way two rows in one batch differ in their
        caller-supplied scopes is a caller that edits the plan — which is
        also the shape a future source-carried scope would take. Editing
        `row.scopes` after planning is therefore not a contrivance around
        the plan-side check; it is the case that makes `apply_ingest_plan`
        the enforcement boundary rather than a re-check, which is exactly
        why the check stayed there when the plan side grew one too. The
        pre-fix version of this test told the rows apart by their
        type-derived scope, which stopped meaning anything once ingest's
        own stamps became exempt."""
        _write_auto_memory(
            source_root,
            "keeper",
            auto_type="feedback",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        _write_auto_memory(
            source_root,
            "outsider",
            auto_type="project",
            description="the release workflow",
            body="The release tag push triggers the PyPI publish workflow.",
        )
        config = Config(scopes=ScopesConfig(allowed=["sanctioned"]))
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["sanctioned"],
            config=config,
        )
        assert {r.action for r in plan.rows} == {"write"}
        rows = {r.source_path.name: r for r in plan.rows}
        rows["outsider.md"].scopes.append("rogue")
        apply_ingest_plan(plan, store, config=config)
        assert rows["keeper.md"].action == "write"
        assert rows["keeper.md"].written_id is not None
        assert rows["outsider.md"].action == "skip_invalid"
        assert rows["outsider.md"].written_id is None
        # The decisive assertion: exactly the conforming row reached disk,
        # carrying the stamps the allowlist never named.
        assert [m.scopes for m in store.load_all()] == [
            [DEFAULT_PROVENANCE_SCOPE, "feedback", "sanctioned"]
        ]

    def test_extra_scope_outside_the_allowlist_is_refused(
        self, source_root: Path, store: Store
    ) -> None:
        """The originally-reported repro verbatim: `ingest --scope rogue`
        against `allowed=["tools"]` planted a scope `memory_write` and
        `memory_update` both refuse. Still refused — the stamp exemption
        narrows the check to caller-supplied scopes, and this is one."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        rows = self._apply(
            source_root,
            store,
            allowed=["tools"],
            extra_scopes=["rogue"],
            expect_plan_actions={"skip_invalid"},
        )
        assert rows["ordinary.md"].action == "skip_invalid"
        assert store.load_all() == []

    def test_scopes_ingest_stamps_itself_are_exempt(
        self, source_root: Path, store: Store
    ) -> None:
        """The regression that broke ingest for every allowlist user.

        `allowed=["projects:demo"]` with `--scope projects:demo` names
        every scope the operator asked for and none of the two ingest
        stamps. Enforcing the list against the stamps refused all four
        rows — one per auto-memory type, so all four type-derived tags are
        covered — and the import silently landed nothing. Fails without
        the `_tool_stamped_scopes` exemption: `_scope_allowlist_reason`
        reports `['imported-from-claude-code', <type tag>]` as unknown."""
        for name, auto_type in (
            ("feedback-row", "feedback"),
            ("user-row", "user"),
            ("project-row", "project"),
            ("reference-row", "reference"),
        ):
            _write_auto_memory(
                source_root,
                name,
                auto_type=auto_type,
                description=f"summary for {name}",
                body=f"Distinct prose about {name} so dedup keeps them apart.",
            )
        rows = self._apply(
            source_root,
            store,
            allowed=["projects:demo"],
            extra_scopes=["projects:demo"],
        )
        assert {r.action for r in rows.values()} == {"write"}
        assert len(store.load_all()) == 4
        # And the stamps are on disk — exempt from the check, not stripped
        # to satisfy it.
        for stored in store.load_all():
            assert stored.scopes[0] == DEFAULT_PROVENANCE_SCOPE
            assert stored.scopes[-1] == "projects:demo"
            assert len(stored.scopes) == 3

    def test_stamped_exemption_is_per_row_not_a_flat_constant_set(
        self, source_root: Path, store: Store
    ) -> None:
        """`--scope feedback` on a `project`-typed row is a caller-supplied
        scope and stays checked, even though `feedback` is a string ingest
        stamps on OTHER rows. Exempting the union of every type tag would
        quietly widen the allowlist by four strings for everybody."""
        _write_auto_memory(
            source_root,
            "project-row",
            auto_type="project",
            description="the release workflow",
            body="The release tag push triggers the PyPI publish workflow.",
        )
        rows = self._apply(
            source_root,
            store,
            allowed=["tools"],
            extra_scopes=["feedback"],
            expect_plan_actions={"skip_invalid"},
        )
        assert "'feedback'" in (rows["project-row.md"].reason or "")
        assert store.load_all() == []

    def test_reason_names_the_offenders_in_memory_writes_own_words(
        self, source_root: Path, store: Store
    ) -> None:
        """The operator reads `render_ingest_text`, not the source. The
        reason opens on `_validate_write_payload`'s sentence so the words
        match what `memory_write` would have returned (the same rule
        `_gate_skip_reason` follows for gate statuses), then names the
        scopes that were EXEMPT — otherwise the obvious reading of a
        refusal is "add `imported-from-claude-code` to the allowlist",
        which is a fix for a bug that is no longer there."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        config = Config(scopes=ScopesConfig(allowed=["tools"]))
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["rogue"],
            config=config,
        )
        apply_ingest_plan(plan, store, config=config)
        [row] = plan.rows
        reason = row.reason or ""
        assert "scope(s) not in allowed list" in reason
        # Only the caller's unsanctioned scope is named as an offender —
        # the stamps must NOT appear in the offender list.
        assert "not in allowed list: ['rogue']" in reason
        # The allowlist itself is echoed so the fix is visible from the
        # line, and the exempt stamps are named so nobody goes adding them.
        assert "'tools'" in reason
        assert DEFAULT_PROVENANCE_SCOPE in reason
        assert "exempt" in reason
        # And it survives to the surface the operator actually reads.
        assert reason in render_ingest_text(plan, dry_run=False)

    def test_allowlist_read_from_the_same_config_the_gates_use(self) -> None:
        """`apply_ingest_plan` reads the list off `_gate_deps(...).config`
        rather than re-deriving the `config or Config()` fallback. A second
        copy of that resolution is how the plan and apply sides drifted
        apart over the dedup policy, and here the drift would be silent in
        the unsafe direction — a divergent fallback decides the knob is
        empty and enforces nothing."""
        import inspect

        from bettermemory import ingest as ingest_mod

        src = inspect.getsource(ingest_mod.apply_ingest_plan)
        assert "gate_deps.config.scopes.allowed" in src, (
            "the allowlist must come from the same object the gates run "
            "against; re-resolving `config if config is not None else "
            "Config()` here reopens the drift this pins shut"
        )


class TestIngestWriteCaps:
    """The three `[behavior]` write caps bind the ingest path — at the plan
    AND the apply, against what the CALLER supplied.

    Unlike the allowlist regressions above, there was never a plan/apply
    divergence to reconcile: `min_content_tokens`, `max_content_bytes` and
    `max_scopes_per_write` were unenforced at BOTH phases, so the plan and
    the commit agreed exactly — on landing rows `memory_write` refuses.
    Measured 2026-08-02 with all three set tight (200 bytes / 50 tokens /
    1 scope): a 3,098-byte body and a 3-token body, two caller scopes on
    each, every row planned as `write` and every row committed. That
    measured shape is pinned verbatim below.

    Two decisions carried over from the allowlist work, deliberately:
    the scope-count cap counts caller-supplied scopes only (ingest's own
    stamps would otherwise consume the whole budget of any tight cap —
    the same broke-every-allowlist-user shape, with a count instead of a
    list), and the refusal is per ROW via `skip_invalid`, never a batch
    abort. One decision is new: `config=None` means the SHIPPED cap
    defaults, not "caps off" — the allowlist's unset value is a no-op,
    the byte and scope caps' unset values are not, so treating absence
    as absence would enforce different caps on the plan and the commit.
    """

    _TIGHT = BehaviorConfig(
        max_content_bytes=200, min_content_tokens=50, max_scopes_per_write=1
    )

    @staticmethod
    def _run(
        source_root: Path,
        store: Store,
        *,
        behavior: BehaviorConfig | None,
        extra_scopes: list[str] | None = None,
        expect_plan_actions: set[str] | None = None,
    ) -> dict[str, Any]:
        """Plan + apply under `behavior`, keyed by source filename.

        `behavior=None` passes `config=None` to BOTH phases — the
        library caller whose caps must come out as the shipped defaults
        on each side. Mirrors the allowlist class's `_apply`, including
        the plan-action assertion that makes every test state whether
        the plan and the apply agreed.
        """
        config = None if behavior is None else Config(behavior=behavior)
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=extra_scopes or [],
            config=config,
        )
        assert {r.action for r in plan.rows} == (expect_plan_actions or {"write"})
        apply_ingest_plan(plan, store, config=config)
        return {r.source_path.name: r for r in plan.rows}

    def test_the_measured_repro_now_refuses_every_row(
        self, source_root: Path, store: Store
    ) -> None:
        """The 2026-08-02 measurement, re-run against the fix: the
        3,098-byte body trips the byte cap, the 3-token body trips the
        floor, both are `skip_invalid` in the PLAN (no `--dry-run`
        over-promise), nothing reaches disk, and each reason is
        `memory_write`'s own sentence — by construction, since the shared
        validators produce it. The reasons also survive to
        `render_ingest_text`, the surface the operator reads."""
        # description "d" + "\n\n" + 3,095-byte body composes to exactly
        # the measured 3,098 bytes, with ~770 tokens so the floor passes
        # and the SIZE arm is the one that fires.
        _write_auto_memory(
            source_root,
            "oversize",
            description="d",
            body=("tok " * 773 + "end")[:3095],
        )
        _write_auto_memory(
            source_root,
            "fragment",
            description="",
            body="kubernetes ingress tls",
        )
        config = Config(behavior=self._TIGHT)
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["projects:demo", "second"],
            config=config,
        )
        assert {r.action for r in plan.rows} == {"skip_invalid"}
        apply_ingest_plan(plan, store, config=config)
        rows = {r.source_path.name: r for r in plan.rows}
        assert "content exceeds max_content_bytes (3098 bytes > 200 bytes)" in (
            rows["oversize.md"].reason or ""
        )
        assert "content is below min_content_tokens (3 tokens < 50 tokens)" in (
            rows["fragment.md"].reason or ""
        )
        assert store.load_all() == []
        rendered = render_ingest_text(plan, dry_run=False)
        assert (rows["oversize.md"].reason or "") in rendered
        assert (rows["fragment.md"].reason or "") in rendered

    def test_two_caller_scopes_refused_under_cap_one(
        self, source_root: Path, store: Store
    ) -> None:
        """The count arm of the measured repro, isolated: the other two
        rows above never reach it because the floor and size fire first,
        in `_validate_write_payload`'s order. A compliant body with two
        `--scope` extras under `max_scopes_per_write = 1` is refused, and
        the reason names the exemption so "2 entries > 1" is legible
        against a row visibly carrying four scopes."""
        _write_auto_memory(
            source_root,
            "crowded",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        rows = self._run(
            source_root,
            store,
            behavior=BehaviorConfig(max_scopes_per_write=1),
            extra_scopes=["projects:demo", "second"],
            expect_plan_actions={"skip_invalid"},
        )
        reason = rows["crowded.md"].reason or ""
        assert "scopes exceeds max_scopes_per_write (2 entries > 1 entries)" in reason
        assert "exempt" in reason
        assert DEFAULT_PROVENANCE_SCOPE in reason
        assert store.load_all() == []

    def test_stamps_dont_count_against_the_cap(
        self, source_root: Path, store: Store
    ) -> None:
        """`max_scopes_per_write = 1` with ONE caller scope lands, even
        though the row's full list is three entries — provenance stamp,
        type tag, caller scope. Counting the stamps would refuse every
        import on any store with a tight cap, including one with no
        `--scope` at all: the broke-every-allowlist-user regression,
        re-armed as arithmetic."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        rows = self._run(
            source_root,
            store,
            behavior=BehaviorConfig(max_scopes_per_write=1),
            extra_scopes=["projects:demo"],
        )
        assert rows["ordinary.md"].written_id is not None
        [stored] = store.load_all()
        assert len(stored.scopes) == 3
        assert stored.scopes[0] == DEFAULT_PROVENANCE_SCOPE

    def test_caps_run_ahead_of_the_allowlist(
        self, source_root: Path, store: Store
    ) -> None:
        """A row that breaks a cap AND the allowlist reports the cap —
        the order `_validate_write_payload` checks them, so the plan's
        reason is the sentence `memory_write` would have raised first.
        Ordering is the parity being claimed, so it gets its own pin."""
        _write_auto_memory(
            source_root,
            "fragment",
            description="",
            body="kubernetes ingress tls",
        )
        config = Config(
            behavior=BehaviorConfig(min_content_tokens=50),
            scopes=ScopesConfig(allowed=["tools"]),
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["rogue"],
            config=config,
        )
        [row] = plan.rows
        assert row.action == "skip_invalid"
        assert "min_content_tokens" in (row.reason or "")
        assert "allowed list" not in (row.reason or "")

    def test_apply_is_the_enforcement_boundary_for_an_edited_row(
        self, source_root: Path, store: Store
    ) -> None:
        """Same shape as the allowlist's edited-row test: a caller that
        mutates `row.body` after planning meets the caps again at apply
        time, because only that side writes. The conforming sibling
        still lands — per-row containment, not batch abort."""
        # Each body needs >= 50 tokens for the configured floor AND a
        # vocabulary disjoint from its sibling's, or the batch dedup
        # (`planned` fold-in) marks the second row `skip_duplicate`
        # before the caps are ever the question.
        _write_auto_memory(
            source_root,
            "keeper",
            auto_type="feedback",
            description="summary for keeper",
            body=" ".join(f"alpha{i} bravo{i}" for i in range(30)),
        )
        _write_auto_memory(
            source_root,
            "victim",
            auto_type="project",
            description="summary for victim",
            body=" ".join(f"gamma{i} delta{i}" for i in range(30)),
        )
        config = Config(behavior=BehaviorConfig(min_content_tokens=50))
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            config=config,
        )
        assert {r.action for r in plan.rows} == {"write"}
        rows = {r.source_path.name: r for r in plan.rows}
        rows["victim.md"].body = "now a fragment"
        apply_ingest_plan(plan, store, config=config)
        assert rows["keeper.md"].written_id is not None
        assert rows["victim.md"].action == "skip_invalid"
        assert "min_content_tokens" in (rows["victim.md"].reason or "")
        assert len(store.load_all()) == 1

    def test_absent_config_enforces_the_shipped_defaults(
        self, source_root: Path, store: Store
    ) -> None:
        """`config=None` on both phases judges rows under `Config()`'s
        caps, which are live (byte cap 1 MB), not absent. A source FILE
        can't demonstrate that at plan time — `_frontmatter`'s read
        ceiling rejects it first — so the probe is the edited-row shape:
        plan a normal row, inflate `row.body` past the default cap, and
        the apply refuses under a config nobody passed. Treating `None`
        as "caps off" makes this land 1.2 MB on disk."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        [row] = plan.rows
        assert row.action == "write"
        row.body = "word " * 240_000
        apply_ingest_plan(plan, store)
        # Re-fetch: `apply_ingest_plan` mutates the row in place, which
        # mypy's narrowing on `row.action` above can't see.
        [row_after] = plan.rows
        assert row_after.action == "skip_invalid"
        assert "max_content_bytes" in (row_after.reason or "")
        assert store.load_all() == []


class TestScopeAllowlistPlanMatchesApply:
    """A `--dry-run` under `[scopes] allowed` must predict the commit.

    For one commit it did not: the allowlist was checked in
    `apply_ingest_plan` only, so `compute_ingest_plan` — which is what
    `--dry-run` renders and what `doctor._check_auto_memory_stranded`
    reads — reported `write` for rows the commit refused. Reproduced with
    `allowed=["projects:demo"]` and `ingest --scope projects:demo`: "would
    write 1", then `wrote 0 / skip invalid 1`.

    That is the same dry-run-lies class the `--scope` pre-validation in
    `cli/ingest.py` and `resolve_dedup_policy` both exist to prevent, so
    it is pinned the same way: on the summary, which is what the operator
    actually reads off the two runs.
    """

    @staticmethod
    def _both(
        source_root: Path, store: Store, *, allowed: list[str], extra: list[str]
    ) -> tuple[dict[str, int], dict[str, int], int]:
        """`(dry-run summary, commit summary, rows on disk)`."""
        config = Config(scopes=ScopesConfig(allowed=allowed))

        def _plan() -> Any:
            return compute_ingest_plan(
                source_root,
                existing_memories=store.load_all(),
                existing_tombstones=store.load_tombstones(),
                extra_scopes=extra,
                config=config,
            )

        dry = _plan()
        live = _plan()
        apply_ingest_plan(live, store, config=config)
        return dry.summary, live.summary, len(store.load_all())

    def test_dry_run_counts_match_the_commit_for_an_allowlist_user(
        self, source_root: Path, store: Store
    ) -> None:
        """The reported repro. Fails without the plan-side check AND
        without the stamp exemption, for opposite reasons: with neither,
        the dry-run promises 2 writes and the commit delivers 0; with only
        the plan-side check, the two agree on 0 and the allowlist user
        still cannot import anything."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        _write_auto_memory(
            source_root,
            "second",
            auto_type="project",
            description="the release workflow",
            body="The release tag push triggers the PyPI publish workflow.",
        )
        dry, live, on_disk = self._both(
            source_root, store, allowed=["projects:demo"], extra=["projects:demo"]
        )
        assert dry == live
        assert dry["write"] == 2
        assert on_disk == 2

    def test_dry_run_counts_match_the_commit_when_rows_are_refused(
        self, source_root: Path, store: Store
    ) -> None:
        """The other direction, and the one the plan-side check is really
        for: a genuinely-unsanctioned `--scope` is `skip_invalid` in the
        plan too, so the dry-run never promises a write the commit refuses.
        Fails without threading `config` into `compute_ingest_plan` — the
        plan reports `write: 1` against the commit's `skip_invalid: 1`."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        dry, live, on_disk = self._both(
            source_root, store, allowed=["tools"], extra=["rogue"]
        )
        assert dry == live
        assert dry["write"] == 0
        assert dry["skip_invalid"] == 1
        assert on_disk == 0

    def test_plan_refuses_scope_before_dedup_just_like_the_apply(
        self, source_root: Path, store: Store
    ) -> None:
        """Agreement on the COUNT is not enough — the two sides must also
        agree on the reason. `apply_ingest_plan` runs the allowlist ahead
        of its gate chain (the order `memory_write` uses), so the plan runs
        it ahead of the dedup pass: a row that is both a duplicate and
        unsanctioned reads `skip_invalid` on both sides rather than
        `skip_duplicate` in the plan and `skip_invalid` at commit."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        config = Config(scopes=ScopesConfig(allowed=["tools"]))
        # First import with no allowlist, so the store holds the memory
        # this row now duplicates.
        seed = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(seed, store)
        assert len(store.load_all()) == 1

        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["rogue"],
            config=config,
        )
        [row] = plan.rows
        assert row.action == "skip_invalid"
        assert "not in allowed list" in (row.reason or "")
        # Same verdict from the apply side, on a plan it did not compute.
        apply_ingest_plan(plan, store, config=config)
        assert row.action == "skip_invalid"
        assert len(store.load_all()) == 1

    def test_plan_without_config_enforces_nothing(
        self, source_root: Path, store: Store
    ) -> None:
        """`config` is optional on `compute_ingest_plan` and omitting it
        means "no allowlist", not "an empty one I should invent". That is
        what keeps `doctor._check_auto_memory_stranded` — a read-only
        caller that passes no config — classifying exactly as it did
        before this parameter existed."""
        _write_auto_memory(
            source_root,
            "ordinary",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
            extra_scopes=["rogue"],
        )
        assert [r.action for r in plan.rows] == ["write"]


# ---------------------------------------------------------------------------
# apply_ingest_plan — origin capture (honest-evidence gate)
# ---------------------------------------------------------------------------


class TestApplyIngestPlanOrigin:
    """Write-time origin lands ONLY when the plan's source root is the
    auto-memory directory keyed to the ingesting cwd AND the session
    `.jsonl` records alongside it don't contradict that claim.

    The layout (`~/.claude/projects/<sanitized-cwd>/memory/`) is keyed
    to the writing session's cwd, but the sanitization is MANY-TO-ONE
    (`web-app` / `web.app` / `web/app` collide), so path equality alone
    can be satisfied from a colliding foreign project — the session
    transcripts' real `cwd` values are the cross-check. Any other
    `--from` root keeps the conservative `origin=None` ("global")
    default. Before origin stamping existed, every ingested memory
    landed `origin=None` and could never satisfy the audit's
    positive-evidence suppression gate — recurring false `search_miss`
    findings for in-project continuations."""

    @pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
    def test_matching_auto_memory_root_captures_origin(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """Source root == the auto-memory dir for `cwd`, and `cwd` is a
        git checkout with a remote: the written memory carries the full
        origin block (cwd, repo, worktree_root)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd = tmp_path / "checkout"
        cwd.mkdir()
        _init_git_repo(cwd, remote="git@github.com:example/repo.git")

        auto_dir = _auto_memory_dir_for(cwd, fake_home)
        _write_auto_memory(auto_dir, "origin-test", description="hello", body="world")

        plan = compute_ingest_plan(
            auto_dir,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store, cwd=cwd)

        [row] = plan.rows
        assert row.written_id is not None
        [written] = [m for m in store.load_all() if m.id == row.written_id]
        assert written.origin is not None
        assert written.origin.cwd == str(cwd.resolve())
        assert written.origin.repo == "git@github.com:example/repo.git"
        assert written.origin.worktree_root == str(cwd.resolve())
        # Branch is deliberately nulled: the source files come from many
        # historical sessions, and stamping them with the branch the
        # ingest happens to run on would be misinformation — the same
        # documented stance migrate.py takes for its origin backfill.
        assert written.origin.branch is None

    def test_matching_root_without_repo_still_captures_cwd(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """A matching auto-memory root under a NON-repo cwd still gets
        `origin.cwd` (honest: the dir is keyed to this cwd) with
        repo/worktree_root null — same degrade `capture` itself uses
        outside a checkout. The discovery ceiling keeps the non-repo
        premise honest when tmp_path itself sits under a real checkout
        (poisoned basetemp/TMPDIR)."""
        set_git_discovery_ceiling(tmp_path, monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd = tmp_path / "plain-dir"
        cwd.mkdir()

        auto_dir = _auto_memory_dir_for(cwd, fake_home)
        _write_auto_memory(auto_dir, "no-repo", description="hello", body="world")

        plan = compute_ingest_plan(
            auto_dir,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store, cwd=cwd)

        [row] = plan.rows
        assert row.written_id is not None
        [written] = [m for m in store.load_all() if m.id == row.written_id]
        assert written.origin is not None
        assert written.origin.cwd == str(cwd.resolve())
        assert written.origin.repo is None
        assert written.origin.worktree_root is None

    def test_non_matching_source_root_keeps_origin_none(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """An explicit `--from` pointing anywhere other than the cwd's
        own auto-memory dir keeps `origin=None` — no evidence the
        content belongs to this checkout, so the conservative "global"
        default stands."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd = tmp_path / "checkout"
        cwd.mkdir()
        source = tmp_path / "copied-from-elsewhere"
        _write_auto_memory(source, "no-origin", description="hello", body="world")

        plan = compute_ingest_plan(
            source,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store, cwd=cwd)

        [row] = plan.rows
        assert row.written_id is not None
        [written] = [m for m in store.load_all() if m.id == row.written_id]
        assert written.origin is None

    def test_colliding_sanitized_dir_keeps_origin_none(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """The sanitized layout is many-to-one (`web-app` and `web.app`
        fold to the same directory), so the default-root path equality
        can be satisfied from the WRONG project. The session `.jsonl`
        records alongside `memory/` carry each writing session's real
        cwd; when any of them resolves elsewhere, the stamp is skipped
        and the conservative `origin=None` default stands — one
        matching record doesn't rescue it (ambiguity is disqualifying)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        projects = tmp_path / "projects"
        foreign = projects / "web.app"
        foreign.mkdir(parents=True)
        cwd = projects / "web-app"
        cwd.mkdir()

        auto_dir = _auto_memory_dir_for(cwd, fake_home)
        # The collision is the premise — pin it.
        assert auto_dir == _auto_memory_dir_for(foreign, fake_home)
        _write_auto_memory(auto_dir, "foreign-note", description="hello", body="world")
        # Session evidence: this shared directory's transcripts include
        # one written from the foreign sibling.
        (auto_dir.parent / "session-a.jsonl").write_text(
            json.dumps({"cwd": str(foreign)}) + "\n"
        )
        (auto_dir.parent / "session-b.jsonl").write_text(
            json.dumps({"cwd": str(cwd)}) + "\n"
        )

        plan = compute_ingest_plan(
            auto_dir,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store, cwd=cwd)

        [row] = plan.rows
        assert row.written_id is not None
        [written] = [m for m in store.load_all() if m.id == row.written_id]
        assert written.origin is None

    def test_matching_session_evidence_still_stamps_origin(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """Session records whose cwd resolves to the ingest cwd are
        confirming evidence — the stamp proceeds. (The no-`.jsonl`-at-all
        case is covered by the capture tests above, which create none.)"""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd = tmp_path / "checkout"
        cwd.mkdir()

        auto_dir = _auto_memory_dir_for(cwd, fake_home)
        _write_auto_memory(auto_dir, "own-note", description="hello", body="world")
        (auto_dir.parent / "session-a.jsonl").write_text(
            json.dumps({"cwd": str(cwd)}) + "\n"
        )

        plan = compute_ingest_plan(
            auto_dir,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store, cwd=cwd)

        [row] = plan.rows
        assert row.written_id is not None
        [written] = [m for m in store.load_all() if m.id == row.written_id]
        assert written.origin is not None
        assert written.origin.cwd == str(cwd.resolve())


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_dry_run_says_would_write(self, source_root: Path, store: Store) -> None:
        _write_auto_memory(source_root, "x")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        text = render_ingest_text(plan, dry_run=True)
        assert "--dry-run" in text
        assert "would write" in text

    def test_commit_says_wrote_with_ids(self, source_root: Path, store: Store) -> None:
        _write_auto_memory(source_root, "x")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        apply_ingest_plan(plan, store)
        text = render_ingest_text(plan, dry_run=False)
        assert "wrote" in text
        # The written id surfaces in the row line.
        [row] = plan.rows
        assert row.written_id is not None
        assert row.written_id in text


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


class TestDiscoverDefaultSourceRoot:
    def test_returns_none_when_no_auto_memory_for_cwd(self, tmp_path: Path) -> None:
        """A cwd with no `~/.claude/projects/<sanitized>/memory/`
        dir resolves to None — CLI treats that as "pass --from"."""
        # Pick a path guaranteed not to exist under the user's
        # ~/.claude/projects tree.
        result = discover_default_source_root(tmp_path / "fake-project-path")
        assert result is None

    def test_finds_auto_memory_for_simple_cwd(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The positive case: a cwd whose sanitised name maps to an
        existing `~/.claude/projects/<sanitised>/memory/` directory
        resolves to that path. Without this test, the sanitisation
        algorithm has no lock-in — a refactor that reordered the
        replaces or restored a slash-only behaviour would still pass
        the negative test."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # `/Users/me/projects/foo` → `-Users-me-projects-foo`
        # Mirrors Claude Code's real scheme (every non-alphanumeric char
        # folds to `-`), so on Windows the drive-letter colon in
        # `C:\\Users\\...` becomes a valid filename component instead of
        # one Windows rejects with WinError 123.
        cwd = tmp_path / "cwd_simple" / "projects" / "foo"
        cwd.mkdir(parents=True)
        resolved = cwd.resolve().as_posix().lstrip("/")
        sanitised = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
        target = fake_home / ".claude" / "projects" / sanitised / "memory"
        target.mkdir(parents=True)

        assert discover_default_source_root(cwd) == target

    def test_finds_auto_memory_for_dotted_cwd(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Locks in the 2.7.0 audit-fix: cwds containing a dot (a
        common shape for `.claude/worktrees/*`, hidden dirs, or
        version-suffixed paths) sanitise BOTH `/` and `.` to `-`.
        An earlier slash-only sanitiser silently missed every dotted
        path. Without this test, the dot-replacement is a one-line
        change a future maintainer could revert."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Mirrors the `.claude/worktrees/<branch>` layout the audit
        # comment calls out. Same Windows-aware normalisation as the
        # simple-cwd test above.
        cwd = tmp_path / "repo" / ".claude" / "worktrees" / "feat-branch"
        cwd.mkdir(parents=True)
        resolved = cwd.resolve().as_posix().lstrip("/")
        sanitised = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
        # The sanitised name must contain `--claude-` (the dot before
        # `claude` mapped to a second `-`); if a refactor produces
        # `-.claude-` instead, this assertion fails fast.
        assert "--claude-" in sanitised
        target = fake_home / ".claude" / "projects" / sanitised / "memory"
        target.mkdir(parents=True)

        assert discover_default_source_root(cwd) == target

    def test_finds_auto_memory_for_cwd_with_special_chars(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Locks in the Claude-Code-correct sanitiser: EVERY
        non-alphanumeric character folds to `-`
        (`path.replace(/[^a-zA-Z0-9]/g, "-")`), so a snake_case /
        punctuation-bearing checkout like `.../my_repo!` lives at
        `...-my-repo-`. The earlier 3-char sanitiser (`/`, `.`, `:`
        only) left `_` and `!` intact, resolved to a directory that
        does not exist, and returned None — invisibly for `ingest`
        auto-discovery and as a WRONG negative for the doctor's
        `auto_memory_stranded` check. The fixture dir is created under
        the fully-folded name; discovery MUST find it. Fails against the
        pre-fix 3-char sanitiser (which would probe `...-my_repo!`)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # `_` and `!` are the load-bearing characters: the legacy
        # sanitiser preserved both, the real one folds both to `-`.
        cwd = tmp_path / "code" / "my_repo!"
        cwd.mkdir(parents=True)
        resolved = cwd.resolve().as_posix().lstrip("/")
        sanitised = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
        assert sanitised.endswith("-my-repo-")
        assert "_" not in sanitised and "!" not in sanitised
        target = fake_home / ".claude" / "projects" / sanitised / "memory"
        target.mkdir(parents=True)

        assert discover_default_source_root(cwd) == target

    def test_legacy_sanitised_layout_still_resolves(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Belt-and-suspenders for the dual-probe: a directory named
        under bettermemory's OLD 3-char sanitiser (which left `_`
        intact) must keep resolving after the switch to Claude Code's
        full-fold scheme — discovery probes both candidates and returns
        whichever exists. Guards the legacy fallback against a future
        refactor that drops it. (Passes pre- and post-fix — a
        preservation guard, not the mutation-sound pin above.)"""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd = tmp_path / "legacy_named"
        cwd.mkdir(parents=True)
        resolved = cwd.resolve().as_posix().lstrip("/")
        legacy = "-" + resolved.replace("/", "-").replace(".", "-").replace(":", "")
        new = "-" + re.sub(r"[^A-Za-z0-9]", "-", resolved)
        # Premise: the `_` makes the two schemes diverge, so only the
        # legacy-named directory exists on disk here.
        assert legacy != new
        target = fake_home / ".claude" / "projects" / legacy / "memory"
        target.mkdir(parents=True)

        assert discover_default_source_root(cwd) == target


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLI:
    def test_ingest_dry_run_smoke(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-1")
        store_dir = tmp_path / "store"

        monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
        monkeypatch.setattr(
            sys, "argv", ["bettermemory", "ingest", "--from", str(source), "--dry-run"]
        )
        server_main()

        captured = capsys.readouterr()
        assert "bettermemory ingest --dry-run" in captured.out
        assert "cli-test-1" in captured.out

    def test_ingest_commit_persists_to_store(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-2")
        store_dir = tmp_path / "store"

        monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
        monkeypatch.setattr(
            sys, "argv", ["bettermemory", "ingest", "--from", str(source)]
        )
        server_main()

        captured = capsys.readouterr()
        assert "wrote" in captured.out
        # The store now contains exactly one memory carrying the
        # provenance scope.
        store = Store(store_dir)
        all_mems = store.load_all()
        assert len(all_mems) == 1
        assert DEFAULT_PROVENANCE_SCOPE in all_mems[0].scopes

    def test_ingest_force_commits_the_duplicate(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        """`--force` end-to-end through `main()`.

        The flag reached `compute_ingest_plan` but not `apply_ingest_plan`,
        so the apply loop's own `DedupActiveGate` refused every forced row
        — with a hint telling the operator to pass `force=True`, which is
        what they had just done. Nothing below the CLI can catch that:
        both halves work in isolation, the threading is the defect.
        """
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(
            source,
            "cli-force",
            description="ripgrep over grep",
            body="The team uses ripgrep instead of grep.",
        )
        store_dir = tmp_path / "store"
        monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))

        monkeypatch.setattr(
            sys, "argv", ["bettermemory", "ingest", "--from", str(source)]
        )
        server_main()
        capsys.readouterr()
        assert len(Store(store_dir).load_all()) == 1

        monkeypatch.setattr(
            sys, "argv", ["bettermemory", "ingest", "--from", str(source), "--force"]
        )
        server_main()

        out = capsys.readouterr().out
        assert "write gate refused" not in out
        assert len(Store(store_dir).load_all()) == 2

    def test_ingest_json_output(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-3")
        store_dir = tmp_path / "store"

        monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
        monkeypatch.setattr(
            sys,
            "argv",
            ["bettermemory", "ingest", "--from", str(source), "--json", "--dry-run"],
        )
        server_main()

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["summary"]["total"] == 1
        assert parsed["summary"]["write"] == 1
        assert parsed["rows"][0]["title"] == "cli-test-3"

    def test_ingest_missing_source_errors_cleanly(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from bettermemory.server import main as server_main

        monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
        monkeypatch.setattr(
            sys,
            "argv",
            ["bettermemory", "ingest", "--from", str(tmp_path / "nope"), "--dry-run"],
        )
        with pytest.raises(SystemExit):
            server_main()

    def test_ingest_honours_telemetry_opt_out(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        """`[telemetry] enabled = false` + a committing ingest run: the
        memories land, but no event segment is created and nothing is
        appended to a pre-existing one. Third instance of the
        enabled=-omission class (the Stop hook shipped the same bug —
        see test_hook's telemetry-disabled regression): the recorder in
        cli/ingest.py must thread `ctx.config.telemetry` like every
        other construction site. The whole class is pinned statically
        by the AST sweep in test_events.py; this is the behavioural
        half for the lane that regressed."""
        import argparse

        from bettermemory.cli.ingest import _cli_ingest
        from bettermemory.config import Config, StorageConfig, TelemetryConfig
        from bettermemory.events import (
            _SEGMENT_TEMPLATE,
            EVENT_LOG_FILENAME,
            SHARD_COUNT,
        )

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        cfg = Config(
            storage=StorageConfig(directory=str(store_dir)),
            telemetry=TelemetryConfig(enabled=False),
        )
        monkeypatch.setattr("bettermemory.cli._common.load_config", lambda: cfg)

        def _ingest(source: Path) -> None:
            _cli_ingest(
                source=str(source),
                dry_run=False,
                extra_scopes=[],
                force=False,
                json_out=False,
                parser=argparse.ArgumentParser(),
            )
            capsys.readouterr()

        # Lane 1 — no log exists: the opted-out run must not create one.
        source_a = tmp_path / "source-a"
        _write_auto_memory(source_a, "opt-out-a", body="lane one body prose")
        _ingest(source_a)
        log = store_dir / EVENT_LOG_FILENAME
        assert len(Store(store_dir).load_all()) == 1  # the ingest committed
        # ...but conjured no event log — neither legacy nor any shard.
        assert not list(store_dir.glob(".events*.jsonl"))
        assert not list(store_dir.glob(".events-*.jsonl.gz"))  # nor rotation residue

        # Lane 2 — segments already exist (written while telemetry was
        # on): the opted-out run must not append to any of them.
        #
        # Since 3.24.0 the active log is sharded: a Recorder appends to
        # `.events.NN.jsonl` for NN = crc32(session_id) % SHARD_COUNT,
        # and `_cli_ingest` mints a fresh random `SessionState()` per
        # run — so the shard this run would pick is not knowable from
        # here. Seed EVERY shard and require all of them back
        # byte-identical; whichever one the run would have chosen, the
        # append shows up. (Pinning only the legacy `.events.jsonl`,
        # as this lane did before 3.24.0, is now vacuous: no code path
        # writes there any more — it is read-only merge input.)
        def _seed_segments(root: Path) -> dict[Path, str]:
            """Seed the legacy log plus all `SHARD_COUNT` shards with a
            per-file sentinel; return {path: expected bytes}. Distinct
            per file so a cross-shard mixup can't hide behind equality."""
            seeded = {
                root / _SEGMENT_TEMPLATE.format(shard): (
                    f'{{"ts":"2026-01-01T00:00:{shard:02d}Z",'
                    f'"session":"old-{shard:02d}","kind":"search"}}\n'
                )
                for shard in range(SHARD_COUNT)
            }
            seeded[root / EVENT_LOG_FILENAME] = (
                '{"ts":"2026-01-01T00:00:00Z","session":"legacy","kind":"search"}\n'
            )
            for path, text in seeded.items():
                path.write_text(text, encoding="utf-8")
            return seeded

        def _grown(seeded: dict[Path, str]) -> list[str]:
            """Names of seeded segments whose bytes changed."""
            return sorted(
                path.name
                for path, text in seeded.items()
                if path.read_text(encoding="utf-8") != text
            )

        segments = _seed_segments(store_dir)
        source_b = tmp_path / "source-b"
        _write_auto_memory(source_b, "opt-out-b", body="lane two body prose")
        _ingest(source_b)
        assert len(Store(store_dir).load_all()) == 2
        assert _grown(segments) == []  # every segment byte-identical
        # ...and no segment outside the seeded set was conjured either.
        assert set(store_dir.glob(".events*.jsonl")) == set(segments)
        assert not list(store_dir.glob(".events-*.jsonl.gz"))
        assert log.read_text(encoding="utf-8") == segments[log]  # legacy intact

        # Contrast lane — telemetry on: the same run shape DOES record
        # the ingest `write` event, so the opt-out pins above cannot
        # pass vacuously (e.g. if apply_ingest_plan stopped recording).
        store_dir2 = tmp_path / "store2"
        store_dir2.mkdir()
        cfg2 = Config(
            storage=StorageConfig(directory=str(store_dir2)),
            telemetry=TelemetryConfig(enabled=True),
        )
        monkeypatch.setattr("bettermemory.cli._common.load_config", lambda: cfg2)
        source_c = tmp_path / "source-c"
        _write_auto_memory(source_c, "opt-in-c")
        _ingest(source_c)
        from bettermemory.events import iter_events

        assert list(store_dir2.glob(".events*.jsonl"))  # a shard was created
        events = list(iter_events(store_dir2))
        assert [e["kind"] for e in events] == ["write"]
        assert events[0]["triggered_from"] == "cli_ingest"

        # Proof lane — telemetry on, segments pre-seeded exactly as in
        # lane 2. Demonstrates in-band that lane 2's "all segments
        # byte-identical" pin is *able* to fail: with the opt-out off,
        # precisely one seeded segment grows. It also pins the separate
        # (and still true) sharding-layout invariant that the grown
        # segment is always a shard, never the legacy `.events.jsonl`.
        store_dir3 = tmp_path / "store3"
        store_dir3.mkdir()
        seeded3 = _seed_segments(store_dir3)
        cfg3 = Config(
            storage=StorageConfig(directory=str(store_dir3)),
            telemetry=TelemetryConfig(enabled=True),
        )
        monkeypatch.setattr("bettermemory.cli._common.load_config", lambda: cfg3)
        source_d = tmp_path / "source-d"
        _write_auto_memory(source_d, "opt-in-d", body="proof lane body prose")
        _ingest(source_d)
        assert len(Store(store_dir3).load_all()) == 1
        grown = _grown(seeded3)
        assert len(grown) == 1, grown
        assert grown[0] != EVENT_LOG_FILENAME  # a shard, not the legacy log
        assert re.fullmatch(r"\.events\.\d{2}\.jsonl", grown[0])

    def test_ingest_under_a_scope_allowlist_dry_run_matches_commit(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        """The reported repro, on the surface it was reported from.

        `[scopes] allowed = ["projects:demo"]` plus
        `ingest --scope projects:demo` printed `would write 1` and then
        `wrote 0 / skip invalid 1`, refusing the row for
        `imported-from-claude-code` and `feedback` — two scopes the
        operator never typed. Both halves are pinned here rather than only
        in the library tests because neither half is visible below the
        CLI: the plan/apply agreement needs one caller running both legs
        off one `Config`, and the "would write" wording lives in
        `render_ingest_text`.
        """
        import argparse

        from bettermemory.cli.ingest import _cli_ingest
        from bettermemory.config import Config, ScopesConfig, StorageConfig

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        cfg = Config(
            storage=StorageConfig(directory=str(store_dir)),
            scopes=ScopesConfig(allowed=["projects:demo"]),
        )
        monkeypatch.setattr("bettermemory.cli._common.load_config", lambda: cfg)

        source = tmp_path / "source"
        _write_auto_memory(
            source,
            "allowlisted",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )

        def _run(dry_run: bool) -> str:
            _cli_ingest(
                source=str(source),
                dry_run=dry_run,
                extra_scopes=["projects:demo"],
                force=False,
                json_out=False,
                parser=argparse.ArgumentParser(),
            )
            return capsys.readouterr().out

        # Matched by regex, not by literal column spacing: the claim is
        # "the count is 1 on both legs", and hardcoding the renderer's
        # padding would make this test fail on a reflow that changed
        # nothing about the behaviour it exists to pin.
        dry = _run(True)
        assert re.search(r"^\s+would write\s+1$", dry, re.MULTILINE), dry
        assert "skip invalid" not in dry
        assert len(Store(store_dir).load_all()) == 0  # a dry run wrote nothing

        live = _run(False)
        assert re.search(r"^\s+wrote\s+1$", live, re.MULTILINE), live
        assert "skip invalid" not in live
        [stored] = Store(store_dir).load_all()
        # The two stamps rode along; only `--scope` was ever checked.
        assert stored.scopes == [DEFAULT_PROVENANCE_SCOPE, "feedback", "projects:demo"]

    def test_cli_dry_run_predicts_the_allowlist_refusal(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        """The REFUSING half of the same agreement, pinned on the CLI.

        The sibling test above runs a `--scope` the allowlist permits, so
        plan and apply agree at `write` — and they agree there whether or
        not the plan was told about `[scopes] allowed`, because a plan
        that knows nothing about the knob also says `write`. That makes it
        blind to the one line the agreement actually rests on: the
        `config=ctx.config` argument `cli/ingest.py` threads into
        `compute_ingest_plan`. Deleting that argument was verified by
        mutation testing to leave the entire suite green.

        This lane is the half that is NOT blind. With a `--scope` OUTSIDE
        the allowlist the two legs only agree if the plan was handed the
        config: without it the dry run prints `would write 1` for a row
        the commit then refuses `skip_invalid` — the exact `--dry-run` lie
        the fix exists to remove, and a lie that is worse than useless
        because `--dry-run` is what an operator reaches for precisely when
        they are unsure the run will do what they want.

        Nothing below the CLI can catch this: `compute_ingest_plan` and
        `apply_ingest_plan` each enforce the list correctly in isolation
        (both are covered directly elsewhere in this file) — the wiring
        that hands the plan leg a config is the thing under test, and the
        CLI is its only caller.

        The APPLY leg of that wiring is out of this lane's reach and is
        pinned separately by
        `test_cli_dry_run_predicts_the_semantic_dedup_verdict`: the apply
        loop skips every row the plan did not mark `write`, so no
        allowlist scenario can distinguish an apply that was handed the
        config from one that was not.
        """
        import argparse

        from bettermemory.cli.ingest import _cli_ingest
        from bettermemory.config import Config, ScopesConfig, StorageConfig

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        cfg = Config(
            storage=StorageConfig(directory=str(store_dir)),
            scopes=ScopesConfig(allowed=["projects:demo"]),
        )
        monkeypatch.setattr("bettermemory.cli._common.load_config", lambda: cfg)

        source = tmp_path / "source"
        _write_auto_memory(
            source,
            "outside-the-allowlist",
            description="the parser lives in ingest.py",
            body="Auto-memory files are read from the project memory dir.",
        )

        def _run(dry_run: bool) -> str:
            _cli_ingest(
                source=str(source),
                dry_run=dry_run,
                # A well-formed scope (`validate_scope` passes it, so the
                # up-front `parser.error` arm is not what refuses this
                # row) that simply is not on the list.
                extra_scopes=["projects:other"],
                force=False,
                json_out=False,
                parser=argparse.ArgumentParser(),
            )
            return capsys.readouterr().out

        def _detail(out: str) -> list[str]:
            """The per-row lines of `render_ingest_text`: the
            `[action] file …` line and its indented reason.

            These are what "the same per-row outcome and the same reason"
            means concretely, and the renderer emits them identically on
            both legs — the banner and the `would write`/`wrote` verb are
            the only lines that are SUPPOSED to differ, which is why they
            are filtered out here instead of being compared."""
            return [
                line
                for line in out.splitlines()
                if line.startswith("  [") or line.startswith("      ")
            ]

        dry = _run(True)
        # The headline claim. Under the mutation this line is absent and
        # `would write 1` is printed in its place.
        assert re.search(r"^\s+skip invalid\s+1$", dry, re.MULTILINE), dry
        assert re.search(r"^\s+would write\s+0$", dry, re.MULTILINE), dry
        assert len(Store(store_dir).load_all()) == 0  # a dry run wrote nothing

        live = _run(False)
        assert re.search(r"^\s+skip invalid\s+1$", live, re.MULTILINE), live
        assert re.search(r"^\s+wrote\s+0$", live, re.MULTILINE), live
        # The commit refused it too, so the dry run was not merely
        # pessimistic — it was right.
        assert len(Store(store_dir).load_all()) == 0

        # Same ACTION and same REASON, not merely the same counts: a plan
        # that reached `skip_invalid` by some other route (dedup, say)
        # would satisfy the count assertions above while still telling the
        # operator the wrong thing about why.
        assert _detail(dry) == _detail(live), (dry, live)
        assert any(
            "scope(s) not in allowed list: ['projects:other']" in line
            for line in _detail(dry)
        ), dry
