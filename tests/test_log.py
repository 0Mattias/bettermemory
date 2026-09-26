"""The hash-chained log of the bettermemory 9 store (unit U2).

Every mutation and every telemetry event is one row of `log`, MAC'd with
a key kept outside the store and chained to the row before it; a head
checkpoint beside the key records the last row. The tests here pin the
primitives, the key and head handling, and the four tamper cases the
phase 1 declaration predicts `bettermemory log verify` reports: an edited
row, a row inserted without the key, a deleted tail, and a table edit
that no log row accounts for.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import log as chain
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import Store


def _memory(body: str, *scopes: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=list(scopes) or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def store(tmp_path: Path, keys_dir: Path) -> Iterator[Store]:
    s = Store.create(tmp_path / "memory.sqlite", keys_dir=keys_dir)
    yield s
    s.close()


def _outside(store: Store) -> sqlite3.Connection:
    """A second connection to the store file: the editor with sqlite3 and
    no key, which is the threat the chain exists to expose."""
    conn = sqlite3.connect(str(store.path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_canonical_payload_sorts_keys_and_is_compact() -> None:
    text = chain.canonical_payload({"b": 1, "a": [1, 2], "c": {"z": None, "y": "é"}})
    assert text == '{"a":[1,2],"b":1,"c":{"y":"é","z":null}}'


def test_compute_mac_is_deterministic_and_field_sensitive() -> None:
    key = b"k" * 32
    base: dict[str, Any] = dict(
        seq=1,
        ts="2026-09-26T00:00:00.000Z",
        session="sess_a",
        kind="memory_put",
        payload="{}",
        prev_mac=chain.GENESIS_MAC,
    )
    first = chain.compute_mac(key, **base)
    assert first == chain.compute_mac(key, **base)
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    for field, value in (
        ("seq", 2),
        ("ts", "2026-09-26T00:00:01.000Z"),
        ("session", "sess_b"),
        ("session", None),
        ("kind", "memory_delete"),
        ("payload", "{ }"),
        ("prev_mac", "ab" * 32),
    ):
        assert chain.compute_mac(key, **{**base, field: value}) != first, field
    assert chain.compute_mac(b"j" * 32, **base) != first


def test_fingerprint_is_sha256_of_the_key_bytes() -> None:
    import hashlib

    key = secrets.token_bytes(32)
    assert chain.fingerprint(key) == hashlib.sha256(key).hexdigest()


def test_genesis_mac_is_thirty_two_zero_bytes() -> None:
    assert chain.GENESIS_MAC == "00" * 32


def test_the_keys_dir_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scratch store (a bench, a demo, a spawned server) names its own
    keys directory through ``BETTERMEMORY_KEYS_DIR`` and leaves nothing
    under the user's config directory; unset, the config directory is
    the default. The chain module itself never reads the environment."""
    import inspect

    from bettermemory import store as store_module
    from bettermemory.config import KEYS_DIR_ENV

    monkeypatch.delenv(KEYS_DIR_ENV, raising=False)
    assert store_module.resolve_keys_dir() == chain.default_keys_dir()
    assert "environ" not in inspect.getsource(chain)
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert store_module.resolve_keys_dir() == tmp_path / "elsewhere"
    with Store.create(tmp_path / "memory.sqlite") as store:
        assert store.status()["keys_dir"] == str(tmp_path / "elsewhere")
    assert (tmp_path / "elsewhere").is_dir()


# ---------------------------------------------------------------------------
# Birth: the genesis row, the key, the head
# ---------------------------------------------------------------------------


def test_a_fresh_store_starts_its_chain_with_a_store_created_row(
    store: Store,
) -> None:
    rows = store.log_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.seq == 1
    assert row.kind == chain.STORE_CREATED
    assert row.prev_mac == chain.GENESIS_MAC
    payload = json.loads(row.payload)
    assert payload["store_id"] == store.store_id
    assert payload["schema_version"] == 1
    assert store.log_verify()["status"] == "ok"


def test_the_key_lives_outside_the_store_and_only_its_fingerprint_inside(
    store: Store, keys_dir: Path
) -> None:
    key_path = keys_dir / f"{store.store_id}.key"
    assert key_path.is_file()
    key = key_path.read_bytes()
    assert len(key) == 32
    assert store.meta()["key_fingerprint"] == chain.fingerprint(key)
    assert store.key_fingerprint == chain.fingerprint(key)
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert key not in store.path.read_bytes()
    assert key.hex().encode() not in store.path.read_bytes()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_key_file_and_its_directory_are_owner_only(
    store: Store, keys_dir: Path
) -> None:
    key_path = keys_dir / f"{store.store_id}.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_reopening_uses_the_same_key_and_appends_no_rekey_row(
    tmp_path: Path, keys_dir: Path
) -> None:
    path = tmp_path / "memory.sqlite"
    first = Store.create(path, keys_dir=keys_dir)
    first.put_memory(_memory("alpha"))
    fingerprint = first.key_fingerprint
    first.close()

    again = Store.open(path, keys_dir=keys_dir)
    try:
        assert again.key_fingerprint == fingerprint
        assert [r.kind for r in again.log_rows()] == [chain.STORE_CREATED, "memory_put"]
        again.put_memory(_memory("beta"))
        assert again.log_verify()["status"] == "ok"
    finally:
        again.close()


def test_the_head_follows_every_append_and_never_moves_backwards(
    store: Store,
) -> None:
    head = store.keyring.read_head()
    assert head is not None and head.seq == 1
    store.put_memory(_memory("one"))
    store.put_memory(_memory("two"))
    last = store.log_rows()[-1]
    head = store.keyring.read_head()
    assert head is not None
    assert (head.seq, head.mac) == (last.seq, last.mac)
    # A stale writer racing behind a newer one must not rewind the checkpoint.
    store.keyring.write_head(seq=1, mac="00" * 32)
    head = store.keyring.read_head()
    assert head is not None and head.seq == last.seq


def test_the_default_keys_dir_is_under_the_user_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import platformdirs

    monkeypatch.setattr(
        platformdirs, "user_config_dir", lambda name: str(tmp_path / "cfg" / name)
    )
    assert chain.default_keys_dir() == tmp_path / "cfg" / "bettermemory" / "keys"


# ---------------------------------------------------------------------------
# P7: the four tamper cases
# ---------------------------------------------------------------------------


def test_an_edited_row_fails_its_mac(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    store.put_memory(_memory("beta"))
    outside = _outside(store)
    outside.execute("UPDATE log SET session = 'sess_forged' WHERE seq = 2")
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert any(
        p["seq"] == 2 and p["problem"] == "mac_mismatch" for p in report["problems"]
    )


def test_an_edited_payload_is_reported_and_the_fold_survives_it(
    store: Store,
) -> None:
    store.put_memory(_memory("alpha"))
    outside = _outside(store)
    outside.execute("UPDATE log SET payload = '{}' WHERE seq = 2")
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    problems = {p["problem"] for p in report["problems"] if p["seq"] == 2}
    assert "mac_mismatch" in problems
    assert "payload_invalid" in problems


def test_a_row_inserted_without_the_key_is_reported(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    last = store.log_rows()[-1]
    outside = _outside(store)
    outside.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            last.seq + 1,
            "2026-09-26T00:00:00.000Z",
            None,
            "memory_delete",
            json.dumps({"id": memory.id}),
            last.mac,
            secrets.token_hex(32),
        ),
    )
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert any(
        p["seq"] == last.seq + 1 and p["problem"] == "mac_mismatch"
        for p in report["problems"]
    )


def test_a_deleted_tail_is_caught_by_the_head(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    store.put_memory(_memory("beta"))
    outside = _outside(store)
    outside.execute("DELETE FROM log WHERE seq > 2")
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert report["head"]["status"] == "tail_deleted"
    assert report["head"]["seq"] == 3
    assert report["rows"] == 2


def test_a_deleted_tail_with_matching_table_edits_is_still_caught(
    store: Store,
) -> None:
    """The attacker removes the row AND the log entry that wrote it, so the
    fold agrees with the tables; only the head outside the store knows."""
    store.put_memory(_memory("alpha"))
    beta = _memory("beta")
    store.put_memory(beta)
    outside = _outside(store)
    outside.execute("DELETE FROM memories WHERE id = ?", (beta.id,))
    outside.execute("DELETE FROM log WHERE seq > 2")
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["fold"]["status"] == "ok"
    assert report["head"]["status"] == "tail_deleted"
    assert report["status"] == "tampered"


def test_a_deleted_middle_row_breaks_the_chain(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    store.put_memory(_memory("beta"))
    outside = _outside(store)
    outside.execute("DELETE FROM log WHERE seq = 2")
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    problems = {(p["seq"], p["problem"]) for p in report["problems"]}
    assert (3, "chain_break") in problems
    assert (3, "seq_gap") in problems


def test_a_table_edit_without_a_log_row_is_unaccounted(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    outside = _outside(store)
    outside.execute("UPDATE memories SET body = 'edited' WHERE id = ?", (memory.id,))
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert report["fold"]["status"] == "diverged"
    assert report["fold"]["tables"]["memories"]["unaccounted"] == [memory.id]
    assert report["unaccounted_ids"] == [memory.id]
    assert store.provenance_for([memory.id]) == {memory.id: "unaccounted"}


def test_a_planted_row_is_unaccounted(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    planted = generate_ulid()
    outside = _outside(store)
    columns = [
        row["name"]
        for row in outside.execute("PRAGMA table_info(memories)")
        if row["name"] != "rowid"
    ]
    selected = ", ".join("?" if c == "id" else c for c in columns)
    outside.execute(
        f"INSERT INTO memories ({', '.join(columns)}) "
        f"SELECT {selected} FROM memories WHERE id = ?",
        (planted, memory.id),
    )
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert report["fold"]["tables"]["memories"]["unaccounted"] == [planted]
    assert report["fold"]["tables"]["memories"]["missing"] == []


def test_a_row_deleted_from_a_table_is_missing(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    outside = _outside(store)
    outside.execute("DELETE FROM memories WHERE id = ?", (memory.id,))
    outside.commit()
    outside.close()

    report = store.log_verify()
    assert report["status"] == "tampered"
    assert report["fold"]["tables"]["memories"]["missing"] == [memory.id]
    assert report["fold"]["tables"]["memories"]["unaccounted"] == []


def test_the_fold_covers_every_folded_table(store: Store) -> None:
    from bettermemory.store import FOLDED_TABLES

    report = store.log_verify()
    assert set(report["fold"]["tables"]) == set(FOLDED_TABLES)
    assert "pending_writes" not in report["fold"]["tables"]
    assert "log" not in report["fold"]["tables"]


# ---------------------------------------------------------------------------
# Keys that go missing, and keys that come back
# ---------------------------------------------------------------------------


def test_opening_without_the_key_rekeys_and_splits_the_chain(
    tmp_path: Path, keys_dir: Path
) -> None:
    path = tmp_path / "memory.sqlite"
    first = Store.create(path, keys_dir=keys_dir)
    first.put_memory(_memory("alpha"))
    old_fingerprint = first.key_fingerprint
    first.close()
    (keys_dir / f"{first.store_id}.key").unlink()

    again = Store.open(path, keys_dir=keys_dir)
    try:
        assert again.key_fingerprint != old_fingerprint
        rows = again.log_rows()
        assert rows[-1].kind == chain.REKEY
        payload = json.loads(rows[-1].payload)
        assert payload["old_fingerprint"] == old_fingerprint
        assert payload["new_fingerprint"] == again.key_fingerprint
        assert again.meta()["key_fingerprint"] == again.key_fingerprint
        again.put_memory(_memory("beta"))

        report = again.log_verify()
        assert report["status"] == "unverifiable"
        assert [s["status"] for s in report["segments"]] == ["unverifiable", "ok"]
        assert report["segments"][0]["key_fingerprint"] == old_fingerprint
        assert report["segments"][0]["last_seq"] == 2
        assert report["segments"][1]["first_seq"] == 3
        assert report["segments"][1]["key_fingerprint"] == again.key_fingerprint
        assert report["fold"]["status"] == "ok"
    finally:
        again.close()


def test_a_retired_key_that_is_present_verifies_its_segment(
    tmp_path: Path, keys_dir: Path
) -> None:
    path = tmp_path / "memory.sqlite"
    first = Store.create(path, keys_dir=keys_dir)
    first.put_memory(_memory("alpha"))
    old_fingerprint = first.key_fingerprint
    first.close()
    key_path = keys_dir / f"{first.store_id}.key"
    old_key = key_path.read_bytes()
    key_path.unlink()

    again = Store.open(path, keys_dir=keys_dir)
    try:
        assert again.log_verify()["status"] == "unverifiable"
        again.keyring.retired_path(old_fingerprint).write_bytes(old_key)
        report = again.log_verify()
        assert report["status"] == "ok"
        assert [s["status"] for s in report["segments"]] == ["ok", "ok"]
    finally:
        again.close()


def test_a_foreign_key_under_the_store_name_is_retired_not_destroyed(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The key file exists but is not the one meta names (the store came
    back from a machine that rekeyed it). It is kept under a retired name
    so the segment it signed can still be verified once that key is
    identified; a fresh key takes over the chain."""
    path = tmp_path / "memory.sqlite"
    first = Store.create(path, keys_dir=keys_dir)
    first.put_memory(_memory("alpha"))
    first.close()
    key_path = keys_dir / f"{first.store_id}.key"
    foreign = secrets.token_bytes(32)
    key_path.write_bytes(foreign)

    again = Store.open(path, keys_dir=keys_dir)
    try:
        assert again.key_fingerprint != chain.fingerprint(foreign)
        retired = again.keyring.retired_path(chain.fingerprint(foreign))
        assert retired.read_bytes() == foreign
        assert again.log_rows()[-1].kind == chain.REKEY
    finally:
        again.close()


def test_a_missing_head_reads_unverifiable(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    store.keyring.head_path().unlink()
    report = store.log_verify()
    assert report["head"]["status"] == "absent"
    assert report["status"] == "unverifiable"


def test_a_head_that_names_a_different_mac_is_tampering(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    head = store.keyring.read_head()
    assert head is not None
    store.keyring.head_path().write_text(
        json.dumps({"seq": head.seq, "mac": "ab" * 32, "ts": head.ts})
    )
    report = store.log_verify()
    assert report["head"]["status"] == "mismatch"
    assert report["status"] == "tampered"


def test_a_stale_head_is_a_warning_not_tampering(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    head = store.keyring.read_head()
    assert head is not None
    first = store.log_rows()[0]
    store.keyring.head_path().write_text(
        json.dumps({"seq": first.seq, "mac": first.mac, "ts": head.ts})
    )
    report = store.log_verify()
    assert report["head"]["status"] == "stale"
    assert report["status"] == "ok"


def test_a_forged_trailing_rekey_row_cannot_read_as_ok(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    last = store.log_rows()[-1]
    outside = _outside(store)
    outside.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            last.seq + 1,
            "2026-09-26T00:00:00.000Z",
            None,
            chain.REKEY,
            chain.canonical_payload(
                {
                    "old_fingerprint": store.key_fingerprint,
                    "new_fingerprint": "f" * 64,
                }
            ),
            last.mac,
            secrets.token_hex(32),
        ),
    )
    outside.commit()
    outside.close()
    report = store.log_verify()
    assert report["status"] == "tampered"


# ---------------------------------------------------------------------------
# Telemetry rows
# ---------------------------------------------------------------------------


def test_telemetry_rows_redact_query_fields_before_the_mac(
    store: Store,
) -> None:
    secret = "sk-ant-" + "a" * 40
    store.record_event(
        "search",
        session="sess_a",
        query=f"where is {secret} kept",
        probe_query="the probe",
        returned=["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
    )
    row = store.log_rows()[-1]
    assert row.kind == "search"
    assert row.session == "sess_a"
    payload = json.loads(row.payload)
    assert set(payload["query"]) == {"hash", "preview", "len"}
    assert secret not in row.payload
    assert payload["query"]["preview"].startswith("where is [REDACTED:anthropic")
    assert len(payload["query"]["preview"]) == 32
    assert payload["query"]["len"] == len(f"where is {secret} kept")
    assert set(payload["probe_query"]) == {"hash", "preview", "len"}
    assert payload["returned"] == ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert store.log_verify()["status"] == "ok"


def test_telemetry_rows_carry_the_actor_when_given(store: Store) -> None:
    store.record_event(
        "show", session="sess_a", id="x", actor={"client": "claude-code"}
    )
    payload = json.loads(store.log_rows()[-1].payload)
    assert payload["actor"] == {"client": "claude-code"}


def test_a_telemetry_kind_cannot_impersonate_a_mutation_or_control_row(
    store: Store,
) -> None:
    with pytest.raises(ValueError):
        store.record_event("memory_put", session="sess_a")
    with pytest.raises(ValueError):
        store.record_event(chain.REKEY, session="sess_a")
    with pytest.raises(ValueError):
        store.record_event(chain.STORE_CREATED, session="sess_a")


def test_telemetry_rows_do_not_touch_the_fold(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    for _ in range(5):
        store.record_event("search", session="sess_a", query="alpha")
    report = store.log_verify()
    assert report["status"] == "ok"
    assert report["rows"] == 7


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_a_failed_append_rolls_the_table_change_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = _memory("alpha")
    before = store.log_rows()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no append")

    monkeypatch.setattr("bettermemory.store.append_row", boom)
    with pytest.raises(RuntimeError):
        store.put_memory(memory)
    assert not store.has_memory(memory.id)
    assert store.log_rows() == before
    assert store.log_verify()["status"] == "ok"


def test_rows_are_seq_contiguous_from_one(store: Store) -> None:
    for i in range(4):
        store.put_memory(_memory(f"m{i}"))
    store.record_event("search", session="sess_a", query="m")
    rows = store.log_rows()
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    for earlier, later in zip(rows, rows[1:]):
        assert later.prev_mac == earlier.mac
    assert store.log_rows(since_seq=3)[0].seq == 4


def test_rows_stamp_a_utc_zulu_timestamp(store: Store) -> None:
    store.put_memory(_memory("alpha"))
    for row in store.log_rows():
        assert row.ts.endswith("Z")
        datetime.fromisoformat(row.ts.replace("Z", "+00:00"))


def test_verify_never_needs_the_environment_key_variable() -> None:
    """The key is a file, never an environment variable: nothing in the
    chain module reads one."""
    import inspect

    assert "environ" not in inspect.getsource(chain)
    assert os.environ is not None
