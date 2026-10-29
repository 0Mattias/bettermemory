"""A store file this build did not write is refused before it is read (ENG-172).

A repository can ship ``.claude-memory/memory.sqlite``, and ``[storage]
cwd_store`` (on by default) makes it the store every entry path opens in
that checkout. One whose ``meta`` is a view over an endless recursive CTE
hung `security audit`, `doctor`, the daemon's open and the hooks for good;
one with a trigger of its own on ``log`` hung the first write, which the
session-start hook makes. Every opener now admits the file before it reads
a value from it (`store_schema.admit_store`): ``trusted_schema`` off, the
schema checked against this build's own objects, and the reads made before
the file is admitted held to `store_schema.ADMISSION_DEADLINE_SECONDS`. A
file that fails is refused with one finding, and this build writes
nothing to it.

The cold review built three stores that passed that check and still hung
every surface: a ``meta`` whose ``value`` is a generated column over one
costly ``instr`` call (one virtual-machine step no progress handler can
interrupt), a ``sqlite_stat1`` rewritten the same way (SQLite reads it
while the schema loads, before any check looks), and a CHECK on ``log``
that every insert runs. Each is refused now (the length limit during
admission, SQLite's own text for its statistics tables, and the column
check of each table this build creates), and so is a schema SQLite
cannot parse, which ended ``up --foreground`` in a traceback.

The four surfaces run in a child process each, from the repository's
directory with no ``BETTERMEMORY_DIR``, so the store is the one
``cwd_store`` picks, under a harness timeout (`_CHILD_TIMEOUT`): against a
build without the check the child never returns, and the timeout ends the
run as a failure instead of hanging the suite. Inside the child the call
that refuses is timed and printed, and must come back well under the
deadline (`_REFUSAL_BOUND`).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import pytest

from bettermemory import store_schema
from bettermemory.cli import _common
from bettermemory.config import PROJECT_DIR_NAME
from bettermemory.log import KeyRing
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_keys import _store_id_at
from bettermemory.store_schema import MEMBERSHIP_TABLES

from .conftest import shielded_child_env

_T = TypeVar("_T")

#: What every refusal of a file's schema says. The tests match the refusal
#: as the ValueError it is and read `store_schema` names where they are
#: used, so this file collects on a build without the check, where each
#: test then fails on its own (`_in_time`, `_CHILD_TIMEOUT`).
_REFUSED = "not a store this build wrote"

#: How long a child may run before the harness ends it: far past the
#: admission deadline, so only a child that hangs reaches it.
_CHILD_TIMEOUT = 30.0
#: How long the refusing call may take inside the child: a quarter of the
#: 2 s admission deadline (a test below holds the two together), which a
#: refusal by the schema check never comes near (it reads one table of
#: about 70 rows). A literal, so this file collects on a build without
#: the deadline.
_REFUSAL_BOUND = 0.5

#: A query that never returns a row and never ends.
_ENDLESS = (
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
    "SELECT 'store_id' AS key, x AS value FROM n WHERE x < 0"
)

#: The ways a repository's store is made to hang a reader: ``meta`` as an
#: endless view (every reader), and a trigger of its own on ``log`` whose
#: body never ends (every writer of a row, the session-start hook's brief
#: among them).
_POISONS: dict[str, tuple[str, ...]] = {
    "view": ("DROP TABLE meta", f"CREATE VIEW meta AS {_ENDLESS}"),
    "trigger": (
        "CREATE TRIGGER log_spin AFTER INSERT ON log BEGIN "
        f"SELECT count(*) FROM ({_ENDLESS}); END",
    ),
    # The cold review's three (ENG-172 H1): each passed the schema check by
    # its tables' names. One `instr` over ten million characters is one
    # step that takes seconds.
    "generated": (
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = 'CREATE TABLE meta (key TEXT PRIMARY KEY, "
        "value TEXT GENERATED ALWAYS AS (v || substr(instr(printf(''%.*c'', "
        "10000000 + length(v) * 0, ''a''), printf(''%.*c'', 100000, ''a'') || ''b''), "
        "1, 0)) VIRTUAL, v TEXT)' WHERE type = 'table' AND name = 'meta'",
        "PRAGMA writable_schema = OFF",
    ),
    "stat1": (
        "ANALYZE",
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = 'CREATE TABLE sqlite_stat1(tbl, idx, stat "
        "GENERATED ALWAYS AS (s || substr(instr(printf(''%.*c'', 10000000 + "
        "length(s) * 0, ''a''), printf(''%.*c'', 100000, ''a'') || ''b''), 1, 0)) "
        "VIRTUAL, s)' WHERE name = 'sqlite_stat1'",
        "PRAGMA writable_schema = OFF",
    ),
    "check": (
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = substr(sql, 1, length(sql) - 1) || "
        "', CHECK (instr(printf(''%.*c'', 10000000, ''a''), printf(''%.*c'', "
        "100000, ''a'') || ''b'') = 0))' WHERE type = 'table' AND name = 'log'",
        "PRAGMA writable_schema = OFF",
    ),
    # A schema text SQLite cannot parse (ENG-172 M2).
    "malformed": (
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = 'CREATE VIEW meta AS SELECT nonsense FROM ((' "
        "WHERE name = 'meta'",
        "PRAGMA writable_schema = OFF",
    ),
}


def _sql(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


#: How long an in-process call that could hang is waited for (`_in_time`).
_HANG_TIMEOUT = 10.0


def _in_time(call: Callable[[], _T]) -> _T:
    """`call()`'s value, or its exception, from a daemon thread waited on
    for `_HANG_TIMEOUT`: the harness's bound on an in-process call that a
    build without the check never returns from. SQLite gives the thread
    no way to be stopped, so on that build it spins on until the run
    ends; the test fails at once all the same."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_HANG_TIMEOUT)
    if thread.is_alive():
        pytest.fail(
            f"the call did not return in {_HANG_TIMEOUT:g} s: the store hung it"
        )
    if "error" in box:
        raise box["error"]
    value: _T = box["value"]
    return value


def _snapshot(path: Path) -> tuple[bytes, int, list[str]]:
    """The file's bytes, its mode and every name beside it."""
    return (
        path.read_bytes(),
        stat.S_IMODE(path.stat().st_mode),
        sorted(p.name for p in path.parent.iterdir()),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose ``.claude-memory`` holds a store this build wrote."""
    root = tmp_path / "repo"
    path = root / PROJECT_DIR_NAME / STORE_FILENAME
    Store.create(path).close()
    return root


def _poisoned(repo: Path, poison: str) -> Path:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *_POISONS[poison])
    return path


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _master(path: Path) -> list[tuple[str, str, str, str | None]]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            (str(r[0]), str(r[1]), str(r[2]), r[3])
            for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        ]
    finally:
        conn.close()


def test_every_store_this_build_writes_is_admitted(repo: Path) -> None:
    """A fresh store; one an rc3 build left without the membership tables
    and the additive index, which a user then ran ANALYZE on; one an
    earlier build left without an optional column, whose table text is
    not this build's; each opens."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    assert store_schema.schema_refusal(_master(path)) is None
    older = repo / "older" / STORE_FILENAME
    older.parent.mkdir()
    shutil.copy(path, older)
    _sql(
        older,
        *(f"DROP TABLE {table}" for table in MEMBERSHIP_TABLES),
        "DROP INDEX log_corroborations",
        "ANALYZE",
    )
    rows = _master(older)
    assert any(name == "sqlite_stat1" for _, name, _, _ in rows)
    assert store_schema.schema_refusal(rows) is None
    lacking = repo / "lacking" / STORE_FILENAME
    lacking.parent.mkdir()
    shutil.copy(path, lacking)
    _sql(lacking, "ALTER TABLE checkouts DROP COLUMN unresolved_json")
    assert store_schema.schema_refusal(_master(lacking)) is None
    for each in (older, lacking):
        Store.open(each).close()


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (_POISONS["view"], "view named 'meta'"),
        ((f"CREATE VIEW recent AS {_ENDLESS}",), "view named 'recent'"),
        (_POISONS["trigger"], "trigger named 'log_spin'"),
        (("CREATE TABLE extra(x)",), "table named 'extra'"),
        (
            (
                "DROP TRIGGER memories_ai",
                "CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN "
                f"SELECT count(*) FROM ({_ENDLESS}); END",
            ),
            "trigger 'memories_ai' is not the one",
        ),
        (
            (
                "DROP INDEX log_by_kind",
                "CREATE INDEX log_by_kind ON log(kind, seq, ts)",
            ),
            "index 'log_by_kind' is not the one",
        ),
        (
            (
                "DROP TABLE quarantine",
                "CREATE VIRTUAL TABLE quarantine USING fts5(name)",
            ),
            "'quarantine' is a virtual table",
        ),
    ],
)
def test_an_object_this_build_does_not_create_is_refused(
    repo: Path, statements: tuple[str, ...], named: str
) -> None:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *statements)
    why = store_schema.schema_refusal(_master(path))
    assert why is not None and named in why


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT, at TEXT DEFAULT (datetime('now')))",
            ),
            "default that is not a literal",
        ),
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT REFERENCES memories(id))",
            ),
            "foreign key",
        ),
        (
            ("DROP TABLE imports", "CREATE TABLE imports(id TEXT CHECK (id <> ''))"),
            "has a check",
        ),
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT, n AS (length(id)))",
            ),
            "generated column",
        ),
        (
            (
                "ANALYZE",
                "PRAGMA writable_schema = ON",
                "UPDATE sqlite_master SET sql = 'CREATE TABLE sqlite_stat1(tbl, idx, "
                "stat)' WHERE name = 'sqlite_stat1'",
                "PRAGMA writable_schema = OFF",
            ),
            "not the one SQLite writes",
        ),
    ],
)
def test_a_table_of_this_builds_name_that_runs_an_expression_is_refused(
    repo: Path, statements: tuple[str, ...], named: str
) -> None:
    """A plain table is admitted by its name; what it could make SQLite run
    is read from the file (`store_schema.table_refusal`), and SQLite's own
    statistics tables pass only with the text SQLite gives them."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *statements)
    with pytest.raises(ValueError, match=_REFUSED) as refused:
        _in_time(lambda: Store.open(path))
    assert named in str(refused.value)


@pytest.mark.parametrize("poison", sorted(_POISONS))
@pytest.mark.parametrize("allow_rekey", [False, True])
def test_every_open_refuses_it_and_writes_nothing(
    repo: Path, poison: str, allow_rekey: bool
) -> None:
    """The reader's and the owner's open alike: refused in one error
    naming the object, quickly, with the file's bytes, mode and siblings as
    they were (no WAL switch, no chmod, no additive step)."""
    path = _poisoned(repo, poison)
    os.chmod(path, 0o644)
    before = _snapshot(path)
    started = time.perf_counter()
    with pytest.raises(ValueError, match=_REFUSED) as refused:
        _in_time(lambda: Store.open(path, allow_rekey=allow_rekey))
    assert time.perf_counter() - started < _REFUSAL_BOUND
    assert isinstance(refused.value, store_schema.StoreRefusedError)
    assert not refused.value.timed_out
    assert _snapshot(path) == before


def test_a_read_that_runs_past_the_deadline_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose schema is this build's can still be made slow to read:
    a ``meta`` of half a million rows. The deadline ends the read, and the
    refusal says it ran out of time (``timed_out``), not that the schema is
    foreign; with the deadline at its value the same file opens."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(
        path,
        "INSERT INTO meta(key, value) WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL "
        "SELECT x + 1 FROM n WHERE x < 500000) SELECT 'padding' || x, x FROM n",
    )
    with monkeypatch.context() as patched:
        patched.setattr(store_schema, "ADMISSION_DEADLINE_SECONDS", 0.01)
        with pytest.raises(ValueError, match="ran past") as refused:
            Store.open(path)
    assert isinstance(refused.value, store_schema.StoreRefusedError)
    assert refused.value.timed_out
    Store.open(path).close()


def test_the_refusal_bound_is_well_under_the_deadline() -> None:
    assert _REFUSAL_BOUND <= store_schema.ADMISSION_DEADLINE_SECONDS / 4


def test_the_deadline_ends_an_endless_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline itself, on a statement that never ends: interrupted
    once the deadline has passed, raised as a timed-out refusal, and the
    progress handler removed after."""
    monkeypatch.setattr(store_schema, "ADMISSION_DEADLINE_SECONDS", 0.2)
    path = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        started = time.perf_counter()
        with pytest.raises(ValueError, match="ran past") as refused:
            with store_schema._deadline(conn, path, "test"):
                conn.execute(_ENDLESS).fetchall()
        took = time.perf_counter() - started
        assert isinstance(refused.value, store_schema.StoreRefusedError)
        assert refused.value.timed_out
        assert 0.2 <= took < 1.2
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_a_bound_file_replaced_by_a_foreign_one_reads_as_no_store(
    repo: Path, tmp_path: Path
) -> None:
    """The key ring's binding names a path; a file put there since (a
    ``git pull`` over the bound store) is admitted before its id is read,
    and a foreign one holds no store, at once, where its read hung the
    open of every other file with the id."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    copy = tmp_path / "copy" / STORE_FILENAME
    copy.parent.mkdir()
    shutil.copy(path, copy)
    with Store.open(path) as bound:
        store_id = bound.store_id
    _poisoned(repo, "view")
    started = time.perf_counter()
    assert _in_time(lambda: _store_id_at(path)) is None
    with Store.open_or_create(copy) as opened:
        assert opened.has_key
        assert opened.store_id == store_id
    assert time.perf_counter() - started < _REFUSAL_BOUND * 4
    ring = KeyRing(Path(os.environ["BETTERMEMORY_KEYS_DIR"]), store_id)
    assert ring.read_binding() == copy.resolve()


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


def test_a_bound_store_with_one_more_index_keeps_the_ring(
    repo: Path, tmp_path: Path
) -> None:
    """The cold review's H2: the bound store gains one object this build
    does not create (another build's index, or one added by hand). This
    build refuses it, but it may still hold the store's id, so the key
    ring stays bound to it: a copy opens read-only and writes nothing, and
    the original, once the index is gone, verifies clean. Read as no
    store, the ring went to the copy, which signed with the key and moved
    the head, and the original read tampered (``tail_deleted``)."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    copy = tmp_path / "copy" / STORE_FILENAME
    copy.parent.mkdir()
    with Store.open_or_create(path) as bound:
        bound.put_memory(_memory("the staging database listens on 5432"))
        store_id = bound.store_id
    shutil.copy(path, copy)
    _sql(path, "CREATE INDEX extra ON memories(created)")
    with pytest.raises(OSError):
        _in_time(lambda: _store_id_at(path))
    with Store.open_or_create(copy) as opened:
        assert opened.has_key is False
        rows = len(opened.log_rows())
        with pytest.raises(RuntimeError, match="open for reading only"):
            opened.put_memory(_memory("written into the copy"))
        assert len(opened.log_rows()) == rows
    ring = KeyRing(Path(os.environ["BETTERMEMORY_KEYS_DIR"]), store_id)
    assert ring.read_binding() == path.resolve()
    _sql(path, "DROP INDEX extra")
    with Store.open_or_create(path) as original:
        assert original.has_key
        assert original.log_verify()["status"] == "ok"


def test_the_owners_opener_ends_in_one_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`open_owner_store`, which `cli_context`, `serve` and the daemon's
    `up --foreground` open through: exit 1 and the refusal on one line."""
    path = _poisoned(repo, "view")
    with pytest.raises(SystemExit) as exited:
        _in_time(lambda: _common.open_owner_store(path))
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert _REFUSED in err


# ---------------------------------------------------------------------------
# The four surfaces, each in a child under the harness timeout
# ---------------------------------------------------------------------------

#: Each child imports what the surface imports on first use, then times the
#: call that meets the store and prints one JSON line: ``elapsed`` and what
#: the surface reported.
_IMPORTS = """
import json, time
from pathlib import Path
import bettermemory.backup, bettermemory.config, bettermemory.recall
import bettermemory.store
"""
_AUDIT = (
    _IMPORTS
    + """
from bettermemory import posture
store = Path.cwd() / ".claude-memory" / "memory.sqlite"
started = time.perf_counter()
meta, error, refused = posture._meta_or_error(store)
elapsed = time.perf_counter() - started
report = posture.audit(cwd=Path.cwd())
keys = [f for f in report.findings if f.id == "key.match"]
print(json.dumps({"elapsed": elapsed, "refused": refused is not None,
                  "findings": [[f.status, f.summary] for f in keys]}))
"""
)
_DOCTOR = (
    _IMPORTS
    + """
from bettermemory import doctor
cwd = Path.cwd()
started = time.perf_counter()
store, key = doctor.check_store(
    doctor._Context(cwd=cwd, directory=cwd / ".claude-memory"))
elapsed = time.perf_counter() - started
checks = {c.id: c for c in doctor.run_checks(cwd=cwd)}
print(json.dumps({"elapsed": elapsed, "store": [store.status, store.summary],
                  "key": key.status, "run": checks["store"].status}))
"""
)
_DAEMON_OPEN = (
    _IMPORTS
    + """
from bettermemory.cli._common import cli_context
started = time.perf_counter()
try:
    cli_context(migrate=False)
    outcome = "opened"
except SystemExit as exc:
    outcome = f"exit {exc.code}"
print(json.dumps({"elapsed": time.perf_counter() - started, "outcome": outcome}))
"""
)
_HOOK = (
    _IMPORTS
    + """
from bettermemory.cli import session_start_cmd
started = time.perf_counter()
try:
    block = session_start_cmd._build_context_block(session_id="s1", source="startup")
    outcome = f"block {block!r}"
except Exception as exc:
    outcome = f"{type(exc).__name__}: {exc}"
print(json.dumps({"elapsed": time.perf_counter() - started, "outcome": outcome}))
"""
)


def _child_env(tmp_path: Path) -> dict[str, str]:
    """This test's directories, and no ``BETTERMEMORY_DIR``: the store is
    the one ``cwd_store`` picks in the child's working directory."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = shielded_child_env()
    env.pop("BETTERMEMORY_DIR", None)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        APPDATA=str(home / "AppData" / "Roaming"),
        BETTERMEMORY_STATE_DIR=str(tmp_path / "state"),
        GIT_CEILING_DIRECTORIES=str(tmp_path),
    )
    return env


def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_CHILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{argv[1:3]} did not finish in {_CHILD_TIMEOUT:g} s "
            f"(ran {time.monotonic() - started:.1f} s): the store hung it"
        )


def _reported(code: str, repo: Path, tmp_path: Path) -> dict[str, Any]:
    done = _run([sys.executable, "-c", code], cwd=repo, env=_child_env(tmp_path))
    assert done.returncode == 0, done.stderr
    reported: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    elapsed = reported["elapsed"]
    assert isinstance(elapsed, float) and elapsed < _REFUSAL_BOUND, reported
    return reported


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_audit_refuses_it_in_one_finding(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    """One ``key.match`` finding, high; a schema SQLite cannot read is the
    audit's warning for a meta it cannot read, as for a file that is not a
    database (`test_posture.test_unreadable_meta_is_a_warning`)."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    reported = _reported(_AUDIT, repo, tmp_path)
    findings: list[list[str]] = reported["findings"]
    assert len(findings) == 1
    status, summary = findings[0]
    if poison == "malformed":
        assert reported["refused"] is False
        assert status == "warn" and "meta cannot be read" in summary
    else:
        assert reported["refused"] is True
        assert status == "high" and _REFUSED in summary
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_doctor_refuses_it_in_one_finding(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    reported = _reported(_DOCTOR, repo, tmp_path)
    store: list[str] = reported["store"]
    status, summary = store
    assert status == "fail" and _REFUSED in summary
    assert reported["key"] == "warn"  # not checked: the store does not open
    assert reported["run"] == "fail"
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_daemons_open_refuses_it(repo: Path, tmp_path: Path, poison: str) -> None:
    """What `up --foreground` opens through (`cli_context(migrate=False)`),
    timed; then the daemon itself, which exits 1 with the line and no
    traceback instead of serving."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    assert _reported(_DAEMON_OPEN, repo, tmp_path)["outcome"] == "exit 1"
    env = _child_env(tmp_path)
    done = _run(
        [sys.executable, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
        cwd=repo,
        env=env,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert _REFUSED in done.stderr
    assert "Traceback" not in done.stderr
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_session_start_hook_refuses_it(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    """The in-process hook path (`session-start`, which records the brief
    it shows as a log row): refused, timed, and as a command it prints no
    block and exits 0 with one stderr note, as every hook failure does."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    outcome = _reported(_HOOK, repo, tmp_path)["outcome"]
    assert isinstance(outcome, str)
    assert outcome.startswith("StoreRefusedError") and "not a store" in outcome
    done = _run(
        [sys.executable, "-m", "bettermemory", "session-start", "--session-id", "s1"],
        cwd=repo,
        env=_child_env(tmp_path),
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert _REFUSED in done.stderr
    assert _snapshot(path) == before
