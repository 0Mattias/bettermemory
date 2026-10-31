"""The telemetry recorder: one row per tool call, into the store's log.

Every tool handler holds one `Recorder`, constructed once per server
process, and calls `record()` once per invocation. The recorder carries
what the store's `record_event` does not know on its own: the session
and the worktree an event belongs to (the request's own when the caller
declared them, else the process's), and the admin attribution a CLI
command stamps on its writes. `enabled=False` (`[telemetry] enabled =
false`) makes every measurement call a no-op, so handlers call
unconditionally. A row is one of two classes by its kind (`RowClass`,
read off the one table `STATE_FIELDS` by the writer and a pruning):
measurement, opt-out and prunable; or state, recorded whatever the switch
says and never pruned. State is every kind a product module reads back
(the shows, searches, writes, updates and verifies, the uses, the Stop's
audit and its misses, the recall's, the review's and the journal's rows)
and the audit trail SECURITY.md promises (`is_audit_row`: the standing
guard's row and the override evidence on a write or an update). With
telemetry off a state row keeps only the fields its readers read
(`state_fields`), so no free text lands.

A measurement row that fails to land is swallowed at WARNING:
measurement never fails the tool call it describes. A state or audit row
is not best-effort: a write that meets another writer's lock is tried
again (`STATE_WRITE_ATTEMPTS`), and a failure that remains is logged and
raised, so a tool call answers ``store_busy`` and a hook fails with its
one stderr line rather than the row going missing in silence; inside a
hook no try outlasts the hook's deadline (`within_hook_budget`). Query
text is never stored verbatim: `query` and `probe_query` are redacted to
a hash, a short preview and a length, with known secret shapes stripped
first, before the row is signed; with telemetry off the one query a
state row keeps (the Stop's `probe_query`) is its hash and its length.
"""

from __future__ import annotations

import contextlib
import contextvars
import enum
import functools
import hashlib
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

from . import identity
from .credentials import secret_spans

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.events")

_P = ParamSpec("_P")
_R = TypeVar("_R")


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


# Audit rows: the trail SECURITY.md promises. An audit row records that a
# guard was passed on the user's say-so or by an escape hatch ("Every such
# change is one signed `standing_guard` row … so a wrong one is found and
# reverted"), or that a gate refused what a hatch passes. Before 9.0.0rc12
# the recorder's switch dropped them with the rest of telemetry, so a store
# with telemetry off changed standing memories and committed forced or
# acknowledged writes with no trail at all, while the guard itself still
# ran. Their kinds are state kinds (`STATE_FIELDS`), recorded whatever
# `[telemetry] enabled` says; `is_audit_row` names the rows among them that
# are the trail, which health's override rate counts and whose evidence
# telemetry off keeps.
#
#: The kinds that are audit rows whatever their fields: the standing
#: guard's row (`handlers._guard.record`), one per change the user's
#: quoted words carried.
AUDIT_KINDS: frozenset[str] = frozenset({"standing_guard"})

#: The kinds whose rows are audit rows when they carry override evidence:
#: memory_write's and memory_update's events, which name the escape
#: hatches a committed change used (`forced`, and every field ending in
#: `_ACKNOWLEDGED_SUFFIX`: `credentials_acknowledged`,
#: `markers_acknowledged`, `user_claims_acknowledged`,
#: `scope_mismatches_acknowledged`, `truncation_acknowledged`), a refusal
#: such a hatch passes (`OVERRIDABLE_REFUSALS`) or one `[policy]` refused
#: (`AUDIT_STATUSES`). Every row of the two kinds is state; one with none
#: of these is no audit row.
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

#: The gate refusals an escape hatch passes, by status, each with the
#: hatch that passes it: the fire side of an override, as `forced` and the
#: `*_acknowledged` fields on a committed row are its other side. A row of
#: `OVERRIDE_EVIDENCE_KINDS` with one of these statuses is an audit row
#: (ENG-108), so health's override rate (`health_model.MarkerStats`:
#: overrides over fires plus overrides) counts both sides off rows the
#: writer records under one rule. Before 9.0.0rc14 the refusals were
#: measurement and the overrides audit rows, so the rate read 1.0 with
#: telemetry off, every fire dropped and every override kept, and above
#: its true value whenever a busy store dropped a fire. A refusal no flag
#: passes (`control_token_warning`, an update's `stale`) is no fire. The
#: gates spell their statuses as literals; tests/test_override_rate.py
#: ties this table to the write handler's refusal statuses and to
#: memory_update's.
OVERRIDABLE_REFUSALS: Mapping[str, str] = MappingProxyType(
    {
        "credential_warning": "acknowledge_credential",
        "duplicate": "force",
        "previously_removed": "force",
        "scope_mismatch": "acknowledge_scope_mismatch",
        "transient_warning": "acknowledge_transient",
        "truncation_warning": "acknowledge_truncation",
        "user_claim_warning": "acknowledge_user_claim",
    }
)


def is_audit_row(kind: str, fields: Mapping[str, Any]) -> bool:
    """Whether a `kind` row with `fields` is an audit row: a standing
    guard row, a write or update that used an escape hatch (`force=True`,
    or an acknowledge flag that waved a gate's hit through), a refusal
    such a hatch passes (`OVERRIDABLE_REFUSALS`), or one `[policy]`
    refused (`AUDIT_STATUSES`). Read on the fields as the handler passes
    them, so a new `*_acknowledged` field on either kind is covered
    without a second list to keep in step, and safe on a row read back
    from a hand-edited log (a status that is not a string is no
    status)."""
    if kind in AUDIT_KINDS:
        return True
    if kind not in OVERRIDE_EVIDENCE_KINDS:
        return False
    status = fields.get("status")
    if isinstance(status, str) and (
        status in AUDIT_STATUSES or status in OVERRIDABLE_REFUSALS
    ):
        return True
    if fields.get("forced") is True:
        return True
    return any(
        bool(value)
        for key, value in fields.items()
        if key.endswith(_ACKNOWLEDGED_SUFFIX)
    )


# Product state (ENG-108): every row a product module reads back to decide
# what it does next, the audit trail above among them. Before 9.0.0rc14
# these rows were telemetry like the rest, so `[telemetry] enabled =
# false`, which an organisation may pin, left the session brief and the
# prompt recall showing nothing, a contradicted memory surfacing again, a
# searched memory never settled at the Stop and a review chain impossible
# to find; and with telemetry on, a write that met another writer's lock
# dropped the row with one log line. A state row is recorded whatever the
# switch says, written with a retry and raised when it cannot land
# (`Recorder.record`), and never pruned (`prunable`). With telemetry off it
# keeps only the fields its readers read (`state_fields`), so no free text
# lands: no query or prompt text, no body snippet or claim excerpt, no
# note, reason or quoted words, no transcript path.
# tests/test_row_classes.py fails when a product module reads a kind the
# table below does not name, and tests/test_state_rows.py when a row
# recorded with telemetry off keeps a field it does not list.
#
#: The fields every state row keeps with telemetry off, whatever its kind
#: (the store writes its session column and the actor block beside them):
#: the session a hook row names (``session_id``) and the recorder's
#: stamps, which the session joins read (`sessions`): the admin
#: ``attribution``, the hook that wrote the row (``triggered_from``), the
#: checkout's ``worktree_root`` and the conversation's
#: ``harness_session``.
STATE_STAMP_FIELDS: frozenset[str] = frozenset(
    {
        "attribution",
        HARNESS_SESSION_FIELD,
        "session_id",
        "triggered_from",
        "worktree_root",
    }
)

#: One state kind's kept fields: each field its readers read, mapped to
#: None when the field is kept as it is, or to the keys kept inside it (a
#: dict, or each dict of a list) when its readers read only those.
KeptFields = Mapping[str, frozenset[str] | None]


def _kept(*names: str, **inside: tuple[str, ...]) -> KeptFields:
    fields: dict[str, frozenset[str] | None] = dict.fromkeys(names)
    fields.update({name: frozenset(keys) for name, keys in inside.items()})
    return MappingProxyType(fields)


#: Every state kind, with the fields a row of it keeps with telemetry off
#: beside `STATE_STAMP_FIELDS` (and, on a write or an update, the override
#: evidence by its suffix). The one table of the state class: the writer
#: (`Recorder.record`) and a pruning (`prunable`) read it through
#: `row_class`, and health reads the audit rows among its rows through
#: `is_audit_row`. The readers, by kind:
#:
#: - ``show`` (``id``) and ``search`` (``returned``, the ids): the prompt
#:   recall's seen gate (`recall._seen_in_log`), the Stop's settlement of
#:   the turn's retrievals (`hook._pending_retrievals`), the probe's shield
#:   (`audit._RETRIEVAL_EVENT_KINDS`) and health's retrieval counts; a
#:   show also closes a segment's opened pointer
#:   (`recall_settle.settle_unused`).
#: - ``write`` and ``update``: ``id`` and ``status`` (the seen gate reads
#:   a committed one's id), a write's ``review_id`` (`review.find_chain`),
#:   the gates' detector kinds and markers (health's override rate,
#:   `secret_scan`'s acknowledged credentials), ``forced`` and the policy
#:   refusal's flags (the audit trail).
#: - ``verify``: ``id``, ``status``, ``verified_head`` and
#:   ``verified_commits`` (`membership_migrate`'s evidence).
#: - ``use``, every outcome: ``ids``, ``outcome`` and ``auto``
#:   (`Store.contradicted_at`, which holds a contradicted record back from
#:   both recall surfaces; the search response's recent outcomes; health;
#:   the auto settlement's dedup), and a shown pointer's ``surface``,
#:   ``shown_event_id``, ``segment``, ``harness`` and ``agent_id`` (the
#:   session's rebuild, `recall_state`).
#: - ``turn_audited``, the Stop's row: its stamp opens the next turn's
#:   window (`recall_settle.previous_audit_at`); ``prompt_id`` names the
#:   turns already audited (`settle_interrupted`); ``probe_query``'s hash
#:   is the re-audit dedup's key (`audit.is_duplicate_audit`), kept with
#:   its length and never its preview; ``verdict``, ``repeat`` and
#:   ``no_signal_reason`` are health's miss rate; ``settled_count`` says
#:   what the Stop settled.
#: - ``search_miss`` (``event_id`` and each top hit's ``id``), ``miss_ack``
#:   (``event_id``) and ``silent_miss_cutoff`` (``cutoff_ts``): health's
#:   silent misses and `memory_acknowledge_miss`.
#: - ``session_brief``, ``prompt_recall``, ``segment_opened`` and
#:   ``review_unanswered``: the rows a session's recall state is rebuilt
#:   from (`recall_state._STATE_KINDS`, with ``turn_audited`` and
#:   ``use``); the brief's and the recall's ``checkout`` also places a
#:   session in a checkout for the episode handoff's quiet-session check.
#: - ``episode_handoff`` and ``episode_write``: their ``harness_session``
#:   stamp alone, which tells that check a session journaled.
#: - ``review_issued``: ``review_id``, ``parent_review_id``, the
#:   ``related`` ids and the ``roster`` ids, a review chain
#:   (`review.find_chain`).
#: - ``scope_disable`` and ``scope_enable``: ``scope``, what a session
#:   turned off (`sessions._disabled_scopes_from_events`).
#: - ``standing_guard``, the audit row of a standing change: ``door``,
#:   ``standing`` (the record ids) and ``said_checked``; the user's quoted
#:   words (``said``) only with telemetry on.
STATE_FIELDS: Mapping[str, KeptFields] = MappingProxyType(
    {
        "episode_handoff": _kept(),
        "episode_write": _kept(),
        "miss_ack": _kept("event_id"),
        "prompt_recall": _kept(
            "agent_id", "checkout", "event_id", "harness", "segment", "shown_ids"
        ),
        "review_issued": _kept("parent_review_id", "related", "review_id", "roster"),
        "review_unanswered": _kept("harness", "root_ids"),
        "scope_disable": _kept("scope"),
        "scope_enable": _kept("scope"),
        "search": _kept("returned"),
        "search_miss": _kept("event_id", top_hits=("id",)),
        "segment_opened": _kept(
            "agent_id",
            "brief_event_id",
            "harness",
            "marker",
            "marker_absent",
            "opened_at",
            "segment",
            "withdrawn",
        ),
        "session_brief": _kept(
            "agent_id",
            "checkout",
            "event_id",
            "harness",
            "ids",
            "marker",
            "marker_absent",
            "segment",
        ),
        "show": _kept("id"),
        "silent_miss_cutoff": _kept("cutoff_ts"),
        "standing_guard": _kept("door", "said_checked", "standing"),
        "turn_audited": _kept(
            "no_signal_reason",
            "prompt_id",
            "repeat",
            "settled_count",
            "verdict",
            probe_query=("hash", "len"),
        ),
        "update": _kept(
            "control_token_kinds",
            "credential_kinds",
            "forced",
            "id",
            "markers",
            "policy_flags",
            "policy_keys",
            "policy_managed",
            "status",
        ),
        "use": _kept(
            "agent_id",
            "auto",
            "harness",
            "ids",
            "outcome",
            "segment",
            "shown_event_id",
            "surface",
        ),
        "verify": _kept("id", "status", "verified_commits", "verified_head"),
        "write": _kept(
            "control_token_kinds",
            "credential_kinds",
            "forced",
            "id",
            "markers",
            "mismatch_kinds",
            "policy_flags",
            "policy_keys",
            "policy_managed",
            "review_id",
            "status",
        ),
    }
)

#: The state kinds: `STATE_FIELDS`' keys.
STATE_KINDS: frozenset[str] = frozenset(STATE_FIELDS)

#: How many times a state row is tried against a store another writer
#: holds (SQLITE_BUSY or SQLITE_LOCKED) before the failure is raised: the
#: first try and one more. Each try waits for the lock up to the
#: connection's busy timeout (`Store.set_busy_timeout`, 5 s for each
#: role), and inside a hook no later than the hook's deadline
#: (`within_hook_budget`), which falls before the harness kills the hook.
#: A row recorded inside another call's transaction is tried once: that
#: transaction already holds the lock.
STATE_WRITE_ATTEMPTS = 2


class RowClass(enum.Enum):
    """The two classes of row the event log holds (ENG-108), and what
    each promises. `row_class` puts every row in one of them by its kind;
    the writer (`Recorder.record`) and a pruning of the log (`prunable`)
    read that answer, and the answer reads one table (`STATE_FIELDS`), so
    the two cannot disagree about a row.

    ``STATE``: a row of a kind a product module reads back to decide what
    it does next, the audit trail SECURITY.md promises among them
    (`STATE_FIELDS`, each kind with its readers). Recorded whatever
    `[telemetry] enabled` says; with it off, only the fields its readers
    read (`state_fields`). Written with `STATE_WRITE_ATTEMPTS` against a
    busy store and raised when it cannot land, so the call that needed it
    fails rather than the row going missing; never prunable.

    ``MEASUREMENT``: a row of a kind no product module reads
    (`MEASUREMENT_KINDS`): the grades, the held recalls, the conflict
    scans and verdicts, the removals, restores and renames, the tombstone
    listings, the standing answers and the expired use tokens. Only the
    reports read one (eval, the recall and review reports, a memory's
    history), and they report on what is there. Opt-out: with telemetry
    off none is recorded. Best-effort: a write that fails is logged at
    WARNING and the call it describes goes on. Prunable at any age.
    """

    STATE = "state"
    MEASUREMENT = "measurement"


#: The kinds whose rows are measurement (`RowClass.MEASUREMENT`).
MEASUREMENT_KINDS: frozenset[str] = frozenset(
    {
        "answer_standing",
        "capture_grade",
        "conflict_scan",
        "conflict_verdict",
        "list_tombstones",
        "recall_grade",
        "recall_held",
        "remove",
        "rename_scope",
        "restore",
        "review_grade",
        "use_token_expired",
    }
)

#: Every kind the product records, by class, for the docs and the tests:
#: `STATE_KINDS` and `MEASUREMENT_KINDS`. The writer and a pruning read
#: `row_class`, which reads `STATE_FIELDS` alone: a kind neither list names
#: is measurement, the safe default for a row nothing reads back, and the
#: test that scans the recording sites (tests/test_row_classes.py) fails
#: until a new kind is listed under one class.
ROW_KINDS: Mapping[RowClass, frozenset[str]] = MappingProxyType(
    {
        RowClass.STATE: STATE_KINDS,
        RowClass.MEASUREMENT: MEASUREMENT_KINDS,
    }
)


def row_class(kind: str) -> RowClass:
    """The class of a `kind` row (`RowClass`): state for a kind
    `STATE_FIELDS` names, measurement for every other."""
    return RowClass.STATE if kind in STATE_FIELDS else RowClass.MEASUREMENT


def prunable(kind: str) -> bool:
    """Whether a pruning of the event log may drop a `kind` row: a
    measurement row may, at any age; a state row may not, at any age.
    The pruning (ENG-82) is not on this branch; this is the rule it
    reads, so no row a product module reads back is the one it drops."""
    return row_class(kind) is RowClass.MEASUREMENT


def state_fields(kind: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a `kind` state row that telemetry off keeps: its
    kind's (`STATE_FIELDS`), the stamps (`STATE_STAMP_FIELDS`) and, on a
    write or an update, the override evidence. A field kept by its keys
    keeps only those, and a query among them still in text is redacted
    first (`redact_query`), so its hash and length are kept and never
    its text. Everything else is measurement and is dropped."""
    kept = STATE_FIELDS.get(kind, {})
    evidence = kind in OVERRIDE_EVIDENCE_KINDS
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name in kept:
            keys = kept[name]
            if keys is None:
                out[name] = value
                continue
            if name in _REDACTED_TEXT_FIELDS and isinstance(value, str):
                value = redact_query(value)
            out[name] = _keys_of(value, keys)
        elif name in STATE_STAMP_FIELDS or (
            evidence and name.endswith(_ACKNOWLEDGED_SUFFIX)
        ):
            out[name] = value
    return out


def _keys_of(value: Any, keys: frozenset[str]) -> Any:
    """`value` with only `keys` kept: inside a dict, or inside each dict of
    a list (an item that is not a dict is dropped); None for anything
    else."""
    if isinstance(value, Mapping):
        return {k: v for k, v in value.items() if k in keys}
    if isinstance(value, list):
        return [
            {k: v for k, v in item.items() if k in keys}
            for item in value
            if isinstance(item, Mapping)
        ]
    return None


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


# A hook's deadline (ENG-108). Claude Code kills a hook at the timeout the
# plugin's hooks.json gives it (10 s for the prompt), and a state row tried
# twice under a 5 s busy timeout outlasted that: the hook was gone before
# the daemon answered, and a row landing late recorded a recall the model
# never saw. Inside a hook body every row is tried no later than the
# deadline (`Store.record_event`), and a state row another writer kept out
# is the hook's failure, named for what it did not do.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "bettermemory_events_deadline", default=None
)

#: What a hook does not do when its state row cannot land, by hook event
#: (`_daemon_client.HOOK_EVENTS`).
_NOT_DONE: Mapping[str, str] = MappingProxyType(
    {
        "session-start": "the brief was not shown",
        "prompt": "the recall was not shown",
        "stop": "the turn was not audited",
    }
)


class StoreBusy(RuntimeError):
    """A hook body's state row met another writer's lock until the hook's
    deadline (`within_hook_budget`): the hook showed nothing, as a row
    that cannot land shows nothing, and says so in the one line its
    client prints."""


def deadline() -> float | None:
    """The `time.monotonic()` reading by which the current hook body's
    rows are tried, or None outside one (a tool call waits its busy
    timeout on each try)."""
    return _DEADLINE.get()


def _store_busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return (
        isinstance(exc, sqlite3.OperationalError)
        and isinstance(code, int)
        and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )


@contextlib.contextmanager
def within_hook_budget(event: str, store: Store | None = None) -> Iterator[None]:
    """Run a hook body for `event` (`_daemon_client.HOOK_EVENTS`) under its
    deadline: its budget (`_daemon_client.hook_budget_seconds`: the
    harness's timeout for the hook as hooks.json gives it, less a margin)
    from now, or an earlier deadline already set. Every row is tried no
    later than the deadline (`Store.record_event`), and the body's other
    writes on `store` wait the budget at most, not the busy timeout. A busy
    store that keeps a row out raises `StoreBusy`, whose text says the
    store was busy, what the hook did not do, and to run ``bettermemory
    doctor``."""
    from ._daemon_client import hook_budget_seconds

    budget = hook_budget_seconds(event)
    at = time.monotonic() + budget
    held = _DEADLINE.get()
    token = _DEADLINE.set(at if held is None else min(held, at))
    waited = store.busy_timeout if store is not None else None
    if store is not None and waited is not None and waited > budget:
        store.set_busy_timeout(budget)
    try:
        yield
    except Exception as exc:
        if not _store_busy(exc):
            raise
        raise StoreBusy(
            f"the store was busy, so {_NOT_DONE.get(event, 'nothing was shown')}: "
            "another writer held it past this hook's time; run "
            "`bettermemory doctor`"
        ) from exc
    finally:
        _DEADLINE.reset(token)
        if store is not None and waited is not None and waited > budget:
            store.set_busy_timeout(waited)


def hook_body(event: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate the body of the `event` hook: it runs `within_hook_budget`
    over the store it is passed as ``store=``."""

    def wrap(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def body(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            given = kwargs.get("store")
            store = cast("Store", given) if hasattr(given, "set_busy_timeout") else None
            with within_hook_budget(event, store):
                return fn(*args, **kwargs)

        return body

    return wrap


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

    def records(self, kind: str) -> bool:
        """Whether `record(kind, ...)` writes a row (`RowClass`): a state
        row always, a measurement row with telemetry on, nothing inside
        `suppressed`. A caller whose row costs work to build asks first
        rather than reading the switch itself."""
        if _SUPPRESSED.get():
            return False
        return self.enabled or row_class(kind) is RowClass.STATE

    def record(self, kind: str, **fields: Any) -> None:
        """Record one `kind` row, by its class (`row_class`). A
        measurement row is a no-op with telemetry off and swallowed at
        WARNING when it fails. A state row passes the switch and is
        stamped and signed as it would be with telemetry on; with it off
        the row keeps only the fields its readers read (`state_fields`).
        It is tried `STATE_WRITE_ATTEMPTS` times against a busy store, and
        a failure that remains is logged and raised; a store open for
        reading only records nothing, with one warning, as before
        (`Store.record_event`). Inside a hook body no try outlasts the
        hook's deadline (`within_hook_budget`). Inside `suppressed`
        nothing is recorded."""
        if _SUPPRESSED.get():
            return
        # Off means no measurement, not no state and no trail.
        state = row_class(kind) is RowClass.STATE
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
        # A hook body's rows are tried no later than its deadline; a tool
        # call's are not bounded beyond the busy timeout.
        at = _DEADLINE.get()
        bounded: dict[str, Any] = {} if at is None else {"deadline": at}
        if not state:
            try:
                self.store.record_event(
                    kind, session=session, actor=actor, **bounded, **fields
                )
            except Exception as exc:  # noqa: BLE001 - measurement never fails the call
                log.warning("event %s not recorded: %s", kind, exc)
            return
        if not self.enabled:
            fields = state_fields(kind, fields)
        try:
            self.store.record_event(
                kind,
                session=session,
                actor=actor,
                best_effort=False,
                attempts=STATE_WRITE_ATTEMPTS,
                **bounded,
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
    "OVERRIDABLE_REFUSALS",
    "OVERRIDE_EVIDENCE_KINDS",
    "ROW_KINDS",
    "Recorder",
    "RowClass",
    "STATE_FIELDS",
    "STATE_KINDS",
    "STATE_STAMP_FIELDS",
    "STATE_WRITE_ATTEMPTS",
    "KeptFields",
    "StoreBusy",
    "deadline",
    "hook_body",
    "is_audit_row",
    "prunable",
    "redact_query",
    "row_class",
    "state_fields",
    "suppressed",
    "within_hook_budget",
]
