"""`bettermemory stats`: the read-only usage and growth report.

A scratch store is seeded through the store's own methods with records
at fixed creation times, homes in projects (one of them merged into
another), declared and undeclared writers, a tombstone and episodes; the
report's sections are checked against what was written. `collect` takes
`now`, so the per-day series is deterministic; the CLI is run in process
on a store named by `BETTERMEMORY_DIR`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.cli.stats_cmd import FORMAT, collect, render
from bettermemory.identity import Actor
from bettermemory.models import (
    Category,
    Confidence,
    Memory,
    Source,
    generate_ulid,
)
from bettermemory.store import STORE_FILENAME, RecordHome, Store

_NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "store"
    directory.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    return directory


@pytest.fixture
def store(store_dir: Path, keys_dir: Path) -> Iterator[Store]:
    s = Store.create(store_dir / STORE_FILENAME, keys_dir=keys_dir)
    yield s
    s.close()


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def _put(
    store: Store,
    *scopes: str,
    days_ago: int,
    client: str | None = None,
    model: str | None = None,
    category: Category | None = None,
    project: str | None = None,
) -> Memory:
    created = _NOW - timedelta(days=days_ago)
    memory = Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=list(scopes),
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=f"A fact about {' and '.join(scopes)}, {days_ago} days old.\n",
        actor=None if client is None else Actor(client=client, model=model),
        category=category,
    )
    home = None if project is None else RecordHome(project, None, "test")
    return store.put_memory(memory, home=home)


def _project(store: Store, project_id: str, name: str, merged_into: str | None) -> None:
    store.put_membership(
        "project",
        {
            "id": project_id,
            "name": name,
            "merged_into": merged_into,
            "provisional": None,
            "parent": None,
            "tenant": None,
            "folder": 0,
            "boundary": 0,
            "minted": _NOW.isoformat(),
        },
    )


def _seed(store: Store) -> dict[str, Any]:
    _project(store, "P001", "alpha", None)
    _project(store, "P002", "beta", None)
    # P003 was merged into P001: its records count under P001.
    _project(store, "P003", "gamma", "P001")
    a = _put(
        store,
        "tools",
        "deploy",
        days_ago=0,
        client="claude-code",
        model="opus",
        category=Category.FACT,
        project="P001",
    )
    _put(
        store,
        "tools",
        days_ago=1,
        client="claude-code",
        model="opus",
        category=Category.FACT,
        project="P003",
    )
    _put(
        store,
        "ops",
        days_ago=1,
        client="cursor",
        model="gpt",
        category=Category.AMBIENT,
        project="P002",
    )
    _put(store, "tools", days_ago=40, project="global")
    doomed = _put(store, "ops", days_ago=2)
    store.tombstone(doomed.id, "wrong", now=_NOW - timedelta(days=1))
    store.write_episode(
        session_id="s-1",
        body="tried the fold",
        now=_NOW - timedelta(days=3),
        home=RecordHome("P002", None, "test"),
    )
    store.write_episode(session_id="s-2", body="tried again", now=_NOW)
    return {"newest": a}


def test_every_section_counts_what_was_written(store: Store) -> None:
    seeded = _seed(store)
    report = collect(store, days=7, now=_NOW)
    assert report["format"] == FORMAT
    assert report["version"] == 1
    assert report["store"]["memories"] == 4
    assert report["store"]["key_present"] is True
    assert report["files"]["db"] > 0
    assert report["files"]["total"] == sum(
        report["files"][name] for name in ("db", "wal", "shm")
    )
    tables = report["tables"]
    assert tables["memories"] == 4
    assert tables["tombstones"] == 1
    assert tables["episodes"] == 2
    assert tables["projects"] == 3
    assert tables["log"] == report["store"]["log_rows"]

    memories = report["memories"]
    assert memories["by_project"] == [
        {"project": "P001", "name": "alpha", "memories": 2, "episodes": 0},
        {"project": "P002", "name": "beta", "memories": 1, "episodes": 1},
        {"project": "global", "name": None, "memories": 1, "episodes": 0},
        {"project": None, "name": None, "memories": 0, "episodes": 1},
    ]
    assert memories["scopes_distinct"] == 3
    assert memories["by_scope"] == [
        {"scope": "tools", "memories": 3},
        {"scope": "deploy", "memories": 1},
        {"scope": "ops", "memories": 1},
    ]
    assert memories["by_client"] == [
        {"client": "claude-code", "memories": 2},
        {"client": None, "memories": 1},
        {"client": "cursor", "memories": 1},
    ]
    assert {e["model"]: e["memories"] for e in memories["by_model"]} == {
        "opus": 2,
        "gpt": 1,
        None: 1,
    }
    assert {e["category"]: e["memories"] for e in memories["by_category"]} == {
        "fact": 2,
        "ambient": 1,
        None: 1,
    }
    assert memories["newest"]["id"] == seeded["newest"].id
    assert memories["oldest"]["at"].startswith("2026-09-17")

    assert report["tombstones"]["total"] == 1
    assert report["tombstones"]["newest_removed"]["at"].startswith("2026-10-26")
    journal = report["episodes"]
    assert (journal["episodes"], journal["sessions"]) == (2, 2)
    assert journal["oldest"]["at"].startswith("2026-10-24")

    log = report["log"]
    assert sum(entry["rows"] for entry in log["by_kind"]) == log["rows"]
    assert sum(log["by_class"].values()) == log["rows"]
    kinds = {entry["kind"]: entry for entry in log["by_kind"]}
    assert kinds["memory_put"]["class"] == "mutation"
    assert kinds["store_created"]["class"] == "control"
    assert kinds["project_put"]["class"] == "mutation"


def test_the_window_buckets_records_by_day(store: Store) -> None:
    _seed(store)
    activity = collect(store, days=7, now=_NOW)["activity"]
    assert activity["window"] == {"days": 7, "from": "2026-10-21", "to": "2026-10-27"}
    days = {entry["day"]: entry for entry in activity["days"]}
    assert list(days) == [f"2026-10-{n}" for n in range(21, 28)]
    # Created by the record's own timestamp: the tombstoned record too,
    # the 40-day-old one outside the window.
    assert days["2026-10-27"]["created"] == 1
    assert days["2026-10-26"]["created"] == 2
    assert days["2026-10-25"]["created"] == 1
    assert activity["totals"]["created"] == 4
    assert days["2026-10-26"]["removed"] == 1
    assert activity["totals"]["removed"] == 1
    # The log rows were all written as the test ran, outside a window
    # ending on the fixed day; a two-day window ending at the last row
    # holds every one of them, even across a midnight.
    assert activity["totals"]["log_rows"] == 0
    last = store.log_rows()[-1].ts
    written = datetime.fromisoformat(last.replace("Z", "+00:00"))
    recent = collect(store, days=2, now=written)
    assert recent["activity"]["totals"]["log_rows"] == recent["store"]["log_rows"]
    assert recent["activity"]["totals"]["memory_puts"] == 5
    assert recent["activity"]["totals"]["episode_puts"] == 2
    assert recent["activity"]["sessions"] == 2


def test_an_empty_store_reports_zeros(store: Store) -> None:
    report = collect(store, days=30, now=_NOW)
    assert report["memories"]["total"] == 0
    assert report["memories"]["by_project"] == []
    assert report["memories"]["by_scope"] == []
    assert report["memories"]["oldest"] is None
    assert report["tombstones"]["oldest_removed"] is None
    assert report["episodes"]["episodes"] == 0
    assert report["log"]["rows"] == 1
    assert len(report["activity"]["days"]) == 30
    text = render(report)
    assert "records 0 memories, 0 tombstones, 0 episodes" in text
    assert "oldest none" in text


def test_text_names_every_section(store: Store) -> None:
    _seed(store)
    text = render(collect(store, days=7, now=_NOW))
    for line in (
        "bettermemory stats: ",
        "  records 4 memories, 1 tombstone, 2 episodes in 2 sessions",
        "  tables",
        "  by project",
        "    P001 alpha: 2 memories, 0 episodes",
        "    global: 1 memory, 0 episodes",
        "    (unhomed): 0 memories, 1 episode",
        "  top scopes (3 distinct)",
        "    tools 3",
        "  by client",
        "    (undeclared) 1",
        "  by category",
        "    (none) 1",
        "  last 7 days, 2026-10-21 to 2026-10-27:",
        "    2026-10-26        2        1",
    ):
        assert line in text, line
    # A table and its count never wrap apart.
    for row in text.splitlines():
        assert len(row) <= 88 or not row.startswith("    memories")


def test_store_text_is_made_printable(store: Store, store_dir: Path) -> None:
    memory = _put(store, "tools", days_ago=0, client="evil\x1b[2Jclient")
    assert "evil�[2Jclient 1" in render(collect(store, days=1, now=_NOW))
    # Not only names: ids, stamps and the engine version in `meta` are
    # whatever an editor of the file wrote there too.
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute(
        "UPDATE memories SET id = ?, updated = ? WHERE id = ?",
        ("X\x1b]0;id\x07", "2026-10-27\x1b[2J", memory.id),
    )
    outside.execute(
        "UPDATE meta SET value = ? WHERE key = 'engine_version'", ("9\x9b31m",)
    )
    outside.commit()
    outside.close()
    text = render(collect(store, days=1, now=_NOW))
    for raw in ("\x1b", "\x07", "\x9b"):
        assert raw not in text
    assert "engine 9�31m" in text


def test_cli_json_and_text(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(store)
    rows = len(store.log_rows())
    assert _run_cli(["stats", "--json", "--since", "3"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["format"] == "bettermemory-stats"
    assert report["activity"]["window"]["days"] == 3
    assert report["memories"]["total"] == 4
    assert _run_cli(["stats"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"bettermemory stats: {store.path}")
    assert "last 30 days" in out
    # Read-only: nothing was appended to the log.
    assert len(store.log_rows()) == rows


def test_cli_reads_a_keyless_store_without_rekeying(
    store: Store,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(store)
    rows = len(store.log_rows())
    store.close()
    for key in keys_dir.glob("*.key"):
        key.unlink()
    assert _run_cli(["stats", "--json"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["store"]["key_present"] is False
    assert report["log"]["rows"] == rows
    reopened = Store.open(store.path, allow_rekey=False)
    try:
        assert len(reopened.log_rows()) == rows
    finally:
        reopened.close()
    assert not list(keys_dir.glob("*.key"))
    assert _run_cli(["stats"], monkeypatch) == 0
    assert "key not held here" in capsys.readouterr().out


def test_cli_refuses_a_directory_without_a_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["stats"], monkeypatch) == 2
    assert STORE_FILENAME in capsys.readouterr().err
    assert not (tmp_path / STORE_FILENAME).exists()


@pytest.mark.parametrize("value", ["0", "-3", "seven", "99999"])
def test_cli_refuses_a_bad_window(
    store: Store,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["stats", "--since", value], monkeypatch) == 2
    assert "--since" in capsys.readouterr().err


def test_a_store_without_membership_tables_counts_every_record_unhomed(
    store: Store,
) -> None:
    _put(store, "tools", days_ago=0, project="P001")
    store.conn.execute("DROP TABLE homes")
    report = collect(store, days=1, now=_NOW)
    assert report["memories"]["by_project"] == [
        {"project": None, "name": None, "memories": 1, "episodes": 0}
    ]
    assert "homes" not in report["tables"]
