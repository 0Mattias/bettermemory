"""The reference's call sites of ``re``, found by an ``ast`` scan of
``src/bettermemory``, and the patterns its modules build at import, found
by importing them in a child interpreter with ``re``'s functions wrapped.

A site is named without a line number: the file, then the enclosing
functions and classes and the name the call is bound to, joined by ``.``,
and ``#2``, ``#3`` for the second and third call of one name. The scan
reads every call of ``re.<function>`` and of a name imported from ``re``.

A site's ``methods`` are the calls it is used through, each written as
the method, the count of positional arguments and the keywords sorted
(``split/2``, ``search/1:pos``): the function itself for a call of
``re`` other than ``compile``; for ``compile``, every call of a pattern
method on the name it is bound to, inside the scope that binds it, and
for a module-level name also in every module that imports it by name.
A compiled pattern handed on any other way has no method read.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[2] / "src" / "bettermemory"

#: The functions of ``re`` that take a pattern, and where each takes its
#: flags (positional index after the pattern, counting the pattern as 0).
FLAG_INDEX = {
    "compile": 1,
    "search": 2,
    "match": 2,
    "fullmatch": 2,
    "findall": 2,
    "finditer": 2,
    "sub": 4,
    "subn": 4,
    "split": 3,
}

_FLAG_NAMES = {
    "I": 2,
    "IGNORECASE": 2,
    "L": 4,
    "LOCALE": 4,
    "M": 8,
    "MULTILINE": 8,
    "S": 16,
    "DOTALL": 16,
    "U": 32,
    "UNICODE": 32,
    "X": 64,
    "VERBOSE": 64,
    "A": 256,
    "ASCII": 256,
}


@dataclass
class Site:
    id: str
    file: str
    func: str
    first: int
    last: int
    literal: str | bytes | None
    flags: int | None
    instances: list[tuple[str | bytes, int]] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


def _flags(
    node: ast.expr | None, aliases: set[str], names: dict[str, str]
) -> int | None:
    """The value of a flags argument written from ``re``'s flag names, ``|``
    and integers; ``None`` for anything else."""
    if node is None:
        return 0
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id in aliases:
            return _FLAG_NAMES.get(node.attr)
        if node.value.id in aliases | {"RegexFlag"}:
            return _FLAG_NAMES.get(node.attr)
    if isinstance(node, ast.Name) and node.id in names:
        return _FLAG_NAMES.get(names[node.id])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _flags(node.left, aliases, names)
        right = _flags(node.right, aliases, names)
        if left is None or right is None:
            return None
        return left | right
    return None


#: The methods of a compiled pattern that run the engine.
PATTERN_METHODS = {
    "match",
    "fullmatch",
    "search",
    "findall",
    "finditer",
    "sub",
    "subn",
    "split",
}


def _signature(name: str, call: ast.Call, skip: int) -> str:
    """``name/<positional arguments after the first skip>[:keywords]``."""
    keys = sorted(kw.arg or "**" for kw in call.keywords)
    head = f"{name}/{max(len(call.args) - skip, 0)}"
    return head + (":" + ",".join(keys) if keys else "")


def _uses(scope: ast.AST, binding: str) -> list[str]:
    """The pattern methods called on ``binding`` inside ``scope``."""
    found = set()
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PATTERN_METHODS
            and ast.unparse(node.func.value) == binding
        ):
            found.add(_signature(node.func.attr, node, 0))
    return sorted(found)


def _scan_file(path: Path) -> list[Site]:
    rel = path.relative_to(SOURCE).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "re"}
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            names |= {a.asname or a.name: a.name for a in node.names}
    found: list[Site] = []
    counts: dict[str, int] = {}

    def visit(
        node: ast.AST, scope: list[str], binding: str | None, owner: ast.AST
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.iter_child_nodes(node):
                visit(child, [*scope, node.name], None, node)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            binding = " ".join(ast.unparse(t) for t in targets)
        if isinstance(node, ast.Call):
            func = node.func
            attr = None
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in aliases
            ):
                attr = func.attr
            elif isinstance(func, ast.Name) and func.id in names:
                attr = names[func.id]
            if attr in FLAG_INDEX and node.args:
                key = ".".join([*scope, binding or "-"])
                counts[key] = counts.get(key, 0) + 1
                site_id = f"{rel}:{key}"
                if counts[key] > 1:
                    site_id += f"#{counts[key]}"
                first = node.args[0]
                literal = (
                    first.value
                    if isinstance(first, ast.Constant)
                    and isinstance(first.value, (str, bytes))
                    else None
                )
                index = FLAG_INDEX[attr]
                arg = node.args[index - 0] if len(node.args) > index else None
                if arg is None:
                    for kw in node.keywords:
                        if kw.arg == "flags":
                            arg = kw.value
                site = Site(
                    site_id,
                    rel,
                    attr,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    literal,
                    _flags(arg, aliases, names),
                )
                if attr != "compile":
                    site.methods = [_signature(attr, node, 1)]
                elif binding is not None:
                    site.methods = _uses(owner, binding)
                    if not scope:
                        _MODULE_BINDINGS.append((site, binding))
                found.append(site)
        for child in ast.iter_child_nodes(node):
            visit(child, scope, binding, owner)

    visit(tree, [], None, tree)
    return found


#: The module-level compiled names of the files scanned, for `scan`.
_MODULE_BINDINGS: list[tuple[Site, str]] = []


def scan() -> list[Site]:
    sites: list[Site] = []
    _MODULE_BINDINGS.clear()
    paths = sorted(SOURCE.rglob("*.py"))
    for path in paths:
        sites += _scan_file(path)
    trees = {
        p.relative_to(SOURCE).as_posix(): ast.parse(p.read_text(encoding="utf-8"))
        for p in paths
    }
    for site, binding in _MODULE_BINDINGS:
        for rel, tree in trees.items():
            if rel == site.file:
                continue
            imported = {
                a.asname or a.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for a in node.names
                if a.name == binding
            }
            for name in imported:
                site.methods = sorted({*site.methods, *_uses(tree, name)})
    return sites


_CAPTURE = r"""
import importlib, json, re, sys
SRC = sys.argv[1]
# The scanned tree, not whichever bettermemory the interpreter has.
sys.path.insert(0, SRC.rstrip("/").rsplit("/", 1)[0])
FLAG_INDEX = json.loads(sys.argv[2])
modules = json.loads(sys.argv[3])
records = []
orig = {f: getattr(re, f) for f in FLAG_INDEX}
def wrap(name):
    o = orig[name]
    def w(pattern, *a, **k):
        fr = sys._getframe(1)
        fn = fr.f_code.co_filename.replace("\\", "/")
        if fn.startswith(SRC) and not isinstance(pattern, re.Pattern):
            i = FLAG_INDEX[name] - 1
            flags = a[i] if len(a) > i else k.get("flags", 0)
            p = pattern if isinstance(pattern, str) else pattern.decode("latin-1")
            records.append([fn[len(SRC):], fr.f_lineno, name, p,
                            isinstance(pattern, bytes), int(flags)])
        return o(pattern, *a, **k)
    return w
for f in FLAG_INDEX:
    setattr(re, f, wrap(f))
failed = []
for m in modules:
    try:
        importlib.import_module(m)
    except Exception as e:
        failed.append([m, type(e).__name__])
home = sys.modules["bettermemory"].__file__.replace("\\", "/")
assert home.startswith(SRC), "bettermemory imported from " + home + ", not " + SRC
json.dump({"records": records, "failed": failed}, sys.stdout)
"""


def capture(sites: list[Site]) -> list[str]:
    """Fills each site's instances from what the modules build at import
    and, for a literal not run at import, from its text and flags. Answers
    the modules that failed to import."""
    files = sorted({s.file for s in sites})
    modules = [
        "bettermemory."
        + f.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for f in files
    ]
    src = SOURCE.as_posix() + "/"
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _CAPTURE,
            src,
            json.dumps(FLAG_INDEX),
            json.dumps(modules),
        ],
        check=True,
        capture_output=True,
        env=dict(os.environ),
    )
    out = json.loads(done.stdout)
    by_file: dict[str, list[Site]] = {}
    for s in sites:
        by_file.setdefault(s.file, []).append(s)
    for rel, line, name, text, is_bytes, flags in out["records"]:
        owners = [
            s
            for s in by_file.get(rel, [])
            if s.first <= line <= s.last and s.func == name
        ]
        assert len(owners) == 1, (rel, name, len(owners))
        pattern: str | bytes = text.encode("latin-1") if is_bytes else text
        if (pattern, flags) not in owners[0].instances:
            owners[0].instances.append((pattern, flags))
    for s in sites:
        if not s.instances and s.literal is not None and s.flags is not None:
            s.instances.append((s.literal, s.flags))
    return [f"{m}: {e}" for m, e in out["failed"]]


def as_json(sites: list[Site]) -> Any:
    return [
        {
            "id": s.id,
            "n": len(s.instances),
            "literal": s.literal is not None,
            "methods": s.methods,
        }
        for s in sites
    ]
