"""W3-C builder: two evidence tables and their composition, emitted.

Implements `bench/w/W3C_DECLARATION.md` §5. Table P scores exclusive
title substitution from the W3-P2 pair file with PPMI under W3-P's
declared floors; Table D scores the W3-D edge file's attestations
under W3-D's label weights, SYMMETRIZED per the declared deviation
(storage direction is not use direction — the leg expands query
tokens). The composition interleaves per-table RANK (never
cross-table score), D-first 2+2 with backfill to four, and emits the
`SURFACE_NEIGHBORS` module shape the committed W1 harness
(`bench/w/w1_measure.py`) loads unchanged.

Both input files are re-verified against the sha256 values their
units' census artifacts published before a line is read. The build is
deterministic end to end: same inputs, same floors → same bytes; the
CI leg (`tests/test_w3c_determinism.py`) drives every rule with
hand-written fixtures and no derived-file bytes.

Run: fastvenv/bin/python bench/w/w3c_bridge.py
Artifacts: bench/w/artifacts/w3c_table_<date>.py + w3c-build-<date>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w3p_anatomy import NEEDS  # noqa: E402
from w3p_pairs import content_tokens, provenance, sha256_of  # noqa: E402

PAIRS_CENSUS = REPO / "bench/w/results/w3p2-census-2026-08-17.json"
EDGES_CENSUS = REPO / "bench/w/results/w3d-census-2026-08-17.json"

# Declaration §5: Table P floors (W3-P §5's rule made concrete).
P_COUNT_FLOOR = 10
P_PPMI_FLOOR = 2.0
P_MUTUAL_RANK = 8
P_TERMS_PER_HEAD = 4
P_HEAD_CAP = 5_000

# Declaration §5: Table D label weights and floors.
D_LABEL_WEIGHTS = {
    "synonyms": 6,
    "hypernyms": 4,
    "hyponyms": 4,
    "gloss-link": 2,
    "lead-link": 2,
    "gloss": 1,
    "lead": 1,
}
D_SCORE_FLOOR = 2
D_TERMS_PER_HEAD = 4
D_HEAD_CAP = 5_000

# Declaration §5: the composition.
COMPOSE_D_SLOTS = 2
COMPOSE_P_SLOTS = 2
COMPOSE_TERMS_PER_HEAD = 4
COMPOSE_HEAD_CAP = 5_000
TABLE_SOURCE_CAP_BYTES = 300 * 1024


@dataclass
class RankedTable:
    """heads in cap order; per head, terms in kept-rank order."""

    heads: list[str]
    terms: dict[str, list[str]]

    def rank_of(self, head: str) -> int | None:
        try:
            return self.heads.index(head)
        except ValueError:
            return None


def build_table_p(
    pair_lines: Iterable[str],
    *,
    count_floor: int = P_COUNT_FLOOR,
    ppmi_floor: float = P_PPMI_FLOOR,
    mutual_rank: int = P_MUTUAL_RANK,
    terms_per_head: int = P_TERMS_PER_HEAD,
    head_cap: int = P_HEAD_CAP,
) -> tuple[RankedTable, dict[str, int]]:
    """PPMI over exclusive substitution, floored and capped (§5)."""
    counts: dict[tuple[str, str], int] = {}
    for line in pair_lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            continue
        _site, left, right = parts
        t1 = frozenset(content_tokens(left))
        t2 = frozenset(content_tokens(right))
        for a in t1 - t2:
            for b in t2 - t1:
                key = (a, b) if a <= b else (b, a)
                counts[key] = counts.get(key, 0) + 1

    kept = {k: c for k, c in counts.items() if c >= count_floor}
    marginal: dict[str, int] = {}
    for (a, b), c in kept.items():
        marginal[a] = marginal.get(a, 0) + c
        marginal[b] = marginal.get(b, 0) + c
    total = sum(marginal.values())  # both directions of every pair

    ppmi: dict[str, list[tuple[str, float]]] = {}
    for (a, b), c in kept.items():
        value = math.log2((c * total) / (marginal[a] * marginal[b]))
        if value < ppmi_floor:
            continue
        ppmi.setdefault(a, []).append((b, value))
        ppmi.setdefault(b, []).append((a, value))
    for head in ppmi:
        ppmi[head].sort(key=lambda tv: (-tv[1], tv[0]))

    def within_mutual(head: str, term: str) -> bool:
        mates = ppmi.get(term, [])
        return any(t == head for t, _v in mates[:mutual_rank])

    pruned: dict[str, list[tuple[str, float]]] = {}
    for head, mates in ppmi.items():
        rows = [(t, v) for t, v in mates[:mutual_rank] if within_mutual(head, t)][
            :terms_per_head
        ]
        if rows:
            pruned[head] = rows

    ordered_heads = sorted(
        pruned,
        key=lambda h: (-sum(v for _t, v in pruned[h]), h),
    )[:head_cap]
    table = RankedTable(
        heads=ordered_heads,
        terms={h: [t for t, _v in pruned[h]] for h in ordered_heads},
    )
    stats = {
        "substitution_pair_types": len(counts),
        "types_at_count_floor": len(kept),
        "heads_kept": len(table.heads),
    }
    return table, stats


def build_table_d(
    edge_lines: Iterable[str],
    *,
    score_floor: int = D_SCORE_FLOOR,
    terms_per_head: int = D_TERMS_PER_HEAD,
    head_cap: int = D_HEAD_CAP,
) -> tuple[RankedTable, dict[str, int]]:
    """Label-weighted attestation sums, symmetrized, floored, capped."""
    scores: dict[tuple[str, str], int] = {}
    attested = 0
    for line in edge_lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 5:
            continue
        head, term, label, _source, _count = parts
        weight = D_LABEL_WEIGHTS.get(label)
        if weight is None:
            continue
        attested += 1
        scores[(head, term)] = scores.get((head, term), 0) + weight
        scores[(term, head)] = scores.get((term, head), 0) + weight

    by_head: dict[str, list[tuple[str, int]]] = {}
    for (head, term), score in scores.items():
        if score < score_floor:
            continue
        by_head.setdefault(head, []).append((term, score))
    for head in by_head:
        by_head[head].sort(key=lambda ts: (-ts[1], ts[0]))
        by_head[head] = by_head[head][:terms_per_head]

    ordered_heads = sorted(
        by_head,
        key=lambda h: (-sum(s for _t, s in by_head[h]), h),
    )[:head_cap]
    table = RankedTable(
        heads=ordered_heads,
        terms={h: [t for t, _s in by_head[h]] for h in ordered_heads},
    )
    stats = {
        "attestations_read": attested,
        "directed_pairs_scored": len(scores),
        "heads_kept": len(table.heads),
    }
    return table, stats


def compose(
    table_d: RankedTable,
    table_p: RankedTable,
    *,
    d_slots: int = COMPOSE_D_SLOTS,
    p_slots: int = COMPOSE_P_SLOTS,
    terms_per_head: int = COMPOSE_TERMS_PER_HEAD,
    head_cap: int = COMPOSE_HEAD_CAP,
) -> dict[str, list[str]]:
    """§5's interleave: D[:2], P[:2], dedup keeps first, backfill D then P."""

    def head_key(head: str) -> tuple[int, int, int, str]:
        rank_d = table_d.rank_of(head)
        rank_p = table_p.rank_of(head)
        in_both = 0 if (rank_d is not None and rank_p is not None) else 1
        candidates = [
            (rank, source)
            for rank, source in ((rank_d, 0), (rank_p, 1))
            if rank is not None
        ]
        best_rank, source = min(candidates)
        return (in_both, best_rank, source, head)

    heads = sorted(set(table_d.heads) | set(table_p.heads), key=head_key)
    composed: dict[str, list[str]] = {}
    for head in heads[:head_cap]:
        d_terms = table_d.terms.get(head, [])
        p_terms = table_p.terms.get(head, [])
        out: list[str] = []
        for term in (
            d_terms[:d_slots]
            + p_terms[:p_slots]
            + d_terms[d_slots:]
            + p_terms[p_slots:]
        ):
            if term != head and term not in out:
                out.append(term)
            if len(out) >= terms_per_head:
                break
        if out:
            composed[head] = out
    return composed


def b0_rows(composed: dict[str, list[str]]) -> dict[str, object]:
    """§4's build census: per need, does the table carry a bridge."""
    rows = {}
    survived = 0
    for qid in sorted(NEEDS):
        left, right, register, gloss = NEEDS[qid]
        bridges = []
        for a_side, b_side in ((left, right), (right, left)):
            for head in sorted(a_side):
                hits = [t for t in composed.get(head, []) if t in b_side]
                bridges.extend((head, t) for t in hits)
        rows[qid] = {
            "bridges": [list(b) for b in bridges],
            "survived": bool(bridges),
            "register": register,
            "gloss": gloss,
        }
        if bridges:
            survived += 1
    return {"needs": rows, "survived": survived}


def emit_table_source(composed: dict[str, list[str]], provenance_line: str) -> str:
    lines = [
        '"""W3-C composed bridge table — generated, do not hand-edit.',
        "",
        provenance_line,
        "Loaded by bench/w/w3c_measure invocations through",
        "bench/w/w1_measure.py (arm: full). Shape: SURFACE_NEIGHBORS,",
        "head -> bridge terms in composed rank order.",
        '"""',
        "",
        "SURFACE_NEIGHBORS: dict[str, tuple[str, ...]] = {",
    ]
    for head in sorted(composed):
        terms = ", ".join(repr(t) for t in composed[head])
        lines.append(f"    {head!r}: ({terms},),")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _pinned_sha(census_path: Path, key: str) -> str:
    artifact = json.loads(census_path.read_text())
    return str(artifact[key]["sha256"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the W3-C composed table.")
    parser.add_argument("--p-count-floor", type=int, default=P_COUNT_FLOOR)
    parser.add_argument("--p-ppmi-floor", type=float, default=P_PPMI_FLOOR)
    parser.add_argument("--d-score-floor", type=int, default=D_SCORE_FLOOR)
    parser.add_argument("--d-slots", type=int, default=COMPOSE_D_SLOTS)
    parser.add_argument("--p-slots", type=int, default=COMPOSE_P_SLOTS)
    parser.add_argument("--tag", default="", help="artifact filename suffix")
    args = parser.parse_args()

    date = time.strftime("%Y-%m-%d")
    tag = f"-{args.tag}" if args.tag else ""
    pairs_path = REPO / "bench/w/corpus/derived/w3p2-pairs-2026-08-17.tsv"
    edges_path = REPO / "bench/w/corpus/derived/w3d-edges-2026-08-17.tsv"
    table_path = REPO / "bench" / "w" / "artifacts" / f"w3c_table_{date}{tag}.py"
    json_path = REPO / "bench" / "w" / "artifacts" / f"w3c-build-{date}{tag}.json"

    pinned = {
        "pairs": _pinned_sha(PAIRS_CENSUS, "pair_file"),
        "edges": _pinned_sha(EDGES_CENSUS, "edge_file"),
    }
    for name, path in (("pairs", pairs_path), ("edges", edges_path)):
        print(f"{path.name}: re-verifying pinned sha256 ...", file=sys.stderr)
        actual = sha256_of(path)
        if actual != pinned[name]:
            raise SystemExit(
                f"{path.name}: sha mismatch (pinned {pinned[name]}, actual"
                f" {actual}); the pin is the authority — nothing is read"
            )

    started = time.time()
    with pairs_path.open("r", encoding="utf-8") as handle:
        table_p, stats_p = build_table_p(
            handle,
            count_floor=args.p_count_floor,
            ppmi_floor=args.p_ppmi_floor,
        )
    with edges_path.open("r", encoding="utf-8") as handle:
        table_d, stats_d = build_table_d(handle, score_floor=args.d_score_floor)
    composed = compose(table_d, table_p, d_slots=args.d_slots, p_slots=args.p_slots)

    floors = {
        "p_count_floor": args.p_count_floor,
        "p_ppmi_floor": args.p_ppmi_floor,
        "p_mutual_rank": P_MUTUAL_RANK,
        "p_terms_per_head": P_TERMS_PER_HEAD,
        "p_head_cap": P_HEAD_CAP,
        "d_score_floor": args.d_score_floor,
        "d_terms_per_head": D_TERMS_PER_HEAD,
        "d_head_cap": D_HEAD_CAP,
        "compose_d_slots": args.d_slots,
        "compose_p_slots": args.p_slots,
        "compose_terms_per_head": COMPOSE_TERMS_PER_HEAD,
        "compose_head_cap": COMPOSE_HEAD_CAP,
    }
    provenance_line = (
        f"Inputs: w3p2-pairs sha256 {pinned['pairs'][:16]}..., "
        f"w3d-edges sha256 {pinned['edges'][:16]}...; floors in "
        f"{json_path.name}."
    )
    source = emit_table_source(composed, provenance_line)
    head_order = list(composed)  # composition cap order; the trim eats its tail
    while len(source.encode()) > TABLE_SOURCE_CAP_BYTES and head_order:
        composed.pop(head_order.pop())
        source = emit_table_source(composed, provenance_line)
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(source)
    table_sha = hashlib.sha256(source.encode()).hexdigest()

    b0 = b0_rows(composed)
    artifact = {
        "provenance": provenance(),
        "declaration": "bench/w/W3C_DECLARATION.md",
        "inputs": {
            "pair_file": {
                "path": str(pairs_path.relative_to(REPO)),
                "sha256": pinned["pairs"],
            },
            "edge_file": {
                "path": str(edges_path.relative_to(REPO)),
                "sha256": pinned["edges"],
            },
        },
        "floors": floors,
        "table_p": stats_p,
        "table_d": stats_d,
        "composed": {
            "heads": len(composed),
            "source_bytes": len(source.encode()),
            "table_path": str(table_path.relative_to(REPO)),
            "table_sha256": table_sha,
        },
        "b0": b0,
        "build_seconds": round(time.time() - started, 1),
    }
    json_path.write_text(json.dumps(artifact, indent=1) + "\n")
    print(json.dumps({"b0": b0, "composed_heads": len(composed)}, indent=1))
    print(f"table: {table_path.relative_to(REPO)} (sha256 {table_sha[:16]}...)")
    print(f"build artifact: {json_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
