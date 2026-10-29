"""The guard on membership decisions (design 8.2): every site that decides
whether a record reaches a caller decides through `membership.admitted`,
and rc3's origin tests decide nothing outside the places that keep them
as data.

The guard reads the source of every module under src/bettermemory and
src/bettermemory/handlers. It fails when a call to one of rc3's origin
tests (`should_include_for_caller`, `worktrees_match`, `repos_match`,
`in_repository`, `in_project`) appears anywhere the allow-list below does
not name (origin.py, health.py and the rc3_admitted helper, by name), with
the reason per entry, and when a membership site of
design 8.2 does not import `admitted` from membership or `Admission` from
admission. Unit RS-F7 adds two (P12): a global record's named projects
are read in membership.py and admission.py alone, and the walk over a
record's ``supersedes`` and ``extends`` links is `in_force`'s alone.
A site that takes its Admission from another module's decision names
that import in `_SITE_IMPORTS` (hook.py's Stop reads
`recall.caller_admission`) and the guard accepts it there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "bettermemory"

#: rc3's origin tests: membership by the origin columns.
_ORIGIN_TESTS = frozenset(
    {
        "should_include_for_caller",
        "worktrees_match",
        "repos_match",
        "in_repository",
        "in_project",
    }
)

#: Modules that may call them anywhere, with the reason.
_MODULES: dict[str, str] = {
    "origin.py": "defines the origin tests, kept as data for the rc3_admitted "
    "shadow column (design 8.11)",
    "health.py": "path and commit-drift checks of the origin columns, which "
    "stay as data (health's path probing), not a delivery",
}

#: The prefix of the helper that computes the rc3_admitted shadow column
#: (design 8.11, phase 6c): rc3's answer computed beside the membership
#: one and recorded, never delivered by. Allowed by name, in any module.
_RC3_HELPER = "_rc3_"


def _rc3_function(function: str | None) -> bool:
    """Whether `function` is the rc3_admitted helper (`_RC3_HELPER`)."""
    return function is not None and function.startswith(_RC3_HELPER)


#: The membership sites of design 8.2. Each imports `membership.admitted`
#: or the `admission.Admission` that caches its answers, except where
#: `_SITE_IMPORTS` names the import that carries the site's decision.
_SITES = (
    "recall.py",
    "search.py",
    "handlers/search.py",
    "hook.py",
    "review.py",
    "handlers/write.py",
    "audit.py",
    "recall_review.py",
    "_response.py",
    "handlers/show.py",
)


#: A site whose `Admission` arrives through another module's resolver.
#: `hook.py`'s Stop ranks its probe and settles interrupted turns on the
#: caller's admission, which it takes from `recall.caller_admission` since
#: the probe and the settlement moved into `recall` (the recall/hook
#: import cycle, open-list item 16); that import is the site's evidence.
_SITE_IMPORTS: dict[str, frozenset[tuple[str, str]]] = {
    "hook.py": frozenset({("recall", "caller_admission")}),
}


def _modules() -> list[Path]:
    return sorted([*_SRC.glob("*.py"), *(_SRC / "handlers").glob("*.py")])


def _name(path: Path) -> str:
    return path.relative_to(_SRC).as_posix()


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Calls(ast.NodeVisitor):
    """Every call to an origin test, with the function it sits in."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[str | None, str, int]] = []

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        name = _called(node)
        if name in _ORIGIN_TESTS:
            outer = self.stack[0] if self.stack else None
            self.found.append((outer, name, node.lineno))
        self.generic_visit(node)


def test_no_module_decides_membership_by_origin() -> None:
    """Design 8.2: no module but those the allow-lists name calls rc3's
    origin tests; each entry carries its reason."""
    offenders: list[str] = []
    for path in _modules():
        module = _name(path)
        if module in _MODULES:
            continue
        visitor = _Calls()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for function, called, line in visitor.found:
            if _rc3_function(function):
                continue
            offenders.append(f"{module}:{line} {function or '<module>'} calls {called}")
    assert not offenders, "\n".join(offenders)


def test_allow_lists_name_real_places_with_reasons() -> None:
    """The allow-list is origin.py and health.py, each existing and with
    its reason (decision B23: the verification path compares no
    remotes)."""
    names = {_name(path) for path in _modules()}
    assert set(_MODULES) == {"origin.py", "health.py"}
    for module, reason in _MODULES.items():
        assert module in names, module
        assert reason.strip(), module


def _imports(module: str) -> set[tuple[str, str]]:
    """``(module the import names, name)`` for every ``from`` import of
    `module`, at any depth (a local or a TYPE_CHECKING import counts)."""
    tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out.add((node.module.rsplit(".", 1)[-1], alias.name))
    return out


@pytest.mark.parametrize("module", _SITES)
def test_every_membership_site_decides_through_admitted(module: str) -> None:
    """Each site of design 8.2 imports `membership.admitted` or the
    `admission.Admission` that caches its answers, or the resolver
    `_SITE_IMPORTS` names for it."""
    found = _imports(module)
    accepted = {("membership", "admitted"), ("admission", "Admission")}
    accepted |= _SITE_IMPORTS.get(module, frozenset())
    assert found & accepted, (
        f"{module} decides membership through none of {sorted(accepted)}"
    )


def test_the_handoff_decides_by_checkout_through_membership() -> None:
    """Design 3.6: which episodes are this checkout's is
    `Membership.handoff`'s decision; the handoff keeps no worktree test."""
    source = (_SRC / "handlers" / "episode_handoff.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {_called(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "handoff" in calls
    assert "_worktrees_equal_strict" not in source
    assert "worktree_root" not in source


def test_the_store_readers_take_the_home() -> None:
    """The store's admission readers join the ``homes`` table and never
    read the origin columns to decide."""
    tree = ast.parse((_SRC / "store.py").read_text(encoding="utf-8"))
    readers = {
        "brief_rows",
        "scope_counts",
        "category_rows",
        "document_frequencies",
        "_read_doc_freq_inputs",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in readers:
            seen.add(node.name)
            text = ast.unparse(node)
            assert "origin_repo" not in text, node.name
            assert "origin_worktree" not in text, node.name
    assert seen == readers


def _callers(name: str) -> set[str]:
    """The modules that call `name` (a function or a method), anywhere."""
    out: set[str] = set()
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call) and _called(node) == name
            for node in ast.walk(tree)
        ):
            out.add(_name(path))
    return out


def test_a_global_records_named_projects_are_read_in_one_place() -> None:
    """RS-F7 part 2: which projects a global record's scopes name is
    `Membership.named`'s answer, read by the homes reader and the
    request's admission alone."""
    assert _callers("named") == {"membership.py", "admission.py"}


def test_what_is_in_force_is_decided_in_one_place() -> None:
    """RS-F7 part 3: the walk over inbound ``supersedes`` and ``extends``
    links is `in_force`'s, and the surfaces that deliver (the brief and
    the prompt recall, in recall.py) and the standing set (standing.py,
    part 1: a superseded record stands no more) call it."""
    assert _callers("inbound_links") == {"in_force.py"}
    assert _callers("in_force") == {"recall.py", "standing.py"}
    assert ("in_force", "in_force") in _imports("recall.py")
