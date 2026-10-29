"""The fold reads the columns its build knows (ENG-107).

A later 9.x may add a nullable column to a folded table (CONTRIBUTING.md,
"Store versions"). This build's refold cannot produce that column, so it
compares each row over the columns its own schema gives the table, by
name. Reading the extra column made every row of the table differ, and
`log verify` on the older build reported the store tampered.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.models import Confidence, LinkType, Memory, MemoryLink, Source
from bettermemory.models import generate_ulid
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_schema import RecordHome

_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

#: The tables a later build adds a column to here: record tables, the
#: links folded with a memory, and a membership table.
_WIDENED = ("memories", "memory_links", "tombstones", "homes")


def _memory(body: str, **fields: Any) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
        **fields,
    )


@pytest.fixture
def path(tmp_path: Path) -> Path:
    """A store with a row in every table `_WIDENED` names."""
    path = tmp_path / "store" / STORE_FILENAME
    home = RecordHome("global", None, "test")
    with Store.create(path) as store:
        first = store.put_memory(_memory("the first record"), home=home)
        store.put_memory(
            _memory(
                "the second record",
                links=[MemoryLink(type=LinkType.EXTENDS, target_id=first.id)],
            ),
            home=home,
        )
        gone = store.put_memory(_memory("a record removed later"), home=home)
        store.tombstone(gone.id, reason="replaced")
    return path


def _sql(path: Path, script: str) -> None:
    """Change the store file as a later build's schema step would: past
    this build's store."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def _fold(path: Path) -> dict[str, Any]:
    with Store.open(path) as store:
        report = store.log_verify()
    return report


def _unaccounted(report: dict[str, Any]) -> dict[str, list[Any]]:
    fold = report["fold"]
    return {
        table: entry["unaccounted"]
        for section in (fold["tables"], fold["membership"])
        for table, entry in section.items()
        if entry["unaccounted"] or entry["missing"]
    }


def test_the_store_verifies_before_any_column_is_added(path: Path) -> None:
    report = _fold(path)
    assert report["status"] == "ok", _unaccounted(report)


def test_a_column_a_later_build_added_is_not_compared(path: Path) -> None:
    _sql(
        path,
        "".join(
            f"ALTER TABLE {table} ADD COLUMN later_note TEXT DEFAULT 'later';"
            for table in _WIDENED
        )
        + "UPDATE memories SET later_note = id;",
    )
    report = _fold(path)
    assert _unaccounted(report) == {}
    assert report["status"] == "ok"


def test_the_fold_still_compares_every_column_it_knows(path: Path) -> None:
    """The extra column is skipped, nothing else is: a body edited past
    the log is still the one row reported."""
    _sql(path, "ALTER TABLE memories ADD COLUMN later_note TEXT DEFAULT 'later';")
    with Store.open(path) as store:
        edited = next(iter(store.memory_ids()))
    _sql(
        path, f"UPDATE memories SET body = 'edited past the log' WHERE id = '{edited}';"
    )
    report = _fold(path)
    assert report["status"] == "tampered"
    assert _unaccounted(report) == {"memories": [edited]}


def test_a_table_holding_its_columns_in_another_order_is_compared_by_name(
    path: Path,
) -> None:
    """A later build that rebuilt a table (columns reordered, one added)
    leaves rows this build's fold still reads as its own."""
    _sql(
        path,
        """
        CREATE TABLE homes_rebuilt (
            later_note TEXT,
            decided TEXT NOT NULL,
            rule TEXT NOT NULL,
            checkout TEXT,
            project TEXT NOT NULL,
            kind TEXT NOT NULL,
            record_id TEXT PRIMARY KEY
        );
        INSERT INTO homes_rebuilt (decided, rule, checkout, project, kind, record_id)
            SELECT decided, rule, checkout, project, kind, record_id FROM homes;
        DROP TABLE homes;
        ALTER TABLE homes_rebuilt RENAME TO homes;
        """,
    )
    report = _fold(path)
    assert _unaccounted(report) == {}
    assert report["status"] == "ok"
