"""Blind files for the chat-model arm: the states and questions under opaque ids, in chunks.

Reads a driver export (decisions_recall.py, decisions_integrity.py with
--instrument export, or review_integrity.py export), keeps only what a
judge needs, replaces every id with an opaque token, shuffles, and writes
chunk files plus the map back. The map is private where the export is;
the chunks carry nothing that names a memory, a session or a label.

Two integrity shapes. A row with a `state` (decisions_integrity.py) keeps
its state and questions. A row with `related` (review_integrity.py, the
write review of unit D1) keeps the new statement and the related stored
statements the review showed, each under an opaque per-item token in
place of its store id, with its scopes, updated date, claim_sized flag
and text, and no label of any kind; the questions live in the committed
judge prompt, not in the chunk. Its map entry is
{"stmt_id": ..., "related": {token: store id}}.

    python bench/decisions/blind.py EXPORT.jsonl OUTDIR --kind recall|integrity [--chunk 40] [--seed 11]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any

KEEP = {"recall": ("state",), "integrity": ("state", "questions")}
# What a review row's chunk entry keeps of each related statement; its id
# is replaced by an opaque token.
RELATED_KEEP = ("scopes", "updated", "claim_sized", "text")


def opaque(seed: int, key: str) -> str:
    """Ten characters, always opening with a letter so no id reads as a number."""
    return "c" + hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:9]


def related_token(seed: int, case: str, memory_id: str) -> str:
    """An opaque per-item token for a related statement: eight characters
    opening with "r", derived from the item's case and the store id, so
    no store ULID or its time prefix reaches a chunk."""
    return "r" + hashlib.sha256(f"{seed}:{case}:{memory_id}".encode()).hexdigest()[:7]


def _review_row(row: dict[str, Any], oid: str, seed: int) -> tuple[dict, dict]:
    """A review row's blind entry and its map entry."""
    tokens: dict[str, str] = {}
    related = []
    for entry in row["related"]:
        token = related_token(seed, oid, str(entry["id"]))
        if token in tokens:
            raise SystemExit(f"related token collision in {row['stmt_id']}")
        tokens[token] = str(entry["id"])
        related.append({"id": token, **{k: entry[k] for k in RELATED_KEEP}})
    return (
        {"case": oid, "statement": row["statement"], "related": related},
        {"stmt_id": row["stmt_id"], "related": tokens},
    )


def export(
    rows: list[dict], kind: str, chunk: int, seed: int
) -> tuple[list[list[dict]], dict]:
    """Blind chunks and the map {opaque id -> the export's own key}, or
    for a review row {opaque id -> {stmt_id, related token map}}."""
    key = "i" if kind == "recall" else "stmt_id"
    keep = KEEP[kind]
    mapping: dict[str, object] = {}
    blind = []
    for r in rows:
        oid = opaque(seed, str(r[key]))
        if oid in mapping:
            raise SystemExit(f"opaque id collision on {r[key]}")
        if kind == "integrity" and "related" in r:
            entry, mapped = _review_row(r, oid, seed)
            mapping[oid] = mapped
            blind.append(entry)
            continue
        mapping[oid] = r[key]
        blind.append({"case": oid, **{k: r[k] for k in keep}})
    random.Random(seed).shuffle(blind)
    chunks = [blind[i : i + chunk] for i in range(0, len(blind), chunk)]
    return chunks, mapping


def chunk_name(kind: str, n: int) -> str:
    return f"{kind}-{n:02d}.jsonl"


def chunk_bytes(rows: list[dict]) -> bytes:
    """A chunk file's bytes: one JSON object per line, UTF-8, LF only."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode(
        "utf-8"
    )


def write_chunks(
    out: Path, kind: str, chunks: list[list[dict]], mapping: dict
) -> dict[str, str]:
    """Write the chunk files and the map into `out`, as bytes; returns
    {file name: sha256} for each file written."""
    out.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    for n, ch in enumerate(chunks, 1):
        data = chunk_bytes(ch)
        (out / chunk_name(kind, n)).write_bytes(data)
        shas[chunk_name(kind, n)] = hashlib.sha256(data).hexdigest()
    map_name = f"{kind}_blind_map.json"
    data = (json.dumps(mapping, indent=0) + "\n").encode("utf-8")
    (out / map_name).write_bytes(data)
    shas[map_name] = hashlib.sha256(data).hexdigest()
    return shas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("outdir")
    ap.add_argument("--kind", required=True, choices=sorted(KEEP))
    ap.add_argument("--chunk", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args(argv)
    rows = [
        json.loads(line)
        for line in Path(a.export).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chunks, mapping = export(rows, a.kind, a.chunk, a.seed)
    out = Path(a.outdir)
    write_chunks(out, a.kind, chunks, mapping)
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
