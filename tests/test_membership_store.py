"""The membership tables of the store (RS-F6, design 8.5 and 8.10).

Every membership change is a mutation row of the log, applied by the one
`_apply_mutation` the live path and the fold share, so `log verify`
covers membership as it covers memories: a planted home, an edited file
binding and an edited proposal answer are reported unaccounted. A
record's home is written in the record's own transaction and never by
`Store.update`; a merge is one project row. The tables arrive by an
additive step at open, so a store an rc3 build created gains them and an
rc3 build still reads what it wrote.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import store as store_module
from bettermemory.membership import (
    BLOBS_PER_WITNESS,
    GLOBAL,
    KEEP_OLDEST,
    REPO,
    WITNESS_BOUND,
    Location,
    Membership,
    ObjectKey,
)
from bettermemory.models import Confidence, Episode, Memory, Source, generate_ulid
from bettermemory.store import (
    FOLDED_TABLES,
    MEMBERSHIP_EVENT_KINDS,
    MEMBERSHIP_MUTATION_KINDS,
    MEMBERSHIP_STORE_ID,
    MEMBERSHIP_TABLES,
    MUTATION_KINDS,
    SCHEMA,
    SCHEMA_VERSION,
    RecordHome,
    StandingAnswer,
    Store,
)
from bettermemory.location_git import reachable_among

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

#: The mutation kinds an rc3 build folds, as its store module names them.
_RC3_MUTATION_KINDS = frozenset(
    f"{table}_{op}"
    for table in (
        "memory",
        "tombstone",
        "episode",
        "verification",
        "conflict",
        "import",
        "quarantine",
    )
    for op in ("put", "delete")
)
_RC3_FOLDED_TABLES = (
    "memories",
    "memory_links",
    "tombstones",
    "episodes",
    "verifications",
    "conflicts",
    "imports",
    "quarantine",
)


def _memory(body: str = "alpha") -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
    )


def _episode(session: str = "s1") -> Episode:
    return Episode(id=generate_ulid(), session_id=session, created=_NOW, body="ep\n")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.create(tmp_path / "memory.sqlite", keys_dir=tmp_path / "keys")
    yield s
    s.close()


def _outside(store: Store) -> sqlite3.Connection:
    """A second connection with sqlite3 and no key: the editor the log
    exists to expose."""
    conn = sqlite3.connect(str(store.path))
    conn.row_factory = sqlite3.Row
    return conn


def _project(store: Store, pid: str, **extra: Any) -> None:
    row: dict[str, Any] = {
        "id": pid,
        "name": pid.lower(),
        "merged_into": None,
        "provisional": None,
        "parent": None,
        "tenant": None,
        "folder": 0,
        "boundary": 0,
        "minted": "t0",
    }
    row.update(extra)
    store.put_membership("project", row)


_HOME = RecordHome("P001", "K001", "object")

#: One row of every membership table, by its mutation kind prefix.
_ROWS: dict[str, dict[str, Any]] = {
    "machine": {
        "id": "MACHINE1",
        "facts_json": {"hardware": "HW", "hostname": "host", "id_file": None},
        "predecessor": None,
        "minted": "t0",
    },
    "project": {
        "id": "P001",
        "name": "app",
        "merged_into": None,
        "provisional": None,
        "parent": None,
        "tenant": None,
        "folder": False,
        "boundary": False,
        "minted": "t0",
    },
    "project_witness": {
        "project": "P001",
        "commit_id": "c" * 40,
        "arrival": 1,
        "source": "head@K001",
        "fingerprint": "f" * 32,
    },
    "project_blob": {"project": "P001", "blob": "b" * 40, "witness": "c" * 40},
    "project_remote": {"project": "P001", "remote_key": "example.invalid/acme/app"},
    "project_file": {"file_id": "bmp_X", "project": "P001", "evidence": "init"},
    "declared_remote": {
        "remote_key": "example.invalid/acme/app",
        "project": "P001",
        "principal": "os:ci",
    },
    "checkout": {
        "id": "K001",
        "project": "P001",
        "machine": "MACHINE1",
        "kind": "primary",
        "object_device": "1",
        "object_inode": "2",
        "object_birth": "3",
        "common_device": "1",
        "common_inode": "2",
        "common_birth": "3",
        "git_dir": "/w/app/.git",
        "token": "TOKEN",
        "token_status": "written",
        "stamps_json": [["description", 5, 1, 1, 73]],
        "last_head": "c" * 40,
        "branch": "main",
        "remotes_json": ["example.invalid/acme/app"],
        "manifest_json": None,
        "legacy": 0,
        "retired": 0,
        "registered": "object",
    },
    "checkout_view": {
        "checkout": "K001",
        "machine": "MACHINE1",
        "path": "/w/app",
        "first_seen": "t0",
        "last_seen": "t0",
        "seen_order": 1,
    },
    "home": {
        "record_id": "R1",
        "kind": "memory",
        "project": "P001",
        "checkout": "K001",
        "rule": "object",
        "decided": "t0",
    },
    "proposal": {
        "id": "Q001",
        "checkout": "K001",
        "kind": "first_sight",
        "candidates_json": ["P001"],
        "evidence_json": {"P001": ["H"]},
        "reason": "one_witness",
        "line": "bettermemory: ...",
        "state": "open",
        "answer": None,
        "answered_by": None,
        "signature_json": ["c" * 40, [], None, "TOKEN", False],
        "opened": "t0",
    },
    "standing": {
        "record_id": "01M5K000000000000000000000",
        "probability": 0.5,
        "body_sha256": "d" * 64,
        "source": "asked",
        "answered_by": "test/test",
        "answered": "t0",
    },
    "returned": {
        "record_id": "01M5K000000000000000000000",
        "via": "01M5K000000000000000000001",
        "returned": "t0",
    },
}


def _key(kind: str) -> dict[str, Any]:
    spec = store_module._MEMBERSHIP_SPECS[kind]
    return {column: _ROWS[kind][column] for column in spec.key}


# ---------------------------------------------------------------------------
# The tables and their rows
# ---------------------------------------------------------------------------


def test_the_membership_tables_and_kinds() -> None:
    assert set(MEMBERSHIP_TABLES) == {
        "machines",
        "projects",
        "project_witnesses",
        "project_blobs",
        "project_remotes",
        "project_files",
        "declared_remotes",
        "checkouts",
        "checkout_views",
        "homes",
        "proposals",
        "standing",
        "returned",
    }
    assert MEMBERSHIP_MUTATION_KINDS <= MUTATION_KINDS
    assert MUTATION_KINDS - MEMBERSHIP_MUTATION_KINDS == _RC3_MUTATION_KINDS
    assert tuple(FOLDED_TABLES) == _RC3_FOLDED_TABLES
    assert "store_forked" in MEMBERSHIP_EVENT_KINDS
    assert not MEMBERSHIP_EVENT_KINDS & MUTATION_KINDS
    assert set(_ROWS) == set(store_module._MEMBERSHIP_SPECS)


@pytest.mark.parametrize("kind", sorted(_ROWS))
def test_every_membership_row_is_put_read_and_deleted_through_the_log(
    store: Store, kind: str
) -> None:
    spec = store_module._MEMBERSHIP_SPECS[kind]
    put = store.put_membership(kind, _ROWS[kind])
    assert put.kind == f"{kind}_put"
    got = store.membership_rows(spec.table)
    expected = {
        column: int(value) if isinstance(value, bool) else value
        for column, value in _ROWS[kind].items()
    }
    # A column added after the table reads as null on a row without it.
    expected.update({c: None for c in spec.optional if c not in expected})
    assert got == [expected]
    assert store.refold()["status"] == "ok"
    gone = store.delete_membership(kind, _key(kind))
    assert gone.kind == f"{kind}_delete"
    assert store.membership_rows(spec.table) == []
    assert [r.kind for r in store.membership_log()] == [put.kind, gone.kind]
    assert store.log_verify()["status"] == "ok"


def test_a_malformed_row_is_refused_before_it_is_logged(store: Store) -> None:
    before = len(store.log_rows())
    with pytest.raises(ValueError):
        store.put_membership("home", {**_ROWS["home"], "extra": 1})
    with pytest.raises(KeyError):
        store.put_membership(
            "home", {k: v for k, v in _ROWS["home"].items() if k != "rule"}
        )
    with pytest.raises(TypeError):
        store.put_membership("home", {**_ROWS["home"], "decided": 1.5})
    # A float is taken only where a table declares one (standing.probability).
    with pytest.raises(TypeError):
        store.put_membership("standing", {**_ROWS["standing"], "answered": 1.5})
    with pytest.raises(TypeError):
        store.put_membership(
            "standing", {**_ROWS["standing"], "probability": float("nan")}
        )
    with pytest.raises(ValueError):
        store.put_membership("elsewhere", {})
    with pytest.raises(ValueError):
        store.record_membership("memory_put", {})
    with pytest.raises(ValueError):
        store.membership_rows("homes", where={"nope": 1})
    assert len(store.log_rows()) == before


def test_rows_read_in_batches_keep_one_order(store: Store) -> None:
    for n in range(1203):
        store.put_membership(
            "project_remote", {"project": f"P{n % 7}", "remote_key": f"r{n:05d}"}
        )
    keys = [f"r{n:05d}" for n in range(1203)]
    got = store.membership_rows(
        "project_remotes", column_in=("remote_key", keys), order_by=("-remote_key",)
    )
    assert [r["remote_key"] for r in got] == sorted(keys, reverse=True)
    assert store.membership_rows("project_remotes", column_in=("remote_key", [])) == []
    assert store.next_membership_number("project_witnesses", "arrival") == 1


def test_membership_rows_are_not_telemetry(store: Store) -> None:
    store.put_membership("project", _ROWS["project"])
    store.record_membership("resolution_joined", {"checkout": "K001"})
    store.record_event("recall", query="q")
    kinds = [e["kind"] for e in store.iter_events()]
    assert kinds == ["recall"]
    assert not any(k in MEMBERSHIP_EVENT_KINDS for k in kinds)
    assert [r.kind for r in store.membership_log(["resolution_joined"])] == [
        "resolution_joined"
    ]


# ---------------------------------------------------------------------------
# Homes: written with the record, never by an update
# ---------------------------------------------------------------------------


def test_put_memory_and_put_episode_write_the_home_in_one_transaction(
    store: Store,
) -> None:
    memory, episode = _memory(), _episode()
    store.put_memory(memory, home=_HOME)
    store.put_episode(episode, home=RecordHome(GLOBAL, None, "outside_repository"))
    rows = store.log_rows()
    assert [r.kind for r in rows[-4:]] == [
        "memory_put",
        "home_put",
        "episode_put",
        "home_put",
    ]
    homes = {r["record_id"]: r for r in store.membership_rows("homes")}
    assert homes[memory.id]["kind"] == "memory"
    assert (homes[memory.id]["project"], homes[memory.id]["checkout"]) == (
        "P001",
        "K001",
    )
    assert homes[episode.id]["project"] == GLOBAL
    assert store.wrote_record(memory.id) and store.wrote_record(episode.id)
    assert not store.wrote_record(generate_ulid())
    # The same home again is no write; another home is refused, and the
    # record's own put with it.
    store.put_memory(memory, home=_HOME)
    assert store.log_rows()[-1].kind == "memory_put"
    edited = memory.model_copy(update={"body": "beta\n"})
    with pytest.raises(ValueError):
        store.put_memory(edited, home=RecordHome("P002", "K002", "object"))
    assert store.get_memory(memory.id).body == "alpha\n"
    assert store.log_verify()["status"] == "ok"


def test_a_failed_home_write_takes_the_record_with_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_module._apply_mutation

    def failing(conn: sqlite3.Connection, kind: str, payload: Any, **kw: Any) -> None:
        if kind == "home_put":
            raise sqlite3.OperationalError("disk I/O error")
        real(conn, kind, payload, **kw)

    monkeypatch.setattr(store_module, "_apply_mutation", failing)
    before = len(store.log_rows())
    memory = _memory()
    with pytest.raises(sqlite3.OperationalError):
        store.put_memory(memory, home=_HOME)
    assert store.count_memories() == 0
    assert store.membership_rows("homes") == []
    assert len(store.log_rows()) == before
    monkeypatch.undo()
    assert store.log_verify()["status"] == "ok"


def test_store_update_leaves_the_home_alone(store: Store) -> None:
    memory = _memory()
    store.put_memory(memory, home=_HOME)
    before = store.membership_rows("homes")
    current = store.get_memory(memory.id)
    seq = store.log_rows()[-1].seq
    store.update(current.model_copy(update={"body": "edited\n"}))
    assert store.get_memory(memory.id).body == "edited\n"
    assert store.membership_rows("homes") == before
    assert [r.kind for r in store.log_rows(seq)] == ["memory_put"]
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# A merge is one project row
# ---------------------------------------------------------------------------


def test_a_merge_is_one_project_row_and_never_a_record_rewrite(store: Store) -> None:
    _project(store, "P001")
    _project(store, "P002")
    rid = generate_ulid()
    store.put_memory(_memory().model_copy(update={"id": rid}), home=_HOME)
    homes = store.membership_rows("homes")
    model = Membership(store, "MACHINE1")
    seq = store.log_rows()[-1].seq
    model.merge("P001", "P002", evidence="test")
    written = [r.kind for r in store.log_rows(seq)]
    assert written == ["project_put", "project_merged"]
    assert store.membership_rows("homes") == homes
    merged = store.membership_rows("projects", where={"id": "P001"})[0]
    assert merged["merged_into"] == "P002"
    home = model.home(rid)
    assert home is not None
    assert (home.project, home.recorded_project) == ("P002", "P001")
    assert model.canonical("P001") == "P002"
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# The fold and log verify
# ---------------------------------------------------------------------------


class _History:
    """Commits in a line, each with its own blobs."""

    def __init__(self, n: int, blobs: int = 3) -> None:
        self.line = [f"{i:040x}" for i in range(1, n + 1)]
        self._blobs = {
            c: [f"b{c[-12:]}{j:027x}" for j in range(blobs)] for c in self.line
        }

    def holds(self, commits: Sequence[str]) -> set[str]:
        return set(commits) & set(self.line)

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        every = {b for bs in self._blobs.values() for b in bs}
        return set(ids) & every

    def fingerprint(self, commit: str) -> str | None:
        return "fp" + commit[-8:] if commit in self._blobs else None

    def parents(self, commit: str) -> Sequence[str]:
        i = self.line.index(commit)
        return (self.line[i - 1],) if i else ()

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        return reachable_among(self, commits)

    def blobs(self, commit: str) -> Sequence[str]:
        return self._blobs.get(commit, [])


def _populated(store: Store) -> Membership:
    """A store with rows in every membership table, written the way the
    model writes them."""
    model = Membership(store, "MACHINE1", liveness=lambda k, v: "live")
    history = _History(3)
    loc = Location(
        machine="MACHINE1",
        cwd="/w/app",
        state=REPO,
        root="/w/app",
        kind="primary",
        git_dir="/w/app/.git",
        object_key=ObjectKey("1", "2", "3"),
        common_object_key=ObjectKey("1", "2", "3"),
        head=history.line[-1],
        remote_keys=frozenset({"example.invalid/acme/app"}),
        token="TOKEN",
        token_status="present",
        history=history,
    )
    res = model.resolve(loc)
    assert res.home is not None
    memory = store.put_memory(_memory(), home=res.home)
    store.put_standing(memory.id, StandingAnswer(0.5, "asked", "test/test"))
    store.put_membership("returned", {**_ROWS["returned"], "record_id": memory.id})
    store.put_membership("machine", _ROWS["machine"])
    store.put_membership(
        "project_file", {**_ROWS["project_file"], "project": res.project}
    )
    store.put_membership(
        "declared_remote", {**_ROWS["declared_remote"], "project": res.project}
    )
    model.open_proposal(
        checkout=res.checkout,
        kind="first_sight",
        candidates=[str(res.project)],
        evidence={},
        reason="one_witness",
        line="bettermemory: a question",
    )
    for table in MEMBERSHIP_TABLES:
        assert store.membership_rows(table), table
    return model


def test_refold_reproduces_every_membership_table(store: Store) -> None:
    _populated(store)
    report = store.refold()
    assert report["status"] == "ok"
    assert set(report["membership"]) == set(MEMBERSHIP_TABLES)
    assert set(report["tables"]) == set(FOLDED_TABLES)
    for table, entry in report["membership"].items():
        assert entry["unaccounted"] == [] and entry["missing"] == [], table
        assert entry["rows"] == len(store.membership_rows(table))


def test_log_verify_reports_a_planted_home(store: Store) -> None:
    _populated(store)
    assert store.log_verify()["status"] == "ok"
    outside = _outside(store)
    outside.execute(
        "INSERT INTO homes(record_id, kind, project, checkout, rule, decided) "
        "VALUES ('PLANTED', 'memory', 'P001', NULL, 'object', 't0')"
    )
    outside.commit()
    outside.close()
    report = store.log_verify()
    assert report["status"] == "tampered"
    assert report["fold"]["status"] == "diverged"
    assert report["fold"]["membership"]["homes"]["unaccounted"] == ["PLANTED"]


def test_log_verify_reports_an_edited_binding_and_an_edited_answer(
    store: Store,
) -> None:
    _populated(store)
    outside = _outside(store)
    outside.execute("UPDATE project_files SET project = 'P999'")
    outside.execute("UPDATE proposals SET state = 'answered', answer = 'join:P999'")
    outside.commit()
    outside.close()
    report = store.log_verify()
    assert report["status"] == "tampered"
    membership = report["fold"]["membership"]
    assert membership["project_files"]["unaccounted"] == [
        _ROWS["project_file"]["file_id"]
    ]
    assert len(membership["proposals"]["unaccounted"]) == 1
    assert membership["homes"]["unaccounted"] == []


def test_log_verify_reports_a_deleted_membership_row_missing(store: Store) -> None:
    _populated(store)
    outside = _outside(store)
    outside.execute("DELETE FROM checkout_views")
    outside.commit()
    outside.close()
    report = store.log_verify()
    assert report["status"] == "tampered"
    assert len(report["fold"]["membership"]["checkout_views"]["missing"]) == 1


# ---------------------------------------------------------------------------
# The witness and blob bounds (decision D15)
# ---------------------------------------------------------------------------


def test_a_project_keeps_sixty_four_witnesses_and_eight_blobs_each(
    store: Store,
) -> None:
    _project(store, "P001")
    model = Membership(store, "MACHINE1")
    history = _History(WITNESS_BOUND + 10, blobs=BLOBS_PER_WITNESS + 5)
    for commit in history.line:
        model.add_witness("P001", commit, source="test", history=history)
    kept = [
        r["commit_id"]
        for r in store.membership_rows(
            "project_witnesses", where={"project": "P001"}, order_by=("arrival",)
        )
    ]
    assert len(kept) == WITNESS_BOUND
    assert kept[:KEEP_OLDEST] == history.line[:KEEP_OLDEST]
    assert kept[KEEP_OLDEST:] == history.line[-(WITNESS_BOUND - KEEP_OLDEST) :]
    blobs = store.membership_rows("project_blobs", where={"project": "P001"})
    per: dict[str, int] = {}
    for row in blobs:
        per[row["witness"]] = per.get(row["witness"], 0) + 1
    assert set(per) == set(kept)
    assert max(per.values()) == BLOBS_PER_WITNESS
    released = [r for r in store.log_rows() if r.kind == "witness_released"]
    assert len(released) == 10
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# The additive step and an rc3 build reading the store (decision D9)
# ---------------------------------------------------------------------------


def _strip_to_rc3(path: Path) -> None:
    """The store as an rc3 build created it: no membership table, index
    or store id."""
    conn = sqlite3.connect(str(path))
    for table in MEMBERSHIP_TABLES:
        conn.execute(f"DROP TABLE {table}")
    conn.execute("DELETE FROM meta WHERE key = ?", (MEMBERSHIP_STORE_ID,))
    conn.commit()
    conn.close()


def _master(path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(
            (str(r[0]), str(r[1]), str(r[2] or ""))
            for r in conn.execute("SELECT type, name, sql FROM sqlite_master")
        )
    finally:
        conn.close()


def _table_info(path: Path, tables: Sequence[str]) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            t: [tuple(r) for r in conn.execute(f"PRAGMA table_info({t})")]
            for t in tables
        }
    finally:
        conn.close()


def test_the_additive_step_adds_the_tables_once_and_keeps_the_schema_version(
    tmp_path: Path,
) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as s:
        memory = _memory()
        s.put_memory(memory)
        created_id = s.membership_store_id
        rows = len(s.log_rows())
    rc3_info = _table_info(path, FOLDED_TABLES)
    _strip_to_rc3(path)
    with Store.open(path, keys_dir=keys) as s:
        assert s.meta()["schema_version"] == str(SCHEMA_VERSION) == "1"
        minted = s.membership_store_id
        assert minted != created_id
        assert len(s.log_rows()) == rows
        assert s.get_memory(memory.id).body == "alpha\n"
        assert s.log_verify()["status"] == "ok"
    first = _master(path)
    with Store.open(path, keys_dir=keys) as s:
        assert s.membership_store_id == minted
        assert len(s.log_rows()) == rows
    assert _master(path) == first
    assert _table_info(path, FOLDED_TABLES) == rc3_info


def test_an_optional_column_is_added_to_a_table_that_lacks_it(
    tmp_path: Path,
) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as s:
        s.put_membership("checkout", _ROWS["checkout"])
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE checkouts DROP COLUMN unresolved_json")
    conn.commit()
    conn.close()
    with Store.open(path, keys_dir=keys) as s:
        row = s.membership_rows("checkouts")[0]
        assert row["unresolved_json"] is None
        assert s.log_verify()["status"] == "ok"
        unresolved = {**_ROWS["checkout"], "id": "K002", "kind": "unresolved"}
        unresolved["unresolved_json"] = {"sessions": ["s1"], "replaced_by": None}
        s.put_membership("checkout", unresolved)
        assert s.membership_rows("checkouts", where={"id": "K002"})[0][
            "unresolved_json"
        ] == {"sessions": ["s1"], "replaced_by": None}
        assert s.log_verify()["status"] == "ok"


def test_a_read_only_store_without_the_tables_still_opens(tmp_path: Path) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as s:
        memory = _memory()
        s.put_memory(memory)
    _strip_to_rc3(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta: dict[str, str] = {}
        store_module._add_membership_tables(conn, path, meta)
        assert MEMBERSHIP_STORE_ID not in meta
    finally:
        conn.close()


def _rc3_fold(path: Path) -> dict[str, dict[Any, Any]]:
    """The fold an rc3 build runs: its mutation kinds replayed into its
    tables, every other row skipped."""
    live = sqlite3.connect(str(path))
    live.row_factory = sqlite3.Row
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    scratch.row_factory = sqlite3.Row
    try:
        scratch.executescript(SCHEMA)
        scratch.execute("BEGIN")
        for row in live.execute("SELECT kind, payload, mac FROM log ORDER BY seq"):
            if row["kind"] in _RC3_MUTATION_KINDS:
                store_module._apply_mutation(
                    scratch,
                    str(row["kind"]),
                    json.loads(row["payload"]),
                    log_mac=row["mac"],
                )
        scratch.execute("COMMIT")
        diverged: dict[str, dict[Any, Any]] = {}
        for table in _RC3_FOLDED_TABLES:
            ours = store_module._table_rows(live, table)
            theirs = store_module._table_rows(scratch, table)
            if ours != theirs:
                diverged[table] = {"live": len(ours), "fold": len(theirs)}
        return diverged
    finally:
        scratch.close()
        live.close()


def test_an_rc3_build_reads_the_migrated_store(tmp_path: Path) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as s:
        old = _memory("written by rc3")
        s.put_memory(old)
        s.put_episode(_episode())
        s.record_event("recall", query="q")
    _strip_to_rc3(path)
    with Store.open(path, keys_dir=keys) as s:
        _populated(s)
        s.put_episode(
            _episode("s2"), home=RecordHome(GLOBAL, None, "outside_repository")
        )
        assert s.log_verify()["status"] == "ok"
    # What rc3 checks and reads: the schema version, its tables' columns,
    # its fold over its own kinds, and the rows it wrote.
    conn = sqlite3.connect(str(path))
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        bodies = {str(r[0]) for r in conn.execute("SELECT body FROM memories")}
        episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    finally:
        conn.close()
    assert version == "1"
    assert "written by rc3\n" in bodies
    assert episodes == 2
    assert _rc3_fold(path) == {}
    with Store.open(path, keys_dir=keys) as s:
        assert s.get_memory(old.id).body == "written by rc3\n"
        assert [e["kind"] for e in s.iter_events()] == ["recall"]


# ---------------------------------------------------------------------------
# The store id (design 8.7)
# ---------------------------------------------------------------------------


def test_the_store_id_is_minted_with_the_store_and_reissued_at_a_fork(
    store: Store,
) -> None:
    first = store.membership_store_id
    assert len(first) == 32 and first != store.store_id
    old, new = store.reissue_membership_store_id(record_id="R1", source="import.json")
    assert (old, new) == (first, store.membership_store_id)
    assert new != first
    row = store.log_rows()[-1]
    assert row.kind == "store_forked"
    assert json.loads(row.payload) == {
        "old_store": first,
        "new_store": new,
        "record": "R1",
        "source": "import.json",
    }
    assert store.log_verify()["status"] == "ok"
