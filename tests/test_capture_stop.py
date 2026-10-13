"""Capture at the Stop endpoint (`bettermemory.capture_hook`): when a turn
end asks the chat model to save the stretch since its last capture, and
how each request settles.

No model is involved: the endpoint's whole capture output is an
instruction string in the hook's JSON, and the model's side is the
ordinary write path (`test_capture_batch.py`). Ported from the
capture-in-session branch's `test_capture_hook.py`, minus catch-up.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import capture as cap
from bettermemory import capture_hook as ch
from bettermemory.config import CaptureConfig, Config, StorageConfig
from bettermemory.store import Store

from .test_daemon import _auth, running_app

SESSION = "3f2b9c1e-1111-4aaa-8bbb-0123456789ab"
OTHER = "7a1b2c3d-aaaa-4bbb-8ccc-0123456789ab"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def user(text: str, minute: int = 0) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": f"2026-10-12T14:{minute:02d}:00Z",
        "message": {"role": "user", "content": text},
    }


def assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
        },
    }


def tool_result(size: int) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "r" * size}
            ],
        },
    }


def long_session(pairs: int, size: int = 700, start: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(start, start + pairs):
        rows += [user(f"note {i} " + "n" * size, i % 60), assistant("o" * size)]
    return rows


def append(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture
def enabled(tmp_path: Path) -> Config:
    """Capture on, with a checkpoint of 500 tokens (2,000 characters)."""
    return Config(
        storage=StorageConfig(directory=str(tmp_path / "store")),
        capture=CaptureConfig(stop_checkpoint=True, checkpoint_tokens=500),
    )


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / f"{SESSION}.jsonl"


def stop(
    config: Config,
    store: Store,
    transcript: Path,
    *,
    session: str = SESSION,
    active: bool = False,
    attended: str | None = ch.ATTENDED,
    now: datetime = NOW,
) -> ch.StopStep | None:
    payload: dict[str, Any] = {
        "session_id": session,
        "transcript_path": str(transcript),
        "stop_hook_active": active,
    }
    if attended is not None:
        payload["attended"] = attended
    return ch.on_stop(config, store, payload, now=now)


def _close(store: Store, batch: str) -> cap.Close:
    return cap.closes(store)[batch]


# ---------------------------------------------------------------------------
# When a checkpoint is due
# ---------------------------------------------------------------------------


def test_nothing_is_asked_below_the_threshold(
    enabled: Config, store: Store, transcript: Path
) -> None:
    append(transcript, long_session(1))
    assert stop(enabled, store, transcript) is None
    assert cap.batches(store) == []


def test_bytes_alone_are_not_a_stretch(
    enabled: Config, store: Store, transcript: Path
) -> None:
    """Tool results grow the file past the byte gate, but only human
    prompts and assistant text count toward the checkpoint."""
    append(transcript, [user("run it"), tool_result(50_000), assistant("Done.")])
    assert stop(enabled, store, transcript) is None
    assert cap.batches(store) == []


def test_past_the_threshold_the_model_is_asked_from_its_own_context(
    enabled: Config, store: Store, transcript: Path
) -> None:
    append(transcript, long_session(3))
    step = stop(enabled, store, transcript)
    assert step is not None and step.action == ch.OPENED
    [batch] = cap.batches(store)
    assert step.batch == batch.batch
    assert (batch.session_id, batch.mode, batch.offset) == (
        SESSION,
        cap.MODE_CHECKPOINT,
        0,
    )
    assert batch.transcript_bytes == transcript.stat().st_size
    assert step.instruction is not None
    assert f'source="capture:{batch.token}"' in step.instruction
    assert "2026-10-12T14:00:00Z" in step.instruction  # the prompt times
    output = json.loads(ch.hook_output(step.instruction))
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": step.instruction,
        }
    }


def test_a_payload_must_name_its_own_transcript(
    enabled: Config, store: Store, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "other.jsonl"
    append(elsewhere, long_session(3))
    assert stop(enabled, store, elsewhere) is None
    assert stop(enabled, store, tmp_path / f"{SESSION}.jsonl") is None
    assert cap.batches(store) == []


def test_a_compacted_stretch_says_so_and_is_marked(
    enabled: Config, store: Store, transcript: Path
) -> None:
    rows = long_session(3)
    rows.insert(2, {"type": "system", "subtype": "compact_boundary"})
    append(transcript, rows)
    step = stop(enabled, store, transcript)
    assert step is not None and step.instruction is not None
    assert "compacted" in step.instruction
    assert cap.batches(store)[0].mode == cap.MODE_CHECKPOINT_COMPACTED
    plain = cap.in_session_instruction(
        token=cap.batches(store)[0].token, since=None, scopes=[]
    )
    assert "compacted" not in plain


def test_the_daily_cap_bounds_requests_per_store(
    enabled: Config, store: Store, transcript: Path, tmp_path: Path
) -> None:
    capped = Config(
        storage=enabled.storage,
        capture=CaptureConfig(stop_checkpoint=True, checkpoint_tokens=500, daily_cap=1),
    )
    append(transcript, long_session(3))
    assert stop(capped, store, transcript) is not None
    assert stop(capped, store, transcript, active=True) is not None
    other = tmp_path / f"{OTHER}.jsonl"
    append(other, long_session(3))
    assert stop(capped, store, other, session=OTHER) is None
    # The next UTC day the cap starts over.
    assert stop(capped, store, other, session=OTHER, now=NOW + timedelta(days=1))


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def test_the_continuation_settles_the_capture_to_the_transcript_end(
    enabled: Config, store: Store, transcript: Path
) -> None:
    append(transcript, long_session(3))
    opened = stop(enabled, store, transcript)
    assert opened is not None
    append(transcript, [assistant("Saved one memory: a durable fact.")])
    settled = stop(enabled, store, transcript, active=True)
    assert settled is not None and settled.action == ch.SETTLED
    assert settled.instruction is None
    close = _close(store, opened.batch)
    assert close.outcome == cap.OUTCOME_SETTLED
    assert close.watermark == transcript.stat().st_size
    assert cap.watermark(store, SESSION) == close.watermark
    # Captured to the end: the next Stop waits for a new stretch.
    assert stop(enabled, store, transcript) is None
    append(transcript, long_session(3, start=10))
    again = stop(enabled, store, transcript)
    assert again is not None and again.action == ch.OPENED
    assert cap.batches(store)[-1].offset == close.watermark


def test_a_continuation_never_opens_a_capture(
    enabled: Config, store: Store, transcript: Path
) -> None:
    """Another hook's continuation sets stop_hook_active too: due or not,
    no capture rides on it."""
    append(transcript, long_session(3))
    assert stop(enabled, store, transcript, active=True) is None
    assert cap.batches(store) == []


def test_a_stop_settles_only_its_own_sessions_batch(
    enabled: Config, store: Store, transcript: Path, tmp_path: Path
) -> None:
    append(transcript, long_session(3))
    opened = stop(enabled, store, transcript)
    assert opened is not None
    other = tmp_path / f"{OTHER}.jsonl"
    append(other, long_session(1))
    assert stop(enabled, store, other, session=OTHER, active=True) is None
    assert cap.open_batch_for_session(store, SESSION, now=NOW) is not None
    assert cap.closes(store) == {}


def test_an_interrupted_capture_captures_nothing_and_goes_quiet(
    enabled: Config, store: Store, transcript: Path
) -> None:
    append(transcript, long_session(3))
    opened = stop(enabled, store, transcript)
    assert opened is not None
    # The user pressed escape and typed on: the next Stop has no flag.
    append(transcript, long_session(1, start=20))
    step = stop(enabled, store, transcript, now=NOW + timedelta(minutes=2))
    assert step is not None and step.action == ch.INTERRUPTED
    assert step.instruction is None
    close = _close(store, opened.batch)
    assert (close.outcome, close.watermark) == (cap.OUTCOME_INTERRUPTED, 0)
    assert cap.watermark(store, SESSION) == 0
    # Quiet for an hour, then the stretch is asked for again from the start.
    later = NOW + timedelta(minutes=30)
    assert stop(enabled, store, transcript, now=later) is None
    after = NOW + timedelta(minutes=63)
    again = stop(enabled, store, transcript, now=after)
    assert again is not None and again.action == ch.OPENED
    assert cap.batches(store)[-1].offset == 0


def test_an_expired_batch_closes_as_expired(
    enabled: Config, store: Store, transcript: Path
) -> None:
    append(transcript, long_session(3))
    opened = stop(enabled, store, transcript)
    assert opened is not None
    later = NOW + cap.BATCH_TTL + timedelta(minutes=1)
    assert stop(enabled, store, transcript, active=True, now=later) is None
    assert _close(store, opened.batch).outcome == cap.OUTCOME_EXPIRED


# ---------------------------------------------------------------------------
# Off and unattended: exactly what a Stop did before capture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attended", [None, "0", "", "true"])
def test_an_unattended_session_is_never_asked(
    enabled: Config, store: Store, transcript: Path, attended: str | None
) -> None:
    append(transcript, long_session(3))
    assert stop(enabled, store, transcript, attended=attended) is None
    assert stop(enabled, store, transcript, attended=attended, active=True) is None
    assert cap.batches(store) == []


def test_nothing_happens_while_capture_is_off(
    store: Store, transcript: Path, tmp_path: Path
) -> None:
    off = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    assert off.capture.stop_checkpoint is False
    append(transcript, long_session(3))
    assert stop(off, store, transcript) is None
    assert cap.batches(store) == []


# ---------------------------------------------------------------------------
# Through the daemon's endpoint
# ---------------------------------------------------------------------------


async def _post_stop(config: Config, store: Store, payload: dict[str, Any]) -> bytes:
    async with running_app(config, store) as client:
        response = await client.post("/api/v1/hook/stop", json=payload, headers=_auth())
    assert response.status_code == 200
    return response.content


async def test_off_and_unattended_replies_are_byte_identical(
    enabled: Config, transcript: Path, tmp_path: Path
) -> None:
    """Assistant text only: the audit has nothing to audit, so the reply
    is the quiet one, and a capture that acted would show in it."""
    append(transcript, [assistant("o" * 3_000)])
    base = {"session_id": SESSION, "transcript_path": str(transcript)}
    off = Config(storage=enabled.storage)
    replies = []
    for config, extra in (
        (off, {}),
        (off, {"attended": "1"}),
        (enabled, {}),
        (enabled, {"attended": "0"}),
    ):
        store = Store(tmp_path / "store")
        replies.append(await _post_stop(config, store, {**base, **extra}))
        assert cap.batches(store) == []
        store.close()
    assert len(set(replies)) == 1
    assert b"hook_output" not in replies[0]
    store = Store(tmp_path / "store")
    attended = json.loads(await _post_stop(enabled, store, {**base, "attended": "1"}))
    assert attended["capture"]["action"] == ch.OPENED
    assert (
        "additionalContext" in json.loads(attended["hook_output"])["hookSpecificOutput"]
    )


async def test_the_audit_leaves_out_the_settling_stop(
    enabled: Config, store: Store, transcript: Path, tmp_path: Path
) -> None:
    append(transcript, long_session(3))
    base = {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "attended": "1",
    }

    def audited() -> int:
        return sum(1 for e in store.iter_events() if e["kind"] == "turn_audited")

    opened = json.loads(await _post_stop(enabled, store, base))
    assert opened["capture"]["action"] == ch.OPENED
    assert audited() == 1
    append(transcript, [assistant("Saved one memory.")])
    settled = json.loads(
        await _post_stop(enabled, store, {**base, "stop_hook_active": True})
    )
    assert settled["capture"]["action"] == ch.SETTLED
    assert settled["result"] is None and "hook_output" not in settled
    assert audited() == 1


async def test_a_capture_failure_never_costs_the_audit(
    enabled: Config,
    store: Store,
    transcript: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("capture broke")

    monkeypatch.setattr(ch, "on_stop", boom)
    append(transcript, long_session(3))
    reply = json.loads(
        await _post_stop(
            enabled,
            store,
            {
                "session_id": SESSION,
                "transcript_path": str(transcript),
                "cwd": str(tmp_path),
                "attended": "1",
            },
        )
    )
    assert reply["result"] is not None
    assert "hook_output" not in reply
