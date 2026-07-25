"""Pydantic models, enums, and ULID generation for bettermemory."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

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
# `Store.load_all` and friends catch ValueError and skip the file (silently —
# the skip path emits no log line), so a user who downgrades bettermemory
# after writing some memories with a newer minor sees those memories disappear
# from the retrieval surface (rather than the reader silently misinterpreting
# them under the wrong field semantics). `bettermemory doctor`'s
# `memory_parse_health` check is what surfaces the count gap explicitly.
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


# Allowed characters for id-shaped tokens (session ids, swarm ids).
# Mirrors the filesystem-safe charset `episodes._session_dir` enforces
# for directory names. `swarm_id` reuses it because the value is
# normally a coordinator's `sess_<hex>` session id; ULIDs also pass.
_ID_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
# Cap the swarm_id length so a runaway value can't bloat the episode's
# YAML frontmatter (same 64 KB ceiling concern the episode_write body /
# takeaway / scope caps guard). 128 is generous for any session-id or
# ULID-shaped coordinator token.
_MAX_SWARM_ID_LEN = 128


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


class Category(str, Enum):
    """What kind of claim a memory makes.

    - ``"fact"``: project / infrastructure / reference / tooling facts
      about the world. Default. Counts toward dead-weight curation.
    - ``"user-inference"``: a claim about the user themselves
      (preferences, beliefs, working style). Routed through the
      pending-write flow so the user can confirm before commit.
    - ``"ambient"``: response-shaping context that informs every reply
      without being cited (user identity, persistent environment
      quirks). Excluded from the dead-weight rule because its value
      is implicit. Long-body warned at write time so they don't drift
      into catch-all dumps.

    Persisted on the memory record (additive frontmatter field —
    legacy memories without it load as `None` and are treated as
    ``"fact"`` for runtime semantics).
    """

    FACT = "fact"
    USER_INFERENCE = "user-inference"
    AMBIENT = "ambient"


# Closed-protocol whitelist: the `Category` values an automated /
# unattended path is allowed to set without explicit user
# confirmation. ``user-inference`` is deliberately excluded — that
# tier requires explicit user confirmation, supplied by
# ``memory_write``'s pending-confirm flow. Two production sites need
# the same set:
#
# - ``handlers.update.memory_update``'s ``category`` retag gate: a
#   retag *into* ``user-inference`` would silently bypass the
#   write-time pending-confirm gate (update has no equivalent), so
#   ``fact`` and ``ambient`` only.
# - ``llm._validate_demote`` and ``llm._validate_propose_new``: an
#   LLM running ``consolidate --llm`` can't supply the user
#   confirmation that the ``user-inference`` tier demands, so the
#   proposable set is the same ``{fact, ambient}``.
#
# Shared here (alongside the ``Category`` enum that defines the
# universe of values) so silent divergence between the two sites
# can't happen — and so adding a fourth ``Category`` member ships
# the decision about whether it's automation-eligible to one place.
# Enum-anchored (not bare string literals) so renaming a member is
# caught by mypy at the import site. Pinned by ``_EXPECTED_
# PROPOSABLE_CATEGORIES`` in ``tests/test_llm.py`` and
# ``tests/test_server.py``.
_PROPOSABLE_CATEGORIES: frozenset[str] = frozenset(
    {Category.FACT.value, Category.AMBIENT.value}
)


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


# Defensive cap on the per-record scope list — every list-shaped frontmatter
# field needs one. The risk is the same silent-data-loss path the takeaway cap
# (t16) closed: each scope serialises into YAML frontmatter, which
# `_frontmatter._MAX_YAML_BYTES` caps at 64 KB to neutralise alias-expansion
# DoS. Roughly 2200 short scope names (e.g. `"abc-def" * 2200`) would push the
# frontmatter past that ceiling — the loader would then raise `ValueError` on
# every subsequent read and the record would vanish from `memory_search` /
# `memory_list` / `episode_handoff` despite the write returning
# `status="committed"`. 64 matches the established ceiling on
# `verified_paths` / `verified_commits` / `verified_versions` / `links` (see
# `_cap_verified_list` / `_check_links` below) — well above any realistic
# per-record scope count (1-5 in practice) but low enough that an
# uncontrolled-append regression surfaces as an immediate write-time failure
# instead of slow on-disk bloat. The model-layer cap is defense-in-depth: the
# handler layer applies a configurable cap via `BehaviorConfig.max_scopes_per_write`,
# but a programmatic caller that bypasses the handler (sync, migration, future
# in-process API) still can't smuggle an unbounded list onto disk.
_MAX_SCOPES_PER_RECORD = 64


def _validate_scopes_list(scopes: list[str], *, allow_empty: bool = False) -> list[str]:
    """Per-scope validity + list-length cap.

    `allow_empty=True` is set by the Episode validator only — episodes are
    keyed by `session_id` not by scope, so a tagless episode is still
    routable via `episode_handoff`. Memory records (which require a non-
    empty list for retrieval) leave the flag at False.
    """
    if not scopes and not allow_empty:
        raise ValueError("scopes must contain at least one entry")
    if len(scopes) > _MAX_SCOPES_PER_RECORD:
        raise ValueError(
            f"scopes list capped at {_MAX_SCOPES_PER_RECORD} entries (got {len(scopes)})"
        )
    return [validate_scope(s) for s in scopes]


# ---------------------------------------------------------------------------
# Inter-memory link type
# ---------------------------------------------------------------------------


class LinkType(str, Enum):
    """How one memory relates to another (T2.2 of the 1.6 plan).

    Adopted from `mcp-memory-service`'s typed-edges idea but plumbed
    into retrieval so consumers can act on the relationship, not just
    inspect it.

    - ``"supersedes"``: this memory replaces the target. Retrieval-side
      consumers should prefer this memory and demote / suppress the
      target.
    - ``"contradicts"``: this memory contradicts the target. Both
      surface in retrieval; consumer should reconcile, typically by
      running memory_verify to attest which one matches reality.
    - ``"extends"``: this memory adds nuance / detail to the target.
      Both stay relevant; consumer might want to read both together.
    - ``"depends_on"``: this memory is meaningful only in the context
      of the target. A consumer retrieving this should consider
      pulling the target too.
    """

    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    EXTENDS = "extends"
    DEPENDS_ON = "depends_on"


class MemoryLink(BaseModel):
    """One typed edge from one memory to another. The source memory
    holds the link in its `links` field; the relationship is one-way
    on disk but exposed bidirectionally at retrieval (both the source
    memory and the target memory see the link, the target via a
    `reverse_links` field).

    `target_id` must be a valid ULID, and that is the ONLY check the
    write side makes: the runtime does not enforce that the target
    exists, then or ever. Nothing flags a broken link (target
    tombstoned or never written) afterwards either. The entry stays
    verbatim in the source's frontmatter and `memory_show` returns it
    in `links` unchanged — pinned by
    `test_broken_link_to_tombstoned_memory_still_surfaces` — while the
    retrieval-side resolvers that hydrate a target's summary
    (`_response.attach_depends_on_resolved`,
    `_response.attach_link_annotations`) drop a target they cannot
    load without saying so. A consumer that follows a link is the one
    that discovers it is broken; `memory_list_tombstones` finds a
    target that was removed rather than never written. Not blocking
    the write is the point of the arrangement: a consolidation pass
    that tombstones the source half of a pair can't accidentally
    orphan the surviving half.

    `note` is an optional free-form string capturing *why* the link
    exists. Important for `contradicts` and `supersedes` where the
    relationship's motivation matters to a future curator.
    """

    type: LinkType
    target_id: str
    note: str | None = None

    @field_validator("target_id")
    @classmethod
    def _check_target_id(cls, v: str) -> str:
        if not is_valid_ulid(v):
            raise ValueError(f"target_id must be a valid ULID, got {v!r}")
        return v


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

    `category` is the kind-of-claim axis (``fact`` / ``user-inference`` /
    ``ambient``). None on legacy memories written before the field shipped;
    runtime treats None as ``fact``. Persisted to frontmatter when set.

    `verified_paths` / `verified_commits` / `verified_versions` are the
    structured claims the caller attested when running `memory_verify`.
    They feed the staleness verdict's path-drift / commit-drift
    short-circuit: if a `path_drift` candidate appears in
    `verified_paths` and still exists, drift downgrades; if commits
    since `last_verified_at` didn't touch any of `verified_paths`, the
    commit-drift signal can stay clean. Empty by default; legacy
    memories load as empty lists.

    `verified_absent_paths` is the mirror attestation: body-cited paths
    the caller confirmed are *intentionally* absent on this machine
    (remote-host paths, platform-conditional locations, paths cited as
    NOT the real one). Path-drift excludes them from `missing` into an
    `expected_absent` bucket instead of raising a phantom drift signal.

    `corroborations` / `last_corroborated` are the recurrence rollup:
    bumped by `Store.record_corroboration` when a `memory_write` is
    dedup-rejected against this memory — the stored claim re-entered a
    conversation independently, which is evidence it still holds and
    still matters. A third timestamp axis, orthogonal to the other two:
    `updated` moves on content edits, `last_verified_at` on
    reality-checks, `last_corroborated` on recurrence. Feeds the
    freshest-touch curation window (a corroborated memory isn't dead
    weight) and, under `[behavior] corroboration_boost`, a bounded
    ranking nudge. Additive frontmatter fields — legacy memories load
    as 0 / None.
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
    category: Category | None = None
    verified_paths: list[str] = Field(default_factory=list)
    verified_commits: list[str] = Field(default_factory=list)
    verified_versions: list[str] = Field(default_factory=list)
    verified_absent_paths: list[str] = Field(default_factory=list)
    links: list[MemoryLink] = Field(default_factory=list)
    corroborations: int = Field(default=0, ge=0)
    last_corroborated: datetime | None = None

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

    @field_validator(
        "verified_paths",
        "verified_commits",
        "verified_versions",
        "verified_absent_paths",
    )
    @classmethod
    def _cap_verified_list(cls, v: list[str]) -> list[str]:
        # Defensive cap — a runaway frontmatter list shouldn't grow without
        # bound. 64 is well above any realistic per-memory claim count
        # (the largest in practice cite 5-10 paths) but low enough that
        # a regression that started appending uncontrollably surfaces as
        # an immediate write-time failure rather than a slow file bloat.
        if len(v) > 64:
            raise ValueError(
                f"verified-claims list capped at 64 entries (got {len(v)})"
            )
        return [str(item) for item in v]

    @field_validator("links")
    @classmethod
    def _check_links(cls, v: list[MemoryLink], info: Any) -> list[MemoryLink]:
        # Reject self-links: a memory shouldn't supersede or contradict
        # itself, that's incoherent and would foul up the retrieval-side
        # suppression logic. We can't easily access `id` from a field
        # validator unless we go through `info.data`, which is the
        # pydantic v2 idiom for cross-field validation.
        memory_id = info.data.get("id") if info.data else None
        for link in v:
            if memory_id is not None and link.target_id == memory_id:
                raise ValueError(
                    f"memory {memory_id} cannot link to itself (type={link.type.value})"
                )
        # Cap to keep frontmatter small and defensive. Same rationale as
        # verified-claims list — a hand-edited file or buggy migration
        # shouldn't grow this without bound.
        if len(v) > 64:
            raise ValueError(f"links list capped at 64 entries (got {len(v)})")
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

    `path_drift_checked_paths` / `path_drift_missing_paths` /
    `path_drift_verified_paths` are the actual path lists behind the
    counts. They let the consumer act on a non-fresh hit without a
    memory_show round-trip — when a hit comes back
    `spot_check_recommended` with `path_drift_missing_paths=["src/auth/middleware.py"]`,
    the caller can directly memory_update the rotted bit or
    memory_verify the rest. The counts above stay around for cheap
    triage; the lists are the actionable detail surfaced when the
    response builder folds them in.

    `path_drift_dropped_as_route_paths` mirrors the report's
    `dropped_as_route` bucket: candidates the route rule suppressed
    instead of stat-reporting — neither checked nor missing, see
    `PathDriftReport`. Threaded onto the hit so the response builder
    can fold a non-empty suppressed set into the per-hit `path_drift`
    dict, the same signal `memory_show` and the expanded top hit ship
    via the report's own serialisation. Before this field existed,
    non-top-ranked search hits were the one MCP surface that could not
    carry the bucket at all.

    `category` mirrors the persisted memory field; surfaced on every hit
    so triage can spot ambient context without expanding.

    `query_unique` is the coverage denominator the relevance label was
    computed against (distinct query tokens; the same value on every hit
    of one search, 0 on browse-mode hits). Together with
    `len(match_terms)` it makes the label REPLAYABLE — a logged hit can
    be re-labelled under a candidate formula without re-running the
    ranker. Telemetry-only: the response builder deliberately does not
    serialise it, so the shadow-label calibration never nudges live
    model behavior.
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
    path_drift_checked_paths: list[str] = []
    path_drift_missing_paths: list[str] = []
    path_drift_verified_paths: list[str] = []
    path_drift_expected_absent_paths: list[str] = []
    path_drift_dropped_as_route_paths: list[str] = []
    category: Category | None = None
    query_unique: int = 0


class MemorySummary(BaseModel):
    """One row from memory_list — body stripped, just a one-line summary."""

    id: str
    scopes: list[str]
    confidence: Confidence
    summary: str
    created: datetime
    updated: datetime
    last_verified_at: datetime | None = None
    category: Category | None = None


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
    category: Category | None = None
    verified_paths: list[str] = Field(default_factory=list)
    verified_commits: list[str] = Field(default_factory=list)
    verified_versions: list[str] = Field(default_factory=list)
    verified_absent_paths: list[str] = Field(default_factory=list)

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

    @field_validator(
        "verified_paths",
        "verified_commits",
        "verified_versions",
        "verified_absent_paths",
    )
    @classmethod
    def _cap_verified_list(cls, v: list[str]) -> list[str]:
        if len(v) > 64:
            raise ValueError(
                f"verified-claims list capped at 64 entries (got {len(v)})"
            )
        return [str(item) for item in v]


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
    category: Category | None = None
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
# Episodes — sibling tier to Memory for run-state / journal-shaped entries
# ---------------------------------------------------------------------------
#
# Episodes are NOT durable facts. They exist for journal-shaped writes the
# durability gate (durability.TRANSIENT_PHRASE_MARKERS) explicitly rejects on
# memory_write: loop-iteration state, "what we tried", run-local takeaways.
# Stored at `<root>/episodes/<session_id>/<ulid>.md`, TTL-pruned to keep the
# directory bounded (default 30 days). Excluded from memory_search,
# memory_health, memory_list — episodes are a sibling primitive, not a tier
# of memory. A useful episodic takeaway can be *promoted* into a durable
# memory via `episode_promote`, which routes through `memory_write` so the
# durability gate fires as normal.


class Episode(BaseModel):
    """A journal-shaped entry written by a session for its own or a
    future session's reference.

    Fields:
    - `id`: ULID (lexicographic time order at ms resolution).
    - `session_id`: the writer's session id at write time. Defines the
      grouping for `episode_handoff` (last takeaways from prior session
      in this worktree) and the storage subdirectory.
    - `created`: write time (UTC). No `updated` field — episodes are
      append-only; if a takeaway changes, write a new episode.
    - `body`: the full journal entry. Free-form markdown.
    - `takeaway`: optional one-sentence summary surfaced at handoff.
      When None, handoff falls back to the first line of `body`.
    - `scopes`: free-form tags. Auto-defaulted from origin (e.g.
      `projects:<name>`) at the handler layer when not provided, the
      same way memory scopes are.
    - `origin`: cwd / repo / branch / worktree_root snapshot at write
      time. Drives the worktree filter on `episode_handoff` so only
      episodes from the same workspace are surfaced.
    - `is_floor`: True for a session-tag floor episode written at
      `episode_handoff` entry to mark the current session on disk
      BEFORE the handoff event is recorded. Floors carry no journal
      content (empty body, empty takeaway, empty scopes) — they exist
      purely so a tick that crashes between `episode_handoff` and
      `episode_write` still leaves a worktree-tagged journal anchor
      for the next tick's handoff to walk. Consumers that summarise
      takeaways for the model (e.g. `episode_handoff`'s emit step)
      filter out `is_floor=True` so the floor never shows up as
      "what the prior session concluded". Defaults False; legacy
      episodes written before the field shipped load as False.
    - `swarm_id`: optional cohort link for multi-agent swarm fan-in.
      When a coordinator fans out N parallel sub-agents, each sub-agent
      stamps its episodes with the coordinator's id here; the
      coordinator then gathers every sub-agent's takeaways across their
      individual session directories via `EpisodeStore.list_by_swarm`
      / `episode_search(swarm_id=…)`. Distinct from the single-chain
      `episode_handoff` predecessor link (1:1, temporal "my previous
      self") — a swarm is an N:1 cohort. Optional/additive like
      `is_floor`: legacy episodes load as None and the writer only
      emits the frontmatter key when set, so the on-disk shape is
      unchanged when absent (no SCHEMA_VERSION bump needed).
    """

    id: str
    session_id: str
    created: datetime
    body: str
    # Free-form scope tags. Empty is allowed — episodes are keyed by
    # session_id, not scope, so a tagless episode is still routable
    # via `episode_handoff`. When populated, each scope must pass the
    # same validator memory scopes use so the on-disk shape stays
    # consistent across the two stores.
    scopes: list[str] = Field(default_factory=list)
    takeaway: str | None = None
    origin: Origin | None = None
    # Floor marker — see class docstring. Defaults False so non-floor
    # episodes serialise identically to the pre-field shape; the
    # episode writer only emits the frontmatter key when True (the
    # opt-in path mirrors how `origin` / `takeaway` / `scopes` already
    # omit when empty/None).
    is_floor: bool = False
    # Swarm/cohort grouping for multi-agent fan-in — see class docstring.
    # Defaults None (the non-swarm common case); the writer only emits
    # the frontmatter key when set, so non-swarm episodes serialise
    # identically to the pre-field shape.
    swarm_id: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not is_valid_ulid(v):
            raise ValueError(f"invalid ULID: {v!r}")
        return v

    @field_validator("swarm_id")
    @classmethod
    def _check_swarm_id(cls, v: str | None) -> str | None:
        # None is the common case (a non-swarm episode). When set, the
        # value is the coordinator's id (typically a `sess_<hex>`
        # session id) and is only ever matched for equality at fan-in
        # time — never used as a path component. Still constrain it to
        # the filesystem-safe id charset and cap the length so a runaway
        # value can't bloat the YAML frontmatter (the same ceiling the
        # episode_write body / takeaway / scope caps guard).
        if v is None:
            return None
        if (
            not v
            or len(v) > _MAX_SWARM_ID_LEN
            or any(c not in _ID_TOKEN_CHARS for c in v)
        ):
            raise ValueError(
                f"invalid swarm_id {v!r}: must be 1-{_MAX_SWARM_ID_LEN} "
                "characters of [A-Za-z0-9_-]"
            )
        return v

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, v: list[str]) -> list[str]:
        # Episodes allow an empty scope list (handoff keys on session_id, not
        # scope) — route through the shared validator with `allow_empty=True`
        # so the 64-entry cap is enforced uniformly with Memory/TombstonedMemory
        # while the empty-list semantics for the episode tier are preserved.
        return _validate_scopes_list(v, allow_empty=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware UTC `now`. Centralised so tests can monkey-patch."""
    return datetime.now(timezone.utc)


_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Leading ISO-8601 date (with optional time) at the start of the first line.
# `build_filename` already prepends the memory's `created` date, so a body
# beginning with its own date would otherwise produce a doubled prefix
# (`2026-05-07-2026-05-07-tightened-the-mvp.md`).
_LEADING_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ tT]\d{2}:\d{2}(?::\d{2})?(?:[zZ]|[+\-]\d{2}:?\d{2})?)?"
    r"[\s\-_:.,;|/]*"
)


def make_slug(content: str, max_words: int = 6, max_chars: int = 60) -> str:
    """Build a filename-friendly slug from the first words of `content`."""
    text = content.strip().lower()
    # Take first line — multi-line bodies often have a leading title.
    text = text.splitlines()[0] if text else ""
    # Strip a leading ISO date so we don't double up with `build_filename`'s
    # date prefix. If the body is *only* a date, fall through to the
    # `["memory"]` fallback below.
    text = _LEADING_DATE_RE.sub("", text)
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
    # Whitespace boundary, not just ASCII space: `snippet_for` does NOT
    # normalize newlines (a markdown list of paths/URLs keeps its line
    # structure), so a body with one token per line has no space near the
    # window edge — the newline IS the word boundary there. Backing off
    # only to `rfind(" ")` hard-cut those bodies mid-path, leaving the
    # snippet ending in a plausible-but-wrong path.
    space_idx = max(truncated.rfind(" "), truncated.rfind("\n"), truncated.rfind("\t"))
    if space_idx >= max_chars - 40:
        truncated = truncated[:space_idx]
    return truncated.rstrip(" ,;:.-") + "..."


# Re-export so callers don't import from os.
__all__ = [
    "Category",
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
