"""The write review's counters, read from the event log.

`bettermemory eval` reports them in its `review` section, and
`bettermemory review samples` draws its samples from the same chains, so
the two surfaces cannot count a chain two ways.

A chain is a root `review_issued` row with its follow-ups, which name the
root in `parent_review_id`. It is answered when a `write` row carrying any
review_id of the chain arrives within `ANSWER_WINDOW` of the chain's latest
review before it, or when a closing call arrives (DESIGN item 3): a call
from the same session and client without a review_id that commits within
`ANSWER_WINDOW` and names every related id of the open chain's latest row.
That call is pre-answered in the log (its own `review_issued` row has
`pre_answered` true); it is folded into the chain it closes rather than
counted as a chain of its own. The daemon records a call whose review_id
names no open chain (one the log does not hold, or one a write row
already answered) the same way, so no reviewed write row falls outside
every chain. A chain with neither is unanswered once its
window has passed, and pending before that.

Every count is over chains whose root was issued inside the window. A
chain issued inside the window is judged from rows up to `ANSWER_WINDOW`
after its end, so the window's last half hour is judged like the rest.
Rows carry ids, labels, scores, client and model, never a body, and so
does everything here.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import review as _review
from .time_utils import isoformat_utc, parse_event_ts

#: How long a review waits for its answer before the chain counts as
#: unanswered (DESIGN item 12), and how far past --until a chain issued
#: inside the window is judged.
ANSWER_WINDOW = timedelta(minutes=30)

#: The event kind `bettermemory review grade` writes.
REVIEW_GRADE = "review_grade"

#: The actions a reviewed write ends in. Every one but commit writes
#: nothing.
REFUSAL_ACTIONS: tuple[str, ...] = (
    _review.ACTION_SECRET,
    _review.ACTION_UNSAFE,
    _review.ACTION_RESTATES,
)
DETECTORS: tuple[str, ...] = (
    _review.DETECTOR_REVIEW,
    _review.DETECTOR_REVIEW_DISAGREES,
)

# The action of a write row that predates `review_action`, by status.
_STATUS_ACTION = {
    "committed": _review.ACTION_COMMIT,
    "credential_warning": _review.ACTION_SECRET,
    "instruction_warning": _review.ACTION_UNSAFE,
    "duplicate": _review.ACTION_RESTATES,
    _review.ACTION_STANDING: _review.ACTION_STANDING,
}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _session_of(event: Mapping[str, Any]) -> str | None:
    actor = event.get("actor")
    if isinstance(actor, Mapping):
        session = _str(actor.get("session"))
        if session is not None:
            return session
    return _str(event.get("session"))


def _bucket_value(event: Mapping[str, Any], key: str) -> str:
    """The client or model a row is counted under: the row's own field,
    then its actor block, then `none`."""
    value = _str(event.get(key))
    if value is None:
        actor = event.get("actor")
        if isinstance(actor, Mapping):
            value = _str(actor.get(key))
    return value or _review.UNDECLARED


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedRow:
    """One `review_issued` row."""

    review_id: str
    parent_review_id: str | None
    ts: datetime
    session: str | None
    client: str
    model: str
    related: tuple[str, ...]
    pre_answered: bool
    response_chars: int | None

    @property
    def chain_key(self) -> str:
        return self.parent_review_id or self.review_id

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> IssuedRow | None:
        review_id = _str(event.get("review_id"))
        ts = parse_event_ts(event.get("ts"))
        if review_id is None or ts is None:
            return None
        chars = event.get("response_chars")
        return cls(
            review_id=review_id,
            parent_review_id=_str(event.get("parent_review_id")),
            ts=ts,
            session=_session_of(event),
            client=_bucket_value(event, "client"),
            model=_bucket_value(event, "model"),
            related=_ids(event.get("related")),
            pre_answered=event.get("pre_answered") is True,
            response_chars=(
                chars
                if isinstance(chars, int) and not isinstance(chars, bool)
                else None
            ),
        )


@dataclass(frozen=True)
class DecidedRow:
    """One `write` row a reviewed call recorded: its status, action and
    answers, and what the commit set. Ids and scores only."""

    review_id: str
    ts: datetime
    session: str | None
    client: str
    status: str
    action: str
    relation: Mapping[str, str]
    unsafe: float | None
    secret: float | None
    memory_id: str | None
    links: tuple[str, ...]
    update_suggested: tuple[str, ...]
    filings: tuple[tuple[str, str], ...]
    credential_kinds: tuple[str, ...]
    matches: tuple[str, ...]

    @property
    def committed(self) -> bool:
        return self.action == _review.ACTION_COMMIT

    @property
    def zero_attestation(self) -> bool:
        """Both scores 0 and no update, conflict or restates answer."""
        return (
            self.unsafe == 0
            and self.secret == 0
            and all(
                label == _review.LABEL_UNRELATED for label in self.relation.values()
            )
        )

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> DecidedRow | None:
        review_id = _str(event.get("review_id"))
        ts = parse_event_ts(event.get("ts"))
        status = _str(event.get("status"))
        if review_id is None or ts is None or status is None:
            return None
        action = _str(event.get("review_action")) or _STATUS_ACTION.get(status, status)
        answers = event.get("review_answers")
        relation: dict[str, str] = {}
        unsafe: float | None = None
        secret: float | None = None
        if isinstance(answers, Mapping):
            raw_relation = answers.get("relation")
            if isinstance(raw_relation, Mapping):
                relation = {
                    k: v
                    for k, v in raw_relation.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
            unsafe = _score(answers.get("unsafe"))
            secret = _score(answers.get("secret"))
        filings: list[tuple[str, str]] = []
        raw_filings = event.get("review_filings")
        if isinstance(raw_filings, list):
            for row in raw_filings:
                if not isinstance(row, Mapping):
                    continue
                target = _str(row.get("id"))
                detector = _str(row.get("detector"))
                if target is not None and detector is not None:
                    filings.append((target, detector))
        return cls(
            review_id=review_id,
            ts=ts,
            session=_session_of(event),
            client=_bucket_value(event, "client"),
            status=status,
            action=action,
            relation=relation,
            unsafe=unsafe,
            secret=secret,
            memory_id=_str(event.get("id")),
            links=_ids(event.get("supersedes_reviewed")),
            update_suggested=_ids(event.get("update_suggested")),
            filings=tuple(filings),
            credential_kinds=_ids(event.get("credential_kinds")),
            matches=_ids(event.get("matches")),
        )


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class GradeRow:
    """One `review_grade` row."""

    review_id: str
    ts: datetime
    sample_ts: datetime | None
    right_answers: bool
    right_action: bool
    pre_answered: bool

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> GradeRow | None:
        review_id = _str(event.get("review_id"))
        ts = parse_event_ts(event.get("ts"))
        right_answers = event.get("right_answers")
        right_action = event.get("right_action")
        if (
            review_id is None
            or ts is None
            or not isinstance(right_answers, bool)
            or not isinstance(right_action, bool)
        ):
            return None
        return cls(
            review_id=review_id,
            ts=ts,
            sample_ts=parse_event_ts(event.get("sample_ts")),
            right_answers=right_answers,
            right_action=right_action,
            pre_answered=event.get("pre_answered") is True,
        )


def is_review_row(event: Mapping[str, Any]) -> bool:
    """True for a row the review counters read: a review_issued row, a
    write row a reviewed call recorded, or a grade."""
    kind = event.get("kind")
    if kind in (_review.REVIEW_ISSUED, REVIEW_GRADE):
        return True
    return kind == "write" and isinstance(event.get("review_id"), str)


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------


@dataclass
class ReviewChain:
    """One review chain and how it ended.

    `state` is "answered", "unanswered" or "pending". `decided` is the
    write row that answered it: its own, or the closing call's. `closed`
    is True when a closing call answered it. `absorbed` is True for a
    pre-answered chain whose call closed another chain; it is counted
    there, not as a chain of its own."""

    key: str
    issued: list[IssuedRow] = field(default_factory=list)
    decided: DecidedRow | None = None
    closed: bool = False
    absorbed: bool = False
    state: str = "pending"

    @property
    def root(self) -> IssuedRow:
        return self.issued[0]

    @property
    def issued_at(self) -> datetime:
        return self.root.ts

    @property
    def client(self) -> str:
        return self.root.client

    @property
    def model(self) -> str:
        return self.root.model

    @property
    def session(self) -> str | None:
        return self.root.session

    @property
    def pre_answered(self) -> bool:
        return self.root.pre_answered

    @property
    def review_ids(self) -> list[str]:
        ids = [self.key]
        ids += [r.review_id for r in self.issued if r.review_id != self.key]
        return ids

    @property
    def related(self) -> list[str]:
        """Every id a review of the chain showed, in the order shown."""
        out: list[str] = []
        for row in self.issued:
            for memory_id in row.related:
                if memory_id not in out:
                    out.append(memory_id)
        return out

    @property
    def calls(self) -> int:
        """The memory_write calls the chain took: one per review issued in
        answer to a call, plus the call that decided it. A pre-answered
        row is the deciding call's own row, so it adds nothing."""
        issued_calls = sum(1 for row in self.issued if not row.pre_answered)
        return issued_calls + (1 if self.decided is not None else 0)

    @property
    def outcome(self) -> str | None:
        """The decided action, or None while not answered."""
        return self.decided.action if self.decided is not None else None

    def last_issued_before(self, ts: datetime) -> IssuedRow:
        before = [row for row in self.issued if row.ts <= ts]
        return before[-1] if before else self.root


def build_chains(
    events: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    horizon: datetime | None = None,
) -> list[ReviewChain]:
    """Every review chain in `events`, judged from rows up to `horizon`
    (all rows when None) as of `now`. Rows after the horizon are not
    read. Chains come back in the order their roots were issued."""
    issued_rows: list[IssuedRow] = []
    decided_rows: list[DecidedRow] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind == _review.REVIEW_ISSUED:
            row = IssuedRow.from_event(event)
            if row is not None and (horizon is None or row.ts <= horizon):
                issued_rows.append(row)
        elif kind == "write" and isinstance(event.get("review_id"), str):
            decided = DecidedRow.from_event(event)
            if decided is not None and (horizon is None or decided.ts <= horizon):
                decided_rows.append(decided)

    chains: dict[str, ReviewChain] = {}
    chain_of: dict[str, str] = {}
    for row in sorted(issued_rows, key=lambda r: r.ts):
        chain = chains.setdefault(row.chain_key, ReviewChain(key=row.chain_key))
        chain.issued.append(row)
        chain_of[row.review_id] = row.chain_key
        chain_of.setdefault(row.chain_key, row.chain_key)

    # A chain's own answer: the first write row carrying one of its
    # review_ids within the answer window of its latest review before it.
    for decided in sorted(decided_rows, key=lambda r: r.ts):
        key = chain_of.get(decided.review_id)
        if key is None:
            continue
        chain = chains[key]
        if chain.decided is not None:
            continue
        last = chain.last_issued_before(decided.ts)
        if decided.ts - last.ts <= ANSWER_WINDOW:
            chain.decided = decided
            chain.state = "answered"

    # The closing rule: an open chain closed by a later call from the same
    # session and client, without a review_id, that commits within the
    # window and names every related id of the open chain's latest row.
    closers = [
        c
        for c in chains.values()
        if c.pre_answered and c.decided is not None and c.decided.committed
    ]
    closers.sort(key=lambda c: c.decided.ts if c.decided is not None else c.issued_at)
    for chain in sorted(chains.values(), key=lambda c: c.issued_at):
        if chain.decided is not None or chain.pre_answered:
            continue
        last = chain.issued[-1]
        for closer in closers:
            decided = closer.decided
            if closer.absorbed or decided is None:
                continue
            if closer.session != chain.session or closer.client != chain.client:
                continue
            if not last.ts < decided.ts <= last.ts + ANSWER_WINDOW:
                continue
            if not set(last.related) <= set(decided.relation):
                continue
            closer.absorbed = True
            chain.decided = decided
            chain.closed = True
            chain.state = "answered"
            break

    for chain in chains.values():
        if chain.decided is not None:
            continue
        deadline = chain.issued[-1].ts + ANSWER_WINDOW
        chain.state = "unanswered" if deadline <= now else "pending"

    return sorted(chains.values(), key=lambda c: c.issued_at)


def _in_window(ts: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and ts < start:
        return False
    return end is None or ts < end


def chains_in_window(
    events: Iterable[Mapping[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
) -> list[ReviewChain]:
    """The chains whose root was issued in [start, end), judged from rows
    up to `ANSWER_WINDOW` after `end`, absorbed closing calls left out."""
    horizon = end + ANSWER_WINDOW if end is not None else None
    return [
        chain
        for chain in build_chains(events, now=now, horizon=horizon)
        if not chain.absorbed and _in_window(chain.issued_at, start, end)
    ]


# ---------------------------------------------------------------------------
# The counters
# ---------------------------------------------------------------------------


def _share(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _median(values: list[int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _iso_week(ts: datetime) -> str:
    year, week, _ = ts.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


@dataclass
class _Tally:
    """The counts one bucket (all chains, one client or one model) keeps."""

    issued: int = 0
    answered: int = 0
    unanswered: int = 0
    pending: int = 0
    closed_by_call: int = 0
    pre_answered: int = 0
    pre_answered_zero: int = 0
    actions: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            (_review.ACTION_COMMIT, *REFUSAL_ACTIONS), 0
        )
    )
    links: int = 0
    update_suggested: int = 0
    filings: dict[str, int] = field(default_factory=lambda: dict.fromkeys(DETECTORS, 0))
    committed_calls: int = 0
    refused_calls: int = 0
    unanswered_calls: int = 0
    response_chars: list[int] = field(default_factory=list)

    def add(self, chain: ReviewChain) -> None:
        self.issued += 1
        if chain.pre_answered:
            self.pre_answered += 1
        if chain.state == "pending":
            self.pending += 1
            return
        if chain.state == "unanswered":
            self.unanswered += 1
            self.unanswered_calls += chain.calls
            return
        self.answered += 1
        if chain.closed:
            self.closed_by_call += 1
        decided = chain.decided
        assert decided is not None
        if chain.pre_answered and decided.zero_attestation:
            self.pre_answered_zero += 1
        self.actions[decided.action] = self.actions.get(decided.action, 0) + 1
        if decided.committed:
            self.committed_calls += chain.calls
            self.links += len(decided.links)
            self.update_suggested += len(decided.update_suggested)
            for _, detector in decided.filings:
                self.filings[detector] = self.filings.get(detector, 0) + 1
        else:
            self.refused_calls += chain.calls

    @property
    def committed(self) -> int:
        return self.actions.get(_review.ACTION_COMMIT, 0)

    @property
    def refused(self) -> int:
        return sum(self.actions.get(a, 0) for a in REFUSAL_ACTIONS)

    def filings_per_100(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for detector, count in self.filings.items():
            out[detector] = (
                round(100 * count / self.committed, 2) if self.committed else None
            )
        total = sum(self.filings.values())
        out["total"] = (
            round(100 * total / self.committed, 2) if self.committed else None
        )
        return out

    def to_dict(self) -> dict[str, Any]:
        judged = self.answered + self.unanswered
        return {
            "issued": self.issued,
            "answered": self.answered,
            "unanswered": self.unanswered,
            "pending": self.pending,
            "closed_by_call": self.closed_by_call,
            "unanswered_share": _share(self.unanswered, judged),
            "pre_answered": self.pre_answered,
            "pre_answered_share": _share(self.pre_answered, self.issued),
            "pre_answered_zero": self.pre_answered_zero,
            "committed": self.committed,
            "refused": self.refused,
            "actions": dict(self.actions),
            "links": self.links,
            "update_suggested": self.update_suggested,
            "filings": dict(self.filings),
            "filings_per_100_committed": self.filings_per_100(),
            "calls_per_committed_write": (
                round(self.committed_calls / self.committed, 3)
                if self.committed
                else None
            ),
            "response_chars_p50": _median(self.response_chars),
            "response_chars_n": len(self.response_chars),
        }


@dataclass
class ReviewReport:
    """The `review` section of `bettermemory eval`."""

    start: datetime | None
    end: datetime | None
    judged_through: datetime
    total: _Tally
    by_client: dict[str, _Tally]
    by_model: dict[str, _Tally]
    filings_by_week: dict[str, dict[str, int]]
    grades: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        total = self.total.to_dict()
        return {
            "answer_window_minutes": int(ANSWER_WINDOW.total_seconds() // 60),
            "judged_through": isoformat_utc(self.judged_through),
            "chains": {
                "issued": total["issued"],
                "answered": total["answered"],
                "unanswered": total["unanswered"],
                "pending": total["pending"],
                "closed_by_call": total["closed_by_call"],
            },
            "unanswered_share": total["unanswered_share"],
            "pre_answered": total["pre_answered"],
            "pre_answered_share": total["pre_answered_share"],
            "pre_answered_zero": {
                "total": total["pre_answered_zero"],
                "by_client": {
                    k: v.pre_answered_zero for k, v in sorted(self.by_client.items())
                },
                "by_model": {
                    k: v.pre_answered_zero for k, v in sorted(self.by_model.items())
                },
            },
            "actions": total["actions"],
            "links": total["links"],
            "update_suggested": total["update_suggested"],
            "filings": total["filings"],
            "response_chars_p50": total["response_chars_p50"],
            "response_chars_n": total["response_chars_n"],
            "calls_per_committed_write": total["calls_per_committed_write"],
            "committed_chains": {
                "chains": self.total.committed,
                "calls": self.total.committed_calls,
            },
            "refused_chains": {
                "chains": self.total.refused,
                "calls": self.total.refused_calls,
            },
            "unanswered_chains": {
                "chains": self.total.unanswered,
                "calls": self.total.unanswered_calls,
            },
            "filings_per_100_committed": total["filings_per_100_committed"],
            "filings_by_week": {
                week: dict(counts)
                for week, counts in sorted(self.filings_by_week.items())
            },
            "by_client": {k: v.to_dict() for k, v in sorted(self.by_client.items())},
            "by_model": {k: v.to_dict() for k, v in sorted(self.by_model.items())},
            "grades": dict(self.grades),
        }


def compute_review_report(
    events: Iterable[Mapping[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
) -> ReviewReport:
    """The review counters over chains issued in [start, end)."""
    rows = [e for e in events if isinstance(e, Mapping) and is_review_row(e)]
    chains = chains_in_window(rows, start=start, end=end, now=now)
    total = _Tally()
    by_client: dict[str, _Tally] = {}
    by_model: dict[str, _Tally] = {}
    filings_by_week: dict[str, dict[str, int]] = {}
    for chain in chains:
        for tally in (
            total,
            by_client.setdefault(chain.client, _Tally()),
            by_model.setdefault(chain.model, _Tally()),
        ):
            tally.add(chain)
        decided = chain.decided
        if chain.state == "answered" and decided is not None and decided.committed:
            for _, detector in decided.filings:
                week = filings_by_week.setdefault(
                    _iso_week(decided.ts), dict.fromkeys(DETECTORS, 0)
                )
                week[detector] = week.get(detector, 0) + 1

    # Response sizes come from the issued review rows inside the window,
    # each counted under its own row's client and model.
    for event in rows:
        if event.get("kind") != _review.REVIEW_ISSUED:
            continue
        row = IssuedRow.from_event(event)
        if row is None or row.pre_answered or row.response_chars is None:
            continue
        if not _in_window(row.ts, start, end):
            continue
        for tally in (
            total,
            by_client.setdefault(row.client, _Tally()),
            by_model.setdefault(row.model, _Tally()),
        ):
            tally.response_chars.append(row.response_chars)

    # Grades: the latest grade per chain, for samples written inside the
    # window (the grade itself is written after the window closes).
    latest: dict[str, GradeRow] = {}
    for event in rows:
        if event.get("kind") != REVIEW_GRADE:
            continue
        grade = GradeRow.from_event(event)
        if grade is None or grade.sample_ts is None:
            continue
        if not _in_window(grade.sample_ts, start, end):
            continue
        prior = latest.get(grade.review_id)
        if prior is None or grade.ts >= prior.ts:
            latest[grade.review_id] = grade
    graded = list(latest.values())
    grades = {
        "n": len(graded),
        "right_both": sum(1 for g in graded if g.right_answers and g.right_action),
        "right_answers": sum(1 for g in graded if g.right_answers),
        "right_action": sum(1 for g in graded if g.right_action),
        "pre_answered_n": sum(1 for g in graded if g.pre_answered),
    }

    judged_through = now if end is None else min(now, end + ANSWER_WINDOW)
    return ReviewReport(
        start=start,
        end=end,
        judged_through=judged_through,
        total=total,
        by_client=by_client,
        by_model=by_model,
        filings_by_week=filings_by_week,
        grades=grades,
    )


__all__ = [
    "ANSWER_WINDOW",
    "DETECTORS",
    "DecidedRow",
    "GradeRow",
    "IssuedRow",
    "REFUSAL_ACTIONS",
    "REVIEW_GRADE",
    "ReviewChain",
    "ReviewReport",
    "build_chains",
    "chains_in_window",
    "compute_review_report",
    "is_review_row",
]
