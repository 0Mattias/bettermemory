"""Per-session, in-memory state.

Tracked here:
- Scopes disabled for the rest of the session.
- Pending memory writes awaiting `memory_write_confirm`.
- Pending use-tokens issued by `memory_search` / `memory_show` so older
  retrievals can be auto-committed as `applied` on the next memory_*
  call (the auto-`record_use` flow). The model can override the auto
  outcome by calling `memory_record_use(memory_ids=[...], outcome=...)`
  before the token expires.

For the stdio transport (one MCP client per server process), a single
`SessionState` is correct: there's exactly one client to track. For
transports that serve multiple clients from one server process (HTTP,
SSE), each client needs its own state — otherwise client A's pending
write could be confirmed by client B, and client A's `disabled_scopes`
would silently bleed into client B's searches. The `SessionRegistry`
in this module is the routing layer: it hands out (and lazily creates)
a distinct `SessionState` per client identifier.

The single-state and registry shapes are unified through the
`SessionSource` protocol so tests can pass a concrete `SessionState`
directly (no per-client routing needed when there's only one) and
production can pass a `SessionRegistry` (per-client when a stable
client_id is available, falling back to a shared "default" state when
not). `server._register_tools` calls `sessions.for_request(ctx)` at
the entry of every tool handler; the resolution layer is invisible
to the handler bodies.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, TypeAlias

from ._fsutil import atomic_write_bytes, flock_excl
from .models import Category, Confidence, Source
from .origin import Origin

if TYPE_CHECKING:
    # `mcp.server.mcpserver.Context` is the SDK's request-scoped context,
    # which exposes `request_context` (and through it the request's
    # wire-level _meta map) and `request_id`. It's imported under TYPE_CHECKING so
    # the session module stays usable in environments that don't have the
    # MCP server extras loaded (the same way the rest of the package
    # handles optional imports). `Context` is generic over two type
    # parameters that callers here don't constrain — alias it as `_Ctx`
    # so mypy gets the explicit `Any, Any` once instead of every
    # annotation site repeating it. The arity is version-specific: mcp
    # 1.x had a third (session) parameter and 2.x dropped it.
    from mcp.server.mcpserver import Context

    _Ctx: TypeAlias = Context[Any, Any]


log = logging.getLogger("bettermemory.session")


# Pending writes expire after this many seconds — keeps stale entries from
# accumulating if the consumer never confirms or cancels.
_PENDING_TTL_SECONDS = 60 * 60  # 1 hour

# Sidecar mirroring the staged writes to disk so a server restart mid
# confirmation doesn't drop them silently. See `PendingWriteLog`.
PENDING_WRITES_FILENAME = ".pending_writes.jsonl"

# Key under which clients that don't expose a stable identifier (e.g.
# stdio transport, where `ctx.client_id` may be None) share a single
# SessionState. Anything else opts into per-client isolation by passing
# a real client_id through. Declared up here (rather than beside
# `SessionRegistry`, where it used to live) because `SessionState` now
# carries it as a field default.
_DEFAULT_CLIENT_KEY = "__default__"

# Pending use-tokens are evicted on a wall-clock TTL as a safety net for
# very long-lived sessions. The primary expiry is the *turn-counter*
# delta inside `consume_old_tokens` — wall-clock is only the last-resort
# cleanup so the token map can't grow without bound.
_PENDING_USE_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes

# Default turn delta after which a pending use-token is considered
# "old enough" to auto-commit as `applied`. Two turns means: the search
# happens on turn N, and by turn N+2 (two more memory_* calls later)
# we treat the retrieval as having shaped a response. One turn is too
# eager (the model's very next call could be unrelated); three turns
# is too forgiving (the auto-applied event would land far enough away
# from the retrieval that a `record_use(ignored)` override could miss
# its window). Tunable on the call-site for tests.
DEFAULT_USE_TOKEN_TTL_TURNS = 2

# Wall-clock floor for the in-process auto-commit, the second axis of
# `consume_old_tokens`'s readiness test. Mirrors
# `audit.ATTRIBUTION_LOOKBACK_SECONDS` (cross-pinned in tests, not
# imported — session.py stays dependency-free of the audit stack): the
# Stop hook settles a turn's retrievals at turn end within that
# window, so the in-process fallback must not fire earlier or it
# races the hook and re-creates the mid-turn auto-commit problem the
# floor exists to close. Deliberately below
# `_PENDING_USE_TOKEN_TTL_SECONDS` (30 min) so hookless deployments
# still auto-commit before the silent wall-clock eviction.
AUTO_COMMIT_MIN_AGE_SECONDS = 600.0


def _new_session_id() -> str:
    """Per-process session identifier. Stamped onto every event in the log
    so retrieval-vs-write streams can be correlated to a single client."""
    return "sess_" + secrets.token_hex(8)


# The escape hatches a `memory_write` call can carry, by the name
# `handlers.write.GateContext` gives each one — the staged copy is splatted
# straight into a `GateContext` when `memory_write_confirm` re-runs the
# gates, so these names are load-bearing rather than descriptive. Every key
# is always present after `_normalize_gate_flags`, because the fields they
# feed have no defaults on `GateContext`.
#
# `groundedness_check` / `source_transcript` are deliberately absent:
# `GroundednessGate` judges the body against a transcript that no longer
# exists by confirm time, so the confirm-side chain doesn't run it and
# carrying the transcript would persist a whole conversation to disk for a
# gate that can't fire.
GATE_FLAG_KEYS: tuple[str, ...] = (
    "force",
    "acknowledge_transient",
    "acknowledge_scope_mismatch",
    "acknowledge_ungrounded",
    "acknowledge_credential",
    "acknowledge_user_claim",
)


def _normalize_gate_flags(raw: dict[str, Any] | None) -> dict[str, bool]:
    """Every `GATE_FLAG_KEYS` entry as a bool, unknown keys dropped.

    Both a caller and a decoded sidecar row go through here, so a row
    written by a newer version (extra key) or an older one (missing key)
    still produces a mapping the `GateContext` constructor accepts.
    """
    if not raw:
        return dict.fromkeys(GATE_FLAG_KEYS, False)
    return {key: bool(raw.get(key, False)) for key in GATE_FLAG_KEYS}


@dataclass
class PendingWrite:
    """A `memory_write` call awaiting confirmation.

    `gate_flags` is the original call's overrides. Without them the
    confirm-time re-gate runs with everything False and re-refuses a write
    the caller already forced or acknowledged at staging time — the store
    can legitimately have grown a near-twin during the wait, so the
    re-refusal would look like a fresh finding rather than the same one
    the caller already answered.
    """

    pending_id: str
    payload: dict[str, Any]  # the kwargs to pass to Store.write
    created_at: float  # monotonic-ish epoch seconds
    gate_flags: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalized HERE rather than only in `stage_write`, because the
        # confirm-side reader indexes the mapping by key: a `PendingWrite`
        # built directly with a partial dict would raise `KeyError` from
        # inside a gate chain instead of judging the write.
        self.gate_flags = _normalize_gate_flags(self.gate_flags)


@dataclass
class PendingUseToken:
    """A retrieval the model has been told about but not yet recorded
    a `record_use` event for.

    Issued by `memory_search` / `memory_show` and folded into the
    response so the model gets an opaque correlation handle (`use_token`).
    The server tracks the token internally; the next memory_* call from
    the same session lets the auto-commit pass that runs at handler
    entry sweep tokens older than `ttl_turns` and emit one
    `record_use` event per batch with `outcome="applied"` and
    `auto=True`.

    `issued_at_turn` is the session-local turn counter at issue time;
    `issued_at` is the wall-clock for the safety-net eviction.
    """

    token: str
    memory_id: str
    issued_at: float
    issued_at_turn: int


# ---------------------------------------------------------------------------
# Pending-write sidecar
# ---------------------------------------------------------------------------


# The payload keys whose values are not plain JSON. Everything else
# (`content`, `scopes`) round-trips as-is.
_PAYLOAD_ENUMS: dict[str, Any] = {
    "confidence": Confidence,
    "source": Source,
    "category": Category,
}


def _encode_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """`Store.write` kwargs as JSON-safe values.

    The three enums are `str` subclasses, so `json.dumps` would already
    emit them — but `origin` is a pydantic model and raises, which is the
    reason this function exists rather than a bare dumps.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _PAYLOAD_ENUMS:
            out[key] = getattr(value, "value", value)
        elif key == "origin":
            out[key] = value.model_dump() if value is not None else None
        else:
            out[key] = value
    return out


def _decode_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Inverse of `_encode_payload`. Raises on an unrecognised enum value
    so the caller can drop the row rather than stage an invalid write.

    `Origin._repo_url_alternates` is a pydantic PrivateAttr and does not
    survive `model_dump()`. That is the same thing that happens to an
    origin the moment it reaches memory frontmatter, so a rehydrated
    staged write is exactly as origin-complete as a committed memory.
    """
    out = dict(raw)
    for key, enum_cls in _PAYLOAD_ENUMS.items():
        value = out.get(key)
        if value is not None:
            out[key] = enum_cls(value)
    origin = out.get("origin")
    if isinstance(origin, dict):
        out["origin"] = Origin.model_validate(origin)
    return out


class PendingRow(NamedTuple):
    """One decoded sidecar row.

    `pending` is None for an EXPIRY MARKER — a row kept only so
    `was_recently_expired` can still tell "the TTL ate it" from "that id
    never existed" after a restart. The payload is dropped at eviction
    (nothing may commit it any more), the id and the eviction time are
    not.

    `promotion` is the `(episode_session_id, episode_id)` linkage when the
    write was staged by `episode_promote`, and None otherwise.

    CONSUMED rows never surface here — `load` is the adoption feed, and a
    consumed id must not be adoptable by anything. `is_consumed` is the
    read path for those.
    """

    pending_id: str
    expired_at: float | None
    pending: PendingWrite | None
    promotion: tuple[str, str] | None = None


# The three terminal states a sidecar row can be in. `live` carries a
# payload something may still commit; `expired` and `consumed` are
# TOMBSTONES — one-way, and the reason this file is safe to share.
_ROW_LIVE = "live"
_ROW_EXPIRED = "expired"
_ROW_CONSUMED = "consumed"


def _row_state(raw: dict[str, Any]) -> str:
    """Classify a raw row. Order matters: a row carrying both markers
    (impossible today, cheap to be right about) reads as consumed, the
    stronger of the two — nothing may commit it either way."""
    if raw.get("consumed_at") is not None:
        return _ROW_CONSUMED
    if raw.get("expired_at") is not None:
        return _ROW_EXPIRED
    return _ROW_LIVE


class PendingAlreadyConsumed(ValueError):
    """`take_pending` lost the race for a pending id.

    Raised — not returned as None — because the confirm handler consumes
    the staged write for its side effect and commits from the object it
    peeked earlier. A quiet None there would let a write whose durable
    claim was REFUSED land in the store anyway, which is the whole defect
    the claim exists to close. A `ValueError` subclass so the MCP
    boundary renders it like every other bad-pending-id error.
    """


@dataclass
class PendingWriteLog:
    """The on-disk mirror of one store's staged writes.

    Same discipline as `proposals.ProposalQueue` and
    `conflicts.ConflictQueue`: one JSON object per line under
    `<store root>/.pending_writes.jsonl`, every mutation a
    read-modify-write inside `flock_excl`, the rewrite atomic with the
    private mode set BEFORE the rename (a staged body is un-reviewed user
    content — it has passed the credential gate, but it is content the
    user has not yet agreed to store at all).

    ROWS ARE KEYED BY CLIENT, NOT BY PENDING ID. `SessionRegistry` exists
    to stop client B confirming client A's staged user-inference write,
    and a sidecar keyed by pending_id alone would hand that back through
    the disk. Every mutator touches only the rows it names and leaves
    every other client's untouched, so two clients sharing a store root
    never see each other's staged writes.

    The client key — not `session_id` — is what a row is filed under, and
    that choice is the whole feature: `session_id` is minted fresh per
    process, so a sidecar keyed by it could never be read back by the
    restart it exists to survive. The cost is that two server processes
    serving the SAME client key against the SAME store (two stdio servers,
    both bucketed into `_DEFAULT_CLIENT_KEY`) share the staged set. A
    pending id is 16 bytes of `secrets` output, is returned only to the
    caller that staged it, and is never enumerated by any tool — so a
    second process can count the staged writes but cannot name one.
    `session` is recorded on each row for the audit trail, never matched
    on.

    NO METHOD HERE TAKES A SNAPSHOT. That is the load-bearing part, and
    it is not a style preference: the first cut of this class had one
    `save(client_key, pending=[...])` that replaced all of a client's
    rows with the caller's live in-memory set. Two live `SessionState`s
    sharing a key (which the paragraph above accepts as normal) then
    clobber each other, and the damage is not symmetric — a state that
    staged BEFORE another one confirmed writes the CONSUMED row back onto
    disk on its next stage, and the id becomes confirmable a second time.
    One `memory_write`, two durable memories, one pending id. So the API
    is a set of deltas — `append`, `claim`, `mark_expired`, `gc` — each a
    read-modify-write under `flock_excl` that merges into whatever is on
    disk. A consumed id leaves a TOMBSTONE (`consumed_at`) rather than a
    hole, because a hole is indistinguishable from "never persisted" and
    an id that has been consumed must never be re-runnable.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / PENDING_WRITES_FILENAME

    def _mutate(
        self, fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]
    ) -> bool:
        """Read-modify-write the whole file under the exclusive lock.

        `fn` receives EVERY client's rows and returns the replacement set,
        or None to leave the file untouched — returning None is how a
        mutator that finds nothing to do skips the write, which matters
        because `_evict_expired` runs at the entry of every tool call.

        Best-effort by contract: a store root that cannot be written (a
        read-only mount, a permission change, a directory sitting where
        the file should be) degrades this feature back to the in-process
        behaviour it replaced. Losing the mirror must never turn a working
        `memory_write` into an error. Returns True only if the file was
        actually rewritten.
        """
        try:
            with flock_excl(self.path):
                updated = fn(self._read_rows())
                if updated is None:
                    return False
                body = "".join(
                    json.dumps(row, separators=(",", ":")) + "\n" for row in updated
                )
                atomic_write_bytes(
                    self.path, body.encode("utf-8"), mode_before_rename=0o600
                )
                return True
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist pending writes to %s: %s", self.path, exc)
            return False

    def _read_rows(self) -> list[dict[str, Any]]:
        """Raw rows for every client. Malformed lines are skipped — and,
        as in `ProposalQueue.load`, that skip is destructive at the next
        `save`. Same blast-radius argument: nothing here is a durable
        memory, only a write the user has not yet confirmed."""
        path = self.path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(raw, dict) and isinstance(raw.get("pending_id"), str):
                rows.append(raw)
        return rows

    def load(self, client_key: str) -> list[PendingRow]:
        """Decoded ADOPTABLE rows belonging to `client_key`, oldest first.

        Consumed tombstones are filtered out: this is the feed
        `_adopt_persisted` walks, and the one thing a consumed id must
        never do is come back as something a restart can commit. Their
        only reader is `is_consumed`.
        """
        out: list[PendingRow] = []
        for raw in self._read_rows():
            if raw.get("client") != client_key:
                continue
            if _row_state(raw) == _ROW_CONSUMED:
                continue
            pending_id = str(raw["pending_id"])
            expired_at = raw.get("expired_at")
            expired_at = (
                float(expired_at) if isinstance(expired_at, (int, float)) else None
            )
            payload = raw.get("payload")
            pending: PendingWrite | None = None
            if expired_at is None and isinstance(payload, dict):
                try:
                    pending = PendingWrite(
                        pending_id=pending_id,
                        payload=_decode_payload(payload),
                        created_at=float(raw.get("created_at") or 0.0),
                        gate_flags=_normalize_gate_flags(raw.get("gate_flags")),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning(
                        "dropping unreadable pending write %s: %s", pending_id, exc
                    )
                    continue
            promo = raw.get("promotion")
            promotion = (
                (str(promo[0]), str(promo[1]))
                if isinstance(promo, list) and len(promo) == 2
                else None
            )
            out.append(PendingRow(pending_id, expired_at, pending, promotion))
        return out

    def is_consumed(self, client_key: str, pending_id: str) -> bool:
        """True only on POSITIVE evidence that `pending_id` was consumed.

        Absence answers False, deliberately. An id can be missing from the
        file because a sibling process committed it, or because the
        sidecar was never writable in the first place — and the second is
        the documented degraded mode, where the in-memory set is the only
        truth there is. Refusing on absence would turn an unwritable store
        root into "your staged write cannot be confirmed", which is
        exactly the error the best-effort contract promises never to
        produce.
        """
        return any(
            raw.get("client") == client_key
            and raw.get("pending_id") == pending_id
            and _row_state(raw) == _ROW_CONSUMED
            for raw in self._read_rows()
        )

    def append(
        self,
        client_key: str,
        *,
        session_id: str,
        pending: PendingWrite,
        promotion: tuple[str, str] | None = None,
    ) -> None:
        """Add one live row, leaving every other row alone.

        Refuses to write if the id already has ANY row — a fresh id
        cannot collide (16 bytes of `secrets`), so a collision here would
        mean a caller re-staging a consumed id, and the append is where
        that would resurrect it.
        """

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            for raw in rows:
                if (
                    raw.get("client") == client_key
                    and raw.get("pending_id") == pending.pending_id
                ):
                    return None
            rows.append(
                {
                    "client": client_key,
                    "session": session_id,
                    "pending_id": pending.pending_id,
                    "created_at": pending.created_at,
                    "expired_at": None,
                    "gate_flags": pending.gate_flags,
                    "promotion": list(promotion) if promotion is not None else None,
                    "payload": _encode_payload(pending.payload),
                }
            )
            return rows

        self._mutate(_fn)

    def set_promotion(
        self, client_key: str, pending_id: str, promotion: tuple[str, str]
    ) -> None:
        """Stamp the `episode_promote` linkage onto an existing live row.

        The linkage rides on the row rather than in a file of its own:
        without it a restart would carry the staged write across but not
        the linkage, so the eventual confirm would commit the memory AND
        leave the source episode on disk as a duplicate — a failure the
        pre-sidecar behaviour could not produce, because it lost the
        staged write too.
        """

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            changed = False
            for raw in rows:
                if (
                    raw.get("client") == client_key
                    and raw.get("pending_id") == pending_id
                    and _row_state(raw) == _ROW_LIVE
                ):
                    raw["promotion"] = list(promotion)
                    changed = True
            return rows if changed else None

        self._mutate(_fn)

    def claim(self, client_key: str, pending_id: str, *, session_id: str) -> bool:
        """Consume `pending_id` durably and exactly once. True if we won.

        The decision and the tombstone happen inside ONE `flock_excl`, so
        two processes confirming the same id serialise and only the first
        gets True. A live row is replaced by the tombstone; an id with no
        row at all is claimable (the degraded, never-persisted case);
        anything already tombstoned — consumed OR expired — is refused,
        because a stale in-memory snapshot is the only thing that could
        still be offering it.
        """
        won = True

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            nonlocal won
            kept: list[dict[str, Any]] = []
            for raw in rows:
                if (
                    raw.get("client") == client_key
                    and raw.get("pending_id") == pending_id
                ):
                    if _row_state(raw) != _ROW_LIVE:
                        won = False
                        return None
                    continue  # the live row becomes the tombstone below
                kept.append(raw)
            kept.append(
                {
                    "client": client_key,
                    "session": session_id,
                    "pending_id": pending_id,
                    "consumed_at": time.time(),
                }
            )
            return kept

        self._mutate(_fn)
        return won

    def mark_expired(
        self, client_key: str, expiries: dict[str, float], *, session_id: str
    ) -> None:
        """Turn live rows into expiry markers, keeping any existing verdict.

        An id already tombstoned is left exactly as it is: re-stamping an
        expiry marker would restart the `was_recently_expired` window at
        every server launch, and overwriting a CONSUMED marker would erase
        the record of a commit that already happened.
        """
        if not expiries:
            return

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            settled = {
                raw.get("pending_id")
                for raw in rows
                if raw.get("client") == client_key and _row_state(raw) != _ROW_LIVE
            }
            todo = {pid: when for pid, when in expiries.items() if pid not in settled}
            if not todo:
                return None
            kept = [
                raw
                for raw in rows
                if not (
                    raw.get("client") == client_key and raw.get("pending_id") in todo
                )
            ]
            for pending_id, when in todo.items():
                kept.append(
                    {
                        "client": client_key,
                        "session": session_id,
                        "pending_id": pending_id,
                        "expired_at": when,
                    }
                )
            return kept

        self._mutate(_fn)

    def gc(self, client_key: str, *, before: float) -> None:
        """Drop this client's tombstones older than `before`, and any live
        row too malformed to ever decode.

        Tombstones are what keeps the file from being a snapshot, so they
        cannot be dropped eagerly — but they cannot accumulate forever
        either. One full `_PENDING_TTL_SECONDS` past the tombstone is the
        safe horizon: any in-memory copy of that id has itself crossed the
        TTL by then and has been evicted, so there is nothing left that
        could resurrect it.
        """

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            kept: list[dict[str, Any]] = []
            for raw in rows:
                if raw.get("client") == client_key:
                    state = _row_state(raw)
                    stamp = raw.get("consumed_at") or raw.get("expired_at")
                    if state != _ROW_LIVE and isinstance(stamp, (int, float)):
                        if float(stamp) < before:
                            continue
                    elif state == _ROW_LIVE and not isinstance(
                        raw.get("payload"), dict
                    ):
                        continue
                kept.append(raw)
            return kept if len(kept) != len(rows) else None

        self._mutate(_fn)

    def clear(self, client_key: str) -> None:
        """Drop this client's adoptable rows — `SessionState.reset`'s half
        of "forget this session".

        Tombstones survive: reset is a statement about THIS state's memory,
        not a licence to make an already-committed id confirmable again.
        """

        def _fn(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            kept = [
                raw
                for raw in rows
                if not (
                    raw.get("client") == client_key and _row_state(raw) != _ROW_CONSUMED
                )
            ]
            return kept if len(kept) != len(rows) else None

        self._mutate(_fn)


@dataclass
class SessionState:
    """Mutable per-session state.

    Everything here resets when the server restarts EXCEPT the staged
    writes, which are mirrored to a `PendingWriteLog` once
    `bind_pending_log` has been called with a store root and read back on
    the next bind. Nothing else is durable: disabled scopes, use-tokens
    and the turn counter are all per-process by design.
    """

    session_id: str = field(default_factory=_new_session_id)
    # Which `SessionRegistry` bucket this state serves — the key the
    # pending-write sidecar files rows under, so a restart under the same
    # client reads back its own staged writes and no one else's. Left at
    # the default for a bare `SessionState`, which is what the stdio
    # transport and the tests both collapse to anyway.
    client_key: str = _DEFAULT_CLIENT_KEY
    disabled_scopes: set[str] = field(default_factory=set)
    pending_writes: dict[str, PendingWrite] = field(default_factory=dict)
    # `pending_use_tokens` is keyed by `memory_id` rather than token
    # value because the override path takes ids (the model rarely sees
    # the opaque token; it sees the memory it's overriding). Token
    # values stay opaque to consumers so they can't leak the id back
    # via the wire shape.
    pending_use_tokens: dict[str, PendingUseToken] = field(default_factory=dict)
    # Monotonic per-session turn counter. Bumped at the entry of every
    # memory_* tool call (via `advance_turn`). Auto-commit decisions
    # are based on the delta between the current turn and each
    # token's `issued_at_turn`.
    turn_counter: int = 0
    # Pending writes that hit `_PENDING_TTL_SECONDS` before confirmation.
    # Drained by `pop_recently_expired()` so handlers can emit one
    # `pending_expired` event per drop and `memory_write_confirm` can
    # tell "expired" apart from "never existed". Garbage-collected on
    # the same eviction pass once each id has been expired for one full
    # TTL — at that point the model has no live reference to it.
    _expired_pending: dict[str, "PendingWrite"] = field(default_factory=dict)
    _expired_pending_at: dict[str, float] = field(default_factory=dict)
    # Use-tokens the wall-clock safety net evicted before anything
    # settled them. Same stash-then-drain shape as `_expired_pending`
    # one field up, and for the same reason: a bare `del` made the loss
    # unobservable. Drained by `pop_expired_use_tokens()` so the handler
    # layer can emit one `use_token_expired` event per batch.
    #
    # Deliberately ONE map, with none of the second-TTL companion
    # bookkeeping `_expired_pending` carries above: that companion
    # exists solely so `was_recently_expired` can tell
    # `memory_write_confirm` "expired" from "never existed", and a
    # use-token has no equivalent return path — the model never hands a
    # token back (`memory_record_use` takes `memory_ids`). The drain is
    # the whole lifetime.
    _expired_use_tokens: dict[str, "PendingUseToken"] = field(default_factory=dict)
    # Tracks pending writes that originated from `episode_promote`. The
    # value is `(episode_session_id, episode_id)` — what the promote
    # handler needs to delete the source episode once the user
    # confirms. Without this, a `pending` round-trip would leak the
    # source episode past `memory_write_confirm`: the confirm handler
    # has no episode context on its own. Cleared on confirm (after the
    # delete), on cancel (preserving the episode so the user can
    # retry), and alongside `_expired_pending` when the TTL elapses.
    _promotion_episodes: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Memory ids already corroborated by a dedup-rejected write THIS
    # session. One conversation restating the same claim five times is
    # one recurrence, not five — the counter measures independent
    # re-entries, so the bump is once per (memory, session). In-memory
    # only: a server restart legitimately opens a new session, which is
    # a new opportunity to corroborate.
    corroborated_ids: set[str] = field(default_factory=set)
    # One-shot per-session marker for the passive curation-pressure
    # check that may inline a hint on the first `memory_write` of a
    # session. Set True the first time the check runs regardless of
    # whether the threshold was crossed — dead_weight, drifted, and
    # cold_endorsement_memories all accumulate across sessions and don't move
    # meaningfully within one, so a single check at first write is
    # the right cadence. Keeps the model from re-walking the event
    # log on every subsequent write.
    curation_hint_checked: bool = False
    # The disk mirror, once a handler has told this state which store it
    # belongs to (`bind_pending_log`). None means in-process only — the
    # behaviour every caller had before the sidecar existed, and still the
    # behaviour of any state nobody bound.
    _pending_log: "PendingWriteLog | None" = field(
        default=None, repr=False, compare=False
    )

    # ---- scopes ----------------------------------------------------------

    def disable(self, scope: str) -> set[str]:
        self.disabled_scopes.add(scope)
        return set(self.disabled_scopes)

    def enable(self, scope: str) -> set[str]:
        self.disabled_scopes.discard(scope)
        return set(self.disabled_scopes)

    # ---- pending writes --------------------------------------------------

    def bind_pending_log(self, root: Path) -> None:
        """Point this state at a store root and adopt what is already
        staged there for this client.

        Idempotent and cheap after the first call: a bound state re-reads
        nothing, because from that point on it is the writer. Call it
        BEFORE `advance_turn` on any handler that can touch pending state
        — a write that crossed its TTL while the process was down has to
        be in `pending_writes` when `_evict_expired` runs, or it lands in
        `_expired_pending` on the following turn instead of this one and
        its `pending_expired` event is a call late.
        """
        if self._pending_log is not None:
            return
        self._pending_log = PendingWriteLog(root)
        self._adopt_persisted()

    def _adopt_persisted(self) -> None:
        """Merge the sidecar's rows for this client into memory.

        In-memory always wins: a state that already holds an id ignores
        the disk copy of it, so adoption can never resurrect something
        this process just consumed.

        The TTL is applied HERE, not left to `_evict_expired`, because the
        two cases a restart produces are not the same case. A row whose
        payload outlived the window while the process was down is a fresh
        eviction — it goes into `_expired_pending` so the drain emits its
        `pending_expired` event. A row already recorded as expired keeps
        its ORIGINAL eviction time, so its `was_recently_expired` window
        ends when it was always going to end rather than being restarted
        by every server launch.
        """
        log_ = self._pending_log
        if log_ is None:
            return
        now = time.time()
        cutoff = now - _PENDING_TTL_SECONDS
        newly_expired: dict[str, float] = {}
        for row in log_.load(self.client_key):
            pid = row.pending_id
            if pid in self.pending_writes or pid in self._expired_pending_at:
                continue
            if row.expired_at is not None:
                if row.expired_at >= cutoff:
                    self._expired_pending_at[pid] = row.expired_at
                continue
            if row.pending is None:
                continue
            if row.pending.created_at < cutoff:
                self._expired_pending[pid] = row.pending
                self._expired_pending_at[pid] = now
                # Record the eviction on disk NOW rather than leaving the
                # stale live row for the next boot to re-judge: without
                # the marker, every restart re-derives "expired just now"
                # and the `was_recently_expired` window never closes.
                newly_expired[pid] = now
                continue
            self.pending_writes[pid] = row.pending
            if row.promotion is not None:
                self._promotion_episodes[pid] = row.promotion
        log_.mark_expired(self.client_key, newly_expired, session_id=self.session_id)
        # Compaction is a bind-time chore, not a per-call one, and it
        # rewrites nothing when there is nothing past the horizon.
        log_.gc(self.client_key, before=cutoff)

    def stage_write(
        self,
        payload: dict[str, Any],
        *,
        gate_flags: dict[str, Any] | None = None,
    ) -> PendingWrite:
        """Park a write request awaiting `memory_write_confirm`.

        `gate_flags` is the originating call's override set (see
        `GATE_FLAG_KEYS`); omitting it stages a write that the
        confirm-time re-gate judges with every escape hatch closed.
        """
        self._evict_expired()
        pending_id = "pending_" + secrets.token_hex(8)
        pending = PendingWrite(
            pending_id=pending_id,
            payload=payload,
            created_at=time.time(),
            gate_flags=_normalize_gate_flags(gate_flags),
        )
        self.pending_writes[pending_id] = pending
        if self._pending_log is not None:
            self._pending_log.append(
                self.client_key, session_id=self.session_id, pending=pending
            )
        return pending

    def _drop_local(self, pending_id: str) -> None:
        """Forget an id another writer already resolved. Local only — the
        durable record of what happened to it belongs to whoever won."""
        self.pending_writes.pop(pending_id, None)
        self._promotion_episodes.pop(pending_id, None)

    def peek_pending(self, pending_id: str) -> PendingWrite | None:
        """Look up a pending write WITHOUT consuming it.

        `memory_write_confirm` re-runs the content gates against the
        staged payload before it commits, and a `take_pending` ahead of
        that check would destroy the very write the check refuses:
        nothing left to re-confirm, and the promotion linkage orphaned
        against an id that no longer exists. Peek, judge, and only then
        take.

        The peek also consults the sidecar, so a state holding a stale
        in-memory copy of an id some other process has already committed
        answers None here — the confirm handler then raises its ordinary
        "no pending write with id" rather than re-running the gates on a
        write that is already in the store. `take_pending`'s claim is
        still the authority (it closes the window between this read and
        the commit); this is what makes the common case a clean error
        instead of a race the caller has to read a traceback to
        understand.
        """
        self._evict_expired()
        pending = self.pending_writes.get(pending_id)
        if pending is None:
            return None
        log_ = self._pending_log
        if log_ is not None and log_.is_consumed(self.client_key, pending_id):
            self._drop_local(pending_id)
            return None
        return pending

    def take_pending(self, pending_id: str) -> PendingWrite | None:
        """Consume a pending write. Returns None if missing/expired.

        Raises `PendingAlreadyConsumed` when the id IS in this state's
        memory but its durable claim is refused — i.e. another process
        sharing this client key and store root already committed or
        cancelled it. That case cannot be a quiet None: the confirm
        handler ignores this return value and commits the payload it
        peeked earlier, so a silent failure here is a second durable
        memory from one `memory_write`.
        """
        self._evict_expired()
        if pending_id not in self.pending_writes:
            return None
        log_ = self._pending_log
        if log_ is not None and not log_.claim(
            self.client_key, pending_id, session_id=self.session_id
        ):
            self._drop_local(pending_id)
            raise PendingAlreadyConsumed(
                f"pending write {pending_id!r} was already resolved by another "
                "session sharing this store (committed, cancelled or expired). "
                "Nothing was written. Re-stage with memory_write if the memory "
                "is still wanted."
            )
        return self.pending_writes.pop(pending_id)

    def cancel_pending(self, pending_id: str) -> bool:
        """Discard a pending write without committing. True if it existed.

        A cancel is a durable claim too: leaving the row live on disk
        would let a restart re-adopt the write the user just declined.
        A claim refused by another session's commit/cancel reports False
        — the id is resolved, just not by us.
        """
        if pending_id not in self.pending_writes:
            return False
        existed = True
        log_ = self._pending_log
        if log_ is not None:
            existed = log_.claim(
                self.client_key, pending_id, session_id=self.session_id
            )
        self.pending_writes.pop(pending_id, None)
        return existed

    # ---- pending promotion linkage --------------------------------------

    def stash_promotion_episode(
        self, pending_id: str, episode_session_id: str, episode_id: str
    ) -> None:
        """Remember that `pending_id` was staged by `episode_promote`.

        When the user later confirms the pending write, the confirm
        handler needs to delete the source episode so the journal entry
        doesn't survive past commit as a duplicate. The confirm handler
        has no episode context on its own (it only sees the pending id),
        so the promotion handler stashes the linkage here at staging
        time and the confirm handler reads it on commit. Cancel just
        drops the link — the episode stays so the user can retry."""
        self._promotion_episodes[pending_id] = (episode_session_id, episode_id)
        # The linkage is stashed AFTER `stage_write` has already persisted
        # the row, so it needs its own write-through. The removal paths all
        # ride along with a claim, which tombstones the row anyway.
        if self._pending_log is not None:
            self._pending_log.set_promotion(
                self.client_key, pending_id, (episode_session_id, episode_id)
            )

    def take_promotion_episode(self, pending_id: str) -> tuple[str, str] | None:
        """Pop and return `(episode_session_id, episode_id)` if `pending_id`
        was staged by `episode_promote`. Returns None otherwise."""
        return self._promotion_episodes.pop(pending_id, None)

    def discard_promotion_episode(self, pending_id: str) -> None:
        """Drop the promotion linkage for `pending_id` without acting on
        the source episode. Used by the cancel path so the episode
        remains intact for a retry. Idempotent — calling on an unknown
        id is a no-op."""
        self._promotion_episodes.pop(pending_id, None)

    def _evict_expired(self) -> None:
        """Move stale pending writes into the `_expired_pending` queue.

        The handler drains the queue via `pop_recently_expired()` and
        emits a `pending_expired` event for each entry — without that,
        the model has no way to tell that its 61-minute-later "yes,
        save it" confirmation lost the race with the TTL. Entries that
        have themselves been expired for one full TTL are GC'd here:
        once the original 1h window has passed twice, the model isn't
        plausibly still referencing the id.
        """
        now = time.time()
        cutoff = now - _PENDING_TTL_SECONDS
        stale = [pid for pid, p in self.pending_writes.items() if p.created_at < cutoff]
        for pid in stale:
            self._expired_pending[pid] = self.pending_writes.pop(pid)
            self._expired_pending_at[pid] = now
            # Drop any promotion linkage too — the pending payload
            # expired without confirmation, so the round-trip is
            # done. The source episode stays in place (the promote
            # handler's contract: any non-committed outcome leaves
            # the episode for the caller to retry from).
            self._promotion_episodes.pop(pid, None)
        gone = [pid for pid, at in self._expired_pending_at.items() if at < cutoff]
        for pid in gone:
            self._expired_pending_at.pop(pid, None)
            self._expired_pending.pop(pid, None)
        # Only when something moved: this runs at the entry of EVERY tool
        # call, and an unconditional rewrite would take the sidecar lock on
        # each one to write back what it just read.
        log_ = self._pending_log
        if log_ is None:
            return
        if stale:
            log_.mark_expired(
                self.client_key,
                dict.fromkeys(stale, now),
                session_id=self.session_id,
            )
        if gone:
            log_.gc(self.client_key, before=cutoff)

    def pop_recently_expired(self) -> list["PendingWrite"]:
        """Drain and return pending writes evicted since the last drain.

        Returned in insertion order. The handler emits one
        `pending_expired` event per entry; the entries themselves stay
        out of the live `pending_writes` map regardless. Idempotent —
        a second call returns an empty list until the next eviction.
        """
        drained = list(self._expired_pending.values())
        self._expired_pending.clear()
        # `_expired_pending_at` stays populated so `take_pending` can
        # still distinguish "recently expired" from "never existed"
        # for the duration of one TTL window.
        return drained

    def was_recently_expired(self, pending_id: str) -> bool:
        """True if `pending_id` was evicted within the past TTL window."""
        return pending_id in self._expired_pending_at

    # ---- turns and use-tokens -------------------------------------------

    def advance_turn(self) -> int:
        """Bump the per-session turn counter. Returns the new value.

        Called at the entry of every memory_* tool handler so the
        auto-commit pass has a stable monotonic clock to compare token
        ages against. Also evicts wall-clock-expired use-tokens AND any
        pending writes that crossed their TTL. Both evictions stash
        rather than delete — `_expired_use_tokens` and
        `_expired_pending` respectively — so the handler layer can
        drain each and emit one event per drop (`use_token_expired`,
        `pending_expired`), and the confirm handler can distinguish
        "expired" from "never existed."
        """
        self.turn_counter += 1
        self._evict_expired_use_tokens()
        self._evict_expired()
        return self.turn_counter

    def issue_use_tokens(self, memory_ids: list[str]) -> dict[str, str]:
        """Mint one opaque token per `memory_id`.

        Returns `{memory_id: token}`. Re-issuing for an id whose previous
        token is still pending replaces it: the search-then-search-again
        pattern shouldn't accumulate phantom tokens for the same memory.
        Called by `memory_search` and `memory_show` after their event
        records have been emitted.
        """
        out: dict[str, str] = {}
        now = time.time()
        for mid in memory_ids:
            token = "use_" + secrets.token_hex(8)
            self.pending_use_tokens[mid] = PendingUseToken(
                token=token,
                memory_id=mid,
                issued_at=now,
                issued_at_turn=self.turn_counter,
            )
            out[mid] = token
        return out

    def consume_old_tokens(
        self,
        *,
        ttl_turns: int = DEFAULT_USE_TOKEN_TTL_TURNS,
        min_age_seconds: float | None = None,
        override_ids: set[str] | None = None,
    ) -> list[str]:
        """Pop and return memory_ids whose tokens aged past BOTH TTL axes.

        A token is ready when it is older than `ttl_turns` handler
        entries AND older than `min_age_seconds` of wall clock (default
        `AUTO_COMMIT_MIN_AGE_SECONDS`; resolved at call time so tests
        can monkeypatch the module constant). The turn axis alone was
        the original design — and its clock is HANDLER ENTRIES, not
        conversational turns, so a tool-heavy turn advanced it fast
        enough to auto-commit this turn's own retrievals mid-turn,
        before the reply existed. That mid-turn commit starved the Stop
        hook's end-of-turn attribution pass (`hook.
        _emit_hook_attributions`) of every id it would have matched:
        ~98% of applied events on the 2026-07-03 dogfood store were
        bare autos. The wall-clock floor makes the hook — which fires
        seconds after the reply — the normal settlement path; this
        in-process pass remains the fallback for hookless deployments
        (their auto-commits now land on the first handler call ≥ the
        floor, still inside the 30-minute wall-clock eviction window).

        `override_ids` are excluded — used by the explicit
        `memory_record_use` path so a caller's deliberate choice
        beats the auto-commit. Returned ids are removed from the
        pending map atomically.

        The list is in deterministic order (insertion order, which on
        Python 3.7+ dict preserves) so the resulting auto-commit event
        is reproducible across runs.
        """
        if override_ids is None:
            override_ids = set()
        if min_age_seconds is None:
            min_age_seconds = AUTO_COMMIT_MIN_AGE_SECONDS
        cutoff_turn = self.turn_counter - ttl_turns
        age_cutoff = time.time() - min_age_seconds
        ready: list[str] = []
        for mid, tok in list(self.pending_use_tokens.items()):
            if mid in override_ids:
                continue
            if tok.issued_at_turn <= cutoff_turn and tok.issued_at <= age_cutoff:
                ready.append(mid)
        for mid in ready:
            del self.pending_use_tokens[mid]
        return ready

    def purge_use_token(self, memory_id: str) -> bool:
        """Drop the pending token for `memory_id`. True if one existed.

        Used by the explicit-override path so a `memory_record_use`
        for an id removes its token before the auto-commit pass can
        fire. Idempotent — calling on an unknown id is a no-op.
        """
        return self.pending_use_tokens.pop(memory_id, None) is not None

    def _evict_expired_use_tokens(self) -> None:
        """Move wall-clock-expired use-tokens into the
        `_expired_use_tokens` stash.

        This used to be a bare `del`, and that was the last silent loss
        left in the retrieval-settlement chain. A retrieval that no
        surface settled — no Stop-hook attribution, no explicit
        `memory_record_use`, and no in-process auto-commit because the
        session went idle before the next memory_* call — simply
        disappeared at the 30-minute mark. Downstream the store then
        read "retrieved, never applied", which is exactly the shape
        dead-weight curation punishes: the memory looked useless when
        in fact the evidence was thrown away.

        Stashing mirrors `_evict_expired`'s treatment of pending
        writes. The event itself is emitted handler-side
        (`handlers/_shared._emit_expired_use_tokens`) because this
        module stays dependency-free of the audit stack by design — the
        same rule that keeps `AUTO_COMMIT_MIN_AGE_SECONDS` a
        cross-pinned copy rather than an import of the audit constant.
        """
        cutoff = time.time() - _PENDING_USE_TOKEN_TTL_SECONDS
        stale = [
            mid
            for mid, tok in self.pending_use_tokens.items()
            if tok.issued_at < cutoff
        ]
        for mid in stale:
            self._expired_use_tokens[mid] = self.pending_use_tokens.pop(mid)

    def pop_expired_use_tokens(self) -> list["PendingUseToken"]:
        """Drain and return use-tokens evicted since the last drain.

        Returned in insertion order — oldest eviction first — so the
        `use_token_expired` event the handler builds from them is
        reproducible across runs. The tokens stay out of the live
        `pending_use_tokens` map regardless of what the caller does
        with them. Idempotent: a second call returns an empty list
        until the next eviction.

        Callers must drain unconditionally, before any
        telemetry-enabled check. That ordering (the one
        `_drain_pending_expired` already uses) is what keeps the stash
        bounded on a telemetry-disabled deployment, where the events
        are never written but the evictions still happen.
        """
        drained = list(self._expired_use_tokens.values())
        self._expired_use_tokens.clear()
        return drained

    # ---- lifecycle -------------------------------------------------------

    def reset(self) -> None:
        # `session_id` is intentionally NOT reset — it's a stable per-process
        # tag used in the event log, and a reset() is meant to clear in-memory
        # session state (disabled scopes, pending writes), not to "rotate" the
        # process identity. The turn counter likewise stays — resetting it
        # mid-session would mis-age any still-pending use-tokens.
        self.disabled_scopes.clear()
        self.pending_writes.clear()
        self.pending_use_tokens.clear()
        self._expired_pending.clear()
        self._expired_pending_at.clear()
        # Dropped rather than drained: a reset discards the live tokens
        # on the line above too, so emitting expiry events for the
        # stash would report losses for retrievals the caller just
        # declared irrelevant.
        self._expired_use_tokens.clear()
        self._promotion_episodes.clear()
        # The sidecar mirrors the live set, so a reset that only cleared
        # memory would be undone by the next `bind_pending_log`.
        if self._pending_log is not None:
            self._pending_log.clear(self.client_key)

    # ---- SessionSource protocol -----------------------------------------

    def for_request(self, ctx: "_Ctx | None") -> "SessionState":
        """Return this state regardless of `ctx`.

        Lets a bare `SessionState` satisfy the `SessionSource` protocol,
        so tests that construct a single state and pass it into
        `build_server(state=...)` keep working — the handlers call
        `state.for_request(ctx)` uniformly and get the same instance
        back. Per-client routing only kicks in when a `SessionRegistry`
        is used instead.
        """
        return self


class SessionSource(Protocol):
    """The minimum interface every tool handler depends on for state.

    Both `SessionState` (single-state, test-friendly) and
    `SessionRegistry` (per-client routing, production-friendly) satisfy
    this protocol, so `_register_tools` can take either without
    branching. `for_request(ctx)` is the entry point: pass the SDK
    `Context` (or `None` for an in-process call outside a request) and
    receive the right `SessionState` for this request.
    """

    def for_request(self, ctx: "_Ctx | None") -> SessionState: ...


class SessionRegistry:
    """Per-client `SessionState` map for multi-client server processes.

    Keys are the request's client id (or `_DEFAULT_CLIENT_KEY` when
    the transport doesn't supply one). States are created lazily on
    first `for_request` for a given key, so an idle client doesn't
    pre-allocate anything.

    Backed by an `OrderedDict` with an LRU eviction cap (`max_clients`,
    default 256): on each `for_request` an existing key is touched to
    the end via `move_to_end`, and inserting past the cap evicts the
    oldest entry. The cap matters under HTTP/SSE transports that fan
    arbitrary client_ids through one server process — without it, the
    map grows unbounded for the lifetime of the process. The stdio
    transport collapses every request into `_DEFAULT_CLIENT_KEY`, so a
    long-running stdio process sees a fixed map of size 1; the LRU is
    inert there.

    A `threading.Lock` guards mutations because the touch+evict pass
    is non-atomic (read, move, possibly pop) and HTTP/SSE transports
    can dispatch concurrent requests. The pre-LRU shape relied on
    `dict.setdefault` being atomic on CPython; the OrderedDict path
    can't replicate that without an explicit lock.

    The registry is intentionally a stateful object rather than a
    module-level singleton: tests construct fresh ones via
    `SessionRegistry()`, and the package-level `get_default_registry()`
    is the single production instance shared across `main()` calls in
    the same process.
    """

    # Default cap. 256 is generous — the typical case is one client
    # per server (stdio). HTTP/SSE deployments with more concurrent
    # clients should pass an explicit value sized to expected fan-out.
    DEFAULT_MAX_CLIENTS = 256

    def __init__(self, max_clients: int = DEFAULT_MAX_CLIENTS) -> None:
        self._states: "OrderedDict[str, SessionState]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_clients = max_clients
        self._evicted_count = 0

    def for_request(self, ctx: "_Ctx | None") -> SessionState:
        key = self._key_for_ctx(ctx)
        # The touch-on-access + insert-with-eviction pass mutates two
        # pieces of state and must be atomic against concurrent callers
        # — otherwise two requests for the same new client_id could
        # each insert a fresh SessionState and one would overwrite the
        # other (losing pending_writes / disabled_scopes / turn_counter).
        # The pre-LRU shape used `dict.setdefault`'s atomicity for the
        # same guarantee; the OrderedDict path needs an explicit lock.
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                # Touch-on-access: move the existing entry to the
                # end so it's the most-recently-used.
                self._states.move_to_end(key)
                return state
            # `client_key` travels onto the state because the pending-write
            # sidecar files rows under it — a state that didn't know its own
            # bucket would have to key the disk mirror by the per-process
            # `session_id`, which no restart can ever match back.
            state = SessionState(client_key=key)
            self._states[key] = state
            # Evict the oldest entry if we crossed the cap. `popitem(last=False)`
            # removes the front of the OrderedDict — the least-recently-used.
            if len(self._states) > self.max_clients:
                self._states.popitem(last=False)
                self._evicted_count += 1
            return state

    @staticmethod
    def _key_for_ctx(ctx: "_Ctx | None") -> str:
        if ctx is None:
            return _DEFAULT_CLIENT_KEY
        try:
            meta = ctx.request_context.meta
            client_id = meta.get("client_id") if meta is not None else None
        except (AttributeError, ValueError):
            # `ctx.request_context` may raise ValueError if no request is
            # in progress (a Context constructed outside a tool call).
            # Treat that as "no identifier" and bucket into the default;
            # the alternative would be to crash the tool call for a
            # degenerate context shape. AttributeError covers the stand-in
            # contexts the suite forges, which carry no request context.
            #
            # mcp 1.x exposed this as a `Context.client_id` property that
            # did the same `getattr(request_context.meta, ...)` read; 2.x
            # dropped the property, and `meta` went from a pydantic model
            # to `RequestParamsMeta`, an open TypedDict (`extra_items=Any`)
            # that round-trips arbitrary keys — so the key is still
            # reachable, by mapping read instead of attribute read.
            return _DEFAULT_CLIENT_KEY
        if not client_id:
            return _DEFAULT_CLIENT_KEY
        return str(client_id)

    def known_keys(self) -> set[str]:
        """Snapshot of the registered session keys. Test-only; lets
        suites assert that two distinct clients produced two distinct
        states, or that one client reused the same state across calls."""
        with self._lock:
            return set(self._states)

    def stats(self) -> dict[str, int]:
        """Debug-visible counters: current size, lifetime evictions, cap.

        Exposed so HTTP-transport deployments can spot a runaway
        client_id fan-out (where `evicted` climbs monotonically). The
        snapshot is a point-in-time read under the lock; the values
        may drift the moment the lock is released.
        """
        with self._lock:
            return {
                "size": len(self._states),
                "evicted": self._evicted_count,
                "max_clients": self.max_clients,
            }


# Process-wide default registry. `main()` uses this; tests that want
# isolation construct their own via `SessionRegistry()`.
_default_registry = SessionRegistry()


def get_default_registry() -> SessionRegistry:
    """Return the process-wide default registry.

    Production code reaches for this when no explicit registry was
    passed; tests construct their own to keep per-test state
    isolated. The registry is mutable, so callers that depend on
    starting clean should construct their own — don't rely on
    `get_default_registry()` being empty.
    """
    return _default_registry


def get_state() -> SessionState:
    """Back-compat shim for callers that want a single `SessionState`.

    Equivalent to `get_default_registry().for_request(None)` — returns
    the "no-client-id" entry of the process-wide registry. Existing
    tests and the stdio-transport entry point keep working; new code
    that wants per-client isolation should call `for_request(ctx)` on
    a registry instead.
    """
    return _default_registry.for_request(None)
