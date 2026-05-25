"""Cross-cutting datetime helpers — the canonical ISO-8601 parse / format
pair used everywhere we read or write a `ts` field on an event, frontmatter
key, or response body.

Pre-Round-2 these duplicated across `audit.py`, `health.py`, `eval.py`,
`_handlers.py`, `_response.py` and a few render paths. The duplicates
all had the same shape — strip the trailing ``Z``, parse with
``datetime.fromisoformat``, stamp UTC on the naive result — but each
ad-hoc copy was just different enough that a fix to one (e.g. permissive
``.timestamp()`` fallback) had to be re-applied n times. Centralising
keeps the parse semantics one definition.

`parse_event_ts`, `isoformat_utc`, `ensure_utc` are the public surface.
They're intentionally permissive: a malformed input returns ``None`` (for
parse) or the input unchanged (for format) so the caller can skip a single
bad row without crashing the whole sweep. The Recorder always emits a
well-formed canonical string; this lenience exists for legacy fixtures
and hand-edited events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_event_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``ts`` value into a tz-aware UTC datetime.

    Accepts the recorder's canonical ``YYYY-MM-DDTHH:MM:SS.fffZ`` shape
    and the equivalent ``+00:00`` variant. Returns ``None`` when the
    input is not a string or cannot be parsed — callers iterate event
    streams and skip malformed rows rather than aborting the sweep.

    Naive results (legacy fixtures, hand-written events) are stamped as
    UTC so the return value is always tz-aware. That's the property
    every downstream comparison relies on; without it, a naive ts would
    raise ``TypeError`` when compared against the tz-aware cutoff every
    rollup derives from ``datetime.now(timezone.utc)``.
    """
    if not isinstance(value, str):
        return None
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def isoformat_utc(dt: datetime) -> str:
    """Render a datetime as the recorder's canonical
    ``YYYY-MM-DDTHH:MM:SS.fffZ`` string.

    Replaces the trailing ``+00:00`` with ``Z`` so the on-disk shape is
    stable across every event / response / frontmatter site. Use this
    everywhere we emit a ts — the alternative (each call site doing its
    own ``.isoformat().replace("+00:00", "Z")``) is what we're
    centralising away from.

    The input must be tz-aware (UTC); naive inputs render without the
    offset and would not round-trip through ``parse_event_ts``. Callers
    that may hold a naive datetime should run it through ``ensure_utc``
    first.
    """
    return dt.isoformat().replace("+00:00", "Z")


def isoformat_utc_optional(dt: datetime | None) -> str | None:
    """ISO-format `dt`, returning None when the input is None.

    Distinct from `isoformat_utc` because `None` is a meaningful response
    value for `last_verified_at` — "never verified" is a valid state, not
    an error. Returning the literal None lets JSON-serialisation produce
    `"last_verified_at": null` which the caller can branch on directly.
    """
    return None if dt is None else isoformat_utc(dt)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Stamp naive datetimes as UTC; pass tz-aware datetimes through.

    The event-log timestamps the recorder writes are always UTC; naive
    `created` fields from older test fixtures (pre-tz models) are treated
    as UTC too. Returns the input on tz-aware datetimes and ``None`` on
    ``None`` — the optional-return shape matches the parse helper so the
    common ``ensure_utc(parse_event_ts(raw))`` pipeline composes cleanly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "ensure_utc",
    "isoformat_utc",
    "isoformat_utc_optional",
    "parse_event_ts",
]
