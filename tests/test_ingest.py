"""Tests for the `bettermemory ingest` module + CLI subcommand.

Covers the auto-memory parser (frontmatter `name`/`description`/`type`),
the type→category mapping, dedup against the active store and
tombstones, the skip reasons (invalid / empty / duplicate / tombstone),
plus an end-to-end CLI smoke through `main()`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import typing

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

    Mirrors the production sanitiser in `discover_default_source_root`
    (`/`, `.` → `-` after stripping the leading `/`; `:` stripped for
    Windows drive letters — same Windows-aware normalisation as the
    `TestDiscoverDefaultSourceRoot` cases)."""
    resolved = cwd.resolve().as_posix().lstrip("/")
    sanitised = "-" + resolved.replace("/", "-").replace(".", "-").replace(":", "")
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
        gates have to be controllable independently."""
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


# ---------------------------------------------------------------------------
# apply_ingest_plan — origin capture (honest-evidence gate)
# ---------------------------------------------------------------------------


class TestApplyIngestPlanOrigin:
    """Write-time origin lands ONLY when the plan's source root is the
    auto-memory directory keyed to the ingesting cwd.

    That layout is per-project-cwd by construction
    (`~/.claude/projects/<sanitized-cwd>/memory/`), so `capture(cwd)`
    is honest provenance there. Any other `--from` root keeps the
    conservative `origin=None` ("global") default. Before this fix,
    every ingested memory landed `origin=None` and could never satisfy
    the audit's positive-evidence suppression gate — recurring false
    `search_miss` findings for in-project continuations."""

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

    def test_matching_root_without_repo_still_captures_cwd(
        self, tmp_path: Path, store: Store, monkeypatch: Any
    ) -> None:
        """A matching auto-memory root under a NON-repo cwd still gets
        `origin.cwd` (honest: the dir is keyed to this cwd) with
        repo/worktree_root null — same degrade `capture` itself uses
        outside a checkout."""
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
        # On Windows `as_posix()` + `:` strip mirrors the production
        # sanitiser so `C:\\Users\\...` becomes a valid filename
        # component instead of one Windows rejects with WinError 123.
        cwd = tmp_path / "cwd_simple" / "projects" / "foo"
        cwd.mkdir(parents=True)
        resolved = cwd.resolve().as_posix().lstrip("/")
        sanitised = "-" + resolved.replace("/", "-").replace(".", "-").replace(":", "")
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
        sanitised = "-" + resolved.replace("/", "-").replace(".", "-").replace(":", "")
        # The sanitised name must contain `--claude-` (the dot before
        # `claude` mapped to a second `-`); if a refactor produces
        # `-.claude-` instead, this assertion fails fast.
        assert "--claude-" in sanitised
        target = fake_home / ".claude" / "projects" / sanitised / "memory"
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
