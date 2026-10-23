"""Standing records (unit RS-F7 part 1): the records that direct future
work in their scope until they are replaced, and the answers that say
which they are.

A record is standing when it is ambient (its category says it shapes
every reply) or its standing answer is at or above `TAU_STANDING`, and it
is in force (`in_force.in_force`: no admitted active record supersedes
it). `guarded_ids` reads the same under every record, for the guard on a
change to a standing record (`handlers._guard`). The answer is the
probability that `review.STANDING_STATEMENT` holds of the record's body,
asked of the agent, never of a human:

- by the write review's fourth question, on every reviewed write;
- by memory_update, on an edit of the body (its `standing` argument);
- for a record with no answer for its current body (written before this
  unit, imported, or edited by a build that does not ask): in
  memory_show's response, and by `memory_admin(action="answer_standing")`,
  which a session's first output names while any such record is admitted
  for the caller (`backlog_line`).

An answer is a row of the ``standing`` table naming the body it was given
for (`store.body_sha256`). A row naming another body is no answer, so an
edit by any path, this build's or another's, puts the record back to the
agent. A body is hashed once per version of its row (`log_mac`), so the
session's first output reads the backlog without reading every body.

The question is asked while the write review is in force (`asking`, the
review's own switch, `review.current_tau`): the review's fourth question,
memory_update's, memory_show's and the first-output line go together.
memory_admin answer_standing takes an answer whenever it is given.

`TAU_STANDING` is the tau round's (RS-F7 phase 2): the ENG-7 judges
answered the question blind on a stratified seeded draw of stored
records, and the threshold is the one on the 0.05 grid whose answers
agree best with their settled verdicts (ties to the lower). Set to None,
nothing is standing but ambient records.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from typing import TYPE_CHECKING, Any

from . import _caches, identity
from .in_force import in_force
from .models import Category, snippet_window
from .review import RELATED_CAP, RELATED_TEXT_CHARS, STANDING_STATEMENT, current_tau
from .store import StandingAnswer, body_sha256

if TYPE_CHECKING:
    from .admission import Admission, RecordView
    from .store import Store

__all__ = [
    "BATCH",
    "SOURCES",
    "SOURCE_ASKED",
    "SOURCE_REVIEW",
    "SOURCE_UPDATE",
    "STANDING_STATEMENT",
    "TAU_STANDING",
    "Backlog",
    "answer_of",
    "answered_by",
    "answers",
    "asking",
    "backlog",
    "backlog_line",
    "batch_hint",
    "check_probability",
    "guarded_ids",
    "is_standing",
    "question",
    "record_text",
    "refusal_rows",
    "standing_ids",
]

#: The standing threshold, inclusive, measured by the tau round on 100
#: drawn records answered blind by the three ENG-7 judges: weighted kappa
#: of the thresholded answers against the settled verdicts peaks at 0.30
#: (0.637, 95% CI 0.446 to 0.816; 0.613 at 0.20, 0.612 at 0.35), where the
#: judges agree with each other at 0.764. None turns it off: nothing is
#: standing then but ambient records.
TAU_STANDING: float | None = 0.3

#: Where an answer came from: the write review's fourth question, the
#: `standing` argument of memory_update, or memory_admin answer_standing.
SOURCE_REVIEW = "review"
SOURCE_UPDATE = "update"
SOURCE_ASKED = "asked"
SOURCES = (SOURCE_REVIEW, SOURCE_UPDATE, SOURCE_ASKED)

#: The records one answer_standing response shows: one review's worth
#: (`review.RELATED_CAP`), each as a review shows a stored statement
#: (`record_text`, `review.RELATED_TEXT_CHARS`), the shape the agent
#: already answers per write.
BATCH = RELATED_CAP

_CALL = 'memory_admin(action="answer_standing")'
_ANSWER_FORMAT = "standing={<id>: <0 to 1>, ...}"

# The body sha of each record version read, keyed by the record's id and
# its row's MAC (which every put changes). Exact: an entry is the sha of
# the body that row version holds.
_SHA_BOUND = 65_536
_SHAS: OrderedDict[tuple[str, str], str] = OrderedDict()
_SHAS_LOCK = threading.Lock()


@_caches.register
def _clear_shas() -> None:
    with _SHAS_LOCK:
        _SHAS.clear()


def asking() -> bool:
    """Whether the standing question is asked: while the write review is
    in force (both its thresholds set)."""
    return current_tau() is not None


def is_standing(category: str | None, probability: float | None) -> bool:
    """Whether a record of `category` with the answer `probability` (None
    for none) is standing, before in-force is read."""
    if category == Category.AMBIENT.value:
        return True
    if TAU_STANDING is None or probability is None:
        return False
    return probability >= TAU_STANDING


def check_probability(value: Any, name: str = "standing") -> float:
    """`value` as a probability from 0 to 1, or a teaching ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{name} must be a number from 0 to 1, got {type(value).__name__}"
        )
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be from 0 to 1, got {value!r}")
    return number


def answered_by() -> str:
    """Who gives an answer in this request: the declared client and
    model, ``none`` for an undeclared one."""
    actor = identity.current_actor()
    client = actor.client if actor is not None else None
    model = actor.model if actor is not None else None
    return f"{client or 'none'}/{model or 'none'}"


def answer_of(probability: float, source: str) -> StandingAnswer:
    """The answer a put writes beside its record."""
    if source not in SOURCES:
        raise ValueError(f"unknown standing source {source!r}")
    return StandingAnswer(
        probability=check_probability(probability),
        source=source,
        answered_by=answered_by(),
    )


def _current(store: Store, row: Mapping[str, Any]) -> float | None:
    """The row's answer if it was given for the record's current body."""
    probability = row["probability"]
    if probability is None:
        return None
    mac = row["log_mac"]
    key = (row["id"], mac) if mac is not None else None
    sha: str | None = None
    if key is not None:
        with _SHAS_LOCK:
            sha = _SHAS.get(key)
            if sha is not None:
                _SHAS.move_to_end(key)
    if sha is None:
        body = store.body_of(row["id"])
        if body is None:
            return None
        sha = body_sha256(body)
        if key is not None:
            with _SHAS_LOCK:
                _SHAS[key] = sha
                while len(_SHAS) > _SHA_BOUND:
                    _SHAS.popitem(last=False)
    return float(probability) if sha == row["body_sha256"] else None


def answers(store: Store, ids: Sequence[str]) -> dict[str, float | None]:
    """The current answer of each of `ids` that is an active record (None
    where it has none for its current body)."""
    return {row["id"]: _current(store, row) for row in store.standing_rows(ids)}


@dataclass(frozen=True)
class Backlog:
    """The admitted records with no answer for their current body, newest
    ``updated`` first."""

    ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.ids)


def backlog(store: Store, admission: Admission) -> Backlog:
    """The records `admission` admits that wait for a standing answer."""
    waiting: list[str] = []
    for row in store.standing_rows():
        if not admission.admits_row(row["scopes"], row["recorded"]):
            continue
        if _current(store, row) is None:
            waiting.append(row["id"])
    return Backlog(tuple(waiting))


def guarded_ids(store: Store, ids: Sequence[str]) -> list[str]:
    """The records of `ids` a change to which rests on the user's words
    (`handlers._guard`): `standing_ids` under every record
    (`admission.EVERY_RECORD`), whatever the caller is admitted to, since
    admission decides what a surface delivers, not who may change a
    record."""
    from .admission import EVERY_RECORD

    if not ids:
        return []
    return standing_ids(store, EVERY_RECORD, ids)


def standing_ids(
    store: Store, admission: RecordView, ids: Sequence[str] | None = None
) -> list[str]:
    """The admitted records (of `ids`, when given) that are standing and
    in force, newest ``updated`` first."""
    if ids is not None and not ids:
        return []
    candidates: list[str] = []
    for row in store.standing_rows(ids):
        if row["category"] != Category.AMBIENT.value and (
            TAU_STANDING is None or row["probability"] is None
        ):
            continue
        if not admission.admits_row(row["scopes"], row["recorded"]):
            continue
        if is_standing(row["category"], _current(store, row)):
            candidates.append(row["id"])
    if not candidates:
        return []
    resolved = in_force(store, candidates, admission)
    return [rid for rid in candidates if not resolved[rid].history]


def record_text(body: str) -> str:
    """A stored record as a standing batch shows it: the whole body
    (stripped) up to `review.RELATED_TEXT_CHARS` characters, else its
    head in a window of that size, as a review shows a related
    statement that shares nothing with the write."""
    text = body.strip()
    if len(text) <= RELATED_TEXT_CHARS:
        return text
    return snippet_window(text, 0, RELATED_TEXT_CHARS - 3)


def refusal_rows(
    store: Store, ids: Sequence[str], admission: RecordView
) -> list[dict[str, Any]]:
    """Each guarded record a refusal names, as a standing batch shows it
    (`record_text`), in the order asked. A record the caller is not
    admitted to is guarded all the same, but its text is not quoted
    (``text`` None, ``admitted`` false), as no surface quotes a record it
    would not deliver."""
    admission.prime(ids)
    rows: list[dict[str, Any]] = []
    for memory in store.get_many(list(dict.fromkeys(ids))):
        admitted = admission.admits_id(memory.id)
        row: dict[str, Any] = {
            "id": memory.id,
            "scopes": list(memory.scopes),
            "updated": memory.updated.astimezone(timezone.utc).date().isoformat(),
            "text": record_text(memory.body) if admitted else None,
        }
        if not admitted:
            row["admitted"] = False
        rows.append(row)
    return rows


def backlog_line(count: int) -> str:
    """The first-output line naming the backlog, or "" for none."""
    if count <= 0:
        return ""
    noun = "memory has" if count == 1 else "memories have"
    return (
        f"bettermemory: {count} {noun} no standing answer. Answer them "
        f"yourself: {_CALL} shows {BATCH} at a time."
    )


def question(record_id: str) -> dict[str, Any]:
    """The standing question as a response carries it for one record."""
    return {
        "type": "probability",
        "instructions": STANDING_STATEMENT,
        "hint": (
            "This memory has no standing answer. Give the probability that "
            f'the statement holds of it: memory_admin(action="answer_standing", '
            f'standing={{"{record_id}": <0 to 1>}}).'
        ),
    }


def batch_hint(remaining: int) -> str:
    """What an answer_standing response asks next."""
    if remaining <= 0:
        return "No memory here waits for a standing answer."
    return (
        "Give each record the probability that the statement holds of its "
        f"text, in one call: {_CALL[:-1]}, {_ANSWER_FORMAT}). The next "
        "records come back with the reply."
    )
