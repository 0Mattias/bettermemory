"""Every membership citation names a part of `docs/membership-design.md`,
and the document stays redacted (ENG-98).

The membership family cites its design by number: `design N.N` for a
section, `decision Dn` for a ruling of round 2 and `decision Bn` for a
build decision of RS-F6, beside witness codes, case rows (I34, T6, X2)
and experiments (E10). Until 9.0.0 the design lived outside the
repository, so a citation named nothing a reader could open. These tests
read every `design N.N` and every `decision Dn` or `Bn` in src, tests
and docs, and require the section heading or the appendix entry each
one names.

The document was assembled from the design's working copy with the
owner's store taken out: none of the owner's project names, no memory
id, no `[[...]]` link, no path under the owner's home or cache, no
sha256 value. The project names are not in the repository in any form:
the check for them reads a list, one name per line, from the file the
environment variable `BETTERMEMORY_REDACTION_LIST` names, and is
skipped where that file is not there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESIGN = REPO / "docs" / "membership-design.md"
#: The variable naming the redaction list, which lives outside the repository.
REDACTION_LIST = "BETTERMEMORY_REDACTION_LIST"

#: `design 3.5`, `Design 8.12`, `design section 7`.
_SECTION_CITE = re.compile(r"\b[Dd]esign (?:section )?(\d+(?:\.\d+)?)\b")
#: `decision B6`, `decisions D12 and D13`, `Decision D7`.
_DECISION_CITE = re.compile(
    r"\b[Dd]ecisions? ([DB]\d+(?:(?:,? and |, | or )[DB]\d+)*)\b"
)
_DECISION_ID = re.compile(r"[DB]\d+")


def _design() -> str:
    return DESIGN.read_text(encoding="utf-8")


def _citing_lines() -> Iterator[tuple[str, str]]:
    """(``path:line``, text) for every line of src, tests and docs."""
    files = [
        *sorted((REPO / "src").rglob("*.py")),
        *sorted((REPO / "tests").rglob("*.py")),
        *sorted((REPO / "docs").rglob("*.md")),
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            yield f"{path.relative_to(REPO)}:{number}", line


def _headings(text: str) -> set[str]:
    """The numbers of the document's sections: ``## 3. ...`` and
    ``### 3.5 ...``."""
    found = set()
    for line in text.splitlines():
        match = re.match(r"#{2,3} (\d+(?:\.\d+)?)\.? ", line)
        if match:
            found.add(match.group(1))
    return found


def _appendix(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _entries(text: str) -> set[str]:
    """The decisions the appendices hold: ``**D1. ...`` and ``B1 ...;
    B2 ...``."""
    ruled = _appendix(text, "Decisions D1 to D19")
    built = _appendix(text, "Build decisions B1 to B24")
    return {f"D{n}" for n in re.findall(r"^\*\*D(\d+)\. ", ruled, re.M)} | {
        f"B{n}" for n in re.findall(r"(?:^|; )B(\d+) ", built, re.M)
    }


def test_the_appendices_hold_d1_to_d19_and_b1_to_b24() -> None:
    expected = {f"D{n}" for n in range(1, 20)} | {f"B{n}" for n in range(1, 25)}
    assert _entries(_design()) == expected


def test_every_cited_design_section_is_a_heading_of_the_document() -> None:
    headings = _headings(_design())
    cited = [
        (where, number)
        for where, line in _citing_lines()
        for number in _SECTION_CITE.findall(line)
    ]
    assert len(cited) > 100, "the citation pattern no longer matches the code's"
    assert [(where, n) for where, n in cited if n not in headings] == []


def test_every_cited_decision_is_an_entry_of_the_appendices() -> None:
    entries = _entries(_design())
    cited = [
        (where, ident)
        for where, line in _citing_lines()
        for group in _DECISION_CITE.findall(line)
        for ident in _DECISION_ID.findall(group)
    ]
    assert len(cited) > 100, "the citation pattern no longer matches the code's"
    assert [(where, i) for where, i in cited if i not in entries] == []


def test_the_document_names_none_of_the_listed_names() -> None:
    """Each name in the redaction list, compared in lowercase. The count
    is all a failure prints, so the output does not spell a name."""
    listed = os.environ.get(REDACTION_LIST, "")
    if not listed or not Path(listed).is_file():
        pytest.skip(
            f"{REDACTION_LIST} names no file; the list lives outside the repository"
        )
    names = [
        line.strip().lower()
        for line in Path(listed).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    text = _design().lower()
    found = sum(1 for name in names if name in text)
    assert found == 0, f"{found} name(s) from {REDACTION_LIST} in the document"


def test_the_document_holds_no_memory_id_link_home_path_or_digest() -> None:
    text = _design()
    for needle in ("01M", "[[", "/Users/", "~/.cache", "REDACT"):
        assert needle not in text, needle
    assert re.search(r"\b[0-9a-fA-F]{64}\b", text) is None
