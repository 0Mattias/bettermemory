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
    """One result from memory_search."""

    id: str
    scopes: list[str]
    confidence: Confidence
    snippet: str
    score: float
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


def first_summary_line(body: str, max_chars: int = 80) -> str:
    """First sentence or first ~80 chars of `body`, single line."""
    text = body.strip().replace("\n", " ")
    # Sentence split on `. ` or end of string.
    if "." in text:
        sentence = text.split(".", 1)[0].strip()
        if sentence and len(sentence) <= max_chars:
            return sentence
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def snippet_for(body: str, max_chars: int = 200) -> str:
    """Snippet shown in search results."""
    text = body.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


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
