"""Building the scale bench's stores (ENG-109).

A store at scale k is built in two passes, both through the product's
own write paths, so its log is signed, hash-chained and verifies:

1. Template sessions through the public API (`api_session`): a session
   start (`recall.session_brief`), turns of the prompt hook
   (`recall.prompt_recall`) and the Stop audit (`hook.audit_transcript`),
   and the tools (`build_server(...).call_tool`: memory_search,
   memory_show, memory_record_use, memory_write answered through its
   review, memory_update, memory_verify, memory_remove, episode) in the
   session's checkout. The rows each session appends are kept as its
   template. `calibrate` runs them on a template store whose records are
   back-dated (written over the span before the sessions), so the hooks
   deliver as on a store in use, and times them: the public API's
   generation rate.

2. The scale store (`Replay`): the templates replayed in time order over
   the store's span, through the store's own write and sign code in
   batched transactions (`Store.batch`): each template's record writes
   become new records in the replayed session's checkout
   (`put_memory` with the record's home and standing answer, `tombstone`,
   `mark_verified`, `put_episode`), and its other rows are appended as
   they were, their ids, session ids, paths and checkout and project ids
   mapped to the replayed session's (`Store._append`). Checkouts are real
   repositories under the scratch directory, each resolved at its first
   sight through `location.resolve_caller`. The rows are stamped by the
   synthetic clock (`world.stamping`), in time order, so the log's
   stamps rise with its sequence as a store's do.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import heapq
import json
import math
import os
import random
import re
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import world

ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: Rows appended per transaction while replaying.
BATCH_ROWS = 4_000

#: The span of the 1x store's activity, and the most the span grows: a
#: store at 10x is a year of the 1x store's use (ten times the span, the
#: same density); at 100x, a team of ten for that year (the same span,
#: ten times the density).
SPAN_DAYS_1X = 42.0
SPAN_GROWS_TO = 10.0

#: Projects at 1x; a store at k has about 12 * sqrt(k) (a team shares
#: many projects, and a year adds some).
PROJECTS_1X = 12


# ---------------------------------------------------------------------------
# Checkouts
# ---------------------------------------------------------------------------


@dataclass
class Checkout:
    """One place sessions stand in, and what its first sight resolved."""

    index: int
    path: Path
    project: int
    name: str
    words: list[str]
    weight: float
    remote: str | None
    kind: str
    first_sight: datetime | None = None
    home: Any = None
    checkout_id: str | None = None
    project_id: str | None = None

    def origin(self) -> dict[str, Any]:
        if self.kind == "folder":
            return {"cwd": str(self.path)}
        return {
            "cwd": str(self.path),
            "repo": self.remote,
            "branch": "main",
            "worktree_root": str(self.path),
        }


def projects_at(scale: float) -> int:
    return max(4, round(PROJECTS_1X * math.sqrt(scale)))


def topology(root: Path, scale: float, rng: random.Random) -> list[Checkout]:
    """The repositories and folders of a store at `scale`: projects with
    a remote, some with a second worktree or a second clone, some
    commit-less, some local, and two plain folders; each with a weight
    (Zipf over the projects) for how often a session stands there."""
    count = projects_at(scale)
    ranks = list(range(count))
    rng.shuffle(ranks)
    out: list[Checkout] = []
    for i in range(count):
        words = world.project_words(i, rng)
        name = f"{words[0]}{i // 40 or ''}"
        kind = rng.choices(
            ["remote", "worktree", "clone", "commitless", "local"],
            weights=[60, 15, 8, 10, 7],
        )[0]
        weight = 1.0 / (ranks[i] + 1) ** 0.9
        base = root / f"p{i:03d}" / name
        remote = (
            None
            if kind in ("commitless", "local")
            else (f"git@example.invalid:team/{name}.git")
        )
        path = world.make_repo(base, remote=remote, commit=kind != "commitless")
        out.append(Checkout(len(out), path, i, name, words, weight, remote, kind))
        if kind == "worktree":
            other = world.add_worktree(path, base.parent / f"{name}-wt", "feature")
            out[-1].weight *= 0.7
            out.append(
                Checkout(len(out), other, i, name, words, weight * 0.3, remote, kind)
            )
        elif kind == "clone":
            other = world.clone(path, root / "clones" / f"p{i:03d}" / name)
            out[-1].weight *= 0.6
            out.append(
                Checkout(len(out), other, i, name, words, weight * 0.4, remote, kind)
            )
    for j, name in enumerate(("notes", "downloads")):
        folder = root / "folders" / name
        folder.mkdir(parents=True, exist_ok=True)
        words = world.project_words(count + j, rng)
        out.append(
            Checkout(
                len(out),
                folder.resolve(),
                -1 - j,
                "global",
                words,
                0.15,
                None,
                "folder",
            )
        )
    return out


def sight(store: Any, config: Any, checkout: Checkout) -> None:
    """Resolve `checkout` at its first sight, through the product's own
    resolution, and keep the home a record written there gets."""
    from bettermemory.location import resolve_caller

    caller = resolve_caller(store, config, str(checkout.path))
    home = caller.home()
    checkout.home = home
    checkout.project_id = home.project
    checkout.checkout_id = home.checkout


def config_for(store_dir: Path) -> Any:
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        StorageConfig,
        TelemetryConfig,
    )

    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        telemetry=TelemetryConfig(enabled=True),
        behavior=BehaviorConfig(),
    )


def open_store(store_dir: Path) -> Any:
    from scratch_store import scratch_store

    return scratch_store(
        store_dir / "memory.sqlite", Path(os.environ["BETTERMEMORY_KEYS_DIR"])
    )


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


class Transcript:
    """A Claude Code transcript a session appends its turns to, in the
    harness's row shape, so the hooks read what they read in use."""

    def __init__(self, path: Path, session: str, cwd: Path) -> None:
        self.path = path
        self.session = session
        self.cwd = cwd
        self.parent: str | None = None
        self.turn = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def _row(self, kind: str, message: dict[str, Any], **extra: Any) -> None:
        row_id = str(uuid.uuid4())
        doc = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": str(self.cwd),
            "sessionId": self.session,
            "version": "2.1.295",
            "gitBranch": "main",
            "type": kind,
            "message": message,
            "uuid": row_id,
            "timestamp": world.iso(datetime.now(timezone.utc)),
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc) + "\n")
        self.parent = row_id

    def user(self, text: str, prompt_id: str) -> None:
        self._row("user", {"role": "user", "content": text}, promptId=prompt_id)

    def assistant(self, text: str) -> None:
        self._row(
            "assistant",
            {
                "id": f"msg_{uuid.uuid4().hex[:20]}",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            },
        )
        self.turn += 1


# ---------------------------------------------------------------------------
# One session through the public API
# ---------------------------------------------------------------------------


@dataclass
class SessionPlan:
    """How often a turn calls each tool: tuned so the replayed 1x store
    has about the live store's records per log row."""

    mean_turns: float = 7.0
    search: float = 0.30
    show: float = 0.35
    use: float = 0.30
    write: float = 0.22
    update: float = 0.03
    verify: float = 0.04
    remove: float = 0.035
    handoff: float = 0.15
    episode: float = 0.18


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    try:
        result = await server.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - a refusal is a status, not a failure
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"[:200]}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    content = getattr(result, "content", None)
    if content and hasattr(content[0], "text"):
        try:
            return json.loads(content[0].text)
        except ValueError:
            return {"error": content[0].text}
    return None


async def reviewed_write(
    server: Any, arguments: dict[str, Any], tool: str = "memory_write"
) -> tuple[str, int]:
    """memory_write (or memory_update), its review answered as the daemon
    latency bench answers it (every related id unrelated, unsafe and
    secret 0, no roster record named) with a standing drawn from 0 to 1:
    returns the final status and the calls made."""
    response = await _call(server, tool, arguments)
    calls = 1
    while (
        isinstance(response, dict) and response.get("status") == "review" and calls < 4
    ):
        review = response.get("review") or {}
        related = review.get("related") or []
        ids = [r["id"] if isinstance(r, dict) else r for r in related]
        answer: dict[str, Any] = {
            "relation": {str(i): "unrelated" for i in ids},
            "named": [],
        }
        if tool == "memory_write":
            answer.update(unsafe=0, secret=0, standing=round(random.random(), 2))
        if review.get("review_id"):
            answer["review_id"] = review["review_id"]
        if review:
            arguments = {**arguments, "review": answer}
        if tool == "memory_update" and "standing" in (response.get("questions") or {}):
            # An update asks its standing as an argument of its own.
            arguments = {**arguments, "standing": round(random.random(), 2)}
        response = await _call(server, tool, arguments)
        calls += 1
    status = response.get("status") if isinstance(response, dict) else None
    return str(status), calls


def _project_ids(store: Any, checkout: Checkout, limit: int = 50) -> list[str]:
    if checkout.project_id is None:
        return []
    rows = store.conn.execute(
        "SELECT h.record_id FROM homes h JOIN memories m ON m.id = h.record_id "
        "WHERE h.project = ? ORDER BY m.updated DESC LIMIT ?",
        (checkout.project_id, limit),
    ).fetchall()
    return [str(r[0]) for r in rows]


@dataclass
class World:
    """What every public-API session shares: the store, its config, the
    daemon's recall state, and the transcripts' directory."""

    store: Any
    config: Any
    recall_state: Any
    transcripts: Path
    timings: dict[str, list[float]] = field(default_factory=dict)

    def timed(self, name: str, seconds: float) -> None:
        self.timings.setdefault(name, []).append(seconds)


async def api_session(
    w: World,
    checkout: Checkout,
    session: str,
    rng: random.Random,
    plan: SessionPlan,
    marks: list[tuple[int, int]],
) -> dict[str, Any]:
    """One session in `checkout` through the public API. `marks` gets the
    log's last seq at each turn's start, (turn, seq), for the template's
    turn boundaries."""
    from bettermemory import hook, recall
    from bettermemory.builder import build_server
    from bettermemory.session import SessionState

    os.environ["CLAUDE_CODE_SESSION_ID"] = session
    os.chdir(checkout.path)
    server = build_server(config=w.config, store=w.store, state=SessionState())
    transcript = Transcript(w.transcripts / f"{session}.jsonl", session, checkout.path)
    statuses: Counter[str] = Counter()
    started = time.perf_counter()
    t0 = time.perf_counter()
    recall.session_brief(
        session_id=session,
        source="startup",
        harness="claude-code",
        transcript_path=str(transcript.path),
        cwd=checkout.path,
        config=w.config,
        store=w.store,
        state=w.recall_state,
    )
    w.timed("session_start", time.perf_counter() - t0)
    if rng.random() < plan.handoff:
        await _call(server, "episode", {"action": "handoff"})
        statuses["handoff"] += 1
    turns = max(1, min(30, round(rng.expovariate(1.0 / plan.mean_turns))))
    known = _project_ids(w.store, checkout)
    for turn in range(turns):
        marks.append((turn, _last_seq(w.store)))
        text = world.prompt(rng, checkout.words)
        prompt_id = f"p{turn}"
        transcript.user(text, prompt_id)
        t0 = time.perf_counter()
        recall.prompt_recall(
            prompt=text,
            session_id=session,
            harness="claude-code",
            prompt_id=prompt_id,
            transcript_path=str(transcript.path),
            cwd=checkout.path,
            config=w.config,
            store=w.store,
            state=w.recall_state,
        )
        w.timed("prompt", time.perf_counter() - t0)
        await _tools(w, server, checkout, rng, plan, known, statuses)
        answer = world.sentence(rng, checkout.words, 14)
        transcript.assistant(answer)
        t0 = time.perf_counter()
        hook.audit_transcript(
            transcript_path=transcript.path,
            session_id=session,
            config=w.config,
            store=w.store,
            cwd=checkout.path,
            assistant_message=answer,
            harness="claude-code",
            prompt_id=prompt_id,
            state=w.recall_state,
        )
        w.timed("stop", time.perf_counter() - t0)
    marks.append((turns, _last_seq(w.store)))
    if rng.random() < plan.episode:
        await _call(
            server,
            "episode",
            {
                "action": "write",
                "body": world.body(rng, checkout.words),
                "takeaway": world.sentence(rng, checkout.words, 10),
            },
        )
        statuses["episode"] += 1
    return {
        "turns": turns,
        "seconds": time.perf_counter() - started,
        "statuses": dict(statuses),
    }


async def _tools(
    w: World,
    server: Any,
    checkout: Checkout,
    rng: random.Random,
    plan: SessionPlan,
    known: list[str],
    statuses: Counter[str],
) -> None:
    """The tool calls of one turn."""
    if rng.random() < plan.search:
        t0 = time.perf_counter()
        hits = await _call(
            server, "memory_search", {"query": world.query(rng, checkout.words)}
        )
        w.timed("memory_search", time.perf_counter() - t0)
        ids = (
            [h["id"] for h in hits if isinstance(h, dict) and "id" in h]
            if isinstance(hits, list)
            else []
        )
        known.extend(i for i in ids if i not in known)
        if ids and rng.random() < plan.show:
            await _call(server, "memory_show", {"id": ids[0]})
        if ids and rng.random() < plan.use:
            outcome = rng.choice(["applied", "applied", "ignored"])
            await _call(
                server, "memory_record_use", {"memory_ids": ids[:2], "outcome": outcome}
            )
    if rng.random() < plan.write:
        t0 = time.perf_counter()
        status, _ = await reviewed_write(
            server,
            {
                "content": world.body(rng, checkout.words),
                "scopes": [f"projects:{checkout.name}"]
                if checkout.kind != "folder"
                else [rng.choice(["tools", "preferences", "infrastructure"])],
                "category": rng.choice(
                    ["fact", "fact", "fact", "user-inference", "ambient"]
                ),
            },
        )
        w.timed("memory_write", time.perf_counter() - t0)
        statuses[f"write:{status}"] += 1
    if known and rng.random() < plan.update:
        target = rng.choice(known)
        status, _ = await reviewed_write(
            server,
            {"id": target, "content": world.body(rng, checkout.words)},
            tool="memory_update",
        )
        statuses[f"update:{status}"] += 1
    if known and rng.random() < plan.verify:
        result = await _call(
            server, "memory_verify", {"id": rng.choice(known), "note": "still holds"}
        )
        statuses[f"verify:{_status(result)}"] += 1
    if known and rng.random() < plan.remove:
        target = known.pop(rng.randrange(len(known)))
        result = await _call(
            server,
            "memory_remove",
            {"id": target, "reason": "superseded by a later fact"},
        )
        statuses[f"remove:{_status(result)}"] += 1


def _status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("status", "error" if "error" in result else "ok"))
    return "none"


def _last_seq(store: Any) -> int:
    row = store.conn.execute("SELECT MAX(seq) FROM log").fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def capture_template(
    store: Any,
    checkout: Checkout,
    session: str,
    first_seq: int,
    marks: list[tuple[int, int]],
    existed: set[str],
) -> dict[str, Any]:
    """The rows one session appended after `first_seq`, each with the
    turn it fell in (-1 the session's start, the turn count its end) and,
    for a record put, whether the record existed before the session."""
    bounds = sorted(marks, key=lambda m: m[1])
    rows = []
    for seq, kind, payload, row_session in store.conn.execute(
        "SELECT seq, kind, payload, session FROM log WHERE seq > ? ORDER BY seq",
        (first_seq,),
    ):
        turn = -1
        for t, mark in bounds:
            if seq > mark:
                turn = t
        doc = json.loads(payload)
        role = None
        if kind == "memory_put":
            mid = doc.get("memory", {}).get("id")
            role = "existing" if mid in existed else "new"
            existed.add(mid)
        rows.append(
            {
                "turn": turn,
                "kind": kind,
                "payload": doc,
                "session": row_session,
                "role": role,
            }
        )
    return {
        "session": session,
        "cwd": str(checkout.path),
        "checkout": checkout.checkout_id,
        "project": checkout.project_id,
        "global": checkout.kind == "folder",
        "turns": max((t for t, _ in marks), default=0),
        "rows": rows,
    }


def calibrate(
    scratch: Path,
    *,
    sessions: int,
    seed: int,
    plan: SessionPlan,
    scale: float = 1.0,
    backdated: int = 900,
) -> dict[str, Any]:
    """Build a template store at the topology of `scale` (1x), back-date
    `backdated` records into it over the span, then run `sessions`
    sessions through the public API and keep each one's rows as a
    template. Returns the templates and the API's timings and rate."""
    rng = random.Random(seed)
    store_dir = scratch / "store"
    config = config_for(store_dir)
    checkouts = topology(scratch / "repos", scale, rng)
    span_end = datetime.now(timezone.utc) - timedelta(hours=2)
    clock = world.Clock(span_end - world.days(SPAN_DAYS_1X))
    with world.stamping(clock):
        # Created under the clock: its first row is stamped at the span's
        # start, so the log's stamps rise from it.
        store = open_store(store_dir)
        for ck in checkouts:
            sight(store, config, ck)
        _backdate(store, checkouts, clock, rng, count=backdated, end=span_end)
    from bettermemory.recall_state import RecallState

    w = World(store, config, RecallState(), scratch / "transcripts")
    templates = []
    per_session = []
    weights = [ck.weight for ck in checkouts]
    existed = {str(r[0]) for r in store.conn.execute("SELECT id FROM memories")}
    existed |= {str(r[0]) for r in store.conn.execute("SELECT id FROM tombstones")}
    template_memory_ids = set(existed)
    started = time.perf_counter()
    rows_before = _last_seq(store)
    for index in range(sessions):
        ck = rng.choices(checkouts, weights=weights)[0]
        session = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        first = _last_seq(store)
        marks: list[tuple[int, int]] = []
        result = asyncio.run(api_session(w, ck, session, rng, plan, marks))
        per_session.append(result)
        templates.append(capture_template(store, ck, session, first, marks, existed))
        if index % 10 == 9:
            print(f"calibrate: {index + 1}/{sessions} sessions", flush=True)
    elapsed = time.perf_counter() - started
    rows = _last_seq(store) - rows_before
    template_memory_ids |= {
        str(r[0]) for r in store.conn.execute("SELECT id FROM memories")
    } | {str(r[0]) for r in store.conn.execute("SELECT id FROM tombstones")}
    store.close()
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    return {
        "templates": templates,
        "template_memory_ids": sorted(template_memory_ids),
        "api": {
            "sessions": sessions,
            "seconds": round(elapsed, 2),
            "rows": rows,
            "rows_per_second": round(rows / elapsed, 1),
            "sessions_per_second": round(sessions / elapsed, 3),
            "timings": {k: world.summary(v) for k, v in w.timings.items()},
            "statuses": dict(
                sum((Counter(r["statuses"]) for r in per_session), Counter())
            ),
        },
    }


def _backdate(
    store: Any,
    checkouts: list[Checkout],
    clock: world.Clock,
    rng: random.Random,
    *,
    count: int,
    end: datetime,
) -> None:
    """`count` records written over the span ending at `end`, each in a
    checkout drawn by weight, through `put_memory` in one batch."""
    from bettermemory.models import Memory

    start = clock.now
    span = (end - start).total_seconds()
    times = sorted(start + timedelta(seconds=rng.random() * span) for _ in range(count))
    weights = [ck.weight for ck in checkouts]
    with store.batch():
        for when in times:
            clock.now = when
            ck = rng.choices(checkouts, weights=weights)[0]
            memory = Memory.model_validate(
                {
                    "id": world.ulid_at(when, rng),
                    "created": world.iso(when),
                    "updated": world.iso(when),
                    "scopes": [f"projects:{ck.name}"]
                    if ck.kind != "folder"
                    else ["tools"],
                    "confidence": "medium",
                    "source": "explicit-statement",
                    "body": world.body(rng, ck.words) + "\n",
                    "origin": ck.origin(),
                    "category": "fact",
                }
            )
            store.put_memory(memory, provenance="local", home=ck.home)


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


class Pool:
    """Active record ids, overall and by project, with O(1) add, remove
    and draw."""

    def __init__(self) -> None:
        self._sets: dict[Any, tuple[list[str], dict[str, int]]] = {}
        self.projects: dict[str, Any] = {}

    def _set(self, key: Any) -> tuple[list[str], dict[str, int]]:
        return self._sets.setdefault(key, ([], {}))

    def add(self, record: str, project: Any) -> None:
        for key in (None, project):
            items, where = self._set(key)
            if record not in where:
                where[record] = len(items)
                items.append(record)
        self.projects[record] = project

    def remove(self, record: str) -> None:
        project = self.projects.pop(record, None)
        for key in (None, project):
            items, where = self._set(key)
            index = where.pop(record, None)
            if index is None:
                continue
            last = items.pop()
            if last != record:
                items[index] = last
                where[last] = index

    def draw(self, rng: random.Random, project: Any, local: float = 0.7) -> str | None:
        items = self._set(project)[0]
        if not items or rng.random() > local:
            items = self._set(None)[0]
        return rng.choice(items) if items else None

    def __len__(self) -> int:
        return len(self._set(None)[0])


@dataclass
class Replayed:
    """One replayed session: its template, checkout, start, and the maps
    from the template's ids to its own."""

    template: dict[str, Any]
    checkout: Checkout
    start: datetime
    session: str
    offsets: list[float]
    ids: dict[str, str] = field(default_factory=dict)
    consumed: set[int] = field(default_factory=set)
    next_row: int = 0


@dataclass
class Shape:
    """What a store at a scale is built from: an import at the span's
    start (the records a store holds from before its log, as a v8
    migration brings them: `imported` memories, `imported_episodes`
    episodes, a standing answer on `imported_standing` of them) and the
    replayed sessions after it, over `span_days`."""

    scale: float
    sessions: int
    imported: int
    imported_episodes: int
    imported_standing: float
    span_days: float
    projects: int
    rates: dict[str, float]


#: Template rows a replay does not write: the template store's own
#: membership rows, its conflicts and the rows no session writes.
_REPLAYED_MUTATIONS = frozenset(
    {
        "memory_put",
        "memory_delete",
        "tombstone_put",
        "episode_put",
        "home_put",
        "standing_put",
        "verification_put",
    }
)


def template_rates(templates: list[dict[str, Any]]) -> dict[str, float]:
    """Per template session: the records it writes and removes, its
    episodes, standing answers and the rows a replay of it appends."""
    from bettermemory.store_schema import MUTATION_KINDS

    n = len(templates)
    count: Counter[str] = Counter()
    for t in templates:
        for r in t["rows"]:
            kind = r["kind"]
            if kind == "memory_put":
                count["new" if r["role"] == "new" else "existing"] += 1
            elif kind in ("memory_delete", "episode_put", "standing_put"):
                count[kind] += 1
            if kind.startswith("capture_") or (
                kind in MUTATION_KINDS and kind not in _REPLAYED_MUTATIONS
            ):
                continue
            count["rows"] += 1
    return {
        "new": count["new"] / n,
        "removed": count["memory_delete"] / n,
        "episodes": count["episode_put"] / n,
        "standing": count["standing_put"] / n,
        "rows": count["rows"] / n,
    }


#: The log rows and membership rows a checkout's first sight writes, as
#: measured on the 1x build (about), and the checkouts per project.
_SIGHT_LOG_ROWS = 10.0
_SIGHT_MEMBERSHIP_ROWS = 7.0
_CHECKOUTS_PER_PROJECT = 1.25


def shape_for(scale: float, templates: list[dict[str, Any]]) -> Shape:
    """The import and the sessions that give a store the live store's
    memories, episodes, log rows and membership rows times `scale`, at
    the templates' rates per session (solved by fixed point: the import's
    standing share moves the rows each imported record writes)."""
    rates = template_rates(templates)
    live = world.LIVE_SHAPE
    memories = live["memories"] * scale
    episodes = live["episodes"] * scale
    rows = live["log_rows"] * scale
    membership = live["membership_rows"] * scale
    projects = projects_at(scale)
    checkouts = projects * _CHECKOUTS_PER_PROJECT + 2
    rows -= checkouts * _SIGHT_LOG_ROWS
    membership -= checkouts * _SIGHT_MEMBERSHIP_ROWS
    net = rates["new"] - rates["removed"]
    share = 0.5
    sessions = 0.0
    imported = imported_episodes = 0.0
    for _ in range(50):
        # rows = imported * (2 + share) + 2 * imported_episodes + sessions * rows/session
        # imported = memories - sessions * net; imported_episodes = episodes - sessions * e
        e = rates["episodes"]
        per = rates["rows"] - net * (2 + share) + (2 * e if episodes > 0 else 0)
        fixed = rows - memories * (2 + share) - 2 * episodes
        sessions = max(1.0, fixed / per)
        imported = max(0.0, memories - sessions * net)
        imported_episodes = max(0.0, episodes - sessions * e)
        homes = imported + sessions * rates["new"] + imported_episodes + sessions * e
        standing = membership - homes - sessions * rates["standing"]
        share = min(1.0, max(0.0, standing / imported)) if imported else 0.0
    return Shape(
        scale=scale,
        sessions=round(sessions),
        imported=round(imported),
        imported_episodes=round(imported_episodes),
        imported_standing=round(share, 3),
        span_days=SPAN_DAYS_1X * min(scale, SPAN_GROWS_TO),
        projects=projects,
        rates={k: round(v, 3) for k, v in rates.items()},
    )


class Replay:
    """The time-ordered replay of the templates into a store."""

    def __init__(
        self,
        store: Any,
        config: Any,
        checkouts: list[Checkout],
        templates: list[dict[str, Any]],
        memory_ids: set[str],
        rng: random.Random,
        clock: world.Clock,
    ) -> None:
        self.store = store
        self.config = config
        self.checkouts = checkouts
        self.templates = templates
        self.memory_ids = memory_ids
        self.rng = rng
        self.clock = clock
        self.pool = Pool()
        self.counts: Counter[str] = Counter()
        self.global_home: Any = None
        self._batch: contextlib.ExitStack | None = None
        self._rows_in_batch = 0
        self._last = clock.now
        self._seq = _last_seq(store)

    # -- transactions ------------------------------------------------------

    def _open(self) -> None:
        if self._batch is None:
            self._batch = contextlib.ExitStack()
            self._batch.enter_context(self.store.batch())
            self._rows_in_batch = 0

    def _commit(self) -> None:
        if self._batch is not None:
            self._batch.close()
            self._batch = None

    def _tick(self, when: datetime) -> None:
        if when <= self._last:
            when = self._last + timedelta(microseconds=1000)
        self._last = when
        self.clock.now = when

    # -- the run -------------------------------------------------------------

    def run(self, shape: Shape, start: datetime, end: datetime) -> None:
        span = (end - start).total_seconds()
        starts = sorted(
            start + timedelta(seconds=self.rng.random() * span)
            for _ in range(shape.sessions)
        )
        self._plan_sights(start, span)
        weights = [ck.weight for ck in self.checkouts]
        heap: list[tuple[datetime, int, int]] = []
        sessions: dict[int, Replayed] = {}
        counter = 0
        pending = iter(enumerate(starts))
        upcoming = next(pending, None)
        progress = time.perf_counter()
        with world.stamping(self.clock):
            for ck in self.checkouts:
                if ck.first_sight == start:
                    self._sight(ck, start)
            self._import(shape, start)
            while heap or upcoming is not None:
                # Admit every session that starts before the next row.
                while upcoming is not None and (not heap or upcoming[1] <= heap[0][0]):
                    index, when = upcoming
                    replay = self._session(when, weights)
                    sessions[index] = replay
                    heapq.heappush(
                        heap,
                        (
                            replay.start + timedelta(seconds=replay.offsets[0]),
                            counter,
                            index,
                        ),
                    )
                    counter += 1
                    upcoming = next(pending, None)
                when, _, index = heapq.heappop(heap)
                replay = sessions[index]
                self._tick(when)
                self._row(replay)
                replay.next_row += 1
                while replay.next_row in replay.consumed:
                    replay.next_row += 1
                if replay.next_row < len(replay.template["rows"]):
                    at = replay.start + timedelta(
                        seconds=replay.offsets[replay.next_row]
                    )
                    heapq.heappush(heap, (at, counter, index))
                    counter += 1
                else:
                    del sessions[index]
                if time.perf_counter() - progress > 30:
                    progress = time.perf_counter()
                    print(
                        f"replay: {self.counts['rows']:,} rows, {len(self.pool):,} active, "
                        f"at {world.iso(self.clock.now)[:10]}",
                        flush=True,
                    )
            self._commit()

    def _import(self, shape: Shape, start: datetime) -> None:
        """The records the store holds from before its log, put at the
        span's start as a migration puts them: created over the half year
        before, in the checkouts seen at the start, a standing answer on
        `shape.imported_standing` of them."""
        from bettermemory.models import Episode, Memory
        from bettermemory.store_schema import StandingAnswer

        rng = self.rng
        early = [ck for ck in self.checkouts if ck.home is not None]
        weights = [ck.weight for ck in early]
        made = sorted(
            start - timedelta(days=rng.random() * 180)
            for _ in range(shape.imported + shape.imported_episodes)
        )
        kinds = ["memory"] * shape.imported + ["episode"] * shape.imported_episodes
        rng.shuffle(kinds)
        session = "import-" + format(rng.getrandbits(32), "08x")
        for when, kind in zip(made, kinds):
            self._open()
            self._tick(start)
            ck = rng.choices(early, weights=weights)[0]
            if kind == "memory":
                memory = Memory.model_validate(
                    {
                        "id": world.ulid_at(when, rng),
                        "created": world.iso(when),
                        "updated": world.iso(when),
                        "scopes": [f"projects:{ck.name}"]
                        if ck.kind != "folder"
                        else [rng.choice(["tools", "preferences", "infrastructure"])],
                        "confidence": rng.choice(["high", "medium", "medium", "low"]),
                        "source": "explicit-statement",
                        "body": world.body(rng, ck.words) + "\n",
                        "origin": ck.origin(),
                        "category": rng.choices(
                            ["fact", "user-inference", "ambient"], weights=[85, 10, 5]
                        )[0],
                    }
                )
                standing = None
                if rng.random() < shape.imported_standing:
                    standing = StandingAnswer(round(rng.random(), 2), "asked", "bench")
                self.store.put_memory(
                    memory,
                    provenance="imported",
                    session=session,
                    home=ck.home,
                    standing=standing,
                )
                self.pool.add(memory.id, ck.home.project)
                self.counts["imported"] += 1
            else:
                episode = Episode.model_validate(
                    {
                        "id": world.ulid_at(when, rng),
                        "session_id": "import-" + format(rng.getrandbits(32), "08x"),
                        "created": world.iso(when),
                        "body": world.body(rng, ck.words) + "\n",
                        "takeaway": world.sentence(rng, ck.words, 10),
                        "origin": ck.origin(),
                    }
                )
                self.store.put_episode(episode, session=session, home=ck.home)
                self.counts["imported_episodes"] += 1
            self._count()
        self._commit()

    def _count(self) -> None:
        """Count the rows appended since the last count (a first sight's
        among them) and commit the batch once it holds `BATCH_ROWS`."""
        after = self.store._last_appended
        if after is not None and after.seq > self._seq:
            appended = after.seq - self._seq
            self._seq = after.seq
            self.counts["rows"] += appended
            self._rows_in_batch += appended
        if self._rows_in_batch >= BATCH_ROWS:
            self._commit()

    def _plan_sights(self, start: datetime, span: float) -> None:
        """The order checkouts are first seen in: the most used third and
        the folders at the start, the rest at times across the span."""
        order = sorted(self.checkouts, key=lambda ck: -ck.weight)
        early = max(3, len(order) // 3)
        for rank, ck in enumerate(order):
            if rank < early or ck.kind == "folder":
                ck.first_sight = start
            else:
                ck.first_sight = start + timedelta(
                    seconds=self.rng.random() * span * 0.9
                )

    def _session(self, when: datetime, weights: list[float]) -> Replayed:
        live = [
            ck
            for ck in self.checkouts
            if ck.first_sight is not None and ck.first_sight <= when
        ]
        if not live:
            live = self.checkouts[:1]
        ck = self.rng.choices(live, weights=[c.weight for c in live])[0]
        if ck.home is None:
            self._sight(ck, when)
        template = self.rng.choice(self.templates)
        while not template["rows"]:
            template = self.rng.choice(self.templates)
        return Replayed(
            template=template,
            checkout=ck,
            start=when,
            session=str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
            offsets=self._offsets(template),
        )

    def _sight(self, ck: Checkout, when: datetime) -> None:
        """`ck`'s first sight at `when`, outside the replay's batch: the
        resolution takes the write lock itself."""
        self._tick(when)
        self._commit()
        sight(self.store, self.config, ck)
        self.counts["sights"] += 1
        if ck.kind == "folder" and self.global_home is None:
            self.global_home = ck.home

    def _offsets(self, template: dict[str, Any]) -> list[float]:
        """Seconds from the session's start for each row: a turn every 40
        s to 7 min, its rows a fraction of a second apart."""
        turns = template["turns"] + 2
        starts = [0.0]
        for _ in range(turns):
            starts.append(starts[-1] + self.rng.uniform(40, 420))
        out = []
        within: Counter[int] = Counter()
        for row in template["rows"]:
            turn = min(row["turn"] + 1, turns)
            out.append(starts[turn] + 0.35 * within[turn])
            within[turn] += 1
        # Rows keep their order within a session.
        for i in range(1, len(out)):
            if out[i] <= out[i - 1]:
                out[i] = out[i - 1] + 0.01
        return out

    # -- one row -------------------------------------------------------------

    def _row(self, replay: Replayed) -> None:
        from bettermemory.store_schema import MUTATION_KINDS

        self._open()
        index = replay.next_row
        row = replay.template["rows"][index]
        kind = row["kind"]
        if kind in MUTATION_KINDS:
            self._mutation(replay, index, row)
        elif kind.startswith("capture_") or kind in ("store_created", "rekey"):
            self.counts[f"skipped:{kind}"] += 1
        else:
            payload = self._map(replay, row["payload"])
            session = self._map(replay, row["session"]) if row["session"] else None
            self.store._append(self.store.conn, kind, payload, session=session)
        self._count()

    def _lookahead(
        self, replay: Replayed, index: int, kind: str, key: str, value: str
    ) -> tuple[int, dict[str, Any]] | None:
        """The first row of `kind` within the next few after `index` whose
        payload names `value` at `key` (dotted), not yet consumed."""
        rows = replay.template["rows"]
        for j in range(index + 1, min(len(rows), index + 12)):
            if j in replay.consumed or rows[j]["kind"] != kind:
                continue
            found: Any = rows[j]["payload"]
            for part in key.split("."):
                found = found.get(part) if isinstance(found, dict) else None
            if found == value:
                return j, rows[j]["payload"]
        return None

    def _home(self, replay: Replayed, index: int, record: str) -> Any:
        found = self._lookahead(replay, index, "home_put", "home.record_id", record)
        if found is None:
            return None
        replay.consumed.add(found[0])
        if found[1]["home"].get("project") == "global" and self.global_home is not None:
            return self.global_home
        return replay.checkout.home

    def _mutation(self, replay: Replayed, index: int, row: dict[str, Any]) -> None:
        kind = row["kind"]
        payload = row["payload"]
        ck = replay.checkout
        rng = self.rng
        if kind == "memory_put" and row["role"] == "new":
            self._new_memory(replay, index, payload)
        elif kind == "memory_put":
            template_id = payload["memory"]["id"]
            verified = self._lookahead(
                replay, index, "verification_put", "memory_id", template_id
            )
            target = self._target(replay, template_id)
            if target is None:
                self.counts["skipped:no_target"] += 1
                return
            if verified is not None:
                replay.consumed.add(verified[0])
                self.store.mark_verified(
                    target, verified_head=None, session=replay.session
                )
                self.counts["verified"] += 1
            elif "corroboration" in payload:
                self.store.record_corroboration(target, session=replay.session)
                self.counts["corroborated"] += 1
            else:
                memory = self.store.get_memory(target)
                changed = memory.model_copy(
                    update={
                        "body": memory.body.rstrip()
                        + " "
                        + world.sentence(rng, ck.words, 10)
                        + "\n",
                        "updated": self.clock.now,
                    }
                )
                self.store.put_memory(changed, session=replay.session)
                self.counts["updated"] += 1
        elif kind == "memory_delete":
            target = self._target(replay, payload["id"])
            if target is None:
                self.counts["skipped:no_target"] += 1
                return
            dead = self._lookahead(
                replay, index, "tombstone_put", "tombstone.id", payload["id"]
            )
            reason = "superseded"
            if dead is not None:
                replay.consumed.add(dead[0])
                reason = str(dead[1]["tombstone"].get("removed_reason") or reason)
            self.store.tombstone(
                target, reason, session=replay.session, now=self.clock.now
            )
            self.pool.remove(target)
            self.counts["removed"] += 1
        elif kind == "episode_put":
            self._episode(replay, index, payload)
        elif kind == "standing_put":
            target = self._target(replay, payload["standing"]["record_id"])
            if target is None:
                return
            from bettermemory.store_schema import StandingAnswer

            standing = payload["standing"]
            self.store.put_standing(
                target,
                StandingAnswer(
                    float(standing["probability"]),
                    str(standing["source"]),
                    str(standing["answered_by"]),
                ),
                session=replay.session,
            )
            self.counts["standing"] += 1
        else:
            self.counts[f"skipped:{kind}"] += 1

    def _target(self, replay: Replayed, template_id: str) -> str | None:
        """The record a template's put of an existing record lands on: the
        one this session already mapped it to, else one drawn from the
        active records (of the session's project, mostly)."""
        mapped = replay.ids.get(template_id)
        if mapped is not None and mapped in self.pool.projects:
            return mapped
        drawn = self.pool.draw(self.rng, replay.checkout.project_id)
        if drawn is not None:
            replay.ids[template_id] = drawn
        return drawn

    def _new_memory(
        self, replay: Replayed, index: int, payload: dict[str, Any]
    ) -> None:
        from bettermemory.models import Memory
        from bettermemory.store_schema import StandingAnswer

        ck = replay.checkout
        template = payload["memory"]
        record = world.ulid_at(self.clock.now, self.rng)
        replay.ids[template["id"]] = record
        doc = self._map(replay, template)
        doc.update(
            {
                "id": record,
                "created": world.iso(self.clock.now),
                "updated": world.iso(self.clock.now),
                "body": world.body(self.rng, ck.words) + "\n",
                "origin": ck.origin(),
                "scopes": [f"projects:{ck.name}"]
                if ck.kind != "folder"
                else [self.rng.choice(["tools", "preferences", "infrastructure"])],
                "last_verified_at": None,
            }
        )
        memory = Memory.model_validate(doc)
        home = self._home(replay, index, template["id"])
        standing = None
        found = self._lookahead(
            replay, index, "standing_put", "standing.record_id", template["id"]
        )
        if found is not None:
            replay.consumed.add(found[0])
            row = found[1]["standing"]
            standing = StandingAnswer(
                float(row["probability"]), str(row["source"]), str(row["answered_by"])
            )
        self.store.put_memory(
            memory,
            provenance=str(payload.get("provenance") or "local"),
            session=replay.session,
            home=home or ck.home,
            standing=standing,
        )
        self.pool.add(record, (home or ck.home).project)
        self.counts["written"] += 1

    def _episode(self, replay: Replayed, index: int, payload: dict[str, Any]) -> None:
        from bettermemory.models import Episode

        ck = replay.checkout
        template = payload["episode"]
        record = world.ulid_at(self.clock.now, self.rng)
        replay.ids[template["id"]] = record
        episode = Episode.model_validate(
            {
                "id": record,
                "session_id": replay.session,
                "created": world.iso(self.clock.now),
                "body": world.body(self.rng, ck.words) + "\n",
                "scopes": list(template.get("scopes") or []),
                "takeaway": world.sentence(self.rng, ck.words, 10)
                if template.get("takeaway")
                else None,
                "origin": ck.origin(),
                "is_floor": bool(template.get("is_floor")),
            }
        )
        home = self._home(replay, index, template["id"])
        self.store.put_episode(episode, session=replay.session, home=home or ck.home)
        self.counts["episodes"] += 1

    # -- mapping -------------------------------------------------------------

    def _map(self, replay: Replayed, value: Any) -> Any:
        if isinstance(value, str):
            return self._map_text(replay, value)
        if isinstance(value, list):
            return [self._map(replay, v) for v in value]
        if isinstance(value, dict):
            return {
                (self._map_text(replay, k) if ULID.fullmatch(k) else k): self._map(
                    replay, v
                )
                for k, v in value.items()
            }
        return value

    def _map_text(self, replay: Replayed, text: str) -> str:
        ids = replay.ids
        if text in ids:
            return ids[text]
        template = replay.template
        if ULID.fullmatch(text):
            if text in self.memory_ids:
                drawn = self.pool.draw(self.rng, replay.checkout.project_id)
                mapped = (
                    drawn
                    if drawn is not None
                    else world.ulid_at(self.clock.now, self.rng)
                )
            else:
                mapped = world.ulid_at(self.clock.now, self.rng)
            ids[text] = mapped
            return mapped
        ck = replay.checkout
        if text == template["checkout"] and ck.checkout_id:
            return ck.checkout_id
        if text == template["project"] and ck.project_id:
            return ck.project_id
        if text.startswith("sess_"):
            mapped = "sess_" + format(self.rng.getrandbits(64), "016x")
            ids[text] = mapped
            return mapped
        if template["session"] in text:
            text = text.replace(template["session"], replay.session)
        cwd = template["cwd"]
        if text == cwd or text.startswith(cwd + "/"):
            text = str(ck.path) + text[len(cwd) :]
        return text


def census(store: Any) -> dict[str, Any]:
    """What a store holds: its memories, episodes, tombstones, log rows
    by kind, membership rows by table, and its file's size."""
    from bettermemory.store_schema import MEMBERSHIP_TABLES

    conn = store.conn

    def count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    kinds = {
        str(k): int(n)
        for k, n in conn.execute(
            "SELECT kind, COUNT(*) FROM log GROUP BY kind ORDER BY 2 DESC"
        )
    }
    membership = {t: count(t) for t in MEMBERSHIP_TABLES}
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM log").fetchone()
    path = Path(store.path)
    size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*"))
    return {
        "memories": count("memories"),
        "episodes": count("episodes"),
        "tombstones": count("tombstones"),
        "log_rows": count("log"),
        "membership_rows": sum(membership.values()),
        "membership_tables": membership,
        "projects": count("projects"),
        "checkouts": count("checkouts"),
        "sessions": int(
            conn.execute("SELECT COUNT(DISTINCT session) FROM log").fetchone()[0]
        ),
        "log_kinds": kinds,
        "log_span": [span[0], span[1]],
        "file_mb": round(size / 1e6, 1),
    }


def generate(
    scratch: Path,
    *,
    scale: float,
    templates_file: Path,
    seed: int,
) -> dict[str, Any]:
    """Build the store at `scale` under `scratch` from the templates in
    `templates_file`. Returns its census and the build's timings."""
    from bettermemory import store_index

    data = json.loads(templates_file.read_text(encoding="utf-8"))
    templates = data["templates"]
    memory_ids = set(data["template_memory_ids"])
    rng = random.Random(seed)
    shape = shape_for(scale, templates)
    store_dir = scratch / "store"
    config = config_for(store_dir)
    started = time.perf_counter()
    checkouts = topology(scratch / "repos", scale, rng)
    repos_s = time.perf_counter() - started
    end = datetime.now(timezone.utc) - timedelta(hours=1)
    start = end - world.days(shape.span_days)
    clock = world.Clock(start)
    with world.stamping(clock):
        # Created under the clock (`calibrate`).
        store = open_store(store_dir)
    replay = Replay(store, config, checkouts, templates, memory_ids, rng, clock)
    os.chdir(scratch / "home")
    t0 = time.perf_counter()
    replay.run(shape, start, end)
    replay_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    store_index.optimize_apart(store, "schedule")
    optimize_s = time.perf_counter() - t0
    shape_out = census(store)
    store.close()
    (scratch / "checkouts.json").write_text(
        json.dumps(
            [
                {
                    "path": str(ck.path),
                    "name": ck.name,
                    "kind": ck.kind,
                    "weight": ck.weight,
                    "sighted": ck.home is not None,
                    "words": ck.words,
                }
                for ck in checkouts
            ],
            indent=1,
        ),
        encoding="utf-8",
    )
    return {
        "scale": scale,
        "seed": seed,
        "planned": dataclasses.asdict(shape),
        "census": shape_out,
        "replay_counts": dict(replay.counts),
        "seconds": {
            "repositories": round(repos_s, 1),
            "replay": round(replay_s, 1),
            "optimize": round(optimize_s, 1),
        },
        "rows_per_second": round(replay.counts["rows"] / max(replay_s, 1e-9), 1),
        "peak_rss_mb": world.self_peak_rss_mb(),
    }


@contextlib.contextmanager
def chdir(path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(here)
