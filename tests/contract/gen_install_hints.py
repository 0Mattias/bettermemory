"""Fixtures: the install-command spellings of `_install_hints`
(``install_hints.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/install_hints.rs``. Each row is one
call: ``f`` the function, ``a`` its arguments, ``r`` the returned text.
Not gated.
"""

from __future__ import annotations

from bettermemory import _install_hints as ih

from ._common import jsonl

EXTRAS = ["ui", "", "dev,test", "it's", "été", "a]b[c", "\U0001f600", " x "]
MODULES = ["numpy", "", "torch==2.0", "möd", "a'b"]


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"f": "tool_reinstall_package", "a": [], "r": ih.tool_reinstall_package()}
    ]
    for name in ("extras_spec", "tool_reinstall", "pipx_force", "dev_clone_editable"):
        rows += [{"f": name, "a": [e], "r": getattr(ih, name)(e)} for e in EXTRAS]
    rows += [
        {"f": "install_extra_command", "a": [e], "r": ih.install_extra_command(e)}
        for e in EXTRAS
    ]
    rows += [
        {"f": "pip_force_reinstall", "a": [m], "r": ih.pip_force_reinstall(m)}
        for m in MODULES
    ]
    rows += [
        {
            "f": "reinstall_extra_command",
            "a": [m, e],
            "r": ih.reinstall_extra_command(m, e),
        }
        for m in MODULES
        for e in EXTRAS[:4]
    ]
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows()
    header = {"tool_reinstall": ih._TOOL_REINSTALL, "cases": len(rows)}
    return {"install_hints.jsonl": jsonl([header, *rows])}
