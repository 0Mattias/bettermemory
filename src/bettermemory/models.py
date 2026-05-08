"""Pydantic models, enums, and ULID generation for bettermemory."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from .origin import Origin


# ---------------------------------------------------------------------------
# On-disk schema version
# ---------------------------------------------------------------------------
#
# Every memory and tombstone written by this version of bettermemory carries
# `schema_version: <SCHEMA_VERSION>` in its YAML frontmatter. Readers default
# to `1` when the field is missing — that's the implicit version of memories
# written before this constant existed (additive-fields-only era).
#
# Forward-compatibility rule: a reader that encounters a schema_version
# strictly greater than its own SCHEMA_VERSION raises ValueError on load.
# `Store.load_all` and friends catch ValueError and skip the file with a
# logged warning, so a user who downgrades bettermemory after writing some
# memories with a newer minor sees those memories disappear from the
# retrieval surface (rather than the reader silently misinterpreting them
# under the wrong field semantics). `bettermemory doctor`'s
# `memory_parse_health` check surfaces the count gap explicitly.
#
# Backward-compatibility rule: minor bumps within a major version
# (1 → 1+, ...) are additive-only — new optional fields, never renamed
# fields, never removed fields, never re-defined semantics. A *major* bump
# (1 → 2) is reserved for breaking format changes and ships with a
# `bettermemory migrate` subcommand that walks the store and rewrites the
# frontmatter into the new shape. Until that day, the constant stays at 1.

SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# ULID generation
# ---------------------------------------------------------------------------
#
# Crockford's Base32 alphabet (no I, L, O, U).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def generate_ulid() -> str:
    """Return a new ULID string (26 chars, Crockford Base32).

    48-bit timestamp (ms since epoch) + 80 bits of randomness.
    Lexicographic order is time order at ms resolution.
    """
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    return _encode_crockford(ts_ms, 10) + _encode_crockford(rand, 16)


_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def is_valid_ulid(s: str) -> bool:
    return bool(_ULID_RE.match(s))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Source(str, Enum):
    EXPLICIT = "explicit-statement"
    INFERRED = "inferred"
    CORRECTION = "user-correction"


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------
#
# Scopes are lowercase, alphanumeric, hyphens and colons (for nesting like
# `projects:foo`). No whitespace, no uppercase. Reject everything else at
# write time so the on-disk format stays grep-friendly.
_SCOPE_RE = re.compile(r"^[a-z0-9]+(?:[-:][a-z0-9]+)*$")


def validate_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE_RE.match(scope):
        raise ValueError(
            f"invalid scope {scope!r}: must be lowercase alphanumeric, "
            "with optional hyphens/colons (e.g. 'projects:foo')"
        )
    return scope


def _validate_scopes_list(scopes: list[str]) -> list[str]:
    if not scopes:
        raise ValueError("scopes must contain at least one entry")
    return [validate_scope(s) for s in scopes]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


ScopesField = Annotated[list[str], Field(min_length=1)]


class Memory(BaseModel):
    """Full memory record, body included.

    `origin` is optional and defaults to None. Memories written before the
    auto-scope feature shipped have no origin and are treated as "global"
    by `memory_search(auto_scope=True)`.

    `last_verified_at` is bumped by `memory_verify` (and only that tool) when
    the caller has spot-checked the body's claims against ground truth — file
    paths still exist, version numbers still match, etc. None means "never
    verified since write". Distinct from `updated`, which moves whenever
    `memory_update` rewrites content. Editing isn't verifying: a typo fix or
    a scope retag shouldn't pretend the body has been re-checked. The two
    fields together form a staleness signal — `updated` is "the body changed
    on this date", `last_verified_at` is "a human/agent confirmed the body
    matched reality on this date".
    """

    id: str
    created: datetime
    updated: datetime
    scopes: ScopesField
    confidence: Confidence
    source: Source
    body: str
    origin: Origin | None = None
    last_verified_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, v: list[str]) -> list[str]:
        return _validate_scopes_list(v)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not is_valid_ulid(v):
            raise ValueError(f"invalid ULID: {v!r}")
        return v


class MemoryHit(BaseModel):
    """One result from memory_search.

    `score` is the raw ranking number (corpus-relative — useful for sorting,
    not for thresholding by hand). `relevance` is the calibrated label —
    `"high" | "medium" | "low"` — based on what fraction of the query's
    content words actually matched. Consumers should branch on `relevance`,
    not on `score`. `match_terms` lists which query tokens hit the body or
    scopes, so the caller can sanity-check whether a hit is meaningful or
    stopword noise. `updated` lets a consumer spot stale memories at a
    glance — bumped by `memory_update`, equal to `created` on first write.
    `last_verified_at` is the orthogonal verification timestamp — None when
    the memory has never been spot-checked since it was written.

    `path_drift_checked` / `path_drift_missing` are cheap drift signals
    surfaced on every hit (not just the expanded top hit). The integers
    let the caller decide whether to spend a memory_show round-trip on a
    given hit — high `path_drift_missing` is the cue. A nominal hit
    with `path_drift_missing=0` and a non-zero `path_drift_checked` is
    a positive signal: the memory cites real paths that still exist.
    Both default to 0 (the load path doesn't run drift detection in
    other contexts, e.g. memory_show, where the full PathDriftReport
    is the right surface).
    """

    id: str
    scopes: list[str]
    confidence: Confidence
    snippet: str
    score: float
    relevance: str = "medium"
    match_terms: list[str] = []
    created: datetime
    updated: datetime
    last_verified_at: datetime | None = None
    path_drift_checked: int = 0
    path_drift_missing: int = 0


class MemorySummary(BaseModel):
    """One row from memory_list — body stripped, just a one-line summary."""

    id: str
    scopes: list[str]
    confidence: Confidence
    summary: str
    created: datetime
    updated: datetime
    last_verified_at: datetime | None = None


class TombstonedMemory(BaseModel):
    """A removed memory loaded from `.tombstones/`.

    Carries the same content fields as `Memory` plus removal metadata.
    Kept as a separate type (not a subclass of `Memory`) so callers can't
    accidentally mix active records and tombstones — typing catches it
    statically, and the dedup pass that walks both can branch explicitly.

    `removed_session` is additive: tombstones written before that field
    shipped have `None` here and the load path silently fills in the
    default. The lookup-by-session join (event log → tombstone) only
    works for tombstones written after the upgrade; older ones still
    carry `removed` / `removed_reason` for human-readable audit.
    """

    id: str
    created: datetime
    updated: datetime
    scopes: ScopesField
    confidence: Confidence
    source: Source
    body: str
    origin: Origin | None = None
    last_verified_at: datetime | None = None

    # Removal metadata. `removed` and `removed_reason` are required —
    # a tombstone without them is malformed and won't load.
    removed: datetime
    removed_reason: str
    removed_session: str | None = None

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, v: list[str]) -> list[str]:
        return _validate_scopes_list(v)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not is_valid_ulid(v):
            raise ValueError(f"invalid ULID: {v!r}")
        return v


class TombstonedSummary(BaseModel):
    """One row from `memory_list_tombstones` — body stripped, plus removal
    metadata. Mirrors `MemorySummary` in shape so triage tooling can treat
    the two uniformly modulo the extra `removed_*` fields."""

    id: str
    scopes: list[str]
    confidence: Confidence
    summary: str
    created: datetime
    updated: datetime
    last_verified_at: datetime | None = None
    removed: datetime
    removed_reason: str
    removed_session: str | None = None


class SimilarHit(BaseModel):
    """One existing memory that overlaps a candidate write.

    Surfaced by `find_similar` and by `memory_write` when it refuses to
    create a parallel entry. `similarity` is Jaccard on stopword-stripped,
    kebab-expanded token sets (or cosine when semantic dedup is on) —
    symmetric, unlike `MemoryHit.score`.

    `relevance` is one of `"high" | "medium" | "high-removed" | "medium-
    removed"`. The `-removed` suffix means the matched record is a
    tombstone, not an active memory: the user explicitly removed a
    similar fact at some point. The dedup gate treats `high-removed`
    differently from `high` — it warns the writer about a previously-
    rejected fact rather than just routing them to memory_update on an
    active id. `removed_at` and `removed_reason` are populated only for
    tombstone matches; they are `None` on hits against active memories.
    """

    id: str
    scopes: list[str]
    confidence: Confidence
    snippet: str
    similarity: float
    relevance: str
    created: datetime
    updated: datetime
    removed_at: datetime | None = None
    removed_reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware UTC `now`. Centralised so tests can monkey-patch."""
    return datetime.now(timezone.utc)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def make_slug(content: str, max_words: int = 6, max_chars: int = 60) -> str:
    """Build a filename-friendly slug from the first words of `content`."""
    text = content.strip().lower()
    # Take first line — multi-line bodies often have a leading title.
    text = text.splitlines()[0] if text else ""
    words = [w for w in _SLUG_RE.split(text) if w]
    if not words:
        words = ["memory"]
    slug = "-".join(words[:max_words])[:max_chars].strip("-")
    return slug or "memory"


def build_filename(created: datetime, slug: str) -> str:
    """`<YYYY-MM-DD>-<slug>.md`."""
    return f"{created.strftime('%Y-%m-%d')}-{slug}.md"


# Sentence boundary: terminator (.!?) followed by whitespace or end-of-string.
# The trailing-context check is what stops `user.name` or `git config` from
# being treated as a sentence break — bare `.` inside an identifier was the
# cause of summaries like "memory-mcp (a" and "git config --global user".
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


# Abbreviations whose trailing period isn't a sentence boundary. Looked up
# case-insensitively against the alnum/dot run ending at the period (so
# "e.g." matches the stored "e.g", "Mr." matches "mr", etc.). Kept short
# and conservative — adding a word here means losing one legitimate sentence
# break for every false positive, so we only list ones that show up in
# normal technical/note prose. Bare `!` and `?` aren't ambiguous, so they
# never consult this list.
_ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "etc",
        "vs",
        "cf",
        "viz",
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "jr",
        "sr",
        "st",
        "u.s",
        "u.k",
        "a.k.a",
        "a.m",
        "p.m",
        "fig",
        "no",
        "vol",
        "ca",
        "approx",
    }
)


def _word_ending_at(text: str, end: int) -> str:
    r"""Return the alnum/dot run ending just before position `end`.

    Used to recover the abbreviation candidate whose trailing period sits at
    `end` — for `"e.g. python"` with `end=3` (the second `.`) returns `"e.g"`.
    Walks back through alphanumerics and dots; stops at whitespace, punctuation
    (`,;:`), brackets, etc. Empty string when the previous char is non-word
    (e.g. closing backtick before the period in `` `...`. Next sentence ``).
    """
    i = end
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "."):
        i -= 1
    return text[i:end]


def first_summary_line(body: str, max_chars: int = 80) -> str:
    """First sentence or first ~80 chars of `body`, single line.

    Sentence boundary is `[.!?]` followed by whitespace or end-of-string,
    so dotted identifiers (`user.name`, `git config --global x`) and version
    numbers (`1.0.2`) don't get treated as sentence breaks. We also walk past
    a small known list of abbreviations (`e.g.`, `i.e.`, `etc.`, `Mr.`, `U.S.`,
    …) — without that, a body that opens with one collapses its summary to
    "e.g" or "Mr".
    """
    text = body.strip().replace("\n", " ")
    for match in _SENTENCE_END_RE.finditer(text):
        # Only `.` is ambiguous — `!` and `?` always end sentences.
        if match.group() == "." and (
            _word_ending_at(text, match.start()).lower() in _ABBREVIATIONS
        ):
            continue
        sentence = text[: match.start()].strip()
        if sentence and len(sentence) <= max_chars:
            return sentence
        # First real boundary is past max_chars — fall through to truncation
        # rather than scanning further; subsequent boundaries would only
        # produce longer sentences.
        break
    if len(text) <= max_chars:
        return text
    return _truncate_at_word(text, max_chars)


def snippet_for(body: str, max_chars: int = 200) -> str:
    """Snippet shown in search results.

    Truncates on a word boundary so we don't cut mid-token. Prevents
    snippets like "...does NOT write `git config --global user" where the
    trailing identifier is sliced — the consumer then has to round-trip to
    `memory_show` to recover what it was.
    """
    text = body.strip()
    if len(text) <= max_chars:
        return text
    return _truncate_at_word(text, max_chars)


def _truncate_at_word(text: str, max_chars: int) -> str:
    """Trim to <=max_chars, backing off to the last whitespace boundary.

    If there's no whitespace in the last 40 chars (long URL, dense code),
    we accept the hard cut — backing off too far makes the snippet empty.
    """
    truncated = text[:max_chars]
    space_idx = truncated.rfind(" ")
    if space_idx >= max_chars - 40:
        truncated = truncated[:space_idx]
    return truncated.rstrip(" ,;:.-") + "..."


# Re-export so callers don't import from os.
__all__ = [
    "Confidence",
    "Source",
    "Memory",
    "MemoryHit",
    "MemorySummary",
    "SimilarHit",
    "TombstonedMemory",
    "TombstonedSummary",
    "generate_ulid",
    "is_valid_ulid",
    "validate_scope",
    "utcnow",
    "make_slug",
    "build_filename",
    "first_summary_line",
    "snippet_for",
]
