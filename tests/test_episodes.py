"""Storage-layer tests for `EpisodeStore`.

Handler-level tests live in `tests/test_server.py` alongside the other
MCP tool tests. The cuts here pin the on-disk shape and prune semantics
so a future refactor can't silently break the format or the TTL
contract.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.episodes import EpisodeStore
from bettermemory.origin import Origin


@pytest.fixture
def episode_store(tmp_path: Path) -> EpisodeStore:
    return EpisodeStore(tmp_path)


def test_write_creates_session_dir_lazily(episode_store: EpisodeStore) -> None:
    """The `episodes/` subtree only appears on first write — a fresh
    install with no episodes leaves no empty dir behind."""
    assert not episode_store.episodes_dir.exists()
    episode_store.write(session_id="sess_aaaa1111", body="hello")
    assert episode_store.episodes_dir.exists()
    assert (episode_store.episodes_dir / "sess_aaaa1111").is_dir()


def test_write_persists_takeaway_and_scopes(episode_store: EpisodeStore) -> None:
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="iteration body",
        takeaway="one-line summary",
        scopes=["projects:foo", "tools"],
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert len(loaded) == 1
    assert loaded[0].id == ep.id
    assert loaded[0].takeaway == "one-line summary"
    assert loaded[0].scopes == ["projects:foo", "tools"]


def test_write_persists_origin(episode_store: EpisodeStore) -> None:
    origin = Origin(
        cwd="/tmp/work",
        repo="https://github.com/0Mattias/example",
        branch="main",
        worktree_root="/tmp/work",
    )
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="origin test",
        origin=origin,
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert loaded[0].origin is not None
    assert loaded[0].origin.repo == "https://github.com/0Mattias/example"
    assert loaded[0].id == ep.id


def test_swarm_id_persists_and_round_trips(episode_store: EpisodeStore) -> None:
    ep = episode_store.write(
        session_id="sess_subagent1",
        body="sub-agent finding",
        takeaway="found X",
        swarm_id="sess_coordaaa",
    )
    loaded = episode_store.list_by_session("sess_subagent1")
    assert len(loaded) == 1
    assert loaded[0].id == ep.id
    assert loaded[0].swarm_id == "sess_coordaaa"


def test_swarm_id_omitted_from_frontmatter_when_absent(
    episode_store: EpisodeStore,
) -> None:
    """A non-swarm episode keeps the pre-field on-disk shape: the
    `swarm_id` frontmatter key is only emitted when set (parity with
    `is_floor` / `takeaway` / `scopes`)."""
    episode_store.write(session_id="sess_aaaa1111", body="no swarm here")
    files = list((episode_store.episodes_dir / "sess_aaaa1111").glob("*.md"))
    assert len(files) == 1
    assert "swarm_id" not in files[0].read_text()
    assert episode_store.list_by_session("sess_aaaa1111")[0].swarm_id is None


def test_naive_created_episode_does_not_crash_reads(
    episode_store: EpisodeStore,
) -> None:
    """Whole-tree sweep (MEDIUM): a hand-edited or legacy episode whose
    frontmatter `created` has no UTC offset parses as a NAIVE datetime.
    Before the fix that naive value reached the `created` sort
    (list_by_session / list_by_swarm) and the `since` filter
    (episode_search) and raised an uncaught TypeError comparing naive vs
    aware — failing the WHOLE episode read instead of skipping one row.
    Load-time ensure_utc now normalises it so reads sort it alongside aware
    siblings."""
    episode_store.write(session_id="sess_aaaa1111", body="aware one")
    episode_store.write(session_id="sess_aaaa1111", body="naive one")

    # Rewrite the second file's `created` to a bare (offset-less) value, as a
    # hand-edit or a legacy/external writer would leave it. Reusing a real
    # episode keeps the id/scopes valid so it still loads rather than being
    # skipped for an unrelated reason.
    target = next(
        p
        for p in episode_store._iter_session_paths("sess_aaaa1111")
        if "naive one" in p.read_text(encoding="utf-8")
    )
    rewritten = [
        "created: 2024-01-01 12:00:00" if line.startswith("created:") else line
        for line in target.read_text(encoding="utf-8").splitlines()
    ]
    target.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # The sort must not raise mixing naive + aware; both episodes return.
    eps = episode_store.list_by_session("sess_aaaa1111")
    assert {e.body.strip() for e in eps} == {"aware one", "naive one"}
    naive_ep = next(e for e in eps if e.body.strip() == "naive one")
    assert naive_ep.created.tzinfo is not None  # normalised to aware UTC


def test_bare_date_created_episode_does_not_crash_reads(
    episode_store: EpisodeStore,
) -> None:
    """A bare YAML date `created: 2026-05-31` (no time component) parses
    as a `datetime.date` — NOT a datetime, NOT a str. The earlier
    hardening pass coerced `created` with bare `ensure_utc`, which is
    typed `datetime | None -> datetime | None` and touches `.tzinfo`,
    so this shape raised AttributeError. That is NOT in the
    (ValueError, KeyError, OSError) catch in `list_by_session`, so a
    SINGLE such file crashed the whole episode read surface
    (episode_search, episode_promote, list_by_swarm, and
    episode_handoff on the /loop hot path). Load-time date-aware
    coercion (mirroring `store._as_dt`) now lifts a bare date to UTC
    midnight, so the read sorts it alongside well-formed siblings
    instead of crashing.

    Distinct from `test_naive_created_episode_does_not_crash_reads`,
    which exercises a naive *datetime* shape that the datetime branch
    already handled — this one pins the `datetime.date` branch."""
    episode_store.write(session_id="sess_aaaa1111", body="well-formed sibling")
    episode_store.write(session_id="sess_aaaa1111", body="bare date one")

    target = next(
        p
        for p in episode_store._iter_session_paths("sess_aaaa1111")
        if "bare date one" in p.read_text(encoding="utf-8")
    )
    # Unquoted, date-only — the vendored YAML loader parses this as a
    # `datetime.date`, the shape that used to crash the read.
    rewritten = [
        "created: 2026-05-31" if line.startswith("created:") else line
        for line in target.read_text(encoding="utf-8").splitlines()
    ]
    target.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # Must not raise; the well-formed sibling is always returned, and the
    # bare-date row is coerced (UTC midnight) rather than crashing the read.
    eps = episode_store.list_by_session("sess_aaaa1111")
    bodies = {e.body.strip() for e in eps}
    assert "well-formed sibling" in bodies
    bare_ep = next(e for e in eps if e.body.strip() == "bare date one")
    assert bare_ep.created == datetime(2026, 5, 31, tzinfo=timezone.utc)
    assert bare_ep.created.tzinfo is not None


def test_quoted_str_created_episode_does_not_crash_reads(
    episode_store: EpisodeStore,
) -> None:
    """A quoted `created: "2026-05-31T12:00:00"` stays a `str` through
    the vendored YAML loader (quoting suppresses native timestamp
    parsing). The earlier hardening pass coerced `created` with bare
    `ensure_utc`, which touches `.tzinfo`, so a str raised
    AttributeError — NOT caught by the (ValueError, KeyError, OSError)
    skip in `list_by_session`, crashing the whole episode read surface
    (including episode_handoff on the /loop hot path) on a single such
    file. Load-time coercion now routes a str through `parse_event_ts`,
    the canonical ISO parser, so the read sorts it alongside
    well-formed siblings.

    Distinct from `test_naive_created_episode_does_not_crash_reads`
    (naive *datetime* shape) — this one pins the `str` branch."""
    episode_store.write(session_id="sess_aaaa1111", body="well-formed sibling")
    episode_store.write(session_id="sess_aaaa1111", body="quoted str one")

    target = next(
        p
        for p in episode_store._iter_session_paths("sess_aaaa1111")
        if "quoted str one" in p.read_text(encoding="utf-8")
    )
    # Quoted forces the loader to keep it as a string (no offset) — the
    # shape that used to crash the read.
    rewritten = [
        'created: "2026-05-31T12:00:00"' if line.startswith("created:") else line
        for line in target.read_text(encoding="utf-8").splitlines()
    ]
    target.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # Must not raise; the well-formed sibling is always returned, and the
    # quoted-str row is parsed (and stamped UTC) rather than crashing.
    eps = episode_store.list_by_session("sess_aaaa1111")
    bodies = {e.body.strip() for e in eps}
    assert "well-formed sibling" in bodies
    quoted_ep = next(e for e in eps if e.body.strip() == "quoted str one")
    assert quoted_ep.created == datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    assert quoted_ep.created.tzinfo is not None


@pytest.mark.parametrize(
    "scopes_line",
    ["scopes: 5", "scopes: banana", "scopes: {nested: 1}"],
    ids=["scalar-int", "bare-str", "mapping"],
)
def test_non_list_scopes_episode_does_not_crash_reads(
    episode_store: EpisodeStore, scopes_line: str
) -> None:
    """`scopes: 5` is well-formed YAML, so the frontmatter boundary
    accepts it — the parse then died at the bare `list(...)` coercion
    with TypeError, OUTSIDE the (ValueError, KeyError, OSError) skip set
    `list_by_session` catches, so a SINGLE such file (hand-edited or
    written by a buggy client) crashed the whole episode read surface:
    episode_handoff on the /loop iteration-entry hot path,
    episode_search, episode_promote, list_by_swarm. Same defect class
    the store side fixed for memories (`scopes: 5` →
    `list(meta["scopes"])`). The load now degrades a non-list `scopes`
    to [] with body/takeaway preserved, mirroring the defensive
    `is_floor` / `swarm_id` coercions. The bare-str shape is included
    because `list("banana")` didn't crash — it silently exploded into
    per-character garbage scopes."""
    episode_store.write(session_id="sess_aaaa1111", body="well-formed sibling")
    episode_store.write(
        session_id="sess_aaaa1111", body="bad scopes one", takeaway="kept takeaway"
    )

    target = next(
        p
        for p in episode_store._iter_session_paths("sess_aaaa1111")
        if "bad scopes one" in p.read_text(encoding="utf-8")
    )
    # `write` omits the `scopes` key when empty, so inject the
    # adversarial line right after the opening `---` (no duplicate key).
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, scopes_line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Must not raise; BOTH rows return — the malformed one with its
    # scopes dropped but body/takeaway intact (scopes are advisory
    # tags, not identity).
    eps = episode_store.list_by_session("sess_aaaa1111")
    assert {e.body.strip() for e in eps} == {"well-formed sibling", "bad scopes one"}
    bad = next(e for e in eps if e.body.strip() == "bad scopes one")
    assert bad.scopes == []
    assert bad.takeaway == "kept takeaway"
    # The swarm fan-in walks every session through the same parser —
    # it must survive the malformed file too.
    assert episode_store.list_by_swarm("sess_nonesuch") == []


def test_numeric_scope_list_entry_coerces_to_str(
    episode_store: EpisodeStore,
) -> None:
    """A list-shaped `scopes` with a numeric entry (`scopes: [5, tools]`)
    used to trip the Episode model's list[str] validation (pydantic does
    not coerce int → str), silently dropping the whole row via the
    ValueError skip path. Elements are now str()-coerced — the same
    idiom as the `swarm_id` guard — so the row loads with the tag's
    string form."""
    episode_store.write(session_id="sess_aaaa1111", body="numeric scope one")
    target = next(iter(episode_store._iter_session_paths("sess_aaaa1111")))
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "scopes: [5, tools]")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    eps = episode_store.list_by_session("sess_aaaa1111")
    assert len(eps) == 1
    assert eps[0].scopes == ["5", "tools"]


def test_scalar_takeaway_episode_coerced_not_dropped(
    episode_store: EpisodeStore,
) -> None:
    """Sibling guard to the scopes coercion: a hand-edited numeric
    `takeaway: 7` used to trip the model's `str | None` validation and
    silently drop the whole row via the ValueError skip path. The load
    now str()-coerces it — same idiom as `swarm_id` — preserving the
    row and its body."""
    episode_store.write(session_id="sess_aaaa1111", body="numeric takeaway one")
    target = next(iter(episode_store._iter_session_paths("sess_aaaa1111")))
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "takeaway: 7")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    eps = episode_store.list_by_session("sess_aaaa1111")
    assert len(eps) == 1
    assert eps[0].takeaway == "7"
    assert eps[0].body.strip() == "numeric takeaway one"


async def _call_episode_tool(server: Any, name: str, **kwargs: Any) -> Any:
    """Minimal mirror of test_server.py's `_call` + `_unwrap` for the
    two handler-surface pins below — kept local so this module doesn't
    import the whole handler test suite."""
    content, structured = await server.call_tool(name, kwargs)
    res = structured
    if res is None and content and hasattr(content[0], "text"):
        res = json.loads(content[0].text)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _corrupt_scopes_to_scalar(memory_dir: Path, body_marker: str) -> None:
    """Rewrite the frontmatter of the episode whose body contains
    `body_marker` so its `scopes` value is the scalar `5`. Handler-
    written episodes carry an auto-defaulted (block-style, multi-line)
    `scopes` key, so a line-insert would create a duplicate key — the
    frontmatter round-trip replaces the value shape exactly."""
    from bettermemory import _frontmatter as frontmatter

    target = next(
        p
        for p in (memory_dir / "episodes").rglob("*.md")
        if body_marker in p.read_text(encoding="utf-8")
    )
    post = frontmatter.load(target)
    post.metadata["scopes"] = 5
    target.write_text(frontmatter.dumps(post), encoding="utf-8")


async def test_scalar_scopes_episode_does_not_crash_episode_handoff(
    memory_dir: Path,
) -> None:
    """End-to-end pin on the /loop iteration-entry surface: ONE
    scalar-scopes episode file in the prior session used to raise
    TypeError straight through the `episode_handoff` MCP tool (its
    `list_by_session` guard catches only ValueError). Handler-level
    tests generally live in test_server.py; this pin sits beside the
    storage-layer adversarial-frontmatter family that shares its
    fixture shape."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call_episode_tool(
        server_a, "episode_write", body="clean iteration", takeaway="clean takeaway"
    )
    await _call_episode_tool(
        server_a, "episode_write", body="corrupted iteration", takeaway="doomed scopes"
    )
    _corrupt_scopes_to_scalar(memory_dir, "corrupted iteration")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    res = await _call_episode_tool(server_b, "episode_handoff")

    assert res["prior_session_id"] is not None
    # Both episodes surface: the malformed one degrades to scopes=[]
    # with its takeaway preserved instead of crashing the handoff.
    takeaways = {e["takeaway"] for e in res["episodes"]}
    assert takeaways == {"clean takeaway", "doomed scopes"}
    corrupted = next(e for e in res["episodes"] if e["takeaway"] == "doomed scopes")
    assert corrupted["scopes"] == []


async def test_scalar_scopes_episode_does_not_crash_episode_search(
    memory_dir: Path,
) -> None:
    """Same adversarial fixture as the handoff pin, on the other
    crash surface the parser feeds: the `episode_search` bare walk
    (its per-session guard also catches only ValueError, so the
    TypeError escaped to the MCP caller)."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server_a = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call_episode_tool(
        server_a, "episode_write", body="clean iteration", takeaway="clean takeaway"
    )
    await _call_episode_tool(
        server_a, "episode_write", body="corrupted iteration", takeaway="doomed scopes"
    )
    _corrupt_scopes_to_scalar(memory_dir, "corrupted iteration")

    server_b = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    rows = await _call_episode_tool(server_b, "episode_search")

    takeaways = {r["takeaway"] for r in rows}
    assert takeaways == {"clean takeaway", "doomed scopes"}
    corrupted = next(r for r in rows if r["takeaway"] == "doomed scopes")
    assert corrupted["scopes"] == []


def test_list_by_swarm_fans_in_across_sessions(
    episode_store: EpisodeStore,
) -> None:
    """The multi-agent fan-in: episodes written under different
    sub-agent session directories but tagged with the same coordinator
    swarm_id are gathered together, globally oldest-first."""
    coord = "sess_coordaaa"
    a = episode_store.write(
        session_id="sess_agent1", body="agent 1 takeaway", swarm_id=coord
    )
    time.sleep(0.002)
    b = episode_store.write(
        session_id="sess_agent2", body="agent 2 takeaway", swarm_id=coord
    )
    time.sleep(0.002)
    c = episode_store.write(
        session_id="sess_agent1", body="agent 1 second", swarm_id=coord
    )
    fan_in = episode_store.list_by_swarm(coord)
    # Globally oldest-first across both sub-agent session directories.
    assert [e.id for e in fan_in] == [a.id, b.id, c.id]
    assert {e.session_id for e in fan_in} == {"sess_agent1", "sess_agent2"}


def test_list_by_swarm_excludes_other_cohorts_and_legacy(
    episode_store: EpisodeStore,
) -> None:
    """Fan-in returns only the matching cohort — episodes with a
    different swarm_id, or none at all, are excluded."""
    episode_store.write(session_id="sess_agent1", body="mine", swarm_id="sess_coordaaa")
    episode_store.write(
        session_id="sess_agent2", body="other swarm", swarm_id="sess_coordbbb"
    )
    episode_store.write(session_id="sess_agent3", body="legacy / no swarm")
    fan_in = episode_store.list_by_swarm("sess_coordaaa")
    assert len(fan_in) == 1
    assert fan_in[0].body.strip() == "mine"


def test_list_by_swarm_unknown_id_returns_empty(
    episode_store: EpisodeStore,
) -> None:
    """An unknown swarm_id matches nothing (equality match, never used
    as a path) — empty result, not a raise."""
    episode_store.write(session_id="sess_agent1", body="x", swarm_id="sess_real")
    assert episode_store.list_by_swarm("sess_nonesuch") == []


def test_invalid_swarm_id_rejected(episode_store: EpisodeStore) -> None:
    """swarm_id is constrained to the filesystem-safe id charset and a
    length cap so a runaway / unsafe value can't reach the frontmatter."""
    with pytest.raises(ValueError, match="swarm_id"):
        episode_store.write(
            session_id="sess_aaaa1111", body="x", swarm_id="bad/id with spaces!"
        )
    with pytest.raises(ValueError, match="swarm_id"):
        episode_store.write(session_id="sess_aaaa1111", body="x", swarm_id="x" * 200)
    with pytest.raises(ValueError, match="swarm_id"):
        episode_store.write(session_id="sess_aaaa1111", body="x", swarm_id="")


def test_list_by_session_sorts_oldest_first(episode_store: EpisodeStore) -> None:
    """ULIDs sort lexically by creation timestamp; list_by_session
    surfaces them oldest first so a handoff caller can take the most
    recent N from the tail."""
    a = episode_store.write(session_id="sess_aaaa1111", body="first")
    time.sleep(0.005)  # ms-resolution ULID needs a beat to bump
    b = episode_store.write(session_id="sess_aaaa1111", body="second")
    eps = episode_store.list_by_session("sess_aaaa1111")
    assert [e.id for e in eps] == [a.id, b.id]


def test_rejects_traversal_in_session_id(episode_store: EpisodeStore) -> None:
    """A session_id containing `/` or `..` would let a hostile caller
    escape the episodes subtree. Reject at the storage boundary."""
    with pytest.raises(ValueError):
        episode_store.write(session_id="../etc/passwd", body="x")
    with pytest.raises(ValueError):
        episode_store.write(session_id="a/b", body="x")


def test_prune_drops_sessions_past_ttl(episode_store: EpisodeStore) -> None:
    """Sessions whose newest episode mtime is past the TTL get rmtree'd."""
    episode_store.write(session_id="sess_old", body="ancient")
    episode_store.write(session_id="sess_new", body="fresh")

    # Backdate the "old" session's files past the TTL.
    old_dir = episode_store.episodes_dir / "sess_old"
    for f in old_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)  # 40 days ago
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_old" in pruned
    assert "sess_new" not in pruned
    assert not (episode_store.episodes_dir / "sess_old").exists()
    assert (episode_store.episodes_dir / "sess_new").exists()


def test_prune_respects_keep_session_id(episode_store: EpisodeStore) -> None:
    """The active session's dir is exempt from pruning even if its
    newest mtime is past the TTL — a session that paused for >30d
    shouldn't lose its own scratch when it resumes writing."""
    episode_store.write(session_id="sess_active", body="paused-then-resumed")
    active_dir = episode_store.episodes_dir / "sess_active"
    for f in active_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(
        ttl_days=30, keep_session_id="sess_active"
    )
    assert "sess_active" not in pruned
    assert (episode_store.episodes_dir / "sess_active").exists()


def test_prune_zero_ttl_is_noop(episode_store: EpisodeStore) -> None:
    """`ttl_days <= 0` disables the prune entirely. Used by callers
    that want to manage retention explicitly via the CLI rather than
    on every write."""
    episode_store.write(session_id="sess_aaaa1111", body="any")
    pruned = episode_store.prune_old_sessions(ttl_days=0)
    assert pruned == []
    assert (episode_store.episodes_dir / "sess_aaaa1111").exists()


def test_prune_locked_recheck_skips_race_winner(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-MCP race: process A's `prune_old_sessions` does an
    unlocked stat, decides a session dir is stale, then acquires the
    per-session flock. Between the stat and the flock-acquire,
    process B's `episode_write` slipped in and rename'd a fresh
    `<ulid>.md` into the dir — mtime is now well past the cutoff
    and the dir is logically live again. The recheck under the lock
    must see the fresh mtime and skip the `shutil.rmtree`, otherwise
    A wipes B's just-committed episode.

    Pin by stubbing `_newest_mtime_in_dir` to return stale on the
    first call (the unlocked walk) and fresh on the second (the
    locked recheck), reproducing the exact race window
    deterministically without threading."""
    import bettermemory.episodes as episodes_mod
    import os as _os

    episode_store.write(session_id="sess_raced", body="real file")
    raced_dir = episode_store.episodes_dir / "sess_raced"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in raced_dir.iterdir():
        _os.utime(f, (past, past))

    cutoff_epoch = time.time() - 30 * 24 * 60 * 60
    real_newest = episodes_mod._newest_mtime_in_dir
    calls: list[Path] = []

    def staggered_newest(path: Path) -> float | None:
        calls.append(path)
        if path == raced_dir:
            # First call (unlocked walk): return stale so prune
            # decides to delete. Second call (locked recheck):
            # return fresh so prune skips. Other paths get the
            # real implementation untouched.
            if calls.count(raced_dir) == 1:
                return cutoff_epoch - 1.0
            return time.time()
        return real_newest(path)

    monkeypatch.setattr(episodes_mod, "_newest_mtime_in_dir", staggered_newest)

    pruned = episode_store.prune_old_sessions(ttl_days=30)

    assert "sess_raced" not in pruned, (
        f"prune deleted a session whose locked-recheck saw a fresh mtime: {pruned}"
    )
    assert raced_dir.exists(), (
        "session_dir was rmtree'd despite the locked-recheck seeing fresh mtime"
    )
    # Both walks must have happened — the unlocked stat and the
    # locked recheck. A single call would mean the recheck was
    # skipped and the race window is still open.
    assert calls.count(raced_dir) == 2, (
        f"expected unlocked stat + locked recheck (2 calls on raced_dir), "
        f"saw {calls.count(raced_dir)}"
    )


def test_prune_blocks_while_writer_holds_flock(
    episode_store: EpisodeStore,
) -> None:
    """Direct pin on the lock-acquire semantics: while the writer
    side holds the per-session flock, a concurrent `prune_old_sessions`
    must BLOCK on the flock-acquire (not race past it and rmtree
    the session dir). Use a thread to hold the writer's flock,
    launch the prune in a second thread, and assert the prune
    cannot complete until the writer releases."""
    import os as _os
    import threading
    from bettermemory._fsutil import flock_excl

    episode_store.write(session_id="sess_held", body="seed")
    held_dir = episode_store.episodes_dir / "sess_held"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in held_dir.iterdir():
        _os.utime(f, (past, past))

    lock_anchor = episode_store.episodes_dir / ".session-sess_held"
    writer_holding = threading.Event()
    writer_release = threading.Event()

    def hold_writer_lock() -> None:
        with flock_excl(lock_anchor):
            writer_holding.set()
            writer_release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    try:
        writer_holding.wait(timeout=5.0)
        assert writer_holding.is_set()

        prune_done = threading.Event()
        prune_result: list[list[str]] = []

        def background_prune() -> None:
            prune_result.append(episode_store.prune_old_sessions(ttl_days=30))
            prune_done.set()

        pt = threading.Thread(target=background_prune)
        pt.start()
        # Give the prune a generous window to (incorrectly) race
        # through and rmtree the held dir. If it completes here,
        # the flock isn't serialising — that's the bug we're
        # protecting against.
        time.sleep(0.1)
        assert not prune_done.is_set(), (
            "prune raced through the per-session flock — writer is not "
            "serialising the rmtree window"
        )
        assert held_dir.exists(), "prune rmtree'd the dir before lock release"

        writer_release.set()
        pt.join(timeout=5.0)
        assert prune_done.is_set()
        # After the writer released, prune acquires the lock, rechecks
        # mtime — which is still past cutoff (we backdated the seed
        # file, no writer bumped it) — and proceeds with the rmtree.
        assert "sess_held" in prune_result[0]
        assert not held_dir.exists()
    finally:
        writer_release.set()
        holder.join(timeout=5.0)


def test_prune_still_deletes_truly_stale_dirs(
    episode_store: EpisodeStore,
) -> None:
    """Regression pin: the flock + recheck logic must NOT break the
    base case — a stale session dir with no concurrent writer still
    gets deleted on the next prune pass. Without this pin a future
    refactor could leave the recheck always-truthy and silently turn
    `prune_old_sessions` into a no-op.

    E1 / A3-13: the past-cutoff branch also unlinks the sidecar
    lockfile while still holding the flock. Pre-fix the lockfile was
    deliberately persisted to preserve flock-inode identity — but for
    a past-TTL session there are no live writers, so the inode-
    identity race is closed by construction (the only concurrent
    acquirers can be peer prunes, and both prunes reach the same
    "session gone, lockfile gone" end state). Without the unlink each
    fresh /loop tick (new process => new session_id) leaks a 0-byte
    file, and at N≈10⁵ ticks `iterdir(episodes_dir)` dominates
    handoff latency."""
    import os as _os

    episode_store.write(session_id="sess_truly_stale", body="ancient")
    stale_dir = episode_store.episodes_dir / "sess_truly_stale"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in stale_dir.iterdir():
        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_truly_stale" in pruned
    assert not stale_dir.exists()
    lock_path = episode_store.episodes_dir / ".session-sess_truly_stale.lock"
    assert not lock_path.exists(), (
        "past-TTL prune must unlink the sidecar lockfile; otherwise "
        "each fresh /loop tick (new session_id) leaks a 0-byte file "
        "and iterdir(episodes_dir) slows handoff latency at N≈10⁵."
    )


def test_prune_treats_vanished_dir_as_success(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer-prune race: another bettermemory process pruning the
    same store rmtree'd the session dir between OUR unlocked mtime
    stat and OUR flock acquisition. `prune_old_sessions` must treat
    the resulting `FileNotFoundError` from `shutil.rmtree` as
    "already gone, success" and append the session to `pruned`, not
    swallow it as a generic OSError and lose the bookkeeping."""
    import os as _os
    import shutil as _shutil

    episode_store.write(session_id="sess_peer_raced", body="will vanish")
    target = episode_store.episodes_dir / "sess_peer_raced"
    past = time.time() - (40 * 24 * 60 * 60)

    for f in target.iterdir():
        _os.utime(f, (past, past))

    # Patch `shutil.rmtree` (the binding the episodes module imported
    # via `import shutil`) to wipe the dir AND raise FileNotFoundError
    # — simulating a peer prune that won the race.
    real_rmtree = _shutil.rmtree

    def racing_rmtree(path: Path | str) -> None:
        real_rmtree(path)
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(_shutil, "rmtree", racing_rmtree)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_peer_raced" in pruned, (
        "prune should record a peer-raced rmtree as a successful prune"
    )
    assert not target.exists()


def test_writer_progresses_while_prune_waits_on_lock(
    episode_store: EpisodeStore,
) -> None:
    """Reverse-direction race: a `prune_old_sessions` call holds the
    per-session flock (mtime-recheck phase) while the active session
    writes a new episode. The writer must block on the flock, then
    proceed normally when prune releases — NOT fail, deadlock, or
    silently skip the write.

    Pins that the lock is released cleanly by prune even on the
    skip-delete branch (the locked recheck sees fresh mtime), so
    the writer can immediately complete."""
    import threading
    from bettermemory._fsutil import flock_excl

    episode_store.write(session_id="sess_active", body="seed")

    lock_anchor = episode_store.episodes_dir / ".session-sess_active"
    prune_holding = threading.Event()
    prune_release = threading.Event()

    def prune_holds_lock() -> None:
        """Hold the per-session flock for a beat to simulate the
        prune sitting inside its locked recheck → rmtree section."""
        with flock_excl(lock_anchor):
            prune_holding.set()
            prune_release.wait(timeout=5.0)

    t = threading.Thread(target=prune_holds_lock)
    t.start()
    try:
        prune_holding.wait(timeout=5.0)
        assert prune_holding.is_set()

        # Writer must block on the flock. Launch the write in a
        # background thread so we can assert it's blocked, then
        # release the lock and assert the write completes.
        writer_done = threading.Event()
        write_error: list[BaseException] = []

        def background_write() -> None:
            try:
                episode_store.write(session_id="sess_active", body="post-lock episode")
            except BaseException as exc:  # noqa: BLE001
                write_error.append(exc)
            finally:
                writer_done.set()

        wt = threading.Thread(target=background_write)
        wt.start()
        # The writer should NOT complete while prune holds the lock.
        # Give it a brief window to (incorrectly) sneak through.
        time.sleep(0.1)
        assert not writer_done.is_set(), (
            "writer raced through the per-session flock — prune lock is "
            "not serialising writes"
        )

        prune_release.set()
        wt.join(timeout=5.0)
        assert writer_done.is_set(), "writer never completed after lock release"
        assert not write_error, f"writer raised: {write_error[0]!r}"

        eps = episode_store.list_by_session("sess_active")
        bodies = [e.body.strip() for e in eps]
        assert "post-lock episode" in bodies, (
            "writer's episode did not land after the prune lock released"
        )
    finally:
        prune_release.set()
        t.join(timeout=5.0)


def test_write_is_atomic_and_durable(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path must (a) leave no `.tmp` artifacts behind,
    (b) chmod the final file to 0o600 on POSIX, and (c) call all three
    durability primitives expected on a first-ever write:
    `fsync_file` on the open fd, `fsync_dir` on the leaf parent
    (session_dir), AND the dirent-flush ceremony introduced for
    audit-3 A3-05 — `fsync_dir` on `root` (because episodes_dir was
    just created) and `fsync_dir` on `episodes_dir` (because
    session_dir was just created). Pre-fix `_write_path` used
    `Path.write_text` + `os.replace` with no fsyncs, so power-loss
    between rename and kernel flush could leave a zero-byte
    `<ulid>.md` at the target."""
    fsync_file_calls: list[int] = []
    fsync_dir_calls: list[Path] = []

    import bettermemory.episodes as episodes_mod
    from bettermemory import _fsutil

    def spy_fsync_file(fd: int) -> None:
        fsync_file_calls.append(fd)

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    # `_write_path` now delegates the file write to `atomic_write_bytes`,
    # so the per-write `fsync_file` and the session_dir `fsync_dir` fire
    # from `_fsutil`'s bindings; the root/episodes_dir dirent fsyncs still
    # fire from `episodes`. Patch both modules into the same spies so the
    # full ordered ceremony stays observable.
    monkeypatch.setattr(_fsutil, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)
    monkeypatch.setattr(_fsutil, "fsync_dir", spy_fsync_dir)

    ep = episode_store.write(session_id="sess_aaaa1111", body="durable")

    session_dir = episode_store.episodes_dir / "sess_aaaa1111"
    target = session_dir / f"{ep.id}.md"
    assert target.is_file()

    # No `.tmp` artifacts left behind after a successful write.
    stragglers = [
        p for p in session_dir.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name
    ]
    assert stragglers == [], f"unexpected tmp artifacts: {stragglers}"

    # fsync_file: one call on the per-write tmp fd before rename.
    assert len(fsync_file_calls) == 1

    # fsync_dir: three calls on a first-ever write to a fresh store.
    # Order matches the durability ceremony:
    #   1. `root` after `episodes_dir.mkdir` (audit-3 A3-05) — the
    #      new `episodes/` dirent in root.
    #   2. `session_dir` from `_write_path`'s rename ceremony — the
    #      `<ulid>.md` rename.
    #   3. `episodes_dir` after `_write_path` returns inside the flock
    #      (audit-3 A3-05) — the new session_dir dirent.
    assert fsync_dir_calls == [
        episode_store.root,
        session_dir,
        episode_store.episodes_dir,
    ], f"expected 3-stage first-write fsync_dir ceremony, got: {fsync_dir_calls}"

    # 0o600 mode on POSIX. Windows has no mode bits, so skip there.
    if sys.platform != "win32":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_excluded_from_memory_store_iteration(tmp_path: Path) -> None:
    """Episodes must not appear in `Store.load_all` — episodes live in
    a sibling subdirectory (`episodes/`), so the memory store's
    `_iter_active_paths` (which uses `iterdir` on the root) should
    skip directory entries naturally. This pin catches a future
    refactor that accidentally recurses or globs."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    ep_store = EpisodeStore(tmp_path)
    ep_store.write(session_id="sess_aaaa1111", body="not a memory")

    memories = store.load_all()
    assert memories == []


def test_prune_empty_dir_holds_flock_while_writer_runs(
    episode_store: EpisodeStore,
) -> None:
    """Empty-dir branch of `prune_old_sessions` must respect the same
    per-session flock the past-cutoff branch does. Symmetric to
    `test_writer_progresses_while_prune_waits_on_lock` and
    `test_prune_blocks_while_writer_holds_flock` but for the empty-dir
    branch: a writer holds the per-session flock with an empty
    session_dir in place; the concurrent prune must BLOCK on the
    flock-acquire (not race past it and rmdir the dir that the writer
    just `mkdir`'d, about to land a tempfile into).
    """
    import threading
    from bettermemory._fsutil import flock_excl

    # Set up an empty session_dir without writing any episode — this
    # is the exact shape the bug targets (writer has mkdir'd but not
    # yet rename'd its tempfile into place).
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    empty_dir = episode_store.episodes_dir / "sess_empty"
    empty_dir.mkdir(mode=0o700)

    lock_anchor = episode_store.episodes_dir / ".session-sess_empty"
    writer_holding = threading.Event()
    writer_release = threading.Event()

    def hold_writer_lock() -> None:
        with flock_excl(lock_anchor):
            writer_holding.set()
            writer_release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    try:
        writer_holding.wait(timeout=5.0)
        assert writer_holding.is_set()

        prune_done = threading.Event()
        prune_result: list[list[str]] = []

        def background_prune() -> None:
            prune_result.append(episode_store.prune_old_sessions(ttl_days=30))
            prune_done.set()

        pt = threading.Thread(target=background_prune)
        pt.start()
        # Give the prune a generous window to (incorrectly) race
        # through and rmdir the held dir. If it completes here, the
        # flock isn't serialising the empty-dir branch — that's the
        # bug we're protecting against.
        time.sleep(0.1)
        assert not prune_done.is_set(), (
            "prune raced through the per-session flock on the empty-dir "
            "branch — writer's mkdir+tempfile window is not serialised"
        )
        assert empty_dir.exists(), "prune rmdir'd the empty dir before lock release"

        writer_release.set()
        pt.join(timeout=5.0)
        assert prune_done.is_set()
        # After the writer released, the prune acquires the lock, the
        # recheck still sees an empty dir (we never landed a file),
        # and the rmdir succeeds.
        assert "sess_empty" in prune_result[0]
        assert not empty_dir.exists()
    finally:
        writer_release.set()
        holder.join(timeout=5.0)


def test_prune_empty_dir_recheck_skips_when_writer_landed_after_unlocked_walk(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-MCP race on the empty-dir branch: process A's
    `prune_old_sessions` does an unlocked stat on an empty session_dir,
    decides to rmdir. Between the stat and the flock-acquire, process
    B's `episode_write` slipped in, completed its mkdir + tempfile
    rename — the dir is no longer empty. The locked recheck must see
    the fresh mtime and SKIP the rmdir, otherwise A wipes a directory
    that's logically live.

    Pin by stubbing `_newest_mtime_in_dir` to return None on the first
    call (the unlocked walk) and a fresh mtime on the second call
    (the locked recheck), reproducing the exact race deterministically
    without threading."""
    import bettermemory.episodes as episodes_mod

    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    raced_dir = episode_store.episodes_dir / "sess_emptied_then_filled"
    raced_dir.mkdir(mode=0o700)

    real_newest = episodes_mod._newest_mtime_in_dir
    calls: list[Path] = []

    def staggered_newest(path: Path) -> float | None:
        calls.append(path)
        if path == raced_dir:
            # First call (unlocked walk): return None so prune takes
            # the empty-dir branch. Second call (locked recheck):
            # return a fresh mtime so prune skips the rmdir. Other
            # paths get the real implementation untouched.
            if calls.count(raced_dir) == 1:
                return None
            return time.time()
        return real_newest(path)

    monkeypatch.setattr(episodes_mod, "_newest_mtime_in_dir", staggered_newest)

    pruned = episode_store.prune_old_sessions(ttl_days=30)

    assert "sess_emptied_then_filled" not in pruned, (
        f"prune deleted an empty dir whose locked-recheck saw a fresh "
        f"mtime (writer landed during the race window): {pruned}"
    )
    assert raced_dir.exists(), (
        "session_dir was rmdir'd despite the locked-recheck seeing fresh mtime"
    )
    # Both walks must have happened — the unlocked stat and the
    # locked recheck. A single call would mean the recheck was
    # skipped and the race window is still open.
    assert calls.count(raced_dir) == 2, (
        f"expected unlocked stat + locked recheck (2 calls on raced_dir), "
        f"saw {calls.count(raced_dir)}"
    )


def test_prune_empty_dir_still_deletes_truly_empty_session(
    episode_store: EpisodeStore,
) -> None:
    """Regression pin: the flock + recheck logic on the empty-dir
    branch must NOT break the base case — a session_dir with no
    files and no concurrent writer still gets deleted on the next
    prune pass. Without this pin a future refactor could leave the
    locked recheck always-truthy and silently turn the empty-dir
    branch into a no-op."""
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    empty_dir = episode_store.episodes_dir / "sess_truly_empty"
    empty_dir.mkdir(mode=0o700)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_truly_empty" in pruned
    assert not empty_dir.exists()


def test_prune_empty_dir_treats_vanished_dir_as_success(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer-prune race on the empty-dir branch: another bettermemory
    process pruning the same store rmdir'd the session dir between
    OUR unlocked mtime stat and OUR flock acquisition. The empty-dir
    branch must treat the resulting `FileNotFoundError` from `rmdir`
    as "already gone, success" and append the session to `pruned`,
    not swallow it as a generic OSError and lose the bookkeeping.

    Patch `Path.rmdir` to raise FileNotFoundError, simulating a peer
    prune that won the race."""
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    target = episode_store.episodes_dir / "sess_peer_raced_empty"
    target.mkdir(mode=0o700)

    real_rmdir = Path.rmdir
    raised = {"done": False}

    def racing_rmdir(self: Path) -> None:
        # Actually remove the dir (so the post-condition holds), then
        # raise FileNotFoundError on the first call against our target
        # to simulate a peer prune that won. Other rmdir callers
        # (none in this test, but defensive) get the real behaviour.
        if self == target and not raised["done"]:
            real_rmdir(self)
            raised["done"] = True
            raise FileNotFoundError(2, "No such file or directory", str(self))
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_peer_raced_empty" in pruned, (
        "empty-dir branch should record a peer-raced rmdir as a successful prune"
    )
    assert not target.exists()


def test_prune_past_cutoff_fsyncs_episodes_dir(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit-3 A3-04: after `shutil.rmtree(session_dir)` in the past-
    cutoff prune branch, the parent `episodes_dir`'s dirent listing has
    changed (the session_dir entry is gone). Without an explicit
    `fsync_dir(episodes_dir)`, that metadata change lives in the
    parent's page-cache until a natural flush — on power loss between
    rmtree returning and the kernel persisting dirty pages, the kernel
    can present a recovered `episodes_dir` that still lists the
    deleted session as a phantom entry.
    """
    import os as _os
    import bettermemory.episodes as episodes_mod

    episode_store.write(session_id="sess_past_cutoff_fsync", body="ancient")
    stale_dir = episode_store.episodes_dir / "sess_past_cutoff_fsync"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in stale_dir.iterdir():
        _os.utime(f, (past, past))

    # Spy on the fsync_dir binding the episodes module imports; the
    # write path that created the seed episode above will have called
    # fsync_dir on session_dir (for the rename) AND on episodes_dir
    # (first-create dirent flush, audit-3 A3-05). Reset the spy AFTER
    # the seed write so the assertion below only sees the prune's
    # fsync.
    fsync_dir_calls: list[Path] = []

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_past_cutoff_fsync" in pruned
    assert not stale_dir.exists()

    # The past-cutoff branch must fsync episodes_dir AFTER rmtree to
    # make the dropped dirent durable. A missing call would mean a
    # crash between rmtree returning and the next dir-fsync (which
    # only happens on the next write to a fresh session_id, hours or
    # days away) could resurrect a phantom dirent for the deleted
    # session.
    assert episode_store.episodes_dir in fsync_dir_calls, (
        f"prune past-cutoff branch must fsync_dir(episodes_dir) after "
        f"rmtree; saw calls only on: {fsync_dir_calls}"
    )


def test_prune_empty_dir_fsyncs_episodes_dir(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit-3 A3-04: symmetric to the past-cutoff test above but for
    the empty-dir branch. `Path.rmdir(session_dir)` drops the session
    dirent from `episodes_dir`; without an explicit `fsync_dir` the
    metadata change can be lost on crash."""
    import bettermemory.episodes as episodes_mod

    # Empty session_dir — no files inside, so the prune takes the
    # empty-dir branch (newest_mtime is None).
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    empty_dir = episode_store.episodes_dir / "sess_empty_fsync"
    empty_dir.mkdir(mode=0o700)

    fsync_dir_calls: list[Path] = []

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_empty_fsync" in pruned
    assert not empty_dir.exists()

    assert episode_store.episodes_dir in fsync_dir_calls, (
        f"prune empty-dir branch must fsync_dir(episodes_dir) after "
        f"rmdir; saw calls only on: {fsync_dir_calls}"
    )


def test_episode_write_fsyncs_episodes_dir_on_first_create(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit-3 A3-05: the first write to a fresh session_id creates a
    new session_dir under `episodes_dir`. `_write_path` fsyncs the
    leaf parent (session_dir) so the file rename is durable, but the
    NEW dirent for session_dir itself in `episodes_dir` needs its own
    dir-fsync — without it, a crash after the very first write leaves
    the file + session_dir on disk but unreachable via path traversal
    (the dirent is missing from the recovered episodes_dir listing).
    Subsequent writes into the same session_dir don't re-trigger the
    fsync — the dirent already exists.
    """
    import bettermemory.episodes as episodes_mod
    from bettermemory import _fsutil

    fsync_dir_calls: list[Path] = []

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    # session_dir's fsync_dir now fires from the shared helper
    # (`atomic_write_bytes`); root and episodes_dir still fire from
    # `episodes`. Patch both bindings so the spy sees the whole sequence.
    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)
    monkeypatch.setattr(_fsutil, "fsync_dir", spy_fsync_dir)

    # First write: episodes_dir doesn't exist yet, session_dir doesn't
    # exist yet. We expect fsync_dir on:
    #  - self.root (because episodes_dir was just created)
    #  - session_dir (from _write_path's existing rename ceremony)
    #  - self.episodes_dir (because session_dir was just created — the
    #    fix this test pins)
    episode_store.write(session_id="sess_first_create_fsync", body="first")
    session_dir = episode_store.episodes_dir / "sess_first_create_fsync"

    assert episode_store.episodes_dir in fsync_dir_calls, (
        f"first write to a fresh session_id must fsync_dir(episodes_dir) "
        f"to persist the new session_dir dirent; saw: {fsync_dir_calls}"
    )
    assert session_dir in fsync_dir_calls, (
        f"_write_path's rename ceremony should still fsync_dir(session_dir); "
        f"saw: {fsync_dir_calls}"
    )

    # Second write into the SAME session_dir — the dirent already
    # exists, so no extra fsync_dir(episodes_dir) call. Only the
    # session_dir fsync from _write_path's rename should fire.
    fsync_dir_calls.clear()
    episode_store.write(session_id="sess_first_create_fsync", body="second")
    assert episode_store.episodes_dir not in fsync_dir_calls, (
        f"subsequent writes to an existing session_dir must NOT re-fsync "
        f"episodes_dir; saw: {fsync_dir_calls}"
    )
    assert session_dir in fsync_dir_calls, (
        f"_write_path should still fsync session_dir on every write; "
        f"saw: {fsync_dir_calls}"
    )


def test_prune_unlinks_lockfile_for_every_pruned_session(
    episode_store: EpisodeStore,
) -> None:
    """Leak-prevention pin (E1 / A3-13): after a TTL prune, zero
    `.session-*.lock` files remain in `episodes_dir`. Pre-E1 each
    fresh /loop tick (new process => new session_id) left a 0-byte
    lockfile behind on prune, and at N≈10⁵ ticks `iterdir()` over
    `episodes_dir` dominated handoff latency. This pin asserts the
    fix is unconditional across multiple sessions on a single prune
    pass.
    """
    import os as _os

    # Five distinct session_ids — bigger than 1 to catch a fix that
    # only handles the first session in the loop, smaller than
    # something that'd slow the test suite. Each write creates its
    # own sidecar lockfile.
    session_ids = [f"sess_leak_{i}" for i in range(5)]
    for sid in session_ids:
        episode_store.write(session_id=sid, body=f"body {sid}")

    # Sanity-check: pre-prune, every session has its own lockfile.
    lock_files_before = sorted(
        p.name
        for p in episode_store.episodes_dir.iterdir()
        if p.is_file() and p.name.startswith(".session-") and p.name.endswith(".lock")
    )
    assert len(lock_files_before) == 5, (
        f"expected one lockfile per session pre-prune; saw {lock_files_before}"
    )

    # Backdate every file in every session_dir past the TTL cutoff.
    past = time.time() - (40 * 24 * 60 * 60)
    for sid in session_ids:
        sd = episode_store.episodes_dir / sid
        for f in sd.iterdir():
            _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert set(pruned) == set(session_ids), (
        f"expected all 5 sessions pruned, got {pruned}"
    )

    # Post-prune: zero `.session-*.lock` files survive. iterdir cost
    # over `episodes_dir` returns to baseline (just the bare
    # `episodes/` if nothing else lives there).
    lock_files_after = [
        p.name
        for p in episode_store.episodes_dir.iterdir()
        if p.is_file() and p.name.startswith(".session-") and p.name.endswith(".lock")
    ]
    assert lock_files_after == [], (
        f"prune leaked lockfiles — N≈10⁵ ticks would fill episodes_dir; "
        f"survivors: {lock_files_after}"
    )


def test_prune_orphan_lockfile_swept_even_without_session_dir(
    episode_store: EpisodeStore,
) -> None:
    """Orphan-cleanup pin (E1 / A3-13): a `.session-*.lock` file
    whose corresponding session_dir doesn't exist (the pre-E1 leak
    pattern, or a peer-prune race that left a fresh inode behind)
    gets swept on the next prune pass. Without the orphan sweep,
    pre-E1 leaks would never be reclaimed unless every old session
    was independently re-pruned.

    The orphan-cleanup case is independent of any TTL-pruned session
    in the same call; we set up a store with NO past-TTL sessions
    and assert the orphan still gets cleaned up.
    """
    # Materialise `episodes_dir` (the prune is a no-op against a
    # non-existent dir) by writing one fresh, live session.
    episode_store.write(session_id="sess_live", body="not stale")

    # Pre-create an orphan lockfile mimicking the pre-E1 leak: a
    # `.session-<id>.lock` with no corresponding session_dir.
    orphan = episode_store.episodes_dir / ".session-sess_dead.lock"
    orphan.touch()
    assert orphan.exists()
    assert not (episode_store.episodes_dir / "sess_dead").exists()

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    # The live session is NOT pruned (mtime is fresh).
    assert "sess_live" not in pruned
    # The orphan lockfile got swept anyway by the cleanup-at-end pass.
    assert not orphan.exists(), (
        "orphan lockfile (no session_dir) should be swept by "
        "_cleanup_orphan_lockfiles at the end of prune_old_sessions"
    )


def test_prune_preserves_lockfile_for_live_session(
    episode_store: EpisodeStore,
) -> None:
    """Live-session protection pin (E1 / A3-13): a session whose
    `session_dir` exists with fresh mtime must keep its lockfile.
    The lockfile-cleanup logic must only fire for sessions whose
    `session_dir` is GONE — never for live sessions where a future
    writer might be racing for the flock.

    Specifically: a session that's been written-to recently (mtime
    < cutoff_age) and is still active. Without this pin, a future
    refactor could broaden the unlink to live sessions, breaking
    flock-inode identity for in-flight acquirers.
    """
    episode_store.write(session_id="sess_live_protected", body="recent write")
    live_dir = episode_store.episodes_dir / "sess_live_protected"
    lock_path = episode_store.episodes_dir / ".session-sess_live_protected.lock"
    assert live_dir.exists()
    assert lock_path.exists()

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_live_protected" not in pruned
    assert live_dir.exists(), "fresh session_dir must survive prune"
    assert lock_path.exists(), (
        "live session's lockfile must NOT be unlinked — a writer racing "
        "for the flock could end up on a different inode than a peer "
        "if the lockfile is recreated underneath it"
    )


def test_prune_empty_dir_unlinks_lockfile(
    episode_store: EpisodeStore,
) -> None:
    """Empty-dir branch of `prune_old_sessions` must unlink the
    sidecar lockfile too. Same lifecycle argument as the past-cutoff
    branch (E1 / A3-13): an empty session_dir past the unlocked walk
    has no live writer (the writer would have land its file by now,
    or the dir would still be empty because the writer is mid-mkdir
    behind the flock — in which case the locked recheck would have
    seen a fresh mtime and skipped).
    """
    # Set up an empty session_dir + its sidecar lockfile manually.
    # Easiest is to write once, delete the file, leaving an empty
    # session_dir + its lockfile (which `write` created when it
    # took the flock).
    ep = episode_store.write(session_id="sess_empty_lock", body="trash")
    empty_dir = episode_store.episodes_dir / "sess_empty_lock"
    (empty_dir / f"{ep.id}.md").unlink()
    assert empty_dir.exists()
    assert list(empty_dir.iterdir()) == []  # empty session_dir

    lock_path = episode_store.episodes_dir / ".session-sess_empty_lock.lock"
    assert lock_path.exists()

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_empty_lock" in pruned
    assert not empty_dir.exists()
    assert not lock_path.exists(), (
        "empty-dir prune branch must unlink the sidecar lockfile too — "
        "same lifecycle argument as past-cutoff (no live writers "
        "possible past the locked emptiness recheck)."
    )


def test_prune_lockfile_unlink_persists_via_fsync(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockfile unlink in the prune path must call `fsync_dir`
    on `episodes_dir` so the dropped dirent survives a crash — same
    durability gate audit-3 A3-04 covered for rmtree/rmdir. Without
    a post-unlink fsync, a crash between unlink returning and the
    next natural flush could resurrect the lockfile dirent, partially
    undoing the leak fix.
    """
    import os as _os
    import bettermemory.episodes as episodes_mod

    episode_store.write(session_id="sess_unlink_fsync", body="ancient")
    stale_dir = episode_store.episodes_dir / "sess_unlink_fsync"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in stale_dir.iterdir():
        _os.utime(f, (past, past))

    # Spy on fsync_dir AFTER the seed write — the seed write already
    # called fsync_dir for its own durability ceremony.
    fsync_dir_calls: list[Path] = []

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_unlink_fsync" in pruned

    # The prune must fsync_dir(episodes_dir) at least twice:
    #   1. After `shutil.rmtree(session_dir)` (existing A3-04).
    #   2. After `lock_anchor.unlink()` (the new E1 / A3-13 work).
    # We assert ≥2 calls on episodes_dir; an exact count would
    # over-constrain the test against future refactors that add more
    # fsync points (e.g. the orphan sweep's final fsync).
    episodes_dir_fsyncs = fsync_dir_calls.count(episode_store.episodes_dir)
    assert episodes_dir_fsyncs >= 2, (
        f"prune must fsync_dir(episodes_dir) twice on the past-cutoff "
        f"branch — once after rmtree, once after lockfile unlink. "
        f"Saw {episodes_dir_fsyncs} calls on episodes_dir; full "
        f"call list: {fsync_dir_calls}"
    )


def test_prune_peer_race_dual_process_cleans_both_dir_and_lockfile(
    tmp_path: Path,
) -> None:
    """Peer-prune race pin (E1 / A3-13, task #3): two processes
    converging on the same past-TTL session_dir + lockfile both
    return success, and neither leaves a lockfile orphan behind.

    The race window the unlink-inside-flock contract has to handle:
    process A has rmtree'd the session_dir, is about to unlink the
    lockfile and release. Process B is blocked on flock-acquire; once
    A releases, B's `_newest_mtime_in_dir` returns None and
    `session_dir.exists()` is False — B falls through to
    `_unlink_session_lockfile`, which is a no-op for the (rare) case
    of a fresh inode that A's unlink-then-release-then-O_CREAT might
    have created. Either way, the final state is: session_dir gone,
    lockfile gone.

    Uses `multiprocessing.spawn` workers to mirror the pattern in
    `tests/test_concurrency.py` — `spawn` is required so the second
    process opens a fresh fd on the lockfile inode rather than
    inheriting one from a fork.
    """
    import multiprocessing as mp
    import os as _os

    if sys.platform == "win32":
        pytest.skip("fcntl-based locking is POSIX-only; race only fires on POSIX")

    store = EpisodeStore(tmp_path)
    store.write(session_id="sess_race", body="ancient")
    raced_dir = store.episodes_dir / "sess_race"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in raced_dir.iterdir():
        _os.utime(f, (past, past))

    ctx = mp.get_context("spawn")
    with ctx.Pool(2) as pool:
        results = pool.map(_prune_worker, [str(tmp_path)] * 2)

    # Both workers must have completed without raising. The session
    # may appear in one or both `pruned` lists (the loser sees the
    # session_dir gone and falls through to "vanished — success").
    assert all(r is not None for r in results), (
        f"worker raised during peer-prune race: {results}"
    )
    combined = [s for r in results if r is not None for s in r]
    assert "sess_race" in combined, (
        f"at least one worker must record sess_race as pruned; saw {results}"
    )

    # End state: session_dir gone, lockfile gone.
    assert not raced_dir.exists()
    lock_path = store.episodes_dir / ".session-sess_race.lock"
    assert not lock_path.exists(), (
        "peer-prune race left an orphan lockfile — the contract "
        "guarantees both prunes converge on a clean end state"
    )


def _prune_worker(root: str) -> list[str] | None:
    """Module-level worker for the peer-prune race test.

    `mp.get_context("spawn")` requires module-level callables for
    pickling. Returns the prune's result list, or None on any error
    so the parent can surface a structured failure.
    """
    try:
        from bettermemory.episodes import EpisodeStore as _EpStore

        return _EpStore(Path(root)).prune_old_sessions(ttl_days=30)
    except BaseException:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# E2 — session-tag floor episodes (crash-recovery anchors)
# ---------------------------------------------------------------------------


def test_write_floor_creates_floor_episode(episode_store: EpisodeStore) -> None:
    """`write_floor` produces a discoverable Episode with `is_floor=True`,
    an empty takeaway, empty scopes, and the supplied origin. Reuses the
    same on-disk discipline `write` does (atomic rename, fsync, etc.)
    via the shared `_persist_episode`."""
    origin = Origin(
        cwd="/tmp/work",
        repo="https://github.com/example/repo",
        branch="main",
        worktree_root="/tmp/work",
    )
    ep = episode_store.write_floor(session_id="sess_floor_test", origin=origin)

    # The floor itself
    assert ep.is_floor is True
    assert ep.takeaway is None
    assert ep.scopes == []
    assert ep.origin is not None
    assert ep.origin.worktree_root == "/tmp/work"

    # Discoverable via list_by_session — the worktree-filter side of
    # episode_handoff relies on this.
    loaded = episode_store.list_by_session("sess_floor_test")
    assert len(loaded) == 1
    assert loaded[0].id == ep.id
    assert loaded[0].is_floor is True
    assert loaded[0].origin is not None
    assert loaded[0].origin.worktree_root == "/tmp/work"


def test_write_floor_persists_is_floor_in_frontmatter(
    episode_store: EpisodeStore,
) -> None:
    """The `is_floor` flag round-trips through YAML frontmatter — a
    floor written today and loaded tomorrow still reads as a floor.
    Non-floor episodes do NOT emit the key (size + back-compat: legacy
    readers ignore unknown keys, and omitting on the common path keeps
    the on-disk shape stable for real-takeaway episodes)."""
    floor = episode_store.write_floor(session_id="sess_persist_floor")
    real = episode_store.write(session_id="sess_persist_real", body="actual body")

    floor_path = episode_store.episodes_dir / "sess_persist_floor" / f"{floor.id}.md"
    real_path = episode_store.episodes_dir / "sess_persist_real" / f"{real.id}.md"
    floor_yaml = floor_path.read_text()
    real_yaml = real_path.read_text()

    # Floor frontmatter contains the key set to True.
    assert "is_floor: true" in floor_yaml.lower(), (
        f"floor frontmatter missing is_floor key: {floor_yaml!r}"
    )
    # Real episode frontmatter omits the key (default-False isn't
    # serialised — keeps the on-disk shape stable for non-floors).
    assert "is_floor" not in real_yaml, (
        f"non-floor episode should not emit is_floor key: {real_yaml!r}"
    )


def test_floor_loads_from_disk_without_is_floor_key_as_false(
    episode_store: EpisodeStore,
) -> None:
    """Back-compat regression: legacy episodes written before the
    `is_floor` field shipped have no `is_floor` key in their frontmatter.
    The loader must default to False, not raise."""
    # Write a real episode (no is_floor key in frontmatter).
    ep = episode_store.write(session_id="sess_legacy", body="legacy episode body")
    assert ep.is_floor is False

    loaded = episode_store.list_by_session("sess_legacy")
    assert len(loaded) == 1
    assert loaded[0].is_floor is False


def test_floor_and_real_coexist_in_same_session(
    episode_store: EpisodeStore,
) -> None:
    """A session can contain both a floor (written first by handoff at
    entry) and a real takeaway (written later by episode_write). Both
    appear in `list_by_session` so consumers that distinguish on the
    flag can branch correctly."""
    floor = episode_store.write_floor(session_id="sess_mixed")
    time.sleep(0.005)
    real = episode_store.write(
        session_id="sess_mixed",
        body="real body",
        takeaway="real takeaway",
    )

    loaded = episode_store.list_by_session("sess_mixed")
    assert len(loaded) == 2
    # Sorted oldest first — floor was written first.
    assert loaded[0].id == floor.id
    assert loaded[0].is_floor is True
    assert loaded[1].id == real.id
    assert loaded[1].is_floor is False
    assert loaded[1].takeaway == "real takeaway"


def test_write_floor_atomic_no_tmp_stragglers(episode_store: EpisodeStore) -> None:
    """`write_floor` shares the atomic-rename discipline `write` does
    via `_persist_episode`. After a successful floor write, no `.tmp`
    files survive in the session_dir."""
    episode_store.write_floor(session_id="sess_atomic_floor")
    session_dir = episode_store.episodes_dir / "sess_atomic_floor"
    stragglers = [
        p for p in session_dir.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name
    ]
    assert stragglers == [], f"unexpected tmp artifacts: {stragglers}"


@pytest.mark.parametrize("branch", ["empty_dir", "past_cutoff"])
def test_prune_unlinks_sidecar_after_flock_release_not_inside(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    """Cross-platform sidecar-unlink pin (regression for the in-lock
    unlink being DEAD on Windows; same root-cause class the 3.4.2
    store.py fix 40e71e4 addressed).

    `prune_old_sessions` used to call `_unlink_session_lockfile` while
    still INSIDE the `with flock_excl(...)` block, on both the empty-dir
    and past-cutoff branches. On Windows `msvcrt.locking` keeps the
    lockfile handle open for the whole `with` duration, so unlinking the
    still-open `.session-<id>.lock` raised `OSError` and was swallowed —
    the unlink never landed (it was masked only by the post-loop orphan
    sweep). `store.prune_tombstones` avoids this by deferring ALL sidecar
    unlinks to AFTER lock release; this test pins that `episodes.py` does
    the same.

    Deterministic, platform-independent pin: wrap the module's
    `flock_excl` to track nesting depth, and record the depth observed at
    the moment `_unlink_session_lockfile` is invoked. The OLD code calls
    it at depth 1 (still inside the flock); the FIXED code calls it at
    depth 0 (handle released). Also assert the sidecar is actually gone
    post-prune on both branches.
    """
    import os as _os
    import bettermemory.episodes as episodes_mod
    from bettermemory._fsutil import flock_excl as real_flock_excl

    if branch == "empty_dir":
        # Empty session_dir + its sidecar lockfile (write once, delete
        # the file so `write`'s flock-created lockfile survives).
        ep = episode_store.write(session_id="sess_defer", body="trash")
        sess_dir = episode_store.episodes_dir / "sess_defer"
        (sess_dir / f"{ep.id}.md").unlink()
        assert list(sess_dir.iterdir()) == []  # truly empty
    else:
        # Past-cutoff session: backdate its file past the TTL so the
        # past-cutoff branch (newest_mtime < cutoff) fires.
        episode_store.write(session_id="sess_defer", body="ancient")
        sess_dir = episode_store.episodes_dir / "sess_defer"
        past = time.time() - (40 * 24 * 60 * 60)
        for f in sess_dir.iterdir():
            _os.utime(f, (past, past))

    lock_path = episode_store.episodes_dir / ".session-sess_defer.lock"
    assert lock_path.exists()

    # Track flock nesting depth across the module's `flock_excl` binding.
    flock_depth = {"value": 0}

    from contextlib import contextmanager
    from typing import Iterator as _Iterator

    @contextmanager
    def tracking_flock(path: Path) -> _Iterator[None]:
        with real_flock_excl(path):
            flock_depth["value"] += 1
            try:
                yield
            finally:
                flock_depth["value"] -= 1

    monkeypatch.setattr(episodes_mod, "flock_excl", tracking_flock)

    # Record the flock depth at the moment the sidecar unlink runs.
    real_unlink = episodes_mod._unlink_session_lockfile
    observed_depths: list[int] = []

    def recording_unlink(
        episodes_dir: Path, lock_file: Path, session_dir: Path
    ) -> None:
        observed_depths.append(flock_depth["value"])
        real_unlink(episodes_dir, lock_file, session_dir)

    monkeypatch.setattr(episodes_mod, "_unlink_session_lockfile", recording_unlink)

    pruned = episode_store.prune_old_sessions(ttl_days=30)

    assert "sess_defer" in pruned
    assert not sess_dir.exists()
    # The unlink must have run exactly once for this session, and the
    # flock must already have been RELEASED (depth 0) when it ran.
    assert observed_depths == [0], (
        f"_unlink_session_lockfile ran while the per-session flock was "
        f"still held (depth {observed_depths}) — on Windows that in-lock "
        f"unlink raises against the open msvcrt handle and the sidecar "
        f"leaks. It must be deferred to AFTER flock release on both the "
        f"empty-dir and past-cutoff branches (mirrors store.prune_tombstones)."
    )
    # End state on both platforms: the sidecar is gone.
    assert not lock_path.exists(), (
        "sidecar lockfile must be unlinked post-prune (deferred past "
        "flock release so the unlink lands on Windows too)."
    )
