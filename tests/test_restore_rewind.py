"""The range a restore rewound stays known, or known to be unknown
(ENG-103).

A restore records where the log it replaced ended, so `log export` can
warn a cursor inside the rewound range (`audit_export.rewinds_across`).
It took that from the head checkpoint and from the store at the restore's
path. A restore into a directory that held no copy of the store, with the
head checkpoint gone, recorded neither end, and a restore onto a host
that held no copy at all could not know one: in both the export warned
of nothing, and a cursor taken before the restore skipped the new rows.
The store the key ring is bound to is now read as well, and a record
that names neither end reads as a rewind from an unknown seq.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import audit_export
from bettermemory.backup import create_backup, restore_bundle, restore_record_path
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def _grow_past(store_file: Path, seq: int) -> None:
    with Store.open(store_file) as opened:
        n = 0
        while opened.log_rows()[-1].seq <= seq:
            opened.put_memory(_memory(f"the new history {n}"))
            n += 1


def _older_bundle(
    store_file: Path, *, include_key: bool = False
) -> tuple[Path, int, int, str]:
    """A store with a bundle taken at seq `rewound` and rows written after
    it up to seq `cursor`, where an export cursor taken now would stand."""
    with Store.create(store_file) as created:
        created.put_memory(_memory("before the bundle"))
        store_id = created.store_id
    bundle = Path(create_backup(store_file, include_key=include_key)["bundle"])
    with Store.open(store_file) as opened:
        rewound = opened.log_rows()[-1].seq
        for n in range(3):
            opened.put_memory(_memory(f"after the bundle {n}"))
        cursor = opened.log_rows()[-1].seq
    return bundle, rewound, cursor, store_id


def test_a_restore_into_a_directory_with_no_copy_reads_the_bound_stores_end(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The store the bundle came from is still where the key ring is
    bound, and the head checkpoint is gone (the `origin_mismatch`
    recovery, or a keys directory put back with only its key). The
    restore goes to a new directory: the plan and the record take the
    replaced log's end from the bound store, and the export warns a
    cursor inside the rewound range."""
    old = tmp_path / "old" / STORE_FILENAME
    bundle, rewound, cursor, store_id = _older_bundle(old)
    with Store.open(old) as opened:
        opened.keyring.head_path().unlink()
    new = tmp_path / "new" / STORE_FILENAME

    plan = restore_bundle(bundle, new, apply=False)["plan"]
    assert plan["current_store_id"] is None
    assert plan["head"]["before"] is None
    assert plan["log_end_before"] == {"seq": cursor, "path": str(old.resolve())}

    report = restore_bundle(bundle, new, apply=True)
    assert report["status"] == "restored", report["detail"]
    [entry] = json.loads(restore_record_path(keys_dir, store_id).read_text())[
        "restores"
    ]
    assert entry["head_before"] is None
    assert entry["log_end_before"]["seq"] == cursor

    monkeypatch.setenv("BETTERMEMORY_DIR", str(new.parent))
    _grow_past(new, cursor)
    assert _run_cli(["log", "export", "--since-seq", str(cursor)], monkeypatch) == 0
    err = capsys.readouterr().err
    assert f"rewound the log from seq {cursor} to seq {rewound}" in err
    assert f"--since-seq {rewound}" in err


def test_a_restore_onto_a_host_with_no_copy_warns_from_an_unknown_seq(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A new machine: no store, no head checkpoint, no binding, the key
    from the bundle. Nothing here knows how far the replaced log went, so
    the record names neither end, and the export warns every cursor past
    the restored head, naming the seq to export again from."""
    bundle, rewound, cursor, store_id = _older_bundle(
        tmp_path / "old" / STORE_FILENAME, include_key=True
    )
    new = tmp_path / "new-machine" / "store" / STORE_FILENAME
    new_keys = tmp_path / "new-machine" / "keys"
    monkeypatch.setenv(KEYS_DIR_ENV, str(new_keys))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(new.parent))
    report = restore_bundle(bundle, new, apply=True, trust_bundle_key=True)
    assert report["status"] == "restored", report["detail"]
    [entry] = json.loads(restore_record_path(new_keys, store_id).read_text())[
        "restores"
    ]
    assert entry["head_before"] is None and entry["log_end_before"] is None

    _grow_past(new, cursor)
    assert _run_cli(["log", "export", "--since-seq", str(cursor)], monkeypatch) == 0
    err = capsys.readouterr().err
    assert f"put the log back to seq {rewound} on a host that held no copy" in err
    assert f"--since-seq {rewound}" in err
    # A cursor at the restored head lies before the rewound range.
    assert _run_cli(["log", "export", "--since-seq", str(rewound)], monkeypatch) == 0
    assert "put the log back" not in capsys.readouterr().err


def test_an_entry_with_neither_end_rewinds_from_an_unknown_seq(tmp_path: Path) -> None:
    path = restore_record_path(tmp_path, "s" * 32)
    path.write_text(
        json.dumps(
            {
                "restores": [
                    {
                        "restored_at": "t",
                        "head_before": None,
                        "head_after": {"seq": 2},
                        "log_end_before": None,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert audit_export.rewinds_across(tmp_path, "s" * 32, 3) == [
        {
            "kind": "store_restored",
            "restored_at": "t",
            "from_seq": None,
            "to_seq": 2,
            "seq": None,
        }
    ]
    assert audit_export.rewinds_across(tmp_path, "s" * 32, 2) == []
