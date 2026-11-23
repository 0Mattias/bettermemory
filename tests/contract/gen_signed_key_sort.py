"""The signed payload bytes and first failing comparison of sorted mapping keys.

A new store for each event fixes the previous MAC. Inputs retain their
insertion order as a list of keys; the value of each key is its position.
CPython's run detection changed in 3.13. Emit the active interpreter's
actual order and diagnostic in as-found runs.
"""

from __future__ import annotations

import hashlib
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

from bettermemory.store import Store

from ._common import jsonl
from ._fixed import Ticker, fixed
from ._sandbox import world
from .gen_store_open import _engine, _made
from .gen_store_record_event import LOG0, WALL0
from .gen_store_write import decode


def cases() -> list[tuple[str, list[Any]]]:
    result: list[tuple[str, list[Any]]] = []
    for count in (63, 64, 65, 80, 127, 128, 257):
        for placement in ("first", "middle", "last", "shuffled"):
            keys: list[Any] = list(range(count - 1))
            if placement == "shuffled":
                random.Random(497).shuffle(keys)
            pos = {
                "first": 0,
                "middle": len(keys) // 2,
                "last": len(keys),
                "shuffled": 17,
            }
            keys.insert(pos[placement], {"$float": "nan"})
            result.append((f"nan-{count}-{placement}", keys))
    result.append(
        ("numeric", [True, 3.5, -9, False, 2**53, 2**53 + 1, float(2**53 + 2)])
    )
    result.append(("string", ["z", "Z", "", "π"]))
    # Full merges and binary insertion alone can compare different pairs.
    for seed in (0, 4, 7, 14):
        rng = random.Random(seed)
        count = rng.choice((64, 65, 80, 127, 128, 257))
        keys = list(range(10, count + 10))
        rng.shuffle(keys)
        for i in range(1, count, 2):
            if rng.randrange(2):
                keys[i] += 0.25
        left, right = rng.sample(range(count // 2, count), 2)
        keys[left], keys[right] = "late", None
        result.append((f"mixed-{seed}", keys))
    # Descending runs with equality at a NaN are split differently by 3.11/12.
    # Observe both the resulting MAC and the first incompatible comparison.
    for count, nan_at, text_at in ((64, 1, 7), (65, 5, 33)):
        keys = [k + 0.5 if i % 3 == 0 else k for i, k in enumerate(range(count, 0, -1))]
        keys[nan_at] = {"$float": "nan"}
        result.append((f"descending-{count}", keys.copy()))
        keys[text_at] = "late"
        result.append((f"descending-error-{count}", keys))
    return result


def _run(
    directory: Path, keys: list[Any], use_actor: bool
) -> tuple[dict[str, Any], str]:
    _made("created", {}, directory)
    metadata = {"metadata": {decode(key): i for i, key in enumerate(keys)}}
    kwargs = {"actor": metadata} if use_actor else metadata
    with (
        _engine(),
        Store.open(directory / "memory.sqlite", keys_dir=directory / "keys") as store,
    ):
        previous = store._conn.execute("SELECT mac FROM log WHERE seq=1").fetchone()[0]
        with fixed(log=Ticker(LOG0, timedelta(0)), wall=Ticker(WALL0, timedelta(0))):
            try:
                row = store.record_event(
                    "search", session="S", best_effort=False, **kwargs
                )
            except TypeError as error:
                assert (
                    store._conn.execute("SELECT count(*) FROM log").fetchone()[0] == 1
                )
                return {"error": str(error)}, previous
        assert row is not None and row.seq == 2
        payload, mac = store._conn.execute(
            "SELECT payload,mac FROM log WHERE seq=2"
        ).fetchone()
        assert (row.payload, row.mac) == (payload, mac)
        return {
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "mac": mac,
        }, previous


def generate() -> dict[str, bytes]:
    rows = []
    with world() as env:
        for i, (name, keys) in enumerate(cases()):
            actor = i % 2 == 0
            out, previous = _run(env.root / str(i), keys, actor)
            rows.append({"case": name, "keys": keys, "actor": actor, "out": out})
    header = {
        "fixture": "signed_key_sort_posix_python",
        "previous_mac": previous,
        "count": len(rows),
    }
    return {"signed_key_sort_posix_python.jsonl": jsonl([header, *rows])}
