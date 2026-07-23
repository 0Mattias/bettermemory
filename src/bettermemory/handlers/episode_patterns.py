"""episode_patterns MCP tool — handler implementation + DESC.

The cross-session half of consolidation. `episode_promote` distills ONE
journal entry; this tool surfaces themes recurring across MANY sessions'
episodes (mechanical detection — see `patterns.py`) and lets the model
either promote a pattern into a durable memory or dismiss it.

Promotion deliberately requires the model to AUTHOR the body: the
server can prove that four sessions kept circling the same terms, but
only the model can write the sentence that is true across all four.
The write routes through the full `memory_write` gate stack (dedup,
durability, scope-mismatch, user-inference confirmation), so a pattern
whose fact is already stored dedup-rejects — and that rejection records
a corroboration on the existing memory, which is the recurrence signal
landing where it belongs.

On a committed promote the member episodes are deleted (their content
is distilled — same lifecycle as `episode_promote`). On `pending`
(user-inference) the episodes are LEFT IN PLACE: the multi-episode
confirm-time cleanup isn't wired, and the worst case — journal entries
surviving until their ~30-day TTL — is the pre-existing behavior for
every unpromoted episode. On any other non-committed status the
episodes are untouched so the caller can adjust and retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import Episode
from ..patterns import PatternDismissals, find_episode_patterns
from . import write as _write_mod
from ._shared import Context, _advance_turn
from .episode_promote import _delete_source_episode

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_EPISODE_PATTERNS = (
    "Surface themes recurring across MULTIPLE sessions' episodes — the "
    "consolidation no single session can see (four different days each "
    "journaling the same flaky proxy). The server clusters episodes "
    "sharing distinctive terms across >= 3 distinct sessions; YOU judge "
    "each candidate.\n\n"
    "Modes (mutually exclusive):\n"
    "- default (no args): list current candidates — `{id, terms, "
    "episode_ids, distinct_sessions, snippets}` per pattern, one "
    "snippet per member episode so it's judgeable in place.\n"
    "- `promote=<pattern_id>` + `body` + `scopes`: write the durable "
    "memory. AUTHOR the body yourself — state the fact that is true "
    "across the member episodes (the terms are evidence pointers, not "
    "a synthesis). Routes through the full memory_write gate stack; "
    "category/confidence/source accepted as on memory_write. On "
    "commit, member episodes are deleted (distilled). A `duplicate` "
    "rejection is still a WIN: it records a corroboration on the "
    "existing memory. On `pending` (user-inference) episodes stay "
    "until their TTL.\n"
    "- `dismiss=<pattern_id>`: not worth consolidating (incidental "
    "vocabulary overlap). Sticky for that exact episode set; a NEW "
    "episode joining the theme legitimately reopens it under a fresh "
    "id.\n\n"
    "Candidates recompute on every call (episodes churn constantly); "
    "ids are content-stable hashes of the member set, so a listed id "
    "stays valid while the members live. Detection is conservative "
    "(>= 3 episodes, >= 3 sessions, ubiquitous project vocabulary "
    "excluded) — an empty list usually just means the journal is "
    "young or already consolidated."
)


def _all_episodes(deps: "ToolHandlers") -> list[Episode]:
    out: list[Episode] = []
    for sid in deps.episode_store.iter_session_ids():
        try:
            out.extend(deps.episode_store.list_by_session(sid))
        except ValueError:
            continue
    return out


async def episode_patterns(
    deps: "ToolHandlers",
    promote: str | None = None,
    dismiss: str | None = None,
    body: str | None = None,
    scopes: list[str] | None = None,
    category: str = "fact",
    confidence: str = "medium",
    source: str = "inferred",
    min_sessions: int = 3,
    max_patterns: int = 5,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if promote is not None and dismiss is not None:
        raise ValueError("pass either promote=<id> or dismiss=<id>, not both")
    if min_sessions < 2:
        raise ValueError("min_sessions must be >= 2")
    if max_patterns < 1:
        raise ValueError("max_patterns must be a positive integer")

    # The promote path routes through memory_write, which advances the
    # turn itself — same single-advance contract episode_promote documents.
    if promote is None:
        state = deps.sessions.for_request(ctx)
        _advance_turn(state, deps.recorder)

    episodes = _all_episodes(deps)
    candidates = find_episode_patterns(
        episodes, min_sessions=min_sessions, max_patterns=max_patterns
    )
    dismissals = PatternDismissals(deps.store.root)
    live_ids = {ep.id for ep in episodes}
    dismissed = dismissals.dismissed_ids(live_ids)
    candidates = [c for c in candidates if c.id not in dismissed]

    if dismiss is not None:
        target = next((c for c in candidates if c.id == dismiss), None)
        if target is None:
            raise ValueError(
                f"no live pattern candidate with id {dismiss!r} — list first; "
                "candidates recompute as episodes churn"
            )
        dismissals.dismiss(target.id, target.episode_ids)
        deps.recorder.record(
            "episode_pattern",
            action="dismissed",
            pattern=target.id,
            episodes=len(target.episode_ids),
        )
        return {"dismissed": target.id, "remaining": len(candidates) - 1}

    if promote is not None:
        target = next((c for c in candidates if c.id == promote), None)
        if target is None:
            raise ValueError(
                f"no live pattern candidate with id {promote!r} — list first; "
                "candidates recompute as episodes churn"
            )
        if not body or not body.strip():
            raise ValueError(
                "promote requires `body`: author the durable fact that is "
                "true across the member episodes (see their snippets)"
            )
        if not scopes:
            raise ValueError("promote requires `scopes` for the durable memory")

        response = await _write_mod.memory_write(
            deps,
            content=body,
            scopes=scopes,
            confidence=confidence,
            source=source,
            category=category,
            ctx=ctx,
        )
        response["promoted_from_pattern"] = target.id
        response["pattern_episode_ids"] = target.episode_ids
        if response.get("status") == "committed":
            deleted = 0
            by_id = {ep.id: ep for ep in episodes}
            for eid in target.episode_ids:
                ep = by_id.get(eid)
                if ep is None:
                    continue
                try:
                    _delete_source_episode(deps, ep.session_id, eid)
                    deleted += 1
                except OSError:  # pragma: no cover - crash-safe best effort
                    continue
            response["episodes_deleted"] = deleted
            deps.recorder.record(
                "episode_pattern",
                action="promoted",
                pattern=target.id,
                memory_id=response.get("id"),
                episodes_deleted=deleted,
            )
        return response

    return {
        "patterns": [c.to_dict() for c in candidates],
        "episodes_scanned": len(episodes),
        **(
            {}
            if candidates
            else {
                "hint": (
                    "No recurring cross-session themes right now. Detection "
                    "needs >= 3 episodes across >= 3 distinct sessions "
                    "sharing distinctive terms — a young or freshly-"
                    "consolidated journal legitimately lists none."
                )
            }
        ),
    }
