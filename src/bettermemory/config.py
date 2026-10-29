"""Config loading and the rules for picking a memory directory.

Resolution order for the storage directory:

1. The `BETTERMEMORY_DIR` env var, if set.
2. `[storage] directory` in `config.toml`, if set.
3. `./.claude-memory/` if it exists in the current working directory
   (project-scoped: write a memory while in that project, see it only
   when you come back to that project), unless `[storage] cwd_store` is
   false.
4. `~/.claude-memory/` (global fallback).

A user-level config file lives at `~/.config/bettermemory/config.toml` (or the
platform equivalent). It's created on first run with every setting commented
out (`DEFAULT_CONFIG`), so the code's defaults apply until a line is
uncommented; a file that exists is never rewritten.

An organisation-managed config (`managed.py`: a file at a fixed system
path, on Windows a registry value under HKEY_LOCAL_MACHINE) is read after
it, and every key it sets is pinned: the user file's value for that key is
ignored (`_apply_managed`).

The section builders. `config_sections` builds the longer sections for
`_config_from_data`, so that splitting that function (ENG-97) keeps this
module under the package's 2,000-line bound (ENG-96). This module
imports it only when a file is read, and it reads this module's names
through it when it runs, so a patch here reaches it.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import platformdirs

if TYPE_CHECKING:
    import logging

from . import managed as _managed
from ._fsutil import atomic_write_bytes


CONFIG_FILENAME = "config.toml"
PROJECT_DIR_NAME = ".claude-memory"
GLOBAL_DIR_NAME = ".claude-memory"
ENV_DIR_OVERRIDE = "BETTERMEMORY_DIR"
# Where the store's key and head checkpoint live, ahead of the user config
# directory. A scratch store (a bench, a demo, a test that spawns the
# server) sets it so it leaves no key behind.
KEYS_DIR_ENV = "BETTERMEMORY_KEYS_DIR"
#: The store file inside the resolved directory (`store.py` re-exports it).
STORE_FILENAME = "memory.sqlite"

#: Where one parsed config came from, as the loader names it in its errors
#: and notices: a file, or the registry value that holds the managed config
#: on Windows (`managed.RegistryValue`), which has no path.
ConfigSource: TypeAlias = Path | _managed.RegistryValue

DEFAULT_CONFIG = """\
# bettermemory config
#
# See: https://github.com/0Mattias/bettermemory
#
# Nothing in this file is set. Every setting is commented out, so the
# built-in default applies to each, and a later release that changes a
# default reaches you. To choose a value, remove the "# " in front of its
# line, under its [section] header, and edit it. Most lines show the
# default; a few show the value that changes it, and the comment above
# such a line says what the default is.

[storage]
# Where memories live. The resolution rule:
#   1. $BETTERMEMORY_DIR
#   2. this key, when set
#   3. ./.claude-memory if cwd has one
#   4. ~/.claude-memory
# directory = "~/.claude-memory"

# Rule 3 lets the directory a client starts in supply the store, and a
# cloned repository that commits a .claude-memory/ directory then becomes
# your store: its memories reach the session brief and the prompt recall,
# and your own writes in that checkout land inside its working tree. Set
# false to skip rule 3, so only $BETTERMEMORY_DIR, `directory` above or
# ~/.claude-memory can be the store. Default true.
# cwd_store = false

[behavior]
# Default cap on memory_search results. Clamped to 1..50 where it is read
# (the same range an explicit max_results is clamped to), not rejected
# here: a value outside the range is a harmless typo, and erroring at load
# would take the whole server down over one knob.
# default_max_results = 5

# Recency boost decay. Larger = older memories get a meaningful bump.
# recency_boost_half_life_days = 30

# Retrieval ranker for memory_search. One of:
#   "keyword" — the original TF + coverage + recency scorer (legacy default
#       in 1.6.0). No IDF weighting, so rare-term queries underperform.
#   "bm25"    — Okapi BM25 with the same scope-bonus + recency boost
#   "hybrid"  — reciprocal-rank-fusion of keyword + BM25. Default since
#       2.6.8; a strict improvement over either alone. Every mode is
#       deterministic code — the project ships no embedding models
#       (removed in 4.0.0). The MCP `mode` parameter on memory_search
#       overrides this per-call.
# search_mode = "hybrid"

# The Lane L conversational repairs (6.1.0): when a query has a temporal
# reading ("how many weeks ago did I…", "what did I do in March?"), its
# temporal-scaffold words (day/week/ago/last/many and kin) are priced as
# common words in the BM25 legs so the question's own syntax cannot
# outprice its content, and date-anchored items matching an explicit
# window are boosted. ON BY DEFAULT: the L1 gate read measured +1.27
# LongMemEval macro-recall@5 points (+0.93 at @1) with the dev
# instrument byte-identical and no question type regressed
# (the L1 record). Queries with no temporal reading are untouched
# byte for byte. Set false to reproduce the pre-6.1.0 ranking exactly.
# conversational = true

# Write-time supersession. When a claim-sized memory_write carries a
# change cue (moved, switched, renamed, raised, no longer, the previous,
# ...) and diverges on a value from a stored claim about the same
# subject, the new memory gets a `supersedes` link to the stored one,
# which memory_search renders as `superseded_by` on the stale hit and
# on nothing else. The same divergence with no cue files the pair for
# memory_conflicts instead of guessing which side is current. Measured
# on bench/integrity's sealed corpus: 27 of the 40 update statements
# link to the statement they replace and nothing links a distractor,
# a hard negative or a cross-topic pair (bench/integrity/results/). Set
# false to leave links to the writer's explicit `supersedes=` list.
# write_supersession = true

# Recall at prompt time. The plugin's UserPromptSubmit hook probes every
# submitted prompt with the production ranker (the pool and ranking the
# silent-miss audit uses) and puts up to three pointers in front of the
# model before the turn starts: an id, a relevance label, an 80-character
# summary and the matched snippet, never a body, so the read path's
# verify-before-relying discipline still runs through memory_show. A
# pointer is one of the probe's top three hits, written or imported here
# (never a synced or unaccounted one), with no open contradiction, that
# this session segment has not already been shown, retrieved, written or
# updated; a superseded hit yields its slot to its head. No relevance
# label and no count holds a delivery; a compact or a clear opens a new
# segment. "project" (default) delivers only memories of the project the
# prompt comes from (inside a project only); "everywhere" delivers as an
# unscoped search reaches: every project's memories and the ones written
# outside any project, inside a project or outside one, never a memory
# across a boundary (`boundary = true`) and never one whose location
# could not be read; "off" returns to purely opt-in retrieval. Each delivery is recorded as
# a `prompt_recall` event, product state kept whatever [telemetry] says,
# so nothing is injected off the books. true / false in an older config
# read as "project" / "off".
# prompt_recall = "project"

# The session brief: on SessionStart inside a project, two lists of
# pointers (id and an 80-character summary): the project's standing records
# in force, each marked in force, and the records the conversation so far
# bears on, ranked by the owner's words in the session's transcript, eight
# of each at most; a superseded record is never shown, and an open
# contradiction holds a record out of the second list. Pointers, not
# instructions; recorded as a `session_brief` event, with telemetry off
# too. Set false to open sessions with nothing.
# session_brief = true

# Floor on `applied_count` for inclusion in `memory_health.heavily_used`.
# Default 3 — at 1 the bucket is dominated by one-off acknowledgements
# rather than repeat-use patterns. Lower it on a fresh store; raise it
# once the event log has weeks of data.
# heavily_used_min_applied = 3

# Cold-endorsement ratio threshold (0.0-1.0). When > 0, the
# `cold_endorsement_memories` bucket in `memory_health` ALSO flags
# memories whose explicit-applied / total-applied ratio falls below
# this fraction — catching the "1 explicit endorsement out of 50
# auto" case the strict "explicit == 0" check misses. Default 0.0
# keeps the strict behaviour (only zero-explicit memories surface).
# Set to 0.1 to additionally surface memories where less than 10% of
# applies are explicit endorsements.
# cold_endorsement_ratio_threshold = 0.0

# Days after `last_verified_at` past which a memory's verification is
# considered "stale" — the retrieval surface attaches a re-spot-check
# recommendation to the response. 30 days mirrors the recency-boost
# half-life: memories the ranker no longer treats as fresh for ordering
# also stop counting as fresh for verification. Set 0 to mark every
# verified memory stale immediately (useful in tests, rarely in practice).
# verification_stale_days = 30

# Default retention for `bettermemory tombstones prune` (days). Tombstones
# are never auto-pruned; this is just the default for the CLI subcommand,
# which still requires an explicit invocation. 0 means "no default" — the
# CLI requires --older-than to be passed. Set this to e.g. 365 if you want
# `bettermemory tombstones prune` with no flag to default to one-year
# retention. Active memories are unaffected.
# tombstone_retention_days = 0

# Hard cap on a single memory body's UTF-8 byte length at `memory_write`
# / `memory_update` time. Existing memories on disk are never re-validated
# — this is a write-time bound that protects against a runaway model or a
# hostile client filling disk with a multi-gigabyte body. The default of
# 1 MB is ~1000x a typical memory (which sits at 1–2 KB); raise it if you
# legitimately curate very long context dumps as single memories, lower
# it for stricter resource boundaries. Set to 0 to disable the cap.
# max_content_bytes = 1000000

# Hard cap on a single episode takeaway's UTF-8 byte length at
# `episode(action="write")` time, checked at the handler boundary.
# Separate from `max_content_bytes` because the takeaway is the one-line
# summary `episode(action="handoff")` delivers ahead of any body, so it
# has a bound of its own. 4 KB is generous for a one-sentence summary and
# keeps an 8.x mirror's frontmatter (`bettermemory export --mirror`,
# capped at 64 KB by `_frontmatter._MAX_YAML_BYTES`) well inside its cap.
# Set to 0 to disable the cap.
# max_takeaway_bytes = 4096

# Hard cap on the number of scopes accepted by a single memory_write,
# memory_update or episode(action="write") call, checked at the handler
# boundary alongside the model-layer cap (also 64). 64 matches the
# verified_paths cap and is well above any realistic per-record scope
# count (1 to 5 in practice). Set to 0 to disable the handler-boundary
# cap (the model-layer cap still fires at 64).
# max_scopes_per_write = 64

# Opt-in floor on a memory body's whitespace-separated token count at
# `memory_write` time. 0 (the default) disables it: the only shipped lower
# bound is "non-empty after stripping", so a one-word body still commits,
# which is correct for a caller storing an identifier, a path, or a version
# pin. Set it (5 to 8 is a reasonable band for a self-contained statement)
# when writes arrive from unattended or bulk callers, where a fragment costs
# a durable record plus the curation pass that later removes it. The floor
# binds `memory_write` only; `memory_update` does NOT, so a body edit can
# still take an existing memory below the floor.
# min_content_tokens = 0

[scopes]
# If non-empty, writes with caller-supplied scopes outside this list fail.
# Empty = anything. One narrow exemption, and it is not a property of
# the scopes themselves: `memory_update`, whose `scopes` argument REPLACES
# the stored list. Keeping a scope means resubmitting it, so the check
# runs over what an edit ADDS. A scope already on the record passes
# because it was already accepted; one that is not is still checked by
# name, so the exemption cannot be borrowed to plant an unallowed scope.
# allowed = []

# Singleton scopes the `fix_typo_scopes` health check must stop flagging.
# That check looks for a one-memory scope that resembles a more common one
# ("projct:foo" against "projects:foo") and recommends folding it with
# memory_admin's rename_scope action. When two projects legitimately share
# a name stem it
# is a FALSE POSITIVE, and it had no off switch: the recommendation
# re-fired on every curation pass, so every pass had to re-adjudicate the
# same question, and the cost of getting it wrong is asymmetric. On
# 2026-09-15 a client folded a scope this way that the memory it was
# folding forbids in bold on the owner's confirmation, after several
# earlier passes had each declined it. Its siblings (`dead_weight`,
# `cold_endorsement`) already carry suppression flags; this is that.
# Name the scope exactly as it appears; entries that are not currently
# flagged are simply inert.
# typo_exceptions = []

[policy]
# The write gates' escape hatches are parameters the agent sets itself:
# `force` on memory_write skips the duplicate gates, and each
# `acknowledge_*` flag passes one gate. These switches take them away. A
# call that passes a forbidden flag as true is refused whole, naming the
# switch, and writes nothing. Both are off by default.
#
# Refuse memory_write calls with force=true.
# forbid_force = true
#
# Refuse calls that pass any of these acknowledge flags as true. Names:
# credential, transient, scope_mismatch and user_claim (memory_write),
# credential, transient, user_claim and truncation (memory_update). An
# unknown name stops the config from loading.
# forbid_acknowledge = ["credential"]

[daemon]
# The port the local daemon binds on 127.0.0.1. `bettermemory up` falls
# back to an ephemeral port when this one is held by something else and
# records the port it got in the state file, which is how the stdio
# shim and the hook commands find it.
# port = 7397

[telemetry]
# Telemetry events are rows of the store's own signed log: one per tool
# call (search queries redacted to a hash and a short preview, returned
# ids, write, update and remove events). The health report and
# `bettermemory eval` read them. Same file, same trust boundary, no new
# permissions story. Set `enabled = false` to opt out of measurement:
# the held recalls, grades and conflict scans are not recorded. The rows
# the product reads back (the shows and searches, the writes, the uses,
# the Stop's audit, the recall's and the review's rows) and the audit
# trail are kept either way, with only the ids, statuses, counts and
# stamps their readers read: no query, prompt or body text, note or
# quoted word (docs/operations.md lists each kind's). The brief and the
# recall still work.
# enabled = true

[security]
# Which rows of the signed log this machine trusts. A record reads
# `unaccounted` when the log row behind it does not verify under a key
# this machine holds. Rows added to the log after this machine's own
# under a key it never held read `unaccounted`: only someone writing
# into the file without the key leaves that shape, and
# `bettermemory log verify` reports it as tampering. Rows from before
# this machine first signed the store (a store copied here from another
# machine without its key) keep the labels they arrived with. Where this
# machine's own rows begin is kept beside the key (`<store_id>.origin`),
# written at the store's first writing open under this release; an edit
# made before that open, or one that gives the store a fresh id, can
# still pass an editor's rows off as such a copy's. With
# `strict_provenance = true` those read `unaccounted` too, and only what
# this machine's own keys verify is trusted: the setting for a managed
# deployment, or wherever a store may arrive from somewhere else or be
# written to by someone else. Off by default.
# strict_provenance = true

[capture]
# Session capture at a turn end, Claude Code only, and off by default.
# With `stop_checkpoint = true`, once the conversation since the last
# capture holds `checkpoint_tokens` of your prompts and the assistant's
# text, the Stop hook asks the chat model in the session to save what the
# stretch established, with memory_write. No other model and no API key
# is involved, and only a session a person is attending is ever asked.
# Each capture is a batch: `bettermemory capture review` lists them and
# `bettermemory capture undo <batch>` takes one back.
# stop_checkpoint = false
# checkpoint_tokens = 12000
# At most this many capture requests per store per UTC day.
# daily_cap = 12

[membership]
# Which project a session belongs to is decided from what its checkout
# shows: its git directory, its commits, its remotes, its
# bettermemory.toml, and a checkout token. The token is the one file
# bettermemory writes into a repository: `info/bettermemory` inside the git
# directory (`.git/info/bettermemory`), never in the working tree. git does
# not track it, `git clone` does not copy it, and a moved or copied checkout
# carries it. It is never written into a bare repository, another user's
# repository or a read-only one. With `write_token = false` it is written
# nowhere and the other evidence decides alone: a moved or copied checkout
# with no commits, which its token would have recognised, is asked about.
# write_token = true
# With `strict = true`, a checkout that would join a project because it
# holds the project's commits asks once instead. Joins by a
# bettermemory.toml, by your own `bettermemory project join`, by the same
# git directory or object store, or by a restore at the project's old path
# stay silent.
# strict = false
"""


@dataclass
class StorageConfig:
    directory: str | None = None  # if None, use resolution rule.
    # Resolution rule 3, `./.claude-memory/` in the working directory.
    # False skips it (`Config.resolved_directory`): the rule lets a cloned
    # repository that commits that directory become the store, its rows
    # reading with the labels they were stored under. Default true keeps
    # the rule exactly; the key is the one an organisation pins false in
    # the managed file (`managed.py`).
    cwd_store: bool = True


@dataclass
class BehaviorConfig:
    # Coerced to an int at load and NOT range-checked. Every consumer
    # narrows it through `handlers.search.clamp_search_width` instead —
    # one clamp for the request width and the audit probe's guard width,
    # so an out-of-range knob cannot move one without the other.
    default_max_results: int = 5
    recency_boost_half_life_days: float = 30.0
    # The Lane L conversational repairs: the temporal-scaffold df-floor
    # plus boost-only date-anchor windows (see `search.search`'s
    # `conversational` parameter; unit contract the L1 declaration,
    # gate read the L1 record). DEFAULT ON since 6.1.0 — the gate
    # measured +1.27 LongMemEval macro@5 / +0.93 @1 on the full 500 with
    # the dev instrument byte-identical, no question type regressed, and
    # the untouched holdout half generalizing stronger than the tuning
    # half. False reproduces the pre-6.1.0 ranking exactly (the repairs
    # engage only on queries with a temporal reading, so non-temporal
    # queries are byte-identical either way).
    conversational: bool = True
    # Retrieval ranker for `memory_search`. One of `keyword` (the
    # original TF + coverage + recency scorer; legacy), `bm25` (Okapi
    # BM25), or `hybrid` (RRF fusion of both; default since 2.6.8 —
    # the keyword scorer lacks IDF weighting, so rare-term queries
    # underperform, and hybrid is a strict improvement). The MCP
    # `mode` parameter on memory_search overrides this per-call.
    search_mode: str = "hybrid"
    # Write-time supersession (`supersession.detect_supersession`): a
    # claim-sized write that carries a change cue and diverges on a value
    # from a stored claim about the same subject gets a `supersedes` link
    # to it; the same divergence without a cue files the pair for
    # memory_conflicts. Default ON — the link is annotation only (never
    # reorders a hit) and the rule set nothing false on the benchmark
    # corpus or the maintainer's store. False leaves links to the
    # writer's explicit `supersedes=` parameter.
    write_supersession: bool = True
    # Recall at prompt time (`recall.prompt_recall`, wired to the plugin's
    # UserPromptSubmit hook): "project" (default) delivers pointers to
    # memories of the caller's project (the prompt recall's admission,
    # design 3.5), "everywhere" those an unscoped search admits (every
    # project's and the global ones, never across a boundary, decision
    # B21), "off" nothing. The loader reads a
    # legacy boolean as "project" / "off" (`_coerce_prompt_recall`).
    # Each delivery's `prompt_recall` row is product state, recorded with
    # telemetry off too (`events.STATE_FIELDS`, ENG-108), so no injection is
    # invisible to the Stop-hook shield or goes off the books. See
    # DEFAULT_CONFIG for prose.
    prompt_recall: str = "project"
    # The session brief (`recall.session_brief`): on SessionStart inside a
    # project, two lists of pointers, the caller's standing records in
    # force and the records the conversation so far bears on, eight of
    # each at most. Default ON; its `session_brief` row is product state,
    # like the prompt recall's.
    session_brief: bool = True
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
    # Hard cap on an episode takeaway's UTF-8 byte length at write time,
    # checked at the handler boundary (`handlers.episode_write`). Separate
    # from `max_content_bytes` because the takeaway is the one-line
    # summary the handoff delivers ahead of any body. Default 4 KB, which
    # also keeps an 8.x mirror's frontmatter inside the 64 KB cap
    # `_frontmatter` holds it to. 0 disables the cap.
    max_takeaway_bytes: int = 4_096
    # Hard cap on the number of scopes accepted by a single memory_write /
    # memory_update / episode write call, checked at the handler boundary
    # (`handlers._shared._validate_scope_count`) alongside the model-layer
    # cap (`models._MAX_SCOPES_PER_RECORD`, also 64). 64 matches
    # `verified_paths` and is well above the typical 1 to 5 scopes used in
    # practice. Set to 0 to disable the handler-boundary cap (the
    # model-layer cap still fires).
    max_scopes_per_write: int = 64
    # Opt-in floor on a memory body's whitespace-token count at write time.
    # 0 (default, and the loader default too, so this is NOT one of the
    # deliberate asymmetries) leaves the shipped "non-empty after strip"
    # bound, so a one-word body commits exactly as it always has. Enabling
    # it binds `memory_write` through `_validate_write_payload`, but not
    # `memory_update`, which size-checks a replacement body without
    # routing through that validator. See DEFAULT_CONFIG for the
    # recommendation.
    min_content_tokens: int = 0


@dataclass
class ScopesConfig:
    allowed: list[str] = field(default_factory=list)
    # Singleton scopes that `memory_health`'s `fix_typo_scopes` check must
    # stop flagging. See DEFAULT_CONFIG for why this exists.
    typo_exceptions: list[str] = field(default_factory=list)


#: The names `[policy] forbid_acknowledge` takes: each is an
#: `acknowledge_<name>` parameter of memory_write or memory_update.
#: memory_write has credential, transient, scope_mismatch and user_claim;
#: memory_update has credential, transient, user_claim and truncation.
#: Spelled here, beside the section that reads them, because the loader
#: validates against them and `config` imports no handler module;
#: `tests/test_policy.py` pins them to the two tools' served parameters.
ACKNOWLEDGE_NAMES: tuple[str, ...] = (
    "credential",
    "transient",
    "scope_mismatch",
    "user_claim",
    "truncation",
)


@dataclass
class PolicyConfig:
    """Switches that take the write gates' escape hatches away from the
    agent (`policy.check_overrides`). Off by default, and then the two
    tools behave exactly as they did before the section existed.

    `forbid_force` refuses a memory_write that passes ``force=True``;
    memory_update has no `force`. `forbid_acknowledge` names the
    `acknowledge_<name>` flags (`ACKNOWLEDGE_NAMES`) a call may not pass
    as true. A refusal is a tool error naming the switch and whether the
    organisation's managed file pins it."""

    forbid_force: bool = False
    forbid_acknowledge: list[str] = field(default_factory=list)


@dataclass
class DaemonConfig:
    port: int = 7397


@dataclass
class TelemetryConfig:
    """Event-log toggles. See DEFAULT_CONFIG for prose."""

    enabled: bool = True


@dataclass
class SecurityConfig:
    """How far the store trusts rows its own keys do not verify.

    `strict_provenance` widens the read-time provenance check: rows in a
    segment of the signed log whose key this machine does not hold read
    `unaccounted` wherever that segment sits. Off, only a segment after
    one this machine signed reads so (a foreign segment, always), and the
    leading segments of a store copied here without its key keep their
    labels. The store reads the switch through `load_config` the first
    time a row in such a leading segment is labelled
    (`Store.strict_provenance`), so a store with one key throughout never
    reads it. See DEFAULT_CONFIG for prose."""

    strict_provenance: bool = False


@dataclass
class CaptureConfig:
    """The turn-end capture checkpoint: at a Stop the daemon may ask the
    chat model to save the stretch since its last capture. Off unless
    `stop_checkpoint` is set; 8.x's `enabled` never sets it, because it
    switched on a capture that called a model of its own."""

    stop_checkpoint: bool = False
    # The stretch a checkpoint waits for, in tokens of four characters.
    checkpoint_tokens: int = 12_000
    # At most this many capture requests per store per UTC day.
    daily_cap: int = 12


@dataclass
class MembershipConfig:
    """Membership by checkout and project. `write_token` is the policy
    switch for the checkout token (design 1.1, decision D11); `strict` is
    the A3 switch, under which a join resting on history without the
    project file asks once (decision D8)."""

    write_token: bool = True
    strict: bool = False


@dataclass
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    membership: MembershipConfig = field(default_factory=MembershipConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    config_path: Path | None = None
    # The organisation-managed config this config was overlaid with (its
    # file, or on Windows its registry value), None when the machine has
    # none (`managed.py`), and the settings it pinned as "section.key"
    # (`"policy.forbid_force"`). Only keys that name a setting of this
    # config are listed; a key the loader does not read is ignored in the
    # managed config as it is in config.toml. A surface that reports where
    # a value came from labels these managed.
    managed_path: _managed.ManagedSource | None = None
    managed_keys: frozenset[str] = frozenset()

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

        # `[storage] cwd_store = false` skips rule 3 whole: the working
        # directory is not even looked at, so a repository's
        # `.claude-memory/` can never be chosen, and the fall-through below
        # is the home default. The hook client and the shim resolve through
        # this method (`_daemon_client.resolved_store_path`, `shim.py`), so
        # they follow the switch with no code of their own.
        if not self.storage.cwd_store:
            return (Path.home() / GLOBAL_DIR_NAME).resolve()

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
            # `os.path.isdir` rather than `Path.is_dir()`: the latter
            # re-raises EACCES and friends on 3.11-3.13 (and answers
            # False on 3.14), so a cwd whose children cannot be stat'd
            # aborted every entry path — CLI, server startup, both
            # hooks — instead of taking the global fallback the
            # unreadable-cwd branch above already takes. "Could not
            # stat the project store" and "there is no project store"
            # both mean the store this process can use lives elsewhere.
            if os.path.isdir(project_dir):
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

#: Set while `_read_alone` reads one line alone. Every way the loader does
#: not take the line as written lands in the list: each coercion notes its
#: fallback, clamp or lossy reading (`_trial_note`), whether or not the
#: logging configuration lets its warning through, and `_trial_filter`
#: keeps the warnings off the log. None otherwise.
_TRIAL: ContextVar[list[str] | None] = ContextVar("config_trial", default=None)


def _trial_note(what: str) -> None:
    """Record, inside `_read_alone`'s read, that the loader did not take
    the line as written; outside one, nothing."""
    notes = _TRIAL.get()
    if notes is not None:
        notes.append(what)


def _trial_filter(record: logging.LogRecord) -> bool:
    """The `bettermemory.config` logger's filter. Inside `_read_alone`'s
    read a warning is a note on the line, never logged: the line is
    dropped unread. Outside one every record passes. A ContextVar, so a
    read in one thread hides nothing another thread logs. The coercions
    note their own fallbacks too, since a disabled logger or level never
    reaches a filter."""
    notes = _TRIAL.get()
    if notes is None:
        return True
    notes.append(record.getMessage())
    return False


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a TOML-sourced value to bool without the str trap.

    - A real ``bool`` passes through unchanged.
    - A ``str`` is matched case-insensitively after trimming against the
      true/false spellings above ("true"/"1"/"yes"/"on" -> True;
      "false"/"0"/"no"/"off"/"" -> False). An UNRECOGNISED string falls
      back to ``default`` — never to ``bool(non_empty_str) == True``,
      which is the bug this helper exists to prevent (a quoted
      ``conversational = "false"`` must stay False).
    - Anything else (int, None, list, ...) falls back to ``default``.

    The fallback logs nothing, as before; a pinned user line read through
    `_read_alone` still learns it was not read as written.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
    if value is not None:
        _trial_note(f"{value!r} is no boolean spelling")
    return default


_BARE_KEY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_INF = float("inf")


def toml_spelling(value: object) -> str:
    """`value` as config.toml writes it: a string in double quotes with
    JSON's escapes, which are TOML's, ``true`` and ``false``, a number
    (``inf`` and ``nan`` as TOML spells them), a list in brackets and a
    table inline (``{port = 1}``). A date or time is its own text. The
    loader's messages and `bettermemory config check` both print a value
    through it, so a line reads as it is written in the file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and (value != value or abs(value) == _INF):
        return "nan" if value != value else ("inf" if value > 0 else "-inf")
    if isinstance(value, (int, float, str)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_spelling(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = (
            f"{_toml_key(key)} = {toml_spelling(item)}" for key, item in value.items()
        )
        return "{" + ", ".join(pairs) + "}"
    return str(value)


def _toml_key(key: object) -> str:
    """A table key bare when TOML allows it bare, quoted otherwise."""
    text = str(key)
    if text and set(text) <= _BARE_KEY_CHARS:
        return text
    return json.dumps(text, ensure_ascii=False)


def _malformed_config_msg(
    label: str, value: object, config_path: ConfigSource | None, expected: str
) -> str:
    """Clear, located error for a mistyped config value — names the
    section/key, the offending value, and the file, so a bad TOML entry
    fails with ``malformed config in <path>: [section] key = "…" must be
    <expected>`` instead of an opaque stdlib ``int()``/``float()``
    traceback escaping ``load_config`` (which crashes ``bettermemory
    serve`` startup with no hint at the culprit key)."""
    where = f" in {config_path}" if config_path is not None else ""
    shown = toml_spelling(value)
    return f"malformed config{where}: {label} = {shown} must be {expected}"


def _coerce_int(
    value: object, default: int, *, label: str, config_path: ConfigSource | None
) -> int:
    """Coerce a TOML value to ``int``, raising a located ValueError on a
    non-numeric value rather than letting a bare ``int(...)`` escape
    ``load_config``. Valid-input behaviour is identical to the prior bare
    ``int(...)``: a real int (or bool, an int subclass) passes through; a
    numeric string parses; a float truncates. ``None`` (key absent) yields
    ``default``. A boolean, and a float the truncation changes, are noted
    as not read as written (`_trial_note`)."""
    if value is None:
        return default
    if isinstance(value, bool):
        _trial_note(f"{value!r} read as the integer {int(value)}")
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if int(value) != value:
            _trial_note(f"{value!r} truncated to {int(value)}")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(
                _malformed_config_msg(label, value, config_path, "an integer")
            ) from exc
    raise ValueError(_malformed_config_msg(label, value, config_path, "an integer"))


#: The smallest `[capture] checkpoint_tokens`: below it every attended
#: Stop would ask for a capture of an all-but-empty stretch, up to the
#: daily cap. `capture_hook` applies it to a config built in code too.
MIN_CHECKPOINT_TOKENS = 500


def _clamp_checkpoint_tokens(value: int, *, config_path: ConfigSource | None) -> int:
    """Raise `[capture] checkpoint_tokens` to `MIN_CHECKPOINT_TOKENS`,
    loudly, rather than reject it: by `default_max_results`' rule a bad
    value in one knob must not take the server down."""
    if value >= MIN_CHECKPOINT_TOKENS:
        return value
    _trial_note(f"checkpoint_tokens {value} clamped")
    import logging

    logging.getLogger("bettermemory.config").warning(
        "[capture] checkpoint_tokens = %d%s is below %d; using %d.",
        value,
        f" (in {config_path})" if config_path is not None else "",
        MIN_CHECKPOINT_TOKENS,
        MIN_CHECKPOINT_TOKENS,
    )
    return MIN_CHECKPOINT_TOKENS


#: The four modes `search.search` dispatches on. Duplicated from the
#: `SearchMode` Literal rather than imported because `config` is imported
#: by `search`, not the other way round; `test_config.py` cross-pins the
#: two so the copy cannot drift.
_SEARCH_MODES = ("keyword", "bm25", "hybrid")


def _coerce_search_mode(value: object, *, config_path: ConfigSource | None) -> str:
    """Normalise `[behavior] search_mode`, falling back to `hybrid`.

    Unlike the scalars above this does NOT raise, and unlike the old bare
    `str(...)` it does not pass the value through untouched. Both of those
    were wrong for this knob, because its consumers each made a different
    assumption about what it holds: `handlers.search.memory_search` passed
    `search_mode or "hybrid"` through unnormalised, `search.search` raises
    on anything outside the three literals, and the since-removed web UI
    silently ranked with its own hybrid fallback. Historically a config
    saying `search_mode = "Semantic"` loaded an embedding model, made
    EVERY `memory_search` call raise, and served a working lexical web
    page — three behaviours from one string. Normalising here makes every
    consumer agree. Falling back rather
    than raising follows `default_max_results`' rule that a bad value in
    one knob must not take the server down — and the fallback is loud.
    The legacy pre-4.0 value `"semantic"` lands here too: the lane was
    removed outright, so the warning below is that config's one
    migration notice.

    Scope, stated so it is not mistaken for a whole-system invariant:
    this runs in `load_config`, so it covers config FILES. A programmatic
    embedder building `BehaviorConfig(search_mode=...)` directly still
    reaches the consumers unnormalised. That is deliberate — these are
    value types and the loader is the policy layer — and it is why
    downstream consumers keep their own guards rather than trusting
    every constructor path was normalised.
    """
    if value is None:
        return "hybrid"
    normalised = str(value).strip().lower()
    if normalised in _SEARCH_MODES:
        return normalised
    _trial_note(f"search_mode {value!r} fell back")
    import logging

    logging.getLogger("bettermemory.config").warning(
        "[behavior] search_mode = %s is not one of %s%s; falling back to "
        '"hybrid" (keyword + BM25 fused). The pre-4.0 "semantic" mode was '
        "removed with the embedding lane — delete the line to silence "
        "this; nothing else will report it.",
        toml_spelling(value),
        ", ".join(_SEARCH_MODES),
        f" (in {config_path})" if config_path is not None else "",
    )
    return "hybrid"


PROMPT_RECALL_MODES: tuple[str, ...] = ("project", "everywhere", "off")


def _prompt_recall_spelling(value: object) -> str | None:
    """The mode a `[behavior] prompt_recall` value spells, or None when it
    spells none: a mode in any case and with any surrounding whitespace,
    or the legacy boolean (``true`` is ``"project"``, ``false`` ``"off"``),
    as a TOML boolean or one of the spellings `_coerce_bool` accepts in a
    string. Logs nothing: `_coerce_prompt_recall` warns about a value this
    reads as None, and the pinned-key notice (`_apply_managed`) asks what
    a user line it ignores unread would have said."""
    if isinstance(value, bool):
        return "project" if value else "off"
    if isinstance(value, str):
        token = value.strip().lower()
        if token in PROMPT_RECALL_MODES:
            return token
        if token in _TRUE_STRINGS:
            return "project"
        if token in _FALSE_STRINGS:
            return "off"
    return None


def _coerce_prompt_recall(value: object, *, config_path: ConfigSource | None) -> str:
    """Normalise `[behavior] prompt_recall` to one of `PROMPT_RECALL_MODES`.

    A config written before the recall surface took a mode carries a
    boolean: ``true`` reads as ``"project"`` (the delivery it now names)
    and ``false`` as ``"off"``, and the boolean spellings `_coerce_bool`
    accepts in a string read the same way. Anything else falls back to
    ``"project"`` with a warning, by `_coerce_search_mode`'s rule that a
    bad value in one knob must not take the server down, and that the
    fallback is loud."""
    if value is None:
        return "project"
    mode = _prompt_recall_spelling(value)
    if mode is not None:
        return mode
    _trial_note(f"prompt_recall {value!r} fell back")
    import logging

    logging.getLogger("bettermemory.config").warning(
        '[behavior] prompt_recall = %s is not one of %s%s; falling back to "project".',
        toml_spelling(value),
        ", ".join(PROMPT_RECALL_MODES),
        f" (in {config_path})" if config_path is not None else "",
    )
    return "project"


def _coerce_float(
    value: object, default: float, *, label: str, config_path: ConfigSource | None
) -> float:
    """Coerce a TOML value to ``float``, raising a located ValueError on a
    non-numeric value (cf. ``_coerce_int``). Valid-input behaviour matches
    the prior bare ``float(...)``; a boolean is noted as not read as
    written (`_trial_note`)."""
    if value is None:
        return default
    if isinstance(value, bool):
        _trial_note(f"{value!r} read as the number {float(value)}")
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
    value: object, *, label: str, config_path: ConfigSource | None
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


def _coerce_optional_str(
    value: object, *, label: str, config_path: ConfigSource | None
) -> str | None:
    """Require a TOML value to be a string when present (cf. ``_coerce_int``).

    ``[storage] directory`` was the one config key with no type check: a
    mistyped ``directory = 123`` (or ``true``, or ``["/a"]``) loaded
    cleanly and first failed deep inside ``Config.resolved_directory()``
    at ``Path(self.storage.directory)`` with a bare stdlib TypeError
    naming neither the key nor the file — crashing ``bettermemory serve``
    startup, and silently no-opping the Stop-hook audit lane (whose broad
    except swallows the traceback and exits 0). Reject non-strings at
    load time with the same located ``_malformed_config_msg`` error every
    other scalar key gets. ``None`` (key absent) passes through so the
    resolution rule in ``resolved_directory`` still fires. No coercion of
    other types to str: a quoted path is the only valid spelling."""
    if value is None or isinstance(value, str):
        return value
    raise ValueError(_malformed_config_msg(label, value, config_path, "a string path"))


def _coerce_switch(
    value: object, default: bool, *, label: str, config_path: ConfigSource | None
) -> bool:
    """`_coerce_bool` for a security switch: the same spellings, and a
    located error for anything else instead of the default.

    `_coerce_bool` falls back to the default on an unrecognised string,
    which is right for a ranking knob and wrong for a switch whose default
    is the loose side: `forbid_force = "ture"` would load cleanly and
    forbid nothing, and the operator who pinned it would never learn.
    The new `[policy]` switches and `[storage] cwd_store` read through
    this; the older keys keep `_coerce_bool`, which a config written for
    them may rely on."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
    raise ValueError(_malformed_config_msg(label, value, config_path, "true or false"))


def _coerce_acknowledge_names(
    value: object, *, label: str, config_path: ConfigSource | None
) -> list[str]:
    """`[policy] forbid_acknowledge`: a list of `ACKNOWLEDGE_NAMES`.

    An unknown name is a located load error, not an inert entry: a
    misspelt `"credentials"` would otherwise leave the flag it meant to
    forbid open while the file reads as if it were closed. A name written
    with the parameter's own prefix (`"acknowledge_credential"`) is
    refused with the bare name it should be."""
    names = _coerce_str_list(value, label=label, config_path=config_path)
    for name in names:
        if name in ACKNOWLEDGE_NAMES:
            continue
        expected = "one of " + ", ".join(ACKNOWLEDGE_NAMES)
        bare = name.removeprefix("acknowledge_")
        if bare != name and bare in ACKNOWLEDGE_NAMES:
            expected += f' (write "{bare}", without the acknowledge_ prefix)'
        raise ValueError(_malformed_config_msg(label, name, config_path, expected))
    return names


def default_config_path() -> Path:
    return Path(platformdirs.user_config_dir("bettermemory")) / CONFIG_FILENAME


# The one-shot guard of the config notices: (resolved config, key) pairs
# already reported. Once per process: a long-lived server that rereads
# config shouldn't spam the log on every reload, but two distinct config
# paths in the same process each get their own notice. The removed-key
# notice keys it `<key>+removed`.
_DEPRECATED_KEY_WARNED_PATHS: set[tuple[ConfigSource, str]] = set()


# [behavior] keys a major release removed after a deprecation cycle:
# key -> (the release that removed it, why). A config written for the
# earlier line can still carry the line, and the upgrade must not turn
# that into a failed load — the same rule `_coerce_search_mode` applies
# to a stale `search_mode = "semantic"`. The key is ignored and the
# operator is told once, since nothing else will ever report it.
_REMOVED_BEHAVIOR_KEYS: dict[str, tuple[str, str]] = {
    "corroboration_boost": (
        "8.0.0",
        "the ranking nudge it enabled read the `corroborations` rollup, "
        "which only bumps when a memory_write is dedup-rejected — a bar "
        "prose-sized memories do not reach, so the flag never changed a "
        "ranking (deprecated in 7.6.0). The rollup itself stays and "
        "still keeps corroborated memories out of dead-weight curation",
    ),
    "require_write_confirmation": (
        "9.0.0",
        "the staged-write flow left with the nine-tool surface",
    ),
    "rescue_expansion": (
        "9.0.0",
        "the expansion leg is measured by the retrieval bench and no "
        "longer a shipped knob",
    ),
    "endorsement_boost": (
        "9.0.0",
        "measured a wash on the owner's labelled replay and ruled out of 9.0",
    ),
    "outcome_demotion": (
        "9.0.0",
        "measured a wash on the owner's labelled replay and ruled out of 9.0",
    ),
    "standing_tier": (
        "9.0.0",
        "the standing section left with the session-start rewrite",
    ),
    "full_tool_surface": (
        "9.0.0",
        "the surface is nine tools, always",
    ),
    "curation_hint_threshold": (
        "9.0.0",
        "the one-shot curation hint on memory_write left with the nine-tool "
        "surface; `bettermemory health` carries the counts",
    ),
    "curation_hint_enabled": (
        "9.0.0",
        "the one-shot curation hint on memory_write left with the nine-tool "
        "surface; `bettermemory health` carries the counts",
    ),
    "recall_in_project": (
        "9.0.0",
        "the prompt recall delivers in the caller's project by default now; "
        '`prompt_recall = "everywhere"` widens it and `"off"` stops it',
    ),
    "endorsement_debt_ratio_threshold": (
        "9.0.0",
        "it was renamed `cold_endorsement_ratio_threshold` in 3.2.0 and read "
        "under that name until 9.0; set `cold_endorsement_ratio_threshold` "
        "to keep the threshold",
    ),
}

# `[telemetry]` keys removed the same way. `enabled` is the section's one
# remaining setting: the log no longer rotates, and queries are always
# redacted before they land in it.
_REMOVED_TELEMETRY_KEYS: dict[str, tuple[str, str]] = {
    "log_queries_verbatim": (
        "9.0.0",
        "queries are always redacted in the event log; there is no verbatim "
        "shape to restore",
    ),
    "max_bytes": (
        "9.0.0",
        "the event log no longer rotates",
    ),
}

# `[capture]` keys 8.x shipped that 9.0 does not read. `enabled` switched
# on a capture that called a model of its own; the 9.0 checkpoint asks the
# chat model instead and has its own key, so an 8.x `enabled = true` must
# not turn it on unseen.
_REMOVED_CAPTURE_KEYS: dict[str, tuple[str, str]] = {
    "enabled": (
        "9.0.0",
        "the 9.0 capture checkpoint asks the chat model at a turn end and is "
        "switched on by `stop_checkpoint`, which this line does not set",
    ),
    "idle_minutes": (
        "9.0.0",
        "capture no longer catches up sessions that went quiet",
    ),
}

# Whole sections a major removed: section -> (the release, why). Every key
# the section still carries is dropped with its own notice, so an operator
# who set three of them learns about all three in one load.
_REMOVED_SECTIONS: dict[str, tuple[str, str]] = {
    "consolidate": (
        "9.0.0",
        "unattended consolidation left with the nine-tool surface; "
        "`bettermemory health` carries the counts it acted on",
    ),
    "proposals": (
        "9.0.0",
        "the write-reflex proposal queue left with the nine-tool surface",
    ),
}


def _resolved_for_guard(config_path: ConfigSource) -> ConfigSource:
    """The path the one-shot warning guards key on: resolved so two
    `load_config` calls naming the same file via different paths share
    one warning, or the path as given when it cannot be resolved (deleted
    out from under us between the `open()` and here). A registry value has
    one name and nothing to resolve."""
    if isinstance(config_path, _managed.RegistryValue):
        return config_path
    try:
        return config_path.resolve()
    except OSError:
        return config_path


def _warn_removed_key(
    *, section: str, key: str, removed_in: str, why: str, resolved: ConfigSource
) -> None:
    """One notice per (config, section, key), on the log lane.

    Removed keys use the log lane, like the deprecation notices that
    preceded them — the operator who set the line reads server logs,
    not Python's warnings channel — with a one-shot `(resolved path,
    key)` guard (`_DEPRECATED_KEY_WARNED_PATHS`), so a long-lived server
    that rereads config on signal does not repeat itself. The `[behavior]` guard key keeps its historical shape
    (`<key>+removed`); the other sections carry their name in it.
    """
    guard_key = (
        resolved,
        f"{key}+removed" if section == "behavior" else f"{section}.{key}+removed",
    )
    if guard_key in _DEPRECATED_KEY_WARNED_PATHS:
        return
    _DEPRECATED_KEY_WARNED_PATHS.add(guard_key)
    import logging

    logging.getLogger("bettermemory.config").warning(
        "bettermemory: TOML config at %s sets [%s] `%s`, which "
        "was removed in bettermemory %s — %s. The line is ignored; "
        "delete it to silence this warning.",
        resolved,
        section,
        key,
        removed_in,
        why,
    )


def _drop_removed_keys(
    section_raw: dict[str, object],
    *,
    section: str,
    registry: dict[str, tuple[str, str]],
    config_path: ConfigSource,
) -> None:
    """Drop every removed key of one section, warning once per (config, key).

    Whatever value the line holds is discarded unread, so no spelling
    of it (`true`, `false`, a quoted string) can fail the load: the
    setting it named no longer exists to receive one.
    """
    present = [k for k in registry if k in section_raw]
    if not present:
        return
    resolved = _resolved_for_guard(config_path)
    for key in present:
        # Popped, so the dict the loader reads below holds only settings
        # that still exist.
        section_raw.pop(key)
        removed_in, why = registry[key]
        _warn_removed_key(
            section=section, key=key, removed_in=removed_in, why=why, resolved=resolved
        )


def _drop_removed_sections(data: dict[str, Any], config_path: ConfigSource) -> None:
    """Warn once per key of every removed section a config still carries.

    A removed section is never an error: the loader reads nothing from
    it, so every key it holds is ignored, and each one gets its own
    notice so the operator learns about all of them in one load.
    """
    present = [name for name in _REMOVED_SECTIONS if name in data]
    if not present:
        return
    resolved = _resolved_for_guard(config_path)
    for name in present:
        removed_in, why = _REMOVED_SECTIONS[name]
        section_raw = data.pop(name)
        keys = list(section_raw) if isinstance(section_raw, dict) else [""]
        for key in keys:
            _warn_removed_key(
                section=name, key=key, removed_in=removed_in, why=why, resolved=resolved
            )


def read_config_file(path: Path) -> dict[str, Any]:
    """One config file, parsed, with a parse failure that names the file.

    `tomllib`'s own error says only where in the text it stopped ("Invalid
    value (at line 3, column 9)"), and a file that is not UTF-8 fails with
    a bare codec error; neither names the file, so a daemon that would not
    start, or a CLI command that stopped, left the user to guess which of
    the config files was at fault. Both are re-raised here as a ValueError
    that starts ``malformed config in <path>:``, the prefix the loader's
    value errors carry (`_malformed_config_msg`).

    Decoded as ``utf-8-sig``, as `managed.read_managed` decodes the managed
    file: a leading byte-order mark, which Windows PowerShell 5.1 writes
    with ``-Encoding UTF8``, is dropped rather than refused as an invalid
    statement at line 1, column 1. Without a mark the decoding is plain
    UTF-8. An OSError (a file this account cannot read, a directory at the
    path) propagates unchanged; its text already names the path."""
    raw = path.read_bytes()
    try:
        return tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"malformed config in {path}: {exc}") from exc


def load_config(path: Path | None = None, *, create: bool = True) -> Config:
    """Load config from `path`, creating it with defaults if missing, and
    pin every key the organisation-managed file sets, when the machine has
    one (`managed.py`, `_apply_managed`).

    The file written on first run is DEFAULT_CONFIG, which sets no key:
    every setting in it is commented out, so a fresh install pins nothing
    and the code's defaults (`Config()`) apply, a later release's changed
    default included. A file that exists is read, never rewritten, so a
    config.toml an earlier release wrote, with every key spelled at that
    release's default, keeps the values it spells.

    With ``create=False`` nothing is written: a missing file reads as the
    defaults it would have been created with (the shipped DEFAULT_CONFIG
    sets no key), and the managed file is still applied over them. The
    read-only commands use it (`bettermemory config`), so a machine with a
    managed file and no config.toml reports the pins in force rather than
    the bare defaults."""
    config_path = path or default_config_path()
    if not create:
        # Missing means FileNotFoundError and nothing else. `Path.exists`
        # answers False on 3.14 for a file in a directory this account
        # cannot search (and raises on 3.11 to 3.13), and the first-run
        # write below fails on that file; reading it as "no file, the
        # defaults apply" would report a config the daemon cannot load as
        # one that loads.
        try:
            data: dict[str, Any] = read_config_file(config_path)
        except FileNotFoundError:
            data = {}
        return _overlay_managed(data, config_path)
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

    return _overlay_managed(read_config_file(config_path), config_path)


def _overlay_managed(data: dict[str, Any], config_path: Path) -> Config:
    """The `Config` for one parsed user file, with the managed config's
    pins applied when the machine has one."""
    # The organisation-managed config, a file or on Windows a registry
    # value, looked up through the module so the suite's autouse fixture
    # can point it at a scratch path. None (no file, no value: every
    # install that never deployed one) leaves the user's file the only
    # source, and the result is what it was before managed configs existed.
    managed_path = _managed.managed_config_source()
    managed_data = _managed.read_managed_source(managed_path)
    if managed_data is None:
        return _config_from_data(data, config_path)
    return _apply_managed(
        data,
        config_path=config_path,
        managed_data=managed_data,
        managed_path=managed_path,
    )


def _apply_managed(
    data: dict[str, Any],
    *,
    config_path: Path,
    managed_data: dict[str, Any],
    managed_path: _managed.ManagedSource,
) -> Config:
    """The user's config with every setting the managed file sets pinned.

    The managed file is parsed first, alone and through the same code as
    config.toml (`_config_from_data`), so its values get the loader's
    located errors naming the managed file and its removed keys their
    notices; what is left in `managed_data` afterwards names only settings that exist. A malformed
    value there fails the load, as a malformed config.toml does: a pin
    that cannot be read is never skipped.

    Every remaining key that names a setting of `Config` is pinned. The
    user's value for it is dropped BEFORE the user's file is parsed, so a
    pinned key the user mistyped cannot fail the load: it is ignored, not
    read. A user value the loader would not read as the pinned one gets
    one notice per key, naming both files (`_warn_pinned_key`), unless it
    is the code's own default for the key (`_restates_pin_or_default`): such a
    line restates the default and expresses no choice the pin overrides,
    and a config.toml an earlier release wrote spelled every key at its
    default, so every process, every hook among them, would warn about a
    line the user never chose. A key that release spelled in a form the
    loader now translates (``prompt_recall = true``) is compared as the
    loader reads it (`_LEGACY_SPELLINGS`). `bettermemory config check`
    and `config show` still report the line, from the files themselves
    rather than this log. Lists are replaced whole, never merged: a
    managed `[scopes] allowed` is the allowlist.

    A key the loader does not read is ignored here as it is in
    config.toml, and is not listed in `managed_keys`. The keys are found
    by walking the dataclasses rather than a list kept here, so a section
    a later release adds to `Config` is pinnable the moment it parses.

    `BETTERMEMORY_DIR` still decides the store before `[storage]
    directory` (resolution rule 1): the variable is the process's own
    choice, and the managed file binds the user's config, not a process
    running as the user.
    """
    pinned = _config_from_data(managed_data, managed_path)
    keys: list[tuple[str, str]] = []
    for section, section_raw in managed_data.items():
        # A section is an attribute holding a dataclass INSTANCE; the
        # class check keeps a table named after a dunder from reaching a
        # class object.
        target = getattr(pinned, section, None)
        if (
            not isinstance(section_raw, dict)
            or not is_dataclass(target)
            or isinstance(target, type)
        ):
            continue
        known = {f.name for f in fields(target)}
        keys.extend((section, key) for key in section_raw if key in known)

    defaults = Config()
    for section, key in keys:
        user_section = data.get(section)
        if isinstance(user_section, dict) and key in user_section:
            user_value = user_section.pop(key)
            if not _restates_pin_or_default(
                section,
                key,
                user_value,
                config_path=config_path,
                pinned=getattr(getattr(pinned, section), key),
                default=getattr(getattr(defaults, section), key),
            ):
                _warn_pinned_key(
                    section=section,
                    key=key,
                    config_path=config_path,
                    managed_path=managed_path,
                )

    config = _config_from_data(data, config_path)
    for section, key in keys:
        setattr(getattr(config, section), key, getattr(getattr(pinned, section), key))
    config.managed_path = managed_path
    config.managed_keys = frozenset(f"{section}.{key}" for section, key in keys)
    return config


#: The keys whose loader reads an earlier release's spelling as a current
#: value, each with what a raw value spells: the value the loader reads it
#: as, or None for one it does not recognise (which the loader replaces
#: with the default and warns about). `_restates_pin_or_default` compares
#: a pinned user line for such a key with the default as read, not as
#: written; against the pin every key is compared as read (`_reads_as_pin`).
#:
#: Found by loading every release's `DEFAULT_CONFIG`, v1.4.1 to 9.0.0rc12,
#: against `Config()`, and reading each normalisation `_config_from_data`
#: applies. `prompt_recall` is the one key a shipped config.toml spelled in
#: a form the loader now translates: v3.41.0 to v8.0.0 wrote ``true``, which
#: reads as ``"project"``, the default. Not here, on purpose:
#: `endorsement_debt_ratio_threshold`, renamed in 3.2.0, is a removed key
#: (`_REMOVED_BEHAVIOR_KEYS`), dropped unread; a pre-4.0
#: ``search_mode = "semantic"`` named a mode 4.0 removed, which the loader
#: replaces with the default through the fallback a typo takes, so it
#: spells no current value; ``search_mode = "keyword"``, which 2.x wrote,
#: is a mode of its own; and a boolean's string spellings (``"true"``) are
#: a coercion the user chose, compared with the default as written.
_LEGACY_SPELLINGS: dict[tuple[str, str], Callable[[object], object | None]] = {
    ("behavior", "prompt_recall"): _prompt_recall_spelling,
}


def _restates_pin_or_default(
    section: str,
    key: str,
    raw: object,
    *,
    config_path: ConfigSource,
    pinned: object,
    default: object,
) -> bool:
    """Whether a user's line for a pinned key expresses no choice the pin
    overrides: the loader reads it as the pinned value (`_reads_as_pin`),
    or it is the code's default (`_restates_default`).

    Against the pin the line is compared as the loader would load it, not
    as written. Compared as written, Python's ``1 == True`` took
    ``strict_provenance = 1`` under a pinned ``true`` for the managed
    file's own line, though the loader reads it as the default ``false``,
    and ``forbid_force = 1`` too, though the loader refuses it; and
    ``"true"`` under a pinned ``true``, which loads as the pin, drew the
    notice.

    Against the default, a key with a legacy spelling (`_LEGACY_SPELLINGS`)
    is compared as the loader reads it: ``prompt_recall = true``, in every
    config.toml v3.41.0 to v8.0.0 wrote, reads as ``"project"`` and
    restates the default. A spelling that reads as another value is a
    choice, and so is one the reader does not recognise: the line is
    dropped unread, so the notice is the only word a mistyped value gets.
    Neither reading logs anything, so the coercion's own fallback warning
    is not raised for a line that is never read."""
    if _reads_as_pin(section, key, raw, config_path=config_path, pinned=pinned):
        return True
    spelling = _LEGACY_SPELLINGS.get((section, key))
    if spelling is not None:
        read = spelling(raw)
        return read is not None and read == default
    return _restates_default(raw, default)


def _reads_as_pin(
    section: str, key: str, raw: object, *, config_path: ConfigSource, pinned: object
) -> bool:
    """Whether the loader, reading a user's line for a pinned key alone,
    takes it as written and loads the pinned value (`pinned`, the managed
    line as loaded).

    One the loader refuses (``forbid_force = 1``, ``port = "seven"``,
    ``port = inf``), or reads only through a fallback, a clamp or a lossy
    coercion (``enabled = 1``, which a boolean reads as its default;
    ``search_mode = "semantic"``; ``checkpoint_tokens = 100``; ``port =
    7400.9``), is not read as written and restates no pin, even where what
    it lands on is the pinned value."""
    try:
        read, notes = _read_alone(section, key, raw, config_path)
    except Exception:  # noqa: BLE001 - every refusal reads as not the pin
        return False
    return not notes and read == pinned


def _read_alone(
    section: str, key: str, raw: object, config_path: ConfigSource
) -> tuple[object, list[str]]:
    """What the loader reads `raw` as for ``[section] key``, parsed through
    the loader itself (`_config_from_data`) in a table of its own, and
    every way it did not take the line as written (`_TRIAL`). Raises what
    the loader raises. Logs nothing (`_trial_filter`), and a table holding
    one current key of a known section reaches none of the one-shot
    notices, so their guards are untouched."""
    import logging

    logging.getLogger("bettermemory.config").addFilter(_trial_filter)
    notes: list[str] = []
    token = _TRIAL.set(notes)
    try:
        read = _config_from_data({section: {key: raw}}, config_path)
    finally:
        _TRIAL.reset(token)
    return getattr(getattr(read, section), key), notes


def line_reads_as_pin(
    section: str,
    key: str,
    user_raw: object,
    managed_raw: object,
    *,
    config_path: ConfigSource,
    managed_path: ConfigSource,
) -> bool:
    """Whether a user's config.toml line for a key the managed config pins
    reads as the pin, compared as the loader's notice compares it
    (`_reads_as_pin`), for `bettermemory config check` (a `pinned` note on
    every line that does not) and `config show` (the user's value beside
    the pinned one). The managed line is read alone as the loader reads
    it; one the loader refuses is compared as written, type and all,
    since the managed config then does not load, which `config check`
    reports itself."""
    try:
        pinned, _ = _read_alone(section, key, managed_raw, managed_path)
    except Exception:  # noqa: BLE001 - the whole load reports the managed line
        return type(user_raw) is type(managed_raw) and user_raw == managed_raw
    return _reads_as_pin(section, key, user_raw, config_path=config_path, pinned=pinned)


def _restates_default(raw: object, default: object) -> bool:
    """Whether `raw`, a user's line for a pinned key as TOML parsed it, is
    the code's default for that key (`default`, the field of `Config()`).

    Compared as written, before any coercion, for every key without a
    legacy spelling (`_restates_pin_or_default` reads those first):
    ``30`` restates a default of ``30.0`` and ``[]`` an empty list, but
    ``"true"`` does not restate ``True``, since the loader would read that
    spelling through a coercion the user chose. A boolean only restates a
    boolean: Python takes ``False == 0``, but
    ``tombstone_retention_days = false`` restates no default of 0."""
    if isinstance(raw, bool) or isinstance(default, bool):
        return type(raw) is type(default) and raw == default
    return raw == default


def _warn_pinned_key(
    *, section: str, key: str, config_path: Path, managed_path: _managed.ManagedSource
) -> None:
    """One notice per (config, managed file, key) that the user's value
    is ignored, on the log lane and with the one-shot guard the removed-key
    notices use, so a daemon that rereads its config does not repeat it."""
    resolved = _resolved_for_guard(config_path)
    guard_key = (resolved, f"{section}.{key}+managed:{managed_path}")
    if guard_key in _DEPRECATED_KEY_WARNED_PATHS:
        return
    _DEPRECATED_KEY_WARNED_PATHS.add(guard_key)
    import logging

    logging.getLogger("bettermemory.config").warning(
        "bettermemory: TOML config at %s sets [%s] `%s`, which the managed "
        "config at %s pins. The managed value is in force and this line is "
        "ignored; delete it to silence this warning.",
        resolved,
        section,
        key,
        managed_path,
    )


# Sections rc11 read as their defaults when the file wrote them as a scalar
# (`daemon = 5`). They still do, with a warning, so a config that ran the
# hooks under rc11 still runs them; every other section refuses the line
# (`_section_table`).
_SCALAR_TOLERATED_SECTIONS = frozenset({"daemon", "capture", "membership"})


def _section_table(
    data: dict[str, Any], name: str, config_path: ConfigSource
) -> dict[str, Any]:
    """The `[name]` table of one parsed file: ``{}`` when the file has no
    such section, the table itself (mutated in place by the steps below)
    when it has one, and a located ValueError naming the file and the
    section when the name holds anything else (``storage = 5``).

    Before this guard the sections went three ways. `[storage]` and
    `[scopes]` crashed with an AttributeError from ``5.get(...)``, and
    `[behavior]` and `[telemetry]` with a TypeError, which the commands
    that catch a bad config as ``(ValueError, OSError)`` (`security
    audit`, `security sandbox`) let through as a traceback.
    `[daemon]`, `[capture]` and `[membership]` read as their defaults
    without a word. Those three still read as their defaults, so a config
    the hooks ran on before still runs them, and now log a warning that
    names the file and the section. Every other section refuses, as the
    managed file refuses any top-level value that is not a table
    (`managed.read_managed`): `[security]` and `[policy]` are new, and
    ``security = true``, written by someone who meant to switch a
    protection on, must not leave it off. `bettermemory config check`
    reports every such line as an error before a daemon starts on it."""
    raw = data.get(name, {})
    if isinstance(raw, dict):
        return raw
    if name not in _SCALAR_TOLERATED_SECTIONS:
        raise ValueError(
            _malformed_config_msg(f"[{name}]", raw, config_path, "a table")
        )
    resolved = _resolved_for_guard(config_path)
    guard_key = (resolved, f"[{name}] scalar")
    if guard_key not in _DEPRECATED_KEY_WARNED_PATHS:
        _DEPRECATED_KEY_WARNED_PATHS.add(guard_key)
        import logging

        logging.getLogger("bettermemory.config").warning(
            "bettermemory: TOML config at %s writes `%s = %s` where a [%s] "
            "section belongs. The line is ignored and the section's defaults "
            "are in force; write its settings under a [%s] header.",
            resolved,
            name,
            toml_spelling(raw),
            name,
            name,
        )
    return {}


def _config_from_data(data: dict[str, Any], config_path: ConfigSource) -> Config:
    """Build a `Config` from one parsed TOML file.

    `config_path` locates every error and notice: the user's config.toml,
    or the managed file when `_apply_managed` parses that one. Mutates
    `data` as it reads it: removed keys and sections are dropped, so a
    caller reading `data` afterwards sees only settings that still exist.
    """
    from .config_sections import (
        _behavior_config,
        _capture_config,
        _policy_config,
        _scopes_config,
        _storage_config,
    )

    storage_raw = _section_table(data, "storage", config_path)
    behavior_raw = _section_table(data, "behavior", config_path)
    scopes_raw = _section_table(data, "scopes", config_path)
    policy_raw = _section_table(data, "policy", config_path)
    telemetry_raw = _section_table(data, "telemetry", config_path)
    daemon_raw = _section_table(data, "daemon", config_path)
    capture_raw = _section_table(data, "capture", config_path)
    membership_raw = _section_table(data, "membership", config_path)
    security_raw = _section_table(data, "security", config_path)

    # Keys and sections a major removed: ignored with a one-time notice
    # per key, so a config written for the previous line still loads.
    _drop_removed_keys(
        behavior_raw,
        section="behavior",
        registry=_REMOVED_BEHAVIOR_KEYS,
        config_path=config_path,
    )
    _drop_removed_keys(
        telemetry_raw,
        section="telemetry",
        registry=_REMOVED_TELEMETRY_KEYS,
        config_path=config_path,
    )
    _drop_removed_keys(
        capture_raw,
        section="capture",
        registry=_REMOVED_CAPTURE_KEYS,
        config_path=config_path,
    )
    _drop_removed_sections(data, config_path)

    return Config(
        storage=_storage_config(storage_raw, config_path),
        behavior=_behavior_config(behavior_raw, config_path),
        scopes=_scopes_config(scopes_raw, config_path),
        policy=_policy_config(policy_raw, config_path),
        telemetry=TelemetryConfig(
            enabled=_coerce_bool(telemetry_raw.get("enabled"), True),
        ),
        daemon=DaemonConfig(
            port=_coerce_int(
                daemon_raw.get("port"),
                7397,
                label="[daemon] port",
                config_path=config_path,
            ),
        ),
        capture=_capture_config(capture_raw, config_path),
        membership=MembershipConfig(
            write_token=_coerce_bool(membership_raw.get("write_token"), True),
            strict=_coerce_bool(membership_raw.get("strict"), False),
        ),
        security=SecurityConfig(
            strict_provenance=_coerce_bool(
                security_raw.get("strict_provenance"), False
            ),
        ),
        # The managed config's own parse (`_apply_managed`) can name a
        # registry value, which is no file's path.
        config_path=config_path if isinstance(config_path, Path) else None,
    )
