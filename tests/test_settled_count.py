"""`turn_audited` carries `settled_count` (ENG-108).

The Stop's audit row says how many memories the audit of its turn
settled as used: the retrievals `hook._emit_hook_attributions` settled
plus the shown pointers `recall.settle_shown` settled, one `use` row
each. The Stop records the row after its settlements, so the count is
theirs, and before the walk over the session's interrupted turns, whose
own rows carry zero (their pointers stay pending).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import hook as hook_module
from bettermemory.audit import no_owner_prompt_report, turn_audited_fields
from bettermemory.recall import RecallState

from .test_recall_prompt import BODY, QUERY, PromptWorld, config
from .test_recall_settlement import (
    SESSION,
    _audited,
    _brief,
    _interrupted,
    _rows,
    _stop,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptWorld:
    world = PromptWorld(tmp_path)
    monkeypatch.setattr(
        "bettermemory.origin.capture_caller", lambda *a, **k: world.caller
    )
    return world


def _stop_with(
    world: PromptWorld, state: RecallState, reply: str | None, **kw: Any
) -> dict[str, Any]:
    return hook_module.run_audit(
        user_message=kw.pop("user_message", QUERY),
        assistant_response=reply,
        session_id=SESSION,
        config=kw.pop("config", world.config),
        store=world.store,
        state=state,
        **kw,
    )


@pytest.mark.parametrize("telemetry", [True, False], ids=["on", "off"])
def test_the_stop_counts_the_pointers_its_reply_used(
    world: PromptWorld, telemetry: bool
) -> None:
    cfg = config(world.store_dir, telemetry=telemetry)
    world.config = cfg
    used = world.write()
    world.write("the on-call rota rotates every friday for the payments team")
    state = RecallState()
    _brief(world, state)
    _stop_with(world, state, f"The answer: {BODY}.", config=cfg)
    [audited] = _rows(world, "turn_audited")
    settled = [e for e in _rows(world, "use") if e.get("outcome") == "applied"]
    assert [e["ids"] for e in settled] == [[used.id]]
    assert audited["settled_count"] == 1
    # The row lands after the settlements it counts.
    kinds = [e["kind"] for e in world.store.iter_events()]
    assert kinds.index("turn_audited") > max(
        i for i, kind in enumerate(kinds) if kind == "use"
    )


def test_the_stop_counts_the_retrievals_it_settled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two retrievals pending: the reply quotes one (attributed) and the
    other settles by the auto fallback; both are the turn's settlements."""
    from bettermemory.hook import main as hook_main
    from bettermemory.store import Store

    from .test_hook import _events, _seed_search_event, _write_transcript

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    store = Store(mem_dir)
    quoted = store.write(
        content="The kubernetes ingress is on the staging cluster only for now.",
        scopes=["infrastructure"],
    )
    other = store.write(
        content="The payments team rotates its on-call rota every friday.",
        scopes=["infrastructure"],
    )
    _seed_search_event(mem_dir, session_id="sess-two", returned=[quoted.id, other.id])
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "where is the ingress?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "The kubernetes ingress is on the staging "
                        "cluster only for now.",
                    }
                ]
            },
        },
    )
    assert (
        hook_main(
            [
                "--transcript-path",
                str(transcript),
                "--session-id",
                "sess-two",
                "--quiet",
            ]
        )
        == 0
    )
    events = _events(mem_dir)
    uses = [e for e in events if e["kind"] == "use"]
    assert sorted(e["attribution"] for e in uses) == ["auto", "hook"]
    [audited] = [e for e in events if e["kind"] == "turn_audited"]
    assert audited["settled_count"] == 2


def test_a_stop_that_settled_nothing_counts_zero(world: PromptWorld) -> None:
    world.write()
    state = RecallState()
    _brief(world, state)
    _stop_with(world, state, None)
    _stop_with(world, state, "Nothing of the kind.", user_message="ok thanks")
    _stop_with(world, state, f"The answer: {BODY}.", user_message=None)
    assert [row["settled_count"] for row in _rows(world, "turn_audited")] == [0, 0, 0]


def test_a_repeat_stop_does_not_count_a_pointer_twice(world: PromptWorld) -> None:
    world.write()
    state = RecallState()
    _brief(world, state)
    _stop_with(world, state, f"The answer: {BODY}.")
    _stop_with(world, state, f"The answer again: {BODY}.")
    rows = _rows(world, "turn_audited")
    assert [row["settled_count"] for row in rows] == [1, 0]
    assert rows[1].get("repeat") is True


def test_an_interrupted_turns_row_counts_zero(
    world: PromptWorld, tmp_path: Path
) -> None:
    world.write()
    transcript = _interrupted(
        tmp_path / "t.jsonl", at=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    assert _stop(world, transcript, "p2") is not None
    audited = _audited(world)
    assert audited["p1"]["reply_source"] == "interrupted"
    assert audited["p1"]["settled_count"] == 0
    assert audited["p2"]["settled_count"] == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1"])
def test_the_builder_takes_a_count_or_nothing(bad: Any) -> None:
    report = no_owner_prompt_report(
        session_id="s", now=datetime.now(timezone.utc), lookback_seconds=0
    )
    kwargs: dict[str, Any] = {
        "session_id": "s",
        "probe_mode": "hybrid",
        "assistant_present": False,
        "triggered_from": "stop_hook",
    }
    assert "settled_count" not in turn_audited_fields(report, **kwargs)
    assert turn_audited_fields(report, settled_count=3, **kwargs)["settled_count"] == 3
    with pytest.raises(ValueError, match="settled_count"):
        turn_audited_fields(report, settled_count=bad, **kwargs)
