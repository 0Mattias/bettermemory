"""The export mirror (unit U3): the v8 file format written from the
bettermemory 9 store.

The renderers are pinned against the v8 writers themselves: a memory,
a tombstone and an episode written by `Store` and `EpisodeStore` must
come out of `render_memory`, `render_tombstone` and `render_episode`
byte for byte. `write_mirror` is pinned on naming, on leaving unchanged
files alone, on removing what the store no longer holds, and on
refusing a directory it did not make.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.episodes import EPISODES_DIR, EpisodeStore
from bettermemory.identity import Actor
from bettermemory.mirror import (
    MIRROR_MARKER,
    MirrorRefused,
    active_filename_for_tombstone,
    is_legacy_tombstone_name,
    memory_filename,
    render_episode,
    render_memory,
    render_tombstone,
    tombstone_filename,
    write_mirror,
)
from bettermemory.models import (
    Category,
    Confidence,
    Episode,
    LinkType,
    Memory,
    MemoryLink,
    Source,
    generate_ulid,
)
from bettermemory.origin import Origin
from bettermemory.sqlite_store import STORE_FILENAME, SqliteStore
from bettermemory.store import TOMBSTONE_DIR, Store, _parse_memory_file

_NOW = datetime(2026, 9, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)
_HEAD = "c" * 40


def _memory(body: str, *scopes: str, **overrides: Any) -> Memory:
    fields: dict[str, Any] = dict(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=list(scopes) or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )
    fields.update(overrides)
    return Memory(**fields)


def _full_memory(target: str) -> Memory:
    return _memory(
        "the whole record, every field set",
        "projects:foo",
        "tools",
        confidence=Confidence.HIGH,
        source=Source.INFERRED,
        origin=Origin(
            cwd="/w/foo",
            repo="https://example.com/foo.git",
            branch="main",
            worktree_root="/w/foo",
            source="roots",
        ),
        actor=Actor(
            client="claude-code",
            client_version="2.1.281",
            model="claude-fable-5-1",
            sources={"client": "client-info", "model": "header"},
        ),
        last_verified_at=_NOW + timedelta(hours=1),
        category=Category.USER_INFERENCE,
        verified_paths=["src/a.py", "src/b.py"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_absent_paths=["tools/gone.py"],
        claims=["src/a.py::main", "!tools/gone.py"],
        verified_head=_HEAD,
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=target, note="newer"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target),
        ],
        corroborations=2,
        last_corroborated=_NOW + timedelta(days=1),
    )


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def store(tmp_path: Path, keys_dir: Path) -> Iterator[SqliteStore]:
    s = SqliteStore.create(tmp_path / "v9" / STORE_FILENAME, keys_dir=keys_dir)
    try:
        yield s
    finally:
        s.close()


def _md_files(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {p.name for p in directory.iterdir() if p.is_file() and p.suffix == ".md"}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_the_names_follow_the_v8_rules() -> None:
    memory = _memory(
        "Kubernetes networking, the short version", id="01KR24QXN9A66G8DB2637E92G7"
    )
    assert memory_filename(memory) == (
        "2026-09-26-kubernetes-networking-the-short-version-01kr24qxn9a66g8db2637e92g7.md"
    )
    modern = "2026-05-07-homelab-backup-design.01KR289R24FAKSF731MMSVF3C0.tombstone.md"
    legacy = "2026-05-10-audit-sandbox-marker.tombstone.md"
    assert (
        active_filename_for_tombstone(modern) == "2026-05-07-homelab-backup-design.md"
    )
    assert active_filename_for_tombstone(legacy) == "2026-05-10-audit-sandbox-marker.md"
    assert active_filename_for_tombstone("2026-05-10-plain.md") == "2026-05-10-plain.md"
    assert is_legacy_tombstone_name(legacy) is True
    assert is_legacy_tombstone_name(modern) is False
    assert is_legacy_tombstone_name("2026-05-10-plain.md") is False
    assert (
        tombstone_filename(
            "2026-05-07-homelab-backup-design.md", "01KR289R24FAKSF731MMSVF3C0"
        )
        == modern
    )


# ---------------------------------------------------------------------------
# The renderers against the v8 writers
# ---------------------------------------------------------------------------


def test_render_memory_reproduces_the_v8_file(tmp_path: Path) -> None:
    v8 = Store(tmp_path / "v8")
    v8.ensure()
    plain = _memory("a plain record with nothing optional set")
    full = _full_memory(plain.id)
    for memory in (plain, full):
        path = v8._path_for(memory)
        v8._write_path(path, memory)
        assert render_memory(memory) == path.read_bytes()
        assert render_memory(_parse_memory_file(path)) == path.read_bytes()
    written = v8.write(
        content="a record written through the gate\n",
        scopes=["tools"],
        origin=Origin(cwd=str(tmp_path)),
    )
    path = next(p for p, m in v8.iter_active() if m.id == written.id)
    assert render_memory(written) == path.read_bytes()


def test_render_tombstone_reproduces_the_v8_file(tmp_path: Path) -> None:
    v8 = Store(tmp_path / "v8")
    v8.ensure()
    plain = _memory("a plain record")
    full = _full_memory(plain.id)
    for memory in (plain, full):
        v8._write_path(v8._path_for(memory), memory)
    plain_tombstone = v8.tombstone(plain.id, "gone", session_id="sess_x")
    full_tombstone = v8.tombstone(full.id, "gone too")
    for memory, path in ((plain, plain_tombstone), (full, full_tombstone)):
        dead = v8._load_tombstone_path(path)
        rendered = render_tombstone(
            dead,
            links=[
                link.model_dump(mode="json", exclude_none=True) for link in memory.links
            ],
            corroborations=memory.corroborations,
            last_corroborated=memory.last_corroborated,
        )
        assert rendered == path.read_bytes(), path.name


def test_render_episode_reproduces_the_v8_file(tmp_path: Path) -> None:
    episodes = EpisodeStore(tmp_path / "v8")
    origin = Origin(cwd=str(tmp_path), worktree_root=str(tmp_path), source="roots")
    written = [
        episodes.write(
            session_id="sess_a",
            body="tried the thing\n\nit worked  ",
            scopes=["projects:foo"],
            takeaway="it worked",
            origin=origin,
            swarm_id="sess_coordinator",
            now=_NOW,
        ),
        episodes.write(session_id="sess_b", body="bare", now=_NOW),
        episodes.write_floor(session_id="sess_b", origin=origin, now=_NOW),
    ]
    for episode in written:
        path = tmp_path / "v8" / EPISODES_DIR / episode.session_id / f"{episode.id}.md"
        assert render_episode(episode) == path.read_bytes(), path.name
        loaded = episodes._load_path(path)
        assert render_episode(loaded) == path.read_bytes(), path.name


# ---------------------------------------------------------------------------
# write_mirror
# ---------------------------------------------------------------------------


def test_the_mirror_lays_out_the_v8_tree(store: SqliteStore, tmp_path: Path) -> None:
    named = _memory("a named record")
    unnamed = _memory("an unnamed record about kubernetes")
    store.put_memory(named, filename="2026-09-26-a-named-record.md")
    store.put_memory(unnamed)
    store.tombstone(named.id, "gone", session="sess_x")
    episode = Episode(
        id=generate_ulid(),
        session_id="sess_a",
        created=_NOW,
        body="tried the thing\n",
        takeaway="it worked",
    )
    store.put_episode(episode)

    target = tmp_path / "mirror"
    report = write_mirror(store, target)
    assert report.active == 1 and report.tombstones == 1 and report.episodes == 1
    assert report.written == 3 and report.unchanged == 0 and report.removed == 0
    assert report.target == str(target.resolve())
    assert _md_files(target) == {memory_filename(unnamed)}
    assert _md_files(target / TOMBSTONE_DIR) == {
        tombstone_filename("2026-09-26-a-named-record.md", named.id)
    }
    assert _md_files(target / EPISODES_DIR / "sess_a") == {f"{episode.id}.md"}
    assert (target / memory_filename(unnamed)).read_bytes() == render_memory(unnamed)
    assert (target / EPISODES_DIR / "sess_a" / f"{episode.id}.md").read_bytes() == (
        render_episode(episode)
    )
    marker = json.loads((target / MIRROR_MARKER).read_text(encoding="utf-8"))
    assert marker["store_id"] == store.store_id
    assert marker["counts"] == {"active": 1, "tombstones": 1, "episodes": 1}
    text = report.render_text()
    assert "1 active" in text and str(target.resolve()) in text


def test_a_second_run_leaves_unchanged_files_alone(
    store: SqliteStore, tmp_path: Path
) -> None:
    memory = _memory("a record")
    store.put_memory(memory)
    store.put_episode(
        Episode(id=generate_ulid(), session_id="sess_a", created=_NOW, body="x\n")
    )
    target = tmp_path / "mirror"
    write_mirror(store, target)
    path = target / memory_filename(memory)
    before = path.stat().st_mtime_ns
    report = write_mirror(store, target)
    assert report.written == 0 and report.unchanged == 2 and report.removed == 0
    assert path.stat().st_mtime_ns == before


def test_a_changed_record_is_rewritten(store: SqliteStore, tmp_path: Path) -> None:
    memory = _memory("a record")
    store.put_memory(memory, filename="2026-09-26-a-record.md")
    target = tmp_path / "mirror"
    write_mirror(store, target)
    edited = memory.model_copy(update={"body": "a record, edited\n"})
    store.put_memory(edited)
    report = write_mirror(store, target)
    assert report.written == 1 and report.unchanged == 0
    assert (target / "2026-09-26-a-record.md").read_bytes() == render_memory(edited)


def test_what_the_store_no_longer_holds_is_removed(
    store: SqliteStore, tmp_path: Path
) -> None:
    first = _memory("the first record")
    second = _memory("the second record")
    store.put_memory(first, filename="2026-09-26-first.md")
    store.put_memory(second, filename="2026-09-26-second.md")
    episode = Episode(id=generate_ulid(), session_id="sess_a", created=_NOW, body="x\n")
    store.put_episode(episode)
    target = tmp_path / "mirror"
    write_mirror(store, target)

    store.tombstone(first.id, "gone")
    report = write_mirror(store, target)
    assert report.removed == 1 and report.written == 1
    assert _md_files(target) == {"2026-09-26-second.md"}
    assert _md_files(target / TOMBSTONE_DIR) == {
        tombstone_filename("2026-09-26-first.md", first.id)
    }

    store.delete_tombstone(first.id)
    store.delete_episode(episode.id)
    report = write_mirror(store, target)
    assert report.removed == 2 and report.written == 0
    assert _md_files(target / TOMBSTONE_DIR) == set()
    assert not (target / EPISODES_DIR / "sess_a").exists()
    assert _md_files(target) == {"2026-09-26-second.md"}

    store.put_memory(first, filename="2026-09-26-first.md")
    report = write_mirror(store, target)
    assert report.written == 1 and report.removed == 0
    assert _md_files(target) == {"2026-09-26-first.md", "2026-09-26-second.md"}


def test_the_mirror_refuses_a_directory_it_did_not_make(
    store: SqliteStore, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.md").write_text("mine\n", encoding="utf-8")
    with pytest.raises(MirrorRefused):
        write_mirror(store, foreign)
    assert (foreign / "notes.md").read_text(encoding="utf-8") == "mine\n"
    with pytest.raises(MirrorRefused):
        write_mirror(store, store.path.parent)
    as_file = tmp_path / "a-file"
    as_file.write_text("", encoding="utf-8")
    with pytest.raises(MirrorRefused):
        write_mirror(store, as_file)


def test_the_mirror_refuses_another_stores_mirror(
    store: SqliteStore, tmp_path: Path, keys_dir: Path
) -> None:
    other = SqliteStore.create(tmp_path / "other" / STORE_FILENAME, keys_dir=keys_dir)
    try:
        target = tmp_path / "mirror"
        write_mirror(other, target)
    finally:
        other.close()
    with pytest.raises(MirrorRefused):
        write_mirror(store, target)


def test_an_empty_or_absent_target_is_accepted(
    store: SqliteStore, tmp_path: Path
) -> None:
    store.put_memory(_memory("a record"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert write_mirror(store, empty).written == 1
    absent = tmp_path / "deeper" / "absent"
    assert write_mirror(store, absent).written == 1
    assert absent.is_dir()
