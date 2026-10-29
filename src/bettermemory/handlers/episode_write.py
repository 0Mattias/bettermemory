"""episode_write MCP tool — handler implementation + DESC.

Episodes are the sibling-to-memory primitive for journal-shaped writes
the durability gate (`durability.TRANSIENT_PHRASE_MARKERS`) explicitly
rejects on `memory_write`: loop-iteration state, "what we tried",
run-local takeaways that need to survive one context reset but aren't
durable facts. Stored at `<root>/episodes/<session_id>/<ulid>.md`,
TTL-pruned (default 30 days) on each write so the directory stays
bounded without a separate cleanup pass.

Episode content is excluded from `memory_search`, `memory_health`,
`memory_list` — the write here lands in a sibling subtree the memory
iterators never see. `memory_health` reports the subtree's aggregate
volume (`episode_volume`) and nothing else about it.

The durability gate does not apply here; the credential gate does. An
episode body and takeaway land in the store and in the signed log's
`episode_put` row, which no path rewrites, and the handoff delivers them
to the next session. A body or takeaway carrying a secret-shaped token
(`credentials.find_credential_markers`) is refused before anything is
written, in the shape memory_write's `credential_warning` takes. Unlike
memory_write there is no `acknowledge_credential`: the episode tool takes
no override, so the only way through is to write the episode without the
value.

The control-token gate applies too, after the credential gate, as on
memory_write: a body or takeaway carrying a chat-template control token
(`control_tokens.find_control_tokens`, the detector `ControlTokenGate`
and memory_update run) is refused in memory_write's
`control_token_warning` shape, since the handoff reads the episode into
the next session's context, where the token can forge a turn boundary.
No flag passes it on any of the three tools.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from .. import identity
from ..control_tokens import find_control_tokens
from ..credentials import find_credential_markers
from ..events import HARNESS_SESSION_FIELD
from ..store import error_code_name
from ._shared import (
    Context,
    _advance_turn,
    _validate_content_size,
    _validate_scope_count,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers

log = logging.getLogger("bettermemory.handlers.episode_write")


#: The refusal's hint. It names the one remedy there is: the episode tool
#: has no acknowledge flag, so a caller that reads memory_write's hint and
#: re-sends with `acknowledge_credential=True` would only meet the
#: unknown-parameter error.
_CREDENTIAL_HINT = (
    "Not written: the episode carries a secret-shaped token (API key, "
    "private-key PEM, JWT, or a `password=…`-style assignment), and an "
    "episode lands in the signed log and the next session's handoff. "
    "Episodes take no acknowledgement: remove the value and write the "
    "episode again, describing the secret without embedding it (e.g. 'the "
    "deploy uses an AWS key, stored in 1Password'). The value is redacted "
    "from this warning and the event log."
)

#: The control-token refusal's hint, in the same terms: no flag passes it.
_CONTROL_TOKEN_HINT = (
    "Not written: the episode carries a chat-template control token, "
    "named in `markers` by its template family, its bare name and the "
    "field it sits in, and an episode lands in the signed log and the "
    "next session's handoff, where the token can forge a turn boundary. "
    "Episodes take no acknowledgement: write the token's bare name "
    "without its delimiters (`im_start`, `INST`) or describe the format "
    "in words. A marker of kind `anthropic` is `Human:` or `Assistant:` "
    "opening the field or a line after a blank line: quote the line "
    "(`> Human: ...`) or name the speaker another way. Then write the "
    "episode again."
)


def _credential_refusal(
    deps: ToolHandlers, body: str, takeaway: str | None
) -> dict[str, Any] | None:
    """The `credential_warning` refusal for a body or takeaway carrying a
    secret-shaped token, or None when neither does.

    Each field is scanned on its own, so a detector whose context reaches
    across a line (the PEM header's key-material lookahead) reads the
    field as written. Each marker is memory_write's (`kind` and a snippet
    with every secret span redacted) plus the `field` it was found in,
    since an episode has two. The event names the kinds and the fields,
    never the value; it carries no harness session, so a refused write
    does not count as the conversation having journaled
    (`episode_handoff._quiet_session`)."""
    markers: list[dict[str, Any]] = []
    for field, text in (("body", body), ("takeaway", takeaway)):
        if not text:
            continue
        for hit in find_credential_markers(text):
            markers.append({**deps.responses.credential_to_dict(hit), "field": field})
    if not markers:
        return None
    deps.recorder.record(
        "episode_write",
        status="credential_warning",
        credential_kinds=list(dict.fromkeys(m["kind"] for m in markers)),
        credential_fields=list(dict.fromkeys(m["field"] for m in markers)),
    )
    return {
        "status": "credential_warning",
        "markers": markers,
        "hint": _CREDENTIAL_HINT,
    }


def _control_token_refusal(
    deps: ToolHandlers, body: str, takeaway: str | None
) -> dict[str, Any] | None:
    """The `control_token_warning` refusal for a body or takeaway carrying
    a chat-template control token, or None when neither does.

    memory_write's markers (`kind`, the template family, and `token`, the
    bare name) plus the `field` each was found in. The event names the
    families and the fields only and, as the credential refusal's, carries
    no harness session, so a refused write does not count as the
    conversation having journaled."""
    markers: list[dict[str, Any]] = []
    for field, text in (("body", body), ("takeaway", takeaway)):
        if not text:
            continue
        for hit in find_control_tokens(text):
            markers.append({**hit.to_dict(), "field": field})
    if not markers:
        return None
    deps.recorder.record(
        "episode_write",
        status="control_token_warning",
        control_token_kinds=list(dict.fromkeys(m["kind"] for m in markers)),
        control_token_fields=list(dict.fromkeys(m["field"] for m in markers)),
    )
    return {
        "status": "control_token_warning",
        "markers": markers,
        "hint": _CONTROL_TOKEN_HINT,
    }


async def episode_write(
    deps: ToolHandlers,
    body: str,
    takeaway: str | None = None,
    scopes: list[str] | None = None,
    swarm_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `episode_write` MCP tool.

    Captures `session_id` from the recorder (the process-wide id every
    event in the log is tagged with), captures origin via the same
    shim the other handlers use, prunes old session dirs as a cheap
    side-effect of the write path, and returns the committed episode
    summary.
    """
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if not body or not body.strip():
        raise ValueError("episode body must be a non-empty string")
    # Mirror the size cap memory_write / memory_update enforce so a
    # multi-MB episode body can't slip past the write surface and land
    # on disk uncapped. Episodes share the same fsynced-file storage
    # path memories use; the DoS/disk-fill exposure is identical.
    # Raises ValueError with the same message shape as the memory_write
    # path, so the MCP error surface is uniform across both tiers.
    _validate_content_size(body, deps.config.behavior.max_content_bytes)
    # Cap the takeaway separately from the body. Takeaway lives in the
    # YAML frontmatter region, which `_frontmatter` caps at 64 KB to
    # neutralise alias-expansion DoS — a takeaway over that threshold
    # would corrupt the frontmatter, the loader would raise ValueError
    # on every subsequent read, and `list_by_session` would silently
    # skip the file. Net effect pre-fix: write returned status="committed"
    # but the episode vanished from every read surface (search /
    # handoff / promote). The default 4 KB cap is generous for the
    # documented "one-sentence summary" while keeping the frontmatter
    # comfortably inside the 64 KB ceiling. None-guard so absence of a
    # takeaway (the common case) doesn't trip the validator.
    if takeaway is not None:
        _validate_content_size(
            takeaway,
            deps.config.behavior.max_takeaway_bytes,
            field_name="takeaway",
            config_key="max_takeaway_bytes",
        )
    # Cap the scope list alongside the body + takeaway byte caps. Same
    # silent-data-loss class as t16: scopes serialise into YAML
    # frontmatter, and a ~2200-entry list would push the frontmatter
    # past `_frontmatter._MAX_YAML_BYTES`. The loader would then raise
    # `ValueError` on every subsequent read and the episode would
    # vanish from every read surface (search / handoff / promote)
    # despite the write returning `status="committed"`. Empty scope
    # list is valid (handoff keys on session_id), so no min-count check.
    if scopes is not None:
        _validate_scope_count(scopes, deps.config.behavior.max_scopes_per_write)
    # The credential gate, after the caps (a multi-MB body is refused by
    # size before any detector reads it, as on memory_write) and before
    # the origin is captured or anything reaches the store or the log;
    # then the control-token gate, in memory_write's order.
    refusal = _credential_refusal(deps, body, takeaway)
    if refusal is None:
        refusal = _control_token_refusal(deps, body, takeaway)
    if refusal is not None:
        return refusal

    origin = _h.capture_origin()
    # The request's session (the conversation's, under the daemon) is the
    # id the recorder tags on this call's events. Keying episodes on the
    # same id keeps the episode_handoff handler aligned with what the
    # event log actually carries.
    session_id = deps.recorder.session_for_request()

    # The episode is homed to the caller's checkout (design 3.6, 8.4),
    # resolved at its origin's cwd; the record, its home and HEAD as a
    # witness of the project commit in one transaction (design 8.1).
    from ..location import resolve_caller

    cwd = origin.cwd if origin is not None else None
    caller = resolve_caller(
        deps.store,
        deps.config,
        cwd or None,
        session=identity.current_harness_session(),
    )

    # `swarm_id` (when set) is validated by the Episode model's
    # `_check_swarm_id` (charset + length) inside `store.write`; an
    # invalid value raises ValueError that surfaces uniformly with the
    # body/takeaway/scope caps above.
    with deps.store.batch():
        episode = deps.store.write_episode(
            session_id=session_id,
            body=body,
            takeaway=takeaway,
            scopes=list(scopes or []),
            swarm_id=swarm_id,
            origin=origin,
            home=caller.home(),
        )
        caller.add_head_witness("episode")
    # Prune the sessions past the TTL as a side effect of the write path,
    # the active session exempt so a long-paused worktree's history
    # survives the pause. Best-effort: the episode is committed, and a
    # store that is busy or failing here leaves the old sessions for the
    # next write to prune rather than turning a kept write into an error.
    try:
        pruned = deps.store.prune_episode_sessions(keep_session_id=session_id)
    except sqlite3.Error as exc:
        log.warning(
            "episode %s: the prune after it did not run (%s: %s)",
            episode.id,
            error_code_name(exc),
            exc,
        )
        pruned = []

    # The conversation this server session serves, when the request named
    # it: the handoff reads it to know that conversation journaled
    # (decision B22, `episode_handoff._quiet_session`).
    harness = identity.current_harness_session()
    deps.recorder.record(
        "episode_write",
        id=episode.id,
        session=session_id,
        scopes=episode.scopes,
        has_takeaway=episode.takeaway is not None,
        swarm_id=episode.swarm_id,
        pruned_sessions=pruned,
        **({HARNESS_SESSION_FIELD: harness} if harness is not None else {}),
    )
    return {
        "status": "committed",
        "id": episode.id,
        "session_id": session_id,
        "created": episode.created.isoformat().replace("+00:00", "Z"),
        "scopes": episode.scopes,
        "takeaway": episode.takeaway,
        "swarm_id": episode.swarm_id,
        "pruned_sessions": pruned,
    }


__all__ = ["episode_write"]
