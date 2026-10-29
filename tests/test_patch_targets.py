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

A name the patched module binds only by an import, as every re-export of
the splits is, is held to a stricter reading: the module's own load counts
only inside a function, where no parameter or local of the same name
shadows it, since a load made as the module is imported (an alias, a
decorator, a default) ran before any patch was placed.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
        target = _module_of(parts, bound)
        if target is not None:
            sites.append((node.lineno, target, second.value))
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


# ---------------------------------------------------------------------------
# Parts: modules split out of another (ENG-97)
# ---------------------------------------------------------------------------


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@dataclass
class _Imports(ast.NodeVisitor):
    """The package modules a module imports as it is imported (`eager`), and
    those it imports only inside a function or for type checking (`lazy`)."""

    current: str
    is_package: bool
    eager: set[str] = field(default_factory=set)
    lazy: set[str] = field(default_factory=set)
    depth: int = 0

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self.depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def _add(self, module: str) -> None:
        (self.lazy if self.depth else self.eager).add(module)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in MODULES:
                self._add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = (
            _resolve(self.current, node.level, node.module, self.is_package)
            if node.level
            else (node.module or "")
        )
        for alias in node.names:
            full = f"{base}.{alias.name}"
            if full in MODULES:
                self._add(full)
            elif base in MODULES:
                self._add(base)


def _parts() -> dict[str, str]:
    """{part: module}: each module split out of another. Only that module
    imports it, and only inside a function (or for type checking), while
    the part imports that module as it is imported."""
    found: dict[str, _Imports] = {}
    for module, path in MODULES.items():
        imports = _Imports(module, path.name == "__init__.py")
        imports.visit(_tree(path))
        found[module] = imports
    importers: dict[str, set[str]] = {}
    for module, imports in found.items():
        for target in imports.eager | imports.lazy:
            importers.setdefault(target, set()).add(module)
    parts: dict[str, str] = {}
    for part, owners in importers.items():
        if len(owners) != 1:
            continue
        (owner,) = owners
        if owner != part and owner in found[part].eager:
            parts[part] = owner
    return parts


def _bound_at_import(body: list[ast.stmt]) -> set[str]:
    """The names a module's top level binds as it is imported, outside
    `if TYPE_CHECKING:`."""
    bound: set[str] = set()
    for node in body:
        if isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                bound |= _bound_at_import(node.body)
            bound |= _bound_at_import(node.orelse)
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                bound |= _bound_at_import(block)
            for handler in node.handlers:
                bound |= _bound_at_import(handler.body)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bound |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return bound


def _binds_at_import(tree: ast.Module, current: str, name: str) -> bool:
    """Whether a module binds `name` as it is imported and reads that
    binding at run time, so a patch placed on another module's `name`
    later never reaches it."""
    reads = _Reads(_bindings(tree, current, False), current, False)
    reads.visit(tree)
    return name in reads.names and name in _bound_at_import(tree.body)


def test_no_part_binds_a_name_patched_on_its_module() -> None:
    """A part split out of a module reads that module's names when it runs,
    so a patch on `module.name` reaches the readers the split moved, and a
    part first imported under a patch does not keep the fake. A part that
    binds a name patched on its module as it is imported fails here."""
    parts = _parts()
    owners = set(parts.values())
    assert {f"{PACKAGE}.backup", f"{PACKAGE}.config", f"{PACKAGE}.store"} <= owners
    trees = {part: _tree(MODULES[part]) for part in parts}
    early: list[str] = []
    for top in PATCHING:
        for path in _files(top):
            for line, module, name in _patch_sites(path):
                for part, owner in parts.items():
                    if owner == module and _binds_at_import(trees[part], part, name):
                        rel = path.relative_to(ROOT).as_posix()
                        early.append(
                            f"{rel}:{line} patches {module}.{name}, but {part} "
                            f"binds {name} as it is imported"
                        )
    assert early == [], "\n".join(early)


#: The review's cases (ENG-97 set B, M1): a name a part reads, which no test
#: patched when the part bound it at import, and the binding it had then.
REVIEW_CASES = (
    (
        f"{PACKAGE}.backup",
        "_store_facts",
        f"{PACKAGE}.backup_restore",
        "from .backup import _store_facts",
    ),
    (
        f"{PACKAGE}.config",
        "_coerce_float",
        f"{PACKAGE}.config_sections",
        "from .config import _coerce_float",
    ),
    (
        f"{PACKAGE}.store",
        "key_state",
        f"{PACKAGE}.store_open",
        "from .store_keys import key_state",
    ),
)


@pytest.mark.parametrize(("module", "name", "part", "binding"), REVIEW_CASES)
def test_the_rule_refuses_the_binding_each_review_case_had(
    module: str, name: str, part: str, binding: str
) -> None:
    """Each part now reads the name through its module when it runs, and
    the binding it had before is one the rule above refuses."""
    assert _parts()[part] == module
    assert (module, name) in _reads_of(MODULES[part], part, False).attrs
    assert not _binds_at_import(_tree(MODULES[part]), part, name)
    before = ast.parse(f"{binding}\n\n\ndef reader() -> object:\n    return {name}\n")
    assert _binds_at_import(before, part, name)


def _spy(real: Callable[..., Any], callers: list[str]) -> Callable[..., Any]:
    """`real`, noting the module of each caller."""

    def spy(*args: Any, **kwargs: Any) -> Any:
        callers.append(str(sys._getframe(1).f_globals.get("__name__")))
        return real(*args, **kwargs)

    return spy


def test_a_patch_on_backup_reaches_the_restore_plan(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import backup, backup_restore
    from bettermemory.store import STORE_FILENAME, Store

    store_file = tmp_path / "store" / STORE_FILENAME
    Store.create(store_file, keys_dir=keys_dir).close()
    bundle = Path(backup.create_backup(store_file, keys_dir=keys_dir)["bundle"])
    # The part is loaded before the patch, as in a process that restored once.
    assert backup_restore.__name__ in sys.modules
    callers: list[str] = []
    monkeypatch.setattr(backup, "_store_facts", _spy(backup._store_facts, callers))
    report = backup.restore_bundle(bundle, store_file, apply=False, keys_dir=keys_dir)
    assert report["status"] == "planned", report["detail"]
    assert f"{PACKAGE}.backup_restore" in callers


def test_a_patch_on_config_reaches_the_section_builders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import config, config_sections

    # The part is loaded before the patch, as in a process that read a config.
    assert config_sections.__name__ in sys.modules
    callers: list[str] = []
    monkeypatch.setattr(config, "_coerce_float", _spy(config._coerce_float, callers))
    config_sections._behavior_config({}, tmp_path / "config.toml")
    assert f"{PACKAGE}.config_sections" in callers


def test_a_patch_on_store_reaches_the_open_steps(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import store, store_open
    from bettermemory.store import STORE_FILENAME, Store

    path = tmp_path / STORE_FILENAME
    Store.create(path, keys_dir=keys_dir).close()
    # The part is loaded before the patch, as in a process that opened a store.
    assert store_open.__name__ in sys.modules
    callers: list[str] = []
    monkeypatch.setattr(store, "key_state", _spy(store.key_state, callers))
    Store.open(path, keys_dir=keys_dir).close()
    assert f"{PACKAGE}.store_open" in callers


# ---------------------------------------------------------------------------
# Re-exports: names a module imports only to hand them on
# ---------------------------------------------------------------------------

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
_Comprehension = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp


def _locals_of(node: _Function) -> tuple[set[str], set[str]]:
    """The names a function binds in its own scope (its parameters and the
    names it assigns, deletes, imports, defines or catches anywhere in its
    body, less those it declares `global` or `nonlocal`), and those of
    them it binds by an import."""
    args = node.args
    bound = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    bound |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    imported: set[str] = set()
    declared: set[str] = set()
    stack: list[ast.AST] = (
        list(node.body) if isinstance(node.body, list) else [node.body]
    )
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
            continue
        if isinstance(child, (ast.Lambda, *_COMPREHENSIONS)):
            continue
        if isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Load):
            bound.add(child.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            imported |= {a.asname or a.name.split(".")[0] for a in child.names}
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            bound.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            bound.add(child.rest)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            declared |= set(child.names)
        stack.extend(ast.iter_child_nodes(child))
    return (bound | imported) - declared, imported - declared


@dataclass
class _CallTimeReads(ast.NodeVisitor):
    """The module-level names a module reads as one of its functions runs
    (`names`), and the `module.name` reads its functions make (`attrs`).

    Stricter than `_Reads`: a load made as the module is imported (at its
    top level, in a class body, a decorator or a default) ran before any
    patch, and a load of a parameter or local of the same name never
    reaches the module's binding, so neither counts."""

    bound: dict[str, str]
    current: str
    is_package: bool
    names: set[str] = field(default_factory=set)
    attrs: set[tuple[str, str]] = field(default_factory=set)
    scopes: list[tuple[set[str], set[str]]] = field(default_factory=list)
    functions: int = 0

    def _reaches_module(self, name: str, *, local_imports: bool) -> bool:
        """Whether a load of `name` here, as a function runs, reads the
        module's own binding; with `local_imports`, a function's own
        import of the name also counts, for the root of `module.name`."""
        if self.functions == 0:
            return False
        for bound, imported in reversed(self.scopes):
            if name in bound:
                return local_imports and name in imported
        return True

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        for stmt in node.orelse:
            self.visit(stmt)

    def _enter(self, node: _Function, body: list[ast.AST]) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        self.scopes.append(_locals_of(node))
        self.functions += 1
        for item in body:
            self.visit(item)
        self.functions -= 1
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._enter(node, list(node.body))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._enter(node, list(node.body))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._enter(node, [node.body])

    def _comprehension(self, node: _Comprehension) -> None:
        targets = {
            n.id
            for generator in node.generators
            for n in ast.walk(generator.target)
            if isinstance(n, ast.Name)
        }
        self.scopes.append((targets, set()))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and self._reaches_module(
            node.id, local_imports=False
        ):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _dotted(node.value)
        if (
            parts is not None
            and isinstance(node.ctx, ast.Load)
            and self._reaches_module(parts[0], local_imports=True)
        ):
            module = _module_of(parts, self.bound)
            if module is not None:
                self.attrs.add((module, node.attr))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Inside a function, `from m import n` reads m's binding of n when
        # the function runs, so a patch placed on m.n before the call
        # reaches it.
        if self.functions == 0:
            return
        base = (
            _resolve(self.current, node.level, node.module, self.is_package)
            if node.level
            else (node.module or "")
        )
        if base in MODULES:
            for alias in node.names:
                self.attrs.add((base, alias.name))


def _call_time_reads(
    tree: ast.Module, current: str, is_package: bool
) -> _CallTimeReads:
    reads = _CallTimeReads(_bindings(tree, current, is_package), current, is_package)
    reads.visit(tree)
    return reads


def _only_imported(body: list[ast.stmt]) -> set[str]:
    """The names a module's top level binds by an import as it is imported
    (outside `if TYPE_CHECKING:`) and binds no other way."""
    imported: set[str] = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.Try):
            stack.extend([*node.body, *node.orelse, *node.finalbody])
            for handler in node.handlers:
                stack.extend(handler.body)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imported |= {a.asname or a.name.split(".")[0] for a in node.names}
    defined = _bound_at_import(
        [n for n in body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    )
    return imported - defined


def test_no_patch_targets_a_name_its_module_only_hands_on() -> None:
    """A patch on `module.name`, where `module` binds `name` only by an
    import, targets a name a function reads through `module` as it runs:
    one of `module`'s own, as a global no parameter or local shadows, or
    another module's, the bench's or the tools', as `module.name`. A name
    a module imports only to re-export it is read where it is defined, or
    by the module that took the reader in a split, and a patch on the
    re-export replaces a binding nothing runs."""
    hands_on: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    attrs: set[tuple[str, str]] = set()
    for module, path in MODULES.items():
        tree = _tree(path)
        reads = _call_time_reads(tree, module, path.name == "__init__.py")
        hands_on[module] = _only_imported(tree.body)
        names[module] = reads.names
        attrs |= reads.attrs
    for top in READING:
        for path in _files(top):
            attrs |= _call_time_reads(_tree(path), top, False).attrs
    sites = 0
    unread: list[str] = []
    for top in PATCHING:
        for path in _files(top):
            for line, module, name in _patch_sites(path):
                if name not in hands_on[module]:
                    continue
                sites += 1
                if name not in names[module] and (module, name) not in attrs:
                    rel = path.relative_to(ROOT).as_posix()
                    unread.append(
                        f"{rel}:{line} patches {module}.{name}, which {module} "
                        "imports and no function reads through it"
                    )
    assert sites > 0, sites
    assert unread == [], "\n".join(unread)


#: A module that imports two names and reads neither as a function runs:
#: one at its top level, one in a class body, one through a parameter of
#: the same name and one through its own import.
_HANDS_ON = """\
from .recall_result import HookResult as HookResult
from .recall_state import RULE

ALIAS = HookResult


class Answer:
    kind = RULE


def shadowed(HookResult: object) -> object:
    return HookResult


def imported() -> object:
    from .recall_result import HookResult

    return HookResult
"""


def test_the_call_time_reader_refuses_the_loads_the_first_rule_counts() -> None:
    current = f"{PACKAGE}.recall"
    tree = ast.parse(_HANDS_ON)
    assert _only_imported(tree.body) == {"HookResult", "RULE"}
    assert {"HookResult", "RULE"} <= _reads_of_tree(tree, current).names
    assert not {"HookResult", "RULE"} & _call_time_reads(tree, current, False).names
    reader = ast.parse(_HANDS_ON + "\n\ndef reader() -> object:\n    return RULE\n")
    assert "RULE" in _call_time_reads(reader, current, False).names


def _reads_of_tree(tree: ast.Module, current: str) -> _Reads:
    reads = _Reads(_bindings(tree, current, False), current, False)
    reads.visit(tree)
    return reads
