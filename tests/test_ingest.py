"""Tests for the `bettermemory ingest` module + CLI subcommand.

Covers the auto-memory parser (frontmatter `name`/`description`/`type`),
the type→category mapping, dedup against the active store and
tombstones, the skip reasons (invalid / empty / duplicate / tombstone),
plus an end-to-end CLI smoke through `main()`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory.ingest import (
    DEFAULT_PROVENANCE_SCOPE,
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
        (source_root / "README.md").write_text("# Auto-memory readme\n")
        _write_auto_memory(source_root, "real-mem", auto_type="feedback")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        names = {r.source_path.name for r in plan.rows}
        assert names == {"real-mem.md"}

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

    def test_flat_type_key_is_honored(
        self, source_root: Path, store: Store
    ) -> None:
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
                "ripgrep is the preferred grep tool\n\n"
                "Use ripgrep instead of grep.\n"
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
    def test_write_action_lands_in_store(
        self, source_root: Path, store: Store
    ) -> None:
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

    def test_skip_actions_do_not_write(
        self, source_root: Path, store: Store
    ) -> None:
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
            source_root, "user-claim", auto_type="user",
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
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_dry_run_says_would_write(
        self, source_root: Path, store: Store
    ) -> None:
        _write_auto_memory(source_root, "x")
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
        text = render_ingest_text(plan, dry_run=True)
        assert "--dry-run" in text
        assert "would write" in text

    def test_commit_says_wrote_with_ids(
        self, source_root: Path, store: Store
    ) -> None:
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


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLI:
    def test_ingest_dry_run_smoke(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-1")
        store_dir = tmp_path / "store"

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(store_dir)
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "ingest", "--from", str(source), "--dry-run"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        assert "bettermemory ingest --dry-run" in captured.out
        assert "cli-test-1" in captured.out

    def test_ingest_commit_persists_to_store(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-2")
        store_dir = tmp_path / "store"

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(store_dir)
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "ingest", "--from", str(source)]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        assert "wrote" in captured.out
        # The store now contains exactly one memory carrying the
        # provenance scope.
        store = Store(store_dir)
        all_mems = store.load_all()
        assert len(all_mems) == 1
        assert DEFAULT_PROVENANCE_SCOPE in all_mems[0].scopes

    def test_ingest_json_output(self, tmp_path: Path, capsys: Any) -> None:
        from bettermemory.server import main as server_main

        source = tmp_path / "source"
        _write_auto_memory(source, "cli-test-3")
        store_dir = tmp_path / "store"

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(store_dir)
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "ingest", "--from", str(source), "--json", "--dry-run"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["summary"]["total"] == 1
        assert parsed["summary"]["write"] == 1
        assert parsed["rows"][0]["title"] == "cli-test-3"

    def test_ingest_missing_source_errors_cleanly(self, tmp_path: Path) -> None:
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "store")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "ingest", "--from", str(tmp_path / "nope"), "--dry-run"]
        try:
            with pytest.raises(SystemExit):
                server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save
