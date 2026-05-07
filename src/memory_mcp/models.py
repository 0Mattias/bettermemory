"""Pydantic models, enums, and ULID generation for memory-mcp."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


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
    """Full memory record, body included."""

    id: str
    created: datetime
    updated: datetime
    scopes: ScopesField
    confidence: Confidence
    source: Source
    body: str

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
    stopword noise.
    """

    id: str
    scopes: list[str]
    confidence: Confidence
    snippet: str
    score: float
    relevance: str = "medium"
    match_terms: list[str] = []
    created: datetime


class MemorySummary(BaseModel):
    """One row from memory_list — body stripped, just a one-line summary."""

    id: str
    scopes: list[str]
    confidence: Confidence
    summary: str
    created: datetime


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


def first_summary_line(body: str, max_chars: int = 80) -> str:
    """First sentence or first ~80 chars of `body`, single line.

    Sentence boundary is `[.!?]` followed by whitespace or end-of-string,
    so dotted identifiers (`user.name`, `git config --global x`) and version
    numbers (`1.0.2`) don't get treated as sentence breaks.
    """
    text = body.strip().replace("\n", " ")
    match = _SENTENCE_END_RE.search(text)
    if match:
        sentence = text[: match.start()].strip()
        if sentence and len(sentence) <= max_chars:
            return sentence
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
    "generate_ulid",
    "is_valid_ulid",
    "validate_scope",
    "utcnow",
    "make_slug",
    "build_filename",
    "first_summary_line",
    "snippet_for",
]
