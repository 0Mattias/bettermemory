"""The `depends_on` auto-pull steps of `ResponseBuilder.attach_depends_on_resolved`.

`_load_depends_targets` is the targeted-load fallback: it loads, through
the store, the `depends_on` targets the search's candidates lack and adds
the ones the caller admits to the side-map. `_resolve_depends` builds one
hit's `depends_on_resolved` entries under the per-hit and per-result caps.
The caps, the filters and the absence-as-signal contract are set out on
`attach_depends_on_resolved`.

These steps live apart from `_response.py` to keep it under the package's
1,900-line watch line (ENG-97). `_response` imports this module and this
module never imports it: it imports `models` and `store` inside the steps
that read them, as the method did, and the rest only for type hints.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .admission import Admission
    from .models import Memory, MemoryHit, MemoryLink


def _load_depends_targets(
    hits: Sequence[MemoryHit],
    memory_by_id: dict[str, Memory],
    store: Any,
    *,
    max_per_hit: int,
    max_total: int,
    admission: Admission | None,
    excluded: set[str],
) -> dict[str, Memory]:
    """Take the side-map; return it with the admitted depends_on targets it lacked."""
    import sqlite3

    from .store import MemoryNotFoundError, TombstonedError

    if max_per_hit <= 0 or max_total <= 0:
        return memory_by_id
    tried: set[str] = set()
    while len(tried) < max_total:
        batch = _pending_depends_targets(
            hits,
            memory_by_id,
            tried,
            max_per_hit=max_per_hit,
            max_total=max_total,
            load_limit=max_total - len(tried),
            admission=admission,
            excluded=excluded,
        )
        if not batch:
            break
        tried.update(batch)
        if admission is not None:
            admission.prime(batch)
        for tid in batch:
            try:
                target = store.load_one(tid)
            except (MemoryNotFoundError, TombstonedError, OSError, sqlite3.Error):
                # Absent, tombstoned and unreadable targets are best effort;
                # their links remain available for explicit memory_show reads.
                continue
            # A dependency edge grants no admission or disabled-scope bypass.
            if excluded and (set(target.scopes) & excluded):
                continue
            if admission is not None and not admission.admits_id(target.id):
                continue
            memory_by_id[tid] = target
    return memory_by_id


def _pending_depends_targets(
    hits: Sequence[MemoryHit],
    memory_by_id: dict[str, Memory],
    tried: set[str],
    *,
    max_per_hit: int,
    max_total: int,
    load_limit: int,
    admission: Admission | None,
    excluded: set[str],
) -> list[str]:
    """Reserve display slots in hit/link order for one bounded load round.

    Each hit reserves at most its display cap before later hits get a turn.
    A failed load frees its reservation for the next round; no target is
    attempted twice, and every round shares the original hard load limit.
    """
    batch: list[str] = []
    pending: set[str] = set()
    total = 0
    for hit in hits:
        source = memory_by_id.get(hit.id)
        if source is None:
            continue
        used = 0
        for link in source.links:
            if used >= max_per_hit or total >= max_total:
                break
            if link.type.value != "depends_on":
                continue
            tid = link.target_id
            target = memory_by_id.get(tid)
            if target is not None:
                if excluded and (set(target.scopes) & excluded):
                    continue
                if admission is not None and not admission.admits_id(tid):
                    continue
            else:
                if tid in tried:
                    continue
                if tid not in pending:
                    if len(batch) >= load_limit:
                        continue
                    batch.append(tid)
                    pending.add(tid)
            used += 1
            total += 1
    return batch


def _resolve_depends(
    depends_links: list[MemoryLink],
    memory_by_id: dict[str, Memory],
    total: int,
    *,
    max_per_hit: int,
    max_total: int,
    admission: Admission | None,
    excluded: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Take one hit's depends_on links; return their entries and the running total."""
    from .models import first_summary_line

    resolved: list[dict[str, Any]] = []
    for link in depends_links:
        if len(resolved) >= max_per_hit or total >= max_total:
            break
        target = memory_by_id.get(link.target_id)
        if target is None:
            # Not in the loaded candidate set (the target may be
            # outside the search's auto-scope, just not in the
            # FTS5 prefilter, or — when `store` was supplied —
            # dropped by the targeted-load filter in `_load_depends_targets` as
            # tombstoned / scope-excluded / cross-project).
            # Skip silently — the link still exists on disk
            # and `memory_show` will surface it; auto-pull is
            # a best-effort surface.
            continue
        # Re-apply the caller's scope filter to the resolved
        # target. The side-map is built from the pre-filter
        # loader output (so we can find targets by id at all),
        # but a depended-on memory in a session-disabled scope
        # or a different project under auto_scope must not
        # leak in via the dependency edge.
        if excluded and (set(target.scopes) & excluded):
            continue
        if admission is not None and not admission.admits_id(target.id):
            continue
        resolved.append(
            {
                "id": target.id,
                "scopes": list(target.scopes),
                "summary": first_summary_line(target.body),
                "link_note": link.note,
            }
        )
        total += 1
    return resolved, total
