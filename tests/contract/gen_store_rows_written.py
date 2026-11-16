"""Fixture: the store's small row writers, call by call
(``store_rows_written.jsonl``).

The native build ports a record's home (`Store._home_row`), a standing
answer (`_standing_row`, `put_standing`), the episode journal's writers
(`put_episode`, `write_episode`, `write_floor`, `delete_episode`) and the
verification, conflict, import and quarantine writers (``native/``,
`store::home`, `store::tables`) and is held to this file. Each case starts
from the store `Store.create` makes under ``store_create.json``'s fixed
values, opened as the owner (as ``store_tx.jsonl``'s are), and runs its
steps: an edit (the journal mode, a table dropped), a memory put through
``_mutate`` in a transaction of its own (the records the writers name), or
a writer call. ``home`` is ``_home_row`` and its ``home_put`` in one
transaction, as ``put_memory`` writes a record's home. Every clock and
mint is fixed (`_fixed.fixed`), the log clock and the wall clock apart,
and each step records what each handed out (the ids minted among them)
and the order of every read (``log``, ``wall``, ``mint``),
so a replay reads them in the same order and as often.

After each call: its returned value (an episode's JSON dump, a standing
row, a home row, or null) or its refusal's class and text, and, as
``store_tx.jsonl`` records after each transaction, the rows appended
(every column), the state's digest, the head's text and the FTS5 index as
queries see it.

Gates, the same as ``store_tx.jsonl``'s and for the same reasons.
Unicode: the index text is the tokenizer's spelling and the digest's rows
its tokens, and ``str.strip`` is Unicode's whitespace. pydantic-core: a
refused episode's text is the first line of a ValidationError, and the
models decide what a payload holds. POSIX only: the state includes modes
and the binding's resolved path. Not on the Python version: every text
compared is the product's own wording, pydantic's first line, or a
``repr`` of plain text. Not on the SQLite version: FTS5's shadow tables
are not compared, only what a ``MATCH`` returns.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bettermemory.models import Episode, Memory
from bettermemory.store import Store
from bettermemory.store_schema import RecordHome, StandingAnswer

from ._fixed import Ticker, fixed, ulid_mint
from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS, _engine, _made, state
from .gen_store_tx import LOG0, LOG_STEP, WALL0, WALL_STEP
from .gen_store_write import (
    decode,
    _last_seq,
    _refusal,
    fts_view,
    log_rows,
    memory,
    state_digest,
    ulid,
)

NOW = "2004-01-02T03:04:05.678900+00:00"
H1 = ["p-one", "c-one", "repository"]
H2 = ["p-two", None, "project-file"]


def _m(n: int) -> str:
    return ulid(n)


def mem(n: int, body: str | None = None) -> dict[str, Any]:
    return {"call": "memory_put", "payload": memory(n, body=body)}


def ep(n: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": ulid(n, "01E"),
        "session_id": "s-ep",
        "created": "2002-03-04T05:06:07.000008Z",
        "body": f"episode {n} about sqlite\n",
    }
    record.update(fields)
    return record


def call(name: str, /, **kw: Any) -> dict[str, Any]:
    return {"call": name, **kw}


def write(body: Any = "a journal entry\n", **kw: Any) -> dict[str, Any]:
    args = {
        "session_id": "s-w",
        "body": body,
        "scopes": None,
        "takeaway": None,
        "swarm_id": None,
        "origin": None,
    }
    args.update(kw.pop("args", {}))
    return call(
        "write_episode", args=args, now=kw.pop("now", None), home=kw.pop("home", None)
    )


def standing(n: int, p: Any, src: Any = "user", by: Any = "tester") -> dict[str, Any]:
    return call("put_standing", id=_m(n), answer=[p, src, by], session="s-st")


def verification(n: int, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "memory_id": _m(n),
        "machine_id": None,
        "verified_head": None,
        "verified_paths": [],
        "verified_commits": [],
        "verified_versions": [],
        "verified_absent_paths": [],
        "verified_path_targets": None,
        "verified_absent_path_targets": None,
    }
    at = kw.pop("verified_at", "2005-06-07T08:09:10.111213+00:00")
    args.update(kw)
    return call("put_verification", args=args, verified_at=at, session="s-v")


def conflict(**record: Any) -> dict[str, Any]:
    return call("put_conflict", record=record, session="s-c")


def cases() -> list[dict[str, Any]]:
    full_conflict = {
        "id": "c1",
        "a_id": _m(1),
        "b_id": _m(2),
        "summary_a": "a",
        "summary_b": "b",
        "similarity": 0.75,
        "method": "lexical",
        "detector": "write",
        "created": "2006-01-01T00:00:00Z",
        "status": "open",
    }
    return [
        {
            "name": "homes",
            "steps": [
                mem(1),
                call("home", id=_m(1), kind="memory", home=H1, session="s-h"),
                # The same home again: None, no row.
                call("home", id=_m(1), kind="memory", home=H1, session="s-h"),
                # Another home for the same id.
                call("home", id=_m(1), kind="memory", home=H2, session="s-h"),
                call(
                    "home",
                    id=_m(1),
                    kind="memory",
                    home=["p-one", "c-two", "repository"],
                ),
                call("home", id=_m(1), kind="memory", home=None),
                call("home", id=_m(2), kind="memory", home=["", None, "r"]),
                call("home", id=_m(2), kind="memory", home=[None, None, "r"]),
                call("home", id=_m(2), kind="memory", home=[7, None, "r"]),
                call("home", id=_m(2), kind="memory", home=["p", None, ""]),
                call("home", id=_m(2), kind="memory", home=["p", None, None]),
                # Both refused: the project is checked first.
                call("home", id=_m(2), kind="memory", home=["", None, ""]),
                # Not a str checkout is written as given.
                call("home", id=_m(3), kind="episode", home=["global", 5, "fallback"]),
                call("home", id=_m(3), kind="episode", home=["global", 5, "fallback"]),
                call(
                    "home", id=_m(3), kind="episode", home=["global", 5.0, "fallback"]
                ),
                call(
                    "home", id=_m(3), kind="episode", home=["global", True, "fallback"]
                ),
            ],
        },
        {
            "name": "standing",
            "steps": [
                mem(1, body="  padded body needing the strip  "),
                mem(2, body="a body\nover lines\n"),
                standing(1, 0),
                standing(1, 1),
                standing(1, 0.25),
                standing(2, 1.0),
                standing(2, 0.0),
                standing(1, 1.5),
                standing(1, -0.1),
                standing(1, 2),
                standing(1, "0.5"),
                standing(1, None),
                standing(1, True),
                standing(1, False),
                standing(1, {"$int": "1" + "0" * 400}),
                standing(1, 0.5, src=""),
                standing(1, 0.5, by=None),
                standing(1, 0.5, src=None, by=""),
                standing(9, 0.5),
                standing(9, 7),
                standing(2, 0.5, src="hook", by=["a", "list"]),
            ],
        },
        {
            "name": "episodes",
            "steps": [
                call("put_episode", episode=ep(1), session="s-p", home=None),
                call("put_episode", episode=ep(2), session=None, home=H1),
                call("put_episode", episode=ep(2), session="s-p", home=H1),
                call(
                    "put_episode",
                    episode=ep(2, body="changed\n"),
                    session="s-p",
                    home=H2,
                ),
                call(
                    "put_episode",
                    episode=ep(3, scopes=["global"], takeaway="t", swarm_id="sw-1"),
                    session="s-p",
                    home=["global", None, "fallback"],
                ),
                call("put_episode", episode=ep(4), session="s-p", home=["", None, "r"]),
                call("delete_episode", id=ulid(1, "01E"), session="s-d"),
                call("delete_episode", id=ulid(1, "01E"), session="s-d"),
                call("delete_episode", id="no-such", session=None),
            ],
        },
        {
            "name": "write_episode",
            "steps": [
                write(),
                write("  needs the strip \t\n"),
                write("x", now=NOW),
                write("homed", home=H1),
                write("homed at a given time", now=NOW, home=H2),
                write("refused home", home=[None, None, "r"]),
                write(""),
                write("   \n\t"),
                write(None),
                write(0),
                write(5),
                write(["a list"]),
                write(
                    "with takeaway",
                    args={"takeaway": "  keep this  ", "scopes": ["global", "repo:x"]},
                ),
                write("takeaway blank", args={"takeaway": "   "}),
                write("takeaway number", args={"takeaway": 3}),
                write("scopes string", args={"scopes": "ab"}),
                write("scopes number", args={"scopes": 4}),
                write("bad scope", args={"scopes": ["NOT A SCOPE"]}),
                write("bad session", args={"session_id": 12}),
                write("bad swarm", args={"swarm_id": "has space"}),
                write("good swarm", args={"swarm_id": "sw-2"}),
                call("write_floor", session_id="s-f", origin=None, now=None, home=None),
                call("write_floor", session_id="s-f", origin=None, now=NOW, home=H1),
                call("write_floor", session_id=None, origin=None, now=None, home=None),
            ],
        },
        {
            "name": "verifications",
            "steps": [
                verification(1),
                verification(
                    1,
                    verified_head="a" * 40,
                    verified_paths=["src/x.py"],
                    verified_commits=["b" * 40],
                    verified_versions=["pkg==1"],
                    verified_absent_paths=["gone.py"],
                    verified_path_targets={"src/x.py": "c" * 64},
                    verified_absent_path_targets={"gone.py": None},
                ),
                verification(1, machine_id="m1"),
                verification(2, verified_at="2005-06-07T08:09:10"),
                verification(3, verified_paths=[1, 2]),
                call("delete_verification", memory_id=_m(1), machine_id=None),
                call("delete_verification", memory_id=_m(1), machine_id=None),
                call("delete_verification", memory_id=_m(1), machine_id="m1"),
                call("delete_verification", memory_id=_m(1), machine_id="m2"),
            ],
        },
        {
            "name": "conflicts_imports_quarantine",
            "steps": [
                conflict(**full_conflict),
                conflict(**{**full_conflict, "status": "resolved", "note": "n"}),
                conflict(id="c2", a_id=_m(1), b_id=_m(3), unknown="ignored"),
                conflict(a_id=_m(1), b_id=_m(3)),
                conflict(id="c3", a_id="", b_id=_m(3)),
                conflict(id="c3", a_id=_m(1)),
                conflict(id="c4", a_id=_m(1), b_id=_m(3), similarity="high"),
                call("put_conflict", record=["not", "a", "mapping"], session=None),
                call("delete_conflict", id="c1", session="s-c"),
                call("delete_conflict", id="c1", session="s-c"),
                call(
                    "put_import",
                    source="notes/a.md",
                    content_hash="d" * 64,
                    imported_at="2006-02-03T04:05:06Z",
                    memory_id=_m(1),
                ),
                call(
                    "put_import",
                    source="notes/a.md",
                    content_hash="e" * 64,
                    imported_at="2006-02-03T04:05:07Z",
                    memory_id=None,
                ),
                call(
                    "put_import",
                    source=None,
                    content_hash="e" * 64,
                    imported_at="t",
                    memory_id=None,
                ),
                call("delete_import", source="notes/a.md"),
                call("delete_import", source="notes/a.md"),
                call(
                    "put_quarantine",
                    name="q1",
                    reason="unreadable",
                    at="2006-03-04T05:06:07Z",
                    detail="bad bytes",
                    remote="origin",
                    size=12,
                    sha256="f" * 64,
                ),
                call(
                    "put_quarantine",
                    name="q1",
                    reason="again",
                    at="2006-03-04T05:06:08Z",
                    detail=None,
                    remote=None,
                    size=None,
                    sha256=None,
                ),
                call(
                    "put_quarantine",
                    name="q2",
                    reason=None,
                    at="t",
                    detail=None,
                    remote=None,
                    size=None,
                    sha256=None,
                ),
                call("delete_quarantine", name="q1"),
                call("delete_quarantine", name="q1"),
            ],
        },
        {
            # Every type in each position whose refusal quotes a type name.
            "name": "type_names",
            "steps": [
                mem(1),
                *[write(v) for v in TYPED],
                *[write("scoped", args={"scopes": v}) for v in TYPED],
                *[write("taken", args={"takeaway": v}) for v in TYPED],
                *[call("put_conflict", record=v, session="s-c") for v in TYPED],
                *[standing(1, 0.5, by=v) for v in TYPED],
            ],
        },
        {
            "name": "rollback_journal",
            "steps": [
                {"edit": "journal", "mode": "DELETE"},
                mem(1),
                write("in rollback-journal mode", home=H1),
                standing(1, 0.5),
                call("delete_episode", id="no-such", session=None),
            ],
        },
        {
            "name": "homes_table_dropped",
            "steps": [
                mem(1),
                {"edit": "drop", "table": "homes"},
                write("no homes table", home=H1),
                write("no home asked"),
                {"edit": "drop", "table": "standing"},
                standing(1, 0.5),
            ],
        },
    ]


# ---------------------------------------------------------------------------


class Read(Ticker):
    """A `Ticker` that also writes its name to a shared list of reads, so
    a step records the order the log clock, the wall clock and the mint
    were read in."""

    def __init__(
        self, start: datetime, step: timedelta, name: str, order: list[str]
    ) -> None:
        super().__init__(start, step)
        self.name = name
        self.order = order

    def __call__(self) -> datetime:
        self.order.append(self.name)
        return super().__call__()


#: A value of every type the native arguments can hold, in the fixture's
#: tagged spelling (`gen_store_write.decode`), for the refusals that quote
#: a type's name.
TYPED: list[Any] = [
    None,
    True,
    7,
    1.5,
    [1],
    {"$tuple": ["a"]},
    {"k": 1},
    {"$datetime": "2004-01-02T03:04:05+00:00"},
    {"$path": "a/b"},
]


def _home(h: list[Any] | None) -> RecordHome | None:
    return None if h is None else RecordHome(*h)


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _run(store: Store, step: dict[str, Any]) -> Any:
    name = step["call"]
    if name == "memory_put":
        with store._transaction() as conn:
            store._mutate(conn, "memory_put", step["payload"], session="s-m")
        return None
    if name == "home":
        with store._transaction() as conn:
            row = store._home_row(step["id"], step["kind"], _home(step["home"]))
            if row is not None:
                store._mutate(
                    conn, "home_put", {"home": row}, session=step.get("session")
                )
        return row
    if name == "put_standing":
        p, src, by = step["answer"]
        if isinstance(p, dict) and "$int" in p:
            p = int(p["$int"])
        else:
            p = decode(p)
        src, by = decode(src), decode(by)
        return store.put_standing(
            step["id"], StandingAnswer(p, src, by), session=step["session"]
        )
    if name == "put_episode":
        episode = Episode.model_validate(step["episode"])
        out = store.put_episode(
            episode, session=step["session"], home=_home(step["home"])
        )
        return out.model_dump(mode="json")
    if name == "write_episode":
        out = store.write_episode(
            **decode(step["args"]), now=_dt(step["now"]), home=_home(step["home"])
        )
        return out.model_dump(mode="json")
    if name == "write_floor":
        out = store.write_floor(
            session_id=step["session_id"],
            origin=step["origin"],
            now=_dt(step["now"]),
            home=_home(step["home"]),
        )
        return out.model_dump(mode="json")
    if name == "delete_episode":
        return store.delete_episode(step["id"], session=step["session"])
    if name == "put_verification":
        return store.put_verification(
            verified_at=datetime.fromisoformat(step["verified_at"]),
            session=step["session"],
            **step["args"],
        )
    if name == "delete_verification":
        return store.delete_verification(
            step["memory_id"], machine_id=step["machine_id"]
        )
    if name == "put_conflict":
        return store.put_conflict(decode(step["record"]), session=step["session"])
    if name == "delete_conflict":
        return store.delete_conflict(step["id"], session=step["session"])
    if name == "put_import":
        return store.put_import(
            step["source"],
            content_hash=step["content_hash"],
            imported_at=step["imported_at"],
            memory_id=step["memory_id"],
        )
    if name == "delete_import":
        return store.delete_import(step["source"])
    if name == "put_quarantine":
        kw = {k: v for k, v in step.items() if k not in ("call", "name")}
        return store.put_quarantine(step["name"], **kw)
    if name == "delete_quarantine":
        return store.delete_quarantine(step["name"])
    raise AssertionError(name)


def _edit(edit: dict[str, Any], store: Store) -> None:
    if edit["edit"] == "journal":
        store.conn.execute(f"PRAGMA journal_mode = {edit['mode']}")
    elif edit["edit"] == "drop":
        store.conn.execute(f"DROP TABLE {edit['table']}")
        store.conn.commit()
    else:
        raise AssertionError(edit)


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
                if "edit" in step:
                    _edit(step, store)
                    steps_out.append({"edit": step})
                    continue
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
                        returned = _run(store, step)
                        outcome: dict[str, Any] = {"returned": returned}
                    except Exception as exc:  # noqa: BLE001 - the refusal is the record
                        outcome = {"refusal": _refusal(exc)}
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
            "fixture": "store_rows_written",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
            "memory_fields": list(Memory.model_fields),
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
    return {"store_rows_written.jsonl": text.encode("ascii")}
