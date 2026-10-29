"""Every membership citation names a part of `docs/membership-design.md`,
and the document stays redacted (ENG-98).

The membership family cites its design by number: `design N.N` for a
section, `decision Dn` for a ruling of round 2 and `decision Bn` for a
build decision of RS-F6, beside witness codes, case rows (I34, T6, X2)
and experiments (E10). Until 9.0.0 the design lived outside the
repository, so a citation named nothing a reader could open. These tests
read every citation in src, tests and docs and in the root CHANGELOG.md,
CONTRIBUTING.md and README.md: the whole list after the word, not its
first number only (`design 3.5, 8.2`, `design 2.1, decision D13`,
`design 3.5 and D13`, `decisions D12, D13 and D17`), and across a line
break where a comment wraps. Each item must name a section of the
document that holds text or an entry of its appendices. The document's
own `section N.N` references are held to its sections the same way, and
an appendix entry counts only as one: a `**Dn. title**` line, or a `Bn`
that opens a clause of the build paragraph with words, each once and in
order.

The document was assembled from the design's working copy with the
owner's store taken out: none of the owner's project names, no memory
id, no `[[...]]` link, no path under the owner's home or cache, no
sha256 value. The project names are not in the repository in any form:
the check for them reads a list, one name per line, from the file the
environment variable `BETTERMEMORY_REDACTION_LIST` names, and is
skipped where that file is not there.
"""

from __future__ import annotations

import bisect
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESIGN = REPO / "docs" / "membership-design.md"
#: The variable naming the redaction list, which lives outside the repository.
REDACTION_LIST = "BETTERMEMORY_REDACTION_LIST"

_NUMBER = r"\d+(?:\.\d+)?"
_DECISION = r"[DB]\d+"
#: What joins two items of a list: `, `, ` and `, `, and `, ` or `.
_JOIN = r"(?:,? and |,? or |, )"
#: What may follow a list's later item. A number that runs on into a
#: word (`design 8.9, at 50 projects`) is not an item, and the list ends
#: before it.
_ENDS = r"(?=[,;:)\]`'\"]|\.(?:\s|$)|\s+(?:and|or)\b|\s*$)"
#: `design 3.5`, `Design 8.12`, `design section 7`, and the list after
#: it: `design 3.5, 8.2`, `design 2.1 and 8.7, decision D16`,
#: `design 3.5, D13`, `design 1.1, decisions D10 and D11`.
_DESIGN_CITE = re.compile(
    rf"\b[Dd]esign (?:section )?({_NUMBER}\b"
    rf"(?:{_JOIN}(?:(?:[Dd]ecisions? )?{_DECISION}|{_NUMBER}){_ENDS})*)"
)
#: `decision B6`, `decisions D12 and D13`, `decisions D13, D17, B6, B9`.
_DECISION_CITE = re.compile(
    rf"\b[Dd]ecisions? ({_DECISION}\b"
    rf"(?:{_JOIN}(?:[Dd]ecisions? )?{_DECISION}{_ENDS})*)"
)
#: The document's own references: `section 3.6`, `sections 3.4 and 3.5`.
_SECTION_REF = re.compile(rf"\b[Ss]ections? ({_NUMBER}\b(?:{_JOIN}{_NUMBER}{_ENDS})*)")
_ITEM = re.compile(rf"{_DECISION}|{_NUMBER}")
#: What a wrapped comment line opens with: indentation and a `#` or `#:`.
_LEADER = re.compile(r"^\s*(?:#:?\s?)?")


def _design() -> str:
    return DESIGN.read_text(encoding="utf-8")


def _citing_files() -> list[Path]:
    return [
        *sorted((REPO / "src").rglob("*.py")),
        *sorted((REPO / "tests").rglob("*.py")),
        *sorted((REPO / "docs").rglob("*.md")),
        REPO / "CHANGELOG.md",
        REPO / "CONTRIBUTING.md",
        REPO / "README.md",
    ]


def _flat(path: Path) -> tuple[str, list[int]]:
    """The file as one line, each line's indentation and comment marker
    dropped so a citation wrapped over two comment lines reads as one,
    and the offset in it at which each line starts."""
    starts: list[int] = []
    parts: list[str] = []
    offset = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        part = _LEADER.sub("", line).rstrip()
        starts.append(offset)
        parts.append(part)
        offset += len(part) + 1
    return " ".join(parts), starts


def _items(
    pattern: re.Pattern[str], path: Path, name: str
) -> Iterator[tuple[str, str]]:
    """(``name:line``, item) for every item of every list the pattern
    matches in the file, where the line is the one the citation opens on."""
    text, starts = _flat(path)
    for match in pattern.finditer(text):
        line = bisect.bisect_right(starts, match.start())
        for item in _ITEM.findall(match.group(1)):
            yield f"{name}:{line}", item


def _cited(
    patterns: Iterable[re.Pattern[str]], paths: Iterable[Path]
) -> list[tuple[str, str]]:
    return [
        found
        for path in paths
        for pattern in patterns
        for found in _items(pattern, path, path.relative_to(REPO).as_posix())
    ]


def _headings(text: str) -> set[str]:
    """The numbers of the document's sections that hold something: a
    ``## 3. title`` or ``### 3.5 title`` heading with words below it
    before the next heading, or a ``## 3. title`` one of whose
    subsections has them."""
    marks = list(re.finditer(r"^#{1,6} .*$", text, re.M))
    titled = set()
    held = set()
    for here, after in zip(marks, [*marks[1:], None], strict=True):
        match = re.match(rf"#{{2,3}} ({_NUMBER})\.? \S", here.group(0))
        if match is None:
            continue
        titled.add(match.group(1))
        body = text[here.end() : after.start() if after else len(text)]
        if re.search(r"[A-Za-z]{2}", body):
            held.add(match.group(1))
    return held | {n for n in titled if any(h.startswith(f"{n}.") for h in held)}


def _appendix(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _entries(text: str) -> list[str]:
    """The decisions the appendices hold, in their order: a
    ``**Dn. title**`` line with a title, and each clause of the build
    paragraph (``B1 text; B2 text; ...``), every one of which must open
    with its ``Bn`` and go on with words."""
    ruled = _appendix(text, "Decisions D1 to D19")
    built = _appendix(text, "Build decisions B1 to B24")
    found = [
        f"D{n}"
        for n in re.findall(r"^\*\*D(\d+)\. [^*\n]*[A-Za-z][^*\n]*\*\*", ruled, re.M)
    ]
    for paragraph in built.split("\n\n"):
        if not re.match(r"B\d+ ", paragraph):
            continue
        for clause in paragraph.split("; "):
            match = re.match(r"B(\d+) (?=.*[A-Za-z]{2})", clause, re.S)
            found.append(f"B{match.group(1)}" if match else f"no entry: {clause[:20]}")
    return found


def test_the_appendices_hold_d1_to_d19_and_b1_to_b24() -> None:
    expected = [f"D{n}" for n in range(1, 20)] + [f"B{n}" for n in range(1, 25)]
    assert _entries(_design()) == expected


def test_a_build_decision_without_words_is_not_an_entry() -> None:
    """A `Bn` left with no text, or another's id where it stood, does not
    stand in for the entry."""
    text = _design()
    emptied = re.sub(r"; B12 [^;]*;", "; B12 ;", text, count=1)
    assert emptied != text
    assert "B12" not in _entries(emptied)
    renamed = re.sub(r"; B12 [^;]*;", "; B11 again;", text, count=1)
    assert "B12" not in _entries(renamed)
    assert _entries(renamed).count("B11") == 2


def test_a_section_is_a_heading_with_words_below_it() -> None:
    text = "## 1. A\n\n### 1.1 B\n\nSome text.\n\n### 1.2 C\n\n## 2. D\n\n### 2.1\n"
    assert _headings(text) == {"1", "1.1"}


def test_every_cited_design_section_is_a_section_of_the_document() -> None:
    sections = _headings(_design())
    cited = [
        (where, item)
        for where, item in _cited([_DESIGN_CITE], _citing_files())
        if not re.fullmatch(_DECISION, item)
    ]
    assert len(cited) > 100, "the citation pattern no longer matches the code's"
    assert [(where, n) for where, n in cited if n not in sections] == []


def test_every_cited_decision_is_an_entry_of_the_appendices() -> None:
    entries = set(_entries(_design()))
    cited = [
        (where, item)
        for where, item in _cited([_DESIGN_CITE, _DECISION_CITE], _citing_files())
        if re.fullmatch(_DECISION, item)
    ]
    assert len(cited) > 100, "the citation pattern no longer matches the code's"
    assert [(where, i) for where, i in cited if i not in entries] == []


def test_every_section_the_document_names_is_one_of_its_sections() -> None:
    sections = _headings(_design())
    named = _cited([_SECTION_REF], [DESIGN])
    assert len(named) > 30, "the reference pattern no longer matches the document's"
    assert [(where, n) for where, n in named if n not in sections] == []


@pytest.mark.parametrize(
    ("line", "items"),
    [
        ("(design 3.5, 8.2): the surface", ["3.5", "8.2"]),
        ("(design 3.5, D13).", ["3.5", "D13"]),
        ("(design 2.1 and 8.7, decision D16).", ["2.1", "8.7", "D16"]),
        ("Design 1.1, decisions D10 and D11. The token", ["1.1", "D10", "D11"]),
        ("design 2.1, 3.4, 8.8 and 8.10; decisions", ["2.1", "3.4", "8.8", "8.10"]),
        ("(design 8.9, at 50 projects", ["8.9"]),
        ("(design 8.7, 3 witnesses)", ["8.7"]),
        ("(design 4, failure 6)", ["4"]),
        ("(design 8.7, E27, T16), the", ["8.7"]),
        ("decisions D13, D17, B6, B9): what", ["D13", "D17", "B6", "B9"]),
        ("decisions D12, D13 and D17. The file", ["D12", "D13", "D17"]),
        ("decision B16, design 3.5), the", ["B16", "3.5"]),
    ],
)
def test_a_citation_list_is_read_to_its_end(
    line: str, items: list[str], tmp_path: Path
) -> None:
    path = tmp_path / "cite.py"
    path.write_text(f"# {line}\n", encoding="utf-8")
    found = {
        item
        for pattern in (_DESIGN_CITE, _DECISION_CITE)
        for _, item in _items(pattern, path, "cite.py")
    }
    assert sorted(found) == sorted(items)


def test_a_citation_wrapped_over_two_comment_lines_is_read_whole(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cite.py"
    path.write_text("    # the brief (design 8.1,\n    # 8.7) and\n", encoding="utf-8")
    assert list(_items(_DESIGN_CITE, path, "cite.py")) == [
        ("cite.py:1", "8.1"),
        ("cite.py:1", "8.7"),
    ]


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
