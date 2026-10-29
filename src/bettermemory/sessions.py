"""The session join: the in-process server sessions a hook's
conversation is served by, read off the event log.

A client-side hook (the Stop audit, the prompt recall, the session brief)
records under the harness's transcript session id, while the in-process
MCP server records its tool events under its own per-process id
(`sess_<hex>`). The functions here join the two id spaces from the event
rows alone: the harness join first (tool events stamped with the hook's
session id as their `events.HARNESS_SESSION_FIELD`), the worktree bridge
(`_latest_in_process_session`) only when nothing is stamped
(`bridged_session`, `joined_sessions`, `event_joined`). The
session-disabled scope replay (`_disabled_scopes_from_events`) is anchored
by the same join.

The Stop audit (`hook`), the recall surface (`recall`), the recall review
and its counters, and the capture batch read the join. This module
imports only `events` and `time_utils`, so each of them reads it without
importing another; `hook` re-exports every name here under the path it
had before the join moved out of it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any

from .events import HARNESS_SESSION_FIELD
from .time_utils import parse_event_ts

# `triggered_from` values stamped by OUT-OF-PROCESS writers: the two
# client-side hooks, which record under Claude Code's transcript session
# id rather than the server's `sess_<hex>`. `_latest_in_process_session`
# must skip every member when recovering the live server session: the
# bridge's premise is "the newest event not written by a hook was
# written by the in-process server", and a `prompt_recall` row admitted
# as an anchor would hand the shield a transcript id that matches no
# server-emitted retrieval event, silently killing it (the same
# structurally-dead-shield failure the `retrieval_session_id` bridge
# exists to prevent). `audit._VALID_TRIGGERED_FROM` cross-references
# this set; extend both together. `session_hook` is the third: the
# SessionStart hook's `session_brief` row (`recall.session_brief`),
# recorded under the transcript id like the prompt hook's.
# `session_capture` stays for the rows 8.x's `bettermemory capture`
# recorded under a transcript id, which a migrated store still holds;
# nothing writes it now, so it is no longer a valid trigger to stamp.
_OUT_OF_PROCESS_TRIGGERS: frozenset[str] = frozenset(
    {"stop_hook", "prompt_hook", "session_capture", "session_hook"}
)


def _disabled_scopes_from_events(
    events: list[dict[str, Any]],
    *,
    worktree_root: str | None = None,
    server_session: str | None = None,
) -> set[str]:
    """Reconstruct the session-disabled scope set from the event log.

    `memory_scope_disable` / `memory_scope_enable` append `scope_disable`
    / `scope_enable` events stamped with the MCP server's stable
    per-process session id. The Stop hook runs in a separate process and
    can't read the server's in-memory `SessionState.disabled_scopes`, but
    it CAN replay those events off disk.

    The replay is anchored to the *current in-process server session*:
    the session id of the most recent event that did NOT originate from a
    Stop hook (see `_latest_in_process_session`; `worktree_root` is this
    hook's worktree, threaded through so a concurrent session in another
    worktree can't hijack the anchor and leak its scope toggles into this
    window's probe exclusions). Anchoring this way is what preserves
    reset-on-restart semantics: a restarted server mints a fresh session
    id and has emitted no scope toggles under it yet, so the
    reconstructed set is empty until the user disables a scope again,
    mirroring the in-memory state the server itself reset on startup.

    Returns the net disabled set: each `scope_disable` adds, each later
    `scope_enable` removes, walked in chronological (append) order. Empty
    when no in-process session is found or it toggled no scopes.
    `server_session`, when given, is the anchor the caller already
    resolved (`bridged_session`, the harness join first).
    """
    if server_session is None:
        server_session = _latest_in_process_session(events, worktree_root=worktree_root)
    if server_session is None:
        return set()
    disabled: set[str] = set()
    for event in events:
        # Canonical-first session lookup with legacy fallback, the same
        # discipline `hook._pending_retrievals` uses.
        if (event.get("session") or event.get("session_id")) != server_session:
            continue
        kind = event.get("kind")
        if kind not in ("scope_disable", "scope_enable"):
            continue
        scope = event.get("scope")
        if not isinstance(scope, str):
            continue
        if kind == "scope_disable":
            disabled.add(scope)
        else:
            disabled.discard(scope)
    return disabled


def _latest_in_process_session(
    events: list[dict[str, Any]],
    *,
    worktree_root: str | None = None,
) -> str | None:
    """Session id of the most recent in-process event, or None.

    In-process MCP tool calls and the client-side hooks write to the
    same event log under different session-id spaces (the server's
    `sess_<hex>` vs. Claude Code's transcript session id). Hook events
    tag `triggered_from` with a member of `_OUT_OF_PROCESS_TRIGGERS`
    (`"stop_hook"` for the Stop hook, `"prompt_hook"` for the
    UserPromptSubmit recall); everything else is in-process. The last
    such event identifies the live server session whose disabled-scope
    toggles the hook should honour. `events` is in chronological (append)
    order, so the latest match is found by walking in reverse.

    `worktree_root` makes the anchor concurrency-safe: the store and
    event log are shared across ALL projects and processes, so with two
    concurrent Claude Code windows the latest event frequently belongs
    to the OTHER window's server, which used to hijack the anchor and
    flip the audit verdict both ways (the foreign session's search
    shielding this window's miss; the foreign session's unrelated events
    unshielding a turn that searched correctly). The server-side
    Recorder already stamps `worktree_root` on every in-process event,
    so when the hook's own worktree is known we prefer the latest event
    whose stamp matches it. When no stamped match exists (legacy logs
    written before the stamp shipped, a server running outside any git
    checkout, or the restart gap before this worktree's server writes
    its first event) we fall back to the latest-any behaviour, which
    preserves the documented reset-on-restart semantics.
    """
    fallback: str | None = None
    for event in reversed(events):
        if event.get("triggered_from") in _OUT_OF_PROCESS_TRIGGERS:
            continue
        session = event.get("session") or event.get("session_id")
        if not isinstance(session, str):
            continue
        if worktree_root is None or event.get("worktree_root") == worktree_root:
            return session
        if fallback is None:
            fallback = session
    return fallback


def _session_of(event: dict[str, Any]) -> str | None:
    """The session id an event row was recorded under (the canonical
    `session`, else the legacy `session_id`), or None."""
    session = event.get("session") or event.get("session_id")
    return session if isinstance(session, str) else None


def harness_joined_sessions(
    events: Iterable[dict[str, Any]], harness_session: str | None
) -> set[str]:
    """The in-process sessions whose tool events carry `harness_session`
    (`events.HARNESS_SESSION_FIELD`, stamped from the shim's header or the
    in-process server's environment): the server sessions that served
    this conversation. Empty when nothing is stamped with it."""
    if not harness_session:
        return set()
    found: set[str] = set()
    for event in events:
        if event.get("triggered_from") in _OUT_OF_PROCESS_TRIGGERS:
            continue
        if event.get(HARNESS_SESSION_FIELD) != harness_session:
            continue
        session = _session_of(event)
        if session is not None:
            found.add(session)
    return found


def hook_spans(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[datetime, datetime]]:
    """Per harness session, the first and the last of its hook rows in
    `events` (rows a hook wrote: `triggered_from` names one)."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    for event in events:
        if event.get("triggered_from") not in _OUT_OF_PROCESS_TRIGGERS:
            continue
        session = event.get("session") or event.get("session_id")
        stamp = parse_event_ts(event.get("ts"))
        if not isinstance(session, str) or not session or stamp is None:
            continue
        span = spans.get(session)
        if span is None:
            spans[session] = (stamp, stamp)
        else:
            spans[session] = (min(span[0], stamp), max(span[1], stamp))
    return spans


def runs_beside(
    spans: Mapping[str, tuple[datetime, datetime]], other: str, conversation: str
) -> bool:
    """Whether harness session `other`'s hooks run beside `conversation`'s
    (`spans` from `hook_spans`): it has a hook row of its own, and, once
    `conversation` has one, a hook row at or after its first.

    A server's stamp is fixed when it is spawned (the in-process server
    reads CLAUDE_CODE_SESSION_ID once, the shim forwards what it was
    started with), while Claude Code issues a new session id on /clear
    and /resume. A stamp whose conversation shows no hook row, or went
    quiet before this one began, is this window's own server under its
    old id, not another window's; a conversation whose hooks still run
    is another window. Before `conversation` has a hook row of its own
    any conversation with one counts as running beside it, so a /resume
    whose brief showed nothing joins through the bridge from its second
    hook call. The hooks' join (`bridged_session`) and the eval's
    surfaces section (`recall_counters`) both read a stamp by this rule."""
    span = spans.get(other)
    if span is None:
        return False
    own = spans.get(conversation)
    return own is None or span[1] >= own[0]


def live_harness_sessions(
    events: Iterable[dict[str, Any]], harness_session: str | None
) -> set[str]:
    """The other harness sessions whose hooks run beside `harness_session`
    in `events` (`runs_beside`)."""
    if not harness_session:
        return set()
    spans = hook_spans(events)
    return {
        session
        for session in spans
        if session != harness_session and runs_beside(spans, session, harness_session)
    }


def bridged_session(
    events: list[dict[str, Any]],
    *,
    harness_session: str | None,
    worktree_root: str | None = None,
) -> str | None:
    """The in-process session a hook's rows join: the harness join first,
    the 9.0.0 worktree bridge only when it has nothing.

    The newest in-process event stamped with this hook's harness session
    names the session (a restarted server stamps the same harness id, so
    the newest is the live one, the reset-on-restart the bridge keeps).
    With no such event, `_latest_in_process_session` answers, over the
    events of every session NOT stamped with another live harness session
    (`runs_beside`): a server that declared it serves another window's
    conversation is never this one's bridge, while a server stamped with
    this window's id from before a /clear or /resume is found by the
    bridge as 9.0.0 found it.

    The bridge never chooses between two such servers the log cannot
    tell apart: when servers stamped with two or more ids that do not
    run beside this conversation have written in this checkout since
    its first hook row, every server stamped with another id is set
    aside, as before the /clear repair. That is the case of a /clear in
    one window while another window in the same checkout is in a turn
    that began before the clear (its last hook row predates this
    conversation's first): the other window's server looks exactly like
    this window's own under its old id until that window's next hook
    row, and the latest of the two would be a coin toss.

    What remains: a /resume's conversation joins through the bridge only
    from its second hook call (`runs_beside`); while another window's
    turn that began before a /clear is still running, the clear loses
    its join until that window's next hook row (a missed join, as before
    the repair); and when this window's server has not written since the
    clear while that turn's server has, that server is the only
    candidate and the bridge takes it until that window's next hook row
    (a foreign join: nothing on the log tells the two apart then; from
    the first row this window's server writes, it is the missed join)."""
    if harness_session:
        stamped_by: dict[str, str] = {}
        for event in reversed(events):
            if event.get("triggered_from") in _OUT_OF_PROCESS_TRIGGERS:
                continue
            stamped = event.get(HARNESS_SESSION_FIELD)
            if not isinstance(stamped, str) or not stamped:
                continue
            session = _session_of(event)
            if session is None:
                continue
            if stamped == harness_session:
                return session
            stamped_by.setdefault(session, stamped)
        if stamped_by:
            spans = hook_spans(events)
            foreign = {
                s
                for s, stamped in stamped_by.items()
                if runs_beside(spans, stamped, harness_session)
            }
            own = spans.get(harness_session)
            if own is not None:
                stamps: set[str] = set()
                for event in events:
                    if event.get("triggered_from") in _OUT_OF_PROCESS_TRIGGERS:
                        continue
                    session = _session_of(event)
                    if session is None or session not in stamped_by:
                        continue
                    if session in foreign:
                        continue
                    if worktree_root is not None and (
                        event.get("worktree_root") != worktree_root
                    ):
                        continue
                    stamp = parse_event_ts(event.get("ts"))
                    if stamp is not None and stamp >= own[0]:
                        stamps.add(stamped_by[session])
                if len(stamps) > 1:
                    foreign = set(stamped_by)
            if foreign:
                events = [e for e in events if _session_of(e) not in foreign]
    return _latest_in_process_session(events, worktree_root=worktree_root)


def joined_sessions(
    events: list[dict[str, Any]],
    *,
    harness_session: str | None,
    worktree_root: str | None = None,
) -> set[str]:
    """Every in-process session the harness join names, or the one the
    bridge names when it names none (`bridged_session`)."""
    found = harness_joined_sessions(events, harness_session)
    if found:
        return found
    bridged = bridged_session(
        events, harness_session=harness_session, worktree_root=worktree_root
    )
    return {bridged} if bridged is not None else set()


def event_joined(
    event: dict[str, Any], *, harness_session: str | None, sessions: Collection[str]
) -> bool:
    """Whether a tool event belongs to the hook's conversation: by its
    harness stamp when it names this conversation, else by its session
    (one of `sessions`, the joined in-process sessions and the hook's own
    id). A stamp naming another id joins through its session: the bridge
    puts a server in `sessions` only when its stamp is not another live
    window's (`bridged_session`), so a server stamped before a /clear
    stays joined."""
    if event.get(HARNESS_SESSION_FIELD) == harness_session and harness_session:
        return True
    return _session_of(event) in sessions


__all__ = [
    "bridged_session",
    "event_joined",
    "harness_joined_sessions",
    "hook_spans",
    "joined_sessions",
    "live_harness_sessions",
    "runs_beside",
]
