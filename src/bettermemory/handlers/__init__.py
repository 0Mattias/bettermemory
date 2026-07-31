"""Per-tool MCP handler implementations.

Pre-Round-2 every handler was a method on a 1700-line `ToolHandlers`
god class in `_handlers.py`. Round 2 split that into one module per
tool (or one module per symmetric pair, in the scope_toggle case).
Each module owns its description constant + the handler function
itself. Shared bookkeeping (`_advance_turn`, payload validation, the
auto-`record_use` token scan) lives in `_shared.py`.

The MCP-facing surface (the `ToolHandlers` class) still lives in
`_handlers.py` as a thin facade: it captures the dependency
references (`config`, `store`, `sessions`, `recorder`, `responses`,
`_semantic_model_factory`) once per server and exposes one bound
method per tool that delegates straight to the per-tool module. That
shape is what keeps the test suite's `mcp._tool_manager.get_tool(name).fn`
patterns working — `fn` is still a bound method, and the SDK's
introspection still strips `self` from the JSON schema.

`server._register_tools` imports the `DESC_*` constants from this
package's `__init__` so the wiring layer can stay a short index.
"""

from __future__ import annotations

from .acknowledge_miss import DESC_MEMORY_ACKNOWLEDGE_MISS, memory_acknowledge_miss
from .audit_turn import DESC_MEMORY_AUDIT_TURN, memory_audit_turn
from .conflicts import DESC_MEMORY_CONFLICTS, memory_conflicts
from .curate import DESC_MEMORY_CURATE, memory_curate
from .episode_handoff import DESC_EPISODE_HANDOFF, episode_handoff
from .episode_patterns import DESC_EPISODE_PATTERNS, episode_patterns
from .episode_promote import DESC_EPISODE_PROMOTE, episode_promote
from .episode_search import DESC_EPISODE_SEARCH, episode_search
from .episode_write import DESC_EPISODE_WRITE, episode_write
from .health import DESC_MEMORY_HEALTH, memory_health
from .list_active import DESC_MEMORY_LIST, memory_list
from .proposals import DESC_MEMORY_PROPOSALS, memory_proposals
from .record_use import DESC_MEMORY_RECORD_USE, memory_record_use
from .remove import DESC_MEMORY_REMOVE, memory_remove
from .rename_scope import DESC_MEMORY_RENAME_SCOPE, memory_rename_scope
from .restore import DESC_MEMORY_RESTORE, memory_restore
from .scope_overview import DESC_MEMORY_SCOPE_OVERVIEW, memory_scope_overview
from .scope_toggle import (
    DESC_MEMORY_SCOPE_DISABLE,
    DESC_MEMORY_SCOPE_ENABLE,
    memory_scope_disable,
    memory_scope_enable,
)
from .search import DESC_MEMORY_SEARCH, memory_search
from .show import DESC_MEMORY_SHOW, memory_show
from .tombstones import DESC_MEMORY_LIST_TOMBSTONES, memory_list_tombstones
from .update import DESC_MEMORY_LINKS_TAIL, DESC_MEMORY_UPDATE, memory_update
from .verify import DESC_MEMORY_VERIFY, memory_verify
from .write import (
    DESC_MEMORY_WRITE,
    DESC_MEMORY_WRITE_CANCEL,
    DESC_MEMORY_WRITE_CONFIRM,
    memory_write,
    memory_write_cancel,
    memory_write_confirm,
)

__all__ = [
    "DESC_EPISODE_HANDOFF",
    "DESC_EPISODE_PATTERNS",
    "DESC_EPISODE_PROMOTE",
    "DESC_EPISODE_SEARCH",
    "DESC_EPISODE_WRITE",
    "DESC_MEMORY_ACKNOWLEDGE_MISS",
    "DESC_MEMORY_AUDIT_TURN",
    "DESC_MEMORY_CONFLICTS",
    "DESC_MEMORY_CURATE",
    "DESC_MEMORY_HEALTH",
    "DESC_MEMORY_LINKS_TAIL",
    "DESC_MEMORY_LIST",
    "DESC_MEMORY_LIST_TOMBSTONES",
    "DESC_MEMORY_PROPOSALS",
    "DESC_MEMORY_RECORD_USE",
    "DESC_MEMORY_REMOVE",
    "DESC_MEMORY_RENAME_SCOPE",
    "DESC_MEMORY_RESTORE",
    "DESC_MEMORY_SCOPE_DISABLE",
    "DESC_MEMORY_SCOPE_ENABLE",
    "DESC_MEMORY_SCOPE_OVERVIEW",
    "DESC_MEMORY_SEARCH",
    "DESC_MEMORY_SHOW",
    "DESC_MEMORY_UPDATE",
    "DESC_MEMORY_VERIFY",
    "DESC_MEMORY_WRITE",
    "DESC_MEMORY_WRITE_CANCEL",
    "DESC_MEMORY_WRITE_CONFIRM",
    "episode_handoff",
    "episode_patterns",
    "episode_promote",
    "episode_search",
    "episode_write",
    "memory_acknowledge_miss",
    "memory_audit_turn",
    "memory_conflicts",
    "memory_curate",
    "memory_health",
    "memory_list",
    "memory_list_tombstones",
    "memory_proposals",
    "memory_record_use",
    "memory_remove",
    "memory_rename_scope",
    "memory_restore",
    "memory_scope_disable",
    "memory_scope_enable",
    "memory_scope_overview",
    "memory_search",
    "memory_show",
    "memory_update",
    "memory_verify",
    "memory_write",
    "memory_write_cancel",
    "memory_write_confirm",
]
