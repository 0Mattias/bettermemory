"""The export mirror (unit U3): the v8 file format written from the
bettermemory 9 store.

The renderers are pinned against the golden v8 fixture under
`tests/fixtures/v8/store`, written once at 8.0.0 by the v8 writers
themselves (`Store` and `EpisodeStore`; see `tests/fixtures/v8/generate.py`):
every memory, tombstone and episode file there must come out of
`render_memory`, `render_tombstone` and `render_episode` byte for byte
when rendered from what the frozen readers (`bettermemory.v8`) parse.
`write_mirror` is pinned on naming, on leaving unchanged files alone, on
removing what the store no longer holds, and on refusing a directory it
did not make.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _frontmatter as frontmatter
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
    Confidence,
    Episode,
    Memory,
    Source,
    generate_ulid,
)
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.v8 import (
    EPISODE_METADATA_KEYS,
    EPISODES_DIR,
    MEMORY_METADATA_KEYS,
    TOMBSTONE_DIR,
    TOMBSTONE_METADATA_KEYS,
    iter_active_memory_paths,
    iter_session_ids,
    iter_tombstone_paths,
    list_by_session,
    parse_episode_file,
    parse_memory_file,
    parse_tombstone_file,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v8" / "store"
_NOW = datetime(2026, 9, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)


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


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def store(tmp_path: Path, keys_dir: Path) -> Iterator[Store]:
    s = Store.create(tmp_path / "v9" / STORE_FILENAME, keys_dir=keys_dir)
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


def test_the_fixture_names_are_the_ones_the_writers_gave() -> None:
    for path in iter_active_memory_paths(FIXTURE):
        assert memory_filename(parse_memory_file(path)) == path.name
    for path in iter_tombstone_paths(FIXTURE):
        dead = parse_tombstone_file(path)
        active = active_filename_for_tombstone(path.name)
        assert active == memory_filename(parse_memory_file(path))
        if not is_legacy_tombstone_name(path.name):
            assert tombstone_filename(active, dead.id) == path.name


# ---------------------------------------------------------------------------
# The renderers against the files the v8 writers wrote
# ---------------------------------------------------------------------------


def test_the_fixture_is_checked_out_byte_for_byte() -> None:
    """The renderer tests below and the mirror tests in `test_migrate_v8.py`
    compare bytes with the fixture, so git must check every file under it
    out as committed. With `core.autocrlf` on, the windows-latest leg got
    the text files with CRLF endings, which no v8 writer produced, and
    every byte comparison failed; `.gitattributes` unsets `text` for the
    tree, which turns end-of-line conversion off on every platform."""
    repo = FIXTURE.parents[3]
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", "tests/fixtures/v8/store"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    paths = [path for path in listed.split("\0") if path]
    assert paths
    checked = subprocess.run(
        ["git", "-C", str(repo), "check-attr", "-z", "text", "--", *paths],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    # `-z` prints <path> NUL <attribute> NUL <value> NUL per path.
    fields = checked.split("\0")
    assert dict(zip(fields[0::3], fields[2::3])) == dict.fromkeys(paths, "unset")


def test_render_memory_reproduces_the_v8_files() -> None:
    paths = sorted(iter_active_memory_paths(FIXTURE))
    assert len(paths) == 3
    keys: set[str] = set()
    for path in paths:
        assert render_memory(parse_memory_file(path)) == path.read_bytes(), path.name
        keys |= set(frontmatter.load(path).metadata)
    # Between them the three files carry every key the writer could emit,
    # so a renderer that dropped or renamed one would be caught here.
    assert keys == set(MEMORY_METADATA_KEYS)


def test_render_tombstone_reproduces_the_v8_files() -> None:
    paths = sorted(iter_tombstone_paths(FIXTURE))
    assert len(paths) == 2
    sessions: set[str | None] = set()
    with_links = 0
    for path in paths:
        dead = parse_tombstone_file(path)
        kept = parse_memory_file(path)
        rendered = render_tombstone(
            dead,
            links=[
                link.model_dump(mode="json", exclude_none=True) for link in kept.links
            ],
            corroborations=kept.corroborations,
            last_corroborated=kept.last_corroborated,
        )
        assert rendered == path.read_bytes(), path.name
        sessions.add(dead.removed_session)
        with_links += bool(kept.links and kept.corroborations)
        removal_keys = set(frontmatter.load(path).metadata) & set(
            TOMBSTONE_METADATA_KEYS
        )
        assert {"removed", "removed_reason"} <= removal_keys
    assert sessions == {"sess_x", None}
    assert with_links == 1


def test_render_episode_reproduces_the_v8_files() -> None:
    keys: set[str] = set()
    floors = 0
    count = 0
    for session_id in iter_session_ids(FIXTURE):
        for episode in list_by_session(FIXTURE, session_id):
            path = FIXTURE / EPISODES_DIR / session_id / f"{episode.id}.md"
            assert render_episode(episode) == path.read_bytes(), path.name
            assert render_episode(parse_episode_file(path)) == path.read_bytes()
            keys |= set(frontmatter.load(path).metadata)
            floors += episode.is_floor
            count += 1
    assert count == 3 and floors == 1
    assert keys == set(EPISODE_METADATA_KEYS)


# ---------------------------------------------------------------------------
# write_mirror
# ---------------------------------------------------------------------------


def test_the_mirror_lays_out_the_v8_tree(store: Store, tmp_path: Path) -> None:
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
    store: Store, tmp_path: Path
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


def test_a_changed_record_is_rewritten(store: Store, tmp_path: Path) -> None:
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
    store: Store, tmp_path: Path
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
    store: Store, tmp_path: Path
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
    store: Store, tmp_path: Path, keys_dir: Path
) -> None:
    other = Store.create(tmp_path / "other" / STORE_FILENAME, keys_dir=keys_dir)
    try:
        target = tmp_path / "mirror"
        write_mirror(other, target)
    finally:
        other.close()
    with pytest.raises(MirrorRefused):
        write_mirror(store, target)


def test_an_empty_or_absent_target_is_accepted(store: Store, tmp_path: Path) -> None:
    store.put_memory(_memory("a record"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert write_mirror(store, empty).written == 1
    absent = tmp_path / "deeper" / "absent"
    assert write_mirror(store, absent).written == 1
    assert absent.is_dir()
