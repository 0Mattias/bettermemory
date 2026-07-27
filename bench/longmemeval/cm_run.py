"""LongMemEval — the claude-mem arms.

Companion to `run.py`, which measures bettermemory. Same corpus, same
labels, and the SAME attribution rule fixed in PREREGISTRATION.md:
one item per conversational round, rank items, map each to its parent
session, dedup preserving first occurrence, take the first k DISTINCT
sessions, score against `answer_session_ids`.

WHAT DRIVES WHAT.

  ingest    `cm_ingest.js` under bun -> SessionStore.importObservation,
            one observation per round, `memory_session_id` = the
            LongMemEval session id. claude-mem has a native session
            column, so the label association is theirs, not imposed.

  semantic  GET /api/search        (unified; ChromaDB, their DEFAULT)
  lexical   GET /api/search/observations  (FTS5)

Both are real routes on their worker, and the lexical one calls the same
`sessionSearch.searchObservations` the unified endpoint falls back to.
Driving each arm through its own route is deliberate: there is no
supported switch that disables Chroma, and breaking `uvx` to force the
fallback proved unreliable (a cached uv environment still resolves).

THE 90-DAY RECENCY WINDOW, AND WHY THE HARNESS WIDENS IT.
`performChromaSemanticSearch` applies `Date.now() - RECENCY_WINDOW_MS`
when the caller passes no date range. LongMemEval's corpus is dated
2023-05, so EVERY semantic match is discarded before it reaches the
store lookup and claude-mem scores 0.0 on every question — for a reason
that has nothing to do with retrieval quality. The harness therefore
passes an explicit `dateStart`/`dateEnd` spanning the corpus.
bettermemory has no comparable filter, so there is nothing symmetric to
apply on our side. Publishing the un-widened number would not be a weak
result for them, it would be a false accusation.

(Note the parameter names: `dateStart`/`dateEnd`. `startDate`/`endDate`
are accepted and silently ignored, which is how this stayed hidden
through several probes.)

ISOLATION IS BY PROJECT. One worker serves every question; each
question's haystack is ingested under `project = <question_id>` and every
query filters on it. Per-question data directories would mean a ~20 s
worker boot per question.

Usage:

    python bench/longmemeval/cm_run.py --limit 20
    python bench/longmemeval/cm_run.py --limit 20 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
DEFAULT_CORPUS = _HERE / "data" / "longmemeval_s_cleaned.json"
INGEST = _HERE / "cm_ingest.js"
WORKER = _HERE / "vendor" / "package" / "plugin" / "scripts" / "worker-service.cjs"

K_VALUES = (1, 5, 10)
RETRIEVAL_DEPTH = 200

# Spans the corpus (2023-05) with room to spare. See the module docstring:
# without this the semantic arm returns nothing at all.
DATE_START = "2020-01-01"
DATE_END = "2030-01-01"

_ID_RE = re.compile(r"#(\d+)")


def rounds_of(session: list[dict[str, Any]]) -> list[str]:
    """Identical pairing to run.py — the two arms must see one corpus."""
    out: list[str] = []
    i = 0
    turns = list(session)
    while i < len(turns):
        parts = [f"{turns[i].get('role', '?')}: {turns[i].get('content', '')}"]
        if i + 1 < len(turns) and turns[i + 1].get("role") != turns[i].get("role"):
            parts.append(
                f"{turns[i + 1].get('role', '?')}: {turns[i + 1].get('content', '')}"
            )
            i += 2
        else:
            i += 1
        out.append("\n".join(parts))
    return out


def ingest(data_dir: Path, project: str, inst: dict[str, Any]) -> dict[str, Any]:
    dates = inst.get("haystack_dates") or []
    sessions = [
        {
            "session_id": sid,
            "date": dates[idx] if idx < len(dates) else None,
            "rounds": rounds_of(sess),
        }
        for idx, (sid, sess) in enumerate(
            zip(inst["haystack_session_ids"], inst["haystack_sessions"])
        )
    ]
    job = {"dataDir": str(data_dir), "project": project, "sessions": sessions}
    proc = subprocess.run(
        ["bun", str(INGEST)],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ingest failed: {proc.stderr[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class Worker:
    """Boots claude-mem's worker from a NEUTRAL cwd.

    Starting it inside their own package directory makes it infer the
    project from there. That produced a `cm__claude-mem` Chroma
    collection and cost a long detour, so the neutral cwd is deliberate.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.cwd = Path(tempfile.mkdtemp(prefix="cm-neutral-"))
        self.proc: subprocess.Popen[bytes] | None = None
        self.port: int | None = None

    def start(self, timeout: float = 90.0) -> None:
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_MEM_DATA_DIR": str(self.data_dir),
            "CLAUDE_MEM_LOG_LEVEL": "error",
        }
        self.proc = subprocess.Popen(
            ["bun", str(WORKER)],
            cwd=self.cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        settings = self.data_dir / "settings.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            port = 37701
            if settings.exists():
                try:
                    port = int(
                        json.loads(settings.read_text()).get(
                            "CLAUDE_MEM_WORKER_PORT", 37701
                        )
                    )
                except Exception:
                    port = 37701
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=3
                ) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        self.port = port
                        return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("worker did not become healthy")

    def get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"http://127.0.0.1:{self.port}{path}?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.loads(r.read())

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.cwd, ignore_errors=True)

    def await_chroma_backfill(
        self, expected: int, *, timeout: float = 28800.0, quiet_for: float = 900.0
    ) -> tuple[int, bool]:
        """Block until ChromaDB has embedded the ingested corpus.

        THIS IS NOT OPTIONAL AND IT IS NOT A PERFORMANCE TWEAK. The
        worker backfills Chroma asynchronously after boot. A fixed sleep
        (the first version used 20 s for 10,104 observations) means most
        projects are queried before they have been embedded, every one of
        those returns empty, and the arm reports a near-zero score that
        looks like a product defect and is entirely an artifact of this
        harness. Publishing that number would be a false accusation, so
        readiness is measured rather than assumed.

        Returns (embedded, complete). `complete` is False when the count
        plateaued below `expected` or the timeout hit — the caller must
        surface that instead of quietly scoring a partial index.

        `quiet_for` is 15 MINUTES, not seconds-scale, and that is
        deliberate. The worker backfills in periodic cycles with real
        gaps between them, and the rate decays as collections grow (~10/s
        early, ~5/s late across 500 projects). A 90 s plateau threshold
        looked generous and still tripped at 70,537 of 124,361 on the
        first full run — the guard correctly refused to publish, but the
        run was wasted. Patience here is cheaper than a re-run.
        """
        import sqlite3

        chroma_db = self.data_dir / "chroma" / "chroma.sqlite3"
        deadline = time.time() + timeout
        last_count = -1
        last_change = time.time()
        while time.time() < deadline:
            count = 0
            if chroma_db.exists():
                try:
                    con = sqlite3.connect(f"file:{chroma_db}?mode=ro", uri=True)
                    count = con.execute("SELECT count(*) FROM embeddings").fetchone()[0]
                    con.close()
                except Exception:
                    count = last_count if last_count > 0 else 0
            if count >= expected:
                return count, True
            if count != last_count:
                last_count = count
                last_change = time.time()
                print(
                    f"  chroma backfill {count:,}/{expected:,}",
                    file=sys.stderr,
                )
            elif time.time() - last_change > quiet_for:
                return count, False
            time.sleep(10)
        return max(last_count, 0), False


def semantic_sessions(w: Worker, project: str, question: str) -> list[str]:
    """Unified endpoint. Returns ranked distinct session ids."""
    d = w.get(
        "/api/search",
        {
            "query": question,
            "project": project,
            "format": "json",
            "limit": RETRIEVAL_DEPTH,
            "dateStart": DATE_START,
            "dateEnd": DATE_END,
        },
    )
    out: list[str] = []
    for o in d.get("observations", []) if isinstance(d, dict) else []:
        sid = o.get("memory_session_id")
        if sid and sid not in out:
            out.append(sid)
    return out


def lexical_sessions(
    w: Worker, project: str, question: str, id_to_session: dict[int, str]
) -> list[str]:
    """FTS5 endpoint. Returns a markdown table; ids map back via SQLite."""
    d = w.get(
        "/api/search/observations",
        {
            "query": question,
            "project": project,
            "limit": RETRIEVAL_DEPTH,
            "dateStart": DATE_START,
            "dateEnd": DATE_END,
        },
    )
    text = ""
    if isinstance(d, dict):
        if isinstance(d.get("observations"), list):
            out: list[str] = []
            for o in d["observations"]:
                sid = o.get("memory_session_id")
                if sid and sid not in out:
                    out.append(sid)
            return out
        content = d.get("content") or []
        if content:
            text = content[0].get("text", "")
    out = []
    for m in _ID_RE.finditer(text):
        sid = id_to_session.get(int(m.group(1)))
        if sid and sid not in out:
            out.append(sid)
    return out


@dataclass
class ArmResult:
    arm: str
    n: int = 0
    macro: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    hit: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    ceiling: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    total_evidence: int = 0
    empty: int = 0
    by_type: dict[str, dict[int, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    type_n: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seconds: float = 0.0

    def recall_macro(self, k: int) -> float:
        return self.macro[k] / self.n if self.n else 0.0

    def recall_micro(self, k: int) -> float:
        return self.hit[k] / self.total_evidence if self.total_evidence else 0.0

    def ceiling_at(self, k: int) -> float:
        return self.ceiling[k] / self.n if self.n else 0.0


def main() -> int:
    p = argparse.ArgumentParser(description="claude-mem arms on LongMemEval.")
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--arms", default="semantic,lexical")
    p.add_argument("--json", action="store_true")
    p.add_argument("--keep", action="store_true", help="Keep the data dir.")
    args = p.parse_args()

    corpus_path = Path(args.corpus)
    if not corpus_path.is_absolute():
        corpus_path = (_HERE / corpus_path).resolve()
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    total = len(corpus)
    if args.limit:
        corpus = corpus[: args.limit]

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    data_dir = Path(tempfile.mkdtemp(prefix="cm-lme-data-"))
    notes: list[str] = [
        "ingest bypasses claude-mem's hook pipeline (SessionStore.importObservation); "
        "their observations_fts spans six columns and their own pipeline fills all "
        "six via LLM extraction — this harness fills `text` only, which may "
        "UNDERSTATE claude-mem. See PREREGISTRATION.md addendum 2.",
        f"semantic arm passes explicit dateStart={DATE_START}/dateEnd={DATE_END}; "
        "without it their default 90-day recency window discards every match on "
        "this historical corpus and the arm scores 0.0 for reasons unrelated to "
        "retrieval.",
    ]
    if args.limit and args.limit < total:
        notes.append(
            f"SUBSET — first {len(corpus)} of {total}, not a stratified sample. "
            "Question-type mix is skewed; not publishable."
        )

    ingest_stats = {"rounds_offered": 0, "items_written": 0}
    meta_chroma: dict[str, object] = {"embedded": 0, "complete": False}
    id_maps: dict[str, dict[int, str]] = {}

    print(f"ingesting {len(corpus)} questions into {data_dir} ...", file=sys.stderr)
    for i, inst in enumerate(corpus):
        st = ingest(data_dir, inst["question_id"], inst)
        ingest_stats["rounds_offered"] += st["rounds_offered"]
        ingest_stats["items_written"] += st["items_written"]
        if (i + 1) % 5 == 0:
            print(f"  ingested {i + 1}/{len(corpus)}", file=sys.stderr)

    # observation id -> session id, read straight from their SQLite. Used
    # only by the lexical arm, whose route returns a formatted table.
    import sqlite3

    con = sqlite3.connect(data_dir / "claude-mem.db")
    for project, oid, sid in con.execute(
        "SELECT project, id, memory_session_id FROM observations"
    ):
        id_maps.setdefault(project, {})[oid] = sid
    con.close()

    w = Worker(data_dir)
    rows: list[ArmResult] = []
    try:
        print("starting worker ...", file=sys.stderr)
        w.start()
        print(f"worker healthy on :{w.port}", file=sys.stderr)
        # Measured, never assumed — see await_chroma_backfill's docstring.
        embedded, complete = w.await_chroma_backfill(ingest_stats["items_written"])
        print(
            f"chroma: {embedded:,}/{ingest_stats['items_written']:,} embedded "
            f"({'complete' if complete else 'INCOMPLETE'})",
            file=sys.stderr,
        )
        meta_chroma = {"embedded": embedded, "complete": complete}
        if not complete:
            notes.append(
                f"CHROMA BACKFILL INCOMPLETE — {embedded:,} of "
                f"{ingest_stats['items_written']:,} observations embedded when "
                "querying began. The semantic arm is scoring a partially built "
                "index and its number is NOT a claude-mem result."
            )

        for arm in arms:
            res = ArmResult(arm=arm)
            started = time.time()
            for i, inst in enumerate(corpus):
                evidence = set(inst["answer_session_ids"])
                if not evidence:
                    continue
                project = inst["question_id"]
                try:
                    if arm == "semantic":
                        ranked = semantic_sessions(w, project, inst["question"])
                    else:
                        ranked = lexical_sessions(
                            w, project, inst["question"], id_maps.get(project, {})
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{arm}] {project} FAILED: {exc}", file=sys.stderr)
                    ranked = []
                if not ranked:
                    res.empty += 1
                res.n += 1
                res.total_evidence += len(evidence)
                qtype = inst.get("question_type", "unknown")
                res.type_n[qtype] += 1
                for k in K_VALUES:
                    got = set(ranked[:k]) & evidence
                    recall = len(got) / len(evidence)
                    res.macro[k] += recall
                    res.hit[k] += len(got)
                    res.ceiling[k] += min(k, len(evidence)) / len(evidence)
                    res.by_type[qtype][k] += recall
                if (i + 1) % 5 == 0:
                    print(
                        f"  [{arm}] {i + 1}/{len(corpus)} "
                        f"macro@5={res.recall_macro(5):.3f}",
                        file=sys.stderr,
                    )
            res.seconds = time.time() - started
            rows.append(res)
    finally:
        w.stop()
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)

    shortfall = 1 - ingest_stats["items_written"] / max(
        1, ingest_stats["rounds_offered"]
    )
    meta = {
        "corpus": corpus_path.name,
        "scored": rows[0].n if rows else 0,
        "instances": total,
        "retrieval_depth": RETRIEVAL_DEPTH,
        "ingest": {**ingest_stats, "shortfall": round(shortfall, 5)},
        "chroma": meta_chroma,
        "notes": notes,
    }

    if args.json:
        print(
            json.dumps(
                {
                    **meta,
                    "results": [
                        {
                            "arm": r.arm,
                            "n": r.n,
                            "seconds": round(r.seconds, 1),
                            "empty_result_questions": r.empty,
                            "macro": {
                                str(k): round(r.recall_macro(k), 4) for k in K_VALUES
                            },
                            "micro": {
                                str(k): round(r.recall_micro(k), 4) for k in K_VALUES
                            },
                            "ceiling": {
                                str(k): round(r.ceiling_at(k), 4) for k in K_VALUES
                            },
                            "by_type": {
                                t: {
                                    str(k): round(r.by_type[t][k] / r.type_n[t], 4)
                                    for k in K_VALUES
                                }
                                for t in sorted(r.type_n)
                            },
                            "type_n": dict(r.type_n),
                        }
                        for r in rows
                    ],
                },
                indent=2,
            )
        )
    else:
        print()
        print(f"corpus: {meta['corpus']}  scored: {meta['scored']}/{total}")
        print(
            f"ingest: {ingest_stats['items_written']:,} items from "
            f"{ingest_stats['rounds_offered']:,} rounds "
            f"({100 * shortfall:.3f}% shortfall)"
        )
        print()
        print(
            "| arm       |      @1      |      @5      |     @10      |   n | empty |"
        )
        print(
            "|-----------|--------------|--------------|--------------|-----|-------|"
        )
        for r in rows:
            cells = "".join(
                f"| {100 * r.recall_macro(k):>5.1f}% [{100 * r.ceiling_at(k):>4.0f}%] "
                for k in K_VALUES
            )
            print(f"| {r.arm:<9} {cells}| {r.n:>3} | {r.empty:>5} |")
        for note in notes:
            print(f"\nnote: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
