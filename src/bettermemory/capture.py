"""Session capture, the pipeline: a Claude Code transcript in, dated
memories written through `memory_write`'s gates out.

`session_capture` owns the prompt, the output contract and the validator
as pure functions. This module owns everything with a side effect: it
reads the transcript from where the last capture of that session stopped,
cuts it into segments, asks a model for memories, and writes each
survivor through the same gate chain and persist step `memory_write`
uses. `bettermemory capture` is its command line.

WHAT A SESSION LEAVES BEHIND
- memories in the store, tagged with the `session-capture` scope and
  carrying an `actor` whose client is `bettermemory-capture` and whose
  session is the transcript's session id, so a bad run can be reviewed
  as a set and undone with `bettermemory rollback --by-actor`;
- `captures/<session>/`, host-local like `episodes/` and never synced:
  one file per captured segment holding exactly the text the model saw
  (redacted), and `watermark.json`, which records how far the transcript
  has been read and what each segment produced.

READING. The transcript is JSONL written by Claude Code. Only complete
lines are consumed, from the watermark's byte offset, so a capture that
runs while the session is still writing picks up the rest next time. What
reaches the prompt is the human's own messages and the assistant's text,
with each assistant turn's tool calls reduced to a digest line (which
files were edited, which commands ran, what was saved to memory).
Thinking blocks, tool results, sidechains, skill expansions and harness
envelopes (task notifications, system reminders) are dropped: the first
two are most of a transcript's bytes and none of its facts about the
user, and the rest is text the user never wrote. Secrets are redacted and
`<private>...</private>` spans removed before anything is rendered, so
neither reaches the model or the segment file.

WRITING. Every memory the validator keeps is judged by
`handlers.write.CAPTURE_GATES` against the live store: credential,
transient phrasing, the user-claim label, scope mismatch, groundedness
against the segment it came from, and dedup against the active set and
against tombstones. Two refusals have a remedy capture can apply itself,
once each: a `user_claim_warning` re-files the memory as `user-inference`,
and a `scope_mismatch` adopts the suggested scopes. A duplicate credits
the matched memory with a corroboration, which is what a claim
re-entering a conversation is. Every other refusal drops the memory and
is counted in the report. A memory that passes every gate but restates
what one stored memory already says (`_covering_memory`) is dropped as
`covered`: sessions where the model saved its own memories as it went
otherwise yield mostly restatements. A plan with no date ahead of the
conversation is dropped as `open_plan` before any gate runs: it is the
session's to-do list, which belongs in its journal, not in memory.
Under `require_write_confirmation` nothing is committed: survivors go on
the proposal queue, and accepting one is the confirmation.

The prompt is `session_capture`'s, with `WORK_SESSION_RULES` added, since
a Claude Code session is a working session and the base prompt was
written for conversations.

IDEMPOTENCE. A segment is identified by the hash of its rendered text.
The watermark records each processed segment and the offset after it,
under a per-session lock, and advances only after the segment's writes
land. A failed model call stops the run with the watermark before that
segment, so the next run retries it; a segment the fence check refuses
is recorded as refused and never retried.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import identity
from . import session_capture as sc
from ._fsutil import atomic_write_bytes, ensure_owner_only_dir, flock_excl
from .credentials import _redact_all
from .handlers._shared import _validate_write_payload
from .handlers.write import (
    CAPTURE_GATES,
    GateBundle,
    GateContext,
    Reject,
    _persist,
    apply_write_gates,
)
from .hook import _SYNTHETIC_USER_PREFIXES
from .models import Category, Memory, generate_ulid, utcnow
from .origin import Origin
from .origin import capture as capture_origin
from .proposals import Proposal, ProposalQueue
from .search import _content_token_set
from .time_utils import parse_event_ts

if TYPE_CHECKING:
    from .config import Config
    from .events import Recorder
    from .store import MemoryStore

log = logging.getLogger("bettermemory.capture")

CAPTURES_DIR = "captures"
WATERMARK_FILENAME = "watermark.json"
CAPTURE_SCOPE = "session-capture"
TRIGGER = "session_capture"
# The `actor.client` every captured memory declares. Its own name rather
# than `claude-code`, which the session's model declares on the memories
# it saves itself: `bettermemory rollback --by-actor bettermemory-capture`
# then removes exactly what capture wrote, and the `client` filter on
# `memory_search` and `memory_list` can include or leave it out.
CAPTURE_CLIENT = "bettermemory-capture"
# Set in the environment of the `claude -p` child a capture spawns, so a
# bettermemory process started inside it (a hook, a nested capture)
# knows it is running on behalf of a capture and does nothing.
CHILD_ENV = "BETTERMEMORY_CAPTURE_CHILD"
# Variables Claude Code sets for the processes of one live session: its
# ids, its entrypoint, the socket and token its host talks to it on. A
# capture started from a hook inherits them, and a `claude -p` child that
# saw them would take itself for part of that session (or refuse to run
# nested in it). They are removed from the child's environment; the ones
# that choose a login or a provider (`CLAUDE_CODE_OAUTH_TOKEN`,
# `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CONFIG_DIR`, ...) are kept.
_SESSION_ENV = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_HOST_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    }
)

# About 12k tokens of rendered conversation per model call. A segment
# closes at a user-turn boundary before it would pass this.
SEGMENT_MAX_CHARS = 48_000
# Segments processed per run. The watermark stops after the last one, so
# a longer transcript continues on the next run rather than costing more
# in this one.
DEFAULT_MAX_SEGMENTS = 4
# The one place a user's own words are cut: a pasted log or document
# past this length is capped so a single message cannot fill a segment.
USER_TURN_CHARS = 12_000
# Room kept for the tool digest inside `session_capture`'s per-turn
# assistant cap, so the render's cut falls on the prose, not the digest.
DIGEST_MAX_CHARS = 600
DIGEST_LINE_CHARS = 160
# Bytes read from the transcript per run. A session larger than this is
# captured across runs, since the watermark only ever moves forward.
READ_MAX_BYTES = 64 * 1024 * 1024
# A captured memory is already stored when this share of its content
# words, and every number or identifier in it, sit in one stored memory
# (`_covering_memory`). Measured 2026-09-24 on 44 memories two real
# sessions yielded against a 443-memory store: the twelve at or above
# 0.85 were each restated from a stored memory, while matches below 0.8
# included a sprawling state record that contains a little of
# everything. Short facts have few words to judge by, so under
# COVERED_MIN_TOKENS the check does not apply.
COVERED_SHARE = 0.85
COVERED_MIN_TOKENS = 6

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)
_EDIT_TOOLS = {"Edit": "edited", "MultiEdit": "edited", "NotebookEdit": "edited"}
_INTERRUPT_PREFIX = "[Request interrupted"
_BODY_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# Shell commands that change something outside the working tree's reads:
# a commit, a push, a release, an install. They outrank the reads (`ls`,
# `grep`, `git log`) that make up most of a session's shell calls, so the
# digest's budget goes to what happened rather than to how it was found.
_STATE_CHANGING_RE = re.compile(
    r"\b(?:git\s+(?:commit|push|tag|merge|rebase|cherry-pick|revert|reset)"
    r"|gh\s+(?:pr|release|issue)\s+(?:create|merge|close|edit)"
    r"|(?:pip|uv|npm|pnpm|yarn|brew|cargo)\s+(?:tool\s+)?(?:install|upgrade|add|publish)"
    r"|deploy|terraform\s+apply|kubectl\s+apply)\b"
)


class CaptureError(RuntimeError):
    """A capture that cannot run at all: a bad session id, an unreadable
    transcript, a transcript that belongs to another session, or another
    capture of the same session holding the lock."""


class CaptureBusy(CaptureError):
    """Another capture of the same session holds its lock, and this one
    was asked not to wait (`capture_transcript(wait=False)`)."""


class CaptureModelError(RuntimeError):
    """The model call for one segment failed. The run stops there and the
    watermark stays before the segment, so the next run retries it."""


# ---------------------------------------------------------------------------
# Reading the transcript
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptTurn:
    """One turn as capture reads it: `session_capture.Turn` plus the byte
    offset just past the last transcript line that contributed to it."""

    role: str
    text: str
    ts_ms: int | None
    end_offset: int

    def to_turn(self) -> sc.Turn:
        return sc.Turn(role=self.role, text=self.text, ts_ms=self.ts_ms)


@dataclass(frozen=True)
class TranscriptRead:
    turns: list[TranscriptTurn]
    start_offset: int
    end_offset: int
    cwd: str | None


def scrub(text: str) -> str:
    """Remove `<private>` spans and redact secret-shaped tokens. Applied
    to every turn before it is rendered, so what the model sees and what
    the segment file keeps are the same, and neither holds a secret the
    credential detectors know the shape of."""
    return _redact_all(_PRIVATE_RE.sub("[private]", text))


def _ts_ms(row: dict[str, Any]) -> int | None:
    raw = row.get("timestamp")
    if not isinstance(raw, str):
        return None
    parsed = parse_event_ts(raw)
    return int(parsed.timestamp() * 1000) if parsed is not None else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _digest_line(block: dict[str, Any]) -> tuple[int, str] | None:
    """`(rank, line)` for a tool call worth remembering, or None. Lower
    ranks win the digest's budget: a memory_write names the body it saved
    (so the model does not propose it again), an edit or write names the
    file, a state-changing shell call its first line, any other shell
    call its first line last."""
    name = block.get("name")
    args = block.get("input")
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    if name.endswith("memory_write"):
        content = args.get("content")
        if isinstance(content, str) and content.strip():
            return 0, _clip(f"[saved to memory: {content}]", DIGEST_LINE_CHARS + 80)
        return None
    if name in _EDIT_TOOLS or name == "Write":
        path = args.get("file_path") or args.get("notebook_path")
        if isinstance(path, str) and path:
            verb = "wrote" if name == "Write" else _EDIT_TOOLS[name]
            return 1, _clip(f"[{verb} {path}]", DIGEST_LINE_CHARS)
        return None
    if name == "Bash":
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            first = command.strip().splitlines()[0]
            rank = 2 if _STATE_CHANGING_RE.search(command) else 3
            return rank, _clip(f"[ran: {first}]", DIGEST_LINE_CHARS)
    return None


def _user_text(row: dict[str, Any]) -> str | None:
    """The human's words in a user row, or None for everything else a
    user row carries (tool results, skill expansions, harness envelopes,
    interrupt markers)."""
    if row.get("isMeta"):
        return None
    content = row.get("message", {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_SYNTHETIC_USER_PREFIXES):
        return None
    if text.startswith(_INTERRUPT_PREFIX):
        return None
    return text


def _assistant_parts(
    row: dict[str, Any],
) -> tuple[list[str], list[tuple[int, str]]]:
    content = row.get("message", {}).get("content")
    texts: list[str] = []
    digest: list[tuple[int, str]] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                line = _digest_line(block)
                if line is not None:
                    digest.append(line)
    return texts, digest


def _assistant_text(texts: list[str], digest: list[tuple[int, str]]) -> str:
    """Prose first, then the digest, deduplicated and bounded, with the
    prose trimmed so `session_capture`'s per-turn cap never cuts the
    digest off. The digest keeps its lines in call order but admits them
    by rank, so a budget spent is spent on the edits and releases."""
    chosen: set[int] = set()
    seen: set[str] = set()
    used = 0
    for index, (_rank, line) in sorted(enumerate(digest), key=lambda e: e[1][0]):
        if line in seen or used + len(line) > DIGEST_MAX_CHARS:
            continue
        seen.add(line)
        chosen.add(index)
        used += len(line) + 1
    lines = [line for index, (_rank, line) in enumerate(digest) if index in chosen]
    prose = "\n\n".join(t.strip() for t in texts if t.strip())
    tail = "\n".join(lines)
    room = sc.ASSISTANT_TURN_CHARS - len(tail) - 8
    if len(prose) > room:
        prose = prose[: max(room, 0)].rstrip() + " [...]"
    return f"{prose}\n{tail}".strip() if tail else prose


def _check_regular_file(path: Path) -> None:
    # `os.stat` rather than `Path.is_file()`, and before any open, for the
    # reasons `consolidate._load_transcript` gives: a FIFO would block the
    # open, and absent / unreadable / not-a-file must read differently.
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError as exc:
        raise CaptureError(f"transcript {path} does not exist") from exc
    except OSError as exc:
        raise CaptureError(f"transcript {path} could not be read: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise CaptureError(f"transcript {path} is not a regular file")


def read_transcript(
    path: Path, *, offset: int = 0, max_bytes: int = READ_MAX_BYTES
) -> TranscriptRead:
    """The conversation in `path` from byte `offset`, as turns.

    Consecutive user messages merge into one turn. An assistant turn is
    one stretch of prose plus the tool calls that follow it, up to the
    next prose or the next user message: an autonomous run of an hour is
    many turns, each stamped with its own time, not one turn cut down to
    its opening lines, so what the run ended with (its results) reaches
    the model. Only complete lines are consumed; `end_offset` is the byte
    after the last one. A transcript shorter than `offset` (it
    was replaced, not appended to) is read from the start: the segment
    hashes in the watermark keep that from writing anything twice.
    """
    _check_regular_file(path)
    size = os.stat(path).st_size
    if offset > size:
        offset = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        raw = fh.read(max_bytes)
    cut = raw.rfind(b"\n")
    if cut < 0:
        return TranscriptRead([], offset, offset, None)
    raw = raw[: cut + 1]

    turns: list[TranscriptTurn] = []
    cwd: str | None = None
    # Pending assistant turn: prose and digest collected across rows.
    a_texts: list[str] = []
    a_digest: list[tuple[int, str]] = []
    a_ts: int | None = None
    a_end = offset

    def flush_assistant() -> None:
        nonlocal a_texts, a_digest, a_ts
        text = _assistant_text(a_texts, a_digest)
        if text:
            turns.append(TranscriptTurn("assistant", scrub(text), a_ts, a_end))
        a_texts, a_digest, a_ts = [], [], None

    position = offset
    # Split on b"\n" only, never splitlines(): Node writes U+2028 raw
    # inside JSON strings (`consolidate._load_transcript` has the story).
    for line in raw.split(b"\n")[:-1]:
        position += len(line) + 1
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        if isinstance(row.get("cwd"), str):
            cwd = row["cwd"]
        kind = row.get("type")
        if not isinstance(row.get("message"), dict):
            continue
        if kind == "user":
            text = _user_text(row)
            if text is None:
                continue
            flush_assistant()
            text = scrub(text)
            if len(text) > USER_TURN_CHARS:
                text = text[:USER_TURN_CHARS].rstrip() + " [...]"
            prev = turns[-1] if turns else None
            if prev is not None and prev.role == "user":
                turns[-1] = TranscriptTurn(
                    "user", f"{prev.text}\n\n{text}", prev.ts_ms, position
                )
            else:
                turns.append(TranscriptTurn("user", text, _ts_ms(row), position))
        elif kind == "assistant":
            texts, digest = _assistant_parts(row)
            if not texts and not digest:
                continue
            if texts and a_texts:
                flush_assistant()
            if a_ts is None:
                a_ts = _ts_ms(row)
            a_texts.extend(texts)
            a_digest.extend(digest)
            a_end = position
    flush_assistant()
    return TranscriptRead(turns, offset, offset + len(raw), cwd)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """Turns sent to the model in one call, with the byte range they came
    from and the hash that identifies them across runs."""

    turns: tuple[sc.Turn, ...]
    start_offset: int
    end_offset: int
    text: str
    sha: str


def _segment(turns: Sequence[TranscriptTurn], start_offset: int) -> Segment:
    plain = tuple(t.to_turn() for t in turns)
    text = sc.render_turns(plain)
    h = hashlib.sha256()
    for t in plain:
        h.update(f"{t.role}\x00{t.ts_ms}\x00{t.text}\x01".encode())
    return Segment(plain, start_offset, turns[-1].end_offset, text, h.hexdigest())


def build_segments(
    read: TranscriptRead, *, max_chars: int = SEGMENT_MAX_CHARS
) -> list[Segment]:
    """Cut the turns into segments of about `max_chars` of rendered text.

    A segment closes at a user turn, so a question and its answer travel
    together. One exchange can outgrow that on its own (an hour of
    autonomous work answering one prompt), so past half again the budget
    a segment also closes between assistant turns. A trailing user turn
    with no reply yet is left for the next run, when its answer will be
    there too."""
    turns = list(read.turns)
    if turns and turns[-1].role == "user":
        turns.pop()
    segments: list[Segment] = []
    current: list[TranscriptTurn] = []
    size = 0
    start = read.start_offset
    hard_max = max_chars + max_chars // 2
    for turn in turns:
        cost = len(turn.text) + 48
        limit = max_chars if turn.role == "user" else hard_max
        if current and size + cost > limit:
            segment = _segment(current, start)
            segments.append(segment)
            start = segment.end_offset
            current, size = [], 0
        current.append(turn)
        size += cost
    if current:
        segments.append(_segment(current, start))
    return segments


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelReply:
    text: str
    cost_usd: float | None = None


class CaptureModel(Protocol):
    """Anything that answers `session_capture`'s messages with text."""

    name: str

    def complete(self, messages: list[dict[str, str]]) -> ModelReply: ...


def _split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    return system, user


@dataclass
class ClaudeCliModel:
    """Claude Code in print mode, on the user's own login: no API key, no
    SDK. The child gets the capture prompt as its whole system prompt, no
    tools, no MCP servers, no hooks and no saved session, and runs in the
    system temp directory so no project's CLAUDE.md is read. It still
    reads the user-level CLAUDE.md (only `--bare` skips that, and `--bare`
    refuses a subscription login); with no tools it can act on none of it.
    """

    name: str = "claude-cli"
    model: str = "haiku"
    binary: str | None = None
    max_budget_usd: float = 0.25
    timeout_s: float = 300.0
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def complete(self, messages: list[dict[str, str]]) -> ModelReply:
        binary = self.binary or shutil.which("claude")
        if binary is None:
            raise CaptureModelError("the `claude` command is not on PATH")
        system, user = _split_messages(messages)
        argv = [
            binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(sc.CAPTURE_SCHEMA),
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-budget-usd",
            f"{self.max_budget_usd:.2f}",
            "--settings",
            json.dumps({"disableAllHooks": True, "autoMemoryEnabled": False}),
        ]
        try:
            done = self.runner(
                argv,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_s,
                cwd=tempfile.gettempdir(),
                env=child_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureModelError(f"claude -p did not complete: {exc}") from exc
        try:
            result = json.loads(done.stdout)
        except ValueError as exc:
            detail = (done.stderr or done.stdout or "").strip()[:300]
            raise CaptureModelError(
                f"claude -p exited {done.returncode} without a JSON result: {detail}"
            ) from exc
        if not isinstance(result, dict):
            raise CaptureModelError("claude -p returned JSON that is not an object")
        subtype = result.get("subtype")
        if result.get("is_error") or subtype != "success":
            # A refused login comes back as subtype "success" with
            # is_error set; naming that subtype would read as nonsense.
            message = str(result.get("result") or "").strip()[:300]
            kind = f" ({subtype})" if subtype != "success" else ""
            raise CaptureModelError(f"claude -p failed{kind}: {message}")
        cost = result.get("total_cost_usd")
        structured = result.get("structured_output")
        text = (
            json.dumps(structured)
            if isinstance(structured, dict)
            else str(result.get("result") or "")
        )
        return ModelReply(
            text=text, cost_usd=float(cost) if isinstance(cost, int | float) else None
        )


@dataclass
class AnthropicApiModel:
    """The Messages API with `ANTHROPIC_API_KEY`, through the `anthropic`
    SDK when it is installed. Same bounded-call rules as
    `llm.AnthropicProvider`: no SDK retries stacking the timeout, and a
    truncated reply raises rather than parsing as empty."""

    name: str = "anthropic"
    model: str = "claude-haiku-4-5"
    api_key: str | None = None
    max_tokens: int = 4096
    timeout_s: float = 120.0

    def complete(self, messages: list[dict[str, str]]) -> ModelReply:
        try:
            import anthropic  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise CaptureModelError(
                "the anthropic provider needs the `anthropic` SDK installed"
            ) from exc
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise CaptureModelError("the anthropic provider needs ANTHROPIC_API_KEY")
        system, user = _split_messages(messages)
        client = anthropic.Anthropic(api_key=key, max_retries=0)
        try:
            msg = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.0,
                system=system,
                messages=[{"role": "user", "content": user}],
                timeout=self.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — every SDK failure is one outcome here
            raise CaptureModelError(f"Messages API call failed: {exc}") from exc
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise CaptureModelError(f"reply truncated at max_tokens={self.max_tokens}")
        text = "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        )
        return ModelReply(text=text)


# The most of a chat-completions reply read into memory. A capture reply
# is at most sixteen short memories; anything near this is not one.
_REPLY_MAX_BYTES = 1024 * 1024


@dataclass
class OpenAICompatibleModel:
    """Any server that speaks OpenAI's chat completions API: OpenAI,
    DeepSeek, OpenRouter, a local Ollama, vLLM or LM Studio. Standard
    library only, so it works in a bare `uvx bettermemory` with no SDK
    installed.

    `base_url` is the API root the server documents (the part before
    `/chat/completions`), `api_key_env` the name of the environment
    variable holding the key: the key itself never goes in the config
    file. An unset or empty variable sends no Authorization header,
    which is what a local server expects. JSON mode is asked for, and
    asked for once more without it when a server refuses the option.
    """

    name: str = "openai"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    max_tokens: int = 4096
    timeout_s: float = 120.0
    opener: Callable[..., Any] = urllib.request.urlopen

    def complete(self, messages: list[dict[str, str]]) -> ModelReply:
        if not self.model:
            raise CaptureModelError(
                "the openai provider needs a model: set [capture] model or --model"
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            payload = self._post(body)
        except _Refused as exc:
            if "response_format" not in exc.detail:
                raise CaptureModelError(str(exc)) from exc
            del body["response_format"]
            try:
                payload = self._post(body)
            except _Refused as again:
                raise CaptureModelError(str(again)) from again
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise CaptureModelError(f"{self._url()} replied without choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("finish_reason") == "length":
            raise CaptureModelError(f"reply truncated at max_tokens={self.max_tokens}")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise CaptureModelError(f"{self._url()} replied without message content")
        usage = payload.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        return ModelReply(
            text=content,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _post(self, body: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env, "").strip() if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(  # noqa: S310 — the configured endpoint
            self._url(),
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                raw = response.read(_REPLY_MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if exc.code == 400:
                raise _Refused(
                    f"{self._url()} returned HTTP 400: {detail}", detail
                ) from exc
            raise CaptureModelError(
                f"{self._url()} returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CaptureModelError(
                f"{self._url()} could not be reached: {exc}"
            ) from exc
        if len(raw) > _REPLY_MAX_BYTES:
            raise CaptureModelError(f"{self._url()} replied with more than 1 MB")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CaptureModelError(f"{self._url()} replied with no JSON body") from exc


class _Refused(Exception):
    """An HTTP 400, with the server's own words in `detail`."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(600).decode("utf-8", errors="replace").strip()[:300]
    except OSError:
        return ""


def child_env() -> dict[str, str]:
    """This process's environment minus the live session's variables
    (`_SESSION_ENV`), marked as a capture child."""
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
    env[CHILD_ENV] = "1"
    return env


PROVIDERS = ("auto", "claude-cli", "anthropic", "openai")


def resolve_model(
    provider: str = "auto",
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key_env: str | None = None,
) -> CaptureModel:
    """`auto` takes the Anthropic API when a key is set, else Claude
    Code's own login when `claude` is on PATH. `openai` is never chosen
    automatically: it needs a server and a model named for it."""
    if provider not in PROVIDERS:
        raise CaptureError(f"unknown provider {provider!r}; valid: {PROVIDERS}")
    if provider == "auto":
        provider = (
            "anthropic"
            if os.environ.get("ANTHROPIC_API_KEY")
            else "claude-cli"
            if shutil.which("claude")
            else ""
        )
        if not provider:
            raise CaptureError(
                "no model to capture with: set ANTHROPIC_API_KEY or put "
                "Claude Code's `claude` command on PATH"
            )
    model = model or None
    if provider == "openai":
        return OpenAICompatibleModel(
            model=model or "",
            base_url=base_url or OpenAICompatibleModel.base_url,
            api_key_env=api_key_env or OpenAICompatibleModel.api_key_env,
        )
    if provider == "anthropic":
        return AnthropicApiModel(model=model or AnthropicApiModel.model)
    return ClaudeCliModel(model=model or ClaudeCliModel.model)


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------


@dataclass
class Watermark:
    """Per-session capture state, `captures/<session>/watermark.json`.

    `settled_size` is the transcript's size when a capture last read it
    to the end, holding nothing back: until the file grows past it there
    is nothing left for a sweep to do. `failures` counts model calls
    that failed in a row, `last_failure_at` and `last_error` the latest;
    the hooks wait out a backoff after each (`retry_after`) so a broken
    login or an exhausted budget is not retried on every turn. A capture
    that gets through a segment resets all three."""

    session_id: str
    transcript: str | None = None
    offset: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    settled_size: int | None = None
    failures: int = 0
    last_failure_at: str | None = None
    last_error: str | None = None

    def seen(self, sha: str) -> bool:
        return any(s.get("sha") == sha for s in self.segments)

    def retry_after(self) -> datetime | None:
        """When a hook may next try this session after failures: one hour
        after the first, doubling to a day. None when nothing failed."""
        if not self.failures or self.last_failure_at is None:
            return None
        failed = parse_event_ts(self.last_failure_at)
        if failed is None:
            return None
        hours = min(2 ** (self.failures - 1), 24)
        return failed + timedelta(hours=hours)

    def to_json(self) -> bytes:
        return (
            json.dumps(
                {
                    "version": 1,
                    "session_id": self.session_id,
                    "transcript": self.transcript,
                    "offset": self.offset,
                    "segments": self.segments,
                    "settled_size": self.settled_size,
                    "failures": self.failures,
                    "last_failure_at": self.last_failure_at,
                    "last_error": self.last_error,
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def load(cls, path: Path, session_id: str) -> Watermark:
        """The stored watermark, or a fresh one. An unreadable file starts
        over rather than blocking the session for good; the dedup gates
        then keep a re-read from writing the same memories twice."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(session_id=session_id)
        except (OSError, ValueError) as exc:
            log.warning("capture watermark %s unreadable, starting over: %s", path, exc)
            return cls(session_id=session_id)
        if not isinstance(raw, dict) or raw.get("session_id") != session_id:
            return cls(session_id=session_id)
        offset = raw.get("offset")
        segments = raw.get("segments")
        settled = raw.get("settled_size")
        failures = raw.get("failures")
        return cls(
            session_id=session_id,
            transcript=raw.get("transcript")
            if isinstance(raw.get("transcript"), str)
            else None,
            offset=offset if isinstance(offset, int) and offset >= 0 else 0,
            segments=[s for s in segments if isinstance(s, dict)]
            if isinstance(segments, list)
            else [],
            settled_size=settled if isinstance(settled, int) and settled >= 0 else None,
            failures=failures if isinstance(failures, int) and failures > 0 else 0,
            last_failure_at=_opt_str(raw.get("last_failure_at")),
            last_error=_opt_str(raw.get("last_error")),
        )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def capture_dir(root: Path, session_id: str) -> Path:
    return root / CAPTURES_DIR / session_id


def watermark_path(root: Path, session_id: str) -> Path:
    return capture_dir(root, session_id) / WATERMARK_FILENAME


@contextlib.contextmanager
def _session_lock_nowait(mark_path: Path) -> Iterator[None]:
    """`flock_excl`'s lock on the watermark (the same sidecar file), taken
    without waiting: raises `CaptureBusy` while another capture holds it.
    One attempt on each platform, where `flock_excl` would block (POSIX)
    or retry for up to 30 s (Windows): a hook asking whether a session is
    busy must hear back at once."""
    ensure_owner_only_dir(mark_path.parent, parents=True)
    lock_path = mark_path.with_suffix(mark_path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    busy = CaptureBusy(f"another capture of session {mark_path.parent.name} is running")
    try:
        if sys.platform == "win32":  # pragma: no cover - non-unix in CI
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined,unused-ignore]
            except OSError as exc:
                raise busy from exc
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined,unused-ignore]
            return
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise busy from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def session_busy(root: Path, session_id: str) -> bool:
    """Whether a capture of `session_id` is running right now."""
    mark_path = watermark_path(root, session_id)
    if not mark_path.parent.is_dir():
        return False
    try:
        with _session_lock_nowait(mark_path):
            return False
    except CaptureBusy:
        return True


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass
class MemoryOutcome:
    """What happened to one captured memory. `status` is `committed`,
    `proposed`, `would_commit` (dry run), `duplicate`, `covered` (a
    stored memory already says it: `_covering_memory`), `open_plan` (a
    to-do rather than a commitment: `_is_open_plan`), or the refusing
    gate's status (`transient_warning`, `ungrounded`, ...), or `invalid`
    when the payload failed validation."""

    status: str
    body: str
    kind: str
    category: str
    scopes: list[str]
    id: str | None = None
    matched: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "kind": self.kind,
            "category": self.category,
            "scopes": self.scopes,
            "body": self.body,
        }
        for key in ("id", "matched", "reason"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def project_scope(origin: Origin | None) -> str | None:
    """`projects:<checkout name>` for a session inside a git checkout."""
    root = origin.worktree_root if origin is not None else None
    if not root:
        return None
    name = re.sub(r"[^a-z0-9]+", "-", Path(root).name.lower()).strip("-")
    return f"projects:{name}" if name else None


@dataclass
class _Writer:
    bundle: GateBundle
    recorder: Recorder
    session: str
    origin: Origin | None
    base_scopes: list[str]
    dry_run: bool
    corroborated: set[str] = field(default_factory=set)
    # Stored memories' token sets for `_covering_memory`, keyed by id and
    # `updated` so an edit mid-run is re-read: one tokenisation per
    # memory per run instead of one per captured memory.
    tokens: dict[tuple[str, datetime], set[str]] = field(default_factory=dict)

    def write(self, item: sc.Captured, segment: Segment) -> MemoryOutcome:
        config = self.bundle.config
        if _is_open_plan(item, segment):
            return MemoryOutcome(
                status="open_plan",
                body=item.body,
                kind=item.kind,
                category=Category.FACT.value,
                scopes=list(self.base_scopes),
            )
        category = (
            Category.USER_INFERENCE.value
            if item.kind == "preference"
            else Category.FACT.value
        )
        scopes = list(self.base_scopes)
        adopted_scopes = False

        def outcome(status: str, **extra: Any) -> MemoryOutcome:
            return MemoryOutcome(
                status=status,
                body=item.body,
                kind=item.kind,
                category=category,
                scopes=list(scopes),
                **extra,
            )

        while True:
            try:
                payload = _validate_write_payload(
                    content=item.body,
                    scopes=scopes,
                    confidence="medium",
                    source="inferred",
                    category=category,
                    allowed_scopes=config.scopes.allowed,
                    max_content_bytes=config.behavior.max_content_bytes,
                    min_content_tokens=config.behavior.min_content_tokens,
                    max_scopes_per_write=config.behavior.max_scopes_per_write,
                )
            except ValueError as exc:
                return outcome("invalid", reason=str(exc))
            payload["origin"] = self.origin
            payload["actor"] = identity.current_actor()
            gc = GateContext(
                payload=payload,
                force=False,
                acknowledge_transient=False,
                acknowledge_scope_mismatch=False,
                acknowledge_ungrounded=False,
                acknowledge_credential=False,
                groundedness_check=True,
                source_transcript=segment.text,
            )
            decision = _apply_capture_gates(self.bundle, gc)
            if decision is None:
                break
            status = str(decision.response.get("status"))
            if (
                status == "user_claim_warning"
                and category != Category.USER_INFERENCE.value
            ):
                category = Category.USER_INFERENCE.value
                continue
            if status == "scope_mismatch" and not adopted_scopes:
                adopted_scopes = True
                suggested = decision.response.get("suggested_scopes") or []
                extra = [s for s in suggested if isinstance(s, str) and s not in scopes]
                if extra:
                    scopes = scopes + extra
                    continue
            if status == "duplicate":
                return outcome("duplicate", matched=self._corroborate(decision))
            return outcome(status, reason=_reason(decision))

        covering = _covering_memory(item.body, gc.active_snapshot or [], self.tokens)
        if covering is not None:
            return outcome("covered", matched=covering)

        if self.dry_run:
            return outcome("would_commit")
        if config.behavior.require_write_confirmation:
            return outcome("proposed", id=self._propose(item, category))
        # The persist step `memory_write` commits through (supersession
        # links, cue-less disagreements filed for memory_conflicts), then
        # capture's own event rather than `write`: see
        # `eval._KNOWN_SIDE_EFFECT_KINDS` on why the kinds are kept apart.
        memory, supersession = _persist(
            self.bundle, payload, active_snapshot=gc.active_snapshot
        )
        self.recorder.record(
            "capture_write",
            id=memory.id,
            capture_session=self.session,
            segment=segment.sha,
            kind_captured=item.kind,
            category=category,
            scopes=memory.scopes,
            related=[h.id for h in gc.related],
            **supersession.event_fields(),
        )
        return outcome("committed", id=memory.id)

    def _corroborate(self, decision: Reject) -> str | None:
        """Credit the top match once per run, as `memory_write` credits it
        once per session: a claim that re-enters a conversation is a
        recurrence of that claim, however many captured lines repeat it."""
        matches = decision.response.get("matches") or []
        top = matches[0].get("id") if matches and isinstance(matches[0], dict) else None
        if not isinstance(top, str):
            return None
        if self.dry_run or top in self.corroborated:
            return top
        try:
            self.bundle.store.record_corroboration(top)
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a capture
            log.warning("corroboration bump for %s failed: %s", top, exc)
            return top
        self.corroborated.add(top)
        return top

    def _propose(self, item: sc.Captured, category: str) -> str:
        proposal = Proposal(
            id=generate_ulid(),
            body=item.body,
            source_excerpt=item.quote,
            suggested_category=category,
            created=utcnow().isoformat(),
        )
        ProposalQueue(self.bundle.store.root).append([proposal])
        return proposal.id


def _is_open_plan(item: sc.Captured, segment: Segment) -> bool:
    """A plan the session made for itself rather than a commitment worth
    remembering: no date, or a date no later than the segment it was said
    in. In a working session those are the to-do lists and "next session"
    steps that `WORK_SESSION_RULES` asks the model to skip and the model
    does not always skip (two real sessions, 2026-09-24, dated "Next
    session will verify CI" to the day it was said). A plan dated ahead
    of the conversation ("the speedrun window opens 2026-10-12") is kept.
    The plan's date is its `happened_at` or, since the model often leaves
    that empty on a plan and writes the date into the body, the latest
    full date in the body. The comparison is at the date's own precision,
    so "2026-10" is ahead of any day in September."""
    if item.kind != "plan":
        return False
    stamps = [t.ts_ms for t in segment.turns if t.ts_ms is not None]
    dates = [d for d in (item.happened_at, *_BODY_DATE_RE.findall(item.body)) if d]
    if not dates or not stamps:
        return True
    said = datetime.fromtimestamp(max(stamps) / 1000, tz=timezone.utc)
    day = said.strftime("%Y-%m-%d")
    return not any(d > day[: len(d)] for d in dates)


def _covering_memory(
    body: str,
    stored: Sequence[Memory],
    tokens: dict[tuple[str, datetime], set[str]] | None = None,
) -> str | None:
    """The id of a stored memory that already says `body`, or None.

    The dedup gate compares whole bodies, and a short fact restating one
    sentence of a long memory never looks like a duplicate of it: the
    long body's other words swamp the score. This asks the one-sided
    question instead, how much of the captured fact the stored memory
    contains, and it is strict about what is new: a number, version,
    date or id the stored memory lacks means the fact is not covered,
    because that token is usually the whole point of the fact.
    """
    words = _content_token_set(body)
    if len(words) < COVERED_MIN_TOKENS:
        return None
    marked = {w for w in words if any(ch.isdigit() for ch in w)}
    best_id: str | None = None
    best = 0.0
    for memory in stored:
        key = (memory.id, memory.updated)
        have = tokens.get(key) if tokens is not None else None
        if have is None:
            have = _content_token_set(memory.body)
            if tokens is not None:
                tokens[key] = have
        if not marked <= have:
            continue
        share = len(words & have) / len(words)
        if share > best:
            best, best_id = share, memory.id
    return best_id if best >= COVERED_SHARE else None


def _apply_capture_gates(bundle: GateBundle, gc: GateContext) -> Reject | None:
    decision = apply_write_gates(bundle, gc, gates=CAPTURE_GATES)
    # `Pending` is unreachable: CAPTURE_GATES leaves out `PendingGate`.
    return decision if isinstance(decision, Reject) else None


def _reason(decision: Reject) -> str | None:
    response = decision.response
    for key in ("markers", "matches", "claims", "suggested_scopes"):
        value = response.get(key)
        if value:
            return json.dumps(value)[:200]
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class SegmentReport:
    sha: str
    start_offset: int
    end_offset: int
    turns: int
    status: str  # captured | refused | failed | skipped
    file: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    memories: list[MemoryOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sha": self.sha,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "turns": self.turns,
            "status": self.status,
            "memories": [m.to_dict() for m in self.memories],
        }
        for key in ("file", "cost_usd", "error"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class CaptureReport:
    session_id: str
    transcript: str
    dry_run: bool
    model: str
    start_offset: int
    end_offset: int
    segments: list[SegmentReport] = field(default_factory=list)
    remaining_segments: int = 0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for seg in self.segments:
            for m in seg.memories:
                out[m.status] = out.get(m.status, 0) + 1
        return out

    @property
    def cost_usd(self) -> float | None:
        costs = [s.cost_usd for s in self.segments if s.cost_usd is not None]
        return round(sum(costs), 6) if costs else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "transcript": self.transcript,
            "dry_run": self.dry_run,
            "model": self.model,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "counts": self.counts(),
            "cost_usd": self.cost_usd,
            "remaining_segments": self.remaining_segments,
            "segments": [s.to_dict() for s in self.segments],
        }


def _segment_file(segment: Segment, session_id: str, model: str) -> bytes:
    header = (
        f"<!-- bettermemory session capture: session {session_id}, "
        f"transcript bytes {segment.start_offset}-{segment.end_offset}, "
        f"model {model}, captured {utcnow().isoformat()}, "
        f"sha256 {segment.sha} -->\n\n"
    )
    return (header + segment.text + "\n").encode("utf-8")


def capture_transcript(
    *,
    store: MemoryStore,
    config: Config,
    recorder: Recorder,
    transcript: Path,
    model: CaptureModel,
    session_id: str | None = None,
    dry_run: bool = False,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    hold_tail: bool = False,
    wait: bool = True,
) -> CaptureReport:
    """Capture what `transcript` holds past this session's watermark.

    The session id defaults to the transcript's file name, which is how
    Claude Code names it (`<session id>.jsonl`); a different id passed
    for a file named like a session is refused, since the watermark it
    would read belongs to another session.

    A dry run calls the model and runs every gate against the live store,
    so it shows what a real run would write; it writes nothing, bumps no
    corroboration, and leaves the watermark and the segment files alone.

    `hold_tail` leaves the newest segment for a later run: a checkpoint
    of a session still in progress captures the whole segments behind
    it, and the conversation still going on joins the next one. `wait`
    False raises `CaptureBusy` instead of waiting when another capture
    of the session is running, which is what a hook wants: the run in
    progress will get to the same lines.
    """
    transcript = transcript.expanduser().resolve()
    _check_regular_file(transcript)
    stem = transcript.stem if _SESSION_ID_RE.match(transcript.stem) else None
    session = session_id or stem
    if session is None or not _SESSION_ID_RE.match(session):
        raise CaptureError(
            f"session id {session!r} is not a safe identifier; pass --session-id"
        )
    if session_id is not None and stem is not None and stem != session_id:
        raise CaptureError(
            f"transcript {transcript.name} is named for session {stem}, "
            f"not {session_id}"
        )

    directory = capture_dir(Path(store.root), session)
    mark_path = directory / WATERMARK_FILENAME
    if not dry_run:
        ensure_owner_only_dir(directory, parents=True)
    lock: contextlib.AbstractContextManager[None] = (
        contextlib.nullcontext()
        if dry_run
        else flock_excl(mark_path)
        if wait
        else _session_lock_nowait(mark_path)
    )
    try:
        with lock:
            return _run(
                store=store,
                config=config,
                recorder=recorder,
                transcript=transcript,
                model=model,
                session=session,
                directory=directory,
                mark_path=mark_path,
                dry_run=dry_run,
                max_segments=max_segments,
                hold_tail=hold_tail,
            )
    except TimeoutError as exc:
        raise CaptureError(
            f"another capture of session {session} holds the lock"
        ) from exc


def _run(
    *,
    store: MemoryStore,
    config: Config,
    recorder: Recorder,
    transcript: Path,
    model: CaptureModel,
    session: str,
    directory: Path,
    mark_path: Path,
    dry_run: bool,
    max_segments: int,
    hold_tail: bool,
) -> CaptureReport:
    mark = Watermark.load(mark_path, session)
    size = os.stat(transcript).st_size
    read = read_transcript(transcript, offset=mark.offset)
    segments = build_segments(read, max_chars=SEGMENT_MAX_CHARS)
    if hold_tail:
        segments = segments[:-1]
    report = CaptureReport(
        session_id=session,
        transcript=str(transcript),
        dry_run=dry_run,
        model=f"{model.name}:{getattr(model, 'model', '?')}",
        start_offset=read.start_offset,
        end_offset=read.start_offset,
    )

    # The captured session's own working directory, not this process's:
    # the origin is what auto-scope and drift detection key on, and a
    # capture run later from anywhere must still file the session's
    # memories under the checkout it happened in.
    origin = (
        capture_origin(Path(read.cwd)) if read.cwd and Path(read.cwd).is_dir() else None
    )
    identity.bind_transcript(
        session_id=session, model=getattr(model, "model", None), client=CAPTURE_CLIENT
    )
    scopes = [s for s in (project_scope(origin), CAPTURE_SCOPE) if s]
    writer = _Writer(
        bundle=GateBundle.for_store(store, config),
        recorder=recorder,
        session=session,
        origin=origin,
        base_scopes=scopes,
        dry_run=dry_run,
    )

    processed = 0
    for index, segment in enumerate(segments):
        if processed >= max_segments:
            report.remaining_segments = len(segments) - index
            break
        seg_report = SegmentReport(
            sha=segment.sha,
            start_offset=segment.start_offset,
            end_offset=segment.end_offset,
            turns=len(segment.turns),
            status="captured",
        )
        if mark.seen(segment.sha):
            seg_report.status = "skipped"
            report.segments.append(seg_report)
            _advance(mark, segment, None, dry_run, mark_path, transcript)
            report.end_offset = segment.end_offset
            continue
        processed += 1
        try:
            messages = sc.build_capture_messages(
                segment.turns, extra_rules=sc.WORK_SESSION_RULES
            )
        except sc.FenceInjectionError:
            seg_report.status = "refused"
            seg_report.error = "a turn contains the conversation fence"
            report.segments.append(seg_report)
            _advance(mark, segment, seg_report, dry_run, mark_path, transcript)
            report.end_offset = segment.end_offset
            continue
        try:
            reply = model.complete(messages)
        except CaptureModelError as exc:
            seg_report.status = "failed"
            seg_report.error = str(exc)
            report.segments.append(seg_report)
            if not dry_run:
                mark.failures += 1
                mark.last_failure_at = utcnow().isoformat()
                mark.last_error = str(exc)[:500]
                atomic_write_bytes(mark_path, mark.to_json(), mode_before_rename=0o600)
            break
        mark.failures, mark.last_failure_at, mark.last_error = 0, None, None
        seg_report.cost_usd = reply.cost_usd
        for item in sc.parse_capture(reply.text, segment.turns):
            seg_report.memories.append(writer.write(item, segment))
        if not dry_run:
            name = f"{len(mark.segments) + 1:03d}-{segment.sha[:12]}.md"
            atomic_write_bytes(
                directory / name,
                _segment_file(segment, session, report.model),
                mode_before_rename=0o600,
            )
            seg_report.file = name
        report.segments.append(seg_report)
        _advance(mark, segment, seg_report, dry_run, mark_path, transcript)
        report.end_offset = segment.end_offset

    finished = not hold_tail and not report.remaining_segments
    if finished and not any(s.status == "failed" for s in report.segments):
        # Read to the end with nothing held back: a sweep has nothing to
        # do here until the transcript grows. Recorded even when no
        # segment came of it (a tail of tool calls only), or the sweep
        # would start a capture for it at every session start.
        if not dry_run and mark.settled_size != size:
            mark.settled_size = size
            if mark_path.exists() or report.segments:
                atomic_write_bytes(mark_path, mark.to_json(), mode_before_rename=0o600)

    if not dry_run and report.segments:
        counts = report.counts()
        recorder.record(
            "capture_run",
            capture_session=session,
            transcript=str(transcript),
            model=report.model,
            segments=len(report.segments),
            committed=[
                m.id
                for s in report.segments
                for m in s.memories
                if m.status == "committed"
            ],
            counts=counts,
            start_offset=report.start_offset,
            end_offset=report.end_offset,
            **({"cost_usd": report.cost_usd} if report.cost_usd is not None else {}),
        )
    return report


def _advance(
    mark: Watermark,
    segment: Segment,
    seg_report: SegmentReport | None,
    dry_run: bool,
    mark_path: Path,
    transcript: Path,
) -> None:
    """Move the watermark past `segment` and persist it. Written after
    each segment rather than once at the end, so a crash midway loses at
    most the segment in flight."""
    if dry_run:
        return
    mark.transcript = str(transcript)
    mark.offset = segment.end_offset
    if seg_report is not None:
        mark.segments.append(
            {
                "sha": segment.sha,
                "status": seg_report.status,
                "file": seg_report.file,
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "memory_ids": [
                    m.id
                    for m in seg_report.memories
                    if m.status == "committed" and m.id
                ],
                "proposal_ids": [
                    m.id for m in seg_report.memories if m.status == "proposed" and m.id
                ],
                "counts": {
                    status: sum(1 for m in seg_report.memories if m.status == status)
                    for status in {m.status for m in seg_report.memories}
                },
                **(
                    {"cost_usd": seg_report.cost_usd}
                    if seg_report.cost_usd is not None
                    else {}
                ),
            }
        )
    atomic_write_bytes(mark_path, mark.to_json(), mode_before_rename=0o600)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_VERDICT = {
    "committed": "saved",
    "proposed": "queued for review",
    "would_commit": "would save",
    "duplicate": "already stored",
    "covered": "already stored",
}


def render_text(report: CaptureReport) -> str:
    lines = [
        f"{'Dry run: ' if report.dry_run else ''}session {report.session_id}, "
        f"model {report.model}",
        f"transcript bytes {report.start_offset}-{report.end_offset}",
    ]
    for n, seg in enumerate(report.segments, 1):
        head = f"\nsegment {n} ({seg.turns} turns, {seg.status})"
        if seg.cost_usd is not None:
            head += f", ${seg.cost_usd:.4f}"
        lines.append(head)
        if seg.error:
            lines.append(f"  error: {seg.error}")
        for m in seg.memories:
            verdict = _VERDICT.get(m.status, f"dropped: {m.status}")
            ref = f" {m.id}" if m.id else f" (matches {m.matched})" if m.matched else ""
            lines.append(f"  [{verdict}{ref}] ({m.category}) {m.body}")
    counts = report.counts()
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no memories"
    lines.append(f"\n{summary}")
    if report.cost_usd is not None:
        lines.append(f"model cost ${report.cost_usd:.4f}")
    if report.remaining_segments:
        lines.append(
            f"{report.remaining_segments} more segment(s) left for the next run"
        )
    return "\n".join(lines)
