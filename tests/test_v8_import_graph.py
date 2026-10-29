"""Nothing outside the v8 migration modules imports them (ENG-98).

`v8.py` (the frozen v8 file format: its frontmatter shapes and file
names) and `migrate_v8.py` (`bettermemory migrate v8`, which reads an
8.x directory into the store) are the v8 migration modules. 9.x keeps
them only so an 8.x directory can still be imported, and the release
that drops that import deletes them; the deletion is clean only while
nothing else in the package imports them. This test reads every module
under src/bettermemory with ast, lazy imports inside functions and
`TYPE_CHECKING` blocks included, and holds the imports of either module
to the allow-list `_ALLOWED`: the two modules and `cli/migrate.py`,
which exists only for the v8 path (its one subcommand is `migrate v8`,
pinned below). The tests are outside the pin: they import the two
modules to test them.

At f66b6044, the base of ENG-98, the pin fails on one importer outside
the allow-list (and on `cli/migrate.py` too, before it joined the list
for the reason above). It is marked here, one `FAILING:` line per
importer, instead of being added to the allow-list:

FAILING: bettermemory.mirror imports bettermemory.v8
    (EPISODES_DIR, TOMBSTONE_DIR, active_filename_for_tombstone,
    episode_metadata, is_legacy_tombstone_name, memory_filename,
    memory_metadata, tombstone_filename): `export --mirror`, a 9.x
    export, writes the v8 file format from the shapes frozen in
    `v8.py`, and `cli/export.py` reaches v8 through it.

The test holds the importers outside the allow-list to exactly the
marked lines, so a new importer fails it, and so does a marked importer
that stops importing, whose line is then deleted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "bettermemory"

#: The v8 migration modules the pin holds.
_V8_MODULES = frozenset({"bettermemory.v8", "bettermemory.migrate_v8"})
# The allow-list, the modules that may import them: v8.py and
# migrate_v8.py, which import each other, and cli/migrate.py, which exists
# only for the v8 path (its one subcommand, `migrate v8`, imports
# `migrate_v8.migrate` in its handler).
_ALLOWED = _V8_MODULES | {"bettermemory.cli.migrate"}

_FAILING = re.compile(r"^FAILING: (\S+) imports (\S+)$", re.M)
_MODULE_STRING = re.compile(r"\.*[A-Za-z_]\w*(?:\.\w+)*")


def _module_name(path: Path) -> tuple[str, bool]:
    """The dotted name of a file under src, and whether it is a package's
    ``__init__``."""
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _absolute(name: str, level: int, package: str) -> str:
    """`name` imported at `level` dots from inside `package`."""
    if not level:
        return name
    parts = package.split(".")
    base = ".".join(parts[: len(parts) - (level - 1)])
    return f"{base}.{name}" if name else base


def _imports(module: str, is_package: bool, source: str) -> set[str]:
    """Every dotted name `source` imports, made absolute: the modules of
    ``import`` and ``from ... import`` statements wherever they stand,
    each ``from`` name as a submodule candidate, and a string constant
    that spells a module of the package (an ``importlib.import_module``
    argument)."""
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute(node.module or "", node.level, package)
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if _MODULE_STRING.fullmatch(text) and (
                text.startswith(".") or text.startswith("bettermemory.")
            ):
                level = len(text) - len(text.lstrip("."))
                found.add(_absolute(text.lstrip("."), level, package))
    return found


def _v8_targets(names: set[str]) -> set[str]:
    return {
        target
        for target in _V8_MODULES
        for name in names
        if name == target or name.startswith(target + ".")
    }


def _graph() -> dict[str, set[str]]:
    """Each module of the package that imports a v8 migration module, and
    which."""
    graph: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        module, is_package = _module_name(path)
        targets = _v8_targets(_imports(module, is_package, path.read_text("utf-8")))
        if targets:
            graph[module] = targets
    return graph


def test_nothing_outside_the_v8_migration_modules_imports_them() -> None:
    outside = {
        (importer, target)
        for importer, targets in _graph().items()
        if importer not in _ALLOWED
        for target in targets
    }
    marked = set(_FAILING.findall(__doc__ or ""))
    assert outside - marked == set(), "a new importer of a v8 migration module"
    assert marked - outside == set(), "a marked importer no longer imports"


def test_the_scan_sees_the_imports_the_allow_list_permits() -> None:
    """`migrate_v8` imports `v8` and `cli.migrate` imports `migrate_v8`,
    so a scan that sees nothing cannot pass the pin."""
    graph = _graph()
    assert "bettermemory.v8" in graph["bettermemory.migrate_v8"]
    assert "bettermemory.migrate_v8" in graph["bettermemory.cli.migrate"]


def test_cli_migrate_exists_only_for_the_v8_path() -> None:
    """The allow-list's reason for `cli/migrate.py`: its parser registers
    `migrate` and the one subcommand `v8`. A second migration there takes
    the module off the v8 path, and its import of `migrate_v8` then moves
    into a module of its own."""
    tree = ast.parse((PACKAGE / "cli" / "migrate.py").read_text("utf-8"))
    registered = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert registered == {"migrate", "v8"}


@pytest.mark.parametrize(
    ("module", "is_package", "source", "expected"),
    [
        ("bettermemory.mirror", False, "from .v8 import memory_filename\n", True),
        ("bettermemory.cli.export", False, "from .. import migrate_v8\n", True),
        ("bettermemory.cli", True, "from ..v8 import EPISODES_DIR\n", True),
        ("bettermemory.cli", True, "from .v8 import EPISODES_DIR\n", False),
        ("bettermemory.doctor", False, "import bettermemory.v8 as v8\n", True),
        ("bettermemory.doctor", False, "from bettermemory import v8\n", True),
        (
            "bettermemory.doctor",
            False,
            "def run():\n    from .migrate_v8 import migrate\n",
            True,
        ),
        (
            "bettermemory.doctor",
            False,
            "import importlib\nimportlib.import_module('bettermemory.v8')\n",
            True,
        ),
        ("bettermemory.doctor", False, "from .v8_notes import x\n", False),
        ("bettermemory.log", False, 'MIGRATE_V8 = "migrate_v8"\n', False),
    ],
)
def test_the_scan_reads_each_spelling_of_an_import(
    module: str, is_package: bool, source: str, expected: bool
) -> None:
    assert bool(_v8_targets(_imports(module, is_package, source))) is expected
