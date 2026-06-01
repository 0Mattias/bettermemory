"""memory_show MCP tool — handler implementation + DESC.

The handler is a focused single-id read: load the memory, surface every
drift signal we can in one call (so the caller doesn't bounce back for
a second tool round-trip), issue a use-token so the auto-`record_use`
flow has something to commit on the next turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..models import utcnow
from ..store import MemoryNotFoundError, TombstonedError
from ..verify import (
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_SHOW = (
    "Fetch a single memory's full content by id. Typically used "
    "after a memory_search snippet looks relevant. The response "
    "carries the same staleness signals as a search hit:\n"
    "- `verification.status` ('never' / 'stale' / 'fresh') with an "
    "actionable `recommendation` when not fresh.\n"
    "- `staleness_verdict` (fresh / spot_check_recommended / "
    "spot_check_required) — rolled-up signal across calendar, path "
    "and commit drift.\n"
    "- `path_drift` (the full report; missing-on-disk paths "
    "listed).\n"
    "- `commit_drift` (when caller is inside the memory's origin "
    "repo) — `status: 'clean' | 'drift'` + `commits_since_verify`.\n"
    "- Forward `links` and `reverse_links` for navigation.\n\n"
    "When the verdict isn't fresh, spot-check one claim before "
    "relying. memory_verify(id, …) if it holds; memory_update if "
    "drifted (content updates reset last_verified_at, so verify "
    "again after the fix)."
)


async def memory_show(
    deps: "ToolHandlers", id: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Body of the ``memory_show`` MCP tool."""
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    try:
        memory = deps.store.load_one(id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    # Path-drift runs against the full body. We surface `path_drift`
    # only when there's something actionable: a memory with no path
    # claims (or all-healthy paths) returns the field as null so the
    # consumer can branch on `if path_drift is not None`. Without
    # that, every memory_show would carry an empty `path_drift` dict
    # and the model would learn to ignore the field even when it
    # mattered. `verified_paths` is threaded in so a path the user
    # has previously attested gets surfaced in `path_drift.verified`
    # even when no other claims drift.
    drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
    # Verification staleness is structurally always present — emitted
    # even for "fresh" memories — because consistent shape means the
    # consumer can branch on `verification.status` without an
    # existence check. The recommendation field is null on fresh,
    # populated otherwise; that's the actionable handle.
    verification = compute_verification_status(
        memory.last_verified_at,
        now=utcnow(),
        stale_after_days=deps.config.behavior.verification_stale_days,
    )
    # Commit-drift is the cwd-aware sibling of verification: when the
    # caller is currently inside a checkout of the same repo the
    # memory came from, count commits authored since the last verify.
    # Stays null when the caller isn't in the matching repo or the
    # memory has no anchor to count from — emitting an "unknown"
    # branch every consumer would have to filter is worse than
    # silence, mirroring path_drift's null-when-clean contract.
    # Verified paths narrow the count to commits that touched at
    # least one of those paths — a memory verified for `[/etc/foo]`
    # reads as `clean` when the project moved but `/etc/foo`
    # didn't.
    commit_drift = compute_commit_drift(
        memory.last_verified_at,
        memory.origin.repo if memory.origin else None,
        caller_origin=_h.capture_origin(),
        verified_paths=memory.verified_paths,
    )
    commit_drift_count_for_verdict: int | None = (
        commit_drift.commits_since_verify if commit_drift is not None else None
    )
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.missing),
        commit_drift_count=commit_drift_count_for_verdict,
    )
    # Issue a use-token for this show before returning so the
    # auto-`record_use` flow has something to commit on the next
    # turn if the model doesn't override.
    token_map = state.issue_use_tokens([memory.id])
    deps.recorder.record(
        "show",
        id=memory.id,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        verification_status=verification.status,
        staleness_verdict=verdict,
        commit_drift_status=(commit_drift.status if commit_drift is not None else None),
        commits_since_verify=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )
    return {
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "category": (memory.category.value if memory.category is not None else None),
        "created": isoformat(memory.created),
        "updated": isoformat(memory.updated),
        "last_verified_at": isoformat_optional(memory.last_verified_at),
        "verification": verification.to_dict(),
        "staleness_verdict": verdict,
        "body": memory.body,
        "origin": deps.responses.origin_to_dict(memory.origin),
        "path_drift": (
            drift.to_dict() if (drift.has_drift or drift.verified) else None
        ),
        "commit_drift": (commit_drift.to_dict() if commit_drift is not None else None),
        "use_token": token_map[memory.id],
        "verified_paths": list(memory.verified_paths),
        "verified_commits": list(memory.verified_commits),
        "verified_versions": list(memory.verified_versions),
        **_links_payload(deps, memory),
    }


def _links_payload(deps: "ToolHandlers", memory: Any) -> dict[str, Any]:
    """Build the `links` + `reverse_links` payload for memory_show.

    Forward `links` come from the memory's own frontmatter. Reverse
    `reverse_links` are computed by querying the FTS5 index's
    `memory_links` table — every memory that links AT this id is
    a row keyed on `target_id`, so the lookup is O(k) on the
    number of reverse links rather than O(N) on the store size.
    Surfaced so a retrieval consumer sees the relationship both
    ways (e.g. "this memory is superseded by X" alongside "X
    supersedes this").

    Both lists are omitted when empty (absence-as-signal contract,
    matches `path_drift` / `commit_drift`). Reverse links carry the
    source `memory_id` so the consumer can navigate to the linking
    memory; forward links carry the `target_id`.

    Fallback: if the index file doesn't exist (fresh install, just
    deleted) OR exists but reports zero indexed rows,
    `links_for_with_status` returns empty lists with
    `indexed_count == 0` and we walk the active set once. That matches
    the same fallback shape `_load_search_candidates` uses — search
    keeps working through a torn-down index, just slower.

    The present-but-empty case is the post-upgrade window: a
    `SCHEMA_VERSION` bump makes `_ensure_schema` drop+recreate the
    index tables EMPTY on the first index op after the upgrade (the
    index FILE still exists, `indexed_count` resets to 0) and the
    rows refill lazily per-write or via `bettermemory reindex`. An
    `exists()`-only guard would let the lookup return `[]` and emit
    NO reverse_links for affected memories through that window, even
    though the linking data is intact on disk. Routing the
    zero-row case to `load_all` too keeps reverse_links correct
    while the index repopulates.

    `links_for_with_status` returns the inbound links AND
    `indexed_count` from a SINGLE index open — the empty-index signal
    rides the connection the inbound query already holds. That keeps
    the common populated-but-no-inbound `memory_show` (most memories
    are not link targets) to ONE index open: a non-zero count proves
    the index is usable, so we return empty reverse_links with no
    second connection. Only a zero count (index absent, empty, or
    corrupt — every state where the index can't answer) triggers the
    `load_all` scan.
    """
    from .. import index as _index

    out: dict[str, Any] = {}
    if memory.links:
        out["links"] = [
            {
                "type": link.type.value,
                "target_id": link.target_id,
                **({"note": link.note} if link.note is not None else {}),
            }
            for link in memory.links
        ]
    outbound, inbound, indexed_count = _index.links_for_with_status(
        deps.store.root, memory.id
    )
    reverse: list[dict[str, Any]] = []
    if inbound:
        for ltype, source_id, note in inbound:
            if source_id == memory.id:
                # Defensive: self-links shouldn't appear as reverse
                # since they're already in `links`. Skip to keep
                # the surface stable across index drift.
                continue
            entry: dict[str, Any] = {"type": ltype, "source_id": source_id}
            if note is not None:
                entry["note"] = note
            reverse.append(entry)
    elif indexed_count == 0:
        # No usable index — fall back to the old shape so a freshly-
        # initialised store (file absent) AND a post-upgrade store
        # (file present but tables dropped empty by the SCHEMA_VERSION
        # rebuild) still get reverse links. `links_for_with_status`
        # reports `indexed_count == 0` for the absent, empty, and
        # corrupt cases alike — read on the SAME connection it already
        # opened for the inbound query, so the common populated-but-no-
        # inbound case stays a single index open (no second `status()`
        # connection). Any zero count means the index can't answer, so
        # walk the active set. After the next write / reindex the index
        # repopulates and this branch stops firing.
        for other in deps.store.load_all():
            if other.id == memory.id:
                continue
            for link in other.links:
                if link.target_id == memory.id:
                    entry = {
                        "type": link.type.value,
                        "source_id": other.id,
                    }
                    if link.note is not None:
                        entry["note"] = link.note
                    reverse.append(entry)
    if reverse:
        out["reverse_links"] = reverse
    return out


__all__ = ["DESC_MEMORY_SHOW", "memory_show"]
