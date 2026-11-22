"""Fixtures: the episode journal's reads (``store_journal.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/store/journal.rs``. This pins it to
`store_journal`'s `get_episode`, `iter_episodes`, `count_episodes`,
`episodes_by_session`, `episodes_by_swarm`, `episode_session_ids`,
`prunable_episode_sessions` and `episode_volume`, each called with a
store whose only part they reach is ``_conn`` (an in-memory connection
holding `store_schema.SCHEMA`, rows as `sqlite3.Row`) and the two
methods they call on it.

The header holds the schema text. Each case holds ``rows``, the
``episodes`` rows inserted in order (``id``, ``session_id``,
``created``, ``body``, ``scopes_json``, ``takeaway``, ``is_floor``,
``swarm_id``; no origin, no log MAC), and ``calls``, each ``do`` (the
function), its arguments and ``r``: the answer (episodes as
``model_dump(mode="json")``, an `EpisodeVolume` as ``to_dict``) or
``{"raised": class, "arg": the exception's first argument}``.

Gated on pydantic-core: the episodes are read through the model.
"""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime
from typing import Any

from bettermemory import store_journal
from bettermemory.store_schema import SCHEMA

COLUMNS = (
    "id",
    "session_id",
    "created",
    "body",
    "scopes_json",
    "takeaway",
    "is_floor",
    "swarm_id",
)

NOW = "2026-03-01T00:00:00+00:00"


def _store(rows: list[list[Any]]) -> Any:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    marks = ", ".join("?" for _ in COLUMNS)
    for row in rows:
        conn.execute(
            f"INSERT INTO episodes ({', '.join(COLUMNS)}) VALUES ({marks})", row
        )
    conn.commit()
    store: Any = types.SimpleNamespace(_conn=conn)
    store.iter_episodes = lambda **kw: store_journal.iter_episodes(store, **kw)
    store.prunable_episode_sessions = lambda **kw: (
        store_journal.prunable_episode_sessions(store, **kw)
    )
    return store


def _answer(value: Any) -> Any:
    if isinstance(value, list):
        return [_answer(v) for v in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _call(store: Any, call: dict[str, Any]) -> Any:
    do = call["do"]
    kw: dict[str, Any] = {}
    if "ttl" in call:
        kw["ttl_days"] = call["ttl"]
    if "now" in call:
        kw["now"] = datetime.fromisoformat(call["now"])
    try:
        if do == "get":
            out: Any = store_journal.get_episode(store, call["a"])
        elif do == "iter":
            out = list(store_journal.iter_episodes(store, session_id=call.get("a")))
        elif do == "count":
            out = store_journal.count_episodes(store)
        elif do == "session":
            out = store_journal.episodes_by_session(store, call["a"])
        elif do == "swarm":
            out = store_journal.episodes_by_swarm(store, call["a"])
        elif do == "ids":
            out = store_journal.episode_session_ids(store)
        elif do == "prunable":
            out = store_journal.prunable_episode_sessions(store, **kw)
        elif do == "volume":
            out = store_journal.episode_volume(store, **kw)
        else:
            raise AssertionError(do)
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        arg = exc.args[0] if exc.args and isinstance(exc.args[0], str) else None
        if type(exc).__name__ != "NotFoundError":
            arg = None
        return {"raised": type(exc).__name__, "arg": arg}
    return _answer(out)


def _id(n: int) -> str:
    """A ULID whose last digits are ``n``."""
    return f"01HZZZZZZZZZZZZZZZZZZZZZ{n:02d}"


def _ep(
    eid: str,
    session: str,
    created: str,
    body: str = "b\n",
    *,
    takeaway: str | None = None,
    swarm: str | None = None,
    scopes: str = "[]",
    floor: int = 0,
) -> list[Any]:
    return [_id(int(eid[1:])), session, created, body, scopes, takeaway, floor, swarm]


def cases() -> list[dict[str, Any]]:
    mixed = [
        _ep("e1", "s1", "2026-01-02T00:00:00+00:00", swarm="w1"),
        _ep("e2", "s1", "2026-01-01T12:00:00+02:00", takeaway="t", swarm="w1"),
        _ep("e3", "s1", "2026-01-01T10:00:00+00:00", swarm="w1"),
        _ep("e4", "s2", "2026-01-01T10:00:00Z", "é€\n", swarm="w2"),
        _ep("e5", "s1", "2026-01-01T10:00:00", scopes='["repo:x"]', floor=1),
        _ep("e6", "s3", "2026-02-20T00:00:00.000001+00:00", takeaway="é"),
        _ep("e7", "s2", "2025-12-01T00:00:00-05:00", swarm="w1"),
        _ep("e8", "s4", "2026-01-31T00:00:00+00:00"),
    ]
    common_calls: list[dict[str, Any]] = [
        {"do": "get", "a": _id(2)},
        {"do": "get", "a": _id(9)},
        {"do": "get", "a": ""},
        {"do": "iter"},
        {"do": "iter", "a": "s1"},
        {"do": "iter", "a": "none"},
        {"do": "count"},
        {"do": "session", "a": "s1"},
        {"do": "session", "a": "s2"},
        {"do": "session", "a": "none"},
        {"do": "swarm", "a": "w1"},
        {"do": "swarm", "a": "w2"},
        {"do": "swarm", "a": "none"},
        {"do": "ids"},
    ]
    sweep = [
        {"do": "prunable", "ttl": ttl, "now": NOW}
        for ttl in (0, -1, 1, 29, 30, 31, 60, 100000, 999999999, 1000000000, 739000)
    ]
    sweep += [
        {"do": "prunable", "ttl": 30, "now": "2026-03-01T00:00:00"},
        {"do": "prunable", "ttl": 30, "now": "2026-03-02T23:00:00+05:00"},
        {"do": "prunable", "ttl": 1, "now": "0001-01-01T00:00:00+00:00"},
        {"do": "prunable", "ttl": 0},
        {"do": "volume", "ttl": 30, "now": NOW},
        {"do": "volume", "ttl": 0},
        {"do": "volume", "ttl": 1000000000, "now": NOW},
    ]
    return [
        {"case": "mixed", "rows": mixed, "calls": common_calls + sweep},
        {
            "case": "empty",
            "rows": [],
            "calls": [
                {"do": "count"},
                {"do": "iter"},
                {"do": "ids"},
                {"do": "get", "a": _id(1)},
                {"do": "session", "a": "s1"},
                {"do": "prunable", "ttl": 30, "now": NOW},
                {"do": "volume", "ttl": 30, "now": NOW},
            ],
        },
        {
            "case": "bad_created",
            "rows": [
                _ep("e1", "s1", "2026-01-01T00:00:00+00:00"),
                _ep("e2", "s1", "garbage"),
            ],
            "calls": [
                {"do": "get", "a": _id(1)},
                {"do": "get", "a": _id(2)},
                {"do": "iter"},
                {"do": "session", "a": "s1"},
                {"do": "prunable", "ttl": 1, "now": NOW},
                {"do": "volume", "ttl": 1, "now": NOW},
            ],
        },
    ]


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {"fixture": "store_journal", "schema": SCHEMA, "columns": list(COLUMNS)}
    ]
    for case in cases():
        store = _store(case["rows"])
        calls = [{**c, "r": _call(store, c)} for c in case["calls"]]
        store._conn.close()
        lines.append({"case": case["case"], "rows": case["rows"], "calls": calls})
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_journal.jsonl": text.encode("ascii")}
