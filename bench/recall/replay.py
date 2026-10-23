"""The per-prompt recall replayed on the logged hit shapes (unit RS, D20).

Unit RS's M4 replayed decisions 4 and 6 over the Stop audit's logged hit
shapes (`turn_audited.top_hits`) for 2026-08-26 to 2026-09-25 and counted
189 deliveries. It was not hermetic: 57 turns took the caller's repository
from a live git remote, and eligibility came from a scratch store that the
brief's memory_show passes had appended to. This runner splits the replay
in two so the count can be read again by anyone holding the fixture:

  freeze   read a store (a v9 `memory.sqlite`, opened read-only) and write
           a fixture: the window's audited turns with their logged hits
           (id and relevance only), a frozen caller map (each turn's
           worktree to the repository the caller stood in, taken once,
           M4's way: the repository most memories written in that
           worktree carry, else the checkout's git remote, else none), a
           frozen project map (for each caller, which hit memories
           rc3's rule admits (`recall._rc3_in_project`), read once since it touches the
           filesystem), the hit memories' static facts (created, category,
           origin, provenance label, summary length) and the timelines the
           as-of reads need (contradicted uses, updates and verifies, the
           session's retrievals and writes, conflict rows). No body, no
           summary, no snippet and no prompt is written into it.
  run      replay the fixture under a rule and write counts and rates.

The rules, from M4's to the built one, each step changing one thing so a
count that moves can be traced to the step that moved it:

  m4         M4 as run: eligibility read at the window's end (present in
             the store, label local or imported, not ambient, no
             contradiction open then), the repository gate on the frozen caller
             (equal repositories, or for a repo-less caller equal worktree
             roots), and the session-touch gate: an id is held back once a
             show, write, update, verify or use (`id` or `ids`) under the
             turn's session came before the turn, or the replay already
             delivered it in that session.
  asof       m4 with eligibility read as of each turn: the memory existed
             (created before the turn) and no contradicted use was open at
             the turn (no update or verify of it between that use and the
             turn).
  project    asof with rc3's built candidate gate (`recall._rc3_in_project`) on
             the frozen caller (which also applies `worktrees_match`), and
             no ambient exclusion, which the built prompt recall does not
             make (the brief does).
  rs-v1      project with the built segment gate: the ids shown in the
             segment, plus `recall._seen_in_log` over the retrievals,
             writes and updates before the turn (the session's own since
             the idle window, the worktree's since the segment opened), the
             twelve per segment and the three per block; the block rendered by
             `recall.render_recall_block` with the summary at its length,
             the snippet at its 100-character cap and the as-of disputed
             mark (`recall.disputed_marks` over the pairs pending at the
             turn).

The old log carries no segment boundary (a compact or clear left nothing
then), so each session is one segment, opened at its first audited turn.
The block of the three earlier rules is M4's: header, one line per
candidate with the summary at its length and a 100-character snippet, the
footer, no cap.

    python bench/recall/replay.py freeze \\
        --store ~/.cache/bettermemory-v9/rs-slice5/sealed/store/memory.sqlite \\
        --since 2026-08-26 --until 2026-09-26 \\
        --out ~/.cache/bettermemory-v9/rs-slice5/replay-fixture-sealed.json
    python bench/recall/replay.py run \\
        --fixture ~/.cache/bettermemory-v9/rs-slice5/replay-fixture-sealed.json \\
        --out bench/recall/results/replay-sealed-2026-10-11.json

The fixture stays beside the store it came from and is never committed;
the result file holds counts, rates and the fixture's sha256 only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FIXTURE_KIND = "recall-replay-fixture"
FIXTURE_VERSION = 1
RESULT_KIND = "recall-replay"

RULES: tuple[str, ...] = ("m4", "asof", "project", "rs-v1")

# The kinds M4's session-touch gate read, and the fields it read ids from.
M4_TOUCH_KINDS: frozenset[str] = frozenset({"show", "write", "update", "verify", "use"})
# What the fixture keeps of a retrieval or write row: enough for either
# gate, nothing a person typed.
_EVENT_FIELDS: tuple[str, ...] = (
    "ts",
    "kind",
    "session",
    "session_id",
    "worktree_root",
    "harness_session",
    "id",
    "ids",
    "returned",
    "memory_ids",
    "hit_ids",
    "status",
)
_SEEN_EVENT_KINDS: frozenset[str] = frozenset(
    {"search", "show", "list", "list_active", "write", "update", "verify", "use"}
)
# M4's block: a 100-character stand-in snippet on every line.
_M4_SNIPPET = 100
# Two gates every rule replayed here had and rs-v6 removed from the product
# (RS-F7 part 0): the coverage labels a hit had to carry, and the
# deliveries a session segment allowed.
_RELEVANT: frozenset[str] = frozenset({"medium", "high"})
SEGMENT_DELIVERY_CAP = 12


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


def _connect(store: Path) -> sqlite3.Connection:
    """`store` opened read-only: the replay never writes the store it
    reads, and the URI refuses a write if one were tried."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _events(conn: sqlite3.Connection, until: str) -> list[dict[str, Any]]:
    """Every log row before `until`, its payload with the row's ts, kind and
    session put back, oldest first."""
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT ts, session, kind, payload FROM log WHERE ts < ? ORDER BY seq",
        (until,),
    ):
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        payload["ts"] = row["ts"]
        payload["kind"] = row["kind"]
        if row["session"] and not payload.get("session"):
            payload["session"] = row["session"]
        out.append(payload)
    return out


def _session_of(event: Mapping[str, Any]) -> str:
    return str(event.get("session") or event.get("session_id") or "")


def _ids_of(event: Mapping[str, Any], *fields: str) -> list[str]:
    for name in fields:
        value = event.get(name)
        if isinstance(value, str) and value:
            return [value]
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, str)]
    return []


def _git_remote(worktree: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def freeze(
    store: Path,
    *,
    since: str,
    until: str,
    git_remotes: bool = True,
) -> dict[str, Any]:
    """The fixture for the window [`since`, `until`) of `store`."""
    from bettermemory.models import Origin
    from bettermemory.recall import _rc3_in_project as in_project
    from bettermemory.recall import summarize

    conn = _connect(store)
    memories: dict[str, dict[str, Any]] = {}
    worktree_repos: dict[str, Counter[str]] = defaultdict(Counter)
    for row in conn.execute(
        "SELECT id, created, updated, category, origin_repo, origin_worktree, "
        "provenance, body FROM memories"
    ):
        memories[str(row["id"])] = {
            "created": row["created"],
            "category": row["category"],
            "repo": row["origin_repo"],
            "worktree_root": row["origin_worktree"],
            "provenance": row["provenance"],
            "summary_chars": len(summarize(str(row["body"]))),
        }
        if row["origin_worktree"] and row["origin_repo"]:
            worktree_repos[str(row["origin_worktree"])][str(row["origin_repo"])] += 1
    conflicts = [
        {
            "id": str(row["id"]),
            "a_id": str(row["a_id"]),
            "b_id": str(row["b_id"]),
            "created": row["created"],
            "status": row["status"],
            "verdict_ts": row["verdict_ts"],
        }
        for row in conn.execute(
            "SELECT id, a_id, b_id, created, status, verdict_ts FROM conflicts"
        )
    ]
    events = _events(conn, until)
    conn.close()

    turns: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "turn_audited" or not event.get("probe_query"):
            continue
        if not since <= str(event["ts"]) < until:
            continue
        hits = [
            [str(hit.get("id")), str(hit.get("relevance"))]
            for hit in (event.get("top_hits") or [])[:3]
            if isinstance(hit, dict) and hit.get("id")
        ]
        turns.append(
            {
                "ts": event["ts"],
                "session": _session_of(event),
                "worktree_root": event.get("worktree_root"),
                "verdict": event.get("verdict"),
                "hits": hits,
            }
        )
    turns.sort(key=lambda turn: str(turn["ts"]))

    callers: dict[str, dict[str, Any]] = {}
    for turn in turns:
        worktree = turn["worktree_root"]
        if not worktree or worktree in callers:
            continue
        if worktree_repos.get(worktree):
            repo, source = worktree_repos[worktree].most_common(1)[0][0], "memories"
        elif git_remotes:
            repo = _git_remote(worktree)
            source = "git_remote" if repo else "none"
        else:
            repo, source = None, "none"
        callers[worktree] = {"repo": repo, "source": source}

    hit_ids = sorted({hit[0] for turn in turns for hit in turn["hits"]})
    project: dict[str, list[str]] = {}
    for worktree, caller in callers.items():
        origin = Origin(cwd=worktree, repo=caller["repo"], worktree_root=worktree)
        project[worktree] = [
            memory_id
            for memory_id in hit_ids
            if memory_id in memories
            and in_project(
                _memory_origin(memories[memory_id]),
                origin,
            )
        ]

    sessions = {turn["session"] for turn in turns}
    worktrees = set(callers)
    contradicted: dict[str, list[str]] = defaultdict(list)
    answered: dict[str, list[str]] = defaultdict(list)
    seen_events: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("kind")
        if kind == "use" and event.get("outcome") == "contradicted":
            for memory_id in _ids_of(event, "ids", "memory_ids"):
                contradicted[memory_id].append(str(event["ts"]))
        elif kind in ("update", "verify"):
            for memory_id in _ids_of(event, "id", "memory_id"):
                answered[memory_id].append(str(event["ts"]))
        if kind in _SEEN_EVENT_KINDS and (
            _session_of(event) in sessions or event.get("worktree_root") in worktrees
        ):
            seen_events.append(
                {name: event[name] for name in _EVENT_FIELDS if name in event}
            )
    # M4's contradicted set: a contradicted use with no later update or
    # verify anywhere in the log up to the window's end (M4 read the whole
    # sealed log, which ends there).
    contradicted_at_freeze = sorted(
        memory_id
        for memory_id, stamps in contradicted.items()
        if max(answered.get(memory_id, [""])) < max(stamps)
    )
    return {
        "kind": FIXTURE_KIND,
        "version": FIXTURE_VERSION,
        "window": {"since": since, "until": until},
        "store_sha256": _sha256(store),
        "turns": turns,
        "callers": callers,
        "project": project,
        "memories": {
            memory_id: memories[memory_id]
            for memory_id in hit_ids
            if memory_id in memories
        },
        "contradicted": {k: sorted(v) for k, v in contradicted.items() if k in hit_ids},
        "answered": {k: sorted(v) for k, v in answered.items() if k in hit_ids},
        "contradicted_at_freeze": [m for m in contradicted_at_freeze if m in hit_ids],
        "conflicts": [
            row for row in conflicts if row["a_id"] in hit_ids or row["b_id"] in hit_ids
        ],
        "events": seen_events,
    }


def _memory_origin(memory: Mapping[str, Any]) -> Any:
    from bettermemory.models import Origin

    if memory.get("repo") is None and memory.get("worktree_root") is None:
        return None
    return Origin(repo=memory.get("repo"), worktree_root=memory.get("worktree_root"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _stamp(value: str) -> datetime:
    from bettermemory.time_utils import parse_event_ts

    parsed = parse_event_ts(value)
    if parsed is None:
        raise ValueError(f"unparseable timestamp in the fixture: {value!r}")
    return parsed


def _contradiction_open_at(memory_id: str, ts: str, fixture: Mapping[str, Any]) -> bool:
    """Whether a contradicted use of `memory_id` before `ts` had no update
    or verify after it and before `ts`: `recall._contradiction_open` read
    as of the turn."""
    uses = [stamp for stamp in fixture["contradicted"].get(memory_id, []) if stamp < ts]
    if not uses:
        return False
    last = max(uses)
    return not any(
        last < stamp < ts for stamp in fixture["answered"].get(memory_id, [])
    )


def _pending_pairs_at(
    ts: str, conflicts: Iterable[Mapping[str, Any]]
) -> list[tuple[str, str, str]]:
    """The conflict pairs pending at `ts`: created before it, and either
    still pending or decided after it."""
    pairs: list[tuple[str, str, str]] = []
    for row in conflicts:
        if str(row["created"]) >= ts:
            continue
        decided = row.get("verdict_ts")
        if row["status"] == "pending" or (decided and str(decided) > ts):
            pairs.append((str(row["a_id"]), str(row["b_id"]), str(row["id"])))
    return pairs


def _m4_block(lines: list[tuple[str, str, int]]) -> int:
    from bettermemory.recall import _RECALL_FOOTER, _RECALL_HEADER

    body = [
        f"- {memory_id} [{relevance}] {'x' * chars} " + "x" * _M4_SNIPPET
        for memory_id, relevance, chars in lines
    ]
    return len("\n".join([_RECALL_HEADER, *body, _RECALL_FOOTER]))


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def replay(fixture: Mapping[str, Any], rule: str) -> dict[str, Any]:
    """Counts and rates for `rule` over the fixture."""
    from bettermemory.recall import (
        _DELIVERED_PROVENANCE,
        _SEEN_KINDS,
        RECALL_MAX_POINTERS,
        STATE_IDLE_SECONDS,
        RecallPointer,
        _seen_in_log,
        disputed_marks,
        render_recall_block,
    )

    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}; one of {', '.join(RULES)}")
    step = RULES.index(rule)
    asof, built_project, built_segment = step >= 1, step >= 2, step >= 3
    memories: Mapping[str, Mapping[str, Any]] = fixture["memories"]
    callers: Mapping[str, Mapping[str, Any]] = fixture["callers"]
    project = {worktree: set(ids) for worktree, ids in fixture["project"].items()}
    contradicted_at_freeze = set(fixture["contradicted_at_freeze"])
    events: list[Mapping[str, Any]] = fixture["events"]
    touches: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for event in events:
        if event.get("kind") in M4_TOUCH_KINDS:
            for memory_id in _ids_of(event, "ids", "id"):
                touches[str(event.get("session") or "")].append(
                    (str(event["ts"]), memory_id)
                )
    started: dict[str, str] = {}
    for turn in fixture["turns"]:
        started.setdefault(turn["session"], turn["ts"])

    stats: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    shown: dict[str, set[str]] = defaultdict(set)
    deliveries_in: Counter[str] = Counter()
    candidates: list[int] = []
    chars: list[int] = []
    twice = 0
    for turn in fixture["turns"]:
        stats["turns"] += 1
        worktree = turn["worktree_root"]
        if not worktree:
            stats["outside_repository"] += 1
            continue
        stats["in_repository"] += 1
        if turn["verdict"] == "no_signal":
            stats["no_signal"] += 1
            continue
        ts, session = str(turn["ts"]), str(turn["session"])
        caller = callers[worktree]
        if built_segment:
            now = _stamp(ts)
            seen = set(shown[session]) | _seen_in_log(
                (
                    e
                    for e in events
                    if e.get("kind") in _SEEN_KINDS and str(e["ts"]) < ts
                ),
                since=_stamp(started[session]),
                own_since=now - timedelta(seconds=STATE_IDLE_SECONDS),
                sessions={session},
                worktree_root=worktree,
            )
        else:
            seen = set(shown[session]) | {
                memory_id for stamp, memory_id in touches.get(session, []) if stamp < ts
            }
        chosen: list[tuple[str, str]] = []
        held: Counter[str] = Counter()
        for memory_id, relevance in turn["hits"]:
            memory = memories.get(memory_id)
            reason: str | None = None
            if memory is None or (asof and str(memory["created"]) > ts):
                reason = "absent"
            elif relevance not in _RELEVANT:
                reason = "low_relevance"
            elif built_project and memory_id not in project.get(worktree, set()):
                reason = "other_project"
            elif not built_project and not _m4_same_project(memory, caller, worktree):
                reason = "other_project"
            elif memory["provenance"] not in _DELIVERED_PROVENANCE:
                reason = "provenance"
            elif not built_project and memory.get("category") == "ambient":
                reason = "ambient"
            elif (
                _contradiction_open_at(memory_id, ts, fixture)
                if asof
                else memory_id in contradicted_at_freeze
            ):
                reason = "contradicted"
            elif memory_id in seen:
                reason = "seen_in_segment"
            elif deliveries_in[session] >= SEGMENT_DELIVERY_CAP:
                reason = "segment_cap"
            elif len(chosen) >= RECALL_MAX_POINTERS:
                continue
            if reason is not None:
                held[reason] += 1
                continue
            chosen.append((memory_id, relevance))
        reasons.update(held)
        if not chosen:
            stats["segment_cap" if held["segment_cap"] else "no_candidate"] += 1
            continue
        if built_segment:
            marks = disputed_marks(_pending_pairs_at(ts, fixture["conflicts"]))
            pointers = [
                RecallPointer(
                    id=memory_id,
                    relevance=relevance,
                    summary="x" * int(memories[memory_id]["summary_chars"]),
                    snippet="y" * 200,
                    provenance=str(memories[memory_id]["provenance"]),
                    disputed=marks.get(memory_id),
                )
                for memory_id, relevance in chosen
            ]
            block, shown_ids = render_recall_block(pointers)
            if not shown_ids:
                stats["nothing_fits"] += 1
                continue
            stats["pointers_cut_by_cap"] += len(chosen) - len(shown_ids)
            stats["deliveries_with_mark"] += any(p.disputed for p in pointers)
            size = len(block)
        else:
            shown_ids = [memory_id for memory_id, _ in chosen]
            size = _m4_block(
                [(m, r, int(memories[m]["summary_chars"])) for m, r in chosen]
            )
        twice += sum(1 for memory_id in shown_ids if memory_id in shown[session])
        shown[session].update(shown_ids)
        deliveries_in[session] += 1
        stats["deliveries"] += 1
        candidates.append(len(shown_ids))
        chars.append(size)

    per_session = [count for count in deliveries_in.values() if count]
    in_repo = stats["in_repository"]
    return {
        "rule": rule,
        "counts": dict(sorted(stats.items())),
        "held_back": dict(sorted(reasons.items())),
        "sessions_with_turns": len(started),
        "delivering_sessions": len(per_session),
        "fire_rate_in_repository": round(stats["deliveries"] / in_repo, 3)
        if in_repo
        else None,
        "candidates_per_delivery": round(statistics.mean(candidates), 2)
        if candidates
        else None,
        "candidates_histogram": dict(sorted(Counter(candidates).items())),
        "deliveries_per_session_p50": statistics.median(per_session)
        if per_session
        else None,
        "deliveries_per_session_max": max(per_session) if per_session else 0,
        "block_chars_p50": _pct([float(c) for c in chars], 50),
        "block_chars_max": max(chars) if chars else 0,
        "ids_shown_twice_in_a_segment": twice,
    }


def _m4_same_project(
    memory: Mapping[str, Any], caller: Mapping[str, Any], worktree: str
) -> bool:
    """M4's repository gate: the memory's repository (its worktree root for
    a memory with no remote) equals the caller's; a repo-less caller
    matches a memory recorded in the same worktree root."""
    if caller["repo"] is not None:
        mine = memory.get("repo") or (
            f"wt:{memory['worktree_root']}" if memory.get("worktree_root") else None
        )
        return bool(mine == caller["repo"])
    return memory.get("worktree_root") == worktree


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("kind") != FIXTURE_KIND or fixture.get("version") != FIXTURE_VERSION:
        raise SystemExit(f"{path} is not a version-{FIXTURE_VERSION} replay fixture")
    return dict(fixture)


def write_json(out: Path, payload: Mapping[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    frz = sub.add_parser("freeze", help="write a fixture from a store")
    frz.add_argument("--store", type=Path, required=True, help="a v9 memory.sqlite")
    frz.add_argument("--since", required=True)
    frz.add_argument("--until", required=True)
    frz.add_argument("--out", type=Path, required=True)
    frz.add_argument(
        "--no-git",
        action="store_true",
        help="never ask git for a caller's remote (a caller no memory names reads as none)",
    )
    run = sub.add_parser("run", help="replay a fixture")
    run.add_argument("--fixture", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--rules", nargs="+", choices=RULES, default=list(RULES))
    run.add_argument("--note", default=None)
    args = parser.parse_args(argv)

    if args.command == "freeze":
        fixture = freeze(
            args.store.expanduser(),
            since=args.since,
            until=args.until,
            git_remotes=not args.no_git,
        )
        write_json(args.out.expanduser(), fixture)
        sources = Counter(c["source"] for c in fixture["callers"].values())
        print(
            f"froze {len(fixture['turns'])} turns, {len(fixture['callers'])} callers "
            f"({dict(sources)}), {len(fixture['memories'])} hit memories",
            file=sys.stderr,
        )
        return 0

    path = args.fixture.expanduser()
    fixture = load_fixture(path)
    from bettermemory import __version__

    result = {
        "kind": RESULT_KIND,
        "version": __version__,
        "commit": _head_commit(),
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fixture": {
            "name": path.name,
            "sha256": _sha256(path),
            "store_sha256": fixture["store_sha256"],
            "window": fixture["window"],
            "turns": len(fixture["turns"]),
            "callers": dict(Counter(c["source"] for c in fixture["callers"].values())),
        },
        "note": args.note,
        "rules": {rule: replay(fixture, rule) for rule in args.rules},
    }
    write_json(args.out, result)
    for rule, read in result["rules"].items():
        print(
            f"{rule}: {read['counts'].get('deliveries', 0)} deliveries, "
            f"{read['candidates_per_delivery']} each, block max {read['block_chars_max']}",
            file=sys.stderr,
        )
    return 0


def _head_commit() -> str | None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(Path(__file__).resolve().parents[2]),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
