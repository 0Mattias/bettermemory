"""Tests for store.py — filesystem CRUD and tombstone behavior."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

import pytest
from pydantic import ValidationError

from bettermemory.models import (
    Confidence,
    Memory,
    Source,
    generate_ulid,
    is_valid_ulid,
)
from bettermemory.store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
    count_unparseable_memory_files,
)


def test_write_and_read_back(store: Store) -> None:
    memory = store.write(
        content="Prefer code-driven tutorials.",
        scopes=["learning-style", "tools"],
    )
    assert is_valid_ulid(memory.id)

    loaded = store.load_one(memory.id)
    assert loaded.id == memory.id
    assert loaded.scopes == ["learning-style", "tools"]
    assert loaded.confidence is Confidence.MEDIUM
    assert loaded.source is Source.EXPLICIT
    assert "code-driven tutorials" in loaded.body
    # Filename embeds the date.
    assert any(
        p.name.startswith(memory.created.strftime("%Y-%m-%d"))
        for p in store.root.iterdir()
    )


def test_write_records_creation_and_update_timestamps(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    assert memory.created == memory.updated


def test_update_bumps_updated_only(store: Store) -> None:
    memory = store.write(content="initial", scopes=["tools"])
    original_created = memory.created
    time.sleep(0.01)

    new = memory.model_copy(update={"body": "edited\n"})
    updated = store.update(new)

    assert updated.created == original_created
    assert updated.updated > original_created
    # Disk reflects the change.
    re_loaded = store.load_one(memory.id)
    assert "edited" in re_loaded.body


def test_load_all_skips_tombstoned(store: Store) -> None:
    a = store.write(content="alive", scopes=["tools"])
    b = store.write(content="dying", scopes=["tools"])

    store.tombstone(b.id, reason="superseded")

    ids = {m.id for m in store.load_all()}
    assert a.id in ids
    assert b.id not in ids


def test_tombstone_preserves_body_and_adds_removal_metadata(store: Store) -> None:
    memory = store.write(content="goodbye world", scopes=["tools"])
    path = store.tombstone(memory.id, reason="user said so")

    assert path.exists()
    text = path.read_text()
    assert "goodbye world" in text
    assert "removed:" in text
    assert "user said so" in text
    # Tombstone lives under .tombstones/.
    assert path.parent == store.tombstone_dir


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
def test_tombstone_dir_has_owner_only_permissions(store: Store) -> None:
    """The tombstone directory is created with mode 0o700 explicitly,
    not via umask. Stored tombstones carry the same trust boundary as
    active memories — directory-listing them should require the owner."""
    mode = store.tombstone_dir.stat().st_mode & 0o777
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


def test_tombstoned_memory_load_one_raises_clearly(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="bad fact")

    with pytest.raises(TombstonedError) as excinfo:
        store.load_one(memory.id)
    assert "bad fact" in str(excinfo.value)


def test_load_one_missing_id(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.load_one(generate_ulid())


def test_invalid_scope_rejected_at_write(store: Store) -> None:
    with pytest.raises(ValidationError):
        # Capital letters not allowed.
        store.write(content="x", scopes=["Tools"])
    with pytest.raises(ValidationError):
        # Whitespace not allowed.
        store.write(content="x", scopes=["my tools"])


def test_empty_scopes_rejected(store: Store) -> None:
    with pytest.raises(ValidationError):
        store.write(content="x", scopes=[])


def test_filename_collision_doesnt_clobber(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force two writes to the same date+slug.
    fixed = datetime(2025, 3, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("bettermemory.store.utcnow", lambda: fixed)
    monkeypatch.setattr("bettermemory.models.utcnow", lambda: fixed)

    a = store.write(content="hello world", scopes=["tools"])
    b = store.write(content="hello world", scopes=["tools"])
    assert a.id != b.id

    files = sorted(p.name for p in store.root.iterdir() if p.suffix == ".md")
    assert len(files) == 2
    # Both memories survive.
    assert {store.load_one(a.id).id, store.load_one(b.id).id} == {a.id, b.id}


def test_list_summaries_filters_by_scope(store: Store) -> None:
    store.write(content="python notes", scopes=["learning-style"])
    store.write(content="home lab notes", scopes=["infrastructure"])

    only_infra = store.list_summaries(scopes=["infrastructure"])
    assert len(only_infra) == 1
    assert only_infra[0].scopes == ["infrastructure"]


def test_list_summaries_strips_body(store: Store) -> None:
    store.write(
        content="One sentence. A second sentence that should be invisible.",
        scopes=["tools"],
    )
    summaries = store.list_summaries()
    assert len(summaries) == 1
    # Summary is the first sentence (or first 80 chars).
    assert "second sentence" not in summaries[0].summary


def test_list_summaries_carries_updated_timestamp(store: Store) -> None:
    """The `updated` field is durable on disk and threaded through to the
    summary so callers can spot stale memories at a glance."""
    memory = store.write(content="x", scopes=["tools"])
    summaries = store.list_summaries()
    assert len(summaries) == 1
    assert summaries[0].updated == memory.updated
    # On a fresh write, created and updated agree.
    assert summaries[0].updated == summaries[0].created


def test_summary_does_not_split_inside_dotted_identifier(store: Store) -> None:
    """The previous implementation split on bare `.`, so a body like
    `gh auth login does NOT write git config --global user.name` produced
    a summary truncated to `...write git config --global user`. Sentence
    boundary is `.!?` followed by whitespace or end-of-string.
    """
    store.write(
        content=(
            "`gh auth login` does NOT write `git config --global user.name`. "
            "It only sets up GitHub credentials."
        ),
        scopes=["tools"],
    )
    summary = store.list_summaries()[0].summary
    assert "user.name" in summary  # didn't split inside the identifier
    assert "It only sets up" not in summary  # did stop at the real boundary


def test_summary_uses_first_real_sentence_when_short_enough(store: Store) -> None:
    store.write(
        content="Short topic sentence. Then more detail follows on a new line.",
        scopes=["tools"],
    )
    summary = store.list_summaries()[0].summary
    assert summary == "Short topic sentence"


def test_summary_walks_past_eg_abbreviation(store: Store) -> None:
    """`e.g.` opening a body shouldn't chop the summary to "e.g"."""
    store.write(
        content=("e.g. always lower-case scope names. Otherwise validation fails."),
        scopes=["tools"],
    )
    summary = store.list_summaries()[0].summary
    assert "lower-case scope names" in summary
    assert "Otherwise" not in summary  # stopped at the real boundary


def test_summary_walks_past_ie_abbreviation(store: Store) -> None:
    store.write(
        content=(
            "i.e. one memory per fact. Combining unrelated bullets defeats the search."
        ),
        scopes=["tools"],
    )
    summary = store.list_summaries()[0].summary
    assert "one memory per fact" in summary
    assert "Combining" not in summary


def test_summary_walks_past_mr_abbreviation(store: Store) -> None:
    """Title abbreviations like `Mr.` are case-folded for the lookup."""
    store.write(
        content="Mr. Smith owns the deploy box. Ping him before pushing.",
        scopes=["infrastructure"],
    )
    summary = store.list_summaries()[0].summary
    assert "Mr. Smith" in summary
    assert "Ping" not in summary


def test_summary_walks_past_us_abbreviation(store: Store) -> None:
    """Multi-dot abbreviations (`U.S.`) — the lookup token is `u.s`."""
    store.write(
        content=(
            "U.S. infrastructure pricing differs from EU. "
            "See the spreadsheet for the breakdown."
        ),
        scopes=["infrastructure"],
    )
    summary = store.list_summaries()[0].summary
    assert "U.S. infrastructure" in summary
    assert "See the spreadsheet" not in summary


def test_summary_still_breaks_on_genuine_sentence_after_abbreviation(
    store: Store,
) -> None:
    """The skip is per-match, not global — once we walk past `e.g.` we still
    pick up the next real boundary."""
    store.write(
        content=(
            "Use lower-case scope names, e.g. tools or learning-style. "
            "Allowed list rejects everything else."
        ),
        scopes=["tools"],
    )
    summary = store.list_summaries()[0].summary
    assert summary.endswith("learning-style")
    assert "Allowed list" not in summary


def test_round_trip_through_disk(memory_dir: Path) -> None:
    """A second Store on the same directory sees the same memories."""
    s1 = Store(memory_dir)
    a = s1.write(content="persistent", scopes=["tools"])

    s2 = Store(memory_dir)
    assert s2.load_one(a.id).body.strip() == "persistent"


# ---------------------------------------------------------------------------
# last_verified_at — orthogonal verification timestamp
# ---------------------------------------------------------------------------


def test_fresh_write_has_null_last_verified_at(store: Store) -> None:
    """A new memory hasn't been spot-checked yet — null, not the
    write timestamp. Conflating those would make every memory appear
    "verified" the moment it's written, which defeats the signal."""
    memory = store.write(content="x", scopes=["tools"])
    assert memory.last_verified_at is None
    assert store.load_one(memory.id).last_verified_at is None


def test_mark_verified_sets_timestamp(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    verified = store.mark_verified(memory.id)
    assert verified.last_verified_at is not None
    assert verified.last_verified_at.tzinfo is not None  # aware


def test_mark_verified_does_not_bump_updated(store: Store) -> None:
    """Verification is the orthogonal axis to content edits. A spot-check
    that confirms reality matched the body shouldn't make the body look
    edited — `updated` should be unchanged."""
    memory = store.write(content="x", scopes=["tools"])
    time.sleep(0.01)
    verified = store.mark_verified(memory.id)
    assert verified.updated == memory.updated
    assert verified.created == memory.created


def test_mark_verified_persists_to_disk(memory_dir: Path) -> None:
    s1 = Store(memory_dir)
    memory = s1.write(content="x", scopes=["tools"])
    s1.mark_verified(memory.id)

    # Fresh store reads the same data.
    s2 = Store(memory_dir)
    reloaded = s2.load_one(memory.id)
    assert reloaded.last_verified_at is not None


def test_mark_verified_idempotent_slides_timestamp_forward(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    first = store.mark_verified(memory.id)
    time.sleep(0.01)
    second = store.mark_verified(memory.id)
    assert second.last_verified_at is not None
    assert first.last_verified_at is not None
    assert second.last_verified_at >= first.last_verified_at


def test_mark_verified_missing_id_raises(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.mark_verified(generate_ulid())


def test_mark_verified_tombstoned_raises(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="bad")
    with pytest.raises(TombstonedError):
        store.mark_verified(memory.id)


def test_store_update_preserves_last_verified_at(store: Store) -> None:
    """The Store layer is content-blind: it preserves whatever
    last_verified_at the caller passed in. The semantic decision to reset
    on content edits lives in the server tool layer (memory_update); the
    store just persists what it's handed."""
    memory = store.write(content="x", scopes=["tools"])
    verified = store.mark_verified(memory.id)
    edited = verified.model_copy(update={"body": "edited\n"})
    saved = store.update(edited)
    assert saved.last_verified_at == verified.last_verified_at


def test_list_summaries_carries_last_verified_at(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.mark_verified(memory.id)
    summaries = store.list_summaries()
    assert len(summaries) == 1
    assert summaries[0].last_verified_at is not None


def test_legacy_memory_without_last_verified_at_field_loads(memory_dir: Path) -> None:
    """A frontmatter file written by an older bettermemory has no
    `last_verified_at` key. Loading must not raise — the field is
    additive, missing means "never verified"."""
    legacy = memory_dir / "2025-01-01-legacy.md"
    legacy.write_text(
        "---\n"
        f"id: {generate_ulid()}\n"
        "created: 2025-01-01T00:00:00Z\n"
        "updated: 2025-01-01T00:00:00Z\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "legacy body\n"
    )
    store = Store(memory_dir)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].last_verified_at is None


def test_malformed_last_verified_at_silently_falls_back_to_none(
    memory_dir: Path,
) -> None:
    """A typo'd timestamp in frontmatter shouldn't render the whole memory
    unloadable. Treat malformed the same as missing — the rest of the
    memory is still useful, and the next memory_verify call will write a
    valid timestamp."""
    legacy = memory_dir / "2025-01-01-malformed.md"
    legacy.write_text(
        "---\n"
        f"id: {generate_ulid()}\n"
        "created: 2025-01-01T00:00:00Z\n"
        "updated: 2025-01-01T00:00:00Z\n"
        "last_verified_at: not-a-date\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "body\n"
    )
    store = Store(memory_dir)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].last_verified_at is None


def test_quoted_naive_iso_string_loads_as_utc_aware(
    memory_dir: Path,
) -> None:
    """A hand-edited frontmatter with `last_verified_at` written as a
    quoted ISO string with no offset (e.g. `"2025-01-01T10:00:00"`)
    must load as a UTC-aware datetime — not naive. The audit caught
    that the str-branch of `_as_dt` skipped the tz-coercion step the
    datetime branch already had, so a naive value flowed through and
    crashed `health.compute_health` on the first comparison against
    an aware `now`. Pin the round-trip so both branches stay
    symmetric."""
    legacy = memory_dir / "2025-01-01-naive.md"
    legacy.write_text(
        "---\n"
        f"id: {generate_ulid()}\n"
        "created: 2025-01-01T00:00:00Z\n"
        "updated: 2025-01-01T00:00:00Z\n"
        # Quoted forces YAML to keep it as a string; without quotes
        # PyYAML parses it natively as a (naive) datetime, which the
        # *other* branch of `_as_dt` already coerced. Quoting is the
        # path that was broken.
        '"last_verified_at": "2025-01-01T10:00:00"\n'
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "body\n"
    )
    store = Store(memory_dir)
    loaded = store.load_all()
    assert len(loaded) == 1
    lva = loaded[0].last_verified_at
    assert lva is not None
    assert lva.tzinfo is not None  # would have been None before the fix
    # Comparable against an aware now without TypeError — the actual
    # symptom that surfaced from the broken path.
    _ = lva < datetime.now(timezone.utc)


def test_mark_verified_emits_field_into_frontmatter(memory_dir: Path) -> None:
    """Once verified, the field shows up in the on-disk frontmatter."""
    store = Store(memory_dir)
    memory = store.write(content="x", scopes=["tools"])
    store.mark_verified(memory.id)
    md_files = [p for p in memory_dir.iterdir() if p.suffix == ".md"]
    assert len(md_files) == 1
    text = md_files[0].read_text()
    assert "last_verified_at:" in text


def test_unverified_memory_omits_field_from_frontmatter(memory_dir: Path) -> None:
    """Newly-written memories shouldn't carry a `last_verified_at: null`
    placeholder line — visual noise on every file."""
    store = Store(memory_dir)
    store.write(content="x", scopes=["tools"])
    md_files = [p for p in memory_dir.iterdir() if p.suffix == ".md"]
    assert len(md_files) == 1
    text = md_files[0].read_text()
    assert "last_verified_at" not in text


# ---------------------------------------------------------------------------
# Symlink rejection — sync trust boundary
# ---------------------------------------------------------------------------


def test_iter_active_paths_skips_symlinks(memory_dir: Path, tmp_path: Path) -> None:
    """Regression: `_iter_active_paths` must reject symlinks. With
    `sync pull` shipped, the memory directory is a worktree a remote
    can push to — a `something.md` symlinked to an arbitrary file
    elsewhere on disk would otherwise be loaded and parsed on the
    next `load_all`. The parse would fail (the targeted file isn't
    valid frontmatter) but the contract we want is "memories are
    regular files in this directory, full stop"."""
    import sys

    if sys.platform == "win32":
        pytest.skip("symlink semantics differ on Windows; POSIX-only test")

    store = Store(memory_dir)
    real_memory = store.write(content="real body", scopes=["tools"])

    # Put a target file somewhere outside the memory dir, then symlink
    # an `.md` entry inside the memory dir to it. The store must not
    # surface it.
    target = tmp_path / "secret.txt"
    target.write_text("sensitive content elsewhere on disk")
    rogue_link = memory_dir / "2026-01-01-rogue.md"
    rogue_link.symlink_to(target)

    paths = list(store._iter_active_paths())
    assert len(paths) == 1, (
        f"expected only the real memory file; got {[p.name for p in paths]}"
    )
    assert paths[0].name.endswith(".md")
    assert real_memory.id == store._load_path(paths[0]).id


def test_iter_tombstone_paths_skips_symlinks(memory_dir: Path, tmp_path: Path) -> None:
    """Same protection on the tombstone iterator. The tombstone dir
    is part of the sync-able tree, so the same trust-boundary
    argument applies."""
    import sys

    if sys.platform == "win32":
        pytest.skip("symlink semantics differ on Windows; POSIX-only test")

    store = Store(memory_dir)
    real_memory = store.write(content="real body", scopes=["tools"])
    store.tombstone(real_memory.id, reason="cleanup")

    target = tmp_path / "secret.txt"
    target.write_text("sensitive content elsewhere on disk")
    rogue_link = store.tombstone_dir / "rogue.tombstone.md"
    rogue_link.symlink_to(target)

    paths = list(store._iter_tombstone_paths())
    assert len(paths) == 1
    assert ".tombstone.md" in paths[0].name
    assert "rogue" not in paths[0].name


# ---------------------------------------------------------------------------
# File permissions — 0o600 on memory files (L4)
# ---------------------------------------------------------------------------


def test_memory_file_is_owner_only(memory_dir: Path) -> None:
    """Regression: memory files written by the store must land at
    0o600 (owner read/write only). Inherit-user-umask would put them
    at 0o644 on a default Linux/macOS install — world-readable, which
    contradicts the user's privacy expectation for memory content.
    The lock files already use 0o600 (see `_locked`); this brings the
    data path in line."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission bits don't apply on Windows")

    store = Store(memory_dir)
    memory = store.write(content="private body", scopes=["tools"])
    md_files = [p for p in memory_dir.iterdir() if p.suffix == ".md"]
    assert len(md_files) == 1
    mode = md_files[0].stat().st_mode & 0o777
    assert mode == 0o600, (
        f"memory file mode is {oct(mode)}, expected 0o600 — "
        f"file is readable by group/world"
    )

    # Same check after an `update` (rewrites in-place via _atomic_write_post).
    store.update(memory.model_copy(update={"body": "rewritten\n"}))
    mode = md_files[0].stat().st_mode & 0o777
    assert mode == 0o600, (
        f"memory file mode after update is {oct(mode)}, expected 0o600"
    )


def test_tombstone_file_is_owner_only(memory_dir: Path) -> None:
    """Tombstones land in `.tombstones/` via the same `_atomic_write_post`
    helper. Verify the chmod applies there too — tombstones carry the
    same body content as live memories, plus the removal reason and
    session id, so the privacy bar is the same."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission bits don't apply on Windows")

    store = Store(memory_dir)
    memory = store.write(content="body", scopes=["tools"])
    store.tombstone(memory.id, reason="not needed")
    tombstones = list(store._iter_tombstone_paths())
    assert len(tombstones) == 1
    mode = tombstones[0].stat().st_mode & 0o777
    assert mode == 0o600, f"tombstone mode is {oct(mode)}, expected 0o600"


def test_atomic_write_post_runs_full_durability_ceremony(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's canonical private-write helper `_atomic_write_post`
    delegates to `_fsutil.atomic_write_bytes`, which owns the
    fsync_file → rename → fsync_dir(parent) discipline. Episodes' own
    write path has a dedicated spy test pinning this ceremony
    (`test_episodes.test_write_is_atomic_and_durable`); the store path
    was only pinned transitively through the `_fsutil` primitive tests.
    Pin it directly so a future edit that re-inlined a partial copy of
    the write in `store.py` and dropped the dir-fsync — the exact
    regression the 2.6.x audit cycle kept catching — fails loudly here
    instead of sailing through the 0o600/round-trip checks."""
    fsync_file_calls: list[int] = []
    fsync_dir_calls: list[Path] = []

    from bettermemory import _fsutil

    def spy_fsync_file(fd: int) -> None:
        fsync_file_calls.append(fd)

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(_fsutil, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(_fsutil, "fsync_dir", spy_fsync_dir)

    store = Store(memory_dir)
    store.write(content="durable body", scopes=["tools"])

    md_files = [p for p in memory_dir.iterdir() if p.suffix == ".md"]
    assert len(md_files) == 1
    target = md_files[0]

    # No `.tmp` artifacts left behind after a successful write.
    stragglers = [p.name for p in memory_dir.iterdir() if ".tmp" in p.name]
    assert stragglers == [], f"unexpected tmp artifacts: {stragglers}"

    # fsync_file fired on the write's tmp fd before the rename.
    assert len(fsync_file_calls) >= 1, (
        "store write did not fsync the file before rename — a crash between "
        "the rename and the kernel flush could leave a zero-byte memory file"
    )
    # fsync_dir fired on the memory file's parent so the new dirent is durable.
    assert target.parent in fsync_dir_calls, (
        f"expected fsync_dir({target.parent!r}) on the store write path; got "
        f"{fsync_dir_calls!r}. Without it the rename is not durable past a crash."
    )


# ---------------------------------------------------------------------------
# H13 — `Store.show` alias for MCP API symmetry
#
# The MCP surface exposes the read-one operation as `memory_show`. The
# Python `Store` API names it `load_one`, which is a discoverability
# foot-gun for anyone adopting the programmatic client — they read the
# tool-name docs and try `store.show(id)` first. The `show` alias
# (with `load_one` retained as the canonical name) closes that gap.
# Round 2 landed the alias; this test pins the behavior.
# ---------------------------------------------------------------------------


def test_store_show_is_an_alias_for_load_one(store: Store) -> None:
    """`Store.show(id)` exists and returns the same Memory as
    `Store.load_one(id)`. Pins the MCP-name / Python-name parity."""
    memory = store.write(content="hello show", scopes=["tools"])
    # Attribute presence and call equivalence — both must hold.
    assert hasattr(Store, "show"), (
        "Store.show is missing — MCP surface exposes `memory_show` but "
        "the Python API has no matching attribute. Adopters trying the "
        "programmatic client get an AttributeError instead of the same "
        "read-one shape they saw in the tool docs."
    )
    via_show = store.show(memory.id)
    via_load = store.load_one(memory.id)
    assert via_show == via_load
    assert via_show.id == memory.id
    assert via_show.body == via_load.body


# ---------------------------------------------------------------------------
# Tombstone-fallback robustness — corrupt entries must not crash readers.
#
# `load_one`, `mark_verified`, and `tombstone` each fall back to iterating
# `.tombstones/` when the id isn't found in the active set. The iteration
# used to call `frontmatter.load(path)` without try/except: one corrupt
# tombstone (sync-pull truncation, hand-edit typo, partial-write recovery)
# would crash the whole tool — `memory_show`, `memory_record_use`, and
# the hook's attribution loop. The fix mirrors `load_tombstones`'s
# defensive catch tuple. These tests exercise each callsite by dropping
# a junk tombstone into `.tombstones/` and asserting the callsite still
# resolves cleanly.
# ---------------------------------------------------------------------------


def _drop_corrupt_tombstone(store: Store) -> Path:
    """Place a corrupt-YAML tombstone in `.tombstones/` and return the path.

    The content is malformed YAML (`{unterminated`) inside an otherwise
    well-formed frontmatter wrapper, so the file passes the iteration
    filter (regular `.md`, not a symlink) but blows up on parse — the
    exact shape a torn sync-pull or hand-edit typo produces.
    """
    store.tombstone_dir.mkdir(mode=0o700, exist_ok=True)
    corrupt = store.tombstone_dir / "00000000-corrupt.tombstone.md"
    corrupt.write_text(
        "---\nid: x\nbroken: {unterminated\n---\n\nbody\n",
        encoding="utf-8",
    )
    return corrupt


def test_load_one_skips_corrupt_tombstone_during_fallback(store: Store) -> None:
    """`load_one` falls back to iterating tombstones when the id isn't
    active. A corrupt tombstone in that iteration must not crash —
    `memory_show` of a never-existing id should still raise the
    expected `MemoryNotFoundError`, not propagate a parse error.
    """
    _drop_corrupt_tombstone(store)
    # The corrupt tombstone exists; querying a fresh id should fall
    # through cleanly to MemoryNotFoundError without the parser
    # blowing up on the way.
    with pytest.raises(MemoryNotFoundError):
        store.load_one(generate_ulid())


def test_load_one_finds_tombstone_alongside_corrupt_entry(store: Store) -> None:
    """The corrupt-tombstone skip path must not eat *legitimate*
    tombstones. Tombstone a real memory, drop a corrupt entry beside
    it, and verify `load_one` still surfaces the `TombstonedError`
    with the original removal reason — the skip is per-file, not per-
    iteration."""
    memory = store.write(content="real", scopes=["tools"])
    store.tombstone(memory.id, reason="real-reason")
    _drop_corrupt_tombstone(store)

    with pytest.raises(TombstonedError) as excinfo:
        store.load_one(memory.id)
    assert "real-reason" in str(excinfo.value)


def test_mark_verified_skips_corrupt_tombstone_during_fallback(store: Store) -> None:
    """`mark_verified` falls back to the tombstone iteration when the
    id isn't active. A corrupt tombstone must not crash the callsite —
    a clean miss should still raise `MemoryNotFoundError`."""
    _drop_corrupt_tombstone(store)
    with pytest.raises(MemoryNotFoundError):
        store.mark_verified(generate_ulid())


def test_mark_verified_tombstoned_alongside_corrupt_entry(store: Store) -> None:
    """When the id is actually tombstoned, a corrupt sibling must not
    prevent `mark_verified` from raising `TombstonedError` with the
    proper removal context."""
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="superseded")
    _drop_corrupt_tombstone(store)

    with pytest.raises(TombstonedError) as excinfo:
        store.mark_verified(memory.id)
    assert "superseded" in str(excinfo.value)


def test_tombstone_skips_corrupt_tombstone_during_double_tombstone_check(
    store: Store,
) -> None:
    """`tombstone(id)` falls back to the tombstone iteration when the
    id isn't active, to give a clearer "already tombstoned" error. A
    corrupt tombstone in that iteration must not crash the call —
    a clean miss for an unknown id should still raise
    `MemoryNotFoundError`."""
    _drop_corrupt_tombstone(store)
    with pytest.raises(MemoryNotFoundError):
        store.tombstone(generate_ulid(), reason="never existed")


def test_tombstone_already_tombstoned_alongside_corrupt_entry(store: Store) -> None:
    """When the id is already tombstoned, a corrupt sibling must not
    prevent `tombstone` from surfacing the clear
    "already tombstoned" `TombstonedError`."""
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="first removal")
    _drop_corrupt_tombstone(store)

    with pytest.raises(TombstonedError, match="already tombstoned"):
        store.tombstone(memory.id, reason="second attempt")


# ---------------------------------------------------------------------------
# Bare-date frontmatter coercion (_as_dt) — silent-data-loss regression
#
# A hand-edited or hand-migrated frontmatter with a DATE-ONLY `created`
# (`created: 2025-01-01`, unquoted, no time component) is parsed by PyYAML
# as a `datetime.date` — which is neither a `datetime` nor a `str`. Before
# the fix, `_as_dt` fell through to `raise ValueError`, which `_load_path`'s
# caller (`load_all` / `load_one`) catches and SKIPS — the whole memory
# vanished from every read surface with no warning. The fix adds a
# `datetime.date` branch coercing to midnight UTC.
# ---------------------------------------------------------------------------


def test_bare_date_created_loads_not_skipped(memory_dir: Path) -> None:
    """A memory whose `created`/`updated` are bare YAML dates (no time)
    must LOAD, not be silently dropped. PyYAML turns `created: 2025-01-01`
    into a `datetime.date`; `_as_dt` must coerce it rather than raise (which
    upstream would swallow as a skip = silent data loss)."""
    bare = memory_dir / "2025-01-01-bare-date.md"
    bare.write_text(
        "---\n"
        f"id: {generate_ulid()}\n"
        # Unquoted, date-only — PyYAML parses these as datetime.date,
        # NOT datetime and NOT str. This is the path that used to drop
        # the memory.
        "created: 2025-01-01\n"
        "updated: 2025-01-02\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "bare date body\n"
    )
    store = Store(memory_dir)
    loaded = store.load_all()
    assert len(loaded) == 1, (
        "memory with a bare-date `created` was silently skipped — _as_dt "
        "raised ValueError on the datetime.date and load_all swallowed it"
    )
    mem = loaded[0]
    # Coerced to tz-aware UTC midnight on both axes.
    assert mem.created == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert mem.updated == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert mem.created.tzinfo is not None
    # Comparable against an aware now without TypeError.
    _ = mem.created < datetime.now(timezone.utc)


def test_bare_date_memory_is_loadable_by_id(memory_dir: Path) -> None:
    """The bare-date memory must also resolve through `load_one` (the
    single-id read path uses the same `_as_dt` coercion in `_load_path`),
    not just the bulk `load_all`."""
    mid = generate_ulid()
    bare = memory_dir / "2025-03-14-bare-date-byid.md"
    bare.write_text(
        "---\n"
        f"id: {mid}\n"
        "created: 2025-03-14\n"
        "updated: 2025-03-14\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "body\n"
    )
    store = Store(memory_dir)
    loaded = store.load_one(mid)
    assert loaded.id == mid
    assert loaded.created == datetime(2025, 3, 14, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# prune_tombstones sidecar cleanup — unbounded `.lock` leak regression
#
# `Store._locked` is `_fsutil.flock_excl`, which creates a sibling
# `<path>.lock` sidecar and DELIBERATELY never unlinks it on release
# (per-inode flock identity). `prune_tombstones` acquires that lock on the
# tombstone path, then hard-deletes the tombstone `.md` — but used to leave
# the `<name>.tombstone.md.lock` sidecar behind, accumulating one orphan
# per pruned tombstone forever. The fix unlinks the sidecar after the
# tombstone, mirroring `episodes._unlink_session_lockfile`.
# ---------------------------------------------------------------------------


def test_prune_tombstones_removes_lock_sidecar(store: Store) -> None:
    """Pruning a tombstone must also remove the `.lock` sidecar that the
    per-file flock created next to it, or the lockfile leaks unbounded."""
    memory = store.write(content="to be pruned", scopes=["tools"])
    tombstone_path = store.tombstone(memory.id, reason="cleanup")
    sidecar = tombstone_path.with_suffix(tombstone_path.suffix + ".lock")

    # The flock acquisition during `tombstone()`'s prune-side lock (and any
    # subsequent `_locked` on the tombstone) materialises the sidecar.
    # Force it into existence deterministically by acquiring the lock once,
    # so the test pins cleanup rather than lock-creation timing.
    from bettermemory.store import _locked

    with _locked(tombstone_path):
        pass
    assert sidecar.exists(), (
        "precondition: the per-file flock should have created a .lock "
        "sidecar next to the tombstone"
    )

    # Cutoff in the future (negative window) so the tombstone is pruned.
    pruned = store.prune_tombstones(timedelta(seconds=-1))
    assert memory.id in pruned
    assert not tombstone_path.exists(), "tombstone .md should be deleted"
    assert not sidecar.exists(), (
        "prune_tombstones left the .lock sidecar behind — flock_excl never "
        "unlinks it on release, so it leaks one orphan per pruned tombstone"
    )


# ---------------------------------------------------------------------------
# verified_absent_paths — the absent-attestation mirror axis (3.8.x)
# ---------------------------------------------------------------------------


def test_mark_verified_persists_absent_paths(memory_dir: Path) -> None:
    s1 = Store(memory_dir)
    memory = s1.write(
        content="stacks live in /data/compose on the zimaboard\n",
        scopes=["tools"],
    )
    s1.mark_verified(memory.id, verified_absent_paths=["/data/compose"])

    s2 = Store(memory_dir)
    reloaded = s2.load_one(memory.id)
    assert reloaded.verified_absent_paths == ["/data/compose"]


def test_mark_verified_none_preserves_absent_paths(store: Store) -> None:
    memory = store.write(content="x at /data/compose\n", scopes=["tools"])
    store.mark_verified(memory.id, verified_absent_paths=["/data/compose"])
    again = store.mark_verified(memory.id)
    assert again.verified_absent_paths == ["/data/compose"]


def test_mark_verified_empty_list_clears_absent_paths(store: Store) -> None:
    memory = store.write(content="x at /data/compose\n", scopes=["tools"])
    store.mark_verified(memory.id, verified_absent_paths=["/data/compose"])
    cleared = store.mark_verified(memory.id, verified_absent_paths=[])
    assert cleared.verified_absent_paths == []


def test_absent_paths_survive_tombstone_and_restore(store: Store) -> None:
    memory = store.write(content="x at /data/compose\n", scopes=["tools"])
    store.mark_verified(memory.id, verified_absent_paths=["/data/compose"])
    store.tombstone(memory.id, "test removal")
    restored = store.restore(memory.id)
    assert restored.verified_absent_paths == ["/data/compose"]


def test_update_preserve_verification_keeps_absent_paths(store: Store) -> None:
    """Scope-only updates run the preserve_verification branch — the
    absent attestation must ride along with the other verified_* lists."""
    memory = store.write(content="x at /data/compose\n", scopes=["tools"])
    store.mark_verified(memory.id, verified_absent_paths=["/data/compose"])
    snapshot = store.load_one(memory.id)
    edited = snapshot.model_copy(update={"scopes": ["tools", "infrastructure"]})
    updated = store.update(edited, preserve_verification=True)
    assert updated.verified_absent_paths == ["/data/compose"]


def test_adversarial_scalar_scopes_file_never_bricks_store_construction(
    tmp_path: Path,
) -> None:
    """`scopes: 5` is a well-formed YAML mapping, so the frontmatter
    boundary accepts it — the parse then dies at `list(meta["scopes"])`
    with TypeError, OUTSIDE the (ValueError, KeyError, OSError) tuple
    the read surfaces catch. Pre-fix, the parse-aware divergence walk
    in `Store.__post_init__` (`_warn_on_index_divergence` →
    `count_unparseable_memory_files`) let that TypeError escape, so one
    weird file bricked every Store construction: server boot and every
    CLI command. The contract is "any parse failure == unparseable
    file, never a construction crash" — and `iter_active` must skip
    exactly the file the counter counted."""
    root = tmp_path / "adversarial"
    root.mkdir()
    (root / "2026-01-01-scalar-scopes.md").write_text(
        "---\n"
        "schema_version: 1\n"
        "id: 01HXYZAAAAAAAAAAAAAAAAAAAA\n"
        "created: 2026-01-01T00:00:00Z\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "scopes: 5\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n\nvalid YAML, wrong shape\n",
        encoding="utf-8",
    )

    # The counter classifies the parse failure instead of crashing...
    assert count_unparseable_memory_files(root) == 1
    # ...construction survives the walk (disk=1 vs indexed=0 diverges,
    # so the parse-aware refinement runs right here, in __post_init__)...
    store = Store(root)
    # ...and the rebuild feed skips the same file the counter counted.
    assert list(store.iter_active()) == []


def test_unparseable_counter_and_iter_active_agree_on_any_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip/count width is `Exception`, not an enumerated tuple:
    `_parse_memory_file` delegates to pydantic and enum internals whose
    raise surface can't be enumerated durably, and the counter runs at
    every Store construction — a new exception type must degrade to
    "one more unparseable file", not a boot crash. Force a type no real
    file produces today and assert both surfaces treat the file as
    unparseable rather than propagating."""
    from bettermemory import store as store_mod

    store = Store(tmp_path / "contract")
    store.write(content="parses fine before the patch\n", scopes=["tools"])

    def _boom(path: Path) -> NoReturn:
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(store_mod, "_parse_memory_file", _boom)
    assert count_unparseable_memory_files(store.root) == 1
    assert list(store.iter_active()) == []


def _write_scalar_scopes_file(root: Path, *, memory_id: str, filename: str) -> None:
    """Well-formed YAML whose `scopes: 5` dies at `list(meta["scopes"])`
    with TypeError — outside the historic (ValueError, KeyError, OSError)
    catch tuples. The same fixture the construction-crash test above
    uses, parameterized so it can sit next to healthy memories."""
    (root / filename).write_text(
        "---\n"
        "schema_version: 1\n"
        f"id: {memory_id}\n"
        "created: 2026-01-01T00:00:00Z\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "scopes: 5\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n\nvalid YAML, wrong shape\n",
        encoding="utf-8",
    )


def test_adversarial_scalar_scopes_file_skipped_by_every_read_surface(
    tmp_path: Path,
) -> None:
    """The construction fix taught the counter and `iter_active` to
    survive the scalar-scopes TypeError, but `load_all`, `load_one`,
    and `rename_scope` kept narrower tuples — so the same file that no
    longer bricked Store() still crashed memory_search (`load_all` is
    its candidate source), memory_list, and memory_health one layer up.
    All per-file parse catches now share `PARSE_SKIP_EXCEPTIONS`: the
    file the counter counts is the file every reader skips."""
    root = tmp_path / "adversarial"
    root.mkdir()
    store = Store(root)
    good = store.write(content="survives the bad neighbor\n", scopes=["tools"])
    _write_scalar_scopes_file(
        root,
        memory_id="01HXYZAAAAAAAAAAAAAAAAAAAA",
        filename="2026-01-01-scalar-scopes.md",
    )

    assert count_unparseable_memory_files(root) == 1
    # The bulk readers skip the file the counter counted (TypeError
    # pre-fix in load_all).
    assert [m.id for m in store.load_all()] == [good.id]
    assert [m.id for _, m in store.iter_active()] == [good.id]
    # The id walks survive it too: a hit on a healthy id, and a miss
    # that walks past the bad file into MemoryNotFoundError — the miss
    # is guaranteed to visit every file, so it crashed pre-fix
    # regardless of directory iteration order.
    assert store.load_one(good.id).id == good.id
    with pytest.raises(MemoryNotFoundError):
        store.load_one(generate_ulid())
    # rename_scope's active walk skips it rather than dying mid-walk.
    renamed = store.rename_scope("tools", "infrastructure")
    assert renamed["active"] == [good.id]


async def _search_ids(memory_dir: Path, query: str) -> list[str]:
    """Run memory_search end-to-end through the MCP tool surface (the
    same build_server + call_tool shape test_server.py uses) and return
    the hit ids. Local imports keep this module's header pure-store."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState

    server: Any = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=Store(memory_dir),
        state=SessionState(),
    )
    _content, structured = await server.call_tool("memory_search", {"query": query})
    hits = (
        structured.get("result", structured)
        if isinstance(structured, dict)
        else structured
    )
    return [h["id"] for h in hits]


async def test_memory_search_survives_adversarial_file_on_load_all_path(
    memory_dir: Path, store: Store
) -> None:
    """E2e for the crash that outlived the construction fix: a small
    store routes `memory_search` through `load_all`, whose per-file
    catch didn't cover the scalar-scopes TypeError — one hand-written
    file next to a healthy store crashed the whole tool call pre-fix."""
    good = store.write(
        content="The home lab is on subnet 10.42.\n", scopes=["infrastructure"]
    )
    _write_scalar_scopes_file(
        memory_dir,
        memory_id="01HXYZAAAAAAAAAAAAAAAAAAAA",
        filename="2026-01-01-scalar-scopes.md",
    )

    assert await _search_ids(memory_dir, "home lab subnet") == [good.id]


async def test_memory_search_survives_file_gone_adversarial_after_indexing(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lazy-load twin: a memory hand-edited into the scalar-scopes
    shape AFTER it was indexed. The FTS prefilter still returns its id
    and filename, so the per-candidate `_load_path` in
    `_handlers._load_search_candidates` hit the same TypeError pre-fix
    — the crash just moved from the full scan to the indexed path."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    good = store.write(
        content="alpha survives the corrupted sibling\n", scopes=["tools"]
    )
    bad = store.write(content="alpha goes bad after indexing\n", scopes=["tools"])
    # Rewrite bad's file in place, keeping id and filename, so the index
    # row (body text + filename column) still resolves to it.
    bad_path = next(p for p, m in store.iter_active() if m.id == bad.id)
    _write_scalar_scopes_file(memory_dir, memory_id=bad.id, filename=bad_path.name)

    assert await _search_ids(memory_dir, "alpha") == [good.id]


# ---------------------------------------------------------------------------
# Construction-time auto-rebuild failure backoff
# ---------------------------------------------------------------------------


def _flag_index_rebuild_pending(root: Path) -> None:
    """Set `meta.needs_rebuild='1'` directly on the index sidecar — the
    state a schema migration leaves behind for the construction-time
    auto-rebuild to pick up."""
    import sqlite3

    from bettermemory import index as _index

    conn = sqlite3.connect(str(_index.index_path(root)))
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('needs_rebuild', '1')"
            )
    finally:
        conn.close()


def _set_failure_marker(root: Path, value: str) -> None:
    import sqlite3

    from bettermemory import index as _index

    conn = sqlite3.connect(str(_index.index_path(root)))
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) "
                "VALUES ('last_rebuild_failure', ?)",
                (value,),
            )
    finally:
        conn.close()


@pytest.fixture()
def fresh_backoff_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level backoff memo/warn state per test."""
    from bettermemory import store as store_mod

    monkeypatch.setattr(store_mod, "_REBUILD_FAILURE_MEMO", {})
    monkeypatch.setattr(store_mod, "_REBUILD_SKIP_WARNED", set())


def _count_rebuild_attempts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace `index.rebuild` with a counting stub that always fails."""
    from bettermemory import index as _index

    attempts: list[int] = []

    def _boom(root: Path, items: Any) -> NoReturn:
        attempts.append(1)
        raise sqlite3_like_error("simulated persistent rebuild failure")

    monkeypatch.setattr(_index, "rebuild", _boom)
    return attempts


def sqlite3_like_error(msg: str) -> Exception:
    import sqlite3

    return sqlite3.OperationalError(msg)


def test_failing_auto_rebuild_attempted_once_not_per_construction(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    fresh_backoff_state: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The in-process memo suppresses re-attempts within the backoff
    window: three constructions pay ONE full-store rebuild attempt, and
    the skip notice logs once per process."""
    store.write(content="alpha memory", scopes=["tools"])
    store.write(content="beta memory", scopes=["tools"])
    _flag_index_rebuild_pending(store.root)
    attempts = _count_rebuild_attempts(monkeypatch)

    with caplog.at_level("INFO", logger="bettermemory.store"):
        for _ in range(3):
            Store(store.root)

    assert len(attempts) == 1
    skip_notices = [r for r in caplog.records if "skipping the retry" in r.getMessage()]
    assert len(skip_notices) == 1


def test_failure_marker_suppresses_fresh_process(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    fresh_backoff_state: None,
) -> None:
    """The best-effort meta marker carries the backoff across process
    boundaries: after a failed attempt, a simulated fresh process (empty
    in-process memo) still skips the retry."""
    from bettermemory import index as _index
    from bettermemory import store as store_mod

    store.write(content="alpha memory", scopes=["tools"])
    _flag_index_rebuild_pending(store.root)
    attempts = _count_rebuild_attempts(monkeypatch)

    Store(store.root)
    assert len(attempts) == 1
    marker = _index.status(store.root).get("last_rebuild_failure")
    assert isinstance(marker, float)

    # Fresh process: the module-level memo dies with the process.
    monkeypatch.setattr(store_mod, "_REBUILD_FAILURE_MEMO", {})
    monkeypatch.setattr(store_mod, "_REBUILD_SKIP_WARNED", set())
    Store(store.root)
    assert len(attempts) == 1  # marker suppressed the second attempt


def test_expired_marker_allows_retry_and_success_clears_backoff(
    store: Store,
    fresh_backoff_state: None,
) -> None:
    """A marker older than the backoff window does not suppress, the
    real rebuild runs, and success clears BOTH the flag and the marker
    transactionally."""
    from bettermemory import index as _index

    store.write(content="alpha memory", scopes=["tools"])
    store.write(content="beta memory", scopes=["tools"])
    _flag_index_rebuild_pending(store.root)
    _set_failure_marker(store.root, str(time.time() - 7200))

    Store(store.root)

    st = _index.status(store.root)
    assert st.get("needs_rebuild") is False
    assert st.get("last_rebuild_failure") is None
    assert st.get("indexed_count") == 2


def test_garbage_failure_marker_is_no_marker_not_corruption(
    store: Store,
    fresh_backoff_state: None,
) -> None:
    """An unparseable marker value degrades to 'no marker' — it neither
    taints status() as corrupt nor suppresses the retry."""
    from bettermemory import index as _index

    store.write(content="alpha memory", scopes=["tools"])
    _flag_index_rebuild_pending(store.root)
    _set_failure_marker(store.root, "banana")

    st = _index.status(store.root)
    assert "corrupt" not in st
    assert st.get("last_rebuild_failure") is None

    Store(store.root)  # retry not suppressed; real rebuild heals
    st_after = _index.status(store.root)
    assert st_after.get("needs_rebuild") is False


# ---------------------------------------------------------------------------
# Tombstone twin of the scalar-scopes hardening: a tombstone whose
# frontmatter carries `scopes: 5` raises TypeError at the `list(...)`
# coercion inside `_load_tombstone_path` — every read surface must skip
# it, never crash.
# ---------------------------------------------------------------------------


def _drop_scalar_scopes_tombstone(store: Store) -> Path:
    """A VALID-YAML tombstone whose `scopes` is a scalar — parses fine,
    then blows up the `list(meta["scopes"])` coercion with TypeError,
    the shape the memory-side hardening already covers."""
    store.tombstone_dir.mkdir(mode=0o700, exist_ok=True)
    bad = store.tombstone_dir / "00000000-scalar-scopes.tombstone.md"
    bad.write_text(
        "---\n"
        "id: 01JZZZZZZZZZZZZZZZZZZZZZZZ\n"
        "created: 2026-01-01T00:00:00+00:00\n"
        "updated: 2026-01-01T00:00:00+00:00\n"
        "removed: 2026-01-02T00:00:00+00:00\n"
        "scopes: 5\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n\nadversarial tombstone body\n",
        encoding="utf-8",
    )
    return bad


def test_scalar_scopes_tombstone_skipped_by_every_read_surface(
    store: Store,
) -> None:
    good = store.write(content="keep me", scopes=["tools"])
    store.tombstone(good.id, reason="cleanup")
    _drop_scalar_scopes_tombstone(store)

    loaded = store.load_tombstones()
    assert [t.id for t in loaded] == [good.id]

    listed = store.list_tombstones()
    assert [t.id for t in listed] == [good.id]

    fetched = store.load_tombstone(good.id)
    assert fetched.id == good.id

    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone(generate_ulid())


def test_scalar_scopes_tombstone_survives_prune(store: Store) -> None:
    good = store.write(content="keep me too", scopes=["tools"])
    store.tombstone(good.id, reason="cleanup")
    bad_path = _drop_scalar_scopes_tombstone(store)

    pruned = store.prune_tombstones(timedelta(days=0))
    assert isinstance(pruned, list)
    # The adversarial file is skipped, not deleted and not fatal.
    assert bad_path.exists()


def test_path_for_full_ulid_suffix_avoids_slug_collision(store: Store) -> None:
    """F5: two memories whose bodies slugify identically (non-ASCII
    bodies both collapse to the `memory` fallback) written on the same
    day must land on DISTINCT paths. Pre-fix the suffix was only
    `id[-6:]` (30 bits), so two ids sharing their last 6 chars produced
    the same `<date>-memory-<tail>.md` and one memory silently clobbered
    the other. Full-ULID suffixing makes the whole id the entropy.
    """
    created = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
    # Two valid ULIDs sharing their last 6 chars but differing earlier.
    # (Position -6 must be equal: both end in `0WEVGZ`.)
    id_a = "01BX5ZZKBKAAAAAAAAAA0WEVGZ"
    id_b = "01BX5ZZKBKBBBBBBBBBB0WEVGZ"
    assert is_valid_ulid(id_a) and is_valid_ulid(id_b)
    assert id_a[-6:] == id_b[-6:]  # the truncated suffix collides
    assert id_a != id_b

    def _mk(mid: str, body: str) -> Memory:
        return Memory(
            id=mid,
            created=created,
            updated=created,
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=body,
        )

    # Non-ASCII bodies both slugify to the bare `memory` fallback, so
    # the slug contributes zero entropy — the suffix is all there is.
    mem_a = _mk(id_a, "日本語のメモ\n")
    mem_b = _mk(id_b, "中文的内容\n")

    path_a = store._path_for(mem_a)
    path_b = store._path_for(mem_b)
    # Same date prefix and same `memory` slug — the fix's full-id
    # suffix is the only thing keeping the two names apart.
    assert path_a != path_b, (
        f"path collision: both memories map to {path_a.name!r} — "
        f"the filename suffix carries too little of the ULID"
    )

    # And both are actually retrievable after writing through the store.
    store._write_path(path_a, mem_a)
    store._write_path(path_b, mem_b)
    assert store.load_one(id_a).id == id_a
    assert store.load_one(id_b).id == id_b


def test_tombstone_redump_uses_full_read_cap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: tombstone appends removal metadata to an already-valid record, so
    its re-dump must use the full read cap (`_MAX_FILE_BYTES`), not the
    headroom-reserved write cap. Otherwise a record written right up to the
    write cap becomes un-removable — the tombstone re-dump crosses the write
    cap and the write-side guard rejects it, leaving a fully-visible record
    that can neither be removed nor renamed."""
    import bettermemory.store as store_module
    from bettermemory import _frontmatter as fm

    memory = store.write(content="body to remove", scopes=["tools"])
    captured: dict[str, int] = {}
    real = store_module._atomic_write_post

    def spy(path, post, *, max_file_bytes=fm._MAX_WRITE_BYTES):
        captured["max_file_bytes"] = max_file_bytes
        return real(path, post, max_file_bytes=max_file_bytes)

    monkeypatch.setattr(store_module, "_atomic_write_post", spy)
    store.tombstone(memory.id, reason="obsolete")
    assert captured["max_file_bytes"] == fm._MAX_FILE_BYTES
    # The record really is gone from the active set and readable as a tombstone.
    assert memory.id in {t.id for t in store.load_tombstones()}


def test_near_write_cap_record_tombstoneable_with_overlong_reason(
    store: Store,
) -> None:
    """F1 completeness: a record admitted right at the write cap must ALWAYS be
    tombstoneable, even with a pathologically long removal reason.

    The maintenance headroom reserved below the read cap covers the fixed
    removal keys plus a BOUNDED reason; `Store.tombstone` caps `removed_reason`
    (`_cap_removed_reason`) so appending it can never push the tombstone
    re-dump past the read cap. Without the cap, an unbounded reason on a
    near-cap record re-dumped over `_MAX_FILE_BYTES` and the write-side guard
    left the record un-removable."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import _MAX_REMOVED_REASON_BYTES

    # Fresh-record frontmatter is a couple hundred bytes; size the body so the
    # serialized record lands just under the write cap (accepted at write).
    body = "x" * (fm._MAX_WRITE_BYTES - 512)
    mem = store.write(content=body, scopes=["tools"])

    # A reason far larger than the headroom must not make the record
    # un-removable — it is bounded, and the removal completes.
    overlong = "why-" * 4000  # ~16 KB, well over _MAX_REMOVED_REASON_BYTES
    path = store.tombstone(mem.id, reason=overlong)

    assert path.exists()
    # The tombstone stays readable (<= read cap) and appears in the listing.
    assert path.stat().st_size <= fm._MAX_FILE_BYTES
    assert mem.id in {t.id for t in store.load_tombstones()}
    # Reason was bounded to the cap (as UTF-8 bytes).
    text = path.read_text()
    assert "why-" in text  # a prefix of the reason survived
    stored_reason_len = len(overlong.encode("utf-8"))
    assert stored_reason_len > _MAX_REMOVED_REASON_BYTES  # test really is over-cap
