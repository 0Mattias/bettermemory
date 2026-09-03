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

from ._mcp import call_tool as _mcp_call


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
def test_store_root_has_owner_only_permissions_under_default_umask(
    tmp_path: Path,
) -> None:
    """The store ROOT gets the same explicit 0o700 the tombstone dir has
    always had, and it must not depend on the caller's umask.

    Load-bearing because a memory's filename embeds the first ~43 chars
    of its summary: under the usual 022 umask a 0o755 root let any local
    account read the gist of the entire store from `ls`, which the 0o600
    on the bodies does nothing to prevent. The umask is forced to 022
    here rather than trusted — that is precisely the condition the bug
    needed, so a test running under a stricter ambient umask would pass
    against the unfixed code.
    """
    previous = os.umask(0o022)
    try:
        root = tmp_path / "fresh-store"
        store = Store(root)
        mode = store.root.stat().st_mode & 0o777
    finally:
        os.umask(previous)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
@pytest.mark.parametrize(
    ("starting_mode", "expected"),
    [
        (0o755, 0o700),  # the default-umask legacy store
        (0o750, 0o700),  # group can still list filenames
        (0o705, 0o700),  # other can list filenames
        (0o770, 0o700),  # group has full access
        (0o700, 0o700),  # already correct — untouched
    ],
)
def test_store_root_mode_is_healed_on_open(
    tmp_path: Path, starting_mode: int, expected: int
) -> None:
    """`mkdir(mode=…)` is a no-op on a directory that already exists, so
    the explicit 0o700 above never reaches a store created by an earlier
    version. Opening one heals it.

    Every row lands on 0o700 because the heal MASKS to the owner triad
    rather than assigning a constant, and 0o700 is the strictest mode a
    store root can actually hold — a directory needs owner r/w/x for the
    store to open `.tombstones` and write memories at all. So there is no
    "owner went stricter" case to preserve here; the masking still
    matters as the reason the heal cannot ADD an owner bit that was
    deliberately absent, which `test_store_root_heal_is_best_effort`
    covers from the other side.
    """
    root = tmp_path / "legacy-store"
    root.mkdir()
    os.chmod(root, starting_mode)

    Store(root)

    mode = root.stat().st_mode & 0o777
    assert mode == expected, (
        f"{oct(starting_mode)} -> {oct(mode)}, want {oct(expected)}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
def test_store_root_heal_is_best_effort_when_chmod_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that refuses `chmod` must not stop the store opening.

    Sandboxed and network filesystems reject chmod on directories the
    caller genuinely owns. The store is entirely usable in that state —
    it is only the disclosure that persists — so the heal swallows OSError
    and leaves `doctor` to report the residual exposure. Without this the
    tightening would turn a cosmetic permission gap into a hard failure
    to open the store at all, which is strictly worse than the bug.
    """
    root = tmp_path / "readonly-fs-store"
    root.mkdir()
    os.chmod(root, 0o755)

    real_chmod = Path.chmod

    def refuse(self: Path, mode: int, **kwargs: object) -> None:
        if self == root:
            raise PermissionError(13, "Operation not permitted")
        real_chmod(self, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "chmod", refuse)

    store = Store(root)  # must not raise

    assert store.root == root.resolve()
    # The exposure is still there — that is the honest outcome, and it is
    # what the doctor check reports rather than silently claiming a heal.
    assert root.stat().st_mode & 0o077


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
    structured = await _mcp_call(server, "memory_search", {"query": query})
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
    `_handlers.load_search_candidates` hit the same TypeError pre-fix
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


def test_frontmatter_broken_in_place_is_silent_at_construction_and_doctors_job(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pins the equal-count gate's documented silence, and the surface
    that is supposed to break it instead.

    An in-place frontmatter break adds and removes nothing: the file
    keeps its name, so the raw `.md` count still equals the index row
    count and `_warn_on_index_divergence` returns at the count
    comparison without ever comparing identities. The memory is gone
    from every reader — `load_all`, and therefore `memory_search` — with
    its index row intact and not a word logged.

    That silence is a deliberate cost split, not an oversight: the
    identity leg parses every memory file — an order of magnitude more
    than the bare count it would replace — at every Store construction,
    which is server boot and every CLI command. `bettermemory doctor`
    pays it instead: `_check_index_health` reconciles identities before
    it will certify anything, and `_check_memory_parse_health` names the
    file outright. Both are asserted here, because the silence is only
    acceptable while they speak.

    A change that makes construction warn here turns this test red on
    purpose. It is a signal to move the cost deliberately, not a bug."""
    from bettermemory import index as _index
    from bettermemory import store as store_mod
    from bettermemory.doctor import _check_index_health, _check_memory_parse_health

    root = tmp_path / "broken-in-place"
    store = Store(root)
    kept = store.write(content="kept claim about ports\n", scopes=["tools"])
    doomed = store.write(content="doomed claim about ports\n", scopes=["tools"])
    resolved = root.expanduser().resolve()

    doomed_path = store._find_path_for_id(doomed.id)
    assert doomed_path is not None
    # Break the YAML itself, in place: same file, same name, same count.
    doomed_path.write_text(
        doomed_path.read_text(encoding="utf-8").replace(
            "scopes:", "scopes: [unterminated\nbogus:", 1
        ),
        encoding="utf-8",
    )

    # The gate's own two inputs are equal, which is what makes it return.
    assert len(list(root.glob("*.md"))) == 2
    assert len(_index.indexed_ids(resolved)) == 2

    store_mod._DIVERGENCE_WARNED_ROOTS.discard(resolved)
    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)

    logged = [
        r.getMessage()
        for r in caplog.records
        if r.name == "bettermemory.store" and r.levelname == "WARNING"
    ]
    assert logged == [], (
        "the equal-count gate returns before any identity comparison; a "
        f"warning here means the cost split moved: {logged!r}"
    )
    assert resolved not in store_mod._DIVERGENCE_WARNED_ROOTS

    # What that silence covers: one of the two memories is now invisible
    # to every reader while the index still carries its row.
    assert [m.id for m in store.load_all()] == [kept.id]
    assert doomed.id in _index.indexed_ids(resolved)

    # The compensating control: the surfaces that pay the parse. Both
    # must keep reporting this state for the silence above to be safe.
    index_health = _check_index_health(root)
    assert index_health.status == "warn"
    assert "no longer describes the store" in index_health.message
    parse_health = _check_memory_parse_health(root)
    assert parse_health.status == "warn"
    assert doomed_path.name in parse_health.message


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


# ---------------------------------------------------------------------------
# Size-cap axis (item 1): metadata-only re-dumps of an already-admitted record
# use the FULL read cap so a record whose serialized size sits in the reserved
# band `(_MAX_WRITE_BYTES, _MAX_FILE_BYTES]` — e.g. a pre-3.14.1 record written
# before the total-file cap existed — is never frozen from maintenance. New
# admission (write/update/restore) stays at the write cap so headroom is
# reserved for the tombstone re-dump.
# ---------------------------------------------------------------------------


def _write_band_memory_file(
    memory_dir: Path,
    *,
    memory_id: str,
    body_bytes: int,
    scopes: list[str] | None = None,
    updated: datetime | None = None,
    last_verified_at: datetime | None = None,
) -> Path:
    """Hand-write a memory file whose serialized size lands in the reserved
    band (> _MAX_WRITE_BYTES, <= _MAX_FILE_BYTES). The store's own `write()`
    caps at the write cap and could never produce one, so we serialize at the
    read cap directly — the exact shape a pre-3.14.1 record (written before the
    total-file cap) presents on disk today.

    `last_verified_at` (when passed) is embedded so the fixture models a band
    record that has ALREADY been attested — the shape a re-verify must keep
    maintainable at its CURRENT size under the max(write-cap, current-size) cap.
    """
    from bettermemory import _frontmatter as fm

    meta: dict[str, Any] = {
        "schema_version": 1,
        "id": memory_id,
        "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated": updated or datetime(2025, 1, 1, tzinfo=timezone.utc),
        "scopes": scopes or ["tools"],
        "confidence": "medium",
        "source": "explicit-statement",
    }
    if last_verified_at is not None:
        meta["last_verified_at"] = last_verified_at
    post = fm.Post(content="x" * body_bytes, metadata=meta)
    text = fm.dumps(post, max_file_bytes=fm._MAX_FILE_BYTES)
    total = len(text.encode("utf-8"))
    assert fm._MAX_WRITE_BYTES < total <= fm._MAX_FILE_BYTES, (
        f"fixture must land in the reserved band; got {total}"
    )
    path = memory_dir / f"2025-01-01-band-{memory_id.lower()}.md"
    # Write RAW BYTES, not text: the store persists via
    # `_fsutil.atomic_write_bytes` (UTF-8 bytes, LF preserved) on every
    # platform, so a band fixture must too. `write_text` translates `\n` to
    # `\r\n` on Windows, inflating the on-disk size past the LF `total` this
    # helper asserts and (near the read cap) risks pushing a band file over
    # the cap it was sized to sit under.
    path.write_bytes(text.encode("utf-8"))
    return path


def _write_yaml_band_memory_file(
    memory_dir: Path,
    *,
    memory_id: str,
    verified_paths: list[str],
    last_verified_at: datetime | None = None,
) -> Path:
    """Hand-write a memory whose FRONTMATTER-YAML region lands in the reserved
    YAML band (`> _MAX_YAML_BYTES - _REMOVAL_META_BUDGET_BYTES`, `<= _MAX_YAML_BYTES`)
    while the whole file stays well under the file-size band ceiling.

    The frontmatter-YAML twin of `_write_band_memory_file`. Post-fix,
    `mark_verified` / `rename_scope` reserve the removal-metadata budget on the
    YAML axis (`_lifecycle_redump_yaml_cap`), so the store can no longer GROW a
    record into this band — the only shapes that reach it now are a
    legacy/hand-written file or a record the pre-discipline code minted, which is
    exactly what this fixture models. We dump at the default (flat) YAML ceiling,
    which still admits a band-region frontmatter; only the opt-in lifecycle
    ceiling rejects it.
    """
    from bettermemory import _frontmatter as fm
    from bettermemory.store import (
        _REMOVAL_META_BUDGET_BYTES,
        _serialized_frontmatter_bytes,
    )

    meta: dict[str, Any] = {
        "schema_version": 1,
        "id": memory_id,
        "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "scopes": ["tools"],
        "confidence": "medium",
        "source": "explicit-statement",
        "verified_paths": list(verified_paths),
    }
    if last_verified_at is not None:
        meta["last_verified_at"] = last_verified_at
    post = fm.Post(content="body to remove", metadata=meta)
    # Default (flat) YAML ceiling — a first-write-shaped dump still admits a
    # band-region frontmatter; only the opt-in lifecycle ceiling rejects it.
    text = fm.dumps(post, max_file_bytes=fm._MAX_FILE_BYTES)
    yaml_region = _serialized_frontmatter_bytes(meta)
    assert (
        fm._MAX_YAML_BYTES - _REMOVAL_META_BUDGET_BYTES
        < yaml_region
        <= fm._MAX_YAML_BYTES
    ), f"fixture must land YAML in the reserved band; got {yaml_region}"
    path = memory_dir / f"2025-01-01-yamlband-{memory_id.lower()}.md"
    # Raw bytes, LF preserved — see `_write_band_memory_file` for why.
    path.write_bytes(text.encode("utf-8"))
    return path


def test_mark_verified_rewrites_band_record_at_current_size(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (intent preserved): a record whose serialized size ALREADY sits in the
    reserved band — e.g. a pre-3.14.1 record — stays verifiable. `mark_verified`
    caps its re-dump at max(write cap, current on-disk size), so re-verifying at
    the record's CURRENT size (no caller-driven growth) still succeeds even
    though the record is above the write cap. Reverting the cap to the plain
    write cap freezes the record (the write-side guard rejects the re-dump)."""
    from bettermemory import _frontmatter as fm  # noqa: F401

    fixed = datetime(2025, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
    # Pin utcnow to the fixture's own last_verified_at so the re-verify re-dump
    # is byte-for-byte the current size (only last_verified_at is re-stamped,
    # and to the same value) — it lands exactly at the max(write-cap, current)
    # cap, not over it.
    monkeypatch.setattr("bettermemory.store.utcnow", lambda: fixed)
    monkeypatch.setattr("bettermemory.models.utcnow", lambda: fixed)

    mid = generate_ulid()
    # Band record that is ALREADY attested (carries last_verified_at).
    _write_band_memory_file(
        memory_dir, memory_id=mid, body_bytes=1_047_000, last_verified_at=fixed
    )

    store = Store(memory_dir)
    # No caller-driven growth: a plain re-verify slides last_verified_at at the
    # record's current size and is admitted.
    verified = store.mark_verified(mid)
    assert verified.last_verified_at == fixed
    # And it persisted: a fresh store reads it back, still band-sized.
    reloaded = Store(memory_dir).load_one(mid)
    assert reloaded.last_verified_at == fixed
    assert len(reloaded.body) >= 1_047_000  # still a band-sized record


def test_mark_verified_first_verify_of_legacy_band_record_succeeds(
    memory_dir: Path,
) -> None:
    """F1 (scenario D): a legacy band record with NO last_verified_at (a genuine
    pre-3.14.1 record) must accept its FIRST verify — which necessarily GROWS the
    record by ~40 bytes of last_verified_at. Because the record already sits in
    the band, the cap is the full read cap, so the small attestation-timestamp
    growth is admitted: it cannot make the record less maintainable than it
    already is. Capping a band record at its EXACT current size (the earlier
    max(write-cap, current-size) form) froze first-time verification of exactly
    the records F1 exists to keep maintainable — reverting to that makes this
    fail."""
    mid = generate_ulid()
    # Band record that has NEVER been attested, with room below the read cap for
    # the added last_verified_at.
    _write_band_memory_file(memory_dir, memory_id=mid, body_bytes=1_046_000)

    store = Store(memory_dir)
    verified = store.mark_verified(mid)
    assert verified.last_verified_at is not None
    reloaded = Store(memory_dir).load_one(mid)
    assert reloaded.last_verified_at is not None
    assert len(reloaded.body) >= 1_046_000  # still band-sized, just now attested


def test_mark_verified_rejects_growth_into_reserved_band(
    store: Store, tmp_path: Path
) -> None:
    """Invariant restored (the data-loss seam): a verify whose caller-controlled
    verified_* additions would GROW a record admitted just under the write cap up
    into the reserved band `(_MAX_WRITE_BYTES, _MAX_FILE_BYTES]` is REJECTED.

    Without the max(write-cap, current-size) cap, `mark_verified` re-dumped at
    the flat read cap and silently minted a band record that `update` (write cap)
    can no longer touch, `tombstone` with a normal reason can push past the read
    cap (un-removable), and `restore` refuses to re-admit — so
    `prune_tombstones` eventually hard-deletes it: silent data loss. Reverting
    the cap to the plain read cap makes this verify succeed and this test fail."""
    from bettermemory import _frontmatter as fm

    # Admit a record a few KB under the write cap (a normal, maintainable
    # record — headroom reserved for its own tombstone/rename re-dump).
    body = "x" * (fm._MAX_WRITE_BYTES - 3000)
    mem = store.write(content=body, scopes=["tools"])
    assert mem.last_verified_at is None

    # Attest verified_paths whose re-dump crosses the write cap and lands SQUARELY
    # in the reserved band `(_MAX_WRITE_BYTES, _MAX_FILE_BYTES]` (~5 KB of paths on
    # a record ~2.8 KB below the write cap → ~2 KB into the 4 KB band). This is
    # the exact seam: at the flat read cap the growth is admitted (minting the
    # un-maintainable band record); at max(write-cap, current-size) it is
    # rejected. Well within the handler's 64 x 1024 per-list limits.
    #
    # The paths must EXIST: `mark_verified` refuses attestations the attesting
    # machine cannot stat, and that check runs before the re-dump, so fabricated
    # filler would make this test pass on the wrong error and stop covering the
    # band seam at all. Built as nested long components because a single 490-char
    # filename exceeds NAME_MAX on every filesystem this runs on.
    big_paths: list[str] = []
    for i in range(10):
        leaf = tmp_path / f"{i:02d}{'a' * 198}" / ("b" * 200) / ("c" * 80)
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("filler\n")
        big_paths.append(str(leaf))
    assert all(len(p) > 400 for p in big_paths)  # ~5 KB of paths in total
    with pytest.raises(ValueError, match="(?i)shrink"):
        store.mark_verified(mem.id, verified_paths=big_paths)

    # The rejected verify left the on-disk record untouched: not attested, not
    # grown into the band — so it stays updatable/removable/restorable.
    reloaded = store.load_one(mem.id)
    assert reloaded.last_verified_at is None
    assert reloaded.verified_paths == []
    assert reloaded.body.strip() == body
    # Still admissible at the write cap: a normal metadata update succeeds.
    store.update(reloaded.model_copy(update={"scopes": ["tools", "infra"]}))


def test_rename_scope_rewrites_band_record_at_current_size(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 twin for `rename_scope`'s active branch: a band-sized record stays
    renameable when the rename does not grow it (equal-length scope swap). The
    active-branch re-dump caps at max(write cap, current size); reverting to the
    plain write cap raises out of the re-dump and strands the record's scopes."""
    fixed = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Pin utcnow to the fixture's `updated` so the `updated` bump on rename does
    # not change the serialized width; the equal-length scope swap keeps the
    # re-dump exactly at the current size.
    monkeypatch.setattr("bettermemory.store.utcnow", lambda: fixed)
    monkeypatch.setattr("bettermemory.models.utcnow", lambda: fixed)

    mid = generate_ulid()
    _write_band_memory_file(
        memory_dir,
        memory_id=mid,
        scopes=["oldscope"],
        body_bytes=1_047_000,
        updated=fixed,
    )

    store = Store(memory_dir)
    changed = store.rename_scope("oldscope", "newscope")
    assert changed["active"] == [mid]
    assert "failed" not in changed
    reloaded = Store(memory_dir).load_one(mid)
    assert reloaded.scopes == ["newscope"]


def test_rename_scope_reports_growth_into_band_as_failed(store: Store) -> None:
    """Invariant twin for `rename_scope`: a rename whose longer `new` scope would
    grow a record admitted just under the write cap into the reserved band is
    collected in `failed` (not applied, not counted as a clean rename) — the same
    max(write-cap, current-size) cap as `mark_verified`. Reverting to the plain
    read cap admits the growth and drops the record from `failed`."""
    from bettermemory import _frontmatter as fm

    # A record just under the write cap carrying a short scope.
    body = "x" * (fm._MAX_WRITE_BYTES - 512)
    mem = store.write(content=body, scopes=["s"])
    # Rename `s` -> a scope ~1 KB longer, pushing the re-dump past the write cap.
    long_new = "s" + ("z" * 1000)
    result = store.rename_scope("s", long_new)

    assert result["active"] == []  # not admitted into the band
    assert mem.id in {entry["id"] for entry in result["failed"]}
    assert any("shrink" in entry["reason"].lower() for entry in result["failed"])
    # On-disk record is untouched: still the short scope, still maintainable.
    reloaded = store.load_one(mem.id)
    assert reloaded.scopes == ["s"]


def test_restore_band_tombstone_round_trips(store: Store) -> None:
    """Item-1a (v2): restore RE-ADMITS any loadable tombstone — including one
    whose stripped active record exceeds the write cap (a pre-3.14.1 record, or
    one that grew while active). The 3.15.0 write-cap refusal turned these into
    a one-way door: no tombstone-edit surface exists, so nothing could shrink a
    refused tombstone, and `prune_tombstones` eventually HARD-DELETED it —
    silent data loss for exactly the records the band discipline exists to
    protect. Restore now re-admits at the read cap; the restored band active
    stays maintainable (`_lifecycle_redump_cap`) and removable (adaptive
    removal-metadata trimming), so the tombstone⇄active round-trip holds in
    both directions. Reverting restore to the write-cap refusal makes the
    restore below raise and this test fail."""
    from bettermemory import _frontmatter as fm

    mid = generate_ulid()
    # Body == the write cap: the stripped active record (body + frontmatter)
    # necessarily exceeds the write cap, while the tombstone (+ removal
    # metadata) still fits under the read cap so `frontmatter.load` can read it.
    body = "x" * fm._MAX_WRITE_BYTES
    post = fm.Post(
        content=body,
        metadata={
            "schema_version": 1,
            "id": mid,
            "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "scopes": ["tools"],
            "confidence": "medium",
            "source": "explicit-statement",
            "removed": datetime(2025, 1, 2, tzinfo=timezone.utc),
            "removed_reason": "legacy oversized record",
        },
    )
    tomb_text = fm.dumps(post, max_file_bytes=fm._MAX_FILE_BYTES)
    assert len(tomb_text.encode("utf-8")) <= fm._MAX_FILE_BYTES  # readable tombstone
    store.tombstone_dir.mkdir(mode=0o700, exist_ok=True)
    tpath = store.tombstone_dir / f"2025-01-01-legacy.{mid}.tombstone.md"
    # Raw bytes (see `_write_band_memory_file`): match how the store writes,
    # and keep the on-disk size platform-independent near the cap.
    tpath.write_bytes(tomb_text.encode("utf-8"))

    restored = store.restore(mid)
    assert restored.id == mid
    assert restored.body.strip() == body
    assert not tpath.exists()

    # The restored band active is NOT a one-way object: it can be re-removed
    # (adaptive trimming keeps its tombstone under the read cap) …
    new_tomb = store.tombstone(mid, "re-removed after restore", session_id="sess-x")
    assert new_tomb.exists()
    assert new_tomb.stat().st_size <= fm._MAX_FILE_BYTES

    # … and restored again: the full round-trip holds both ways.
    again = store.restore(mid)
    assert again.body.strip() == body


def test_mark_verified_band_arm_reserves_removal_budget(memory_dir: Path) -> None:
    """F1 (v2): the band arm of `mark_verified` caps at the read cap MINUS
    `_REMOVAL_META_BUDGET_BYTES`, not the flat read cap. At the flat read cap a
    LEGAL verified_paths attestation (one entry, well within the handler's
    64x1024 limits) could grow a band record to within a few bytes of the read
    cap — after which `tombstone`, which must append removal metadata under the
    same read cap, raised: an un-removable record. The band arm must reject
    growth past the ceiling AND the record must remain removable afterwards.
    Reverting the band arm to the flat read cap admits the growth and fails
    this test at the `raises` below."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import _REMOVAL_META_BUDGET_BYTES

    mid = generate_ulid()
    _write_band_memory_file(memory_dir, memory_id=mid, body_bytes=1_046_600)
    store = Store(memory_dir)
    path = next(p for p in store._iter_active_paths() if mid.lower() in p.name)
    current = path.stat().st_size
    ceiling = fm._MAX_FILE_BYTES - _REMOVAL_META_BUDGET_BYTES
    # Fixture sanity: in the band, below the ceiling — the shape the band arm
    # exists to keep maintainable.
    assert fm._MAX_WRITE_BYTES < current < ceiling

    # One verified path sized to cross the ceiling while staying comfortably
    # under the read cap — the exact growth the flat-read-cap arm admitted.
    overshoot = ceiling - current + 400
    assert overshoot < 1024  # legal single-entry input at the handler layer
    with pytest.raises(ValueError, match="(?i)shrink"):
        store.mark_verified(mid, verified_paths=["/repo/" + "y" * overshoot])

    # The refused verify left the record intact — and, the actual point,
    # still REMOVABLE: its tombstone re-dump fits under the read cap.
    tomb = store.tombstone(mid, "still removable", session_id="sess-y")
    assert tomb.stat().st_size <= fm._MAX_FILE_BYTES


def test_mark_verified_rejects_yaml_growth_into_reserved_band(store: Store) -> None:
    """Frontmatter-YAML-axis twin of
    `test_mark_verified_band_arm_reserves_removal_budget`.

    `_frontmatter.dumps` enforces `_MAX_YAML_BYTES` on the frontmatter region
    unconditionally, but the file axis was the only one that reserved tombstone
    room. So a LEGAL `mark_verified` (dense `verified_paths`, every entry within
    the handler's 64x1024 bounds) could grow a record's frontmatter to within the
    removal-metadata budget of the YAML cap — a band in which even the dual-axis
    adaptive trim can no longer fit the `removed:` line, leaving the record
    un-removable. `_lifecycle_redump_yaml_cap` closes that: a verify may grow the
    frontmatter up to the reserved ceiling but no further.

    Mutation-soundness: pre-fix (no YAML band reservation) the SECOND verify below
    succeeds — its frontmatter still parses under the flat YAML cap — so the
    `pytest.raises` gets no exception and the test fails. Post-fix it is refused."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import (
        _REMOVAL_META_BUDGET_BYTES,
        _serialized_frontmatter_bytes,
    )

    ceiling = fm._MAX_YAML_BYTES - _REMOVAL_META_BUDGET_BYTES
    entry = "/repo/" + ("y" * (1019 - len("/repo/")))  # a legal ~1019-char path

    mem = store.write(content="keep me removable", scopes=["tools"])

    # 61 legal paths grow the frontmatter YAML close to — but under — the reserved
    # ceiling: ADMITTED (growth up to the ceiling is allowed).
    store.mark_verified(mem.id, verified_paths=[entry for _ in range(61)])
    path = next(p for p in store._iter_active_paths() if mem.id.lower() in p.name)
    near_yaml = _serialized_frontmatter_bytes(fm.load(path).metadata)
    # Fixture sanity: near the ceiling, still under it (the shape the reservation
    # keeps maintainable).
    assert ceiling - 2000 < near_yaml < ceiling

    # 63 legal paths (still <= 64 entries, <= 1024 chars each) would push the
    # frontmatter YAML INTO the reserved band (> ceiling, < the flat YAML cap) —
    # the exact LEGAL attestation the pre-fix code admitted, minting an
    # un-removable record. Post-fix it is refused with a shrink-first hint.
    band_paths = [entry for _ in range(63)]
    assert len(band_paths) <= 64 and all(len(p) <= 1024 for p in band_paths)
    with pytest.raises(ValueError, match="(?i)shrink"):
        store.mark_verified(mem.id, verified_paths=band_paths)

    # The refused verify left the near-ceiling attestation intact …
    reloaded = store.load_one(mem.id)
    assert len(reloaded.verified_paths) == 61
    # … and the record that reached the cap is STILL removable: its tombstone
    # re-dump fits under the YAML cap (adaptive trim covers the rest).
    tomb = store.tombstone(mem.id, "still removable", session_id="sess-y")
    assert _serialized_frontmatter_bytes(fm.load(tomb).metadata) <= fm._MAX_YAML_BYTES
    assert tomb.stat().st_size <= fm._MAX_FILE_BYTES


def test_tombstone_adaptively_trims_removal_metadata_near_read_cap(
    memory_dir: Path,
) -> None:
    """Adaptive removal-metadata trimming: a legacy record within the removal
    budget of the read cap (ABOVE the `_lifecycle_redump_cap` ceiling — nothing
    post-fix can create one, but a pre-3.14.1 file presents exactly this shape)
    must still be removable. The fixed 1 KiB reason budget alone would push its
    tombstone past the read cap; the caps ADAPT to the room the record actually
    has left: the session id is dropped first, then the reason is trimmed
    toward empty. Reverting to the fixed budgets makes this tombstone raise out
    of the re-dump — the un-removable-record class, one band deeper."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import _REMOVAL_META_BUDGET_BYTES

    mid = generate_ulid()
    _write_band_memory_file(memory_dir, memory_id=mid, body_bytes=1_048_100)
    store = Store(memory_dir)
    path = next(p for p in store._iter_active_paths() if mid.lower() in p.name)
    current = path.stat().st_size
    # Fixture sanity: above the band ceiling (the doomed sliver).
    assert current > fm._MAX_FILE_BYTES - _REMOVAL_META_BUDGET_BYTES

    long_reason = "r" * 2000
    tomb = store.tombstone(mid, long_reason, session_id="s" * 100)
    assert tomb.stat().st_size <= fm._MAX_FILE_BYTES

    match = next(t for t in store.load_tombstones() if t.id == mid)
    # The reason survived as a (much shorter) prefix; the session was dropped
    # — the event log remains the canonical session join for the audit trail.
    assert match.removed_reason
    assert long_reason.startswith(match.removed_reason)
    assert len(match.removed_reason) < 2000
    assert match.removed_session is None

    # And the round-trip back to active still holds.
    restored = store.restore(mid)
    assert restored.id == mid


def test_tombstone_adaptively_trims_removal_metadata_near_yaml_cap(
    memory_dir: Path,
) -> None:
    """Item 5 — the un-removable class on the frontmatter-YAML axis, the mirror
    of `test_tombstone_adaptively_trims_removal_metadata_near_read_cap` on the
    file-size axis.

    `_frontmatter.dumps` enforces `_MAX_YAML_BYTES` on the frontmatter region
    UNCONDITIONALLY, independent of total file size. A record whose frontmatter
    sits just under that YAML cap while the whole file is ~1 MB below the read
    cap must still be removable: `tombstone` budgets its removal metadata on BOTH
    axes and takes the tighter, so the adaptive trim fires on the YAML axis (the
    session is dropped, the reason trimmed) and the removal completes. Reverting
    the budget to the file axis alone makes the `store.tombstone` below raise
    `frontmatter YAML exceeds ...` and this test fail.

    The record is HAND-WRITTEN into the YAML band (the frontmatter-YAML twin of
    `_write_band_memory_file`), NOT grown via `mark_verified`: post-fix the
    lifecycle YAML band reservation (`_lifecycle_redump_yaml_cap`) forbids a
    verify from minting such a record, so the only shapes that reach this band
    are legacy/hand-written files — exactly as the file-axis sibling reaches its
    doomed sliver via `_write_band_memory_file` rather than a lifecycle grow."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import (
        _REMOVAL_META_BUDGET_BYTES,
        _cap_removed_reason,
        _serialized_frontmatter_bytes,
    )

    mid = generate_ulid()
    # 63 verified paths of 1019 chars each — a wholly LEGAL attestation shape
    # (<= 64 entries, each <= 1024 chars) — puts the frontmatter YAML a few
    # hundred bytes under `_MAX_YAML_BYTES` while the file stays ~1 MB under the
    # read cap. Hand-written because post-fix `mark_verified` would refuse to grow
    # a record here (that refusal is what closes the un-removable corner at the
    # source; this test covers the residual legacy/hand-written shape).
    legal_paths = ["/repo/" + ("y" * (1019 - len("/repo/"))) for _ in range(63)]
    assert len(legal_paths) <= 64 and all(len(p) <= 1024 for p in legal_paths)
    _write_yaml_band_memory_file(memory_dir, memory_id=mid, verified_paths=legal_paths)
    store = Store(memory_dir)

    path = next(p for p in store._iter_active_paths() if mid.lower() in p.name)
    file_size = path.stat().st_size
    yaml_region = _serialized_frontmatter_bytes(fm.load(path).metadata)

    # Two-axis fixture sanity — the exact shape the bug lives in:
    #  * the file has ~1 MB of headroom, so the pre-fix file-axis budget alone
    #    dwarfs the removal metadata and would NOT trim, yet
    assert file_size < fm._MAX_FILE_BYTES - _REMOVAL_META_BUDGET_BYTES
    #  * the frontmatter YAML sits inside the removal-metadata headroom of the
    #    YAML cap, so the untrimmed removal metadata cannot fit on THAT axis.
    assert (
        fm._MAX_YAML_BYTES - _REMOVAL_META_BUDGET_BYTES
        < yaml_region
        < fm._MAX_YAML_BYTES
    )

    long_reason = "r" * 2000
    # Pre-fix (file-axis budget only) this raises `frontmatter YAML exceeds
    # 65536-byte cap` and leaves the record active; post-fix the YAML-axis
    # budget forces the trim and the removal completes.
    tomb = store.tombstone(mid, long_reason, session_id="s" * 100)

    # The re-dump fits under BOTH caps.
    assert tomb.stat().st_size <= fm._MAX_FILE_BYTES
    assert _serialized_frontmatter_bytes(fm.load(tomb).metadata) <= fm._MAX_YAML_BYTES

    match = next(t for t in store.load_tombstones() if t.id == mid)
    # The trim is what made it fit: the reason survived only as a prefix, trimmed
    # BELOW even the fixed 1 KiB reason cap (proof the ADAPTIVE YAML-axis trim
    # fired, not merely the fixed per-field cap), and the session was dropped
    # first — the YAML-axis mirror of the file-axis adaptation.
    assert match.removed_reason
    assert long_reason.startswith(match.removed_reason)
    assert len(match.removed_reason) < len(_cap_removed_reason(long_reason))
    assert match.removed_session is None

    # The record is no longer stranded active — gone from the active set …
    assert mid not in {m.id for m in store.load_all()}
    # … and the round-trip back to active still holds.
    restored = store.restore(mid)
    assert restored.id == mid


def test_cap_removed_reason_bounds_serialized_size_not_raw() -> None:
    """Item-1d: `_cap_removed_reason` bounds the reason on its SERIALIZED
    (YAML-escaped) size, not its raw byte length. A control-character reason
    escape-inflates ~4x under `yaml.dump` (`\\xNN`), so a raw-length bound of
    1 KiB serializes to ~4.3 KiB — past the 4 KiB maintenance headroom, making a
    near-write-cap record un-removable. Reverting to a raw-length bound leaves
    the control-char reason at ~4.3 KiB serialized, failing this assertion."""
    from bettermemory.store import (
        _MAX_REMOVED_REASON_BYTES,
        _cap_removed_reason,
        _serialized_reason_bytes,
    )

    control_heavy = "\x01" * 4096  # 4 KiB of control bytes, all escaped on dump
    capped = _cap_removed_reason(control_heavy)
    assert _serialized_reason_bytes(capped) <= _MAX_REMOVED_REASON_BYTES
    # A short printable reason is returned verbatim — the bound only bites when
    # the *serialized* form would overflow.
    assert _cap_removed_reason("user said so") == "user said so"


# ---------------------------------------------------------------------------
# rename_scope partial-rename abort (item 6): a single record that overflows
# the read cap on re-dump (the scope swap grows the file past _MAX_FILE_BYTES)
# or hits a disk error must NOT abort the whole rename mid-loop. Both the active
# and tombstone branches guard the per-record re-dump: on failure they collect
# {id, reason}, skip the record, and continue; the FTS index upsert runs only
# on a successful write. The failed ids are returned so a partial run reports
# which records did not rename instead of silently claiming full success.
# ---------------------------------------------------------------------------


def _write_overflow_on_rename_active(
    memory_dir: Path, *, memory_id: str, old_scope: str
) -> Path:
    """Hand-write an active memory that is readable on disk with `old_scope`
    (total <= read cap) but whose re-dump overflows the read cap once
    `old_scope` is swapped for a much longer new scope — the exact
    partial-rename-abort trigger item 6 guards. The store's own `write()` caps
    at the write cap and could never mint one; we serialize at the read cap."""
    from bettermemory import _frontmatter as fm

    post = fm.Post(
        content="x" * 1_045_000,
        metadata={
            "schema_version": 1,
            "id": memory_id,
            "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "scopes": [old_scope],
            "confidence": "medium",
            "source": "explicit-statement",
        },
    )
    text = fm.dumps(post, max_file_bytes=fm._MAX_FILE_BYTES)
    assert len(text.encode("utf-8")) <= fm._MAX_FILE_BYTES  # readable as written
    path = memory_dir / f"2025-01-01-overflow-{memory_id.lower()}.md"
    path.write_bytes(text.encode("utf-8"))  # raw bytes — see _write_band_memory_file
    return path


def _write_overflow_on_rename_tombstone(
    store: Store, *, memory_id: str, old_scope: str
) -> Path:
    """Tombstone twin of `_write_overflow_on_rename_active`: readable as a
    tombstone with `old_scope`, but its re-dump overflows the read cap once the
    scope swaps to a much longer value."""
    from bettermemory import _frontmatter as fm

    post = fm.Post(
        content="y" * 1_045_000,
        metadata={
            "schema_version": 1,
            "id": memory_id,
            "created": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "scopes": [old_scope],
            "confidence": "medium",
            "source": "explicit-statement",
            "removed": datetime(2025, 1, 2, tzinfo=timezone.utc),
            "removed_reason": "legacy oversized tombstone",
        },
    )
    text = fm.dumps(post, max_file_bytes=fm._MAX_FILE_BYTES)
    assert len(text.encode("utf-8")) <= fm._MAX_FILE_BYTES
    store.tombstone_dir.mkdir(mode=0o700, exist_ok=True)
    tpath = (
        store.tombstone_dir / f"2025-01-01-overflow.{memory_id.lower()}.tombstone.md"
    )
    tpath.write_bytes(text.encode("utf-8"))  # raw bytes — see _write_band_memory_file
    return tpath


def test_rename_scope_skips_overflowing_records_on_both_branches(
    store: Store, memory_dir: Path
) -> None:
    """Item 6: `rename_scope` completes the healthy records and REPORTS the
    overflowing ones instead of aborting mid-loop. Exercises BOTH branches:
    an overflowing active record and an overflowing tombstone sit alongside a
    healthy active record and a healthy tombstone, all carrying the old scope.

    Mutation-sound for both guards: reverting EITHER branch's try/except lets
    the overflowing record's re-dump raise straight out of `rename_scope`, so
    the call under test raises and the test errors. (The active branch runs
    first, so dropping its guard raises before the tombstone branch; dropping
    only the tombstone guard raises after the active branch completes.)"""
    old, new = "oldscope", "z" * 6000

    # Healthy active + healthy tombstone (renamed cleanly).
    active_good = store.write(content="small active body\n", scopes=[old])
    tomb_seed = store.write(content="small tombstoned body\n", scopes=[old])
    store.tombstone(tomb_seed.id, reason="removed for the test")
    tomb_good_id = tomb_seed.id

    # Overflowing active + overflowing tombstone (skipped + reported).
    active_bad_id = generate_ulid()
    _write_overflow_on_rename_active(memory_dir, memory_id=active_bad_id, old_scope=old)
    tomb_bad_id = generate_ulid()
    _write_overflow_on_rename_tombstone(store, memory_id=tomb_bad_id, old_scope=old)

    result = store.rename_scope(old, new)

    # Healthy records renamed on both branches.
    assert result["active"] == [active_good.id]
    assert result["tombstoned"] == [tomb_good_id]
    # Both overflowing records reported failed with a reason, neither renamed.
    failed_ids = {entry["id"] for entry in result["failed"]}
    assert failed_ids == {active_bad_id, tomb_bad_id}
    assert all(entry["reason"] for entry in result["failed"])

    # The healthy active record actually carries the new scope on a fresh read.
    reloaded = Store(memory_dir).load_one(active_good.id)
    assert reloaded.scopes == [new]
    # The overflowing active record kept the OLD scope (skip, not partial write).
    from bettermemory import _frontmatter as fm

    bad_meta = fm.load(memory_dir / f"2025-01-01-overflow-{active_bad_id.lower()}.md")
    assert bad_meta.metadata["scopes"] == [old]


def test_tombstone_caps_pathological_session_id(store: Store) -> None:
    """Item 7: `Store.tombstone` bounds `removed_session` on its SERIALIZED size
    the same way `removed_reason` is bounded, so a pathological session id can't
    push a near-write-cap record's tombstone re-dump past the read cap and make
    the record un-removable.

    Mutation-sound: reverting the cap (storing the raw session id) escape-
    inflates ~8 KB of control bytes to ~32 KB, overflowing `_MAX_FILE_BYTES` in
    the tombstone re-dump so `store.tombstone` raises — the call under test
    would error instead of returning a path."""
    from bettermemory import _frontmatter as fm
    from bettermemory.store import (
        _MAX_REMOVED_SESSION_BYTES,
        _serialized_session_bytes,
    )

    # Land the record just under the write cap so only the session id's headroom
    # contribution decides whether the tombstone stays under the read cap.
    body = "x" * (fm._MAX_WRITE_BYTES - 512)
    mem = store.write(content=body, scopes=["tools"])

    # ~8 KiB of control bytes: raw ~8 KB, YAML-escaped ~4x. Uncapped, this alone
    # overruns the 4 KiB maintenance headroom and the read cap.
    pathological = "\x01" * 8192
    path = store.tombstone(mem.id, reason="obsolete", session_id=pathological)

    assert path.exists()
    # The tombstone stays readable (<= read cap) and appears in the listing.
    assert path.stat().st_size <= fm._MAX_FILE_BYTES
    tomb = store.load_tombstone(mem.id)
    # Session was bounded on serialized size, not dropped entirely.
    assert tomb.removed_session is not None
    assert len(tomb.removed_session) > 0
    assert _serialized_session_bytes(tomb.removed_session) <= _MAX_REMOVED_SESSION_BYTES


def test_cap_removed_session_bounds_serialized_size_not_raw() -> None:
    """Item 7 helper: `_cap_removed_session` bounds on SERIALIZED (YAML-escaped)
    size, mirroring `_cap_removed_reason`. A control-character session id
    escape-inflates under `yaml.dump`, so a raw-length bound would let it
    overflow the headroom. A short printable id is returned verbatim."""
    from bettermemory.store import (
        _MAX_REMOVED_SESSION_BYTES,
        _cap_removed_session,
        _serialized_session_bytes,
    )

    control_heavy = "\x01" * 4096
    capped = _cap_removed_session(control_heavy)
    assert _serialized_session_bytes(capped) <= _MAX_REMOVED_SESSION_BYTES
    assert _cap_removed_session("session-01HXYZ") == "session-01HXYZ"


def test_episode_write_admits_band_body_at_read_cap(memory_dir: Path) -> None:
    """Item-1 critic gap: episodes are written once and pruned wholesale, never
    tombstoned/renamed, so they reserve no headroom and admit at the full read
    cap. The 3.14.1 total-file cap made `frontmatter.dumps` default to the
    reduced write cap, which silently froze episode bodies in the reserved band.
    A band-sized episode body must still persist and read back. Reverting the
    episode fix (write-cap default) raises at write time."""
    from bettermemory.episodes import EpisodeStore

    est = EpisodeStore(memory_dir)
    band_body = "e" * 1_046_000  # over the write cap, under the read cap
    ep = est.write(session_id="sess1", body=band_body, scopes=["tools"])
    loaded = est.list_by_session("sess1")
    assert [e.id for e in loaded] == [ep.id]
    assert len(loaded[0].body) >= 1_046_000


# ---------------------------------------------------------------------------
# mark_verified — attestations must be checkable on the attesting machine
# ---------------------------------------------------------------------------


def test_mark_verified_does_not_itself_check_path_existence(store: Store) -> None:
    """The LAYER SPLIT, pinned deliberately. Refusing an attestation whose
    path cannot be stat'd is the `memory_verify` HANDLER's job (see
    `tests/test_server.py::test_memory_verify_refuses_unstattable_attestation`),
    NOT this primitive's.

    Two reasons, and both bite if someone "helpfully" moves the check down
    here. First, `Store.mark_verified` is the persistence primitive, matching
    `Store.write`'s split: the store enforces structural limits, policy sits
    above it — and no production caller besides the handler passes
    attestations at all, so there is no second attesting caller for a
    handler-level check to miss. Second, the concurrency and cap suites use
    `verified_paths` as an opaque marker to exercise CAS and size seams;
    enforcing existence here breaks four concurrency tests and roughly
    eighteen call sites for no gain in coverage.

    So this asserts the primitive stays dumb."""
    memory = store.write(content="Config lives at `/no/such/file.toml`.", scopes=["t"])
    verified = store.mark_verified(memory.id, verified_paths=["/no/such/file.toml"])
    assert verified.last_verified_at is not None
    assert verified.verified_paths == ["/no/such/file.toml"]


def test_mark_verified_accepts_paths_that_exist(store: Store, tmp_path: Path) -> None:
    """The control. Without it every assertion above would also pass if the
    check had become a blanket refusal of all attestations."""
    real = tmp_path / "present.py"
    real.write_text("x = 1\n")
    memory = store.write(content=f"The module lives at `{real}`.", scopes=["t"])
    verified = store.mark_verified(memory.id, verified_paths=[str(real)])
    assert verified.last_verified_at is not None
    assert verified.verified_paths == [str(real)]


def test_mark_verified_absent_paths_are_exempt(store: Store) -> None:
    """`verified_absent_paths` attests intentional ABSENCE, so
    non-existence IS the claim. Applying the existence check to it would
    invert the escape hatch into a permanent failure."""
    memory = store.write(content="No `vendor/` directory in this tree.", scopes=["t"])
    verified = store.mark_verified(
        memory.id, verified_absent_paths=["/deliberately/absent"]
    )
    assert verified.last_verified_at is not None
    assert verified.verified_absent_paths == ["/deliberately/absent"]


def test_mark_verified_skips_unanchored_relative_attestation(store: Store) -> None:
    """A relative attestation with no `origin.worktree_root` to anchor it
    cannot be resolved, and could-not-ask must never manufacture a negative
    verdict — the same distinction `compute_commit_drift` draws by returning
    None. So it passes rather than being reported as missing."""
    memory = store.write(content="Lives at `src/pkg/mod.py`.", scopes=["t"])
    assert memory.origin is None or memory.origin.worktree_root is None
    verified = store.mark_verified(memory.id, verified_paths=["src/pkg/mod.py"])
    assert verified.last_verified_at is not None


def test_mark_verified_stamps_the_local_verification_in_the_index(
    store: Store, memory_dir: Path
) -> None:
    """`verified_locally_at` (schema v8) is set at the verify upsert and
    only there: a write leaves it NULL, an update keeps whatever the row
    carries, and a verify stamps the instant it wrote `last_verified_at`."""
    from bettermemory import index

    memory = store.write(content="a memory to verify on this host", scopes=["tools"])
    assert (
        index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at is None
    )

    verified = store.mark_verified(memory.id)
    row = index.trust_for(memory_dir, [memory.id])[memory.id]
    assert row.verified_locally_at is not None
    assert row.verified_locally_at == verified.last_verified_at.isoformat()

    current = store.load_one(memory.id)
    store.update(
        current.model_copy(update={"scopes": ["tools", "infrastructure"]}),
        preserve_verification=True,
    )
    assert index.trust_for(memory_dir, [memory.id])[memory.id].verified_locally_at == (
        row.verified_locally_at
    )
