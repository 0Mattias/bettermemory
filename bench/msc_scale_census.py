"""MSC scale census: does store-trained dense retrieval work at
conversational store scale, or does the technical-corpus wall hold?

`bench/MSC_SCALE_CENSUS_DECLARATION.md` fixes everything this script
computes — the three store scales, the probe rules over MSC's own
per-turn persona annotations, both arms' definitions, every read, and
the four-rung outcome ladder — and was committed before this file
existed. This is the mechanical half: it composes the MSC loader, the
trainer pipeline, and the shipped engine's search path, tabulates
gold-session ranks, and applies the declared ladder. No fusion, no
weights, no engine path changes.

    .venv/bin/python bench/msc_scale_census.py \\
        --out retrieval/results/msc-scale-census-YYYY-MM-DD.json

Statistics only. The corpus is a pinned, uncommitted download; the
artifact carries ids, hashes, counts, ranks, and shares — never corpus
text — and reproduces only for a holder of the same bytes. Nothing
under `bench/heldout/` or `bench/retrieval/` is read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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

import bettermemory.search as engine  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402

import embed_train  # noqa: E402
from embed_census import Model  # noqa: E402
from embed_hybrid import bridge, ngrams  # noqa: E402
from embed_train import load_bench_module  # noqa: E402

msc = load_bench_module("msc_load", _HERE / "msc" / "load.py")

POOLINGS = ("mean", "idf")
POSTPROCS = ("raw", "centred")
BRIDGINGS = (False, True)
PRIMARY_CELL = "mean_centred_bridge"
REACH_RANK = 10
POOL_FLOOR = 20
LICENSE_SHARE = 0.50
TWITCH_SHARE = 0.25
RETRIEVAL_DEPTH = 200
E1_A40_EPISODES = 40
A160_EPISODES = 160
PROBE_SESSIONS = (2, 3, 4)


def _cell_name(pooling: str, postproc: str, bridging: bool) -> str:
    return f"{pooling}_{postproc}_{'bridge' if bridging else 'nobridge'}"


def _query_tokens(text: str) -> list[str]:
    raw = engine._expand_kebab(engine.tokenize(text))
    return sorted(set(engine._strip_stopwords(raw)))


def _doc_tokens(memory: Any) -> set[str]:
    return set(engine._memory_tokens(memory).content)


def _distinct_sessions(
    ranked_ids: list[str], id_to_session: dict[str, str]
) -> list[str]:
    """The LongMemEval runner's collapse, verbatim: first occurrence wins."""
    seen: list[str] = []
    known = set()
    for mid in ranked_ids:
        sid = id_to_session.get(mid)
        if sid is not None and sid not in known:
            known.add(sid)
            seen.append(sid)
    return seen


def _stratum(rank_0: int | None) -> str:
    if rank_0 is None:
        return "absent"
    if rank_0 == 0:
        return "hit@1"
    if rank_0 <= 4:
        return "near(2-5)"
    if rank_0 <= 9:
        return "mid(6-10)"
    return "far(11+)"


def _quartiles(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p25": ordered[len(ordered) // 4],
        "median": ordered[len(ordered) // 2],
        "p75": ordered[(3 * len(ordered)) // 4],
    }


def _median_none_as_inf(ranks: list[int | None]) -> int | str | None:
    if not ranks:
        return None
    ordered = sorted(math.inf if r is None else r for r in ranks)
    mid = ordered[len(ordered) // 2]
    return "inf" if math.isinf(mid) else int(mid)


# ---------------------------------------------------------------------------
# Probes — the declaration's §2, mechanically
# ---------------------------------------------------------------------------


def _annotation_rows(session_index: int) -> list[dict[str, Any]]:
    path = (
        msc.DATA
        / "msc"
        / "msc_personasummary"
        / f"session_{session_index}"
        / "test.txt"
    )
    if not path.exists():
        raise SystemExit(f"annotation file missing: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _annotation_sha(session_index: int) -> str:
    path = (
        msc.DATA
        / "msc"
        / "msc_personasummary"
        / f"session_{session_index}"
        / "test.txt"
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _session_turns(session: dict[str, Any]) -> list[str]:
    """Flatten a loader session's rounds back to bare turn texts."""
    turns: list[str] = []
    for rd in session["rounds"]:
        for line in rd.split("\n"):
            turns.append(line.split(": ", 1)[1])
    return turns


def build_probes(
    episode_list: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every declared probe over the full test split, in declared order.

    Returns (probes, counters). A probe is {episode_id, episode_index,
    session, line, sha16}; the declared sort is (episode file order,
    session, line sha). The alignment gate aborts on any (episode,
    session) whose annotation turns differ from the loader's.
    """
    by_id = {e["episode_id"]: e for e in episode_list}
    order = {e["episode_id"]: i for i, e in enumerate(episode_list)}
    lines_seen = 0
    per_episode: dict[str, dict[str, set[int]]] = {}
    for session_index in PROBE_SESSIONS:
        for row in _annotation_rows(session_index):
            episode_id = str(row["initial_data_id"])
            episode = by_id.get(episode_id)
            if episode is None:
                raise SystemExit(f"annotation row for unknown episode {episode_id!r}")
            session = next(
                s for s in episode["sessions"] if s["index"] == session_index
            )
            annotation_turns = [str(t["text"]) for t in row["dialog"]]
            if _session_turns(session) != annotation_turns:
                raise SystemExit(
                    f"ALIGNMENT FAILURE at episode {episode_id} session "
                    f"{session_index} — annotation turns do not match the "
                    "loader's; no artifact is produced from misaligned gold"
                )
            bucket = per_episode.setdefault(episode_id, {})
            for turn in row["dialog"]:
                for raw in turn.get("agg_persona_list", []):
                    line = str(raw).strip()
                    if not line:
                        continue
                    lines_seen += 1
                    bucket.setdefault(line, set()).add(session_index)

    probes: list[dict[str, Any]] = []
    dropped_multi = 0
    for episode_id, bucket in per_episode.items():
        for line, sessions in bucket.items():
            if len(sessions) > 1:
                dropped_multi += 1
                continue
            digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
            probes.append(
                {
                    "episode_id": episode_id,
                    "episode_index": order[episode_id],
                    "session": next(iter(sessions)),
                    "line": line,
                    "sha16": digest,
                }
            )
    probes.sort(key=lambda p: (p["episode_index"], p["session"], p["sha16"]))
    counters = {
        "annotation_lines": lines_seen,
        "unique_episode_lines": sum(len(b) for b in per_episode.values()),
        "dropped_multi_session": dropped_multi,
        "probes": len(probes),
    }
    return probes, counters


def probe_set_sha256(probes: list[dict[str, Any]]) -> str:
    payload = "\n".join(
        f"{p['episode_id']}/s{p['session']}/{p['sha16']}" for p in probes
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Dense machinery — the dense census declaration's §1, verbatim semantics
# ---------------------------------------------------------------------------


def _train_units(units: list[str]) -> tuple[list[str], dict[str, list[float]]]:
    """`embed_train.build`'s core over store bodies, constants untouched."""
    streams = embed_train.token_streams(units)
    vocab, index = embed_train.build_vocab(streams)
    counts = embed_train.cooccurrence(streams, index)
    counts = {k: v for k, v in counts.items() if v >= embed_train.MIN_COOC}
    if not vocab or not counts:
        return [], {}
    vectors, _ = embed_train.train(counts, len(vocab))
    return vocab, {t: vectors[i] for i, t in enumerate(vocab)}


def _pool_vector(
    tokens: list[str],
    model: Model,
    *,
    weights: dict[str, float] | None,
    default_weight: float = 1.0,
    gram_index: dict[str, frozenset[str]] | None,
) -> tuple[list[float] | None, int, int]:
    """(pooled unit vector | None, tokens bridged, tokens dropped)."""
    dim = len(next(iter(model.vec.values())))
    acc = [0.0] * dim
    total = 0.0
    bridged = dropped = 0
    for token in tokens:
        vec = model.vec.get(token)
        if vec is None and gram_index is not None:
            vec = bridge(token, model, gram_index)
            if vec is not None:
                bridged += 1
        if vec is None:
            dropped += 1
            continue
        weight = 1.0 if weights is None else weights.get(token, default_weight)
        if weight <= 0.0:
            dropped += 1
            continue
        for d in range(dim):
            acc[d] += weight * vec[d]
        total += weight
    if total <= 0.0:
        return None, bridged, dropped
    norm = math.sqrt(sum(v * v for v in acc))
    if norm <= 0.0:
        return None, bridged, dropped
    return [v / norm for v in acc], bridged, dropped


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _idf_weights(doc_token_sets: list[set[str]]) -> dict[str, float]:
    n = len(doc_token_sets)
    df: dict[str, int] = {}
    for tokens in doc_token_sets:
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    return {t: math.log(n / max(d, 1)) for t, d in df.items()}


class DenseStore:
    """One store's trained model plus everything scoring needs."""

    def __init__(
        self,
        memories: list[Any],
        id_to_session: dict[str, str],
        postprocs: tuple[str, ...],
    ):
        self.id_to_session = id_to_session
        self.docs = [
            (hashlib.sha256(m.body.encode("utf-8")).hexdigest(), m.id, m)
            for m in memories
        ]
        self.doc_token_sets = [_doc_tokens(m) for _, _, m in self.docs]
        self.idf = _idf_weights(self.doc_token_sets)
        self.clamped = math.log(max(len(self.doc_token_sets), 2))
        vocab, vectors = _train_units([m.body for _, _, m in self.docs])
        self.trained = bool(vocab)
        self.vocab_size = len(vocab)
        self.models: dict[str, Model] = {}
        self.gram_indexes: dict[str, dict[str, frozenset[str]]] = {}
        if self.trained:
            for mode in postprocs:
                model = Model(vocab, vectors, mode)
                self.models[mode] = model
                self.gram_indexes[mode] = {t: ngrams(t) for t in model.vocab}
        self._doc_vecs: dict[tuple[str, str], list[tuple[str, str, list[float]]]] = {}
        self._unpooled: dict[tuple[str, str], int] = {}

    def doc_vecs(
        self, postproc: str, pooling: str
    ) -> tuple[list[tuple[str, str, list[float]]], int]:
        key = (postproc, pooling)
        if key not in self._doc_vecs:
            model = self.models[postproc]
            weights = self.idf if pooling == "idf" else None
            pooled: list[tuple[str, str, list[float]]] = []
            unpooled = 0
            for (digest, mid, _), tokens in zip(self.docs, self.doc_token_sets):
                vec, _, _ = _pool_vector(
                    sorted(tokens),
                    model,
                    weights=weights,
                    default_weight=self.clamped,
                    gram_index=None,
                )
                if vec is None:
                    unpooled += 1
                else:
                    pooled.append((digest, mid, vec))
            self._doc_vecs[key] = pooled
            self._unpooled[key] = unpooled
        return self._doc_vecs[key], self._unpooled[key]

    def dense_rank(
        self, query: str, gold_key: str, pooling: str, postproc: str, bridging: bool
    ) -> tuple[int | None, int, int]:
        """(1-indexed gold-session rank | None, bridged, dropped)."""
        if not self.trained:
            return None, 0, 0
        model = self.models[postproc]
        weights = self.idf if pooling == "idf" else None
        tokens = _query_tokens(query)
        qvec, bridged, dropped = _pool_vector(
            tokens,
            model,
            weights=weights,
            default_weight=self.clamped,
            gram_index=self.gram_indexes[postproc] if bridging else None,
        )
        if qvec is None:
            return None, bridged, dropped
        docs, _ = self.doc_vecs(postproc, pooling)
        scored = sorted((-_dot(qvec, vec), digest, mid) for digest, mid, vec in docs)
        ranked = _distinct_sessions([mid for _, _, mid in scored], self.id_to_session)
        try:
            return ranked.index(gold_key) + 1, bridged, dropped
        except ValueError:
            return None, bridged, dropped


def lexical_rank(
    memories: list[Any], query: str, gold_key: str, id_to_session: dict[str, str]
) -> int | None:
    """0-indexed collapsed gold rank under the shipped engine, or None."""
    hits = run_search(
        memories,
        query,
        max_results=RETRIEVAL_DEPTH,
        mode="hybrid",
        rescue_expansion=False,
    )
    ranked = _distinct_sessions([h.id for h in hits], id_to_session)
    try:
        return ranked.index(gold_key)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


def _gold_key_aggregate(probe: dict[str, Any]) -> str:
    return f"{probe['episode_id']}/s{probe['session']}"


def _assert_gold_present(
    probes: list[dict[str, Any]], id_to_session: dict[str, str], label: str
) -> None:
    present = set(id_to_session.values())
    for probe in probes:
        if _gold_key_aggregate(probe) not in present:
            raise SystemExit(
                f"{label}: gold session {_gold_key_aggregate(probe)} missing "
                "from the store — construction and probes disagree"
            )


def e1_census(
    episode_list: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    progress: bool,
) -> dict[str, Any]:
    """Single-episode anchor: primary cell dense + lexical, per probe."""
    by_episode: dict[str, list[dict[str, Any]]] = {}
    for probe in probes:
        by_episode.setdefault(probe["episode_id"], []).append(probe)

    lex_ranks: list[int | None] = []
    dense_ranks: list[int | None] = []
    stores_untrained = 0
    training: list[dict[str, Any]] = []
    started = time.time()
    for i, episode in enumerate(episode_list):
        eplist = by_episode.get(episode["episode_id"], [])
        root = Path(tempfile.mkdtemp(prefix="bm-msc-e1-"))
        try:
            id_to_session, n_items = msc.build_episode_store(root, episode)
            memories = Store(root).load_all()
        finally:
            shutil.rmtree(root, ignore_errors=True)
        for probe in eplist:
            if f"s{probe['session']}" not in set(id_to_session.values()):
                raise SystemExit(
                    f"E1: gold session s{probe['session']} missing for "
                    f"episode {probe['episode_id']}"
                )
        dense = DenseStore(memories, id_to_session, ("centred",))
        if not dense.trained:
            stores_untrained += 1
        training.append(
            {
                "episode_id": episode["episode_id"],
                "items": n_items,
                "vocab": dense.vocab_size,
            }
        )
        for probe in eplist:
            gold = f"s{probe['session']}"
            lex_ranks.append(lexical_rank(memories, probe["line"], gold, id_to_session))
            rank, _, _ = dense.dense_rank(probe["line"], gold, "mean", "centred", True)
            dense_ranks.append(rank)
        if progress:
            rate = (i + 1) / max(1e-9, time.time() - started)
            print(
                f"  [E1] {i + 1}/{len(episode_list)} episodes ({rate:.2f}/s)",
                file=sys.stderr,
            )

    def _first_share(ranks: list[int | None], first: int) -> float:
        return round(
            sum(1 for r in ranks if r == first) / len(ranks) if ranks else 0.0, 4
        )

    return {
        "episodes": len(episode_list),
        "probes": len(lex_ranks),
        "stores_untrained": stores_untrained,
        "training": training,
        "lexical_rank0": lex_ranks,
        "dense_rank1_primary": dense_ranks,
        "summary": {
            "lexical_first_share": _first_share(lex_ranks, 0),
            "dense_first_share": _first_share(dense_ranks, 1),
            "lexical_median_none_as_inf": _median_none_as_inf(lex_ranks),
            "dense_median_none_as_inf": _median_none_as_inf(dense_ranks),
            "chance_first_share": 0.2,
        },
    }


def a40_census(
    episode_list: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    progress: bool,
) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="bm-msc-a40-"))
    try:
        id_to_session, n_items = msc.build_aggregate_store(root, episode_list)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    _assert_gold_present(probes, id_to_session, "A40")
    n_sessions = len(set(id_to_session.values()))

    lex_ranks: list[int | None] = []
    overlaps: list[float | None] = []
    by_id = {m.id: m for m in memories}
    session_tokens: dict[str, set[str]] = {}
    for mid, key in id_to_session.items():
        session_tokens.setdefault(key, set()).update(_doc_tokens(by_id[mid]))

    started = time.time()
    for i, probe in enumerate(probes):
        gold = _gold_key_aggregate(probe)
        lex_ranks.append(lexical_rank(memories, probe["line"], gold, id_to_session))
        qtokens = set(_query_tokens(probe["line"]))
        overlaps.append(
            round(len(qtokens & session_tokens[gold]) / len(qtokens), 4)
            if qtokens
            else None
        )
        if progress and (i + 1) % 100 == 0:
            rate = (i + 1) / max(1e-9, time.time() - started)
            print(
                f"  [A40 lexical] {i + 1}/{len(probes)} ({rate:.1f}/s)",
                file=sys.stderr,
            )
    strata = [_stratum(r) for r in lex_ranks]

    if progress:
        print("  [A40] training store model...", file=sys.stderr)
    dense = DenseStore(memories, id_to_session, POSTPROCS)
    if not dense.trained:
        raise SystemExit("A40: store model failed to train — no artifact")

    cells: dict[str, Any] = {}
    for pooling in POOLINGS:
        for postproc in POSTPROCS:
            for bridging in BRIDGINGS:
                name = _cell_name(pooling, postproc, bridging)
                ranks: list[int | None] = []
                bridged_total = dropped_total = 0
                started = time.time()
                for i, probe in enumerate(probes):
                    rank, bridged, dropped = dense.dense_rank(
                        probe["line"],
                        _gold_key_aggregate(probe),
                        pooling,
                        postproc,
                        bridging,
                    )
                    ranks.append(rank)
                    bridged_total += bridged
                    dropped_total += dropped
                    if progress and (i + 1) % 400 == 0:
                        rate = (i + 1) / max(1e-9, time.time() - started)
                        print(
                            f"  [A40 {name}] {i + 1}/{len(probes)} ({rate:.0f}/s)",
                            file=sys.stderr,
                        )
                _, unpooled = dense.doc_vecs(postproc, pooling)
                far_absent = [
                    r for r, s in zip(ranks, strata) if s in ("far(11+)", "absent")
                ]
                hit1 = [r for r, s in zip(ranks, strata) if s == "hit@1"]
                reached = sum(
                    1 for r in far_absent if r is not None and r <= REACH_RANK
                )
                cells[name] = {
                    "dense_rank1": ranks,
                    "unpooled_docs": unpooled,
                    "query_tokens_bridged": bridged_total,
                    "query_tokens_dropped": dropped_total,
                    "summary": {
                        "far_absent_pool": len(far_absent),
                        "far_absent_reached_at_10": reached,
                        "far_absent_reach_share": round(reached / len(far_absent), 4)
                        if far_absent
                        else None,
                        "hit1_median_none_as_inf": _median_none_as_inf(hit1),
                        "unconditional_reach_share": round(
                            sum(1 for r in ranks if r is not None and r <= REACH_RANK)
                            / len(ranks),
                            4,
                        ),
                        "by_stratum": {
                            stratum: _quartiles(
                                [
                                    r
                                    for r, s in zip(ranks, strata)
                                    if s == stratum and r is not None
                                ]
                            )
                            for stratum in sorted(set(strata))
                        },
                    },
                }

    overlap_by_stratum: dict[str, Any] = {}
    for stratum in sorted(set(strata)):
        values = [o for o, s in zip(overlaps, strata) if s == stratum and o is not None]
        overlap_by_stratum[stratum] = (
            {
                "n": len(values),
                "mean": round(statistics.mean(values), 4),
                "median": round(statistics.median(values), 4),
            }
            if values
            else None
        )

    return {
        "items": n_items,
        "sessions": n_sessions,
        "probes": len(probes),
        "model_vocab": dense.vocab_size,
        "lexical_rank0": lex_ranks,
        "lexical_strata_counts": {s: strata.count(s) for s in sorted(set(strata))},
        "overlap_share": overlaps,
        "overlap_by_stratum": overlap_by_stratum,
        "cells": cells,
    }


def a160_census(
    episode_list: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    progress: bool,
) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="bm-msc-a160-"))
    try:
        id_to_session, n_items = msc.build_aggregate_store(root, episode_list)
        memories = Store(root).load_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    _assert_gold_present(probes, id_to_session, "A160")
    n_sessions = len(set(id_to_session.values()))

    if progress:
        print("  [A160] training store model...", file=sys.stderr)
    dense = DenseStore(memories, id_to_session, ("centred",))
    if not dense.trained:
        raise SystemExit("A160: store model failed to train — no artifact")

    ranks: list[int | None] = []
    started = time.time()
    for i, probe in enumerate(probes):
        rank, _, _ = dense.dense_rank(
            probe["line"], _gold_key_aggregate(probe), "mean", "centred", True
        )
        ranks.append(rank)
        if progress and (i + 1) % 400 == 0:
            rate = (i + 1) / max(1e-9, time.time() - started)
            print(
                f"  [A160 primary] {i + 1}/{len(probes)} ({rate:.0f}/s)",
                file=sys.stderr,
            )
    return {
        "items": n_items,
        "sessions": n_sessions,
        "probes": len(probes),
        "model_vocab": dense.vocab_size,
        "dense_rank1_primary": ranks,
        "summary": {
            "unconditional_reach_share": round(
                sum(1 for r in ranks if r is not None and r <= REACH_RANK) / len(ranks),
                4,
            ),
            "median_none_as_inf": _median_none_as_inf(ranks),
        },
    }


# ---------------------------------------------------------------------------
# The ladder — the declaration's §5, top to bottom
# ---------------------------------------------------------------------------


def readiness(a40: dict[str, Any]) -> dict[str, Any]:
    shares = {
        name: cell["summary"]["far_absent_reach_share"]
        for name, cell in a40["cells"].items()
    }
    pool = a40["cells"][PRIMARY_CELL]["summary"]["far_absent_pool"]
    primary = shares[PRIMARY_CELL]
    median = a40["cells"][PRIMARY_CELL]["summary"]["hit1_median_none_as_inf"]
    r2 = median is not None and median != "inf" and median <= REACH_RANK

    if pool < POOL_FLOOR:
        outcome = "shape_only"
    elif primary is not None and primary >= LICENSE_SHARE:
        outcome = "licensed"
    elif any(s is not None and s >= LICENSE_SHARE for s in shares.values()):
        outcome = "anti_gate_shopping"
    elif any(s is not None and s >= TWITCH_SHARE for s in shares.values()):
        outcome = "twitch"
    else:
        outcome = "park"

    return {
        "primary_cell": PRIMARY_CELL,
        "far_absent_pool": pool,
        "pool_floor": POOL_FLOOR,
        "family_far_absent_reach_share": shares,
        "R1_reach": outcome == "licensed",
        "R2_preservation": r2,
        "routing": (
            "leg or rerank-window"
            if outcome == "licensed" and r2
            else "rerank-window only"
            if outcome == "licensed"
            else None
        ),
        "outcome": outcome,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="MSC scale census. Statistics only.")
    p.add_argument("--out", default=None, metavar="PATH")
    p.add_argument("--progress", action="store_true")
    args = p.parse_args()

    episode_list = msc.episodes("test")
    probes_all, counters = build_probes(episode_list)
    first40 = episode_list[:E1_A40_EPISODES]
    first160 = episode_list[:A160_EPISODES]
    ids40 = {e["episode_id"] for e in first40}
    ids160 = {e["episode_id"] for e in first160}
    probes40 = [p_ for p_ in probes_all if p_["episode_id"] in ids40]
    probes160 = [p_ for p_ in probes_all if p_["episode_id"] in ids160]

    if args.progress:
        print(
            f"probes: {counters['probes']} declared "
            f"({len(probes40)} in first {E1_A40_EPISODES}, "
            f"{len(probes160)} in first {A160_EPISODES})",
            file=sys.stderr,
        )

    e1 = e1_census(first40, probes40, args.progress)
    a40 = a40_census(first40, probes40, args.progress)
    a160 = a160_census(first160, probes160, args.progress)

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
            "declaration": "bench/MSC_SCALE_CENSUS_DECLARATION.md",
            "corpus": {
                "tarball_sha256": msc.TARBALL_SHA256,
                "split": "test",
                "split_sha256": msc.corpus_fingerprint("test"),
                "annotation_sha256": {
                    f"session_{k}": _annotation_sha(k) for k in PROBE_SESSIONS
                },
                "reproducibility": (
                    "corpus not committed; this artifact reproduces only "
                    "for a holder of the pinned bytes"
                ),
            },
            "trainer": {
                "dim": embed_train.DIM,
                "epochs": embed_train.EPOCHS,
                "seed": embed_train.SEED,
                "min_cooc": embed_train.MIN_COOC,
            },
            "retrieval_depth": RETRIEVAL_DEPTH,
            "reach_rank": REACH_RANK,
        },
        "probe_rules": counters,
        "probe_set_sha256": probe_set_sha256(probes_all),
        "probe_order_note": (
            "per-probe arrays align with the declared sort — (episode file "
            "order, session, line sha) restricted to each store's episodes; "
            "identities are re-derivable from the pinned bytes and are not "
            "repeated here"
        ),
        "E1": e1,
        "A40": a40,
        "A160": a160,
        "readiness": readiness(a40),
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
