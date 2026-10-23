"""The session brief (`recall.session_brief`): the SessionStart surface.

Pinned against real temp git repositories, through the store directly:

- selection: the project rule (design 3.5: the memories homed in the
  caller's project, from any checkout of it, a linked worktree included;
  a remote-less checkout a project of its own), provenance local or
  imported only, no
  ambient notes, no open contradiction, strictly ``fresh`` as
  `memory_show` computes it with the trust rule, newest first, eight at
  most, 1,200 characters at most, nothing for an empty selection or
  outside a repository;
- cost: a warm brief runs no git process; a HEAD move resolves at most 24
  rows on the request path and hands the rest to the background fill,
  after which a call equals a full scan's selection;
- invalidation: an update and a verify move the selection on the next
  call;
- the resume: a SessionStart with source ``resume`` keeps the segment,
  and its brief omits the ids the segment has shown, recording them as
  ``omitted``; startup, clear, and a compact on its marker omit nothing.

Git processes are counted at `origin._git_result` (`test_verdict`'s
counter), the one place every git subprocess in the package is spawned.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches
from bettermemory import _response as response_module
from bettermemory import origin as origin_module
from bettermemory import recall as recall_module
from bettermemory.admission import CountedRecords
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.location import Caller, resolve_caller
from bettermemory.membership import RecordHome
from bettermemory.models import Category, Memory
from bettermemory.origin import Origin
from bettermemory.recall import (
    BRIEF_CAP_CHARS,
    BRIEF_MAX_POINTERS,
    BRIEF_SYNC_RESOLVE,
    STATE_IDLE_SECONDS,
    SURFACE_BRIEF,
    RecallState,
    render_brief,
    session_brief,
    summarize,
)
from bettermemory.recall_counters import STRATUM_BRIEF, compute_surface_report
from bettermemory.recall_review import candidates
from bettermemory.store import StandingAnswer, Store
from bettermemory.verdict import (
    FillReport,
    UnresolvedVerdict,
    fill_verdicts,
    memoised_verdicts,
    resolve_verdicts,
)

from . import _transcript_rows as tr
from .test_server_commit_drift import _REMOTE, _commit_touching
from .test_membership_security import checkout
from .test_verdict import _repo

_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_OTHER_REMOTE = "git@github.com:example/other.git"

# The drift memo and the origin cache key on `githead.signature`, which
# declines file discovery off POSIX: on Windows every signature is
# undetermined, nothing is memoised and git runs on every call.
_MEMO_IS_POSIX = pytest.mark.skipif(
    sys.platform == "win32", reason="githead declines file discovery off POSIX"
)


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
    """One store and one repository the caller stands in.

    A memory is briefed by its home (design 3.5), which `fresh` gives it
    from the origin it is written with: the repository's project for an
    origin in it (or below it), ``global`` for none or for a directory
    outside any repository, and the project of another checkout (made on
    first use, one per directory name) for any other origin. The origin
    stays on the record as data."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.repo = _repo(tmp_path)
        self.store_dir = tmp_path / "store"
        self.store = Store(self.store_dir)
        self.caller = _origin(self.repo)
        self.config = _config(self.store_dir)
        self.state = RecallState(background=True)
        # A checkout seen before: its first sight's line (L2) is not what
        # these tests read (`test_membership_resolution.
        # test_first_sight_line_leads_the_brief` does).
        self.home = self.resolve(self.repo).home()
        self._homes: dict[str, RecordHome] = {}

    def resolve(self, where: Path | None) -> Caller:
        """The caller standing at `where` (None: nothing readable)."""
        return resolve_caller(
            self.store, self.config, None if where is None else str(where)
        )

    def other_home(self, name: str, *, remote: str | None = None) -> RecordHome:
        """The home of a record written in another checkout, `name`, its
        own project."""
        if name not in self._homes:
            other = checkout(self.tmp_path / "others" / name, remote=remote)
            self._homes[name] = self.resolve(other).home()
        return self._homes[name]

    def home_of(self, origin: Origin | None) -> RecordHome:
        """The home a record written with `origin` has (see the class)."""
        if origin is None:
            plain = self.tmp_path / "plain"
            plain.mkdir(exist_ok=True)
            return self.resolve(plain).home()
        where = origin.worktree_root or origin.cwd
        if where is not None and Path(where).is_relative_to(self.repo):
            return self.home
        if origin.repo is None and origin.worktree_root is None:
            return self.home_of(None)
        return self.other_home(Path(where or "other").name)

    def fresh(self, body: str, *, origin: Origin | None = None, **kw: Any) -> Memory:
        """A memory written here and verified now: fresh while nothing
        moves under its claims."""
        where = origin if origin is not None else self.caller
        kw.setdefault("home", self.home_of(where))
        written = self.store.write(
            content=body,
            scopes=["projects:foo"],
            origin=where,
            **kw,
        )
        return self.store.mark_verified(written.id)

    def brief(self, **kw: Any) -> recall_module.HookResult:
        """A brief, from the session ``transcript-brief`` unless told
        otherwise. A session's segment shows no id twice, so a test that
        briefs the same repository again and expects the selection again
        briefs it from another session; the state's warmth is keyed on
        the repository and carries over."""
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
    assert result.rule == "rs-v5"
    (event,) = world.briefs()
    assert event["event_id"] == result.event_id
    assert event["ids"] == [second.id, first.id]
    assert event["source"] == "startup"
    assert event["segment"] == 0
    assert event["chars"] == len(result.stdout)
    assert event["rows_examined"] == 2
    assert event["unresolved"] == 0
    assert event["rule"] == "rs-v5"
    assert event["checkout"] == world.home.checkout
    assert event["rc3_admitted"] == [second.id, first.id]
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


def test_only_this_project_is_briefed(world: _World, tmp_path: Path) -> None:
    """The brief shows the memories of the caller's project (design 3.5),
    from whichever checkout of it they were written in, a linked worktree
    of the repository included (D1); another repository's, a global one
    and one written outside any checkout are not shown."""
    here = world.fresh("prefer short release notes in this project")
    world.fresh(
        "another repository's deploy rule",
        origin=_origin(tmp_path / "other", _OTHER_REMOTE),
        home=world.other_home("other", remote=_OTHER_REMOTE),
    )
    # Global: no origin at all.
    world.store.mark_verified(
        world.store.write(
            content="a global preference", scopes=["tools"], home=world.home_of(None)
        ).id
    )
    # Written outside any repository: a cwd and nothing else.
    world.fresh("written outside a checkout", origin=Origin(cwd=str(tmp_path)))
    # A linked worktree of the same repository: the same project.
    sibling = tmp_path / "sibling"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "sibling", str(sibling)],
        cwd=world.repo,
        check=True,
        capture_output=True,
    )
    theirs = world.fresh(
        "a sibling worktree's note",
        origin=Origin(cwd=str(sibling), repo=_REMOTE, worktree_root=str(sibling)),
        home=world.resolve(sibling).home(),
    )
    assert world.brief().ids == (theirs.id, here.id)


def test_a_remote_less_checkout_briefs_its_own(world: _World, tmp_path: Path) -> None:
    """A checkout with no remote is a project of its own (design 2.1): its
    memories are briefed there, and neither another remote-less
    checkout's nor another repository's are."""
    bare = checkout(tmp_path / "bare")
    caller = _origin(bare, remote=None)
    mine = world.fresh(
        "prefer short release notes here",
        origin=caller,
        home=world.resolve(bare).home(),
    )
    elsewhere = tmp_path / "elsewhere"
    world.fresh(
        "a note from another remote-less checkout",
        origin=Origin(cwd=str(elsewhere), worktree_root=str(elsewhere)),
        home=world.other_home("elsewhere"),
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
    assert set(world.brief(session_id="transcript-brief-2").ids) == {held.id, kept.id}


def test_a_contradiction_does_not_hold_a_standing_record(world: _World) -> None:
    """A ``contradicted`` use is the agent's word: it does not take the
    user's rule out of the brief (unit RS-F7 part 5)."""
    ruling = world.fresh("prefer short release notes in this project")
    kept = world.fresh("the deploy window is tuesday mornings here")
    world.store.put_standing(ruling.id, StandingAnswer(0.9, "asked", "test/test"))
    world.store.record_event(
        "use", session="sess_model", ids=[ruling.id], outcome="contradicted"
    )
    assert set(world.brief().ids) == {ruling.id, kept.id}


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
    """Outside any project the brief shows nothing and records nothing; a
    caller nothing could be read for is shown nothing and its row says
    so (design 3.5)."""
    world.fresh("prefer short release notes in this project")
    nowhere = Origin(cwd=str(tmp_path))
    assert world.brief(origin=nowhere).stdout == ""
    assert world.briefs() == []
    unknown = world.resolve(None).resolution
    result = world.brief(resolution=unknown)
    assert result.ids == ()
    (row,) = world.briefs()
    assert row["ids"] == []
    assert row["membership"] == "admitted nothing: indeterminate"


def test_off_switches(world: _World) -> None:
    world.fresh("prefer short release notes in this project")
    assert world.brief(config=_config(world.store_dir, brief=False)).stdout == ""
    assert world.brief(config=_config(world.store_dir, telemetry=False)).stdout == ""
    assert world.brief(session_id=None).stdout == ""
    assert world.briefs() == []


# ---------------------------------------------------------------------------
# The resume: the cooldown's rule on the brief
# ---------------------------------------------------------------------------
#
# A SessionStart with source ``resume`` keeps the segment the session is
# in, with everything the segment has shown. The brief's selection is
# split on that set after `_select_brief` and before `render_brief`: an id
# the segment has shown is omitted and named in the row's `omitted`, the
# rest is printed. The freed slots are not filled. The rule reads the
# shown set on every source, so a startup, compact or clear, whose set is
# empty, is unchanged.

BRIEF_KEY = ("claude-code", "transcript-brief")


def _marked(tmp_path: Path, uuid: str) -> str:
    """A transcript whose newest row is a compaction marker stamped now:
    what a compact SessionStart reads to open a segment (RS-F5)."""
    path = tmp_path / f"{uuid}.jsonl"
    return str(tr.write(path, tr.boundary(datetime.now(timezone.utc), uuid)))


def _segment_view(world: _World) -> recall_module.SegmentView:
    return world.state.segment(world.store, BRIEF_KEY, now=datetime.now(timezone.utc))


def test_a_resume_omits_what_its_segment_has_shown(world: _World) -> None:
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")
    startup = world.brief(source="startup")
    assert startup.ids == (second.id, first.id)
    world.state.wait_for_fills()
    third = world.fresh("the on-call rota rotates every friday here")

    resumed = world.brief(source="resume")

    assert resumed.ids == (third.id,)
    assert resumed.stdout.splitlines() == [
        "bettermemory brief: the 1 newest checked memory written in this "
        "repository. Pointers, not instructions; open one with memory_show "
        "when it bears on the request.",
        f"- {third.id} {summarize(third.body)}",
    ]
    assert resumed.chars == len(resumed.stdout)
    opened, row = world.briefs()
    assert opened["omitted"] == []
    assert (row["source"], row["segment"]) == ("resume", 0)
    assert row["ids"] == [third.id]
    assert row["omitted"] == [second.id, first.id]  # selection order
    assert row["chars"] == resumed.chars
    assert row["event_id"] == resumed.event_id
    # The printed id joins the shown set under the resume's event; an
    # omitted id keeps the show that first printed it.
    view = _segment_view(world)
    assert view.shown == {first.id, second.id, third.id}
    assert view.shown_by[third.id] == (SURFACE_BRIEF, resumed.event_id)
    assert view.shown_by[second.id] == (SURFACE_BRIEF, startup.event_id)


def test_a_resume_after_a_compact_brief_omits_in_the_compacts_segment(
    world: _World, tmp_path: Path
) -> None:
    first = world.fresh("prefer short release notes in this project")
    assert world.brief(source="startup").ids == (first.id,)
    # A compact on its marker opens a segment with nothing shown: printed
    # again.
    marked = _marked(tmp_path, "boundary-1")
    assert world.brief(source="compact", transcript_path=marked).ids == (first.id,)
    world.state.wait_for_fills()
    second = world.fresh("the deploy window is tuesday mornings for this project")
    assert world.brief(source="resume").ids == (second.id,)
    briefs = [
        (r["source"], r["segment"], r["ids"], r["omitted"]) for r in world.briefs()
    ]
    assert briefs == [
        ("startup", 0, [first.id], []),
        ("compact", 1, [first.id], []),
        ("resume", 1, [second.id], [first.id]),
    ]
    assert world.briefs()[1]["marker"] == "boundary-1"


def test_a_resume_with_every_candidate_shown_prints_nothing_and_records_the_row(
    world: _World,
) -> None:
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")
    startup = world.brief(source="startup")
    assert startup.ids == (second.id, first.id)
    world.state.wait_for_fills()

    resumed = world.brief(source="resume", transcript_path="/tmp/t.jsonl")

    assert (resumed.stdout, resumed.ids, resumed.chars) == ("", (), 0)
    assert resumed.surface == "session_brief"
    assert resumed.rule == "rs-v5"
    assert resumed.event_id is not None and resumed.event_id != startup.event_id
    _, row = world.briefs()
    assert row["event_id"] == resumed.event_id
    assert (row["ids"], row["chars"], row["omitted"]) == ([], 0, [second.id, first.id])
    assert (row["source"], row["segment"], row["rule"]) == ("resume", 0, "rs-v5")
    assert (row["rows_examined"], row["unresolved"]) == (2, 0)
    assert row["session"] == "transcript-brief"
    assert row["transcript_path"] == "/tmp/t.jsonl"
    assert row["triggered_from"] == "session_hook"
    # No segment was opened, and the shown set is as the startup left it.
    assert [e for e in world.store.iter_events() if e["kind"] == "segment_opened"] == []
    view = _segment_view(world)
    assert view.shown == {first.id, second.id}
    assert view.shown_by[first.id] == (SURFACE_BRIEF, startup.event_id)


def test_startup_compact_and_clear_omit_nothing(world: _World, tmp_path: Path) -> None:
    """A compact here reads its marker (RS-F5); one without it is a
    resume (`test_recall_compaction.py`)."""
    memory = world.fresh("prefer short release notes in this project")
    marked = _marked(tmp_path, "boundary-1")
    for source, segment in (("startup", 0), ("compact", 1), ("clear", 2)):
        assert world.brief(source=source, transcript_path=marked).ids == (memory.id,)
        row = world.briefs()[-1]
        assert (row["source"], row["segment"], row["omitted"]) == (source, segment, [])
    # A startup under a session the state has not seen: nothing shown yet.
    assert world.brief(source="startup", session_id="transcript-other").ids == (
        memory.id,
    )
    assert world.briefs()[-1]["omitted"] == []


def test_the_cli_path_reads_the_omission_from_the_log_past_the_idle_window(
    world: _World,
) -> None:
    """The in-process path holds no state between calls and rebuilds the
    segment from the session's rows in the log: a brief is omitted on a
    resume, and still is past `STATE_IDLE_SECONDS` (RS-F5, changed
    deliberately: the rebuild read the last day only, and a resume a day
    later printed the segment's ids again)."""
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")
    startup = world.brief(source="startup", state=RecallState(background=False))
    assert startup.ids == (second.id, first.id)

    resumed = world.brief(source="resume", state=RecallState(background=False))
    assert resumed.ids == () and resumed.event_id is not None
    assert world.briefs()[-1]["omitted"] == [second.id, first.id]
    # The empty row adds nothing to the rebuilt set: the same omission.
    again = world.brief(source="resume", state=RecallState(background=False))
    assert again.ids == () and world.briefs()[-1]["omitted"] == [second.id, first.id]

    past_the_window = datetime.now(timezone.utc) + timedelta(
        seconds=STATE_IDLE_SECONDS + 3600
    )
    later = world.brief(
        source="resume", state=RecallState(background=False), now=past_the_window
    )
    assert later.ids == ()
    assert world.briefs()[-1]["omitted"] == [second.id, first.id]


def test_a_brief_then_a_resume_30_hours_later_omits_it_on_a_fresh_state(
    world: _World,
) -> None:
    """RS-F5's pin: a brief's show, then a resume 30 hours later on the
    CLI path's fresh state, omits the id; the daemon's state, evicted
    after a day idle, rebuilds the shown set it held."""
    first = world.fresh("prefer short release notes in this project")
    now = datetime.now(timezone.utc)
    assert world.brief(source="startup", now=now).ids == (first.id,)
    world.state.wait_for_fills()
    held = world.state.segment(world.store, BRIEF_KEY, now=now).shown
    assert held == {first.id}

    later = now + timedelta(hours=30)
    resumed = world.brief(
        source="resume", state=RecallState(background=False), now=later
    )
    assert resumed.ids == ()
    assert world.briefs()[-1]["omitted"] == [first.id]
    fresh = RecallState(background=False).segment(world.store, BRIEF_KEY, now=later)
    evicted = world.state.segment(world.store, BRIEF_KEY, now=later)
    assert fresh.shown == evicted.shown == held


def test_a_compact_without_its_marker_briefs_as_a_resume(
    world: _World, tmp_path: Path
) -> None:
    """RS-F5: the compact SessionStart's transcript holds no compaction
    marker yet, so its brief omits what the segment has shown, as a
    resume's does, and its row says the marker was absent. When a later
    read finds the marker, the segment it opens at the compact holds the
    compact brief's ids, which were shown after the compaction."""
    first = world.fresh("prefer short release notes in this project")
    assert world.brief(source="startup").ids == (first.id,)
    world.state.wait_for_fills()
    second = world.fresh("the deploy window is tuesday mornings for this project")
    transcript = tr.write(tmp_path / "t.jsonl", tr.user("an ask", "p1"))
    signal = datetime.now(timezone.utc)
    compacted = world.brief(
        source="compact", transcript_path=str(transcript), now=signal
    )
    assert compacted.ids == (second.id,)
    row = world.briefs()[-1]
    assert (row["source"], row["segment"], row["omitted"]) == (
        "compact",
        0,
        [first.id],
    )
    assert row["marker_absent"] is True and "marker" not in row
    world.state.wait_for_fills()

    tr.append(transcript, tr.boundary(signal + timedelta(seconds=1)))
    resumed = world.brief(
        source="resume",
        transcript_path=str(transcript),
        now=signal + timedelta(seconds=120),
    )
    [opened] = [e for e in world.store.iter_events() if e["kind"] == "segment_opened"]
    assert (opened["segment"], opened["marker"]) == (1, "boundary-1")
    assert resumed.ids == (first.id,)
    row = world.briefs()[-1]
    assert (row["source"], row["segment"], row["omitted"]) == (
        "resume",
        1,
        [second.id],
    )


def test_an_all_omitted_resume_is_no_show_for_eval_and_the_review(
    world: _World,
) -> None:
    """The row with no ids is not a show: eval's surfaces count no brief
    and no characters for it, and the review does not draw it."""
    world.fresh("prefer short release notes in this project")
    startup = world.brief(source="startup")
    world.state.wait_for_fills()
    resumed = world.brief(source="resume")
    assert resumed.ids == () and resumed.event_id is not None
    events = list(world.store.iter_events())
    surfaces = compute_surface_report(events, start=None, end=None)
    assert surfaces is not None
    brief = surfaces[SURFACE_BRIEF]
    assert (brief["deliveries"], brief["shown"], brief["chars_p50"]) == (
        1,
        1,
        startup.chars,
    )
    population = candidates(events, start=None, end=None, memories={})
    assert [c.sample_id for c in population[STRATUM_BRIEF]] == [startup.event_id]


# ---------------------------------------------------------------------------
# Cost: warm, cold, the background fill
# ---------------------------------------------------------------------------


@_MEMO_IS_POSIX
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
        warm = world.brief(session_id="transcript-brief-2")
    assert warm_calls == []
    assert warm.stdout == cold.stdout


def _counted(memories: list[Memory]) -> CountedRecords:
    """The records a full scan reads verdicts for, named: what the commit
    legs are read under (`admission.DriftScope`), as the brief names the
    records its admission decided."""
    return CountedRecords(frozenset(m.id for m in memories))


def _spy_resolve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every `resolve_verdicts` the brief runs on the request path and every
    `fill_verdicts` its background fill runs: (thread name, rows)."""
    calls: list[tuple[str, int]] = []
    real = resolve_verdicts
    real_fill = fill_verdicts

    def spy(memories: Any, **kw: Any) -> Any:
        calls.append((threading.current_thread().name, len(memories)))
        return real(memories, **kw)

    def spy_fill(memories: Any, **kw: Any) -> Any:
        calls.append((threading.current_thread().name, len(memories)))
        return real_fill(memories, **kw)

    monkeypatch.setattr(recall_module, "resolve_verdicts", spy)
    monkeypatch.setattr(recall_module, "fill_verdicts", spy_fill)
    return calls


@_MEMO_IS_POSIX
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
        admission=_counted(every),
    )
    expected = [v.id for v in full if v.staleness_verdict == "fresh"][
        :BRIEF_MAX_POINTERS
    ]
    assert list(later.ids) == expected
    events = world.briefs()
    assert events[-1]["unresolved"] == 0


@_MEMO_IS_POSIX
def test_a_warm_brief_resolves_the_rows_its_memo_cannot_answer(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm call that would show fewer than eight because the memo
    cannot answer some rows resolves them on the request path, at most
    24, as a cold call does: the pointer is not held back a call."""
    first = world.fresh("prefer short release notes in this project")
    assert world.brief().ids == (first.id,)
    world.state.wait_for_fills()
    # Verified after the last brief: its commit leg is not in the memo.
    late = world.fresh("the widget rule lives in notes.md")
    calls = _spy_resolve(monkeypatch)
    warm = world.brief(session_id="transcript-brief-2")
    assert warm.ids == (late.id, first.id)
    assert calls == [(threading.current_thread().name, 1)]
    assert world.briefs()[-1]["unresolved"] == 0


def test_a_brief_from_a_subdirectory_shows_the_same_pointers(
    world: _World,
) -> None:
    """The drift memo keys on the directory the caller stands in, so a
    brief from a subdirectory is not warm off one from the root: it
    resolves, and shows what the root's brief showed, at once."""
    tethered = world.fresh("the widget rule lives in notes.md")
    untethered = world.fresh("prefer short release notes in this project")
    at_root = world.brief()
    assert set(at_root.ids) == {tethered.id, untethered.id}
    world.state.wait_for_fills()
    pkg = world.repo / "pkg"
    pkg.mkdir()
    below = Origin(
        cwd=str(pkg),
        repo=world.caller.repo,
        branch="main",
        worktree_root=str(world.repo),
    )
    from_pkg = world.brief(origin=below, session_id="transcript-brief-2")
    assert from_pkg.ids == at_root.ids


def test_one_fill_per_repository_at_a_time(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    started: list[int] = []

    def slow(memories: Any, **kw: Any) -> FillReport:
        started.append(len(memories))
        gate.wait(10)
        return FillReport(rows=len(memories))

    memory = world.fresh("the widget rule lives in notes.md")
    monkeypatch.setattr(recall_module, "fill_verdicts", slow)

    def fill(repo_key: str) -> bool:
        return world.state.schedule_fill(
            repo_key,
            [memory],
            caller_origin=world.caller,
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
    )


# ---------------------------------------------------------------------------
# The fill while the repository moves under it
# ---------------------------------------------------------------------------
#
# The fill stores row by row (`verdict.fill_verdicts`): a disturbance that
# moves a held stamp (the root directory, the index) loses the one row
# whose resolution overlapped it, which is resolved again after the rest,
# and never the whole fill. The fixture makes the fill matter: the 24
# newest rows (C, citing notes.md) and the 24 before them (B, citing
# docs/guide.md) drift after a commit to each file, so only the 16 oldest
# (A, citing other.md) read fresh, and they sit beyond the 24 a cold call
# resolves and beyond the 24 a warm call resolves for rows the memo cannot
# answer. A fill that keeps nothing therefore leaves the warm brief empty
# where the full scan shows eight.

_FILL_ROWS = 40  # B and A: what the cold brief after the commits hands the fill


def _fill_world(world: _World) -> list[Memory]:
    """The three groups, oldest first, after the commits that drift B and
    C; the repository's head noted by a brief before them."""
    older = [world.fresh(f"gadget rule {i} lives in other.md") for i in range(16)]
    middle = [world.fresh(f"guide rule {i} lives in docs/guide.md") for i in range(24)]
    newer = [world.fresh(f"widget rule {i} lives in notes.md") for i in range(24)]
    assert world.brief().ids  # note the repository's head
    assert world.state.wait_for_fills()
    _commit_touching(world.repo, "post", when=_FUTURE)
    _commit_touching(world.repo, "post", when=_FUTURE, filename="docs/guide.md")
    return older + middle + newer


def _on_fill_rows(monkeypatch: pytest.MonkeyPatch, steps: dict[int, Any]) -> list[int]:
    """Run `steps[n]` on the fill's thread just before its n-th row
    resolution (`_response._resolve_hit_drift`), inside the window of the
    pass that resolves that row. Returns the fill-thread resolution count
    (one element, updated in place)."""
    real = response_module._resolve_hit_drift
    seen = [0]

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name.startswith("bettermemory-brief-fill"):
            seen[0] += 1
            step = steps.get(seen[0])
            if step is not None:
                step()
        return real(*args, **kwargs)

    monkeypatch.setattr(response_module, "_resolve_hit_drift", wrapped)
    return seen


def _full_scan(world: _World, memories: list[Memory]) -> list[str]:
    """The first eight rows that read fresh, newest first, resolved from an
    empty memo; the memo is restored afterwards."""
    saved = dict(response_module._DRIFT_MEMO)
    response_module._clear_drift_memo()
    rows = [world.store.load_one(m.id) for m in reversed(memories)]
    full = resolve_verdicts(
        rows,
        caller_origin=world.caller,
        now=datetime.now(timezone.utc),
        stale_after_days=world.config.behavior.verification_stale_days,
        admission=_counted(rows),
    )
    response_module._clear_drift_memo()
    response_module._remember_drift(saved)
    return [v.id for v in full if v.staleness_verdict == "fresh"][:BRIEF_MAX_POINTERS]


def _repo_key(world: _World) -> str:
    return recall_module.brief_key(world.caller)


def _stamp_of(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_mtime_ns, status.st_ctime_ns


def _fill_then_warm(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> tuple[recall_module.HookResult, list[int]]:
    """The cold brief after the commits, its fill joined, then a warm
    brief: the warm brief and the rows it resolved on the request path."""
    cold = world.brief()
    assert cold.stdout == ""  # none of the 24 newest is fresh
    assert world.briefs()[-1]["unresolved"] == _FILL_ROWS
    assert world.state.wait_for_fills()
    calls = _spy_resolve(monkeypatch)
    warm = world.brief()
    request_thread = threading.current_thread().name
    return warm, [rows for name, rows in calls if name == request_thread]


@_MEMO_IS_POSIX
def test_a_fill_keeps_its_rows_when_a_root_file_comes_and_goes(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file created and removed in the root mid-fill (an editor's swap
    file) moves the root's stamp: the row whose resolution it overlapped
    is discarded and resolved again, every other row is kept, and the
    warm brief equals the full scan with nothing resolved on the request
    path."""
    every = _fill_world(world)
    moved: list[bool] = []

    def swap_file() -> None:
        before = _stamp_of(world.repo)
        scratch = world.repo / ".notes.md.swp"
        scratch.write_text("x")
        scratch.unlink()
        moved.append(_stamp_of(world.repo) != before)

    seen = _on_fill_rows(monkeypatch, {10: swap_file})
    warm, on_request = _fill_then_warm(world, monkeypatch)
    assert moved == [True]
    assert list(warm.ids) == _full_scan(world, every)
    assert len(warm.ids) == BRIEF_MAX_POINTERS
    assert on_request == []
    assert world.briefs()[-1]["unresolved"] == 0
    report = world.state.fill_report(_repo_key(world))
    assert report is not None
    assert (report.rows, report.retried, report.lost) == (_FILL_ROWS, 1, 0)
    assert report.kept == report.resolved == _FILL_ROWS
    assert seen[0] == _FILL_ROWS + 1  # the discarded row, resolved again


@_MEMO_IS_POSIX
def test_a_fill_keeps_its_rows_when_the_index_is_rewritten(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``git status`` that rewrites the index mid-fill (a file stat-dirty
    with its content unchanged, as an IDE's poll finds it) loses one row,
    which the retry recovers: the warm brief equals the full scan."""
    every = _fill_world(world)
    index = world.repo / ".git" / "index"
    moved: list[bool] = []

    def refresh_index() -> None:
        before = _stamp_of(index)
        target = world.repo / "other.md"
        later = target.stat().st_mtime_ns + 5_000_000_000
        os.utime(target, ns=(later, later))
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=world.repo,
            check=True,
            capture_output=True,
        )
        moved.append(_stamp_of(index) != before)

    _on_fill_rows(monkeypatch, {10: refresh_index})
    warm, on_request = _fill_then_warm(world, monkeypatch)
    assert moved == [True]
    assert list(warm.ids) == _full_scan(world, every)
    assert on_request == []
    report = world.state.fill_report(_repo_key(world))
    assert report is not None
    assert (report.retried, report.lost) == (1, 0)
    assert report.kept == report.resolved == _FILL_ROWS


@_MEMO_IS_POSIX
def test_a_quiet_fill_keeps_every_row(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = _fill_world(world)
    warm, on_request = _fill_then_warm(world, monkeypatch)
    assert list(warm.ids) == _full_scan(world, every)
    assert on_request == []
    assert world.briefs()[-1]["unresolved"] == 0
    report = world.state.fill_report(_repo_key(world))
    assert report is not None
    assert (report.rows, report.resolved, report.kept) == (_FILL_ROWS,) * 3
    assert (report.retried, report.lost) == (0, 0)


@_MEMO_IS_POSIX
def test_a_fill_never_memoises_a_verdict_computed_across_a_claimed_change(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit to the path the oldest rows claim lands mid-fill and is
    undone a few rows later (the head moves away and back). The rows
    resolved while it stood read drifted, and no such reading may be
    memoised under the head the repository is back on: every row the memo
    answers afterwards reads as a full scan from an empty memo reads it,
    and the memo answers all but the rows resolved under the passing
    head."""
    every = _fill_world(world)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=world.repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def passing_commit() -> None:
        _commit_touching(world.repo, "passing", when=_FUTURE, filename="other.md")

    def undo() -> None:
        subprocess.run(
            ["git", "reset", "--hard", head],
            cwd=world.repo,
            check=True,
            capture_output=True,
        )

    # The fill takes B first (newest first), then A: rows 25 to 40 cite
    # other.md.
    _on_fill_rows(monkeypatch, {28: passing_commit, 31: undo})
    assert world.brief().stdout == ""
    assert world.state.wait_for_fills()

    rows = [world.store.load_one(m.id) for m in reversed(every)]
    now = datetime.now(timezone.utc)
    stale = world.config.behavior.verification_stale_days
    memoised = memoised_verdicts(
        rows,
        caller_origin=world.caller,
        now=now,
        stale_after_days=stale,
        admission=_counted(rows),
    )
    response_module._clear_drift_memo()
    full = {
        v.id: v
        for v in resolve_verdicts(
            rows,
            caller_origin=world.caller,
            now=now,
            stale_after_days=stale,
            admission=_counted(rows),
        )
    }
    assert sum(1 for v in full.values() if v.staleness_verdict == "fresh") == 16
    answered = [v for v in memoised if not isinstance(v, UnresolvedVerdict)]
    assert [v for v in answered if v != full[v.id]] == []
    assert len(rows) - len(answered) <= 3
    report = world.state.fill_report(_repo_key(world))
    assert report is not None
    assert report.retried == 2


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
        after_update = world.brief(session_id="transcript-brief-2")
    assert calls == []
    assert after_update.ids == (kept.id,)

    # Re-verified, it keys on new anchors the memo cannot answer: the
    # warm call resolves it on the request path, back in at once.
    world.store.mark_verified(edited.id)
    assert set(world.brief(session_id="transcript-brief-3").ids) == {
        kept.id,
        edited.id,
    }


def test_a_failed_fill_is_logged(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(memories: Any, **kw: Any) -> Any:
        raise RuntimeError("git went away")

    memory = world.fresh("the widget rule lives in notes.md")
    monkeypatch.setattr(recall_module, "fill_verdicts", broken)
    with caplog.at_level("WARNING", logger="bettermemory.recall"):
        assert world.state.schedule_fill(
            "repo",
            [memory],
            caller_origin=world.caller,
        )
        assert world.state.wait_for_fills()
    assert "brief fill for repo failed: RuntimeError: git went away" in caplog.text


def test_the_brief_tables_are_bounded(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recall_module, "fill_verdicts", lambda m, **kw: FillReport(rows=len(m))
    )
    memory = world.fresh("the widget rule lives in notes.md")
    state = RecallState(background=True)
    for i in range(recall_module.STATE_MAX_BRIEF_KEYS + 10):
        state.note_brief_head(f"repo-{i}", "head")
        state.schedule_fill(
            f"repo-{i}",
            [memory],
            caller_origin=world.caller,
        )
    assert state.wait_for_fills()
    assert len(state._heads) == recall_module.STATE_MAX_BRIEF_KEYS
    assert len(state._fills) <= recall_module.STATE_MAX_BRIEF_KEYS
    assert not state.brief_is_warm("repo-0", "head")
    assert state.brief_is_warm(f"repo-{recall_module.STATE_MAX_BRIEF_KEYS + 9}", "head")
