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
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1: a real takeaway (no handoff — a single real episode on disk).
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s1, "episode_write", body="S1 body", takeaway="from S1")

    # Recover S1's session id from disk so we can assert the rewind
    # resolved to it (not to the floor-only S2).
    ep_store = EpisodeStore(memory_dir)
    s1_session_id: str
    for sid in ep_store.iter_session_ids():
        eps = ep_store.list_by_session(sid)
        if any("S1 body" in e.body for e in eps):
            s1_session_id = sid
            break
    else:
        raise AssertionError("could not locate session S1's id")

    # S2: a clean read-only tick — handoff writes the entry floor, then
    # the session ends without an episode_write. Floor-only on disk.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "episode_handoff")

    # S3: handoff. Must rewind past the floor-only S2 to S1's takeaway.
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_s3, "episode_handoff")

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
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1: a real takeaway (no handoff — a single real episode on disk).
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s1, "episode_write", body="S1 zero body", takeaway="from S1")

    # Recover S1's session id from disk so we can assert the rewind
    # resolved to it (not to the zero-episode S2).
    ep_store = EpisodeStore(memory_dir)
    s1_session_id: str
    for sid in ep_store.iter_session_ids():
        eps = ep_store.list_by_session(sid)
        if any("S1 zero body" in e.body for e in eps):
            s1_session_id = sid
            break
    else:
        raise AssertionError("could not locate session S1's id")

    # S2: a search-only tick. `memory_search` records an event (stamped
    # with S2's worktree_root) but writes NO episode — and crucially S2
    # never calls episode_handoff, so there is no entry floor either.
    # S2 is a genuine ZERO-EPISODE session on disk.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "memory_search", query="anything at all")

    # Precondition: S2 truly has zero episodes on disk (distinguishes this
    # from the floor-only sibling test — no floor exists for S2).
    s2_ids_with_eps = {
        sid for sid in ep_store.iter_session_ids() if ep_store.list_by_session(sid)
    }
    assert s1_session_id in s2_ids_with_eps
    assert len(s2_ids_with_eps) == 1, (
        f"only S1 should have episodes on disk; S2 must be zero-episode. "
        f"sessions-with-episodes: {s2_ids_with_eps!r}"
    )

    # S3: handoff. Must rewind PAST the zero-episode S2 to S1's takeaway
    # rather than adopting-and-breaking on S2 (episodes: []).
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_s3, "episode_handoff")

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
        reader: memory_scope_disable("projects:alpha") → episode_handoff

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
    await _call(server_s2, "episode_handoff")

    # S3 (newer): a real takeaway in projects:alpha (no handoff — the
    # repro's minimal shape; the mixed floor+hidden variant is covered
    # by the transparency test below).
    server_s3 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep_s3 = await _call(
        server_s3,
        "episode_write",
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
    await _call(server_reader, "memory_scope_disable", scope="projects:alpha")
    res = await _call(server_reader, "episode_handoff")

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
    assert "memory_scope_enable" in note, (
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
        reader: memory_scope_disable("projects:alpha") → episode_handoff
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S1 (older): a visible real takeaway.
    server_s1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep_s1 = await _call(
        server_s1,
        "episode_write",
        body="S1 tools body",
        takeaway="from S1",
        scopes=["tools"],
    )
    s1_session_id = ep_s1["session_id"]

    # S2 (newer): handoff (floor) + alpha-scoped takeaway → floor +
    # hidden-real once the reader disables the scope.
    server_s2 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_s2, "episode_handoff")
    await _call(
        server_s2,
        "episode_write",
        body="S2 alpha body",
        takeaway="S2 on alpha",
        scopes=["projects:alpha"],
    )

    server_reader = build_server(
        config=cfg, store=Store(memory_dir), state=SessionState()
    )
    await _call(server_reader, "memory_scope_disable", scope="projects:alpha")
    res = await _call(server_reader, "episode_handoff")

    # The rewind reaches THROUGH the hidden S2 to S1's visible takeaway.
    assert res["prior_session_id"] == s1_session_id, (
        f"walk must rewind through the hidden S2 to S1; got: {res!r}"
    )
    assert [e["takeaway"] for e in res["episodes"]] == ["from S1"]
    # Transparent: no note while a visible takeaway was reachable.
    assert "note" not in res, (
        f"rewinding past a scope-hidden session must stay noteless; got: {res!r}"
    )


async def test_episode_handoff_floor_only_note_names_promotion_when_log_shows_it(
    memory_dir: Path,
) -> None:
    """Honesty fix: the floor-only note asserted "no episode_write
    followed — either it crashed before the takeaway, or it was a clean
    read-only tick", but `episode_promote` DELETES the source episode on
    commit, so a perfectly healthy handoff → episode_write →
    episode_promote session ends floor-only on disk and the note's
    either/or was false on both horns. The event log disambiguates:
    the session's `episode_write` event carries the episode id, and a
    matching `episode_promote` event proves the deletion path. When that
    trace exists the note must say the takeaway was PROMOTED (and lives
    on as a durable memory) instead of hedging crash-or-empty.

    Mutation-soundness: reverting the promotion-trace check makes the
    note fall back to the old either/or text — the "promoted into a
    durable memory" assertion fails and the two `not in` assertions
    fail (the false "no episode_write followed" claim reappears).
    """
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # Tick T: the healthy /loop shape — handoff (floor), a real
    # takeaway, then promotion of that takeaway into a durable memory.
    server_t = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server_t, "episode_handoff")
    ep = await _call(
        server_t,
        "episode_write",
        body="iter 1 — tuned GC, gophers cleared",
        takeaway="GC tuning fixed gopher frame drops",
    )
    t_session_id = ep["session_id"]
    promo = await _call(
        server_t,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["projects:alpha"],
    )
    assert promo["status"] == "committed", (
        f"test precondition: promotion must commit synchronously; got: {promo!r}"
    )

    # On-disk precondition: T is floor-only now — the real episode was
    # deleted by the promotion, only the entry floor survives.
    eps_on_disk = EpisodeStore(memory_dir).list_by_session(t_session_id)
    assert eps_on_disk and all(e.is_floor for e in eps_on_disk), (
        f"T must be floor-only after the promotion; got: {eps_on_disk!r}"
    )

    # Tick T+1: fresh session resolves the floor-only T.
    server_t1 = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_t1, "episode_handoff")

    assert res["prior_session_id"] == t_session_id
    assert res["episodes"] == []
    assert "note" in res, f"floor-only prior must carry a note; got: {res!r}"
    note = res["note"]
    # The precise cause, not the hedge.
    assert "promoted into a durable memory" in note, (
        f"note must name the promotion the event log proves; got: {note!r}"
    )
    # The disproven either/or must be gone: episode_write demonstrably
    # ran, and the tick was neither a crash-victim nor read-only.
    assert "no episode_write followed" not in note, (
        f"note must not claim episode_write never ran; got: {note!r}"
    )
    assert "read-only tick" not in note, (
        f"note must not hedge the clean-tick reading when the log shows a "
        f"promotion; got: {note!r}"
    )


async def test_episode_handoff_zero_episode_note_names_promotion_when_log_shows_it(
    memory_dir: Path,
) -> None:
    """Zero-episode sibling of the promotion-honesty fix: a session that
    calls episode_write WITHOUT ever calling episode_handoff has no
    floor, so once its only takeaway is promoted out it is zero-episode
    on disk. The old note guessed "it may have been a non-handoff tick,
    or crashed before journaling" — false on both horns: the session
    journaled fine and the journal entry was deliberately distilled into
    a durable memory. With the event-log trace present the note must say
    so.

    Mutation-soundness: reverting the promotion-trace check restores the
    old zero-episode hedge — the "promoted into a durable memory"
    assertion fails and the "non-handoff tick" guess reappears.
    """
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S_a: write-then-promote, no handoff → zero-episode on disk.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep = await _call(
        server_a,
        "episode_write",
        body="iter 1 — tuned GC, gophers cleared",
        takeaway="GC tuning fixed gopher frame drops",
    )
    a_session_id = ep["session_id"]
    promo = await _call(
        server_a,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["projects:alpha"],
    )
    assert promo["status"] == "committed", (
        f"test precondition: promotion must commit synchronously; got: {promo!r}"
    )

    # On-disk precondition: S_a has ZERO episodes (no floor either).
    assert EpisodeStore(memory_dir).list_by_session(a_session_id) == [], (
        "S_a must be zero-episode after the promotion"
    )

    # Reader: resolves S_a via its events' worktree_root (zero-episode
    # branch) and must surface the promotion-aware note.
    server_r = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_r, "episode_handoff")

    assert res["prior_session_id"] == a_session_id
    assert res["episodes"] == []
    assert "note" in res, f"zero-episode prior must carry a note; got: {res!r}"
    note = res["note"]
    assert "promoted into a durable memory" in note, (
        f"note must name the promotion the event log proves; got: {note!r}"
    )
    # The disproven guesses must be gone…
    assert "non-handoff tick" not in note, (
        f"note must not guess a non-handoff tick when the log shows a "
        f"promotion; got: {note!r}"
    )
    assert "crashed" not in note, (
        f"note must not hedge a crash when the log shows a promotion; got: {note!r}"
    )
    # …while the still-true floor facts stay stated (no floor was ever
    # written — the session never called episode_handoff).
    assert "left no handoff floor" in note, (
        f"zero-episode promotion note must still state no floor exists; got: {note!r}"
    )


async def test_episode_handoff_promotion_note_covers_deferred_confirm_path(
    memory_dir: Path,
) -> None:
    """The promotion delete has TWO triggers: synchronous (promote
    returns `committed`) and deferred (promote stages `pending` for the
    user-inference flow; `memory_write_confirm` commits AND deletes the
    source episode later). The confirm event records no episode id, so
    the `episode_promote` event with `write_status="pending"` is the
    only trace of the deferred path — the detection must accept it, and
    it is safe to: a cancelled/expired pending leaves the episode ON
    disk, so a session whose episode is demonstrably gone with a pending
    trace was confirm-deleted.

    Mutation-soundness: narrowing the detection to
    `write_status == "committed"` alone (or reverting the fix entirely)
    routes this case back to the old zero-episode hedge and the
    "promoted into a durable memory" assertion fails.
    """
    from bettermemory.episodes import EpisodeStore

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S_a: write → promote as user-inference (stages pending) → user
    # confirms → durable memory commits, source episode deleted. No
    # handoff, so no floor: S_a ends zero-episode.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep = await _call(
        server_a,
        "episode_write",
        body="Iter 2 — observed the user reaching for terse summaries.",
        takeaway="user prefers terse summaries",
    )
    a_session_id = ep["session_id"]
    pending = await _call(
        server_a,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert pending["status"] == "pending", (
        f"test precondition: user-inference promotion must stage pending; "
        f"got: {pending!r}"
    )
    confirmed = await _call(
        server_a, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert confirmed["status"] == "committed"
    assert EpisodeStore(memory_dir).list_by_session(a_session_id) == [], (
        "S_a must be zero-episode after the confirmed promotion"
    )

    server_r = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_r, "episode_handoff")

    assert res["prior_session_id"] == a_session_id
    assert res["episodes"] == []
    assert "note" in res, f"zero-episode prior must carry a note; got: {res!r}"
    assert "promoted into a durable memory" in res["note"], (
        f"deferred-confirm promotion must be detected via the pending "
        f"trace; got: {res['note']!r}"
    )


async def test_episode_handoff_pending_promotion_not_named_after_cancel_then_prune(
    memory_dir: Path,
) -> None:
    """False-positive guard (round-131): a `pending` episode_promote that is
    CANCELLED (or TTL-expired) never commits — `memory_write_cancel` KEEPS the
    source episode on disk for a retry, so NO durable memory is written. When
    `prune_old_sessions` later rmtrees the whole session directory (the
    automatic TTL sweep any shared-root episode_write triggers), the session
    goes zero-episode on disk while the event log still holds the bare
    `episode_promote` (write_status="pending") trace. The handoff must NOT read
    that as a promotion: the affirmative "promoted into a durable memory" note
    would assert a promotion that never happened and misdirect the reader to
    memory_search for content that does not exist.

    Every trigger step is a designed flow: unattended /loop ticks promote
    user-inference takeaways that stage pending and expire/cancel unconfirmed,
    and the prune is automatic.

    Mutation-soundness: pre-fix `_episode_promoted_out_of_session` counted a
    bare `pending` promote event as proof of deletion, so this sequence
    produced the affirmative promotion note — the
    `"promoted into a durable memory" not in note` assertion FAILS against the
    pre-fix source (verified by reverting the source change with this test in
    place). Post-fix the pending trace is ignored (confirm never ran, so no
    `write_confirm` event carries this episode's id) and the note falls through
    to the honest hedged zero-episode text, which asserts no promotion.
    """
    from datetime import timedelta

    from bettermemory.episodes import EpisodeStore
    from bettermemory.models import utcnow

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))

    # S_a: write a takeaway, promote it as user-inference (stages pending),
    # then CANCEL. Cancel drops the linkage but KEEPS the source episode on
    # disk so the caller can retry — no promotion ever committed.
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    ep = await _call(
        server_a,
        "episode_write",
        body="Iter 3 — user seemed to reach for dark mode in the demo.",
        takeaway="user prefers dark mode",
    )
    a_session_id = ep["session_id"]
    pending = await _call(
        server_a,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
        category="user-inference",
    )
    assert pending["status"] == "pending", (
        f"test precondition: user-inference promotion must stage pending; "
        f"got: {pending!r}"
    )
    cancelled = await _call(
        server_a, "memory_write_cancel", pending_id=pending["pending_id"]
    )
    assert cancelled["existed"] is True
    # Cancel keeps the episode: it is still on disk right now.
    assert EpisodeStore(memory_dir).list_by_session(a_session_id), (
        "cancel must KEEP the source episode on disk for a retry"
    )

    # Prune the whole session dir 31 days on — the automatic TTL sweep that
    # any later shared-root episode_write triggers. S_a is now zero-episode on
    # disk, but its `episode_write` + pending `episode_promote` events live
    # forever in the (never-pruned) event log.
    pruned = EpisodeStore(memory_dir).prune_old_sessions(
        now=utcnow() + timedelta(days=31)
    )
    assert a_session_id in pruned, (
        f"S_a's session dir must be pruned past the 30d TTL; pruned: {pruned!r}"
    )
    assert EpisodeStore(memory_dir).list_by_session(a_session_id) == [], (
        "S_a must be zero-episode after the prune"
    )

    # Reader: resolves the pruned S_a via its events' worktree_root
    # (zero-episode branch). The note must NOT assert a promotion.
    server_r = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call(server_r, "episode_handoff")

    assert res["prior_session_id"] == a_session_id
    assert res["episodes"] == []
    note = res.get("note", "")
    # THE regression assertion: no false promotion claim for an unconfirmed
    # (cancelled) pending that was subsequently pruned.
    assert "promoted into a durable memory" not in note, (
        f"a cancelled-then-pruned pending promotion must NOT be reported as a "
        f"promotion — no durable memory was ever written; got: {note!r}"
    )
    # The honest hedged zero-episode note fires instead (its distinctive
    # phrase, absent from the promotion note).
    assert "non-handoff tick" in note, (
        f"note must fall through to the honest hedged zero-episode text; got: {note!r}"
    )
