"""Fixtures: `origin_model.Origin`'s private capture state
(``origin_model.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_model.rs``. One case: a fresh
`Origin`'s ``_repo_url_alternates``, ``git_indeterminate`` and
``git_refusal``, and its ``model_dump()``, which holds none of the three.
"""

from __future__ import annotations

from typing import Any

from bettermemory.origin_model import Origin

from ._common import jsonl


def generate() -> dict[str, bytes]:
    origin = Origin(cwd="/w", repo="u", branch="b", worktree_root="/w", source="env")
    case: dict[str, Any] = {
        "alternates": list(origin._repo_url_alternates),
        "indeterminate": origin.git_indeterminate,
        "refusal": origin.git_refusal,
        "dump": origin.model_dump(),
    }
    return {"origin_model.jsonl": jsonl([{"cases": 1}, case])}
