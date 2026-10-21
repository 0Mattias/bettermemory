"""`bettermemory recall review` and `bettermemory recall grade` (D12).

The review draws prompt recall deliveries, session briefs and silent
prompts (Stop turns the recall delivered nothing on, in which the
conversation retrieved a project memory itself) and shows each one's
pointers with the prompt it answered, read from the session's transcript
at review time and never stored. A grade is a `recall_grade` row, which
the eval's surfaces section counts; a graded sample is not drawn again.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.eval import compute_eval
from bettermemory.events import Recorder
from bettermemory.models import generate_ulid
from bettermemory.origin import Origin
from bettermemory.recall import clean_prompt
from bettermemory.recall_counters import STRATUM_BRIEF, STRATUM_DELIVERY, STRATUM_SILENT
from bettermemory.recall_review import (
    Candidate,
    candidates,
    draw,
    draw_order,
    silent_sample_id,
)

from .test_recall_prompt import OTHER_REPO, QUERY, PromptWorld

DELIVERY_SESSION = "transcript-delivery"
BRIEF_SESSION = "transcript-brief"
SILENT_SESSION = "transcript-silent"
PROMPT = "what is our backup strategy again"
BODY_DEPLOY = "deploy pipeline signs every release with the vault key"
SECRETISH = "the-words-only-the-transcript-holds"


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def _stamp(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _transcript(root: Path, session: str, rows: list[dict[str, Any]]) -> Path:
    path = root / "-project" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _user(
    text: str, *, prompt_id: str | None = None, hours: float = 0.0, **kw: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "user",
        "timestamp": _stamp(hours),
        "message": {"content": text},
        **kw,
    }
    if prompt_id is not None:
        row["promptId"] = prompt_id
    return row


def _brief(world: PromptWorld, ids: list[str]) -> str:
    event_id = generate_ulid()
    Recorder(
        store=world.store,
        session_id=BRIEF_SESSION,
        worktree_root=str(world.worktree),
        triggered_from="session_hook",
    ).record(
        "session_brief",
        event_id=event_id,
        session_id=BRIEF_SESSION,
        harness="claude-code",
        source="startup",
        segment=0,
        ids=ids,
        chars=200,
        rows_examined=len(ids),
        unresolved=0,
        rule="rs-v1",
    )
    return event_id


def _stop(world: PromptWorld, session: str = SILENT_SESSION, **fields: Any) -> None:
    Recorder(
        store=world.store,
        session_id=session,
        worktree_root=str(world.worktree),
        triggered_from="stop_hook",
    ).record("turn_audited", verdict="ok", **fields)


def _search(world: PromptWorld, ids: list[str], session: str = SILENT_SESSION) -> None:
    Recorder(
        store=world.store, session_id="sess_srv", worktree_root=str(world.worktree)
    ).record("search", returned=ids, harness_session=session)


def _delivery_row(
    world: PromptWorld,
    *,
    session: str,
    prompt_text: str,
    ids: list[str],
    prompt_id: str | None,
    transcript_path: str | None,
    rule: str = "rs-v1",
) -> str:
    """A `prompt_recall` row as rs-v1 recorded it, on text the fixed
    recall no longer probes; the review still has to grade it."""
    event_id = generate_ulid()
    Recorder(
        store=world.store,
        session_id=session,
        worktree_root=str(world.worktree),
        triggered_from="prompt_hook",
    ).record(
        "prompt_recall",
        event_id=event_id,
        session_id=session,
        harness="claude-code",
        threshold_rule="v1",
        lookback_seconds=0,
        recent_retrieval_count=0,
        top_hits=[],
        probe_query=clean_prompt(prompt_text),
        probe_mode="hybrid",
        injected_chars=100,
        chars=100,
        delivered_reason="project",
        shown_ids=ids,
        suppressed_ids=[],
        rule=rule,
        segment=0,
        prompt_id=prompt_id,
        transcript_path=transcript_path,
    )
    return event_id


def _seed(world: PromptWorld, transcripts: Path) -> dict[str, Any]:
    """One delivery, one brief and one silent turn, with transcripts."""
    delivered = world.write()
    briefed = world.write("deploy pipeline signs every release with the vault key")
    retrieved = world.write("nightly builds publish coverage to the shared bucket")
    typed = f"{PROMPT} <pasted_content>{SECRETISH}</pasted_content>"
    path = _transcript(
        transcripts,
        DELIVERY_SESSION,
        [
            _user("an earlier prompt", prompt_id="turn-0", hours=-2),
            _user(typed, prompt_id="turn-1", hours=-1),
        ],
    )
    # The hook receives the prompt as the transcript row holds it.
    result = world.recall(
        typed,
        session_id=DELIVERY_SESSION,
        prompt_id="turn-1",
        transcript_path=str(path),
    )
    assert result.ids == (delivered.id,)
    brief_id = _brief(world, [briefed.id])
    _transcript(
        transcripts,
        BRIEF_SESSION,
        [
            _user("<system-reminder>not the human</system-reminder>", hours=1),
            _user("ship the deploy", hours=1.5),
        ],
    )
    _stop(world)  # the session's previous turn
    _search(world, [retrieved.id])
    _stop(world)
    _transcript(
        transcripts,
        SILENT_SESSION,
        [
            _user("an old prompt", hours=-3),
            _user("where do coverage reports go", hours=-1),
        ],
    )
    return {
        "delivery": result.event_id,
        "delivered": delivered.id,
        "brief": brief_id,
        "briefed": briefed.id,
        "retrieved": retrieved.id,
    }


def _cli(
    store_dir: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    from bettermemory.server import main as server_main

    saved_argv, saved_dir = sys.argv[:], os.environ.get("BETTERMEMORY_DIR")
    os.environ["BETTERMEMORY_DIR"] = str(store_dir)
    sys.argv = ["bettermemory", "recall", *args]
    code = 0
    try:
        server_main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        sys.argv = saved_argv
        if saved_dir is None:
            os.environ.pop("BETTERMEMORY_DIR", None)
        else:
            os.environ["BETTERMEMORY_DIR"] = saved_dir
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _review(
    world: PromptWorld,
    transcripts: Path,
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> dict[str, Any]:
    code, out, err = _cli(
        world.store_dir,
        [
            "review",
            "--json",
            "--since",
            "all",
            "--transcripts",
            str(transcripts),
            *args,
        ],
        capsys,
    )
    assert code == 0, err
    return json.loads(out)


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


def test_the_review_draws_each_stratum_with_its_prompt(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts = tmp_path / "transcripts"
    seeded = _seed(world, transcripts)
    doc = _review(world, transcripts, capsys)
    assert doc["population"] == {
        STRATUM_DELIVERY: 1,
        STRATUM_BRIEF: 1,
        STRATUM_SILENT: 1,
    }
    by_stratum = {s["stratum"]: s for s in doc["samples"]}

    delivery = by_stratum[STRATUM_DELIVERY]
    assert delivery["sample_id"] == seeded["delivery"]
    assert [p["id"] for p in delivery["pointers"]] == [seeded["delivered"]]
    assert delivery["pointers"][0]["summary"].startswith("backup strategy")
    # By the row its probe hashes to, as the probe read it: the paste is
    # not shown.
    assert delivery["prompt"].strip() == PROMPT
    assert SECRETISH not in json.dumps(doc)
    assert delivery["prompt_source"] == "transcript"
    assert delivery["prompt_kind"] == "owner"
    assert delivery["rule"] == "rs-v5"

    brief = by_stratum[STRATUM_BRIEF]
    assert brief["sample_id"] == seeded["brief"]
    assert brief["prompt"] == "ship the deploy"
    assert brief["prompt_kind"] == "owner"
    assert [p["id"] for p in brief["pointers"]] == [seeded["briefed"]]

    silent = by_stratum[STRATUM_SILENT]
    assert silent["prompt"] == "where do coverage reports go"
    assert silent["prompt_kind"] == "owner"
    assert silent["rule"] is None
    assert [p["id"] for p in silent["pointers"]] == [seeded["retrieved"]]
    assert silent["sample_id"].startswith("turn-")
    assert doc["ungradeable"] == []


def test_the_text_form_names_the_grade_command(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts = tmp_path / "transcripts"
    seeded = _seed(world, transcripts)
    code, out, err = _cli(
        world.store_dir,
        ["review", "--since", "all", "--transcripts", str(transcripts)],
        capsys,
    )
    assert code == 0, err
    assert f"recall delivery {seeded['delivery']}" in out
    assert "prompt (owner):" in out
    assert "first prompt after it (owner):" in out
    assert "bettermemory recall grade <sample_id>" in out


def test_a_missing_transcript_or_another_host_is_passed_over(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sample with no prompt to grade against is not drawn; it is
    listed, with why, so the record shows what the draw skipped."""
    memory = world.write()
    world.recall(
        PROMPT, session_id="transcript-elsewhere", prompt_id="p", harness="codex"
    )
    world.recall(PROMPT, session_id="transcript-gone", prompt_id="p")
    doc = _review(world, tmp_path / "none", capsys)
    assert doc["samples"] == []
    sources = {s["harness"]: s["prompt_source"] for s in doc["ungradeable"]}
    assert sources == {"codex": "other_host", "claude-code": "unavailable"}
    assert all(s["prompt"] is None for s in doc["ungradeable"])
    assert all(s["prompt_kind"] == "unknown" for s in doc["ungradeable"])
    assert doc["ungradeable"][0]["pointers"][0]["id"] == memory.id


# ---------------------------------------------------------------------------
# The prompt a delivery answered
# ---------------------------------------------------------------------------


def test_a_delivery_is_matched_by_its_probe_hash_not_the_turns_first_row(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude Code gives every user row of a turn one promptId; the row
    the review shows is the one whose text hashes to the log's probe."""
    transcripts = tmp_path / "transcripts"
    memory = world.write()
    path = _transcript(
        transcripts,
        DELIVERY_SESSION,
        [
            _user(
                "the turn's first row, not the probed one",
                prompt_id="turn-1",
                hours=-1.5,
            ),
            _user(PROMPT, prompt_id="turn-1", hours=-1),
        ],
    )
    result = world.recall(
        PROMPT,
        session_id=DELIVERY_SESSION,
        prompt_id="turn-1",
        transcript_path=str(path),
    )
    assert result.ids == (memory.id,)
    (sample,) = _review(world, transcripts, capsys)["samples"]
    assert sample["prompt"] == PROMPT
    assert sample["prompt_source"] == "transcript"
    assert sample["prompt_kind"] == "owner"


def test_the_prompt_kinds_and_the_wrapper_strip(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rows written before the gate: a delivery on a task notification is
    graded against the notification's text; one on a message from another
    session is matched to the wrapper row the transcript keeps it behind,
    by the tagged body after the wrapper's first line, and named by the
    tag the hook saw."""
    transcripts = tmp_path / "transcripts"
    memory = world.write()
    notification = (
        "<task-notification>\n<task-id>bq5</task-id>\n"
        "<status>completed</status>\n</task-notification>"
    )
    agent = (
        '<agent-message from="acf8" name="helper">\nthe restic job is ready'
        "\n</agent-message>"
    )
    cross = (
        '<cross-session-message from="uds">\nplease look at the backup'
        "\n</cross-session-message>"
    )
    session = "transcript-machine"
    path = _transcript(
        transcripts,
        session,
        [
            _user("an owner prompt", prompt_id="turn-0", hours=-3),
            _user(notification, prompt_id="turn-1", hours=-2),
            _user(
                f"Another Claude session sent a message:\n{agent}",
                prompt_id="turn-2",
                hours=-1.5,
                isMeta=True,
            ),
            _user(
                "Another Claude session sent a message while you were working:"
                f"\n{cross}",
                prompt_id="turn-3",
                hours=-1,
                isMeta=True,
            ),
        ],
    )
    expected = {}
    for kind, text, turn in [
        ("task_notification", notification, "turn-1"),
        ("agent_message", agent, "turn-2"),
        ("cross_session", cross, "turn-3"),
    ]:
        expected[
            _delivery_row(
                world,
                session=session,
                prompt_text=text,
                ids=[memory.id],
                prompt_id=turn,
                transcript_path=str(path),
            )
        ] = (kind, text)
    doc = _review(world, transcripts, capsys)
    assert doc["ungradeable"] == []
    by_id = {s["sample_id"]: s for s in doc["samples"]}
    assert set(by_id) == set(expected)
    for sample_id, (kind, text) in expected.items():
        sample = by_id[sample_id]
        assert sample["prompt_kind"] == kind
        assert sample["prompt_source"] == "transcript"
        assert sample["prompt"] == text
        assert sample["rule"] == "rs-v1"
    assert "Another Claude session" not in json.dumps(doc["samples"])
    code, out, err = _cli(
        world.store_dir,
        ["review", "--since", "all", "--transcripts", str(transcripts)],
        capsys,
    )
    assert code == 0, err
    assert "prompt (task notification):" in out
    assert "prompt (agent message):" in out
    assert "prompt (cross session):" in out


def test_a_delivery_no_row_hashes_to_takes_its_prompt_id_row_unverified(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook can receive text the transcript holds differently (a
    merged or queued message); the prompt_id row then stands in, and the
    sample says so."""
    transcripts = tmp_path / "transcripts"
    world.write()
    held = f"{PROMPT}, and also the deploy"
    path = _transcript(
        transcripts, DELIVERY_SESSION, [_user(held, prompt_id="turn-1", hours=-1)]
    )
    world.recall(
        PROMPT,
        session_id=DELIVERY_SESSION,
        prompt_id="turn-1",
        transcript_path=str(path),
    )
    (sample,) = _review(world, transcripts, capsys)["samples"]
    assert sample["prompt"] == held
    assert sample["prompt_source"] == "unverified"
    assert sample["prompt_kind"] == "owner"
    code, out, _ = _cli(
        world.store_dir,
        ["review", "--since", "all", "--transcripts", str(transcripts)],
        capsys,
    )
    assert code == 0
    assert "prompt (owner, unverified):" in out


def test_a_prompt_over_the_cut_hashes_the_same_on_both_sides(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts = tmp_path / "transcripts"
    memory = world.write()
    long_prompt = QUERY + " " + "restic replication " * 150
    assert len(long_prompt) > 2_000
    path = _transcript(
        transcripts,
        DELIVERY_SESSION,
        [_user(long_prompt, prompt_id="turn-1", hours=-1)],
    )
    result = world.recall(
        long_prompt,
        session_id=DELIVERY_SESSION,
        prompt_id="turn-1",
        transcript_path=str(path),
    )
    assert result.ids == (memory.id,)
    (sample,) = _review(world, transcripts, capsys)["samples"]
    assert sample["prompt_source"] == "transcript"
    assert sample["prompt"] == clean_prompt(long_prompt)


def test_an_ungradeable_sample_is_listed_and_the_next_is_drawn(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A delivery whose probed text is in no row of its transcript, and
    whose prompt_id names no row either, is passed over for the next
    sample of its stratum in the seeded order and listed with its kind,
    never silently dropped."""
    transcripts = tmp_path / "transcripts"
    memory = world.write()
    other = world.write(f"{BODY_DEPLOY}")
    path = _transcript(
        transcripts,
        DELIVERY_SESSION,
        [
            _user(PROMPT, prompt_id="turn-1", hours=-2),
            _user("how does the deploy pipeline sign", prompt_id="turn-2", hours=-1),
        ],
    )
    first = world.recall(
        PROMPT,
        session_id=DELIVERY_SESSION,
        prompt_id="turn-1",
        transcript_path=str(path),
    ).event_id
    second = world.recall(
        "how does the deploy pipeline sign",
        session_id=DELIVERY_SESSION,
        prompt_id="turn-2",
        transcript_path=str(path),
    ).event_id
    assert first and second
    lost = _delivery_row(
        world,
        session=DELIVERY_SESSION,
        prompt_text='<agent-message from="x">\ntext in no row\n</agent-message>',
        ids=[memory.id, other.id],
        prompt_id="turn-9",
        transcript_path=str(path),
    )
    memories = {m.id: m for m in world.store.load_all()}
    population = candidates(
        world.store.iter_events(), start=None, end=None, memories=memories
    )
    # A seed whose order puts the lost one first, so the walk must pass it.
    seed = next(
        s
        for s in range(100)
        if draw_order(population, seed=s)[STRATUM_DELIVERY][0].sample_id == lost
    )
    doc = _review(world, transcripts, capsys, "--deliveries", "2", "--seed", str(seed))
    assert {s["sample_id"] for s in doc["samples"]} == {first, second}
    (passed,) = doc["ungradeable"]
    assert passed["sample_id"] == lost and passed["prompt"] is None
    assert passed["prompt_source"] == "ungradeable"
    assert passed["prompt_kind"] == "agent_message"
    assert [p["id"] for p in passed["pointers"]] == [memory.id, other.id]
    code, out, err = _cli(
        world.store_dir,
        [
            "review",
            "--since",
            "all",
            "--deliveries",
            "2",
            "--seed",
            str(seed),
            "--transcripts",
            str(transcripts),
        ],
        capsys,
    )
    assert code == 0, err
    assert "ungradeable delivery=1" in out
    assert f"recall delivery {lost}" in out
    assert "ungradeable, agent message" in out


def test_the_probe_digest_is_the_logs_hash() -> None:
    from bettermemory.events import redact_query
    from bettermemory.recall_review import probe_digest, probed_text

    body = '<agent-message from="a">\nhello\n</agent-message>'
    assert probed_text(f"Another Claude session sent a message:\n{body}") == body
    assert probed_text("Another Claude session sent a message:") == ""
    assert probed_text("plain words") == "plain words"
    assert probe_digest(body) == redact_query(clean_prompt(body))["hash"]


def test_a_turn_the_recall_served_or_without_a_project_retrieval_is_not_silent(
    world: PromptWorld,
) -> None:
    here = world.write()
    elsewhere = world.write(
        "nightly builds publish coverage to the shared bucket",
        origin=Origin(cwd="/x", repo=OTHER_REPO, worktree_root="/x"),
    )
    # A turn whose only retrieval was another project's memory.
    _stop(world, "transcript-a")
    _search(world, [elsewhere.id], session="transcript-a")
    _stop(world, "transcript-a")
    # A turn the recall delivered on.
    _stop(world, "transcript-b")
    world.recall(PROMPT, session_id="transcript-b")
    _search(world, [here.id], session="transcript-b")
    _stop(world, "transcript-b")
    # A repeat audit is not a turn.
    _stop(world, "transcript-c")
    _search(world, [here.id], session="transcript-c")
    _stop(world, "transcript-c", repeat=True)
    # Another window's retrieval (its hooks run beside this one) is not
    # this conversation's.
    _stop(world, "transcript-d")
    _stop(world, "transcript-other")
    _search(world, [here.id], session="transcript-other")
    _stop(world, "transcript-d")
    memories = {m.id: m for m in world.store.load_all()}
    found = candidates(
        world.store.iter_events(), start=None, end=None, memories=memories
    )
    assert found[STRATUM_SILENT] == []


def test_the_silent_sample_id_is_stable() -> None:
    assert silent_sample_id("s", "t") == silent_sample_id("s", "t")
    assert silent_sample_id("s", "t") != silent_sample_id("s", "u")


def test_the_draw_takes_up_to_each_size_by_seed() -> None:
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)

    def pool(stratum: str, n: int) -> list[Candidate]:
        return [
            Candidate(
                sample_id=f"{stratum}-{i}",
                stratum=stratum,
                ts=now + timedelta(minutes=i),
                session_id="s",
                harness="claude-code",
                segment=0,
                ids=("m",),
            )
            for i in range(n)
        ]

    population = {
        STRATUM_DELIVERY: pool(STRATUM_DELIVERY, 30),
        STRATUM_BRIEF: pool(STRATUM_BRIEF, 2),
        STRATUM_SILENT: pool(STRATUM_SILENT, 9),
    }
    sizes = {STRATUM_DELIVERY: 12, STRATUM_BRIEF: 4, STRATUM_SILENT: 4}
    drawn = draw(population, sizes=sizes, seed=3, exclude={"brief-0"})
    counts = {s: sum(1 for c in drawn if c.stratum == s) for s in sizes}
    assert counts == {STRATUM_DELIVERY: 12, STRATUM_BRIEF: 1, STRATUM_SILENT: 4}
    assert [c.sample_id for c in drawn] == [
        c.sample_id for c in draw(population, sizes=sizes, seed=3, exclude={"brief-0"})
    ]
    deliveries = [c for c in drawn if c.stratum == STRATUM_DELIVERY]
    assert deliveries == sorted(deliveries, key=lambda c: c.ts)
    # The draw is the head of the seeded order, whatever its size, so the
    # samples after it are the replacements.
    order = draw_order(population, seed=3, exclude={"brief-0"})
    assert len(order[STRATUM_DELIVERY]) == 30
    assert {c.sample_id for c in deliveries} == {
        c.sample_id for c in order[STRATUM_DELIVERY][:12]
    }
    smaller = draw(population, sizes={STRATUM_DELIVERY: 5}, seed=3, exclude={"brief-0"})
    assert {c.sample_id for c in smaller} == {
        c.sample_id for c in order[STRATUM_DELIVERY][:5]
    }


# ---------------------------------------------------------------------------
# The grade
# ---------------------------------------------------------------------------


def test_a_grade_is_a_recall_grade_row_the_eval_counts(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcripts = tmp_path / "transcripts"
    seeded = _seed(world, transcripts)
    code, out, err = _cli(
        world.store_dir,
        ["grade", seeded["delivery"], "--relevant", seeded["delivered"], "--json"],
        capsys,
    )
    assert code == 0, err
    silent_id = _review(world, transcripts, capsys)["samples"][-1]["sample_id"]
    code, _, err = _cli(
        world.store_dir,
        ["grade", silent_id, "--irrelevant", seeded["retrieved"]],
        capsys,
    )
    assert code == 0, err
    grades = [e for e in world.store.iter_events() if e["kind"] == "recall_grade"]
    assert [g["stratum"] for g in grades] == [STRATUM_DELIVERY, STRATUM_SILENT]
    first = grades[0]
    assert first["sample_id"] == seeded["delivery"]
    assert first["relevant_ids"] == [seeded["delivered"]]
    assert first["irrelevant_ids"] == []
    assert first["attribution"] == "cli_recall_grade"
    assert first["rule"] == "rs-v5"
    # The rule that made the sample, beside the rule in force at grading.
    assert first["sample_rule"] == "rs-v5"
    assert "sample_rule" not in grades[1]
    # The prompt is read at review time and never stored.
    blob = json.dumps(list(world.store.iter_events()))
    assert PROMPT not in blob and "coverage reports go" not in blob

    report = compute_eval(world.store.load_all(), world.store.iter_events())
    assert report.surfaces is not None
    assert report.surfaces["grades"][STRATUM_DELIVERY]["relevant"] == 1
    assert report.surfaces["grades"][STRATUM_SILENT]["graded"] == 1
    # Graded samples are not drawn again.
    doc = _review(world, transcripts, capsys)
    assert [s["stratum"] for s in doc["samples"]] == [STRATUM_BRIEF]


def test_a_grade_names_a_sample_and_only_its_ids(
    world: PromptWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded = _seed(world, tmp_path / "transcripts")
    code, _, err = _cli(
        world.store_dir, ["grade", "no-such-sample", "--relevant", "x"], capsys
    )
    assert code == 1 and "no recall sample" in err
    code, _, err = _cli(
        world.store_dir,
        ["grade", seeded["brief"], "--relevant", seeded["delivered"]],
        capsys,
    )
    assert code == 1 and "did not show" in err
    code, _, err = _cli(world.store_dir, ["grade", seeded["brief"]], capsys)
    assert code == 2 and "at least one id" in err
    code, _, err = _cli(
        world.store_dir,
        [
            "grade",
            seeded["brief"],
            "--relevant",
            seeded["briefed"],
            "--irrelevant",
            seeded["briefed"],
        ],
        capsys,
    )
    assert code == 2 and "both" in err
    assert [e for e in world.store.iter_events() if e["kind"] == "recall_grade"] == []
