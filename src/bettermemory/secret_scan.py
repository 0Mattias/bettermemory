"""`bettermemory security scan`: where a credential-shaped string persists.

The credential gate (`credentials.py`) runs at one moment: when
`memory_write` or `memory_update` is about to commit a body. Everything
that reaches the store any other way is never looked at again. An episode
skips the gate entirely (`handlers/episode_write.py` checks size and
scope count only). A body written before a detector existed, carried in
by `migrate v8` or an import, or put through the Store API directly, was
never checked by today's detectors. A body the caller waved through with
`acknowledge_credential=True` was checked and kept on purpose. And the
signed log keeps every version of every record: a `memory_put` row for
each write, update, verify and corroboration, a `tombstone_put` for each
removal, an `episode_put` for each journal entry. Editing a secret out,
removing the record and pruning the tombstone each append a row; none of
them takes the earlier payload out of the file, because no path re-signs
history. Before this module, nothing could answer "where does this secret
live now", which is the first question of an incident and the last one
before an export is shared.

What it reads, through the gate's own detectors so it finds exactly what
the gate would refuse (precision over recall, the detector discipline
`credentials.py` documents):

- the record tables: every text column of `memories`, `tombstones`,
  `episodes` and `conflicts` (a conflict row quotes the first hundred
  characters of each record's body, so it is a copy of body text in its
  own right), and every string inside their JSON columns, so claims, link
  notes and the origin's raw remote URL (`origin.repo`, which carries a
  token when the remote was cloned with one) are covered with the body;
- with `include_log`, the payload of every log row, whatever its kind:
  the record versions above, the membership rows (a checkout's raw
  remotes) and the telemetry events (a write event's claims, a
  `standing_guard` quote).

The tables are read as raw rows rather than through `Store.iter_memories`
and its siblings. Those validate each row into a model and raise on one
that does not validate, and a row that entered outside the store (the
`unaccounted` rows `log verify` reports) is exactly where an unvetted
secret sits. The FTS columns and `scopes_text` are skipped: they are
derived from the body and the scopes of the same row, so scanning them
would report every body hit twice. For the same reason a text a row
holds twice (the mirrored `origin_repo` column beside `origin_json`) is
scanned once, under the first field that holds it.

What it reports, per hit: the location (a table, or the log with the
row's seq and kind), the record id, the field as a path into the row
(`body`, `claims[0]`, `origin.repo`, `memory.body` inside a log payload),
the detector kind, the character offset of the first hit of that kind in
the field and how many there are, the record's state (live, tombstoned,
or pruned: in no table, so only the log still holds it), whether the log
shows the write was acknowledged, and what to do about it.

What it never prints is the value. Not the secret, and not the gate's
leak-free snippet either: `credentials._safe_snippet` redacts every span
a detector matched, but its context window runs 24 characters past the
match, and the text right after a detected secret is very often its
undetected partner. An AWS access key id followed by its secret access
key is the plain case: no detector matches the secret key on its own, so
the gate's snippet for the pair carries its first 23 characters
(tests/test_secret_scan.py pins both halves). The gate returns that
snippet to the model that already holds the body; the scan's output goes
to a terminal, a ticket or a CI log, so it reports an offset into the
field instead, which locates the hit for `memory_show` without carrying a
byte of it. Field paths are printed only where a JSON key is a plain
identifier and no detector fires on it, and a record id or a log kind
only where it is a plain token no detector fires on: the id column and
an event's `id` are scanned text like any other.

The acknowledgement comes from the telemetry events. A committed
`write` and a successful `update` event always carry
`credentials_acknowledged`, the detector kinds the caller waved through
(kinds only, never the value). For a record with such events, a hit
whose kind one of them names is `acknowledged: true`, and a hit no event
acknowledged is `false`: the secret reached the record past the gate (an
import, the Store API, an episode copied in) or through a detector added
since. A record with no such event (telemetry off, an imported v8 record,
an episode, a conflict row) is `null`: the log cannot say.

Read-only. The CLI opens the store with `Store.open(...,
read_only=True)`, as `log verify` and `stats` do: a scan must never
write a rekey row into the log it is reading, a store copied from
another machine scans without its key, and the open writes nothing to
the file or beside it (`store_schema.connect_read_only`: no WAL
switch, no chmod, no added table, no ``-wal`` or ``-shm`` left behind).
Nothing here writes a row, a key or a record. A file inside a backup
bundle (spelled through a symlink too) is still refused with the
instruction to scan a copy, and so is a `--store` file with other names
(hard links), which may be a bundle's under a name no manifest sits
beside: a bundle is checked with `backup verify`, and every other
command refuses it the same way.

Not covered, and said in every report: freed pages and earlier page
images in the file and its WAL, the full-text index's token pages, saved
orphans beside the store (named in the report: copied to `memory.sqlite`
in a directory of its own, each scans with `--store`), backups and
exports. The report names them because a clean scan of the records is
not a clean file.

The same pass reads every text for a model's chat-template control
token (ENG-189), through the detector the write gates refuse a body by
(`control_tokens.control_token_offsets`). A store written before
9.0.0rc14, when no gate read a body for one, or before 9.0.0rc18, when
none read `said`, a removal's reason or a note, can hold one, and every
read that hands the text to a model hands it the token too. Each such
finding is a `ControlTokenFinding` in a list of its own beside the
credential findings, reported the way the refusals' events record them:
the template family, with an offset and a count, and never the token nor
its bare name. A control token is no credential, so its findings carry no
acknowledgement (no flag passes the gate) and their remedy is the
rewrite, not a rotation (`CONTROL_TOKEN_REMEDIATION`), except in a path,
a session, an id or a declared name, which the store keeps as given and
nothing rewrites (`CONTROL_TOKEN_IDENTITY_REMEDIATION`). Either kind of
finding makes the scan `found`, exit 1.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from .control_tokens import control_token_offsets, find_control_tokens
from .credentials import _merged_secret_spans
from .events import NAMED_EVENT_FIELDS
from .log import iter_rows
from .store import Store, orphans_beside, store_path

# The table pass, in report order. `memories` and `episodes` hold live
# records; `tombstones` holds removed ones until `tombstones prune`;
# `conflicts` holds body quotes for the curation queue.
TABLES = ("memories", "tombstones", "episodes", "conflicts")
LOG = "log"

# What a scan looks for, as `--only` names it: every kind by default.
CREDENTIALS = "credentials"
CONTROL_TOKENS = "control-tokens"
KINDS = (CREDENTIALS, CONTROL_TOKENS)

# Record states. `pruned` is a record no table holds any more: a pruned
# tombstone, a deleted episode, a memory removed by a path that left no
# tombstone. Only the log still has its payload.
LIVE = "live"
TOMBSTONED = "tombstoned"
PRUNED = "pruned"

# Singular and plural, for the text report's counts.
_NOUNS = {
    "memories": ("memory", "memories"),
    "tombstones": ("tombstone", "tombstones"),
    "episodes": ("episode", "episodes"),
    "conflicts": ("conflict", "conflicts"),
    LOG: ("log row", "log rows"),
}

_TABLE_STATE = {
    "memories": LIVE,
    "tombstones": TOMBSTONED,
    "episodes": LIVE,
    "conflicts": LIVE,
}

# Columns derived from other columns of the same row (the FTS token
# streams and the flattened scope text) or structural (the log pointer).
# Scanning a derived column would report a body hit a second time.
_SKIPPED_COLUMNS = frozenset({"body_fts", "scopes_fts", "scopes_text", "log_mac"})

# The log kinds whose payload is one row of a scanned table, and the key
# it sits under. A conflict is not a record, but its row is a copy of
# body text the table pass reports by its id, so its log rows are named
# the same way.
_RECORD_PUTS = {
    "memory_put": "memory",
    "tombstone_put": "tombstone",
    "episode_put": "episode",
    "conflict_put": "conflict",
}
_RECORD_DELETES = frozenset(
    {"memory_delete", "tombstone_delete", "episode_delete", "conflict_delete"}
)

# The telemetry kinds that carry `credentials_acknowledged`: the committed
# write and the successful update (handlers/write.py, handlers/update.py).
_ACK_EVENT_KINDS = ("write", "update")
_ACK_FIELD = "credentials_acknowledged"
# Before any event a store holds: `events_of_kinds` cuts on a stamp.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# A JSON key printed as a path segment: a plain identifier. Anything else
# (and an identifier a detector fires on) prints as `*`, so a field path
# can never carry a value.
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_MASKED_KEY = "*"

# A record id or a log kind printed as it is: a plain token (a ULID, a
# `cf-` conflict id, a log kind). The id column and an event's `id` are
# scanned text, and a row written outside the store can hold anything in
# them or in a log row's kind, a secret or a terminal escape included, so
# anything else, and a token a detector fires on, prints as `*`. The
# class holds no delimiter a control token opens with, but it does spell
# a turn marker (`Human:`), which the control-token detector fires on, so
# that prints as `*` too (ENG-189). A JSON key's class (`_PLAIN_KEY_RE`)
# holds no `:` and spells none.
_PLAIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

# Rotation is the remedy for every location, and each text says so first:
# the signed log keeps every version of every record (the module
# docstring), so editing the value out, removing the record and pruning
# the tombstone or the episode each leave the earlier payload in the store
# file and in every backup of it. The steps after rotation only stop the
# value being read from a table; none of them is a delete of the value.
_ROTATE = (
    "Rotate the credential at its issuer: that is the fix, because no "
    "command deletes the value. The signed log keeps every earlier version "
    "of the record, after an edit, a removal and a prune, in the store file "
    "and in every backup of it"
)
REMEDIATION = {
    "memories": (
        f"{_ROTATE}. Then rewrite the record without it (memory_update) or "
        "remove it (memory_remove, then `bettermemory tombstones prune`), so "
        "it is no longer read or searched."
    ),
    "tombstones": (
        f"{_ROTATE}. `bettermemory tombstones prune --older-than DAYS` (at "
        "least one day after the removal) then drops the tombstone row, not "
        "the log's copy of its payload."
    ),
    "episodes": (
        f"{_ROTATE}. No command edits or removes a single episode: "
        "`bettermemory episodes prune --ttl-days DAYS` drops whole sessions "
        "past the TTL from the episodes table, not the log's copy of them."
    ),
    "conflicts": (
        f"{_ROTATE}. Then fix the quoted record (memory_update or "
        "memory_remove): the conflict scan refreshes a pending row's quote "
        "and drops a row whose record is gone."
    ),
    LOG: (
        f"{_ROTATE}. No command erases a log row today: removing or pruning "
        "the record leaves this copy where it is."
    ),
}

# A control token is no credential: there is nothing to rotate, and the
# remedy is to stop the reads that put it in front of a model, the
# table's. The signed log keeps a record's earlier versions after an
# edit, a removal and a prune, a token as much as a secret, and no command
# erases a log row (ENG-189).
_TOKEN_LOGGED = (
    "The signed log keeps the earlier version, in the store file and in "
    "every backup of it, and no command erases a log row"
)
CONTROL_TOKEN_REMEDIATION = {
    "memories": (
        "Rewrite the field without the token, writing its bare name or "
        "describing it in words (memory_update for the body or a link's "
        "note, memory_verify for claims and attestations), or remove the "
        "record (memory_remove), so recall, search and memory_show stop "
        f"reading it into a session. {_TOKEN_LOGGED}."
    ),
    "tombstones": (
        'A removed record is still read: memory_admin(action="tombstones") '
        "lists it, and a write that overlaps it quotes its reason. "
        "`bettermemory tombstones prune --older-than DAYS` (at least one day "
        f"after the removal) drops the row. {_TOKEN_LOGGED}."
    ),
    "episodes": (
        "The handoff reads an episode into the next session until the TTL "
        "prunes it, and no command edits or removes a single episode: "
        "`bettermemory episodes prune --ttl-days DAYS` drops whole sessions "
        f"past the TTL. {_TOKEN_LOGGED}."
    ),
    "conflicts": (
        "Fix the quoted record (memory_update or memory_remove): the conflict "
        "scan refreshes a pending row's quote and drops a row whose record is "
        "gone."
    ),
    LOG: (
        "No command erases a log row: rewriting or removing the record stops "
        "the tables' copy being read, and this one stays in the store file "
        "and in every backup of it."
    ),
}

# A log row that is no version of a record (an event, a membership row)
# has no record to rewrite. In its free-text fields
# (`events.NAMED_EVENT_FIELDS`) the event writer has kept each control
# token by its bare name since 9.0.0rc18, and a join its quote
# (`control_tokens.folded_quote`), so a token there was written before
# that or outside the store, and the tool replies and hook text that read
# the field to a model name it the same way (`control_tokens.named_tokens`).
# Nothing can take it out of the file, so the remedy is to know it is
# there, and `--only credentials` is how a scheduled scan stops reporting
# it (ENG-189).
CONTROL_TOKEN_LOG_ROW_REMEDIATION = (
    "No command erases a log row, and this one is no version of a record: "
    "an event or a membership row. In this field the event writer, and a "
    "join for its quote, have kept a token by its bare name since "
    "9.0.0rc18, so the row was written before that or outside the store, "
    "and the tools that read the field back to a model name the token the "
    "same way. It stays in the store file and in every backup of it, and "
    "every scan with --log reports it; `--only credentials` scans for "
    "credentials alone."
)

# A path, a session, an id or a declared name is kept as given, a token
# and all, because the store compares it with the live value: a renamed
# checkout path in an event lost the Stop audit's anchor, the
# foreign-window filter and the handoff's quiet session (the round-2
# review's `[INST]` probe), and a renamed client name no longer matched
# memory_search's `client` filter. No command rewrites one: a record's
# origin and actor are its writer's, an id is the record's. The tool
# replies and the hooks' text name the token where they read the field to
# a model (`control_tokens.named_tokens`). The fields: in a record table
# and a record's version in the log, `_IDENTITY_FIELDS`; in any other log
# row, every field the event writer does not read as text (ENG-189).
CONTROL_TOKEN_IDENTITY_REMEDIATION = (
    "Kept as given: a path, a session, an id or a declared client or model "
    "name, which the store compares with the live value, or another field "
    "of a log row that the event writer does not read as text. No command "
    "rewrites it. A tool's reply and the hooks' text name the token where "
    "they read the field to a model. Where it is a checkout's path or a "
    "client's declared name, a checkout whose path holds no token, or a "
    "client declared under another name, keeps it out of new rows; the "
    "rows written stay in the store file and in every backup of it. "
    "`--only credentials` scans for credentials alone."
)

#: The fields of a record, as a table row or a record's version in the
#: log names them, that the store keeps as given
#: (`CONTROL_TOKEN_IDENTITY_REMEDIATION`): the record's id and file name,
#: its origin (a checkout's path and remote, mirrored in `origin_repo` and
#: `origin_worktree`) and actor (the declared client and model, mirrored
#: in `actor_client` and `actor_model`), an episode's session and swarm,
#: a tombstone's removing session, a conflict's two record ids.
_IDENTITY_FIELDS = frozenset(
    {
        "a_id",
        "actor",
        "actor_client",
        "actor_model",
        "b_id",
        "filename",
        "id",
        "origin",
        "origin_repo",
        "origin_worktree",
        "removed_session",
        "session_id",
        "swarm_id",
    }
)

# A field path's keys: `top_hits[0].snippet` is `top_hits`, `snippet`.
_PATH_KEYS_RE = re.compile(r"\.|\[\d+\]\.?")

NOT_SCANNED = (
    "freed pages and earlier page images in the store file and its WAL",
    "the full-text index's token pages",
    "saved orphans beside the store",
    "backups and exports",
)


@dataclass(frozen=True)
class SecretFinding:
    """One detector kind in one field of one stored copy.

    `location` is a table name or ``"log"``; `seq` and `log_kind` are set
    for a log row only. `offset` is the character offset of the first hit
    of `kind` in the field and `occurrences` how many hits of that kind
    the field holds; neither carries any of the value. `record_id` and
    `log_kind` are `*` when they are not plain tokens or a detector fires
    on them. `record_state` is None for a log row that names no record (a
    membership or control row, a telemetry event about an id no record
    row in the log names). `acknowledged` is None when the log holds no
    gated write for the record."""

    location: str
    record_id: str | None
    seq: int | None
    log_kind: str | None
    field: str
    kind: str
    offset: int
    occurrences: int
    record_state: str | None
    acknowledged: bool | None

    @property
    def remediation(self) -> str:
        return REMEDIATION[self.location]

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "record_id": self.record_id,
            "seq": self.seq,
            "log_kind": self.log_kind,
            "field": self.field,
            "kind": self.kind,
            "offset": self.offset,
            "occurrences": self.occurrences,
            "record_state": self.record_state,
            "acknowledged": self.acknowledged,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class ControlTokenFinding:
    """One template family of control token in one field of one stored
    copy (ENG-189).

    The fields are `SecretFinding`'s, `kind` being the template family
    (`chatml`, `llama2`, `anthropic`, ...), less `acknowledged`: no flag
    passes the control-token gate. `offset` is the character offset of
    the family's first token in the field and `occurrences` how many of
    its tokens the field holds; neither carries the token, and nothing
    here names it, not even by its bare name."""

    location: str
    record_id: str | None
    seq: int | None
    log_kind: str | None
    field: str
    kind: str
    offset: int
    occurrences: int
    record_state: str | None
    kept_as_given: bool = False

    @property
    def remediation(self) -> str:
        if self.kept_as_given:
            return CONTROL_TOKEN_IDENTITY_REMEDIATION
        if self.location == LOG and self.log_kind not in _RECORD_PUTS:
            return CONTROL_TOKEN_LOG_ROW_REMEDIATION
        return CONTROL_TOKEN_REMEDIATION[self.location]

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "record_id": self.record_id,
            "seq": self.seq,
            "log_kind": self.log_kind,
            "field": self.field,
            "kind": self.kind,
            "offset": self.offset,
            "occurrences": self.occurrences,
            "record_state": self.record_state,
            "kept_as_given": self.kept_as_given,
            "remediation": self.remediation,
        }


def _kept_as_given(location: str, log_kind: str | None, field_name: str) -> bool:
    """Whether a control-token finding's field is one the store keeps as
    given (`CONTROL_TOKEN_IDENTITY_REMEDIATION`): in a record, one of
    `_IDENTITY_FIELDS`; in a log row that is no version of a record, any
    field but the free-text ones the event writer names
    (`events.NAMED_EVENT_FIELDS`). A payload that is no JSON, which only a
    writer outside the store leaves, is text."""
    keys = [key for key in _PATH_KEYS_RE.split(field_name) if key]
    if not keys:
        return False
    if location == LOG and log_kind not in _RECORD_PUTS:
        if field_name == "payload":
            return False
        if keys[0] not in NAMED_EVENT_FIELDS:
            return True
        inside = NAMED_EVENT_FIELDS[keys[0]]
        return inside is not None and (len(keys) < 2 or keys[1] not in inside)
    if location == LOG and keys[0] == _RECORD_PUTS.get(log_kind or ""):
        keys = keys[1:]
    return bool(keys) and keys[0] in _IDENTITY_FIELDS


def _per_location(
    findings: list[SecretFinding] | list[ControlTokenFinding],
) -> dict[str, int]:
    """Findings per location, every location present."""
    out = {name: 0 for name in (*TABLES, LOG)}
    for finding in findings:
        out[finding.location] += 1
    return out


@dataclass
class ScanReport:
    """What `scan_store` read and found. `scanned` counts rows per table,
    and log rows under ``"log"`` when the log was read. `findings` are the
    credential findings, `control_tokens` the control-token ones, and
    `kinds` what was looked for (`KINDS`, or the one `--only` names)."""

    path: str
    log_scanned: bool
    scanned: dict[str, int] = field(default_factory=dict)
    findings: list[SecretFinding] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    control_tokens: list[ControlTokenFinding] = field(default_factory=list)
    kinds: tuple[str, ...] = KINDS

    @property
    def status(self) -> str:
        return "found" if self.findings or self.control_tokens else "clean"

    def counts(self) -> dict[str, int]:
        """Credential findings per location, every location present."""
        return _per_location(self.findings)

    def control_token_counts(self) -> dict[str, int]:
        """Control-token findings per location, every location present."""
        return _per_location(self.control_tokens)

    def only(self, kind: str) -> ScanReport:
        """This report narrowed to one of `KINDS`, as `--only` asks: the
        other kind's findings dropped, and `kinds` saying so."""
        return replace(
            self,
            kinds=(kind,),
            findings=list(self.findings) if kind == CREDENTIALS else [],
            control_tokens=list(self.control_tokens) if kind == CONTROL_TOKENS else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "log_scanned": self.log_scanned,
            "kinds": list(self.kinds),
            "scanned": dict(self.scanned),
            "counts": self.counts(),
            "findings": [finding.to_dict() for finding in self.findings],
            "control_token_counts": self.control_token_counts(),
            "control_tokens": [finding.to_dict() for finding in self.control_tokens],
            "orphans": list(self.orphans),
            "not_scanned": list(NOT_SCANNED),
        }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _hits(text: str) -> dict[str, tuple[int, int]]:
    """Detector kind -> (offset of its first span, span count) in `text`.

    The spans are `credentials._merged_secret_spans`, the one place the
    gate's detectors, their masked-token guard and the generic rule's
    `_looks_like_secret` gate are applied together, so the scan fires on
    exactly what the gate would refuse. Overlapping spans are merged
    under the first kind there, so one secret two detectors both match
    (`api_key = AKIA...`) is one hit, not two."""
    out: dict[str, tuple[int, int]] = {}
    for start, _end, kind in _merged_secret_spans(text):
        first, count = out.get(kind, (start, 0))
        out[kind] = (first, count + 1)
    return out


def _segment(key: object) -> str:
    """A JSON key as a printable path segment, or `*` when it is not a
    plain identifier or a detector fires on it."""
    text = str(key)
    if _PLAIN_KEY_RE.fullmatch(text) and not _merged_secret_spans(text):
        return text
    return _MASKED_KEY


def _shown(value: str | None) -> str | None:
    """A record id or a log kind as a finding prints it: as it is when it
    is a plain token no detector fires on, the control-token detector
    included, `*` otherwise. The state and acknowledgement lookups use the
    value as stored."""
    if value is None:
        return None
    if (
        _PLAIN_TOKEN_RE.fullmatch(value)
        and not _merged_secret_spans(value)
        and not find_control_tokens(value)
    ):
        return value
    return _MASKED_KEY


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string inside a parsed JSON value as (path, text), in
    document order. Iterative, so a deeply nested payload (a row written
    outside the store) cannot exhaust the interpreter's stack."""
    stack: list[tuple[str, Any]] = [(path, value)]
    while stack:
        where, item = stack.pop()
        if isinstance(item, str):
            yield where, item
        elif isinstance(item, Mapping):
            children = [
                (f"{where}.{_segment(k)}" if where else _segment(k), v)
                for k, v in item.items()
            ]
            stack.extend(reversed(children))
        elif isinstance(item, list):
            stack.extend(reversed([(f"{where}[{i}]", v) for i, v in enumerate(item)]))


def _row_strings(row: sqlite3.Row) -> Iterator[tuple[str, str]]:
    """The text a table row holds, as (field, text): each text column by
    its name, and each string inside a `*_json` column by its path under
    the column's name without the suffix (`claims_json` -> `claims[0]`).
    A JSON column that does not parse is scanned as text. SQLite keeps
    the type a statement binds, so a row written outside the store can
    hold a text column as a BLOB; it is read as UTF-8, an undecodable
    byte replaced."""
    for column in row.keys():
        if column in _SKIPPED_COLUMNS:
            continue
        value = row[column]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            continue
        if not column.endswith("_json"):
            yield column, value
            continue
        name = column[: -len("_json")]
        try:
            parsed = json.loads(value)
        except ValueError:
            yield name, value
            continue
        yield from _strings(parsed, name)


def _distinct(pairs: Iterator[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    """Each text once per row, under the first field that holds it.

    `memories` and `tombstones` mirror the origin's remote and worktree
    and the actor's client and model into columns of their own
    (`origin_repo` beside `origin_json`), so a token-bearing remote URL
    would otherwise be two findings in one row. Comparing the text rather
    than skipping the mirror columns keeps a mirror that disagrees with
    its JSON (a row written outside the store) in the scan."""
    seen: set[str] = set()
    for field_name, text in pairs:
        if text in seen:
            continue
        seen.add(text)
        yield field_name, text


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _acknowledgements(store: Store) -> dict[str, frozenset[str]]:
    """Record id -> the detector kinds its gated writes acknowledged.

    Only events that carry `credentials_acknowledged` count, so a record
    in this map had at least one write or update pass the gate; an empty
    set means none of them acknowledged anything. A record outside the
    map has no gated write on record."""
    acks: dict[str, set[str]] = {}
    for event in store.events_of_kinds(_ACK_EVENT_KINDS, since=_EPOCH):
        record_id = event.get("id")
        listed = event.get(_ACK_FIELD)
        if not isinstance(record_id, str) or not isinstance(listed, list):
            continue
        acks.setdefault(record_id, set()).update(
            kind for kind in listed if isinstance(kind, str)
        )
    return {record_id: frozenset(kinds) for record_id, kinds in acks.items()}


def _acknowledged(
    acks: Mapping[str, frozenset[str]], record_id: str | None, kind: str
) -> bool | None:
    if record_id is None or record_id not in acks:
        return None
    return kind in acks[record_id]


def _ids(store: Store, table: str) -> set[str]:
    return {str(row[0]) for row in store.conn.execute(f"SELECT id FROM {table}")}


def _log_record_id(kind: str, payload: Any) -> str | None:
    """The record a log row is about: the id inside a record put, the
    `id` of a record delete or a telemetry event; None otherwise."""
    if not isinstance(payload, Mapping):
        return None
    inner = _RECORD_PUTS.get(kind)
    if inner is not None:
        record = payload.get(inner)
        value = record.get("id") if isinstance(record, Mapping) else None
    else:
        value = payload.get("id")
    return value if isinstance(value, str) and value else None


def _scan_table(
    store: Store, table: str, acks: Mapping[str, frozenset[str]]
) -> tuple[int, list[SecretFinding], list[ControlTokenFinding]]:
    """One record table in rowid order (tombstones in removal order, as
    `Store.iter_tombstones` reads them): the rows read, the credential
    findings and the control-token findings. Only memories and tombstones
    pass the credential gate, so only their credential findings carry an
    acknowledgement."""
    gated = table in ("memories", "tombstones")
    order = "removed, rowid" if table == "tombstones" else "rowid"
    rows = 0
    findings: list[SecretFinding] = []
    tokens: list[ControlTokenFinding] = []
    for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
        rows += 1
        record_id = str(row["id"])
        for field_name, text in _distinct(_row_strings(row)):
            for kind, (offset, count) in _hits(text).items():
                findings.append(
                    SecretFinding(
                        location=table,
                        record_id=_shown(record_id),
                        seq=None,
                        log_kind=None,
                        field=field_name,
                        kind=kind,
                        offset=offset,
                        occurrences=count,
                        record_state=_TABLE_STATE[table],
                        acknowledged=(
                            _acknowledged(acks, record_id, kind) if gated else None
                        ),
                    )
                )
            for family, (offset, count) in control_token_offsets(text).items():
                tokens.append(
                    ControlTokenFinding(
                        location=table,
                        record_id=_shown(record_id),
                        seq=None,
                        log_kind=None,
                        field=field_name,
                        kind=family,
                        offset=offset,
                        occurrences=count,
                        record_state=_TABLE_STATE[table],
                        kept_as_given=_kept_as_given(table, None, field_name),
                    )
                )
    return rows, findings, tokens


def _log_state(
    record_id: str | None, live: set[str], tombstoned: set[str], logged: set[str]
) -> str | None:
    """The state of the record a log row names. A record some put or
    delete in the log names and no table holds is `pruned`, whether this
    row is that put or a telemetry event about the record; an id no
    record row ever named is not a record (None)."""
    if record_id is None:
        return None
    if record_id in live:
        return LIVE
    if record_id in tombstoned:
        return TOMBSTONED
    if record_id in logged:
        return PRUNED
    return None


#: One hit in a log row: (seq, log kind, record id, field, detector kind
#: or template family, offset, count).
_LogHit = tuple[int, str, str | None, str, str, int, int]


def _scan_log(
    store: Store, acks: Mapping[str, frozenset[str]]
) -> tuple[int, list[SecretFinding], list[ControlTokenFinding]]:
    """Every log row's payload in seq order, whatever its kind: the rows
    read, the credential findings and the control-token findings. A
    payload that is not JSON is scanned as text under the field name
    ``payload``. The states are settled after the walk, against every
    record the log's puts and deletes name, so an event logged before its
    record's put (an imported v8 event) is judged by the whole log."""
    live = _ids(store, "memories") | _ids(store, "episodes") | _ids(store, "conflicts")
    tombstoned = _ids(store, "tombstones")
    logged: set[str] = set()
    rows = 0
    hits: list[_LogHit] = []
    token_hits: list[_LogHit] = []
    for log_row in iter_rows(store.conn):
        rows += 1
        try:
            payload: Any = json.loads(log_row.payload)
        except ValueError:
            payload = log_row.payload
        record_id = _log_record_id(log_row.kind, payload)
        if record_id is not None and (
            log_row.kind in _RECORD_PUTS or log_row.kind in _RECORD_DELETES
        ):
            logged.add(record_id)
        for field_name, text in _distinct(_strings(payload, "")):
            where = (log_row.seq, log_row.kind, record_id, field_name or "payload")
            for kind, (offset, count) in _hits(text).items():
                hits.append((*where, kind, offset, count))
            for family, (offset, count) in control_token_offsets(text).items():
                token_hits.append((*where, family, offset, count))
    findings = [
        SecretFinding(
            location=LOG,
            record_id=_shown(record_id),
            seq=seq,
            log_kind=_shown(log_kind),
            field=field_name,
            kind=kind,
            offset=offset,
            occurrences=count,
            record_state=_log_state(record_id, live, tombstoned, logged),
            acknowledged=_acknowledged(acks, record_id, kind),
        )
        for seq, log_kind, record_id, field_name, kind, offset, count in hits
    ]
    tokens = [
        ControlTokenFinding(
            location=LOG,
            record_id=_shown(record_id),
            seq=seq,
            log_kind=_shown(log_kind),
            field=field_name,
            kind=family,
            offset=offset,
            occurrences=count,
            record_state=_log_state(record_id, live, tombstoned, logged),
            kept_as_given=_kept_as_given(LOG, log_kind, field_name),
        )
        for seq, log_kind, record_id, field_name, family, offset, count in token_hits
    ]
    return rows, findings, tokens


def scan_store(store: Store, *, include_log: bool = False) -> ScanReport:
    """Scan `store`'s record tables, and its log with `include_log`, for
    credentials and for control tokens.

    Reads only: the table pass is plain SELECTs, the log pass walks
    `log.iter_rows`, and the acknowledgements come from
    `Store.events_of_kinds`. Each list of findings is in table order, then
    log order.

    Every pass reads one snapshot. The daemon writes while a scan runs,
    and in autocommit each SELECT sees the store as it was when that
    SELECT began: a restore landing between the memories and the
    tombstones pass moves the record into a table already read and out
    of one not yet read, so neither pass sees it, and a write landing
    after the log pass took the live ids reads as pruned. One read
    transaction pins the snapshot the first SELECT takes; under WAL a
    reader never blocks a writer. A caller already in a transaction
    keeps its own."""
    report = ScanReport(
        path=str(store.path),
        log_scanned=include_log,
        orphans=[p.name for p in orphans_beside(store.path)],
    )
    conn = store.conn
    snapshot = not conn.in_transaction
    if snapshot:
        conn.execute("BEGIN")
    try:
        acks = _acknowledgements(store)
        for table in TABLES:
            rows, findings, tokens = _scan_table(store, table, acks)
            report.scanned[table] = rows
            report.findings.extend(findings)
            report.control_tokens.extend(tokens)
        if include_log:
            rows, findings, tokens = _scan_log(store, acks)
            report.scanned[LOG] = rows
            report.findings.extend(findings)
            report.control_tokens.extend(tokens)
    finally:
        # An I/O error can end the transaction itself; ROLLBACK then has
        # nothing to end and would raise over the error that ended it.
        if snapshot and conn.in_transaction:
            conn.execute("ROLLBACK")
    return report


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _ack_text(acknowledged: bool | None) -> str:
    if acknowledged is None:
        return "no gated write on record"
    return "acknowledged at write" if acknowledged else "not acknowledged"


def _count(count: int, location: str) -> str:
    singular, plural = _NOUNS[location]
    return f"{count} {singular if count == 1 else plural}"


def _findings(count: int, what: str = "") -> str:
    noun = f"{what} finding" if what else "finding"
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _finding_line(finding: SecretFinding | ControlTokenFinding) -> str:
    """One finding's line. A control-token finding names its template
    family where a credential finding names its detector kind, and has no
    acknowledgement to name."""
    hits = "" if finding.occurrences == 1 else f" ({finding.occurrences} hits)"
    where = f"{finding.field}: {finding.kind} at offset {finding.offset}{hits}"
    ack = (
        f"; {_ack_text(finding.acknowledged)}"
        if isinstance(finding, SecretFinding)
        else ""
    )
    if finding.location != LOG:
        return f"{finding.record_id} {where}{ack}"
    head = f"seq {finding.seq} {finding.log_kind}"
    if finding.record_id is not None:
        head += f" {finding.record_id}"
    state = "" if finding.record_state is None else f"; record {finding.record_state}"
    return f"{head} {where}{state}{ack}"


def _token_groups(report: ScanReport) -> list[str]:
    """The control-token findings' lines, grouped by where they sit as the
    credential findings are, each group headed as control tokens and
    closed by its remedy: the log's rows in two groups, a record's
    versions and the rows that are none (`CONTROL_TOKEN_LOG_ROW_REMEDIATION`),
    and then, in each place, the paths, ids and names the store keeps as
    given (`CONTROL_TOKEN_IDENTITY_REMEDIATION`)."""
    lines: list[str] = []
    places = [(t, CONTROL_TOKEN_REMEDIATION[t], t) for t in (*TABLES, LOG)]
    places.append((LOG, CONTROL_TOKEN_LOG_ROW_REMEDIATION, "log rows of no record"))
    places.extend(
        (t, CONTROL_TOKEN_IDENTITY_REMEDIATION, f"{t} paths, ids and names")
        for t in (*TABLES, LOG)
    )
    for location, remedy, label in places:
        group = [
            f
            for f in report.control_tokens
            if f.location == location and f.remediation == remedy
        ]
        if not group:
            continue
        lines.append("")
        lines.append(f"  control tokens in {label}: {_findings(len(group))}")
        lines.extend(f"    {_finding_line(f)}" for f in group)
        lines.append(f"    {remedy}")
    return lines


def render_report(report: ScanReport) -> str:
    """The text form of a scan: one line per finding, grouped by where it
    sits, the credential findings first and the control-token ones after,
    each group closed by what to do about it. The summary counts each
    kind, or says a kind `--only` left out was not scanned. Never a value,
    and never a control token."""
    found = (
        _findings(len(report.findings), "credential")
        if CREDENTIALS in report.kinds
        else "credentials not scanned"
    )
    tokens = (
        _findings(len(report.control_tokens), "control-token")
        if CONTROL_TOKENS in report.kinds
        else "control tokens not scanned"
    )
    lines = [
        f"bettermemory security scan: {report.status}, {found}, {tokens}",
        f"  store {report.path}",
    ]
    read = ", ".join(_count(report.scanned.get(t, 0), t) for t in TABLES)
    if report.log_scanned:
        read += f", {_count(report.scanned.get(LOG, 0), LOG)}"
    else:
        read += "; log not scanned (pass --log)"
    lines.append(f"  scanned {read}")
    for location in (*TABLES, LOG):
        group = [f for f in report.findings if f.location == location]
        if not group:
            continue
        lines.append("")
        lines.append(f"  {location}: {_findings(len(group))}")
        lines.extend(f"    {_finding_line(f)}" for f in group)
        lines.append(f"    {REMEDIATION[location]}")
    lines.extend(_token_groups(report))
    lines.append("")
    lines.append(f"  not scanned: {'; '.join(NOT_SCANNED)}")
    for name in report.orphans:
        lines.append(
            f"  saved orphan not scanned: {name} (copy it to memory.sqlite in a "
            "directory of its own and scan that with --store)"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SCAN_HELP = (
    "Find where a credential-shaped string or a model's chat-template "
    "control token persists in the store, using the write gates' own "
    "detectors: every text field and JSON string of the "
    "memories, tombstones, episodes and conflicts tables, and with --log "
    "every log row's payload (the log keeps every version of every record, "
    "after an edit, a removal and a prune). Each finding names the table or "
    "log seq, the record, the field, the detector kind (a control token's "
    "template family), the record's state and, for a credential, whether "
    "the write was acknowledged; it never prints the value or the token, "
    "only an offset into the field; --only looks for one kind alone. "
    "Read-only: the store is opened without "
    "rekeying and no row, record or key is written; opening a store file "
    "does switch one saved in rollback-journal mode to WAL, so a backup "
    "bundle's own file is refused (scan a copy of it). Exit 0 when nothing "
    "is found, 1 when anything is, 2 on a usage error or when there is no "
    "store it can read."
)


def add_scan_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``scan`` on `subparsers` (the ``security`` group's).

    The destinations are prefixed so they cannot collide with an option
    the group itself declares."""
    parser = subparsers.add_parser("scan", help=_SCAN_HELP, description=_SCAN_HELP)
    parser.add_argument(
        "--log",
        dest="scan_log",
        action="store_true",
        help=(
            "Also scan the payload of every log row, whatever its kind: "
            "every version of every record, the membership rows and the "
            "telemetry events. Slower on a large log."
        ),
    )
    parser.add_argument(
        "--store",
        dest="scan_store",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "The store to scan instead of the configured one: a directory "
            "holding memory.sqlite, or that file itself, such as a copy of a "
            "backup bundle's memory.sqlite in a directory of its own or a "
            "copy from another machine (it needs no key). A file inside a "
            "backup bundle, or one with other names (hard links), is "
            "refused: opening it would change it and fail the bundle's hash. "
            "Default: memory.sqlite in the resolved store directory."
        ),
    )
    parser.add_argument(
        "--json",
        dest="scan_json",
        action="store_true",
        help="Emit the report as JSON instead of text.",
    )
    parser.add_argument(
        "--only",
        dest="scan_only",
        choices=KINDS,
        default=None,
        help=(
            "Look for one kind alone, and exit 1 only on it: credentials, "
            "or control tokens. A log row from before 9.0.0rc18 can hold a "
            "control token no command erases, so a scheduled credential "
            "scan with --log passes --only credentials. Default: both."
        ),
    )
    return parser


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"bettermemory security scan: error: {message}\n")
    raise SystemExit(2)


def run_scan(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory security scan``. Always exits:
    0 clean, 1 with findings of either kind, 2 when there is no store it
    can read."""
    from .config import STORE_FILENAME, load_config

    store_arg = getattr(args, "scan_store", None)
    if store_arg is not None:
        path = store_path(store_arg)
    else:
        # Without the first-run write: a scan writes nothing, a default
        # config.toml included, and the managed file's pins still apply.
        try:
            config = load_config(create=False)
        except (ValueError, OSError) as exc:
            _fail(f"the config does not load: {exc}")
        path = store_path(config.resolved_directory())
    if not path.is_file():
        named = None if store_arg is None else Path(store_arg).expanduser()
        if named is not None and named.is_file():
            # `store_path` reads any other file name as a directory, the
            # rule every store opener follows; say so rather than leave
            # "no store at backup.sqlite/memory.sqlite" to puzzle over.
            _fail(
                f"no bettermemory 9 store at {path}: --store names a store "
                f"directory or its {STORE_FILENAME} file, so copy {named.name} "
                f"to {STORE_FILENAME} in a directory of its own and scan that"
            )
        _fail(f"no bettermemory 9 store at {path}")
    from .backup import bundle_refusal

    # A bundle's snapshot is refused as every command refuses it: a
    # writing command's open switches its journal mode, which rewrites
    # the header, and the bundle would then fail its signed hash. Checked
    # through a symlink too, and, for a `--store` file, through a hard
    # link: a hard link to a bundle's snapshot is the snapshot under a
    # name no manifest sits beside.
    refusal = bundle_refusal(path, named=store_arg is not None)
    if refusal is not None:
        _fail(refusal)
    # `read_only`: a scan reads; it must not write a rekey row into the
    # log it is scanning, a key beside a copied store, or a byte of the
    # file (`store_schema.connect_read_only`).
    try:
        store = Store.open(path, read_only=True)
    except (ValueError, sqlite3.DatabaseError, OSError) as exc:
        _fail(f"cannot read the store at {path}: {exc}")
    # A store whose header opens can still fail on a page the scan reads
    # (a damaged copy). That is a store the scan cannot read, exit 2: an
    # uncaught traceback exits 1, which a CI gate would take for a finding.
    try:
        report = scan_store(store, include_log=bool(getattr(args, "scan_log", False)))
    except sqlite3.DatabaseError as exc:
        _fail(f"cannot read the store at {path}: {exc}")
    finally:
        store.close()
    only = getattr(args, "scan_only", None)
    if only is not None:
        report = report.only(only)
    if getattr(args, "scan_json", False):
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))
    raise SystemExit(1 if report.status == "found" else 0)


__all__ = [
    "CONTROL_TOKENS",
    "CONTROL_TOKEN_IDENTITY_REMEDIATION",
    "CONTROL_TOKEN_LOG_ROW_REMEDIATION",
    "CONTROL_TOKEN_REMEDIATION",
    "CREDENTIALS",
    "KINDS",
    "LIVE",
    "LOG",
    "NOT_SCANNED",
    "PRUNED",
    "REMEDIATION",
    "TABLES",
    "TOMBSTONED",
    "ControlTokenFinding",
    "ScanReport",
    "SecretFinding",
    "add_scan_parser",
    "render_report",
    "run_scan",
    "scan_store",
]
