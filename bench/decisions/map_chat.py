"""Map the chat model's blind judgements back to the drivers' saved-answers format.

    python bench/decisions/map_chat.py --kind recall    --map MAP.json --blind ANSWERS.json --out recall-chat.answers.json
    python bench/decisions/map_chat.py --kind integrity --map MAP.json --blind ANSWERS.json --out integrity-chat.answers.json

ANSWERS.json is one object over every case: for recall {opaque id -> p};
for the write gate {opaque id -> {contradicts, instruction, secret, relation?{option -> p}}}.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=["recall", "integrity"])
    ap.add_argument("--map", required=True)
    ap.add_argument("--blind", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    mapping = json.load(open(a.map))
    blind = json.load(open(a.blind))
    if a.kind == "recall":
        answers: object = recall_answers(mapping, blind)
    else:
        answers = integrity_answers(mapping, blind)
    Path(a.out).write_text(json.dumps(answers, indent=0))
    print(f"{a.kind} answers: {len(mapping)} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
