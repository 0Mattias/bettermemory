"""The recall surface's counters, read from the event log.

`bettermemory eval` reports them in its `surfaces` section, and
`bettermemory recall review` grades the deliveries they count, so the
two read the same rows.

A show is a `session_brief` row or a `prompt_recall` row carrying `rule`
(the recall surface's; a `prompt_recall` written by the old one-hit hook
carries none and is left to the trio's reading of it). Each id a show
names is one shown pointer. What the conversation did with it is read
from the rows after the show: a `show` of the id is an open, an applied
`use` of it that is not the auto fallback and carries an excerpt is a
use (the Stop's settlement of the pointer included), and a `use` with
outcome ``ignored`` is an ignore. These count use after show, not use
because of it: a memory the model would have opened anyway counts the
same.

A row after the show belongs to the show's conversation by its
``harness_session`` stamp when it carries one, by its own session when
that is the conversation's (the hooks' rows), and otherwise by the
worktree bridge: the conversation whose hook row (a show or a Stop's
`turn_audited`) was the latest in the row's worktree before it. A
conversation any row is stamped for is joined by the stamp only, as the
hooks join it.

Every count is over shows inside the window; the rows after a show are
read up to the window's end. Rows carry ids, counts and labels, never a
body or a prompt, and so does everything here.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .events import HARNESS_SESSION_FIELD, HARNESS_STAMPED_KINDS
from .recall import SURFACE_BRIEF, SURFACE_RECALL, is_settlement
from .time_utils import parse_event_ts

#: The event kind `bettermemory recall grade` writes.
RECALL_GRADE = "recall_grade"

#: The three samples `bettermemory recall review` draws, and the strata a
#: grade names: a prompt recall's delivery, a session brief, and a prompt
#: the recall stayed silent on before the model retrieved a project
#: memory itself.
STRATUM_DELIVERY = "delivery"
STRATUM_BRIEF = "brief"
STRATUM_SILENT = "silent"
STRATA: tuple[str, ...] = (STRATUM_DELIVERY, STRATUM_BRIEF, STRATUM_SILENT)

# The surface a show is made by, keyed by its row's kind; a settlement's
# `surface` carries the same names.
_SURFACE_OF_KIND: dict[str, str] = {
    "session_brief": SURFACE_BRIEF,
    "prompt_recall": SURFACE_RECALL,
}
SURFACES: tuple[str, ...] = (SURFACE_BRIEF, SURFACE_RECALL)

# The kinds the counters read beyond the shows: what the conversation did
# after one, the Stop's rows, the segments, the review count, the grades,
# and every kind a harness stamp rides on (a conversation any of them is
# stamped for is joined by the stamp only).
_ROW_KINDS: frozenset[str] = frozenset(
    {
        "session_brief",
        "prompt_recall",
        "turn_audited",
        "segment_opened",
        "review_unanswered",
        RECALL_GRADE,
    }
    | HARNESS_STAMPED_KINDS
)

#: The sentence the section carries about the miss rate beside it.
SILENT_MISS_NOTE = (
    "silent_miss_rate reads differently once the recall surface runs: the "
    "old miss lane turned a would-be miss into a delivery, while the "
    "recall delivers project memories only, so a miss it does not serve "
    "is flagged again. Do not compare the rate across the switch."
)


def is_surface_row(event: Mapping[str, Any]) -> bool:
    """True for a row the counters read."""
    return event.get("kind") in _ROW_KINDS


def is_surface_show(event: Mapping[str, Any]) -> bool:
    """True for a show of the recall surface: a `session_brief` row, or a
    `prompt_recall` row carrying the rule that delivered it."""
    kind = event.get("kind")
    if kind == "session_brief":
        return True
    return kind == "prompt_recall" and isinstance(event.get("rule"), str)


def surface_of(event: Mapping[str, Any]) -> str | None:
    """The surface a show row was made by, or None for any other row."""
    if not is_surface_show(event):
        return None
    return _SURFACE_OF_KIND[str(event.get("kind"))]


def shown_ids(event: Mapping[str, Any]) -> list[str]:
    """The ids a show row put in front of the model, in its order."""
    field_name = "ids" if event.get("kind") == "session_brief" else "shown_ids"
    return _ids(event.get(field_name))


def session_of(event: Mapping[str, Any]) -> str | None:
    session = event.get("session") or event.get("session_id")
    return session if isinstance(session, str) and session else None


def _ids(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _in_window(
    stamp: datetime | None, start: datetime | None, end: datetime | None
) -> bool:
    if stamp is None:
        return start is None and end is None
    return (start is None or stamp >= start) and (end is None or stamp < end)


def _attested(event: Mapping[str, Any], memory_id: str) -> bool:
    """Whether an applied `use` row attests `memory_id`: not the auto
    fallback, with a non-empty excerpt in the id's slot."""
    if event.get("outcome") != "applied" or event.get("auto") is True:
        return False
    if event.get("attribution") == "auto":
        return False
    ids = event.get("ids") or event.get("memory_ids")
    excerpts = event.get("claim_excerpts")
    if not isinstance(ids, list) or not isinstance(excerpts, list):
        return False
    for index, value in enumerate(ids):
        if value != memory_id or index >= len(excerpts):
            continue
        excerpt = excerpts[index]
        if isinstance(excerpt, str) and excerpt.strip():
            return True
    return False


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    from .eval import RateCI

    return RateCI.from_counts(numerator, denominator).to_dict()


@dataclass
class _SurfaceTally:
    deliveries: int = 0
    shown: int = 0
    opened: int = 0
    used: int = 0
    ignored: int = 0
    helped: int = 0
    chars: list[int] = field(default_factory=list)
    suppressed: dict[str, int] = field(default_factory=dict)

    def to_dict(self, *, with_suppressed: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "deliveries": self.deliveries,
            "shown": self.shown,
            "opened": self.opened,
            "used": self.used,
            "ignored": self.ignored,
            "deliveries_helped": self.helped,
            "chars_p50": (statistics.median_low(self.chars) if self.chars else None),
            "shown_opened_rate": _rate(self.opened, self.shown),
            "shown_used_rate": _rate(self.used, self.shown),
            "ignored_rate": _rate(self.ignored, self.shown),
            "delivery_helped_rate": _rate(self.helped, self.deliveries),
        }
        if with_suppressed:
            out["suppressed"] = dict(sorted(self.suppressed.items()))
        return out


@dataclass(frozen=True)
class _After:
    """A row after a show that may be what the conversation did with it."""

    stamp: datetime
    kind: str
    ids: tuple[str, ...]
    event: Mapping[str, Any]


def _conversation_rows(
    rows: list[Mapping[str, Any]], end: datetime | None
) -> dict[str, list[_After]]:
    """The `show` and `use` rows, each under the conversation it joins.

    Walks the rows in log order, holding per worktree the conversation of
    the latest hook row there (the bridge); a row stamped with a harness
    session joins that one, a row recorded under a conversation's own id
    joins it, and any other row joins the bridge's answer for its
    worktree unless that conversation is joined by stamps. Rows at or
    after `end` take no part, so a window's report does not move when the
    log grows past it."""
    bounded: list[Mapping[str, Any]] = []
    for row in rows:
        stamp = parse_event_ts(row.get("ts"))
        if stamp is not None and (end is None or stamp < end):
            bounded.append(row)
    conversations: set[str] = set()
    stamped: set[str] = set()
    for row in bounded:
        if is_surface_show(row) or row.get("kind") == "turn_audited":
            hook_session = session_of(row)
            if hook_session is not None:
                conversations.add(hook_session)
        harness = _str(row.get(HARNESS_SESSION_FIELD))
        if harness is not None:
            stamped.add(harness)
    latest: dict[str, str] = {}
    out: dict[str, list[_After]] = {}
    for row in bounded:
        kind = row.get("kind")
        stamp = parse_event_ts(row.get("ts"))
        assert stamp is not None
        worktree = _str(row.get("worktree_root"))
        session = session_of(row)
        if is_surface_show(row) or kind == "turn_audited":
            if worktree is not None and session is not None:
                latest[worktree] = session
            continue
        if kind not in ("show", "use"):
            continue
        conversation = _str(row.get(HARNESS_SESSION_FIELD))
        if conversation is None and session in conversations:
            conversation = session
        if conversation is None and worktree is not None:
            bridged = latest.get(worktree)
            if bridged is not None and bridged not in stamped:
                conversation = bridged
        if conversation is None:
            continue
        ids = (
            _ids(row.get("id"))
            if kind == "show"
            else _ids(row.get("ids") or row.get("memory_ids"))
        )
        if ids:
            out.setdefault(conversation, []).append(
                _After(stamp=stamp, kind=str(kind), ids=tuple(ids), event=row)
            )
    return out


def _latest_grades(
    rows: Iterable[Mapping[str, Any]], start: datetime | None, end: datetime | None
) -> dict[str, dict[str, int]]:
    """Per stratum, the graded ids and how many were graded relevant; the
    latest grade of an id in a sample counts."""
    latest: dict[tuple[str, str], tuple[str, bool]] = {}
    for row in rows:
        if row.get("kind") != RECALL_GRADE:
            continue
        if not _in_window(parse_event_ts(row.get("ts")), start, end):
            continue
        sample = _str(row.get("sample_id"))
        stratum = _str(row.get("stratum"))
        if sample is None or stratum not in STRATA:
            continue
        for memory_id in _ids(row.get("relevant_ids")):
            latest[(sample, memory_id)] = (stratum, True)
        for memory_id in _ids(row.get("irrelevant_ids")):
            latest[(sample, memory_id)] = (stratum, False)
    out = {stratum: {"graded": 0, "relevant": 0} for stratum in STRATA}
    for stratum, relevant in latest.values():
        out[stratum]["graded"] += 1
        out[stratum]["relevant"] += int(relevant)
    return out


def compute_surface_report(
    events: Iterable[Mapping[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, Any] | None:
    """The surfaces section over [start, end), or None when the window
    holds none of the recall surface's rows (a show, a settlement, a
    `segment_opened`, a `review_unanswered` or a `recall_grade`), so a
    log written before the surface reports exactly as it did."""
    rows = [e for e in events if isinstance(e, Mapping) and is_surface_row(e)]
    in_window = [
        row for row in rows if _in_window(parse_event_ts(row.get("ts")), start, end)
    ]
    shows = [row for row in in_window if is_surface_show(row)]
    settlements = [row for row in in_window if is_settlement(row)]
    opened_rows = [row for row in in_window if row.get("kind") == "segment_opened"]
    unanswered = [row for row in in_window if row.get("kind") == "review_unanswered"]
    grades = [row for row in in_window if row.get("kind") == RECALL_GRADE]
    if not (shows or settlements or opened_rows or unanswered or grades):
        return None

    after = _conversation_rows(rows, end)
    tallies = {surface: _SurfaceTally() for surface in SURFACES}
    sessions: set[str] = set()
    segments: set[tuple[str, int]] = set()
    rules: set[str] = set()
    for show in shows:
        surface = surface_of(show)
        session = session_of(show)
        stamp = parse_event_ts(show.get("ts"))
        assert surface is not None
        tally = tallies[surface]
        tally.deliveries += 1
        chars = show.get("chars")
        if isinstance(chars, int) and not isinstance(chars, bool):
            tally.chars.append(chars)
        rule = _str(show.get("rule"))
        if rule is not None:
            rules.add(rule)
        for entry in show.get("suppressed_ids") or []:
            if isinstance(entry, Mapping) and isinstance(entry.get("reason"), str):
                reason = str(entry["reason"])
                tally.suppressed[reason] = tally.suppressed.get(reason, 0) + 1
        if session is not None:
            sessions.add(session)
            segment = show.get("segment")
            if isinstance(segment, int) and not isinstance(segment, bool) and segment:
                segments.add((session, segment))
        later = [
            row
            for row in after.get(session or "", [])
            if stamp is not None and row.stamp >= stamp
        ]
        helped = False
        for memory_id in shown_ids(show):
            tally.shown += 1
            mine = [row for row in later if memory_id in row.ids]
            opened = any(row.kind == "show" for row in mine)
            used = any(
                row.kind == "use" and _attested(row.event, memory_id) for row in mine
            )
            ignored = any(
                row.kind == "use" and row.event.get("outcome") == "ignored"
                for row in mine
            )
            tally.opened += int(opened)
            tally.used += int(used)
            tally.ignored += int(ignored)
            helped = helped or opened or used
        tally.helped += int(helped)
    for row in opened_rows:
        session = session_of(row)
        segment = row.get("segment")
        if session is None:
            continue
        sessions.add(session)
        if isinstance(segment, int) and not isinstance(segment, bool) and segment:
            segments.add((session, segment))
        rule = _str(row.get("rule"))
        if rule is not None:
            rules.add(rule)

    settled = dict.fromkeys(SURFACES, 0)
    for row in settlements:
        settled[str(row["surface"])] += len(_ids(row.get("ids")))
    chains = 0
    for row in unanswered:
        count = row.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            chains += count

    graded = _latest_grades(grades, start, end)
    return {
        "rules": sorted(rules),
        SURFACE_BRIEF: tallies[SURFACE_BRIEF].to_dict(with_suppressed=False),
        SURFACE_RECALL: tallies[SURFACE_RECALL].to_dict(with_suppressed=True),
        "settlements": settled,
        "segments": {"sessions": len(sessions), "opened": len(segments)},
        "review_unanswered": {"turns": len(unanswered), "chains": chains},
        "grades": {
            stratum: {
                "graded": counts["graded"],
                "relevant": counts["relevant"],
                "relevant_rate": _rate(counts["relevant"], counts["graded"]),
            }
            for stratum, counts in graded.items()
        },
        "note": SILENT_MISS_NOTE,
    }


__all__ = [
    "RECALL_GRADE",
    "SILENT_MISS_NOTE",
    "STRATA",
    "STRATUM_BRIEF",
    "STRATUM_DELIVERY",
    "STRATUM_SILENT",
    "SURFACES",
    "compute_surface_report",
    "is_surface_row",
    "is_surface_show",
    "session_of",
    "shown_ids",
    "surface_of",
]
