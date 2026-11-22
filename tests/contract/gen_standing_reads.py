"""Fixtures: the store-reading half of standing.py (``standing_reads.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/standing/reads.rs``. This pins
`standing._current` (with the body sha memo `_SHAS`), `answers`,
`backlog`, `guarded_ids`, `makes_standing`, `held_ids`, `standing_ids`,
`refusal_rows` and `held_by_removal`, over an in-memory store and
admission, with `in_force.in_force` run as it is.

Each row is a case: a store and an admission, the review switch
(``tau``: `review.current_tau` set or not), an optional ``bound`` (the
memo's `_SHA_BOUND` rebound for the case), and ``ops`` run in order on
one memo, cleared when the case starts. The store:

- ``recs``: the active records as `Store.standing_rows` reads them,
  ``[id, category, updated, scopes, recorded, log_mac, probability,
  for]``, where ``for`` is the body the standing row's sha was taken of
  (null for no sha); rows come back newest ``updated`` (then id) first;
- ``bodies``: what `Store.body_of` answers (a missing id answers None);
- ``links``: what `Store.inbound_links` answers (``{target: [[type,
  source, updated]]}``);
- ``returned``: the ids `Store.returned_rows` holds marks for;
- ``unanswered``, ``candidates``: what `digest_unanswered_ids` and
  `standing_candidate_ids` answer (null for None);
- ``tombs``: `tombstone_links` per id (``[[type, target]]``), any other
  id raising NotFoundError;
- ``mems``: the records `get_many` finds, ``[id, scopes, updated,
  body]``.

The admission admits by id the ids of ``admit`` (null for all) and by
row the rows whose ``recorded`` is not in ``deny``.

Each op answers ``[result, calls]``, ``calls`` being every read of the
store and the admission in order: ``["b", id]`` body_of, ``["r", ids]``
standing_rows, ``["u"]`` digest_unanswered_ids, ``["m", ids]``
returned_rows, ``["c", tau]`` standing_candidate_ids, ``["g", ids]``
get_many, ``["t", id]`` tombstone_links, ``["l", targets]``
inbound_links, ``["p", ids]`` prime, ``["a", id]`` admits_id, ``["w",
scopes, recorded]`` admits_row. A refusal row is written as its
``[key, value]`` pairs in the reference's order. Everything is ASCII
text after JSON escaping, and no answer follows the interpreter or the
platform, so the file is not gated.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from unittest import mock

from bettermemory import standing
from bettermemory.models import Memory, MemoryLink
from bettermemory.store import NotFoundError

from ._common import jsonl


def R(c: str) -> str:
    """A ULID-shaped id ending in `c`."""
    return "01K" + "0" * 22 + c


A, B, C_, D, E_ = R("A"), R("B"), R("C"), R("D"), R("E")
S, X = "supersedes", "extends"
TAU = 0.3
BELOW = 0.29999999999999993
ABOVE = 0.30000000000000004


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _World:
    def __init__(self, c: dict[str, Any]) -> None:
        self.c = c
        self.calls: list[list[Any]] = []

    # The store.
    def body_of(self, record_id: str) -> str | None:
        self.calls.append(["b", record_id])
        body: str | None = self.c["bodies"].get(record_id)
        return body

    def standing_rows(self, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        wanted = None if ids is None else list(dict.fromkeys(ids))
        self.calls.append(["r", wanted])
        out = [
            {
                "id": r[0],
                "category": r[1],
                "updated": r[2],
                "scopes": tuple(r[3]),
                "recorded": r[4],
                "log_mac": r[5],
                "probability": r[6],
                "body_sha256": None if r[7] is None else _sha(r[7]),
            }
            for r in self.c["recs"]
            if wanted is None or r[0] in wanted
        ]
        out.sort(key=lambda row: (row["updated"], row["id"]), reverse=True)
        return out

    def digest_unanswered_ids(self) -> list[str] | None:
        self.calls.append(["u"])
        got: list[str] | None = self.c["unanswered"]
        return got

    def returned_rows(self, ids: Iterable[str] | None = None) -> dict[str, Any]:
        wanted = None if ids is None else list(dict.fromkeys(ids))
        self.calls.append(["m", wanted])
        marks = self.c["returned"]
        keys = marks if wanted is None else [i for i in wanted if i in marks]
        return {k: {"via": "v", "returned": "t"} for k in keys}

    def standing_candidate_ids(self, tau: float | None) -> list[str] | None:
        self.calls.append(["c", tau])
        got: list[str] | None = self.c["candidates"]
        return got

    def get_many(self, ids: Sequence[str]) -> list[Memory]:
        self.calls.append(["g", list(ids)])
        found = {m[0]: m for m in self.c["mems"]}
        return [
            Memory(
                id=found[i][0],
                created=datetime.fromisoformat(found[i][2]),
                updated=datetime.fromisoformat(found[i][2]),
                scopes=found[i][1],
                confidence="high",
                source="inferred",
                body=found[i][3],
            )
            for i in ids
            if i in found
        ]

    def tombstone_links(self, memory_id: str) -> list[MemoryLink]:
        self.calls.append(["t", memory_id])
        if memory_id not in self.c["tombs"]:
            raise NotFoundError(memory_id)
        return [
            MemoryLink(type=t, target_id=target)
            for t, target in self.c["tombs"][memory_id]
        ]

    def inbound_links(
        self, target_ids: Iterable[str], types: Sequence[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        frontier = list(target_ids)
        self.calls.append(["l", frontier])
        links = self.c["links"]
        return {
            t: [(r[0], r[1], r[2]) for r in links[t]] for t in frontier if t in links
        }

    # The admission.
    def prime(self, record_ids: Iterable[str]) -> None:
        self.calls.append(["p", list(record_ids)])

    def admits_id(self, record_id: str) -> bool:
        self.calls.append(["a", record_id])
        admit = self.c["admit"]
        return admit is None or record_id in admit

    def admits_row(self, scopes: Sequence[str], recorded: str | None) -> bool:
        self.calls.append(["w", list(scopes), recorded])
        return recorded not in self.c["deny"]


def _op(w: _World, op: list[Any]) -> Any:
    name = op[0]
    store: Any = w
    if name == "answers":
        return [[k, v] for k, v in standing.answers(store, op[1]).items()]
    if name == "backlog":
        got = standing.backlog(store, w)
        return [list(got.ids), got.count]
    if name == "guarded":
        return standing.guarded_ids(store, op[1])
    if name == "makes":
        return standing.makes_standing(
            store, op[1], category=op[2], probability=op[3], body_changed=op[4]
        )
    if name == "held":
        return sorted(standing.held_ids(store, op[1]))
    if name == "standing":
        return standing.standing_ids(store, w, op[1], held=op[2])
    if name == "refusal":
        rows = standing.refusal_rows(store, op[1], w)
        return [[[k, v] for k, v in row.items()] for row in rows]
    if name == "removal":
        return standing.held_by_removal(store, op[1])
    if name == "count":
        return len(standing._SHAS)
    raise AssertionError(name)


def answer(c: dict[str, Any]) -> list[Any]:
    w = _World(c)
    out: list[Any] = []
    tau = object() if c["tau"] else None
    with (
        mock.patch.object(standing, "current_tau", lambda: tau),
        mock.patch.object(standing, "_SHA_BOUND", c.get("bound", standing._SHA_BOUND)),
    ):
        standing._clear_shas()
        for op in c["ops"]:
            w.calls = []
            result = _op(w, op)
            out.append([result, w.calls])
        standing._clear_shas()
    return out


def K(
    ops: list[Any],
    recs: Any = None,
    *,
    bodies: Any = None,
    tau: bool = True,
    **rest: Any,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "ops": ops,
        "recs": recs or [],
        "bodies": bodies if bodies is not None else {},
        "tau": tau,
        "links": {},
        "returned": [],
        "unanswered": None,
        "candidates": None,
        "tombs": {},
        "mems": [],
        "admit": None,
        "deny": [],
    }
    case.update(rest)
    return case


def rec(
    rid: str,
    p: float | None,
    *,
    cat: str | None = None,
    upd: str = "1",
    mac: str | None = "m",
    body: str | None = "b",
    rp: str | None = None,
) -> list[Any]:
    """A standing row whose answer was given for `body` (None: no sha)."""
    return [rid, cat, upd, ["global"], rp, mac, p, body]


BODIES = {A: "b", B: "b", C_: "b", D: "b", E_: "b"}
G = ["global"]

CASES = [
    # _current: no probability; no mac (never memoized); a memo hit; a sha
    # for another body; no sha; a body gone; a body in UTF-8.
    K([["answers", [A]], ["count"]], [rec(A, None)], bodies=BODIES),
    K(
        [["answers", [A]], ["answers", [A]], ["count"]],
        [rec(A, 0.5, mac=None)],
        bodies=BODIES,
    ),
    K([["answers", [A]], ["answers", [A]], ["count"]], [rec(A, 0.5)], bodies=BODIES),
    K(
        [["answers", [A, B]]],
        [rec(A, 0.5, body="c"), rec(B, 0.5, body=None)],
        bodies=BODIES,
    ),
    K([["answers", [A]], ["count"]], [rec(A, 0.5)], bodies={}),
    K(
        [["answers", [A]]],
        [rec(A, 0.9, body="é\U0001f600 x")],
        bodies={A: "é\U0001f600 x"},
    ),
    K(
        [["answers", [A, A, B, C_]]],
        [rec(A, 0.1, upd="2"), rec(B, 1.0, upd="3")],
        bodies=BODIES,
    ),
    # The memo keyed by (id, mac): another mac reads the body again.
    K(
        [["answers", [A]], ["answers", [B]], ["count"]],
        [rec(A, 0.5, mac="x"), rec(B, 0.5, mac="x")],
        bodies=BODIES,
    ),
    # The bound: the oldest is popped, a hit moves to the end.
    K(
        [
            ["answers", [A]],
            ["answers", [B]],
            ["answers", [A]],
            ["answers", [C_]],
            ["count"],
            ["answers", [A]],
            ["answers", [B]],
            ["count"],
        ],
        [rec(A, 0.5, upd="3"), rec(B, 0.5, upd="2"), rec(C_, 0.5, upd="1")],
        bodies=BODIES,
        bound=2,
    ),
    K(
        [["answers", [A, B]], ["count"]],
        [rec(A, 0.5), rec(B, 0.5)],
        bodies=BODIES,
        bound=0,
    ),
    # backlog: the digest's ids or every row; admission by row first.
    K(
        [["backlog"]],
        [rec(A, None, upd="2"), rec(B, 0.5, upd="3"), rec(C_, 0.5, body="z")],
        bodies=BODIES,
    ),
    K(
        [["backlog"]],
        [rec(A, None), rec(B, None, rp="p")],
        bodies=BODIES,
        unanswered=[B, A, B],
        deny=["p"],
    ),
    K([["backlog"]], [rec(A, None)], bodies=BODIES, unanswered=[]),
    K([["backlog"]], [rec(A, None, rp=None)], bodies=BODIES, deny=[None]),
    # standing_ids: the pre-filter on the raw probability, threshold edges.
    K(
        [["standing", None, False]],
        [
            rec(A, TAU, upd="5"),
            rec(B, BELOW, upd="4"),
            rec(C_, ABOVE, upd="3"),
            rec(D, None, cat="ambient", upd="2"),
            rec(E_, None, cat="fact", upd="1"),
        ],
        bodies=BODIES,
    ),
    K(
        [["standing", None, False]],
        [rec(A, 0.0, cat="ambient", body="old"), rec(B, 1.0, upd="0", body="old")],
        bodies=BODIES,
    ),
    K(
        [["standing", [], False], ["standing", None, False]],
        [rec(A, 1.0)],
        bodies=BODIES,
        candidates=[],
    ),
    K(
        [["standing", None, False]],
        [rec(A, 1.0), rec(B, 1.0)],
        bodies=BODIES,
        candidates=[B],
    ),
    K(
        [["standing", [B, A], False]],
        [rec(A, 1.0), rec(B, 1.0, rp="p")],
        bodies=BODIES,
        deny=["p"],
    ),
    # In force: superseded by an admitted record is out; held is out
    # unless asked; an unadmitted superseder changes nothing.
    K(
        [["standing", None, False], ["standing", None, True]],
        [rec(A, 1.0, upd="2"), rec(B, 1.0, upd="3")],
        bodies=BODIES,
        links={A: [[S, C_, "9"]]},
    ),
    K(
        [["standing", None, False]],
        [rec(A, 1.0)],
        bodies=BODIES,
        links={A: [[S, C_, "9"]]},
        admit=[A],
    ),
    K(
        [["standing", None, False], ["standing", None, True], ["guarded", [A, B]]],
        [rec(A, 1.0, upd="2"), rec(B, 1.0, upd="3")],
        bodies=BODIES,
        returned=[A],
    ),
    K(
        [["standing", None, False], ["held", None]],
        [rec(A, 1.0, upd="2"), rec(B, 1.0, upd="3")],
        bodies=BODIES,
        returned=[A],
        tau=False,
    ),
    # guarded_ids: none asked reads nothing; every record admitted.
    K(
        [["guarded", []], ["guarded", [A, C_]]],
        [rec(A, 1.0, rp="p")],
        bodies=BODIES,
        deny=["p"],
        admit=[],
    ),
    K(
        [["guarded", [A]]],
        [rec(A, 1.0)],
        bodies=BODIES,
        links={A: [[S, B, "1"], [X, C_, "2"]]},
    ),
    # held_ids: the review off, no marks, marks of records not standing.
    K(
        [["held", None], ["held", [A]]],
        [rec(A, 1.0)],
        bodies=BODIES,
        returned=[A],
        tau=False,
    ),
    K([["held", None], ["held", [B]]], [rec(A, 1.0)], bodies=BODIES, returned=[A]),
    K(
        [["held", None], ["held", [C_, B, A, B]]],
        [rec(A, 1.0), rec(B, 0.1), rec(C_, None, cat="ambient"), rec(D, 1.0, body="x")],
        bodies=BODIES,
        returned=[A, B, C_, D],
    ),
    # makes_standing: not standing after; history; held; unchanged body
    # already standing; not standing now; body changed; no row.
    K(
        [["makes", A, None, BELOW, False], ["makes", A, "fact", None, True]],
        [rec(A, 1.0)],
        bodies=BODIES,
    ),
    K(
        [["makes", A, None, 1.0, True]],
        [rec(A, 1.0)],
        bodies=BODIES,
        links={A: [[S, B, "1"]]},
    ),
    K([["makes", A, None, 1.0, False]], [rec(A, 1.0)], bodies=BODIES, returned=[A]),
    K(
        [["makes", A, None, 1.0, False]],
        [rec(A, 1.0)],
        bodies=BODIES,
        returned=[A],
        tau=False,
    ),
    K(
        [
            ["makes", A, None, TAU, False],
            ["makes", A, None, TAU, True],
            ["makes", A, "ambient", None, False],
        ],
        [rec(A, ABOVE)],
        bodies=BODIES,
    ),
    K(
        [
            ["makes", A, None, 1.0, False],
            ["makes", B, "ambient", None, False],
            ["makes", C_, None, 1.0, False],
        ],
        [rec(A, BELOW), rec(B, 0.9, body="other"), rec(C_, None, cat="ambient")],
        bodies=BODIES,
    ),
    K([["makes", D, None, 1.0, False]], [], bodies=BODIES),
    # refusal_rows: key order, admitted false only when not admitted, the
    # UTC date across a day boundary, dedup in order, unknown skipped.
    K(
        [["refusal", [B, A, B, D]], ["refusal", []]],
        mems=[
            [A, G, "2026-01-01T23:30:00-05:00", "  a body  "],
            [B, ["repo:x", "global"], "2026-01-02T00:30:00+02:00", "x" * 700],
        ],
        admit=[A],
    ),
    K(
        [["refusal", [A, C_]]],
        mems=[
            [A, G, "2026-03-01T00:00:00+00:00", "y" * 600],
            [C_, G, "2025-12-31T23:59:59.999999-00:01", "w" * 601],
        ],
    ),
    # held_by_removal: no tombstone, other link types, dedup order, held
    # only, the review off.
    K([["removal", [A]], ["removal", []]], [rec(A, 1.0)], bodies=BODIES),
    K(
        [["removal", [D, E_, A]]],
        [rec(A, 1.0), rec(B, 1.0), rec(C_, 1.0)],
        bodies=BODIES,
        returned=[A, B, C_],
        tombs={D: [[S, C_], [X, A], [S, B], [S, C_]], E_: [[S, A], ["contradicts", B]]},
    ),
    K(
        [["removal", [D]]],
        [rec(A, 1.0)],
        bodies=BODIES,
        returned=[A],
        tombs={D: [[S, A], [S, B]]},
        tau=False,
    ),
    K([["removal", [D]]], [rec(A, 1.0)], bodies=BODIES, tombs={D: [[X, A]]}),
]


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for c in CASES:
        rows.append({**c, "out": answer(c)})
    header = {"fixture": "standing_reads", "count": len(rows)}
    return {"standing_reads.jsonl": jsonl([header, *rows])}
