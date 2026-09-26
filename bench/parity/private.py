"""Rank parity, the private half: the owner's own memory_search calls,
replayed over a scratch copy of the live store.

The public fixture (run.py) proves the port on a corpus anyone can rebuild.
This one proves it on the store that matters: the calls a real model made
against a real store over months, with the scopes, widths and repo filters
it actually passed. Everything here is private, so the OUTPUT NEVER ENTERS
THE REPO: it is written under ~/.cache/bettermemory-v9 by default, and the
harness refuses a path inside the checkout.

WHAT IS READ. Every `memory_search` tool call in the local Claude Code
transcripts (top-level session files and the nested subagent files under
each session directory), with the working directory the call was made
from. Nothing is written to the transcripts. The store is a COPY of the
live store's `memory.sqlite`, opened read-only (no rekey, so the copy is
never mutated either); the live store is never opened.

WHAT IS REPLAYED. The handler's own steps, with the two things a replay
must pin held fixed: the clock (PINNED_NOW, so recency and the verification
verdict cannot drift between runs) and the caller's origin (resolved once
per working directory with `origin.capture`, exactly what the handler's
auto-scope reads; a directory that no longer exists resolves to no repo,
which is what the handler would see too). Each call then goes through
`resolve_search_pool` and `search.search` with the same arguments
`memory_search` passes. `since_prior_session` calls are skipped, because
their pool is a slice of the event log by session boundary, not a ranking.

ONE ARM. `default` ranks with the shipped configuration. bettermemory 9
dropped the two usage flags (endorsement_boost, outcome_demotion) an
`owner` arm once measured against it; the cost of the drop on this store
is recorded in bench/parity/results/usage-labels-8.0.0-2026-09-25.json.

Usage:

    .venv/bin/python bench/parity/private.py --store ~/.cache/bettermemory-v9/live-copy
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory import origin as _origin  # noqa: E402
from bettermemory._handlers import (  # noqa: E402
    _INDEX_THRESHOLD_DEFAULT,
    _PREFILTER_CAP,
)
from bettermemory.config import BehaviorConfig  # noqa: E402
from bettermemory.handlers.search import (  # noqa: E402
    clamp_search_width,
    resolve_search_pool,
)
from bettermemory.models import validate_scope  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.search import tokenizer_fingerprint  # noqa: E402
from bettermemory.store import Store  # noqa: E402

TRANSCRIPTS = Path.home() / ".claude" / "projects"
CACHE = Path.home() / ".cache" / "bettermemory-v9"
DEFAULT_STORE = CACHE / "live-copy"
PINNED_NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)
ARMS = {"default": BehaviorConfig()}


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def extract_calls(root: Path = TRANSCRIPTS) -> list[dict[str, Any]]:
    """Every memory_search tool call, in file order, with its cwd."""
    calls: list[dict[str, Any]] = []
    for tp in sorted(glob.glob(str(root / "**" / "*.jsonl"), recursive=True)):
        try:
            fh = open(tp, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "assistant":
                    continue
                for block in (row.get("message") or {}).get("content") or []:
                    if not (
                        isinstance(block, dict) and block.get("type") == "tool_use"
                    ):
                        continue
                    if not str(block.get("name", "")).endswith("memory_search"):
                        continue
                    inp = block.get("input") or {}
                    query = inp.get("query")
                    if not isinstance(query, str) or not query.strip():
                        continue
                    calls.append(
                        {
                            "transcript": os.path.relpath(tp, root),
                            "ts": row.get("timestamp"),
                            "cwd": row.get("cwd"),
                            "query": query,
                            "scopes": inp.get("scopes"),
                            "auto_scope": inp.get("auto_scope", True),
                            "max_results": inp.get("max_results"),
                            "mode": inp.get("mode"),
                            "client": inp.get("client"),
                            "model": inp.get("model"),
                            "expand_top": bool(inp.get("expand_top", False)),
                            "since_prior_session": bool(
                                inp.get("since_prior_session", False)
                            ),
                        }
                    )
    return calls


_origin_cache: dict[str, dict[str, Any]] = {}


def resolve_origin(cwd: str | None) -> dict[str, Any]:
    """What `capture_origin()` would have read for this call, resolved
    from the directory the call was made in."""
    key = cwd or ""
    if key in _origin_cache:
        return _origin_cache[key]
    resolved: dict[str, Any] = {
        "repo": None,
        "worktree_root": None,
        "cwd_exists": False,
    }
    if cwd and Path(cwd).is_dir():
        resolved["cwd_exists"] = True
        try:
            o = _origin.capture(Path(cwd))
            resolved["repo"] = o.repo
            resolved["worktree_root"] = o.worktree_root
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            resolved["error"] = f"{type(exc).__name__}: {exc}"
    _origin_cache[key] = resolved
    return resolved


def _spec_key(call: dict[str, Any], origin: dict[str, Any]) -> str:
    material = [
        call["query"],
        sorted(call["scopes"]) if call["scopes"] else None,
        bool(call["auto_scope"]),
        call["max_results"],
        call["mode"],
        call["client"],
        call["model"],
        origin["repo"] if call["auto_scope"] else None,
        origin["worktree_root"] if call["auto_scope"] else None,
    ]
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]


def unique_specs(
    calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One replay spec per distinct (query, arguments, origin); the count
    of calls each stands for; and the skip tally."""
    specs: dict[str, dict[str, Any]] = {}
    skipped: dict[str, int] = collections.Counter()
    for call in calls:
        if call["since_prior_session"]:
            skipped["since_prior_session"] += 1
            continue
        if call["mode"] not in (None, "hybrid", "bm25", "keyword"):
            skipped["unknown_mode"] += 1
            continue
        scopes = call["scopes"]
        if scopes is not None:
            if not isinstance(scopes, list):
                skipped["scopes_not_a_list"] += 1
                continue
            try:
                scopes = [validate_scope(s) for s in scopes]
            except ValueError:
                skipped["invalid_scope"] += 1
                continue
        origin = resolve_origin(call["cwd"])
        key = _spec_key(call, origin)
        spec = specs.get(key)
        if spec is None:
            spec = {
                "key": key,
                "first_transcript": call["transcript"],
                "first_ts": call["ts"],
                "cwd": call["cwd"],
                "origin": origin,
                "query": call["query"],
                "scopes": scopes,
                "auto_scope": bool(call["auto_scope"]),
                "max_results": call["max_results"],
                "mode": call["mode"],
                "client": call["client"],
                "model": call["model"],
                "count": 0,
            }
            specs[key] = spec
        spec["count"] += 1
    return list(specs.values()), dict(skipped)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    store: Store,
    spec: dict[str, Any],
    behavior: BehaviorConfig,
) -> dict[str, Any]:
    """`memory_search`'s pool and rank steps, with the clock pinned."""
    width = clamp_search_width(
        spec["max_results"]
        if spec["max_results"] is not None
        else behavior.default_max_results
    )
    mode = spec["mode"] or behavior.search_mode or "hybrid"
    scopes = spec["scopes"]
    repo_filter = spec["origin"]["repo"] if spec["auto_scope"] else None
    worktree_filter = spec["origin"]["worktree_root"] if spec["auto_scope"] else None
    pool = resolve_search_pool(
        store,
        spec["query"],
        scopes=scopes,
        excluded_scopes=set(),
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        client_filter=spec["client"],
        model_filter=spec["model"],
        min_survivors=width,
    )
    hits = run_search(
        pool.memories,
        spec["query"],
        scopes=scopes,
        excluded_scopes=set(),
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        client_filter=spec["client"],
        model_filter=spec["model"],
        max_results=width,
        half_life_days=behavior.recency_boost_half_life_days,
        mode=mode,
        allow_empty_query=False,
        corpus_stats_provider=pool.corpus_stats_provider,
        rescue_expansion=False,
        conversational=behavior.conversational,
        now=PINNED_NOW,
    )
    return {
        "ids": [h.id for h in hits],
        "scores": [round(float(h.score), 6) for h in hits],
        "relevance": [str(h.relevance) for h in hits],
        "engaged": pool.corpus_stats_provider is not None,
        "pool_size": len(pool.memories),
        "width": width,
    }


def digest(rows: list[dict[str, Any]], arm: str) -> str:
    material = [[r["key"], r[arm]["ids"], r[arm]["relevance"]] for r in rows]
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


def run_private(store_root: Path, transcripts: Path = TRANSCRIPTS) -> dict[str, Any]:
    try:
        store = Store.open(store_root, allow_rekey=False)
    except FileNotFoundError:
        raise SystemExit(
            f"no store at {store_root}; copy memory.sqlite there"
        ) from None
    status = store.status()
    calls = extract_calls(transcripts)
    specs, skipped = unique_specs(calls)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        row = dict(spec)
        for arm, behavior in ARMS.items():
            row[arm] = replay(store, spec, behavior)
        rows.append(row)
    return {
        "kind": "rank-parity/private",
        "provenance": _provenance(),
        "engine": {
            "tokenizer_fingerprint": tokenizer_fingerprint(),
            "schema_version": status["schema_version"],
            "prefilter_cap": _PREFILTER_CAP,
            "index_threshold": _INDEX_THRESHOLD_DEFAULT,
        },
        "store": {
            "root": str(store_root),
            "path": str(store.path),
            "active": store.count_memories(),
        },
        "params": {
            "pinned_now": PINNED_NOW.isoformat(),
            "arms": {
                arm: {
                    "half_life_days": b.recency_boost_half_life_days,
                    "search_mode": b.search_mode,
                    "rescue_expansion": False,
                    "conversational": b.conversational,
                }
                for arm, b in ARMS.items()
            },
        },
        "calls": len(calls),
        "transcripts": len({c["transcript"] for c in calls}),
        "specs": len(specs),
        "skipped": skipped,
        "digests": {arm: digest(rows, arm) for arm in ARMS},
        "rows": rows,
    }


def summarize(artifact: dict[str, Any]) -> str:
    rows = artifact["rows"]
    engaged = sum(1 for r in rows if r["default"]["engaged"])
    empty = sum(1 for r in rows if not r["default"]["ids"])
    return (
        f"{artifact['calls']} calls in {artifact['transcripts']} transcripts -> "
        f"{artifact['specs']} unique specs (skipped {artifact['skipped']}); "
        f"prefilter engaged {engaged}/{len(rows)}; empty result {empty}; "
        f"digest default {artifact['digests']['default'][:16]}"
    )


def compare(
    a: dict[str, Any], b: dict[str, Any], arm: str = "default"
) -> list[dict[str, Any]]:
    index_b = {r["key"]: r for r in b["rows"]}
    diffs: list[dict[str, Any]] = []
    for ra in a["rows"]:
        rb = index_b.pop(ra["key"], None)
        if rb is None:
            diffs.append({"key": ra["key"], "field": "missing_in_b"})
            continue
        for field in ("ids", "relevance", "engaged"):
            if ra[arm][field] != rb[arm][field]:
                diffs.append(
                    {
                        "key": ra["key"],
                        "field": field,
                        "a": ra[arm][field],
                        "b": rb[arm][field],
                    }
                )
    for key in index_b:
        diffs.append({"key": key, "field": "missing_in_a"})
    return diffs


def _refuse_inside_repo(path: Path) -> None:
    try:
        path.resolve().relative_to(_ROOT)
    except ValueError:
        return
    raise SystemExit(f"refusing to write private data inside the checkout: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--transcripts", type=Path, default=TRANSCRIPTS)
    parser.add_argument(
        "--out", type=Path, default=CACHE / "private-fixture-8.0.0.json"
    )
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None)
    parser.add_argument("--arm", default="default", choices=sorted(ARMS))
    args = parser.parse_args()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        diffs = compare(a, b, args.arm)
        print(f"A: {summarize(a)}")
        print(f"B: {summarize(b)}")
        for d in diffs:
            print(
                json.dumps(
                    {k: v for k, v in d.items() if k != "query"}, ensure_ascii=False
                )
            )
        print(f"{len(diffs)} differing (query, field) entries in arm {args.arm}")
        return 0 if not diffs else 1

    _refuse_inside_repo(args.out)
    live = (Path.home() / ".claude-memory").resolve()
    if args.store.resolve() == live:
        raise SystemExit("refusing to replay against the live store; pass a copy")
    artifact = run_private(args.store, args.transcripts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(artifact, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}")
    print(summarize(artifact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
