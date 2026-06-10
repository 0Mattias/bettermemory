"""Write-reflex closure: a review-gated queue of candidate memories.

The model under-writes durable content during head-down work — the
documented writing-reflex gap (`attribution.py` exists because of it;
`memory_helped_rate` read 0% in dogfood). This module is the *capture*
half of the self-improving loop; the *curate* half is
`consolidate.run_auto_consolidate`. The Stop hook scans the just-ended
exchange for durable-looking statements the model didn't write and
appends them here as inert proposals. The model reviews them via the
`memory_proposals` MCP tool and either accepts one (→ a normal memory
write, through the usual audit surface) or dismisses it.

Two invariants make this safe to run unattended:

1. **Nothing here ever writes to the memory store.** Proposals are inert
   JSON until a human/model explicitly accepts one — the same
   "stage, then confirm" discipline the `user-inference` write path uses.
   A bad proposal costs one dismissal, never a bad memory.
2. **Generation-agnostic.** The heuristic extractor below is v1
   (cheap, no LLM, runs in the turn-end hook without blocking it). The
   queue + review surface are deliberately decoupled from how proposals
   are produced, so an LLM-backed pass (`consolidate --from-transcript`'s
   `propose_new`) can append to the same queue later — e.g. from a
   detached background process — with no change to the review side.

On-disk: ``<root>/.write_proposals.jsonl`` — one JSON object per line,
0o600, atomically rewritten under a per-file ``flock`` on every mutation
(the hook appends while the `memory_proposals` handler removes; the lock
serialises that cross-process read-modify-write).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ._fsutil import atomic_write_bytes, flock_excl
from .durability import find_transient_markers
from .models import generate_ulid, utcnow

log = logging.getLogger("bettermemory.proposals")

PROPOSALS_FILENAME = ".write_proposals.jsonl"

# Minimum shape of a candidate sentence. Below these it's too short to be
# a self-contained durable statement worth proposing. Mirrors the floor
# `attribution._candidate_sentences` uses for the symmetric problem
# (deciding a sentence is distinctive enough to attribute on).
_MIN_CHARS = 30
_MIN_TOKENS = 6

# Explicit capture requests are exempt from the full floor — "Remember
# that I use zsh." is a self-contained instruction whose marker text alone
# eats half the char budget. A small token guard still rejects degenerate
# fragments ("Remember that.").
_MIN_TOKENS_EXPLICIT = 4

# Default cap on proposals produced from a single exchange — capture
# wants the strongest one or two, not a transcript dump. The queue-level
# `max_pending` cap (config) bounds the total across turns.
_MAX_PER_EXCHANGE = 3

# Sentence splitter: terminal punctuation followed by whitespace, or a
# newline run. Trailing-space requirement preserves "v1.6" / "3.11" /
# "src/foo.py" (the dot is followed by a digit/letter, not space).
# Fixed-width negative lookbehinds keep "e.g." / "i.e." mid-sentence from
# fragmenting the sentence (a truncated body is a noisy proposal — the
# module's named expensive failure).
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+|\n+")

# Hard-wrap repair: join a SINGLE newline into a space only when the prior
# char isn't terminal punctuation / a colon / another newline (so blank-line
# runs and punctuation-terminated lines keep their boundary) and the next
# line doesn't open with a list/quote marker (so bulleted lists stay split
# for _LIST_PREFIX_RE + the ^-anchored command reject to see). Without this,
# a hard-wrapped sentence is split mid-clause and the truncated dangling
# fragment gets proposed verbatim as the memory body.
_HARD_WRAP_RE = re.compile(r"(?<![.!?:\n])\n(?=\S)(?![-*+•>])(?!\d{1,3}[.)]\s)")

# Leading markdown list/quote markers ("- ", "* ", "1. ", "> ", possibly
# stacked). Stripped from each candidate before any gate runs, so the
# ^-anchored question/command reject sees the real sentence start and
# proposal bodies don't carry the bullet prefix.
_LIST_PREFIX_RE = re.compile(r"^(?:[-*+•]\s+|\d{1,3}[.)]\s+|>\s+)+")

# Typographic single quotes (U+2018/U+2019) → ASCII apostrophe. macOS and
# iOS keyboards substitute smart quotes by default, so contractions arrive
# as "Let’s" / "I’m" / "Don’t" — and every contraction-aware pattern below
# (`_EXPLICIT_MARKERS`' "don't forget", `_PREFERENCE_RE`'s "i'?m using",
# `_QUESTION_OR_COMMAND_RE`'s "let'?s" and its negated contractions) is
# written against the ASCII apostrophe. Applied ONCE, at the top of
# `extract_proposals` before any pattern sees the text, so no pattern needs
# a smart-quote variant and dedup-by-source_excerpt can't treat the smart
# and ASCII spellings of one sentence as distinct entries.
_SMART_APOSTROPHES = str.maketrans({"‘": "'", "’": "'"})

# Explicit "remember this" intent. High precision — when the user says
# any of these, they are asking to be remembered, so we propose even if
# the sentence also looks like a question/command ("can you remember
# that…"). Matched word-boundary-anchored against the lowercased sentence
# (see _EXPLICIT_MARKER_RE) so "remember i" can't prefix-match "remember
# if"/"remember it". Deliberately absent: bare per-turn connectives like
# "note that" / "make sure to" / "for the future" — ubiquitous instruction
# and roadmap prose with no remember-intent, which violated the
# high-precision premise above.
_EXPLICIT_MARKERS: tuple[str, ...] = (
    "remember that",
    "remember to",
    "remember i",
    "remember we",
    "remember this",
    "remember:",
    "keep in mind",
    "for future reference",
    "don't forget",
    "do not forget",
    "from now on",
    "going forward",
    "always remember",
)

# \b-anchor each marker edge that is a word character (a trailing ":" is
# its own boundary). Markers are lowercase; callers match against the
# lowercased sentence.
_EXPLICIT_MARKER_RE = re.compile(
    "|".join(
        ("\\b" if m[0].isalnum() else "")
        + re.escape(m)
        + ("\\b" if m[-1].isalnum() else "")
        for m in _EXPLICIT_MARKERS
    )
)

# Retrieval interrogatives ("Do you remember if/that …?") ask what is
# already stored and assert nothing durable — rejected BEFORE the explicit
# marker override is honored, since the marker's "asking to be remembered"
# premise is false for them. "Can you remember that…" (capture intent,
# test-pinned) opens with "can you" and is untouched.
_RETRIEVAL_QUESTION_RE = re.compile(
    r"^(?:do|does|did)\s+you\s+(?:still\s+)?remember\b", re.IGNORECASE
)

# First-person preference / setup declarations — the canonical
# memory-worthy content a user states about themselves or their project.
# Word-boundary anchored on BOTH sides so "my" doesn't fire inside
# "myself" and past-tense narration ("I wanted", "We used") doesn't fire
# the present-tense branch. "i (want|need)" excludes delegations to the
# assistant ("I want you to refactor…" is a task request — contract (c)).
# The bare possessives are anchored to sentence start AND a nearby stative
# verb: mid-sentence "my"/"our" is how imperatives ("Fix the bug in my
# parser…") and pasted third-party text leak in as user-inference facts.
_PREFERENCE_RE = re.compile(
    r"\b(i (?:prefer|like|love|hate|avoid|always|never|usually|use)\b"
    r"|i (?:want|need)\b(?!\s+you\b)"
    r"|i(?:'?m| am) using|i(?:'?ve| have) been using"
    r"|we (?:use|prefer|avoid|always|never)\b"
    r"|we(?:'?re| are) using|we(?:'?ve| have) been using"
    r"|^(?:my|our)\s+(?:\w+\s+){0,4}?(?:is|are|was|were|prefers?|uses?|runs?|lives)\b)",
    re.IGNORECASE,
)

# Sentences that open like a question or a task-request to the assistant
# are not durable facts. Rejected UNLESS an explicit marker fires (an
# explicit "remember…" request overrides). The wh-words carry a trailing
# \b (so "However,"/"Whenever" don't match bare "how"/"when"); the
# auxiliary alternatives keep the house trailing-space convention,
# including the negated contractions ("Shouldn't I use…").
_QUESTION_OR_COMMAND_RE = re.compile(
    r"^(can you|could you|would you|will you|please|let'?s|"
    r"(?:what|whats|what'?s|how|why|when|where|which|who)\b|"
    r"is |are |do |does |did |should |can |could |"
    r"isn'?t |aren'?t |don'?t |doesn'?t |didn'?t |"
    r"shouldn'?t |couldn'?t |wouldn'?t |can'?t |won'?t )",
    re.IGNORECASE,
)

# Hypothetical/conditional protases ("If I use X…", "Suppose I always…")
# describe a scenario, not a durable preference or fact. Sentence-initial
# only — mid-sentence conditionals ("I always run black if the file is
# Python.") still propose — and overridable by an explicit marker,
# consistent with the existing "explicit wins" rule.
_HYPOTHETICAL_RE = re.compile(
    r"^(if|suppose|supposing|imagine|assuming|assume|what if|hypothetically|in case)\b",
    re.IGNORECASE,
)


@dataclass
class Proposal:
    """One candidate memory awaiting review. Inert until accepted.

    - ``id``: ULID, the handle the `memory_proposals` tool acts on.
    - ``body``: the proposed memory body (v1: the user's sentence verbatim;
      the model may refine it on accept).
    - ``source_excerpt``: the transcript sentence this came from —
      provenance, mirroring `consolidate --from-transcript`'s stamping.
    - ``suggested_category``: heuristic guess (``user-inference`` for a
      first-person preference, else ``fact``). The model may override on
      accept; ``user-inference`` proposals are exactly the tier that
      needs human confirmation, which the review step provides.
    - ``created``: ISO-8601 capture time.
    """

    id: str
    body: str
    source_excerpt: str
    suggested_category: str
    created: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "body": self.body,
            "source_excerpt": self.source_excerpt,
            "suggested_category": self.suggested_category,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Proposal":
        return cls(
            id=str(raw["id"]),
            body=str(raw["body"]),
            source_excerpt=str(raw.get("source_excerpt", "")),
            suggested_category=str(raw.get("suggested_category", "fact")),
            created=str(raw.get("created", "")),
        )


@dataclass
class ProposalQueue:
    """The on-disk proposal queue rooted at a memory store directory.

    Cheap to construct (no I/O until a method is called), mirroring
    `EpisodeStore`. All mutations are read-modify-write under a per-file
    ``flock`` so the hook (appending) and the `memory_proposals` handler
    (removing) can't lose each other's updates across processes.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / PROPOSALS_FILENAME

    def load(self) -> list[Proposal]:
        """All queued proposals, oldest first. Skips malformed lines
        defensively — one bad row shouldn't blind the rest of the queue
        (same discipline as `Store.load_all` / `iter_events`)."""
        path = self.path
        if not path.exists():
            return []
        out: list[Proposal] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(raw, dict) or "id" not in raw or "body" not in raw:
                continue
            try:
                out.append(Proposal.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def append(self, proposals: list[Proposal]) -> None:
        """Append proposals under the per-file lock. No-op for an empty
        list so the lock + rewrite are skipped on the common quiet turn."""
        if not proposals:
            return
        with flock_excl(self.path):
            current = self.load()
            current.extend(proposals)
            self._write_all_locked(current)

    def append_within_cap(
        self, candidates: list[Proposal], *, max_pending: int
    ) -> list[Proposal]:
        """Append as many of `candidates` as fit under `max_pending`, deduped
        against what's already queued by ``source_excerpt``. The room check,
        dedup, and write all run inside the per-file ``flock`` so two Stop
        hooks racing on a shared store root can't each size `room` against the
        same stale pre-lock snapshot and overshoot the cap (or double-queue
        the same sentence). Returns the proposals actually appended."""
        if not candidates:
            return []
        with flock_excl(self.path):
            current = self.load()
            room = max_pending - len(current)
            if room <= 0:
                return []
            # Dedup against the queue AND within this batch: a user repeating
            # the same durable sentence twice in one exchange yields two
            # candidates with identical source_excerpt, and updating `seen` as
            # each is admitted keeps the second from also being queued (the
            # prior `[:room]`-sliced comprehension only deduped against the
            # existing queue, contradicting the "can't double-queue the same
            # sentence" guarantee in this method's docstring).
            seen = {p.source_excerpt for p in current}
            fresh: list[Proposal] = []
            for c in candidates:
                if c.source_excerpt in seen:
                    continue
                seen.add(c.source_excerpt)
                fresh.append(c)
                if len(fresh) >= room:
                    break
            if not fresh:
                return []
            self._write_all_locked(current + fresh)
            return fresh

    def remove(self, proposal_id: str) -> Proposal | None:
        """Drop one proposal by id under the lock. Returns the removed
        proposal, or None if no proposal had that id."""
        with flock_excl(self.path):
            current = self.load()
            kept: list[Proposal] = []
            removed: Proposal | None = None
            for p in current:
                if removed is None and p.id == proposal_id:
                    removed = p
                    continue
                kept.append(p)
            if removed is not None:
                self._write_all_locked(kept)
            return removed

    def _write_all_locked(self, proposals: list[Proposal]) -> None:
        """Atomically replace the queue file with `proposals`. Caller
        must already hold the flock. An empty queue writes an empty file
        rather than unlinking, so the path stays stable for the lock."""
        body = "".join(
            json.dumps(p.to_dict(), separators=(",", ":")) + "\n" for p in proposals
        )
        atomic_write_bytes(self.path, body.encode("utf-8"), mode_before_rename=0o600)


def _iter_candidate_sentences(text: str) -> list[tuple[str, bool]]:
    """Split `text` into trimmed ``(sentence, is_explicit)`` pairs clearing
    the length floor.

    `is_explicit` is computed here (after list-prefix stripping, before the
    floor) because explicit capture requests are exempt from the full floor
    — the floor is the one gate the documented "explicit marker wins"
    override would otherwise never reach.
    """
    text = _HARD_WRAP_RE.sub(" ", text)
    out: list[tuple[str, bool]] = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        s = _LIST_PREFIX_RE.sub("", raw.strip()).strip()
        if not s:
            continue
        is_explicit = bool(_EXPLICIT_MARKER_RE.search(s.lower()))
        if is_explicit:
            if len(s.split()) < _MIN_TOKENS_EXPLICIT:
                continue
        else:
            if len(s) < _MIN_CHARS:
                continue
            if len(s.split()) < _MIN_TOKENS:
                continue
        out.append((s, is_explicit))
    return out


def extract_proposals(
    user_text: str | None,
    *,
    now: datetime | None = None,
    max_proposals: int = _MAX_PER_EXCHANGE,
    exclude_excerpts: frozenset[str] = frozenset(),
) -> list[Proposal]:
    """Conservative, no-LLM extraction of durable-looking statements from
    a turn's USER message.

    Precision over recall by design — a noisy proposal trains the model
    to ignore the review surface, and the review gate already makes a
    *missed* capture cheaper than a *bad* one. A sentence is proposed
    only when it (a) clears the length floor (a reduced degenerate guard
    for explicit-marker sentences), (b) carries an explicit "remember
    this" marker OR a first-person preference/setup pattern, (c) is not a
    question, a hypothetical, or a task-request to the assistant (unless
    an explicit marker overrides — retrieval questions like "Do you
    remember if…?" are rejected outright), and (d) trips no
    transient-state marker (`durability.find_transient_markers`) —
    run-local state is the episode tier's job, not a durable memory.
    Explicit markers get no exemption from (d), but their drop is logged
    at WARNING so the swallowed capture request stays observable.

    `exclude_excerpts`: sentences already queued (matched verbatim) are
    skipped BEFORE they count against `max_proposals`, so a recurring
    preamble can't occupy every slot and mask new durable statements later
    in the message. Authoritative dedup still happens under the queue lock
    (`ProposalQueue.append_within_cap`); this is only a budget hint.

    Only the user's own words are mined: they are the highest-value,
    most-durable, most-extractable source (preferences and facts the user
    states about themselves / their project).
    """
    if not user_text or not user_text.strip():
        return []
    # Normalize smart quotes before ANY pattern — including the
    # explicit-marker scan inside `_iter_candidate_sentences` — sees the
    # text (see _SMART_APOSTROPHES).
    user_text = user_text.translate(_SMART_APOSTROPHES)
    when = (now or utcnow()).isoformat().replace("+00:00", "Z")
    out: list[Proposal] = []
    for sentence, is_explicit in _iter_candidate_sentences(user_text):
        if sentence in exclude_excerpts:
            continue
        # A retrieval interrogative asks what's stored, asserts nothing —
        # rejected even when "remember that/i…" makes it look explicit.
        if _RETRIEVAL_QUESTION_RE.match(sentence):
            continue
        is_preference = bool(_PREFERENCE_RE.search(sentence))
        if not (is_explicit or is_preference):
            continue
        # Questions / task-requests / hypotheticals aren't durable facts —
        # but an explicit "remember…" request is exactly a capture
        # instruction, so it wins. rstrip("!") counts "?!" as a question.
        if not is_explicit:
            if sentence.rstrip().rstrip("!").endswith("?"):
                continue
            if _QUESTION_OR_COMMAND_RE.match(sentence):
                continue
            if _HYPOTHETICAL_RE.match(sentence):
                continue
        # Transient run-state belongs in episodes, never durable memory.
        transient_hits = find_transient_markers(sentence)
        if transient_hits:
            # Design decision (settled): an explicit "remember…" request
            # gets NO exemption from this gate — parity with memory_write,
            # where even a direct write of transient content must clear the
            # acknowledge_transient bar. But a silent drop leaves the user
            # believing their explicit request was captured, so the drop
            # must be OBSERVABLE. WARNING, not INFO: the production caller
            # chain (Stop hook → hook.main → propose_from_exchange) never
            # configures logging, and Python's lastResort handler only
            # emits WARNING and above — an INFO record here would be a
            # production no-op. Non-explicit drops stay silent: ordinary
            # prose trips this gate constantly and must not spam the
            # hook's stderr.
            if is_explicit:
                log.warning(
                    "proposals: explicit capture request dropped by the "
                    "transient-marker gate (marker=%r): %.80s",
                    transient_hits[0].marker,
                    sentence,
                )
            continue
        # An explicit capture request is a stated fact; a bare first-person
        # preference is a claim about the user → the tier that wants
        # confirmation, which the review step supplies.
        category = "fact" if is_explicit else "user-inference"
        out.append(
            Proposal(
                id=generate_ulid(),
                body=sentence,
                source_excerpt=sentence,
                suggested_category=category,
                created=when,
            )
        )
        if len(out) >= max_proposals:
            break
    return out


def propose_from_exchange(
    root: Path,
    *,
    user_text: str | None,
    max_pending: int = 20,
    now: datetime | None = None,
) -> list[Proposal]:
    """Extract proposals from one turn's user message and enqueue the new
    ones.

    Returns the proposals actually appended (possibly empty). Enforces the
    queue cap and dedups against what's already queued by source_excerpt,
    so the same recurring sentence isn't re-proposed every turn and the
    queue can't grow without bound.

    Dedup is intentionally against the QUEUE only, not the whole store —
    keeping extraction O(1) in store size so it can run on every turn
    without the O(N) cost a store-wide `find_similar` would add to the
    turn-end hook. A proposal that duplicates an existing memory is caught
    at review time (the model dismisses it); the cheap, always-on path is
    the one that matters for actually closing the capture gap.
    """
    queue = ProposalQueue(root)
    # Already-queued sentences must not count against the per-exchange
    # extraction budget — a recurring preamble would otherwise occupy every
    # slot each turn and permanently mask new durable statements later in
    # the message. This pre-lock read is only a budget hint (O(max_pending));
    # append_within_cap still dedups authoritatively under the flock.
    exclude = frozenset(p.source_excerpt for p in queue.load())
    candidates = extract_proposals(user_text, now=now, exclude_excerpts=exclude)
    if not candidates:
        return []
    # Cap + dedup + write happen together under the queue lock (see
    # append_within_cap) so concurrent Stop hooks can't overshoot max_pending.
    return queue.append_within_cap(candidates, max_pending=max_pending)


__all__ = [
    "PROPOSALS_FILENAME",
    "Proposal",
    "ProposalQueue",
    "extract_proposals",
    "propose_from_exchange",
]
