"""The restore's closed limitations are gone from the docs, and the rc14
notes say what each fix closed and what is left (ENG-103), as do the
rc18 notes for the four residuals ENG-208 closed.

9.0.0rc12 recorded that a restore was guarded only against the daemon and
spawn locks, that its text output did not show the origin anchor, that a
hard link named through `BETTERMEMORY_DIR` was not checked, and that a
restore onto a host with no copy of the store warned of no rewound range.
rc14 closes each: its bullets say so, the docs that repeated the
limitations no longer do, the residuals are rc14 Known limitations, and
the rc12 section, history, is left as it was.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "changelog_section", REPO / "tools" / "changelog_section.py"
)
assert _SPEC is not None and _SPEC.loader is not None
changelog_section = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(changelog_section)


def _flat(text: str) -> str:
    """`text` with its line breaks and indents read as single spaces, so a
    phrase wrapped across lines matches."""
    return " ".join(text.split())


def _section(version: str) -> str:
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    return _flat(changelog_section.section(text, version))


def _doc(name: str) -> str:
    return _flat((REPO / "docs" / name).read_text(encoding="utf-8"))


def test_the_rc14_bullets_say_which_rc12_limitations_they_close() -> None:
    rc14 = _section("9.0.0rc14")
    for closed in (
        'closes the 9.0.0rc12 Known limitation "A restore is guarded only '
        'against the daemon and spawn locks"',
        'closes what was left of the 9.0.0rc12 Known limitation "Only some '
        "readers refuse a backup bundle's file\"",
        "closes the 9.0.0rc12 note that a restore onto a host that held no "
        "copy of the store knows no such range and warns of none",
        "closes the 9.0.0rc12 note that the text output does not show the "
        "origin anchor a restore replaces",
    ):
        assert closed in rc14, closed


def test_the_docs_no_longer_state_the_closed_limitations() -> None:
    assert "A restore is guarded against the daemon and the spawn lock only" not in (
        _doc("internals.md")
    )
    operations = _doc("operations.md")
    assert "open the store without those locks, so close them too" not in operations
    assert "The text output does not show the origin anchor" not in operations
    assert "the text output does not show them" not in _doc("api.md")
    rc14 = _section("9.0.0rc14")
    assert "The restore plan's read of the store id and of the log's last seq" not in (
        rc14
    )


def test_the_residual_left_is_an_rc14_known_limitation() -> None:
    """The export cursor's residual is ENG-127's, and stays listed."""
    known = _section("9.0.0rc14").split("#### Known limitations", 1)[1]
    assert (
        "After a restore onto a host with no copy of the store, `log export` "
        "warns every cursor past the restored head." in known
    )


#: The rc14 Known limitations ENG-208 closed, as rc14 worded them.
ENG208_CLOSED = (
    "A restore holds off only the writers that open through `store._connect`, "
    "and readers only as they start.",
    "On Windows a writing open holds the restore lock only for its check.",
    "A restore that fails between its record and its row leaves an entry with no row.",
    "A restored store waiting on a schema step gets no `store_restored` row.",
)


def test_the_limitations_eng208_closed_leave_the_rc14_section_and_the_docs() -> None:
    """ENG-208 closed four of the restore's rc14 residuals: the rc18 bullets
    say what each closed and the boundary left, rc14's Known limitations no
    longer list them, and internals.md and operations.md no longer state
    them as limitations."""
    known = _section("9.0.0rc14").split("#### Known limitations", 1)[1]
    for closed in ENG208_CLOSED:
        assert closed not in known, closed
    fixed = _section("9.0.0rc18").split("#### Fixed", 1)[1]
    for said in (
        "A restore's `store_restored` row that the restore could not write is "
        "appended by the next owner open (ENG-208).",
        "A read holds a restore off until it has read (ENG-208).",
        "On Windows a writing open that races a restore is refused or stops it "
        "(ENG-208).",
        "A process that writes the store file through a SQLite connection of its "
        "own, not bettermemory's code, takes no lock, and no lock can hold it off",
    ):
        assert said in fixed, said
    assert fixed.count("9.0.0rc14 listed") >= 3
    internals, operations = _doc("internals.md"), _doc("operations.md")
    for stale in (
        "an opener holds the lock only for its check",
        "one that started before it can read the store between the restore's steps",
        "a process writing the file through a connection of its own is not held off",
    ):
        assert stale not in internals, stale
        assert stale not in operations, stale
    assert "takes no part in it, and nothing holds it off" in internals
    assert "One that started before the restore holds it off" in operations


def test_the_eng208_docs_say_what_the_review_found() -> None:
    """The ENG-208 review's corrections stand in the docs: the late row is
    the owner open's and is withheld once a record came first; not every
    read-only command exits 75; the restore's own `log verify` catches a
    foreign writer's rows only in the restored file, before it ran. A
    withhold the time decided names what else the file may be, under the
    code's own test, not a missing or lagging checkpoint; a Windows swap
    waits out a racing check's handle, and only a handle; and no text
    says a Windows restore that an admitted opener races fails at its
    swap, or that the handle kept every restore off."""
    fixed = _section("9.0.0rc18").split("#### Fixed", 1)[1]
    assert "the read-only commands still exit 75" not in fixed
    assert "`key show` exits 1 and `security scan` 2" in fixed
    assert "Every entry the open keeps off its log is withheld" in fixed
    assert "exits 1 saying a command is reading or opening the store" in fixed
    assert "(`head_before` null, or not past `head_after`)" in fixed
    assert "another copy of the store where it read no log here" in fixed
    assert "missing or behind the log does the time" not in fixed
    assert "tries its first rename again for 2 s and a second more" in fixed
    assert "a restore that takes the lock after that fails at its swap" not in fixed
    assert "No opener writes after the probe of a restore that goes on" in fixed
    assert "The swap waits only for a file held open" in fixed
    operations = _doc("operations.md")
    assert "from the first writing open after the unlock" not in operations
    assert "row from the first owner open after the unlock" in operations
    assert "the row is withheld: on the log it would stand" in operations
    assert "(`head_before` null, or not past `head_after`)" in operations
    assert "(`log_end_before` null), another copy of the store" in operations
    api = _doc("api.md")
    assert "so a restore that takes the lock later fails at its swap" not in api
    assert "checks again behind the handle and once more when its connection" in api
    from bettermemory import restore_lock

    lock_doc = " ".join((restore_lock.__doc__ or "").split())
    assert "is refused or stops the restore at its swap" not in lock_doc
    assert "kept every restore off since then, and there is nothing" not in " ".join(
        (restore_lock._RecordCheck.confirm.__doc__ or "").split()
    )
    security = _flat((REPO / "SECURITY.md").read_text(encoding="utf-8"))
    assert "what it writes is caught as any write without the key is: its rows" not in (
        security
    )
    assert "Rows it writes into the restored file before the restore's own" in (
        security
    )


def test_the_limitations_the_fold_closed_are_gone() -> None:
    """The cold review's fold closed these rc14 Known limitations; the
    section says so in its bullets, not as limitations."""
    known = _section("9.0.0rc14").split("#### Known limitations", 1)[1]
    for closed in (
        "A `backup create` with an unreadable key still writes a bundle.",
        "The probe does not see an idle connection to a rollback-journal store.",
        "`backup create` still reads a store through a connection of its own.",
        "`backup create --out` checks the named directory and its parent only.",
        "A restored store this build reads only gets no `store_restored` row.",
    ):
        assert closed not in known, closed


def test_the_rc12_section_keeps_its_own_words() -> None:
    """Older entries are history and are not edited."""
    rc12 = _section("9.0.0rc12")
    assert "A restore is guarded only against the daemon and spawn locks." in rc12
    assert "nor does anything check a hard link named through `BETTERMEMORY_DIR`" in (
        rc12
    )
