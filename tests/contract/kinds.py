"""Every log kind the source tree can write, read from the code.

A row reaches the log through `Store._append`, which these functions call
with the kind as one positional argument: `Store._mutate` (record and
membership mutations), `store_events.record_event` and `Recorder.record`
(telemetry and state rows), `record_membership`, `record_capture`,
`append_history_replaced`, `key_lifecycle._append_row` and
`membership.Membership._event`. `call_sites` reads each call whose kind
is a string literal, a module-level string constant, or a conditional of
them, with the payload keys the call names (keyword arguments, or the
keys of a literal dict). A call that forwards a kind it was given is
covered by the table that bounds what it forwards (`TABLE_KINDS`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from bettermemory import log as blog
from bettermemory.events import AUDIT_KINDS, ROW_KINDS
from bettermemory.store_schema import MEMBERSHIP_EVENT_KINDS, MUTATION_KINDS

SRC = Path(__file__).resolve().parents[2] / "src" / "bettermemory"

#: Call name -> position of its kind argument.
SITES = {
    "_mutate": 1,
    "_append": 1,
    "record_membership": 0,
    "record_capture": 0,
    "record": 0,
    "record_event": 0,
    "append_history_replaced": 1,
    "_append_row": 1,
    "_event": 0,
}

#: Payload positions for the calls whose payload is a positional dict.
PAYLOAD_POS = {
    "_mutate": 2,
    "_append": 2,
    "record_membership": 1,
    "record_capture": 1,
    "_append_row": 2,
    "_event": 1,
    "append_history_replaced": 2,
}


def table_kinds() -> frozenset[str]:
    """The kinds the forwarding writers are bounded by: mutations (the
    membership tables' ``f"{spec.kind}_put"`` among them), control rows,
    membership rows and every recorded telemetry kind."""
    telemetry: set[str] = set(AUDIT_KINDS)
    for group in ROW_KINDS.values():
        telemetry |= group
    return frozenset(
        set(MUTATION_KINDS) | set(blog.CONTROL_KINDS) | set(MEMBERSHIP_EVENT_KINDS)
    ) | frozenset(telemetry)


@dataclass
class Site:
    kind: str
    path: str
    line: int
    call: str
    #: Payload key -> the source of its value; None when the payload is
    #: not spelled at the call.
    keys: dict[str, str] | None = field(default=None)


def _constants() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                        out.setdefault(target.id, set()).add(value.value)
    return out


def _kinds_of(node: ast.expr, constants: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        a = _kinds_of(node.body, constants)
        b = _kinds_of(node.orelse, constants)
        return None if a is None or b is None else a | b
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    if isinstance(name, str) and name in constants:
        return set(constants[name])
    return None


def _dict_keys(node: ast.expr) -> dict[str, str] | None:
    if isinstance(node, ast.Dict) and all(
        isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys
    ):
        return {
            str(k.value): ast.unparse(v)  # type: ignore[union-attr]
            for k, v in zip(node.keys, node.values, strict=True)
        }
    return None


def _callee(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name if isinstance(name, str) else None


def _local_dicts(
    func: ast.AST, builders: dict[str, dict[str, str]] | None = None
) -> dict[str, dict[str, str]]:
    """The keys a function gives each local name it binds to a dict, in
    source order: a literal dict or the result of a builder (`_builders`),
    then ``name["k"] = ...`` and ``name.update(...)`` of either."""
    known = builders or {}

    def keys_of(value: ast.expr) -> dict[str, str] | None:
        keys = _dict_keys(value)
        if keys is None:
            callee = _callee(value)
            if callee == "dict" and isinstance(value, ast.Call) and value.args:
                return keys_of(value.args[0])
            if callee in known:
                keys = dict(known[callee])
        return keys

    out: dict[str, dict[str, str]] = {}
    nodes = sorted(
        (
            n
            for n in ast.walk(func)
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.Expr))
        ),
        key=lambda n: (n.lineno, n.col_offset),
    )
    for node in nodes:
        if isinstance(node, ast.Expr):
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "update"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in out
            ):
                for arg in call.args:
                    keys = keys_of(arg)
                    if keys is not None:
                        out[call.func.value.id].update(keys)
                for kw in call.keywords:
                    if kw.arg is not None:
                        out[call.func.value.id][kw.arg] = ast.unparse(kw.value)
            continue
        target: ast.expr | None
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target, value = node.targets[0], node.value
        else:
            target, value = node.target, node.value
        if value is None:
            continue
        if isinstance(target, ast.Name):
            keys = keys_of(value)
            if keys is not None:
                out[target.id] = keys
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
            and target.value.id in out
        ):
            out[target.value.id][target.slice.value] = ast.unparse(value)
    return out


@lru_cache(maxsize=1)
def _builders() -> dict[str, dict[str, str]]:
    """Functions of the tree that return a dict whose keys the code spells
    (a literal, or a local name bound to one): their name and the keys.
    Two passes, so a builder that starts from another builder's dict is
    read too."""
    out: dict[str, dict[str, str]] = {}
    trees = [
        ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SRC.rglob("*.py"))
    ]
    for _ in range(2):
        found: dict[str, dict[str, str]] = {}
        for tree in trees:
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                local = _local_dicts(func, out)
                for node in ast.walk(func):
                    if isinstance(node, ast.Return) and node.value is not None:
                        keys = _dict_keys(node.value)
                        if keys is None and isinstance(node.value, ast.Name):
                            keys = local.get(node.value.id)
                        if keys is not None:
                            found.setdefault(func.name, {}).update(keys)
        out = found
    return out


def _payload_keys(
    name: str,
    node: ast.Call,
    local: dict[str, dict[str, str]],
    builders: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    if name in ("record", "record_event", "_event"):
        keys: dict[str, str] = {}
        for k in node.keywords:
            if k.arg is None:
                spread = k.value
                callee = spread.func if isinstance(spread, ast.Call) else None
                target = (
                    callee.attr
                    if isinstance(callee, ast.Attribute)
                    else getattr(callee, "id", None)
                )
                if isinstance(target, str) and target in builders:
                    keys.update(builders[target])
                elif isinstance(spread, ast.Name) and spread.id in local:
                    keys.update(local[spread.id])
            elif k.arg not in ("session", "actor", "best_effort", "attempts"):
                keys[k.arg] = ast.unparse(k.value)
        return keys
    if name not in PAYLOAD_POS or len(node.args) <= PAYLOAD_POS[name]:
        return None
    payload = node.args[PAYLOAD_POS[name]]
    if isinstance(payload, ast.Call) and getattr(payload.func, "id", "") == "dict":
        payload = payload.args[0] if payload.args else payload
    if isinstance(payload, ast.Name):
        return local.get(payload.id)
    return _dict_keys(payload)


@lru_cache(maxsize=1)
def call_sites() -> tuple[list[Site], list[str]]:
    """The sites with a kind read from the code, and the sites that
    forward a kind (as ``path:line call(expr)``). A payload passed as a
    local name is read from the literal dict the enclosing function binds
    it to."""
    constants = _constants()
    builders = _builders()
    sites: dict[tuple[str, int, str], Site] = {}
    forwarded: dict[str, None] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes: list[ast.AST] = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        scopes.append(tree)
        for scope in scopes:
            local = _local_dicts(scope, builders) if scope is not tree else {}
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                if name not in SITES or len(node.args) <= SITES[name]:
                    continue
                arg = node.args[SITES[name]]
                kinds = _kinds_of(arg, constants)
                if kinds is None:
                    forwarded[f"{rel}:{node.lineno} {name}({ast.unparse(arg)})"] = None
                    continue
                keys = _payload_keys(name, node, local, builders)
                for kind in sorted(kinds):
                    found = sites.get((rel, node.lineno, kind))
                    if found is None or (found.keys is None and keys is not None):
                        sites[(rel, node.lineno, kind)] = Site(
                            kind, rel, node.lineno, name, keys
                        )
    return sorted(sites.values(), key=lambda s: (s.kind, s.path, s.line)), list(
        forwarded
    )


def writable_kinds() -> frozenset[str]:
    """Every kind the tree can write: the call sites' and the tables'."""
    sites, _ = call_sites()
    return frozenset(s.kind for s in sites) | table_kinds()
