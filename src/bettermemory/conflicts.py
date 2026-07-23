"""Corpus-level contradiction candidates: detection + verdict queue.

The store has always been able to represent a contradiction (the
`contradicts` link type) and to *hear about* one (`record_use
outcome=contradicted` — the model vs. the world at use time). What it
could not do is NOTICE one on its own: two stored memories disagreeing
with EACH OTHER sat unflagged until one of them happened to be
retrieved and judged. The dedup passes even computed the evidence and
threw it away — `polarity_skipped` pairs surfaced on a consolidate
report and died with it, re-derived and re-shown every run.

This module is the persistence + lifecycle half of corpus-level
contradiction detection. The DETECTION stays mechanical and lives in
`consolidate`'s pairwise dedup scan (high similarity + a polarity flip
or a numeric divergence — see `_find_dedup_with_skips`); the JUDGMENT
stays with the model (the `memory_conflicts` MCP tool lists pending
pairs and takes a verdict). The split mirrors the architecture
everywhere else in this codebase: the server does the corpus-scale
mechanical work no conversation would ever do by hand, the calling
model does the semantics.

Verdict lifecycle, designed for convergence (a dismissed pair must
never haunt every future scan):

- ``pending``: detected, awaiting judgment. Re-detection refreshes
  similarity/summaries but never duplicates the row (stable pair id).
- ``confirmed``: the model judged it a real contradiction; a
  `contradicts` link now ties the pair (written by the handler BEFORE
  the verdict is stamped, so the link-write's `updated` bump cannot
  re-trigger anything). Terminal: the link is the durable artifact,
  retrieval surfaces it on both memories, and re-arbitrating it would
  add nothing.
- ``dismissed``: the model judged the pair compatible. Sticky across
  scans — UNLESS either member's `updated` later moves past the
  verdict timestamp, in which case the content the verdict judged no
  longer exists and the pair resurrects as pending. (The same
  claim-vs-resolution ordering rule `health._has_unresolved_contradiction`
  and the outcome-demotion tally use.)

Rows whose members stop being active (tombstoned, merged) are dropped
on the next full-corpus upsert — a conflict with a dead side is moot.

On-disk: ``<root>/.conflicts.jsonl`` — one JSON object per line,
0o600, atomically rewritten under a per-file ``flock`` (the
`memory_conflicts` handler and the auto-consolidate scan can race
across processes; same discipline as `proposals.ProposalQueue`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._fsutil import atomic_write_bytes, flock_excl
from .models import Memory, utcnow
from .time_utils import parse_event_ts

log = logging.getLogger("bettermemory.conflicts")

CONFLICTS_FILENAME = ".conflicts.jsonl"

_VALID_STATUSES = ("pending", "confirmed", "dismissed")


def _pair_id(a_id: str, b_id: str) -> str:
    """Stable, order-independent id for a memory pair. Detector is
    deliberately NOT part of the key: a pair flagged by both the
    polarity and the numeric detector is one disagreement to arbitrate,
    not two."""
    lo, hi = sorted((a_id, b_id))
    return "cf-" + hashlib.sha256(f"{lo}:{hi}".encode("utf-8")).hexdigest()[:12]


@dataclass
class ConflictCandidate:
    """One suspected memory-vs-memory contradiction awaiting (or past)
    judgment. `detector` says WHY the pair was flagged: ``"polarity"``
    (negation flip between near-identical bodies) or ``"numeric"``
    (near-identical bodies whose number-bearing tokens diverge — ports,
    versions, dates)."""

    id: str
    a_id: str
    b_id: str
    summary_a: str
    summary_b: str
    similarity: float
    method: str
    detector: str
    created: str
    status: str = "pending"
    verdict_ts: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "a_id": self.a_id,
            "b_id": self.b_id,
            "summary_a": self.summary_a,
            "summary_b": self.summary_b,
            "similarity": round(self.similarity, 4),
            "method": self.method,
            "detector": self.detector,
            "created": self.created,
            "status": self.status,
        }
        if self.verdict_ts is not None:
            out["verdict_ts"] = self.verdict_ts
        if self.note is not None:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConflictCandidate":
        status = str(raw.get("status", "pending"))
        if status not in _VALID_STATUSES:
            status = "pending"
        return cls(
            id=str(raw["id"]),
            a_id=str(raw["a_id"]),
            b_id=str(raw["b_id"]),
            summary_a=str(raw.get("summary_a", "")),
            summary_b=str(raw.get("summary_b", "")),
            similarity=float(raw.get("similarity", 0.0)),
            method=str(raw.get("method", "jaccard")),
            detector=str(raw.get("detector", "polarity")),
            created=str(raw.get("created", "")),
            status=status,
            verdict_ts=(
                str(raw["verdict_ts"]) if raw.get("verdict_ts") is not None else None
            ),
            note=(str(raw["note"]) if raw.get("note") is not None else None),
        )


def skip_to_candidate(pair: Any, *, created: str) -> ConflictCandidate:
    """Lift a consolidate `PolaritySkippedPair` (either detector) into a
    queue row. Shared by both producers — the `memory_conflicts(scan=True)`
    handler and the consolidate apply pass — so the mapping can't drift."""
    return ConflictCandidate(
        id=_pair_id(pair.memory_id_a, pair.memory_id_b),
        a_id=pair.memory_id_a,
        b_id=pair.memory_id_b,
        summary_a=pair.summary_a,
        summary_b=pair.summary_b,
        similarity=pair.similarity,
        method=pair.method,
        detector=getattr(pair, "detector", "polarity"),
        created=created,
    )


def find_conflict_candidates(
    memories: list[Memory],
    *,
    semantic_model: Any | None = None,
    threshold: float | None = None,
) -> list[ConflictCandidate]:
    """Run the dedup scan and lift its conflict-shaped skips into
    candidates. Pure detection — no queue I/O; `scan_conflicts` is the
    persistence wrapper."""
    from .consolidate import _find_dedup_with_skips

    _, skipped, _method = _find_dedup_with_skips(
        memories, semantic_model=semantic_model, threshold=threshold
    )
    now_iso = utcnow().isoformat()
    out: list[ConflictCandidate] = []
    seen: set[str] = set()
    for pair in skipped:
        cand = skip_to_candidate(pair, created=now_iso)
        if cand.id in seen:
            # Same pair via two detectors: keep the first (higher-
            # similarity ordering upstream makes it the stronger frame).
            continue
        seen.add(cand.id)
        out.append(cand)
    return out


@dataclass
class ConflictQueue:
    """The on-disk conflict queue rooted at a memory store directory.
    Cheap to construct (no I/O until a method is called); every
    mutation is a read-modify-write under a per-file ``flock``."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / CONFLICTS_FILENAME

    def load(self) -> list[ConflictCandidate]:
        """All rows, file order. Malformed lines are skipped defensively
        (same discipline as `ProposalQueue.load`)."""
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[ConflictCandidate] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            try:
                out.append(ConflictCandidate.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def pending(self) -> list[ConflictCandidate]:
        return [c for c in self.load() if c.status == "pending"]

    def upsert_scan(
        self,
        fresh: list[ConflictCandidate],
        memories_by_id: dict[str, Memory],
    ) -> dict[str, int]:
        """Merge one FULL-CORPUS scan's candidates into the queue.

        `memories_by_id` must cover the whole active set — it doubles as
        the liveness authority: rows with a non-active member are
        dropped (a conflict with a tombstoned side is moot). Callers
        scanning a subset must not call this.

        Per row: new pair → pending; pending → refresh summaries and
        similarity (bodies may have drifted since detection); dismissed
        → resurrect to pending ONLY when a member's `updated` postdates
        the verdict (the judged content no longer exists); confirmed →
        terminal, left alone. Returns integer counters for telemetry:
        `{added, resurrected, refreshed, dropped, pending_total}`.
        """
        with flock_excl(self.path):
            current = self.load()
            by_id = {c.id: c for c in current}
            added = resurrected = refreshed = 0
            for cand in fresh:
                existing = by_id.get(cand.id)
                if existing is None:
                    by_id[cand.id] = cand
                    added += 1
                    continue
                if existing.status == "pending":
                    existing.summary_a = cand.summary_a
                    existing.summary_b = cand.summary_b
                    existing.similarity = cand.similarity
                    existing.detector = existing.detector or cand.detector
                    refreshed += 1
                elif existing.status == "dismissed" and self._member_moved_since(
                    existing, memories_by_id
                ):
                    existing.status = "pending"
                    existing.verdict_ts = None
                    existing.note = None
                    existing.summary_a = cand.summary_a
                    existing.summary_b = cand.summary_b
                    existing.similarity = cand.similarity
                    existing.created = cand.created
                    resurrected += 1
                # confirmed: terminal — the contradicts link is the artifact.
            kept = [
                c
                for c in by_id.values()
                if c.a_id in memories_by_id and c.b_id in memories_by_id
            ]
            dropped = len(by_id) - len(kept)
            self._write_all_locked(kept)
            return {
                "added": added,
                "resurrected": resurrected,
                "refreshed": refreshed,
                "dropped": dropped,
                "pending_total": sum(1 for c in kept if c.status == "pending"),
            }

    @staticmethod
    def _member_moved_since(
        cand: ConflictCandidate, memories_by_id: dict[str, Memory]
    ) -> bool:
        verdict = parse_event_ts(cand.verdict_ts)
        if verdict is None:
            return True  # unprovable verdict cannot stay sticky
        for mid in (cand.a_id, cand.b_id):
            m = memories_by_id.get(mid)
            if m is not None and m.updated > verdict:
                return True
        return False

    def resolve(
        self,
        candidate_id: str,
        *,
        status: str,
        note: str | None = None,
    ) -> ConflictCandidate | None:
        """Stamp a verdict on a PENDING candidate. Returns the updated
        row, or None when no pending row has that id. `verdict_ts` is
        stamped HERE (post-caller-side-effects by contract: the handler
        writes the `contradicts` link BEFORE resolving, so the link's
        `updated` bump lands before the verdict timestamp and the
        resurrect rule stays quiet)."""
        if status not in ("confirmed", "dismissed"):
            raise ValueError(
                f"verdict status must be 'confirmed' or 'dismissed', got {status!r}"
            )
        with flock_excl(self.path):
            current = self.load()
            hit: ConflictCandidate | None = None
            for c in current:
                if c.id == candidate_id and c.status == "pending":
                    c.status = status
                    c.verdict_ts = utcnow().isoformat()
                    c.note = note
                    hit = c
                    break
            if hit is not None:
                self._write_all_locked(current)
            return hit

    def _write_all_locked(self, rows: list[ConflictCandidate]) -> None:
        body = "".join(
            json.dumps(c.to_dict(), separators=(",", ":")) + "\n" for c in rows
        )
        atomic_write_bytes(self.path, body.encode("utf-8"), mode_before_rename=0o600)


def scan_conflicts(
    root: Path,
    memories: list[Memory],
    *,
    semantic_model: Any | None = None,
    threshold: float | None = None,
) -> dict[str, int]:
    """Detect over the (full) active set and merge into the queue.
    The one-call orchestration both producers use — the
    `memory_conflicts(scan=True)` handler and the consolidate pass."""
    fresh = find_conflict_candidates(
        memories, semantic_model=semantic_model, threshold=threshold
    )
    queue = ConflictQueue(root)
    return queue.upsert_scan(fresh, {m.id: m for m in memories})


def conflicts_pending_count(root: Path) -> int:
    """Cheap rollup for `curation_pending`. One small-file read; 0 when
    the queue has never been created."""
    try:
        return len(ConflictQueue(root).pending())
    except Exception:  # noqa: BLE001 — a corrupt queue must not break health
        log.warning("conflict queue unreadable under %s", root, exc_info=True)
        return 0


__all__ = [
    "CONFLICTS_FILENAME",
    "ConflictCandidate",
    "ConflictQueue",
    "conflicts_pending_count",
    "find_conflict_candidates",
    "scan_conflicts",
]
