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
import os
import re
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
    load_claims,
)
from .origin import (
    MAX_PATCH_STREAM_COMMITS,
    Origin,
    commit_author_sha_pairs_touching_pathspecs,
    commit_author_timestamps,
    commit_author_timestamps_touching_pathspecs,
    commit_patch_stream,
    repo_toplevel,
    repos_match,
    resolve_repo_pathspecs,
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

# Repo-relative citation: `src/bettermemory/eval.py`, `docs/ROADMAP.md`,
# `plugin/.claude-plugin/plugin.json`, `CHANGELOG.md` — the dominant
# citation style in real memory bodies, which the absolute/`~` extractor
# above deliberately ignores (a relative path can't be stat'd without
# knowing its root, so it is useless for PATH drift). For COMMIT drift the
# root is known — the memory's origin repo — so relative citations are
# first-class claim anchors there (see `commit_drift_anchor_paths`).
#
# Deliberately conservative, with over-match being the cheap direction: a
# phantom anchor resolves to a repo path no commit ever touched and
# contributes zero to the filtered count (verdict-neutral), while an
# UNDER-match can strip a memory of its only anchor and misclassify it as
# untethered (exempt from commit drift entirely). Shape rules:
#
# - Optional dir run (`{0,12}` segments, each ≤64 chars, non-digit-leading
#   so URL tails like `python.org/3/library/...` never chain) then a
#   filename with a REQUIRED extension of 2-8 chars starting with a letter.
#   The two-char extension floor rejects prose abbreviations (`e.g`, `i.e`,
#   `U.S`) that would otherwise anchor every English-language body; the
#   letter-first rule rejects version strings (`3.16.0`, `v3.16.0rc1`).
# - Zero-dir matches make root-file citations work (`CHANGELOG.md`,
#   `pyproject.toml`) at the cost of occasionally matching a bare domain
#   (`pypi.org`) — phantom-safe per the above. The `(?![\w/])` lookahead
#   keeps a domain-with-route (`pypi.org/simple/...`) from anchoring: that
#   token is a URL, and its route tail is already suppressed for the
#   absolute extractor via `_DOMAIN_ROUTE_RE`. The `\w` half of the
#   lookahead is what makes the rejection backtrack-proof: shrinking the
#   extension (`org` → `or`) always leaves a word character adjacent, so
#   every re-partition of a URL token fails rather than sneaking through
#   as a truncated match. The `\.\w` alternative extends the same guard
#   across label boundaries (`docs.python` inside `docs.python.org` sits
#   before `.o`) without sacrificing sentence-final citations
#   (`…docs/ROADMAP.md.` sits before `. ` — dot-then-space passes).
# - The lookbehind bars `[\w/~.\\-]` so mid-path and mid-token starts
#   never re-match (`src/foo.py` must match once, not once per segment).
#   The `-` is load-bearing: without it a match could start right after a
#   dash, minting a phantom anchor from a leading-dash token
#   (`-leading-dash.md` → `leading-dash.md`) or from a dash inside an
#   absolute path (`/opt/claude-code/src/cli.ts` → `code/src/cli.ts`).
#   Backticks, quotes, parens, and start-of-line stay valid openers, and a
#   markdown bullet is unaffected (`- ` puts a space before the token).
# - Every quantifier is bounded (same ReDoS discipline as
#   `_DOMAIN_ROUTE_RE`): iterations are separated by a literal `/`, so
#   backtracking is confined to one bounded segment window per start
#   offset — linear over the (already `_MAX_BODY_SCAN_BYTES`-truncated)
#   body.
#
# A trailing `:407` / `:407-461` code-citation suffix simply falls outside
# the captured group, mirroring `_LINE_SUFFIX_RE`'s "the line number is not
# a filesystem claim; the file is".
_RELATIVE_CITATION_RE = re.compile(
    r"(?<![\w/~.\\-])"
    r"((?:[A-Za-z_.][\w.\-]{0,63}/){0,12}"
    r"[A-Za-z_.][\w.\-]{0,63}\.[A-Za-z][A-Za-z0-9_]{1,7})"
    r"(?![\w/]|\.\w)"
)

# Cap on relative-citation anchors folded in per body — layered on top of
# `_MAX_PATHS_PER_BODY` (which caps the absolute/`~` extractor). Higher
# than that cap because audit-backlog memories legitimately cite a dozen-
# plus `file.py:line` locations, and an anchor is one pathspec string in a
# single `git rev-list` invocation, not a stat() per retrieval.
_MAX_ANCHOR_CITATIONS = 24

# Stat budget for the ANCHORED CITATION pass (`_check_anchored_citations`),
# reconciling the two caps above rather than picking one. They are
# different currencies: `_MAX_ANCHOR_CITATIONS` (24) bounds pathspec
# STRINGS handed to one `git log` invocation, while every citation checked
# here costs a `stat()` on the hottest retrieval path — the same currency
# `_MAX_PATHS_PER_BODY` (8) already prices. So the citation pass borrows
# the stat cap, not the anchor cap, and gets its own budget rather than
# sharing the body extractor's: a body that already spent all eight slots
# on absolute citations still gets its relative claims checked, and the
# worst case per call stays a fixed, small number of syscalls.
_MAX_ANCHORED_CITATION_STATS = _MAX_PATHS_PER_BODY

# Extensions a relative citation must carry to be existence-checked.
#
# `_RELATIVE_CITATION_RE` accepts ANY 2-8 char letter-first extension
# because over-match is the cheap direction for commit anchors (a phantom
# anchor touches no commit and is verdict-neutral). Existence checking
# inverts that: a phantom stat's `missing` is a fabricated drift signal
# that escalates a verdict. An allowlist is the conservative direction —
# an unlisted real extension loses its check (a false negative, which this
# module has always preferred) while prose that merely happens to be
# slash-and-dot shaped ("the read/write.access split") cannot manufacture
# one. Kept to extensions that actually appear in developer citations;
# growing it is a normal, safe edit.
_CHECKABLE_CITATION_EXTENSIONS = frozenset(
    # source
    "py pyi ts tsx js jsx rs go rb java kt swift c h cc cpp hpp cs php "
    "lua sh sql vue svelte ipynb "
    # config / data
    "toml yaml yml json ini cfg conf env lock xml csv proto tf nix "
    "gradle plist service mod "
    # docs / assets
    "md mdx rst txt html css scss log".split()
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

    `claim_anchored_missing` is the PROVENANCE split over `missing`: the
    subset whose absence is evidence about a claim the memory actually
    makes, as opposed to a path shape scraped out of prose. Three
    producers qualify, and only these three:

      * an absolute body candidate the caller ATTESTED via
        `memory_verify(verified_paths=[...])` and which has since
        disappeared (verified-then-deleted);
      * a relative attestation resolved against the memory's recorded
        `origin.worktree_root` (`_check_anchored_attestations`);
      * a filtered relative citation resolved against that same live
        worktree (`_check_anchored_citations`).

    Everything else in `missing` is an unattested absolute token the
    extractor lifted out of prose. The split exists because the two
    halves were measured and they are not the same instrument: on the
    2026-07-26 store sweep, ~0 of 15 prose-extracted missing-path alerts
    were real drift (remote-host paths, `/etc/nope`-style placeholders,
    documentation examples) against 3 of 3 for anchored attestations.
    Merged into one bucket, the noisy half drove the verdict.

    So `missing` stays the FULL set — every surface that showed a prose
    miss before still shows it, as advisory evidence the caller can act
    on — while `claim_anchored_missing` is what the staleness verdict
    escalates on (`has_claim_anchored_drift`). Evidence stays visible;
    only what ESCALATES narrowed. A path here is always also in
    `missing`; the bucket is a subset marker, never a separate list of
    paths the other buckets lack.

    `dropped_as_route` is the SUPPRESSED set: candidates the scanner
    extracted, found absent, and then declined to stat-report because
    `_is_multi_segment_routelike` judged them application routes rather
    than filesystem citations. They are deliberately NOT in `checked`
    ("we looked and it wasn't there" is a meaningless statement about a
    URL path) and NOT in `missing` (no drift signal). Before this bucket
    existed they were readable from nowhere on this object at all, which
    is how 3.25.2's over-broad route rule swallowed real missing paths
    without the report showing a trace.

    HOW FAR THIS REACHES TODAY — read before relying on it. The bucket
    is populated by `detect_path_drift` and serialised by `to_dict()`,
    so an IN-PROCESS caller holding a `PathDriftReport` can always
    inspect the suppression. A TOOL caller sees it only when the report
    has some OTHER reason to be emitted; a report whose ONLY non-empty
    bucket is `dropped_as_route` — which is what a body citing nothing
    but suppressed paths produces, the case this bucket was added for —
    stays invisible to it, because every surface gates its `path_drift`
    block on the missing / verified / expected-absent buckets, which a
    route-only report fails:

      * `memory_show` and `memory_search`'s expanded top hit gate on
        `has_drift or verified or expected_absent` (`handlers/show.py`,
        `handlers/search.py`) and emit `to_dict()` wholesale once the
        gate fires — a report emitted for any other reason carries the
        suppressed set with it.
      * per-hit `path_drift` on non-top-ranked search hits is rebuilt
        from `MemoryHit` fields (`_response.py`), gated on the same
        three buckets' path lists. The hit carries this bucket as
        `path_drift_dropped_as_route_paths`, and the response builder
        folds a non-empty suppressed set into the emitted dict —
        additively, the key present only when the rule ate something —
        so a firing per-hit block has value-parity with the other two
        surfaces.

    Widening those gate expressions is all a route-only-visibility
    change still needs. Until that is done, the escape hatch for a
    citation you believe is wrongly suppressed remains
    `memory_verify(verified_absent_paths=[...])`, which routes it to
    `expected_absent` before the route rule is consulted — and which a
    caller must reach for without being shown that the suppression
    happened.
    """

    checked: tuple[str, ...]
    missing: tuple[str, ...]
    verified: tuple[str, ...] = ()
    expected_absent: tuple[str, ...] = ()
    dropped_as_route: tuple[str, ...] = ()
    claim_anchored_missing: tuple[str, ...] = ()

    @property
    def has_drift(self) -> bool:
        """True when a candidate failed its disk check unattested.

        Strictly `missing`-only, and deliberately so: a suppressed route
        is by definition NOT drift.

        This is the VISIBILITY term, not the escalation term. It used to
        be both — `has_drift` was the sole path input to the staleness
        verdict — and that is what put prose-scraped absences in charge
        of a tier the caller is told to act on. The verdict now reads
        `has_claim_anchored_drift`; this property keeps its old meaning
        and its old job of deciding whether the caller gets to SEE the
        path-drift block at all.

        NOT the emit gate on its own. `memory_show` and `memory_search`'s
        expanded top hit decide whether to emit a `path_drift` block with
        their own inline expression, `has_drift or verified or
        expected_absent` (`handlers/show.py`, `handlers/search.py`) —
        which this property is only one term of. Adding a bucket here
        therefore does NOT widen those gates; see the `dropped_as_route`
        note above for what that currently costs.
        """
        return bool(self.missing)

    @property
    def has_claim_anchored_drift(self) -> bool:
        """True when a miss is backed by a claim the memory itself makes.

        The escalation term: what `path_drift_missing` means at every
        `compute_staleness_verdict` / `verdict_from_signals` call site.
        See `claim_anchored_missing` for the provenance rule and the
        measurement behind it.

        Deliberately NOT folded into `has_drift`: widening that property
        is the lazy edit that would put prose back in charge of the
        verdict, and narrowing it is the lazy edit that would make prose
        misses invisible. The two questions are separate and each has
        exactly one answer here.
        """
        return bool(self.claim_anchored_missing)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "checked": list(self.checked),
            "missing": list(self.missing),
            "verified": list(self.verified),
            "expected_absent": list(self.expected_absent),
            "dropped_as_route": list(self.dropped_as_route),
            # Unconditional, like every other bucket here: `to_dict()` is
            # the wholesale serialisation two handlers emit, and a caller
            # reading `missing` needs to know which entries drove the
            # verdict WITHOUT having to infer it from the verdict. An
            # empty list next to a non-empty `missing` is the honest,
            # readable statement "we saw these, none of them escalated".
            "claim_anchored_missing": list(self.claim_anchored_missing),
        }


def detect_path_drift(
    body: str,
    *,
    verified_paths: tuple[str, ...] | list[str] = (),
    absent_paths: tuple[str, ...] | list[str] = (),
    worktree_root: str | Path | None = None,
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

    `worktree_root` anchors RELATIVE claims — both attestations
    (`_check_anchored_attestations`) and body citations
    (`_check_anchored_citations`). Body extraction drops relative paths on
    purpose (checking them would otherwise mean checking the reader's
    cwd), but resolved against the memory's own `origin.worktree_root` —
    captured at WRITE time — a relative claim names one file in one tree,
    so it gets a real existence check. Without an anchor a memory citing
    `src/pkg/mod.py`, which is how developers actually write it, receives
    no deletion detection at all; a bare `detect_path_drift(body)` call
    keeps returning nothing for relative paths, unchanged.

    The anchor is used only when it is LIVE on this machine
    (`_worktree_root_is_live`). A store synced from another host records a
    worktree this machine never had, and joining citations to it would
    mark every one of them missing — fabricated drift, on every memory
    from that host at once.

    `absent_paths` is the mirror attestation (`memory_verify(
    verified_absent_paths=[...])`): paths the caller confirmed are
    *intentionally* not present on this machine. A candidate in that
    set that fails the disk check lands in `report.expected_absent`
    instead of `missing` — no drift signal, but the skip stays visible.

    Every miss lands in `report.missing`; the subset backed by an
    attestation or by an anchored citation ALSO lands in
    `report.claim_anchored_missing`, which is the only bucket the
    staleness verdict escalates on. See `PathDriftReport` for the
    measurement that split them.

    A candidate the route rule suppresses lands in
    `report.dropped_as_route` — not `checked`, not `missing`, but
    readable off the returned report instead of nowhere at all. See the
    reach note on `PathDriftReport` for how far the bucket travels
    beyond this return value: each MCP surface's `path_drift` block
    carries a non-empty suppressed set once the block fires, but a
    route-ONLY report still fires no block anywhere.
    """
    candidates = _extract_candidates(body)
    # Resolve the anchor ONCE, for both relative passes. A recorded
    # worktree that is not a live directory here disables them entirely —
    # see `_worktree_root_is_live` for why that direction is right.
    anchor_root = (
        Path(worktree_root)
        if worktree_root is not None and _worktree_root_is_live(worktree_root)
        else None
    )
    if not candidates and anchor_root is None:
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
    # Parallel provenance list, not a filter applied afterwards: by the
    # time a path is a string in `missing` there is nothing left on it to
    # tell prose from attestation, so the split has to be recorded where
    # the decision is made. Every append here is paired with an append to
    # `missing` — the bucket is a subset marker, and the two helpers below
    # take it for the same reason.
    claim_anchored: list[str] = []
    verified: list[str] = []
    expected_absent: list[str] = []
    dropped_as_route: list[str] = []
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
            if _is_multi_segment_routelike(path):
                # An application route, not a deleted file. `_is_route`
                # upstream can only suppress routes when the SAME body
                # also happens to carry a domain-qualified URL to learn a
                # vocabulary from, so a memory citing bare routes
                # (`/api/v1/events/presence`, `/admin/macros`) had every
                # one of them stat'd and reported missing. Kept out of
                # `checked` and `missing` — "we looked and it wasn't
                # there" is a meaningless statement about a URL path —
                # but recorded in `dropped_as_route` so the suppression
                # is at least readable off the returned report instead of
                # leaving no trace anywhere. (Leaving no trace is what let
                # 3.25.2's over-broad rule swallow real missing paths.)
                # NOTE: a report whose ONLY non-empty bucket is this one
                # still reaches no MCP surface — see the reach note on
                # `PathDriftReport`.
                #
                # Deliberately LAST in this block: the spaced-bare and
                # ambiguous-truncation arms above must arbitrate first,
                # or a prose-glued candidate (`/tmp/real-dir TCP/IP`)
                # would read as a route on its manufactured tail and skip
                # the prefix-existence fallback that recovers the real
                # path.
                #
                # Appended unguarded, unlike `checked` below. Every value
                # that lands here is a candidate `path`, and
                # `_extract_candidates` already dedupes those (it keys an
                # index on the comparison form and appends only on a
                # miss), so one route cannot arrive twice — a
                # `not in dropped_as_route` guard would be unreachable.
                # `checked` needs its guard for a reason that does not
                # apply here: the spaced-bare arm above appends a DERIVED
                # `prefix`, which a later, genuinely distinct candidate
                # can equal.
                dropped_as_route.append(path)
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
                # on a homelab board) is stat'd against the LOCAL filesystem,
                # so absence here says nothing about the host that owns it.
                # Two outcomes, split by the route rule above:
                #
                #   * Reaches this line and reads `missing` — a perpetual
                #     drift signal — when the citation carries an extension
                #     (`/data/compose/.env`) or its parent happens to exist
                #     locally (`/opt/gophish`, on a host that has `/opt`).
                #   * Never reaches this line at all, since 3.25.2: any
                #     extensionless non-home citation whose parent is absent
                #     locally (`/srv/docker/gitea`, `/mnt/tank/media`) reads
                #     as an application route and is DROPPED — not reported
                #     missing, not even `checked`. It IS recorded in
                #     `dropped_as_route`, which an in-process caller can
                #     read. These shapes produce a route-ONLY report, so
                #     no MCP surface emits them (a report that also
                #     carries drift does ship the bucket via `to_dict()`).
                #
                # Either way `memory_verify(verified_absent_paths=[...])` is
                # the intended escape hatch: it routes the citation to
                # `expected_absent` above and pins it against BOTH outcomes
                # (attested candidates skip the route drop entirely). There
                # is no local-only heuristic that can tell a legitimately-
                # remote path from a genuinely-deleted local one without
                # also suppressing real local drift, so an unattested
                # absence that gets this far stays `missing`.
                missing.append(path)
                if norm in verified_set:
                    # Verified-then-deleted: the caller named this exact
                    # path in `memory_verify(verified_paths=[...])`, so
                    # its absence is evidence about a REVIEWED claim, not
                    # about a token the extractor lifted out of a
                    # sentence. That is the 3-of-3-real class in the
                    # sweep, and the only shape in this loop that earns
                    # escalation. (An absent-attested path never reaches
                    # here — it took the `expected_absent` arm above.)
                    claim_anchored.append(path)
            continue
        if norm in verified_set:
            verified.append(path)

    _check_anchored_attestations(
        anchor_root,
        verified_paths,
        absent_paths,
        checked=checked,
        missing=missing,
        claim_anchored=claim_anchored,
        verified=verified,
        expected_absent=expected_absent,
    )
    # Attestations FIRST, citations second: an attested path is the
    # caller's reviewed claim and carries the `verified` /
    # `expected_absent` semantics a bare citation cannot, so when a body
    # both cites and attests one file the attestation must be the entry
    # that lands. The citation pass dedupes against everything already
    # checked, so it can only ever ADD claims neither earlier pass saw.
    if anchor_root is not None:
        _check_anchored_citations(
            anchor_root,
            body,
            checked=checked,
            missing=missing,
            claim_anchored=claim_anchored,
        )
    return PathDriftReport(
        checked=tuple(checked),
        missing=tuple(missing),
        verified=tuple(verified),
        expected_absent=tuple(expected_absent),
        dropped_as_route=tuple(dropped_as_route),
        claim_anchored_missing=tuple(claim_anchored),
    )


def _check_anchored_attestations(
    worktree_root: str | Path | None,
    verified_paths: tuple[str, ...] | list[str],
    absent_paths: tuple[str, ...] | list[str],
    *,
    checked: list[str],
    missing: list[str],
    claim_anchored: list[str],
    verified: list[str],
    expected_absent: list[str],
) -> None:
    """Existence-check RELATIVE attestations against the memory's worktree.

    Body extraction drops relative paths on purpose, and the reason given
    is that without an anchor, checking them would mean checking the cwd
    at RETRIEVAL time — which would make a memory's verdict depend on
    where the reader happens to stand. That objection does not reach an
    attestation resolved against `origin.worktree_root`: the worktree is
    captured at WRITE time and stored on the memory, so the check is
    anchored to the tree the author actually attested in.

    Measured before this existed, on a 206-memory store: 104 memories
    attested relative paths, 72 of them received NO path check of any
    kind, and three attested files were already gone with nothing
    surfacing it — one genuinely deleted, one moved, and one that never
    existed (a false attestation). All three read clean.

    Scoped to `verified_paths` / `verified_absent_paths`. An attestation
    is a caller's explicit, reviewed claim that a path IS the citation,
    so it needs no filtering; body-prose citations are checked separately
    and behind a much stricter filter (`_check_anchored_citations`).

    `worktree_root` arrives ALREADY vetted as live by `detect_path_drift`
    — a `None` here means either "no anchor recorded" or "the recorded
    anchor is not a directory on this machine", and both must leave this
    check inert. See `_worktree_root_is_live`.

    Every miss this function records is claim-anchored by construction —
    an attestation IS the reviewed claim — so `claim_anchored` gets the
    same append `missing` does. This is the 3-of-3-real half of the
    measurement that motivated the provenance split.
    """
    if worktree_root is None:
        return
    root = Path(worktree_root)
    seen = {_normalize_for_compare(p) for p in checked}

    def _anchored(raw: str) -> str | None:
        rel = raw.strip() if raw else ""
        if not rel or rel.startswith(("/", "~")):
            return None
        return _normalize_candidate(str(root / rel))

    # Built from the ANCHORED form, not from `_normalize_attestations`:
    # that helper runs the bare relative path through `_normalize_candidate`,
    # which rejects relative paths by design, so the absent set would come
    # back empty and every intentionally-absent attestation would read as
    # drift — the escape hatch inverted into a permanent false alarm.
    absent_set = {
        _normalize_for_compare(anchored)
        for anchored in (_anchored(raw) for raw in absent_paths)
        if anchored is not None
    }
    # Anchor BEFORE validating (see `_anchored`): `_normalize_candidate` is
    # the gate that enforces the relative exclusion, so running the bare
    # relative form through it would drop every attestation this function
    # exists to check. Joining to the worktree root first produces the
    # absolute form the validator is built for, which is also the honest
    # object of the check — the file as it stands in the tree the author
    # attested in.
    for raw in (*verified_paths, *absent_paths):
        resolved = _anchored(raw)
        if resolved is None:
            continue
        norm = _normalize_for_compare(resolved)
        if norm in seen:
            continue
        seen.add(norm)
        checked.append(resolved)
        if _path_exists(resolved):
            if norm not in absent_set:
                verified.append(resolved)
        elif norm in absent_set:
            expected_absent.append(resolved)
        else:
            missing.append(resolved)
            claim_anchored.append(resolved)


def _worktree_root_is_live(worktree_root: str | Path) -> bool:
    """True when the memory's recorded worktree is a directory HERE.

    REVERSES A RECORDED DECISION, so the argument is written down.
    `origin.py`'s auto-scope filter already degrades on a dead worktree
    (`worktrees_match` / `_worktree_root_is_gone`) and explicitly says
    verify takes the OPPOSITE bias: an indeterminate stat there "folds
    into the `missing` path-drift bucket, i.e. toward MORE signal". That
    bias is right for a path the caller attested ON THIS MACHINE and
    which then disappeared — absence is the evidence.

    It is wrong for the root itself. A store synced from another host
    carries that host's `worktree_root`, so `root/rel` cannot exist here
    for ANY relative claim; every one lands in `missing` and every memory
    from that host escalates at once. That is not more signal, it is a
    constant function — the same failure mode as the `always_flag`
    detector the rot benchmark exists to distinguish real detection from.
    A machine that has never seen the checkout has no evidence either
    way, and "no evidence" must read as silence, not as drift.

    So the fail-open is scoped exactly to the thing the reversal is about:
    the ROOT's own liveness, checked once per call. Everything below the
    root keeps the original bias — an unstattable file under a live
    worktree still folds to `missing` via `_path_exists`.

    `os.path.isdir` rather than `Path.is_dir()`: it swallows OSError and
    ValueError internally and keeps this independent of the module-level
    `Path` symbol, which the suite patches wholesale when exercising the
    stat-failure path (same reasoning as `_is_multi_segment_routelike`).
    """
    try:
        return os.path.isdir(os.path.expanduser(str(worktree_root)))
    except (OSError, ValueError):
        return False


def _is_checkable_citation(rel: str) -> bool:
    """True when a `_RELATIVE_CITATION_RE` match may be stat'd once anchored.

    The regex is deliberately OVER-MATCHY and is only safe that way for
    commit drift, where a phantom anchor touches no commit and is
    verdict-neutral. Existence checking inverts the asymmetry — a phantom
    stats as missing and FABRICATES drift — so every citation crosses
    this gate first. Three rules, each closing a measured shape:

    * **At least one directory segment.** A bare filename (`run.py`,
      `CHANGELOG.md`) is the single largest false-positive class: prose
      names a file without its directory constantly ("the `_MODES` tuple
      in run.py"), and joined to the worktree ROOT almost none of those
      exist. It is also what makes the whole bare-domain class
      (`pypi.org`, `example.com`, `fly.io` — zero-dir matches the regex
      admits on purpose) unreachable here, since a domain WITH a route
      never matches the regex at all. The cost is real root-file
      citations (`CHANGELOG.md`) losing their check; that is the cheap
      direction, and an attestation still checks them.

    * **A first segment that is not host-shaped.** A schemeless URL
      (`www.example.com/a/b.md`, `docs.rs/serde/latest/index.html`) DOES
      match the regex — the lookahead only rejects the domain-with-route
      form when the tail has no extension. A dot inside the first segment
      is the tell; a LEADING dot is not (`.github/workflows/ci.yml`,
      `plugin/.claude-plugin/plugin.json`), so only dots past position 0
      disqualify.

    * **A real file extension** (`_CHECKABLE_CITATION_EXTENSIONS`), which
      the regex itself cannot check — it accepts any 2-8 letter-first run.

    Placeholders are refused through the existing `_is_placeholder_path`
    machinery, applied to the ROOT-SLASHED form: `path/to/config.yaml` is
    the same documentation placeholder as `/path/to/config.yaml`, and
    once joined to a worktree root the prefix test would no longer see
    it. Glob/template shapes need no test here — their characters are
    outside the regex's own character class.
    """
    first, slash, _ = rel.partition("/")
    if not slash:
        return False
    if "." in first[1:]:
        return False
    if rel.rsplit(".", 1)[-1].lower() not in _CHECKABLE_CITATION_EXTENSIONS:
        return False
    return not _is_placeholder_path("/" + rel)


def _check_anchored_citations(
    root: Path,
    body: str,
    *,
    checked: list[str],
    missing: list[str],
    claim_anchored: list[str],
) -> None:
    """Existence-check RELATIVE citations in body prose against `root`.

    The counterpart to `_check_anchored_attestations` for the citations
    nobody attested — which is most of them. Measured on the rot
    benchmark before this existed: the relative-citation arm produced
    EXACTLY ZERO path-drift flags, i.e. the citation style developers
    actually write got no path protection at all, while the same claims
    written absolutely were checked. That gap, not the prose-swamp risk,
    is what the anchor closes: `origin.worktree_root` is captured at
    write time, so the check is against the tree the author wrote in
    rather than against the reader's cwd.

    `_is_checkable_citation` is the filter that makes it safe. Two more
    rules live here because they need the resolved path:

    * `_normalize_candidate` runs on the ANCHORED form (as in
      `_check_anchored_attestations`) — it is itself the relative gate,
      so validating the bare form would drop everything.

    * **A missing file is only reported when its immediate parent
      directory exists.** An existing parent means the neighbourhood is
      real and the absence is genuine drift; a missing parent means the
      citation was probably never root-relative in the first place (prose
      noise, or a path written relative to a subdirectory the author was
      standing in). This is the same "existing parent proves the
      neighbourhood" test `_is_multi_segment_routelike` already uses, and
      it inherits the same bound: a whole-directory rename or delete
      takes its citations down with it and they are silently dropped
      rather than flagged. False negative over fabricated drift, as
      everywhere else in this module.

    Dropped citations leave no trace on the report, matching the
    ambiguous-truncation and spaced-prefix drops in `detect_path_drift`.
    A citation that EXISTS lands in `checked` only — never in `verified`,
    which means "attested AND present" — so a healthy body adds evidence
    without firing any surface's emit gate.

    Budget: `_MAX_ANCHORED_CITATION_STATS` stats per call, counted at the
    stat and not at the match, so filtered-out noise cannot exhaust the
    budget a real citation later in the body needs.

    Misses here ARE claim-anchored, and the filter above is what earns
    that. A raw `_RELATIVE_CITATION_RE` match is prose noise and would
    belong on the advisory side; what reaches the stat has survived the
    directory-segment, host-shape, extension, placeholder and
    live-parent rules AND resolves inside a worktree the memory itself
    recorded. That is a claim about one file in one tree, which is
    exactly the thing the verdict is allowed to escalate on. If the
    filter is ever loosened, this append is what has to be re-argued.
    """
    seen = {_normalize_for_compare(p) for p in checked}
    stats = 0
    for match in _RELATIVE_CITATION_RE.finditer(_bounded_scan_text(body)):
        if stats >= _MAX_ANCHORED_CITATION_STATS:
            break
        cited = match.group(1)
        if not _is_checkable_citation(cited):
            continue
        resolved = _normalize_candidate(str(root / cited))
        if resolved is None:
            continue
        norm = _normalize_for_compare(resolved)
        if norm in seen:
            continue
        seen.add(norm)
        stats += 1
        if _path_exists(resolved):
            checked.append(resolved)
            continue
        parent = os.path.expanduser(os.path.dirname(resolved))
        if not os.path.isdir(parent):
            continue
        checked.append(resolved)
        missing.append(resolved)
        claim_anchored.append(resolved)


def _is_absolute_attestation(s: str) -> bool:
    """True when `s` is already anchored and needs no worktree to resolve.

    Mirrors `_normalize_candidate`'s own accepted anchors, and must keep
    mirroring them: a form this call treats as RELATIVE gets skipped when
    there is no worktree to join it to, so a disagreement between the two
    silently disables the check rather than erroring.

    That is not hypothetical. The first version tested only
    `startswith(("/", "~"))`, which classifies every Windows drive-absolute
    path (`C:\\Users\\me\\thing.toml`, and the forward-slash spelling
    `C:/Users/me/thing.toml` that `Path.as_posix()` produces) as relative —
    so on Windows every absolute attestation fell through the unanchored
    skip and the whole existence check was inert. Caught by CI on
    windows-latest, never by a POSIX developer machine.

    Deliberately NOT platform-gated: a drive path attested on Linux is
    refused there, which is the correct reading of "attest only what you
    checked here". The READ side stays lenient for the synced case.
    """
    if s.startswith(("/", "~")):
        return True
    # `$HOME/` / `${HOME}/` — the env-var spellings of `~/`, which
    # `_normalize_candidate` canonicalizes to `~` before validating.
    # Home-anchored, so no worktree is needed to resolve them.
    if s.startswith(("$HOME/", "${HOME}/")):
        return True
    # Windows drive-absolute, both separators — the same test
    # `_normalize_candidate` applies in its own trailing branch.
    return len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "/\\"


def unverifiable_attestations(
    verified_paths: tuple[str, ...] | list[str],
    *,
    worktree_root: str | Path | None = None,
) -> list[str]:
    """Which `verified_paths` the ATTESTING machine cannot see right now.

    Write-side counterpart to `detect_path_drift`, and the asymmetry
    between them is the whole point. `_normalize_for_compare` documents
    why the READ side tolerates an attested path that isn't on disk: "it
    could have been verified from a different machine", which is true and
    load-bearing for `sync` — a memory attested against a path on host A
    is legitimately read on host B where that path does not exist.

    That tolerance does not transfer to the moment of attestation. A
    caller running `memory_verify(id, verified_paths=[...])` is claiming
    it checked reality HERE and NOW, so a path it cannot stat is not
    evidence of anything — and `Store.mark_verified` previously accepted
    it and stamped `last_verified_at`, which is how a memory reaches
    `fresh` on an attestation that was never true. The read side cannot
    recover this on its own: an ABSOLUTE attested path is only ever
    existence-checked when the body also names it (see
    `_normalize_attestations`' set-membership role in
    `detect_path_drift`), so an attestation the prose never references is
    inert forever.

    Returns the offending paths in input order, resolved to the form that
    was checked, so the caller can name them in an error. Relative paths
    with no `worktree_root` to anchor them are SKIPPED rather than
    reported: unanchored means "could not ask", and the project's
    standing rule is that could-not-ask never manufactures a negative
    verdict (the same distinction `compute_commit_drift` draws by
    returning None).
    """
    root = Path(worktree_root) if worktree_root is not None else None
    bad: list[str] = []
    for raw in verified_paths:
        stripped = raw.strip() if raw else ""
        if not stripped:
            continue
        # Documentation placeholders (`/etc/foo`, `/path/to/thing`) are
        # rejected by `_normalize_candidate` — correctly, for PROSE, where
        # they illustrate path shape rather than cite a file. That veto must
        # not carry over to an explicit attestation: these strings are
        # perfectly stat-able, they simply never name a real file, so
        # attesting one is definitionally the fabricated attestation this
        # function exists to catch. Checked before `_normalize_candidate`
        # gets a chance to silently drop them.
        if _is_placeholder_path(stripped):
            bad.append(stripped)
            continue
        if _is_absolute_attestation(stripped):
            resolved = _normalize_candidate(stripped)
        elif root is not None:
            # Anchor before validating, for the reason `_anchored` gives:
            # `_normalize_candidate` is itself the relative-path gate.
            resolved = _normalize_candidate(str(root / stripped))
        else:
            continue
        if resolved is None:
            # Everything `_normalize_candidate` still rejects here is a
            # shape that cannot be stat'd as a claim at all: a glob or
            # template (`/var/log/*.log`, `/opt/{svc}/data`), a URL, an
            # SSH remote (`user@host:/path`), an SMB share, or a
            # single-segment route (`/healthz`). Refusing those would
            # manufacture a failure out of a caller naming a shape; only a
            # concrete path can be present or absent. Skipped, not refused
            # — and the placeholder branch above is what keeps that
            # exemption from becoming a way to launder a bogus attestation.
            continue
        if not _path_exists(_normalize_for_compare(resolved)):
            bad.append(resolved)
    return bad


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


def _bounded_scan_text(body: str) -> str:
    """Truncate `body` to `_MAX_BODY_SCAN_BYTES` before any regex scan.

    Cut at the LAST WHITESPACE inside the cap, never mid-token: a hard
    slice can bisect a legitimate citation straddling the boundary, and
    the surviving prefix — itself a well-formed path — validates, fails
    the disk check, and FABRICATES a `path_drift_missing` entry (a false
    non-fresh staleness verdict) from a body whose real path exists.
    Dropping the partial tail token keeps the cap's contract honest: it
    only ever DROPS claims, never invents one. (A capped body with no
    whitespace at all keeps the hard slice — a single 32 KiB token is no
    valid path claim and dies at the candidate-length gate.)
    """
    if len(body) <= _MAX_BODY_SCAN_BYTES:
        return body
    truncated = body[:_MAX_BODY_SCAN_BYTES]
    last_ws = max(truncated.rfind(" "), truncated.rfind("\n"), truncated.rfind("\t"))
    return truncated[:last_ws] if last_ws > 0 else truncated


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
    # by `finditer`. Truncation lives in `_bounded_scan_text` (shared with
    # the relative-citation anchor scan).
    body = _bounded_scan_text(body)

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
        # lost. Concrete bite this fixed: a memory body cited `/verify`
        # (a POST route of the since-removed web UI) and the extractor
        # was reading it as a missing filesystem path, producing a
        # phantom `path_drift_missing=1` on every retrieval.
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


def _fold_altsep(p: str, sep: str, altsep: str | None) -> str:
    """Fold the platform's ALTERNATE path separator into the primary one.

    On Windows (``sep="\\\\"``, ``altsep="/"``) the OS accepts both
    characters interchangeably, so ``C:/Users/me/x`` and the mixed
    ``C:\\Users\\me/x`` (the exact shape ``ntpath.expanduser`` returns
    for ``~/x``) are spellings of the backslash-canonical path — the
    same equivalence `_normalize_for_compare` gets from ``pathlib`` on
    that platform. On POSIX ``altsep`` is None and the fold is the
    identity: ``\\`` is a legal filename character there, never a
    separator, and folding it would invent directory boundaries the
    filesystem does not have.

    ``sep`` / ``altsep`` are explicit parameters (the caller passes the
    live ``os.sep`` / ``os.altsep``) so Windows semantics stay
    unit-testable from any platform by passing ``ntpath.sep`` /
    ``ntpath.altsep``.
    """
    return p.replace(altsep, sep) if altsep else p


def _is_under_home(s: str) -> bool:
    """True when `s` expands to the user's home directory or something
    inside it.

    Used to exempt home-rooted candidates from the route rule so the two
    spellings of one path (`~/x/y/z` and `/Users/me/x/y/z`) get the SAME
    verdict — matching how `_normalize_for_compare` already treats them
    everywhere else in this module.

    `os.path` rather than `Path`, deliberately: `os.path.expanduser`
    cannot raise for these inputs, and it keeps the route rule
    independent of the module-level `Path` symbol, which the suite
    patches wholesale when exercising the stat-failure path (a
    `Path.home()` here would blow up under that patch).

    A home that can't be resolved (`expanduser` echoes `~` back when
    `$HOME` is unset) or that IS the filesystem root (`HOME=/`, seen in
    some container images) disables the exemption: treating every
    absolute path as home-rooted would nullify the route rule wholesale.

    SEPARATORS: home, candidate, and the derived prefix are all folded
    through `_fold_altsep` (alternate separator → primary) before any
    comparison, so on Windows the forward-slash and mixed spellings the
    OS accepts — ``C:/Users/me/x/y/z``, ``C:\\Users\\me/x`` — are
    recognised as home-rooted exactly like the backslash form. The raw
    comparison against ``home + os.sep`` used to miss those spellings
    and report them as not home-rooted (a deliberately deferred gap,
    closed here). On POSIX ``os.altsep`` is None, the fold is the
    identity, and behaviour is unchanged — including for filenames that
    legitimately contain ``\\``. The root-home guard runs on the FOLDED
    spelling for the same reason: on Windows ``HOME=/`` folds to
    ``os.sep``, and a root home must keep disabling the exemption or
    every slash-rooted candidate would read as home-rooted and nullify
    the route rule. `_home_ignores_case` receives the folded home; on
    the one platform where folding rewrites anything, both spellings
    stat the same filesystem entry, so the probe's verdict is
    unaffected.

    CASE: the byte comparison runs first and settles every candidate on
    a case-sensitive filesystem. Only when it MISSES do we ask the
    filesystem whether it folds case (`_home_ignores_case`) and retry
    case-insensitively — because on a default macOS APFS volume
    `/users/me/x/y/z` and `/Users/me/x/y/z` are literally the same
    directory, and a byte comparison would exempt one spelling and drop
    the other as a route. That is the same false-negative divergence the
    home escape was added to kill, just one layer down. The probe is
    gated behind the miss: a candidate that is not home-shaped under ANY
    casing is settled by string comparison alone and never touches the
    filesystem, and even a case-modulo match is only probed for
    candidates that already failed their existence check.
    """
    home = os.path.expanduser("~")
    if not home or home == "~":
        return False
    home = _fold_altsep(home, os.sep, os.altsep)
    if home == os.sep:
        return False
    expanded = _fold_altsep(os.path.expanduser(s), os.sep, os.altsep)
    prefix = home if home.endswith(os.sep) else home + os.sep
    if expanded == home or expanded.startswith(prefix):
        return True
    lowered = expanded.lower()
    if lowered != home.lower() and not lowered.startswith(prefix.lower()):
        # Not the same path under ANY casing — no filesystem probe needed.
        return False
    return _home_ignores_case(home)


def _home_ignores_case(home: str) -> bool:
    """True when the filesystem backing `home` resolves paths
    case-insensitively (macOS APFS/HFS+ in their default configuration,
    Windows NTFS, exFAT volumes).

    Probed rather than inferred from `sys.platform`: case sensitivity is
    a per-VOLUME property, not a per-OS one. macOS ships case-insensitive
    by default but case-sensitive APFS is a supported format, Linux
    mounts exFAT/NTFS/SMB shares that fold case, and `os.path.normcase`
    is a no-op on POSIX so it cannot answer this. Asking the actual
    filesystem is the only correct answer.

    The probe is `samefile` against the case-flipped spelling of `home`
    itself: on a folding volume both names stat to one inode; on a
    case-sensitive one the flipped name simply does not exist and
    `samefile` raises, which we read as "case matters here". A home with
    no cased characters at all cannot be probed this way (the flip is a
    no-op and `samefile` would trivially succeed), so it reports False —
    the conservative direction, since without cased characters no
    candidate could have differed by case in the home prefix anyway.

    Read-only: two `stat` calls and no file is created. (Not
    allocation-free — the case flip builds a new string.)
    Deliberately NOT memoised — `$HOME` is read fresh on every call
    upstream (the suite monkeypatches it), and the probe only runs for a
    candidate that already matched home modulo case, which is rare
    enough that a cache would buy nothing but an invalidation hazard.
    """
    flipped = home.swapcase()
    if flipped == home:
        return False
    try:
        return os.path.samefile(home, flipped)
    except (OSError, ValueError):
        # Flipped spelling does not resolve (case-sensitive volume), or
        # home itself is unstattable. Either way: no case folding.
        return False


def _is_multi_segment_routelike(s: str) -> bool:
    """True when a NON-EXISTENT leading-slash candidate is far more likely
    an application route than a file that went missing.

    Only ever consulted for candidates that already failed the existence
    check and carry no attestation, so a real path that still exists is
    never affected, and an attested path is never reached.

    `_is_single_segment_routelike` covers the one-segment case
    (`/healthz`). This covers the multi-segment case — `/api/v1/events/
    presence`, `/admin/macros`, `/portal/incidents/new` — which was
    previously suppressed ONLY when the same body happened to contain a
    domain-qualified URL for `_DOMAIN_ROUTE_RE` to harvest a vocabulary
    from. A memory that cited bare routes got an empty vocabulary and
    every route reported as a missing file, which then inflated
    `staleness_verdict` on an otherwise healthy record.

    Three escapes keep real filesystem drift reportable:

    * **An extension on the terminal segment** (`/srv/app/config.yaml`)
      reads as a file, not a route.
    * **A home-rooted candidate** (`~/projects/old/src`, and its expanded
      twin `/Users/me/projects/old/src`) is never a route: application
      routes are not served out of a user's home directory. Without this
      the rule gated on the RAW spelling, so `~/x/y/z` reported drift
      while the byte-identical `/Users/me/x/y/z` was dropped as a route —
      opposite verdicts for the one path the rest of this module
      deliberately treats as equivalent (`_normalize_for_compare`).
      FALSE-NEGATIVE drift is worse than the false positive this rule was
      built to kill: silence reads as "clean".
    * **An existing parent directory** (`/etc/nope`) means the
      neighbourhood is real, so absence is genuine drift worth reporting.

    BOUND ON THE PARENT ESCAPE (know this before relying on it): it tests
    only the IMMEDIATE parent, so it survives a ONE-LEVEL deletion and
    nothing deeper. Delete or rename a whole project directory and the
    cited path's parent is gone too, so every extensionless citation
    under it is silently dropped. Outside home — say `/srv/docker/gitea`
    once `/srv/docker` is gone — that drop still stands. Walking up to
    the nearest EXISTING ancestor was considered and rejected: the walk
    terminates at `/`, which always exists, so it would nullify the rule
    for every candidate (`/api/v1/events/presence` included) and hand
    back the pre-3.25.2 false positives. The home escape above is the
    targeted fix for the common real-world case (a renamed repo under
    `~`); everything below is the accepted remainder.

    THE ACCEPTED FALSE NEGATIVE, BY SHAPE (not by story). Every
    candidate matching ALL FOUR of these is dropped as a route no matter
    what it actually is:

      1. leading `/`, at least two segments;
      2. no `.` in the TERMINAL segment;
      3. not under `$HOME` on this machine;
      4. its IMMEDIATE parent is not a directory on this machine.

    Say it that way because the shape is all this rule can see. An
    earlier revision of this docstring described the residue as "an
    extensionless remote-host citation", which reads as a promise that
    local paths are safe — they are not, and the difference bites:

      * `/srv/docker/gitea`, `/data/compose/stacks`, `/mnt/tank/media` —
        genuinely another host's filesystem;
      * `/Volumes/My Book/archive/2024` — an entirely LOCAL macOS
        volume that merely happens to be unmounted right now, so its
        parent is absent and it drops;
      * `/home/mattias/scripts/backup` — a home path, but a FOREIGN-OS
        one. Escape (2) keys on this machine's `$HOME` (`/Users/…` on
        macOS), so a Linux-spelled home matches nothing and drops.

    Absence of `/foo/bar` is indistinguishable from absence of
    `/api/v1/thing` using only a local stat, so no local-only heuristic
    can separate these from real routes without handing back the false
    positives. What HAS changed: each of them now lands in
    `PathDriftReport.dropped_as_route`, so an in-process caller holding
    the report can see the suppression. A tool caller usually still
    cannot: cited alone, each of these yields a report whose only
    non-empty bucket is `dropped_as_route`, which no MCP surface emits —
    so for that (common) case they stay as silent as in 3.25.2. Cited in
    a body that ALSO drifts, the bucket rides along on the emitted
    block. See the reach note on `PathDriftReport`.
    `/opt/gophish`-style citations
    survive to `missing` only via the parent escape — because `/opt`
    happens to exist on this host, NOT because of any single-segment
    exclusion: `"/opt/gophish".count("/") == 2`, so the `< 2` guard
    below never fires for that shape. The pinning escape hatch remains
    `memory_verify(verified_absent_paths=[...])`, which routes the
    citation to `expected_absent` before this rule is ever consulted.

    PLATFORM NOTE: the parent test makes this environment-sensitive by
    construction. On Windows no POSIX root exists, so a memory citing
    `/etc/hosts` or `/opt/gophish` reads as a route there while the same
    citation reports drift on the POSIX host that wrote it. That is the
    intended trade — a POSIX path cannot be meaningfully stat'd from
    Windows, so staying silent beats manufacturing a missing-file claim
    the host could never confirm. Tests that assert the unattested
    `missing` half must gate on the root existing.
    """
    if not s.startswith("/") or s.count("/") < 2:
        return False
    parent, _, last = s.rpartition("/")
    if "." in last:
        return False
    if _is_under_home(s):
        return False
    # `os.path.isdir` rather than `Path(...).is_dir()`: it swallows OSError
    # and ValueError internally and simply returns False, so an unstattable
    # parent can never raise out of a read-only drift check. It also keeps
    # this helper independent of the module-level `Path` symbol, which the
    # suite patches wholesale when exercising the stat-failure path.
    return not os.path.isdir(os.path.expanduser(parent))


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

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "commits_since_verify": self.commits_since_verify,
            "recommendation": self.recommendation,
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
       as the claims they spot-checked — the strongest anchor signal);
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
    """
    anchors: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        key = _normalize_for_compare(candidate)
        if not key or key in seen:
            return
        seen.add(key)
        anchors.append(candidate)

    for raw in verified_paths:
        if isinstance(raw, str) and raw:
            _add(raw)
    if body:
        for path, _, _ in _extract_candidates(body):
            _add(path)
        relative_added = 0
        for match in _RELATIVE_CITATION_RE.finditer(_bounded_scan_text(body)):
            if relative_added >= _MAX_ANCHOR_CITATIONS:
                break
            before = len(seen)
            _add(match.group(1))
            if len(seen) > before:
                relative_added += 1
    return tuple(anchors)


@dataclass(frozen=True)
class ResolvedCommitDrift:
    """A resolved commit-drift measurement, claim-narrowing included.

    `count` keeps `resolve_commit_drift_count`'s integer contract (the
    exact number of post-`since` commits that ESCALATE for this memory).
    `claims_checked` / `claims_drifted` carry the claim-level detail the
    display surfaces attach as `claim_drift` — zero/empty whenever the
    memory declares no claims, in which case the count is the incumbent
    per-file measurement unchanged.
    """

    count: int
    claims_checked: int = 0
    claims_drifted: tuple[str, ...] = ()


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
    stream = commit_patch_stream(cwd, shas, specs, toplevel=toplevel)
    if stream is None:
        return [], None
    index = build_binding_index(stream)
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
) -> ResolvedCommitDrift | None:
    """The claim-aware core behind `resolve_commit_drift_count`.

    With no `claims` this is exactly the incumbent per-file narrowing
    (same git calls, same three-valued semantics), wrapped in a
    `ResolvedCommitDrift`. With claims, the anchor set splits into a
    GOVERNED half (files named by at least one claim — toplevel-relative
    by construction, since the declare-time oracle resolved them against
    the origin worktree root) and an UNGOVERNED half (every other
    cited/attested anchor). The ungoverned half keeps the incumbent
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
    the unfiltered/any-touch count, never to silence.
    """
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
    fully governed — the claims are the anchor, which is the entire
    point of declaring them.
    """
    checked = len(claims)
    conservative = ResolvedCommitDrift(unfiltered, checked, ())
    governed = claim_paths(list(claims))
    governed_set = set(governed)
    ungoverned: list[str] = []
    if anchors:
        specs = resolve_repo_pathspecs(cwd, list(anchors), toplevel=toplevel)
        if specs is None:
            # Git itself couldn't answer for the ungoverned half; the
            # governed half would fare no better. Conservative.
            return conservative
        ungoverned = [s for s in specs if s not in governed_set]
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    escalating: set[str] = set()
    every_leg_phantom = True

    if ungoverned:
        pairs = commit_author_sha_pairs_touching_pathspecs(
            cwd, ungoverned, toplevel=toplevel
        )
        if pairs is None:
            return conservative
        if pairs:
            every_leg_phantom = False
            escalating.update(sha for ts, sha in pairs if ts > since)

    drifted: list[str] = []
    gpairs = commit_author_sha_pairs_touching_pathspecs(
        cwd, governed, toplevel=toplevel
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
) -> int | None:
    """Map a positive repo-wide commit count to the claim-anchored count.

    The shared policy step behind all four commit-drift surfaces
    (memory_show via `compute_commit_drift`, the memory_search top-hit
    fold in `_response.attach_commit_drift_counts`, and the two
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
    real, since its removal commit keeps it in the log.
    """
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
        cwd, specs, toplevel=toplevel
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


def compute_commit_drift(
    last_verified_at: datetime | None,
    memory_origin_repo: str | None,
    *,
    caller_origin: Origin | None,
    verified_paths: list[str] | tuple[str, ...] = (),
    body: str = "",
    claims: list[str] | tuple[str, ...] = (),
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
    only runs when the unfiltered count is already positive (mirroring
    the health rollup), and falls back to the unfiltered count when git
    can't answer the path-filtered query — but resolves to None (signal
    not applicable, see above) when the memory has no anchors at all or
    none land inside this repo.
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
    # `commit_author_timestamps` returns timezone-aware datetimes already
    # ascending, so `bisect_right` yields the first index strictly after the
    # verify instant. Equal-instant commits fall before the cut (no drift),
    # matching the health rollup and per-hit search count exactly. The sort
    # lives at the source because THIS function runs once per memory: doing
    # it here re-sorted the repo's whole history per row.
    timestamps = commit_author_timestamps(cwd_path)
    if timestamps is None:
        return None
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
    idx = bisect.bisect_right(timestamps, last_verified_at)
    count = len(timestamps) - idx
    # Claim-anchored narrowing. Anchor derivation is pure CPU (bounded
    # regex over the body) and runs unconditionally so an untethered
    # memory reads consistently as not-applicable; the git-backed
    # resolution + `git log` only run when there's drift to narrow
    # (`count > 0`), mirroring `_compute_commit_drift_debt` / the
    # curation rollup so a caught-up memory never pays a git call and
    # all four surfaces gate on the exact same condition. The narrowed
    # count is measured on AUTHOR date, the same space as the bisect
    # above, so it is a strict subset of `count` — no clamp, and no
    # committer-date boundary to fall back from.
    anchors = commit_drift_anchor_paths(body, verified_paths)
    # Stored claim strings parse leniently — a hand-edited bad entry
    # contributes nothing rather than crashing the hottest read path.
    # A memory whose ONLY anchors are its claims is fully governed: the
    # declaration is the anchor.
    parsed_claims = load_claims(list(claims)) if claims else []
    if not anchors and not parsed_claims:
        return None
    claims_checked = 0
    claims_drifted: tuple[str, ...] = ()
    if count > 0:
        # Resolve the repo root ONCE for this call and thread it through —
        # anchor resolution (`resolve_repo_pathspecs`) and the path-filtered
        # log (`commit_author_timestamps_touching_pathspecs`) would otherwise
        # EACH pay a `git rev-parse --show-toplevel` fork+exec on the hottest
        # read path (every memory_show). Mirrors the batch surfaces
        # (`health._compute_commit_drift_debt`,
        # `_response.attach_commit_drift_counts`), which already thread a
        # once-resolved toplevel. None is tolerated (the resolvers re-derive,
        # preserving the exact conservative fallback), but with
        # `commit_author_timestamps` having just answered, git is
        # demonstrably reachable here.
        toplevel = repo_toplevel(cwd_path)
        resolved = resolve_commit_drift(
            cwd=cwd_path,
            since=last_verified_at,
            unfiltered=count,
            anchors=anchors,
            claims=parsed_claims,
            toplevel=toplevel,
        )
        if resolved is None:
            return None
        count = resolved.count
        claims_checked = resolved.claims_checked
        claims_drifted = resolved.claims_drifted
    if count == 0:
        return CommitDriftStatus(
            status="clean",
            commits_since_verify=0,
            recommendation=None,
            claims_checked=claims_checked,
            claims_drifted=(),
        )
    recommendation = (
        _claim_drift_recommendation(count, claims_drifted)
        if claims_drifted
        else _drift_recommendation(count)
    )
    return CommitDriftStatus(
        status="drift",
        commits_since_verify=count,
        recommendation=recommendation,
        claims_checked=claims_checked,
        claims_drifted=claims_drifted,
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
# which is why "Claims-at-write" sits where it does in
# `docs/ROADMAP.md`. Write-up: `bench/rot/README.md`.
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
    if status == "never":
        # No anchor was ever laid down, so there is no "since when" to
        # measure against and nothing can stand the calendar leg down.
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
    "DEFAULT_VERIFICATION_STALE_DAYS",
    "CommitDriftStatus",
    "PathDriftReport",
    "ResolvedCommitDrift",
    "VerificationStatus",
    "commit_drift_anchor_paths",
    "compute_commit_drift",
    "compute_staleness_verdict",
    "compute_verification_status",
    "detect_path_drift",
    "resolve_commit_drift",
    "resolve_commit_drift_count",
    "verdict_from_signals",
]
