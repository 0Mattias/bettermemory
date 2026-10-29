"""Every module of the package stays under the 2,000-line bound (ENG-96).

The bound is the one the splits of `store`, `membership`, `handlers.write`,
`recall`, `verify`, `search`, `origin`, `eval`, `health` and `location`
were made to; a module that grows past it is split, not excused here.
"""

from __future__ import annotations

from pathlib import Path

import bettermemory

SOURCE = Path(bettermemory.__file__).resolve().parent
#: The bound, in lines of source including blank and comment lines.
MAX_LINES = 2_000


def test_every_module_is_under_the_bound() -> None:
    """No file under `src/bettermemory` reaches `MAX_LINES` lines."""
    over = sorted(
        f"{path.relative_to(SOURCE.parent).as_posix()}: {lines} lines"
        for path in SOURCE.rglob("*.py")
        if (lines := len(path.read_text(encoding="utf-8").splitlines())) >= MAX_LINES
    )
    assert over == [], "\n".join(over)
