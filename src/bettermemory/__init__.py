"""bettermemory: a trust layer between a coding agent and its own past.

A local file-backed MCP server where a stored fact is a claim that
keeps earning belief — per-hit staleness verdicts, gated writes,
claim-level use attribution, measured effectiveness, curation, and an
episode tier for session run-state. Memory is opt-in retrieval, not
forced context. See the module-level docs and
`prompts.SYSTEM_PROMPT_ADDENDUM` for the consumer-side instructions.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

try:
    # Single source of truth: pyproject.toml. Anything else drifts.
    __version__ = _pkg_version("bettermemory")
except PackageNotFoundError:
    # Running from a source tree without an install (e.g. `python -m
    # bettermemory` against a clone with no `pip install -e .`). Rare,
    # but the fallback keeps imports working instead of raising at
    # module import time.
    __version__ = "0+unknown"


def __getattr__(name: str) -> Any:
    """`build_server`, `main` and `SYSTEM_PROMPT_ADDENDUM` on demand.

    Importing the package used to import `builder`, and with it the MCP
    SDK, on every entry: `import mcp` measures 316 ms against 11 ms for
    the interpreter on the author's machine. The hook commands
    (`_daemon_client`) and the CLI's own startup must not pay that, so
    the three names resolve when first read; `from bettermemory import
    build_server` still works.
    """
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
