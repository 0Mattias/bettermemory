#!/usr/bin/env python3
"""Print the CHANGELOG section for one version: the GitHub release's notes.

The ``github-release`` job of ``.github/workflows/release.yml`` runs it for
the tag's version and hands the output to the release as its body, ahead
of GitHub's generated compare link; ``tests/test_changelog_section.py``
holds it to the file's shape.

    python3 tools/changelog_section.py CHANGELOG.md 9.0.0rc9
    python3 tools/changelog_section.py --max-chars 120000 CHANGELOG.md 9.0.0rc9

A candidate's section is the text under its ``### <version>`` heading up
to the next heading of its level or higher. A stable release's is the text
under its ``## <version> - <date>`` heading up to the next heading of any
level: the summary above the first candidate's subsection. A ``#`` line
inside a fenced code block is text, not a heading. Exit status 1 when the
version has no section or an empty one, so a release run fails before it
publishes an empty body; 2 on a usage error.

GitHub refuses a release body over 125,000 characters, and the
github-release job runs after the PyPI publish, so a section longer than
`RELEASE_BODY_CHARS` (``--max-chars`` overrides it) is cut at a paragraph
boundary and ends in a note saying so and where the whole section is.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6}) (\S+)")
_FENCES = ("```", "~~~")
#: The most characters the notes may hold: GitHub refuses a release body
#: over 125,000, and the compare link its generated notes add follows them.
RELEASE_BODY_CHARS = 120_000
#: The least ``--max-chars`` accepts: room for the note and a paragraph.
MIN_MAX_CHARS = 1_000
#: How far back from the room's end a paragraph or bullet boundary may be
#: before the cut falls back to a line boundary.
_NEAR = 2_000
_CUT_NOTE = (
    "\n\n_The notes stop here: this section is {total:,} characters, more than "
    "a GitHub release body holds. The whole section is in CHANGELOG.md at "
    "this tag._\n"
)


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


def cut(body: str, max_chars: int) -> str:
    """`body` when it holds at most `max_chars` characters; otherwise its
    longest prefix ending at a paragraph or bullet boundary (a line boundary
    when neither is within `_NEAR` characters of the room, as in a long list
    of one-line bullets) followed by `_CUT_NOTE`, together at most
    `max_chars`. ValueError when `max_chars` is under `MIN_MAX_CHARS`."""
    if max_chars < MIN_MAX_CHARS:
        raise ValueError(f"max_chars {max_chars} is under {MIN_MAX_CHARS}")
    if len(body) <= max_chars:
        return body
    note = _CUT_NOTE.format(total=len(body))
    head = body[: max_chars - len(note)]
    boundary = max(head.rfind("\n\n"), head.rfind("\n- "))
    if boundary < len(head) - _NEAR:
        boundary = head.rfind("\n")
    if boundary <= 0:
        boundary = len(head)
    return head[:boundary].rstrip("\n") + note


def main(argv: list[str]) -> int:
    args = argv[1:]
    max_chars = RELEASE_BODY_CHARS
    if len(args) == 4 and args[0] == "--max-chars":
        try:
            max_chars = int(args[1])
        except ValueError:
            max_chars = 0
        args = args[2:]
    if len(args) != 2 or max_chars < MIN_MAX_CHARS:
        print(__doc__, file=sys.stderr)
        return 2
    text = Path(args[0]).read_text(encoding="utf-8")
    body = section(text, args[1])
    if body is None:
        print(f"no CHANGELOG section for {args[1]}", file=sys.stderr)
        return 1
    sys.stdout.write(cut(body, max_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
