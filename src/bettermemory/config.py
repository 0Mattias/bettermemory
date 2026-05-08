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

# When true, memory_write dedup uses sentence-transformers cosine
# similarity instead of Jaccard on token sets — catches paraphrases like
# "the database" / "Postgres" that lexical overlap misses. Requires the
# `embeddings` extra: `pip install bettermemory[embeddings]`. Falls back
# to Jaccard with a WARNING log line when the extra isn't installed, so
# flipping the bit without the deps is safe.
semantic_dedup = false

# Embedding model name for semantic dedup. all-MiniLM-L6-v2 is the small
# default; replace with a larger model if you need better paraphrase
# detection and have CPU/RAM headroom.
semantic_model_name = "all-MiniLM-L6-v2"

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

[scopes]
# If non-empty, writes with scopes outside this list fail. Empty = anything.
allowed = []

[telemetry]
# Append-only JSONL event log at <storage>/.events.jsonl. One line per tool
# call: search queries, returned IDs, write/update/remove events. Used by the
# memory_health view, by use-recording feedback, and to tune the durability
# marker list against real traffic. Lives next to the memories — same trust
# boundary, no new permissions story. Search queries are recorded verbatim;
# set `enabled = false` to opt out.
enabled = true

# Rotate (gzip) the active log when it crosses this many bytes. Archives are
# kept indefinitely — prune by hand if disk pressure matters.
max_bytes = 10000000
"""


@dataclass
class StorageConfig:
    directory: str | None = None  # if None, use resolution rule.


@dataclass
class BehaviorConfig:
    require_write_confirmation: bool = False
    default_max_results: int = 5
    recency_boost_half_life_days: float = 30.0
    # Semantic dedup is opt-in — see DEFAULT_CONFIG for prose.
    semantic_dedup: bool = False
    semantic_model_name: str = "all-MiniLM-L6-v2"
    semantic_high_threshold: float = 0.85
    semantic_medium_threshold: float = 0.65
    # Floor on `applied_count` for inclusion in the heavily_used report.
    # Default is 3 — at 1 the bucket is mostly noise (one acknowledgement
    # is not a usage pattern). Raising it sharpens the signal at the cost
    # of seeing fewer rows when the event log is young; lowering it makes
    # the bucket more inclusive for fresh stores. Tune to taste.
    heavily_used_min_applied: int = 3


@dataclass
class ScopesConfig:
    allowed: list[str] = field(default_factory=list)


@dataclass
class TelemetryConfig:
    """Event-log toggles. See DEFAULT_CONFIG for prose."""

    enabled: bool = True
    max_bytes: int = 10_000_000


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
            return Path(env_override).expanduser().resolve()

        if self.storage.directory:
            return Path(self.storage.directory).expanduser().resolve()

        cwd = (cwd or Path.cwd()).resolve()
        project_dir = cwd / PROJECT_DIR_NAME
        if project_dir.is_dir():
            return project_dir.resolve()

        return (Path.home() / GLOBAL_DIR_NAME).resolve()


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
            recency_boost_half_life_days=float(
                behavior_raw.get("recency_boost_half_life_days", 30.0)
            ),
            semantic_dedup=bool(behavior_raw.get("semantic_dedup", False)),
            semantic_model_name=str(
                behavior_raw.get("semantic_model_name", "all-MiniLM-L6-v2")
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
        ),
        scopes=ScopesConfig(
            allowed=list(scopes_raw.get("allowed", [])),
        ),
        telemetry=TelemetryConfig(
            enabled=bool(telemetry_raw.get("enabled", True)),
            max_bytes=int(telemetry_raw.get("max_bytes", 10_000_000)),
        ),
        config_path=config_path,
    )
