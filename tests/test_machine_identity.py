"""The machine id and the store id (RS-F6, design 2.1 and 8.7, decision
D16).

The two machine facts a host reports (the hardware or machine id and the
hostname) are injected, and the third, the id file's own object, is read
from a state directory under tmp_path, so nothing here depends on the
host. A state directory carried to another machine takes a new id naming
its predecessor, and `Membership.open` writes ``machine_cloned`` before
any resolution; a fact that changed on the same machine keeps the id with
a ``machine_facts_updated`` row (E26 a, b). The store id is minted with
the store and replaced when an import shows the store was copied whole
and both copies wrote (E26 e).
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import machine as machine_module
from bettermemory.machine import (
    MACHINE_FACTS_FILE,
    MACHINE_ID_FILE,
    MachineFacts,
    MachineIdentity,
    check_import_fork,
    is_clone,
    new_machine_id,
    open_machine,
)
from bettermemory.membership import REPO, Location, Membership, ObjectKey
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import Store

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _open(
    state: Path, *, hardware: str | None = "HW-A", hostname: str | None = "host-a"
) -> MachineIdentity:
    return open_machine(state, hardware=lambda: hardware, hostname=lambda: hostname)


def _memory() -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="alpha\n",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.create(tmp_path / "store" / "memory.sqlite", keys_dir=tmp_path / "keys")
    yield s
    s.close()


def _kinds(store: Store) -> list[str]:
    return [r.kind for r in store.membership_log()]


# ---------------------------------------------------------------------------
# The machine id
# ---------------------------------------------------------------------------


def test_a_first_open_mints_an_id_with_its_three_facts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = _open(state)
    assert (first.minted, first.cloned, first.predecessor) == (True, False, None)
    assert len(first.id) == 26
    lines = (state / MACHINE_ID_FILE).read_text(encoding="utf-8").splitlines()
    assert lines == ["bettermemory machine id v1", first.id]
    record = json.loads((state / MACHINE_FACTS_FILE).read_text(encoding="utf-8"))
    assert record["id"] == first.id
    facts = MachineFacts.from_dict(record["facts"])
    assert facts == first.facts
    assert (first.facts.hardware, first.facts.hostname) == ("HW-A", "host-a")
    assert first.facts.id_file is not None


def test_unchanged_facts_keep_the_id(tmp_path: Path, store: Store) -> None:
    state = tmp_path / "state"
    first = _open(state)
    Membership.open(store, first)
    again = _open(state)
    assert again.id == first.id
    assert (again.minted, again.cloned, again.facts_updated) == (False, False, False)
    Membership.open(store, again)
    assert _kinds(store) == ["machine_put"]


def test_another_hardware_id_mints_a_new_id_and_the_row_comes_first(
    tmp_path: Path, store: Store
) -> None:
    state = tmp_path / "state"
    first = _open(state)
    Membership.open(store, first)
    carried = _open(state, hardware="HW-B")
    assert carried.id != first.id
    assert (carried.minted, carried.cloned, carried.predecessor) == (
        True,
        True,
        first.id,
    )
    assert carried.recorded == first.facts
    seq = store.log_rows()[-1].seq
    model = Membership.open(store, carried, liveness=lambda k, v: "live")
    model.resolve(
        Location(
            machine=carried.id,
            cwd="/w/app",
            state=REPO,
            root="/w/app",
            kind="primary",
            git_dir="/w/app/.git",
            object_key=ObjectKey("1", "2", "3"),
            token="T" * 26,
            token_status="present",
        )
    )
    after = [r.kind for r in store.log_rows(seq)]
    assert after[:2] == ["machine_put", "machine_cloned"]
    cloned = json.loads(store.log_rows(seq)[1].payload)
    assert (cloned["old"], cloned["new"]) == (first.id, carried.id)
    machines = {m.id: m for m in model.machines()}
    assert machines[carried.id].predecessor == first.id
    # The new id stays at the next open on that machine.
    assert _open(state, hardware="HW-B").id == carried.id


def test_e26a_a_renamed_host_keeps_the_id_with_a_facts_row(
    tmp_path: Path, store: Store
) -> None:
    state = tmp_path / "state"
    first = _open(state)
    Membership.open(store, first)
    renamed = _open(state, hostname="host-renamed")
    assert renamed.id == first.id
    assert (renamed.minted, renamed.cloned, renamed.facts_updated) == (
        False,
        False,
        True,
    )
    Membership.open(store, renamed)
    assert _kinds(store) == ["machine_put", "machine_put", "machine_facts_updated"]
    record = json.loads((state / MACHINE_FACTS_FILE).read_text(encoding="utf-8"))
    assert record["facts"]["hostname"] == "host-renamed"
    assert _open(state, hostname="host-renamed").facts_updated is False


def test_the_id_file_rewritten_on_the_same_hardware_keeps_the_id(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = _open(state)
    copied = tmp_path / "copied"
    shutil.copytree(state, copied)
    again = _open(copied)
    assert again.facts.id_file != first.facts.id_file
    assert (again.id, again.facts_updated, again.cloned) == (first.id, True, False)


def test_without_a_hardware_id_the_hostname_and_the_id_file_decide_together(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = _open(state, hardware=None)
    # The hostname alone: a rename.
    assert _open(state, hardware=None, hostname="host-b").id == first.id
    # The id file alone: the same host.
    copied = tmp_path / "copied"
    shutil.copytree(state, copied)
    assert _open(copied, hardware=None, hostname="host-b").id == first.id
    # Both: another machine.
    moved = tmp_path / "moved"
    shutil.copytree(copied, moved)
    carried = _open(moved, hardware=None, hostname="host-c")
    assert carried.cloned and carried.predecessor == first.id


def test_is_clone_reads_the_hardware_first() -> None:
    a = MachineFacts("HW-A", "host-a", ("1", "2", "3"))
    assert not is_clone(a, a)
    assert is_clone(a, MachineFacts("HW-B", "host-a", ("1", "2", "3")))
    assert not is_clone(a, MachineFacts("HW-A", "host-b", ("9", "9", "9")))
    assert not is_clone(a, MachineFacts(None, "host-b", ("1", "2", "3")))
    assert is_clone(a, MachineFacts(None, "host-b", ("9", "9", "9")))


def test_a_damaged_id_file_mints_a_new_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = _open(state)
    (state / MACHINE_ID_FILE).write_text("not an id\n", encoding="utf-8")
    again = _open(state)
    assert again.id != first.id
    assert (again.minted, again.cloned, again.predecessor) == (True, False, None)


def test_machine_ids_are_crockford_and_distinct() -> None:
    ids = {new_machine_id() for _ in range(64)}
    assert len(ids) == 64
    for value in ids:
        assert machine_module._ID_RE.fullmatch(value)


def test_facts_round_trip_and_bad_records_read_as_none() -> None:
    facts = MachineFacts("HW", None, ("1", "2", None))
    assert MachineFacts.from_dict(facts.to_dict()) == facts
    assert MachineFacts.from_dict("garbage") is None
    assert MachineFacts.from_dict({"hardware": 3, "id_file": [1]}) == MachineFacts(
        None, None, None
    )


def test_the_platform_facts_are_read_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def reader() -> str:
        calls.append(1)
        return "HW-X"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(machine_module, "_linux_hardware", reader)
    machine_module._clear_hardware()
    try:
        assert machine_module.read_hardware_id() == "HW-X"
        assert machine_module.read_hardware_id() == "HW-X"
        assert calls == [1]
    finally:
        machine_module._clear_hardware()


def test_the_platform_reader_answers_text_or_none() -> None:
    machine_module._clear_hardware()
    try:
        value = machine_module.read_hardware_id()
        assert value is None or (isinstance(value, str) and value)
        assert machine_module.read_hardware_id() == value
    finally:
        machine_module._clear_hardware()
    name = machine_module.current_hostname()
    assert name is None or isinstance(name, str)


def test_the_linux_machine_id_is_read_from_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty, named = tmp_path / "empty", tmp_path / "machine-id"
    empty.write_text("\n", encoding="ascii")
    named.write_text("abc123\n", encoding="ascii")
    missing = str(tmp_path / "missing")
    monkeypatch.setattr(
        machine_module, "_LINUX_MACHINE_ID", (missing, str(empty), str(named))
    )
    assert machine_module._linux_hardware() == "abc123"
    monkeypatch.setattr(machine_module, "_LINUX_MACHINE_ID", (missing,))
    assert machine_module._linux_hardware() is None


# ---------------------------------------------------------------------------
# The store id and the fork rule (design 8.7, E26 e)
# ---------------------------------------------------------------------------


def test_the_store_id_is_minted_with_each_store(tmp_path: Path, store: Store) -> None:
    other = Store.create(
        tmp_path / "other" / "memory.sqlite", keys_dir=tmp_path / "keys2"
    )
    try:
        assert len(store.membership_store_id) == 32
        assert store.membership_store_id != other.membership_store_id
    finally:
        other.close()


def test_the_fork_rule_reads_the_store_id_and_what_the_store_wrote(
    store: Store,
) -> None:
    own = _memory()
    store.put_memory(own)
    mine = store.membership_store_id
    # This store's own export brought back: no fork.
    assert (
        check_import_fork(store, record_store_id=mine, record_id=own.id, source="x")
        is None
    )
    # Another store's record, or one without a store id: no fork.
    assert (
        check_import_fork(store, record_store_id="f" * 32, record_id="R", source="x")
        is None
    )
    assert (
        check_import_fork(store, record_store_id=None, record_id="R", source="x")
        is None
    )
    assert "store_forked" not in _kinds(store)
    # A record carrying this store's id that it never wrote: a fork.
    foreign = generate_ulid()
    forked = check_import_fork(
        store, record_store_id=mine, record_id=foreign, source="clone.json"
    )
    assert forked is not None
    old, new = forked
    assert old == mine and new == store.membership_store_id != mine
    assert _kinds(store) == ["store_forked"]
    # The old id names the other copy now: its records are another store's.
    assert (
        check_import_fork(store, record_store_id=mine, record_id="R2", source="x")
        is None
    )


def test_i37a_a_block_clone_keeps_one_machine_id_and_an_import_forks(
    tmp_path: Path,
) -> None:
    image = tmp_path / "image"
    state = image / "state"
    machine = _open(state)
    created = Store.create(image / "store" / "memory.sqlite", keys_dir=image / "keys")
    created.close()
    clone = tmp_path / "clone"
    shutil.copytree(image, clone)
    a = Store.open(image / "store" / "memory.sqlite", keys_dir=image / "keys")
    b = Store.open(clone / "store" / "memory.sqlite", keys_dir=clone / "keys")
    try:
        assert a.membership_store_id == b.membership_store_id
        # Every fact equal on the booted image: one machine id, no row.
        assert _open(state).id == machine.id
        written = _memory()
        b.put_memory(written)
        forked = check_import_fork(
            a,
            record_store_id=b.membership_store_id,
            record_id=written.id,
            source="clone export",
        )
        assert forked is not None
        assert a.membership_store_id != b.membership_store_id
        assert a.log_verify()["status"] == "ok"
    finally:
        a.close()
        b.close()
