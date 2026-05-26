"""Cross-cutting helpers every MCP tool handler reaches for.

The per-tool modules in this package own their happy-path logic; the
helpers here own the bookkeeping every handler runs (turn counter
advance, pending-write TTL drain, use-token attribution scan, payload
validation, event log timestamp parsing).

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

from mcp.server.fastmcp import Context as _FastMCPContext

from ..events import Recorder, iter_events
from ..models import Category, Confidence, Source, validate_scope
from ..session import SessionState
from ..time_utils import parse_event_ts


# Local alias filling FastMCP's three generic params with Any — the
# handlers only ever read `ctx.client_id`, never the typed
# lifespan/request/session data, so unconstrained generics are the
# right shape. Aliasing once via `TypeAlias` (not a bare runtime
# assignment) keeps every handler signature readable AND keeps strict
# checkers happy — a plain `Context = X[Any, ...]` would type-check on
# mypy but trip "Variable not allowed in type expression" on
# Pyright/Pylance.
Context: TypeAlias = _FastMCPContext[Any, Any, Any]


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
# `memory_record_use` events. The web UI already enforces 500 chars on
# the /verify POST — this is the matching cap for the MCP entry points,
# so a hostile client (or a runaway model) can't inflate the JSONL
# event log with multi-megabyte notes. 500 chars covers any reasonable
# rationale ("verified against commit abc123" sort of thing); pasting
# whole transcripts belongs in a memory body, not in an event note.
_NOTE_MAX_LEN = 500


# ---------------------------------------------------------------------------
# Validation + per-handler bookkeeping helpers.
# ---------------------------------------------------------------------------


def _validate_content_size(content: str, max_bytes: int) -> None:
    """Reject memory bodies whose UTF-8 byte length exceeds `max_bytes`.

    A no-op when `max_bytes <= 0` (cap disabled). Centralised so that
    `memory_write`, `memory_update`, and any future write entry point
    share the same bound. The check is on encoded byte length rather
    than character count because that's the unit that lands on disk
    and in the JSONL event log — a body of CJK or emoji characters
    expands meaningfully under UTF-8 encoding.
    """
    if max_bytes <= 0:
        return
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > max_bytes:
        raise ValueError(
            f"content exceeds max_content_bytes "
            f"({encoded_size} bytes > {max_bytes} bytes). "
            f"Split into multiple memories or raise the "
            f"[behavior] max_content_bytes config setting."
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
) -> dict[str, Any]:
    """Validate and normalise the kwargs for `Store.write`.

    Returns a dict suitable for `Store.write(**payload)`. Raises ValueError
    on any input problem so the model gets a clear error.
    """
    if not content or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not scopes:
        raise ValueError("scopes must contain at least one entry")
    _validate_content_size(content, max_content_bytes)

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
    structural rather than racey.

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
    """
    state.advance_turn()
    _drain_pending_expired(state, recorder)
    if state.pending_use_tokens and recorder.enabled:
        already_recorded = _already_recorded_pending_ids(state, recorder)
        for mid in already_recorded:
            state.purge_use_token(mid)
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

    The `event.ts >= token.issued_at` filter is load-bearing: without
    it, a stale `use` event for the same id (from an earlier retrieval
    in the same session, or replay-after-rotation) would falsely purge
    a freshly-issued token. The pre-2.6.8 hook-only scan had the same
    bug — it just happened only on the hook path.

    Reads the active event log BACKWARD (most-recent first) and early-
    exits once events fall behind the oldest pending token's
    `issued_at`: any event older than that cannot have recorded any of
    our pending tokens (since the tokens were minted after that point).
    Bounded by the rotation cap (default 10 MB) and only invoked when
    there ARE pending tokens. The simplest correct approach: list
    the active log once and iterate `reversed(...)` — the cap keeps
    the materialised list bounded.
    """
    if not state.pending_use_tokens:
        return set()
    pending_issued_at = {
        mid: tok.issued_at for mid, tok in state.pending_use_tokens.items()
    }
    # Oldest pending token's mint time. Any event timestamped before
    # this cannot have recorded any of these tokens, so we can stop
    # the backward scan as soon as we cross that boundary.
    oldest_pending_issued_at = min(pending_issued_at.values())
    out: set[str] = set()
    # Materialise once and iterate reversed. The active log is bounded
    # by the rotation cap (default 10 MB ≈ tens of thousands of events),
    # so the list is bounded too — and the early-exit below typically
    # bails after a handful of recent events.
    events = list(iter_events(recorder.root))
    for event in reversed(events):
        ev_ts = _event_ts_epoch(event.get("ts"))
        if ev_ts is not None and ev_ts < oldest_pending_issued_at:
            # Every earlier event has an `ev_ts` that's older still
            # (the active log is append-only by wall-clock), so no
            # remaining event can satisfy `ev_ts >= issued` for any
            # pending token. Early-exit.
            break
        if event.get("kind") != "use":
            continue
        # `use` events always carry `session` (the Recorder stamps it
        # on every event); `session_id` only appears on events whose
        # producer passed it explicitly (`turn_audited` / `search_miss`).
        # The `or` keeps the read robust regardless — canonical-first,
        # the discipline 70e41a4 established for llm.py.
        if (event.get("session") or event.get("session_id")) != recorder.session_id:
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


__all__ = [
    "Context",
    "_AMBIENT_LONG_BODY_WORDS",
    "_NOTE_MAX_LEN",
    "_USE_OUTCOMES",
    "_WRITE_CATEGORIES",
    "_advance_turn",
    "_already_recorded_pending_ids",
    "_attach_use_tokens",
    "_drain_pending_expired",
    "_event_ts_epoch",
    "_hook_attributed_pending_ids",
    "_validate_content_size",
    "_validate_write_payload",
]
