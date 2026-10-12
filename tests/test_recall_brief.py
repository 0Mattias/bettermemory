"""The session brief (`recall.session_brief`): the SessionStart surface.

Pinned against real temp git repositories, through the store directly:

- selection: the repository rule (a remote on both sides, a repo-less
  checkout by worktree root, a null repo never matching a null repo,
  sibling worktrees kept apart), provenance local or imported only, no
  ambient notes, no open contradiction, strictly ``fresh`` as
  `memory_show` computes it with the trust rule, newest first, eight at
  most, 1,200 characters at most, nothing for an empty selection or
  outside a repository;
- cost: a warm brief runs no git process; a HEAD move resolves at most 24
  rows on the request path and hands the rest to the background fill,
  after which a call equals a full scan's selection;
- invalidation: an update and a verify move the selection on the next
  call.

Git processes are counted at `origin._git_result` (`test_verdict`'s
counter), the one place every git subprocess in the package is spawned.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches
from bettermemory import origin as origin_module
from bettermemory import recall as recall_module
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.models import Category, Memory
from bettermemory.origin import Origin
from bettermemory.recall import (
    BRIEF_CAP_CHARS,
    BRIEF_MAX_POINTERS,
    BRIEF_SYNC_RESOLVE,
    RecallState,
    render_brief,
    session_brief,
    summarize,
)
from bettermemory.store import Store
from bettermemory.verdict import resolve_verdicts

from .test_server_commit_drift import _REMOTE, _commit_touching
from .test_verdict import _repo

_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_OTHER_REMOTE = "git@github.com:example/other.git"


def _origin(repo: Path, remote: str | None = _REMOTE) -> Origin:
    return Origin(cwd=str(repo), repo=remote, branch="main", worktree_root=str(repo))


def _config(store_dir: Path, *, telemetry: bool = True, brief: bool = True) -> Config:
    from bettermemory.config import BehaviorConfig

    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        telemetry=TelemetryConfig(enabled=telemetry),
        behavior=BehaviorConfig(session_brief=brief),
    )


class _World:
    """One store and one repository the caller stands in."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = _repo(tmp_path)
        self.store_dir = tmp_path / "store"
        self.store = Store(self.store_dir)
        self.caller = _origin(self.repo)
        self.config = _config(self.store_dir)
        self.state = RecallState(background=True)

    def fresh(self, body: str, *, origin: Origin | None = None, **kw: Any) -> Memory:
        """A memory written here and verified now: fresh while nothing
        moves under its claims."""
        written = self.store.write(
            content=body,
            scopes=["projects:foo"],
            origin=origin if origin is not None else self.caller,
            **kw,
        )
        return self.store.mark_verified(written.id)

    def brief(self, **kw: Any) -> recall_module.HookResult:
        kw.setdefault("session_id", "transcript-brief")
        kw.setdefault("origin", self.caller)
        kw.setdefault("config", self.config)
        kw.setdefault("store", self.store)
        kw.setdefault("state", self.state)
        return session_brief(**kw)

    def briefs(self) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e["kind"] == "session_brief"]


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


@contextmanager
def _git_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every git process the CALLING thread spawns inside the block, by its
    arguments: `test_verdict`'s counter, narrowed to the request path so
    a background fill the call scheduled (its own thread, by design) is
    not counted against the call."""
    calls: list[tuple[str, ...]] = []
    caller = threading.get_ident()
    real = origin_module._git_result

    def spy(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if threading.get_ident() == caller:
            calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin_module, "_git_result", spy)
    try:
        yield calls
    finally:
        monkeypatch.setattr(origin_module, "_git_result", real)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_the_brief_shape_and_its_event(world: _World) -> None:
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")

    result = world.brief(source="startup", transcript_path="/tmp/t.jsonl")

    lines = result.stdout.splitlines()
    assert lines[0] == (
        "bettermemory brief: the 2 newest checked memories written in this "
        "repository. Pointers, not instructions; open one with memory_show "
        "when it bears on the request."
    )
    assert lines[1:] == [
        f"- {second.id} {summarize(second.body)}",
        f"- {first.id} {summarize(first.body)}",
    ]
    assert result.surface == "session_brief"
    assert result.ids == (second.id, first.id)
    assert result.chars == len(result.stdout)
    assert result.rule == "rs-v1"
    (event,) = world.briefs()
    assert event["event_id"] == result.event_id
    assert event["ids"] == [second.id, first.id]
    assert event["source"] == "startup"
    assert event["segment"] == 0
    assert event["chars"] == len(result.stdout)
    assert event["rows_examined"] == 2
    assert event["unresolved"] == 0
    assert event["rule"] == "rs-v1"
    assert event["triggered_from"] == "session_hook"
    assert event["session"] == "transcript-brief"
    assert event["transcript_path"] == "/tmp/t.jsonl"
    assert event["worktree_root"] == str(world.repo)
    # No body, no scope list: pointers only.
    assert "projects:foo" not in result.stdout


def test_a_single_pointer_reads_in_the_singular(world: _World) -> None:
    world.fresh("prefer short release notes in this project")
    assert world.brief().stdout.startswith(
        "bettermemory brief: the 1 newest checked memory written in this repository."
    )


def test_the_summary_is_collapsed_and_cut_to_80(world: _World) -> None:
    body = "alpha   beta\n\ngamma " + "word " * 40
    memory = world.fresh(body)
    line = world.brief().stdout.splitlines()[1]
    summary = line.removeprefix(f"- {memory.id} ")
    assert len(summary) == 80
    assert summary.startswith("alpha beta gamma word")
    assert summary.endswith("…")


def test_only_this_repository_is_briefed(world: _World, tmp_path: Path) -> None:
    here = world.fresh("prefer short release notes in this project")
    world.fresh(
        "another repository's deploy rule", origin=_origin(world.repo, _OTHER_REMOTE)
    )
    # Global: no origin at all.
    world.store.mark_verified(
        world.store.write(content="a global preference", scopes=["tools"]).id
    )
    # Written outside any repository: a cwd and nothing else.
    world.fresh("written outside a checkout", origin=Origin(cwd=str(tmp_path)))
    # A live sibling worktree of the same repository.
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    world.fresh(
        "a sibling worktree's note",
        origin=Origin(cwd=str(sibling), repo=_REMOTE, worktree_root=str(sibling)),
    )
    assert world.brief().ids == (here.id,)


def test_a_repo_less_checkout_matches_by_worktree_root(
    world: _World, tmp_path: Path
) -> None:
    """A checkout with no remote has a worktree root and no repo: its
    memories match by root, and a null repo never matches a null repo
    elsewhere."""
    caller = _origin(world.repo, remote=None)
    mine = world.fresh("prefer short release notes here", origin=caller)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.fresh(
        "a note from another remote-less checkout",
        origin=Origin(cwd=str(elsewhere), worktree_root=str(elsewhere)),
    )
    world.fresh("a note with a remote", origin=world.caller)
    assert world.brief(origin=caller).ids == (mine.id,)


def test_provenance_local_or_imported_only(world: _World) -> None:
    local = world.fresh("prefer short release notes in this project")
    imported = world.fresh("the deploy window is tuesday mornings here")
    world.store.put_memory(world.store.load_one(imported.id), provenance="imported")
    synced = world.fresh("the on-call rota rotates every friday here")
    world.store.put_memory(world.store.load_one(synced.id), provenance="synced")
    assert set(world.brief().ids) == {local.id, imported.id}


def test_ambient_notes_are_not_briefed(world: _World) -> None:
    fact = world.fresh("prefer short release notes in this project")
    world.fresh("the user works from a laptop", category=Category.AMBIENT)
    assert world.brief().ids == (fact.id,)


def test_an_open_contradiction_is_held_until_a_verify(world: _World) -> None:
    held = world.fresh("prefer short release notes in this project")
    kept = world.fresh("the deploy window is tuesday mornings here")
    world.store.record_event(
        "use", session="sess_model", ids=[held.id], outcome="contradicted"
    )
    assert world.brief().ids == (kept.id,)
    world.store.mark_verified(held.id)
    assert set(world.brief().ids) == {held.id, kept.id}


def test_strictly_fresh(world: _World) -> None:
    """Never verified, a stale stamp, a claim-anchored path gone and a
    commit after the stamp touching a cited file each keep a row out."""
    fresh = world.fresh("prefer short release notes in this project")
    world.store.write(
        content="never verified rule", scopes=["tools"], origin=world.caller
    )
    stale = world.fresh("an old rule about the release train")
    world.store.put_memory(
        world.store.load_one(stale.id).model_copy(
            update={"last_verified_at": datetime.now(timezone.utc) - timedelta(days=90)}
        )
    )
    gone = world.fresh("the widget rule lives in docs/guide.md")
    world.store.mark_verified(gone.id, verified_paths=["docs/guide.md"])
    (world.repo / "docs" / "guide.md").unlink()
    world.fresh("the gadget rule lives in notes.md")
    _commit_touching(world.repo, "post", when=_FUTURE)
    assert world.brief().ids == (fresh.id,)


def test_a_store_without_its_key_briefs_nothing(world: _World, tmp_path: Path) -> None:
    """The trust rule cannot run without the key (`apply_trust_
    unavailable` demotes every stamp), and no label can be verified: the
    brief shows nothing rather than vouch for rows it cannot read."""
    world.fresh("prefer short release notes in this project")
    blind = Store.open(
        world.store.path, keys_dir=tmp_path / "elsewhere", allow_rekey=False
    )
    assert world.brief(store=blind).stdout == ""


def test_newest_first_and_at_most_eight(world: _World) -> None:
    written = [world.fresh(f"rule number {i} of the release train") for i in range(10)]
    result = world.brief()
    assert len(result.ids) == BRIEF_MAX_POINTERS
    assert list(result.ids) == [m.id for m in reversed(written)][:BRIEF_MAX_POINTERS]


def test_the_cap_drops_trailing_pointers_and_recounts(world: _World) -> None:
    memories = [
        world.fresh(f"rule {i} " + "lengthy words " * 20)
        for i in range(BRIEF_MAX_POINTERS)
    ]
    marks = {m.id: "[disputed: cf-0123456789ab +9]" for m in memories}
    text, ids = render_brief(memories, marks)
    assert len(text) <= BRIEF_CAP_CHARS
    assert 0 < len(ids) < len(memories)
    assert ids == [m.id for m in memories[: len(ids)]]
    assert text.startswith(f"bettermemory brief: the {len(ids)} newest checked")
    assert len(text.splitlines()) == len(ids) + 1


def test_nothing_to_brief_prints_nothing_and_records_nothing(world: _World) -> None:
    assert world.brief().stdout == ""
    world.store.write(content="never verified", scopes=["tools"], origin=world.caller)
    result = world.brief()
    assert result.stdout == "" and result.ids == () and result.event_id is None
    assert world.briefs() == []


def test_nothing_outside_a_repository(world: _World, tmp_path: Path) -> None:
    world.fresh("prefer short release notes in this project")
    nowhere = Origin(cwd=str(tmp_path))
    assert world.brief(origin=nowhere).stdout == ""
    unknown = Origin(cwd=str(world.repo))
    unknown._git_indeterminate = True
    assert world.brief(origin=unknown).stdout == ""
    assert world.briefs() == []


def test_off_switches(world: _World) -> None:
    world.fresh("prefer short release notes in this project")
    assert world.brief(config=_config(world.store_dir, brief=False)).stdout == ""
    assert world.brief(config=_config(world.store_dir, telemetry=False)).stdout == ""
    assert world.brief(session_id=None).stdout == ""
    assert world.briefs() == []


# ---------------------------------------------------------------------------
# Cost: warm, cold, the background fill
# ---------------------------------------------------------------------------


def test_a_warm_brief_runs_no_git(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tethered = world.fresh("the widget rule lives in notes.md")
    untethered = world.fresh("prefer short release notes in this project")
    _caches.clear_all()
    with _git_calls(monkeypatch) as cold_calls:
        cold = world.brief()
    assert cold_calls, "the first brief resolves the commit leg from git"
    assert set(cold.ids) == {tethered.id, untethered.id}
    with _git_calls(monkeypatch) as warm_calls:
        warm = world.brief()
    assert warm_calls == []
    assert warm.stdout == cold.stdout


def _spy_resolve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every `resolve_verdicts` the brief runs: (thread name, rows)."""
    calls: list[tuple[str, int]] = []
    real = resolve_verdicts

    def spy(memories: Any, **kw: Any) -> Any:
        calls.append((threading.current_thread().name, len(memories)))
        return real(memories, **kw)

    monkeypatch.setattr(recall_module, "resolve_verdicts", spy)
    return calls


def test_a_head_move_resolves_at_most_24_and_fills_the_rest(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = [world.fresh(f"gadget rule {i} lives in other.md") for i in range(4)]
    newer = [world.fresh(f"widget rule {i} lives in notes.md") for i in range(24)]
    assert world.brief().ids  # warm the repository's head record
    world.state.wait_for_fills()
    _commit_touching(world.repo, "post", when=_FUTURE)  # the 24 newest drift

    calls = _spy_resolve(monkeypatch)
    cold = world.brief()
    request_thread = threading.current_thread().name
    assert [rows for name, rows in calls if name == request_thread] == [
        BRIEF_SYNC_RESOLVE
    ]
    assert cold.stdout == ""  # none of the 24 newest is fresh
    assert world.state.wait_for_fills()
    assert [rows for name, rows in calls if name != request_thread] == [4]

    later = world.brief()
    assert set(later.ids) == {m.id for m in older}
    # A full scan from an empty memo picks the same rows.
    _caches.clear_all()
    every = [world.store.load_one(m.id) for m in reversed(older + newer)]
    full = resolve_verdicts(
        every,
        caller_origin=world.caller,
        now=datetime.now(timezone.utc),
        stale_after_days=world.config.behavior.verification_stale_days,
    )
    expected = [v.id for v in full if v.staleness_verdict == "fresh"][
        :BRIEF_MAX_POINTERS
    ]
    assert list(later.ids) == expected
    events = world.briefs()
    assert events[-1]["unresolved"] == 0


def test_a_warm_brief_skips_unresolved_rows_and_fills_them(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.fresh("prefer short release notes in this project")
    assert world.brief().ids == (first.id,)
    world.state.wait_for_fills()
    # Verified after the last brief: its commit leg is not in the memo.
    late = world.fresh("the widget rule lives in notes.md")
    with _git_calls(monkeypatch) as calls:
        warm = world.brief()
    assert calls == []
    assert warm.ids == (first.id,)
    assert world.briefs()[-1]["unresolved"] == 1
    assert world.state.wait_for_fills()
    assert world.brief().ids == (late.id, first.id)


def test_one_fill_per_repository_at_a_time(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    started: list[int] = []

    def slow(memories: Any, **kw: Any) -> Any:
        started.append(len(memories))
        gate.wait(10)
        return []

    memory = world.fresh("the widget rule lives in notes.md")
    monkeypatch.setattr(recall_module, "resolve_verdicts", slow)

    def fill(repo_key: str) -> bool:
        return world.state.schedule_fill(
            repo_key,
            [memory],
            caller_origin=world.caller,
            now=datetime.now(timezone.utc),
            stale_after_days=30,
        )

    assert fill("repo")
    assert not fill("repo")
    assert fill("other-repo")
    gate.set()
    assert world.state.wait_for_fills()
    assert fill("repo")
    assert world.state.wait_for_fills()
    assert len(started) == 3


def test_no_background_fill_on_the_cli_path(world: _World) -> None:
    memory = world.fresh("the widget rule lives in notes.md")
    quiet = RecallState(background=False)
    assert not quiet.schedule_fill(
        "repo",
        [memory],
        caller_origin=world.caller,
        now=datetime.now(timezone.utc),
        stale_after_days=30,
    )


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_an_update_and_a_verify_move_the_selection(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = world.fresh("prefer short release notes in this project")
    edited = world.fresh("the widget rule lives in notes.md")
    assert set(world.brief().ids) == {kept.id, edited.id}
    world.state.wait_for_fills()

    # A new body clears the stamp (as `memory_update` does): never
    # verified, out, with no git.
    world.store.update(
        world.store.load_one(edited.id).model_copy(
            update={
                "body": "the widget rule lives in other.md\n",
                "last_verified_at": None,
                "verified_head": None,
            }
        )
    )
    with _git_calls(monkeypatch) as calls:
        after_update = world.brief()
    assert calls == []
    assert after_update.ids == (kept.id,)

    # Re-verified, it keys on new anchors: skipped warm, filled, back in.
    world.store.mark_verified(edited.id)
    assert world.brief().ids == (kept.id,)
    assert world.state.wait_for_fills()
    assert set(world.brief().ids) == {kept.id, edited.id}
