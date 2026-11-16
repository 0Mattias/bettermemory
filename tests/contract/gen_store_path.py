"""Fixtures 4b and 4c: search through the store, by caller.

A real store file holds the synthetic records of ``search.jsonl``, homed
in two projects (two git repositories with remotes), in ``global`` (a
caller outside any repository) and with no home at all. Three callers are
resolved where a request resolves them (`location.resolve_caller`, with
the directory as the request's), and each query runs the way
`handlers.search.memory_search` runs it: the pool from the FTS5 index
through the caller's admission (`resolve_search_pool`), ranked by
`search.search` with the pool's store statistics, the caller's anchoring
and the configured defaults. The clock is the fixture's (`_NOW`); the
handler reads the wall clock. One run has no admission (an offline
caller) and is compared with ``search.jsonl``'s in-memory ranking.

What the native build reads to answer as the reference does is recorded
beside the answers: the store as it stood while the queries ran, as the
SQL that rebuilds it (`_store_dump`: the tables a search reads, with the
values the wall clock and the key mint masked, and checked here to rank,
admit and price as the store did), each admission's answers for that
store's rows (`_admission_data`; membership is not ported), the digest
fingerprint the statistics trust, and each pool's statistics as the
ranking asked for them. ``hostile`` lines run queries the FTS5 syntax,
NUL, length, stopwords and emptiness make awkward, and pin the
reference's answer or the exception it raises.

The same build also exercises store writes beyond the records (tombstone,
restore, episodes, verification, conflict, import, quarantine), and its
log rows are the shapes `gen_log` reads for those kinds (`log_rows`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from datetime import timedelta
from typing import Any

from bettermemory import _daemon_client, location, store_digest
from bettermemory import search as bsearch
from bettermemory.admission import Admission
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.verify_anchoring import Anchoring
from bettermemory.handlers.search import POOL_SIZE, resolve_search_pool
from bettermemory.identity import Actor
from bettermemory.membership import SEARCH_AUTO_SCOPE, SEARCH_UNSCOPED
from bettermemory.membership_records import REVIEW_STRATA
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store
from bettermemory.store_index import _ADMISSION_INPUTS, ranked_rows

from ._common import float_bits, hit_records, jsonl, raw_scores
from .gen_store import _NOW, _QUERIES, _memories

#: The git environment a resolution runs under: no user or system
#: configuration, a fixed identity, nothing pointing git elsewhere.
_GIT_CLEAR = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "BETTERMEMORY_WORKSPACE",
    _daemon_client.HARNESS_SESSION_ENV,
)

#: Where each record lives, by its index in the corpus (cycled): a
#: project, ``global``, or no home row.
_HOMES = ("alpha", "beta", "plain", None)
_CALLERS = ("alpha", "beta", "plain")
_MODES = ("hybrid", "bm25", "keyword")
_SURFACES = {"auto_scope": SEARCH_AUTO_SCOPE, "unscoped": SEARCH_UNSCOPED}


@contextmanager
def _neutral_git(tmp: Path) -> Iterator[None]:
    saved = dict(os.environ)
    empty = tmp / "gitconfig-empty"
    # No user or system configuration, and no line-ending conversion, so a
    # fixture commit's SHA is the same on every platform.
    empty.write_bytes(b"[core]\n\tautocrlf = false\n\teol = lf\n")
    try:
        for var in _GIT_CLEAR:
            os.environ.pop(var, None)
        os.environ.update(
            GIT_CONFIG_GLOBAL=str(empty),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _repo(path: Path, remote: str, day: int) -> None:
    path.mkdir()
    stamp = f"2026-01-{day:02d}T00:00:00Z"
    env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, env=env
        )

    git("init", "-q", "-b", "main")
    (path / "README.md").write_bytes(f"{path.name}\n".encode("ascii"))
    git("add", "-A")
    git("commit", "-q", "-m", f"{path.name} initial")
    git("remote", "add", "origin", remote)


class _Built:
    def __init__(self) -> None:
        self.homes: dict[str, str | None] = {}
        self.callers: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.rows: list[tuple[str, str | None, str]] = []
        self.store: dict[str, Any] = {}
        self.admissions: dict[str, dict[str, Any]] = {}
        self.digest_fingerprint = ""
        self.hostile: list[dict[str, Any]] = []
        self.schema: dict[str, Any] = {}
        self.stores: dict[str, dict[str, Any]] = {}
        self.lines: list[dict[str, Any]] = []


def _labels(project: str | None, ids: dict[str, str]) -> str | None:
    return None if project is None else ids.get(project, project)


def _run(
    store: Store,
    cfg: Config,
    query: str,
    mode: str,
    admission: Admission | None,
    anchoring: Anchoring | None,
    *,
    filters: dict[str, Any] | None = None,
    size: int = POOL_SIZE,
    max_results: int = 10,
    compact: bool = False,
) -> dict[str, Any]:
    """One pool and its ranking, as `memory_search` makes them. With
    `compact` the pool is its row numbers and each hit ``[id, bits,
    raw_bits, relevance, match_terms]``."""
    f = filters or {}
    scopes = f.get("scopes")
    excluded = set(f["excluded"]) if "excluded" in f else None
    pool = resolve_search_pool(
        store,
        query,
        scopes=scopes,
        excluded_scopes=excluded,
        admission=admission,
        client_filter=f.get("client"),
        model_filter=f.get("model"),
        size=size,
    )
    provider = pool.corpus_stats_provider
    assert provider is not None
    stats: list[list[Any]] = []

    def recorded(terms: list[str]) -> Any:
        found = provider(terms)
        stats.append(
            [list(terms)]
            + (
                [None]
                if found is None
                else [
                    found.size,
                    found.body_df,
                    found.scope_df,
                    None if found.avgdl is None else float_bits(found.avgdl),
                ]
            )
        )
        return found

    with raw_scores() as raw:
        hits = bsearch.search(
            pool.memories,
            query,
            scopes=scopes,
            excluded_scopes=excluded,
            admission=admission,
            client_filter=f.get("client"),
            model_filter=f.get("model"),
            max_results=max_results,
            now=_NOW,
            half_life_days=cfg.behavior.recency_boost_half_life_days,
            mode=mode,  # type: ignore[arg-type]
            corpus_stats_provider=recorded,
            matched_leg_out={},
            rescue_expansion=False,
            conversational=cfg.behavior.conversational,
            anchoring=anchoring,
            hydrate=pool.hydrate,
        )
    if not compact:
        return {
            "pool": [m.id for m in pool.memories],
            "hits": hit_records(hits, raw),
            "stats": stats,
        }
    rowid = {
        str(i): int(r) for r, i in store._conn.execute("SELECT rowid, id FROM memories")
    }
    return {
        "pool": [rowid[m.id] for m in pool.memories],
        "hits": [
            [
                h.id,
                float_bits(h.score),
                float_bits(raw[h.id]),
                h.relevance,
                list(h.match_terms),
            ]
            for h in hits
        ],
        "stats": stats,
    }


#: Queries a caller can send that FTS5's syntax, NUL, length, stopwords
#: or emptiness make awkward. A ``times`` case is ``query`` repeated.
_HOSTILE: tuple[dict[str, Any], ...] = (
    {"label": "fts operators", "query": 'docker AND (NOT compose OR "staging'},
    {"label": "fts column and prefix", "query": "body_fts: dock* NEAR(a b, 2) ^x"},
    {"label": "fts keywords only", "query": "AND OR NOT NEAR"},
    {"label": "quotes", "query": "it's \"docker\" '' \"\" '"},
    {"label": "nul inside", "query": "docker\x00compose"},
    {"label": "nul only", "query": "\x00"},
    {"label": "punctuation only", "query": "*-^:()\"'{}[]"},
    {"label": "stopwords only", "query": "the and of to is"},
    {"label": "empty", "query": ""},
    {"label": "whitespace only", "query": " \t\n\u3000 "},
    {"label": "very long", "query": "docker staging cluster ", "times": 2000},
    {"label": "very long one token", "query": "d", "times": 20000},
)


def hostile_query(case: dict[str, Any]) -> str:
    return str(case["query"]) * int(case.get("times", 1))


def _guarded(
    store: Store,
    cfg: Config,
    query: str,
    mode: str,
    admission: Admission | None,
    anchoring: Anchoring | None,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return _run(store, cfg, query, mode, admission, anchoring, **kwargs)
    except Exception as exc:  # the reference's answer is the exception
        return {"error": type(exc).__name__}


#: The tables a pooled search reads, kept in the dump with their rows;
#: every other table keeps its definition and no row.
_KEPT = (
    "meta",
    "memories",
    "tombstones",
    "homes",
    "log",
    "digest_records",
    "digest_tokens",
    "digest_state",
)


def _masked_mac(seq: int) -> str:
    return f"{seq:064x}"


def _id_payload(kind: str, payload: str) -> str:
    """A record mutation's payload cut to the id `_mutated_ids` reads."""
    record_id = json.loads(payload)
    if kind == "memory_put":
        return json.dumps({"memory": {"id": record_id["memory"]["id"]}})
    if kind == "tombstone_put":
        return json.dumps({"tombstone": {"id": record_id["tombstone"]["id"]}})
    return json.dumps({"id": record_id["id"]})


def _store_dump(
    store: Store,
    tmp: Path,
    name: str,
    queries: Sequence[str],
    recipe: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The store as its queries see it: the schema (`gen_store_files.dump`'s
    statements, tables first, then the full-text table, its rebuild, the
    indexes and triggers) and the rows of the tables a pooled search reads,
    from a copy with every other table emptied and the values the wall
    clock and the key mint give masked (meta's store id, key fingerprint,
    membership id and creation stamp; a record row's log MAC; a home's
    decision stamp). The log keeps the rows the digest's mark and catch-up
    read (the row the digest saw, every record mutation after it, the last
    of each mutation kind), each with a MAC derived from its seq and a
    mutation's payload cut to its record's id. The digest's token postings
    are left out (a search reads none). `recipe` is SQL applied to the
    store and to the rebuilt copy after the dump. The copy is checked to
    rank `queries`, read the same admission inputs, digest rows and digest
    state, and name the same mutations after the digest's mark, as the
    store."""
    # gen_log reads this module's log rows, and gen_store_files reads gen_log.
    from .gen_log import STORE_ID
    from .gen_store_files import MEMBERSHIP_ID, dump

    # A directory of its own, never named after the case: a case's name
    # may hold what a file name cannot on Windows (a colon, CI run
    # 43789459358).
    folder = Path(tempfile.mkdtemp(prefix="dump-", dir=tmp))
    copy = folder / "memory.sqlite"
    keys = folder / "keys"
    keys.mkdir()
    target = sqlite3.connect(copy)
    store._conn.backup(target)
    stamp = _NOW.isoformat().replace("+00:00", "Z")
    seen = int(
        target.execute("SELECT value FROM digest_state WHERE key = 'seq'").fetchone()[0]
    )
    kinds = store_digest._KINDS
    marks = ",".join("?" * len(kinds))
    with target:
        for (table,) in target.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite%' AND name NOT LIKE 'memories_fts%'"
        ).fetchall():
            if table not in _KEPT:
                target.execute(f'DELETE FROM "{table}"')
        for key, value in (
            ("store_id", STORE_ID),
            ("key_fingerprint", "0" * 64),
            ("membership_store_id", MEMBERSHIP_ID),
            ("created", stamp),
        ):
            target.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
        target.execute("UPDATE memories SET log_mac = NULL")
        target.execute("UPDATE tombstones SET log_mac = NULL")
        target.execute("UPDATE homes SET decided = ?", (stamp,))
        target.execute(
            f"DELETE FROM log WHERE NOT (seq = ? OR (kind IN ({marks}) AND (seq > ? "
            f"OR seq IN (SELECT MAX(seq) FROM log WHERE kind IN ({marks}) "
            "GROUP BY kind))))",
            (seen, *kinds, seen, *kinds),
        )
        for seq, kind, payload in target.execute(
            "SELECT seq, kind, payload FROM log"
        ).fetchall():
            target.execute(
                "UPDATE log SET ts = ?, session = NULL, payload = ?, "
                "prev_mac = '', mac = ? WHERE seq = ?",
                (
                    stamp,
                    _id_payload(str(kind), str(payload)) if kind in kinds else "{}",
                    _masked_mac(int(seq)),
                    seq,
                ),
            )
        target.execute(
            "UPDATE digest_state SET value = ? WHERE key = 'mac'",
            (_masked_mac(seen),),
        )
    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    tables = {table: _table_rows(target, table) for table in _KEPT}
    target.close()
    schema = _schema(dump(copy, keys))
    with store._conn:
        for sql in recipe:
            store._conn.execute(sql)
    rebuilt = Path(tempfile.mkdtemp(prefix="rebuilt-", dir=tmp)) / "memory.sqlite"
    conn = rebuild_store(schema, tables, recipe, rebuilt)
    try:
        here = _Conn(conn)
        there = _Conn(store._conn)
        for query in queries:
            assert ranked_rows(here, query) == ranked_rows(there, query), query  # type: ignore[arg-type]
        for select in (
            "SELECT rowid, id, body, updated, scopes_json, actor_client, actor_model "
            "FROM memories ORDER BY rowid",
            "SELECT rowid FROM memories ORDER BY updated DESC, id DESC",
            _ADMISSION_INPUTS,
            "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl "
            "FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id "
            "WHERE d.removed = 0",
        ):
            try:
                found = [tuple(r) for r in conn.execute(select)]
            except sqlite3.Error as exc:
                found = [(type(exc).__name__,)]
            try:
                want = [tuple(r) for r in store._conn.execute(select)]
            except sqlite3.Error as exc:
                want = [(type(exc).__name__,)]
            assert found == want, select
        # Each record mutation the copy keeps names the record the store's
        # row at that seq names (the copy keeps the id alone).
        kinds_in = ",".join("?" * len(store_digest._KINDS))
        for seq, kind, payload in conn.execute(
            f"SELECT seq, kind, payload FROM log WHERE kind IN ({kinds_in})",
            store_digest._KINDS,
        ):
            (original,) = store._conn.execute(
                "SELECT payload FROM log WHERE seq = ?", (seq,)
            ).fetchone()
            if payload == original:
                continue
            assert _id_payload(str(kind), str(original)) == payload, seq
        assert (store_digest._mark(conn) is None) == (
            store_digest._mark(store._conn) is None
        )
        built_here = store_digest._built(conn, store_digest._state(conn))
        built_there = store_digest._built(store._conn, store_digest._state(store._conn))
        assert (built_here is None) == (built_there is None)
        if built_here is not None and built_there is not None:
            assert store_digest._head(conn) == store_digest._head(store._conn)
            assert _ids_or_error(conn, built_here) == _ids_or_error(
                store._conn, built_there
            )
    finally:
        conn.close()
    return schema, tables


def _ids_or_error(conn: sqlite3.Connection, after: int) -> Any:
    """`_mutated_ids`' ids, or the exception it raises (which
    `digest_mark` catches)."""
    try:
        return store_digest._mutated_ids(conn, after)[0]
    except Exception as exc:
        return type(exc).__name__


def _table_rows(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    """A table's rows in rowid order (key order for one without rowid),
    each a list of values; the row number first where it is not a column.
    The digest's postings are left out."""
    if table == "digest_tokens":
        return {"columns": [], "rows": []}
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = [str(c[1]) for c in info]
    pks = [c for c in info if c[5]]
    alias = len(pks) == 1 and str(pks[0][2]).upper() == "INTEGER"
    without = table == "digest_state"
    names = columns if alias or without else ["rowid", *columns]
    select = ", ".join(f'"{c}"' for c in names)
    order = "1" if without else "rowid"
    rows = [
        list(r)
        for r in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}')
    ]
    for row in rows:
        assert all(v is None or isinstance(v, (int, str)) for v in row), table
    return {"columns": names, "rows": rows}


def _schema(base: dict[str, Any]) -> dict[str, Any]:
    """A dump's statements as the schema every store here shares: tables
    first, then the full-text table, its rebuild, the indexes and
    triggers. Rows go between the tables and the rest."""
    statements = base["statements"]
    return {
        "page_size": base["page_size"],
        "journal_mode": base["journal_mode"],
        "tables": [sql for sql in statements if sql.startswith("CREATE TABLE")],
        "rest": [
            *[sql for sql in statements if sql.startswith("CREATE VIRTUAL")],
            *[sql for sql in statements if "VALUES ('rebuild')" in sql],
            *[
                sql
                for sql in statements
                if not sql.startswith(("CREATE TABLE", "CREATE VIRTUAL", "INSERT INTO"))
            ],
        ],
    }


def rebuild_store(
    schema: dict[str, Any],
    tables: dict[str, Any],
    recipe: Sequence[str],
    path: Path,
) -> sqlite3.Connection:
    """The store `schema` and `tables` describe, written at `path` with
    `recipe` applied, open (rows as `sqlite3.Row`)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA page_size = {int(schema['page_size'])}")
    for sql in schema["tables"]:
        conn.execute(sql)
    for table, content in tables.items():
        if not content["rows"]:
            continue
        names = ", ".join(f'"{c}"' for c in content["columns"])
        marks = ", ".join("?" * len(content["columns"]))
        conn.executemany(
            f'INSERT INTO "{table}"({names}) VALUES ({marks})', content["rows"]
        )
    for sql in schema["rest"]:
        conn.execute(sql)
    conn.execute(f"PRAGMA journal_mode = {schema['journal_mode']}")
    for sql in recipe:
        conn.execute(sql)
    conn.row_factory = sqlite3.Row
    return conn


class _Conn:
    """What `store_index.ranked_rows` reads of a store: its connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn


def _admission_data(store: Store, admission: Admission) -> dict[str, Any]:
    """What the admission answers for this store's rows, as data: whether
    it admits a record each project names (``global`` included), and, for
    each distinct scope list, whether it admits a global record with it
    (a global record is decided with the projects its scopes name)."""
    projects = sorted(
        {str(r[0]) for r in store._conn.execute("SELECT project FROM homes")}
        | {str(r[0]) for r in store._conn.execute("SELECT id FROM projects")}
        | {"global"}
    )
    scopes = sorted(
        {str(r[0]) for r in store._conn.execute("SELECT scopes_json FROM memories")}
    )
    return {
        "projects": {p: admission.admits_project(p) for p in projects},
        "global_rows": [
            [json.loads(s), admission.admits_row(json.loads(s), "global")]
            for s in scopes
        ],
    }


def _extra_writes(store: Store, memories: list[Memory]) -> None:
    """The store writes beyond the records, for their log rows."""
    victim = memories[-1].id
    store.tombstone(victim, "superseded", now=_NOW)
    store.restore(victim)
    episode = store.write_episode(
        session_id="session-0001", body="Synthetic journal entry.", now=_NOW
    )
    store.delete_episode(episode.id)
    store.mark_verified(memories[0].id, verified_paths=["README.md"])
    store.put_conflict(
        {
            "id": "conflict-0001",
            "a_id": memories[0].id,
            "b_id": memories[1].id,
            "summary_a": "a",
            "summary_b": "b",
            "similarity": 0.75,
            "method": "jaccard",
            "detector": "fixture",
            "created": _NOW.isoformat(),
            "status": "open",
        }
    )
    store.delete_conflict("conflict-0001")
    store.put_import(
        "v8:fixture",
        content_hash="0" * 64,
        imported_at=_NOW.isoformat(),
        memory_id=None,
    )
    store.delete_import("v8:fixture")
    store.put_quarantine(
        "fixture.md", reason="unparsable", at=_NOW.isoformat(), detail="d", size=3
    )
    store.delete_quarantine("fixture.md")


#: The small store's vocabulary: few words, so many records tie.
_WORDS = ("ledger", "vault", "quill", "harbor", "ember", "cobalt")
_BIG = 236
_BIG_SCOPES = (
    ["tools"],
    ["tools", "deploy"],
    ["notes", "projects:alpha"],
    ["projects:beta"],
)
_BIG_ACTORS: tuple[Actor | None, ...] = (
    None,
    Actor(client="cli-a"),
    Actor(model="m-1"),
    Actor(client="cli-b", model="m-1"),
    Actor(client=""),
)


def _big_memories() -> list[Memory]:
    """The larger store's records: most bodies hold ``ledger`` (so a
    query for it matches more than a pool holds), bodies repeat every 12
    records (exact `bm25()` ties), ``updated`` repeats every 4 (browse
    ties), one or two scopes, several writers."""
    out = []
    for i in range(_BIG):
        pattern = i % 12
        words = [] if i % 16 == 0 else ["ledger"]
        words += [_WORDS[1 + pattern % 5]] * (1 + pattern % 2)
        if pattern % 3 == 0:
            words.append("quill")
        updated = _NOW - timedelta(hours=i // 4)
        out.append(
            Memory(
                id=f"01K{i:023d}",
                created=updated - timedelta(hours=1),
                updated=updated,
                scopes=_BIG_SCOPES[(i // 2) % len(_BIG_SCOPES)],
                confidence=list(Confidence)[i % len(list(Confidence))],
                source=Source.EXPLICIT,
                body=" ".join(words),
                actor=_BIG_ACTORS[i % len(_BIG_ACTORS)],
            )
        )
    return out


#: Filter combinations the larger store's runs pass.
_FILTERS: tuple[dict[str, Any], ...] = (
    {},
    {"scopes": ["deploy"]},
    {"excluded": ["deploy"]},
    {"scopes": ["tools"], "excluded": ["deploy"]},
    {"scopes": []},
    {"client": "cli-a"},
    {"model": "m-1"},
    {"client": "cli-b", "model": "m-1"},
    {"client": ""},
)
#: (pool size, result width) pairs, some cutting the pool.
_WIDTHS = ((200, 10), (50, 5), (5, 10), (1, 1), (300, 50))
_BIG_QUERIES = ("ledger", "vault quill", "")
#: Who the larger store's runs search as. The search surfaces admit a
#: global record whatever projects its scopes name; the write review's
#: surface (REVIEW_STRATA, which `resolve_search_pool` also serves)
#: decides it by them, so its admission tells scope lists apart.
_BIG_WHO = (
    "offline",
    "alpha/auto_scope",
    "alpha/review_strata",
    "beta/unscoped",
    "beta/review_strata",
)

#: The small stores: a digest brought current, then written (or edited)
#: before the queries run. ``puts``: new records; ``update``: an existing
#: record rewritten; ``tombstone``, ``restore``; ``sql``: a recipe.


def _unseen_delete(payload: str) -> str:
    """SQL appending a ``memory_delete`` log row with `payload` (an SQL
    expression) after every other: a mutation the digest has not seen,
    naming a record the store does not hold."""
    return (
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "SELECT MAX(seq) + 1, '2026-06-01T12:00:00Z', NULL, 'memory_delete', "
        f"{payload}, '', printf('%064x', MAX(seq) + 1) FROM log"
    )


#: Unseen log rows whose id `_mutated_ids` reads through `str()`, or
#: whose payload `json.loads` reads from bytes or refuses.
_ODD_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("id null", """'{"id": null}'"""),
    ("id float", """'{"id": 1.5}'"""),
    ("id true", """'{"id": true}'"""),
    ("blob payload", """CAST('{"id": "gone"}' AS BLOB)"""),
    ("blob payload with bom", """X'EFBBBF7B226964223A2022676F6E65227D'"""),
    ("blob payload not utf-8", """X'7B226964223A2280227D'"""),
    ("text payload with bom", """char(65279) || '{"id": "gone"}'"""),
)


_DIGEST_CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("current", {}),
    ("one put", {"puts": 1}),
    ("several puts", {"puts": 3}),
    ("far behind", {"puts": 15}),
    ("update", {"update": True}),
    ("tombstone", {"tombstone": True}),
    ("restore", {"restore": True}),
    (
        "other fingerprint",
        {"sql": ["UPDATE digest_state SET value = 'other' WHERE key = 'fingerprint'"]},
    ),
    ("no digest", {"sql": ["DROP TABLE digest_state"]}),
    (
        "real dl",
        {
            "sql": [
                "UPDATE digest_records SET dl = 3.5 WHERE doc = "
                "(SELECT MIN(doc) FROM digest_records WHERE removed = 0)"
            ]
        },
    ),
    (
        "text rid",
        {
            "sql": [
                "UPDATE digest_records SET rid = 'abc' WHERE doc = "
                "(SELECT MIN(doc) FROM digest_records WHERE removed = 0)"
            ]
        },
    ),
    (
        "one put, other fingerprint",
        {
            "puts": 1,
            "sql": [
                "UPDATE digest_state SET value = 'other' WHERE key = 'fingerprint'"
            ],
        },
    ),
    *(
        (f"unseen {label}", {"sql": [_unseen_delete(sql)]})
        for label, sql in _ODD_PAYLOADS
    ),
)
_SMALL_QUERIES = ("docker", "staging cluster deploy")


def _put_note(store: Store, k: int, home: Any) -> None:
    store.put_memory(
        Memory(
            id=f"01M{k:023d}",
            created=_NOW - timedelta(days=1, hours=k),
            updated=_NOW - timedelta(hours=k),
            scopes=["tools"],
            confidence=Confidence.HIGH,
            source=Source.EXPLICIT,
            body=f"docker staging note {k} with the deploy cluster words",
        ),
        home=home,
    )


def _new_store(
    tmp: Path, name: str, names: Sequence[str]
) -> tuple[Store, Config, dict[str, Any]]:
    # Directories of their own, never named after the case (see _store_dump).
    root = Path(tempfile.mkdtemp(prefix="store-", dir=tmp))
    store = Store.create(root / "store", keys_dir=root / "keys")
    cfg = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        behavior=BehaviorConfig(),
    )
    callers = {
        n: location.resolve_caller(store, cfg, str(tmp / n), session=None)
        for n in names
    }
    return store, cfg, callers


def _admission_of(callers: dict[str, Any], who: str) -> tuple[Any, Any]:
    if who == "offline":
        return None, None
    name, label = who.split("/")
    c = callers[name]
    surface = REVIEW_STRATA if label == "review_strata" else _SURFACES[label]
    return Admission.of(surface, c), Anchoring(c.model, c.resolution)


def _record(
    built: _Built,
    store: Store,
    tmp: Path,
    name: str,
    queries: Sequence[str],
    recipe: Sequence[str],
    callers: dict[str, Any],
    who: Sequence[str],
) -> None:
    schema, rows = _store_dump(store, tmp, name.replace(" ", "-"), queries, recipe)
    if not built.schema:
        built.schema = schema
    assert built.schema == schema, name
    built.stores[name] = {
        "tables": rows,
        "recipe": list(recipe),
        "admissions": {
            w: _admission_data(store, _admission_of(callers, w)[0])
            for w in who
            if w != "offline"
        },
    }


def _line(
    built: _Built,
    store: Store,
    cfg: Config,
    callers: dict[str, Any],
    name: str,
    query: str,
    mode: str,
    who: str,
    filters: dict[str, Any],
    size: int,
    max_results: int,
) -> None:
    admission, anchoring = _admission_of(callers, who)
    built.lines.append(
        {
            "store": name,
            "query": query,
            "mode": mode,
            "who": who,
            "filters": filters,
            "size": size,
            "max_results": max_results,
            "result": _guarded(
                store,
                cfg,
                query,
                mode,
                admission,
                anchoring,
                filters=filters,
                size=size,
                max_results=max_results,
                compact=True,
            ),
        }
    )


def _big_store(built: _Built, tmp: Path) -> None:
    store, cfg, callers = _new_store(tmp, "big", _CALLERS)
    try:
        homes = ("alpha", "beta", "plain", None)
        memories = _big_memories()
        for i, memory in enumerate(memories):
            home = homes[i % len(homes)]
            store.put_memory(
                memory, home=None if home is None else callers[home].home()
            )
        for i, memory in enumerate(memories):
            if i % 17 == 3:
                store.tombstone(memory.id, "superseded", now=_NOW)
        assert store.digest_mark() is not None
        _record(built, store, tmp, "big", _BIG_QUERIES, (), callers, _BIG_WHO)
        for filters in _FILTERS:
            for who in _BIG_WHO[:3]:
                _line(
                    built,
                    store,
                    cfg,
                    callers,
                    "big",
                    "ledger",
                    "hybrid",
                    who,
                    filters,
                    POOL_SIZE,
                    10,
                )
        for size, width in _WIDTHS:
            for query in _BIG_QUERIES:
                for mode in _MODES if size == POOL_SIZE else ("hybrid",):
                    _line(
                        built,
                        store,
                        cfg,
                        callers,
                        "big",
                        query,
                        mode,
                        "offline",
                        {},
                        size,
                        width,
                    )
        for mode in _MODES:
            _line(
                built,
                store,
                cfg,
                callers,
                "big",
                "ledger",
                mode,
                "beta/review_strata",
                {"excluded": ["notes"]},
                20,
                10,
            )
    finally:
        store.close()


def _small_store(built: _Built, tmp: Path, case: str, spec: dict[str, Any]) -> None:
    name = f"digest: {case}"
    store, cfg, callers = _new_store(
        tmp, case.replace(" ", "-").replace(",", ""), ("alpha",)
    )
    try:
        memories = _memories()[:6]
        home = callers["alpha"].home()
        for i, memory in enumerate(memories):
            store.put_memory(memory, home=home if i % 2 == 0 else None)
        assert store.digest_mark() is not None
        for k in range(int(spec.get("puts", 0))):
            _put_note(store, k, home if k % 2 == 0 else None)
        if spec.get("update"):
            store.put_memory(
                memories[0].model_copy(
                    update={"body": memories[0].body + " docker docker staging"}
                ),
                home=home,
            )
        if spec.get("tombstone"):
            store.tombstone(memories[2].id, "superseded", now=_NOW)
        if spec.get("restore"):
            store.tombstone(memories[2].id, "superseded", now=_NOW)
            assert store.digest_mark() is not None
            store.restore(memories[2].id)
        _record(
            built,
            store,
            tmp,
            name,
            _SMALL_QUERIES,
            spec.get("sql", ()),
            callers,
            ("alpha/auto_scope",),
        )
        for query in _SMALL_QUERIES:
            for mode in ("hybrid",):
                for who in ("offline", "alpha/auto_scope"):
                    _line(
                        built,
                        store,
                        cfg,
                        callers,
                        name,
                        query,
                        mode,
                        who,
                        {},
                        POOL_SIZE,
                        10,
                    )
    finally:
        store.close()


@lru_cache(maxsize=1)
def build() -> _Built:
    built = _Built()
    with tempfile.TemporaryDirectory() as raw, _neutral_git(Path(raw)):
        tmp = Path(raw)
        _repo(tmp / "alpha", "https://example.invalid/fixture/alpha.git", 1)
        _repo(tmp / "beta", "https://example.invalid/fixture/beta.git", 2)
        (tmp / "plain").mkdir()
        store = Store.create(tmp / "store", keys_dir=tmp / "keys")
        try:
            cfg = Config(
                storage=StorageConfig(directory=str(store.path.parent)),
                behavior=BehaviorConfig(),
            )
            callers = {
                name: location.resolve_caller(store, cfg, str(tmp / name), session=None)
                for name in _CALLERS
            }
            ids = {
                str(c.resolution.project): name
                for name, c in callers.items()
                if c.resolution.project is not None
            }
            ids["global"] = "global"
            memories = _memories()
            for i, memory in enumerate(memories):
                home = _HOMES[i % len(_HOMES)]
                store.put_memory(
                    memory, home=None if home is None else callers[home].home()
                )
                built.homes[memory.id] = (
                    None if home is None else _labels(callers[home].home().project, ids)
                )
            for name, c in callers.items():
                built.callers[name] = {
                    "directory": name,
                    "outcome": c.resolution.outcome,
                    "project": _labels(c.resolution.project, ids),
                }
            for name, c in callers.items():
                for label, surface in _SURFACES.items():
                    built.admissions[f"{name}/{label}"] = _admission_data(
                        store, Admission.of(surface, c)
                    )
            built.digest_fingerprint = store_digest.fingerprint()
            for query in _QUERIES:
                for mode in _MODES:
                    run: dict[str, Any] = {"query": query, "mode": mode}
                    run["offline"] = _run(store, cfg, query, mode, None, None)
                    for name, c in callers.items():
                        anchoring = Anchoring(c.model, c.resolution)
                        for label, surface in _SURFACES.items():
                            run[f"{name}/{label}"] = _run(
                                store,
                                cfg,
                                query,
                                mode,
                                Admission.of(surface, c),
                                anchoring,
                            )
                    built.runs.append(run)
            alpha = callers["alpha"]
            for case in _HOSTILE:
                query = hostile_query(case)
                for mode in _MODES:
                    built.hostile.append(
                        {
                            "hostile": case["label"],
                            "query": case["query"],
                            "times": case.get("times", 1),
                            "mode": mode,
                            "offline": _guarded(store, cfg, query, mode, None, None),
                            "alpha/auto_scope": _guarded(
                                store,
                                cfg,
                                query,
                                mode,
                                Admission.of(SEARCH_AUTO_SCOPE, alpha),
                                Anchoring(alpha.model, alpha.resolution),
                            ),
                        }
                    )
            schema, rows = _store_dump(store, tmp, "main", _QUERIES)
            built.schema = schema
            built.store = {"tables": rows}
            _extra_writes(store, memories)
            built.rows = [
                (str(kind), session, str(payload))
                for kind, session, payload in store._conn.execute(
                    "SELECT kind, session, payload FROM log ORDER BY seq"
                )
            ]
        finally:
            store.close()
        _big_store(built, tmp)
        for label, spec in _DIGEST_CASES:
            _small_store(built, tmp, label, spec)
    return built


def log_rows() -> list[tuple[str, str | None, str]]:
    """(kind, session, payload text) of every row the build wrote."""
    return build().rows


def _in_memory(query: str, mode: str) -> list[dict[str, Any]]:
    bsearch.clear_token_cache()
    with raw_scores() as raw:
        hits = bsearch.search(
            _memories(),
            query,
            now=_NOW,
            max_results=10,
            path_drift=False,
            mode=mode,  # type: ignore[arg-type]
        )
    return hit_records(hits, raw)


def generate() -> dict[str, bytes]:
    built = build()
    records: list[Any] = [
        {
            "fixture": "search_store",
            "now": _NOW.isoformat(),
            "max_results": 10,
            "records": [{"id": rid, "home": home} for rid, home in built.homes.items()],
            "home_legend": (
                "alpha and beta: a project (a git repository with a remote); "
                "global: written outside any repository; null: no homes row"
            ),
            "callers": built.callers,
            "surfaces": _SURFACES,
            "runs": (
                "offline: no admission; '<caller>/<surface>': the caller's "
                "admission on that surface. pool: the candidate ids in pool "
                "order (FTS5 rank through the admission); hits: ranked; "
                "stats: each call of the pool's statistics, [terms, size, "
                "body_df, scope_df, avgdl] or [terms, null]."
            ),
            "admissions": built.admissions,
            "digest_fingerprint": built.digest_fingerprint,
            "store": built.store,
            "schema": built.schema,
            "stores": built.stores,
            "lines": (
                "a line with 'store' runs one query on that store (a "
                "'stores' entry: its tables' rows and the SQL applied after them) "
                "with the filters, pool size and result width it names; "
                "its pool is row numbers, each hit [id, bits, raw_bits, "
                "relevance, match_terms]; avgdl is IEEE-754 bits"
            ),
        }
    ]
    for run in built.runs:
        offline = run["offline"]["hits"]
        memory = _in_memory(run["query"], run["mode"])
        records.append({**run, "in_memory_equal": offline == memory})
    records.extend(built.hostile)
    records.extend(built.lines)
    return {"search_store.jsonl": jsonl(records)}
