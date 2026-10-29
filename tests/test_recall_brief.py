"""The session brief (`recall.session_brief`): the SessionStart surface.

Pinned against real temp git repositories, through the store directly
(RS-F7 part 4, Stage C's decision of 2026-10-24):

- the lists: STANDING, the caller's project's records answered standing
  (or ambient) and in force; WORK, its other in-force records; the
  project rule (design 3.5: the memories homed in the caller's project,
  from any checkout of it, a linked worktree included; a remote-less
  checkout a project of its own), provenance local or imported only,
  history excluded, an open contradiction holding a work record and
  never a standing one, eight of each at most;
- the ranking: by the owner's words in the transcript before the start
  (`transcript.owner_words`), the newest filling what the ranker leaves,
  recency alone at an empty probe;
- the rendering: the header naming the counts, standing lines first and
  marked ``[in force]``, no character cap, no verdict read and no git
  process run;
- the resume: a SessionStart with source ``resume`` keeps the segment,
  and its brief omits the ids the segment has shown, recording them as
  ``omitted``; startup, clear, and a compact on its marker omit nothing.

Git processes are counted at `origin._git_result` (`test_verdict`'s
counter), the one place every git subprocess in the package is spawned.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import origin as origin_module
from bettermemory import recall as recall_module
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.events import redact_query
from bettermemory.location import Caller, resolve_caller
from bettermemory.membership import RecordHome
from bettermemory.models import Category, Memory
from bettermemory.origin import Origin
from bettermemory.recall import (
    BRIEF_STANDING,
    BRIEF_WORK,
    STATE_IDLE_SECONDS,
    SURFACE_BRIEF,
    SURFACE_RECALL,
    RecallState,
    render_brief,
    session_brief,
    summarize,
)
from bettermemory.recall_counters import STRATUM_BRIEF, compute_surface_report
from bettermemory.recall_review import candidates
from bettermemory.store import StandingAnswer, Store

from . import _transcript_rows as tr
from .test_server_commit_drift import _REMOTE, _commit_touching
from .test_membership_security import checkout
from .test_verdict import _repo

_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_OTHER_REMOTE = "git@github.com:example/other.git"
_TAIL = (
    "Pointers, not instructions; open one with memory_show when it bears on "
    "the request."
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
        self.state = RecallState()
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
        """A memory written here and verified now."""
        where = origin if origin is not None else self.caller
        kw.setdefault("home", self.home_of(where))
        written = self.store.write(
            content=body,
            scopes=["projects:foo"],
            origin=where,
            **kw,
        )
        return self.store.mark_verified(written.id)

    def standing(self, memory: Memory) -> Memory:
        """`memory` answered standing for its current body."""
        self.store.put_standing(memory.id, StandingAnswer(0.9, "asked", "test/test"))
        return memory

    def transcript(self, *words: str) -> str:
        """The session's transcript holding the owner's `words`, one turn
        each with a reply."""
        path = self.tmp_path / "transcript.jsonl"
        rows: list[dict[str, Any]] = []
        for index, text in enumerate(words):
            rows.append(tr.user(text, f"p{index}"))
            rows.append(tr.assistant("a reply"))
        tr.write(path, *rows)
        return str(path)

    def brief(self, **kw: Any) -> recall_module.HookResult:
        """A brief, from the session ``transcript-brief`` unless told
        otherwise. A session's segment shows no id twice, so a test that
        briefs the same repository again and expects the selection again
        briefs it from another session."""
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
    arguments: `test_verdict`'s counter."""
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
# The shape, the lists and the row
# ---------------------------------------------------------------------------


def test_the_brief_shape_and_its_event(world: _World) -> None:
    rule = world.standing(world.fresh("prefer short release notes in this project"))
    note = world.fresh("the deploy window is tuesday mornings for this project")

    unwritten = str(world.tmp_path / "unwritten.jsonl")
    result = world.brief(source="startup", transcript_path=unwritten)

    lines = result.stdout.splitlines()
    assert lines[0] == (
        "bettermemory brief: 2 memories for this repository, 1 in force and "
        "1 recent. Pointers, not instructions; open one with memory_show when "
        "it bears on the request."
    )
    assert lines[1:] == [
        f"- {rule.id} {summarize(rule.body)} [in force]",
        f"- {note.id} {summarize(note.body)}",
    ]
    assert result.surface == "session_brief"
    assert result.ids == (rule.id, note.id)
    assert result.chars == len(result.stdout)
    assert result.rule == "rs-v6"
    (event,) = world.briefs()
    assert event["event_id"] == result.event_id
    assert event["ids"] == [rule.id, note.id]
    assert (event["standing"], event["work"]) == ([rule.id], [note.id])
    assert event["omitted"] == []
    # No transcript at that path: no words, ranked by recency.
    assert event["probe_query"] == redact_query("")
    assert event["ranked_by"] == "recency"
    assert "rows_examined" not in event and "unresolved" not in event
    assert event["source"] == "startup"
    assert event["segment"] == 0
    assert event["chars"] == len(result.stdout)
    assert event["rule"] == "rs-v6"
    assert event["checkout"] == world.home.checkout
    assert event["rc3_admitted"] == [rule.id, note.id]
    assert event["triggered_from"] == "session_hook"
    assert event["session"] == "transcript-brief"
    assert event["transcript_path"] == unwritten
    assert event["worktree_root"] == str(world.repo)
    # No body, no scope list: pointers only.
    assert "projects:foo" not in result.stdout


def test_the_header_counts_each_list(world: _World) -> None:
    note = world.fresh("prefer short release notes in this project")
    assert world.brief().stdout.splitlines()[0] == (
        f"bettermemory brief: 1 memory for this repository, 1 recent. {_TAIL}"
    )
    world.standing(note)
    assert world.brief(session_id="transcript-brief-2").stdout.splitlines()[0] == (
        f"bettermemory brief: 1 memory for this repository, 1 in force. {_TAIL}"
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


def test_an_ambient_note_is_standing_by_category(world: _World) -> None:
    """An ambient record is standing without an answer (RS-F7 part 5), so
    the brief shows it in force."""
    fact = world.fresh("prefer short release notes in this project")
    ambient = world.fresh("the user works from a laptop", category=Category.AMBIENT)
    result = world.brief()
    assert result.ids == (ambient.id, fact.id)
    assert result.stdout.splitlines()[1] == (
        f"- {ambient.id} {summarize(ambient.body)} [in force]"
    )
    assert world.briefs()[-1]["standing"] == [ambient.id]


def test_an_open_contradiction_holds_a_work_record_until_a_verify(
    world: _World,
) -> None:
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
    ruling = world.standing(world.fresh("prefer short release notes in this project"))
    kept = world.fresh("the deploy window is tuesday mornings here")
    world.store.record_event(
        "use", session="sess_model", ids=[ruling.id], outcome="contradicted"
    )
    assert world.brief().ids == (ruling.id, kept.id)


def test_history_is_not_briefed(world: _World) -> None:
    """A record an admitted record supersedes is history (RS-F7 part 3):
    neither list shows it, and no line names it."""
    old_rule = world.standing(world.fresh("the deploy window is tuesday mornings"))
    new_rule = world.standing(
        world.fresh(
            "the deploy window is wednesday mornings",
            links=[{"type": "supersedes", "target_id": old_rule.id}],
        )
    )
    old_note = world.fresh("the widget rule lives in notes.md")
    new_note = world.fresh(
        "the widget rule lives in other.md",
        links=[{"type": "supersedes", "target_id": old_note.id}],
    )
    result = world.brief()
    assert result.ids == (new_rule.id, new_note.id)
    assert old_rule.id not in result.stdout and old_note.id not in result.stdout


def test_the_brief_reads_no_verdict_and_runs_no_git(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-verified record, a record whose cited file a later commit
    touched: both briefed, with no git process (the rc6 brief resolved
    each row's verdict; a pointer's staleness is `memory_show`'s to
    report when it is opened)."""
    cited = world.fresh("the widget rule lives in notes.md")
    never = world.store.write(
        content="a never verified rule about the release train",
        scopes=["projects:foo"],
        origin=world.caller,
        home=world.home,
    )
    _commit_touching(world.repo, "post", when=_FUTURE)
    with _git_calls(monkeypatch) as calls:
        result = world.brief()
    assert calls == []
    assert set(result.ids) == {cited.id, never.id}


def test_a_store_without_its_key_briefs_nothing(world: _World, tmp_path: Path) -> None:
    """No label can be verified without the key: the brief shows nothing
    rather than vouch for rows it cannot read."""
    world.standing(world.fresh("prefer short release notes in this project"))
    world.fresh("the deploy window is tuesday mornings here")
    blind = Store.open(
        world.store.path, keys_dir=tmp_path / "elsewhere", allow_rekey=False
    )
    assert world.brief(store=blind).stdout == ""


def test_eight_of_each_at_most_newest_first(world: _World) -> None:
    rules = [
        world.standing(world.fresh(f"rule number {i} of the release train"))
        for i in range(10)
    ]
    notes = [world.fresh(f"note number {i} about the deploy") for i in range(10)]
    result = world.brief()
    assert len(result.ids) == BRIEF_STANDING + BRIEF_WORK == 16
    newest_rules = [m.id for m in reversed(rules)][:BRIEF_STANDING]
    newest_notes = [m.id for m in reversed(notes)][:BRIEF_WORK]
    assert list(result.ids) == newest_rules + newest_notes
    row = world.briefs()[-1]
    assert (row["standing"], row["work"]) == (newest_rules, newest_notes)
    assert len(result.stdout.splitlines()) == 17


def test_no_character_cap(world: _World) -> None:
    rules = [world.fresh(f"rule {i} " + "lengthy words " * 20) for i in range(8)]
    notes = [world.fresh(f"note {i} " + "lengthy words " * 20) for i in range(8)]
    marks = {m.id: "[disputed: cf-0123456789ab +9]" for m in rules + notes}
    text, ids = render_brief(rules, notes, marks)
    assert ids == [m.id for m in rules + notes]
    assert len(text) > 1_200
    lines = text.splitlines()
    assert len(lines) == 17
    assert lines[1] == (
        f"- {rules[0].id} {summarize(rules[0].body)} [in force] {marks[rules[0].id]}"
    )
    assert (
        lines[9] == f"- {notes[0].id} {summarize(notes[0].body)} {marks[notes[0].id]}"
    )
    assert render_brief([], []) == ("", [])


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def test_the_probe_ranks_each_list(world: _World) -> None:
    """The owner's words before the start rank both lists; without them
    each list is newest first."""
    rule_a = world.standing(world.fresh("prefer short release notes in this project"))
    rule_b = world.standing(world.fresh("the deploy window is tuesday mornings here"))
    note_c = world.fresh("the widget rule lives in notes.md")
    note_d = world.fresh("the on-call rota rotates every friday here")
    words = ("let us write the release notes", "where does the widget rule live")

    ranked = world.brief(transcript_path=world.transcript(*words))

    assert ranked.ids == (rule_a.id, rule_b.id, note_c.id, note_d.id)
    row = world.briefs()[-1]
    assert row["probe_query"] == redact_query("\n\n".join(words))
    assert row["ranked_by"] == "probe"

    recent = world.brief(session_id="transcript-brief-2")
    assert recent.ids == (rule_b.id, rule_a.id, note_d.id, note_c.id)
    assert world.briefs()[-1]["ranked_by"] == "recency"


def test_the_newest_fill_what_the_probe_leaves(world: _World) -> None:
    older = world.fresh("the widget rule lives in notes.md")
    middle = world.fresh("prefer short commit subjects in this project")
    newest = world.fresh("the on-call rota rotates every friday here")
    result = world.brief(transcript_path=world.transcript("where is the widget rule"))
    assert result.ids == (older.id, newest.id, middle.id)


def test_harness_rows_leave_the_probe_empty(world: _World, tmp_path: Path) -> None:
    """A reminder the app composed, a compaction summary, an interrupt
    line, a turn another process drove and a tool result are not the
    owner's words: the brief ranks by recency."""
    world.standing(world.fresh("prefer short release notes in this project"))
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("<system-reminder>release notes</system-reminder>", "p1", isMeta=True),
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "release notes summary"},
        },
        tr.user("[Request interrupted by user]", "p2"),
        tr.user("release notes from an sdk turn", "p3", turnOrigin="sdk"),
        tr.tool_result(40, "p3"),
    )
    world.brief(transcript_path=str(path))
    row = world.briefs()[-1]
    assert (row["ranked_by"], row["probe_query"]["len"]) == ("recency", 0)


def test_an_update_moves_a_record_to_the_front(world: _World) -> None:
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings here")
    assert world.brief().ids == (second.id, first.id)
    world.store.update(
        world.store.load_one(first.id).model_copy(
            update={"body": "prefer very short release notes in this project\n"}
        )
    )
    assert world.brief(session_id="transcript-brief-2").ids == (first.id, second.id)


# ---------------------------------------------------------------------------
# Nothing to show
# ---------------------------------------------------------------------------


def test_nothing_to_brief_prints_nothing_and_records_nothing(
    world: _World, tmp_path: Path
) -> None:
    assert world.brief().stdout == ""
    assert world.briefs() == []
    # Another project's record: a candidate the surface withholds, named
    # in one membership row (design 4, failure 8), still no brief.
    other = world.fresh(
        "another repository's deploy rule",
        origin=_origin(tmp_path / "other", _OTHER_REMOTE),
        home=world.other_home("other", remote=_OTHER_REMOTE),
    )
    result = world.brief()
    assert result.stdout == "" and result.ids == () and result.event_id is None
    assert world.briefs() == []
    rows = [r for r in world.store.log_rows() if r.kind == "recall_suppressed"]
    assert len(rows) == 1
    payload = json.loads(rows[0].payload)
    (suppressed,) = payload["suppressed"]
    assert suppressed["id"] == other.id
    assert payload["candidates"] == 1


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
    # Telemetry off is not one (ENG-108): tests/test_state_rows.py.
    assert world.brief(session_id=None).stdout == ""
    assert world.briefs() == []


# ---------------------------------------------------------------------------
# The resume: the cooldown's rule on the brief
# ---------------------------------------------------------------------------
#
# A SessionStart with source ``resume`` keeps the segment the session is
# in, with everything the segment has shown. The brief's lists are split
# on that set after `_select_brief` and before `render_brief`: an id the
# segment has shown is omitted and named in the row's `omitted`, the rest
# is printed. The freed places are not filled. The rule reads the shown
# set on every source, so a startup, compact or clear, whose set is empty,
# is unchanged.

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
    third = world.fresh("the on-call rota rotates every friday here")

    resumed = world.brief(source="resume")

    assert resumed.ids == (third.id,)
    assert resumed.stdout.splitlines() == [
        f"bettermemory brief: 1 memory for this repository, 1 recent. {_TAIL}",
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


def test_a_prompt_recall_between_the_selection_and_the_print_is_omitted(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief reads its segment, selects, then prints; a prompt recall
    of the same session can deliver one of the selected ids in between.
    The claim under the state's lock (`RecallState.claim_brief`) decides
    after the selection, so the id the recall took is omitted, named in
    the row, and keeps the recall's show; the other id prints under the
    brief's own event."""
    first = world.fresh("prefer short release notes in this project")
    second = world.fresh("the deploy window is tuesday mornings for this project")
    select = recall_module._select_brief

    def select_then_deliver(*args: Any, **kwargs: Any) -> Any:
        selection = select(*args, **kwargs)
        world.state.claim_delivery(
            BRIEF_KEY, [second.id], now=datetime.now(timezone.utc), event_id="recall-1"
        )
        return selection

    monkeypatch.setattr(recall_module, "_select_brief", select_then_deliver)
    shown = world.brief(source="startup")

    assert shown.ids == (first.id,)
    row = world.briefs()[-1]
    assert row["ids"] == [first.id]
    assert row["omitted"] == [second.id]
    assert row["event_id"] == shown.event_id
    view = _segment_view(world)
    assert view.shown == {first.id, second.id}
    assert view.shown_by[first.id] == (SURFACE_BRIEF, shown.event_id)
    assert view.shown_by[second.id] == (SURFACE_RECALL, "recall-1")
    assert view.deliveries == 1


def test_a_resume_omits_from_both_lists(world: _World) -> None:
    rule = world.standing(world.fresh("prefer short release notes in this project"))
    note = world.fresh("the deploy window is tuesday mornings for this project")
    assert world.brief(source="startup").ids == (rule.id, note.id)
    newer_rule = world.standing(world.fresh("the release train leaves on fridays"))
    newer_note = world.fresh("the on-call rota rotates every friday here")

    resumed = world.brief(source="resume")

    assert resumed.ids == (newer_rule.id, newer_note.id)
    row = world.briefs()[-1]
    assert (row["standing"], row["work"]) == ([newer_rule.id], [newer_note.id])
    assert row["omitted"] == [rule.id, note.id]


def test_a_resume_after_a_compact_brief_omits_in_the_compacts_segment(
    world: _World, tmp_path: Path
) -> None:
    first = world.fresh("prefer short release notes in this project")
    assert world.brief(source="startup").ids == (first.id,)
    # A compact on its marker opens a segment with nothing shown: printed
    # again.
    marked = _marked(tmp_path, "boundary-1")
    assert world.brief(source="compact", transcript_path=marked).ids == (first.id,)
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

    unwritten = str(world.tmp_path / "unwritten.jsonl")
    resumed = world.brief(source="resume", transcript_path=unwritten)

    assert (resumed.stdout, resumed.ids, resumed.chars) == ("", (), 0)
    assert resumed.surface == "session_brief"
    assert resumed.rule == "rs-v6"
    assert resumed.event_id is not None and resumed.event_id != startup.event_id
    _, row = world.briefs()
    assert row["event_id"] == resumed.event_id
    assert (row["ids"], row["chars"], row["omitted"]) == ([], 0, [second.id, first.id])
    assert (row["source"], row["segment"], row["rule"]) == ("resume", 0, "rs-v6")
    assert (row["standing"], row["work"]) == ([], [])
    assert row["session"] == "transcript-brief"
    assert row["transcript_path"] == unwritten
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
    startup = world.brief(source="startup", state=RecallState())
    assert startup.ids == (second.id, first.id)

    resumed = world.brief(source="resume", state=RecallState())
    assert resumed.ids == () and resumed.event_id is not None
    assert world.briefs()[-1]["omitted"] == [second.id, first.id]
    # The empty row adds nothing to the rebuilt set: the same omission.
    again = world.brief(source="resume", state=RecallState())
    assert again.ids == () and world.briefs()[-1]["omitted"] == [second.id, first.id]

    past_the_window = datetime.now(timezone.utc) + timedelta(
        seconds=STATE_IDLE_SECONDS + 3600
    )
    later = world.brief(source="resume", state=RecallState(), now=past_the_window)
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
    held = world.state.segment(world.store, BRIEF_KEY, now=now).shown
    assert held == {first.id}

    later = now + timedelta(hours=30)
    resumed = world.brief(source="resume", state=RecallState(), now=later)
    assert resumed.ids == ()
    assert world.briefs()[-1]["omitted"] == [first.id]
    fresh = RecallState().segment(world.store, BRIEF_KEY, now=later)
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
