"""Session capture from the hooks (`bettermemory.capture_hook`): when a
session is captured, and the background process that captures it.

The model is scripted as in `test_capture.py`, whose transcript helpers
these tests share. `spawn_capture` is replaced by a recorder everywhere
but in the one test that starts a real background process.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import capture as cap
from bettermemory import capture_hook as ch
from bettermemory.config import CaptureConfig, Config, load_config
from bettermemory.models import utcnow
from bettermemory.store import Store

from .test_capture import (
    BEAGLE,
    SESSION,
    ScriptedModel,
    assistant,
    mem,
    run,
    user,
    write_transcript,
)
from .conftest import set_git_discovery_ceiling

OTHER = "7a1b2c3d-aaaa-4bbb-8ccc-0123456789ab"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The captured session's working directory, outside any checkout
    (as in `test_capture.py`)."""
    set_git_discovery_ceiling(tmp_path, monkeypatch)
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / f"{SESSION}.jsonl"


@pytest.fixture
def enabled(config: Config) -> Config:
    return dataclasses.replace(config, capture=CaptureConfig(enabled=True))


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ch, "spawn_capture", lambda root, args: calls.append(list(args))
    )
    return calls


def long_session(pairs: int, size: int = 700) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(pairs):
        rows += [
            user(f"note {i} " + "n" * size, (i * 2) % 60),
            assistant(f"ok {i} " + "o" * size, (i * 2 + 1) % 60),
        ]
    return rows


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def mark_of(store: Store, session: str = SESSION) -> dict[str, Any]:
    return json.loads(cap.watermark_path(store.root, session).read_text())


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_capture_is_off_by_default_and_reads_its_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load_config(path).capture == CaptureConfig()
    path.write_text(
        '[capture]\nenabled = true\nprovider = "claude-cli"\nmodel = "sonnet"\n'
        "checkpoint_tokens = 1000\nidle_minutes = 5\n",
        encoding="utf-8",
    )
    assert load_config(path).capture == CaptureConfig(
        enabled=True,
        provider="claude-cli",
        model="sonnet",
        checkpoint_tokens=1000,
        idle_minutes=5,
    )


@pytest.mark.parametrize(
    "line",
    [
        'provider = "gpt"',
        "model = 3",
        "base_url = true",
        'checkpoint_tokens = "lots"',
    ],
)
def test_a_malformed_capture_key_names_itself(tmp_path: Path, line: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[capture]\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[capture\]"):
        load_config(path)


# ---------------------------------------------------------------------------
# The watermark's new fields
# ---------------------------------------------------------------------------


def test_backoff_doubles_from_an_hour_to_a_day() -> None:
    failed = utcnow()
    mark = cap.Watermark(SESSION, failures=1, last_failure_at=failed.isoformat())
    assert mark.retry_after() == failed + timedelta(hours=1)
    mark.failures = 3
    assert mark.retry_after() == failed + timedelta(hours=4)
    mark.failures = 9
    assert mark.retry_after() == failed + timedelta(hours=24)
    assert cap.Watermark(SESSION).retry_after() is None


def test_a_checkpoint_holds_the_newest_segment_back(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap, "SEGMENT_MAX_CHARS", 1_000)
    write_transcript(transcript, long_session(3), cwd=workdir)
    total = len(cap.build_segments(cap.read_transcript(transcript), max_chars=1_000))
    assert total >= 3
    model = ScriptedModel([[] for _ in range(total)])
    held = run(store, config, transcript, model, hold_tail=True, max_segments=10)
    assert len(held.segments) == total - 1
    mark = mark_of(store)
    assert mark["offset"] == held.segments[-1].end_offset < transcript.stat().st_size
    assert mark["settled_size"] is None

    final = run(store, config, transcript, model)
    assert len(final.segments) == 1
    assert mark_of(store)["settled_size"] == transcript.stat().st_size


def test_reading_to_the_end_settles_even_with_nothing_to_capture(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    run(store, config, transcript, ScriptedModel([[]]))
    # A tail of tool traffic only: no turn, so no segment.
    write_transcript(
        transcript,
        [assistant(None, 5, tools=[{"name": "Read", "input": {"file_path": "x"}}])],
        cwd=workdir,
    )
    report = run(store, config, transcript, ScriptedModel([]))
    assert report.segments == []
    assert mark_of(store)["settled_size"] == transcript.stat().st_size


def test_a_capture_that_must_not_wait_refuses_a_busy_session(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    with hold_lock(store.root, SESSION):
        assert cap.session_busy(store.root, SESSION)
        with pytest.raises(cap.CaptureBusy):
            run(store, config, transcript, ScriptedModel([[]]), wait=False)
    assert not cap.session_busy(store.root, SESSION)


def hold_lock(root: Path, session: str) -> Any:
    """Hold a session's capture lock from outside, as a running capture
    in another process would. A second open of the lock file conflicts
    with the first even inside one process, on both platforms."""
    return cap._session_lock_nowait(cap.watermark_path(root, session))


def test_the_model_child_leaves_the_live_session_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "t")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "keep")
    env = cap.child_env()
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep"
    assert env[cap.CHILD_ENV] == "1"


# ---------------------------------------------------------------------------
# Registration and the checkpoint
# ---------------------------------------------------------------------------


def test_register_never_replaces_a_watermark(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    ch.register(store.root, SESSION, transcript)
    registered = mark_of(store)
    assert registered["offset"] == 0 and registered["segments"] == []
    assert registered["transcript"] == str(transcript)
    run(store, config, transcript, ScriptedModel([[]]))
    captured = mark_of(store)
    ch.register(store.root, SESSION, transcript)
    assert mark_of(store) == captured


def test_a_payload_must_name_its_own_transcript(tmp_path: Path) -> None:
    good = tmp_path / f"{SESSION}.jsonl"
    good.write_text("", encoding="utf-8")
    assert ch._session_transcript(SESSION, str(good)) == good.resolve()
    assert ch._session_transcript(OTHER, str(good)) is None
    assert ch._session_transcript("../x", str(good)) is None
    assert ch._session_transcript(SESSION, str(tmp_path)) is None
    assert ch._session_transcript(SESSION, None) is None


def test_a_checkpoint_is_due_only_past_the_threshold_with_two_segments(
    store: Store, transcript: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cap, "SEGMENT_MAX_CHARS", 2_000)
    monkeypatch.setattr(ch, "SEGMENT_MAX_CHARS", 2_000)
    write_transcript(transcript, long_session(1), cwd=workdir)
    due = ch.checkpoint_due
    assert not due(store.root, SESSION, transcript, threshold_chars=1_000)
    write_transcript(transcript, long_session(4), cwd=workdir)
    assert due(store.root, SESSION, transcript, threshold_chars=1_000)
    assert not due(store.root, SESSION, transcript, threshold_chars=100_000)
    with hold_lock(store.root, SESSION):
        assert not due(store.root, SESSION, transcript, threshold_chars=1_000)


def test_a_small_transcript_is_judged_without_being_parsed(
    store: Store, transcript: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("parsed a transcript under the threshold")

    monkeypatch.setattr(ch, "read_transcript", boom)
    assert not ch.checkpoint_due(
        store.root, SESSION, transcript, threshold_chars=160_000
    )


def test_a_failing_session_waits_out_its_backoff(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap, "SEGMENT_MAX_CHARS", 1_000)
    monkeypatch.setattr(ch, "SEGMENT_MAX_CHARS", 1_000)
    write_transcript(transcript, long_session(4), cwd=workdir)
    run(store, config, transcript, ScriptedModel([cap.CaptureModelError("401")]))
    assert not ch.checkpoint_due(store.root, SESSION, transcript, threshold_chars=100)
    later = utcnow() + timedelta(hours=2)
    assert ch.checkpoint_due(
        store.root, SESSION, transcript, now=later, threshold_chars=100
    )

    mark = cap.Watermark.load(cap.watermark_path(store.root, SESSION), SESSION)
    mark.failures = ch.MAX_FAILURES
    cap.watermark_path(store.root, SESSION).write_bytes(mark.to_json())
    much_later = utcnow() + timedelta(days=30)
    assert not ch.checkpoint_due(
        store.root, SESSION, transcript, now=much_later, threshold_chars=100
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_the_sweep_takes_registered_idle_sessions_newest_first(
    store: Store, tmp_path: Path, workdir: Path
) -> None:
    paths = {}
    for sid, idle in ((SESSION, 7200), (OTHER, 3600)):
        path = write_transcript(tmp_path / f"{sid}.jsonl", BEAGLE, cwd=workdir)
        ch.register(store.root, sid, path)
        age(path, idle)
        paths[sid] = path
    fresh = write_transcript(tmp_path / "fresh-session.jsonl", BEAGLE, cwd=workdir)
    ch.register(store.root, "fresh-session", fresh)
    unregistered = tmp_path / "3c3c3c3c-0000-4000-8000-000000000000.jsonl"
    write_transcript(unregistered, BEAGLE, cwd=workdir)
    age(unregistered, 7200)

    pending = ch.pending_sessions(store.root, idle_seconds=1800)
    assert [p.session_id for p in pending] == [OTHER, SESSION]
    assert pending[0].transcript == paths[OTHER]
    assert [
        p.session_id
        for p in ch.pending_sessions(store.root, idle_seconds=1800, limit=1)
    ] == [OTHER]
    with hold_lock(store.root, OTHER):
        assert [
            p.session_id for p in ch.pending_sessions(store.root, idle_seconds=1800)
        ] == [SESSION]


def test_the_sweep_leaves_a_settled_session_until_it_grows(
    store: Store, config: Config, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    ch.register(store.root, SESSION, transcript)
    age(transcript, 7200)
    assert ch.pending_sessions(store.root, idle_seconds=1800)
    run(store, config, transcript, ScriptedModel([[]]))
    age(transcript, 7200)
    assert ch.pending_sessions(store.root, idle_seconds=1800) == []
    write_transcript(
        transcript, [user("One more thing.", 9), assistant("Sure.", 10)], cwd=workdir
    )
    age(transcript, 7200)
    assert [
        p.session_id for p in ch.pending_sessions(store.root, idle_seconds=1800)
    ] == [SESSION]


def test_the_sweep_skips_a_vanished_transcript(
    store: Store, transcript: Path, workdir: Path
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    ch.register(store.root, SESSION, transcript)
    transcript.unlink()
    assert ch.pending_sessions(store.root, idle_seconds=0) == []


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------


def test_the_hooks_do_nothing_while_capture_is_off(
    store: Store,
    config: Config,
    transcript: Path,
    workdir: Path,
    spawned: list[list[str]],
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    assert not ch.on_stop(config, SESSION, str(transcript))
    assert not ch.on_session_end(config, SESSION, str(transcript))
    assert not ch.on_session_start(config)
    assert spawned == []
    assert not (store.root / cap.CAPTURES_DIR).exists()


def test_the_hooks_do_nothing_inside_a_capture_child(
    enabled: Config,
    transcript: Path,
    workdir: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cap.CHILD_ENV, "1")
    write_transcript(transcript, BEAGLE, cwd=workdir)
    assert not ch.on_stop(enabled, SESSION, str(transcript))
    assert not ch.on_session_end(enabled, SESSION, str(transcript))
    assert spawned == []


def test_stop_registers_then_starts_a_checkpoint_when_due(
    store: Store,
    enabled: Config,
    transcript: Path,
    workdir: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    assert not ch.on_stop(enabled, SESSION, str(transcript))
    assert mark_of(store)["transcript"] == str(transcript.resolve())
    assert spawned == []

    monkeypatch.setattr(cap, "SEGMENT_MAX_CHARS", 1_000)
    monkeypatch.setattr(ch, "SEGMENT_MAX_CHARS", 1_000)
    write_transcript(transcript, long_session(4), cwd=workdir)
    small = dataclasses.replace(
        enabled, capture=CaptureConfig(enabled=True, checkpoint_tokens=250)
    )
    assert ch.on_stop(small, SESSION, str(transcript))
    assert spawned == [
        [
            "--transcript",
            str(transcript.resolve()),
            "--session-id",
            SESSION,
            "--checkpoint",
        ]
    ]


def test_session_end_captures_the_rest_unless_settled_or_failing(
    store: Store,
    config: Config,
    enabled: Config,
    transcript: Path,
    workdir: Path,
    spawned: list[list[str]],
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    assert ch.on_session_end(enabled, SESSION, str(transcript))
    assert spawned[-1] == [
        "--transcript",
        str(transcript.resolve()),
        "--session-id",
        SESSION,
    ]

    run(store, config, transcript, ScriptedModel([[]]))
    assert not ch.on_session_end(enabled, SESSION, str(transcript))

    write_transcript(
        transcript, [user("Also this.", 9), assistant("Noted.", 10)], cwd=workdir
    )
    run(store, config, transcript, ScriptedModel([cap.CaptureModelError("401")]))
    assert not ch.on_session_end(enabled, SESSION, str(transcript))
    assert len(spawned) == 1


def test_session_start_sweeps_only_when_a_session_waits(
    store: Store,
    enabled: Config,
    transcript: Path,
    workdir: Path,
    spawned: list[list[str]],
) -> None:
    assert not ch.on_session_start(enabled)
    write_transcript(transcript, BEAGLE, cwd=workdir)
    ch.register(store.root, SESSION, transcript)
    age(transcript, 7200)
    assert ch.on_session_start(enabled)
    assert spawned == [["--pending"]]


# ---------------------------------------------------------------------------
# The background process
# ---------------------------------------------------------------------------


def test_spawn_detaches_and_logs(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> None:
        seen["argv"], seen["kwargs"] = argv, kwargs

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ch.spawn_capture(store.root, ["--pending"])
    assert seen["argv"] == [
        sys.executable,
        "-m",
        "bettermemory",
        "capture",
        "--pending",
    ]
    # Its own process group, so the hook's exit does not take it along:
    # a new session on POSIX, a detached new group on Windows.
    if sys.platform == "win32":
        flags = seen["kwargs"]["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS  # type: ignore[attr-defined,unused-ignore]
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]
    else:
        assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    log = store.root / cap.CAPTURES_DIR / ch.LOG_FILENAME
    assert "capture --pending" in log.read_text()
    if sys.platform != "win32":
        assert log.stat().st_mode & 0o777 == 0o600


def test_the_log_keeps_its_newest_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ch, "LOG_MAX_BYTES", 100)
    log = tmp_path / "capture.log"
    log.write_bytes(b"".join(f"line {i:03d}\n".encode() for i in range(30)))
    ch._trim_log(log)
    lines = log.read_text().splitlines()
    assert lines[-1] == "line 029" and len(log.read_bytes()) <= 50


def test_a_real_background_sweep_runs_and_logs(
    tmp_path: Path, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one test that starts the real process: `python -m bettermemory
    capture --pending` must exist, find the store through the inherited
    environment, and write its report to the log."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    ch.spawn_capture(memory_dir, ["--pending"])
    log = memory_dir / cap.CAPTURES_DIR / ch.LOG_FILENAME
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "no session is waiting" in log.read_text():
            break
        time.sleep(0.2)
    assert "no session is waiting" in log.read_text()


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _cli(args: list[str], memory_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bettermemory.cli import main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    monkeypatch.setattr(sys, "argv", ["bettermemory", *args])
    main()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[capture]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr("bettermemory.config.default_config_path", lambda: path)
    return path


def test_capture_pending_captures_each_waiting_session(
    memory_dir: Path,
    tmp_path: Path,
    workdir: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for sid in (SESSION, OTHER):
        path = write_transcript(tmp_path / f"{sid}.jsonl", BEAGLE, cwd=workdir)
        ch.register(memory_dir, sid, path)
        age(path, 7200)
    fact = mem(
        "2026-09-21: The user adopted a beagle named Biscuit.",
        "adopted a beagle named Biscuit",
        [0],
        "event",
    )
    model = ScriptedModel([[fact], [fact]])
    monkeypatch.setattr(cap, "resolve_model", lambda *a, **k: model)
    _cli(["capture", "--pending", "--json"], memory_dir, monkeypatch)
    reports = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in reports] == [OTHER, SESSION]
    assert reports[0]["counts"] == {"committed": 1}
    assert reports[1]["counts"] == {"duplicate": 1}
    assert ch.pending_sessions(memory_dir, idle_seconds=1800) == []


def test_capture_pending_with_nothing_waiting_is_quiet(
    memory_dir: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("resolved a model with nothing to capture")

    monkeypatch.setattr(cap, "resolve_model", boom)
    _cli(["capture", "--pending"], memory_dir, monkeypatch)
    assert "no session is waiting" in capsys.readouterr().out


def test_a_checkpoint_command_defers_to_a_running_capture(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    monkeypatch.setattr(cap, "resolve_model", lambda *a, **k: ScriptedModel([]))
    with hold_lock(memory_dir, SESSION):
        _cli(
            ["capture", "--transcript", str(transcript), "--checkpoint"],
            memory_dir,
            monkeypatch,
        )


def test_the_provider_comes_from_config_unless_given(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file.write_text(
        '[capture]\nprovider = "openai"\nmodel = "deepseek-chat"\n'
        'base_url = "https://api.deepseek.com"\napi_key_env = "DEEPSEEK_API_KEY"\n',
        encoding="utf-8",
    )
    write_transcript(transcript, BEAGLE, cwd=workdir)
    asked: list[tuple[Any, ...]] = []

    def resolve(provider: str, model: str | None, **kw: Any) -> ScriptedModel:
        asked.append((provider, model, kw["base_url"], kw["api_key_env"]))
        return ScriptedModel([[]])

    monkeypatch.setattr(cap, "resolve_model", resolve)
    _cli(["capture", "--transcript", str(transcript)], memory_dir, monkeypatch)
    _cli(
        [
            "capture",
            "--transcript",
            str(transcript),
            "--provider",
            "claude-cli",
            "--model",
            "haiku",
        ],
        memory_dir,
        monkeypatch,
    )
    assert asked == [
        ("openai", "deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
        ("claude-cli", "haiku", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    ]


def test_session_end_reads_the_payload_and_starts_a_capture(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    payload = {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "hook_event_name": "SessionEnd",
        "reason": "prompt_input_exit",
    }
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )
    with pytest.raises(SystemExit) as done:
        _cli(["session-end"], memory_dir, monkeypatch)
    assert done.value.code == 0
    assert spawned == [
        ["--transcript", str(transcript.resolve()), "--session-id", SESSION]
    ]


@pytest.mark.parametrize("stdin", [b"", b"not json", b'{"session_id": 3}'])
def test_session_end_shrugs_off_a_bad_payload(
    memory_dir: Path,
    config_file: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    stdin: bytes,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    with pytest.raises(SystemExit) as done:
        _cli(["session-end"], memory_dir, monkeypatch)
    assert done.value.code == 0
    assert spawned == []


def test_the_stop_hook_registers_the_session(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bettermemory import hook

    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    write_transcript(transcript, BEAGLE, cwd=workdir)
    assert (
        hook.main(
            ["--transcript-path", str(transcript), "--session-id", SESSION, "--quiet"]
        )
        == 0
    )
    assert cap.watermark_path(memory_dir, SESSION).is_file()


def test_a_dry_run_audit_registers_nothing(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bettermemory import hook

    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    write_transcript(transcript, BEAGLE, cwd=workdir)
    hook.main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            SESSION,
            "--dry-run",
            "--quiet",
        ]
    )
    assert not (memory_dir / cap.CAPTURES_DIR).exists()


def test_session_start_sweeps_without_printing(
    memory_dir: Path,
    transcript: Path,
    workdir: Path,
    config_file: Path,
    spawned: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_transcript(transcript, BEAGLE, cwd=workdir)
    ch.register(memory_dir, SESSION, transcript)
    age(transcript, 7200)
    with pytest.raises(SystemExit):
        _cli(["session-start"], memory_dir, monkeypatch)
    assert spawned == [["--pending"]]
    assert capsys.readouterr().out == ""
