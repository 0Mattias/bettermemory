"""RS-F7 P6 read from a snapshot of the log: the standing roster reaches
every standing write.

THE PREDICTION (unit RS, RS-F7 P6, as Linear ENG-136 restates it for the
standing roster 9.0.0rc15 ships, ENG-112): "For each standing write, a
committed write whose own standing answer is >= TAU_STANDING or whose
category is ambient, the union of `related` and `roster` over its chain's
`review_issued` rows holds every standing record that was in force and
admitted to the writer at the write's time. Declared `supersedes` targets
and the session's disabled scopes are left out. HIT: 1.00 over at least
100 such writes. MISSED (M6): a chain lacking one, or a commit with
`conflict` answered on a named standing record. A non-standing write is
not shown the roster."

THE FORMULAS. Probe sessions (a write row whose session, session_id or
harness_session opens with "switch-probe") are excluded throughout and
counted. A write is counted by its `write` row (status committed) inside
the window.

(1) standing writes. The commit of a write is the first `memory_put` of
the id its `write` row names (`Store.put_memory` logs the record, then its
`home_put`, then its `standing_put`, in one transaction; the cue code's and
the review's `conflict_put` filings and the `write` row follow,
`handlers/write.py` `_persist` and `_commit_write`). It is standing when
`standing.is_standing(category, probability)` holds of the category that
`memory_put` carries and the probability of the commit's own
`standing_put` (none: not standing unless ambient). Both are mutation
rows, kept whatever the telemetry setting.

(2) full_set / read, the HIT ratio. expected is
`review.roster_candidates` over the store as it stood just before the
commit's first row, with `exclude_ids` the commit's declared `supersedes`
targets (its links whose note is DECLARED_NOTE) and `excluded_scopes` the
session's disabled scopes: the admitted standing records in force
(`standing.standing_ids`) the product's roster reads, related set
included. shown is `Chain.shown | Chain.roster` of `review.find_chain`
for the write row's review_id, read over the log up to the same row,
which is the chain the commit's call read; a review_id the log does not
hold shows nothing. A write holds its full set when expected - shown is
empty; each that does not prints as a miss with the ids it lacks and its
door. Target 1.00 over at least 100 read writes; a line says whether the
count has reached 100.

(3) M6's conflict clause, over every standing write, its caller recorded
or not: answers carrying `conflict` for a record standing at the write's
time, read as the guard reads it (`standing.guarded_ids`: every record,
held included, no admission; the answers name only shown ids, so every
such record is named). The answers are the write row's
`review_answers.relation`, kept with telemetry on only; when no standing
write's row carries them the answers' line prints "n/a (telemetry off)".
A write row without them is read from the `conflict_put` rows its commit
filed naming it. Detector `review` (`review.DETECTOR_REVIEW`) is a
conflict answer, which the product files only on a record the guard does
not hold standing, so one on a record standing at the write's time is
M6. That reading is a lower bound: a conflict answered on a record the
cue code linked is filed `review-disagrees`, as an unrelated or extends
answer on one is (`review._DISPUTED`), so those filings on a standing
record are printed apart as undecided and not counted; a conflict
answered on a pair the cue code filed on the same write is not filed
again (`write._file_review_pairs` skips `cue_filed`); and a filing that
fails is logged, not raised. Target 0.

(4) roster_shown / not standing, informational, no target: a committed
write that is not standing whose chain shows a roster page. A write
answered standing when its roster was shown may commit under that chain
with a lower answer, since `review.check_named` requires `named` of a
standing write only (`write._with_named`), so the product allows it and
the count has no target.

AT THE WRITE'S TIME. The log is the only history. The replay is
`Store.refold`'s: every `MUTATION_KINDS` row applied in seq order through
`store_rows._apply_mutation` into a scratch database built from `SCHEMA`
and `MEMBERSHIP_SCHEMA`, here incrementally, every other row appended to
the scratch log as the store holds it. Before a commit's first row is
applied the scratch is read through a `Store` bound to it, so the
memories, links, standing answers, `returned` marks and membership tables
(projects, checkouts, views, homes) are the ones the commit's call read.
Membership is therefore as of the write, not as of the snapshot: the
replay folds the membership tables, so the admission needs no current
model. Rows the fold cannot apply are skipped and counted, as `refold`
reports them.

ADMISSION. The writer is admitted on `REVIEW_STRATA`, the surface the
write review reads. Its caller is `admission.recorded_caller` at the
commit's origin cwd, where `handlers/write._caller_at` resolves it: the
recorded view there or above it (its worktree root's included) stands for
the resolution the call made (outcome SILENT, rule recorded_view,
project, chain and tenant as the model has them). An origin with no cwd
the product resolves at no path (decision B6), never at another, so
neither the origin's worktree root nor the write row's `worktree_root`
stamp (with no workspace declared, the daemon's own start directory,
`Recorder.worktree_for_request`) stands in for it. Such a write, and one
with no recorded view at or above its cwd, is counted on its own line,
"caller not recorded", and not read for its full set.

LEFT OUT BY DESIGN. Held records (ENG-200, rc15): a standing record with
a `returned` mark while the review is in force is on no roster until it
is read (`standing.held_ids`), and `standing_ids` leaves it out, so it is
not expected. The review is read as in force, as the shipped thresholds
set it (`review.current_tau`). Records a supersedes link from an
admitted active record takes out of force (`in_force.in_force`) are not
standing in force and are not expected. The disabled scopes are the
`scope_disable` and `scope_enable` rows replayed by
`sessions._disabled_scopes_from_events` over the rows of the write row's
session and of its SessionState: the daemon keeps one SessionState per
`identity.registry_key` of the request's actor (`SessionRegistry`), several
of which log under one process session when none declares a session, and
every row carries its actor block whatever the telemetry setting, so the
rows are keyed the same way. Not seen: a daemon restart that reset the
in-memory set inside one declared session, and a server built over one
SessionState for every caller (as a test's is), whose callers share one
set; the first can only drop a record from expected.

THE UPDATE DOOR, apart. `review_issued` rows with ``door``
"memory_update" are an update's standing roster
(`handlers/standing_review.py`), not a write's, and are printed on their
own lines, outside the count: the chains whose first row is inside the
window, those an `update` row closed (only a commit carries the
review_id), and each closed one read as a write is, M6 included, with the
memory itself left out (`_due_roster` passes it as declared). Its commit
starts at the update's own last `memory_put` of the memory after the page
it answered (`_put_memory` logs the record first in its transaction), else
its last `standing_put` (an answer-only commit writes only that), else at
the `update` row; the log ties a mutation row to no session, so a change
another call makes to the same memory between that commit and the
`update` row would be taken for it. The caller is at the `update` row's
`worktree_root` stamp: the update resolves its caller at the request's
cwd (`caller_anchoring`), which the log does not keep; the stamp is the
declared workspace's worktree, else the daemon's start directory, where an
undeclared request's cwd is too. A `review_issued` row carries no harness
stamp, so each page is tied to the `update` row its call records next
(the same session and memory: `standing_review.early_response`, or the
commit), and a chain any of whose tied rows is a probe's is excluded and
counted.

THE SNAPSHOT. --store must name a copy taken by an SQLite online backup
through a ``mode=ro`` connection. The store the product would open from
here (`_daemon_client.resolved_store_path`'s rule: BETTERMEMORY_DIR, else
the config's storage directory rule, read without creating a config) is
refused when --store is the same file. A copy left in WAL mode is read
``mode=ro``, else ``immutable=1`` when nothing is beside it; a copy that
cannot be opened ``mode=ro`` with a ``-wal`` beside it is refused, since
``immutable=1`` would not read the rows in that WAL.

Usage (counts and ids only, no memory text):

    .venv/bin/python bench/recall/read_rs_f7_p6.py --store /tmp/p6/memory.sqlite \
        --since 2026-10-31T20:40:24Z
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from pydantic import ValidationError  # noqa: E402

from bettermemory._fsutil import sqlite_file_uri  # noqa: E402
from bettermemory.admission import Admission, recorded_caller  # noqa: E402
from bettermemory.config import ENV_DIR_OVERRIDE, STORE_FILENAME  # noqa: E402
from bettermemory.config import load_config  # noqa: E402
from bettermemory.events import HARNESS_SESSION_FIELD  # noqa: E402
from bettermemory.handlers.standing_review import UPDATE_DOOR  # noqa: E402
from bettermemory.identity import Actor, registry_key  # noqa: E402
from bettermemory.location_git import NO_MACHINE  # noqa: E402
from bettermemory.log import KeyRing, LogRow, iter_rows  # noqa: E402
from bettermemory.membership import REVIEW_STRATA, Membership  # noqa: E402
from bettermemory.review import (  # noqa: E402
    DETECTOR_REVIEW,
    DETECTOR_REVIEW_DISAGREES,
    LABEL_CONFLICT,
    REVIEW_ISSUED,
    find_chain,
    roster_candidates,
)
from bettermemory.sessions import _disabled_scopes_from_events  # noqa: E402
from bettermemory.standing import guarded_ids, is_standing  # noqa: E402
from bettermemory.store import Store  # noqa: E402
from bettermemory.store_rows import _apply_mutation, _event_from_columns  # noqa: E402
from bettermemory.store_schema import (  # noqa: E402
    MEMBERSHIP_SCHEMA,
    MUTATION_KINDS,
    SCHEMA,
)

PROBE_SESSION_PREFIX = "switch-probe"
#: The door a write's chain answers (its rows carry no ``door``).
WRITE_DOOR = "memory_write"
#: The note `handlers.write._validate_declared_supersedes` gives a link the
#: writer declared in `supersedes=` (a literal there; the tests pin it).
DECLARED_NOTE = "declared at write time"
#: The count the prediction is read over.
TARGET_WRITES = 100
#: The kinds the disabled-scope replay reads.
SCOPE_KINDS = ("scope_disable", "scope_enable")
#: The kinds the first pass reads.
_SCANNED = (
    "memory_put",
    "standing_put",
    "conflict_put",
    REVIEW_ISSUED,
    "write",
    "update",
)
#: The review's filings: a conflict answer, and a disputed cue link.
_FILED = (DETECTOR_REVIEW, DETECTOR_REVIEW_DISAGREES)
#: A store id for the key ring of the scratch store, which reads no key.
_SCRATCH_STORE_ID = "0" * 32


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ro(path: Path) -> sqlite3.Connection:
    """The snapshot, read-only: ``mode=ro``, else ``immutable=1`` for a
    WAL-mode copy with nothing beside it (SQLite cannot open it
    ``mode=ro``). Raises ValueError for a copy with a ``-wal`` beside it
    that cannot be opened ``mode=ro``: ``immutable=1`` would not read the
    rows in that WAL."""
    uri = sqlite_file_uri(path.absolute())
    db = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    try:
        db.execute("select count(*) from sqlite_master").fetchone()
        return db
    except sqlite3.OperationalError:
        db.close()
    if Path(f"{path}-wal").exists():
        raise ValueError(
            f"{path} cannot be opened mode=ro and has a -wal beside it, whose "
            "rows immutable=1 would not read; take the copy by an SQLite online "
            "backup through a mode=ro connection"
        )
    return sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)


def live_store() -> Path:
    """The store the product would open from here:
    `_daemon_client.resolved_store_path`'s rule (BETTERMEMORY_DIR, else
    `Config.resolved_directory`), the config read without creating it."""
    named = os.environ.get(ENV_DIR_OVERRIDE)
    if named:
        return Path(named).expanduser().resolve() / STORE_FILENAME
    return load_config(create=False).resolved_directory() / STORE_FILENAME


def _is_live(path: Path) -> bool:
    """Whether `path` is the store the product would open: the same file
    where both exist, the same resolved path otherwise."""
    live = live_store()
    candidate = path.expanduser()
    try:
        return os.path.samefile(candidate, live)
    except OSError:
        return candidate.resolve() == live.resolve()


def _is_probe(session: Any) -> bool:
    return isinstance(session, str) and session.startswith(PROBE_SESSION_PREFIX)


def _probe_row(session: str | None, event: Mapping[str, Any]) -> bool:
    return (
        _is_probe(session)
        or _is_probe(event.get("session_id"))
        or _is_probe(event.get(HARNESS_SESSION_FIELD))
    )


def _client_key(event: Mapping[str, Any]) -> str | None:
    """The SessionState a row's call was served from: `identity.
    registry_key` of the actor block the row carries."""
    block = event.get("actor")
    if not isinstance(block, Mapping):
        return None
    try:
        return registry_key(Actor.model_validate(dict(block)))
    except ValidationError:
        return None


def _payload(row: LogRow) -> dict[str, Any] | None:
    try:
        payload = json.loads(row.payload)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Commit:
    """One committed write, or one update that closed its standing
    roster's chain, as the log shows it: `start` is the seq of its first
    commit row, `end` its `write` or `update` row's. `filed` and
    `disputed` are the records its commit filed a pair with under
    detector `review` and `review-disagrees`."""

    door: str
    record: str
    review_id: str | None
    session: str | None
    stamp: datetime
    start: int
    end: int
    category: str | None = None
    probability: float | None = None
    locations: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()
    answers: Mapping[str, Any] | None = None
    filed: tuple[str, ...] = ()
    disputed: tuple[str, ...] = ()
    client_key: str | None = None


@dataclass
class Scan:
    """The first pass: the commits to read and what the window counts
    beside them."""

    commits: list[Commit] = field(default_factory=list)
    committed_writes: int = 0
    probe_writes: int = 0
    unplaced_writes: int = 0
    update_chains: set[str] = field(default_factory=set)
    probe_chains: set[str] = field(default_factory=set)
    closed_chains: set[str] = field(default_factory=set)
    probe_update_rows: int = 0


@dataclass
class _Record:
    """What the first pass keeps of a record's rows: its first put, the
    seqs of its puts, its standing answers."""

    first_put: int | None = None
    put: Mapping[str, Any] | None = None
    puts: list[int] = field(default_factory=list)
    answers: list[tuple[int, float | None]] = field(default_factory=list)


def _origin_cwd(memory: Mapping[str, Any]) -> tuple[str, ...]:
    origin = memory.get("origin")
    cwd = _str(origin.get("cwd")) if isinstance(origin, Mapping) else None
    return (cwd,) if cwd is not None else ()


def _declared(memory: Mapping[str, Any]) -> tuple[str, ...]:
    links = memory.get("links")
    out: list[str] = []
    for link in links if isinstance(links, list) else []:
        if (
            isinstance(link, Mapping)
            and link.get("type") == "supersedes"
            and link.get("note") == DECLARED_NOTE
            and isinstance(link.get("target_id"), str)
        ):
            out.append(link["target_id"])
    return tuple(dict.fromkeys(out))


def _answers(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    answers = event.get("review_answers")
    if isinstance(answers, Mapping) and isinstance(answers.get("relation"), Mapping):
        return answers
    return None


def _between(
    seqs: Iterable[tuple[int, Any]], low: int, high: int
) -> list[tuple[int, Any]]:
    return [(seq, value) for seq, value in seqs if low < seq < high]


def _filed(
    filings: list[tuple[int, str, str, str]],
    record: str,
    low: int,
    high: int,
    detector: str,
) -> tuple[str, ...]:
    return tuple(
        b
        for seq, a, b, by in filings
        if a == record and by == detector and low < seq < high
    )


def scan(db: sqlite3.Connection, since: datetime, until: datetime) -> Scan:
    """Read the log once: the committed writes and the roster-closed
    updates inside the window, each with its commit rows; the update
    door's chains."""
    records: dict[str, _Record] = {}
    #: (seq, a_id, b_id, detector) of the review's filings
    filings: list[tuple[int, str, str, str]] = []
    #: review_id -> (seq, its chain's root, its door)
    issued: dict[str, tuple[int, str, str | None]] = {}
    #: (session, memory) -> the update-door roots whose page waits for the
    #: `update` row its call records next
    waiting: dict[tuple[str | None, str], list[str]] = {}
    opened: set[str] = set()
    writes: list[tuple[LogRow, dict[str, Any], datetime]] = []
    updates: list[tuple[LogRow, dict[str, Any], datetime]] = []
    out = Scan()
    for row in iter_rows(db):
        kind = row.kind
        if kind not in _SCANNED:
            continue
        payload = _payload(row)
        if payload is None:
            continue
        if kind == "memory_put":
            memory = payload.get("memory")
            rid = memory.get("id") if isinstance(memory, Mapping) else None
            if isinstance(memory, Mapping) and isinstance(rid, str):
                record = records.setdefault(rid, _Record())
                if record.first_put is None:
                    record.first_put, record.put = row.seq, memory
                record.puts.append(row.seq)
        elif kind == "standing_put":
            answer = payload.get("standing")
            rid = answer.get("record_id") if isinstance(answer, Mapping) else None
            if isinstance(answer, Mapping) and isinstance(rid, str):
                probability = answer.get("probability")
                records.setdefault(rid, _Record()).answers.append(
                    (
                        row.seq,
                        float(probability)
                        if isinstance(probability, (int, float))
                        and not isinstance(probability, bool)
                        else None,
                    )
                )
        elif kind == "conflict_put":
            pair = payload.get("conflict")
            if isinstance(pair, Mapping) and pair.get("detector") in _FILED:
                a, b = pair.get("a_id"), pair.get("b_id")
                if isinstance(a, str) and isinstance(b, str):
                    filings.append((row.seq, a, b, str(pair["detector"])))
        elif kind == REVIEW_ISSUED:
            event = _event_from_columns(row.ts, row.session, kind, row.payload) or {}
            review_id = _str(event.get("review_id"))
            if review_id is None:
                continue
            parent = _str(event.get("parent_review_id"))
            door = _str(event.get("door"))
            root = parent or review_id
            issued[review_id] = (row.seq, root, door)
            if door != UPDATE_DOOR:
                continue
            reviewed = _str(event.get("record"))
            if reviewed is not None:
                waiting.setdefault((row.session, reviewed), []).append(root)
            stamp = _stamp(row.ts)
            if parent is None and stamp is not None and since <= stamp < until:
                opened.add(root)
        else:
            event = _event_from_columns(row.ts, row.session, kind, row.payload) or {}
            if kind == "update":
                rid = _str(event.get("id"))
                roots = waiting.pop((row.session, rid), []) if rid is not None else []
                if roots and _probe_row(row.session, event):
                    out.probe_chains.update(roots)
            stamp = _stamp(row.ts)
            if stamp is None or not since <= stamp < until:
                continue
            if kind == "write" and event.get("status") == "committed":
                writes.append((row, event, stamp))
            elif kind == "update" and _str(event.get("review_id")) is not None:
                updates.append((row, event, stamp))
    out.update_chains = opened - out.probe_chains
    out.probe_chains &= opened
    for row, event, stamp in writes:
        if _probe_row(row.session, event):
            out.probe_writes += 1
            continue
        out.committed_writes += 1
        rid = _str(event.get("id"))
        written = records.get(rid) if rid is not None else None
        if (
            rid is None
            or written is None
            or written.first_put is None
            or written.put is None
            or written.first_put > row.seq
        ):
            out.unplaced_writes += 1
            continue
        start = written.first_put
        own = _between(written.answers, start - 1, row.seq)
        category = written.put.get("category")
        out.commits.append(
            Commit(
                door=WRITE_DOOR,
                record=rid,
                review_id=_str(event.get("review_id")),
                session=row.session or _str(event.get("session_id")),
                stamp=stamp,
                start=start,
                end=row.seq,
                category=category if isinstance(category, str) else None,
                probability=own[-1][1] if own else None,
                locations=_origin_cwd(written.put),
                declared=_declared(written.put),
                answers=_answers(event),
                filed=_filed(filings, rid, start, row.seq, DETECTOR_REVIEW),
                disputed=_filed(
                    filings, rid, start, row.seq, DETECTOR_REVIEW_DISAGREES
                ),
                client_key=_client_key(event),
            )
        )
    for row, event, stamp in updates:
        review_id = _str(event.get("review_id"))
        page = issued.get(review_id) if review_id is not None else None
        rid = _str(event.get("id"))
        if page is None or page[2] != UPDATE_DOOR or rid is None:
            continue
        if _probe_row(row.session, event):
            out.probe_update_rows += 1
            continue
        out.closed_chains.add(page[1])
        record = records.get(rid, _Record())
        puts = [seq for seq in record.puts if page[0] < seq < row.seq]
        answered = [seq for seq, _ in _between(record.answers, page[0], row.seq)]
        start = puts[-1] if puts else answered[-1] if answered else row.seq
        worktree = _str(event.get("worktree_root"))
        out.commits.append(
            Commit(
                door=UPDATE_DOOR,
                record=rid,
                review_id=review_id,
                session=row.session or _str(event.get("session_id")),
                stamp=stamp,
                start=start,
                end=row.seq,
                locations=(worktree,) if worktree is not None else (),
                declared=(rid,),
                answers=_answers(event),
                filed=_filed(filings, rid, start, row.seq, DETECTOR_REVIEW),
                disputed=_filed(
                    filings, rid, start, row.seq, DETECTOR_REVIEW_DISAGREES
                ),
                client_key=_client_key(event),
            )
        )
    out.commits.sort(key=lambda c: (c.start, c.end))
    return out


@dataclass(frozen=True)
class Reading:
    """One commit read at its time. `expected` and `missing` are empty
    and `read` False for a write that is not standing or whose caller was
    not recorded. `conflicted` is the records standing at its time its
    answers (or its `review` filings, `conflict_source`) answered
    conflict; `undecided` those its `review-disagrees` filings name."""

    commit: Commit
    standing: bool
    chain_found: bool
    roster_shown: bool
    caller_recorded: bool = False
    expected: frozenset[str] = frozenset()
    missing: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()
    conflict_source: str | None = None
    door: str = WRITE_DOOR

    @property
    def read(self) -> bool:
        return self.standing and self.caller_recorded


@dataclass
class Replay:
    """The readings, and the mutation rows the fold could not apply."""

    readings: list[Reading] = field(default_factory=list)
    rows: int = 0
    invalid: int = 0


def _folded_store(conn: sqlite3.Connection, path: Path) -> Store:
    """A `Store` reading the scratch database the replay folds into. It
    holds no key, so it writes nothing, and reads none."""
    keyring = KeyRing(path.parent / "no-keys", _SCRATCH_STORE_ID)
    return Store._bind(path, conn, keyring, None, _SCRATCH_STORE_ID)


def _guarded(store: Store, ids: Iterable[str]) -> tuple[str, ...]:
    wanted = list(dict.fromkeys(ids))
    return tuple(guarded_ids(store, wanted)) if wanted else ()


def _conflicts(
    store: Store, commit: Commit
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """M6's reading of one commit: the standing records answered
    conflict, those a `review-disagrees` filing names, and the source."""
    if commit.answers is not None:
        relation = commit.answers["relation"]
        named = [i for i, label in relation.items() if label == LABEL_CONFLICT]
        return _guarded(store, named), (), "answers"
    return (
        _guarded(store, commit.filed),
        _guarded(store, commit.disputed),
        "conflict_put",
    )


def _read_one(
    store: Store,
    commit: Commit,
    scope_events: list[tuple[str | None, dict[str, Any]]],
) -> Reading:
    chain = find_chain(store, commit.review_id) if commit.review_id else None
    found = chain is not None and chain.found
    shown = (chain.shown | chain.roster) if chain is not None and found else frozenset()
    roster_shown = bool(chain is not None and found and chain.roster)
    door = commit.door if chain is None or chain.door is None else chain.door
    standing = commit.door == UPDATE_DOOR or is_standing(
        commit.category, commit.probability
    )
    if not standing:
        return Reading(commit, False, found, roster_shown, door=door)
    conflicted, undecided, source = _conflicts(store, commit)
    model = Membership(store, NO_MACHINE)
    caller = None
    for place in commit.locations:
        caller = recorded_caller(model, place)
        if caller is not None:
            break
    if caller is None:
        return Reading(
            commit,
            True,
            found,
            roster_shown,
            conflicted=conflicted,
            undecided=undecided,
            conflict_source=source,
            door=door,
        )
    admission = Admission(REVIEW_STRATA, caller, model)
    own = [event for key, event in scope_events if key == commit.client_key]
    disabled = (
        _disabled_scopes_from_events(own, server_session=commit.session)
        if commit.session
        else set()
    )
    expected = frozenset(
        m.id
        for m in roster_candidates(
            store,
            admission=admission,
            excluded_scopes=frozenset(disabled),
            exclude_ids=commit.declared,
        )
    )
    return Reading(
        commit,
        True,
        found,
        roster_shown,
        caller_recorded=True,
        expected=expected,
        missing=tuple(sorted(expected - shown)),
        conflicted=conflicted,
        undecided=undecided,
        conflict_source=source,
        door=door,
    )


def read(db: sqlite3.Connection, commits: list[Commit], path: Path) -> Replay:
    """Replay the log into a scratch database and read each commit just
    before its first row is applied."""
    pending = sorted(commits, key=lambda c: (c.start, c.end))
    out = Replay()
    if not pending:
        return out
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    scratch.row_factory = sqlite3.Row
    try:
        scratch.executescript(SCHEMA)
        scratch.executescript(MEMBERSHIP_SCHEMA)
        scratch.execute("BEGIN")
        store = _folded_store(scratch, path)
        scope_events: list[tuple[str | None, dict[str, Any]]] = []
        index = 0
        for row in iter_rows(db):
            while index < len(pending) and pending[index].start <= row.seq:
                out.readings.append(_read_one(store, pending[index], scope_events))
                index += 1
            if index >= len(pending):
                break
            out.rows += 1
            if row.kind in MUTATION_KINDS:
                try:
                    payload = json.loads(row.payload)
                    if not isinstance(payload, dict):
                        raise TypeError("payload is not an object")
                    _apply_mutation(scratch, row.kind, payload, log_mac=row.mac)
                except (ValueError, KeyError, TypeError, ValidationError):
                    out.invalid += 1
                continue
            scratch.execute(
                "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )
            if row.kind in SCOPE_KINDS:
                event = _event_from_columns(row.ts, row.session, row.kind, row.payload)
                if event is not None:
                    scope_events.append((_client_key(event), event))
    finally:
        scratch.close()
    return out


def _ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.3f}" if denominator else "n/a"


def _misses(readings: list[Reading]) -> list[str]:
    return [
        f"    miss {r.commit.record}: missing {', '.join(r.missing)} (door {r.door})"
        for r in readings
        if r.read and r.missing
    ]


def _conflict_lines(readings: list[Reading]) -> list[str]:
    lines: list[str] = []
    for r in readings:
        if r.conflicted:
            lines.append(
                f"    conflict {r.commit.record}: {', '.join(r.conflicted)} "
                f"(from {r.conflict_source}, door {r.door})"
            )
        if r.undecided:
            lines.append(
                f"    undecided {r.commit.record}: {', '.join(r.undecided)} "
                f"(review-disagrees, door {r.door})"
            )
    return lines


def _m6(readings: list[Reading], noun: str) -> list[str]:
    """M6's conflict clause over `readings` (standing ones), by source."""
    by_answers = [r for r in readings if r.conflict_source == "answers"]
    by_filings = [r for r in readings if r.conflict_source == "conflict_put"]
    if by_answers:
        head = (
            f"  M6 conflict clause (review_answers): "
            f"{sum(1 for r in by_answers if r.conflicted)} of {len(by_answers)} "
            f"standing {noun}"
        )
    elif readings:
        head = "  M6 conflict clause (review_answers): n/a (telemetry off)"
    else:
        head = f"  M6 conflict clause (review_answers): n/a (no standing {noun})"
    return [
        head,
        f"  M6 conflict clause (conflict_put, {noun} without answers; a lower "
        f"bound): {sum(1 for r in by_filings if r.conflicted)} of "
        f"{len(by_filings)} standing {noun} (detector review); undecided "
        f"{sum(1 for r in by_filings if r.undecided)} (review-disagrees: a "
        "conflict, unrelated or extends answer on a cue-linked record)",
        *_conflict_lines(readings),
    ]


def render(found: Scan, replay: Replay, since: datetime, until: datetime) -> list[str]:
    """The report: counts and ids, no text."""
    writes = [r for r in replay.readings if r.commit.door == WRITE_DOOR]
    updates = [r for r in replay.readings if r.commit.door == UPDATE_DOOR]
    standing = [r for r in writes if r.standing]
    read_ = [r for r in standing if r.read]
    full = sum(1 for r in read_ if not r.missing)
    not_standing = [r for r in writes if not r.standing]
    lines = [
        f"RS-F7 P6 window {_iso(since)} to {_iso(until)}; log rows replayed "
        f"{replay.rows} (mutation rows the fold could not apply: {replay.invalid})",
        f"standing writes {len(standing)} (committed writes in the window "
        f"{found.committed_writes}, not standing {len(not_standing)}; probe "
        f"sessions excluded {found.probe_writes}; writes whose commit rows the "
        f"log lacks {found.unplaced_writes})",
        f"  caller not recorded (not read): {len(standing) - len(read_)}",
        f"  no chain on the log: {sum(1 for r in read_ if not r.chain_found)}",
        f"  full expected set held: {full} / {len(read_)} = {_ratio(full, len(read_))}",
        f"  misses: {len(read_) - full}",
        *_misses(read_),
        *_m6(standing, "writes"),
        f"  not standing but shown a roster (informational, no target): "
        f"{sum(1 for r in not_standing if r.roster_shown)} of {len(not_standing)}",
    ]
    reached = "yes" if len(read_) >= TARGET_WRITES else "no"
    lines.append(
        f"count reached {TARGET_WRITES}: {reached} ({len(read_)} read standing writes)"
    )
    upd_read = [r for r in updates if r.read]
    upd_full = sum(1 for r in upd_read if not r.missing)
    lines += [
        "update-door chains (door memory_update: memory_update rosters, not "
        "writes; outside the count):",
        f"  chains opened in the window {len(found.update_chains)} (probe chains "
        f"excluded {len(found.probe_chains)}), closed by a committed update "
        f"{len(found.update_chains & found.closed_chains)} (probe update rows "
        f"excluded {found.probe_update_rows})",
        f"  committed updates read {len(upd_read)} (caller not recorded "
        f"{len(updates) - len(upd_read)}); full expected set held: {upd_full} / "
        f"{len(upd_read)} = {_ratio(upd_full, len(upd_read))}",
        *_misses(upd_read),
        *_m6(updates, "updates"),
        "HIT: full set 1.00 over at least 100 read standing writes and an M6 "
        "conflict count of 0; the caller is the view recorded at the write's "
        "place in the membership the replay folds",
    ]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store", required=True, help="a snapshot copy of memory.sqlite"
    )
    parser.add_argument("--since", required=True, help="ISO 8601 UTC, inclusive")
    parser.add_argument(
        "--until", default=None, help="ISO 8601 UTC, exclusive; default now"
    )
    args = parser.parse_args()
    since = _stamp(args.since)
    until = _stamp(args.until) if args.until else datetime.now(timezone.utc)
    if since is None or until is None:
        parser.error("--since and --until take ISO 8601 timestamps")
    path = Path(args.store).expanduser()
    if _is_live(path):
        parser.error(
            "--store names the store the product opens; read a snapshot copy of "
            "it, taken by an SQLite online backup"
        )
    if not path.is_file():
        parser.error(f"no store file at {path}")
    try:
        db = _ro(path)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        found = scan(db, since, until)
        replay = read(db, found.commits, path)
    finally:
        db.close()
    for line in render(found, replay, since, until):
        print(line)


if __name__ == "__main__":
    main()
