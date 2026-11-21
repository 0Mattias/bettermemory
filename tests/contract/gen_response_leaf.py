"""Fixtures: three leaves of the response layer (``response_leaf.jsonl``,
not gated).

The native build reads it in
``native/crates/bettermemory-core/src/response_links.rs``,
``response_memo.rs`` and ``verdict_signals.rs``. This pins them to
`_response_links.candidate_link_rows`, `_response`'s per-hit drift memo
(`_clear_drift_memo`, `_recall_drift`, `_remember_drift`,
`_DRIFT_MEMO_CAP`, `_TIMED_OUT_SECONDS`) and `verify`'s verdict ladder
(`_commit_leg_escalates`, `verdict_from_signals`,
`compute_staleness_verdict` and the three verdict strings).

One JSON object per line, keys sorted, ASCII. The first line is the
header ``{"k": "header", "count": n, "verdicts": [...], "cap": ...,
"seconds": ...}``. Every other line has ``k``:

- ``"links"``: ``m`` the snapshot, each ``[id, [[type, target, note],
  ...]]``; ``ids`` the wanted ids; ``o`` the answer, each ``[id,
  outbound, inbound]`` in the dict's order, each edge ``[type, other,
  note]``. Ids are ``I<n>``, spelled in full by `_uid`.
- ``"links_sweep"``: ``n`` cases from the rule `_sweep_case` (a 64-bit
  LCG from ``seed``), each answer spelled by `_spell`, the lines joined
  by a newline; ``sha`` the SHA-256 of that text.
- ``"memo"``: a script over one memo with ``cap`` (the patched
  `_DRIFT_MEMO_CAP`). Each step of ``s`` is ``["r", key, now]``
  (`_recall_drift`), ``["m", resolved, timed_out, now]``
  (`_remember_drift`, each a list of ``[key, value]``), ``["mr", n,
  now]`` (`_remember_drift` of ``k0`` .. ``k<n-1>``, each its own key as
  value) or ``["c"]`` (`_clear_drift_memo`). ``now`` is what
  `time.monotonic` returns, spelled as the ``repr`` of the float (so
  ``"nan"`` for NaN) and read back by a correctly rounded parse. Each step's answer in
  ``o``: ``{"r": [hit, value]}`` for a recall, and the keys of each table
  afterwards (``mk``, ``tk``, oldest first; past ten keys
  ``{"n", "first", "last"}``).
- ``"verdict"``: ``a`` the arguments of `verdict_from_signals` (status,
  path_drift_missing, commit_drift_count, cited_missing,
  attested_absent_present), ``o`` the answer, ``e``
  `_commit_leg_escalates` of the commit count.
- ``"verdict_sweep"``: every combination of ``statuses`` and the counts
  in ``pdm``, ``cdc``, ``cm``, ``aap`` (in that nesting, the last
  innermost), the answers joined by a newline; ``n`` and ``sha``.
- ``"compute"``: `compute_staleness_verdict` over a `VerificationStatus`
  of status ``st`` (no stamp, 30 days) and the same counts in ``a``.
"""

from __future__ import annotations

import hashlib
import itertools
import types
from typing import Any
from unittest import mock

from bettermemory import _response, verify
from bettermemory._response_links import candidate_link_rows
from bettermemory.models import Memory, MemoryLink
from bettermemory.verify_status import VerificationStatus

from ._common import jsonl

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_KINDS = ("supersedes", "contradicts", "extends", "depends_on")


def _uid(n: int) -> str:
    """``I<n>`` as a ULID: ``01J`` and ``n`` in 23 base-32 digits."""
    digits = []
    for _ in range(23):
        digits.append(_CROCKFORD[n % 32])
        n //= 32
    return "01J" + "".join(reversed(digits))


def _memory(n: int, links: list[list[Any]]) -> Memory:
    """A memory ``I<n>`` with ``links``, set past `Memory`'s own link
    checks (a self link, a repeated link), which the function does not
    rely on."""
    base = Memory.model_validate(
        {
            "id": _uid(n),
            "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z",
            "scopes": ["global"],
            "confidence": "high",
            "source": "explicit-statement",
            "body": "b",
        }
    )
    return base.model_copy(
        update={
            "links": [
                MemoryLink.model_validate(
                    {"type": kind, "target_id": _uid(t), "note": note}
                )
                for kind, t, note in links
            ]
        }
    )


def _short(uid: str) -> int:
    n = 0
    for ch in uid[3:]:
        n = n * 32 + _CROCKFORD.index(ch)
    return n


def _answer(snapshot: list[list[Any]], ids: list[int]) -> list[Any]:
    memories = [_memory(n, links) for n, links in snapshot]
    rows = candidate_link_rows(memories, [_uid(i) for i in ids])
    return [
        [
            _short(mid),
            [[k, _short(o), note] for k, o, note in outbound],
            [[k, _short(o), note] for k, o, note in inbound],
        ]
        for mid, (outbound, inbound) in rows.items()
    ]


_LINK_CASES: list[tuple[list[list[Any]], list[int]]] = [
    ([], []),
    ([], [1, 2]),
    ([[1, [["extends", 2, None]]]], []),
    # Neither end wanted: skipped.
    ([[1, [["extends", 2, None]]]], [3]),
    # Outbound and inbound, sorted by type then id.
    (
        [
            [1, [["supersedes", 3, None], ["extends", 4, None], ["extends", 2, None]]],
            [5, [["contradicts", 1, None], ["depends_on", 1, "x"]]],
            [2, [["contradicts", 1, None]]],
        ],
        [1],
    ),
    # Ties keep the snapshot's order; distinct notes kept, exact
    # duplicates collapsed (also across a repeated memory).
    (
        [
            [
                1,
                [
                    ["extends", 2, "z"],
                    ["extends", 2, "a"],
                    ["extends", 2, "z"],
                    ["extends", 2, None],
                ],
            ],
            [1, [["extends", 2, "a"], ["extends", 2, "m"]]],
        ],
        [1, 2],
    ),
    # A self link is both outbound and inbound; repeated wanted ids keep
    # their first place; a wanted id with no links still has a row.
    ([[1, [["depends_on", 1, None]]], [2, []]], [3, 1, 3, 2, 1]),
    # Every kind, both ends wanted, target before source in the ids.
    (
        [[n, [[k, 9, None] for k in _KINDS]] for n in (7, 8)],
        [9, 7],
    ),
    # Empty and non-ASCII notes.
    ([[1, [["extends", 2, ""], ["extends", 2, "é\U0001f600"]]]], [2]),
]


def _lcg(state: int) -> int:
    return (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)


def _sweep_case(state: int) -> tuple[int, list[list[Any]], list[int]]:
    """One sweep case from ``state``: draws are ``state >> 33`` after each
    step. Up to four memories of ids 0..5, each up to two links (type,
    target 0..5, note None, "a" or "b"), and up to three wanted ids."""

    def draw(m: int) -> int:
        nonlocal state
        state = _lcg(state)
        return (state >> 33) % m

    snapshot: list[list[Any]] = []
    for _ in range(draw(5)):
        src = draw(6)
        links = [
            [_KINDS[draw(4)], draw(6), (None, "a", "b")[draw(3)]]
            for _ in range(draw(3))
        ]
        snapshot.append([src, links])
    ids = [draw(6) for _ in range(draw(4))]
    return state, snapshot, ids


def _spell(answer: list[Any]) -> str:
    def edges(rows: list[Any]) -> str:
        return ",".join(f"{k}/{o}/{'-' if n is None else n}" for k, o, n in rows)

    return ";".join(f"{mid}>{edges(o)}<{edges(i)}" for mid, o, i in answer)


def _links() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"k": "links", "m": snapshot, "ids": ids, "o": _answer(snapshot, ids)}
        for snapshot, ids in _LINK_CASES
    ]
    seed, n, state, lines = 2026, 400, 2026, []
    for _ in range(n):
        state, snapshot, ids = _sweep_case(state)
        lines.append(_spell(_answer(snapshot, ids)))
    sha = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    out.append({"k": "links_sweep", "seed": seed, "n": n, "sha": sha})
    return out


def _now(v: Any) -> float:
    return float("nan") if v == "nan" else float(v)


def _keys(memo: Any) -> Any:
    keys = list(memo)
    if len(keys) > 10:
        return {"n": len(keys), "first": keys[0], "last": keys[-1]}
    return keys


def _run_memo(cap: int, steps: list[list[Any]]) -> list[dict[str, Any]]:
    clock: list[float] = []
    fake = types.SimpleNamespace(monotonic=lambda: clock.pop(0))
    out = []
    with (
        mock.patch.object(_response, "time", fake),
        mock.patch.object(_response, "_DRIFT_MEMO_CAP", cap),
    ):
        _response._clear_drift_memo()
        for step in steps:
            answer: dict[str, Any] = {}
            if step[0] == "r":
                clock.append(_now(step[2]))
                hit, value = _response._recall_drift(step[1])
                answer["r"] = [hit, value]
            elif step[0] == "m":
                clock.append(_now(step[3]))
                _response._remember_drift(dict(step[1]), dict(step[2]))
            elif step[0] == "mr":
                clock.append(_now(step[2]))
                bulk: dict[Any, Any] = {f"k{i}": f"k{i}" for i in range(step[1])}
                _response._remember_drift(bulk, {})
            else:
                _response._clear_drift_memo()
            clock.clear()
            answer["mk"] = _keys(_response._DRIFT_MEMO)
            answer["tk"] = _keys(_response._DRIFT_TIMED_OUT)
            out.append(answer)
        _response._clear_drift_memo()
    return out


_MEMO_CASES: list[tuple[int, list[list[Any]]]] = [
    # A miss; a hit moves to the end; a None value is a hit.
    (
        3,
        [
            ["r", "a", 0],
            ["m", [["a", "A"], ["b", None]], [], 0],
            ["r", "a", 1],
            ["r", "b", 2],
            ["r", "z", 3],
        ],
    ),
    # Setting a held key keeps its place; eviction of the oldest.
    (
        3,
        [
            ["m", [["a", "1"], ["b", "2"], ["c", "3"]], [], 0],
            ["m", [["a", "new"]], [], 0],
            ["r", "a", 0],
            ["m", [["d", "4"], ["e", "5"]], [], 0],
            ["r", "b", 0],
            ["r", "c", 0],
        ],
    ),
    # Timed out: answered for 60 s from when it was kept, then removed.
    (
        3,
        [
            ["m", [], [["t", "T"]], 100.0],
            ["r", "t", 100.0],
            ["r", "t", 159.999],
            ["r", "t", 160.0],
            ["r", "t", 161.0],
        ],
    ),
    # A clock that went back, and NaN, remove it.
    (
        3,
        [
            ["m", [], [["t", "T"], ["u", None]], 100.0],
            ["r", "t", 99.5],
            ["r", "u", "nan"],
            ["m", [], [["v", "V"]], "nan"],
            ["r", "v", 100.0],
        ],
    ),
    # A timed-out hit keeps its place; remembering it again restamps it
    # and moves it to the end; the timed-out table is bounded too.
    (
        3,
        [
            ["m", [], [["a", "1"], ["b", "2"], ["c", "3"]], 0.0],
            ["r", "a", 30.0],
            ["m", [], [["a", "1b"]], 50.0],
            ["r", "b", 70.0],
            ["r", "a", 100.0],
            ["m", [], [["d", "4"], ["e", "5"]], 100.0],
        ],
    ),
    # The memo answers before the timed-out table; clear empties both.
    (
        3,
        [
            ["m", [["k", "memo"]], [["k", "late"]], 0.0],
            ["r", "k", 1000.0],
            ["c"],
            ["r", "k", 0.0],
        ],
    ),
    # A long decimal one step short of the 60 s bound: a parse that is
    # not correctly rounded lands a step low and answers a hit.
    (
        3,
        [
            ["m", [], [["t", "T"]], 59.999999999],
            ["r", "t", 119.99999999900001],
            ["m", [], [["u", "U"]], 0.1],
            ["r", "u", 60.1],
        ],
    ),
    # A cap of zero keeps nothing.
    (0, [["m", [["a", "1"]], [["b", "2"]], 0.0], ["r", "a", 0.0], ["r", "b", 0.0]]),
    # The real cap.
    (
        _response._DRIFT_MEMO_CAP,
        [["mr", 5002, 0.0], ["r", "k2", 0.0], ["r", "k1", 0.0], ["mr", 3, 0.0]],
    ),
]


_CLOCK_AT = {"r": 2, "m": 3, "mr": 2}


def _spell_step(step: list[Any]) -> list[Any]:
    at = _CLOCK_AT.get(step[0])
    if at is None:
        return list(step)
    return [*step[:at], repr(_now(step[at])), *step[at + 1 :]]


def _memo() -> list[dict[str, Any]]:
    return [
        {
            "k": "memo",
            "cap": cap,
            "s": [_spell_step(step) for step in steps],
            "o": _run_memo(cap, steps),
        }
        for cap, steps in _MEMO_CASES
    ]


_STATUSES = ["never", "stale", "fresh", "remote", "", "Never", "other"]
_PDM = [-1, 0, 1, 2]
_CDC: list[int | None] = [None, -1, 0, 1, 2]
_CM = [-1, 0, 1]
_AAP = [-1, 0, 1]
_BIG = (1 << 63) - 1


def _verdict(status: str, pdm: int, cdc: int | None, cm: int, aap: int) -> str:
    return verify.verdict_from_signals(
        status=status,
        path_drift_missing=pdm,
        commit_drift_count=cdc,
        cited_missing=cm,
        attested_absent_present=aap,
    )


def _verdicts() -> list[dict[str, Any]]:
    explicit: list[list[Any]] = [
        ["fresh", 0, None, 0, 0],
        ["never", 0, None, 0, 0],
        ["stale", 0, 0, 0, 0],
        ["remote", 0, None, 0, 0],
        ["never", 1, None, 0, 0],
        ["stale", 0, 1, 0, 0],
        ["fresh", 0, 1, 0, 0],
        ["fresh", 0, None, 0, 1],
        ["never", 0, None, 1, 0],
        ["fresh", 0, None, 1, 0],
        ["never", 0, -5, 0, 0],
        ["fresh", -_BIG - 1, -_BIG - 1, _BIG, -_BIG - 1],
        ["stale", _BIG, None, 0, 0],
        ["Stale", 1, None, 0, 0],
    ]
    out: list[dict[str, Any]] = [
        {
            "k": "verdict",
            "a": a,
            "o": _verdict(*a),
            "e": verify._commit_leg_escalates(a[2]),
        }
        for a in explicit
    ]
    answers = [
        _verdict(*a) for a in itertools.product(_STATUSES, _PDM, _CDC, _CM, _AAP)
    ]
    out.append(
        {
            "k": "verdict_sweep",
            "statuses": _STATUSES,
            "pdm": _PDM,
            "cdc": _CDC,
            "cm": _CM,
            "aap": _AAP,
            "n": len(answers),
            "sha": hashlib.sha256("\n".join(answers).encode("ascii")).hexdigest(),
        }
    )
    for st in ("never", "stale", "fresh", "remote"):
        rows: list[list[Any]] = [
            [0, None, 0, 0],
            [1, None, 0, 0],
            [0, 3, 0, 0],
            [0, None, 2, 0],
        ]
        for a in rows:
            vs = VerificationStatus(
                status=st,
                last_verified_at=None,
                age_days=None,
                recommendation=None,
                stale_after_days=30,
            )
            o = verify.compute_staleness_verdict(
                verification=vs,
                path_drift_missing=a[0],
                commit_drift_count=a[1],
                cited_missing=a[2],
                attested_absent_present=a[3],
            )
            out.append({"k": "compute", "st": st, "a": a, "o": o})
    return out


def generate() -> dict[str, bytes]:
    """``response_leaf.jsonl``: not gated."""
    cases = [*_links(), *_memo(), *_verdicts()]
    header = {
        "k": "header",
        "count": len(cases),
        "verdicts": [
            verify._VERDICT_FRESH,
            verify._VERDICT_RECOMMENDED,
            verify._VERDICT_REQUIRED,
        ],
        "raise": sorted(verify._VERDICT_RAISE_STATUSES),
        "escalates": verify._COMMIT_DRIFT_ESCALATES,
        "cap": _response._DRIFT_MEMO_CAP,
        "seconds": _response._TIMED_OUT_SECONDS,
    }
    return {"response_leaf.jsonl": jsonl([header, *cases])}
