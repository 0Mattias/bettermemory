"""Per-tool MCP handler implementations.

One module per served tool, or per action of the two dispatch tools
(`episode`, `memory_admin`). Each module owns its description constant
and the handler function; shared bookkeeping (`_advance_turn`, payload
validation, the auto-`record_use` token scan) lives in `_shared.py`.

The MCP-facing surface, the `ToolHandlers` class, lives in
`_handlers.py` as a thin facade: it captures the dependency references
(`config`, `store`, `sessions`, `recorder`, `responses`) once per server
and exposes one bound method per served tool that delegates straight to
the module function here. `builder._register_tools` imports the `DESC_*`
constants from this package so the wiring layer stays a short index.
"""

from __future__ import annotations

from .admin import DESC_MEMORY_ADMIN, memory_admin
from .episode import DESC_EPISODE, episode
from .record_use import DESC_MEMORY_RECORD_USE, memory_record_use
from .remove import DESC_MEMORY_REMOVE, memory_remove
from .search import DESC_MEMORY_SEARCH, memory_search
from .show import DESC_MEMORY_SHOW, memory_show
from .update import DESC_MEMORY_UPDATE, memory_update
from .verify import DESC_MEMORY_VERIFY, memory_verify
from .write import DESC_MEMORY_WRITE, memory_write

__all__ = [
    "DESC_EPISODE",
    "DESC_MEMORY_ADMIN",
    "DESC_MEMORY_RECORD_USE",
    "DESC_MEMORY_REMOVE",
    "DESC_MEMORY_SEARCH",
    "DESC_MEMORY_SHOW",
    "DESC_MEMORY_UPDATE",
    "DESC_MEMORY_VERIFY",
    "DESC_MEMORY_WRITE",
    "episode",
    "memory_admin",
    "memory_record_use",
    "memory_remove",
    "memory_search",
    "memory_show",
    "memory_update",
    "memory_verify",
    "memory_write",
]
