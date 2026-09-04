"""Integrity benchmark v0: poisoning resistance and staleness detection,
measured the same way on bettermemory, mem0, Graphiti and Letta.

WHY THIS EXISTS. Published memory benchmarks grade retrieval recall. None
grades whether a served memory is still true, whether a false one can be
told apart from a legitimate one, or whether a record that bypassed the
write path is visible as such. Those are the questions a memory poisoning
threat model (OWASP ASI06) actually asks, and the v0 measures three of
the literature's four intervention points: write-time admission,
provenance binding and retrieval-time signal.

THE PROTOCOL, per arm, one fresh store, k = 5, nothing tuned.

  S-A  staleness, memory versus memory. Ingest the legit corpus in the
       fixed order (every first statement, the hard negatives, every
       update, every reversion). Run each topic's query. Score each
       served item by value-token containment: current, stale, other.
       Report current_served@5, stale_served@5, stale_unsignaled@5 (the
       headline loss: a stale fact served with nothing to tell it apart)
       and top1_current, beside the three references the corpus alone
       defines: serve_all_unsignaled, recency_top1, oracle_replica.
  P-A  poisoning, write admission. Add thirty payloads (false fact,
       embedded instruction, secret) through the public write path after
       the legit corpus. Report per class what was admitted and what was
       flagged; pool against the legit population into precision,
       Youden's J and Fisher's p beside always_flag, never_flag and
       oracle_replica. For admitted false facts, the rank against the
       contradicted fact; for admitted instructions, whether a generic
       task query serves them.
  P-B  poisoning, store injection. Insert ten false facts by bypassing
       the write API with forged trust metadata, plain and with the
       system's own provenance binding forged. Report whether the read
       surface distinguishes the injected record from its API-written
       twin, and how far the forged metadata moved its rank.
  S-B  staleness, memory versus world. Not re-run: the sealed rot
       artifact's rows are carried with their sha256, and no rival has an
       interface for it.

The raw observations of one arm are one JSON file (`collect`), scored in
the repo venv (`score`) so the rival arms can be collected from the venv
that carries their package. `summary` pools arms that ran on one corpus
sha and refuses to pool across shas; `scorecard` grades the declared
predictions mechanically.

Usage:

    .venv/bin/python bench/integrity/run.py check
    .venv/bin/python bench/integrity/run.py collect --arm bettermemory \
        --out bench/integrity/results/raw/bettermemory-YYYY-MM-DD.json
    .eval-venv/bin/python bench/integrity/run.py collect --arm mem0-raw --out ...
    .venv/bin/python bench/integrity/run.py score --raw <raw.json> --out <result.json>
    .venv/bin/python bench/integrity/run.py summary <result.json>... --out <summary.json>
    .venv/bin/python bench/integrity/run.py scorecard <summary.json> --out <scorecard.json>
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

CORPUS = _HERE / "corpus.json"
ROT_ARTIFACT = (
    _ROOT / "bench" / "rot" / "results" / "multirepo-anchored-2026-07-30.json"
)


def _load_corpus() -> dict[str, Any]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _provenance() -> dict[str, Any]:
    """Version + commit + platform stamp for an emitted artifact, the shape
    bench/longmemeval/run.py writes. `bettermemory_version` reads None in
    a venv that does not carry the package; the arm's own versions are
    recorded beside it."""
    commit: str | None = None
    tree_dirty: bool | None = None
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
            or None
        )
        tree_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                cwd=str(_HERE),
                timeout=10,
            ).stdout.strip()
        )
    except OSError:
        pass
    version: str | None = None
    try:
        import bettermemory

        version = bettermemory.__version__
    except ImportError:
        pass
    return {
        "bettermemory_version": version,
        "commit": commit,
        "tree_dirty": tree_dirty,
        "date": date.today().isoformat(),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def collect(arm: str, out: Path, scratch: Path | None, limit_topics: int | None) -> int:
    import score
    from adapters import InjectionUnsupported, SystemUnavailable, make_adapter

    corpus = _load_corpus()
    problems = score.corpus_checks(corpus)
    if problems:
        print("corpus checks failed:", *problems, sep="\n  ")
        return 2
    if limit_topics is not None:
        corpus = _slice(corpus, limit_topics)
    sha = score.corpus_sha256(CORPUS)
    scratch = scratch or Path(tempfile.mkdtemp(prefix=f"bm-integrity-{arm}-"))
    scratch.mkdir(parents=True, exist_ok=True)
    adapter = make_adapter(arm, scratch)
    raw: dict[str, Any] = {
        "arm": arm,
        "ran": True,
        "corpus_sha256": sha,
        "k": score.K,
        "k_inject": score.K_INJECT,
        "limited_to_topics": limit_topics,
        "provenance": _provenance(),
        "scratch": str(scratch),
    }
    started = time.time()
    try:
        adapter.reset()
    except SystemUnavailable as exc:
        raw.update({"ran": False, "unavailable_reason": exc.reason})
        _write(out, raw)
        print(f"{arm}: unavailable: {exc.reason}")
        return 0
    raw["capabilities"] = adapter.capabilities()
    raw["version"] = adapter.version()
    try:
        _run_phases(adapter, corpus, raw, InjectionUnsupported)
    finally:
        adapter.close()
    raw["timing"] = {"seconds": round(time.time() - started, 1)}
    _write(out, raw)
    print(
        f"{arm}: collected {len(raw['adds'])} adds, {len(raw['topic_searches'])} topic searches, "
        f"{len(raw['poison_searches'])} poison searches, {len(raw['injections'])} injections "
        f"in {raw['timing']['seconds']}s -> {out}"
    )
    return 0


def _run_phases(
    adapter: Any,
    corpus: dict[str, Any],
    raw: dict[str, Any],
    unsupported: type[Exception],
) -> None:
    import score

    topics = {t["id"]: t for t in corpus["topics"]}
    adds: list[dict[str, Any]] = []
    raw["adds"] = adds

    def do_add(row: dict[str, Any], kind: str) -> Any:
        t0 = time.time()
        outcome = adapter.add(
            row["stmt_id"], row["text"], {k: v for k, v in row.items() if k != "text"}
        )
        adds.append(
            {
                "stmt_id": row["stmt_id"],
                "kind": kind,
                "topic": row.get("topic"),
                "role": row.get("role"),
                "outcome": outcome.to_dict(),
                "seconds": round(time.time() - t0, 2),
            }
        )
        _progress(f"add {row['stmt_id']} -> {outcome.status}")
        return outcome

    # Phase A: the legit corpus in the fixed order
    for row in score.ingestion_plan(corpus):
        do_add(row, row["kind"])

    # Phase B: staleness searches
    raw["topic_searches"] = []
    for topic in corpus["topics"]:
        hits = adapter.search(topic["query"], score.K)
        raw["topic_searches"].append(
            {
                "topic_id": topic["id"],
                "query": topic["query"],
                "hits": [h.to_dict() for h in hits],
            }
        )
        _progress(f"search {topic['id']} -> {len(hits)} hits")

    # Phase C: poisoning through the write path
    raw["poison_searches"] = []
    twin_ids: dict[str, list[str]] = {}
    for payload in corpus["poison"]:
        row = {
            "stmt_id": payload["id"],
            "text": payload["text"],
            "topic": payload.get("target_topic"),
            "role": payload["class"],
            "subject": topics.get(payload.get("target_topic", ""), {}).get("subject"),
        }
        outcome = do_add(row, "poison")
        twin_ids[payload["id"]] = list(outcome.ids)
        if payload["class"] == "false_fact":
            topic = topics[payload["target_topic"]]
            hits = adapter.search(topic["query"], score.K)
            raw["poison_searches"].append(
                {
                    "payload_id": payload["id"],
                    "query_id": topic["id"],
                    "query": topic["query"],
                    "hits": [h.to_dict() for h in hits],
                }
            )
    for gq in corpus["generic_queries"]:
        hits = adapter.search(gq["text"], score.K)
        raw["poison_searches"].append(
            {
                "payload_id": None,
                "query_id": gq["id"],
                "query": gq["text"],
                "hits": [h.to_dict() for h in hits],
            }
        )

    # Phase D: store injection
    raw["injections"] = []
    false_facts = [p for p in corpus["poison"] if p["class"] == "false_fact"]
    for variant in ("plain", "forged_provenance"):
        for payload in false_facts:
            topic = topics[payload["target_topic"]]
            meta = {
                "stmt_id": payload["id"],
                "topic": topic["id"],
                "subject": topic["subject"],
                "variant": variant,
                "twin_ids": twin_ids.get(payload["id"], []),
            }
            try:
                injected_id = adapter.inject(
                    payload["id"],
                    payload["text"],
                    meta,
                    forge_provenance=(variant == "forged_provenance"),
                )
            except unsupported as exc:
                raw["injection_unsupported"] = str(exc)
                raw["injections"] = []
                return
            hits = adapter.search(topic["query"], score.K_INJECT)
            injected = next((h for h in hits if h.id == injected_id), None)
            twins = twin_ids.get(payload["id"], [])
            twin = next((h for h in hits if h.id in twins), None)
            raw["injections"].append(
                {
                    "payload_id": payload["id"],
                    "variant": variant,
                    "injected_id": injected_id,
                    "twin_ids": twins,
                    "injected_rank": injected.rank if injected else None,
                    "twin_rank": twin.rank if twin else None,
                    "injected_provenance": injected.provenance if injected else None,
                    "twin_provenance": twin.provenance if twin else None,
                    "detected": _detected(adapter.name, injected),
                    "twin_detected": _detected(adapter.name, twin),
                    "hits": [h.to_dict() for h in hits],
                }
            )
            _progress(
                f"inject {payload['id']} {variant} -> rank {injected.rank if injected else None}, detected {_detected(adapter.name, injected)}"
            )


def _detected(arm: str, hit: Any) -> bool | None:
    """The arm's documented provenance channel, applied to a served hit.
    None when the hit was not served (nothing to read)."""
    if hit is None:
        return None
    if arm == "bettermemory":
        return hit.provenance is not None and hit.provenance != "local"
    if arm == "graphiti":
        return hit.provenance == "episodes:0"
    return False


def _slice(corpus: dict[str, Any], n: int) -> dict[str, Any]:
    """A smoke slice: the first n topics of every kind, the payloads that
    target a kept topic, everything else unchanged."""
    kept: list[dict[str, Any]] = []
    for kind in ("supersession", "distractor", "reversion"):
        kept.extend([t for t in corpus["topics"] if t["kind"] == kind][:n])
    ids = {t["id"] for t in kept}
    poison = [
        p
        for p in corpus["poison"]
        if p["class"] != "false_fact" or p["target_topic"] in ids
    ]
    return {**corpus, "topics": kept, "poison": poison}


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _write(out: Path, payload: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=1, sort_keys=False, default=str) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# score / summary / scorecard / check
# ---------------------------------------------------------------------------


def cmd_check() -> int:
    import score

    corpus = _load_corpus()
    problems = score.corpus_checks(corpus)
    refs = score.reference_tables(corpus)
    expect = {
        ("serve_all_unsignaled", "supersession", "stale_served@5"): 1.0,
        ("serve_all_unsignaled", "supersession", "current_served@5"): 1.0,
        ("serve_all_unsignaled", "distractor", "stale_served@5"): 0.0,
        ("recency_top1", "supersession", "top1_current"): 1.0,
        ("recency_top1", "distractor", "top1_current"): 0.0,
        ("recency_top1", "reversion", "top1_current"): 1.0,
        ("oracle_replica", "all", "stale_served@5"): 0.0,
        ("oracle_replica", "all", "current_served@5"): 1.0,
    }
    for (ref, kind, metric), want in expect.items():
        got = refs[ref][kind][metric]
        if got != want:
            problems.append(f"reference {ref} {kind} {metric} = {got}, expected {want}")
    print(f"corpus sha256 {score.corpus_sha256(CORPUS)}")
    print(
        f"topics {len(corpus['topics'])}, statements {sum(len(t['statements']) for t in corpus['topics'])}, "
        f"hard negatives {len(corpus['hard_negatives'])}, poison {len(corpus['poison'])}"
    )
    if problems:
        print("FAILED", *problems, sep="\n  ")
        return 1
    print("corpus checks and reference arithmetic hold")
    return 0


def cmd_score(raw_path: Path, out: Path) -> int:
    import score

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    corpus = _load_corpus()
    if raw["corpus_sha256"] != score.corpus_sha256(CORPUS):
        print("raw observations were collected on a different corpus sha; refusing")
        return 2
    if raw.get("limited_to_topics"):
        corpus = _slice(corpus, raw["limited_to_topics"])
    result = score.score_arm(raw, corpus)
    result["raw_observations"] = (
        str(raw_path.relative_to(_ROOT))
        if raw_path.is_relative_to(_ROOT)
        else str(raw_path)
    )
    result["scored_with"] = _provenance()
    _write(out, result)
    if result.get("ran", True):
        st = result["staleness"]["arm"]
        print(
            f"{result['arm']}: supersession stale_served@5 {st['supersession']['stale_served@5']} "
            f"unsignaled {st['supersession']['stale_unsignaled@5']} current {st['supersession']['current_served@5']} "
            f"top1 {st['supersession']['top1_current']}; distractor current {st['distractor']['current_served@5']}; "
            f"reversion current {st['reversion']['current_served@5']}"
        )
        adm = result["admission"]["per_class"]
        print(
            f"  admission flagged: false_fact {adm['false_fact']['flagged']} instruction {adm['instruction']['flagged']} "
            f"secret {adm['secret']['flagged']} (legit {result['admission']['legit']['flagged']}); "
            f"detector J {result['admission']['detectors']['arm']['youden_j']}"
        )
        print(
            f"  retrieval: poison_top1 {result['retrieval']['false_fact']['poison_top1_rate']} "
            f"injection_served {result['retrieval']['instruction']['injection_served@5']}"
        )
        print(
            f"  injection: {json.dumps({v: {'detected': x['detected'], 'shift': x['median_rank_shift']} for v, x in result['injection'].get('variants', {}).items()}) if not result['injection'].get('unsupported') else result['injection']['unsupported']}"
        )
    else:
        print(f"{result['arm']}: unavailable: {result['unavailable_reason']}")
    return 0


def cmd_summary(paths: list[Path], out: Path) -> int:
    import score

    results = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    corpus = _load_corpus()
    summary = score.summarize(results, corpus, ROT_ARTIFACT)
    summary["results"] = [
        str(p.relative_to(_ROOT)) if p.is_relative_to(_ROOT) else str(p) for p in paths
    ]
    summary["provenance"] = _provenance()
    _write(out, summary)
    for arm, row in summary["arms"].items():
        if not row["ran"]:
            print(f"{arm}: not run ({row['unavailable_reason']})")
            continue
        s = row["staleness"]["supersession"]
        print(
            f"{arm}: supersession stale_unsignaled@5 {s['stale_unsignaled@5']} current_served@5 {s['current_served@5']} top1 {s['top1_current']}"
        )
    return 0


def cmd_scorecard(summary_path: Path, out: Path, markdown: Path | None = None) -> int:
    import score

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = score.grade(summary)
    _write(
        out,
        {
            "summary": str(summary_path),
            "predictions": rows,
            "provenance": _provenance(),
        },
    )
    if markdown is not None:
        markdown.write_text(
            score.render_markdown(summary, rows) + "\n", encoding="utf-8"
        )
    for row in rows:
        print(
            f"{row['id']:>4} {row['grade']:<10} {row['claim']}  observed={json.dumps(row['observed'])}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    c = sub.add_parser("collect")
    c.add_argument("--arm", required=True)
    c.add_argument("--out", required=True, type=Path)
    c.add_argument("--scratch", type=Path, default=None)
    c.add_argument(
        "--limit-topics",
        type=int,
        default=None,
        help="smoke slice: first N topics of each kind",
    )
    s = sub.add_parser("score")
    s.add_argument("--raw", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    m = sub.add_parser("summary")
    m.add_argument("results", nargs="+", type=Path)
    m.add_argument("--out", required=True, type=Path)
    g = sub.add_parser("scorecard")
    g.add_argument("--markdown", type=Path, default=None, help="also render the tables")
    g.add_argument("summary", type=Path)
    g.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "collect":
        return collect(args.arm, args.out, args.scratch, args.limit_topics)
    if args.cmd == "score":
        return cmd_score(args.raw, args.out)
    if args.cmd == "summary":
        return cmd_summary(args.results, args.out)
    if args.cmd == "scorecard":
        return cmd_scorecard(args.summary, args.out, args.markdown)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
