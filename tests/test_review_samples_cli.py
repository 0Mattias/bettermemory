"""`bettermemory review samples` and `bettermemory review grade`.

Unit D1 (TESTS FIRST item 10; DESIGN item 14; P12). The samples command
draws reviewed writes for the owner to grade: every refusal, link and
filing up to half the sample, then pre-answered writes up to 10, then
random draws. A committed write shows its body from the store, a secret
refusal its kinds only, another refusal its body from the transcript
host's files by review_id, and a refusal from any other host no body.
Grades are review_grade event rows. Rows are made by the real
`memory_write` handler on a scratch store with tau set explicitly.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import review, review_samples
from bettermemory.eval import compute_eval

from ._review_rows import (
    BACKUP_NEW,
    BACKUP_OLD,
    FACTS,
    STATUS_NEW,
    STATUS_OLD,
    Clock,
    Harness,
    fix_origin_and_identity,
    install_clock,
    set_tau,
)

HOST = review_samples.TRANSCRIPT_HOST_CLIENT
MODEL = "writer-x"


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    fix_origin_and_identity(monkeypatch)
    set_tau(monkeypatch)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return install_clock(monkeypatch, offset=timedelta(hours=1))


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _cli(
    store_dir: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    from bettermemory.server import main as server_main

    saved_argv, saved_dir = sys.argv[:], os.environ.get("BETTERMEMORY_DIR")
    os.environ["BETTERMEMORY_DIR"] = str(store_dir)
    sys.argv = ["bettermemory", "review", *args]
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
    return code, capsys.readouterr().out


def _samples(
    h: Harness,
    capsys: pytest.CaptureFixture[str],
    *args: str,
    transcripts: Path | None = None,
) -> dict[str, Any]:
    argv = ["samples", "--json", "--since", "all", *args]
    argv += ["--transcripts", str(transcripts or h.root / "no-transcripts")]
    code, out = _cli(h.root, argv, capsys)
    assert code == 0, out
    assert MODEL not in out
    return json.loads(out)


def _write_transcript(
    path: Path,
    calls: list[tuple[str, str]],
    stamps: list[str | None] | None = None,
) -> None:
    """A host transcript holding one memory_write call per (review_id,
    content), as assistant tool_use rows, one JSON object per line; each row
    carries its `stamps` entry as `timestamp` when one is given."""
    rows: list[dict[str, Any]] = [
        {"type": "user", "message": {"content": "remember this"}},
    ]
    for n, (review_id, content) in enumerate(calls):
        stamp = {"timestamp": stamps[n]} if stamps and stamps[n] else {}
        rows.append(
            {
                **stamp,
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__bettermemory__memory_write",
                            "input": {
                                "content": content,
                                "scopes": ["tools"],
                                "review": {
                                    "review_id": review_id,
                                    "relation": {},
                                    "unsafe": 0.9,
                                    "secret": 0,
                                },
                            },
                        }
                    ]
                },
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))


async def _refuse(
    h: Harness, content: str, *, unsafe: float = 0.0, secret: float = 0.0
) -> str:
    """A two-call round trip refused by its answers; the review_id."""
    first = await h.write(content)
    res = await h.answer(first, content, unsafe=unsafe, secret=secret)
    assert res["status"] in ("instruction_warning", "credential_warning"), res
    return first["review"]["review_id"]


async def test_samples_stratified_refusals_links_filings_pre_answered_then_random(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    h.as_host(HOST, MODEL)
    old = h.seed(BACKUP_OLD)
    status = h.seed(STATUS_OLD)
    refused = await _refuse(h, FACTS[0], unsafe=0.9)
    filed = await h.reviewed_commit(BACKUP_NEW, labels={old: "conflict"})
    linked = await h.reviewed_commit(STATUS_NEW, labels={status: "update"})
    assert {"id": status, "evidence": "review"} in linked["supersedes"]
    for content in FACTS[1:12]:
        assert (await h.pre_answer(content))["status"] == "committed"
    for content in FACTS[12:15]:
        await h.reviewed_commit(content)

    doc = _samples(h, capsys, "--n", "16", "--seed", "7")
    assert doc["population"] == 17
    assert doc["strata"] == {
        review_samples.STRATUM_FLAGGED: 3,
        review_samples.STRATUM_PRE_ANSWERED: 10,
        review_samples.STRATUM_RANDOM: 3,
    }
    flagged = [
        s for s in doc["samples"] if s["stratum"] == review_samples.STRATUM_FLAGGED
    ]
    assert {s["action"] for s in flagged} == {
        review.ACTION_UNSAFE,
        review.ACTION_COMMIT,
    }
    assert refused in {s["review_id"] for s in flagged}
    assert any(s["links"] == [status] for s in flagged)
    assert any(
        s["filings"] == [{"id": old, "detector": review.DETECTOR_REVIEW}]
        for s in flagged
    )
    assert filed["status"] == "committed"
    pre = [
        s for s in doc["samples"] if s["stratum"] == review_samples.STRATUM_PRE_ANSWERED
    ]
    assert all(s["pre_answered"] and s["zero_attestation"] for s in pre)
    assert len({s["review_id"] for s in doc["samples"]}) == 16
    # Every sample shows its related set, its answers and its action.
    for sample in doc["samples"]:
        assert {"related", "unsafe", "secret", "action"} <= set(sample)
        assert all("answer" in row for row in sample["related"])

    # Refusals, links and filings take at most half the sample.
    small = _samples(h, capsys, "--n", "4", "--seed", "7")
    assert small["strata"] == {
        review_samples.STRATUM_FLAGGED: 2,
        review_samples.STRATUM_PRE_ANSWERED: 2,
    }
    # The same seed draws the same samples.
    again = _samples(h, capsys, "--n", "16", "--seed", "7")
    assert [s["review_id"] for s in again["samples"]] == [
        s["review_id"] for s in doc["samples"]
    ]


async def test_secret_samples_show_kinds_only(
    h: Harness,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h.as_host(HOST, MODEL)
    body = "The ops team keeps the staging recovery details in the shared safe."
    review_id = await _refuse(h, body, secret=0.9, unsafe=0.9)
    transcripts = tmp_path / "projects"
    _write_transcript(transcripts / "proj" / "session.jsonl", [(review_id, body)])

    asked: list[set[str]] = []
    real = review_samples.transcript_bodies

    def watching(root: Path, review_ids: Mapping[str, datetime]) -> dict[str, str]:
        asked.append(set(review_ids))
        return real(root, review_ids)

    monkeypatch.setattr(review_samples, "transcript_bodies", watching)
    doc = _samples(h, capsys, transcripts=transcripts)
    (sample,) = doc["samples"]
    assert sample["action"] == review.ACTION_SECRET
    assert sample["status"] == "credential_warning"
    assert sample["body"] is None
    assert sample["body_source"] == review_samples.BODY_KINDS_ONLY
    assert sample["kinds"] == ["review"]
    assert body not in json.dumps(doc)
    # The transcript is not even consulted for a secret.
    assert all(review_id not in ids for ids in asked)

    code, text = _cli(
        h.root,
        ["samples", "--since", "all", "--transcripts", str(transcripts)],
        capsys,
    )
    assert code == 0
    assert body not in text
    assert "kinds: review" in text
    assert MODEL not in text


async def test_refused_body_read_only_from_claude_code_transcripts_by_review_id(
    h: Harness, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    h.as_host(HOST, MODEL)
    found = await _refuse(h, FACTS[0], unsafe=0.9)
    missing = await _refuse(h, FACTS[1], unsafe=0.9)
    transcripts = tmp_path / "projects"
    decoy_id = "01" + "Z" * 24
    _write_transcript(
        transcripts / "proj-a" / "one.jsonl",
        [(decoy_id, "A decoy body that no sample may show.")],
    )
    _write_transcript(transcripts / "proj-b" / "two.jsonl", [(found, FACTS[0])])
    (transcripts / "proj-b" / "notes.txt").write_bytes(
        f"{missing} {FACTS[1]}".encode("utf-8")
    )

    doc = _samples(h, capsys, transcripts=transcripts)
    by_id = {s["review_id"]: s for s in doc["samples"]}
    assert by_id[found]["body"] == FACTS[0]
    assert by_id[found]["body_source"] == review_samples.BODY_TRANSCRIPT
    # Only *.jsonl transcripts are read, and only by review_id.
    assert by_id[missing]["body"] is None
    assert by_id[missing]["body_source"] == review_samples.BODY_UNAVAILABLE
    assert "decoy" not in json.dumps(doc)
    # Neither refused body is in the store.
    assert all(m.body not in (FACTS[0], FACTS[1]) for m in h.store.load_all())


def test_transcript_body_is_the_deciding_call_and_never_a_refused_secret(
    tmp_path: Path,
) -> None:
    rid = "01" + "A" * 24
    decided = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    secret = "The deploy key is " + "AKIA" + "IOSFODNN7EXAMPLE" + "."
    root = tmp_path / "projects"
    _write_transcript(
        root / "a" / "s.jsonl",
        [
            (rid, "The first draft the review answered."),
            (rid, secret),
            (rid, "The rephrased call that decided the review."),
            (rid, "A later call reusing the id after the decision."),
        ],
        stamps=[
            "2026-09-01T11:58:00.000Z",
            "2026-09-01T11:58:30.000Z",
            "2026-09-01T11:59:00.000Z",
            "2026-09-01T12:01:00.000Z",
        ],
    )
    bodies = review_samples.transcript_bodies(root, {rid: decided})
    assert bodies == {rid: "The rephrased call that decided the review."}
    # When the deciding call carried a secret, no body shows, not a draft.
    _write_transcript(
        root / "c" / "s.jsonl",
        [(rid, secret)],
        stamps=["2026-09-01T11:59:45.000Z"],
    )
    assert review_samples.transcript_bodies(root, {rid: decided}) == {}
    (root / "c" / "s.jsonl").unlink()
    # Unstamped calls rank by the order they were read; with the secret
    # last, no body shows.
    _write_transcript(root / "a" / "s.jsonl", [(rid, "An unstamped first call.")])
    _write_transcript(
        root / "b" / "s.jsonl",
        [(rid, "An unstamped later call."), (rid, secret)],
    )
    assert review_samples.transcript_bodies(root, {rid: decided}) == {}
    _write_transcript(root / "b" / "s.jsonl", [(rid, "An unstamped later call.")])
    bodies = review_samples.transcript_bodies(root, {rid: decided})
    assert bodies == {rid: "An unstamped later call."}


async def test_other_hosts_samples_carry_related_and_answers_without_body(
    h: Harness, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    old = h.seed(BACKUP_OLD)
    h.as_host("other-host", MODEL)
    first = await h.write(BACKUP_NEW)
    refused = await h.answer(first, BACKUP_NEW, labels={old: "conflict"}, unsafe=0.9)
    assert refused["status"] == "instruction_warning"
    review_id = first["review"]["review_id"]
    committed = await h.reviewed_commit(FACTS[0])
    transcripts = tmp_path / "projects"
    _write_transcript(transcripts / "proj" / "s.jsonl", [(review_id, BACKUP_NEW)])

    doc = _samples(h, capsys, transcripts=transcripts)
    by_id = {s["review_id"]: s for s in doc["samples"]}
    sample = by_id[review_id]
    assert sample["client"] == "other-host"
    assert sample["body"] is None
    assert sample["body_source"] == review_samples.BODY_OTHER_HOST
    assert sample["action"] == review.ACTION_UNSAFE
    assert sample["unsafe"] == 0.9 and sample["secret"] == 0.0
    assert {"id": old, "answer": "conflict", "text": BACKUP_OLD} in sample["related"]
    # A committed write from the same host shows its body from the store.
    (stored,) = [s for s in doc["samples"] if s["memory_id"] == committed["id"]]
    assert stored["body"] == FACTS[0]
    assert stored["body_source"] == review_samples.BODY_STORE


async def test_grade_rows_written_as_review_grade_events(
    h: Harness, capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    h.as_host(HOST, MODEL)
    first = await h.write(FACTS[0])
    clock.advance(minutes=2)
    await h.answer(first, FACTS[0])
    root = first["review"]["review_id"]
    pre = await h.pre_answer(FACTS[1])
    (pre_row,) = [r for r in h.events("write") if r.get("id") == pre["id"]]

    code, out = _cli(
        h.root, ["grade", root, "--answers", "right", "--action", "wrong"], capsys
    )
    assert code == 0, out
    assert out.startswith(f"graded {root}")
    assert MODEL not in out
    (grade,) = h.events("review_grade")
    assert grade["review_id"] == root
    assert grade["right_answers"] is True
    assert grade["right_action"] is False
    assert grade["right_both"] is False
    assert grade["pre_answered"] is False
    assert grade["attribution"] == "cli_review_grade"
    (write_row,) = [r for r in h.events("write") if r.get("review_id") == root]
    assert grade["sample_ts"] == write_row["ts"]

    def grades() -> dict[str, int]:
        report = compute_eval(
            memories=h.store.load_all(),
            events=h.store.iter_events(),
            now=clock.now + timedelta(hours=1),
        )
        assert report.review is not None
        return report.review["grades"]

    assert grades() == {
        "n": 1,
        "right_both": 0,
        "right_answers": 1,
        "right_action": 0,
        "pre_answered_n": 0,
    }
    # A later grade of the same write replaces the earlier one.
    clock.advance(minutes=1)
    assert (
        _cli(
            h.root, ["grade", root, "--answers", "right", "--action", "right"], capsys
        )[0]
        == 0
    )
    clock.advance(minutes=1)
    assert (
        _cli(
            h.root,
            ["grade", pre_row["review_id"], "--answers", "wrong", "--action", "right"],
            capsys,
        )[0]
        == 0
    )
    assert grades() == {
        "n": 2,
        "right_both": 1,
        "right_answers": 1,
        "right_action": 2,
        "pre_answered_n": 1,
    }
    code, _ = _cli(
        h.root,
        ["grade", "01" + "Z" * 24, "--answers", "right", "--action", "right"],
        capsys,
    )
    assert code == 1
    assert len(h.events("review_grade")) == 3
