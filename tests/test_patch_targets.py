"""Every patch a test or a bench places on a package module reaches a reader.

A name a module binds but never reads is not a patch point: a test that
patches it on that module replaces a binding nothing runs, and the code
under test keeps calling the original. The ENG-96 splits left many such
bindings on purpose (a parent module re-exports every name a caller
imported from it, so no import path changes), and a patch that followed a
name to its new reader was repointed in the same commit. This test holds
the line for every patch site at once: the module named by a
`monkeypatch.setattr`, `patch` or `patch.object` call must read the name
itself at run time, or another module of the package, the bench or the
tools must read it as `module.name` at call time, which a function-level
`from module import name` also does.

A read counts when it runs: a name loaded outside annotations and outside
`if TYPE_CHECKING:` blocks. A module-level `from m import n` elsewhere does
not count, since it bound the original before the patch was placed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import bettermemory

PACKAGE = "bettermemory"
SOURCE = Path(bettermemory.__file__).resolve().parent
ROOT = SOURCE.parent.parent
#: The trees whose contents are never read or printed (bench corpora).
SKIP_TREES = ("bench/integrity/v1", "bench/decisions/data", "bench/decisions/results")
#: The directories whose patches are held to this rule.
PATCHING = ("tests", "bench")
#: The directories whose call-time reads make a name a patch point.
READING = ("bench", "tools")


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SOURCE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


MODULES: dict[str, Path] = {_module_name(p): p for p in SOURCE.rglob("*.py")}


def _resolve(current: str, level: int, module: str | None, is_package: bool) -> str:
    base = current.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def _bindings(tree: ast.AST, current: str, is_package: bool) -> dict[str, str]:
    """Local names bound to package modules by an import of any form, at any depth."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    head = alias.name.split(".")[0]
                    bound[alias.asname or head] = alias.name if alias.asname else head
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve(current, node.level, node.module, is_package)
                if node.level
                else (node.module or "")
            )
            if base != PACKAGE and not base.startswith(PACKAGE + "."):
                continue
            for alias in node.names:
                full = f"{base}.{alias.name}"
                if full in MODULES:
                    bound[alias.asname or alias.name] = full
    return bound


def _dotted(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else [*head, node.attr]
    return None


def _module_of(parts: list[str], bound: dict[str, str]) -> str | None:
    """The package module a local dotted path names, or None."""
    full = [*bound[parts[0]].split("."), *parts[1:]] if parts[0] in bound else parts
    name = ".".join(full)
    return name if name in MODULES else None


@dataclass
class _Reads(ast.NodeVisitor):
    """The names a module reads at run time, and the `module.name` reads it makes."""

    bound: dict[str, str]
    current: str
    is_package: bool
    names: set[str] = field(default_factory=set)
    attrs: set[tuple[str, str]] = field(default_factory=set)
    depth: int = 0

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        ):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def _visit_but_annotations(self, node: ast.AST) -> None:
        for name, value in ast.iter_fields(node):
            if name in ("annotation", "returns"):
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, ast.AST):
                    self.visit(item)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.depth += 1
        self._visit_but_annotations(node)
        self.depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.depth += 1
        self._visit_but_annotations(node)
        self.depth -= 1

    def visit_arg(self, node: ast.arg) -> None:
        self._visit_but_annotations(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_but_annotations(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _dotted(node.value)
        if parts is not None and isinstance(node.ctx, ast.Load):
            module = _module_of(parts, self.bound)
            if module is not None:
                self.attrs.add((module, node.attr))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Inside a function, `from m import n` reads m's binding of n when the
        # function runs, so a patch placed on m.n before the call reaches it.
        if self.depth == 0:
            return
        base = (
            _resolve(self.current, node.level, node.module, self.is_package)
            if node.level
            else (node.module or "")
        )
        if base in MODULES:
            for alias in node.names:
                self.attrs.add((base, alias.name))


def _reads_of(path: Path, current: str, is_package: bool) -> _Reads:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reads = _Reads(_bindings(tree, current, is_package), current, is_package)
    reads.visit(tree)
    return reads


def _files(top: str) -> list[Path]:
    return [
        p
        for p in sorted((ROOT / top).rglob("*.py"))
        if not any(
            p.relative_to(ROOT).as_posix().startswith(t + "/") for t in SKIP_TREES
        )
    ]


def _patch_sites(path: Path) -> list[tuple[int, str, str]]:
    """Every (line, module, name) a `setattr`, `patch` or `patch.object` call targets."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bound = _bindings(tree, "tests", False)
    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        head = _dotted(func)
        if head is None or head[-1] not in ("setattr", "patch", "object"):
            continue
        if head[-1] == "object" and (len(head) < 2 or head[-2] != "patch"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            module, _, name = first.value.rpartition(".")
            if module in MODULES and name:
                sites.append((node.lineno, module, name))
            continue
        if len(node.args) < 2:
            continue
        parts = _dotted(first)
        second = node.args[1]
        if parts is None or not (
            isinstance(second, ast.Constant) and isinstance(second.value, str)
        ):
            continue
        module = _module_of(parts, bound)
        if module is not None:
            sites.append((node.lineno, module, second.value))
    return sites


def test_every_patched_name_has_a_reader() -> None:
    """A patch on `module.name` in tests/ or bench/ targets a name `module`
    reads at run time, or one another module reads as `module.name` at
    call time; a binding kept only so a patch does not raise is a patch
    on nothing."""
    names: dict[str, set[str]] = {}
    attrs: set[tuple[str, str]] = set()
    for module, path in MODULES.items():
        reads = _reads_of(path, module, path.name == "__init__.py")
        names[module] = reads.names
        attrs |= reads.attrs
    for top in READING:
        for path in _files(top):
            attrs |= _reads_of(path, top, False).attrs
    sites = 0
    unread: list[str] = []
    for top in PATCHING:
        for path in _files(top):
            for line, module, name in _patch_sites(path):
                sites += 1
                if name not in names[module] and (module, name) not in attrs:
                    rel = path.relative_to(ROOT).as_posix()
                    unread.append(
                        f"{rel}:{line} patches {module}.{name}, which nothing reads"
                    )
    assert sites > 400, sites
    assert unread == [], "\n".join(unread)
