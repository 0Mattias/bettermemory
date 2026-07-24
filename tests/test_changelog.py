"""CHANGELOG hygiene lint.

The 2.6.2 and 2.6.3 audit passes both found `## <version> -` headings
that had silently disappeared from CHANGELOG.md (1.3.0, 1.2.1, 2.6.0).
The narrative bodies were intact but renderers walking the heading
hierarchy stitched the prose of one release into the next, making
release notes useless for the missing entries.

This test pins three invariants:

1. The version in ``pyproject.toml`` has a matching ``## <version> -``
   heading in ``CHANGELOG.md``. If you bump pyproject without writing
   the entry, the suite fails before the release tag goes out.

2. Every ``## <version> -`` heading parses as a valid semver
   ``X.Y.Z - YYYY-MM-DD`` line. A malformed heading (missing date,
   trailing junk, wrong dash) trips the lint instead of silently
   landing in the rendered output.

3. Every non-merge commit inside the NEWEST release tag's window
   (``prev_tag..tag``) is represented in that release's entry — by a
   short-SHA citation or by lexical overlap between the commit subject
   and the entry body — unless its conventional-commit type or scope
   marks it trivial. Tag ``v3.24.0`` carried commit ``096218e`` (a
   perf rework of the by-id lookup path) with no entry anywhere in
   this file; the omission shipped and had to be repaired by erratum.
   This invariant turns that class into a test failure while the tag
   is still local. Only the newest window is judged: older entries
   are a frozen record that summarizes thematically, and re-litigating
   them would produce exactly the allowlist churn the doc-claims
   checker's tiering exists to avoid.

We deliberately *don't* try to assert monotonicity or gap-free patch
series — release branches diverge and rebase, and a "1.5.0 then
2.0.0 then 1.5.1" sequence (where 1.5.1 is a backport) is legal even
if rare. The invariants above cover the actual classes of bug the
audit cycle keeps surfacing.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


_HEADING_RE = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+(?:[-+][\w.]+)?) - (?P<date>\d{4}-\d{2}-\d{2})$"
)


def _changelog_headings() -> list[tuple[str, str, int]]:
    """Return ``(version, date, line_number)`` for every ``## ...`` heading.

    Lines that *start with* ``## `` but don't match the canonical
    ``X.Y.Z - YYYY-MM-DD`` shape are excluded from the return — they're
    flagged separately by ``test_all_version_headings_well_formed``.
    """
    out: list[tuple[str, str, int]] = []
    for i, line in enumerate(_CHANGELOG.read_text(encoding="utf-8").splitlines(), 1):
        m = _HEADING_RE.match(line)
        if m:
            out.append((m["version"], m["date"], i))
    return out


def _pyproject_version() -> str:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def test_pyproject_version_has_matching_changelog_heading() -> None:
    """The version currently in ``pyproject.toml`` MUST have an entry.

    This is the 2.6.4 audit follow-up: 1.3.0, 1.2.1, and 2.6.0 all
    shipped at one point without their ``## <version> -`` heading
    because the changelog body got written but the heading didn't.
    Anchoring to ``pyproject.toml`` means the next release that bumps
    the version without adding the heading trips the suite.
    """
    expected = _pyproject_version()
    headings = {v for v, _, _ in _changelog_headings()}
    assert expected in headings, (
        f"CHANGELOG.md is missing a `## {expected} - YYYY-MM-DD` heading "
        f"for the current pyproject.toml version. Add it before "
        f"tagging the release. Found headings: "
        f"{sorted(headings, reverse=True)[:5]} (and {len(headings) - 5} more)."
    )


def test_all_version_headings_well_formed() -> None:
    """Every ``## `` heading that looks like a version line MUST match
    the canonical ``X.Y.Z - YYYY-MM-DD`` shape.

    Catches typos that would otherwise silently land — e.g.
    ``## 2.6.3-2026-05-21`` (no spaces around the dash), ``## 2.6.3``
    (missing date), ``## 2.6.3 — 2026-05-21`` (em-dash not hyphen).
    Each of these renders differently and breaks downstream parsers.
    """
    bad: list[tuple[int, str]] = []
    version_like = re.compile(r"^## \d+\.\d+")
    for i, line in enumerate(_CHANGELOG.read_text(encoding="utf-8").splitlines(), 1):
        if not version_like.match(line):
            continue
        if not _HEADING_RE.match(line):
            bad.append((i, line))
    if bad:
        msg = "\n".join(f"  line {i}: {line!r}" for i, line in bad)
        pytest.fail(
            f"CHANGELOG.md has version-like headings that don't match the "
            f"canonical `## X.Y.Z - YYYY-MM-DD` shape:\n{msg}"
        )


def test_plugin_marketplace_version_matches_pyproject() -> None:
    """Three places carry the version: ``pyproject.toml``,
    ``plugin/.claude-plugin/plugin.json``, and
    ``.claude-plugin/marketplace.json``. The 2.6.2 release notes call
    out keeping all three in sync as a recurring foot-gun. Pin it.
    """
    import json

    expected = _pyproject_version()
    plugin_json = json.loads(
        (_REPO_ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    marketplace = json.loads(
        (_REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert plugin_json.get("version") == expected, (
        f"plugin/.claude-plugin/plugin.json version "
        f"{plugin_json.get('version')!r} != pyproject.toml {expected!r}"
    )
    # marketplace.json carries the version at `metadata.version` at the
    # marketplace level (the per-plugin entries omit version because
    # the marketplace metadata is what Claude Code reads).
    market_version = (marketplace.get("metadata") or {}).get("version")
    assert market_version == expected, (
        f".claude-plugin/marketplace.json metadata.version "
        f"{market_version!r} != pyproject.toml {expected!r}"
    )


# --- Release-window commit coverage -----------------------------------------
#
# The machinery below implements invariant 3 from the module docstring.
# Everything is deliberately deterministic: the same repo state always
# produces the same verdict, so a failure is a fact about the CHANGELOG,
# never about the environment. Environmental gaps (no git, no tags,
# shallow history) SKIP instead.

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# Conventional-commit subject: type, optional (scope,...), optional "!".
_SUBJECT_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scopes>[^)]*)\))?!?: (?P<tail>.+)$"
)

# Commit types that never owe the release notes an entry. `release` is
# the version-bump commit itself; `bench` matches this repo's usage
# (benchmark harness work is narrated in commit messages, not notes).
_EXEMPT_TYPES = frozenset(
    {"bench", "build", "chore", "ci", "docs", "release", "style", "test", "tests"}
)

# A substantive TYPE whose every scope is test/docs/ci/bench tooling is
# still trivial for release-note purposes: this repo spells test-suite
# repairs `fix(test)` and doc repairs `fix(docs)`.
_EXEMPT_SCOPES = frozenset({"bench", "ci", "doc", "docs", "test", "tests"})

# Function words plus prose glue that carries no identity. Number words
# are dropped for the same reason bare digits are: "three new surfaces"
# identifies a change by "surfaces", not by "three".
_STOPWORDS = frozenset(
    """
    a an and are as at be been being both but by can cannot could did do
    does don down each eight every five for four from get got had has have
    her his how in into is it its just keep kept least less let lets made
    make makes more most new nine no nor not now of off on one only onto or
    our out over own per same seven should six so some still stop stops
    ten than that the their them then these they this those three to too
    two under until up use used uses using very via was were what when
    where which while who whose why will with within without would yet
    your
    """.split()
)

# One inflection strip, longest suffix first, keeping at least three
# characters of stem — enough to unite "corroborate"/"corroboration"
# and "markers"/"marker" without a stemming dependency.
_SUFFIXES = (
    "ations",
    "ation",
    "ating",
    "ates",
    "ated",
    "ate",
    "ities",
    "ity",
    "ments",
    "ment",
    "ings",
    "ing",
    "ions",
    "ion",
    "ies",
    "ers",
    "er",
    "ed",
    "es",
    "s",
)

_WORD = re.compile(r"[a-z0-9_]+")

# Unigram fallback: nearly the whole subject must appear, and never
# fewer than this many words. The primary lexical path is the shared
# bigram — an entry that actually documents a commit reuses at least
# one two-word phrase from its subject, while single generic words
# ("search", "verdict") recur in almost every entry of this project
# and must not count as coverage on their own.
_UNIGRAM_FLOOR = 4


def _git_lines(*args: str) -> list[str] | None:
    """Run git against the repo root; ``None`` means "cannot answer".

    Any failure — no git binary, not a checkout, shallow history that
    cannot resolve a range — collapses to ``None`` so the caller skips.
    A missing answer is an environment fact, not a CHANGELOG defect.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


def _newest_tag_window() -> tuple[str, str, str] | None:
    """``(version, prev_tag, newest_tag)`` for the two highest release tags.

    Only strict ``vX.Y.Z`` tags participate; release candidates or
    oddly-shaped tags never define a window. Ordering is by semver
    tuple, not tag creation date, so a late-pushed backport tag cannot
    hijack the window. ``None`` when fewer than two such tags are
    visible — which is every shallow CI checkout.
    """
    lines = _git_lines("tag", "--list", "v[0-9]*")
    if lines is None:
        return None
    versioned: list[tuple[tuple[int, int, int], str]] = []
    for raw in lines:
        m = _TAG_RE.match(raw.strip())
        if m:
            versioned.append(((int(m[1]), int(m[2]), int(m[3])), raw.strip()))
    if len(versioned) < 2:
        return None
    versioned.sort()
    (_, prev_tag), ((major, minor, patch), newest_tag) = versioned[-2], versioned[-1]
    return f"{major}.{minor}.{patch}", prev_tag, newest_tag


def _window_commits(prev_tag: str, newest_tag: str) -> list[tuple[str, str]] | None:
    """``(sha, subject)`` for every non-merge commit in ``prev..newest``."""
    lines = _git_lines(
        "log", "--no-merges", "--format=%H%x00%s", f"{prev_tag}..{newest_tag}"
    )
    if lines is None:
        return None
    commits: list[tuple[str, str]] = []
    for raw in lines:
        sha, sep, subject = raw.partition("\x00")
        if sep:
            commits.append((sha, subject))
    return commits


def _entry_text(version: str) -> str | None:
    """The CHANGELOG section for ``version``: its heading to the next ``## ``."""
    lines = _CHANGELOG.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m["version"] == version:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def _classify_subject(subject: str) -> tuple[bool, str]:
    """``(exempt, tail)`` — trivial-by-convention, and the words to match.

    A subject that does not parse as a conventional commit is treated
    as substantive: an unclassifiable change is exactly the one that
    should not slip through unexamined.
    """
    m = _SUBJECT_RE.match(subject)
    if m is None:
        return False, subject
    if m["type"] in _EXEMPT_TYPES:
        return True, ""
    raw_scopes = m["scopes"]
    if raw_scopes:
        scopes = [s.strip() for s in raw_scopes.split(",") if s.strip()]
        if scopes and all(s in _EXEMPT_SCOPES for s in scopes):
            return True, ""
    return False, m["tail"]


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _distinctive(words: list[str]) -> list[str]:
    return [w for w in words if len(w) >= 3 and not w.isdigit() and w not in _STOPWORDS]


def _unigram_hit(token_stem: str, entry_stems: frozenset[str]) -> bool:
    if token_stem in entry_stems:
        return True
    if len(token_stem) < 4:
        return False
    # Prefix forgiveness unites inflections one truncation can't, e.g.
    # a stem pair like ("writ", "write"), and lets a bare word find its
    # snake_case surface spelling in the entry.
    return any(
        len(e) >= 4 and (e.startswith(token_stem) or token_stem.startswith(e))
        for e in entry_stems
    )


def test_newest_tag_window_commits_are_represented() -> None:
    """Every substantive commit in the newest tag's window has an entry trace.

    What "represented" means here, in order of authority:

    1. The commit's 7+ character SHA prefix appears in the entry — the
       deterministic escape hatch for an entry that deliberately
       paraphrases (this is how the erratum convention cites commits).
    2. The entry reuses a two-word phrase from the commit subject
       (stem-equal adjacent word pair).
    3. Nearly all of the subject's distinctive words appear in the
       entry (all but one, and no fewer than ``_UNIGRAM_FLOOR``).

    The bar is "zero-representation is impossible", not "the entry is
    good prose". Lexical overlap can in principle be satisfied
    coincidentally, and a heavily-paraphrased real entry can miss both
    lexical tiers — the failure message names the SHA hatch for that
    case. Merge commits, trivially-typed commits, and commits whose
    every scope is test/docs/ci/bench tooling are out of scope. Skips
    when git, two release tags, or the window's history are missing
    (shallow CI checkouts); the moment it has teeth is a full checkout
    right after ``git tag``, before the tag is pushed — see the release
    runbook in ``docs/release.md``.
    """
    window = _newest_tag_window()
    if window is None:
        pytest.skip(
            "needs a git checkout with two or more vX.Y.Z tags visible "
            "(shallow CI checkouts have at most the pushed tag)"
        )
    version, prev_tag, newest_tag = window
    commits = _window_commits(prev_tag, newest_tag)
    if commits is None:
        pytest.skip(
            f"cannot enumerate {prev_tag}..{newest_tag} "
            f"(shallow or partial git history)"
        )

    entry = _entry_text(version)
    assert entry is not None, (
        f"tag {newest_tag} exists but CHANGELOG.md has no "
        f"`## {version} - YYYY-MM-DD` entry for it — the release shipped "
        f"with no notes at all"
    )
    entry_words = _WORD.findall(entry.lower())
    entry_stems = frozenset(_stem(w) for w in entry_words)
    entry_bigrams = frozenset(
        (_stem(a), _stem(b)) for a, b in zip(entry_words, entry_words[1:])
    )
    entry_lower = entry.lower()

    offenders: list[str] = []
    for sha, subject in commits:
        exempt, tail = _classify_subject(subject)
        if exempt:
            continue
        if sha[:7] in entry_lower:
            continue
        words = _WORD.findall(tail.lower())
        tokens = _distinctive(words)
        pairs = {
            (_stem(a), _stem(b))
            for a, b in zip(words, words[1:])
            if a in tokens and b in tokens
        }
        if pairs & entry_bigrams:
            continue
        matched = [t for t in tokens if _unigram_hit(_stem(t), entry_stems)]
        if tokens and len(matched) >= max(_UNIGRAM_FLOOR, len(tokens) - 1):
            continue
        offenders.append(
            f"  {sha[:9]}  {subject!r}\n    entry words matched: {matched or 'none'}"
        )

    if offenders:
        listing = "\n".join(offenders)
        pytest.fail(
            f"The `## {version}` CHANGELOG entry does not represent every "
            f"substantive commit in {prev_tag}..{newest_tag}:\n{listing}\n"
            f"Each commit above needs either a mention in the {version} "
            f"entry that reuses a two-word phrase from its subject, or an "
            f"explicit short-SHA citation there (the erratum convention). "
            f"This is the v3.24.0/`096218e` omission class — see the module "
            f"docstring."
        )
