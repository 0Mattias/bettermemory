"""Optional target metadata preserves legacy fields and a stamp's meaning."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from bettermemory.store import Store, MemoryNotFoundError
from bettermemory.handlers.restore import trust_strip_for
from bettermemory.origin import Origin
from bettermemory.migrate_v8 import migrate
from bettermemory.mirror import write_mirror

from .test_store import _memory


def test_target_metadata_round_trips_and_refolds_without_rewriting_legacy_fields(
    tmp_path: Path,
) -> None:
    raw = "src/app.py:2"
    targets = {raw: "src/app.py"}
    absent = {"routes/[slug]": "routes/[slug]"}
    with Store.create(tmp_path / "store.sqlite") as store:
        record = _memory(
            "a checked record",
            verified_paths=[raw],
            verified_absent_paths=["routes/[slug]"],
            verified_path_targets=targets,
            verified_absent_path_targets=absent,
        )
        store.put_memory(record)
        reads = [
            store.get_memory(record.id),
            *store.get_many([record.id]),
            *store.load_many([record.id]),
            *store.iter_memories(),
            *(row.memory for row in store.iter_memory_rows()),
        ]
        assert all(memory.verified_path_targets == targets for memory in reads)
        for _ in range(2):
            loaded = store.load_one(record.id)
            assert loaded is not None
            assert loaded.verified_paths == [raw]
            assert loaded.verified_absent_paths == ["routes/[slug]"]
            assert loaded.verified_path_targets == targets
            assert loaded.verified_absent_path_targets == absent
            assert store.log_verify()["status"] == "ok"
            assert store.refold()["status"] == "ok"


def test_a_replacement_stamp_without_targets_uses_legacy_grammar(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        record = store.put_memory(_memory("a checked record"))
        verified = store.mark_verified(
            record.id,
            verified_paths=[],
            verified_path_targets={"app.py,": "app.py,"},
        )
        assert verified.last_verified_at is not None
        store.put_verification(
            record.id, verified_at=verified.last_verified_at + timedelta(seconds=1)
        )
        stamp = store.verifications_for([record.id])[record.id][0]
        assert "verified_path_targets" not in stamp
        assert store.load_one(record.id).verified_path_targets == {"app.py,": "app.py,"}
        assert store.log_verify()["status"] == "ok"


def test_a_record_cannot_borrow_targets_from_another_records_log_row(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        checked = store.put_memory(
            _memory("checked", verified_path_targets={"app.py,": "app.py,"})
        )
        other = store.put_memory(_memory("another record"))
        store._conn.execute(
            "UPDATE memories SET log_mac = (SELECT log_mac FROM memories WHERE id = ?) "
            "WHERE id = ?",
            (checked.id, other.id),
        )
        assert store.load_one(other.id).verified_path_targets is None
        assert store.log_verify()["status"] == "tampered"


def test_preserving_and_replacing_lists_keep_target_metadata_consistent(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        record = store.put_memory(_memory("a checked record"))
        first = store.mark_verified(
            record.id,
            verified_paths=["app.py:2"],
            verified_path_targets={"app.py:2": "app.py"},
        )
        assert first.verified_path_targets == {"app.py:2": "app.py"}
        assert (
            store.mark_verified(record.id).verified_path_targets
            == first.verified_path_targets
        )
        stamps = store.verifications_for([record.id])[record.id]
        assert stamps[0]["verified_path_targets"] == first.verified_path_targets
        assert (
            store.mark_verified(
                record.id, verified_paths=["other.py"]
            ).verified_path_targets
            is None
        )
        assert store.log_verify()["status"] == "ok"


def test_tombstone_and_restore_preserve_the_compiled_target(tmp_path: Path) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        record = store.put_memory(
            _memory(
                "a checked record",
                verified_paths=[],
                verified_path_targets={"app.py,": "app.py,"},
            )
        )
        store.tombstone(record.id, reason="test")
        assert store.get_tombstone(record.id).verified_path_targets == {
            "app.py,": "app.py,"
        }
        restored = store.restore(record.id)
        assert restored.verified_path_targets == {"app.py,": "app.py,"}
        assert store.log_verify()["status"] == "ok"


def test_unset_target_fields_are_absent_from_the_signed_record_payload(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        record = store.put_memory(_memory("an ordinary record"))
        row = store.conn.execute(
            "SELECT payload FROM log WHERE kind = 'memory_put' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row[0])["memory"]
        assert payload["id"] == record.id
        assert "verified_path_targets" not in payload
        assert "verified_absent_path_targets" not in payload


def test_a_mismatched_target_map_is_refused_before_it_can_be_recorded(
    tmp_path: Path,
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        record = _memory(
            "a checked record",
            verified_paths=["app.py"],
            verified_path_targets={"other.py": "app.py"},
        )
        before = store.conn.execute("SELECT count(*) FROM log").fetchone()[0]
        with pytest.raises(ValueError, match="must cover every legacy"):
            store.put_memory(record)
        with pytest.raises(MemoryNotFoundError):
            store.load_one(record.id)
        assert store.conn.execute("SELECT count(*) FROM log").fetchone()[0] == before


def test_mirror_migration_preserves_optional_targets_on_active_and_removed_records(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    with Store.create(tmp_path / "first.sqlite", keys_dir=keys) as store:
        active = store.put_memory(
            _memory(
                "active literal target", verified_path_targets={"app.py,": "app.py,"}
            )
        )
        dead = store.put_memory(
            _memory(
                "removed literal target",
                verified_absent_path_targets={"routes/[slug]": "routes/[slug]"},
            )
        )
        store.tombstone(dead.id, reason="test")
        write_mirror(store, tmp_path / "mirror")
    migrate(tmp_path / "mirror", tmp_path / "second.sqlite", keys_dir=keys)
    with Store.open(
        tmp_path / "second.sqlite", keys_dir=keys, allow_rekey=True
    ) as copied:
        assert copied.load_one(active.id).verified_path_targets == {
            "app.py,": "app.py,"
        }
        assert copied.get_tombstone(dead.id).verified_absent_path_targets == {
            "routes/[slug]": "routes/[slug]"
        }
        assert copied.log_verify()["status"] == "ok"


def test_restore_strips_a_missing_frozen_target_using_its_input_key(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py,"
    target.write_text("literal\n")
    with Store.create(tmp_path / "memory.sqlite") as store:
        record = store.put_memory(
            _memory(
                "a checked literal",
                origin=Origin(worktree_root=str(tmp_path)),
                verified_path_targets={"app.py,": "app.py,"},
            )
        )
        store.mark_verified(record.id)
        store.tombstone(record.id, reason="test")
        target.unlink()
        dead = store.get_tombstone(record.id)
        strip = trust_strip_for(dead)
        assert strip.verified_paths == ["app.py,"]
        restored = store.restore_trimmed(
            record.id,
            drop_verified_paths=strip.verified_paths,
            clear_verification=strip.any,
        )
        assert restored.verified_path_targets == {}
        assert restored.last_verified_at is None
        assert store.log_verify()["status"] == "ok"
