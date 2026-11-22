"""Fixtures: ``ntpath.realpath`` (non-strict) and ``ntpath.abspath`` as
Windows runs them, and `view_path` over them (``ntrealpath_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/fsdata/realpath.rs`` (`nt_realpath`,
`nt_abspath`) and ``native/crates/bettermemory-core/src/location_probe.rs``
(`view_path_windows`).

``ntpath`` is loaded from its own source under another name with a fake
``nt`` module, so its Windows branches run on any host: the fake answers
``_getfinalpathname``, ``readlink``, ``_findfirstfile`` and
``_getfullpathname`` from the case's table, ``islink`` from the case's list
of links, and ``os.getcwd`` from the case. The fake ``nt`` has none of the
C helpers (``_path_normpath``, ``_path_splitroot_ex``, ``_path_islink``),
so the module's Python spellings run, as the port reproduces them. Every
path is ASCII, where ``normcase`` (``LCMapStringEx`` on a Windows host,
``str.lower`` elsewhere) answers the same.

A case's table ``t``: ``f`` (``_getfinalpathname``), ``r`` (``readlink``),
``ff`` (``_findfirstfile``) and ``p`` (``_getfullpathname``), each a map
from the exact path asked to its answer: a string, an integer (an OSError
with that ``winerror``) or null (ValueError). A path not in a map raises
winerror 2 (``f``, ``ff``), 4390 (``r``) or 123 (``p``). ``l`` lists the
links ``islink`` admits; ``c`` is ``os.getcwd()``'s answer (a string) or
an integer (the winerror of an OSError it raises).

Every text in the file spells a backslash as ``|`` (no case holds one),
so the long prefix and UNC paths do not read as host paths to the
golden directory's guard (`gen_store_files.host_fact`).

Each line: ``q`` the path, then ``real``, ``abs`` and ``view``: the
answer, or ``{"raise": class, "w": winerror}`` (``w`` only for an
OSError).

The file is gated on the Python release: ``ntpath`` changed its walk in
each of 3.11, 3.12 and 3.13, and the port is 3.13.13's.
"""

from __future__ import annotations

import importlib.util
import ntpath
import os
import sys
import types
from typing import Any

from ._common import jsonl

_T: dict[str, Any] = {}

_DEFAULT = {"f": 2, "r": 4390, "ff": 2, "p": 123}


class _WinError(OSError):
    def __init__(self, code: int) -> None:
        super().__init__(f"winerror {code}")
        self.winerror = code


def _look(key: str, path: str) -> str:
    value = _T.get(key, {}).get(path, _DEFAULT[key])
    if value is None:
        raise ValueError("embedded null character")
    if isinstance(value, int):
        raise _WinError(value)
    return str(value)


def _cwd() -> str:
    value = _T.get("c", "C:\\w")
    if isinstance(value, int):
        raise _WinError(value)
    return str(value)


class _Os:
    """``os`` with the case's working directory."""

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    @staticmethod
    def getcwd() -> str:
        return _cwd()


def _load() -> Any:
    fake = types.ModuleType("nt")
    fake._getfinalpathname = lambda p: _look("f", p)  # type: ignore[attr-defined]
    fake.readlink = lambda p: _look("r", p)  # type: ignore[attr-defined]
    fake._findfirstfile = lambda p: _look("ff", p)  # type: ignore[attr-defined]
    fake._getfullpathname = lambda p: _look("p", p)  # type: ignore[attr-defined]
    held = sys.modules.get("nt")
    sys.modules["nt"] = fake
    try:
        spec = importlib.util.spec_from_file_location(
            "_ntpath_reference", ntpath.__file__
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if held is None:
            del sys.modules["nt"]
        else:
            sys.modules["nt"] = held
    setattr(mod, "os", _Os())  # noqa: B010 - the module's own global
    setattr(mod, "islink", lambda p: p in _T.get("l", []))  # noqa: B010
    return mod


def _answer(call: Any) -> Any:
    try:
        return call()
    except OSError as exc:
        return {"raise": "OSError", "w": getattr(exc, "winerror", None)}
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raise": type(exc).__name__}


P = "\\\\?\\"
# fmt: off
_CASES: list[tuple[dict[str, Any], str]] = [
    # Resolved at once; the prefix stays unless the plain path resolves the same.
    ({"f": {"C:\\a\\b": P + "C:\\A\\B"}}, "C:\\a\\b"),
    ({"f": {"C:\\a\\b": P + "C:\\A\\B", "C:\\A\\B": P + "C:\\A\\B"}}, "C:\\a\\b"),
    ({"f": {"C:\\a": P + "C:\\A", "C:\\A": P + "C:\\B"}}, "C:\\a"),
    ({"f": {"C:\\a": P + "C:\\A", "C:\\A": None}}, "C:\\a"),
    ({"f": {"C:\\a": P + "C:\\A", "C:\\A": 5}}, "C:\\a"),
    ({"f": {"C:\\a": "C:\\plain"}}, "C:\\a"),
    # Relative, forward slashes, dots, the empty path.
    ({"f": {"C:\\w\\x": P + "C:\\w\\x", "C:\\w\\x\\y": P + "C:\\w\\x\\y"}}, "x/./y"),
    ({"f": {"C:\\w": P + "C:\\W", "C:\\W": P + "C:\\W"}}, ""),
    ({"c": "D:\\cur", "f": {"D:\\cur\\..\\q": P + "D:\\q"}}, "..\\q"),
    ({"f": {"C:\\q": P + "C:\\q", "C:\\q\\r": P + "C:\\q\\r"}}, "C:/q/r/"),
    # NUL in every spelling normcase folds to `nul`.
    ({}, "nul"), ({}, "NuL"), ({}, "C:\\nul"), ({}, "nul\\"),
    # A missing tail: the deepest prefix that resolves, then the rest joined.
    ({"f": {"C:\\a": P + "C:\\A"}}, "C:\\a\\b\\c"),
    ({"f": {"C:\\a": P + "C:\\A"}}, "C:\\a\\b\\..\\c"),
    ({}, "C:\\gone\\deeper"),
    ({"f": {"C:\\": 21}}, "C:\\"),
    ({"f": {"E:\\x": 21, "E:\\": 21}}, "E:\\x"),
    ({"f": {"C:\\a\\b": 3, "C:\\a": P + "C:\\A"}}, "C:\\a\\b"),
    # The OS cannot follow a link (1921): readlink is followed by hand.
    ({"f": {"C:\\l": 1921}, "r": {"C:\\l": "C:\\target"}}, "C:\\l"),
    ({"f": {"C:\\l\\t": 1921}, "r": {"C:\\l\\t": "C:\\target"}}, "C:\\l\\t"),
    ({"f": {"C:\\d\\l": 1921}, "r": {"C:\\d\\l": "rel\\x"}, "l": ["C:\\d\\l"]}, "C:\\d\\l"),
    ({"f": {"C:\\d\\l": 1921}, "r": {"C:\\d\\l": "rel\\x"}}, "C:\\d\\l"),
    ({"f": {"C:\\d\\l": 1921}, "r": {"C:\\d\\l": "..\\..\\..\\up"}, "l": ["C:\\d\\l"]}, "C:\\d\\l"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": "C:\\b", "C:\\b": "C:\\A"}}, "C:\\a"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": "C:\\b", "C:\\b": "C:\\c"}}, "C:\\a"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": None}}, "C:\\a"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": 1450}}, "C:\\a"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": "C:\\b", "C:\\b": 1450}}, "C:\\a"),
    ({"f": {"C:\\a": 1921}, "r": {"C:\\a": "C:\\b", "C:\\b": 5}}, "C:\\a"),
    ({"f": {"C:\\a\\b": 1921, "C:\\a": P + "C:\\A"}, "r": {"C:\\a\\b": "C:\\a\\b"}}, "C:\\a\\b"),
    # A ValueError mid-chain ends the walk on the path reached so far.
    ({"f": {"C:\\A": 2}, "r": {"C:\\A": "C:\\B", "C:\\B": None}}, "C:\\A"),
    # The real name without access (1, 5, 32, 50, 87, 1920, 1921).
    ({"f": {"C:\\s\\x": 5, "C:\\s": P + "C:\\S"}, "ff": {"C:\\s\\x": "X-Real"}}, "C:\\s\\x"),
    ({"f": {"C:\\s\\x": 32, "C:\\s": P + "C:\\S"}}, "C:\\s\\x"),
    ({"f": {"C:\\s\\x": 87, "C:\\s": P + "C:\\S"}, "ff": {"C:\\s\\x": None}}, "C:\\s\\x"),
    ({"f": {"C:\\s\\x": 53, "C:\\s": P + "C:\\S"}, "ff": {"C:\\s\\x": "never"}}, "C:\\s\\x"),
    # An error outside the allowed codes rises.
    ({"f": {"C:\\a": 1450}}, "C:\\a"),
    ({"f": {"C:\\a\\b": 2, "C:\\a": 1450}}, "C:\\a\\b"),
    ({"f": {"C:\\a\\b": 2, "C:\\a": None}}, "C:\\a\\b"),
    ({"c": 2}, "C:\\abs"),
    ({"c": 2}, "rel"),
    # A ValueError on the first call under a working directory not normal.
    ({"c": "C:\\x\\..\\w", "f": {"C:\\x\\..\\w\\a": None}}, "a"),
    # UNC and the long prefix.
    ({"f": {"\\\\srv\\sh\\x": P + "UNC\\srv\\sh\\x"}}, "\\\\srv\\sh\\x"),
    ({"f": {"\\\\srv\\sh\\x": 67, "\\\\srv\\sh\\": 67}}, "\\\\srv\\sh\\x"),
    ({"f": {"\\\\srv\\sh\\x\\y": 53, "\\\\srv\\sh\\x": P + "UNC\\srv\\sh\\X"}}, "\\\\srv\\sh\\x\\y"),
    ({"f": {P + "C:\\a": P + "C:\\A"}}, P + "C:\\a"),
    ({"f": {P + "C:\\a\\b": 2, P + "C:\\a": P + "C:\\A"}}, P + "C:\\a\\b"),
    ({"f": {"\\\\.\\pipe\\p": 2}}, "\\\\.\\pipe\\p"),
    # A NUL: the path as made absolute; under a prefixed cwd, the
    # winerror of the plain spelling meets an unset first one.
    ({"f": {"C:\\a\0b": None}}, "C:\\a\0b"),
    ({"c": P + "C:\\w", "f": {P + "C:\\w\\x\0": None}}, "x\0"),
    ({"c": P + "C:\\w", "f": {P + "C:\\w\\x\0": None, "C:\\w\\x\0": None}}, "x\0"),
    # abspath's fallbacks.
    ({"p": {"C:\\a": "C:\\full"}}, "C:\\a"),
    ({"p": {"C:": "C:\\dir"}}, "C:x\\y"),
    ({}, "C:x\\y"),
    ({"p": {"\\": "D:\\"}}, "\\x"),
    ({}, "\\x"),
    ({"p": {"rel": None}}, "rel"),
    ({"c": 5, "p": {"C:\\a": "C:\\x\\..\\b"}}, "C:\\a"),
    # view_path's separators and drives.
    ({}, "C:\\"), ({}, "C:"), ({}, "C:\\x\\\\"), ({}, "\\\\srv\\sh\\"), ({}, "/"),
    ({"f": {"C:\\v": P + "C:\\v\\"}}, "C:\\v"),
    ({"f": {"C:\\v": "D:"}}, "C:\\v"),
    ({"f": {"C:\\v": "///"}}, "C:\\v"),
    ({"f": {"C:\\": "\\"}}, "/"),
]
# fmt: on


def _lines() -> list[dict[str, Any]]:
    from bettermemory import _location_probe

    mod = _load()
    held = _location_probe._VIEW_PATHS
    _location_probe._VIEW_PATHS = mod
    out: list[dict[str, Any]] = []
    try:
        for table, query in _CASES:
            _T.clear()
            _T.update(table)
            out.append(
                {
                    "t": table,
                    "q": query,
                    "real": _answer(lambda q=query: mod.realpath(q)),
                    "abs": _answer(lambda q=query: mod.abspath(q)),
                    "view": _answer(lambda q=query: _location_probe.view_path(q)),
                }
            )
    finally:
        _location_probe._VIEW_PATHS = held
        _T.clear()
    return out


def _spelled(value: Any) -> Any:
    if isinstance(value, str):
        assert "|" not in value
        return value.replace("\\", "|")
    if isinstance(value, dict):
        return {_spelled(k): _spelled(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_spelled(v) for v in value]
    return value


def generate_python() -> dict[str, bytes]:
    lines = [_spelled(line) for line in _lines()]
    header = {"fixture": "ntrealpath_python", "cases": len(lines)}
    return {"ntrealpath_python.jsonl": jsonl([header, *lines])}
