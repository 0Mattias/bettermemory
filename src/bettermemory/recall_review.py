"""Samples of the recall surface for the owner to grade (D12).

`bettermemory recall review` draws three kinds of sample from a window:

- deliveries: `prompt_recall` rows the recall surface recorded (12 by
  default), each with the pointers it showed;
- briefs: `session_brief` rows (4), each with its pointers;
- silent prompts (4): Stop turns the recall delivered nothing on, in
  which the conversation then retrieved (a search hit or a show) a
  memory written in the caller's project, with those memories.

The prompt each sample answers is read from the session's transcript at
review time and never stored: a delivery's by its `prompt_id` (the latest
prompt before it when the row carries none), a brief's as the first
prompt after it, a silent turn's as the latest prompt before its Stop.
The CLI finds transcripts of one host only, under ``~/.claude/projects``;
a sample from another harness shows no prompt. The prompt is shown as
the probe read it (`recall.clean_prompt`).

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

from .hook import event_joined, human_prompt, joined_sessions
from .models import Memory
from .origin import Origin
from .origin import capture as capture_origin
from .recall import DEFAULT_HARNESS, clean_prompt, in_project, in_repository
from .recall_counters import (
    RECALL_GRADE,
    STRATUM_BRIEF,
    STRATUM_DELIVERY,
    STRATUM_SILENT,
    is_surface_show,
    session_of,
    shown_ids,
)
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

PROMPT_TRANSCRIPT = "transcript"
PROMPT_UNAVAILABLE = "unavailable"
PROMPT_OTHER_HOST = "other_host"

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
    """One gradable sample as the log holds it: no prompt, no body."""

    sample_id: str
    stratum: str
    ts: datetime
    session_id: str
    harness: str
    segment: int | None
    ids: tuple[str, ...]
    prompt_id: str | None = None
    transcript_path: str | None = None


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
            )
        )
    return out


def _project_test(
    memories: Mapping[str, Memory],
) -> Callable[[str, str | None], bool]:
    """Whether a memory was written in the project of a turn's worktree:
    the recall's own test (`recall.in_project`) against the worktree's
    origin as git reads it now, or, where that directory is no longer a
    repository, the same worktree root on the memory's origin."""
    callers: dict[str, Origin | None] = {}

    def caller_for(worktree: str) -> Origin | None:
        if worktree not in callers:
            caller: Origin | None = None
            if Path(worktree).is_dir():
                found = capture_origin(Path(worktree))
                if in_repository(found) and not found.git_indeterminate:
                    caller = found
            callers[worktree] = caller
        return callers[worktree]

    def test(memory_id: str, worktree: str | None) -> bool:
        memory = memories.get(memory_id)
        if memory is None or memory.origin is None or worktree is None:
            return False
        caller = caller_for(worktree)
        if caller is None:
            return memory.origin.worktree_root == worktree
        return in_project(memory.origin, caller)

    return test


def _silent(
    events: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
    memories: Mapping[str, Memory],
) -> list[Candidate]:
    """Stop turns inside the window the recall delivered nothing on, in
    which the conversation retrieved a memory of its project.

    A turn runs from the session's previous Stop (`TURN_LOOKBACK` before
    its own when it has none) to its own; a repeat audit is not a turn.
    The conversation's retrievals are joined as the hooks join them: the
    harness stamp first, the worktree bridge over the turn's rows when
    nothing is stamped."""
    project = _project_test(memories)
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
) -> dict[str, list[Candidate]]:
    """Every gradable sample of the window, per stratum, oldest first."""
    rows = [dict(e) for e in events if isinstance(e, Mapping)]
    shows = _shows(rows, start, end)
    return {
        STRATUM_DELIVERY: [c for c in shows if c.stratum == STRATUM_DELIVERY],
        STRATUM_BRIEF: [c for c in shows if c.stratum == STRATUM_BRIEF],
        STRATUM_SILENT: _silent(rows, start, end, memories),
    }


def graded_samples(events: Iterable[Mapping[str, Any]]) -> set[str]:
    """The sample ids any `recall_grade` row names."""
    return {
        sample
        for e in events
        if e.get("kind") == RECALL_GRADE and (sample := _str(e.get("sample_id")))
    }


def draw(
    population: Mapping[str, list[Candidate]],
    *,
    sizes: Mapping[str, int],
    seed: int,
    exclude: Collection[str] = (),
) -> list[Candidate]:
    """Up to `sizes[stratum]` samples of each stratum, none of `exclude`,
    in stratum then time order."""
    rng = random.Random(seed)
    chosen: list[Candidate] = []
    for stratum in (STRATUM_DELIVERY, STRATUM_BRIEF, STRATUM_SILENT):
        pool = [c for c in population.get(stratum, []) if c.sample_id not in exclude]
        size = max(0, sizes.get(stratum, 0))
        picked = rng.sample(pool, size) if len(pool) > size else list(pool)
        chosen += sorted(picked, key=lambda c: c.ts)
    return chosen


def find_candidate(
    events: Iterable[Mapping[str, Any]],
    sample_id: str,
    *,
    memories: Mapping[str, Memory],
) -> Candidate | None:
    """The sample `sample_id` names, from the whole log."""
    for pool in candidates(events, start=None, end=None, memories=memories).values():
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


def _prompts(
    rows: list[dict[str, Any]],
) -> list[tuple[datetime | None, str | None, str]]:
    """The human's prompts in a transcript: (stamp, promptId, text)."""
    out: list[tuple[datetime | None, str | None, str]] = []
    for row in rows:
        text = human_prompt(row)
        if text is not None:
            out.append(
                (parse_event_ts(row.get("timestamp")), _str(row.get("promptId")), text)
            )
    return out


def prompt_for(
    candidate: Candidate, transcripts: Transcripts
) -> tuple[str | None, str]:
    """The prompt a sample answers, from its transcript, and where it came
    from (`PROMPT_TRANSCRIPT`, `PROMPT_UNAVAILABLE`, `PROMPT_OTHER_HOST`)."""
    if candidate.harness != DEFAULT_HARNESS:
        return None, PROMPT_OTHER_HOST
    path = transcripts.find(candidate.session_id, candidate.transcript_path)
    if path is None:
        return None, PROMPT_UNAVAILABLE
    prompts = _prompts(transcripts.rows(path))
    found: str | None = None
    if candidate.prompt_id is not None:
        for _, prompt_id, text in prompts:
            if prompt_id == candidate.prompt_id:
                found = text
                break
    elif candidate.stratum == STRATUM_BRIEF:
        for stamp, _, text in prompts:
            if stamp is not None and stamp >= candidate.ts:
                found = text
                break
    else:
        for stamp, _, text in prompts:
            if stamp is not None and stamp <= candidate.ts:
                found = text
    if found is None:
        return None, PROMPT_UNAVAILABLE
    return clean_prompt(found), PROMPT_TRANSCRIPT


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
    from the store, and the prompt from the transcript."""
    samples: list[dict[str, Any]] = []
    for candidate in drawn:
        prompt, source = prompt_for(candidate, transcripts)
        samples.append(
            {
                "sample_id": candidate.sample_id,
                "stratum": candidate.stratum,
                "ts": isoformat_utc(candidate.ts),
                "session_id": candidate.session_id,
                "harness": candidate.harness,
                "segment": candidate.segment,
                "pointers": [
                    {"id": memory_id, "summary": _summary(memories.get(memory_id))}
                    for memory_id in candidate.ids
                ],
                "prompt": prompt,
                "prompt_source": source,
            }
        )
    return samples


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


def render_samples_text(doc: Mapping[str, Any]) -> str:
    """The text form of a review document."""
    window = doc["window"]
    lines = [
        f"bettermemory recall review: {len(doc['samples'])} samples "
        f"({window['since'] or 'the start'} to {window['until'] or 'now'}), "
        f"seed {doc['seed']}",
        "population "
        + " ".join(f"{name}={count}" for name, count in doc["population"].items()),
    ]
    for index, sample in enumerate(doc["samples"], start=1):
        stratum = sample["stratum"]
        lines.append("")
        lines.append(
            f"[{index}] {_STRATUM_TITLE[stratum]} {sample['sample_id']} "
            f"{sample['ts']} session {sample['session_id']}"
        )
        if sample["prompt"] is not None:
            lines.append(f"  {_PROMPT_LABEL[stratum]}:")
            lines.extend(f"    {line}" for line in sample["prompt"].splitlines())
        else:
            lines.append(f"  prompt not shown ({sample['prompt_source']})")
        for pointer in sample["pointers"]:
            lines.append(f"  {_POINTER_LABEL[stratum]} {pointer['id']}")
            lines.append(f"    {pointer['summary'] or '(no longer in the store)'}")
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
    "PROMPT_OTHER_HOST",
    "PROMPT_TRANSCRIPT",
    "PROMPT_UNAVAILABLE",
    "Transcripts",
    "build_samples",
    "candidates",
    "default_transcripts_dir",
    "draw",
    "find_candidate",
    "graded_samples",
    "prompt_for",
    "render_samples_text",
    "silent_sample_id",
]
