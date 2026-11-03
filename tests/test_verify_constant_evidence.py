"""Each tuned verify constant says what evidence chose it, or that none did.

ENG-171 H3, the verify half. `_MAX_PATH_LENGTH` gave a reason (cap the
stat() work) and `BODY_PARSE_ENTRIES` said what the memo does, and neither
said why its value. Each now carries an `Evidence:` paragraph in the
comment above it; this keeps the paragraph there, and keeps true the path
cap's statement that a candidate past it is dropped, never flagged missing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bettermemory import verify_candidates
from bettermemory.verify import commit_drift_anchor_paths, detect_path_drift

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "bettermemory"
    / "verify_candidates.py"
)


def _comment_above(name: str) -> str:
    """The run of `#` lines directly above the module-level assignment."""
    source = _MODULE.read_text(encoding="utf-8")
    lines = source.splitlines()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            above: list[str] = []
            i = node.lineno - 2
            while i >= 0 and lines[i].lstrip().startswith("#"):
                above.append(lines[i].lstrip("# ").rstrip())
                i -= 1
            return " ".join(reversed(above))
    raise AssertionError(f"{name} is not assigned at module level")


@pytest.mark.parametrize("name", ["_MAX_PATH_LENGTH", "BODY_PARSE_ENTRIES"])
def test_the_constant_states_its_evidence(name: str) -> None:
    """The comment above the constant carries an Evidence paragraph."""
    assert "Evidence:" in _comment_above(name)


def test_the_memo_evidence_prices_both_memos() -> None:
    """The cap bounds each memo on its own, so its evidence sizes both."""
    evidence = _comment_above("BODY_PARSE_ENTRIES").partition("Evidence:")[2]
    assert "_CANDIDATES" in evidence
    assert "_ANCHORS" in evidence


def _path_of(length: int) -> str:
    """An absolute path spelled in exactly `length` characters."""
    path = ("/srv" + "/segment" * length)[:length]
    return path[:-1] + "s" if path.endswith("/") else path


@pytest.mark.parametrize("spelling", ["`{}`", "{}"], ids=["backtick", "bare"])
def test_a_path_past_the_cap_is_dropped_never_flagged(spelling: str) -> None:
    """A path at the cap is a candidate; one a character past it is no claim
    on either leg: not extracted, not checked, not missing, not an anchor."""
    cap = verify_candidates._MAX_PATH_LENGTH
    at, past = _path_of(cap), _path_of(cap + 1)
    kept = verify_candidates._extract_candidates(f"Kept at {spelling.format(at)} here.")
    assert [path for path, _, _ in kept] == [at]

    body = f"Kept at {spelling.format(past)} here."
    assert verify_candidates._extract_candidates(body) == []
    report = detect_path_drift(body)
    assert not report.checked and not report.missing
    assert commit_drift_anchor_paths(body) == ()
