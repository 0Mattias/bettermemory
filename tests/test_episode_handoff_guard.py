"""The handoff's guards: an explicit, path-shaped `prior_session_id`
degrades gracefully, and the auto-resolution walk rewinds past sessions
that left nothing visible and says so honestly.

`episode(action="handoff")` is the documented FIRST call at a /loop
iteration entry, and `prior_session_id` is caller-supplied: a child agent
may pass its parent's id, which can be mistyped or path-shaped. Such an
id flows verbatim into the store's per-session read, which holds no rows
for it; the caller gets the graceful `episodes: []` shape rather than a
raw tool error on the hot path.

The rest of the file pins the walk: a floor-only or zero-episode prior
session does not sever the chain to an older real takeaway, a session
hidden by a disabled scope is rewound past without a note while
something visible is reachable, and an out-of-process hook row never
manufactures a phantom session.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _session_with_body(memory_dir: Path, marker: str) -> str:
    """The session id whose journal holds `marker`, read from the store."""
    store = Store.open(memory_dir)
    for sid in store.episode_session_ids():
        if any(marker in e.body for e in store.episodes_by_session(sid)):
            return sid
    raise AssertionError(f"could not locate the session holding {marker!r}")


# Each value is path-shaped: a slash (traversal-shaped), a space, a
# dot/relative segment, and an embedded "../". The store holds no rows
# under any of them, so none of these is a real traversal; the point is
# the failure mode, which must be quiet.
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

    A raw error here would surface as a tool error at /loop iteration
    entry; the explicit path returns the same empty shape the
    auto-resolution branch does.
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
    res = await _call(server, "episode", action="handoff", prior_session_id=bad_id)

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
    `prior_session_id` must still surface that session's takeaways. A
    well-formed id reads through unchanged."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Session A writes a real takeaway.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_a, "episode", action="write", body="A's note", takeaway="from A")

    # Recover A's (valid) session_id from the store.
    a_session_id = _session_with_body(memory_dir, "A's note")

    # A fresh session asks for A's id explicitly — the read still works.
    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(
        server_b, "episode", action="handoff", prior_session_id=a_session_id
    )
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
    await _call(server_t, "episode", action="handoff")

    # Tick T+1: a fresh session in the same (test) worktree resolves the
    # floor-only prior session and surfaces the marker note.
    server_t_plus_1 = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    res = await _call(server_t_plus_1, "episode", action="handoff")

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


async def test_episode_handoff_rewinds_past_floor_only_to_older_real_takeaway(
    memory_dir: Path,
) -> None:
    """Rewind contract (episode-handoff-chain): a floor-only session must
    not sever the handoff chain. Sequence:

        S1: writes a REAL takeaway ("from S1")
        S2: a clean read-only /loop tick — episode_handoff at entry
            (writes the unconditional floor) and NO episode_write, so on
            disk it is floor-only
        S3: calls episode_handoff at entry

    S2 is S3's immediately-prior worktree session, and it is floor-only.
    The pre-fix walk adopted the FIRST worktree-matching session (S2) and
    stopped, returning `episodes: []` — S1's real takeaway became
    unreachable, severing the chain. The rewind walks PAST S2 to S1 and
    surfaces S1's takeaway, while still attaching the honest soft note
    that the immediately-preceding session (S2) recorded nothing.

    Mutation-soundness: reverting the rewind makes the walk stop at S2
    and return `episodes: []` with `prior_session_id == S2` — both the
    `takeaway == "from S1"` and `prior_session_id == S1` assertions
    below fail. The note assertion fails if the soft note is dropped.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1: a real takeaway (no handoff — a single real episode on disk).
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_s1, "episode", action="write", body="S1 body", takeaway="from S1"
    )

    # Recover S1's session id from disk so we can assert the rewind
    # resolved to it (not to the floor-only S2).
    s1_session_id = _session_with_body(memory_dir, "S1 body")

    # S2: a clean read-only tick — handoff writes the entry floor, then
    # the session ends without an episode_write. Floor-only on disk.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "episode", action="handoff")

    # S3: handoff. Must rewind past the floor-only S2 to S1's takeaway.
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_s3, "episode", action="handoff")

    # The chain is intact: S1's real takeaway is surfaced, NOT episodes:[].
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["from S1"], (
        f"rewind must surface S1's takeaway past the floor-only S2; got: {res!r}"
    )
    # The resolved prior id is S1 (the rewound-to real session), not S2.
    assert res["prior_session_id"] == s1_session_id, (
        f"prior_session_id should rewind to S1, not the floor-only S2; got: {res!r}"
    )
    # The honest soft note still fires: the IMMEDIATELY-preceding session
    # (S2) recorded no takeaway, even though an older one is surfaced.
    assert "note" in res, (
        f"floor-only immediately-prior session should still surface the "
        f"soft note alongside the rewound takeaway; got: {res!r}"
    )
    assert "read-only tick" in res["note"], (
        f"note must acknowledge the benign read-only-tick reading; got: {res['note']!r}"
    )


async def test_episode_handoff_rewinds_past_zero_episode_to_older_real_takeaway(
    memory_dir: Path,
) -> None:
    """Rewind contract (episode-zero-episode): a ZERO-EPISODE session — one
    that recorded events but wrote NO episodes at all (not even a floor) —
    must not sever the handoff chain, exactly like the floor-only case.
    Sequence:

        S1: writes a REAL takeaway ("from S1")
        S2: a search-only tick — records an event (memory_search) but never
            calls episode_handoff (no entry floor) and never episode_write,
            so it has ZERO episodes on disk while its events carry S2's
            worktree_root
        S3: calls episode_handoff at entry

    S2 is S3's immediately-prior worktree session, and it is zero-episode.
    The pre-fix zero-episode branch adopted-and-broke on the FIRST
    worktree-matching zero-episode candidate (S2), AHEAD of the rewind,
    returning `episodes: []` with `prior_session_id == S2` — S1's real
    takeaway became unreachable, severing the chain (the exact bug the
    round-120 rewind fixed for floor-only sessions, left unhandled for
    zero-episode sessions). The fix treats S2 like a floor-only tick:
    remember it as the fallback and walk PAST it to S1, surfacing S1's
    takeaway plus the honest soft note.

    Mutation-soundness: reverting the fix (restoring the zero-episode
    `resolved_session_id = sid; break`) makes the walk stop at S2 and
    return `episodes: []` with `prior_session_id == S2` — both the
    `takeaway == "from S1"` and `prior_session_id == S1` assertions below
    fail. The note assertion fails if the soft note is dropped.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1: a real takeaway (no handoff — a single real episode on disk).
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_s1, "episode", action="write", body="S1 zero body", takeaway="from S1"
    )

    # Recover S1's session id from disk so we can assert the rewind
    # resolved to it (not to the zero-episode S2).
    s1_session_id = _session_with_body(memory_dir, "S1 zero body")

    # S2: a search-only tick. `memory_search` records an event (stamped
    # with S2's worktree_root) but writes NO episode — and crucially S2
    # never calls episode_handoff, so there is no entry floor either.
    # S2 is a genuine ZERO-EPISODE session on disk.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "memory_search", query="anything at all")

    # Precondition: S2 truly has zero episodes on disk (distinguishes this
    # from the floor-only sibling test — no floor exists for S2).
    journal = Store.open(memory_dir)
    s2_ids_with_eps = {
        sid for sid in journal.episode_session_ids() if journal.episodes_by_session(sid)
    }
    assert s1_session_id in s2_ids_with_eps
    assert len(s2_ids_with_eps) == 1, (
        f"only S1 should have episodes on disk; S2 must be zero-episode. "
        f"sessions-with-episodes: {s2_ids_with_eps!r}"
    )

    # S3: handoff. Must rewind PAST the zero-episode S2 to S1's takeaway
    # rather than adopting-and-breaking on S2 (episodes: []).
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_s3, "episode", action="handoff")

    # The chain is intact: S1's real takeaway is surfaced, NOT episodes:[].
    takeaways = [e["takeaway"] for e in res["episodes"]]
    assert takeaways == ["from S1"], (
        f"rewind must surface S1's takeaway past the zero-episode S2; got: {res!r}"
    )
    # The resolved prior id is S1 (the rewound-to real session), not S2.
    assert res["prior_session_id"] == s1_session_id, (
        f"prior_session_id should rewind to S1, not the zero-episode S2; got: {res!r}"
    )
    # The honest soft note still fires: the IMMEDIATELY-preceding session
    # (S2) recorded no takeaway, even though an older one is surfaced.
    assert "note" in res, (
        f"zero-episode immediately-prior session should still surface the "
        f"soft note alongside the rewound takeaway; got: {res!r}"
    )
    # The note must be the ZERO-EPISODE variant, not the floor-only one: S2
    # left NO floor (it never called episode_handoff), so a note claiming "it
    # called episode_handoff (which wrote the session-tag floor...)" would be a
    # lie. Mutation-sound: reverting the note split (routing zero-episode
    # through the floor-only note) makes the "wrote the session-tag floor"
    # clause appear and fails the `not in` assertions.
    assert "left no handoff floor" in res["note"], (
        f"zero-episode note must state no floor was left; got: {res['note']!r}"
    )
    assert "wrote the session-tag floor" not in res["note"], (
        f"zero-episode note must NOT claim a floor was written; got: {res['note']!r}"
    )
    assert "it called episode_handoff" not in res["note"], (
        f"zero-episode note must NOT claim episode_handoff was called; got: "
        f"{res['note']!r}"
    )


async def test_episode_handoff_all_hidden_prior_falls_back_instead_of_first_ever(
    memory_dir: Path,
) -> None:
    """Regression (v3.15.0, transparent-rewind suppression): a worktree
    session whose REAL episodes are ALL hidden by `disabled_scopes` set
    `seen_worktree_match=True` while remembering NOTHING — so when no
    visible takeaway existed anywhere, it both suppressed every older
    floor-only / zero-episode fallback and contributed no fallback
    itself, and the handoff fell out with `{prior_session_id: None,
    episodes: []}` — the shape the docstring reserves for "first-ever
    invocation in a worktree". Sequence (the confirmed repro):

        S2 (older): a clean read-only tick — episode_handoff at entry
            (unconditional floor), no episode_write → floor-only on disk
        S3 (newer): writes a REAL takeaway scoped projects:alpha
        reader: disables projects:alpha, then episode handoff

    v3.14.1 surfaced an honest "prior existed, no takeaway" result;
    v3.15.0 returned the first-ever shape with no note. The fix treats
    the fully hidden S3 as a fallback CANDIDATE: it keeps its
    immediately-prior role (an older floor-only session must not
    masquerade as immediately-prior), the walk still rewinds past it,
    and when the walk exhausts with nothing visible S3 itself is
    surfaced as `prior_session_id` with `episodes: []` plus a note
    naming the scope-hide cause (not the floor-only/zero-episode texts,
    which would both be lies — S3 journaled fine).

    Mutation-soundness: reverting the fix (restoring the branch that
    sets `seen_worktree_match` without remembering a scope-hidden
    fallback) makes `prior_session_id` come back None and drops the
    note — the first three assertions below all fail.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S2 (older): clean read-only tick — floor-only on disk.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "episode", action="handoff")

    # S3 (newer): a real takeaway in projects:alpha (no handoff — the
    # repro's minimal shape; the mixed floor+hidden variant is covered
    # by the transparency test below).
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep_s3 = await _call(
        server_s3,
        "episode",
        action="write",
        body="S3 alpha-scoped body",
        takeaway="S3 on alpha",
        scopes=["projects:alpha"],
    )
    s3_session_id = ep_s3["session_id"]

    # Reader: disables projects:alpha, so S3's only real takeaway is
    # hidden and S2 is floor-only — NO visible takeaway exists anywhere.
    server_reader = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    await _call(
        server_reader, "memory_admin", action="disable_scope", scope="projects:alpha"
    )
    res = await _call(server_reader, "episode", action="handoff")

    # Never the first-ever shape while worktree sessions demonstrably
    # exist — this is THE regression assertion (v3.15.0 returned None).
    assert res["prior_session_id"] is not None, (
        f"fully-hidden prior must not collapse to the first-ever shape; got: {res!r}"
    )
    # The fallback is the immediately-prior worktree session (the hidden
    # S3), not the older floor-only S2 — the note describes the session
    # that actually sits immediately behind the caller.
    assert res["prior_session_id"] == s3_session_id, (
        f"fallback should be the hidden immediately-prior S3; got: {res!r}"
    )
    # Hidden bodies stay hidden: the emit-step scope filter applies.
    assert res["episodes"] == []
    assert "note" in res, (
        f"scope-hidden terminal shape must carry an honest note; got: {res!r}"
    )
    note = res["note"]
    # The note names the actual cause (scope disable) and the way out.
    assert "disabled" in note, f"note must name the scope-disable cause; got: {note!r}"
    assert "enable_scope" in note, (
        f"note should point at the re-enable escape hatch; got: {note!r}"
    )
    # And it must NOT be either empty-session text — S3 neither crashed
    # nor skipped journaling, so those hedges would be false claims.
    assert "crash" not in note.lower(), (
        f"scope-hidden note must not hedge a crash; got: {note!r}"
    )
    assert "no episode_write followed" not in note, (
        f"scope-hidden note must not claim episode_write never ran; got: {note!r}"
    )


async def test_episode_handoff_rewinds_through_hidden_session_without_note(
    memory_dir: Path,
) -> None:
    """Transparent-rewind contract preserved by the scope-hidden
    fallback fix: a fully hidden session sitting between the caller and
    an older VISIBLE takeaway is still rewound past — the older takeaway
    is adopted, and NO note fires (the user explicitly suppressed that
    scope; while something visible is reachable the hidden session stays
    silent). This pins the fix's set-note-at-fallback-resolution shape:
    an implementation that flags the scope-hidden note eagerly in the
    walk (like the floor-only note) would attach a note here and fail
    the final assertion.

        S1 (older): visible real takeaway in `tools`
        S2 (newer): a /loop-shaped tick — episode_handoff at entry
            (writes its floor) then a real takeaway in projects:alpha,
            so on disk it is floor + hidden-real (the mixed shape)
        reader: disables projects:alpha, then episode handoff
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1 (older): a visible real takeaway.
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep_s1 = await _call(
        server_s1,
        "episode",
        action="write",
        body="S1 tools body",
        takeaway="from S1",
        scopes=["tools"],
    )
    s1_session_id = ep_s1["session_id"]

    # S2 (newer): handoff (floor) + alpha-scoped takeaway → floor +
    # hidden-real once the reader disables the scope.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "episode", action="handoff")
    await _call(
        server_s2,
        "episode",
        action="write",
        body="S2 alpha body",
        takeaway="S2 on alpha",
        scopes=["projects:alpha"],
    )

    server_reader = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    await _call(
        server_reader, "memory_admin", action="disable_scope", scope="projects:alpha"
    )
    res = await _call(server_reader, "episode", action="handoff")

    # The rewind reaches THROUGH the hidden S2 to S1's visible takeaway.
    assert res["prior_session_id"] == s1_session_id, (
        f"walk must rewind through the hidden S2 to S1; got: {res!r}"
    )
    assert [e["takeaway"] for e in res["episodes"]] == ["from S1"]
    # Transparent: no note while a visible takeaway was reachable.
    assert "note" not in res, (
        f"rewinding past a scope-hidden session must stay noteless; got: {res!r}"
    )


async def test_episode_handoff_skips_out_of_process_hook_phantom_sessions(
    memory_dir: Path,
) -> None:
    """A newer "session" whose only events are client-side hook rows
    (`triggered_from` in `hook._OUT_OF_PROCESS_TRIGGERS`) must not enter
    the auto-resolution walk at all. Those rows record under Claude
    Code's transcript session id — a namespace that can never hold
    episodes — so admitting one manufactures a worktree-matching
    zero-episode phantom between the caller and its real predecessor.

    Sequence:

        S1: writes a REAL takeaway ("from S1"), plus a search event so
            the server's stamped worktree_root is on disk to harvest
        P:  a forged Stop-hook row — transcript-id session, newest ts,
            same worktree_root, `triggered_from="stop_hook"`
        S3: calls episode_handoff at entry

    Post-fix, P is invisible: S1 resolves as the immediately-prior
    session and its takeaway surfaces with NO note. Pre-fix, P is a
    zero-episode candidate: the rewind still reaches S1's takeaway, but
    the handoff reports the misleading zero-episode `note` claiming the
    immediately-preceding session journaled nothing — the exact shape
    this store's own run log showed while the defect was live.

    Mutation-soundness: reverting the `_OUT_OF_PROCESS_TRIGGERS` skip in
    the walk makes `"note" in res` true and the note assertion below
    fail; the takeaway/id assertions keep the rewind contract honest
    either way.
    """
    from bettermemory.events import Recorder

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1: a real takeaway plus one search event (worktree_root donor).
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(
        server_s1, "episode", action="write", body="S1 phantom body", takeaway="from S1"
    )
    await _call(server_s1, "memory_search", query="anything at all")

    s1_session_id = _session_with_body(memory_dir, "S1 phantom body")

    # Harvest the worktree stamp S1's server put on its events, so the
    # phantom is a worktree MATCH for the walk (a None-worktree phantom
    # would be invisible even pre-fix and pin nothing).
    journal = Store.open(memory_dir)
    stamps = {
        ev.get("worktree_root")
        for ev in journal.iter_events()
        if isinstance(ev.get("worktree_root"), str)
    }
    assert stamps, "expected at least one worktree-stamped event to harvest"
    (worktree,) = stamps

    # P: the forged Stop-hook row — newest event in the log, transcript
    # session id, out-of-process trigger. Recorder stamps ts=now, which
    # is strictly newer than S1's events written above.
    phantom = Recorder(
        store=journal,
        session_id="cc-transcript-phantom-1234",
        worktree_root=worktree,
    )
    phantom.record("turn_audited", triggered_from="stop_hook", verdict="ok")

    # S3: handoff must resolve straight to S1 — no phantom, no note.
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_s3, "episode", action="handoff")

    assert [e["takeaway"] for e in res["episodes"]] == ["from S1"], (
        f"handoff must surface S1's takeaway; got: {res!r}"
    )
    assert res["prior_session_id"] == s1_session_id, (
        f"prior_session_id must be S1, never the hook-phantom transcript id; "
        f"got: {res!r}"
    )
    assert "note" not in res, (
        f"a hook-phantom candidate must not manufacture the zero-episode "
        f"rewind note; got: {res!r}"
    )
