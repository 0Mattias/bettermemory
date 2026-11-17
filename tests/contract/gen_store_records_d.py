"""Fixture: the record writers ``put_tombstone``, ``restore`` and
``restore_trimmed``, with the relink a restore runs
(``store_records_d.jsonl``).

The native build ports them as functions on the writable store
(``native/``, `store::writers`, part W6r) and is held to this file. A file
of its own, not more cases in ``store_records_c.jsonl``: that file is at
its budget, and each file keeps its own. The cases run as
``store_records_b.jsonl``'s do (`gen_store_records_b.run_case`): each step
records the stamps each clock handed out, the ids minted and the order of
every read (``log``, ``wall``, ``mint``), then the returned record's
canonical payload text (null for a hand edit) or the refusal's class and
text, the rows appended, the state's digest (each row's provenance label
among it), the head's text and the FTS5 index.

A step ``{"call": "sql"}`` is a hand edit run on the store's connection
and committed: a source's pointer into the log broken (its label then
reads ``unaccounted``, so the relink passes it over), or a later build's
field put on the log row a source points at (passed over too).

Gates, the same as ``store_tx.jsonl``'s, chosen by what the cases put in
(record text through the tokenizer, pydantic models, modes and paths),
not by any answer.

Which refusal texts are compared, by one rule (`refusal`): a text that is
the product's own or pydantic's first line is recorded and compared; a
text CPython words is recorded where the native build carries a port of
that wording, else recorded as null and compared by class.

- Compared, the product's: the label, active-id, not-tombstoned and
  not-found texts. Pydantic's first line: ``1 validation error for
  MemoryLink``, ``... for TombstonedMemory``, ``... for Actor``.
- Compared, CPython's with a native port: ``'int' object is not
  iterable``, ``unhashable type: 'list'`` (the writers' own), a
  JSONDecodeError's ``Expecting value: line 1 column 1 (char 0)`` and
  ``Expecting ':' delimiter: ...`` for ``links_json`` (`json`'s port),
  ``invalid literal for int() with base 10: 'x'`` and ``int() argument
  must be a string, a bytes-like object or a real number, not 'list'``
  (`fold`'s port of ``int()``), and sqlite3's OverflowError ``Python int
  too large to convert to SQLite INTEGER`` (`digest`'s).
- Null, compared by class: ``Invalid isoformat string: 'garbage'`` (no
  port), a JSONDecodeError from the other JSON columns of a tombstone row
  (the native row reader carries the class only; such steps are marked
  ``"compare": "class"``), RecursionError and OSError (the interpreter's
  and the machine's wording), and sqlite3's own errors.

The CPython texts are not promised stable across releases (Python 3.15
words ``unhashable type`` differently, believed changed in 3.14); the file
is safe because the Unicode gate (15.1) confines its generation and golden
test to Python 3.13. No value is nested deep enough to reach a recursion
limit. Arguments are spelled with `gen_store_write.decode`'s tags where
JSON would change them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bettermemory.log import canonical_payload
from bettermemory.models import TombstonedMemory, record_payload
from bettermemory.store import Store

from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS
from .gen_store_records_b import put, run_case, tomb
from .gen_store_write import _refusal, decode, memory, ulid

T1 = "2001-02-03T04:05:07.000000Z"
REMOVED = "2001-02-04T00:00:00.000000Z"


def dead(n: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": ulid(n),
        "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z",
        "scopes": ["global"],
        "confidence": "high",
        "source": "explicit-statement",
        "body": f"removed record {n}",
        "removed": REMOVED,
        "removed_reason": "outdated",
        "removed_session": "s-remove",
    }
    record.update(fields)
    return record_payload(TombstonedMemory.model_validate(record))


def link(kind: str, n: int) -> dict[str, str]:
    return {"type": kind, "target_id": ulid(n)}


def pt(n: int, session: str | None = "s6", **kw: Any) -> dict[str, Any]:
    return {"call": "put_tombstone", "dead": dead(n), "kw": kw, "session": session}


def restore(n: int | str, session: str | None = "s7") -> dict[str, Any]:
    return {
        "call": "restore",
        "id": n if isinstance(n, str) else ulid(n),
        "session": session,
    }


def trimmed(n: int | str, session: str | None = "s8", **kw: Any) -> dict[str, Any]:
    return {
        "call": "restore_trimmed",
        "id": n if isinstance(n, str) else ulid(n),
        "kw": kw,
        "session": session,
    }


def linked(n: int, *links: dict[str, str], **f: Any) -> dict[str, Any]:
    """A `put_memory` step of record `n` holding `links`."""
    return {
        "call": "put",
        "memory": memory(n, links=list(links), **f)["memory"],
        "session": "s0",
    }


def sql(text: str | list[str], n: int) -> dict[str, Any]:
    return {"call": "sql", "sql": text, "id": ulid(n)}


def by_class(step: dict[str, Any]) -> dict[str, Any]:
    """A step whose refusal is compared by class: its text is worded by a
    reader that carries only the class natively (see the docstring)."""
    return {**step, "compare": "class"}


def refusal(exc: BaseException, step: dict[str, Any]) -> dict[str, Any]:
    """The refusal by the file's rule (docstring): the text, or null where
    CPython words it and no native port of that wording carries it."""
    out = _refusal(exc)
    if (
        step.get("compare") == "class"
        or isinstance(exc, (RecursionError, OSError))
        or (type(exc) is ValueError and str(exc).startswith("Invalid isoformat string"))
    ):
        out["text"] = None
    return out


def _set(column: str, value: str | None) -> str:
    text = "NULL" if value is None else "'" + value.replace("'", "''") + "'"
    return f"UPDATE tombstones SET {column} = {text} WHERE id = ?"


def corrupt_rows() -> list[dict[str, Any]]:
    """One tombstone, each column a restore reads broken in turn (the one
    before put back in the same edit), then restored."""
    good = {
        "links_json": "[]",
        "scopes_json": '["global"]',
        "verified_paths_json": "[]",
        "verified_commits_json": "[]",
        "verified_versions_json": "[]",
        "verified_absent_paths_json": "[]",
        "claims_json": "[]",
        "actor_json": None,
        "origin_json": None,
        "corroborations": "0",
        "last_corroborated": None,
        "confidence": "high",
    }
    # (column, bad text, the call, compared by class only)
    plan: list[tuple[str, str | None, str, bool]] = [
        ("links_json", "not json", "restore", False),
        ("links_json", '[{"type"', "trimmed", False),
        ("links_json", "5", "restore", False),
        ("links_json", "", "trimmed", False),
        ("links_json", '{"a": 1}', "restore", False),
        ("scopes_json", "not json", "restore", True),
        ("verified_paths_json", '["a"', "trimmed", True),
        ("verified_commits_json", "5", "restore", False),
        ("verified_versions_json", "", "restore", True),
        ("verified_absent_paths_json", "nope", "trimmed", True),
        ("claims_json", "[1", "restore", True),
        ("actor_json", "5", "restore", False),
        ("origin_json", "", "restore", True),
        ("corroborations", "x", "restore", False),
        ("last_corroborated", "garbage", "trimmed", False),
        ("removed", "garbage", "restore", False),
        ("confidence", "bogus", "restore", False),
    ]
    steps: list[dict[str, Any]] = [put(1), tomb(1)]
    before: str | None = None
    for column, bad, call, class_only in plan:
        edits = []
        if before is not None and before != column:
            edits.append(
                "UPDATE tombstones SET removed = created WHERE id = ?"
                if before == "removed"
                else _set(before, good[before])
            )
        edits.append(_set(column, bad))
        steps.append(sql(edits, 1))
        step = restore(1) if call == "restore" else trimmed(1, clear_verification=False)
        steps.append(by_class(step) if class_only else step)
        before = column
    steps += [
        pt(5, corroborations="x"),
        pt(5, corroborations=[1]),
        pt(5, corroborations={"$int": str(2**70)}),
    ]
    return steps


BREAK_POINTER = "UPDATE memories SET log_mac = 'broken' WHERE id = ?"
LATER_FIELD = (
    "UPDATE log SET payload = json_set(payload, '$.memory.later_field', 1) "
    "WHERE mac = (SELECT log_mac FROM memories WHERE id = ?)"
)


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "put_tombstone",
            "steps": [
                pt(1),
                pt(
                    1,
                    provenance="imported",
                    filename="one.md",
                    links=[link("supersedes", 5), link("extends", 6)],
                    corroborations=3,
                    last_corroborated=T1,
                ),
                pt(1, corroborations=2.5, links={"$tuple": []}),
                put(2),
                pt(2),
                pt(3, provenance="bogus"),
                pt(3, provenance=["x"]),
                pt(3, links=5),
                pt(3, links=[5]),
                pt(3, links=[{"type": "nope", "target_id": ulid(1)}]),
                restore(1, None),
            ],
        },
        {
            "name": "restore",
            "steps": [
                put(1),
                tomb(1),
                restore(1),
                restore(1),
                restore(9),
                restore("not-a-ulid"),
                restore(""),
                tomb(1),
                restore(1, None),
            ],
        },
        {
            "name": "relink",
            "steps": [
                linked(
                    1, link("extends", 2), link("depends_on", 6), link("contradicts", 7)
                ),
                linked(2, link("extends", 1)),
                put(3, [1]),
                linked(4, link("depends_on", 1)),
                linked(5, link("contradicts", 1)),
                put(6),
                tomb(6),
                sql(BREAK_POINTER, 4),
                sql(LATER_FIELD, 5),
                tomb(1),
                restore(1),
            ],
        },
        {
            "name": "returned_marks",
            "steps": [
                put(1),
                put(2, [1]),
                tomb(2),
                restore(2),
                tomb(1),
                restore(1, None),
            ],
        },
        {"name": "corrupt_rows", "steps": corrupt_rows()},
        {
            "name": "restore_trimmed",
            "steps": [
                put(
                    1,
                    claims=["a", "b", "c"],
                    verified_paths=["p", "q"],
                    verified_path_targets={"p": None, "q": "t"},
                    last_verified_at=T1,
                    verified_head="a" * 40,
                ),
                linked(2, link("extends", 1)),
                tomb(1),
                trimmed(1, drop_claims=[["x"]]),
                trimmed(
                    1,
                    drop_claims=["a", "zz"],
                    drop_verified_paths={"$tuple": ["p"]},
                    clear_verification=True,
                    drop_verified_head=True,
                ),
                trimmed(1),
                trimmed("not-a-ulid"),
                trimmed(9),
                tomb(1),
                trimmed(
                    1, drop_claims="ab", clear_verification=1, drop_verified_head=""
                ),
                tomb(1),
                trimmed(1, session=None, drop_verified_paths=["q"], drop_claims=[]),
            ],
        },
    ]


def _run(store: Store, call: dict[str, Any]) -> Any:
    from .gen_store_records_b import _run as run_b

    what = call["call"]
    if what == "sql":
        statements = call["sql"] if isinstance(call["sql"], list) else [call["sql"]]
        for statement in statements:
            store.conn.execute(statement, (call["id"],))
        store.conn.commit()
        return None
    if what == "put_tombstone":
        kw = {k: decode(v) for k, v in call["kw"].items()}
        if isinstance(kw.get("last_corroborated"), str):
            kw["last_corroborated"] = datetime.fromisoformat(kw["last_corroborated"])
        got = store.put_tombstone(
            TombstonedMemory.model_validate(call["dead"]),
            session=call["session"],
            **kw,
        )
        return canonical_payload(record_payload(got))
    if what == "restore":
        got_m = store.restore(call["id"], session=call["session"])
        return canonical_payload(record_payload(got_m))
    if what == "restore_trimmed":
        kw = {k: decode(v) for k, v in call["kw"].items()}
        got_m = store.restore_trimmed(call["id"], session=call["session"], **kw)
        return canonical_payload(record_payload(got_m))
    return run_b(store, call)


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_records_d",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c, _run, refusal) for c in cases()]
    opened = runs[0]["opened"]
    for run in runs:
        if run.pop("opened") != opened:
            raise AssertionError(f"{run['case']} opened another state")
    lines[0]["opened"] = opened
    lines += runs
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_records_d.jsonl": text.encode("ascii")}
