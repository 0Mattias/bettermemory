"""Response-shape helpers for the MCP tool surface.

Pure serialisation: takes domain objects (`MemoryHit`, `MemorySummary`,
`Memory`, `Origin`, `SimilarHit`, `TransientMatch`) and the per-request
inputs they need (a pinned `now`, the caller's origin, etc.) and returns
JSON-serializable dicts. No I/O, no event recording, no session state
— that lives on `ToolHandlers`.

`ResponseBuilder` captures the one piece of config every method needs
(`verification_stale_days`) so handlers don't have to thread it through
on every call. Each method takes a `now: datetime` kwarg per request so
a single multi-hit response uses one consistent "now" across rows.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .durability import TransientMatch
from .models import (
    MemoryHit,
    MemorySummary,
    SimilarHit,
    TombstonedSummary,
)
from .origin import Origin, commit_author_timestamps, repos_match
from .time_utils import isoformat_utc as _isoformat_utc
from .time_utils import isoformat_utc_optional as _isoformat_utc_optional
from .verify import (
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)


__all__ = ["ResponseBuilder", "isoformat", "isoformat_optional"]


# `isoformat` / `isoformat_optional` are the public names this module
# has exported since 1.x; keep them as aliases over the canonical
# `time_utils` helpers so the wire-format definition lives in one
# place but downstream callers (handlers, tests) don't have to chase
# the rename.
isoformat = _isoformat_utc
isoformat_optional = _isoformat_utc_optional


class ResponseBuilder:
    """Serializer for the tool response shapes.

    Stateless except for `stale_after_days` (a config knob). Methods
    take `now` per request so a multi-row response uses one consistent
    "now" — preventing the awkward case where the first row in a result
    set is judged fresh and the last is judged stale because we crossed
    a day boundary mid-loop.
    """

    def __init__(self, *, stale_after_days: int) -> None:
        self._stale_after_days = stale_after_days

    # ---- search / show / list rows --------------------------------------

    def hit_to_dict(
        self,
        hit: MemoryHit,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Serialise a search hit, including the structured verification block.

        `last_verified_at` stays in the response as a raw timestamp for
        callers that already branch on it; the new `verification` field
        is the structured replacement.

        `staleness_verdict` is initialised here from verification +
        path_drift only; the commit-drift contribution is folded in by
        `attach_commit_drift_counts` once the per-search timestamp list
        has been read. Initial verdict is correct for hits where commit
        drift isn't applicable (caller not in a repo, hit from a
        different repo, hit never verified) — those verdicts never get
        revisited.

        `path_drift_checked` / `path_drift_missing` stay around as cheap
        triage counts on every hit. `path_drift` is the rich shape that
        carries the actual paths (`{checked, missing, verified}`),
        emitted only when the body has drift or verified attestations
        — matching `memory_show`'s `path_drift` contract. A
        `spot_check_recommended` hit with `path_drift.missing =
        ["src/auth/middleware.py"]` is directly actionable: the model
        memory_updates the rotted bit or memory_verifies the rest, no
        memory_show round-trip required.
        """
        verification = compute_verification_status(
            hit.last_verified_at, now=now, stale_after_days=self._stale_after_days
        )
        verdict = compute_staleness_verdict(
            verification=verification,
            path_drift_missing=hit.path_drift_missing,
            commit_drift_count=None,
        )
        out: dict[str, Any] = {
            "id": hit.id,
            "scopes": hit.scopes,
            "confidence": hit.confidence.value,
            "category": hit.category.value if hit.category is not None else None,
            "snippet": hit.snippet,
            "score": hit.score,
            "relevance": hit.relevance,
            "match_terms": hit.match_terms,
            "created": isoformat(hit.created),
            "updated": isoformat(hit.updated),
            "last_verified_at": isoformat_optional(hit.last_verified_at),
            "verification": verification.to_dict(),
            "path_drift_checked": hit.path_drift_checked,
            "path_drift_missing": hit.path_drift_missing,
            "staleness_verdict": verdict,
        }
        if hit.path_drift_missing_paths or hit.path_drift_verified_paths:
            out["path_drift"] = {
                "checked": list(hit.path_drift_checked_paths),
                "missing": list(hit.path_drift_missing_paths),
                "verified": list(hit.path_drift_verified_paths),
            }
        return out

    def summary_to_dict(
        self,
        summary: MemorySummary,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Serialise a memory_list summary with verification status attached.

        Same contract as `hit_to_dict`: `now` injected for consistency,
        `verification` carries the actionable signal, raw `last_verified_at`
        kept for back-compat. Listing is the cheap-triage view, exactly the
        surface where staleness should be visible — a curator scrolling the
        list shouldn't have to call memory_show to see whether a row is
        fresh.

        `staleness_verdict` here intentionally only reflects verification
        + the commit-drift signal isn't computed at the list level (the
        list view never loads bodies for path_drift, never resolves the
        per-row repo for commit_drift). A list-row verdict of "fresh"
        therefore means "calendar-fresh"; a row landing in
        spot_check_recommended via the list view would imply the
        body-level drift signal was already known, which it isn't here.
        Effectively the list verdict collapses to fresh ↔ verification
        fresh, spot_check_required ↔ never|stale.
        """
        verification = compute_verification_status(
            summary.last_verified_at, now=now, stale_after_days=self._stale_after_days
        )
        verdict = compute_staleness_verdict(
            verification=verification,
            path_drift_missing=0,
            commit_drift_count=None,
        )
        return {
            "id": summary.id,
            "scopes": summary.scopes,
            "confidence": summary.confidence.value,
            "category": summary.category.value
            if summary.category is not None
            else None,
            "summary": summary.summary,
            "created": isoformat(summary.created),
            "updated": isoformat(summary.updated),
            "last_verified_at": isoformat_optional(summary.last_verified_at),
            "verification": verification.to_dict(),
            "staleness_verdict": verdict,
        }

    def tombstone_summary_to_dict(self, summary: TombstonedSummary) -> dict[str, Any]:
        """Same shape as `summary_to_dict` plus removal metadata.

        Mirroring the active shape lets a curator iterate uniformly: a row
        has `removed` set if and only if it's a tombstone. `removed_session`
        is `null` for legacy tombstones written before that field shipped.
        """
        return {
            "id": summary.id,
            "scopes": summary.scopes,
            "confidence": summary.confidence.value,
            "category": summary.category.value
            if summary.category is not None
            else None,
            "summary": summary.summary,
            "created": isoformat(summary.created),
            "updated": isoformat(summary.updated),
            "last_verified_at": isoformat_optional(summary.last_verified_at),
            "removed": isoformat(summary.removed),
            "removed_reason": summary.removed_reason,
            "removed_session": summary.removed_session,
        }

    def memory_to_dict(  # type: ignore[no-untyped-def]
        self,
        memory,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Full memory shape used by `memory_list(with_bodies=True)`.

        Same fields as `memory_show` plus the `summary` line so a consumer
        can treat the response uniformly with the body-less `memory_list`
        shape. Includes the `verification` block for parity with the rest
        of the retrieval surface — a `with_bodies=True` listing carries the
        same staleness signal a `memory_show` would.
        """
        from .models import first_summary_line

        verification = compute_verification_status(
            memory.last_verified_at, now=now, stale_after_days=self._stale_after_days
        )
        drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
        verdict = compute_staleness_verdict(
            verification=verification,
            path_drift_missing=len(drift.missing),
            commit_drift_count=None,
        )
        return {
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "category": (
                memory.category.value if memory.category is not None else None
            ),
            "summary": first_summary_line(memory.body),
            "body": memory.body,
            "created": isoformat(memory.created),
            "updated": isoformat(memory.updated),
            "last_verified_at": isoformat_optional(memory.last_verified_at),
            "verification": verification.to_dict(),
            "staleness_verdict": verdict,
            "origin": self.origin_to_dict(memory.origin),
        }

    # ---- write / update / restore commit responses ----------------------

    def committed(  # type: ignore[no-untyped-def]
        self,
        memory,
        *,
        related: list[SimilarHit] | None = None,
        removed_related: list[SimilarHit] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Serialise a freshly-written Memory into the tool response shape.

        `related` carries any medium-overlap matches that didn't block the
        write — surfaced so the caller can still consider memory_update on a
        similar existing entry, just without a hard refusal.

        `removed_related` carries medium-overlap matches against tombstoned
        memories. Same shape as `related` plus the populated `removed_at` /
        `removed_reason` fields. Surfaced advisorily — the writer hasn't
        duplicated a previously-removed fact, but they're working in the
        same neighbourhood and may want to consult the removal reason.

        `warnings` is a list of canonical advisory codes — non-blocking
        flags surfaced on a successful commit. Currently:
        - ``"ambient_body_long"``: an ambient memory exceeded the
          long-body threshold; consider splitting.
        Empty list omitted from the response so the shape stays minimal
        on the common no-warning case.
        """
        out: dict[str, Any] = {
            "status": "committed",
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "category": (
                memory.category.value if memory.category is not None else None
            ),
            "created": isoformat(memory.created),
            "updated": isoformat(memory.updated),
            "last_verified_at": isoformat_optional(memory.last_verified_at),
        }
        if related:
            out["related"] = [self.similar_to_dict(h) for h in related]
        if removed_related:
            out["removed_related"] = [self.similar_to_dict(h) for h in removed_related]
        if warnings:
            out["warnings"] = list(warnings)
        return out

    # ---- minor row shapes ----------------------------------------------

    def similar_to_dict(self, hit: SimilarHit) -> dict[str, Any]:
        """Serialise a SimilarHit to the tool response shape.

        `removed_at` / `removed_reason` are emitted only when populated —
        active hits keep the response shape lean by omitting the keys, while
        tombstone hits carry both. Consumers can branch on
        `"removed_reason" in hit` or on `relevance.endswith("-removed")`.
        """
        out: dict[str, Any] = {
            "id": hit.id,
            "scopes": hit.scopes,
            "confidence": hit.confidence.value,
            "snippet": hit.snippet,
            "similarity": hit.similarity,
            "relevance": hit.relevance,
            "created": isoformat(hit.created),
            "updated": isoformat(hit.updated),
        }
        if hit.removed_at is not None:
            out["removed_at"] = isoformat(hit.removed_at)
        if hit.removed_reason is not None:
            out["removed_reason"] = hit.removed_reason
        return out

    def transient_to_dict(self, hit: TransientMatch) -> dict[str, Any]:
        """Serialize a transient-marker match for the tool response."""
        return {"marker": hit.marker, "snippet": hit.snippet}

    def origin_to_dict(self, origin: Origin | None) -> dict[str, Any] | None:
        """Serialize an Origin for tool responses, or None if absent.

        Empty fields are stripped so the response carries only the data that
        was actually captured at write time. A memory written before this
        feature shipped returns None; a memory written outside any git repo
        returns `{"cwd": "..."}` without `repo` or `branch` keys.
        """
        if origin is None:
            return None
        payload = origin.model_dump(mode="json", exclude_none=True)
        return payload or None

    # ---- per-search bulk decorators -------------------------------------

    def attach_commit_drift_counts(  # type: ignore[no-untyped-def]
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        memories,
        *,
        caller_origin: Origin,
    ) -> None:
        """Mutate `out` in-place, adding `commit_drift_count` to each hit
        whose memory is anchored to the caller's current repo and has been
        verified at some point.

        The signal mirrors `path_drift_checked` / `path_drift_missing`: a
        cheap integer surfaced on every hit so the model can self-triage
        which hit to expand without a memory_show round-trip. Cost is
        bounded — one `commit_author_timestamps` call for the whole search,
        then a per-hit `bisect_right` against the sorted timestamp list.
        Independent of result count.

        Omitted (key absent from the dict, not set to null) when:

        - caller is not currently in any repo (`caller_origin.repo` is None),
        - git was unreachable in the caller's cwd,
        - the hit's memory has no `origin.repo` (legacy / global memory),
        - the hit's memory's repo doesn't match the caller's,
        - the hit's memory has never been verified (no anchor to count from).

        Absence-as-signal mirrors `path_drift`'s null-when-clean contract
        and keeps the hit shape uniform: a consumer either sees the field
        or doesn't, no third "unknown" branch to filter.
        """
        if caller_origin.repo is None or caller_origin.cwd is None:
            return
        timestamps = commit_author_timestamps(Path(caller_origin.cwd))
        if timestamps is None:
            return
        timestamps_sorted = sorted(timestamps)
        # Build the id → origin.repo side-map from the in-memory `memories`
        # list (already loaded by the caller for the search itself), avoiding
        # a second store round-trip per hit.
        origin_repo_by_id: dict[str, str | None] = {
            m.id: (m.origin.repo if m.origin else None) for m in memories
        }
        for hit_dict, hit in zip(out, hits):
            if hit.last_verified_at is None:
                continue
            origin_repo = origin_repo_by_id.get(hit.id)
            if origin_repo is None:
                continue
            if not repos_match(origin_repo, caller_origin.repo):
                continue
            since = hit.last_verified_at
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # bisect_right on the ascending list gives the first index
            # strictly greater than `since`; len - idx is the count of
            # commits strictly after the verify timestamp. Equal-timestamp
            # commits are not counted as drift, matching the health
            # rollup's semantics.
            idx = bisect.bisect_right(timestamps_sorted, since)
            count = len(timestamps_sorted) - idx
            hit_dict["commit_drift_count"] = count
            # Recompute the verdict now that we have the commit-drift
            # contribution. `hit_to_dict` initialised it without that
            # input; the upgrade only fires for hits where the count was
            # actually applicable.
            verification_dict = hit_dict["verification"]
            verification_status = verification_dict["status"]
            verdict_required = verification_status in {"never", "stale"}
            if verdict_required:
                hit_dict["staleness_verdict"] = "spot_check_required"
            elif count > 0 or hit_dict.get("path_drift_missing", 0) > 0:
                hit_dict["staleness_verdict"] = "spot_check_recommended"
            else:
                hit_dict["staleness_verdict"] = "fresh"

    def attach_recent_negative_outcomes(
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        events: list[dict[str, Any]],
        *,
        now: datetime,
        window_days: int = 30,
    ) -> None:
        """Mutate `out` in-place, adding `recent_negative_outcomes` to any
        hit whose memory has been ignored or contradicted within the
        window AND not since been applied.

        Negative outcomes that have been superseded by a later applied
        event are filtered out — the user already validated the memory
        after the rejection, surfacing the rejection would be misleading.
        Only "ignored" and "contradicted" count as negative; "corrected"
        is audit-only (the model fixed and moved on); "applied" is the
        positive case.

        Shape per hit (when present):

            "recent_negative_outcomes": [
                {
                    "outcome": "ignored" | "contradicted",
                    "most_recent_ts": "...",
                    "count_in_window": N,
                    "session_id": "...",
                    "note": str | None,
                    "claim_excerpt": str | None,
                }
            ]

        Keyed by `outcome` so each hit gets at most one entry per outcome
        type (so 2 entries max — one ignored, one contradicted). The
        `claim_excerpt` field (T1.1) is the load-bearing claim recorded
        at rejection time, when present — it tells the model not just
        "this was rejected" but "*this specific claim* was rejected",
        which it can use to rephrase or skip the body's bad sentence.

        The field is OMITTED (key absent from the dict, not null) when
        no qualifying negative outcomes exist — same absence-as-signal
        contract as `commit_drift_count`.
        """
        if not out or not hits or not events:
            return

        hit_ids = {hit.id for hit in hits}
        cutoff = now.timestamp() - window_days * 86400

        # Walk events once. Build per-id timelines of use events filtered
        # to the window and to the hit ids — we don't care about events
        # for memories that aren't in this result set.
        per_id_events: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("kind") != "use":
                continue
            ts_str = event.get("ts")
            if not isinstance(ts_str, str):
                continue
            ts = _parse_iso_ts(ts_str)
            if ts is None or ts.timestamp() < cutoff:
                continue
            # Legacy fallback for `memory_ids` — same class as the
            # 70e41a4 llm.py fix. Old `use` archives have `memory_ids`.
            ids = event.get("ids") or event.get("memory_ids") or []
            for i, mid in enumerate(ids):
                if mid not in hit_ids:
                    continue
                per_id_events.setdefault(mid, []).append(
                    {
                        "ts": ts,
                        "outcome": event.get("outcome"),
                        "session": event.get("session") or event.get("session_id"),
                        "note": event.get("note"),
                        "claim_excerpt": _claim_at_index(event, i),
                    }
                )

        for hit_dict, hit in zip(out, hits):
            timeline = per_id_events.get(hit.id)
            if not timeline:
                continue
            timeline.sort(key=lambda e: e["ts"])

            # Walk timeline chronologically. An applied event after a
            # negative supersedes it: the user validated the memory
            # after rejecting it earlier, so the rejection no longer
            # tells us anything actionable. Clear the active buckets
            # on each applied event so only post-applied negatives
            # surface.
            ignored_active: list[dict[str, Any]] = []
            contradicted_active: list[dict[str, Any]] = []
            for entry in timeline:
                outcome = entry["outcome"]
                if outcome == "applied":
                    ignored_active.clear()
                    contradicted_active.clear()
                elif outcome == "ignored":
                    ignored_active.append(entry)
                elif outcome == "contradicted":
                    contradicted_active.append(entry)
                # "corrected" is audit-only; skip.

            entries: list[dict[str, Any]] = []
            for bucket, outcome_label in (
                (ignored_active, "ignored"),
                (contradicted_active, "contradicted"),
            ):
                if not bucket:
                    continue
                most_recent = bucket[-1]
                entries.append(
                    {
                        "outcome": outcome_label,
                        "most_recent_ts": isoformat(most_recent["ts"]),
                        "count_in_window": len(bucket),
                        "session_id": most_recent.get("session"),
                        "note": most_recent.get("note"),
                        "claim_excerpt": most_recent.get("claim_excerpt"),
                    }
                )

            if entries:
                hit_dict["recent_negative_outcomes"] = entries


def _parse_iso_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp tolerant of the `Z` suffix used in event
    logs. Returns None on malformed input rather than raising — a single
    bad event shouldn't break the whole annotation pass."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _claim_at_index(event: dict[str, Any], index: int) -> str | None:
    """Pluck the `claim_excerpts[index]` entry from a use event if the
    event carries the field (T1.1). Returns None when claim_excerpts
    is absent or the index is out of range — both legitimate states
    for older events written before T1.1 landed."""
    excerpts = event.get("claim_excerpts")
    if not isinstance(excerpts, list) or index >= len(excerpts):
        return None
    value = excerpts[index]
    return value if isinstance(value, str) else None
