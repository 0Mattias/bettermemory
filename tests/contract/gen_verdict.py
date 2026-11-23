"""Fixtures: one staleness verdict per memory (``verdict.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/verdict.rs``. This pins
`verdict.resolve_verdicts`, `memoised_verdicts` and `memory_verdict`
(through them `_open_legs` and `_verdict`) with the module's bindings of
`CommitLegs` and `detect_path_drift` scripted and logged, and its binding
of `compute_verification_status` logged around the real function. Every
patch is restored before `generate` returns.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "count": n}``. Every other line is a case:

- ``f``: ``r`` (`resolve_verdicts`), ``m`` (`memoised_verdicts`) or ``o``
  (`memory_verdict` of the one memory given);
- ``now`` and ``s``: the call's ``now`` (ISO text) and ``stale_after_days``;
- ``mem``: the memories, each ``id``, ``body``, ``lva`` (ISO text or null),
  ``wr`` (the origin's worktree root, or null with no origin, or the
  absent key with an origin whose root is None), ``vp``/``vap`` (legacy
  path lists) and ``vt``/``vat`` (target maps, or null);
- ``legs``: ``{"active": bool, "look": {id: [answered, resolved]}}``,
  ``resolved`` null, ``{"d": [count, checked, drifted, basis]}``,
  ``{"u": unread value}`` or ``{"c": [why, rule]}``;
- ``anch``: null (no anchoring) or ``{id: anchor text or null}``;
- ``rep``: ``{id: {"c", "m", "cam", "pbaa"}}``, the report the path drift
  read answers for that id's body (the body is the id);
- ``o``: ``{"log": calls in order, "out": verdicts}`` or ``{"log",
  "raised": class}``. A verdict is ``{"id", "v", "pd", "cd", "sv", "u",
  "c"}`` or, unresolved, ``{"id", "v", "pd", "unresolved": true}``.

A recommendation in ``v`` or ``cd`` is written as its first 24
characters and its length (`verify_status.jsonl` and the status leaves pin
the texts whole).

Nothing here depends on the Python release or the platform.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from bettermemory import verdict as _verdict
from bettermemory.commit_legs import CommitLegUnchecked
from bettermemory.origin_history import HistoryUnread
from bettermemory.verify import ResolvedCommitDrift
from bettermemory.verify_anchoring import PathDriftReport

from ._common import jsonl

#: The module, read as `Any`: its bindings are patched here.
V: Any = _verdict

NOW = "2026-03-01T00:00:00+00:00"


def _memory(m: dict[str, Any]) -> Any:
    if "wr" not in m:
        origin: Any = SimpleNamespace(worktree_root=None)
    elif m["wr"] is None:
        origin = None
    else:
        origin = SimpleNamespace(worktree_root=m["wr"])
    lva = m.get("lva")
    return SimpleNamespace(
        id=m["id"],
        body=m["id"],
        origin=origin,
        last_verified_at=None if lva is None else datetime.fromisoformat(lva),
        verified_paths=m.get("vp", []),
        verified_absent_paths=m.get("vap", []),
        verified_path_targets=m.get("vt"),
        verified_absent_path_targets=m.get("vat"),
    )


def _resolved(spec: Any) -> Any:
    if spec is None:
        return None
    if "d" in spec:
        count, checked, drifted, basis = spec["d"]
        return ResolvedCommitDrift(count, checked, tuple(drifted), basis)
    if "u" in spec:
        return HistoryUnread(spec["u"])
    why, rule = spec["c"]
    return CommitLegUnchecked(why, rule)


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def _paths(p: Any) -> list[Any]:
    return [type(p).__name__, list(p)]


def _run(case: dict[str, Any]) -> dict[str, Any]:
    log: list[Any] = []
    legs_spec = case["legs"]
    rep = case["rep"]

    class Legs:
        def __init__(self, caller_origin: Any, admission: Any, anchoring: Any) -> None:
            log.append(["legs"])
            self.active = legs_spec["active"]

        def prime(self, ids: list[str]) -> None:
            log.append(["prime_legs", list(ids)])

        def lookup(self, memory: Any, lva: Any, *, resolve: bool) -> Any:
            log.append(["lookup", memory.id, _iso(lva), resolve])
            answered, spec = legs_spec["look"][memory.id]
            return answered, _resolved(spec)

        def store(self) -> None:
            log.append(["store"])

    class Anch:
        def prime(self, ids: list[str]) -> None:
            log.append(["prime_anch", list(ids)])

        def for_record(self, rid: str, *, origin_root: Any) -> Any:
            log.append(["for_record", rid, origin_root])
            return case["anch"][rid]

    def detect(
        body: str,
        *,
        verified_paths: Any,
        absent_paths: Any,
        worktree_root: Any,
        anchor: Any,
    ) -> Any:
        log.append(
            [
                "pd",
                body,
                _paths(verified_paths),
                _paths(absent_paths),
                worktree_root,
                anchor,
            ]
        )
        r = rep.get(body, {})
        return PathDriftReport(
            checked=r.get("c", []),
            missing=r.get("m", []),
            claim_anchored_missing=r.get("cam", []),
            present_but_attested_absent=r.get("pbaa", []),
        )

    real_cvs = V.compute_verification_status

    def cvs(lva: Any, *, now: Any, stale_after_days: int) -> Any:
        log.append(["cvs", _iso(lva), _iso(now), stale_after_days])
        return real_cvs(lva, now=now, stale_after_days=stale_after_days)

    saved = (V.CommitLegs, V.detect_path_drift, V.compute_verification_status)
    V.CommitLegs, V.detect_path_drift, V.compute_verification_status = (
        Legs,
        detect,
        cvs,
    )
    try:
        mems = [_memory(m) for m in case["mem"]]
        kw: dict[str, Any] = {
            "caller_origin": None,
            "now": datetime.fromisoformat(case["now"]),
            "stale_after_days": case["s"],
            "anchoring": None if case["anch"] is None else Anch(),
        }
        try:
            if case["f"] == "r":
                out: Any = V.resolve_verdicts(mems, **kw)
            elif case["f"] == "m":
                out = V.memoised_verdicts(mems, **kw)
            else:
                out = [V.memory_verdict(mems[0], **kw)]
        except (TypeError, AssertionError) as exc:
            return {"log": log, "raised": type(exc).__name__}
    finally:
        V.CommitLegs, V.detect_path_drift, V.compute_verification_status = saved
    return {"log": log, "out": [_out(v) for v in out]}


def _short(d: dict[str, Any]) -> dict[str, Any]:
    """A dict with its recommendation cut to its first 24 characters and
    its length: `verify_status.jsonl` pins the texts whole."""
    rec = d.get("recommendation")
    if isinstance(rec, str):
        d = {**d, "recommendation": [rec[:24], len(rec)]}
    return d


def _out(v: Any) -> dict[str, Any]:
    pd = v.path_drift
    base = {
        "id": v.id,
        "v": _short(v.verification.to_dict()),
        "pd": [list(pd.checked), list(pd.missing)],
    }
    if isinstance(v, V.UnresolvedVerdict):
        return {**base, "unresolved": True}
    u = v.commit_drift_unchecked
    return {
        **base,
        "cd": None if v.commit_drift is None else _short(v.commit_drift.to_dict()),
        "sv": v.staleness_verdict,
        "u": None if v.commit_drift_unread is None else v.commit_drift_unread.value,
        "c": None if u is None else [u.why, u.rule],
    }


def _m(i: str, **kw: Any) -> dict[str, Any]:
    return {"id": i, "wr": "/w/" + i, **kw}


_FRESH = "2026-02-20T00:00:00+00:00"
_OLD = "2025-01-01T00:00:00+00:00"


def _cases() -> list[dict[str, Any]]:
    off = {"active": False, "look": {}}
    out: list[dict[str, Any]] = []

    def add(
        f: str, mem: list[Any], legs: Any = off, anch: Any = None, **kw: Any
    ) -> None:
        case = {
            "f": f,
            "now": kw.get("now", NOW),
            "s": kw.get("s", 30),
            "mem": mem,
            "legs": legs,
            "anch": anch,
            "rep": kw.get("rep", {}),
        }
        case["o"] = _run(case)
        out.append(case)

    def on(look: dict[str, Any]) -> dict[str, Any]:
        return {"active": True, "look": look}

    # No legs, no anchoring: nothing primed, no lookup.
    for f in ("r", "m", "o"):
        add(f, [_m("a"), _m("b", lva=_FRESH), _m("c", lva=_OLD)])
    add("r", [])
    add("m", [])
    # Origin shapes and the attestation legs.
    add(
        "r",
        [
            {"id": "n", "wr": None},
            {"id": "x"},
            _m("t", vp=["p1"], vap=["q1"]),
            _m("l", vt={"a": "A", "b": None}, vat={}),
            _m("k", vp=["p"], vt={"z": None}, vap=["q"], vat={"y": "Y"}),
        ],
    )
    # Anchoring without legs: primed, anchors found and not.
    anch = {"a": "home", "b": None, "n": "h2"}
    for f in ("r", "m"):
        add(f, [_m("a"), _m("b"), {"id": "n", "wr": None}], anch=anch)
    # Legs built but inactive: the anchoring is primed instead.
    add(
        "r", [_m("a", lva=_FRESH)], legs={"active": False, "look": {}}, anch={"a": None}
    )
    # Every resolved kind, resolving.
    kinds: list[tuple[str, Any]] = [
        ("none", None),
        ("d0", {"d": [0, 0, [], "author-date"]}),
        ("d3", {"d": [3, 0, [], "author-date"]}),
        ("d1", {"d": [1, 0, [], "reachability"]}),
        ("dc", {"d": [2, 4, ["src/a.py", "b.md"], "reachability"]}),
        ("dc0", {"d": [0, 2, [], "author-date"]}),
        ("cn", {"c": ["unconfirmed", "rule-a"]}),
        ("ch", {"c": ["no_home", ""]}),
    ]
    kinds += [("u_" + h.name.lower(), {"u": h.value}) for h in HistoryUnread]
    for f, lva in (("r", _FRESH), ("m", _OLD), ("r", None)):
        mem = [_m(k, lva=lva) for k, _ in kinds]
        look = {k: [True, spec] for k, spec in kinds}
        add(
            f,
            mem,
            legs=on(look),
            anch=None if f == "r" else {k: None for k, _ in kinds},
        )
    # Path drift signals against each kind.
    rep = {
        "pm": {"c": ["a"], "m": ["a"]},
        "pc": {"c": ["a"], "cam": ["a"]},
        "pb": {"c": ["a"], "pbaa": ["a"]},
        "pk": {"c": ["a", "b"]},
    }
    for _, spec in kinds[:3] + kinds[6:7] + kinds[-1:]:
        mem = [_m(i, lva=_FRESH) for i in rep]
        add("r", mem, legs=on({i: [True, spec] for i in rep}), rep=rep)
    # The memo: answered and not.
    look = {
        "a": [True, None],
        "b": [False, None],
        "c": [True, {"d": [5, 0, [], "author-date"]}],
    }
    mem3 = [_m("a", lva=_FRESH), _m("b", lva=_OLD), _m("c", lva=_FRESH)]
    add("m", mem3, legs=on(look), anch={"a": "x", "b": "y", "c": None})
    add("m", mem3, legs=on({k: [False, None] for k in "abc"}))
    add("m", mem3, legs=on({k: [0, {"u": "timeout"}] for k in "abc"}))
    # Resolving where the legs do not answer: AssertionError, no store.
    add("r", mem3, legs=on(look))
    add("o", mem3[1:2], legs=on(look))
    # A naive now against an aware stamp, on the second row.
    for f in ("r", "m"):
        add(
            f,
            [_m("a"), _m("b", lva=_FRESH), _m("c")],
            legs=on({"a": [True, None]}),
            now="2026-03-01T00:00:00",
        )
    add("r", [_m("a", lva="2026-02-20T00:00:00")], now="2026-03-01T00:00:00")
    add("r", [_m("a", lva="2026-02-20T00:00:00")])
    # Duplicate ids, stale_after_days bounds.
    add(
        "r",
        [_m("a", lva=_FRESH), _m("a", lva=_OLD)],
        legs=on({"a": [True, None]}),
        anch={"a": "z"},
    )
    for s in (-5, 0, 1, 9, 2**40):
        add("r", [_m("a", lva=_FRESH), _m("b", lva=_OLD)], s=s)
    add(
        "o",
        [_m("a", lva="2026-02-28T23:59:59-05:00")],
        legs=on({"a": [True, {"c": ["no_history", "r"]}]}),
    )
    return out


def generate() -> dict[str, bytes]:
    """``verdict.jsonl``: not gated."""
    before = (V.CommitLegs, V.detect_path_drift, V.compute_verification_status)
    cases = _cases()
    assert before == (V.CommitLegs, V.detect_path_drift, V.compute_verification_status)
    return {"verdict.jsonl": jsonl([{"k": "header", "count": len(cases)}, *cases])}
