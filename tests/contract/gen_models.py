"""Fixture N1d: records as the reference's models validate and write them.

A stored row and a log payload become a record through the pydantic models
of `bettermemory.models` (`Memory`, `TombstonedMemory`, `Episode`,
`MemoryLink`), `origin_model.Origin` and `identity.Actor`, fed by
`store_rows`' readers. The native build reproduces them; these files pin
what the reference does with every input, valid or not.

Two files:

- ``models.jsonl``: the PAYLOAD form. The input is the record as the fold
  reads it out of a log payload (`json.loads`), handed to
  `<Model>.model_validate`. Nothing in it depends on the Python release,
  the Unicode tables or the platform (pydantic-core is the locked one, the
  JSON is ASCII, the writers are `json.dumps` and `datetime.isoformat`), so
  it is not gated.
- ``models_rows.jsonl``: the ROW form. The input is a row of SQLite values
  handed to `store_rows._row_to_memory`, `_row_to_tombstone` and
  `_row_to_episode`, which read datetimes through `datetime.fromisoformat`
  (whose grammar changed between 3.11 and 3.13): gated on the Python
  release.

The first line of each file is a header: per model its base input (a valid
record or row) and its field list, the value pool, the linear congruence,
the models' ValidationError titles and the table of pydantic error types
the cases name by index. Then three kinds of line:

- a grid line, ``[model, field, outcomes]``: ``outcomes[i]`` is what the
  base with ``field`` set to ``pool[i]`` gives, and in the payload form a
  last entry for the field deleted (``field`` ``"$root"`` sets the whole
  input instead);
- a hand line, ``[model, ops, outcome]``: ``ops`` edits the base in order,
  ``[field, {"v": value}]`` setting and ``[field]`` deleting; a showcase
  case writes ``["full", outcome]`` (the whole outcome, below);
- a battery line, ``{"battery": model, "n", "seed", "digests"}``.

A case's outcome is written compactly: a validated record as the digest of
its whole outcome (8 hex digits), or ``[digest, value]`` where the edited
field's value in the payload dump is spelled otherwise than the value set
(a coercion); another exception as ``"!<class>"``; a ValidationError as
``{"e": errors}`` (``{"t": title, "e": errors}`` when the title is not the
model's own), each error the index of its type when its location is the
edited field alone, else ``[loc, index]``.

A whole outcome is ``["ok", dump, dump_error, row]`` or ``["err", class,
title, errors]``: ``dump`` is the record's payload text as the reference
writes it (`log.canonical_payload` of `models.record_payload` for a memory
and a tombstone, of `model_dump(mode="json")` otherwise), ``dump_error``
the class of the exception the payload writer raised instead (null when it
wrote), ``row`` the column values the reference writes for the record
(`store_rows._record_columns` and its companions; the index text columns
``body_fts`` and ``scopes_fts`` are left out, since they go through the
Unicode tables, and so are the columns the record does not carry);
``errors`` the ``[loc, type]`` pairs in pydantic's order. Its digest is the
sha256 of its compact, sorted, ASCII JSON (`tag`ged), in hex, cut to 8
digits (a battery case: 6; ``models_stores.jsonl``: 16).

Values that JSON cannot carry are tagged: ``{"$cps": [...]}`` is a string
holding a lone surrogate, ``{"$bytes": "<hex>"}`` bytes (a BLOB),
``{"$float": "nan" | "inf" | "-inf"}`` a non-finite float (in the row
pool, text holding a backslash is written as ``$cps`` too),
``{"$dict": [[key, value], ...]}`` a dict whose keys hold a lone surrogate, ``{"$deep": n}``
the string of ``n`` ``[`` then ``n`` ``]``. Every other
value is itself.

The battery: for each model, case ``i`` (0-based) draws from the linear
congruence ``s = (6364136223846793005 * s + 1442695040888963407) mod 2**64``
(seeded with the header's ``seed``; each draw advances ``s`` once and uses
``s >> 33``): first the number of edits ``1 + d % 3``, then per edit the
field ``d % len(fields)`` and the value ``d % (len(pool) + 1)``, where
``len(pool)`` means delete (in the row form: null, or for
``attestation_payload`` delete). The battery line lists the digests of
cases ``0..n-1`` concatenated.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._common import jsonl

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID2 = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
SHA40 = "0123456789abcdef0123456789abcdef01234567"
SHA64 = SHA40 + "89abcdef0123456789abcdef"
SEED = 20261115
BATTERY = 2000
MULT = 6364136223846793005
INC = 1442695040888963407
MASK = (1 << 64) - 1

ORIGIN = {
    "cwd": "/w/x",
    "repo": "git@example.invalid:x.git",
    "branch": "main",
    "worktree_root": "/w/x",
    "source": "env",
}
ACTOR = {
    "client": "c",
    "client_version": "1.0",
    "model": "m",
    "principal": "p",
    "session": "s",
    "sources": {"client": "header", "model": "env"},
}
LINK = {"type": "extends", "target_id": ULID2, "note": "why"}

MEMORY = {
    "id": ULID,
    "created": "2026-01-02T03:04:05.123456+00:00",
    "updated": "2026-01-02T03:04:05Z",
    "scopes": ["projects:foo", "global"],
    "confidence": "high",
    "source": "inferred",
    "body": "A body. \u00e9\u4e2d",
    "origin": ORIGIN,
    "actor": ACTOR,
    "last_verified_at": "2026-01-03T00:00:00+01:00",
    "category": "fact",
    "verified_paths": ["a.py"],
    "verified_commits": [SHA40],
    "verified_versions": ["1.0"],
    "verified_absent_paths": [],
    "verified_path_targets": {"a.py": "a.py", "b.py": None},
    "verified_absent_path_targets": None,
    "claims": ["a.py::f"],
    "verified_head": SHA40,
    "links": [LINK],
    "corroborations": 2,
    "last_corroborated": "2026-01-04T00:00:00Z",
}
TOMBSTONE = {
    **{
        k: v
        for k, v in MEMORY.items()
        if k not in ("links", "corroborations", "last_corroborated")
    },
    "removed": "2026-01-05T00:00:00+00:00",
    "removed_reason": "gone",
    "removed_session": "sess_1",
}
EPISODE = {
    "id": ULID,
    "session_id": "sess_1",
    "created": "2026-01-02T03:04:05Z",
    "body": "Tried a thing.",
    "scopes": ["projects:foo"],
    "takeaway": "It worked.",
    "origin": ORIGIN,
    "is_floor": False,
    "swarm_id": "sess_coord",
}

EXTRA = "an_unknown_field"


def _cps(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


class Deep(str):
    """A string of ``n`` brackets opened and ``n`` closed: deep JSON,
    written ``{"$deep": n}``."""

    depth: int

    def __new__(cls, depth: int) -> Deep:
        made = super().__new__(cls, "[" * depth + "]" * depth)
        made.depth = depth
        return made


def tag(value: Any) -> Any:
    """A value in the file's JSON: tagged where JSON cannot carry it."""
    if isinstance(value, Deep):
        return {"$deep": value.depth}
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, str):
        return {"$cps": [ord(c) for c in value]} if _cps(value) else value
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, list):
        return [tag(v) for v in value]
    if isinstance(value, dict):
        if any(_cps(k) for k in value):
            return {"$dict": [[tag(k), tag(v)] for k, v in value.items()]}
        return {k: tag(v) for k, v in value.items()}
    raise TypeError(type(value).__name__)


# ---------------------------------------------------------------------------
# The reference's answers
# ---------------------------------------------------------------------------


class _Row(dict[str, Any]):
    """What `store_rows` reads a row through: `row[name]` and `row.keys()`."""


def _models() -> dict[str, Any]:
    from bettermemory.identity import Actor
    from bettermemory.models import Episode, Memory, MemoryLink, TombstonedMemory
    from bettermemory.origin import Origin

    return {
        "memory": Memory,
        "tombstone": TombstonedMemory,
        "episode": Episode,
        "origin": Origin,
        "actor": Actor,
        "link": MemoryLink,
    }


def _record_outcome(model: str, record: Any) -> list[Any]:
    """``["ok", dump, dump_error, row]`` for a validated record."""
    from bettermemory import store_rows as sr
    from bettermemory.log import canonical_payload
    from bettermemory.models import record_payload

    dump: str | None = None
    dump_error: str | None = None
    row: list[Any] | None = None
    try:
        if model in ("memory", "tombstone"):
            dump = canonical_payload(record_payload(record))
        else:
            dump = canonical_payload(record.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - recorded as data
        dump_error = type(exc).__name__
    try:
        if model == "memory":
            columns = sr._record_columns(record)
            row = [columns[c] for c in _RECORD_COLUMNS] + [
                sr._scopes_text(record.scopes),
                sr._links_json(record.links),
                record.corroborations,
                sr._iso_opt(record.last_corroborated),
            ]
        elif model == "tombstone":
            columns = sr._record_columns(record)
            row = [columns[c] for c in _RECORD_COLUMNS] + [
                sr._iso(record.removed),
                record.removed_reason,
                record.removed_session,
            ]
        elif model == "episode":
            row = [
                record.id,
                record.session_id,
                sr._iso(record.created),
                record.body,
                sr._json_text(list(record.scopes)),
                record.takeaway,
                sr._origin_json(record.origin),
                1 if record.is_floor else 0,
                record.swarm_id,
            ]
        elif model == "origin":
            row = [sr._origin_json(record)]
        elif model == "actor":
            row = [sr._actor_json(record)]
        else:
            row = [sr._links_json([record])]
    except Exception as exc:  # noqa: BLE001 - recorded as data
        if dump_error is None:
            dump_error = type(exc).__name__
        row = None
    return ["ok", dump, dump_error, row]


def _error_outcome(exc: BaseException) -> list[Any]:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return [
            "err",
            "ValidationError",
            exc.title,
            [[list(e["loc"]), e["type"]] for e in exc.errors()],
        ]
    return ["err", type(exc).__name__, None, None]


def payload_outcome(model: str, data: Any) -> list[Any]:
    cls = _models()[model]
    try:
        record = cls.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - recorded as data
        return _error_outcome(exc)
    return _record_outcome(model, record)


def row_outcome(model: str, row: dict[str, Any]) -> list[Any]:
    from bettermemory import store_rows as sr

    reader: Callable[[Any], Any] = {
        "memory": sr._row_to_memory,
        "tombstone": sr._row_to_tombstone,
        "episode": sr._row_to_episode,
    }[model]
    try:
        record = reader(_Row(row))
    except Exception as exc:  # noqa: BLE001 - recorded as data
        return _error_outcome(exc)
    return _record_outcome(model, record)


_RECORD_COLUMNS = (
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "scopes_json",
    "origin_repo",
    "origin_worktree",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
)


def digest(outcome: list[Any], length: int) -> str:
    text = json.dumps(
        tag(outcome), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

FIELDS: dict[str, list[str]] = {
    "memory": [*MEMORY, EXTRA],
    "tombstone": [*TOMBSTONE, EXTRA],
    "episode": [*EPISODE, EXTRA],
    "origin": [*ORIGIN, EXTRA],
    "actor": [*ACTOR, EXTRA],
    "link": [*LINK, EXTRA],
}
BASES: dict[str, dict[str, Any]] = {
    "memory": MEMORY,
    "tombstone": TOMBSTONE,
    "episode": EPISODE,
    "origin": ORIGIN,
    "actor": ACTOR,
    "link": LINK,
}

#: Every kind of value a JSON payload can hold, at its edges.
GENERAL: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    2,
    -1,
    2**63,
    -(2**63) - 1,
    10**30,
    1.0,
    0.0,
    -0.0,
    1.5,
    1e300,
    float("nan"),
    float("inf"),
    "",
    " ",
    "x",
    "\x00",
    "\ud800",
    "\U0001f600\u0301",
    [],
    ["x"],
    [1],
    [None],
    {},
    {"a": "b"},
]

#: Values the validators single out.
SPECIFIC: list[Any] = [
    ULID,
    ULID2,
    ULID.lower(),
    ULID + "\n",
    ULID[:-1] + "U",
    "projects:foo",
    "a-b:c",
    "Projects",
    "a--b",
    "a:",
    SHA40,
    SHA64,
    " " + SHA40.upper() + "\n",
    SHA40[:-1],
    SHA40[:-1] + "g",
    "\u00a0" + SHA40 + "\u2003",
    "2026-01-02T03:04:05Z",
    "2026-01-02",
    "2026-01-02 03:04",
    "2026-01-02T03:04:05.1234567-23:59",
    "2026-01-02T03:04:05+00:00:30",
    "1700000000",
    "1700000000.5",
    "-1.25",
    "253402300799999",
    "0000-01-01",
    "2026-02-30",
    "2026-01-02T24:00:00",
    1700000000,
    1700000000.25,
    2**63,
    2**1024 - 2**970 - 1,
    2**1024 - 2**970,
    -(2**1024 - 2**970 - 1),
    -(2**1024 - 2**970),
    10**400,
    -62135596801000,
    253402300800000,
    "high",
    "medium",
    "low",
    "HIGH",
    "explicit-statement",
    "inferred",
    "user-correction",
    "fact",
    "user-inference",
    "ambient",
    "supersedes",
    "contradicts",
    "extends",
    "depends_on",
    "true",
    "Off",
    "yes ",
    "0",
    "1",
    "2",
    " 5 ",
    "\x1c5",
    "\x855",
    "1_0",
    "1.00",
    "-3",
    "\u0665",
    "sess_ABC-123",
    "sess abc",
    "s" * 128,
    "s" * 129,
    ["projects:foo"],
    ["Bad"],
    ["projects:foo"] * 64,
    ["projects:foo"] * 65,
    ["a.py", 1],
    [SHA40] * 65,
    {"a.py": "a.py"},
    {"b.py": "b.py"},
    {"a.py": None, "b.py": 1},
    {"a\udfff": "x", "b\ud800\udfff": None},
    {"a\udfff": 1},
    {f"k{i}": None for i in range(65)},
    {"cwd": "/w/y"},
    {"cwd": 1, "repo": None, "extra": 2},
    {"client": "c2", "sources": {"client": "env"}},
    {"sources": {"client": None}},
    {"sources": []},
    LINK,
    {"type": "extends", "target_id": ULID},
    {"type": "bogus", "target_id": "x", "note": 5},
    [LINK],
    [LINK, LINK],
    [{"type": "extends", "target_id": ULID}],
    [{"type": "supersedes", "target_id": ULID2, "note": None}, 7],
    [LINK] * 65,
]


def canon(value: Any) -> Any:
    """The value with every dict's keys in sorted order: the order the file
    (sorted JSON) gives them back in, which is the order pydantic reports a
    dict's errors in."""
    if isinstance(value, dict):
        return {k: canon(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [canon(v) for v in value]
    return value


POOL: list[Any] = canon(GENERAL + SPECIFIC)


def _apply(base: Any, ops: list[list[Any]]) -> Any:
    data = canon(base)
    for op in ops:
        if op[0] == "$root":
            data = canon(op[1])
        elif len(op) == 1:
            data.pop(op[0], None)
        else:
            data[op[0]] = canon(op[1])
    return data


def _resolve(ops: list[list[Any]], pool: list[Any]) -> list[list[Any]]:
    """Edits with each value as the pool index an int names (a literal
    value is wrapped as ``Lit``)."""
    return [
        op
        if len(op) == 1
        else [op[0], op[1].value if isinstance(op[1], Lit) else pool[op[1]]]
        for op in ops
    ]


def _written(ops: list[list[Any]]) -> list[Any]:
    """Edits as the file writes them: a pool index, or ``{"v": value}``."""
    return [
        op
        if len(op) == 1
        else [op[0], {"v": tag(op[1].value)} if isinstance(op[1], Lit) else op[1]]
        for op in ops
    ]


def _lit(ops: list[list[Any]]) -> list[list[Any]]:
    return [op if len(op) == 1 else [op[0], Lit(op[1])] for op in ops]


class Lit:
    """A literal value in a hand-written edit (not a pool index)."""

    def __init__(self, value: Any) -> None:
        self.value = value


def _lcg(seed: int) -> Callable[[], int]:
    state = [seed & MASK]

    def draw() -> int:
        state[0] = (MULT * state[0] + INC) & MASK
        return state[0] >> 33

    return draw


def battery_ops(
    fields: list[str], pool_len: int, n: int, seed: int
) -> list[list[list[Any]]]:
    draw = _lcg(seed)
    cases = []
    for _ in range(n):
        ops: list[list[Any]] = []
        for _ in range(1 + draw() % 3):
            f = draw() % len(fields)
            v = draw() % (pool_len + 1)
            ops.append([f, v])
        cases.append(ops)
    return cases


# ---------------------------------------------------------------------------
# Row inputs
# ---------------------------------------------------------------------------

ROW_COLUMNS: dict[str, list[str]] = {
    "memory": [
        "id",
        "created",
        "updated",
        "scopes_json",
        "confidence",
        "source",
        "body",
        "origin_json",
        "actor_json",
        "last_verified_at",
        "category",
        "verified_paths_json",
        "verified_commits_json",
        "verified_versions_json",
        "verified_absent_paths_json",
        "claims_json",
        "verified_head",
        "links_json",
        "corroborations",
        "last_corroborated",
        "attestation_payload",
    ],
    "tombstone": [
        "id",
        "created",
        "updated",
        "scopes_json",
        "confidence",
        "source",
        "body",
        "origin_json",
        "actor_json",
        "last_verified_at",
        "category",
        "verified_paths_json",
        "verified_commits_json",
        "verified_versions_json",
        "verified_absent_paths_json",
        "claims_json",
        "verified_head",
        "removed",
        "removed_reason",
        "removed_session",
        "attestation_payload",
    ],
    "episode": [
        "id",
        "session_id",
        "created",
        "body",
        "scopes_json",
        "takeaway",
        "origin_json",
        "is_floor",
        "swarm_id",
        "attestation_payload",
    ],
}


def _attest(key: str, record: dict[str, Any], native: Any = None) -> str:
    payload: dict[str, Any] = {key: record, "provenance": "native"}
    if native is not None:
        payload["native_origin"] = native
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _base_rows() -> dict[str, dict[str, Any]]:
    """Each base row as the reference writes it for the base record."""
    from bettermemory import store_rows as sr

    models = _models()
    rows: dict[str, dict[str, Any]] = {}
    memory = models["memory"].model_validate(MEMORY)
    columns = sr._record_columns(memory)
    rows["memory"] = {
        **{c: columns[c] for c in ROW_COLUMNS["memory"] if c in columns},
        "links_json": sr._links_json(memory.links),
        "corroborations": memory.corroborations,
        "last_corroborated": sr._iso_opt(memory.last_corroborated),
        "attestation_payload": None,
    }
    tomb = models["tombstone"].model_validate(TOMBSTONE)
    columns = sr._record_columns(tomb)
    rows["tombstone"] = {
        **{c: columns[c] for c in ROW_COLUMNS["tombstone"] if c in columns},
        "removed": sr._iso(tomb.removed),
        "removed_reason": tomb.removed_reason,
        "removed_session": tomb.removed_session,
        "attestation_payload": None,
    }
    ep = models["episode"].model_validate(EPISODE)
    rows["episode"] = {
        "id": ep.id,
        "session_id": ep.session_id,
        "created": sr._iso(ep.created),
        "body": ep.body,
        "scopes_json": sr._json_text(list(ep.scopes)),
        "takeaway": ep.takeaway,
        "origin_json": sr._origin_json(ep.origin),
        "is_floor": 0,
        "swarm_id": ep.swarm_id,
        "attestation_payload": None,
    }
    for model in rows:
        assert list(rows[model]) == ROW_COLUMNS[model], model
    return rows


ROW_POOL: list[Any] = [
    None,
    0,
    1,
    2,
    -1,
    2**63 - 1,
    -(2**63),
    0.0,
    1.0,
    1.5,
    float("inf"),
    "",
    " ",
    "x",
    "\x00",
    ULID,
    ULID.lower(),
    "sess_1",
    "high",
    "inferred",
    "fact",
    "Fact",
    "true",
    "0",
    " 7 ",
    "\x1c8",
    "\x858\u2028",
    "\u0663",
    "1e3",
    SHA40,
    " " + SHA64.upper() + " ",
    "2026-01-02T03:04:05Z",
    "2026-01-02",
    "2026-01-02 03:04:05.5+01:00:30",
    "2026-01-02T03:04:05,123",
    "20260102T030405",
    "2026-13-01",
    "1700000000",
    "[]",
    "[ ]",
    '["projects:foo"]',
    '["Bad"]',
    '["a", 1]',
    "{}",
    "null",
    "1",
    '"x"',
    "[",
    "NaN",
    '{"cwd": "/w/y", "repo": "r"}',
    '{"cwd": "\\ud800"}',
    '{"client": "c", "sources": {}}',
    '{"sources": {"a": 1}}',
    '{"x": 1}',
    '[{"type": "extends", "target_id": "' + ULID2 + '"}]',
    '[{"type": "extends", "target_id": "' + ULID + '"}]',
    '[{"type": "nope"}]',
    "\ufeff[]",
    "1" * 4301,
    b"",
    b"x",
    b"[]",
    b"\xff",
    b"\xef\xbb\xbf[]",
    b"\xff\xfe[\x00]\x00",
    b"[\x00\x00\x00]\x00\x00\x00",
    b"\x00[",
    b"\xed\xa0\x80",
    b"7",
    _attest("memory", {"id": ULID, "verified_path_targets": {"a.py": "a.py"}}),
    _attest("memory", {"id": ULID, "verified_path_targets": {"z.py": None}}),
    _attest("memory", {"id": ULID, "verified_absent_path_targets": {"q": 1}}),
    _attest("tombstone", {"id": ULID, "verified_absent_path_targets": {}}),
    _attest("memory", {"id": ULID2, "verified_path_targets": {"a.py": None}}),
    _attest("episode", {"id": ULID}, native='{"cwd": "/w/\\udcff"}'),
    _attest("memory", {"id": ULID}, native='{"cwd": 3}'),
    _attest("memory", {"id": ULID}, native="[1"),
    _attest("tombstone", {"id": ULID}, native='{"branch": "b"}'),
    _attest("episode", {"id": ULID2}, native='{"branch": "b"}'),
    '["memory"]',
    "memory",
    '{"memory": 5}',
    '{"memory": {"id": "' + ULID + '"}, "native_origin": 5}',
]


class _Codec:
    """Outcomes written compactly (see the module docstring)."""

    def __init__(self, title: str | None) -> None:
        self.title = title
        self.types: list[str] = []

    def type_code(self, name: str) -> int:
        if name not in self.types:
            self.types.append(name)
        return self.types.index(name)

    def outcome(
        self, model: str, outcome: list[Any], field: str | None, given: Any = None
    ) -> Any:
        """``given`` is the value the edit set (a coercion is written when
        the dumped field spells otherwise; None for a deletion)."""
        if outcome[0] == "ok":
            head = digest(outcome, 8)
            dumped = _dumped(outcome)
            if field is not None and dumped is not None and field in dumped:
                if _spelled(dumped[field]) != _spelled(given):
                    return [head, _file_value(dumped[field])]
            return head
        _, cls, title, errors = outcome
        if cls != "ValidationError":
            return "!" + cls
        items = [
            self.type_code(t)
            if field is not None and loc == [field]
            else [loc, self.type_code(t)]
            for loc, t in errors
        ]
        if title == _TITLES[model]:
            return {"e": items}
        return {"t": title, "e": items}


_TITLES = {
    "memory": "Memory",
    "tombstone": "TombstonedMemory",
    "episode": "Episode",
    "origin": "Origin",
    "actor": "Actor",
    "link": "MemoryLink",
}


def _spelled(value: Any) -> str:
    """A value's canonical text, which a coercion changes."""
    return json.dumps(
        tag(value), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def _file_value(value: Any) -> Any:
    """A dumped value as the file writes it: `tag`, with text holding a
    backslash as code points too (see `_row_value`)."""
    if isinstance(value, str) and "\\" in value:
        return {"$cps": [ord(c) for c in value]}
    if isinstance(value, list):
        return [_file_value(v) for v in value]
    if isinstance(value, dict) and not any(_cps(k) for k in value):
        return {k: _file_value(v) for k, v in value.items()}
    return tag(value)


def _row_value(value: Any) -> Any:
    """A row pool value as the file writes it: text holding a backslash as
    code points too, so the file never spells a run of escaped backslashes
    (which the host-fact guard reads as a UNC path)."""
    if isinstance(value, str) and "\\" in value:
        return {"$cps": [ord(c) for c in value]}
    return tag(value)


def _row_cases(model: str, base: dict[str, Any], codec: _Codec) -> list[list[Any]]:
    """Every column at every row pool value, then the hand-picked rows."""
    out: list[list[Any]] = []
    for column in ROW_COLUMNS[model]:
        outcomes = []
        for value in ROW_POOL:
            outcome = row_outcome(model, _apply(base, [[column, value]]))
            outcomes.append(codec.outcome(model, outcome, column, value))
        out.append([model, column, outcomes])
    hand: list[list[list[Any]]] = [
        [["attestation_payload"]],
        [["scopes_json", Deep(100_000)]],
        [["origin_json", Deep(100_000)]],
        [["attestation_payload", Deep(100_000)]],
        [["created", "2026-01-02T03:04:05"], ["updated", "2026-01-02T03:04:05-00:00"]],
    ]
    for ops in hand:
        outcome = row_outcome(model, _apply(base, ops))
        out.append([model, _written(_lit(ops)), codec.outcome(model, outcome, None)])
    return out


def _dumped(outcome: list[Any]) -> dict[str, Any] | None:
    if outcome[0] != "ok" or outcome[1] is None:
        return None
    value = json.loads(outcome[1])
    return value if isinstance(value, dict) else None


#: The tombstone's fields shared with the memory run validators written
#: twice in `models` (the same code, copied): the battery covers them on
#: the tombstone, the explicit grid only its own fields and a few shared.
EXPLICIT = {
    "tombstone": [
        "id",
        "scopes",
        "verified_head",
        "verified_path_targets",
        "claims",
        "removed",
        "removed_reason",
        "removed_session",
        "links",
        "corroborations",
        EXTRA,
    ]
}


def _payload_cases(model: str, codec: _Codec) -> list[list[Any]]:
    """Every field at every pool value and deleted, the whole input at
    every pool value, then the hand-picked inputs."""
    base = BASES[model]
    out: list[list[Any]] = []
    for field in [*EXPLICIT.get(model, FIELDS[model]), "$root"]:
        outcomes = []
        for value in POOL:
            data = _apply(base, [[field, value]])
            outcomes.append(
                codec.outcome(model, payload_outcome(model, data), field, value)
            )
        if field != "$root":
            data = _apply(base, [[field]])
            outcomes.append(codec.outcome(model, payload_outcome(model, data), field))
        out.append([model, field, outcomes])
    for ops in _HAND.get(model, []):
        outcome = payload_outcome(model, _apply(base, ops))
        out.append([model, _written(_lit(ops)), codec.outcome(model, outcome, None)])
    return out


#: Inputs no single edit of a base reaches: the minimal records, the
#: cross-field rules and the canonical spellings.
_HAND: dict[str, list[list[list[Any]]]] = {
    "memory": [
        [
            [
                "$root",
                {
                    k: MEMORY[k]
                    for k in (
                        "id",
                        "created",
                        "updated",
                        "scopes",
                        "confidence",
                        "source",
                        "body",
                    )
                },
            ]
        ],
        [["links", [{"type": "extends", "target_id": ULID}]], ["id", "bad"]],
        [["links", [LINK] * 2], ["id", ULID2]],
        [["verified_path_targets", None], ["verified_absent_path_targets", None]],
        [
            ["verified_absent_paths", ["z"]],
            ["verified_absent_path_targets", {"z": None}],
        ],
        [["verified_absent_paths", ["z"]], ["verified_absent_path_targets", {}]],
        [["verified_paths", []], ["verified_path_targets", {}]],
        [["verified_paths", ["a.py", "a.py"]]],
        [["created", "2026-01-02T03:04:05"], ["updated", "1.5"]],
        [
            ["created", "2026-01-02T03:04:05.000001-00:00"],
            ["updated", "2026-01-02T03:04:05,9+23:59"],
        ],
        [["scopes", ["x"] * 64 + ["Bad"]]],
        [["scopes", ["Bad", 1]]],
        [["corroborations", "-1"], ["id", 1], ["created", None]],
        [["origin", {"cwd": "\ud800", "repo": "r"}]],
        [["origin", {}]],
        [["actor", {}]],
        [["actor", {"sources": {}}]],
        [["actor", {"sources": {"a": "b"}}]],
        [["body", "\U0001f600\udfff\x00"], ["scopes", ["global"]]],
        [["verified_head", "  " + SHA64.upper()]],
        [
            ["verified_paths", ["a\udfff"]],
            ["verified_path_targets", {"a\udfff": None, "a\ufffd\ufffd\ufffd": "x"}],
        ],
        [
            [
                "actor",
                {"sources": {"a\udfff": "1", "b": "b", "a\ufffd\ufffd\ufffd": "2"}},
            ]
        ],
        [["verified_head", "\u212a" * 40]],
    ],
    "tombstone": [
        [
            [
                "$root",
                {
                    k: TOMBSTONE[k]
                    for k in (
                        "id",
                        "created",
                        "updated",
                        "scopes",
                        "confidence",
                        "source",
                        "body",
                        "removed",
                        "removed_reason",
                    )
                },
            ]
        ],
        [["links", "ignored"], ["corroborations", -5]],
        [["removed", None], ["removed_reason", None], ["removed_session", 3]],
    ],
    "episode": [
        [["$root", {k: EPISODE[k] for k in ("id", "session_id", "created", "body")}]],
        [["scopes", []], ["takeaway", None], ["origin", None], ["swarm_id", None]],
        [["is_floor", "on"], ["swarm_id", "a" * 128]],
        [["scopes", ["x"] * 65]],
        [["swarm_id", "\u00e9"]],
    ],
    "link": [
        [["$root", {"type": "contradicts", "target_id": ULID}]],
        [["note", None]],
    ],
    "origin": [[["$root", {}]], [["$root", {"cwd": None, "repo": None}]]],
    "actor": [[["$root", {}]], [["$root", {"sources": {"x": "y"}}]]],
}


def _showcase(model: str) -> list[list[Any]]:
    """The base and a minimal record, outcome in full."""
    out = []
    ops_list: list[list[list[Any]]] = [[], *(_HAND.get(model, [])[:1])]
    for ops in ops_list:
        data = _apply(BASES[model], ops)
        out.append(
            [model, _written(_lit(ops)), ["full", tag(payload_outcome(model, data))]]
        )
    return out


def _battery(
    model: str, pool: list[Any], fields: list[str], base: Any, row: bool
) -> dict[str, Any]:
    seed = SEED + sorted(BASES).index(model) + (100 if row else 0)
    digests = []
    for ops in battery_ops(fields, len(pool), BATTERY, seed):
        data = canon(base)
        for f, v in ops:
            name = fields[f]
            if v == len(pool):
                if row and name != "attestation_payload":
                    data[name] = None
                else:
                    data.pop(name, None)
            else:
                data[name] = pool[v]
        outcome = row_outcome(model, data) if row else payload_outcome(model, data)
        digests.append(digest(outcome, 6))
    return {"battery": model, "n": BATTERY, "seed": seed, "digests": "".join(digests)}


def generate() -> dict[str, bytes]:
    """``models.jsonl``: the payload form (not gated)."""
    codec = _Codec(None)
    lines: list[Any] = []
    for model in BASES:
        lines.extend(_showcase(model))
        lines.extend(_payload_cases(model, codec))
    for model in BASES:
        lines.append(_battery(model, POOL, FIELDS[model], BASES[model], row=False))
    header = {
        "about": "records as the reference's models validate and write them (payload form)",
        "bases": {m: tag(BASES[m]) for m in BASES},
        "fields": FIELDS,
        "pool": tag(POOL),
        "lcg": [MULT, INC],
        "record_columns": list(_RECORD_COLUMNS),
        "titles": _TITLES,
        "types": codec.types,
    }
    return {"models.jsonl": jsonl([header, *lines])}


def rows() -> dict[str, bytes]:
    """``models_rows.jsonl``: the row form (gated on the Python release)."""
    bases = _base_rows()
    codec = _Codec(None)
    lines: list[Any] = []
    for model in bases:
        lines.append([model, [], ["full", tag(row_outcome(model, bases[model]))]])
        lines.extend(_row_cases(model, bases[model], codec))
    for model in bases:
        lines.append(
            _battery(model, ROW_POOL, ROW_COLUMNS[model], bases[model], row=True)
        )
    header = {
        "about": "records as store_rows reads them from rows (row form)",
        "bases": {m: tag(bases[m]) for m in bases},
        "fields": ROW_COLUMNS,
        "pool": [_row_value(v) for v in ROW_POOL],
        "lcg": [MULT, INC],
        "titles": _TITLES,
        "types": codec.types,
    }
    return {"models_rows.jsonl": jsonl([header, *lines])}


#: The selects the reference reads records with (`store_records`,
#: `store_journal`), in rowid order.
STORE_SELECTS = {
    "memories": ("memory", "_MEMORY_SELECT", "memories"),
    "tombstones": ("tombstone", "_TOMBSTONE_SELECT", "tombstones"),
    "episodes": ("episode", "_EPISODE_SELECT", "episodes"),
}


def _store_records(store_file: Any) -> dict[str, Any]:
    """Every record row of a store file through the reference's own select
    and reader: each row's outcome digest (16 hex digits), or ``"!sql"``
    for a table SQLite will not read."""
    import sqlite3

    from bettermemory import store_journal, store_records

    out: dict[str, Any] = {}
    # A file URI, so a Windows drive path reads as one.
    uri = Path(store_file).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for table, (model, name, alias) in STORE_SELECTS.items():
            module = store_journal if table == "episodes" else store_records
            select = getattr(module, name) + f" ORDER BY {alias}.rowid"
            try:
                fetched = conn.execute(select).fetchall()
            except sqlite3.Error:
                out[table] = "!sql"
                continue
            out[table] = [
                [
                    row["id"] if isinstance(row["id"], str) else None,
                    digest(_read(model, row), 16),
                ]
                for row in fetched
            ]
    finally:
        conn.close()
    return out


def _read(model: str, row: Any) -> list[Any]:
    from bettermemory import store_rows as sr

    reader = {
        "memory": sr._row_to_memory,
        "tombstone": sr._row_to_tombstone,
        "episode": sr._row_to_episode,
    }[model]
    try:
        record = reader(row)
    except Exception as exc:  # noqa: BLE001 - recorded as data
        return _error_outcome(exc)
    return _record_outcome(model, record)


def stores() -> dict[str, bytes]:
    """``models_stores.jsonl``: every record row of the store fixtures
    (each ``store_files.jsonl`` case rebuilt from ``store_bases.json``, and
    ``store_real.sqlite``) read as the reference reads it, by outcome
    digest. Gated like the store files it rebuilds and on the Python
    release (row datetimes)."""
    import shutil
    import tempfile
    from pathlib import Path

    from . import gen_store_files as sf
    from ._sandbox import world

    bases = sf.load_bases()
    lines: list[Any] = [
        {
            "about": "every record row of the store fixtures, as the reference reads it",
            "digest": "sha256 of the outcome as models.jsonl spells it, first 16 hex digits",
            "selects": {t: list(v) for t, v in STORE_SELECTS.items()},
        }
    ]
    with tempfile.TemporaryDirectory() as tmp, world():
        root = Path(tmp).resolve()
        for name, (_what, base, recipe) in sf.cases(bases).items():
            case = root / name
            sf._materialize(bases, base, recipe, case)
            lines.append({"case": name, **_store_records(case / "memory.sqlite")})
        real = root / "real" / "memory.sqlite"
        real.parent.mkdir()
        shutil.copyfile(sf._real_path(), real)
        lines.append({"case": "store_real.sqlite", **_store_records(real)})
    return {"models_stores.jsonl": jsonl(lines)}


__all__ = ["generate", "payload_outcome", "row_outcome", "rows", "stores"]
