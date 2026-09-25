"""Session capture from the Claude Code hooks: when to capture, and the
background process that does it.

`capture` distils a transcript into memories; this module decides when,
under `[capture] enabled`. A hook never captures by itself. It decides,
starts `bettermemory capture` as a detached background process, and
returns: a model call takes seconds to minutes, and the hooks share
budgets of seconds (`SessionEnd` has 1.5 s in all).

WHEN A SESSION IS CAPTURED. Claude Code's end of a session is only one of
the moments, because it is not a reliable one: a crash or a killed
process fires nothing, and a session left open for days (the desktop app,
a long `/loop`) may never end at all. So there are three, and the
watermark makes any overlap between them harmless (each run reads on from
where the last one stopped):

- Checkpoint, from the Stop hook (`audit-turn`) at every turn end: once
  the uncaptured conversation passes `checkpoint_tokens`, capture every
  whole segment of it and hold the newest back for later, since the
  session is still adding to it.
- End, from the SessionEnd hook (`session-end`): capture the rest.
- Sweep, from the SessionStart hook (`session-start`): capture the rest
  of sessions whose transcript has sat unchanged for `idle_minutes` and
  still holds something uncaptured. This is what catches a crash, a
  closed laptop, or a session that was simply left open.

Only sessions the hooks have seen are candidates: the Stop hook registers
each one by creating its watermark. Turning capture on therefore never
sweeps up the sessions that came before it.

FAILURE. A model call that fails is retried by the next capture of the
session, but the hooks wait out a backoff first (`Watermark.retry_after`:
an hour, doubling to a day), and after `MAX_FAILURES` in a row they stop
trying. A broken login or an exhausted budget costs one call per backoff
rather than one per turn. `bettermemory capture` run by hand ignores the
backoff, and a success clears it.

The background processes write to `captures/capture.log`, which is kept
under `LOG_MAX_BYTES` and is host-local like the rest of `captures/`.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ._fsutil import ensure_owner_only_dir
from .capture import (
    _SESSION_ID_RE,
    CAPTURES_DIR,
    CHILD_ENV,
    SEGMENT_MAX_CHARS,
    WATERMARK_FILENAME,
    Watermark,
    build_segments,
    read_transcript,
    session_busy,
    watermark_path,
)
from .models import utcnow

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("bettermemory.capture_hook")

# A token is about four characters of English text; the checkpoint
# threshold is configured in tokens and measured in rendered characters.
CHARS_PER_TOKEN = 4
# Consecutive failed captures of one session after which the hooks give
# up on it: 1 + 2 + 4 + 8 + 16 hours of backoff, about a day and a half.
MAX_FAILURES = 6
# Sessions one sweep captures. The rest wait for the next session start.
MAX_SWEEP_SESSIONS = 5
LOG_FILENAME = "capture.log"
LOG_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Pending:
    """A session a sweep will capture."""

    session_id: str
    transcript: Path
    mtime: float


def _session_transcript(session_id: object, transcript: object) -> Path | None:
    """The transcript a hook payload names, when it is safe to act on: a
    regular file named for its session, as Claude Code names them. Any
    other payload is ignored, as `capture_transcript` would refuse it."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    if not isinstance(transcript, str | os.PathLike) or not str(transcript):
        return None
    path = Path(transcript).expanduser()
    try:
        path = path.resolve()
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
    except OSError:
        return None
    return path if path.stem == session_id else None


def _gave_up_or_waiting(mark: Watermark, now: datetime) -> bool:
    if mark.failures >= MAX_FAILURES:
        return True
    retry = mark.retry_after()
    return retry is not None and now < retry


def register(root: Path, session_id: str, transcript: Path) -> None:
    """Make `session_id` a candidate for capture by creating its
    watermark, at offset 0. Exclusive create: a watermark a capture
    already wrote is never replaced by this empty one."""
    mark_path = watermark_path(root, session_id)
    if mark_path.exists():
        return
    ensure_owner_only_dir(mark_path.parent, parents=True)
    try:
        fd = os.open(mark_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as fh:
        fh.write(Watermark(session_id=session_id, transcript=str(transcript)).to_json())


def checkpoint_due(
    root: Path,
    session_id: str,
    transcript: Path,
    *,
    threshold_chars: int,
    now: datetime | None = None,
) -> bool:
    """Whether an open session's uncaptured conversation has passed
    `threshold_chars` and holds more than one segment (a checkpoint
    holds the newest back, so one segment alone would capture nothing).

    Most turns answer this from two `stat`s: rendered text is shorter
    than the JSON lines it comes from, so while the transcript has grown
    by less than the threshold since the watermark, nothing is due and
    nothing is parsed."""
    mark = Watermark.load(watermark_path(root, session_id), session_id)
    if _gave_up_or_waiting(mark, now or utcnow()):
        return False
    try:
        size = os.stat(transcript).st_size
    except OSError:
        return False
    if size - mark.offset < threshold_chars:
        return False
    if session_busy(root, session_id):
        return False
    read = read_transcript(transcript, offset=mark.offset)
    rendered = sum(len(t.text) + 48 for t in read.turns)
    if rendered < threshold_chars:
        return False
    return len(build_segments(read, max_chars=SEGMENT_MAX_CHARS)) > 1


def pending_sessions(
    root: Path,
    *,
    idle_seconds: float,
    now: datetime | None = None,
    limit: int = MAX_SWEEP_SESSIONS,
) -> list[Pending]:
    """Registered sessions a sweep should capture, newest first: the
    transcript still exists, has not changed for `idle_seconds`, has
    changed since a capture last read it to the end, and the session is
    neither backing off after failures nor being captured right now."""
    base = root / CAPTURES_DIR
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    when = now or utcnow()
    wall = when.timestamp()
    found: list[Pending] = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or not _SESSION_ID_RE.match(
            entry.name
        ):
            continue
        mark_path = Path(entry.path) / WATERMARK_FILENAME
        if not mark_path.is_file():
            continue
        mark = Watermark.load(mark_path, entry.name)
        if not mark.transcript or _gave_up_or_waiting(mark, when):
            continue
        transcript = Path(mark.transcript)
        try:
            st = os.stat(transcript)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or wall - st.st_mtime < idle_seconds:
            continue
        if mark.settled_size == st.st_size:
            continue
        found.append(Pending(entry.name, transcript, st.st_mtime))
    found.sort(key=lambda p: p.mtime, reverse=True)
    picked: list[Pending] = []
    for pending in found:
        if len(picked) >= limit:
            break
        if not session_busy(root, pending.session_id):
            picked.append(pending)
    return picked


def _trim_log(path: Path) -> None:
    """Keep the log's newest half once it passes `LOG_MAX_BYTES`."""
    try:
        if path.stat().st_size <= LOG_MAX_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(-(LOG_MAX_BYTES // 2), os.SEEK_END)
            tail = fh.read()
        cut = tail.find(b"\n")
        with open(path, "wb") as fh:
            fh.write(tail[cut + 1 :] if cut >= 0 else tail)
    except OSError:
        pass


def spawn_capture(root: Path, args: Sequence[str]) -> None:
    """Start `bettermemory capture <args>` in the background and return.

    The process gets its own session (process group), so it outlives the
    hook that started it even when Claude Code ends the hook's process
    group on exit; its output goes to `captures/capture.log`. It runs
    from the system temp directory: nothing it does reads the current
    directory, and a checkout it started in could be deleted under it.
    """
    log_path = root / CAPTURES_DIR / LOG_FILENAME
    ensure_owner_only_dir(log_path.parent, parents=True)
    _trim_log(log_path)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        os.write(fd, f"\n[{stamp}] capture {' '.join(args)}\n".encode())
        argv = [sys.executable, "-m", "bettermemory", "capture", *args]
        if sys.platform == "win32":  # pragma: no cover - non-unix in CI
            subprocess.Popen(  # noqa: S603 — our own interpreter and module
                argv,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                cwd=tempfile.gettempdir(),
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(  # noqa: S603 — our own interpreter and module
                argv,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                cwd=tempfile.gettempdir(),
                close_fds=True,
                start_new_session=True,
            )
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------


def _active(config: Config) -> bool:
    return config.capture.enabled and not os.environ.get(CHILD_ENV)


def on_stop(config: Config, session_id: object, transcript: object) -> bool:
    """Stop hook: register the session, and start a checkpoint capture
    when one is due. True when a capture was started."""
    if not _active(config):
        return False
    path = _session_transcript(session_id, transcript)
    if path is None:
        return False
    root = config.resolved_directory()
    sid = path.stem
    register(root, sid, path)
    threshold = config.capture.checkpoint_tokens * CHARS_PER_TOKEN
    if not checkpoint_due(root, sid, path, threshold_chars=threshold):
        return False
    spawn_capture(
        root, ["--transcript", str(path), "--session-id", sid, "--checkpoint"]
    )
    return True


def on_session_end(config: Config, session_id: object, transcript: object) -> bool:
    """SessionEnd hook: capture the rest of the session, unless it is
    backing off after failures (the sweep takes it up once that ends).
    The capture waits for one already running on the session. True when
    a capture was started."""
    if not _active(config):
        return False
    path = _session_transcript(session_id, transcript)
    if path is None:
        return False
    root = config.resolved_directory()
    sid = path.stem
    register(root, sid, path)
    mark = Watermark.load(watermark_path(root, sid), sid)
    if _gave_up_or_waiting(mark, utcnow()):
        return False
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    if mark.settled_size == size:
        return False
    spawn_capture(root, ["--transcript", str(path), "--session-id", sid])
    return True


def on_session_start(config: Config) -> bool:
    """SessionStart hook: start one background sweep when any session is
    waiting for it. True when a sweep was started."""
    if not _active(config):
        return False
    root = config.resolved_directory()
    if not pending_sessions(root, idle_seconds=config.capture.idle_minutes * 60):
        return False
    spawn_capture(root, ["--pending"])
    return True
