"""Verification staleness: how long ago a memory's claims were spot-checked.

`compute_verification_status` classifies a memory's `last_verified_at` as
``never``, ``stale`` or ``fresh``, and `remote_verification_status` builds
the ``remote`` block a synced record's stamp reads as on a host that never
made it; `VerificationStatus` is the block both return. `verify` re-exports
every name here, where callers import them.

This module lives apart from `verify.py` to keep it under the package's
1,900-line watch line (ENG-97). It imports the standard library alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Verification staleness
# ---------------------------------------------------------------------------
#
# `last_verified_at` is the timestamp the `memory_verify` tool stamps when an
# agent has confirmed a memory's body still matches reality. Null means
# never verified. The retrieval surface used to expose the raw timestamp
# and lean on prose guidance to make the consuming model do the staleness
# arithmetic — which fails open whenever the model's attention wavers.
#
# Replacing the raw timestamp with a structured `verification` block (status,
# age, recommendation) puts the staleness verdict in the response payload
# itself. The model can ignore prose; it cannot easily ignore a literal
# `recommendation` string sitting next to the body it just retrieved. That
# inversion — from "model decides whether to spot-check" to "tool decides
# whether to ask the model to spot-check" — is the whole point. This caught
# a real-world drift in the field (a memory whose tool list lagged the code
# by three new tools). The cost of being too cautious here is one extra
# spot-check per retrieval; the cost of being too lax is exactly the kind
# of stale-memory incident this project exists to prevent.


# Default freshness window. After this many days, a verified memory flips
# from "fresh" to "stale" and gets a re-spot-check recommendation. 30 days
# matches the recency-boost half-life (`recency_boost_half_life_days`) —
# memories the ranker is no longer treating as "fresh" for ordering also
# stop counting as "fresh" for verification. Override via
# `behavior.verification_stale_days` in config.toml.
DEFAULT_VERIFICATION_STALE_DAYS = 30


@dataclass(frozen=True)
class VerificationStatus:
    """Structured staleness verdict for a memory's `last_verified_at`.

    `status` is one of:

    - ``"never"``: `last_verified_at` is None — the memory has not been
      spot-checked since it was written. Highest-risk profile; the body
      may have been wrong on day 1, may have drifted since, and no
      human/agent has confirmed otherwise.
    - ``"stale"``: `last_verified_at` is set, but more than
      `stale_after_days` ago. The world may have moved on since the
      last spot-check.
    - ``"fresh"``: verified within the staleness window. No action
      needed beyond the usual path-drift triage.
    - ``"remote"`` (6.6.0): the file carries a `last_verified_at`, but it
      arrived by `sync pull` and this host has never verified the record
      since (`index.verified_locally_at` is NULL on a `synced` row). The
      stamp is another host's, or an attacker's; on this host it is not
      evidence. Built by `remote_verification_status`, applied by
      `_response.ResponseBuilder.apply_trust`.

    `age_days` is the integer day count since the last verification, or
    None when status is ``"never"`` or ``"remote"``. `recommendation` is a short
    actionable string aimed at the retrieving model — non-None for
    ``"never"`` and ``"stale"``, None for ``"fresh"``. Putting the
    recommendation in the payload (rather than only in the prose system
    prompt) is the load-bearing piece: a model scanning structured
    fields cannot miss the explicit ask to spot-check.
    """

    status: str
    last_verified_at: datetime | None
    age_days: int | None
    recommendation: str | None
    stale_after_days: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialization for the tool response.

        `last_verified_at` is rendered ISO-8601 with the `+00:00` suffix
        normalised to ``Z`` to match the rest of the server's datetime
        formatting. The block is emitted in full on every retrieval
        (including ``"fresh"``) so a consumer can branch on a stable
        shape — `recommendation: null` is the explicit "nothing to do"
        signal.
        """
        return {
            "status": self.status,
            "last_verified_at": (
                None
                if self.last_verified_at is None
                else self.last_verified_at.isoformat().replace("+00:00", "Z")
            ),
            "age_days": self.age_days,
            "recommendation": self.recommendation,
            "stale_after_days": self.stale_after_days,
        }


# Recommendation strings live as module constants so tests can match on
# them without duplicating prose, and so future tone tweaks happen in one
# place. Both end with the concrete tool call to make next-step routing
# unambiguous from the model's side.
_NEVER_RECOMMENDATION = (
    "This memory has never been spot-checked since it was written. Before "
    "relying on any specific claim it makes (file path, commit hash, "
    "version, configuration, list of items), confirm at least one against "
    "ground truth. If the claim still holds, call memory_verify(id, "
    "note=...) to record the check. If it has drifted, fix the body via "
    "memory_update first, then memory_verify the corrected memory."
)


def _stale_recommendation(age_days: int) -> str:
    return (
        f"Last spot-checked {age_days} days ago — past the freshness window. "
        "Re-confirm at least one verifiable claim (path, commit, version, "
        "config, list) and call memory_verify(id, note=...) to refresh, or "
        "memory_update if a claim has drifted."
    )


def _remote_recommendation(last_verified_at: datetime) -> str:
    stamp = last_verified_at.isoformat().replace("+00:00", "Z")
    return (
        f"This memory arrived by sync pull and its verification stamp ({stamp}) "
        "was written on another host; this host has not checked it, so the "
        "stamp is not evidence here. Confirm at least one claim against "
        "ground truth on this machine and call memory_verify(id, note=..., "
        "verified_paths=[...]) with paths that resolve here to adopt it, or "
        "memory_update if a claim has drifted."
    )


def remote_verification_status(
    last_verified_at: datetime,
    *,
    stale_after_days: int = DEFAULT_VERIFICATION_STALE_DAYS,
) -> VerificationStatus:
    """The verification block for a synced record whose stamp this host
    never made (see the ``"remote"`` status on `VerificationStatus`).

    `last_verified_at` is the stamp the file carries, kept in the block
    for transparency; `age_days` is None because the calendar leg
    measures distance from a local check and there was none.
    `verdict_from_signals` reads ``"remote"`` like ``"never"``: no local
    anchor, so the verdict is `spot_check_required` whatever the drift
    legs say.
    """
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
    return VerificationStatus(
        status="remote",
        last_verified_at=last_verified_at,
        age_days=None,
        recommendation=_remote_recommendation(last_verified_at),
        stale_after_days=max(0, stale_after_days),
    )


def compute_verification_status(
    last_verified_at: datetime | None,
    *,
    now: datetime,
    stale_after_days: int = DEFAULT_VERIFICATION_STALE_DAYS,
) -> VerificationStatus:
    """Classify a memory's verification staleness.

    `now` is injected rather than read from the clock so callers can fix
    a single timestamp across a multi-hit retrieval (consistent with how
    `search.search` threads its own `now`) and tests can pin time. A
    timezone-naive `last_verified_at` is treated as UTC, matching the
    convention `search_bm25._recency_factor` uses — every datetime in the
    store is UTC-aware in practice, but defensive normalisation keeps a
    legacy file from raising.

    `stale_after_days <= 0` collapses the "fresh" window to nothing —
    every verified memory becomes "stale" immediately. Useful for tests
    that want the stale-recommendation branch without sleeping. A
    negative value behaves the same as 0 (clamped), to avoid a silent
    inverted comparison.

    Boundary semantic: the comparison is strict-greater on the actual
    elapsed seconds (not the floored day count), so a memory at
    exactly `stale_after_days` of age is still fresh and only flips
    to stale once it crosses the threshold. The intuitive reading of
    "fresh for 30 days, then stale" then matches the implementation —
    without the strict boundary the verdict flipped at midnight UTC
    on day 30 instead of day 31, the audit's "fresh at 23:59, stale
    at 00:01" surprise. Comparing in seconds (rather than the floored
    `age_days`) keeps the zero-threshold carve-out intact: any
    measurable elapsed time satisfies `age_seconds > 0`, so
    `stale_after_days=0` still flips immediately.
    """
    threshold = max(0, stale_after_days)

    if last_verified_at is None:
        return VerificationStatus(
            status="never",
            last_verified_at=None,
            age_days=None,
            recommendation=_NEVER_RECOMMENDATION,
            stale_after_days=threshold,
        )

    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (now - last_verified_at).total_seconds())
    age_days = int(age_seconds // 86400)
    threshold_seconds = threshold * 86400

    if age_seconds > threshold_seconds:
        return VerificationStatus(
            status="stale",
            last_verified_at=last_verified_at,
            age_days=age_days,
            recommendation=_stale_recommendation(age_days),
            stale_after_days=threshold,
        )

    return VerificationStatus(
        status="fresh",
        last_verified_at=last_verified_at,
        age_days=age_days,
        recommendation=None,
        stale_after_days=threshold,
    )


__all__ = [
    "DEFAULT_VERIFICATION_STALE_DAYS",
    "VerificationStatus",
    "compute_verification_status",
    "remote_verification_status",
]
