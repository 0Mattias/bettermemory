"""The telemetry recorder: one row per tool call, into the store's log.

Every tool handler holds one `Recorder`, constructed once per server
process, and calls `record()` once per invocation. The recorder carries
what the store's `record_event` does not know on its own: the session
id the process runs under, the worktree the server was started in, and
the admin attribution a CLI command stamps on its writes. `enabled=False`
makes every call a no-op, so handlers call unconditionally.

A failure to record is swallowed at WARNING: telemetry never fails the
tool call it describes. Query text is never stored verbatim: `query` and
`probe_query` are redacted to a hash, a short preview and a length, with
known secret shapes stripped first, before the row is signed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import identity

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


def _strip_known_secrets(text: str) -> str:
    """Replace known secret token shapes with redaction markers.

    Applied before the 32-char preview is taken so the preview never
    captures a partial token. The hash is also computed on the
    secret-stripped text so a repeated query with the same secret
    still correlates, but the secret bytes don't feed into the hash
    input either.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
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

    Known token shapes (Anthropic / OpenAI / GitHub / AWS) are
    stripped to opaque markers BEFORE the 32-char preview is taken,
    so the preview never carries a partial high-entropy secret. The
    pre-strip text length is retained as ``len`` so downstream
    triage can still see "this query was 87 chars" without seeing
    what those chars were.
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

    def record(self, kind: str, **fields: Any) -> None:
        if not self.enabled:
            return
        if self.attribution:
            fields.setdefault("attribution", self.attribution)
        if self.triggered_from:
            fields.setdefault("triggered_from", self.triggered_from)
        if self.worktree_root is not None:
            fields.setdefault("worktree_root", self.worktree_root)
        # A handler that names the actor itself wins over the published
        # one; the field must leave `fields`, or the store sees it twice.
        actor = fields.pop("actor", None)
        if actor is None:
            actor = identity.current_actor()
        # A field named `session` names the session the event belongs to
        # (the hooks record under the transcript's id); the recorder's
        # own id is the default.
        session = fields.pop("session", self.session_id)
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
    "Recorder",
    "redact_query",
]
