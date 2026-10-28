"""Capture in the session: the bar, the instruction, and the batches.

WHY IN THE SESSION. The owner ruled on 2026-09-25 that capture uses the
model being used in the chat, or nothing: no API key, no provider, no
model plugged in beside the chat. So nothing here calls a model. At the
end of a turn the daemon may hand the chat model one instruction through
the Stop hook (`in_session_instruction`), and the model saves what the
stretch established itself, with `memory_write`, from everything it holds
of the session. Every write gate applies to what it writes, and the
enforced half of the bar (`durability.find_capture_rule_hits`) applies on
top, with no acknowledge flag to pass it.

A CAPTURE IS A BATCH ON THE LOG. Asking opens a batch: a `capture_open`
control row with an unguessable token, the Stop's session id and the
transcript's size. The instruction tells the model to pass the token as
`source="capture:<token>"`, and the token is the binding: a write that
carries it joins that one batch, until the batch settles. Background
subagents share the session's ids and never see the instruction, so the
token is what keeps their writes out. Each write the token commits is a
`capture_member` row, written in the memory's own transaction, and the
batch ends with a `capture_close` that records the transcript offset it
captured to. The rows are control rows (`log.CAPTURE_KINDS`), so all of
this is on the log with telemetry off, and everything capture needs after
a restart (the watermark, the day's count, the open batches, the bodies
an undo reverted) is read back from them. There are no state files.

THE SESSION AS EVIDENCE. A capture write's harness session (the Claude
Code session id the shim forwards) and server session are recorded on
its member row. The harness session must be the Stop's, or one the hooks'
/clear bridge joins to it (`hook.joined_sessions`): the shim keeps the id
it was started with, and Claude Code issues a new one on /clear, so an
exact match alone would refuse every capture after a /clear.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .log import (
    CAPTURE_CLOSE,
    CAPTURE_MEMBER,
    CAPTURE_OPEN,
    CAPTURE_RELATIONS,
    CAPTURE_UNDO,
    LogRow,
)
from .models import generate_ulid, utcnow
from .review import SECRET_STATEMENT, STANDING_STATEMENT, UNSAFE_STATEMENT
from .time_utils import isoformat_utc, parse_event_ts

if TYPE_CHECKING:
    from .store import Store

# ---------------------------------------------------------------------------
# The bar and the instruction
# ---------------------------------------------------------------------------

# The owner's bar for a captured memory, carried byte for byte from the
# in-session design of 2026-09-25 (217ed02 on the capture-in-session
# branch; 1,274 characters, sha256 69ab74f4b20bd067...). Drafted from two
# sources: the owner's store, where the memories that read well lead with
# the exact claim and close on **Why:** / **How to apply:** lines, and
# what the first captures wrote that the owner rejected ("The user's
# project bettermemory has zero GitHub stars.", "The assistant said ...",
# to-dos filed as facts). Numbered so that feedback on a capture, and a
# refusal, can cite a rule by number.
CAPTURE_RULES = """How to write each memory:
1. Durable: still true and useful in a month without upkeep. No to-dos, next steps, progress notes, or numbers that drift (star counts, test totals).
2. One claim, self-contained: a future session understands it without this conversation.
3. Name the subject plainly: the tool, file, project or person. Never "The user's project ..." or "The assistant said ...".
4. Specific: names, versions, commits, paths and numbers exactly as given.
5. Dated: an event or decision starts with its absolute date (YYYY-MM-DD), never "today", "yesterday", "recently" or "now".
6. The why when there is one; for a lesson or preference, how to apply it too, as "**Why:**" and "**How to apply:**" lines.
7. The user's own words verbatim, with the date, for rulings and preferences. Never soften or strengthen them.
8. Category: a claim about the user is "user-inference"; a project or tool fact is "fact".
9. New: skip what the store already holds (memory_search when unsure). When a stored memory is now wrong, memory_update it instead of writing a contradiction.
10. True to the conversation: only what it supports, and never the assistant's claims filed as the user's facts.
Fewer, denser memories beat many. Saving nothing is right when nothing durable happened."""

# Claude Code caps a hook's `additionalContext` at 10,000 characters and
# drops the excess. The instruction stays far inside it; the tests pin the
# bound with the longest arguments the daemon passes.
INSTRUCTION_MAX_CHARS = 10_000
# The UTC times of the user's messages the instruction lists, the latest
# ones when the stretch has more.
MAX_PROMPT_TIMES = 20

_SETUP_NOTE = (
    "This comes from the user's memory setup, not from a new message by the user."
)


def _scopes_line(scopes: Sequence[str]) -> str:
    """The scope of the project the session's folder belongs to, offered
    for memories about that project only: a chat run in one repository
    is often about another project or a tool, and a memory filed under
    the folder's scope by default would surface in the wrong project."""
    named = ", ".join(f'"{s}"' for s in scopes if s)
    if not named:
        return ""
    return (
        f"This folder's project scope is {named}: use it only for memories "
        "about that project. A memory about another project or tool takes "
        "its subject's own scope (projects:<name>), wherever this chat runs."
    )


def in_session_instruction(
    *,
    token: str,
    since: str | None,
    scopes: Sequence[str],
    prompt_times: Sequence[str] = (),
    compacted: bool = False,
) -> str:
    """What the Stop hook hands the chat model once the conversation since
    its last capture has grown past the checkpoint: save what is worth
    keeping, from its own context, into the batch `token` names.

    Around the bar it carries the batch token, the folder's project scope
    for memories about that project, the rule 9 amendment for a capture
    (supersedes, never an update, so an undo restores exactly what was
    there), whose words a body records (rule 10 read for one body: what
    the user said, decided or adopted, not the assistant's own
    suggestions or caveats), the times of the user's messages to date
    events by, and how to answer the write review: the two scores on each
    write, the relations in one closing call."""
    stretch = f"this conversation since {since}" if since else "this conversation"
    times = [t for t in prompt_times if t][-MAX_PROMPT_TIMES:]
    parts = [
        f"bettermemory session capture. Before you stop, look back over {stretch} "
        "and save what it established that is worth keeping, with memory_write. "
        f"Skip anything you already saved. {_SETUP_NOTE}",
        CAPTURE_RULES,
        "A body records what the user said, decided or adopted, never your "
        "own suggestions or caveats unless the user adopted them.",
        f'Save each memory with memory_write and source="{SOURCE_PREFIX}{token}". '
        + _scopes_line(scopes),
        "Do not update, remove or verify stored memories during this capture. "
        "When a stored memory is now wrong, write the corrected memory with "
        "supersedes=[its id] instead.",
        (
            "The user's messages in this stretch were sent at (UTC): "
            + ", ".join(times)
            + "."
            if times
            else ""
        ),
        (
            "Part of this stretch was compacted: save from the summary you hold, "
            "and nothing it does not support."
            if compacted
            else ""
        ),
        'Pass review={"unsafe": <0 to 1>, "secret": <0 to 1>, "standing": <0 to 1>} '
        "with each write, the probability that each holds. unsafe: "
        f"{UNSAFE_STATEMENT} secret: {SECRET_STATEMENT} standing: "
        f"{STANDING_STATEMENT} Once all are saved, answer every relations_pending "
        "in one closing call, as its hint says.",
        "When you are done, stop, and tell the user in one short line what you "
        "saved, or that nothing new was worth saving.",
    ]
    return "\n\n".join(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Tokens and bodies
# ---------------------------------------------------------------------------

# A capture write's `source`: this prefix and the batch token.
SOURCE_PREFIX = "capture:"
# 64 random bits as 16 lowercase hex characters: short enough to copy
# exactly, too many to guess inside a batch's life.
TOKEN_BYTES = 8
_TOKEN_RE = re.compile(r"[0-9a-f]{16}")


def mint_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def token_of(source: object) -> str | None:
    """The token a tagged `source` carries ("" for a bare prefix), or None
    when `source` is not a capture tag."""
    if not isinstance(source, str) or not source.startswith(SOURCE_PREFIX):
        return None
    return source[len(SOURCE_PREFIX) :].strip()


def body_hash(body: str) -> str:
    """The normalized body's SHA-256: casefolded, whitespace collapsed,
    so a re-capture that differs only in spacing or case still matches
    the body an undo reverted."""
    normal = " ".join(body.casefold().split())
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Batches on the log
# ---------------------------------------------------------------------------

# A batch the Stop never settled (the session ended mid-capture) stops
# accepting writes after this long; its members stay undoable.
BATCH_TTL = timedelta(minutes=30)

MODE_CHECKPOINT = "checkpoint"
MODE_CHECKPOINT_COMPACTED = "checkpoint_compacted"
OUTCOME_SETTLED = "settled"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_EXPIRED = "expired"
# The batch opened but its instruction never reached the model: nothing
# was asked, so no quiet hour follows and it takes no `daily_cap` slot.
OUTCOME_FAILED = "failed"
_OUTCOMES = frozenset(
    {OUTCOME_SETTLED, OUTCOME_INTERRUPTED, OUTCOME_EXPIRED, OUTCOME_FAILED}
)

# How a capture write's harness session was accepted.
JOINED_BY_STOP_SESSION = "stop_session"
JOINED_BY_BRIDGE = "bridge"

# Why a token does not admit a write, by the rule name a refusal carries.
TOKEN_UNKNOWN = "capture_token_unknown"
BATCH_CLOSED = "capture_batch_closed"
BATCH_EXPIRED = "capture_batch_expired"


class CaptureError(RuntimeError):
    """A batch operation the log refuses."""


class BatchAlreadyOpenError(CaptureError):
    """The session already has an open batch."""


class BatchClosedError(CaptureError):
    """The batch was already closed."""


@dataclass(frozen=True)
class Batch:
    """One `capture_open` row. `session_id` is the Stop payload's session
    id (the harness session the batch was asked in); `offset` the
    watermark the stretch starts at and `transcript_bytes` the
    transcript's size when it was asked."""

    batch: str
    token: str
    session_id: str
    opened_at: datetime
    seq: int
    mode: str
    offset: int
    transcript_bytes: int
    transcript_path: str | None
    worktree_root: str | None

    def expired(self, now: datetime) -> bool:
        return now - self.opened_at >= BATCH_TTL


@dataclass(frozen=True)
class Close:
    """One `capture_close` row: how the batch ended and the transcript
    offset it captured to (`watermark`; the batch's own `offset` when
    nothing was captured), and the relations its members left unanswered,
    by member id."""

    batch: str
    session_id: str
    outcome: str
    watermark: int
    transcript_bytes: int
    closed_at: datetime
    relations_pending: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Member:
    """One `capture_member` row: a memory a batch's token committed, its
    normalized body hash, and the session evidence it was accepted on.
    `review` is the deferred review of a write that committed with its
    relations pending (`deferred_review`), None for every other write."""

    batch: str
    memory_id: str
    body_hash: str
    harness_session: str | None
    server_session: str | None
    joined_by: str
    seq: int
    review: Mapping[str, Any] | None = None


def _payload(row: LogRow) -> dict[str, Any]:
    try:
        loaded = json.loads(row.payload)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _stamp(row: LogRow) -> datetime:
    return parse_event_ts(row.ts) or datetime.min.replace(tzinfo=timezone.utc)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _id_lists(value: object) -> dict[str, tuple[str, ...]]:
    """A {member id: [ids]} payload field, malformed entries dropped."""
    if not isinstance(value, dict):
        return {}
    return {
        k: tuple(i for i in v if isinstance(i, str))
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def _batch_of(row: LogRow) -> Batch | None:
    fields = _payload(row)
    batch, token, session_id = (
        _str(fields.get("batch")),
        _str(fields.get("token")),
        _str(fields.get("session_id")),
    )
    if batch is None or token is None or session_id is None:
        return None
    return Batch(
        batch=batch,
        token=token,
        session_id=session_id,
        opened_at=_stamp(row),
        seq=row.seq,
        mode=_str(fields.get("mode")) or MODE_CHECKPOINT,
        offset=_int(fields.get("offset")),
        transcript_bytes=_int(fields.get("transcript_bytes")),
        transcript_path=_str(fields.get("transcript_path")),
        worktree_root=_str(fields.get("worktree_root")),
    )


def _close_of(row: LogRow) -> Close | None:
    fields = _payload(row)
    batch, session_id = _str(fields.get("batch")), _str(fields.get("session_id"))
    if batch is None or session_id is None:
        return None
    return Close(
        batch=batch,
        session_id=session_id,
        outcome=_str(fields.get("outcome")) or OUTCOME_SETTLED,
        watermark=_int(fields.get("watermark")),
        transcript_bytes=_int(fields.get("transcript_bytes")),
        closed_at=_stamp(row),
        relations_pending=_id_lists(fields.get("relations_pending")),
    )


def _member_of(row: LogRow) -> Member | None:
    fields = _payload(row)
    batch, memory_id, digest = (
        _str(fields.get("batch")),
        _str(fields.get("memory_id")),
        _str(fields.get("body_hash")),
    )
    if batch is None or memory_id is None or digest is None:
        return None
    return Member(
        batch=batch,
        memory_id=memory_id,
        body_hash=digest,
        harness_session=_str(fields.get("harness_session")),
        server_session=_str(fields.get("server_session")),
        joined_by=_str(fields.get("joined_by")) or JOINED_BY_STOP_SESSION,
        seq=row.seq,
        review=review if isinstance(review := fields.get("review"), dict) else None,
    )


def batches(store: Store) -> list[Batch]:
    """Every batch the log opened, in log order."""
    return [b for row in store.capture_rows({CAPTURE_OPEN}) if (b := _batch_of(row))]


def closes(store: Store) -> dict[str, Close]:
    """Every batch's close by batch id (the first, should two exist)."""
    out: dict[str, Close] = {}
    for row in store.capture_rows({CAPTURE_CLOSE}):
        close = _close_of(row)
        if close is not None:
            out.setdefault(close.batch, close)
    return out


def members(store: Store, batch: str | None = None) -> list[Member]:
    """The member rows of `batch`, or of every batch, in log order."""
    out: list[Member] = []
    for row in store.capture_rows({CAPTURE_MEMBER}):
        member = _member_of(row)
        if member is not None and (batch is None or member.batch == batch):
            out.append(member)
    return out


def open_batches(store: Store, *, now: datetime | None = None) -> list[Batch]:
    """The batches with no close that are inside their time to live."""
    when = now or utcnow()
    closed = closes(store)
    return [b for b in batches(store) if b.batch not in closed and not b.expired(when)]


def open_batch_for_session(
    store: Store, session_id: str, *, now: datetime | None = None
) -> Batch | None:
    """The open batch the Stop session `session_id` asked for, if any."""
    for batch in open_batches(store, now=now):
        if batch.session_id == session_id:
            return batch
    return None


def batch_for_token(
    store: Store, token: str, *, now: datetime | None = None
) -> tuple[Batch | None, str | None]:
    """The open batch `token` names and None, or None and why not
    (`TOKEN_UNKNOWN`, `BATCH_CLOSED`, `BATCH_EXPIRED`). The comparison is
    constant-time, and a token of the wrong shape is unknown."""
    if not _TOKEN_RE.fullmatch(token):
        return None, TOKEN_UNKNOWN
    # Compared as bytes: a stored token is whatever the log row says, and
    # `secrets.compare_digest` raises TypeError on a str that is not ASCII,
    # so one an editor of the store file rewrote must read as no match.
    given = token.encode("utf-8", errors="surrogatepass")
    found = next(
        (
            b
            for b in batches(store)
            if secrets.compare_digest(
                b.token.encode("utf-8", errors="surrogatepass"), given
            )
        ),
        None,
    )
    if found is None:
        return None, TOKEN_UNKNOWN
    if found.batch in closes(store):
        return None, BATCH_CLOSED
    if found.expired(now or utcnow()):
        return None, BATCH_EXPIRED
    return found, None


def open_batch(
    store: Store,
    *,
    session_id: str,
    transcript_bytes: int,
    offset: int,
    transcript_path: str | None = None,
    worktree_root: str | None = None,
    mode: str = MODE_CHECKPOINT,
    token: str | None = None,
    now: datetime | None = None,
) -> Batch:
    """Open a batch for the Stop session `session_id` with `token`, or a
    token minted here. The Stop mints it first, so the instruction that
    carries it is built before anything is on the log.

    Atomic per session: the check for an open batch and the row run in one
    transaction under the store's write lock, so two Stops racing for one
    session open one batch, and the other raises `BatchAlreadyOpenError`.
    Other hooks set `stop_hook_active` too, which is why a Stop settles
    only the batch this records."""
    when = now or utcnow()

    def guard() -> None:
        if open_batch_for_session(store, session_id, now=when) is not None:
            raise BatchAlreadyOpenError(f"session {session_id} has an open capture")

    payload = {
        "batch": generate_ulid(),
        "token": token or mint_token(),
        "session_id": session_id,
        "mode": mode,
        "offset": int(offset),
        "transcript_bytes": int(transcript_bytes),
        "transcript_path": transcript_path,
        "worktree_root": worktree_root,
    }
    row = store.record_capture(
        CAPTURE_OPEN, payload, session=session_id, ts=isoformat_utc(when), guard=guard
    )
    opened = _batch_of(row)
    assert opened is not None
    return opened


def close_batch(
    store: Store,
    batch: Batch,
    *,
    outcome: str,
    watermark: int,
    transcript_bytes: int,
    now: datetime | None = None,
) -> Close:
    """Close `batch`: settled (the capture turn came back to a stop; the
    stretch up to `watermark` is captured), interrupted, expired or failed
    (pass the batch's own offset, nothing is captured). A second close raises
    `BatchClosedError`."""
    if outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_OUTCOMES)}")
    when = now or utcnow()

    def guard() -> None:
        if batch.batch in closes(store):
            raise BatchClosedError(f"capture batch {batch.batch} is already closed")

    payload: dict[str, Any] = {
        "batch": batch.batch,
        "session_id": batch.session_id,
        "outcome": outcome,
        "watermark": int(watermark),
        "transcript_bytes": int(transcript_bytes),
    }
    # A batch that settles with relations unanswered keeps its commits and
    # names every id it left pending; nothing is filed for them.
    pending = relations_pending(store, batch.batch)
    if pending:
        payload["relations_pending"] = {k: list(v) for k, v in pending.items()}
    row = store.record_capture(
        CAPTURE_CLOSE,
        payload,
        session=batch.session_id,
        ts=isoformat_utc(when),
        guard=guard,
    )
    closed = _close_of(row)
    assert closed is not None
    return closed


def record_member(
    store: Store,
    batch: Batch,
    *,
    memory_id: str,
    body: str,
    harness_session: str | None,
    server_session: str | None,
    joined_by: str,
    review: Mapping[str, Any] | None = None,
) -> LogRow:
    """The member row for a memory `batch`'s token committed. The write
    path calls this inside the `Store.batch` that puts the memory, so the
    two commit together. Never the body: its normalized hash. `review` is
    a deferred review (`deferred_review`), kept for the closing call."""
    payload: dict[str, Any] = {
        "batch": batch.batch,
        "memory_id": memory_id,
        "body_hash": body_hash(body),
        "harness_session": harness_session,
        "server_session": server_session,
        "joined_by": joined_by,
    }
    if review is not None:
        payload["review"] = dict(review)
    return store.record_capture(CAPTURE_MEMBER, payload, session=server_session)


# ---------------------------------------------------------------------------
# Relations answered at the end
# ---------------------------------------------------------------------------
#
# A capture write that carries its two scores commits in one call even when
# its related set is unanswered. Its member row then holds the deferred
# review: the review_id, the scores and the thresholds they were judged by,
# the relation answers it gave, the related ids with their claim_sized
# flags, the cue code's links among them, the declared supersedes targets
# and the ids still pending. Ids, flags and scores only, never a body. The
# closing call (memory_admin action "relations") answers the rest; each one
# is a `capture_relations` row per batch naming what it answered and what it
# changed, which is what an undo of the batch reverts.


def deferred_review(
    *,
    review_id: str,
    unsafe: float,
    secret: float,
    tau: Sequence[float],
    relation: Mapping[str, str],
    related: Sequence[tuple[str, bool]],
    cue_linked: Sequence[str],
    claim_sized: bool,
    declared: Sequence[str],
    pending: Sequence[str],
    standing: Sequence[str] = (),
    said: bool = False,
) -> dict[str, Any]:
    """The deferred review a member row carries: with the standing records
    among its related, declared and cue ids at commit, and whether the
    user's words carried it (unit RS-F7 part 5)."""
    return {
        "review_id": review_id,
        "unsafe": unsafe,
        "secret": secret,
        "tau": list(tau),
        "relation": dict(relation),
        "related": [[memory_id, bool(sized)] for memory_id, sized in related],
        "cue_linked": list(cue_linked),
        "claim_sized": bool(claim_sized),
        "declared": list(declared),
        "pending": list(pending),
        "standing": list(standing),
        "said": bool(said),
    }


def relations_rows(store: Store, batch: str | None = None) -> list[dict[str, Any]]:
    """The `capture_relations` payloads of `batch`, or of every batch."""
    rows = (_payload(row) for row in store.capture_rows({CAPTURE_RELATIONS}))
    return [r for r in rows if batch is None or r.get("batch") == batch]


def relations_answered(store: Store, batch: str) -> dict[str, dict[str, str]]:
    """The relation answers the closing calls gave, by member id."""
    out: dict[str, dict[str, str]] = {}
    for row in relations_rows(store, batch):
        answers = row.get("answers")
        for member_id, given in (answers if isinstance(answers, dict) else {}).items():
            if isinstance(given, dict):
                out.setdefault(member_id, {}).update(given)
    return out


def relations_pending(store: Store, batch: str) -> dict[str, tuple[str, ...]]:
    """The related ids each member of `batch` still has unanswered."""
    answered = relations_answered(store, batch)
    out: dict[str, tuple[str, ...]] = {}
    for member in members(store, batch):
        pending = (member.review or {}).get("pending")
        if not isinstance(pending, list):
            continue
        done = answered.get(member.memory_id, {})
        left = tuple(i for i in pending if isinstance(i, str) and i not in done)
        if left:
            out[member.memory_id] = left
    return out


def record_relations(
    store: Store, batch: str, payload: Mapping[str, Any], *, session: str | None
) -> LogRow:
    """One closing call's `capture_relations` row for `batch`. Call it
    inside the `Store.batch` that applies the answers."""
    return store.record_capture(
        CAPTURE_RELATIONS, {"batch": batch, **payload}, session=session
    )


def record_undo(
    store: Store,
    batch: str,
    memory_ids: Collection[str],
    *,
    session: str | None = None,
) -> LogRow:
    """The `capture_undo` row for the members of `batch` named in
    `memory_ids`, carrying their body hashes so a later capture of the
    same body is refused as previously undone. Call it inside the
    `Store.batch` that reverts them."""
    wanted = set(memory_ids)
    kept = [m for m in members(store, batch) if m.memory_id in wanted]
    payload = {
        "batch": batch,
        "memory_ids": [m.memory_id for m in kept],
        "body_hashes": sorted({m.body_hash for m in kept}),
    }
    return store.record_capture(CAPTURE_UNDO, payload, session=session)


def undone_hashes(store: Store) -> frozenset[str]:
    """Every body hash a capture undo reverted."""
    out: set[str] = set()
    for row in store.capture_rows({CAPTURE_UNDO}):
        hashes = _payload(row).get("body_hashes")
        if isinstance(hashes, list):
            out.update(h for h in hashes if isinstance(h, str))
    return frozenset(out)


def watermark(store: Store, session_id: str) -> int:
    """The transcript offset `session_id` is captured to: the furthest
    watermark of its settled batches, 0 when none settled."""
    return max(
        (
            close.watermark
            for close in closes(store).values()
            if close.session_id == session_id and close.outcome == OUTCOME_SETTLED
        ),
        default=0,
    )


def last_close(store: Store, session_id: str) -> Close | None:
    """The latest close, in log order, of a batch `session_id` asked for:
    what the quiet hour after an interrupt starts from."""
    found = [c for c in closes(store).values() if c.session_id == session_id]
    return found[-1] if found else None


def daily_count(store: Store, *, now: datetime | None = None) -> int:
    """The batches opened since the start of the current UTC day, but for
    those that failed before the model was asked: what `[capture]
    daily_cap` bounds."""
    when = (now or utcnow()).astimezone(timezone.utc)
    start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    failed = {b for b, c in closes(store).items() if c.outcome == OUTCOME_FAILED}
    return sum(
        1 for b in batches(store) if b.opened_at >= start and b.batch not in failed
    )


def joined_by(
    store: Store,
    batch: Batch,
    *,
    harness_session: str | None,
    server_session: str | None,
) -> str | None:
    """How a capture write's sessions join `batch`: its harness session is
    the Stop's (`JOINED_BY_STOP_SESSION`), or the hooks' bridge joins its
    server session to the Stop's (`JOINED_BY_BRIDGE`: a shim started
    before a /clear still forwards the old id). None when neither holds.
    The bridge reads the telemetry the server stamped, from an hour before
    the batch opened; with telemetry off only the exact match answers."""
    if harness_session is not None and harness_session == batch.session_id:
        return JOINED_BY_STOP_SESSION
    if server_session is None:
        return None
    from .audit import REAUDIT_DEDUP_WINDOW_SECONDS
    from .hook import joined_sessions

    events = list(
        store.events_since(
            batch.opened_at - timedelta(seconds=REAUDIT_DEDUP_WINDOW_SECONDS)
        )
    )
    joined = joined_sessions(
        events, harness_session=batch.session_id, worktree_root=batch.worktree_root
    )
    return JOINED_BY_BRIDGE if server_session in joined else None


__all__ = [
    "BATCH_CLOSED",
    "BATCH_EXPIRED",
    "BATCH_TTL",
    "CAPTURE_RULES",
    "INSTRUCTION_MAX_CHARS",
    "MAX_PROMPT_TIMES",
    "SOURCE_PREFIX",
    "TOKEN_UNKNOWN",
    "Batch",
    "BatchAlreadyOpenError",
    "BatchClosedError",
    "CaptureError",
    "Close",
    "Member",
    "batch_for_token",
    "body_hash",
    "close_batch",
    "daily_count",
    "deferred_review",
    "in_session_instruction",
    "joined_by",
    "members",
    "mint_token",
    "open_batch",
    "open_batch_for_session",
    "open_batches",
    "record_member",
    "record_relations",
    "record_undo",
    "relations_answered",
    "relations_pending",
    "relations_rows",
    "token_of",
    "undone_hashes",
    "watermark",
]
