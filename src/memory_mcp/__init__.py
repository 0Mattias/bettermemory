"""memory-mcp: a local file-backed memory MCP server with retrieval-on-demand.

Memory is opt-in retrieval, not forced context. See the module-level docs
and `prompts.SYSTEM_PROMPT_ADDENDUM` for the consumer-side instructions.
"""

from .prompts import SYSTEM_PROMPT_ADDENDUM
from .server import build_server, main

__version__ = "0.1.0"

__all__ = ["SYSTEM_PROMPT_ADDENDUM", "build_server", "main", "__version__"]
