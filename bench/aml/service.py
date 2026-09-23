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

A ROUND SPLIT ACROSS TWO ADDS. AML cuts a session into Add requests at
20 messages or 2,000 words, so a chunk can end on a user message whose
reply opens the next chunk (on BEAM-1M about 35% of rounds, LongMemEval-S
7%, LoCoMo 2%). Pairing each chunk on its own would store the question
and its answer as two memories, which is not what the whole-session
harness measured. So a chunk that ends on an unpaired message records it
as the session's tail; it is written, and so searchable at once as the
contract requires, and when the next chunk of the same session opens
with the other role, the tail is paired with it and the lone copy is
hidden from Search. The rounds that result are exactly the rounds
`rounds_of` makes from the whole session.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aml import distill
from bettermemory import search as _engine
from bettermemory.models import Memory
from bettermemory.search import search as run_search
from bettermemory.store import Store

SCOPE = ["aml"]

# The serving configuration the live endpoint runs: at most this many
# characters of ranked rounds per Search. AML's own answer step keeps a
# 117,760-token prefix of what Search returns; 90,000 characters read best
# of the budgets measured (bench/aml/REPORT.md cites each run), and a
# larger budget read worse on PersonaMem v1 and LongMemEval-S.
SERVING_BUDGET_CHARS = 90_000
# How many stores keep their parsed memories in RAM between Searches.
LOADED_STORES = 64


# SEARCH COST. The engine tokenizes every candidate once per search, and
# builds a query-biased snippet for every hit it returns; on a 400-round
# store those two are ~88% of a Search, and on the live host a Search is
# CPU-bound on one core. The adapter's memories never change after they
# are written, so each store keeps its candidates' token streams across
# Searches, and the snippet (a display field this adapter never serves)
# is skipped. Both are active only on a thread inside `MemoryService.search`
# (a thread-local the adapter sets), so every other caller of the engine,
# including the tests that share this process, runs the unmodified code.
# Ranking is unchanged: a test asserts byte-identical Search results with
# and without the cache.
_adapter_call = threading.local()
_engine_memory_tokens = _engine._memory_tokens
_engine_snippet = _engine._query_biased_snippet


def _memory_tokens_cached(memory: Memory) -> Any:
    cache: dict[str, Any] | None = getattr(_adapter_call, "tokens", None)
    if cache is None:
        return _engine_memory_tokens(memory)
    toks = cache.get(memory.id)
    if toks is None:
        toks = cache[memory.id] = _engine_memory_tokens(memory)
    return toks


def _snippet_unless_adapter(body: str, matched: list[str], max_chars: int = 200) -> str:
    if getattr(_adapter_call, "tokens", None) is not None:
        return ""
    return _engine_snippet(body, matched, max_chars)


_engine._memory_tokens = _memory_tokens_cached
_engine._query_biased_snippet = _snippet_unless_adapter


def _utc_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _fmt_ts(ms: int | None) -> str | None:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y/%m/%d (%a) %H:%M")


DIALOGUE_ROLES = frozenset({"user", "assistant"})


def _pairs(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two adjacent messages form a round when both are dialogue turns of
    different roles. Any other role (a "system" persona prompt, a tool
    result) stands alone: pairing it with the next turn would shift every
    later round of the session by one message."""
    ra, rb = a.get("role"), b.get("role")
    return ra in DIALOGUE_ROLES and rb in DIALOGUE_ROLES and ra != rb


def round_spans(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Greedy pairing: a message and the next one when `_pairs` holds,
    else the message alone. Returns [start, end) index spans."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        pair = i + 1 < len(messages) and _pairs(messages[i], messages[i + 1])
        spans.append((i, i + 2 if pair else i + 1))
        i = spans[-1][1]
    return spans


def rounds_of(messages: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """Pair messages into rounds; returns (body, source timestamp ms)."""
    out: list[tuple[str, int | None]] = []
    for start, end in round_spans(messages):
        group = messages[start:end]
        body = "\n".join(
            f"{m.get('role', '?')}: {_text(m.get('content'))}" for m in group
        )
        ts = next(
            (m["timestamp"] for m in group if m.get("timestamp") is not None), None
        )
        out.append((body, int(ts) if isinstance(ts, (int, float)) else None))
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
    # session_id -> {"id": memory id, "message": the unpaired message}
    tails: dict[str, dict[str, Any]] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    memories: list[Memory] | None = None
    # memory id -> the engine's token streams for it; dropped with `memories`
    tokens: dict[str, Any] = field(default_factory=dict)
    # distilled units (declaration E1): id -> {"ts", "kind", "round"}
    unit_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    unit_memories: list[Memory] | None = None

    @property
    def sidecar(self) -> Path:
        return self.root / "aml-sidecar.json"

    @property
    def units_root(self) -> Path:
        """A sibling of the rounds store, so neither store reads the other."""
        return self.root.parent / f"{self.root.name}.units"

    def load(self) -> None:
        if self.sidecar.exists():
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
            self.event_ts = {k: int(v) for k, v in data.get("event_ts", {}).items()}
            self.session_of = dict(data.get("session_of", {}))
            self.seq = {k: int(v) for k, v in data.get("seq", {}).items()}
            self.done_requests = set(data.get("done_requests", []))
            self.tails = dict(data.get("tails", {}))
            self.hidden = set(data.get("hidden", []))
            self.unit_meta = dict(data.get("unit_meta", {}))

    def save(self) -> None:
        tmp = self.sidecar.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "event_ts": self.event_ts,
                    "session_of": self.session_of,
                    "seq": self.seq,
                    "done_requests": sorted(self.done_requests),
                    "tails": self.tails,
                    "hidden": sorted(self.hidden),
                    "unit_meta": self.unit_meta,
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(self.sidecar)


FILLS = ("none", "neighbors", "neighbors-recent")
ORDERS = ("rank", "chronological", "session")
ANNOTATIONS = ("none", "age")
TRIMS = ("none", "assistant", "tail")
SHEETS = ("none", "units", "units-age")
_UNIT_STAMP = re.compile(r"^\[[^\]]*\]\s*")
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
        cache_tokens: bool = True,
        sheet: str = "none",
        sheet_chars: int = 8000,
        sheet_units: int = 40,
        sheet_prefs: int = 15,
        sheet_last: bool = False,
    ) -> None:
        self.sheet_last = sheet_last
        if sheet not in SHEETS:
            raise ValueError(f"sheet {sheet!r}")
        self.sheet = sheet
        self.sheet_chars = sheet_chars
        self.sheet_units = sheet_units
        self.sheet_prefs = sheet_prefs
        self.cache_tokens = cache_tokens
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
        self._loaded: OrderedDict[str, None] = OrderedDict()
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
            if self.granularity == "turns":
                units = turns_of(messages, self.chunk_chars)
                lone_tail = None
            else:
                tail = us.tails.pop(session_id, None)
                merged_tail = False
                if (
                    tail is not None
                    and messages
                    and _pairs(tail["message"], messages[0])
                ):
                    us.hidden.add(tail["id"])
                    messages = [tail["message"], *messages]
                    merged_tail = True
                spans = round_spans(messages)
                units = rounds_of(messages)
                lone_tail = (
                    messages[-1] if spans and spans[-1][1] - spans[-1][0] == 1 else None
                )
            last_id = None
            round_ids: list[str] = []
            for body, ts in units:
                stamp = _fmt_ts(ts)
                content = f"[{stamp}]\n{body}" if stamp else body
                memory = store.write(content=content, scopes=SCOPE)
                if ts is not None:
                    us.event_ts[memory.id] = ts
                us.session_of[memory.id] = session_id
                us.seq[memory.id] = len(us.seq)
                last_id = memory.id
                round_ids.append(memory.id)
                n += 1
            if lone_tail is not None and last_id is not None:
                us.tails[session_id] = {"id": last_id, "message": lone_tail}
            if self.sheet != "none" and self.granularity == "rounds":
                self._write_units(us, messages, spans, round_ids, merged_tail)
            us.done_requests.add(request_id)
            us.save()
            us.memories = None
            us.unit_memories = None
            us.tokens = {}
            return n

    def _write_units(
        self,
        us: _UserStore,
        messages: list[dict[str, Any]],
        spans: list[tuple[int, int]],
        round_ids: list[str],
        merged_tail: bool,
    ) -> None:
        """Distil the chunk's user turns into dated units (declaration E1).
        A tail re-joined from the previous chunk was distilled when it first
        arrived, so it is skipped here."""
        ustore = Store(us.units_root)
        round_of = {
            i: rid for (a, b), rid in zip(spans, round_ids) for i in range(a, b)
        }
        for i, m in enumerate(messages):
            if merged_tail and i == 0:
                continue
            ts = m.get("timestamp")
            ts = int(ts) if isinstance(ts, (int, float)) else None
            for kind, text in distill.units_of(
                str(m.get("role", "")), _text(m.get("content")), ts
            ):
                stamp = _fmt_ts(ts)
                memory = ustore.write(
                    content=f"[{stamp}] {text}" if stamp else text, scopes=SCOPE
                )
                us.unit_meta[memory.id] = {
                    "ts": ts,
                    "kind": kind,
                    "round": round_of.get(i, ""),
                }

    def search(self, user_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
        us = self._user(user_id)
        with us.lock:
            if us.memories is None:
                loaded = Store(us.root).load_all() if any(us.root.glob("*.md")) else []
                us.memories = [m for m in loaded if m.id not in us.hidden]
                us.tokens = {}
            memories = us.memories
            tokens = us.tokens
            event_ts = dict(us.event_ts)
            session_of = dict(us.session_of)
            seq = dict(us.seq)
        self._touch(us)
        if not memories:
            return []
        now = (
            datetime.fromtimestamp(max(event_ts.values()) / 1000, tz=timezone.utc)
            if event_ts
            else None
        )
        top_k = min(top_k, self.serve)
        _adapter_call.tokens = tokens if self.cache_tokens else None
        try:
            hits = run_search(
                memories,
                query,
                max_results=top_k,
                mode="hybrid",
                conversational=self.conversational,
                now=now,
            )
        finally:
            _adapter_call.tokens = None
        by_id = {m.id: m for m in memories}
        chosen = [h.id for h in hits if h.id in by_id]
        scores = {h.id: float(h.score) for h in hits}
        chosen = self._fill(chosen, top_k, by_id, event_ts, session_of, seq)
        chosen = self._order(chosen, event_ts, session_of, seq)
        latest = max(event_ts.values()) if event_ts else None
        terms = query_terms(query) if self.trim != "none" else set()
        out: list[dict[str, Any]] = []
        used = 0
        sheet = None
        if self.sheet != "none":
            sheet = self._sheet(us, query, now, tokens, latest)
            if sheet is not None:
                used += len(sheet["content"])
                if not self.sheet_last:
                    out.append(sheet)
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
        if sheet is not None and self.sheet_last:
            # nearest the question, where a reader weighs context most
            out.append(sheet)
        return out

    def sheet_unit_ids(
        self, us: _UserStore, query: str, now: datetime | None, tokens: dict[str, Any]
    ) -> list[str]:
        """The units a Search puts on the sheet, most relevant first: the
        engine's ranking over the user's units, and, for a question asking
        for suggestions, the user's stated preferences even where they share
        no word with it (the preference that shapes a recommendation rarely
        repeats the question's words)."""
        with us.lock:
            if us.unit_memories is None:
                root = us.units_root
                us.unit_memories = (
                    Store(root).load_all()
                    if root.exists() and any(root.glob("*.md"))
                    else []
                )
            units = us.unit_memories
            meta = dict(us.unit_meta)
        if not units:
            return []
        _adapter_call.tokens = tokens if self.cache_tokens else None
        try:
            hits = run_search(
                units,
                query,
                max_results=self.sheet_units,
                mode="hybrid",
                conversational=self.conversational,
                now=now,
            )
        finally:
            _adapter_call.tokens = None
        chosen = [h.id for h in hits if h.score > 0]
        if distill.wants_suggestions(query):
            prefs = [
                m.id
                for m in sorted(
                    units, key=lambda m: meta.get(m.id, {}).get("ts") or 0, reverse=True
                )
                if meta.get(m.id, {}).get("kind") == "preference" and m.id not in chosen
            ]
            chosen += prefs[: self.sheet_prefs]
        return chosen

    def _sheet(
        self,
        us: _UserStore,
        query: str,
        now: datetime | None,
        tokens: dict[str, Any],
        latest_ms: int | None = None,
    ) -> dict[str, Any] | None:
        ids = self.sheet_unit_ids(us, query, now, tokens)
        if not ids:
            return None
        by_id = {m.id: m for m in us.unit_memories or []}
        meta = us.unit_meta
        picked: list[str] = []
        size = 0
        for uid in ids:
            body = by_id[uid].body.strip()
            if size + len(body) + 3 > self.sheet_chars and picked:
                continue
            picked.append(uid)
            size += len(body) + 3
        picked.sort(key=lambda uid: (meta.get(uid, {}).get("ts") or 0, uid))
        latest = max((meta.get(uid, {}).get("ts") or 0 for uid in picked), default=0)
        if self.sheet == "units-age" and latest_ms:
            # Dates as distances from the store's newest conversation, the
            # nearest clock the store has (AML's Search carries no question
            # date): "weeks ago" becomes a division, "days between" a
            # subtraction of two integers.
            head = (
                "Things the user said, oldest first. The latest conversation "
                f"was on {_fmt_ts(latest_ms)[:16]}.\n"
            )
            lines = []
            for uid in picked:
                ts = meta.get(uid, {}).get("ts")
                text = _UNIT_STAMP.sub("", by_id[uid].body.strip(), count=1)
                if ts:
                    days = (_utc_day(latest_ms) - _utc_day(ts)).days
                    when = (
                        "the day of the latest conversation"
                        if days == 0
                        else f"{days} day{'s' if days != 1 else ''} before the latest conversation"
                    )
                    lines.append(f"- {_fmt_ts(ts)[:16]}, {when}: {text}")
                else:
                    lines.append(f"- {text}")
            content = head + "\n".join(lines)
        else:
            lines = [f"- {by_id[uid].body.strip()}" for uid in picked]
            content = "Things the user said, oldest first:\n" + "\n".join(lines)
        return {
            "id": "sheet",
            "content": content,
            "score": 1e6,
            **(
                {
                    "created_at": datetime.fromtimestamp(
                        latest / 1000, tz=timezone.utc
                    ).isoformat()
                }
                if latest
                else {}
            ),
        }

    def _touch(self, us: _UserStore) -> None:
        """Keep parsed memories for the LOADED_STORES most recently searched
        stores only; a full evaluation opens thousands."""
        with self._guard:
            key = us.root.name
            self._loaded[key] = None
            self._loaded.move_to_end(key)
            while len(self._loaded) > LOADED_STORES:
                old, _ = self._loaded.popitem(last=False)
                evicted = self._stores.get(old)
                if evicted is not None:
                    evicted.memories = None
                    evicted.unit_memories = None
                    evicted.tokens = {}

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
