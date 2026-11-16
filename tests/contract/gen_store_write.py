"""Fixture: one record mutation at a time on a store open for writing
(``store_write.jsonl``).

The native build writes record mutations too (``native/``,
`store::write`, `WritableStore::mutate`) and is held to this file. Each
sequence starts from the store `Store.create` makes under
``store_create.json``'s fixed values (rebuilt natively from that recipe),
edits the key ring as its recipe says, opens it as the owner
(``Store.open(..., allow_rekey=True)``, as ``store_open.jsonl`` does),
and runs its steps: a mutation (``store._mutate`` inside
``store._transaction``, the lowest call that does only that, its row's
and head's clock fixed), or an edit between two mutations (the key file
replaced or removed, the binding moved, a pending rotation, meta's key
edited, the journal mode). After each mutation it records the outcome
(the row's columns, or the refusal's class and text) and the state
(`gen_store_open.state`: every table hashed, meta and the digest's state
row by row, the key ring's texts; with the rows the step appended in
full and the FTS5 index as queries see it: for every word of every
memory's index text, the rowids ``MATCH`` gives).

Each step names what the native unit does: ``same`` (writes what the
reference writes, or refuses as it refuses), or ``not_ported:<class>``
(refuses with nothing written; the reference's state after it is not
recorded, since the native store must stay as it was).

Gates. Unicode: the index text is the tokenizer's spelling and the
digest's rows its tokens. pydantic-core: a refused payload's text is the
first line of a ValidationError (``N validation error(s) for Memory``),
and the models decide what a payload holds. POSIX only: the state
includes modes and the binding's resolved path, as ``store_open``'s.
Not on the Python version: every text compared is the product's own
wording or pydantic's first line, and no case reaches a standard-library
message (the KeyError text is ``repr`` of an ASCII name). Not on the
SQLite version: FTS5's shadow tables are not compared, only what a
``MATCH`` returns, which the index's content decides.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

from bettermemory import log as blog
from bettermemory._fsutil import sqlite_file_uri
from bettermemory.models import Memory, TombstonedMemory
from bettermemory.store import Store

from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS, _engine, _made, state

#: The first row's clock; each step's row and head stamps follow it.
T0 = datetime(2001, 2, 3, 5, 0, 0, 111111, tzinfo=timezone.utc)

#: Another key, 32 bytes, for the edits that replace the key file.
OTHER_KEY = "ee" * 32
OTHER_FINGERPRINT = "4d12332c7f14cdaafa2b617b50feec749250000a8d75662c72c8931374176fca"


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def ulid(n: int, prefix: str = "01J") -> str:
    digits = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    tail = ""
    for _ in range(26 - len(prefix)):
        tail = digits[n % 32] + tail
        n //= 32
    return prefix + tail


def memory(
    n: int, *, body: str | None = None, prefix: str = "01J", **fields: Any
) -> dict[str, Any]:
    """A memory payload as `put_memory` builds it."""
    record: dict[str, Any] = {
        "id": ulid(n, prefix),
        "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z",
        "scopes": ["global"],
        "confidence": "high",
        "source": "explicit-statement",
        "body": body if body is not None else f"record number {n} about sqlite",
    }
    record.update(fields)
    m = Memory.model_validate(record)
    from bettermemory.models import record_payload

    return {
        "memory": record_payload(m),
        "provenance": "local",
        "filename": None,
    }


def tombstone(n: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": ulid(n),
        "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z",
        "scopes": ["global"],
        "confidence": "high",
        "source": "explicit-statement",
        "body": f"removed record {n}",
        "removed": "2001-02-04T00:00:00.000000Z",
        "removed_reason": "outdated",
        "removed_session": "s-remove",
    }
    record.update(fields)
    from bettermemory.models import record_payload

    return {
        "tombstone": record_payload(TombstonedMemory.model_validate(record)),
        "provenance": "local",
        "filename": None,
        "links": [],
        "corroborations": 0,
        "last_corroborated": None,
    }


def full(n: int) -> dict[str, Any]:
    """A memory with every optional field."""
    return memory(
        n,
        scopes=["global", "project:alpha", "topic:x-y"],
        confidence="medium",
        source="user-correction",
        origin={
            "cwd": "/work/a",
            "repo": "git@example.invalid:a.git",
            "branch": "main",
            "worktree_root": "/work/a",
            "source": "cli",
        },
        actor={
            "client": "fixture",
            "client_version": "1.0",
            "model": "m",
            "principal": "p",
            "session": "s",
            "sources": {"client": "flag"},
        },
        last_verified_at="2001-02-03T04:05:07.000000Z",
        category="fact",
        verified_paths=["src/a.py"],
        verified_commits=["a" * 40],
        verified_versions=["pkg==1.0"],
        verified_absent_paths=["src/gone.py"],
        verified_path_targets={"src/a.py": "src/a.py"},
        claims=["claim one", "claim two"],
        verified_head="b" * 40,
        links=[
            {"type": "extends", "target_id": ulid(31), "note": "see"},
            {"type": "depends_on", "target_id": ulid(30), "note": None},
            {"type": "extends", "target_id": ulid(31), "note": "see"},
        ],
        corroborations=3,
        last_corroborated="2001-02-03T04:05:08.000000Z",
    )


def put(
    payload: dict[str, Any], session: str | None = "s1", **kw: Any
) -> dict[str, Any]:
    return {"kind": "memory_put", "payload": payload, "session": session, **kw}


def op(
    kind: str, payload: Any, session: str | None = "s1", **kw: Any
) -> dict[str, Any]:
    return {"kind": kind, "payload": payload, "session": session, **kw}


def sequences() -> list[dict[str, Any]]:
    """Each: name, recipe (key ring edits before the open), steps."""
    unicode_body = "Zürich 東京 ½ ǅ ﬁ ́e \U0001f600 ​ tab\tnl\nnul\x00end"
    long_body = ("lorem ipsum dolor sit amet " * 800).strip()
    no_journal = {"edit": "journal", "mode": "DELETE"}
    seqs: list[dict[str, Any]] = [
        {"name": "one_put", "steps": [put(memory(1))]},
        {
            "name": "many_puts",
            "steps": [put(memory(n)) for n in range(1, 5)] + [put(full(5))],
        },
        {
            "name": "reput",
            "steps": [
                put(memory(1)),
                put(memory(2)),
                put(full(3)),
                put(
                    memory(
                        3,
                        body="a changed body",
                        scopes=["global", "project:beta"],
                        links=[{"type": "contradicts", "target_id": ulid(2)}],
                    )
                ),
                put(
                    memory(3, body="a changed body", scopes=["global", "project:beta"])
                ),
                put(
                    memory(3, body="a changed body", scopes=["global", "project:beta"])
                ),
            ],
        },
        {
            "name": "delete",
            "steps": [
                put(memory(1)),
                put(memory(2)),
                op("memory_delete", {"id": ulid(1)}),
                op("memory_delete", {"id": ulid(1)}),
                op("memory_delete", {"id": ulid(9)}),
            ],
        },
        {
            "name": "tombstones",
            "steps": [
                put(memory(1)),
                op("memory_delete", {"id": ulid(1)}),
                op("tombstone_put", tombstone(1)),
                op("tombstone_put", tombstone(1, removed_reason="duplicate")),
                op("tombstone_delete", {"id": ulid(1)}),
                put(memory(1, body="back again")),
            ],
        },
        {
            "name": "unicode",
            "steps": [
                put(
                    memory(
                        1,
                        body=unicode_body,
                        scopes=["global", "projekt:oe"],
                        claims=["ñ \x00 x"],
                        origin={"cwd": "/work/ü\x00", "branch": "ß"},
                    ),
                    session="séance-\U0001f600",
                ),
                put(memory(2, body=long_body)),
            ],
        },
        {
            "name": "sessions_and_clock",
            "steps": [
                put(memory(1), session="a"),
                put(memory(2), session=None),
                put(memory(3), session="b", back=3600),
                put(memory(4), session="a", back=86400 * 400),
            ],
        },
        {
            "name": "id_order",
            "steps": [
                put(memory(5)),
                put(memory(1, prefix="01A")),
                put(memory(1, prefix="01Z")),
                put(memory(6)),
            ],
        },
        {
            "name": "rollback_journal",
            "steps": [
                no_journal,
                put(memory(1)),
                put(full(2)),
                op("memory_delete", {"id": ulid(1)}),
                op("tombstone_put", tombstone(1)),
            ],
        },
        {
            "name": "other_kinds",
            "steps": [
                op(
                    "episode_put",
                    {
                        "episode": {
                            "id": ulid(40),
                            "session_id": "s1",
                            "created": "2001-02-03T04:05:06.000000Z",
                            "body": "an episode",
                            "scopes": ["global"],
                            "takeaway": "t",
                            "origin": None,
                            "is_floor": False,
                            "swarm_id": None,
                        }
                    },
                ),
                op("episode_delete", {"id": ulid(40)}),
                op(
                    "verification_put",
                    {
                        "memory_id": ulid(1),
                        "machine_id": "m1",
                        "verified_at": "2001-02-03T04:05:06.000000Z",
                        "verified_head": None,
                        "verified_paths": ["a"],
                    },
                ),
                op("verification_delete", {"memory_id": ulid(1), "machine_id": "m1"}),
                op(
                    "import_put",
                    {
                        "source": "src",
                        "content_hash": "h",
                        "imported_at": "2001-02-03T04:05:06.000000Z",
                        "memory_id": None,
                    },
                ),
                op("import_delete", {"source": "src"}),
                op(
                    "quarantine_put",
                    {
                        "name": "q",
                        "reason": "r",
                        "at": "2001-02-03T04:05:06.000000Z",
                        "size": 3,
                    },
                ),
                op("quarantine_delete", {"name": "q"}),
            ],
        },
        # The head checkpoint as the open finds it: missing, behind, ahead.
        {
            "name": "head_missing",
            "recipe": [{"unlink": ".head"}],
            "steps": [put(memory(1))],
        },
        {
            "name": "head_ahead",
            "recipe": [{"head": 99}],
            "steps": [put(memory(1)), put(memory(2))],
        },
        {
            "name": "head_junk",
            "recipe": [{"head_text": "not json"}],
            "steps": [put(memory(1))],
        },
        # The key ring edited after the open: the reference signs with
        # the key it opened with while meta names it.
        {
            "name": "key_edits",
            "steps": [
                put(memory(1)),
                {"edit": "write", "file": ".key", "hex": OTHER_KEY},
                put(memory(2)),
                {"edit": "unlink", "file": ".key"},
                put(memory(3)),
                {"edit": "write", "file": ".key.pending", "hex": OTHER_KEY},
                put(memory(4)),
                {"edit": "bind_elsewhere"},
                put(memory(5)),
            ],
        },
        {
            "name": "meta_rekeyed",
            "steps": [
                put(memory(1)),
                {"edit": "meta_key", "fingerprint": "f" * 64},
                put(memory(2), native="same"),
            ],
        },
        {
            "name": "meta_rekeyed_pending",
            "steps": [
                {"edit": "meta_key", "fingerprint": "f" * 64},
                {"edit": "write", "file": ".key.pending", "hex": OTHER_KEY},
                put(memory(1)),
            ],
        },
        # `int()` of text that is no integer: CPython quotes the value's
        # repr cut to 200 code points (`%.200R`), escapes counted as
        # written.
        {
            "name": "int_text",
            "steps": [
                op("tombstone_put", {**tombstone(1), "corroborations": text})
                for text in [
                    "w" * 197,
                    "w" * 198,
                    "w" * 199,
                    "w" * 200,
                    "w" * 300,
                    "\u00e9\x00\n'" * 80,
                    "\U0001f600" * 250,
                ]
            ],
        },
        # A rotation that committed and did not rename: meta names the
        # pending key, which `finish_rotation` renames over the current one.
        {
            "name": "meta_rotation_to_finish",
            "steps": [
                put(memory(1)),
                {"edit": "meta_key", "fingerprint": OTHER_FINGERPRINT},
                {"edit": "write", "file": ".key.pending", "hex": OTHER_KEY},
                put(memory(2), native="not_ported:rekeyed"),
            ],
        },
        # A rekey in this ring: meta and the current key file name the
        # other key; both builds sign with it.
        {
            "name": "meta_rekeyed_in_ring",
            "steps": [
                put(memory(1)),
                {"edit": "write", "file": ".key", "hex": OTHER_KEY},
                {"edit": "meta_key", "fingerprint": OTHER_FINGERPRINT},
                put(memory(2)),
                put(memory(3)),
            ],
        },
        # meta's fingerprint stored as an INTEGER: `str(row[0])`.
        {
            "name": "meta_key_integer",
            "steps": [
                {"edit": "meta_key", "fingerprint": 12345},
                put(memory(1)),
            ],
        },
        # (A datetime bound as a column value goes through sqlite3's
        # default adapter, deprecated since 3.12: a DeprecationWarning, an
        # error under the test suite's filters; not a case here.)
        # Values JSON does not have, as a caller's mapping holds them
        # (``{"$tuple": [...]}``, ``{"$datetime": iso}``, ``{"$path": text}``
        # in this file): `_mutate` applies the mapping as given, the row
        # holds its canonical text (`default=str`).
        {
            "name": "python_values",
            "steps": [
                put(memory(1)),
                op(
                    "tombstone_put",
                    {
                        **tombstone(1),
                        "links": {
                            "$tuple": [
                                {"type": "extends", "target_id": ulid(2), "note": None}
                            ]
                        },
                    },
                ),
                op("tombstone_put", {**tombstone(1), "links": {"$tuple": []}}),
                put(
                    {
                        **memory(2),
                        "memory": {
                            **memory(2)["memory"],
                            "created": {
                                "$datetime": "2001-02-03T04:05:06.250000+00:00"
                            },
                            "scopes": {"$tuple": ["global", "project:x"]},
                        },
                    }
                ),
                op(
                    "verification_put",
                    {
                        "memory_id": ulid(1),
                        "machine_id": None,
                        "verified_at": {"$datetime": "2001-02-03T04:05:06+00:00"},
                        "verified_paths": {"$tuple": ["a", "b"]},
                    },
                ),
                op(
                    "quarantine_put",
                    {
                        "name": {"$path": "q/one"},
                        "reason": "r",
                        "at": "2001-02-03T04:05:06.000000Z",
                    },
                ),
                put({**memory(3), "filename": {"$path": "notes/a.md"}}),
                put(
                    {
                        **memory(4),
                        "memory": {
                            **memory(4)["memory"],
                            "origin": {"cwd": {"$path": "/work/a"}},
                        },
                    }
                ),
                put(
                    {
                        **memory(5),
                        "memory": {
                            **memory(5)["memory"],
                            "origin": {"cwd": {"$path": "/work/\udcff"}},
                        },
                    }
                ),
            ],
        },
        # Refusals: the reference's own, and what native does not port.
        {
            "name": "refused",
            "steps": [
                put(memory(1)),
                op("memory_put", {"memory": {"id": "x"}, "provenance": "local"}),
                op("memory_put", {"provenance": "local"}),
                op("memory_put", {**memory(2), "provenance": "elsewhere"}),
                op("memory_delete", {}),
                op("tombstone_put", {"tombstone": {}, "provenance": "local"}),
                op("tombstone_delete", {"ids": []}),
                op("episode_put", {"episode": {"id": 1}}),
                op("verification_put", {"memory_id": ulid(1)}),
                op("quarantine_put", {"name": "q"}),
                op("home_put", {"home": {}}, native="not_ported:kind"),
                op("returned_put", {"returned": {}}, native="not_ported:kind"),
                op("standing_put", {"standing": {}}, native="not_ported:kind"),
                op("telemetry", {}, native="not_ported:kind"),
                put(memory(4)),
            ],
        },
        {
            "name": "native_origin",
            "steps": [
                put(memory(1)),
                put(
                    memory(3, origin={"cwd": "/work/\udcff"}),
                    native="not_ported:native_origin",
                ),
            ],
        },
        {
            "name": "returned_on_delete",
            "steps": [
                put(memory(1)),
                put(memory(2, links=[{"type": "supersedes", "target_id": ulid(1)}])),
                put(memory(3, links=[{"type": "supersedes", "target_id": ulid(9)}])),
                op("memory_delete", {"id": ulid(3)}),
                op("memory_delete", {"id": ulid(2)}, native="not_ported:returned"),
            ],
        },
        {
            "name": "returned_on_reput",
            "steps": [
                put(memory(1)),
                put(memory(2, links=[{"type": "supersedes", "target_id": ulid(1)}])),
                put(
                    memory(
                        2,
                        body="kept the link",
                        links=[{"type": "supersedes", "target_id": ulid(1)}],
                    )
                ),
                put(memory(2, links=[]), native="not_ported:returned"),
            ],
        },
        {
            "name": "unknown_fields",
            "steps": [
                put(memory(1)),
                {"edit": "unknown_field", "id": ulid(1)},
                put(memory(1, body="edited")),
                op("memory_delete", {"id": ulid(1)}),
            ],
        },
    ]
    return seqs


# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fts_view(store: Path) -> dict[str, Any]:
    """The FTS5 index as queries see it: for every word (split on spaces)
    of every memory's ``body_fts`` and ``scopes_fts``, the rowids a
    phrase ``MATCH`` of it returns, or the error's class."""
    conn = sqlite3.connect(sqlite_file_uri(store) + "?mode=ro", uri=True)
    try:
        words: set[str] = set()
        for body, scopes in conn.execute("SELECT body_fts, scopes_fts FROM memories"):
            words.update(w for w in f"{body} {scopes}".split(" ") if w)
        out: list[Any] = []
        for word in sorted(words):
            query = '"' + word.replace('"', '""') + '"'
            try:
                hits: Any = [
                    r[0]
                    for r in conn.execute(
                        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? "
                        "ORDER BY rowid",
                        (query,),
                    )
                ]
            except sqlite3.Error as exc:
                hits = type(exc).__name__
            out.append([word, hits])
    finally:
        conn.close()
    text = json.dumps(out, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {"words": len(out), "sha256": _sha(text)}


#: A payload longer than this many UTF-8 bytes is recorded as its length
#: and sha256 (the long-body case), to keep the file small.
LONG_PAYLOAD = 2048


def _payload_text(payload: str) -> Any:
    data = payload.encode("utf-8")
    if len(data) > LONG_PAYLOAD:
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return payload


def state_digest(full_state: dict[str, Any]) -> dict[str, Any]:
    """What a step records of the state: the sha256 of its canonical JSON
    (sorted keys, no whitespace, ASCII), with the tables' hash, the log's
    and the key ring's head text beside it to say what differs."""
    text = json.dumps(
        full_state, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    head = [
        k.get("text") for k in full_state.get("keys", []) if k["name"] == "<id>.head"
    ]
    return {
        "sha256": _sha(text),
        "tables": full_state.get("tables"),
        "log": full_state.get("log"),
        "head": head[0] if head else None,
    }


def log_rows(store: Path, after: int) -> list[list[Any]]:
    conn = sqlite3.connect(sqlite_file_uri(store) + "?mode=ro", uri=True)
    try:
        return [
            [seq, ts, session, kind, _payload_text(payload), prev, mac]
            for seq, ts, session, kind, payload, prev, mac in conn.execute(
                "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
                "WHERE seq > ? ORDER BY seq",
                (after,),
            )
        ]
    finally:
        conn.close()


def _last_seq(store: Path) -> int:
    conn = sqlite3.connect(sqlite_file_uri(store) + "?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT max(seq) FROM log").fetchone()[0])
    finally:
        conn.close()


@contextmanager
def _clock(stamps: list[str]) -> Iterator[None]:
    def utcnow_iso() -> str:
        if not stamps:
            raise AssertionError("the clock was read more often than fixed")
        return stamps.pop(0)

    with patch.object(blog, "_utcnow_iso", utcnow_iso):
        yield


def _recipe(recipe: list[dict[str, Any]], keys: Path, store: Path) -> None:
    from bettermemory.store import Store as _S  # noqa: F401

    for item in recipe:
        if "unlink" in item:
            (keys / f"{STORE_ID}{item['unlink']}").unlink()
        elif "head" in item:
            conn = sqlite3.connect(store)
            mac = conn.execute("SELECT mac FROM log ORDER BY seq DESC").fetchone()[0]
            conn.close()
            (keys / f"{STORE_ID}.head").write_text(
                json.dumps(
                    {"seq": item["head"], "mac": mac, "ts": "2001-01-01T00:00:00Z"}
                )
            )
        elif "head_text" in item:
            (keys / f"{STORE_ID}.head").write_text(item["head_text"])


def decode(value: Any) -> Any:
    """A payload as the fixture spells it, its tagged values made the
    Python values they stand for. ``{"$dict": [[k, v], ...]}`` is a dict
    in that order: the fixture's ``sort_keys`` would lose an object's.
    ``{"$int": digits}`` and ``{"$float": repr}`` are numbers the native
    reader takes exactly."""
    if isinstance(value, dict):
        if len(value) == 1:
            ((tag, inner),) = value.items()
            if tag == "$tuple":
                return tuple(decode(v) for v in inner)
            if tag == "$datetime":
                return datetime.fromisoformat(inner)
            if tag == "$path":
                return PurePosixPath(inner)
            if tag == "$int":
                return int(inner)
            if tag == "$float":
                return float(inner)
            if tag == "$dict":
                return {decode(k): decode(v) for k, v in inner}
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def _edit(edit: dict[str, Any], store: Store, case: Path) -> None:
    keys = case / "keys"
    what = edit["edit"]
    if what == "journal":
        store.conn.execute(f"PRAGMA journal_mode = {edit['mode']}")
    elif what == "write":
        (keys / f"{STORE_ID}{edit['file']}").write_bytes(bytes.fromhex(edit["hex"]))
    elif what == "unlink":
        (keys / f"{STORE_ID}{edit['file']}").unlink()
    elif what == "bind_elsewhere":
        (case / "other").mkdir()
        (keys / f"{STORE_ID}.path").write_text(
            json.dumps({"path": str((case / "other" / "memory.sqlite").resolve())})
        )
    elif what == "meta_key" and not isinstance(edit["fingerprint"], str):
        store.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            (edit["fingerprint"],),
        )
        store.conn.commit()
    elif what == "meta_key":
        store.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            (edit["fingerprint"],),
        )
        store.conn.commit()
    elif what == "returned":
        store.conn.execute(
            "INSERT INTO returned(record_id, via, returned) VALUES (?, ?, ?)",
            (edit["id"], edit["id"], "2001-02-03T04:05:06.000000Z"),
        )
        store.conn.commit()
    elif what == "unknown_field":
        # A later build's field on the log row the record points at, as
        # that build would have signed it (the MAC is not this test's).
        store.conn.execute(
            "UPDATE log SET payload = json_set(payload, '$.memory.later_field', 1) "
            "WHERE mac = (SELECT log_mac FROM memories WHERE id = ?)",
            (edit["id"],),
        )
        store.conn.commit()
    else:
        raise AssertionError(what)


def _refusal(exc: BaseException) -> dict[str, Any]:
    text = str(exc)
    if isinstance(exc, sqlite3.Error):
        # sqlite3's wording is the interpreter's; the class is compared.
        return {"class": type(exc).__name__, "text": None}
    if type(exc).__name__ == "ValidationError":
        text = text.splitlines()[0]
    return {"class": type(exc).__name__, "text": text}


def run_sequence(seq: dict[str, Any]) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        _recipe(seq.get("recipe", []), case / "keys", file)
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            opened = state(case, root)
            t = T0
            for i, step in enumerate(seq["steps"]):
                if "edit" in step:
                    _edit(step, store, case)
                    steps_out.append({"edit": step})
                    continue
                t = t + timedelta(seconds=1, microseconds=1)
                ts = t - timedelta(seconds=int(step.get("back", 0)))
                head_ts = ts.replace(microsecond=0) + timedelta(seconds=1)
                # The row's stamp, then the head's; a write the native unit
                # refuses for its returned marks reads the clock again.
                stamps = [_iso(ts)] + [_iso(head_ts)] * 3
                payload = decode(json.loads(json.dumps(step["payload"])))
                before = _last_seq(file)
                with _clock(stamps):
                    try:
                        with store._transaction() as tx:
                            store._mutate(
                                tx, step["kind"], payload, session=step["session"]
                            )
                        outcome: dict[str, Any] = {"written": True}
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"written": False, "refusal": _refusal(exc)}
                native = step.get("native", "same")
                record: dict[str, Any] = {
                    "step": i,
                    "kind": step["kind"],
                    "payload": json.dumps(
                        step["payload"], ensure_ascii=True, sort_keys=True
                    ),
                    "session": step["session"],
                    "clock": [_iso(ts), _iso(head_ts)],
                    "native": native,
                    "outcome": outcome,
                }
                if native != "same":
                    record["reference_wrote"] = _last_seq(file) - before
                else:
                    record["rows"] = log_rows(file, before)
                    record["state"] = state_digest(state(case, root))
                    record["fts"] = fts_view(file)
                steps_out.append(record)
            # The fold's replay of the rows the sequence left: what
            # `refold` says of them (the native fold check is held to it).
            folded = store.refold()
            refold = {
                "status": folded["status"],
                "problems": sorted(
                    [p["seq"], p["kind"], p["detail"]] for p in folded["problems"]
                ),
            }
        finally:
            store.close()
    return {
        "refold": refold,
        "sequence": seq["name"],
        "recipe": seq.get("recipe", []),
        "opened": opened,
        "steps": steps_out,
    }


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_write",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
            "memory_fields": list(Memory.model_fields),
            "tombstone_fields": list(TombstonedMemory.model_fields),
        }
    ]
    lines += [run_sequence(s) for s in sequences()]
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_write.jsonl": text.encode("ascii")}
