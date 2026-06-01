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
from .origin import (
    Origin,
    commit_author_timestamps,
    commits_since_touching_paths,
    repos_match,
    should_include_for_caller,
)
from .time_utils import isoformat_utc as _isoformat_utc
from .time_utils import isoformat_utc_optional as _isoformat_utc_optional
from .time_utils import parse_event_ts
from .verify import (
    _VERDICT_FRESH,
    _VERDICT_RAISE_STATUSES,
    _VERDICT_RECOMMENDED,
    _VERDICT_REQUIRED,
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
        # verified_paths per id: when present, the count is narrowed to
        # commits that touched the attested paths, matching what
        # memory_show does. Without this, the loud search surface ignored
        # verified_paths and nagged spot_check_recommended on a memory the
        # user deliberately attested as stable — defeating the feature on
        # its highest-traffic surface and disagreeing with memory_show.
        verified_paths_by_id: dict[str, list[str]] = {
            m.id: list(m.verified_paths) for m in memories
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
            # If the memory carries verified_paths, narrow to commits that
            # touched those paths (mirrors memory_show / the expand_top
            # block), so an attestation of stable paths suppresses drift
            # noise here too. Bounded to the few hits with verified_paths;
            # falls back to the unfiltered count when the path filter can't
            # run (git unreachable, all paths outside the repo).
            vpaths = verified_paths_by_id.get(hit.id) or []
            if vpaths:
                filtered = commits_since_touching_paths(
                    Path(caller_origin.cwd), since, vpaths
                )
                if filtered is not None:
                    count = filtered
            hit_dict["commit_drift_count"] = count
            # Recompute the verdict now that we have the commit-drift
            # contribution. `hit_to_dict` initialised it without that
            # input; the upgrade only fires for hits where the count was
            # actually applicable.
            verification_dict = hit_dict["verification"]
            verification_status = verification_dict["status"]
            # Mirror the gate in `compute_staleness_verdict` — same
            # closed-protocol whitelist, single source of truth. Silent
            # divergence here would let `memory_search`'s top hit
            # surface a different verdict than `memory_show` does for
            # the same stale memory.
            verdict_required = verification_status in _VERDICT_RAISE_STATUSES
            # Mirror the tier strings ``compute_staleness_verdict``
            # emits at ``verify.py`` — same closed-protocol output, one
            # source of truth. A rename of any tier in ``verify.py``
            # that didn't reach this recompute would silently desync
            # the ``memory_search`` top-hit verdict from the
            # ``memory_show`` verdict for the same memory.
            if verdict_required:
                hit_dict["staleness_verdict"] = _VERDICT_REQUIRED
            elif count > 0 or hit_dict.get("path_drift_missing", 0) > 0:
                hit_dict["staleness_verdict"] = _VERDICT_RECOMMENDED
            else:
                hit_dict["staleness_verdict"] = _VERDICT_FRESH

    def attach_depends_on_resolved(  # type: ignore[no-untyped-def]
        self,
        out: list[dict[str, Any]],
        hits: list[MemoryHit],
        memories,
        *,
        max_per_hit: int = 3,
        max_total: int = 10,
        caller_origin: Origin | None = None,
        excluded_scopes: set[str] | None = None,
        store: Any = None,
    ) -> None:
        """Mutate `out` in-place, adding `depends_on_resolved` to any hit
        whose memory carries `depends_on`-typed links.

        `MemoryLink.depends_on` has been in the schema since 2.x but
        retrieval has never surfaced the linked content automatically —
        the model had to call `memory_show` on each target to see what
        the hit depended on. This decorator closes that gap for the
        common case: when a hit has explicit dependencies, the model
        sees their summaries inline.

        Bounded: at most `max_per_hit` resolved entries per hit, at
        most `max_total` across the whole result set. Without the
        caps a hit with N dependencies (each with M reverse-links)
        could balloon the response; the caps keep the surface
        predictable for the model.

        Tombstoned targets are skipped silently — they remain
        accessible via `memory_list_tombstones` if the caller needs
        to investigate. A pruned target shouldn't surface as a stale
        link in normal retrieval flow.

        Shape per resolved entry:

            {
                "id": "...",
                "scopes": [...],
                "summary": "...",   # first_summary_line of the target's body
                "link_note": str | None,
            }

        Omitted (key absent from the dict, not null) when the hit has
        no `depends_on` links or all the targets are tombstoned —
        same absence-as-signal contract as the other attach_* helpers.

        `caller_origin` and `excluded_scopes` re-apply the caller-side
        scope filter the search layer ran against the hit list. The
        side-map below is built from the pre-filter loader output (so
        cross-repo and session-disabled targets are still resolvable
        by id), but the deliberate scope/origin filter on the hit
        list must extend to depended-on summaries too — otherwise a
        memory in a session-disabled scope (or in a different
        project under `auto_scope=True`) leaks back in via the
        dependency edge. Default both to None for back-compat: an
        omitted `caller_origin` means "no auto-scope check" (mirroring
        `auto_scope=False` at the caller), and an omitted
        `excluded_scopes` means "no session disables".

        `store` enables the targeted-load fallback: a `depends_on`
        target unrelated to the query is not in the FTS prefilter
        candidate set (`memories` is capped at 50 query-relevant
        rows), so the side-map built from it would silently skip the
        target — defeating the auto-pull contract for the cross-topic
        case (B depends_on A precisely because A provides context B
        needs to be intelligible; the query that finds B usually
        won't find A). When `store` is supplied, missing target ids
        referenced by any hit are loaded directly via `store.load_one`
        and merged into the side-map, capped at `max_total` loads
        per call so the targeted path can't dominate the response
        budget. Tombstoned or missing targets are absorbed silently
        (same as the pre-existing prefilter-miss branch). The same
        `caller_origin` + `excluded_scopes` filter is re-applied to
        the loaded targets at the same place as side-map hits, so
        the targeted-load path cannot reintroduce the scope-leak
        that the per-target filter closed for the prefilter path.
        Default `None` for back-compat: when omitted, behaviour is
        identical to before (silent prefilter-miss skip).
        """
        from .models import first_summary_line
        from .store import MemoryNotFoundError, TombstonedError

        # Build the id → memory side-map once. The caller (`search.py`)
        # has already paid the load cost for ranking; reuse it here.
        memory_by_id = {m.id: m for m in memories}
        excluded = excluded_scopes or set()

        # Targeted-load fallback. Without this, a `depends_on` target
        # whose text doesn't match the query is never in the FTS
        # prefilter candidate set, so `memory_by_id.get(...)` returns
        # None and we silently skip — the exact case the auto-pull
        # feature exists to handle (B depends_on A because A provides
        # context the query for B usually won't surface). When a store
        # handle is supplied, collect every depends_on target id any
        # hit's source carries, subtract the ids already resolvable
        # via the side-map, and `load_one` the rest. Cap the load count
        # at `max_total` so a pathological hit (1000 depends_on links)
        # can't balloon the lookup budget — `max_total` is the natural
        # ceiling because nothing past that ever appears in the output
        # anyway. Tombstoned / removed targets raise here; absorb as
        # missing so the resolved list mirrors the pre-existing
        # "silently skip" semantics of the prefilter-miss branch.
        if store is not None:
            missing_target_ids: list[str] = []
            seen: set[str] = set()
            for hit in hits:
                source = memory_by_id.get(hit.id)
                if source is None or not source.links:
                    continue
                for link in source.links:
                    if link.type.value != "depends_on":
                        continue
                    tid = link.target_id
                    if tid in memory_by_id or tid in seen:
                        continue
                    seen.add(tid)
                    missing_target_ids.append(tid)
                    if len(missing_target_ids) >= max_total:
                        break
                if len(missing_target_ids) >= max_total:
                    break
            for tid in missing_target_ids:
                try:
                    target = store.load_one(tid)
                except (MemoryNotFoundError, TombstonedError, OSError):
                    # Same silent-skip semantics as the existing
                    # prefilter-miss branch below: a deleted or
                    # tombstoned target is best-effort dropped from
                    # auto-pull. The link still exists on disk for
                    # explicit `memory_show` investigation.
                    #
                    # OSError is in the set because `store.load_one` →
                    # `frontmatter.load(path)` reads the target's backing
                    # file and does not guard OSError itself; a transient
                    # read failure (EIO, a flaky network mount) on a
                    # depends-on target would otherwise propagate out of
                    # `attach_depends_on_resolved` and abort an
                    # otherwise-successful `memory_search`. Dropping the
                    # unreadable target from the auto-pull mirrors the
                    # missing/tombstoned skip and matches the OSError guard
                    # already on the `expand_top` body load in
                    # `handlers/search.py`.
                    continue
                # Apply the caller's scope/origin filter at load time
                # — must mirror the per-target check below so a
                # targeted load can't sneak a session-disabled or
                # cross-project target into the side-map. Without
                # this, the targeted-load path would reintroduce the
                # exact scope leak the per-target check (bf92912)
                # closed for the prefilter path.
                if excluded and (set(target.scopes) & excluded):
                    continue
                if caller_origin is not None and not should_include_for_caller(
                    target.origin,
                    caller_origin.repo,
                    caller_worktree_root=caller_origin.worktree_root,
                ):
                    continue
                memory_by_id[tid] = target

        total = 0
        for hit_dict, hit in zip(out, hits):
            if total >= max_total:
                break
            source = memory_by_id.get(hit.id)
            if source is None or not source.links:
                continue
            depends_links = [
                link for link in source.links if link.type.value == "depends_on"
            ]
            if not depends_links:
                continue

            resolved: list[dict[str, Any]] = []
            for link in depends_links:
                if len(resolved) >= max_per_hit or total >= max_total:
                    break
                target = memory_by_id.get(link.target_id)
                if target is None:
                    # Not in the loaded candidate set (the target may be
                    # outside the search's auto-scope, just not in the
                    # FTS5 prefilter, or — when `store` was supplied —
                    # dropped by the targeted-load filter above as
                    # tombstoned / scope-excluded / cross-project).
                    # Skip silently — the link still exists on disk
                    # and `memory_show` will surface it; auto-pull is
                    # a best-effort surface.
                    continue
                # Re-apply the caller's scope filter to the resolved
                # target. The side-map is built from the pre-filter
                # loader output (so we can find targets by id at all),
                # but a depended-on memory in a session-disabled scope
                # or a different project under auto_scope must not
                # leak in via the dependency edge.
                if excluded and (set(target.scopes) & excluded):
                    continue
                if caller_origin is not None and not should_include_for_caller(
                    target.origin,
                    caller_origin.repo,
                    caller_worktree_root=caller_origin.worktree_root,
                ):
                    continue
                resolved.append(
                    {
                        "id": target.id,
                        "scopes": list(target.scopes),
                        "summary": first_summary_line(target.body),
                        "link_note": link.note,
                    }
                )
                total += 1

            if resolved:
                hit_dict["depends_on_resolved"] = resolved

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
            # Canonical parse: tz-aware UTC, or None on malformed input.
            # An offset-less event ts (e.g. a hand-written or legacy
            # "2026-05-31T12:00:00") would otherwise come back naive, and
            # then (a) `ts.timestamp()` below would read the wall-clock in
            # the host's LOCAL zone — silently mis-windowing the event by
            # the local UTC offset — and (b) the `timeline.sort(...)` further
            # down would mix naive and tz-aware datetimes and raise
            # `TypeError`. `parse_event_ts` stamps UTC on offset-less input
            # so both the window math and the sort stay correct everywhere.
            ts = parse_event_ts(ts_str)
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
                        "auto": bool(event.get("auto")),
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

            # Walk timeline chronologically. A GENUINE applied event after a
            # negative supersedes it: the user/model validated the memory
            # after rejecting it earlier, so the rejection no longer tells us
            # anything actionable. But the auto-`record_use` fallback emits
            # outcome="applied" with no model/user judgment — it fires merely
            # because a re-surfaced use-token aged past its ~2-turn TTL. An
            # auto-apply must NOT clear a contradiction, or a memory the model
            # explicitly flagged as wrong would lose its warning the next time
            # it's retrieved. Only a non-auto applied event supersedes.
            ignored_active: list[dict[str, Any]] = []
            contradicted_active: list[dict[str, Any]] = []
            for entry in timeline:
                outcome = entry["outcome"]
                if outcome == "applied" and not entry.get("auto"):
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
