"""``Store.open_or_create`` refuses to create an empty store beside an
un-migrated v8 directory: a server pointed at such a directory would
otherwise serve nothing and say nothing, which is what happened to the
developer's install on 2026-09-26. ``bettermemory migrate v8`` is the
answer, and the error names it."""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory.store import STORE_FILENAME, Store, UnmigratedV8DirectoryError


def test_an_empty_directory_gets_a_new_store(tmp_path: Path) -> None:
    with Store.open_or_create(tmp_path / "fresh" / STORE_FILENAME) as store:
        assert store.count_memories() == 0
    assert (tmp_path / "fresh" / STORE_FILENAME).is_file()


@pytest.mark.parametrize(
    "marker",
    [
        "2026-05-07-a-memory-01KR8BXGMCK7YB3G9DW2Y5XC8C.md",
        ".events.jsonl",
        ".events.03.jsonl",
    ],
)
def test_a_v8_directory_without_a_store_is_refused(tmp_path: Path, marker: str) -> None:
    v8 = tmp_path / "v8"
    v8.mkdir()
    (v8 / marker).write_text("")
    with pytest.raises(UnmigratedV8DirectoryError) as caught:
        Store.open_or_create(v8 / STORE_FILENAME)
    assert "migrate v8" in str(caught.value)
    assert not (v8 / STORE_FILENAME).exists()


def test_a_migrated_directory_opens_its_store(tmp_path: Path) -> None:
    v8 = tmp_path / "v8"
    v8.mkdir()
    (v8 / ".events.jsonl").write_text("")
    Store.create(v8 / STORE_FILENAME).close()
    with Store.open_or_create(v8 / STORE_FILENAME) as store:
        assert store.count_memories() == 0


def test_an_unrelated_markdown_file_is_not_a_v8_marker(tmp_path: Path) -> None:
    d = tmp_path / "notes"
    d.mkdir()
    (d / "README.md").write_text("# notes")
    with Store.open_or_create(d / STORE_FILENAME) as store:
        assert store.count_memories() == 0
