"""Rank parity, the public half: the fixed query set the bettermemory 9 gate
compares across engines.

WHY THIS EXISTS. bettermemory 9 moves the store from markdown files plus a
derived SQLite index to one SQLite file, and ports the retrieval engine on
top of it. The claim the port makes is exact: the same store and the same
query return the same ranked ids and the same relevance labels before and
after. A recall number cannot carry that claim, because two different
rankings can score the same recall. This harness records the rankings
themselves, so the comparison is a list of differing queries, and the
number the gate reads is zero.

WHAT IS FIXED. Everything a ranking depends on:

  the corpus     bench/retrieval/corpus.jsonl, 1,080 documents, blind-authored
  the questions  bench/retrieval/questions.jsonl, 120, each probed three ways
                 (asked, requery, control) exactly as bench/retrieval does
  the ids        deterministic ULIDs derived from the slug and the corpus
                 ordinal, so a rebuilt store carries the same ids
  the clock      created stamps start at BASE_CREATED one second apart in
                 corpus order; the engine clock is PINNED_NOW
  the width      MAX_RESULTS, deeper than the bench's recall depth so a
                 swap below rank five is still a difference

TWO ARMS PER QUERY. `full` ranks the whole store in-process, the engine
alone. `prefilter` drives `handlers.search.resolve_search_pool`, the
production path: above the index threshold the FTS5 index nominates a
capped slice by bm25 and hands back the corpus-statistics provider. The
corpus is above the threshold, so every prefilter row must report
`engaged`; a row that does not is a harness fault, not a result. The
prefilter arm is the one that exercises the store: its SQL, its tie order
at the cap, its document frequencies.

THE ARTIFACT. Per query: the query text, both arms' ranked ids, scores
(diagnostic, outside the digest) and relevance labels. `digest` is a
sha256 over the ids and labels, so two artifacts agree or differ in one
line. `--compare A B` prints every differing query.

Usage:

    .venv/bin/python bench/parity/run.py --out bench/parity/results/public-8.0.0-2026-09-25.json
    .venv/bin/python bench/parity/run.py --compare A.json B.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory import index as _index  # noqa: E402
from bettermemory._handlers import (  # noqa: E402
    _INDEX_THRESHOLD_DEFAULT,
    _PREFILTER_CAP,
)
from bettermemory.handlers.search import resolve_search_pool  # noqa: E402
from bettermemory.models import (  # noqa: E402
    Confidence,
    Memory,
    Source,
    _encode_crockford,
)
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.search import tokenizer_fingerprint  # noqa: E402
from bettermemory.store import Store  # noqa: E402

RETRIEVAL = _ROOT / "bench" / "retrieval"
CORPUS = RETRIEVAL / "corpus.jsonl"
QUESTIONS = RETRIEVAL / "questions.jsonl"

PROBES = ("asked", "requery", "control")
MAX_RESULTS = 10
PINNED_NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)
BASE_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
PREFILTER_CAP = _PREFILTER_CAP
INDEX_THRESHOLD = _INDEX_THRESHOLD_DEFAULT

# The product defaults. Fixed here rather than read from any config file,
# so the artifact never measures the operator's own settings.
MODE = "hybrid"
RESCUE_EXPANSION = False
CONVERSATIONAL = True
HALF_LIFE_DAYS = 30.0


# ---------------------------------------------------------------------------
# Fixture inputs
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_retrieval_runner: ModuleType | None = None


def _retrieval() -> ModuleType:
    """The retrieval bench's own module, so the three probes are ITS
    definitions (one implementation of the control's word stripping, not
    a copy that could drift)."""
    global _retrieval_runner
    if _retrieval_runner is None:
        if str(RETRIEVAL) not in sys.path:
            sys.path.insert(0, str(RETRIEVAL))
        spec = importlib.util.spec_from_file_location(
            "bench_retrieval_run_for_parity", RETRIEVAL / "run.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Registered before exec: the runner defines dataclasses, and the
        # decorator resolves string annotations through sys.modules.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _retrieval_runner = module
    return _retrieval_runner


def query_for(question: dict[str, Any], probe: str) -> str:
    if probe not in PROBES:
        raise ValueError(f"unknown probe {probe!r}")
    return str(_retrieval()._query_for(question, probe))


# ---------------------------------------------------------------------------
# Deterministic store
# ---------------------------------------------------------------------------


def deterministic_id(slug: str, ordinal: int) -> str:
    """A ULID whose time part is BASE_CREATED plus `ordinal` seconds and
    whose random part is the slug's digest. Valid, sortable by ordinal,
    identical on every rebuild."""
    ts_ms = (int(BASE_CREATED.timestamp()) + ordinal) * 1000
    rand = int.from_bytes(hashlib.sha256(slug.encode("utf-8")).digest()[:10], "big")
    return _encode_crockford(ts_ms & ((1 << 48) - 1), 10) + _encode_crockford(rand, 16)


def build_store(root: Path, corpus_path: Path) -> dict[str, str]:
    """Write the corpus into a fresh v8 store at `root` with deterministic
    ids and stamps, then rebuild the index the way a fresh reindex would.
    Returns slug -> id."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"refusing to build into a non-empty directory: {root}")
    store = Store(root)
    store.ensure()
    ids: dict[str, str] = {}
    for ordinal, row in enumerate(_read_jsonl(corpus_path)):
        created = BASE_CREATED + timedelta(seconds=ordinal)
        memory = Memory(
            id=deterministic_id(row["slug"], ordinal),
            created=created,
            updated=created,
            scopes=list(row["scopes"]),
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=str(row["body"]).strip() + "\n",
        )
        store._write_path(store._path_for(memory), memory)
        ids[row["slug"]] = memory.id
    _index.rebuild(root, store.iter_active())
    return ids


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _hits_to_dict(hits: list[Any], *, engaged: bool, pool_size: int) -> dict[str, Any]:
    return {
        "ids": [h.id for h in hits],
        "scores": [round(float(h.score), 6) for h in hits],
        "relevance": [str(h.relevance) for h in hits],
        "engaged": engaged,
        "pool_size": pool_size,
    }


def rank_full(memories: list[Any], query: str) -> dict[str, Any]:
    hits = run_search(
        memories,
        query,
        max_results=MAX_RESULTS,
        mode=MODE,
        now=PINNED_NOW,
        half_life_days=HALF_LIFE_DAYS,
        rescue_expansion=RESCUE_EXPANSION,
        conversational=CONVERSATIONAL,
    )
    return _hits_to_dict(hits, engaged=False, pool_size=len(memories))


def rank_prefilter(store: Store, query: str) -> dict[str, Any]:
    pool = resolve_search_pool(
        store,
        query,
        scopes=None,
        excluded_scopes=None,
        repo_filter=None,
        worktree_filter=None,
        min_survivors=MAX_RESULTS,
    )
    hits = run_search(
        pool.memories,
        query,
        max_results=MAX_RESULTS,
        mode=MODE,
        now=PINNED_NOW,
        half_life_days=HALF_LIFE_DAYS,
        rescue_expansion=RESCUE_EXPANSION,
        conversational=CONVERSATIONAL,
        corpus_stats_provider=pool.corpus_stats_provider,
    )
    return _hits_to_dict(
        hits,
        engaged=pool.corpus_stats_provider is not None,
        pool_size=len(pool.memories),
    )


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def digest(rows: list[dict[str, Any]]) -> str:
    material = [
        [
            r["slug"],
            r["probe"],
            r["full"]["ids"],
            r["full"]["relevance"],
            r["prefilter"]["ids"],
            r["prefilter"]["relevance"],
        ]
        for r in rows
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _provenance() -> dict[str, Any]:
    def git(*argv: str) -> str:
        try:
            return subprocess.run(
                ["git", *argv],
                capture_output=True,
                text=True,
                cwd=str(_ROOT),
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    import bettermemory

    return {
        "bettermemory_version": bettermemory.__version__,
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "tree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def run_public(
    root: Path,
    corpus_path: Path = CORPUS,
    questions_path: Path = QUESTIONS,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    slug_to_id = build_store(root, corpus_path)
    store = Store(root)
    full = store.load_all()
    questions = _read_jsonl(questions_path)
    if limit is not None:
        questions = questions[:limit]
    rows: list[dict[str, Any]] = []
    for q in questions:
        gold = slug_to_id.get(q["slug"])
        if gold is None:
            raise SystemExit(
                f"question {q['slug']!r} has no gold document in the corpus"
            )
        for probe in PROBES:
            query = query_for(q, probe)
            rows.append(
                {
                    "slug": q["slug"],
                    "probe": probe,
                    "query": query,
                    "gold_id": gold,
                    "full": rank_full(full, query),
                    "prefilter": rank_prefilter(store, query),
                }
            )
    status = _index.status(root)
    return {
        "kind": "rank-parity/public",
        "provenance": _provenance(),
        "engine": {
            "tokenizer_fingerprint": tokenizer_fingerprint(),
            "index_schema_version": status.get("schema_version"),
            "prefilter_cap": PREFILTER_CAP,
            "index_threshold": INDEX_THRESHOLD,
        },
        "params": {
            "pinned_now": PINNED_NOW.isoformat(),
            "base_created": BASE_CREATED.isoformat(),
            "max_results": MAX_RESULTS,
            "mode": MODE,
            "rescue_expansion": RESCUE_EXPANSION,
            "conversational": CONVERSATIONAL,
            "half_life_days": HALF_LIFE_DAYS,
            "probes": list(PROBES),
        },
        "corpus": corpus_path.name,
        "corpus_sha256": _sha256(corpus_path),
        "corpus_size": len(slug_to_id),
        "questions": questions_path.name,
        "questions_sha256": _sha256(questions_path),
        "questions_n": len(questions),
        "id_to_slug": {v: k for k, v in slug_to_id.items()},
        "digest": digest(rows),
        "rows": rows,
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Every (query, arm, field) where the two artifacts disagree."""
    index_b = {(r["slug"], r["probe"]): r for r in b["rows"]}
    diffs: list[dict[str, Any]] = []
    for ra in a["rows"]:
        key = (ra["slug"], ra["probe"])
        rb = index_b.pop(key, None)
        if rb is None:
            diffs.append(
                {"slug": key[0], "probe": key[1], "arm": None, "field": "missing_in_b"}
            )
            continue
        if ra["query"] != rb["query"]:
            diffs.append(
                {
                    "slug": key[0],
                    "probe": key[1],
                    "arm": None,
                    "field": "query",
                    "a": ra["query"],
                    "b": rb["query"],
                }
            )
        for arm in ("full", "prefilter"):
            for field in ("ids", "relevance", "engaged"):
                if ra[arm][field] != rb[arm][field]:
                    diffs.append(
                        {
                            "slug": key[0],
                            "probe": key[1],
                            "arm": arm,
                            "field": field,
                            "a": ra[arm][field],
                            "b": rb[arm][field],
                        }
                    )
    for key in index_b:
        diffs.append(
            {"slug": key[0], "probe": key[1], "arm": None, "field": "missing_in_a"}
        )
    return diffs


def _summary(artifact: dict[str, Any]) -> str:
    rows = artifact["rows"]
    engaged = sum(1 for r in rows if r["prefilter"]["engaged"])
    same = sum(1 for r in rows if r["full"]["ids"] == r["prefilter"]["ids"])
    return (
        f"{len(rows)} rows over {artifact['questions_n']} questions x {len(PROBES)} probes; "
        f"corpus {artifact['corpus_size']}; prefilter engaged {engaged}/{len(rows)}; "
        f"full == prefilter on {same}/{len(rows)}; digest {artifact['digest'][:16]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, default=None, help="Write the artifact here."
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        default=None,
        help="Compare two artifacts and print every differing query.",
    )
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--limit", type=int, default=None, help="First N questions.")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Scratch directory for the built store (default: a fresh temp dir).",
    )
    args = parser.parse_args()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        diffs = compare(a, b)
        print(f"A: {_summary(a)}")
        print(f"B: {_summary(b)}")
        if a.get("digest") == b.get("digest"):
            print("digests agree")
        for d in diffs:
            print(json.dumps(d, ensure_ascii=False))
        print(f"{len(diffs)} differing (query, arm, field) entries")
        return 0 if not diffs else 1

    root = args.root or Path(tempfile.mkdtemp(prefix="bm-parity-"))
    artifact = run_public(root, args.corpus, args.questions, limit=args.limit)
    unengaged = [r for r in artifact["rows"] if not r["prefilter"]["engaged"]]
    if unengaged:
        print(
            f"{len(unengaged)} prefilter rows came back un-prefiltered; refusing to emit",
            file=sys.stderr,
        )
        return 1
    text = json.dumps(artifact, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    print(_summary(artifact), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
