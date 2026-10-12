"""The prompt recall (`recall.prompt_recall`): the UserPromptSubmit surface.

The probe is the audit's (`hook._probe_message`), unchanged, so this file
does not re-test ranking. What it pins is delivery: the prompt the probe
reads (pasted blocks removed, 2,000 characters), the floor and the
acknowledgment gate, the candidate gates and their recorded reasons, up
to three pointers, the block's format and its 900-character cap, the
three `prompt_recall` modes, and the telemetry gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig, TelemetryConfig
from bettermemory.models import Memory
from bettermemory.origin import Origin
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
from bettermemory.store import Store

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
    """A store, a worktree in `REPO` the caller stands in, and helpers to
    seed memories the probe can rank (backdated past its creation
    shield) and to run the recall."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store_dir = tmp_path / "store"
        self.store = Store(self.store_dir)
        self.worktree = tmp_path / "foo"
        self.worktree.mkdir()
        self.caller = Origin(
            cwd=str(self.worktree), repo=REPO, worktree_root=str(self.worktree)
        )
        self.config = config(self.store_dir)

    def write(
        self,
        body: str = BODY,
        *,
        origin: Origin | None | str = "here",
        provenance: str | None = None,
        scopes: list[str] | None = None,
    ) -> Memory:
        where = self.caller if origin == "here" else origin
        assert not isinstance(where, str)
        written = self.store.write(
            content=body, scopes=scopes or ["projects:foo"], origin=where
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
    assert result.rule == "rs-v1"
    # The old block's promise that recording `ignored` tunes recall down
    # is gone: no ranking reads outcomes.
    assert "ignored" not in result.stdout

    (event,) = world.recalls()
    assert event["event_id"] == result.event_id
    assert event["shown_ids"] == [memory.id]
    assert event["suppressed_ids"] == []
    assert event["rule"] == "rs-v1"
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


def test_low_relevance_is_held(world: PromptWorld) -> None:
    strong = world.write("backup strategy zebra quokka uses restic")
    weak = world.write("a backup elsewhere entirely")
    result = world.recall("backup strategy zebra quokka")
    assert result.ids == (strong.id,)
    assert _suppressed(world) == {weak.id: "low_relevance"}


def test_another_project_and_a_global_are_held(world: PromptWorld) -> None:
    mine = world.write()
    # A global: the probe's auto-scope admits it, the project rule does not.
    stray = world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    result = world.recall()
    assert result.ids == (mine.id,)
    assert _suppressed(world) == {stray.id: "other_project"}


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


def test_everywhere_also_delivers_global_memories(world: PromptWorld) -> None:
    """ "everywhere" adds global memories (no repository of their own);
    another repository's memory stays out, as the probe's auto-scope
    keeps it."""
    mine = world.write()
    stray = world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    world.write(
        f"{BODY} elsewhere",
        origin=Origin(cwd="/bar", repo=OTHER_REPO, worktree_root="/bar"),
    )
    result = world.recall(config=config(world.store_dir, mode="everywhere"))
    assert set(result.ids) == {mine.id, stray.id}
    assert world.recalls()[0]["delivered_reason"] == "everywhere"


def test_outside_a_repository_only_everywhere_delivers(world: PromptWorld) -> None:
    memory = world.write(origin=Origin(cwd=str(world.tmp_path)))
    nowhere = Origin(cwd=str(world.tmp_path))
    assert world.recall(origin=nowhere).stdout == ""
    wide = world.recall(
        origin=nowhere, config=config(world.store_dir, mode="everywhere")
    )
    assert wide.ids == (memory.id,)


def test_off_delivers_nothing(world: PromptWorld) -> None:
    world.write()
    assert world.recall(config=config(world.store_dir, mode="off")).stdout == ""
    assert world.recalls() == []


def test_telemetry_off_delivers_nothing(world: PromptWorld) -> None:
    world.write()
    result = world.recall(config=config(world.store_dir, telemetry=False))
    assert result.stdout == ""
    assert list(world.store.iter_events()) == []


def test_git_indeterminate_delivers_nothing(world: PromptWorld) -> None:
    world.write()
    unknown = Origin(cwd=str(world.worktree))
    unknown._git_indeterminate = True
    for mode in ("project", "everywhere"):
        result = world.recall(origin=unknown, config=config(world.store_dir, mode=mode))
        assert result.stdout == ""
    assert world.recalls() == []


def test_the_state_is_shared_across_calls(world: PromptWorld) -> None:
    """With a daemon's state the second prompt is held from memory, not
    from the log: the same answer either way."""
    world.write()
    state = RecallState(background=False)
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
