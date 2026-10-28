"""`bettermemory memory history`: one record's life, read off the signed log.

The engine (`bettermemory.history`) is tested on stores written through
the store's own methods, so every row is the one the live paths write;
the telemetry rows the tool calls add beside them are recorded with
`Store.record_event` and an explicit actor, and one test drives the real
tools through the MCP server to pin the event shapes the engine reads.
The CLI is run in process on a scratch store named by `BETTERMEMORY_DIR`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.history import (
    ACTOR_FROM_EVENT,
    ACTOR_FROM_RECORD,
    EVENT,
    FORMAT,
    RECORD,
    REFERENCE,
    printable,
    record_history,
    render_json,
    render_text,
)
from bettermemory.identity import Actor
from bettermemory.models import LinkType, MemoryLink, generate_ulid, utcnow
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import STORE_FILENAME, RecordHome, Store

from ._mcp import call_tool as _mcp_call

_WRITER = Actor(client="claude-code", model="opus-test", session="s-1")
_EDITOR = Actor(client="cursor", model="gpt-test", principal="os:owner")


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


def _life(store: Store) -> str:
    """A memory written, edited, verified, corroborated, removed, restored
    and removed again, with the tool calls' telemetry beside each change."""
    memory = store.write(
        content="The deploy runs from the main branch.\nIt takes ten minutes.",
        scopes=["tools"],
        actor=_WRITER,
        session="s-1",
    )
    store.record_event("write", id=memory.id, status="committed", actor=_WRITER)
    edited = memory.model_copy(
        update={
            "body": "The deploy runs from the release branch.\nIt takes ten minutes.\n",
            "scopes": ["tools", "deploy"],
        }
    )
    store.update(edited, session="s-2")
    store.record_event(
        "update", id=memory.id, fields=["content", "scopes"], actor=_EDITOR
    )
    store.mark_verified(memory.id, session="s-2")
    store.record_event("verify", id=memory.id, actor=_EDITOR)
    store.record_corroboration(memory.id, session="s-3")
    store.tombstone(memory.id, "superseded by the new pipeline", session="s-3")
    store.record_event(
        "remove", id=memory.id, reason="superseded by the new pipeline", actor=_EDITOR
    )
    store.restore(memory.id, session="s-4")
    store.record_event("restore", id=memory.id, actor=_EDITOR)
    # A read names the record too; it is not history.
    store.record_event("show", id=memory.id, actor=_EDITOR)
    return memory.id


def _actions(history: Any) -> list[tuple[str, str]]:
    return [(row.kind, row.action) for row in history.rows]


def test_a_record_s_life_is_listed_in_log_order_with_what_each_row_did(
    store: Store,
) -> None:
    record_id = _life(store)
    history = record_history(store, record_id)
    assert _actions(history) == [
        ("memory_put", "created"),
        ("write", "write"),
        ("memory_put", "updated"),
        ("update", "update"),
        ("memory_put", "verified"),
        ("verification_put", "verification"),
        ("verify", "verify"),
        ("memory_put", "corroborated"),
        ("memory_delete", "removed"),
        ("tombstone_put", "tombstoned"),
        ("remove", "remove"),
        ("tombstone_delete", "restored"),
        ("memory_put", "restored"),
        ("restore", "restore"),
    ]
    seqs = [row.seq for row in history.rows]
    assert seqs == sorted(seqs)
    assert history.state == "active"
    assert history.record_kind == "memory"
    assert history.provenance == "local"
    assert history.key_present is True
    assert all(row.verified is True for row in history.rows)
    assert history.exit_code == 0
    assert history.findings == []


def test_rows_say_what_changed_and_who_acted(store: Store) -> None:
    history = record_history(store, _life(store))
    rows = {(row.kind, row.action): row for row in history.rows}
    created = rows[("memory_put", "created")]
    # A put row carries the record, whose actor is its author, kept on
    # every later version; the event rows carry the request's actor.
    assert created.role == RECORD
    assert created.actor_source == ACTOR_FROM_RECORD
    assert created.actor is not None and created.actor["client"] == "claude-code"
    updated = rows[("memory_put", "updated")]
    assert updated.changed == ("body", "scopes")
    assert updated.actor is not None and updated.actor["client"] == "claude-code"
    update_event = rows[("update", "update")]
    assert update_event.role == EVENT
    assert update_event.actor_source == ACTOR_FROM_EVENT
    assert update_event.actor is not None
    assert update_event.actor["client"] == "cursor"
    assert update_event.actor["principal"] == "os:owner"
    assert update_event.detail["fields"] == ["content", "scopes"]
    verified = rows[("memory_put", "verified")]
    assert "last_verified_at" in verified.changed
    assert rows[("memory_put", "corroborated")].changed == (
        "corroborations",
        "last_corroborated",
    )
    tombstoned = rows[("tombstone_put", "tombstoned")]
    assert tombstoned.detail["reason"] == "superseded by the new pipeline"
    assert rows[("remove", "remove")].detail["reason"] == (
        "superseded by the new pipeline"
    )
    assert rows[("memory_delete", "removed")].actor is None


def test_diff_shows_the_edited_line_and_the_first_body(store: Store) -> None:
    history = record_history(store, _life(store))
    created, updated = history.rows[0], history.rows[2]
    assert created.diff is not None
    assert "--- (none)" in created.diff
    assert "+The deploy runs from the main branch." in created.diff
    assert updated.diff is not None
    assert "-The deploy runs from the main branch." in updated.diff
    assert "+The deploy runs from the release branch." in updated.diff
    # The body did not change on a verify: no diff there.
    assert history.rows[4].diff is None
    text = render_text(history, diff=True)
    assert "      +The deploy runs from the release branch." in text
    assert "-The deploy" not in render_text(history, diff=False)


def test_a_pruned_record_keeps_its_history(store: Store) -> None:
    memory = store.write(content="A short-lived fact.", scopes=["tools"])
    store.tombstone(memory.id, "wrong")
    pruned = store.prune_tombstones(timedelta(0), now=utcnow() + timedelta(days=1))
    assert pruned == [memory.id]
    history = record_history(store, memory.id)
    assert history.state == "pruned"
    assert _actions(history) == [
        ("memory_put", "created"),
        ("memory_delete", "removed"),
        ("tombstone_put", "tombstoned"),
        ("tombstone_delete", "pruned"),
    ]
    assert history.exit_code == 0


def test_an_episode_and_its_home(store: Store) -> None:
    episode = store.write_episode(
        session_id="s-9",
        body="tried the fold first",
        home=RecordHome("global", None, "outside"),
    )
    history = record_history(store, episode.id)
    assert history.record_kind == "episode"
    assert history.state == "active"
    assert history.provenance == "local"
    assert _actions(history) == [("episode_put", "created"), ("home_put", "homed")]
    assert history.rows[1].detail == {"project": "global", "rule": "outside"}
    store.prune_episode_sessions(ttl_days=1, now=utcnow() + timedelta(days=3))
    gone = record_history(store, episode.id)
    assert gone.state == "pruned"
    assert gone.rows[-1].action == "pruned"


def test_links_from_other_records_are_references_reported_once(
    store: Store,
) -> None:
    old = store.write(content="Paging goes through PagerDuty.", scopes=["ops"])
    new = store.write(
        content="Paging moved to Opsgenie.",
        scopes=["ops"],
        links=[MemoryLink(type=LinkType.SUPERSEDES, target_id=old.id)],
    )
    # A later version of the new record keeps the link: no second row.
    store.mark_verified(new.id)
    current = store.load_one(new.id)
    store.update(current.model_copy(update={"links": []}))
    history = record_history(store, old.id)
    references = [row for row in history.rows if row.role == REFERENCE]
    assert [(row.action, row.detail) for row in references] == [
        ("superseded_by", {"source": new.id, "type": "supersedes"}),
        ("unlinked", {"source": new.id, "type": "supersedes"}),
    ]
    # The rows of the other record are not this record's versions.
    assert [row.action for row in history.rows if row.role == RECORD] == ["created"]


def test_a_link_goes_when_the_record_carrying_it_is_pruned(store: Store) -> None:
    """Removing the linking record keeps the link on its tombstone, and a
    restore brings it back: neither is the link going. Pruning the
    tombstone is, though no row of it names the linked record."""
    old = store.write(content="Paging goes through PagerDuty.", scopes=["ops"])
    new = store.write(
        content="Paging moved to Opsgenie.",
        scopes=["ops"],
        links=[MemoryLink(type=LinkType.SUPERSEDES, target_id=old.id)],
    )
    store.tombstone(new.id, "wrong")
    store.restore(new.id)
    store.tombstone(new.id, "wrong again")
    assert [row.action for row in record_history(store, old.id).rows] == [
        "created",
        "superseded_by",
    ]
    store.prune_tombstones(timedelta(0), now=utcnow() + timedelta(days=1))
    references = [
        (row.kind, row.action, row.detail)
        for row in record_history(store, old.id).rows
        if row.role == REFERENCE
    ]
    assert references == [
        ("memory_put", "superseded_by", {"source": new.id, "type": "supersedes"}),
        ("tombstone_delete", "unlinked", {"source": new.id, "type": "supersedes"}),
    ]


def test_a_conflict_filed_against_the_record_is_a_reference(store: Store) -> None:
    a = store.write(content="The port is 8443.", scopes=["ops"])
    b = store.write(content="The port is 9443.", scopes=["ops"])
    store.put_conflict(
        {
            "id": generate_ulid(),
            "a_id": a.id,
            "b_id": b.id,
            "summary_a": "8443",
            "summary_b": "9443",
            "similarity": 0.9,
            "method": "jaccard",
            "detector": "test",
            "created": utcnow().isoformat(),
            "status": "pending",
        }
    )
    row = record_history(store, b.id).rows[-1]
    assert (row.role, row.action) == (REFERENCE, "conflict")
    assert row.detail["other"] == a.id
    assert row.detail["status"] == "pending"


def test_json_schema(store: Store) -> None:
    record_id = _life(store)
    data = json.loads(render_json(record_history(store, record_id), diff=True))
    assert data["format"] == FORMAT
    assert data["version"] == 1
    assert data["id"] == record_id
    assert data["record_kind"] == "memory"
    assert data["state"] == "active"
    assert data["findings"] == []
    row = data["rows"][0]
    assert set(row) == {
        "seq",
        "ts",
        "session",
        "kind",
        "role",
        "action",
        "verified",
        "actor",
        "actor_source",
        "changed",
        "detail",
        "diff",
    }
    plain = json.loads(render_json(record_history(store, record_id)))
    assert "diff" not in plain["rows"][0]


def test_a_row_whose_mac_fails_is_a_finding(store: Store, store_dir: Path) -> None:
    memory = store.write(content="The deploy runs nightly.", scopes=["tools"])
    seq = int(
        store.conn.execute(
            "SELECT seq FROM log WHERE kind = 'memory_put' ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
    )
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute(
        "UPDATE log SET payload = replace(payload, 'nightly', 'hourly') WHERE seq = ?",
        (seq,),
    )
    outside.commit()
    outside.close()
    history = record_history(store, memory.id)
    assert history.rows[0].verified is False
    assert history.exit_code == 1
    assert any("fails its MAC" in line for line in history.findings)
    assert "MAC FAILS" in render_text(history)


def test_a_planted_record_reads_unaccounted(store: Store, store_dir: Path) -> None:
    memory = store.write(content="The real record.", scopes=["tools"])
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute(
        "UPDATE memories SET body = 'A planted body.', log_mac = 'x' WHERE id = ?",
        (memory.id,),
    )
    outside.commit()
    outside.close()
    history = record_history(store, memory.id)
    assert history.provenance == "unaccounted"
    assert history.exit_code == 1


def test_a_record_no_log_row_ever_named_is_found_in_its_table(
    store: Store, store_dir: Path
) -> None:
    """The planted shape: a row copied in with sqlite3 under a fresh id.
    The log names it nowhere, yet the history knows the record (it is in
    a table) and says it entered outside the store."""
    memory = store.write(content="A real record to copy.", scopes=["tools"])
    planted = generate_ulid()
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    columns = [row[1] for row in outside.execute("PRAGMA table_info(memories)")]
    copied = [c for c in columns if c not in ("rowid", "id")]
    outside.execute(
        f"INSERT INTO memories(id, {', '.join(copied)}) "
        f"SELECT ?, {', '.join(copied)} FROM memories WHERE id = ?",
        (planted, memory.id),
    )
    outside.commit()
    outside.close()
    history = record_history(store, planted)
    assert history.rows == ()
    assert (history.state, history.record_kind) == ("active", "memory")
    assert history.provenance == "unaccounted"
    assert history.exit_code == 1
    assert "unaccounted" in render_text(history)


def test_a_tombstone_the_log_never_put_reads_unaccounted(
    store: Store, store_dir: Path, keys_dir: Path
) -> None:
    """The planted shape for a tombstone: a row copied in with sqlite3
    under a fresh id. No pointer of a tombstone's is checked, but the
    log's last word on a real one is the `tombstone_put` that wrote it,
    with the key or without it."""
    memory = store.write(content="A record to remove and copy.", scopes=["tools"])
    store.tombstone(memory.id, "wrong")
    planted = generate_ulid()
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    columns = [row[1] for row in outside.execute("PRAGMA table_info(tombstones)")]
    copied = [c for c in columns if c != "id"]
    outside.execute(
        f"INSERT INTO tombstones(id, {', '.join(copied)}) "
        f"SELECT ?, {', '.join(copied)} FROM tombstones WHERE id = ?",
        (planted, memory.id),
    )
    outside.commit()
    outside.close()
    real = record_history(store, memory.id)
    assert (real.state, real.provenance, real.exit_code) == ("tombstoned", None, 0)
    history = record_history(store, planted)
    assert (history.state, history.record_kind) == ("tombstoned", "memory")
    assert (history.provenance, history.exit_code) == ("unaccounted", 1)
    assert "the tombstoned record reads unaccounted" in render_text(history)
    store.close()
    for key in keys_dir.glob("*.key"):
        key.unlink()
    keyless = Store.open(store_dir / STORE_FILENAME, keys_dir=keys_dir)
    try:
        history = record_history(keyless, planted)
        assert (history.provenance, history.exit_code) == ("unaccounted", 1)
        assert "  provenance unaccounted\n" in render_text(history)
    finally:
        keyless.close()


def test_the_rows_beside_a_record_are_its_history_too(store: Store) -> None:
    """Its home moving, its standing answer, this host's stamp going, the
    capture batch that wrote it and the v8 file it came from."""
    from bettermemory.log import CAPTURE_MEMBER

    memory = store.write(
        content="Captured from a chat.",
        scopes=["tools"],
        home=RecordHome("P001", None, "repo"),
    )
    store.record_capture(
        CAPTURE_MEMBER,
        {"batch": "b-1", "memory_id": memory.id, "joined_by": "token"},
    )
    store.put_import(
        "notes/captured.md",
        content_hash="0" * 64,
        imported_at=utcnow().isoformat(),
        memory_id=memory.id,
    )
    store.put_membership(
        "standing",
        {
            "record_id": memory.id,
            "probability": 0.9,
            "body_sha256": "0" * 64,
            "source": "owner",
            "answered_by": "os:owner",
            "answered": utcnow().isoformat(),
        },
    )
    store.delete_membership("standing", {"record_id": memory.id})
    store.mark_verified(memory.id)
    store.delete_verification(memory.id)
    store.put_membership(
        "home",
        {
            "record_id": memory.id,
            "kind": "memory",
            "project": "P002",
            "checkout": None,
            "rule": "declared",
            "decided": utcnow().isoformat(),
        },
    )
    store.delete_membership("home", {"record_id": memory.id})
    rows = record_history(store, memory.id).rows
    assert [(row.kind, row.action) for row in rows] == [
        ("memory_put", "created"),
        ("home_put", "homed"),
        ("capture_member", "captured"),
        ("import_put", "import"),
        ("standing_put", "standing"),
        ("standing_delete", "standing_removed"),
        ("memory_put", "verified"),
        ("verification_put", "verification"),
        ("verification_delete", "verification_removed"),
        ("home_put", "moved"),
        ("home_delete", "unhomed"),
    ]
    by_action = {row.action: row for row in rows}
    assert by_action["captured"].detail == {"batch": "b-1", "joined_by": "token"}
    assert by_action["import"].detail == {"source": "notes/captured.md"}
    assert by_action["standing"].detail["probability"] == 0.9
    assert by_action["moved"].detail == {"project": "P002", "rule": "declared"}


def test_an_imported_record_says_so(store: Store) -> None:
    memory = store.write(content="From the v8 directory.", scopes=["tools"])
    imported = memory.model_copy(update={"id": generate_ulid()})
    store.put_memory(imported, provenance="imported")
    row = record_history(store, imported.id).rows[0]
    assert (row.action, row.detail) == ("imported", {"provenance": "imported"})


def test_rows_under_a_key_this_host_lost_are_unverifiable(
    store_dir: Path, keys_dir: Path
) -> None:
    """A store rekeyed after its key went missing: the rows the lost key
    signed cannot be checked (null), the rows after the rekey can."""
    path = store_dir / STORE_FILENAME
    first = Store.create(path, keys_dir=keys_dir)
    before = first.write(content="Written under the first key.", scopes=["tools"])
    first.close()
    for key in keys_dir.glob("*.key"):
        key.unlink()
    rekeyed = Store.open(path, keys_dir=keys_dir, allow_rekey=True)
    try:
        after = rekeyed.write(content="Written under the new key.", scopes=["tools"])
        assert [row.verified for row in record_history(rekeyed, before.id).rows] == [
            None
        ]
        assert [row.verified for row in record_history(rekeyed, after.id).rows] == [
            True
        ]
        # Unverifiable is not a finding: the key is gone, not the row.
        assert record_history(rekeyed, before.id).exit_code == 0
    finally:
        rekeyed.close()


def test_a_record_deleted_outside_the_store_reads_missing(
    store: Store, store_dir: Path
) -> None:
    memory = store.write(content="Deleted with sqlite3.", scopes=["tools"])
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute("DELETE FROM memories WHERE id = ?", (memory.id,))
    outside.commit()
    outside.close()
    history = record_history(store, memory.id)
    assert history.state == "missing"
    assert history.exit_code == 1


def test_printable_neutralises_terminal_controls() -> None:
    assert printable("ok\x1b[31mred‮\n") == "ok�[31mred��"
    assert printable("a​zero-width") == "a�zero-width"
    assert printable("tab\tkept, é kept") == "tab\tkept, é kept"
    # A lone surrogate (a `\ud800` escape in stored JSON) would make a
    # strict UTF-8 stdout raise instead of printing the report.
    assert printable("a\ud800b") == "a�b"


def test_text_output_never_carries_a_raw_escape(store: Store) -> None:
    memory = store.write(content="Plain body.", scopes=["tools"])
    store.tombstone(memory.id, "evil \x1b]0;title\x07 reason")
    text = render_text(record_history(store, memory.id), diff=True)
    assert "\x1b" not in text and "\x07" not in text


def test_the_header_is_made_printable_too(store: Store, store_dir: Path) -> None:
    """The provenance label the header names is the table's own column.
    An editor can set it; the record's pointer into the log still
    verifies, so the label comes back as written."""
    memory = store.write(content="Plain body.", scopes=["tools"])
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute(
        "UPDATE memories SET provenance = ? WHERE id = ?",
        ("local\x1b]0;title\x07", memory.id),
    )
    outside.commit()
    outside.close()
    text = render_text(record_history(store, memory.id))
    assert "\x1b" not in text and "\x07" not in text
    assert "  provenance local�]0;title�\n" in text


def test_a_prune_committed_mid_read_does_not_read_as_tampering(
    store: Store, store_dir: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon writes while a history is read. An episode pruned
    between the log scan and the table lookup must not read as a record
    the log puts and no table holds: the whole read is one snapshot."""
    import bettermemory.history as history_module

    episode = store.write_episode(session_id="s-9", body="tried the fold first")
    scan = history_module._rows_naming

    def scan_then_prune(
        s: Store, record_id: str, *, kinds: tuple[str, ...] = ()
    ) -> list[Any]:
        rows = scan(s, record_id, kinds=kinds)
        daemon = Store.open(store_dir / STORE_FILENAME, keys_dir=keys_dir)
        try:
            daemon.prune_episode_sessions(ttl_days=1, now=utcnow() + timedelta(days=3))
        finally:
            daemon.close()
        return rows

    monkeypatch.setattr(history_module, "_rows_naming", scan_then_prune)
    raced = record_history(store, episode.id)
    assert (raced.state, raced.exit_code) == ("active", 0)
    monkeypatch.setattr(history_module, "_rows_naming", scan)
    # Read again, after the prune committed: it shows.
    assert record_history(store, episode.id).state == "pruned"
    assert not store.conn.in_transaction


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_prints_the_history_and_exits_0(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_id = _life(store)
    assert _run_cli(["memory", "history", record_id], monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"bettermemory memory history {record_id}: memory, active")
    assert "memory_put created [mac ok] author client=claude-code" in out
    assert "update update [mac ok] actor client=cursor" in out
    assert "changed body,scopes" in out
    assert (
        _run_cli(["memory", "history", record_id, "--json", "--diff"], monkeypatch) == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["rows"][0]["diff"].startswith("--- (none)")


def test_cli_refuses_an_unknown_or_malformed_id(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(["memory", "history", generate_ulid()], monkeypatch) == 2
    assert "no record" in capsys.readouterr().err
    assert _run_cli(["memory", "history", "not-an-id"], monkeypatch) == 2
    assert "not a record id" in capsys.readouterr().err


def test_cli_refuses_a_directory_without_a_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["memory", "history", generate_ulid()], monkeypatch) == 2
    assert STORE_FILENAME in capsys.readouterr().err


def test_cli_reads_a_keyless_store_without_rekeying(
    store: Store,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_id = _life(store)
    rows_before = len(store.log_rows())
    store.close()
    for key in keys_dir.glob("*.key"):
        key.unlink()
    assert _run_cli(["memory", "history", record_id, "--json"], monkeypatch) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["key_present"] is False
    assert {row["verified"] for row in data["rows"]} == {None}
    reopened = Store.open(store.path, allow_rekey=False)
    try:
        assert len(reopened.log_rows()) == rows_before
        assert not list(keys_dir.glob("*.key"))
    finally:
        reopened.close()
    assert _run_cli(["memory", "history", record_id], monkeypatch) == 0
    assert "key not held here" in capsys.readouterr().out


def test_cli_exits_1_on_a_tampered_history(
    store: Store,
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory = store.write(content="The deploy runs nightly.", scopes=["tools"])
    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute("DELETE FROM memories WHERE id = ?", (memory.id,))
    outside.commit()
    outside.close()
    assert _run_cli(["memory", "history", memory.id], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "findings" in out and "deleted outside the store" in out


def test_memory_without_a_subcommand_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["memory"], monkeypatch) == 2
    out = capsys.readouterr().out
    assert "move" in out and "history" in out


# ---------------------------------------------------------------------------
# The tools' own rows
# ---------------------------------------------------------------------------


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def test_the_tool_calls_rows_are_read_as_events(memory_dir: Path) -> None:
    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )
    written = await _call(
        server,
        "memory_write",
        content="The build uses uv for locking.",
        scopes=["tools"],
    )
    record_id = written["id"]
    await _call(
        server, "memory_update", id=record_id, content="The build uses uv lock files."
    )
    await _call(server, "memory_verify", id=record_id)
    await _call(server, "memory_remove", id=record_id, reason="moved to the docs")
    await _call(server, "memory_admin", action="restore", id=record_id)
    history = record_history(store, record_id)
    events = [row.action for row in history.rows if row.role == EVENT]
    assert events == ["write", "update", "verify", "remove", "restore"]
    remove = next(row for row in history.rows if row.action == "remove")
    assert remove.detail["reason"] == "moved to the docs"
    records = [row.action for row in history.rows if row.role == RECORD]
    assert records[0] == "created"
    assert "updated" in records and "verified" in records
    assert records[-1] == "restored"
    assert history.exit_code == 0
    store.close()
