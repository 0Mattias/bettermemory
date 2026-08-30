"""Register/df census: where is the vocabulary wall, per corpus?

The register-DF-census declaration fixes everything this script
computes — three corpora through their own committed loaders, the
matched / gap-elsewhere / gap-absent token classes against declared
gold, the register margins, the dev-only requery decomposition, and
what the artifact may contain — and was committed before this file
existed. This is the mechanical half. It runs NO retrieval: no
`search()` call, no ranking, no recall, no stratum. Token sets and
document counts only.

    .venv/bin/python bench/register_df_census.py \\
        --out retrieval/results/register-df-census-YYYY-MM-DD.json

Statistics only. The LongMemEval and MSC corpora are pinned,
uncommitted downloads; their records carry identities, counts and
band histograms — never corpus text — and the artifact reproduces
for them only for a holder of the same bytes. Token strings appear
in dev records alone, whose corpus is committed. Nothing under
`bench/heldout/` is opened.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory  # noqa: E402
import bettermemory.search as engine  # noqa: E402
from bettermemory.store import Store  # noqa: E402

from df_census import _BANDS  # noqa: E402
from embed_train import load_bench_module  # noqa: E402
from msc_scale_census import (  # noqa: E402
    E1_A40_EPISODES,
    PROBE_SESSIONS,
    _annotation_sha,
    _doc_tokens,
    _query_tokens,
    build_probes,
    msc,
    probe_set_sha256,
)

# Both bench runners are called `run`; the loader gives each an explicit
# module name so one process can hold both (the pattern store_census.py
# established and embed_train exports).
rr = load_bench_module("bench_retrieval_run", _HERE / "retrieval" / "run.py")
lr = load_bench_module("bench_longmemeval_run", _HERE / "longmemeval" / "run.py")

CLASSES = ("matched", "gap_elsewhere", "gap_absent")


# ---------------------------------------------------------------------------
# The declaration's §2, mechanically
# ---------------------------------------------------------------------------


def term_df(doc_sets: list[set[str]], terms: list[str]) -> dict[str, int]:
    """Document frequency over the ranked collection, one pass.

    The same quantity `df_census.term_df` counts, against the same
    per-document content sets; re-stated here over prebuilt sets so
    the collection is tokenised once per store, not once per probe.
    """
    df = dict.fromkeys(terms, 0)
    wanted = set(terms)
    for doc in doc_sets:
        for tok in wanted & doc:
            df[tok] += 1
    return df


def _class_of(token: str, gold_set: set[str], df: dict[str, int]) -> str:
    if token in gold_set:
        return "matched"
    return "gap_elsewhere" if df[token] > 0 else "gap_absent"


def _band_hist(ratios: list[float]) -> dict[str, int]:
    """`df_census._BANDS`, the shared ruler across the three corpora."""
    bands: dict[str, int] = {}
    for ratio in ratios:
        for lo, hi in zip(_BANDS, _BANDS[1:]):
            if lo <= ratio < hi:
                key = f"[{lo:g},{hi:g})"
                bands[key] = bands.get(key, 0) + 1
                break
    return bands


def _med_max(ratios: list[float]) -> dict[str, float] | None:
    if not ratios:
        return None
    return {
        "median": round(statistics.median(ratios), 6),
        "max": round(max(ratios), 6),
    }


def _register_counts(text: str) -> tuple[int, int]:
    """(raw token count, question-word count) — the probe-side margins.

    Raw tokens are the engine tokenizer's output with repeats kept;
    the question-word lexicon is the dev runner's committed
    `_QUESTION_WORDS`, imported so one fixed list rides every corpus
    (tokenize lowercases, so membership is direct).
    """
    raw = engine.tokenize(text)
    qwords = sum(1 for tok in raw if tok in rr._QUESTION_WORDS)
    return len(raw), qwords


def probe_record(
    text: str,
    gold_set: set[str],
    doc_sets: list[set[str]],
    *,
    include_tokens: bool,
) -> dict[str, Any]:
    """One probe's declared per-probe read. Identity is the caller's."""
    qtoks = _query_tokens(text)
    raw_n, qword_n = _register_counts(text)
    rec: dict[str, Any] = {
        "raw_tokens": raw_n,
        "question_word_tokens": qword_n,
        "content_tokens": len(qtoks),
    }
    if not qtoks:
        rec["excluded_empty"] = True
        return rec
    n = len(doc_sets)
    df = term_df(doc_sets, qtoks)
    by_class: dict[str, list[str]] = {c: [] for c in CLASSES}
    for tok in qtoks:
        by_class[_class_of(tok, gold_set, df)].append(tok)
    ratio = {t: (df[t] / n if n else 0.0) for t in qtoks}
    rec.update(
        {
            "overlap_share": round(len(by_class["matched"]) / len(qtoks), 6),
            "counts": {c: len(by_class[c]) for c in CLASSES},
            "bands": {c: _band_hist([ratio[t] for t in by_class[c]]) for c in CLASSES},
            "df_ratio": {
                "matched": _med_max([ratio[t] for t in by_class["matched"]]),
                "gap_elsewhere": _med_max(
                    [ratio[t] for t in by_class["gap_elsewhere"]]
                ),
            },
        }
    )
    if include_tokens:
        rec["tokens"] = {c: by_class[c] for c in CLASSES}
    return rec


def _share_distribution(shares: list[float]) -> dict[str, float] | None:
    if not shares:
        return None
    ordered = sorted(shares)
    pick = lambda q: ordered[int(q * (len(ordered) - 1))]  # noqa: E731
    return {
        "min": round(ordered[0], 6),
        "p25": round(pick(0.25), 6),
        "p50": round(statistics.median(ordered), 6),
        "p75": round(pick(0.75), 6),
        "p90": round(pick(0.90), 6),
        "max": round(ordered[-1], 6),
        "mean": round(sum(ordered) / len(ordered), 6),
    }


def _merge_bands(hists: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for hist in hists:
        for key, count in hist.items():
            merged[key] = merged.get(key, 0) + count
    return merged


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The declaration's per-(corpus, probe kind) rollup. No selection."""
    live = [r for r in records if not r.get("excluded_empty")]
    token_total = sum(sum(r["counts"].values()) for r in live)
    pooled = {c: sum(r["counts"][c] for r in live) for c in CLASSES}
    return {
        "probes": len(records),
        "excluded_empty": len(records) - len(live),
        "overlap_share": _share_distribution([r["overlap_share"] for r in live]),
        "class_shares_pooled": {
            "tokens": token_total,
            **{
                c: (round(pooled[c] / token_total, 6) if token_total else None)
                for c in CLASSES
            },
        },
        "bands_pooled": {
            c: _merge_bands([r["bands"][c] for r in live]) for c in CLASSES
        },
    }


def store_shape(doc_sets: list[set[str]]) -> dict[str, int]:
    vocab: set[str] = set()
    for doc in doc_sets:
        vocab |= doc
    return {
        "docs": len(doc_sets),
        "distinct_vocab": len(vocab),
        "content_token_mass": sum(len(s) for s in doc_sets),
    }


# ---------------------------------------------------------------------------
# dev — committed corpus, three probe kinds, the requery decomposition
# ---------------------------------------------------------------------------


def dev_census() -> dict[str, Any]:
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-regdf-dev-"))
    try:
        slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=None)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    id_to_set = {m.id: _doc_tokens(m) for m in memories}
    doc_sets = [id_to_set[m.id] for m in memories]

    kinds: dict[str, list[dict[str, Any]]] = {
        "asked": [],
        "requery": [],
        "control": [],
    }
    for q in questions:
        gold_set = id_to_set[slug_to_id[q["slug"]]]
        for kind in kinds:
            rec = probe_record(
                rr._query_for(q, kind), gold_set, doc_sets, include_tokens=True
            )
            rec["slug"] = q["slug"]
            rec["probe"] = kind
            kinds[kind].append(rec)

    decomposition = []
    pooled: dict[str, dict[str, int]] = {
        "introduced": dict.fromkeys(CLASSES, 0),
        "dropped": dict.fromkeys(CLASSES, 0),
    }
    kept_total = 0
    for q in questions:
        gold_set = id_to_set[slug_to_id[q["slug"]]]
        asked = set(_query_tokens(q["question"]))
        requery = set(_query_tokens(q["requery"]))
        union = sorted(asked | requery)
        df = term_df(doc_sets, union)
        n = len(doc_sets)
        kept = sorted(asked & requery)
        dropped = [
            {"token": t, "class": _class_of(t, gold_set, df)}
            for t in sorted(asked - requery)
        ]
        introduced = [
            {
                "token": t,
                "class": _class_of(t, gold_set, df),
                "df": df[t],
                "df_ratio": round(df[t] / n, 6) if n else 0.0,
            }
            for t in sorted(requery - asked)
        ]
        kept_total += len(kept)
        for row in dropped:
            pooled["dropped"][row["class"]] += 1
        for row in introduced:
            pooled["introduced"][row["class"]] += 1
        decomposition.append(
            {
                "slug": q["slug"],
                "kept": kept,
                "dropped": dropped,
                "introduced": introduced,
            }
        )

    return {
        "corpus": rr.CORPUS.name,
        "corpus_sha256": rr.corpus_fingerprint(rr.CORPUS),
        "store": store_shape(doc_sets),
        "collection_size": size,
        "probe_kinds": {k: aggregate(v) for k, v in kinds.items()},
        "requery_decomposition": {
            "pooled": {"kept": kept_total, **pooled},
            "per_question": decomposition,
        },
        "records": [rec for recs in kinds.values() for rec in recs],
    }


# ---------------------------------------------------------------------------
# LongMemEval — one store per instance, the question as asked
# ---------------------------------------------------------------------------


def lme_census(progress: bool) -> dict[str, Any]:
    path = lr.DEFAULT_CORPUS
    corpus = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    abstentions = 0
    docs_per_store: list[int] = []
    mass_total = 0
    vocab: set[str] = set()
    started = time.time()
    for i, inst in enumerate(corpus):
        if not inst["answer_session_ids"]:
            abstentions += 1
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-regdf-lme-"))
        try:
            id_to_session, n = lr.build_question_store(root, inst)
            memories = Store(root).load_all()
        finally:
            shutil.rmtree(root, ignore_errors=True)
        id_to_set = {m.id: _doc_tokens(m) for m in memories}
        doc_sets = [id_to_set[m.id] for m in memories]
        evidence = set(inst["answer_session_ids"])
        gold_set: set[str] = set()
        for mid, sid in id_to_session.items():
            if sid in evidence:
                gold_set |= id_to_set[mid]
        rec = probe_record(inst["question"], gold_set, doc_sets, include_tokens=False)
        rec["question_id"] = inst.get("question_id", "")
        records.append(rec)
        docs_per_store.append(n)
        mass_total += sum(len(s) for s in doc_sets)
        vocab |= set().union(*doc_sets) if doc_sets else set()
        if progress and (i + 1) % 100 == 0:
            print(
                f"  longmemeval {i + 1}/{len(corpus)} ({time.time() - started:.0f}s)",
                file=sys.stderr,
            )
    return {
        "corpus": path.name,
        "corpus_sha256": lr.corpus_fingerprint(path),
        "instances": len(corpus),
        "abstentions_excluded": abstentions,
        "store": {
            "stores": len(docs_per_store),
            "docs_total": sum(docs_per_store),
            "docs_per_store": _share_distribution([float(n) for n in docs_per_store]),
            "distinct_vocab": len(vocab),
            "content_token_mass": mass_total,
        },
        "probe_kinds": {"asked": aggregate(records)},
        "records": records,
    }


# ---------------------------------------------------------------------------
# MSC — the A40 aggregate, the scale census's probe rules verbatim
# ---------------------------------------------------------------------------


def msc_census(progress: bool) -> dict[str, Any]:
    episode_list = msc.episodes("test")
    probes_all, counters = build_probes(episode_list)
    first40 = episode_list[:E1_A40_EPISODES]
    ids40 = {e["episode_id"] for e in first40}
    probes = [p for p in probes_all if p["episode_id"] in ids40]

    root = Path(tempfile.mkdtemp(prefix="bm-regdf-msc-"))
    try:
        id_to_session, n = msc.build_aggregate_store(root, first40)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    id_to_set = {m.id: _doc_tokens(m) for m in memories}
    doc_sets = [id_to_set[m.id] for m in memories]
    session_sets: dict[str, set[str]] = {}
    for mid, key in id_to_session.items():
        session_sets.setdefault(key, set()).update(id_to_set[mid])

    records: list[dict[str, Any]] = []
    started = time.time()
    for i, probe in enumerate(probes):
        gold_set = session_sets[f"{probe['episode_id']}/s{probe['session']}"]
        rec = probe_record(probe["line"], gold_set, doc_sets, include_tokens=False)
        rec["episode_id"] = probe["episode_id"]
        rec["session"] = probe["session"]
        rec["sha16"] = probe["sha16"]
        records.append(rec)
        if progress and (i + 1) % 500 == 0:
            print(
                f"  msc {i + 1}/{len(probes)} ({time.time() - started:.0f}s)",
                file=sys.stderr,
            )
    return {
        "tarball_sha256": msc.TARBALL_SHA256,
        "split": "test",
        "split_sha256": msc.corpus_fingerprint("test"),
        "annotation_sha256": {
            f"session_{k}": _annotation_sha(k) for k in PROBE_SESSIONS
        },
        "probe_rules_full_split": counters,
        "episodes_in_store": E1_A40_EPISODES,
        "probes_in_store": len(probes),
        "probe_set_sha256_restricted": probe_set_sha256(probes),
        "store": store_shape(doc_sets),
        "collection_size": n,
        "probe_kinds": {"persona-line": aggregate(records)},
        "records": records,
    }


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description="Register/df census. Statistics only; no retrieval."
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Write the census here. A RELATIVE path resolves against this "
            "script's own directory (bench/), matching the other runners."
        ),
    )
    p.add_argument("--progress", action="store_true")
    args = p.parse_args()

    if args.progress:
        print("dev…", file=sys.stderr)
    dev = dev_census()
    if args.progress:
        print("longmemeval…", file=sys.stderr)
    lme = lme_census(args.progress)
    if args.progress:
        print("msc…", file=sys.stderr)
    msc_part = msc_census(args.progress)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_HERE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    payload = {
        "provenance": {
            "generated": date.today().isoformat(),
            "git_commit": commit,
            "bettermemory_version": bettermemory.__version__,
            "declaration": "bench/REGISTER_DF_CENSUS_DECLARATION.md",
            "reproducibility": (
                "dev is committed text; the LongMemEval and MSC halves "
                "reproduce only for a holder of the same pinned bytes"
            ),
        },
        "bands": list(_BANDS),
        "note": (
            "STATISTICS ONLY — token classes and document frequencies "
            "against declared gold. No retrieval was run: no ranking, no "
            "recall, no stratum, no label beyond the gold identity each "
            "instrument already declares."
        ),
        "dev": dev,
        "longmemeval": lme,
        "msc": msc_part,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
