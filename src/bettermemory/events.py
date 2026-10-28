"""The telemetry recorder: one row per tool call, into the store's log.

Every tool handler holds one `Recorder`, constructed once per server
process, and calls `record()` once per invocation. The recorder carries
what the store's `record_event` does not know on its own: the session
and the worktree an event belongs to (the request's own when the caller
declared them, else the process's), and the admin attribution a CLI
command stamps on its writes. `enabled=False` (`[telemetry] enabled =
false`) makes every telemetry call a no-op, so handlers call
unconditionally; an audit row (`is_audit_row`: the standing guard's row
and the override evidence on a write or an update) is recorded whatever
the switch says, because it is the trail SECURITY.md promises, not a
measurement.

A failure to record is swallowed at WARNING: telemetry never fails the
tool call it describes. Query text is never stored verbatim: `query` and
`probe_query` are redacted to a hash, a short preview and a length, with
known secret shapes stripped first, before the row is signed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
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

#: The suffix every override-evidence field but `forced` carries.
_ACKNOWLEDGED_SUFFIX = "_acknowledged"


def is_audit_row(kind: str, fields: Mapping[str, Any]) -> bool:
    """Whether a `kind` row with `fields` is an audit row, recorded with
    telemetry off: a standing guard row, or a write or update that used
    an escape hatch (`force=True`, or an acknowledge flag that waved a
    gate's hit through). Read on the fields as the handler passes them,
    so a new `*_acknowledged` field on either kind is covered without a
    second list to keep in step."""
    if kind in AUDIT_KINDS:
        return True
    if kind not in OVERRIDE_EVIDENCE_KINDS:
        return False
    if fields.get("forced") is True:
        return True
    return any(
        bool(value)
        for key, value in fields.items()
        if key.endswith(_ACKNOWLEDGED_SUFFIX)
    )


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
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for start, end, kind in reversed(secret_spans(text)):
        text = f"{text[:start]}{_DETECTOR_MARKER.format(kind=kind)}{text[end:]}"
    return text


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

    def record(self, kind: str, **fields: Any) -> None:
        # Off means no telemetry, not no trail: an audit row passes the
        # switch (`is_audit_row`) and is stamped and signed exactly as it
        # would be with telemetry on.
        if not self.enabled and not is_audit_row(kind, fields):
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
        try:
            self.store.record_event(kind, session=session, actor=actor, **fields)
        except Exception as exc:  # noqa: BLE001 - telemetry never fails the call
            log.warning("event %s not recorded: %s", kind, exc)


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
    "HARNESS_SESSION_FIELD",
    "HARNESS_STAMPED_KINDS",
    "OVERRIDE_EVIDENCE_KINDS",
    "Recorder",
    "is_audit_row",
    "redact_query",
]
