"""The recall surface: what bettermemory puts in front of a model without
a tool call, on two hook moments.

- The session brief (`session_brief`), on SessionStart inside a
  project: the caller's standing records and the records the
  conversation bears on, eight of each at most (RS-F7 part 4, Stage C's
  decision of 2026-10-24). STANDING is the project's records answered
  standing or ambient and in force (the SESSION_BRIEF surface of design
  3.5), WORK its other in-force records; each list is ranked by the
  owner's words in the transcript before the start
  (`transcript.owner_words`) with production's ranker, the newest
  filling what the ranker leaves, and by recency alone at a startup with
  no words yet. Each line is an id and an 80-character summary, a
  standing line marked ``[in force]``, under a header that says they are
  pointers, not instructions. No body, no scope list, nothing outside a
  project, nothing for a caller nothing could be read for (its row says
  so), nothing for an empty selection.
- The prompt recall (`prompt_recall`), on UserPromptSubmit: the owner's
  words as the one definition reads them (`owner_prompt.owner_text`: the
  app's envelope blocks removed, cut at 2,000 characters, nothing for a
  prompt the harness composed), probed through the audit's own probe
  (`_probe_message`: the production ranker, top three, over the
  pool the mode admits: the hooks' prefilter under ``"project"``, the
  unscoped search's under ``"everywhere"``),
  and up to three of those hits delivered as pointers when they are
  relevant (medium or high), written or imported here, admitted for the
  caller (under `prompt_recall = "project"` the PROMPT_RECALL surface of
  design 3.5, the caller's own project; under ``"everywhere"`` the
  unscoped search's admission, decision B21), not contradicted, and not
  already shown, retrieved, written or updated in this session segment.

Both are pointers by construction. The read path's staleness machinery
lives on `memory_show`; a body delivered around it would be the one read
surface that skips verification, so the model is told to fetch, and the
fetch carries the signals.

Per-session state (`RecallState`) replaces the old 600-second
any-retrieval shield for delivery: a session segment shows no id twice,
by either surface, and delivers at most twelve recalls. A SessionStart
with source ``clear`` opens a new segment; one with source ``compact``
opens one only on a compaction marker the transcript holds, newer than
the one the session's segments last opened on, and is otherwise kept as
a signal a later read of the transcript confirms
(`confirm_compaction`); ``startup`` and ``resume`` keep the segment. The
brief on a resume, and on a compact without its marker, omits the ids
its segment has shown and records them as ``omitted``. The state lives
in the daemon, lock-guarded, rebuilt from the session's rows in the log
after a restart or after a day idle (`rebuild_session`, by session, so
the held and the rebuilt state agree), and bounded at a thousand
sessions; the in-process CLI path, which has no daemon, computes it from
the log on every call. The Stop audit keeps its own shield unchanged: a
delivered `prompt_recall` is still a retrieval kind there, so the turn
it served is not a silent miss.

Telemetry is the gate for both: each show is recorded (`session_brief`,
`prompt_recall`) at show time, and with `telemetry.enabled = false`
nothing is shown, since an unlogged injection could be neither measured
nor seen by the audit.

The brief reads no verdict and runs no git process: a pointer's
staleness is `memory_show`'s to report when the pointer is opened. The
rc6 brief selected the newest rows `memory_show` would read fresh,
through `verdict.memoised_verdicts` and a background fill; Stage C
(2026-10-24, 41 session starts replayed on a copy of the live store,
one reply each, judged blind) read that brief as not separable from no
brief (-0.07 [-0.33, +0.16]) and the standing and work lists at +0.45
[+0.11, +0.82], so the selection and its machinery left with rs-v6.

The per-session state and the vocabulary its rows share live in
`recall_state`; this module re-exports the names callers import from
it, so every import path is unchanged.
"""

from __future__ import annotations

import logging
import os
import shlex
import threading
from collections import OrderedDict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import identity
from . import origin as _origin
from . import standing as _standing
from .audit import (
    ATTRIBUTION_LOOKBACK_SECONDS,
    REAUDIT_DEDUP_WINDOW_SECONDS,
    MissHit,
    MissReport,
    probe_for_miss,
    prompt_recall_fields,
    recall_held_fields,
    turn_audited_fields,
)
from .config import Config, load_config
from .events import HARNESS_SESSION_FIELD, Recorder, redact_query
from .models import Memory, generate_ulid, utcnow
from .origin import (
    Origin,
    repos_match,
    should_include_for_caller,
    worktrees_match,
)
from .owner_prompt import PROMPT_CUT_CHARS, owner_text, probe_text
from .recall_state import (
    DEFAULT_HARNESS,
    REVIEW_UNANSWERED,
    RULE,
    STATE_IDLE_SECONDS as STATE_IDLE_SECONDS,
    STATE_MAX_SESSIONS as STATE_MAX_SESSIONS,
    SUMMARY_CHARS,
    SURFACE_BRIEF,
    SURFACE_RECALL,
    _DELIVERED_PROVENANCE as _DELIVERED_PROVENANCE,
    RecallState,
    SegmentView as SegmentView,
    _str_ids,
    is_settlement,
    rebuild_session as rebuild_session,
)
from .search import SearchMode
from .search import search as rank_search
from .sessions import (
    _disabled_scopes_from_events,
    bridged_session,
    event_joined,
    joined_sessions,
)
from .store import LOCAL, STORE_FILENAME, Store, TrustRow
from .time_utils import ensure_utc, isoformat_utc, parse_event_ts
from .transcript import CompactMarker, compact_marker, interrupted_turns, owner_words
from .admission import NO_RESOLUTION, Admission, admitted_nothing
from .in_force import InForce, in_force
from .location import Exchange
from .membership import (
    POOL_PREFILTER,
    PROMPT_RECALL,
    SEARCH_AUTO_SCOPE,
    SEARCH_UNSCOPED,
    SESSION_BRIEF,
    UNRESOLVED,
    Membership,
    Resolution,
)

if TYPE_CHECKING:
    from .handlers.search import SearchPool

log = logging.getLogger("bettermemory.recall")

__all__ = [
    "BRIEF_PROBE_CHARS",
    "BRIEF_STANDING",
    "BRIEF_WORK",
    "COMPACT_MARKER_WINDOW_SECONDS",
    "DEFAULT_HARNESS",
    "HookResult",
    "PROMPT_CUT_CHARS",
    "RECALL_CAP_CHARS",
    "RECALL_MAX_POINTERS",
    "REVIEW_UNANSWERED",
    "RULE",
    "RecallPointer",
    "RecallState",
    "SURFACE_BRIEF",
    "SURFACE_RECALL",
    "admission_for",
    "membership_model",
    "caller_admission",
    "clean_prompt",
    "confirm_compaction",
    "disputed_marks",
    "is_settlement",
    "previous_audit_at",
    "prompt_recall",
    "render_brief",
    "render_recall_block",
    "review_unanswered",
    "session_brief",
    "settle_interrupted",
    "settle_shown",
    "summarize",
]


# -- the brief ----------------------------------------------------------------

# Stage C (2026-10-24): 41 session starts since 2026-10-12 replayed on a
# copy of the live store, one reply each under six briefs, the replies
# judged blind; the paired score against no brief, 95% bootstrap by
# session. The standing records alone at 3 (+0.19 [-0.14, +0.52]), 3
# standing and 3 work (+0.16 [-0.17, +0.55]) and rc6's newest-fresh
# eight (-0.07 [-0.33, +0.16]) did not clear zero; 8 standing and 8 work
# did (+0.45 [+0.11, +0.82]), as did the oracle of graded-relevant
# pointers (+0.46 [+0.10, +0.88]). The brief is that condition.
BRIEF_STANDING = 8
BRIEF_WORK = 8
# The owner's words before the start the lists are ranked by: the
# length Stage C ranked with (Stage B set 12,000 as the conversation
# tail the agent and the judges saw).
BRIEF_PROBE_CHARS = 12_000
# How many withheld candidates a `recall_suppressed` row names (design
# 4, failure 8): the rc6 brief's count, kept as the row's size.
_SUPPRESSED_NAMED = 8

# SessionStart sources (Claude Code's `source` field). Anything else
# reads as a startup.
_SESSION_SOURCES: frozenset[str] = frozenset({"startup", "resume", "compact", "clear"})
_NEW_SEGMENT_SOURCES: frozenset[str] = frozenset({"compact", "clear"})

# How far from a compact SessionStart its compaction marker (the
# transcript's `compact_boundary` row) may be stamped. Measured on 2026-10-19
# over the 92 compact SessionStart rows since 2026-10-12 and the 49
# distinct boundaries their transcripts hold: 45 boundaries had a compact
# row within 2.5 seconds of their stamp (13 rows stamped up to 2.46 s
# before the boundary, 32 up to 1.22 s after it), the other 4 none within
# 120 s, and the other 47 compact rows had no boundary within 120 s. The
# boundary reaches the file only after the hook has run (in each of the
# 45 pairs a row written with it is stamped after the hook row, up to
# 29.2 s after), so a SessionStart rarely reads its own marker and a
# later read confirms it. Any window from 2.5 s to 120 s parts the
# measured rows alike. The window bounds the marker's stamp, not the
# read: the signal waits for whichever later read finds its marker. A
# deadline on the read would be applied by reads that record nothing (a
# prompt nothing is delivered for), which the log, and so the rebuilt
# state, never sees.
COMPACT_MARKER_WINDOW_SECONDS = 60

# -- the prompt recall --------------------------------------------------------

# The probe's top three, each delivered unless a hold names it (RS-F7 part
# 0). Stage B (2026-10-22, 60 prompts, replies judged blind) measured the
# paired reply score against no block: the top 3 +0.39 [+0.17, +0.62],
# above the top 5 by +0.27 [+0.07, +0.47] and the top 1 by +0.25 [+0.05,
# +0.45] on prompts with a relevant record; the top 2 was not separable
# from it, and Stage A's replay reaches 69 of 100 prompts with three
# against 61 with two. No hold reads the coverage label since rs-v6: in
# Stage A the labels did not separate relevant from irrelevant records
# (high 0.34, medium 0.30, low 0.28), and no segment cap counts
# deliveries (the live log's 202 segments reached twelve once).
RECALL_MAX_POINTERS = 3
# The block's format as Stage B tested it; alternatives are unmeasured.
RECALL_CAP_CHARS = 900
SNIPPET_CHARS = 100
# A snippet cut shorter than this to fit the cap is dropped instead: a
# dozen characters of matched text tells the model nothing.
_MIN_SNIPPET_CHARS = 12

#: The words the prompt hook keeps per checkout, for a join to rest on.
WORDS_PER_CHECKOUT = 20
WORDS_CHECKOUTS = 500


class WordsSeen:
    """The user's own words, as the prompt hook saw them, per store and
    checkout: the last `WORDS_PER_CHECKOUT` prompts of each, in this
    process only, never written anywhere. A join of two projects rests on
    them (design 8.8, `handlers.questions`): the agent quotes the user,
    and the quote must be in a prompt the hook saw in that checkout. A
    process that saw no prompt for a checkout (no hook installed) holds
    nothing for it, which `seen` tells apart from a quote not found."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._words: OrderedDict[tuple[str, str], deque[str]] = OrderedDict()

    def remember(self, store_path: object, checkout: str, words: str) -> None:
        key = (str(store_path), checkout)
        with self._lock:
            held = self._words.get(key)
            if held is None:
                held = deque(maxlen=WORDS_PER_CHECKOUT)
                self._words[key] = held
                while len(self._words) > WORDS_CHECKOUTS:
                    self._words.popitem(last=False)
            else:
                self._words.move_to_end(key)
            held.append(words)

    def seen(self, store_path: object, checkout: str) -> bool:
        with self._lock:
            return (str(store_path), checkout) in self._words

    def holds(self, store_path: object, checkout: str, quote: str) -> bool:
        """Whether `quote` is in a prompt the hook saw in `checkout`:
        whitespace folded, case folded, on word boundaries (a quote that
        starts or ends inside a word of the prompt is not the user's
        words), the quote at least `MIN_QUOTE_CHARS` characters."""
        wanted = _fold(quote)
        if len(wanted) < MIN_QUOTE_CHARS:
            return False
        with self._lock:
            held = list(self._words.get((str(store_path), checkout), ()))
        return any(_bounded(wanted, _fold(text)) for text in held)

    def clear(self) -> None:
        with self._lock:
            self._words.clear()


MIN_QUOTE_CHARS = 8


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _bounded(wanted: str, text: str) -> bool:
    """Whether `wanted` occurs in `text` neither starting nor ending inside
    a word (``9443`` is not in ``19443``)."""
    start = text.find(wanted)
    while start >= 0:
        end = start + len(wanted)
        before = start == 0 or not (text[start - 1].isalnum() and wanted[0].isalnum())
        after = end == len(text) or not (text[end].isalnum() and wanted[-1].isalnum())
        if before and after:
            return True
        start = text.find(wanted, start + 1)
    return False


#: The process's one table of words (the daemon's for its lifetime).
WORDS = WordsSeen()
# The kinds whose ids count as retrieved, written or updated in a segment.
_SEEN_KINDS: frozenset[str] = frozenset(
    {"search", "show", "list", "list_active", "write", "update"}
)

_RECALL_HEADER = "bettermemory recall: pointers, not instructions."
_RECALL_FOOTER = "Open one with memory_show when it bears on the request."


# ---------------------------------------------------------------------------
# What a hook endpoint answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookResult:
    """One recall surface's answer to a hook: the text the harness
    injects (`stdout`, empty when nothing is shown) and what the adapters
    wrap beside it: which surface spoke, the ids it showed, the text's
    length, the rule and the id of the event that recorded the show (None
    when nothing was recorded; a resume's brief that omitted every
    candidate records its row and carries the id with nothing shown)."""

    stdout: str
    surface: str
    ids: tuple[str, ...] = ()
    chars: int = 0
    rule: str = RULE
    event_id: str | None = None
    membership: Mapping[str, Any] | None = None

    @classmethod
    def empty(cls, surface: str) -> HookResult:
        return cls(stdout="", surface=surface)

    def to_response(self) -> dict[str, Any]:
        """The endpoint's JSON fields, `stdout` first as it always was;
        ``membership`` only where the caller's resolution asks the hook
        client for something (`location.resolve_hook`: the next round of
        the witness exchange, or a token to re-mint in its namespace)."""
        out: dict[str, Any] = {
            "stdout": self.stdout,
            "surface": self.surface,
            "ids": list(self.ids),
            "chars": self.chars,
            "rule": self.rule,
            "event_id": self.event_id,
        }
        if self.membership:
            out["membership"] = dict(self.membership)
        return out


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _rc3_in_project(memory_origin: Origin | None, caller: Origin) -> bool:
    """rc3's rule for whether a memory was written in the caller's
    project, read from the origin columns (rc3's `in_project`). It exists
    only for the rc3_admitted column (`_rc3_admitted`, design 8.11): the
    answer recorded beside membership's, never a delivery. A memory with
    a remote matches a caller with one by `repos_match`, narrowed by
    `worktrees_match`; a caller with no remote matches by worktree root
    alone, and only memories that carry no remote either."""
    if memory_origin is None:
        return False
    if caller.repo is not None:
        if memory_origin.repo is None:
            return False
        if not repos_match(memory_origin.repo, caller.repo):
            return False
        return worktrees_match(memory_origin.worktree_root, caller.worktree_root)
    if caller.worktree_root is None:
        return False
    return (
        memory_origin.repo is None
        and memory_origin.worktree_root == caller.worktree_root
    )


def _rc3_admitted(
    memories: Iterable[Memory], caller: Origin, *, project_only: bool
) -> list[str]:
    """The ids of `memories`, in their order, that rc3's rule would have
    delivered to `caller`, read from the origin columns: the rc3_admitted
    column of the `session_brief` and `prompt_recall` rows (design 8.11).
    It exists only for that column; no surface delivers by it.

    rc3 delivered nothing to a caller git could not read. Under
    `project_only` (the brief, and the recall under ``prompt_recall =
    "project"``) it delivered only inside a repository and only what
    `_rc3_in_project` admits; under ``"everywhere"`` what the search's
    auto-scope admitted (`origin.should_include_for_caller` against the
    caller's remote, every record for a caller with none)."""
    if caller.git_indeterminate:
        return []
    if project_only and caller.repo is None and caller.worktree_root is None:
        return []
    admitted: list[str] = []
    for memory in memories:
        if project_only:
            ok = _rc3_in_project(memory.origin, caller)
        else:
            ok = caller.repo is None or should_include_for_caller(
                memory.origin,
                caller.repo,
                caller_worktree_root=caller.worktree_root,
            )
        if ok:
            admitted.append(memory.id)
    return admitted


def _delivery_membership(
    resolution: Resolution | None, admission: Admission
) -> dict[str, Any]:
    """The membership keys a `session_brief` or `prompt_recall` row carries
    from rs-v5 (design 8.11): ``checkout``, the caller's checkout (None
    where its resolution names none), and ``membership_rule``, the rule its
    resolution named, or the outcome that admitted it nothing (a caller
    nothing could be read for, a deferred read, a resolution that
    raised)."""
    if admission.nothing is not None:
        rule = admission.nothing
    else:
        rule = resolution.rule if resolution is not None else NO_RESOLUTION
    return {
        "checkout": resolution.checkout if resolution is not None else None,
        "membership_rule": rule,
    }


def summarize(body: str, limit: int = SUMMARY_CHARS) -> str:
    """The body, whitespace collapsed, cut to `limit` characters with an
    ellipsis when it was longer."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def disputed_marks(pairs: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{memory id: mark}`` for every member of a pending conflict pair.

    A memory is disputed when it is either side of any conflicts row
    whose status is ``pending``, whatever the detector. The mark names one
    pair (the lowest id, so the mark is stable) and, when the memory sits
    in several, how many more: ``[disputed: cf-0123456789ab]`` or
    ``[disputed: cf-0123456789ab +2]``, never more than 31 characters.
    Disputed rows are delivered with the mark, not held back: the
    disagreement is itself worth knowing, and the queue is where it is
    settled. A dismissed pair a later scan resurrects is pending again,
    so it marks again."""
    by_member: dict[str, list[str]] = {}
    for a_id, b_id, pair_id in pairs:
        for member in (a_id, b_id):
            listed = by_member.setdefault(member, [])
            if pair_id not in listed:
                listed.append(pair_id)
    out: dict[str, str] = {}
    for member, pair_ids in by_member.items():
        pair_ids.sort()
        more = len(pair_ids) - 1
        suffix = f" +{min(more, 99)}" if more else ""
        out[member] = f"[disputed: {pair_ids[0][:15]}{suffix}]"
    return out


def _unless_standing(
    store: Store, admission: Admission, contradicted: dict[str, datetime]
) -> dict[str, datetime]:
    """`contradicted` less the standing records': a ``contradicted`` use
    is the agent's word, and it does not take the user's rule out of
    delivery (unit RS-F7 part 5). Health still counts the mark."""
    if not contradicted:
        return contradicted
    standing = set(_standing.standing_ids(store, admission, list(contradicted)))
    return {k: v for k, v in contradicted.items() if k not in standing}


def _contradiction_open(memory: Memory, contradicted: Mapping[str, datetime]) -> bool:
    """Whether a `use` event with outcome ``contradicted`` names `memory`
    and no update or verify of it came later. An update bumps `updated`
    and a verify stamps `last_verified_at`, so the later of the two is
    when the record was last answered for (the rule `health` reads for
    its contradicted bucket)."""
    stamp = contradicted.get(memory.id)
    if stamp is None:
        return False
    answered = ensure_utc(memory.updated)
    verified = ensure_utc(memory.last_verified_at)
    if verified is not None and (answered is None or verified > answered):
        answered = verified
    return answered is None or stamp > answered


def _resolve_caller(
    *,
    store: Store,
    cfg: Config,
    cwd: Path | None,
    origin: Origin | None,
    posted: Mapping[str, Any] | None,
    session_id: str | None,
) -> Resolution | Exchange | None:
    """The caller's membership resolution (design 3.2) from what its hook
    posted (`location.resolve_hook`): an `Exchange` when the daemon cannot
    read the caller's namespace and asks its client for the next answer.
    The caller stands at `cwd`, else at the cwd of the `origin` it was
    given, else (in-process) where the request declared
    (`identity.workspace_declaration`) or at the process cwd. None, with
    a warning, when the resolution raised: a hook never fails its turn."""
    from .location import resolve_hook

    where: Path | None = cwd
    if where is None and origin is not None and origin.cwd:
        where = Path(origin.cwd)
    if where is None:
        declared = identity.workspace_declaration()
        if declared is not None:
            where = declared[0]
    try:
        return resolve_hook(
            store,
            cfg,
            None if where is None else str(where),
            posted,
            session=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the hook
        log.warning("membership resolution failed: %s: %s", exc.__class__.__name__, exc)
        return None


def membership_model(store: Store) -> Membership:
    """A membership model over `store` for this process's machine, which
    reads the homes and projects a hook's resolution is judged against
    (`location.resolve_hook` keeps its own model), and the recall review's
    silent stratum reads."""
    from .location import NO_MACHINE, process_machine

    try:
        machine = process_machine().id
    except OSError:
        # The caller was resolved without a machine (`location.NO_MACHINE`).
        machine = NO_MACHINE
    return Membership(store, machine)


def admission_for(
    surface: str, store: Store, resolution: Resolution | None
) -> Admission:
    """The admission of `surface` (design 3.5) for a hook's caller: one per
    request, built from its resolution. None (the resolution raised) is
    admitted nothing."""
    model = membership_model(store) if resolution is not None else None
    return Admission(surface, resolution, model)


def caller_admission(
    surface: str,
    *,
    store: Store,
    cfg: Config,
    cwd: Path | None,
    origin: Origin | None,
    posted: Mapping[str, Any] | None,
    session_id: str | None,
) -> tuple[Admission, Resolution | Exchange | None]:
    """The caller's resolution (`_resolve_caller`) and the admission of
    `surface` built from it. A namespace the daemon cannot read (an
    `Exchange`) and a resolution that raised are admitted nothing."""
    resolved = _resolve_caller(
        store=store,
        cfg=cfg,
        cwd=cwd,
        origin=origin,
        posted=posted,
        session_id=session_id,
    )
    resolution = resolved if isinstance(resolved, Resolution) else None
    return admission_for(surface, store, resolution), resolved


def membership_lines(store: Store, caller: Resolution | None) -> list[str]:
    """The lines a session's first output carries for its caller
    (`Membership.first_output`: L1 to L10, every open proposal of the
    checkout), or none."""
    if caller is None:
        return []
    return membership_model(store).first_output(caller)


def standing_line(store: Store, caller: Resolution | None) -> str:
    """The first-output line naming how many records the caller's
    auto-scoped search reads wait for a standing answer, and the call that
    shows them (`standing.backlog_line`, unit RS-F7 part 1); "" for
    none, and while the question is not asked (`standing.asking`)."""
    if caller is None or not _standing.asking():
        return ""
    waiting = _standing.backlog(store, admission_for(SEARCH_AUTO_SCOPE, store, caller))
    return _standing.backlog_line(waiting.count)


def _open_store(cfg: Config) -> Store | None:
    """The store the config names, opened for the in-process hook path, or
    None when there is none (a hook never creates one). A store holding
    records without homes is migrated at this open, once, when no daemon
    serves the hook (design 8.6); the migration logs its own lines."""
    from .cli._common import run_migration_at_open

    root = cfg.resolved_directory()
    if not (root / STORE_FILENAME).is_file():
        return None
    store = Store.open_or_create(root / STORE_FILENAME)
    run_migration_at_open(store, cfg)
    return store


# ---------------------------------------------------------------------------
# The session brief
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BriefSelection:
    """The brief's two lists in rank order, or why there is none."""

    standing: list[Memory]
    work: list[Memory]
    #: "probe" when the owner's words ranked the lists, "recency" when
    #: there were none.
    ranked_by: str
    #: Each shown record's in-force note by id (`_in_force_marks`: the
    #: newest record extending it), for the records that have one.
    notes: dict[str, str] = field(default_factory=dict)
    suppressed: tuple[dict[str, str], ...] = ()
    candidates: int = 0


def _suppressed_reason(admission: Admission, memory_id: str) -> str:
    """Why a surface did not deliver a record, as `Membership.admit_recall`
    names it: ``no_home``, ``unresolved`` (decision B6), else the home's
    relation to the caller."""
    home = admission.home(memory_id)
    if home is None:
        return "no_home"
    if home.unresolved:
        return UNRESOLVED
    return admission.relation(home)


def _ranked_by(probe: str) -> str:
    return "probe" if probe.strip() else "recency"


def _delivered(store: Store, memories: Sequence[Memory]) -> list[Memory] | None:
    """`memories` whose verified label is local or imported, in order; None
    when the store verifies no label (opened without its key), so the
    brief shows nothing rather than vouch for rows it cannot read."""
    if not memories:
        return []
    trust = store.trust_rows([memory.id for memory in memories])
    if trust is None:
        return None
    return [
        memory
        for memory in memories
        if (row := trust.get(memory.id)) is not None
        and row.provenance in _DELIVERED_PROVENANCE
    ]


def _ranked(
    memories: Sequence[Memory],
    probe: str,
    *,
    limit: int,
    admission: Admission,
    pool: SearchPool,
    cfg: Config,
    now: datetime,
) -> list[Memory]:
    """The top `limit` of `memories` by `probe` under production's ranker
    (`search.search`, the configured mode and half-life, the pool's
    corpus statistics), the newest by ``updated`` filling the places the
    ranker leaves (the ranker's browse order; every place when `probe` is
    empty): Stage C's ranking."""
    if not memories:
        return []
    rows = list(memories)
    by_id = {memory.id: memory for memory in rows}
    mode = cast(SearchMode, cfg.behavior.search_mode or "hybrid")
    picked: list[str] = []
    if probe.strip():
        picked = [
            hit.id
            for hit in rank_search(
                rows,
                probe,
                admission=admission,
                max_results=limit,
                now=now,
                half_life_days=cfg.behavior.recency_boost_half_life_days,
                mode=mode,
                corpus_stats_provider=pool.corpus_stats_provider,
                conversational=cfg.behavior.conversational,
            )
        ]
    if len(picked) < limit:
        for hit in rank_search(
            rows,
            "",
            admission=admission,
            max_results=len(rows),
            now=now,
            half_life_days=cfg.behavior.recency_boost_half_life_days,
            mode=mode,
            allow_empty_query=True,
            conversational=cfg.behavior.conversational,
        ):
            if hit.id not in picked:
                picked.append(hit.id)
    return [by_id[memory_id] for memory_id in picked[:limit]]


def _select_brief(
    store: Store,
    *,
    admission: Admission,
    probe: str,
    cfg: Config,
    now: datetime,
) -> _BriefSelection:
    """The brief's lists (RS-F7 part 4, Stage C's decision).

    STANDING: the caller's standing records in force (`standing_ids`
    under the SESSION_BRIEF `admission`: answered standing for the
    current body, or category ambient; not history), label local or
    imported. WORK: the admitted in-force records that are not standing,
    from the production pool over `probe` (`resolve_search_pool` under
    the admission on the pool prefilter surface, which admits more than
    the brief does, so each row is re-admitted here), the same label
    rule, history excluded (`in_force`), an open contradiction held
    (`_contradiction_open`; a standing record is never held, part 5).
    Each list is ranked by the probe (`_ranked`), `BRIEF_STANDING` and
    `BRIEF_WORK` at most; a shown record another in-force record extends
    carries that record's id as its note (part 3).

    When both lists are empty and the store holds candidates of a
    deliverable label the admission withholds, the selection names the
    newest `_SUPPRESSED_NAMED` of them with why each was withheld, and how
    many there were, for the `recall_suppressed` row (design 4, failure
    8); a candidate the admission admits is not withheld and is not
    named. A store opened without its key verifies no label, so it shows
    nothing."""
    # Imported here: the handlers import this module's word table
    # (`handlers.questions`), so a module-level import would be a cycle.
    from .handlers.search import default_search_width, resolve_search_pool

    pool = resolve_search_pool(
        store,
        probe,
        excluded_scopes=set(),
        admission=admission.on(POOL_PREFILTER),
        min_survivors=default_search_width(cfg.behavior),
    )
    standing_ids = _standing.standing_ids(store, admission)
    standing = _delivered(store, store.load_many(standing_ids) if standing_ids else [])
    excluded = set(standing_ids)
    admission.prime([memory.id for memory in pool.memories])
    work = _delivered(
        store,
        [
            memory
            for memory in pool.memories
            if memory.id not in excluded and admission.admits_id(memory.id)
        ],
    )
    if standing is None or work is None:
        return _BriefSelection([], [], _ranked_by(probe))
    forced: dict[str, InForce] = {}
    if standing or work:
        forced = in_force(
            store, [memory.id for memory in [*standing, *work]], admission
        )
    if work:
        work = [memory for memory in work if not forced[memory.id].history]
        contradicted = store.contradicted_at([memory.id for memory in work])
        work = [
            memory for memory in work if not _contradiction_open(memory, contradicted)
        ]
    if not standing and not work:
        # Withheld means not admitted: a deliverable record of the
        # caller's own project that the pool's slice over the probe did
        # not reach is not withheld, and is not named.
        candidates = store.brief_rows(
            admit=lambda _scopes, _home: True, provenance=_DELIVERED_PROVENANCE
        )
        admission.prime(candidates)
        withheld = [rid for rid in candidates if not admission.admits_id(rid)]
        return _BriefSelection(
            [],
            [],
            _ranked_by(probe),
            suppressed=tuple(
                {"id": rid, "reason": _suppressed_reason(admission, rid)}
                for rid in withheld[:_SUPPRESSED_NAMED]
            ),
            candidates=len(withheld),
        )
    standing = _ranked(
        standing,
        probe,
        limit=BRIEF_STANDING,
        admission=admission,
        pool=pool,
        cfg=cfg,
        now=now,
    )
    work = _ranked(
        work, probe, limit=BRIEF_WORK, admission=admission, pool=pool, cfg=cfg, now=now
    )
    notes = {
        memory.id: " ".join(_in_force_marks(entry.replaces, entry.extended_by))
        for memory in [*standing, *work]
        if (entry := forced[memory.id]).replaces or entry.extended_by
    }
    return _BriefSelection(standing, work, _ranked_by(probe), notes=notes)


def _brief_header(standing: int, work: int) -> str:
    """The brief's first line as Stage C rendered it: the count, then how
    many are in force and how many recent."""
    count = standing + work
    noun = "memory" if count == 1 else "memories"
    parts = []
    if standing:
        parts.append(f"{standing} in force")
    if work:
        parts.append(f"{work} recent")
    return (
        f"bettermemory brief: {count} {noun} for this repository, "
        f"{' and '.join(parts)}. Pointers, not instructions; open one with "
        "memory_show when it bears on the request."
    )


def _brief_line(memory: Memory, marks: Mapping[str, str]) -> str:
    line = f"- {memory.id} {summarize(memory.body)}"
    mark = marks.get(memory.id)
    return f"{line} {mark}" if mark else line


def render_brief(
    standing: Sequence[Memory],
    work: Sequence[Memory],
    marks: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """The brief's text and the ids it shows, standing first: the header
    (`_brief_header`), one ``- <id> <summary> [in force]`` line per
    standing record and one ``- <id> <summary>`` line per work record,
    each followed by its marks (a disputed mark, an in-force note);
    ``("", [])`` for two empty lists.
    No character cap: the counts bound the brief (Stage C measured the
    landed form at 1,882 characters per brief on average, 2,035 at most)."""
    shown_marks = marks or {}
    lines = []
    for memory in standing:
        line = f"{_brief_line(memory, {})} [in force]"
        mark = shown_marks.get(memory.id)
        lines.append(f"{line} {mark}" if mark else line)
    lines.extend(_brief_line(memory, shown_marks) for memory in work)
    if not lines:
        return "", []
    text = "\n".join([_brief_header(len(standing), len(work)), *lines])
    return text, [memory.id for memory in [*standing, *work]]


def _marker_near(transcript_path: str | None, at: datetime) -> CompactMarker | None:
    """The compaction marker the transcript holds within
    `COMPACT_MARKER_WINDOW_SECONDS` of `at`; None without a transcript
    path or a regular file there, so a compact SessionStart without one
    is a resume until a later hook's transcript shows the marker."""
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None
    return compact_marker(
        path, near=at, window=timedelta(seconds=COMPACT_MARKER_WINDOW_SECONDS)
    )


def confirm_compaction(
    *,
    store: Store,
    state: RecallState,
    key: tuple[str, str],
    transcript_path: str | None,
    cfg: Config,
    triggered_from: str,
    now: datetime,
    origin: Origin | None = None,
    cwd: Path | None = None,
) -> SegmentView | None:
    """Resolve the session's pending compact signal, if it holds one,
    against the transcript: the segment a compaction opened, or None.

    Every hook reads the segment after this: the SessionStart, the prompt
    recall and the Stop. A compact SessionStart rarely reads its own
    marker, which reaches the file after the hook has run, so the signal
    waits for a later read: one that finds a marker within the window of
    the signal, other than the one the segments last opened on, opens
    the segment at the signal, holding what was shown since
    (`RecallState.resolve_compaction`), and records `segment_opened` with
    the ``marker`` and the ``opened_at`` of the signal, stamped
    `triggered_from` by the hook that read it. A read that finds none
    leaves the signal for the next, however late: the signal ends only on
    a row the log holds (its confirmation, a clear, another compact), so
    the state rebuilt from the log holds it exactly when the daemon does.
    While a signal is pending each hook reads the transcript once more
    (`transcript.compact_marker`, 64 MiB at most). With telemetry off
    nothing is resolved and nothing recorded, as nothing is shown."""
    if not cfg.telemetry.enabled:
        return None
    at = state.pending_compaction(store, key, now=now)
    if at is None:
        return None
    marker = _marker_near(transcript_path, at)
    opened = state.resolve_compaction(key, at=at, marker=marker)
    if opened is None:
        return None
    caller = origin if origin is not None else _origin.capture_caller(cwd)
    settle_unused(
        store=store,
        state=state,
        key=key,
        now=now,
        worktree_root=caller.worktree_root,
        triggered_from=triggered_from,
    )
    Recorder(
        store=store,
        session_id=key[1],
        enabled=cfg.telemetry.enabled,
        worktree_root=caller.worktree_root,
        triggered_from=triggered_from,
    ).record(
        "segment_opened",
        session_id=key[1],
        harness=key[0],
        source="compact",
        segment=opened.segment,
        marker=opened.marker,
        opened_at=isoformat_utc(at),
        rule=RULE,
    )
    return opened


def session_brief(
    *,
    session_id: str | None,
    source: str | None = None,
    harness: str | None = None,
    transcript_path: str | None = None,
    cwd: Path | None = None,
    origin: Origin | None = None,
    config: Config | None = None,
    store: Store | None = None,
    state: RecallState | None = None,
    now: datetime | None = None,
    posted: Mapping[str, Any] | None = None,
    resolution: Resolution | None = None,
) -> HookResult:
    """The SessionStart surface: open the session's segment (a new one on
    ``clear``, and on ``compact`` when its marker is read) and, inside a
    project, return the brief.

    Nothing is shown without a session id (the show has no session to be
    recorded under or to count against), with `session_brief` or
    telemetry off, outside a project, for a caller nothing could be read
    for (its row says so, `_brief_in_segment`), on an empty store, or
    when no row passes (`_select_brief`). A shown brief is recorded as a
    `session_brief` event under the session id, stamped
    ``triggered_from="session_hook"`` so the hooks' server-session
    anchor (`sessions._latest_in_process_session`) skips it like every other
    out-of-process row; it is not a retrieval kind, so the Stop audit's
    shield does not count it. Its ids join the segment's shown set, so
    neither surface points at them again in the segment. On a ``resume``,
    which keeps the segment, the brief omits the ids the segment has
    shown and records them as ``omitted``; when every candidate is
    omitted the row is still recorded, with no ids, and nothing is
    printed.

    A ``compact`` opens a segment only when the transcript holds a
    compaction marker near it other than the one the session's segments
    last opened on (`RecallState.open_segment`); its row then names the
    ``marker``. Without one it keeps the segment, as a resume does, its
    row carries ``marker_absent: true``, and a later read of the
    transcript may still open the segment (`confirm_compaction`, which
    runs first here too). A SessionStart without a transcript path reads
    no marker, so its compact is a resume.

    A ``compact`` or ``clear`` that shows no brief, with telemetry on,
    records a `segment_opened` row instead (the session id, harness,
    source, the segment's number and the compact's ``marker`` or
    ``marker_absent``), so the segment, or the pending signal, is read
    back after a restart and on the in-process path, which rebuilds the
    state from the log on every call.

    One line leads whatever is shown, or stands alone, while the store
    was created fresh after a daemon saved the one it lost
    (`orphan_notice`): it is a data-loss alarm, so it shows with
    `session_brief` or telemetry off, outside a project and on an
    empty store as well, given a session id.

    The caller's membership is resolved first (`location.resolve_hook`
    over what the hook `posted`, or `resolution` when the caller already
    holds it), and the lines of its first output (`membership_lines`:
    L1 to L10 and every open proposal of the checkout, design 5.4), then
    the line naming the records that wait for a standing answer
    (`standing_line`), come right after that notice and before the brief,
    with the brief's switches off as well. Where the daemon cannot read the caller's
    namespace the result carries the exchange's next question in
    ``membership`` and nothing else runs."""
    empty = HookResult.empty("session_brief")
    if not session_id:
        return empty
    cfg = config or load_config(None)
    if store is None:
        store = _open_store(cfg)
        if store is None:
            return empty
    caller_resolution: Resolution | Exchange | None = resolution
    if caller_resolution is None:
        caller_resolution = _resolve_caller(
            store=store,
            cfg=cfg,
            cwd=cwd,
            origin=origin,
            posted=posted,
            session_id=str(session_id),
        )
    if isinstance(caller_resolution, Exchange):
        return HookResult(
            stdout="",
            surface="session_brief",
            membership={"ask": caller_resolution.to_reply()},
        )
    lines = membership_lines(store, caller_resolution)
    waiting = standing_line(store, caller_resolution)
    if waiting:
        lines.append(waiting)
    state = state if state is not None else RecallState()
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    opened_by = source if source in _SESSION_SOURCES else "startup"
    confirm_compaction(
        store=store,
        state=state,
        key=key,
        transcript_path=transcript_path,
        cfg=cfg,
        triggered_from="session_hook",
        now=now,
        origin=origin,
        cwd=cwd,
    )
    marker = _marker_near(transcript_path, now) if opened_by == "compact" else None
    view, opened = state.open_segment(
        store, key, source=opened_by, now=now, marker=marker
    )
    if opened and cfg.telemetry.enabled:
        settle_unused(
            store=store,
            state=state,
            key=key,
            now=now,
            worktree_root=(
                origin if origin is not None else _origin.capture_caller(cwd)
            ).worktree_root,
            triggered_from="session_hook",
        )
    compaction: dict[str, Any] = {}
    if opened_by == "compact":
        compaction = {"marker": view.marker} if opened else {"marker_absent": True}
    result = _brief_in_segment(
        key=key,
        view=view,
        opened_by=opened_by,
        compaction=compaction,
        transcript_path=transcript_path,
        cwd=cwd,
        origin=origin,
        cfg=cfg,
        store=store,
        state=state,
        now=now,
        resolution=caller_resolution,
    )
    if (
        result.event_id is None
        and opened_by in _NEW_SEGMENT_SOURCES
        and cfg.telemetry.enabled
    ):
        # No brief carried the segment's number, or the compact's marker
        # or its absence, to the log: record them alone, or a restarted
        # daemon and the in-process path (which rebuilds on every call)
        # would never see the clear or the compact.
        caller = origin if origin is not None else _origin.capture_caller(cwd)
        Recorder(
            store=store,
            session_id=key[1],
            enabled=cfg.telemetry.enabled,
            worktree_root=caller.worktree_root,
            triggered_from="session_hook",
        ).record(
            "segment_opened",
            session_id=key[1],
            harness=key[0],
            source=opened_by,
            segment=view.segment,
            rule=RULE,
            **compaction,
        )
    lead = "\n".join(lines)
    if lead:
        stdout = f"{lead}\n{result.stdout}" if result.stdout else lead
        result = replace(result, stdout=stdout, chars=len(stdout))
    notice = orphan_notice(store)
    if notice:
        stdout = f"{notice}\n{result.stdout}" if result.stdout else notice
        result = replace(result, stdout=stdout, chars=len(stdout))
    if caller_resolution is not None and caller_resolution.remint_token:
        # The checkout's token is the client's to re-mint, in its own
        # namespace (design 3.3, E21 g).
        result = replace(result, membership={"remint": True})
    return result


#: The most characters a path in the orphan notice takes; a longer one
#: keeps its two ends. `/health` lists the full paths.
_NOTICE_PATH_CHARS = 200


def _home_as_tilde(path: Path) -> str:
    """`path` with the home directory as ``~``."""
    shown = str(path)
    home = str(Path.home())
    if home not in ("", os.sep) and (
        shown == home or shown.startswith(home.rstrip(os.sep) + os.sep)
    ):
        shown = "~" + shown[len(home.rstrip(os.sep)) :]
    return shown


def _notice_path(path: Path) -> str:
    """`path` for the orphan notice: the home directory as ``~``, and no
    longer than `_NOTICE_PATH_CHARS`."""
    shown = _home_as_tilde(path)
    if len(shown) > _NOTICE_PATH_CHARS:
        half = (_NOTICE_PATH_CHARS - 3) // 2
        shown = f"{shown[:half]}...{shown[-half:]}"
    return shown


def _down_command(store: Store) -> str:
    """``bettermemory down`` for `store`, always prefixed with its
    directory as ``BETTERMEMORY_DIR``: `down` stops the daemon of the
    store its caller resolves, which depends on the shell's environment,
    config and working directory (a project store), so only the explicit
    directory is right from anywhere. The ``~`` survives quoting, since
    `down` expands it itself."""
    directory = store.path.parent.resolve()
    return (
        f"BETTERMEMORY_DIR={shlex.quote(_home_as_tilde(directory))} bettermemory down"
    )


def orphan_notice(store: Store) -> str:
    """The line the brief leads with while the store was created fresh
    after a daemon saved the one it lost (`Store.store_orphans`), naming
    the newest saved file and the restore steps in the order that works;
    empty otherwise. Shown whatever else the brief holds, since the fresh
    store is usually empty, and never acted on.

    The steps are all needed: an open session's shim starts a daemon
    again on its next call, and a WAL left beside the store is read over
    the copied file, which then reads as the fresh store."""
    orphans = store.store_orphans()
    if not orphans:
        return ""
    down = _down_command(store)
    name = store.path.name
    return (
        f"bettermemory: {_notice_path(store.path)} was created fresh after the "
        f"previous store was lost; that store is saved beside it as "
        f"{orphans[-1].name}. To restore it: close every session using "
        f"bettermemory (Claude Code, Codex, ...), run `{down}`, delete "
        f"{name}-wal and {name}-shm there, then copy the saved file over {name}."
    )


def _brief_in_segment(
    *,
    key: tuple[str, str],
    view: SegmentView,
    opened_by: str,
    compaction: Mapping[str, Any],
    transcript_path: str | None,
    cwd: Path | None,
    origin: Origin | None,
    cfg: Config,
    store: Store,
    state: RecallState,
    now: datetime,
    resolution: Resolution | None = None,
) -> HookResult:
    """The brief for the segment `session_brief` just opened or resumed,
    recorded when shown; empty when nothing is.

    What the brief may show is the SESSION_BRIEF surface's admission for
    the caller's `resolution` (design 3.5): its own project's memories
    (the home on the caller's chain), nothing for a caller outside any
    project. A caller whose resolution raised (None), read nothing
    (INDETERMINATE) or is deferred (DEFERRED) is shown nothing and its
    row says so (``membership: "admitted nothing: <outcome>"``, no ids);
    the origin columns never stand in. A brief whose candidates the
    surface admitted none of writes one `recall_suppressed` membership row
    (``surface: session_brief``, design 4 failure 8).

    The cooldown's rule, on the brief: an id the segment has shown (by
    either surface) is not printed again. The selection is split after
    `_select_brief` and before `render_brief`, in selection order, into
    the ids kept and the ids omitted, under the state's lock
    (`RecallState.claim_brief`), which notes the kept ids as shown in the
    same step: a prompt recall of the same session running at once can
    neither show an id the brief is printing nor have the brief print one
    it has just delivered. The freed slots are not filled, so the
    selection stays the ranker's. The
    rule reads the shown set on every source: on a ``clear``, and on a
    ``compact`` that opened a segment on its marker, the set is empty by
    construction, on a ``startup`` for a session the state has not seen
    it is empty too, and a ``resume``, like a ``compact`` without its
    marker, keeps what its segment showed. `compaction` (the compact's
    ``marker`` or ``marker_absent``) rides on the row. A brief with
    nothing left to print still records its row, with no ids and
    `omitted` naming the selection's, so the log shows the rule acted and
    on what; nothing is recorded when the selection itself is empty.

    From rs-v5 every row carries the caller's ``checkout`` and
    ``membership_rule`` (`_delivery_membership`) and ``rc3_admitted``:
    the printed ids rc3's rule would have delivered (`_rc3_admitted`,
    design 8.11), empty on the row of a caller admitted nothing.

    From rs-v6 the brief is STANDING and WORK (`_select_brief`), ranked by
    the owner's words in the transcript before this start
    (`transcript.owner_words`, the newest `BRIEF_PROBE_CHARS`); the row
    carries the two lists as printed (``standing``, ``work``), the probe
    as `redact_query` renders it (``probe_query``) and ``ranked_by``
    ("probe", or "recency" when there were no words)."""
    empty = HookResult.empty("session_brief")
    if not cfg.behavior.session_brief or not cfg.telemetry.enabled:
        return empty
    caller = origin if origin is not None else _origin.capture_caller(cwd)
    if store.count_memories() == 0:
        return empty
    admission = admission_for(SESSION_BRIEF, store, resolution)
    membership = _delivery_membership(resolution, admission)

    def record(
        ids: list[str],
        *,
        chars: int,
        extra: Mapping[str, Any],
        event_id: str | None = None,
        segment: int | None = None,
    ) -> str:
        identity.bind_transcript(session_id=key[1], model=None, client=key[0])
        if event_id is None:
            event_id = generate_ulid()
        recorder = Recorder(
            store=store,
            session_id=key[1],
            enabled=cfg.telemetry.enabled,
            worktree_root=caller.worktree_root,
            triggered_from="session_hook",
        )
        fields: dict[str, Any] = {
            "event_id": event_id,
            "session_id": key[1],
            "harness": key[0],
            "source": opened_by,
            "segment": view.segment if segment is None else segment,
            "ids": ids,
            "chars": chars,
            **extra,
            **membership,
            "rule": RULE,
            **compaction,
        }
        if transcript_path:
            fields["transcript_path"] = transcript_path
        recorder.record("session_brief", **fields)
        return event_id

    if admission.nothing is not None:
        event_id = record(
            [],
            chars=0,
            extra={
                "omitted": [],
                "membership": admitted_nothing(admission.nothing),
                "rc3_admitted": [],
            },
        )
        state.note_shown(
            key, [], delivery=False, now=now, surface=SURFACE_BRIEF, event_id=event_id
        )
        return HookResult(
            stdout="", surface="session_brief", rule=RULE, event_id=event_id
        )
    if not admission.in_project:
        return empty
    probe = (
        owner_words(Path(transcript_path).expanduser(), chars=BRIEF_PROBE_CHARS)
        if transcript_path
        else ""
    )
    selection = _select_brief(store, admission=admission, probe=probe, cfg=cfg, now=now)
    if not selection.standing and not selection.work:
        if selection.suppressed and resolution is not None:
            store.record_membership(
                "recall_suppressed",
                {
                    "surface": SESSION_BRIEF,
                    "checkout": resolution.checkout,
                    "caller_rule": resolution.rule,
                    "suppressed": [dict(entry) for entry in selection.suppressed],
                    "candidates": selection.candidates,
                },
                session=key[1],
            )
        return empty
    # The one store read before the print comes first, so that between
    # the claim and the row only pure functions and the recorder run (the
    # recorder never raises): ids the claim noted as shown are never left
    # without their row by a read that fails.
    marks = disputed_marks(store.pending_conflict_pairs())
    for memory_id, note in selection.notes.items():
        marks[memory_id] = f"{marks[memory_id]} {note}" if memory_id in marks else note
    # The cooldown decides atomically: a concurrent prompt of the same
    # session may have shown some of these ids since `view` was read, and
    # what the claim keeps is noted as shown before anything is printed.
    event_id = generate_ulid()
    kept_standing, kept_work, segment = state.claim_brief(
        key,
        [memory.id for memory in selection.standing],
        [memory.id for memory in selection.work],
        now=now,
        event_id=event_id,
    )
    kept = {*kept_standing, *kept_work}
    standing = [memory for memory in selection.standing if memory.id in kept]
    work = [memory for memory in selection.work if memory.id in kept]
    omitted = [
        memory.id
        for memory in [*selection.standing, *selection.work]
        if memory.id not in kept
    ]
    text, ids = render_brief(standing, work, marks)
    if not ids and not omitted:
        return empty
    record(
        ids,
        chars=len(text),
        extra={
            "standing": [memory.id for memory in standing],
            "work": [memory.id for memory in work],
            "omitted": omitted,
            "probe_query": redact_query(probe),
            "ranked_by": selection.ranked_by,
            "rc3_admitted": _rc3_admitted(
                [*standing, *work], caller, project_only=True
            ),
        },
        event_id=event_id,
        segment=segment,
    )
    return HookResult(
        stdout=text,
        surface="session_brief",
        ids=tuple(ids),
        chars=len(text),
        rule=RULE,
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# The prompt recall
# ---------------------------------------------------------------------------


def clean_prompt(prompt: str) -> str:
    """The prompt as the probe reads it (`owner_prompt.probe_text`): every
    complete app envelope block removed (reminder blocks the app composes
    ahead of the typed text, a paste the owner handed over, an artifact
    view), then cut at `PROMPT_CUT_CHARS`.

    The recall review hashes a transcript row through this same function
    to find the prompt a delivery answered, so a prompt without an
    envelope block reads byte for byte as it did before the reminder
    rule, and the hashes the log already holds keep matching."""
    return probe_text(prompt)


@dataclass(frozen=True)
class RecallPointer:
    """One delivered candidate as the block renders it.

    `provenance` is the verified label; it is named on the line only when
    it is not ``local``. `remote_stamp` and `trust_unavailable` keep the
    two annotations the one-hit block carried (a synced record's stamp
    this host never made; a store opened without its key): the rule's
    provenance gate admits local and imported records only, so neither is
    reached by a delivery today, and the renderer keeps them so a widened
    gate cannot deliver such a record unannounced. `replaces` names the
    superseded record whose slot this one took, `extended_by` the newest
    in-force record extending it (`in_force`, RS-F7 part 3)."""

    id: str
    relevance: str
    summary: str
    snippet: str
    provenance: str | None = None
    remote_stamp: bool = False
    trust_unavailable: bool = False
    disputed: str | None = None
    replaces: str | None = None
    extended_by: str | None = None


def _annotation(pointer: RecallPointer) -> str:
    if pointer.trust_unavailable:
        return "[provenance: unknown, key absent]"
    if pointer.provenance is not None and pointer.provenance != LOCAL:
        qualifier = ", unverified here" if pointer.remote_stamp else ""
        return f"[provenance: {pointer.provenance}{qualifier}]"
    return ""


def _in_force_marks(replaces: str | None, extended_by: str | None) -> list[str]:
    """The line's in-force notes (`in_force`): the record whose slot this
    one took, and the newest record extending it."""
    marks: list[str] = []
    if replaces:
        marks.append(f"[replaces {replaces}]")
    if extended_by:
        marks.append(f"[extended by {extended_by}]")
    return marks


def _recall_line(pointer: RecallPointer, snippet: str | None) -> str:
    parts = [f"- {pointer.id} [{pointer.relevance}] {pointer.summary}"]
    annotation = _annotation(pointer)
    if annotation:
        parts.append(annotation)
    if pointer.disputed:
        parts.append(pointer.disputed)
    parts.extend(_in_force_marks(pointer.replaces, pointer.extended_by))
    line = " ".join(parts)
    return f"{line} — {snippet}" if snippet else line


def render_recall_block(pointers: Sequence[RecallPointer]) -> tuple[str, list[str]]:
    """The recall block and the ids it shows.

    The header line, one line per pointer (``- <id> [<relevance>]
    <summary>``, its annotations, then `` — `` and the query-biased
    snippet cut at `SNIPPET_CHARS`), and the footer. Over
    `RECALL_CAP_CHARS` the snippets are cut first, from the last pointer
    back (a snippet that would fall under a dozen characters is dropped),
    then trailing pointers go. ``("", [])`` when nothing fits."""
    kept = list(pointers)
    snippets: list[str | None] = [
        summarize(pointer.snippet, SNIPPET_CHARS) or None for pointer in kept
    ]

    def render() -> str:
        lines = [_recall_line(p, s) for p, s in zip(kept, snippets)]
        return "\n".join([_RECALL_HEADER, *lines, _RECALL_FOOTER])

    text = render()
    for index in range(len(kept) - 1, -1, -1):
        overflow = len(text) - RECALL_CAP_CHARS
        if overflow <= 0:
            break
        snippet = snippets[index]
        if snippet is None:
            continue
        target = len(snippet) - overflow
        snippets[index] = (
            summarize(snippet, target) if target >= _MIN_SNIPPET_CHARS else None
        )
        text = render()
    while kept and len(text) > RECALL_CAP_CHARS:
        kept.pop()
        snippets.pop()
        text = render()
    if not kept:
        return "", []
    return text, [pointer.id for pointer in kept]


def _seen_in_log(
    events: Iterable[dict[str, Any]],
    *,
    since: datetime,
    own_since: datetime,
    sessions: Collection[str],
    worktree_root: str | None,
    harness_session: str | None = None,
) -> set[str]:
    """The ids retrieved (a search, list or show hit), written or updated
    in the segment, from the log.

    Two halves, the union the audit's retrieval shield counts
    (`audit._count_recent_retrievals`): events recorded under one of
    `sessions` (the harness session and the bridged server session) at
    or after `own_since`, and events stamped with the caller's worktree
    under any session at or after `since`, the segment's start. The two
    cuts differ for a session's first segment, which began when the
    session did, a moment no record may carry (a brief is recorded only
    when one was shown): the caller passes the idle window for its own
    sessions there, and the first sight of the session for the worktree
    half, which would otherwise reach back into an earlier conversation
    in the same checkout. The worktree half errs toward holding a pointer
    back: a concurrent session's retrieval in the same checkout counts.

    An event stamped with a harness session (`events.HARNESS_SESSION_FIELD`)
    is this conversation's, under the own cut, when the stamp names
    `harness_session` or its session is one of `sessions` (a server whose
    stamp predates a /clear, which the bridge joined), and never by
    worktree. The two halves above are the bridge for events that carry
    no stamp."""
    seen: set[str] = set()
    for event in events:
        kind = event.get("kind")
        if kind not in _SEEN_KINDS:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None:
            continue
        stamped = event.get(HARNESS_SESSION_FIELD)
        if isinstance(stamped, str) and stamped:
            # Another stamp joins through its session alone: a server
            # stamped with this window's id from before a /clear is in
            # `sessions` by the bridge, another window's never is.
            own = stamped == harness_session or (
                (event.get("session") or event.get("session_id")) in sessions
            )
            if not own or stamp < own_since:
                continue
        else:
            session = event.get("session") or event.get("session_id")
            own = session in sessions and stamp >= own_since
            here = (
                worktree_root is not None
                and event.get("worktree_root") == worktree_root
                and stamp >= since
            )
            if not (own or here):
                continue
        if kind in ("show", "write", "update"):
            # A refused write or update carries a status; a committed
            # update carries none, a committed write may say so.
            if kind != "show" and event.get("status", "committed") != "committed":
                continue
            memory_id = event.get("id")
            if isinstance(memory_id, str):
                seen.add(memory_id)
            continue
        seen.update(
            _str_ids(
                event.get("returned") or event.get("memory_ids") or event.get("hit_ids")
            )
        )
    return seen


def _probe_message(
    *,
    cfg: Config,
    store: Store,
    user_message: str,
    session_id: str,
    server_session: str | None,
    admission: Admission,
    excluded_scopes: set[str],
    recent: list[dict[str, Any]],
    worktree_root: str | None = None,
    now: datetime,
) -> MissReport:
    """The one probe both hooks run: production pool, production
    ranking inputs, `probe_for_miss`, nothing else.

    `admission` is the caller's admission for the pool, built once by the
    hook from the caller's resolution: the pool, the ranked set, the
    document frequencies and the audit's project tests all decide
    through it. The Stop's is always POOL_PREFILTER (design 3.5), and so
    is the prompt recall's under `prompt_recall = "project"`; under
    ``"everywhere"`` the prompt recall draws under the unscoped search's
    admission (decision B21), the pool its delivery admits, and a turn it
    served is shielded from the Stop's narrower probe by its
    `prompt_recall` row.

    Shared by `hook.run_audit` (Stop: the after-the-fact audit) and
    `prompt_recall` (UserPromptSubmit: the same probe run before the
    turn, its hits delivered as pointers). One body because the two
    callers MUST rank alike over one pool: a pointer the recall delivers
    is a hit the audit's probe ranks for the same message, and two
    hand-kept copies of this plumbing would let them drift the first
    time one of them was retuned.

    `now` is the caller's instant: the Stop's clock reading at the probe,
    the prompt recall's `now`, the settlement's `now` for an interrupted
    turn. It dates the report (`checked_at`) and closes the creation and
    retrieval shields.
    """
    from .handlers.search import default_search_width, resolve_search_pool

    # Candidate pool: production's, not an unconditional `load_all()`.
    # `resolve_search_pool` is the same helper `memory_search` builds its
    # pool with: the FTS prefilter above `_INDEX_THRESHOLD_DEFAULT`, the
    # cap-starvation guard, and the BM25 corpus-statistics provider that
    # capped slice needs. Probing the whole corpus instead ranked a
    # strict SUPERSET of what the model's retrieval could reach, and the
    # miss verdict reads only the rank-1 hit, so a memory production's
    # prefilter would have dropped could take that slot and decide the
    # verdict on its own. The filters handed here are the ones
    # `probe_for_miss` re-applies inside `run_search` (the same
    # admission object), so the
    # document frequencies price exactly the collection about to be
    # ranked. `min_survivors` is the width of a DEFAULT `memory_search`
    # (`default_search_width`: the config knob under the same clamp a
    # request goes through, NOT the raw knob, which `config.py` never
    # range-checks), not the probe's `_TOP_HITS_RETAINED`: the starvation
    # guard has to fire on the same slices a default-width retrieval's
    # would. It does NOT track a wider or narrower REQUEST: there is no
    # request on this path, and `resolve_search_pool` records what that
    # leaves open.
    #
    # `probe_pool` is deliberately NOT named `memories`: it is a capped,
    # query-biased SEARCH pool, not the store.
    probe_pool = resolve_search_pool(
        store,
        user_message,
        excluded_scopes=excluded_scopes,
        admission=admission,
        min_survivors=default_search_width(cfg.behavior),
    )
    # The ranker knobs production `memory_search` threads (the recency
    # half-life and the Lane L conversational repairs) are read straight
    # off the config, so the probe cannot rank on a different set of
    # factors than the model's actual retrieval would have. No event read
    # is issued here: the usage-aware multipliers that once needed one
    # left in 9.0.0, and `recent` (the dedup-widened coverage read the
    # caller took) already serves the retrieval shield.
    return probe_for_miss(
        probe_pool.memories,
        user_message,
        recent_events=recent,
        session_id=session_id,
        retrieval_session_id=server_session,
        now=now,
        # The probe's "did the model already retrieve this turn?" shield
        # must use the same wall-clock definition of "this turn" as the
        # attribution pass at the Stop hook: the Stop hook fires at turn
        # END, so a tool-heavy turn easily outlives a short window.
        # Pre-fix this hardcoded 60s while attribution used 600s: any
        # turn longer than a minute aged its own search event out of the
        # shield and emitted a false `search_miss`. 600s is also the
        # ceiling the MCP handler clamps `lookback_seconds` to; the
        # wider window's bias is conservative (over-suppress), matching
        # the project's stance on miss-signal noise. The recall surface
        # reads the hits, not this verdict: its anti-spam bound is its
        # own per-session cooldown (`RecallState`).
        lookback_seconds=ATTRIBUTION_LOOKBACK_SECONDS,
        admission=admission,
        worktree_root=worktree_root,
        excluded_scopes=excluded_scopes,
        mode=cfg.behavior.search_mode or "hybrid",
        half_life_days=cfg.behavior.recency_boost_half_life_days,
        conversational=cfg.behavior.conversational,
        corpus_stats_provider=probe_pool.corpus_stats_provider,
    )


def prompt_recall(
    *,
    prompt: str,
    session_id: str,
    harness: str | None = None,
    prompt_id: str | None = None,
    transcript_path: str | None = None,
    cwd: Path | None = None,
    origin: Origin | None = None,
    config: Config | None = None,
    store: Store | None = None,
    state: RecallState | None = None,
    now: datetime | None = None,
    posted: Mapping[str, Any] | None = None,
    resolution: Resolution | None = None,
) -> HookResult:
    """The UserPromptSubmit surface: probe the prompt and return up to
    three pointers, or nothing.

    The probe is the audit's (`_probe_message`), unchanged: the
    production pool (drawn under the POOL_PREFILTER admission of the same
    resolution, design 3.5, so a record no surface may deliver never takes
    a rank) and ranker over the owner's words as the one
    definition reads them (`owner_prompt.owner_text`: envelope blocks
    removed, cut at `PROMPT_CUT_CHARS`, the bytes the Stop probes and
    hashes for the same text), the content-token floor and the
    acknowledgment gate, top three. A prompt the harness composed and no
    human typed (a task notification, a message from another session, a
    harness tag, a prompt that is only reminder blocks) is not probed and
    records nothing, so the segment keeps its ids for the prompt the
    owner types next. A ``no_signal`` report delivers nothing. Each of
    the top three hits is then a candidate, whatever its coverage label
    (rs-v6, RS-F7 part 0), held back with a reason when it is
    ``other_project`` (the admission does not admit its home,
    `Membership.admit_recall`, which names the relation on the entry and
    writes `recall_suppressed` when it withholds every hit),
    ``provenance`` (not local or imported, or unverifiable),
    ``contradicted`` (an open contradiction) or ``seen_in_segment``
    (shown, retrieved, written or updated in this session segment). A hit
    an admitted record supersedes is history (`in_force`, RS-F7 part 3):
    its head takes the slot, named ``replaces <id>`` on the line and
    decided by its own gates, and the slot is held as ``superseded`` when
    the head is shown in its own slot, already taken, or seen in the
    segment. A line names the newest in-force record extending it. Until
    rs-v6 a hit was also held as ``low_relevance`` (not medium or high)
    and as ``segment_cap`` (the segment had had twelve deliveries). The
    probe's verdict and its retrieval shield do not gate delivery; the
    segment does.

    The switch names the admission (decision B21): under `prompt_recall =
    "project"` the PROMPT_RECALL surface, the caller's own project and
    nothing outside a project (design 3.5); under ``"everywhere"`` the
    unscoped search's, every project's records and the global ones, inside
    a project or outside one, never across a boundary and never an
    unresolved home, with the pool drawn under the same admission. The
    row names it (``admission``). Nothing for a caller whose resolution
    read nothing, is deferred or raised; nothing with telemetry off. A delivery is recorded as a `prompt_recall`
    event BEFORE the block is returned: it is a member of the audit's
    retrieval kinds, so the Stop audit of the same turn sees the
    delivery and reports ``ok`` rather than a silent miss.

    Before the probe the caller's membership is resolved
    (`location.resolve_hook`, or `resolution` when given). A caller
    nothing could be read for (INDETERMINATE) or whose HEAD or config is
    still being fetched (DEFERRED) is shown nothing from any project: the
    resolution wrote its row, and the result carries its line (L8, L9)
    and no pointer; a resolution that raised is shown nothing either.
    Where the daemon cannot read the caller's namespace
    the result carries the exchange's next question in ``membership``.

    From rs-v5 the row carries the caller's ``checkout`` and
    ``membership_rule`` (`_delivery_membership`) and ``rc3_admitted``
    (design 8.11): of the hits every gate but the admission passed, the
    ones rc3's rule would have delivered (`_rc3_admitted`; under
    ``"project"`` rc3's project test, under ``"everywhere"`` rc3's
    auto-scope), less any pointer the render or the cooldown dropped from
    this delivery."""
    empty = HookResult.empty("prompt_recall")
    cfg = config or load_config(None)
    mode = cfg.behavior.prompt_recall
    if mode not in ("project", "everywhere") or not cfg.telemetry.enabled:
        return empty
    if store is None:
        store = _open_store(cfg)
        if store is None:
            return empty
    query = owner_text(prompt)
    if query is None:
        # Not the owner's words: blank once the envelope blocks are gone,
        # or composed by the harness (a task notification, a message from
        # another session, a side-session note). No one asked anything, so
        # nothing is probed, recorded or claimed, and no id is held for the
        # rest of the segment on its account (RS-F1, RS-F5).
        return empty
    caller_resolution: Resolution | Exchange | None = resolution
    if caller_resolution is None:
        caller_resolution = _resolve_caller(
            store=store,
            cfg=cfg,
            cwd=cwd,
            origin=origin,
            posted=posted,
            session_id=str(session_id),
        )
    if isinstance(caller_resolution, Exchange):
        return HookResult(
            stdout="",
            surface="prompt_recall",
            membership={"ask": caller_resolution.to_reply()},
        )
    if caller_resolution is not None and caller_resolution.checkout is not None:
        # The user's words, whole, kept for this checkout in this process:
        # what a join of two projects must quote (design 8.8).
        whole = owner_text(prompt, cut=False)
        if whole:
            WORDS.remember(store.path, caller_resolution.checkout, whole)
    # The admission the switch names (decision B21): the prompt recall's
    # own under "project", the unscoped search's under "everywhere".
    surface = PROMPT_RECALL if mode == "project" else SEARCH_UNSCOPED
    admission = admission_for(surface, store, caller_resolution)
    if admission.nothing is not None:
        # Nothing was read where the caller stands, or the read is
        # deferred, or the resolution raised: no surface admits anything,
        # and the origin columns never stand in. The resolution wrote its
        # row (`resolution_indeterminate`, `resolution_deferred`).
        said = "\n".join(caller_resolution.lines) if caller_resolution else ""
        return HookResult(stdout=said, surface="prompt_recall", chars=len(said))
    remint: Mapping[str, Any] | None = None
    if caller_resolution is not None and caller_resolution.remint_token:
        # The checkout's token is the client's to re-mint, in its own
        # namespace (design 3.3, E21 g); every answer below carries it.
        remint = {"remint": True}
        empty = replace(empty, membership=remint)
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    # Same caller the Stop hook publishes (`hook.run_audit`), minus the
    # model: this hook fires before the assistant has answered.
    identity.bind_transcript(session_id=key[1], model=None, client=key[0])
    caller = origin if origin is not None else _origin.capture_caller(cwd)
    if surface == PROMPT_RECALL and not admission.in_project:
        # PROMPT_RECALL admits nothing for a caller outside any project
        # (design 3.5): nothing to probe for.
        return empty

    state = state if state is not None else RecallState()
    confirm_compaction(
        store=store,
        state=state,
        key=key,
        transcript_path=transcript_path,
        cfg=cfg,
        triggered_from="prompt_hook",
        now=now,
        origin=caller,
    )
    view = state.segment(store, key, now=now)
    recent_cut = now - timedelta(seconds=REAUDIT_DEDUP_WINDOW_SECONDS)
    # A first segment began with the session; a later one when its
    # compact or clear opened it (`_seen_in_log` says why the cuts differ).
    own_since = (
        view.started_at
        if view.segment > 0
        else now - timedelta(seconds=STATE_IDLE_SECONDS)
    )
    recent = list(store.events_since(recent_cut))
    # The conversation's in-process rows: the harness join first (tool
    # events stamped with this session id), the worktree bridge only
    # when nothing is stamped with it.
    server_session = bridged_session(
        recent, harness_session=key[1], worktree_root=caller.worktree_root
    )
    excluded_scopes = _disabled_scopes_from_events(
        recent, worktree_root=caller.worktree_root, server_session=server_session
    )
    # Best effort, as every settlement is: a past turn's audit never costs
    # this prompt its recall.
    try:
        settle_interrupted(
            store=store,
            cfg=cfg,
            session_id=key[1],
            transcript_path=transcript_path,
            # An interrupted turn's row is a Stop's row: its probe draws
            # under the Stop's prefilter in both modes, so the verdict
            # does not depend on which hook settled the turn.
            admission=admission.on(POOL_PREFILTER),
            excluded_scopes=excluded_scopes,
            recent=recent,
            server_session=server_session,
            worktree_root=caller.worktree_root,
            now=now,
            previous_audit=previous_audit_at(store, key[1], now=now),
            triggered_from="prompt_hook",
            exclude_prompt_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - logged, never raised at the hook
        log.warning(
            "interrupted turns not settled at the prompt: %s: %s",
            exc.__class__.__name__,
            exc,
        )
    report = _probe_message(
        cfg=cfg,
        store=store,
        user_message=query,
        session_id=key[1],
        server_session=server_session,
        # The pool is drawn under the hooks' prefilter for "project" and
        # under the unscoped search's admission for "everywhere", so a
        # record the delivery may not admit never takes a rank.
        admission=admission.on(POOL_PREFILTER)
        if surface == PROMPT_RECALL
        else admission,
        excluded_scopes=excluded_scopes,
        recent=recent,
        worktree_root=caller.worktree_root,
        now=now,
    )

    def recorded_hold(cause: str, entries: Sequence[Mapping[str, str]]) -> HookResult:
        """Nothing delivered: the `recall_held` row says why (RS-F7 part 6)."""
        if cfg.telemetry.enabled:
            fields = recall_held_fields(
                report,
                session_id=key[1],
                probe_mode=cfg.behavior.search_mode or "hybrid",
                cause=cause,
                held=entries,
                rule=RULE,
                segment=view.segment,
                prompt_id=prompt_id,
                transcript_path=transcript_path,
                harness=key[0],
            )
            fields["admission"] = surface
            fields.update(_delivery_membership(caller_resolution, admission))
            Recorder(
                store=store,
                session_id=key[1],
                enabled=True,
                worktree_root=caller.worktree_root,
            ).record("recall_held", **fields)
        return empty

    if report.verdict == "no_signal":
        return recorded_hold("no_signal", [])
    if not report.top_hits:
        return recorded_hold("no_hits", [])
    # The hits through the PROMPT_RECALL surface
    # (`Membership.admit_recall`, which writes `recall_suppressed` when it
    # withholds every one of them, design 4 failure 8).
    model = admission.model
    if model is None or caller_resolution is None:
        return empty
    turn = model.admit_recall(
        caller_resolution,
        [hit.id for hit in report.top_hits],
        admission=surface,
    )
    admitted_here = set(turn.delivered)
    withheld = dict(turn.suppressed)

    sessions = {key[1]} | joined_sessions(
        recent, harness_session=key[1], worktree_root=caller.worktree_root
    )
    seen = set(view.shown) | _seen_in_log(
        store.events_of_kinds(_SEEN_KINDS, since=min(own_since, view.started_at)),
        since=view.started_at,
        own_since=own_since,
        sessions=sessions,
        worktree_root=caller.worktree_root,
        harness_session=key[1],
    )
    hit_ids = [hit.id for hit in report.top_hits]
    # What is in force (RS-F7 part 3): an admitted hit some admitted
    # record supersedes yields its slot to its head; a head shown in its
    # own slot, or already in the segment, holds the slot (``superseded``).
    forced = in_force(
        store, [rid for rid in hit_ids if rid in admitted_here], admission
    )
    own = {entry.id for entry in forced.values() if not entry.history}
    wanted = list(dict.fromkeys([*hit_ids, *(entry.id for entry in forced.values())]))
    memories = {memory.id: memory for memory in store.load_many(wanted)}
    trust = store.trust_rows(wanted)
    contradicted = _unless_standing(store, admission, store.contradicted_at(wanted))
    chosen: list[tuple[MissHit, Memory, TrustRow, InForce | None]] = []
    suppressed: list[dict[str, str]] = []
    # The hits every gate but the admission passes, in rank order: the
    # candidates rc3's rule is asked about for the rc3_admitted column
    # (design 8.11), so the column differs from the delivery by the
    # admission alone.
    rc3_candidates: list[Memory] = []

    def gate(memory: Memory, row: TrustRow | None) -> str | None:
        if row is None or row.provenance not in _DELIVERED_PROVENANCE:
            return "provenance"
        if _contradiction_open(memory, contradicted):
            return "contradicted"
        if memory.id in seen:
            return "seen_in_segment"
        return None

    for hit in report.top_hits:
        memory = memories.get(hit.id)
        if memory is None:
            continue
        row = trust.get(hit.id) if trust is not None else None
        held = gate(memory, row)
        if held is None and len(rc3_candidates) < RECALL_MAX_POINTERS:
            rc3_candidates.append(memory)
        forced_entry = forced.get(hit.id)
        if forced_entry is not None and forced_entry.history:
            # The hit is history: the slot is its head's, and the head's
            # own gates decide it.
            head_id = forced_entry.id
            memory = memories.get(head_id)
            row = trust.get(head_id) if trust is not None else None
            if (
                memory is None
                or head_id in own
                or head_id in seen
                or any(taken.id == head_id for _hit, taken, _row, _f in chosen)
            ):
                held = "superseded"
            else:
                held = gate(memory, row)
        reason: str | None = None
        relation: str | None = None
        if hit.id not in admitted_here:
            reason = "other_project"
            relation = withheld.get(hit.id)
        elif held is not None:
            reason = held
        elif len(chosen) >= RECALL_MAX_POINTERS:
            continue
        if reason is not None:
            entry = {"id": hit.id, "reason": reason}
            if relation is not None:
                entry["relation"] = relation
            suppressed.append(entry)
            continue
        assert row is not None and memory is not None
        chosen.append((hit, memory, row, forced_entry))
    if not chosen:
        return recorded_hold("every_hit_held", suppressed)

    marks = disputed_marks(store.pending_conflict_pairs())
    pointers = [
        RecallPointer(
            id=memory.id,
            relevance=hit.relevance,
            summary=summarize(memory.body),
            # The query's snippet is the hit's text; a head that took the
            # slot is named by its summary alone.
            snippet=hit.snippet if memory.id == hit.id else "",
            provenance=row.provenance,
            disputed=marks.get(memory.id),
            replaces=forced_entry.replaces if forced_entry is not None else None,
            extended_by=forced_entry.extended_by if forced_entry is not None else None,
        )
        for hit, memory, row, forced_entry in chosen
    ]
    block, shown_ids = render_recall_block(pointers)
    if not shown_ids:
        return recorded_hold(
            "nothing_rendered", [{"id": p.id, "reason": "cap"} for p in pointers]
        )
    # The cooldown decides atomically: a concurrent prompt of the same
    # session may have shown some of these ids since `view` was read.
    event_id = generate_ulid()
    claimed, segment = state.claim_delivery(key, shown_ids, now=now, event_id=event_id)
    if not claimed:
        return recorded_hold(
            "segment_claimed",
            [{"id": memory_id, "reason": "seen_in_segment"} for memory_id in shown_ids],
        )
    if claimed != shown_ids:
        kept = set(claimed)
        block, shown_ids = render_recall_block([p for p in pointers if p.id in kept])
        if not shown_ids:
            return recorded_hold(
                "nothing_rendered",
                [{"id": p.id, "reason": "cap"} for p in pointers if p.id in kept],
            )
    recorder = Recorder(
        store=store,
        session_id=key[1],
        enabled=cfg.telemetry.enabled,
        worktree_root=caller.worktree_root,
    )
    fields = prompt_recall_fields(
        report,
        session_id=key[1],
        probe_mode=cfg.behavior.search_mode or "hybrid",
        injected_chars=len(block),
        delivered_reason=mode,
        shown_ids=shown_ids,
        suppressed=suppressed,
        rule=RULE,
        segment=segment,
        prompt_id=prompt_id,
        transcript_path=transcript_path,
        harness=key[0],
    )
    fields["event_id"] = event_id
    # The admission the delivery was decided under (decision B21).
    fields["admission"] = surface
    fields.update(_delivery_membership(caller_resolution, admission))
    # A pointer the render or the cooldown dropped is dropped whatever the
    # admission, so the column leaves it out as well.
    dropped = {hit.id for hit, _memory, _row, _forced in chosen} - set(shown_ids)
    fields["rc3_admitted"] = [
        memory_id
        for memory_id in _rc3_admitted(
            rc3_candidates, caller, project_only=surface == PROMPT_RECALL
        )
        if memory_id not in dropped
    ]
    recorder.record("prompt_recall", **fields)
    return HookResult(
        stdout=block,
        surface="prompt_recall",
        ids=tuple(shown_ids),
        chars=len(block),
        rule=RULE,
        event_id=str(fields["event_id"]),
        membership=remint,
    )


# ---------------------------------------------------------------------------
# At Stop: settling what was shown, and the unanswered reviews
# ---------------------------------------------------------------------------


def _used_in_segment(
    events: Iterable[dict[str, Any]],
    *,
    since: datetime,
    harness_session: str,
    sessions: Collection[str],
) -> set[str]:
    """The ids any `use` event of this conversation names at or after
    `since` (the segment's start): an explicit `memory_record_use`, the
    in-process fallback, or a Stop's attribution of a real retrieval.
    Joined by harness stamp first, else by session (the joined in-process
    sessions and the hook's own id)."""
    used: set[str] = set()
    for event in events:
        if event.get("kind") != "use":
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None or stamp < since:
            continue
        if event_joined(event, harness_session=harness_session, sessions=sessions):
            used.update(_str_ids(event.get("ids") or event.get("memory_ids")))
    return used


def settle_shown(
    *,
    store: Store,
    recorder: Recorder,
    state: RecallState,
    session_id: str,
    assistant_response: str,
    sessions: Collection[str],
    harness: str | None = None,
    exclude: Collection[str] = (),
    prompt_id: str | None = None,
    client_model: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Settle the session segment's shown pointers against the turn's
    reply; the ids settled.

    Every id the brief or the prompt recall showed in the segment, not
    settled in it yet, runs through the containment matcher
    (`attribution.attribute_uses`) against `assistant_response`. A match
    records ONE `use` event: outcome ``applied``, ``attribution="hook"``
    (the matcher's tier), the matched sentence as its claim excerpt, the
    ``surface`` that showed it (``brief`` or ``recall``), that show's
    ``shown_event_id``, the segment, the harness, and the harness turn
    (``prompt_id``) when the Stop named one. An unmatched id records
    nothing: a pointer is not a retrieval, so the auto fallback never
    applies to it.

    Left out: ids in `exclude` (the real retrievals this Stop settled
    through `hook._emit_hook_attributions`), and ids any `use` of this
    conversation already named in the segment, joined by harness stamp
    first and by `sessions` otherwise (`_used_in_segment`), so one reply
    never credits a memory twice. With telemetry off the recorder writes
    nothing and the state is left as it was."""
    from .attribution import attribute_uses
    from .store import MemoryNotFoundError, TombstonedError

    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    view = state.segment(store, key, now=now)
    skip = set(view.settled) | set(exclude)
    pending = [memory_id for memory_id in view.shown_by if memory_id not in skip]
    if not pending or not assistant_response:
        return []
    used = _used_in_segment(
        store.events_of_kinds(("use",), since=view.started_at),
        since=view.started_at,
        harness_session=key[1],
        sessions=set(sessions) | {key[1]},
    )
    bodies: dict[str, str] = {}
    for memory_id in sorted(pending):
        if memory_id in used:
            continue
        try:
            bodies[memory_id] = store.load_one(memory_id).body
        except (MemoryNotFoundError, TombstonedError):
            continue
    if not bodies:
        return []
    settled: list[str] = []
    for match in attribute_uses(bodies, assistant_response):
        surface, shown_event_id = view.shown_by[match.memory_id]
        fields: dict[str, Any] = {
            "ids": [match.memory_id],
            "outcome": "applied",
            "auto": False,
            "attribution": "hook",
            "claim_excerpts": [match.claim_excerpt],
            "surface": surface,
            "shown_event_id": shown_event_id,
            "segment": view.segment,
            "harness": key[0],
            "triggered_from": "stop_hook",
            "rule": RULE,
        }
        if prompt_id:
            fields["prompt_id"] = prompt_id
        if client_model is not None:
            fields["client_model"] = client_model
        recorder.record("use", **fields)
        settled.append(match.memory_id)
    if settled and recorder.enabled:
        state.note_settled(key, settled, now=now)
    return settled


def settle_unused(
    *,
    store: Store,
    state: RecallState,
    key: tuple[str, str],
    now: datetime,
    worktree_root: str | None,
    triggered_from: str,
) -> list[str]:
    """Settle the segment the session just left (`RecallState.take_closed`):
    every pointer it showed that no Stop settled (`settle_shown`), no
    `memory_show` of this conversation opened and no `use` row of it
    attested records one `use` row with outcome ``unused`` (RS-F7 part
    6), with the surface that showed it, that show's event id, the closed
    segment, the harness and the hook that closed it (`triggered_from`);
    the ids recorded. The caller writes these before the row that opens
    the next segment, so a state rebuilt from the log reads them in the
    segment they settle. Nothing when no segment waits."""
    closed = state.take_closed(key)
    if closed is None:
        return []
    pending = [
        memory_id for memory_id in closed.shown_by if memory_id not in closed.settled
    ]
    if not pending:
        return []
    recent = list(
        store.events_since(now - timedelta(seconds=REAUDIT_DEDUP_WINDOW_SECONDS))
    )
    sessions = {key[1]} | joined_sessions(
        recent, harness_session=key[1], worktree_root=worktree_root
    )
    events = list(store.events_of_kinds(("show", "use"), since=closed.started_at))
    opened: set[str] = set()
    for event in events:
        if event.get("kind") != "show":
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None or stamp < closed.started_at:
            continue
        if event_joined(event, harness_session=key[1], sessions=sessions):
            memory_id = event.get("id")
            if isinstance(memory_id, str):
                opened.add(memory_id)
    attested = _used_in_segment(
        events, since=closed.started_at, harness_session=key[1], sessions=sessions
    )
    recorder = Recorder(
        store=store,
        session_id=key[1],
        enabled=True,
        worktree_root=worktree_root,
        triggered_from=triggered_from,
    )
    settled: list[str] = []
    for memory_id in sorted(pending):
        if memory_id in opened or memory_id in attested:
            continue
        surface, shown_event_id = closed.shown_by[memory_id]
        recorder.record(
            "use",
            ids=[memory_id],
            outcome="unused",
            auto=False,
            attribution="hook",
            surface=surface,
            shown_event_id=shown_event_id,
            segment=closed.segment,
            harness=key[0],
            rule=RULE,
        )
        settled.append(memory_id)
    return settled


def previous_audit_at(
    store: Store, session_id: str, *, now: datetime
) -> datetime | None:
    """When the session's newest `turn_audited` was stamped, inside the
    idle window; None when it has none there. Read before the Stop
    records its own."""
    latest: datetime | None = None
    for event in store.events_of_kinds(
        ("turn_audited",), since=now - timedelta(seconds=STATE_IDLE_SECONDS)
    ):
        if (event.get("session") or event.get("session_id")) != session_id:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def settle_interrupted(
    *,
    store: Store,
    cfg: Config,
    session_id: str,
    transcript_path: str | None,
    admission: Admission,
    excluded_scopes: set[str],
    recent: list[dict[str, Any]],
    server_session: str | None,
    worktree_root: str | None,
    now: datetime,
    previous_audit: datetime | None,
    triggered_from: str,
    exclude_prompt_id: str | None,
) -> list[str]:
    """Settle the session's interrupted turns (RS-F7 part 6, RS-F3-M5): a
    turn the owner interrupted ends with no Stop hook, so the next hook
    that reads the transcript, the Stop of a later turn or the prompt
    recall of the next prompt, writes its `turn_audited` row. For each
    turn `transcript.interrupted_turns` finds since `previous_audit`, the
    session's previous `turn_audited` as the caller read it before
    recording its own (`previous_audit_at`; the idle window back
    when it has none), other than `exclude_prompt_id` (the turn this Stop
    audits itself) and any turn a row already names: the probe's verdict
    over the prompt at `now`, as a Stop probes it (`_probe_message` under
    the Stop's POOL_PREFILTER admission whichever hook settles the turn,
    the same scopes and shield),
    ``reply_source="interrupted"``, whether a partial reply exists and its
    model, stamped `triggered_from` the hook that settled it. No
    `search_miss`: the turn did not finish, so a miss is not the reply's;
    the pointers it showed stay pending for the segment's later Stops or
    its close. Nothing with telemetry off or without a transcript file.
    The prompt ids settled."""
    if not cfg.telemetry.enabled or not transcript_path:
        return []
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return []
    since = previous_audit
    if since is None:
        since = now - timedelta(seconds=STATE_IDLE_SECONDS)
    audited: set[str] = set()
    for event in store.events_of_kinds(("turn_audited",), since=since):
        if (event.get("session") or event.get("session_id")) != session_id:
            continue
        named = event.get("prompt_id")
        if isinstance(named, str):
            audited.add(named)
    recorder = Recorder(
        store=store, session_id=session_id, enabled=True, worktree_root=worktree_root
    )
    probe_mode = cfg.behavior.search_mode or "hybrid"
    settled: list[str] = []
    for turn in interrupted_turns(path, since=since):
        if (
            turn.prompt_id is None
            or turn.prompt_id == exclude_prompt_id
            or turn.prompt_id in audited
            or turn.prompt is None
        ):
            continue
        report = _probe_message(
            cfg=cfg,
            store=store,
            user_message=turn.prompt,
            session_id=session_id,
            server_session=server_session,
            admission=admission,
            excluded_scopes=excluded_scopes,
            recent=recent,
            worktree_root=worktree_root,
            now=now,
        )
        recorder.record(
            "turn_audited",
            **turn_audited_fields(
                report,
                session_id=session_id,
                probe_mode=probe_mode,
                assistant_present=turn.reply is not None,
                triggered_from=triggered_from,
                client_model=turn.model,
                prompt_id=turn.prompt_id,
                reply_source="interrupted",
                rule=RULE,
            ),
        )
        settled.append(turn.prompt_id)
    return settled


def review_unanswered(
    *,
    store: Store,
    recorder: Recorder,
    state: RecallState,
    session_id: str,
    sessions: Collection[str],
    since: datetime,
    harness: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Count the review chains the conversation left open since its
    previous turn; the roots counted.

    Reads the `review_issued` rows recorded under `sessions` (the joined
    in-process sessions: the harness join first, the bridge otherwise) at
    or after `since`, takes each row's chain root (its
    ``parent_review_id``, else its own ``review_id``), and counts each
    root `review.find_chain` reads open: no write row carries any of the
    chain's review ids. A re-send another gate refused before the review
    decided anything carries no review id on its write row, so its chain
    also reads open: the count means unanswered or refused. A root is
    counted once per session (`RecallState.note_review_roots`, rebuilt
    from these rows after a restart).

    A non-zero count records one `review_unanswered` event: ``count``,
    ``root_ids``, the hook's ``session_id``, the ``review_sessions`` read,
    the ``segment`` and the ``harness``. Nothing is recorded for zero,
    and nothing at all with telemetry off. No text is shown."""
    from .review import REVIEW_ISSUED, find_chain
    from .models import is_valid_ulid

    if not recorder.enabled or not sessions:
        return []
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    view = state.segment(store, key, now=now)
    roots: list[str] = []
    for event in store.events_of_kinds((REVIEW_ISSUED,), since=since):
        if (event.get("session") or event.get("session_id")) not in sessions:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None or stamp < since:
            continue
        parent = event.get("parent_review_id")
        own = event.get("review_id")
        root = parent if isinstance(parent, str) and is_valid_ulid(parent) else own
        if not isinstance(root, str) or not is_valid_ulid(root):
            continue
        if root in view.review_roots or root in roots:
            continue
        roots.append(root)
    open_roots = [root for root in roots if find_chain(store, root).open]
    if not open_roots:
        return []
    recorder.record(
        "review_unanswered",
        count=len(open_roots),
        root_ids=open_roots,
        session_id=key[1],
        review_sessions=sorted(sessions),
        segment=view.segment,
        harness=key[0],
        triggered_from="stop_hook",
    )
    state.note_review_roots(key, open_roots, now=now)
    return open_roots
