"""CHANGELOG hygiene lint.

The 2.6.2 and 2.6.3 audit passes both found `## <version> -` headings
that had silently disappeared from CHANGELOG.md (1.3.0, 1.2.1, 2.6.0).
The narrative bodies were intact but renderers walking the heading
hierarchy stitched the prose of one release into the next, making
release notes useless for the missing entries.

This test pins two invariants:

1. The version in ``pyproject.toml`` has a matching ``## <version> -``
   heading in ``CHANGELOG.md``. If you bump pyproject without writing
   the entry, the suite fails before the release tag goes out.

2. Every ``## <version> -`` heading parses as a valid semver
   ``X.Y.Z - YYYY-MM-DD`` line. A malformed heading (missing date,
   trailing junk, wrong dash) trips the lint instead of silently
   landing in the rendered output.

We deliberately *don't* try to assert monotonicity or gap-free patch
series — release branches diverge and rebase, and a "1.5.0 then
2.0.0 then 1.5.1" sequence (where 1.5.1 is a backport) is legal even
if rare. The two invariants above cover the actual class of bug the
audit cycle keeps surfacing.
"""

from __future__ import annotations

import re
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
        (_REPO_ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    marketplace = json.loads(
        (_REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
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
