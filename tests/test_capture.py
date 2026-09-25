"""Session capture's pipeline (`bettermemory.capture`) and its CLI.

The model is scripted here: `session_capture`'s prompt and validator have
their own tests (`test_session_capture.py`), and what these tests pin is
everything around the call — what reaches the prompt, what the gates do
with each reply, what the watermark and the event log say afterwards.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bettermemory import capture as cap
from bettermemory import session_capture as sc
from bettermemory.config import BehaviorConfig, Config
from bettermemory.events import AttributedRecorder, iter_events
from bettermemory.hook import _OUT_OF_PROCESS_TRIGGERS, _latest_in_process_session
from bettermemory.models import Category
from bettermemory.proposals import ProposalQueue
from bettermemory.provenance import creation_id
from bettermemory.store import Store

from .conftest import set_git_discovery_ceiling

SESSION = "0f5c2a9e-1111-4222-8333-944455556666"


# ---------------------------------------------------------------------------
# Transcript fixtures
# ---------------------------------------------------------------------------


def _ts(minute: int) -> str:
    return f"2026-09-22T10:{minute:02d}:00.000Z"


def user(text: str, minute: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": SESSION,
        "timestamp": _ts(minute),
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(
    text: str | None = None,
    minute: int = 1,
    tools: list[dict[str, Any]] | None = None,
    message_model: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if text is not None:
        blocks.append({"type": "text", "text": text})
    for tool in tools or []:
        blocks.append({"type": "tool_use", "id": "t", **tool})
    return {
        "type": "assistant",
        "sessionId": SESSION,
        "timestamp": _ts(minute),
        "message": {
            "role": "assistant",
            "content": blocks,
            **({"model": message_model} if message_model else {}),
        },
        **extra,
    }


def tool_result(minute: int = 2) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": SESSION,
        "timestamp": _ts(minute),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000}
            ],
        },
    }


def write_transcript(path: Path, rows: list[dict[str, Any]], *, cwd: Path) -> Path:
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({"cwd": str(cwd), **row}) + "\n")
    return path


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The captured session's working directory: outside any checkout, so
    its memories carry only the `session-capture` scope."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / f"{SESSION}.jsonl"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ScriptedModel:
    """Answers each call with the next scripted reply (a list of memory
    dicts, or an exception to raise)."""

    replies: list[Any]
    name: str = "scripted"
    model: str = "test"
    calls: list[list[dict[str, str]]] = dataclasses.field(default_factory=list)

    def complete(self, messages: list[dict[str, str]]) -> cap.ModelReply:
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return cap.ModelReply(text=json.dumps({"memories": reply}), cost_usd=0.01)


def mem(body: str, quote: str, turns: list[int], kind: str = "fact") -> dict[str, Any]:
    return {"kind": kind, "body": body, "turns": turns, "quote": quote}


def recorder_for(store: Store) -> AttributedRecorder:
    return AttributedRecorder(
        root=store.root,
        session_id=SESSION,
        attribution="cli_capture",
        triggered_from=cap.TRIGGER,
    )


def run(
    store: Store,
    config: Config,
    transcript: Path,
    model: ScriptedModel,
    **kwargs: Any,
) -> cap.CaptureReport:
    return cap.capture_transcript(
        store=store,
        config=config,
        recorder=recorder_for(store),
        transcript=transcript,
        model=model,
        **kwargs,
    )


BEAGLE = [
    user("I adopted a beagle named Biscuit yesterday.", 0),
    assistant("Congratulations on Biscuit!", 1),
]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_reader_keeps_the_humans_words_and_drops_harness_rows(
    transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [
            user("Base directory for this skill: ...", 0, isMeta=True),
            user("<task-notification>done</task-notification>", 0),
            user("<system-reminder>be brief</system-reminder>", 0),
            user("[Request interrupted by user]", 0),
            user("What port does the API listen on?", 1),
            assistant(None, 2, tools=[{"name": "Read", "input": {"file_path": "a"}}]),
            tool_result(3),
            assistant("It listens on 8080.", 4, extra_field=True),
            assistant("sidechain words", 4, isSidechain=True),
            {"type": "summary", "summary": "not a turn"},
        ],
        cwd=workdir,
    )
    read = cap.read_transcript(transcript)
    assert [(t.role, t.text) for t in read.turns] == [
        ("user", "What port does the API listen on?"),
        ("assistant", "It listens on 8080."),
    ]
    assert read.cwd == str(workdir)
    assert read.end_offset == transcript.stat().st_size


def test_reader_splits_assistant_turns_at_prose_and_digests_tool_calls(
    transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [
            user("Release it.", 0),
            assistant(
                "Checking the tree.",
                1,
                tools=[
                    {"name": "Bash", "input": {"command": "git status\ngit log"}},
                    {"name": "Edit", "input": {"file_path": "/r/CHANGELOG.md"}},
                    {"name": "Bash", "input": {"command": "git push origin main"}},
                    {"name": "Grep", "input": {"pattern": "x"}},
                    {
                        "name": "mcp__bettermemory__memory_write",
                        "input": {"content": "8.0.0 shipped", "scopes": ["x"]},
                    },
                ],
            ),
            assistant("Released 8.0.0.", 9),
        ],
        cwd=workdir,
    )
    turns = cap.read_transcript(transcript).turns
    assert [t.role for t in turns] == ["user", "assistant", "assistant"]
    first = turns[1].text.splitlines()
    assert first == [
        "Checking the tree.",
        "[ran: git status]",
        "[edited /r/CHANGELOG.md]",
        "[ran: git push origin main]",
        "[saved to memory: 8.0.0 shipped]",
    ]
    assert turns[2].text == "Released 8.0.0."
    assert turns[2].ts_ms is not None and turns[1].ts_ms is not None
    assert turns[2].ts_ms > turns[1].ts_ms


def test_digest_budget_goes_to_edits_before_reads(
    transcript: Path, workdir: Path
) -> None:
    reads = [
        {"name": "Bash", "input": {"command": f"grep -rn needle{i} src/ | head"}}
        for i in range(40)
    ]
    edit = {"name": "Write", "input": {"file_path": "/r/src/new_module.py"}}
    write_transcript(
        transcript,
        [user("Build it.", 0), assistant("Working.", 1, tools=[*reads, edit])],
        cwd=workdir,
    )
    text = cap.read_transcript(transcript).turns[1].text
    assert "[wrote /r/src/new_module.py]" in text
    assert len(text) <= sc.ASSISTANT_TURN_CHARS


def test_reader_redacts_secrets_and_private_spans(
    transcript: Path, workdir: Path
) -> None:
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    write_transcript(
        transcript,
        [
            user(f"My token is {secret} and <private>my diagnosis</private>.", 0),
            assistant(f"Stored {secret}.", 1),
        ],
        cwd=workdir,
    )
    turns = cap.read_transcript(transcript).turns
    joined = "\n".join(t.text for t in turns)
    assert secret not in joined
    assert "diagnosis" not in joined
    assert "[redacted:" in joined and "[private]" in joined


def test_reader_consumes_complete_lines_only_and_resumes_from_offset(
    transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    complete = transcript.stat().st_size
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "message": {"content": "half a li')
    read = cap.read_transcript(transcript)
    assert read.end_offset == complete
    assert len(read.turns) == 2

    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write('ne"}}\n')
    write_transcript(
        transcript,
        [user("We moved to Lisbon.", 5), assistant("Noted.", 6)],
        cwd=workdir,
    )
    later = cap.read_transcript(transcript, offset=read.end_offset)
    # The line finished after the first read is a message like any other.
    assert [t.text for t in later.turns] == [
        "half a line\n\nWe moved to Lisbon.",
        "Noted.",
    ]


def test_offset_past_the_end_reads_from_the_start(
    transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    read = cap.read_transcript(transcript, offset=10**9)
    assert read.start_offset == 0 and len(read.turns) == 2


def test_reader_refuses_what_is_not_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(cap.CaptureError, match="does not exist"):
        cap.read_transcript(tmp_path / "missing.jsonl")
    with pytest.raises(cap.CaptureError, match="not a regular file"):
        cap.read_transcript(tmp_path)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def test_segments_close_at_user_turns_and_leave_an_unanswered_prompt(
    transcript: Path, workdir: Path
) -> None:
    rows = []
    for i in range(6):
        rows += [
            user(f"question {i} " + "q" * 400, i * 2),
            assistant("a" * 400, i * 2 + 1),
        ]
    rows.append(user("still waiting for an answer", 30))
    write_transcript(transcript, rows, cwd=workdir)
    read = cap.read_transcript(transcript)
    segments = cap.build_segments(read, max_chars=2000)
    assert len(segments) == 3
    for seg in segments:
        assert seg.turns[0].role == "user" and seg.turns[-1].role == "assistant"
    assert all(a.end_offset == b.start_offset for a, b in zip(segments, segments[1:]))
    assert segments[-1].end_offset < read.end_offset  # the last prompt waits
    assert "still waiting" not in "".join(s.text for s in segments)


def test_one_long_exchange_splits_between_assistant_turns(
    transcript: Path, workdir: Path
) -> None:
    rows = [user("Do the whole release.", 0)]
    rows += [assistant(f"step {i} " + "s" * 900, i + 1) for i in range(10)]
    write_transcript(transcript, rows, cwd=workdir)
    segments = cap.build_segments(cap.read_transcript(transcript), max_chars=2000)
    assert len(segments) > 1
    assert all(len(s.text) <= 3000 + 100 for s in segments)


# ---------------------------------------------------------------------------
# Capture runs
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel(
        [
            [
                mem(
                    "2026-09-21: The user adopted a beagle named Biscuit.",
                    "adopted a beagle named Biscuit",
                    [0],
                    "event",
                )
            ]
        ]
    )
    report = run(store, config, transcript, model, dry_run=True)
    assert [m.status for m in report.segments[0].memories] == ["would_commit"]
    assert store.load_all() == []
    assert not (store.root / cap.CAPTURES_DIR).exists()
    assert list(iter_events(store.root)) == []


def test_capture_commits_through_the_write_path(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel(
        [
            [
                mem(
                    "2026-09-21: The user adopted a beagle named Biscuit.",
                    "adopted a beagle named Biscuit",
                    [0],
                    "event",
                )
            ]
        ]
    )
    report = run(store, config, transcript, model)
    [outcome] = report.segments[0].memories
    assert outcome.status == "committed"

    [memory] = store.load_all()
    assert memory.id == outcome.id
    assert memory.scopes == [cap.CAPTURE_SCOPE]
    assert memory.category == Category.FACT
    assert memory.source.value == "inferred"
    assert memory.actor is not None and memory.actor.session == SESSION
    assert memory.actor.client == cap.CAPTURE_CLIENT
    assert memory.actor.model == "test"
    assert memory.origin is not None and memory.origin.cwd == str(workdir)

    events = list(iter_events(store.root))
    assert not [e for e in events if e["kind"] == "write"]
    writes = [e for e in events if e["kind"] == "capture_write"]
    assert [creation_id(e) for e in writes] == [memory.id]
    assert writes[0]["capture_session"] == SESSION
    assert writes[0]["segment"] == report.segments[0].sha
    assert all(e["triggered_from"] == cap.TRIGGER for e in events)
    assert all(e["attribution"] == "cli_capture" for e in events)
    [summary] = [e for e in events if e["kind"] == "capture_run"]
    assert summary["committed"] == [memory.id]
    assert summary["counts"] == {"committed": 1}

    directory = cap.capture_dir(store.root, SESSION)
    mark = json.loads((directory / cap.WATERMARK_FILENAME).read_text())
    assert mark["offset"] == transcript.stat().st_size
    assert mark["segments"][0]["memory_ids"] == [memory.id]
    segment_file = directory / mark["segments"][0]["file"]
    assert "adopted a beagle named Biscuit" in segment_file.read_text()
    if sys.platform != "win32":
        assert segment_file.stat().st_mode & 0o777 == 0o600
        assert (directory / cap.WATERMARK_FILENAME).stat().st_mode & 0o777 == 0o600


def test_rerun_reads_only_what_was_appended(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel([[], []])
    run(store, config, transcript, model)
    assert len(model.calls) == 1

    again = run(store, config, transcript, model)
    assert again.segments == [] and len(model.calls) == 1

    write_transcript(
        transcript,
        [user("We moved to Lisbon.", 5), assistant("Noted.", 6)],
        cwd=workdir,
    )
    run(store, config, transcript, model)
    assert len(model.calls) == 2
    assert "Biscuit" not in model.calls[1][1]["content"]
    assert "Lisbon" in model.calls[1][1]["content"]


def test_an_invented_quote_never_reaches_the_store(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel(
        [[mem("The user owns a cat named Tom.", "my cat Tom is asleep", [0])]]
    )
    report = run(store, config, transcript, model)
    assert report.segments[0].memories == []
    assert store.load_all() == []


def test_a_preference_is_filed_as_user_inference(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [user("I prefer tabs over spaces in every repo.", 0), assistant("Got it.", 1)],
        cwd=workdir,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "The user prefers tabs over spaces in every repo.",
                    "I prefer tabs over spaces",
                    [0],
                    "preference",
                )
            ]
        ]
    )
    run(store, config, transcript, model)
    [memory] = store.load_all()
    assert memory.category == Category.USER_INFERENCE


def test_a_user_claim_filed_as_fact_is_relabelled(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [user("I prefer dark roast coffee every morning.", 0), assistant("Nice.", 1)],
        cwd=workdir,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "The user prefers dark roast coffee every morning.",
                    "I prefer dark roast coffee",
                    [0],
                    "fact",
                )
            ]
        ]
    )
    report = run(store, config, transcript, model)
    [outcome] = report.segments[0].memories
    assert outcome.status == "committed"
    assert outcome.category == "user-inference"


def test_transient_phrasing_is_dropped_and_counted(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [user("The deploy is currently blocked on review.", 0), assistant("OK.", 1)],
        cwd=workdir,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "The deploy is currently blocked on review.",
                    "deploy is currently blocked",
                    [0],
                )
            ]
        ]
    )
    report = run(store, config, transcript, model)
    assert [m.status for m in report.segments[0].memories] == ["transient_warning"]
    assert store.load_all() == []
    assert report.counts() == {"transient_warning": 1}


def test_a_duplicate_credits_the_stored_memory_once(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    body = "The user adopted a beagle named Biscuit on 2026-09-21."
    existing = store.write(content=body, scopes=["pets"])
    write_transcript(transcript, BEAGLE, cwd=workdir)
    quote = "adopted a beagle named Biscuit"
    model = ScriptedModel(
        [
            [
                mem(body, quote, [0]),
                mem(
                    "On 2026-09-21 the user adopted a beagle named Biscuit.", quote, [0]
                ),
            ]
        ]
    )
    report = run(store, config, transcript, model)
    outcomes = report.segments[0].memories
    assert [o.status for o in outcomes] == ["duplicate", "duplicate"]
    assert {o.matched for o in outcomes} == {existing.id}
    assert store.load_one(existing.id).corroborations == 1
    assert len(store.load_all()) == 1


def test_confirmation_mode_queues_proposals_instead(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    confirm = dataclasses.replace(
        config, behavior=BehaviorConfig(require_write_confirmation=True)
    )
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel(
        [
            [
                mem(
                    "2026-09-21: The user adopted a beagle named Biscuit.",
                    "adopted a beagle named Biscuit",
                    [0],
                    "event",
                )
            ]
        ]
    )
    report = run(store, confirm, transcript, model)
    [outcome] = report.segments[0].memories
    assert outcome.status == "proposed"
    assert store.load_all() == []
    [proposal] = ProposalQueue(store.root).load()
    assert proposal.id == outcome.id
    assert proposal.source_excerpt == "adopted a beagle named Biscuit"


def test_a_failed_model_call_leaves_the_segment_for_the_next_run(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel([cap.CaptureModelError("rate limited"), []])
    report = run(store, config, transcript, model)
    assert report.segments[0].status == "failed"
    assert report.segments[0].error == "rate limited"
    mark = cap.capture_dir(store.root, SESSION) / cap.WATERMARK_FILENAME
    failed = json.loads(mark.read_text())
    assert failed["offset"] == 0 and failed["segments"] == []
    assert failed["failures"] == 1 and failed["last_error"] == "rate limited"
    assert failed["settled_size"] is None

    retry = run(store, config, transcript, model)
    assert retry.segments[0].status == "captured"
    done = json.loads(mark.read_text())
    assert done["offset"] == transcript.stat().st_size
    assert done["failures"] == 0 and done["last_error"] is None


def test_a_fence_in_the_transcript_refuses_the_segment_for_good(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)

    def refuse(turns: Any, **kwargs: Any) -> Any:
        raise sc.FenceInjectionError("fence")

    monkeypatch.setattr(sc, "build_capture_messages", refuse)
    model = ScriptedModel([])
    report = run(store, config, transcript, model)
    assert report.segments[0].status == "refused"
    assert model.calls == []
    mark = json.loads(
        (cap.capture_dir(store.root, SESSION) / cap.WATERMARK_FILENAME).read_text()
    )
    assert mark["segments"][0]["status"] == "refused"
    assert mark["offset"] == transcript.stat().st_size


def test_max_segments_leaves_the_rest_for_the_next_run(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap, "SEGMENT_MAX_CHARS", 600)
    rows = []
    for i in range(4):
        rows += [
            user(f"note {i} " + "n" * 400, i * 2),
            assistant("ok " + "o" * 200, i * 2 + 1),
        ]
    write_transcript(transcript, rows, cwd=workdir)
    model = ScriptedModel([[], [], [], []])
    first = run(store, config, transcript, model, max_segments=2)
    assert len(first.segments) == 2 and first.remaining_segments == 2
    second = run(store, config, transcript, model, max_segments=2)
    assert len(second.segments) == 2 and second.remaining_segments == 0
    assert len(model.calls) == 4


def test_session_id_must_be_safe_and_match_the_file(
    store: Store, config: Config, transcript: Path, workdir: Path, tmp_path: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    with pytest.raises(cap.CaptureError, match="named for session"):
        run(store, config, transcript, ScriptedModel([]), session_id="other-session")
    odd = tmp_path / "not a session!.jsonl"
    write_transcript(odd, BEAGLE, cwd=workdir)
    with pytest.raises(cap.CaptureError, match="safe identifier"):
        run(store, config, odd, ScriptedModel([]))
    with pytest.raises(cap.CaptureError, match="safe identifier"):
        run(store, config, odd, ScriptedModel([]), session_id="../escape")


def test_a_session_in_a_checkout_gets_its_project_scope(
    store: Store,
    config: Config,
    transcript: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    repo = tmp_path / "Garden_Planner"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    write_transcript(
        transcript,
        [user("The sprinkler runs at 6am daily.", 0), assistant("Noted.", 1)],
        cwd=repo,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "The garden sprinkler runs at 6am daily.",
                    "sprinkler runs at 6am daily",
                    [0],
                )
            ]
        ]
    )
    run(store, config, transcript, model)
    [memory] = store.load_all()
    assert memory.scopes == ["projects:garden-planner", cap.CAPTURE_SCOPE]


def test_capture_events_never_anchor_the_live_session() -> None:
    assert cap.TRIGGER in _OUT_OF_PROCESS_TRIGGERS
    events = [
        {"kind": "search", "session": "sess_live", "worktree_root": "/w"},
        {
            "kind": "write",
            "session": SESSION,
            "worktree_root": "/w",
            "triggered_from": cap.TRIGGER,
        },
    ]
    assert _latest_in_process_session(events, worktree_root="/w") == "sess_live"


def test_captures_directory_never_syncs() -> None:
    from bettermemory import sync

    assert cap.CAPTURES_DIR in sync._GITIGNORE_LINES


# ---------------------------------------------------------------------------
# The claude -p adapter
# ---------------------------------------------------------------------------


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return runner, seen


def _messages() -> list[dict[str, str]]:
    return sc.build_capture_messages([sc.Turn("user", "hello there", None)])


def test_claude_cli_reads_structured_output() -> None:
    payload = {"memories": [{"kind": "fact", "body": "b", "turns": [0], "quote": "q"}]}
    runner, seen = _fake_run(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "",
                "structured_output": payload,
                "total_cost_usd": 0.0123,
            }
        )
    )
    model = cap.ClaudeCliModel(
        model="claude-opus-5-5", binary="/bin/claude", runner=runner
    )
    reply = model.complete(_messages())
    assert json.loads(reply.text) == payload
    assert reply.cost_usd == pytest.approx(0.0123)

    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    for flag in (
        "-p",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--json-schema",
        "--system-prompt",
        "--max-budget-usd",
    ):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == sc.SYSTEM_PROMPT
    assert json.loads(argv[argv.index("--settings") + 1])["disableAllHooks"] is True
    assert "hello there" in seen["input"]
    assert seen["env"][cap.CHILD_ENV] == "1"
    assert seen["cwd"] == tempfile.gettempdir()


@pytest.mark.parametrize(
    ("stdout", "match"),
    [
        (
            json.dumps(
                {"subtype": "success", "is_error": True, "result": "OAuth expired"}
            ),
            r"^claude -p failed: OAuth expired$",
        ),
        (
            json.dumps({"subtype": "error_max_budget_usd", "is_error": False}),
            "error_max_budget_usd",
        ),
        ("not json at all", "without a JSON result"),
    ],
)
def test_claude_cli_failures_raise(stdout: str, match: str) -> None:
    runner, _ = _fake_run(stdout, returncode=1)
    model = cap.ClaudeCliModel(
        model="claude-opus-5-5", binary="/bin/claude", runner=runner
    )
    with pytest.raises(cap.CaptureModelError, match=match):
        model.complete(_messages())


def test_claude_cli_timeout_raises() -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 1)

    model = cap.ClaudeCliModel(
        model="claude-opus-5-5", binary="/bin/claude", runner=runner
    )
    with pytest.raises(cap.CaptureModelError, match="did not complete"):
        model.complete(_messages())


def test_capture_uses_the_chat_model_or_nothing() -> None:
    """The model that writes a session's memories is the one the session
    was talking to. There is no default, no API key and no fallback."""
    model = cap.resolve_model("claude-opus-5-5")
    assert isinstance(model, cap.ClaudeCliModel)
    assert model.model == "claude-opus-5-5"
    for missing in (None, ""):
        with pytest.raises(cap.CaptureError, match="own model or nothing"):
            cap.resolve_model(missing)


def test_the_reader_names_the_chat_model(transcript: Path, workdir: Path) -> None:
    write_transcript(
        transcript,
        [
            user("hello", 0),
            assistant("hi", 1, message_model="claude-sonnet-5"),
            user("and now?", 2),
            assistant("still here", 3, message_model="claude-opus-5-5"),
            assistant("API Error", 4, message_model="<synthetic>"),
        ],
        cwd=workdir,
    )
    assert cap.read_transcript(transcript).model == "claude-opus-5-5"


def test_capture_asks_the_session_s_own_model(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(
        transcript,
        [
            user("I adopted a beagle named Biscuit yesterday.", 0),
            assistant(
                "Congratulations on Biscuit!", 1, message_model="claude-opus-5-5"
            ),
        ],
        cwd=workdir,
    )
    asked: list[str | None] = []

    def resolve(chat_model: str | None) -> ScriptedModel:
        asked.append(chat_model)
        return ScriptedModel([[]])

    monkeypatch.setattr(cap, "resolve_model", resolve)
    cap.capture_transcript(
        store=store,
        config=config,
        recorder=recorder_for(store),
        transcript=transcript,
    )
    assert asked == ["claude-opus-5-5"]


def test_a_transcript_with_no_chat_model_is_not_captured(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    with pytest.raises(cap.CaptureError, match="own model or nothing"):
        cap.capture_transcript(
            store=store,
            config=config,
            recorder=recorder_for(store),
            transcript=transcript,
        )
    mark = json.loads(
        (cap.capture_dir(store.root, SESSION) / cap.WATERMARK_FILENAME).read_text()
    )
    assert mark["offset"] == 0 and mark["failures"] == 1


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _cli(args: list[str], memory_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bettermemory.cli import main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(sys, "argv", ["bettermemory", *args])
    main()


def test_cli_captures_and_prints_the_report(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel(
        [
            [
                mem(
                    "2026-09-21: The user adopted a beagle named Biscuit.",
                    "adopted a beagle named Biscuit",
                    [0],
                    "event",
                )
            ]
        ]
    )
    monkeypatch.setattr(cap, "resolve_model", lambda *a, **k: model)
    _cli(
        ["capture", "--transcript", str(transcript), "--json"], memory_dir, monkeypatch
    )
    report = json.loads(capsys.readouterr().out)
    assert report["session_id"] == SESSION
    assert report["counts"] == {"committed": 1}
    events = list(iter_events(memory_dir))
    assert {e["attribution"] for e in events} == {"cli_capture"}
    assert {e["session"] for e in events} == {SESSION}


def test_cli_does_nothing_inside_a_capture_child(
    memory_dir: Path, transcript: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cap.CHILD_ENV, "1")

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("capture ran inside its own child")

    monkeypatch.setattr(cap, "capture_transcript", boom)
    _cli(["capture", "--transcript", str(transcript)], memory_dir, monkeypatch)


def test_cli_reports_a_capture_error(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cap, "resolve_model", lambda *a, **k: ScriptedModel([]))
    with pytest.raises(SystemExit) as exit_info:
        _cli(
            ["capture", "--transcript", str(tmp_path / "gone.jsonl")],
            memory_dir,
            monkeypatch,
        )
    assert exit_info.value.code == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_exits_nonzero_when_a_segment_failed(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel([cap.CaptureModelError("down")])
    monkeypatch.setattr(cap, "resolve_model", lambda *a, **k: model)
    with pytest.raises(SystemExit) as exit_info:
        _cli(["capture", "--transcript", str(transcript)], memory_dir, monkeypatch)
    assert exit_info.value.code == 1
    assert os.path.exists(memory_dir)


# ---------------------------------------------------------------------------
# Covered and work-session rules
# ---------------------------------------------------------------------------


def test_a_fact_a_long_memory_already_states_is_covered(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    long_body = (
        "Release notes for the garden planner, gathered over the summer: the "
        "sprinkler controller moved to a Raspberry Pi 4 in the shed, the "
        "watering schedule runs at 6am on weekdays and 7am on weekends, the "
        "tomato bed gets a drip line with a 2 litre per hour emitter, and "
        "the compost bin was relocated behind the greenhouse near the fence."
    )
    stored = store.write(content=long_body, scopes=["garden"])
    write_transcript(
        transcript,
        [
            user("Remind me, the watering runs at 6am on weekdays right?", 0),
            assistant("Yes, 6am on weekdays and 7am on weekends.", 1),
        ],
        cwd=workdir,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "The watering schedule runs at 6am on weekdays and 7am on weekends.",
                    "6am on weekdays and 7am on weekends",
                    [1],
                ),
                mem(
                    "The watering schedule runs at 5am on weekdays and 7am on weekends.",
                    "the watering runs at 6am on weekdays",
                    [0],
                ),
            ]
        ]
    )
    report = run(store, config, transcript, model)
    covered, changed = report.segments[0].memories
    assert covered.status == "covered" and covered.matched == stored.id
    # A number the stored memory lacks is new information, not coverage.
    assert changed.status == "committed"


def test_short_facts_are_never_judged_covered() -> None:
    memory = _memory("The user lives in Lisbon, Portugal.")
    assert cap._covering_memory("Lives in Lisbon.", [memory]) is None


def _memory(body: str) -> Any:
    from bettermemory.models import Confidence, Memory, Source, generate_ulid, utcnow

    now = utcnow()
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["x"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def test_capture_sends_the_work_session_rules(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    model = ScriptedModel([[]])
    run(store, config, transcript, model)
    system = model.calls[0][0]["content"]
    assert system.startswith(sc.SYSTEM_PROMPT)
    assert system.endswith(sc.WORK_SESSION_RULES)


@pytest.mark.parametrize(
    ("happened_at", "kept"),
    [
        (None, False),
        ("2026-09-22", False),
        ("2026-09-21", False),
        ("2026-09", False),
        ("2026-09-23", True),
        ("2026-10", True),
        ("2027", True),
    ],
)
def test_only_plans_dated_ahead_of_the_session_are_kept(
    happened_at: str | None, kept: bool
) -> None:
    item = sc.Captured(
        kind="plan", body="Ship it.", happened_at=happened_at, turns=(0,), quote="q"
    )
    segment = cap.Segment(
        turns=(sc.Turn("user", "q", 1_790_086_400_000),),  # 2026-09-22 UTC
        start_offset=0,
        end_offset=1,
        text="",
        sha="x",
    )
    assert cap._is_open_plan(item, segment) is not kept


def test_a_plan_dated_in_its_body_is_kept() -> None:
    segment = cap.Segment(
        turns=(sc.Turn("user", "q", 1_790_086_400_000),),  # 2026-09-22 UTC
        start_offset=0,
        end_offset=1,
        text="",
        sha="x",
    )

    def plan(body: str) -> sc.Captured:
        return sc.Captured(
            kind="plan", body=body, happened_at=None, turns=(0,), quote="q"
        )

    assert not cap._is_open_plan(
        plan("Record the founder video by 2026-10-27."), segment
    )
    assert cap._is_open_plan(plan("2026-09-22: Release 8.0.0 next session."), segment)


def test_an_open_plan_is_not_written(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(
        transcript,
        [
            user("Next session we verify CI and cut the release.", 0),
            assistant("OK.", 1),
        ],
        cwd=workdir,
    )
    model = ScriptedModel(
        [
            [
                mem(
                    "Next session: verify CI and cut the release.",
                    "verify CI and cut the release",
                    [0],
                    "plan",
                )
            ]
        ]
    )
    report = run(store, config, transcript, model)
    assert [m.status for m in report.segments[0].memories] == ["open_plan"]
    assert store.load_all() == []
