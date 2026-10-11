"""Map the chat model's blind judgements back to the drivers' saved-answers format.

    python bench/decisions/map_chat.py --kind recall    --map MAP.json --blind ANSWERS.json --out recall-chat.answers.json
    python bench/decisions/map_chat.py --kind integrity --map MAP.json --blind ANSWERS.json --out integrity-chat.answers.json

ANSWERS.json is one object over every case: for recall {opaque id -> p};
for the write gate {opaque id -> {contradicts, instruction, secret, relation?{option -> p}}}.

For the write review (unit D1; a map whose entries carry `related`, from
review_integrity.py export), --blind may be given more than once, one
answer file per chunk, each {opaque id -> {relation: {token -> label},
unsafe: 0..1, secret: 0..1}}. The review keys map back to
{stmt_id -> {relation: {store id -> label}, unsafe, secret}}. A case with
no answer, or with an answer that cannot be read (an unknown key or
label, a token the case did not show, or a score outside 0 to 1 or
missing), is left out and
counted, never guessed: the review scorer reads it as missing. A relation
that leaves a shown token unanswered is kept with `complete` false: the
daemon decides such a call only when a score refuses it, so the scorer
reads it as missing otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

# The review's labels, as bettermemory.review.LABELS holds them (a test
# pins the two equal; this script imports nothing from the package).
REVIEW_LABELS = ("update", "conflict", "restates", "unrelated")
REVIEW_KEYS = frozenset({"relation", "unsafe", "secret"})


def recall_answers(mapping: dict, blind: dict) -> list[float]:
    """A probability list aligned to the export's index order."""
    probs: list[float | None] = [None] * len(mapping)
    for oid, i in mapping.items():
        if oid not in blind:
            raise SystemExit(f"missing judgement for {oid}")
        probs[int(i)] = float(blind[oid])
    if any(p is None for p in probs):
        raise SystemExit("gaps in recall judgements")
    return [float(p) for p in probs if p is not None]


def integrity_answers(mapping: dict, blind: dict) -> dict:
    out = {}
    for oid, sid in mapping.items():
        a = blind.get(oid)
        if a is None:
            raise SystemExit(f"missing judgement for {oid}")
        row = {
            k: {"noul": float(a[k])} for k in ("contradicts", "instruction", "secret")
        }
        if isinstance(a.get("relation"), dict) and a["relation"]:
            probs = {k: float(v) for k, v in a["relation"].items()}
            s = sum(probs.values()) or 1.0
            probs = {k: v / s for k, v in probs.items()}
            row["relation"] = {
                "choice": max(probs, key=probs.get),
                "probabilities": probs,
            }
        out[sid] = row
    return out


def is_review_map(mapping: dict) -> bool:
    return any(isinstance(v, dict) and "related" in v for v in mapping.values())


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return number


def _review_row(answer: Any, tokens: dict[str, str]) -> dict[str, Any] | None:
    """One case's answer mapped back, or None when it cannot be read."""
    if not isinstance(answer, dict) or set(answer) - REVIEW_KEYS:
        return None
    unsafe, secret = _score(answer.get("unsafe")), _score(answer.get("secret"))
    relation = answer.get("relation", {})
    if unsafe is None or secret is None or not isinstance(relation, dict):
        return None
    mapped: dict[str, str] = {}
    for token, label in relation.items():
        if token not in tokens or label not in REVIEW_LABELS:
            return None
        mapped[tokens[token]] = label
    # A relation that leaves a shown token unanswered is kept and marked:
    # the daemon decides such a call when a score refuses it, and otherwise
    # asks again, which the scorer reads as a missing answer.
    return {
        "relation": mapped,
        "unsafe": unsafe,
        "secret": secret,
        "complete": set(mapped) == set(tokens.values()),
    }


def review_answers(
    mapping: dict, blind: dict
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """The review keys mapped back: ({stmt_id: {relation, unsafe, secret}},
    the stmt ids with no answer, the stmt ids whose answer could not be
    read)."""
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    malformed: list[str] = []
    for oid, entry in mapping.items():
        sid = str(entry["stmt_id"])
        if oid not in blind:
            missing.append(sid)
            continue
        row = _review_row(blind[oid], dict(entry["related"]))
        if row is None:
            malformed.append(sid)
            continue
        out[sid] = row
    return out, missing, malformed


def merge_blind(paths: list[Path]) -> dict:
    """Several answer files as one object; a case answered twice is an
    error, not a choice."""
    merged: dict = {}
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise SystemExit(f"{path} is not one JSON object")
        clash = set(doc) & set(merged)
        if clash:
            raise SystemExit(f"{len(clash)} cases answered twice, in {path}")
        merged.update(doc)
    return merged


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=["recall", "integrity"])
    ap.add_argument("--map", required=True)
    ap.add_argument("--blind", required=True, action="append")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    mapping = json.loads(Path(a.map).read_text(encoding="utf-8"))
    blind = merge_blind([Path(p) for p in a.blind])
    if a.kind == "recall":
        answers: object = recall_answers(mapping, blind)
    elif is_review_map(mapping):
        answers, missing, malformed = review_answers(mapping, blind)
        Path(a.out).write_bytes((json.dumps(answers, indent=0) + "\n").encode("utf-8"))
        print(
            json.dumps(
                {
                    "kind": "review",
                    "cases": len(mapping),
                    "answered": len(answers),
                    "missing": len(missing),
                    "malformed": len(malformed),
                    "out": a.out,
                }
            )
        )
        return 0
    else:
        answers = integrity_answers(mapping, blind)
    Path(a.out).write_text(json.dumps(answers, indent=0))
    print(f"{a.kind} answers: {len(mapping)} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
