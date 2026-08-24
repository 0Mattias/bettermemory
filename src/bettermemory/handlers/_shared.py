"""Cross-cutting helpers every MCP tool handler reaches for.

The per-tool modules in this package own their happy-path logic; the
helpers here own the bookkeeping every handler runs (turn counter
advance, pending-write TTL drain, use-token attribution scan,
use-token expiry drain, payload validation, event log timestamp
parsing).

Importing ``capture_origin`` THROUGH ``bettermemory._handlers`` rather
than directly from ``bettermemory.origin`` is load-bearing: the test
suite (`tests/test_server_origin.py`,
`tests/test_server_commit_drift.py`) monkey-patches
``bettermemory._handlers.capture_origin``, and routing every handler
through the same shim is what makes the patch propagate to the new
per-tool modules. Same pattern for any future cross-cutting symbol
the test suite wants to override.
"""

from __future__ import annotations

import time
from typing import Any, TypeAlias

from mcp.server.mcpserver import Context as _SDKContext

from ..claims import check_claim, parse_claims
from ..events import Recorder, iter_events_backward
from ..models import Category, Confidence, Source, validate_scope
from ..session import PendingUseToken, SessionState
from ..time_utils import parse_event_ts


# Local alias filling the SDK Context's two generic params with Any —
# the handlers only ever read the request's client id, never the typed
# lifespan/request data, so unconstrained generics are the right shape.
# Aliasing once via `TypeAlias` (not a bare runtime assignment) keeps
# every handler signature readable AND keeps strict checkers happy — a
# plain `Context = X[Any, ...]` would type-check on mypy but trip
# "Variable not allowed in type expression" on Pyright/Pylance.
#
# The arity is load-bearing and version-specific: mcp 1.x was
# `Generic[ServerSessionT, LifespanContextT, RequestT]` and 2.x dropped
# the session parameter, so this is `[Any, Any]` and a stale `[Any, Any,
# Any]` is an import-time TypeError, not a slow type-checker complaint.
# Every handler signature resolves through this one alias, so it is also
# the single site the SDK's ctx injection matches against — see the
# subclass assertion in `tests/test_session_registry.py`, which exists
# because a Context pointing at the wrong class stops injection silently.
Context: TypeAlias = _SDKContext[Any, Any]


# ---------------------------------------------------------------------------
# Use-recording outcomes — values land verbatim in the event log so the
# health view can aggregate them. Add new outcomes by extending this set;
# don't rename existing values without a migration story.
# ---------------------------------------------------------------------------


_USE_OUTCOMES: frozenset[str] = frozenset(
    {
        "applied",  # The retrieved memory shaped the response.
        "ignored",  # Retrieved but turned out off-topic.
        "contradicted",  # The user or current state contradicted the memory.
        # The retrieved memory had drifted and was fixed in the same turn
        # (memory_update / memory_verify already called). Audit-only — does
        # not raise the unresolved-contradiction flag the way `contradicted`
        # does. Use this for the post-fix log entry; use `contradicted` when
        # you've noticed a conflict but haven't fixed it yet.
        "corrected",
    }
)


# ---------------------------------------------------------------------------
# memory_write categories. Orthogonal to `confidence` (how sure) and
# `source` (where the fact came from): `category` is what kind of claim
# the memory makes. See `models.Category` for the persisted enum.
# ---------------------------------------------------------------------------


_WRITE_CATEGORIES: frozenset[str] = frozenset({c.value for c in Category})


# Ambient memories that grow past this word count get a non-blocking
# warning attached to the committed response. We don't refuse the
# write — ambient is a soft category and a long body is sometimes
# correct (e.g. a curated user-context dump) — but the warning gives
# the writer a chance to decide whether to split. Mirrors the way
# `transient_warning` is firm but `ambient_body_long` is advisory.
_AMBIENT_LONG_BODY_WORDS = 500


# Cap on free-text `note` strings recorded on `memory_verify` and
# `memory_record_use` events, so a hostile client (or a runaway model)
# can't inflate the JSONL event log with multi-megabyte notes. Raised
# 500 -> 800 on the T1 live-store census: 11.5% of recorded notes sat
# within 50 chars of the old cap, and the unconstrained pre-cap tail
# has its knee at 800 (the T3 note-cap decision). Over-cap
# notes are refused with a teaching error, never truncated; pasting
# whole transcripts belongs in a memory body, not in an event note.
_NOTE_MAX_LEN = 800


# ---------------------------------------------------------------------------
# Validation + per-handler bookkeeping helpers.
# ---------------------------------------------------------------------------


def _validate_scope_count(scopes: list[str], max_count: int) -> None:
    """Reject scope lists whose length exceeds `max_count`.

    A no-op when `max_count <= 0` (cap disabled). Centralised so that
    ``memory_write``, ``memory_update``, and ``episode_write`` share the
    same bound. Mirrors the discipline ``_validate_content_size`` set for
    byte caps: a configurable handler-boundary check on top of the model-
    layer hard ceiling. Without this, a ~2200-entry scope list would
    serialise to ~64 KB of YAML, push the frontmatter past
    `_frontmatter._MAX_YAML_BYTES`, and the record would vanish from every
    read surface despite the write returning ``status="committed"`` — the
    same silent-data-loss class the takeaway cap closed in t16.

    Raises ``ValueError`` with the same message shape as
    ``_validate_content_size`` so the MCP error surface stays uniform across
    the byte-cap and count-cap families.
    """
    if max_count <= 0:
        return
    if len(scopes) > max_count:
        raise ValueError(
            f"scopes exceeds max_scopes_per_write "
            f"({len(scopes)} entries > {max_count} entries). "
            f"Shorten the scope list or raise the "
            f"[behavior] max_scopes_per_write config setting."
        )


def _validate_content_size(
    content: str,
    max_bytes: int,
    *,
    field_name: str = "content",
    config_key: str = "max_content_bytes",
) -> None:
    """Reject memory bodies whose UTF-8 byte length exceeds `max_bytes`.

    A no-op when `max_bytes <= 0` (cap disabled). Centralised so that
    `memory_write`, `memory_update`, `episode_write`, and any future
    write entry point share the same bound. The check is on encoded byte
    length rather than character count because that's the unit that
    lands on disk and in the JSONL event log — a body of CJK or emoji
    characters expands meaningfully under UTF-8 encoding.

    `field_name` and `config_key` are message-only knobs so the
    `episode_write` takeaway path can raise the same `ValueError`
    shape with a takeaway-specific message ("takeaway exceeds
    max_takeaway_bytes …") instead of misleadingly mentioning the
    body cap. The defaults preserve the legacy message verbatim so
    existing tests pinning `match="max_content_bytes"` keep passing.
    """
    if max_bytes <= 0:
        return
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > max_bytes:
        raise ValueError(
            f"{field_name} exceeds {config_key} "
            f"({encoded_size} bytes > {max_bytes} bytes). "
            f"Shorten the {field_name} or raise the "
            f"[behavior] {config_key} config setting."
        )


def _validate_content_floor(content: str, min_tokens: int) -> None:
    """Reject memory bodies with fewer than `min_tokens` whitespace tokens.

    A no-op when `min_tokens <= 0`, which is the shipped default. Out of the
    box the only lower bound on a body is the "non-empty after strip" check
    in `_validate_write_payload`: a one-word body is a legitimate write for a
    caller that knows what it is storing (an identifier, a path, a version
    pin), so a floor is deployment policy rather than a server invariant. It
    earns its keep for unattended or bulk callers, where a fragment costs a
    durable record plus the curation pass that later removes it.

    Blast radius when enabled, because the validator is shared: the floor
    binds `memory_write` AND `accept_proposal` (`handlers/proposals.py`).
    It does NOT reach `memory_update`, which validates a replacement body
    through `_validate_content_size` directly and never calls this
    validator — a body edit can still take a memory below the floor.

    Raises `ValueError` in the mirror-image message shape of
    `_validate_content_size` so the MCP error surface stays uniform across
    the content-bound family.
    """
    if min_tokens <= 0:
        return
    token_count = len(content.split())
    if token_count < min_tokens:
        raise ValueError(
            f"content is below min_content_tokens "
            f"({token_count} tokens < {min_tokens} tokens). "
            f"Expand the content or lower the "
            f"[behavior] min_content_tokens config setting."
        )


def _validate_write_payload(
    *,
    content: str,
    scopes: list[str],
    confidence: str,
    source: str,
    allowed_scopes: list[str],
    category: str = "fact",
    max_content_bytes: int = 0,
    min_content_tokens: int = 0,
    max_scopes_per_write: int = 0,
) -> dict[str, Any]:
    """Validate and normalise the kwargs for `Store.write`.

    Returns a dict suitable for `Store.write(**payload)`. Raises ValueError
    on any input problem so the model gets a clear error.

    `min_content_tokens` defaults to 0 (floor disabled) so an omitted
    argument is byte-identical to the pre-floor behaviour; callers thread
    `[behavior] min_content_tokens` in. See `_validate_content_floor` for
    which tools the floor reaches when it is enabled.
    """
    if not content or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not scopes:
        raise ValueError("scopes must contain at least one entry")
    _validate_content_floor(content, min_content_tokens)
    _validate_content_size(content, max_content_bytes)
    _validate_scope_count(scopes, max_scopes_per_write)

    clean_scopes = [validate_scope(s) for s in scopes]

    if allowed_scopes:
        allowed_set = set(allowed_scopes)
        unknown = [s for s in clean_scopes if s not in allowed_set]
        if unknown:
            raise ValueError(
                f"scope(s) not in allowed list: {unknown}. "
                f"Allowed: {sorted(allowed_scopes)}"
            )

    try:
        conf_enum = Confidence(confidence)
    except ValueError as exc:
        raise ValueError(
            f"confidence must be one of {[c.value for c in Confidence]}"
        ) from exc
    try:
        src_enum = Source(source)
    except ValueError as exc:
        raise ValueError(f"source must be one of {[s.value for s in Source]}") from exc

    if category not in _WRITE_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(_WRITE_CATEGORIES)}")
    cat_enum = Category(category)

    return {
        "content": content,
        "scopes": clean_scopes,
        "confidence": conf_enum,
        "source": src_enum,
        "category": cat_enum,
    }


def _validate_declared_claims(
    claims: list[str],
    *,
    worktree_root: str | None,
    surface: str,
) -> list[str]:
    """Type-check, parse, and oracle-check a declared `claims` list.

    Returns the canonical rendered forms (deduplicated, values
    `repr`-normalized) — what gets persisted. Raises ValueError naming
    the exact defect, because the refusal is where a caller learns the
    contract: claims are declared true-right-now against the worktree,
    or not at all. Shared by `memory_write` (fresh origin capture) and
    `memory_verify` (the memory's recorded origin) so the two surfaces
    cannot drift on what a valid claim is — the same lockstep argument
    as the four commit-drift surfaces.

    `surface` names the caller in the refusal ("memory_write" /
    "memory_verify"); the remedies differ only in where the worktree
    root comes from, and the message says which one was missing.
    """
    from pathlib import Path

    if not isinstance(claims, list) or not all(isinstance(s, str) for s in claims):
        raise ValueError("claims must be a list of strings if provided")
    parsed = parse_claims(claims)
    if not parsed:
        return []
    if worktree_root is None:
        raise ValueError(
            f"claims are checked against a worktree at declaration, and "
            f"{surface} has none to check against"
            + (
                " (the caller is not inside a git worktree). Write from "
                "the repo the claims are about, or drop the claims."
                if surface == "memory_write"
                else " (the memory records no origin worktree). Re-write "
                "the memory from its repo, or drop the claims."
            )
        )
    root = Path(worktree_root)
    if not root.is_dir():
        raise ValueError(
            f"claims cannot be checked: the origin worktree "
            f"{worktree_root!r} is not visible from this machine. "
            "Declare claims from a machine that can see the tree."
        )
    failures = [
        (claim.render(), reason)
        for claim in parsed
        if (reason := check_claim(claim, root)) is not None
    ]
    if failures:
        detail = "; ".join(f"{rendered}: {reason}" for rendered, reason in failures)
        raise ValueError(
            f"{len(failures)} claim(s) do not hold against the worktree — "
            f"{detail}. Claims are declared true-right-now; fix the claim "
            "(or the tree) before declaring it."
        )
    return [claim.render() for claim in parsed]


def _drain_pending_expired(state: SessionState, recorder: Recorder) -> None:
    """Emit one `pending_expired` event per pending write that hit its
    TTL since the last drain.

    Pre-2.6.8 expiry was a silent map deletion — a user saying "yes,
    save it" 61 minutes after the prompt would see `memory_write_confirm`
    fail with "no pending write" and have no way to know it had been
    evicted. The recorder log now carries the eviction so the eval
    surface can render a curation cue, and the confirm handler can
    distinguish "expired" from "never existed" via
    `state.was_recently_expired`.
    """
    drained = state.pop_recently_expired()
    if not drained:
        return
    for pending in drained:
        # `category` is the headline payload field used to distinguish
        # user-inference writes (the always-pending tier) from regular
        # writes. Surface it so the curation cue downstream can tell
        # which tier was lost — losing a user-inference confirmation
        # is worse than losing a plain fact.
        category = None
        payload = pending.payload
        if isinstance(payload, dict):
            cat = payload.get("category")
            if isinstance(cat, str):
                category = cat
        recorder.record(
            "pending_expired",
            pending_id=pending.pending_id,
            ttl_seconds=int(time.time() - pending.created_at),
            category=category,
        )


def _drain_expired_use_tokens(
    state: SessionState,
    recorder: Recorder,
    *,
    override_ids: set[str] | None = None,
) -> list[PendingUseToken]:
    """Drain the wall-clock-evicted use-token stash and return only the
    tokens that were genuinely LOST — nothing settled them.

    This owns the whole dedup scan, live tokens included, because the
    scan reads the event log and one read per handler entry is the
    budget. `already_recorded` therefore covers both populations and
    the live purge happens here too.

    The false-expiry defect this closes is subtle and only shows on
    hookful stores: `SessionState.advance_turn` evicts expired tokens
    at the TOP of `_advance_turn`, BEFORE the dedup scan runs. A
    retrieval the Stop hook already settled at t=5s, followed by an
    idle gap past `_PENDING_USE_TOKEN_TTL_SECONDS`, is therefore out of
    `state.pending_use_tokens` by the time the scan looks — so a naive
    drain would report a settled retrieval as a loss, inverting the
    whole point of the event. Folding the drained batch into the scan
    as `extra_pending` is what makes the scan see it.

    `override_ids` closes the same defect on the THIRD settlement path,
    where the log scan cannot help at all. `memory_record_use` passes
    the ids it is recording for, and writes its `use` event only AFTER
    `_advance_turn` returns — so however far back the scan reaches, the
    settlement is not on disk yet when it runs. Here the caller's
    declared intent IS the evidence: an id the model is explicitly
    settling in this very handler entry is settled, and dropping it
    before the scan is what keeps one call from filing the same
    retrieval as both settled and lost. Dropped rather than reported,
    because the token is already out of the live map either way.

    Residual, recorded honestly: `iter_events_backward` reads active
    segments only, so a log rotation between the hook's `use` event and
    the eviction still yields a false expiry. That is the same residual the
    auto-commit dedup already carries, and implausible inside 30
    minutes at the default 10 MB cap.
    """
    expired = state.pop_expired_use_tokens()
    if override_ids:
        expired = [tok for tok in expired if tok.memory_id not in override_ids]
    if not recorder.enabled:
        # The pop above already happened — that is the point. Telemetry
        # being off must not let the stash grow without bound, and
        # there is nothing to dedup against when no events are written.
        return expired
    if not state.pending_use_tokens and not expired:
        return expired
    already_recorded = _already_recorded_pending_ids(
        state,
        recorder,
        extra_pending={tok.memory_id: tok.issued_at for tok in expired},
    )
    for mid in already_recorded:
        state.purge_use_token(mid)
    return [tok for tok in expired if tok.memory_id not in already_recorded]


def _emit_expired_use_tokens(
    state: SessionState,
    recorder: Recorder,
    expired: list[PendingUseToken],
) -> None:
    """Record ONE `use_token_expired` event for a batch of lost tokens.

    Batched rather than one-per-token (the shape `pending_expired`
    uses) because this population is the auto-commit's population: a
    single 20-hit search that goes unsettled would otherwise write 20
    events, inflating `total_events` in both `doctor.py`'s cadence
    census and health's accumulator — denominators that are supposed to
    count client activity, not fan-out.

    Field choices, all load-bearing:

    * `ids` — the canonical id-list field name, so every existing
      reader that already handles `ids` keeps working. Safe because
      every `.get("ids")` reader in `src/` sits inside a `kind` guard.
    * `age_seconds` / `turns_since_issue` — measured against the OLDEST
      token in the batch, i.e. the worst case. Both axes are reported
      because the two TTLs are independent: a token can die of wall
      clock while its turn delta says the session never advanced (the
      idle-session case) or with a large turn delta (the case where the
      auto-commit somehow never fired).
    * `reason` — names the eviction axis so a future second one (a
      session reset that decided to report its stash, say) does not
      have to be told apart by inference.

    Deliberately ABSENT: `outcome`, `auto` and `attribution`. This is
    not a `use` event and must never be read as one — an expiry is the
    absence of evidence, and `outcome="applied"` on evidence-free
    leftovers would manufacture endorsements out of exactly the
    retrievals that failed to earn any. `attribution` is also what
    `eval.is_admin_recorded_event` keys its second exclusion axis on;
    omitting it keeps the event (and its session) inside doctor's
    client-session census, where it belongs.
    """
    if not expired:
        return
    now = time.time()
    oldest = min(expired, key=lambda tok: tok.issued_at)
    recorder.record(
        "use_token_expired",
        ids=[tok.memory_id for tok in expired],
        age_seconds=int(now - oldest.issued_at),
        turns_since_issue=state.turn_counter - oldest.issued_at_turn,
        reason="wall_clock_ttl",
    )


def _advance_turn(
    state: SessionState,
    recorder: Recorder,
    *,
    override_ids: set[str] | None = None,
) -> None:
    """Bump the per-session turn counter and auto-commit any use-tokens
    that crossed their TTL.

    Called at the entry of every memory_* tool handler so the
    auto-`record_use` flow has a stable monotonic clock and the
    bookkeeping fires even on calls that don't issue new tokens
    (e.g. `memory_write`, `memory_health`). Telemetry-disabled
    recorders no-op, so this is safe to call unconditionally.

    `override_ids` is used by the `memory_record_use` path: ids the
    caller is explicitly recording for shouldn't be auto-committed
    as `applied` first — the explicit outcome wins. The session's
    `consume_old_tokens` accepts the same set so the exclusion is
    structural rather than racey. The expiry drain accepts it for the
    same reason and one more: its own settlement evidence is written
    after this function returns, so no amount of log scanning can find
    it (see `_drain_expired_use_tokens`).

    Hook-attributed ids (the Stop hook substring-matched a retrieved
    memory's body against the assistant turn and emitted a
    `record_use` event with `attribution="hook"`) are purged from
    the pending map before consume_old_tokens runs. The hook lives
    in a different process and can't touch this in-memory state, so
    its attribution is communicated through the event log. Without
    the purge, the auto-commit would fire a *second* `applied`
    event for the same retrieval — duplicating the audit signal and
    inflating the eval CLI's denominators.

    Auto-committed ids land in the event log under
    `kind="use", outcome="applied", auto=True, attribution="auto"`
    so the eval CLI can distinguish the three applied tiers (model
    explicit, hook attributed, auto fallback). Older events without
    `attribution` fall back to `model` when auto=false and `auto`
    when auto=true at read time.

    Tokens that reached the 30-minute wall-clock eviction without ANY
    of those three settling them are the fourth outcome, and the only
    one that used to be silent: they are drained here and emitted as
    `use_token_expired` (see `_drain_expired_use_tokens`). All three
    settling surfaces are subtracted first — the hook's and the
    auto-commit's via the log scan, the explicit one via
    `override_ids` — so the fourth outcome really is the leftovers.
    The drain runs before `consume_old_tokens` because the dedup purge
    it performs is the same purge the auto-commit depends on.
    """
    state.advance_turn()
    _drain_pending_expired(state, recorder)
    # Drain + dedup in one pass: `_drain_expired_use_tokens` also
    # purges the LIVE tokens the log already settled, which is why the
    # standalone purge loop that used to live here is gone.
    lost_tokens = _drain_expired_use_tokens(state, recorder, override_ids=override_ids)
    _emit_expired_use_tokens(state, recorder, lost_tokens)
    auto_ids = state.consume_old_tokens(override_ids=override_ids)
    if auto_ids:
        recorder.record(
            "use",
            ids=list(auto_ids),
            outcome="applied",
            auto=True,
            attribution="auto",
        )


def _event_ts_epoch(raw: Any) -> float | None:
    """Parse the recorder's ISO-8601 `ts` (always UTC, trailing `Z`) into
    a POSIX epoch. Returns None on a malformed value so the caller can
    skip the event without crashing.

    Routes through the canonical `parse_event_ts` so the parse semantics
    stay one definition; the epoch projection is local because the only
    caller (the pending-token consume loop) needs an epoch for
    comparison against `PendingUseToken.issued_at` (a wall-clock float).
    """
    parsed = parse_event_ts(raw)
    return parsed.timestamp() if parsed is not None else None


def _already_recorded_pending_ids(
    state: SessionState,
    recorder: Recorder,
    *,
    extra_session_ids: set[str] | None = None,
    extra_pending: dict[str, float] | None = None,
) -> set[str]:
    """Return the subset of pending-token memory_ids that already have
    a `use` event in the log emitted AFTER the token was issued.

    Generalises the pre-2.6.8 hook-only scan (`_hook_attributed_pending_ids`)
    to cover three race classes the auto-fallback would otherwise
    double-emit against:

    1. Stop-hook attribution (out-of-process — the hook writes a
       `use, attribution="hook"` event that the in-memory state can
       only see by reading the log).
    2. Explicit model `record_use` that landed in the log *after* a
       prior search re-issued a token for the same id (in-process
       state's `purge_use_token` covers the same-turn case; the log
       scan catches the same-id-different-turn re-issue case).
    3. Any future attribution tier added to the log without a matching
       in-memory hook.

    Session-id bridge (load-bearing): the dedup must accept a `use`
    event whose `session` is EITHER the in-process server session
    (`recorder.session_id`, the `sess_<hex>` the auto-fallback runs
    under) OR the Stop hook's transcript id. The production Stop hook
    builds its Recorder with `session_id=<Claude Code transcript id>`
    (see `hook.run_audit`), so the `applied, attribution="hook"` event
    it writes is stamped `session=<transcript_id>` — a DIFFERENT id
    space from the server's. A bare `== recorder.session_id` filter
    never matched it, so the pending token wasn't purged and
    `_advance_turn` fired a SECOND `applied, attribution="auto"` event
    for the same retrieval, permanently double-counting in the
    append-only log. This mirrors the bridge the hook already applies
    on its own retrieval side (`_emit_hook_attributions` dedups against
    `used_session_ids={retrieval_session, session_id}`); the dedup side
    was the missing half.

    The bridge is decided PER EVENT, from the event alone: a `use`
    event under a foreign session counts as the hook's when it itself
    carries the `triggered_from="stop_hook"` tag (with a string
    `session`). No pre-pass recovering the transcript id(s) from the
    log is needed, because the tag is stamped by the producer on the
    very events being matched: the hook stamps `triggered_from=
    "stop_hook"` on every event it writes — both `use` shapes in
    `hook._emit_hook_attributions` included — and the hook's Recorder
    is the ONLY producer that writes `use` events under a
    transcript-id session (the explicit `memory_record_use` handler
    and the `_advance_turn` auto-fallback write under
    `recorder.session_id`; the CLI acknowledge-debt path mints its own
    `sess_<hex>`). So for `use` events, "session appears on some
    stop-hook event in the log" — what the pre-pass set derivation
    collected — and "the event carries the tag" select the same
    events, and deriving per event is what lets the backward scan stop
    parsing at the early-exit boundary instead of paying a full-log
    pre-pass on every turn. Accepting ANY tagged `use` event — a prior
    server lifetime's, a concurrent window's transcript — is the same
    over-breadth the derived set had, and it is safe for the same
    reason: the per-id timestamp guard below, not the session match,
    is the correctness boundary. `extra_session_ids` lets a caller
    thread additional ids explicitly (e.g. a known transcript id); it
    is unioned with the server session.

    `extra_pending` is `{memory_id: issued_at}` for tokens that are no
    longer in `state.pending_use_tokens` but still need the same
    "did anything settle this?" answer — in practice the batch
    `_drain_expired_use_tokens` just popped out of the wall-clock
    eviction stash. Without it the scan cannot see them at all
    (eviction runs at the top of `_advance_turn`, before this scan) and
    a hook-settled retrieval would be reported as a loss. Entries also
    widen the backward-scan boundary below, so the older mint times
    are actually reached. On the (production-impossible, since the
    stash is drained in the same handler entry that filled it) collision
    where an id is in BOTH maps, `extra_pending` wins: erring toward the
    older mint time can only suppress a settlement, never manufacture a
    phantom loss report.

    The `event.ts >= token.issued_at` filter is load-bearing: without
    it, a stale `use` event for the same id (from an earlier retrieval
    in the same session, or replay-after-rotation) would falsely purge
    a freshly-issued token. The pre-2.6.8 hook-only scan had the same
    bug — it just happened only on the hook path. The filter is also
    what keeps the session bridge safe: a stop-hook `use` event from a
    prior server lifetime still bridges by its tag, but an event older
    than any live token's mint time is still excluded per-id.

    Streams the active event log BACKWARD (`iter_events_backward`,
    newest-first, one lazy json-parse per pulled line) and early-exits
    once events fall behind the oldest pending token's `issued_at`:
    any event older than that cannot have recorded any of our pending
    tokens (since the tokens were minted after that point). Because
    the reader parses lazily, the early-exit bounds the PARSE cost as
    well as the loop — a materialise-then-reverse
    `list(iter_events(root))` pays the whole active log's parse before
    the loop starts (measured: 14.0 ms of a 15.6 ms call, with the
    loop examining ONE event), which made this scan a per-turn O(N)
    tax. Still bounded by the rotation cap (default 10 MB) in the
    no-early-exit worst case, and only invoked when there ARE pending
    tokens.
    """
    if not state.pending_use_tokens and not extra_pending:
        return set()
    pending_issued_at = {
        mid: tok.issued_at for mid, tok in state.pending_use_tokens.items()
    }
    pending_issued_at.update(extra_pending or {})
    # Oldest pending token's mint time. Any event timestamped before
    # this cannot have recorded any of these tokens, so we can stop
    # the backward scan as soon as we cross that boundary.
    oldest_pending_issued_at = min(pending_issued_at.values())
    out: set[str] = set()
    # `use` events under these sessions count as ours with no further
    # evidence: the in-process server session, plus any ids the caller
    # threads explicitly. The Stop hook's transcript id(s) are NOT
    # pre-collected from the log — the hook stamps every event it
    # writes, so the bridge is decided per event inside the loop. See
    # the docstring's session-id bridge note.
    allowed_sessions = {recorder.session_id}
    if extra_session_ids:
        allowed_sessions |= extra_session_ids
    for event in iter_events_backward(recorder.root):
        ev_ts = _event_ts_epoch(event.get("ts"))
        if ev_ts is not None and ev_ts < oldest_pending_issued_at:
            # Every later-yielded event has an `ev_ts` that's older
            # still (the reader yields newest-first), so no remaining
            # event can satisfy `ev_ts >= issued` for any pending
            # token. Early-exit — and because the reader parses each
            # line only as it is pulled, this also ends the JSON
            # parsing, not just the loop.
            break
        if event.get("kind") != "use":
            continue
        # `use` events always carry `session` (the Recorder stamps it
        # on every event); `session_id` only appears on events whose
        # producer passed it explicitly (`turn_audited` / `search_miss`).
        # The `or` keeps the read robust regardless — canonical-first,
        # the discipline 70e41a4 established for llm.py.
        session = event.get("session") or event.get("session_id")
        if session not in allowed_sessions and not (
            isinstance(session, str) and event.get("triggered_from") == "stop_hook"
        ):
            # The isinstance guard keeps the bridge on real identities:
            # a tagged event with a missing/non-string session carries
            # nothing to match across id spaces and never bridged under
            # the derived-set shape either.
            continue
        if ev_ts is None:
            continue
        # Legacy fallback for `memory_ids` — same class as the 70e41a4
        # fix. Pre-2.6.3 `use` events landed with `memory_ids=[…]`
        # before the Recorder canonicalized to `ids=[…]`.
        ids = event.get("ids") or event.get("memory_ids") or []
        if not isinstance(ids, list):
            continue
        for mid in ids:
            if not isinstance(mid, str):
                continue
            issued = pending_issued_at.get(mid)
            if issued is None:
                continue
            # Tolerance: clock skew between Recorder.record() (UTC-now
            # at log time) and PendingUseToken.issued_at (wall-clock at
            # mint time) is well under a second. Strict `>=` keeps the
            # invariant: the event must reflect a use attribution that
            # happened *after* this token was minted, not an older one
            # left over from a previous search-of-same-id cycle.
            if ev_ts >= issued:
                out.add(mid)
    return out


# Legacy alias kept for any out-of-tree caller; the new name is more
# accurate now that the scan also covers explicit model/hook events
# beyond the original hook-only role.
_hook_attributed_pending_ids = _already_recorded_pending_ids


def _attach_use_tokens(
    out: list[dict[str, Any]],
    state: SessionState,
) -> None:
    """Mint a `use_token` for each hit dict and inject it into the dict.

    Tokens are minted in bulk (`state.issue_use_tokens`) rather than
    per-hit to keep the secret-generation cost off the response's
    critical path on large result sets. Re-issuing for an id whose
    previous token is still pending is fine — the new token replaces
    the old, and the old one can never be exchanged.
    """
    if not out:
        return
    ids = [h["id"] for h in out]
    tokens = state.issue_use_tokens(ids)
    for h in out:
        h["use_token"] = tokens[h["id"]]


def _maybe_attach_curation_hint(
    response: dict[str, Any],
    deps: Any,
    state: SessionState,
) -> None:
    """One-shot per-session passive curation-pressure surface.

    Closes the in-conversation surfacing loop the audit identified:
    `curation_pending` aggregation has lived on
    `memory_scope_overview` since 2.7.x, but the model has to call
    that tool to see it. When pressure crosses the configured
    threshold, attach a `curation_hint` block to the first
    `memory_write` response of the session so a model that never
    asks for `memory_scope_overview` still gets the nudge.

    Pull-based discovery (calling `memory_health` /
    `memory_scope_overview`) remains the primary surface. This is a
    passive notification, not auto-detour. Flipped off by setting
    `curation_hint_enabled = False` or `curation_hint_threshold = 0`
    in `[behavior]`.

    Cost: one `curation_counts` walk over the event log + a
    `load_all` of the memory directory. Both are bounded (rotation
    cap on the log, store size in practice). Paid once per session
    because the underlying counts (dead_weight, drifted,
    cold_endorsement_memories) accumulate across sessions and don't
    shift meaningfully within one, so re-walking on every write
    would burn cost for no signal.
    """
    if state.curation_hint_checked:
        return
    behavior = deps.config.behavior
    if not behavior.curation_hint_enabled:
        return
    threshold = behavior.curation_hint_threshold
    if threshold <= 0:
        return

    # Mark checked unconditionally so a session that doesn't cross the
    # threshold doesn't re-pay the walk on every subsequent write.
    state.curation_hint_checked = True

    from ..events import iter_all_events
    from ..health import curation_counts

    counts = curation_counts(
        deps.store.load_all(),
        iter_all_events(deps.store.root),
        window_days=30,
        verification_stale_days=behavior.verification_stale_days,
        cold_endorsement_ratio_threshold=behavior.cold_endorsement_ratio_threshold,
        # Arm the dead-weight telemetry gate — production entry point.
        # `0` (rather than a pre-measured count) because the event
        # stream above is a generator handed straight in; zero turns the
        # gate on and delegates the measurement to the walk
        # `curation_counts` is about to do. Without this the hint would
        # nag about dead weight that is really an unwired Stop hook —
        # and it is the one curation surface the model does not have to
        # ask for.
        hook_telemetry_events=0,
    )
    pressure = counts["dead"] + counts["drifted"] + counts["cold_endorsement_memories"]
    if pressure < threshold:
        return

    # Every route named below has to exist on the surface the hint can
    # actually reach. This fires on `memory_write`, which is registered
    # under BOTH surfaces, and `load_config()` defaults
    # `full_tool_surface` to false — so the stock install reading this
    # message has no `memory_health` to call. The full-bucket route is
    # therefore the `bettermemory health` CLI, which every install ships.
    # `tests/test_server.py` ratchets this twice over: the message may
    # not name a tool the lean server doesn't register, and every
    # backticked `bettermemory <subcommand>` it names is resolved against
    # the argparse subparsers the CLI actually registers — a renamed
    # route fails the build here rather than misdirecting the model.
    #
    # The remedies are also per-axis rather than a shared "or". Cold
    # endorsements are defined by `explicit_applied_count == 0`
    # (health._is_weakly_endorsed), and `memory_verify` writes a
    # verification, not a use event — it cannot decrement that counter,
    # so naming it here aimed the drift remedy at a bucket it cannot
    # move. The thing that does move it is `consolidate
    # --acknowledge-debt`, which writes one explicit `use(applied)` per
    # cold row; it is what `memory_health`'s own
    # `cleanup_cold_endorsements` recommendation names.
    response["curation_hint"] = {
        "pressure": pressure,
        "threshold": threshold,
        "counts": {
            "dead_weight": counts["dead"],
            "drifted": counts["drifted"],
            "cold_endorsement_memories": counts["cold_endorsement_memories"],
        },
        "message": (
            f"Curation pressure {pressure} >= threshold {threshold}: "
            f"{counts['dead']} dead_weight + {counts['drifted']} drifted + "
            f"{counts['cold_endorsement_memories']} "
            "cold_endorsement_memories. Run `bettermemory health` for the "
            "full buckets. Drifted: memory_update the rotted claims, then "
            "memory_verify. Dead weight: memory_remove. Cold endorsements: "
            "`bettermemory consolidate --acknowledge-debt` (memory_verify "
            "does not touch that axis). One-shot per session."
        ),
    }


__all__ = [
    "Context",
    "_AMBIENT_LONG_BODY_WORDS",
    "_NOTE_MAX_LEN",
    "_USE_OUTCOMES",
    "_WRITE_CATEGORIES",
    "_advance_turn",
    "_already_recorded_pending_ids",
    "_attach_use_tokens",
    "_drain_expired_use_tokens",
    "_drain_pending_expired",
    "_emit_expired_use_tokens",
    "_event_ts_epoch",
    "_hook_attributed_pending_ids",
    "_maybe_attach_curation_hint",
    "_validate_content_floor",
    "_validate_content_size",
    "_validate_declared_claims",
    "_validate_scope_count",
    "_validate_write_payload",
]
