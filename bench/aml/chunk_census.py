"""How often AML's Add chunking splits a user/assistant round.

AML's API guide: "Ordinary Textual splits deterministically at either 20
messages or 2,000 Adapter-counted words." The local harness sends whole
sessions, so this counts, per dataset, the chunks after the first that
open on an assistant message, i.e. the rounds whose question and answer
arrive in different Add requests. Word counting is whitespace splitting;
AML's own counter is unpublished. No model calls.

    .venv/bin/python bench/aml/chunk_census.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aml import run  # noqa: E402


def aml_chunks(
    messages: list[dict[str, Any]], max_msgs: int = 20, max_words: int = 2000
) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    words = 0
    for m in messages:
        n = len(str(m.get("content", "")).split())
        if cur and (len(cur) >= max_msgs or words + n > max_words):
            out.append(cur)
            cur, words = [], 0
        cur.append(m)
        words += n
    if cur:
        out.append(cur)
    return out


def census(sessions: Iterable[list[dict[str, Any]]]) -> dict[str, Any]:
    n_sessions = n_chunks = n_split = n_rounds = 0
    for msgs in sessions:
        n_sessions += 1
        chunks = aml_chunks(msgs)
        n_chunks += len(chunks)
        n_split += sum(1 for c in chunks[1:] if c and c[0].get("role") == "assistant")
        n_rounds += sum(1 for m in msgs if m.get("role") == "user")
    return {
        "sessions": n_sessions,
        "chunks": n_chunks,
        "split_rounds": n_split,
        "rounds": n_rounds,
        "split_share": round(n_split / n_rounds, 4) if n_rounds else 0.0,
    }


def main() -> None:
    corpus = json.loads(run.CORPUS.read_text(encoding="utf-8"))
    dev = set(run.split(corpus)[0])
    out = {
        "longmemeval-s-dev": census(
            s for q in corpus if q["question_id"] in dev for s in q["haystack_sessions"]
        )
    }
    _, sessions = run.load_locomo()
    out["locomo-refined"] = census(c["messages"] for v in sessions.values() for c in v)
    _, sessions = run.load_beam("1M")
    out["beam-1m"] = census(c["messages"] for v in sessions.values() for c in v)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
