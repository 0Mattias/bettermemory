"""`bettermemory capture review` and `capture undo`: what each capture
batch did, and taking one back.

Read from the batch rows (`capture`) and, for refusals, from the write
events that carry the batch. An undo follows `rollback`'s plan, apply and
render shape and, like it, TOMBSTONES: `tombstones restore` puts a record
back, and the dedup-tombstone gate covers a body the owner undid. The
`capture_undo` row commits in the same transaction as the tombstones, so
a later capture of an undone body is refused as previously undone.

A member someone has since relied on is not undone silently: one later
updated, verified, linked to by a memory outside the batch, or filed in a
conflict its own write did not file is refused, and with `--skip-changed`
left in place and reported. The check runs again inside the transaction
that applies the undo, and the conflict rows the members' own writes
filed are deleted in it.
Members come from the membership rows, so an interrupted batch, which has
no settled close, is undoable too.

`capture sample` draws live members for the owner to grade against the
bar, and `capture grade` records each grade as a `capture_grade` event,
which `grade_tally` counts per ISO week.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import capture as cap
from .log import CAPTURE_UNDO
from .models import Memory, utcnow
from .standing import held_by_removal, held_lines
from .store import NotFoundError, Store, TombstonedError
from .time_utils import isoformat_utc, parse_event_ts

# Why a member is not undone, by the name the report carries.
CHANGED_UPDATED = "updated"
CHANGED_VERIFIED = "verified"
CHANGED_LINKED = "linked"
CHANGED_CONFLICT = "conflict"

# The write path files a member's own conflict pairs just after its commit
# (`handlers.write._file_conflicts`, `_file_review_pairs`), the member as
# `a_id`. A pending row filed this soon is the capture's own, not a sign
# anyone relied on the member. The filings run in the same request right
# after the commit, with nothing between them but the commit and, for the
# review pairs, one read of the store's memories, so their rows land well
# inside the minute. A row a later write files names that write as `a_id`,
# and one filed for the member past the minute reads as reliance on it.
OWN_FILING_WINDOW = timedelta(minutes=1)


def refusal_rules(event: Mapping[str, Any]) -> list[str]:
    """The rules a write event refused a capture write by: each capture
    rule it broke, the token or undone rule, or another gate's status.
    Empty for a committed write."""
    status = event.get("status")
    if status == "committed" or not isinstance(status, str):
        return []
    if status == "capture_rule":
        rules = event.get("capture_rules")
        return (
            [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []
        )
    rule = event.get("capture_rule")
    return [rule] if status == "capture_refused" and isinstance(rule, str) else [status]


def refusals_by_batch(events: Iterable[Mapping[str, Any]]) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {}
    for event in events:
        batch = event.get("capture_batch")
        if event.get("kind") == "write" and isinstance(batch, str):
            out.setdefault(batch, Counter()).update(refusal_rules(event))
    return out


def review(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Every batch with its outcome (`open` while it takes writes), member
    count, refusals by rule, undone state and the relations its writes left
    unanswered (as the close recorded them, or as they stand while open),
    and the totals."""
    when = now or utcnow()
    closed = cap.closes(store)
    members = cap.members(store)
    undone = cap.undone_ids(store)
    refused = refusals_by_batch(store.iter_events())
    rows: list[dict[str, Any]] = []
    for batch in cap.batches(store):
        close = closed.get(batch.batch)
        outcome = close.outcome if close else "open"
        if close is None and batch.expired(when):
            outcome = cap.OUTCOME_EXPIRED
        ids = [m.memory_id for m in members if m.batch == batch.batch]
        pending = (
            close.relations_pending
            if close is not None
            else cap.relations_pending(store, batch.batch)
        )
        rows.append(
            {
                "batch": batch.batch,
                "session_id": batch.session_id,
                "opened_at": isoformat_utc(batch.opened_at),
                "mode": batch.mode,
                "outcome": outcome,
                "members": len(ids),
                "undone": sum(1 for i in ids if i in undone),
                "relations_pending": sum(len(v) for v in pending.values()),
                "refusals": dict(sorted(refused.get(batch.batch, Counter()).items())),
            }
        )
    totals: Counter[str] = Counter()
    for counts in refused.values():
        totals.update(counts)
    return {
        "batches": rows,
        "by_outcome": dict(sorted(Counter(r["outcome"] for r in rows).items())),
        "refusals_by_rule": dict(sorted(totals.items())),
        "undos": sum(1 for _ in store.capture_rows({CAPTURE_UNDO})),
    }


def render_review(doc: Mapping[str, Any]) -> str:
    lines = [
        "Capture batches: "
        + (
            ", ".join(f"{n} {k}" for k, n in doc["by_outcome"].items())
            if doc["batches"]
            else "none yet"
        )
    ]
    for row in doc["batches"]:
        refusals = ", ".join(f"{k} {n}" for k, n in row["refusals"].items())
        lines.append(
            f"  {row['batch']}  {row['opened_at']}  {row['outcome']}  "
            f"{row['members']} saved"
            + (f", {row['undone']} undone" if row["undone"] else "")
            + (
                f", {row['relations_pending']} relations pending"
                if row["relations_pending"]
                else ""
            )
            + (f", refused: {refusals}" if refusals else "")
        )
    if doc["refusals_by_rule"]:
        lines.append(
            "Refusals by rule: "
            + ", ".join(f"{k} {n}" for k, n in doc["refusals_by_rule"].items())
        )
    lines.append(f"Undos: {doc['undos']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


@dataclass
class UndoReport:
    """What undoing one batch would do, or did."""

    batch: str
    outcome: str | None = None
    to_undo: list[str] = field(default_factory=list)
    changed: dict[str, list[str]] = field(default_factory=dict)
    gone: list[str] = field(default_factory=list)
    # The conflict rows the undone members' own writes filed, deleted with
    # them.
    own_conflicts: list[str] = field(default_factory=list)
    skip_changed: bool = False
    refusal_reason: str | None = None
    committed: bool = False
    # The standing memories the undone members replaced, back in force
    # and held until a standing roster reads them (ENG-200).
    held: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_undo(
    store: Store,
    batch_id: str,
    *,
    skip_changed: bool = False,
    now: datetime | None = None,
) -> UndoReport:
    """Sort `batch_id`'s members into those to tombstone, those changed
    since (each with why) and those already gone, and say whether the
    undo may go ahead."""
    report = UndoReport(batch=batch_id, skip_changed=skip_changed)
    batch = next((b for b in cap.batches(store) if b.batch == batch_id), None)
    if batch is None:
        report.refusal_reason = f"no capture batch {batch_id}"
        return report
    close = cap.closes(store).get(batch_id)
    report.outcome = close.outcome if close else None
    if close is None and not batch.expired(now or utcnow()):
        report.refusal_reason = "the batch is still open; undo it once it settles"
        return report
    ids = list(dict.fromkeys(m.memory_id for m in cap.members(store, batch_id)))
    active = {}
    for memory_id in ids:
        try:
            active[memory_id] = store.load_one(memory_id)
        except (NotFoundError, TombstonedError):
            continue
    links = store.links_for_many(list(active))
    conflicted: set[str] = set()
    own: list[dict[str, Any]] = []
    closing = _closing_filed(store, batch_id)
    for conflict in store.list_conflicts():
        if _filed_by_own_write(conflict, active) or (
            conflict["id"] in closing and conflict.get("status") == "pending"
        ):
            own.append(conflict)
        else:
            conflicted.update(str(conflict[side]) for side in ("a_id", "b_id"))
    for memory_id in ids:
        memory = active.get(memory_id)
        if memory is None:
            report.gone.append(memory_id)
            continue
        why = []
        if memory.updated > memory.created:
            why.append(CHANGED_UPDATED)
        if memory.last_verified_at is not None:
            why.append(CHANGED_VERIFIED)
        if any(source not in ids for _type, source, _note in links[memory_id][1]):
            why.append(CHANGED_LINKED)
        if memory_id in conflicted:
            why.append(CHANGED_CONFLICT)
        if why:
            report.changed[memory_id] = why
        else:
            report.to_undo.append(memory_id)
    undone = set(report.to_undo)
    report.own_conflicts = [
        str(c["id"]) for c in own if undone & {str(c["a_id"]), str(c["b_id"])}
    ]
    if report.changed and not skip_changed:
        report.refusal_reason = (
            f"{len(report.changed)} member(s) changed since the capture; "
            "re-run with --skip-changed to undo the rest and leave those"
        )
    return report


def _closing_filed(store: Store, batch_id: str) -> set[str]:
    """The pair ids the batch's closing calls filed (`capture_relations`
    rows): the members' own, like the pairs their writes filed."""
    out: set[str] = set()
    for row in cap.relations_rows(store, batch_id):
        applied = row.get("applied")
        for result in (applied if isinstance(applied, dict) else {}).values():
            for filed in result.get("conflicts_filed") or []:
                if isinstance(filed, dict) and isinstance(filed.get("pair_id"), str):
                    out.add(filed["pair_id"])
    return out


def _filed_by_own_write(conflict: Mapping[str, Any], active: Mapping[str, Any]) -> bool:
    """Whether `conflict` is one a member's own write filed: still pending,
    with the member as `a_id`, created within `OWN_FILING_WINDOW` of it."""
    filer = active.get(str(conflict["a_id"]))
    created = parse_event_ts(conflict.get("created"))
    return (
        filer is not None
        and conflict.get("status") == "pending"
        and created is not None
        and filer.created <= created <= filer.created + OWN_FILING_WINDOW
    )


def apply_undo(store: Store, report: UndoReport) -> UndoReport:
    """Tombstone the planned members, delete the conflict rows their own
    writes filed and record the undo, in one transaction. Refuses a report
    that carries a refusal. The members are checked again inside the
    transaction, under the write lock: one changed since the plan refuses
    the undo, or under `skip_changed` is left in place, as a plan made
    then would have said."""
    if report.refusal_reason is not None:
        raise ValueError(report.refusal_reason)
    reason = f"capture undo: batch {report.batch}"
    with store.batch():
        fresh = plan_undo(store, report.batch, skip_changed=report.skip_changed)
        report.to_undo, report.changed, report.gone = (
            fresh.to_undo,
            fresh.changed,
            fresh.gone,
        )
        report.own_conflicts = fresh.own_conflicts
        report.refusal_reason = fresh.refusal_reason
        if report.refusal_reason is not None:
            return report
        for memory_id in report.to_undo:
            store.tombstone(memory_id, reason=reason)
        for conflict_id in report.own_conflicts:
            store.delete_conflict(conflict_id)
        cap.record_undo(store, report.batch, report.to_undo)
    report.committed = True
    report.held = held_by_removal(store, report.to_undo)
    return report


def render_undo(report: UndoReport) -> str:
    state = "APPLIED" if report.committed else "dry-run"
    if report.refusal_reason is not None:
        state = "refused"
    lines = [f"Capture undo: batch {report.batch} ({state})"]
    if report.outcome is not None:
        lines.append(f"Outcome: {report.outcome}")
    lines.append(f"To tombstone ({len(report.to_undo)}): {' '.join(report.to_undo)}")
    for memory_id, why in report.changed.items():
        lines.append(f"  changed since: {memory_id} ({', '.join(why)})")
    if report.gone:
        lines.append(f"Already gone ({len(report.gone)}): {' '.join(report.gone)}")
    if report.own_conflicts:
        lines.append(
            f"Conflicts their own writes filed, deleted with them "
            f"({len(report.own_conflicts)}): {' '.join(report.own_conflicts)}"
        )
    if report.refusal_reason:
        lines.append(f"Refused: {report.refusal_reason}")
    elif not report.committed:
        lines.append("Re-run with --apply to tombstone them.")
    else:
        lines.append("`bettermemory tombstones restore <id>` puts one back.")
    lines.extend(held_lines(report.held))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

# The owner grades a sample of captured memories against `CAPTURE_RULES`
# (`bettermemory capture sample` / `capture grade`); CAPS-P9 reads the
# tally: from week 2, at least 16 of 20 meet all ten rules.
DEFAULT_SAMPLE_SIZE = 20
CAPTURE_GRADE = "capture_grade"


def live_members(store: Store) -> list[tuple[cap.Member, Memory]]:
    """Each captured memory still active and not undone, with its record,
    in log order."""
    undone = cap.undone_ids(store)
    out: list[tuple[cap.Member, Memory]] = []
    seen: set[str] = set()
    for member in cap.members(store):
        if member.memory_id in undone or member.memory_id in seen:
            continue
        seen.add(member.memory_id)
        try:
            out.append((member, store.load_one(member.memory_id)))
        except (NotFoundError, TombstonedError):
            continue
    return out


def draw_members(
    population: list[tuple[cap.Member, Memory]], *, n: int, seed: int
) -> list[tuple[cap.Member, Memory]]:
    """A seeded random draw of `n`, in log order."""
    picked = random.Random(seed).sample(population, min(max(0, n), len(population)))
    return sorted(picked, key=lambda pair: pair[0].seq)


def grade_tally(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Graded and meets-all-rules counts per ISO week (`2026-W42`), the
    latest grade of a memory in a week counting once."""
    latest: dict[tuple[str, str], bool] = {}
    for event in events:
        stamp = parse_event_ts(event.get("ts"))
        memory_id = event.get("memory_id")
        if event.get("kind") != CAPTURE_GRADE or stamp is None:
            continue
        if not isinstance(memory_id, str):
            continue
        year, number, _ = stamp.isocalendar()
        latest[(f"{year}-W{number:02d}", memory_id)] = event.get("meets") is True
    out: dict[str, dict[str, int]] = {}
    for (week, _memory), meets in sorted(latest.items()):
        counts = out.setdefault(week, {"graded": 0, "meets": 0})
        counts["graded"] += 1
        counts["meets"] += int(meets)
    return out


def render_tally(tally: Mapping[str, Mapping[str, int]]) -> str:
    if not tally:
        return "No capture grades yet.\n"
    return "".join(
        f"{week}: {c['meets']} of {c['graded']} meet all ten rules\n"
        for week, c in tally.items()
    )


__all__ = [
    "CAPTURE_GRADE",
    "DEFAULT_SAMPLE_SIZE",
    "UndoReport",
    "apply_undo",
    "draw_members",
    "grade_tally",
    "live_members",
    "plan_undo",
    "refusal_rules",
    "render_review",
    "render_tally",
    "render_undo",
    "review",
]
