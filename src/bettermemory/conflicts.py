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
- ``dismissed``: the model judged the pair compatible. Any standing
  `contradicts` link between the pair is cleared by the handler first
  (same before-the-stamp ordering as the confirm side), so the queue
  and the link layer cannot end up disagreeing about the same pair.
  The verdict also records a fingerprint of each member's BODY as it
  stood when it was judged (`verdict_hash_a` / `verdict_hash_b`).
  Sticky across scans — UNLESS a member's body stops hashing to what
  the verdict judged, in which case that content no longer exists and
  the pair resurrects as pending.

  Keying the resurrect rule on content rather than on `updated` is what
  keeps a dismissal from reappearing for reasons that have nothing to
  do with its own pair. Both arbitration paths REWRITE memories — the
  confirm path adds a `contradicts` link, the dismiss path strips one —
  and each rewrite bumps `updated` on a memory that may well sit in
  other queued pairs too (near-identical bodies cluster, so one memory
  routinely has several partners). Under an `updated`-keyed rule,
  arbitrating one pair silently re-queued every dismissed pair sharing
  a member with it. A body hash cannot mistake a link edit for a claim
  edit. Rows dismissed before the hashes existed carry none and fall
  back to the old `updated > verdict_ts` rule until they are dismissed
  again.

Rows whose members stop being active (tombstoned, merged) are dropped
on the next full-corpus upsert — a conflict with a dead side is moot.
`upsert_scan` is the ONLY garbage collector, which is why every
applying consolidate pass calls it unconditionally: a pass that only
upserted when it had fresh candidates would strand those rows in the
file indefinitely. Until a scan collects one, no reporting surface
advertises it — `split_judgeable` is the shared filter both the
`memory_conflicts` listing and `memory_scope_overview`'s
`curation_pending.conflicts` count run their rows through — but each of
them re-pays a liveness check on it every time they report.

Collecting needs the caller's snapshot to be COMPLETE, not merely
full-corpus. `Store.load_all` skips any file it cannot parse
(`PARSE_SKIP_EXCEPTIONS` is `(Exception,)` — a truncated write, a bad
`chmod`, a mid-tombstone race), so "absent from the snapshot" is on its
own evidence of a bad read and not of a death, and GC is permanent: a
dropped row takes its status, `verdict_ts`, `note` and body hashes with
it, and re-detection can only ever re-file the pair as `pending`. So
`upsert_scan` compares the snapshot against the number of active `.md`
files under the root and, when it holds fewer, merges and refreshes as
usual but collects NOTHING and reports `gc_deferred`. See
`_snapshot_is_complete` for what else can trip that comparison.

On-disk: ``<root>/.conflicts.jsonl`` — one JSON object per line,
0o600, atomically rewritten under a per-file ``flock`` (the
`memory_conflicts` handler and the auto-consolidate scan can race
across processes; same discipline as `proposals.ProposalQueue`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._fsutil import atomic_write_bytes, flock_excl
from .models import Memory, utcnow
from .store import count_active_memory_files
from .time_utils import parse_event_ts

log = logging.getLogger("bettermemory.conflicts")

CONFLICTS_FILENAME = ".conflicts.jsonl"

_VALID_STATUSES = ("pending", "confirmed", "dismissed")


def _body_hash(body: str) -> str:
    """Fingerprint of one member's body, as a verdict judged it.

    The resurrect rule's key. Deliberately over the RAW body with no
    normalisation: the question is "is this still the text the model
    ruled on", and a rewrite that only moved whitespace is still a
    rewrite the verdict never saw. Erring toward re-arbitration matches
    the direction the old `updated`-keyed rule erred in, so nothing that
    used to re-queue silently stops.

    Truncated to 64 bits — this compares a body against its own earlier
    self, not against an attacker-chosen one, and the queue row it lives
    on is already inside the store's 0o600 trust boundary."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


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
    versions, dates).

    `verdict_hash_a` / `verdict_hash_b` are `_body_hash` of each member
    as the verdict judged it — the resurrect rule's key on a dismissed
    row (see the module docstring). `None` on a pending row, and `None`
    on a row whose verdict predates the field or whose member was
    already gone at verdict time, in which case that side falls back to
    the `updated > verdict_ts` rule."""

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
    verdict_hash_a: str | None = None
    verdict_hash_b: str | None = None

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
        if self.verdict_hash_a is not None:
            out["verdict_hash_a"] = self.verdict_hash_a
        if self.verdict_hash_b is not None:
            out["verdict_hash_b"] = self.verdict_hash_b
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
            verdict_hash_a=(
                str(raw["verdict_hash_a"])
                if raw.get("verdict_hash_a") is not None
                else None
            ),
            verdict_hash_b=(
                str(raw["verdict_hash_b"])
                if raw.get("verdict_hash_b") is not None
                else None
            ),
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
        → resurrect to pending ONLY when a member's body no longer
        matches the fingerprint the verdict recorded (see
        `_judged_content_changed`); confirmed → terminal, left alone.

        Collection is skipped wholesale when `memories_by_id` looks
        incomplete against the store root — see `_snapshot_is_complete`
        and the module docstring. Returns integer counters for
        telemetry: `{added, resurrected, refreshed, dropped, gc_deferred,
        pending_total}`, where `gc_deferred` is 1 on exactly the pass
        that declined to collect.
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
                elif existing.status == "dismissed" and self._judged_content_changed(
                    existing, memories_by_id
                ):
                    existing.status = "pending"
                    existing.verdict_ts = None
                    existing.note = None
                    existing.verdict_hash_a = None
                    existing.verdict_hash_b = None
                    existing.summary_a = cand.summary_a
                    existing.summary_b = cand.summary_b
                    existing.similarity = cand.similarity
                    existing.created = cand.created
                    resurrected += 1
                # confirmed: terminal — the contradicts link is the artifact.
            collectable = self._snapshot_is_complete(memories_by_id)
            kept = [
                c
                for c in by_id.values()
                if not collectable
                or (c.a_id in memories_by_id and c.b_id in memories_by_id)
            ]
            dropped = len(by_id) - len(kept)
            self._write_all_locked(kept)
            return {
                "added": added,
                "resurrected": resurrected,
                "refreshed": refreshed,
                "dropped": dropped,
                "gc_deferred": 0 if collectable else 1,
                "pending_total": sum(1 for c in kept if c.status == "pending"),
            }

    def _snapshot_is_complete(self, memories_by_id: dict[str, Memory]) -> bool:
        """Is the caller's snapshot fit to rule that a member DIED?

        True when it holds at least as many memories as the store root
        holds active `.md` files. GC is permanent and irreversible — the
        dropped row carries away a settled verdict's status, timestamp,
        note and body hashes — so it may only run on evidence that
        distinguishes "the file is gone" from "the file did not parse".
        `Store.load_all` cannot: it skips per-file failures on
        `PARSE_SKIP_EXCEPTIONS`, which is `(Exception,)`, so one
        truncated write or one bad `chmod` used to be enough to erase an
        arbitration decision the model can never re-derive.

        A bare file count (`store.count_active_memory_files` — the same
        regular-file/non-symlink/`.md` filter `Store._iter_active_paths`
        walks, borrowed rather than re-spelt so the two cannot drift)
        answers that without re-parsing anything: a load that skipped a
        file necessarily returns fewer memories than there are files.

        The comparison is `>=`, not `==`, and it is deliberately
        one-sided — it detects an under-count, never an over-count:

        - A memory WRITTEN between the caller's load and this call makes
          the count exceed the snapshot. Benign: GC waits for the next
          pass, and every applying curation pass runs one.
        - A memory TOMBSTONED in that window leaves the snapshot larger
          than the count. Also benign — the stale member is still in the
          map, so its rows are kept, and the next pass collects them.
        - Two active files carrying the SAME id collapse to one entry in
          the caller's dict, and a stray non-memory `.md` dropped into
          the root parses as nothing. Both read as an under-count and
          defer GC until they are cleaned up; both are corruption the
          store already flags loudly (the S4 divergence warning at
          construction, `doctor`'s `index_health`). Dead rows lingering
          in the queue file cost nothing model-visible — every reporting
          surface filters them through `split_judgeable` — so deferring
          is the cheap side of this trade in a way that dropping a
          verdict is not.

        An unlistable root is treated as incomplete for the same reason:
        no evidence, no collection.
        """
        try:
            on_disk = count_active_memory_files(self.root)
        except OSError:
            log.warning(
                "conflict-queue GC deferred: cannot list %s", self.root, exc_info=True
            )
            return False
        if len(memories_by_id) < on_disk:
            log.warning(
                "conflict-queue GC deferred: caller's snapshot has %d memories but "
                "%s holds %d active .md files — an unreadable file must not look "
                "like a dead conflict member",
                len(memories_by_id),
                self.root,
                on_disk,
            )
            return False
        return True

    @staticmethod
    def _judged_content_changed(
        cand: ConflictCandidate, memories_by_id: dict[str, Memory]
    ) -> bool:
        """Has either member's body stopped being the text the verdict
        ruled on? The resurrect predicate for a dismissed row.

        Per side, keyed on the `_body_hash` the verdict recorded — NOT on
        `updated`. The arbitration surface itself rewrites memories (the
        confirm path adds a `contradicts` link, the dismiss path strips
        one), so `updated` moves for reasons that are not claim edits and
        that belong to a DIFFERENT pair: any dismissed pair sharing a
        member with the pair just arbitrated used to resurrect on the
        next scan. A body hash is blind to link edits, which is the whole
        point.

        A side whose row carries no hash (dismissed before the field
        existed, or its member was already gone at verdict time) falls
        back to the old `updated > verdict_ts` rule — including its
        "unprovable verdict cannot stay sticky" escape when
        `verdict_ts` will not parse. Such a row converges: resurrecting
        it once re-queues it, and the next dismissal records hashes.

        A member absent from the snapshot is skipped rather than treated
        as changed. Callers only reach this for a pair in `fresh`, whose
        members came from the very list `memories_by_id` was built from,
        so absence here means a caller broke the full-corpus contract —
        and guessing "changed" would flip a settled verdict on no
        evidence at all.
        """
        verdict = parse_event_ts(cand.verdict_ts)
        for mid, judged in (
            (cand.a_id, cand.verdict_hash_a),
            (cand.b_id, cand.verdict_hash_b),
        ):
            m = memories_by_id.get(mid)
            if m is None:
                continue
            if judged is not None:
                if _body_hash(m.body) != judged:
                    return True
            elif verdict is None or m.updated > verdict:
                return True
        return False

    def resolve(
        self,
        candidate_id: str,
        *,
        status: str,
        note: str | None = None,
        member_bodies: dict[str, str] | None = None,
    ) -> ConflictCandidate | None:
        """Stamp a verdict on a PENDING candidate. Returns the updated
        row, or None when no pending row has that id.

        `member_bodies` maps memory id → body text as the model judged
        it; the two sides of this pair are fingerprinted onto the row and
        become what the resurrect rule compares against. A caller that
        omits it (or omits one side — a member already gone leaves no
        body to hash) leaves that side hashless and on the legacy
        `updated > verdict_ts` fallback, so pass it whenever the bodies
        are in hand.

        `verdict_ts` is stamped HERE, after the caller's side effects by
        contract: the handler writes or clears the `contradicts` link
        BEFORE resolving. With hashes recorded that ordering no longer
        carries the resurrect rule — a link edit does not touch the body
        — but it still keeps the fallback correct for rows that predate
        them."""
        if status not in ("confirmed", "dismissed"):
            raise ValueError(
                f"verdict status must be 'confirmed' or 'dismissed', got {status!r}"
            )
        bodies = member_bodies or {}
        with flock_excl(self.path):
            current = self.load()
            hit: ConflictCandidate | None = None
            for c in current:
                if c.id == candidate_id and c.status == "pending":
                    c.status = status
                    c.verdict_ts = utcnow().isoformat()
                    c.note = note
                    body_a, body_b = bodies.get(c.a_id), bodies.get(c.b_id)
                    c.verdict_hash_a = None if body_a is None else _body_hash(body_a)
                    c.verdict_hash_b = None if body_b is None else _body_hash(body_b)
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


def split_judgeable(
    pending: Iterable[ConflictCandidate],
    is_active: Callable[[str], bool],
) -> tuple[list[ConflictCandidate], int]:
    """Split pending rows into the judgeable ones and a count of the rest.

    The single definition of "this queued row is real arbitration
    work": both members still active. A row failing it cannot be ruled
    on at all — `_load_active_member` refuses the verdict and the
    remedy is a scan, not a judgment — so a surface that counts it
    advertises work that resolves to nothing, and a cue that keeps
    resolving to nothing teaches the model to stop following it.

    Every surface that reports a pending count runs its rows through
    here: `memory_conflicts`'s `pending_total` (and the list beside it)
    and `memory_scope_overview`'s `curation_pending.conflicts`. They
    hold different liveness authorities — per-row `store.load_one` vs.
    membership in the full-corpus `load_all` snapshot the overview
    already paid for — which is exactly why the *predicate* is shared
    rather than each surface re-deriving it: the two counts point at
    each other, and the whole point of the fix is that they agree.
    (The two authorities coincide by construction: `load_one` walks the
    same active set `load_all` materialises and refuses anything
    `load_all` skips.)

    `is_active` is called left-to-right and short-circuits, so an
    authority that pays real I/O per member does not price the second
    side of an already-dead pair. Input order is preserved (callers
    sort by similarity before windowing), and the whole iterable is
    consumed — a caller that renders only a window still gets a total
    over everything.

    Report-only: nothing is GC'd here. `upsert_scan` stays the queue's
    only collector, ruling on liveness from one full-corpus snapshot
    under the file lock.
    """
    judgeable: list[ConflictCandidate] = []
    omitted = 0
    for cand in pending:
        if is_active(cand.a_id) and is_active(cand.b_id):
            judgeable.append(cand)
        else:
            omitted += 1
    return judgeable, omitted


def conflicts_pending_count(root: Path) -> int:
    """Raw count of rows sitting in `pending` status on disk. One
    small-file read; 0 when the queue has never been created.

    Deliberately NOT what the model-facing surfaces report: a row whose
    member has since been removed is still `pending` in the file but is
    not judgeable work, so both `memory_conflicts`'s `pending_total`
    and `memory_scope_overview`'s `curation_pending.conflicts` filter it
    out through `split_judgeable`. This counts the file — the probe for
    "has a scan collected that row yet?", not for "is there arbitration
    work?".
    """
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
    "split_judgeable",
]
