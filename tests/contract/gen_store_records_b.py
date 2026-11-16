"""Fixture: the record writers ``write`` and ``update``
(``store_records_b.jsonl``).

The native build ports them as functions on the writable store
(``native/``, `store::writers`, part B) and is held to this file. A file
of its own, not more cases in ``store_records.jsonl``: that file is near
its budget, and each step here also records the ids minted and the order
of every read, as ``store_rows_written.jsonl`` does.

Each case starts from the store `Store.create` makes under
``store_create.json``'s fixed values, opened as the owner, and runs its
steps: an edit, or one call (``put_memory`` to set a record up, ``write``,
``update``). Every clock and mint is fixed (`_fixed.fixed`), the log clock,
the wall clock and the mint apart; each step records what each handed out
and the order of every read (``log``, ``wall``, ``mint``).

After each call: the returned record's canonical payload text, or the
refusal's class and text, and the rows appended (every column), the
state's digest, the head's text and the FTS5 index as queries see it. A
step marked ``reference_only`` passes the reference a record no native
caller can build (an `update` whose snapshot holds non-text claims: the
copy is written as given, `models.py` 467-485's ``str()`` applied only to
the record validated as a check); it is the last step of its case, and
the case's refold records what the reference left.

Gates, the same as ``store_tx.jsonl``'s and for the same reasons.
Unicode: the index text is the tokenizer's spelling and the digest's rows
its tokens, and ``str.strip`` is Unicode's whitespace. pydantic-core: a
refusal's text is the first line of a ValidationError, and the models
decide what a payload holds. POSIX only: the state includes modes and the
binding's resolved path. Not on the Python version: every text compared
is the product's own wording, pydantic's first line, or a ``repr`` or
``isoformat`` of plain values. Not on the SQLite version: FTS5's shadow
tables are not compared, only what a ``MATCH`` returns.
"""

from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from bettermemory.log import canonical_payload
from bettermemory.models import Memory, record_payload
from bettermemory.store import Store
from bettermemory.store_schema import RecordHome, StandingAnswer

from ._fixed import fixed, ulid_mint
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS, _engine, _made, state
from .gen_store_rows_written import Read
from .gen_store_tx import LOG0, LOG_STEP, WALL0, WALL_STEP
from .gen_store_write import (
    _last_seq,
    _refusal,
    decode,
    fts_view,
    log_rows,
    memory,
    state_digest,
    ulid,
)

HOME = ["global", None, "fallback"]
ANSWER = [0.5, "explicit", "test"]
T = "2001-02-03T04:05:06.000000Z"


def rec(n: int, sup: list[int] | None = None, **f: Any) -> dict[str, Any]:
    links = [{"type": "supersedes", "target_id": ulid(t)} for t in sup or []]
    return memory(n, links=links, **f)["memory"]


def put(n: int, sup: list[int] | None = None, **f: Any) -> dict[str, Any]:
    return {"call": "put", "memory": rec(n, sup, **f), "session": "s0"}


def write(content: Any = "a body about sqlite", scopes: Any = None, **kw: Any):
    args = {"content": content, "scopes": ["global"] if scopes is None else scopes}
    args.update(kw)
    home = args.pop("home", None)
    standing = args.pop("standing", None)
    session = args.pop("session", "s1")
    return {
        "call": "write",
        "args": args,
        "home": home,
        "standing": standing,
        "session": session,
    }


def update(
    n: int,
    sup: list[int] | None = None,
    *,
    force: bool = False,
    preserve: bool = False,
    standing: list[Any] | None = None,
    session: str | None = "s2",
    native: str = "same",
    **f: Any,
) -> dict[str, Any]:
    record = rec(n, sup, **f) if native == "same" else _raw(n, **f)
    return {
        "call": "update",
        "memory": record,
        "force": force,
        "preserve_verification": preserve,
        "standing": standing,
        "session": session,
        "native": native,
    }


def _raw(n: int, **f: Any) -> dict[str, Any]:
    record = rec(n)
    record.update(f)
    return record


def tomb(n: int) -> dict[str, Any]:
    return {"call": "tombstone", "id": ulid(n), "session": "s0"}


VERIFIED: dict[str, Any] = {
    "last_verified_at": "2001-02-03T04:05:07.000000Z",
    "verified_paths": ["src/a.py"],
    "verified_commits": ["c1"],
    "claims": ["claim one"],
    "verified_head": "a" * 40,
}


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "write_plain",
            "steps": [
                write(),
                write(" \t\x1c\x1f　 body with Python's spaces \x0b \n"),
                write("​zero width stays​"),
                write("x", ["global", "work"], session=None),
            ],
        },
        {
            "name": "write_every_argument",
            "steps": [
                put(1),
                write(
                    "every argument about docker",
                    ["global"],
                    confidence="high",
                    source="user-correction",
                    origin={"cwd": "/w", "repo": "r", "branch": "main"},
                    category="ambient",
                    claims=["one claim", "two"],
                    links=[{"type": "supersedes", "target_id": ulid(1)}],
                    actor={"client": "cli", "model": "m", "sources": {"a": "b"}},
                    home=HOME,
                    standing=ANSWER,
                ),
                write("tuple scopes", {"$tuple": ["global"]}, claims={"$tuple": []}),
                write("str scopes are letters", "ab"),
            ],
        },
        {
            "name": "write_refused",
            "steps": [
                write(scopes=5),
                write(content=5),
                write(content=None, scopes=7),
                write(scopes=[]),
                write(confidence="bogus"),
                write(actor={"client": 5}),
                write(actor=5),
                write(claims=7),
                write(links=[5]),
                write(links=[{"type": "nope", "target_id": "x"}]),
                write(home=["", None, "r"]),
                write(standing=[2, "s", "a"]),
                write(standing=[0.5, "", "a"]),
                write(home=HOME, standing=[True, "s", "a"]),
            ],
        },
        {
            "name": "update_snapshot",
            "steps": [
                put(1),
                update(1, body="fresh snapshot"),
                update(1, body="stale snapshot"),
                update(1, body="forced over a stale one", force=True),
                update(9),
                tomb(1),
                update(1, force=True),
            ],
        },
        {
            "name": "update_offset_snapshot",
            "steps": [
                put(1, updated="2001-02-03T06:05:06.250000+02:00"),
                update(1, updated="2001-02-03T04:05:06.250000Z", body="same instant"),
                update(1, updated="2001-02-03T04:05:06.250000", body="naive"),
            ],
        },
        {
            "name": "update_verification",
            "steps": [
                put(1, corroborations=4, **VERIFIED),
                update(1, body="kept", preserve=True, updated=T),
                update(
                    1,
                    body="reset",
                    force=True,
                    verified_paths=["other.py"],
                    claims=["other claim"],
                ),
            ],
        },
        {
            "name": "update_links_and_standing",
            "steps": [
                put(1),
                put(2, [1]),
                update(2, [], body="link dropped", force=True),
                update(2, [1], body="link back", force=True, standing=ANSWER),
                update(2, [1], force=True, standing=[-1, "s", "a"]),
            ],
        },
        {
            # The copy is written as given; `str()` applies only to the
            # record validated as a check.
            "name": "update_unvalidated_copy",
            "steps": [
                put(1),
                update(1, force=True, native="reference_only", claims=[1, 2.5]),
            ],
        },
    ]


def _memory_of(call: dict[str, Any]) -> Memory:
    m = Memory.model_validate(call["memory"])
    if call.get("native") == "reference_only":
        # `model_copy` sets the values without validating them.
        m = m.model_copy(update={"claims": call["memory"]["claims"]})
    return m


def _run(store: Store, call: dict[str, Any]) -> str | None:
    what = call["call"]
    session = call["session"]
    if what == "put":
        got = store.put_memory(Memory.model_validate(call["memory"]), session=session)
    elif what == "tombstone":
        store.tombstone(call["id"], "removed", session=session)
        return None
    elif what == "write":
        args = {k: decode(v) for k, v in call["args"].items()}
        home = call["home"]
        standing = call["standing"]
        got = store.write(
            **args,
            session=session,
            home=None if home is None else RecordHome(*home),
            standing=None if standing is None else StandingAnswer(*standing),
        )
    elif what == "update":
        standing = call["standing"]
        got = store.update(
            _memory_of(call),
            force=call["force"],
            preserve_verification=call["preserve_verification"],
            session=session,
            standing=None if standing is None else StandingAnswer(*standing),
        )
    else:
        raise AssertionError(what)
    return canonical_payload(record_payload(got))


def run_case(case_def: dict[str, Any]) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            opened = state(case, root)
            for i, step in enumerate(case_def["steps"]):
                reads: list[str] = []
                log = Read(LOG0 + timedelta(minutes=i), LOG_STEP, "log", reads)
                wall = Read(WALL0 + timedelta(hours=i), WALL_STEP, "wall", reads)
                minted: list[str] = []
                inner = ulid_mint(1000 * i)

                def mint(
                    inner: Any = inner,
                    minted: list[str] = minted,
                    reads: list[str] = reads,
                ) -> str:
                    reads.append("mint")
                    minted.append(inner())
                    return minted[-1]

                before = _last_seq(file)
                with fixed(log=log, wall=wall, mint=mint):
                    try:
                        outcome: dict[str, Any] = {
                            "written": True,
                            "returned": _run(store, step),
                        }
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"written": False, "refusal": _refusal(exc)}
                steps_out.append(
                    {
                        "step": i,
                        "call": step,
                        "log_clock": log.stamps(),
                        "wall_clock": wall.stamps(),
                        "minted": minted,
                        "reads": reads,
                        "outcome": outcome,
                        "rows": log_rows(file, before),
                        "state": state_digest(state(case, root)),
                        "fts": fts_view(file),
                    }
                )
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
        "case": case_def["name"],
        "opened": opened,
        "refold": refold,
        "steps": steps_out,
    }


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_records_b",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c) for c in cases()]
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
    return {"store_records_b.jsonl": text.encode("ascii")}
