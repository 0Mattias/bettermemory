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
from typing import Any

import platformdirs

from ._fsutil import atomic_write_bytes


CONFIG_FILENAME = "config.toml"
PROJECT_DIR_NAME = ".claude-memory"
GLOBAL_DIR_NAME = ".claude-memory"
ENV_DIR_OVERRIDE = "BETTERMEMORY_DIR"
# Where the store's key and head checkpoint live, ahead of the user config
# directory. A scratch store (a bench, a demo, a test that spawns the
# server) sets it so it leaves no key behind.
KEYS_DIR_ENV = "BETTERMEMORY_KEYS_DIR"

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
# Default cap on memory_search results. Clamped to 1..50 where it is read
# (the same range an explicit max_results is clamped to), not rejected
# here: a value outside the range is a harmless typo, and erroring at load
# would take the whole server down over one knob.
default_max_results = 5

# Recency boost decay. Larger = older memories get a meaningful bump.
recency_boost_half_life_days = 30

# Retrieval ranker for memory_search. One of:
#   "keyword" — the original TF + coverage + recency scorer (legacy default
#       in 1.6.0). No IDF weighting, so rare-term queries underperform.
#   "bm25"    — Okapi BM25 with the same scope-bonus + recency boost
#   "hybrid"  — reciprocal-rank-fusion of keyword + BM25. Default since
#       2.6.8; a strict improvement over either alone. Every mode is
#       deterministic code — the project ships no embedding models
#       (removed in 4.0.0). The MCP `mode` parameter on memory_search
#       overrides this per-call.
search_mode = "hybrid"

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
conversational = true

# Write-time supersession. When a claim-sized memory_write carries a
# change cue (moved, switched, renamed, raised, no longer, the previous,
# ...) and diverges on a value from a stored claim about the same
# subject, the new memory gets a `supersedes` link to the stored one,
# which memory_search renders as `superseded_by` on the stale hit and
# on nothing else. The same divergence with no cue files the pair for
# memory_conflicts instead of guessing which side is current. Measured
# on bench/integrity's sealed corpus: 27 of the 40 update statements
# link to the statement they replace and nothing links a distractor,
# a hard negative or a cross-topic pair (docs/eval-results.md). Set
# false to leave links to the writer's explicit `supersedes=` list.
write_supersession = true

# Score-gated recall at prompt time. The plugin's UserPromptSubmit hook
# probes every submitted prompt with the SAME predicate the Stop hook's
# silent-miss audit uses (same pool, ranker, threshold, shields); where
# the audit would later have flagged "the model should have searched",
# the hook instead injects the top hit's id + snippet into context
# before the turn starts — a pointer, never a body, so the read path's
# verify-before-relying discipline still runs through memory_show. The
# predicate fires on ~2% of audited turns (docs/eval-results.md), which
# is what keeps generic answers unpolluted. A delivered recall is
# recorded as a `prompt_recall` event and counts as retrieval for the
# audit's shield, so it also suppresses further injections for the
# ~10-minute attribution window. Needs telemetry.enabled (off-the-books
# injections are refused). Set false to return to purely opt-in
# retrieval.
prompt_recall = true

# Deliver recall inside the caller's own project. The silent-miss audit
# deliberately declines to FLAG a turn whose top hit belongs to the git
# project the caller is standing in ("update <repo>", "push it" — no
# memory_search was owed with the source tree open). Delivery answers a
# different question: memory-resident facts (decisions, run state,
# rulings) are exactly what the source tree cannot serve, and measured
# dogfood put ~95% of replayable misses in this cohort. With this on
# (default), the prompt-recall hook also injects on those turns; the v1
# "high" bar, the retrieval shield, and the attribution-window anti-spam
# bound still apply unchanged. The audit lane is unaffected either way.
# Set false to restore the strict fires-only-where-the-audit-would-flag
# coupling.
recall_in_project = true

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

# Opt-in floor on a memory body's whitespace-separated token count at
# `memory_write` time. 0 (the default) disables it: the only shipped lower
# bound is "non-empty after stripping", so a one-word body still commits —
# which is correct for a caller storing an identifier, a path, or a version
# pin. Set it (5-8 is a reasonable band for a self-contained statement) when
# writes arrive from unattended or bulk callers, where a fragment costs a
# durable record plus the curation pass that later removes it. Enabling it
# also binds `bettermemory proposals accept` / `memory_proposals`, which
# share the write validator; `memory_update` does NOT, so a body edit can
# still take an existing memory below the floor. The default is revisited
# at 4.0.
min_content_tokens = 0

[scopes]
# If non-empty, writes with caller-supplied scopes outside this list fail.
# Empty = anything. One narrow exemption, and it is not a property of
# the scopes themselves: `memory_update`, whose `scopes` argument REPLACES
# the stored list. Keeping a scope means resubmitting it, so the check
# runs over what an edit ADDS. A scope already on the record passes
# because it was already accepted; one that is not is still checked by
# name, so the exemption cannot be borrowed to plant an unallowed scope.
allowed = []

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
typo_exceptions = []

[telemetry]
# Append-only JSONL event log at <storage>/.events.jsonl. One line per tool
# call: search queries, returned IDs, write/update/remove events. Used by the
# memory_health view, by use-recording feedback, and to tune the durability
# marker list against real traffic. Lives next to the memories — same trust
# boundary, no new permissions story. Set `enabled = false` to opt out.
enabled = true
"""


@dataclass
class StorageConfig:
    directory: str | None = None  # if None, use resolution rule.


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
    # Score-gated recall at prompt time (`hook.run_prompt_recall`,
    # wired to the plugin's UserPromptSubmit hook). Default ON — the
    # delivery is gated by the silent-miss predicate itself (v1 top-1
    # "high" plus its shields, ~2% of audited turns), which is the
    # don't-pollute-generic-answers stance carried by a bar instead of
    # by opt-in. Requires `telemetry.enabled` at the call site: an
    # unlogged injection is invisible to the Stop-hook shield and
    # unmeasurable, so the recall path refuses to fire rather than
    # fire off the books. See DEFAULT_CONFIG for prose.
    prompt_recall: bool = True
    # Deliver prompt recall on the project cohort (`hook.run_prompt_recall`):
    # when the caller works inside the git project the top hit was written
    # from, the silent-miss AUDIT deliberately reports "ok" (no search was
    # owed — the source tree is open), but the audit's premise does not
    # transfer to DELIVERY: memory-resident facts (decisions, run state,
    # rulings) are precisely what a source tree cannot serve, and dogfood
    # measured ~95% of replayable misses in this cohort. Default ON — the
    # v1 top-1 "high" bar, the retrieval shield, and the attribution-window
    # anti-spam bound all still gate the injection. Set false to restore
    # the strict fires-only-where-the-audit-would-flag coupling.
    recall_in_project: bool = True
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
    # Opt-in floor on a memory body's whitespace-token count at write time.
    # 0 (default, and the loader default too — this is NOT one of the
    # deliberate asymmetries) leaves the shipped "non-empty after strip"
    # bound, so a one-word body commits exactly as it always has. Enabling
    # it binds every caller of `_validate_write_payload` — memory_write and
    # accept_proposal — but not `memory_update`, which size-checks a
    # replacement body without routing through that validator. See
    # DEFAULT_CONFIG for the recommendation and the 4.0 revisit.
    min_content_tokens: int = 0


@dataclass
class ScopesConfig:
    allowed: list[str] = field(default_factory=list)
    # Singleton scopes that `memory_health`'s `fix_typo_scopes` check must
    # stop flagging. See DEFAULT_CONFIG for why this exists.
    typo_exceptions: list[str] = field(default_factory=list)


@dataclass
class TelemetryConfig:
    """Event-log toggles. See DEFAULT_CONFIG for prose."""

    enabled: bool = True


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


#: The four modes `search.search` dispatches on. Duplicated from the
#: `SearchMode` Literal rather than imported because `config` is imported
#: by `search`, not the other way round; `test_config.py` cross-pins the
#: two so the copy cannot drift.
_SEARCH_MODES = ("keyword", "bm25", "hybrid")


def _coerce_search_mode(value: object, *, config_path: Path | None) -> str:
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
    import logging

    logging.getLogger("bettermemory.config").warning(
        "[behavior] search_mode = %r is not one of %s%s; falling back to "
        "'hybrid' (keyword + BM25 fused). The pre-4.0 'semantic' mode was "
        "removed with the embedding lane — delete the line to silence "
        "this; nothing else will report it.",
        value,
        ", ".join(_SEARCH_MODES),
        f" (in {config_path})" if config_path is not None else "",
    )
    return "hybrid"


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


def _coerce_optional_str(
    value: object, *, label: str, config_path: Path | None
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
# 3.1.x user has seen the deprecation warning at least once. The
# removed-key notice below shares the set, keyed `<key>+removed`.
_DEPRECATED_KEY_WARNED_PATHS: set[tuple[Path, str]] = set()


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
    "capture": (
        "9.0.0",
        "session capture from the hooks left with the nine-tool surface",
    ),
}


def _resolved_for_guard(config_path: Path) -> Path:
    """The path the one-shot warning guards key on: resolved so two
    `load_config` calls naming the same file via different paths share
    one warning, or the path as given when it cannot be resolved (deleted
    out from under us between the `open()` and here)."""
    try:
        return config_path.resolve()
    except OSError:
        return config_path


def _warn_removed_key(
    *, section: str, key: str, removed_in: str, why: str, resolved: Path
) -> None:
    """One notice per (config, section, key), on the log lane.

    Removed keys use the log lane, like the deprecation notices that
    preceded them — the operator who set the line reads server logs,
    not Python's warnings channel — with the same one-shot `(resolved
    path, key)` guard as `_apply_legacy_endorsement_debt_alias`, so a
    long-lived server that rereads config on signal does not repeat
    itself. The `[behavior]` guard key keeps its historical shape
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
    config_path: Path,
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
        # Popped, as the legacy alias pops its stale key, so the dict the
        # loader reads below holds only settings that still exist.
        section_raw.pop(key)
        removed_in, why = registry[key]
        _warn_removed_key(
            section=section, key=key, removed_in=removed_in, why=why, resolved=resolved
        )


def _drop_removed_sections(data: dict[str, Any], config_path: Path) -> None:
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

    # T9: back-compat for the 3.1.x -> 3.2.0 TOML key rename. Mutates
    # `behavior_raw` so the downstream `behavior_raw.get(...)` lookups
    # below pick up the legacy value under the new key.
    _apply_legacy_endorsement_debt_alias(behavior_raw, config_path)

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
    _drop_removed_sections(data, config_path)

    return Config(
        storage=StorageConfig(
            directory=_coerce_optional_str(
                storage_raw.get("directory"),
                label="[storage] directory",
                config_path=config_path,
            )
        ),
        behavior=BehaviorConfig(
            default_max_results=_coerce_int(
                behavior_raw.get("default_max_results"),
                5,
                label="[behavior] default_max_results",
                config_path=config_path,
            ),
            search_mode=_coerce_search_mode(
                behavior_raw.get("search_mode"), config_path=config_path
            ),
            conversational=_coerce_bool(behavior_raw.get("conversational"), True),
            recency_boost_half_life_days=_coerce_float(
                behavior_raw.get("recency_boost_half_life_days"),
                30.0,
                label="[behavior] recency_boost_half_life_days",
                config_path=config_path,
            ),
            write_supersession=_coerce_bool(
                behavior_raw.get("write_supersession"), True
            ),
            prompt_recall=_coerce_bool(behavior_raw.get("prompt_recall"), True),
            recall_in_project=_coerce_bool(behavior_raw.get("recall_in_project"), True),
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
            min_content_tokens=_coerce_int(
                behavior_raw.get("min_content_tokens"),
                0,
                label="[behavior] min_content_tokens",
                config_path=config_path,
            ),
        ),
        scopes=ScopesConfig(
            allowed=_coerce_str_list(
                scopes_raw.get("allowed"),
                label="[scopes] allowed",
                config_path=config_path,
            ),
            typo_exceptions=_coerce_str_list(
                scopes_raw.get("typo_exceptions"),
                label="[scopes] typo_exceptions",
                config_path=config_path,
            ),
        ),
        telemetry=TelemetryConfig(
            enabled=_coerce_bool(telemetry_raw.get("enabled"), True),
        ),
        config_path=config_path,
    )
