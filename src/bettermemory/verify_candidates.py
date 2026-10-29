"""Path-candidate extraction for `verify.py`: the caps, the scans, the memo.

The per-body caps and citation budgets, the extraction regexes with their
ReDoS notes, the candidate scan (`_scan_candidates`) and validator
(`_normalize_candidate`) with the route, home and placeholder rules,
`_normalize_for_compare`, `_path_exists`, and the body parse memo
(`_CANDIDATES`, `_ANCHORS`, `BODY_PARSE_ENTRIES`) with its registered
clearer. A test that patches `_scan_candidates`, `BODY_PARSE_ENTRIES` or
`Path` for the existence check patches this module, where the readers are.
It lives apart from `verify.py` to keep each module under the package's
2,000-line bound (ENG-96). It imports nothing from the verify family,
which imports it; `verify.py` re-exports the names callers import from it.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import _caches


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
# with every regex linear (`_DOMAIN_ROUTE_RE` starts once per token). Real
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
# A match starts only where a token starts: the lookbehind bars a word
# character, a dot or a dash before the first label, and the optional
# leading dot lets ` .example.com/route` count. That anchor is what keeps
# the scan linear. This regex is `finditer`'d over the ENTIRE raw body
# inside `_extract_candidates`, which runs per search hit on a daemon
# worker. The earlier `\b` opener let a match start at every label of a
# dotted run and at every letter of a dashed run (`a-a-a...`), and each
# start rescanned the rest of the token, so a 32 KiB dashed body (inside
# `_MAX_BODY_SCAN_BYTES`) took about 9 s while holding the GIL, stalling
# all four workers; a `{1,20}` label bound covered the dotted shape and
# not the dashed one. A match now starts at a token only: not after a
# word character or a dash (`(?<![\w-])`), and not at a later label of a
# dotted run (`(?<![\w-]\.)`: such a label follows `label.`), so the
# engine's backtracking stays inside one token, since `[\w-]` and `.`
# are disjoint and a shorter label never opens a new partition: about
# 1 ms on the same body and 2.0x at twice the length (measured by hand;
# `test_domain_route_regex_linear_on_dash_runs` pins a 0.5 s ceiling). A
# start after a run of two or more dots (`see...example.com/x`) stays
# open, as it was under `\b`, so a route glued to such a run is still
# found and the same path elsewhere in the body stays suppressed. The
# label repetition is unbounded on purpose: with no restart at a later
# label, a bound would drop the route of a domain longer than the bound,
# and it buys nothing once the start is anchored.
_DOMAIN_ROUTE_RE = re.compile(
    r"(?<![\w-])(?<![\w-]\.)\.?[\w-]+(?:\.[\w-]+)+/([\w.\-]+)"
)

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
# - Every quantifier is bounded (`_DOMAIN_ROUTE_RE` is linear by its
#   anchored start instead): iterations are separated by a literal `/`, so
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

# Stat budget for the ANCHORED CITATION pass
# (`verify_anchoring._check_anchored_citations`), reconciling the two caps
# above rather than picking one. They are different currencies:
# `_MAX_ANCHOR_CITATIONS` (24) bounds pathspec STRINGS handed to one
# `git log` invocation, while every citation that pass checks costs a
# `stat()` on the hottest retrieval path — the same currency
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


def _claim_key(path: str) -> str:
    """The identity of a claimed path in a read's seen, absent and verified
    sets: the ``~``-expanded path resolved without requiring it to exist
    (a non-strict ``realpath``), in this platform's case
    (``os.path.normcase``).

    One form for every set, so identity and containment read the same
    path (ENG-162). The containment test resolves (`_resolves_under`), and
    the sets keyed on the lexical `_normalize_for_compare`, so with a root
    spelled ``/tmp/x`` (a link to ``/private/tmp/x`` on macOS) and an
    absence attested as ``/private/tmp/x/src/gone.py``, the attestation
    pass read the file ``expected_absent`` while the citation pass, keyed
    on ``/tmp/x/src/gone.py``, read the same file again and reported it
    ``claim_anchored_missing``, the one path signal that escalates a
    verdict. On Windows a short 8.3 name or another case of a component
    is the same mismatch, and ``normcase`` folds the case.

    Resolved at read time, never memoised with a body's parse: what a
    link points to is the disk's, not the body's. A spelling this
    platform cannot read as absolute (a Windows drive path read on POSIX)
    keeps its expanded spelling: resolving it would join it onto the
    working directory. A path that cannot be resolved (an embedded NUL)
    keeps its expanded spelling too, and a spelling the path gate refuses
    (`_refused_spelling`) is never resolved: on Windows ``\\\\host\\x`` is
    absolute, and resolving it opens the host (ENG-164). A read keys its
    paths through `_ClaimKeys`, which keys each path once."""
    if not path:
        return ""
    if _refused_spelling(path):
        return os.path.normcase(path)
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return os.path.normcase(expanded)
    try:
        return os.path.normcase(os.path.realpath(expanded))
    except (OSError, ValueError):
        return os.path.normcase(expanded)


class _ClaimKeys:
    """`_claim_key` for one read: each path keyed once, and off Windows
    each directory on the way resolved once.

    A read keys the same paths in several passes (the body's candidates,
    the attestations, the citations, the seen set each pass dedupes
    against), and a non-strict ``realpath`` makes one ``lstat`` per
    component. Keyed afresh at each use, a read of a record nine
    directories deep made about 400 ``lstat`` calls where the lexical keys
    before ENG-162 made none, and each one is a call that blocks on a
    mount that stopped answering. Here a path is keyed once per read and,
    on POSIX, resolved as ``posixpath.realpath`` resolves it, one
    component at a time on top of its parent's memoised resolution: a
    directory the read's paths share is resolved once, and each further
    component costs one ``lstat`` (is it a link). A link is resolved by
    ``realpath`` itself. Windows resolves a path through one handle open
    (``GetFinalPathNameByHandle``), not per component, so there each path
    is resolved by ``realpath`` once per read. Never shared across reads:
    what a link points to is the disk's, read when the claims are read.
    A refused spelling (`_refused_spelling`) is never resolved."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._resolved: dict[str, str] = {}

    def __call__(self, path: str) -> str:
        key = self._keys.get(path)
        if key is None:
            key = self._keys[path] = self._key(path)
        return key

    def _key(self, path: str) -> str:
        if not path:
            return ""
        if _refused_spelling(path):
            return os.path.normcase(path)
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            return os.path.normcase(expanded)
        try:
            if os.name == "nt":
                return os.path.normcase(os.path.realpath(expanded))
            return os.path.normcase(self._resolve(expanded))
        except (OSError, ValueError):
            return os.path.normcase(expanded)

    def _resolve(self, path: str) -> str:
        """``os.path.realpath(path)`` for an absolute POSIX `path`, its
        parent read from the memo."""
        held = self._resolved.get(path)
        if held is not None:
            return held
        parent, name = os.path.split(path)
        if not name:
            resolved = (
                os.path.realpath(path) if parent == path else self._resolve(parent)
            )
        elif name == ".":
            resolved = self._resolve(parent)
        elif name == "..":
            resolved = os.path.dirname(self._resolve(parent))
        else:
            joined = os.path.join(self._resolve(parent), name)
            resolved = os.path.realpath(joined) if os.path.islink(joined) else joined
        self._resolved[path] = resolved
        return resolved


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# The body parse memo. A verdict parses a memory's body twice: the path
# drift leg extracts its path candidates (`detect_path_drift`), and the
# commit leg extracts them again, with the relative citations, for the
# claim anchors (`commit_drift_anchor_paths`). Both results are functions
# of the body alone (the anchors also of the attestations), and of the
# home directory `~` expands to when candidates are compared; a body
# changes only when its memory is written or updated, so a warm brief or
# search parsed every row again for the same answer. The memo keeps each
# result keyed on a digest of the body, the attestations the anchors read,
# and the home directory: an edited body digests differently and is
# parsed afresh, so no result is served for a body it was not computed
# from. Results are tuples and each caller gets its own list, so no caller
# can change a kept one. Bounded to the most recently used
# BODY_PARSE_ENTRIES results of each kind (0 keeps nothing); a key holds a
# 16-byte digest, never the body. The lock guards each look-up and each
# store, the parse runs outside it: two threads that miss on the same body
# parse it once each and store equal values. Registered with `_caches`,
# which empties it before each test.
BODY_PARSE_ENTRIES = 4096

_BodyKey = tuple[bytes, tuple[str, ...], str]
_CANDIDATES: OrderedDict[_BodyKey, tuple[tuple[str, bool, bool], ...]] = OrderedDict()
_ANCHORS: OrderedDict[_BodyKey, tuple[str, ...]] = OrderedDict()
_BODY_PARSE_LOCK = threading.Lock()


@_caches.register
def _clear_body_parses() -> None:
    with _BODY_PARSE_LOCK:
        _CANDIDATES.clear()
        _ANCHORS.clear()


def _body_key(body: str, attested: tuple[str, ...] = ()) -> _BodyKey:
    """The memo key for `body` read with `attested`: the body's digest,
    the attestations, and the directory `~` expands to now."""
    digest = hashlib.blake2b(
        body.encode("utf-8", "surrogatepass"), digest_size=16
    ).digest()
    return (digest, attested, os.path.expanduser("~"))


def _recall_parse(memo: OrderedDict[_BodyKey, Any], key: _BodyKey) -> Any | None:
    with _BODY_PARSE_LOCK:
        found = memo.get(key)
        if found is not None:
            memo.move_to_end(key)
        return found


def _keep_parse(memo: OrderedDict[_BodyKey, Any], key: _BodyKey, value: Any) -> None:
    if BODY_PARSE_ENTRIES <= 0:
        return
    with _BODY_PARSE_LOCK:
        memo[key] = value
        memo.move_to_end(key)
        while len(memo) > BODY_PARSE_ENTRIES:
            memo.popitem(last=False)


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
    """`_scan_candidates(body)`, through the body parse memo
    (`_CANDIDATES`)."""
    if not body:
        return []
    key = _body_key(body)
    found: tuple[tuple[str, bool, bool], ...] | None = _recall_parse(_CANDIDATES, key)
    if found is None:
        found = tuple(_scan_candidates(body))
        _keep_parse(_CANDIDATES, key, found)
    return list(found)


def _scan_candidates(body: str) -> list[tuple[str, bool, bool]]:
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


def _trim_candidate(raw: str) -> str:
    """Strip, unescape and de-punctuate `raw`; return the spelling the checks read."""
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
    return s


def _command_shaped(s: str) -> bool:
    """Read the spaced candidate `s`; return True when it is a CLI line, not a path."""
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
            return True
    return False


def _anchored_candidate(s: str) -> str | None:
    """Read `s` by its anchor (home, root or drive); return it, or None if no path."""
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

    s = _trim_candidate(raw)

    if not s or len(s) > _MAX_PATH_LENGTH:
        return None

    # The path gate (`_refused_spelling`), before any branch below can hand
    # the spelling on to a stat: a network or device spelling is not a
    # citation, whatever follows it.
    if _refused_spelling(s):
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

    if _command_shaped(s):
        return None

    return _anchored_candidate(s)


def _refused_spelling(s: str) -> bool:
    """True when `s` opens with two separators, either slash in either
    place (``//``, ``/\\``, ``\\/``, ``\\\\``): a spelling no read may stat.

    The path gate (ENG-164). On Windows each of these is a UNC path
    (``\\\\host\\share``) or a device-namespace one (``\\\\.\\pipe``,
    ``\\\\?\\UNC``), whatever the slashes: a stat of one opens an SMB
    connection to the host it names, which hands the host the user's NTLM
    hash and holds the worker while an unreachable host times out. A
    memory body is written by whatever the model was shown, so a cited
    ``/\\evil.example\\share\\x.txt`` would be stat'd on every retrieval
    that rendered the hit. On POSIX ``//host/share`` is SMB/CIFS
    mount-source notation (mount_smbfs, fstab, smbclient), a network-share
    spec and never a local claim, which this module refused before the
    gate. A spelling that holds a NUL byte (no read can stat it) or a
    URL's ``://`` (a scheme, never a path on this disk: ``file://host/x``
    joined onto a root names nothing the claim meant) is refused the same
    way. Checked on the spelling, before ``~`` is expanded and before any
    join, so a refused spelling never becomes a candidate, an attestation
    or a pathspec: it is not a citation, and the verdict reads as if the
    body never named it. A home on a network share is the user's own and
    is not refused, since ``~/`` is expanded only after this check.
    """
    return (len(s) >= 2 and s[0] in "/\\" and s[1] in "/\\") or "\0" in s or "://" in s


_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _escapes_root(rel: str) -> bool:
    """True when the relative spelling `rel`, joined onto a root, would
    name a path outside it, read lexically: rooted (a leading separator,
    which on Windows keeps only the root's drive), drive-qualified
    (``C:x``, which on Windows leaves the root's drive), or a ``..`` that
    climbs above the root at any point of the walk (``src/../../x``, even
    when a later segment comes back in by name).

    The other half of the path gate (ENG-164): every relative citation and
    attestation is joined onto a root and stat'd there, and a join is a
    claim about that one tree only while it stays inside it. Either slash
    separates, on every platform: a relative claim recorded on Windows is
    read on POSIX, and a ``\\`` that POSIX keeps in a file name refuses
    nothing a citation needs. Lexical, so no stat runs to decide it; a
    symbolic link inside the tree that points out of it is the reads'
    own concern (`_resolves_under` reads it resolved). A spelling the gate
    refuses outright (`_refused_spelling`: a NUL byte, a URL) is never
    joined either."""
    if _refused_spelling(rel):
        return True
    folded = rel.replace("\\", "/")
    if folded.startswith("/") or _DRIVE_PREFIX_RE.match(folded):
        return True
    depth = 0
    for part in folded.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


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
