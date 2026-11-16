"""Fixture: the record writers ``mark_verified`` and
``record_corroboration`` (``store_records_c.jsonl``).

The native build ports them as functions on the writable store
(``native/``, `store::writers`, part B2) and is held to this file. A file
of its own, not more cases in ``store_records_b.jsonl``: each file keeps
its own size budget, and part B1's file stays byte for byte as it was.
The cases run as ``store_records_b.jsonl``'s do (`gen_store_records_b.
run_case`): each step records the stamps each clock handed out, the ids
minted and the order of every read (``log``, ``wall``, ``mint``), then the
returned record's canonical payload text (null for a corroboration that
writes nothing) or the refusal's class and text, the rows appended, the
state's digest, the head's text and the FTS5 index. A case marked
``same_tick`` reads the same wall-clock start in every step.

Gates, the same as ``store_tx.jsonl``'s. Besides the product's wording,
pydantic's first line (pinned by the pydantic-core gate) and ``repr`` of
plain text, the file records these texts worded by the interpreter:

- ``'int' object is not iterable`` (``list()`` and ``dict()`` of an int);
- ``'int' object has no attribute 'strip'``;
- ``dictionary update sequence element #0 has length 1; 2 is required``;
- ``cannot convert dictionary update sequence element #0 to a sequence``;
- ``unhashable type: 'list'``.

They are not stable across versions: Python 3.15 words ``dict([[["k"],
"v"]])`` as "cannot use 'list' as a dict key (unhashable type: 'list')"
(the unhashable wording is believed to have changed in 3.14) and
``dict([5])`` as "object is not iterable". The file is safe today only
because the Unicode gate (Unicode 15.1) confines its generation and its
golden test to Python 3.13; under any other interpreter the test skips it.
No value is nested deep enough to reach a recursion limit, so no case is
placed by Python version beyond that.

Three things JSON with ``sort_keys`` cannot carry are spelled as tags
(`gen_store_write.decode`): a dict whose order matters as
``{"$dict": [[k, v], ...]}``, a float as ``{"$float": repr}`` and an int
past 2**53 as ``{"$int": digits}`` (the native reader would otherwise
round them). ``check_expected`` is recorded as given and both sides read
its truthiness. A lone surrogate cannot be held: the native reader parses
each line with serde_json, which refuses one, so no case passes one.
"""

from __future__ import annotations

import json
from typing import Any

from bettermemory.log import canonical_payload
from bettermemory.models import record_payload
from bettermemory.store import Store

from ._sandbox import world
from .gen_log import STORE_ID
from .gen_store_open import ENGINE, PRAGMAS
from .gen_store_records_b import put, run_case, tomb
from .gen_store_write import decode, ulid

T1 = "2001-02-03T04:05:07.000000Z"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def exact(value: Any) -> Any:
    """``value`` with each float as ``{"$float": repr}`` and each int past
    2**53 as ``{"$int": digits}``, so the native reader takes them exactly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return {"$float": repr(value)}
    if isinstance(value, int) and abs(value) > 2**53:
        return {"$int": str(value)}
    if isinstance(value, list):
        return [exact(v) for v in value]
    if isinstance(value, dict):
        return {k: exact(v) for k, v in value.items()}
    return value


def verify(n: int | str, session: str | None = "s3", **kw: Any) -> dict[str, Any]:
    return {
        "call": "verify",
        "id": n if isinstance(n, str) else ulid(n),
        "kw": exact(kw),
        "session": session,
    }


def corr(n: int | str, session: str | None = "s4") -> dict[str, Any]:
    return {
        "call": "corroborate",
        "id": n if isinstance(n, str) else ulid(n),
        "session": session,
    }


def dt(text: str) -> dict[str, str]:
    return {"$datetime": text}


def dict_of(*keys: str, value: Any = None) -> dict[str, Any]:
    """A dict in this key order, as the fixture keeps it (``$dict``)."""
    return {"$dict": [[k, value] for k in keys]}


def paths(k: int, prefix: str = "p") -> list[str]:
    return [f"{prefix}{i}" for i in range(k)]


def cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "verify_arguments",
            "steps": [
                put(1, claims=["old claim"], verified_head="b" * 40),
                verify(1),
                verify(
                    1,
                    verified_paths=["a.py"],
                    verified_commits=["c1"],
                    verified_versions=["1.0"],
                    verified_absent_paths=["gone.py"],
                    claims=["new claim"],
                    verified_head="  " + HEAD.upper() + "\n",
                ),
                verify(1, verified_head="c" * 64, verified_paths={"$tuple": ["t.py"]}),
                verify(
                    1, verified_paths=[], claims=[], verified_commits="ab", session=None
                ),
            ],
        },
        {
            "name": "verify_refused",
            "steps": [
                put(1),
                verify(1, verified_head="not a hash"),
                verify(1, verified_head="a" * 39),
                verify(1, verified_head=5),
                verify(1, verified_head=""),
                verify(1, verified_head="É" * 40),
                verify(9),
                verify("not-a-ulid"),
                verify(1, verified_paths=5),
                verify(1, claims=7),
                verify(1, claims=[1]),
                verify(1, verified_commits=[None]),
                verify(1, verified_path_targets=5),
                verify(1, verified_path_targets=[["a"]]),
                verify(1, verified_path_targets=[5]),
                verify(1, verified_path_targets=[[["k"], "v"]]),
                verify(1, verified_path_targets="ab"),
                verify(1, verified_path_targets=[[1, "v"], [True, "w"]]),
                verify(1, verified_path_targets=[[1, "v"], [1.0, "w"]]),
                verify(1, verified_path_targets=[[True, "v"], [1, "w"], [2, "x"]]),
                verify(
                    1,
                    verified_path_targets=[
                        [0, "v"],
                        [0.0, "w"],
                        [-0.0, "x"],
                        [False, "y"],
                    ],
                ),
                verify(
                    1, verified_path_targets=[[2**53 + 1, "v"], [float(2**53), "w"]]
                ),
                verify(1, verified_path_targets=[[2**70, "v"], [float(2**70), "w"]]),
                verify(
                    1,
                    verified_absent_path_targets=[
                        [{"$tuple": [1, "a"]}, "v"],
                        [{"$tuple": [True, "a"]}, "w"],
                    ],
                ),
                verify(1, check_expected="x", expected_updated=dt(T1)),
                verify(
                    1,
                    check_expected=0,
                    expected_updated=dt(T1),
                    verified_paths=dict_of("x/y0", "b.py1"),
                    verified_commits=dict_of("z", "a"),
                    verified_versions=dict_of("x/y0", "b.py1"),
                    verified_absent_paths=dict_of("q", "c"),
                    claims=dict_of("second", "first"),
                    verified_path_targets=dict_of("x/y0", "b.py1", value="t"),
                    verified_absent_path_targets=dict_of("q", "c"),
                ),
                tomb(1),
                verify(1),
            ],
        },
        {
            "name": "check_expected",
            "steps": [
                put(1, last_verified_at=T1),
                verify(1, check_expected=True, expected_last_verified_at=dt(T1)),
                verify(1, check_expected=True, expected_last_verified_at=dt(T1)),
                put(2),
                verify(2, check_expected=True),
                verify(
                    2,
                    check_expected=True,
                    expected_last_verified_at=None,
                    expected_updated=dt("2001-02-03T04:05:06+00:00"),
                ),
                put(3),
                verify(
                    3,
                    check_expected=True,
                    expected_updated=dt("2001-02-03T06:05:06+02:00"),
                ),
                verify(
                    3,
                    check_expected=True,
                    expected_updated=dt("2001-02-03T04:05:06"),
                ),
            ],
        },
        {
            "name": "target_maps",
            "steps": [
                put(1, verified_paths=["a", "b"]),
                verify(1, verified_path_targets={"a": None, "b": "t"}),
                verify(
                    1,
                    verified_paths=["a"],
                    verified_path_targets=[["a", "x"], ["z", None]],
                ),
                verify(1, verified_paths=["a", "q"]),
                verify(1, verified_paths=["q"], verified_path_targets={"r": None}),
                verify(
                    1,
                    verified_absent_paths=["g"],
                    verified_absent_path_targets={"g": None},
                ),
                verify(1, verified_absent_path_targets={"h": "x"}),
                verify(
                    1,
                    verified_paths=paths(64),
                    verified_path_targets={p: None for p in paths(64)},
                ),
                verify(1, verified_paths=paths(65)),
                verify(
                    1,
                    verified_paths=["a"],
                    verified_path_targets={p: None for p in paths(65)},
                ),
            ],
        },
        {
            "name": "same_tick",
            "same_tick": True,
            "steps": [
                put(1),
                verify(1, verified_head=HEAD),
                verify(1, verified_head=HEAD),
                corr(1),
                corr(1, "s5"),
            ],
        },
        {
            "name": "corroboration",
            "steps": [
                put(1, corroborations=2, last_corroborated=T1),
                corr(1),
                corr(1),
                corr(1, "other"),
                corr(1, None),
                corr(1, None),
                corr(9),
                corr("not-a-ulid"),
                tomb(1),
                corr(1),
            ],
        },
    ]


def _run(store: Store, call: dict[str, Any]) -> Any:
    from .gen_store_records_b import _run as run_b

    what = call["call"]
    if what == "verify":
        kw = {k: decode(v) for k, v in call["kw"].items()}
        got = store.mark_verified(call["id"], session=call["session"], **kw)
        return canonical_payload(record_payload(got))
    if what == "corroborate":
        found = store.record_corroboration(call["id"], session=call["session"])
        return None if found is None else canonical_payload(record_payload(found))
    return run_b(store, call)


def generate() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = [
        {
            "fixture": "store_records_c",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    with world():
        runs = [run_case(c, _run) for c in cases()]
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
    return {"store_records_c.jsonl": text.encode("ascii")}
