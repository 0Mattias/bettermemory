"""Each tuned health constant says what evidence chose it, or that none did.

ENG-171 H3. `_ENDORSEMENT_GRACE_DAYS`, `_CROSS_REPO_MAX_ROOTS` and
`_RECOMMENDATION_SIZE_FLOOR` carried reasons but no evidence, and the
grace's reason cited a lag measured in turns and a 30-minute token TTL
for a value in days. Each now carries an `Evidence:` paragraph in the
comment above it; this keeps the paragraph there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MODEL = (
    Path(__file__).resolve().parents[1] / "src" / "bettermemory" / "health_model.py"
)


def _comment_above(name: str) -> str:
    """The run of `#` lines directly above the module-level assignment."""
    source = _MODEL.read_text(encoding="utf-8")
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


@pytest.mark.parametrize(
    "name",
    ["_ENDORSEMENT_GRACE_DAYS", "_CROSS_REPO_MAX_ROOTS", "_RECOMMENDATION_SIZE_FLOOR"],
)
def test_the_constant_states_its_evidence(name: str) -> None:
    assert "Evidence:" in _comment_above(name)


def test_the_grace_names_the_bound_it_is_margin_over() -> None:
    comment = _comment_above("_ENDORSEMENT_GRACE_DAYS")
    assert "_PENDING_USE_TOKEN_TTL_SECONDS" in comment
    assert "ATTRIBUTION_LOOKBACK_SECONDS" in comment
    assert "Evidence: none in days" in comment
