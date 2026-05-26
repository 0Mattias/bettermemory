"""Staleness signals for retrieved memories.

Three concepts live here, all surfaced on retrieval so a consuming model
can self-triage before relying on stored content:

1. **Path drift** (`detect_path_drift`, `PathDriftReport`). A memory body
   that names a file path is making a verifiable claim — "this path
   exists on disk and is the thing I'm describing." Filesystems move on,
   projects reorganise, scripts get renamed — claims like that go stale
   long before the memory's `updated` timestamp would suggest. We catch
   the easy cases by extracting path-shaped tokens and stat'ing them.

2. **Verification staleness** (`compute_verification_status`,
   `VerificationStatus`). A memory's `last_verified_at` timestamp records
   the last time its claims were spot-checked against ground truth.
   Null means never verified; an old timestamp means the world may have
   moved on since. Path drift is per-claim; verification staleness is
   the umbrella signal — useful for facts whose claims aren't filesystem
   paths (commit hashes, version numbers, tool lists, configurations).

3. **Commit drift** (`compute_commit_drift`, `CommitDriftStatus`). The
   calendar staleness in (2) doesn't notice when a project moves faster
   than the user re-verifies. A memory whose `last_verified_at` is "two
   hours ago" reads as fresh while the repo it describes can sit six
   commits ahead of HEAD. When the caller is currently inside a checkout
   of the same repo the memory was written from, we count commits in
   that repo since `last_verified_at` and surface a `commit_drift`
   advisory alongside `verification`. Cwd-aware by design: if the user
   is not in the matching project, the signal stays silent rather than
   guessing about a remote we have no checkout for.

All three are advisory. They never block; they shape a structured payload
that the retrieval surface (memory_show, memory_search, memory_list)
attaches to its responses, so the model receives a recommendation as a
first-class field rather than having to do timestamp arithmetic on a raw
datetime. The cost of a false positive (a small extra prompt asking the
model to spot-check) is much lower than the cost of a false negative (the
model treats a stale fact as ground truth).

Detection coverage for path drift is deliberately conservative — better
to miss a real path than chase ghosts:

- Backtick-wrapped paths: ```/etc/foo``` or ```~/Downloads```.
  Highest precision because the author chose to set the path off as code.
- Bare absolute Unix paths: ``/etc/foo``, ``~/Downloads/x.txt``.
- Bare Windows paths: ``C:\\Users\\me``, ``D:/data``.

Excluded by design:
- Relative paths (``docs/installation.md``) — too many false positives in
  prose, and without an anchor we'd be checking the cwd at retrieval time
  which is meaningless.
- URLs (``https://...``, ``git://...``, ``ssh://...``) — they have ``/``
  but aren't filesystem paths.
- SSH remotes (``user@host:path``) — colon-prefixed paths would otherwise
  parse as bare paths; filtered via an ``@`` check on the leading run.
- Paths shorter than 3 characters (``/x``) — the false-positive rate at
  that length is too high (``/`` alone, ``/n`` from prose, etc.).
- CLI / slash-command invocations (``/plugin install foo``,
  ``/usr/bin/env python -m bettermemory``) — a slash-prefixed token
  followed by space-delimited arguments looks path-shaped to the
  extractor but maps to no file on disk. Distinguished from a real path
  with internal spaces (rare but legal: ``/Users/Some User/x``) by
  counting slashes in the first whitespace-separated chunk: a true path
  crosses directory boundaries and so contains multiple slashes there,
  while a command name is a single ``/word`` chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .origin import (
    Origin,
    commits_since,
    commits_since_touching_paths,
    repos_match,
)


# ---------------------------------------------------------------------------
# Caps — bound the work we do per memory
# ---------------------------------------------------------------------------
#
# A pathological body (a directory listing pasted into a memory) could
# otherwise produce hundreds of stat() calls per retrieval. The limits here
# cap the worst case at a few stat()s while still covering normal usage —
# real memories rarely cite more than 2-3 paths.

_MAX_PATHS_PER_BODY = 8
_MAX_PATH_LENGTH = 512


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
#
# Two passes: backtick-wrapped first (high-precision, the author marked it as
# code), then bare paths from what's left. Order matters because we mask out
# the backtick contents before the bare scan to avoid double-counting the
# same path under both rules.

# Anything between non-newline backticks. We require a non-empty inner span
# (`[^`\n]+`) so empty `` `` `` doesn't match. Multi-line code fences aren't
# part of memory bodies in practice — this would over-match if they were.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Bare path: starts at a "word boundary" (start-of-string or one of a small
# set of punctuation/whitespace chars) with a path-y prefix, then runs
# through path-friendly characters. The boundary character is consumed by
# the non-capturing group so the captured group is only the path itself —
# `match.start(1)` gives the path's true offset.
_BARE_RE = re.compile(
    r"(?:^|[\s(\[{<\"',;])"
    r"((?:~/|/|[a-zA-Z]:[\\/])[\w./\-_~\\]+)"
)

# Trailing punctuation that's almost never part of a real path. We strip
# these from the right edge of a candidate before validating. `~` is in
# the path-body class so we don't strip it.
_TRAILING_PUNCT = ".,;:!?)>]}\"'"


# Documentation-placeholder paths that authors use in prose to demonstrate
# path-shape without citing a real file. Treating these as drift candidates
# produces a phantom `path_drift_missing` entry on every memory whose body
# documents a path-typed API ("a memory verified for `/etc/foo` reads as
# clean…"). The list is deliberately narrow — broader filters
# (terminal-component `foo` / `bar`) overlap with legitimate tmp-path
# test fixtures and would suppress real drift signals there. We trade a
# negligible false-negative risk (a real `/etc/foo` script existing on
# someone's machine) for fixing the reliable false-positive on
# documentation prose.
_PLACEHOLDER_PATHS = frozenset(
    {
        "/etc/foo",
        "/etc/bar",
        "/etc/baz",
        "/foo",
        "/foo/bar",
        "/foo/baz",
        "/foo/bar/baz",
        "/path/to",
    }
)

# Anything under these prefixes is also a placeholder. `/path/to/...` is
# the universal placeholder convention; the home-relative variant catches
# `~/path/to/...` for the same reason.
_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "/path/to/",
    "~/path/to/",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathDriftReport:
    """Result of a path-drift scan.

    `checked` is the deduplicated set of paths the scanner extracted and
    actually attempted to stat. `missing` is the subset whose `exists()`
    returned False (or raised, suppressed). Empty `missing` with non-empty
    `checked` means the memory's path claims look healthy as of right now.
    Both empty means no path-shaped tokens were found in the body — the
    memory makes no checkable filesystem claims.

    `verified` is the subset of `checked` that the caller previously
    attested via `memory_verify(verified_paths=[...])` AND that still
    exists on disk. Membership in `verified` does NOT exempt a path
    from `missing` if it has since disappeared — verified-then-deleted
    is a real drift signal — but it lets downstream logic distinguish
    "the body cites a path that exists" from "the body cites a path
    the user spot-checked and which still exists." Empty when the
    caller passed no `verified_paths` or none of them appeared in the
    body's candidate set.
    """

    checked: tuple[str, ...]
    missing: tuple[str, ...]
    verified: tuple[str, ...] = ()

    @property
    def has_drift(self) -> bool:
        return bool(self.missing)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "checked": list(self.checked),
            "missing": list(self.missing),
            "verified": list(self.verified),
        }


def detect_path_drift(
    body: str,
    *,
    verified_paths: tuple[str, ...] | list[str] = (),
) -> PathDriftReport:
    """Extract path-shaped tokens from `body` and check them on disk.

    Returns an empty report when the body is empty, contains no path
    candidates, or every candidate failed validation. Existence checks
    swallow OSError (permission denied, ELOOP, etc.) and treat them as
    "missing" — the caller should treat a `missing` entry as "the path
    couldn't be confirmed", not as "definitely deleted". The semantic
    difference doesn't matter for the staleness signal.

    Order in `checked` and `missing` is deterministic: paths appear in
    the order they were first encountered in the body, which makes the
    report stable for snapshot tests.

    `verified_paths` is an optional set of paths the caller has
    previously attested via `memory_verify`. When a candidate from the
    body is in that set AND still exists, it lands in `report.verified`
    (in addition to the usual `checked` slot). A verified path that's
    since disappeared still lands in `missing` — verification doesn't
    paper over deletion.
    """
    candidates = _extract_candidates(body)
    if not candidates:
        return PathDriftReport(checked=(), missing=(), verified=())

    # Run verified_paths through the same trim/validate pipeline the body
    # candidates pass through (`_normalize_candidate`) before the
    # `_normalize_for_compare` pass that handles `~`-expansion. Without
    # this, an attestation like `/etc/foo.conf,` (extracted from prose
    # with a trailing comma) would not match the body candidate
    # `/etc/foo.conf` (already trimmed) — the audit flagged the
    # asymmetry between the two normalisation paths. We keep the
    # validator's `None` rejection in the same step so `verified_paths`
    # can't introduce shapes the extractor would itself have dropped.
    verified_set: set[str] = set()
    for raw in verified_paths:
        if not raw:
            continue
        candidate = _normalize_candidate(raw)
        if candidate is None:
            continue
        verified_set.add(_normalize_for_compare(candidate))
    verified_set.discard("")

    checked: list[str] = []
    missing: list[str] = []
    verified: list[str] = []
    for path in candidates:
        checked.append(path)
        exists = _path_exists(path)
        if not exists:
            missing.append(path)
            continue
        if _normalize_for_compare(path) in verified_set:
            verified.append(path)
    return PathDriftReport(
        checked=tuple(checked),
        missing=tuple(missing),
        verified=tuple(verified),
    )


def _normalize_for_compare(raw: str) -> str:
    """Normalise a path for set membership across `verified_paths` and the
    body's extracted candidates.

    Both sides may carry ``~/``-prefixed or absolute forms; we expand
    ``~`` to the user's home before comparing. We do NOT call
    ``Path.resolve()`` — that would follow symlinks and require the
    path to exist, which a user-attested verified path is allowed not
    to (it could have been verified from a different machine; its
    existence is the responsibility of the disk check that runs
    separately).
    """
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser())
    except (OSError, ValueError):
        return raw


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_candidates(body: str) -> list[str]:
    """Pull validated, deduplicated path candidates from `body`.

    Capped at `_MAX_PATHS_PER_BODY`. Backtick-wrapped matches come first;
    when the same path appears both backtick-wrapped and bare, only the
    backtick form is kept (the bare scan operates on a body with backtick
    contents masked out).
    """
    if not body:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    masked = body
    for match in _BACKTICK_RE.finditer(body):
        raw = match.group(1).strip()
        normalized = _normalize_candidate(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
            if len(candidates) >= _MAX_PATHS_PER_BODY:
                return candidates
        # Mask the backtick span (including the delimiters) so the bare
        # scan doesn't see it again. Replace with spaces of the same
        # length to preserve offsets — not strictly required since we
        # don't use offsets after this, but it keeps debug-printing the
        # masked body honest.
        span = match.span()
        masked = masked[: span[0]] + " " * (span[1] - span[0]) + masked[span[1] :]

    for match in _BARE_RE.finditer(masked):
        raw = match.group(1)
        normalized = _normalize_candidate(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
            if len(candidates) >= _MAX_PATHS_PER_BODY:
                break

    return candidates


def _normalize_candidate(raw: str) -> str | None:
    """Trim trailing punctuation and validate. Returns None if the
    candidate doesn't look like a real path.

    Validation is intentionally cheap — we don't try to enforce filesystem
    syntax beyond shape. The disk check that follows is the source of truth
    for "does this path exist"; the validator's job is to keep prose like
    "I/O" or "yes/no" out of the candidate set."""
    if not raw:
        return None

    # Trim leading whitespace just in case (backtick groups can carry it).
    s = raw.strip()
    # Trim trailing punctuation. Repeated rstrip handles "/etc/foo.,"
    # cleanly without a regex pass.
    while s and s[-1] in _TRAILING_PUNCT:
        s = s[:-1]
    # Trim trailing slashes for dedup purposes (so /tmp/ and /tmp register
    # as the same candidate). We DO want to check the path with the slash
    # stripped — `Path.exists()` returns the same value either way for an
    # existing directory, and stripping avoids double-counting.
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]

    if not s or len(s) > _MAX_PATH_LENGTH:
        return None

    if "://" in s:
        return None

    # SSH-style remote: `user@host:path` shouldn't be treated as a path
    # just because the part after `:` starts with `/`. The bare regex
    # already rules out a leading `@` via the boundary, but a candidate
    # extracted from backticks could still contain one — be explicit.
    if "@" in s and ":" in s and s.index("@") < s.index(":"):
        return None

    # CLI / slash-command shape: a real path with internal whitespace has
    # `/` separating each directory boundary, so the first whitespace-
    # separated chunk crosses multiple boundaries and contains multiple
    # slashes (`/Users/My Stuff/file` → first chunk `/Users/My`, two `/`).
    # CLI invocations don't look like that:
    #
    # 1. Slash commands have a single-slash command name as their first
    #    chunk (`/plugin install foo` → first chunk `/plugin`, one `/`;
    #    `/plugin install owner/repo` → also one — even when an argument
    #    contains `/`, the leading command name does not).
    #
    # 2. Shell invocations starting at an absolute binary path (`/usr/bin/env
    #    python -m bettermemory`) defeat the first-chunk rule because
    #    `/usr/bin/env` has multiple slashes — but they always have 2+
    #    adjacent slashless tokens after the binary (`python`, `-m`,
    #    `bettermemory`). A path with internal whitespace can have one
    #    slashless component (`/Users/My Stuff` → `Stuff` has no slash)
    #    but rarely two adjacent — every directory boundary is a `/`.
    #
    # Combining the two rules catches both shapes without over-rejecting
    # the rare-but-legal "path with internal whitespace". Counts `\` as
    # well as `/` so Windows paths with internal spaces (`C:\Users\Me My
    # Stuff\file`) still pass.
    #
    # Without this filter, backtick-wrapped Claude Code slash commands
    # quoted in prose ended up in `path_drift_missing` because no such
    # file existed on disk — noisy false positives on any memory
    # describing the install path.
    if " " in s or "\t" in s:
        parts = s.split()
        first = parts[0]
        slashless = sum(1 for p in parts if "/" not in p and "\\" not in p)
        if first.count("/") + first.count("\\") <= 1 or slashless >= 2:
            return None

    if s.startswith("~/"):
        if len(s) <= 2:
            return None
        return None if _is_placeholder_path(s) else s
    if s.startswith("/"):
        # `/x` is too short to be a meaningful claim; `/` alone is the root.
        if len(s) < 3:
            return None
        if _is_placeholder_path(s):
            return None
        # Single-segment absolute path with no extension (`/verify`,
        # `/healthz`, `/login`, `/api`): almost always a URL route or
        # identifier in prose, not a filesystem citation. Real
        # filesystem citations in memory bodies are either multi-segment
        # (`/Users/...`, `/etc/foo.conf`), home-relative (`~/...`), or
        # carry an extension (`/foo.txt`). The narrowing also filters
        # bare top-level dirs (`/etc`, `/usr`, `/var`) — those always
        # exist on the systems this runs on, so no real drift signal is
        # lost. Concrete bite this fixes: the canonical bettermemory
        # body cites `/verify` (the web UI POST route) and the
        # extractor was reading it as a missing filesystem path,
        # producing a phantom `path_drift_missing=1` on every retrieval.
        if _is_single_segment_routelike(s):
            return None
        return s
    # Windows drive: `C:\` or `C:/`
    if len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "/\\":
        return s
    return None


def _is_single_segment_routelike(s: str) -> bool:
    """True when `s` is a single-segment absolute path with no extension
    (`/verify`, `/healthz`, `/api`, `/etc`).

    Detect by counting `/` after the leading one: exactly one `/` total
    AND the terminal segment has no `.`. Multi-segment paths
    (`/etc/foo`), extensioned single segments (`/foo.txt`), and the
    root (`/`, already filtered upstream by the length check) are not
    matched. Windows paths never enter this branch — they're handled
    via the drive-letter check after the `/` branch.
    """
    if s.count("/") != 1:
        return False
    segment = s[1:]
    if not segment:
        return False
    return "." not in segment


def _is_placeholder_path(s: str) -> bool:
    """True when `s` is a documentation-placeholder path the author used
    to illustrate path-shape rather than to cite a real filesystem entry.

    Match strategy: exact membership in `_PLACEHOLDER_PATHS`, or under one
    of `_PLACEHOLDER_PREFIXES`. A single trailing extension on the
    candidate is stripped before matching so `/etc/foo.conf` reads as a
    placeholder via the `/etc/foo` entry; multi-dotted paths
    (`/etc/foo.bar.baz`) only strip the final `.baz` and are deliberately
    not unfolded further — the false-positive surface gets too wide.

    The `~/.X` case (`~/.claude-memory`, `~/.config`) is safe: stripping
    the trailing extension off a leading-dot terminal segment leaves a
    stem that doesn't match any placeholder.
    """
    if s in _PLACEHOLDER_PATHS or s.startswith(_PLACEHOLDER_PREFIXES):
        return True
    # Strip a single trailing extension (only when there's a `.` in the
    # final path component) and re-test.
    last_segment = s.rsplit("/", 1)[-1]
    if "." in last_segment:
        stem = s.rsplit(".", 1)[0]
        if stem != s and (
            stem in _PLACEHOLDER_PATHS or stem.startswith(_PLACEHOLDER_PREFIXES)
        ):
            return True
    return False


def _path_exists(candidate: str) -> bool:
    """Stat a candidate path. Returns False on any OSError so a weird
    path on disk (broken symlink loop, permission denied, ENOTDIR) doesn't
    crash retrieval. False here means "couldn't confirm existence", which
    folds into the `missing` bucket — semantically correct for a staleness
    signal."""
    try:
        return Path(candidate).expanduser().exists()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Verification staleness
# ---------------------------------------------------------------------------
#
# `last_verified_at` is the timestamp the `memory_verify` tool stamps when an
# agent has confirmed a memory's body still matches reality. Null means
# never verified. The retrieval surface used to expose the raw timestamp
# and lean on prose guidance to make the consuming model do the staleness
# arithmetic — which fails open whenever the model's attention wavers.
#
# Replacing the raw timestamp with a structured `verification` block (status,
# age, recommendation) puts the staleness verdict in the response payload
# itself. The model can ignore prose; it cannot easily ignore a literal
# `recommendation` string sitting next to the body it just retrieved. That
# inversion — from "model decides whether to spot-check" to "tool decides
# whether to ask the model to spot-check" — is the whole point. This caught
# a real-world drift in the field (a memory whose tool list lagged the code
# by three new tools). The cost of being too cautious here is one extra
# spot-check per retrieval; the cost of being too lax is exactly the kind
# of stale-memory incident this project exists to prevent.


# Default freshness window. After this many days, a verified memory flips
# from "fresh" to "stale" and gets a re-spot-check recommendation. 30 days
# matches the recency-boost half-life (`recency_boost_half_life_days`) —
# memories the ranker is no longer treating as "fresh" for ordering also
# stop counting as "fresh" for verification. Override via
# `behavior.verification_stale_days` in config.toml.
DEFAULT_VERIFICATION_STALE_DAYS = 30


@dataclass(frozen=True)
class VerificationStatus:
    """Structured staleness verdict for a memory's `last_verified_at`.

    `status` is one of:

    - ``"never"``: `last_verified_at` is None — the memory has not been
      spot-checked since it was written. Highest-risk profile; the body
      may have been wrong on day 1, may have drifted since, and no
      human/agent has confirmed otherwise.
    - ``"stale"``: `last_verified_at` is set, but more than
      `stale_after_days` ago. The world may have moved on since the
      last spot-check.
    - ``"fresh"``: verified within the staleness window. No action
      needed beyond the usual path-drift triage.

    `age_days` is the integer day count since the last verification, or
    None when status is ``"never"``. `recommendation` is a short
    actionable string aimed at the retrieving model — non-None for
    ``"never"`` and ``"stale"``, None for ``"fresh"``. Putting the
    recommendation in the payload (rather than only in the prose system
    prompt) is the load-bearing piece: a model scanning structured
    fields cannot miss the explicit ask to spot-check.
    """

    status: str
    last_verified_at: datetime | None
    age_days: int | None
    recommendation: str | None
    stale_after_days: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialization for the tool response.

        `last_verified_at` is rendered ISO-8601 with the `+00:00` suffix
        normalised to ``Z`` to match the rest of the server's datetime
        formatting. The block is emitted in full on every retrieval
        (including ``"fresh"``) so a consumer can branch on a stable
        shape — `recommendation: null` is the explicit "nothing to do"
        signal.
        """
        return {
            "status": self.status,
            "last_verified_at": (
                None
                if self.last_verified_at is None
                else self.last_verified_at.isoformat().replace("+00:00", "Z")
            ),
            "age_days": self.age_days,
            "recommendation": self.recommendation,
            "stale_after_days": self.stale_after_days,
        }


# Recommendation strings live as module constants so tests can match on
# them without duplicating prose, and so future tone tweaks happen in one
# place. Both end with the concrete tool call to make next-step routing
# unambiguous from the model's side.
_NEVER_RECOMMENDATION = (
    "This memory has never been spot-checked since it was written. Before "
    "relying on any specific claim it makes (file path, commit hash, "
    "version, configuration, list of items), confirm at least one against "
    "ground truth. If the claim still holds, call memory_verify(id, "
    "note=...) to record the check. If it has drifted, fix the body via "
    "memory_update first, then memory_verify the corrected memory."
)


def _stale_recommendation(age_days: int) -> str:
    return (
        f"Last spot-checked {age_days} days ago — past the freshness window. "
        "Re-confirm at least one verifiable claim (path, commit, version, "
        "config, list) and call memory_verify(id, note=...) to refresh, or "
        "memory_update if a claim has drifted."
    )


def compute_verification_status(
    last_verified_at: datetime | None,
    *,
    now: datetime,
    stale_after_days: int = DEFAULT_VERIFICATION_STALE_DAYS,
) -> VerificationStatus:
    """Classify a memory's verification staleness.

    `now` is injected rather than read from the clock so callers can fix
    a single timestamp across a multi-hit retrieval (consistent with how
    `search.search` threads its own `now`) and tests can pin time. A
    timezone-naive `last_verified_at` is treated as UTC, matching the
    convention `search._recency_factor` uses — every datetime in the
    store is UTC-aware in practice, but defensive normalisation keeps a
    legacy file from raising.

    `stale_after_days <= 0` collapses the "fresh" window to nothing —
    every verified memory becomes "stale" immediately. Useful for tests
    that want the stale-recommendation branch without sleeping. A
    negative value behaves the same as 0 (clamped), to avoid a silent
    inverted comparison.

    Boundary semantic: the comparison is strict-greater on the actual
    elapsed seconds (not the floored day count), so a memory at
    exactly `stale_after_days` of age is still fresh and only flips
    to stale once it crosses the threshold. The intuitive reading of
    "fresh for 30 days, then stale" then matches the implementation —
    without the strict boundary the verdict flipped at midnight UTC
    on day 30 instead of day 31, the audit's "fresh at 23:59, stale
    at 00:01" surprise. Comparing in seconds (rather than the floored
    `age_days`) keeps the zero-threshold carve-out intact: any
    measurable elapsed time satisfies `age_seconds > 0`, so
    `stale_after_days=0` still flips immediately.
    """
    threshold = max(0, stale_after_days)

    if last_verified_at is None:
        return VerificationStatus(
            status="never",
            last_verified_at=None,
            age_days=None,
            recommendation=_NEVER_RECOMMENDATION,
            stale_after_days=threshold,
        )

    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (now - last_verified_at).total_seconds())
    age_days = int(age_seconds // 86400)
    threshold_seconds = threshold * 86400

    if age_seconds > threshold_seconds:
        return VerificationStatus(
            status="stale",
            last_verified_at=last_verified_at,
            age_days=age_days,
            recommendation=_stale_recommendation(age_days),
            stale_after_days=threshold,
        )

    return VerificationStatus(
        status="fresh",
        last_verified_at=last_verified_at,
        age_days=age_days,
        recommendation=None,
        stale_after_days=threshold,
    )


# ---------------------------------------------------------------------------
# Commit drift — repo-aware staleness
# ---------------------------------------------------------------------------
#
# Calendar verification staleness ((2) above) misses the case the project
# was actually built to catch: a memory describes the state of the repo
# the user is currently working on, the user just rewrote half of it, and
# the calendar still says "fresh" because they verified the row two hours
# ago. The repo itself is the source of truth — if commits landed since
# `last_verified_at`, the verification verdict is lagging reality even
# when the calendar disagrees.
#
# We only emit this signal when the caller is *currently* inside a
# checkout of the memory's origin repo. Trying to be helpful when the
# user is somewhere else (mapping a remote URL to a local clone via some
# global registry) overreaches into ground we can't trust. A memory might
# describe a repo the user has cloned in three places; guessing which is
# canonical is worse than staying quiet. Silence is the correct default.


@dataclass(frozen=True)
class CommitDriftStatus:
    """Repo-aware staleness verdict.

    `status` is one of:

    - ``"clean"``: zero commits authored after `last_verified_at`. The
      project hasn't moved; the existing verification still reflects the
      repo state the caller is sitting in.
    - ``"drift"``: at least one commit authored after `last_verified_at`.
      The calendar `verification.status` may say "fresh," but the repo
      has moved on. Spot-check before relying on the body.

    `commits_since_verify` is the integer count (always 0 for ``"clean"``,
    positive for ``"drift"``). `recommendation` is a short actionable
    string for the model on ``"drift"``, None on ``"clean"``.
    """

    status: str
    commits_since_verify: int
    recommendation: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "commits_since_verify": self.commits_since_verify,
            "recommendation": self.recommendation,
        }


def _drift_recommendation(count: int) -> str:
    plural = "" if count == 1 else "s"
    return (
        f"{count} commit{plural} landed in this repo since the last "
        "memory_verify — calendar verification looks fresh but the "
        "project has moved. Spot-check at least one verifiable claim "
        "against the current HEAD; call memory_verify(id, note=...) "
        "if it still holds, or memory_update first if it has drifted."
    )


def compute_commit_drift(
    last_verified_at: datetime | None,
    memory_origin_repo: str | None,
    *,
    caller_origin: Origin | None,
    verified_paths: list[str] | tuple[str, ...] = (),
) -> CommitDriftStatus | None:
    """Return a commit-drift verdict, or None when the signal isn't useful.

    The signal is emitted only when:

    - the memory has been verified at some point (`last_verified_at` is
      not None — without an anchor we have nothing to count from, and
      `verification.status == "never"` already maxes the alarm);
    - the memory carries an `origin.repo` (no remote, no project
      identity);
    - the caller is currently inside a repo (`caller_origin.cwd` and
      `caller_origin.repo` both set);
    - the caller's repo matches the memory's `origin.repo` via
      `repos_match` (host/owner/name normalisation, not raw URL);
    - `commits_since` returned a parseable integer (git was reachable).

    Otherwise None — emit nothing rather than a noisy "unknown" branch
    every consumer would have to filter. This mirrors `path_drift`'s
    pattern: advisory signals stay invisible when they have nothing
    to advise.

    `verified_paths`, when non-empty, narrows the count to commits
    that touched at least one of those paths since
    `last_verified_at`. The path-filtered count subsumes the
    unfiltered one: a memory verified for ``[/etc/foo]`` reports
    drift only when commits touched ``/etc/foo``, not when other
    parts of the repo moved. Falls back to the unfiltered count
    when the path-filtered query fails (git error, no paths
    resolved inside the repo, etc.) so we never under-count drift.
    """
    if last_verified_at is None:
        return None
    if memory_origin_repo is None or caller_origin is None:
        return None
    if caller_origin.cwd is None or caller_origin.repo is None:
        return None
    if not repos_match(memory_origin_repo, caller_origin.repo):
        return None
    cwd_path = Path(caller_origin.cwd)
    count: int | None = None
    if verified_paths:
        count = commits_since_touching_paths(
            cwd_path, last_verified_at, list(verified_paths)
        )
    if count is None:
        count = commits_since(cwd_path, last_verified_at)
    if count is None:
        return None
    if count == 0:
        return CommitDriftStatus(
            status="clean",
            commits_since_verify=0,
            recommendation=None,
        )
    return CommitDriftStatus(
        status="drift",
        commits_since_verify=count,
        recommendation=_drift_recommendation(count),
    )


# ---------------------------------------------------------------------------
# Staleness verdict — one rollup over verification + path drift + commit drift
# ---------------------------------------------------------------------------
#
# Three independent staleness signals (verification.status, path_drift_missing,
# commit_drift_count) produce real cognitive load when consumers need to OR
# them together every time. The verdict is the derived rollup: one field per
# retrieval that branches into "fresh" / "spot_check_recommended" /
# "spot_check_required". The underlying fields stay; the verdict is the
# load-bearing one consumers should branch on first.


# Tier strings emitted on the wire by ``compute_staleness_verdict``
# below AND by ``ResponseBuilder.attach_commit_drift_counts`` in
# ``_response.py`` (the per-search recompute that folds commit-drift
# into the verdict once the per-search commit-timestamp list has been
# read). The two sites are independent emission points; without a
# shared source of truth a rename here — say ``"spot_check_required"``
# → ``"verify_now"`` — would only propagate to whichever site the
# refactor reached first. ``memory_show`` (canonical site) would emit
# the new string while ``memory_search``'s top hit (recompute site)
# would still emit the old one for any memory matched by the
# commit-drift recompute path. Same divergence-hazard pattern as
# ``_VERDICT_RAISE_STATUSES`` below, but on the OUTPUT side of the
# rollup. Pinned by ``test_staleness_verdict_tier_strings_match_*`` in
# ``tests/test_server_v12_features.py``.
_VERDICT_FRESH: str = "fresh"
_VERDICT_RECOMMENDED: str = "spot_check_recommended"
_VERDICT_REQUIRED: str = "spot_check_required"

# Closed-protocol whitelist: the `verification.status` values that
# pre-empt every drift input and force the verdict to
# ``spot_check_required``. Lives as a module-level frozenset so the
# two consumers (``compute_staleness_verdict`` below and
# ``ResponseBuilder.attach_commit_drift_counts`` in ``_response.py``,
# which re-runs the same gate after folding in commit-drift) share a
# single source of truth. Silent divergence between the two sites
# would let a stale memory surfaced by ``memory_search`` carry a
# different verdict than the same memory surfaced by ``memory_show``
# — the loudest re-verify signal we emit, downgraded by a one-site
# typo. Pinned by ``_EXPECTED_RAISE_STATUSES`` in
# ``tests/test_server_v12_features.py``.
_VERDICT_RAISE_STATUSES: frozenset[str] = frozenset({"never", "stale"})


def compute_staleness_verdict(
    *,
    verification: VerificationStatus,
    path_drift_missing: int,
    commit_drift_count: int | None,
) -> str:
    """Three-valued rollup over verification + path drift + commit drift.

    Returns one of:

    - ``"fresh"``: ``verification.status == "fresh"`` AND no drift on
      either axis. Nothing to do; the body's claims are presumed
      current.
    - ``"spot_check_recommended"``: verification is calendar-fresh but
      the world has moved — a path went missing on disk, or the repo
      this memory came from has commits since the last verify. Worth
      a quick check before relying on the body.
    - ``"spot_check_required"``: ``verification.status`` in
      ``_VERDICT_RAISE_STATUSES`` (``{"never", "stale"}``). Pre-empts
      the drift inputs because the verification anchor itself is
      missing or expired.

    `commit_drift_count` is `None` when the signal isn't applicable
    (caller not in a repo, hit from a different repo, hit never
    verified). None never elevates the verdict on a fresh memory; it
    behaves the same as 0.
    """
    if verification.status in _VERDICT_RAISE_STATUSES:
        return _VERDICT_REQUIRED
    drifty = path_drift_missing > 0 or (
        commit_drift_count is not None and commit_drift_count > 0
    )
    return _VERDICT_RECOMMENDED if drifty else _VERDICT_FRESH


__all__ = [
    "DEFAULT_VERIFICATION_STALE_DAYS",
    "CommitDriftStatus",
    "PathDriftReport",
    "VerificationStatus",
    "compute_commit_drift",
    "compute_staleness_verdict",
    "compute_verification_status",
    "detect_path_drift",
]
