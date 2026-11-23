"""Reference decisions, replacement order and event spelling from review.py.

IDs are indices into a code-point table, so the fixture can express any Python
string. Expected answers are the ASCII spelling of json.dumps in insertion
order. The seeded sweep selects independent masks and labels from two 64-bit
LCG words per case. No host, clock, global override or store is involved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from bettermemory import review, supersession

from ._common import jsonl

IDS = ["a", "b", "c", "d", "e", "past", "", "δ", "\udcff", "\ufffd", "x\0y"]
LABELS = ["update", "extends", "conflict", "restates", "unrelated", "unknown"]
SCORES = [
    "0",
    "0.29999999999999993",
    "0.3",
    "0.30000000000000004",
    "1",
    "nan",
    "inf",
    "-inf",
]
SEED = 20261123
COUNT = 8192
MASK = 2**64 - 1


def _plan(
    entries: Iterable[tuple[int, bool]] = (),
    relation: Iterable[tuple[int, int]] = (),
    *,
    cue: list[int] | None = None,
    declared: Iterable[int] = (),
    standing: Iterable[int] = (),
    flags: int = 11,
    scores: tuple[int, int, int, int] = (0, 0, 2, 2),
) -> dict[str, Any]:
    return dict(
        entries=list(entries),
        relation=list(relation),
        cue=cue,
        declared=list(declared),
        standing=list(standing),
        flags=flags,
        scores=scores,
    )


def _match(i: int) -> supersession.SupersessionMatch:
    return supersession.SupersessionMatch(
        IDS[i], "supersedes", "cue", None, "", "", 0.0, ""
    )


def _outputs(p: dict[str, Any]) -> str:
    u, s, tu, ts = (float(SCORES[i]) for i in p["scores"])
    answers = review.ReviewAnswers(
        {IDS[i]: LABELS[label] for i, label in p["relation"]}, u, s
    )
    cue = p["cue"]
    related = review.RelatedSet(
        tuple(
            review.RelatedEntry(IDS[i], (), "", claim, "") for i, claim in p["entries"]
        ),
        None
        if cue is None
        else supersession.SupersessionReport(True, [_match(i) for i in cue], []),
    )
    declared = [IDS[i] for i in p["declared"]]
    flags = p["flags"]
    decision = review.apply_review(
        answers,
        related,
        tau=review.Tau(tu, ts),
        write_claim_sized=bool(flags & 1),
        write_supersession=bool(flags & 2),
        said=bool(flags & 4),
        restates_refuses=bool(flags & 8),
        write_standing=bool(flags & 16),
        declared_ids=declared,
        standing_ids=[IDS[i] for i in p["standing"]],
    )
    return json.dumps(
        [review.replaced_ids(answers, related, declared), decision.to_event()]
    )


def _sweep_plan(a: int, b: int) -> dict[str, Any]:
    entries = [(i, bool(b & 1 << (8 + i))) for i in range(5) if b & 1 << i]
    if entries and a & (1 << 52):
        entries.append(entries[0])
    relation = [(i, lab) for i in range(6) if (lab := a >> (3 * i) & 7) < len(LABELS)]
    cue = [i for i in range(6) if b & 1 << (16 + i)]
    if cue and a & (1 << 49):
        cue.append(cue[0])
    return _plan(
        entries,
        relation,
        cue=cue if a & (1 << 51) else None,
        declared=[i for i in range(6) if b & 1 << (24 + i)],
        standing=[i for i in range(6) if b & 1 << (32 + i)],
        flags=(a >> 18) & 31,
        scores=((a >> 24) & 7, (a >> 27) & 7, (a >> 30) & 7, (a >> 33) & 7),
    )


def _next(n: int) -> int:
    return (n * 6364136223846793005 + 1442695040888963407) & MASK


def generate() -> dict[str, bytes]:
    plans = [
        _plan(),
        _plan(
            [(0, True), (0, False), (1, True)],
            [(0, 0), (1, 1)],
            cue=[2, 2, 5, 1, 8],
            declared=[5, 5],
            flags=27,
        ),
        _plan(
            [(0, True), (0, False), (1, True)],
            [(0, 0), (1, 1)],
            cue=[2, 2, 5, 1, 8],
            declared=[5, 5],
            flags=31,
        ),
        _plan(
            [(8, True), (9, True), (10, False), (7, True), (6, True)],
            [(8, 0), (9, 3), (10, 2), (7, 1), (6, 4)],
            cue=[10, 7, 6, 5],
        ),
        _plan(
            [(0, True), (1, True)], [(0, 3), (1, 4)], cue=[5], standing=[5], flags=27
        ),
        _plan(
            [(0, True), (1, True)], [(0, 3), (1, 4)], cue=[5], standing=[5], flags=19
        ),
        _plan(
            [(1, True), (0, True), (1, True)],
            [(0, 2), (1, 2)],
            standing=[0, 1],
            flags=31,
        ),
        _plan(standing=[5], declared=[5, 5], flags=11),
        _plan([(0, True)], [(0, 3)], declared=[5], flags=11),
        _plan([(0, True), (1, True)], [(0, 3), (1, 1)], flags=11),
        _plan(
            [(0, True), (1, False), (2, True), (3, True), (4, True)],
            [(0, 0), (1, 0), (2, 2), (3, 1), (4, 0)],
            cue=[3, 4],
            declared=[3],
        ),
        _plan([(0, True)], [(0, 2)], cue=[0], standing=[0], scores=(2, 2, 2, 2)),
    ]
    plans.extend(
        _plan([(0, claim)], [(0, 0)], flags=f)
        for f in (0, 1, 2, 3)
        for claim in (False, True)
    )
    for score in range(len(SCORES)):
        plans.extend((_plan(scores=(score, 0, 2, 2)), _plan(scores=(0, score, 2, 2))))
    plans.extend(
        _plan([(0, True)], [(0, lab)], cue=[0, 5], declared=[0] if declared else [])
        for lab in range(len(LABELS))
        for declared in (True, False)
    )
    rows: list[dict[str, Any]] = [
        {
            "k": "tables",
            "ids": [list(map(ord, i)) for i in IDS],
            "labels": LABELS,
            "scores": SCORES,
        }
    ]
    rows.extend({"k": "case", "plan": p, "answer": _outputs(p)} for p in plans)
    # This checks the entire serialization order, including combinations which
    # no single branch of apply_review emits, on a directly constructed decision.
    event = review.ReviewDecision(
        IDS[8],
        (IDS[0],),
        (IDS[1],),
        (IDS[2],),
        ((IDS[3], IDS[8]),),
        (IDS[4],),
        (IDS[5],),
        (IDS[6],),
        True,
    )
    rows.append({"k": "event", "answer": json.dumps(event.to_event())})
    digest = hashlib.sha256()
    n = SEED
    for _ in range(COUNT):
        a = _next(n)
        n = _next(a)
        digest.update((_outputs(_sweep_plan(a, n)) + "\n").encode("ascii"))
    rows.append(
        {"k": "sweep", "seed": SEED, "count": COUNT, "sha256": digest.hexdigest()}
    )
    return {"review_decision.jsonl": jsonl(rows)}
