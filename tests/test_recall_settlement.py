"""RS-F7 part 6: every probed owner prompt leaves a row, every shown pointer
is settled, every interrupted turn is audited.

- `recall_held`: a probed owner prompt the recall delivered nothing for
  writes one row naming the cause (`audit.HELD_CAUSES`) and the held hits
  with their reasons, so the `prompt_recall` and `recall_held` rows of a
  session add up to its probed owner prompts;
- ``unused``: when a segment closes (a clear, a compact on its marker, a
  compaction confirmed by a later read) every pointer it showed that no
  Stop settled, no `memory_show` of the conversation opened and no `use`
  row attested gets a `use` row with outcome ``unused``, recorded before
  the row that opens the next segment, so a state rebuilt from the log
  reads it in the segment it settles;
- interrupted turns: a turn the owner interrupted ends with no Stop; the
  next Stop, or the next prompt's recall, writes its `turn_audited` row
  (``reply_source="interrupted"``), once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import hook as hook_module
from bettermemory import recall as recall_module
from bettermemory.audit import HELD_CAUSES
from bettermemory.hook import audit_transcript
from bettermemory.recall import RecallState, session_brief
from bettermemory.time_utils import isoformat_utc

from . import _transcript_rows as tr
from .test_hook import _TURN_AUDITED_KEYS
from .test_recall_prompt import BODY, QUERY, PromptWorld

SESSION = "transcript-recall"
KEY = ("claude-code", SESSION)
INTERRUPTED_KEYS = _TURN_AUDITED_KEYS | {"prompt_id", "reply_source", "client_model"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptWorld:
    world = PromptWorld(tmp_path)
    monkeypatch.setattr(hook_module, "capture_origin", lambda *a, **k: world.caller)
    return world


def _rows(world: PromptWorld, kind: str) -> list[dict[str, Any]]:
    return [e for e in world.store.iter_events() if e["kind"] == kind]


def _held(world: PromptWorld) -> list[dict[str, Any]]:
    return _rows(world, "recall_held")


def _unused(world: PromptWorld) -> list[dict[str, Any]]:
    return [e for e in _rows(world, "use") if e.get("outcome") == "unused"]


def _brief(
    world: PromptWorld, state: RecallState, **kw: Any
) -> recall_module.HookResult:
    kw.setdefault("source", "startup")
    return session_brief(
        session_id=SESSION,
        origin=world.caller,
        config=world.config,
        store=world.store,
        state=state,
        **kw,
    )


def _index(world: PromptWorld, kind: str, **match: Any) -> int:
    """The log position of the first row of `kind` whose fields hold
    `match`."""
    for index, event in enumerate(world.store.iter_events()):
        if event["kind"] == kind and all(event.get(k) == v for k, v in match.items()):
            return index
    raise AssertionError((kind, match))


# ---------------------------------------------------------------------------
# recall_held: the other half of prompt_recall
# ---------------------------------------------------------------------------


def test_a_prompt_below_the_floor_is_held_as_no_signal(world: PromptWorld) -> None:
    world.write()
    assert world.recall("ok").stdout == ""
    assert world.recalls() == []
    (row,) = _held(world)
    assert row["cause"] == "no_signal"
    assert row["verdict"] == "no_signal"
    assert row["held"] == []
    assert row["rule"] == "rs-v6"
    assert row["triggered_from"] == "prompt_hook"
    assert (row["segment"], row["harness"]) == (0, "claude-code")
    assert row["admission"] == "prompt_recall"
    assert row["session_id"] == row["session"] == SESSION
    assert row["checkout"] == world.home.checkout


def test_a_prompt_the_harness_composed_leaves_no_row(world: PromptWorld) -> None:
    """Not an owner prompt: not probed, so neither row is written."""
    from .test_recall_prompt import TASK_NOTIFICATION

    world.write()
    assert world.recall(TASK_NOTIFICATION).stdout == ""
    assert world.recalls() == [] and _held(world) == []


def test_an_empty_pool_is_held_as_no_signal(world: PromptWorld) -> None:
    """The probe reads an empty pool as no signal: no verdict was formed,
    so the cause is ``no_signal`` with no hits and nothing held."""
    assert world.recall().stdout == ""
    (row,) = _held(world)
    assert (row["cause"], row["top_hits"], row["held"]) == ("no_signal", [], [])
    # As on a `turn_audited` row, a no-signal verdict carries no probe.
    assert row["probe_query"] is None


def test_every_hit_held_names_each_hold(world: PromptWorld) -> None:
    pulled = world.write(f"{BODY} pulled", provenance="synced")
    elsewhere = world.write(f"{BODY} globally", origin=None, scopes=["tools"])
    assert world.recall().stdout == ""
    (row,) = _held(world)
    assert row["cause"] == "every_hit_held"
    reasons = {entry["id"]: entry["reason"] for entry in row["held"]}
    assert reasons == {pulled.id: "provenance", elsewhere.id: "other_project"}
    assert [hit["id"] for hit in row["top_hits"]] == [
        entry["id"] for entry in row["held"]
    ]


def test_a_segment_that_showed_every_hit_holds_the_next_prompt(
    world: PromptWorld,
) -> None:
    shown = world.write()
    state = RecallState()
    assert world.recall(state=state).ids == (shown.id,)
    assert world.recall(state=state).stdout == ""
    (row,) = _held(world)
    assert row["cause"] == "every_hit_held"
    assert row["held"] == [{"id": shown.id, "reason": "seen_in_segment"}]
    assert row["segment"] == 0


def test_a_delivery_a_concurrent_prompt_claimed_first_is_held(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown decides atomically: when another prompt of the session
    showed the ids between the read and the claim, nothing is printed and
    the row says the segment claimed them."""
    shown = world.write()
    monkeypatch.setattr(
        RecallState, "claim_delivery", lambda self, key, ids, *, now, event_id: ([], 0)
    )
    assert world.recall().stdout == ""
    (row,) = _held(world)
    assert row["cause"] == "segment_claimed"
    assert row["held"] == [{"id": shown.id, "reason": "seen_in_segment"}]


def test_a_block_the_cap_empties_is_held_as_nothing_rendered(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = world.write()
    monkeypatch.setattr(recall_module, "RECALL_CAP_CHARS", 10)
    assert world.recall().stdout == ""
    (row,) = _held(world)
    assert row["cause"] == "nothing_rendered"
    assert row["held"] == [{"id": shown.id, "reason": "cap"}]


def test_the_causes_are_the_declared_set(world: PromptWorld) -> None:
    assert HELD_CAUSES == {
        "no_signal",
        "no_hits",
        "every_hit_held",
        "nothing_rendered",
        "segment_claimed",
    }


def test_delivered_and_held_rows_add_up_to_the_probed_prompts(
    world: PromptWorld,
) -> None:
    world.write()
    state = RecallState()
    prompts = [QUERY, "ok", QUERY, "the deploy window for the payments service"]
    for prompt in prompts:
        world.recall(prompt, state=state)
    assert len(world.recalls()) + len(_held(world)) == len(prompts)
    assert len(world.recalls()) == 1
    assert [row["cause"] for row in _held(world)] == [
        "no_signal",
        "every_hit_held",
        "no_signal",
    ]


def test_telemetry_off_writes_no_held_row(world: PromptWorld) -> None:
    from .test_recall_prompt import config

    world.write()
    assert (
        world.recall("ok", config=config(world.store_dir, telemetry=False)).stdout == ""
    )
    assert _held(world) == []


# ---------------------------------------------------------------------------
# unused: the segment's close settles what it showed
# ---------------------------------------------------------------------------


def test_a_clear_settles_the_closed_segments_unused_pointers(
    world: PromptWorld,
) -> None:
    opened = world.write()
    attested = world.write(f"{BODY} attested")
    idle = world.write(f"{BODY} idle")
    state = RecallState()
    startup = _brief(world, state)
    assert set(startup.ids) == {opened.id, attested.id, idle.id}
    world.store.record_event("show", session=SESSION, id=opened.id)
    world.store.record_event(
        "use", session=SESSION, ids=[attested.id], outcome="applied"
    )

    cleared = _brief(world, state, source="clear")

    # The new segment has shown nothing yet: the three print again.
    assert set(cleared.ids) == {opened.id, attested.id, idle.id}
    (row,) = _unused(world)
    assert row["ids"] == [idle.id]
    assert (row["surface"], row["shown_event_id"]) == ("brief", startup.event_id)
    assert (row["segment"], row["harness"], row["rule"]) == (0, "claude-code", "rs-v6")
    assert (row["attribution"], row["auto"]) == ("hook", False)
    assert row["triggered_from"] == "session_hook"
    assert row["session"] == SESSION
    # Recorded before the row that opens the next segment.
    assert _index(world, "use", outcome="unused") < _index(
        world, "session_brief", source="clear"
    )


def test_a_clear_reprints_in_the_new_segment_and_settles_once(
    world: PromptWorld,
) -> None:
    memory = world.write()
    state = RecallState()
    first = _brief(world, state)
    assert first.ids == (memory.id,)
    cleared = _brief(world, state, source="clear")
    # The new segment has shown nothing: the pointer prints again.
    assert cleared.ids == (memory.id,)
    (row,) = _unused(world)
    assert row["shown_event_id"] == first.event_id
    # Settled once: a fresh state rebuilt from the log reads the first
    # segment as settled and the second as holding the reprint alone.
    again = _brief(world, RecallState(), source="clear")
    assert again.ids == (memory.id,)
    rows = _unused(world)
    assert [r["shown_event_id"] for r in rows] == [first.event_id, cleared.event_id]
    assert [r["segment"] for r in rows] == [0, 1]


def test_a_stop_settled_pointer_is_not_settled_unused(world: PromptWorld) -> None:
    """The Stop's attribution settled it (`settle_shown`); the close has
    nothing left for it."""
    used = world.write()
    # A body the reply does not contain: the matcher leaves it pending.
    idle = world.write("the on-call rota rotates every friday for the payments team")
    state = RecallState()
    _brief(world, state)
    hook_module.run_audit(
        user_message=QUERY,
        assistant_response=f"The answer: {BODY}.",
        session_id=SESSION,
        config=world.config,
        store=world.store,
        state=state,
    )
    _brief(world, state, source="clear")
    assert [row["ids"] for row in _unused(world)] == [[idle.id]]
    assert used.id in {i for e in _rows(world, "use") for i in e["ids"]}


def test_a_compact_on_its_marker_settles_the_segment(
    world: PromptWorld, tmp_path: Path
) -> None:
    memory = world.write()
    state = RecallState()
    startup = _brief(world, state)
    marked = tr.write(
        tmp_path / "t.jsonl", tr.boundary(datetime.now(timezone.utc), "boundary-1")
    )
    _brief(world, state, source="compact", transcript_path=str(marked))
    (row,) = _unused(world)
    assert (row["ids"], row["shown_event_id"]) == ([memory.id], startup.event_id)
    assert row["triggered_from"] == "session_hook"
    assert _index(world, "use", outcome="unused") < _index(
        world, "session_brief", source="compact"
    )


def test_a_compaction_confirmed_by_the_prompt_hook_settles_the_segment(
    world: PromptWorld, tmp_path: Path
) -> None:
    """The compact SessionStart found no marker (a resume with a signal);
    the next prompt's read finds it, opens the segment at the signal and
    settles what the closed segment showed, before the `segment_opened`
    row."""
    memory = world.write()
    state = RecallState()
    startup = _brief(world, state)
    transcript = tr.write(tmp_path / "t.jsonl", tr.user("an ask", "p1"))
    signal = datetime.now(timezone.utc)
    compacted = _brief(
        world, state, source="compact", transcript_path=str(transcript), now=signal
    )
    assert compacted.ids == () and _unused(world) == []
    tr.append(transcript, tr.boundary(signal + timedelta(seconds=1)))

    world.recall(
        "the deploy window for the payments service",
        state=state,
        transcript_path=str(transcript),
        now=signal + timedelta(seconds=120),
    )

    (row,) = _unused(world)
    assert (row["ids"], row["shown_event_id"]) == ([memory.id], startup.event_id)
    assert row["triggered_from"] == "prompt_hook"
    assert _index(world, "use", outcome="unused") < _index(world, "segment_opened")


def test_a_resume_closes_nothing(world: PromptWorld) -> None:
    world.write()
    state = RecallState()
    _brief(world, state)
    _brief(world, state, source="resume")
    assert _unused(world) == []


# ---------------------------------------------------------------------------
# Interrupted turns: audited by the next hook, once
# ---------------------------------------------------------------------------


def _interrupted(path: Path, *, at: datetime) -> Path:
    """An owner turn ``p1`` the owner interrupted after a partial reply,
    then a turn ``p2`` below the probe's floor with its reply."""
    return tr.write(
        path,
        tr.user(QUERY, "p1", timestamp=isoformat_utc(at)),
        tr.assistant("a partial reply"),
        tr.user(
            "[Request interrupted by user]",
            "p1",
            timestamp=isoformat_utc(at + timedelta(seconds=1)),
        ),
        tr.user("ok thanks", "p2", timestamp=isoformat_utc(at + timedelta(seconds=2))),
        tr.assistant("reply two"),
    )


def _stop(
    world: PromptWorld, transcript: Path, prompt_id: str
) -> dict[str, Any] | None:
    return audit_transcript(
        transcript_path=transcript,
        session_id=SESSION,
        config=world.config,
        store=world.store,
        prompt_id=prompt_id,
    )


def _audited(world: PromptWorld) -> dict[str, dict[str, Any]]:
    rows = _rows(world, "turn_audited")
    return {row["prompt_id"]: row for row in rows}


def test_the_next_stop_audits_the_interrupted_turn(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    transcript = _interrupted(
        tmp_path / "t.jsonl", at=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    assert _stop(world, transcript, "p2") is not None
    audited = _audited(world)
    assert set(audited) == {"p1", "p2"}
    own, interrupted = audited["p2"], audited["p1"]
    assert own["reply_source"] == "tail"
    assert set(interrupted) == INTERRUPTED_KEYS
    assert interrupted["reply_source"] == "interrupted"
    # Probed as a Stop probes it: the same verdict a Stop of the prompt
    # yields, over the same hits.
    plain = hook_module.run_audit(
        user_message=QUERY,
        assistant_response=None,
        session_id="another-session",
        config=world.config,
        store=world.store,
    )
    assert interrupted["verdict"] == plain["verdict"]
    assert [hit["id"] for hit in interrupted["top_hits"]] == [
        hit["id"] for hit in plain["top_hits"]
    ]
    assert interrupted["assistant_present"] is True
    assert interrupted["client_model"] == "claude-test"
    assert (interrupted["triggered_from"], interrupted["rule"]) == (
        "stop_hook",
        "rs-v6",
    )
    # A miss the turn did not finish is not a search_miss.
    assert _rows(world, "search_miss") == []


def test_an_interrupted_turn_is_audited_once(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    at = datetime.now(timezone.utc) - timedelta(minutes=10)
    transcript = _interrupted(tmp_path / "t.jsonl", at=at)
    _stop(world, transcript, "p2")
    tr.append(
        transcript,
        tr.user("ok", "p3", timestamp=isoformat_utc(at + timedelta(seconds=30))),
        tr.assistant("reply three"),
    )
    _stop(world, transcript, "p3")
    rows = _rows(world, "turn_audited")
    assert [row["prompt_id"] for row in rows] == ["p2", "p1", "p3"]


def test_the_prompt_hook_audits_it_before_any_stop(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    transcript = _interrupted(
        tmp_path / "t.jsonl", at=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    world.recall(
        "the deploy window for the payments service",
        transcript_path=str(transcript),
    )
    (row,) = _rows(world, "turn_audited")
    assert (row["prompt_id"], row["reply_source"]) == ("p1", "interrupted")
    assert row["triggered_from"] == "prompt_hook"
    # The Stop that follows does not audit it again.
    _stop(world, transcript, "p2")
    assert [r["prompt_id"] for r in _rows(world, "turn_audited")] == ["p1", "p2"]


def test_an_interrupt_before_any_words_is_not_a_turn(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    at = datetime.now(timezone.utc) - timedelta(minutes=10)
    transcript = tr.write(
        tmp_path / "t.jsonl",
        tr.user("[Request interrupted by user]", "p1", timestamp=isoformat_utc(at)),
        tr.user("ok thanks", "p2", timestamp=isoformat_utc(at + timedelta(seconds=2))),
        tr.assistant("reply two"),
    )
    _stop(world, transcript, "p2")
    assert list(_audited(world)) == ["p2"]


def test_a_turn_before_the_previous_audit_is_not_walked(
    world: PromptWorld, tmp_path: Path
) -> None:
    """The walk ends at the session's previous `turn_audited`: a Stop
    whose transcript held no interrupted row audits its own turn alone,
    and the next Stop, whose transcript holds the interrupted turn, walks
    back no further than that previous audit's stamp, so an interrupted
    turn stamped before it is not walked again."""
    world.write()
    at = datetime.now(timezone.utc) - timedelta(minutes=10)
    transcript = _interrupted(tmp_path / "t.jsonl", at=at)
    # A Stop of a turn stamped after the interrupt, with the interrupted
    # rows absent from the walk's file at the time: write a plain
    # transcript for it first.
    plain = tr.write(
        tmp_path / "plain.jsonl",
        tr.user("ok thanks", "p0", timestamp=isoformat_utc(at + timedelta(minutes=5))),
        tr.assistant("reply zero"),
    )
    _stop(world, plain, "p0")
    _stop(world, transcript, "p2")
    assert set(_audited(world)) == {"p0", "p2"}
