"""Fixtures: the reason a row asks for a check, the verdict with its
objection, and the trust appliers (``response_reason.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/response_reason.rs`` and
``response_trust.rs``. This pins it to `_response`'s `check_reason` (and
through it `_named` and `_rejection_note`), `_untrusted`,
`verdict_and_reason`, `_row_reason`, `_claim_at_index`,
`ResponseBuilder.apply_trust_unavailable`, `apply_trust` and
`attach_provenance`, and to `verdict.with_objection`.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "count": n}``. Every other line is a case: ``f`` names
the function, ``a`` its arguments and ``o`` the answer, or
``{"raised": class}``. Values are JSON values read as Python's: an object
a ``dict``, an array a ``list``. A dict iterated as a sequence has its
keys in sorted order, so its order is the same read either way.

- ``cr``: ``check_reason(**a)``; ``o`` the text or null.
- ``vr``: ``verdict_and_reason(a)``; ``o`` ``[verdict, reason]``.
- ``rr``: ``_row_reason(a.row, a.c, a.u)``.
- ``wo``: ``with_objection(*a)``.
- ``ci``: ``_claim_at_index(*a)``.
- ``tu``: ``apply_trust_unavailable(a.row)`` (``a.rec`` the
  recommendation when given); ``o`` the row after.
- ``at``: ``apply_trust(a.row, provenance=a.p, verified_locally_at=a.v)``
  under ``stale_after_days`` ``a.s``; ``o`` the row after.
- ``ap``: ``attach_provenance(a.rows, store=...)`` under ``a.s``, the
  store a stub: ``a.st`` null (`trust_rows` answers None), ``"raise"``
  (it raises `RuntimeError`) or an object of id to ``[label, stamp]``;
  ``o`` ``{"q": ids asked or null, "out": rows after}`` or the class
  raised with ``q`` and ``out`` beside it.
- ``sweep``: `check_reason` over the product `_sweep` names, in order:
  ``n`` the count and ``sha256`` the digest of each answer's text (or
  ``\\0`` for None) followed by a newline, UTF-8.

Nothing here depends on the Python release or the platform: no error
class read here changes between 3.11 and 3.14 (a ``dict`` sliced, which
raises `TypeError` on 3.11 and `KeyError` after, is left out).
"""

from __future__ import annotations

import copy
import hashlib
import itertools
from types import SimpleNamespace
from typing import Any

from bettermemory import _response
from bettermemory._response import (
    TRUST_UNAVAILABLE_RECOMMENDATION,
    ResponseBuilder,
    check_reason,
    verdict_and_reason,
)
from bettermemory.verdict import with_objection

from ._common import jsonl

_P = ["a.py", "b/c.md", "d", "e.txt", "f"]
_REJ1 = {
    "count_in_window": 1,
    "most_recent_ts": "2026-01-02T03:04:05Z",
    "outcome": "ignored",
}
_REJ2 = {
    "count_in_window": 4,
    "most_recent_ts": "2026-02-03T00:00:00+00:00",
    "outcome": "contradicted",
}


def _sweep() -> list[dict[str, Any]]:
    """The product: missing (0, 1, 3, 4, 5 paths), anchored, attested,
    commits, claims, leg note, remote, trust unavailable, untrusted,
    contradicted, rejections; the last varies fastest."""
    out = []
    combos: Any = itertools.product(
        [0, 1, 3, 4, 5],
        [(), ("b/c.md",), ("x", "a.py"), tuple(_P)],
        [0, 1, 4],
        [None, 0, 1, 2, True],
        [0, 1, 4],
        [None, "Leg."],
        [False, True],
        [False, True],
        [None, "Untrusted."],
        [0, 1, 4],
        [0, 1, 2],
    )
    for m, an, at, c, cl, ln, rm, tu, ut, cb, rj in combos:
        out.append(
            {
                "missing": _P[:m],
                "anchored_missing": list(an),
                "attested_absent_present": _P[:at],
                "commits": c,
                "claims_drifted": _P[:cl],
                "leg_note": ln,
                "remote": rm,
                "trust_unavailable": tu,
                "untrusted": ut,
                "contradicted_by": _P[:cb],
                "rejections": [_REJ1, _REJ2][:rj],
            }
        )
    return out


def _run(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raised": type(exc).__name__}


_CR: list[dict[str, Any]] = [
    {},
    {"missing": ["a"]},
    {"missing": ["a", "b"], "anchored_missing": ["b", "z"]},
    {"missing": _P, "anchored_missing": []},
    {"missing": _P[:4]},
    {"missing": "abcd"},
    {"missing": {"k1": 1, "k2": 2}},
    {"missing": [1, True], "anchored_missing": [1.0]},
    {"missing": [1], "anchored_missing": [True]},
    {"missing": ["a"], "anchored_missing": [["x"]]},
    {"missing": [["x"]], "anchored_missing": ["a"]},
    {"missing": [], "anchored_missing": [["x"]]},
    {"missing": 5},
    {"anchored_missing": 5},
    {"missing": [], "anchored_missing": "xy"},
    {"anchored_missing": ["a", "a"]},
    {"anchored_missing": "remote"},
    {"missing": ["a", "b"], "anchored_missing": ["b", "z", "z", "b"]},
    {"attested_absent_present": _P},
    {"attested_absent_present": 7},
    {"attested_absent_present": [None]},
    {"commits": 1},
    {"commits": 2},
    {"commits": 0},
    {"commits": True},
    {"commits": False},
    {"commits": -3},
    {"commits": 2**63 - 1},
    {"commits": 1, "claims_drifted": _P},
    {"commits": 2, "claims_drifted": "xy"},
    {"commits": 0, "claims_drifted": _P},
    {"commits": 3, "claims_drifted": 9},
    {"leg_note": "Leg."},
    {"leg_note": ""},
    {"leg_note": 5},
    {"leg_note": 5, "rejections": 4},
    {"remote": True},
    {"trust_unavailable": True},
    {"untrusted": "U."},
    {"untrusted": ""},
    {"contradicted_by": ["m1"]},
    {"contradicted_by": _P},
    {"contradicted_by": "abcde"},
    {"contradicted_by": 3},
    {"contradicted_by": True},
    {"contradicted_by": [1]},
    {"rejections": [_REJ1]},
    {"rejections": [_REJ1, _REJ2]},
    {"rejections": [{"count_in_window": True, "most_recent_ts": 12, "outcome": None}]},
    {"rejections": [{"count_in_window": 1.0, "most_recent_ts": "x", "outcome": "o"}]},
    {
        "rejections": [
            {"count_in_window": 2.5, "most_recent_ts": [1], "outcome": {"a": 1}}
        ]
    },
    {"rejections": [{"count_in_window": 1}]},
    {"rejections": [{"most_recent_ts": "x"}]},
    {"rejections": [{"count_in_window": 2, "most_recent_ts": "x"}]},
    {"rejections": ["s"]},
    {"rejections": "s"},
    {"rejections": [[1]]},
    {"rejections": 3},
    {"rejections": {"k": 1}},
]


def _rows() -> list[dict[str, Any]]:
    """Hand-built rows for `verdict_and_reason`."""
    stamp = {"status": "fresh", "recommendation": "Rec."}
    return [
        {"staleness_verdict": "fresh"},
        {"staleness_verdict": "stale"},
        {"staleness_verdict": 5},
        {},
        {"staleness_verdict": "fresh", "_contradicted_by": ["m2", "m3"]},
        {"staleness_verdict": "fresh", "_contradicted_by": "m"},
        {"staleness_verdict": "fresh", "_contradicted_by": 0},
        {"staleness_verdict": "fresh", "_contradicted_by": 4},
        {"staleness_verdict": "fresh", "recent_negative_outcomes": [_REJ1]},
        {"staleness_verdict": "fresh", "recent_negative_outcomes": []},
        {"staleness_verdict": "fresh", "trust_unavailable": True},
        {"staleness_verdict": "fresh", "trust_unavailable": 0},
        {"staleness_verdict": "fresh", "provenance": "unaccounted"},
        {
            "staleness_verdict": "fresh",
            "provenance": "unaccounted",
            "verification": stamp,
        },
        {
            "staleness_verdict": "fresh",
            "provenance": "unaccounted",
            "verification": {"status": "never"},
        },
        {
            "staleness_verdict": "fresh",
            "provenance": "unaccounted",
            "verification": {"status": None},
        },
        {
            "staleness_verdict": "fresh",
            "provenance": "synced",
            "verification": {"status": "never"},
        },
        {"staleness_verdict": "fresh", "provenance": "synced", "verification": stamp},
        {"staleness_verdict": "stale", "provenance": "local", "verification": {}},
        {"staleness_verdict": "fresh", "verification": "x"},
        {"staleness_verdict": "fresh", "verification": ""},
        {"staleness_verdict": "fresh", "verification": [1]},
        {"staleness_verdict": "stale", "path_drift": {"missing": ["p"]}},
        {
            "staleness_verdict": "stale",
            "path_drift": {
                "missing": ["p", "q"],
                "claim_anchored_missing": ["q", "r"],
                "present_but_attested_absent": ["s"],
            },
        },
        {
            "staleness_verdict": "stale",
            "path_drift": {"claim_anchored_missing": ["p", "p"]},
        },
        {"staleness_verdict": "stale", "path_drift": "x"},
        {"staleness_verdict": "stale", "path_drift": 0},
        {"staleness_verdict": "stale", "path_drift": {"missing": None}},
        {"staleness_verdict": "stale", "commit_drift": {"commits_since_verify": 3}},
        {
            "staleness_verdict": "stale",
            "commit_drift": {
                "commits_since_verify": 1,
                "claim_drift": {"drifted": ["c1"]},
            },
        },
        {"staleness_verdict": "stale", "commit_drift": {"commits_since_verify": True}},
        {"staleness_verdict": "stale", "commit_drift": {"commits_since_verify": 2.0}},
        {"staleness_verdict": "stale", "commit_drift": {"commits_since_verify": "2"}},
        {"staleness_verdict": "stale", "commit_drift": {"claim_drift": "x"}},
        {
            "staleness_verdict": "stale",
            "commit_drift": {"claim_drift": {"drifted": None}},
        },
        {"staleness_verdict": "stale", "commit_drift": {}, "commit_drift_count": 5},
        {"staleness_verdict": "stale", "commit_drift": [], "commit_drift_count": 5},
        {
            "staleness_verdict": "stale",
            "commit_drift_count": 2,
            "claim_drift": {"drifted": "ab"},
        },
        {"staleness_verdict": "stale", "commit_drift_count": 2, "claim_drift": 1},
        {"staleness_verdict": "stale", "commit_drift_count": False},
        {
            "staleness_verdict": "stale",
            "commit_drift_unread": True,
            "verification": stamp,
        },
        {
            "staleness_verdict": "stale",
            "commit_drift_unchecked": 1,
            "verification": stamp,
        },
        {"staleness_verdict": "stale", "commit_drift_unread": True},
        {
            "staleness_verdict": "stale",
            "commit_drift_unread": True,
            "verification": {"recommendation": 7},
        },
        {
            "staleness_verdict": "stale",
            "commit_drift_unread": True,
            "verification": {"status": "remote", "recommendation": "R."},
        },
        {
            "staleness_verdict": "stale",
            "commit_drift_unchecked": True,
            "verification": {"status": "remote"},
        },
        {
            "staleness_verdict": "stale",
            "commit_drift_unchecked": True,
            "trust_unavailable": True,
            "verification": stamp,
        },
        {
            "staleness_verdict": "spot_check_required",
            "commit_drift_unread": True,
            "commit_drift_unchecked": True,
            "trust_unavailable": True,
        },
        {
            "staleness_verdict": "stale",
            "provenance": "synced",
            "verification": {"status": "never"},
            "_contradicted_by": ["m9"],
            "recent_negative_outcomes": [_REJ2],
            "trust_unavailable": True,
            "path_drift": {"missing": _P},
            "commit_drift_count": 7,
            "claim_drift": {"drifted": _P},
        },
        {"staleness_verdict": "stale", "recent_negative_outcomes": "s"},
        {"staleness_verdict": "stale", "_contradicted_by": [1]},
    ]


def _trust_rows() -> list[dict[str, Any]]:
    return [
        {},
        {"last_verified_at": "2026-01-01T00:00:00Z"},
        {
            "last_verified_at": "2026-01-01T00:00:00Z",
            "verification": {"status": "fresh"},
        },
        {"last_verified_at": "", "verification": {"status": "fresh"}},
        {"last_verified_at": 5, "verification": {"status": "fresh"}},
        {"last_verified_at": "x", "verification": [1], "staleness_verdict": "fresh"},
        {"last_verified_at": "not a stamp", "staleness_verdict": "fresh"},
        {"last_verified_at": "2026-01-01T00:00:00", "staleness_verdict": "fresh"},
        {"last_verified_at": "2026-01-01T05:00:00+05:00", "provenance": "local"},
    ]


class _Store:
    def __init__(self, st: Any) -> None:
        self.st = st
        self.asked: list[str] | None = None

    def trust_rows(self, ids: list[str]) -> Any:
        self.asked = list(ids)
        if self.st == "raise":
            raise RuntimeError("store")
        if self.st is None:
            return None
        return {
            k: SimpleNamespace(provenance=v[0], verified_locally_at=v[1])
            for k, v in self.st.items()
        }


def _ap(rows: list[Any], st: Any, s: int) -> dict[str, Any]:
    out = copy.deepcopy(rows)
    store = _Store(st)
    got = _run(
        lambda: ResponseBuilder(stale_after_days=s).attach_provenance(out, store=store)
    )
    answer = got if isinstance(got, dict) else {}
    answer.update({"q": store.asked, "out": out})
    return answer


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for a in _CR:
        cases.append({"f": "cr", "a": a, "o": _run(lambda a=a: check_reason(**a))})
    for row in _rows():
        cases.append(
            {
                "f": "vr",
                "a": row,
                "o": _run(lambda row=row: list(verdict_and_reason(row))),
            }
        )
    rr: list[Any] = [
        ({}, (), None),
        ({}, ["m"], "U."),
        ({"verification": {"status": "never"}}, "xy", None),
        ({"path_drift": {"missing": ["p"]}}, [], "U."),
        ({"commit_drift_unread": True}, [], None),
    ]
    for row, c, u in rr:
        cases.append(
            {
                "f": "rr",
                "a": {"row": row, "c": list(c) if isinstance(c, tuple) else c, "u": u},
                "o": _run(lambda row=row, c=c, u=u: _response._row_reason(row, c, u)),
            }
        )
    for v, o in itertools.product(
        ["fresh", "stale", "spot_check_required", 1, None], [False, True]
    ):
        cases.append({"f": "wo", "a": [v, o], "o": with_objection(v, o)})  # type: ignore[arg-type]
    for ev, i in itertools.product(
        [
            {},
            {"claim_excerpts": None},
            {"claim_excerpts": "abc"},
            {"claim_excerpts": ["x", 2, "z"]},
        ],
        [0, 1, 2, 3, 4, -1, -2, -3, -4, 2**62],
    ):
        cases.append(
            {
                "f": "ci",
                "a": [ev, i],
                "o": _run(lambda ev=ev, i=i: _response._claim_at_index(ev, i)),
            }
        )
    for row in _trust_rows():
        for rec in (None, "Other."):
            got = copy.deepcopy(row)
            args: dict[str, Any] = {"row": row}
            if rec is None:
                ResponseBuilder(stale_after_days=1).apply_trust_unavailable(got)
            else:
                args["rec"] = rec
                ResponseBuilder(stale_after_days=1).apply_trust_unavailable(
                    got, recommendation=rec
                )
            cases.append({"f": "tu", "a": args, "o": got})
        for p, v, s in [
            (None, None, 30),
            ("local", None, 30),
            ("synced", None, 30),
            ("synced", None, -5),
            ("synced", None, 0),
            ("synced", "2026-01-01T00:00:00Z", 30),
            ("unaccounted", None, 30),
        ]:
            got = copy.deepcopy(row)
            ResponseBuilder(stale_after_days=s).apply_trust(
                got, provenance=p, verified_locally_at=v
            )
            cases.append(
                {"f": "at", "a": {"row": row, "p": p, "v": v, "s": s}, "o": got}
            )
    stamped = {
        "id": "m1",
        "last_verified_at": "2026-03-04T05:06:07Z",
        "verification": {},
    }
    aps: list[Any] = [
        ([], None),
        ([{"id": 5}, {}], None),
        ([stamped, {"id": "m2"}, {"id": 3}], None),
        ([stamped, {"id": "m2"}], "raise"),
        (
            [stamped, {"id": "m2"}, {"id": "m3"}],
            {"m1": ["synced", None], "m2": ["local", None]},
        ),
        ([stamped, {"id": "m1"}], {"m1": ["synced", "2026-01-01T00:00:00Z"]}),
        ([stamped, {"id": ""}, {}], {"": ["unaccounted", None]}),
        ([stamped, {"id": None}], {"m1": ["synced", None]}),
        ([stamped, {"id": ["x"]}, {"id": "m1"}], {"m1": ["synced", None]}),
        ([stamped, {"id": {"k": 1}}], {}),
    ]
    for rows, st in aps:
        cases.append(
            {"f": "ap", "a": {"rows": rows, "st": st, "s": 30}, "o": _ap(rows, st, 30)}
        )
    digest = hashlib.sha256()
    sweep = _sweep()
    for a in sweep:
        r = check_reason(**a)
        digest.update(("\0" if r is None else r).encode() + b"\n")
    cases.append({"f": "sweep", "n": len(sweep), "sha256": digest.hexdigest()})
    return cases


def generate() -> dict[str, bytes]:
    """``response_reason.jsonl``: not gated."""
    assert TRUST_UNAVAILABLE_RECOMMENDATION.startswith("This machine")
    cases = _cases()
    return {
        "response_reason.jsonl": jsonl([{"k": "header", "count": len(cases)}, *cases])
    }
