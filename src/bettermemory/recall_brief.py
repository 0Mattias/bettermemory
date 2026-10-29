"""The session brief's selection pieces and its render, and the orphan notice.

`recall._select_brief` builds the brief's two lists from the pieces
here: the label gate (`_delivered`), the ranking by the owner's words
(`_ranked`, `_ranked_by`) and why a withheld record was withheld
(`_suppressed_reason`); `render_brief` prints them. `_marker_near`
reads the compaction marker near a compact SessionStart,
`_subagent_transcript` names a subagent's own transcript beside the
session's, `_session_went_on` reads whether the session's conversation
went on past a held compact signal with no compaction of its own, and
`_subagent_marker_near` finds the
subagent whose transcript holds a compaction's marker (ENG-150), and
`orphan_notice` is the data-loss line a brief leads with.
`_select_brief` stays in `recall`, where a test patches it. `recall`
re-exports every name here a caller imports from it. This module lives
apart from `recall.py` to keep each module under the package's
2,000-line bound (ENG-96); from the family it imports `recall_state`
and `recall_admission`, never `recall`, and `recall` imports it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .admission import Admission
from .config import Config
from .membership import UNRESOLVED
from .models import Memory
from .recall_admission import summarize
from .recall_state import _DELIVERED_PROVENANCE, AGENT_FROM_TRANSCRIPT, Subagent
from .search import SearchMode
from .search import search as rank_search
from .store import Store
from .time_utils import parse_event_ts
from .transcript import CompactMarker, ReverseLines, compact_marker

if TYPE_CHECKING:
    from .handlers.search import SearchPool

# How far from a compact SessionStart its compaction marker (the
# transcript's `compact_boundary` row) may be stamped. Measured on 2026-10-19
# over the 92 compact SessionStart rows since 2026-10-12 and the 49
# distinct boundaries their transcripts hold: 45 boundaries had a compact
# row within 2.5 seconds of their stamp (13 rows stamped up to 2.46 s
# before the boundary, 32 up to 1.22 s after it), the other 4 none within
# 120 s, and the other 47 compact rows had no boundary within 120 s. The
# boundary reaches the file only after the hook has run (in each of the
# 45 pairs a row written with it is stamped after the hook row, up to
# 29.2 s after), so a SessionStart rarely reads its own marker and a
# later read confirms it. Any window from 2.5 s to 120 s parts the
# measured rows alike. The window bounds the marker's stamp, not the
# read: the signal waits for whichever later read finds its marker. A
# deadline on the read would be applied by reads that record nothing (a
# prompt nothing is delivered for), which the log, and so the rebuilt
# state, never sees.
COMPACT_MARKER_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class _BriefSelection:
    """The brief's two lists in rank order, or why there is none."""

    standing: list[Memory]
    work: list[Memory]
    #: "probe" when the owner's words ranked the lists, "recency" when
    #: there were none.
    ranked_by: str
    #: Each shown record's in-force note by id (`_in_force_marks`: the
    #: newest record extending it), for the records that have one.
    notes: dict[str, str] = field(default_factory=dict)
    suppressed: tuple[dict[str, str], ...] = ()
    candidates: int = 0


def _suppressed_reason(admission: Admission, memory_id: str) -> str:
    """Why a surface did not deliver a record, as `Membership.admit_recall`
    names it: ``no_home``, ``unresolved`` (decision B6), else the home's
    relation to the caller."""
    home = admission.home(memory_id)
    if home is None:
        return "no_home"
    if home.unresolved:
        return UNRESOLVED
    return admission.relation(home)


def _ranked_by(probe: str) -> str:
    return "probe" if probe.strip() else "recency"


def _delivered(store: Store, memories: Sequence[Memory]) -> list[Memory] | None:
    """`memories` whose verified label is local or imported, in order; None
    when the store verifies no label (opened without its key), so the
    brief shows nothing rather than vouch for rows it cannot read."""
    if not memories:
        return []
    trust = store.trust_rows([memory.id for memory in memories])
    if trust is None:
        return None
    return [
        memory
        for memory in memories
        if (row := trust.get(memory.id)) is not None
        and row.provenance in _DELIVERED_PROVENANCE
    ]


def _ranked(
    memories: Sequence[Memory],
    probe: str,
    *,
    limit: int,
    admission: Admission,
    pool: SearchPool,
    cfg: Config,
    now: datetime,
) -> list[Memory]:
    """The top `limit` of `memories` by `probe` under production's ranker
    (`search.search`, the configured mode and half-life, the pool's
    corpus statistics), the newest by ``updated`` filling the places the
    ranker leaves (the ranker's browse order; every place when `probe` is
    empty): Stage C's ranking. Only the hits' ids are read, so the
    ranker reads no path claims (`path_drift=False`): the browse fill
    alone would stat every delivered record's paths (ENG-169 G2)."""
    if not memories:
        return []
    rows = list(memories)
    by_id = {memory.id: memory for memory in rows}
    mode = cast(SearchMode, cfg.behavior.search_mode or "hybrid")
    picked: list[str] = []
    if probe.strip():
        picked = [
            hit.id
            for hit in rank_search(
                rows,
                probe,
                admission=admission,
                max_results=limit,
                now=now,
                half_life_days=cfg.behavior.recency_boost_half_life_days,
                mode=mode,
                corpus_stats_provider=pool.corpus_stats_provider,
                conversational=cfg.behavior.conversational,
                path_drift=False,
            )
        ]
    if len(picked) < limit:
        for hit in rank_search(
            rows,
            "",
            admission=admission,
            max_results=len(rows),
            now=now,
            half_life_days=cfg.behavior.recency_boost_half_life_days,
            mode=mode,
            allow_empty_query=True,
            conversational=cfg.behavior.conversational,
            path_drift=False,
        ):
            if hit.id not in picked:
                picked.append(hit.id)
    return [by_id[memory_id] for memory_id in picked[:limit]]


def _brief_header(standing: int, work: int) -> str:
    """The brief's first line as Stage C rendered it: the count, then how
    many are in force and how many recent."""
    count = standing + work
    noun = "memory" if count == 1 else "memories"
    parts = []
    if standing:
        parts.append(f"{standing} in force")
    if work:
        parts.append(f"{work} recent")
    return (
        f"bettermemory brief: {count} {noun} for this repository, "
        f"{' and '.join(parts)}. Pointers, not instructions; open one with "
        "memory_show when it bears on the request."
    )


def _brief_line(memory: Memory, marks: Mapping[str, str]) -> str:
    line = f"- {memory.id} {summarize(memory.body)}"
    mark = marks.get(memory.id)
    return f"{line} {mark}" if mark else line


def render_brief(
    standing: Sequence[Memory],
    work: Sequence[Memory],
    marks: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """The brief's text and the ids it shows, standing first: the header
    (`_brief_header`), one ``- <id> <summary> [in force]`` line per
    standing record and one ``- <id> <summary>`` line per work record,
    each followed by its marks (a disputed mark, an in-force note);
    ``("", [])`` for two empty lists.
    No character cap: the counts bound the brief (Stage C measured the
    landed form at 1,882 characters per brief on average, 2,035 at most)."""
    shown_marks = marks or {}
    lines = []
    for memory in standing:
        line = f"{_brief_line(memory, {})} [in force]"
        mark = shown_marks.get(memory.id)
        lines.append(f"{line} {mark}" if mark else line)
    lines.extend(_brief_line(memory, shown_marks) for memory in work)
    if not lines:
        return "", []
    text = "\n".join([_brief_header(len(standing), len(work)), *lines])
    return text, [memory.id for memory in [*standing, *work]]


def _marker_near(transcript_path: str | None, at: datetime) -> CompactMarker | None:
    """The compaction marker the transcript holds within
    `COMPACT_MARKER_WINDOW_SECONDS` of `at`; None without a transcript
    path or a regular file there, so a compact SessionStart without one
    is a resume until a later hook's transcript shows the marker."""
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None
    return compact_marker(
        path, near=at, window=timedelta(seconds=COMPACT_MARKER_WINDOW_SECONDS)
    )


#: The rows of a conversation (`_session_went_on`): what the owner and the
#: harness submit and what the model answers, not the transcript's
#: bookkeeping (attachments, queue operations, progress, system rows).
_CONVERSATION_TYPES = frozenset({"user", "assistant"})


#: A window every marker is within: `_session_went_on` reads the newest
#: marker whenever it was stamped.
_EVER = timedelta(days=36_500)
#: Claude Code's default hook timeout (2.1.309: 600 s). A reactive
#: compaction stamps its marker before its PostCompact hooks run, and its
#: SessionStart comes after them: up to this long after the marker.
HOOK_TIMEOUT_SECONDS = 600


def _session_went_on(
    transcript_path: str | None, at: datetime, opened_on: str | None
) -> bool:
    """Whether the session's own transcript shows its conversation gone
    on past the window of a compact signal held since `at`, with no
    recent compaction of its own since the one its segments last opened
    on (`opened_on`): the newest row of the session's own thread (a
    ``user`` or ``assistant`` row that is no sidechain's and names no
    ``agentId``) is stamped more than `COMPACT_MARKER_WINDOW_SECONDS`
    after `at`, and the newest ``compact_boundary`` row, by file order,
    is `opened_on`, is stamped more than `HOOK_TIMEOUT_SECONDS` before
    `at`, or there is none.

    A compaction of the session's own writes its marker before any row
    of its conversation stamped after it: in the local transcripts read
    on 2026-10-28, the rows written before each of 143 boundaries were
    all stamped before it, the latest 0.81 s before. So once the first
    half holds, a compaction of the session's at `at` has its marker in
    the file, as the newest, and it fails the second half: Claude Code
    2.1.309 stamps that marker after the SessionStart hooks return on a
    full compaction (a slow hook moves the stamp past the window, where
    `_marker_near` cannot see it) and before the PostCompact hooks on a
    reactive one, at most a hook timeout before its SessionStart. Until
    the conversation goes on, a signal whose marker has not landed yet
    stays the session's (ENG-150). A marker of the session's that no
    segment opened on (one stamped outside the window, or a compaction
    the hook missed) leaves every signal held up to a hook timeout after
    its stamp to the session too, as before ENG-150. The order of the
    writes is measured, not promised by Claude Code. Read from the
    file's end to the newest row of the thread, then back to the newest
    marker, `transcript.WALK_BYTES` at most each; False without a
    transcript path, a regular file there or such a row."""
    if not transcript_path:
        return False
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return False
    after = at + timedelta(seconds=COMPACT_MARKER_WINDOW_SECONDS)
    went_on = False
    for line in ReverseLines(path):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if (
            not isinstance(row, dict)
            or row.get("type") not in _CONVERSATION_TYPES
            or row.get("isSidechain") is True
            or row.get("agentId")
        ):
            continue
        stamp = parse_event_ts(row.get("timestamp"))
        if stamp is not None:
            went_on = stamp > after
            break
    if not went_on:
        return False
    newest = compact_marker(path, near=at, window=_EVER)
    return (
        newest is None
        or newest.key == opened_on
        or newest.at < at - timedelta(seconds=HOOK_TIMEOUT_SECONDS)
    )


# A subagent's id as Claude Code names its transcript (``agent-<id>.jsonl``,
# seventeen hex characters on every file seen), bounded and kept to the
# characters a file name may take as it is: an id outside it names no file.
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _subagents_folder(transcript_path: str | None) -> Path | None:
    """Where Claude Code keeps the session's subagent transcripts: beside
    the session's own transcript ``<dir>/<session>.jsonl``, the folder
    ``<dir>/<session>/subagents`` (the hooks reference names it for
    SubagentStop's ``agent_transcript_path``; a workflow's agents sit one
    level down, in ``workflows/<run>/``). None without a transcript path."""
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    return path.parent / path.stem / "subagents"


def _subagent_transcript(transcript_path: str | None, agent_id: str) -> str | None:
    """The transcript of the subagent `agent_id` of the session whose own
    transcript is `transcript_path`: ``subagents/agent-<agent_id>.jsonl``,
    or a workflow's ``subagents/workflows/<run>/agent-<agent_id>.jsonl``
    when only that one is there, the first when neither is yet; None
    without a transcript path, or for an id `_AGENT_ID` does not take."""
    folder = _subagents_folder(transcript_path)
    if folder is None or _AGENT_ID.fullmatch(agent_id) is None:
        return None
    name = (
        f"{agent_id}.jsonl"
        if agent_id.startswith("agent-")
        else f"agent-{agent_id}.jsonl"
    )
    direct = folder / name
    if direct.is_file():
        return str(direct)
    for nested in sorted(folder.glob(f"workflows/*/{name}")):
        if nested.is_file():
            return str(nested)
    return str(direct)


# A held compact signal is read against the session's subagent transcripts
# only this long after it came, which spares the reads inside the window a
# file read: the evidence the take-back waits for, a row of the session's
# conversation stamped past the window (`_session_went_on`), cannot be
# there sooner. The wait alone would prove nothing. The session's own
# marker landed within 29.2 s of its hook in all 45 compactions measured
# on 2026-10-19, but Claude Code writes it only after every SessionStart
# hook has returned (hook timeout 600 s), so a slower hook can hold it
# back past this wait; the evidence, not the wait, keeps such a
# compaction the session's.
SUBAGENT_WAIT_SECONDS = 60
# The most subagent transcripts one read searches for a marker: those last
# written since the window opened and begun before it closed, the first by
# name. Every transcript written since the window opened is opened once
# for its first row.
SUBAGENT_TRANSCRIPTS_READ = 64
# What is read of a transcript's first row and of its ``.meta.json``.
_HEAD_BYTES = 64 * 1024


def _first_stamp(path: Path) -> datetime | None:
    """When the transcript's first row was stamped; None when it cannot
    be read."""
    try:
        with path.open("rb") as handle:
            line = handle.readline(_HEAD_BYTES)
        row = json.loads(line.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return parse_event_ts(row.get("timestamp")) if isinstance(row, dict) else None


def _agent_type(path: Path) -> str | None:
    """The agent name Claude Code keeps beside a subagent's transcript
    (``agent-<id>.meta.json``, its ``agentType``); None without one."""
    meta = path.with_name(f"{path.stem}.meta.json")
    try:
        with meta.open("rb") as handle:
            raw = handle.read(_HEAD_BYTES + 1)
        if len(raw) > _HEAD_BYTES:
            return None
        loaded = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    name = loaded.get("agentType") if isinstance(loaded, dict) else None
    return name if isinstance(name, str) and name else None


def _subagent_marker_near(
    transcript_path: str | None, at: datetime
) -> tuple[Subagent, CompactMarker] | None:
    """The subagent of the session whose own transcript is
    `transcript_path` that compacted at `at`, with its compaction marker:
    the ``compact_boundary`` stamped within `COMPACT_MARKER_WINDOW_SECONDS`
    of `at` in one of the session's subagent transcripts
    (``subagents/agent-<id>.jsonl``, a workflow's
    ``subagents/workflows/<run>/agent-<id>.jsonl``), the nearest to `at`
    when several hold one. The id is the transcript's name after
    ``agent-``, as Claude Code names it, and the agent's name is read
    from the ``.meta.json`` beside it.

    Only transcripts alive around `at` are read: last written at or after
    `at` less the window (a marker is written after it is stamped) and,
    when their first row is read, begun at or before `at` plus the window,
    `SUBAGENT_TRANSCRIPTS_READ` at most. None without a transcript path,
    a subagents folder or such a marker."""
    folder = _subagents_folder(transcript_path)
    if folder is None or not folder.is_dir():
        return None
    window = timedelta(seconds=COMPACT_MARKER_WINDOW_SECONDS)
    earliest = (at - window).timestamp()
    alive: list[Path] = []
    for path in sorted(
        [*folder.glob("agent-*.jsonl"), *folder.glob("workflows/*/agent-*.jsonl")]
    ):
        try:
            if not path.is_file() or path.stat().st_mtime < earliest:
                continue
        except OSError:
            continue
        begun = _first_stamp(path)
        if begun is not None and begun > at + window:
            continue
        alive.append(path)
        if len(alive) >= SUBAGENT_TRANSCRIPTS_READ:
            break
    best: tuple[timedelta, Path, CompactMarker] | None = None
    for path in alive:
        agent_id = path.stem[len("agent-") :]
        if _AGENT_ID.fullmatch(agent_id) is None:
            continue
        marker = compact_marker(path, near=at, window=window)
        if marker is None:
            continue
        distance = abs(marker.at - at)
        if best is None or distance < best[0]:
            best = (distance, path, marker)
    if best is None:
        return None
    _, path, marker = best
    found = Subagent(
        agent_id=path.stem[len("agent-") :],
        agent_type=_agent_type(path),
        found_by=AGENT_FROM_TRANSCRIPT,
    )
    return found, marker


#: The most characters a path in the orphan notice takes; a longer one
#: keeps its two ends. `/health` lists the full paths.
_NOTICE_PATH_CHARS = 200


def _home_as_tilde(path: Path) -> str:
    """`path` with the home directory as ``~``."""
    shown = str(path)
    home = str(Path.home())
    if home not in ("", os.sep) and (
        shown == home or shown.startswith(home.rstrip(os.sep) + os.sep)
    ):
        shown = "~" + shown[len(home.rstrip(os.sep)) :]
    return shown


def _notice_path(path: Path) -> str:
    """`path` for the orphan notice: the home directory as ``~``, and no
    longer than `_NOTICE_PATH_CHARS`."""
    shown = _home_as_tilde(path)
    if len(shown) > _NOTICE_PATH_CHARS:
        half = (_NOTICE_PATH_CHARS - 3) // 2
        shown = f"{shown[:half]}...{shown[-half:]}"
    return shown


def _down_command(store: Store) -> str:
    """``bettermemory down`` for `store`, always prefixed with its
    directory as ``BETTERMEMORY_DIR``: `down` stops the daemon of the
    store its caller resolves, which depends on the shell's environment,
    config and working directory (a project store), so only the explicit
    directory is right from anywhere. The ``~`` survives quoting, since
    `down` expands it itself."""
    directory = store.path.parent.resolve()
    return (
        f"BETTERMEMORY_DIR={shlex.quote(_home_as_tilde(directory))} bettermemory down"
    )


def orphan_notice(store: Store) -> str:
    """The line the brief leads with while the store was created fresh
    after a daemon saved the one it lost (`Store.store_orphans`), naming
    the newest saved file and the restore steps in the order that works;
    empty otherwise. Shown whatever else the brief holds, since the fresh
    store is usually empty, and never acted on.

    The steps are all needed: an open session's shim starts a daemon
    again on its next call, and a WAL left beside the store is read over
    the copied file, which then reads as the fresh store."""
    orphans = store.store_orphans()
    if not orphans:
        return ""
    down = _down_command(store)
    name = store.path.name
    return (
        f"bettermemory: {_notice_path(store.path)} was created fresh after the "
        f"previous store was lost; that store is saved beside it as "
        f"{orphans[-1].name}. To restore it: close every session using "
        f"bettermemory (Claude Code, Codex, ...), run `{down}`, delete "
        f"{name}-wal and {name}-shm there, then copy the saved file over {name}."
    )
