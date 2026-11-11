"""Recover link rows from an already loaded candidate snapshot."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from .models import Memory

Edge: TypeAlias = tuple[str, str, str | None]
LinkRows: TypeAlias = dict[str, tuple[list[Edge], list[Edge]]]


def candidate_link_rows(memories: Iterable[Memory], ids: Iterable[str]) -> LinkRows:
    """The store's row shape from candidates alone, without another store read.

    Collapse exact duplicates as ``store_rows._sync_links`` does, retaining
    distinct notes. Type/id order matches the bulk query's ordering; ties
    between notes retain their order in the candidate snapshot.
    """
    out: LinkRows = {mid: ([], []) for mid in ids}
    seen: set[tuple[str, str, str, str | None]] = set()
    for memory in memories:
        for link in memory.links:
            target = link.target_id
            if memory.id not in out and target not in out:
                continue
            kind = link.type.value
            key = (memory.id, kind, target, link.note)
            if key in seen:
                continue
            seen.add(key)
            if memory.id in out:
                out[memory.id][0].append((kind, target, link.note))
            if target in out:
                out[target][1].append((kind, memory.id, link.note))
    for outbound, inbound in out.values():
        outbound.sort(key=lambda row: (row[0], row[1]))
        inbound.sort(key=lambda row: (row[0], row[1]))
    return out
