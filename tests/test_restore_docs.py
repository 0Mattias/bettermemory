"""The restore's closed limitations are gone from the docs, and the rc14
notes say what each fix closed and what is left (ENG-103).

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


def test_the_residuals_are_rc14_known_limitations() -> None:
    known = _section("9.0.0rc14").split("#### Known limitations", 1)[1]
    for residual in (
        "A restore holds off only the writers that open through `store._connect`, "
        "and readers only as they start.",
        "On Windows a writing open holds the restore lock only for its check.",
        "After a restore onto a host with no copy of the store, `log export` "
        "warns every cursor past the restored head.",
        "A restore that fails between its record and its row leaves an entry "
        "with no row.",
        "A restored store this build reads only gets no `store_restored` row.",
    ):
        assert residual in known, residual


def test_the_limitations_the_fold_closed_are_gone() -> None:
    """The cold review's fold closed these rc14 Known limitations; the
    section says so in its bullets, not as limitations."""
    known = _section("9.0.0rc14").split("#### Known limitations", 1)[1]
    for closed in (
        "A `backup create` with an unreadable key still writes a bundle.",
        "The probe does not see an idle connection to a rollback-journal store.",
        "`backup create` still reads a store through a connection of its own.",
        "`backup create --out` checks the named directory and its parent only.",
    ):
        assert closed not in known, closed


def test_the_rc12_section_keeps_its_own_words() -> None:
    """Older entries are history and are not edited."""
    rc12 = _section("9.0.0rc12")
    assert "A restore is guarded only against the daemon and spawn locks." in rc12
    assert "nor does anything check a hard link named through `BETTERMEMORY_DIR`" in (
        rc12
    )
