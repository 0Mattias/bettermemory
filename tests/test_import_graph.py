"""The package's import graph, read from its source.

`recall` imports nothing from `hook`, and `sessions` (the session join
both of them read) imports neither, at module level, inside a function
or under `TYPE_CHECKING`: `hook` is the recall surface's CLI shim and
imports `recall`, never the reverse. The modules split out of `recall`
(ENG-96) are held to the same edges. `hook` keeps every session-join
name that moved out of it importable under its old path; the probe and
the settlement live in `recall_settle`, read from `recall`, and `hook`
binds neither. No import cycle runs at module level anywhere in the
package.

The graph is every `import` statement under `src/bettermemory`, read with
`ast`, so an import inside a function body is an edge though the
interpreter runs it only when the function is called. An import a module
builds at run time (`importlib.import_module`) is not read.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import bettermemory
from bettermemory import hook, sessions

from .conftest import shielded_child_env

PACKAGE = "bettermemory"
SOURCE = Path(bettermemory.__file__).resolve().parent

#: `recall` and the modules split out of it (ENG-96, ENG-97): `hook` imports them
#: only inside the functions that call them, and `sessions` never.
RECALL_FAMILY: tuple[str, ...] = (
    "bettermemory.recall",
    "bettermemory.recall_state",
    "bettermemory.recall_admission",
    "bettermemory.recall_brief",
    "bettermemory.recall_settle",
    "bettermemory.recall_prompt",
)

#: The edges that must not exist, of any kind: a module-level import, an
#: import inside a function, or one under `TYPE_CHECKING`. The modules
#: split out of `recall` are held to the same edges as `recall`.
FORBIDDEN: dict[str, frozenset[str]] = {
    **{module: frozenset({"bettermemory.hook"}) for module in RECALL_FAMILY},
    "bettermemory.sessions": frozenset({*RECALL_FAMILY, "bettermemory.hook"}),
}

#: The module-level import cycles the package holds, each as the sorted
#: modules of one strongly connected component. Empty when this file was
#: written; a cycle added later fails `test_no_module_level_import_cycle`
#: until it is broken or named here.
KNOWN_CYCLES: frozenset[tuple[str, ...]] = frozenset()

#: Every name that moved out of `hook` and stays importable from it, by
#: the module that holds it now. The probe and the settlement moved to
#: `recall_settle`, read from `recall`, with no re-export from `hook`
#: (`test_the_seams_that_left_hook_are_gone`).
MOVED_FROM_HOOK: dict[str, tuple[str, ...]] = {
    "bettermemory.sessions": (
        "_OUT_OF_PROCESS_TRIGGERS",
        "_disabled_scopes_from_events",
        "_latest_in_process_session",
        "_session_of",
        "bridged_session",
        "event_joined",
        "harness_joined_sessions",
        "hook_spans",
        "joined_sessions",
        "live_harness_sessions",
        "runs_beside",
    ),
}


@dataclass(frozen=True)
class Edge:
    """One import statement's dependency of `source` on `target`."""

    source: str
    target: str
    line: int
    #: Whether the interpreter runs it when `source` is imported: not
    #: inside a function body and not under `if TYPE_CHECKING:`.
    module_level: bool


@dataclass(frozen=True)
class _Module:
    name: str
    path: Path
    is_package: bool


def _modules() -> dict[str, _Module]:
    found: dict[str, _Module] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        parts = list(path.relative_to(SOURCE.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        name = ".".join(parts)
        found[name] = _Module(name=name, path=path, is_package=is_package)
    return found


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imports(
    body: list[ast.stmt], *, module_level: bool
) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Every import statement under `body`, with whether it runs when the
    module is imported."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, module_level
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _imports(node.body, module_level=False)
        elif isinstance(node, ast.If):
            yield from _imports(
                node.body,
                module_level=module_level and not _is_type_checking(node.test),
            )
            yield from _imports(node.orelse, module_level=module_level)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            for block in (node.body, node.orelse, node.finalbody):
                yield from _imports(block, module_level=module_level)
            for handler in node.handlers:
                yield from _imports(handler.body, module_level=module_level)
        else:
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(node, field, None)
                if isinstance(nested, list):
                    yield from _imports(nested, module_level=module_level)
            if isinstance(node, ast.Match):
                for case in node.cases:
                    yield from _imports(case.body, module_level=module_level)


def _targets(
    node: ast.Import | ast.ImportFrom, module: _Module, known: dict[str, _Module]
) -> list[str]:
    """The package modules one import statement depends on: the module it
    names, or the submodule when a `from` import names one."""
    if isinstance(node, ast.Import):
        return [
            alias.name
            for alias in node.names
            if alias.name == PACKAGE or alias.name.startswith(PACKAGE + ".")
        ]
    if node.level:
        base = module.name.split(".")
        if not module.is_package:
            base = base[:-1]
        if node.level > 1:
            base = base[: len(base) - (node.level - 1)]
        named = ".".join([*base, *([node.module] if node.module else [])])
    else:
        named = node.module or ""
    if not (named == PACKAGE or named.startswith(PACKAGE + ".")):
        return []
    found: list[str] = []
    for alias in node.names:
        submodule = f"{named}.{alias.name}"
        found.append(submodule if submodule in known else named)
    return found


def import_edges() -> list[Edge]:
    """Every dependency between two modules of the package, one per
    imported name."""
    known = _modules()
    edges: list[Edge] = []
    for module in known.values():
        tree = ast.parse(module.path.read_text(encoding="utf-8"), str(module.path))
        for node, module_level in _imports(tree.body, module_level=True):
            for target in _targets(node, module, known):
                if target != module.name:
                    edges.append(Edge(module.name, target, node.lineno, module_level))
    return edges


def module_level_cycles(edges: list[Edge]) -> set[tuple[str, ...]]:
    """The strongly connected components of more than one module in the
    graph of module-level edges (Tarjan), each as its sorted modules."""
    graph: dict[str, set[str]] = {}
    for edge in edges:
        if edge.module_level:
            graph.setdefault(edge.source, set()).add(edge.target)
            graph.setdefault(edge.target, set())
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph[node]):
            if successor not in index:
                visit(successor)
                low[node] = min(low[node], low[successor])
            elif successor in on_stack:
                low[node] = min(low[node], index[successor])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                found.add(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return found


@pytest.fixture(scope="module")
def edges() -> list[Edge]:
    return import_edges()


def test_the_walk_tells_when_an_import_runs() -> None:
    """Premise: an import at module level, in a class body, in either arm
    of a `try` or in the `else` of a TYPE_CHECKING test runs on import;
    one under TYPE_CHECKING or in a function body does not. Every one of
    them is read."""
    tree = ast.parse(
        "import bettermemory.a\n"
        "if TYPE_CHECKING:\n"
        "    import bettermemory.b\n"
        "else:\n"
        "    import bettermemory.c\n"
        "try:\n"
        "    import bettermemory.d\n"
        "except ImportError:\n"
        "    import bettermemory.e\n"
        "class K:\n"
        "    import bettermemory.f\n"
        "    def m(self):\n"
        "        import bettermemory.g\n"
        "def f():\n"
        "    import bettermemory.h\n"
    )
    seen = {
        node.names[0].name.rsplit(".", 1)[-1]: runs
        for node, runs in _imports(tree.body, module_level=True)
    }
    assert seen == {
        "a": True,
        "b": False,
        "c": True,
        "d": True,
        "e": True,
        "f": True,
        "g": False,
        "h": False,
    }


def test_the_graph_holds_the_edges_the_shim_is_built_on(edges: list[Edge]) -> None:
    """Premise, on the package itself: `hook` and `recall` import
    `sessions` at module level, and `hook` imports `recall` only where it
    does not run on import and no module of the recall family at module
    level, so the assertions below read a real graph."""
    kinds = {(edge.source, edge.target, edge.module_level) for edge in edges}
    assert ("bettermemory.hook", "bettermemory.sessions", True) in kinds
    assert ("bettermemory.recall", "bettermemory.sessions", True) in kinds
    assert ("bettermemory.hook", "bettermemory.recall", False) in kinds
    on_import = sorted(
        target
        for source, target, module_level in kinds
        if source == "bettermemory.hook" and target in RECALL_FAMILY and module_level
    )
    assert on_import == [], on_import


def test_recall_and_sessions_do_not_import_the_hook_shim(edges: list[Edge]) -> None:
    """No import of any kind runs from `recall` to `hook`, or from
    `sessions` to `recall` or `hook`."""
    found = [
        f"{edge.source}:{edge.line} -> {edge.target}"
        for edge in edges
        if edge.target in FORBIDDEN.get(edge.source, frozenset())
    ]
    assert found == [], found


@pytest.mark.parametrize(
    ("module", "absent"),
    [
        ("bettermemory.recall", ("bettermemory.hook",)),
        *(
            (child, ("bettermemory.hook", "bettermemory.recall"))
            for child in RECALL_FAMILY[1:]
        ),
        ("bettermemory.sessions", (*RECALL_FAMILY, "bettermemory.hook")),
        ("bettermemory.hook", RECALL_FAMILY),
    ],
)
def test_importing_a_module_loads_none_of_the_others(
    module: str, absent: tuple[str, ...]
) -> None:
    """In a fresh interpreter: `recall` loads no `hook`, no module split
    out of it loads `hook` or `recall`, `sessions` loads none of them,
    and `hook` loads none of the recall family, which it imports only
    inside the functions that call it."""
    code = (
        "import sys\n"
        f"import {module}\n"
        f"loaded = sorted(m for m in {list(absent)!r} if m in sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=shielded_child_env(),
    )
    assert result.returncode == 0, result.stderr


def test_no_module_level_import_cycle(edges: list[Edge]) -> None:
    """The package's module-level imports form no cycle beyond the ones
    `KNOWN_CYCLES` names. An import inside a function or under
    TYPE_CHECKING is not an edge here: it is how a module that must
    reach another one later in the import order does so."""
    cycles = module_level_cycles(edges)
    assert cycles <= KNOWN_CYCLES, sorted(cycles - KNOWN_CYCLES)


def test_the_cycle_finder_reports_a_cycle() -> None:
    """Premise: two modules that import each other at module level are
    one cycle; the same pair through a function-level import is none."""
    loop = [
        Edge("bettermemory.a", "bettermemory.b", 1, True),
        Edge("bettermemory.b", "bettermemory.a", 1, True),
        Edge("bettermemory.b", "bettermemory.c", 2, True),
    ]
    assert module_level_cycles(loop) == {("bettermemory.a", "bettermemory.b")}
    deferred = [loop[0], Edge("bettermemory.b", "bettermemory.a", 1, False)]
    assert module_level_cycles(deferred) == set()


@pytest.mark.parametrize(
    ("home", "name"),
    [(home, name) for home, names in MOVED_FROM_HOOK.items() for name in names],
)
def test_every_moved_name_is_importable_from_hook(home: str, name: str) -> None:
    """`hook` re-exports every name that moved out of it: the object read
    from `hook` is the one its new module holds."""
    holder = {"bettermemory.sessions": sessions}[home]
    assert getattr(hook, name) is getattr(holder, name)


@pytest.mark.parametrize(
    "name", ["capture_origin", "probe_for_miss", "_probe_message", "settle_interrupted"]
)
def test_the_seams_that_left_hook_are_gone(name: str) -> None:
    """The Stop and the recall read the caller's origin through
    `origin.capture_caller` and run the probe and the interrupted-turn
    settlement from `recall_settle`, which `hook` reads inside the
    functions that call them (the probe from `recall_settle`, the
    settlement through `recall`), so `hook` binds none of these four
    names: a test that patches one on `hook` fails at the patch instead
    of patching a name nothing reads."""
    with pytest.raises(AttributeError, match=f"no attribute {name!r}"):
        getattr(hook, name)
