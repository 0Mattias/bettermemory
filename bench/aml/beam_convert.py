"""Convert BEAM's Parquet splits to the JSON bench/aml/run.py reads.

BEAM (Tavakoli et al., ICLR 2026; huggingface.co/datasets/Mohammadta/BEAM,
CC BY-SA 4.0) ships Parquet. Only this script needs pyarrow, so the harness
itself stays on the repo's dependency set:

    uv run --no-project --with pyarrow python bench/aml/beam_convert.py 100K

Output: bench/aml/data/beam/<split>.json, one record per conversation with
its chat batches and its probing questions (the `probing_questions` column
is a Python-literal string upstream; it is parsed with `ast.literal_eval`).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

DATA = Path(__file__).resolve().parent / "data" / "beam"


def _ids(value: object) -> list[int]:
    """`source_chat_ids` arrives as a list, a dict of lists, or nothing,
    depending on the question category; flatten to message ids."""
    out: list[int] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_ids(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_ids(v))
    elif isinstance(value, int):
        out.append(value)
    return out


def main() -> None:
    for split in sys.argv[1:] or ["100K"]:
        rows = pq.read_table(DATA / f"{split}.parquet").to_pylist()
        out = []
        for row in rows:
            probing = ast.literal_eval(row["probing_questions"])
            questions = []
            for category, items in probing.items():
                for i, q in enumerate(items):
                    gold = (
                        q.get("answer")
                        or q.get("ideal_answer")
                        or q.get("ideal_response")
                    )
                    gold = (
                        gold
                        or q.get("ideal_summary")
                        or q.get("expected_compliance")
                        or ""
                    )
                    questions.append(
                        {
                            "question_id": f"beam-{split}:{row['conversation_id']}:{category}:{i}",
                            "category": category,
                            "question": q["question"],
                            "gold": str(gold),
                            "rubric": [str(r) for r in q.get("rubric", [])],
                            "source_chat_ids": _ids(q.get("source_chat_ids")),
                        }
                    )
            batches = [
                [
                    {
                        "id": m.get("id"),
                        "role": m["role"],
                        "content": m["content"],
                        "time_anchor": m.get("time_anchor"),
                    }
                    for m in batch
                ]
                for batch in row["chat"]
            ]
            out.append(
                {
                    "conversation_id": str(row["conversation_id"]),
                    "category": (row.get("conversation_seed") or {}).get("category"),
                    "batches": batches,
                    "questions": questions,
                }
            )
        path = DATA / f"{split}.json"
        path.write_text(json.dumps(out, ensure_ascii=False) + "\n", encoding="utf-8")
        n = sum(len(c["questions"]) for c in out)
        print(f"{split}: {len(out)} conversations, {n} questions -> {path}")


if __name__ == "__main__":
    main()
