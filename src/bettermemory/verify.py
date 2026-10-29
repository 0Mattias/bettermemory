"""Staleness signals for retrieved memories.

Three concepts, all surfaced on retrieval so a consuming model can
self-triage before relying on stored content. The first lives in
`verify_anchoring`, which this module re-exports; the other two live here.

1. **Path drift** (`verify_anchoring.detect_path_drift`,
   `PathDriftReport`). A memory body
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
   that repo since the verify and surface a `commit_drift` advisory
   alongside `verification`. The count has two bases, and every block
   names the one it used: ``reachability`` — the commits in
   ``<verified_head>..HEAD``, `verified_head` being the commit the
   stamp recorded — when that anchor is an ancestor of HEAD, and
   ``author-date`` — the commits AUTHORED after `last_verified_at` —
   for a record verified before the anchor shipped, or whose anchor a
   rewritten history no longer reaches. The first sees a branch
   authored before the stamp and merged after it; the second counts
   that branch as zero. Cwd-aware by design: if the user is not in the
   matching project, the signal stays silent rather than guessing
   about a remote we have no checkout for.

All three are advisory. They never block; they shape a structured payload
that the retrieval surface (memory_show, memory_search)
attaches to its responses, so the model receives a recommendation as a
first-class field rather than having to do timestamp arithmetic on a raw
datetime. The cost of a false positive (a small extra prompt asking the
model to spot-check) is much lower than the cost of a false negative (the
model treats a stale fact as ground truth).

Detection coverage for path drift is deliberately conservative — better
to miss a real path than chase ghosts:

- Backtick-wrapped paths: ```/etc/foo``` or ```~/Downloads```.
  Highest precision because the author chose to set the path off as code.
- Bare absolute Unix paths: ``/etc/foo``, ``~/Downloads/x.txt`` — including
  title-cased spaced directory segments that resume with a slash
  (``~/Library/Application Support/Claude/config.json``); a bare match
  that instead stops right before `` Capitalized…`` is treated as an
  ambiguous truncation and silently dropped when it fails the disk
  check, never flagged missing (see ``_extract_candidates``).
- Bare Windows paths: ``C:\\Users\\me``, ``D:/data``.

Excluded by design:
- Relative paths (``docs/installation.md``) in a body with NO anchor —
  too many false positives in prose, and without an anchor we'd be
  checking the cwd at retrieval time, which is meaningless. Given an
  anchor — the memory's own ``origin.worktree_root``, captured at write
  time — the same citation names one file in one tree, so it is checked
  through a much stricter filter than the commit-drift anchor scan uses
  (``_check_anchored_citations``). A machine that never had that
  checkout skips the whole check rather than reporting everything gone
  (``_worktree_root_is_live``).
- URLs (``https://...``, ``git://...``, ``ssh://...``) — they have ``/``
  but aren't filesystem paths.
- URL routes cross-referenced against the body: when the body cites a
  domain-attached route (``pypi.org/pypi/bettermemory/json``), absolute
  candidates sharing its first segment (``/pypi/bettermemory/json``) are
  routes, not filesystem citations — suppressed (``_DOMAIN_ROUTE_RE``).
- SSH remotes (``user@host:path``) — colon-prefixed paths would otherwise
  parse as bare paths; filtered via an ``@`` check on the leading run.
- Network and device spellings (``//host/share``, ``/\\host\\share``,
  ``/\\.\\UNC\\...``) and relative claims that climb out of their root
  (``src/../../x``) — the path gate (``_refused_spelling``,
  ``_escapes_root``) refuses them before any stat, since on Windows a
  stat of a UNC path opens an SMB connection to the host it names.
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

Two modules beside this one hold the path-drift scan, and this module
re-exports the names callers import from them: `verify_candidates` (the
caps, the candidate scans and the body parse memo) and `verify_anchoring`
(`detect_path_drift`, its report, the anchors and the anchored reads). A
third, `verify_basis`, a leaf, holds the two bases a commit-drift count is
measured on (`BASIS_REACHABILITY`, `BASIS_AUTHOR_DATE`, re-exported here)
and `compute_commit_drift`'s count of the commits since a verify.
"""

from __future__ import annotations

import bisect
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .claims import (
    Claim,
    build_binding_index,
    claim_level_drift,
    claim_paths,
    governed_claim_paths,
    load_claims,
)
from .origin import (
    MAX_PATCH_STREAM_COMMITS,
    Origin,
    ReachableWalk,
    commit_author_sha_pairs_touching_pathspecs,
    commit_author_timestamps_touching_pathspecs,
    commit_patch_stream,
    repo_toplevel,
    resolve_repo_pathspecs,
)
from .verify_basis import BASIS_AUTHOR_DATE, BASIS_REACHABILITY, _count_since_verify

# The path-drift scan lives in `verify_anchoring` and `verify_candidates`,
# neither of which imports anything from here. The plain imports are
# `__all__`'s names and the names only this module reads; the `name as name`
# form keeps a name importable from here, as the same object, for a caller
# outside this module (tests, `scope_match`, the verify and restore
# handlers), which the type checkers would otherwise see as not exported;
# `_clear_body_parses` is kept so `verify._clear_body_parses` stays the
# registered clearer a test reads in `_caches._CLEARERS`. A patch on a
# re-exported name reaches this module's own readers alone: the readers
# that moved read their child's binding, where a test patches them.
from .verify_anchoring import (
    CONVICTING_ANCHORS,
    SPARSE_CHECKOUT,
    UNANCHORED,
    UNCONFIRMED_COMMIT,
    Anchoring,
    ClaimAnchor,
    PathDriftReport,
    detect_path_drift,
    holds_commit,
)
from .verify_anchoring import (
    _absent_by_sparsity as _absent_by_sparsity,
    _is_absolute_attestation as _is_absolute_attestation,
    _parts_below as _parts_below,
    _worktree_root_is_live as _worktree_root_is_live,
    unverifiable_attestations as unverifiable_attestations,
)
from .verify_candidates import (
    _bounded_scan_text,
    _escapes_root,
    _keep_parse,
    _recall_parse,
)
from .verify_candidates import (
    _ANCHORS as _ANCHORS,
    _CANDIDATES as _CANDIDATES,
    _DOMAIN_ROUTE_RE as _DOMAIN_ROUTE_RE,
    _MAX_ANCHORED_CITATION_STATS as _MAX_ANCHORED_CITATION_STATS,
    _MAX_ANCHOR_CITATIONS as _MAX_ANCHOR_CITATIONS,
    _MAX_BODY_SCAN_BYTES as _MAX_BODY_SCAN_BYTES,
    _MAX_PATH_LENGTH as _MAX_PATH_LENGTH,
    _PLACEHOLDER_PATHS as _PLACEHOLDER_PATHS,
    _PLACEHOLDER_PREFIXES as _PLACEHOLDER_PREFIXES,
    _RELATIVE_CITATION_RE as _RELATIVE_CITATION_RE,
    _body_key as _body_key,
    _clear_body_parses as _clear_body_parses,
    _extract_candidates as _extract_candidates,
    _fold_altsep as _fold_altsep,
    _home_ignores_case as _home_ignores_case,
    _is_multi_segment_routelike as _is_multi_segment_routelike,
    _is_under_home as _is_under_home,
    _normalize_candidate as _normalize_candidate,
    _normalize_for_compare as _normalize_for_compare,
    _scan_candidates as _scan_candidates,
)


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
    - ``"remote"`` (6.6.0): the file carries a `last_verified_at`, but it
      arrived by `sync pull` and this host has never verified the record
      since (`index.verified_locally_at` is NULL on a `synced` row). The
      stamp is another host's, or an attacker's; on this host it is not
      evidence. Built by `remote_verification_status`, applied by
      `_response.ResponseBuilder.apply_trust`.

    `age_days` is the integer day count since the last verification, or
    None when status is ``"never"`` or ``"remote"``. `recommendation` is a short
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


def _remote_recommendation(last_verified_at: datetime) -> str:
    stamp = last_verified_at.isoformat().replace("+00:00", "Z")
    return (
        f"This memory arrived by sync pull and its verification stamp ({stamp}) "
        "was written on another host; this host has not checked it, so the "
        "stamp is not evidence here. Confirm at least one claim against "
        "ground truth on this machine and call memory_verify(id, note=..., "
        "verified_paths=[...]) with paths that resolve here to adopt it, or "
        "memory_update if a claim has drifted."
    )


def remote_verification_status(
    last_verified_at: datetime,
    *,
    stale_after_days: int = DEFAULT_VERIFICATION_STALE_DAYS,
) -> VerificationStatus:
    """The verification block for a synced record whose stamp this host
    never made (see the ``"remote"`` status on `VerificationStatus`).

    `last_verified_at` is the stamp the file carries, kept in the block
    for transparency; `age_days` is None because the calendar leg
    measures distance from a local check and there was none.
    `verdict_from_signals` reads ``"remote"`` like ``"never"``: no local
    anchor, so the verdict is `spot_check_required` whatever the drift
    legs say.
    """
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
    return VerificationStatus(
        status="remote",
        last_verified_at=last_verified_at,
        age_days=None,
        recommendation=_remote_recommendation(last_verified_at),
        stale_after_days=max(0, stale_after_days),
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
    convention `search_bm25._recency_factor` uses — every datetime in the
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

    - ``"clean"``: zero commits since the verify. The project hasn't
      moved; the existing verification still reflects the repo state
      the caller is sitting in.
    - ``"drift"``: at least one commit since the verify. The calendar
      `verification.status` may say "fresh," but the repo has moved
      on. Spot-check before relying on the body.

    `commits_since_verify` is the integer count (always 0 for ``"clean"``,
    positive for ``"drift"``). `recommendation` is a short actionable
    string for the model on ``"drift"``, None on ``"clean"``.

    `basis` names the axis the count was measured on — `BASIS_REACHABILITY`
    (the commits in ``<verified_head>..HEAD``) or `BASIS_AUTHOR_DATE`
    (the commits authored after `last_verified_at`) — and rides every
    block, so a reader knows whether a zero could be hiding a merged
    branch that predates the stamp. `compute_commit_drift` decides it.

    `claims_checked` / `claims_drifted` carry the claim-level detail
    when the memory declares claims AND the narrowing actually ran
    (`unfiltered > 0` — a repo with no post-verify commits never pays
    the claim evaluation, so a clean status from the zero-commit path
    reports no claim block). `to_dict` folds them into a `claim_drift`
    sub-dict only when `claims_checked > 0`, so claim-less memories
    keep their exact wire shape.
    """

    status: str
    commits_since_verify: int
    recommendation: str | None
    claims_checked: int = 0
    claims_drifted: tuple[str, ...] = ()
    basis: str = BASIS_AUTHOR_DATE

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "commits_since_verify": self.commits_since_verify,
            "recommendation": self.recommendation,
            "basis": self.basis,
        }
        if self.claims_checked:
            out["claim_drift"] = {
                "checked": self.claims_checked,
                "drifted": list(self.claims_drifted),
            }
        return out


def _drift_recommendation(count: int) -> str:
    plural = "" if count == 1 else "s"
    return (
        f"{count} commit{plural} touching this memory's cited or attested "
        "paths landed since the last memory_verify — calendar verification "
        "looks fresh but the claims' ground truth has moved. Spot-check at "
        "least one verifiable claim against the current HEAD; call "
        "memory_verify(id, note=...) if it still holds, or memory_update "
        "first if it has drifted."
    )


def _claim_drift_recommendation(count: int, drifted: Sequence[str]) -> str:
    plural = "" if count == 1 else "s"
    cplural = "" if len(drifted) == 1 else "s"
    head = ", ".join(drifted[:3]) + ("…" if len(drifted) > 3 else "")
    return (
        f"{count} commit{plural} since the last memory_verify implicate "
        f"{len(drifted)} declared claim{cplural} ({head}) — the claimed "
        "binding itself was touched, not merely its file. Spot-check "
        "those claims at HEAD; memory_verify if they hold, memory_update "
        "(body and claims together) if not."
    )


def commit_drift_anchor_paths(
    body: str,
    verified_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """The memory's claim anchors for commit-drift purposes.

    Union, in order, of:

    1. `verified_paths` attestations (the caller explicitly named these
       as the claims they spot-checked — the strongest anchor signal),
       an absolute or home spelling validated as the body scan validates
       its candidates (`_attested_anchor`);
    2. absolute/`~` path candidates cited in the body (the same
       extractor path drift uses, `_extract_candidates`);
    3. repo-relative citations (`src/x.py:12`, `docs/y.md`,
       `CHANGELOG.md`) via `_RELATIVE_CITATION_RE` — the dominant
       citation style in real bodies, invisible to path drift (nothing
       to stat without a root) but first-class here (the origin repo IS
       the root).

    An EMPTY result is the claim-kind signal: the memory cites no
    path-shaped claims at all — a preference, lesson, strategy note, or
    reflection — and repo commits cannot invalidate it, so commit drift
    is not applicable (`compute_commit_drift` returns None rather than
    counting the whole repo against it). Calendar staleness remains the
    backstop for that class: `stale_after_days` forces a periodic
    spot-check regardless.

    Deduplicated on the same `~`-expanded comparison form the path-drift
    extractor uses; relative citations are capped at
    `_MAX_ANCHOR_CITATIONS`. Whether an anchor actually resolves INSIDE
    the caller's repo is deliberately not decided here — that's
    `resolve_repo_pathspecs`' job at the git boundary
    (`resolve_commit_drift_count`).

    Served through the body parse memo (`_ANCHORS`), keyed on the body
    and the attestations that count (the non-empty strings, in order).
    """
    attested = tuple(raw for raw in verified_paths if isinstance(raw, str) and raw)
    if not body:
        return _anchor_paths(body, attested)
    key = _body_key(body, attested)
    found: tuple[str, ...] | None = _recall_parse(_ANCHORS, key)
    if found is None:
        found = _anchor_paths(body, attested)
        _keep_parse(_ANCHORS, key, found)
    return found


def _anchor_paths(body: str, attested: tuple[str, ...]) -> tuple[str, ...]:
    """`commit_drift_anchor_paths` uncached, for the attestations already
    filtered to the non-empty strings."""
    anchors: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        key = _normalize_for_compare(candidate)
        if not key or key in seen:
            return
        seen.add(key)
        anchors.append(candidate)

    for raw in attested:
        anchor = _attested_anchor(raw)
        if anchor is not None:
            _add(anchor)
    if body:
        for path, _, _ in _extract_candidates(body):
            _add(path)
        relative_added = 0
        for match in _RELATIVE_CITATION_RE.finditer(_bounded_scan_text(body)):
            if relative_added >= _MAX_ANCHOR_CITATIONS:
                break
            cited = match.group(1)
            if _escapes_root(cited):
                continue
            before = len(seen)
            _add(cited)
            if len(seen) > before:
                relative_added += 1
    return tuple(anchors)


def _attested_anchor(raw: str) -> str | None:
    """The attestation `raw` as a commit-drift anchor, or None when it
    names nothing in a repository.

    An absolute or home spelling passes the validator the body scan
    applies to its own candidates (`_normalize_candidate`): ``$HOME/`` and
    ``${HOME}/`` become ``~/``, trailing punctuation and a ``:line``
    suffix go, and the path gate refuses a network or device spelling,
    since the pathspec resolver (`resolve_repo_pathspecs`) resolves each
    anchor on the disk. Unnormalised, ``$HOME/proj/src/x.py`` reached the
    resolver as a relative path, was joined onto the repository root and
    became a pathspec no commit touches, so a record whose attested file
    changed read not-applicable, where ``~/proj/src/x.py`` counted the
    commit (ENG-167). A relative spelling is a repository path as it
    stands, unless it climbs out of the root (`_escapes_root`)."""
    spelled = raw.strip()
    if not spelled:
        return None
    if _is_absolute_attestation(spelled):
        return _normalize_candidate(spelled)
    return None if _escapes_root(spelled) else spelled


@dataclass(frozen=True)
class ResolvedCommitDrift:
    """A resolved commit-drift measurement, claim-narrowing included.

    `count` keeps `resolve_commit_drift_count`'s integer contract (the
    exact number of post-`since` commits that ESCALATE for this memory).
    `claims_checked` / `claims_drifted` carry the claim-level detail the
    display surfaces attach as `claim_drift` — zero/empty whenever the
    memory declares no claims, in which case the count is the incumbent
    per-file measurement unchanged. `basis` is the axis the count lives
    on (`CommitDriftStatus.basis`).
    """

    count: int
    claims_checked: int = 0
    claims_drifted: tuple[str, ...] = ()
    basis: str = BASIS_AUTHOR_DATE


def _literal_pathspecs(specs: Sequence[str]) -> list[str]:
    """`specs` spelled so git reads each as the path it names, never as a
    pattern: the ``:(literal)`` magic on each (ENG-183).

    A spec is a repository path (`resolve_repo_pathspecs`, a claimed
    path), and git reads a plain one as a pattern: ``src/foo[1].py`` also
    matched ``src/foo1.py``, and a file named ``:(glob)**`` matched every
    commit, so the author-date logs over-counted where the reachable walk
    (`ReachableWalk.shas_touching`, which compares paths) did not, and
    the two bases disagreed about one memory. Every log, phantom check
    and patch stream this module runs over a spec takes it through here;
    the walk keeps the plain specs it compares.

    Where the environment already makes git read every pathspec
    literally (``GIT_LITERAL_PATHSPECS``), git reads no magic either and
    would take the prefix as part of the name, so the specs pass as they
    stand."""
    if _git_bool(os.environ.get("GIT_LITERAL_PATHSPECS")):
        return list(specs)
    return [f":(literal){spec}" for spec in specs]


def _git_bool(value: str | None) -> bool:
    """A boolean environment variable as git reads it: unset or empty is
    false, ``true`` / ``yes`` / ``on`` and a nonzero integer are true."""
    if value is None:
        return False
    word = value.strip().lower()
    if word in ("true", "yes", "on"):
        return True
    try:
        return int(word) != 0
    except ValueError:
        return False


def _weak_tier_evaluation(
    cwd: Path,
    shas: list[str],
    specs: list[str],
    claims: Sequence[Claim],
    toplevel: Path | None,
) -> tuple[list[str], set[str] | None]:
    """Run the claim-level `weak` tier over one patch window.

    Returns `(drifted_renders, implicated_shas)`, or `(…, None)` when
    the patch stream could not be fetched or the window exceeds
    `MAX_PATCH_STREAM_COMMITS` — the caller falls back to incumbent
    per-file counting for the governed half (never under-count on
    infrastructure failure; a hundreds-of-commits window is loudly
    drifted under either signal, so precision there buys nothing).

    Implication is per-claim: a weak-fired symbol/literal claim
    implicates the commits that touched its binding (plus content-anchor
    hits for literals); a weak-fired path or absent claim implicates
    every post-`since` commit whose diff carried lines for that path
    (the deletion's own sha never entered the index, so per-line
    attribution is the closest exact stand-in — and for an absent claim
    the re-creating commit's added lines ARE in the index). A fired
    claim that implicates nothing at all — a deleted binary, an
    all-blank-line file — attributes the whole window rather than
    silently contributing zero to a count whose `weak` verdict just
    said "drifted".
    """
    if len(shas) > MAX_PATCH_STREAM_COMMITS:
        return [], None
    stream = commit_patch_stream(
        cwd, shas, _literal_pathspecs(specs), toplevel=toplevel
    )
    if stream is None:
        return [], None
    index = build_binding_index(stream)
    # A stream the parser could not read is an infrastructure failure,
    # not a clean window. `build_binding_index` COUNTS a malformed hunk
    # rather than raising, and a non-empty stream whose diff headers
    # never parsed (a locale-mojibaked or otherwise unrecognised diff)
    # indexes NO files at all — every claim then reads "untouched" and
    # the governed half contributes a silent zero to a count whose
    # `weak` verdict was never actually evaluated. Demote to the
    # incumbent per-file count (the caller's None branch) on either
    # signal. An EMPTY stream keeps its zero: that is the legitimate
    # no-touch case, the window's commits diffed nothing under these
    # specs. A markers-only stream (commits listed, no diff bodies —
    # the shape `git log -p` produces for merge commits) is non-empty
    # and file-less, so it demotes too: the merge DID touch the spec.
    if index["parse_mismatches"] or (stream.strip() and not index["files"]):
        return [], None
    window = set(shas)
    drifted: list[str] = []
    implicated: set[str] = set()
    for claim in claims:
        result = claim_level_drift(claim, index)
        if not result["weak"]:
            continue
        drifted.append(claim.render())
        claim_shas: set[str] = set(result["binding_shas"])
        claim_shas |= set(result["anchor_shas"])
        if claim.kind in ("path", "absent"):
            for sha_set in index["changed_text"].get(claim.rel_path, {}).values():
                claim_shas |= sha_set
        implicated |= claim_shas
    if drifted and not implicated:
        implicated = set(window)
    return drifted, implicated & window


def resolve_commit_drift(
    *,
    cwd: Path,
    since: datetime,
    unfiltered: int,
    anchors: Sequence[str],
    claims: Sequence[Claim] = (),
    toplevel: Path | None = None,
    walk: ReachableWalk | None = None,
) -> ResolvedCommitDrift | None:
    """The claim-aware core behind `resolve_commit_drift_count`.

    With a `walk` — the reachable range from the memory's `verified_head`
    to HEAD, resolved by the caller through `commits_since_anchor` — the
    count lives in reachability space and `_resolve_reachable` is the
    whole story: `since` and `unfiltered` are not read. Without one the
    author-date narrowing below runs unchanged.

    With no `claims` this is exactly the incumbent per-file narrowing
    (same git calls, same three-valued semantics), wrapped in a
    `ResolvedCommitDrift`. With claims, the anchor set splits into a
    GOVERNED half (files named by at least one claim the `weak` tier can
    address — toplevel-relative by construction, since the declare-time
    oracle resolved them against the origin worktree root) and an
    UNGOVERNED half (every other cited/attested anchor, and every claimed
    file an unaddressable claim keeps out of the governed half;
    `_claim_legs` builds both). The ungoverned half keeps the incumbent
    any-touch rule; the governed half escalates only the commits the
    claim-level `weak` tier implicates (1.1 alerts per catch at 94%
    precision on the 30-repo corpus, vs 3.4 for any-touch —
    `bench/rot/results/multirepo-anchored-2026-07-30.json`). The two
    halves union on COMMIT IDENTITY, so a commit touching both an
    unclaimed anchor and a claimed binding counts once, the total stays
    a strict subset of the post-`since` commits, and a measured zero
    still stands the calendar leg down (`verdict_from_signals`'s
    stale-plus-zero demotion) — now on a stronger zero: nothing touched
    the unclaimed anchors AND nothing implicated a claimed binding.

    Declaring a claim on a file is therefore what STOPS that file's
    method-body churn from nagging; a file the memory cites but never
    claims keeps the conservative default. Fallbacks all point the same
    direction as the incumbent's: git failure on either half degrades to
    the unfiltered/any-touch count, never to silence. A claim whose path
    the path gate refuses is not read (`_gated_claims`).
    """
    claims = _gated_claims(claims)
    if walk is not None:
        return _resolve_reachable(
            cwd=cwd, walk=walk, anchors=anchors, claims=claims, toplevel=toplevel
        )
    if claims:
        return _resolve_with_claims(
            cwd=cwd,
            since=since,
            unfiltered=unfiltered,
            anchors=anchors,
            claims=claims,
            toplevel=toplevel,
        )
    count = resolve_commit_drift_count(
        cwd=cwd,
        since=since,
        unfiltered=unfiltered,
        anchors=anchors,
        claims=(),
        toplevel=toplevel,
    )
    if count is None:
        return None
    return ResolvedCommitDrift(count)


def _gated_claims(claims: Sequence[Claim]) -> list[Claim]:
    """`claims` less every claim whose path the path gate refuses
    (`_escapes_root`, which holds `_refused_spelling`): a network or
    device spelling, a NUL byte, a URL, a rooted or drive-qualified path,
    or one that climbs out of the repository.

    A claimed path becomes a git pathspec, and git on Windows resolves
    each component of an absolute pathspec outside the worktree, so a
    claim on ``//host/share/x.py`` opened the host (ENG-164); a ``..``
    claim made git exit 128, and the count fell back to the repo-wide
    one. Declaration refuses only a backslash, and `load_claims` keeps a
    hand-edited frontmatter entry, so the read side gates what reaches
    git. Claimed paths are toplevel-relative by construction, so no
    claim the oracle declared is refused here."""
    return [claim for claim in claims if not _escapes_root(claim.rel_path)]


def _claim_legs(
    cwd: Path,
    anchors: Sequence[str],
    claims: Sequence[Claim],
    toplevel: Path | None,
) -> tuple[list[str], list[str]] | None:
    """The two legs of a memory's commit-drift narrowing, as pathspecs:
    `(ungoverned, governed)`, or None when git cannot resolve the anchors.

    GOVERNED is every claimed path whose claims the `weak` tier can
    address (`governed_claim_paths`); UNGOVERNED is every other spec the
    memory names: its cited and attested anchors resolved into the repo
    (`resolve_repo_pathspecs`), and the claimed paths a claim the detector
    cannot address takes out of the governed leg (a container literal),
    which fall back to any-touch on their own path, the floor a claim is
    never worse than. Claimed paths are toplevel-relative by construction
    (the declare-time oracle resolved them against the origin worktree
    root), so they are pathspecs as they stand.

    The one place both bases build their legs (`_resolve_reachable`,
    `_resolve_with_claims`), so they cannot disagree about which leg a
    path is in (ENG-161). Before it, an unaddressable claim's path was in
    neither leg: a claims-only memory read not-applicable on the
    reachable basis and the repo-wide count on the author-date one."""
    governed = governed_claim_paths(list(claims)) if claims else []
    governed_set = set(governed)
    specs: list[str] = []
    if anchors:
        resolved = resolve_repo_pathspecs(cwd, list(anchors), toplevel=toplevel)
        if resolved is None:
            return None
        specs = resolved
    claimed = claim_paths(list(claims)) if claims else []
    ungoverned = [
        spec for spec in dict.fromkeys((*specs, *claimed)) if spec not in governed_set
    ]
    return ungoverned, governed


def _resolve_reachable(
    *,
    cwd: Path,
    walk: ReachableWalk,
    anchors: Sequence[str],
    claims: Sequence[Claim],
    toplevel: Path | None,
) -> ResolvedCommitDrift | None:
    """The narrowing in reachability space: which of the walk's commits
    escalate for this memory.

    The same two legs as `_resolve_with_claims`, over the walk instead
    of the author-date logs: the UNGOVERNED anchors (cited or attested,
    named by no claim) escalate on any touch, the GOVERNED files (named
    by a claim) escalate only the commits the claim-level `weak` tier
    implicates, and the two union on commit identity. Which commits
    touched what is read off the walk in Python — no git process per
    memory — so the per-search cost is the walk itself, shared by every
    hit verified at the same anchor.

    Three-valued like the author-date core, with the same fallbacks in
    the same direction: None when every anchor escapes the repo (not
    applicable), the walk's whole length when git cannot resolve the
    pathspecs (conservative, never silence). A range that touched no
    anchor is the one question the walk cannot settle alone — an
    untouched real file reads clean/0, a PHANTOM spec (a citation that
    resolves to a path no commit ever touched) is not applicable, and
    clean/0 is what stands the calendar backstop down — so that case
    pays the author-date readers' phantom classification, the same
    full-history log `_quiescent_drift_applicable` runs, and keeps the
    conservative count when that log cannot run.
    """
    unfiltered = len(walk.commits)
    checked = len(claims)
    conservative = ResolvedCommitDrift(unfiltered, checked, (), BASIS_REACHABILITY)
    legs = _claim_legs(cwd, anchors, claims, toplevel)
    if legs is None:
        return conservative
    ungoverned, governed = legs
    if not ungoverned and not governed:
        # Git answered: every anchor escapes this repo, and no claim names
        # a path in it. The memory's claims are real but not about this
        # repo's code.
        return None

    escalating: set[str] = set()
    if ungoverned:
        escalating |= walk.shas_touching(ungoverned)
    drifted: list[str] = []
    if governed:
        touched = walk.shas_touching(governed)
        # Walk order, so the patch stream names the commits the way a
        # log would; `--no-walk=unsorted` does not care, a reader does.
        post = [sha for sha in walk.commits if sha in touched]
        if post:
            drifted, implicated = _weak_tier_evaluation(
                cwd, post, governed, claims, toplevel
            )
            if implicated is None:
                # Patch stream unavailable or window too large —
                # incumbent any-touch semantics for the governed half.
                escalating.update(post)
            else:
                escalating.update(implicated)
    if escalating:
        return ResolvedCommitDrift(
            len(escalating), checked, tuple(drifted), BASIS_REACHABILITY
        )
    combined = list(dict.fromkeys((*ungoverned, *governed)))
    touching = commit_author_timestamps_touching_pathspecs(
        cwd, _literal_pathspecs(combined), toplevel=toplevel
    )
    if touching is None:
        # Git couldn't run the classification log — never under-count on
        # infrastructure failure.
        return conservative
    if not touching:
        # Clean exit, empty log: no commit reachable from HEAD ever
        # touched any spec — every anchor is a phantom. Not clean; not
        # applicable, the rule the author-date core applies.
        return None
    return ResolvedCommitDrift(0, checked, (), BASIS_REACHABILITY)


def _resolve_with_claims(
    *,
    cwd: Path,
    since: datetime,
    unfiltered: int,
    anchors: Sequence[str],
    claims: Sequence[Claim],
    toplevel: Path | None,
) -> ResolvedCommitDrift | None:
    """Two-leg resolution for a claim-carrying memory.

    Mirrors `resolve_commit_drift_count`'s three-valued discipline
    exactly: conservative unfiltered fallback when git cannot answer,
    None when EVERY spec on both legs is phantom or escapes the repo
    (commit drift not applicable), an exact escalating-commit count
    otherwise. The one asymmetry is deliberate: a memory whose ONLY
    anchors are its claims (`anchors` empty or all-escaping) is still
    counted — the claims are the anchor, which is the entire point of
    declaring them: governed where the `weak` tier can address them,
    any-touch on their own path where it cannot (`_claim_legs`).
    """
    checked = len(claims)
    conservative = ResolvedCommitDrift(unfiltered, checked, ())
    legs = _claim_legs(cwd, anchors, claims, toplevel)
    if legs is None:
        # Git itself couldn't answer for the ungoverned half; the
        # governed half would fare no better. Conservative.
        return conservative
    ungoverned, governed = legs
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    escalating: set[str] = set()
    every_leg_phantom = True

    if ungoverned:
        pairs = commit_author_sha_pairs_touching_pathspecs(
            cwd, _literal_pathspecs(ungoverned), toplevel=toplevel
        )
        if pairs is None:
            return conservative
        if pairs:
            every_leg_phantom = False
            escalating.update(sha for ts, sha in pairs if ts > since)

    drifted: list[str] = []
    if governed:
        # Skipped when no claim is addressable: the log answers None for
        # an empty pathspec list, which read as "git could not answer"
        # and fell back to the repo-wide count.
        gpairs = commit_author_sha_pairs_touching_pathspecs(
            cwd, _literal_pathspecs(governed), toplevel=toplevel
        )
        if gpairs is None:
            return conservative
        if gpairs:
            every_leg_phantom = False
            post = [sha for ts, sha in gpairs if ts > since]
            if post:
                drifted, implicated = _weak_tier_evaluation(
                    cwd, post, governed, claims, toplevel
                )
                if implicated is None:
                    # Patch stream unavailable or window too large —
                    # incumbent any-touch semantics for the governed half.
                    escalating.update(post)
                else:
                    escalating.update(implicated)

    if every_leg_phantom:
        # No commit in this repo's history ever touched any spec on
        # either leg — every anchor is phantom (a claim on a file git
        # never tracked lands here too). Not clean; not applicable.
        return None
    return ResolvedCommitDrift(len(escalating), checked, tuple(drifted))


def resolve_commit_drift_count(
    *,
    cwd: Path,
    since: datetime,
    unfiltered: int,
    anchors: Sequence[str],
    claims: Sequence[Claim] = (),
    toplevel: Path | None = None,
    walk: ReachableWalk | None = None,
) -> int | None:
    """Map a positive repo-wide commit count to the claim-anchored count.

    With a `walk` (see `resolve_commit_drift`) the count is the
    reachability-space narrowing's; the author-date narrowing below is
    what runs without one.

    The shared policy step behind all four commit-drift surfaces
    (memory_search's expanded top hit via `compute_commit_drift`, the
    per-hit fold in `_response.attach_commit_drift_counts`, which
    memory_show reads too through `verdict.memory_verdict`, and the two
    memory_health rollups). Keeping the decision in one function is what
    keeps the surfaces in lockstep — the historical failure mode here is
    one surface learning a policy refinement the others didn't. That is
    why `claims` lives on THIS signature too: a count-only surface (the
    health rollups) passes the memory's claims and gets the same
    claim-narrowed number the display surfaces show, just without the
    per-claim detail (`resolve_commit_drift` is the richer entry point
    over the same core). A count computed without the memory's claims
    is a DIFFERENT policy, and the moment one surface computes it, the
    loudest freshness signal disagrees with the quietest.

    Returns:

    - ``None`` — commit drift is NOT APPLICABLE: `anchors` is empty (the
      memory makes no path-shaped claims), none of the anchors resolve
      inside the caller's repo (the claims live elsewhere — a remote
      host, another checkout, the home directory), or every resolved
      anchor is PHANTOM — it resolves LEXICALLY to a repo-relative
      pathspec no commit in this repo's history ever touched (a sub-root
      or bare-filename citation `resolve_repo_pathspecs` mapped without an
      existence check). A bare repo-wide commit count carries no
      information about any of these; counting it anyway is exactly the
      100%-false-positive noise the claim-kind calibration measured
      (12/12 labeled false positives at 3.13.0, 24/24 at 3.16.0).
    - an ``int`` — the EXACT count of commits authored after `since` that
      touched at least one anchor. Measured in AUTHOR-date space via the
      same `bisect_right` boundary the `unfiltered` count uses
      (`commit_author_timestamps_touching_pathspecs` + bisect), so the two
      counts share one date axis and the filtered count is a strict subset
      of the unfiltered commits — it can never exceed `unfiltered`, and no
      min() clamp is needed. (The prior implementation counted the anchor's
      commits on COMMITTER date via ``git rev-list --since``, whose boundary
      a rebase could inflate past the author-date truth; a clamp bounded
      that at `unfiltered` but could not repair it — post-verify churn on
      OTHER files raised `unfiltered` enough that the inflated committer-date
      filtered count slipped under the clamp and still over-reported.
      Author-date counting removes the mismatch at the source.) Falls back
      to `unfiltered` when git can't run the path-filtered query (never
      under-count on infrastructure failure).

    Callers gate on ``unfiltered > 0`` before calling (a caught-up memory
    pays no git work). One git call
    (``git log --format=%aI HEAD -- <specs>``) now serves double duty: its
    author timestamps give the exact count, and an EMPTY log is itself the
    phantom signal — no spec ever appeared in history — so the separate
    existence probe is gone. A since-deleted cited file still resolves as
    real, since its removal commit keeps it in the log. A claim whose
    path the path gate refuses is not read (`_gated_claims`).
    """
    claims = _gated_claims(claims)
    if walk is not None:
        reachable = _resolve_reachable(
            cwd=cwd, walk=walk, anchors=anchors, claims=claims, toplevel=toplevel
        )
        return None if reachable is None else reachable.count
    if claims:
        resolved = _resolve_with_claims(
            cwd=cwd,
            since=since,
            unfiltered=unfiltered,
            anchors=anchors,
            claims=claims,
            toplevel=toplevel,
        )
        return None if resolved is None else resolved.count
    if not anchors:
        return None
    specs = resolve_repo_pathspecs(cwd, list(anchors), toplevel=toplevel)
    if specs is None:
        # Git itself couldn't answer (not a repo from here, git missing).
        # We can't judge anchoring, so keep the conservative unfiltered
        # count rather than silently exempting a possibly-drifted memory.
        return unfiltered
    if not specs:
        # Git answered: every anchor escapes this repo. The memory's
        # claims are real but not about this repo's code.
        return None
    touching = commit_author_timestamps_touching_pathspecs(
        cwd, _literal_pathspecs(specs), toplevel=toplevel
    )
    if touching is None:
        # Git couldn't run the path-filtered log — never under-count on
        # infrastructure failure; keep the conservative unfiltered count.
        return unfiltered
    if not touching:
        # Clean exit, empty log: no commit reachable from HEAD ever touched
        # any resolved spec, so every anchor is a PHANTOM — a sub-root /
        # bare-filename / spaced-tail citation (`handlers/x.py` for a file at
        # `src/pkg/handlers/x.py`, `Notes.md` sheared off `docs/My Notes.md`)
        # that `resolve_repo_pathspecs` mapped LEXICALLY onto a repo-relative
        # path no commit touched. A phantom is NOT clean; it is NOT
        # APPLICABLE, exactly like an anchor that escapes the repo. No extra
        # existence probe is needed: an empty author-date log IS "no spec ever
        # appeared in history". A since-deleted cited file never reaches here —
        # its removal commit keeps it in the log — so it still counts as real.
        return None
    # EXACT author-date count via the same `bisect_right` boundary the
    # unfiltered count uses (`compute_commit_drift` and the search / health
    # rollups all bisect `commit_author_timestamps`). Both counts now live in
    # author-date space, so the path-filtered count is a strict subset of the
    # unfiltered commits and can never exceed `unfiltered` — no min() clamp is
    # needed. The clamp only ever bounded the previous COMMITTER-date filter,
    # whose `--since` boundary a rebase could inflate past the author-date
    # truth; counting the anchor's commits on author date removes that
    # mismatch at the source instead of capping it after the fact.
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    idx = bisect.bisect_right(touching, since)
    return len(touching) - idx


def _quiescent_drift_applicable(
    cwd: Path,
    anchors: Sequence[str],
    claims: Sequence[Claim],
    *,
    toplevel: Path | None = None,
) -> bool:
    """Classify commit-drift APPLICABILITY when the repo is quiescent.

    `compute_commit_drift` narrows a POSITIVE repo-wide count through
    `resolve_commit_drift`, whose escape/phantom classification returns
    None (signal not applicable) for a memory none of whose anchors land
    in this repo's history. A ZERO repo-wide count used to skip that
    classification entirely and mint an affirmative clean/0 for the very
    same memory: a calendar-stale record citing only
    ``/opt/gitea/conf/app.ini`` read *fresh* whenever the caller's repo
    happened to be quiescent since the verify (`verdict_from_signals`'
    stale-plus-zero demotion consumed the 0 as a measurement), then
    flipped to ``spot_check_required`` the moment one unrelated commit
    landed. The measurement never ran — the demotion's premise ("the
    commit leg has actually run and returned zero") was false, and the
    calendar backstop this module promises for out-of-repo claims was
    stood down by mere repo inactivity.

    So the quiescent branch classifies applicability with the same two
    rules the positive branch applies, over ONE combined spec set —
    anchors resolved via `resolve_repo_pathspecs`, plus the governed
    `claim_paths` (toplevel-relative by construction, used as pathspecs
    directly, exactly as `_resolve_with_claims` does):

    - every spec ESCAPES the repo (and no claim governs a path) →
      False: the claims are real, they just aren't about this repo's
      code;
    - every spec is PHANTOM (clean-exit EMPTY touching log — no commit
      in history ever touched any spec) → False, the same rule
      `resolve_commit_drift_count` applies on the positive branch;
    - otherwise → True: at least one anchor is real and untouched since
      the verify, which is the affirmative evidence clean/0 exists to
      report.

    Could-not-ask keeps the signal, mirroring the positive branch's
    conservative unfiltered fallback: a None from
    `resolve_repo_pathspecs` or a None touching log returns True, so
    infrastructure failure degrades to the incumbent clean/0 rather
    than silently widening the not-applicable exemption.

    Cost: one ``rev-parse`` (skipped when the caller threads an
    already-resolved `toplevel`, as the per-hit search surface does)
    plus at most one path-filtered ``git log`` (both legs share a
    single log over the union, where the positive branch's claim split
    needs two), paid only on the quiescent branch of an ANCHORED
    memory — the untethered class (the measured 100%-false-positive
    population the anchor gate exists for) still pays no git work,
    because `compute_commit_drift` returns None before reaching here.
    The escape half is pure path arithmetic: an all-escaping anchor
    set returns False before the log is ever forked.

    Two other surfaces mint or count a per-memory drift figure. The
    per-hit search surface (`_response.attach_commit_drift_counts`)
    runs THIS classification on its own quiescent branch and omits the
    count where the signal does not apply — an affirmative 0 there is
    read exactly as memory_show's clean/0 is. The health rollups
    (`_compute_commit_drift_debt`, the curation ``drifted`` count) do
    not need it: they report only ``count > 0`` rows and totals, so a
    quiescent memory contributes nothing whether or not the signal
    applies — there is no affirmative 0 for a classification to
    correct, and running the log per caught-up memory on a store-wide
    pass would buy nothing but forks.
    """
    if toplevel is None:
        toplevel = repo_toplevel(cwd)
    specs: list[str] = []
    if anchors:
        resolved = resolve_repo_pathspecs(cwd, list(anchors), toplevel=toplevel)
        if resolved is None:
            # Git itself couldn't answer. We can't judge anchoring, so
            # keep the signal (conservative), mirroring the positive
            # branch's unfiltered fallback.
            return True
        specs = resolved
    gated = _gated_claims(claims)
    governed = claim_paths(gated) if gated else []
    combined = list(dict.fromkeys((*specs, *governed)))
    if not combined:
        return False
    touching = commit_author_timestamps_touching_pathspecs(
        cwd, _literal_pathspecs(combined), toplevel=toplevel
    )
    if touching is None:
        # Git couldn't run the path-filtered log — could-not-ask never
        # manufactures a not-applicable verdict.
        return True
    return bool(touching)


def compute_commit_drift(
    last_verified_at: datetime | None,
    memory_origin_repo: str | None,
    *,
    caller_origin: Origin | None,
    verified_paths: list[str] | tuple[str, ...] = (),
    body: str = "",
    claims: list[str] | tuple[str, ...] = (),
    verified_head: str | None = None,
    anchor: ClaimAnchor | None = None,
) -> CommitDriftStatus | None:
    """Return a commit-drift verdict, or None when the signal isn't useful.

    The commits are counted where the record's cited commit resolves
    under its `anchor` (design 3.6, `ClaimAnchor.commit_root`): in the
    home checkout's live view, else in the caller's checkout when it
    holds `verified_head`, else nowhere, and the leg reads None
    (unconfirmed). Without an anchor (a record with no home row) the leg
    reads None too: no remote is compared to find a checkout that speaks
    for it (decision B23). `memory_origin_repo` and `caller_origin` are
    kept in the signature and decide nothing.

    `verified_head` is the commit the stamp recorded (`Memory.verified_
    head`). When it is set and still an ancestor of the caller's HEAD,
    the count is the number of commits in ``verified_head..HEAD`` that
    touch the memory's anchors — reachability space, which holds a
    branch authored before the stamp and merged after it — and the block
    reads `basis: "reachability"`. Otherwise (no anchor on the record,
    or one a rewritten history no longer reaches) the author-date count
    described below stands, and the block says `basis: "author-date"`.
    The applicability rules and the claim narrowing are the same on both
    bases; only the set of commits they run over differs.

    The signal is emitted only when:

    - the memory has been verified at some point (`last_verified_at` is
      not None — without an anchor we have nothing to count from, and
      `verification.status == "never"` already maxes the alarm);
    - the record has an anchor whose commit root resolves (above);
    - `commit_author_timestamps` returned a list (git was reachable);
    - the memory makes CLAIMS this repo's commits could invalidate —
      it has at least one claim anchor (`commit_drift_anchor_paths`:
      attested `verified_paths` or body-cited paths), and when commits
      have landed, at least one anchor resolves inside the caller's
      repo. A memory with no path-shaped claims (a preference, lesson,
      or reflection that merely ORIGINATED here) is exempt: a bare
      repo-wide commit count carries no information about it, and
      counting it anyway measured as 100% false positives on the
      dogfood store (12/12 at 3.13.0, 24/24 at 3.16.0). Calendar
      staleness (`stale_after_days`) remains the backstop for that
      class.

    Otherwise None — emit nothing rather than a noisy "unknown" branch
    every consumer would have to filter. This mirrors `path_drift`'s
    pattern: advisory signals stay invisible when they have nothing
    to advise.

    The count uses author timestamps + ``bisect_right`` — the same date
    source and strictly-greater boundary that memory_search and
    memory_health use — so all three surfaces agree on the same memory.
    The prior implementation counted via ``git rev-list --since`` (committer
    date, inclusive whole-second), which disagreed with the other two
    after a rebase (committer date rewritten, author date preserved) and
    even with zero rebases when `last_verified_at` landed in the same UTC
    second as a commit.

    The count is CLAIM-ANCHORED: `commit_drift_anchor_paths` derives the
    memory's anchors from `verified_paths` + `body`, and the count is
    narrowed to commits that touched at least one anchor since
    `last_verified_at` (`resolve_commit_drift_count`). A memory anchored
    to ``[/etc/foo]`` reports drift only when commits touched
    ``/etc/foo``, not when other parts of the repo moved. The narrowing
    only runs when the unfiltered count is already positive, and falls
    back to the unfiltered count when git can't answer the path-filtered
    query — but resolves to None (signal not applicable, see above) when
    the memory has no anchors at all or none land inside this repo. That
    not-applicable classification holds on BOTH sides of the count gate:
    a QUIESCENT repo (zero commits since the verify) runs the same
    escape/phantom rules via `_quiescent_drift_applicable` before an
    affirmative clean/0 may be minted, because clean/0 is what stands
    the calendar backstop down (`verdict_from_signals`' stale-plus-zero
    demotion) and an all-escaping or all-phantom memory earned no such
    measurement from the repo merely sitting still.
    """
    if last_verified_at is None:
        return None
    if anchor is None:
        # A record with no anchor has no checkout whose commits speak for
        # it; no remote is compared to find one (decision B23).
        return None
    commit_root, _rule = anchor.commit_root(verified_head)
    if commit_root is None:
        return None
    cwd_path = Path(commit_root)
    # Claim-anchored gate first, since it is pure CPU (a bounded regex
    # over the body; a lenient parse of the stored claims, so a
    # hand-edited bad entry contributes nothing rather than crashing the
    # hottest read path): an untethered memory — no cited or attested
    # path, no declared claim — reads not-applicable without a git
    # process. A memory whose ONLY anchors are its claims is fully
    # governed: the declaration is the anchor.
    anchors = commit_drift_anchor_paths(body, verified_paths)
    parsed_claims = load_claims(list(claims)) if claims else []
    if not anchors and not parsed_claims:
        return None
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
    counted = _count_since_verify(cwd_path, last_verified_at, verified_head)
    if counted is None:
        return None
    count, basis, walk, toplevel = counted
    resolved = _narrow_commit_drift(
        cwd_path, last_verified_at, count, anchors, parsed_claims, toplevel, walk, basis
    )
    if resolved is None:
        return None
    return commit_drift_status(resolved)


def _narrow_commit_drift(
    cwd_path: Path,
    last_verified_at: datetime,
    count: int,
    anchors: tuple[str, ...],
    parsed_claims: list[Claim],
    toplevel: Path | None,
    walk: ReachableWalk | None,
    basis: str,
) -> ResolvedCommitDrift | None:
    """Take the count and its claims; return the narrowed resolution, or None."""
    # Claim-anchored narrowing: the full git-backed NARROWING (claim split,
    # weak tier, per-claim detail) only runs when there's drift to narrow
    # (`count > 0`). The narrowed count is measured on the same axis as
    # `verify_basis._count_since_verify`'s count — a subset of the walk's
    # commits, or an author-date subset of `count` — so it is a strict
    # subset either way: no clamp, and no committer-date boundary to fall
    # back from. The quiescent branch (`count == 0`) no longer skips git
    # entirely: it classifies APPLICABILITY first
    # (`_quiescent_drift_applicable` — escape/phantom → None) so an
    # all-escaping memory can't read clean, and only the untethered class
    # (`compute_commit_drift`'s gate) keeps the pays-no-git-work guarantee.
    # The batch surfaces (`_compute_commit_drift_debt` / the curation rollup
    # / `_response.attach_commit_drift_counts`) still gate all resolution on
    # `count > 0` — see `_quiescent_drift_applicable`'s closing note.
    claims_checked = 0
    claims_drifted: tuple[str, ...] = ()
    if count > 0:
        # Resolve the repo root ONCE for this call and thread it
        # through — anchor resolution (`resolve_repo_pathspecs`) and the
        # path-filtered log (`commit_author_timestamps_touching_
        # pathspecs`) would otherwise EACH pay a `git rev-parse
        # --show-toplevel` fork+exec on a hot read path (every expanded
        # search top hit). Mirrors the batch surfaces
        # (`health._compute_commit_drift_debt`,
        # `_response.attach_commit_drift_counts`), which already thread
        # a once-resolved toplevel. None is tolerated (the resolvers
        # re-derive, preserving the exact conservative fallback), but
        # with `commit_author_timestamps` having just answered, git is
        # demonstrably reachable here. The reachability branch resolved
        # it beside the head already.
        if toplevel is None:
            toplevel = repo_toplevel(cwd_path)
        resolved = resolve_commit_drift(
            cwd=cwd_path,
            since=last_verified_at,
            unfiltered=count,
            anchors=anchors,
            claims=parsed_claims,
            toplevel=toplevel,
            walk=walk,
        )
        if resolved is None:
            return None
        count = resolved.count
        claims_checked = resolved.claims_checked
        claims_drifted = resolved.claims_drifted
    elif not _quiescent_drift_applicable(
        cwd_path, anchors, parsed_claims, toplevel=toplevel
    ):
        # Zero repo-wide commits since the verify. Before an affirmative
        # clean/0 is minted — the exact value the stale-plus-zero
        # demotion reads as "measured, nothing moved" — classify whether
        # the signal applies to this memory AT ALL, by the same
        # escape/phantom rules the positive branch gets from
        # `resolve_commit_drift`. A memory citing only remote-host paths
        # must read the same not-applicable here as it does the moment
        # one unrelated commit lands.
        return None
    return ResolvedCommitDrift(
        count,
        claims_checked=claims_checked,
        claims_drifted=claims_drifted,
        basis=basis,
    )


def commit_drift_status(resolved: ResolvedCommitDrift) -> CommitDriftStatus:
    """The `memory_show` block for a resolved measurement: ``clean`` at a
    zero count, ``drift`` with its recommendation otherwise.

    The one place a `ResolvedCommitDrift` becomes a `CommitDriftStatus`,
    so `compute_commit_drift` and the per-hit memo's readers
    (`verdict.resolve_verdicts`, which serves `memory_show` from
    `_response._DRIFT_MEMO`) emit the same block for the same
    measurement: every field of it is a function of the count, the
    basis and the claim detail the memo stores. A clean block names no
    drifted claim, whatever the resolution carried.
    """
    if resolved.count == 0:
        return CommitDriftStatus(
            status="clean",
            commits_since_verify=0,
            recommendation=None,
            claims_checked=resolved.claims_checked,
            claims_drifted=(),
            basis=resolved.basis,
        )
    recommendation = (
        _claim_drift_recommendation(resolved.count, resolved.claims_drifted)
        if resolved.claims_drifted
        else _drift_recommendation(resolved.count)
    )
    return CommitDriftStatus(
        status="drift",
        commits_since_verify=resolved.count,
        recommendation=recommendation,
        claims_checked=resolved.claims_checked,
        claims_drifted=resolved.claims_drifted,
        basis=resolved.basis,
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
# rollup. Pinned by
# ``test_staleness_verdict_tier_string_values_unchanged`` (wire values)
# and ``test_staleness_verdict_string_matches_constant_across_show_and_search``
# (cross-surface equality) in ``tests/test_server_v12_features.py``.
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

# THE ONE PLACE the commit leg's ESCALATING term is switched, and the
# only thing that may ever read it is `_commit_leg_escalates` below.
#
# What flipping it to False does: a memory whose anchored paths saw
# commits since its last verification stops being nudged to
# `spot_check_recommended` on that evidence alone. What it must NOT do,
# and does not: touch the DEMOTION. `stale` + a measured `0` still reads
# `fresh` — that is the 58a4fa4 fix, the branch that stops the shipped
# default from being the J=0.000 constant function `bench/rot` caught,
# and it reads `commit_drift_count` directly rather than through this
# switch precisely so that a subtraction here cannot resurrect it. The
# None-vs-0 distinction is untouched in both directions.
#
# THE MEASURED CONDITION FOR FLIPPING IT FIRED ON 2026-07-31, AND THE
# GATE IS RETRACTED RATHER THAN HONOURED. The condition stood here as a
# pre-registration — "after the path-drift provenance split and the
# anchored-relative citation arm, re-run `bench/rot`'s new arms pooled;
# if alerts-per-catch for the escalating tier is still >= 1.5, this
# flips to False" — attributed to an "upgrade plan item B2b" that exists
# nowhere in this repository. Both preconditions have since shipped, so
# the condition was live and had to be graded.
#
# WHICH NUMBER IT MEANT, because the artifact carries two and they read
# opposite ways. The trigger is
# `pooled.file_level_incumbent.ALL.alerts_per_catch` = 3.4 in
# `bench/rot/results/multirepo-anchored-2026-07-30.json`, over the 1.5
# line. The other candidate,
# `path_drift_anchored_relative_arm.ALL.alerts_per_catch` = 1.0, is
# under the line and would mean "stays True" — but it grades the PATH
# leg, whose flags are not this switch's term at all.
# `file_level_incumbent` is scored on `_MODES[0]` rows, where the
# calendar leg is stood down and path drift fires exactly zero times, so
# 100% of its flags ARE the escalating commit term. It is the only
# column in that artifact that measures what flipping this constant
# would remove.
#
# WHAT THE FLIP WOULD DO was then measured rather than argued: this
# constant was monkeypatched to False and `bench/rot/run.py` re-run over
# the pinned 60-day window (t0 053ab9de, t1 388b5be7) that produced
# `results/bettermemory-60d-2026-07-26.json`. The control run, switch
# untouched, reproduces that published artifact bit for bit on all three
# arms, its detectors and its baselines, so the instrument is the one
# that was published. With the switch off, every drift arm goes from
# 96.74% flagged / J = 0.0339 to 0.00% flagged / J = 0.000 at a 100%
# unflagged-stale rate — decision columns identical to the `never_flag`
# baseline, which is the mirror image of the `always_flag` constant
# function 3.30.0 fixed and postmortemed. `shipped_default` is
# bit-identical between the two runs, because the demotion below reads
# `commit_drift_count` directly and never consults this switch.
# Artifact: `bench/rot/results/escalation-off-60d-2026-07-31.json`.
#
# THE PREMISE IS FALSIFIED ON DISK. The gate assumed the anchored path
# leg would substitute for what the commit leg was doing; on the
# 30-repository corpus that leg reaches `flag_rate` 0.0073 at
# `unflagged_stale_rate` 0.968 — precise where it fires and silent
# nearly everywhere. Subtracting the commit term does not trade noise
# for a cleaner signal, it removes the only escalating term the verdict
# has and leaves a constant. 3.4 alerts per catch at J = 0.2875, against
# `always_flag`'s J = 0.000 at 4.4, is a weak signal; it is not nothing,
# and nothing is what the flip measures.
#
# So the switch stays True and the gate is closed. Reopening it needs a
# REPLACEMENT measured first, not a subtraction — the claim-level `weak`
# tier costs 1.1 alerts per catch at 94% precision on the same corpus,
# which is what made claims-at-write the replacement to build.
# Write-up: the rot-bench notes.
#
# THE REPLACEMENT HAS SINCE LANDED — WITHOUT TOUCHING THIS SWITCH. For a
# memory that DECLARES claims, `_resolve_with_claims` narrows the count
# this leg escalates on to the commits the `weak` tier implicates, per
# claim-governed file; unclaimed anchors keep the any-touch rule this
# comment defends. The switch still governs whether a positive count
# escalates at all, so subtracting the leg would still zero the verdict
# for claim-less memories — everything above remains the reason it
# stays True. The narrowing is upstream, per-memory, and opt-in via
# declaration, which is exactly the "replacement measured first" shape
# the retraction demanded.
#
# One constraint survives for whoever reopens it: a flip is NOT complete
# on its own. `DESC_MEMORY_SEARCH` / `DESC_MEMORY_SHOW` describe
# `staleness_verdict` as folding commit drift in, and a verdict that no
# longer does must say so in the same change. A model told to read a
# signal that cannot fire is worse than no signal.
_COMMIT_DRIFT_ESCALATES: bool = True


def _commit_leg_escalates(commit_drift_count: int | None) -> bool:
    """Does the commit leg get to RAISE the verdict on this input?

    Split out of the `drifty` disjunction so the commit term has one
    named home instead of being half of a boolean expression: the
    decision the switch above records is "subtract this leg from
    escalation", and a subtraction that has to be surgically extracted
    from an `or` is how the demotion branch would get taken out with it.
    That subtraction was measured and retracted on 2026-07-31 — it
    scores `never_flag` — so it is the SWITCH, not this function, that
    reads `True` in every shipped build: the early return below is dead
    code there, and what this answers stays per-input. The branch is
    kept because the retraction is a result about one measurement, not a
    proof that no successor signal could ever want the seam.

    `None` never escalates and never has: it means the leg could not ask
    (no origin repo, caller elsewhere, git unreachable, no anchor landing
    in this repo), and absence of evidence is not evidence of drift. The
    guard is kept explicit here rather than relying on `None > 0` raising.
    """
    if not _COMMIT_DRIFT_ESCALATES:
        return False
    return commit_drift_count is not None and commit_drift_count > 0


def verdict_from_signals(
    *,
    status: str,
    path_drift_missing: int,
    commit_drift_count: int | None,
) -> str:
    """Primitive rollup over the three staleness signals.

    **`path_drift_missing` is the CLAIM-ANCHORED count, not
    `len(report.missing)`.** Every production call site passes
    `len(report.claim_anchored_missing)` — attested paths that vanished,
    and citations resolved against the memory's own recorded worktree.
    Prose-scraped absences still travel to the caller in
    `report.missing`; they no longer raise a tier. The parameter kept its
    name because the signature is pinned (see below) and because the
    rename would have been the only visible part of the change; the
    meaning moved, so read `PathDriftReport.claim_anchored_missing` for
    what it counts and the 15-vs-3 sweep for why. A new surface that
    wires `len(drift.missing)` in here silently re-broadens the alarm,
    which is why an AST guard in `tests/test_verify.py` checks the
    keyword at every call site rather than trusting the name.

    Split out of `compute_staleness_verdict` so the per-search
    recompute in `_response.attach_commit_drift_counts` — which holds a
    serialised verification dict rather than a `VerificationStatus` —
    can share the ladder instead of restating it. The two sites had
    already accreted three "mirror the gate above" comments warning
    that a one-site edit would desync `memory_search`'s top hit from
    `memory_show` for the same memory; sharing the primitive is the
    fix those comments were asking for.

    **There are three signals by decision, not by accident.** A fourth —
    resolving body-cited or attested commit SHAs read-side — was designed
    and measured on 2026-07-26 and rejected: it fires on 34 of 34
    SHA-carrying in-repo memories, Youden's J = 0.000, and the memories it
    would flip are exactly the ones already reading fresh, so a zero-git
    predictor reproduces its whole output. The full record, including the
    honest cost of leaving that class uncovered, is the `SHA_MARKER`
    tombstone in `src/bettermemory/durability.py`; the count is pinned by
    `test_verdict_from_signals_takes_exactly_three_signals` in
    `tests/test_verify.py`.
    """
    drifty = path_drift_missing > 0 or _commit_leg_escalates(commit_drift_count)
    if status in ("never", "remote"):
        # No anchor was ever laid down, so there is no "since when" to
        # measure against and nothing can stand the calendar leg down.
        # "remote" is the same absence seen from this host: the stamp in
        # the file was laid down elsewhere (`remote_verification_status`).
        return _VERDICT_REQUIRED
    if drifty:
        if status in _VERDICT_RAISE_STATUSES:
            return _VERDICT_REQUIRED
        return _VERDICT_RECOMMENDED
    if status not in _VERDICT_RAISE_STATUSES:
        return _VERDICT_FRESH
    # `status == "stale"`, and every leg that could speak came back
    # clean. See `compute_staleness_verdict` for why that yields.
    return _VERDICT_FRESH if commit_drift_count == 0 else _VERDICT_REQUIRED


def compute_staleness_verdict(
    *,
    verification: VerificationStatus,
    path_drift_missing: int,
    commit_drift_count: int | None,
) -> str:
    """Three-valued rollup over verification + path drift + commit drift.

    Returns one of:

    - ``"fresh"``: nothing to do; the body's claims are presumed
      current. Reached either by ``verification.status == "fresh"``
      with no drift on either axis, or — see below — by a calendar-
      stale memory whose commit-drift leg measured zero.
    - ``"spot_check_recommended"``: verification is calendar-fresh but
      the world has moved — a path went missing on disk, or the repo
      this memory came from has commits since the last verify. Worth
      a quick check before relying on the body.
    - ``"spot_check_required"``: the verification anchor is missing
      (``"never"``), or it is expired (``"stale"``) and no measurement
      is available to stand the calendar leg down.

    `commit_drift_count` is `None` when the signal isn't applicable
    (caller not in a repo, hit from a different repo, hit never
    verified, memory makes no claims this repo's commits could
    invalidate). None never elevates the verdict on a fresh memory; it
    behaves the same as 0 there.

    **Why a calendar-stale memory can still read "fresh".** Until
    3.30.0 ``verification.status in {"never", "stale"}`` pre-empted
    both drift inputs outright. That made the verdict a CONSTANT
    FUNCTION at the shipped default: with a 30-day freshness window,
    every memory older than 30 days reported ``spot_check_required``
    no matter what the drift legs found, so the legs that carry the
    actual discrimination were unreachable in the configuration most
    users run. ``bench/rot`` measured the consequence directly — the
    ``shipped_default`` arm flags 100% of claims in every class and
    both windows, Youden's J = 0.000, arithmetically identical to
    ``always_flag``.

    The asymmetry was never intended. ``compute_commit_drift`` already
    states the division of labour from the other side: a memory with
    no path-shaped claims is exempt from commit drift because a bare
    repo-wide count carries no information about it, and *"calendar
    staleness remains the backstop for that class"*. A backstop is
    what you fall back to when the measurement cannot speak — not
    something that overrides the measurement when it can. So the
    ladder now honours that contract in both directions: when the
    commit leg has actually run and returned zero, it means no commit
    touched anything this memory cites SINCE ITS OWN LAST
    VERIFICATION, which is precisely the question the calendar leg is
    a crude proxy for. The measurement wins; the proxy yields.

    The demotion is deliberately gated on the commit leg alone, not on
    path drift:

    - ``"never"`` never demotes. ``compute_commit_drift`` returns
      ``None`` without a ``last_verified_at`` anyway, so this is
      belt-and-braces, but it is the load-bearing carve-out: a memory
      nobody ever checked has no anchor for "since when".
    - ``commit_drift_count is None`` does not demote. None means the
      leg could not ask — no origin repo, caller elsewhere, git
      unreachable, or no anchor landing in this repo. Absence of
      evidence is not evidence of freshness, and this is the branch
      that keeps preference/lesson/reflection memories (the ~36% of
      real bodies `bench/claims.py` grades as judgement rather than
      checkable claim) pinned at ``spot_check_required`` where they
      belong.
    - Path existence alone does not demote. "The cited file still
      exists" answers a weaker question than "nothing touched it since
      you checked", and the 2026-07-26 store sweep put a number on how
      much weaker: of 15 missing-path alerts raised from paths scraped
      out of body prose, ~0 were real drift (remote-host paths,
      ``/etc/nope``-style placeholders), against 3 of 3 real for
      anchored attestations. A missing path still raises the verdict
      via ``drifty`` — this carve-out is about what may LOWER it.

    That same 15-vs-3 sweep later moved the RAISING side too, which is
    the one line of this docstring's history worth keeping straight: the
    prose half no longer raises either. ``path_drift_missing`` is the
    claim-anchored count now (``verdict_from_signals`` has the full
    note), so "a missing path still raises the verdict" holds for an
    attested or anchored miss and no longer holds for a token scraped
    out of a sentence. The carve-out above is unchanged — path evidence
    of either provenance still cannot LOWER a verdict.
    """
    return verdict_from_signals(
        status=verification.status,
        path_drift_missing=path_drift_missing,
        commit_drift_count=commit_drift_count,
    )


__all__ = [
    "BASIS_AUTHOR_DATE",
    "BASIS_REACHABILITY",
    "CONVICTING_ANCHORS",
    "DEFAULT_VERIFICATION_STALE_DAYS",
    "SPARSE_CHECKOUT",
    "UNANCHORED",
    "UNCONFIRMED_COMMIT",
    "Anchoring",
    "ClaimAnchor",
    "CommitDriftStatus",
    "PathDriftReport",
    "ResolvedCommitDrift",
    "VerificationStatus",
    "commit_drift_anchor_paths",
    "commit_drift_status",
    "compute_commit_drift",
    "compute_staleness_verdict",
    "compute_verification_status",
    "detect_path_drift",
    "holds_commit",
    "resolve_commit_drift",
    "resolve_commit_drift_count",
    "verdict_from_signals",
]
