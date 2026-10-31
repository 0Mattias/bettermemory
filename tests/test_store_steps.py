"""The store's numbered schema steps and the runner that applies them
(ENG-107).

A step is numbered, recorded in meta and idempotent. One that is not
additive runs only after a backup bundle of the store as it stood, under
the store's write lock and in one transaction with its record, so a crash
anywhere leaves the store as it was. A later build is simulated by adding
a step 2 to `store_steps.STEPS`, the tuple `run_steps` reads, and raising
`store_schema.SCHEMA_REVISION`, which `check_versions` reads. Both are
patched by their dotted names, and the runner module is imported when a
test runs, so a build without the runner fails each test here rather than
the module's collection.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import bettermemory
from bettermemory import backup, store_schema
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

from .conftest import shielded_child_env

# The contract's meta keys (CONTRIBUTING.md), spelled as a build from
# another release would find them in the file.
SCHEMA_REVISION_KEY = "schema_revision"
MIN_READER_KEY = "min_reader_revision"
MIN_WRITER_KEY = "min_writer_revision"
SCHEMA_STEPS_KEY = "schema_steps"
_VERSION_KEYS = (SCHEMA_REVISION_KEY, MIN_READER_KEY, MIN_WRITER_KEY)

_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
#: The table the simulated step 2 adds, which no build defines.
LATER_TABLE = "later_notes"

# This build's revision and the minimums a store it creates carries, and
# the revision a later build's step takes a store to.
OURS = store_schema.SCHEMA_REVISION
OURS_KEYS = [
    str(OURS),
    str(store_schema.MIN_READER_REVISION),
    str(store_schema.MIN_WRITER_REVISION),
]
LATER = OURS + 1

Apply = Callable[[sqlite3.Connection, Path, dict[str, str]], None]


def runner() -> ModuleType:
    return importlib.import_module("bettermemory.store_steps")


def _memory(body: str) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
    )


@contextlib.contextmanager
def later_build(
    apply: Apply,
    *,
    additive: bool = False,
    reader: int = LATER,
    writer: int = LATER,
    version: str = "9.1.0",
) -> Iterator[None]:
    """A build one step ahead of this one for the length of the block: its
    step `LATER` runs `apply`, and a store it creates or steps carries
    `reader` and `writer` as its minimums, never lower than they were."""
    steps = runner()
    step = steps.Step(
        number=LATER,
        name="a later build's step",
        additive=additive,
        reader=reader,
        writer=writer,
        apply=apply,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bettermemory.store_steps.STEPS", (*steps.STEPS, step))
        patch.setattr("bettermemory.store_schema.SCHEMA_REVISION", LATER)
        patch.setattr("bettermemory.store_schema.MIN_READER_REVISION", reader)
        patch.setattr("bettermemory.store_schema.MIN_WRITER_REVISION", writer)
        patch.setattr(bettermemory, "__version__", version)
        yield


def adds_a_table(calls: list[str]) -> Apply:
    """A step's `apply` that is not additive in kind: it writes rows a
    store carried nowhere before, inside the runner's transaction."""

    def apply(conn: sqlite3.Connection, path: Path, meta: dict[str, str]) -> None:
        calls.append(str(path))
        conn.execute(f"CREATE TABLE {LATER_TABLE} (note TEXT NOT NULL)")
        conn.execute(f"INSERT INTO {LATER_TABLE} (note) VALUES ('stepped')")

    return apply


def meta_of(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def tables_of(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def bundles(path: Path) -> list[Path]:
    """The backup bundles written beside the store, oldest first."""
    root = backup.default_backups_dir(path)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / backup.MANIFEST_NAME).is_file())


def steps_of(path: Path) -> list[dict[str, Any]]:
    raw = meta_of(path).get(SCHEMA_STEPS_KEY)
    return [] if raw is None else list(json.loads(raw))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    """A store at revision 1 holding one memory, written by this build."""
    store_file = tmp_path / "store" / STORE_FILENAME
    with Store.create(store_file) as store:
        store.put_memory(_memory("A memory the store held before any later step."))
    return store_file


def test_the_registry_is_numbered_and_names_this_builds_revision() -> None:
    """Steps are numbered from 1 without a gap, the last is this build's
    revision, and the minimums a new store carries are the highest any
    step names: a store this build creates is one that had every step."""
    steps = runner().STEPS
    numbers = [step.number for step in steps]
    assert numbers == list(range(1, len(steps) + 1))
    assert numbers[-1] == store_schema.SCHEMA_REVISION
    assert max(step.reader for step in steps) == store_schema.MIN_READER_REVISION
    assert max(step.writer for step in steps) == store_schema.MIN_WRITER_REVISION
    for step in steps:
        assert step.reader <= step.number
        assert step.writer <= step.number
    first = steps[0]
    assert first.additive is True
    assert first.apply is store_schema._add_membership_tables
    assert runner().SCHEMA_STEPS_KEY == SCHEMA_STEPS_KEY


def test_a_new_store_has_had_every_step_and_lists_none(path: Path) -> None:
    meta = meta_of(path)
    assert meta[SCHEMA_REVISION_KEY] == str(store_schema.SCHEMA_REVISION)
    assert SCHEMA_STEPS_KEY not in meta
    with Store.open(path, allow_rekey=True):
        pass
    assert SCHEMA_STEPS_KEY not in meta_of(path)
    assert bundles(path) == []


def test_a_store_from_before_the_runner_has_step_one_recorded_at_a_writing_open(
    path: Path,
) -> None:
    """A store an earlier candidate wrote carries no revision. A reader's
    open applies no step and records nothing; the owner's open records
    every step this build knows, each additive, with no backup."""
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executemany(
            "DELETE FROM meta WHERE key = ?", [(k,) for k in _VERSION_KEYS]
        )
    conn.close()
    with Store.open(path) as reader:
        assert reader.count_memories() == 1
    assert not set(_VERSION_KEYS) & set(meta_of(path))
    with Store.open(path, allow_rekey=True) as owner:
        assert owner.count_memories() == 1
    meta = meta_of(path)
    assert [meta.get(k) for k in _VERSION_KEYS] == OURS_KEYS
    entries = steps_of(path)
    assert [entry["step"] for entry in entries] == [
        step.number for step in runner().STEPS
    ]
    assert [entry["name"] for entry in entries] == [
        step.name for step in runner().STEPS
    ]
    assert all(entry["backup"] is None for entry in entries)
    assert all(entry["by"] == bettermemory.__version__ for entry in entries)
    assert bundles(path) == []


def test_an_additive_step_runs_once_without_a_backup(path: Path) -> None:
    calls: list[str] = []

    def apply(conn: sqlite3.Connection, store_file: Path, meta: dict[str, str]) -> None:
        calls.append(str(store_file))
        conn.execute(f"CREATE TABLE IF NOT EXISTS {LATER_TABLE} (note TEXT)")

    with later_build(apply, additive=True, reader=1, writer=1):
        with Store.open(path, allow_rekey=True):
            pass
        with Store.open(path, allow_rekey=True):
            pass
    assert calls == [str(path)]
    assert LATER_TABLE in tables_of(path)
    meta = meta_of(path)
    assert [meta[k] for k in _VERSION_KEYS] == [str(LATER), *OURS_KEYS[1:]]
    assert [(e["step"], e["backup"]) for e in steps_of(path)] == [(LATER, None)]
    assert bundles(path) == []
    # The minimums stayed where this build's are, so it still writes the
    # store.
    with Store.open(path, allow_rekey=True) as older:
        older.put_memory(_memory("This build still writes after an additive step."))


def test_a_step_that_is_not_additive_runs_after_a_backup_of_the_store_before_it(
    path: Path,
) -> None:
    calls: list[str] = []
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True) as newer:
            assert newer.count_memories() == 1
    assert calls == [str(path)]
    meta = meta_of(path)
    assert [meta[k] for k in _VERSION_KEYS] == [str(LATER)] * 3
    assert meta["engine_version"] == "9.1.0"
    assert LATER_TABLE in tables_of(path)

    (bundle,) = bundles(path)
    (entry,) = steps_of(path)
    assert (entry["step"], entry["by"]) == (LATER, "9.1.0")
    assert entry["backup"] == str(bundle.relative_to(path.parent))
    # The build that took the bundle verifies it: its open of the snapshot,
    # a store waiting on its step, reads it as it stands.
    with later_build(adds_a_table(calls)):
        report = backup.verify_bundle(bundle)
    assert report["status"] == backup.OK, report["problems"]
    # The bundle is the store as it was before the step.
    snapshot = bundle / backup.SNAPSHOT_NAME
    assert LATER_TABLE not in tables_of(snapshot)
    before = meta_of(snapshot)
    assert [before[k] for k in _VERSION_KEYS] == OURS_KEYS
    assert SCHEMA_STEPS_KEY not in before

    # Idempotent: the step is recorded, so no open runs or backs it up again.
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True):
            pass
        with Store.open(path):
            pass
    assert calls == [str(path)]
    assert len(bundles(path)) == 1

    # This build, a revision behind, no longer reads the stepped store.
    with pytest.raises(ValueError) as refused:
        Store.open(path)
    assert type(refused.value).__name__ == "StoreVersionError"
    assert f"needs a reader at schema revision {LATER}" in str(refused.value)


def test_a_readers_open_of_a_store_waiting_on_a_step_reads_and_writes_nothing(
    path: Path,
) -> None:
    """A reader's open runs no step: of a store waiting on one that is not
    additive it reads the store as it stands, every write is refused, and
    nothing is backed up or written."""
    calls: list[str] = []
    before = meta_of(path)
    with later_build(adds_a_table(calls)):
        with Store.open(path) as reader:
            assert reader.count_memories() == 1
            assert reader.log_verify()["status"] == "ok"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.put_memory(
                    _memory("A reader of a waiting store writes nothing.")
                )
    assert calls == []
    assert LATER_TABLE not in tables_of(path)
    assert bundles(path) == []
    assert meta_of(path) == before
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True):
            pass
        with Store.open(path) as reader:
            assert reader.count_memories() == 1
    assert calls == [str(path)]


def test_the_build_that_ran_a_step_restores_the_bundle_it_took(path: Path) -> None:
    """`backup restore` under the build that ran the step puts back the store
    as it was before it: the restore verifies the bundle and opens the
    restored store, both a store waiting on the step, as a reader."""
    calls: list[str] = []
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True) as newer:
            newer.put_memory(_memory("A memory written after the step."))
        (bundle,) = bundles(path)
        report = backup.restore_bundle(bundle, path, apply=True)
        assert report["status"] == backup.RESTORED, report["detail"]
        assert LATER_TABLE not in tables_of(path)
        assert meta_of(path)[SCHEMA_REVISION_KEY] == str(OURS)
        with Store.open(path) as reader:
            assert reader.count_memories() == 1
    # This build, which the step left behind, reads and writes it again.
    with Store.open(path, allow_rekey=True) as older:
        older.put_memory(_memory("This build writes the restored store."))
        assert older.count_memories() == 2


def test_a_step_whose_backup_fails_does_not_run(path: Path) -> None:
    def fails(*args: object, **kwargs: object) -> dict[str, Any]:
        raise backup.BackupError("no room for the bundle")

    calls: list[str] = []
    before = meta_of(path)
    with later_build(adds_a_table(calls)), pytest.MonkeyPatch.context() as patch:
        patch.setattr("bettermemory.backup.create_backup", fails)
        with pytest.raises(ValueError) as refused:
            Store.open(path, allow_rekey=True)
    message = str(refused.value)
    assert type(refused.value).__name__ == "StoreVersionError"
    assert "runs only after a backup, and the backup failed" in message
    assert "no room for the bundle; the store is unchanged" in message
    assert calls == []
    assert meta_of(path) == before
    assert LATER_TABLE not in tables_of(path)
    with Store.open(path, allow_rekey=True) as store:
        assert store.log_verify()["status"] == "ok"


def test_a_step_whose_backup_does_not_verify_does_not_run(path: Path) -> None:
    """The runner verifies the bundle under the lock before the step: one
    altered after it was written reads mismatched, and the step does not
    run, though the bundle stays for a look."""
    made = backup.create_backup

    def altered(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = made(*args, **kwargs)
        snapshot = Path(result["bundle"]) / backup.SNAPSHOT_NAME
        with snapshot.open("ab") as handle:
            handle.write(b"altered after the manifest was written")
        return result

    calls: list[str] = []
    before = meta_of(path)
    with later_build(adds_a_table(calls)), pytest.MonkeyPatch.context() as patch:
        patch.setattr("bettermemory.backup.create_backup", altered)
        with pytest.raises(ValueError) as refused:
            Store.open(path, allow_rekey=True)
    message = str(refused.value)
    assert type(refused.value).__name__ == "StoreVersionError"
    assert f"needs schema step {LATER} (a later build's step); its backup" in message
    assert "reads mismatched" in message
    assert "so the step did not run and the store is unchanged" in message
    assert calls == []
    assert meta_of(path) == before
    assert LATER_TABLE not in tables_of(path)
    assert len(bundles(path)) == 1
    with Store.open(path, allow_rekey=True) as store:
        assert store.log_verify()["status"] == "ok"


def test_a_step_another_process_ran_meanwhile_is_not_run_again(path: Path) -> None:
    """The runner reads the revision again under the write lock: an opener
    whose meta was read before another process ran the step backs up
    nothing and applies nothing, and takes the store's record as it is."""
    calls: list[str] = []
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True):
            pass
        stale = dict(zip(_VERSION_KEYS, OURS_KEYS, strict=True))
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            ran = runner().run_steps(conn, path, stale, writing=True)
        finally:
            conn.close()
    assert ran == ()
    assert stale[SCHEMA_REVISION_KEY] == str(LATER)
    assert calls == [str(path)]
    assert len(bundles(path)) == 1


_CRASH = """\
import os
import sys
from pathlib import Path

import bettermemory
from bettermemory import store_schema, store_steps
from bettermemory.store import Store


def apply(conn, path, meta):
    conn.execute("CREATE TABLE later_notes (note TEXT NOT NULL)")
    conn.execute("INSERT INTO later_notes (note) VALUES ('half')")
    os._exit(17)


LATER = store_schema.SCHEMA_REVISION + 1
store_steps.STEPS = (
    *store_steps.STEPS,
    store_steps.Step(
        number=LATER,
        name="a later build's step",
        additive=False,
        reader=LATER,
        writer=LATER,
        apply=apply,
    ),
)
store_schema.SCHEMA_REVISION = LATER
bettermemory.__version__ = "9.1.0"
Store.open(Path(sys.argv[1]), allow_rekey=True)
sys.exit(3)
"""


def test_a_crash_between_the_backup_and_the_step_leaves_the_store_readable(
    path: Path, tmp_path: Path
) -> None:
    """The process dies inside the step, after the bundle is written and
    before the step's transaction commits: the store is the one every
    build read before, and the next owner's open backs it up again and
    runs the step."""
    script = tmp_path / "crash_in_step.py"
    script.write_text(_CRASH, encoding="utf-8")
    env = shielded_child_env()
    # The child imports the package this test imported (a fail-before run
    # points PYTHONPATH at another checkout's src).
    src = str(Path(bettermemory.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env["PYTHONPATH"]
    child = subprocess.run(
        [sys.executable, str(script), str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 17, child.stderr

    (bundle,) = bundles(path)
    with later_build(adds_a_table([])):
        assert backup.verify_bundle(bundle)["status"] == backup.OK
    meta = meta_of(path)
    assert [meta[k] for k in _VERSION_KEYS] == OURS_KEYS
    assert SCHEMA_STEPS_KEY not in meta
    assert LATER_TABLE not in tables_of(path)
    with Store.open(path) as reader:
        assert reader.count_memories() == 1
        assert reader.log_verify()["status"] == "ok"
    with Store.open(path, allow_rekey=True) as owner:
        owner.put_memory(_memory("This build still writes the store after the crash."))

    calls: list[str] = []
    with later_build(adds_a_table(calls)):
        with Store.open(path, allow_rekey=True) as newer:
            assert newer.count_memories() == 2
    assert calls == [str(path)]
    assert len(bundles(path)) == 2
    (entry,) = steps_of(path)
    assert entry["backup"] == str(bundles(path)[1].relative_to(path.parent))
    assert meta_of(path)[SCHEMA_REVISION_KEY] == str(LATER)
