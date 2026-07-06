"""Regression: an explicit, path-shaped `prior_session_id` passed to
`episode_handoff` must degrade gracefully, not surface a raw ValueError.

`episode_handoff` is the documented FIRST call at a /loop iteration
entry, and `prior_session_id` is caller-supplied — a child agent may
pass its parent's id, which can be mistyped or path-shaped. Such an id
flows verbatim into `deps.episode_store.list_by_session(...)`, whose
`_session_dir` validator raises `ValueError` for anything outside the
`[A-Za-z0-9_-]` charset (slash / space / dot / "../"). Before the fix
that ValueError propagated out of the handler on the hot path; FastMCP
wraps it, so the caller saw a raw tool error rather than the graceful
`episodes: []` shape every other episode read surface returns.

The auto-resolution branch (episode_handoff.py ~lines 271-274) and the
sibling `episode_search` per-session lookup (episode_search.py
~126-131) both already catch this and return the empty shape. This pin
extends the same handling to the explicit-`prior_session_id` emit path
(~line 372) and guards against a regression that re-introduces the
loud failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool through FastMCP and return the parsed payload.

    Mirrors the helper in `tests/test_server.py` so this guard exercises
    the exact dispatch path where the raw ValueError used to surface as
    a tool error.
    """
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# Each value fails `_session_dir`'s `[A-Za-z0-9_-]` validator: a slash
# (traversal-shaped), a space, a dot/relative segment, and an embedded
# "../". The validator fails closed, so none of these is a real
# traversal — the point is the failure mode, which must be quiet.
_HOSTILE_IDS = [
    "sess/with/slash",
    "sess with space",
    "sess.with.dot",
    "../etc/passwd",
    "..",
]


@pytest.mark.parametrize("bad_id", _HOSTILE_IDS)
async def test_episode_handoff_explicit_invalid_prior_session_id_degrades(
    memory_dir: Path,
    bad_id: str,
) -> None:
    """A path-shaped explicit `prior_session_id` returns the graceful
    `{prior_session_id: <id>, episodes: []}` shape instead of raising.

    Pre-fix, `list_by_session(<bad_id>)` on the emit path raised a raw
    ValueError that FastMCP surfaced as a tool error at /loop iteration
    entry. The fix wraps that call in `try/except ValueError: all_eps =
    []`, matching the auto-resolution branch and `episode_search`.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )

    # Must not raise — call_tool would otherwise propagate a wrapped
    # ValueError. The returned shape echoes the (invalid) id verbatim
    # and surfaces an empty episode list.
    res = await _call(server, "episode_handoff", prior_session_id=bad_id)

    assert res["prior_session_id"] == bad_id
    assert res["episodes"] == []
    # The crash-signal `note` key is reserved for the floor-only case;
    # an invalid id is not a crash, so it must stay absent (additive
    # surface key — callers that don't know it see the unchanged shape).
    assert "note" not in res


async def test_episode_handoff_valid_explicit_prior_session_id_still_reads(
    memory_dir: Path,
) -> None:
    """Guard against over-broad swallowing: a VALID explicit
    `prior_session_id` must still surface that session's takeaways. The
    fix only catches the validator's ValueError; a well-formed id reads
    through unchanged."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A writes a real takeaway.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode_write", body="A's note", takeaway="from A")

    # Recover A's (valid) session_id from its episode file's frontmatter.
    from bettermemory.episodes import EpisodeStore

    ep_store = EpisodeStore(memory_dir)
    a_session_id: str
    for sid in ep_store.iter_session_ids():
        eps = ep_store.list_by_session(sid)
        if any("A's note" in e.body for e in eps):
            a_session_id = sid
            break
    else:
        raise AssertionError("could not locate session A's id")

    # A fresh session asks for A's id explicitly — the read still works.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_b, "episode_handoff", prior_session_id=a_session_id)
    assert res["prior_session_id"] == a_session_id
    assert len(res["episodes"]) == 1
    assert res["episodes"][0]["takeaway"] == "from A"


async def test_episode_handoff_floor_only_note_is_not_a_bare_crash_claim(
    memory_dir: Path,
) -> None:
    """A clean read-only /loop tick (episode_handoff at entry, no
    episode_write at exit) leaves the SAME floor-only shape on disk as a
    genuine crash, because the session-tag floor is written
    UNCONDITIONALLY at handoff entry. The adopted-prior note must NOT
    assert unconditionally that the prior session *crashed* — it must
    acknowledge the benign read-only-tick reading too.

    Pre-fix the note was the bare sentence "Prior session crashed before
    writing a takeaway. ... but no episode_write was issued before the
    crash." — a misleading definitive claim for the clean-tick case.
    Post-fix the note acknowledges both readings (crash OR read-only
    tick) while still mentioning 'crashed' as one possibility.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: a fresh session runs episode_handoff (writes the entry
    # floor) and then ends WITHOUT calling episode_write. This is a
    # clean read-only tick, not a crash — but on disk it is
    # indistinguishable from one.
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode_handoff")

    # Tick T+1: a fresh session in the same (test) worktree resolves the
    # floor-only prior session and surfaces the marker note.
    server_t_plus_1 = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = await _call(server_t_plus_1, "episode_handoff")

    # The floor-only prior session IS adopted (T anchored its worktree
    # on disk via the floor) and no takeaway bodies are emitted.
    assert res["prior_session_id"] is not None
    assert res["episodes"] == []
    # The marker note fires for the floor-only adoption.
    assert "note" in res, (
        f"floor-only prior session should surface a marker note; got: {res!r}"
    )
    note = res["note"]

    # The note must NOT be a bare, unconditional crash claim. The
    # clean read-only-tick reading has to be acknowledged. The old
    # (buggy) note began with this exact sentence and never mentioned a
    # read-only tick.
    assert not note.startswith("Prior session crashed before writing a takeaway."), (
        f"note must not assert a bare crash for the ambiguous floor-only "
        f"shape; got: {note!r}"
    )
    assert "read-only tick" in note, (
        f"note must acknowledge the benign read-only-tick reading; got: {note!r}"
    )
    # 'crashed' is still an acknowledged possibility, just no longer the
    # sole framing — this keeps the shape informative and keeps the
    # existing crash-recovery assertions in test_server.py green.
    assert "crash" in note.lower(), (
        f"note should still name crash as one possible reading; got: {note!r}"
    )
