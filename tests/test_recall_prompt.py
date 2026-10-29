"""The prompt recall (`recall.prompt_recall`): the UserPromptSubmit surface.

The probe is the audit's (`recall._probe_message`), unchanged, so this file
does not re-test ranking. What it pins is delivery: the prompt the probe
reads (pasted blocks removed, 2,000 characters), the floor and the
acknowledgment gate, the candidate gates and their recorded reasons, up
to three pointers, the block's format and its 900-character cap, the
three `prompt_recall` modes, and the telemetry gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig, TelemetryConfig
from bettermemory.location import Caller, resolve_caller
from bettermemory.membership import PROMPT_RECALL, SEARCH_UNSCOPED, RecordHome
from bettermemory.models import Memory
from bettermemory.origin import Origin
from bettermemory.project_file import PROJECT_FILE_NAME
from bettermemory.recall import (
    PROMPT_CUT_CHARS,
    RECALL_CAP_CHARS,
    HookResult,
    RecallPointer,
    RecallState,
    clean_prompt,
    prompt_recall,
    render_recall_block,
    summarize,
)
from bettermemory.store import StandingAnswer, Store

from .test_membership_security import checkout, project_file

REPO = "git@github.com:owner/foo.git"
OTHER_REPO = "git@github.com:owner/bar.git"
BODY = "backup strategy uses triangular restic replication"
QUERY = "backup strategy"


def config(store_dir: Path, *, mode: str = "project", telemetry: bool = True) -> Config:
    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        telemetry=TelemetryConfig(enabled=telemetry),
        behavior=BehaviorConfig(prompt_recall=mode),
    )


class PromptWorld:
    """A store, a checkout (`worktree`) the caller stands in, and helpers
    to seed memories the probe can rank (backdated past its creation
    shield) and to run the recall.

    A memory is delivered by its home (design 3.5), which `write` gives it
    from the origin it is written with: the checkout's project for the
    caller's own origin, ``global`` for none or for a directory outside
    any repository, and the project of another checkout (made on first
    use, one per directory) for any other origin. The origin stays on the
    record as data."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store_dir = tmp_path / "store"
        self.store = Store(self.store_dir)
        self.worktree = checkout(tmp_path / "foo")
        self.caller = Origin(
            cwd=str(self.worktree), repo=REPO, worktree_root=str(self.worktree)
        )
        self.config = config(self.store_dir)
        # A checkout seen before: its first sight's line is not what these
        # tests read.
        self.home = self.resolve(self.worktree).home()
        self._homes: dict[str, RecordHome] = {}

    def resolve(self, where: Path | None) -> Caller:
        """The caller standing at `where` (None: nothing readable)."""
        return resolve_caller(
            self.store, self.config, None if where is None else str(where)
        )

    def global_home(self) -> RecordHome:
        """The home of a record written outside any project."""
        plain = self.tmp_path / "plain"
        plain.mkdir(exist_ok=True)
        return self.resolve(plain).home()

    def other_home(self, name: str = "bar") -> RecordHome:
        """The home of a record written in another checkout, `name`, its
        own project."""
        if name not in self._homes:
            other = checkout(self.tmp_path / "others" / name)
            self._homes[name] = self.resolve(other).home()
        return self._homes[name]

    def tenant_home(self, name: str = "client") -> RecordHome:
        """The home of a record written in a checkout whose project file
        declares a boundary: a tenant of its own (design E34, B9)."""
        key = f"tenant:{name}"
        if key not in self._homes:
            other = checkout(
                self.tmp_path / "others" / name,
                files={PROJECT_FILE_NAME: project_file(name, boundary=True)},
            )
            self._homes[key] = self.resolve(other).home()
        return self._homes[key]

    def home_of(self, origin: Origin | None) -> RecordHome:
        """The home a record written with `origin` has (see the class)."""
        if origin is None:
            return self.global_home()
        if str(self.worktree) in (origin.worktree_root, origin.cwd):
            return self.home
        if origin.repo is None and origin.worktree_root is None:
            return self.global_home()
        where = origin.worktree_root or origin.cwd or "bar"
        return self.other_home(Path(where).name or "bar")

    def write(
        self,
        body: str = BODY,
        *,
        origin: Origin | None | str = "here",
        provenance: str | None = None,
        scopes: list[str] | None = None,
        home: RecordHome | None = None,
        links: list[dict[str, str]] | None = None,
    ) -> Memory:
        where = self.caller if origin == "here" else origin
        assert not isinstance(where, str)
        written = self.store.write(
            content=body,
            scopes=scopes or ["projects:foo"],
            origin=where,
            home=home if home is not None else self.home_of(where),
            links=links,
        )
        stamp = datetime.now(timezone.utc) - timedelta(hours=1)
        return self.store.put_memory(
            written.model_copy(update={"created": stamp, "updated": stamp}),
            provenance=provenance,
        )

    def recall(self, prompt: str = QUERY, **kw: Any) -> HookResult:
        kw.setdefault("session_id", "transcript-recall")
        kw.setdefault("origin", self.caller)
        kw.setdefault("config", self.config)
        kw.setdefault("store", self.store)
        return prompt_recall(prompt=prompt, **kw)

    def recalls(self) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e["kind"] == "prompt_recall"]


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


# ---------------------------------------------------------------------------
# The prompt the probe reads
# ---------------------------------------------------------------------------


def test_clean_prompt_removes_pasted_blocks_and_cuts() -> None:
    text = (
        'see <pasted_content id="1" lines="40">backup strategy secrets\n'
        "more</pasted_content> and <pasted_content ref=2 /> here"
    )
    cleaned = clean_prompt(text)
    assert "backup" not in cleaned and "pasted_content" not in cleaned
    assert cleaned.startswith("see ") and cleaned.endswith(" here")
    assert len(clean_prompt("x" * 5_000)) == PROMPT_CUT_CHARS


def test_a_pasted_block_is_not_probed(world: PromptWorld) -> None:
    world.write()
    pasted = f"<pasted_content>{QUERY}</pasted_content> what does this say"
    assert world.recall(pasted).stdout == ""
    assert world.recall(f"<PASTED_CONTENT a=b>{QUERY}</pasted_content>").stdout == ""
    assert world.recalls() == []


def test_the_prompt_is_cut_at_2000(world: PromptWorld) -> None:
    world.write()
    assert world.recall(("lorem " * 400)[:PROMPT_CUT_CHARS] + QUERY).stdout == ""
    assert world.recall(QUERY + " " + "lorem " * 400).stdout != ""


@pytest.mark.parametrize("prompt", ["yes", "go for it", "looks good", "sounds good"])
def test_the_floor_and_the_acknowledgment_gate_deliver_nothing(
    world: PromptWorld, prompt: str
) -> None:
    world.write(f"{prompt} {BODY}")
    assert world.recall(prompt).stdout == ""
    assert world.recalls() == []


# ---------------------------------------------------------------------------
# The prompt the harness composed
# ---------------------------------------------------------------------------

TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>bq5</task-id>\n<status>completed</status>\n"
    f"<summary>{QUERY} check finished</summary>\n</task-notification>"
)
AGENT_MESSAGE = (
    f'<agent-message from="acf8db08" name="helper">\n{QUERY}: the restic job '
    "is ready for review\n</agent-message>"
)
CROSS_SESSION = (
    f'<cross-session-message from="uds">\n{QUERY} please\n</cross-session-message>'
)
SIDE_SESSIONS_EVENT = (
    f"<side-sessions-event>\n{QUERY} session ended\n</side-sessions-event>"
)
REMINDER = (
    "<system-reminder>\nLinked sessions now: " + "x " * 1_200 + "\n</system-reminder>"
)


@pytest.mark.parametrize(
    "prompt", [TASK_NOTIFICATION, AGENT_MESSAGE, CROSS_SESSION, SIDE_SESSIONS_EVENT]
)
def test_a_prompt_the_harness_composed_delivers_nothing_and_holds_nothing(
    world: PromptWorld, prompt: str
) -> None:
    """A task notification, a message from another session or a
    side-session note reaches the hook as the prompt; no one asked
    anything. Nothing is probed or recorded, and the id is still there
    for the owner's next prompt in the same session."""
    memory = world.write()
    assert world.recall(prompt, session_id="transcript-harness").stdout == ""
    assert world.recalls() == []
    assert world.recall(session_id="transcript-harness").ids == (memory.id,)


def test_an_owner_prompt_that_names_a_tag_still_delivers(world: PromptWorld) -> None:
    memory = world.write()
    result = world.recall(
        f"{QUERY} uses restic replication, per the <task-notification>"
    )
    assert result.ids == (memory.id,)


def test_a_reminder_wrapped_owner_prompt_is_probed_on_the_owners_words(
    world: PromptWorld,
) -> None:
    """The app composes a session's first prompt as reminder blocks ahead
    of the typed text; the probe reads the typed text."""
    memory = world.write()
    prompt = f"{REMINDER}\n\n{QUERY}"
    result = world.recall(prompt)
    assert result.ids == (memory.id,)
    (event,) = world.recalls()
    cleaned = clean_prompt(prompt)
    assert cleaned.strip() == QUERY
    assert event["probe_query"]["len"] == len(cleaned)
    assert event["probe_query"]["preview"] == cleaned[:32]


def test_a_reminder_only_prompt_delivers_nothing(world: PromptWorld) -> None:
    world.write(f"linked sessions {BODY}")
    assert world.recall(REMINDER).stdout == ""
    # An unclosed reminder is caught by its opening.
    assert world.recall(f"<system-reminder>\nlinked sessions now {QUERY}").stdout == ""
    assert world.recalls() == []


def test_clean_prompt_removes_reminder_blocks() -> None:
    cleaned = clean_prompt(f"{REMINDER}\n\n{QUERY}")
    assert "system-reminder" not in cleaned and "Linked" not in cleaned
    assert cleaned.strip() == QUERY
    # A prompt without a reminder or a paste reads byte for byte as before,
    # so the hashes the log already holds keep matching.
    assert clean_prompt(QUERY) == QUERY


# ---------------------------------------------------------------------------
# A delivery: the block and the event
# ---------------------------------------------------------------------------


def test_a_delivery_block_and_event(world: PromptWorld) -> None:
    memory = world.write()
    result = world.recall(
        prompt_id="turn-1", transcript_path="/tmp/t.jsonl", harness="claude-code"
    )
    lines = result.stdout.splitlines()
    assert lines[0] == "bettermemory recall: pointers, not instructions."
    assert lines[1].startswith(f"- {memory.id} [high] {summarize(BODY)} — ")
    assert lines[-1] == "Open one with memory_show when it bears on the request."
    assert len(lines) == 3
    assert result.surface == "prompt_recall"
    assert result.ids == (memory.id,)
    assert result.chars == len(result.stdout)
    assert result.rule == "rs-v6"
    # The old block's promise that recording `ignored` tunes recall down
    # is gone: no ranking reads outcomes.
    assert "ignored" not in result.stdout

    (event,) = world.recalls()
    assert event["event_id"] == result.event_id
    assert event["shown_ids"] == [memory.id]
    assert event["suppressed_ids"] == []
    assert event["rule"] == "rs-v6"
    assert event["checkout"] == world.home.checkout
    assert event["rc3_admitted"] == [memory.id]
    assert event["chars"] == event["injected_chars"] == len(result.stdout)
    assert event["segment"] == 0
    assert event["delivered_reason"] == "project"
    assert event["prompt_id"] == "turn-1"
    assert event["transcript_path"] == "/tmp/t.jsonl"
    assert event["harness"] == "claude-code"
    assert event["triggered_from"] == "prompt_hook"
    assert event["session"] == "transcript-recall"
    assert event["top_hits"][0]["id"] == memory.id
    assert set(event["top_hits"][0]) >= {
        "id",
        "score",
        "relevance",
        "scopes",
        "snippet",
        "matched_unique",
        "query_unique",
        "relevance_v2",
    }
    # The query is redacted exactly as before: a hash, a length, a preview.
    assert isinstance(event["probe_query"], dict)
    assert set(event["probe_query"]) == {"hash", "len", "preview"}


def test_up_to_three_pointers_in_rank_order(world: PromptWorld) -> None:
    ids = {
        world.write(f"backup strategy note {i} uses restic replication").id
        for i in range(4)
    }
    result = world.recall()
    assert len(result.ids) == 3
    assert set(result.ids) <= ids
    assert len(result.stdout.splitlines()) == 5


def test_an_imported_record_announces_itself(world: PromptWorld) -> None:
    memory = world.write(provenance="imported")
    line = world.recall().stdout.splitlines()[1]
    assert line.startswith(
        f"- {memory.id} [high] {summarize(BODY)} [provenance: imported] — "
    )


def test_the_retrieval_shield_no_longer_holds_a_delivery(world: PromptWorld) -> None:
    """The old lane went quiet for ten minutes after ANY retrieval in the
    session or worktree. The cooldown holds only what this segment has
    already seen: a search that returned another memory leaves this one
    deliverable."""
    memory = world.write()
    world.store.record_event(
        "search",
        session="sess_server",
        query="something else",
        returned=["01UNRELATEDMEMORY000000000"],
        worktree_root=str(world.worktree),
    )
    assert world.recall().ids == (memory.id,)


# ---------------------------------------------------------------------------
# The candidate gates
# ---------------------------------------------------------------------------


def _suppressed(world: PromptWorld) -> dict[str, str]:
    (event,) = world.recalls()
    return {entry["id"]: entry["reason"] for entry in event["suppressed_ids"]}


def test_a_low_label_is_delivered(world: PromptWorld) -> None:
    """rs-v6 (RS-F7 part 0): the coverage label holds nothing back; it
    stays on the line. In Stage A the labels did not separate relevant
    from irrelevant records (high 0.34, medium 0.30, low 0.28); until
    rs-v6 a low hit was held as ``low_relevance``."""
    strong = world.write("backup strategy zebra quokka uses restic")
    weak = world.write("a backup elsewhere entirely")
    result = world.recall("backup strategy zebra quokka")
    assert result.ids == (strong.id, weak.id)
    assert result.stdout.splitlines()[2].startswith(f"- {weak.id} [low] ")
    (event,) = world.recalls()
    assert event["suppressed_ids"] == []


def test_another_project_and_a_global_are_held(world: PromptWorld) -> None:
    mine = world.write()
    # A global naming no project: the probe's pool admits it, the
    # recall does not.
    stray = world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    result = world.recall()
    assert result.ids == (mine.id,)
    assert _suppressed(world) == {stray.id: "other_project"}


def test_a_global_naming_this_project_is_delivered(world: PromptWorld) -> None:
    """RS-F7 part 2: a record written outside any project whose scopes
    name the caller's project is delivered (Stage A: 0.62 relevant in the
    top 3, against 0.40 for the project's own)."""
    named = world.write(f"{BODY} globally", origin=None, scopes=["projects:foo"])
    assert world.recall().ids == (named.id,)
    (event,) = world.recalls()
    assert event["suppressed_ids"] == []


def test_a_global_naming_another_project_or_none_is_held(world: PromptWorld) -> None:
    """A global record naming only another project, or none, stays held
    (Stage A: relevant 0.12 either way)."""
    world.other_home("bar")
    theirs = world.write(f"{BODY} for bar", origin=None, scopes=["projects:bar"])
    loose = world.write(f"{BODY} anywhere", origin=None, scopes=["tools"])
    mine = world.write()
    assert world.recall().ids == (mine.id,)
    (event,) = world.recalls()
    held = {e["id"]: (e["reason"], e.get("relation")) for e in event["suppressed_ids"]}
    assert held == {
        theirs.id: ("other_project", "global"),
        loose.id: ("other_project", "global"),
    }


def test_provenance_other_than_local_or_imported_is_held(world: PromptWorld) -> None:
    mine = world.write()
    pulled = world.write(f"{BODY} pulled", provenance="synced")
    assert world.recall().ids == (mine.id,)
    assert _suppressed(world) == {pulled.id: "provenance"}


def test_a_store_without_its_key_delivers_nothing(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    blind = Store.open(
        world.store.path, keys_dir=tmp_path / "elsewhere", allow_rekey=False
    )
    assert world.recall(store=blind).stdout == ""


def test_an_open_contradiction_is_held_until_an_update_or_verify(
    world: PromptWorld,
) -> None:
    held = world.write()
    kept = world.write(f"{BODY} too")
    world.store.record_event(
        "use", session="sess_model", ids=[held.id], outcome="contradicted"
    )
    assert world.recall().ids == (kept.id,)
    assert _suppressed(world) == {held.id: "contradicted"}
    world.store.mark_verified(held.id)
    assert held.id in world.recall(session_id="another-session").ids


def test_a_contradiction_does_not_hold_a_standing_record(
    world: PromptWorld,
) -> None:
    """A ``contradicted`` use is the agent's word: it does not take the
    user's rule out of the prompt recall (unit RS-F7 part 5)."""
    ruling = world.write()
    world.store.put_standing(ruling.id, StandingAnswer(0.9, "asked", "test/test"))
    world.store.record_event(
        "use", session="sess_model", ids=[ruling.id], outcome="contradicted"
    )
    assert ruling.id in world.recall().ids


def test_retrieved_in_the_segment_is_held(world: PromptWorld) -> None:
    """A search hit under any session stamped with this worktree, and a
    show under the harness session, are seen: the model already has them."""
    searched = world.write()
    shown = world.write(f"{BODY} shown")
    fresh = world.write(f"{BODY} untouched")
    world.store.record_event(
        "search",
        session="sess_server",
        query=QUERY,
        returned=[searched.id],
        worktree_root=str(world.worktree),
    )
    world.store.record_event("show", session="transcript-recall", id=shown.id)
    assert world.recall().ids == (fresh.id,)
    assert _suppressed(world) == {
        searched.id: "seen_in_segment",
        shown.id: "seen_in_segment",
    }


def test_written_in_the_segment_is_held(world: PromptWorld) -> None:
    written = world.write()
    fresh = world.write(f"{BODY} untouched")
    world.store.record_event(
        "write", session="transcript-recall", status="duplicate", id=fresh.id
    )
    world.store.record_event(
        "write", session="transcript-recall", status="committed", id=written.id
    )
    assert world.recall().ids == (fresh.id,)
    assert _suppressed(world) == {written.id: "seen_in_segment"}


def test_updated_in_the_segment_is_held(world: PromptWorld) -> None:
    """A memory the session only updated is seen, like one it wrote; an
    update a gate refused (it carries a status) changed nothing."""
    updated = world.write()
    refused = world.write(f"{BODY} refused")
    world.store.record_event(
        "update", session="transcript-recall", status="transient_warning", id=refused.id
    )
    world.store.record_event(
        "update", session="transcript-recall", id=updated.id, fields=["content"]
    )
    assert world.recall().ids == (refused.id,)
    assert _suppressed(world) == {updated.id: "seen_in_segment"}


def test_an_update_in_the_worktree_holds_the_pointer_back(world: PromptWorld) -> None:
    """The worktree half reads an update under another session, as it
    reads a write."""
    memory = world.write()
    world.store.record_event(
        "update",
        session="sess_server",
        id=memory.id,
        fields=["scopes"],
        worktree_root=str(world.worktree),
    )
    assert world.recall().stdout == ""


def test_another_worktree_retrieval_does_not_count(world: PromptWorld) -> None:
    """A retrieval stamped with another checkout, under another server
    session, is another conversation's. (This worktree's own server
    session is on record, so the hooks' bridge anchors to it rather than
    falling back to the latest session anywhere.)"""
    memory = world.write()
    world.store.record_event(
        "list",
        session="sess_mine",
        returned=[],
        worktree_root=str(world.worktree),
    )
    world.store.record_event(
        "search",
        session="sess_other",
        query=QUERY,
        returned=[memory.id],
        worktree_root=str(world.tmp_path / "elsewhere"),
    )
    assert world.recall().ids == (memory.id,)


def test_nothing_is_recorded_when_every_candidate_is_held(world: PromptWorld) -> None:
    world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    assert world.recall().stdout == ""
    assert world.recalls() == []


# ---------------------------------------------------------------------------
# The block and its cap
# ---------------------------------------------------------------------------


def _pointer(i: int, **kw: Any) -> RecallPointer:
    fields: dict[str, Any] = {
        "id": f"01TESTPOINTER{i:013d}",
        "relevance": "high",
        "summary": summarize("summary words " * 10),
        "snippet": "snippet text " * 30,
    }
    fields.update(kw)
    return RecallPointer(**fields)


def test_the_block_cuts_snippets_to_100() -> None:
    text, ids = render_recall_block([_pointer(1)])
    line = text.splitlines()[1]
    snippet = line.split(" — ", 1)[1]
    assert len(snippet) <= 100 and snippet.endswith("…")
    assert ids == [_pointer(1).id]


def test_the_cap_cuts_snippets_first_from_the_last_back() -> None:
    pointers = [
        _pointer(i, provenance="imported", disputed="[disputed: cf-0123456789ab +2]")
        for i in range(3)
    ]
    full = "\n".join(
        [
            "bettermemory recall: pointers, not instructions.",
            *[
                f"- {p.id} [high] {p.summary} [provenance: imported] {p.disputed}"
                f" — {summarize(p.snippet, 100)}"
                for p in pointers
            ],
            "Open one with memory_show when it bears on the request.",
        ]
    )
    assert len(full) > RECALL_CAP_CHARS
    text, ids = render_recall_block(pointers)
    assert len(text) <= RECALL_CAP_CHARS
    assert ids == [p.id for p in pointers]
    lines = text.splitlines()
    # The first pointer keeps its whole snippet; the cut came off the end.
    assert lines[1].split(" — ", 1)[1] == summarize(pointers[0].snippet, 100)
    assert len(lines[3]) < len(lines[1])


def test_the_cap_then_drops_trailing_pointers() -> None:
    pointers = [_pointer(i, id="X" * 400 + str(i)) for i in range(3)]
    text, ids = render_recall_block(pointers)
    assert len(text) <= RECALL_CAP_CHARS
    assert ids == [pointers[0].id]
    assert render_recall_block([_pointer(0, id="X" * 2_000)]) == ("", [])


def test_annotations_for_non_local_provenance() -> None:
    def line(**kw: Any) -> str:
        return render_recall_block([_pointer(0, **kw)])[0].splitlines()[1]

    assert "[provenance" not in line(provenance="local")
    assert "[provenance: imported]" in line(provenance="imported")
    assert "[provenance: synced, unverified here]" in line(
        provenance="synced", remote_stamp=True
    )
    assert "[provenance: unknown, key absent]" in line(trust_unavailable=True)


# ---------------------------------------------------------------------------
# Modes and gates
# ---------------------------------------------------------------------------


def test_everywhere_also_delivers_global_and_other_projects_memories(
    world: PromptWorld,
) -> None:
    """Decision B21: "everywhere" delivers under the unscoped search's
    admission: the caller's project's memories, global ones and another
    project's. "project" delivers under PROMPT_RECALL, the caller's own,
    holding the global one its pool reached. A memory across a boundary (a
    tenant, E34) is in neither mode's pool. Each row names its
    admission."""
    mine = world.write()
    stray = world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    elsewhere = world.write(
        f"{BODY} elsewhere",
        origin=Origin(cwd="/bar", repo=OTHER_REPO, worktree_root="/bar"),
    )
    tenant = world.write(
        f"{BODY} behind a boundary",
        origin=Origin(cwd="/client", worktree_root="/client"),
        home=world.tenant_home(),
    )
    wide = world.recall(config=config(world.store_dir, mode="everywhere"))
    assert set(wide.ids) == {mine.id, stray.id, elsewhere.id}
    (event,) = world.recalls()
    assert event["delivered_reason"] == "everywhere"
    assert event["admission"] == SEARCH_UNSCOPED
    assert tenant.id not in {entry["id"] for entry in event["suppressed_ids"]}

    narrow = world.recall(
        session_id="another-session", config=config(world.store_dir, mode="project")
    )
    assert narrow.ids == (mine.id,)
    event = world.recalls()[1]
    assert event["delivered_reason"] == "project"
    assert event["admission"] == PROMPT_RECALL
    held = {entry["id"]: entry for entry in event["suppressed_ids"]}
    assert held[stray.id]["reason"] == "other_project"
    assert held[stray.id]["relation"] == "global"
    assert elsewhere.id not in held and tenant.id not in held


def test_outside_a_repository_only_everywhere_delivers(world: PromptWorld) -> None:
    """Outside any project "project" delivers nothing (PROMPT_RECALL
    admits nothing there, design 3.5) and "everywhere" delivers what an
    unscoped search reaches there (decision B21), a memory written where
    the caller stands included."""
    memory = world.write(origin=Origin(cwd=str(world.tmp_path)))
    nowhere = Origin(cwd=str(world.tmp_path))
    assert world.recall(origin=nowhere).stdout == ""
    wide = world.recall(
        origin=nowhere, config=config(world.store_dir, mode="everywhere")
    )
    assert wide.ids == (memory.id,)


def test_a_turn_whose_relevant_hits_were_all_held_says_so(
    world: PromptWorld,
) -> None:
    """Design 4 failure 8: a prompt recall whose relevant hits the
    admission withheld, every one, writes one `recall_suppressed` row
    naming the surface and the admission."""
    world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    assert world.recall().stdout == ""
    rows = [
        json.loads(row.payload)
        for row in world.store.log_rows()
        if row.kind == "recall_suppressed"
    ]
    assert len(rows) == 1
    assert rows[0]["surface"] == PROMPT_RECALL
    assert rows[0]["admission"] == PROMPT_RECALL
    assert [entry["reason"] for entry in rows[0]["suppressed"]] == ["global"]


def test_off_delivers_nothing(world: PromptWorld) -> None:
    world.write()
    assert world.recall(config=config(world.store_dir, mode="off")).stdout == ""
    assert world.recalls() == []


def test_telemetry_off_still_delivers(world: PromptWorld) -> None:
    """The delivery's row is product state (ENG-108): recorded with
    telemetry off, keeping what its readers read and no probe (no reader
    reads a recall's probe)."""
    world.write()
    result = world.recall(config=config(world.store_dir, telemetry=False))
    assert result.stdout != ""
    (row,) = list(world.store.iter_events())
    assert row["kind"] == "prompt_recall"
    assert row["event_id"] == result.event_id
    assert "probe_query" not in row


def test_git_indeterminate_delivers_nothing(world: PromptWorld) -> None:
    """A caller nothing could be read for (INDETERMINATE) is shown nothing
    from any project in either mode: the result carries its resolution's
    line and no pointer, and no `prompt_recall` row is written."""
    world.write()
    unknown = world.resolve(None).resolution
    for mode in ("project", "everywhere"):
        result = world.recall(
            resolution=unknown, config=config(world.store_dir, mode=mode)
        )
        assert result.ids == ()
        assert "nothing from any project is shown here" in result.stdout
    assert world.recalls() == []


def test_the_state_is_shared_across_calls(world: PromptWorld) -> None:
    """With a daemon's state the second prompt is held from memory, not
    from the log: the same answer either way."""
    world.write()
    state = RecallState()
    assert world.recall(state=state).ids
    assert world.recall(state=state).stdout == ""
    assert world.recall().stdout == ""  # the log says the same


def test_contradicted_at_reads_contradicted_uses_only(world: PromptWorld) -> None:
    """`Store.contradicted_at` filters the outcome in SQL: other outcomes,
    other ids and a payload with no outcome never answer."""
    from bettermemory.events import Recorder

    recorder = Recorder(store=world.store, session_id="sess_x")
    recorder.record("use", ids=["a", "b"], outcome="applied")
    recorder.record("use", ids=["a"], outcome="contradicted")
    recorder.record("use", ids=["b"])
    recorder.record("use", ids=["a", "c"], outcome="contradicted")
    found = world.store.contradicted_at(["a", "b"])
    assert set(found) == {"a"}
    stamps = [
        e["ts"]
        for e in world.store.iter_events()
        if e["kind"] == "use" and e.get("outcome") == "contradicted"
    ]
    assert found["a"].isoformat().startswith(stamps[-1][:19])
