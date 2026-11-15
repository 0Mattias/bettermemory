"""Replay the window's memory_write chains through a tree's handlers.

The harness transcripts (``~/.claude/projects/**/*.jsonl``) hold every
``memory_write`` call a model made, with its arguments and the response
it got. This program groups a session's calls into chains (the calls of
one write, from its first call to its commit or the call after which the
model stopped), takes a read-only snapshot of the store
(`store_side.snapshot`), opens a copy of it under a scratch key directory
(`scratch_store`), removes from the copy the records and tombstones
created in the window, and replays the chains in time order through
`handlers.write.memory_write` of the tree on ``sys.path`` (``--src``
puts another tree's ``src`` first, for a baseline).

Each chain is replayed as the model answered it. The first call is the
model's first call as sent. After it the replay follows the pages the
handler now issues: a page is answered with the label the model gave
each id anywhere in the chain, the scores of its last answers and its
last ``named``; an id it never answered is a miss, and the chain ends as
``needs_new_answer``. A gate refusal or a text error moves the replay to
the model's next distinct arguments (its content, scopes, category and
flags), as the model moved on from it; a ``refused_standing`` is retried
with the model's ``said`` when it gave one. The chain ends at a commit, a
refusal the model did not move past, or when its arguments run out.

What it reports, aggregates only: calls and characters per committed
write, the same for standing writes (the calls before ``said`` apart),
chains per outcome kind, the original outcome against the replayed one,
the commits that took a standing record out of force without ``said``,
and the chains that could not be replayed and why. Characters are
measured as the MCP server puts a result on the wire (JSON with an
indent of two), and a text error by its message. No content, id, query
or project name is printed or written. The copy is deleted when the
program ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent

DEFAULT_SINCE = "2026-11-04"

#: The arguments memory_write takes; a call that sent another is replayed
#: as the text error it got, which the review does not change.
WRITE_PARAMS = frozenset(
    {
        "content",
        "scopes",
        "confidence",
        "source",
        "force",
        "acknowledge_transient",
        "acknowledge_scope_mismatch",
        "acknowledge_credential",
        "acknowledge_user_claim",
        "category",
        "claims",
        "supersedes",
        "review",
    }
)

#: The arguments that make one call's request differ from another's,
#: besides its answers.
_ARG_KEYS = tuple(sorted(WRITE_PARAMS - {"review"}))

#: The gates' refusals a flag passes, and the review's and policy's own.
GATE_REFUSALS = frozenset(
    {"credential_warning", "transient_warning", "user_claim_warning", "scope_mismatch"}
)

STANDING_REFUSED = "refused_standing"


def _setup_path(src: Path | None) -> None:
    sys.path.insert(0, str(_ROOT / "bench"))
    sys.path.insert(0, str(_HERE))
    sys.path.insert(0, str(src if src is not None else _ROOT / "src"))


def wire_chars(result: Any) -> int:
    import pydantic_core

    return len(pydantic_core.to_json(result, fallback=str, indent=2).decode())


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The transcripts
# ---------------------------------------------------------------------------


@dataclass
class Call:
    """One memory_write call as the transcript holds it."""

    use_id: str
    session: str
    ts: datetime
    cwd: str | None
    args: dict[str, Any]
    text: str = ""
    is_error: bool = False
    chars: int = 0

    @property
    def status(self) -> str:
        if self.is_error:
            return "text_error"
        try:
            body = json.loads(self.text)
        except ValueError:
            return "text_error"
        status = body.get("status") if isinstance(body, dict) else None
        return str(status) if status else "text_error"

    @property
    def body(self) -> dict[str, Any]:
        try:
            body = json.loads(self.text)
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def arg_state(self) -> tuple[Any, ...]:
        return tuple(json.dumps(self.args.get(k), sort_keys=True) for k in _ARG_KEYS)


@dataclass
class ChainOf:
    """The calls of one write in one session, in order."""

    calls: list[Call] = field(default_factory=list)
    review_ids: set[str] = field(default_factory=set)

    @property
    def outcome(self) -> str:
        return self.calls[-1].status

    @property
    def standing(self) -> bool:
        for call in self.calls:
            review = call.args.get("review")
            if call.args.get("category") == "ambient":
                return True
            if isinstance(review, dict) and isinstance(
                review.get("standing"), (int, float)
            ):
                if review["standing"] >= 0.3:
                    return True
        return False


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def read_calls(root: Path, since: datetime) -> list[Call]:
    """Every memory_write call under `root` from `since` on, once by its
    tool_use id, with its result."""
    floor = since.timestamp()
    calls: dict[str, Call] = {}
    results: dict[str, tuple[str, bool]] = {}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < floor:
                continue
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = str(block.get("name") or "")
                        if not name.endswith("__memory_write"):
                            continue
                        use_id = block.get("id")
                        ts = _parse_ts(entry.get("timestamp"))
                        if not isinstance(use_id, str) or ts is None or ts < since:
                            continue
                        if use_id in calls:
                            continue
                        session = (
                            f"{entry.get('sessionId')}/{entry.get('agentId') or ''}"
                        )
                        calls[use_id] = Call(
                            use_id=use_id,
                            session=session,
                            ts=ts,
                            cwd=entry.get("cwd"),
                            args=dict(block.get("input") or {}),
                        )
                    elif block.get("type") == "tool_result":
                        use_id = block.get("tool_use_id")
                        if isinstance(use_id, str) and use_id not in results:
                            results[use_id] = (
                                _result_text(block.get("content")),
                                bool(block.get("is_error")),
                            )
    out: list[Call] = []
    for use_id, call in calls.items():
        found = results.get(use_id)
        if found is None:
            continue
        call.text, call.is_error = found
        call.chars = len(call.text)
        out.append(call)
    out.sort(key=lambda c: (c.ts, c.use_id))
    return out


def _tokens(text: Any) -> set[str]:
    return set(str(text or "").lower().split())


def _close(a: Any, b: Any) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.5


def group_chains(calls: list[Call]) -> list[ChainOf]:
    """A session's calls in chains: a call joins the session's open chain
    when it answers a review_id the chain was given, re-sends a content
    the chain sent, or follows a refusal or text error with a close
    content; otherwise it opens a chain. A commit closes its chain."""
    by_session: dict[str, list[Call]] = defaultdict(list)
    for call in calls:
        by_session[call.session].append(call)
    chains: list[ChainOf] = []
    for session_calls in by_session.values():
        open_chain: ChainOf | None = None
        for call in session_calls:
            review = call.args.get("review")
            sent_id = review.get("review_id") if isinstance(review, dict) else None
            joins = False
            if open_chain is not None:
                last = open_chain.calls[-1]
                joins = (
                    (isinstance(sent_id, str) and sent_id in open_chain.review_ids)
                    or any(
                        c.args.get("content") == call.args.get("content")
                        for c in open_chain.calls
                    )
                    or (
                        last.status != "review"
                        and _close(last.args.get("content"), call.args.get("content"))
                    )
                )
            if not joins:
                open_chain = ChainOf()
                chains.append(open_chain)
            assert open_chain is not None
            open_chain.calls.append(call)
            page = call.body.get("review")
            if isinstance(page, dict) and isinstance(page.get("review_id"), str):
                open_chain.review_ids.add(page["review_id"])
            if call.status == "committed":
                open_chain = None
    chains.sort(key=lambda ch: ch.calls[0].ts)
    return chains


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------


class Copy:
    """The scratch copy of the store, set back for each chain: the records
    created in the window are held apart, and before a chain that started
    at `t` the copy holds every one created before `t` under its own id,
    and none of the records the replay wrote. The digest is rebuilt from
    the records on each open."""

    def __init__(self, snapshot: Path, directory: Path, keys_dir: Path) -> None:
        self.directory = directory
        self.path = directory / "memory.sqlite"
        self.keys_dir = keys_dir
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot, self.path)
        self.held: list[tuple[str, datetime]] = []
        self.restored: set[str] = set()
        self.written: set[str] = set()
        self.deps: Any = None

    def hold(self, since: datetime) -> dict[str, int]:
        conn = sqlite3.connect(str(self.path))
        try:
            rows = conn.execute("SELECT id, created FROM memories").fetchall()
            held = [
                (str(i), ts)
                for i, created in rows
                if (ts := _parse_ts(created)) is not None and ts >= since
            ]
            ids = [i for i, _ in held]
            conn.execute("CREATE TABLE held_memories AS SELECT * FROM memories WHERE 0")
            conn.execute(
                "CREATE TABLE held_links AS SELECT * FROM memory_links WHERE 0"
            )
            for i in ids:
                conn.execute(
                    "INSERT INTO held_links SELECT * FROM memory_links "
                    "WHERE source_id = ? OR target_id = ?",
                    (i, i),
                )
                conn.execute(
                    "INSERT INTO held_memories SELECT * FROM memories WHERE id = ?",
                    (i,),
                )
                conn.execute("DELETE FROM memories WHERE id = ?", (i,))
            conn.commit()
        finally:
            conn.close()
        self.held = sorted(held, key=lambda row: row[1])
        return {"records_held": len(held)}

    def at(self, start: datetime) -> Any:
        """The handlers over the copy as it stood when a chain began at
        `start`."""
        if self.deps is not None:
            self.deps.store.close()
            self.deps = None
        due = [i for i, ts in self.held if ts < start and i not in self.restored]
        conn = sqlite3.connect(str(self.path))
        try:
            for i in sorted(self.written):
                conn.execute("DELETE FROM memories WHERE id = ?", (i,))
            for i in due:
                conn.execute(
                    "INSERT OR IGNORE INTO memories SELECT * FROM held_memories "
                    "WHERE id = ?",
                    (i,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_links SELECT * FROM held_links "
                    "WHERE source_id = ? OR target_id = ?",
                    (i, i),
                )
            try:
                conn.execute("DELETE FROM digest_state")
            except sqlite3.OperationalError:
                pass  # no digest yet: the first open builds it
            conn.commit()
        finally:
            conn.close()
        self.written.clear()
        self.restored.update(due)
        self.deps = _handlers(self.directory, self.keys_dir)
        return self.deps

    def close(self) -> None:
        if self.deps is not None:
            self.deps.store.close()
            self.deps = None


def _handlers(copy_dir: Path, keys_dir: Path) -> Any:
    import dataclasses

    from bettermemory._handlers import ToolHandlers
    from bettermemory._response import ResponseBuilder
    from bettermemory.config import Config, StorageConfig
    from bettermemory.events import Recorder
    from bettermemory.session import SessionState
    from scratch_store import scratch_store

    store = scratch_store(copy_dir, keys_dir)
    config = Config(storage=StorageConfig(directory=str(copy_dir)))
    config = dataclasses.replace(
        config,
        membership=dataclasses.replace(config.membership, write_token=False),
    )
    state = SessionState()
    return ToolHandlers(
        config=config,
        store=store,
        sessions=state,
        recorder=Recorder(store=store, session_id=state.session_id, enabled=True),
        responses=ResponseBuilder(
            stale_after_days=config.behavior.verification_stale_days
        ),
    )


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


class Miss(Exception):
    """A page asks for an answer the model never gave."""

    def __init__(self, kind: str, ids: list[str] | None = None) -> None:
        super().__init__(kind)
        self.ids = ids or []


@dataclass
class Model:
    """What the model said over a chain: its labels, its last scores, its
    last named, its words, and its distinct argument states."""

    labels: dict[str, str]
    scores: dict[str, float] | None
    named: list[str] | None
    said: str | None
    states: list[dict[str, Any]]
    listed: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, chain: ChainOf) -> Model:
        labels: dict[str, str] = {}
        scores: dict[str, float] | None = None
        named: list[str] | None = None
        said: str | None = None
        states: list[dict[str, Any]] = []
        seen: list[tuple[Any, ...]] = []
        for call in chain.calls:
            review = call.args.get("review")
            if isinstance(review, dict):
                relation = review.get("relation")
                if isinstance(relation, dict):
                    labels.update(
                        {k: v for k, v in relation.items() if isinstance(v, str)}
                    )
                got = {
                    k: review[k]
                    for k in ("unsafe", "secret", "standing")
                    if isinstance(review.get(k), (int, float))
                    and not isinstance(review.get(k), bool)
                }
                if len(got) == 3:
                    scores = got
                if isinstance(review.get("named"), list):
                    named = [i for i in review["named"] if isinstance(i, str)]
                if isinstance(review.get("said"), str):
                    said = review["said"]
            state = call.arg_state()
            if not seen or seen[-1] != state:
                seen.append(state)
                states.append({k: v for k, v in call.args.items() if k != "review"})
        return cls(labels, scores, named, said, states)

    def label(self, memory_id: str, alias: dict[str, str]) -> str | None:
        return self.labels.get(alias.get(memory_id, memory_id))

    def answers(
        self,
        page: dict[str, Any],
        shown: set[str],
        alias: dict[str, str],
        *,
        said: bool,
    ) -> dict[str, Any]:
        """The model's answers to `page`: every id the replay has shown,
        labelled as the model labelled it (a record the replay wrote under
        the id its original commit had); a page id it never labelled is a
        miss."""
        if self.scores is None:
            raise Miss("scores")
        related = [e["id"] for e in page.get("related") or [] if isinstance(e, dict)]
        unknown = [i for i in related if self.label(i, alias) is None]
        if unknown:
            raise Miss("relation", unknown)
        relation: dict[str, str] = {}
        for i in sorted(shown):
            got = self.label(i, alias)
            if got is not None:
                relation[i] = got
        out: dict[str, Any] = {"relation": relation, **self.scores}
        if isinstance(page.get("review_id"), str):
            out["review_id"] = page["review_id"]
        roster = page.get("roster")
        if roster is not None or self.named is not None:
            if roster is not None and self.named is None:
                raise Miss("named")
            back = {v: k for k, v in alias.items()}
            named = [back.get(i, i) for i in self.named or []]
            if roster is not None:
                listed = {str(line).split(" ", 1)[0] for line in roster}
                self.listed |= listed
            out["named"] = [i for i in named if i in self.listed]
        if said and self.said is not None:
            out["said"] = self.said
        return out


@dataclass
class Replayed:
    outcome: str
    calls: int = 0
    chars: int = 0
    calls_before_said: int = 0
    chars_before_said: int = 0
    miss: str | None = None
    record: str | None = None
    out_of_force_without_said: bool = False
    trace: list[str] = field(default_factory=list)
    declared: bool = False
    refusal_then_page: int = 0
    miss_causes: Counter[str] = field(default_factory=Counter)


def _kwargs(state: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    out = {k: v for k, v in state.items() if k in WRITE_PARAMS}
    out.pop("review", None)
    if review is not None:
        out["review"] = review
    return out


async def replay_chain(
    deps: Any, chain: ChainOf, neutral: Path, alias: dict[str, str]
) -> Replayed:
    from bettermemory.handlers.write import memory_write

    model = Model.of(chain)
    first = chain.calls[0]
    cwd = first.cwd if isinstance(first.cwd, str) and os.path.isdir(first.cwd) else None
    os.chdir(cwd or neutral)
    if os.environ.get("REPLAY_WRITES_DEBUG"):
        print(f"chain cwd_exists={cwd is not None}", file=sys.stderr)
    result = Replayed(outcome="unanswered")
    review: dict[str, Any] | None = first.args.get("review")
    result.declared = first.args.get("category") == "ambient" or (
        isinstance(review, dict)
        and "review_id" not in review
        and isinstance(review.get("standing"), (int, float))
        and review["standing"] >= 0.3
    )
    original_shown = {
        e["id"]
        for c in chain.calls
        for e in (c.body.get("review") or {}).get("related") or []
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    }
    previous = ""
    state_at = 0
    shown: set[str] = set()
    said_sent = False
    last_page: dict[str, Any] | None = None
    for _ in range(12):
        state = model.states[min(state_at, len(model.states) - 1)]
        extra = set(state) - WRITE_PARAMS
        result.calls += 1
        if not said_sent:
            result.calls_before_said += 1
        if extra:
            # An argument the tool does not take: the call fails as it did.
            failed = next((c for c in chain.calls if set(c.args) - WRITE_PARAMS), first)
            result.chars += failed.chars
            if not said_sent:
                result.chars_before_said += failed.chars
            status = "text_error"
            response: dict[str, Any] = {}
        else:
            try:
                response = await memory_write(deps, **_kwargs(state, review))
                chars = wire_chars(response)
                status = str(response.get("status") or "unknown")
            except Exception as exc:  # noqa: BLE001  a teaching error counts as a call
                if os.environ.get("REPLAY_WRITES_DEBUG"):
                    print(_masked(str(exc)), file=sys.stderr)
                response = {}
                chars = len(f"Error executing tool memory_write: {exc}")
                status = "text_error"
            result.chars += chars
            if not said_sent:
                result.chars_before_said += chars
        result.outcome = status
        page = response.get("review") if isinstance(response, dict) else None
        if not isinstance(page, dict) and isinstance(response.get("related"), list):
            page = {"related": response["related"]}
        token = status
        if isinstance(page, dict) and "roster" in page:
            token += "+roster"
        if "roster records this review now holds" in str(response.get("hint")):
            token += "+named"
        result.trace.append(token)
        if previous in GATE_REFUSALS and status == "review":
            result.refusal_then_page += 1
        previous = status
        if status == "committed":
            original = chain.calls[-1].body.get("id")
            if isinstance(response.get("id"), str):
                result.record = response["id"]
                if isinstance(original, str):
                    alias[response["id"]] = original
            replaced = [
                row["id"]
                for row in response.get("supersedes") or []
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            ]
            said = isinstance(review, dict) and bool(review.get("said"))
            result.out_of_force_without_said = not said and _any_standing(
                deps, replaced
            )
            return result
        if isinstance(page, dict):
            last_page = page
            shown.update(
                e["id"] for e in page.get("related") or [] if isinstance(e, dict)
            )
        if status == "review" or (status in GATE_REFUSALS and isinstance(page, dict)):
            if status in GATE_REFUSALS:
                state_at += 1
                if state_at >= len(model.states):
                    return result
            try:
                review = model.answers(page or {}, shown, alias, said=said_sent)
            except Miss as miss:
                result.outcome = "needs_new_answer"
                result.miss = str(miss)
                start = first.ts
                for memory_id in miss.ids:
                    row = deps.store._conn.execute(
                        "SELECT updated FROM memories WHERE id = ?", (memory_id,)
                    ).fetchone()
                    updated = _parse_ts(row[0]) if row else None
                    if updated is not None and updated >= start:
                        result.miss_causes["changed_after_chain_start"] += 1
                    elif memory_id in original_shown:
                        result.miss_causes["shown_but_unlabelled"] += 1
                    else:
                        result.miss_causes["unchanged_never_shown"] += 1
                return result
            continue
        if status == STANDING_REFUSED:
            if model.said is None or said_sent:
                return result
            said_sent = True
            assert isinstance(review, dict)
            review = {**review, "said": model.said}
            if last_page is not None and "review_id" not in review:
                review["review_id"] = last_page["review_id"]
            continue
        # A gate refusal without a page, or a text error: the model's next
        # arguments, with the answers it holds.
        state_at += 1
        if state_at >= len(model.states):
            return result
        if review is not None and last_page is not None:
            try:
                review = model.answers(last_page, shown, alias, said=said_sent)
            except Miss:
                pass
    result.outcome = "loop"
    return result


def _any_standing(deps: Any, ids: list[str]) -> bool:
    """Whether any of `ids` was standing: ambient, or a standing answer at
    or above the threshold."""
    from bettermemory import standing as _standing

    for memory_id in ids:
        row = deps.store._conn.execute(
            "SELECT probability FROM standing WHERE record_id = ?", (memory_id,)
        ).fetchone()
        category = deps.store._conn.execute(
            "SELECT category FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if _standing.is_standing(
            category[0] if category else None, row[0] if row else None
        ):
            return True
    return False


def _masked(text: str) -> str:
    """An error's first words with ids and quoted text masked, for a
    private debugging run only."""
    import re

    text = re.sub(r"[0-9A-HJKMNP-TV-Z]{26}", "<id>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<q>", text)
    return text[:160]


def _replayable(chain: ChainOf) -> str | None:
    """Why `chain` cannot be replayed, or None."""
    first = chain.calls[0]
    source = str(first.args.get("source") or "")
    if source.startswith("capture:"):
        return "capture_batch"
    if not isinstance(first.args.get("content"), str):
        return "no_content"
    return None


def _per(values: list[int], n: int) -> float | None:
    return round(sum(values) / n, 2) if n else None


async def replay(copy: Copy, chains: list[ChainOf], neutral: Path) -> dict[str, Any]:
    outcomes: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    misses: Counter[str] = Counter()
    committed: list[Replayed] = []
    standing_committed: list[Replayed] = []
    original_committed: list[ChainOf] = []
    original_standing: list[ChainOf] = []
    refused_again_same = 0
    refused_in_window = 0
    new_refusal = 0
    refused_committed = 0
    alias: dict[str, str] = {}
    without_said = 0
    miss_causes: Counter[str] = Counter()
    refusal_pages = 0
    long_traces: Counter[str] = Counter()
    standing_declared: list[Replayed] = []
    standing_undeclared: list[Replayed] = []
    for chain in chains:
        if chain.outcome == "committed":
            original_committed.append(chain)
            if chain.standing:
                original_standing.append(chain)
        why = _replayable(chain)
        if why is not None:
            skipped[why] += 1
            continue
        deps = copy.at(chain.calls[0].ts)
        done = await replay_chain(deps, chain, neutral, alias)
        if done.record is not None:
            copy.written.add(done.record)
        without_said += done.out_of_force_without_said
        outcomes[done.outcome] += 1
        transitions[f"{chain.outcome} -> {done.outcome}"] += 1
        if done.miss:
            misses[done.miss] += 1
        miss_causes.update(done.miss_causes)
        refusal_pages += done.refusal_then_page
        if chain.standing and done.calls_before_said >= 4:
            long_traces[
                ("declared " if done.declared else "undeclared ")
                + " > ".join(done.trace)
            ] += 1
        if done.outcome == "committed":
            committed.append(done)
            if chain.standing:
                standing_committed.append(done)
                (standing_declared if done.declared else standing_undeclared).append(
                    done
                )
        original_refused = chain.outcome not in ("committed", "review", "text_error")
        if original_refused:
            refused_in_window += 1
            if done.outcome == chain.outcome:
                refused_again_same += 1
            if done.outcome == "committed":
                refused_committed += 1
        elif chain.outcome == "committed" and done.outcome not in (
            "committed",
            "needs_new_answer",
        ):
            new_refusal += 1

    def stats(rows: list[Replayed]) -> dict[str, Any]:
        n = len(rows)
        return {
            "n": n,
            "calls_per_write": _per([r.calls for r in rows], n),
            "chars_per_write": _per([r.chars for r in rows], n),
            "calls_before_said_per_write": _per([r.calls_before_said for r in rows], n),
            "chars_before_said_per_write": _per([r.chars_before_said for r in rows], n),
            "max_calls_before_said": max(
                (r.calls_before_said for r in rows), default=None
            ),
            "max_chars": max((r.chars for r in rows), default=None),
        }

    def original(rows: list[ChainOf]) -> dict[str, Any]:
        n = len(rows)
        return {
            "n": n,
            "calls_per_write": _per([len(c.calls) for c in rows], n),
            "chars_per_write": _per([sum(x.chars for x in c.calls) for c in rows], n),
        }

    return {
        "chains": len(chains),
        "replayed": sum(outcomes.values()),
        "not_replayed": dict(skipped.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "transitions": dict(transitions.most_common()),
        "needs_new_answer_by_kind": dict(misses.most_common()),
        "committed": stats(committed),
        "standing_committed": stats(standing_committed),
        "standing_declared_committed": stats(standing_declared),
        "standing_undeclared_committed": stats(standing_undeclared),
        "standing_traces_of_four_or_more_calls": dict(long_traces.most_common()),
        "pages_right_after_a_gate_refusal": refusal_pages,
        "miss_ids_by_cause": dict(miss_causes.most_common()),
        "transcript_committed": original(original_committed),
        "transcript_standing_committed": original(original_standing),
        "refused_in_window": refused_in_window,
        "refused_again_as_same_kind": refused_again_same,
        "refused_that_now_commit": refused_committed,
        "committed_that_now_refuse": new_refusal,
        "commits_out_of_force_without_said": without_said,
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
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="a snapshot taken before (`--take-snapshot`) to replay over instead",
    )
    parser.add_argument(
        "--take-snapshot",
        type=Path,
        help="only take a read-only snapshot of the store to this path and stop",
    )
    parser.add_argument("--src", type=Path, help="the tree's src to replay through")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="window start, UTC")
    parser.add_argument(
        "--transcripts",
        type=Path,
        default=Path(os.path.expanduser("~/.claude/projects")),
    )
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path, help="where to write the aggregates")
    args = parser.parse_args(argv)
    for path in (args.out, args.take_snapshot):
        if path is not None and path.resolve().is_relative_to(_ROOT):
            parser.error("outputs must lie outside the repository")
    _setup_path(args.src)
    import store_side

    if args.take_snapshot is not None:
        store_side.snapshot(args.store, args.take_snapshot)
        return 0

    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    started = datetime.now(timezone.utc)
    calls = read_calls(args.transcripts, since)
    chains = group_chains(calls)
    home = os.getcwd()
    from bettermemory import review as _review

    with tempfile.TemporaryDirectory(prefix="bettermemory-replay-writes-") as scratch:
        root = Path(scratch)
        copy_dir = root / "store"
        keys_dir = root / "keys"
        keys_dir.mkdir()
        neutral = root / "neutral"
        neutral.mkdir()
        os.environ["BETTERMEMORY_KEYS_DIR"] = str(keys_dir)
        snapshot = args.snapshot
        if snapshot is None:
            snapshot = store_side.snapshot(args.store, root / "snapshot.sqlite")
        copy = Copy(snapshot, copy_dir, keys_dir)
        prepared = copy.hold(since)
        try:
            body = asyncio.run(replay(copy, chains, neutral))
        finally:
            os.chdir(home)
            copy.close()

    out = {
        "kind": "write-replay",
        "label": args.label,
        "run_at": started.isoformat(),
        "window_since": args.since,
        "seal": _review.seal_shas(),
        "transcript_calls": len(calls),
        "copy": prepared,
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
