"""What a dedup `related`-breadcrumb floor change actually buys.

THE QUESTION. `find_similar` labels a pair `high` at >= 0.75 (block the
write) and `medium` at >= `MEDIUM_SIMILARITY` (0.40 — surface it as a
`related` breadcrumb on the committed response). The 2026-07-30 audit
observed real paraphrase pairs sitting at Jaccard 0.17-0.33, below the
medium floor, and proposed lowering the floor to 0.30 so they surface.
A breadcrumb is a hint attached to EVERY write, so a floor change is
paid per write forever — it has to be measured before it ships.

TWO ARMS, BECAUSE "LOWER THE FLOOR" IS TWO DIFFERENT CHANGES.

  surgical  Only the reported floor moves: `find_similar` already takes
            `medium_threshold`, so this arm just passes a lower one.
            `_pairwise_content_jaccard`'s containment gate and ceiling
            stay where they are.

  naive     `MEDIUM_SIMILARITY = 0.30` in the source. That constant is
            load-bearing in THREE places, not one: the reported floor,
            the gate that lets the containment score fire at all
            (`_pairwise_content_jaccard`), and `_CONTAINMENT_CEILING =
            (HIGH + MEDIUM) / 2`. This arm reproduces the edit by
            rebinding the module globals, the same technique bench/rot
            uses on verify.py.

NO RE-IMPLEMENTATION. Every arm calls the shipped `find_similar`. A
bench that re-derives the scorer measures the re-derivation; this one
cannot drift from the code it is about.

PRIVACY. The default corpus is the operator's own live store, which is
personal data. This script READS it and never writes to it (it parses
files through `store._parse_memory_file` rather than constructing a
`Store`, which would mkdir and chmod). The emitted report is COUNTS
ONLY — no bodies, no ids, no scopes, no filenames — so a result JSON is
safe to commit from a private store. `--corpus` runs the same arms over
a committed public corpus for anyone who wants to reproduce the shape.

METHOD: LEAVE-ONE-OUT. Real future writes aren't available, so each
stored memory stands in for "a write of this content", scored against
the other N-1. This OVERSTATES breadcrumb load in both directions: a
store member's nearest neighbour is closer than a novel write's would
be. Read the per-write numbers as a ceiling, not an expectation.

Usage:

    venv/bin/python bench/dedup/run.py                    # live store
    venv/bin/python bench/dedup/run.py --json
    venv/bin/python bench/dedup/run.py --store ~/other-store
    venv/bin/python bench/dedup/run.py --corpus bench/retrieval/corpus.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

# Add `src/` to sys.path so this script is runnable without an install.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from bettermemory import search as search_mod  # noqa: E402
from bettermemory.models import (  # noqa: E402
    Confidence,
    Memory,
    Source,
    TombstonedMemory,
    generate_ulid,
    snippet_for,
)
from bettermemory.search import HIGH_SIMILARITY, find_similar  # noqa: E402
from bettermemory.store import (  # noqa: E402
    PARSE_SKIP_EXCEPTIONS,
    TOMBSTONE_DIR,
    _parse_memory_file,
)

# The floors swept in the surgical arm. 0.40 is production; 0.30 is the
# proposal; the rest bracket it so the report shows where (and whether)
# the knob starts doing anything at all.
FLOORS = (0.40, 0.35, 0.30, 0.25, 0.20, 0.15)


def load_store(directory: Path) -> tuple[list[Memory], list[TombstonedMemory]]:
    """Active + tombstoned memories, parsed WITHOUT constructing a Store.

    `Store.__post_init__` mkdirs and chmods its root. This bench points
    at the operator's real store, so it goes through the module-level
    parser instead and stays strictly read-only.

    Tombstone files share the active on-disk format, so they parse with
    the same reader; `removed` / `removed_reason` are re-attached as
    placeholders because only `body` participates in scoring and neither
    field reaches the counts-only report.
    """

    def _parse(paths: Any) -> list[Memory]:
        out: list[Memory] = []
        for path in sorted(paths):
            if not path.is_file() or path.is_symlink() or path.suffix != ".md":
                continue
            try:
                out.append(_parse_memory_file(path))
            except PARSE_SKIP_EXCEPTIONS:
                continue
        return out

    active = _parse(directory.iterdir())
    tomb_dir = directory / TOMBSTONE_DIR
    tombs = [
        TombstonedMemory(
            **memory.model_dump(),
            removed=memory.updated,
            removed_reason="(not read by this bench)",
        )
        for memory in (_parse(tomb_dir.iterdir()) if tomb_dir.is_dir() else [])
    ]
    return active, tombs


def load_corpus(path: Path) -> list[Memory]:
    """A JSONL bench corpus as Memory rows. Only `body` is load-bearing;
    the rest are placeholders the model requires."""
    now = datetime.now(timezone.utc)
    out: list[Memory] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            body = rec.get("body") or rec.get("content") or rec.get("text") or ""
            if not body:
                continue
            out.append(
                Memory(
                    id=generate_ulid(),
                    created=now,
                    updated=now,
                    scopes=["bench"],
                    confidence=Confidence.MEDIUM,
                    source=Source.EXPLICIT,
                    body=body,
                )
            )
    return out


def _scan(memories: list[Memory], *, floor: float) -> list[list[float]]:
    """Leave-one-out similarities per write, via production `find_similar`.

    One pass at the LOWEST floor of interest; every higher floor is a
    filter over the result, so the sweep costs one O(n^2) scan instead of
    one per floor. `_verify_derivation` re-runs the shipped floor
    directly and asserts the derived count matches, which is what makes
    the shortcut safe to trust.
    """
    out: list[list[float]] = []
    for i, mem in enumerate(memories):
        others = memories[:i] + memories[i + 1 :]
        hits = find_similar(mem.body, others, medium_threshold=floor)
        out.append([h.similarity for h in hits])
    return out


def _load_at(scan: list[list[float]], floor: float) -> dict[str, float | int]:
    counts = sorted(sum(1 for s in row if floor <= s < HIGH_SIMILARITY) for row in scan)
    n = len(counts) or 1
    return {
        # Each unordered pair is counted from both sides.
        "related_pairs": sum(counts) // 2,
        "mean_per_write": round(sum(counts) / n, 3),
        "median_per_write": counts[n // 2],
        "p90_per_write": counts[min(n - 1, int(n * 0.9))],
        "max_per_write": counts[-1],
        "writes_with_zero": sum(1 for c in counts if c == 0),
        "writes_with_5_or_more": sum(1 for c in counts if c >= 5),
    }


def _verify_derivation(memories: list[Memory], scan: list[list[float]]) -> None:
    """Derived counts vs a direct run at the shipped floor.

    `SimilarHit.similarity` is rounded to 4 places while the threshold
    test upstream of it is not, so a value within 5e-5 of a floor could
    in principle bucket differently in the derivation than in a real
    run. Cheap to rule out; silently wrong if it ever happens.
    """
    direct = sum(
        1
        for i, mem in enumerate(memories)
        for h in find_similar(mem.body, memories[:i] + memories[i + 1 :])
        if h.relevance == "medium"
    )
    derived = int(_load_at(scan, search_mod.MEDIUM_SIMILARITY)["related_pairs"]) * 2
    if direct != derived:
        raise AssertionError(
            f"derived related count {derived} != direct {direct} — "
            "a similarity is sitting on a rounding boundary"
        )


def _blocking_pairs(scan: list[list[float]]) -> int:
    """Pairs at or above the block threshold. Unaffected by the floor —
    reported so a reader can see the floor debate is entirely about the
    advisory band."""
    return sum(1 for row in scan for s in row if s >= HIGH_SIMILARITY) // 2


def _naive_edit(memories: list[Memory], medium: float) -> dict[str, float | int]:
    """`MEDIUM_SIMILARITY = medium` as a source edit would do it: the
    reported floor, the containment gate and the containment ceiling all
    move together."""
    saved_medium = search_mod.MEDIUM_SIMILARITY
    saved_ceiling = search_mod._CONTAINMENT_CEILING
    try:
        search_mod.MEDIUM_SIMILARITY = medium
        search_mod._CONTAINMENT_CEILING = (HIGH_SIMILARITY + medium) / 2
        return _load_at(_scan(memories, floor=medium), medium)
    finally:
        search_mod.MEDIUM_SIMILARITY = saved_medium
        search_mod._CONTAINMENT_CEILING = saved_ceiling


def _band(memories: list[Memory], *, floor: float) -> set[tuple[int, str]]:
    """`(write index, matched id)` for every `related` hit at `floor`."""
    out: set[tuple[int, str]] = set()
    for i, mem in enumerate(memories):
        others = memories[:i] + memories[i + 1 :]
        for hit in find_similar(mem.body, others, medium_threshold=floor):
            if hit.relevance == "medium":
                out.add((i, hit.id))
    return out


def _raw_jaccard_map(memories: list[Memory]) -> dict[tuple[int, str], float]:
    """Every pair's UNLIFTED Jaccard.

    Obtained by raising `_CONTAINMENT_MIN_TOKENS` out of reach rather
    than re-deriving the score: with the containment branch unreachable,
    `_pairwise_content_jaccard` returns exactly |intersection| / |union|,
    so this is still the shipped scorer answering a narrower question.
    """
    saved = search_mod._CONTAINMENT_MIN_TOKENS
    try:
        search_mod._CONTAINMENT_MIN_TOKENS = 10**9
        out: dict[tuple[int, str], float] = {}
        for i, mem in enumerate(memories):
            others = memories[:i] + memories[i + 1 :]
            for hit in find_similar(mem.body, others, medium_threshold=0.0):
                out[(i, hit.id)] = hit.similarity
        return out
    finally:
        search_mod._CONTAINMENT_MIN_TOKENS = saved


def naive_admits_what(
    memories: list[Memory],
    medium: float,
    raw: dict[tuple[int, str], float] | None = None,
) -> dict[str, Any]:
    """The real token overlap of the pairs the constant edit newly admits.

    This is the arm that decides the item. The proposal was framed as
    rescuing paraphrase pairs at Jaccard ~0.30; if the pairs the edit
    actually adds sit an order of magnitude below that, the edit is not
    a lower floor at all — it is a wider containment gate, and containment
    is generous exactly where Jaccard is not (a short body against a long
    one). Quartiles rather than a mean: the distribution is what shows
    whether the added set is paraphrase or vocabulary coincidence.
    """
    base = _band(memories, floor=search_mod.MEDIUM_SIMILARITY)
    saved_medium = search_mod.MEDIUM_SIMILARITY
    saved_ceiling = search_mod._CONTAINMENT_CEILING
    try:
        search_mod.MEDIUM_SIMILARITY = medium
        search_mod._CONTAINMENT_CEILING = (HIGH_SIMILARITY + medium) / 2
        widened = _band(memories, floor=medium)
    finally:
        search_mod.MEDIUM_SIMILARITY = saved_medium
        search_mod._CONTAINMENT_CEILING = saved_ceiling

    if raw is None:
        raw = _raw_jaccard_map(memories)
    added = sorted(raw.get(key, 0.0) for key in widened - base)
    if not added:
        return {"added_hits": 0, "raw_jaccard_quartiles": None}
    return {
        "added_hits": len(added),
        "raw_jaccard_quartiles": {
            "min": round(added[0], 4),
            "p25": round(added[len(added) // 4], 4),
            "median": round(added[len(added) // 2], 4),
            "p75": round(added[(3 * len(added)) // 4], 4),
            "max": round(added[-1], 4),
        },
        "added_hits_at_or_above_the_new_floor": sum(1 for j in added if j >= medium),
    }


def target_band_already_surfaced(
    memories: list[Memory],
    raw: dict[tuple[int, str], float],
    *,
    lower: float = 0.30,
) -> dict[str, int]:
    """How many pairs sit in the band the proposal was aimed at, and how
    many of those are ALREADY breadcrumbed today.

    The item's premise was that pairs with real Jaccard in
    [`lower`, `MEDIUM_SIMILARITY`) go unsurfaced. This counts them and
    checks the premise directly — the closed form says the containment
    lift should have caught every one whose smaller side clears
    `_CONTAINMENT_MIN_TOKENS`, and a count that disagrees means the
    argument in the README is wrong, not merely the numbers.
    """
    shipped = _band(memories, floor=search_mod.MEDIUM_SIMILARITY)
    in_band = [k for k, j in raw.items() if lower <= j < search_mod.MEDIUM_SIMILARITY]
    return {
        "hits_with_raw_jaccard_in_target_band": len(in_band),
        "of_those_already_surfaced_as_related": sum(1 for k in in_band if k in shipped),
    }


def containment_dominance(memories: list[Memory]) -> dict[str, Any]:
    """Why the surgical arm is flat: the containment lift gets there first.

    `_pairwise_content_jaccard` returns `max(jaccard, containment)` once
    containment clears `MEDIUM_SIMILARITY`, and containment is
    |intersection| / |smaller| against Jaccard's |intersection| /
    |union|. Since |union| >= |smaller|, containment >= jaccard always;
    tightening that, a pair whose Jaccard reaches f has containment at
    least 2f / (1 + f). At f = 0.25 that bound is exactly 0.40, so ANY
    reported floor >= 0.25 is unreachable — every pair it could newly
    admit was already lifted into the band by containment.

    The one gap is `_CONTAINMENT_MIN_TOKENS`: below 8 tokens on the
    smaller side containment never fires, so a short body can still land
    between the floors. This reports how many memories are even small
    enough to take that exit.
    """
    tokens = [search_mod._raw_content_token_set(m.body) for m in memories]
    return {
        "predicted_containment_floor_at_jaccard": {
            f"{f:.2f}": round(2 * f / (1 + f), 4) for f in FLOORS
        },
        "containment_gate": search_mod.MEDIUM_SIMILARITY,
        "floor_below_which_the_knob_can_bite": round(
            search_mod.MEDIUM_SIMILARITY / (2 - search_mod.MEDIUM_SIMILARITY), 4
        ),
        "containment_min_tokens": search_mod._CONTAINMENT_MIN_TOKENS,
        "memories_below_containment_min_tokens": sum(
            1 for t in tokens if len(t) < search_mod._CONTAINMENT_MIN_TOKENS
        ),
    }


def breadcrumb_wire_bytes(memories: list[Memory]) -> dict[str, float]:
    """What one breadcrumb costs on the wire, priced on real bodies.

    The shape is `_response.similar_to_dict` — the 200-char snippet plus
    id / scopes / confidence / similarity / relevance / created /
    updated. Placeholders for everything except the snippet, which is
    the part that varies.
    """
    sizes = []
    for mem in memories:
        sizes.append(
            len(
                json.dumps(
                    {
                        "id": generate_ulid(),
                        "scopes": ["projects:example"],
                        "confidence": "medium",
                        "snippet": snippet_for(mem.body),
                        "similarity": 0.4123,
                        "relevance": "medium",
                        "created": "2026-07-30T12:00:00+00:00",
                        "updated": "2026-07-30T12:00:00+00:00",
                    },
                    separators=(",", ":"),
                )
            )
        )
    sizes.sort()
    return {
        "mean": round(sum(sizes) / len(sizes), 1),
        "median": float(sizes[len(sizes) // 2]),
        "max": float(sizes[-1]),
    }


def measure(
    memories: list[Memory], tombstones: list[TombstonedMemory], label: str
) -> dict[str, Any]:
    scan = _scan(memories, floor=min(FLOORS))
    _verify_derivation(memories, scan)
    raw = _raw_jaccard_map(memories)
    report: dict[str, Any] = {
        "corpus": label,
        "bettermemory_version": version("bettermemory"),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "memories": len(memories),
        "tombstones": len(tombstones),
        "blocking_pairs_at_high": _blocking_pairs(scan),
        "surgical_floor_sweep": {f"{f:.2f}": _load_at(scan, f) for f in FLOORS},
        "naive_constant_edit": {
            f"MEDIUM_SIMILARITY={m:.2f}": _naive_edit(memories, m) for m in (0.40, 0.30)
        },
        "naive_admits_what": naive_admits_what(memories, 0.30, raw),
        "target_band_already_surfaced": target_band_already_surfaced(memories, raw),
        "containment_dominance": containment_dominance(memories),
        "breadcrumb_wire_bytes": breadcrumb_wire_bytes(memories),
    }
    if tombstones:
        # The `removed_related` leg reads the same constants, so it moves
        # with them; measured separately because it is scored against a
        # different (usually much smaller) population.
        tomb_scan = [
            [
                h.similarity
                for h in search_mod.find_similar_tombstones(
                    mem.body, tombstones, medium_threshold=min(FLOORS)
                )
            ]
            for mem in memories
        ]
        report["removed_related_hits"] = {
            f"{f:.2f}": sum(
                sum(1 for s in row if f <= s < HIGH_SIMILARITY) for row in tomb_scan
            )
            for f in (0.40, 0.30)
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="memory store to read (default: the configured store)",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="JSONL corpus to measure instead of a store (public, reproducible)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.corpus is not None:
        memories = load_corpus(args.corpus)
        report = measure(memories, [], f"corpus:{args.corpus.name}")
    else:
        if args.store is not None:
            directory = args.store.expanduser().resolve()
        else:
            from bettermemory.config import load_config

            directory = load_config().resolved_directory()
        memories, tombstones = load_store(directory)
        # The directory is deliberately NOT recorded: it is a private path.
        report = measure(memories, tombstones, "live store")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"corpus: {report['corpus']}  ({report['memories']} memories)")
    print(f"blocking pairs at >= {HIGH_SIMILARITY}: {report['blocking_pairs_at_high']}")
    print("\nsurgical (reported floor only, containment gate pinned):")
    print(f"  {'floor':>6}  {'pairs':>6}  {'mean/write':>10}  {'max':>5}  {'zero':>5}")
    for floor, row in report["surgical_floor_sweep"].items():
        print(
            f"  {floor:>6}  {row['related_pairs']:>6}  "
            f"{row['mean_per_write']:>10}  {row['max_per_write']:>5}  "
            f"{row['writes_with_zero']:>5}"
        )
    print("\nnaive (MEDIUM_SIMILARITY edited at the source):")
    for name, row in report["naive_constant_edit"].items():
        print(
            f"  {name}: {row['related_pairs']} pairs, "
            f"{row['mean_per_write']} per write (median {row['median_per_write']})"
        )
    admits = report["naive_admits_what"]
    if admits["raw_jaccard_quartiles"] is not None:
        q = admits["raw_jaccard_quartiles"]
        print(
            f"  the {admits['added_hits']} hits it adds have real Jaccard "
            f"{q['min']}/{q['median']}/{q['max']} (min/median/max); "
            f"{admits['added_hits_at_or_above_the_new_floor']} of them "
            f"actually reach 0.30"
        )
    band = report["target_band_already_surfaced"]
    print(
        f"\nhits with real Jaccard in [0.30, 0.40): "
        f"{band['hits_with_raw_jaccard_in_target_band']}, of which "
        f"{band['of_those_already_surfaced_as_related']} are already "
        f"breadcrumbed today."
    )
    dom = report["containment_dominance"]
    print(
        f"containment gate {dom['containment_gate']} makes any floor above "
        f"{dom['floor_below_which_the_knob_can_bite']} inert; "
        f"{dom['memories_below_containment_min_tokens']} memories are short "
        f"enough to escape it."
    )
    print(f"one breadcrumb costs {report['breadcrumb_wire_bytes']['median']} B median.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
