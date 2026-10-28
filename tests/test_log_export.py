"""`bettermemory log export`: the signed log as JSON Lines for a SIEM.

The export is a trail, not a verdict: one line per log row, in seq order,
carrying who changed what and when, the row's MAC and the MAC it chains
to, the segment's key and whether the row's MAC verifies under a key this
host holds. These tests pin the line schema against the log it reads, the
default redaction of bodies and other free text, the incremental ranges a
cron job uses, determinism, the read-only open (no rekey, no write) and
the output file's mode.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import audit_export
from bettermemory.audit import MissHit
from bettermemory.audit_export import (
    AUDIT_SCHEMA,
    REDACTED_FIELDS,
    export,
    export_lines,
    record_actor,
    record_ids,
    redact_payload,
    redact_text,
    row_actor,
    row_class,
    utc_ts,
)
from bettermemory.identity import Actor
from bettermemory.models import (
    Confidence,
    Episode,
    LinkType,
    Memory,
    MemoryLink,
    Source,
    generate_ulid,
)
from bettermemory.store import STORE_FILENAME, Store

LINE_KEYS = {
    "schema",
    "store_id",
    "seq",
    "ts",
    "session",
    "kind",
    "class",
    "actor",
    "record_actor",
    "record_ids",
    "payload",
    "mac",
    "prev_mac",
    "key_fingerprint",
    "verified",
}

BODY = "the staging ledger listens on port 6543 behind the bastion"
TAKEAWAY = "ledger port settled at 6543"
EPISODE_BODY = "walked the ledger config and found the bastion rule"
REASON = "superseded once the ledger moved to the new bastion"
CLAIM = "ledger/settings.py::LEDGER_PORT=6543"
LINK_NOTE = "the bastion note ties these two ledger records"
SAID = "always route the ledger through the bastion from now on"
VERIFY_NOTE = "checked the ledger config by hand against the bastion"


def _memory(body: str = "alpha", **fields: Any) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
        **fields,
    )


def _episode(body: str, takeaway: str | None = None) -> Episode:
    return Episode(
        id=generate_ulid(),
        session_id="sess_a",
        created=datetime.now(timezone.utc),
        body=body,
        takeaway=takeaway,
    )


def _outside(store: Store) -> sqlite3.Connection:
    """The editor with sqlite3 and no key."""
    conn = sqlite3.connect(str(store.path))
    conn.row_factory = sqlite3.Row
    return conn


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


@pytest.fixture
def cli_store(memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The test's store, resolved by the CLI through BETTERMEMORY_DIR."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    return store


def _populate(store: Store) -> dict[str, Any]:
    """A log with a row of each family: a put with an actor, a link note
    and a claim, telemetry with an actor and the user's words, a
    tombstone with its reason, an episode with a takeaway, a conflict."""
    other = _memory("the other ledger record")
    store.put_memory(other)
    memory = _memory(
        BODY,
        actor=Actor(client="claude-code", model="opus"),
        claims=[CLAIM],
        links=[MemoryLink(type=LinkType.EXTENDS, target_id=other.id, note=LINK_NOTE)],
    )
    store.put_memory(memory)
    store.record_event(
        "show", session="sess_a", id=memory.id, actor={"client": "claude-code"}
    )
    store.record_event("standing_guard", session="sess_a", said=SAID, standing=[])
    store.record_event(
        "write",
        session="sess_a",
        id=memory.id,
        review_answers={"said": SAID, "unsafe": 0.0},
    )
    store.record_event("verify", session="sess_a", id=memory.id, note=VERIFY_NOTE)
    store.put_conflict(
        {
            "id": "c_1",
            "a_id": memory.id,
            "b_id": other.id,
            "summary_a": BODY,
            "summary_b": "the other ledger record",
            "similarity": 0.9,
            "method": "jaccard",
            "detector": "write",
            "created": "2026-10-01T00:00:00Z",
            "status": "open",
        }
    )
    store.tombstone(memory.id, REASON, session="sess_a")
    store.record_event("remove", session="sess_a", id=memory.id, reason=REASON)
    episode = _episode(EPISODE_BODY, TAKEAWAY)
    store.put_episode(episode)
    store.record_event("search", session="sess_a", query="ledger", returned=[other.id])
    return {"memory": memory, "other": other, "episode": episode}


# ---------------------------------------------------------------------------
# The line schema against the log
# ---------------------------------------------------------------------------


def test_every_row_is_one_line_in_seq_order_carrying_its_chain(store: Store) -> None:
    _populate(store)
    rows = store.log_rows()
    lines = export_lines(store)
    assert [line["seq"] for line in lines] == [row.seq for row in rows]
    for line, row in zip(lines, rows):
        assert set(line) == LINE_KEYS
        assert line["schema"] == AUDIT_SCHEMA
        assert line["store_id"] == store.store_id
        assert line["kind"] == row.kind
        assert line["session"] == row.session
        assert line["ts"] == row.ts
        assert line["mac"] == row.mac
        assert line["prev_mac"] == row.prev_mac
        assert line["key_fingerprint"] == store.key_fingerprint
        assert line["verified"] is True
    # The chain is followable from the lines alone.
    for earlier, later in zip(lines, lines[1:]):
        assert later["prev_mac"] == earlier["mac"]


def test_each_line_names_its_class_records_and_actors(store: Store) -> None:
    made = _populate(store)
    memory, other, episode = made["memory"], made["other"], made["episode"]
    lines = export_lines(store)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        by_kind.setdefault(line["kind"], []).append(line)

    assert by_kind["store_created"][0]["class"] == "control"
    assert by_kind["store_created"][0]["record_ids"] == []
    put = by_kind["memory_put"][1]
    assert put["class"] == "mutation"
    assert put["record_ids"] == [memory.id]
    assert memory.actor is not None
    assert put["record_actor"] == memory.actor.model_dump(mode="json")
    assert put["actor"] is None
    show = by_kind["show"][0]
    assert show["class"] == "telemetry"
    assert show["actor"] == {"client": "claude-code"}
    assert show["record_actor"] is None
    assert show["record_ids"] == [memory.id]
    assert by_kind["conflict_put"][0]["record_ids"] == [memory.id, other.id]
    assert by_kind["memory_delete"][0]["record_ids"] == [memory.id]
    tomb = by_kind["tombstone_put"][0]
    assert tomb["record_ids"] == [memory.id]
    assert tomb["record_actor"] == put["record_actor"]
    assert by_kind["episode_put"][0]["record_ids"] == [episode.id]
    # A search's results stay in the payload; they are not the row's records.
    assert by_kind["search"][0]["record_ids"] == []
    assert by_kind["search"][0]["payload"]["returned"] == [other.id]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("memory_put", "mutation"),
        ("home_put", "mutation"),
        ("standing_delete", "mutation"),
        ("store_created", "control"),
        ("rekey", "control"),
        ("capture_member", "control"),
        ("resolution_joined", "membership"),
        ("store_forked", "membership"),
        ("search", "telemetry"),
        ("some_future_event", "telemetry"),
    ],
)
def test_row_class_by_kind(kind: str, expected: str) -> None:
    assert row_class(kind) == expected


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        ("memory_put", {"memory": {"id": "m1"}}, ["m1"]),
        ("tombstone_put", {"tombstone": {"id": "m1"}}, ["m1"]),
        ("episode_put", {"episode": {"id": "e1"}}, ["e1"]),
        ("memory_delete", {"id": "m1"}, ["m1"]),
        ("tombstone_delete", {"id": "m1"}, ["m1"]),
        ("episode_delete", {"id": "e1"}, ["e1"]),
        ("verification_put", {"memory_id": "m1", "machine_id": "x"}, ["m1"]),
        ("verification_delete", {"memory_id": "m1"}, ["m1"]),
        ("import_put", {"source": "a.md", "memory_id": "m1"}, ["m1"]),
        ("import_put", {"source": "a.md", "memory_id": None}, []),
        (
            "conflict_put",
            {"conflict": {"id": "c", "a_id": "m1", "b_id": "m2"}},
            ["m1", "m2"],
        ),
        ("conflict_delete", {"id": "c"}, []),
        ("home_put", {"home": {"record_id": "m1", "project": "p"}}, ["m1"]),
        ("home_delete", {"record_id": "m1"}, ["m1"]),
        ("standing_put", {"standing": {"record_id": "m1"}}, ["m1"]),
        ("standing_delete", {"record_id": "m1"}, ["m1"]),
        ("project_put", {"project": {"id": "p1"}}, []),
        ("capture_member", {"memory_id": "m1", "batch": "b"}, ["m1"]),
        ("show", {"id": "m1"}, ["m1"]),
        ("show", {"id": "m1", "memory_id": "m1", "record_id": "m2"}, ["m1", "m2"]),
        ("search", {"returned": ["m1"]}, []),
        ("show", {"id": 7}, []),
        ("memory_put", {"memory": "not a record"}, []),
        ("show", ["not", "an", "object"], []),
    ],
)
def test_record_ids_by_kind(kind: str, payload: Any, expected: list[str]) -> None:
    assert record_ids(kind, payload) == expected


def test_actor_blocks_come_from_the_row_or_its_record() -> None:
    actor = {"client": "c"}
    assert row_actor({"actor": actor}) == actor
    assert row_actor({"actor": "not a block"}) is None
    assert row_actor(None) is None
    assert record_actor("memory_put", {"memory": {"actor": actor}}) == actor
    assert record_actor("tombstone_put", {"tombstone": {"actor": actor}}) == actor
    assert record_actor("episode_put", {"episode": {"actor": actor}}) is None
    assert record_actor("memory_put", {"memory": {"id": "m"}}) is None
    assert record_actor("memory_put", {"memory": "x"}) is None
    assert record_actor("memory_put", None) is None


def test_stamps_are_utc_with_a_z(store: Store) -> None:
    """A store stamp is kept byte for byte; an imported v8 event's offset
    stamp is rendered in UTC."""
    assert utc_ts("2026-09-26T00:00:00.000Z") == "2026-09-26T00:00:00.000Z"
    assert utc_ts("2025-01-01T01:30:00+01:00") == "2025-01-01T00:30:00Z"
    assert utc_ts("2025-01-01T00:30:00") == "2025-01-01T00:30:00Z"
    assert utc_ts("not a stamp") == "not a stamp"
    store.import_event({"kind": "search", "ts": "2025-01-01T01:30:00+01:00"})
    assert export_lines(store)[-1]["ts"] == "2025-01-01T00:30:00Z"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _all_text(lines: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)


def test_bodies_and_free_text_are_redacted_by_default(store: Store) -> None:
    _populate(store)
    lines = export_lines(store)
    text = _all_text(lines)
    for secret in (BODY, TAKEAWAY, EPISODE_BODY, REASON, CLAIM, LINK_NOTE, SAID):
        assert secret not in text, secret
    assert VERIFY_NOTE not in text
    # A stand-in carries the hash of the text it replaces, so a consumer
    # holding the body can still match it.
    assert redact_text(BODY + "\n")["sha256"] in text
    put = next(
        line
        for line in lines
        if line["kind"] == "memory_put" and line["record_actor"] is not None
    )
    memory = put["payload"]["memory"]
    assert memory["body"] == {
        "sha256": hashlib.sha256((BODY + "\n").encode()).hexdigest(),
        "len": len(BODY) + 1,
    }
    assert memory["claims"] == [redact_text(CLAIM)]
    assert memory["links"][0]["note"] == redact_text(LINK_NOTE)
    # What an audit is about stays: ids, scopes, provenance, the link.
    assert memory["scopes"] == ["tools"]
    assert memory["links"][0]["type"] == "extends"
    assert put["payload"]["provenance"] == "local"
    # Queries are redacted in the log itself and pass through as stored.
    search = next(line for line in lines if line["kind"] == "search")
    assert set(search["payload"]["query"]) == {"hash", "preview", "len"}


def test_body_and_reply_text_in_telemetry_rows_is_redacted_by_default(
    store: Store,
) -> None:
    """The telemetry rows carry pieces of bodies and replies under names
    of their own: the body window of each recall or miss hit, the reply
    sentence a use names, the body phrases a refused write quotes. Each
    is free text and leaves the host only as its hash."""
    snippet = "the staging ledger listens on 6543 behind the bastion"
    excerpt = "so I routed the ledger through the bastion as agreed"
    phrase = "I prefer the ledger on 6543"
    hit = MissHit(
        id="m1", score=1.0, relevance="high", scopes=("tools",), snippet=snippet
    )
    for kind in ("prompt_recall", "recall_held", "search_miss"):
        store.record_event(kind, session="sess_a", top_hits=[hit.to_dict()])
    store.record_event("use", session="sess_a", ids=["m1"], claim_excerpts=[excerpt])
    store.record_event(
        "update", session="sess_a", id="m1", claim_phrases=[phrase, phrase]
    )
    lines = export_lines(store)
    text = _all_text(lines)
    for secret in (snippet, excerpt, phrase):
        assert secret not in text, secret
    recall = next(line for line in lines if line["kind"] == "prompt_recall")
    (shown,) = recall["payload"]["top_hits"]
    assert shown["snippet"] == redact_text(snippet)
    assert (shown["id"], shown["relevance"]) == ("m1", "high")
    use = next(line for line in lines if line["kind"] == "use")
    assert use["payload"]["claim_excerpts"] == [redact_text(excerpt)]
    assert use["payload"]["ids"] == ["m1"]
    update = next(line for line in lines if line["kind"] == "update")
    assert update["payload"]["claim_phrases"] == [redact_text(phrase)] * 2
    kept = _all_text(export_lines(store, include_bodies=True))
    for secret in (snippet, excerpt, phrase):
        assert secret in kept, secret


def test_a_query_still_in_text_is_redacted_and_its_stand_in_passes() -> None:
    stand_in = {"hash": "ab" * 8, "preview": "ledger", "len": 6}
    out = redact_payload({"query": "ledger port", "probe_query": stand_in})
    assert out == {"query": redact_text("ledger port"), "probe_query": stand_in}


def test_include_bodies_keeps_every_payload_as_the_log_holds_it(store: Store) -> None:
    _populate(store)
    lines = export_lines(store, include_bodies=True)
    text = _all_text(lines)
    for secret in (BODY, TAKEAWAY, EPISODE_BODY, REASON, CLAIM, LINK_NOTE, SAID):
        assert secret in text, secret
    for line, row in zip(lines, store.log_rows()):
        assert line["payload"] == json.loads(row.payload)


def test_redaction_reaches_nested_fields_and_spares_non_text() -> None:
    payload = {
        "review_answers": {"said": "words", "unsafe": 0.0},
        "review": {"said": True},
        "said": None,
        "note": ["a", 1, {"body": "inner"}],
        "items": [{"reason": "cap", "id": "m1"}],
        "id": "m1",
    }
    out = redact_payload(payload)
    assert out["review_answers"] == {"said": redact_text("words"), "unsafe": 0.0}
    assert out["review"] == {"said": True}
    assert out["said"] is None
    assert out["note"] == [redact_text("a"), 1, {"body": redact_text("inner")}]
    assert out["items"] == [{"reason": redact_text("cap"), "id": "m1"}]
    assert out["id"] == "m1"
    assert {"body", "takeaway", "removed_reason", "said"} <= REDACTED_FIELDS


def test_a_payload_that_is_not_an_object_is_redacted_whole(store: Store) -> None:
    store.record_event("search", session="sess_a", query="alpha")
    seq = store.log_rows()[-1].seq
    outside = _outside(store)
    outside.execute("UPDATE log SET payload = ? WHERE seq = ?", ("not json", seq))
    outside.execute("UPDATE log SET payload = ? WHERE seq = 1", ("[1, 2]",))
    outside.commit()
    outside.close()
    lines = export_lines(store)
    assert lines[-1]["payload"] == redact_text("not json")
    assert lines[-1]["record_ids"] == [] and lines[-1]["actor"] is None
    assert lines[0]["payload"] == redact_text("[1, 2]")
    kept = export_lines(store, include_bodies=True)
    assert kept[-1]["payload"] == "not json"
    assert kept[0]["payload"] == [1, 2]


# ---------------------------------------------------------------------------
# What verified says
# ---------------------------------------------------------------------------


def test_an_edited_row_exports_as_not_verified(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    store.record_event("search", session="sess_a", query="alpha")
    outside = _outside(store)
    outside.execute("UPDATE log SET session = 'sess_forged' WHERE seq = 3")
    outside.commit()
    outside.close()
    verified = {line["seq"]: line["verified"] for line in export_lines(store)}
    assert verified == {1: True, 2: True, 3: False}


def test_a_hand_edited_row_reads_unverified_and_the_export_goes_on(
    store: Store,
) -> None:
    """A row edited into a shape the store never writes is one unverified
    line, not the end of the export: a MAC column holding non-ASCII text,
    a payload nested past what the parser recurses through, and one
    nested past what the redaction walk does."""
    for n in range(4):
        store.record_event("show", session="sess_a", id=f"m{n}")
    rows = store.log_rows()
    deep = 100_000
    walk_deep = sys.getrecursionlimit()
    outside = _outside(store)
    outside.execute("UPDATE log SET mac = ? WHERE seq = ?", ("é" * 64, 2))
    outside.execute(
        "UPDATE log SET payload = ? WHERE seq = ?",
        ('{"a":' * deep + "1" + "}" * deep, 3),
    )
    outside.execute(
        "UPDATE log SET payload = ? WHERE seq = ?",
        ('{"a":' * walk_deep + "1" + "}" * walk_deep, 4),
    )
    outside.commit()
    outside.close()
    edited = {row.seq: row for row in store.log_rows()}

    lines = export_lines(store)
    assert [line["seq"] for line in lines] == [row.seq for row in rows]
    verified = {line["seq"]: line["verified"] for line in lines}
    assert verified[2] is False and verified[3] is False and verified[4] is False
    assert all(verified[row.seq] for row in rows if row.seq not in (2, 3, 4))
    assert lines[1]["mac"] == "é" * 64
    for seq in (3, 4):
        assert lines[seq - 1]["payload"] == redact_text(edited[seq].payload)
        assert lines[seq - 1]["record_ids"] == []
    (kept,) = export_lines(store, since_seq=2, until_seq=3, include_bodies=True)
    assert kept["payload"] == edited[3].payload


def test_a_seq_past_sqlites_range_is_refused(store: Store) -> None:
    with pytest.raises(ValueError, match="at most"):
        export_lines(store, until_seq=audit_export.MAX_SEQ + 1)
    with pytest.raises(ValueError, match="at most"):
        export_lines(store, since_seq=audit_export.MAX_SEQ + 1)
    assert export_lines(store, since_seq=audit_export.MAX_SEQ) == []


def test_a_rekeyed_log_names_each_segment_and_verifies_with_retired_keys(
    store: Store, keys_dir: Path
) -> None:
    store.put_memory(_memory("alpha"))
    old_fingerprint = store.key_fingerprint
    path = store.path
    store.close()
    key_file = keys_dir / f"{store.store_id}.key"
    key = key_file.read_bytes()
    key_file.unlink()
    rekeyed = Store.open(path, keys_dir=keys_dir, allow_rekey=True)
    try:
        rekeyed.put_memory(_memory("beta"))
        lines = export_lines(rekeyed)
        kinds = [line["kind"] for line in lines]
        rekey_at = kinds.index("rekey")
        new_fingerprint = rekeyed.key_fingerprint
        assert new_fingerprint != old_fingerprint
        # The old key is gone from this host: its segment reads null.
        for line in lines[:rekey_at]:
            assert line["key_fingerprint"] == old_fingerprint
            assert line["verified"] is None
        for line in lines[rekey_at:]:
            assert line["key_fingerprint"] == new_fingerprint
            assert line["verified"] is True
        # Filed back as a retired key, it verifies its segment again.
        rekeyed.keyring.retired_path(old_fingerprint).write_bytes(key)
        assert all(line["verified"] is True for line in export_lines(rekeyed))
    finally:
        rekeyed.close()


def test_a_rekey_row_that_names_no_key_reads_null() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT, session TEXT, "
        "kind TEXT, payload TEXT, prev_mac TEXT, mac TEXT)"
    )
    rows = [
        (1, "store_created", "{}"),
        (2, "rekey", "garbage"),
        (3, "search", "{}"),
        (4, "rekey", json.dumps({"old_fingerprint": "x", "new_fingerprint": "b"})),
        (5, "search", "{}"),
    ]
    conn.executemany(
        "INSERT INTO log VALUES (?, 't', NULL, ?, ?, '', '')",
        rows,
    )
    segments = audit_export.segment_map(conn, current_fingerprint="cur")
    # The first rekey row names nothing, so the first segment keeps the
    # store's current key, as `log.verify_chain` reads it.
    assert [segments.fingerprint_for(seq) for seq in range(1, 6)] == [
        "cur",
        None,
        None,
        "b",
        "b",
    ]
    conn.close()


def test_export_never_rekeys_or_writes_a_keyless_store(
    cli_store: Store,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_store.put_memory(_memory("alpha"))
    before = cli_store.log_rows()
    (keys_dir / f"{cli_store.store_id}.key").unlink()
    keys_before = sorted(p.name for p in keys_dir.iterdir())

    assert _run_cli(["log", "export"], monkeypatch) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == len(before)
    assert all(line["verified"] is None for line in lines)
    assert cli_store.log_rows() == before
    assert sorted(p.name for p in keys_dir.iterdir()) == keys_before


# ---------------------------------------------------------------------------
# Ranges, kinds, determinism, the snapshot
# ---------------------------------------------------------------------------


def test_incremental_ranges_and_kinds(store: Store) -> None:
    _populate(store)
    last = store.log_rows()[-1].seq
    assert [line["seq"] for line in export_lines(store, since_seq=5)] == list(
        range(6, last + 1)
    )
    assert [line["seq"] for line in export_lines(store, until_seq=3)] == [1, 2, 3]
    assert [line["seq"] for line in export_lines(store, since_seq=2, until_seq=4)] == [
        3,
        4,
    ]
    assert export_lines(store, since_seq=last) == []
    assert export_lines(store, since_seq=3, until_seq=3) == []
    kinds = {line["kind"] for line in export_lines(store, kinds=["show", "rekey"])}
    assert kinds == {"show"}
    assert export_lines(store, kinds=[]) == []
    with pytest.raises(ValueError):
        export_lines(store, since_seq=-1)
    with pytest.raises(ValueError):
        export_lines(store, since_seq=4, until_seq=3)


def test_the_summary_gives_the_next_cursor(store: Store) -> None:
    _populate(store)
    last = store.log_rows()[-1].seq
    out: list[str] = []
    summary = export(store, out.append, since_seq=2, kinds=["show"])
    assert summary.rows == len(out) == 1
    assert summary.first_seq == summary.last_seq == 4
    # The cursor moves to the end of what was read, not to the last row
    # the kind filter let through.
    assert summary.through_seq == summary.log_last_seq == last
    assert export(store, out.append, until_seq=2).through_seq == 2
    nothing = export(store, out.append, since_seq=last + 10)
    assert (nothing.rows, nothing.through_seq) == (0, last + 10)
    assert nothing.to_dict()["first_seq"] is None


def test_the_export_is_deterministic(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _populate(cli_store)
    assert _run_cli(["log", "export"], monkeypatch) == 0
    first = capsys.readouterr().out
    assert _run_cli(["log", "export"], monkeypatch) == 0
    assert capsys.readouterr().out == first
    assert first.isascii()
    assert first.count("\n") == len(cli_store.log_rows())


def test_the_export_reads_one_snapshot_while_another_connection_writes(
    store: Store,
) -> None:
    """A writer that commits while the export streams does not change
    what the export holds: one read transaction serves every row."""
    store.put_memory(_memory("alpha"))
    rows_before = len(store.log_rows())
    writer = Store.open(store.path, keys_dir=store.keys_dir)
    written: list[str] = []

    def write(line: str) -> None:
        if not written:
            writer.put_memory(_memory("written meanwhile"))
        written.append(line)

    try:
        summary = export(store, write)
    finally:
        writer.close()
    assert summary.rows == rows_before == len(written)
    assert len(store.log_rows()) == rows_before + 1


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_cli_out_writes_an_owner_only_file_and_a_summary(
    cli_store: Store,
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _populate(cli_store)
    last = cli_store.log_rows()[-1].seq
    out = memory_dir.parent / "audit.jsonl"
    assert _run_cli(["log", "export", "--out", str(out)], monkeypatch) == 0
    printed = capsys.readouterr().out
    assert f"{last} row(s), seq 1 to {last}" in printed
    assert f"next --since-seq {last}" in printed
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["seq"] for line in lines] == list(range(1, last + 1))
    assert not list(out.parent.glob("audit.jsonl.*.tmp"))

    # A rerun replaces the file; --json prints the summary as JSON.
    argv = ["log", "export", "--since-seq", str(last - 2), "--out", str(out), "--json"]
    assert _run_cli(argv, monkeypatch) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 2
    assert summary["through_seq"] == last
    assert summary["out"] == str(out)
    assert summary["store_id"] == cli_store.store_id
    assert len(out.read_text().splitlines()) == 2

    # Nothing new: an empty file and the same cursor.
    assert (
        _run_cli(
            ["log", "export", "--since-seq", str(last), "--out", str(out)], monkeypatch
        )
        == 0
    )
    assert f"no rows to export after seq {last}" in capsys.readouterr().out
    assert out.read_text() == ""


def test_cli_kinds_and_ranges(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _populate(cli_store)
    argv = ["log", "export", "--kinds", " show , search ,", "--until-seq", "4"]
    assert _run_cli(argv, monkeypatch) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["seq"], line["kind"]) for line in lines] == [(4, "show")]


def test_cli_include_bodies_warns_on_stderr(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _populate(cli_store)
    assert _run_cli(["log", "export", "--include-bodies"], monkeypatch) == 0
    captured = capsys.readouterr()
    assert BODY in captured.out
    assert "--include-bodies" in captured.err
    assert BODY not in captured.err


def test_cli_warns_when_the_cursor_is_past_the_log(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    last = cli_store.log_rows()[-1].seq
    assert _run_cli(["log", "export", "--since-seq", str(last + 5)], monkeypatch) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"the log ends at seq {last}" in captured.err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--since-seq", "-1"], "--since-seq"),
        (["--since-seq", "4", "--until-seq", "3"], "--until-seq"),
        (["--kinds", " , "], "--kinds"),
        (["--out", "missing-dir/audit.jsonl"], "no directory"),
        (["--out", "."], "is a directory"),
        (["--until-seq", str(2**63)], "at most"),
        (["--since-seq", str(2**63)], "at most"),
    ],
)
def test_cli_refuses_bad_arguments(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    assert _run_cli(["log", "export", *argv], monkeypatch) == 2
    assert message in capsys.readouterr().err


def test_cli_refuses_a_directory_without_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["log", "export"], monkeypatch) == 2
    assert STORE_FILENAME in capsys.readouterr().err
    assert not (tmp_path / STORE_FILENAME).exists()


def test_cli_exits_1_when_the_reader_goes_away(
    cli_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ClosedPipe(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert _run_cli(["log", "export"], monkeypatch) == 1


def test_a_closed_stdout_descriptor_is_pointed_at_devnull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the reader goes away, the descriptor behind stdout is
    redirected, so the interpreter's flush at exit writes nowhere instead
    of failing again."""
    from bettermemory.cli.log_cmd import _blackhole_stdout

    target = tmp_path / "stdout.txt"
    with target.open("w") as handle:
        monkeypatch.setattr(sys, "stdout", handle)
        _blackhole_stdout()
        handle.write("swallowed\n")
        handle.flush()
    assert target.read_text() == ""


def test_a_failed_out_write_leaves_no_file_behind(
    cli_store: Store,
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = memory_dir.parent / "audit.jsonl"
    out.write_text("previous export\n")

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(audit_export, "dumps_line", failing)
    assert _run_cli(["log", "export", "--out", str(out)], monkeypatch) == 2
    assert "cannot write" in capsys.readouterr().err
    assert out.read_text() == "previous export\n"
    assert not list(out.parent.glob("audit.jsonl.*.tmp"))


def test_log_help_lists_the_new_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["log"], monkeypatch) == 2
    out = capsys.readouterr().out
    assert "{verify,export,anchor}" in out
    assert _run_cli(["log", "export", "--help"], monkeypatch) == 0
    help_text = capsys.readouterr().out
    for flag in ("--since-seq", "--until-seq", "--kinds", "--include-bodies", "--out"):
        assert flag in help_text
    assert "Exit 0" in help_text
