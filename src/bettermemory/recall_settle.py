"""The probe both hooks run, and the Stop's settlement of what was shown.

`_probe_message` is the one probe the Stop audit (`hook.run_audit`) and
the prompt recall run: production's pool and ranking inputs through
`audit.probe_for_miss`, read here, so a test that spies on the probe
patches this module. `settle_shown` credits a shown pointer the reply
used, `settle_unused` closes the unopened pointers of a segment the
session left, `settle_interrupted` audits the turns the owner
interrupted, `review_unanswered` counts the review chains left open and
`previous_audit_at` reads the previous audit. `recall` re-exports every
name here a caller imports from it. This module lives apart from
`recall.py` to keep each module under the package's 2,000-line bound
(ENG-96); from the family it imports `recall_state` alone, never
`recall`, and `recall` imports it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .admission import Admission
from .audit import (
    ATTRIBUTION_LOOKBACK_SECONDS,
    REAUDIT_DEDUP_WINDOW_SECONDS,
    MissReport,
    probe_for_miss,
    turn_audited_fields,
)
from .config import Config
from .events import Recorder
from .models import utcnow
from .recall_state import (
    DEFAULT_HARNESS,
    RULE,
    STATE_IDLE_SECONDS,
    RecallState,
    _str_ids,
)
from .sessions import event_joined, joined_sessions
from .store import Store
from .time_utils import parse_event_ts
from .transcript import interrupted_turns


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
    never credits a memory twice. The `use` rows are product state,
    recorded with telemetry off as well (`events.STATE_KINDS`)."""
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
    if settled:
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
    telemetry: bool,
) -> list[str]:
    """Settle the segment the session just left (`RecallState.take_closed`):
    every pointer it showed that no Stop settled (`settle_shown`), no
    `memory_show` of this conversation opened and no `use` row of it
    attested records one `use` row with outcome ``unused`` (RS-F7 part
    6), with the surface that showed it, that show's event id, the closed
    segment, the harness and the hook that closed it (`triggered_from`);
    the ids recorded. The caller writes these before the row that opens
    the next segment, so a state rebuilt from the log reads them in the
    segment they settle. Nothing when no segment waits. `telemetry` is
    `[telemetry] enabled`: with it off the rows keep only what their
    readers read (`events.state_fields`)."""
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
        enabled=telemetry,
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
    its close. Nothing without a transcript file; the rows are product
    state, recorded with telemetry off as well (`events.STATE_KINDS`).
    The prompt ids settled."""
    if not transcript_path:
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
        store=store,
        session_id=session_id,
        enabled=cfg.telemetry.enabled,
        worktree_root=worktree_root,
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
                # Its pointers stay pending for the segment's later Stops.
                settled_count=0,
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
    root `review.find_chain` reads open: no write or update row carries
    any of the chain's review ids. A re-send another gate refused before
    the review decided anything carries no review id on its write row,
    so its chain also reads open, as does an update's standing roster
    that refused (`handlers.standing_review`): the count means unanswered
    or refused, a write's chain or an update's. A root is
    counted once per session (`RecallState.note_review_roots`, rebuilt
    from these rows after a restart).

    A non-zero count records one `review_unanswered` event: ``count``,
    ``root_ids``, the hook's ``session_id``, the ``review_sessions`` read,
    the ``segment`` and the ``harness``. Nothing is recorded for zero;
    the row is product state, recorded with telemetry off as well
    (`events.STATE_KINDS`). No text is shown."""
    from .review import REVIEW_ISSUED, find_chain
    from .models import is_valid_ulid

    if not sessions:
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
