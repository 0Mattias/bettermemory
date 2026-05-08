"""Drift test for the system-prompt addendum.

`SYSTEM_PROMPT_ADDENDUM` in `prompts.py` and the fenced code block in
`docs/system_prompt.md` are the same instruction surface — one is exported
to programmatic consumers (clients embedding the addendum from the package),
the other is what a human copies into their CLAUDE.md. If they drift, code
clients and doc readers see different rules. There's no build step that
generates one from the other; this test fails the suite when they diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM


# Match the first ```...``` fence in the doc — the addendum is the first one.
_DOC_FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)


def test_addendum_matches_docs() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "system_prompt.md"
    text = doc_path.read_text(encoding="utf-8")
    matches = _DOC_FENCE_RE.findall(text)
    assert matches, f"no fenced code block in {doc_path}"

    canonical = matches[0].strip()
    expected = SYSTEM_PROMPT_ADDENDUM.strip()
    assert canonical == expected, (
        "SYSTEM_PROMPT_ADDENDUM in prompts.py has drifted from "
        "docs/system_prompt.md. Update both in sync."
    )
