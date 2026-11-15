"""Replay the store's logged reads through the current tree's handlers.

The store's log records every ``memory_search`` (its scopes, width,
``expand_top`` and ``auto_scope``, the caller's worktree, and the query
redacted to a hash, a 32-character preview and a length) and every
``memory_show`` (its id and the caller's worktree). The full query is
not in the log, so it is read from the harness transcripts: each
``memory_search`` call there is hashed as the log hashes it
(`events.redact_query`), and a logged search is replayed with the
transcript query of the same hash and the log's other arguments. A
logged search no transcript holds is skipped and counted. This program
takes a read-only snapshot of the store (`store_side.snapshot`), opens
the copy under a scratch key directory, and replays those calls in log
order through `handlers.search.memory_search` and
`handlers.show.memory_show`, each from the worktree the log names when
it still exists.

What it reports, aggregates only:

- characters per response, p50 and p90, for search and show, measured as
  the MCP server puts a result on the wire (each list item, or the one
  dict, as JSON with an indent of two);
- the flagged share of shows: the replayed ``staleness_verdict`` asks for
  a check (`store_side.FLAGGED`);
- the shows whose reason names a recent rejection recorded through
  memory_record_use;
- for the shown records whose check finds a missing path (any path the
  body cites or attests, and the claim-anchored subset apart) or commits
  since the stamp that touch what the record cites, how many still carry
  a flag.

The replay reads the store as it is now, not as it was at each call: a
record removed since is skipped and counted, and a record updated since
is replayed in its current form. The configuration is the defaults, with
the checkout token's write off, so the replay writes nothing into a
checkout. The copy and its key directory are deleted when the program
ends. No query, id, snippet or project name is printed or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pydantic_core

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "bench"))

import store_side  # noqa: E402
import transcripts  # noqa: E402

DEFAULT_SINCE = "2026-11-04"


def wire_chars(result: Any) -> int:
    """Characters the MCP server sends for a tool result: each item of a
    list, or the one value, as JSON indented by two."""
    items = result if isinstance(result, list) else [result]
    return sum(
        len(pydantic_core.to_json(item, fallback=str, indent=2).decode())
        for item in items
    )


def _quantile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


def _sizes(values: list[int]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": _quantile(values, 50),
        "p90": _quantile(values, 90),
    }


def _since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def transcript_queries(root: Path, since: datetime) -> dict[str, str]:
    """Every ``memory_search`` query the harness transcripts under `root`
    hold from `since` on, keyed by the hash the log redacts it to
    (`events.redact_query`)."""
    from bettermemory.events import redact_query

    floor = since.timestamp()
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < floor:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"tool_use"' not in line or "memory_search" not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if transcripts.tool_of(str(block.get("name") or "")) != "memory_search":
                    continue
                query = (block.get("input") or {}).get("query")
                if isinstance(query, str):
                    out[redact_query(query)["hash"]] = query
    return out


def _handlers(copy_dir: Path, keys_dir: Path) -> Any:
    import dataclasses

    from bettermemory._handlers import ToolHandlers
    from bettermemory._response import ResponseBuilder
    from bettermemory.config import Config, StorageConfig
    from bettermemory.events import Recorder
    from bettermemory.session import SessionState
    from scratch_store import scratch_store

    # The copy is opened with a scratch key directory, so its log is
    # rekeyed in the copy and nothing reads or writes the user's keys.
    store = scratch_store(copy_dir, keys_dir)
    config = Config(storage=StorageConfig(directory=str(copy_dir)))
    config = dataclasses.replace(
        config,
        membership=dataclasses.replace(config.membership, write_token=False),
    )
    state = SessionState()
    # No retrieval settles during the replay: an auto-committed `applied`
    # would land in the copy's log and clear a later show's rejection.
    setattr(state, "consume_old_tokens", lambda *args, **kwargs: [])
    return ToolHandlers(
        config=config,
        store=store,
        sessions=state,
        recorder=Recorder(store=store, session_id=state.session_id, enabled=True),
        responses=ResponseBuilder(
            stale_after_days=config.behavior.verification_stale_days
        ),
    )


def _evidence(deps: Any, record_id: str) -> tuple[bool, bool, bool]:
    """Whether the record's check finds a missing path (any cited), a
    missing claim-anchored path, and commits since the stamp."""
    from bettermemory.handlers.show import _show_verdict

    shown, _links = _show_verdict(deps, deps.store.load_one(record_id))
    drift = shown.path_drift
    commit = shown.commit_drift
    moved = commit is not None and commit.commits_since_verify > 0
    return bool(drift.missing), bool(drift.claim_anchored_missing), moved


class _At:
    """Run from the logged worktree when it still exists, else from a
    neutral directory."""

    def __init__(self, neutral: Path) -> None:
        self.neutral = neutral
        self.located = 0
        self.unlocated = 0

    def enter(self, worktree: Any) -> None:
        if isinstance(worktree, str) and os.path.isdir(worktree):
            os.chdir(worktree)
            self.located += 1
        else:
            os.chdir(self.neutral)
            self.unlocated += 1


async def replay(
    deps: Any, evs: list[dict[str, Any]], queries: dict[str, str], neutral: Path
) -> dict[str, Any]:
    from bettermemory.handlers.search import memory_search
    from bettermemory.handlers.show import memory_show

    at = _At(neutral)
    search_chars: list[int] = []
    hit_chars: list[int] = []
    show_chars: list[int] = []
    skipped: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    rejected = 0
    contradicted = 0
    provenance: Counter[str] = Counter()
    evidence: dict[str, Counter[str]] = {
        "path_missing_any": Counter(),
        "path_missing_claim_anchored": Counter(),
        "commits_moved": Counter(),
    }
    for ev in evs:
        kind = ev.get("kind")
        if kind == "search":
            logged = ev.get("query")
            digest = logged.get("hash") if isinstance(logged, dict) else None
            query = queries.get(str(digest))
            if query is None:
                skipped["search_query_not_in_transcripts"] += 1
                continue
            if ev.get("since_prior_session"):
                skipped["search_since_prior_session"] += 1
                continue
            at.enter(ev.get("worktree_root"))
            try:
                result = await memory_search(
                    deps,
                    query,
                    scopes=ev.get("scopes_filter") or None,
                    exclude_scopes=ev.get("exclude_scopes") or None,
                    max_results=ev.get("max_results"),
                    expand_top=bool(ev.get("expand_top")),
                    auto_scope=ev.get("auto_scope") is not False,
                    client=ev.get("client_filter"),
                    model=ev.get("model_filter"),
                )
            except Exception as exc:  # noqa: BLE001  a replay counts, never stops
                skipped[f"search_error_{type(exc).__name__}"] += 1
                continue
            search_chars.append(wire_chars(result))
            hit_chars.extend(wire_chars(hit) for hit in result)
        elif kind == "show":
            record_id = ev.get("id")
            if not isinstance(record_id, str):
                skipped["show_without_id"] += 1
                continue
            at.enter(ev.get("worktree_root"))
            try:
                result = await memory_show(deps, record_id)
            except Exception as exc:  # noqa: BLE001  removed since, mostly
                skipped[f"show_error_{type(exc).__name__}"] += 1
                continue
            show_chars.append(wire_chars(result))
            verdict = str(result.get("staleness_verdict"))
            verdicts[verdict] += 1
            reason = str(result.get("reason") or "")
            if "A use marked it" in reason:
                rejected += 1
            if "A recorded link says it contradicts" in reason:
                contradicted += 1
            if result.get("trust_unavailable"):
                provenance["trust_unavailable"] += 1
            else:
                provenance[str(result.get("provenance", "local"))] += 1
            flagged = verdict in store_side.FLAGGED
            for name, found in zip(evidence, _evidence(deps, record_id), strict=True):
                if found:
                    evidence[name]["flagged" if flagged else "not_flagged"] += 1
    shows = sum(verdicts.values())
    return {
        "search_chars": _sizes(search_chars),
        "search_hit_chars": _sizes(hit_chars),
        "show_chars": _sizes(show_chars),
        "shows": shows,
        "flagged_share": store_side._rate(
            sum(n for v, n in verdicts.items() if v in store_side.FLAGGED), shows
        ),
        "verdicts": dict(verdicts.most_common()),
        "shows_naming_a_recent_rejection": rejected,
        "shows_naming_a_contradiction": contradicted,
        "show_provenance": dict(provenance.most_common()),
        "drifted_shows": {name: dict(c) for name, c in evidence.items()},
        "skipped": dict(skipped.most_common()),
        "worktree": {"located": at.located, "unlocated": at.unlocated},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(os.path.expanduser("~/.bettermemory/memory.sqlite")),
        help="the store file to copy read-only",
    )
    parser.add_argument("--since", default=DEFAULT_SINCE, help="window start, UTC")
    parser.add_argument(
        "--transcripts",
        type=Path,
        default=transcripts.default_root(),
        help="the harness transcripts the full queries are read from",
    )
    parser.add_argument("--label", default="", help="a name for this run")
    parser.add_argument("--out", type=Path, help="where to write the aggregates")
    args = parser.parse_args(argv)
    if args.out is not None and args.out.resolve().is_relative_to(_ROOT):
        parser.error("--out must lie outside the repository")
    since = _since(args.since)
    started = datetime.now(timezone.utc)
    home = os.getcwd()

    with tempfile.TemporaryDirectory(prefix="bettermemory-replay-") as scratch:
        root = Path(scratch)
        copy_dir = root / "store"
        keys_dir = root / "keys"
        keys_dir.mkdir()
        neutral = root / "neutral"
        neutral.mkdir()
        os.environ["BETTERMEMORY_KEYS_DIR"] = str(keys_dir)
        copy = store_side.snapshot(args.store, copy_dir / "memory.sqlite")
        conn = store_side.open_copy(copy)
        try:
            evs = [
                ev
                for ev in store_side.events(conn)
                if ev.get("kind") in ("search", "show")
                and store_side._in(ev, since, None)
            ]
            uses_before = sum(
                1 for ev in store_side.events(conn) if ev.get("kind") == "use"
            )
        finally:
            conn.close()
        queries = transcript_queries(args.transcripts, since)
        deps = _handlers(copy_dir, keys_dir)
        try:
            body = asyncio.run(replay(deps, evs, queries, neutral))
            # Uses the replay itself wrote into the copy; zero keeps the
            # rejection count read from the log as it stood.
            body["uses_written_to_copy"] = (
                sum(
                    1
                    for ev in store_side.events(deps.store._conn)
                    if ev.get("kind") == "use"
                )
                - uses_before
            )
        finally:
            os.chdir(home)
            deps.store.close()

    out = {
        "kind": "response-replay",
        "label": args.label,
        "run_at": started.isoformat(),
        "window_since": args.since,
        "logged": dict(Counter(str(ev.get("kind")) for ev in evs)),
        **body,
    }
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    print(text, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
