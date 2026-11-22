"""Fixtures: the prompt recall's hit choice and delivery
(``recall_choose.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall/choose.rs``. This pins
`recall`'s `_seen_in_log`, `_seen_in_segment` (through `_choose_hits`),
`_choose_hits` and `_deliver_recall`, with `in_force`,
`recall_admission._unless_standing` and `_contradiction_open` through
them, and `recall_result.HookResult`.

One file, not gated: no row depends on the Python release or the
platform. One JSON object per line, keys sorted, ASCII. Its first line is
the header (``{"k": "consts", ...}``: `RECALL_MAX_POINTERS`,
`SUMMARY_CHARS`, the delivered provenance labels, the seen kinds and the
count of the lines after it). Every other line is one of:

- ``{"k": "log", "c": case, "o": ids | {"err": name}}``: `_seen_in_log`
  over the case's events, ``since``, ``own``, ``ss`` (the sessions),
  ``wt`` and ``hs``, the answer sorted;
- ``{"k": "choose", "c": case, "log": calls, "o": answer}``:
  `_choose_hits` over scripted fakes, then `_deliver_recall` over its
  choice when the case says ``deliver``; every call the fakes saw in
  order, in `gen_recall_settle._plain`'s compact form.

A case's fakes: ``hits`` (``[id, snippet]`` in rank order), ``admit``
(``[delivered, suppressed pairs]``, or ``"!raise"``), ``shown`` and
``at`` (the segment's view), ``own`` (the own cut), ``joined`` (what
`joined_sessions` answers), ``ev`` (what `events_of_kinds` answers),
``wt`` (the caller's worktree root), ``links`` (``{target: [[type,
source, updated]]}``), ``held`` (what `held_ids` finds held), ``deny``
(ids the admission refuses), ``mem`` (``{id: [body, updated,
verified]}``), ``trust`` (``{id: provenance}``, or ``null`` for a store
that answers None), ``contra`` (``[[id, stamp]]``), ``standing``, ``pairs``
(pending conflict pairs), ``claim`` (the ids `claim_delivery` keeps, all
when absent) and ``remint``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import recall, recall_state, standing
from bettermemory.store import TrustRow

from ._common import jsonl
from .gen_recall_settle import _plain

T = "2026-01-01T12:00:00+00:00"
SURFACE = "prompt_recall"
KEY = ["claude-code", "H"]


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _ts(seconds: int) -> str:
    base = _dt(T).timestamp() + seconds
    return datetime.fromtimestamp(base, _dt(T).tzinfo).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(
        _dt(T).timestamp() + seconds, _dt(T).tzinfo
    ).isoformat()


class _Fakes:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.log: list[Any] = []

    # the store
    def events_of_kinds(self, kinds: Any, *, since: datetime) -> list[Any]:
        assert sorted(kinds) == sorted(recall._SEEN_KINDS)
        self.log.append(["eok", since])
        return [dict(e) for e in self.case.get("ev", [])]

    def inbound_links(self, targets: Any, types: Any) -> dict[str, Any]:
        self.log.append(["inbound", list(targets), list(types)])
        links = self.case.get("links", {})
        return {t: [tuple(x) for x in links[t]] for t in targets if t in links}

    def load_many(self, ids: Any) -> list[Any]:
        self.log.append(["load", list(ids)])
        mem = self.case.get("mem", {})
        out = []
        for rid in ids:
            if rid in mem:
                body, upd, ver = mem[rid]
                out.append(
                    SimpleNamespace(
                        id=rid,
                        body=body,
                        updated=None if upd is None else _dt(upd),
                        last_verified_at=None if ver is None else _dt(ver),
                    )
                )
        return out

    def trust_rows(self, ids: Any) -> Any:
        self.log.append(["trust", list(ids)])
        trust = self.case.get("trust", {})
        if trust is None:
            return None
        return {rid: TrustRow(trust[rid], None) for rid in ids if rid in trust}

    def contradicted_at(self, ids: Any) -> dict[str, datetime]:
        self.log.append(["contra", list(ids)])
        # ``contra`` is a list of ``[id, stamp]`` pairs so that the Rust
        # reader keeps the dict's insertion order.
        contra = self.case.get("contra", [])
        return {rid: _dt(s) for rid, s in contra if rid in ids}

    def pending_conflict_pairs(self) -> list[Any]:
        self.log.append(["pairs"])
        return [tuple(p) for p in self.case.get("pairs", [])]

    # the admission
    def prime(self, ids: Any) -> None:
        self.log.append(["prime", list(ids)])

    def admits_id(self, rid: str) -> bool:
        self.log.append(["admits", rid])
        return rid not in self.case.get("deny", [])

    # the model
    def admit_recall(self, resolution: Any, hits: Any, *, admission: str) -> Any:
        self.log.append(["admit", list(hits), admission])
        admit = self.case.get("admit")
        if admit == "!raise":
            raise ValueError("unknown surface")
        if admit is None:
            return SimpleNamespace(delivered=list(hits), suppressed=[])
        return SimpleNamespace(
            delivered=list(admit[0]), suppressed=[tuple(p) for p in admit[1]]
        )

    # the state
    def claim_delivery(
        self, key: Any, ids: Any, *, now: datetime, event_id: str
    ) -> tuple[list[str], int]:
        self.log.append(["claim", list(key), list(ids), now, event_id])
        keep = self.case.get("claim")
        return (list(ids) if keep is None else [i for i in ids if i in keep]), 4

    # the patched functions
    def held_ids(self, store: Any, ids: Any) -> set[str]:
        self.log.append(["held", list(ids)])
        return {i for i in ids if i in self.case.get("held", [])}

    def standing_ids(self, store: Any, admission: Any, ids: Any) -> list[str]:
        self.log.append(["standing", list(ids)])
        return [i for i in ids if i in self.case.get("standing", [])]

    def joined(self, events_: Any, *, harness_session: str, worktree_root: Any) -> Any:
        self.log.append(["joined", len(events_), harness_session, worktree_root])
        return set(self.case.get("joined", []))

    def mint(self) -> str:
        self.log.append(["mint"])
        return "01HZZZZZZZZZZZZZZZZZZZZZZE"

    def record(self, turn: Any, report: Any, state: Any, **kw: Any) -> dict[str, Any]:
        self.log.append(
            [
                "record",
                kw["block"],
                list(kw["shown_ids"]),
                kw["event_id"],
                kw["segment"],
            ]
        )
        return {"event_id": kw["event_id"]}

    def hold(self, reason: str, entries: Any) -> Any:
        self.log.append(["hold", reason, [dict(e) for e in entries]])
        return recall.HookResult.empty(SURFACE)


def _mem(m: Any) -> str:
    return str(m.id)


def _choose(case: dict[str, Any]) -> dict[str, Any]:
    f = _Fakes(case)
    turn = SimpleNamespace(
        store=f,
        admission=f,
        surface=SURFACE,
        key=tuple(KEY),
        caller=SimpleNamespace(worktree_root=case.get("wt")),
        now=_dt(T),
        remint=case.get("remint"),
    )
    report = SimpleNamespace(
        top_hits=[SimpleNamespace(id=h[0], snippet=h[1]) for h in case["hits"]]
    )
    view = SimpleNamespace(
        shown=set(case.get("shown", [])), started_at=_dt(case.get("at", T))
    )
    out: Any
    with (
        mock.patch.object(standing, "held_ids", f.held_ids),
        mock.patch.object(standing, "standing_ids", f.standing_ids),
        mock.patch.object(recall, "joined_sessions", f.joined),
        mock.patch.object(recall, "generate_ulid", f.mint),
        mock.patch.object(recall, "_record_delivery", f.record),
    ):
        try:
            chosen, suppressed, rc3 = recall._choose_hits(
                turn,  # type: ignore[arg-type]
                report,  # type: ignore[arg-type]
                view,  # type: ignore[arg-type]
                f,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                own_since=_dt(case.get("own", T)),
                recent=[{"n": 1}],
            )
        except (TypeError, ValueError) as exc:
            out = {"err": type(exc).__name__}
        else:
            out = {
                "chosen": [
                    [
                        hit.id,
                        m.id,
                        row.provenance,
                        None if e is None else [e.asked, e.id],
                    ]
                    for hit, m, row, e in chosen
                ],
                "held": suppressed,
                "rc3": [_mem(m) for m in rc3],
            }
            if case.get("deliver"):
                res = recall._deliver_recall(
                    turn,  # type: ignore[arg-type]
                    report,  # type: ignore[arg-type]
                    f,  # type: ignore[arg-type]
                    chosen,
                    suppressed,
                    rc3,
                    f.hold,
                )
                out["result"] = res.to_response()
    return {"k": "choose", "c": case, "log": _plain(f.log), "o": out}


def _log(case: dict[str, Any]) -> dict[str, Any]:
    try:
        seen = recall._seen_in_log(
            case["ev"],
            since=_dt(case.get("since", T)),
            own_since=_dt(case.get("own", T)),
            sessions=set(case.get("ss", ["S"])),
            worktree_root=case.get("wt"),
            harness_session=case.get("hs"),
        )
        out: Any = sorted(seen)
    except TypeError as exc:
        out = {"err": type(exc).__name__}
    return {"k": "log", "c": case, "o": out}


def _e(kind: Any, at: int, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "ts": _ts(at), **fields}


_LOG_CASES: list[dict[str, Any]] = [
    # kinds: unseen, unhashable, not a str
    {
        "ev": [
            _e("use", 5, session="S", ids=["u"]),
            _e("search", 5, session="S", returned=["a"]),
        ]
    },
    {"ev": [_e(["search"], 5, session="S", returned=["a"])]},
    {"ev": [_e(3, 5, session="S"), _e(None, 5, session="S")]},
    # stamps: missing, not parsed, not a str, naive, before and at the cut
    {
        "ev": [
            {"kind": "search", "session": "S", "returned": ["a"]},
            _e("search", 0, session="S", returned=["b"]) | {"ts": "yesterday"},
            _e("search", 0, session="S", returned=["c"]) | {"ts": 5},
            _e("search", 0, session="S", returned=["d"])
            | {"ts": "2026-01-01T12:00:01"},
            _e("search", -1, session="S", returned=["e"]),
            _e("search", 0, session="S", returned=["f"]),
        ]
    },
    # the harness stamp
    {
        "hs": "H",
        "ev": [
            _e("search", 5, harness_session="H", returned=["a"]),
            _e("search", 5, harness_session="X", session="S", returned=["b"]),
            _e("search", 5, harness_session="X", session="Q", returned=["c"]),
            _e("search", -5, harness_session="H", returned=["d"]),
            _e("search", 5, harness_session="H", session=["u"], returned=["e"]),
            _e("search", 5, harness_session="X", worktree_root="/w", returned=["f"]),
            _e("search", 5, harness_session="", session="S", returned=["g"]),
            _e("search", 5, harness_session=7, session="S", returned=["h"]),
        ],
        "wt": "/w",
    },
    {"hs": "H", "ev": [_e("search", 5, harness_session="X", session=["u"])]},
    {"ev": [_e("search", 5, harness_session="H", returned=["a"])]},
    # a stamped event exactly at the own cut is the session's
    {
        "hs": "H",
        "own": _iso(5),
        "ev": [
            _e("search", 5, harness_session="H", returned=["at"]),
            _e("search", 4, harness_session="H", returned=["before"]),
        ],
    },
    # the two halves without a stamp
    {
        "since": _iso(-100),
        "own": _iso(0),
        "wt": "/w",
        "ev": [
            _e("search", -50, session="S", returned=["own-early"]),
            _e("search", 5, session="S", returned=["own"]),
            _e("search", 5, session="", session_id="S", returned=["by-id"]),
            _e("search", -50, session="Q", worktree_root="/w", returned=["here"]),
            _e("search", -150, session="Q", worktree_root="/w", returned=["too-early"]),
            _e("search", 5, session="Q", worktree_root="/v", returned=["elsewhere"]),
            _e("search", 5, session=4, returned=["int-session"]),
        ],
    },
    # an event from another session exactly at the segment's start
    {
        "since": _iso(5),
        "own": _iso(50),
        "wt": "/w",
        "ev": [
            _e("search", 5, session="Q", worktree_root="/w", returned=["at"]),
            _e("search", 4, session="Q", worktree_root="/w", returned=["before"]),
        ],
    },
    {
        "wt": None,
        "ev": [_e("search", 5, session="Q", worktree_root=None, returned=["a"])],
    },
    {"wt": "/w", "ev": [_e("search", 5, session={"a": 1}, worktree_root="/w")]},
    {"wt": "/w", "ev": [_e("search", 5, session=[], session_id="S", returned=["a"])]},
    # show, write, update and their status
    {
        "ev": [
            _e("show", 5, session="S", id="s1", status="refused"),
            _e("write", 5, session="S", id="w1", status="refused"),
            _e("write", 5, session="S", id="w2", status=None),
            _e("write", 5, session="S", id="w3", status="committed"),
            _e("update", 5, session="S", id="u1"),
            _e("update", 5, session="S", id=["u2"]),
            _e("show", 5, session="S", returned=["r1"]),
        ]
    },
    # the id lists
    {
        "ev": [
            _e("search", 5, session="S", returned=[], memory_ids=["m1"], hit_ids=["x"]),
            _e("list", 5, session="S", hit_ids=["h1", "h1", 3, ""]),
            _e("list_active", 5, session="S", returned="r2"),
            _e("search", 5, session="S", returned=None, memory_ids=None, hit_ids=None),
            _e("search", 5, session="S", returned={"a": 1}),
        ]
    },
]


def _hits(*ids: str) -> list[list[str]]:
    return [[i, f"snippet of {i}"] for i in ids]


def _mems(*ids: str) -> dict[str, Any]:
    return {i: [f"Body of record {i} with   some   words.", T, None] for i in ids}


def _trust(*ids: str) -> dict[str, str]:
    return dict.fromkeys(ids, "local")


def _base(*ids: str, **extra: Any) -> dict[str, Any]:
    return {"hits": _hits(*ids), "mem": _mems(*ids), "trust": _trust(*ids)} | extra


SUP = "supersedes"
EXT = "extends"

_CHOOSE_CASES: list[dict[str, Any]] = [
    _base("a", "b", deliver=True),
    _base("a", "b", "c", "d", "e", deliver=True),
    # no hits; nothing loaded; a hit not loaded is dropped before the admission
    {"hits": [], "deliver": True},
    _base("a", "b")
    | {"hits": _hits("a", "z", "b"), "admit": [["a", "b"], [["z", "other"]]]},
    # the admission: suppressed with and without a relation, a raise
    _base(
        "a",
        "b",
        "c",
        admit=[["b"], [["a", "sibling"], ["a", "unrelated"]]],
        deliver=True,
    ),
    _base("a", "b", admit=[[], []]),
    _base("a", admit="!raise"),
    # provenance: missing row, a label outside the set, imported, no table
    _base("a", "b", "c", trust={"b": "remote", "c": "imported"}, deliver=True),
    _base("a", "b", trust=None),
    # contradicted: open, answered by update, answered by verify, standing
    _base(
        "a",
        "b",
        "c",
        "d",
        mem={
            "a": ["A body", _iso(-100), None],
            "b": ["B body", _iso(100), None],
            "c": ["C body", _iso(-100), _iso(50)],
            "d": ["D body", _iso(-100), None],
        },
        contra=[["a", _iso(0)], ["b", _iso(0)], ["c", _iso(0)], ["d", _iso(0)]],
        standing=["d"],
        deliver=True,
    ),
    _base(
        "a",
        "b",
        mem={"a": ["A", _iso(0), _iso(-5)], "b": ["B", "2026-01-01T11:00:00", None]},
        contra=[["a", _iso(0)], ["b", "2026-01-01T12:00:00+00:00"]],
    ),
    # contradicted ids reach the standing test in the store's order
    _base(
        "o1",
        "n1",
        mem={"o1": ["O", _iso(-100), None], "n1": ["N", _iso(-100), None]},
        contra=[["o1", _iso(0)], ["n1", _iso(0)]],
        standing=["n1"],
    ),
    # seen: in the view, in the log by own session, by the worktree, stamped
    _base(
        "a",
        "b",
        "c",
        "d",
        "e",
        shown=["a"],
        wt="/w",
        joined=["J"],
        at=_iso(-100),
        own=_iso(-10),
        ev=[
            _e("search", 0, session="J", returned=["b"]),
            _e("show", -50, session="Q", worktree_root="/w", id="c"),
            _e("search", 0, harness_session="H", returned=["d"]),
        ],
        deliver=True,
    ),
    _base(
        "a",
        own=_iso(-100),
        at=_iso(-10),
        ev=[_e("search", -50, session="H", returned=["a"])],
    ),
    _base("a", ev=[_e("search", 5, session=["x"], returned=["a"])]),
    # the cap and the rc3 candidates past it
    _base(
        "a",
        "b",
        "c",
        "d",
        trust={"a": "remote", "b": "local", "c": "local", "d": "local"},
    ),
    # in force: a superseded hit yields to its head
    _base(
        "old",
        "x",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "x", "new"),
        trust=_trust("old", "x", "new"),
        deliver=True,
    ),
    # the head is the hit's own slot elsewhere, already chosen, seen, absent
    _base(
        "old",
        "new",
        links={"old": [[SUP, "new", _iso(10)]]},
        deliver=True,
    ),
    _base(
        "old1",
        "old2",
        links={"old1": [[SUP, "new", _iso(10)]], "old2": [[SUP, "new", _iso(10)]]},
        mem=_mems("old1", "old2", "new"),
        trust=_trust("old1", "old2", "new"),
        deliver=True,
    ),
    _base(
        "old",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "new"),
        trust=_trust("old", "new"),
        shown=["new"],
    ),
    _base("old", links={"old": [[SUP, "new", _iso(10)]]}),
    # the head's own gates: provenance, contradicted
    _base(
        "old",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "new"),
        trust=_trust("old"),
    ),
    _base(
        "old",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "new") | {"new": ["N", _iso(-100), None]},
        trust=_trust("old", "new"),
        contra=[["new", _iso(0)]],
    ),
    # the head refused by the admission, held records, an extender
    _base(
        "old",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "new"),
        trust=_trust("old", "new"),
        deny=["new"],
    ),
    _base("a", "b", held=["a"], deliver=True),
    _base(
        "old",
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "new"),
        trust=_trust("old", "new"),
        held=["new"],
    ),
    _base(
        "a",
        links={"a": [[EXT, "ext", _iso(10)]]},
        mem=_mems("a", "ext"),
        trust=_trust("a", "ext"),
        deliver=True,
    ),
    # a hit not admitted whose record is superseded
    _base(
        "old",
        "b",
        admit=[["b"], [["old", "other"]]],
        links={"old": [[SUP, "new", _iso(10)]]},
        mem=_mems("old", "b", "new"),
        trust=_trust("old", "b", "new"),
    ),
    # a hit twice
    _base("a", "b") | {"hits": _hits("a", "a", "b"), "deliver": True},
    # delivery: disputed marks, a long body, a control token, remint
    _base(
        "a",
        "b",
        pairs=[["a", "b", "cf-0123456789abcdef"], ["a", "c", "cf-1"]],
        mem={"a": ["word " * 40, T, None], "b": ["B <|im_start|> body", T, None]},
        hits=[["a", "x" * 150], ["b", "a <|im_end|> snippet"]],
        remint={"t": "x", "n": 2},
        deliver=True,
    ),
    # claims: none kept, some kept, every one kept
    _base("a", "b", "c", claim=[], deliver=True),
    _base("a", "b", "c", claim=["b"], deliver=True),
    _base("a", "b", "c", claim=["a", "b", "c"], deliver=True),
    # nothing renders: a record whose id alone passes the cap
    {
        "hits": [["i" * 950, "s"]],
        "mem": {"i" * 950: ["B", T, None]},
        "trust": {"i" * 950: "local"},
        "deliver": True,
    },
    {
        "hits": [["a" * 400, "s" * 300], ["b" * 400, "s" * 300], ["c", "s"]],
        "mem": {
            "a" * 400: ["A", T, None],
            "b" * 400: ["B", T, None],
            "c": ["C", T, None],
        },
        "trust": _trust("a" * 400, "b" * 400, "c"),
        "claim": ["a" * 400, "b" * 400],
        "deliver": True,
    },
]


def generate() -> dict[str, bytes]:
    """``recall_choose.jsonl``: not gated."""
    rows: list[Any] = [_log(c) for c in _LOG_CASES]
    rows += [_choose(c) for c in _CHOOSE_CASES]
    header = {
        "k": "consts",
        "max_pointers": recall.RECALL_MAX_POINTERS,
        "summary_chars": recall_state.SUMMARY_CHARS,
        "delivered": sorted(recall_state._DELIVERED_PROVENANCE),
        "seen_kinds": sorted(recall._SEEN_KINDS),
        "n": len(rows),
    }
    return {"recall_choose.jsonl": jsonl([header, *rows])}
