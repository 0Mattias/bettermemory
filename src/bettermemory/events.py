"""The telemetry recorder: one row per tool call, into the store's log.

Every tool handler holds one `Recorder`, constructed once per server
process, and calls `record()` once per invocation. The recorder carries
what the store's `record_event` does not know on its own: the session
and the worktree an event belongs to (the request's own when the caller
declared them, else the process's), and the admin attribution a CLI
command stamps on its writes. `enabled=False` (`[telemetry] enabled =
false`) makes every measurement call a no-op, so handlers call
unconditionally. A row is one of two classes (`RowClass`, the one list
the writer, health and a pruning read): measurement, opt-out and
prunable; or state, recorded whatever the switch says and never pruned.
State is what the product reads back (`STATE_KINDS`: the shows, the
uses, the Stop's audit, the recall's and the review's rows) and the
audit trail SECURITY.md promises (`is_audit_row`: the standing guard's
row and the override evidence on a write or an update).

A measurement row that fails to land is swallowed at WARNING:
measurement never fails the tool call it describes. A state or audit row
is not best-effort: a write that meets another writer's lock is tried
again (`STATE_WRITE_ATTEMPTS`), and a failure that remains is logged and
raised, so a tool call answers ``store_busy`` and a hook fails with its
one stderr line rather than the row going missing in silence. Query
text is never stored verbatim: `query` and `probe_query` are redacted to
a hash, a short preview and a length, with known secret shapes stripped
first, before the row is signed; with telemetry off a state row keeps
the hash and the length and no preview.
"""

from __future__ import annotations

import contextlib
import contextvars
import enum
import hashlib
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import identity
from .credentials import secret_spans

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.events")


# Fields whose values are model/user-typed free text and may carry
# secrets. Redacted in `Recorder.record` when
# `telemetry.log_queries_verbatim = false` (the default since 2.6.8).
# Each value is replaced with `{"hash": "<sha256-prefix>", "preview":
# "<32 chars>", "len": N}` — cross-event correlation by hash works,
# the first 32 characters survive for triage, no raw body lands.
_REDACTED_TEXT_FIELDS = frozenset({"query", "probe_query"})
_QUERY_PREVIEW_CHARS = 32
_QUERY_HASH_PREFIX = 16

# Defense-in-depth pattern strip for known secret shapes. The 32-char
# preview alone can capture entire short tokens — a GitHub PAT
# (`ghp_<36chars>`) or an AWS access key (`AKIA<16chars>`) easily fits
# inside the preview window, and an OpenAI / Anthropic secret can have
# enough of its high-entropy tail land in the preview to be usable.
# The query log is local 0o600, so the primary defense is filesystem
# permissions; pattern-strip closes the gap when logs leave that
# perimeter (a `bettermemory eval` export, an attached transcript,
# a shared bug report). Patterns are applied BEFORE the 32-char
# truncation so the truncation never captures a partial secret.
#
# These five run first and keep their markers, so a query they strip
# reads in the log exactly as it read before the second pass existed.
# The second pass is the write gate's own detector list
# (`credentials.secret_spans`), which covers what these five never knew
# (`gho_`/`ghs_` tokens, `sk-proj-` keys, Stripe, Slack, Google, the
# `password=…` assignment and the rest) and every shape it learns
# later: the preview and the gate read one list. The five stay because
# they are wider than the detectors in places a redaction can afford to
# be (an all-lowercase `sk-` body, a masked `AKIA` run), and because
# their markers are already in signed logs.
#
# Order matters: the more-specific Anthropic key pattern runs before
# the generic `sk-` pattern so `sk-ant-…` is labelled correctly rather
# than caught as a generic OpenAI key.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED:openai-key]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-pat]"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[REDACTED:aws-access-key]"),
)


# The tool kinds stamped with the caller's harness session
# (`identity.Caller.harness_session`) when it declared one: the reads and
# writes the recall surface and the Stop settlement join to the hook rows
# of the same conversation. Every other kind keeps its field set; the
# Stop audit's own rows (`turn_audited`, `search_miss`) above all.
HARNESS_STAMPED_KINDS: frozenset[str] = frozenset(
    {"show", "search", "use", "write", "update", "verify"}
)

#: The field the harness session is stamped under.
HARNESS_SESSION_FIELD = "harness_session"


# Audit rows: the rows `[telemetry] enabled = false` does not switch off.
# Telemetry measures how the tools are used, and a user may decline to
# be measured. An audit row records that a guard was passed on the
# user's say-so or by an escape hatch, and SECURITY.md promises it is on
# the log ("Every such change is one signed `standing_guard` row … so a
# wrong one is found and reverted"). Before 9.0.0rc12 the recorder's
# switch dropped both kinds with the rest, so a store with telemetry off
# changed standing memories and committed forced or acknowledged writes
# with no trail at all, while the guard itself still ran.
#
#: The kinds that are audit rows whatever their fields: the standing
#: guard's row (`handlers._guard.record`), one per change the user's
#: quoted words carried.
AUDIT_KINDS: frozenset[str] = frozenset({"standing_guard"})

#: The kinds whose rows are audit rows when they carry override evidence:
#: memory_write's and memory_update's events, which name the escape
#: hatches a committed change used (`forced`, `credentials_acknowledged`,
#: `markers_acknowledged`, `user_claims_acknowledged`,
#: `truncation_acknowledged`). A row of these kinds with no hatch used
#: (`forced=False`, every `*_acknowledged` empty or false) is telemetry.
OVERRIDE_EVIDENCE_KINDS: frozenset[str] = frozenset({"write", "update"})

#: The suffix every override-evidence field but `forced` carries. The
#: scope-mismatch gate's is `scope_mismatches_acknowledged` (ENG-108): the
#: mismatch kinds `acknowledge_scope_mismatch` waved through.
_ACKNOWLEDGED_SUFFIX = "_acknowledged"

#: The statuses that make a row of `OVERRIDE_EVIDENCE_KINDS` an audit row
#: whatever else it carries: a call `[policy]` refused for passing a flag
#: the install forbids (`policy.REFUSAL_STATUS`, ENG-108). Nothing was
#: written, and the row is the trail that the agent reached for a hatch
#: the user or the organisation took away.
AUDIT_STATUSES: frozenset[str] = frozenset({"policy_refused"})


def is_audit_row(kind: str, fields: Mapping[str, Any]) -> bool:
    """Whether a `kind` row with `fields` is an audit row, recorded with
    telemetry off: a standing guard row, a write or update that used
    an escape hatch (`force=True`, or an acknowledge flag that waved a
    gate's hit through), or one `[policy]` refused (`AUDIT_STATUSES`).
    Read on the fields as the handler passes them, so a new
    `*_acknowledged` field on either kind is covered without a second
    list to keep in step."""
    if kind in AUDIT_KINDS:
        return True
    if kind not in OVERRIDE_EVIDENCE_KINDS:
        return False
    if fields.get("forced") is True or fields.get("status") in AUDIT_STATUSES:
        return True
    return any(
        bool(value)
        for key, value in fields.items()
        if key.endswith(_ACKNOWLEDGED_SUFFIX)
    )


# Product state: the rows the product reads back to decide what it does
# next (ENG-108). Before 9.0.0rc14 they were telemetry like the rest, so
# `[telemetry] enabled = false`, which an organisation may pin, left the
# session brief and the prompt recall showing nothing, a contradicted
# memory surfacing again and a review chain impossible to find; and with
# telemetry on, a write that met another writer's lock dropped the row
# with one log line.
#
#: The kinds whose every row is product state: recorded whatever
#: `[telemetry] enabled` says, written with a retry and raised when it
#: cannot land (`Recorder.record`), and never pruned while a reader below
#: can still read it. A pruning (ENG-82) skips every row `is_state_row`
#: answers true for. The readers, by kind:
#:
#: - ``show``: the prompt recall's seen gate (`recall._seen_in_log`) and
#:   the close of a segment, which leaves an opened pointer unsettled
#:   (`recall_settle.settle_unused`).
#: - ``use``, every outcome: a ``contradicted`` one holds the record back
#:   from both recall surfaces until it is updated or verified
#:   (`Store.contradicted_at`); a shown pointer's settlement, the auto
#:   settlement's dedup and the session's rebuild read the rest.
#: - ``turn_audited``: the Stop's row, which opens the next turn's window
#:   (`recall_settle.previous_audit_at`), names the turns already audited
#:   (`settle_interrupted`) and is a session's first sight in the rebuild.
#: - ``session_brief``, ``prompt_recall``, ``segment_opened`` and
#:   ``review_unanswered``: the rows a session's recall state is rebuilt
#:   from (`recall_state._STATE_KINDS`, with ``turn_audited`` and ``use``).
#: - ``review_issued``: a review chain (`review.find_chain`).
#: - ``scope_disable`` and ``scope_enable``: the scopes a session turned
#:   off, which the hooks' probe and the prompt recall leave out
#:   (`sessions._disabled_scopes_from_events`).
STATE_KINDS: frozenset[str] = frozenset(
    {
        "prompt_recall",
        "review_issued",
        "review_unanswered",
        "scope_disable",
        "scope_enable",
        "segment_opened",
        "session_brief",
        "show",
        "turn_audited",
        "use",
    }
)

#: The kinds whose row is state when it answers a review: a memory_write
#: row carrying the ``review_id`` it answered, which closes the chain
#: (`review.find_chain`'s ``decided``).
REVIEW_ANSWER_KINDS: frozenset[str] = frozenset({"write"})

#: The field a review answer's row names its review under.
REVIEW_ID_FIELD = "review_id"

#: How many times a state or audit row is tried against a store another
#: writer holds (SQLITE_BUSY or SQLITE_LOCKED) before the failure is
#: raised: the first try and one more. Each try waits for the lock up to
#: the connection's busy timeout (`Store.set_busy_timeout`, 5 s for each
#: role), so a row costs its call at most twice that, which stays under
#: the hook client's 15 s wait (`_daemon_client.HOOK_TIMEOUT_SECONDS`).
#: A row recorded inside another call's transaction is tried once: that
#: transaction already holds the lock.
STATE_WRITE_ATTEMPTS = 2


def is_state_row(kind: str, fields: Mapping[str, Any]) -> bool:
    """Whether a `kind` row with `fields` is product state or an audit
    row: recorded whatever `[telemetry] enabled` says, not best-effort,
    and never pruned while live (`STATE_KINDS`)."""
    if kind in STATE_KINDS or is_audit_row(kind, fields):
        return True
    return kind in REVIEW_ANSWER_KINDS and isinstance(fields.get(REVIEW_ID_FIELD), str)


class RowClass(enum.Enum):
    """The two classes of row the event log holds (ENG-108), and what
    each promises. `row_class` puts every row in one of them; the writer
    (`Recorder.record`), health's override rate (`health_stats`) and a
    pruning of the log (`prunable`) read that answer and nothing else, so
    the three cannot disagree about a row.

    ``STATE``: a row the product reads back to decide what it does next
    (`STATE_KINDS`, each with its reader), a review's answer
    (`REVIEW_ANSWER_KINDS` with a ``review_id``), and the audit trail
    SECURITY.md promises (`is_audit_row`: the standing guard's row, a
    write or an update that used an escape hatch). Recorded whatever
    `[telemetry] enabled` says, with no query preview when it is off;
    written with `STATE_WRITE_ATTEMPTS` against a busy store and raised
    when it cannot land, so the call that needed it fails rather than
    the row going missing; never prunable.

    ``MEASUREMENT``: a row that measures how the tools are used
    (`MEASUREMENT_KINDS`): searches, plain writes and updates, the miss
    telemetry, the grades, the held recalls. Opt-out: with telemetry off
    none is recorded. Best-effort: a write that fails is logged at
    WARNING and the call it describes goes on. Prunable at any age:
    nothing the product does reads one back, and a reader of one (health,
    eval, a memory's history) reports on what is there.
    """

    STATE = "state"
    MEASUREMENT = "measurement"


#: The kinds whose row is measurement unless its fields make it state
#: (`row_class`): a ``write`` or an ``update`` that used an escape hatch
#: is an audit row, and a ``write`` that answers a review is state.
MEASUREMENT_KINDS: frozenset[str] = frozenset(
    {
        "answer_standing",
        "capture_grade",
        "conflict_scan",
        "conflict_verdict",
        "episode_handoff",
        "episode_write",
        "list_tombstones",
        "miss_ack",
        "recall_grade",
        "recall_held",
        "remove",
        "rename_scope",
        "restore",
        "review_grade",
        "search",
        "search_miss",
        "silent_miss_cutoff",
        "update",
        "use_token_expired",
        "verify",
        "write",
    }
)

#: Every kind the product records, under the class its row has when its
#: fields promote it to nothing. A kind no list names is measurement
#: (`row_class`), the safe default for a row nothing reads back; the
#: test that scans the recording sites (tests/test_row_classes.py)
#: fails until a new kind is listed under one class.
ROW_KINDS: Mapping[RowClass, frozenset[str]] = MappingProxyType(
    {
        RowClass.STATE: STATE_KINDS | AUDIT_KINDS,
        RowClass.MEASUREMENT: MEASUREMENT_KINDS,
    }
)


def row_class(kind: str, fields: Mapping[str, Any]) -> RowClass:
    """The class of a `kind` row with `fields` (`RowClass`): state when
    `is_state_row` says so, as a measurement kind's row is when it
    carries override evidence or answers a review; measurement
    otherwise."""
    return RowClass.STATE if is_state_row(kind, fields) else RowClass.MEASUREMENT


def prunable(kind: str, fields: Mapping[str, Any]) -> bool:
    """Whether a pruning of the event log may drop a `kind` row with
    `fields`: a measurement row may, at any age; a state row may not.
    The pruning (ENG-82) is not on this branch; this is the rule it
    reads, so a row the brief, the recall, the contradiction, a review
    chain or the audit trail reads back is never the one it drops."""
    return row_class(kind, fields) is RowClass.MEASUREMENT


# A dry run (`hook.run_audit(dry_run=True)`, `bettermemory audit-turn
# --dry-run`) computes everything and records nothing, of any class.
# Telemetry off no longer does that, since state rows pass the switch.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bettermemory_events_suppressed", default=False
)


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Record no row at all inside the block, state and audit rows
    included: what a dry run is. Scoped to the calling context, so a
    daemon's other workers keep recording."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def _without_previews(fields: dict[str, Any]) -> None:
    """Drop the preview from each redacted text field of `fields`, in
    place, keeping its hash and length: a state row recorded with
    telemetry off carries no prompt text. The hash is what its readers
    join on (the re-audit dedup, the recall review)."""
    for key in _REDACTED_TEXT_FIELDS:
        value = fields.get(key)
        if isinstance(value, str):
            value = redact_query(value)
        if isinstance(value, Mapping) and "preview" in value:
            fields[key] = {k: v for k, v in value.items() if k != "preview"}


#: The marker the second pass splices in for a span the five patterns
#: above left: the detector's own kind (`credentials.secret_spans`), in
#: the five patterns' spelling.
_DETECTOR_MARKER = "[REDACTED:{kind}]"


def _strip_known_secrets(text: str) -> str:
    """Replace known secret token shapes with redaction markers.

    Applied before the 32-char preview is taken so the preview never
    captures a partial token. The hash is also computed on the
    secret-stripped text so a repeated query with the same secret
    still correlates, but the secret bytes don't feed into the hash
    input either.

    Two passes: the five legacy patterns with their markers, then every
    span the write gate's detectors still see in what is left (see the
    comment on `_SECRET_PATTERNS`). The second pass reads the first's
    output, so a marker is never cut by a later splice, and a text in
    which neither pass finds a shape comes back as the same string: its
    hash, preview and length are what they were before the second pass
    existed. A legacy marker alone does not read as a secret to the
    second pass: the markers carry no digit, which the generic
    assignment rule needs, and no vendor prefix.

    The second pass joins the pieces once rather than splicing each span
    into a fresh copy of the text: a query is as long as the transport
    lets it be, and a copy per span made a 4 MB query of tokens cost
    seconds.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    spans = secret_spans(text)
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, kind in spans:
        pieces += (text[cursor:start], _DETECTOR_MARKER.format(kind=kind))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redact_query(text: str) -> dict[str, Any]:
    """Replace a free-text field with a structured redaction.

    Shape: ``{"hash": "<16-hex-prefix>", "preview": "<first 32 chars>",
    "len": <total length>}``. The hash lets a consumer correlate
    repeated queries without seeing them; the preview is enough to
    triage what kind of query it was (e.g. "kubernetes networking"
    survives, "my-api-key=sk-…" survives only its first 32 chars
    rather than the full secret). The full text is not recoverable
    from the event log.

    Known token shapes (every shape the write gate's credential
    detectors know, plus the five legacy patterns) are stripped to
    opaque markers BEFORE the 32-char preview is taken, so the preview
    never carries a partial high-entropy secret. The pre-strip text
    length is retained as ``len`` so downstream triage can still see
    "this query was 87 chars" without seeing what those chars were.
    """
    original_len = len(text)
    stripped = _strip_known_secrets(text)
    digest = hashlib.sha256(stripped.encode("utf-8", errors="replace")).hexdigest()
    return {
        "hash": digest[:_QUERY_HASH_PREFIX],
        "preview": stripped[:_QUERY_PREVIEW_CHARS],
        "len": original_len,
    }


def _redact_event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `fields` with redacted-text fields replaced.

    Non-string values pass through untouched — the field set isn't
    guaranteed across producers and we don't want to silently drop a
    legitimate non-string payload.
    """
    out = dict(fields)
    for key in _REDACTED_TEXT_FIELDS:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = redact_query(value)
    return out


@dataclass
class Recorder:
    """The per-process telemetry writer over a store.

    `attribution` is stamped on every event when set: the CLI records
    under a throwaway session id, often under kinds a live client session
    also emits, and `eval.is_admin_recorded_event` reads the `cli_` prefix
    of this field to keep those rows out of the client census.
    `triggered_from` marks an out-of-process writer (the hooks) the same
    way, so the session-anchoring walk can tell a hook's rows from the
    server's.
    """

    store: Store
    session_id: str
    enabled: bool = True
    worktree_root: str | None = None
    attribution: str = ""
    triggered_from: str = ""

    def session_for_request(self) -> str:
        """The session the current request belongs to: the caller's own
        when it declared one through the transport or the shim's
        `x-bettermemory-session` (one per conversation), else this
        process's. A daemon serves every conversation on the machine from
        one process, so its own id would merge them all."""
        actor = identity.current_actor()
        if (
            actor is not None
            and actor.session is not None
            and actor.sources.get("session")
            in (identity.SOURCE_TRANSPORT, identity.SOURCE_HEADER)
        ):
            return actor.session
        return self.session_id

    def worktree_for_request(self) -> str | None:
        """The worktree of the workspace the current request declared, else
        the one this process was started in. A daemon's own directory says
        nothing about the client it is answering."""
        if identity.workspace_declaration() is None:
            return self.worktree_root
        from . import _handlers as _h

        return _h.capture_origin().worktree_root

    def records(self, kind: str, fields: Mapping[str, Any] | None = None) -> bool:
        """Whether `record(kind, **fields)` writes a row (`RowClass`): a
        state row always, a measurement row with telemetry on, nothing
        inside `suppressed`. A caller whose row costs work to build asks
        first rather than reading the switch itself."""
        if _SUPPRESSED.get():
            return False
        return self.enabled or row_class(kind, fields or {}) is RowClass.STATE

    def record(self, kind: str, **fields: Any) -> None:
        """Record one `kind` row, by its class (`row_class`). A
        measurement row is a no-op with telemetry off and swallowed at
        WARNING when it fails. A state or audit row passes the switch, is stamped and
        signed exactly as it would be with telemetry on (less the query
        preview when it is off), is tried `STATE_WRITE_ATTEMPTS` times
        against a busy store, and a failure that remains is logged and
        raised; a store open for reading only records nothing, with one
        warning, as before (`Store.record_event`). Inside `suppressed`
        nothing is recorded."""
        if _SUPPRESSED.get():
            return
        # Off means no measurement, not no state and no trail.
        state = row_class(kind, fields) is RowClass.STATE
        if not self.enabled and not state:
            return
        if self.attribution:
            fields.setdefault("attribution", self.attribution)
        if self.triggered_from:
            fields.setdefault("triggered_from", self.triggered_from)
        if "worktree_root" not in fields:
            worktree_root = self.worktree_for_request()
            if worktree_root is not None:
                fields["worktree_root"] = worktree_root
        if kind in HARNESS_STAMPED_KINDS:
            harness_session = identity.current_harness_session()
            if harness_session is not None:
                fields.setdefault(HARNESS_SESSION_FIELD, harness_session)
        # A handler that names the actor itself wins over the published
        # one; the field must leave `fields`, or the store sees it twice.
        actor = fields.pop("actor", None)
        if actor is None:
            actor = identity.current_actor()
        # A field named `session` names the session the event belongs to
        # (the hooks record under the transcript's id); the request's
        # session is the default.
        session = (
            fields.pop("session") if "session" in fields else self.session_for_request()
        )
        if not state:
            try:
                self.store.record_event(kind, session=session, actor=actor, **fields)
            except Exception as exc:  # noqa: BLE001 - measurement never fails the call
                log.warning("event %s not recorded: %s", kind, exc)
            return
        if not self.enabled:
            _without_previews(fields)
        try:
            self.store.record_event(
                kind,
                session=session,
                actor=actor,
                best_effort=False,
                attempts=STATE_WRITE_ATTEMPTS,
                **fields,
            )
        except Exception as exc:
            log.warning(
                "state row %s not recorded, the call fails: %s: %s",
                kind,
                exc.__class__.__name__,
                exc,
            )
            raise


def _event_id_items(value: Any) -> list[tuple[int, str]]:
    """Normalize an event's id-list field (`returned` / `memory_ids` /
    `hit_ids` / `ids`) to `(original_index, id)` pairs before iteration.

    The event log is plaintext, git-synced, and hand-editable, so every
    consumer must survive a malformed field: a numeric scalar raises
    `TypeError` under `for mid in <scalar>`, a bare string iterates by
    CHARACTER (mis-attributing per-char counts), and a well-formed list
    whose ELEMENTS are lists/dicts (`ids=[[id]]`) passes a container
    check but blows up at the first hash/lookup of the element. One bad
    event in the active log would otherwise take down every consumer of
    the walk — memory_health blanked this way in 3.14.x, and
    memory_search / memory_audit_turn did in 3.15.0 via their own raw
    reads. This is the single choke point: consumers never iterate the
    raw field.

    The ORIGINAL index is preserved so parallel arrays recorded alongside
    the ids (`claim_excerpts` on `use` events) still attribute to the
    right slot after malformed elements are dropped — compacting the list
    would silently shift every claim after a dropped element onto the
    wrong memory. A lone non-empty string is treated as a single id at
    slot 0; every other non-list shape coerces to empty.
    """
    if isinstance(value, list):
        return [(i, v) for i, v in enumerate(value) if isinstance(v, str)]
    if isinstance(value, str) and value:
        return [(0, value)]
    return []


def _event_id_list(value: Any) -> list[str]:
    """`_event_id_items` without the indices — for consumers that only
    tally per-id and carry no parallel arrays. Same normalization, same
    guarantees; see `_event_id_items` for the rationale."""
    return [v for _, v in _event_id_items(value)]


__all__ = [
    "AUDIT_KINDS",
    "AUDIT_STATUSES",
    "HARNESS_SESSION_FIELD",
    "HARNESS_STAMPED_KINDS",
    "MEASUREMENT_KINDS",
    "OVERRIDE_EVIDENCE_KINDS",
    "REVIEW_ANSWER_KINDS",
    "REVIEW_ID_FIELD",
    "ROW_KINDS",
    "Recorder",
    "RowClass",
    "STATE_KINDS",
    "STATE_WRITE_ATTEMPTS",
    "is_audit_row",
    "is_state_row",
    "prunable",
    "redact_query",
    "row_class",
    "suppressed",
]
