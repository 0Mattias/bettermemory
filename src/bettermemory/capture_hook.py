"""The Stop endpoint's capture half: when a turn end asks the chat model
to save the stretch since its last capture, and how that request settles.

Nothing here calls a model (`capture` says why). With `[capture]
stop_checkpoint` on, in a session a person is attending, a Stop may open
a batch (`capture.open_batch`) and answer with the instruction as the
hook's `additionalContext`, which Claude Code hands the model as it
continues the turn. The next Stop arrives with `stop_hook_active` set and
settles the batch: the stretch up to the transcript's size then is
captured. A Stop without the flag that finds the batch still open means
the user interrupted the capture turn: it closes as interrupted, nothing
is captured, and the session is left alone for `QUIET_AFTER_INTERRUPT`.
A batch whose instruction never reached the model closes as failed
instead (`close_failed`), which neither goes quiet nor counts.
Its stretch is asked for again after that; writes that landed before the
interrupt rely on the dedup gates.

ATTENDED. Claude Code sets `CLAUDE_CODE_SESSION_ATTENDED` to "1" in the
hooks of an interactive session and "0" for `claude -p` and SDK runs. The
hook client forwards `attended` only when it reads exactly "1", and this
reads only the payload, never the daemon's own environment, so a scripted
session, an older host and a daemon started from an attended shell all
read as unattended. Unattended or switched off, a Stop does exactly what
it did before capture existed.

WHEN. One request per Stop, none while `stop_hook_active` is set
(whoever set it: other hooks continue turns too, which is why only a
batch the log shows open for the session is settled), at most
`[capture] daily_cap` per store per UTC day, and only once the stretch
since the watermark holds `checkpoint_tokens` of human prompts and
assistant text. The file must first have grown by that many tokens'
bytes, so nothing is parsed until a checkpoint could be due.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import capture as cap
from .models import utcnow

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

# A token is about four characters of English text; the checkpoint is
# configured in tokens and measured in characters.
CHARS_PER_TOKEN = 4
# After an interrupted capture the session is not asked again for this long.
QUIET_AFTER_INTERRUPT = timedelta(hours=1)
# The payload field the hook client sets, to exactly this, when Claude Code
# marked the session attended.
ATTENDED = "1"

# What a Stop did about capture, by the name the reply carries.
OPENED = "opened"
SETTLED = "settled"
INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class StopStep:
    """What the capture half did at one Stop. `instruction` is what to hand
    the model; `action` SETTLED means the Stop ended a capture turn, which
    the turn audit leaves out."""

    action: str
    batch: str
    instruction: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "batch": self.batch}


@dataclass
class Stretch:
    """The conversation past the watermark, as a checkpoint weighs it."""

    chars: int = 0
    since: str | None = None
    prompt_times: list[str] = field(default_factory=list)
    compacted: bool = False


def hook_output(instruction: str) -> str:
    """The JSON a Stop hook prints for Claude Code to continue the turn
    with `instruction` as context the user does not see."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": instruction,
            }
        }
    )


def _session_transcript(session_id: object, transcript: object) -> Path | None:
    """The transcript a payload names, when it is safe to act on: a regular
    file named for its session, as Claude Code names them."""
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        path = Path(transcript).expanduser().resolve()
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
    except OSError:
        return None
    return path if path.stem == session_id else None


def read_stretch(path: Path, offset: int) -> Stretch:
    """The characters of human prompts and assistant text past `offset`,
    the UTC times of the prompts, where the stretch starts, and whether a
    compact_boundary row lies in it."""
    from .hook import _flatten_assistant_content, human_prompt

    out = Stretch()
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    for line in data.splitlines():
        # A tool result is never the human's text nor the assistant's, and
        # tool results are most of a transcript's bytes: not parsed.
        if not line.strip() or b'"tool_result"' in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        stamp = row.get("timestamp")
        if out.since is None and isinstance(stamp, str):
            out.since = stamp
        if "compact_boundary" in (row.get("type"), row.get("subtype")):
            out.compacted = True
            continue
        if row.get("type") == "assistant":
            message = row.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            out.chars += len(_flatten_assistant_content(content) or "")
            continue
        prompt = human_prompt(row)
        if prompt is not None:
            out.chars += len(prompt)
            if isinstance(stamp, str):
                out.prompt_times.append(stamp)
    return out


def _close_expired(store: Store, now: datetime) -> None:
    closed = cap.closes(store)
    for batch in cap.batches(store):
        if batch.batch in closed or not batch.expired(now):
            continue
        try:
            cap.close_batch(
                store,
                batch,
                outcome=cap.OUTCOME_EXPIRED,
                watermark=batch.offset,
                transcript_bytes=batch.transcript_bytes,
                now=now,
            )
        except cap.BatchClosedError:
            continue


def _project_scopes(store: Store, cwd: str | None) -> list[str]:
    """The `projects:` scope whose memories were written nearest above
    `cwd` (`scope_match.collect_project_roots`), as a one-item list."""
    if not cwd:
        return []
    from .scope_match import collect_project_roots

    roots = collect_project_roots(store.load_all())
    here = os.path.normpath(cwd)
    under = [
        scope
        for scope, root in roots.items()
        if here == os.path.normpath(root)
        or here.startswith(os.path.normpath(root).rstrip(os.sep) + os.sep)
    ]
    return [max(under, key=lambda s: len(roots[s]))] if under else []


def on_stop(
    config: Config,
    store: Store,
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> StopStep | None:
    """The capture half of one Stop: settle, interrupt or open a batch, or
    nothing (None)."""
    if not config.capture.stop_checkpoint or payload.get("attended") != ATTENDED:
        return None
    if payload.get("dry_run"):
        return None
    session_id = payload.get("session_id")
    path = _session_transcript(session_id, payload.get("transcript_path"))
    if path is None or not isinstance(session_id, str):
        return None
    when = now or utcnow()
    _close_expired(store, when)
    size = path.stat().st_size
    mine = cap.open_batch_for_session(store, session_id, now=when)
    if payload.get("stop_hook_active") is True:
        if mine is None:
            return None
        try:
            cap.close_batch(
                store,
                mine,
                outcome=cap.OUTCOME_SETTLED,
                watermark=size,
                transcript_bytes=size,
                now=when,
            )
        except cap.BatchClosedError:
            return None
        return StopStep(SETTLED, mine.batch)
    if mine is not None:
        try:
            cap.close_batch(
                store,
                mine,
                outcome=cap.OUTCOME_INTERRUPTED,
                watermark=mine.offset,
                transcript_bytes=size,
                now=when,
            )
        except cap.BatchClosedError:
            return None
        return StopStep(INTERRUPTED, mine.batch)
    last = cap.last_close(store, session_id)
    if (
        last is not None
        and last.outcome == cap.OUTCOME_INTERRUPTED
        and when - last.closed_at < QUIET_AFTER_INTERRUPT
    ):
        return None
    if cap.daily_count(store, now=when) >= config.capture.daily_cap:
        return None
    offset = cap.watermark(store, session_id)
    if offset > size:
        # The transcript was rewritten under the watermark: start over.
        offset = 0
    from .config import MIN_CHECKPOINT_TOKENS

    tokens = max(config.capture.checkpoint_tokens, MIN_CHECKPOINT_TOKENS)
    threshold = tokens * CHARS_PER_TOKEN
    if size - offset < threshold:
        return None
    stretch = read_stretch(path, offset)
    if stretch.chars < threshold:
        return None
    from .hook import _origin_for

    raw_cwd = payload.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
    origin = _origin_for(Path(cwd)) if cwd else None
    # Everything that can fail runs before the batch opens: a batch whose
    # instruction was never built would read as interrupted at the next Stop.
    token = cap.mint_token()
    instruction = cap.in_session_instruction(
        token=token,
        since=stretch.since if offset else None,
        scopes=_project_scopes(store, cwd),
        prompt_times=stretch.prompt_times,
        compacted=stretch.compacted,
    )
    try:
        batch = cap.open_batch(
            store,
            session_id=session_id,
            transcript_bytes=size,
            offset=offset,
            transcript_path=str(path),
            worktree_root=origin.worktree_root if origin else None,
            mode=(
                cap.MODE_CHECKPOINT_COMPACTED
                if stretch.compacted
                else cap.MODE_CHECKPOINT
            ),
            token=token,
            now=when,
        )
    except cap.BatchAlreadyOpenError:
        return None
    return StopStep(OPENED, batch.batch, instruction)


def close_failed(store: Store, batch_id: str, *, now: datetime | None = None) -> None:
    """Close an opened batch whose instruction never reached the model
    (the reply carrying it failed) as failed: nothing is captured, no quiet
    hour starts and no `daily_cap` slot is spent. A no-op once closed."""
    batch = next((b for b in cap.batches(store) if b.batch == batch_id), None)
    if batch is None:
        return
    try:
        cap.close_batch(
            store,
            batch,
            outcome=cap.OUTCOME_FAILED,
            watermark=batch.offset,
            transcript_bytes=batch.transcript_bytes,
            now=now,
        )
    except cap.BatchClosedError:
        return


__all__ = [
    "ATTENDED",
    "CHARS_PER_TOKEN",
    "INTERRUPTED",
    "OPENED",
    "QUIET_AFTER_INTERRUPT",
    "SETTLED",
    "StopStep",
    "Stretch",
    "close_failed",
    "hook_output",
    "on_stop",
    "read_stretch",
]
