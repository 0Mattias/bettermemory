"""Tests for `schema_version` on the on-disk frontmatter.

The format invariants:
- New writes emit `schema_version: <SCHEMA_VERSION>`.
- Memories without the field are implicitly version 1 (legacy compat).
- Memories with a higher version raise `ValueError` on load and are
  skipped by `load_all` so a downgraded reader degrades gracefully
  rather than silently misinterpreting fields whose semantics changed.
- Tombstones share the same gate.

These tests pin the contract so a future schema-version bump must
update both the constant and the tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory import _frontmatter as frontmatter
from bettermemory.models import SCHEMA_VERSION, Confidence, Source
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


def test_new_write_emits_schema_version(store: Store, tmp_path: Path) -> None:
    memory = store.write(
        content="durable architectural decision worth keeping",
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
    )
    md_files = list(tmp_path.glob("*.md"))
    assert len(md_files) == 1
    post = frontmatter.load(md_files[0])
    assert post.metadata["schema_version"] == SCHEMA_VERSION
    assert post.metadata["id"] == memory.id


def test_update_preserves_schema_version(store: Store, tmp_path: Path) -> None:
    memory = store.write(
        content="durable architectural decision worth keeping",
        scopes=["tools"],
    )
    bumped = memory.model_copy(update={"body": memory.body + " (refined)\n"})
    store.update(bumped)
    md_files = list(tmp_path.glob("*.md"))
    post = frontmatter.load(md_files[0])
    assert post.metadata["schema_version"] == SCHEMA_VERSION


def test_tombstone_carries_schema_version(store: Store, tmp_path: Path) -> None:
    memory = store.write(
        content="durable architectural decision worth keeping",
        scopes=["tools"],
    )
    store.tombstone(memory.id, reason="just testing")
    tombstone_files = list((tmp_path / ".tombstones").glob("*.md"))
    assert len(tombstone_files) == 1
    post = frontmatter.load(tombstone_files[0])
    assert post.metadata["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Read side: legacy compat
# ---------------------------------------------------------------------------


_LEGACY_BODY = """---
id: 01HXYZ123ABCDEFGHJKMNPQRST
created: 2025-01-01T00:00:00+00:00
updated: 2025-01-01T00:00:00+00:00
scopes: [tools]
confidence: medium
source: explicit-statement
---
A durable architectural decision worth keeping
"""


def test_legacy_memory_without_schema_version_loads(
    store: Store, tmp_path: Path
) -> None:
    """Memories written before this constant existed have no
    `schema_version` field; readers must treat that as version 1."""
    legacy = tmp_path / "2025-01-01-legacy.md"
    legacy.write_text(_LEGACY_BODY, encoding="utf-8")
    memories = store.load_all()
    assert len(memories) == 1
    assert memories[0].id == "01HXYZ123ABCDEFGHJKMNPQRST"


# ---------------------------------------------------------------------------
# Read side: forward-incompatibility
# ---------------------------------------------------------------------------


def _future_body(version: int) -> str:
    return f"""---
schema_version: {version}
id: 01HXYZ999FUTUREVERSION0001
created: 2099-01-01T00:00:00+00:00
updated: 2099-01-01T00:00:00+00:00
scopes: [tools]
confidence: medium
source: explicit-statement
---
Memory written by a future bettermemory version.
"""


def test_future_version_raises_on_load_one(store: Store, tmp_path: Path) -> None:
    future = tmp_path / "2099-01-01-future.md"
    future.write_text(_future_body(SCHEMA_VERSION + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        store._load_path(future)


def test_future_version_skipped_by_load_all(store: Store, tmp_path: Path) -> None:
    """load_all is defensive: a forward-version file is skipped (silently —
    no log, no crash) so the rest of the store still works."""
    legacy = tmp_path / "2025-01-01-legacy.md"
    legacy.write_text(_LEGACY_BODY, encoding="utf-8")
    future = tmp_path / "2099-01-01-future.md"
    future.write_text(_future_body(SCHEMA_VERSION + 1), encoding="utf-8")
    memories = store.load_all()
    # Only the legacy memory loads; the future one is silently skipped.
    assert len(memories) == 1
    assert memories[0].id == "01HXYZ123ABCDEFGHJKMNPQRST"


def test_non_integer_schema_version_raises(store: Store, tmp_path: Path) -> None:
    weird = tmp_path / "2025-01-01-weird.md"
    weird.write_text(
        _LEGACY_BODY.replace(
            "id: 01HXYZ123ABCDEFGHJKMNPQRST",
            'schema_version: "not-an-int"\nid: 01HXYZ123ABCDEFGHJKMNPQRST',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        store._load_path(weird)


def test_future_version_tombstone_raises(store: Store, tmp_path: Path) -> None:
    """Tombstones share the same gate as active memories. A
    future-version tombstone causes `_load_tombstone_path` to raise;
    `load_tombstones` skips it the same way `load_all` does for active
    memories."""
    tombstone_dir = tmp_path / ".tombstones"
    tombstone_dir.mkdir(exist_ok=True)
    future_tomb = tombstone_dir / "2099-01-01-future.tombstone.md"
    future_tomb.write_text(
        _future_body(SCHEMA_VERSION + 1).replace(
            "Memory written by a future bettermemory version.",
            "removed: 2099-01-01T00:00:00+00:00\n"
            "removed_reason: future testing\n"
            "---\nbody",
        ),
        encoding="utf-8",
    )
    # The synthetic body above is wrong-shaped — the test reaches in
    # via the private loader to exercise the gate directly.
    with pytest.raises(ValueError, match="schema_version"):
        store._load_tombstone_path(future_tomb)
