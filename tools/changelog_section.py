#!/usr/bin/env python3
"""Print the CHANGELOG section for one version: the GitHub release's notes.

The ``github-release`` job of ``.github/workflows/release.yml`` runs it for
the tag's version and hands the output to the release as its body, ahead
of GitHub's generated compare link; ``tests/test_changelog_section.py``
holds it to the file's shape.

    python3 tools/changelog_section.py CHANGELOG.md 9.0.0rc9

A candidate's section is the text under its ``### <version>`` heading up
to the next heading of its level or higher. A stable release's is the text
under its ``## <version> - <date>`` heading up to the next heading of any
level: the summary above the first candidate's subsection. A ``#`` line
inside a fenced code block is text, not a heading. Exit status 1 when the
version has no section or an empty one, so a release run fails before it
publishes an empty body; 2 on a usage error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6}) (\S+)")
_FENCES = ("```", "~~~")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Every heading outside a fenced code block: its line index, level and
    first word (a version for a release heading)."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(_FENCES):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def section(text: str, version: str) -> str | None:
    """The section's text for `version`, without its heading, ending in one
    newline; None when the file has no heading for it or the section is
    empty."""
    lines = text.splitlines()
    headings = _headings(lines)
    start = level = None
    for index, heading_level, name in headings:
        if name == version and heading_level in (2, 3):
            start, level = index, heading_level
            break
    if start is None or level is None:
        return None
    end = len(lines)
    for index, heading_level, _name in headings:
        if index > start and (level == 2 or heading_level <= level):
            end = index
            break
    body = "\n".join(lines[start + 1 : end]).strip("\n")
    return body + "\n" if body else None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    text = Path(argv[1]).read_text(encoding="utf-8")
    body = section(text, argv[2])
    if body is None:
        print(f"no CHANGELOG section for {argv[2]}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
