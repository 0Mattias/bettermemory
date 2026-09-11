"""`bettermemory rollback` — targeted, reversible removal by actor.

The identity resolver's third and last consumer. 7.10.0 stamped an
`actor` on every record, 7.12.0 let search and `memory_list` filter on
it, 7.14.0 pivoted the store by it in `memory_health`. This removes by
it: one agent's contributions leave the active set, everyone else's
stay, and `tombstones restore` puts any of them back.

**Why this is not `consolidate --by-actor`.** `consolidate` runs its
four structural passes unconditionally before any mode flag fires, and
its mode vocabulary is documented as extending those passes — so a
destructive rollback riding along would mean "roll back this one agent"
also committed whole-store dedup tombstones and category demotions.
Beyond that mechanical trap, every other flag on `consolidate` is a
pass-tuning parameter, so `--by-actor` sitting among them reads as a
scope filter ("run the passes over only this actor's memories") rather
than as a removal — a plausible, wrong and destructive misreading that
no help text fully repairs. And nothing here is consolidation: nothing
is merged, deduplicated, or judged. It is operator-specified bulk
removal with an undo, so it gets its own command.

**What "contribution" means here, precisely.** `actor` is stamped at
write time and is never restamped: `Store.update` takes no actor. So
this selects on AUTHORSHIP OF THE ORIGINAL WRITE — a record this actor
wrote and another client later rewrote IS selected; one another client
wrote and this actor extensively edited is NOT. That is what the stamp
means, but it is not what "roll back an agent's contributions" sounds
like, so both the help text and the rendered header say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .identity import actor_matches
from .models import Memory, snippet_for

if TYPE_CHECKING:
    from .store import Store


@dataclass
class RollbackCandidate:
    """One record the rollback would remove."""

    memory_id: str
    created: str
    scopes: list[str]
    model: str | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "created": self.created,
            "scopes": self.scopes,
            "model": self.model,
            "summary": self.summary,
        }


@dataclass
class RollbackAction:
    """A removal actually committed — only populated when the apply
    path ran and was authorised."""

    kind: str  # always "tombstoned"
    memory_id: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "memory_id": self.memory_id,
            "detail": self.detail,
        }


@dataclass
class RollbackFailure:
    """A removal the apply pass attempted but could not complete.
    Aggregated so a run that hits ten I/O errors surfaces as one
    rollup rather than ten stray warnings."""

    memory_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"memory_id": self.memory_id, "reason": self.reason}


@dataclass
class RollbackReport:
    """What a rollback selected, what it declined, and why.

    The four population counts PARTITION the active set exactly:

        len(candidates) + declined_undeclared
            + other_actor + out_of_window == total_active

    That is an asserted invariant (`assert_reconciles`), not a comment.
    It is affordable here for the same reason 7.14.0's actor slices
    could promise one: a memory carries exactly one actor, so the
    buckets cannot double-count the way a scope pivot does.

    `declined_undeclared` is the load-bearing one. `actor_matches`
    excludes a record with no actor by design — selection is not
    admission — and on a real store that is the overwhelming majority
    (378 of 417 active records when this shipped). A rollback that
    printed a small selection and said nothing about the other 90% would
    let an operator infer the store is mostly one actor's. So the count
    is reported rather than silently omitted, the same rule 7.14.0
    applied to its `undeclared` bucket.
    """

    client: str
    since: str | None
    candidates: list[RollbackCandidate] = field(default_factory=list)
    total_active: int = 0
    declined_undeclared: int = 0
    other_actor: int = 0
    out_of_window: int = 0
    applied: bool = False
    committed: bool = False
    refusal_reason: str | None = None
    actions_taken: list[RollbackAction] = field(default_factory=list)
    failures: list[RollbackFailure] = field(default_factory=list)

    def assert_reconciles(self) -> None:
        """Raise if the four populations do not sum to the active set.

        Called on every constructed report. A census that cannot
        reconcile against its own total is not evidence of anything,
        and the cheapest moment to find that out is here.
        """
        total = (
            len(self.candidates)
            + self.declined_undeclared
            + self.other_actor
            + self.out_of_window
        )
        if total != self.total_active:
            raise AssertionError(
                f"rollback report does not reconcile: selected "
                f"{len(self.candidates)} + undeclared {self.declined_undeclared} "
                f"+ other-actor {self.other_actor} + out-of-window "
                f"{self.out_of_window} = {total}, but the active set holds "
                f"{self.total_active}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "since": self.since,
            "applied": self.applied,
            "committed": self.committed,
            "refusal_reason": self.refusal_reason,
            "total_active": self.total_active,
            "selected": len(self.candidates),
            "declined_undeclared": self.declined_undeclared,
            "other_actor": self.other_actor,
            "out_of_window": self.out_of_window,
            "candidates": [c.to_dict() for c in self.candidates],
            "actions_taken": [a.to_dict() for a in self.actions_taken],
            "failures": [f.to_dict() for f in self.failures],
        }


def tombstone_reason(client: str, since: str | None) -> str:
    """The removal reason a rolled-back record carries.

    Names both the actor and the window, so the rollback is auditable
    from `tombstones list` alone without joining the event log — the
    same house shape `consolidate`'s reasons use (`"<command> <mode>:
    <why>"`). The window is spelled out even when absent, because
    "all time" and "a window the reason forgot to record" must not read
    the same.
    """
    window = f"created since {since}" if since else "all time, no --since window"
    return f"rollback --by-actor: written by client={client} ({window})"


def plan_rollback(
    memories: list[Memory],
    *,
    client: str,
    since: datetime | None = None,
) -> RollbackReport:
    """Select the records `client` wrote, and account for every one it
    did not.

    Pure over the memory list: no store, no index, no events. The actor
    is read off the loaded record rather than off the v11 index columns,
    which serve search's prefilter — the same call 7.14.0 made, and for
    the same reason (a report that promises a reconciliation invariant
    cannot draw its halves from two sources that can legitimately
    disagree, and this store has been observed one row out of sync with
    its own index).

    `since` filters `created`, never `updated`, because `actor` and
    `created` are stamped by the same write event. Filtering on
    `updated` would pair a write-time actor with an edit-time
    timestamp.
    """
    candidates: list[RollbackCandidate] = []
    declined_undeclared = 0
    other_actor = 0
    out_of_window = 0

    for memory in memories:
        actor = memory.actor
        # `actor_matches` is the single definition of the actor-
        # selection rule (it is also what search and `memory_list`
        # read); respelling it here is how two surfaces drift into
        # disagreeing about who wrote what. It excludes a record with
        # no actor, which is the case counted separately below.
        if actor is None or actor.client is None:
            declined_undeclared += 1
            continue
        if not actor_matches(actor, client=client, model=None):
            other_actor += 1
            continue
        if since is not None and memory.created < since:
            out_of_window += 1
            continue
        candidates.append(
            RollbackCandidate(
                memory_id=memory.id,
                created=memory.created.isoformat().replace("+00:00", "Z"),
                scopes=list(memory.scopes),
                model=actor.model,
                summary=snippet_for(memory.body, max_chars=100),
            )
        )

    candidates.sort(key=lambda c: (c.created, c.memory_id))
    report = RollbackReport(
        client=client,
        since=since.isoformat().replace("+00:00", "Z") if since else None,
        candidates=candidates,
        total_active=len(memories),
        declined_undeclared=declined_undeclared,
        other_actor=other_actor,
        out_of_window=out_of_window,
    )
    report.assert_reconciles()
    return report


def apply_rollback(
    store: "Store",
    report: RollbackReport,
    *,
    session_id: str | None = None,
) -> RollbackReport:
    """Commit the planned removals as tombstones.

    Mutates and returns the same report. No event is recorded: a
    tombstone leaves the active set, and `Store.tombstone` already
    captures the reason and the session on the tombstone frontmatter,
    which is what `tombstones list` reads. That mirrors `consolidate`'s
    posture exactly — its dedup pass records `consolidate_update` for
    the keeper it rewrites and records nothing for the record it
    removes.

    One failure never aborts the pass; each is collected so a partial
    run reports precisely which records did not move.
    """
    for candidate in report.candidates:
        reason = tombstone_reason(report.client, report.since)
        try:
            store.tombstone(
                candidate.memory_id,
                reason=reason,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — never break the pass
            report.failures.append(
                RollbackFailure(memory_id=candidate.memory_id, reason=str(exc))
            )
            continue
        report.actions_taken.append(
            RollbackAction(
                kind="tombstoned",
                memory_id=candidate.memory_id,
                detail=reason,
            )
        )
    report.committed = True
    return report


def render_text(report: RollbackReport) -> str:
    """Human-readable rollback report. Pairs with `render_json`."""
    lines: list[str] = []
    title = f"Rollback report — client={report.client}"
    if report.committed:
        title += " (APPLIED)"
    elif report.applied:
        title += " (refused)"
    else:
        title += " (dry-run)"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    window = report.since or "all time (no --since window)"
    lines.append(f"Window: {window}")
    # State the sense of "contribution" in the output itself, not only
    # in --help. An operator reading a rollback report is exactly the
    # person who must not believe they rolled back an agent's INFLUENCE
    # when they rolled back its AUTHORSHIP.
    lines.append(
        "Selects records this client WROTE. The actor is stamped at "
        "write time and never restamped, so a record it wrote and "
        "another client later edited is included, and a record it only "
        "edited is not."
    )
    lines.append("")

    lines.append(f"Selected for removal ({len(report.candidates)})")
    if report.candidates:
        for c in report.candidates:
            model = f", model={c.model}" if c.model else ""
            lines.append(f"  {c.memory_id}  ({c.created}{model})")
            lines.append(f"    scopes: {', '.join(c.scopes)}")
            lines.append(f"    {c.summary}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Not considered ({report.total_active} active records in total)")
    lines.append(
        f"  {report.declined_undeclared} declared no client at all "
        f"— excluded, not assumed to be someone else's"
    )
    lines.append(f"  {report.other_actor} written by another client")
    lines.append(f"  {report.out_of_window} written by this client before the window")
    lines.append("")

    if report.failures:
        lines.append(f"Failures ({len(report.failures)})")
        for f in report.failures:
            lines.append(f"  {f.memory_id}: {f.reason}")
        lines.append("")

    if report.committed:
        lines.append(
            f"Removed {len(report.actions_taken)} record(s). Restore any of "
            f"them with `bettermemory tombstones restore <ID>`."
        )
    elif report.refusal_reason:
        lines.append(f"NOTHING WAS REMOVED: {report.refusal_reason}")
    else:
        lines.append(
            "Dry run — nothing was removed. Re-run with --apply --yes to commit."
        )
    return "\n".join(lines) + "\n"


def render_json(report: RollbackReport) -> str:
    import json

    return json.dumps(report.to_dict(), separators=(",", ":")) + "\n"
