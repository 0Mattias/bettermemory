"""What is in force at delivery (unit RS-F7 part 3): the one decision the
session brief and the prompt recall make about a record's links before
they show it.

A record that an admitted active record ``supersedes`` is history: the
link says the source replaces it (`models.LinkType`), so a surface that
shows it shows a value the store itself says is no longer current. In the
RS read (2026-10-21) superseded ids were 9 of 48 at a recall show (rs-v5)
and 52 of 397 at a brief show (rs-v3), and history ids were graded
relevant 5 of 19 times against 44 of 104 for the rest. Its slot goes to
its head: the newest admitted record superseding it, followed until a
record nothing admitted supersedes (``replaces <id>`` on the line).

A record that an admitted record ``extends`` stays in force, and its line
names the newest in-force record extending it, so the newer detail is
named wherever the older one is shown.

A standing record held out of force (ENG-200, `standing.held_ids`: it
came back into force and no standing roster has read it since) is not
delivered: its own slot is held (``held``), it takes a record's slot as
its head only when no record that is not held supersedes that record,
and then that slot is held too, and it is never named as an extender.

Admission is the surface's own (`admission.Admission`): a link from a
record the caller may not be shown changes nothing, so no surface names
an id it could not deliver. Search is unchanged: it renders
``superseded_by`` on the hit and leaves the choice to the reader.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import LinkType

if TYPE_CHECKING:
    from .admission import RecordView
    from .store import Store

__all__ = ["InForce", "in_force"]

_WALKED = (LinkType.SUPERSEDES.value, LinkType.EXTENDS.value)


@dataclass(frozen=True)
class InForce:
    """One asked id as a surface delivers it: `id` is the record in force
    that takes its slot (the asked id itself, or its head when it is
    history), `replaces` the asked id when the head took the slot,
    `extended_by` the newest in-force record extending `id`, and `held`
    whether `id` is held out of force (ENG-200), so the slot delivers
    nothing."""

    asked: str
    id: str
    replaces: str | None = None
    extended_by: str | None = None
    held: bool = False

    @property
    def history(self) -> bool:
        return self.replaces is not None


def in_force(
    store: Store, ids: Sequence[str], admission: RecordView
) -> dict[str, InForce]:
    """`InForce` for each of `ids`, keyed by the asked id.

    Walks the inbound ``supersedes`` and ``extends`` links from the asked
    ids through every admitted source, one read per step
    (`Store.inbound_links`). Of several admitted records superseding one,
    the newest (``updated``, then id) that is not held is followed, else
    the newest held one; a cycle ends the walk at the record where it
    closes."""
    inbound: dict[str, list[tuple[str, str, str]]] = {}
    updated: dict[str, str] = {}
    frontier = list(dict.fromkeys(ids))
    while frontier:
        found = store.inbound_links(frontier, _WALKED)
        sources: list[str] = []
        for target in frontier:
            rows = found.get(target, [])
            inbound[target] = rows
            for _type, source, stamp in rows:
                updated[source] = stamp
                sources.append(source)
        admission.prime(sources)
        frontier = [
            source
            for source in dict.fromkeys(sources)
            if source not in inbound and admission.admits_id(source)
        ]

    # Imported here: `standing` imports this module.
    from .standing import held_ids

    held = held_ids(store, [*ids, *updated]) if ids else set()

    def newest(candidates: Iterable[str]) -> str | None:
        ranked = sorted(set(candidates), key=lambda rid: (updated.get(rid, ""), rid))
        return ranked[-1] if ranked else None

    def newest_head(candidates: Iterable[str]) -> str | None:
        """The newest of `candidates` not held, else the newest held."""
        pool = set(candidates)
        return newest(pool - held) or newest(pool)

    def admitted_sources(record_id: str, link_type: str) -> list[str]:
        return [
            source
            for kind, source, _stamp in inbound.get(record_id, [])
            if kind == link_type and admission.admits_id(source)
        ]

    def head(record_id: str) -> str:
        seen = {record_id}
        current = record_id
        while True:
            nxt = newest_head(admitted_sources(current, LinkType.SUPERSEDES.value))
            if nxt is None or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt

    out: dict[str, InForce] = {}
    for asked in dict.fromkeys(ids):
        target = head(asked)
        extenders = {
            head(source) for source in admitted_sources(target, LinkType.EXTENDS.value)
        } - {target}
        out[asked] = InForce(
            asked=asked,
            id=target,
            replaces=asked if target != asked else None,
            extended_by=newest(extenders - held),
            held=target in held,
        )
    return out
