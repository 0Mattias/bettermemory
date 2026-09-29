"""bettermemory: a trust layer between a coding agent and its own past.

A local file-backed MCP server where a stored fact is a claim that
keeps earning belief — per-hit staleness verdicts, gated writes,
claim-level use attribution, measured effectiveness, curation, and an
episode tier for session run-state. Memory is opt-in retrieval, not
forced context. See the module-level docs and
`prompts.SYSTEM_PROMPT_ADDENDUM` for the consumer-side instructions.
"""

from typing import Any

# Bound on first read by `__getattr__` below, which then stores it here.
__version__: str


def _installed_version() -> str:
    """The version this package was built as.

    Single source of truth: pyproject.toml. Hatchling's version build hook
    writes it into `_version.py` at every build and editable install
    (`[tool.hatch.build.hooks.version]`), and reading that file costs one
    small import where `importlib.metadata` was the largest import a hook
    process paid before 9.0.0. Where the file is absent, a source tree no
    build has touched, the installed metadata answers as it did before, and
    without an install at all (`python -m bettermemory` against a clone
    with no `pip install -e .`) the version reads "0+unknown" rather than
    failing the import."""
    try:
        from ._version import __version__ as built
    except ImportError:
        pass
    else:
        return built
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bettermemory")
    except PackageNotFoundError:
        return "0+unknown"


def __getattr__(name: str) -> Any:
    """`__version__`, `build_server`, `main` and `SYSTEM_PROMPT_ADDENDUM`
    on demand.

    Importing the package used to import `builder`, and with it the MCP
    SDK, on every entry: `import mcp` measures 316 ms against 11 ms for
    the interpreter on the author's machine. The hook commands
    (`_daemon_client`) and the CLI's own startup must not pay that, so
    the three names resolve when first read; `from bettermemory import
    build_server` still works. `__version__` is read once
    (`_installed_version`) and kept as a module attribute.
    """
    if name == "__version__":
        version = _installed_version()
        globals()["__version__"] = version
        return version
    if name == "build_server":
        from .builder import build_server

        return build_server
    if name == "main":
        from .cli import main

        return main
    if name == "SYSTEM_PROMPT_ADDENDUM":
        from .prompts import SYSTEM_PROMPT_ADDENDUM

        return SYSTEM_PROMPT_ADDENDUM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["SYSTEM_PROMPT_ADDENDUM", "build_server", "main", "__version__"]
