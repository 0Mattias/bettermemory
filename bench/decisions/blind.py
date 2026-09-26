"""Blind files for the chat-model arm: the states and questions under opaque ids, in chunks.

Reads a driver export (decisions_recall.py or decisions_integrity.py with
--instrument export), keeps only what a judge needs (the state, and for
the write gate the questions), replaces every id with an opaque token,
shuffles, and writes chunk files plus the map back. The map is private
where the export is; the chunks carry nothing that names a memory, a
session or a label.

    python bench/decisions/blind.py EXPORT.jsonl OUTDIR --kind recall|integrity [--chunk 40] [--seed 11]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

KEEP = {"recall": ("state",), "integrity": ("state", "questions")}


def opaque(seed: int, key: str) -> str:
    """Ten characters, always opening with a letter so no id reads as a number."""
    return "c" + hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:9]


def export(
    rows: list[dict], kind: str, chunk: int, seed: int
) -> tuple[list[list[dict]], dict]:
    """Blind chunks and the map {opaque id -> the export's own key}."""
    key = "i" if kind == "recall" else "stmt_id"
    keep = KEEP[kind]
    mapping: dict[str, object] = {}
    blind = []
    for r in rows:
        oid = opaque(seed, str(r[key]))
        if oid in mapping:
            raise SystemExit(f"opaque id collision on {r[key]}")
        mapping[oid] = r[key]
        blind.append({"case": oid, **{k: r[k] for k in keep}})
    random.Random(seed).shuffle(blind)
    chunks = [blind[i : i + chunk] for i in range(0, len(blind), chunk)]
    return chunks, mapping


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("outdir")
    ap.add_argument("--kind", required=True, choices=sorted(KEEP))
    ap.add_argument("--chunk", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.export) if line.strip()]
    chunks, mapping = export(rows, a.kind, a.chunk, a.seed)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for n, ch in enumerate(chunks, 1):
        (out / f"{a.kind}-{n:02d}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ch)
        )
    (out / f"{a.kind}_blind_map.json").write_text(json.dumps(mapping, indent=0))
    print(
        json.dumps(
            {
                "cases": len(rows),
                "chunks": len(chunks),
                "chunk_size": a.chunk,
                "outdir": str(out),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
