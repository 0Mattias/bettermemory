"""Tests for store.py — filesystem CRUD and tombstone behavior."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from bettermemory.models import Confidence, Source, generate_ulid, is_valid_ulid
from bettermemory.store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
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
