"""Real events.Recorder -> signed SQLite store, owning, joined and keyless.

Fresh POSIX-bound keys and the fixed signed genesis are provided by gen_store_open.
Every group runs sequential calls through the same recorder. Fields and errors
use code points where needed. Only temporary path prefixes are normalized.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import math
import random
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bettermemory import events, identity, store_events
from bettermemory.log import canonical_payload
from bettermemory.store import Store

from ._common import captured_by, jsonl, neutral
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_store_open import _engine, _made
from .gen_store_record_event import LOG0, LOG_STEP, WALL0
from .gen_store_write import log_rows


def call(
    kind: str = "recall", *, fields: dict[str, Any] | None = None, **options: Any
) -> dict[str, Any]:
    return {"kind": kind, "fields": fields or {}, **options}


def alias_case(pairs: list[Any], **options: Any) -> dict[str, Any]:
    identities: dict[int, int] = {}
    aliases = []
    for i, (key, _) in enumerate(pairs):
        pending = [key]
        ordinal = 0
        while pending:
            node = pending.pop()
            if isinstance(node, tuple):
                pending.extend(reversed(node))
            elif isinstance(node, float) and math.isnan(node):
                aliases.append(
                    [i, ordinal, identities.setdefault(id(node), len(identities))]
                )
                ordinal += 1
    return call(
        "write",
        fields={"actor": pairs},
        aliases=aliases,
        survivors=list(dict(pairs).values()),
        **options,
    )


def sort_case(count: int, layout: str, field: str) -> dict[str, Any]:
    """Keep the dict insertion order Python sorts, including its unordered NaN."""
    keys: list[Any] = list(range(count - 1))
    if layout == "scattered":
        random.Random(19 + count).shuffle(keys)
    elif layout == "descending":
        keys.reverse()
    index = {
        "leading": 0,
        "trailing": count - 1,
        "scattered": count // 3,
        "descending": count // 2,
    }[layout]
    keys.insert(index, float("nan"))
    mapping = {key: f"v{i}" for i, key in enumerate(keys)}
    digest = hashlib.sha256(canonical_payload(mapping).encode()).hexdigest()
    if field == "actor":
        fields = {"actor": list(mapping.items())}
        return call(
            "write", fields=fields, raw_json=json.dumps(fields), canonical_sha=digest
        )
    return call("write", fields={field: mapping}, canonical_sha=digest)


def groups() -> dict[str, list[dict[str, Any]]]:
    a, b = float("nan"), float("nan")
    wire = '{"actor":[[NaN,"a"],[3,"integer"],[NaN,"last"]]}'
    return {
        "nan_order": [
            sort_case(63, "leading", "actor"),
            sort_case(64, "leading", "other"),
            sort_case(65, "scattered", "actor"),
            sort_case(129, "scattered", "other"),
            sort_case(267, "trailing", "actor"),
        ],
        "actor_objects": [
            alias_case(json.loads(wire)["actor"], raw_json=wire),
            alias_case(json.loads(wire, parse_constant=float)["actor"]),
            alias_case([(a, "first"), (b, "second"), (a, "final")]),
            alias_case([(((a,), 1), "first"), (((a,), 1), "final")]),
            alias_case([(((a,), 1), "first"), (((b,), 1), "final")]),
            alias_case([(((a,), "tag"), "first"), (((a,), 0), "last")]),
            alias_case([(((a,), "tag"), "first"), (((b,), 0), "last")]),
            alias_case(
                [
                    (a, "first"),
                    (a, "collapse"),
                    (((a, b), "tag"), "first tuple"),
                    (((a, b), 3), "final tuple"),
                ]
            ),
        ],
        "owner": [
            call(
                "search",
                fields={"query": "trace ghp_" + "a" * 36, "note": "<|im_start|>"},
                caller="a",
            ),
            call(
                "search",
                fields={
                    "query": "one more précis",
                    "session": None,
                    "actor": [("same", 1), ("same", 2)],
                },
                caller="b",
            ),
            call(
                "standing_guard",
                fields={"said": "discard me", "door": "ok", "attempts": []},
                enabled=False,
            ),
            call("standing_guard", fields={"attempts": [], "best_effort": False}),
            call("standing_guard", fields={"best_effort": False, "attempts": []}),
            call("standing_guard", fields={"session": 0}),
            call("standing_guard", fields={"session": "\udcff"}),
            call(fields={"session": 99, "actor": {1: 0, "s": 1}}),
            call(fields={"actor": {"s": 1, 2: 0}}),
            call(fields={"actor": [[1]]}),
            call(fields={"attempts": float("nan"), "deadline": False, "session": ""}),
            call(fields={"attempts": float("inf"), "deadline": []}),
            call(fields={"attempts": [], "deadline": []}),
            call(fields={"deadline": 10**400}),
            call(fields={"deadline": ["early"], "session": 1}),
            call(
                fields={
                    "deadline": 0.0,
                    "attempts": 3,
                    "best_effort": [],
                    "extra": "\udcff",
                }
            ),
            call(fields={"deadline": 1}, hook=True),
            call(
                "write", fields={"query": "quiet", "extra": "discard"}, suppressed=True
            ),
            call(fields={"actor": 99}, enabled=False),
            call("memory_put", fields={"actor": 99}),
        ],
        "ordered_keys": [
            call("write", fields={"other": {(4,): 1, (1,): 2}}),
            call(fields={"other": {("a", (1,)): 2, ("a", ("x",)): 0}, "session": 0}),
            call(
                "standing_guard",
                fields={"extra": {PurePosixPath("xx/z"): 4, PurePosixPath("b"): 3}},
            ),
            call(
                fields={
                    "extra": {
                        datetime(2002, 1, 1): 1,
                        datetime(2002, 1, 1, tzinfo=timezone.utc): 0,
                    }
                }
            ),
            call(fields={"actor": [(False, 1), (0.0, 2), (1, 4), (True, 5), ("b", 0)]}),
            call(fields={"actor": [(False, 1), (0.0, 2), (1, 4), (True, 5), (2.5, 6)]}),
        ],
        "keyless": [
            call(
                fields={"actor": 42, "attempts": [], "deadline": [], "query": "secret"}
            ),
            call("standing_guard", fields={"session": 7}),
            call("memory_put", fields={"actor": 42}),
        ],
        "joined_commit": [
            call(
                fields={"attempts": [], "deadline": ["never read"], "query": "joined"}
            ),
            call(fields={"actor": 0}),
            call("write", fields={"session": "joined"}),
        ],
        "joined_rollback": [
            call(fields={"session": None}),
            call(fields={"attempts": [], "deadline": float("inf")}),
        ],
        "locked": [
            call(fields={"attempts": 2, "best_effort": []}, lock=True),
            call(
                fields={"attempts": 10**80 + 7, "deadline": 99}, lock=True, release=True
            ),
            call("standing_guard", lock=True),
            call(fields={"deadline": -1, "best_effort": [False]}, lock=True),
        ],
        "readonly": [
            call(fields={"session": 99}),
            call("write", fields={"session": 42}),
        ],
    }


def points(s: str) -> list[int]:
    return list(map(ord, s))


def run(name: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    logs: list[Any] = []
    with tempfile.TemporaryDirectory() as raw, contextlib.ExitStack() as stack:
        root = Path(raw) / "case"
        _made("created", {}, root)
        file = root / "memory.sqlite"
        with _engine():
            store = stack.enter_context(
                Store.open(
                    file, keys_dir=root / ("absent" if name == "keyless" else "keys")
                )
            )

        def norm(s: str) -> list[int]:
            return points(s.replace(str(root), "<case>"))

        refusal = norm(store.key_state.refusal(file)) if name == "keyless" else None

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                logs.append([record.name, record.levelname, norm(record.getMessage())])

        for logger in (events.log, store_events.log):
            stack.enter_context(captured_by(logger, Capture(), logging.INFO))
        recorder = events.Recorder(
            store,
            "process",
            worktree_root="/fixture",
            attribution="cli_fixture",
            triggered_from="hook",
        )
        if name == "locked":
            store.set_busy_timeout(0)
        if name == "readonly":
            store._conn.execute("PRAGMA query_only=ON")
        before = store._conn.execute("SELECT max(seq) FROM log").fetchone()[0]
        transaction = (
            store.batch() if name.startswith("joined") else contextlib.nullcontext()
        )
        steps = []

        class Undo(Exception):
            pass

        try:
            with transaction:
                for index, item in enumerate(calls):
                    log = Ticker(LOG0 + timedelta(minutes=index), LOG_STEP)
                    mono: list[int] = []
                    pauses: list[float] = []
                    logs.clear()
                    with contextlib.ExitStack() as per:
                        if item.get("lock"):
                            blocker = sqlite3.connect(file, isolation_level=None)
                            per.callback(blocker.close)
                            blocker.execute("BEGIN IMMEDIATE")

                        def now() -> float:
                            mono.append(3)
                            return 3.0

                        def pause(seconds: float) -> None:
                            pauses.append(seconds)
                            if item.get("release"):
                                blocker.commit()

                        per.enter_context(fixed(log=log, wall=Ticker(WALL0, LOG_STEP)))
                        per.enter_context(
                            patch.object(
                                store_events,
                                "time",
                                SimpleNamespace(monotonic=now, sleep=pause),
                            )
                        )
                        if caller := item.get("caller"):
                            actor = identity.Actor(
                                session=caller, sources={"session": "header"}
                            )
                            token = identity.publish(
                                identity.Caller(
                                    actor=actor, harness_session=f"h-{caller}"
                                )
                            )
                            per.callback(identity.reset, token)
                        if item.get("suppressed"):
                            per.enter_context(events.suppressed())
                        if item.get("hook"):
                            import time

                            per.enter_context(
                                patch.object(time, "monotonic", lambda: 0.0)
                            )
                            per.enter_context(events.within_hook_budget("stop"))
                        recorder.enabled = item.get("enabled", True)
                        raised = None
                        try:
                            recorder.record(item["kind"], **item["fields"])
                        except Exception as exc:
                            raised = [
                                type(exc).__name__,
                                norm(str(exc)),
                                getattr(exc, "sqlite_errorcode", None),
                            ]
                        description = dict(
                            item,
                            fields=[[k, neutral(v)] for k, v in item["fields"].items()],
                        )
                        steps.append(
                            dict(
                                call=description,
                                clock=log.stamps(),
                                log=list(logs),
                                mono=mono,
                                pauses=pauses,
                                raised=raised,
                                inside=store._conn.execute(
                                    "SELECT max(seq) FROM log"
                                ).fetchone()[0],
                                outside=log_rows(file, before),
                                timeout=store._conn.execute(
                                    "PRAGMA busy_timeout"
                                ).fetchone()[0],
                            )
                        )
                if name == "joined_rollback":
                    raise Undo
        except Undo:
            pass
        head = store._keyring.read_head()
        return {
            "name": name,
            "keyless_refusal": refusal,
            "steps": steps,
            "rows": log_rows(file, before),
            "head": None if head is None else [head.seq, head.mac],
        }


def generate_posix() -> dict[str, bytes]:
    with world():
        rows = [
            contextvars.Context().run(run, name, calls)
            for name, calls in groups().items()
        ]
    return {"recorder_store_posix.jsonl": jsonl([{"count": len(rows)}, *rows])}


def generate_python_posix() -> dict[str, bytes]:
    """Unhashable wording changes in 3.14; descending NaN runs differ before 3.13."""
    with world():
        rows = [
            contextvars.Context().run(
                run, "unhashable", [call(fields={"actor": [[["unhashable"], 2]]})]
            ),
            contextvars.Context().run(
                run,
                "nan_order_python",
                [
                    sort_case(63, "descending", "actor"),
                    sort_case(129, "descending", "other"),
                ],
            ),
        ]
    return {"recorder_store_python_posix.jsonl": jsonl([{"count": len(rows)}, *rows])}
