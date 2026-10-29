"""Every function of the package stays under the 150-line bound (ENG-97).

The bound is the one the rc7 review used (its finding 6) and the splits of
the 49 functions over it at 9.0.0rc13 were made to; a function that grows
past it is split into named steps, not excused here, unless the comment on
the line above its `def` begins `# over 150 lines:` and argues why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import bettermemory

SOURCE = Path(bettermemory.__file__).resolve().parent
#: The bound, in lines from the first decorator or `def` to the last line.
MAX_LINES = 150
#: What an argued exception's comment begins with, on the line above `def`.
EXCEPTION_MARK = "# over 150 lines:"


def _long_functions(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        length = node.end_lineno - first + 1 if node.end_lineno else 0
        if length <= MAX_LINES:
            continue
        above = lines[first - 2].strip() if first >= 2 else ""
        if above.startswith(EXCEPTION_MARK):
            continue
        where = path.relative_to(SOURCE.parent).as_posix()
        found.append(f"{where}:{node.lineno} {node.name}: {length} lines")
    return found


def test_every_function_is_under_the_bound() -> None:
    """No function under `src/bettermemory` is over `MAX_LINES` lines
    without an argued exception above it."""
    over = sorted(
        line for path in SOURCE.rglob("*.py") for line in _long_functions(path)
    )
    assert over == [], "\n".join(over)
