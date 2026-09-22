"""bettermemory behind the Agent Memory Leaderboard's Add/Search contract.

WHAT THIS IS. AML (agentmemoryleaderboard.ai) fixes the answer model,
prompts, judge and top_k, and calls exactly two operations on a system
under test: Add writes a chunk of conversation for a `user_id`, Search
returns ranked evidence for a question under the same `user_id`. This
module is that surface over the shipped engine: `Store.write` for Add,
`search.search` for Search. No model is called anywhere on this path;
the entry is deterministic code end to end, which is also what makes it
reproducible by AML's own rerun rule.

ONE STORE PER user_id. The user_id is AML's isolation scope and Search
must never read across it, so each gets its own store root, named by a
hash of the id (the id itself is caller-controlled text and never
touches the filesystem).

THE INGEST UNIT is one round: a user message plus the assistant reply
that follows it, the unit bench/longmemeval has measured since 2026-07.
A chunk that ends on an unpaired message stores it alone. Each round's
body leads with its source timestamp, because the fixed answer prompt
sees only what Search returns and the temporal questions are
unanswerable without the date.

EVENT TIME. `Memory.created` is storage time, and AML's timestamps are
when the conversation happened. The adapter keeps a per-store sidecar
mapping memory id to source timestamp; it feeds Search's `created_at`
field and the engine's `now` (the latest source timestamp in the store,
the clock a live assistant would have after the last conversation).

IDEMPOTENCY. AML retries Add with the same request_id up to 32 times.
A retried chunk must not write its rounds twice, so completed
request_ids are recorded in the same sidecar, and the write and the
record happen under the store's lock.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bettermemory import search as _engine
from bettermemory.models import Memory
from bettermemory.search import search as run_search
from bettermemory.store import Store

SCOPE = ["aml"]


def _fmt_ts(ms: int | None) -> str | None:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y/%m/%d (%a) %H:%M")


def rounds_of(messages: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """Pair messages into rounds; returns (body, source timestamp ms)."""
    out: list[tuple[str, int | None]] = []
    i = 0
    while i < len(messages):
        first = messages[i]
        parts = [f"{first.get('role', '?')}: {_text(first.get('content'))}"]
        ts = first.get("timestamp")
        if i + 1 < len(messages) and messages[i + 1].get("role") != first.get("role"):
            parts.append(
                f"{messages[i + 1].get('role', '?')}: {_text(messages[i + 1].get('content'))}"
            )
            if ts is None:
                ts = messages[i + 1].get("timestamp")
            i += 2
        else:
            i += 1
        out.append(
            ("\n".join(parts), int(ts) if isinstance(ts, (int, float)) else None)
        )
    return out


def turns_of(
    messages: list[dict[str, Any]], chunk_chars: int
) -> list[tuple[str, int | None]]:
    """One unit per message; an assistant message longer than `chunk_chars`
    is split on paragraph boundaries into parts no longer than that (a
    single paragraph over the limit stays whole). Returns (body, source
    timestamp ms), the same shape `rounds_of` returns."""
    out: list[tuple[str, int | None]] = []
    for m in messages:
        role = m.get("role", "?")
        text = _text(m.get("content"))
        ts = m.get("timestamp")
        ts = int(ts) if isinstance(ts, (int, float)) else None
        if role != "assistant" or len(text) <= chunk_chars:
            out.append((f"{role}: {text}", ts))
            continue
        parts: list[str] = []
        current = ""
        for para in text.split("\n\n"):
            candidate = f"{current}\n\n{para}" if current else para
            if current and len(candidate) > chunk_chars:
                parts.append(current)
                current = para
            else:
                current = candidate
        if current:
            parts.append(current)
        for i, part in enumerate(parts, 1):
            out.append((f"{role} (part {i} of {len(parts)}): {part}", ts))
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


@dataclass
class _UserStore:
    root: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    event_ts: dict[str, int] = field(default_factory=dict)
    session_of: dict[str, str] = field(default_factory=dict)
    seq: dict[str, int] = field(default_factory=dict)
    done_requests: set[str] = field(default_factory=set)
    memories: list[Memory] | None = None

    @property
    def sidecar(self) -> Path:
        return self.root / "aml-sidecar.json"

    def load(self) -> None:
        if self.sidecar.exists():
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
            self.event_ts = {k: int(v) for k, v in data.get("event_ts", {}).items()}
            self.session_of = dict(data.get("session_of", {}))
            self.seq = {k: int(v) for k, v in data.get("seq", {}).items()}
            self.done_requests = set(data.get("done_requests", []))

    def save(self) -> None:
        tmp = self.sidecar.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "event_ts": self.event_ts,
                    "session_of": self.session_of,
                    "seq": self.seq,
                    "done_requests": sorted(self.done_requests),
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(self.sidecar)


FILLS = ("none", "neighbors", "neighbors-recent")
ORDERS = ("rank", "chronological", "session")
ANNOTATIONS = ("none", "age")
TRIMS = ("none", "assistant", "tail")
TRIM_KEEP_WHOLE = 10


def query_terms(query: str) -> set[str]:
    """The query's content words, stemmed the way the engine stems them."""
    return {
        _engine._stem_token(t)
        for t in _engine._tokenize_unstemmed(query)
        if t not in _engine._STOPWORDS and len(t) > 2
    }


def _line_terms(line: str) -> set[str]:
    out: set[str] = set()
    for tok in _engine.tokenize(line):
        out.add(tok)
        if "-" in tok:
            out.update(p for p in tok.split("-") if p)
    return out


def trim_assistant(body: str, terms: set[str], min_chars: int = 0) -> str:
    """Keep the header and the user's words whole; from each assistant turn
    of at least `min_chars` characters keep its first line and every line
    sharing a content word with the query, and mark each dropped run with
    an ellipsis line. A shorter assistant turn is kept whole.

    Assistant replies are most of the served text, and most of it is
    general advice the question does not ask about. The user's own words
    carry the facts about the user, so they are never cut; an assistant
    line the question names survives, which is what single-session-
    assistant questions ask for. `min_chars` spares conversational turns:
    in a two-person dialogue (LoCoMo) the "assistant" is the second
    speaker, whose short turns carry facts as surely as the first's.
    """
    lines = body.split("\n")
    # Length of the assistant turn each line belongs to (0 outside one).
    turn_len = [0] * len(lines)
    start = None
    for i, line in enumerate(lines + ["user: "]):
        if line.startswith(("assistant: ", "user: ")) or i == len(lines):
            if start is not None:
                n = sum(len(x) + 1 for x in lines[start:i])
                for j in range(start, i):
                    turn_len[j] = n
                start = None
            if i < len(lines) and line.startswith("assistant: "):
                start = i
    out: list[str] = []
    in_assistant = False
    first = False
    dropped = False
    for i, line in enumerate(lines):
        if line.startswith("assistant: "):
            in_assistant, first = True, True
        elif line.startswith("user: "):
            in_assistant = False
        keep = (
            not in_assistant
            or first
            or turn_len[i] < min_chars
            or bool(terms & _line_terms(line))
        )
        if keep:
            if dropped:
                out.append("[...]")
                dropped = False
            out.append(line)
            first = False
        elif line.strip():
            dropped = True
    if dropped:
        out.append("[...]")
    return "\n".join(out)


class MemoryService:
    """Add/Search over one bettermemory store per AML user_id.

    `granularity` is the ingest unit: "rounds" (a user message with the
    assistant reply after it, the default) or "turns" (each message on its
    own, long assistant replies split by `turns_of`). It is fixed per store,
    so a store root is built under one granularity only.

    `fill` and `order` are the presentation levers, measured as arms by
    bench/aml/run.py. The engine ranks; these decide what the fixed
    reader is shown within AML's top_k:

      fill   none              only rounds the engine scored
             neighbors         pad the unused slots with the rounds that
                               sit next to a hit in its own session,
                               nearest the best-ranked hits first
             neighbors-recent  then pad what is still free with the most
                               recent rounds
      order  rank              engine order
             chronological     source time, oldest first
             session           sessions in order of their best hit,
                               rounds inside a session in source order
      annotate none            the stored "[date]" header as written
               age             the header also says how many days before
                               the store's most recent conversation the
                               round happened. AML's Search request
                               carries no question date, so "how many
                               weeks ago" is unanswerable from dates
                               alone; the newest conversation is the
                               nearest clock the store has (on
                               LongMemEval-S it is within a day of the
                               question date for 77% of questions)
      serve    the most rounds returned, whatever top_k asks for; fewer
               rounds is less for a fixed reader to wade through
      budget   the most characters returned across all rounds (0 = no
               limit). Ranked rounds are taken in order while they fit;
               the first is always served; a round too long for what is
               left is skipped for a later one that fits. On BEAM-1M a
               hundred whole rounds median 455k characters, past a 128k-
               token answer model's window, so an unbudgeted answer
               prompt fails outright
      trim     none       rounds served whole
               assistant  assistant lines that share no content word with
                          the question are elided (`trim_assistant`)
               tail       the same, below the first TRIM_KEEP_WHOLE served
                          rounds only; the strongest evidence stays whole,
                          since a trim can cut a line the question points at
                          by position ("the 7th item") rather than by word
    """

    def __init__(
        self,
        root: Path,
        *,
        conversational: bool = True,
        granularity: str = "rounds",
        chunk_chars: int = 1500,
        fill: str = "none",
        order: str = "rank",
        annotate: str = "none",
        serve: int = 100,
        budget: int = 0,
        trim: str = "none",
        trim_min_chars: int = 0,
    ) -> None:
        if trim not in TRIMS:
            raise ValueError(f"trim {trim!r}")
        self.trim = trim
        self.trim_min_chars = trim_min_chars
        if fill not in FILLS or order not in ORDERS or annotate not in ANNOTATIONS:
            raise ValueError(f"fill {fill!r} / order {order!r} / annotate {annotate!r}")
        self.root = root
        self.conversational = conversational
        if granularity not in ("rounds", "turns"):
            raise ValueError(f"granularity {granularity!r}")
        self.granularity = granularity
        self.chunk_chars = chunk_chars
        self.fill = fill
        self.order = order
        self.annotate = annotate
        self.serve = serve
        self.budget = budget
        self._stores: dict[str, _UserStore] = {}
        self._guard = threading.Lock()

    def _user(self, user_id: str) -> _UserStore:
        key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        with self._guard:
            us = self._stores.get(key)
            if us is None:
                us = _UserStore(root=self.root / key)
                us.root.mkdir(parents=True, exist_ok=True)
                us.load()
                self._stores[key] = us
            return us

    def add(
        self,
        request_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        session_id: str = "",
    ) -> int:
        us = self._user(user_id)
        with us.lock:
            if request_id in us.done_requests:
                return 0
            store = Store(us.root)
            n = 0
            units = (
                turns_of(messages, self.chunk_chars)
                if self.granularity == "turns"
                else rounds_of(messages)
            )
            for body, ts in units:
                stamp = _fmt_ts(ts)
                content = f"[{stamp}]\n{body}" if stamp else body
                memory = store.write(content=content, scopes=SCOPE)
                if ts is not None:
                    us.event_ts[memory.id] = ts
                us.session_of[memory.id] = session_id
                us.seq[memory.id] = len(us.seq)
                n += 1
            us.done_requests.add(request_id)
            us.save()
            us.memories = None
            return n

    def search(self, user_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
        us = self._user(user_id)
        with us.lock:
            if us.memories is None:
                us.memories = (
                    Store(us.root).load_all() if any(us.root.glob("*.md")) else []
                )
            memories = us.memories
            event_ts = dict(us.event_ts)
            session_of = dict(us.session_of)
            seq = dict(us.seq)
        if not memories:
            return []
        now = (
            datetime.fromtimestamp(max(event_ts.values()) / 1000, tz=timezone.utc)
            if event_ts
            else None
        )
        top_k = min(top_k, self.serve)
        hits = run_search(
            memories,
            query,
            max_results=top_k,
            mode="hybrid",
            conversational=self.conversational,
            now=now,
        )
        by_id = {m.id: m for m in memories}
        chosen = [h.id for h in hits if h.id in by_id]
        scores = {h.id: float(h.score) for h in hits}
        chosen = self._fill(chosen, top_k, by_id, event_ts, session_of, seq)
        chosen = self._order(chosen, event_ts, session_of, seq)
        latest = max(event_ts.values()) if event_ts else None
        terms = query_terms(query) if self.trim != "none" else set()
        out: list[dict[str, Any]] = []
        used = 0
        for rank, mid in enumerate(chosen):
            m = by_id[mid]
            ts = event_ts.get(mid)
            content = self._present(m.body, ts, latest, terms, rank)
            if self.budget and out and used + len(content) > self.budget:
                continue
            used += len(content)
            out.append(
                {
                    "id": mid,
                    "content": content,
                    "score": scores.get(mid, 0.0),
                    **(
                        {
                            "created_at": datetime.fromtimestamp(
                                ts / 1000, tz=timezone.utc
                            ).isoformat()
                        }
                        if ts is not None
                        else {}
                    ),
                }
            )
        return out

    def _fill(
        self,
        chosen: list[str],
        top_k: int,
        by_id: dict[str, Memory],
        event_ts: dict[str, int],
        session_of: dict[str, str],
        seq: dict[str, int],
    ) -> list[str]:
        if self.fill == "none" or len(chosen) >= top_k or not seq:
            return chosen
        taken = set(chosen)
        out = list(chosen)
        order_in_session: dict[str, list[str]] = {}
        for mid in sorted(seq, key=seq.__getitem__):
            if mid in by_id:
                order_in_session.setdefault(session_of.get(mid, ""), []).append(mid)
        position = {
            mid: i
            for rounds in order_in_session.values()
            for i, mid in enumerate(rounds)
        }
        # Widen around every hit one step at a time, best hit first, so the
        # closest context of the strongest evidence is taken before the
        # farther context of weaker evidence.
        for radius in range(1, 64):
            grew = False
            for mid in chosen:
                rounds = order_in_session.get(session_of.get(mid, ""), [])
                i = position.get(mid)
                if i is None:
                    continue
                for j in (i - radius, i + radius):
                    if 0 <= j < len(rounds) and rounds[j] not in taken:
                        taken.add(rounds[j])
                        out.append(rounds[j])
                        grew = True
                        if len(out) >= top_k:
                            return out
            if not grew:
                break
        if self.fill == "neighbors-recent":
            rest = sorted(
                (mid for mid in by_id if mid not in taken),
                key=lambda mid: (event_ts.get(mid, 0), seq.get(mid, 0)),
                reverse=True,
            )
            out.extend(rest[: top_k - len(out)])
        return out

    def _order(
        self,
        chosen: list[str],
        event_ts: dict[str, int],
        session_of: dict[str, str],
        seq: dict[str, int],
    ) -> list[str]:
        if self.order == "rank":
            return chosen
        if self.order == "chronological":
            return sorted(
                chosen, key=lambda mid: (event_ts.get(mid, 0), seq.get(mid, 0))
            )
        best: dict[str, int] = {}
        for rank, mid in enumerate(chosen):
            best.setdefault(session_of.get(mid, ""), rank)
        return sorted(
            chosen,
            key=lambda mid: (best[session_of.get(mid, "")], seq.get(mid, 0)),
        )

    def sessions_for(self, user_id: str, ids: list[str]) -> list[str]:
        """Source session of each id; local diagnostics only, never served."""
        us = self._user(user_id)
        with us.lock:
            return [us.session_of.get(i, "") for i in ids]

    def _present(
        self,
        body: str,
        ts: int | None,
        latest: int | None,
        terms: set[str],
        rank: int,
    ) -> str:
        if self.trim == "assistant" or (
            self.trim == "tail" and rank >= TRIM_KEEP_WHOLE
        ):
            body = trim_assistant(body, terms, self.trim_min_chars)
        if self.annotate == "none" or ts is None or latest is None:
            return body
        stamp = _fmt_ts(ts)
        head = f"[{stamp}]\n"
        if not body.startswith(head):
            return body
        days = (latest - ts) // 86_400_000
        latest_day = datetime.fromtimestamp(latest / 1000, tz=timezone.utc).strftime(
            "%Y/%m/%d (%a)"
        )
        when = (
            "the same day as the most recent conversation"
            if days == 0
            else f"{days} day{'s' if days != 1 else ''} before the most recent "
            f"conversation on {latest_day}"
        )
        return f"[{stamp}; {when}]\n" + body[len(head) :]
