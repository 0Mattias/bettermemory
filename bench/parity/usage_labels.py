"""The two usage-signal ranking flags, graded against the owner's own usage
labels.

`endorsement_boost` and `outcome_demotion` are bounded multipliers fed by
`memory_record_use` outcomes. Whether they help is a question about what
the model went on to use, so the labels come from the transcripts: after a
`memory_search`, a `memory_show` of a served hit, or an explicit
`memory_record_use(applied)` carrying a claim excerpt, marks that hit as a
positive; an `ignored` or `contradicted` outcome marks it as a negative.
Each labelled search is then replayed on the scratch store copy under four
arms (shipped defaults, each flag alone, both flags) with the clock pinned,
through the same pool and rank steps `memory_search` runs, and every arm
is scored on where it ranks the positives and the negatives.

The labels were produced while the owner's own flags were on, so position
bias favours the flag arms: the model opened what it was shown. A flag arm
that still loses on positives loses against a tilt in its favour.

Aggregates only leave this harness. The transcripts, the store copy and
the per-search rows never do.

Usage:

    .venv/bin/python bench/parity/usage_labels.py --out bench/parity/results/usage-labels-8.0.0-2026-09-25.json
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import glob
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory.config import BehaviorConfig  # noqa: E402
from bettermemory.models import validate_scope  # noqa: E402
from bettermemory.store import Store  # noqa: E402

TRANSCRIPTS = Path.home() / ".claude" / "projects"
DEFAULT_STORE = Path.home() / ".cache" / "bettermemory-v9" / "live-copy"
ARMS: dict[str, BehaviorConfig] = {
    "default": BehaviorConfig(),
    "endorsement": dataclasses.replace(BehaviorConfig(), endorsement_boost=True),
    "demotion": dataclasses.replace(BehaviorConfig(), outcome_demotion=True),
    "both": dataclasses.replace(
        BehaviorConfig(), endorsement_boost=True, outcome_demotion=True
    ),
}
MISSING_RANK = 99


def _private() -> ModuleType:
    """The private fixture harness, for its origin resolution and replay."""
    spec = importlib.util.spec_from_file_location(
        "bench_parity_private_for_labels", _HERE / "private.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tool_result_text(block: dict[str, Any]) -> str:
    body = block.get("content")
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "".join(x.get("text", "") for x in body if isinstance(x, dict))
    return ""


def labelled_searches(root: Path = TRANSCRIPTS) -> list[dict[str, Any]]:
    """Every memory_search with a served result, with the ids the model
    opened, explicitly applied with an excerpt, or marked negative."""
    out: list[dict[str, Any]] = []
    for tp in sorted(glob.glob(str(root / "**" / "*.jsonl"), recursive=True)):
        try:
            fh = open(tp, encoding="utf-8", errors="replace")
        except OSError:
            continue
        pending: dict[str, dict[str, Any]] = {}
        searches: list[dict[str, Any]] = []
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get("type")
                msg = row.get("message") or {}
                content = msg.get("content")
                if kind == "user" and isinstance(content, list):
                    for block in content:
                        if not (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            continue
                        search = pending.pop(block.get("tool_use_id"), None)
                        if search is None:
                            continue
                        try:
                            parsed = json.loads(_tool_result_text(block))
                        except ValueError:
                            parsed = None
                        served: list[str] = []
                        if isinstance(parsed, dict):
                            served = [
                                h["id"]
                                for h in parsed.get("result") or []
                                if isinstance(h, dict) and h.get("id")
                            ]
                        search["served"] = served
                        searches.append(search)
                elif kind == "assistant":
                    for block in content or []:
                        if not (
                            isinstance(block, dict) and block.get("type") == "tool_use"
                        ):
                            continue
                        name = str(block.get("name", ""))
                        inp = block.get("input") or {}
                        if name.endswith("memory_search") and isinstance(
                            inp.get("query"), str
                        ):
                            pending[block.get("id")] = {
                                "cwd": row.get("cwd"),
                                "query": inp["query"],
                                "scopes": inp.get("scopes"),
                                "auto_scope": inp.get("auto_scope", True),
                                "max_results": inp.get("max_results"),
                                "mode": inp.get("mode"),
                                "client": inp.get("client"),
                                "model": inp.get("model"),
                                "served": [],
                                "opened": set(),
                                "excerpt": set(),
                                "outcomes": {},
                            }
                        elif name.endswith("memory_show"):
                            sid = inp.get("id")
                            for s in reversed(searches):
                                if sid in s["served"]:
                                    s["opened"].add(sid)
                                    break
                        elif name.endswith("memory_record_use"):
                            outcome = inp.get("outcome")
                            excerpts = inp.get("claim_excerpts") or []
                            for j, mid in enumerate(inp.get("memory_ids") or []):
                                for s in reversed(searches):
                                    if mid in s["served"]:
                                        s["outcomes"][mid] = outcome
                                        if j < len(excerpts) and excerpts[j]:
                                            s["excerpt"].add(mid)
                                        break
        out.extend(s for s in searches if s["served"])
    return out


def grade(store_root: Path, searches: list[dict[str, Any]]) -> dict[str, Any]:
    priv = _private()
    store = Store(store_root)
    active = {m.id for m in store.load_all()}
    events = {arm: priv._events_for(store.root, b) for arm, b in ARMS.items()}
    stats: dict[str, collections.Counter[str]] = {
        arm: collections.Counter() for arm in ARMS
    }
    pairwise: collections.Counter[str] = collections.Counter()
    kinds: collections.Counter[str] = collections.Counter()
    n_labelled = n_pos = n_neg = 0
    for s in searches:
        negatives = {
            m for m, o in s["outcomes"].items() if o in ("ignored", "contradicted")
        }
        positives = (s["opened"] | s["excerpt"]) - negatives
        positives = {p for p in positives if p in active}
        negatives = {n for n in negatives if n in active}
        if not positives and not negatives:
            continue
        scopes = s["scopes"]
        if scopes is not None:
            try:
                scopes = [validate_scope(x) for x in scopes]
            except (TypeError, ValueError):
                continue
        spec = {
            "query": s["query"],
            "scopes": scopes,
            "auto_scope": bool(s["auto_scope"]),
            "max_results": s["max_results"],
            "mode": s["mode"],
            "client": s["client"],
            "model": s["model"],
            "origin": priv.resolve_origin(s["cwd"]),
        }
        ranked = {
            arm: priv.replay(store, spec, b, events[arm])["ids"]
            for arm, b in ARMS.items()
        }
        n_labelled += 1
        n_pos += len(positives)
        n_neg += len(negatives)
        for p in positives:
            kinds["excerpt" if p in s["excerpt"] else "opened"] += 1
        for arm, ids in ranked.items():
            c = stats[arm]
            for p in positives:
                if p in ids:
                    rank = ids.index(p) + 1
                    c["pos_found"] += 1
                    c["pos_top1"] += rank == 1
                    c["pos_top3"] += rank <= 3
                else:
                    c["pos_missing"] += 1
            for n in negatives:
                if n in ids:
                    rank = ids.index(n) + 1
                    c["neg_found"] += 1
                    c["neg_top1"] += rank == 1
                else:
                    c["neg_missing"] += 1

        def _rank(arm: str, mid: str) -> int:
            ids = ranked[arm]
            return ids.index(mid) + 1 if mid in ids else MISSING_RANK

        for p in positives:
            for arm in ARMS:
                r = _rank(arm, p)
                if r != MISSING_RANK:
                    stats[arm]["pos_rr_x1000"] += round(1000 / r)
            d, o = _rank("default", p), _rank("both", p)
            pairwise[
                "positives_both_higher"
                if o < d
                else "positives_default_higher"
                if d < o
                else "positives_tie"
            ] += 1
        for n in negatives:
            d, o = _rank("default", n), _rank("both", n)
            pairwise[
                "negatives_both_lower"
                if o > d
                else "negatives_default_lower"
                if d > o
                else "negatives_tie"
            ] += 1
    arms_out: dict[str, Any] = {}
    for arm, c in stats.items():
        found = c["pos_found"] or 1
        arms_out[arm] = {
            "endorsement_boost": ARMS[arm].endorsement_boost,
            "outcome_demotion": ARMS[arm].outcome_demotion,
            "positives_found": c["pos_found"],
            "positives_missing": c["pos_missing"],
            "positives_at_top1": c["pos_top1"],
            "positives_in_top3": c["pos_top3"],
            "positives_mrr": round(c["pos_rr_x1000"] / 1000 / found, 4),
            "negatives_found": c["neg_found"],
            "negatives_missing": c["neg_missing"],
            "negatives_at_top1": c["neg_top1"],
        }
    return {
        "kind": "rank-parity/usage-labels",
        "provenance": _private()._provenance(),
        "store": {"root": str(store_root), "active": len(active)},
        "pinned_now": _private().PINNED_NOW.isoformat(),
        "label_rule": (
            "positive: a served hit the model then opened with memory_show or applied "
            "explicitly with a claim excerpt; negative: a served hit marked ignored or "
            "contradicted; a hit that is both counts as negative; only active memories count"
        ),
        "searches_with_a_served_result": len(searches),
        "labelled_searches": n_labelled,
        "positives": n_pos,
        "positives_by_kind": dict(kinds),
        "negatives": n_neg,
        "arms": arms_out,
        "pairwise_both_vs_default": dict(pairwise),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--transcripts", type=Path, default=TRANSCRIPTS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.store.resolve() == (Path.home() / ".claude-memory").resolve():
        raise SystemExit("refusing to replay against the live store; pass a copy")
    artifact = grade(args.store, labelled_searches(args.transcripts))
    text = json.dumps(artifact, indent=1) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    arms = artifact["arms"]
    print(
        f"{artifact['labelled_searches']} labelled searches, {artifact['positives']} positives, "
        f"{artifact['negatives']} negatives; positives at top 1: "
        + ", ".join(f"{a} {v['positives_at_top1']}" for a, v in arms.items())
        + f"; pairwise {artifact['pairwise_both_vs_default']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
