"""Recall-gate dataset from session transcripts (read-only). The output holds private prompts and memory bodies: write it under bench/decisions/data, which git ignores, never anywhere tracked.

Within one transcript the sequence is exact: user prompt -> memory_search(query)
-> result hits (id, score, relevance, snippet) -> ... -> memory_record_use(ids,
outcome). Each (record_use, id) pair becomes one labelled instance with the
prompt and query that led to it and the lexical baseline's score for it.

    python bench/decisions/recall_dataset.py OUTDIR [--until 2026-09-25T19:49:00Z] [--transcripts GLOB] [--store DIR]

--until drops every transcript row stamped after that instant, so a set
built earlier can be rebuilt row for row from transcripts that have grown
since.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ULID = re.compile(r"\b01[0-9A-HJKMNP-TV-Z]{25}\b")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_store(root: Path) -> dict[str, dict]:
    store: dict[str, dict] = {}
    for p in list(root.glob("*.md")) + list((root / ".tombstones").glob("*.md")):
        text = p.read_text(errors="replace")
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n", 4)
        if end < 0:
            continue
        try:
            fm = yaml.safe_load(text[4:end]) or {}
        except Exception:
            continue
        mid = fm.get("id")
        if mid:
            store[str(mid)] = {
                "body": text[end + 5 :].strip(),
                "scopes": fm.get("scopes") or [],
                "tombstoned": ".tombstones" in str(p),
            }
    return store


def user_text(row: dict) -> str:
    c = (row.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def build(
    transcripts: list[str], store: dict[str, dict], until: datetime | None = None
) -> tuple[list[dict], list[dict], collections.Counter, collections.Counter]:
    """(hit rows, record_use rows, stats, per-transcript counts) over the transcripts."""
    rows_out: list[dict] = []
    hits_rows: list[dict] = []
    stats: collections.Counter = collections.Counter()
    per_transcript: collections.Counter = collections.Counter()
    for tp in sorted(transcripts):
        project = os.path.basename(os.path.dirname(tp))
        tname = os.path.basename(tp)
        last_prompt = None  # {"text", "ts"}
        searches: list[dict] = []
        pending: dict = {}  # tool_use_id -> search dict awaiting its result
        injections: list[dict] = []
        try:
            fh = open(tp, errors="replace")
        except Exception:
            continue
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if until is not None:
                ts = parse_ts(row.get("timestamp"))
                if ts is not None and ts > until:
                    stats["rows_after_cutoff"] += 1
                    continue
            t = row.get("type")
            msg = row.get("message") or {}
            if t == "user":
                c = msg.get("content")
                if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c
                ):
                    for b in c:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        s = pending.pop(b.get("tool_use_id"), None)
                        if s is None:
                            continue
                        body = b.get("content")
                        text = (
                            body
                            if isinstance(body, str)
                            else "".join(
                                x.get("text", "") for x in body if isinstance(x, dict)
                            )
                            if isinstance(body, list)
                            else ""
                        )
                        try:
                            parsed = json.loads(text)
                        except Exception:
                            parsed = None
                        hits = {}
                        if isinstance(parsed, dict):
                            for rank, h in enumerate(parsed.get("result") or []):
                                if isinstance(h, dict) and h.get("id"):
                                    hits[h["id"]] = {
                                        "rank": rank,
                                        "score": h.get("score"),
                                        "relevance": h.get("relevance"),
                                        "snippet": (h.get("snippet") or "")[:300],
                                        "verdict": h.get("staleness_verdict"),
                                    }
                        s["hits"] = hits
                        s["n"] = len(hits)
                        searches.append(s)
                    continue
                txt = user_text(row)
                if not txt.strip():
                    continue
                if (
                    txt.startswith("<system-reminder>")
                    or txt.startswith("<local-command")
                    or txt.startswith("[Request interrupted")
                ):
                    continue
                ids = ULID.findall(txt)
                if (
                    ids
                    and ("memory_show" in txt or "bettermemory" in txt.lower())
                    and (txt.lstrip().startswith("<") or "hook" in txt[:200].lower())
                ):
                    injections.append(
                        {
                            "ids": ids,
                            "prompt": last_prompt["text"] if last_prompt else None,
                            "ts": row.get("timestamp"),
                        }
                    )
                    stats["injection_rows"] += 1
                    continue
                last_prompt = {"text": txt, "ts": row.get("timestamp")}
            elif t == "assistant":
                for b in msg.get("content") or []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = str(b.get("name", ""))
                    inp = b.get("input") or {}
                    if name.endswith("memory_search"):
                        q = inp.get("query")
                        if isinstance(q, str):
                            pending[b.get("id")] = {
                                "tool_use_id": b.get("id"),
                                "query": q,
                                "prompt": last_prompt["text"] if last_prompt else None,
                                "prompt_ts": last_prompt["ts"] if last_prompt else None,
                                "ts": row.get("timestamp"),
                                "hits": {},
                            }
                    elif name.endswith("memory_show"):
                        sid = inp.get("id")
                        for s in reversed(searches):
                            if sid in s.get("hits", {}):
                                s["hits"][sid]["opened"] = True
                                s["hits"][sid]["opened_ts"] = row.get("timestamp")
                                break
                        else:
                            for inj in reversed(injections):
                                if sid in inj["ids"]:
                                    inj.setdefault("opened", set()).add(sid)
                                    break
                    elif name.endswith("memory_record_use"):
                        outcome = inp.get("outcome")
                        ids = inp.get("memory_ids") or []
                        excerpts = inp.get("claim_excerpts") or []
                        for j, mid in enumerate(ids):
                            src = None
                            for s in reversed(searches):
                                if mid in s.get("hits", {}):
                                    src = ("search", s)
                                    break
                            if src is None:
                                for inj in reversed(injections):
                                    if mid in inj["ids"]:
                                        src = ("injection", inj)
                                        break
                            mem = store.get(mid)
                            inst = {
                                "project": project,
                                "transcript": tname,
                                "ts": row.get("timestamp"),
                                "memory_id": mid,
                                "outcome": outcome,
                                "note": (inp.get("note") or "")[:400],
                                "excerpt": (excerpts[j] if j < len(excerpts) else None),
                                "body": mem["body"] if mem else None,
                                "scopes": mem["scopes"] if mem else None,
                                "tombstoned": mem["tombstoned"] if mem else None,
                                "model": msg.get("model"),
                            }
                            if src and src[0] == "search":
                                s = src[1]
                                h = s["hits"][mid]
                                inst.update(
                                    {
                                        "source": "search",
                                        "prompt": s["prompt"],
                                        "query": s["query"],
                                        "rank": h["rank"],
                                        "n_hits": s["n"],
                                        "score": h["score"],
                                        "relevance": h["relevance"],
                                        "snippet": h["snippet"],
                                        "verdict": h["verdict"],
                                    }
                                )
                            elif src:
                                inst.update(
                                    {
                                        "source": "injection",
                                        "prompt": src[1]["prompt"],
                                        "query": None,
                                        "rank": 0,
                                        "n_hits": len(src[1]["ids"]),
                                        "score": None,
                                        "relevance": None,
                                    }
                                )
                            else:
                                inst.update(
                                    {
                                        "source": "none",
                                        "prompt": last_prompt["text"]
                                        if last_prompt
                                        else None,
                                        "query": None,
                                        "rank": None,
                                        "n_hits": None,
                                        "score": None,
                                        "relevance": None,
                                    }
                                )
                            rows_out.append(inst)
                            stats[f"{inst['source']}:{outcome}"] += 1
                            per_transcript[tname] += 1
        fh.close()

        # behavioural rows: every hit of every search in this transcript, labelled opened / explicit outcome
        explicit = {
            (r["memory_id"], r.get("query")): r["outcome"]
            for r in rows_out
            if r["transcript"] == tname and r["source"] == "search"
        }
        for s in searches:
            for mid, h in s["hits"].items():
                mem = store.get(mid)
                hits_rows.append(
                    {
                        "project": project,
                        "transcript": tname,
                        "ts": s["ts"],
                        "memory_id": mid,
                        "prompt": s["prompt"],
                        "query": s["query"],
                        "rank": h["rank"],
                        "n_hits": s["n"],
                        "score": h["score"],
                        "relevance": h["relevance"],
                        "snippet": h["snippet"],
                        "verdict": h["verdict"],
                        "opened": bool(h.get("opened")),
                        "explicit": explicit.get((mid, s["query"])),
                        "body": mem["body"] if mem else None,
                        "scopes": mem["scopes"] if mem else None,
                        "tombstoned": mem["tombstoned"] if mem else None,
                    }
                )
    return hits_rows, rows_out, stats, per_transcript


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument(
        "--until",
        default=None,
        help="drop transcript rows stamped after this ISO instant",
    )
    ap.add_argument(
        "--transcripts", default=os.path.expanduser("~/.claude/projects/*/*.jsonl")
    )
    ap.add_argument("--store", default=str(Path.home() / ".claude-memory"))
    a = ap.parse_args()
    until = parse_ts(a.until)
    if a.until and until is None:
        raise SystemExit(f"--until is not an ISO instant: {a.until}")
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    store = load_store(Path(a.store))
    hits_rows, rows_out, stats, per_transcript = build(
        glob.glob(a.transcripts), store, until
    )

    with open(out / "recall_hits.jsonl", "w") as fh:
        for r in hits_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    lab: collections.Counter = collections.Counter()
    for r in hits_rows:
        lab[("opened" if r["opened"] else "not_opened", r["explicit"])] += 1
    print(
        "until:",
        until.isoformat() if until else None,
        "rows after cutoff dropped:",
        stats["rows_after_cutoff"],
    )
    print("hit rows:", len(hits_rows), "labels {(opened, explicit): n}:", dict(lab))
    pos = [r for r in hits_rows if r["opened"] or r["explicit"] == "applied"]
    neg = [r for r in hits_rows if (not r["opened"]) and r["explicit"] == "ignored"]
    print(
        "positives (opened or applied):",
        len(pos),
        "negatives (ignored, not opened):",
        len(neg),
        "distinct memories:",
        len({r["memory_id"] for r in pos + neg}),
        "transcripts:",
        len({r["transcript"] for r in pos + neg}),
    )
    byrel: dict = collections.defaultdict(lambda: [0, 0])
    for r in pos:
        byrel[r["relevance"]][0] += 1
    for r in neg:
        byrel[r["relevance"]][1] += 1
    print("baseline by relevance {label: [pos, neg]}:", dict(byrel))
    byrank: dict = collections.defaultdict(lambda: [0, 0])
    for r in pos:
        byrank[min(r["rank"], 5)][0] += 1
    for r in neg:
        byrank[min(r["rank"], 5)][1] += 1
    print("baseline by rank {rank: [pos, neg]}:", dict(sorted(byrank.items())))
    print(
        "top-1 opened rate over all searches:",
        round(
            sum(1 for r in hits_rows if r["rank"] == 0 and r["opened"])
            / max(1, sum(1 for r in hits_rows if r["rank"] == 0)),
            3,
        ),
        "of",
        sum(1 for r in hits_rows if r["rank"] == 0),
        "searches with hits",
    )

    with open(out / "recall_pairs_v2.jsonl", "w") as fh:
        for r in rows_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("instances:", len(rows_out))
    print(dict(sorted(stats.items())))
    paired = [
        r
        for r in rows_out
        if r["source"] == "search" and r["outcome"] in ("applied", "ignored")
    ]
    print(
        "search-paired applied/ignored:",
        len(paired),
        "with body:",
        sum(bool(r["body"]) for r in paired),
        "with prompt:",
        sum(bool(r["prompt"]) for r in paired),
    )
    if paired:
        print(
            "distinct memories:",
            len({r["memory_id"] for r in paired}),
            "transcripts:",
            len({r["transcript"] for r in paired}),
            "span:",
            min(r["ts"] for r in paired),
            "->",
            max(r["ts"] for r in paired),
        )
    models = collections.Counter(r["model"] for r in paired)
    print("labelling models:", models.most_common(6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
