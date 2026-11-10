"""Resolve a search's commit annotations without changing its output rank."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .git_read_budget import for_resume

if TYPE_CHECKING:
    from .commit_legs import CommitLegs
    from .models import MemoryHit


def count_hits(
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    memories: Sequence[Any],
    drift: CommitLegs,
) -> None:
    """Take the search's rows and its commit legs; stamp each hit's leg."""
    # Build the id → memory side-map from the in-memory `memories`
    # list (already loaded by the caller for the search itself),
    # avoiding a second store round-trip per hit. The full record is
    # needed (not just origin/verified_paths): the claim-anchored
    # narrowing derives anchors from the BODY's citations too,
    # matching what memory_show does. Without surface parity here,
    # the loud search surface would nag spot_check_recommended on a
    # memory memory_show reads as fresh — defeating the policy on
    # its highest-traffic surface.
    from ._response import _stamp_hit_drift

    memory_by_id = {m.id: m for m in memories}
    pending = [
        (row, hit, record)
        for row, hit in zip(out, hits)
        if hit.last_verified_at is not None
        and (record := memory_by_id.get(hit.id)) is not None
    ]
    for hit_dict, hit, record in for_resume(
        pending, lambda entry: drift.read_root(entry[2])
    ):
        if hit.last_verified_at is None:
            continue
        # The gates, the key, the memo and the resolution are the
        # passes' (`_DriftPass.lookup`, through `commit_legs.
        # CommitLegs`), shared with `memory_show` through
        # `verdict.resolve_verdicts`.
        _, resolved = drift.lookup(record, hit.last_verified_at)
        if resolved is None:
            # Not applicable: no repository, no anchor, or the anchors
            # all escape the repository or are phantoms. Omit the
            # field entirely.
            continue
        _stamp_hit_drift(hit_dict, hit, resolved)
