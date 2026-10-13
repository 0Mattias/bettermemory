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
conflict is refused, and with `--skip-changed` left in place and reported.
Members come from the membership rows, so an interrupted batch, which has
no settled close, is undoable too.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import capture as cap
from .log import CAPTURE_UNDO
from .models import utcnow
from .store import NotFoundError, Store, TombstonedError
from .time_utils import isoformat_utc

# Why a member is not undone, by the name the report carries.
CHANGED_UPDATED = "updated"
CHANGED_VERIFIED = "verified"
CHANGED_LINKED = "linked"
CHANGED_CONFLICT = "conflict"


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
    count, refusals by rule and undone state, and the totals."""
    when = now or utcnow()
    closed = cap.closes(store)
    members = cap.members(store)
    undone: set[str] = set()
    for row in store.capture_rows({CAPTURE_UNDO}):
        undone.update(json.loads(row.payload).get("memory_ids") or [])
    refused = refusals_by_batch(store.iter_events())
    rows: list[dict[str, Any]] = []
    for batch in cap.batches(store):
        close = closed.get(batch.batch)
        outcome = close.outcome if close else "open"
        if close is None and batch.expired(when):
            outcome = cap.OUTCOME_EXPIRED
        ids = [m.memory_id for m in members if m.batch == batch.batch]
        rows.append(
            {
                "batch": batch.batch,
                "session_id": batch.session_id,
                "opened_at": isoformat_utc(batch.opened_at),
                "mode": batch.mode,
                "outcome": outcome,
                "members": len(ids),
                "undone": sum(1 for i in ids if i in undone),
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
    skip_changed: bool = False
    refusal_reason: str | None = None
    committed: bool = False

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
    conflicted = {
        str(c[side]) for c in store.list_conflicts() for side in ("a_id", "b_id")
    }
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
    if report.changed and not skip_changed:
        report.refusal_reason = (
            f"{len(report.changed)} member(s) changed since the capture; "
            "re-run with --skip-changed to undo the rest and leave those"
        )
    return report


def apply_undo(store: Store, report: UndoReport) -> UndoReport:
    """Tombstone the planned members and record the undo, in one
    transaction. Refuses a report that carries a refusal."""
    if report.refusal_reason is not None:
        raise ValueError(report.refusal_reason)
    reason = f"capture undo: batch {report.batch}"
    with store.batch():
        for memory_id in report.to_undo:
            store.tombstone(memory_id, reason=reason)
        cap.record_undo(store, report.batch, report.to_undo)
    report.committed = True
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
    if report.refusal_reason:
        lines.append(f"Refused: {report.refusal_reason}")
    elif not report.committed:
        lines.append("Re-run with --apply to tombstone them.")
    else:
        lines.append("`bettermemory tombstones restore <id>` puts one back.")
    return "\n".join(lines) + "\n"


__all__ = [
    "UndoReport",
    "apply_undo",
    "plan_undo",
    "refusal_rules",
    "render_review",
    "render_undo",
    "review",
]
