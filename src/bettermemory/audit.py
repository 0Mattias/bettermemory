"""Silent retrieval-miss telemetry.

The opt-in retrieval contract is bettermemory's load-bearing wager
against auto-injection failure modes: the model searches when
justified, never auto-loads. That contract has a known dark side.
False positives (junk hits) are visible in `dead_weight` and
`recent_negative_outcomes`. False negatives — turns where memory
*should* have been retrieved but wasn't — are structurally invisible,
because nothing in the event log records a search that didn't happen.

This module closes that loop, and since 3.41.0 the loop closes twice:
`probe_for_miss` is also the probe behind `recall.prompt_recall` (the
UserPromptSubmit hook), which runs the SAME search before the turn
starts and delivers up to three of its hits as pointers (relevant,
written in the caller's project, not yet seen in the session segment)
instead of logging a failure after the fact. The recall reads the hits,
not this module's verdict: its own per-session cooldown, not the
retrieval shield, bounds it.

`probe_for_miss` runs a cheap search
sweep over the candidate list its CALLER supplies, using a completed
turn's user message, and looks for a high-relevance hit. Both
production producers supply production's own search pool
(`handlers.search.resolve_search_pool`) rather than the whole active
store, so above the FTS index threshold the probe ranks the same capped
slice the model's retrieval would have; offline tooling that hands over
a full `load_all()` gets the whole store. When a hit exists AND no retrieval
event (see `_RETRIEVAL_EVENT_KINDS`: `search`, `show`, `list`, or
`prompt_recall`) fired in the same session within a configurable
lookback window, the probe returns a `MissReport` — the explicit
signal that the retrieval contract slipped on this turn. The probe
uses the model's configured search mode by default so it measures
what the model would have done, not what a hypothetical scorer might
have found.

Design notes:

- **Probe matches the model's configured search mode.** The default is
  whatever `config.behavior.search_mode` resolves to (typically
  `"hybrid"` since 2.6.8; was `"keyword"` in 1.6.0). Probing with a
  different scorer than the model would have used measures the wrong
  thing — a BM25 hit the model on keyword mode would never have seen
  is not a miss the model could have caught. Callers can override the `mode` parameter on the
  probe for offline curation passes that intentionally want a
  different lens, but the default is "what would the model have
  done."

- **`memory_show` and `memory_list` count as retrieval activity too.**
  The miss probe doesn't fire when a `search`, `show`, `list`, or
  `prompt_recall` event landed in the session's lookback window — the
  first three are retrievals from the model's perspective (`list`
  events, logged while memory_list was on the tool surface, surfaced ids
  and, with `with_bodies=True`, full bodies — the model had the content
  it needed without a `search`), and the fourth is the UserPromptSubmit
  hook having already delivered the pointer, so
  whatever the model does next the miss is not silent. Counting only
  `search` would mis-flag the legitimate search-then-show and
  triage-via-list flows.

- **No event emitted from this module.** Like `search.search` itself,
  the probe returns a structured verdict; the *handler* records the
  event. Keeps audit.py reusable from CLI / tests / offline tooling
  without dragging the recorder dependency in.

- **Threshold rule is a string in the report.** v1 is `top-1 hit
  relevance == "high"`. The relevance label comes from
  `_relevance_label(matched_unique, query_unique)` which classifies on
  coverage >= 0.75. **Calibration is empirical**, and as of 2.7.0
  there's a tool for it: `bettermemory eval --threshold-sweep` runs
  a counterfactual replay of logged `search_miss` events under the
  bundled stricter rules (v2 score floor, v3 dominance, v4
  intersection) and reports `v1_drift` so a divergence between this
  in-process rule and what production actually flagged is visible.
  Single-token user messages structurally always score "high" if the
  token appears anywhere (1/1 = 1.0); multi-token natural language
  with stopwords often lands at 2/3 = "medium" and does not fire.
  Queries with fewer than `MIN_PROBE_CONTENT_TOKENS` unique content
  tokens short-circuit to ``no_signal`` before the rule runs — bare
  continuations like "yes" / "continue" / "go for it" structurally
  carry no signal, so dropping them from the audit corpus keeps the
  threshold rule honest. Pure-digit tokens don't count toward the
  floor (a bare numeric reply like "3.8.0" fragments to digit
  pseudo-tokens), and a message whose content tokens are all
  conversational acknowledgments ("all done", "looks good" — compared
  by surface spelling, see `_ACK_TOKENS`) is gated the same way. The
  rule version is recorded on every
  emitted event so a later calibration pass can replay historical
  logs under a new threshold without losing the audit trail.

- **Lookback is wall-clock, not turn-counter.** The audit fires from a
  client-side hook (Claude Code Stop hook, etc.), which doesn't carry
  the server's per-session turn counter. A 60s window comfortably
  covers a normal model turn while staying short enough that a search
  from two turns ago doesn't paper over a fresh miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Iterable,
    Literal,
    Mapping,
    Sequence,
    cast,
)

from .events import _event_id_items
from .models import Memory, MemoryHit, generate_ulid
from .search import (
    CorpusStats,
    SearchMode,
    _relevance_label_v2,
    _strip_stopwords,
    _tokenize_unstemmed,
    search as run_search,
    tokenize,
)
from .time_utils import ensure_utc, isoformat_utc, parse_event_ts

if TYPE_CHECKING:
    from .admission import Admission


# Events that count as "the model retrieved memory in this turn."
# `search` is the obvious one; `show` is the equally-legitimate
# direct-by-id retrieval; `list` is the same surface with a different
# entry point (scope filter, optionally with bodies). `prompt_recall`
# is the UserPromptSubmit hook's pointer delivery (`recall.
# prompt_recall`): the hook put stored memories' ids + snippets in
# front of the model before the turn began, so the turn is not a
# SILENT miss whatever the model does next. Delivery itself no longer
# reads this shield: the recall surface's own per-session cooldown
# (`recall.RecallState`) bounds it, so a session using memory still
# gets pointers it has not seen. All four put memory content in front
# of the model, so a turn where any of them fired shouldn't trip the
# miss probe. Other event kinds (`use`, `verify`, etc.) are
# downstream of an earlier retrieval — counting them as retrieval
# would double-shield the audit. `session_brief` is deliberately NOT
# here: the brief lands once at session start, before any turn, and
# counting it would shield every turn of the session's first ten
# minutes. Kept as a module-level frozenset so
# a future event kind (e.g. a hypothetical `replay` mode) can be
# added in one place rather than scattered across the function body.
_RETRIEVAL_EVENT_KINDS: frozenset[str] = frozenset(
    {"search", "show", "list", "prompt_recall"}
)


# Threshold rule identifiers. Bumped when the criterion changes so a
# replay of older `search_miss` events can be re-evaluated against the
# new rule without losing the rule the original event was written under.
THRESHOLD_RULE_V1 = "v1_top1_high"

# Top-N hits to retain on every MissReport so a curation pass can replay
# the decision (e.g. "what was the system seeing when it flagged this?").
# Three is enough to triage by eye and small enough that the event log
# doesn't balloon with bodies.
_TOP_HITS_RETAINED = 3

# Default lookback for "was there a search event in this session". Long
# enough to cover a normal multi-tool model turn; short enough that a
# search from earlier in the session doesn't paper over a fresh miss.
DEFAULT_LOOKBACK_SECONDS = 60

# Wall-clock window the Stop hook attributes against (and the window
# the production search handler reads recent negative outcomes over —
# `handlers/search.py` imports this constant so the two share one
# substrate). A retrieval older than this is considered settled —
# the Stop hook settles each turn's retrievals at turn end, and the
# in-process fallback (`session.consume_old_tokens`) holds behind a
# wall-clock floor mirroring this same window (cross-pinned in
# tests), so attributing to a stale retrieval would risk
# double-counting. Wide enough to cover normal conversational
# pauses, narrow enough to focus on the current turn.
# Lives here rather than in hook.py because audit.py imports nothing
# from the handlers or events modules, so both producers AND the
# search handler can import it without a cycle.
ATTRIBUTION_LOOKBACK_SECONDS = 600

# Window for the re-audit dedup (`is_duplicate_audit`). The Stop hook
# fires on EVERY stop of a session, and a long autonomous turn stops
# many times without the user typing anything new — so the same last
# user message gets re-probed on each stop. Dogfood evidence
# (2026-07-03): one ship-go message produced 7 identical `search_miss`
# events over 45 minutes, inflating the miss numerator ~7x for one
# actual decision point. An hour comfortably covers the longest
# observed autonomous turns; a genuinely re-typed identical message an
# hour later is a fresh decision point and legitimately re-audits.
REAUDIT_DEDUP_WINDOW_SECONDS = 3600

# Default creation-shield window: memories CREATED within this many
# seconds of `now` are dropped from the probe's candidate set — a
# memory written during the current turn did not exist when the user
# message arrived, so it cannot be evidence of a retrieval miss (see
# the filter comment in `probe_for_miss`). Deliberately a SEPARATE
# knob from the retrieval-shield lookback: the creation shield asks
# "could this memory have been retrieved this turn?" and wants
# ~turn-duration; the retrieval shield asks "did the model already
# search?" and legitimately wants the much wider attribution window
# (`ATTRIBUTION_LOOKBACK_SECONDS`, 600s, on the Stop hook). Round 84
# calibrated the creation filter at the then-shared 60s window; when
# round 85 widened the hook's lookback to 600s the filter silently
# inherited the 10x window, structurally hiding every memory younger
# than ten minutes from the primary producer's probe — the exact
# freshest-most-relevant cohort whose misses matter most.
DEFAULT_CREATION_SHIELD_SECONDS = 60

# Minimum UNIQUE content tokens (stopwords stripped, pure-digit tokens
# excluded) a probe query must carry before the threshold rule is even
# evaluated. The v1 rule labels any 1/1 coverage as "high", so a bare
# continuation like "yes", "continue", "go for it" (1 content token
# after stopword strip) structurally always fires "miss" if the token
# appears anywhere in any memory — the entire single-content-token
# cohort is a no-signal false-positive class. Surfaced on the 2.7.x
# dogfood log as ~26% of `search_miss` events. Two content tokens isn't
# a calibration claim; it's the floor below which coverage carries no
# information. The count is over UNIQUE tokens because the v1 coverage
# denominator is unique tokens (`query_unique` in search.py) — a
# doubled word ("yes yes", "push it push it") is still the
# single-token class. Pure-digit tokens are excluded because
# `tokenize` splits dotted strings on ".", so a bare numeric reply
# ("3.8.0" -> "3"/"8"/"0", "option 2") would otherwise clear the floor
# with digit fragments that carry no coverage information for the same
# structural reason single tokens don't.
MIN_PROBE_CONTENT_TOKENS = 2

# Conversational acknowledgment tokens. A probe query whose content
# tokens (stopwords stripped) fall ENTIRELY inside this set is a bare
# continuation by another name: "all done", "looks good", "sounds
# good" are two distinct non-stopword tokens, so they clear the
# MIN_PROBE_CONTENT_TOKENS floor and then score 2/2 = "high" against
# any ordinary memory body that happens to contain both words. The set
# lives here (audit-local) rather than in search_lexicon.py's `_STOPWORDS` on
# purpose — "good" / "done" / "all" must stay searchable as body and
# query terms; they're only noise as a COMPLETE probe query. Mixed
# messages ("looks good, now update the backup docs") still pass the
# gate because their non-acknowledgment tokens fall outside the set.
#
# Membership is compared in SURFACE space: the set is built from
# `_tokenize_unstemmed` output and the gate tokenizes the message the
# same way, so both sides carry every tokenize() fold (lowercase,
# diacritics, contractions — a hand-maintained literal set silently
# detached from those) EXCEPT the plural stemmer. The exemption is the
# point, not a shortcut: the curation above is a judgment about
# spellings ('sounds' the ack, not 'sound' the noun), and stems erase
# exactly that line — canonicalising through the stemming `tokenize`
# put 'work'/'look'/'sound'/'don'/'fin'/'nic'/'thank' in the set, so
# ordinary content queries ('does the sound work', 'is Don around')
# fell entirely inside it and were gated to no_signal — silent
# under-detection of the retrieval misses this module exists to count.
_ACK_SURFACE: tuple[str, ...] = (
    "agreed",
    "all",
    "correct",
    "done",
    "exactly",
    "fine",
    "good",
    "great",
    "lgtm",
    "looks",
    "nice",
    "ok",
    "okay",
    "perfect",
    "right",
    "sounds",
    "sure",
    "thanks",
    "works",
    "yeah",
    "yep",
    "yes",
)
_ACK_TOKENS: frozenset[str] = frozenset(
    tok for word in _ACK_SURFACE for tok in _tokenize_unstemmed(word)
)

# Closed set of `triggered_from` discriminator values for `turn_audited`,
# `search_miss`, and `prompt_recall` events. The Stop hook emits
# `"stop_hook"`; the in-process MCP handler emits `"mcp_tool"`; the
# UserPromptSubmit hook emits `"prompt_hook"`. `"session_hook"` is the
# SessionStart hook's stamp on its `session_brief` event
# (`recall.session_brief`). Pinning the set at the builder boundary mirrors the search-mode runtime guard in
# `search.search` — without this check, a typo elsewhere silently
# produces unsplittable eval rows (downstream consumers `groupby`-split
# on this field). Mirrors the same Literal-at-types-only situation:
# Python doesn't enforce it at call time. Any out-of-process value
# added here must ALSO join `sessions._OUT_OF_PROCESS_TRIGGERS`, or its
# events become false anchors for `sessions._latest_in_process_session`
# (the server-session bridge skips on that set alone).
_VALID_TRIGGERED_FROM: frozenset[str] = frozenset(
    {"stop_hook", "mcp_tool", "prompt_hook", "session_hook"}
)


# Verdict literals — surfaced both in the structured return and (verbatim)
# in the emitted event. Treat as a closed set; downstream consumers will
# branch on these.
Verdict = Literal["miss", "ok", "no_signal"]


@dataclass(frozen=True)
class MissHit:
    """Identity + ranking metadata for one probe hit.

    Bodies are deliberately not retained — the id + snippet is enough to
    triage offline, and full bodies in the event log would balloon disk
    usage on a busy store. Snippet is a straight copy of the search hit's,
    so it carries the same query-biased window a live hit does — the body
    text around the terms that matched, not the body's head. Replay
    parity is what makes that safe, and it holds: `probe_for_miss`
    re-runs the same `search()` over the same `user_message`, so the
    retained window is the one a replay of that probe would produce.

    `matched_unique` / `query_unique` are the raw coverage pair the
    relevance label was computed from, and `relevance_v2` is the shadow
    label (`search._relevance_label_v2`) over the same pair. Logging
    the RAW pair — not just the labels — is what makes the historical
    record formula-agnostic: any future candidate rule can be replayed
    from the log alone, closing the "threshold-sweep can narrow but
    never widen" constraint at the data layer.
    """

    id: str
    score: float
    relevance: str
    scopes: tuple[str, ...]
    snippet: str
    matched_unique: int = 0
    query_unique: int = 0
    relevance_v2: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "relevance": self.relevance,
            "scopes": list(self.scopes),
            "snippet": self.snippet,
            "matched_unique": self.matched_unique,
            "query_unique": self.query_unique,
            "relevance_v2": self.relevance_v2,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        """Lean per-hit shape for the every-turn `turn_audited` event.

        Drops `scopes` and `snippet` — those matter for triaging a
        flagged miss (`search_miss` keeps the full shape), while the
        every-turn event only needs the calibration features. Keeping
        the two shapes distinct keeps ~60% of the bytes off the event
        that fires on every single turn.
        """
        return {
            "id": self.id,
            "score": self.score,
            "relevance": self.relevance,
            "relevance_v2": self.relevance_v2,
            "matched_unique": self.matched_unique,
            "query_unique": self.query_unique,
        }


@dataclass(frozen=True)
class MissReport:
    """Structured verdict from `probe_for_miss`.

    `verdict` is the load-bearing field:

    - ``"miss"``: a high-relevance hit exists for this turn's query AND no
      retrieval event (see `_RETRIEVAL_EVENT_KINDS`: `search`, `show`,
      `list`, or `prompt_recall`) fired in the lookback window. The
      retrieval contract slipped — the model should have searched.
    - ``"ok"``: either no hit cleared the threshold (genuine "nothing to
      retrieve here") OR a retrieval event already fired in the lookback
      window (the model did search/show/list; nothing for the audit to
      flag).
    - ``"no_signal"``: the probe couldn't run meaningfully — empty store,
      empty query, all-stopword query. Distinct from "ok" so the consumer
      can tell "audit ran and saw nothing relevant" apart from "audit
      had nothing to work with."

    `recent_retrieval_count` is the number of retrieval events (see
    `_RETRIEVAL_EVENT_KINDS`) found within
    `lookback_seconds` — matched on the session ids (the retrieval
    anchor and the caller's own), plus (when the caller has a project)
    any event that retrieved a record of the caller's project regardless
    of session (see `_count_recent_retrievals`). Zero means no retrieval
    happened within the window; non-zero is the "model did retrieve"
    branch.

    `threshold_rule` records which decision rule was applied. Versioned
    so a future calibration pass can replay old reports under a new rule.

    `no_signal_reason` (optional, additive) explains *why* a
    ``no_signal`` verdict carries no signal when the cause isn't
    obvious from the other fields. Its one producer is the Stop's turn
    with no owner prompt (``"no_owner_prompt"``, `no_owner_prompt_report`):
    the one definition (`owner_prompt`) accepts none of the turn's
    submitted rows, so nothing was probed (RS-F5). Its historical
    producer, the semantic-mode-without-a-model branch
    (``"semantic_model_unavailable"``), was removed with the semantic
    lane in 4.0.0; recorded events still carry it and replay keeps
    reading them. ``None`` from every branch of `probe_for_miss` (empty
    store / empty query / no hits), so existing consumers see an
    unchanged shape.

    `suppressed_by` (optional, additive) names which shield turned a
    threshold-clearing top hit into an ``"ok"`` verdict:
    ``"retrieval"`` (a retrieval event already fired in the lookback
    window), ``"project_cohort"`` (the caller works inside the same
    git project the top hit was written from — see
    `_caller_in_top_hit_project`). A row may also carry
    ``"origin_indeterminate"``, which no branch writes: a caller nothing
    could be read for is admitted nothing by the pool prefilter (design
    3.5), so its probe ranks no hit and reports ``no_signal``, declaring
    no miss. ``None`` on every other branch,
    including genuine misses and below-threshold oks. The field exists
    so verdict CONSUMERS can differ where their questions differ: the
    silent-miss audit keeps treating the project cohort as
    not-a-contract-slip (no search was owed), while the prompt-recall
    delivery lane may still deliver there — the suppression's "the
    context is reachable in the source tree" premise is an audit
    argument, not a delivery argument, and memory-resident facts are
    exactly the ones a source tree cannot serve.
    """

    verdict: Verdict
    checked_at: datetime
    session_id: str
    lookback_seconds: int
    recent_retrieval_count: int
    threshold_rule: str
    top_hits: tuple[MissHit, ...] = field(default_factory=tuple)
    # Echoes back the raw user_message the probe ran against. None when
    # the probe was aborted before run_search executed at all (empty
    # store or empty/whitespace-only query); set to the input string in
    # every branch that reached the ranker — including the "no hits"
    # branch (e.g. an all-stopwords query that run_search filtered to
    # empty). Useful for offline triage: a non-None probe_query on a
    # `no_signal` report tells you the ranker actually ran and saw
    # nothing, distinct from "we never asked it."
    probe_query: str | None = None
    no_signal_reason: str | None = None
    suppressed_by: str | None = None

    @property
    def is_miss(self) -> bool:
        return self.verdict == "miss"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "checked_at": isoformat_utc(self.checked_at),
            "session_id": self.session_id,
            "lookback_seconds": self.lookback_seconds,
            "recent_retrieval_count": self.recent_retrieval_count,
            "threshold_rule": self.threshold_rule,
            "top_hits": [h.to_dict() for h in self.top_hits],
            "probe_query": self.probe_query,
            "no_signal_reason": self.no_signal_reason,
            "suppressed_by": self.suppressed_by,
        }


#: The `no_signal_reason` of a Stop whose turn holds no owner prompt: the
#: one definition (`owner_prompt`) accepts none of the turn's submitted
#: rows, so nothing was probed and nothing settled.
NO_OWNER_PROMPT = "no_owner_prompt"

# The reasons a producer may stamp on `turn_audited` today. A reason
# recorded by an older producer (``"semantic_model_unavailable"``) stays
# readable in the log; no current branch writes it.
_VALID_NO_SIGNAL_REASON: frozenset[str] = frozenset({NO_OWNER_PROMPT})

# Where the reply a Stop settled against came from, stamped as
# `reply_source` on `turn_audited`: the Stop payload's
# `last_assistant_message`, or the transcript's newest assistant text when
# the payload carried none. None (no reply at all) writes no key.
# "interrupted" (RS-F7 part 6): the turn ended with no Stop; a later hook
# audited it from the transcript (`recall.settle_interrupted`), its reply the
# partial one the transcript holds.
_VALID_REPLY_SOURCE: frozenset[str] = frozenset({"payload", "tail", "interrupted"})


def no_owner_prompt_report(
    *, session_id: str, now: datetime, lookback_seconds: int
) -> MissReport:
    """The report of a Stop whose turn holds no owner prompt: verdict
    ``no_signal`` with reason `NO_OWNER_PROMPT`, no probe query and no
    hits, under the rule every Stop row carries. Eval reads it as a
    declined audit, outside the miss rate's denominator."""
    return MissReport(
        verdict="no_signal",
        checked_at=now,
        session_id=session_id,
        lookback_seconds=lookback_seconds,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        no_signal_reason=NO_OWNER_PROMPT,
    )


def turn_audited_fields(
    report: MissReport,
    *,
    session_id: str,
    probe_mode: str,
    assistant_present: bool,
    triggered_from: str,
    repeat: bool = False,
    client_model: str | None = None,
    prompt_id: str | None = None,
    reply_source: str | None = None,
    rule: str | None = None,
    settled_count: int | None = None,
) -> dict[str, Any]:
    """Canonical field set for a ``turn_audited`` event.

    Single source of truth for the one producer, ``hook.run_audit``,
    which two callers reach: the ``bettermemory audit-turn`` process
    (``hook.main``) and the daemon's stop route (``daemon.hook_stop``).
    The 2.6.4 audit found the Stop hook and the in-process MCP handler
    of that time already drifted (``triggered_from`` on the hook, absent
    on the handler); routing every producer through this builder makes
    that drift structurally impossible. ``triggered_from`` is the source
    discriminator, ``"stop_hook"`` or ``"mcp_tool"``, a closed set a
    consumer can split traffic on without guessing (the handler is gone;
    its rows remain in older logs).

    ``no_signal_reason`` rides along additively, omitted when None (the
    common non-no_signal case keeps its exact pre-existing shape), and
    must be one of `_VALID_NO_SIGNAL_REASON` (``"no_owner_prompt"``: the
    definition accepts none of the turn's submitted rows, RS-F5). Without
    it the reason lived only in the tool-response/hook-stdout dict
    (``MissReport.to_dict``), so a STRUCTURAL no_signal (its
    historical producer: the pre-4.0 semantic-mode-without-a-model
    branch, which fired on every turn of such a deployment) was
    event-identical to a benign per-turn one (bare continuation, no
    hits), and no log consumer could split the permanently-unmeasured
    cohort from the healthy one.

    Additive calibration fields (each omitted when absent, so events
    from older producers keep their exact prior shape):

    - ``probe_query`` — the probed user message, which the Recorder
      redacts to ``{hash, preview, len}`` before it lands. Carrying it on EVERY audited turn (not just flagged misses) is
      what makes the re-audit dedup possible and gives the widening
      calibration its denominator.
    - ``top_hits`` — compact per-hit calibration features
      (`MissHit.to_compact_dict`: id/score/relevance/relevance_v2/
      matched_unique/query_unique, no snippet). Pre-change only
      `search_miss` carried hits, so a rule that fires where v1 didn't
      could never be evaluated against history.
    - ``repeat`` — True when `is_duplicate_audit` matched an earlier
      audit of the same (session, message) inside
      `REAUDIT_DEDUP_WINDOW_SECONDS`. Repeat events keep cadence
      visible while eval/health exclude them from denominators, and
      producers skip the companion `search_miss` entirely.
    - ``client_model`` — the model id the Stop hook read off the
      transcript's latest assistant row (e.g. "claude-sonnet-5").
      The MCP channel carries no model identity, so the hook is the
      only producer that can stamp it; per-model usage slices in
      `bettermemory eval` read this field.
    - ``prompt_id``: the harness turn the Stop payload names (Claude
      Code's ``prompt_id``, the same id its ``UserPromptSubmit`` payload
      carries), so a delivery's turn joins its Stop row without a
      transcript read (RS-F3, 2026-10-17). Before it, a Stop named its
      turn only on the ``use`` row of a settlement.
    - ``reply_source``: where the reply the Stop settled against came
      from, ``"payload"`` (the Stop payload's ``last_assistant_message``)
      or ``"tail"`` (the transcript's newest assistant text), absent when
      there was no reply, so it agrees with ``assistant_present``.
    - ``settled_count``: how many memories the audit of this turn settled
      as used (ENG-108): the Stop's retrieval settlement, attributed and
      auto (`hook._emit_hook_attributions`), plus the shown pointers its
      reply used (`recall.settle_shown`), each one ``use`` row. Zero for a
      turn that settled nothing: no owner prompt, no reply, nothing
      pending, or an interrupted turn, whose pointers stay pending. The
      Stop records its row after the settlements so the count is the
      settlements' own, and a reader joins a turn to its settlements
      without walking the ``use`` rows.
    """
    if settled_count is not None and (
        isinstance(settled_count, bool)
        or not isinstance(settled_count, int)
        or settled_count < 0
    ):
        raise ValueError(
            f"settled_count must be a non-negative int or None, got {settled_count!r}"
        )
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    if reply_source is not None and reply_source not in _VALID_REPLY_SOURCE:
        raise ValueError(
            f"reply_source must be one of {sorted(_VALID_REPLY_SOURCE)!r} "
            f"or None, got {reply_source!r}"
        )
    if (
        report.no_signal_reason is not None
        and report.no_signal_reason not in _VALID_NO_SIGNAL_REASON
    ):
        raise ValueError(
            f"no_signal_reason must be one of {sorted(_VALID_NO_SIGNAL_REASON)!r} "
            f"or None, got {report.no_signal_reason!r}"
        )
    fields: dict[str, Any] = {
        "session_id": session_id,
        "verdict": report.verdict,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "probe_mode": probe_mode,
        "threshold_rule": report.threshold_rule,
        "assistant_present": assistant_present,
        "triggered_from": triggered_from,
    }
    if report.no_signal_reason is not None:
        fields["no_signal_reason"] = report.no_signal_reason
    if report.probe_query is not None:
        fields["probe_query"] = report.probe_query
    if report.top_hits:
        fields["top_hits"] = [h.to_compact_dict() for h in report.top_hits]
    if repeat:
        fields["repeat"] = True
    if client_model is not None:
        fields["client_model"] = client_model
    if prompt_id is not None:
        fields["prompt_id"] = prompt_id
    if reply_source is not None:
        fields["reply_source"] = reply_source
    if rule is not None:
        fields["rule"] = rule
    if settled_count is not None:
        fields["settled_count"] = settled_count
    return fields


def search_miss_fields(
    report: MissReport,
    *,
    session_id: str,
    triggered_from: str,
    event_id: str | None = None,
    client_model: str | None = None,
    rule: str | None = None,
) -> dict[str, Any]:
    """Canonical field set for a ``search_miss`` event. Pairs with
    :func:`turn_audited_fields` — see that docstring for the
    drift-prevention rationale.

    Carries ``recent_retrieval_count`` so ``eval._silent_miss_from_event``
    can render it: the 2.6.4 audit found that consumer reading the
    field off the ``search_miss`` event while every producer emitted
    it on ``turn_audited`` only — the eval column was always blank.

    ``event_id`` is a stable per-event ULID stamped at emission time.
    Surfaced so ``memory_acknowledge_miss`` can reference one specific
    miss for resolution (the per-event escape hatch documented in T4 —
    distinct from the bulk ``silent_miss_cutoff`` written by
    ``bettermemory consolidate --acknowledge-misses-before``, which
    wipes EVERY pre-cutoff miss). When omitted a fresh ULID is
    generated; callers should not pass an explicit value outside of
    tests that pin specific ids.
    """
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    fields: dict[str, Any] = {
        "event_id": event_id if event_id is not None else generate_ulid(),
        "session_id": session_id,
        "threshold_rule": report.threshold_rule,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "top_hits": [h.to_dict() for h in report.top_hits],
        "probe_query": report.probe_query,
        "triggered_from": triggered_from,
    }
    if client_model is not None:
        fields["client_model"] = client_model
    if rule is not None:
        fields["rule"] = rule
    return fields


#: Why a probed owner prompt delivered nothing, on its `recall_held` row
#: (RS-F7 part 6): the probe reported ``no_signal``; it ranked no hit; every
#: hit it ranked was held by a gate (`held` names each with its reason);
#: the render kept no pointer under the cap; or a concurrent prompt of the
#: session claimed every pointer first.
HELD_CAUSES: frozenset[str] = frozenset(
    {"no_signal", "no_hits", "every_hit_held", "nothing_rendered", "segment_claimed"}
)


def recall_held_fields(
    report: MissReport,
    *,
    session_id: str,
    probe_mode: str,
    cause: str,
    held: Sequence[Mapping[str, str]],
    rule: str,
    segment: int | None = None,
    prompt_id: str | None = None,
    transcript_path: str | None = None,
    harness: str | None = None,
    triggered_from: str = "prompt_hook",
) -> dict[str, Any]:
    """Canonical field set for a ``recall_held`` event: the UserPromptSubmit
    hook's record of a probed owner prompt it delivered nothing for, the
    other half of `prompt_recall_fields` (RS-F7 part 6: every probed owner
    prompt writes one row or the other). The probe's verdict and hits ride
    along as on a `prompt_recall` row, so a reader counts the probe's
    outcomes over every prompt and not only the delivered ones; `cause`
    (one of `HELD_CAUSES`) says why nothing was shown and `held` names each
    held hit with its reason, the entries `prompt_recall` records as
    ``suppressed_ids`` when it delivers beside them."""
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    if cause not in HELD_CAUSES:
        raise ValueError(f"cause must be one of {sorted(HELD_CAUSES)!r}, got {cause!r}")
    fields: dict[str, Any] = {
        "event_id": generate_ulid(),
        "session_id": session_id,
        "verdict": report.verdict,
        "threshold_rule": report.threshold_rule,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "top_hits": [h.to_dict() for h in report.top_hits],
        "probe_query": report.probe_query,
        "probe_mode": probe_mode,
        "triggered_from": triggered_from,
        "cause": cause,
        "held": [dict(entry) for entry in held],
        "rule": rule,
    }
    if report.no_signal_reason is not None:
        fields["no_signal_reason"] = report.no_signal_reason
    if segment is not None:
        fields["segment"] = segment
    if prompt_id is not None:
        fields["prompt_id"] = prompt_id
    if transcript_path is not None:
        fields["transcript_path"] = transcript_path
    if harness is not None:
        fields["harness"] = harness
    return fields


def prompt_recall_fields(
    report: MissReport,
    *,
    session_id: str,
    probe_mode: str,
    injected_chars: int,
    triggered_from: str = "prompt_hook",
    delivered_reason: str = "project",
    shown_ids: Sequence[str] | None = None,
    suppressed: Sequence[Mapping[str, str]] | None = None,
    rule: str | None = None,
    segment: int | None = None,
    prompt_id: str | None = None,
    transcript_path: str | None = None,
    harness: str | None = None,
) -> dict[str, Any]:
    """Canonical field set for a ``prompt_recall`` event — the
    UserPromptSubmit hook's record that it injected stored memories'
    ids + snippets into the model's context before the turn began.

    Pairs with :func:`turn_audited_fields` / :func:`search_miss_fields`
    and exists for the same drift-prevention reason: today the hook is
    the only producer, but the builder boundary is where the shape is
    pinned, not the call site.

    The probe's full ``MissHit.to_dict`` shapes ride along as
    ``top_hits`` (the candidates, delivered or not), so any future rule
    can be replayed over delivered recalls exactly as it replays over
    flagged misses. Beside them:

    - ``probe_mode`` — the ranker the probe used; ``search_miss``
      leaves this on the companion ``turn_audited`` event, but a
      recall has no companion (the Stop hook's later audit of the same
      turn sees the recall via ``_RETRIEVAL_EVENT_KINDS`` and reports
      ``ok``), so the mode must travel on the event itself.
    - ``injected_chars`` (and ``chars``, the recall surface's name for
      the same number) — rendered size of the injected block. There is
      no budget test for per-turn injected context (the resident
      footprint suite measures the tool surface, not hook stdout), so
      the log carries the number that would let one be written.
    - ``delivered_reason`` — the `[behavior] prompt_recall` mode that
      delivered: ``"project"`` (memories of the caller's repository
      only) or ``"everywhere"``. Events written before the recall
      surface carry ``"miss"`` or ``"project_cohort"``, the two lanes
      of the old one-hit rule; eval slices on the field either way.
    - ``shown_ids`` (the delivered ids, in block order),
      ``suppressed`` (``{"id", "reason"}`` per candidate held back:
      ``seen_in_segment``, ``other_project``, ``provenance``,
      ``contradicted`` or ``superseded``; rows of rs-v5 and earlier also
      ``low_relevance`` and ``segment_cap``), ``rule``
      (the delivery rule's name), ``segment`` (the session segment the
      cooldown counted it in), ``prompt_id`` (the harness's turn id
      when its payload carries one), ``transcript_path`` and
      ``harness``. Each is omitted when the caller passes None, so the
      builder's older call shape keeps its exact output.
    """
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    fields: dict[str, Any] = {
        "event_id": generate_ulid(),
        "session_id": session_id,
        "threshold_rule": report.threshold_rule,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "top_hits": [h.to_dict() for h in report.top_hits],
        "probe_query": report.probe_query,
        "probe_mode": probe_mode,
        "injected_chars": injected_chars,
        "triggered_from": triggered_from,
        "delivered_reason": delivered_reason,
    }
    if shown_ids is not None:
        fields["shown_ids"] = list(shown_ids)
    if suppressed is not None:
        fields["suppressed_ids"] = [dict(entry) for entry in suppressed]
    if rule is not None:
        fields["rule"] = rule
        fields["chars"] = injected_chars
    if segment is not None:
        fields["segment"] = segment
    if prompt_id is not None:
        fields["prompt_id"] = prompt_id
    if transcript_path is not None:
        fields["transcript_path"] = transcript_path
    if harness is not None:
        fields["harness"] = harness
    return fields


def is_duplicate_audit(
    events: list[dict[str, Any]],
    *,
    session_id: str,
    probe_query_hash: str | None,
    probe_query_text: str,
    now: datetime,
    window_seconds: int = REAUDIT_DEDUP_WINDOW_SECONDS,
) -> bool:
    """True when this (session, message) pair was already audited inside
    the dedup window.

    The Stop hook fires on every stop of a session; a long autonomous
    turn stops many times with the same last user message, and each
    stop re-probed and re-flagged it — the 2026-07-03 dogfood log shows
    one message producing 7 identical `search_miss` events. Producers
    call this before emitting: a duplicate still records `turn_audited`
    (with `repeat=True`, keeping cadence observable) but never a second
    `search_miss`, and eval/health exclude repeats from denominators.

    Matching reads the `probe_query` field that `turn_audited_fields`
    now carries, in BOTH shapes the Recorder can produce: the redacted
    ``{hash, preview, len}`` dict (compared via `probe_query_hash` — the
    producer computes it with `events.redact_query` so the comparison
    uses the exact production hash) and the verbatim string a log
    written before 9.0.0 can carry (compared via `probe_query_text`).
    Events without `probe_query` (older producers) never match — the
    dedup only engages on data written after this field shipped, which
    biases toward the pre-existing behavior (re-flag) rather than
    suppressing a genuine miss on legacy evidence.

    `events` is the producer's already-loaded recent-events list in
    append (chronological) order; the walk is newest-first and stops at
    the window boundary, so cost is bounded by the window, not the log.
    """
    cutoff = now - timedelta(seconds=window_seconds)
    for ev in reversed(events):
        if ev.get("kind") != "turn_audited":
            continue
        if (ev.get("session_id") or ev.get("session")) != session_id:
            continue
        ts = parse_event_ts(ev.get("ts"))
        if ts is None:
            continue
        if ts < cutoff:
            # Append order means every remaining event in the reversed
            # walk is older still — nothing left can be in-window.
            break
        pq = ev.get("probe_query")
        if isinstance(pq, dict):
            if probe_query_hash is not None and pq.get("hash") == probe_query_hash:
                return True
        elif isinstance(pq, str):
            if pq == probe_query_text:
                return True
    return False


def probe_for_miss(
    memories: list[Memory],
    user_message: str,
    *,
    recent_events: Iterable[dict[str, Any]],
    session_id: str,
    retrieval_session_id: str | None = None,
    now: datetime | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    creation_shield_seconds: int = DEFAULT_CREATION_SHIELD_SECONDS,
    admission: Admission | None = None,
    worktree_root: str | None = None,
    excluded_scopes: set[str] | None = None,
    mode: str = "hybrid",
    half_life_days: float = 30.0,
    conversational: bool = True,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None = None,
) -> MissReport:
    """Decide whether the just-completed turn was a silent retrieval miss.

    Runs a cheap search probe over `memories` using `user_message`, then
    asks: did the model retrieve memory this session within the last
    `lookback_seconds` (via any event in `_RETRIEVAL_EVENT_KINDS`:
    `search`, `show`, `list`, or `prompt_recall`)? The cross of those
    two facts gives the verdict.

    `recent_events` is an iterable over the session's recent event log
    entries (any iterable — the function only walks it once). Callers
    typically pass `iter_events(store.root)` filtered to the current
    session id, or a slice of the in-memory recorder buffer. The function
    consumes the iterable defensively in a single pass; it does not
    materialise unbounded history.

    `admission` is the hooks' POOL_PREFILTER admission
    (`admission.Admission`, design 3.5) for the caller's resolution: it
    ranks only what the model's auto-scoped search could have reached, so
    the probe matches the *model's* view of the store. Probing with a
    wider lens than the model uses would generate ghost misses for
    memories the model couldn't have retrieved in the first place. It
    also decides the two "same project" tests below on the records'
    homes (`Admission.same_project`: the home on the caller's chain or a
    sub-project of it, design D13). Pass None on intentionally
    cross-project audits.

    `mode` is forwarded to `search`. Production callers pass
    `config.behavior.search_mode` so the probe sees the same ranker
    the model would have used — probing with a different scorer
    measures "would a different ranker have hit" rather than "did the
    model miss what its ranker would have shown." Default falls to
    `"hybrid"` (the package default since 2.6.8) when the caller
    doesn't have a config to thread in. The
    probe deliberately does NOT request `expand_top` or `path_drift` —
    those signals matter for *consuming* a hit, not for deciding
    whether a search should have happened.

    `half_life_days` and `conversational` are forwarded verbatim to
    `search` so the probe ranks with the same scorer configuration
    production retrieval uses — the same probe-matches-the-ranker rule
    the `mode` parameter exists for. Production callers thread
    `config.behavior.recency_boost_half_life_days` and
    `config.behavior.conversational` (the Lane L temporal repairs,
    default ON since 6.1.0); offline callers can leave the defaults,
    which match the package-default ranker. The verdict reads only the
    rank-1 hit, so a probe ranking with different inputs than
    production disagrees with it in both directions (a masked miss
    where production's rank 1 was another memory, a phantom miss where
    the probe's is). The rescue-expansion leg is never engaged here:
    it is a bench instrument, not a shipped knob, so production never
    ranks with it either.

    `corpus_stats_provider` is forwarded for the same reason and belongs
    with the CANDIDATE POOL the caller built: `memories` must be
    production's pool, not an unconditional `store.load_all()`. Above
    `_INDEX_THRESHOLD_DEFAULT` production ranks a `_PREFILTER_CAP`-capped,
    query-relevance-ordered slice and corrects its collapsed IDF with
    corpus document frequencies, so a probe fed the whole corpus with no
    provider ranks a strict SUPERSET under different statistics — and the
    verdict, which reads only the rank-1 hit, can land on a memory
    production would never have surfaced. Both production producers build
    the pair together via `handlers.search.resolve_search_pool`; offline
    callers that pass a full `load_all()` correctly leave this None,
    because for them pool statistics ARE corpus statistics. The honest
    residuals in that pool parity are listed above `_probe_hits`.

    `creation_shield_seconds` is the creation-shield window: memories
    whose `created` falls within it are dropped from the candidate set
    (a memory written during the current turn cannot be retrieval-miss
    evidence — see the filter comment below). It is deliberately
    decoupled from `lookback_seconds`: the retrieval shield wants the
    wide attribution window (600s on the Stop hook), but reusing that
    width here would hide every memory younger than ten minutes from
    the probe. Both production producers leave the
    `DEFAULT_CREATION_SHIELD_SECONDS` (60s, ~turn duration) default.

    `retrieval_session_id` is the session id used ONLY for the "did the
    model already retrieve this turn?" shield (`_count_recent_retrievals`).
    Out-of-process callers (the hooks) must pass the bridged *server*
    session here, because their `session_id` is Claude Code's transcript
    id — a different id space from the server's `sess_<hex>` — so alone
    it never matches the search/show/list events the server emitted,
    leaving the shield dead and every searched-then-continued turn
    mis-flagged as a miss. The shield matches the UNION of the two ids,
    not the anchor alone: a `prompt_recall` delivery records under the
    CALLER's transcript id, and outside any git checkout it carries no
    worktree stamp either, so an anchored-server-only match orphaned
    exactly that event — re-flagging a delivered turn as a silent miss
    and defeating the anti-spam bound. In-process callers omit it; their
    `session_id` already is the server session. The shield additionally
    counts retrieval events under ANY session id that retrieved a record
    of the caller's project, or that carry the caller's `worktree_root`
    stamp, so a concurrent session in the project or the checkout (or a
    mid-conversation server restart that re-anchored the bridge) can't
    orphan an in-window retrieval and re-fire a false miss.

    Returns a `MissReport`. The handler is responsible for emitting a
    `search_miss` event when `report.is_miss` — this function is
    side-effect-free so it can be reused from offline tooling.
    """
    now = _probe_now(now)
    if not memories or not user_message.strip():
        return _no_signal_report(now, session_id, lookback_seconds, None)
    if _too_thin_to_probe(user_message):
        return _no_signal_report(now, session_id, lookback_seconds, user_message)
    memories = _created_before_shield(memories, now, creation_shield_seconds)
    hits = _probe_hits(
        memories,
        user_message,
        now=now,
        mode=mode,
        excluded_scopes=excluded_scopes,
        admission=admission,
        half_life_days=half_life_days,
        corpus_stats_provider=corpus_stats_provider,
        conversational=conversational,
    )
    if not hits:
        return _no_signal_report(now, session_id, lookback_seconds, user_message)
    return _probe_report(
        _miss_hits(hits),
        user_message,
        recent_events,
        session_id=session_id,
        retrieval_session_id=retrieval_session_id,
        now=now,
        lookback_seconds=lookback_seconds,
        admission=admission,
        worktree_root=worktree_root,
    )


def _probe_now(now: datetime | None) -> datetime:
    """Take the caller's `now` (naive, aware or None); return it as aware UTC."""
    # Coerce at the single seam: an INJECTED naive `now` (e.g. a test or
    # offline caller that builds `datetime(...)` without tzinfo) would
    # otherwise flow uncoerced into `_count_recent_retrievals` (where
    # `cutoff = now - timedelta(...)` stays naive) and `run_search`,
    # then raise `TypeError: can't compare offset-naive and offset-aware
    # datetimes` the moment any retrieval event's tz-aware `ts` (always
    # tz-aware — it comes from `parse_event_ts`) is compared against the
    # naive cutoff. `ensure_utc(None)` returns None so the `or` fallback
    # still fires for the unset case; both downstream call sites read this
    # now-coerced local `now`.
    return ensure_utc(now) or datetime.now(timezone.utc)


def _no_signal_report(
    now: datetime,
    session_id: str,
    lookback_seconds: int,
    probe_query: str | None,
) -> MissReport:
    """Take the probe's context and query; return its `no_signal` report."""
    return MissReport(
        verdict="no_signal",
        checked_at=now,
        session_id=session_id,
        lookback_seconds=lookback_seconds,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(),
        probe_query=probe_query,
    )


def _too_thin_to_probe(user_message: str) -> bool:
    """Take the user's message; return True when it carries too little to probe."""
    # Single-content-token cohort: structurally always scores "high" on
    # the v1 rule because 1/1 = 1.0 = "high" against any memory that
    # mentions the token. Bare continuations ("yes", "continue",
    # "go for it" — "for"/"it" are stopwords) fall in this bucket and
    # are pure noise. The count is over UNIQUE content tokens with
    # pure-digit tokens excluded, and an all-acknowledgment message is
    # gated even above the floor — see the MIN_PROBE_CONTENT_TOKENS /
    # _ACK_TOKENS comments for why each carve-out exists. The two
    # checks deliberately read different token spaces: the floor counts
    # STEMMED tokens (its denominator must match the ranker's
    # unique-token coverage), while the ack subset compares UNSTEMMED
    # surfaces ('sound'/'work' must not be swallowed by the stems of
    # 'sounds'/'works' — see _ACK_TOKENS). probe_query is set so a
    # `no_signal` report on this path is distinguishable from the
    # empty-query branch above.
    content_tokens = {
        t for t in _strip_stopwords(tokenize(user_message)) if not t.isdigit()
    }
    surface_tokens = {
        t
        for t in _strip_stopwords(_tokenize_unstemmed(user_message))
        if not t.isdigit()
    }
    return (
        len(content_tokens) < MIN_PROBE_CONTENT_TOKENS or surface_tokens <= _ACK_TOKENS
    )


def _created_before_shield(
    memories: list[Memory], now: datetime, creation_shield_seconds: int
) -> list[Memory]:
    """Take the pool and the shield; return the memories created before it."""
    # A memory created inside the CREATION-SHIELD window did not exist
    # when the user message arrived, so it cannot be evidence of a
    # retrieval miss. Without this filter the proactive-capture flow
    # self-flags: the model writes a NEW durable fact this turn
    # (correctly, without a pointless search), the body echoes the
    # user's phrasing, and the just-written memory scores "high"
    # against the very message that prompted it. `write` events are
    # deliberately NOT a retrieval shield (`_RETRIEVAL_EVENT_KINDS`) —
    # shielding the whole verdict on a write would mask genuine misses
    # on OLDER memories, which this filter leaves free to flag. Filter
    # on `created` only, never `updated`: an updated memory existed
    # before and its prior content was retrievable.
    #
    # Two DECOUPLED windows are in play here (round 88; they were one
    # knob in round 84, when both sat at 60s): the creation shield
    # (`creation_shield_seconds`, ~turn duration) asks "could this
    # memory have been retrieved this turn at all?", while the
    # retrieval/attribution shield (`lookback_seconds`, 600s on the
    # Stop hook since round 85) asks "did the model already search?".
    # Reusing `lookback_seconds` here — the round-84 shape — meant the
    # Stop hook's 600s widening silently made every memory younger
    # than ten minutes invisible to the primary producer's probe: a
    # memory captured two turns back existed well before this message
    # and is exactly the freshest, most-likely-relevant content a
    # failed search should be flagged over. If the filter empties the
    # candidate list, `run_search` returns no hits and the
    # no_signal-with-probe_query branch below reports it.
    creation_cutoff = now - timedelta(seconds=creation_shield_seconds)
    return [m for m in memories if (ensure_utc(m.created) or now) <= creation_cutoff]


# Honest residuals in the pool parity `probe_for_miss` keeps with
# production (its docstring), none of them closed here:
#
# - The producers size the pool's cap-starvation guard for a DEFAULT
#   search (`handlers.search.default_search_width` — the config knob
#   under the same clamp a request goes through), since the search they
#   describe is the one that did not happen and carries no width of its
#   own. A model habitually passing a wider `max_results` is therefore
#   measured against a narrower counterfactual than its own habit;
#   `handlers.search.resolve_search_pool` carries the measurement of
#   how far that can travel.
# - The producers build the pool from the RAW USER MESSAGE, because
#   that is the only query text a turn-end audit has. The model would
#   have typed a distilled query, so above the index threshold the FTS
#   prefilter draws a different capped slice than the model's own
#   search would have — the pool is production's *mechanism*, not
#   necessarily production's *rows*.
# - `corpus_stats_provider` prices document frequencies over the
#   admitted collection, which still contains the memories the
#   creation shield then drops from `memories`. Only the df
#   DENOMINATORS see them — no shielded memory can be ranked or
#   returned — so the effect is a small IDF drift, not a leak.
def _probe_hits(
    memories: list[Memory],
    user_message: str,
    *,
    now: datetime,
    mode: str,
    excluded_scopes: set[str] | None,
    admission: Admission | None,
    half_life_days: float,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    conversational: bool,
) -> list[MemoryHit]:
    """Take the shielded pool, the message and the ranker knobs; return the hits."""
    # `run_search` returns up to `max_results` hits already sorted by
    # relevance; we ask for `_TOP_HITS_RETAINED` so the report carries
    # enough context to triage without a second call. `mode` is cast to
    # the Literal `SearchMode` after validation — anything outside the
    # allowed set lets `run_search` raise the same ValueError the
    # memory_search handler would, so a bad config doesn't silently
    # degrade the audit.
    if mode not in ("keyword", "bm25", "hybrid"):
        raise ValueError(
            f"unknown audit probe mode {mode!r}; must be one of: keyword, bm25, hybrid"
        )
    hits: list[MemoryHit] = run_search(
        memories,
        user_message,
        excluded_scopes=excluded_scopes,
        admission=admission,
        max_results=_TOP_HITS_RETAINED,
        now=now,
        half_life_days=half_life_days,
        mode=cast(SearchMode, mode),
        corpus_stats_provider=corpus_stats_provider,
        # The expansion leg is a bench instrument, not a shipped knob:
        # production ranks without it, so the probe does too.
        rescue_expansion=False,
        conversational=conversational,
    )
    return hits


def _miss_hits(hits: list[MemoryHit]) -> tuple[MissHit, ...]:
    """Take the probe's ranked hits; return them as the report's `MissHit`s."""
    return tuple(
        MissHit(
            id=h.id,
            score=h.score,
            relevance=h.relevance,
            scopes=tuple(h.scopes),
            snippet=h.snippet,
            matched_unique=len(h.match_terms),
            query_unique=h.query_unique,
            relevance_v2=_relevance_label_v2(len(h.match_terms), h.query_unique),
        )
        for h in hits
    )


def _probe_report(
    top_hits: tuple[MissHit, ...],
    user_message: str,
    recent_events: Iterable[dict[str, Any]],
    *,
    session_id: str,
    retrieval_session_id: str | None,
    now: datetime,
    lookback_seconds: int,
    admission: Admission | None,
    worktree_root: str | None,
) -> MissReport:
    """Take the probe's hits and the session's events; return the verdict's report."""
    # v1 rule: any top-1-relevance="high" hit counts as "the probe found
    # something the model should have retrieved." Looser than requiring
    # high on all three; tighter than letting "medium" through (which
    # would fire on every borderline query and drown the bucket).
    top_hit = top_hits[0]
    clears_threshold = top_hit.relevance == "high"

    recent_retrieval_count = _count_recent_retrievals(
        recent_events,
        # Match the *server* session that emitted the retrieval events
        # AND the caller's own id (see `retrieval_session_id` in the
        # docstring): a `prompt_recall` delivery records under the
        # caller's transcript id, and the caller-id match is what keeps
        # "a delivered pointer is not a silent miss" true whatever the
        # pointer's home.
        # In-process callers pass no anchor; the set is just their own
        # server session.
        session_ids={sid for sid in (retrieval_session_id, session_id) if sid},
        now=now,
        lookback_seconds=lookback_seconds,
        # The caller's project widens the shield to ANY session's
        # retrievals of its records: the shield's real question is "did
        # the model retrieve this project's memory within the window",
        # not "under this one anchored session id".
        admission=admission,
        worktree_root=worktree_root,
    )
    verdict, suppressed_by = _probe_verdict(
        top_hits, clears_threshold, recent_retrieval_count, admission
    )
    return MissReport(
        verdict=verdict,
        checked_at=now,
        session_id=session_id,
        lookback_seconds=lookback_seconds,
        recent_retrieval_count=recent_retrieval_count,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=top_hits,
        probe_query=user_message,
        suppressed_by=suppressed_by,
    )


def _probe_verdict(
    top_hits: tuple[MissHit, ...],
    clears_threshold: bool,
    recent_retrieval_count: int,
    admission: Admission | None,
) -> tuple[Verdict, str | None]:
    """Take the hits and the shields' inputs; return (verdict, suppressed_by)."""
    # `suppressed_by` names the shield that downgraded a
    # threshold-clearing hit; None everywhere else (see MissReport).
    suppressed_by: str | None = None
    if not clears_threshold:
        verdict: Verdict = "ok"
    elif recent_retrieval_count > 0:
        verdict = "ok"
        suppressed_by = "retrieval"
    elif _caller_in_top_hit_project(top_hits[:1], admission):
        # Caller is working inside the same git project the
        # threshold-deciding top hit was written from. The model already
        # has that project's source tree open, so the absence of a
        # memory_search isn't a contract slip — the relevant context is
        # reachable without one. Only `top_hits[:1]` is consulted: the
        # verdict threshold reads only the top hit, so only that hit can
        # explain away the missing search — a low-relevance project hit
        # at rank 2-3 must not swallow a real miss on a global top-1.
        # Dogfood evidence (2.7.x): ~95% of replayable misses came from
        # this cohort ("update bettermemory", "push it", "is X up to
        # date") asked from inside the matching repo, with the project
        # memory AS the top hit — so restricting to top-1 preserves the
        # designed suppression.
        verdict = "ok"
        suppressed_by = "project_cohort"
    else:
        verdict = "miss"
    return verdict, suppressed_by


def _caller_in_top_hit_project(
    top_hits: tuple[MissHit, ...],
    admission: Admission | None,
) -> bool:
    """True when the caller is in the same git project as the
    threshold-deciding top hit.

    Used to suppress the miss verdict when "the model has source open"
    explains the missing search. The caller passes ``top_hits[:1]`` —
    the verdict threshold reads only the top hit, so only that hit can
    explain away the missing search; a lower-ranked project hit must
    not swallow a miss on a global top-1. Both halves of the per-hit
    check are load-bearing:

    * ``projects:`` scope on the hit — a global memory has no project
      boundary to suppress against. Surfacing one through this gate
      would swallow real misses on cross-cutting notes (auth tokens,
      home-dir scripts, etc.) that legitimately should have prompted
      a search even from inside a project repo.
    * the memory's home is of the caller's project
      (`Admission.same_project`: on the caller's chain or a sub-project
      of it, design 3.5 and D13): the memory was written in this
      project, so the model is plausibly editing files it describes. A
      project-tagged memory homed elsewhere doesn't qualify.

    The pool prefilter already keeps other projects' memories out of the
    top hits, so in practice the home test usually holds. Checking it
    explicitly keeps the suppression self-contained — the rule reads
    correctly without relying on transitive search behavior, and a
    future caller that bypasses the admission (offline curation, eval
    replays, which pass none) doesn't accidentally suppress real
    cross-project misses.
    """
    if admission is None:
        return False
    for hit in top_hits:
        if not any(s.startswith("projects:") for s in hit.scopes):
            continue
        if admission.same_project(hit.id):
            return True
    return False


def _count_recent_retrievals(
    events: Iterable[dict[str, Any]],
    *,
    session_ids: Collection[str],
    now: datetime,
    lookback_seconds: int,
    admission: Admission | None = None,
    worktree_root: str | None = None,
) -> int:
    """Count retrieval events for the caller's project, its checkout or
    any of `session_ids` within the window.

    Retrieval = `kind in _RETRIEVAL_EVENT_KINDS` (`{"search", "show",
    "list"}`). All three shield the audit from flagging a miss: a
    memory_show by id is an equally legitimate way for the model to
    pull content, and a memory_list call surfaces a known scope without
    re-searching. Counting only `search` would mis-flag the common
    search-then-show round-trip, where the search happens early in the
    turn and the show happens later.

    An in-window retrieval counts when ANY of:

    * it retrieved a record of the caller's project, under ANY session
      id: a record it names (``returned``, ``memory_ids``, ``hit_ids``,
      ``shown_ids``, ``ids``, ``id``) whose home `admission` reads as
      the caller's project (`Admission.same_project`, design 3.5 and
      D13; the homes read in one read). The shield's question is "did
      the model retrieve this project's memory within the window", and a
      single anchored session id can't answer it: a concurrent session in
      the project, or a mid-conversation server restart that flipped
      `_latest_in_process_session`'s anchor, used to orphan every
      in-window retrieval the previous session made and re-fire a
      false miss (round 85's 60→600s widening scaled that collision
      window ~10x in the over-flag direction);
    * its `worktree_root` stamp is the caller's `worktree_root`, under
      ANY session id: a retrieval the caller's own checkout performed,
      whatever it returned (a search that returned nothing included); or
    * its session matches one of `session_ids` — the anchored server
      session plus the hook caller's own transcript id. The caller id
      is load-bearing for exactly one producer: a `prompt_recall`
      delivery records under the transcript id, so an anchored-server-
      only match orphaned the delivery and re-flagged a served turn as a
      silent miss. A caller with no admission keeps the single-session
      behavior. The union
      is strictly additive on the shield side (everything that
      shielded before still does), so the bias is conservative —
      over-suppress, the project's stance on miss-signal noise. The
      session-anchored `_disabled_scopes_from_events` replay
      deliberately keeps its single-session semantics:
      reset-on-restart is load-bearing there, not here.

    Defensive against the same malformed-event cases the rest of the
    health pipeline handles: missing ts, non-string ts, non-session
    events. A single bad row never raises.
    """
    cutoff = now - timedelta(seconds=lookback_seconds)
    window: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("kind") not in _RETRIEVAL_EVENT_KINDS:
            continue
        ts = parse_event_ts(ev.get("ts"))
        if ts is None or ts < cutoff:
            continue
        window.append(ev)
    if admission is not None:
        admission.prime(rid for ev in window for rid in _retrieved_ids(ev))
    count = 0
    for ev in window:
        # Canonical-first session read with the legacy fallback the
        # other event consumers use — see 70e41a4.
        if (ev.get("session") or ev.get("session_id")) in session_ids:
            count += 1
            continue
        # A retrieval the caller's own checkout performed shields
        # regardless of which server session emitted it.
        if worktree_root is not None and ev.get("worktree_root") == worktree_root:
            count += 1
            continue
        # A retrieval of the caller's project's memory shields regardless
        # of which session emitted it.
        if admission is not None and any(
            admission.same_project(rid) for rid in _retrieved_ids(ev)
        ):
            count += 1
    return count


def _retrieved_ids(event: Mapping[str, Any]) -> list[str]:
    """The record ids a retrieval event names, whatever field holds them."""
    out: list[str] = []
    for key in ("returned", "memory_ids", "hit_ids", "shown_ids", "ids", "id"):
        out.extend(rid for _, rid in _event_id_items(event.get(key)))
    return out


__all__ = [
    "ATTRIBUTION_LOOKBACK_SECONDS",
    "DEFAULT_CREATION_SHIELD_SECONDS",
    "DEFAULT_LOOKBACK_SECONDS",
    "MIN_PROBE_CONTENT_TOKENS",
    "NO_OWNER_PROMPT",
    "REAUDIT_DEDUP_WINDOW_SECONDS",
    "THRESHOLD_RULE_V1",
    "MissHit",
    "MissReport",
    "Verdict",
    "is_duplicate_audit",
    "no_owner_prompt_report",
    "probe_for_miss",
    "search_miss_fields",
    "turn_audited_fields",
]
