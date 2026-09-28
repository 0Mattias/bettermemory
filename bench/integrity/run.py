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

THE CORPUS. Every command takes --corpus PATH, the v0 corpus.json by
default, so a v0 command line reads as it always did. The v1 corpus is
two files, bench/integrity/v1/dev.json and test.json, each in v0's shape
plus organisations, labels and a `declared` block that the checks read
their counts from (score.corpus_checks). v1 adds split payloads, one
planted item written as two parts (score.planted_items): `collect` writes
the poison in corpus order into the same store, runs a false fact's
search after its value-carrying part, and injects only value-carrying
false-fact parts in P-B. The raw file records the corpus's path, sha256
and benchmark name under `corpus`; a scored result records them under
`scored_with.corpus`, a summary and a scorecard under
`provenance.corpus`, all score-time stamps, so a v0 re-score still equals
its committed file field for field. `check` with two --corpus runs each
corpus's checks and then the value checks over their union, so no value
collides across the splits.

THE TEST SPLIT is guarded (guard.py): `collect` refuses the v1-test
corpus unless its sha is the sealed one and --operating-points names the
sealed operating-points file, and refuses a second test result for an
arm on the same sha unless --rerun-reason says why.

LLM USAGE. The mem0-infer and graphiti arms' chat completions are
counted (adapters.LLMUsage) into the raw file's `llm_usage`: totals, per
phase and per add, and, on OpenRouter, the key's usage before and after
the run.

Usage:

    .venv/bin/python bench/integrity/run.py check
    .venv/bin/python bench/integrity/run.py collect --arm bettermemory \
        --out bench/integrity/results/raw/bettermemory-YYYY-MM-DD.json
    .eval-venv/bin/python bench/integrity/run.py collect --arm mem0-raw --out ...
    .venv/bin/python bench/integrity/run.py score --raw <raw.json> --out <result.json>
    .venv/bin/python bench/integrity/run.py summary <result.json>... --out <summary.json>
    .venv/bin/python bench/integrity/run.py scorecard <summary.json> --out <scorecard.json>

and on v1 (dev shown; the test split adds the two guard flags to collect):

    .venv/bin/python bench/integrity/run.py check \
        --corpus bench/integrity/v1/dev.json --corpus bench/integrity/v1/test.json
    .venv/bin/python bench/integrity/run.py collect --corpus bench/integrity/v1/dev.json \
        --arm bettermemory --out <raw.json>
    .venv/bin/python bench/integrity/run.py collect --corpus bench/integrity/v1/test.json \
        --arm bettermemory --out <raw.json> --operating-points <sealed file> \
        [--rerun-reason "<why>"]
    .venv/bin/python bench/integrity/run.py score --corpus <split.json> --raw <raw.json> --out <result.json>
    .venv/bin/python bench/integrity/run.py summary --corpus <split.json> <result.json>... --out <summary.json>
    .venv/bin/python bench/integrity/run.py scorecard --corpus <split.json> <summary.json> --out <scorecard.json>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import guard  # noqa: E402 - the path above makes the sibling importable

CORPUS = _HERE / "corpus.json"
ROT_ARTIFACT = (
    _ROOT / "bench" / "rot" / "results" / "multirepo-anchored-2026-07-30.json"
)


def _load_corpus(path: Path = CORPUS) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rel(path: Path) -> str:
    """How an artifact names a path: relative to the repo when inside it."""
    return str(path.relative_to(_ROOT)) if path.is_relative_to(_ROOT) else str(path)


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


def collect(
    arm: str,
    out: Path,
    scratch: Path | None,
    limit_topics: int | None,
    corpus_path: Path = CORPUS,
    operating_points: Path | None = None,
    rerun_reason: str | None = None,
    seals: Path = guard.SEALS,
) -> int:
    import adapters as arms
    import score
    from adapters import InjectionUnsupported, SystemUnavailable, make_adapter

    corpus = _load_corpus(corpus_path)
    problems = score.corpus_checks(corpus)
    if problems:
        print("corpus checks failed:", *problems, sep="\n  ")
        return 2
    sha = score.corpus_sha256(corpus_path)
    try:
        test_guard = guard.guard_run(
            corpus,
            sha,
            arm=arm,
            arm_key="arm",
            out=out,
            operating_points=operating_points,
            rerun_reason=rerun_reason,
            seals=seals,
        )
    except guard.Refused as exc:
        print(f"{arm}: refused: {exc}")
        return 2
    if test_guard is None and (operating_points or rerun_reason):
        print("--operating-points and --rerun-reason apply to the v1 test split only")
    identity = score.corpus_identity(corpus_path, corpus)
    if limit_topics is not None:
        corpus = _slice(corpus, limit_topics)
    scratch = scratch or Path(tempfile.mkdtemp(prefix=f"bm-integrity-{arm}-"))
    scratch.mkdir(parents=True, exist_ok=True)
    adapter = make_adapter(arm, scratch)
    raw: dict[str, Any] = {
        "arm": arm,
        "ran": True,
        "corpus_sha256": sha,
        "corpus": identity,
        "k": score.K,
        "k_inject": score.K_INJECT,
        "limited_to_topics": limit_topics,
        "provenance": _provenance(),
        "scratch": str(scratch),
    }
    if test_guard is not None:
        raw["test_guard"] = test_guard
    usage = getattr(adapter, "llm_usage", None)
    key_before = (
        arms.openrouter_key_usage()
        if usage is not None and arms.is_openrouter(usage.endpoint)
        else None
    )
    attribute = _attributor(usage)
    started = time.time()
    try:
        with attribute("reset", None):
            adapter.reset()
    except SystemUnavailable as exc:
        raw.update({"ran": False, "unavailable_reason": exc.reason})
        _record_usage(raw, usage, key_before)
        _write(out, raw)
        print(f"{arm}: unavailable: {exc.reason}")
        return 0
    raw["capabilities"] = adapter.capabilities()
    raw["version"] = adapter.version()
    try:
        _run_phases(adapter, corpus, raw, InjectionUnsupported, attribute)
    finally:
        adapter.close()
    raw["timing"] = {"seconds": round(time.time() - started, 1)}
    _record_usage(raw, usage, key_before)
    _write(out, raw)
    print(
        f"{arm}: collected {len(raw['adds'])} adds, {len(raw['topic_searches'])} topic searches, "
        f"{len(raw['poison_searches'])} poison searches, {len(raw['injections'])} injections "
        f"in {raw['timing']['seconds']}s -> {out}"
    )
    if "llm_usage" in raw:
        totals = raw["llm_usage"]["totals"]
        print(
            f"{arm}: LLM calls {totals['calls']} ({totals['failed']} failed), "
            f"prompt tokens {totals['prompt_tokens']}, completion tokens "
            f"{totals['completion_tokens']}, reported cost {totals['cost']}"
        )
    return 0


Attributor = Callable[[str, str | None], contextlib.AbstractContextManager[None]]


def _attributor(usage: Any) -> Attributor:
    """A context manager factory naming the phase the arm's LLM calls
    belong to; a no-op for an arm that calls no LLM."""

    def attribute(
        phase: str, key: str | None
    ) -> contextlib.AbstractContextManager[None]:
        if usage is None:
            return contextlib.nullcontext()
        context: contextlib.AbstractContextManager[None] = usage.attribute(phase, key)
        return context

    return attribute


def _record_usage(
    raw: dict[str, Any], usage: Any, key_before: dict[str, Any] | None
) -> None:
    """The arm's LLM usage into the raw file, with the OpenRouter key's
    usage delta when the endpoint is OpenRouter's."""
    if usage is None:
        return
    import adapters as arms

    report = usage.report()
    if key_before is not None:
        report["openrouter_key"] = arms.key_usage_delta(
            key_before, arms.openrouter_key_usage()
        )
    raw["llm_usage"] = report


def _run_phases(
    adapter: Any,
    corpus: dict[str, Any],
    raw: dict[str, Any],
    unsupported: type[Exception],
    attribute: Attributor | None = None,
) -> None:
    import score

    attribute = attribute or _attributor(None)
    topics = {t["id"]: t for t in corpus["topics"]}
    adds: list[dict[str, Any]] = []
    raw["adds"] = adds

    def do_add(row: dict[str, Any], kind: str) -> Any:
        t0 = time.time()
        with attribute("add", row["stmt_id"]):
            outcome = adapter.add(
                row["stmt_id"],
                row["text"],
                {k: v for k, v in row.items() if k != "text"},
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
        with attribute("search", topic["id"]):
            hits = adapter.search(topic["query"], score.K)
        raw["topic_searches"].append(
            {
                "topic_id": topic["id"],
                "query": topic["query"],
                "hits": [h.to_dict() for h in hits],
            }
        )
        _progress(f"search {topic['id']} -> {len(hits)} hits")

    # Phase C: poisoning through the write path, in corpus order. A false
    # fact's search runs once its value is in the store: after the payload
    # itself, or after a split's value-carrying part (part b), never after
    # part a, which only introduces the alias.
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
        if payload["class"] == "false_fact" and "value" in payload:
            topic = topics[payload["target_topic"]]
            with attribute("search", topic["id"]):
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
        with attribute("search", gq["id"]):
            hits = adapter.search(gq["text"], score.K)
        raw["poison_searches"].append(
            {
                "payload_id": None,
                "query_id": gq["id"],
                "query": gq["text"],
                "hits": [h.to_dict() for h in hits],
            }
        )

    # Phase D: store injection, of the value-carrying false-fact parts only
    # (a split's part b; every false fact outside a split)
    raw["injections"] = []
    false_facts = [
        p for p in corpus["poison"] if p["class"] == "false_fact" and "value" in p
    ]
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
                with attribute("inject", payload["id"]):
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
            with attribute("search", topic["id"]):
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
    target a kept topic, everything else unchanged. A split whose target
    is dropped is dropped whole, so no half of a planted item survives."""
    kept: list[dict[str, Any]] = []
    for kind in ("supersession", "distractor", "reversion"):
        kept.extend([t for t in corpus["topics"] if t["kind"] == kind][:n])
    ids = {t["id"] for t in kept}
    dropped_groups = {
        p["group"]
        for p in corpus["poison"]
        if p.get("group")
        and p["class"] == "false_fact"
        and p.get("target_topic") not in ids
    }
    poison = [
        p
        for p in corpus["poison"]
        if (p["class"] != "false_fact" or p.get("target_topic") in ids)
        and p.get("group") not in dropped_groups
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


REFERENCE_EXPECTATIONS = {
    ("serve_all_unsignaled", "supersession", "stale_served@5"): 1.0,
    ("serve_all_unsignaled", "supersession", "current_served@5"): 1.0,
    ("serve_all_unsignaled", "distractor", "stale_served@5"): 0.0,
    ("recency_top1", "supersession", "top1_current"): 1.0,
    ("recency_top1", "distractor", "top1_current"): 0.0,
    ("recency_top1", "reversion", "top1_current"): 1.0,
    ("oracle_replica", "all", "stale_served@5"): 0.0,
    ("oracle_replica", "all", "current_served@5"): 1.0,
}


def _check_one(path: Path, seals: Path) -> tuple[dict[str, Any], list[str]]:
    """One corpus's gates, the reference arithmetic and, for a v1 split,
    its seal; prints what it read."""
    import score

    corpus = _load_corpus(path)
    problems = score.corpus_checks(corpus)
    refs = score.reference_tables(corpus)
    for (ref, kind, metric), want in REFERENCE_EXPECTATIONS.items():
        got = refs[ref][kind][metric]
        if got != want:
            problems.append(f"reference {ref} {kind} {metric} = {got}, expected {want}")
    sha = score.corpus_sha256(path)
    problems.extend(guard.seal_status(corpus, sha, seals))
    print(f"corpus sha256 {sha}")
    print(
        f"topics {len(corpus['topics'])}, statements {sum(len(t['statements']) for t in corpus['topics'])}, "
        f"hard negatives {len(corpus['hard_negatives'])}, poison {len(corpus['poison'])}"
    )
    if "declared" in corpus:
        payloads = {p["id"]: p for p in corpus["poison"]}
        items = score.planted_items(list(payloads), payloads)
        print(
            f"benchmark {score.benchmark_name(corpus)}, organisations "
            f"{len(corpus.get('organisations') or [])}, planted poison items {len(items)}, "
            f"split groups {sum(1 for _, parts in items if len(parts) > 1)}, "
            f"generic queries {len(corpus.get('generic_queries', []))}"
        )
    return corpus, problems


def cmd_check(corpus_paths: list[Path] | None = None, seals: Path = guard.SEALS) -> int:
    """Check each corpus; with more than one, also the value checks over
    their union (score.union_value_checks), so no value collides across
    the splits."""
    import score

    paths = list(corpus_paths or [CORPUS])
    failed = False
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if len(paths) > 1:
            print(f"== {_rel(Path(path).resolve())}")
        corpus, problems = _check_one(path, seals)
        loaded.append((path, corpus))
        if problems:
            print("FAILED", *problems, sep="\n  ")
            failed = True
        else:
            print("corpus checks and reference arithmetic hold")
    if len(paths) > 1:
        labels = _labels(loaded)
        cross = score.union_value_checks(
            [(label, corpus) for label, (_, corpus) in zip(labels, loaded)]
        )
        print(f"== the union of {', '.join(labels)}")
        if cross:
            print("FAILED", *cross, sep="\n  ")
            failed = True
        else:
            print("no value collides across the corpora")
    return 1 if failed else 0


def _labels(loaded: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """A short distinct label per corpus: its version (v1-dev, v1-test)
    when those are distinct, the file's stem otherwise, numbered on a
    tie."""
    labels = [str(corpus.get("version") or Path(path).stem) for path, corpus in loaded]
    if len(set(labels)) != len(labels):
        labels = [f"{Path(path).stem}#{i + 1}" for i, (path, _) in enumerate(loaded)]
    return labels


def cmd_score(raw_path: Path, out: Path, corpus_path: Path = CORPUS) -> int:
    import score

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    corpus = _load_corpus(corpus_path)
    if raw["corpus_sha256"] != score.corpus_sha256(corpus_path):
        print("raw observations were collected on a different corpus sha; refusing")
        return 2
    identity = score.corpus_identity(corpus_path, corpus)
    if raw.get("limited_to_topics"):
        corpus = _slice(corpus, raw["limited_to_topics"])
    result = score.score_arm(raw, corpus)
    result["raw_observations"] = _rel(raw_path)
    result["scored_with"] = {**_provenance(), "corpus": identity}
    _write(out, result)
    if result.get("ran", True):
        st = result["staleness"]["arm"]
        print(
            f"{result['arm']}: supersession stale_served@5 {st['supersession']['stale_served@5']} "
            f"unsignaled {st['supersession']['stale_unsignaled@5']} current {st['supersession']['current_served@5']} "
            f"top1 {st['supersession']['top1_current']}; distractor current {st['distractor']['current_served@5']}; "
            f"reversion current {st['reversion']['current_served@5']}"
        )
        writes = result.get("supersession_writes")
        if writes:
            print(
                f"{result['arm']}: write-time supersession: updates linked "
                f"{writes['updates']['linked']}/{writes['updates']['n']}, "
                f"false facts linked over the true fact "
                f"{writes['false_fact']['linked_over_true']}/{writes['false_fact']['admitted']}"
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


def cmd_summary(paths: list[Path], out: Path, corpus_path: Path = CORPUS) -> int:
    import score

    results = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    corpus = _load_corpus(corpus_path)
    shas = {r["corpus_sha256"] for r in results}
    if len(shas) == 1 and shas != {score.corpus_sha256(corpus_path)}:
        # the references are computed from --corpus, so it must be the
        # corpus the results were scored on (several shas are refused by
        # score.summarize itself)
        print(
            "the results were scored on a different corpus sha than --corpus; refusing"
        )
        return 2
    summary = score.summarize(results, corpus, ROT_ARTIFACT)
    summary["results"] = [_rel(p) for p in paths]
    summary["provenance"] = {
        **_provenance(),
        "corpus": score.corpus_identity(corpus_path, corpus),
    }
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


def cmd_scorecard(
    summary_path: Path,
    out: Path,
    markdown: Path | None = None,
    corpus_path: Path = CORPUS,
) -> int:
    import score

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    corpus = _load_corpus(corpus_path)
    if summary.get("corpus_sha256") != score.corpus_sha256(corpus_path):
        print("the summary pools a different corpus sha than --corpus; refusing")
        return 2
    rows = score.grade(summary)
    _write(
        out,
        {
            "summary": str(summary_path),
            "predictions": rows,
            "provenance": {
                **_provenance(),
                "corpus": score.corpus_identity(corpus_path, corpus),
            },
        },
    )
    if markdown is not None:
        markdown.write_text(
            score.render_markdown(summary, rows, corpus) + "\n", encoding="utf-8"
        )
    for row in rows:
        print(
            f"{row['id']:>4} {row['grade']:<10} {row['claim']}  observed={json.dumps(row['observed'])}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    corpus_help = "the corpus file (default: the v0 corpus.json)"
    k = sub.add_parser("check")
    k.add_argument(
        "--corpus",
        type=Path,
        action="append",
        default=None,
        help="a corpus file; give two (dev and test) to check their union as well "
        "(default: the v0 corpus.json)",
    )
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
    c.add_argument("--corpus", type=Path, default=CORPUS, help=corpus_help)
    c.add_argument(
        "--operating-points",
        type=Path,
        default=None,
        help="v1 test split: the operating-points file whose sha256 SEALS.json seals",
    )
    c.add_argument(
        "--rerun-reason",
        default=None,
        help="v1 test split: why this arm runs again on the same sha (recorded)",
    )
    s = sub.add_parser("score")
    s.add_argument("--raw", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--corpus", type=Path, default=CORPUS, help=corpus_help)
    m = sub.add_parser("summary")
    m.add_argument("results", nargs="+", type=Path)
    m.add_argument("--out", required=True, type=Path)
    m.add_argument("--corpus", type=Path, default=CORPUS, help=corpus_help)
    g = sub.add_parser("scorecard")
    g.add_argument("--markdown", type=Path, default=None, help="also render the tables")
    g.add_argument("summary", type=Path)
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--corpus", type=Path, default=CORPUS, help=corpus_help)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args.corpus)
    if args.cmd == "collect":
        return collect(
            args.arm,
            args.out,
            args.scratch,
            args.limit_topics,
            corpus_path=args.corpus,
            operating_points=args.operating_points,
            rerun_reason=args.rerun_reason,
        )
    if args.cmd == "score":
        return cmd_score(args.raw, args.out, corpus_path=args.corpus)
    if args.cmd == "summary":
        return cmd_summary(args.results, args.out, corpus_path=args.corpus)
    if args.cmd == "scorecard":
        return cmd_scorecard(
            args.summary, args.out, args.markdown, corpus_path=args.corpus
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
