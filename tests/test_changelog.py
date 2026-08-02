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

Invariant 3's judge is separated from its git plumbing
(``_unrepresented``) so that self-tests below can drive it with
synthetic commits, and each of its tiers is then neutered in turn to
prove one of those fixtures notices. That discipline is borrowed from
``tests/test_doc_claims.py`` and ``tests/test_platform_fixture_lint.py``,
and for the reason both of them state: a rule the current tree happens
to satisfy is indistinguishable from a rule that does nothing, so the
green tick on the live corpus is not by itself evidence of anything.

Those self-tests cover invariant 3, which is where the policy lives.
Invariant 1 compares two values read from two files with nothing in
between to go slack. Invariant 2 does have one soft spot — its
``version_like`` prefilter would pass vacuously if it ever stopped
matching — left unpinned deliberately: a fixture for one anchored
prefix regex costs more than it pins.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


_HEADING_RE = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+(?:[-+][\w.]+)?) - (?P<date>\d{4}-\d{2}-\d{2})$"
)

# What COUNTS as a version heading for the well-formedness check, as
# opposed to what a well-formed one looks like (`_HEADING_RE`). Kept
# deliberately loose — tolerant of the missing/extra space and the `v`
# prefix the tag names invite — because anything this misses is not
# reported as malformed, it is not examined at all. See
# `test_all_version_headings_well_formed`.
_VERSION_LIKE_HEADING = re.compile(r"^##\s*v?\d+\.\d+")


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

    The prefilter deciding what "looks like a version line" has to be
    WIDER than the canonical shape, or the check passes vacuously on
    exactly the headings it is for: a line the prefilter skips is never
    compared to `_HEADING_RE` at all, so the more mangled a heading is,
    the likelier it was to escape. `^## \\d+\\.\\d+` required the single
    space and a leading digit, which let three shapes through — measured,
    and pinned in `test_version_heading_prefilter_is_wider_than_canonical`
    below. The one that matters is `## v3.29.0 - ...`: every tag in this
    repo is named `vX.Y.Z`, so an author writing the heading beside
    `git tag -a v3.29.0` is one keystroke from a heading no guard reads.

    Only the CURRENT version is covered elsewhere, by two guards that
    read the headings differently. A mangled heading hides the version
    from `_changelog_headings`, failing
    `test_pyproject_version_has_matching_changelog_heading` — the one
    that always runs. It also makes `_entry_text` return None, failing
    `test_newest_tag_window_commits_are_represented`, but only where that
    test does not skip: it needs the tag pushed, two strict `vX.Y.Z`
    tags, and enough history to enumerate the window. Historical
    headings have this test and nothing else — and rotting historical
    headings are the documented 1.3.0 / 1.2.1 / 2.6.0 class, bodies
    intact with the `##` line silently gone.
    """
    bad: list[tuple[int, str]] = []
    version_like = _VERSION_LIKE_HEADING
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


def test_version_heading_prefilter_is_wider_than_canonical() -> None:
    """The guard above can only fail on a line its prefilter admits, so
    the prefilter is load-bearing and is pinned separately here.

    Every malformation listed is rejected by `_HEADING_RE`, so each one
    MUST be admitted by the prefilter — otherwise
    `test_all_version_headings_well_formed` returns green without ever
    having looked at it. The last three are the shapes the original
    `^## \\d+\\.\\d+` let through.

    The negative cases matter as much: widening the prefilter until it
    swallows the sub-headings that structure every entry would turn the
    guard from vacuous into permanently red.
    """
    malformed = [
        "## 2.6.3",  # missing date
        "## 2.6.3-2026-05-21",  # no spaces around the dash
        "## 2.6.3 — 2026-05-21",  # em-dash, not hyphen
        "## v3.29.0 - 2026-07-30",  # `v` prefix, as the tags are named
        "##3.29.0 - 2026-07-30",  # no space after the hashes
        "##  3.29.0 - 2026-07-30",  # two spaces after the hashes
    ]
    for line in malformed:
        assert not _HEADING_RE.match(line), f"{line!r} should not be canonical"
        assert _VERSION_LIKE_HEADING.match(line), (
            f"{line!r} is malformed but the prefilter skips it, so "
            f"test_all_version_headings_well_formed never examines it and "
            f"passes vacuously. Widen `_VERSION_LIKE_HEADING`."
        )

    assert _VERSION_LIKE_HEADING.match("## 3.29.0 - 2026-07-30")
    for line in (
        "### Fixed — `sync` could destroy uncommitted work",
        "### Added — machinery that re-derives instead of restating",
        "## Changelog",
        "#### 3.29.0",
    ):
        assert not _VERSION_LIKE_HEADING.match(line), (
            f"the prefilter admits {line!r}, which is not a version heading; "
            f"every one of these would be reported as malformed forever"
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


def _unrepresented(commits: list[tuple[str, str]], entry: str) -> list[str]:
    """Commits with no trace in ``entry``, rendered one per line.

    The whole judgement of invariant 3, deliberately pure: no git, no clock,
    no filesystem. That is what lets the self-tests at the bottom of this
    file drive it with synthetic commits and a synthetic entry — and it is
    the only reason the live test below is worth anything, because a rule
    that today's CHANGELOG happens to satisfy is indistinguishable from a
    rule that exempts every commit unconditionally.
    """
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
    return offenders


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

    This test passing says nothing on its own: it is green whenever the
    newest entry happens to cover its window, and equally green against
    a judge that exempts every commit. The self-tests below are what
    make it evidence — see ``_NEUTERINGS``.
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
    offenders = _unrepresented(commits, entry)
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


# --- Self-tests for the coverage judge ---------------------------------------
#
# Everything above this line is a verdict on one repo state. A rule that
# today's CHANGELOG satisfies is indistinguishable from a rule that does
# nothing, which is the argument `tests/test_doc_claims.py` and
# `tests/test_platform_fixture_lint.py` both make about their own extractors —
# and which this file shipped without. So each tier below gets a fixture that
# fires and a paired fixture that stays quiet, and `_NEUTERINGS` then breaks
# each tier in turn and asserts one of those fixtures notices.
#
# The fixtures never touch CHANGELOG.md: each carries its own synthetic entry
# text, and the synthetic SHAs appear in none of them unless a test puts one
# there on purpose, so the SHA hatch can never rescue a fixture by accident.

# A commit whose subject shares nothing with the entry. The baseline offender:
# it is the shape the v3.24.0 omission had, and it is what every always-pass
# neutering below must silence.
_BARE: tuple[list[tuple[str, str]], str] = (
    [("a1b2c3d4e5f6a1b2", "feat(search): demote ranking on negative outcomes")],
    "## 9.9.9 - 2026-01-01\n\n- Unrelated prose about tombstone retention.\n",
)

# Every distinctive word of a short subject appears in the entry, but scattered
# — no shared adjacent pair. `_UNIGRAM_FLOOR` is the only thing that keeps this
# from counting as coverage, which is what makes it the floor's control.
_SCATTERED: tuple[list[tuple[str, str]], str] = (
    [("bbbb1111bbbb2222", "feat(ui): verdict parity")],
    "## 9.9.9 - 2026-01-01\n\n- Parity of every verdict is now shown.\n",
)

# The only two-word phrase shared with the entry is prose glue ("the store").
# `_distinctive` dropping stopwords before pairs are formed is what keeps that
# from reading as coverage.
_GLUE_BIGRAM: tuple[list[tuple[str, str]], str] = (
    [("cccc3333cccc4444", "feat(store): rework the store")],
    "## 9.9.9 - 2026-01-01\n\n- Touched the store lightly.\n",
)

# Each entry breaks one tier to its always-pass form and names the fixture that
# must stop being reported. `_stem` collapsing to a constant is how the bigram
# tier is neutered: every adjacent pair on both sides becomes the same tuple,
# so the tier matches unconditionally.
_NEUTERINGS: tuple[tuple[str, str, object, tuple[list[tuple[str, str]], str]], ...] = (
    ("trivial-commit exemption", "_classify_subject", lambda s: (True, ""), _BARE),
    ("bigram tier", "_stem", lambda w: "x", _BARE),
    ("unigram lookup", "_unigram_hit", lambda stem, stems: True, _BARE),
    ("unigram floor", "_UNIGRAM_FLOOR", 0, _SCATTERED),
    ("stopword filter", "_distinctive", lambda words: list(words), _GLUE_BIGRAM),
)


def test_a_commit_with_no_trace_in_the_entry_is_reported() -> None:
    commits, entry = _BARE
    assert len(_unrepresented(commits, entry)) == 1


def test_a_short_sha_citation_represents_a_paraphrased_commit() -> None:
    """Rank 1: the deterministic hatch for an entry that paraphrases.

    The added line shares no word with the commit subject, so the citation
    is the only thing carrying it — deleting the hatch fails this test.
    """
    commits, entry = _BARE
    cited = entry.rstrip() + "\n- Reworked the ordering wholesale (a1b2c3d).\n"
    assert _unrepresented(commits, cited) == []


def test_a_shared_two_word_phrase_represents_a_commit() -> None:
    """The bigram tier, on a subject too short for the unigram tier to save."""
    commits = [("dddd5555dddd6666", "feat(ui): verdict parity")]
    entry = "## 9.9.9 - 2026-01-01\n\n- The web UI gains verdict parity.\n"
    assert _unrepresented(commits, entry) == []


def test_the_bigram_tier_matches_across_an_inflection() -> None:
    """``_SUFFIXES`` is why "shared marker" finds "shared markers"."""
    commits = [("eeee7777eeee8888", "feat(hook): shared marker table")]
    entry = "## 9.9.9 - 2026-01-01\n\n- The shared markers table replaces it.\n"
    assert _unrepresented(commits, entry) == []


def test_nearly_every_distinctive_word_represents_a_paraphrased_commit() -> None:
    """The unigram tier: all but one word, and no shared adjacent pair."""
    commits = [
        (
            "ffff9999ffffaaaa",
            "feat(curation): corpus-level conflict arbitration across scopes",
        )
    ]
    entry = (
        "## 9.9.9 - 2026-01-01\n\n"
        "- Scopes now surface an arbitration verdict for each conflict; "
        "corpus totals stay level.\n"
    )
    assert _unrepresented(commits, entry) == []


def test_a_scattered_short_subject_is_below_the_unigram_floor() -> None:
    """The floor's whole job: two generic words in common is not coverage."""
    commits, entry = _SCATTERED
    assert len(_unrepresented(commits, entry)) == 1


def test_a_glue_only_shared_phrase_is_not_coverage() -> None:
    """ "the store" is a phrase two unrelated sentences share by accident."""
    commits, entry = _GLUE_BIGRAM
    assert len(_unrepresented(commits, entry)) == 1


def test_a_trivially_typed_commit_owes_the_entry_nothing() -> None:
    commits = [("1111aaaa1111bbbb", "docs(readme): lead the install with the plugin")]
    _, entry = _BARE
    assert _unrepresented(commits, entry) == []


def test_a_substantive_type_with_only_tooling_scopes_is_exempt() -> None:
    """``fix(test)`` is how this repo spells a test-suite repair."""
    _, entry = _BARE
    assert (
        _unrepresented([("2222cccc2222dddd", "fix(test): repair the lint")], entry)
        == []
    )
    # The same subject under a product scope is not exempt.
    assert (
        len(
            _unrepresented([("2222cccc2222dddd", "fix(store): repair the lint")], entry)
        )
        == 1
    )


def test_an_unparseable_subject_is_treated_as_substantive() -> None:
    """An unclassifiable change is the one that must not slip through."""
    _, entry = _BARE
    commits = [
        ("3333eeee3333ffff", "Rework the shard picker with no conventional prefix")
    ]
    assert len(_unrepresented(commits, entry)) == 1


def test_each_tier_has_a_fixture_that_notices_when_it_is_neutered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: break each tier, and a fixture above must fail.

    Without this the fixtures are only evidence that the judge agrees with
    itself today. Every tier here can be neutered to its always-pass form —
    exempt every commit, match every bigram, hit on every unigram, drop the
    floor to zero, keep the stopwords — and this is the test that proves at
    least one fixture distinguishes the working rule from the broken one.

    The SHA hatch is the one tier with no always-pass form to install (it is
    a substring test, not a helper), so it is controlled the other way round:
    ``test_a_short_sha_citation_represents_a_paraphrased_commit`` and
    ``test_a_commit_with_no_trace_in_the_entry_is_reported`` share a commit
    and differ only in whether the entry cites it, so deleting the hatch
    fails the first and widening it fails the second.
    """
    module = sys.modules[__name__]
    for label, attr, replacement, (commits, entry) in _NEUTERINGS:
        assert _unrepresented(commits, entry), (
            f"the {label} fixture must report an offender before the tier is "
            f"neutered, or the assertion below proves nothing"
        )
        with monkeypatch.context() as patched:
            patched.setattr(module, attr, replacement)
            assert not _unrepresented(commits, entry), (
                f"neutering the {label} to its always-pass form changed no "
                f"verdict — no fixture in this file can tell that tier "
                f"working from that tier gone"
            )
