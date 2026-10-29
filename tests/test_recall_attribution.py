"""The Stop's settlement of what the recall surface showed (D9).

Every id the session brief or the prompt recall showed in the session
segment, and not settled in it yet, runs through the containment matcher
against the turn's reply: the Stop payload's ``last_assistant_message``
when Claude Code sends it, the transcript tail otherwise. A match records
one applied `use` naming its surface and the show's event id; no match
records nothing (a pointer is not a retrieval, so the auto fallback never
reaches it). At most one settlement per id per segment, also after a
restart. The attribution of real retrievals is unchanged beside it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config
from bettermemory.events import Recorder
from bettermemory.hook import audit_transcript, run_audit
from bettermemory.models import Memory, generate_ulid
from bettermemory.recall import RecallState, settle_shown
from bettermemory.store import Store

from .test_recall_prompt import PromptWorld, config

SESSION = "transcript-recall"
SERVER = "sess_0123456789abcdef"
SENTENCE = (
    "The deploy pipeline signs every release artifact with the hardware "
    "key kept in the vault on the third floor."
)
OTHER = (
    "Nightly builds publish their coverage reports to the shared bucket "
    "named coverage-archive before six in the morning."
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptWorld:
    built = PromptWorld(tmp_path)
    # The Stop captures the caller's origin; pin it to the world's
    # worktree so no test asks git about the checkout running the suite.
    monkeypatch.setattr(
        "bettermemory.origin.capture_caller", lambda *a, **k: built.caller
    )
    return built


def _brief(
    store: Store,
    ids: list[str],
    *,
    session: str = SESSION,
    segment: int = 0,
    worktree_root: str | None = None,
) -> str:
    """A `session_brief` row as the SessionStart hook records it."""
    event_id = generate_ulid()
    Recorder(
        store=store,
        session_id=session,
        worktree_root=worktree_root,
        triggered_from="session_hook",
    ).record(
        "session_brief",
        event_id=event_id,
        session_id=session,
        harness="claude-code",
        source="startup",
        segment=segment,
        ids=ids,
        chars=100,
        rows_examined=len(ids),
        unresolved=0,
        rule="rs-v1",
    )
    return event_id


def _audit(world: PromptWorld, reply: str | None, **kw: Any) -> dict[str, Any]:
    kw.setdefault("session_id", SESSION)
    kw.setdefault("config", world.config)
    kw.setdefault("store", world.store)
    return run_audit(user_message="ship the release", assistant_response=reply, **kw)


def _uses(store: Store) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e["kind"] == "use"]


def _transcript(path: Path, reply: str, *, prompt_id: str | None = None) -> Path:
    """A one-turn transcript. With `prompt_id` its user row carries it, as
    Claude Code stamps it: the Stop finds its turn by the payload's id
    (RS-F5)."""
    user: dict[str, Any] = {"type": "user", "message": {"content": "ship the release"}}
    if prompt_id is not None:
        user["promptId"] = prompt_id
    rows = [
        user,
        {
            "type": "assistant",
            "message": {
                "model": "claude-test",
                "content": [{"type": "text", "text": reply}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _memory(world: PromptWorld, body: str = SENTENCE) -> Memory:
    return world.write(body)


# ---------------------------------------------------------------------------
# The reply: last_assistant_message first, the transcript otherwise
# ---------------------------------------------------------------------------


def test_last_assistant_message_settles_a_brief_pointer(
    world: PromptWorld, tmp_path: Path
) -> None:
    memory = _memory(world)
    shown = _brief(world.store, [memory.id])
    transcript = _transcript(
        tmp_path / "t.jsonl", "I do not know yet.", prompt_id="turn-7"
    )
    audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
        assistant_message=f"Done. {SENTENCE}",
        prompt_id="turn-7",
    )
    [use] = _uses(world.store)
    assert use["ids"] == [memory.id]
    assert use["outcome"] == "applied"
    assert use["auto"] is False
    assert use["attribution"] == "hook"
    assert use["surface"] == "brief"
    assert use["shown_event_id"] == shown
    assert use["prompt_id"] == "turn-7"
    assert use["segment"] == 0
    assert use["triggered_from"] == "stop_hook"
    assert use["client_model"] == "claude-test"
    assert "hardware key" in use["claim_excerpts"][0]


def test_the_transcript_reply_is_the_fallback(
    world: PromptWorld, tmp_path: Path
) -> None:
    memory = _memory(world)
    _brief(world.store, [memory.id])
    transcript = _transcript(tmp_path / "t.jsonl", f"Done. {SENTENCE}")
    audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
    )
    [use] = _uses(world.store)
    assert (use["ids"], use["surface"]) == ([memory.id], "brief")
    assert "prompt_id" not in use


def test_last_assistant_message_is_preferred_over_the_transcript(
    world: PromptWorld, tmp_path: Path
) -> None:
    """The payload's reply is the one the turn ended on; a transcript tail
    that quotes the memory is not read for the reply when it is present."""
    memory = _memory(world)
    _brief(world.store, [memory.id])
    transcript = _transcript(tmp_path / "t.jsonl", f"Done. {SENTENCE}")
    audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
        assistant_message="Nothing to report on that.",
    )
    assert _uses(world.store) == []


# ---------------------------------------------------------------------------
# The two surfaces
# ---------------------------------------------------------------------------


def test_a_recall_pointer_settles_with_the_recall_surface(world: PromptWorld) -> None:
    memory = world.write()
    state = RecallState()
    delivered = world.recall(state=state)
    assert delivered.ids == (memory.id,)
    _audit(world, f"Sure: {memory.body}, as noted before.", state=state)
    [use] = _uses(world.store)
    assert use["surface"] == "recall"
    assert use["shown_event_id"] == delivered.event_id
    assert use["ids"] == [memory.id]


def test_a_brief_pointer_noted_in_the_daemon_state_settles(world: PromptWorld) -> None:
    """The daemon's state names the show without a log read."""
    memory = _memory(world)
    state = RecallState()
    now = datetime.now(timezone.utc)
    state.open_segment(world.store, ("claude-code", SESSION), source="startup", now=now)
    state.note_shown(
        ("claude-code", SESSION),
        [memory.id],
        delivery=False,
        now=now,
        surface="brief",
        event_id="01SHOWNEVENTID0000000000AA",
    )
    _audit(world, f"Done. {SENTENCE}", state=state)
    [use] = _uses(world.store)
    assert use["shown_event_id"] == "01SHOWNEVENTID0000000000AA"


# ---------------------------------------------------------------------------
# Once per id per segment; nothing for no match
# ---------------------------------------------------------------------------


def test_an_id_settles_once_per_segment(world: PromptWorld) -> None:
    memory = _memory(world)
    _brief(world.store, [memory.id])
    state = RecallState()
    _audit(world, f"Done. {SENTENCE}", state=state)
    _audit(world, f"Again: {SENTENCE}", state=state)
    assert len(_uses(world.store)) == 1


def test_a_restart_rebuilds_the_settlement_from_the_log(world: PromptWorld) -> None:
    memory = _memory(world)
    _brief(world.store, [memory.id])
    _audit(world, f"Done. {SENTENCE}", state=RecallState())
    restarted = RecallState()
    view = restarted.segment(
        world.store, ("claude-code", SESSION), now=datetime.now(timezone.utc)
    )
    assert view.settled == frozenset({memory.id})
    assert view.shown_by[memory.id][0] == "brief"
    _audit(world, f"Again: {SENTENCE}", state=restarted)
    # The in-process path (no state at all) rebuilds it too.
    _audit(world, f"Once more: {SENTENCE}")
    assert len(_uses(world.store)) == 1


def test_a_new_segment_may_settle_the_id_again(world: PromptWorld) -> None:
    memory = _memory(world)
    _brief(world.store, [memory.id], segment=0)
    _audit(world, f"Done. {SENTENCE}")
    _brief(world.store, [memory.id], segment=1)  # after a compact
    _audit(world, f"Again: {SENTENCE}")
    assert [u["segment"] for u in _uses(world.store)] == [0, 1]


def test_an_unmatched_pointer_records_nothing(world: PromptWorld) -> None:
    """No auto fallback: a pointer the reply did not use stays unsettled
    and no `use` row is written for it."""
    memory = _memory(world)
    _brief(world.store, [memory.id])
    _audit(world, "I renamed the variable and reran the tests.")
    assert _uses(world.store) == []
    # Still unsettled: a later turn that uses it settles it.
    _audit(world, f"Now: {SENTENCE}")
    assert len(_uses(world.store)) == 1


def test_settled_with_telemetry_off_too(world: PromptWorld) -> None:
    """The settlement's `use` row is product state (ENG-108): recorded
    with telemetry off, and the state notes it settled."""
    memory = _memory(world)
    _brief(world.store, [memory.id])
    state = RecallState()
    off: Config = config(world.store_dir, telemetry=False)
    _audit(world, f"Done. {SENTENCE}", config=off, state=state)
    assert [use["ids"] for use in _uses(world.store)] == [[memory.id]]
    view = state.segment(
        world.store, ("claude-code", SESSION), now=datetime.now(timezone.utc)
    )
    assert view.settled == frozenset({memory.id})


def test_an_id_the_conversation_already_used_is_not_settled_again(
    world: PromptWorld,
) -> None:
    """A `use` of this conversation already names the id in the segment
    (here an explicit record_use, joined by its harness stamp): one reply
    never credits the memory twice."""
    memory = _memory(world)
    _brief(world.store, [memory.id])
    Recorder(store=world.store, session_id=SERVER).record(
        "use", ids=[memory.id], outcome="applied", harness_session=SESSION
    )
    _audit(world, f"Done. {SENTENCE}")
    assert len(_uses(world.store)) == 1


# ---------------------------------------------------------------------------
# Real retrievals: unchanged beside the settlement
# ---------------------------------------------------------------------------


def _show(store: Store, memory_id: str, *, session: str = SERVER, **extra: Any) -> None:
    Recorder(store=store, session_id=session).record("show", id=memory_id, **extra)


def test_real_retrieval_attribution_is_unchanged(world: PromptWorld) -> None:
    """A shown memory the model also fetched is settled by the real
    retrieval's attribution only; the retrieved-but-unmatched one still
    gets the auto fallback, and neither carries a surface."""
    matched = _memory(world)
    unmatched = _memory(world, OTHER)
    _brief(world.store, [matched.id])
    _show(world.store, matched.id)
    _show(world.store, unmatched.id)
    _audit(world, f"Done. {SENTENCE}")
    uses = _uses(world.store)
    assert [(u["ids"], u["attribution"]) for u in uses] == [
        ([matched.id], "hook"),
        ([unmatched.id], "auto"),
    ]
    assert all("surface" not in u for u in uses)


def test_settle_shown_skips_the_ids_the_real_path_settled(world: PromptWorld) -> None:
    memory = _memory(world)
    _brief(world.store, [memory.id])
    recorder = Recorder(store=world.store, session_id=SESSION)
    settled = settle_shown(
        store=world.store,
        recorder=recorder,
        state=RecallState(),
        session_id=SESSION,
        assistant_response=f"Done. {SENTENCE}",
        sessions=set(),
        exclude={memory.id},
    )
    assert settled == []
    assert _uses(world.store) == []


def test_the_harness_join_picks_the_retrieval_session(world: PromptWorld) -> None:
    """The real retrieval's session is found by its harness stamp, not by
    the worktree bridge: a later event of another conversation's server
    (stamped with its own harness session) does not take the anchor."""
    memory = _memory(world)
    _show(world.store, memory.id, harness_session=SESSION)
    Recorder(store=world.store, session_id="sess_other").record(
        "search", returned=[], harness_session="another-conversation"
    )
    _audit(world, f"Done. {SENTENCE}")
    [use] = _uses(world.store)
    assert (use["ids"], use["attribution"]) == ([memory.id], "hook")


def test_a_use_stamped_before_a_clear_is_not_credited_again(
    world: PromptWorld,
) -> None:
    """After a /clear the server still stamps the conversation's old id.
    The model's own memory_record_use of a pointer is this conversation's
    use, so the Stop does not settle the pointer a second time."""
    memory = _memory(world)
    worktree = str(world.worktree)
    Recorder(
        store=world.store,
        session_id="transcript-before",
        worktree_root=worktree,
        triggered_from="stop_hook",
    ).record("turn_audited", verdict="ok")
    _brief(world.store, [memory.id], segment=1, worktree_root=worktree)
    Recorder(store=world.store, session_id=SERVER, worktree_root=worktree).record(
        "use",
        ids=[memory.id],
        outcome="applied",
        auto=False,
        attribution="model",
        claim_excerpts=[SENTENCE],
        harness_session="transcript-before",
    )
    _audit(world, f"Done. {SENTENCE}")
    assert [u for u in _uses(world.store) if u.get("surface")] == []


# ---------------------------------------------------------------------------
# RS-F3: the Stop row names its turn and where its reply came from
# ---------------------------------------------------------------------------


def _audited(store: Store) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e["kind"] == "turn_audited"]


def test_the_stop_row_names_the_payload_as_the_reply_source(
    world: PromptWorld, tmp_path: Path
) -> None:
    transcript = _transcript(
        tmp_path / "t.jsonl", "I do not know yet.", prompt_id="turn-7"
    )
    audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
        assistant_message="Done.",
        prompt_id="turn-7",
    )
    [row] = _audited(world.store)
    assert (row["prompt_id"], row["reply_source"], row["assistant_present"]) == (
        "turn-7",
        "payload",
        True,
    )


def test_the_stop_row_names_the_tail_as_the_reply_source(
    world: PromptWorld, tmp_path: Path
) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", "Done.")
    audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
    )
    [row] = _audited(world.store)
    assert row["reply_source"] == "tail"
    assert row["assistant_present"] is True
    assert "prompt_id" not in row


def test_the_stop_row_has_no_reply_source_without_a_reply(
    world: PromptWorld, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    rows = [
        {
            "type": "user",
            "promptId": "turn-8",
            "message": {"content": "ship the release"},
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-test",
                "content": [{"type": "tool_use", "name": "x", "input": {}}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    audit_transcript(
        transcript_path=path,
        session_id=SESSION,
        config=world.config,
        store=world.store,
        prompt_id="turn-8",
    )
    [row] = _audited(world.store)
    assert row["assistant_present"] is False
    assert "reply_source" not in row
    assert row["prompt_id"] == "turn-8"
