"""Segment trust: a foreign log segment cannot pass as this host's own.

A writer who holds the store file but not the key used to be able to
plant a memory that read `local`: append a `rekey` row naming a key of
its own, a `memory_put` row signed with that key, the matching table
row, and point meta's `key_fingerprint` at the new key. The owner's next
open took meta at its word, retired the real key and rekeyed, so the
planted row sat in a segment no key here held, whose rows kept their
stored labels; `log verify` reported only `unverifiable`.

What is pinned here:

1. The recipe, and its variant without the meta edit, leave the planted
   row `unaccounted` on every read surface and in the health census, and
   `log verify` reports `foreign_segment` with the segment's range, so
   the status is `tampered` and the command exits 1.
2. A store copied here without its key (created under key A, opened
   under key B with no A) reads exactly as before with
   `[security] strict_provenance` off, and `unaccounted` with it on.
3. A store under one key throughout is unchanged: same labels, same
   verify report, no extra row at open.
4. The owner's own rows after the foreign segment verify `local`, and
   so do rows it appended into the segment while it stood open.

The attacker in these tests is the editor with sqlite3 and the file: a
second connection that appends rows signed with a key of its own (a
well-formed forgery, the strongest a writer without the key can make)
and applies the table row the way the store would.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import config as config_module
from bettermemory import log as chain
from bettermemory import store as store_module
from bettermemory.config import DEFAULT_CONFIG, SecurityConfig, load_config
from bettermemory.models import Confidence, Episode, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import LOCAL, STORE_FILENAME, UNACCOUNTED, Store


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


def _episode(body: str) -> Episode:
    return Episode(
        id=generate_ulid(),
        session_id="sess_planted",
        created=datetime.now(timezone.utc),
        body=body.strip() + "\n",
        scopes=["projects:foo"],
        takeaway="the deploy key is in the shared drive",
        origin=Origin(cwd="/w/foo", worktree_root="/w/foo"),
    )


def _outside(path: Path) -> sqlite3.Connection:
    """A second connection to the store file: the editor with sqlite3 and
    no key."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class Forgery:
    """What the writer without the key appended: the forged `rekey` row
    and the rows after it, signed with a key of the writer's own."""

    key_fingerprint: str
    rekey_seq: int
    last_seq: int


def _forge(
    path: Path,
    *,
    old_fingerprint: str,
    memory: Memory | None = None,
    episode: Episode | None = None,
    edit_meta: bool = True,
) -> Forgery:
    """The survey's recipe. (1) Append a `rekey` row {old, new: X} and a
    put row, both MAC'd with the writer's own key X. (2) Apply the put to
    its table with the forged row's MAC, as the store's fold would. (3)
    With `edit_meta`, set meta's `key_fingerprint` to X, so the owner's
    opener sees a store whose key went missing."""
    forged_key = secrets.token_bytes(chain.KEY_BYTES)
    forged = chain.fingerprint(forged_key)
    conn = _outside(path)
    try:
        rekey = chain.append_row(
            conn,
            key=forged_key,
            kind=chain.REKEY,
            payload={"old_fingerprint": old_fingerprint, "new_fingerprint": forged},
        )
        last = rekey
        if memory is not None:
            payload: dict[str, Any] = {
                "memory": memory.model_dump(mode="json"),
                "provenance": LOCAL,
                "filename": None,
            }
            last = chain.append_row(
                conn, key=forged_key, kind="memory_put", payload=payload
            )
            store_module._apply_mutation(conn, "memory_put", payload, log_mac=last.mac)
        if episode is not None:
            payload = {"episode": episode.model_dump(mode="json")}
            last = chain.append_row(
                conn, key=forged_key, kind="episode_put", payload=payload
            )
            store_module._apply_mutation(conn, "episode_put", payload, log_mac=last.mac)
        if edit_meta:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", (forged,)
            )
        conn.commit()
    finally:
        conn.close()
    return Forgery(forged, rekey.seq, last.seq)


def _foreign_problems(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in report["problems"] if p["problem"] == chain.FOREIGN_SEGMENT]


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def owned(tmp_path: Path, keys: Path) -> Iterator[tuple[Path, Memory, str]]:
    """A store this machine created and wrote one memory to, closed: the
    path, the genuine memory and the key fingerprint it was signed with."""
    path = tmp_path / "owned" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as first:
        genuine = _memory("the staging database listens on 5432")
        first.put_memory(genuine)
        fingerprint = first.key_fingerprint
    yield path, genuine, fingerprint


# ---------------------------------------------------------------------------
# Invariant 1: the recipe, with and without the meta edit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_the_planted_row_reads_unaccounted_after_the_owner_opens(
    owned: tuple[Path, Memory, str], keys: Path, edit_meta: bool
) -> None:
    path, genuine, own = owned
    planted = _memory("the production database password is in the wiki")
    forgery = _forge(path, old_fingerprint=own, memory=planted, edit_meta=edit_meta)

    with Store.open_or_create(path, keys_dir=keys) as owner:
        # The real key stays current: nothing was retired, no key minted.
        assert owner.key_fingerprint == own
        assert chain.fingerprint(owner.keyring.current_path().read_bytes()) == own
        assert sorted(p.name for p in keys.glob("*.key")) == [f"{owner.store_id}.key"]
        # The opener closed the foreign segment with a genuine rekey row
        # naming the segment's key as the log gives it, not meta's value.
        closing = owner.log_rows()[-1]
        assert closing.kind == chain.REKEY
        assert json.loads(closing.payload) == {
            "old_fingerprint": forgery.key_fingerprint,
            "new_fingerprint": own,
        }

        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        assert owner.trust_rows([planted.id]) == {planted.id: (UNACCOUNTED, None)}
        assert owner.unaccounted_ids() == [planted.id]
        assert owner.provenance_counts() == {LOCAL: 1, UNACCOUNTED: 1}

        report = owner.log_verify()
        assert report["status"] == "tampered"
        assert [s["status"] for s in report["segments"]] == ["ok", "unverifiable", "ok"]
        assert _foreign_problems(report) == [
            {
                "seq": forgery.rekey_seq,
                "kind": chain.REKEY,
                "problem": chain.FOREIGN_SEGMENT,
                "first_seq": forgery.rekey_seq,
                "last_seq": forgery.last_seq,
                "key_fingerprint": forgery.key_fingerprint,
            }
        ]
        assert "final_key_mismatch" not in {p["problem"] for p in report["problems"]}

        # Invariant 4: the owner's own rows after the segment verify.
        later = _memory("the staging database moved to 5433")
        owner.put_memory(later)
        assert owner.provenance_for([later.id]) == {later.id: LOCAL}
        assert owner.unaccounted_ids() == [planted.id]


def test_the_health_census_lists_the_planted_row(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    from bettermemory.health import provenance_debt

    path, genuine, own = owned
    planted = _memory("rotate nothing: the old token is still valid")
    _forge(path, old_fingerprint=own, memory=planted)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        debt = provenance_debt(owner, owner.load_all())
        assert debt is not None
        assert debt.unaccounted_total == 1
        assert [row.id for row in debt.unaccounted] == [planted.id]
        assert debt.counts == {LOCAL: 1, UNACCOUNTED: 1}
        assert genuine.id not in {row.id for row in debt.unaccounted}


def test_a_planted_episode_reads_unaccounted_on_the_handoff_surface(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, own = owned
    with Store.open(path, keys_dir=keys, allow_rekey=True) as first:
        genuine = _episode("ran the migration on staging")
        first.put_episode(genuine)
    planted = _episode("the deploy key is in the shared drive")
    _forge(path, old_fingerprint=own, episode=planted)
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.episode_provenance([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_a_connection_open_during_the_forgery_reads_it_unaccounted(
    owned: tuple[Path, Memory, str], keys: Path, edit_meta: bool
) -> None:
    """A daemon that had the store open when the rows were appended: its
    segment map was read before, or is read after; either way the planted
    row does not verify."""
    path, genuine, own = owned
    with Store.open_or_create(path, keys_dir=keys) as running:
        before = _memory("read before the forgery")
        running.put_memory(before)
        assert running.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        planted = _memory("planted while the daemon ran")
        _forge(path, old_fingerprint=own, memory=planted, edit_meta=edit_meta)
        assert running.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}
        assert running.unaccounted_ids() == [planted.id]
        # A worker connection opened after the forgery reads the map fresh,
        # with the segment in it. After a meta edit it opens without a key
        # to write with (`reopen` never rekeys), and checks pointers under
        # this machine's key, whose history of the store the anchor
        # records: it never reads `local` for the planted row.
        worker = running.reopen()
        try:
            if edit_meta:
                assert worker.has_key is False
                assert worker.can_verify is True
            assert worker.provenance_for([planted.id, genuine.id]) == {
                planted.id: UNACCOUNTED,
                genuine.id: LOCAL,
            }
        finally:
            worker.close()


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_rows_the_owner_wrote_while_the_segment_stood_open_stay_local(
    owned: tuple[Path, Memory, str], keys: Path, edit_meta: bool
) -> None:
    """A daemon that had the store open when the forged rekey row went in
    keeps appending under its own key, into the segment that row opened,
    until a restart's opener closes it. Those rows are this machine's:
    they verify under its key on a fresh connection and after the repair,
    strict or not, while the planted row still does not."""
    path, genuine, own = owned
    planted = _memory("planted while the daemon ran")
    with Store.open_or_create(path, keys_dir=keys) as running:
        forgery = _forge(path, old_fingerprint=own, memory=planted, edit_meta=edit_meta)
        during = _memory("written by the daemon after the forgery")
        running.put_memory(during)
        if not edit_meta:
            # A fresh map puts the row in the foreign segment.
            worker = running.reopen()
            try:
                assert worker.provenance_for([planted.id, during.id]) == {
                    planted.id: UNACCOUNTED,
                    during.id: LOCAL,
                }
            finally:
                worker.close()
    for strict in (False, True):
        with Store.open_or_create(
            path, keys_dir=keys, strict_provenance=strict
        ) as owner:
            assert owner.provenance_for([genuine.id, planted.id, during.id]) == {
                genuine.id: LOCAL,
                planted.id: UNACCOUNTED,
                during.id: LOCAL,
            }
            assert owner.unaccounted_ids() == [planted.id]
            assert owner.provenance_counts() == {LOCAL: 2, UNACCOUNTED: 1}
            report = owner.log_verify()
        # The row is still inside the segment, which `log verify` reports
        # whole: the repair closed it after the daemon's row.
        assert report["status"] == "tampered"
        [foreign] = _foreign_problems(report)
        assert (foreign["first_seq"], foreign["last_seq"]) == (
            forgery.rekey_seq,
            forgery.last_seq + 1,
        )


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_log_verify_reports_the_segment_before_the_owner_opens(
    owned: tuple[Path, Memory, str], keys: Path, edit_meta: bool
) -> None:
    """`log verify` opens read-only and never repairs; it still names the
    segment, whatever meta says."""
    path, _genuine, own = owned
    forgery = _forge(
        path, old_fingerprint=own, memory=_memory("planted"), edit_meta=edit_meta
    )
    with Store.open(path, keys_dir=keys, allow_rekey=False) as reader:
        report = reader.log_verify()
        assert reader.log_rows()[-1].seq == forgery.last_seq
    assert report["status"] == "tampered"
    assert [p["first_seq"] for p in _foreign_problems(report)] == [forgery.rekey_seq]


def test_the_log_verify_command_exits_one_on_a_foreign_segment(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bettermemory.cli import main

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    path = store_dir / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as first:
        first.put_memory(_memory("genuine"))
        own = first.key_fingerprint
    forgery = _forge(path, old_fingerprint=own, memory=_memory("planted"))

    def run(argv: list[str]) -> int:
        monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
        with pytest.raises(SystemExit) as exc:
            main()
        return int(exc.value.code or 0)

    assert run(["log", "verify", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "tampered"
    [problem] = _foreign_problems(report)
    assert (problem["first_seq"], problem["last_seq"]) == (
        forgery.rekey_seq,
        forgery.last_seq,
    )
    assert run(["log", "verify"]) == 1
    text = capsys.readouterr().out
    assert f"seq {forgery.rekey_seq} (rekey): foreign_segment" in text


def test_a_forged_first_rekey_cannot_rename_this_hosts_history(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    """The leading segment's name is the first rekey row's
    `old_fingerprint`, which no MAC covers. A forged first rekey row that
    names a key nobody holds would turn this host's own history into the
    copied-store case, whose rows keep their labels; the leading segment
    is identified by its first row's MAC instead."""
    path, genuine, own = owned
    planted = _memory("planted under a renamed history")
    forgery = _forge(
        path, old_fingerprint=secrets.token_hex(32), memory=planted, edit_meta=True
    )
    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.key_fingerprint == own
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = owner.log_verify()
    assert report["status"] == "tampered"
    assert report["segments"][0]["key_fingerprint"] == own
    assert report["segments"][0]["status"] == "ok"
    assert [p["first_seq"] for p in _foreign_problems(report)] == [forgery.rekey_seq]


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_a_rewritten_first_row_is_caught_under_strict_provenance(
    owned: tuple[Path, Memory, str], keys: Path, edit_meta: bool
) -> None:
    """An editor who renames this host's history and also rewrites its
    first row, so that no key here verifies it, leaves nothing in the log
    that says where this host's rows begin: the history and the forged
    segment after it read like a store copied here. The origin anchor
    beside the key says where they begin, and catches it by default
    (tests/test_origin_anchor.py); `strict_provenance` catches it without
    one. Under it the planted row reads `unaccounted`, and a row of this
    host's own that its current key still verifies reads `local`."""
    path, genuine, _own = owned
    conn = _outside(path)
    conn.execute("UPDATE log SET ts = ts || ' ' WHERE seq = 1")
    conn.commit()
    conn.close()
    planted = _memory("planted behind a rewritten first row")
    _forge(
        path,
        old_fingerprint=secrets.token_hex(32),
        memory=planted,
        edit_meta=edit_meta,
    )
    with Store.open_or_create(path, keys_dir=keys, strict_provenance=True) as owner:
        labels = owner.provenance_for([genuine.id, planted.id])
        assert labels[planted.id] == UNACCOUNTED
        assert planted.id in owner.unaccounted_ids()
        if not edit_meta:
            # The opener kept the key, which still verifies the row.
            assert labels[genuine.id] == LOCAL


def test_a_meta_edit_alone_is_set_back_without_a_rekey(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    """Meta names another key, and no rekey row exists: the owner's key
    signs the whole log, so meta is set back, nothing is retired and no
    rekey row is written. A row forged into that log fails its MAC."""
    path, genuine, own = owned
    planted = _memory("planted with only meta edited")
    conn = _outside(path)
    payload: dict[str, Any] = {
        "memory": planted.model_dump(mode="json"),
        "provenance": LOCAL,
        "filename": None,
    }
    forged = chain.append_row(
        conn, key=secrets.token_bytes(32), kind="memory_put", payload=payload
    )
    store_module._apply_mutation(conn, "memory_put", payload, log_mac=forged.mac)
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
        (secrets.token_hex(32),),
    )
    conn.commit()
    conn.close()

    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.key_fingerprint == own
        assert owner.log_rows()[-1].seq == forged.seq
        assert not any(row.kind == chain.REKEY for row in owner.log_rows())
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = owner.log_verify()
    assert report["status"] == "tampered"
    assert {p["problem"] for p in report["problems"]} == {"mac_mismatch"}


def test_a_store_the_previous_opener_rekeyed_over_the_forgery_is_caught(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    """A store whose owner opened it under 9.0.0rc11 after the forgery:
    that opener retired the real key, wrote a fresh one and appended a
    rekey row naming meta's (forged) fingerprint as the old key. The real
    key is still on the machine under its retired name, so the forged
    segment sits between two held ones and is foreign."""
    path, genuine, own = owned
    planted = _memory("planted before an rc11 open")
    forgery = _forge(path, old_fingerprint=own, memory=planted)
    with Store.open(path, keys_dir=keys) as reader:
        ring = reader.keyring
    ring.retire_current()
    fresh = ring.create_key()
    conn = _outside(path)
    chain.append_row(
        conn,
        key=fresh,
        kind=chain.REKEY,
        payload={
            "old_fingerprint": forgery.key_fingerprint,
            "new_fingerprint": chain.fingerprint(fresh),
        },
    )
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
        (chain.fingerprint(fresh),),
    )
    conn.commit()
    conn.close()

    with Store.open_or_create(path, keys_dir=keys) as owner:
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = owner.log_verify()
    assert report["status"] == "tampered"
    assert [s["key_fingerprint"] for s in report["segments"]] == [
        own,
        forgery.key_fingerprint,
        chain.fingerprint(fresh),
    ]
    assert len(_foreign_problems(report)) == 1


def test_a_rekey_row_with_no_names_after_held_rows_is_foreign(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    path, genuine, _own = owned
    planted = _memory("planted behind a nameless rekey row")
    conn = _outside(path)
    forged_key = secrets.token_bytes(32)
    rekey = chain.append_row(conn, key=forged_key, kind=chain.REKEY, payload={})
    payload: dict[str, Any] = {
        "memory": planted.model_dump(mode="json"),
        "provenance": LOCAL,
        "filename": None,
    }
    put = chain.append_row(conn, key=forged_key, kind="memory_put", payload=payload)
    store_module._apply_mutation(conn, "memory_put", payload, log_mac=put.mac)
    conn.commit()
    conn.close()
    with Store.open(path, keys_dir=keys) as reader:
        assert reader.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
        report = reader.log_verify()
    problems = {p["problem"] for p in report["problems"]}
    assert {"payload_invalid", chain.FOREIGN_SEGMENT} <= problems
    assert report["status"] == "tampered"
    [foreign] = _foreign_problems(report)
    assert (foreign["first_seq"], foreign["key_fingerprint"]) == (rekey.seq, "")


def test_reopening_after_the_repair_appends_nothing_more(
    owned: tuple[Path, Memory, str], keys: Path
) -> None:
    path, _genuine, own = owned
    _forge(path, old_fingerprint=own, memory=_memory("planted"), edit_meta=False)
    with Store.open_or_create(path, keys_dir=keys) as first:
        rows = len(first.log_rows())
    with Store.open_or_create(path, keys_dir=keys) as second:
        assert len(second.log_rows()) == rows
        assert second.log_rows()[-1].kind == chain.REKEY


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_two_openers_racing_close_the_segment_once(
    owned: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit_meta: bool,
) -> None:
    """Two owner openers (a hook and the daemon) both see the foreign tail
    before either takes the write lock. The second re-reads the tail under
    the lock and appends nothing: the first already closed it. Simulated
    by opening once, then handing the second opener the tail as it read
    before the first opener's commit."""
    path, _genuine, own = owned
    forgery = _forge(
        path, old_fingerprint=own, memory=_memory("planted"), edit_meta=edit_meta
    )
    with Store.open(path, keys_dir=keys) as reader:
        stale_segments = chain.chain_segments(
            reader.conn,
            keyring=reader.keyring,
            current_fingerprint=reader.key_fingerprint,
        )
    with Store.open_or_create(path, keys_dir=keys) as first:
        rows = len(first.log_rows())

    if edit_meta:
        # The second opener read meta and the segments before the first
        # set meta back and closed the segment.
        conn = _outside(path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            (forgery.key_fingerprint,),
        )
        conn.commit()
        conn.close()

        def stale_map(*_args: Any, **_kwargs: Any) -> list[chain.Segment]:
            return stale_segments

        monkeypatch.setattr(store_module, "chain_segments", stale_map)
    else:
        # The second opener read the tail before the first closed it.
        real = chain.last_rekey_name
        calls: list[None] = []

        def before_the_lock(conn: sqlite3.Connection) -> str | None:
            calls.append(None)
            return forgery.key_fingerprint if len(calls) == 1 else real(conn)

        monkeypatch.setattr(store_module, "last_rekey_name", before_the_lock)
    with Store.open_or_create(path, keys_dir=keys) as second:
        assert len(second.log_rows()) == rows
        assert second.key_fingerprint == own


def test_the_repair_warns_and_names_the_segment(
    owned: tuple[Path, Memory, str],
    keys: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _genuine, own = owned
    forgery = _forge(path, old_fingerprint=own, memory=_memory("planted"))
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        Store.open_or_create(path, keys_dir=keys).close()
    [record] = [r for r in caplog.records if r.name == "bettermemory.store"]
    message = record.getMessage()
    assert forgery.key_fingerprint[:16] in message
    assert f"seq {forgery.rekey_seq}" in message
    assert own[:16] in message
    assert "log verify" in message


@pytest.mark.parametrize("edit_meta", [True, False], ids=["meta-edit", "no-meta-edit"])
def test_a_store_that_cannot_be_written_still_opens(
    owned: tuple[Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    edit_meta: bool,
) -> None:
    """The repair needs the write lock and a writable file. When it cannot
    have them the store still opens, with this machine's key, and the
    planted row still reads unaccounted: the read side never depended on
    the repair. A daemon is never kept from starting by this."""
    path, genuine, own = owned
    planted = _memory("planted on a read-only store")
    _forge(path, old_fingerprint=own, memory=planted, edit_meta=edit_meta)

    @contextlib.contextmanager
    def refused(self: Store) -> Iterator[sqlite3.Connection]:
        raise sqlite3.OperationalError("attempt to write a readonly database")
        yield self.conn  # pragma: no cover - never reached

    monkeypatch.setattr(Store, "_transaction", refused)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        owner = Store.open_or_create(path, keys_dir=keys)
    try:
        assert owner.has_key
        assert owner.provenance_for([genuine.id, planted.id]) == {
            genuine.id: LOCAL,
            planted.id: UNACCOUNTED,
        }
    finally:
        owner.close()
    assert any("readonly database" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Invariant 2: a store copied here without its key
# ---------------------------------------------------------------------------


@pytest.fixture
def moved(tmp_path: Path) -> tuple[Path, Path, Memory, str]:
    """A store created under key directory A, then opened by the owner's
    opener under key directory B, which never held A: the documented
    copied-store case. Returns the path, B, A's memory and A's
    fingerprint."""
    path = tmp_path / "moved" / STORE_FILENAME
    with Store.create(path, keys_dir=tmp_path / "keys-a") as first:
        arrived = _memory("written on the other machine")
        first.put_memory(arrived)
        fingerprint_a = first.key_fingerprint
    keys_b = tmp_path / "keys-b"
    Store.open_or_create(path, keys_dir=keys_b, strict_provenance=False).close()
    return path, keys_b, arrived, fingerprint_a


def test_a_moved_store_reads_as_before_by_default(
    moved: tuple[Path, Path, Memory, str],
) -> None:
    path, keys_b, arrived, fingerprint_a = moved
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=False) as here:
        rekey = [r for r in here.log_rows() if r.kind == chain.REKEY]
        assert [json.loads(r.payload)["old_fingerprint"] for r in rekey] == [
            fingerprint_a
        ]
        written = _memory("written here")
        here.put_memory(written)
        assert here.provenance_for([arrived.id, written.id]) == {
            arrived.id: LOCAL,
            written.id: LOCAL,
        }
        assert here.unaccounted_ids() == []
        report = here.log_verify()
    assert report["status"] == "unverifiable"
    assert report["problems"] == []
    assert [s["status"] for s in report["segments"]] == ["unverifiable", "ok"]
    assert report["segments"][0]["key_fingerprint"] == fingerprint_a


def test_a_moved_store_reads_unaccounted_under_strict_provenance(
    moved: tuple[Path, Path, Memory, str],
) -> None:
    path, keys_b, arrived, _fingerprint_a = moved
    with Store.open_or_create(path, keys_dir=keys_b, strict_provenance=True) as here:
        assert here.strict_provenance is True
        written = _memory("written here")
        here.put_memory(written)
        assert here.provenance_for([arrived.id, written.id]) == {
            arrived.id: UNACCOUNTED,
            written.id: LOCAL,
        }
        assert here.unaccounted_ids() == [arrived.id]
        assert here.trust_rows([arrived.id]) == {arrived.id: (UNACCOUNTED, None)}
        # The verdict on the chain is the same either way: strictness is a
        # read-time policy, not a finding about the log.
        assert here.log_verify()["status"] == "unverifiable"


def test_strict_provenance_is_read_from_the_config(
    moved: tuple[Path, Path, Memory, str],
) -> None:
    path, keys_b, arrived, _fingerprint_a = moved
    config_module.default_config_path().write_text(
        DEFAULT_CONFIG.replace(
            "# strict_provenance = true", "strict_provenance = true"
        ),
        encoding="utf-8",
    )
    with Store.open_or_create(path, keys_dir=keys_b) as here:
        assert here.provenance_for([arrived.id]) == {arrived.id: UNACCOUNTED}
        assert here.strict_provenance is True


def test_the_config_is_read_only_when_a_row_needs_it(
    owned: tuple[Path, Memory, str],
    moved: tuple[Path, Path, Memory, str],
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store under one key never reads the switch; a store with a
    leading segment whose key is not held reads it once per connection,
    and a worker connection inherits what its parent read."""
    calls: list[None] = []
    original = config_module.load_config

    def counting(
        path: Path | None = None, *, create: bool = True
    ) -> config_module.Config:
        calls.append(None)
        return original(path, create=create)

    monkeypatch.setattr(config_module, "load_config", counting)

    owned_path, genuine, _own = owned
    with Store.open_or_create(owned_path, keys_dir=keys) as one_key:
        assert one_key.provenance_for([genuine.id]) == {genuine.id: LOCAL}
        assert one_key.unaccounted_ids() == []
    assert calls == []

    moved_path, keys_b, arrived, _fingerprint_a = moved
    with Store.open_or_create(moved_path, keys_dir=keys_b) as here:
        assert here.provenance_for([arrived.id]) == {arrived.id: LOCAL}
        assert here.unaccounted_ids() == []
        assert len(calls) == 1
        worker = here.reopen()
        try:
            assert worker.provenance_for([arrived.id]) == {arrived.id: LOCAL}
        finally:
            worker.close()
        assert len(calls) == 1


def test_a_config_that_cannot_be_read_reads_as_strict(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(
        path: Path | None = None, *, create: bool = True
    ) -> config_module.Config:
        raise ValueError("malformed config in managed.toml")

    monkeypatch.setattr(config_module, "load_config", broken)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        assert store_module.configured_strict_provenance() is True
    assert "malformed config" in caplog.text


@pytest.mark.parametrize(
    "text",
    ["[security\n", "behavior = 1\n", "scopes = 1\n"],
    ids=["toml-syntax", "section-type-error", "section-attribute-error"],
)
def test_a_config_edited_after_the_daemon_started_never_fails_a_read(
    moved: tuple[Path, Path, Memory, str],
    caplog: pytest.LogCaptureFixture,
    text: str,
) -> None:
    """The store reads the switch the first time a row needs it, which in a
    daemon is long after the daemon read the config. A file edited since
    into something `load_config` raises on, whatever the exception, reads
    as strict and labels the row; the read does not raise."""
    path, keys_b, arrived, _fingerprint_a = moved
    config_module.default_config_path().write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys_b) as here:
            assert here.provenance_for([arrived.id]) == {arrived.id: UNACCOUNTED}
            assert here.strict_provenance is True
    assert "strict_provenance could not be read" in caplog.text


def test_reopen_and_the_constructor_carry_the_switch(tmp_path: Path) -> None:
    with Store(tmp_path, strict_provenance=True) as store:
        assert store.strict_provenance is True
        worker = store.reopen()
        try:
            assert worker.strict_provenance is True
        finally:
            worker.close()
    with Store(tmp_path, strict_provenance=False) as store:
        assert store.strict_provenance is False


def test_a_store_moved_away_and_back_reads_the_other_machines_rows_as_foreign(
    tmp_path: Path,
) -> None:
    """Created here under key A, opened elsewhere under B, brought back.
    9.0 keeps one host per store, and from here B's rows are writes this
    machine never signed after history it did: a foreign segment. A stays
    the store's key. Filing B's key here as a retired key, which is how
    an operator vouches for the other machine, makes the segment held and
    the log verify again."""
    path = tmp_path / STORE_FILENAME
    keys_a, keys_b = tmp_path / "keys-a", tmp_path / "keys-b"
    with Store.create(path, keys_dir=keys_a) as first:
        here = _memory("written here")
        first.put_memory(here)
        fingerprint_a = first.key_fingerprint
    with Store.open_or_create(path, keys_dir=keys_b) as away:
        there = _memory("written on the other machine")
        away.put_memory(there)
        fingerprint_b = away.key_fingerprint
        key_b = away.keyring.current_path().read_bytes()
    with Store.open_or_create(path, keys_dir=keys_a) as back:
        assert back.key_fingerprint == fingerprint_a
        assert back.provenance_for([here.id, there.id]) == {
            here.id: LOCAL,
            there.id: UNACCOUNTED,
        }
        report = back.log_verify()
        assert report["status"] == "tampered"
        [foreign] = _foreign_problems(report)
        assert foreign["key_fingerprint"] == fingerprint_b
        back.keyring.retired_path(fingerprint_b).write_bytes(key_b)
    with Store.open_or_create(path, keys_dir=keys_a) as vouched:
        assert vouched.provenance_for([here.id, there.id]) == {
            here.id: LOCAL,
            there.id: LOCAL,
        }
        report = vouched.log_verify()
    assert report["status"] == "ok"
    assert report["problems"] == []


# ---------------------------------------------------------------------------
# Invariant 3: one key throughout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_a_store_under_one_key_is_unchanged(
    tmp_path: Path, keys: Path, strict: bool
) -> None:
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys, strict_provenance=strict) as first:
        memories = [_memory(f"fact number {n}") for n in range(3)]
        for memory in memories:
            first.put_memory(memory)
        episode = _episode("a session note")
        first.put_episode(episode)
        before = first.log_verify()
        rows_before = len(first.log_rows())
    for _ in range(2):
        with Store.open_or_create(
            path, keys_dir=keys, strict_provenance=strict
        ) as again:
            assert len(again.log_rows()) == rows_before
            assert again.provenance_for([m.id for m in memories]) == {
                m.id: LOCAL for m in memories
            }
            assert again.episode_provenance([episode.id]) == {episode.id: LOCAL}
            assert again.unaccounted_ids() == []
            report = again.log_verify()
            assert report == before
    assert before["status"] == "ok"
    assert before["problems"] == []
    assert [set(s) for s in before["segments"]] == [
        {"first_seq", "last_seq", "key_fingerprint", "status", "rows"}
    ]


def test_a_rekey_this_host_wrote_is_not_foreign(tmp_path: Path, keys: Path) -> None:
    """A key lost here and replaced: the old segment leads, the new one is
    held, and nothing is foreign."""
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as first:
        before = _memory("before the key was lost")
        first.put_memory(before)
        (keys / f"{first.store_id}.key").unlink()
    with Store.open_or_create(path, keys_dir=keys, strict_provenance=False) as again:
        after = _memory("after the key was lost")
        again.put_memory(after)
        assert again.provenance_for([before.id, after.id]) == {
            before.id: LOCAL,
            after.id: LOCAL,
        }
        report = again.log_verify()
    assert report["status"] == "unverifiable"
    assert _foreign_problems(report) == []


# ---------------------------------------------------------------------------
# The store id the key ring is looked up by
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow_rekey", [False, True], ids=["reader", "owner"])
def test_a_store_id_that_names_a_path_is_refused(
    owned: tuple[Path, Memory, str], keys: Path, allow_rekey: bool
) -> None:
    """The key ring names its files after meta's store id. An id holding a
    path would have it read a key the writer put beside the store, and
    every row that writer signed would verify. The opener refuses any id
    `Store.create` never writes, and no key is read or written for it."""
    path, _genuine, _own = owned
    planted_key = secrets.token_bytes(chain.KEY_BYTES)
    (path.parent / "planted.key").write_bytes(planted_key)
    traversal = str(Path("..") / path.parent.name / "planted")
    conn = _outside(path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'store_id'", (traversal,))
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
        (chain.fingerprint(planted_key),),
    )
    conn.commit()
    conn.close()
    keys_before = sorted(p.name for p in keys.iterdir())
    with pytest.raises(ValueError, match="store id this build never writes"):
        Store.open(path, keys_dir=keys, allow_rekey=allow_rekey)
    assert sorted(p.name for p in keys.iterdir()) == keys_before
    assert not (path.parent / "planted.head").exists()


@pytest.mark.parametrize(
    "store_id", ["A" * 32, "a" * 31, "a" * 33, "g" * 32, "a" * 31 + "/"]
)
def test_only_the_shape_create_writes_is_a_store_id(
    owned: tuple[Path, Memory, str], keys: Path, store_id: str
) -> None:
    path, _genuine, _own = owned
    conn = _outside(path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'store_id'", (store_id,))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="store id"):
        Store.open(path, keys_dir=keys)


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_chain_segments_marks_only_a_segment_after_a_held_one_foreign(
    tmp_path: Path, keys: Path
) -> None:
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as first:
        first.put_memory(_memory("alpha"))
        own = first.key_fingerprint
    forgery = _forge(path, old_fingerprint=own, memory=_memory("planted"))
    with Store.open(path, keys_dir=keys) as reader:
        segments = chain.chain_segments(
            reader.conn, keyring=reader.keyring, current_fingerprint=own
        )
    assert [(s.first_seq, s.held, s.foreign) for s in segments] == [
        (1, True, False),
        (forgery.rekey_seq, False, True),
    ]
    assert segments[1].key_fingerprint == forgery.key_fingerprint
    # The key never appears in a segment's repr.
    assert segments[0].key is not None
    assert segments[0].key.hex() not in repr(segments[0])


def test_chain_segments_of_an_empty_log_is_empty(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(store_module.SCHEMA)
    ring = chain.KeyRing(tmp_path / "keys", "0" * 32)
    assert chain.chain_segments(conn, keyring=ring, current_fingerprint="") == []
    assert chain.last_rekey_name(conn) is None
    conn.close()


def test_the_key_ring_lists_its_current_and_retired_keys(tmp_path: Path) -> None:
    ring = chain.KeyRing(tmp_path / "keys", "ab" * 16)
    assert ring.held_keys() == []
    first = ring.create_key()
    ring.retire_current()
    second = ring.create_key()
    other = chain.KeyRing(tmp_path / "keys", "cd" * 16)
    other.create_key()
    assert ring.held_keys() == [second, first]


# ---------------------------------------------------------------------------
# The config key
# ---------------------------------------------------------------------------


def test_the_security_section_defaults_off_and_round_trips(tmp_path: Path) -> None:
    assert SecurityConfig().strict_provenance is False
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    assert load_config(path).security.strict_provenance is False
    path.write_text("[security]\nstrict_provenance = true\n", encoding="utf-8")
    assert load_config(path).security.strict_provenance is True
    path.write_text('[security]\nstrict_provenance = "yes"\n', encoding="utf-8")
    assert load_config(path).security.strict_provenance is True
    # A `security` value that is not a table fails the load, naming the
    # file and the section, which the store reads as strict (fail closed).
    path.write_text("security = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[security\] = 1 must be a table"):
        load_config(path)
    path.write_text("[behavior]\n", encoding="utf-8")
    assert load_config(path).security.strict_provenance is False


def test_the_shipped_security_prose_matches_the_behaviour() -> None:
    """The template's claims, each pinned by a test: rows after this
    machine's own under a key it never held read unaccounted (the recipe
    tests), a copied store keeps its labels by default (the moved-store
    test), where this machine's rows begin is kept beside the key and an
    edit made before it was written, or a fresh store id, still passes
    for a copy (tests/test_origin_anchor.py), and the switch widens that
    (the strict tests)."""
    block = DEFAULT_CONFIG.split("[security]")[1].split("[capture]")[0]
    # Commented out, as every key rc12 adds: a managed file that pins it
    # then draws no notice about a line the user never wrote.
    assert "Off by default.\n# strict_provenance = true" in block
    assert "never held read `unaccounted`" in block
    assert "copied here" in block
    assert "kept beside the key (`<store_id>.origin`)" in block
    assert "fresh id, can\n# still pass an editor's rows off" in block
    assert "read off the log" not in block
    assert "log verify" in block
