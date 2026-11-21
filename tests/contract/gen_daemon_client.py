"""Fixtures: the hook budget of `_daemon_client` (``daemon_client.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/daemon_client.rs``. The header pins
`STORE_FILENAME`, `START_TIMEOUT_SECONDS`, `HOOK_TIMEOUT_SECONDS`,
`HOOK_HARNESS_TIMEOUT_SECONDS` (in order) and `HOOK_MARGIN_SECONDS`; each
row is one event and its `hook_budget_seconds` (``repr``) and harness
timeout (null where the mapping has none). Not gated.
"""

from __future__ import annotations

from bettermemory import _daemon_client as dc

from ._common import jsonl, text_field

EVENTS = [
    "session-start",
    "stop",
    "prompt",
    "",
    "Stop",
    "audit-turn",
    "prompt-recall",
    "session-start ",
    "prömpt",
    "\ud800",
]


def generate() -> dict[str, bytes]:
    rows = [
        {
            **text_field("e", e),
            "budget": repr(dc.hook_budget_seconds(e)),
            "harness": (
                repr(dc.HOOK_HARNESS_TIMEOUT_SECONDS[e])
                if e in dc.HOOK_HARNESS_TIMEOUT_SECONDS
                else None
            ),
        }
        for e in EVENTS
    ]
    header = {
        "store": dc.STORE_FILENAME,
        "start": repr(dc.START_TIMEOUT_SECONDS),
        "hook": repr(dc.HOOK_TIMEOUT_SECONDS),
        "harness": [[k, repr(v)] for k, v in dc.HOOK_HARNESS_TIMEOUT_SECONDS.items()],
        "margin": repr(dc.HOOK_MARGIN_SECONDS),
        "cases": len(rows),
    }
    return {"daemon_client.jsonl": jsonl([header, *rows])}
