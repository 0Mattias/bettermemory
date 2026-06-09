"""Config loading and the rules for picking a memory directory.

Resolution order for the storage directory:

1. The `BETTERMEMORY_DIR` env var, if set.
2. `./.claude-memory/` if it exists in the current working directory
   (project-scoped — write a memory while in that project, see it only
   when you come back to that project).
3. `~/.claude-memory/` (global fallback).

A user-level config file lives at `~/.config/bettermemory/config.toml` (or the
platform equivalent). It's created with defaults on first run.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

from ._fsutil import atomic_write_bytes


CONFIG_FILENAME = "config.toml"
PROJECT_DIR_NAME = ".claude-memory"
GLOBAL_DIR_NAME = ".claude-memory"
ENV_DIR_OVERRIDE = "BETTERMEMORY_DIR"

DEFAULT_CONFIG = """\
# bettermemory config
#
# See: https://github.com/0Mattias/bettermemory

[storage]
# Where memories live. Leave commented to use the default resolution rule:
#   1. $BETTERMEMORY_DIR
#   2. ./.claude-memory if cwd has one
#   3. ~/.claude-memory
# directory = "~/.claude-memory"

[behavior]
# If true, memory_write returns a "pending" result and the consumer must
# call memory_write_confirm to commit. MVP defaults to false for solo use.
require_write_confirmation = false

# Default cap on memory_search results.
default_max_results = 5

# Recency boost decay. Larger = older memories get a meaningful bump.
recency_boost_half_life_days = 30

# Retrieval ranker for memory_search. One of:
#   "keyword" — the original TF + coverage + recency scorer (legacy default
#       in 1.6.0). No IDF weighting, so rare-term queries underperform.
#   "bm25"    — Okapi BM25 with the same scope-bonus + recency boost
#   "semantic" — sentence-transformers cosine; requires the embeddings
#       extra. Falls back to "keyword" with a WARNING log if the extra
#       isn't installed.
#   "hybrid"  — reciprocal-rank-fusion of keyword + BM25 (plus semantic
#       when the embeddings extra is installed). Strict improvement over
#       any single mode; gracefully degrades to keyword + BM25 fusion when
#       no embedding extra is present. Default since 2.6.8.
# The MCP `mode` parameter on memory_search overrides this per-call.
search_mode = "hybrid"

# Usage-aware ranking. When true, a bounded "endorsement" factor (the same
# shape as the recency boost — capped at +10%, so it only breaks near-ties,
# never overrides relevance) nudges memories the model has DELIBERATELY
# applied (an explicit memory_record_use(applied), not the ~2-turn auto-
# fallback) up the results — so a fact that keeps proving load-bearing wins
# a tie over a never-endorsed peer. Off by default: it reorders results and
# costs one event-log read per search. Counts are recent (active-log window),
# so the signal tracks current usefulness rather than lifetime popularity.
endorsement_boost = false

# When true, memory_write dedup uses cosine similarity on sentence
# embeddings instead of Jaccard on token sets — catches paraphrases
# like "the database" / "Postgres" that lexical overlap misses.
# Requires one of the embedding extras (see `semantic_provider`). Falls
# back to Jaccard with a WARNING log line when no extra is installed,
# so flipping the bit without the deps is safe.
semantic_dedup = false

# Which embedding provider to use. One of:
#   "auto"      — pick torch when [embeddings] is installed, else
#                 fastembed when [embeddings-fast] is installed, else
#                 fall back to Jaccard. Default.
#   "torch"     — sentence-transformers + PyTorch. Heavier install
#                 (~500MB) but the well-trodden path.
#   "fastembed" — fastembed + ONNX Runtime. ~50MB total. Same retrieval
#                 surface, smaller footprint. Wheels lag the newest
#                 Python by a release; use the torch extra on 3.14.
# An explicit value is honoured even when the extra isn't installed —
# you'll see a per-provider WARNING and the Jaccard fallback. Auto +
# both installed prefers torch so existing `.embeddings.<model>.npz`
# caches stay byte-stable.
semantic_provider = "auto"

# Embedding model for the torch provider. `all-MiniLM-L6-v2` is the
# small default (~80MB); swap for a larger sentence-transformers
# model if you need better paraphrase detection and have CPU/RAM
# headroom. Read only when `semantic_provider` resolves to "torch".
semantic_model_name = "all-MiniLM-L6-v2"

# Embedding model for the fastembed provider. `BAAI/bge-small-en-v1.5`
# is the 384-dim small default (~33MB ONNX); see the fastembed model
# catalogue for alternatives. Read only when `semantic_provider`
# resolves to "fastembed".
semantic_model_fastembed = "BAAI/bge-small-en-v1.5"

# Cosine thresholds for the semantic path. Cosine on normalized
# embeddings tends to land 0.5-0.9 for semantically similar sentences,
# 0.1-0.3 for unrelated, so the cutoffs sit higher than the Jaccard
# defaults (0.75 / 0.40).
semantic_high_threshold = 0.85
semantic_medium_threshold = 0.65

# Floor on `applied_count` for inclusion in `memory_health.heavily_used`.
# Default 3 — at 1 the bucket is dominated by one-off acknowledgements
# rather than repeat-use patterns. Lower it on a fresh store; raise it
# once the event log has weeks of data.
heavily_used_min_applied = 3

# Cold-endorsement ratio threshold (0.0-1.0). When > 0, the
# `cold_endorsement_memories` bucket in `memory_health` ALSO flags
# memories whose explicit-applied / total-applied ratio falls below
# this fraction — catching the "1 explicit endorsement out of 50
# auto" case the strict "explicit == 0" check misses. Default 0.0
# keeps the strict behaviour (only zero-explicit memories surface).
# Set to 0.1 to additionally surface memories where less than 10% of
# applies are explicit endorsements.
cold_endorsement_ratio_threshold = 0.0

# Days after `last_verified_at` past which a memory's verification is
# considered "stale" — the retrieval surface attaches a re-spot-check
# recommendation to the response. 30 days mirrors the recency-boost
# half-life: memories the ranker no longer treats as fresh for ordering
# also stop counting as fresh for verification. Set 0 to mark every
# verified memory stale immediately (useful in tests, rarely in practice).
verification_stale_days = 30

# Default retention for `bettermemory tombstones prune` (days). Tombstones
# are never auto-pruned; this is just the default for the CLI subcommand,
# which still requires an explicit invocation. 0 means "no default" — the
# CLI requires --older-than to be passed. Set this to e.g. 365 if you want
# `bettermemory tombstones prune` with no flag to default to one-year
# retention. Active memories are unaffected.
tombstone_retention_days = 0

# Hard cap on a single memory body's UTF-8 byte length at `memory_write`
# / `memory_update` time. Existing memories on disk are never re-validated
# — this is a write-time bound that protects against a runaway model or a
# hostile client filling disk with a multi-gigabyte body. The default of
# 1 MB is ~1000x a typical memory (which sits at 1–2 KB); raise it if you
# legitimately curate very long context dumps as single memories, lower
# it for stricter resource boundaries. Set to 0 to disable the cap.
max_content_bytes = 1000000

# Hard cap on a single episode takeaway's UTF-8 byte length at
# `episode_write` time. Separate from `max_content_bytes` because the
# takeaway lives in the YAML frontmatter region, which is itself capped
# at 64 KB (see `_frontmatter._MAX_YAML_BYTES`) — a takeaway over that
# threshold would corrupt the frontmatter, the loader would raise
# ValueError on every subsequent read, and `EpisodeStore.list_by_session`
# would silently skip the file. The episode would look committed (the
# write returned status="committed") but vanish from every read surface.
# 4 KB is generous for the documented "one-sentence summary" while
# leaving comfortable headroom inside the 64 KB YAML cap for the rest
# of the frontmatter (id, session_id, created, scopes, origin). Set to
# 0 to disable the cap.
max_takeaway_bytes = 4096

# Hard cap on the number of scopes accepted by a single memory_write,
# memory_update, or episode_write call. Defense-in-depth alongside the
# model-layer cap (also 64) — every list-shaped frontmatter field needs one.
# Roughly 2200 short scope names serialise to ~64 KB of YAML and push the
# frontmatter past `_frontmatter._MAX_YAML_BYTES`, after which the loader
# raises `ValueError` on every subsequent read and the record vanishes from
# every read surface despite the write returning status="committed". 64
# matches the verified_paths cap and is well above any realistic per-record
# scope count (1-5 in practice). Set to 0 to disable the handler-boundary
# cap (the model-layer cap still fires at 64).
max_scopes_per_write = 64

# Passive in-conversation curation surface. When the sum of dead_weight
# + drifted + cold_endorsement_memories counts (the `curation_pending`
# rollup you'd otherwise have to call `memory_scope_overview` to see)
# crosses this threshold, the FIRST successful `memory_write` of each
# session inlines a one-line `curation_hint` block on its response so
# a model that never asks for the overview still gets the nudge.
# One-shot per session — subsequent writes stay quiet. Pull-based
# discovery via `memory_health` / `memory_scope_overview` remains the
# primary path; this is a non-detour notification. Set to 0 to disable
# numerically, or set `curation_hint_enabled = false` to disable
# structurally.
curation_hint_threshold = 5
curation_hint_enabled = true

# Tool-surface breadth. Lean by default: the seven curation / power-user
# tools (memory_health, memory_curate, memory_acknowledge_miss,
# memory_rename_scope, memory_restore, memory_list_tombstones,
# memory_proposals) are NOT registered on the MCP server, keeping the
# per-turn tool-description context lean for the common case. They stay
# reachable via the `bettermemory` CLI (memory_curate wraps `consolidate`).
# Set true for the full surface — the curate-loop skill needs it.
# (memory_proposals also surfaces when [proposals] auto_propose is on.)
full_tool_surface = false

[scopes]
# If non-empty, writes with scopes outside this list fail. Empty = anything.
allowed = []

[telemetry]
# Append-only JSONL event log at <storage>/.events.jsonl. One line per tool
# call: search queries, returned IDs, write/update/remove events. Used by the
# memory_health view, by use-recording feedback, and to tune the durability
# marker list against real traffic. Lives next to the memories — same trust
# boundary, no new permissions story. Set `enabled = false` to opt out.
enabled = true

# Rotate (gzip) the active log when it crosses this many bytes. Archives are
# kept indefinitely — prune by hand if disk pressure matters.
max_bytes = 10000000

# Search query privacy. When false (the default since 2.6.8), `memory_search`
# `query` and `memory_audit_turn` `probe_query` fields are redacted to
# `{"hash": "<sha256-prefix>", "preview": "<first 32 chars>", "len": N}`
# before landing in the event log. Correlation across events still works (a
# repeated query has the same hash) and the first ~32 characters survive for
# triage, but a secret pasted into a query no longer lives on disk verbatim.
# Set true to restore the legacy verbatim shape — useful for debugging your
# own ranker, less so for shared boxes.
log_queries_verbatim = false

[consolidate]
# Opt-in unattended consolidation — the self-improving loop. OFF by
# default. When enabled (and [telemetry] is on — the event log is both
# the debounce clock and the audit trail), the Stop hook runs the
# STRUCTURALLY-SAFE consolidation subset at turn end: conservative
# near-duplicate dedup (a reversible tombstone) and demote-never-applied
# (a non-destructive fact->ambient retag). No LLM passes, no contradiction
# resolution — nothing that needs judgement. Every action lands as a
# reviewable, reversible tombstone/event (memory_list_tombstones + the
# event log) — the deliberate opposite of invisible "Dreaming"
# consolidation. Turn this on to let the store quietly improve itself.
auto_apply = false

# Minimum hours between unattended runs. The Stop hook fires every turn;
# this debounces so the O(N^2) dedup runs at most once per window.
auto_apply_interval_hours = 24.0

# Skip the unattended pass when the active set exceeds this many memories
# — the pairwise dedup is O(N^2) and the turn-end hook must stay
# responsive. Larger stores should run `bettermemory consolidate --apply`
# by hand (or raise this once you've measured the cost on your store).
auto_apply_max_memories = 500

[proposals]
# Opt-in write-reflex closure — the capture half of the self-improving
# loop. OFF by default. When enabled, the Stop hook scans each turn's
# USER message for durable-looking statements you made but the model
# didn't save (explicit "remember…" requests, first-person
# preferences/setup facts) and queues them as INERT proposals. Nothing
# is ever written to memory automatically: review the queue with the
# `memory_proposals` tool and accept (a normal memory write) or dismiss.
# Closes the gap where durable content slips by during head-down work
# without breaking the "writes are confirmed, never silent" contract.
auto_propose = false

# Cap on the pending-proposal queue. Once it holds this many, extraction
# stops until you accept or dismiss some — bounds growth and avoids
# nagging.
max_pending = 20
"""


@dataclass
class StorageConfig:
    directory: str | None = None  # if None, use resolution rule.


@dataclass
class BehaviorConfig:
    require_write_confirmation: bool = False
    default_max_results: int = 5
    recency_boost_half_life_days: float = 30.0
    # Retrieval ranker for `memory_search`. One of `keyword` (the
    # original TF + coverage + recency scorer; legacy), `bm25` (Okapi
    # BM25), `semantic` (sentence-transformers cosine; requires the
    # embeddings extra), or `hybrid` (RRF fusion of keyword + BM25,
    # plus semantic when the extra is installed). The MCP `mode`
    # parameter on memory_search overrides this per-call. Default
    # is `hybrid` since 2.6.8 — the keyword scorer lacks IDF weighting
    # so rare-term queries underperform; hybrid is a strict improvement
    # and degrades gracefully to keyword+BM25 when no embedding extra
    # is installed.
    search_mode: str = "hybrid"
    # Usage-aware ranking. When true, a bounded endorsement factor (mirrors
    # the recency boost, capped at +10%) nudges memories the model has
    # EXPLICITLY applied up the results, so a load-bearing fact wins a
    # near-tie. Opt-in (default off): it reorders results and costs one
    # event-log read per search; see DEFAULT_CONFIG for prose.
    endorsement_boost: bool = False
    # Semantic dedup is opt-in — see DEFAULT_CONFIG for prose.
    semantic_dedup: bool = False
    # Provider selection — "auto" (default), "torch", or "fastembed".
    # The resolver in `semantic.resolve_provider` honours an explicit
    # value even when the corresponding extra isn't installed; auto-
    # detection prefers torch when both extras are present so legacy
    # `.embeddings.<model>.npz` caches stay byte-stable.
    semantic_provider: str = "auto"
    # Torch-provider model. Read when the resolved provider is "torch".
    semantic_model_name: str = "all-MiniLM-L6-v2"
    # Fastembed-provider model. Read when the resolved provider is
    # "fastembed". Default is the 384-dim BGE small variant — same
    # vector dimensionality as all-MiniLM-L6-v2 so threshold settings
    # are roughly interchangeable.
    semantic_model_fastembed: str = "BAAI/bge-small-en-v1.5"
    semantic_high_threshold: float = 0.85
    semantic_medium_threshold: float = 0.65
    # Floor on `applied_count` for inclusion in the heavily_used report.
    # Default is 3 — at 1 the bucket is mostly noise (one acknowledgement
    # is not a usage pattern). Raising it sharpens the signal at the cost
    # of seeing fewer rows when the event log is young; lowering it makes
    # the bucket more inclusive for fresh stores. Tune to taste.
    heavily_used_min_applied: int = 3
    # Cold-endorsement ratio threshold (0.0-1.0). When > 0, the
    # cold_endorsement_memories bucket also flags memories whose
    # explicit/total-applied ratio falls below this fraction, catching
    # the "1 explicit out of 50 auto" case the binary "explicit == 0"
    # check misses. Default 0.0 preserves the original strict
    # semantics (must have ZERO explicit endorsements to land in the
    # bucket). Try 0.1 to surface memories where less than 10% of
    # applies are explicit endorsements.
    cold_endorsement_ratio_threshold: float = 0.0
    # Default --older-than (days) for `bettermemory tombstones prune`.
    # 0 means "no default" — the CLI requires the flag explicitly.
    # Tombstones are never auto-pruned at runtime; this only affects
    # the human-driven CLI subcommand.
    tombstone_retention_days: int = 0
    # Days after `last_verified_at` past which the retrieval surface
    # marks a memory's verification "stale" and attaches a spot-check
    # recommendation. See `verify.compute_verification_status`. The
    # default mirrors `recency_boost_half_life_days` so freshness for
    # ranking and freshness for verification stay aligned.
    verification_stale_days: int = 30
    # Hard cap on a memory body's UTF-8 byte length at write/update time.
    # Default 1 MB — ~1000x the typical 1–2 KB memory body. 0 disables
    # the cap entirely (legacy behaviour). The check runs at the handler
    # boundary; existing on-disk memories are never re-validated, so
    # raising the cap downward doesn't reject already-stored data.
    max_content_bytes: int = 1_000_000
    # Hard cap on an episode takeaway's UTF-8 byte length at write time.
    # Separate from `max_content_bytes` because the takeaway is stored
    # in the YAML frontmatter region, which `_frontmatter` caps at
    # 64 KB to neutralise an alias-expansion DoS. A takeaway over that
    # threshold would corrupt the frontmatter, the loader would raise
    # ValueError on every subsequent read, and `list_by_session` would
    # silently skip — the episode would look committed but vanish from
    # every read surface (search, handoff, promote). Default 4 KB is
    # generous for the documented "one-sentence summary" while leaving
    # comfortable headroom inside the 64 KB YAML cap for the rest of
    # the frontmatter (id, session_id, created, scopes, origin). 0
    # disables the cap.
    max_takeaway_bytes: int = 4_096
    # Hard cap on the number of scopes accepted by a single memory_write /
    # memory_update / episode_write call. Defense-in-depth alongside the
    # model-layer cap (`models._MAX_SCOPES_PER_RECORD`, also 64) — the same
    # silent-data-loss class the takeaway cap closed in t16, applied to a
    # list-shaped frontmatter field. ~2200 short scope names serialise to
    # ~64 KB of YAML and push the frontmatter past `_frontmatter._MAX_YAML_BYTES`;
    # the loader then raises `ValueError` on every subsequent read and the
    # record vanishes from every read surface (search / list / handoff) despite
    # `status="committed"` returning. 64 matches `verified_paths` and is well
    # above the typical 1-5 scopes used in practice. Set to 0 to disable the
    # handler-boundary cap (the model-layer cap still fires).
    max_scopes_per_write: int = 64
    # One-shot per-session passive curation hint. When the sum of
    # dead_weight + drifted + cold_endorsement_memories counts (the
    # `curation_pending` rollup the model would otherwise have to
    # call `memory_scope_overview` to see) exceeds this threshold,
    # the first successful `memory_write` of the session inlines a
    # one-line nudge on the response. Default 5. 0 disables the
    # nudge entirely; setting it large effectively disables. Pull-
    # based remains the primary discovery path — this just closes
    # the in-conversation surfacing loop the audit identified.
    curation_hint_threshold: int = 5
    curation_hint_enabled: bool = True
    # Tool-surface breadth. When False, the curation / power-user MCP tools —
    # memory_health, memory_acknowledge_miss, memory_rename_scope,
    # memory_restore, memory_list_tombstones, memory_proposals — are NOT
    # registered, trimming six tools (and their long descriptions) out of the
    # context every client pays on every turn. They stay reachable via the
    # `bettermemory` CLI, and memory_proposals also auto-registers whenever
    # [proposals] auto_propose is on.
    #
    # Deliberate default asymmetry (see `load_config` and the round-trip test
    # in tests/test_config.py): this dataclass default is True, so an
    # explicitly-constructed Config — tests, programmatic embedders importing
    # bettermemory — gets the full capability set. The SHIPPED default that
    # `load_config()` applies when the user has no config.toml is False (lean).
    # Leanness is a deployment policy for the typical MCP client, not a
    # property of the config object; the loader is the policy layer (and these
    # objects are frozen, so policy can't be applied post-construction). The
    # curate-loop skill drives memory_health / memory_acknowledge_miss /
    # memory_restore as MCP tools, so it needs full_tool_surface = true. The
    # cut was measured in the dogfood event log: 43% of sessions never called
    # any memory tool, and these six had 0-8 organic calls each across 190
    # sessions.
    full_tool_surface: bool = True


@dataclass
class ScopesConfig:
    allowed: list[str] = field(default_factory=list)


@dataclass
class TelemetryConfig:
    """Event-log toggles. See DEFAULT_CONFIG for prose."""

    enabled: bool = True
    max_bytes: int = 10_000_000
    # When false (the default since 2.6.8), `memory_search` query text and
    # `memory_audit_turn` `probe_query` are redacted in the event log —
    # replaced with `{"hash": "<sha256-prefix>", "preview": "<32 chars>",
    # "len": <int>}`. Correlation across events still works (same query
    # has the same hash), and the first ~32 characters survive for triage,
    # but a secret pasted into a search no longer lands on disk verbatim.
    # Set true to restore the legacy verbatim shape. The event-log file is
    # also chmod'd 0o600 on first write, so this is defense-in-depth rather
    # than a permissions story.
    log_queries_verbatim: bool = False


@dataclass
class ConsolidateConfig:
    """Opt-in unattended consolidation. See DEFAULT_CONFIG for prose.

    Default OFF. When `auto_apply` is true AND telemetry is enabled (the
    event log is both the debounce clock and the audit trail), the Stop
    hook runs the *structurally-safe* consolidation subset — conservative
    near-duplicate dedup (reversible tombstone) and demote-never-applied
    (non-destructive fact→ambient retag) — at most once per
    `auto_apply_interval_hours`, and only when the active set is at or
    below `auto_apply_max_memories` (the pairwise dedup is O(N²); the cap
    keeps the turn-end hook responsive). Every action lands as a
    reviewable, reversible tombstone/event — the deliberate opposite of
    invisible "Dreaming" consolidation.
    """

    auto_apply: bool = False
    auto_apply_interval_hours: float = 24.0
    auto_apply_max_memories: int = 500


@dataclass
class ProposalsConfig:
    """Opt-in write-reflex closure. See DEFAULT_CONFIG for prose.

    Default OFF. When `auto_propose` is true, the Stop hook scans each
    turn's user message for durable-looking statements the model didn't
    write and queues them — inert and review-gated — for the
    `memory_proposals` tool. `max_pending` caps the queue so it can't
    grow without bound or nag: once full, extraction stops until
    proposals are accepted or dismissed.
    """

    auto_propose: bool = False
    max_pending: int = 20


@dataclass
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    consolidate: ConsolidateConfig = field(default_factory=ConsolidateConfig)
    proposals: ProposalsConfig = field(default_factory=ProposalsConfig)
    config_path: Path | None = None

    # ---- methods ----------------------------------------------------------

    def resolved_directory(self, cwd: Path | None = None) -> Path:
        """Apply the resolution rule and return an absolute directory path."""
        env_override = os.environ.get(ENV_DIR_OVERRIDE)
        if env_override:
            resolved = Path(env_override).expanduser().resolve()
            _warn_on_system_dir(ENV_DIR_OVERRIDE, resolved)
            return resolved

        if self.storage.directory:
            resolved = Path(self.storage.directory).expanduser().resolve()
            _warn_on_system_dir("[storage] directory", resolved)
            return resolved

        # `Path.cwd()` raises FileNotFoundError when the process's working
        # directory has been deleted out from under it — a real failure mode
        # in the Stop hook, where the user can `rm -rf` the dir they were
        # working in before the turn ends. Skip the project-scoped branch
        # and fall through to the global default in that case rather than
        # letting the exception escape and surface as a hook error banner.
        resolved_cwd: Path | None
        if cwd is not None:
            resolved_cwd = cwd.resolve()
        else:
            try:
                resolved_cwd = Path.cwd().resolve()
            except (FileNotFoundError, OSError):
                resolved_cwd = None

        if resolved_cwd is not None:
            project_dir = resolved_cwd / PROJECT_DIR_NAME
            if project_dir.is_dir():
                return project_dir.resolve()

        return (Path.home() / GLOBAL_DIR_NAME).resolve()


# Path prefixes that almost certainly indicate a misconfigured env var
# (someone typed `BETTERMEMORY_DIR=/etc` thinking it was relative, or
# the var got expanded against the wrong base). The store would then
# try to `mkdir(parents=True, exist_ok=True)` under a system directory
# and either EPERM at startup or — worse, if run as root — succeed and
# scatter markdown files into `/etc`. The warning is informational
# only; we still honour the value because there are legitimate cases
# (a custom mount, a chroot, an ops-managed prefix) we can't predict.
# `/var` is intentionally NOT in this list: macOS routes its per-user
# tmp dir through `/var/folders/...` (which resolves to `/private/var/...`),
# so warning on `/var` would fire on every legitimate `tmp_path` test
# and every ad-hoc tmpdir use. The protected set focuses on directories
# a user definitely doesn't mean to use as a writable memory store.
_SYSTEM_DIR_PREFIXES: tuple[Path, ...] = (
    Path("/etc"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/boot"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
)


def _warn_on_system_dir(source: str, resolved: Path) -> None:
    import logging

    for raw_prefix in _SYSTEM_DIR_PREFIXES:
        # `.resolve()` normalises macOS symlinks (`/etc` -> `/private/etc`,
        # `/var` -> `/private/var`). Without this, a `BETTERMEMORY_DIR=/etc/...`
        # that resolves to `/private/etc/...` on macOS would slip past
        # the is_relative_to check. Linux is already canonical so the
        # call is a no-op there. Tolerate non-existent prefixes silently
        # — different platforms have different system dirs.
        try:
            prefix = raw_prefix.resolve()
        except (OSError, ValueError):
            prefix = raw_prefix
        try:
            if resolved == prefix or resolved.is_relative_to(prefix):
                logging.getLogger("bettermemory.config").warning(
                    "%s resolves to %s, which is under a system directory "
                    "(%s). bettermemory will still try to use it, but this "
                    "is almost always a misconfiguration — check your env "
                    "var or config and point at a user-writable path.",
                    source,
                    resolved,
                    raw_prefix,
                )
                return
        except ValueError:
            # Path.is_relative_to raises on Windows when comparing
            # across drives; tolerate that silently.
            continue


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


# Strings that a string-typed TOML value can take to mean True / False.
# TOML's own `bool` type round-trips fine, but a value the user *quoted*
# (`x = "false"`) arrives as the str "false", and `bool("false")` is True
# — so a naive `bool(raw.get(...))` silently flips a quoted privacy opt-out
# ON. These sets map the common textual spellings; anything else falls back
# to the caller-supplied default rather than to truthiness.
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a TOML-sourced value to bool without the str trap.

    - A real ``bool`` passes through unchanged.
    - A ``str`` is matched case-insensitively after trimming against the
      true/false spellings above ("true"/"1"/"yes"/"on" -> True;
      "false"/"0"/"no"/"off"/"" -> False). An UNRECOGNISED string falls
      back to ``default`` — never to ``bool(non_empty_str) == True``,
      which is the bug this helper exists to prevent (a quoted
      ``log_queries_verbatim = "false"`` must stay False).
    - Anything else (int, None, list, ...) falls back to ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
        return default
    return default


def _coerce_positive_int(value: object, default: int) -> int:
    """Coerce to ``int`` and clamp a non-positive result to ``default``.

    A 0 or negative byte cap would otherwise reach the rotation guard in
    ``events._rotate_if_needed`` and make ``size < max_bytes`` never hold,
    triggering a gzip rotation on every append (a rotation storm). Treat
    ``<= 0`` as "use the default" at load time; ``events`` separately
    treats ``<= 0`` as "never rotate" for an explicitly-constructed
    Recorder. A non-int / unparseable value also falls back to ``default``.
    """
    # Narrow before calling int() so mypy strict (no int(object) overload) and
    # warn_return_any stay happy. bool is an int subclass — accept it directly.
    coerced: int
    if isinstance(value, bool):
        coerced = int(value)
    elif isinstance(value, int):
        coerced = value
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError:
            return default
    else:
        return default
    if coerced <= 0:
        return default
    return coerced


def _malformed_config_msg(
    label: str, value: object, config_path: Path | None, expected: str
) -> str:
    """Clear, located error for a mistyped config value — names the
    section/key, the offending value, and the file, so a bad TOML entry
    fails with ``malformed config in <path>: [section] key = '…' must be
    <expected>`` instead of an opaque stdlib ``int()``/``float()``
    traceback escaping ``load_config`` (which crashes ``bettermemory
    serve`` startup with no hint at the culprit key)."""
    where = f" in {config_path}" if config_path is not None else ""
    return f"malformed config{where}: {label} = {value!r} must be {expected}"


def _coerce_int(
    value: object, default: int, *, label: str, config_path: Path | None
) -> int:
    """Coerce a TOML value to ``int``, raising a located ValueError on a
    non-numeric value rather than letting a bare ``int(...)`` escape
    ``load_config``. Valid-input behaviour is identical to the prior bare
    ``int(...)``: a real int (or bool, an int subclass) passes through; a
    numeric string parses; a float truncates. ``None`` (key absent) yields
    ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(
                _malformed_config_msg(label, value, config_path, "an integer")
            ) from exc
    raise ValueError(_malformed_config_msg(label, value, config_path, "an integer"))


def _coerce_float(
    value: object, default: float, *, label: str, config_path: Path | None
) -> float:
    """Coerce a TOML value to ``float``, raising a located ValueError on a
    non-numeric value (cf. ``_coerce_int``). Valid-input behaviour matches
    the prior bare ``float(...)``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ValueError(
                _malformed_config_msg(label, value, config_path, "a number")
            ) from exc
    raise ValueError(_malformed_config_msg(label, value, config_path, "a number"))


def _coerce_str_list(
    value: object, *, label: str, config_path: Path | None
) -> list[str]:
    """Coerce a TOML list-of-strings, REJECTING a bare string scalar.

    ``list("myproject")`` silently char-explodes to ``['m', 'y', ...]`` —
    a forgotten-brackets ``allowed = "myproject"`` would then build a
    per-character allowlist that rejects every real write while accepting
    single-character scopes. Reject a non-list (and any non-string entry)
    with a clear, located error instead of the silent explosion."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            _malformed_config_msg(label, value, config_path, "a list of strings")
            + " — did you forget the [ ] brackets?"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                _malformed_config_msg(label, item, config_path, "a string (in a list)")
            )
        out.append(item)
    return out


def default_config_path() -> Path:
    return Path(platformdirs.user_config_dir("bettermemory")) / CONFIG_FILENAME


# T9: 3.2.0 renamed `endorsement_debt_ratio_threshold` ->
# `cold_endorsement_ratio_threshold` (commit 7346ecc) with no alias, so a
# user upgrading from 3.1.x with the old key in their TOML silently lost
# the threshold (fell back to the 0.0 default). This shim accepts the
# old key, maps it to the new field, and emits a one-shot per-(path,key)
# deprecation warning. Once-per-process matches the divergence-warning
# guard in store.py: a long-lived server (`bettermemory serve`) that
# rereads config on signal shouldn't spam the log on every reload, but
# two distinct config paths in the same process each get their own
# warning. Drop this shim no earlier than 3.4.x — long enough that any
# 3.1.x user has seen the deprecation warning at least once.
_DEPRECATED_KEY_WARNED_PATHS: set[tuple[Path, str]] = set()


def _apply_legacy_endorsement_debt_alias(
    behavior_raw: dict[str, object], config_path: Path
) -> None:
    """Translate the pre-3.2 `endorsement_debt_ratio_threshold` key to
    its 3.2 successor `cold_endorsement_ratio_threshold` in place.

    Four cases:

    1. Only the old key present -> copy the value under the new key and
       emit a one-shot DEPRECATION warning pointing at both names.
    2. Only the new key present -> no-op.
    3. Both present -> the new key wins (last writer wins on intent;
       the user clearly added it explicitly). Emit a STRONGER one-shot
       warning telling them to delete the stale old key.
    4. Neither present -> no-op.

    The (config_path, key) key on the warned-set lets a process serving
    multiple memory directories surface each config's drift separately,
    matching the `_DIVERGENCE_WARNED_ROOTS` discipline in store.py.
    Best-effort key resolution: `config_path.resolve()` collapses
    symlinks so two `load_config` calls naming the same file via
    different paths share one warning.
    """
    old_key = "endorsement_debt_ratio_threshold"
    new_key = "cold_endorsement_ratio_threshold"
    if old_key not in behavior_raw:
        return

    try:
        resolved = config_path.resolve()
    except OSError:
        # If the path can't be resolved (deleted out from under us between
        # the `open()` and here), fall back to the unresolved path so the
        # one-shot guard still works for the common case.
        resolved = config_path

    import logging

    log = logging.getLogger("bettermemory.config")

    if new_key in behavior_raw:
        # Both keys present: the new one wins. Stronger nudge — the user
        # is carrying dead config that's silently doing nothing.
        guard_key = (resolved, f"{old_key}+both")
        if guard_key not in _DEPRECATED_KEY_WARNED_PATHS:
            _DEPRECATED_KEY_WARNED_PATHS.add(guard_key)
            log.warning(
                "bettermemory: TOML config at %s sets BOTH the legacy "
                "`%s` and its 3.2.0 replacement `%s` under [behavior]. "
                "The new key wins; the legacy key is being ignored. "
                "Delete `%s` from your TOML to silence this warning.",
                resolved,
                old_key,
                new_key,
                old_key,
            )
        # Drop the legacy key so downstream code sees a clean dict.
        behavior_raw.pop(old_key, None)
        return

    # Old key only: migrate the value and warn once.
    behavior_raw[new_key] = behavior_raw.pop(old_key)
    guard_key = (resolved, old_key)
    if guard_key not in _DEPRECATED_KEY_WARNED_PATHS:
        _DEPRECATED_KEY_WARNED_PATHS.add(guard_key)
        log.warning(
            "bettermemory: TOML config at %s uses the deprecated "
            "`%s` key under [behavior]. The key was renamed to `%s` "
            "in 3.2.0; the legacy name still works for now but will "
            "be dropped in a future release. Rename "
            "`%s` to `%s` in your TOML to silence this warning.",
            resolved,
            old_key,
            new_key,
            old_key,
            new_key,
        )


def load_config(path: Path | None = None) -> Config:
    """Load config from `path`, creating it with defaults if missing."""
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        # Atomic + durable write via `_fsutil.atomic_write_bytes`: a plain
        # `config_path.write_text(...)` here would leave a truncated TOML
        # on power loss / process kill mid-write, and the next run would
        # see a malformed config and crash at `tomllib.load`. The helper
        # writes to a tmp sibling, fsyncs, atomic-renames into place, and
        # fsyncs the parent directory.
        atomic_write_bytes(config_path, DEFAULT_CONFIG.encode("utf-8"))
        # First-run notice on stderr so consumers see what happened.
        print(
            f"[bettermemory] created default config at {config_path}",
            file=sys.stderr,
        )

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    storage_raw = data.get("storage", {})
    behavior_raw = data.get("behavior", {})
    scopes_raw = data.get("scopes", {})
    telemetry_raw = data.get("telemetry", {})
    consolidate_raw = data.get("consolidate", {})
    proposals_raw = data.get("proposals", {})

    # T9: back-compat for the 3.1.x -> 3.2.0 TOML key rename. Mutates
    # `behavior_raw` so the downstream `behavior_raw.get(...)` lookups
    # below pick up the legacy value under the new key.
    _apply_legacy_endorsement_debt_alias(behavior_raw, config_path)

    return Config(
        storage=StorageConfig(directory=storage_raw.get("directory")),
        behavior=BehaviorConfig(
            require_write_confirmation=_coerce_bool(
                behavior_raw.get("require_write_confirmation"), False
            ),
            default_max_results=_coerce_int(
                behavior_raw.get("default_max_results"),
                5,
                label="[behavior] default_max_results",
                config_path=config_path,
            ),
            search_mode=str(behavior_raw.get("search_mode", "hybrid")),
            recency_boost_half_life_days=_coerce_float(
                behavior_raw.get("recency_boost_half_life_days"),
                30.0,
                label="[behavior] recency_boost_half_life_days",
                config_path=config_path,
            ),
            endorsement_boost=_coerce_bool(
                behavior_raw.get("endorsement_boost"), False
            ),
            semantic_dedup=_coerce_bool(behavior_raw.get("semantic_dedup"), False),
            semantic_provider=str(behavior_raw.get("semantic_provider", "auto")),
            semantic_model_name=str(
                behavior_raw.get("semantic_model_name", "all-MiniLM-L6-v2")
            ),
            semantic_model_fastembed=str(
                behavior_raw.get("semantic_model_fastembed", "BAAI/bge-small-en-v1.5")
            ),
            semantic_high_threshold=_coerce_float(
                behavior_raw.get("semantic_high_threshold"),
                0.85,
                label="[behavior] semantic_high_threshold",
                config_path=config_path,
            ),
            semantic_medium_threshold=_coerce_float(
                behavior_raw.get("semantic_medium_threshold"),
                0.65,
                label="[behavior] semantic_medium_threshold",
                config_path=config_path,
            ),
            heavily_used_min_applied=_coerce_int(
                behavior_raw.get("heavily_used_min_applied"),
                3,
                label="[behavior] heavily_used_min_applied",
                config_path=config_path,
            ),
            cold_endorsement_ratio_threshold=_coerce_float(
                behavior_raw.get("cold_endorsement_ratio_threshold"),
                0.0,
                label="[behavior] cold_endorsement_ratio_threshold",
                config_path=config_path,
            ),
            tombstone_retention_days=_coerce_int(
                behavior_raw.get("tombstone_retention_days"),
                0,
                label="[behavior] tombstone_retention_days",
                config_path=config_path,
            ),
            verification_stale_days=_coerce_int(
                behavior_raw.get("verification_stale_days"),
                30,
                label="[behavior] verification_stale_days",
                config_path=config_path,
            ),
            max_content_bytes=_coerce_int(
                behavior_raw.get("max_content_bytes"),
                1_000_000,
                label="[behavior] max_content_bytes",
                config_path=config_path,
            ),
            max_takeaway_bytes=_coerce_int(
                behavior_raw.get("max_takeaway_bytes"),
                4_096,
                label="[behavior] max_takeaway_bytes",
                config_path=config_path,
            ),
            max_scopes_per_write=_coerce_int(
                behavior_raw.get("max_scopes_per_write"),
                64,
                label="[behavior] max_scopes_per_write",
                config_path=config_path,
            ),
            curation_hint_threshold=_coerce_int(
                behavior_raw.get("curation_hint_threshold"),
                5,
                label="[behavior] curation_hint_threshold",
                config_path=config_path,
            ),
            curation_hint_enabled=_coerce_bool(
                behavior_raw.get("curation_hint_enabled"), True
            ),
            # Shipped default is LEAN: when the user hasn't set this key, the
            # server hides the curation/power-user tools. This intentionally
            # diverges from the BehaviorConfig dataclass default (True) — the
            # loader is the deployment-policy layer. See that field's comment
            # and the round-trip test's documented exception. Set
            # `full_tool_surface = true` under [behavior] for the full surface.
            full_tool_surface=_coerce_bool(
                behavior_raw.get("full_tool_surface"), False
            ),
        ),
        scopes=ScopesConfig(
            allowed=_coerce_str_list(
                scopes_raw.get("allowed"),
                label="[scopes] allowed",
                config_path=config_path,
            ),
        ),
        telemetry=TelemetryConfig(
            enabled=_coerce_bool(telemetry_raw.get("enabled"), True),
            # Clamp a 0/negative configured cap to the default — a non-positive
            # value reaching events._rotate_if_needed makes the size guard never
            # hold and gzip-rotates on every append (rotation storm).
            max_bytes=_coerce_positive_int(telemetry_raw.get("max_bytes"), 10_000_000),
            log_queries_verbatim=_coerce_bool(
                telemetry_raw.get("log_queries_verbatim"), False
            ),
        ),
        consolidate=ConsolidateConfig(
            auto_apply=_coerce_bool(consolidate_raw.get("auto_apply"), False),
            auto_apply_interval_hours=_coerce_float(
                consolidate_raw.get("auto_apply_interval_hours"),
                24.0,
                label="[consolidate] auto_apply_interval_hours",
                config_path=config_path,
            ),
            auto_apply_max_memories=_coerce_int(
                consolidate_raw.get("auto_apply_max_memories"),
                500,
                label="[consolidate] auto_apply_max_memories",
                config_path=config_path,
            ),
        ),
        proposals=ProposalsConfig(
            auto_propose=_coerce_bool(proposals_raw.get("auto_propose"), False),
            max_pending=_coerce_int(
                proposals_raw.get("max_pending"),
                20,
                label="[proposals] max_pending",
                config_path=config_path,
            ),
        ),
        config_path=config_path,
    )
