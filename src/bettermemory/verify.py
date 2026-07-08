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
- Bare absolute Unix paths: ``/etc/foo``, ``~/Downloads/x.txt`` — including
  title-cased spaced directory segments that resume with a slash
  (``~/Library/Application Support/Claude/config.json``); a bare match
  that instead stops right before `` Capitalized…`` is treated as an
  ambiguous truncation and silently dropped when it fails the disk
  check, never flagged missing (see ``_extract_candidates``).
- Bare Windows paths: ``C:\\Users\\me``, ``D:/data``.

Excluded by design:
- Relative paths (``docs/installation.md``) — too many false positives in
  prose, and without an anchor we'd be checking the cwd at retrieval time
  which is meaningless.
- URLs (``https://...``, ``git://...``, ``ssh://...``) — they have ``/``
  but aren't filesystem paths.
- URL routes cross-referenced against the body: when the body cites a
  domain-attached route (``pypi.org/pypi/bettermemory/json``), absolute
  candidates sharing its first segment (``/pypi/bettermemory/json``) are
  routes, not filesystem citations — suppressed (``_DOMAIN_ROUTE_RE``).
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

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .origin import (
    Origin,
    commit_author_timestamps,
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

# Hard cap on how many bytes of a body we scan for path candidates. The
# whole extraction pipeline (`_DOMAIN_ROUTE_RE.finditer`, the backtick pass,
# the bare-path pass) is at best O(body length) and runs on every search
# hit, so an adversarial multi-MB body — arriving via git-sync pull, a
# hostile write, or a hand-edited .md — could peg the server per retrieval
# even after the per-regex ReDoS bound (`_DOMAIN_ROUTE_RE`'s `{1,20}`). Real
# memory bodies are a couple KB; 32 KiB sits comfortably above the p99 real
# body while bounding the worst case to a fixed slice. Truncating (rather
# than rejecting) leaves every normal body untouched and only drops path
# claims that live at or past the cap in a pathological paste — the
# conservative direction this module already prefers (miss a real path over
# chasing ghosts / burning CPU). The cut lands on the last WHITESPACE inside
# the cap, never mid-token: a hard byte slice can bisect a citation
# straddling the boundary, and the surviving prefix — itself a well-formed
# path — validates, fails the disk check, and FABRICATES drift out of a body
# whose real path exists.
_MAX_BODY_SCAN_BYTES = 32 * 1024


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
#
# The continuation group handles title-cased spaced directory segments —
# `~/Library/Application Support/...`, `C:\Program Files\...` — where the
# path resumes with a slash right after the spaced word(s). Without it the
# match stops at the space, and the TRUNCATED prefix (`~/Library/Application`)
# validates, fails the disk check, and manufactures a phantom
# `path_drift_missing` on every memory citing such a path bare. Each spaced
# word must start uppercase and be ≥2 chars (excludes `I/O` and ordinary
# prose, which continues lowercase), and the segment must be followed by a
# slash — `path Word/more` is strongly path-shaped, `path Word more` is
# prose. Terminal spaced components (`.../Visual Studio Code.app` with no
# trailing slash) still can't be captured safely; those fall to the
# ambiguous-truncation drop in `detect_path_drift` instead.
_BARE_RE = re.compile(
    # Boundary: `=` admits VAR=/path and --flag=/path assignments; `|`
    # admits markdown table cells; the typographic quotes admit rich-text
    # paste. None of these characters appear inside real paths.
    r"(?:^|[=|\s(\[{<\"',;“”‘’])"
    # Prefixes: `$HOME/` / `${HOME}/` are canonicalized to `~/` by
    # `_normalize_candidate` so both spellings make the same claim.
    # Body class: `@` covers homebrew versioned kegs (python@3.12),
    # systemd template units (foo@1.service) and npm scoped packages;
    # `+` covers /usr/include/c++; `%` covers escaped URLs-on-disk.
    r"((?:~/|\$HOME/|\$\{HOME\}/|/|[a-zA-Z]:[\\/])[\w./\-_~\\@+%]+"
    r"(?:(?: [A-Z][\w.\-]+)+[\\/][\w./\-_~\\@+%]+)*)"
)

# Code-citation line suffix: `path/file.py:407`, `:407-461`, `:12:5`.
# The line number is not a filesystem claim; the file is. Requires a
# slash before the final colon-free segment so Windows drive prefixes
# (`C:\...`) and prose like `foo:123` never match.
_LINE_SUFFIX_RE = re.compile(r"^(.+[\\/][^\\/:]+):\d+(?:[-:]\d+)?$")

# Single-segment absolute candidates whose terminal segment is one of
# these well-known web filenames are URL routes, not filesystem claims
# (`nginx overrides /robots.txt`). The extensionless single-segment
# filter below can't catch them (the dot reads as a file extension), so
# they are allowlisted — same deliberately-narrow shape as
# `_PLACEHOLDER_PATHS`.
_WELLKNOWN_ROUTE_SEGMENTS: frozenset[str] = frozenset(
    {
        "robots.txt",
        "favicon.ico",
        "sitemap.xml",
        "openapi.json",
        "index.html",
        "manifest.json",
        "humans.txt",
        "security.txt",
        "ads.txt",
    }
)

# Domain-attached route: a hostname-shaped token (two-plus dot-separated
# labels) immediately followed by a `/path`. The captured first path
# segment marks every same-rooted absolute candidate in the body as a URL
# route rather than a filesystem path: a body that writes
# `pypi.org/pypi/bettermemory/<ver>/json` in one sentence and the bare
# index route `/pypi/bettermemory/json` in another is citing an endpoint
# both times — stat'ing the latter against the local disk produced a
# permanent phantom `path_drift_missing`. Suppression is deliberately
# narrow: only `/`-rooted candidates (never `~/` or drive paths — domains
# don't precede those) and only on first-segment equality. The cost of a
# false match is one skipped drift check on a same-named top-level dir
# cited alongside a URL sharing its first segment — conservative in the
# direction this module prefers (miss a real path over chasing ghosts).
#
# The label repetition is bounded (`{1,20}`, not `+`) on purpose: this
# regex is `finditer`'d over the ENTIRE raw body inside
# `_extract_candidates`, which runs per search hit, and an unbounded `+`
# backtracks catastrophically on a domain-shaped run that is NOT followed
# by a slash — the `/([\w.\-]+)` tail fails, so the engine retries every
# partition of the repeat at every start offset. A poisoned body
# (`a.a.a…` × tens of thousands, arriving via git-sync pull, a hostile
# write, or a hand-edited .md) otherwise pegs the whole server for
# seconds-to-minutes; `_MAX_PATHS_PER_BODY` can't help because it caps
# only AFTER the regex has already scanned. Twenty dot-separated labels is
# far past any real FQDN (2-5 in practice), and the `\b[\w-]+` anchor lets
# a match restart at a later label on the rare longer token, so the bound
# can't drop a route the suppression logic would otherwise catch.
_DOMAIN_ROUTE_RE = re.compile(r"\b[\w-]+(?:\.[\w-]+){1,20}/([\w.\-]+)")

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

    `expected_absent` is the subset of `checked` that does NOT exist on
    disk but was attested via `memory_verify(verified_absent_paths=[...])`
    as *intentionally* absent here — a path on a remote host, a
    platform-conditional location (`~/.config/...` cited for Linux while
    running on macOS), or a path the body cites precisely because it is
    NOT the real location. These are excluded from `missing` (no drift
    signal — absence is the attested, expected state) but surfaced in
    their own bucket so the report stays honest about what it skipped.
    An attested-absent path that EXISTS again is treated as a normal
    healthy candidate — presence never raises a flag.
    """

    checked: tuple[str, ...]
    missing: tuple[str, ...]
    verified: tuple[str, ...] = ()
    expected_absent: tuple[str, ...] = ()

    @property
    def has_drift(self) -> bool:
        return bool(self.missing)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "checked": list(self.checked),
            "missing": list(self.missing),
            "verified": list(self.verified),
            "expected_absent": list(self.expected_absent),
        }


def detect_path_drift(
    body: str,
    *,
    verified_paths: tuple[str, ...] | list[str] = (),
    absent_paths: tuple[str, ...] | list[str] = (),
) -> PathDriftReport:
    """Extract path-shaped tokens from `body` and check them on disk.

    Returns an empty report when the body is empty, contains no path
    candidates, or every candidate failed validation. Existence checks
    swallow OSError (permission denied, ELOOP, etc.) and treat them as
    "missing" — the caller should treat a `missing` entry as "the path
    couldn't be confirmed", not as "definitely deleted". The semantic
    difference doesn't matter for the staleness signal.

    Order in `checked` and `missing` is deterministic: backtick-wrapped
    paths come first (in body order), then bare paths from the
    backtick-masked body (in body order). It is NOT a single
    "first-encountered" pass — a bare path that appears textually before
    a backtick-wrapped one still sorts after it, because extraction runs
    the backtick pass to completion before the bare pass (see
    `_extract_candidates`). The ordering is still fully deterministic,
    which is what keeps the report stable for snapshot tests.

    `verified_paths` is an optional set of paths the caller has
    previously attested via `memory_verify`. When a candidate from the
    body is in that set AND still exists, it lands in `report.verified`
    (in addition to the usual `checked` slot). A verified path that's
    since disappeared still lands in `missing` — verification doesn't
    paper over deletion.

    `absent_paths` is the mirror attestation (`memory_verify(
    verified_absent_paths=[...])`): paths the caller confirmed are
    *intentionally* not present on this machine. A candidate in that
    set that fails the disk check lands in `report.expected_absent`
    instead of `missing` — no drift signal, but the skip stays visible.
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
    # `absent_paths` goes through the identical pipeline for the same
    # reason — the two attestation lists must match body candidates by
    # the same rules or the asymmetry bug returns on the absent axis.
    verified_set = _normalize_attestations(verified_paths)
    absent_set = _normalize_attestations(absent_paths)

    checked: list[str] = []
    missing: list[str] = []
    verified: list[str] = []
    expected_absent: list[str] = []
    for path, drop_if_missing, bare_spaced in candidates:
        exists = _path_exists(path)
        norm = _normalize_for_compare(path)
        # An attestation pins the citation: a path the caller explicitly
        # named in `verified_paths` / `verified_absent_paths` is proven to
        # be the complete, intended claim, which resolves any extraction
        # ambiguity — the drops below never apply to attested candidates
        # (otherwise a verified-then-deleted path would produce NO drift
        # signal, breaking the documented contract).
        attested = norm in verified_set or norm in absent_set
        if not exists and not attested:
            if bare_spaced:
                # Existence-arbitrated fallback for spaced bare-scan
                # candidates: the continuation rule can glue a prose
                # acronym pair (`TCP/IP`, `CI/CD`) onto a real path
                # (`/etc/hosts TCP/IP keepalive`). If the prefix up to
                # the first space exists, the spaced run was prose —
                # check the prefix instead, so the real path still gets
                # its drift check. If neither form exists, the extraction
                # is too ambiguous to trust; drop rather than flag a
                # claim we may have manufactured. (Backticked spaced
                # paths never take this branch — the author delimited
                # those precisely, so a miss there is real drift.)
                prefix = path.split(" ", 1)[0]
                if _path_exists(prefix) and prefix not in checked:
                    checked.append(prefix)
                    if _normalize_for_compare(prefix) in verified_set:
                        verified.append(prefix)
                continue
            if drop_if_missing:
                # Ambiguous truncation: the bare scan stopped at
                # ` Capitalized…` (or ` (2).pdf`-style continuations)
                # right after this candidate, so the real citation may
                # continue past the space. A missing-flag here would be
                # manufactured by our own truncation, not by drift.
                # An existing candidate is kept: existence proves the
                # prefix is a real path regardless of the prose after it.
                continue
        if path in checked:
            continue
        checked.append(path)
        if not exists:
            if norm in absent_set:
                expected_absent.append(path)
            else:
                # Known limitation: a bare absolute path that legitimately
                # lives on a REMOTE host (`/opt/gophish`, `/data/compose/.env`
                # on a homelab board) is stat'd against the LOCAL filesystem
                # and so reads as `missing` here — a perpetual drift signal
                # until the caller attests it via
                # `memory_verify(verified_absent_paths=[...])`, which routes it
                # to `expected_absent` above. That attestation is the intended
                # escape hatch: there is no local-only heuristic that can tell
                # a legitimately-remote path from a genuinely-deleted local one
                # without also suppressing real local drift, so absence stays
                # `missing` until proven expected.
                missing.append(path)
            continue
        if norm in verified_set:
            verified.append(path)
    return PathDriftReport(
        checked=tuple(checked),
        missing=tuple(missing),
        verified=tuple(verified),
        expected_absent=tuple(expected_absent),
    )


def _normalize_attestations(paths: tuple[str, ...] | list[str]) -> set[str]:
    """Trim/validate/`~`-expand an attestation list into a comparison set —
    the shared pipeline for `verified_paths` and `absent_paths` (see the
    call-site comment in `detect_path_drift` for why both lists must run
    through the exact same normalisation as the body candidates)."""
    out: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        candidate = _normalize_candidate(raw)
        if candidate is None:
            continue
        out.add(_normalize_for_compare(candidate))
    out.discard("")
    return out


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


def _extract_candidates(body: str) -> list[tuple[str, bool, bool]]:
    """Pull validated, deduplicated path candidates from `body`.

    Returns `(path, drop_if_missing, bare_spaced)` triples:

    - `drop_if_missing` is True only for bare-scan candidates whose match
      stopped immediately before a space followed by a non-lowercase
      character — the signature of a truncated spaced component the
      continuation rule in `_BARE_RE` can't safely capture (terminal
      spaced segments like ``.../Visual Studio Code.app``, duplicate
      downloads like ``report (2).pdf``) — AND whose raw match needed no
      trailing trim: a trimmed sentence period is itself a prose
      delimiter, so ``.../old.conf. The new …`` is a complete citation,
      not a truncation. The caller drops such a candidate when it fails
      the disk check instead of flagging it missing; see
      `detect_path_drift`. Backtick candidates are never ambiguous —
      the author delimited the path precisely.
    - `bare_spaced` marks bare-scan candidates carrying an internal
      space — eligible for the existence-arbitrated prefix fallback in
      `detect_path_drift` (the continuation rule can glue prose acronym
      pairs like ``TCP/IP`` onto a real path; the disk is the arbiter).

    Capped at `_MAX_PATHS_PER_BODY`. Backtick-wrapped matches come first;
    when the same path appears both backtick-wrapped and bare, only the
    backtick form is kept (the bare scan operates on a body with backtick
    contents masked out). Dedup keys on the `~`-expanded comparison form
    so ``~/x`` and ``/Users/me/x`` register as one claim (one missing
    entry, one cap slot). A later clean occurrence of a path first seen
    as ambiguous DOWNGRADES the stored ambiguity — never the reverse —
    so sentence order alone cannot decide whether real drift is reported.

    Candidates whose first segment matches a domain-attached route
    elsewhere in the body are suppressed entirely — see
    `_DOMAIN_ROUTE_RE`. Computed over the ORIGINAL body (not the masked
    one): a URL cited inside backticks is still evidence that a
    same-rooted absolute token is a route.
    """
    if not body:
        return []

    # Bound the scan input up front — before any regex touches it. Every
    # pass below is linear in body length at best and runs per search hit;
    # `_MAX_PATHS_PER_BODY` only caps the candidate COUNT, never the bytes
    # scanned, so a pathological multi-MB body would still be walked in full
    # by `finditer`. Truncate once here (see `_MAX_BODY_SCAN_BYTES`) — and
    # cut at the LAST WHITESPACE inside the cap, never mid-token: a hard
    # slice can bisect a legitimate citation straddling the boundary, and
    # the surviving prefix — itself a well-formed path — validates, fails
    # the disk check, and FABRICATES a `path_drift_missing` entry (a false
    # non-fresh staleness verdict) from a body whose real path exists.
    # Dropping the partial tail token keeps the cap's contract honest: it
    # only ever DROPS claims, never invents one. (A capped body with no
    # whitespace at all keeps the hard slice — a single 32 KiB token is no
    # valid path claim and dies at the candidate-length gate.)
    if len(body) > _MAX_BODY_SCAN_BYTES:
        truncated = body[:_MAX_BODY_SCAN_BYTES]
        last_ws = max(
            truncated.rfind(" "), truncated.rfind("\n"), truncated.rfind("\t")
        )
        body = truncated[:last_ws] if last_ws > 0 else truncated

    route_segments = {m.group(1) for m in _DOMAIN_ROUTE_RE.finditer(body)}

    def _is_route(candidate: str) -> bool:
        if not candidate.startswith("/") or not route_segments:
            return False
        return candidate.split("/", 2)[1] in route_segments

    candidates: list[tuple[str, bool, bool]] = []
    index_of: dict[str, int] = {}

    backtick_spans: list[tuple[int, int]] = []
    for match in _BACKTICK_RE.finditer(body):
        raw = match.group(1).strip()
        normalized = _normalize_candidate(raw)
        if normalized and not _is_route(normalized):
            key = _normalize_for_compare(normalized)
            if key not in index_of:
                index_of[key] = len(candidates)
                candidates.append((normalized, False, False))
                if len(candidates) >= _MAX_PATHS_PER_BODY:
                    return candidates
        backtick_spans.append(match.span())

    # Mask the backtick spans (delimiters included) so the bare scan doesn't
    # see them again, replacing each with spaces of the same length to
    # preserve offsets — the ambiguous-truncation lookahead below indexes
    # into the masked body, so offset preservation is load-bearing, not just
    # debug-friendly. Splice in ONE pass rather than rebuilding the whole
    # string per span: `masked = masked[:s] + spaces + masked[e:]` in the
    # loop was O(spans * body length), a second-order DoS on a body packed
    # with backtick runs. Joining the between-span slices is linear.
    if backtick_spans:
        parts: list[str] = []
        prev = 0
        for start, end in backtick_spans:
            parts.append(body[prev:start])
            parts.append(" " * (end - start))
            prev = end
        parts.append(body[prev:])
        masked = "".join(parts)
    else:
        masked = body

    for match in _BARE_RE.finditer(masked):
        raw = match.group(1)
        normalized = _normalize_candidate(raw)
        if not normalized or _is_route(normalized):
            continue
        tail = masked[match.end(1) : match.end(1) + 2]
        ambiguous = (
            normalized == raw
            and len(tail) == 2
            and tail[0] == " "
            and not tail[1].islower()
        )
        key = _normalize_for_compare(normalized)
        if key in index_of:
            i = index_of[key]
            prev_path, prev_ambiguous, prev_spaced = candidates[i]
            if prev_ambiguous and not ambiguous:
                candidates[i] = (prev_path, False, prev_spaced)
            continue
        index_of[key] = len(candidates)
        candidates.append((normalized, ambiguous, " " in normalized))
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
    # Length gate FIRST, before the trailing-trim loops below. Those loops
    # rstrip one char per iteration with a full string copy each time, so a
    # multi-KB adversarial candidate would pay O(n^2) work only to be
    # rejected by the `len(s) > _MAX_PATH_LENGTH` check further down — the
    # very cap meant to bound this work ran AFTER paying it. The `+ 64`
    # headroom covers the trailing punctuation / slashes those loops strip,
    # so a real candidate that trims down under the cap still validates.
    if not raw or len(raw) > _MAX_PATH_LENGTH + 64:
        return None

    # Trim leading whitespace just in case (backtick groups can carry it).
    s = raw.strip()
    # Shell-escaped spaces (`~/Google\ Drive/notes.txt`) — the form a
    # terminal paste produces — denote a literal space in the path.
    # Unescape before anything else so the spaced-path machinery below
    # sees the real spelling.
    s = s.replace("\\ ", " ")
    # `$HOME/...` is the env-var spelling of `~/...`; canonicalize so both
    # forms funnel into the same home-relative branch (placeholder checks,
    # `~`-expansion at stat and compare time). Only HOME — a general
    # expandvars would expand arbitrary machine-local vars and break the
    # cross-machine attestation-matching property of
    # `_normalize_for_compare`.
    if s.startswith("$HOME/"):
        s = "~" + s[len("$HOME") :]
    elif s.startswith("${HOME}/"):
        s = "~" + s[len("${HOME}") :]
    # Trim trailing punctuation. Repeated rstrip handles "/etc/foo.,"
    # cleanly without a regex pass. A closing `)` is stripped only while
    # UNBALANCED — directory names legitimately end in a parenthesized
    # suffix (`bettermemory (archived)`, `Program Files (x86)`), where the
    # balanced `)` is part of the name, not prose punctuation (the
    # standard linkifier balance heuristic).
    while s and s[-1] in _TRAILING_PUNCT:
        if s[-1] == ")" and s.count("(") >= s.count(")"):
            break
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
    # The `@` must sit before the first slash: in a remote the user@host
    # run leads, while an `@` inside a real path (homebrew kegs like
    # `/opt/homebrew/opt/python@3.12`, systemd templates) always follows
    # directory boundaries.
    first_slash = s.find("/")
    at = s.find("@")
    colon = s.find(":")
    if (
        at != -1
        and colon != -1
        and at < colon
        and (first_slash == -1 or at < first_slash)
    ):
        return None

    # Code-citation line suffix (`file.py:407`, `:407-461`, `:12:5`) —
    # the line number is not a filesystem claim; the file is. Strip it so
    # the disk check targets the cited file: a moved file still flags
    # drift, an existing one stops false-flagging.
    line_suffix = _LINE_SUFFIX_RE.match(s)
    if line_suffix:
        s = line_suffix.group(1)

    # Glob patterns (`/var/log/app/*.log`) and template placeholders
    # (`~/.config/<app>/settings.toml`, `/opt/stacks/{service}/data`) are
    # shape claims, not literal filesystem citations — stat'ing them
    # literally manufactures a permanent missing-flag. Same stance as the
    # placeholder-path skip; these characters essentially never appear in
    # real on-disk paths.
    if any(ch in s for ch in "*?<>{}"):
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
    #    contains `/`, the leading command name does not). Anchors count
    #    as boundaries too: the drive prefix in `C:\Program Files\…` and
    #    the home anchor in `~/Calibre Library/…` each cross a root, so
    #    their single-slash first chunks are as boundary-crossing as
    #    `/Users/My`.
    #
    # 2. Shell invocations starting at an absolute binary path
    #    (`/usr/bin/env python -m bettermemory`, `/opt/homebrew/bin/brew
    #    upgrade`) defeat the first-chunk rule because the binary path
    #    has multiple slashes — but their arguments are argument-shaped:
    #    lowercase words or dash-flags (`python`, `-m`, `upgrade`,
    #    `apply`). Legitimate internal-space path components are
    #    title-cased (`Application Support`, `Program Files`, `My Stuff`)
    #    or carry digits/punctuation (`(x86)`), so one argument-shaped
    #    slashless token marks the candidate as a command, not a path.
    #
    # Without this filter, backtick-wrapped Claude Code slash commands
    # and single-argument shell invocations quoted in prose ended up in
    # `path_drift_missing` because no such file existed on disk — noisy
    # false positives on any memory describing an install path or a cron
    # entry.
    if " " in s or "\t" in s:
        parts = s.split()
        first = parts[0]
        boundaries = first.count("/") + first.count("\\")
        if (
            len(first) >= 3
            and first[0].isalpha()
            and first[1] == ":"
            and first[2] in "/\\"
        ):
            boundaries += 1
        if first.startswith("~/"):
            boundaries += 1
        arglike = any(
            ("/" not in p and "\\" not in p)
            and (p.startswith("-") or (p.isalpha() and p[:1].islower()))
            for p in parts[1:]
        )
        if boundaries <= 1 or arglike:
            return None

    if s.startswith("~/"):
        if len(s) <= 2:
            return None
        return None if _is_placeholder_path(s) else s
    if s.startswith("//"):
        # `//host/share` is SMB/CIFS mount-source notation (mount_smbfs,
        # fstab smbfs/cifs entries, smbclient) — a network-share spec,
        # never a local filesystem claim. Mirrors the `://` URL exclusion.
        return None
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

    Exception to the dot rule: well-known web filenames
    (`/robots.txt`, `/openapi.json`, …) are routes despite carrying an
    extension — see `_WELLKNOWN_ROUTE_SEGMENTS`.
    """
    if s.count("/") != 1:
        return False
    segment = s[1:]
    if not segment:
        return False
    if segment.lower() in _WELLKNOWN_ROUTE_SEGMENTS:
        return True
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
    - `commit_author_timestamps` returned a list (git was reachable).

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

    `verified_paths`, when non-empty, narrows the count to commits
    that touched at least one of those paths since
    `last_verified_at`. The path-filtered count subsumes the
    unfiltered one: a memory verified for ``[/etc/foo]`` reports
    drift only when commits touched ``/etc/foo``, not when other
    parts of the repo moved. The narrowing only runs when the
    unfiltered count is already positive (mirroring the health
    rollup), and falls back to the unfiltered count when the
    path-filtered query fails (git error, no paths resolved inside
    the repo, etc.) so we never under-count drift.
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
    # Count via author timestamps + bisect_right — the SAME date source and
    # boundary rule memory_search (`_response.attach_commit_drift_counts`)
    # and memory_health (`_compute_commit_drift_debt`) use. Two prior
    # divergences from those surfaces are both closed here:
    #   1. date source — the old `commits_since` shelled out `git rev-list
    #      --since`, which filters on COMMITTER date; a rebase rewrites
    #      committer date while preserving author date, so the same memory
    #      could read drifted via memory_show yet clean via memory_search.
    #   2. boundary — `git rev-list --since` is INCLUSIVE and whole-second,
    #      so a commit landing in the same UTC second as `last_verified_at`
    #      counted as drift on memory_show but not on the bisect_right
    #      (strictly-greater, microsecond) path the other two use.
    # `commit_author_timestamps` returns timezone-aware datetimes; sort
    # ascending so `bisect_right` yields the first index strictly after the
    # verify instant. Equal-instant commits fall before the cut (no drift),
    # matching the health rollup and per-hit search count exactly.
    timestamps = commit_author_timestamps(cwd_path)
    if timestamps is None:
        return None
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
    timestamps_sorted = sorted(timestamps)
    idx = bisect.bisect_right(timestamps_sorted, last_verified_at)
    count = len(timestamps_sorted) - idx
    # Narrow to commits that touched an attested path — only when there's
    # drift to narrow AND paths to narrow by. Guarding on `count > 0`
    # mirrors `_compute_commit_drift_debt` / the curation rollup so a
    # caught-up memory never pays the extra `git rev-list` call, and the
    # path-filtered fallback (committer-date, inclusive) is applied on the
    # exact same condition across all three surfaces — keeping them in
    # lockstep on the verified-paths branch too. Falls back to the
    # unfiltered count when the path filter can't run.
    if verified_paths and count > 0:
        filtered = commits_since_touching_paths(
            cwd_path, last_verified_at, list(verified_paths)
        )
        if filtered is not None:
            count = filtered
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
