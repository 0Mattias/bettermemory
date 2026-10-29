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
    max_total: int,
    admission: Admission | None,
    excluded: set[str],
) -> dict[str, Memory]:
    """Take the side-map; return it with the admitted depends_on targets it lacked."""
    from .store import MemoryNotFoundError, TombstonedError

    missing_target_ids: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        source = memory_by_id.get(hit.id)
        if source is None or not source.links:
            continue
        for link in source.links:
            if link.type.value != "depends_on":
                continue
            tid = link.target_id
            if tid in memory_by_id or tid in seen:
                continue
            seen.add(tid)
            missing_target_ids.append(tid)
            if len(missing_target_ids) >= max_total:
                break
        if len(missing_target_ids) >= max_total:
            break
    if admission is not None:
        admission.prime(missing_target_ids)
    for tid in missing_target_ids:
        try:
            target = store.load_one(tid)
        except (MemoryNotFoundError, TombstonedError, OSError):
            # Same silent-skip semantics as the prefilter-miss
            # branch in `_resolve_depends`: a deleted or
            # tombstoned target is best-effort dropped from
            # auto-pull. The link still exists in the store for
            # explicit `memory_show` investigation.
            #
            # OSError is in the set because 8.x's `store.load_one`
            # read the target's backing file (`frontmatter.load`)
            # without guarding OSError; a transient read failure
            # (EIO, a flaky network mount) on a depends-on target
            # would otherwise have propagated out of
            # `attach_depends_on_resolved` and aborted an
            # otherwise-successful `memory_search`. The store reads a
            # row; the guard stays, dropping an unreadable target from
            # the auto-pull like a missing or tombstoned one, and
            # matching the OSError guard on the `expand_top` body load
            # in `handlers/search.py`.
            continue
        # Apply the caller's scope/origin filter at load time
        # — must mirror the per-target check in `_resolve_depends` so a
        # targeted load can't sneak a session-disabled or
        # cross-project target into the side-map. Without
        # this, the targeted-load path would reintroduce the
        # exact scope leak the per-target check (bf92912)
        # closed for the prefilter path.
        if excluded and (set(target.scopes) & excluded):
            continue
        if admission is not None and not admission.admits_id(target.id):
            continue
        memory_by_id[tid] = target
    return memory_by_id


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
