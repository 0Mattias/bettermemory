"""bettermemory: persistent memory for Claude Code, retrieved on demand.

A local file-backed MCP server. Memory is opt-in retrieval, not forced
context. See the module-level docs and `prompts.SYSTEM_PROMPT_ADDENDUM`
for the consumer-side instructions.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .builder import build_server
from .cli import main
from .prompts import SYSTEM_PROMPT_ADDENDUM

try:
    # Single source of truth: pyproject.toml. Anything else drifts.
    __version__ = _pkg_version("bettermemory")
except PackageNotFoundError:
    # Running from a source tree without an install (e.g. `python -m
    # bettermemory` against a clone with no `pip install -e .`). Rare,
    # but the fallback keeps imports working instead of raising at
    # module import time.
    __version__ = "0+unknown"

__all__ = ["SYSTEM_PROMPT_ADDENDUM", "build_server", "main", "__version__"]
