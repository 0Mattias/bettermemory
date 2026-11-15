"""Fixture 4: the store's schema, its upgrade steps, and ranked search.

- ``store_schema.json``: a store `Store.create` makes, as its
  ``sqlite_master`` rows (type, name, tbl_name, sql; rootpage left out),
  its pragmas, its meta rows and its first log row's payload keys. Values
  minted at random or from the clock or the version are masked.
- ``store_steps.jsonl``: from the base `SCHEMA` alone, each step of
  `store_steps.STEPS` applied in order through its own ``apply``, with
  the objects it added and the meta keys it set.
- ``search.jsonl``: a fixed synthetic corpus and queries, with the hits
  `search.search` returns in each mode (ids, scores as float repr and
  IEEE-754 bits, relevance, match terms), and the FTS5 prefilter
  (`store_index.query_candidates`'s query) over the same corpus: matched
  rows in ``bm25(memories_fts)`` order with the scores.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import search as bsearch
from bettermemory.models import Confidence, Memory, Source
from bettermemory.search_lexicon import fts_index_text, fts_match_query
from bettermemory.store import Store
from bettermemory.store_schema import SCHEMA
from bettermemory.store_steps import STEPS

from ._common import hit_records, jsonl, raw_scores

_MASKED_META = {
    "store_id": "<32 lowercase hex, random>",
    "membership_store_id": "<32 lowercase hex, random>",
    "key_fingerprint": "<sha256 hex of the random key>",
    "created": "<isoformat_utc of the clock>",
    "engine_version": "<package version>",
}
_PRAGMAS = (
    "application_id",
    "user_version",
    "journal_mode",
    "auto_vacuum",
    "encoding",
    "foreign_keys",
)


def _master(conn: sqlite3.Connection) -> list[list[Any]]:
    return [
        list(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]


@contextmanager
def _scratch() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _fresh_store() -> dict[str, Any]:
    with _scratch() as tmp:
        store = Store.create(tmp / "store", keys_dir=tmp / "keys")
        store.close()
        conn = sqlite3.connect(tmp / "store" / "memory.sqlite")
        try:
            master = _master(conn)
            pragmas = {p: conn.execute(f"PRAGMA {p}").fetchone()[0] for p in _PRAGMAS}
            meta = {
                str(k): _MASKED_META.get(str(k), v)
                for k, v in conn.execute("SELECT key, value FROM meta")
            }
            first = conn.execute(
                "SELECT seq, session, kind, payload FROM log ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
    return {
        "fixture": "store_schema",
        "sqlite_master": master,
        "pragmas": pragmas,
        "meta": meta,
        "masked_meta": _MASKED_META,
        "log": [
            {
                "seq": seq,
                "session": session,
                "kind": kind,
                "payload_keys": sorted(json.loads(payload)),
            }
            for seq, session, kind, payload in first
        ],
    }


def _steps() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _scratch() as tmp:
        path = tmp / "memory.sqlite"
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.executescript(SCHEMA)
            out.append({"fixture": "store_steps", "base": _master(conn)})
            meta: dict[str, str] = {}
            for step in STEPS:
                before = {row[1]: row for row in _master(conn)}
                step.apply(conn, path, meta)
                after = _master(conn)
                out.append(
                    {
                        "step": step.number,
                        "name": step.name,
                        "additive": step.additive,
                        "reader": step.reader,
                        "writer": step.writer,
                        "added": [row for row in after if row[1] not in before],
                        "removed": sorted(
                            set(before) - {row[1] for row in after}, key=str
                        ),
                        "meta_keys": sorted(meta),
                        "meta_rows": sorted(
                            str(k)
                            for (k,) in conn.execute("SELECT key FROM meta").fetchall()
                        ),
                    }
                )
        finally:
            conn.close()
    return out


_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_BODIES = (
    (
        "tools",
        "Use docker compose for the staging cluster; the compose file lives in deploy/.",
    ),
    ("tools", "Docker containers restart with the always policy on the build hosts."),
    ("db", "Postgres 16.3 replaced 15.4; the upgrade ran on 2026-03-02."),
    ("db", "The postgres connection pool caps at 40 connections per service."),
    ("style", "Prefer snake_case for Python identifiers and kebab-case for CLI flags."),
    ("style", "Write commit messages in the imperative mood."),
    (
        "search",
        "The FTS5 index stores the tokenizer's own output, so ranking and MATCH agree.",
    ),
    ("search", "BM25 scoring with k1 1.2 and b 0.75 ranks the candidates."),
    ("i18n", "Kontoret i Zürich flyttar till Tjörn nästa år."),
    ("i18n", "東京オフィスは移転する予定です。"),
    ("i18n", "Das Büro in München schließt im Dezember."),
    ("api", "The APIs return JSON; every endpoint is versioned under /v2/."),
    ("api", "The api gateway rejects requests over 1 MB."),
    ("misc", "C++20 modules and C# records were evaluated, .NET 8 was chosen."),
    ("misc", "What's the policy on caches? The cache policies expire after one hour."),
    ("misc", "python-frontmatter parses the header of every note."),
    ("misc", "Emoji in titles 😀 are allowed but discouraged."),
    ("misc", "Standups happen daily at 09:30; the standup notes go in the wiki."),
)

_QUERIES = (
    "docker compose",
    "docker-compose staging",
    "postgres 16",
    "postgres upgrade 16.3",
    "connection pool",
    "snake_case identifiers",
    "kebab-case",
    "commit message style",
    "fts5 tokenizer ranking",
    "bm25",
    "zurich",
    "Zürich kontoret",
    "東京",
    "移転",
    "munchen buro",
    "api",
    "apis versioned",
    "c++",
    ".net",
    "cache policy",
    "frontmatter",
    "python-frontmatter",
    "emoji 😀",
    "standup",
    "what is the policy",
    "the",
    "nothing matches this query xyzzy",
)

_MODES: dict[str, dict[str, Any]] = {
    "keyword": {"mode": "keyword"},
    "bm25": {"mode": "bm25"},
    "hybrid": {"mode": "hybrid"},
    "hybrid_plain": {"mode": "hybrid", "conversational": False},
    "hybrid_rescue": {"mode": "hybrid", "rescue_expansion": True},
}


def _memories() -> list[Memory]:
    out = []
    for i, (scope, body) in enumerate(_BODIES):
        created = _NOW - timedelta(days=3 * i + 1, hours=i)
        out.append(
            Memory(
                id=f"01J{i:023d}",
                created=created,
                updated=created + timedelta(hours=1),
                scopes=[scope],
                confidence=list(Confidence)[i % len(list(Confidence))],
                source=Source.EXPLICIT,
                body=body,
            )
        )
    return out


def _bits(value: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', value))[0]:016x}"


def _fts(memories: list[Memory]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE memories_fts USING fts5("
        "body_fts, scopes_fts, tokenize='unicode61')"
    )
    conn.executemany(
        "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
        [
            (i + 1, fts_index_text(m.body), fts_index_text(" ".join(m.scopes)))
            for i, m in enumerate(memories)
        ],
    )
    return conn


def _search_records() -> list[dict[str, Any]]:
    memories = _memories()
    records: list[dict[str, Any]] = [
        {
            "fixture": "search",
            "now": _NOW.isoformat(),
            "half_life_days": 30.0,
            "max_results": 10,
            "memories": [
                {
                    "rowid": i + 1,
                    "id": m.id,
                    "created": m.created.isoformat(),
                    "updated": m.updated.isoformat(),
                    "scopes": m.scopes,
                    "confidence": m.confidence.value,
                    "source": m.source.value,
                    "body": m.body,
                    "index_text": fts_index_text(m.body),
                }
                for i, m in enumerate(memories)
            ],
            "modes": _MODES,
            "fts_sql": (
                "SELECT rowid, bm25(memories_fts) FROM memories_fts WHERE "
                "memories_fts MATCH ? ORDER BY bm25(memories_fts), rowid"
            ),
        }
    ]
    fts = _fts(memories)
    try:
        for query in _QUERIES:
            out: dict[str, Any] = {}
            for name, options in _MODES.items():
                bsearch.clear_token_cache()
                with raw_scores() as raw:
                    hits = bsearch.search(
                        list(memories),
                        query,
                        now=_NOW,
                        max_results=10,
                        path_drift=False,
                        **options,
                    )
                out[name] = hit_records(hits, raw)
            match = fts_match_query(query)
            fts_rows = (
                []
                if not match
                else fts.execute(
                    "SELECT rowid, bm25(memories_fts) FROM memories_fts WHERE "
                    "memories_fts MATCH ? ORDER BY bm25(memories_fts), rowid",
                    (match,),
                ).fetchall()
            )
            records.append(
                {
                    "query": query,
                    "match_query": match,
                    "fts": [
                        {"rowid": r, "bm25": repr(s), "bits": _bits(s)}
                        for r, s in fts_rows
                    ],
                    "hits": out,
                }
            )
    finally:
        fts.close()
    return records


def generate() -> dict[str, bytes]:
    return {
        "store_schema.jsonl": jsonl([_fresh_store()]),
        "store_steps.jsonl": jsonl(_steps()),
        "search.jsonl": jsonl(_search_records()),
    }
