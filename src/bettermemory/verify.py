"""Path-drift detection for retrieved memories.

A memory body that names a file path is making a verifiable claim: "this
path exists on disk and is the thing I'm describing." Filesystems move on,
projects reorganise, scripts get renamed — claims like that go stale long
before the memory's `updated` timestamp would suggest. We can catch a class
of those drifts cheaply at retrieval time by extracting path-shaped tokens
from the body and stat'ing them.

The check is advisory, not blocking. A missing path is surfaced as
`path_drift.missing`; the model decides whether to flag the staleness to
the user, follow up with `memory_verify`, or `memory_update` to correct it.
We never auto-tombstone — drift can be a temporary mount, a cwd we don't
have access to, or a path on a different machine entirely.

Detection coverage is deliberately conservative — better to miss a real
path than chase ghosts:

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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
    """

    checked: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.missing)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "checked": list(self.checked),
            "missing": list(self.missing),
        }


def detect_path_drift(body: str) -> PathDriftReport:
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
    """
    candidates = _extract_candidates(body)
    if not candidates:
        return PathDriftReport(checked=(), missing=())

    checked: list[str] = []
    missing: list[str] = []
    for path in candidates:
        checked.append(path)
        if not _path_exists(path):
            missing.append(path)
    return PathDriftReport(checked=tuple(checked), missing=tuple(missing))


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

    if s.startswith("~/"):
        return s if len(s) > 2 else None
    if s.startswith("/"):
        # `/x` is too short to be a meaningful claim; `/` alone is the root.
        return s if len(s) >= 3 else None
    # Windows drive: `C:\` or `C:/`
    if len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "/\\":
        return s
    return None


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


__all__ = [
    "PathDriftReport",
    "detect_path_drift",
]
