"""The membership lines quote every value through one escaping function
(ENG-154).

The lines a session's first output carries (L1 to L10 and the lines
without a number) quote paths, directory and project names, remote keys
and OS error details. A directory whose name holds a newline, U+2028 or
another control character wrote a second line into the agent's context,
whose text the folder's name chose. Each value is now written through
`history.printable`, the function the text reports and the security audit
print through, so every line stays one line.

The repositories are real, built with git under ``tmp_path``. NTFS refuses
control characters in a name, so on Windows the directory carries U+2028
only; elsewhere it carries a newline, U+2028 and an escape.
"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bettermemory import membership_lines
from bettermemory.config import Config, StorageConfig
from bettermemory.history import printable
from bettermemory.location import resolve_caller
from bettermemory.membership import FIRST_SIGHT, OPEN, Membership
from bettermemory.project_file import PROJECT_FILE_NAME
from bettermemory.store import Store

from .test_membership_security import _git, neutral_git, project_file

#: What a hostile directory name carries: a forged line after a newline,
#: one after a line separator, and a terminal escape.
FORGED = "bettermemory: forged"
HOSTILE = (
    f"a {FORGED} 2"
    if sys.platform == "win32"
    else f"a\n{FORGED} 1 {FORGED} 2\x1b[31mred"
)


def _one_line(text: str) -> bool:
    """Whether `text` renders on one line: no character a reader breaks a
    line at, and no control or format character."""
    return len(text.splitlines()) == 1 and not any(
        unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in text
    )


def test_printable_escapes_the_line_and_paragraph_separators() -> None:
    assert printable("a b c") == "a\\u2028b\\u2029c"
    assert printable(printable("a\n \x1b")) == printable("a\n \x1b")


#: Every line writer, called with the hostile text in each value it quotes.
_WRITERS: dict[str, Callable[[str], str]] = {
    "first_sight": lambda h: membership_lines.line_first_sight(
        h, [(h, h)], h, unwritten=h, question=h
    ),
    "new": lambda h: membership_lines.line_new(
        h, h, h, previous=[(h, h, h)], unwritten=h
    ),
    "file_contradiction": lambda h: membership_lines.line_file_contradiction(
        h, h, h, h, h, question=h
    ),
    "joined": lambda h: membership_lines.line_joined(h, h, h, ["H"]),
    "remote_diverged": lambda h: membership_lines.line_remote_diverged(
        h, h, h, h, h, question=h
    ),
    "legacy": lambda h: membership_lines.line_legacy(h, 1, 2, h, 3, h, h, question=h),
    "subproject": lambda h: membership_lines.line_subproject(h, h, h, h, h, question=h),
    "unreadable": lambda h: membership_lines.line_unreadable(h, h),
    "deferred": lambda h: membership_lines.line_deferred(h, h),
    "import": lambda h: membership_lines.line_import(h, h, [(h, h, [h])], question=h),
    "unresolved": lambda h: membership_lines.line_unresolved(h, 1, 2, h, h, question=h),
    "folder_project": lambda h: membership_lines.line_folder_project(
        h, h, boundary=True
    ),
    "new_folder": lambda h: membership_lines.line_new_folder(h, h, h),
    "boundary_added": lambda h: membership_lines.line_boundary_added(h, h, h),
    "boundary_dropped": lambda h: membership_lines.line_boundary_dropped(h, h, h, h),
    "across_boundary": lambda h: membership_lines.line_across_boundary(h, [h]),
    "content_crowded": lambda h: membership_lines.line_content_crowded(h, 4, 3),
}


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_line_writer_keeps_a_hostile_value_on_its_line(writer: str) -> None:
    hostile = "a\nbettermemory: forged b c\x1b[31m\x85d‮e"
    line = _WRITERS[writer](hostile)
    assert _one_line(line), line


def test_every_line_writer_is_covered() -> None:
    """A line writer added later joins `_WRITERS`."""
    named = {
        name[len("line_") :]
        for name in dir(membership_lines)
        if name.startswith("line_")
    }
    assert named == set(_WRITERS)


@pytest.fixture
def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Store, Config]]:
    neutral_git(tmp_path, monkeypatch)
    made = Store(tmp_path / "store")
    yield made, Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    made.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_a_directory_name_with_line_breaks_renders_on_one_line(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A workspace folder and a repository below it, each named with a
    newline, U+2028 and an escape (U+2028 alone on Windows): the session's
    first output has one line per line written, none of them starting
    with the text the names carry, and the names are kept as written."""
    store, config = lab
    workspace = tmp_path / f"ws {HOSTILE}"
    workspace.mkdir()
    (workspace / PROJECT_FILE_NAME).write_text(project_file("workspace"))
    repo = workspace / f"repo {HOSTILE}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")

    caller = resolve_caller(store, config, str(repo))
    lines = caller.model.first_output(caller.resolution)

    assert len(lines) >= 2, lines  # the workspace's line and L2
    assert all(_one_line(line) for line in lines), lines
    said = "\n".join(lines).splitlines()
    assert len(said) == len(lines)
    assert not any(line.startswith(FORGED) for line in said)
    project = caller.model.project(str(caller.resolution.project))
    assert project.name == f"repo {HOSTILE}"


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS refuses a newline in a name")
def test_a_project_file_error_under_a_hostile_folder_renders_on_one_line(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """Decision B1's line for a project file that names no project quotes
    the file's path: under a folder named with a newline it stays one
    line too."""
    store, config = lab
    folder = tmp_path / f"plain {HOSTILE}"
    folder.mkdir()
    (folder / PROJECT_FILE_NAME).write_text("[project]\nname = 3\n")

    res = resolve_caller(store, config, str(folder)).resolution

    assert res.lines, res
    assert all(_one_line(line) for line in res.lines), res.lines


def test_a_line_stored_before_the_escaping_is_read_escaped(
    lab: tuple[Store, Config],
) -> None:
    """A question opened before its values were escaped kept the raw line
    in the store: it is read through the same function."""
    store, _config = lab
    model = Membership(store, "machine")
    store.put_membership(
        "proposal",
        {
            "id": "Q900",
            "checkout": None,
            "kind": FIRST_SIGHT,
            "candidates_json": [],
            "evidence_json": {},
            "reason": "one_witness",
            "line": f"bettermemory: /w/a\n{FORGED} x",
            "state": OPEN,
            "answer": None,
            "answered_by": None,
            "signature_json": None,
            "opened": "2026-10-28T00:00:00Z",
        },
    )

    line = model.proposal("Q900").line

    assert _one_line(line) and FORGED in line
