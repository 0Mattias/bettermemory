"""Samples of the recall surface for the owner to grade (D12).

`bettermemory recall review` draws three kinds of sample from a window:

- deliveries: `prompt_recall` rows the recall surface recorded (12 by
  default), each with the pointers it showed;
- briefs: `session_brief` rows (4), each with its pointers;
- silent prompts (4): Stop turns the recall delivered nothing on, in
  which the conversation then retrieved (a search hit or a show) a
  memory of the caller's project (its home, `membership.relation`), with
  those memories.

The prompt each sample answers is read from the session's transcript at
review time and never stored: a delivery's as the user row, the human's
or not, whose text hashes to the row's `probe_query` hash through
`recall.clean_prompt` and `events.redact_query` (the production hash
exactly), falling back to its `prompt_id` row, labelled ``unverified``,
when no row hashes to it (a queued command, what the owner typed while a
turn ran, is read as a user row here: `owner_prompt.queued_text`); a brief's as the first prompt after it; a
silent turn's as the latest prompt before its Stop. Each sample names its
prompt kind: ``owner``, ``task_notification``, ``agent_message``,
``cross_session`` or ``unknown``. A sample whose prompt is in no row of a
transcript that is there is ``ungradeable``: the draw passes it over for
the next sample of its stratum in the seeded order and lists it, so
nothing leaves the record without a trace. The CLI finds transcripts of
one host only, under ``~/.claude/projects``; a sample from another
harness shows no prompt. The prompt is shown as the probe read it
(`recall.clean_prompt`).

`bettermemory recall grade` records the owner's grade of a sample's
pointers as a `recall_grade` event, which `bettermemory eval` counts in
its surfaces section. A sample already graded is not drawn again.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import random
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .admission import Admission, recorded_caller
from .events import redact_query
from .membership import PROMPT_RECALL, Membership
from .models import Memory
from .owner_prompt import owner_prompt, owner_text, queued_text, submitted_text
from .recall import DEFAULT_HARNESS, clean_prompt
from .recall_counters import (
    RECALL_GRADE,
    STRATUM_BRIEF,
    STRATUM_DELIVERY,
    STRATUM_SILENT,
    is_surface_show,
    session_of,
    shown_ids,
)
from .sessions import event_joined, joined_sessions
from .time_utils import isoformat_utc, parse_event_ts

#: How many of each the draw takes by default.
DEFAULT_DELIVERIES = 12
DEFAULT_BRIEFS = 4
DEFAULT_SILENT = 4

#: How far back a silent turn reaches when its session has no earlier
#: Stop to start from.
TURN_LOOKBACK = timedelta(minutes=30)

#: The length of a pointer's summary in a sample.
SUMMARY_CHARS = 200

#: Where a sample's prompt came from. ``transcript``: the row the probe
#: hash names (a delivery), or the row the stratum's position rule names
#: (a brief, a silent turn, a delivery without a hash). ``unverified``:
#: no row hashes to the delivery's probe, so its `prompt_id` row stands
#: in. ``ungradeable``: the transcript is there and no row fits.
#: ``unavailable``: no transcript. ``other_host``: another harness.
PROMPT_TRANSCRIPT = "transcript"
PROMPT_UNVERIFIED = "unverified"
PROMPT_UNGRADEABLE = "ungradeable"
PROMPT_UNAVAILABLE = "unavailable"
PROMPT_OTHER_HOST = "other_host"

#: What kind of prompt a sample answered, by the probed text's opening
#: (the log's preview first, the matched row second).
KIND_OWNER = "owner"
KIND_TASK_NOTIFICATION = "task_notification"
KIND_AGENT_MESSAGE = "agent_message"
KIND_CROSS_SESSION = "cross_session"
KIND_UNKNOWN = "unknown"

#: The transcript keeps a message from another session behind this first
#: line; the hook saw the tagged body after it.
_WRAPPER_FIRST_LINE = "Another Claude session sent a message"
_KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("<task-notification", KIND_TASK_NOTIFICATION),
    ("<agent-message", KIND_AGENT_MESSAGE),
    ("<cross-session-message", KIND_CROSS_SESSION),
)

_RETRIEVAL_KINDS: frozenset[str] = frozenset({"search", "show"})


def default_transcripts_dir() -> Path:
    """Where the transcript host keeps its session transcripts."""
    return Path.home() / ".claude" / "projects"


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _in_window(
    stamp: datetime | None, start: datetime | None, end: datetime | None
) -> bool:
    if stamp is None:
        return False
    return (start is None or stamp >= start) and (end is None or stamp < end)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One gradable sample as the log holds it: no prompt, no body.

    `rule` is the delivery rule that made a show (None for a silent
    turn), so a grade can be split by it; `probe_hash` and
    `probe_preview` are the redacted probe of a delivery, the hash to
    find its prompt row by and the preview to name its kind."""

    sample_id: str
    stratum: str
    ts: datetime
    session_id: str
    harness: str
    segment: int | None
    ids: tuple[str, ...]
    prompt_id: str | None = None
    transcript_path: str | None = None
    rule: str | None = None
    probe_hash: str | None = None
    probe_preview: str | None = None


def _segment(event: Mapping[str, Any]) -> int | None:
    value = event.get("segment")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def silent_sample_id(session_id: str, ts: str) -> str:
    """A silent turn's id: its Stop row carries none, so the session and
    the row's stamp name it."""
    digest = hashlib.sha256(f"{session_id}\n{ts}".encode()).hexdigest()
    return f"turn-{digest[:16]}"


def _shows(
    events: list[dict[str, Any]], start: datetime | None, end: datetime | None
) -> list[Candidate]:
    out: list[Candidate] = []
    for event in events:
        if not is_surface_show(event):
            continue
        stamp = parse_event_ts(event.get("ts"))
        sample_id = _str(event.get("event_id"))
        session = session_of(event)
        ids = shown_ids(event)
        if not _in_window(stamp, start, end) or not sample_id or not session or not ids:
            continue
        assert stamp is not None
        brief = event.get("kind") == "session_brief"
        probe = event.get("probe_query")
        probe = probe if isinstance(probe, Mapping) else {}
        out.append(
            Candidate(
                sample_id=sample_id,
                stratum=STRATUM_BRIEF if brief else STRATUM_DELIVERY,
                ts=stamp,
                session_id=session,
                harness=_str(event.get("harness")) or DEFAULT_HARNESS,
                segment=_segment(event),
                ids=tuple(ids),
                prompt_id=_str(event.get("prompt_id")),
                transcript_path=_str(event.get("transcript_path")),
                rule=_str(event.get("rule")),
                probe_hash=_str(probe.get("hash")),
                probe_preview=_str(probe.get("preview")),
            )
        )
    return out


def _project_test(
    memories: Mapping[str, Memory],
    model: Membership | None,
) -> Callable[[str, str | None], bool]:
    """Whether a memory is of the project of a turn's worktree: the
    recall's own membership test on the memory's home (`Admission.
    same_project`: on the chain of the checkout recorded at that worktree,
    or a sub-project of it, design 3.5 and D13). A worktree no checkout
    was recorded at, a memory with no home, and a call with no `model`
    answer no; the origin columns never decide."""
    admissions: dict[str, Admission | None] = {}

    def admission_for(worktree: str) -> Admission | None:
        if worktree not in admissions:
            caller = recorded_caller(model, worktree) if model is not None else None
            admissions[worktree] = (
                Admission(PROMPT_RECALL, caller, model) if caller is not None else None
            )
        return admissions[worktree]

    def test(memory_id: str, worktree: str | None) -> bool:
        if memory_id not in memories or worktree is None:
            return False
        admission = admission_for(worktree)
        return admission is not None and admission.same_project(memory_id)

    return test


def _silent(
    events: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
    memories: Mapping[str, Memory],
    model: Membership | None = None,
) -> list[Candidate]:
    """Stop turns inside the window the recall delivered nothing on, in
    which the conversation retrieved a memory of its project
    (`_project_test`).

    A turn runs from the session's previous Stop (`TURN_LOOKBACK` before
    its own when it has none) to its own; a repeat audit is not a turn.
    The conversation's retrievals are joined as the hooks join them: the
    harness stamp first, the worktree bridge over the turn's rows when
    nothing is stamped."""
    project = _project_test(memories, model)
    previous: dict[str, datetime] = {}
    # The log is in time order, so a turn's rows are one slice of it.
    stamped = [
        (event, stamp)
        for event in events
        if (stamp := parse_event_ts(event.get("ts"))) is not None
    ]
    times = [stamp for _, stamp in stamped]
    out: list[Candidate] = []
    for position, (event, stamp) in enumerate(stamped):
        if event.get("kind") != "turn_audited":
            continue
        if event.get("triggered_from") != "stop_hook":
            continue
        session = session_of(event)
        if session is None:
            continue
        since = previous.get(session, stamp - TURN_LOOKBACK)
        previous[session] = stamp
        if event.get("repeat") or not _in_window(stamp, start, end):
            continue
        first = bisect.bisect_left(times, since, hi=position)
        turn = [row for row, _ in stamped[first:position]]
        if any(
            row.get("kind") == "prompt_recall" and session_of(row) == session
            for row in turn
        ):
            continue
        worktree = _str(event.get("worktree_root"))
        sessions = joined_sessions(
            turn, harness_session=session, worktree_root=worktree
        )
        retrieved: list[str] = []
        for row in turn:
            if row.get("kind") not in _RETRIEVAL_KINDS:
                continue
            if not event_joined(row, harness_session=session, sessions=sessions):
                continue
            hits = (
                [row.get("id")]
                if row.get("kind") == "show"
                else row.get("returned") or row.get("memory_ids") or []
            )
            for memory_id in hits if isinstance(hits, list) else []:
                if (
                    isinstance(memory_id, str)
                    and memory_id not in retrieved
                    and project(memory_id, worktree)
                ):
                    retrieved.append(memory_id)
        if not retrieved:
            continue
        out.append(
            Candidate(
                sample_id=silent_sample_id(session, str(event.get("ts"))),
                stratum=STRATUM_SILENT,
                ts=stamp,
                session_id=session,
                harness=DEFAULT_HARNESS,
                segment=None,
                ids=tuple(retrieved),
            )
        )
    return out


def candidates(
    events: Iterable[Mapping[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
    memories: Mapping[str, Memory],
    model: Membership | None = None,
) -> dict[str, list[Candidate]]:
    """Every gradable sample of the window, per stratum, oldest first.
    `model` reads the homes and the recorded checkouts the silent stratum
    is judged by; without it that stratum is empty."""
    rows = [dict(e) for e in events if isinstance(e, Mapping)]
    shows = _shows(rows, start, end)
    return {
        STRATUM_DELIVERY: [c for c in shows if c.stratum == STRATUM_DELIVERY],
        STRATUM_BRIEF: [c for c in shows if c.stratum == STRATUM_BRIEF],
        STRATUM_SILENT: _silent(rows, start, end, memories, model),
    }


def graded_samples(events: Iterable[Mapping[str, Any]]) -> set[str]:
    """The sample ids any `recall_grade` row names."""
    return {
        sample
        for e in events
        if e.get("kind") == RECALL_GRADE and (sample := _str(e.get("sample_id")))
    }


def draw_order(
    population: Mapping[str, list[Candidate]],
    *,
    seed: int,
    exclude: Collection[str] = (),
) -> dict[str, list[Candidate]]:
    """Each stratum's pool, none of `exclude`, in the order the seed
    draws it: a seeded shuffle, so the first `n` are the draw of size `n`
    and the ones after them are the replacements for any the review
    cannot grade, whatever `n` is."""
    rng = random.Random(seed)
    order: dict[str, list[Candidate]] = {}
    for stratum in (STRATUM_DELIVERY, STRATUM_BRIEF, STRATUM_SILENT):
        pool = [c for c in population.get(stratum, []) if c.sample_id not in exclude]
        rng.shuffle(pool)
        order[stratum] = pool
    return order


def draw(
    population: Mapping[str, list[Candidate]],
    *,
    sizes: Mapping[str, int],
    seed: int,
    exclude: Collection[str] = (),
) -> list[Candidate]:
    """Up to `sizes[stratum]` samples of each stratum, none of `exclude`,
    in stratum then time order: the head of `draw_order`."""
    chosen: list[Candidate] = []
    for stratum, order in draw_order(population, seed=seed, exclude=exclude).items():
        size = max(0, sizes.get(stratum, 0))
        chosen += sorted(order[:size], key=lambda c: c.ts)
    return chosen


def find_candidate(
    events: Iterable[Mapping[str, Any]],
    sample_id: str,
    *,
    memories: Mapping[str, Memory],
    model: Membership | None = None,
) -> Candidate | None:
    """The sample `sample_id` names, from the whole log (`model` as for
    `candidates`)."""
    population = candidates(
        events, start=None, end=None, memories=memories, model=model
    )
    for pool in population.values():
        for candidate in pool:
            if candidate.sample_id == sample_id:
                return candidate
    return None


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def _transcript_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("rb")
    except OSError:
        return rows
    with handle:
        for raw in handle:
            try:
                row = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


class Transcripts:
    """The session transcripts under one directory, each read once."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._rows: dict[Path, list[dict[str, Any]]] = {}
        self._user_rows: dict[Path, list[_UserRow]] = {}

    def find(self, session_id: str, hint: str | None) -> Path | None:
        """The transcript of `session_id`: the path its row named when that
        file is there, else ``<session_id>.jsonl`` under the root."""
        if hint is not None and Path(hint).is_file():
            return Path(hint)
        if not self._root.is_dir():
            return None
        for path in sorted(self._root.rglob(f"{session_id}.jsonl")):
            return path
        return None

    def rows(self, path: Path) -> list[dict[str, Any]]:
        if path not in self._rows:
            self._rows[path] = _transcript_rows(path)
        return self._rows[path]

    def user_rows(self, path: Path) -> list[_UserRow]:
        """The transcript's user rows with text, hashed once."""
        if path not in self._user_rows:
            self._user_rows[path] = _user_rows(self.rows(path))
        return self._user_rows[path]


@dataclass(frozen=True)
class _UserRow:
    """One user row of a transcript with text in it, the human's or not,
    or one queued command (`owner_prompt.queued_text`), which has no
    prompt id.

    `probed` is the text the hook would have received for it: the row's
    text, or the tagged body after the wrapper's first line for a message
    from another session; `digest` is `probed` hashed as the log hashes a
    probe, through `recall.clean_prompt` and `events.redact_query`."""

    stamp: datetime | None
    prompt_id: str | None
    text: str
    probed: str
    digest: str
    human: bool
    #: A queued command: stamped when typed, inside the turn before it, so
    #: never the prompt a turn is read by position.
    queued: bool = False


def _row_text(row: dict[str, Any]) -> str | None:
    """The text of a submitted user row (`owner_prompt.submitted_text`),
    the owner's or not; None for a row without text."""
    text = submitted_text(row)
    if text is None or not text.strip():
        return None
    return text


def probed_text(text: str) -> str:
    """What the hook received for a transcript row's text: the text, or,
    for a message from another session, the tagged body after the
    wrapper's first line."""
    head = text.lstrip()
    if head.startswith(_WRAPPER_FIRST_LINE):
        _, separator, body = head.partition("\n")
        return body if separator else ""
    return text


def probe_digest(text: str) -> str:
    """The hash a `prompt_recall` row carries for a prompt whose text was
    `text`: the production redaction of the cleaned prompt."""
    return str(redact_query(clean_prompt(text))["hash"])


def _user_rows(rows: list[dict[str, Any]]) -> list[_UserRow]:
    out: list[_UserRow] = []
    for row in rows:
        text = _row_text(row)
        queued = False
        if text is not None:
            human = owner_prompt(row) is not None
            prompt_id = _str(row.get("promptId"))
        else:
            text = queued_text(row)
            if text is None:
                continue
            human = owner_text(text) is not None
            prompt_id = None
            queued = True
        probed = probed_text(text)
        out.append(
            _UserRow(
                stamp=parse_event_ts(row.get("timestamp")),
                prompt_id=prompt_id,
                text=text,
                probed=probed,
                digest=probe_digest(probed),
                human=human,
                queued=queued,
            )
        )
    return out


def prompt_kind(text: str | None) -> str | None:
    """The kind a probed text's opening names: a task notification, an
    agent message, a cross-session message, `KIND_UNKNOWN` for any other
    tag or the wrapper, None when it opens as someone's words."""
    if text is None:
        return None
    head = text.lstrip()
    for prefix, kind in _KIND_BY_PREFIX:
        if head.startswith(prefix):
            return kind
    if head.startswith("<") or head.startswith(_WRAPPER_FIRST_LINE):
        return KIND_UNKNOWN
    return None


def _by_prompt_id(rows: list[_UserRow], prompt_id: str) -> _UserRow | None:
    for row in rows:
        if row.human and row.prompt_id == prompt_id:
            return row
    return None


def _by_hash(rows: list[_UserRow], candidate: Candidate) -> _UserRow | None:
    """The row whose probed text hashes to the delivery's probe: the
    latest at or before the delivery when several do (a short prompt
    repeats), else the first."""
    matches = [row for row in rows if row.digest == candidate.probe_hash]
    before = [m for m in matches if m.stamp is not None and m.stamp <= candidate.ts]
    if before:
        return before[-1]
    return matches[0] if matches else None


def prompt_for(
    candidate: Candidate, transcripts: Transcripts
) -> tuple[str | None, str, str]:
    """The prompt a sample answers, from its transcript; where it came
    from (`PROMPT_TRANSCRIPT`, `PROMPT_UNVERIFIED`, `PROMPT_UNGRADEABLE`,
    `PROMPT_UNAVAILABLE`, `PROMPT_OTHER_HOST`); and its kind (`KIND_OWNER`,
    `KIND_TASK_NOTIFICATION`, `KIND_AGENT_MESSAGE`, `KIND_CROSS_SESSION`,
    `KIND_UNKNOWN`).

    A delivery with a probe hash is matched to the row whose probed text
    hashes to it, whoever wrote the row; when none does, its `prompt_id`
    row stands in as ``unverified``. A delivery without a hash (a log
    written before 9) takes its `prompt_id` row, else the latest human
    prompt before it. A brief takes the first human prompt after it, a
    silent turn the latest before its Stop; a queued command, stamped
    inside the turn it was typed in, is never read by position. The kind
    is read from the
    log's preview when that opens with a tag, else from the row found."""
    hinted = prompt_kind(candidate.probe_preview)
    if candidate.harness != DEFAULT_HARNESS:
        return None, PROMPT_OTHER_HOST, hinted or KIND_UNKNOWN
    path = transcripts.find(candidate.session_id, candidate.transcript_path)
    if path is None:
        return None, PROMPT_UNAVAILABLE, hinted or KIND_UNKNOWN
    rows = transcripts.user_rows(path)
    found: _UserRow | None = None
    source = PROMPT_TRANSCRIPT
    if candidate.stratum == STRATUM_DELIVERY and candidate.probe_hash is not None:
        found = _by_hash(rows, candidate)
        if found is None and candidate.prompt_id is not None:
            found = _by_prompt_id(rows, candidate.prompt_id)
            source = PROMPT_UNVERIFIED
    elif candidate.prompt_id is not None:
        found = _by_prompt_id(rows, candidate.prompt_id)
    elif candidate.stratum == STRATUM_BRIEF:
        for row in rows:
            if row.queued or not row.human or row.stamp is None:
                continue
            if row.stamp >= candidate.ts:
                found = row
                break
    else:
        for row in rows:
            if row.queued or not row.human or row.stamp is None:
                continue
            if row.stamp <= candidate.ts:
                found = row
    if found is None:
        return None, PROMPT_UNGRADEABLE, hinted or KIND_UNKNOWN
    kind = hinted or (
        KIND_OWNER if found.human else prompt_kind(found.probed) or KIND_UNKNOWN
    )
    return clean_prompt(found.probed), source, kind


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


def _summary(memory: Memory | None) -> str | None:
    if memory is None:
        return None
    text = " ".join(memory.body.split())
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[: SUMMARY_CHARS - 1].rstrip() + "…"


def build_samples(
    drawn: list[Candidate],
    *,
    memories: Mapping[str, Memory],
    transcripts: Transcripts,
) -> list[dict[str, Any]]:
    """The samples as the review shows them: the pointers with a summary
    from the store, and the prompt from the transcript with where it came
    from and what kind of prompt it was."""
    samples: list[dict[str, Any]] = []
    for candidate in drawn:
        prompt, source, kind = prompt_for(candidate, transcripts)
        samples.append(
            {
                "sample_id": candidate.sample_id,
                "stratum": candidate.stratum,
                "ts": isoformat_utc(candidate.ts),
                "session_id": candidate.session_id,
                "harness": candidate.harness,
                "segment": candidate.segment,
                "rule": candidate.rule,
                "pointers": [
                    {"id": memory_id, "summary": _summary(memories.get(memory_id))}
                    for memory_id in candidate.ids
                ],
                "prompt": prompt,
                "prompt_source": source,
                "prompt_kind": kind,
            }
        )
    return samples


def select_samples(
    population: Mapping[str, list[Candidate]],
    *,
    sizes: Mapping[str, int],
    seed: int,
    exclude: Collection[str],
    memories: Mapping[str, Memory],
    transcripts: Transcripts,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The review's samples and the ones it passed over.

    Each stratum is walked in its seeded order (`draw_order`) until
    `sizes[stratum]` samples with a prompt are in hand; a sample whose
    prompt cannot be read (``ungradeable``, ``unavailable`` or
    ``other_host``) is passed over for the next and returned in the
    second list, so the record shows what the draw skipped. The samples
    come in stratum then time order."""
    chosen: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for stratum, order in draw_order(population, seed=seed, exclude=exclude).items():
        size = max(0, sizes.get(stratum, 0))
        taken: list[dict[str, Any]] = []
        for candidate in order:
            if len(taken) >= size:
                break
            (sample,) = build_samples(
                [candidate], memories=memories, transcripts=transcripts
            )
            if sample["prompt"] is None:
                passed.append(sample)
            else:
                taken.append(sample)
        chosen += sorted(taken, key=lambda sample: str(sample["ts"]))
    return chosen, passed


_STRATUM_TITLE = {
    STRATUM_DELIVERY: "recall delivery",
    STRATUM_BRIEF: "session brief",
    STRATUM_SILENT: "silent prompt",
}
_PROMPT_LABEL = {
    STRATUM_DELIVERY: "prompt",
    STRATUM_BRIEF: "first prompt after it",
    STRATUM_SILENT: "prompt",
}
_POINTER_LABEL = {
    STRATUM_DELIVERY: "pointer",
    STRATUM_BRIEF: "pointer",
    STRATUM_SILENT: "retrieved",
}


def _kind_label(sample: Mapping[str, Any]) -> str:
    kind = str(sample.get("prompt_kind") or KIND_UNKNOWN).replace("_", " ")
    if sample.get("prompt_source") == PROMPT_UNVERIFIED:
        return f"{kind}, {PROMPT_UNVERIFIED}"
    return kind


def render_samples_text(doc: Mapping[str, Any]) -> str:
    """The text form of a review document."""
    window = doc["window"]
    passed = list(doc.get("ungradeable") or [])
    population = "population " + " ".join(
        f"{name}={count}" for name, count in doc["population"].items()
    )
    if passed:
        skipped: dict[str, int] = {}
        for sample in passed:
            skipped[sample["stratum"]] = skipped.get(sample["stratum"], 0) + 1
        population += " ungradeable " + " ".join(
            f"{name}={count}" for name, count in skipped.items()
        )
    lines = [
        f"bettermemory recall review: {len(doc['samples'])} samples "
        f"({window['since'] or 'the start'} to {window['until'] or 'now'}), "
        f"seed {doc['seed']}",
        population,
    ]
    for index, sample in enumerate(doc["samples"], start=1):
        stratum = sample["stratum"]
        lines.append("")
        lines.append(
            f"[{index}] {_STRATUM_TITLE[stratum]} {sample['sample_id']} "
            f"{sample['ts']} session {sample['session_id']}"
        )
        if sample["prompt"] is not None:
            lines.append(f"  {_PROMPT_LABEL[stratum]} ({_kind_label(sample)}):")
            lines.extend(f"    {line}" for line in sample["prompt"].splitlines())
        else:
            lines.append(f"  prompt not shown ({sample['prompt_source']})")
        for pointer in sample["pointers"]:
            lines.append(f"  {_POINTER_LABEL[stratum]} {pointer['id']}")
            lines.append(f"    {pointer['summary'] or '(no longer in the store)'}")
    if passed:
        lines.append("")
        lines.append("passed over, no prompt to grade against:")
        for sample in passed:
            lines.append(
                f"  {_STRATUM_TITLE[sample['stratum']]} {sample['sample_id']} "
                f"{sample['ts']} session {sample['session_id']}: "
                f"{sample['prompt_source']}, {_kind_label(sample)}"
            )
    lines.append("")
    lines.append(
        "Grade each with: bettermemory recall grade <sample_id> "
        "--relevant <id>... --irrelevant <id>..."
    )
    lines.append(
        "A silent prompt's ids are relevant when the recall should have "
        "pointed at them."
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "Candidate",
    "DEFAULT_BRIEFS",
    "DEFAULT_DELIVERIES",
    "DEFAULT_SILENT",
    "KIND_AGENT_MESSAGE",
    "KIND_CROSS_SESSION",
    "KIND_OWNER",
    "KIND_TASK_NOTIFICATION",
    "KIND_UNKNOWN",
    "PROMPT_OTHER_HOST",
    "PROMPT_TRANSCRIPT",
    "PROMPT_UNAVAILABLE",
    "PROMPT_UNGRADEABLE",
    "PROMPT_UNVERIFIED",
    "Transcripts",
    "build_samples",
    "candidates",
    "default_transcripts_dir",
    "draw",
    "draw_order",
    "find_candidate",
    "graded_samples",
    "probe_digest",
    "probed_text",
    "prompt_for",
    "prompt_kind",
    "render_samples_text",
    "select_samples",
    "silent_sample_id",
]
