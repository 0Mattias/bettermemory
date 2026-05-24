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
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
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


def default_config_path() -> Path:
    return Path(platformdirs.user_config_dir("bettermemory")) / CONFIG_FILENAME


def load_config(path: Path | None = None) -> Config:
    """Load config from `path`, creating it with defaults if missing."""
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
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

    return Config(
        storage=StorageConfig(directory=storage_raw.get("directory")),
        behavior=BehaviorConfig(
            require_write_confirmation=bool(
                behavior_raw.get("require_write_confirmation", False)
            ),
            default_max_results=int(behavior_raw.get("default_max_results", 5)),
            search_mode=str(behavior_raw.get("search_mode", "hybrid")),
            recency_boost_half_life_days=float(
                behavior_raw.get("recency_boost_half_life_days", 30.0)
            ),
            semantic_dedup=bool(behavior_raw.get("semantic_dedup", False)),
            semantic_provider=str(behavior_raw.get("semantic_provider", "auto")),
            semantic_model_name=str(
                behavior_raw.get("semantic_model_name", "all-MiniLM-L6-v2")
            ),
            semantic_model_fastembed=str(
                behavior_raw.get("semantic_model_fastembed", "BAAI/bge-small-en-v1.5")
            ),
            semantic_high_threshold=float(
                behavior_raw.get("semantic_high_threshold", 0.85)
            ),
            semantic_medium_threshold=float(
                behavior_raw.get("semantic_medium_threshold", 0.65)
            ),
            heavily_used_min_applied=int(
                behavior_raw.get("heavily_used_min_applied", 3)
            ),
            tombstone_retention_days=int(
                behavior_raw.get("tombstone_retention_days", 0)
            ),
            verification_stale_days=int(
                behavior_raw.get("verification_stale_days", 30)
            ),
            max_content_bytes=int(behavior_raw.get("max_content_bytes", 1_000_000)),
        ),
        scopes=ScopesConfig(
            allowed=list(scopes_raw.get("allowed", [])),
        ),
        telemetry=TelemetryConfig(
            enabled=bool(telemetry_raw.get("enabled", True)),
            max_bytes=int(telemetry_raw.get("max_bytes", 10_000_000)),
            log_queries_verbatim=bool(
                telemetry_raw.get("log_queries_verbatim", False)
            ),
        ),
        config_path=config_path,
    )
