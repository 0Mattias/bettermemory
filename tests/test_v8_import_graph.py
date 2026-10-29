"""Nothing outside the v8 migration's own code imports its modules (ENG-98).

`v8.py` (the frozen v8 readers) and `migrate_v8.py` (`bettermemory
migrate v8`, which reads an 8.x directory into the store) are the v8
migration modules. 9.x keeps them only so an 8.x directory can still be
imported, and the release that drops that import deletes them; the
deletion is clean only while nothing else imports them. This test reads
every Python file under src, bench and tools with ast, lazy imports
inside functions and `TYPE_CHECKING` blocks included, and holds the
imports of either module to the two modules themselves and the
allow-list `_ALLOWED`, each entry with its reason. The tests are outside
the pin: they import the two modules to test them, and go with them.

The mirror (`export --mirror`) is a 9.x export and stays. The v8 file
names and frontmatter it writes, which the migration shares, are in
`mirror_format.py`, which `mirror`, `v8` and `migrate_v8` import; so the
mirror takes no name from v8, and no module can take one through it.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PACKAGE = SRC / "bettermemory"
#: The trees the scan reads: the package, the benches and the tools.
_ROOTS = (PACKAGE, REPO / "bench", REPO / "tools")

#: The v8 migration modules the pin holds.
_V8_MODULES = frozenset({"bettermemory.v8", "bettermemory.migrate_v8"})
#: The modules besides those two that may import them, and why. A name
#: ending in `.` holds every module below it.
_ALLOWED: dict[str, str] = {
    "bettermemory.cli.migrate": "its one subcommand is `migrate v8` (pinned below)",
    "bench.parity.": "the parity bench records `migrate v8`'s round trip",
    "bench.daemon.latency": "seeds every bench store by migrating a v8 fixture",
}
#: What mirror.py takes from mirror_format: the names it writes with.
_MIRROR_TAKES = frozenset(
    {
        "EPISODES_DIR",
        "TOMBSTONE_DIR",
        "episode_metadata",
        "memory_filename",
        "memory_metadata",
        "tombstone_filename",
    }
)

_MODULE_STRING = re.compile(r"\.*[A-Za-z_]\w*(?:\.\w+)*")


def _module_name(path: Path) -> tuple[str, bool]:
    """The dotted name of a file under src, bench or tools, and whether it
    is a package's ``__init__``."""
    base = SRC if path.is_relative_to(SRC) else REPO
    parts = list(path.relative_to(base).with_suffix("").parts)
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


def _held(module: str, entry: str) -> bool:
    return module.startswith(entry) if entry.endswith(".") else module == entry


def _allowed(module: str) -> bool:
    return module in _V8_MODULES or any(_held(module, e) for e in _ALLOWED)


def _defined(tree: ast.Module) -> set[str]:
    """The names a module's top level binds by assignment or definition."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _graph() -> dict[str, set[str]]:
    """Each module of src, bench and tools that imports a v8 migration
    module, and which."""
    graph: dict[str, set[str]] = {}
    for root in _ROOTS:
        for path in sorted(root.rglob("*.py")):
            module, is_package = _module_name(path)
            source = path.read_text("utf-8")
            targets = _v8_targets(_imports(module, is_package, source))
            if targets:
                graph[module] = targets
    return graph


def test_nothing_outside_the_allow_list_imports_a_v8_migration_module() -> None:
    outside = {
        (importer, target)
        for importer, targets in _graph().items()
        if not _allowed(importer)
        for target in targets
    }
    assert outside == set(), "a new importer of a v8 migration module"


def test_every_entry_of_the_allow_list_still_imports_one() -> None:
    """An entry whose module stopped importing the v8 modules is deleted,
    so the list never holds a permission nothing uses; the scan seeing
    each one, and `migrate_v8` importing `v8`, is also what shows it
    reads src, bench and tools."""
    graph = _graph()
    unused = {e for e in _ALLOWED if not any(_held(m, e) for m in graph)}
    assert unused == set()
    assert "bettermemory.v8" in graph["bettermemory.migrate_v8"]


def test_mirror_takes_its_names_from_mirror_format_and_none_from_v8() -> None:
    """The names mirror.py imports from mirror_format are the ones it
    writes with, and neither module holds an object v8 or migrate_v8
    defines, so no module can take a v8 name through the mirror."""
    tree = ast.parse((PACKAGE / "mirror.py").read_text("utf-8"))
    taken = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "mirror_format"
        for alias in node.names
    }
    assert taken == _MIRROR_TAKES
    for name in ("bettermemory.mirror", "bettermemory.mirror_format"):
        held = {
            attr
            for attr, value in vars(importlib.import_module(name)).items()
            if getattr(value, "__module__", None) in _V8_MODULES
        }
        assert held == set(), name


def test_mirror_format_holds_what_v8_and_migrate_v8_take_from_it() -> None:
    """v8 and migrate_v8 import the shared names from mirror_format, not
    a copy of their own: neither defines one of them."""
    shared = set(importlib.import_module("bettermemory.mirror_format").__all__)
    assert "memory_metadata" in shared
    for name in sorted(_V8_MODULES):
        path = SRC / f"{name.replace('.', '/')}.py"
        assert _defined(ast.parse(path.read_text("utf-8"))) & shared == set(), name


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
        (
            "bench.capture.undo",
            False,
            "def run():\n    from bettermemory.v8 import TOMBSTONE_DIR\n",
            True,
        ),
        (
            "bench.capture.undo",
            False,
            "from bettermemory.mirror_format import TOMBSTONE_DIR\n",
            False,
        ),
    ],
)
def test_the_scan_reads_each_spelling_of_an_import(
    module: str, is_package: bool, source: str, expected: bool
) -> None:
    assert bool(_v8_targets(_imports(module, is_package, source))) is expected


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("bench.parity.migrate_v8", True),
        ("bench.parity", False),
        ("bench.daemon.latency", True),
        ("bench.daemon.latency_extra", False),
        ("bench.capture.one_call", False),
        ("bettermemory.cli.migrate", True),
        ("bettermemory.migrate_v8", True),
        ("bettermemory.mirror", False),
        ("bettermemory.mirror_format", False),
    ],
)
def test_the_allow_list_names_modules_and_one_tree(module: str, allowed: bool) -> None:
    assert _allowed(module) is allowed
